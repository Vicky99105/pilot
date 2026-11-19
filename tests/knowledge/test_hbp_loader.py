"""PM-JAY HBP 2022 package rows from the Nagaland SHA copy."""

from decimal import Decimal

from app.knowledge.loaders.hbp import join_cell, parse_hbp_rows
from tests.knowledge import reference_sources as src

D = Decimal


def test_package_rows_are_read_and_state_only_codes_dropped() -> None:
    rows, dropped = parse_hbp_rows([(210, src.HBP_TABLE_P210)])

    assert [r.procedure_code for r in rows] == ["SG017A", "SP03B", "SU061A", "MG0111A"]
    assert dropped == ["NAG001"]
    appendix = rows[0]
    assert (appendix.package_code, appendix.name, appendix.rate, appendix.alos_days) == (
        "SG017",
        "Appendicectomy- Open",
        D("21700"),
        3,
    )
    assert appendix.specialty == "General Surgery, Pediatric Surgery"
    assert (appendix.multiple_procedures, appendix.public_only, appendix.stratification) == (
        True,
        False,
        False,
    )
    assert appendix.page == 210


def test_cells_are_repaired_where_the_pdf_wrapped_them() -> None:
    rows, _ = parse_hbp_rows([(210, src.HBP_TABLE_P210)])
    limb, drainage, effusion = rows[1], rows[2], rows[3]

    assert limb.specialty == "Plastic & Reconstructive"
    assert limb.name == "Revascularization of limb"
    assert limb.rate == D("112500")
    assert limb.implant == "B. >1digit/limb : cost -"
    assert drainage.rate == D("6400")
    assert drainage.stratification is False  # "N o"
    assert (drainage.alos_days, drainage.los_text) == (5, "3-5 days")
    assert effusion.stratification_remarks == "Routine Ward - 2310HDU - 3300"


def test_join_cell_keeps_hyphens_and_spaces_between_words() -> None:
    assert join_cell("follow￾\nup dressings") == "follow-up dressings"
    assert join_cell("Pancreatecto\nmy") == "Pancreatectomy"
    assert join_cell("Partial\nCystectomy") == "Partial Cystectomy"
    assert join_cell(None) == ""
