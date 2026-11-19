"""Contract for medical review: verify the diagnosis codes against the knowledge base, check
the clinical facts, and re-run the eligibility rule the verified facts can change (R5).

Two guarantees hold for every MedicalFindings:
- Every verified code was returned by a knowledge-base lookup during the run that produced
  it, and names that lookup.
- Every finding cites a page of the claim or a clause or ICD reference. Uncited items go back
  to the model once, then are dropped with the reason recorded.
- Every query item names one of MEDICAL_DOCUMENTS that the claim does not hold and the
  claimant has not answered. Free-text concerns are findings, never query items.

Callers import this module only.
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import Code, DocType, PageRef, RuleResult

Severity = Literal["info", "query", "adverse"]
FindingKind = Literal[
    "coding", "hospital_codes", "length_of_stay", "pre_existing", "eligibility", "clinical"
]
LookupTool = Literal["search_icd_index", "get_icd_tabular"]

# The documents medical review may ask the claimant for. A query item also excludes any of
# these the claim already holds or the claimant already answered.
MEDICAL_DOCUMENTS = (
    DocType.DISCHARGE_SUMMARY,
    DocType.OT_NOTES,
    DocType.INVESTIGATION_REPORT,
    DocType.PRESCRIPTION,
)

# Citation references other than pages: "clause:<clause_ref>" or "icd:<code>".
CLAUSE_PREFIX = "clause:"
ICD_PREFIX = "icd:"


class Citation(BaseModel):
    page: PageRef | None = None  # a page of the claim's documents
    ref: str | None = None  # "clause:Excl01" or "icd:K35.32"


class LookupCall(BaseModel):
    call_id: int  # 1-based, in call order within the run
    tool: LookupTool
    argument: str
    returned_codes: list[str]


class VerifiedCode(BaseModel):
    code: Code
    phrase: str  # the diagnosis as written in the claim
    lookup_call_id: int  # the LookupCall that returned this code
    citations: list[Citation]
    rationale: str


class Finding(BaseModel):
    kind: FindingKind
    text: str
    severity: Severity
    citations: list[Citation]
    judged_by: Literal["code", "model"]


class DroppedItem(BaseModel):
    item: Literal["code", "finding", "query_item"]
    detail: str  # the code, the finding text, or the query item
    reason: str


class DocumentRequest(BaseModel):
    """A document to ask the claimant for. Never DocType.OTHER."""

    doc_type: DocType
    text: str  # the request as the claimant reads it


class MedicalFindings(BaseModel):
    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version reviewed
    registration_version: int  # the RegistrationResult version used
    verified_codes: list[VerifiedCode]
    findings: list[Finding]
    query_items: list[DocumentRequest]  # the documents to ask the claimant for
    rule_results: list[RuleResult]  # eligibility rules re-run on the verified facts
    lookups: list[LookupCall]  # every tool call of the run, in order
    dropped: list[DroppedItem]
    revisions: int  # how many times the model was sent back (at most 1)
    adverse: bool  # True when any finding has severity "adverse"


class NotReadyForMedicalReview(PermanentError):
    """The claim has no extracted record, no registration or no policy yet."""


class MedicalReviewService(Protocol):
    async def review(self, claim_id: UUID) -> MedicalFindings:
        """Idempotent per (ClaimRecord version, RegistrationResult version)."""
        ...
