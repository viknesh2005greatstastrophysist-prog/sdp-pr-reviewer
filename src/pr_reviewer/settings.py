from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    github_token: str | None = None
    github_app_id: str | None = None
    github_private_key: str | None = None
    github_webhook_secret: str | None = None
    github_api_url: str = "https://api.github.com"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    database_url: str = "sqlite:///./review_history.db"
    agent_timeout: float = 45.0
    publish_reviews: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        def enabled(name: str) -> bool:
            return os.getenv(name, "").casefold() in {"1", "true", "yes", "on"}

        private_key = os.getenv("GITHUB_PRIVATE_KEY")
        if private_key:
            private_key = private_key.replace("\\n", "\n")
        return cls(
            github_token=os.getenv("GITHUB_TOKEN"),
            github_app_id=os.getenv("GITHUB_APP_ID"),
            github_private_key=private_key,
            github_webhook_secret=os.getenv("GITHUB_WEBHOOK_SECRET"),
            github_api_url=os.getenv("GITHUB_API_URL", cls.github_api_url),
            llm_api_key=os.getenv("LLM_API_KEY"),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            llm_base_url=os.getenv("LLM_BASE_URL", cls.llm_base_url),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            agent_timeout=float(os.getenv("AGENT_TIMEOUT", str(cls.agent_timeout))),
            publish_reviews=enabled("PUBLISH_REVIEWS"),
        )
