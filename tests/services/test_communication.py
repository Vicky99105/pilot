"""Communication through its contract: decision letters as PDFs with the deduction table,
the plain-language rewrite and its fallback, and sending through the notification contract."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pypdfium2 as pdfium
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ClaimRow, ReconsiderationRow
from app.database.seed import seed_claims_db
from app.services.communication.contract import CommunicationService, NotReadyForLetter
from app.services.communication.service import PLAIN_PURPOSE, DefaultCommunicationService
from app.services.decision.contract import ApproverOutcome
from app.services.decision.service import DefaultDecisionService
from app.services.model_gateway.contract import ModelRole
from app.services.notification.service import GraphNotificationService, parse_allowlist
from app.shared.seed_ids import DOCTOR_USER_ID, MEMBER_USER_ID
from tests.fakes.file_store import FakeFileStore
from tests.fakes.model_gateway import FakeModelGateway
from tests.fakes.notifications import FakeGraphTransport, FakeNotificationService
from tests.services.decided_claims import BREACH_PAYABLE, adjudicated_claim, decided_claim

D = Decimal
MEMBER_EMAIL = "asha.verma@member.example"


class Env:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.files = FakeFileStore()
        self.notifications = FakeNotificationService()

    def service(self, gateway: FakeModelGateway, notifications: Any = None) -> CommunicationService:
        return DefaultCommunicationService(
            self.sessions, self.files, gateway, notifications or self.notifications
        )

    async def pdf_text(self, key: str) -> str:
        pdf = pdfium.PdfDocument(await self.files.get(key))
        return "\n".join(page.get_textpage().get_text_range() for page in pdf)


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    return Env(sessions)


def plain(raw: list[str]) -> dict[str, Any]:
    """A faithful rewrite: the model keeps every number and clause."""
    return {"items": [f"In simple words: {r}" for r in raw]}


def echo_gateway(rewrite: Any = plain) -> FakeModelGateway:
    """Scripts the plain-language reply from the raw reasons the call sends."""

    class Echo(FakeModelGateway):
        async def structured(self, role: Any, messages: Any, schema: Any, **kw: Any) -> Any:
            raw = re.findall(r"^\d+\. (.*)$", messages[-1].content, flags=re.M)
            self._script = {PLAIN_PURPOSE: [rewrite(raw)]}
            return await super().structured(role, messages, schema, **kw)

    return Echo({})


async def test_a_partial_letter_has_the_deduction_table_from_the_sheet(env: Env) -> None:
    claim = await decided_claim(env.sessions, "partial")
    gateway = echo_gateway()

    letter = await env.service(gateway).issue_letter(claim.claim_id)

    assert (letter.kind, letter.amount) == ("partial", BREACH_PAYABLE)
    rows = [(r.line_id, r.claimed, r.deducted, r.clause_ref) for r in letter.deductions]
    assert rows == [
        (1, D("18000.00"), D("3000.00"), "ROOM-CAP"),
        (2, D("6000.00"), D("1000.00"), "PROP-DED"),
        (None, D("38250.00"), D("1912.50"), "CO-PAY"),
    ]
    assert letter.plain_language is True
    assert all(r.startswith("In simple words: ") for r in letter.reasons)
    [call] = gateway.calls
    assert (call.role, call.purpose) == (ModelRole.FAST_TEXT, PLAIN_PURPOSE)

    text = await env.pdf_text(letter.pdf_key)
    for expected in ["36,337.50", "18,000.00", "3,000.00", "1,912.50", "ROOM-CAP", "PROP-DED"]:
        assert expected in text
    assert "In simple words:" in text


async def test_a_rewrite_that_changes_a_number_falls_back_to_the_raw_reasons(env: Env) -> None:
    claim = await decided_claim(env.sessions, "partial")

    def altered(raw: list[str]) -> dict[str, Any]:
        return {"items": [r.replace("3,000.00", "3,500.00") for r in raw]}

    letter = await env.service(echo_gateway(altered)).issue_letter(claim.claim_id)

    assert letter.plain_language is False
    assert letter.reasons == letter.raw_reasons
    assert any("3,000.00" in r for r in letter.reasons)
    assert not any("3,500.00" in r for r in letter.reasons)
    text = await env.pdf_text(letter.pdf_key)
    assert "3,500.00" not in text and "In simple words" not in text


async def test_a_rewrite_that_drops_a_clause_falls_back(env: Env) -> None:
    claim = await decided_claim(env.sessions, "partial")

    def dropped(raw: list[str]) -> dict[str, Any]:
        return {"items": [r.replace("ROOM-CAP", "the policy") for r in raw]}

    letter = await env.service(echo_gateway(dropped)).issue_letter(claim.claim_id)

    assert letter.plain_language is False


async def test_the_letter_is_sent_once_to_the_member_with_the_pdf(env: Env) -> None:
    claim = await decided_claim(env.sessions, "partial")
    service = env.service(echo_gateway())

    first = await service.issue_letter(claim.claim_id)
    again = await service.issue_letter(claim.claim_id)

    assert again.letter_id == first.letter_id
    [sent] = env.notifications.sent
    assert (sent.to, first.recipient, first.sent) == (MEMBER_EMAIL, MEMBER_EMAIL, True)
    [attachment] = sent.attachments
    assert attachment.data == await env.files.get(first.pdf_key)


async def test_a_rejection_lists_its_reasons_with_clause_refs(env: Env) -> None:
    claim = await decided_claim(env.sessions, "reject")

    letter = await env.service(echo_gateway()).issue_letter(claim.claim_id)

    assert (letter.kind, letter.amount, letter.deductions) == ("rejection", D("0.00"), [])
    assert any("Excl03" in r for r in letter.reasons)
    assert "Excl03" in await env.pdf_text(letter.pdf_key)


async def test_no_letter_before_the_crc_upholds_a_partial(env: Env) -> None:
    claim = await adjudicated_claim(env.sessions, "partial")
    decisions = DefaultDecisionService(
        env.sessions, Settings().config_dir / "authority_matrix.yaml"
    )
    await decisions.record(
        claim.claim_id, ApproverOutcome(action="approve", approver_id=DOCTOR_USER_ID)
    )

    with pytest.raises(NotReadyForLetter, match="CRC"):
        await env.service(echo_gateway()).issue_letter(claim.claim_id)


async def test_an_address_off_the_allowlist_is_not_sent_and_the_letter_says_why(env: Env) -> None:
    claim = await decided_claim(env.sessions, "approve")
    transport = FakeGraphTransport()
    graph = GraphNotificationService(
        transport, sender="claims@insurer.example", allowlist=parse_allowlist("qa@insurer.example")
    )

    letter = await env.service(echo_gateway(), graph).issue_letter(claim.claim_id)

    assert (letter.kind, letter.sent) == ("approval", False)
    assert "not on NOTIFICATION_ALLOWLIST" in (letter.send_error or "")
    assert transport.messages == []


# --- P2-5: the cashless authorisation letter (Part D style) ----------------------------------


async def test_a_cashless_request_gets_an_authorisation_letter(env: Env) -> None:
    """An approved or partly approved cashless request is authorised, not paid to the
    member: the letter states the request, the amount approved now, the earlier approved
    total and the new total, all from the authorisation record."""
    from sqlalchemy import update

    from app.database.models import ClaimRow
    from app.database.repositories.stage_records import StageRecordRepository
    from app.shared.types import Stage

    claim = await decided_claim(env.sessions, "partial")
    async with env.sessions.begin() as session:
        await session.execute(
            update(ClaimRow)
            .where(ClaimRow.id == claim.claim_id)
            .values(request_type="preauth", current_request="enhancement")
        )
    await StageRecordRepository(env.sessions).append(
        claim.claim_id,
        Stage.AUTHORISATION,
        {"request_type": "enhancement", "previous_total": "26500.00",
         "approved_now": "9837.50", "approved_total": "36337.50"},
        created_by="test",
    )  # fmt: skip
    letter = await env.service(echo_gateway()).issue_letter(claim.claim_id)

    assert letter.kind == "authorisation"
    assert letter.request_type == "enhancement"
    assert (letter.previous_total, letter.amount, letter.approved_total) == (
        D("26500.00"), D("9837.50"), D("36337.50"))  # fmt: skip
    text = await env.pdf_text(letter.pdf_key)
    assert "Authorisation letter" in text and "Enhancement" in text
    assert "36,337.50" in text and "26,500.00" in text


async def test_a_reconsidered_decision_letter_says_what_was_paid_and_what_it_adds(
    env: Env,
) -> None:
    """The claim was paid Rs 30,000 before; its reconsideration decides Rs 36,337.50. The
    letter names the reconsideration and its grounds, the earlier payment and the Rs 6,337.50
    it adds; the deduction table is the new sheet's."""
    claim = await decided_claim(env.sessions, "partial")
    async with env.sessions.begin() as session:
        row = await session.get(ClaimRow, claim.claim_id)
        assert row is not None and row.submission_id is not None
        session.add(
            ReconsiderationRow(
                id=uuid4(), claim_id=claim.claim_id, number=1, submission_id=row.submission_id,
                workflow_id=f"{claim.claim_id}:reconsideration:1",
                grounds="The room was the only one free", requested_by=MEMBER_USER_ID,
                requested_at=datetime(2026, 1, 1, tzinfo=UTC), previous_decision="partial",
                previous_amount=D("30000.00"), previously_paid=D("30000.00"),
            )
        )  # fmt: skip

    letter = await env.service(echo_gateway()).issue_letter(claim.claim_id)

    assert letter.kind == "partial" and letter.amount == BREACH_PAYABLE
    assert letter.reconsideration is not None
    assert (letter.reconsideration.number, letter.reconsideration.grounds) == (
        1,
        "The room was the only one free",
    )
    assert (letter.reconsideration.previously_paid, letter.reconsideration.paid_now) == (
        D("30000.00"),
        D("6337.50"),
    )
    text = await env.pdf_text(letter.pdf_key)
    for expected in ["reconsider", "30,000.00", "6,337.50", "36,337.50"]:
        assert expected in text


async def test_a_letter_without_a_reconsideration_has_none(env: Env) -> None:
    claim = await decided_claim(env.sessions, "partial")

    letter = await env.service(echo_gateway()).issue_letter(claim.claim_id)

    assert letter.reconsideration is None
