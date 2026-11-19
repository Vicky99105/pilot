"""Communication through its contract: decision letters as PDFs with the deduction table,
the plain-language rewrite and its fallback, and sending through the notification contract."""

import re
from decimal import Decimal
from typing import Any

import pypdfium2 as pdfium
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.seed import seed_claims_db
from app.services.communication.contract import CommunicationService, NotReadyForLetter
from app.services.communication.service import PLAIN_PURPOSE, DefaultCommunicationService
from app.services.decision.contract import ApproverOutcome
from app.services.decision.service import DefaultDecisionService
from app.services.model_gateway.contract import ModelRole
from app.services.notification.service import GraphNotificationService, parse_allowlist
from app.shared.seed_ids import DOCTOR_USER_ID
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
