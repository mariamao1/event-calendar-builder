from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

# Password hashes use the format:
#   pbkdf2-sha256$<iterations>$<base64 salt>$<base64 derived key>
# Raw passwords and raw session tokens are never persisted or logged; only
# the hash strings and SHA-256 digests of tokens are stored.
_HASH_ALGORITHM = "pbkdf2-sha256"
_DEFAULT_ITERATIONS = 210_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a fresh random salt."""
    if not password:
        raise ValueError("password must not be empty")
    if iterations < 1:
        raise ValueError("iterations must be positive")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    encoded_salt = base64.b64encode(salt).decode("ascii")
    encoded_digest = base64.b64encode(digest).decode("ascii")
    return f"{_HASH_ALGORITHM}${iterations}${encoded_salt}${encoded_digest}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time password check against a hash from :func:`hash_password`."""
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$")
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt)
        expected = base64.b64decode(raw_digest)
    except (ValueError, base64.binascii.Error):
        return False
    if algorithm != _HASH_ALGORITHM or iterations < 1:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate, expected)


def new_token() -> str:
    """Generate a 256-bit unguessable token (calendar link or admin session)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_digest(token: str) -> str:
    """SHA-256 hex digest used as a storage/lookup key for session tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_digest_bytes(token: str) -> bytes:
    """Binary SHA-256 digest used for durable management tokens."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def token_matches_digest(token: str | None, expected: bytes | None) -> bool:
    """Check a raw token against a stored digest without timing leaks."""
    if not token or not expected:
        return False
    return hmac.compare_digest(token_digest_bytes(token), bytes(expected))


def tokens_equal(provided: str | None, expected: str | None) -> bool:
    """Constant-time bearer/link-token comparison that fails closed."""
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except TypeError:
        # Non-ASCII input: not equal, never an error.
        return False


@dataclass
class _Session:
    username: str
    expires_at: float


class SessionStore:
    """Process-local store for admin session tokens.

    Tokens are 256-bit random values looked up by SHA-256 digest with an
    absolute expiry. Sessions never leave this process, so multi-process
    deployments need sticky routing or a shared session backend.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def create(self, username: str, *, ttl_seconds: int) -> tuple[str, float]:
        token = new_token()
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._prune_locked()
            self._sessions[token_digest(token)] = _Session(username, expires_at)
        return token, expires_at

    def validate(self, token: str | None) -> str | None:
        """Return the session username, or None when unknown/expired."""
        if not token:
            return None
        digest = token_digest(token)
        with self._lock:
            session = self._sessions.get(digest)
            if session is None:
                return None
            if session.expires_at <= time.time():
                del self._sessions[digest]
                return None
            return session.username

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token_digest(token), None) is not None

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [
            digest
            for digest, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for digest in expired:
            del self._sessions[digest]
