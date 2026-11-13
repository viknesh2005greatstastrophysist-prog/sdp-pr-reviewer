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
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
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
    token: str | None
    auth_method: str = "github"

    def public(self) -> dict[str, str | None]:
        return {
            "login": self.login,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "auth_method": self.auth_method,
        }


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


def create_session(store: ReviewStore, settings: Settings, user: dict, token: str | None) -> str:
    """Store a session. ``token=None`` marks a password session (no GitHub token exists)."""
    session_id = secrets.token_urlsafe(32)
    try:
        store.purge_expired_sessions()
    except Exception:  # noqa: BLE001 - housekeeping must never block a sign-in
        log.warning("Could not purge expired sessions", exc_info=True)
    store.save_session(
        UserSession(
            id_hash=_hash_session_id(settings, session_id),
            login=user["login"],
            name=user.get("name"),
            avatar_url=user.get("avatar_url"),
            encrypted_token=_fernet(settings).encrypt((token or "").encode()).decode(),
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
    if not token:  # an empty encrypted token is the marker for a password session
        return AuthenticatedUser(record.login, record.name, None, None, "password")
    return AuthenticatedUser(record.login, record.name, record.avatar_url, token)


def destroy_session(store: ReviewStore, settings: Settings, session_id: str | None) -> None:
    if session_id:
        store.delete_session(_hash_session_id(settings, session_id))


# Demo accounts (username + password) ------------------------------------------------------
HASH_SCHEME = "pbkdf2_sha256"
MIN_ITERATIONS = 600_000
MAX_ITERATIONS = 5_000_000
LOCKOUT_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 600
IP_LOCKOUT_ATTEMPTS = 20


def hash_password(password: str, iterations: int = MIN_ITERATIONS) -> str:
    if iterations < MIN_ITERATIONS:
        raise ValueError(f"iterations must be at least {MIN_ITERATIONS}")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            HASH_SCHEME,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_text, salt_b64, hash_b64 = encoded.split("$")
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
    except ValueError:
        return False
    if scheme != HASH_SCHEME or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, len(expected))
    return hmac.compare_digest(actual, expected)


# Verified for unknown usernames so response time does not reveal which accounts exist.
@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def authenticate(settings: Settings, username: str, password: str) -> bool:
    accounts = dict(settings.demo_accounts)
    encoded = accounts.get(username)
    valid = verify_password(password, encoded or _dummy_hash())
    return valid and encoded is not None


class LoginThrottle:
    """In-memory lockout: 5 failures per username or client address within 10 minutes."""

    def __init__(
        self,
        attempts: int = LOCKOUT_ATTEMPTS,
        window: float = LOCKOUT_WINDOW_SECONDS,
        clock=time.monotonic,
    ):
        self.attempts = attempts
        self.window = window
        self.clock = clock
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str) -> deque[float]:
        queue = self.failures[key]
        cutoff = self.clock() - self.window
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if not queue:
            self.failures.pop(key, None)
        return queue

    def locked(self, *keys: str) -> bool:
        return any(len(self._recent(key)) >= self.attempts for key in keys)

    def record_failure(self, *keys: str) -> None:
        now = self.clock()
        if len(self.failures) > 5000:  # bound memory under a username-spraying flood
            for stale in list(self.failures):
                self._recent(stale)
        for key in keys:
            self.failures[key].append(now)

    def reset(self, *keys: str) -> None:
        for key in keys:
            self.failures.pop(key, None)
