from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.deps import Claims, MonitoringDep, StaffUser
from app.database.models import utcnow
from app.services.claims.contract import ClaimPages
from app.services.monitoring.contract import ClaimEvent, ClaimStatusRow, PilotMetrics
from app.shared.types import Role

router = APIRouter(prefix="/api", tags=["monitoring"])

DEFAULT_PERIOD = timedelta(days=7)
LONGEST_PERIOD = timedelta(days=366)
METRICS_ROLES = {Role.SUPERVISOR, Role.ADMIN}


@router.get("/monitoring/claims", response_model=list[ClaimStatusRow])
async def claims(user: StaffUser, monitoring: MonitoringDep) -> list[ClaimStatusRow]:
    return await monitoring.claims_by_stage()


@router.get("/claims/{claim_id}/events", response_model=list[ClaimEvent])
async def claim_events(
    claim_id: UUID, user: StaffUser, monitoring: MonitoringDep
) -> list[ClaimEvent]:
    """The claim's full history, oldest first. Staff only: details are internal."""
    return await monitoring.events(claim_id)


@router.get("/claims/{claim_id}/pages", response_model=ClaimPages)
async def claim_pages(claim_id: UUID, user: StaffUser, claims: Claims) -> ClaimPages:
    """The page viewer's pages: image keys, and each page's place in its document so a
    PageRef(document_id, page) resolves to an image. Staff only."""
    pages = await claims.pages(claim_id)
    if pages is None:
        raise HTTPException(404, "claim has no classified pages yet")
    return pages


@router.get("/monitoring/metrics", response_model=PilotMetrics)
async def metrics(
    user: StaffUser,
    monitoring: MonitoringDep,
    start: datetime | None = None,
    end: datetime | None = None,
) -> PilotMetrics:
    """Pilot metrics over [start, end). Supervisors and admins only. By default the last 7
    days up to now; a start alone runs to now, an end alone covers the 7 days before it."""
    if not METRICS_ROLES.intersection(user.roles):
        raise HTTPException(403, "supervisors and admins only")
    end = end or utcnow()
    start = start or end - DEFAULT_PERIOD
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start and end need a time zone, e.g. 2026-11-01T00:00:00Z")
    if not start < end <= start + LONGEST_PERIOD:
        raise HTTPException(422, "start must be before end, and the period at most 366 days")
    return await monitoring.metrics(start=start, end=end)
