"""Sign in with TradeComply: the OAuth 2.1 authorization server for MCP clients (the MCP authorization spec,
2025-06-18 and 2025-11-25). It lets a customer add TradeComply to Claude (web, desktop, phone) as a custom connector
by pasting <base>/mcp, then approve the connection in TradeComply. API keys keep working unchanged.

  GET  /.well-known/oauth-protected-resource[/mcp]  RFC 9728: resource <base>/mcp, authorization server <base>.
  GET  /.well-known/oauth-authorization-server      RFC 8414 metadata (no openid-configuration: MCP clients and
                                                    Claude try this one first and need only one).
  POST /oauth/register   RFC 7591 dynamic client registration (open; Claude registers itself on every new
                         connection). redirect_uris must be https:// or http:// loopback, plain ASCII. Public
                         clients ("none", PKCE only) or confidential ones (client_secret_basic, the RFC default, or
                         _post; the secret is shown once and stored as a SHA-256).
  GET  /oauth/authorize  Checks the client and redirect_uri first (a bad one gets an error page, never a redirect),
                         then response_type=code, PKCE S256 (required), scope, RFC 8707 resource. Other errors are
                         shown on a page with a link back to the app (error, state, iss): anyone can register a
                         redirect address, so nothing redirects to one automatically before the person chose to.
                         Stores a pending request for 10 minutes, ties it to this browser with a cookie, and
                         sends the browser to the web app: /#/connect?request=<id>.
  /api/oauth/requests/{id}[/approve|/deny]   The consent screen's API (session cookie + CSRF, a person only, in
                         the browser that started the sign-in).
                         Approve binds the request to the signed-in person's company and account and returns
                         {redirect_to: redirect_uri?code&state&iss}; the page navigates there itself (the CSP's
                         form-action 'self' would block a form post that redirects to another site).
  POST /oauth/token      authorization_code (single use, 60 s, PKCE; redirect_uri and resource checked when sent)
                         and refresh_token (rotated every time). Access tokens last 1 hour, refresh tokens 30 days
                         from their last use. Opaque random tokens, only their SHA-256 is stored.
  POST /oauth/revoke     RFC 7009 (200 for unknown tokens).
  /api/oauth/connections[/{id}]   A person's connected apps (owners see the whole company's); disconnect.

Access tokens are accepted on /mcp only (auth.MCPGate), never on /api: their audience is <base>/mcp. They act as
the person who approved, with the role and company read from the users row on every call (a removed person or a
password change ends them at once), the default tool scope (every read-only tool), the same rate limits, the
same entitlement check (402) and the same MCP call log rows (oauth_grant_id). Everything returned here carries
Cache-Control: no-store; discovery, register, token and revoke allow cross-origin calls without credentials.
Without a stable public address (APP_URL, https) none of this is offered: see issuer_base.
See docs/mcp-security.md ("Sign in with TradeComply") for the design notes and what differs from the spec's
SHOULDs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import time
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

import anyio
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import Principal, anyone, hash_key, ip_key, is_https, platform_of, public_base

log = logging.getLogger("tradecomply.oauth")

ACCESS_PREFIX, REFRESH_PREFIX, CODE_PREFIX = "tco_", "tcr_", "tcg_"
CLIENT_PREFIX, SECRET_PREFIX = "tcc_", "tcs_"
SCOPE = "tradecomply"
SCOPES = {SCOPE: "Run TradeComply's compliance tools for your company: checks, lookups and read-only lists"}
IGNORED_SCOPES = {"offline_access"}          # refresh tokens are always issued; asking for one is not an error
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")
ACCESS_TTL_S, REFRESH_TTL_S, CODE_TTL_S, REQUEST_TTL_S = 3600, 30 * 86400, 60, 600
REFRESH_GRACE_S = 5        # an old refresh token sent again this soon after use (a duplicate sent at the same
                           # time) gets invalid_grant with nothing revoked; any later reuse ends the connection
PURGE_EVERY_S = 60
MAX_REDIRECT_URIS, MAX_URI, MAX_NAME, MAX_STATE = 5, 512, 100, 1024
MAX_PENDING = 10           # pending sign-in requests kept per app (the newest)
MAX_REGISTRATION, MAX_FORM = 32 * 1024, 16 * 1024
LOOPBACK = ("127.0.0.1", "::1", "localhost")
HOST_RE = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*\.?$")    # an ASCII DNS name or IPv4 address, as a browser shows it
CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")          # BASE64URL(SHA-256(verifier)), no padding
VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")      # RFC 7636 section 4.1
REQUEST_ID = Path(..., min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
PRM_PATH = "/.well-known/oauth-protected-resource"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version",
        "Access-Control-Max-Age": "86400"}
GONE = "This sign-in request has expired or was already used. Go back to the app and connect again."
ELSEWHERE = ("This sign-in request was started in another browser. Go back to the app you are connecting and start "
             "again there, in the browser it opens.")
OFF = ("Sign in with TradeComply isn't set up on this server: the operator needs to set APP_URL to its public https "
       "address.")
ERRORS = {"invalid_grant": "The code or refresh token is invalid, expired, revoked or was issued to another app. "
                           "Sign in again.",
          "invalid_target": "The resource is not this server's MCP address.",
          "invalid_scope": "The scope asks for more than this connection was given."}


# ---------------------------------------------------------------------------- helpers
def issuer_base(settings, headers, scheme: str | None) -> str | None:
    """The OAuth issuer, <base>: APP_URL (Render's RENDER_EXTERNAL_URL) when it is https or loopback; without it,
    the request's own address only on a loopback host (a developer's machine, local mode). Anything else gets no
    OAuth at all (None), like SSO (app/sso.py missing_env): MCP requires every authorization server endpoint to be
    https, and an issuer derived from a public request's Host or X-Forwarded-Host is whatever that caller says."""
    base = settings.app_url or public_base(settings, headers, scheme)
    try:
        p = urlsplit(base)
        host = p.hostname
    except ValueError:
        return None
    if host in LOOPBACK or (settings.app_url and p.scheme == "https"):
        return base.rstrip("/")
    return None


def base_of(request: Request) -> str | None:
    return issuer_base(platform_of(request).settings, request.headers, request.url.scheme)


def canonical_url(url) -> str | None:
    """The RFC 8707 / MCP canonical form, for comparing resource identifiers: lowercase scheme and host, no default
    port, no trailing slash. None for anything that isn't an absolute http(s) URL without a fragment or user info."""
    try:
        p = urlsplit(str(url or "").strip())
        port = p.port
    except ValueError:
        return None
    scheme = p.scheme.lower()
    if scheme not in ("http", "https") or not p.hostname or "#" in str(url) or p.username or p.password:
        return None
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, p.path.rstrip("/"), p.query, ""))


def resource_of(base: str) -> str:
    return canonical_url(base + "/mcp")


def _json(payload, status: int = 200, cors: bool = False, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={**NO_STORE, **(CORS if cors else {}), **(headers or {})})


def _error(error: str, description: str, status: int = 400, headers: dict | None = None) -> JSONResponse:
    """An RFC 6749 section 5.2 error."""
    return _json({"error": error, "error_description": description}, status, cors=True, headers=headers)


def _busy(wait: int) -> JSONResponse:
    return _error("temporarily_unavailable", f"Too many requests. Try again in {wait} s.", 429,
                  {"Retry-After": str(wait)})


def _with_params(uri: str, **params) -> str:
    p = urlsplit(uri)
    extra = urlencode({k: v for k, v in params.items() if v is not None})
    return urlunsplit(p._replace(query=f"{p.query}&{extra}" if p.query else extra))


def _host(uri: str) -> str:
    """The host the consent card and Connected apps show. Only plain-ASCII hosts are ever registered
    (_redirect_ok), so this is the name the browser goes to: a look-alike Unicode domain (IDN homograph) can't be."""
    return urlsplit(uri).hostname or "?"


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def _redirect_ok(uri) -> bool:
    """https:// with a host, or http:// on the loopback interface (127.0.0.1, [::1], localhost); never a #fragment,
    user info, whitespace or control characters (MCP: redirect URIs MUST be https or localhost). ASCII only, and
    the host a plain DNS name or IP address (no %-escapes): the consent card shows the host as the one thing an app
    can't fake, and a Unicode look-alike (https://clаude.ai with a Cyrillic а) would read as claude.ai while the
    browser goes to xn--clude-5ve.ai. Internationalized domains register in their xn-- form, shown as such."""
    if not isinstance(uri, str) or not 0 < len(uri) <= MAX_URI or not uri.isascii() or \
            re.search(r"[\s\x00-\x1f\x7f\\#]", uri):
        return False
    try:
        p = urlsplit(uri)
        p.port
    except ValueError:
        return False
    if p.username or p.password or not p.hostname or not (HOST_RE.match(p.hostname) or p.hostname == "::1"):
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in LOOPBACK)


def _loopback_key(uri: str):
    """A loopback redirect URI without its port: native apps (Claude Code, the MCP SDK) listen on a port chosen
    at sign-in, so the authorization server MUST accept any port (OAuth 2.1 section 8.4.2, RFC 8252 section 7.3)."""
    p = urlsplit(uri)
    return (p.hostname, p.path, p.query) if p.scheme == "http" and p.hostname in LOOPBACK else None


def _match_redirect(client: dict, requested: str | None) -> str | None:
    """The redirect URI to use: an exact match of a registered one, or a registered loopback one on another port.
    Omitted only when exactly one is registered (OAuth 2.1 section 4.1.1)."""
    uris = client["redirect_uris"]
    if requested is None:
        return uris[0] if len(uris) == 1 else None
    if not _redirect_ok(requested):
        return None
    if requested in uris:
        return requested
    key = _loopback_key(requested)
    return requested if key and any(_loopback_key(u) == key for u in uris) else None


def _scope(raw: str | None) -> tuple[str | None, str | None]:
    """(granted scope, None) or (None, why). One scope, "tradecomply"; omitted means it too."""
    unknown = set((raw or "").split()) - set(SCOPES) - IGNORED_SCOPES
    if unknown:
        return None, f"Unknown scope {', '.join(sorted(unknown))[:200]}. This server has one scope: {SCOPE}."
    return SCOPE, None


def _params(pairs) -> tuple[dict, set]:
    """Parameters and the names sent more than once (RFC 6749 section 3.1: they MUST NOT repeat)."""
    out, dups = {}, set()
    for k, v in pairs:
        if k in out:
            dups.add(k)
        out[k] = v
    return out, dups


_purge_lock = threading.Lock()


def _maybe_purge(platform) -> None:
    """Expired requests and tokens, and registrations that never got a grant, go at most once a minute."""
    now = time.monotonic()
    with _purge_lock:
        if now - platform.oauth_purged_at < PURGE_EVERY_S:
            return
        platform.oauth_purged_at = now
    try:
        platform.store.purge_oauth()
    except Exception:          # noqa: BLE001 - cleanup never fails a sign-in
        log.exception("OAuth cleanup failed")


def _ended(g: dict, reason: str) -> None:
    """A replayed code or refresh token ended its connection (the store wrote oauth.revoked to the activity log):
    a sign that a token was copied, so the server log says so too."""
    log.warning("OAuth connection %s (%r, company %s) revoked: %s", g["id"], g["client_name"], g["tenant_id"], reason)


def principal_from_token(platform, plain: str, resource: str) -> Principal | None:
    """The person an access token acts for, if it is live and was issued for `resource` (this server's canonical
    <base>/mcp). Role and email come from the users row now, not from when the app was approved."""
    if not plain.startswith(ACCESS_PREFIX) or len(plain) > 200:
        return None
    g = platform.store.oauth_access(hash_key(plain))
    if not g or g["resource"] != resource:
        return None
    return Principal(tenant_id=g["tenant_id"], role=g["role"], via="oauth", user_id=g["user_id"], email=g["email"],
                     grant_id=g["id"], client_name=g["client_name"])


# ---------------------------------------------------------------------------- discovery
router = APIRouter(tags=["oauth"])


def protected_resource_metadata(base: str) -> dict:
    return {"resource": f"{base}/mcp", "authorization_servers": [base], "bearer_methods_supported": ["header"],
            "scopes_supported": [SCOPE], "resource_name": "TradeComply"}


def authorization_server_metadata(base: str) -> dict:
    # No client_id_metadata_document_supported: Claude would switch to Client ID Metadata Documents, which this
    # server doesn't fetch yet. Dynamic registration is what Claude uses otherwise.
    return {"issuer": base, "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token", "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke", "response_types_supported": ["code"],
            "response_modes_supported": ["query"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "revocation_endpoint_auth_methods_supported": list(AUTH_METHODS), "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True}


@router.get(PRM_PATH, include_in_schema=False)
@router.get(PRM_PATH + "/mcp", include_in_schema=False)
async def resource_metadata(request: Request):
    base = base_of(request)
    return _json(protected_resource_metadata(base), cors=True) if base else _error("not_found", OFF, 404)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def server_metadata(request: Request):
    base = base_of(request)
    return _json(authorization_server_metadata(base), cors=True) if base else _error("not_found", OFF, 404)


async def _preflight():
    return Response(status_code=204, headers=CORS)


for _path in (PRM_PATH, PRM_PATH + "/mcp", "/.well-known/oauth-authorization-server", "/oauth/register",
              "/oauth/token", "/oauth/revoke"):
    router.add_api_route(_path, _preflight, methods=["OPTIONS"], include_in_schema=False)


# ---------------------------------------------------------------------------- registration (RFC 7591)
def _registration(meta) -> tuple[dict | None, tuple[str, str] | None]:
    """Validated client metadata, or (None, (error, description)). Unknown fields are ignored (the MCP SDK sends
    application_type, for example)."""
    if not isinstance(meta, dict):
        return None, ("invalid_client_metadata", "Send the client metadata as a JSON object.")
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
        return None, ("invalid_redirect_uri", f"redirect_uris must list 1 to {MAX_REDIRECT_URIS} addresses.")
    bad = [u for u in uris if not _redirect_ok(u)]
    if bad:
        return None, ("invalid_redirect_uri", f"{str(bad[0])[:100]!r} is not allowed: use an https:// address, or "
                                              "http:// on 127.0.0.1, [::1] or localhost, without a #fragment.")
    method = meta.get("token_endpoint_auth_method") or "client_secret_basic"     # RFC 7591 section 2 default
    if method not in AUTH_METHODS:
        return None, ("invalid_client_metadata", f"token_endpoint_auth_method must be one of "
                                                 f"{', '.join(AUTH_METHODS)}.")
    for field, allowed in (("grant_types", {"authorization_code", "refresh_token"}), ("response_types", {"code"})):
        value = meta.get(field)
        if value is not None and (not isinstance(value, list) or not set(map(str, value)) <= allowed):
            return None, ("invalid_client_metadata", f"{field} may only contain {', '.join(sorted(allowed))}.")
    name = " ".join(str(meta.get("client_name") or "").split())
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:MAX_NAME] or (urlsplit(uris[0]).hostname or "An app")
    return {"name": name, "redirect_uris": list(dict.fromkeys(uris)), "auth_method": method}, None


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request):
    platform = platform_of(request)
    if not base_of(request):
        return _error("temporarily_unavailable", OFF, 503)
    wait = platform.oauth_register_limiter.claim(ip_key(request)) or \
        platform.oauth_register_global_limiter.claim("all")
    if wait:
        return _busy(wait)
    raw = await request.body()
    try:
        meta = json.loads(raw) if len(raw) <= MAX_REGISTRATION else None
    except (ValueError, RecursionError):
        meta = None
    reg, problem = _registration(meta)
    if problem:
        return _error(*problem)
    client_id = CLIENT_PREFIX + secrets.token_urlsafe(24)
    secret = None if reg["auth_method"] == "none" else SECRET_PREFIX + secrets.token_urlsafe(32)
    await anyio.to_thread.run_sync(lambda: platform.store.create_oauth_client(
        client_id, reg["name"], reg["redirect_uris"], reg["auth_method"], hash_key(secret) if secret else None))
    await anyio.to_thread.run_sync(_maybe_purge, platform)
    out = {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": reg["name"],
           "redirect_uris": reg["redirect_uris"], "grant_types": ["authorization_code", "refresh_token"],
           "response_types": ["code"], "token_endpoint_auth_method": reg["auth_method"], "scope": SCOPE}
    if secret:
        out.update(client_secret=secret, client_secret_expires_at=0)
    return _json(out, 201, cors=True)


# ---------------------------------------------------------------------------- authorization endpoint
def _error_page(status: int, message: str, back: str | None = None) -> HTMLResponse:
    """`back`: the app's redirect URI with the OAuth error, offered as a link (the person decides to go there)."""
    e = html.escape
    link = (f'<a class="btn" href="{e(back)}">Back to {e(_host(back))}</a> ' if back else "")
    body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>App can\'t connect - TradeComply</title><link rel="stylesheet" href="/static/styles.css"></head>'
            '<body><main class="auth-wrap"><div class="card"><h1>This app can\'t connect to TradeComply</h1>'
            f'<p>{e(message)}</p><p>Go back to the app and try connecting again. If it keeps happening, ask whoever '
            f'makes the app.</p><p>{link}<a class="btn secondary" href="/">Open TradeComply</a></p></div></main>'
            '</body></html>')
    return HTMLResponse(body, status_code=status, headers=NO_STORE)


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize(request: Request):
    platform = platform_of(request)
    if not base_of(request):
        return _error_page(503, OFF)
    wait = platform.oauth_ip_limiter.claim(ip_key(request))
    if wait:
        return _error_page(429, f"Too many sign-in attempts from this network. Try again in {wait} s.")
    q, dups = _params(parse_qsl(request.url.query, keep_blank_values=True))
    if dups & {"client_id", "redirect_uri"}:
        return _error_page(400, "The sign-in link is malformed (a parameter is repeated).")
    client = await anyio.to_thread.run_sync(platform.store.get_oauth_client, q.get("client_id") or "")
    if not client:
        return _error_page(400, "This app isn't registered with TradeComply, or its registration has expired.")
    redirect_uri = _match_redirect(client, q.get("redirect_uri"))
    if not redirect_uri:
        return _error_page(400, "The app asked to return to an address it didn't register, so TradeComply won't "
                                "send you there.")
    # From here on, errors go back to the app (OAuth 2.1 section 4.1.2.1) with iss (RFC 9207), but through a link
    # the person follows: with open registration any address can be a client's, and an automatic redirect would
    # make this server an open redirector (MCP security considerations: redirect automatically only to trusted
    # URIs; OAuth 2.1 section 7.12.2).
    base, state = base_of(request), q.get("state")

    def fail(error: str, description: str):
        return _error_page(400, description, _with_params(redirect_uri, error=error, error_description=description,
                                                          state=state, iss=base))
    if dups:
        return fail("invalid_request", f"Repeated parameter: {', '.join(sorted(dups))[:200]}.")
    if q.get("response_type") != "code":
        return fail("unsupported_response_type", "Only response_type=code is supported.")
    challenge = q.get("code_challenge") or ""
    if q.get("code_challenge_method") != "S256" or not CHALLENGE_RE.match(challenge):
        return fail("invalid_request", "PKCE is required: send code_challenge (BASE64URL of the SHA-256 of the "
                                       "code verifier) with code_challenge_method=S256.")
    if state is not None and len(state) > MAX_STATE:
        return fail("invalid_request", f"state is longer than {MAX_STATE} characters.")
    scope, why = _scope(q.get("scope"))
    if why:
        return fail("invalid_scope", why)
    resource = resource_of(base)
    if "resource" in q and canonical_url(q["resource"]) != resource:
        return fail("invalid_target", f"This server's MCP address is {base}/mcp.")
    request_id = secrets.token_urlsafe(32)
    await anyio.to_thread.run_sync(lambda: platform.store.create_oauth_request(
        hash_key(request_id), client["id"], redirect_uri=redirect_uri, state=state, code_challenge=challenge,
        scope=scope, resource=resource, ttl_s=REQUEST_TTL_S, keep=MAX_PENDING))
    await anyio.to_thread.run_sync(_maybe_purge, platform)
    # The web app signs the person in (if needed) and shows the consent card. The id travels in the URL fragment;
    # the web app's calls carry it in their path, which the access log redacts (main.RedactTokens). Only this
    # browser can use it: the request is tied to it by a cookie that only this server can make.
    r = RedirectResponse(f"/#/connect?request={request_id}", status_code=302, headers=NO_STORE)
    name, value = _binding(platform, request_id)
    r.set_cookie(name, value, max_age=REQUEST_TTL_S, path="/api/oauth/requests/", httponly=True, samesite="lax",
                 secure=is_https(request))
    return r


def _binding(platform, request_id: str) -> tuple[str, str]:
    """The cookie tying a pending request to the browser that opened /oauth/authorize (name and value from a MAC of
    the request id). Without it, the id alone (in an address bar, a link someone was sent, a log) would let anyone
    signed in approve the request as themselves, connecting the app that started it to their company. Same-browser
    trips through SSO or sign-up keep it."""
    mac = platform.signer.mac("oauth-request", request_id)
    return f"tc_oauth_{mac[:12]}", mac[12:44]



# ---------------------------------------------------------------------------- client authentication + token
async def _form(request: Request) -> tuple[dict | None, JSONResponse | None]:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/x-www-form-urlencoded":
        return None, _error("invalid_request", "Send the parameters form-encoded (application/x-www-form-urlencoded).")
    raw = await request.body()
    try:
        form, dups = _params(parse_qsl(raw.decode("utf-8"), keep_blank_values=True)) if len(raw) <= MAX_FORM \
            else (None, None)
    except UnicodeDecodeError:
        form, dups = None, None
    if form is None:
        return None, _error("invalid_request", "The request body is too large or not UTF-8.")
    if dups:
        return None, _error("invalid_request", f"Repeated parameter: {', '.join(sorted(dups))[:200]}.")
    return form, None


async def _client(request: Request, form: dict, token: str | None = None) -> tuple[dict | None, JSONResponse | None]:
    """The authenticated client (RFC 6749 section 2.3): a public client sends its client_id (or, refreshing or
    revoking, may leave it out: OAuth 2.1 section 4.3.1, the token names its client); a confidential one the secret
    the way it registered (HTTP Basic with form-encoded id and secret, or client_secret in the body). Nothing here
    counts against the app's own limit: a client_id is no secret, so anyone could use that up."""
    platform = platform_of(request)
    header = request.headers.get("authorization") or ""
    basic_id = basic_secret = None
    if header:
        scheme, _, value = header.partition(" ")
        try:
            if scheme.lower() != "basic":
                raise ValueError
            cid, sep, sec = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
            if not sep:
                raise ValueError
            basic_id, basic_secret = unquote_plus(cid), unquote_plus(sec)
        except ValueError:          # binascii.Error and UnicodeDecodeError are ValueErrors
            return None, _error("invalid_client", "Unreadable client credentials.", 401,
                                {"WWW-Authenticate": 'Basic realm="tradecomply"'})
    body_id, body_secret = form.get("client_id"), form.get("client_secret")
    if basic_id is not None and (body_secret is not None or (body_id and body_id != basic_id)):
        return None, _error("invalid_request", "Authenticate the client one way only.")
    client_id = basic_id if basic_id is not None else body_id
    if not client_id and body_secret is None and token:      # then only a public client ("none") can match below
        client_id = await anyio.to_thread.run_sync(platform.store.oauth_token_client, hash_key(token))
    client = await anyio.to_thread.run_sync(platform.store.get_oauth_client, client_id or "") if client_id else None
    used = "client_secret_basic" if basic_id is not None else "client_secret_post" if body_secret is not None \
        else "none"
    secret = basic_secret if basic_id is not None else body_secret
    if not client or used != client["auth_method"] or (
            used != "none" and not hmac.compare_digest(hash_key(secret), client["secret_hash"])):
        return None, _error("invalid_client", "Unknown client, or wrong client credentials.", 401,
                            {"WWW-Authenticate": 'Basic realm="tradecomply"'} if header else None)
    return client, None


def _tokens() -> tuple[str, str]:
    return ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32)


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request):
    platform = platform_of(request)
    if not base_of(request):
        return _error("temporarily_unavailable", OFF, 503)
    wait = platform.oauth_ip_limiter.claim(ip_key(request))
    if wait:
        return _busy(wait)
    form, problem = await _form(request)
    if problem:
        return problem
    grant_type = form.get("grant_type")
    if not grant_type:
        return _error("invalid_request", "grant_type is required.")
    if grant_type not in ("authorization_code", "refresh_token"):
        return _error("unsupported_grant_type", "grant_type must be authorization_code or refresh_token.")
    client, problem = await _client(request, form, form.get("refresh_token") if grant_type == "refresh_token"
                                    else None)
    if problem:
        return problem
    # The app's own limit counts only tokens it was actually given: only whoever holds a valid code or refresh
    # token (or a confidential app's secret) can fill it, never someone who merely knows the client_id.
    app_key = f"client|{client['id']}"
    wait = platform.oauth_client_limiter.retry_after(app_key)
    if wait:
        return _busy(wait)
    store, resource = platform.store, form.get("resource")
    access, refresh = _tokens()
    if grant_type == "authorization_code":
        code, verifier, redirect_uri = form.get("code") or "", form.get("code_verifier") or "", form.get("redirect_uri")
        if not code or not VERIFIER_RE.match(verifier):
            return _error("invalid_request", "code and code_verifier (43 to 128 characters) are required.")

        def check(g: dict) -> str | None:
            # OAuth 2.1 dropped redirect_uri from the token request; when a client still sends it, it must match.
            if g["client_id"] != client["id"] or not hmac.compare_digest(_s256(verifier), g["code_challenge"]) or \
                    (redirect_uri is not None and redirect_uri != g["redirect_uri"]):
                return "invalid_grant"
            return "invalid_target" if resource is not None and canonical_url(resource) != g["resource"] else None
        g, error = await anyio.to_thread.run_sync(lambda: store.redeem_oauth_code(
            hash_key(code), check, hash_key(access), hash_key(refresh), ACCESS_TTL_S, REFRESH_TTL_S))
        reason = "a one-time code was used twice"
    else:
        old, asked = form.get("refresh_token") or "", form.get("scope")
        if not old:
            return _error("invalid_request", "refresh_token is required.")

        def check(g: dict) -> str | None:
            if g["client_id"] != client["id"]:
                return "invalid_grant"
            if resource is not None and canonical_url(resource) != g["resource"]:
                return "invalid_target"
            wider = set((asked or "").split()) - IGNORED_SCOPES - set(g["scope"].split())
            return "invalid_scope" if wider else None          # the same scope or less (OAuth 2.1 section 4.3.1)
        g, error = await anyio.to_thread.run_sync(lambda: store.rotate_oauth_refresh(
            hash_key(old), check, hash_key(access), hash_key(refresh), ACCESS_TTL_S, REFRESH_TTL_S, REFRESH_GRACE_S))
        reason = "a used refresh token was sent again"
    if error:
        if g:
            _ended(g, reason)
        return _error(error, ERRORS[error])
    platform.oauth_client_limiter.hit(app_key)
    return _json({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_S,
                  "refresh_token": refresh, "scope": g["scope"]}, cors=True)


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request):
    platform = platform_of(request)
    if not base_of(request):
        return _error("temporarily_unavailable", OFF, 503)
    wait = platform.oauth_ip_limiter.claim(ip_key(request))
    if wait:
        return _busy(wait)
    form, problem = await _form(request)
    if problem:
        return problem
    client, problem = await _client(request, form, form.get("token"))
    if problem:
        return problem
    if not form.get("token"):
        return _error("invalid_request", "token is required.")
    await anyio.to_thread.run_sync(platform.store.revoke_oauth_token, hash_key(form["token"]), client["id"])
    return _json({}, cors=True)            # 200 whether or not the token existed (RFC 7009 section 2.2)


# ---------------------------------------------------------------------------- consent and connected apps (web app)
api = APIRouter(prefix="/api/oauth", tags=["oauth"])


def _person(p: Principal) -> None:
    """Only a person signed in to the web app approves or manages connections, never an API key (OWASP MCP02)."""
    if p.via != "cookie":
        raise HTTPException(403, "Sign in to TradeComply to manage connected apps; an API key can't.")


@api.get("/requests/{request_id}")
def view_request(request: Request, request_id: str = REQUEST_ID, p: Principal = Depends(anyone)):
    """What the consent card shows. The app's name is whatever it registered (anyone can call an app "Claude"),
    so the address it returns to is given too: that is the part an app can't fake."""
    store = platform_of(request).store
    req = _pending(request, request_id, p)
    tenant = store.get_tenant(p.tenant_id) or {}
    return {"client_name": req["client_name"], "redirect_host": _host(req["redirect_uri"]),
            "redirect_uri": req["redirect_uri"],
            "local_only": all(_loopback_key(u) for u in req["redirect_uris"]),     # an app on this computer
            "scopes": [{"id": s, "label": SCOPES[s]} for s in req["scope"].split()],
            "user_email": p.email, "company_name": tenant.get("company_name"), "role": p.role,
            "expires_in": max(0, int(req["expires"] - time.time()))}


@api.post("/requests/{request_id}/approve")
def approve(request: Request, request_id: str = REQUEST_ID, p: Principal = Depends(anyone)):
    _pending(request, request_id, p)
    iss = _iss(request)
    store = platform_of(request).store
    user = store.user_by_id(p.user_id)
    code = CODE_PREFIX + secrets.token_urlsafe(32)
    req = store.approve_oauth_request(hash_key(request_id), user, hash_key(code), CODE_TTL_S) if user else None
    if not req:
        raise HTTPException(404, GONE)
    store.audit(p.tenant_id, p.label, "oauth.connect", {"connection_id": req["grant_id"], "app": req["client_name"],
                                                        "returns_to": _host(req["redirect_uri"])}, None, source="api")
    return {"redirect_to": _with_params(req["redirect_uri"], code=code, state=req["state"], iss=iss)}


@api.post("/requests/{request_id}/deny")
def deny(request: Request, request_id: str = REQUEST_ID, p: Principal = Depends(anyone)):
    name = _pending(request, request_id, p)["client_name"]
    iss = _iss(request)
    store = platform_of(request).store
    req = store.take_oauth_request(hash_key(request_id))
    if not req:
        raise HTTPException(404, GONE)
    store.audit(p.tenant_id, p.label, "oauth.deny", {"app": name, "returns_to": _host(req["redirect_uri"])}, None,
                source="api")
    return {"redirect_to": _with_params(req["redirect_uri"], error="access_denied",
                                        error_description="The person declined to connect the app.",
                                        state=req["state"], iss=iss)}


def _pending(request: Request, request_id: str, p: Principal) -> dict:
    """The pending request, for a signed-in person in the browser that started it: 404 when it is gone or expired
    (whichever browser asks), 403 in another browser."""
    _person(p)
    req = platform_of(request).store.get_oauth_request(hash_key(request_id))
    if not req:
        raise HTTPException(404, GONE)
    name, value = _binding(platform_of(request), request_id)
    if not hmac.compare_digest(request.cookies.get(name) or "", value):
        raise HTTPException(403, ELSEWHERE)
    return req


def _iss(request: Request) -> str:
    base = base_of(request)
    if not base:
        raise HTTPException(503, OFF)
    return base


@api.get("/connections")
def connections(request: Request, p: Principal = Depends(anyone)):
    """Your connected apps; an owner sees everyone's in the company."""
    _person(p)
    rows = platform_of(request).store.list_oauth_grants(p.tenant_id, None if p.role == "owner" else p.user_id)
    return [{"id": r["id"], "client_name": r["client_name"], "user_email": r["user_email"],
             "redirect_host": _host(r["redirect_uri"]), "created": r["created"], "last_used": r["last_used"],
             "scopes": [{"id": s, "label": SCOPES.get(s, s)} for s in r["scope"].split()],
             "mine": r["user_id"] == p.user_id} for r in rows]


@api.delete("/connections/{grant_id}")
def disconnect(request: Request, grant_id: int = Path(..., ge=1, le=2 ** 63 - 1), p: Principal = Depends(anyone)):
    """Disconnect now: every token of the connection stops working. Your own, or (owners) anyone's."""
    _person(p)
    store = platform_of(request).store
    if not store.revoke_oauth_grant(p.tenant_id, grant_id, None if p.role == "owner" else p.user_id):
        raise HTTPException(404, "Connection not found")
    store.audit(p.tenant_id, p.label, "oauth.disconnect", {"connection_id": grant_id}, None, source="api")
    return {"ok": True}
