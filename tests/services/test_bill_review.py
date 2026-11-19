"""Bill review through its contract, after a real registration with fake core services."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
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
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.registration.contract import RegistrationService
from app.services.registration.service import DefaultRegistrationService
from app.services.rules.contract import Head, RulesEngine
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import HOSPITAL_ROHINI_IDS
from app.shared.types import RequestType, Stage
from tests.fakes.core import FakeMoneyService, FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.services.claim_builders import (
    WORKED_BILL,
    ClaimSpec,
    add_coding,
    add_digitised_claim,
    bill_line,
)
from tests.services.reference_rows import add_reference_rows
from tests.services.test_registration import asha_policy

D = Decimal
CASHLESS = RequestType.DISCHARGE  # a cashless claim's final bill from a network hospital


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    registration: RegistrationService
    rules: RulesEngine
    gateway: FakeModelGateway

    @property
    def service(self) -> BillReviewService:
        return DefaultBillReviewService(
            self.sessions, self.rules, self.gateway, DefaultKnowledgeBase(self.sessions)
        )

    async def registered(
        self,
        spec: ClaimSpec | None = None,
        procedures: tuple[tuple[str, str, str], ...] = (),
    ) -> UUID:
        """Digitised, registered and coded. The default has no procedure: no package."""
        claim_id = await add_digitised_claim(self.sessions, spec)
        await self.registration.register(claim_id)
        await add_coding(self.sessions, claim_id, procedures=procedures)
        return claim_id

    def script(self, **replies: list[dict[str, Any]]) -> None:
        self.gateway = FakeModelGateway(replies)


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession]) -> Env:
    await seed_claims_db(sessions)
    await add_reference_rows(sessions)
    rules = DefaultRulesEngine(Settings().config_dir / "products")
    registration = DefaultRegistrationService(
        sessions, FakePolicyService([asha_policy()]), FakeMoneyService(), rules
    )
    return Env(sessions, registration, rules, FakeModelGateway({}))


def by_line(sheet: DeductionSheet) -> dict[int, tuple[Head, Decimal, Decimal, str | None]]:
    return {ln.line_id: (ln.head, ln.admissible, ln.deducted, ln.clause_ref) for ln in sheet.lines}


async def test_worked_example_admits_138500_and_pays_131575(env: Env) -> None:
    """No package (no procedure coded). Sunrise's agreement gives 10% off non-package
    charges, except medicines and implants (R24), then the room cap and the proportionate
    deduction apply to what is left (R13). The discount on the room lowers the room rate
    the proportion uses (Rs 6,750 against the Rs 5,000 cap), so the linked heads end where
    they did before the discount; only diagnostics keep the 10% off. A cashless claim: the
    agreement's discount binds the hospital."""
    claim_id = await env.registered(ClaimSpec(request_type=CASHLESS))

    sheet = await env.service.review(claim_id)

    assert by_line(sheet) == {
        1: (Head.ROOM, D("20000.00"), D("10000.00"), "ROOM-CAP"),
        2: (Head.PROFESSIONAL_FEES, D("20000.00"), D("10000.00"), "PROP-DED"),
        3: (Head.OT, D("13333.33"), D("6666.67"), "PROP-DED"),
        4: (Head.NURSING, D("6666.67"), D("3333.33"), "PROP-DED"),
        5: (Head.PHARMACY, D("25000.00"), D("0.00"), None),
        6: (Head.IMPLANTS, D("40000.00"), D("0.00"), None),
        7: (Head.DIAGNOSTICS, D("13500.00"), D("1500.00"), "NON-PKG-DISC"),
    }
    assert [ln.rule_id for ln in sheet.lines] == ["R13"] * 4 + [None, None, "R24"]
    # Every rule that took something off a line, in order: the discount, then the cap.
    assert [(d.rule_id, d.amount) for d in sheet.lines[0].deductions] == [
        ("R24", D("3000.00")),
        ("R13", D("7000.00")),
    ]
    assert {ln.judged_by for ln in sheet.lines} == {"code"}
    assert sheet.claimed_total == D("170000.00")
    assert sheet.admissible_total == D("138500.00")
    assert sheet.co_pay == D("6925.00")
    assert sheet.payable == D("131575.00")
    assert sheet.package is None  # nothing to map without a procedure
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
    # Not a list item, and a reimbursement gets no agreed discount: paid as billed.
    assert (found[10].head, found[10].rule_id, found[10].admissible) == (
        Head.CONSUMABLES,
        None,
        D("800.00"),
    )


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

    def by_r11(line_id: int) -> Decimal:
        return sum((d.amount for d in found[line_id].deductions if d.rule_id == "R11"), D(0))

    assert (found[8].deducted, found[8].clause_ref) == (D("264.00"), "ANNEX-I")
    assert by_r11(9) == D("0")  # list IV, which the product pays
    assert by_r11(10) == D("0")  # only in the fixture, which the loaded list replaced


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


# --- Slice P2-4: package first ------------------------------------------------------------

LAP = (("Laparoscopic appendicectomy", "0DTJ4ZZ",
        "Resection of Appendix, Percutaneous Endoscopic Approach"),)  # fmt: skip
# A laparoscopic appendicectomy bill, itemised, Rs 79,946.
ITEMISED = [
    bill_line(1, "Room Charges", "Deluxe Single Room Rent", "30000", qty="4"),
    bill_line(2, "Nursing Charges", "Nursing Care Charges", "3600", qty="4"),
    bill_line(3, "OT Charges", "Operation Theatre Charges - Major", "18500"),
    bill_line(4, "Professional Fees", "Surgeon Fee (Laparoscopic Appendicectomy)", "18000"),
    bill_line(5, "Pharmacy", "Inj. Ceftriaxone 1 g", "411", qty="6"),
    bill_line(6, "Pharmacy", "Inj. Metronidazole 100 ml", "192", qty="6"),
    bill_line(7, "Consumables", "Gloves", "264", qty="12"),
    bill_line(8, "Implants", "Titanium clip", "2500"),
    bill_line(9, "Investigations", "Histopathology - Appendix Specimen", "1450"),
    bill_line(10, "Investigations", "Complete Blood Count (CBC)", "5029"),
]
ITEMISED_TOTAL = D("79946.00")
ITEMISED_CASHLESS = ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL, request_type=CASHLESS)
CHOOSE_AG095 = {"bill_package_choice": [{"procedure_1": "AG095"}]}


async def test_an_itemised_bill_is_capped_at_the_agreed_package_rate(env: Env) -> None:
    """Sunrise: CGHS, NABH, X tier, semi-private. AG095 is Rs 33,000. R11 takes the gloves;
    R20 caps the other lines a package covers (Rs 77,182) at Rs 33,000, in proportion; the
    implant is outside the package, and implants are never discounted (R24). A cashless
    claim: the agreement binds the hospital."""
    claim_id = await env.registered(ITEMISED_CASHLESS, LAP)
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)
    found = {ln.line_id: ln for ln in sheet.lines}

    assert sheet.package is not None
    [package] = sheet.package.packages
    assert (package.scheme, package.package_code, package.rate) == ("CGHS", "AG095", D("33000"))
    assert [c.chosen for c in sheet.package.choices] == ["AG095"]
    assert (found[7].rule_id, found[7].deducted) == ("R11", D("264.00"))
    covered = [n for n in found if n not in (7, 8)]
    assert all(found[n].rule_id == "R20" and found[n].in_package for n in covered)
    assert sum((found[n].admissible for n in covered), D(0)) == D("33000.00")
    assert (found[8].in_package, found[8].admissible, found[8].rule_id) == (
        False,
        D("2500.00"),
        None,
    )
    assert sheet.admissible_total == D("35500.00")
    assert sheet.co_pay == D("1775.00")
    assert sheet.payable == D("33725.00")
    [r20] = [r for r in sheet.rule_results if r.rule_id == "R20"]
    assert (r20.clause_ref, r20.amount_effect) == ("AGREED-TARIFF", D("-44182.00"))
    # No room cap or proportionate deduction on lines the package prices.
    assert not any(d.rule_id in ("R13", "R14") for ln in sheet.lines for d in ln.deductions)


async def test_a_reimbursement_maps_the_package_but_pays_on_the_policys_own_limits(
    env: Env,
) -> None:
    """The same bill as a reimbursement. AG095 is still mapped (the guideline and the stay
    norm need it), but the agreement does not bind a reimbursement under this wording: no
    package cap, no unbundling, no agreed discount. The room cap applies: Rs 7,500 a day
    against Rs 5,000, so room 30,000 to 20,000 and nursing, OT and the surgeon's fee by the
    same 2/3 (2,400, 12,333.33, 12,000). Gloves go (R11); the rest is paid as billed."""
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)
    found = {ln.line_id: ln for ln in sheet.lines}

    assert sheet.package is not None
    assert [p.package_code for p in sheet.package.packages] == ["AG095"]
    assert not any(ln.in_package for ln in sheet.lines)
    assert {(n, found[n].admissible, found[n].rule_id) for n in (1, 2, 3, 4, 7)} == {
        (1, D("20000.00"), "R13"),
        (2, D("2400.00"), "R13"),
        (3, D("12333.33"), "R13"),
        (4, D("12000.00"), "R13"),
        (7, D("0.00"), "R11"),
    }
    assert not any(d.rule_id in ("R20", "R23", "R24") for ln in sheet.lines for d in ln.deductions)
    [r20] = [r for r in sheet.rule_results if r.rule_id == "R20"]
    assert r20.passed and "reimbursement" in r20.reason and "AG095" in r20.reason
    assert sheet.admissible_total == D("56315.33")
    assert sheet.co_pay == D("2815.77")
    assert sheet.payable == D("53499.56")


async def test_the_package_choice_is_an_enum_of_the_agreement_schemes_candidates(
    env: Env,
) -> None:
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)

    [call] = [c for c in env.gateway.calls if c.purpose == "bill_package_choice"]
    allowed = call.schema.model_json_schema()["properties"]["procedure_1"]["enum"]  # type: ignore[union-attr]
    assert sheet.package is not None
    offered = [c.package_code for c in sheet.package.candidates]
    assert allowed == [*offered, "none"]
    assert {c.scheme for c in sheet.package.candidates} == {"CGHS"}  # not the HBP SG017B
    assert "AG095" in offered and "SG017B" not in allowed
    assert call.schema is not None
    with pytest.raises(ValidationError):
        call.schema.model_validate({"procedure_1": "SG017B"})


async def test_none_leaves_the_bill_to_the_non_package_rules(env: Env) -> None:
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    env.script(bill_package_choice=[{"procedure_1": "none"}])

    sheet = await env.service.review(claim_id)

    assert sheet.package is not None and sheet.package.packages == []
    assert not any(ln.in_package for ln in sheet.lines)
    assert any(ln.rule_id == "R13" for ln in sheet.lines)  # the room cap applies again


async def test_a_package_charge_with_items_billed_beside_it_is_unbundled(env: Env) -> None:
    lines = [
        bill_line(1, "Package", "Laparoscopic Appendicectomy Package", "40000"),
        bill_line(2, "Consumables", "Laparoscopic Trocar Kit", "4850"),
        bill_line(3, "Pharmacy", "Inj. Ceftriaxone 1 g", "411", qty="6"),
        bill_line(4, "Ambulance", "Ambulance pick up", "3500"),
    ]
    spec = ClaimSpec(lines=lines, claimed=D("48761.00"), request_type=CASHLESS)
    claim_id = await env.registered(spec, LAP)
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)
    found = {ln.line_id: ln for ln in sheet.lines}

    assert sheet.package is not None and sheet.package.billed_as_package
    assert (found[1].head, found[1].admissible, found[1].rule_id) == (
        Head.PACKAGE,
        D("33000.00"),
        "R20",
    )
    assert (found[2].admissible, found[2].rule_id, found[2].clause_ref) == (
        D("0.00"),
        "R23",
        "PACKAGE-INCL",
    )
    assert (found[3].admissible, found[3].rule_id) == (D("0.00"), "R23")
    # Ambulance: outside the package, 10% off (R24), then capped at Rs 2,000 (R19).
    assert (found[4].admissible, found[4].rule_id) == (D("2000.00"), "R19")
    assert sheet.admissible_total == D("35000.00")


async def test_unclear_lines_are_read_once_from_an_enum_of_their_own_candidates(
    env: Env,
) -> None:
    lines = [*WORKED_BILL, bill_line(8, "Consumables", "Urine bag", "150")]
    claim_id = await env.registered(ClaimSpec(lines=lines, claimed=D("170150.00")))
    async with env.sessions.begin() as session:  # a loaded list with a close item
        session.add(
            NonPayableItemRow(list_name="I", item_number=47, item="URINE BAG DISPOSABLE",
                              condition=None, payable_in_product="not_payable",
                              source="non_payable", source_version="W")
        )  # fmt: skip
    reading = {"choice": "List I item 47: URINE BAG DISPOSABLE", "confidence": 0.9}
    env.script(bill_non_payable=[{"line_1": reading}])

    sheet = await env.service.review(claim_id)

    [call] = env.gateway.calls
    schema = call.schema
    assert schema is not None
    choice = schema.model_json_schema()["$defs"]["LineReading1"]["properties"]["choice"]["enum"]
    assert choice == ["List I item 47: URINE BAG DISPOSABLE", "payable"]
    line = next(ln for ln in sheet.lines if ln.line_id == 8)
    assert (line.admissible, line.rule_id, line.judged_by) == (D("0.00"), "R12", "model")
    with pytest.raises(ValidationError):
        schema.model_validate({"line_1": {"choice": "List I item 56: GLOVES", "confidence": 1}})


async def test_an_unsure_reading_is_left_to_the_bill_processor(env: Env) -> None:
    lines = [*WORKED_BILL, bill_line(8, "Consumables", "Urine bag", "150")]
    claim_id = await env.registered(ClaimSpec(lines=lines, claimed=D("170150.00")))
    async with env.sessions.begin() as session:
        session.add(
            NonPayableItemRow(list_name="I", item_number=47, item="URINE BAG DISPOSABLE",
                              condition=None, payable_in_product="not_payable",
                              source="non_payable", source_version="W")
        )  # fmt: skip
    reading = {"choice": "List I item 47: URINE BAG DISPOSABLE", "confidence": 0.55}
    env.script(bill_non_payable=[{"line_1": reading}])

    sheet = await env.service.review(claim_id)

    line = next(ln for ln in sheet.lines if ln.line_id == 8)
    assert line.flagged and "a person decides" in (line.flag_reason or "")
    assert not any(d.rule_id == "R12" for d in line.deductions)


async def test_pharmacy_lines_above_the_ceiling_plus_gst_are_findings_not_deductions(
    env: Env,
) -> None:
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)

    drugs = [f for f in sheet.findings if f.kind == "drug_price"]
    # Ceftriaxone Rs 68.50 is within 63.88 plus 12% GST (71.55); metronidazole Rs 32 a
    # 100 mL bottle is above 100 x 0.21 plus GST (23.52).
    assert [f.line_id for f in drugs] == [6]
    assert "23.52" in drugs[0].text
    assert not any(d.rule_id == "DRUG-PRICE" for ln in sheet.lines for d in ln.deductions)


async def test_the_diagnosis_package_fit_is_checked_against_our_crosswalk(env: Env) -> None:
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    env.script(bill_package_choice=[{"procedure_1": "AG094"}])  # cholecystectomy: no crosswalk

    sheet = await env.service.review(claim_id)

    [fit] = [f for f in sheet.findings if f.kind == "diagnosis_package"]
    assert "AG094" in fit.text and "not in the diagnosis crosswalk" in fit.text
    assert sheet.payable > 0  # a finding, not a failure


async def test_a_crosswalk_that_lists_other_diagnoses_is_a_finding(env: Env) -> None:
    claim_id = await env.registered(ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL), LAP)
    await add_coding(env.sessions, claim_id, procedures=LAP, diagnoses=(("K80.20", "Gall"),))
    env.script(**CHOOSE_AG095)

    sheet = await env.service.review(claim_id)

    [fit] = [f for f in sheet.findings if f.kind == "diagnosis_package"]
    assert "K35-K38" in fit.text and "K80.20" in fit.text


async def test_a_hospital_on_its_own_tariff_maps_no_package(env: Env) -> None:
    spec = ClaimSpec(lines=ITEMISED, claimed=ITEMISED_TOTAL, hospital_id=HOSPITAL_ROHINI_IDS[3],
                     hospital_name="Sahyadri Nursing Home", hospital_city="Pune")  # fmt: skip
    claim_id = await env.registered(spec, LAP)

    sheet = await env.service.review(claim_id)

    assert sheet.package is None
    assert sheet.agreement is not None and sheet.agreement.scheme == "OWN"
    assert env.gateway.calls == []


async def test_review_needs_the_medical_coding(env: Env) -> None:
    claim_id = await add_digitised_claim(env.sessions)
    await env.registration.register(claim_id)

    with pytest.raises(NotReadyForBillReview, match="coded"):
        await env.service.review(claim_id)
