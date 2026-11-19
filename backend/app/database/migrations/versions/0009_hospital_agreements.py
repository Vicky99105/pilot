"""hospital agreements and package crosswalk

Slice P2-4: hospital_agreements says which scheme a hospital's packages are priced from and
its discount on non-package charges; package_crosswalk is our own map from a package to the
diagnoses it treats and to the PM-JAY HBP code whose guideline applies.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-21 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "hospital_agreements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("hospital_id", sa.String(length=13), nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("accreditation", sa.String(length=30), nullable=True),
        sa.Column("package_ward", sa.String(length=20), nullable=True),
        sa.Column("non_package_discount_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("source_version", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.rohini_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hospital_id", "valid_from"),
    )
    op.create_table(
        "package_crosswalk",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("package_code", sa.String(length=20), nullable=False),
        sa.Column("icd10cm", JSONB, nullable=False),
        sa.Column("guideline_code", sa.String(length=20), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("source_version", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scheme", "package_code", "source"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("package_crosswalk")
    op.drop_table("hospital_agreements")
