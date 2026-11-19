"""Read-only tools the agents use. Each tool records what it returned, so validators can
check that the model only used what a tool gave it."""

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.database.repositories.stage_records import StageRecordRepository
from app.services.knowledge_base.contract import (
    IcdIndexHit,
    IcdTabularEntry,
    KnowledgeBase,
    PolicyClause,
)
from app.services.medical_review.contract import LookupCall
from app.shared.types import Stage

# --- Medical review: ICD lookups --------------------------------------------------------


class IcdLookupLedger:
    """Wraps the knowledge base for one run and records every call and the codes it
    returned. Only codes in the ledger may be submitted."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb
        self.calls: list[LookupCall] = []
        self._tabular: dict[str, IcdTabularEntry | None] = {}

    async def search_icd_index(self, term: str) -> list[IcdIndexHit]:
        hits = await self._kb.search_icd_index(term)
        codes = list(dict.fromkeys(h.code for h in hits if h.code))
        self._record("search_icd_index", term, codes)
        return hits

    async def get_icd_tabular(self, code: str) -> IcdTabularEntry | None:
        key = code.strip().upper()
        if key not in self._tabular:
            entry = await self._kb.get_icd_tabular(key)
            self._tabular[key] = entry
            self._record("get_icd_tabular", key, [entry.code.code] if entry else [])
        return self._tabular[key]

    def returned_by(self, code: str) -> LookupCall | None:
        """The first call that returned the code, preferring an index search."""
        key = code.strip().upper()
        found = [c for c in self.calls if key in c.returned_codes]
        searches = [c for c in found if c.tool == "search_icd_index"]
        first = searches or found
        return first[0] if first else None

    def tabular(self, code: str) -> IcdTabularEntry | None:
        """A tabular entry already fetched in this run; never a new lookup."""
        return self._tabular.get(code.strip().upper())

    def _record(self, tool: Any, argument: str, codes: list[str]) -> None:
        self.calls.append(
            LookupCall(
                call_id=len(self.calls) + 1, tool=tool, argument=argument, returned_codes=codes
            )
        )


# --- Adjudication: clauses and stage results --------------------------------------------


class StageSnapshot(BaseModel):
    stage: Stage
    version: int
    payload: dict[str, Any]


class AdjudicationTools(Protocol):
    async def get_clause(self, clause_ref: str) -> PolicyClause | None: ...

    async def get_stage_result(self, stage: Stage) -> StageSnapshot | None:
        """The latest stored record of a stage for this claim."""
        ...


class DefaultAdjudicationTools:
    def __init__(
        self,
        records: StageRecordRepository,
        kb: KnowledgeBase,
        *,
        claim_id: UUID,
        product_uin: str,
    ) -> None:
        self._records = records
        self._kb = kb
        self._claim_id = claim_id
        self._product_uin = product_uin

    async def get_clause(self, clause_ref: str) -> PolicyClause | None:
        return await self._kb.get_clause(self._product_uin, clause_ref)

    async def get_stage_result(self, stage: Stage) -> StageSnapshot | None:
        record = await self._records.latest(self._claim_id, stage)
        if record is None:
            return None
        return StageSnapshot(stage=stage, version=record.version, payload=record.payload)
