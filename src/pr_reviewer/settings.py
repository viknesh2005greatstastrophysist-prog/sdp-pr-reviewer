from __future__ import annotations

import os
from dataclasses import dataclass


def normalise_database_url(url: str) -> str:
    """Render and Heroku hand out postgres:// URLs; SQLAlchemy needs the psycopg driver name."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def parse_demo_accounts(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse "user:hash,user2:hash2". Malformed entries are skipped, never guessed."""
    accounts: list[tuple[str, str]] = []
    for entry in (raw or "").split(","):
        username, separator, hashed = entry.strip().partition(":")
        username = username.strip()
        if separator and username and hashed.startswith("pbkdf2_sha256$"):
            accounts.append((username, hashed))
    return tuple(accounts)


@dataclass(frozen=True)
class Settings:
    github_token: str | None = None
    github_app_id: str | None = None
    github_private_key: str | None = None
    github_webhook_secret: str | None = None
    github_api_url: str = "https://api.github.com"
    github_oauth_url: str = "https://github.com"
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    github_oauth_scopes: str = "repo read:user"
    session_secret: str | None = None
    public_base_url: str = "http://localhost:8000"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4.1-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    database_url: str = "sqlite:///./review_history.db"
    agent_timeout: float = 45.0
    # Server-level permission to publish. A review is published only when this is true
    # AND the individual request opts in (dashboard/API) or WEBHOOK_AUTO_PUBLISH is true.
    publish_reviews: bool = False
    webhook_auto_publish: bool = False
    review_concurrency: int = 2
    min_confidence: float = 0.0
    # username -> "pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>" (see pr-reviewer-hash-password)
    demo_accounts: tuple[tuple[str, str], ...] = ()

    @property
    def password_login_enabled(self) -> bool:
        return bool(self.demo_accounts)

    @property
    def login_required(self) -> bool:
        """Review data is private whenever any sign-in method is configured."""
        return self.oauth_configured or self.password_login_enabled

    @property
    def oauth_configured(self) -> bool:
        return bool(self.github_oauth_client_id and self.github_oauth_client_secret)

    @property
    def server_github_configured(self) -> bool:
        return bool(self.github_token or (self.github_app_id and self.github_private_key))

    @classmethod
    def from_env(cls) -> Settings:
        def enabled(name: str) -> bool:
            return os.getenv(name, "").casefold() in {"1", "true", "yes", "on"}

        def text(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        private_key = text("GITHUB_PRIVATE_KEY")
        if private_key:
            private_key = private_key.replace("\\n", "\n")
        return cls(
            github_token=text("GITHUB_TOKEN"),
            github_app_id=text("GITHUB_APP_ID"),
            github_private_key=private_key,
            github_webhook_secret=text("GITHUB_WEBHOOK_SECRET"),
            github_api_url=text("GITHUB_API_URL") or cls.github_api_url,
            github_oauth_url=text("GITHUB_OAUTH_URL") or cls.github_oauth_url,
            github_oauth_client_id=text("GITHUB_OAUTH_CLIENT_ID"),
            github_oauth_client_secret=text("GITHUB_OAUTH_CLIENT_SECRET"),
            github_oauth_scopes=text("GITHUB_OAUTH_SCOPES") or cls.github_oauth_scopes,
            session_secret=text("SESSION_SECRET"),
            public_base_url=(
                # Render sets RENDER_EXTERNAL_URL automatically for every web service.
                text("PUBLIC_BASE_URL") or text("RENDER_EXTERNAL_URL") or cls.public_base_url
            ).rstrip("/"),
            llm_api_key=text("LLM_API_KEY"),
            llm_model=text("LLM_MODEL") or cls.llm_model,
            llm_base_url=text("LLM_BASE_URL") or cls.llm_base_url,
            database_url=normalise_database_url(text("DATABASE_URL") or cls.database_url),
            agent_timeout=float(text("AGENT_TIMEOUT") or cls.agent_timeout),
            publish_reviews=enabled("PUBLISH_REVIEWS"),
            webhook_auto_publish=enabled("WEBHOOK_AUTO_PUBLISH"),
            review_concurrency=max(1, int(text("REVIEW_CONCURRENCY") or cls.review_concurrency)),
            min_confidence=min(1.0, max(0.0, float(text("MIN_CONFIDENCE") or 0.0))),
            demo_accounts=parse_demo_accounts(os.getenv("DEMO_ACCOUNTS")),
        )
