"""per-call cost and ungrounded-detail flags

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_calls", sa.Column("cost_usd", sa.Float, nullable=False, server_default="0"))
    op.add_column("messages", sa.Column("ungrounded", sa.JSON, nullable=True))


def downgrade():
    op.drop_column("messages", "ungrounded")
    op.drop_column("llm_calls", "cost_usd")
