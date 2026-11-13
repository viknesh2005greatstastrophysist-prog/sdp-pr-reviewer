import json
import subprocess

import pytest
from test_parser import DIFF

from pr_reviewer.analyzers import StaticAnalyzer
from pr_reviewer.context import PythonContextExtractor
from pr_reviewer.parser import parse_diff


def test_ast_context_extracts_smallest_symbol(tmp_path):
    (tmp_path / "example.py").write_text("def f():\n    x = 2\n    return x\n", encoding="utf-8")
    files = parse_diff(DIFF)
    assert PythonContextExtractor().enrich(files, tmp_path) == []
    assert files[0].contexts[2].symbol == "f"
    assert "return x" in files[0].contexts[2].source


def test_static_analyzer_filters_to_added_lines(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command[0])
        if command[0] == "ruff":
            data = [
                {
                    "filename": "example.py",
                    "location": {"row": 2},
                    "code": "F841",
                    "message": "unused",
                },
                {
                    "filename": "example.py",
                    "location": {"row": 1},
                    "code": "E501",
                    "message": "old",
                },
            ]
        else:
            data = {
                "results": [
                    {
                        "path": "example.py",
                        "start": {"line": 3},
                        "check_id": "rule",
                        "extra": {"message": "unsafe", "severity": "ERROR"},
                    }
                ]
            }
        return subprocess.CompletedProcess(command, 1, json.dumps(data), "")

    findings, warnings = StaticAnalyzer(runner=runner).analyze(parse_diff(DIFF), tmp_path)
    assert warnings == []
    assert calls == ["ruff", "semgrep"]
    assert {(item.line, item.source) for item in findings} == {(2, "ruff"), (3, "semgrep")}


def test_context_never_escapes_repository(tmp_path):
    files = parse_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x=1\n")
    try:
        (tmp_path / "a.py").symlink_to(tmp_path.parent / "outside.py")
    except OSError:
        pytest.skip("symlinks require elevated privileges on this Windows host")
    assert PythonContextExtractor().enrich(files, tmp_path)
