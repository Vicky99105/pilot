"""Default adjudication: run the adjudication graph over the claim's latest stage records
and store the draft."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.adjudication_graph import run_adjudication
from app.agents.tools import DefaultAdjudicationTools
from app.database.repositories.stage_records import StageRecordRepository
from app.services.adjudication.contract import (
    DOCUMENTS_CLAUSE,
    DraftDecision,
    NotReadyForAdjudication,
    Reason,
)
from app.services.knowledge_base.contract import KnowledgeBase
from app.services.model_gateway.contract import ModelGateway
from app.shared.types import Stage

# The stage records a draft depends on. A new version of any of them means a new draft.
INPUT_STAGES = {
    "registration": Stage.REGISTRATION,
    "bill_review": Stage.BILL_REVIEW,
    "medical_review": Stage.MEDICAL_REVIEW,
    "risk": Stage.RISK,
    "classification": Stage.CLASSIFICATION,
}


class DefaultAdjudicationService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], gateway: ModelGateway, kb: KnowledgeBase
    ) -> None:
        self._gateway = gateway
        self._kb = kb
        self._records = StageRecordRepository(sessions)

    async def draft(self, claim_id: UUID) -> DraftDecision:
        latest = await self._records.latest_per_stage(claim_id)
        registered = latest.get(Stage.REGISTRATION)
        if registered is None or Stage.BILL_REVIEW not in latest:
            raise NotReadyForAdjudication(f"claim {claim_id} has no registration or sheet")
        policy = registered.payload.get("policy")
        if not policy:
            raise NotReadyForAdjudication(f"claim {claim_id} has no policy")
        versions = {
            name: latest[stage].version if stage in latest else None
            for name, stage in INPUT_STAGES.items()
        }
        stored = latest.get(Stage.ADJUDICATION)
        if stored is not None and stored.payload["input_versions"] == versions:
            return DraftDecision.model_validate(stored.payload)

        tools = DefaultAdjudicationTools(
            self._records, self._kb, claim_id=claim_id, product_uin=policy["product_uin"]
        )
        draft = await run_adjudication(claim_id, gateway=self._gateway, tools=tools)
        await self._records.append(
            claim_id, Stage.ADJUDICATION, draft.model_dump(mode="json"), created_by="model"
        )
        return draft

    async def draft_unanswered(self, claim_id: UUID, documents: list[str]) -> DraftDecision:
        stored = await self._records.latest(claim_id, Stage.ADJUDICATION)
        if stored is None:
            raise NotReadyForAdjudication(f"claim {claim_id} has no draft to close")
        current = DraftDecision.model_validate(stored.payload)
        if current.decision == "reject" and current.expected_decision == "reject":
            return current  # a retried activity
        names = "; ".join(documents) or "the documents asked for"
        reason = Reason(
            text=(
                f"The documents the query asked for were not submitted by its deadline, "
                f"after reminders: {names}. The claim cannot be decided without them."
            ),
            clause_ref=DOCUMENTS_CLAUSE,
        )
        rejected = current.model_copy(
            update={
                "decision": "reject",
                "expected_decision": "reject",
                "amount": Decimal("0.00"),
                "reasons": [reason],
                "query_items": [],
                "summary": f"Reject for non-submission of documents ({names}).",
                "self_check_errors": [],
                "guardrail_errors": [],
                "guardrail_passed": True,
                "revisions": 0,
            }
        )
        await self._records.append(
            claim_id, Stage.ADJUDICATION, rejected.model_dump(mode="json"), created_by="code"
        )
        return rejected
