"""Table tests for the slice P2-4 rules, through the RulesEngine contract and the real product
config: package pricing (R20, R23, R24, the multi-procedure rule), the line rules R12, R14
and R18, the sub-limits R16, R17 and R19, the pre-auth check R26, the eligibility rules R6,
R7 and R9 on verified codes, and the drug price check."""

from datetime import date

import pytest

from app.services.rules.contract import (
    DrugFacts,
    Facts,
    Head,
    LineFacts,
    MappedPackage,
    NonPayableItem,
    NonPayableJudgement,
    PackageFacts,
    PreAuthFacts,
    ProductConfig,
    RoomFacts,
    RulesEngine,
)
from tests.services.test_rules import ADMITTED, D, engine, one, policy, product

__all__ = ["engine", "product"]  # pytest fixtures used by name


def line(head: Head, amount: str, *, line_id: int = 1, desc: str = "x", **extra: object
         ) -> LineFacts:  # fmt: skip
    return LineFacts(line_id=line_id, head=head, description=desc, claimed=D(amount), **extra)


def pkg(code: str, rate: str, *, addon: bool = False, standalone: bool = False) -> MappedPackage:
    return MappedPackage(
        scheme="CGHS", package_code=code, name=code, rate=D(rate), addon=addon,
        standalone=standalone,
    )  # fmt: skip


# --- R20: package cap and the multi-procedure rule ----------------------------------------


@pytest.mark.parametrize(
    ("packages", "billed", "effect", "passed"),
    [
        ([pkg("AG095", "33000")], "93003.00", "-60003.00", False),
        ([pkg("AG095", "33000")], "30000.00", "0", True),  # the bill is below the package
        # 100% of the highest, 50% of the second, 25% of the rest: 40000 + 10000 + 2500.
        ([pkg("B", "20000"), pkg("A", "40000"), pkg("C", "10000")], "90000", "-37500.00", False),
        # An add-on is paid in full and does not count as a second procedure.
        ([pkg("A", "40000"), pkg("ADD", "8000", addon=True), pkg("B", "20000")], "90000",
         "-32000.00", False),
        # A stand-alone package is paid alone: the others are not paid.
        ([pkg("A", "40000"), pkg("SA", "25000", standalone=True)], "90000", "-65000.00", False),
    ],
    ids=["one_package", "below_package", "three_procedures", "addon", "standalone"],
)  # fmt: skip
def test_r20_caps_the_package_charges_at_the_package_total(
    engine: RulesEngine,
    product: ProductConfig,
    packages: list[MappedPackage],
    billed: str,
    effect: str,
    passed: bool,
) -> None:
    facts = Facts(
        package=PackageFacts(packages=packages, billed_as_package=False, package_total=D(billed))
    )

    result = one(engine, "R20", facts, product)

    assert (result.amount_effect, result.passed) == (D(effect), passed)
    assert result.clause_ref == "AGREED-TARIFF"


def test_r20_names_each_package_and_its_share(engine: RulesEngine, product: ProductConfig) -> None:
    packages = [pkg("A", "40000"), pkg("B", "20000"), pkg("SA", "25000", standalone=True)]
    facts = Facts(package=PackageFacts(packages=packages, billed_as_package=False,
                                       package_total=D("90000")))  # fmt: skip

    result = one(engine, "R20", facts, product)

    assert "SA" in result.reason and "stand-alone" in result.reason
    assert "A at 0%" in result.reason and "B at 0%" in result.reason


def test_r20_without_a_package_passes(engine: RulesEngine, product: ProductConfig) -> None:
    result = one(engine, "R20", Facts(), product)

    assert (result.passed, result.amount_effect) == (True, D("0"))
    assert "no package" in result.reason


# --- R23: items already inside a package ---------------------------------------------------


@pytest.mark.parametrize(
    ("head", "billed_as_package", "effect"),
    [
        (Head.CONSUMABLES, True, "-4850.00"),  # unbundled beside a package charge
        (Head.PHARMACY, True, "-4850.00"),
        (Head.IMPLANTS, True, "0"),  # implants are paid outside the package
        (Head.PACKAGE, True, "0"),  # the package charge itself: R20 prices it
        (Head.CONSUMABLES, False, "0"),  # itemised bill: R20 caps the lines together
    ],
    ids=["consumables", "pharmacy", "implant", "package_line", "itemised"],
)
def test_r23_deducts_lines_a_package_charge_already_covers(
    engine: RulesEngine,
    product: ProductConfig,
    head: Head,
    billed_as_package: bool,
    effect: str,
) -> None:
    facts = Facts(
        line=line(head, "4850"),
        package=PackageFacts(packages=[pkg("AG095", "33000")], billed_as_package=billed_as_package),
    )

    result = one(engine, "R23", facts, product)

    assert result.amount_effect == D(effect)
    assert result.passed is (D(effect) == 0)
    if not result.passed:
        assert result.clause_ref == "PACKAGE-INCL" and "AG095" in result.reason


# --- R24: non-package discount -------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "package", "percent", "effect"),
    [
        (Head.ROOM, None, "10", "-750.00"),
        (Head.CONSUMABLES, None, "10", "-750.00"),
        (Head.PHARMACY, None, "10", "0"),  # medicines are never discounted
        (Head.IMPLANTS, None, "10", "0"),  # nor implants
        (Head.ROOM, PackageFacts(packages=[pkg("A", "1")], billed_as_package=False), "10", "0"),
        (Head.IMPLANTS, PackageFacts(packages=[pkg("A", "1")], billed_as_package=False), "10",
         "0"),
        (Head.AMBULANCE, PackageFacts(packages=[pkg("A", "1")], billed_as_package=False), "10",
         "-750.00"),  # outside the package: discounted
        (Head.ROOM, None, None, "0"),  # no agreement, no discount
        (Head.ROOM, None, "0", "0"),
    ],
    ids=["room", "consumables", "pharmacy", "implants", "in_package", "implant_in_package",
         "outside_package", "no_agreement", "zero_percent"],
)  # fmt: skip
def test_r24_discounts_non_package_lines_except_medicines_and_implants(
    engine: RulesEngine,
    product: ProductConfig,
    head: Head,
    package: PackageFacts | None,
    percent: str | None,
    effect: str,
) -> None:
    facts = Facts(
        line=line(head, "7500"),
        package=package,
        non_package_discount_percent=None if percent is None else D(percent),
    )

    result = one(engine, "R24", facts, product)

    assert result.amount_effect == D(effect)
    if result.amount_effect:
        assert (result.passed, result.clause_ref) == (False, "NON-PKG-DISC")


# --- R13 and R14: package lines are priced by the package ----------------------------------


def test_r13_and_r14_leave_package_lines_to_the_package(
    engine: RulesEngine, product: ProductConfig
) -> None:
    package = PackageFacts(packages=[pkg("AG095", "33000")], billed_as_package=False)
    room = RoomFacts(sum_insured=D("500000"), room_rate_per_day=D("7500"),
                     differential_billing=True)  # fmt: skip
    room_line = line(Head.ROOM, "7500", qty=D(1), rate=D(7500))
    icu_line = line(Head.ICU, "15000", qty=D(1), rate=D(15000))

    r13 = one(engine, "R13", Facts(line=room_line, room=room, package=package), product)
    r14 = one(engine, "R14", Facts(line=icu_line, room=room, package=package), product)

    assert (r13.passed, r13.amount_effect) == (True, D("0"))
    assert (r14.passed, r14.amount_effect) == (True, D("0"))
    assert "package" in r13.reason and "package" in r14.reason


@pytest.mark.parametrize(
    ("head", "rate", "effect", "clause"),
    [
        (Head.ICU, "12000", "-4000.00", "ICU-CAP"),  # 5% of 3 lakh is 15000; max 10000
        (Head.ICU, "9000", "0", "ICU-CAP"),
        (Head.ROOM, "7500", "0", None),  # a room line: R13's, not R14's
    ],
    ids=["over_cap", "under_cap", "room_line"],
)
def test_r14_icu_cap(
    engine: RulesEngine, product: ProductConfig, head: Head, rate: str, effect: str,
    clause: str | None,
) -> None:  # fmt: skip
    room = RoomFacts(sum_insured=D("300000"), room_rate_per_day=None, differential_billing=True)
    icu = line(head, str(D(rate) * 2), qty=D(2), rate=D(rate))

    result = one(engine, "R14", Facts(line=icu, room=room), product)

    assert (result.amount_effect, result.clause_ref) == (D(effect), clause)


def test_r13_leaves_icu_lines_to_r14(engine: RulesEngine, product: ProductConfig) -> None:
    room = RoomFacts(sum_insured=D("300000"), room_rate_per_day=None, differential_billing=True)
    icu = line(Head.ICU, "24000", qty=D(2), rate=D(12000))

    result = one(engine, "R13", Facts(line=icu, room=room), product)

    assert (result.passed, result.amount_effect) == (True, D("0"))
    assert "R14" in result.reason


# --- R12: the model's reading of an unclear line --------------------------------------------

NURSING_ITEM = NonPayableItem(
    list_name="II", item_number=38, item="Nursing, DMO/ RMO charges included in room rent",
    payable_in_product="not_payable",
)  # fmt: skip
URINE_BAG = NonPayableItem(list_name="IV", item_number=18, item="URINE BAG",
                           payable_in_product="payable_within_limits")  # fmt: skip


@pytest.mark.parametrize(
    ("item", "confidence", "passed", "effect", "clause"),
    [
        (None, 0.95, True, "0", None),  # the model reads the line as payable
        (NURSING_ITEM, 0.9, False, "-900.00", "ANNEX-II"),
        (URINE_BAG, 0.9, True, "0", None),  # an item the product pays
        (NURSING_ITEM, 0.6, False, "0", None),  # unsure: a person decides; nothing deducted
    ],
    ids=["payable", "non_payable", "paid_item", "unsure"],
)
def test_r12_applies_the_reading_and_leaves_unsure_lines_to_a_person(
    engine: RulesEngine,
    product: ProductConfig,
    item: NonPayableItem | None,
    confidence: float,
    passed: bool,
    effect: str,
    clause: str | None,
) -> None:
    facts = Facts(
        line=line(Head.NURSING, "900", desc="Nursing Care Charges"),
        judgement=NonPayableJudgement(item=item, confidence=confidence),
    )

    [result] = engine.evaluate(["R12"], facts, product)

    assert (result.passed, result.amount_effect, result.clause_ref) == (passed, D(effect), clause)
    assert result.judged_by == "model"
    if confidence < product.non_payable_judging.min_confidence:
        assert "a person decides" in result.reason


# --- R16, R17, R19: sub-limits ------------------------------------------------------------


@pytest.mark.parametrize(
    ("codes", "si", "admissible", "effect"),
    [
        (["08DJ3ZZ"], "500000", "55000", "-15000.00"),  # one eye: min(125000, 40000)
        (["08DJ3ZZ", "08RJ3JZ"], "500000", "55000", "-15000.00"),  # still one eye
        (["08DJ3ZZ", "08DK3ZZ"], "500000", "95000", "-15000.00"),  # both eyes: 80000
        (["08DK3ZZ"], "100000", "30000", "-5000.00"),  # 25% of Rs 1 lakh is lower
        (["08DJ3ZZ"], "500000", "35000", "0"),
        (["0DTJ4ZZ"], "500000", "93000", "0"),  # not cataract
    ],
    ids=["one_eye", "extraction_and_lens", "both_eyes", "small_si", "within", "not_cataract"],
)
def test_r16_cataract_limit_per_eye(
    engine: RulesEngine,
    product: ProductConfig,
    codes: list[str],
    si: str,
    admissible: str,
    effect: str,
) -> None:
    facts = Facts(sum_insured=D(si), procedure_codes=codes, admissible_total=D(admissible))

    result = one(engine, "R16", facts, product)

    assert result.amount_effect == D(effect)
    assert result.clause_ref == "CATARACT" or result.passed


@pytest.mark.parametrize(
    ("codes", "admissible", "effect", "named"),
    [
        (["8E0W4CZ"], "180000", "-30000.00", "Robotic surgeries"),  # 50% of 3 lakh
        (["DG20DZZ"], "120000", "0", "Stereotactic radio surgeries"),
        (["0DTJ4ZZ"], "180000", "0", None),
    ],
    ids=["robotic_over", "radiosurgery_within", "not_modern"],
)
def test_r17_modern_treatments_half_the_sum_insured(
    engine: RulesEngine,
    product: ProductConfig,
    codes: list[str],
    admissible: str,
    effect: str,
    named: str | None,
) -> None:
    facts = Facts(sum_insured=D("300000"), procedure_codes=codes, admissible_total=D(admissible))

    result = one(engine, "R17", facts, product)

    assert result.amount_effect == D(effect)
    if named:
        assert named in result.reason and result.clause_ref == "MODERN-TX"


@pytest.mark.parametrize(
    ("total", "effect"),
    [("3500", "-1500.00"), ("2000", "0"), ("0", "0")],
    ids=["over", "at_cap", "none"],
)
def test_r19_ambulance_cap(
    engine: RulesEngine, product: ProductConfig, total: str, effect: str
) -> None:
    result = one(engine, "R19", Facts(ambulance_total=D(total)), product)

    assert result.amount_effect == D(effect)
    assert result.clause_ref == "AMBULANCE"


# --- R18: pre and post hospitalisation windows -------------------------------------------

DISCHARGED = date(2026, 11, 6)


@pytest.mark.parametrize(
    ("served", "effect"),
    [
        (date(2026, 10, 3), "0"),  # 30 days before admission: inside
        (date(2026, 10, 2), "-1200.00"),  # 31 days before
        (date(2026, 11, 4), "0"),  # during the stay
        (date(2027, 1, 5), "0"),  # 60 days after discharge
        (date(2027, 1, 6), "-1200.00"),  # 61 days after
        (None, "0"),  # no date on the line
    ],
    ids=["pre_30", "pre_31", "stay", "post_60", "post_61", "no_date"],
)
def test_r18_pre_and_post_hospitalisation_windows(
    engine: RulesEngine, product: ProductConfig, served: date | None, effect: str
) -> None:
    facts = Facts(
        line=line(Head.DIAGNOSTICS, "1200", service_date=served),
        admission_date=ADMITTED,
        discharge_date=DISCHARGED,
    )

    result = one(engine, "R18", facts, product)

    assert result.amount_effect == D(effect)
    assert result.clause_ref == "PRE-POST"


# --- R26: final bill against the approved pre-auth ----------------------------------------


@pytest.mark.parametrize(
    ("preauth", "verified", "passed", "effect"),
    [
        (None, ["K35.80"], True, "0"),  # reimbursement with no pre-auth
        (PreAuthFacts(approved_total=D("60000"), diagnosis_codes=["K35.80"]), ["K35.89"],
         True, "0"),
        (PreAuthFacts(approved_total=D("50000"), diagnosis_codes=["K35.80"]), ["K35.80"],
         False, "-5000.00"),
        (PreAuthFacts(approved_total=D("60000"), diagnosis_codes=["K80.20"]), ["K35.80"],
         False, "0"),  # the diagnosis changed since the pre-auth
    ],
    ids=["no_preauth", "within", "over_approved", "diagnosis_changed"],
)  # fmt: skip
def test_r26_final_bill_against_the_approved_preauth(
    engine: RulesEngine,
    product: ProductConfig,
    preauth: PreAuthFacts | None,
    verified: list[str],
    passed: bool,
    effect: str,
) -> None:
    facts = Facts(preauth=preauth, admissible_total=D("55000"), diagnosis_codes=verified)

    result = one(engine, "R26", facts, product)

    assert (result.passed, result.amount_effect) == (passed, D(effect))
    assert result.clause_ref == "PREAUTH"


# --- R6, R7, R9 on verified codes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("cover_from", "onsets", "declared", "codes", "passed"),
    [
        (date(2025, 1, 1), [], [], ["K35.80"], True),  # nothing pre-existing
        (date(2025, 1, 1), [date(2023, 6, 1)], [], ["K35.80"], False),  # PED in the wait
        (date(2022, 1, 1), [date(2021, 6, 1)], [], ["K35.80"], True),  # 36 months served
        (date(2025, 1, 1), [], ["E11"], ["E11.65"], False),  # a declared PED treated
        (date(2025, 1, 1), [], ["E11"], ["K35.80"], True),
    ],
    ids=["none", "found_in_wait", "wait_served", "declared", "declared_not_treated"],
)
def test_r6_pre_existing_disease_waiting(
    engine: RulesEngine,
    product: ProductConfig,
    cover_from: date,
    onsets: list[date],
    declared: list[str],
    codes: list[str],
    passed: bool,
) -> None:
    from app.shared.types import Code

    member = policy(
        inception_date=cover_from,
        continuous_cover_start=cover_from,
        declared_peds=[Code(system="ICD-10-CM", version="x", code=c) for c in declared],
    )
    facts = Facts(policy=member, admission_date=ADMITTED, diagnosis_codes=codes,
                  pre_existing_onsets=onsets)  # fmt: skip

    result = one(engine, "R6", facts, product)

    assert result.passed is passed
    assert result.clause_ref == "Excl01"


@pytest.mark.parametrize(
    ("cover_from", "cm", "pcs", "accident", "passed", "named"),
    [
        (date(2025, 1, 1), ["K35.80"], ["0DTJ4ZZ"], False, True, None),
        (date(2025, 6, 1), ["K40.90"], [], False, False, "Hernia of all types"),
        (date(2024, 6, 1), ["K40.90"], [], False, True, None),  # 24 months served
        (date(2025, 6, 1), [], ["0CTP0ZZ"], False, False, "Tonsillectomy"),
        (date(2024, 6, 1), ["M17.11"], ["0SRD0J9"], False, False,
         "Treatment for joint replacement"),  # 36 months for joint replacement
        (date(2025, 6, 1), ["M51.26"], [], True, True, None),  # accident exempt
    ],
    ids=["appendicitis", "hernia_in_wait", "hernia_served", "tonsillectomy",
         "joint_replacement_36", "accident"],
)  # fmt: skip
def test_r7_specific_waiting_on_verified_codes(
    engine: RulesEngine,
    product: ProductConfig,
    cover_from: date,
    cm: list[str],
    pcs: list[str],
    accident: bool,
    passed: bool,
    named: str | None,
) -> None:
    member = policy(inception_date=cover_from, continuous_cover_start=cover_from)
    facts = Facts(policy=member, admission_date=ADMITTED, accident=accident,
                  diagnosis_codes=cm, procedure_codes=pcs)  # fmt: skip

    result = one(engine, "R7", facts, product)

    assert result.passed is passed
    assert result.clause_ref == "Excl02"
    if named:
        assert named in result.reason


@pytest.mark.parametrize(
    ("codes", "passed", "clause"),
    [
        (["K35.80"], True, None),
        (["F10.20"], False, "Excl12"),
        (["O80"], False, "Excl18"),
        (["O00.101"], True, None),  # ectopic pregnancy is covered
        (["N97.9"], False, "Excl17"),
    ],
    ids=["appendicitis", "alcohol", "delivery", "ectopic", "infertility"],
)
def test_r9_standard_exclusions_a_code_shows(
    engine: RulesEngine, product: ProductConfig, codes: list[str], passed: bool,
    clause: str | None,
) -> None:  # fmt: skip
    result = one(engine, "R9", Facts(diagnosis_codes=codes), product)

    assert (result.passed, result.clause_ref) == (passed, clause)


# --- Drug price check: a finding, never a deduction ---------------------------------------


@pytest.mark.parametrize(
    ("desc", "qty", "rate", "drug", "passed"),
    [
        # 63.88 plus 12% GST is 71.55: Rs 68.50 is within the ceiling.
        ("Inj. Ceftriaxone 1 g", "6", "68.50",
         DrugFacts(medicine="Ceftriaxone", dosage_form="Powder for Injection 1000mg",
                   unit="1 Vial", ceiling_price=D("63.88")), True),
        ("Inj. Ceftriaxone 1 g", "1", "72.00",
         DrugFacts(medicine="Ceftriaxone", dosage_form="Powder for Injection 1000mg",
                   unit="1 Vial", ceiling_price=D("63.88")), False),
        # Priced per mL: a 100 mL bottle's ceiling is 100 x 0.21 = 21.00, 23.52 with GST.
        ("Inj. Metronidazole 100 ml", "6", "32.00",
         DrugFacts(medicine="Metronidazole", dosage_form="Injection 500mg/100mL",
                   unit="1 ML", ceiling_price=D("0.21")), False),
        # 50 mg at 50 mg/mL is 1 mL: 12.07, 13.52 with GST.
        ("Inj. Tramadol 50 mg", "4", "18.50",
         DrugFacts(medicine="Tramadol", dosage_form="Injection 50mg/mL (upto 2ml pack)",
                   unit="1 ML", ceiling_price=D("12.07")), False),
        ("Tab. Cefuroxime 500 mg", "10", "42.80",
         DrugFacts(medicine="Cefuroxime", dosage_form="Tablet 500mg", unit="1 Tablet",
                   ceiling_price=D("52.67")), True),
    ],
    ids=["ceftriaxone_with_gst", "ceftriaxone_over", "metronidazole_per_ml",
         "tramadol_per_ml", "cefuroxime"],
)  # fmt: skip
def test_drug_price_compares_against_the_ceiling_plus_gst(
    engine: RulesEngine,
    product: ProductConfig,
    desc: str,
    qty: str,
    rate: str,
    drug: DrugFacts,
    passed: bool,
) -> None:
    facts = Facts(
        line=line(Head.PHARMACY, str(D(qty) * D(rate)), desc=desc, qty=D(qty), rate=D(rate)),
        drug=drug,
    )

    result = one(engine, "DRUG-PRICE", facts, product)

    assert result.passed is passed
    assert result.amount_effect == D("0")  # a finding, never a deduction
    assert "GST" in result.reason


def test_drug_price_with_units_it_cannot_compare_passes_and_says_so(
    engine: RulesEngine, product: ProductConfig
) -> None:
    drug = DrugFacts(medicine="Paracetamol", dosage_form="Oral liquid 125mg/5mL", unit="1 ML",
                     ceiling_price=D("0.50"))  # fmt: skip
    facts = Facts(line=line(Head.PHARMACY, "60", desc="Syp. Paracetamol", qty=D(1),
                            rate=D(60)), drug=drug)  # fmt: skip

    result = one(engine, "DRUG-PRICE", facts, product)

    assert result.passed and "cannot compare" in result.reason


# --- Spreading a claim-level amount over lines --------------------------------------------


@pytest.mark.parametrize(
    ("amount", "weights", "shares"),
    [
        ("-60003.00", ["30000", "0", "63003"], ["-19355.18", "0.00", "-40647.82"]),
        ("-100.00", ["1", "1", "1"], ["-33.33", "-33.33", "-33.34"]),
        ("-50.00", ["0", "0"], ["0.00", "0.00"]),
    ],
    ids=["pro_rata", "remainder_on_last", "no_weight"],
)
def test_allocate_spreads_an_amount_in_proportion_and_sums_exactly(
    engine: RulesEngine, amount: str, weights: list[str], shares: list[str]
) -> None:
    result = engine.allocate(D(amount), [D(w) for w in weights])

    assert result == [D(s) for s in shares]
    if any(D(w) > 0 for w in weights):
        assert sum(result, D("0")) == D(amount)
