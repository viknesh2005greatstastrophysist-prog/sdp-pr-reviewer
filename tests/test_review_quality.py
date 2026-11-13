"""Defects found by looking at the redesigned dashboard (13 Nov): duplicate eval findings
from three sources, a flush-left 'validated' suggestion, Ruff S-rules labelled as style."""

from pr_reviewer.aggregator import aggregate
from pr_reviewer.analyzers import static_category
from pr_reviewer.models import Finding, Severity, Suggestion
from pr_reviewer.parser import parse_diff
from pr_reviewer.refactor import RefactoringEngine

DIFF = """diff --git a/app.py b/app.py
new file mode 100644
--- /dev/null
+++ b/app.py
@@ -0,0 +1,3 @@
+def evaluate(expression: str):
+    value = int(expression)
+    return eval(expression)
"""


def finding(source, title, message, line=3, severity=Severity.HIGH, rule=None):
    return Finding(
        path="app.py",
        line=line,
        category="bug-hunter",
        severity=severity,
        title=title,
        message=message,
        evidence="evidence",
        source=source,
        rule_id=rule,
    )


def test_one_problem_reported_by_three_tools_is_one_finding():
    files = parse_diff(DIFF)
    merged = aggregate(
        [
            finding("bug-hunter", "Dynamic evaluation of input", "Replace eval with a parser"),
            finding("semgrep", "python-dangerous-eval", "Avoid eval on untrusted data"),
            finding("ruff", "ruff: S307", "Consider using ast.literal_eval", rule="S307"),
        ],
        files,
    )
    assert len(merged) == 1
    assert set(merged[0].source.split("+")) == {"bug-hunter", "semgrep", "ruff"}


def test_different_problems_on_one_line_stay_separate():
    files = parse_diff(DIFF)
    merged = aggregate(
        [
            finding("bug-hunter", "Dynamic evaluation of input", "Replace eval with a parser"),
            finding("ruff", "ruff: E501", "Line too long (120 > 100)", rule="E501"),
        ],
        files,
    )
    assert len(merged) == 2


def test_suggestion_keeps_the_replaced_lines_indentation():
    diff_file = parse_diff(DIFF)[0]
    item = finding("bug-hunter", "eval", "Replace eval")
    item.suggestion = Suggestion(
        start_line=3,
        end_line=3,
        replacement="return ast.literal_eval(expression)",
        rationale="Parse literals only",
    )
    validated = RefactoringEngine().validate(item, diff_file)
    assert validated is not None and validated.validated
    assert validated.replacement == "    return ast.literal_eval(expression)"


def test_static_rules_map_onto_the_four_review_axes():
    assert static_category("ruff", "S307") == "bug-hunter"
    assert static_category("ruff", "B006") == "bug-hunter"
    assert static_category("ruff", "PERF401") == "performance"
    assert static_category("ruff", "E501") == "style"
    assert static_category("semgrep", "python-dangerous-eval") == "bug-hunter"
