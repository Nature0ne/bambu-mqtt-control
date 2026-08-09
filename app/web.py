from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import WebConfig

basic_auth = HTTPBasic(auto_error=False)
SESSION_COOKIE_NAME = "bambu_session"
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60


class LoginRateLimited(RuntimeError):
    pass


class LoginRateLimiter:
    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        max_clients: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_clients < 1:
            raise ValueError("invalid login rate limit")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, now: float) -> None:
        for client, attempts in tuple(self._attempts.items()):
            while attempts and now - attempts[0] >= self.window_seconds:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(client, None)

    def _key_locked(self, key: str) -> str:
        canonical_key = key[:256]
        if canonical_key not in self._attempts and len(self._attempts) >= self.max_clients:
            return "__overflow__"
        return canonical_key

    def check(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            attempts = self._attempts.get(self._key_locked(key))
            if attempts is not None and len(attempts) >= self.max_attempts:
                raise LoginRateLimited("Zu viele Anmeldeversuche")

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            attempts = self._attempts.setdefault(self._key_locked(key), deque())
            if len(attempts) < self.max_attempts:
                attempts.append(now)

    def reset_on_success(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            self._attempts.pop(self._key_locked(key), None)


class SessionStore:
    """Keep short-lived web sessions in memory and store only token digests."""

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = 1024,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds < 1 or max_sessions < 1:
            raise ValueError("invalid session limits")
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._sessions: dict[bytes, tuple[str, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    @staticmethod
    def _valid_token(token: str) -> bool:
        return 24 <= len(token) <= 1024 and "\x00" not in token and "\n" not in token and "\r" not in token

    def _prune_locked(self, now: float) -> None:
        expired = [digest for digest, (_actor, expiry) in self._sessions.items() if expiry <= now]
        for digest in expired:
            self._sessions.pop(digest, None)

    def issue(self, actor: str) -> str:
        if not actor:
            raise ValueError("session actor is required")
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions, key=lambda digest: self._sessions[digest][1])
                self._sessions.pop(oldest, None)
            for _attempt in range(8):
                token = self._token_factory()
                if not self._valid_token(token):
                    raise ValueError("invalid generated session token")
                digest = self._digest(token)
                if digest not in self._sessions:
                    self._sessions[digest] = (actor, now + self.ttl_seconds)
                    return token
        raise RuntimeError("could not generate a unique session token")

    def authenticate(self, token: str | None) -> str | None:
        if token is None or not self._valid_token(token):
            return None
        now = self._clock()
        digest = self._digest(token)
        with self._lock:
            self._prune_locked(now)
            session = self._sessions.get(digest)
            return session[0] if session is not None else None

    def revoke(self, token: str | None) -> None:
        if token is None or not self._valid_token(token):
            return
        digest = self._digest(token)
        with self._lock:
            self._sessions.pop(digest, None)


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish_threadsafe(self, snapshot: dict[str, Any]) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._publish, snapshot)

    def _publish(self, snapshot: dict[str, Any]) -> None:
        message = {"type": "snapshot", "data": snapshot}
        for queue in tuple(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)


def credentials_match(username: str, password: str, config: WebConfig) -> bool:
    username_matches = secrets.compare_digest(
        username.encode("utf-8"),
        config.username.encode("utf-8"),
    )
    password_matches = secrets.compare_digest(
        password.encode("utf-8"),
        config.password.encode("utf-8"),
    )
    return username_matches and password_matches


def _matches(credentials: HTTPBasicCredentials | None, config: WebConfig) -> bool:
    return credentials is not None and credentials_match(
        credentials.username,
        credentials.password,
        config,
    )


def _session_user(request: Request) -> str | None:
    session_store: SessionStore = request.app.state.session_store
    return session_store.authenticate(request.cookies.get(SESSION_COOKIE_NAME))


def _client_key(connection: Request | WebSocket) -> str:
    return connection.client.host if connection.client else "unknown"


def _http_rate_limit_error(request: Request) -> HTTPException:
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    retry_after = max(1, int(limiter.window_seconds))
    return HTTPException(
        status_code=429,
        detail="Zu viele Anmeldeversuche",
        headers={"Retry-After": str(retry_after)},
    )


def _request_user(
    request: Request,
    credentials: HTTPBasicCredentials | None,
) -> str | None:
    config = request.app.state.config
    if config is None:
        return None
    actor = _session_user(request)
    if actor is not None:
        return actor
    if credentials is None:
        return None
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    key = _client_key(request)
    try:
        limiter.check(key)
    except LoginRateLimited as exc:
        raise _http_rate_limit_error(request) from exc
    if _matches(credentials, config.web):
        limiter.reset_on_success(key)
        return credentials.username
    limiter.record_failure(key)
    return None


def require_user(
    request: Request,
    credentials: Annotated[
        HTTPBasicCredentials | None,
        Depends(basic_auth),
    ],
) -> str:
    config = request.app.state.config
    if config is None:
        raise HTTPException(status_code=503, detail="Einrichtung erforderlich")
    actor = _request_user(request, credentials)
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Anmeldung erforderlich",
            headers={"WWW-Authenticate": 'Basic realm="Bambu Control", charset="UTF-8"'},
        )
    return actor


def require_page_user(
    request: Request,
    credentials: Annotated[
        HTTPBasicCredentials | None,
        Depends(basic_auth),
    ],
) -> str | None:
    if request.app.state.config is None:
        return "setup"
    return _request_user(request, credentials)


def require_csrf(
    request: Request,
    actor: Annotated[str, Depends(require_user)],
) -> str:
    cookie = request.cookies.get("bambu_csrf", "")
    header = request.headers.get("X-CSRF-Token", "")
    expected = request.app.state.csrf_token
    if (
        not cookie
        or not header
        or not secrets.compare_digest(cookie.encode("utf-8"), expected.encode("utf-8"))
        or not secrets.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="Ungültiges CSRF-Token")
    origin = request.headers.get("origin")
    allowed = request.app.state.config.web.allowed_origins
    if not origin or origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin ist nicht freigegeben")
    return actor


@dataclass(frozen=True)
class WebSocketIdentity:
    actor: str
    session_token: str | None = None

    def is_current(self, store: SessionStore) -> bool:
        return self.session_token is None or store.authenticate(self.session_token) == self.actor


def websocket_user(websocket: WebSocket) -> WebSocketIdentity | None:
    app_config = websocket.app.state.config
    if app_config is None:
        return None
    config: WebConfig = app_config.web
    origin = websocket.headers.get("origin")
    if not origin or origin not in config.allowed_origins:
        return None
    session_store: SessionStore = websocket.app.state.session_store
    session_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    session_actor = session_store.authenticate(session_token)
    if session_actor is not None:
        return WebSocketIdentity(session_actor, session_token)
    authorization = websocket.headers.get("authorization", "")
    if not authorization.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(authorization.split(None, 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    credentials = HTTPBasicCredentials(username=username, password=password)
    limiter: LoginRateLimiter = websocket.app.state.login_rate_limiter
    key = _client_key(websocket)
    limiter.check(key)
    if _matches(credentials, config):
        limiter.reset_on_success(key)
        return WebSocketIdentity(username)
    limiter.record_failure(key)
    return None
