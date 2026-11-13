"""companies: справочник юрлиц и компания документа

Revision ID: 5c1e7a9d2b40
Revises: de906fa4d279
Create Date: 2026-11-13 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "5c1e7a9d2b40"
down_revision: str | None = "de906fa4d279"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("short_name", sa.String(length=255), nullable=False),
        sa.Column("bin", sa.String(length=12), nullable=True),
        sa.Column("legal_address", sa.String(length=500), nullable=True),
        sa.Column("actual_address", sa.String(length=500), nullable=True),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("bank", sa.String(length=255), nullable=True),
        sa.Column("bik", sa.String(length=16), nullable=True),
        sa.Column("kbe", sa.String(length=4), nullable=True),
        sa.Column("director_position", sa.String(length=255), nullable=True),
        sa.Column("director_name", sa.String(length=255), nullable=True),
        sa.Column("addressee", sa.JSON(), nullable=False),
        sa.Column("letterhead_en", sa.JSON(), nullable=False),
        sa.Column("letterhead_ru", sa.JSON(), nullable=False),
        sa.Column("logo", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    with op.batch_alter_table("memos", schema=None) as batch_op:
        batch_op.add_column(sa.Column("company_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_memos_company", "companies", ["company_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("memos", schema=None) as batch_op:
        batch_op.drop_constraint("fk_memos_company", type_="foreignkey")
        batch_op.drop_column("company_id")
    op.drop_table("companies")
