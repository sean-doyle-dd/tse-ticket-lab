from app import engine
from app.config import ROOT
from app.corpus import Corpus
from app.sandbox import SandboxLibrary

from .conftest import FakeLLM, sample_grade, sample_scenario

CFG = {"MODEL_GENERATOR": "g", "MODEL_CUSTOMER": "c", "MODEL_GRADER": "r", "MODEL_CLASSIFIER": "k",
       "EFFORT_GENERATOR": "high", "EFFORT_CUSTOMER": "medium", "EFFORT_GRADER": "high", "EFFORT_CLASSIFIER": "low",
       "MODEL_VERIFIER": "v", "EFFORT_VERIFIER": "medium", "GROUNDING_CHECK": True}


def test_generate_scenario_prompt_and_normalization():
    llm = FakeLLM()
    llm.scenario = sample_scenario(params=[{"name": "STOREFRONT_AGENT_HOST", "value": "localhost"},
                                           {"name": "BOGUS", "value": "1"}])
    corpus, lib = Corpus(ROOT / "corpus", allow_bootstrap=True), SandboxLibrary(ROOT / "sandbox_templates")
    scenario, seed_ids, warnings, usages, grounding = engine.generate_scenario(llm, CFG, corpus, lib, "apm", "low")
    _, schema_name, system, messages = llm.calls[0]
    assert schema_name == "GeneratedScenario"
    assert "template_id: apm/multi-service" in system and "Difficulty rubric" in system
    assert "Source seed" in messages[0]["content"]
    assert [c[1] for c in llm.calls] == ["GeneratedScenario", "GroundingCheck"]
    assert grounding == {"checked": True, "attempts": 1, "unsupported": []}
    assert [p for p, _ in usages] == ["generate", "verify"]
    assert [p.name for p in scenario.sandbox.params] == ["STOREFRONT_AGENT_HOST"]
    assert warnings
    ids = [f.id for f in scenario.truth.facts]
    assert len(ids) == len(set(ids)), "duplicate fact ids must be made unique"
    assert len(seed_ids) == 1 and corpus.get(seed_ids[0])


def test_ungrounded_scenario_is_retried_then_rejected():
    import pytest
    from app.llm import LLMError
    corpus, lib = Corpus(ROOT / "corpus", allow_bootstrap=True), SandboxLibrary(ROOT / "sandbox_templates")
    bad = [{"where": "ticket.body", "claim": "DD_MAGIC=1", "reason": "not in seed"}]

    llm = FakeLLM()
    llm.grounding = [bad, []]  # first attempt flagged, retry clean
    *_, grounding = engine.generate_scenario(llm, CFG, corpus, lib, "apm", "low")
    assert grounding["attempts"] == 2
    retry_prompt = llm.calls[2][3][0]["content"]
    assert "Fix your previous attempt" in retry_prompt and "DD_MAGIC=1" in retry_prompt
    assert "Storefront traces missing since deploy" in retry_prompt  # edits, doesn't restart

    llm = FakeLLM()
    llm.grounding = [bad, bad, bad]
    with pytest.raises(LLMError) as exc:
        engine.generate_scenario(llm, CFG, corpus, lib, "apm", "low")
    assert len(exc.value.usages) == 6 and exc.value.problems == bad


def test_doc_links_not_in_seed_are_dropped():
    corpus, lib = Corpus(ROOT / "corpus", allow_bootstrap=True), SandboxLibrary(ROOT / "sandbox_templates")
    llm = FakeLLM()
    llm.scenario.truth.doc_links = ["https://docs.datadoghq.com/made-up/"]
    scenario, *_ = engine.generate_scenario(llm, CFG, corpus, lib, "apm", "low")
    assert scenario.truth.doc_links == []


def test_generator_system_prompt_is_stable_per_spec():
    lib = SandboxLibrary(ROOT / "sandbox_templates")
    a = engine._render("generator_system.md.j2", spec_label="APM", spec_blurb="x", catalog=lib.catalog_text("apm"))
    b = engine._render("generator_system.md.j2", spec_label="APM", spec_blurb="x", catalog=lib.catalog_text("apm"))
    assert a == b  # byte-stable, so it caches across generations


def test_customer_prompt_contains_facts_but_is_stable():
    s = sample_scenario().model_dump()
    row = {"ticket": s["ticket"], "truth": s["truth"], "difficulty": "high", "spec": "apm", "seed_ids": []}
    p1 = engine.customer_system_prompt(row)
    assert "[f1]" in p1 and "Reveal when" in p1 and "Push back" in p1
    assert p1 == engine.customer_system_prompt(row)


def test_build_customer_messages_roles_and_merging():
    history = [
        {"role": "tse", "kind": "reply", "content": "Hi"},
        {"role": "tse", "kind": "reply", "content": "Also, which version?"},
        {"role": "customer", "kind": "reply", "content": "7.5", "api_blocks": [{"type": "text", "text": "7.5"}]},
        {"role": "tse", "kind": "resolution", "content": "Set DD_AGENT_HOST"},
        {"role": "system", "kind": "directive", "content": "judge it"},
        {"role": "customer", "kind": "reply", "content": "Worked", "api_blocks": None},
    ]
    msgs = engine.build_customer_messages(history)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "system", "assistant"]
    assert len(msgs[0]["content"]) == 2
    assert msgs[2]["content"][0]["text"].startswith("[Proposed resolution]")
    assert msgs[4]["content"] == [{"type": "text", "text": "Worked"}]


def test_unanswered_directive_is_folded():
    history = [
        {"role": "tse", "kind": "resolution", "content": "Try X"},
        {"role": "system", "kind": "directive", "content": "judge it"},
        {"role": "tse", "kind": "reply", "content": "Any luck?"},  # reply to the directive never arrived
    ]
    msgs = engine.build_customer_messages(history)
    assert [m["role"] for m in msgs] == ["user"]
    assert any("judge it" in c["text"] for c in msgs[0]["content"])
    no_mid = engine.build_customer_messages(history[:2], mid_conversation_system=False)
    assert [m["role"] for m in no_mid] == ["user"]


def test_classify_filters_unknown_ids():
    llm = FakeLLM()
    truth = sample_scenario().truth.model_dump()
    truth["facts"][1]["id"] = "f2"
    ids, ungrounded, _ = engine.classify_disclosure(llm, CFG, truth, "some reply")
    assert ids == ["f2"]
    assert engine.classify_disclosure(llm, CFG, truth, "   ")[0] == []


def test_overall_score_bounds():
    assert engine.overall_score(sample_grade(5)) == 100
    assert engine.overall_score(sample_grade(1)) == 0
    assert engine.overall_score(sample_grade(3)) == 50


def test_grade_without_tse_replies_is_zero():
    llm = FakeLLM()
    s = sample_scenario().model_dump()
    row = {"ticket": s["ticket"], "truth": s["truth"], "difficulty": "low", "spec": "apm", "revealed_fact_ids": []}
    grade, overall, _ = engine.grade_scenario(llm, CFG, row, [])
    assert overall == 0 and grade.resolved is False
    grade, overall, _ = engine.grade_scenario(llm, CFG, row, [{"role": "tse", "kind": "reply", "content": "hi"}])
    assert overall == 75
    assert "TSE" in llm.calls[-1][3][0]["content"]
