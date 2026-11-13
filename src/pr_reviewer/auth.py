"""GitHub OAuth web flow with server-side, encrypted sessions.

The browser only ever holds an opaque random session id in an HttpOnly cookie. The GitHub
access token is encrypted with a key derived from SESSION_SECRET and stored server-side;
it is never returned to the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .database import ReviewStore, UserSession
from .settings import Settings

SESSION_COOKIE = "prism_session"
STATE_COOKIE = "prism_oauth_state"
SESSION_TTL = timedelta(hours=8)
STATE_TTL_SECONDS = 600

log = logging.getLogger(__name__)
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)


class OAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    login: str
    name: str | None
    avatar_url: str | None
    token: str

    def public(self) -> dict[str, str | None]:
        return {"login": self.login, "name": self.name, "avatar_url": self.avatar_url}


def _secret(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret
    log.warning("SESSION_SECRET is not set; sessions will not survive a restart.")
    return _EPHEMERAL_SECRET


def _fernet(settings: Settings) -> Fernet:
    digest = hashlib.sha256(("prism-token-key:" + _secret(settings)).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _hash_session_id(settings: Settings, session_id: str) -> str:
    return hmac.new(_secret(settings).encode(), session_id.encode(), hashlib.sha256).hexdigest()


def cookie_secure(settings: Settings) -> bool:
    return settings.public_base_url.startswith("https://")


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url}/auth/github/callback"


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(settings: Settings, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": redirect_uri(settings),
            "scope": settings.github_oauth_scopes,
            "state": state,
            "allow_signup": "false",
        }
    )
    return f"{settings.github_oauth_url.rstrip('/')}/login/oauth/authorize?{query}"


def states_match(expected: str | None, received: str | None) -> bool:
    return bool(expected and received) and hmac.compare_digest(expected, received)


def same_origin(settings: Settings, origin: str | None) -> bool:
    """Reject cross-site state-changing requests. Missing Origin (curl, tests) is allowed."""
    if not origin:
        return True
    allowed = urlparse(settings.public_base_url)
    seen = urlparse(origin)
    return (seen.scheme, seen.netloc) == (allowed.scheme, allowed.netloc)


async def exchange_code(
    settings: Settings, code: str, transport: httpx.AsyncBaseTransport | None = None
) -> str:
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        response = await client.post(
            f"{settings.github_oauth_url.rstrip('/')}/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_oauth_client_id,
                "client_secret": settings.github_oauth_client_secret,
                "code": code,
                "redirect_uri": redirect_uri(settings),
            },
        )
    if response.status_code >= 400:
        raise OAuthError(f"GitHub token exchange failed with status {response.status_code}")
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError(
            f"GitHub rejected the authorization code: {payload.get('error', 'unknown')}"
        )
    return token


def create_session(store: ReviewStore, settings: Settings, user: dict, token: str) -> str:
    session_id = secrets.token_urlsafe(32)
    store.save_session(
        UserSession(
            id_hash=_hash_session_id(settings, session_id),
            login=user["login"],
            name=user.get("name"),
            avatar_url=user.get("avatar_url"),
            encrypted_token=_fernet(settings).encrypt(token.encode()).decode(),
            expires_at=datetime.now(UTC) + SESSION_TTL,
        )
    )
    return session_id


def load_user(
    store: ReviewStore, settings: Settings, session_id: str | None
) -> AuthenticatedUser | None:
    if not session_id:
        return None
    record = store.get_session(_hash_session_id(settings, session_id))
    if record is None:
        return None
    try:
        token = _fernet(settings).decrypt(record.encrypted_token.encode()).decode()
    except InvalidToken:
        return None
    return AuthenticatedUser(record.login, record.name, record.avatar_url, token)


def destroy_session(store: ReviewStore, settings: Settings, session_id: str | None) -> None:
    if session_id:
        store.delete_session(_hash_session_id(settings, session_id))
