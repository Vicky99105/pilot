"""Submit area: submissions, the submitter's claims, and stored files."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.deps import Claims, CurrentUser, Files, Intake, Permissions
from app.integrations.core.contract import PermissionService
from app.services.claims.contract import ClaimDetail, ClaimSummary
from app.services.intake.contract import (
    ClaimForm,
    Submission,
    SubmissionInput,
    SubmissionRejected,
    UploadedFile,
)
from app.services.users.contract import STAFF_ROLES, User
from app.shared.types import DocType, RequestType

router = APIRouter(prefix="/api", tags=["submit"])

MERGED_FIELD = "merged"  # the multipart field name for one merged PDF
SLOTS = {d.value for d in DocType}


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
    if not await can_see(
        user, permissions, policy_number=form.policy_number, hospital_id=form.hospital_id
    ):
        raise HTTPException(403, "you cannot submit for this policy or hospital")
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
