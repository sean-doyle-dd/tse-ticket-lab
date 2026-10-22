#!/usr/bin/env python3
"""Build anonymized scenario seeds from recent solved Zendesk tickets. HOST-ONLY.

This runs on a TSE's laptop, never in the container. It uses the local `claude` CLI and
its Snowflake and Zendesk MCP connectors, the same pattern as ticket-memory, so it needs no
API tokens. The running app only ever sees the reviewed output in corpus/.

Pipeline, per spec:
  1. Snowflake picks recent solved tickets for the spec. Compliance exclusions are applied
     server-side: HIPAA orgs, orgs restricted from AI training, orgs that disabled
     Anthropic, FedRAMP regions, and tickets tagged hipaa/opted_out. By default only
     tickets the customer rated good are used, and each must have a TICKET_COMPLEXITY,
     which becomes the seed's difficulty.
  2. The Zendesk MCP fetches each thread verbatim. The MCP enforces its own restrictions.
  3. A regex pass scrubs identifiers, then Claude distills the thread into a generic
     seed with no names, companies, hosts or IDs, including how the TSE worked it.
     Evidence strings and doc links must appear verbatim in the thread, or they're dropped.
     A second Claude pass lists anything else the thread doesn't support; flagged list
     items are removed, and a flagged root cause or resolution rejects the seed.
  4. The PII lint gates the result. Seeds land in corpus/_staging/<spec>/ for human review.
  5. After review, `--promote` moves the approved files into corpus/<spec>/.

Usage:
  python3 tools/refresh_corpus.py --spec apm --days 60 --limit 20
  python3 tools/refresh_corpus.py --spec web-platform --products "dashboards,monitors"
  python3 tools/refresh_corpus.py --promote corpus/_staging/apm/apm-d-1a2b3c.json ...
"""
import argparse
import importlib.util
import json
import shutil
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent / "_vendor"))

import claude_cli  # noqa: E402
import zendesk_lib  # noqa: E402


def _load_pii():
    # Load app/pii.py directly so the host doesn't need Flask installed.
    spec = importlib.util.spec_from_file_location("tg_pii", ROOT / "app" / "pii.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pii = _load_pii()

STAGING = ROOT / "corpus" / "_staging"
STATE = ROOT / ".corpus-state"  # ticket IDs live here: never under corpus/, which the container mounts
LEDGER = STATE / "processed.json"

# Exact PRIMARY_PRODUCT_COMPONENT values per spec (checked against DIM_ZENDESK_TICKET,
# 2026-10-20). Override with --products.
SPEC_PRODUCTS = {
    "agent": ["agent_ppc"],
    "apm": ["apm_ppc"],
    "synthetics": ["synthetics_ppc"],
    "web-platform": ["web_platform_ppc"],
}

ORG_FLAGS_CTE = (
    "WITH f AS (SELECT ORG_ID, HIPAA_COMPLIANCE, DISABLED_GEN_AI_PROVIDERS_LIST, SOURCE_REGION, "
    "LEGAL_AI_TRAINING_RESTRICTED FROM REPORTING.GENERAL.DIM_ORG_AI_FEATURE_FLAGS "
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY ORG_ID ORDER BY PARTITION_DATETIME DESC) = 1) "
)
ORG_ALLOWED_WHERE = (
    "NOT COALESCE(f.HIPAA_COMPLIANCE, FALSE) "
    "AND NOT COALESCE(f.LEGAL_AI_TRAINING_RESTRICTED, FALSE) "
    "AND (f.DISABLED_GEN_AI_PROVIDERS_LIST IS NULL "
    "     OR TRIM(f.DISABLED_GEN_AI_PROVIDERS_LIST) IN ('', '[]') "
    "     OR (NOT CONTAINS(LOWER(f.DISABLED_GEN_AI_PROVIDERS_LIST), 'anthropic') "
    "         AND NOT ARRAY_CONTAINS('all'::variant, "
    "             SPLIT(LOWER(TRIM(f.DISABLED_GEN_AI_PROVIDERS_LIST, '[] ')), ',')))) "
    "AND COALESCE(f.SOURCE_REGION, '') NOT ILIKE '%fed' "
    "AND NOT EXISTS (SELECT 1 FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET_TAG tt "
    "    WHERE tt.TICKET_ID = t.ID AND (LOWER(tt.TAG_KEY) LIKE '%hipaa%' "
    "    OR LOWER(tt.TAG_KEY) LIKE '%opted_out%')) "
)

SEED_KEYS = {
    "topic": str, "product_area": str, "environment": list, "symptom_pattern": str, "root_cause": str,
    "resolution_path": list, "info_needed": list, "qa_pairs": list, "customer_style": dict,
    "sandbox_hint": str, "evidence": list, "doc_links": list, "tse_approach": dict,
}
APPROACH_KEYS = ("summary", "steps", "key_questions", "communication", "customer_feedback")

DISTILL_PROMPT = """You turn one solved Datadog support ticket into an ANONYMOUS training record ("seed")
for a TSE practice tool. The thread below has had obvious identifiers scrubbed, but treat it as sensitive.

Accuracy comes first. Every field must be supported by the thread itself. Never add knowledge of your
own, even when it is true in general. If the thread doesn't say something, leave the field or list
item out. A separate check rejects seeds containing claims the thread doesn't support.

Rules:
- Generalize identities. No customer, company, person, product or project names, no hostnames,
  URLs (except docs.datadoghq.com), IPs, org/ticket/account IDs, keys or exact timestamps.
- Describe environments generically, using only what the thread says ("Kubernetes", "Agent 7.6x").
- root_cause and resolution_path: what ACTUALLY resolved this ticket, as stated in the thread.
- evidence: up to 15 technical strings copied EXACTLY, character for character, from the thread:
  error messages, log lines, config keys/values, commands, versions, UI paths. Only lines with
  no identifiers in them. These are checked against the thread verbatim; any line that isn't an
  exact substring is dropped.
- doc_links: docs.datadoghq.com links that the support engineer actually sent, copied exactly.
- qa_pairs: questions the support engineer actually asked and the gist of how the customer
  answered, paraphrased and with no identifiers.
- tse_approach: how the support engineer worked the ticket:
    summary: 1-2 sentences; steps: what they did, in order; key_questions: the questions or
    requests that moved the ticket forward; communication: concrete things about how they wrote
    (e.g. "explained why the flare was needed", "gave numbered steps with a verification step").
    customer_feedback: a short paraphrase of the CSAT comment below if there is one, else "".
- If the thread never reached a clear resolution, or is not really a {spec} issue, return
  {{"skip": true, "reason": "..."}} instead.
- sandbox_hint: one of {templates} if it could be reproduced with that template, else "".

Return ONLY a JSON object with exactly these keys:
topic, product_area, environment (list), symptom_pattern, root_cause, resolution_path (list),
info_needed (list), qa_pairs (list of {{"tse_question","customer_answer"}}),
customer_style ({{"tone", "technical_level": low|medium|high, "urgency": low|normal|high|urgent}}),
sandbox_hint, evidence (list), doc_links (list),
tse_approach ({{"summary","steps","key_questions","communication","customer_feedback"}}).

CSAT comment from the customer (may be empty):
<<<
{csat_comment}
>>>

Ticket thread:
<<<
{thread}
>>>"""

VERIFY_PROMPT = """You are a strict fact-checker. Below is a support ticket thread and a JSON record that
was distilled from it. List every claim in the record that the thread does NOT support: anything
added from general knowledge, inferred beyond what the thread says, or contradicting it.
Paraphrases and generalized identities (e.g. "a Kubernetes cluster") are fine.

Return ONLY JSON: {{"unsupported": [{{"field": "<top-level field, e.g. root_cause or tse_approach.steps>",
"item": "<the unsupported text, quoted briefly>", "reason": "<why>"}}]}}. Use an empty list if all is supported.

Record:
{seed}

Thread:
<<<
{thread}
>>>"""

# Fields where any unsupported claim rejects the whole seed (they define the answer key).
CORE_FIELDS = ("root_cause", "symptom_pattern", "resolution_path")


def sql_literal(s):
    return "'" + str(s).replace("'", "''") + "'"


def fetch_candidates(products, days, limit, exclude, csat="good", complexity=None):
    product_clause = "AND t.PRIMARY_PRODUCT_COMPONENT IN (" + ", ".join(sql_literal(p) for p in products) + ") "
    csat_clause = ("AND t.SATISFACTION_RATING_SCORE = 'good' " if csat == "good"
                   else "AND COALESCE(t.SATISFACTION_RATING_SCORE, '') <> 'bad' ")
    complexity_clause = (f"AND LOWER(t.TICKET_COMPLEXITY) = {sql_literal(complexity)} " if complexity
                         else "AND LOWER(t.TICKET_COMPLEXITY) IN ('low', 'medium', 'high') ")
    ids = [str(int(i)) for i in exclude if str(i).isdigit()][:5000]
    exclude_clause = f"AND t.ID NOT IN ({', '.join(ids)}) " if ids else ""
    sql = (
        ORG_FLAGS_CTE +
        "SELECT t.ID AS ID, TO_VARCHAR(t.SOLVED_TIMESTAMP) AS SOLVED_AT, "
        "t.SATISFACTION_RATING_SCORE AS CSAT, t.SATISFACTION_RATING_COMMENT AS CSAT_COMMENT, "
        "LOWER(t.TICKET_COMPLEXITY) AS COMPLEXITY "
        "FROM REPORTING.GENERAL.DIM_ZENDESK_TICKET t "
        "LEFT JOIN f ON f.ORG_ID = t.DATADOG_ORG_ID_COALESCE "
        "WHERE t.STATUS IN ('solved', 'closed') AND t.SOLVED_TIMESTAMP IS NOT NULL "
        f"AND t.SOLVED_TIMESTAMP >= DATEADD(day, -{int(days)}, CURRENT_TIMESTAMP()) "
        + csat_clause + complexity_clause + product_clause + exclude_clause + "AND " + ORG_ALLOWED_WHERE +
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY t.ID ORDER BY t.SOLVED_TIMESTAMP DESC) = 1 "
        "ORDER BY IFF(t.SATISFACTION_RATING_SCORE = 'good', 0, 1), COALESCE(t.REOPEN_COUNT, 0), "
        f"t.SOLVED_TIMESTAMP DESC LIMIT {int(limit)}"
    )
    rows = claude_cli.run_tool("mcp__snowflake__run_snowflake_query", {"statement": sql},
                               deferred=True, per_attempt_timeout=240)
    if isinstance(rows, dict) and isinstance(rows.get("result"), list):
        rows = rows["result"]
    if not isinstance(rows, list):
        raise SystemExit(f"Unexpected Snowflake result: {str(rows)[:200]}")
    return [{"id": str(int(r["ID"])), "solved_at": (r.get("SOLVED_AT") or "")[:10], "csat": (r.get("CSAT") or ""),
             "csat_comment": r.get("CSAT_COMMENT") or "", "complexity": (r.get("COMPLEXITY") or "")}
            for r in rows if str(r.get("ID", "")).isdigit()]


def valid_seed(d):
    problems = [k for k, typ in SEED_KEYS.items() if not isinstance(d.get(k), typ)]
    style = d.get("customer_style") or {}
    if style.get("technical_level") not in ("low", "medium", "high") or style.get("urgency") not in ("low", "normal", "high", "urgent"):
        problems.append("customer_style")
    return problems


def _as_type(value, typ):
    """Coerce the model's loose output: a string where a list belongs becomes [string]."""
    if typ is list:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return [str(value)] if isinstance(value, str) and value.strip() else []
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value else ""


def _norm(text):
    return " ".join(str(text).split())


def verbatim_only(items, thread):
    """Keep only strings that appear verbatim (modulo whitespace) in the scrubbed thread."""
    hay = _norm(thread)
    return [str(e) for e in items if str(e).strip() and _norm(e) in hay]


def drop_unsupported(seed, unsupported):
    """Remove flagged list items. Returns the problems that sink the whole seed."""
    fatal = []
    for u in unsupported:
        field, item = str(u.get("field", "")), _norm(u.get("item", "")).lower()
        top, _, sub = field.partition(".")
        if top in CORE_FIELDS and not isinstance(seed.get(top), list):
            fatal.append(u)
            continue
        target = seed.get(top)
        if sub and isinstance(target, dict):
            target = target.get(sub)
        if isinstance(target, list):
            keep = [x for x in target if not item or item not in _norm(json.dumps(x)).lower()]
            if top in CORE_FIELDS and len(keep) < len(target):
                fatal.append(u)
            target[:] = keep
        elif isinstance(target, str) and sub:
            seed[top][sub] = ""
        else:
            fatal.append(u)
    return fatal


def fetch_comments(ticket_id, limits=(30, 15, 8)):
    """Most recent comments first. Long threads overflow the MCP's response cap, so step
    the page down; the newest comments hold the resolution and the description carries
    the original problem."""
    for limit in limits:
        try:
            return zendesk_lib.get_ticket_comments(ticket_id, limit=limit)
        except claude_cli.ToolCallError as exc:
            too_big = any(m in str(exc) for m in ("exceeds maximum allowed tokens", "<persisted-output>"))
            if not too_big or limit == limits[-1]:
                raise


def distill(spec, cand, templates):
    ticket_id = cand["id"]
    ticket = zendesk_lib.get_ticket(ticket_id)
    if ticket is None:
        return None, "not found"
    comments = fetch_comments(ticket_id)
    thread = pii.scrub(zendesk_lib.thread_text(ticket, list(reversed(comments)), per_comment=2500))[:60000]
    csat_comment = pii.scrub(cand.get("csat_comment") or "")[:2000]
    prompt = DISTILL_PROMPT.format(spec=spec, templates=templates or '[""]', thread=thread, csat_comment=csat_comment)
    out = claude_cli.run_claude_json(prompt, model="claude-sonnet-5-5", per_attempt_timeout=240)
    if not isinstance(out, dict):
        return None, "model returned non-object"
    if out.get("skip"):
        return None, f"skipped by model: {out.get('reason', '')}"
    problems = valid_seed(out)
    if problems:
        return None, f"invalid seed fields: {problems}"

    approach = out["tse_approach"]
    seed = {"id": f"{spec}-d-{uuid.uuid4().hex[:8]}", "spec": spec, "source": "distilled",
            "recency": cand["solved_at"] or date.today().isoformat(),
            **{k: out[k] for k in SEED_KEYS if k != "tse_approach"},
            "difficulty_range": [cand["complexity"]] if cand["complexity"] in ("low", "medium", "high") else ["medium"],
            "tse_approach": {k: _as_type(approach.get(k), str if k in ("summary", "customer_feedback") else list)
                             for k in APPROACH_KEYS},
            "provenance": {"tickets": 1, "csat": cand["csat"] if cand["csat"] in ("good", "offered", "unoffered", "bad") else "",
                           "complexity": cand["complexity"] if cand["complexity"] in ("low", "medium", "high") else "",
                           "solved_month": (cand["solved_at"] or "")[:7]}}
    seed["qa_pairs"] = [{"tse_question": str(q.get("tse_question", "")), "customer_answer": str(q.get("customer_answer", ""))}
                        for q in seed["qa_pairs"] if isinstance(q, dict)]
    seed["customer_style"] = {k: seed["customer_style"].get(k, "") for k in ("tone", "technical_level", "urgency")}

    # Deterministic grounding: evidence and links must literally appear in the thread.
    seed["evidence"] = verbatim_only(seed["evidence"], thread)
    seed["doc_links"] = [u for u in verbatim_only(seed["doc_links"], thread) if u.startswith("https://docs.datadoghq.com/")]

    # Model grounding: a second pass lists anything the thread doesn't support.
    # Only the claims about the ticket are checked; our own metadata (provenance, ids) and the
    # inferred tone/sandbox hint aren't statements the thread could support.
    claims = {k: v for k, v in seed.items() if k not in ("id", "spec", "source", "recency", "provenance",
                                                         "customer_style", "sandbox_hint", "difficulty_range")}
    check = claude_cli.run_claude_json(VERIFY_PROMPT.format(seed=json.dumps(claims, indent=2), thread=thread),
                                       model="claude-sonnet-5-5", per_attempt_timeout=240)
    unsupported = check.get("unsupported", []) if isinstance(check, dict) else None
    if unsupported is None:
        return None, "grounding check returned no verdict"
    fatal = drop_unsupported(seed, [u for u in unsupported if isinstance(u, dict)])
    if fatal:
        return None, f"grounding check failed on {sorted({f.get('field') for f in fatal})}"
    if not seed["resolution_path"] or not seed["root_cause"].strip():
        return None, "no supported resolution left after grounding check"

    hits = pii.find_pii(json.dumps(seed))
    if hits:
        return None, f"PII lint failed: {hits[:3]}"
    return seed, f"ok ({len(unsupported)} unsupported item(s) removed)"


def promote(paths):
    for p in map(Path, paths):
        seed = json.loads(p.read_text())
        hits = pii.find_pii(p.read_text())
        if hits:
            print(f"✗ {p}: PII lint failed {hits[:3]}, not promoted")
            continue
        dest = ROOT / "corpus" / seed["spec"] / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), dest)
        print(f"✓ {dest.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", choices=sorted(SPEC_PRODUCTS))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--products", help="comma-separated PRIMARY_PRODUCT_COMPONENT values (overrides the default map)")
    ap.add_argument("--csat", choices=("good", "any"), default="good",
                    help="good (default): only tickets the customer rated good; any: also unrated")
    ap.add_argument("--complexity", choices=("low", "medium", "high"), help="only tickets with this TICKET_COMPLEXITY")
    ap.add_argument("--promote", nargs="+", metavar="STAGED_JSON", help="move reviewed staged seeds into corpus/")
    args = ap.parse_args()

    if args.promote:
        return promote(args.promote)
    if not args.spec:
        ap.error("--spec is required (or use --promote)")
    if not claude_cli.configured():
        raise SystemExit("The `claude` CLI isn't on PATH. Run this on a machine with Claude Code installed and logged in.")

    products = [p.strip() for p in args.products.split(",")] if args.products else SPEC_PRODUCTS[args.spec]
    templates = sorted(f"{args.spec}/{d.name}" for d in (ROOT / "sandbox_templates" / args.spec).glob("*") if d.is_dir()) \
        if (ROOT / "sandbox_templates" / args.spec).is_dir() else []
    STAGING.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(exist_ok=True)
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else []

    print(f"→ Selecting up to {args.limit} solved {args.spec} tickets from the last {args.days} days (compliance-filtered)…")
    candidates = fetch_candidates(products, args.days, args.limit, ledger, args.csat, args.complexity)
    print(f"  {len(candidates)} candidates")
    out_dir = STAGING / args.spec
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for cand in candidates:
        tid = cand["id"]
        if LEDGER.exists() and tid in json.loads(LEDGER.read_text()):
            continue  # a parallel run took it after we selected
        claim = STATE / f"claim-{tid}"
        try:
            claim.touch(exist_ok=False)  # atomic: only one parallel run distills a ticket
        except FileExistsError:
            continue
        try:
            seed, why = distill(args.spec, cand, templates)
        except zendesk_lib.RestrictedTicketError:
            seed, why = None, "restricted by org policy"
        except claude_cli.ClaudeCliError as exc:
            seed, why = None, f"CLI error: {exc}"
        # Merge with what's on disk: several specs can run in parallel against one ledger.
        on_disk = json.loads(LEDGER.read_text()) if LEDGER.exists() else []
        LEDGER.write_text(json.dumps(sorted(set(on_disk) | {tid})))
        claim.unlink(missing_ok=True)
        if seed is None:
            print(f"  · skipped one ticket: {why}")  # never print ticket ids or content
            continue
        path = out_dir / f"{seed['id']}.json"
        path.write_text(json.dumps(seed, indent=2) + "\n")
        written += 1
        print(f"  ✓ {path.relative_to(ROOT)}: {seed['topic']} [{seed['provenance']['complexity']}, "
              f"CSAT {seed['provenance']['csat'] or 'n/a'}] {why}")
    print(f"\n{written} seed(s) staged in {out_dir.relative_to(ROOT)}/.")
    print("Review each one for accuracy and anonymity, then run:\n  python3 tools/refresh_corpus.py --promote corpus/_staging/"
          f"{args.spec}/<file>.json …")


if __name__ == "__main__":
    main()
