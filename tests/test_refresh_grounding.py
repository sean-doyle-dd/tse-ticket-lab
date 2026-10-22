import importlib.util
import sys

import pytest

from app.config import ROOT

TOOL = ROOT / "tools" / "refresh_corpus.py"
pytestmark = pytest.mark.skipif(not TOOL.exists(), reason="tools/ is host-only (not in the image)")


@pytest.fixture(scope="module")
def rc():
    sys.path.insert(0, str(ROOT / "tools" / "_vendor"))
    spec = importlib.util.spec_from_file_location("refresh_corpus", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_evidence_must_be_verbatim(rc):
    thread = "TSE: please run it\nCustomer: got  failed to send traces: connection refused\nthen nothing"
    kept = rc.verbatim_only(["failed to send traces: connection refused", "DD_TRACE_DEBUG=true", ""], thread)
    assert kept == ["failed to send traces: connection refused"]


def test_unsupported_list_items_dropped_core_fields_fatal(rc):
    seed = {"root_cause": "x", "resolution_path": ["set A", "set B"], "info_needed": ["flare", "version"],
            "tse_approach": {"steps": ["asked for flare", "guessed"], "summary": "s"}}
    fatal = rc.drop_unsupported(seed, [{"field": "info_needed", "item": "version"},
                                       {"field": "tse_approach.steps", "item": "guessed"},
                                       {"field": "tse_approach.summary", "item": "s"}])
    assert not fatal
    assert seed["info_needed"] == ["flare"] and seed["tse_approach"]["steps"] == ["asked for flare"]
    assert seed["tse_approach"]["summary"] == ""
    assert rc.drop_unsupported(seed, [{"field": "root_cause", "item": "x"}])
    assert rc.drop_unsupported(seed, [{"field": "resolution_path", "item": "set B"}])
