import uuid
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import JSON

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id():
    return uuid.uuid4().hex


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)

    scenarios = db.relationship("Scenario", back_populates="user", lazy="dynamic")


class Scenario(db.Model):
    """One generated practice ticket. `ticket` is what the TSE sees, `truth` is hidden
    until the ticket is closed and graded."""

    __tablename__ = "scenarios"
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    number = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    spec = db.Column(db.String(40), nullable=False)
    difficulty = db.Column(db.String(10), nullable=False)
    # generating -> open -> closed  (or generating -> failed)
    status = db.Column(db.String(20), nullable=False, default="generating")
    error = db.Column(db.Text, nullable=True)
    seed_ids = db.Column(JSON, nullable=False, default=list)
    ticket = db.Column(JSON, nullable=True)
    truth = db.Column(JSON, nullable=True)
    sandbox = db.Column(JSON, nullable=True)
    difficulty_rationale = db.Column(db.Text, nullable=True)
    revealed_fact_ids = db.Column(JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", back_populates="scenarios")
    messages = db.relationship("Message", back_populates="scenario", order_by="Message.id",
                               cascade="all, delete-orphan")
    grade = db.relationship("Grade", back_populates="scenario", uselist=False,
                            cascade="all, delete-orphan")

    @property
    def ref(self):
        return f"TG-{self.number:05d}"


class Message(db.Model):
    """A turn in the ticket thread.

    role: tse | customer | system. `system` rows are operator directives sent to the
    customer model mid-conversation; they're never shown to the TSE but are replayed so
    the conversation history stays append-only.
    kind: reply | resolution (TSE proposed a fix) | directive
    """

    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    scenario_id = db.Column(db.String(32), db.ForeignKey("scenarios.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)
    kind = db.Column(db.String(20), nullable=False, default="reply")
    content = db.Column(db.Text, nullable=False, default="")
    # Full assistant content blocks (thinking + text) for replay to the API unchanged.
    api_blocks = db.Column(JSON, nullable=True)
    revealed = db.Column(JSON, nullable=False, default=list)
    # Technical details in a customer reply that the source ticket doesn't support.
    ungrounded = db.Column(JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    scenario = db.relationship("Scenario", back_populates="messages")


class Grade(db.Model):
    __tablename__ = "grades"
    scenario_id = db.Column(db.String(32), db.ForeignKey("scenarios.id"), primary_key=True)
    overall = db.Column(db.Integer, nullable=False)  # 0-100
    result = db.Column(JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    scenario = db.relationship("Scenario", back_populates="grade")


class LLMCall(db.Model):
    """Token accounting per Claude call: cost visibility and the admin usage view."""

    __tablename__ = "llm_calls"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    scenario_id = db.Column(db.String(32), nullable=True, index=True)
    purpose = db.Column(db.String(40), nullable=False)
    model = db.Column(db.String(80), nullable=False)
    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    cache_read_tokens = db.Column(db.Integer, nullable=False, default=0)
    cache_write_tokens = db.Column(db.Integer, nullable=False, default=0)
    duration_ms = db.Column(db.Integer, nullable=False, default=0)
    cost_usd = db.Column(db.Float, nullable=False, default=0.0)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
