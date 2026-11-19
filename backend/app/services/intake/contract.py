"""Contract for intake: accept a submission, store its files and start the claim workflow.

Callers import this module only.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.shared.types import DocType, RequestType

Channel = Literal["member", "hospital"]

ClaimCategory = Literal["inpatient", "day_care", "pre_post_hospitalisation"]
Cause = Literal["illness", "injury", "maternity"]
IFSC_PATTERN = r"^[A-Z]{4}0[A-Z0-9]{6}$"

# A member's new claim must carry the accepted declaration; follow-ups reuse the claim's form.
DECLARATION_REQUIRED = "the declaration must be accepted"

MB = 1024 * 1024
MAX_FILE_BYTES: dict[Channel, int] = {"hospital": 15 * MB, "member": 10 * MB}


class ClaimForm(BaseModel):
    """Form fields the submitter types in. Documents may disagree; digitisation checks."""

    policy_number: str
    member_id: str
    patient_name: str
    hospital_id: str | None = None  # ROHINI ID, when known
    hospital_name: str | None = None
    admission_date: date
    discharge_date: date | None = None
    claimed_amount: Decimal
    # Member form extras. Optional so hospital requests and older submissions still parse.
    hospital_city: str | None = None
    claim_category: ClaimCategory | None = None
    cause: Cause | None = None
    bank_account_number: str | None = Field(default=None, pattern=r"^[0-9]{6,18}$")
    ifsc: str | None = Field(default=None, pattern=IFSC_PATTERN)
    declaration_accepted: bool = False

    @field_validator("claimed_amount")
    @classmethod
    def _rupees(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class UploadedFile(BaseModel):
    filename: str
    content_type: str | None = None  # as declared by the client; the bytes decide
    data: bytes
    slot: DocType | None = None  # None for a merged PDF


class SubmissionInput(BaseModel):
    request_type: RequestType
    submitted_by: UUID
    channel: Channel
    form: ClaimForm
    files: list[UploadedFile]
    parent_claim_id: UUID | None = None


class SubmittedFile(BaseModel):
    file_id: UUID
    filename: str | None
    sha256: str  # of the bytes as uploaded, before any conversion
    slot: DocType | None
    mime: str  # of the stored bytes
    size_bytes: int
    storage_key: str
    quality_warning: str | None = None


class Submission(BaseModel):
    submission_id: UUID
    claim_id: UUID
    request_type: RequestType
    parent_claim_id: UUID | None
    form: ClaimForm
    files: list[SubmittedFile]
    missing_documents: list[DocType]
    warnings: list[str]
    deadline: datetime | None
    clock_deadline: datetime | None = None  # a cashless request's own clock
    duplicate_of: UUID | None = None  # set when this repeats an earlier submission
    workflow_id: str | None = None  # None when the workflow could not be started yet
    created_at: datetime


class SubmissionRejected(Exception):
    """A file has an unsupported type or is too large. Nothing is stored."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class IntakeService(Protocol):
    async def accept(self, submission: SubmissionInput) -> Submission:
        """A submission with parent_claim_id is a follow-up: it joins the parent claim and
        the parent's workflow gets the followup_received signal."""
        ...

    async def accept_followup(
        self,
        parent_claim_id: UUID,
        *,
        submitted_by: UUID,
        channel: Channel,
        files: list[UploadedFile],
        request_type: RequestType = RequestType.QUERY_REPLY,
    ) -> Submission:
        """A follow-up with the parent claim's form: a query reply, or a cashless request
        (enhancement, discharge, cancellation) on the claim its pre-auth opened. A
        cancellation needs no files; any other submission needs at least one."""
        ...

    async def get(self, submission_id: UUID) -> Submission | None: ...

    async def start_missing_workflows(self) -> list[UUID]:
        """Start ClaimWorkflow for every claim still in status "received" with no workflow,
        such as a claim saved while Temporal was down. The workflow ID is the claim ID, so a
        start Temporal refuses as a duplicate is ignored and the ID is recorded. Returns the
        claims whose workflow ID is now recorded."""
        ...


class WorkflowAlreadyStarted(Exception):
    """A workflow with this ID exists already; the duplicate start was refused."""


class WorkflowStarter(Protocol):
    async def start_claim(
        self, *, workflow_id: str, submission_id: UUID, request_type: RequestType
    ) -> None: ...


class FollowupSignaller(Protocol):
    """Sends followup_received(submission_id) to a claim's running workflow."""

    async def followup_received(self, *, workflow_id: str, submission_id: UUID) -> None: ...
