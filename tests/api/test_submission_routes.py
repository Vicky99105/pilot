import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.main import create_app
from app.services.claims.service import DefaultClaimQueryService
from app.services.communication.service import DefaultCommunicationService
from app.services.intake.service import DefaultIntakeService
from app.services.query.contract import QueryItem, QueryService
from app.services.query.service import DefaultQueryService
from app.services.users.contract import User
from app.shared.types import Role
from tests.api.test_task_routes import UNREACHABLE
from tests.docs import make_pdf
from tests.fakes import FakeHealthService
from tests.fakes.core import FakePermissionService
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.notifications import FakeNotificationService
from tests.fakes.tasks import FakeTaskService
from tests.fakes.users import FakeUserService

POLICY, ROHINI = "MOCK/AS/2026/000101", "8900000000011"
MEMBER = User(id=uuid4(), name="Asha", kind="member", roles=[Role.MEMBER])
STRANGER = User(id=uuid4(), name="Ravi", kind="member", roles=[Role.MEMBER])
HOSPITAL = User(
    id=uuid4(), name="Desk", kind="hospital", roles=[Role.HOSPITAL_USER], hospital_id=ROHINI
)
OPS = User(id=uuid4(), name="Ops", kind="staff", roles=[Role.CLAIMS_OPERATOR])
FORM = {
    "policy_number": POLICY,
    "member_id": "MOCK-M-000101-01",
    "patient_name": "Asha Verma",
    "hospital_id": ROHINI,
    "admission_date": "2026-11-02",
    "discharge_date": "2026-11-05",
    "claimed_amount": "48250",
    "declaration_accepted": True,
}


class Api:
    def __init__(
        self,
        client: httpx.AsyncClient,
        files: FakeFileStore,
        starter: FakeWorkflowStarter,
        queries: QueryService,
    ) -> None:
        self.client = client
        self.files = files
        self.starter = starter
        self.queries = queries

    async def reply(
        self, user: User, query_id: object, files: list[tuple[str, bytes]]
    ) -> httpx.Response:
        return await self.client.post(
            f"/api/queries/{query_id}/reply",
            headers={"X-User-Id": str(user.id)},
            files=[(slot, (f"{slot}.pdf", data, "application/pdf")) for slot, data in files],
        )

    async def submit(
        self, user: User, files: list[tuple[str, bytes]], **form: Any
    ) -> httpx.Response:
        return await self.client.post(
            "/api/submissions",
            headers={"X-User-Id": str(user.id)},
            data={"form": json.dumps(FORM | form)},
            files=[(slot, (f"{slot}.pdf", data, "application/pdf")) for slot, data in files],
        )

    async def get(self, user: User, path: str) -> httpx.Response:
        return await self.client.get(path, headers={"X-User-Id": str(user.id)})


@pytest.fixture
async def api(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Api]:
    await seed_claims_db(sessions)
    files, starter = FakeFileStore(), FakeWorkflowStarter()
    communication = DefaultCommunicationService(
        sessions, files, FakeModelGateway({}), FakeNotificationService()
    )
    queries = DefaultQueryService(sessions, communication, reply_days=15, reminder_days=[3, 7])
    app = create_app(
        UNREACHABLE,
        health_service=FakeHealthService(),
        task_service=FakeTaskService(),
        monitoring=FakeMonitoring(),
        user_service=FakeUserService([MEMBER, STRANGER, HOSPITAL, OPS]),
        intake_service=DefaultIntakeService(sessions, files, starter, signaller=starter),
        query_service=queries,
        claim_queries=DefaultClaimQueryService(sessions),
        file_store=files,
        permission_service=FakePermissionService(
            policies={MEMBER.id: {POLICY}}, hospitals={HOSPITAL.id: {ROHINI}}
        ),
        model_gateway=FakeModelGateway({}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client, files, starter, queries)


TYPED = [
    ("claim_form_part_a", make_pdf(["part a"])),
    ("final_bill", make_pdf(["bill"])),
    ("discharge_summary", make_pdf(["summary"])),
]


async def test_a_member_submits_typed_slots_and_gets_a_started_claim(api: Api) -> None:
    response = await api.submit(MEMBER, TYPED)

    assert response.status_code == 201
    body = response.json()
    assert [f["slot"] for f in body["files"]] == [s for s, _ in TYPED]
    assert body["missing_documents"] == ["claim_form_part_b", "payment_receipt", "cancelled_cheque"]
    assert body["workflow_id"] == body["claim_id"]
    assert list(api.starter.started) == [body["claim_id"]]


async def test_one_merged_pdf_is_accepted(api: Api) -> None:
    response = await api.submit(MEMBER, [("merged", make_pdf(["a", "b"]))])

    assert response.status_code == 201
    assert response.json()["files"][0]["slot"] is None


async def test_a_hospital_user_submits_for_its_own_hospital(api: Api) -> None:
    ok = await api.submit(HOSPITAL, TYPED, request_type="preauth")
    other = await api.submit(HOSPITAL, TYPED, hospital_id="8900000000028")

    assert ok.status_code == 201 and ok.json()["request_type"] == "preauth"
    assert other.status_code == 403


async def test_submission_errors_are_422_with_reasons(api: Api) -> None:
    bad_type = await api.submit(MEMBER, [("final_bill", b"PK\x03\x04 not a pdf")])
    bad_slot = await api.submit(MEMBER, [("menu_card", make_pdf(["x"]))])
    bad_form = await api.submit(MEMBER, TYPED, admission_date="yesterday")
    no_form = await api.client.post(
        "/api/submissions",
        headers={"X-User-Id": str(MEMBER.id)},
        files=[("final_bill", ("b.pdf", make_pdf(["b"]), "application/pdf"))],
    )

    assert bad_type.status_code == 422
    assert bad_type.json()["detail"] == [
        "final_bill.pdf: unsupported file type; upload PDF, JPG, PNG, TIFF or HEIC"
    ]
    assert bad_slot.status_code == 422 and "menu_card" in bad_slot.json()["detail"]
    assert bad_form.status_code == 422
    assert no_form.status_code == 422


async def test_only_permitted_outside_users_submit(api: Api) -> None:
    stranger = await api.submit(STRANGER, TYPED)
    staff = await api.submit(OPS, TYPED)

    assert (stranger.status_code, staff.status_code) == (403, 403)
    assert api.files.files == {}


async def test_my_claims_lists_only_the_users_own_claims(api: Api) -> None:
    mine = (await api.submit(MEMBER, TYPED)).json()
    await api.submit(HOSPITAL, [("final_bill", make_pdf(["hospital bill"]))])

    member_view = await api.get(MEMBER, "/api/claims/mine")
    stranger_view = await api.get(STRANGER, "/api/claims/mine")

    assert [c["claim_id"] for c in member_view.json()] == [mine["claim_id"]]
    assert member_view.json()[0]["status"] == "received"
    assert stranger_view.json() == []


async def test_claim_detail_is_visible_to_its_parties_and_staff_only(api: Api) -> None:
    claim_id = (await api.submit(MEMBER, TYPED)).json()["claim_id"]
    path = f"/api/claims/{claim_id}"

    member, hospital, ops, stranger = [
        await api.get(u, path) for u in (MEMBER, HOSPITAL, OPS, STRANGER)
    ]

    assert member.status_code == 200
    assert member.json()["submissions"][0]["files"][1]["slot"] == "final_bill"
    assert (hospital.status_code, ops.status_code, stranger.status_code) == (200, 200, 404)
    assert (await api.get(MEMBER, f"/api/claims/{uuid4()}")).status_code == 404


async def test_files_are_served_only_after_a_permission_check(api: Api) -> None:
    submission = (await api.submit(MEMBER, TYPED)).json()
    key = submission["files"][1]["storage_key"]
    path = f"/api/files/{key}"

    member = await api.get(MEMBER, path)
    stranger = await api.get(STRANGER, path)
    ops = await api.get(OPS, path)
    unknown = await api.get(MEMBER, "/api/files/" + "0" * 32)

    assert member.status_code == 200
    assert member.headers["content-type"] == "application/pdf"
    assert member.content == TYPED[1][1]
    assert (stranger.status_code, ops.status_code, unknown.status_code) == (404, 200, 404)


RECEIPT = QueryItem(text="Payment receipt", source="missing_document")


async def test_a_query_reply_joins_the_claim_and_signals_its_workflow(api: Api) -> None:
    claim_id = UUID((await api.submit(MEMBER, TYPED)).json()["claim_id"])
    query = await api.queries.raise_query(claim_id, [RECEIPT])

    response = await api.reply(MEMBER, query.query_id, [("payment_receipt", make_pdf(["r"]))])

    assert response.status_code == 201
    body = response.json()
    assert (body["claim_id"], body["parent_claim_id"]) == (str(claim_id), str(claim_id))
    assert body["request_type"] == "query_reply"
    assert api.starter.followups == [(str(claim_id), UUID(body["submission_id"]))]
    detail = (await api.get(MEMBER, f"/api/claims/{claim_id}")).json()
    assert detail["open_query"]["query_id"] == str(query.query_id)
    assert [letter["kind"] for letter in detail["letters"]] == ["query"]
    letter = await api.get(MEMBER, detail["letters"][0]["download_url"])
    assert letter.headers["content-type"] == "application/pdf"


async def test_a_query_reply_is_refused_for_strangers_unknown_and_answered_queries(
    api: Api,
) -> None:
    claim_id = UUID((await api.submit(MEMBER, TYPED)).json()["claim_id"])
    query = await api.queries.raise_query(claim_id, [RECEIPT])
    receipt = [("payment_receipt", make_pdf(["r"]))]

    stranger = await api.reply(STRANGER, query.query_id, receipt)
    unknown = await api.reply(MEMBER, uuid4(), receipt)
    staff = await api.reply(OPS, query.query_id, receipt)
    first = await api.reply(MEMBER, query.query_id, receipt)
    await api.queries.attach_reply(query.query_id, UUID(first.json()["submission_id"]))
    answered = await api.reply(MEMBER, query.query_id, [("kyc", make_pdf(["k"]))])

    assert (stranger.status_code, unknown.status_code) == (404, 404)
    assert (staff.status_code, first.status_code, answered.status_code) == (403, 201, 409)


async def test_the_member_form_keeps_bank_category_and_declaration_fields(api: Api) -> None:
    extra = {
        "hospital_city": "Pune",
        "claim_category": "inpatient",
        "cause": "illness",
        "bank_account_number": "001234567890",
        "ifsc": "HDFC0001234",
        "declaration_accepted": True,
    }

    ok = await api.submit(MEMBER, TYPED, **extra)
    bad_ifsc = await api.submit(MEMBER, TYPED, **(extra | {"ifsc": "HDFC1234"}))

    assert ok.status_code == 201
    assert {k: ok.json()["form"][k] for k in extra} == extra
    assert bad_ifsc.status_code == 422


async def test_a_claims_pages_are_for_staff_only(api: Api) -> None:
    claim_id = (await api.submit(MEMBER, TYPED)).json()["claim_id"]
    path = f"/api/claims/{claim_id}/pages"

    member, ops = await api.get(MEMBER, path), await api.get(OPS, path)

    assert member.status_code == 403
    assert ops.status_code == 404  # not classified yet: the fake starter runs no workflow
