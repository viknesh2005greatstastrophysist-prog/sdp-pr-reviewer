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
        max_retries: int = 4,
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
    hinted = _body_retry_delay(response)
    if hinted is not None:
        return min(max(hinted, 1.0), 60.0)
    return min(2**attempt * (4 if response.status_code == 429 else 1), 30)


def _body_retry_delay(response: httpx.Response) -> float | None:
    """Google's APIs put the wait in the body: error.details[].retryDelay = "17s"."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload[0].get("error", {}) if isinstance(payload, list) and payload else None
    if error is None and isinstance(payload, dict):
        error = payload.get("error", {})
    for detail in (error or {}).get("details", []) or []:
        delay = isinstance(detail, dict) and detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                continue
    return None


def _strip_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text


class FallbackLLMClient:
    """Primary provider first; the same request goes to a second provider when it fails.

    Two free tiers (for example Gemini and Groq) have separate quotas, so a rate limit on
    one does not fail an agent. Token counters cover both providers.
    """

    def __init__(self, primary: OpenAICompatibleClient, fallback: OpenAICompatibleClient):
        self.primary = primary
        self.fallback = fallback
        self.model = primary.model
        self.fallback_calls = 0

    @property
    def prompt_tokens(self) -> int:
        return self.primary.prompt_tokens + self.fallback.prompt_tokens

    @property
    def completion_tokens(self) -> int:
        return self.primary.completion_tokens + self.fallback.completion_tokens

    async def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        try:
            return await self.primary.structured(system, user, schema)
        except LLMError as primary_error:
            self.fallback_calls += 1
            try:
                return await self.fallback.structured(system, user, schema)
            except LLMError as fallback_error:
                raise LLMError(
                    f"primary {self.primary.model}: {primary_error}; "
                    f"fallback {self.fallback.model}: {fallback_error}"
                ) from fallback_error


def build_llm_client(settings, timeout: float | None = None):
    """Production client: the configured provider, plus the fallback when one is set."""
    timeout = settings.agent_timeout if timeout is None else timeout
    has_fallback = bool(settings.llm_fallback_api_key)
    primary = OpenAICompatibleClient(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout=timeout,
        # With a fallback available, hand over after one retry instead of waiting out limits.
        max_retries=1 if has_fallback else 4,
    )
    if not has_fallback:
        return primary
    fallback = OpenAICompatibleClient(
        api_key=settings.llm_fallback_api_key,
        model=settings.llm_fallback_model,
        base_url=settings.llm_fallback_base_url,
        timeout=timeout,
    )
    return FallbackLLMClient(primary, fallback)
