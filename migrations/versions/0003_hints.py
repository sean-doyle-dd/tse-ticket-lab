"""hints per scenario

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("scenarios", sa.Column("hints", sa.JSON, nullable=True))


def downgrade():
    op.drop_column("scenarios", "hints")
