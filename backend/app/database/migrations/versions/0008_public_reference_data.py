"""public reference data

Slice P2-3: the tables the public reference loaders fill. Non-payable items gain the
product's payable flag and the source page, and their unique key gains the source, so a
loaded wording can sit beside the fixture. Policy clauses gain the source page. New tables:
exclusions, package_masters, package_items, rate_factors, los_norms, drug_prices,
nlem_medicines and guideline_sections. Name lookups get trigram indexes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-20 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
MONEY = sa.Numeric(14, 2)


def _source_columns() -> list[sa.Column[str]]:
    return [
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("source_version", sa.String(length=40), nullable=False),
    ]


def _trigram(table: str, column: str) -> None:
    op.create_index(
        f"ix_{table}_{column}_trgm",
        table,
        [column],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={column: "gin_trgm_ops"},
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "non_payable_items",
        sa.Column(
            "payable_in_product",
            sa.String(length=30),
            nullable=False,
            server_default="not_payable",
        ),
    )
    op.alter_column("non_payable_items", "payable_in_product", server_default=None)
    op.add_column("non_payable_items", sa.Column("page", sa.Integer(), nullable=True))
    op.drop_constraint(op.f("uq_non_payable_items_list_name"), "non_payable_items", type_="unique")
    op.create_unique_constraint(
        "uq_non_payable_items_source_list",
        "non_payable_items",
        ["source", "source_version", "list_name", "item_number"],
    )
    _trigram("non_payable_items", "item")
    op.add_column("policy_clauses", sa.Column("page", sa.Integer(), nullable=True))

    op.create_table(
        "exclusions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=60), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "code", "source_version"),
    )
    op.create_table(
        "package_masters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("package_code", sa.String(length=20), nullable=True),
        sa.Column("procedure_code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("package_name", sa.Text(), nullable=True),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("specialty", sa.Text(), nullable=True),
        sa.Column("specialty_code", sa.String(length=10), nullable=True),
        sa.Column("rate", MONEY, nullable=True),
        sa.Column("rate_basis", sa.String(length=120), nullable=True),
        sa.Column("alos_days", sa.Integer(), nullable=True),
        sa.Column("los_text", sa.Text(), nullable=True),
        sa.Column("implant", sa.Text(), nullable=True),
        sa.Column("implant_cost", sa.Text(), nullable=True),
        sa.Column("stratification", sa.Boolean(), nullable=True),
        sa.Column("multiple_procedures", sa.Boolean(), nullable=True),
        sa.Column("addon", sa.Boolean(), nullable=True),
        sa.Column("standalone", sa.Boolean(), nullable=True),
        sa.Column("public_only", sa.Boolean(), nullable=True),
        sa.Column("medical_or_surgical", sa.String(length=20), nullable=True),
        sa.Column("extra", JSONB, nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_package_masters_code", "package_masters", ["scheme", "version", "procedure_code"]
    )
    _trigram("package_masters", "search_text")
    op.create_table(
        "package_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=10), nullable=False),
        sa.Column("accreditation", sa.String(length=30), nullable=False),
        sa.Column("ward", sa.String(length=20), nullable=False),
        sa.Column("rate", MONEY, nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["package_id"], ["package_masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_package_items_package", "package_items", ["package_id"])
    op.create_table(
        "rate_factors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("factor", sa.String(length=40), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Numeric(10, 4), nullable=True),
        sa.Column("text_value", sa.String(length=60), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rate_factors_kind", "rate_factors", ["scheme", "version", "factor", "key"])
    op.create_table(
        "los_norms",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scheme", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("package_code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("specialty", sa.Text(), nullable=True),
        sa.Column("los_days", sa.Integer(), nullable=True),
        sa.Column("los_text", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_los_norms_code", "los_norms", ["package_code"])
    _trigram("los_norms", "name")
    op.create_table(
        "drug_prices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("serial", sa.Integer(), nullable=False),
        sa.Column("medicine", sa.Text(), nullable=False),
        sa.Column("dosage_form", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=60), nullable=False),
        sa.Column("ceiling_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("notification", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    _trigram("drug_prices", "medicine")
    op.create_table(
        "nlem_medicines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("medicine", sa.Text(), nullable=False),
        sa.Column("levels", sa.String(length=20), nullable=True),
        sa.Column("formulations", JSONB, nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    _trigram("nlem_medicines", "medicine")
    op.create_table(
        "guideline_sections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("package_code", sa.String(length=20), nullable=False),
        sa.Column("old_code", sa.String(length=20), nullable=True),
        sa.Column("procedure_name", sa.Text(), nullable=False),
        sa.Column("section", sa.String(length=40), nullable=False),
        sa.Column("items", JSONB, nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_guideline_sections_code", "guideline_sections", ["package_code"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("guideline_sections")
    op.drop_table("nlem_medicines")
    op.drop_table("drug_prices")
    op.drop_table("los_norms")
    op.drop_table("rate_factors")
    op.drop_table("package_items")
    op.drop_table("package_masters")
    op.drop_table("exclusions")
    op.drop_column("policy_clauses", "page")
    op.drop_index("ix_non_payable_items_item_trgm", table_name="non_payable_items")
    op.drop_constraint("uq_non_payable_items_source_list", "non_payable_items", type_="unique")
    # Keep one row per (list, number) so the old key can come back: the loaded rows go.
    op.execute("DELETE FROM non_payable_items WHERE source <> 'fixture'")
    op.create_unique_constraint(
        op.f("uq_non_payable_items_list_name"), "non_payable_items", ["list_name", "item_number"]
    )
    op.drop_column("non_payable_items", "page")
    op.drop_column("non_payable_items", "payable_in_product")
