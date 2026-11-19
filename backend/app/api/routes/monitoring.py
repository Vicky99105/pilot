from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.deps import Claims, MonitoringDep, StaffUser
from app.services.claims.contract import ClaimPages
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


@router.get("/claims/{claim_id}/pages", response_model=ClaimPages)
async def claim_pages(claim_id: UUID, user: StaffUser, claims: Claims) -> ClaimPages:
    """The page viewer's pages: image keys, and each page's place in its document so a
    PageRef(document_id, page) resolves to an image. Staff only."""
    pages = await claims.pages(claim_id)
    if pages is None:
        raise HTTPException(404, "claim has no classified pages yet")
    return pages
