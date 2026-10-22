"""Login. AUTH_MODE=google: Google OIDC restricted to ALLOWED_EMAIL_DOMAIN (the `hd` claim
is checked server-side, not just requested). AUTH_MODE=dev: a name + email form for local
use only, with a banner on every page."""
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db, login_manager
from .models import User, utcnow

bp = Blueprint("auth", __name__)
oauth = OAuth()


def init_auth(app):
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None
    if app.config["AUTH_MODE"] == "google":
        oauth.init_app(app)
        oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    app.register_blueprint(bp)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def is_admin(user):
    return bool(user and user.is_authenticated and user.email.lower() in current_app.config["ADMIN_EMAILS"])


def _domain_ok(email):
    domain = current_app.config["ALLOWED_EMAIL_DOMAIN"]
    return not domain or email.lower().endswith("@" + domain)


def _sign_in(email, name):
    email = email.strip().lower()
    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(email=email, name=name.strip() or email.split("@")[0])
        db.session.add(user)
    elif name.strip():
        user.name = name.strip()
    user.last_login_at = utcnow()
    db.session.commit()
    login_user(user, remember=True)
    return user


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("ui.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("ui.index"))
    mode = current_app.config["AUTH_MODE"]
    if request.method == "POST":
        if mode != "dev":
            abort(404)
        email = request.form.get("email", "")
        name = request.form.get("name", "")
        if "@" not in email or not _domain_ok(email):
            flash(f"Use your @{current_app.config['ALLOWED_EMAIL_DOMAIN']} address.", "error")
            return render_template("login.html", mode=mode), 400
        _sign_in(email, name)
        return redirect(_safe_next(request.args.get("next")))
    return render_template("login.html", mode=mode)


@bp.route("/login/google")
def login_google():
    if current_app.config["AUTH_MODE"] != "google":
        abort(404)
    session["post_login"] = _safe_next(request.args.get("next"))
    redirect_uri = url_for("auth.google_callback", _external=True)
    kwargs = {}
    if current_app.config["ALLOWED_EMAIL_DOMAIN"]:
        kwargs["hd"] = current_app.config["ALLOWED_EMAIL_DOMAIN"]
    return oauth.google.authorize_redirect(redirect_uri, **kwargs)


@bp.route("/auth/callback")
def google_callback():
    if current_app.config["AUTH_MODE"] != "google":
        abort(404)
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or {}
    email = info.get("email", "")
    allowed = current_app.config["ALLOWED_EMAIL_DOMAIN"]
    if not info.get("email_verified") or not _domain_ok(email) or (allowed and info.get("hd") != allowed):
        flash(f"Sign in with a verified @{allowed} Google account.", "error")
        return redirect(url_for("auth.login"))
    _sign_in(email, info.get("name", ""))
    return redirect(session.pop("post_login", None) or url_for("ui.index"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
