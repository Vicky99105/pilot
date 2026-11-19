"""The cashless scenario generator: its ground truth comes from the rules engine and holds
together across the three requests, its PDFs carry the values, and it describes the seeded
patient and hospital."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.seed import AGREEMENTS, HOSPITALS
from app.mock_core.models import McInsuredMember, McPolicy
from app.mock_core.seed import seed_mock_core
from tools.mockgen import cashless_claim as gen

ADMITTED = date(2026, 11, 14)
D = Decimal


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("cashless")
    return out, gen.generate(out, ADMITTED)


def text(path: Path) -> str:
    doc = pdfium.PdfDocument(path.read_bytes())
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in doc)
    finally:
        doc.close()


def test_the_ground_truth_file_is_what_generate_returns(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    out, truth = bundle
    assert json.loads((out / gen.GROUND_TRUTH).read_text()) == truth


def test_each_request_is_priced_by_the_package_on_the_cashless_route(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    """The pre-auth is below AG095 and paid less co-pay; the enhancement's revised estimate
    is capped at the package rate; the discharge is held to the approved total by R26."""
    _, truth = bundle
    preauth, enhancement, discharge = (
        truth["requests"][r] for r in ("preauth", "enhancement", "discharge")
    )
    rate = D(truth["expected"]["package_rate"])
    assert D(preauth["sheet"]["claimed_total"]) < rate
    assert (preauth["draft"], preauth["approved_total"]) == ("approve", "26600.00")
    assert D(enhancement["sheet"]["claimed_total"]) > rate
    assert enhancement["sheet"]["rules_failed"] == ["R20"]
    assert D(enhancement["sheet"]["admissible_total"]) == rate
    assert (enhancement["approved_now"], enhancement["approved_total"]) == ("4750.00", "31350.00")
    final = discharge["sheet"]
    assert set(final["rules_failed"]) >= {"R11", "R20", "R26"}
    gloves = [ln for ln in final["lines"] if ln["description"] == gen.NON_PAYABLE_ITEM]
    assert [ln["rule_id"] for ln in gloves] == ["R11"]
    assert D(truth["expected"]["r26_variance"]) < 0
    assert D(final["admissible_total"]) == D(truth["expected"]["approved_admissible"])
    assert truth["expected"]["amount_paid"] == final["payable"] == enhancement["approved_total"]


def test_the_pdfs_carry_the_values_the_ground_truth_expects(
    bundle: tuple[Path, dict[str, Any]],
) -> None:
    out, truth = bundle
    part_c = text(out / "01_preauth_form_part_c.pdf")
    for value in (
        gen.PATIENT,
        gen.POLICY_NUMBER,
        gen.MEMBER_ID,
        gen.HOSPITAL_ROHINI,
        gen.SURGEON_REGISTRATION,
        gen.HOSPITAL_ICD,
        "28,000.00",
        "Emergency",
    ):
        assert value in part_c
    assert "7,000.00" in text(out / "03_enhancement_request.pdf")
    bill = text(out / "08_final_bill.pdf")
    assert truth["fields"]["bill.bill_number"] in bill
    assert f"{D(truth['fields']['bill.grand_total']):,.2f}" in bill
    pages = {f["file"]: f["pages"] for files in truth["files"].values() for f in files}
    assert set(pages) == {p.name for p in out.glob("*.pdf")}


async def test_the_patient_and_hospital_are_the_seeded_ones(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mc.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await seed_mock_core(engine, sessions)
    async with sessions() as session:
        policy = await session.get(McPolicy, gen.POLICY_NUMBER)
        member = await session.get(McInsuredMember, gen.MEMBER_ID)
    await engine.dispose()

    assert policy is not None and member is not None
    assert (member.name, member.dob, policy.sum_insured) == (
        gen.PATIENT,
        gen.PATIENT_DOB,
        gen.SUM_INSURED,
    )
    [hospital] = [h for h in HOSPITALS if h.rohini_id == gen.HOSPITAL_ROHINI]
    assert hospital.name == gen.HOSPITAL and hospital.differential_billing
    [agreement] = [a for a in AGREEMENTS if a.hospital_id == gen.HOSPITAL_ROHINI]
    assert (agreement.scheme, agreement.non_package_discount_percent) == (
        "CGHS",
        gen.NON_PACKAGE_DISCOUNT,
    )
    assert agreement.payee_name == gen.HOSPITAL and agreement.payee_account_number
