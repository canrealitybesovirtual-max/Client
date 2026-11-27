"""Accounts, sessions, CSRF, API keys and the /mcp gate.

- Passwords: hashlib.scrypt (n=2^14, r=8, p=1) with a random salt.
- Sessions: HMAC-SHA256 signed cookie (HttpOnly, SameSite=Lax, Secure over https) with an expiry. The payload holds the
  user's session generation, so a password change logs out every other device.
- CSRF: double-submit. A readable cookie holds a token derived from the session nonce; cookie-authenticated
  POST/PUT/PATCH/DELETE must echo it in the X-CSRF-Token header. Bearer (API key) requests don't use cookies, so they
  are exempt.
- API keys: shown once; only the SHA-256 hash is stored. Usable as `Authorization: Bearer <key>` on /api and /mcp.
- OAuth access tokens (app/oauth.py, "Sign in with TradeComply" for apps such as Claude): `Bearer tco_...` on /mcp
  only (their audience is <base>/mcp), acting as the person who approved the app, with that person's current role.
- Nothing here logs a password, cookie or key.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .store import IntegrityError

log = logging.getLogger("tradecomply.auth")

SESSION_COOKIE = "tc_session"
CSRF_COOKIE = "tc_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_DAYS = 14
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
KEY_PREFIX = "tck_"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
ANON_AUDIT_KEEP = 50_000          # newest anonymous /mcp audit rows kept (they belong to no tenant)


# ---------------------------------------------------------------------------- primitives
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(_unb64(dk)), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, _unb64(dk))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(12))   # equalizes timing when the email doesn't exist


MIN_SECRET_KEY = 32


def load_secret_key(data_dir: str) -> bytes:
    """SECRET_KEY from the environment, else a random key generated once and kept in DATA_DIR/secret_key (0600).

    Everything (session cookies, CSRF tokens, calendar links) is HMAC-signed with this key, so a short or guessable
    key would let anyone forge a login: one under 32 characters is refused at startup."""
    env = os.getenv("SECRET_KEY")
    if env:
        if len(env.strip()) < MIN_SECRET_KEY:
            raise RuntimeError(f"SECRET_KEY is too short ({len(env.strip())} characters). Use at least "
                               f"{MIN_SECRET_KEY} random characters, e.g. python -c \"import secrets; "
                               f"print(secrets.token_urlsafe(48))\", or unset it to use a generated key.")
        return env.strip().encode("utf-8")
    path = os.path.join(data_dir, "secret_key")
    for _ in range(2):
        try:
            with open(path, "rb") as f:
                key = f.read().strip()
            if len(key) >= MIN_SECRET_KEY:
                return key
            os.replace(path, path + ".short")          # unusable; keep a copy, generate a proper one
        except FileNotFoundError:
            pass
        key = secrets.token_urlsafe(48).encode()
        try:   # O_EXCL: if two processes start at once, both end up using the one file that won
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        log.info("generated a new SECRET_KEY in DATA_DIR (set SECRET_KEY to manage it yourself)")
        return key
    raise RuntimeError(f"could not read or create {path}")


class Signer:
    def __init__(self, key: bytes):
        self._key = key

    def mac(self, purpose: str, message: str) -> str:
        return hmac.new(self._key, f"{purpose}:{message}".encode(), hashlib.sha256).hexdigest()

    def sign(self, purpose: str, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._key, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(sig)}"

    def unsign(self, purpose: str, token: str | None) -> dict | None:
        if not token or token.count(".") != 1:
            return None
        body, sig = token.split(".")
        want = hmac.new(self._key, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(want, _unb64(sig)):
                return None
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or float(payload.get("e", 0)) < time.time():
            return None
        return payload


class RateLimiter:
    """In-memory sliding window of events per key (one process; good enough for one Render instance).

    Memory is bounded by max_keys. When full, expired windows are dropped first, then the keys with the fewest
    events, and never a key that is currently locked out (so flooding the table with junk keys can't reset a
    lockout)."""

    def __init__(self, limit: int, window_s: int, max_keys: int = 50000):
        self.limit, self.window, self.max_keys = limit, window_s, max_keys
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, limit: int | None = None) -> int:
        """Seconds until `key` may act again (0 = now). `limit` overrides this limiter's limit for this key (a
        per-API-key rate); it must not exceed self.limit, or a locked-out key could be evicted under memory
        pressure."""
        now = time.monotonic()
        cap = self.limit if limit is None else min(int(limit), self.limit)
        with self._lock:
            q = self._hits.get(key)
            while q and q[0] <= now - self.window:
                q.popleft()
            if q and len(q) >= cap:
                return int(q[len(q) - cap] + self.window - now) + 1
            return 0

    def hit(self, key: str):
        with self._lock:
            self._hits.setdefault(key, deque()).append(time.monotonic())
            if len(self._hits) > self.max_keys:
                self._evict(keep=key)

    def claim(self, key: str) -> int:
        """retry_after() and hit() as one step: 0 means this attempt is counted now (before the slow work it
        guards), anything else is the wait and nothing is counted. Use it where an `await` sits between the check
        and the outcome, e.g. a password check: with retry_after() first and hit() only after a wrong password,
        thirty requests sent at the same moment all pass the check before the first failure is counted. Undo a
        claim with release() (or reset()) when the attempt turns out fine."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            while q and q[0] <= now - self.window:
                q.popleft()
            if q and len(q) >= self.limit:
                return int(q[len(q) - self.limit] + self.window - now) + 1
            self._hits.setdefault(key, deque()).append(now)
            if len(self._hits) > self.max_keys:
                self._evict(keep=key)
            return 0

    def release(self, key: str) -> None:
        """Take back one claim (the newest event) without clearing the key's other events."""
        with self._lock:
            q = self._hits.get(key)
            if q:
                q.pop()
                if not q:
                    del self._hits[key]

    def _evict(self, keep: str):
        cutoff = time.monotonic() - self.window
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]
        excess = len(self._hits) - self.max_keys
        if excess <= 0:
            return
        candidates = sorted((len(q), q[-1], k) for k, q in self._hits.items() if k != keep and len(q) < self.limit)
        for _, _, k in candidates[:excess]:
            del self._hits[k]
        if len(self._hits) > 2 * self.max_keys:     # safety valve: even lockouts go, oldest first
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[:len(self._hits) - self.max_keys]:
                if k != keep:
                    del self._hits[k]

    def reset(self, key: str):
        with self._lock:
            self._hits.pop(key, None)


def new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext key shown once, display prefix, sha256 hash stored)."""
    plain = KEY_PREFIX + secrets.token_urlsafe(32)
    return plain, plain[:12], hash_key(plain)


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- principal
@dataclass
class Principal:
    tenant_id: str
    role: str
    via: str                     # "cookie" | "key" | "oauth" (an app connected with OAuth, on /mcp only)
    user_id: int | None = None
    email: str | None = None
    key_id: int | None = None
    key_prefix: str | None = None
    must_change_password: bool = False
    scopes: dict | None = None        # API keys only: tool scope (None = all read-only tools; mcp_governance.py)
    rate_limit: int | None = None     # API keys only: tool calls per minute (None = the platform default)
    grant_id: int | None = None       # OAuth only: the connection (oauth_grants row) the access token belongs to
    client_name: str | None = None    # OAuth only: the app's name as it registered itself

    @property
    def label(self) -> str:
        if self.via == "oauth":
            return f"{self.email} via {(self.client_name or 'an app')[:60]}"
        return self.email or f"api key {self.key_prefix}…"


def platform_of(request: Request):
    return request.app.state.platform


def forwarded_ip(settings, fwd: str | None, peer: str) -> str:
    """The caller's address. Hosted, the app sits behind a proxy that adds to X-Forwarded-For. With
    TRUSTED_PROXY_HOPS = N (the proxies in front of the app that each append the address they saw), it is the N-th
    entry from the right, which no client can set; a shorter header didn't pass through them, so the peer is used.
    With 0 (the default: how Render fills the header is not confirmed) it is the first entry, which a client can
    set itself, so per-IP limits are best effort; per-email and global limits back them up, and OAuth's per-IP
    limits use ip_key. Locally there is no proxy, so the header is ignored."""
    if not fwd or settings.local_mode:
        return peer
    hops, n = fwd.split(","), settings.trusted_proxy_hops
    if not n:
        return hops[0].strip()[:64]
    return hops[-n].strip()[:64] if len(hops) >= n else peer


def client_ip(request: Request) -> str:
    return forwarded_ip(platform_of(request).settings, request.headers.get("x-forwarded-for"),
                        request.client.host if request.client else "unknown")


def ip_key(request: Request) -> str:
    """The per-IP rate-limit key for limits that a caller must not be able to aim at someone else (OAuth: Claude's
    calls for every customer come from a few Anthropic addresses). With TRUSTED_PROXY_HOPS, the trusted address.
    Otherwise the whole X-Forwarded-For chain: a caller chooses its first entries, not the ones its proxies append,
    so a forged chain never equals another caller's. (It can still start a fresh bucket; the global caps bound that.)"""
    settings, fwd = platform_of(request).settings, request.headers.get("x-forwarded-for")
    if settings.trusted_proxy_hops or settings.local_mode or not fwd:
        return client_ip(request)
    return ",".join(h.strip() for h in fwd.split(","))[-256:]


def is_https(request: Request) -> bool:
    return (request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0] == "https"
            or platform_of(request).settings.cookie_secure)


def public_base(settings, headers, scheme: str | None) -> str:
    """This server's public address without a trailing slash: APP_URL (or Render's RENDER_EXTERNAL_URL) when set,
    else from the request (X-Forwarded-Host or Host; https when the request, the proxy or COOKIE_SECURE says so).
    `headers` is a Starlette Headers or the /mcp gate's lowercase dict. The OAuth issuer, the protected resource
    (<base>/mcp), the /mcp 401 challenge and the links in the app all come from here, so they always agree."""
    if settings.app_url:
        return settings.app_url
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip()
    host = re.sub(r"[^A-Za-z0-9.:\[\]-]", "", host)[:255] or "localhost"    # nothing that breaks out of a header
    https = (scheme == "https" or settings.cookie_secure
             or (headers.get("x-forwarded-proto") or "").split(",")[0].strip() == "https")
    return f"{'https' if https else 'http'}://{host}"


def principal_from_key(platform, plain: str) -> Principal | None:
    if not plain or not plain.startswith(KEY_PREFIX) or len(plain) > 200:
        return None
    key = platform.store.api_key_by_hash(hash_key(plain))
    if not key or key["revoked"]:
        return None
    from .mcp_governance import key_expired        # local import: mcp_governance imports this module
    if key_expired(key):
        return None
    return Principal(tenant_id=key["tenant_id"], role="office", via="key", key_id=key["id"], key_prefix=key["prefix"],
                     scopes=key.get("scopes"), rate_limit=key.get("rate_limit_per_min"))


def user_fingerprint(platform, user: dict) -> str:
    """Binds a session to this exact user row. SQLite reuses the highest user id after a delete, so without this a
    removed user's cookie would log in as whoever is invited next. The password hash has a random salt, so the
    fingerprint also changes whenever the password does."""
    return platform.signer.mac("user", f"{user['id']}|{user['email']}|{user['pw_hash']}")[:32]


def principal_from_session(platform, token: str | None) -> tuple[Principal | None, str | None]:
    payload = platform.signer.unsign("session", token)
    if not payload:
        return None, None
    try:
        user = platform.store.user_by_id(int(payload.get("u", 0)))
    except (TypeError, ValueError):
        return None, None
    if not user or user["tenant_id"] != payload.get("t") or user["session_gen"] != payload.get("g"):
        return None, None
    if not hmac.compare_digest(str(payload.get("f", "")), user_fingerprint(platform, user)):
        return None, None
    if platform.store.session_revoked(str(payload.get("n", ""))):
        return None, None
    return (Principal(tenant_id=user["tenant_id"], role=user["role"], via="cookie", user_id=user["id"],
                      email=user["email"], must_change_password=bool(user["must_change_password"])),
            payload.get("n"))


def bearer(request_headers) -> str | None:
    h = request_headers.get("authorization") or ""
    scheme, _, token = h.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def get_principal(request: Request) -> Principal:
    platform = platform_of(request)
    token = bearer(request.headers)
    if token:
        p = await anyio.to_thread.run_sync(principal_from_key, platform, token)
        if not p:
            raise HTTPException(401, "Invalid, expired or revoked API key", headers={"WWW-Authenticate": "Bearer"})
        from .mcp_governance import rest_api_allowed
        if not rest_api_allowed(p.scopes) and not _tool_route(request):
            raise HTTPException(403, "This API key is limited to tool calls (/mcp and /api/tools). An owner or office "
                                     "user can widen it in Settings > AI assistant (MCP) > API keys.")
    else:
        p, nonce = await anyio.to_thread.run_sync(principal_from_session, platform,
                                                  request.cookies.get(SESSION_COOKIE))
        if not p:
            raise HTTPException(401, "Please log in")
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(CSRF_HEADER) or ""
            cookie = request.cookies.get(CSRF_COOKIE) or ""
            expected = platform.signer.mac("csrf", nonce or "")
            if not (sent and hmac.compare_digest(sent, cookie) and hmac.compare_digest(sent, expected)):
                raise HTTPException(403, "Security check failed (CSRF). Reload the page and try again.")
    request.state.principal = p
    return p


def _tool_route(request: Request) -> bool:
    """The REST routes a tool-calls-only API key may use: list tools, run a tool, and the tool definition hashes."""
    path, method = request.url.path, request.method
    return (path in ("/api/tools", "/api/mcp/tool-hashes") and method in SAFE_METHODS) or \
        (path.startswith("/api/tools/") and path.count("/") == 3 and method == "POST")


def require_roles(*roles: str):
    async def dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(403, f"Your role ({p.role}) can't do this. Ask the account owner.")
        return p
    return dep


owner_only = require_roles("owner")
office_up = require_roles("owner", "office")
anyone = get_principal


def set_session(response, request: Request, user: dict) -> str:
    platform = platform_of(request)
    nonce = secrets.token_urlsafe(16)
    exp = time.time() + SESSION_DAYS * 86400
    token = platform.signer.sign("session", {"u": user["id"], "t": user["tenant_id"], "g": user["session_gen"],
                                             "f": user_fingerprint(platform, user), "n": nonce, "e": int(exp)})
    csrf = platform.signer.mac("csrf", nonce)
    secure = is_https(request)
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=secure, path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_DAYS * 86400, httponly=False, samesite="lax",
                        secure=secure, path="/")
    return csrf


def clear_session(response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# ---------------------------------------------------------------------------- routes
router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupIn(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    cslb_license: str | None = Field(None, max_length=20)
    name: str | None = Field(None, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=256)


def license_digits(value: str | None) -> str | None:
    d = "".join(ch for ch in (value or "") if ch.isdigit())
    return d or None


@router.post("/signup")
async def signup(body: SignupIn, request: Request, background: BackgroundTasks):
    platform = platform_of(request)
    ip = client_ip(request)
    wait = max(platform.signup_limiter.retry_after(ip), platform.signup_global_limiter.retry_after("all"))
    if wait:
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.",
                            headers={"Retry-After": str(wait)})
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "Enter a valid email address")
    if await anyio.to_thread.run_sync(platform.store.user_by_email, email):
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    platform.signup_limiter.hit(ip)
    platform.signup_global_limiter.hit("all")
    pw_hash = await anyio.to_thread.run_sync(hash_password, body.password)
    trial_ends = (datetime.now(timezone.utc) + timedelta(days=platform.settings.trial_days)).replace(
        microsecond=0).isoformat()
    try:      # company and owner in one transaction: a double-submitted form can't leave a company with no owner
        tenant, user = await anyio.to_thread.run_sync(lambda: platform.store.create_tenant_with_owner(
            body.company_name.strip(), license_digits(body.cslb_license), trial_ends, email, pw_hash,
            name=(body.name or "").strip() or None))
    except IntegrityError:    # the same email signed up a moment ago (it passed the check above at the same time)
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    from . import auth_links       # local import: app.auth_links imports this module
    background.add_task(auth_links.after_signup, platform, user)     # the email confirmation link, if set up
    resp = JSONResponse({"ok": True, "tenant_id": tenant["id"], "trial_ends": trial_ends}, background=background)
    set_session(resp, request, user)
    return resp


@router.post("/login")
async def login(body: LoginIn, request: Request):
    platform = platform_of(request)
    email = body.email.strip().lower()
    key, ekey = f"{client_ip(request)}|{email}", f"email|{email}"
    # Each attempt is counted before the password is checked (claim), and taken back if it was right: requests
    # sent all at once can't each slip past the limit while the others are still being checked.
    wait = platform.login_limiter.claim(key)
    if not wait:
        wait = platform.email_limiter.claim(ekey)
        if wait:
            platform.login_limiter.release(key)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = await anyio.to_thread.run_sync(platform.store.user_by_email, email)
    ok = await anyio.to_thread.run_sync(verify_password, body.password, user["pw_hash"] if user else _DUMMY_HASH)
    if not (user and ok):
        raise HTTPException(401, "Email or password is incorrect")
    platform.login_limiter.reset(key)
    platform.email_limiter.release(ekey)
    # A company that requires single sign-on refuses password sign-in for everyone but owners (their break-glass
    # path, logged). Checked only after the password, so the answer says nothing about unknown emails.
    from . import sso       # local import: app.sso imports this module
    refusal = await anyio.to_thread.run_sync(sso.password_login_refusal, platform, user)
    if refusal:
        raise HTTPException(403, refusal)
    await anyio.to_thread.run_sync(lambda: platform.store.update_user(
        user["tenant_id"], user["id"], last_login=datetime.now(timezone.utc).isoformat()))
    resp = JSONResponse({"ok": True, "must_change_password": bool(user["must_change_password"])})
    set_session(resp, request, user)
    return resp


@router.post("/logout")
async def logout(request: Request):
    """Ends this session for good: the cookie is cleared and its nonce is recorded, so a copy of the cookie (stolen,
    or left in another browser profile) stops working too. No CSRF check: the worst a forged request can do is log
    someone out."""
    platform = platform_of(request)
    payload = platform.signer.unsign("session", request.cookies.get(SESSION_COOKIE))
    if payload and payload.get("n"):
        await anyio.to_thread.run_sync(platform.store.revoke_session, str(payload["n"]), int(payload.get("e", 0)))
    resp = JSONResponse({"ok": True})
    clear_session(resp)
    return resp


@router.post("/password")
async def change_password(body: PasswordIn, request: Request, background: BackgroundTasks,
                          p: Principal = Depends(get_principal)):
    if p.via != "cookie":
        raise HTTPException(403, "Log in with your password to change it")
    platform = platform_of(request)
    key = f"pw|{p.user_id}"
    wait = platform.login_limiter.claim(key)          # counted before the check: see login()
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    user = await anyio.to_thread.run_sync(platform.store.user_by_id, p.user_id)
    if not await anyio.to_thread.run_sync(verify_password, body.current_password, user["pw_hash"]):
        raise HTTPException(403, "Current password is incorrect")
    platform.login_limiter.reset(key)
    pw_hash = await anyio.to_thread.run_sync(hash_password, body.new_password)
    user = await anyio.to_thread.run_sync(lambda: platform.store.update_user(
        p.tenant_id, p.user_id, pw_hash=pw_hash, session_gen=user["session_gen"] + 1, must_change_password=0))
    # Apps this person connected (app/oauth.py) are disconnected too. (Their grants are also bound to the session
    # counter just bumped, so they would stop working anyway; this records it.)
    await anyio.to_thread.run_sync(platform.store.revoke_user_oauth_grants, p.tenant_id, p.user_id,
                                 "password_changed")
    # One-time links (app/auth_links.py) were bound to the old session counter, so they no longer work either.
    from . import auth_links
    background.add_task(auth_links.notify_password_changed, platform, user)    # confirmed addresses only
    resp = JSONResponse({"ok": True}, background=background)
    set_session(resp, request, user)          # this device stays logged in, every other session ends
    return resp


@router.get("/local")
async def local_login(token: str, request: Request):
    """One-time login link printed by `python -m app.local` (local mode only)."""
    platform = platform_of(request)
    want = platform.local_login_token
    if not (platform.settings.local_mode and want and platform.local_user_id
            and hmac.compare_digest(token.encode(), want.encode())):
        return RedirectResponse("/#/login", status_code=303)
    platform.local_login_token = None          # single use (cleared before the first await: no second winner)
    user = await anyio.to_thread.run_sync(platform.store.user_by_id, platform.local_user_id)
    resp = RedirectResponse("/#/dashboard", status_code=303)
    if user:
        set_session(resp, request, user)
    return resp


# ---------------------------------------------------------------------------- /mcp gate (pure ASGI)
def _is_mcp(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _summarize_args(args, depth: int = 0):
    """Audit rows keep arguments small and avoid storing bulk data (e.g. a payroll CSV) verbatim."""
    if isinstance(args, str):
        return args if len(args) <= 300 else f"<{len(args)} chars, sha256 {hashlib.sha256(args.encode()).hexdigest()[:12]}>"
    if isinstance(args, dict) and depth < 4:
        return {str(k)[:80]: _summarize_args(v, depth + 1) for k, v in list(args.items())[:50]}
    if isinstance(args, list) and depth < 4:
        out = [_summarize_args(v, depth + 1) for v in args[:20]]
        if len(args) > 20:
            out.append(f"<{len(args) - 20} more>")
        return out
    if isinstance(args, (int, float, bool)) or args is None:
        return args
    return str(args)[:300]


def _parse_jsonrpc(body: bytes) -> list[dict]:
    try:
        msg = json.loads(body or b"null")
    except ValueError:
        return []
    msgs = msg if isinstance(msg, list) else [msg]
    return [m for m in msgs if isinstance(m, dict)]


def _rpc_key(rpc_id) -> str:
    """A hashable key for a JSON-RPC id (a client may send any JSON value there)."""
    return json.dumps(rpc_id, sort_keys=True, default=str)


def _responses_from(body: bytes, content_type: str) -> dict:
    """Map _rpc_key(JSON-RPC id) -> response message, from a JSON or text/event-stream body."""
    out = {}
    chunks = []
    if "text/event-stream" in content_type:
        for line in body.decode("utf-8", "replace").splitlines():
            if line.startswith("data:"):
                chunks.append(line[5:].strip().encode())
    else:
        chunks.append(body)
    for c in chunks:
        for m in _parse_jsonrpc(c):
            if "id" in m and ("result" in m or "error" in m):
                out[_rpc_key(m["id"])] = m
    return out


def _scope_ip(scope, headers: dict, platform) -> str:
    client = scope.get("client")
    return forwarded_ip(platform.settings, headers.get("x-forwarded-for"), client[0] if client else "unknown")


def _call_params(call: dict) -> tuple[str, object]:
    params = call.get("params")
    params = params if isinstance(params, dict) else {}
    name = params.get("name", "?")
    return (name if isinstance(name, str) else str(name))[:200], params.get("arguments")


class MCPGate:
    """Wraps the FastMCP HTTP app. On /mcp (see docs/mcp-security.md and app/mcp_governance.py):

    - Origin: a request carrying an Origin header from another site (not this server's, Claude's or
      MCP_ALLOWED_ORIGINS) gets 403, logged. Non-browser MCP clients send none and are not affected.
    - API key or OAuth access token (app/oauth.py): required when settings.mcp_require_key (on Render unless
      MCP_PUBLIC_DEMO=1), optional otherwise. An invalid, expired or revoked one gets 401 with a WWW-Authenticate
      challenge naming the protected resource metadata (RFC 9728), which is how MCP clients such as Claude find
      the sign-in; a company whose trial or subscription ended gets 402.
    - tools/call: rate limits first (per key or connected app: its own limit or 600/min; per company: 1,200/min
      over all of them; anonymous: 60/min per IP and 300/min in total), then the key's tool scope (a connected app
      has the default scope): a call outside it gets a JSON-RPC error and never reaches the tool.
    - tools/list: a key whose scope is narrower than every tool sees only the tools it may call.
    - audit: one mcp_audit_log row per call (ok / error / denied / rate_limited; the SHA-256 of the arguments, never
      the arguments) plus the activity-log row for calls that ran, written after the response has been sent.
    """

    MAX_BODY = 4 * 1024 * 1024
    MAX_CAPTURE = 2 * 1024 * 1024
    MAX_CALLS = 20
    MAX_LIST_BODY = 4 * 1024 * 1024
    DENIED = -32003            # JSON-RPC server-defined error code (-32000..-32099) for a call outside the key scope

    def __init__(self, app, platform):
        self.app = app
        self.platform = platform

    @staticmethod
    async def _send_json(send, status: int, payload, extra_headers=()):
        body = json.dumps(payload).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                   *extra_headers]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _reply(self, send, status: int, message: str, extra_headers=(), data: dict | None = None,
                     code: int = -32001):
        error = {"code": code, "message": message}
        if data:
            error["data"] = data
        await self._send_json(send, status, {"jsonrpc": "2.0", "id": None, "error": error}, extra_headers)

    @staticmethod
    def _key_help(base: str, issuer: str | None) -> dict:
        out = {"get_a_key": f"{base}/#/settings", "header": "Authorization: Bearer <API key>",
               "where": "TradeComply > Settings > AI assistant (MCP) > Create API key (owner or office user)"}
        if issuer:
            out["sign_in"] = (f"MCP clients that support OAuth (Claude custom connectors) can sign in instead: add "
                              f"{issuer}/mcp and approve it in TradeComply.")
        return out

    @staticmethod
    def _challenge(issuer: str | None, error: str | None = None) -> list:
        """RFC 6750 / RFC 9728: where the protected resource metadata is (MCP clients start OAuth from here). Without
        an OAuth issuer (no APP_URL: app/oauth.py issuer_base) it names none, so no client tries to sign in."""
        value = 'Bearer realm="tradecomply"' + (
            f', resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp", scope="tradecomply"'
            if issuer else "") + (f', error="{error}"' if error else "")
        return [(b"www-authenticate", value.encode("latin-1"))]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_mcp(scope.get("path", "")):
            return await self.app(scope, receive, send)
        from . import mcp_governance as gov     # local imports: these modules import this one
        from . import oauth
        from .billing import entitlement
        platform = self.platform
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not gov.origin_allowed(platform, headers, scope):
            gov.log_refused_origin(platform, headers.get("origin"))
            return await self._reply(send, 403, "Origin not allowed: web pages on other sites can't call this MCP "
                                                "server. MCP clients that send no Origin header are not affected.")
        base = public_base(platform.settings, headers, scope.get("scheme"))
        issuer = oauth.issuer_base(platform.settings, headers, scope.get("scheme"))
        token = bearer(headers)
        principal = None
        if token:
            is_oauth = token.startswith(oauth.ACCESS_PREFIX)
            resource = oauth.resource_of(issuer) if issuer else None

            def lookup():
                if is_oauth:
                    p = oauth.principal_from_token(platform, token, resource) if resource else None
                else:
                    p = principal_from_key(platform, token)
                return p, (platform.store.get_tenant(p.tenant_id) if p else None)
            principal, tenant = await anyio.to_thread.run_sync(lookup)
            if principal is None:
                return await self._reply(
                    send, 401, "Invalid, expired or revoked access token: the app should refresh it or sign in to "
                               "TradeComply again." if is_oauth else
                               "Invalid, expired or revoked API key. Create a new key in TradeComply under Settings > "
                               "AI assistant (MCP).", self._challenge(issuer, "invalid_token"),
                    self._key_help(base, issuer))
            if not entitlement(tenant)["ok"]:
                return await self._reply(send, 402, "Subscription required: this account's trial or subscription "
                                                    "has ended. Subscribe in Settings > Billing.")
        elif platform.settings.mcp_require_key:
            return await self._reply(
                send, 401, "An API key is required for this MCP server, unless the MCP client signs in with OAuth "
                           "(a Claude custom connector does). An owner or office user creates a key in TradeComply "
                           "under Settings > AI assistant (MCP) > Create API key, and the AI assistant sends it as "
                           "'Authorization: Bearer <key>'.", self._challenge(issuer), self._key_help(base, issuer))
        scope.setdefault("state", {})["tc_principal"] = principal

        if scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        # Buffer the request so the tool name and arguments can be checked and audited, then replay it downstream.
        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.MAX_BODY:
                return await self._reply(send, 413, "Request too large")
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if not body.strip():
            return await self.app(scope, replay, send)
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError):
            # Fail closed: a body this gate can't read is never passed on, so no tool call can slip past the
            # scope and rate checks because two JSON parsers disagree.
            return await self._reply(send, 400, "Parse error: the request body is not valid JSON", code=-32700)
        msgs = [m for m in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(m, dict)]
        calls = [m for m in msgs if m.get("method") == "tools/call"]
        lists = [m for m in msgs if m.get("method") == "tools/list"]
        if not calls and not lists:
            return await self.app(scope, replay, send)
        if len(calls) > self.MAX_CALLS:
            # MCP (2025-06-18) has no JSON-RPC batching; without this cap one request could write thousands of
            # audit rows.
            return await self._reply(send, 413, f"Too many tool calls in one request (max {self.MAX_CALLS})")

        index = await gov.tool_index(platform)
        policy = principal.scopes if principal else None
        ip = _scope_ip(scope, headers, platform)
        tenant_id = principal.tenant_id if principal else None
        key_id = principal.key_id if principal else None
        grant_id = principal.grant_id if principal else None
        caller = "connected app" if grant_id else "API key"

        def entry(call: dict, status: str, duration_ms=None) -> dict:
            name, args = _call_params(call)
            return {"tenant_id": tenant_id, "api_key_id": key_id, "oauth_grant_id": grant_id, "tool": name,
                    "status": status, "ip": ip, "args_sha256": gov.args_sha256(args if args is not None else {}),
                    "duration_ms": duration_ms}

        async def record(entries, activity=()):
            with anyio.CancelScope(shield=True):      # a client that hangs up must not cost the audit row
                await gov.record(platform, entries, activity)

        if calls:
            if principal:
                limits = [(platform.mcp_key_limiter, f"oauth|{grant_id}" if grant_id else f"key|{key_id}",
                           principal.rate_limit),
                          (platform.mcp_tenant_limiter, f"tenant|{tenant_id}", None)]
            else:
                limits = [(platform.mcp_anon_limiter, f"ip|{ip}", None),
                          (platform.mcp_anon_global_limiter, "all", None)]
            wait = max(lim.retry_after(k, cap) for lim, k, cap in limits)
            if wait:
                await self._reply(send, 429, "Too many tool calls. Slow down"
                                  + ("" if principal else ", or use an API key from the TradeComply app")
                                  + f" and retry in {wait} s.", [(b"retry-after", str(wait).encode())])
                if gov.reject_logging_allowed(platform, limits[0][1]):
                    await record([entry(c, "rate_limited") for c in calls])
                return
            for _ in calls:
                for lim, k, _cap in limits:
                    lim.hit(k)
            refused = [c for c in calls if not gov.scope_allows(policy, _call_params(c)[0], index)]
            if refused:
                errors = []
                for c in calls:
                    name = _call_params(c)[0]
                    if c not in refused:
                        msg = f"Not run: another tool call in this request is not allowed for this {caller}."
                    elif grant_id:
                        msg = (f"Apps connected with TradeComply sign-in can use the read-only tools; {name!r} is "
                               "not one. Use an API key that an owner or office user allowed to use it.")
                    else:
                        msg = (f"This API key is not allowed to use the tool {name!r}. An owner or office user can "
                               "change the key's tools in TradeComply under Settings > AI assistant (MCP) > API "
                               "keys.")
                    if principal is None:
                        msg = f"The tool {name!r} needs an API key that is allowed to use it."
                    errors.append({"jsonrpc": "2.0", "id": c.get("id"), "error": {
                        "code": self.DENIED, "message": msg, "data": {"tool": name, "reason": "not_in_key_scope"}}})
                await self._send_json(send, 200, errors if isinstance(parsed, list) else errors[0])
                if gov.reject_logging_allowed(platform, limits[0][1]):
                    await record([entry(c, "denied") for c in calls])
                return

        allowed = None                    # tools/list: only the tools this key may call
        if lists:
            names = {n for n in index if gov.scope_allows(policy, n, index)}
            if len(names) < len(index):
                allowed = names

        captured, ctype, finished = bytearray(), "", False
        held: dict = {"start": None, "body": bytearray()}
        started, ended = time.monotonic(), None
        user = principal.label if principal else "anonymous (MCP)"

        async def finish():
            nonlocal finished
            if finished or not calls:
                finished = True
                return
            finished = True
            duration = int(((ended or time.monotonic()) - started) * 1000)
            results = _responses_from(bytes(captured), ctype)
            entries, activity = [], []
            for call in calls:
                name, args = _call_params(call)
                res = results.get(_rpc_key(call.get("id"))) or {}
                result = res.get("result") if isinstance(res.get("result"), dict) else {}
                sc = result.get("structuredContent")
                verified = sc.get("verified") if isinstance(sc, dict) and isinstance(sc.get("verified"), bool) \
                    else None
                ok = bool(res) and "error" not in res and not result.get("isError", False)
                entries.append(entry(call, "ok" if ok else "error", duration))
                activity.append({"tenant_id": tenant_id, "user": user, "tool": name, "verified": verified, "ok": ok,
                                 "args": gov.activity_args(args if args is not None else {}), "source": "mcp"})
            await record(entries, activity)

        async def out(message):
            nonlocal ctype, ended
            kind = message["type"]
            if kind == "http.response.start":
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        ctype = v.decode("latin-1")
                if allowed is not None:
                    held["start"] = message           # sent once the tool list has been filtered
                    return
            elif kind == "http.response.body":
                chunk = message.get("body", b"")
                more = message.get("more_body", False)
                if calls and len(captured) < self.MAX_CAPTURE:
                    captured.extend(chunk)
                if held["start"] is not None:
                    held["body"].extend(chunk)
                    if more and len(held["body"]) <= self.MAX_LIST_BODY:
                        return
                    start, raw = held["start"], bytes(held["body"])
                    held["start"] = None
                    new = None if more else gov.filter_tools_list(raw, ctype, allowed)
                    if new is not None:
                        hs = list(start.get("headers", []))
                        had_length = any(k.lower() == b"content-length" for k, _ in hs)
                        hs = [(k, v) for k, v in hs if k.lower() != b"content-length"]
                        if had_length:
                            hs.append((b"content-length", str(len(new)).encode()))
                        start = {**start, "headers": hs}
                    await send(start)
                    message = {**message, "body": raw if new is None else new}
                await send(message)
                if not more and ended is None:
                    ended = time.monotonic()
                return
            await send(message)

        try:
            await self.app(scope, replay, out)
        finally:
            # Only now, after the last byte of the response went out, are the audit rows written: the client never
            # waits for the database.
            await finish()
