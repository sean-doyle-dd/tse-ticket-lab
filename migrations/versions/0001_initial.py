"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "scenarios",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("spec", sa.String(40), nullable=False),
        sa.Column("difficulty", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("seed_ids", sa.JSON, nullable=False),
        sa.Column("ticket", sa.JSON, nullable=True),
        sa.Column("truth", sa.JSON, nullable=True),
        sa.Column("sandbox", sa.JSON, nullable=True),
        sa.Column("difficulty_rationale", sa.Text, nullable=True),
        sa.Column("revealed_fact_ids", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("opened_at", sa.DateTime, nullable=True),
        sa.Column("closed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_scenarios_number", "scenarios", ["number"])
    op.create_index("ix_scenarios_user_id", "scenarios", ["user_id"])
    op.create_index("ix_scenarios_created_at", "scenarios", ["created_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scenario_id", sa.String(32), sa.ForeignKey("scenarios.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("api_blocks", sa.JSON, nullable=True),
        sa.Column("revealed", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_messages_scenario_id", "messages", ["scenario_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    op.create_table(
        "grades",
        sa.Column("scenario_id", sa.String(32), sa.ForeignKey("scenarios.id"), primary_key=True),
        sa.Column("overall", sa.Integer, nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("scenario_id", sa.String(32), nullable=True),
        sa.Column("purpose", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False),
        sa.Column("cache_write_tokens", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_llm_calls_user_id", "llm_calls", ["user_id"])
    op.create_index("ix_llm_calls_scenario_id", "llm_calls", ["scenario_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])


def downgrade():
    for t in ("llm_calls", "grades", "messages", "scenarios", "users"):
        op.drop_table(t)
