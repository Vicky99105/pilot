"""Read-only tools the agents use. Each tool records what it returned, so validators can
check that the model only used what a tool gave it."""

from datetime import date
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from app.database.repositories.stage_records import StageRecordRepository
from app.services.knowledge_base.contract import (
    ICD10CM,
    IcdIndexHit,
    IcdTabularEntry,
    IndexName,
    KnowledgeBase,
    PcsOption,
    PolicyClause,
)
from app.services.medical_review.contract import LookupCall
from app.shared.types import Stage

# --- Medical review: ICD lookups --------------------------------------------------------


def index_code(code: str) -> str:
    """An index code as a tabular code: "K35.89-" becomes "K35.89"."""
    return code.strip().upper().rstrip("-").rstrip(".")


class IcdLookupLedger:
    """Wraps the knowledge base for one run and records every call and the codes it
    returned. Only codes in the ledger may be submitted. Every lookup reads the code version
    in force on as_of, the claim's admission date."""

    def __init__(self, kb: KnowledgeBase, *, as_of: date | None = None) -> None:
        self._kb = kb
        self._as_of = as_of
        self.calls: list[LookupCall] = []
        self._tabular: dict[tuple[str, str], IcdTabularEntry | None] = {}

    async def search_icd_index(self, term: str, index: IndexName = "diseases") -> list[IcdIndexHit]:
        hits = await self._kb.search_icd_index(term, index, as_of=self._as_of)
        codes = list(dict.fromkeys(index_code(h.code) for h in hits if h.code))
        self._record("search_icd_index", term, codes)
        return hits

    async def get_icd_tabular(self, code: str, *, system: str = ICD10CM) -> IcdTabularEntry | None:
        """The entry, recorded as returning the code and the codes one level below it."""
        key = code.strip().upper()
        if (system, key) not in self._tabular:
            entry = await self._kb.get_icd_tabular(key, system=system, as_of=self._as_of)
            self._tabular[(system, key)] = entry
            returned = [entry.code.code, *entry.children] if entry else []
            self._record("get_icd_tabular", key, returned)
        return self._tabular[(system, key)]

    async def pcs_options(self, prefix: str) -> tuple[list[PcsOption], LookupCall]:
        options = await self._kb.pcs_options(prefix, as_of=self._as_of)
        call = self._record("pcs_options", prefix, [prefix + o.value for o in options])
        return options, call

    async def pcs_validate(self, code: str) -> tuple[bool, LookupCall]:
        valid = await self._kb.pcs_validate(code, as_of=self._as_of)
        return valid, self._record("pcs_validate", code, [code] if valid else [])

    def returned_by(self, code: str) -> LookupCall | None:
        """The first call that returned the code, preferring an index search."""
        key = code.strip().upper()
        found = [c for c in self.calls if key in c.returned_codes]
        searches = [c for c in found if c.tool == "search_icd_index"]
        first = searches or found
        return first[0] if first else None

    def tabular(self, code: str, *, system: str = ICD10CM) -> IcdTabularEntry | None:
        """A tabular entry already fetched in this run; never a new lookup."""
        return self._tabular.get((system, code.strip().upper()))

    def _record(self, tool: Any, argument: str, codes: list[str]) -> LookupCall:
        call = LookupCall(
            call_id=len(self.calls) + 1, tool=tool, argument=argument, returned_codes=codes
        )
        self.calls.append(call)
        return call


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

    async def get_stage_history(self, stage: Stage) -> list[StageSnapshot]:
        """Every stored record of a stage for this claim, oldest first."""
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

    async def get_stage_history(self, stage: Stage) -> list[StageSnapshot]:
        return [
            StageSnapshot(stage=stage, version=r.version, payload=r.payload)
            for r in await self._records.history(self._claim_id, stage)
        ]
