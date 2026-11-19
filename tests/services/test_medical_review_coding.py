"""Medical review coding over CMS archives loaded through the real loaders: the constrained
ICD-10-PCS builder and the diagnosis reply schema. The model is scripted; the knowledge base
is real."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.seed import seed_claims_db
from app.knowledge.loaders.icd10cm import load_icd10cm
from app.knowledge.loaders.icd10pcs import load_icd10pcs
from app.services.knowledge_base.contract import ICD10PCS
from app.services.knowledge_base.service import DefaultKnowledgeBase
from app.services.medical_review.contract import Citation
from tests.fakes.core import FakePolicyService
from tests.fakes.model_gateway import FakeModelGateway
from tests.knowledge import sources
from tests.services.claim_builders import DIAGNOSIS, page
from tests.services.test_medical_review import Env, cite, enum_at
from tests.services.test_registration import asha_policy

PROCEDURE = "Laparoscopic appendicectomy"
PCS = "medical_pcs_step"
REWRITE = {"items": [{"phrase": DIAGNOSIS, "main_term": "appendicitis", "modifiers": ["acute"]}]}


def k35_80() -> dict[str, Any]:
    return {"codes": [{"code": "K35.80", "phrase": DIAGNOSIS, "citations": [cite(1)]}]}


def step(value: str) -> dict[str, str]:
    return {"value": value}


# The model's choices at the steps where more than one value is valid. Position 2 (only D
# under section 0) and position 6 (only Z, no device) are taken by code.
LAPAROSCOPIC_APPENDICECTOMY = [
    step("0: Medical and Surgical"),
    step("T: Resection"),
    step("J: Appendix"),
    step("4: Percutaneous Endoscopic"),
    step("Z: No Qualifier"),
]


@pytest.fixture
async def env(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> Env:
    await seed_claims_db(sessions)
    source_dir = sources.write_sources(tmp_path / "icd-2027")
    await load_icd10cm(sessions, source_dir)
    await load_icd10pcs(sessions, source_dir)
    return Env(sessions, FakePolicyService([asha_policy()]))


def gateway(*pcs_steps: dict[str, str]) -> FakeModelGateway:
    return FakeModelGateway(
        {
            "medical_rewrite_terms": [REWRITE],
            PCS: list(pcs_steps),
            "medical_choose_codes": [k35_80()],
        }
    )


async def test_the_procedure_is_coded_one_valid_character_at_a_time(
    env: Env, sessions: async_sessionmaker[AsyncSession]
) -> None:
    claim_id = await env.claim()
    model = gateway(*LAPAROSCOPIC_APPENDICECTOMY)

    findings = await env.service(model).code(claim_id)

    [procedure] = findings.procedure_codes
    assert procedure.code.code == "0DTJ4ZZ"
    assert procedure.code.system == ICD10PCS and procedure.code.version == "FY2027"
    assert procedure.code.display == "Resection of Appendix, Percutaneous Endoscopic Approach"
    assert procedure.phrase == PROCEDURE
    assert procedure.citations == [Citation(page=page(1))]
    assert [(s.position, s.prefix, s.value, s.chosen_by) for s in procedure.steps] == [
        (1, "", "0", "model"),
        (2, "0", "D", "code"),
        (3, "0D", "T", "model"),
        (4, "0DT", "J", "model"),
        (5, "0DTJ", "4", "model"),
        (6, "0DTJ4", "Z", "code"),
        (7, "0DTJ4Z", "Z", "model"),
    ]
    # Every step names the lookup that offered its value, and the final code was validated.
    calls = {c.call_id: c for c in findings.lookups}
    for s in procedure.steps:
        offered = calls[s.lookup_call_id]
        assert (offered.tool, offered.argument) == ("pcs_options", s.prefix)
        assert s.prefix + s.value in offered.returned_codes
    validated = calls[procedure.validate_call_id]
    assert (validated.tool, validated.argument, validated.returned_codes) == (
        "pcs_validate",
        "0DTJ4ZZ",
        ["0DTJ4ZZ"],
    )
    assert [c.purpose for c in model.calls].count(PCS) == 5


async def test_each_step_schema_is_exactly_the_valid_options_for_the_prefix(
    env: Env, sessions: async_sessionmaker[AsyncSession]
) -> None:
    claim_id = await env.claim()
    model = gateway(*LAPAROSCOPIC_APPENDICECTOMY)
    kb = DefaultKnowledgeBase(sessions)

    await env.service(model).code(claim_id)

    steps = [c for c in model.calls if c.purpose == PCS]
    for call, prefix in zip(steps, ["", "0D", "0DT", "0DTJ", "0DTJ4Z"], strict=True):
        options = [f"{o.value}: {o.label}" for o in await kb.pcs_options(prefix)]
        # The first step may also say that no procedure was performed.
        stop = ["none: no procedure was performed"] if prefix == "" else []
        assert enum_at(call.schema, "value") == options + stop, prefix
    last = steps[-1].schema
    assert last is not None
    with pytest.raises(ValidationError):
        last.model_validate(step("X: Diagnostic"))  # a value of another table
    with pytest.raises(ValidationError):
        last.model_validate(step("none: no procedure was performed"))


async def test_an_invalid_choice_cannot_be_returned(env: Env) -> None:
    claim_id = await env.claim()
    # "K: Ascending Colon" is valid at position 4; at position 5 only 0 and 4 are.
    model = gateway(
        step("0: Medical and Surgical"),
        step("T: Resection"),
        step("J: Appendix"),
        step("K: Ascending Colon"),
    )

    with pytest.raises(ValidationError):
        await env.service(model).code(claim_id)


async def test_no_procedure_performed_leaves_no_procedure_code(env: Env) -> None:
    claim_id = await env.claim()
    model = gateway(step("none: no procedure was performed"))

    findings = await env.service(model).code(claim_id)

    assert findings.procedure_codes == []
    [dropped] = [d for d in findings.dropped if d.item == "procedure"]
    assert (dropped.detail, dropped.reason) == (PROCEDURE, "no procedure was performed")


async def test_diagnosis_codes_are_billable_codes_the_lookups_returned(env: Env) -> None:
    claim_id = await env.claim()
    model = gateway(*LAPAROSCOPIC_APPENDICECTOMY)

    findings = await env.service(model).code(claim_id)

    choose = next(c for c in model.calls if c.purpose == "medical_choose_codes")
    # The index gives K35.80 and the subcategory K35.89-; the subcategory is not billable, so
    # its codes are looked up and offered instead.
    assert enum_at(choose.schema, "codes", "code") == ["K35.80", "K35.890", "K35.891"]
    assert choose.schema is not None
    with pytest.raises(ValidationError):
        choose.schema.model_validate(
            {"codes": [{"code": "K35.89", "phrase": DIAGNOSIS}], "findings": []}
        )
    assert [v.code.code for v in findings.verified_codes] == ["K35.80"]
    [verified] = findings.verified_codes
    assert verified.code.version == "FY2027"
    tabular = [c for c in findings.lookups if c.tool == "get_icd_tabular"]
    assert ("K35.89", ["K35.89", "K35.890", "K35.891"]) in [
        (c.argument, c.returned_codes) for c in tabular
    ]


async def test_index_queries_are_built_from_the_main_term_and_its_modifiers(env: Env) -> None:
    claim_id = await env.claim()
    rewrite = {
        "items": [
            {
                "phrase": DIAGNOSIS,
                "main_term": "Appendicitis",
                "modifiers": ["acute", "suppurative"],
            }
        ]
    }
    model = FakeModelGateway(
        {
            "medical_rewrite_terms": [rewrite],
            PCS: list(LAPAROSCOPIC_APPENDICECTOMY),
            "medical_choose_codes": [k35_80()],
        }
    )

    findings = await env.service(model).code(claim_id)

    searches = [c.argument for c in findings.lookups if c.tool == "search_icd_index"]
    # The whole phrase first, then the main term with each modifier on its own. Every query
    # starts with the main term, so no query is a bare modifier.
    assert searches == [
        "Appendicitis acute suppurative",
        "Appendicitis acute",
        "Appendicitis suppurative",
    ]
    schema = model.calls[0].schema
    assert schema is not None
    item = schema.model_json_schema()["$defs"]["TermRewrite"]
    assert set(item["required"]) >= {"phrase", "main_term"}
    assert "index_terms" not in item["properties"]


async def test_the_main_term_alone_is_searched_when_nothing_else_matches(env: Env) -> None:
    claim_id = await env.claim()
    rewrite = {"items": [{"phrase": DIAGNOSIS, "main_term": "appendicitis", "modifiers": ["x"]}]}
    model = FakeModelGateway(
        {
            "medical_rewrite_terms": [rewrite],
            PCS: list(LAPAROSCOPIC_APPENDICECTOMY),
            "medical_choose_codes": [k35_80()],
        }
    )

    findings = await env.service(model).code(claim_id)

    searches = [c.argument for c in findings.lookups if c.tool == "search_icd_index"]
    assert searches == ["appendicitis x", "appendicitis"]
    assert [v.code.code for v in findings.verified_codes] == ["K35.80"]
