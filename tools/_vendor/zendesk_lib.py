# Vendored from ticket-memory/webapp/zendesk_lib.py (same author). Host-only.
"""Zendesk access via the local `claude` CLI's Zendesk MCP connector — no separate
API token needed. Deliberately narrow allowedTools per call: ticket content is
untrusted external text, so the blast radius of anything unexpected in it stays
contained to "reads tickets," never a write/Bash/Edit.
"""
import html
import re

import claude_cli

ZENDESK_SUBDOMAIN = "datadog"  # only used to build agent-facing ticket URLs for Slack/webapp links

# Compliance boundary: a ticket is treated as restricted ONLY when the org itself has
# stated it — never because ordinary ticket text happens to contain a scary word.
#
# The previous version ran a broad regex (not found|access denied|restrict|hipaa|opt.?out|
# unauthorized|forbidden) over EVERY comment body. Customers write "traces not found",
# "403 Forbidden", "restricted API key" all the time, so real diagnostic tickets were
# silently dropped (every "restriction signal" in app.log through 2026-10-16 was a false
# positive on genuine customer text), and because RestrictedTicketError was logged as a
# retryable error, the same ticket was re-run ~190 times.
#
# Authoritative, org-stated signals only:
#   1. The Zendesk MCP's own restriction response — it marks HIPAA/AI-opt-out tickets
#      with an `error_message` field on single-ticket tools (passed through verbatim by
#      the prompts below), or, as observed in practice, a lone pseudo-comment that IS the
#      tool's error text ("Error: ... not found or access denied").
#   2. The ticket's own Zendesk tags matching *hipaa* or *opted_out* (e.g. hipaa_org,
#      is_hipaa, oai_opted_out) — set from the org's configuration, not free text.
# Snowflake-sourced candidates are additionally filtered server-side on the org's
# DIM_ORG_AI_FEATURE_FLAGS row (see snowflake_lib.py).
_ORG_RESTRICTION_TAG_RE = re.compile(r"hipaa|opted_out", re.IGNORECASE)
# Only ever applied to text that is unmistakably a tool error (never to parsed ticket
# content): the model's prose reply when a call failed, or a lone "Error..." pseudo-comment.
_TOOL_RESTRICTION_RE = re.compile(
    r"error_message|access denied|\bhipaa\b|opted[ _-]?out|ai[ _-]?restricted", re.IGNORECASE
)


class RestrictedTicketError(Exception):
    """Raised when the org has stated this ticket must not be processed by AI (HIPAA or
    AI opt-out) — callers must stop processing it immediately and treat it as terminal,
    not continue with whatever partial data was already fetched."""


def restricted_tags(tags):
    """Subset of a ticket's tags that are org-stated HIPAA / AI-opt-out markers."""
    return sorted(t for t in (tags or []) if isinstance(t, str) and _ORG_RESTRICTION_TAG_RE.search(t))


def _reraise_if_restriction_signal(ticket_id, exc):
    """claude_cli's own auth-hiccup detector can fire on a restriction response before
    this module sees the raw text. exc.raw_texts only holds replies that FAILED to parse
    as JSON (a successful parse returns early), i.e. the model's prose about a tool
    error — never real ticket content — so a narrow match here is safe."""
    for text in (getattr(exc, "raw_texts", None) or []):
        if _TOOL_RESTRICTION_RE.search(text):
            raise RestrictedTicketError(f"ticket {ticket_id}: {text[:200]}") from exc

GET_TICKET_TOOL = "mcp__claude_ai_Zendesk_MCP__get_ticket"
COMMENTS_TOOL = "mcp__claude_ai_Zendesk_MCP__list_ticket_comments"
LIST_MY_TICKETS_TOOL = "mcp__claude_ai_Zendesk_MCP__list_my_tickets"
# search_tickets is NOT usable from a headless `claude -p` call — confirmed by testing:
# the model reports "I don't have access to a search_tickets function" even with it
# listed in --allowedTools (it's likely a deferred tool that needs an interactive
# ToolSearch step first, which a one-shot headless call never gets). list_my_tickets
# works headlessly (same tool ticket-log-local's Mission Control already relies on).


def configured():
    return claude_cli.configured()


# ALL ticket data below comes verbatim from the Zendesk MCP tool result (via
# claude_cli.run_tool), parsed here in Python. A model never retypes ticket content —
# that is how fabricated comment threads got into learning before 2026-10-16.


def _result(payload):
    """The Zendesk MCP wraps responses as {"result": {...}}; unwrap if present."""
    if isinstance(payload, dict) and isinstance(payload.get("result"), (dict, list)):
        return payload["result"]
    return payload


def _error_message(payload):
    for obj in (payload, _result(payload)):
        if isinstance(obj, dict) and obj.get("error_message"):
            return str(obj["error_message"])
    return None


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _call(tool, args, ticket_id):
    try:
        payload = claude_cli.run_tool(tool, args, deferred=True)
    except claude_cli.ClaudeCliError as exc:
        _reraise_if_restriction_signal(ticket_id, exc)
        if getattr(exc, "raw_text", None) and _TOOL_RESTRICTION_RE.search(exc.raw_text):
            raise RestrictedTicketError(f"ticket {ticket_id}: {exc.raw_text[:200]}") from exc
        raise
    err = _error_message(payload)
    if err:
        # The MCP's own restriction / access response, verbatim.
        raise RestrictedTicketError(f"ticket {ticket_id}: {err[:200]}")
    if isinstance(payload, str):
        # Non-JSON text from the tool itself (e.g. "Error: Ticket 123 not found or access denied").
        if _TOOL_RESTRICTION_RE.search(payload):
            raise RestrictedTicketError(f"ticket {ticket_id}: {payload[:200]}")
        raise claude_cli.ToolCallError(f"{tool} returned non-JSON text: {payload[:200]}")
    return _result(payload)


def get_ticket(ticket_id):
    """Ticket metadata, verbatim from Zendesk. Returns None if not found; raises
    RestrictedTicketError on an org-stated restriction (MCP error_message or tags)."""
    t = _call(GET_TICKET_TOOL, {"ticket_id": int(ticket_id)}, ticket_id)
    if not isinstance(t, dict) or not t.get("id"):
        return None
    flagged = restricted_tags(t.get("tags"))
    if flagged:
        raise RestrictedTicketError(f"ticket {ticket_id}: org-restricted tags {flagged}")
    return {
        "id": t.get("id"),
        "subject": t.get("subject") or "",
        "description": t.get("description") or "",
        "status": t.get("status") or "",
        "tags": t.get("tags") or [],
        "custom_fields": t.get("custom_fields") or [],
    }


def get_ticket_comments(ticket_id, limit=20):
    """Comments verbatim from Zendesk, most recent first: [{"id", "body", "public",
    "author", "created_at"}], body = the comment's HTML converted to plain text in Python."""
    r = _call(COMMENTS_TOOL, {"ticket_id": int(ticket_id), "limit": int(limit), "order": "desc"}, ticket_id)
    comments = r.get("comments") if isinstance(r, dict) else r
    if not isinstance(comments, list):
        raise claude_cli.ToolCallError(f"list_ticket_comments for {ticket_id}: unexpected shape")
    out = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        body = c.get("plain_body") or c.get("body") or html_to_text(c.get("html_body") or "")
        out.append({
            "id": c.get("id"),
            "body": body,
            "public": c.get("public"),
            "author": (c.get("author") or {}).get("name", "") if isinstance(c.get("author"), dict) else "",
            "created_at": c.get("created_at", ""),
        })
    # The MCP has been observed answering a restricted ticket with a lone pseudo-comment
    # that IS its error text. Only that exact shape counts; real threads are never scanned.
    if len(out) == 1:
        b = out[0]["body"].strip()
        if b.lower().startswith("error") and _TOOL_RESTRICTION_RE.search(b):
            raise RestrictedTicketError(f"ticket {ticket_id}: {b[:200]}")
    return out


def thread_text(ticket, comments, per_comment=1500):
    """The exact text a model is shown for a ticket — also what its quotes are verified
    against (see grounding.py), so the two can never drift apart."""
    parts = [f"Subject: {ticket.get('subject', '')}", f"Description: {ticket.get('description', '')}"]
    for c in comments:
        parts.append(f"[{c.get('created_at', '')}] {c.get('author', '')}: {(c.get('body') or '')[:per_comment]}")
    return "\n---\n".join(parts)


def _list_my_tickets(limit):
    r = claude_cli.run_tool(LIST_MY_TICKETS_TOOL, {"limit": int(limit)}, deferred=True)
    err = _error_message(r)
    if err:
        raise claude_cli.ToolCallError(f"list_my_tickets: {err}")
    r = _result(r)
    tickets = r.get("tickets") if isinstance(r, dict) else r
    if not isinstance(tickets, list):
        raise claude_cli.ToolCallError("list_my_tickets: unexpected shape")
    return [t for t in tickets if isinstance(t, dict) and t.get("id")]


def list_open_tickets_for_assignee(email, limit=25):
    """Your open/pending/hold tickets (list_my_tickets is always the CLI's own user;
    `email` is kept for display strings elsewhere)."""
    return [{"id": t["id"]} for t in _list_my_tickets(limit)
            if t.get("status") in ("open", "pending", "hold", "new")]


def list_all_tickets_for_assignee(email, limit=100):
    """Your full ticket history, every status (max 100 — list_my_tickets' page cap)."""
    return [{"id": t["id"], "subject": t.get("subject", ""), "status": t.get("status", ""),
             "updated_at": t.get("updated_at", "")} for t in _list_my_tickets(limit)]
