"""Bill review through its contract, after a real registration with fake core services."""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import NonPayableItemRow
from app.database.repositories.stage_records import StageRecordRepository
from app.database.seed import seed_claims_db
from app.services.bill_review.contract import (
    BillReviewService,
    DeductionSheet,
    InvalidEdit,
    LineEdit,
    NotReadyForBillReview,
)
from app.services.bill_review.service import DefaultBillReviewService
from app.services.registration.contract import RegistrationService
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.contract import Head
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS
from app.shared.types import Stage
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.services.claim_builders import WORKED_BILL, ClaimSpec, add_digitised_claim, bill_line
from tests.services.test_registration import asha_policy

D = Decimal


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    registration: RegistrationService
    service: BillReviewService

    async def registered(self, spec: ClaimSpec | None = None) -> UUID:
        claim_id = await add_digitised_claim(self.sessions, spec)
        await self.registration.register(claim_id)
        return claim_id


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    registration = DefaultRegistrationService(
        sessions, FakePolicyService([asha_policy()]), FakeMoneyService(), rules
    )
    return Env(sessions, registration, DefaultBillReviewService(sessions, rules))


def by_line(sheet: DeductionSheet) -> dict[int, tuple[Head, Decimal, Decimal, str | None]]:
    return {ln.line_id: (ln.head, ln.admissible, ln.deducted, ln.clause_ref) for ln in sheet.lines}


async def test_worked_example_admits_140000_and_pays_133000(env: Env) -> None:
    claim_id = await env.registered()

    sheet = await env.service.review(claim_id)

    assert by_line(sheet) == {
        1: (Head.ROOM, D("20000.00"), D("10000.00"), "ROOM-CAP"),
        2: (Head.PROFESSIONAL_FEES, D("20000.00"), D("10000.00"), "PROP-DED"),
        3: (Head.OT, D("13333.33"), D("6666.67"), "PROP-DED"),
        4: (Head.NURSING, D("6666.67"), D("3333.33"), "PROP-DED"),
        5: (Head.PHARMACY, D("25000.00"), D("0.00"), None),
        6: (Head.IMPLANTS, D("40000.00"), D("0.00"), None),
        7: (Head.DIAGNOSTICS, D("15000.00"), D("0.00"), None),
    }
    assert [ln.rule_id for ln in sheet.lines] == ["R13"] * 4 + [None] * 3
    assert {ln.judged_by for ln in sheet.lines} == {"code"}
    assert sheet.claimed_total == D("170000.00")
    assert sheet.admissible_total == D("140000.00")
    assert sheet.co_pay == D("7000.00")
    assert sheet.payable == D("133000.00")
    assert sheet.rule_results[-1].rule_id == "R21"
    assert sheet.flagged_lines == []
    stored = await StageRecordRepository(env.sessions).history(claim_id, Stage.BILL_REVIEW)
    assert [(r.version, r.created_by) for r in stored] == [(1, "code")]


async def test_no_proportionate_deduction_when_the_hospital_has_no_differential_billing(
    env: Env,
) -> None:
    claim_id = await env.registered(
        ClaimSpec(
            hospital_id=HOSPITAL_ROHINI_IDS[3],  # Sahyadri Nursing Home: one rate for all rooms
            hospital_name="Sahyadri Nursing Home",
            hospital_city="Pune",
        )
    )

    sheet = await env.service.review(claim_id)

    assert [ln.deducted for ln in sheet.lines] == [D("10000.00")] + [D("0.00")] * 6
    assert sheet.admissible_total == D("160000.00")
    assert sheet.payable == D("152000.00")


async def test_non_payable_items_are_deducted_in_full_with_their_list_clause(env: Env) -> None:
    lines = [
        *WORKED_BILL,
        bill_line(8, "Consumables", "GLOVES", "450"),
        bill_line(9, "Room Charges", "Admission Kit", "1200"),
        bill_line(10, "Consumables", "Suture material", "800"),
    ]
    claim_id = await env.registered(ClaimSpec(lines=lines, claimed=D("172450.00")))

    sheet = await env.service.review(claim_id)
    found = {ln.line_id: ln for ln in sheet.lines}

    assert (found[8].admissible, found[8].rule_id, found[8].clause_ref) == (
        D("0.00"),
        "R11",
        "ANNEX-I",
    )
    assert (found[9].admissible, found[9].rule_id, found[9].clause_ref) == (
        D("0.00"),
        "R11",
        "ANNEX-II",
    )
    assert "Part of the room charges" in (found[9].reason or "")
    assert (found[10].head, found[10].admissible) == (Head.CONSUMABLES, D("800.00"))


async def test_a_loaded_wording_list_replaces_the_fixture_for_r11(env: Env) -> None:
    async with env.sessions.begin() as session:
        session.add_all(
            [
                NonPayableItemRow(
                    list_name="IV",
                    item_number=18,
                    item="URINE BAG",
                    condition="Paid where medically necessary, at most one bag in 24 hours",
                    payable_in_product="payable_within_limits",
                    source="non_payable",
                    source_version="GODHLIP24179V022324",
                ),
                NonPayableItemRow(
                    list_name="I",
                    item_number=56,
                    item="GLOVES",
                    condition="Sterile gloves are paid; non-sterile gloves are not",
                    payable_in_product="not_payable_unless",
                    source="non_payable",
                    source_version="GODHLIP24179V022324",
                ),
            ]
        )
    lines = [
        *WORKED_BILL,
        bill_line(8, "Consumables", "Gloves", "264"),
        bill_line(9, "Consumables", "Urine bag", "150"),  # list IV in the fixture too
        bill_line(10, "Room Charges", "Admission Kit", "1200"),  # only in the fixture
    ]
    claim_id = await env.registered(ClaimSpec(lines=lines, claimed=D("171614.00")))

    found = {ln.line_id: ln for ln in (await env.service.review(claim_id)).lines}

    assert (found[8].deducted, found[8].clause_ref) == (D("264.00"), "ANNEX-I")
    assert found[9].deducted == D("0.00")
    assert found[10].deducted == D("0.00")


async def test_lines_map_by_heading_then_description_and_unknown_lines_are_flagged(
    env: Env,
) -> None:
    lines = [
        bill_line(1, "Hospital Charges", "ICU bed charges", "8000", qty="2"),
        bill_line(2, "Hospital Charges", "Consultation - Dr Rao", "2000"),
        bill_line(3, "Sundry", "Misc services", "500"),
        bill_line(4, "Ambulance", "Ambulance pick up", "1500"),
        bill_line(5, "Lab", "CBC", "300"),
    ]
    claim_id = await env.registered(ClaimSpec(lines=lines, claimed=D("12300.00")))

    sheet = await env.service.review(claim_id)

    assert [ln.head for ln in sheet.lines] == [
        Head.ICU,
        Head.PROFESSIONAL_FEES,
        Head.OTHER,
        Head.AMBULANCE,
        Head.DIAGNOSTICS,
    ]
    assert sheet.flagged_lines == [3]
    assert [ln.flagged for ln in sheet.lines] == [False, False, True, False, False]


async def test_review_is_idempotent_per_input_versions(env: Env) -> None:
    claim_id = await env.registered()

    first = await env.service.review(claim_id)
    again = await env.service.review(claim_id)

    assert again == first
    history = await StageRecordRepository(env.sessions).history(claim_id, Stage.BILL_REVIEW)
    assert len(history) == 1


async def test_a_person_edit_is_a_new_version_and_recomputes_the_co_pay(env: Env) -> None:
    claim_id = await env.registered()
    await env.service.review(claim_id)

    sheet = await env.service.apply_edits(
        claim_id,
        [LineEdit(line_id=7, admissible=D("5000"), reason="Only one CT scan was advised")],
        actor="task:t1",
    )

    edited = next(ln for ln in sheet.lines if ln.line_id == 7)
    assert (edited.admissible, edited.deducted, edited.judged_by) == (
        D("5000.00"),
        D("10000.00"),
        "person",
    )
    assert edited.reason == "Only one CT scan was advised"
    assert sheet.admissible_total == D("130000.00")
    assert sheet.co_pay == D("6500.00")
    assert sheet.payable == D("123500.00")
    history = await StageRecordRepository(env.sessions).history(claim_id, Stage.BILL_REVIEW)
    assert [(r.version, r.created_by) for r in history] == [(1, "code"), (2, "task:t1")]
    # A later review with unchanged inputs keeps the person's version.
    assert await env.service.review(claim_id) == sheet


@pytest.mark.parametrize(
    "edit",
    [
        LineEdit(line_id=99, admissible=D("1"), reason="x"),
        LineEdit(line_id=7, admissible=D("15000.01"), reason="x"),
        LineEdit(line_id=7, admissible=D("-1"), reason="x"),
        LineEdit(line_id=7, admissible=D("100"), reason="  "),
    ],
    ids=["unknown_line", "above_claimed", "negative", "no_reason"],
)
async def test_invalid_edits_are_refused(env: Env, edit: LineEdit) -> None:
    claim_id = await env.registered()
    await env.service.review(claim_id)

    with pytest.raises(InvalidEdit):
        await env.service.apply_edits(claim_id, [edit], actor="task:t1")


async def test_review_needs_a_registered_claim(env: Env) -> None:
    claim_id = await add_digitised_claim(env.sessions)

    with pytest.raises(NotReadyForBillReview):
        await env.service.review(claim_id)
