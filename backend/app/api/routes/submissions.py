"""Submit area: submissions, query replies, the submitter's claims, and stored files."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData, UploadFile

from app.api.deps import Cashless, Claims, CurrentUser, Files, Intake, Permissions, Queries
from app.integrations.core.contract import PermissionService
from app.services.cashless.contract import NoOriginalsDue, OriginalsStatus
from app.services.claims.contract import ChecklistItem, ClaimDetail, ClaimSummary
from app.services.intake.contract import (
    ClaimForm,
    Submission,
    SubmissionInput,
    SubmissionRejected,
    UploadedFile,
)
from app.services.users.contract import STAFF_ROLES, User
from app.shared.types import DocType, RequestType, Role

router = APIRouter(prefix="/api", tags=["submit"])

MERGED_FIELD = "merged"  # the multipart field name for one merged PDF
SLOTS = {d.value for d in DocType}


# The requests a hospital sends on a claim its pre-auth opened.
LATER_REQUESTS = (RequestType.ENHANCEMENT, RequestType.DISCHARGE, RequestType.CANCELLATION)


class CashlessRequestForm(BaseModel):
    """The multipart "form" part of a later cashless request."""

    request_type: RequestType


class SubmissionForm(ClaimForm):
    """The multipart "form" part: claim form fields plus the request."""

    request_type: RequestType = RequestType.REIMBURSEMENT
    parent_claim_id: UUID | None = None


async def can_see(
    user: User,
    permissions: PermissionService,
    *,
    policy_number: str | None,
    hospital_id: str | None,
) -> bool:
    """Staff see every claim. A member sees claims on their policy, a hospital user
    claims at their hospital, as the permission service decides."""
    if STAFF_ROLES.intersection(user.roles):
        return True
    if user.kind == "member":
        return await permissions.can_submit_for(
            user_id=user.id, policy_number=policy_number, hospital_id=None
        )
    if user.kind == "hospital":
        return await permissions.can_submit_for(
            user_id=user.id, policy_number=None, hospital_id=hospital_id
        )
    return False


@router.post("/submissions", status_code=201, response_model=Submission)
async def submit(
    request: Request, user: CurrentUser, intake: Intake, permissions: Permissions
) -> Submission:
    """Multipart: a "form" part with SubmissionForm JSON, then one file part per document.
    A file part's field name is its slot (a document type), or "merged" for one merged PDF."""
    if user.kind not in ("member", "hospital"):
        raise HTTPException(403, "only members and hospitals submit claims")
    parts = await request.form()
    raw = parts.get("form")
    if not isinstance(raw, str):
        raise HTTPException(422, 'missing "form" part with the claim form as JSON')
    try:
        form = SubmissionForm.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from None
    if user.kind == "hospital" and form.hospital_id is None:
        form.hospital_id = user.hospital_id  # the hospital login's ROHINI ID
    if not await can_see(
        user, permissions, policy_number=form.policy_number, hospital_id=form.hospital_id
    ):
        raise HTTPException(403, "you cannot submit for this policy or hospital")
    files = await _uploads(parts)
    try:
        return await intake.accept(
            SubmissionInput(
                request_type=form.request_type,
                submitted_by=user.id,
                channel="member" if user.kind == "member" else "hospital",
                form=ClaimForm.model_validate(form.model_dump()),
                files=files,
                parent_claim_id=form.parent_claim_id,
            )
        )
    except SubmissionRejected as exc:
        raise HTTPException(422, exc.reasons) from None


@router.post("/queries/{query_id}/reply", status_code=201, response_model=Submission)
async def reply_to_query(
    query_id: UUID,
    request: Request,
    user: CurrentUser,
    intake: Intake,
    queries: Queries,
    claims: Claims,
    permissions: Permissions,
) -> Submission:
    """Multipart: one file part per document, named by slot, or "merged". The reply joins
    the claim and its workflow gets the followup_received signal."""
    if user.kind not in ("member", "hospital"):
        raise HTTPException(403, "only members and hospitals reply to queries")
    query = await queries.get(query_id)
    detail = await claims.get(query.claim_id) if query else None
    if (
        query is None
        or detail is None
        or not await can_see(
            user, permissions, policy_number=detail.policy_number, hospital_id=detail.hospital_id
        )
    ):
        raise HTTPException(404, "query not found")
    if query.status not in ("open", "overdue"):
        raise HTTPException(409, f"query is {query.status}")
    files = await _uploads(await request.form())
    try:
        return await intake.accept_followup(
            query.claim_id,
            submitted_by=user.id,
            channel="member" if user.kind == "member" else "hospital",
            files=files,
        )
    except SubmissionRejected as exc:
        raise HTTPException(422, exc.reasons) from None


@router.post("/claims/{claim_id}/requests", status_code=201, response_model=Submission)
async def send_request(
    claim_id: UUID,
    request: Request,
    user: CurrentUser,
    intake: Intake,
    claims: Claims,
    permissions: Permissions,
) -> Submission:
    """Multipart: a "form" part with {"request_type": "enhancement" | "discharge" |
    "cancellation"}, then the files by slot or "merged". The request joins the claim its
    pre-auth opened, and the claim's workflow gets the followup_received signal."""
    if user.kind != "hospital":
        raise HTTPException(403, "only hospitals send cashless requests")
    parts = await request.form()
    raw = parts.get("form")
    if not isinstance(raw, str):
        raise HTTPException(422, 'missing "form" part with the request type as JSON')
    try:
        form = CashlessRequestForm.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from None
    if form.request_type not in LATER_REQUESTS:
        raise HTTPException(422, f"request_type must be one of {[r.value for r in LATER_REQUESTS]}")
    detail = await claims.get(claim_id)
    if detail is None or not await can_see(
        user, permissions, policy_number=detail.policy_number, hospital_id=detail.hospital_id
    ):
        raise HTTPException(404, "claim not found")
    if detail.request_type is not RequestType.PREAUTH:
        raise HTTPException(409, "only a claim a pre-auth opened takes cashless requests")
    if detail.status == "closed":
        raise HTTPException(409, "the claim is closed")
    files = await _uploads(parts)
    try:
        return await intake.accept_followup(
            claim_id,
            submitted_by=user.id,
            channel="hospital",
            files=files,
            request_type=form.request_type,
        )
    except SubmissionRejected as exc:
        raise HTTPException(422, exc.reasons) from None


@router.get("/checklists/{request_type}", response_model=list[ChecklistItem])
async def get_checklist(
    request_type: RequestType, user: CurrentUser, claims: Claims
) -> list[ChecklistItem]:
    return await claims.checklist(request_type)


@router.post("/claims/{claim_id}/originals-received", response_model=OriginalsStatus)
async def originals_received(
    claim_id: UUID, user: CurrentUser, cashless: Cashless
) -> OriginalsStatus:
    """A claims operator records that the hospital's original documents arrived."""
    if Role.CLAIMS_OPERATOR not in user.roles:
        raise HTTPException(403, "a claims operator marks originals received")
    try:
        return await cashless.mark_originals_received(claim_id, user.id)
    except NoOriginalsDue as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/claims/mine", response_model=list[ClaimSummary])
async def my_claims(user: CurrentUser, claims: Claims) -> list[ClaimSummary]:
    return await claims.list_for_submitter(user.id)


@router.get("/claims/{claim_id}", response_model=ClaimDetail)
async def get_claim(
    claim_id: UUID, user: CurrentUser, claims: Claims, permissions: Permissions
) -> ClaimDetail:
    detail = await claims.get(claim_id)
    if detail is None or not await can_see(
        user, permissions, policy_number=detail.policy_number, hospital_id=detail.hospital_id
    ):
        raise HTTPException(404, "claim not found")
    return detail


@router.get("/files/{storage_key}")
async def get_file(
    storage_key: str, user: CurrentUser, claims: Claims, files: Files, permissions: Permissions
) -> Response:
    stored = await claims.stored_file(storage_key)
    if stored is None or not await can_see(
        user, permissions, policy_number=stored.policy_number, hospital_id=stored.hospital_id
    ):
        raise HTTPException(404, "file not found")
    return Response(content=await files.get(storage_key), media_type=stored.mime)


async def _uploads(parts: FormData) -> list[UploadedFile]:
    """File parts, each named by its slot (a document type) or "merged"."""
    files = []
    for name, value in parts.multi_items():
        if not isinstance(value, UploadFile):
            continue
        if name != MERGED_FIELD and name not in SLOTS:
            raise HTTPException(422, f"unknown slot {name!r}; use a document type or 'merged'")
        files.append(
            UploadedFile(
                filename=value.filename or name,
                content_type=value.content_type,
                data=await value.read(),
                slot=None if name == MERGED_FIELD else DocType(name),
            )
        )
    return files
