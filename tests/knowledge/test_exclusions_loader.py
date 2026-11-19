"""Standard exclusions Excl01 to Excl18 and the wording's specific exclusions."""

from app.knowledge.loaders.exclusions import (
    STANDARD,
    find_codes,
    find_specific,
)
from tests.knowledge import reference_sources as src


def test_codes_are_found_whatever_the_text_extraction_made_of_them() -> None:
    found = find_codes([(6, src.EXCLUSIONS_P6), (8, src.EXCLUSIONS_P8), (9, src.EXCLUSIONS_P9)])

    assert found == {
        "Excl01": 6,
        "Excl02": 6,
        "Excl12": 8,
        "Excl13": 9,
        "Excl15": 9,
        "Excl18": 9,
    }
    assert find_codes([(10, src.WORDING_EXCLUSIONS_P10)]) == {"Excl15": 10, "Excl18": 10}


def test_every_standard_code_has_a_title_and_a_summary_in_our_words() -> None:
    assert [code for code, _, _ in STANDARD] == [f"Excl{n:02d}" for n in range(1, 19)]
    assert all(title and len(summary) > 40 for _, title, summary in STANDARD)


def test_specific_exclusions_are_found_by_number_and_subject() -> None:
    found = find_specific([(10, src.WORDING_EXCLUSIONS_P10), (11, src.WORDING_EXCLUSIONS_P11)])

    assert found == {"SE19": 11, "SE20": 11, "SE21": 11, "SE22": 11, "SE23": 11}
