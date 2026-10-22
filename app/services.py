"""Persistence and orchestration around engine.py: scenario lifecycle, background
generation, the streamed customer turn, grading, quotas and token accounting."""
import logging
import threading
from datetime import timedelta

from flask import current_app
from sqlalchemy import func

from . import engine
from .extensions import db
from .llm import LLMError, to_json
from .models import Grade, LLMCall, Message, Scenario, utcnow

log = logging.getLogger(__name__)

GENERATION_STALE_AFTER = timedelta(minutes=10)


class QuotaExceeded(Exception):
    pass


def ext(name):
    return current_app.extensions["tg"][name]


def record_usage(purpose, usage, user_id=None, scenario_id=None, ok=True):
    if usage is None:
        return
    db.session.add(LLMCall(
        user_id=user_id, scenario_id=scenario_id, purpose=purpose, model=usage.model,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens, cache_write_tokens=usage.cache_write_tokens,
        duration_ms=usage.duration_ms, cost_usd=usage.cost_usd or 0.0, ok=ok,
    ))


# ---------------------------------------------------------------------------- quotas

def check_generation_quota(user):
    limit = current_app.config["GENERATIONS_PER_HOUR"]
    since = utcnow() - timedelta(hours=1)
    n = Scenario.query.filter(Scenario.user_id == user.id, Scenario.created_at >= since).count()
    if n >= limit:
        raise QuotaExceeded(f"You've generated {n} tickets in the last hour (limit {limit}). Try again later.")


def check_message_quota(user):
    limit = current_app.config["MESSAGES_PER_HOUR"]
    since = utcnow() - timedelta(hours=1)
    n = (Message.query.join(Scenario)
         .filter(Scenario.user_id == user.id, Message.role == "tse", Message.created_at >= since).count())
    if n >= limit:
        raise QuotaExceeded(f"You've sent {n} replies in the last hour (limit {limit}). Take a breather.")


# ------------------------------------------------------------------------ generation

def _next_number():
    return (db.session.query(func.max(Scenario.number)).scalar() or 0) + 1


def create_scenario(user, spec, difficulty):
    check_generation_quota(user)
    s = Scenario(user_id=user.id, spec=spec, difficulty=difficulty, status="generating", number=_next_number())
    db.session.add(s)
    db.session.commit()
    app = current_app._get_current_object()
    if app.config["RUN_GENERATION_INLINE"]:
        run_generation(app, s.id)
    else:
        threading.Thread(target=run_generation, args=(app, s.id), daemon=True,
                         name=f"generate-{s.id[:8]}").start()
    return s


def run_generation(app, scenario_id):
    with app.app_context():
        s = db.session.get(Scenario, scenario_id)
        if s is None:
            return
        tg = app.extensions["tg"]
        avoid = [sid for row in Scenario.query.filter(Scenario.user_id == s.user_id, Scenario.id != s.id)
                 .order_by(Scenario.created_at.desc()).limit(15) for sid in (row.seed_ids or [])[:1]]
        try:
            scenario, seed_ids, warnings, usages, grounding = engine.generate_scenario(
                tg["llm"], app.config, tg["corpus"], tg["sandboxes"], s.spec, s.difficulty, avoid)
        except (LLMError, LookupError) as exc:
            for purpose, u in getattr(exc, "usages", []):
                record_usage(purpose, u, s.user_id, s.id, ok=False)
            s.status, s.error = "failed", str(exc)
            db.session.commit()
            return
        except Exception:
            log.exception("generation failed for %s", scenario_id)
            s.status, s.error = "failed", "Unexpected error while generating. Check the server logs."
            db.session.commit()
            return
        for w in warnings:
            log.info("generation %s: %s", scenario_id, w)
        data = to_json(scenario)
        seed = tg["corpus"].get(seed_ids[0])
        # Snapshot the real-ticket material so the customer, grader and answer key keep
        # using it even if the corpus changes later.
        data["truth"]["source"] = engine.source_snapshot(seed)
        data["truth"]["grounding"] = grounding
        s.ticket, s.truth, s.sandbox = data["ticket"], data["truth"], data["sandbox"]
        s.difficulty_rationale = data["difficulty_rationale"]
        s.seed_ids = seed_ids
        s.status, s.opened_at = "open", utcnow()
        for purpose, u in usages:
            record_usage(purpose, u, s.user_id, s.id)
        db.session.commit()


def expire_stale_generation(s):
    if s.status == "generating" and utcnow() - s.created_at > GENERATION_STALE_AFTER:
        s.status, s.error = "failed", "Generation timed out (the server may have restarted). Please generate a new ticket."
        db.session.commit()


# ----------------------------------------------------------------------------- chat

def history_dicts(s):
    return [{"role": m.role, "kind": m.kind, "content": m.content, "api_blocks": m.api_blocks}
            for m in s.messages]


def scenario_dict(s):
    return {"ticket": s.ticket, "truth": s.truth, "difficulty": s.difficulty, "spec": s.spec,
            "seed_ids": s.seed_ids, "revealed_fact_ids": s.revealed_fact_ids,
            "ungrounded": ungrounded_report(s)}


def add_tse_message(s, user, content, kind):
    check_message_quota(user)
    msg = Message(scenario_id=s.id, role="tse", kind=kind, content=content)
    db.session.add(msg)
    if kind == "resolution":
        db.session.add(Message(scenario_id=s.id, role="system", kind="directive",
                               content=engine.RESOLUTION_DIRECTIVE))
    db.session.commit()
    return msg


def stream_customer_turn(s):
    """Generator of ("delta", text) events, then ("done", message_dict). Persists the
    reply and its disclosures. Raises LLMError on failure (the TSE message stays saved,
    and the next send replays it)."""
    tg = current_app.extensions["tg"]
    cfg = current_app.config
    system = engine.customer_system_prompt(scenario_dict(s), tg["corpus"])
    gen = engine.customer_reply_stream(tg["llm"], cfg, system, history_dicts(s))
    result = None
    while True:
        try:
            chunk = next(gen)
        except StopIteration as stop:
            result = stop.value
            break
        yield "delta", chunk
    record_usage("customer", result.usage, s.user_id, s.id)

    revealed, ungrounded = [], None
    try:
        revealed, ungrounded, cusage = engine.classify_disclosure(
            tg["llm"], cfg, s.truth, result.text, (s.ticket or {}).get("body", ""))
        record_usage("classify", cusage, s.user_id, s.id)
    except LLMError as exc:  # disclosure tracking is best-effort; never lose the reply
        log.warning("disclosure check failed for %s: %s", s.id, exc)

    msg = Message(scenario_id=s.id, role="customer", kind="reply", content=result.text,
                  api_blocks=result.blocks, revealed=revealed, ungrounded=ungrounded or None)
    db.session.add(msg)
    s.revealed_fact_ids = sorted(set(s.revealed_fact_ids or []) | set(revealed))
    db.session.commit()
    yield "done", message_public(msg)


def message_public(m):
    return {"id": m.id, "role": m.role, "kind": m.kind, "content": m.content,
            "created_at": m.created_at.isoformat() + "Z"}


def ungrounded_report(s):
    """Customer replies whose technical details the source ticket didn't support."""
    return [{"message_id": m.id, "details": m.ungrounded} for m in s.messages if m.ungrounded]


# --------------------------------------------------------------------------- grading

def close_and_grade(s):
    tg = current_app.extensions["tg"]
    grade, overall, usage = engine.grade_scenario(tg["llm"], current_app.config, scenario_dict(s), history_dicts(s))
    record_usage("grade", usage, s.user_id, s.id)
    db.session.add(Grade(scenario_id=s.id, overall=overall, result=to_json(grade)))
    s.status, s.closed_at = "closed", utcnow()
    db.session.commit()
    return s.grade
