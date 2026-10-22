"""Environment-driven configuration.

Every setting comes from the environment (docker compose passes `.env` through), so the
same image runs locally in dev-auth mode and in a shared deployment behind Google SSO.
`problems()` lists anything that would stop the app working, so startup and /healthz can
say exactly what is missing instead of failing later with a stack trace.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LLM_PROVIDERS = ("anthropic", "bedrock", "vertex", "claude_cli")
AUTH_MODES = ("google", "dev")


def _bool(env, name, default):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(env, name, default):
    raw = env.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _list(env, name):
    return [x.strip().lower() for x in env.get(name, "").split(",") if x.strip()]


def load(env=None):
    """Return a flat dict suitable for `app.config.from_mapping`."""
    e = os.environ if env is None else env
    return {
        "SECRET_KEY": e.get("SECRET_KEY", ""),
        "SQLALCHEMY_DATABASE_URI": e.get("DATABASE_URL") or f"sqlite:///{ROOT / 'instance' / 'dev.db'}",
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
        "AUTH_MODE": e.get("AUTH_MODE", "dev").strip().lower(),
        "GOOGLE_CLIENT_ID": e.get("GOOGLE_CLIENT_ID", ""),
        "GOOGLE_CLIENT_SECRET": e.get("GOOGLE_CLIENT_SECRET", ""),
        "ALLOWED_EMAIL_DOMAIN": e.get("ALLOWED_EMAIL_DOMAIN", "datadoghq.com").strip().lower(),
        "ADMIN_EMAILS": _list(e, "ADMIN_EMAILS"),
        "SESSION_COOKIE_SECURE": _bool(e, "SESSION_COOKIE_SECURE", False),
        "SESSION_COOKIE_SAMESITE": "Lax",
        "WTF_CSRF_TIME_LIMIT": None,
        "LLM_PROVIDER": e.get("LLM_PROVIDER", "anthropic").strip().lower(),
        "ANTHROPIC_API_KEY": e.get("ANTHROPIC_API_KEY", ""),
        "LLM_FALLBACKS": _bool(e, "LLM_FALLBACKS", True),
        "AWS_REGION": e.get("AWS_REGION", ""),
        "VERTEX_PROJECT_ID": e.get("VERTEX_PROJECT_ID", ""),
        "VERTEX_REGION": e.get("VERTEX_REGION", "global"),
        "CLAUDE_CLI_PATH": e.get("CLAUDE_CLI_PATH", ""),
        "CLAUDE_CLI_TIMEOUT": _int(e, "CLAUDE_CLI_TIMEOUT", 600),
        "CLAUDE_CLI_CONCURRENCY": _int(e, "CLAUDE_CLI_CONCURRENCY", 4),
        "MODEL_GENERATOR": e.get("MODEL_GENERATOR", "claude-opus-5-5"),
        "MODEL_CUSTOMER": e.get("MODEL_CUSTOMER", "claude-sonnet-5-5"),
        "MODEL_GRADER": e.get("MODEL_GRADER", "claude-opus-5-5"),
        "MODEL_CLASSIFIER": e.get("MODEL_CLASSIFIER", "claude-haiku-5-5"),
        "MODEL_VERIFIER": e.get("MODEL_VERIFIER", "claude-sonnet-5-5"),
        "EFFORT_GENERATOR": e.get("EFFORT_GENERATOR", "high"),
        "EFFORT_CUSTOMER": e.get("EFFORT_CUSTOMER", "medium"),
        "EFFORT_GRADER": e.get("EFFORT_GRADER", "high"),
        "EFFORT_CLASSIFIER": e.get("EFFORT_CLASSIFIER", "low"),
        "EFFORT_VERIFIER": e.get("EFFORT_VERIFIER", "medium"),
        # Every generated ticket is checked against its source ticket; unsupported technical
        # claims trigger a regeneration (up to GROUNDING_ATTEMPTS in all), then the ticket
        # fails rather than ship them.
        "GROUNDING_CHECK": _bool(e, "GROUNDING_CHECK", True),
        "GROUNDING_ATTEMPTS": _int(e, "GROUNDING_ATTEMPTS", 3),
        # Hand-written starter seeds (not from real tickets) are off unless you opt in.
        "CORPUS_ALLOW_BOOTSTRAP": _bool(e, "CORPUS_ALLOW_BOOTSTRAP", False),
        "GENERATIONS_PER_HOUR": _int(e, "GENERATIONS_PER_HOUR", 10),
        "MESSAGES_PER_HOUR": _int(e, "MESSAGES_PER_HOUR", 120),
        "MAX_MESSAGE_CHARS": _int(e, "MAX_MESSAGE_CHARS", 8000),
        "CORPUS_DIR": e.get("CORPUS_DIR") or str(ROOT / "corpus"),
        "SANDBOX_TEMPLATES_DIR": e.get("SANDBOX_TEMPLATES_DIR") or str(ROOT / "sandbox_templates"),
        "RUN_GENERATION_INLINE": _bool(e, "RUN_GENERATION_INLINE", False),
    }


def problems(cfg):
    """Human-readable configuration errors (empty list == good to go)."""
    out = []
    if cfg["AUTH_MODE"] not in AUTH_MODES:
        out.append(f"AUTH_MODE must be one of {AUTH_MODES}, got {cfg['AUTH_MODE']!r}")
    if cfg["AUTH_MODE"] == "google":
        if not (cfg["GOOGLE_CLIENT_ID"] and cfg["GOOGLE_CLIENT_SECRET"]):
            out.append("AUTH_MODE=google needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        if len(cfg["SECRET_KEY"]) < 32:
            out.append("AUTH_MODE=google needs a SECRET_KEY of at least 32 characters")
    if cfg["LLM_PROVIDER"] not in LLM_PROVIDERS:
        out.append(f"LLM_PROVIDER must be one of {LLM_PROVIDERS}, got {cfg['LLM_PROVIDER']!r}")
    return out


def llm_problem(cfg):
    """Why Claude can't be called right now, or None. Not fatal at startup: the UI still
    loads and shows this message where generation would happen."""
    p = cfg["LLM_PROVIDER"]
    if p == "anthropic" and not cfg["ANTHROPIC_API_KEY"]:
        return "ANTHROPIC_API_KEY is not set. Add it to .env and restart (docker compose up -d)."
    if p == "bedrock" and not cfg["AWS_REGION"]:
        return "LLM_PROVIDER=bedrock needs AWS_REGION (and AWS credentials in the environment)."
    if p == "vertex" and not cfg["VERTEX_PROJECT_ID"]:
        return "LLM_PROVIDER=vertex needs VERTEX_PROJECT_ID (and GCP application-default credentials)."
    if p == "claude_cli":
        if not shutil.which(cfg.get("CLAUDE_CLI_PATH") or "claude"):
            return "LLM_PROVIDER=claude_cli but the claude CLI isn't installed in this environment."
        if os.path.exists("/.dockerenv") and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return ("CLAUDE_CODE_OAUTH_TOKEN is not set. Run `claude setup-token` on your laptop, "
                    "add the token to .env and restart (docker compose up -d).")
    return None
