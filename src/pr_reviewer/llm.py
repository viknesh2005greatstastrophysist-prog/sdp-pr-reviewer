from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Small async client supporting OpenAI-compatible chat-completions APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 45,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport
        self.prompt_tokens = 0
        self.completion_tokens = 0

    async def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self.transport
                ) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions", headers=headers, json=payload
                    )
                if response.status_code == 429 and _quota_exhausted(response):
                    # Billing problems are not transient: fail fast with a clear reason.
                    raise LLMError("LLM provider quota or credit exhausted (insufficient_quota)")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        await asyncio.sleep(_retry_delay(response, attempt))
                        continue
                response.raise_for_status()
                body = response.json()
                usage = body.get("usage", {})
                self.prompt_tokens += int(usage.get("prompt_tokens", 0))
                self.completion_tokens += int(usage.get("completion_tokens", 0))
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                return schema.model_validate(json.loads(_strip_fence(content)))
            except (httpx.HTTPError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2**attempt, 4))
                    continue
                raise LLMError(f"LLM request failed after retries: {exc}") from exc
        raise LLMError("LLM request failed")


def _quota_exhausted(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    if isinstance(body, list):  # Google's OpenAI endpoint returns errors as [{"error": ...}]
        body = body[0] if body else {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if isinstance(error, list):  # some OpenAI-compatible gateways wrap errors in a list
        error = error[0] if error else {}
    return isinstance(error, dict) and error.get("code") == "insufficient_quota"


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the provider sends it (free tiers do), else back off."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(max(float(header), 1.0), 60.0)
        except ValueError:
            pass
    return min(2**attempt * (4 if response.status_code == 429 else 1), 30)


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text
