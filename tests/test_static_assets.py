"""Corpus, templates, schema conversion and the PII lint: everything that ships as data."""
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from app.catalog import SPECS
from app.config import ROOT
from app.corpus import Corpus
from app.llm import replayable_blocks, strict_schema
from app.pii import find_pii, scrub
from app.sandbox import SandboxLibrary
from app.schemas import GeneratedScenario, GradeResult, SandboxSpec

CORPUS = Corpus(ROOT / "corpus", allow_bootstrap=True)
LIB = SandboxLibrary(ROOT / "sandbox_templates")
META = {"short_id": "tg-00042", "ref": "TG-00042", "service_prefix": "tg-00042"}


# ------------------------------------------------------------------------- schemas

def _walk(node, path="$"):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.mark.parametrize("model", [GeneratedScenario, GradeResult])
def test_strict_schema_closes_every_object(model):
    schema = strict_schema(model)
    objects = [(p, n) for p, n in _walk(schema) if n.get("type") == "object"]
    assert objects
    for path, node in objects:
        assert node["additionalProperties"] is False, path
        assert set(node["required"]) == set(node["properties"]), path
    for path, node in _walk(schema):
        if "$ref" in node:
            assert list(node) == ["$ref"], path


def test_strict_schema_keeps_property_named_title():
    # PublicTicket has no 'title' field, but Customer does; dropping schema titles must not drop it.
    schema = strict_schema(GeneratedScenario)
    assert "title" in schema["$defs"]["Customer"]["properties"]


def test_replayable_blocks_mid_output_fallback():
    blocks = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "partial "},
              {"type": "fallback"}, {"type": "text", "text": "rest"}]
    assert replayable_blocks(blocks) == [{"type": "text", "text": "partial "}, {"type": "text", "text": "rest"}]
    plain = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "x"}]
    assert replayable_blocks(plain) == plain


# -------------------------------------------------------------------------- corpus

def test_corpus_loads_cleanly():
    assert CORPUS.errors == []
    for spec in SPECS:
        assert len(CORPUS.seeds(spec)) >= 5, spec


def test_corpus_ids_unique_and_hints_valid():
    ids = [s.id for spec in SPECS for s in CORPUS.seeds(spec)]
    assert len(ids) == len(set(ids))
    for spec in SPECS:
        for s in CORPUS.seeds(spec):
            assert s.spec == spec
            if s.sandbox_hint:
                assert s.sandbox_hint in LIB.templates, (s.id, s.sandbox_hint)


def test_corpus_has_no_identifiers():
    for path in (ROOT / "corpus").glob("*/*.json"):
        assert find_pii(path.read_text()) == [], path


def test_pick_prefers_difficulty_and_avoids_repeats():
    import random
    rng = random.Random(7)
    picks = [CORPUS.pick("apm", "high", rng=rng)[0] for _ in range(200)]
    share_high = sum("high" in p.difficulty_range for p in picks) / len(picks)
    assert share_high > 0.5
    primary, extras = CORPUS.pick("apm", "low", rng=rng)
    assert extras == []  # one real ticket per scenario, never blended


def test_bootstrap_seeds_are_off_by_default():
    assert Corpus(ROOT / "corpus").counts().get("apm", 0) == sum(
        1 for s in CORPUS.seeds("apm") if s.source == "distilled")


def test_real_complexity_decides_difficulty(tmp_path):
    import shutil
    src = next((ROOT / "corpus" / "apm").glob("*.json"))
    for name, complexity in (("a", "high"), ("b", "low")):
        d = json.loads(src.read_text())
        d.update(id=f"apm-{name}", source="distilled", provenance={"tickets": 1, "csat": "good",
                                                                    "complexity": complexity, "solved_month": "2026-09"})
        (tmp_path / "apm").mkdir(exist_ok=True)
        (tmp_path / "apm" / f"{name}.json").write_text(json.dumps(d))
    c = Corpus(tmp_path)
    assert {c.pick("apm", "high")[0].id for _ in range(20)} == {"apm-a"}


# ------------------------------------------------------------------------ pii lint

def test_pii_lint_and_scrub():
    text = "contact bob@acme.io key 0123456789abcdef0123456789abcdef from 34.12.1.9 re ticket #2938475"
    kinds = {k for k, _ in find_pii(text)}
    assert {"email", "api_key", "public_ipv4", "zendesk_ref"} <= kinds
    assert find_pii(scrub(text)) == []
    assert find_pii("docs at https://docs.datadoghq.com, test 203.0.113.10, 10.0.0.4, user@example.com") == []


# ----------------------------------------------------------------------- templates

def test_templates_load():
    assert LIB.errors == []
    assert {"agent", "apm", "synthetics"} <= {t.spec for t in LIB.templates.values()}


def _variants(t):
    yield "defaults", []
    for p in t.params.values():
        for value in p.choices:
            if value != p.default:
                yield f"{p.name}={value}", [{"name": p.name, "value": value}]
    # everything broken at once
    yield "all-broken", [{"name": p.name, "value": next(v for v in p.choices if v != p.default)}
                         for p in t.params.values() if len(p.choices) > 1]


@pytest.mark.parametrize("tid", sorted(LIB.templates))
def test_every_variant_renders_valid_yaml_and_json(tid):
    t = LIB.templates[tid]
    for label, params in _variants(t):
        files = LIB.render(tid, params, META)
        assert "docker-compose.yml" in files, label
        assert "reset.sh" in files and files["reset.sh"][1], label
        for rel in t.editable:
            assert f".tg/original/{rel}" in files, (label, rel)
        for rel, (data, _) in files.items():
            if rel.endswith((".yml", ".yaml")):
                yaml.safe_load(data)
            elif rel.endswith(".json"):
                json.loads(data)
        compose = yaml.safe_load(files["docker-compose.yml"][0])
        assert compose["name"] == META["short_id"]
        for svc in t.services:
            assert svc in compose["services"], (label, svc)


@pytest.mark.parametrize("tid", sorted(LIB.templates))
def test_broken_values_actually_change_output(tid):
    t = LIB.templates[tid]
    healthy = LIB.render(tid, [], META)
    for p in t.params.values():
        for value in p.choices:
            if value == p.default:
                continue
            broken = LIB.render(tid, [{"name": p.name, "value": value}], META)
            assert broken != healthy, f"{p.name}={value} renders identically to the healthy sandbox"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.parametrize("tid", sorted(LIB.templates))
def test_compose_config_validates(tid, tmp_path):
    files = LIB.render(tid, [], META)
    for rel, (data, _) in files.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (tmp_path / ".env").write_text("DD_API_KEY=dummy\n")
    if tid.startswith("synthetics/"):
        (tmp_path / "config" / "worker-config.json").write_text("{}")
    r = subprocess.run(["docker", "compose", "config", "-q"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_zip_layout_and_permissions():
    data, name = LIB.build_zip("agent/dogstatsd-app", [{"name": "STATSD_HOST", "value": "localhost"}], META, "# readme")
    assert name == "tg-sandbox-tg-00042.zip"
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    root = "tg-sandbox-tg-00042/"
    for f in ("README.md", "docker-compose.yml", "verify.sh", "reset.sh", "config/app.env", ".tg/original/config/app.env"):
        assert root + f in names, f
    mode = zf.getinfo(root + "verify.sh").external_attr >> 16
    assert mode & 0o111
    assert b"STATSD_HOST=localhost" in zf.read(root + "config/app.env")


def test_normalize_clamps_to_allowlist():
    sb = SandboxSpec(needed=True, template_id="apm/multi-service",
                     params=[{"name": "STOREFRONT_AGENT_HOST", "value": "localhost"},
                             {"name": "STOREFRONT_AGENT_HOST", "value": "evil; rm -rf /"},
                             {"name": "NOT_A_PARAM", "value": "x"}],
                     repro_steps=[], rationale="")
    clean, warnings = LIB.normalize("apm", sb)
    assert [p.value for p in clean.params] == ["localhost"]
    assert len(warnings) == 2
    unknown, w = LIB.normalize("apm", sb.model_copy(update={"template_id": "apm/nope"}))
    assert unknown.needed is False and unknown.template_id == "" and w
    wrong_spec, _ = LIB.normalize("agent", sb)
    assert wrong_spec.needed is False


def test_unmatched_real_complexity_refuses(tmp_path):
    src = next((ROOT / "corpus" / "apm").glob("*.json"))
    d = json.loads(src.read_text())
    d.update(id="apm-a", source="distilled", provenance={"tickets": 1, "csat": "good",
                                                        "complexity": "low", "solved_month": "2026-09"})
    (tmp_path / "apm").mkdir()
    (tmp_path / "apm" / "a.json").write_text(json.dumps(d))
    with pytest.raises(LookupError, match="rated 'high'"):
        Corpus(tmp_path).pick("apm", "high")
