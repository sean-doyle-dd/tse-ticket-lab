# Vendored from ticket-memory/webapp/claude_cli.py (same author). Host-only: shells out to the
# local `claude` CLI, which carries your Claude Code login and MCP connectors.
"""Shared helper for shelling out to the local `claude` CLI headlessly.

Same pattern as ../ticket-log-local/app.py's "Mission Control" — the CLI is already
authenticated with your Claude Code login and its MCP connectors (Zendesk, Slack), so
no separate Zendesk API token, Anthropic API key, or Slack bot token is needed here.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

CLAUDE_BIN = shutil.which("claude") or "claude"

# Permission mode for every headless call. It used to be bypassPermissions, which allows
# EVERY tool and ignores --allowedTools — so the "narrow allowedTools" that callers pass
# was never actually enforced, and a prompt built from untrusted ticket text could have
# reached any tool. In the default mode, `claude -p` denies anything not listed in
# --allowedTools, so the allow-list is real. Override only if a connector genuinely
# needs it (a denial now shows up as a loud ToolCallError, never as invented data).
PERMISSION_MODE = os.environ.get("TICKET_MEMORY_CLI_PERMISSION_MODE", "default")

AUTH_HICCUP_MARKERS = ("authenticat", "authoriz")

# Concurrent `claude -p` invocations (e.g. the background poller and an on-demand
# Watch/Learn button firing at the same time) were observed to interfere with each
# other — a call that succeeds in isolation can come back with a wrong/empty answer
# under concurrency, without tripping the auth-hiccup detection below. Serializing
# every CLI call through this lock fixed it in testing. This does mean only one
# claude call runs at a time app-wide, which is a real throughput cost worth knowing
# about if the poller ever watches many tickets — not a bug, a deliberate tradeoff
# for correctness over speed.
_cli_lock = threading.Lock()


class ClaudeCliError(Exception):
    def __init__(self, message, status=502, raw_text=None):
        super().__init__(message)
        self.status = status
        # The model's actual raw reply text, when available — callers that need to
        # distinguish "genuine transient hiccup" from e.g. "this is actually a
        # restricted-ticket signal that happened to trip the hiccup-text detector"
        # need the real text, since the generic message above doesn't carry it.
        self.raw_text = raw_text
        self.raw_texts = None


def configured():
    return shutil.which("claude") is not None


def run_claude_json(prompt, allowed_tools="", model="claude-haiku-4-5-20251001",
                     max_attempts=3, per_attempt_timeout=120):
    """Run the claude CLI headlessly and parse a JSON object/array out of its reply.
    Retries on the connector's occasional transient auth hiccup (a follow-up call has
    reliably succeeded in testing even though nothing about the request changed) —
    bumped from 2 to 3 attempts after observing this connector fail its first retry
    too under real load, not just the first attempt.
    Pass allowed_tools="" for pure-reasoning calls that don't need any MCP tool.
    Serialized app-wide via _cli_lock — see the comment on it above."""
    with _cli_lock:
        return _run_claude_json_locked(prompt, allowed_tools, model, max_attempts, per_attempt_timeout)


def _run_claude_json_locked(prompt, allowed_tools, model, max_attempts, per_attempt_timeout):
    last_error = None
    all_raw_texts = []  # every attempt's raw text, not just the last — a caller-side
    # restriction check must not lose an earlier attempt's revealing text just because
    # a later attempt failed a different way (e.g. timed out) and produced none.
    for attempt in range(1, max_attempts + 1):
        cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json", "--model", model,
               "--permission-mode", PERMISSION_MODE]
        if allowed_tools:
            cmd += ["--allowedTools", allowed_tools]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=per_attempt_timeout)
        except subprocess.TimeoutExpired:
            last_error = ClaudeCliError(f"claude CLI timed out after {per_attempt_timeout}s", 504)
            continue
        except FileNotFoundError:
            raise ClaudeCliError("claude CLI not found on PATH — is Claude Code installed?", 500)

        if proc.returncode != 0:
            last_error = ClaudeCliError(f"claude CLI failed: {proc.stderr.strip()[:300]}")
            continue

        try:
            outer = json.loads(proc.stdout)
            result_text = outer["result"].strip()
        except (json.JSONDecodeError, KeyError):
            result_text = ""

        if result_text:
            all_raw_texts.append(result_text)

        # Try to parse the real response FIRST. The auth-hiccup markers below used to
        # be checked unconditionally, before ever attempting to parse — which meant a
        # perfectly valid, successfully-fetched JSON response got discarded and
        # retried-to-death any time the ticket's own real content happened to contain
        # a word like "authorized" (e.g. routine "needs an authorized admin to
        # approve" support-ticket boilerplate — nothing to do with any connector
        # actually needing to re-authorize). Only fall back to the hiccup-text
        # heuristic once parsing has genuinely failed, i.e. the model's reply wasn't
        # usable JSON at all (most often it explaining in prose that a tool/connector
        # isn't available yet), not just "the JSON happens to mention authorization."
        match = re.search(r"[\{\[].*[\}\]]", result_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        if any(m in result_text.lower() for m in AUTH_HICCUP_MARKERS):
            last_error = ClaudeCliError(
                "A connector needs a moment to re-authorize for headless calls (clears on "
                "its own — try again shortly, or run one interactive MCP call in Claude "
                "Code to refresh it).",
                raw_text=result_text,
            )
            last_error.raw_texts = list(all_raw_texts)
            if attempt < max_attempts:
                continue
            raise last_error

        (Path(os.environ.get("TMPDIR", "/tmp")) / "ticket-lab-cli-debug.log").write_text(
            f"attempt={attempt} RC={proc.returncode}\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n"
        )
        last_error = ClaudeCliError("claude CLI returned something unparseable — try again")
        if attempt < max_attempts:
            continue
        raise last_error

    final_error = last_error or ClaudeCliError("claude CLI failed for an unknown reason — try again")
    if getattr(final_error, "raw_texts", None) is None:
        final_error.raw_texts = list(all_raw_texts)
    raise final_error


# --- Verbatim tool calls ------------------------------------------------------------------
#
# WHY. Every data fetch used to be "ask the model to call tool X, then retype the result as
# JSON", and the app trusted the retyped text. When the tool failed, wasn't loaded, or the
# model simply paraphrased, the app received plausible *invented* data with no error at all
# — confirmed 2026-10-16: auto-learn was handed a fabricated "looking into it... fix
# deployed... confirmed resolved" thread for a ticket whose real comments say nothing of
# the kind.
#
# HOW. run_tool asks the model only to make one exact call, runs the CLI with
# --output-format stream-json, and reads the tool's own result out of the event stream.
# The model's reply text is never used as data. If the stream does not contain a tool_use
# for exactly the requested tool with exactly the requested arguments, followed by its
# tool_result, the call fails loudly (ToolCallError) — it can never "succeed" with
# content the tool did not return.


class ToolCallError(ClaudeCliError):
    """The requested tool call did not demonstrably happen as asked (not called, called
    with different arguments, denied, or returned an error)."""


def _blocks(event):
    msg = event.get("message") if isinstance(event, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, list) else []


def _tool_result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _args_match(requested, actual):
    if not isinstance(actual, dict):
        return False
    for k, v in requested.items():
        if k not in actual:
            return False
        a = actual[k]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                if float(a) != float(v):
                    return False
            except (TypeError, ValueError):
                return False
        elif a != v:
            return False
    return True


def parse_tool_stream(stdout, tool, args):
    """Pure parser (unit-testable): return the raw text of the tool_result for the one
    tool_use whose name == tool and whose input matches args. Raises ToolCallError."""
    uses, results = {}, {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for b in _blocks(event):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses[b.get("id")] = (b.get("name"), b.get("input"))
            elif b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = (bool(b.get("is_error")), _tool_result_text(b))
    called = [(uid, inp) for uid, (name, inp) in uses.items() if name == tool]
    if not called:
        raise ToolCallError(f"{tool} was never called (tools used: {sorted({n for n, _ in uses.values()})})")
    exact = [uid for uid, inp in called if _args_match(args, inp)]
    if not exact:
        raise ToolCallError(f"{tool} was called with different arguments than requested: {called[0][1]}")
    uid = exact[-1]
    if uid not in results:
        raise ToolCallError(f"{tool} produced no result")
    is_error, text = results[uid]
    if is_error:
        raise ToolCallError(f"{tool} returned an error: {text[:300]}", raw_text=text)
    return text


def run_tool(tool, args, deferred=False, model="claude-haiku-4-5-20251001", per_attempt_timeout=180,
             max_attempts=2):
    """Call one MCP tool with exact arguments and return its result VERBATIM (parsed JSON
    when the result is JSON, otherwise the raw text). See the note above."""
    allowed = f"ToolSearch,{tool}" if deferred else tool
    load = (f'First call ToolSearch with query "select:{tool}" to load the tool. ' if deferred else "")
    prompt = (
        f"{load}Call the tool {tool} exactly once with exactly these arguments, unchanged:\n"
        f"{json.dumps(args)}\n"
        f"Do not call any other tool and do not retry with different arguments. After the "
        f"call returns, reply with only the word: done"
    )
    with _cli_lock:
        last = None
        for _ in range(max_attempts):
            cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose",
                   "--model", model, "--permission-mode", PERMISSION_MODE, "--allowedTools", allowed]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=per_attempt_timeout)
            except subprocess.TimeoutExpired:
                last = ToolCallError(f"{tool}: claude CLI timed out after {per_attempt_timeout}s", 504)
                continue
            except FileNotFoundError:
                raise ClaudeCliError("claude CLI not found on PATH — is Claude Code installed?", 500)
            try:
                text = parse_tool_stream(proc.stdout, tool, args)
            except ToolCallError as exc:
                last = exc
                if exc.raw_text:  # the tool itself answered with an error: don't retry
                    raise
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        raise last or ToolCallError(f"{tool}: failed for an unknown reason")
