"""The three Claude-backed jobs: generate a scenario, play the customer, grade the TSE.

These are pure functions over plain data (dicts, pydantic models, and an `LLM`), with
no Flask or DB access, so they're easy to test with a fake LLM. The routes layer
persists results and records token usage.
"""
import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .catalog import DIFFICULTIES, SPECS
from .llm import LLMError, drain, to_json
from .schemas import DisclosureCheck, GeneratedScenario, GradeResult, GroundingCheck

PROMPTS = Environment(
    loader=FileSystemLoader(str(Path(__file__).parent / "prompts")),
    undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False,
)

RESOLUTION_DIRECTIVE = (
    "The engineer has just marked the reply above as their proposed resolution. Imagine "
    "applying exactly what they suggested. Judge it against the real root cause: if it "
    "would fix the issue, confirm that it worked and describe what you now see. If it "
    "wouldn't, report that it didn't help and what you still observe. Stay in character."
)


def _render(name, **kw):
    return PROMPTS.get_template(name).render(**kw)


def _seed_json(seed):
    # Drop bookkeeping the model doesn't need, and keep the order stable for caching.
    data = seed.model_dump(exclude={"source", "id"})
    return json.dumps(data, indent=2, sort_keys=True)


# ------------------------------------------------------------------------- generation

def generate_scenario(llm, cfg, corpus, sandboxes, spec, difficulty, avoid_seed_ids=(), rng=None):
    """Build a scenario from ONE real ticket's seed, then check it against that seed.

    Returns (GeneratedScenario, seed_ids, warnings, usages, grounding) where usages is a
    list of (purpose, Usage) and grounding is the verifier's report. Raises LLMError when
    the scenario still contains unsupported technical claims after one retry."""
    primary, _ = corpus.pick(spec, difficulty, avoid_seed_ids, rng=rng)
    catalog = sandboxes.catalog_text(spec)
    system = _render("generator_system.md.j2", spec_label=SPECS[spec]["label"],
                     spec_blurb=SPECS[spec]["blurb"], catalog=catalog)
    user = _render("generator_user.md.j2", difficulty_label=DIFFICULTIES[difficulty]["label"],
                   spec_label=SPECS[spec]["label"], primary=_seed_json(primary),
                   today=date.today().isoformat())
    usages, warnings, problems, previous = [], [], [], None
    attempts = max(1, cfg.get("GROUNDING_ATTEMPTS", 3)) if cfg.get("GROUNDING_CHECK", True) else 1
    for attempt in range(1, attempts + 1):
        # A retry edits the rejected scenario rather than starting over, so it converges
        # instead of trading one unsupported detail for another.
        prompt = user if not problems else user + "\n\n" + _render(
            "generator_retry.md.j2", problems=problems, previous=json.dumps(to_json(previous), indent=2))
        scenario, usage = llm.structured(cfg["MODEL_GENERATOR"], system, [{"role": "user", "content": prompt}],
                                         GeneratedScenario, effort=cfg["EFFORT_GENERATOR"])
        usages.append(("generate", usage))
        scenario, warnings = _normalize(scenario, primary, sandboxes, spec)
        if not cfg.get("GROUNDING_CHECK", True):
            return scenario, [primary.id], warnings, usages, {"checked": False}
        previous = scenario
        problems, vusage = check_grounding(llm, cfg, primary, catalog, scenario)
        usages.append(("verify", vusage))
        if not problems:
            return scenario, [primary.id], warnings, usages, {"checked": True, "attempts": attempt,
                                                              "unsupported": []}
    err = LLMError("Claude couldn't write this ticket using only facts from the source ticket "
                   f"({len(problems)} unsupported detail(s) remained after {attempts} attempts), so it was "
                   "discarded. Generate again.", 502)
    err.usages, err.problems = usages, problems
    raise err


def _normalize(scenario, seed, sandboxes, spec):
    sandbox, warnings = sandboxes.normalize(spec, scenario.sandbox)
    scenario = scenario.model_copy(update={"sandbox": sandbox})
    # Fact ids must be unique, or disclosure tracking gets ambiguous.
    seen, facts = set(), []
    for i, f in enumerate(scenario.truth.facts, 1):
        fid = f.id if f.id and f.id not in seen else f"f{i}"
        seen.add(fid)
        facts.append(f.model_copy(update={"id": fid}))
    scenario.truth.facts = facts
    # Doc links come only from what the real TSE sent; anything else is dropped.
    allowed = set(seed.doc_links)
    dropped = [u for u in scenario.truth.doc_links if u not in allowed]
    if dropped:
        warnings.append(f"dropped {len(dropped)} doc link(s) not in the source ticket")
    scenario.truth.doc_links = [u for u in scenario.truth.doc_links if u in allowed]
    return scenario, warnings


def source_snapshot(seed):
    """The real-ticket material a scenario was built from, stored with the scenario."""
    if seed is None:
        return {}
    return {
        "seed_id": seed.id, "kind": seed.source, "provenance": seed.provenance.model_dump(),
        "symptom_pattern": seed.symptom_pattern, "root_cause": seed.root_cause,
        "resolution_path": seed.resolution_path, "info_needed": seed.info_needed,
        "qa_pairs": [q.model_dump() for q in seed.qa_pairs], "evidence": seed.evidence,
        "doc_links": seed.doc_links, "tse_approach": seed.tse_approach.model_dump(),
    }


def check_grounding(llm, cfg, seed, catalog, scenario):
    """Ask a separate model to list every technical claim the seed (and, for sandbox
    behaviour, the template catalog) doesn't support. Returns (problems, usage)."""
    data = to_json(scenario)
    data["sandbox"].pop("rationale", None)  # maintainer note, never shown to the TSE
    user = _render("verifier_user.md.j2", seed=_seed_json(seed), catalog=catalog,
                   scenario=json.dumps(data, indent=2))
    result, usage = llm.structured(cfg["MODEL_VERIFIER"], _render("verifier_system.md"),
                                   [{"role": "user", "content": user}], GroundingCheck,
                                   effort=cfg["EFFORT_VERIFIER"], max_tokens=16000)
    return [c.model_dump() for c in result.unsupported], usage


# ----------------------------------------------------------------------- customer chat

def customer_system_prompt(scenario_row, corpus=None):
    """Built only from data that never changes after generation, so it stays byte-stable
    and is read from the prompt cache on every turn after the first."""
    t, truth = scenario_row["ticket"], scenario_row["truth"]
    style = ""
    if corpus is not None and scenario_row.get("seed_ids"):
        seed = corpus.get(scenario_row["seed_ids"][0])
        if seed:
            s = seed.customer_style
            style = f"{s.tone}; urgency {s.urgency}"
    return _render("customer_system.md.j2", c=t["customer"], t=t, truth=truth,
                   difficulty=scenario_row["difficulty"], style=style)


def build_customer_messages(history, mid_conversation_system=True):
    """history: list of dicts {role: tse|customer|system, kind, content, api_blocks}.
    Returns API messages. TSE -> user, customer -> assistant (original blocks replayed
    unchanged), system -> mid-conversation system message (or folded into the preceding
    user turn on providers that don't support them). Consecutive user turns are merged."""
    out = []
    for i, m in enumerate(history):
        role = m["role"]
        if role == "tse":
            text = m["content"]
            if m.get("kind") == "resolution":
                text = "[Proposed resolution]\n" + text
            if out and out[-1]["role"] == "user":
                out[-1]["content"].append({"type": "text", "text": text})
            else:
                out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        elif role == "customer":
            blocks = m.get("api_blocks") or [{"type": "text", "text": m["content"]}]
            out.append({"role": "assistant", "content": blocks})
        elif role == "system":
            # A system message must be last or followed by an assistant turn. If the reply
            # to it never arrived (stream failed) and the TSE wrote again, fold it instead.
            answered_next = i + 1 >= len(history) or history[i + 1]["role"] == "customer"
            if mid_conversation_system and answered_next and out and out[-1]["role"] == "user":
                out.append({"role": "system", "content": m["content"]})
            elif out and out[-1]["role"] == "user":
                out[-1]["content"].append({"type": "text", "text": f"(Context: {m['content']})"})
    return out


def customer_reply_stream(llm, cfg, system, history):
    """Generator of text chunks; its return value is the llm StreamResult."""
    messages = build_customer_messages(history, llm.supports_mid_conversation_system)
    return llm.stream_text(cfg["MODEL_CUSTOMER"], system, messages, effort=cfg["EFFORT_CUSTOMER"])


def classify_disclosure(llm, cfg, truth, reply_text, ticket_text=""):
    """Return (fact ids this customer reply disclosed, ungrounded technical details, usage)."""
    facts = truth.get("facts") or []
    if not facts or not reply_text.strip():
        return [], [], None
    listing = "\n".join(f"- {f['id']}: {f['statement']}" for f in facts)
    src = truth.get("source") or {}
    material = "\n".join(f"- {e}" for e in src.get("evidence", [])) or "(none)"
    qa = "\n".join(f"- {q['tse_question']} -> {q['customer_answer']}" for q in src.get("qa_pairs", [])) or "(none)"
    user = (f"Hidden facts:\n{listing}\n\nRoot cause: {truth.get('root_cause', '')}\n\n"
            f"The ticket as the customer opened it:\n<<<\n{ticket_text}\n>>>\n\n"
            f"Real case material. Symptom: {src.get('symptom_pattern', '')}\n"
            f"Environment: {'; '.join(src.get('environment', []))}\n"
            f"Strings copied from the source ticket:\n{material}\nReal Q&A:\n{qa}\n\n"
            f"Customer reply:\n<<<\n{reply_text}\n>>>")
    result, usage = llm.structured(
        cfg["MODEL_CLASSIFIER"], _render("classifier_system.md"),
        [{"role": "user", "content": user}], DisclosureCheck,
        effort=cfg["EFFORT_CLASSIFIER"], max_tokens=4000,
    )
    valid = {f["id"] for f in facts}
    return [fid for fid in result.revealed_fact_ids if fid in valid], result.ungrounded_details, usage


# ---------------------------------------------------------------------------- grading

WEIGHTS = {"diagnosis": 0.3, "information_gathering": 0.25, "communication": 0.2, "resolution": 0.25}


def overall_score(grade: GradeResult):
    total = 0.0
    for key, w in WEIGHTS.items():
        score = min(5, max(1, getattr(grade, key).score))
        total += w * (score - 1) / 4
    return round(total * 100)


def grade_scenario(llm, cfg, scenario_row, history):
    """Returns (GradeResult, overall 0-100, usage)."""
    t, truth = scenario_row["ticket"], scenario_row["truth"]
    transcript = [
        {"who": "TSE" if m["role"] == "tse" else "Customer", "kind": m.get("kind"), "content": m["content"]}
        for m in history if m["role"] in ("tse", "customer")
    ]
    user = _render(
        "grader_user.md.j2",
        spec_label=SPECS.get(scenario_row["spec"], {}).get("label", scenario_row["spec"]),
        difficulty=scenario_row["difficulty"], t=t,
        truth_json=json.dumps({k: v for k, v in truth.items() if k not in ("source", "grounding")}, indent=2),
        revealed=scenario_row.get("revealed_fact_ids") or [],
        total_facts=len(truth.get("facts") or []),
        tse_turns=sum(1 for m in transcript if m["who"] == "TSE"),
        transcript=transcript,
        source=truth.get("source") or {},
        ungrounded=scenario_row.get("ungrounded") or [],
    )
    grade, usage = llm.structured(
        cfg["MODEL_GRADER"], _render("grader_system.md"), [{"role": "user", "content": user}],
        GradeResult, effort=cfg["EFFORT_GRADER"],
    )
    if not any(m["who"] == "TSE" for m in transcript):
        # Nothing to grade: closing without replying is a zero, whatever the model says.
        for key in WEIGHTS:
            getattr(grade, key).score = 1
        grade.resolved = False
    return grade, overall_score(grade), usage


__all__ = [
    "generate_scenario", "customer_system_prompt", "build_customer_messages",
    "customer_reply_stream", "classify_disclosure", "grade_scenario", "overall_score",
    "RESOLUTION_DIRECTIVE", "drain",
]
