from pr_reviewer.aggregator import aggregate
from pr_reviewer.models import Finding, Severity
from pr_reviewer.parser import parse_diff

DIFF = """diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -1,2 +1,3 @@
 def f():
-    return 1
+    x = 2
+    return x
"""


def test_line_mapping():
    files = parse_diff(DIFF)
    assert files[0].path == "example.py"
    assert [(x.kind, x.old_line, x.new_line) for x in files[0].lines] == [
        ("CONTEXT", 1, 1),
        ("DELETE", 2, None),
        ("ADD", None, 2),
        ("ADD", None, 3),
    ]


def test_aggregate():
    f = Finding(
        path="a.py",
        line=1,
        category="style",
        severity=Severity.LOW,
        message="issue",
        evidence="test",
        source="test",
    )
    assert len(aggregate([f, f])) == 1


def test_unsafe_path():
    import pytest

    with pytest.raises(ValueError):
        parse_diff("diff --git a/x b/x\n+++ b/../secret\n")


def test_positions_are_github_patch_positions():
    files = parse_diff(DIFF)
    assert files[0].github_position(2) == 4
    assert files[0].github_position(3) == 5


def test_diff_size_limit():
    import pytest

    with pytest.raises(ValueError, match="size limit"):
        parse_diff(DIFF, max_bytes=10)
