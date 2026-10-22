import json

from app.claude_cli import ClaudeCLI, render_transcript
from app.llm import LLM, drain
from app.schemas import DisclosureCheck


def test_single_user_turn_passes_through():
    assert render_transcript([{"role": "user", "content": "hello"}]) == "hello"


def test_conversation_is_rendered_as_transcript():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Which tracer?"}]},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret"},
                                              {"type": "text", "text": "ddtrace 3"}]},
            {"role": "user", "content": [{"type": "text", "text": "Thanks"}]}]
    out = render_transcript(msgs)
    assert '<turn role="user">\nWhich tracer?\n</turn>' in out
    assert '<turn role="assistant">\nddtrace 3\n</turn>' in out
    assert "secret" not in out
    assert out.rstrip().endswith("commentary.")


def test_errors_are_translated():
    assert ClaudeCLI._error("Invalid API key · Please run /login").status == 503
    assert ClaudeCLI._error("x", 429).status == 429
    assert ClaudeCLI._error("boom", 500).status == 502


def _fake_cli(tmp_path, events, monkeypatch):
    script = tmp_path / "claude"
    lines = "\n".join(json.dumps(e) for e in events)
    script.write_text(f"#!/bin/sh\ncat >/dev/null\ncat <<'EOF_'\n{lines}\nEOF_\n")
    script.chmod(0o755)
    return LLM({"LLM_PROVIDER": "claude_cli", "CLAUDE_CLI_PATH": str(script)})


def _delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta",
                                              "delta": {"type": "text_delta", "text": text}}}


def test_stream_text_through_fake_cli(tmp_path, monkeypatch):
    llm = _fake_cli(tmp_path, [_delta("Hel"), _delta("lo"),
                               {"type": "result", "subtype": "success", "is_error": False, "result": "Hello",
                                "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 2}}],
                    monkeypatch)
    chunks = []
    res = drain(llm.stream_text("claude-sonnet-5-5", "sys", [{"role": "user", "content": "hi"}]), chunks.append)
    assert chunks == ["Hel", "lo"] and res.text == "Hello" and res.usage.output_tokens == 2


def test_structured_through_fake_cli(tmp_path, monkeypatch):
    llm = _fake_cli(tmp_path, [{"type": "result", "subtype": "success", "is_error": False, "stop_reason": "tool_use",
                                "structured_output": {"revealed_fact_ids": ["f1"], "ungrounded_details": []}, "usage": {}}], monkeypatch)
    out, usage = llm.structured("claude-haiku-5-5", "sys", [{"role": "user", "content": "x"}], DisclosureCheck)
    assert out.revealed_fact_ids == ["f1"]
