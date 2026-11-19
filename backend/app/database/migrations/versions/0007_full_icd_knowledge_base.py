"""full ICD knowledge base

Slice P2-2: the full ICD-10-CM index (disease, external cause, drug and neoplasm), the
ICD-10-CM tabular notes and hierarchy, the ICD-10-PCS tables and definitions, and a record of
every loaded source file with its hash and the version's effective dates.

The trigram index moves from icd_index.path to icd_index.search_text, the path plus its
nonessential modifiers, which is the column the search reads.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def _source_columns() -> list[sa.Column[str]]:
    return [
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("source_version", sa.String(length=40), nullable=False),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "icd_index",
        sa.Column("index_name", sa.String(length=20), nullable=False, server_default="diseases"),
    )
    op.add_column(
        "icd_index", sa.Column("search_text", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("icd_index", sa.Column("table_column", sa.Text(), nullable=True))
    op.add_column("icd_index", sa.Column("manifestation", sa.Text(), nullable=True))
    op.add_column("icd_index", sa.Column("see_also", sa.Text(), nullable=True))
    op.execute("UPDATE icd_index SET search_text = path")
    op.alter_column("icd_index", "index_name", server_default=None)
    op.alter_column("icd_index", "search_text", server_default=None)
    op.drop_index("ix_icd_index_path_trgm", table_name="icd_index")
    op.create_index(
        "ix_icd_index_search_trgm",
        "icd_index",
        ["search_text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )

    op.add_column("icd_tabular", sa.Column("parent", sa.String(length=10), nullable=True))
    op.add_column("icd_tabular", sa.Column("chapter", sa.Text(), nullable=True))
    op.add_column("icd_tabular", sa.Column("section", sa.Text(), nullable=True))
    op.create_index("ix_icd_tabular_parent", "icd_tabular", ["system", "version", "parent"])

    op.create_table(
        "kb_source_loads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system", sa.String(length=30), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("member", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_source_loads_system", "kb_source_loads", ["system", "version"])

    op.create_table(
        "pcs_tables",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system", sa.String(length=30), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("code", sa.String(length=3), nullable=False),
        sa.Column("axes", JSONB, nullable=False),
        sa.Column("operation_definition", sa.Text(), nullable=True),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("system", "version", "code"),
    )
    op.create_table(
        "pcs_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system", sa.String(length=30), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("table_code", sa.String(length=3), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("axes", JSONB, nullable=False),
        sa.Column("codes", sa.Integer(), nullable=False),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pcs_rows_table", "pcs_rows", ["system", "version", "table_code"])
    op.create_table(
        "pcs_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system", sa.String(length=30), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("section", sa.String(length=1), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False),
        sa.Column("axis", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("includes", JSONB, nullable=False),
        *_source_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pcs_definitions_axis", "pcs_definitions", ["system", "version", "section", "pos"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_pcs_definitions_axis", table_name="pcs_definitions")
    op.drop_table("pcs_definitions")
    op.drop_index("ix_pcs_rows_table", table_name="pcs_rows")
    op.drop_table("pcs_rows")
    op.drop_table("pcs_tables")
    op.drop_index("ix_kb_source_loads_system", table_name="kb_source_loads")
    op.drop_table("kb_source_loads")

    op.drop_index("ix_icd_tabular_parent", table_name="icd_tabular")
    op.drop_column("icd_tabular", "section")
    op.drop_column("icd_tabular", "chapter")
    op.drop_column("icd_tabular", "parent")

    op.drop_index("ix_icd_index_search_trgm", table_name="icd_index")
    op.create_index(
        "ix_icd_index_path_trgm",
        "icd_index",
        ["path"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"path": "gin_trgm_ops"},
    )
    op.drop_column("icd_index", "see_also")
    op.drop_column("icd_index", "manifestation")
    op.drop_column("icd_index", "table_column")
    op.drop_column("icd_index", "search_text")
    op.drop_column("icd_index", "index_name")
