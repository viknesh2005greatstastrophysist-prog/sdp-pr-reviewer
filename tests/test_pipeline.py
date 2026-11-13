import asyncio

from test_parser import DIFF

from pr_reviewer.agents import BaseReviewAgent
from pr_reviewer.models import Finding, Severity
from pr_reviewer.pipeline import review_diff


class ExampleAgent(BaseReviewAgent):
    category = "correctness"

    async def review(self, files):
        return [
            Finding(
                path="example.py",
                line=2,
                category=self.category,
                severity=Severity.HIGH,
                message="example test finding",
                evidence="fixture",
                source="test",
            )
        ]


class FailingAgent(BaseReviewAgent):
    category = "performance"

    async def review(self, files):
        raise RuntimeError("test failure")


def test_partial_result():
    result = asyncio.run(review_diff(DIFF, agents=[ExampleAgent(), FailingAgent()]))
    assert len(result.findings) == 1
    assert any("performance agent failed" in x for x in result.warnings)
