"""The tracer bundle generator: its ground truth adds up, its PDFs are complete, and it
describes the seeded member and hospital."""

import json
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.medical_case import onset
from app.database.seed import HOSPITALS
from app.database.seed_knowledge import NON_PAYABLE_ITEMS
from app.mock_core.models import McInsuredMember, McPolicy
from app.mock_core.seed import seed_mock_core
from tools.mockgen import tracer_claim as gen

ADMITTED = date(2026, 10, 29)
D = Decimal


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("tracer")
    truth = gen.generate(out, ADMITTED)
    return out, truth


def test_ground_truth_file_is_what_generate_returns(bundle: tuple[Path, dict[str, Any]]) -> None:
    out, truth = bundle
    assert json.loads((out / gen.GROUND_TRUTH).read_text()) == truth


def test_bill_lines_sections_and_totals_add_up(bundle: tuple[Path, dict[str, Any]]) -> None:
    _, truth = bundle
    bill = truth["bill"]
    sums: dict[str, Decimal] = defaultdict(Decimal)
    for line in bill["lines"]:
        assert D(line["qty"]) * D(line["rate"]) == D(line["amount"])
        sums[line["section"]] += D(line["amount"])
    assert {s["section"]: D(s["total"]) for s in bill["section_totals"]} == sums
    grand = D(bill["grand_total"])
    assert sum(sums.values()) == grand
    fields = truth["fields"]
    assert grand == D(fields["claimed.total"]) == D(fields["bill.grand_total"])
    assert grand == D(truth["form"]["claimed_amount"]) == D(truth["sheet"]["claimed_total"])
    assert grand < 100000  # under the KYC threshold, so the receipt is the only missing document
    assert [ln["line_id"] for ln in bill["lines"]] == list(range(1, len(bill["lines"]) + 1))


def test_deductions_are_consistent_and_follow_the_package(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    _, truth = bundle
    sheet = truth["sheet"]
    for line in sheet["lines"]:
        assert D(line["admissible"]) + D(line["deducted"]) == D(line["claimed"])
    admissible = sum(D(ln["admissible"]) for ln in sheet["lines"])
    assert D(sheet["admissible_total"]) == admissible
    assert D(sheet["co_pay"]) == (admissible * D("0.05")).quantize(D("0.01"))
    assert D(sheet["payable"]) == admissible - D(sheet["co_pay"])
    assert sheet["package"]["package_code"] == "AG095"

    by_rule = defaultdict(set)
    for line in sheet["deductions"]:
        by_rule[(line["rule_id"], line["clause_ref"])].add(line["head"])
    # Every head on the bill is one a package covers: R20 caps them all at the package rate.
    assert by_rule == {
        ("R20", "AGREED-TARIFF"): {"room", "nursing", "ot", "professional_fees", "pharmacy",
                                   "consumables", "diagnostics"},
        ("R11", "ANNEX-I"): {"consumables"},
    }  # fmt: skip
    [r11] = [ln for ln in sheet["deductions"] if ln["rule_id"] == "R11"]
    assert r11["description"] == gen.NON_PAYABLE_ITEM and r11["deducted"] == r11["claimed"]
    # What R11 left is capped at the package rate exactly.
    assert admissible == D(sheet["package"]["rate"])
    assert sheet["rules_failed"] == ["R11", "R20"]
    assert truth["expected"]["approver_band"] == "approver"  # payable within Rs 1 lakh


def test_drug_findings_compare_against_the_ceiling_plus_gst(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    _, truth = bundle
    found = {f["description"] for f in truth["drug_price_findings"]}
    # Ceftriaxone Rs 68.50 is within 63.88 plus 12% GST; metronidazole (per mL) and
    # tramadol (per mL at 50 mg/mL) are above theirs.
    assert found == {"Inj. Metronidazole 100 ml", "Inj. Tramadol 50 mg"}


def test_merged_pdf_has_every_part_and_no_receipt(bundle: tuple[Path, dict[str, Any]]) -> None:
    out, truth = bundle
    parts = truth["files"]["bundle"]
    for part in parts:
        assert gen.page_count((out / part["file"]).read_bytes()) == part["pages"]
    merged = gen.page_count((out / gen.MERGED).read_bytes())
    assert merged == sum(p["pages"] for p in parts) == len(truth["files"]["merged_pages"])
    assert "payment_receipt" not in truth["files"]["merged_pages"]
    assert truth["missing_documents"] == ["payment_receipt"]
    # A surgical claim carries the operation theatre notes; the receipt is the only gap.
    assert "ot_notes" in truth["files"]["merged_pages"]
    assert [p["doc_type"] for p in parts if p["doc_type"] == "ot_notes"] == ["ot_notes"]
    assert truth["expected"]["query_items"] == ["payment receipt"]
    assert gen.page_count((out / gen.REPLY).read_bytes()) == truth["files"]["reply"]["pages"] == 1


def test_another_admission_date_gives_different_documents(
    bundle: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    out, truth = bundle
    other = gen.generate(tmp_path, ADMITTED - timedelta(days=30))
    assert other["bill"]["bill_number"] != truth["bill"]["bill_number"]
    assert (tmp_path / gen.MERGED).read_bytes() != (out / gen.MERGED).read_bytes()
    assert other["sheet"]["payable"] == truth["sheet"]["payable"]


def test_the_non_payable_item_is_on_the_seeded_list() -> None:
    assert gen.NON_PAYABLE_ITEM in {item for _, _, item, _, _ in NON_PAYABLE_ITEMS}


def test_the_hospital_is_seeded_with_differential_billing() -> None:
    [hospital] = [h for h in HOSPITALS if h.rohini_id == gen.HOSPITAL_ROHINI]
    assert (hospital.name, hospital.city) == (gen.HOSPITAL, gen.HOSPITAL_CITY)
    assert hospital.differential_billing is gen.DIFFERENTIAL_BILLING is True


async def test_the_member_and_policy_match_the_mock_core_seed(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mock_core.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    today = date(2026, 11, 19)
    await seed_mock_core(engine, sessions, today=today)
    async with sessions() as session:
        policy = await session.get(McPolicy, gen.MEMBER_POLICY_NUMBER)
        member = await session.scalar(
            select(McInsuredMember).where(McInsuredMember.member_id == gen.MEMBER_MEMBER_ID)
        )
    await engine.dispose()
    assert policy is not None and member is not None
    assert policy.sum_insured == gen.SUM_INSURED
    assert (member.name, member.dob, member.gender) == (gen.PATIENT, gen.PATIENT_DOB, "F")
    # The default admission (21 days back) is inside the policy period and long past the
    # 30-day initial wait.
    admitted = today - timedelta(days=21)
    assert policy.policy_start <= admitted <= policy.policy_end
    assert (admitted - policy.inception_date).days > 30
    # The discharge summary's one duration phrase starts after inception: no PED finding.
    started = onset(gen.HISTORY_DURATION, admitted)
    assert started is not None and started >= policy.inception_date
