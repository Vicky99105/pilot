from fastapi import APIRouter

from app.api.deps import MonitoringDep, StaffUser
from app.services.monitoring.contract import ClaimStatusRow

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/claims", response_model=list[ClaimStatusRow])
async def claims(user: StaffUser, monitoring: MonitoringDep) -> list[ClaimStatusRow]:
    return await monitoring.claims_by_stage()
