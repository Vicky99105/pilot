"""Contract for the knowledge base: read-only lookups over the knowledge tables.

Each lookup is a few indexed queries. Agents reach these through their tools; nothing here
writes. Callers import this module only.

Code versions: each lookup reads the version of the code set in force on `as_of` (a claim's
admission date). When no loaded version covers that date, it reads the latest loaded
version. When nothing has been loaded through the loaders (unit tests on the fixture), it
reads whatever rows are present.
"""

from datetime import date
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
