from flask import Blueprint, abort, current_app, render_template
from flask_login import current_user, login_required
from sqlalchemy import func

from ..auth import is_admin
from ..catalog import SPECS
from ..extensions import db
from ..models import Grade, LLMCall, Scenario, User, utcnow

bp = Blueprint("ui", __name__)


def _user_stats(user_id):
    rows = (db.session.query(Scenario.spec, func.count(Grade.scenario_id), func.avg(Grade.overall))
            .join(Grade, Grade.scenario_id == Scenario.id)
            .filter(Scenario.user_id == user_id).group_by(Scenario.spec).all())
    return {spec: {"graded": n, "avg": round(avg or 0)} for spec, n, avg in rows}


@bp.get("/")
@login_required
def index():
    recent = (Scenario.query.filter_by(user_id=current_user.id)
              .order_by(Scenario.created_at.desc()).limit(8).all())
    counts = current_app.extensions["tg"]["corpus"].counts()
    templates = current_app.extensions["tg"]["sandboxes"]
    sandbox_counts = {spec: len(templates.for_spec(spec)) for spec in SPECS}
    return render_template("index.html", recent=recent, seed_counts=counts,
                           difficulty_counts=current_app.extensions["tg"]["corpus"].difficulty_counts(),
                           sandbox_counts=sandbox_counts, stats=_user_stats(current_user.id))


@bp.get("/t/<scenario_id>")
@login_required
def ticket(scenario_id):
    s = db.session.get(Scenario, scenario_id)
    if s is None or (s.user_id != current_user.id and not is_admin(current_user)):
        abort(404)
    return render_template("ticket.html", s=s, readonly=s.user_id != current_user.id)


@bp.get("/history")
@login_required
def history():
    rows = (Scenario.query.filter_by(user_id=current_user.id)
            .order_by(Scenario.created_at.desc()).limit(200).all())
    return render_template("history.html", rows=rows, stats=_user_stats(current_user.id))


@bp.get("/team")
@login_required
def team():
    if not is_admin(current_user):
        abort(404)
    people = (db.session.query(
        User,
        func.count(Scenario.id),
        func.count(Grade.scenario_id),
        func.avg(Grade.overall),
    ).outerjoin(Scenario, Scenario.user_id == User.id)
     .outerjoin(Grade, Grade.scenario_id == Scenario.id)
     .group_by(User.id).order_by(User.name).all())
    usage = (db.session.query(LLMCall.purpose, LLMCall.model, func.count(LLMCall.id),
                              func.sum(LLMCall.input_tokens), func.sum(LLMCall.output_tokens),
                              func.sum(LLMCall.cache_read_tokens), func.sum(LLMCall.cost_usd))
             .group_by(LLMCall.purpose, LLMCall.model).order_by(LLMCall.purpose).all())
    recent = Scenario.query.order_by(Scenario.created_at.desc()).limit(25).all()
    month_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_cost = db.session.query(func.sum(LLMCall.cost_usd)).filter(LLMCall.created_at >= month_start).scalar() or 0
    total_cost = sum((row[6] or 0) for row in usage)
    tickets = Scenario.query.filter(Scenario.status.in_(("open", "closed"))).count()
    return render_template("team.html", people=people, usage=usage, recent=recent, month_cost=month_cost,
                           total_cost=total_cost, per_ticket=(total_cost / tickets) if tickets else 0)


@bp.app_errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="That page doesn't exist, or it isn't yours."), 404
