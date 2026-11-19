"""Submit-area routes for cashless requests: later requests on a pre-auth's claim, the
checklists, the claim's authorisations and the originals courier status."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ClaimRow
from app.database.seed import seed_claims_db
from app.main import create_app
from app.services.cashless.service import DefaultCashlessService
from app.services.claims.service import DefaultClaimQueryService
from app.services.intake.service import DefaultIntakeService
from app.services.users.contract import User
from tests.api.test_submission_routes import FORM, HOSPITAL, MEMBER, OPS, POLICY, ROHINI
from tests.api.test_task_routes import UNREACHABLE
from tests.docs import make_pdf
from tests.fakes import FakeHealthService
from tests.fakes.core import FakeMoneyService, FakePermissionService
from tests.fakes.file_store import FakeFileStore
from tests.fakes.intake import FakeWorkflowStarter
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.monitoring import FakeMonitoring
from tests.fakes.tasks import FakeTaskService
from tests.fakes.users import FakeUserService

PREAUTH = {**FORM, "discharge_date": None, "claimed_amount": "30000", "request_type": "preauth"}
PART_C = [("preauth_form", make_pdf(["part c"])), ("investigation_report", make_pdf(["usg"]))]


class Api:
    def __init__(
        self,
        client: httpx.AsyncClient,
        sessions: async_sessionmaker[AsyncSession],
        starter: FakeWorkflowStarter,
    ) -> None:
        self.client, self.sessions, self.starter = client, sessions, starter

    async def post(
        self,
        user: User,
        path: str,
        form: dict[str, Any] | None = None,
        files: list[tuple[str, bytes]] = (),  # type: ignore[assignment]
    ) -> httpx.Response:
        return await self.client.post(
            path,
            headers={"X-User-Id": str(user.id)},
            data={"form": json.dumps(form)} if form is not None else None,
            files=[(slot, (f"{slot}.pdf", data, "application/pdf")) for slot, data in files]
            or None,
        )

    async def get(self, user: User, path: str) -> httpx.Response:
        return await self.client.get(path, headers={"X-User-Id": str(user.id)})

    async def preauth(self) -> str:
        response = await self.post(HOSPITAL, "/api/submissions", PREAUTH, PART_C)
        assert response.status_code == 201, response.text
        return str(response.json()["claim_id"])


@pytest.fixture
async def api(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Api]:
    await seed_claims_db(sessions)
    files, starter = FakeFileStore(), FakeWorkflowStarter()
    app = create_app(
        UNREACHABLE,
        health_service=FakeHealthService(),
        task_service=FakeTaskService(),
        monitoring=FakeMonitoring(),
        user_service=FakeUserService([MEMBER, HOSPITAL, OPS]),
        intake_service=DefaultIntakeService(sessions, files, starter, signaller=starter),
        claim_queries=DefaultClaimQueryService(sessions),
        file_store=files,
        permission_service=FakePermissionService(
            policies={MEMBER.id: {POLICY}}, hospitals={HOSPITAL.id: {ROHINI}}
        ),
        model_gateway=FakeModelGateway({}),
        cashless_service=DefaultCashlessService(sessions, FakeMoneyService()),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client, sessions, starter)


async def test_a_hospital_sends_an_enhancement_on_its_preauth_claim(api: Api) -> None:
    claim_id = await api.preauth()

    response = await api.post(
        HOSPITAL,
        f"/api/claims/{claim_id}/requests",
        {"request_type": "enhancement"},
        [("enhancement_request", make_pdf(["more"])), ("interim_bill", make_pdf(["bill"]))],
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["claim_id"], body["request_type"]) == (claim_id, "enhancement")
    created = datetime.fromisoformat(body["created_at"])
    assert (datetime.fromisoformat(body["clock_deadline"]) - created).total_seconds() == 3600
    assert api.starter.followups[-1] == (claim_id, UUID(body["submission_id"]))


async def test_a_cancellation_needs_no_files(api: Api) -> None:
    claim_id = await api.preauth()

    response = await api.post(
        HOSPITAL, f"/api/claims/{claim_id}/requests", {"request_type": "cancellation"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["files"] == []


async def test_requests_are_refused_off_a_preauth_or_for_others(api: Api) -> None:
    claim_id = await api.preauth()
    reimbursement = await api.post(MEMBER, "/api/submissions", FORM, PART_C[:1])
    other = str(reimbursement.json()["claim_id"])
    bill = [("final_bill", make_pdf(["bill"]))]

    wrong_type = await api.post(HOSPITAL, f"/api/claims/{claim_id}/requests",
                                {"request_type": "reimbursement"}, bill)  # fmt: skip
    member = await api.post(MEMBER, f"/api/claims/{claim_id}/requests",
                            {"request_type": "discharge"}, bill)  # fmt: skip
    not_cashless = await api.post(HOSPITAL, f"/api/claims/{other}/requests",
                                  {"request_type": "discharge"}, bill)  # fmt: skip

    assert wrong_type.status_code == 422
    assert member.status_code == 403
    assert not_cashless.status_code in (404, 409)


async def test_checklists_list_each_requests_documents(api: Api) -> None:
    response = await api.get(HOSPITAL, "/api/checklists/discharge")

    assert response.status_code == 200
    items = {i["doc_type"]: i["required"] for i in response.json()}
    assert items["final_bill"] is True and items["discharge_summary"] is True
    assert items["ot_notes"] is False


async def test_claim_detail_shows_the_current_request_and_the_originals(api: Api) -> None:
    claim_id = await api.preauth()
    due = datetime(2026, 12, 7, tzinfo=UTC)
    async with api.sessions.begin() as session:
        row = await session.get(ClaimRow, UUID(claim_id))
        assert row is not None
        row.current_request, row.originals_due_at = "discharge", due

    detail = (await api.get(HOSPITAL, f"/api/claims/{claim_id}")).json()
    early = await api.post(HOSPITAL, f"/api/claims/{claim_id}/originals-received")
    marked = await api.post(OPS, f"/api/claims/{claim_id}/originals-received")
    after = (await api.get(HOSPITAL, f"/api/claims/{claim_id}")).json()

    assert detail["current_request"] == "discharge"
    assert datetime.fromisoformat(detail["originals_due_at"]) == due
    assert detail["originals_received_at"] is None and detail["authorisations"] == []
    assert detail["clock_deadline"] is not None
    assert early.status_code == 403  # an operator marks them, not the hospital
    assert marked.status_code == 200, marked.text
    assert after["originals_received_at"] is not None


async def test_the_clock_stops_once_the_current_request_is_authorised(api: Api) -> None:
    from app.database.repositories.stage_records import StageRecordRepository
    from app.shared.types import Stage

    claim_id = await api.preauth()
    async with api.sessions.begin() as session:
        row = await session.get(ClaimRow, UUID(claim_id))
        assert row is not None
        row.current_request = "preauth"
    running = (await api.get(HOSPITAL, f"/api/claims/{claim_id}")).json()
    await StageRecordRepository(api.sessions).append(
        UUID(claim_id),
        Stage.AUTHORISATION,
        {
            "request_type": "preauth",
            "status": "awaiting_discharge",
            "approved_now": "100.00",
            "approved_total": "100.00",
            "authorised_at": "2026-11-19T06:00:00+00:00",
        },
        created_by="test",
    )

    stopped = (await api.get(HOSPITAL, f"/api/claims/{claim_id}")).json()

    assert running["clock_deadline"] is not None
    assert stopped["clock_deadline"] is None
    assert [a["approved_total"] for a in stopped["authorisations"]] == ["100.00"]
