import json
import logging

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, stream_with_context
from flask_login import current_user, login_required
from sqlalchemy import text

from .. import services
from ..auth import is_admin
from ..catalog import DIFFICULTIES, SPECS
from ..config import llm_problem
from ..extensions import csrf, db
from ..llm import LLMError
from ..models import Scenario

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__)


def _err(message, status):
    return jsonify({"error": message}), status


def _own(scenario_id):
    s = db.session.get(Scenario, scenario_id)
    if s is None or (s.user_id != current_user.id and not is_admin(current_user)):
        abort(404)
    return s


def scenario_public(s):
    """Everything the TSE may see. The hidden truth is included only once the ticket is closed."""
    services.expire_stale_generation(s)
    out = {
        "id": s.id, "ref": s.ref, "spec": s.spec, "spec_label": SPECS.get(s.spec, {}).get("label", s.spec),
        "difficulty": s.difficulty, "status": s.status, "error": s.error,
        "created_at": s.created_at.isoformat() + "Z",
    }
    if s.status in ("open", "closed"):
        sb = s.sandbox or {}
        tpl = current_app.extensions["tg"]["sandboxes"].templates.get(sb.get("template_id", ""))
        out.update({
            "ticket": s.ticket,
            "sandbox": {"needed": bool(sb.get("needed") and tpl), "template_title": tpl.title if tpl else None,
                        "repro_steps": sb.get("repro_steps", [])},
            "messages": [services.message_public(m) for m in s.messages if m.role != "system"],
            "facts_total": len((s.truth or {}).get("facts", [])),
            "provenance": _provenance(s.truth or {}),
            "facts_revealed": len(s.revealed_fact_ids or []),
        })
    if s.status == "closed":
        out["truth"] = s.truth
        out["difficulty_rationale"] = s.difficulty_rationale
        out["revealed_fact_ids"] = s.revealed_fact_ids
        out["grade"] = {"overall": s.grade.overall, **s.grade.result} if s.grade else None
        out["ungrounded"] = services.ungrounded_report(s)
    return out


def _provenance(truth):
    """What the TSE may know about the source while the ticket is open (nothing that hints
    at the answer)."""
    src, g = truth.get("source") or {}, truth.get("grounding") or {}
    prov = src.get("provenance") or {}
    return {"real_ticket": src.get("kind") == "distilled", "csat": prov.get("csat", ""),
            "complexity": prov.get("complexity", ""), "solved_month": prov.get("solved_month", ""),
            "verified": bool(g.get("checked")) and not g.get("unsupported")}


@bp.get("/healthz")
@csrf.exempt
def healthz():
    tg = current_app.extensions["tg"]
    try:
        db.session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    body = {
        "ok": db_ok, "db": db_ok,
        "llm": {"provider": current_app.config["LLM_PROVIDER"], "problem": llm_problem(current_app.config)},
        "corpus": tg["corpus"].counts(), "templates": sorted(tg["sandboxes"].templates),
        "warnings": tg["corpus"].errors + tg["sandboxes"].errors,
    }
    return jsonify(body), 200 if db_ok else 503


@bp.get("/api/specs")
@login_required
def specs():
    counts = current_app.extensions["tg"]["corpus"].counts()
    return jsonify({"specs": [{"id": k, **v, "seeds": counts.get(k, 0)} for k, v in SPECS.items()],
                    "difficulties": [{"id": k, **v} for k, v in DIFFICULTIES.items()]})


@bp.post("/api/scenarios")
@login_required
def create():
    body = request.get_json(silent=True) or {}
    spec, difficulty = body.get("spec"), body.get("difficulty")
    if spec not in SPECS or difficulty not in DIFFICULTIES:
        return _err("Pick a spec and a difficulty.", 400)
    problem = llm_problem(current_app.config)
    if problem:
        return _err(problem, 503)
    if not current_app.extensions["tg"]["corpus"].seeds(spec):
        return _err(f"No corpus seeds for {SPECS[spec]['label']} yet.", 409)
    try:
        s = services.create_scenario(current_user, spec, difficulty)
    except services.QuotaExceeded as exc:
        return _err(str(exc), 429)
    return jsonify({"id": s.id, "url": f"/t/{s.id}"}), 202


@bp.get("/api/scenarios/<scenario_id>")
@login_required
def get_scenario(scenario_id):
    return jsonify(scenario_public(_own(scenario_id)))


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@bp.post("/api/scenarios/<scenario_id>/messages")
@login_required
def post_message(scenario_id):
    s = _own(scenario_id)
    if s.user_id != current_user.id:
        return _err("Only the ticket owner can reply.", 403)
    if s.status != "open":
        return _err("This ticket is not open.", 409)
    body = request.get_json(silent=True) or {}
    content = (body.get("content") or "").strip()
    kind = body.get("kind", "reply")
    if kind not in ("reply", "resolution"):
        return _err("Unknown message kind.", 400)
    if not content:
        return _err("Write a reply first.", 400)
    if len(content) > current_app.config["MAX_MESSAGE_CHARS"]:
        return _err(f"Replies are limited to {current_app.config['MAX_MESSAGE_CHARS']} characters.", 400)
    try:
        tse_msg = services.add_tse_message(s, current_user, content, kind)
    except services.QuotaExceeded as exc:
        return _err(str(exc), 429)
    tse_public = services.message_public(tse_msg)  # serialize now; the ORM object may expire mid-stream
    sid = s.id

    def events():
        yield _sse("tse", tse_public)
        try:
            for event, data in services.stream_customer_turn(db.session.get(Scenario, sid)):
                yield _sse(event, {"text": data} if event == "delta" else data)
        except LLMError as exc:
            yield _sse("error", {"message": f"{exc} Your reply was saved, so send a short follow-up to retry."})
        except Exception:
            log.exception("customer turn failed for %s", s.id)
            yield _sse("error", {"message": "The customer didn't respond (server error). Your reply was saved; try again."})

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.post("/api/scenarios/<scenario_id>/close")
@login_required
def close(scenario_id):
    s = _own(scenario_id)
    if s.user_id != current_user.id:
        return _err("Only the ticket owner can close it.", 403)
    if s.status == "closed":
        return jsonify(scenario_public(s))
    if s.status != "open":
        return _err("This ticket is not open.", 409)
    try:
        services.close_and_grade(s)
    except LLMError as exc:
        return _err(str(exc), exc.status)
    return jsonify(scenario_public(s))


def _readme(s, template=None):
    return render_template("sandbox_readme.md", s=s, t=s.ticket, sb=s.sandbox or {}, template=template,
                           spec_label=SPECS.get(s.spec, {}).get("label", s.spec))


@bp.get("/api/scenarios/<scenario_id>/sandbox.zip")
@login_required
def sandbox_zip(scenario_id):
    s = _own(scenario_id)
    sb = s.sandbox or {}
    lib = current_app.extensions["tg"]["sandboxes"]
    tpl = lib.templates.get(sb.get("template_id", ""))
    if s.status not in ("open", "closed") or not sb.get("needed") or tpl is None:
        abort(404)
    meta = {"short_id": s.ref.lower(), "ref": s.ref, "service_prefix": s.ref.lower()}
    data, filename = lib.build_zip(tpl.id, sb.get("params", []), meta, _readme(s, tpl))
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.get("/api/scenarios/<scenario_id>/repro.md")
@login_required
def repro_md(scenario_id):
    s = _own(scenario_id)
    if s.status not in ("open", "closed"):
        abort(404)
    return Response(_readme(s), mimetype="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{s.ref}-repro.md"'})
