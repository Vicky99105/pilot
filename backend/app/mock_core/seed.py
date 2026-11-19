"""Create the mock_core tables and seed 10 synthetic members on 6 policies.

Policy dates are relative to the day the seed runs, so the claims they describe stay
current: policy 6 always started 18 days ago, inside the 30-day initial wait, and policy 4
always lapsed beyond its grace period.

Idempotent: tables are created if missing and rows are merged by primary key. A re-seed on
a later day moves the dates forward and changes nothing else. Balances (utilised, blocked)
are only set when a policy is first inserted, so a re-seed never resets blocks made since.
All data is synthetic.
Run inside the app container: python -m app.mock_core.seed
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.mock_core.models import (
    McInsuredMember,
    McPolicy,
    McPremiumPayment,
    McSubmitGrant,
    MockCoreBase,
)
from app.shared.seed_ids import (
    HOSPITAL_ROHINI_IDS,
    HOSPITAL_USER_ID,
    MEMBER_USER_ID,
    PRODUCT_UIN,
)

DIABETES = {
    "system": "ICD-10-CM",
    "version": "2026",
    "code": "E11.9",
    "display": "Type 2 diabetes mellitus without complications",
}
HYPERTENSION = {
    "system": "ICD-10-CM",
    "version": "2026",
    "code": "I10",
    "display": "Essential (primary) hypertension",
}

# (n, sum insured, cumulative bonus, inception days ago, policy start days ago, premium,
#  peds, members). Member tuple: (name, dob, gender, relationship).
# Policy 4 started 405 days ago: it ended 41 days ago, past the 30-day grace period.
# Policy 6 started 18 days ago: inside the 30-day initial wait.
_POLICIES = [
    (
        1,
        "500000",
        "50000",
        1328,
        232,
        "18450",
        [DIABETES],
        [
            ("Asha Verma", date(1984, 6, 12), "F", "self"),
            ("Rohit Verma", date(1981, 2, 3), "M", "spouse"),
        ],
    ),
    (
        2,
        "300000",
        "0",
        673,
        308,
        "9870",
        [],
        [
            ("Imran Qureshi", date(1990, 9, 30), "M", "self"),
            ("Sana Qureshi", date(1993, 12, 5), "F", "spouse"),
        ],
    ),
    (
        3,
        "400000",
        "20000",
        871,
        141,
        "14200",
        [HYPERTENSION],
        [
            ("Lakshmi Iyer", date(1962, 3, 18), "F", "self"),
            ("Venkat Iyer", date(1958, 11, 22), "M", "spouse"),
        ],
    ),
    (
        4,
        "300000",
        "15000",
        1501,
        405,
        "11300",
        [],
        [("Pooja Nair", date(1995, 5, 9), "F", "self")],
    ),
    (
        5,
        "500000",
        "0",
        536,
        171,
        "16700",
        [DIABETES, HYPERTENSION],
        [
            ("Gurpreet Singh", date(1975, 8, 14), "M", "self"),
            ("Harleen Kaur", date(1978, 1, 27), "F", "spouse"),
        ],
    ),
    (
        6,
        "350000",
        "0",
        18,
        18,
        "10950",
        [],
        [("Arjun Mehta", date(1999, 4, 2), "M", "self")],
    ),
]


def _policy_number(n: int) -> str:
    return f"MOCK/AS/2026/{100 + n:06d}"


def _member_id(n: int, m: int) -> str:
    return f"MOCK-M-{100 + n:06d}-{m:02d}"


def _year_end(start: date) -> date:
    """The last day of a one-year policy that starts on start."""
    try:
        anniversary = start.replace(year=start.year + 1)
    except ValueError:  # 29 February
        anniversary = date(start.year + 1, 3, 1)
    return anniversary - timedelta(days=1)


async def seed_mock_core(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    *,
    today: date | None = None,
) -> int:
    today = today or date.today()
    async with engine.begin() as conn:
        await conn.run_sync(MockCoreBase.metadata.create_all)
    members = 0
    async with sessions.begin() as session:
        for n, si, bonus, inception_ago, start_ago, premium, peds, people in _POLICIES:
            number = _policy_number(n)
            inception = today - timedelta(days=inception_ago)
            start = today - timedelta(days=start_ago)
            existing = await session.get(McPolicy, number)
            policy = McPolicy(
                policy_number=number,
                product_uin=PRODUCT_UIN,
                sum_insured=Decimal(si),
                cumulative_bonus=Decimal(bonus),
                utilised=existing.utilised if existing else Decimal("0"),
                blocked=existing.blocked if existing else Decimal("0"),
                inception_date=inception,
                continuous_cover_start=inception,
                policy_start=start,
                policy_end=_year_end(start),
                declared_peds=peds,
            )
            await session.merge(policy)
            await session.flush()
            premium_id = f"{number}/premium/current"
            # Earlier seeds keyed the payment by its date; keep one payment per policy.
            await session.execute(
                delete(McPremiumPayment).where(
                    McPremiumPayment.policy_number == number, McPremiumPayment.id != premium_id
                )
            )
            await session.merge(
                McPremiumPayment(
                    id=premium_id,
                    policy_number=number,
                    paid_on=start - timedelta(days=3),  # received before cover starts
                    amount=Decimal(premium),
                    status="received",
                )
            )
            for m, (name, dob, gender, relationship) in enumerate(people, start=1):
                await session.merge(
                    McInsuredMember(
                        member_id=_member_id(n, m),
                        policy_number=number,
                        name=name,
                        dob=dob,
                        gender=gender,
                        relationship=relationship,
                    )
                )
                members += 1
        await session.merge(
            McSubmitGrant(id="member-1", user_id=MEMBER_USER_ID, policy_number=_policy_number(1))
        )
        await session.merge(
            McSubmitGrant(
                id="hospital-1", user_id=HOSPITAL_USER_ID, hospital_id=HOSPITAL_ROHINI_IDS[0]
            )
        )
    return members


async def main() -> None:
    engine = create_async_engine(Settings().mock_core_database_url)
    try:
        members = await seed_mock_core(engine, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
    print(f"Seeded mock_core: {len(_POLICIES)} policies, {members} members")


if __name__ == "__main__":
    asyncio.run(main())
