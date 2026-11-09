"""Password checking, session cookies, share tokens and rate limiting."""
import base64
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import config

COOKIE_NAME = "hd_session"
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAXMEM = 128 * 1024 * 1024


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=32, maxmem=_MAXMEM,
    )
    return "scrypt$%s$%s" % (salt.hex(), digest.hex())


def _check_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
            maxmem=_MAXMEM,
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest_hex)


_PASSWORD_HASH = (
    config.ADMIN_PASSWORD_HASH
    or (hash_password(config.ADMIN_PASSWORD) if config.ADMIN_PASSWORD else "")
)


def check_password(candidate: str) -> bool:
    # A password rotated from the Settings page lives in SQLite and wins over
    # the environment value; the env password is only the bootstrap credential.
    try:
        from . import db
        stored = db.kv_get("admin_password_hash", secret=True)
    except Exception:
        stored = None
    if stored:
        return _check_hash(candidate, stored)
    if not _PASSWORD_HASH:
        return False
    if config.ADMIN_PASSWORD and hmac.compare_digest(candidate, config.ADMIN_PASSWORD):
        return True
    return _check_hash(candidate, _PASSWORD_HASH)


def _key(purpose: str = "session") -> bytes:
    return hashlib.sha256((purpose + ":" + config.APP_SECRET).encode()).digest()


def _session_epoch() -> str:
    from . import db
    return db.kv_get("session_epoch", "0") or "0"


def bump_session_epoch() -> None:
    """Invalidate every issued session cookie (logout, password rotation)."""
    from . import db
    db.kv_set("session_epoch", str(int(time.time() * 1000)))


def _decode_token(value: str) -> bytes | None:
    """Decode a signed token, rejecting non-canonical base64.

    Python's base64 decoder tolerates stray trailing characters on some
    versions, so a token with junk appended can decode to the same bytes.
    Re-encoding and comparing forces exactly one valid spelling per token.
    """
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except Exception:
        return None
    if base64.urlsafe_b64encode(raw).decode() != value:
        return None
    return raw


def issue_session(days: int | None = None) -> str:
    days = days or config.SESSION_DAYS
    expiry = int(time.time()) + days * 86400
    payload = f"admin:{_session_epoch()}:{expiry}:{secrets.token_hex(8)}".encode()
    sig = hmac.new(_key("session"), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b"." + sig.encode()).decode()


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    raw = _decode_token(token)
    if raw is None:
        return False
    try:
        payload, sig = raw.rsplit(b".", 1)
        role, epoch, expiry, _nonce = payload.decode().split(":")
    except Exception:
        return False
    expected = hmac.new(_key("session"), payload, hashlib.sha256).hexdigest().encode()
    if not hmac.compare_digest(sig, expected):
        return False
    if role != "admin" or epoch != _session_epoch():
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


# ---- share tokens --------------------------------------------------------
# Share visitors must never hold a token that also opens the admin session.

def issue_share(token: str, ttl_seconds: int = 3600) -> str:
    expiry = int(time.time()) + ttl_seconds
    payload = f"share:{token}:{expiry}".encode()
    sig = hmac.new(_key("share:" + token), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b"." + sig.encode()).decode()


def verify_share(token: str, value: str | None) -> bool:
    if not value:
        return False
    raw = _decode_token(value)
    if raw is None:
        return False
    try:
        payload, sig = raw.rsplit(b".", 1)
        role, share_token, expiry = payload.decode().split(":")
    except Exception:
        return False
    if role != "share" or share_token != token:
        return False
    expected = hmac.new(_key("share:" + token), payload, hashlib.sha256).hexdigest().encode()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(expiry) > time.time()
    except ValueError:
        return False


# ---- request helpers -----------------------------------------------------

def client_ip(request: Request) -> str:
    """Caddy is the only hop in front of the app and appends the real peer, so
    the last X-Forwarded-For entry is the trustworthy one. Anything a client
    sends itself ends up to the left of it."""
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if forwarded:
        return forwarded[-1]
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or proto.split(",")[0].strip() == "https"


def set_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def require_admin(request: Request) -> None:
    if not verify_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_mutation(request: Request) -> None:
    """Custom-header check: browsers cannot send it cross-site without a preflight."""
    if request.headers.get("x-requested-with") not in ("haodrive", "seagulldrive"):
        raise HTTPException(status_code=403, detail="missing mutation header")


class RateLimiter:
    """Per-key sliding window with a hard key cap so hostile traffic cannot
    grow it without bound."""

    MAX_KEYS = 4096

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int = 8, window: int = 300) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < window]
        self._hits[key] = hits
        return len(hits) < limit

    def record(self, key: str) -> None:
        if len(self._hits) >= self.MAX_KEYS and key not in self._hits:
            # Drop the oldest half rather than growing forever.
            for stale in sorted(self._hits, key=lambda k: min(self._hits[k]) if self._hits[k] else 0)[: self.MAX_KEYS // 2]:
                self._hits.pop(stale, None)
        self._hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def failures(self, key: str, window: int = 900) -> int:
        now = time.time()
        return len([t for t in self._hits.get(key, []) if now - t < window])


login_limiter = RateLimiter()
share_limiter = RateLimiter()
