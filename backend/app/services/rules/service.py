"""Default rules engine: the rule registry plus product configs read from YAML files."""

from functools import cached_property
from pathlib import Path

import yaml

from app.integrations.core.contract import InsuredMember
from app.services.rules._helpers import RULES, match_member
from app.services.rules.contract import (
    Claimant,
    Facts,
    MemberMatch,
    ProductConfig,
    UnknownProduct,
    UnknownRule,
)
from app.shared.types import RuleResult


class DefaultRulesEngine:
    def __init__(self, products_dir: Path) -> None:
        self._dir = products_dir

    def evaluate(
        self, rule_ids: list[str], facts: Facts, product: ProductConfig
    ) -> list[RuleResult]:
        unknown = [r for r in rule_ids if r not in RULES]
        if unknown:
            raise UnknownRule(", ".join(unknown))
        return [RULES[r](facts, product) for r in rule_ids]

    def match_member(self, claimant: Claimant, members: list[InsuredMember]) -> MemberMatch:
        return match_member(claimant, members)

    def product(self, product_uin: str) -> ProductConfig:
        try:
            return self._products[product_uin]
        except KeyError:
            raise UnknownProduct(product_uin) from None

    @cached_property
    def _products(self) -> dict[str, ProductConfig]:
        products = {}
        for path in sorted(self._dir.glob("*.yaml")):
            config = ProductConfig.model_validate(yaml.safe_load(path.read_text()))
            products[config.product_uin] = config
        return products
