"""Contract for the knowledge base: read-only lookups over the knowledge tables.

Each method is one query. Agents reach these through their tools; nothing here writes.
Callers import this module only.
"""

from typing import Protocol

from pydantic import BaseModel

from app.shared.types import Code

ICD10CM = "ICD-10-CM"
# The Phase 1 slice is a hand-made fixture. The full CMS load is Phase 2.
ICD10CM_FIXTURE_VERSION = "ICD-10-CM fixture"
MAX_INDEX_HITS = 15


class IcdIndexHit(BaseModel):
    term: str  # the main term, e.g. "Appendicitis"
    path: str  # main term and sub-terms, e.g. "Appendicitis > acute > with perforation"
    code: str | None  # None when the entry only points elsewhere
    see: str | None = None  # a cross-reference to another term


class IcdTabularEntry(BaseModel):
    code: Code  # display holds the tabular title
    billable: bool  # False for a category or subcategory that needs more characters
    includes: list[str] = []
    excludes1: list[str] = []
    excludes2: list[str] = []
    code_first: list[str] = []
    use_additional: list[str] = []
    source: str


class PolicyClause(BaseModel):
    product_uin: str
    clause_ref: str
    title: str
    text: str


class KnowledgeBase(Protocol):
    async def search_icd_index(self, term: str) -> list[IcdIndexHit]:
        """Index entries whose path holds every word of the term, best match first, at
        most MAX_INDEX_HITS."""
        ...

    async def get_icd_tabular(self, code: str) -> IcdTabularEntry | None:
        """The tabular entry for one code, or None when the code is not loaded."""
        ...

    async def get_clause(self, product_uin: str, clause_ref: str) -> PolicyClause | None: ...
