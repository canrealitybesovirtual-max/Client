"""Enterprise single sign-on (SSO) for the web app with OpenID Connect: one identity provider per company (Okta,
Microsoft Entra ID, Google, or any OpenID Provider with a discovery document).

How it works
- The owner enters the provider's issuer URL, the client ID and client secret of an OIDC web application they
  registered there (redirect URI: <APP_URL>/api/auth/sso/callback), and the company's email domains
  (PUT /api/sso/config). TradeComply reads <issuer>/.well-known/openid-configuration (never a hard-coded provider
  URL), checks that its `issuer` is exactly the configured one, and fetches the signing keys (jwks_uri). The client
  secret is stored encrypted (SecretBox, sso_context(tenant)).
- Sign-in: POST /api/auth/sso/start {"email"} (the login page) or GET /api/auth/sso/start?company=<tenant id>
  (a bookmark or the provider's "initiate login URI"; optional iss= must match the configured issuer, per OIDC Core
  section 4) sends the browser to the provider's authorization_endpoint with response_type=code,
  scope="openid email profile", a signed single-use `state` (bound to the company, the issuer and client ID, the
  redirect URI and this browser through an HttpOnly cookie, valid 10 minutes), an OIDC `nonce` and a PKCE S256
  code_challenge (the verifier travels encrypted inside the state).
- GET /api/auth/sso/callback checks the state (signature, expiry, browser cookie, not used before) and, when the
  provider sends one, the RFC 9207 `iss` response parameter (a different issuer is refused; a missing one is
  accepted, because the state already fixes which provider's token endpoint gets the code), exchanges the code at the token_endpoint (client_secret_basic, or
  client_secret_post when that is the only one the provider lists; PKCE code_verifier), and validates the ID token:
  signature with a key from jwks_uri (RS256/384/512, PS256/384/512, ES256/384/512 only, and only those the provider
  lists; never "none" or HMAC), `iss` exactly the issuer, `aud` this client ID and no other audience, `azp` (when
  present) this client ID, `exp` in the future and `iat` at most 10 minutes old (60 s clock leeway), `nonce` equal
  to the one sent. Keys are cached per jwks_uri (Cache-Control max-age, 5 minutes to 24 hours, default 1 hour);
  a token signed with an unknown `kid` refreshes the keys at most once a minute (key rotation).
- The user: the email must be verified by the provider (`email_verified` true; for Microsoft Entra ID, whose email
  claim is not verified, the optional claim `xms_edov` true), its domain must be one of the company's allowed
  domains (for Google also the `hd` hosted-domain claim), and it must belong to an EXISTING user of THAT company:
  never another company's user, and no user is created. When the ID token has no email or verification flag (an
  Okta "thin" token), the UserInfo endpoint is asked with the access token and its `sub` must equal the ID token's.
  Then the normal TradeComply session cookie is set (the same session as a password login).
- Enforcement ("SSO required", owner only): password sign-in is refused for everyone in the company except owners
  (app/auth.py login calls password_login_refusal). It can be turned on only after someone in the company has
  signed in with SSO under the current issuer and client ID, and turning it on ends every non-owner session, so
  people sign in again through SSO. API keys (AI agents) keep working; revoke them separately.

Break-glass (how an owner recovers when the identity provider is down or misconfigured)
- Owners can ALWAYS sign in with their TradeComply password, even when SSO is required. Each such sign-in is written
  to the activity log as `sso.break_glass_login` (and so reaches Splunk when that export is on). The owner can then
  fix or delete the SSO settings (Settings > Single sign-on, or DELETE /api/sso/config) or turn enforcement off.
- If no owner can sign in at all, the server operator runs `python -m app.sso unenforce <tenant id>` (against the
  same DATABASE_URL), which turns enforcement off and logs `sso.unenforce`; `python -m app.sso status <tenant id>`
  shows the settings without the secret.

Operator setup: SSO needs a stable public https address for the redirect URI: APP_URL (or Render's
RENDER_EXTERNAL_URL, which Render sets on every web service). Without it the settings page says SSO is not set up
on this server, and nothing else changes.

Sources (read 2026-11-26; openid.net and developer sites are refused by this build environment's network, so they
were read through Context7's index of the official pages; re-check on the live pages):
- OpenID Connect Core 1.0 (openid.net/specs/openid-connect-core-1_0.html): 3.1.2.1 authentication request (nonce:
  "Sufficient entropy MUST be present"), 3.1.3.7 ID Token Validation steps 1-13 (iss exact match, aud contains the
  client_id and no untrusted audiences, azp, alg default RS256, exp, iat "can be used to reject tokens that were
  issued too far away from the current time", nonce), 4 initiating login from a third party (iss, login_hint),
  5.1 email_verified ("the OP took affirmative steps to ensure that this e-mail address was controlled by the
  End-User"), 5.3 UserInfo, 5.7 (only iss + sub are stable; "an Issuer MAY re-use an email Claim Value").
- OpenID Connect Discovery 1.0 (NOT read this session: from knowledge, to re-check): the document is at the issuer
  with any trailing "/" removed plus "/.well-known/openid-configuration"; its issuer MUST be identical to the issuer
  used to fetch it and to the iss of ID tokens (section 4.3); token_endpoint_auth_methods_supported defaults to
  client_secret_basic. RFC 9207 (iss authorization response parameter) and RFC 6749 2.3.1 (form-encoding the
  client id and secret for Basic auth) are also from knowledge.
- Okta (developer.okta.com): /oauth2/v1/authorize and /oauth2/v1/token with code_challenge_method=S256 and
  code_verifier; org authorization server issuer https://{yourOktaDomain}, custom ones
  https://{yourOktaDomain}/oauth2/{authServerId}; "validate ID tokens": iss, aud, iat, exp, nonce; the email scope
  gives email and email_verified; "The ID token ... may not include all claims associated with the requested
  scopes. The /userinfo endpoint always contains a full set of claims".
- Microsoft Entra ID (learn.microsoft.com/en-us/entra/identity-platform): id-token-claims-reference (email "isn't
  guaranteed to be correct ... Never use it for authorization", nonce), optional-claims-reference (xms_edov "whether
  the user's email domain owner has been verified"), access-tokens "Validate the issuer" (tenant-specific metadata
  https://login.microsoftonline.com/{tenant-id}/v2.0/.well-known/openid-configuration: exact issuer match;
  /common and /organizations return a {tenantid} template, which TradeComply refuses).
- Google (developers.google.com/identity/openid-connect/openid-connect and /reference): discovery at
  https://accounts.google.com/.well-known/openid-configuration (issuer https://accounts.google.com,
  token_endpoint_auth_methods_supported client_secret_post and client_secret_basic, S256 PKCE); "Verify that the
  value of the iss claim in the ID token is equal to https://accounts.google.com or accounts.google.com"; the `hd`
  claim "must be used when restricting access to a resource to only members of certain domains".
- PyJWT 2.x (github.com/jpadilla/pyjwt docs/api.rst): jwt.decode(algorithms=fixed list, audience, issuer, leeway,
  options={"require": [...]}); "Do not compute the algorithms parameter based on the alg from the token itself";
  exp/iat are verified only if present, hence `require`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets as _secrets
import threading
import time
from typing import Optional
from urllib.parse import quote, urlencode, urlsplit

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import EMAIL_RE, Principal, RateLimiter, client_ip, is_https, owner_only, platform_of, set_session
from .billing import require_entitled
from .connectors import code_challenge, new_code_verifier
from .crypto import DecryptError, pkce_context, sso_context
from .safe_http import SafeHTTPError, UnsafeURLError, check_url
from .store import DOMAIN_RE, now_iso

log = logging.getLogger("tradecomply.sso")

STATE_TTL_S = 600
COOKIE = "tc_sso"
COOKIE_PATH = "/api/auth/sso"
CALLBACK_PATH = "/api/auth/sso/callback"
SCOPES = "openid email profile"
LEEWAY_S = 60
MAX_TOKEN_AGE_S = 600                 # iat older than this (plus leeway) is refused
MAX_ID_TOKEN = 16 * 1024
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")
EC_CURVES = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
GOOGLE_ISSUER = "https://accounts.google.com"
CACHE_DEFAULT_S, CACHE_MIN_S, CACHE_MAX_S = 3600, 300, 86400
JWKS_REFRESH_MIN_S = 60
WELL_KNOWN = "/.well-known/openid-configuration"
CLIENT_ID_RE = re.compile(r"^[\x21-\x7e]{1,300}$")          # printable, no spaces
CODE_RE = re.compile(r"^[a-z_]{1,40}$")
LOGIN_PATH = "/#/login"
SUCCESS_PATH = "/#/dashboard"


class SSOError(Exception):
    """A failure whose message is safe to show (no tokens, codes, secrets or response bodies). `code` is a short
    machine reason, written to the activity log."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


# ---------------------------------------------------------------------------- operator setup
def missing_env(platform) -> list[str]:
    """Server settings SSO needs: a stable public https APP_URL for the redirect URI."""
    url = platform.settings.app_url
    if not url:
        return ["APP_URL"]
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        return ["APP_URL"]
    return []


def redirect_uri(platform) -> str:
    return platform.settings.app_url.rstrip("/") + CALLBACK_PATH


def _require_operator(platform) -> None:
    if missing_env(platform):
        raise SSOError("not_configured", "Single sign-on isn't set up on this server yet (the operator needs to set "
                                         "APP_URL to the server's public https address).", 503)


# ---------------------------------------------------------------------------- issuer, discovery and keys
def normalize_issuer(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        raise SSOError("bad_issuer", "Enter the issuer URL (at most 500 characters).")
    if "{" in text or "}" in text:
        raise SSOError("bad_issuer", "Enter your own tenant's issuer, not a template: for Microsoft Entra ID "
                                     "https://login.microsoftonline.com/<your tenant ID>/v2.0.")
    try:
        check_url(text, resolver=None)
    except UnsafeURLError as e:
        raise SSOError("bad_issuer", f"Issuer URL: {e}") from None
    parts = urlsplit(text)
    if parts.query or parts.fragment:
        raise SSOError("bad_issuer", "The issuer URL can't have ? or # in it.")
    if text.rstrip("/").endswith(WELL_KNOWN):
        raise SSOError("bad_issuer", "Enter the issuer itself, without /.well-known/openid-configuration.")
    return text


def _max_age(resp) -> int:
    m = re.search(r"max-age=(\d+)", resp.headers.get("cache-control") or "")
    if not m:
        return CACHE_DEFAULT_S
    return max(CACHE_MIN_S, min(CACHE_MAX_S, int(m.group(1))))


class _Cache:
    """Discovery documents per issuer and signing keys per jwks_uri, for one platform (thread-safe)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.discovery: dict[str, tuple[dict, float]] = {}
        self.jwks: dict[str, dict] = {}          # uri -> {"keys": [jwk...], "expires": t, "fetched": t}


_cache_guard = threading.Lock()


def _cache(platform) -> _Cache:
    with _cache_guard:
        c = getattr(platform, "_oidc_cache", None)
        if c is None:
            c = platform._oidc_cache = _Cache()
        return c


def _get_json(platform, url: str, what: str, headers: dict | None = None):
    """GET a JSON document (no redirects). Returns (response, body). Raises SSOError."""
    host = urlsplit(url).hostname
    try:
        with platform.http() as http:
            r = http.get(url, headers={"accept": "application/json", **(headers or {})}, retries=1,
                         follow_redirects=False, max_bytes=512 * 1024)
    except UnsafeURLError as e:
        raise SSOError("unsafe_url", f"TradeComply can't call {what} at {host}: {e}") from None
    except SafeHTTPError as e:
        raise SSOError("unreachable", f"Could not read {what} from {host}: {e}", 502) from None
    if r.status_code != 200:
        raise SSOError("http_error", f"{host} answered HTTP {r.status_code} for {what}.", 502)
    try:
        body = r.json()
    except ValueError:
        raise SSOError("not_json", f"{what} from {host} is not JSON.", 502) from None
    if not isinstance(body, dict):
        raise SSOError("not_json", f"{what} from {host} is not a JSON object.", 502)
    return r, body


def _https_endpoint(doc: dict, name: str, required: bool = True) -> str | None:
    value = doc.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SSOError("bad_discovery", f"The provider's discovery document has no {name}.")
    try:
        check_url(value, resolver=None)
    except UnsafeURLError as e:
        raise SSOError("bad_discovery", f"The provider's {name} is not usable: {e}") from None
    return value


def validate_discovery(doc: dict, issuer: str) -> dict:
    """Check a discovery document against the configured issuer. Returns the fields TradeComply uses."""
    got = doc.get("issuer")
    if not isinstance(got, str) or got != issuer:
        hint = f" It says its issuer is {got}." if isinstance(got, str) and len(got) <= 300 else ""
        raise SSOError("issuer_mismatch", "The provider's discovery document is for a different issuer than the one "
                                          f"entered.{hint} Enter the issuer exactly as the provider states it.")
    if "{" in got:
        raise SSOError("bad_issuer", "This issuer is a multi-tenant template; enter your own tenant's issuer.")
    out = {"issuer": got, "authorization_endpoint": _https_endpoint(doc, "authorization_endpoint"),
           "token_endpoint": _https_endpoint(doc, "token_endpoint"), "jwks_uri": _https_endpoint(doc, "jwks_uri"),
           "userinfo_endpoint": _https_endpoint(doc, "userinfo_endpoint", required=False)}
    types = doc.get("response_types_supported")
    if isinstance(types, list) and "code" not in types:
        raise SSOError("bad_discovery", "The provider doesn't offer the authorization code flow (response type code).")
    algs = doc.get("id_token_signing_alg_values_supported")
    listed = [a for a in algs if isinstance(a, str)] if isinstance(algs, list) else ["RS256"]
    usable = [a for a in ALLOWED_ALGS if a in listed]
    if not usable:
        raise SSOError("bad_discovery", "The provider signs ID tokens only with algorithms TradeComply doesn't "
                                        f"accept (it accepts {', '.join(ALLOWED_ALGS)}).")
    out["algorithms"] = usable
    methods = doc.get("token_endpoint_auth_methods_supported")
    methods = [m for m in methods if isinstance(m, str)] if isinstance(methods, list) else ["client_secret_basic"]
    if "client_secret_basic" in methods:
        out["token_auth"] = "client_secret_basic"
    elif "client_secret_post" in methods:
        out["token_auth"] = "client_secret_post"
    else:
        raise SSOError("bad_discovery", "The provider doesn't accept a client secret at its token endpoint "
                                        "(client_secret_basic or client_secret_post).")
    pkce = doc.get("code_challenge_methods_supported")
    if isinstance(pkce, list) and "S256" not in pkce:
        raise SSOError("bad_discovery", "The provider doesn't support PKCE with S256.")
    return out


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + WELL_KNOWN


def discover(platform, issuer: str, force: bool = False) -> dict:
    """The validated discovery document of an issuer, cached."""
    cache = _cache(platform)
    with cache.lock:
        hit = cache.discovery.get(issuer)
        if hit and not force and hit[1] > time.time():
            return hit[0]
    resp, doc = _get_json(platform, discovery_url(issuer), "the discovery document")
    meta = validate_discovery(doc, issuer)
    with cache.lock:
        cache.discovery[issuer] = (meta, time.time() + _max_age(resp))
    return meta


def _usable_jwk(k) -> bool:
    return (isinstance(k, dict) and k.get("kty") in ("RSA", "EC") and k.get("use", "sig") == "sig"
            and (not isinstance(k.get("key_ops"), list) or "verify" in k["key_ops"]))


def signing_keys(platform, jwks_uri: str, force: bool = False) -> list[dict]:
    cache = _cache(platform)
    with cache.lock:
        hit = cache.jwks.get(jwks_uri)
        if hit and not force and hit["expires"] > time.time():
            return hit["keys"]
    resp, doc = _get_json(platform, jwks_uri, "the signing keys")
    keys = [k for k in doc.get("keys") or [] if _usable_jwk(k)] if isinstance(doc.get("keys"), list) else []
    if not keys:
        raise SSOError("no_keys", "The provider published no RSA or EC signing keys.", 502)
    with cache.lock:
        cache.jwks[jwks_uri] = {"keys": keys, "expires": time.time() + _max_age(resp), "fetched": time.time()}
    return keys


def _key_fits(jwk_: dict, alg: str) -> bool:
    if jwk_.get("alg") and jwk_["alg"] != alg:
        return False
    if alg.startswith(("RS", "PS")):
        return jwk_.get("kty") == "RSA"
    return jwk_.get("kty") == "EC" and jwk_.get("crv") == EC_CURVES.get(alg)


def _pick_key(keys: list[dict], kid: str | None, alg: str):
    if kid is not None:
        found = [k for k in keys if k.get("kid") == kid and _key_fits(k, alg)]
    else:
        found = [k for k in keys if _key_fits(k, alg)]
        if len(found) != 1:
            return None
    return found[0] if found else None


def _signing_key(platform, jwks_uri: str, kid: str | None, alg: str):
    keys = signing_keys(platform, jwks_uri)
    jwk_ = _pick_key(keys, kid, alg)
    if jwk_ is None:
        cache = _cache(platform)
        with cache.lock:
            fetched = (cache.jwks.get(jwks_uri) or {}).get("fetched", 0)
        if time.time() - fetched >= JWKS_REFRESH_MIN_S:          # the provider may have rotated its keys
            jwk_ = _pick_key(signing_keys(platform, jwks_uri, force=True), kid, alg)
    if jwk_ is None:
        raise SSOError("unknown_key", "The ID token was signed with a key the provider doesn't publish.")
    try:
        return jwt.PyJWK(jwk_, algorithm=alg).key
    except jwt.PyJWTError:
        raise SSOError("bad_key", "The provider's signing key can't be used.", 502) from None


def _truthy(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def validate_id_token(platform, id_token, *, meta: dict, client_id: str, nonce: str) -> dict:
    """OIDC Core 3.1.3.7 for an ID token from the token endpoint. Returns the claims. Raises SSOError."""
    if not isinstance(id_token, str) or not id_token or len(id_token) > MAX_ID_TOKEN or id_token.count(".") != 2:
        raise SSOError("bad_id_token", "The provider's answer had no usable ID token (encrypted ID tokens are not "
                                       "supported).")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise SSOError("bad_id_token", "The ID token can't be read.") from None
    alg = header.get("alg")
    if alg not in meta["algorithms"]:
        raise SSOError("bad_alg", "The ID token is signed with an algorithm TradeComply doesn't accept.")
    if header.get("crit"):
        raise SSOError("bad_id_token", "The ID token uses extensions TradeComply doesn't understand.")
    kid = header.get("kid") if isinstance(header.get("kid"), str) else None
    key = _signing_key(platform, meta["jwks_uri"], kid, alg)
    issuer = meta["issuer"]
    issuers = (issuer, "accounts.google.com") if issuer == GOOGLE_ISSUER else issuer
    try:
        claims = jwt.decode(id_token, key=key, algorithms=[alg], audience=client_id, issuer=issuers,
                            leeway=LEEWAY_S, options={"require": ["iss", "sub", "aud", "exp", "iat"]})
    except jwt.ExpiredSignatureError:
        raise SSOError("expired", "The sign-in took too long (the ID token expired). Try again.") from None
    except jwt.InvalidAudienceError:
        raise SSOError("bad_aud", "The ID token was issued for a different application.") from None
    except jwt.InvalidIssuerError:
        raise SSOError("bad_iss", "The ID token came from a different issuer.") from None
    except jwt.ImmatureSignatureError:
        raise SSOError("not_yet_valid", "The ID token isn't valid yet (check the server clocks).") from None
    except jwt.PyJWTError:
        raise SSOError("bad_signature", "The ID token's signature or claims are not valid.") from None
    aud = claims.get("aud")
    if isinstance(aud, list) and set(aud) != {client_id}:
        raise SSOError("bad_aud", "The ID token is also meant for other applications.")
    if "azp" in claims and claims["azp"] != client_id:
        raise SSOError("bad_azp", "The ID token was issued to a different application (azp).")
    try:
        iat = float(claims["iat"])
    except (TypeError, ValueError):
        raise SSOError("bad_iat", "The ID token has no valid issue time.") from None
    if iat < time.time() - MAX_TOKEN_AGE_S - LEEWAY_S:
        raise SSOError("stale", "The ID token is too old. Try signing in again.")
    sent = claims.get("nonce")
    if not isinstance(sent, str) or not hmac.compare_digest(sent.encode(), nonce.encode()):
        raise SSOError("bad_nonce", "The ID token doesn't belong to this sign-in (nonce mismatch). Try again.")
    if not isinstance(claims.get("sub"), str) or not 0 < len(claims["sub"]) <= 255:
        raise SSOError("bad_sub", "The ID token has no valid subject.")
    return claims


def _basic(client_id: str, secret: str) -> str:
    raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def exchange_code(platform, meta: dict, *, client_id: str, client_secret: str, code: str, redirect: str,
                  verifier: str) -> dict:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "code_verifier": verifier}
    headers = {"accept": "application/json"}
    if meta["token_auth"] == "client_secret_basic":
        headers["authorization"] = _basic(client_id, client_secret)
    else:
        form["client_id"], form["client_secret"] = client_id, client_secret
    host = urlsplit(meta["token_endpoint"]).hostname
    try:
        with platform.http() as http:
            r = http.post(meta["token_endpoint"], data=form, headers=headers, retries=1, follow_redirects=False,
                          max_bytes=256 * 1024)
    except SafeHTTPError as e:
        raise SSOError("unreachable", f"Could not reach {host} to finish signing in: {e}", 502) from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
        err = body.get("error") if isinstance(body, dict) else None
        code_ = err if isinstance(err, str) and CODE_RE.match(err) else None
        log.warning("sso token endpoint %s answered %s (%s)", host, r.status_code, code_ or "no error code")
        raise SSOError(f"token_{code_ or 'error'}", "The identity provider did not accept the sign-in"
                       + (f" ({code_})" if code_ else f" (HTTP {r.status_code})") + ". Try again; if it keeps "
                       "happening, check the client ID, client secret and redirect URI.", 502)
    return body


def userinfo(platform, meta: dict, access_token: str) -> dict:
    url = meta.get("userinfo_endpoint")
    if not url:
        return {}
    _, body = _get_json(platform, url, "the user's profile (UserInfo)",
                        headers={"authorization": f"Bearer {access_token}"})
    return body


def identity(platform, claims: dict, tokens: dict, meta: dict, allowed_domains: list[str]) -> str:
    """The verified, allowed email address of the signed-in person. Raises SSOError."""
    info = dict(claims)
    if not isinstance(info.get("email"), str) or ("email_verified" not in info and "xms_edov" not in info):
        token = tokens.get("access_token")
        if isinstance(token, str) and token and meta.get("userinfo_endpoint"):
            ui = userinfo(platform, meta, token)
            if ui.get("sub") != claims.get("sub"):          # OIDC Core 5.3.2: the same End-User or nothing
                raise SSOError("userinfo_sub", "The provider's profile answer is for a different person.")
            for k in ("email", "email_verified", "xms_edov", "hd"):
                if k in ui and (k not in info or (k == "email" and not isinstance(info.get(k), str))):
                    info[k] = ui[k]
    email = info.get("email")
    if not isinstance(email, str) or not EMAIL_RE.match(email.strip()) or len(email) > 254:
        raise SSOError("no_email", "The identity provider didn't send an email address. Ask your administrator to "
                                   "release the email claim (scope email) to TradeComply.")
    email = email.strip().lower()
    if "email_verified" in info:
        verified = _truthy(info.get("email_verified"))
    else:
        verified = _truthy(info.get("xms_edov"))
    if not verified:
        raise SSOError("email_unverified", "The identity provider didn't confirm this email address is verified. "
                                           "(Microsoft Entra ID: add the optional ID token claims email and "
                                           "xms_edov to the app registration.)")
    domain = email.rsplit("@", 1)[1]
    if domain not in allowed_domains:
        raise SSOError("domain_not_allowed", f"Email addresses at {domain} can't sign in to this company with SSO.")
    if meta["issuer"] == GOOGLE_ISSUER and info.get("hd") not in allowed_domains:
        raise SSOError("hd_not_allowed", "This Google account isn't in one of the company's Google Workspace "
                                         "domains.")
    return email


# ---------------------------------------------------------------------------- the stored settings
def config_secret(platform, tenant_id: str) -> tuple[dict | None, str | None]:
    """(settings, decrypted client secret or None)."""
    cfg = platform.store.get_sso_config(tenant_id, with_secret=True)
    if not cfg or not cfg.get("secret_blob"):
        return cfg, None
    try:
        return cfg, platform.secret_box.decrypt_text(cfg["secret_blob"], sso_context(tenant_id))
    except DecryptError:
        return cfg, None


def sso_verified(store, tenant_id: str, issuer: str, client_id: str) -> bool:
    """Has anyone in this company signed in with SSO under these settings?"""
    with store.connect() as con:
        rows = con.execute("select args_json from audit where tenant_id = ? and tool = 'sso.login' and ok = 1 "
                           "order by id desc limit 200", (tenant_id,)).fetchall()
    for r in rows:
        try:
            a = json.loads(r[0] or "{}")
        except ValueError:
            continue
        if a.get("issuer") == issuer and a.get("client_id") == client_id:
            return True
    return False


def _audit(store, tenant_id: str, who: str, tool: str, args: dict, ok: bool = True) -> None:
    try:
        store.audit(tenant_id, who, tool, args, None, ok=ok, source="sso")
    except Exception:      # noqa: BLE001 - the log must never break a sign-in
        log.exception("sso audit write failed")


def password_login_refusal(platform, user: dict) -> str | None:
    """Called by the password login (app/auth.py) after the password checked out. A message = refuse."""
    cfg = platform.store.get_sso_config(user["tenant_id"])
    if not cfg or not cfg.get("enforced"):
        return None
    if user.get("role") == "owner":
        _audit(platform.store, user["tenant_id"], user["email"], "sso.break_glass_login",
               {"user_id": user["id"]})
        log.info("sso: owner break-glass password sign-in for tenant %s", user["tenant_id"])
        return None
    return ("Your company signs in with single sign-on (SSO). Choose “Sign in with SSO” and use your company "
            "account; your TradeComply password can't be used.")


def _end_non_owner_sessions(store, tenant_id: str) -> int:
    n = 0
    for u in store.list_users(tenant_id):
        if u["role"] == "owner":
            continue
        full = store.user_by_id(u["id"])
        if full and full["tenant_id"] == tenant_id:
            store.update_user(tenant_id, u["id"], session_gen=int(full["session_gen"]) + 1)
            n += 1
    return n


def _view(platform, tenant_id: str) -> dict:
    store = platform.store
    cfg = store.get_sso_config(tenant_id)
    missing = missing_env(platform)
    base = None if missing else platform.settings.app_url.rstrip("/")
    out = {"operator_configured": not missing, "missing_env": missing, "configured": bool(cfg),
           "redirect_uri": f"{base}{CALLBACK_PATH}" if base else None,
           "login_url": f"{base}/api/auth/sso/start?company={tenant_id}" if base else None,
           "break_glass": "Owners can always sign in with their TradeComply password, even when SSO is required; "
                          "each such sign-in is recorded in the activity log as sso.break_glass_login.",
           "issuer": None, "client_id": None, "has_secret": False, "allowed_domains": [], "enforced": False,
           "created": None, "verified": False, "users_without_sso": []}
    if cfg:
        out.update(issuer=cfg["issuer"], client_id=cfg["client_id"], has_secret=cfg["has_secret"],
                   allowed_domains=cfg["allowed_domains"], enforced=cfg["enforced"], created=cfg["created"],
                   verified=sso_verified(store, tenant_id, cfg["issuer"], cfg["client_id"]))
        out["users_without_sso"] = [u["email"] for u in store.list_users(tenant_id)
                                    if u["email"].rsplit("@", 1)[-1] not in cfg["allowed_domains"]]
    return out


# ---------------------------------------------------------------------------- sign-in state
def _binding_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def issue_state(platform, tenant_id: str, cfg: dict, redirect: str) -> tuple[str, str, str, str]:
    """Returns (state, oidc nonce, PKCE verifier, browser binding for the cookie)."""
    n, nonce, binding, verifier = (_secrets.token_urlsafe(16), _secrets.token_urlsafe(24), _secrets.token_urlsafe(32),
                                   new_code_verifier())
    payload = {"t": tenant_id, "n": n, "o": nonce, "b": _binding_hash(binding), "r": redirect, "i": cfg["issuer"],
               "c": cfg["client_id"], "v": platform.secret_box.encrypt(verifier, pkce_context(tenant_id, f"sso.{n}")),
               "e": int(time.time()) + STATE_TTL_S}
    return platform.signer.sign("sso_state", payload), nonce, verifier, binding


def verify_state(platform, state: str | None, binding: str | None) -> dict:
    payload = platform.signer.unsign("sso_state", state)
    if not payload:
        raise SSOError("invalid_state", "This sign-in link expired or was changed. Start again.", 400)
    if not binding or not hmac.compare_digest(_binding_hash(binding), str(payload.get("b", ""))):
        raise SSOError("state_browser", "This sign-in was started in a different browser. Start again here.", 400)
    if not platform.store.claim_nonce(f"sso:{payload.get('n')}", int(payload.get("e", 0))):
        raise SSOError("state_reused", "This sign-in link was already used. Start again.", 400)
    try:
        payload["verifier"] = platform.secret_box.decrypt_text(payload.get("v", ""),
                                                               pkce_context(payload["t"], f"sso.{payload['n']}"))
    except (DecryptError, KeyError):
        raise SSOError("invalid_state", "This sign-in link can't be read on this server. Start again.", 400) from None
    return payload


def authorization_url(meta: dict, client_id: str, redirect: str, state: str, nonce: str, verifier: str,
                      login_hint: str | None = None) -> str:
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": SCOPES,
              "state": state, "nonce": nonce, "code_challenge": code_challenge(verifier),
              "code_challenge_method": "S256"}
    if login_hint:
        params["login_hint"] = login_hint
    url = meta["authorization_endpoint"]
    return url + ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)


# ---------------------------------------------------------------------------- HTTP API
router = APIRouter(tags=["sso"])


class SSOConfigIn(BaseModel):
    issuer: str = Field(min_length=8, max_length=500)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: Optional[str] = Field(None, max_length=2000)
    allowed_domains: list[str] = Field(min_length=1, max_length=50)
    enforced: bool = False


class StartIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


def _limiter(platform, name: str, limit: int, window_s: int) -> RateLimiter:
    with _cache_guard:
        lim = getattr(platform, name, None)
        if lim is None:
            lim = RateLimiter(limit=limit, window_s=window_s)
            setattr(platform, name, lim)
        return lim


def _rate_limit(request: Request, bucket: str, limit: int, window_s: int) -> None:
    """Per client IP. Generous (a crew may sign in from one office network); it bounds abuse such as filling a
    company's activity log with failed callbacks."""
    platform = platform_of(request)
    lim = _limiter(platform, f"_sso_{bucket}_limiter", limit, window_s)
    key = f"ip|{client_ip(request)}"
    wait = lim.retry_after(key)
    if wait:
        raise SSOError("rate_limited", f"Too many sign-in attempts. Try again in {max(1, wait // 60)} minute(s).", 429)
    lim.hit(key)


def _http(e: SSOError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _back_to_login(message: str) -> RedirectResponse:
    resp = RedirectResponse(LOGIN_PATH + "?" + urlencode({"sso": "error", "message": message[:300]}), status_code=303)
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    return resp


@router.get("/api/sso/config")
def get_config(request: Request, p: Principal = Depends(owner_only)):
    """This company's SSO settings (never the client secret), the redirect URI to register at the provider, the
    company's SSO sign-in link, and who couldn't sign in with SSO (email outside the allowed domains)."""
    return _view(platform_of(request), p.tenant_id)


def _clean_domains(values: list[str]) -> list[str]:
    out = sorted({str(d).strip().lower().lstrip("@") for d in values if str(d).strip()})
    bad = [d for d in out if not DOMAIN_RE.match(d)]
    if not out or bad:
        raise HTTPException(422, "Enter the company's email domains like acme.com"
                            + (f" ({', '.join(bad[:3])} isn't one)" if bad else ""))
    return out


@router.put("/api/sso/config")
def put_config(body: SSOConfigIn, request: Request, p: Principal = Depends(owner_only)):
    """Save (and check) the SSO settings. The provider's discovery document and signing keys must load. A blank
    client_secret keeps the saved one (unless the client ID changed). enforced=true needs a successful SSO sign-in
    with these settings first, and ends every non-owner session."""
    platform = platform_of(request)
    require_entitled(platform, p)
    store, tid = platform.store, p.tenant_id
    try:
        _require_operator(platform)
        issuer = normalize_issuer(body.issuer)
    except SSOError as e:
        raise _http(e)
    client_id = body.client_id.strip()
    if not CLIENT_ID_RE.match(client_id):
        raise HTTPException(422, "The client ID can't contain spaces or control characters.")
    secret = (body.client_secret or "").strip()
    if secret and (len(secret) > 2000 or any(ord(ch) < 32 for ch in secret)):
        raise HTTPException(422, "The client secret contains characters that can't be right.")
    domains = _clean_domains(body.allowed_domains)
    old = store.get_sso_config(tid)
    if not secret and (not old or not old.get("has_secret") or old["client_id"] != client_id):
        raise HTTPException(422, "Enter the client secret (it's needed for a new or changed client ID).")
    try:                                     # the provider must answer before anything is saved
        try:
            meta = discover(platform, issuer, force=True)
        except SSOError as e:
            got = _issuer_hint(platform, issuer) if e.code == "issuer_mismatch" else None
            if not got:
                raise
            issuer, meta = got, discover(platform, got, force=True)    # same document, trailing "/" differs only
        signing_keys(platform, meta["jwks_uri"], force=True)
    except SSOError as e:
        _audit(store, tid, p.label, "sso.config", {"issuer": issuer, "error": e.code}, ok=False)
        raise HTTPException(422 if e.status in (422, 400) else e.status, str(e))
    if body.enforced and not sso_verified(store, tid, issuer, client_id):
        raise HTTPException(409, "Sign in with SSO once with these settings (use the company sign-in link) before "
                                 "requiring it, so nobody is locked out by a setup mistake.")
    blob = platform.secret_box.encrypt(secret, sso_context(tid)) if secret else None
    store.save_sso_config(tid, issuer=issuer, client_id=client_id, allowed_domains=domains, enforced=body.enforced,
                          **({"secret_blob": blob} if blob else {}))
    ended = 0
    if body.enforced and not (old and old.get("enforced")):
        ended = _end_non_owner_sessions(store, tid)
    _audit(store, tid, p.label, "sso.config", {"issuer": issuer, "client_id": client_id, "domains": domains,
                                               "enforced": body.enforced, "secret_changed": bool(secret),
                                               "sessions_ended": ended})
    return {**_view(platform, tid), "sessions_ended": ended}


def _issuer_hint(platform, issuer: str) -> str | None:
    """When the discovery document's issuer differs from the entered one only by a trailing "/", use the
    document's (the document is fetched from the same URL either way)."""
    try:
        _, doc = _get_json(platform, discovery_url(issuer), "the discovery document")
    except SSOError:
        return None
    got = doc.get("issuer")
    if isinstance(got, str) and got != issuer and got.rstrip("/") == issuer.rstrip("/"):
        return got
    return None


@router.post("/api/sso/test")
def test_config(request: Request, p: Principal = Depends(owner_only)):
    """Check the saved settings: discovery document, signing keys, client secret readable."""
    platform = platform_of(request)
    require_entitled(platform, p)
    _rate_limit(request, "test", 30, 600)
    cfg, secret = config_secret(platform, p.tenant_id)
    if not cfg:
        raise HTTPException(409, "Save the SSO settings first.")
    try:
        _require_operator(platform)
        meta = discover(platform, cfg["issuer"], force=True)
        keys = signing_keys(platform, meta["jwks_uri"], force=True)
    except SSOError as e:
        return {"ok": False, "message": str(e), "code": e.code}
    if not secret:
        return {"ok": False, "message": "The saved client secret can't be read on this server (the encryption key "
                                        "changed). Enter it again.", "code": "secret_unreadable"}
    return {"ok": True, "message": f"{urlsplit(cfg['issuer']).hostname} answered: discovery document and "
                                   f"{len(keys)} signing key(s) loaded.",
            "issuer": meta["issuer"], "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"], "token_auth": meta["token_auth"],
            "algorithms": meta["algorithms"], "signing_keys": len(keys),
            "verified": sso_verified(platform.store, p.tenant_id, cfg["issuer"], cfg["client_id"])}


@router.delete("/api/sso/config")
def delete_config(request: Request, p: Principal = Depends(owner_only)):
    platform = platform_of(request)
    if not platform.store.delete_sso_config(p.tenant_id):
        raise HTTPException(404, "SSO isn't set up for this company.")
    _audit(platform.store, p.tenant_id, p.label, "sso.delete", {})
    return {"ok": True}


def _start(request: Request, tenant_id: str, cfg: dict, login_hint: str | None) -> tuple[str, str]:
    """(authorize URL, browser binding) for this company's provider."""
    platform = platform_of(request)
    meta = discover(platform, cfg["issuer"])
    redirect = redirect_uri(platform)
    state, nonce, verifier, binding = issue_state(platform, tenant_id, cfg, redirect)
    return authorization_url(meta, cfg["client_id"], redirect, state, nonce, verifier, login_hint), binding


def _with_cookie(resp, request: Request, binding: str):
    resp.set_cookie(COOKIE, binding, max_age=STATE_TTL_S, httponly=True, samesite="lax", secure=is_https(request),
                    path=COOKIE_PATH)
    return resp


_NO_SSO = ("Single sign-on isn't set up for this email address. Sign in with your password, or ask your company's "
           "TradeComply owner.")


@router.post("/api/auth/sso/start")
def start_by_email(body: StartIn, request: Request):
    """The login page's "Sign in with SSO": {"email"} -> {"authorize_url"} for that user's company."""
    platform = platform_of(request)
    try:
        _rate_limit(request, "start", 100, 600)
        _require_operator(platform)
        email = body.email.strip().lower()
        user = platform.store.user_by_email(email) if EMAIL_RE.match(email) else None
        cfg, secret = config_secret(platform, user["tenant_id"]) if user else (None, None)
        if not (cfg and secret and email.rsplit("@", 1)[1] in cfg["allowed_domains"]):
            raise SSOError("no_sso", _NO_SSO, 404)
        url, binding = _start(request, user["tenant_id"], cfg, email)
    except SSOError as e:
        raise _http(e)
    return _with_cookie(JSONResponse({"authorize_url": url}), request, binding)


@router.get("/api/auth/sso/start")
def start_for_company(request: Request, company: str = Query(..., min_length=3, max_length=64),
                      iss: Optional[str] = Query(None, max_length=500),
                      login_hint: Optional[str] = Query(None, max_length=254)):
    """A company's SSO sign-in link (bookmark, or the provider's "initiate login URI"): redirects to the provider."""
    platform = platform_of(request)
    try:
        _rate_limit(request, "start", 100, 600)
        _require_operator(platform)
        cfg, secret = config_secret(platform, company) if re.fullmatch(r"t_[0-9a-f]{1,40}", company) else (None, None)
        if not (cfg and secret):
            raise SSOError("no_sso", "This company's single sign-on isn't set up.", 404)
        if iss is not None and iss != cfg["issuer"]:
            raise SSOError("bad_iss", "This sign-in was started by a different identity provider.", 400)
        hint = login_hint.strip() if login_hint and EMAIL_RE.match(login_hint.strip()) else None
        url, binding = _start(request, company, cfg, hint)
    except SSOError as e:
        return _back_to_login(str(e))
    return _with_cookie(RedirectResponse(url, status_code=303), request, binding)


@router.get(CALLBACK_PATH)
def callback(request: Request, code: Optional[str] = Query(None, max_length=4096),
             state: Optional[str] = Query(None, max_length=8192), error: Optional[str] = Query(None, max_length=200),
             iss: Optional[str] = Query(None, max_length=500)):
    """Where the identity provider sends the browser back. Always answers with a redirect: into the app when the
    sign-in worked, to the login page with ?sso=error&message=... when it didn't."""
    platform = platform_of(request)
    store = platform.store
    try:
        _rate_limit(request, "callback", 100, 600)
        st = verify_state(platform, state, request.cookies.get(COOKIE))
    except SSOError as e:
        return _back_to_login(str(e))
    tid = st["t"]
    try:
        if error:
            reason = error if CODE_RE.match(error) else "error"
            raise SSOError(f"idp_{reason}", f"The identity provider didn't complete the sign-in ({reason}).")
        if not code:
            raise SSOError("no_code", "The identity provider sent no authorization code.")
        cfg, secret = config_secret(platform, tid)
        if not cfg or cfg["issuer"] != st["i"] or cfg["client_id"] != st["c"]:
            raise SSOError("config_changed", "The company's SSO settings changed during the sign-in. Start again.")
        if not secret:
            raise SSOError("secret_unreadable", "The company's SSO client secret can't be read on this server. Ask "
                                                "the owner to enter it again.")
        if iss is not None and iss != cfg["issuer"]:
            raise SSOError("bad_iss", "The sign-in answer came from a different identity provider.")
        meta = discover(platform, cfg["issuer"])
        tokens = exchange_code(platform, meta, client_id=cfg["client_id"], client_secret=secret, code=code,
                               redirect=st["r"], verifier=st["verifier"])
        claims = validate_id_token(platform, tokens.get("id_token"), meta=meta, client_id=cfg["client_id"],
                                   nonce=st["o"])
        email = identity(platform, claims, tokens, meta, cfg["allowed_domains"])
        user = store.user_by_email(email)
        if not user or user["tenant_id"] != tid:            # never another company's user, never a new one
            raise SSOError("no_user", "There's no TradeComply user with this email in this company. Ask your "
                                      "company's TradeComply owner to invite you.")
    except SSOError as e:
        _audit(store, tid, "sso", "sso.login", {"error": e.code}, ok=False)
        log.info("sso sign-in refused for tenant %s (%s)", tid, e.code)
        return _back_to_login(str(e))
    except Exception as e:      # noqa: BLE001 - a browser redirect, never a 500 page; logged without tokens
        log.error("sso sign-in failed unexpectedly for tenant %s (%s)", tid, type(e).__name__)
        _audit(store, tid, "sso", "sso.login", {"error": "unexpected"}, ok=False)
        return _back_to_login("Signing in with SSO failed unexpectedly. Try again.")
    store.update_user(tid, user["id"], last_login=now_iso())
    _audit(store, tid, user["email"], "sso.login", {"user_id": user["id"], "issuer": cfg["issuer"],
                                                    "client_id": cfg["client_id"]})
    log.info("sso sign-in for tenant %s user %s", tid, user["id"])
    resp = RedirectResponse(SUCCESS_PATH, status_code=303)
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    set_session(resp, request, user)
    return resp


# ---------------------------------------------------------------------------- operator CLI (break-glass)
def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("unenforce", "status"):
        print("usage: python -m app.sso unenforce <tenant id>   (turn 'SSO required' off for a company)\n"
              "       python -m app.sso status <tenant id>")
        return 2
    from .store import Store, database_url_from_env, default_data_dir
    store = Store(default_data_dir(), database_url=database_url_from_env())
    try:
        cfg = store.get_sso_config(args[1])
        if not cfg:
            print(f"no SSO settings for {args[1]}")
            return 1
        if args[0] == "unenforce":
            store.save_sso_config(args[1], issuer=cfg["issuer"], client_id=cfg["client_id"],
                                  allowed_domains=cfg["allowed_domains"], enforced=False)
            _audit(store, args[1], "operator (CLI)", "sso.unenforce", {})
            cfg = store.get_sso_config(args[1])
        print(json.dumps({k: cfg[k] for k in ("tenant_id", "issuer", "client_id", "allowed_domains", "enforced",
                                              "has_secret", "created")}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
