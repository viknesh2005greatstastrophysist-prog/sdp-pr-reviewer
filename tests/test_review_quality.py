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


def test_assert_in_test_files_is_not_reported(tmp_path):
    """Found live on pallets/click#3901: Ruff S101 in tests/ ranked as the top High finding."""
    from pr_reviewer.analyzers import StaticAnalyzer

    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1,2 @@\n"
        "+def test_x():\n+    assert 1 + 1 == 2\n"
        "diff --git a/app.py b/app.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,2 @@\n"
        "+def check(x):\n+    assert x\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 + 1 == 2\n")
    (tmp_path / "app.py").write_text("def check(x):\n    assert x\n")
    findings, _ = StaticAnalyzer(timeout=120).analyze(parse_diff(diff), tmp_path)
    s101 = [item for item in findings if item.rule_id == "S101"]
    assert [item.path for item in s101] == ["app.py"]
    assert s101[0].severity == Severity.LOW


def test_gemini_retry_delay_in_the_body_is_honoured():
    """Found live on Render: Gemini 429s carry the wait in error.details[].retryDelay."""
    import httpx

    from pr_reviewer.llm import _retry_delay

    body = [{"error": {"code": 429, "details": [{"@type": "RetryInfo", "retryDelay": "17s"}]}}]
    response = httpx.Response(429, json=body)
    assert _retry_delay(response, 0) == 17.0
    assert _retry_delay(httpx.Response(429, json={"error": {}}), 0) == 4


def test_rate_limited_primary_hands_the_call_to_the_fallback_provider():
    """Two free tiers: a Gemini 429 should not fail an agent when Groq is configured."""
    import asyncio

    import httpx
    from pydantic import BaseModel

    from pr_reviewer.llm import FallbackLLMClient, OpenAICompatibleClient

    class Answer(BaseModel):
        ok: bool

    def limited(request):
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    def healthy(request):
        body = {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}
        return httpx.Response(200, json=body)

    primary = OpenAICompatibleClient(
        api_key="a", model="gemini", max_retries=0, transport=httpx.MockTransport(limited)
    )
    fallback = OpenAICompatibleClient(
        api_key="b", model="groq", max_retries=0, transport=httpx.MockTransport(healthy)
    )
    client = FallbackLLMClient(primary, fallback)
    assert asyncio.run(client.structured("s", "u", Answer)).ok is True
    assert client.fallback_calls == 1


def test_production_client_uses_fallback_only_when_configured():
    from pr_reviewer.llm import FallbackLLMClient, OpenAICompatibleClient, build_llm_client
    from pr_reviewer.settings import Settings

    plain = build_llm_client(Settings(llm_api_key="k", llm_model="m"))
    assert isinstance(plain, OpenAICompatibleClient)
    both = build_llm_client(Settings(llm_api_key="k", llm_fallback_api_key="g"))
    assert isinstance(both, FallbackLLMClient)
    assert both.fallback.base_url == "https://api.groq.com/openai/v1"
    assert both.primary.max_retries == 1
