from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import (
    DocumentChecklistRow,
    HospitalRow,
    NonPayableItemRow,
    PolicyClauseRow,
    UserRoleRow,
    UserRow,
)
from app.database.seed import seed_claims_db
from app.services.rules.service import DefaultRulesEngine
from app.shared.seed_ids import DOCTOR_USER_ID, OPS_USER_ID, PRODUCT_UIN, SENIOR_USER_ID


async def _count(sessions: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_seed_is_idempotent_and_creates_six_users_and_five_hospitals(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    await seed_claims_db(sessions)

    assert await _count(sessions, UserRow) == 6
    assert await _count(sessions, HospitalRow) == 5
    assert await _count(sessions, DocumentChecklistRow) == 11  # with the FIR or MLC (P2-1)


async def test_seed_gives_ops_and_doctor_their_roles(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)

    async with sessions() as session:
        rows = (await session.scalars(select(UserRoleRow))).all()
    roles = {(r.user_id, r.role): r.max_amount for r in rows}

    assert {role for uid, role in roles if uid == OPS_USER_ID} == {
        "claims_operator",
        "bill_processor",
        "investigator",
    }
    assert {role for uid, role in roles if uid == DOCTOR_USER_ID} == {
        "medical_officer",
        "approver",
        "crc_reviewer",
    }
    assert roles[(DOCTOR_USER_ID, "approver")] == 100000
    # The senior approver holds the same role with no limit.
    assert {role for uid, role in roles if uid == SENIOR_USER_ID} == {"approver"}
    assert roles[(SENIOR_USER_ID, "approver")] is None


async def test_seeded_hospitals_have_13_digit_rohini_ids(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)

    async with sessions() as session:
        ids = (await session.scalars(select(HospitalRow.rohini_id))).all()

    assert all(len(i) == 13 and i.isdigit() for i in ids)


async def test_seed_has_a_clause_for_every_clause_the_product_config_cites(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    await seed_claims_db(sessions)
    product = DefaultRulesEngine(Settings().config_dir / "products").product(PRODUCT_UIN)
    clauses = product.clauses

    async with sessions() as session:
        rows = (await session.scalars(select(PolicyClauseRow))).all()
    seeded = {r.clause_ref for r in rows if r.product_uin == PRODUCT_UIN}
    cited = {v for k, v in clauses.model_dump().items() if k != "non_payable"}

    assert cited | set(clauses.non_payable.values()) <= seeded
    assert {e.code for e in product.exclusions} <= seeded  # R9 cites the exclusion itself
    assert {"Excl01", "Excl02", "Excl03"} <= seeded
    assert len(rows) == len(seeded)  # idempotent: no duplicates after two seeds


async def test_seed_has_non_payable_items_from_all_four_lists_marked_fixture(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)
    await seed_claims_db(sessions)

    async with sessions() as session:
        rows = (await session.scalars(select(NonPayableItemRow))).all()

    assert 25 <= len(rows) <= 40
    assert {r.list_name for r in rows} == {"I", "II", "III", "IV"}
    assert {r.source for r in rows} == {"fixture"}
    assert all(r.condition for r in rows)
    assert len({(r.list_name, r.item_number) for r in rows}) == len(rows)


async def test_seed_leaves_a_loaded_non_payable_list_alone_and_adds_no_fixture_beside_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        session.add(
            NonPayableItemRow(
                list_name="I",
                item_number=1,
                item="BABY FOOD",
                condition="Not covered by the policy",
                payable_in_product="not_payable",
                source="non_payable",
                source_version="GODHLIP24179V022324",
            )
        )

    await seed_claims_db(sessions)

    async with sessions() as session:
        rows = (await session.scalars(select(NonPayableItemRow))).all()
    assert [(r.item, r.source) for r in rows] == [("BABY FOOD", "non_payable")]


async def test_one_seeded_hospital_has_no_differential_billing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_claims_db(sessions)

    async with sessions() as session:
        flags = (await session.scalars(select(HospitalRow.differential_billing))).all()

    assert sorted(flags) == [False, True, True, True, True]
