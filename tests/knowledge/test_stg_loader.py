"""PM-JAY standard treatment guidelines: the compilation table, the STG PDFs' sections, and
matching HBP 2.0 codes to HBP 2022 by procedure name."""

from app.knowledge.loaders.stg import (
    match_codes,
    parse_compilation,
    parse_stg_pdf,
    serial_codes,
)
from tests.knowledge import reference_sources as src


def test_compilation_rows_group_into_documents_per_procedure() -> None:
    recovered = serial_codes([(219, src.STG_TEXT_P219)])
    procedures = parse_compilation([(120, src.STG_TABLE_P120)], recovered)

    appendix = procedures["SG017A"]
    assert appendix.name == "Appendicectomy Open"
    assert appendix.los_text == "3 days"
    assert appendix.preauth_documents == ["Clinical notes", "USG Abdomen"]
    assert appendix.claim_documents == [
        "Indoor case papers",
        "Histopathology report",
        "Discharge summary",
    ]
    assert appendix.page == 120
    atresia = procedures["SG014A"]  # recovered from the page text by its serial number
    assert atresia.recovered
    assert "Congenital Atresia & Stenosis of Small Intestine" in atresia.name


def test_an_stg_pdf_gives_its_codes_and_sections_as_short_lists() -> None:
    stg = parse_stg_pdf([(1, src.STG_PDF_P1), (2, src.STG_PDF_P2)])

    assert stg.codes == ["SG017A", "SG017B"]
    assert stg.title == "Appendicectomy/Appendicular Perforation/Appendicular Abscess"
    assert stg.sections["clinical_pointers"].items == [
        "Pain starting in the central abdomen and then shifting to the lower right side of abdomen",
        "Nausea or Vomiting",
    ]
    assert stg.sections["preauth_checks"].items == [
        "Clinical notes clearly indicating pain in abdomen and the need for surgery?"
    ]
    assert stg.sections["claim_checks"].items[1] == (
        "Are detailed operative notes available with indications for surgery and outcomes of "
        "the procedure?"
    )
    assert stg.sections["claim_checks"].page == 2
    assert stg.sections["tms_rules"].items == ["Appendectomy has never been done in the past?"]


def test_codes_match_hbp_2022_by_name_keeping_a_code_that_still_fits() -> None:
    hbp = [
        ("SG017A", "Appendicectomy", "Appendicectomy- Open"),
        ("SG017B", "Appendicectomy", "Lap- Appendicectomy"),
        ("SG072A", "Congenital Atresia & Stenosis of Small Intestine",
         "Congenital Atresia & Stenosis of Small Intestine"),
        ("SU001A", "Cystolithotomy", "Cystolithotomy - Open"),
    ]  # fmt: skip
    old = {
        "SG017A": ["Appendicectomy Open"],
        "SG014A": ["Congenital Atresia & Stenosis of Small Intestine"],
        "ZZ999A": ["Heart transplant"],
    }

    matched = match_codes(old, hbp)

    assert matched["SG017A"][0] == "SG017A"
    assert matched["SG014A"][0] == "SG072A"
    assert matched["ZZ999A"][0] is None
