import asyncio
import json

import httpx
from test_parser import DIFF

from pr_reviewer.agents import LLMReviewAgent
from pr_reviewer.llm import OpenAICompatibleClient
from pr_reviewer.parser import parse_diff


def test_llm_agent_accepts_only_added_lines():
    payload = {
        "findings": [
            {
                "path": "example.py",
                "line": 2,
                "category": "wrong",
                "severity": "HIGH",
                "title": "Bug",
                "message": "Bad value",
                "evidence": "x = 2",
                "source": "wrong",
            },
            {
                "path": "example.py",
                "line": 99,
                "category": "wrong",
                "severity": "HIGH",
                "title": "Hallucination",
                "message": "Not in diff",
                "evidence": "none",
                "source": "wrong",
            },
        ]
    }

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = OpenAICompatibleClient(
        api_key="test", model="test", transport=httpx.MockTransport(handler), max_retries=0
    )
    findings = asyncio.run(LLMReviewAgent("bug-hunter", client).review(parse_diff(DIFF)))
    assert len(findings) == 1
    assert findings[0].category == findings[0].source == "bug-hunter"
    assert (client.prompt_tokens, client.completion_tokens) == (10, 5)
