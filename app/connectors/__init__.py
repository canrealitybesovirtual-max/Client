"""Connector base class and auto-discovering registry. Read docs/integrations-dev.md first.

A connector is one module in this package (app/connectors/<provider>.py) defining one subclass of Connector. The
registry imports every module here whose name doesn't start with "_" and registers each Connector subclass defined
in it. Nothing else needs editing: the API (/api/integrations), the settings UI (it renders `config_fields`
generically) and the event bus (`subscribes`) pick it up.

What a connector declares (class attributes):
    provider       id, ^[a-z][a-z0-9_]{1,39}$, unique, used in URLs and the database (never change it)
    display_name   shown to customers
    catalog_id     this integration's id in the product's integration catalog
    description    one or two plain sentences for the settings page
    category       e.g. "project_management", "accounting", "payroll", "messaging", "calendar", "identity"
    docs_url       the official vendor API documentation the connector was written from (https)
    required_env   operator-level app credentials (e.g. ("PROCORE_CLIENT_ID", "PROCORE_CLIENT_SECRET")); the
                   connector is "not configured on this server" until all are set. Never per-tenant secrets.
    config_fields  per-tenant settings the UI renders (ConfigField); secret=True ones are encrypted at rest
    oauth          OAuth2Spec for an authorization-code connector (None for API-key / settings-only connectors)
    subscribes     event names (app/events.py EVENTS) whose deliveries this connector handles in handle_event()
    unconfirmed    facts the connector relies on that are NOT yet confirmed from the vendor's docs; shown in the UI

What it implements: test() always; handle_event() if it subscribes; on_connected() (OAuth: return the account
label); connect()/disconnect()/revoke() only to change the defaults.

Secrets: self.secrets() / self.save(secrets=...) encrypt and decrypt with the platform SecretBox under
connection_context(tenant, provider). Outbound HTTP: `with self.ctx.http() as http:` (SafeHTTP, SSRF-guarded; tests
swap its transport). Never log a token, a secret field or a request/response body that may carry one.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import logging
import os
import pkgutil
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from ..crypto import DecryptError, connection_context, pkce_context
from ..safe_http import SafeHTTPError, UnsafeURLError, check_url
from ..store import PROVIDER_RE

log = logging.getLogger("tradecomply.connectors")

FIELD_TYPES = ("text", "textarea", "url", "email", "password", "number", "boolean", "select")
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESERVED_IDS = frozenset({"webhooks", "events", "outbound", "providers", "oauth", "config", "test", "connect",
                          "disconnect", "callback", "start", "catalog"})
OAUTH_SECRET_KEY = "oauth"          # where OAuth tokens live inside a connection's secrets dict
STATE_TTL_S = 600                   # an OAuth `state` is good for 10 minutes, once
_UNSET = object()


# ---------------------------------------------------------------------------- errors
class ConnectorError(Exception):
    """A failure whose message is safe to show the customer (no tokens, no response bodies)."""
    status_code = 502


class NotConfigured(ConnectorError):
    """The operator hasn't set this connector's required_env on the server."""
    status_code = 503


class NotConnected(ConnectorError):
    """The tenant has no usable connection (never connected, disconnected, or the access was revoked/expired)."""
    status_code = 409


class ConfigError(ConnectorError, ValueError):
    status_code = 422

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class OAuthError(ConnectorError):
    """An OAuth step failed. `code` is the RFC 6749 error code when the provider sent one (e.g. invalid_grant)."""
    status_code = 400

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class PermanentError(ConnectorError):
    """Raised from handle_event(): this delivery can never succeed (bad data, object gone), so don't retry it."""


# ---------------------------------------------------------------------------- declarations
@dataclass(frozen=True)
class ConfigField:
    """One per-tenant setting, rendered generically by the settings UI.

    type: text | textarea | url (https only) | email | password | number | boolean | select (needs options).
    secret=True: stored encrypted, never sent back to the browser (the UI shows "saved"; leaving it blank on save
    keeps the stored value). A password-type field must be secret."""
    name: str
    label: str
    type: str = "text"
    secret: bool = False
    required: bool = False
    help: str = ""
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()     # select: (value, label) pairs
    max_length: int = 500
    pattern: str | None = None                    # full-match regex for text-like fields
    default: Any = None

    def __post_init__(self):
        if not FIELD_NAME_RE.match(self.name) or self.name == OAUTH_SECRET_KEY:
            raise ValueError(f"bad config field name {self.name!r}")
        if self.type not in FIELD_TYPES:
            raise ValueError(f"field {self.name}: type must be one of {FIELD_TYPES}")
        if self.type == "password" and not self.secret:
            raise ValueError(f"field {self.name}: a password field must be secret=True")
        if self.type == "select" and not self.options:
            raise ValueError(f"field {self.name}: a select needs options")
        if not self.label:
            raise ValueError(f"field {self.name}: label is required")

    def to_schema(self) -> dict:
        return {"name": self.name, "label": self.label, "type": self.type, "secret": self.secret,
                "required": self.required, "help": self.help, "placeholder": self.placeholder,
                "options": [{"value": v, "label": lbl} for v, lbl in self.options], "max_length": self.max_length,
                "pattern": self.pattern, "default": None if self.secret else self.default}

    def clean(self, value):
        """The value as stored, or None when empty. Raises ConfigError with a plain message."""
        if value is None or (isinstance(value, str) and not value.strip() and self.type != "boolean"):
            return None
        label = self.label
        if self.type == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off", ""):
                return False
            raise ConfigError(f"{label}: must be yes or no", self.name)
        if self.type == "number":
            if isinstance(value, bool):
                raise ConfigError(f"{label}: must be a number", self.name)
            try:
                num = float(str(value).strip())
            except ValueError:
                raise ConfigError(f"{label}: must be a number", self.name) from None
            if num != num or num in (float("inf"), float("-inf")):
                raise ConfigError(f"{label}: must be a number", self.name)
            return int(num) if num.is_integer() else num
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ConfigError(f"{label}: must be text", self.name)
        text = str(value) if self.type == "password" else str(value).strip()
        if len(text) > self.max_length:
            raise ConfigError(f"{label}: at most {self.max_length} characters", self.name)
        if "\x00" in text:
            raise ConfigError(f"{label}: contains an invalid character", self.name)
        if self.type == "select" and text not in {v for v, _ in self.options}:
            raise ConfigError(f"{label}: choose one of the listed options", self.name)
        if self.type == "email" and not EMAIL_RE.match(text):
            raise ConfigError(f"{label}: enter a valid email address", self.name)
        if self.type == "url":
            try:
                check_url(text, resolver=None)
            except UnsafeURLError as e:
                raise ConfigError(f"{label}: {e}", self.name) from None
        if self.pattern and not re.fullmatch(self.pattern, text):
            # Say what is expected: the placeholder is the example the settings form shows.
            like = f" (like {self.placeholder})" if self.placeholder and not self.secret else ""
            raise ConfigError(f"{label}: not in the expected format{like}", self.name)
        return text


@dataclass(frozen=True)
class OAuth2Spec:
    """An OAuth 2.0 authorization-code grant (RFC 6749 section 4.1), with PKCE S256 (RFC 7636) when the provider
    supports it. Every URL and scope must come from the provider's official docs (docs_url); mark anything you
    could not confirm in the connector's `unconfirmed`.

    client_auth: how the client authenticates at the token endpoint. "basic" = HTTP Basic with the form-urlencoded
    client id and secret (RFC 6749 2.3.1, which servers MUST support); "body" = client_id/client_secret form fields
    (allowed by 2.3.1, required by some providers); "none" = public client (PKCE only)."""
    authorize_url: str
    token_url: str
    client_id_env: str
    client_secret_env: str | None = None
    scopes: tuple[str, ...] = ()
    scope_separator: str = " "
    pkce: bool = True
    client_auth: str = "basic"
    extra_authorize_params: tuple[tuple[str, str], ...] = ()
    revoke_url: str | None = None          # RFC 7009 token revocation endpoint, if the provider documents one

    def __post_init__(self):
        if self.client_auth not in ("basic", "body", "none"):
            raise ValueError("client_auth must be basic, body or none")


@dataclass
class TestResult:
    __test__ = False                        # not a pytest test class
    ok: bool
    message: str
    account: str | None = None              # e.g. the company name the credentials belong to
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "account": self.account, "details": self.details}


@dataclass
class ConnectorContext:
    """Who a connector instance acts for. Built by the API (from the logged-in user) or the event worker."""
    platform: Any
    tenant_id: str
    user_id: int | None = None
    user_label: str | None = None

    @property
    def store(self):
        return self.platform.store

    @property
    def secret_box(self):
        return self.platform.secret_box

    @property
    def env(self):
        """Operator environment (os.environ, or a dict the tests set as platform.env)."""
        return self.platform.env

    def http(self, **kw):
        """A SafeHTTP client (use `with ctx.http() as http:`). Tests swap the transport on the platform."""
        return self.platform.http(**kw)


# ---------------------------------------------------------------------------- PKCE / state helpers
def new_code_verifier() -> str:
    """RFC 7636 4.1: 43-128 characters from [A-Za-z0-9-._~]. 64 here."""
    return _secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """RFC 7636 4.2, S256: BASE64URL-ENCODE(SHA256(ASCII(code_verifier))), no padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


def issue_oauth_state(platform, tenant_id: str, user_id: int, provider: str, redirect_uri: str) -> tuple[str, str]:
    """A `state` signed with the app secret and bound to tenant + user + provider + redirect URI, valid for
    STATE_TTL_S and usable once. The PKCE verifier travels inside it encrypted (a URL that leaks in a log or the
    browser history doesn't reveal it). Returns (state, code_verifier)."""
    nonce = _secrets.token_urlsafe(16)
    verifier = new_code_verifier()
    payload = {"t": tenant_id, "u": user_id, "p": provider, "r": redirect_uri, "n": nonce,
               "v": platform.secret_box.encrypt(verifier, pkce_context(tenant_id, nonce)),
               "e": int(time.time()) + STATE_TTL_S}
    return platform.signer.sign("oauth_state", payload), verifier


def verify_oauth_state(platform, state: str | None, *, tenant_id: str, user_id: int | None, provider: str) -> dict:
    """Check signature, expiry, tenant, user and provider, then use it up (a replayed state fails). Returns
    {"redirect_uri", "code_verifier"}. Raises OAuthError."""
    payload = platform.signer.unsign("oauth_state", state)
    if not payload:
        raise OAuthError("This connection link expired or was tampered with. Start the connection again.",
                         "invalid_state")
    if payload.get("t") != tenant_id or payload.get("u") != user_id or payload.get("p") != provider:
        raise OAuthError("This connection was started by a different user or account. Start it again while "
                         "logged in as yourself.", "state_mismatch")
    if not platform.store.claim_nonce(f"oauth:{payload.get('n')}", int(payload.get("e", 0))):
        raise OAuthError("This connection link was already used. Start the connection again.", "state_reused")
    try:
        verifier = platform.secret_box.decrypt_text(payload.get("v", ""), pkce_context(tenant_id, payload["n"]))
    except (DecryptError, KeyError):
        raise OAuthError("This connection link can't be read on this server. Start the connection again.",
                         "invalid_state") from None
    return {"redirect_uri": payload.get("r"), "code_verifier": verifier}


_refresh_locks: dict[tuple[str, str], threading.Lock] = {}
_refresh_guard = threading.Lock()


def _refresh_lock(tenant_id: str, provider: str) -> threading.Lock:
    with _refresh_guard:
        return _refresh_locks.setdefault((tenant_id, provider), threading.Lock())


def _safe_code(value) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[a-z_]{1,40}", text) else None


# ---------------------------------------------------------------------------- the base class
class Connector:
    provider: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    catalog_id: ClassVar[str] = ""
    description: ClassVar[str] = ""
    category: ClassVar[str] = "other"
    docs_url: ClassVar[str] = ""
    required_env: ClassVar[tuple[str, ...]] = ()
    config_fields: ClassVar[tuple[ConfigField, ...]] = ()
    oauth: ClassVar[OAuth2Spec | None] = None
    subscribes: ClassVar[tuple[str, ...]] = ()
    unconfirmed: ClassVar[tuple[str, ...]] = ()

    def __init__(self, ctx: ConnectorContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ class-level metadata
    @classmethod
    def auth_kind(cls) -> str:
        return "oauth2" if cls.oauth else ("config" if cls.config_fields else "none")

    @classmethod
    def missing_env(cls, env=None) -> list[str]:
        env = os.environ if env is None else env
        return [name for name in cls.required_env if not (env.get(name) or "").strip()]

    @classmethod
    def operator_configured(cls, env=None) -> bool:
        return not cls.missing_env(env)

    @classmethod
    def schema(cls) -> list[dict]:
        return [f.to_schema() for f in cls.config_fields]

    @classmethod
    def describe(cls, env=None) -> dict:
        return {"provider": cls.provider, "display_name": cls.display_name, "catalog_id": cls.catalog_id,
                "description": cls.description, "category": cls.category, "docs_url": cls.docs_url,
                "auth": cls.auth_kind(), "operator_configured": cls.operator_configured(env),
                "config_schema": cls.schema(), "subscribes": list(cls.subscribes),
                "unconfirmed": list(cls.unconfirmed)}

    @classmethod
    def clean_config(cls, values: dict, existing_config: dict | None = None,
                     existing_secrets: dict | None = None) -> tuple[dict, dict]:
        """Validate submitted settings against config_fields. Returns (config, secret_fields): non-secret values
        to store in config_json and secret values to encrypt. A secret field left blank keeps its stored value;
        unknown keys are refused. Raises ConfigError."""
        if not isinstance(values, dict):
            raise ConfigError("settings must be an object")
        known = {f.name: f for f in cls.config_fields}
        unknown = [k for k in values if k not in known]
        if unknown:
            raise ConfigError(f"Unknown setting(s): {', '.join(sorted(map(str, unknown))[:5])}")
        config, secret = dict(existing_config or {}), {k: v for k, v in (existing_secrets or {}).items()
                                                       if k != OAUTH_SECRET_KEY}
        for f in cls.config_fields:
            if f.name in values:
                value = f.clean(values[f.name])
                if f.secret:
                    if value is not None:
                        secret[f.name] = value
                else:
                    config[f.name] = value
            elif not f.secret and f.name not in config and f.default is not None:
                config[f.name] = f.default
            have = secret.get(f.name) if f.secret else config.get(f.name)
            if f.required and (have is None or have == ""):
                raise ConfigError(f"{f.label} is required", f.name)
        return {k: v for k, v in config.items() if k in known}, secret

    # ------------------------------------------------------------------ stored state
    def _context(self) -> str:
        return connection_context(self.ctx.tenant_id, self.provider)

    def connection(self, with_secret: bool = False) -> dict | None:
        return self.ctx.store.get_connection(self.ctx.tenant_id, self.provider, with_secret=with_secret)

    def config(self) -> dict:
        return (self.connection() or {}).get("config") or {}

    def _decrypt(self, blob: str | None) -> dict:
        if not blob:
            return {}
        try:
            value = self.ctx.secret_box.decrypt_json(blob, self._context())
        except DecryptError:
            raise NotConnected(f"The saved {self.display_name} credentials can't be read on this server (the "
                               "encryption key changed). Reconnect to fix it.") from None
        return value if isinstance(value, dict) else {}

    def secrets(self) -> dict:
        """Decrypted secrets of this tenant's connection ({} if none): secret config fields by name, and OAuth
        tokens under "oauth"."""
        return self._decrypt((self.connection(with_secret=True) or {}).get("secret_blob"))

    def save(self, *, status: str | None = None, config: dict | None = None, secrets=_UNSET,
             external_account=_UNSET, last_error=_UNSET, expect_secret_blob=_UNSET) -> dict | None:
        """Write this tenant's connection (created if missing). secrets: a dict (encrypted here) or None (wipe).
        With expect_secret_blob, a compare-and-set update: None is returned if the stored blob changed."""
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if config is not None:
            fields["config"] = config
        if secrets is not _UNSET:
            fields["secret_blob"] = (None if not secrets else
                                     self.ctx.secret_box.encrypt_json(secrets, self._context()))
        if external_account is not _UNSET:
            fields["external_account"] = external_account
        if last_error is not _UNSET:
            fields["last_error"] = last_error
        store = self.ctx.store
        if expect_secret_blob is not _UNSET:
            return store.update_connection(self.ctx.tenant_id, self.provider, expect_secret_blob=expect_secret_blob,
                                           **fields)
        return store.save_connection(self.ctx.tenant_id, self.provider, **fields)

    def status(self) -> dict:
        conn = self.connection()
        if not conn:
            return {"status": "disconnected", "external_account": None, "last_error": None, "updated": None,
                    "has_secret": False}
        return {k: conn.get(k) for k in ("status", "external_account", "last_error", "updated", "has_secret")}

    def env(self, name: str) -> str | None:
        return (self.ctx.env.get(name) or "").strip() or None

    def require_operator(self) -> None:
        missing = self.missing_env(self.ctx.env)
        if missing:
            raise NotConfigured(f"{self.display_name} isn't set up on this server yet (the operator needs to set "
                                f"{', '.join(missing)}).")

    # ------------------------------------------------------------------ lifecycle (override as needed)
    def test(self) -> TestResult:
        """Make one cheap, read-only call with the stored credentials and report whether it worked. Required."""
        raise NotImplementedError(f"{type(self).__name__}.test() is not implemented")

    def connect(self) -> dict:
        """Settings-based connectors: verify the saved settings with test() and mark the connection connected or
        error. OAuth connectors connect through the OAuth routes instead."""
        if self.oauth:
            raise ConnectorError(f"{self.display_name} connects by signing in: use the Connect button.")
        self.require_operator()
        try:
            result = self.test()
        except ConnectorError as e:
            result = TestResult(False, str(e))
        except SafeHTTPError as e:
            result = TestResult(False, f"Could not reach {self.display_name}: {e}")
        if result.ok:
            self.save(status="connected", last_error=None, external_account=result.account)
        else:
            self.save(status="error", last_error=result.message)
        return {**self.status(), "test": result.to_dict()}

    def disconnect(self) -> dict:
        """Best-effort revoke at the provider, then forget the credentials (settings are kept)."""
        try:
            if self.connection(with_secret=True):
                self.revoke()
        except Exception as e:       # noqa: BLE001 - revocation is best effort; the local credentials still go
            log.warning("%s: revoking at the provider failed (%s)", self.provider, type(e).__name__)
        if self.connection():
            self.save(status="disconnected", secrets=None, external_account=None, last_error=None)
        return self.status()

    def revoke(self) -> None:
        """Revoke OAuth tokens at the provider (RFC 7009) when the spec documents a revoke_url. Override for
        providers with their own revocation call."""
        if not (self.oauth and self.oauth.revoke_url):
            return
        tok = (self.secrets().get(OAUTH_SECRET_KEY) or {})
        token = tok.get("refresh_token") or tok.get("access_token")
        if not token:
            return
        form = {"token": token, "token_type_hint": "refresh_token" if tok.get("refresh_token") else "access_token"}
        with self.ctx.http() as http:
            # no redirects: the form carries the token (and, with client_auth="body", the client secret)
            http.post(self.oauth.revoke_url, data=form, headers=self._client_auth_headers(form), retries=0,
                      follow_redirects=False)

    def handle_event(self, event: str, payload: dict) -> None:
        """Deliver one event this connector subscribes to. Raise PermanentError for a delivery that can never
        succeed; any other exception is retried with backoff."""
        raise NotImplementedError(f"{type(self).__name__} subscribes to events but has no handle_event()")

    # ------------------------------------------------------------------ OAuth 2.0
    def on_connected(self, tokens: dict) -> str | None:
        """Called once after a successful OAuth sign-in; return a label for the connected account (company name)
        to show in the UI, e.g. from the provider's "who am I" call. Default: none."""
        return None

    def _oauth_client(self) -> tuple[str, str | None]:
        spec = self.oauth
        if not spec:
            raise OAuthError(f"{self.display_name} does not use OAuth")
        self.require_operator()
        client_id = self.env(spec.client_id_env)
        secret = self.env(spec.client_secret_env) if spec.client_secret_env else None
        if not client_id or (spec.client_auth != "none" and not secret):
            raise NotConfigured(f"{self.display_name} isn't set up on this server yet.")
        return client_id, secret

    def _client_auth_headers(self, form: dict) -> dict:
        client_id, secret = self._oauth_client()
        headers = {"accept": "application/json"}
        if self.oauth.client_auth == "basic" and secret:
            raw = f"{quote(client_id, safe='')}:{quote(secret, safe='')}".encode()
            headers["authorization"] = "Basic " + base64.b64encode(raw).decode()
        elif self.oauth.client_auth == "body" and secret:
            form["client_id"], form["client_secret"] = client_id, secret
        else:
            form["client_id"] = client_id
        return headers

    def authorization_url(self, redirect_uri: str, state: str, code_challenge_value: str | None) -> str:
        """RFC 6749 4.1.1 authorization request (+ RFC 7636 code_challenge)."""
        spec = self.oauth
        client_id, _ = self._oauth_client()
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state}
        if spec.scopes:
            params["scope"] = spec.scope_separator.join(spec.scopes)
        if spec.pkce and code_challenge_value:
            params["code_challenge"], params["code_challenge_method"] = code_challenge_value, "S256"
        params.update(dict(spec.extra_authorize_params))
        return spec.authorize_url + ("&" if "?" in spec.authorize_url else "?") + urlencode(params, quote_via=quote)

    def _token_request(self, form: dict) -> dict:
        headers = self._client_auth_headers(form)
        try:
            with self.ctx.http() as http:
                r = http.post(self.oauth.token_url, data=form, headers=headers, retries=1, follow_redirects=False)
        except SafeHTTPError as e:
            raise OAuthError(f"Could not reach {self.display_name} to finish signing in: {e}") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            code = _safe_code(body.get("error")) if isinstance(body, dict) else None
            log.warning("%s token endpoint answered %s (%s)", self.provider, r.status_code, code or "no error code")
            raise OAuthError(f"{self.display_name} did not accept the sign-in"
                             + (f" ({code})" if code else f" (HTTP {r.status_code})") + ". Try connecting again.",
                             code)
        tokens = {"access_token": str(body["access_token"]), "token_type": str(body.get("token_type") or "Bearer")}
        if body.get("refresh_token"):
            tokens["refresh_token"] = str(body["refresh_token"])
        if body.get("scope"):
            tokens["scope"] = str(body["scope"])
        try:
            expires_in = int(body.get("expires_in"))
            tokens["expires_at"] = int(time.time()) + max(0, expires_in)
        except (TypeError, ValueError):
            pass
        return tokens

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        """RFC 6749 4.1.3 token request (+ RFC 7636 4.5 code_verifier). Returns normalized tokens."""
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if self.oauth.pkce and code_verifier:
            form["code_verifier"] = code_verifier
        return self._token_request(form)

    def refresh(self, refresh_token: str) -> dict:
        """RFC 6749 section 6 refresh request. Returns normalized tokens."""
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def complete_oauth(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        tokens = self.exchange_code(code, redirect_uri, code_verifier)
        try:
            current = self.secrets() if self.connection() else {}
        except NotConnected:          # the old blob is unreadable (key changed): this sign-in replaces it
            current = {}
        self.save(status="connected", secrets={**current, OAUTH_SECRET_KEY: tokens}, last_error=None)
        try:
            account = self.on_connected(tokens)
        except (ConnectorError, SafeHTTPError) as e:
            log.warning("%s: on_connected failed (%s)", self.provider, type(e).__name__)
            account = None
        if account:
            self.save(external_account=str(account)[:300])
        return self.status()

    def access_token(self, leeway: int = 60) -> str:
        """A valid OAuth access token, refreshed first when it expires within `leeway` seconds. Concurrent refreshes
        are serialized in this process and compare-and-set in the database, so with rotating refresh tokens two
        workers never both spend the same one."""
        conn = self.connection(with_secret=True)
        tok = (self._decrypt((conn or {}).get("secret_blob")).get(OAUTH_SECRET_KEY) or {})
        if not tok.get("access_token") or (conn or {}).get("status") == "disconnected":
            raise NotConnected(f"{self.display_name} is not connected.")
        if not tok.get("expires_at") or tok["expires_at"] - leeway > time.time():
            return tok["access_token"]
        with _refresh_lock(self.ctx.tenant_id, self.provider):
            conn = self.connection(with_secret=True)
            secrets_now = self._decrypt((conn or {}).get("secret_blob"))
            tok = secrets_now.get(OAUTH_SECRET_KEY) or {}
            if tok.get("access_token") and tok.get("expires_at", 0) - leeway > time.time():
                return tok["access_token"]          # refreshed by another thread meanwhile
            if not tok.get("refresh_token"):
                self.save(status="error", last_error="Access expired. Reconnect to continue.")
                raise NotConnected(f"{self.display_name} access expired. Reconnect to continue.")
            try:
                new = self.refresh(tok["refresh_token"])
            except OAuthError as e:
                if e.code in ("invalid_grant", "invalid_client", "unauthorized_client"):
                    self.save(status="error", last_error="Access was revoked or expired. Reconnect to continue.")
                    raise NotConnected(f"{self.display_name} access was revoked or expired. Reconnect.") from None
                raise
            merged = {**tok, **new}
            if not new.get("refresh_token"):
                merged["refresh_token"] = tok["refresh_token"]
            if "expires_at" not in new:
                merged.pop("expires_at", None)
            saved = self.save(secrets={**secrets_now, OAUTH_SECRET_KEY: merged}, status="connected", last_error=None,
                              expect_secret_blob=conn["secret_blob"])
            if saved is None:                       # another instance refreshed first: use what it stored
                other = (self.secrets().get(OAUTH_SECRET_KEY) or {})
                if other.get("access_token"):
                    return other["access_token"]
                raise NotConnected(f"{self.display_name} is not connected.")
            return merged["access_token"]

    def auth_headers(self) -> dict:
        return {"authorization": f"Bearer {self.access_token()}"}


# ---------------------------------------------------------------------------- registry
_registry: dict[str, type[Connector]] = {}
import_errors: dict[str, str] = {}
_discovered = False
_lock = threading.RLock()


def validate_connector_class(cls) -> list[str]:
    """Problems that keep a connector class from being registered (empty list = fine)."""
    errors = []
    if not (isinstance(cls, type) and issubclass(cls, Connector)) or cls is Connector:
        return ["not a Connector subclass"]
    if not isinstance(cls.provider, str) or not PROVIDER_RE.match(cls.provider) or cls.provider in RESERVED_IDS:
        errors.append(f"provider {cls.provider!r} must match {PROVIDER_RE.pattern} and not be reserved")
    for attr in ("display_name", "catalog_id", "description"):
        if not isinstance(getattr(cls, attr), str) or not getattr(cls, attr).strip():
            errors.append(f"{attr} is required")
    if cls.docs_url:
        try:
            check_url(cls.docs_url, resolver=None, allowed_ports=None)
        except UnsafeURLError:
            errors.append("docs_url must be an https URL")
    else:
        errors.append("docs_url (the official API docs this connector follows) is required")
    if not isinstance(cls.required_env, tuple) or not all(isinstance(n, str) and ENV_NAME_RE.match(n)
                                                          for n in cls.required_env):
        errors.append("required_env must be a tuple of environment variable names")
    if not isinstance(cls.config_fields, tuple) or not all(isinstance(f, ConfigField) for f in cls.config_fields):
        errors.append("config_fields must be a tuple of ConfigField")
    else:
        names = [f.name for f in cls.config_fields]
        if len(names) != len(set(names)):
            errors.append("config_fields names must be unique")
    if cls.oauth is not None:
        if not isinstance(cls.oauth, OAuth2Spec):
            errors.append("oauth must be an OAuth2Spec")
        else:
            for name in ("authorize_url", "token_url", "revoke_url"):
                url = getattr(cls.oauth, name)
                if url is None and name == "revoke_url":
                    continue
                try:
                    check_url(url, resolver=None, allowed_ports=None)
                except UnsafeURLError:
                    errors.append(f"oauth.{name} must be an https URL")
            needed = [n for n in (cls.oauth.client_id_env, cls.oauth.client_secret_env) if n]
            if any(n not in cls.required_env for n in needed):
                errors.append("oauth client_id_env / client_secret_env must be listed in required_env")
    from .. import events      # late import: events imports this package
    unknown = [e for e in cls.subscribes if e not in events.EVENTS]
    if not isinstance(cls.subscribes, tuple) or unknown:
        errors.append(f"subscribes must be a tuple of known events (unknown: {unknown})")
    if cls.subscribes and cls.handle_event is Connector.handle_event:
        errors.append("a connector that subscribes to events must implement handle_event()")
    if cls.test is Connector.test:
        errors.append("test() must be implemented")
    if not isinstance(cls.unconfirmed, tuple):
        errors.append("unconfirmed must be a tuple of strings")
    return errors


def register(cls):
    """Register a connector class (discovery does this for modules in this package; tests call it directly).
    Usable as a decorator. Raises ValueError for an invalid class or a provider id taken by another class."""
    errors = validate_connector_class(cls)
    if errors:
        raise ValueError(f"{getattr(cls, '__name__', cls)}: " + "; ".join(errors))
    with _lock:
        existing = _registry.get(cls.provider)
        same = existing is not None and (existing.__module__, existing.__qualname__) == (cls.__module__,
                                                                                           cls.__qualname__)
        if existing is not None and existing is not cls and not same:      # `same`: a reloaded module
            raise ValueError(f"provider {cls.provider!r} is already registered by {existing.__module__}")
        _registry[cls.provider] = cls
    return cls


def unregister(provider: str) -> None:
    with _lock:
        _registry.pop(provider, None)


def discover(force: bool = False) -> dict[str, type[Connector]]:
    """Import every module in app/connectors (names not starting with "_") and register its Connector subclasses.
    A module that fails to import or holds an invalid connector is logged and listed in import_errors; the others
    still load (one broken connector never takes the app down)."""
    global _discovered
    with _lock:
        if _discovered and not force:
            return dict(_registry)
        for mod in pkgutil.iter_modules(__path__):
            if mod.name.startswith("_"):
                continue
            name = f"{__name__}.{mod.name}"
            try:
                module = importlib.import_module(name)
            except Exception as e:      # noqa: BLE001 - reported, not fatal
                import_errors[mod.name] = f"{type(e).__name__}: {e}"[:500]
                log.exception("connector module %s failed to import", name)
                continue
            for obj in list(vars(module).values()):
                if isinstance(obj, type) and issubclass(obj, Connector) and obj is not Connector \
                        and obj.__module__ == module.__name__ and obj.provider:
                    try:
                        register(obj)
                        import_errors.pop(mod.name, None)
                    except ValueError as e:
                        import_errors[mod.name] = str(e)[:500]
                        log.error("connector %s not registered: %s", obj.__name__, e)
        _discovered = True
        return dict(_registry)


def get(provider: str) -> type[Connector] | None:
    discover()
    return _registry.get(provider)


def all_connectors() -> list[type[Connector]]:
    discover()
    return sorted(_registry.values(), key=lambda c: (c.display_name.lower(), c.provider))


def subscribers_of(event: str) -> list[type[Connector]]:
    return [c for c in all_connectors() if event in c.subscribes]
