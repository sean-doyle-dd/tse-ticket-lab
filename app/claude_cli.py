"""LLM_PROVIDER=claude_cli: run every call through the Claude Code CLI (`claude -p`) instead of
the API, so a Claude subscription login works without an API key.

- Auth: whatever the CLI is logged in with. In the container that is CLAUDE_CODE_OAUTH_TOKEN
  (from `claude setup-token` on your laptop); on a host it can also be your normal login.
- Isolation: no tools (--tools ""), no MCP servers, no settings/CLAUDE.md/hooks
  (--setting-sources "" from an empty working directory), no saved sessions. The only thing
  the CLI can do is answer.
- Structured output: --json-schema; the CLI returns the validated object as
  `structured_output` on its final `result` event, and we validate again with pydantic.
- Conversations: the CLI takes one prompt, so earlier turns are rendered as a transcript.
  Thinking blocks are not replayed; the customer's persona and hidden truth live in the
  system prompt, which is all it needs to stay consistent.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

from pydantic import ValidationError

from .llm import LLMError, StreamResult, Usage, strict_schema

log = logging.getLogger(__name__)

# Variables a parent Claude Code session sets; a nested `claude -p` that inherits them can
# attach to the parent session instead of starting cleanly.
_PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                        "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_MESSAGING_SOCKET",
                        "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
                        "CLAUDE_PID", "CLAUDE_EFFORT")
_AUTH_MARKERS = ("not logged in", "/login", "invalid api key", "oauth token", "authentication",
                 "401", "unauthorized")


def render_transcript(messages):
    """API-style messages -> one prompt. A single user turn is passed through as-is."""
    def text_of(content):
        if isinstance(content, str):
            return content
        return "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()

    turns = [(m["role"], text_of(m["content"])) for m in messages]
    if len(turns) == 1 and turns[0][0] == "user":
        return turns[0][1]
    parts = ["The conversation so far. Turns marked `user` are from the other party; turns "
             "marked `assistant` are yours. Notes marked `system` are instructions for you, "
             "not part of the conversation.\n"]
    for role, text in turns:
        parts.append(f'<turn role="{role}">\n{text}\n</turn>')
    parts.append("\nWrite your next turn now. Reply with only the text of that turn: no "
                 "role label, no <turn> tags, no commentary.")
    return "\n".join(parts)


class ClaudeCLI:
    supports_mid_conversation_system = False

    def __init__(self, cfg):
        self.cfg = cfg
        self.bin = cfg.get("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"
        self.timeout = cfg.get("CLAUDE_CLI_TIMEOUT", 600)
        self._slots = threading.BoundedSemaphore(max(1, cfg.get("CLAUDE_CLI_CONCURRENCY", 4)))
        self._workdir = tempfile.mkdtemp(prefix="tg-claude-")

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k not in _PARENT_SESSION_VARS}
        env.setdefault("DISABLE_AUTOUPDATER", "1")
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        return env

    def _cmd(self, model, system_file, effort, schema=None):
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--model", model, "--effort", effort, "--system-prompt-file", system_file,
               "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--no-session-persistence", "--disable-slash-commands"]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        return cmd

    def _run(self, model, system, messages, effort, schema=None):
        """Generator over parsed stream-json events; returns the final `result` event."""
        prompt = render_transcript(messages)
        with self._slots, tempfile.NamedTemporaryFile("w", suffix=".md", dir=self._workdir,
                                                      delete=False) as sf:
            sf.write(system)
            sf.close()
            try:
                proc = subprocess.Popen(self._cmd(model, sf.name, effort, schema), cwd=self._workdir,
                                        env=self._env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
            except FileNotFoundError:
                os.unlink(sf.name)
                raise LLMError("The claude CLI isn't installed (LLM_PROVIDER=claude_cli).", 503)
            killer = threading.Timer(self.timeout, proc.kill)
            killer.start()
            result, stderr = None, ""
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        result = event
                    yield event
                proc.wait()
                stderr = proc.stderr.read()
            finally:
                killer.cancel()
                if proc.poll() is None:
                    proc.kill()
                os.unlink(sf.name)
        if result is None:
            if proc.returncode == -9:
                raise LLMError(f"Claude took longer than {self.timeout}s. Try again.", 504)
            raise self._error(stderr, proc.returncode)
        if result.get("is_error") or result.get("subtype") != "success":
            raise self._error(f"{result.get('result') or ''} {result.get('subtype')} {stderr}",
                              result.get("api_error_status"))
        return result

    @staticmethod
    def _error(detail, status=None):
        low = str(detail).lower()
        log.warning("claude CLI failed (status %s): %s", status, str(detail)[:500])
        if status == 401 or any(m in low for m in _AUTH_MARKERS):
            return LLMError("The claude CLI isn't logged in. Run `claude setup-token` on your laptop, "
                            "put the token in .env as CLAUDE_CODE_OAUTH_TOKEN and restart.", 503)
        if status == 429 or "rate limit" in low or "usage limit" in low:
            return LLMError("Your Claude subscription is rate-limited right now. Wait and retry.", 429)
        return LLMError(f"The claude CLI failed: {str(detail).strip()[:300] or 'no output'}", 502)

    @staticmethod
    def _check_stop(result):
        stop = result.get("stop_reason")
        if stop == "refusal":
            raise LLMError("Claude declined this request (safety classifier). Try generating again.", 502)
        if stop == "max_tokens":
            raise LLMError("Claude's response was cut off (max_tokens). Try again.", 502)

    @staticmethod
    def _usage(model, result, started):
        u = result.get("usage") or {}
        return Usage(model=model, input_tokens=u.get("input_tokens", 0) or 0,
                     output_tokens=u.get("output_tokens", 0) or 0,
                     cache_read_tokens=u.get("cache_read_input_tokens", 0) or 0,
                     cache_write_tokens=u.get("cache_creation_input_tokens", 0) or 0,
                     duration_ms=int((time.monotonic() - started) * 1000),
                     cost_usd=float(result.get("total_cost_usd") or 0)).priced()

    # Same signatures as LLM.structured / LLM.stream_text; max_tokens is the CLI's to manage.

    def structured(self, model, system, messages, schema_model, effort="high", max_tokens=None):
        last_err = None
        for attempt in range(2):
            started = time.monotonic()
            gen = self._run(model, system, messages, effort, strict_schema(schema_model))
            while True:
                try:
                    next(gen)
                except StopIteration as stop:
                    result = stop.value
                    break
            usage = self._usage(model, result, started)
            self._check_stop(result)
            data = result.get("structured_output")
            try:
                if data is None:
                    return schema_model.model_validate_json(result.get("result") or ""), usage
                return schema_model.model_validate(data), usage
            except ValidationError as exc:
                last_err = exc
                log.warning("structured output failed validation (attempt %s): %s", attempt + 1, exc)
        raise LLMError(f"Claude returned output that didn't match the schema: {last_err}", 502)

    def stream_text(self, model, system, messages, effort="medium", max_tokens=None):
        started = time.monotonic()
        gen = self._run(model, system, messages, effort)
        chunks = []
        while True:
            try:
                event = next(gen)
            except StopIteration as stop:
                result = stop.value
                break
            if event.get("type") != "stream_event":
                continue
            ev = event.get("event") or {}
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                chunks.append(delta["text"])
                yield delta["text"]
        usage = self._usage(model, result, started)
        self._check_stop(result)
        text = "".join(chunks) or result.get("result") or ""
        return StreamResult(text=text, blocks=[{"type": "text", "text": text}], usage=usage)
