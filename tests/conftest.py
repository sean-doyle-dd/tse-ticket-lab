import pytest

from app import create_app
from app.extensions import db
from app.llm import StreamResult, Usage
from app.schemas import DisclosureCheck, GeneratedScenario, GradeResult, GroundingCheck


def sample_scenario(**sandbox):
    sb = {"needed": True, "template_id": "apm/multi-service",
          "params": [{"name": "STOREFRONT_AGENT_HOST", "value": "localhost"}],
          "repro_steps": ["Start the sandbox", "Open APM > Services"], "rationale": "fault 1"}
    sb.update(sandbox)
    return GeneratedScenario.model_validate({
        "ticket": {
            "subject": "Storefront traces missing since deploy",
            "body": "Hi team, since this morning our storefront service stopped sending traces.",
            "priority": "high", "product_area": "APM",
            "environment": ["Docker compose", "Agent 7", "Python ddtrace"],
            "attachments": [{"filename": "storefront.log", "kind": "log", "content": "INFO started"}],
            "customer": {"name": "Priya Raman", "title": "SRE", "company": "Northwind Outfitters",
                         "company_profile": "mid-size retailer", "technical_level": "medium",
                         "tone": "busy", "timezone": "Europe/Dublin"},
        },
        "truth": {
            "summary": "DD_AGENT_HOST=localhost", "root_cause": "storefront sends traces to localhost",
            "resolution_steps": ["Set DD_AGENT_HOST=datadog-agent"], "acceptable_alternatives": [],
            "facts": [
                {"id": "f1", "statement": "Deploy moved env vars to a new file", "reveal_when": "asked what changed", "category": "timeline", "grounding": "story", "source": "story"},
                {"id": "f1", "statement": "Tracer log shows agent_url localhost", "reveal_when": "asked for tracer startup log", "category": "evidence", "grounding": "seed", "source": "evidence[0]"},
            ],
            "red_herrings": ["A CDN change"], "customer_misconceptions": ["Datadog is down"],
            "escalation": "not_needed", "escalation_reason": "config issue",
            "learning_objectives": ["tracer connectivity"], "doc_links": [],
        },
        "sandbox": sb,
        "difficulty_rationale": "single misconfiguration",
    })


def sample_grade(score=4):
    c = {"score": score, "justification": "ok"}
    return GradeResult.model_validate({
        "diagnosis": c, "information_gathering": c, "communication": c, "resolution": c,
        "resolved": True, "escalation_judgement": "correctly not escalated", "overall_summary": "Good.",
        "strengths": ["asked for logs"], "improvements": ["batch questions"], "missed_facts": [],
        "approach_comparison": "Matched the real TSE's first step.",
        "model_first_reply": "Hi Priya, ...",
    })


class FakeLLM:
    supports_mid_conversation_system = True

    def __init__(self):
        self.calls = []
        self.scenario = sample_scenario()
        self.reply = "Sure, here's the log:\n```attachment filename=\"tracer.log\"\nagent_url: http://localhost:8126\n```"
        self.revealed = ["f2", "nope"]
        self.grade = sample_grade()
        self.fail_stream = None
        self.grounding = [[]]  # one list of unsupported claims per grounding check, in order

    def structured(self, model, system, messages, schema_model, effort="high", max_tokens=48000):
        self.calls.append(("structured", schema_model.__name__, system, messages))
        usage = Usage(model=model, input_tokens=10, output_tokens=5)
        if schema_model is GeneratedScenario:
            return self.scenario.model_copy(deep=True), usage
        if schema_model is DisclosureCheck:
            return DisclosureCheck(revealed_fact_ids=self.revealed, ungrounded_details=[]), usage
        if schema_model is GroundingCheck:
            claims = self.grounding.pop(0) if len(self.grounding) > 1 else self.grounding[0]
            return GroundingCheck(unsupported=claims), usage
        if schema_model is GradeResult:
            return self.grade.model_copy(deep=True), usage
        raise AssertionError(schema_model)

    def stream_text(self, model, system, messages, effort="medium", max_tokens=16000):
        self.calls.append(("stream", system, messages))
        if self.fail_stream:
            raise self.fail_stream
        for i in range(0, len(self.reply), 10):
            yield self.reply[i:i + 10]
        return StreamResult(text=self.reply, blocks=[{"type": "thinking", "thinking": "", "signature": "sig"},
                                                     {"type": "text", "text": self.reply}],
                            usage=Usage(model=model, input_tokens=100, output_tokens=40, cache_read_tokens=90))


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def app(tmp_path, fake_llm):
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "AUTH_MODE": "dev", "SECRET_KEY": "x" * 40, "WTF_CSRF_ENABLED": False, "TESTING": True,
        "ANTHROPIC_API_KEY": "test-key", "RUN_GENERATION_INLINE": True, "ADMIN_EMAILS": ["boss@datadoghq.com"],
        "GENERATIONS_PER_HOUR": 3, "CORPUS_ALLOW_BOOTSTRAP": True,
    }, llm=fake_llm)
    with app.app_context():
        db.create_all()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, email="tse@datadoghq.com", name="Test TSE"):
    return client.post("/login", data={"email": email, "name": name})
