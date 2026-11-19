"""Non-payable lists I to IV: the parser on wording-shaped text, the label and note split, the
product flag, and the cross-check against another copy of the lists."""

from app.knowledge.loaders.non_payable import (
    classify,
    compare_lists,
    parse_lists,
    split_note,
)
from tests.knowledge import reference_sources as src


def _wording() -> list[tuple[int, str]]:
    return [(20, src.WORDING_LISTS_P20), (21, src.WORDING_LISTS_P21)]


def test_items_are_numbered_within_each_list_and_wrapped_notes_join() -> None:
    items = parse_lists(_wording())

    by_list: dict[str, list[int]] = {}
    for item in items:
        by_list.setdefault(item.list_name, []).append(item.number)
    assert by_list == {"I": [1, 2, 3, 4, 5, 6], "II": [1, 2, 3], "III": [1, 2, 3], "IV": [1, 2, 3]}
    belts = items[1]
    assert belts.text.endswith("Surgery of thoracic or lumbar spine)")
    assert (belts.page, items[8].page) == (20, 21)  # ADMISSION KIT is on page 21


def test_list_v_and_what_follows_are_not_read() -> None:
    items = parse_lists(_wording())

    assert all("Brush" not in i.text for i in items)


def test_a_heading_or_paragraph_inside_a_list_does_not_break_the_numbering() -> None:
    items = parse_lists([(26, src.CIRCULAR_LISTS_P26)])

    assert [(i.list_name, i.number) for i in items] == [
        ("I", 1),
        ("I", 2),
        ("I", 3),
        ("I", 4),
        ("I", 5),
        ("I", 6),
        ("II", 1),
        ("II", 2),
        ("II", 3),
    ]
    assert items[2].text == "FOOD CHARGES (OTHER THAN PATIENT's DIET PROVIDED BY HOSPITAL)"


def test_the_note_is_split_from_the_label_but_a_label_keeps_its_own_brackets() -> None:
    assert split_note("BABY FOOD (Not Payable)") == ("BABY FOOD", "Not Payable")
    assert split_note(
        "FOOD CHARGES (OTHER THAN PATIENT's DIET PROVIDED BY HOSPITAL) (Not Payable)"
    ) == ("FOOD CHARGES (OTHER THAN PATIENT's DIET PROVIDED BY HOSPITAL)", "Not Payable")
    assert split_note(
        "DISPOSABLES RAZORS CHARGES (for site preparations) (Payable for site preparations)"
    ) == ("DISPOSABLES RAZORS CHARGES (for site preparations)", "Payable for site preparations")
    assert split_note("TORNIQUET Not payable (service is charged by hospital.") == (
        "TORNIQUET",
        "Not payable (service is charged by hospital.",
    )
    assert split_note("HOSPITALISATION FOR EVALUATION Unless A Claim Is Accepted") == (
        "HOSPITALISATION FOR EVALUATION",
        "Unless A Claim Is Accepted",
    )
    assert split_note("SERVICE CHARGES WHERE NURSING CHARGE ALSO CHARGED") == (
        "SERVICE CHARGES WHERE NURSING CHARGE ALSO CHARGED",
        "",
    )
    assert split_note("NIMBUS BED CHARGES (Payable for any ICU patient") == (
        "NIMBUS BED CHARGES",
        "Payable for any ICU patient",
    )


def test_each_item_gets_a_payable_flag_and_a_condition_in_our_words() -> None:
    flags = {(i.list_name, i.number): classify(i) for i in parse_lists(_wording())}

    assert flags[("I", 1)].flag == "not_payable"
    assert flags[("I", 1)].condition == "Not covered by the policy"
    assert flags[("II", 2)].flag == "not_payable"
    assert flags[("II", 2)].condition == "Part of the room charges; not paid as a separate item"
    gloves = flags[("I", 4)]
    assert gloves.label == "GLOVES"
    assert gloves.flag == "not_payable_unless"
    assert "sterile" in (gloves.condition or "").lower()
    assert flags[("IV", 3)].flag == "payable_within_limits"  # urine bag, one a day
    # An item with no note follows its list.
    assert flags[("I", 5)].flag == "not_payable"
    assert flags[("I", 5)].reviewed


def test_an_unknown_note_is_reported_not_guessed() -> None:
    [item] = [i for i in parse_lists(_wording()) if i.list_name == "IV" and i.number == 1]
    odd = item.__class__(list_name="IV", number=99, text="NEW ITEM (Payable on Tuesdays)", page=1)

    flagged = classify(odd)

    assert flagged.flag == "not_payable_unless"
    assert not flagged.reviewed


def test_the_cross_check_counts_labels_that_agree_by_list_and_number() -> None:
    primary = parse_lists(_wording())
    other = parse_lists([(26, src.CIRCULAR_LISTS_P26)])

    result = compare_lists(primary, other)

    # I 1-6 and II 1-3 are in both copies; BELTS/BRACES is spaced differently but agrees.
    assert (result.compared, result.agreed) == (9, 9)
    assert result.only_primary == 6  # III and IV are not in the excerpt
