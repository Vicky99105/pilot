"""Default adjudication: run the adjudication graph over the claim's latest stage records
and store the draft."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.adjudication_graph import run_adjudication
from app.agents.tools import DefaultAdjudicationTools
from app.database.repositories.stage_records import StageRecordRepository
from app.services.adjudication.contract import DraftDecision, NotReadyForAdjudication
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
