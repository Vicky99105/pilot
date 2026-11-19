"""Contract for the knowledge base: read-only lookups over the knowledge tables.

Each lookup is a few indexed queries. Agents reach these through their tools; nothing here
writes. Callers import this module only.

Code versions: each lookup reads the version of the code set in force on `as_of` (a claim's
admission date). When no loaded version covers that date, it reads the latest loaded
version. When nothing has been loaded through the loaders (unit tests on the fixture), it
reads whatever rows are present.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

from app.shared.types import Code

ICD10CM = "ICD-10-CM"
ICD10PCS = "ICD-10-PCS"
# The hand-made fixture used by unit tests. The running system loads the CMS release.
ICD10CM_FIXTURE_VERSION = "ICD-10-CM fixture"
MAX_INDEX_HITS = 15
PCS_LENGTH = 7

IndexName = Literal["diseases", "external_causes", "drugs", "neoplasms"]
MAX_TARIFF_CANDIDATES = 10
MAX_NON_PAYABLE_CANDIDATES = 5


class IcdIndexHit(BaseModel):
    index: IndexName = "diseases"
    term: str  # the main term, e.g. "Appendicitis"
    path: str  # main term and sub-terms, e.g. "Appendicitis > acute > with perforation"
    code: str | None  # as written in the index, e.g. "K35.80" or "S72.00-"; None for a
    # pure cross-reference
    column: str | None = None  # drug and neoplasm tables: the column, e.g. "Adverse effect"
    manifestation: str | None = None  # a code to add, written in brackets in the index
    see: str | None = None  # a cross-reference to another term
    see_also: str | None = None


class IcdTabularEntry(BaseModel):
    code: Code  # display holds the tabular title
    billable: bool  # False for a category or subcategory that needs more characters
    inclusion_terms: list[str] = []
    includes: list[str] = []
    excludes1: list[str] = []
    excludes2: list[str] = []
    code_first: list[str] = []
    use_additional: list[str] = []
    code_also: list[str] = []
    notes: list[str] = []
    seven_chr_note: list[str] = []
    seven_chr_def: dict[str, str] = {}  # 7th character -> meaning, own or inherited
    chapter: str | None = None
    section: str | None = None
    parent: str | None = None
    children: list[str] = []  # the codes one level below
    source: str


class PcsOption(BaseModel):
    """One valid value for the next character of a PCS code."""

    position: int  # 1 to 7
    axis: str  # e.g. "Operation", "Approach"
    value: str  # the character
    label: str  # e.g. "Resection"
    definition: str | None = None
    explanation: str | None = None
    includes: list[str] = []


class PolicyClause(BaseModel):
    product_uin: str
    clause_ref: str
    title: str
    text: str


# --- Public reference data (slice P2-3) ---------------------------------------------------


class TariffRate(BaseModel):
    tier: str  # city tier X, Y, Z, or "all"
    accreditation: str  # e.g. non_nabh, nabh, super_speciality, entry_nabh
    ward: str
    rate: Decimal | None


class TariffCandidate(BaseModel):
    """A package or procedure from a scheme's rate list that may fit the treatment."""

    scheme: str  # CGHS, PMJAY, MEDISEP
    version: str
    procedure_code: str
    package_code: str | None
    name: str
    package_name: str | None = None
    specialty: str | None = None
    matched_on: Literal["code", "name"]
    score: float  # 100 for a code; name similarity 0 to 100 otherwise
    # The rate that applies at the hospital where the scheme knows it: for CGHS the NABH
    # rate for the hospital's city tier; otherwise the scheme's base rate (rate_basis says).
    rate: Decimal | None
    rate_basis: str | None = None
    tier: str | None = None  # the hospital's CGHS city tier, when a hospital was given
    rates: list[TariffRate] = []  # the priced variants that apply (that tier, or "all")
    alos_days: int | None = None
    implant: str | None = None
    multiple_procedures: bool | None = None
    addon: bool | None = None
    standalone: bool | None = None
    source: str
    page: int | None = None


class GuidelineSection(BaseModel):
    package_code: str  # HBP 2022 when the STG's code was matched, else the STG's own code
    old_code: str | None
    procedure_name: str
    # preauth_documents, claim_documents, alos, doctor_qualification, clinical_pointers,
    # preauth_checks, claim_checks, tms_rules
    section: str
    items: list[str]
    match_score: float | None = None
    file_name: str
    page: int | None
    source: str


class LosNorm(BaseModel):
    scheme: str
    version: str
    package_code: str
    name: str
    los_days: int | None  # 0 for day care; the upper end of a range
    los_text: str | None
    matched_on: Literal["code", "name"]
    source: str
    page: int | None


class DrugPrice(BaseModel):
    medicine: str
    dosage_form: str
    unit: str
    ceiling_price: Decimal  # rupees per unit, before GST
    notification: str
    page: int | None


class DrugCheck(BaseModel):
    """A pharmacy line against NPPA ceiling prices and NLEM 2022. ceiling_price and
    over_ceiling are set only when exactly one formulation fits the name and strength."""

    name: str
    unit_price: Decimal | None
    matches: list[DrugPrice]
    ceiling_price: Decimal | None = None
    over_ceiling: bool | None = None
    nlem: bool = False
    nlem_medicine: str | None = None


class NonPayableCandidate(BaseModel):
    list_name: str
    item_number: int
    item: str
    condition: str | None
    payable_in_product: str
    score: float  # 0 to 100


class Exclusion(BaseModel):
    scope: str  # "IRDAI" or a product UIN
    code: str
    title: str
    summary: str
    page: int | None
    source: str


# --- Hospital agreements and the package crosswalk (slice P2-4) -------------------------

AgreementScheme = Literal["CGHS", "PMJAY", "MEDISEP", "OWN"]


class HospitalAgreement(BaseModel):
    """The scheme a hospital's packages are priced from, and its non-package discount."""

    hospital_id: str
    scheme: AgreementScheme  # OWN: the hospital's own tariff, no package agreement
    accreditation: str | None  # CGHS: nabh, non_nabh or super_speciality
    package_ward: str | None  # the ward the agreed package rate is for
    non_package_discount_percent: Decimal
    valid_from: date
    note: str | None = None
    source: str


class PackageCrosswalk(BaseModel):
    """Our own crosswalk for one package: the diagnoses it treats and the HBP 2022 code whose
    guideline and length of stay apply."""

    scheme: str
    package_code: str
    icd10cm: list[str]  # ranges, as code_in_ranges reads them
    guideline_code: str | None
    note: str | None = None
    source: str


class KnowledgeBase(Protocol):
    async def search_icd_index(
        self, term: str, index: IndexName = "diseases", *, as_of: date | None = None
    ) -> list[IcdIndexHit]:
        """Entries of one index whose path or nonessential modifiers hold every word of the
        term, best match first, at most MAX_INDEX_HITS."""
        ...

    async def get_icd_tabular(
        self, code: str, *, system: str = ICD10CM, as_of: date | None = None
    ) -> IcdTabularEntry | None:
        """The tabular entry for one code with all its notes, or None when the code is not
        loaded. For ICD-10-PCS it gives the code's title and whether it is valid."""
        ...

    async def pcs_options(self, prefix: str, *, as_of: date | None = None) -> list[PcsOption]:
        """The valid values for the character after prefix, in code order. Empty when the
        prefix is a full code or no PCS table starts with it."""
        ...

    async def pcs_validate(self, code: str, *, as_of: date | None = None) -> bool:
        """True when a PCS table row makes this seven character code."""
        ...

    def code_in_ranges(self, code: str, ranges: list[str]) -> bool:
        """True when the code falls in any range. A range is one code, which covers the
        codes below it ("K35", "K35.-"), or two codes joined by a hyphen ("K35-K38"),
        which covers both ends and everything below them in code order."""
        ...

    async def get_clause(self, product_uin: str, clause_ref: str) -> PolicyClause | None: ...

    async def tariff_candidates(
        self, procedure_codes: list[str], names: list[str], hospital_id: str | None
    ) -> list[TariffCandidate]:
        """Packages whose procedure or package code is one of the codes (any scheme), then
        packages whose package and procedure names best match each name, at most
        MAX_TARIFF_CANDIDATES. Rates follow the hospital's city tier when a hospital is
        given."""
        ...

    async def get_guideline(self, package_code: str) -> list[GuidelineSection]:
        """The STG sections for a package code (HBP 2022, or an STG's own HBP 2.0 code)."""
        ...

    async def get_los_norm(self, package_code_or_name: str) -> LosNorm | None:
        """The length-of-stay norm for a package code, else the best name match, else None."""
        ...

    async def check_drug(self, name: str, unit_price: Decimal | None) -> DrugCheck:
        """The NPPA formulations that fit a pharmacy line's name and strength, and whether
        the medicine is in NLEM 2022."""
        ...

    async def match_non_payable(self, text: str) -> list[NonPayableCandidate]:
        """Non-payable list items that a bill line may be, best first, at most
        MAX_NON_PAYABLE_CANDIDATES; empty when none is close."""
        ...

    async def exclusion(self, code: str) -> Exclusion | None:
        """A standard exclusion (Excl01 to Excl18) or a product's specific one, by code."""
        ...

    async def hospital_agreement(
        self, hospital_id: str, *, as_of: date | None = None
    ) -> HospitalAgreement | None:
        """The hospital's agreement in force on as_of (the latest when as_of is None), or None
        when it has none."""
        ...

    async def package_crosswalk(self, scheme: str, package_code: str) -> PackageCrosswalk | None:
        """Our crosswalk row for a package, or None when the package is not in it."""
        ...

    async def packages_for_diagnoses(
        self, scheme: str, diagnosis_codes: list[str]
    ) -> list[PackageCrosswalk]:
        """Our crosswalk rows of the scheme whose ICD-10-CM ranges hold any of the codes, in
        package code order. Empty when no code is given."""
        ...

    async def city_tier(self, hospital_id: str) -> str | None:
        """The CGHS city tier (X, Y or Z) of the hospital's city, or None when the rate list
        names no tier for it."""
        ...
