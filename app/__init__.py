import logging
import os
import secrets

from flask import Flask

from . import config as config_mod
from .auth import init_auth, is_admin
from .catalog import DIFFICULTIES, SPECS
from .corpus import Corpus
from .extensions import csrf, db
from .llm import LLM
from .sandbox import SandboxLibrary


def create_app(overrides=None, llm=None):
    app = Flask(__name__)
    cfg = config_mod.load()
    if overrides:
        cfg.update(overrides)
    if not cfg["SECRET_KEY"]:
        # Single-process fallback (tests, flask run). Under gunicorn the entrypoint exports a
        # shared key first, because per-worker random keys would reject each other's cookies.
        # problems() refuses a missing key in google mode.
        cfg["SECRET_KEY"] = secrets.token_hex(32)
    app.config.from_mapping(cfg)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    problems = config_mod.problems(app.config)
    if problems:
        raise RuntimeError("Configuration problems:\n  - " + "\n  - ".join(problems))

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///"):
        os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"][len("sqlite:///"):]) or ".", exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    init_auth(app)

    app.extensions["tg"] = {
        "llm": llm or LLM(app.config),
        "corpus": Corpus(app.config["CORPUS_DIR"], app.config["CORPUS_ALLOW_BOOTSTRAP"]),
        "sandboxes": SandboxLibrary(app.config["SANDBOX_TEMPLATES_DIR"]),
    }

    from .routes.api import bp as api_bp
    from .routes.ui import bp as ui_bp
    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp)

    @app.context_processor
    def globals_():
        from flask_login import current_user
        return {
            "SPECS": SPECS, "DIFFICULTIES": DIFFICULTIES,
            "auth_mode": app.config["AUTH_MODE"],
            "is_admin": is_admin(current_user),
            "llm_problem": config_mod.llm_problem(app.config),
        }

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    return app
