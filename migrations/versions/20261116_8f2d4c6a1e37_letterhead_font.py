"""companies.letterhead_font: шрифт фирменной шапки

Revision ID: 8f2d4c6a1e37
Revises: 5c1e7a9d2b40
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8f2d4c6a1e37"
down_revision: str | None = "5c1e7a9d2b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("letterhead_font", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.drop_column("letterhead_font")
