import asyncio
import hashlib
import hmac
import json

import httpx
from test_parser import DIFF

from pr_reviewer.github import GitHubClient, verify_webhook_signature
from pr_reviewer.models import Finding, PullRequestRef, ReviewResult, Severity, Suggestion
from pr_reviewer.parser import parse_diff
from pr_reviewer.refactor import RefactoringEngine


def test_webhook_signature():
    body = b'{"safe": true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(body, signature, "secret")
    assert not verify_webhook_signature(body + b"x", signature, "secret")


def test_refactor_validation_and_github_comment_payload():
    files = parse_diff(DIFF)
    finding = Finding(
        path="example.py",
        line=2,
        category="bug-hunter",
        severity=Severity.HIGH,
        title="Fix",
        message="Issue",
        evidence="x = 2",
        source="bug-hunter",
        suggestion=Suggestion(
            start_line=2, end_line=2, replacement="x = 3", rationale="Correct value"
        ),
    )
    assert RefactoringEngine().validate(finding, files[0]).validated
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    client = GitHubClient("token", transport=httpx.MockTransport(handler))
    result = ReviewResult(files=files, findings=[finding], summary="Summary")
    asyncio.run(
        client.publish_review(PullRequestRef(owner="o", repo="r", number=1, head_sha="abc"), result)
    )
    assert captured["event"] == "COMMENT"
    assert captured["comments"][0]["side"] == "RIGHT"
    assert "```suggestion" in captured["comments"][0]["body"]


def test_refactor_rejects_non_added_line():
    finding = Finding(
        path="example.py",
        line=2,
        category="bug",
        severity=Severity.LOW,
        message="x",
        evidence="x",
        source="test",
        suggestion=Suggestion(start_line=1, end_line=1, replacement="x = 1", rationale="x"),
    )
    assert RefactoringEngine().validate(finding, parse_diff(DIFF)[0]) is None


def test_refactor_accepts_function_body_statement():
    finding = Finding(
        path="example.py",
        line=3,
        category="bug",
        severity=Severity.LOW,
        message="x",
        evidence="x",
        source="test",
        suggestion=Suggestion(
            start_line=3,
            end_line=3,
            replacement="return x + 1",
            rationale="Correct return",
        ),
    )
    assert RefactoringEngine().validate(finding, parse_diff(DIFF)[0]).validated
