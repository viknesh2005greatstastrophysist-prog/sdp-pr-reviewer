import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_suggestions.py"


def _load():
    spec = importlib.util.spec_from_file_location("score_suggestions", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_wilson_and_apply():
    m = _load()
    assert m.wilson(0, 0) is None
    lo, hi = m.wilson(5, 10)
    assert 0.23 < lo < 0.25 and 0.75 < hi < 0.77
    src = "a = 1\nb = 2\nc = 3\n"
    patched, new = m.apply_suggestion(src, 2, 2, "b = 20")
    assert patched == "a = 1\nb = 20\nc = 3\n" and new == ["b = 20"]
    assert m.apply_suggestion(src, 3, 5, "x")[0] is None
    assert m.apply_suggestion(src, 2, 3, "")[0] == "a = 1\n"
    assert m.classify_source({"ruff"}) == "static"
    assert m.classify_source({"generalist+ruff"}) == "agent"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
def test_score_one_injected_restores_original(tmp_path):
    m = _load()
    case = "inj__bug-hunter__01__demo"
    f = tmp_path / "evaluation" / "injected" / case / "pkg" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("def f(x):\n    if x != 1:\n        return 1\n    return 2\n")
    row = {
        "case_id": case,
        "metadata": {
            "injected_line": 2,
            "source_file": "pkg/mod.py",
            "line_before": "if x == 1:",
            "line_after": "if x != 1:",
        },
    }
    u = {
        "case_id": case,
        "path": "pkg/mod.py",
        "start_line": 2,
        "end_line": 2,
        "replacement": "    if x == 1:",
        "finding_line": 2,
        "sources": {"generalist"},
        "rule_ids": set(),
        "seen_in": set(),
        "benchmark": "injected",
        "source_class": "agent",
    }
    rec = m.score_one(u, tmp_path, {case: row})
    assert rec["applies"] and rec["whole_file_parses"]
    assert rec["hits_defect"] and rec["restores_original"] and rec["changes_defect_line"]
    assert rec["no_collateral"] is True
    bad = dict(u, replacement="    if undefined_name:")
    rec = m.score_one(bad, tmp_path, {case: row})
    assert rec["restores_original"] is False and rec["no_collateral"] is False
    json.dumps(rec)
