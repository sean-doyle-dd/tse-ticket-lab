import io
import json
import zipfile

from app.extensions import db
from app.llm import LLMError
from app.models import LLMCall, Message, Scenario

from .conftest import login


def _sse(resp):
    events = []
    for chunk in resp.get_data(as_text=True).strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if ": " in line)
        events.append((lines.get("event"), json.loads(lines.get("data", "null"))))
    return events


def _new_ticket(client, spec="apm", difficulty="low"):
    r = client.post("/api/scenarios", json={"spec": spec, "difficulty": difficulty})
    assert r.status_code == 202, r.get_json()
    return r.get_json()["id"]


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["db"] and body["corpus"]["apm"] >= 5 and "apm/multi-service" in body["templates"]
    assert body["llm"]["problem"] is None


def test_pages_require_login(client):
    assert client.get("/").status_code == 302
    assert client.get("/api/specs").status_code in (302, 401)


def test_dev_login_enforces_domain(client):
    r = login(client, email="someone@gmail.com")
    assert r.status_code == 400
    r = login(client)
    assert r.status_code == 302
    page = client.get("/")
    assert page.status_code == 200 and b"Generate ticket" in page.data


def test_full_ticket_lifecycle(client, app, fake_llm):
    login(client)
    sid = _new_ticket(client)

    data = client.get(f"/api/scenarios/{sid}").get_json()
    assert data["status"] == "open" and data["ref"].startswith("TG-")
    assert "truth" not in data, "hidden truth must not leak while open"
    assert data["sandbox"]["needed"] and data["facts_total"] == 2
    assert client.get(f"/t/{sid}").status_code == 200

    r = client.post(f"/api/scenarios/{sid}/messages", json={"content": "Can you share the tracer startup log?"})
    assert r.mimetype == "text/event-stream"
    events = _sse(r)
    kinds = [e for e, _ in events]
    assert kinds[0] == "tse" and kinds[-1] == "done" and "delta" in kinds
    assert "".join(d["text"] for e, d in events if e == "delta") == fake_llm.reply
    with app.app_context():
        s = db.session.get(Scenario, sid)
        assert s.revealed_fact_ids == ["f2"]
        customer = [m for m in s.messages if m.role == "customer"][0]
        assert customer.api_blocks[0]["type"] == "thinking", "assistant blocks must be stored for unchanged replay"
        assert {c.purpose for c in LLMCall.query.all()} >= {"generate", "customer", "classify"}

    # second turn replays the stored blocks unchanged
    second = _sse(client.post(f"/api/scenarios/{sid}/messages",
                              json={"content": "Set DD_AGENT_HOST=datadog-agent", "kind": "resolution"}))
    assert second[0][1]["kind"] == "resolution" and second[-1][0] == "done"
    _, system, messages = [c for c in fake_llm.calls if c[0] == "stream"][-1]
    assert messages[1]["role"] == "assistant" and messages[1]["content"][0]["type"] == "thinking"
    assert messages[-1]["role"] == "system"
    assert "Priya Raman" in system

    r = client.post(f"/api/scenarios/{sid}/close")
    closed = r.get_json()
    assert r.status_code == 200 and closed["status"] == "closed"
    assert closed["truth"]["root_cause"] and closed["grade"]["overall"] == 75
    assert [m["role"] for m in closed["messages"]].count("system") == 0
    assert client.post(f"/api/scenarios/{sid}/messages", json={"content": "hello?"}).status_code == 409

    hist = client.get("/history")
    assert b"Storefront traces missing" in hist.data


def test_other_users_cannot_see_ticket(client):
    login(client)
    sid = _new_ticket(client)
    client.post("/logout")
    login(client, email="other@datadoghq.com", name="Other")
    assert client.get(f"/api/scenarios/{sid}").status_code == 404
    assert client.get(f"/t/{sid}").status_code == 404
    assert client.get(f"/api/scenarios/{sid}/sandbox.zip").status_code == 404


def test_admin_can_view_but_not_reply(client):
    login(client)
    sid = _new_ticket(client)
    client.post("/logout")
    login(client, email="boss@datadoghq.com", name="Boss")
    assert client.get(f"/api/scenarios/{sid}").status_code == 200
    assert client.post(f"/api/scenarios/{sid}/messages", json={"content": "hi"}).status_code == 403
    assert client.get("/team").status_code == 200


def test_team_page_hidden_from_non_admins(client):
    login(client)
    assert client.get("/team").status_code == 404


def test_sandbox_and_repro_downloads(client, fake_llm):
    login(client)
    sid = _new_ticket(client)
    r = client.get(f"/api/scenarios/{sid}/sandbox.zip")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.data))
    readme = next(n for n in zf.namelist() if n.endswith("/README.md"))
    text = zf.read(readme).decode()
    assert "Storefront traces missing" in text and "sandbox org" in text
    assert "localhost" not in text, "README must not reveal the injected fault"
    env = next(n for n in zf.namelist() if n.endswith("/config/storefront.env") and ".tg/" not in n)
    assert b"DD_AGENT_HOST=localhost" in zf.read(env)
    r = client.get(f"/api/scenarios/{sid}/repro.md")
    assert r.status_code == 200 and b"Reproduction notes" in r.data


def test_no_sandbox_ticket_offers_repro_only(client, fake_llm):
    from .conftest import sample_scenario
    fake_llm.scenario = sample_scenario(needed=False, template_id="", params=[])
    login(client)
    sid = _new_ticket(client, spec="web-platform")
    assert client.get(f"/api/scenarios/{sid}").get_json()["sandbox"]["needed"] is False
    assert client.get(f"/api/scenarios/{sid}/sandbox.zip").status_code == 404
    assert client.get(f"/api/scenarios/{sid}/repro.md").status_code == 200


def test_validation_and_quota(client):
    login(client)
    assert client.post("/api/scenarios", json={"spec": "nope", "difficulty": "low"}).status_code == 400
    for _ in range(3):
        _new_ticket(client)
    r = client.post("/api/scenarios", json={"spec": "apm", "difficulty": "low"})
    assert r.status_code == 429 and "limit" in r.get_json()["error"]


def test_empty_and_oversized_messages_rejected(client, app):
    login(client)
    sid = _new_ticket(client)
    assert client.post(f"/api/scenarios/{sid}/messages", json={"content": "  "}).status_code == 400
    big = "x" * (app.config["MAX_MESSAGE_CHARS"] + 1)
    assert client.post(f"/api/scenarios/{sid}/messages", json={"content": big}).status_code == 400
    assert client.post(f"/api/scenarios/{sid}/messages", json={"content": "hi", "kind": "weird"}).status_code == 400


def test_stream_failure_keeps_tse_message(client, app, fake_llm):
    login(client)
    sid = _new_ticket(client)
    fake_llm.fail_stream = LLMError("Claude is rate-limiting us right now.", 429)
    events = _sse(client.post(f"/api/scenarios/{sid}/messages", json={"content": "Hello"}))
    assert events[-1][0] == "error" and "saved" in events[-1][1]["message"]
    with app.app_context():
        assert [m.role for m in Message.query.filter_by(scenario_id=sid)] == ["tse"]


def test_generation_failure_is_reported(client, fake_llm):
    def boom(*a, **k):
        raise LLMError("Claude declined this request (safety classifier). Try generating again.")
    fake_llm.structured = boom
    login(client)
    sid = _new_ticket(client)
    data = client.get(f"/api/scenarios/{sid}").get_json()
    assert data["status"] == "failed" and "declined" in data["error"]


def test_missing_api_key_blocks_generation(tmp_path, fake_llm):
    from app import create_app
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'k.db'}", "AUTH_MODE": "dev",
                      "WTF_CSRF_ENABLED": False, "ANTHROPIC_API_KEY": ""}, llm=fake_llm)
    with app.app_context():
        db.create_all()
    c = app.test_client()
    login(c)
    r = c.post("/api/scenarios", json={"spec": "apm", "difficulty": "low"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.get_json()["error"]
    assert "Claude isn't configured" in c.get("/").get_data(as_text=True)


def test_csrf_enforced(tmp_path, fake_llm):
    from app import create_app
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'c.db'}", "AUTH_MODE": "dev",
                      "ANTHROPIC_API_KEY": "k"}, llm=fake_llm)
    with app.app_context():
        db.create_all()
    c = app.test_client()
    assert c.post("/login", data={"email": "a@datadoghq.com", "name": "A"}).status_code == 400


def test_google_mode_requires_secrets(tmp_path):
    import pytest
    from app import create_app
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'g.db'}", "AUTH_MODE": "google"})
