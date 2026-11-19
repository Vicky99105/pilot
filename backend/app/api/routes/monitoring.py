from uuid import UUID

from fastapi import APIRouter

from app.api.deps import MonitoringDep, StaffUser
from app.services.monitoring.contract import ClaimEvent, ClaimStatusRow

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/monitoring/claims", response_model=list[ClaimStatusRow])
async def claims(user: StaffUser, monitoring: MonitoringDep) -> list[ClaimStatusRow]:
    return await monitoring.claims_by_stage()


@router.get("/claims/{claim_id}/events", response_model=list[ClaimEvent])
async def claim_events(
    claim_id: UUID, user: StaffUser, monitoring: MonitoringDep
) -> list[ClaimEvent]:
    """The claim's full history, oldest first. Staff only: details are internal."""
    return await monitoring.events(claim_id)
