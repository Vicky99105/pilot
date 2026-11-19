"""NPPA ceiling prices (S.O. 1575(E) of 25.03.2026) and the NLEM 2022 medicine list."""

from decimal import Decimal

from app.knowledge.loaders.drugs import parse_nlem, parse_nppa_rows
from tests.knowledge import reference_sources as src

D = Decimal


def test_nppa_rows_give_the_formulation_unit_price_and_notification() -> None:
    rows = parse_nppa_rows([(31, src.NPPA_TABLE_P31)])

    assert [r.serial for r in rows] == [1, 573, 576]
    mesalazine, tablet, injection = rows
    assert mesalazine.medicine == "5-amino salicylic Acid (Mesalazine/Mesalamine)"
    assert (tablet.medicine, tablet.dosage_form, tablet.unit, tablet.ceiling_price) == (
        "Paracetamol",
        "Tablet 500mg",
        "1 Tablet",
        D("0.93"),
    )
    assert injection.dosage_form == "Injection 150mg/mL (2ML)"
    assert tablet.base_notification == "S.O. 1489(E) dated 27-03-2025"
    assert tablet.page == 31


def test_nlem_entries_carry_levels_and_formulations_and_skip_footnotes() -> None:
    entries = parse_nlem([(7, src.NLEM_P7), (8, src.NLEM_P8), (83, src.NLEM_P83)])

    by_number = {e.number: e for e in entries}
    assert list(by_number) == ["2.1.1", "2.1.2", "2.1.5", "2.2.1", "27.4"]
    aspirin = by_number["2.1.1"]
    assert (aspirin.medicine, aspirin.levels) == ("Acetylsalicylic acid", "P,S,T")
    assert aspirin.formulations == [
        "Tablet 300 mg to 500 mg",
        "Effervescent/ Dispersible/ Enteric coated Tablet 300 mg to 500 mg",
    ]
    paracetamol = by_number["2.1.5"]
    assert paracetamol.formulations == ["Tablet 500 mg", "Oral liquid 120 mg/5 mL (p)"]
    assert by_number["2.1.2"].formulations == ["Tablet 50 mg", "Injection 25 mg/mL"]
    assert by_number["2.2.1"].section.startswith("2 ")
    assert by_number["27.4"].page == 83
