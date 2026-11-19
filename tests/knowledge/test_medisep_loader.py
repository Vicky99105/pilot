"""Kerala MEDISEP Phase II package list: LOS norms and the three accreditation rates."""

from decimal import Decimal

from app.knowledge.loaders.medisep import parse_medisep_rows
from tests.knowledge import reference_sources as src

D = Decimal


def test_rows_give_los_rates_and_the_procedure_label() -> None:
    necrosectomy, gastro = parse_medisep_rows([(200, src.MEDISEP_TABLE_P200)])

    assert (necrosectomy.code, necrosectomy.package_name, necrosectomy.los_days) == (
        "MS1421",
        "Pancreatic Necrosectomy",
        6,
    )
    assert necrosectomy.rates == {
        "no_nabh": D("86250"),
        "entry_nabh": D("93750"),
        "nabh_full": D("112500"),
    }
    assert (necrosectomy.addon, necrosectomy.standalone, necrosectomy.page) == (
        False,
        False,
        200,
    )
    assert (gastro.los_days, gastro.los_text) == (0, "Day care")
    assert gastro.name == "Acute gastroenteritis with moderate dehydration"
    assert (gastro.addon, gastro.addon_applicable, gastro.medical_or_surgical) == (
        True,
        True,
        "Medical",
    )
