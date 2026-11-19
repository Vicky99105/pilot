from app.agents.tools import StageSnapshot
from app.services.knowledge_base.contract import PolicyClause
from app.shared.types import Stage


class FakeAdjudicationTools:
    """Implements the AdjudicationTools contract in memory. Each stage holds a list of
    snapshots: each read returns the next one, and the last one repeats, so a test can
    change a record between the graph's first read and its guardrail re-read."""

    def __init__(
        self,
        snapshots: dict[Stage, list[StageSnapshot]],
        clauses: dict[str, PolicyClause] | None = None,
    ) -> None:
        self._snapshots = {stage: list(items) for stage, items in snapshots.items()}
        self._clauses = clauses or {}
        self.reads: list[Stage] = []

    async def get_clause(self, clause_ref: str) -> PolicyClause | None:
        return self._clauses.get(clause_ref)

    async def get_stage_result(self, stage: Stage) -> StageSnapshot | None:
        self.reads.append(stage)
        items = self._snapshots.get(stage, [])
        if not items:
            return None
        return items.pop(0) if len(items) > 1 else items[0]
