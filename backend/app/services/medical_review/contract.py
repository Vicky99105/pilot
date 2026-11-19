"""Contract for medical review, in two steps around bill review:

1. code(): verify the diagnosis codes against the knowledge base and build the procedure
   codes. Runs before bill review, which maps the verified procedures to a package.
2. review(): the full review against the package bill review mapped: the package's
   guideline and length-of-stay norm, a pre-existing disease re-read of the cited history
   pages, findings and query items, and the eligibility rules the verified codes can change
   (R5, R6, R7 and R9).

These guarantees hold for every MedicalCoding and MedicalFindings:
- Every verified code was returned by a knowledge-base lookup during the run that produced
  it, and names that lookup.
- Every procedure code was built one character at a time, each character chosen from the
  values a pcs_options lookup gave for the code built so far, and the full code passed
  pcs_validate. Each step names its lookup.
- Every finding cites a page of the claim or a clause or ICD reference. Uncited items go back
  to the model once, then are dropped with the reason recorded.
- Every query item names a document type the claim does not hold and no earlier query asked
  for, answered or not. With a guideline for the package, it is one of the guideline's
  mandatory documents; without one, one of MEDICAL_DOCUMENTS. Code writes its text from the
  option's label. Free-text concerns are findings, never query items.
- A mandatory document an earlier query asked for and the claim still does not hold is a
  "documents" finding for the approver, never a second query.
- Document findings are code-only: the findings model call cannot write the "documents"
  kind and never sees the guideline's mandatory document list.

Callers import this module only.
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.shared.errors import PermanentError
from app.shared.types import Code, DocType, PageRef, RuleResult

Severity = Literal["info", "query", "adverse"]
FindingKind = Literal[
    "coding",
    "hospital_codes",
    "length_of_stay",
    "pre_existing",
    "eligibility",
    "clinical",
    "documents",  # a mandatory document a query asked for and the claim still does not hold
]
# The kinds the findings model call may write. Document presence ("documents") and the
# eligibility, stay, history and hospital-code findings are written by code alone.
ModelFindingKind = Literal["clinical", "coding"]
MODEL_FINDING_KINDS: tuple[ModelFindingKind, ...] = ("clinical", "coding")
LookupTool = Literal["search_icd_index", "get_icd_tabular", "pcs_options", "pcs_validate"]
PED_CLAUSE = "Excl01"

# The documents medical review may ask the claimant for when the package has no guideline.
# A query item also excludes any of these the claim already holds or a query asked for.
MEDICAL_DOCUMENTS = (
    DocType.DISCHARGE_SUMMARY,
    DocType.OT_NOTES,
    DocType.INVESTIGATION_REPORT,
    DocType.PRESCRIPTION,
)

# Citation references other than pages: "clause:<clause_ref>", "icd:<code>", or
# "stg:<package_code>:<section>" for a section of the package's treatment guideline.
CLAUSE_PREFIX = "clause:"
ICD_PREFIX = "icd:"
STG_PREFIX = "stg:"


class Citation(BaseModel):
    page: PageRef | None = None  # a page of the claim's documents
    ref: str | None = None  # "clause:Excl01", "icd:K35.32" or "stg:SG017B:claim_documents"


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


class PcsStep(BaseModel):
    position: int  # 1 to 7
    prefix: str  # the code built before this step
    value: str  # the character chosen
    label: str
    lookup_call_id: int  # the pcs_options call that offered the value
    chosen_by: Literal["code", "model"]  # code when only one value was valid


class VerifiedProcedure(BaseModel):
    code: Code  # ICD-10-PCS
    phrase: str  # the procedure as written in the claim
    steps: list[PcsStep]
    validate_call_id: int  # the pcs_validate call that accepted the code
    citations: list[Citation]


class Finding(BaseModel):
    finding_id: int = 0  # 1-based within the record; the service numbers the findings
    kind: FindingKind
    text: str
    severity: Severity
    citations: list[Citation]
    judged_by: Literal["code", "model", "person"]  # "person" once a medical officer overrules
    rule_id: str | None = None  # eligibility findings: the rule that failed
    overruled: bool = False  # a medical officer ruled that the finding does not hold
    overrule_reason: str | None = None


class DroppedItem(BaseModel):
    item: Literal["code", "finding", "query_item", "procedure"]
    detail: str  # the code, the finding text, the query item, or the procedure
    reason: str


class DocumentRequest(BaseModel):
    """A document to ask the claimant for. Never DocType.OTHER."""

    doc_type: DocType
    text: str  # the request as the claimant reads it


class MedicalCoding(BaseModel):
    """Step 1: the verified diagnosis and procedure codes, before bill review."""

    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version coded
    registration_version: int  # the RegistrationResult version used
    verified_codes: list[VerifiedCode]
    procedure_codes: list[VerifiedProcedure] = []
    lookups: list[LookupCall]  # every tool call of the run, in order
    dropped: list[DroppedItem]
    revisions: int  # how many times the model was sent back (at most 1)


class GuidelineUsed(BaseModel):
    """The treatment guideline the review read, and the norms it compared against."""

    package_code: str  # the package bill review mapped
    guideline_code: str | None  # the HBP 2022 code whose guideline applies, if any
    sections: list[str] = []  # the guideline sections read
    mandatory_missing: list[str] = []  # mandatory claim documents the claim does not hold
    los_norm_days: int | None = None
    los_norm_source: str | None = None  # e.g. "PMJAY SG017B"


class MedicalFindings(BaseModel):
    claim_id: UUID
    digitisation_version: int  # the ClaimRecord version reviewed
    registration_version: int  # the RegistrationResult version used
    coding_version: int = 0  # the MedicalCoding version the codes came from
    bill_review_version: int = 0  # the DeductionSheet version whose package was used
    guideline: GuidelineUsed | None = None
    verified_codes: list[VerifiedCode]
    procedure_codes: list[VerifiedProcedure] = []
    findings: list[Finding]
    query_items: list[DocumentRequest]  # the documents to ask the claimant for
    rule_results: list[RuleResult]  # eligibility rules re-run on the verified facts
    lookups: list[LookupCall]  # every tool call of the run, in order
    dropped: list[DroppedItem]
    revisions: int  # how many times the model was sent back (at most 1)
    adverse: bool  # True when any finding that is not overruled has severity "adverse"


class NotReadyForMedicalReview(PermanentError):
    """The claim has no extracted record, no registration or no policy yet; or, for the
    review, no coding or no deduction sheet yet."""


class InvalidOverrule(PermanentError):
    """The overrule names no finding, an unknown finding, or gives no reason."""


class MedicalReviewService(Protocol):
    async def code(self, claim_id: UUID) -> MedicalCoding:
        """Step 1. Idempotent per (ClaimRecord version, RegistrationResult version)."""
        ...

    async def review(self, claim_id: UUID) -> MedicalFindings:
        """Step 2, after bill review. Idempotent per (MedicalCoding version, DeductionSheet
        version)."""
        ...

    async def overrule(
        self, claim_id: UUID, finding_ids: list[int], reason: str, *, actor: str
    ) -> MedicalFindings:
        """A medical officer rules that the named findings do not hold. Writes a new
        MedicalFindings version: those findings are overruled and judged_by "person", and
        the eligibility rule behind an overruled eligibility finding passes, judged by the
        person. Adjudication ignores overruled findings."""
        ...
