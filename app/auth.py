"""Login. AUTH_MODE=none (default): no sign-in; everyone using this instance is one local
user. That fits the normal setup, where each TSE runs their own copy with their own Claude
token. AUTH_MODE=google: Google OIDC restricted to ALLOWED_EMAIL_DOMAIN (the `hd` claim is
checked server-side, not just requested), for a shared deployment."""
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db, login_manager
from .models import User, utcnow

bp = Blueprint("auth", __name__)
oauth = OAuth()

LOCAL_EMAIL = "local@localhost"


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
    else:
        app.before_request(_sign_in_local)
    app.register_blueprint(bp)


def _sign_in_local():
    if current_user.is_authenticated or request.endpoint in ("static", "api.healthz"):
        return
    user = User.query.filter_by(email=LOCAL_EMAIL).first()
    if user is None:
        user = User(email=LOCAL_EMAIL, name="You")
        db.session.add(user)
        db.session.commit()
    login_user(user, remember=True)


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


@bp.route("/login")
def login():
    if current_user.is_authenticated or current_app.config["AUTH_MODE"] != "google":
        return redirect(url_for("ui.index"))
    return render_template("login.html")


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
    if current_app.config["AUTH_MODE"] != "google":
        return redirect(url_for("ui.index"))
    logout_user()
    return redirect(url_for("auth.login"))
