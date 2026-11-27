"""/api/integrations: connectors (list, settings, connect, test, disconnect, OAuth sign-in) and outbound webhooks
(Slack, Microsoft Teams, signed generic), plus the delivery log. See docs/integrations-dev.md.

Same rules as app/api.py: every route needs a session cookie (with the CSRF header on writes) or an API key; reads
of the provider list are open to every role, everything else needs owner or office. The OAuth callback is a browser
redirect from the provider, so it checks the session cookie itself and is protected by the signed, single-use
`state` (bound to tenant + user + provider + redirect URI) instead of the CSRF header.

Handlers are plain `def` (FastAPI runs them on its thread pool): they call the database and make outbound HTTP
requests, neither of which may run on the event loop. Secrets never appear in responses: secret settings come back
as names in `secrets_saved`, webhook URLs as a scheme+host hint, signing secrets only once, at creation or rotation.
"""
import logging
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from tradecomply import integrations_guide

from . import connectors as registry
from . import webhooks
from .auth import SESSION_COOKIE, Principal, anyone, office_up, platform_of, principal_from_session, set_notice
from .billing import base_url, require_entitled
from .connectors import (OAUTH_SECRET_KEY, ConfigError, ConnectorContext, ConnectorError, NotConnected, OAuthError,
                         TestResult, code_challenge, issue_oauth_state, verify_oauth_state)
from .crypto import DecryptError, webhook_context
from .events import EVENTS, TEST_EVENT
from .safe_http import SafeHTTPError
from .store import OUTBOUND_STATUSES, now_iso

log = logging.getLogger("tradecomply.integrations")
router = APIRouter(prefix="/api/integrations", tags=["integrations"])
STAFF = ("owner", "office")
MAX_WEBHOOKS = 25                  # per company


# ---------------------------------------------------------------------------- models
class ConfigIn(BaseModel):
    config: dict = Field(default_factory=dict)


class ConnectIn(BaseModel):
    config: Optional[dict] = None


class WebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2000)
    kind: Literal["slack", "teams", "generic"] = "generic"
    events: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=50)
    enabled: bool = True


class WebhookPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    url: Optional[str] = Field(None, min_length=8, max_length=2000)
    kind: Optional[Literal["slack", "teams", "generic"]] = None
    events: Optional[list[str]] = Field(None, min_length=1, max_length=50)
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------- helpers
def _connector(provider: str):
    cls = registry.get(provider)
    if cls is None:
        raise HTTPException(404, "No such integration")
    return cls


def _instance(request: Request, p: Principal, cls):
    return cls(ConnectorContext(platform_of(request), p.tenant_id, p.user_id, p.label))


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, ConnectorError):
        return HTTPException(e.status_code, str(e))
    if isinstance(e, SafeHTTPError):
        return HTTPException(502, str(e))
    return HTTPException(500, "The integration failed unexpectedly")


def _audit(request: Request, p: Principal, action: str, args: dict, ok: bool = True) -> None:
    """Integration changes go to the tenant's activity log (names and ids only, never secret values)."""
    try:
        platform_of(request).store.audit(p.tenant_id, p.label, f"integrations.{action}", args, None, ok=ok,
                                         source="integrations")
    except Exception:      # noqa: BLE001 - the change itself already happened; don't turn it into a 500
        log.exception("integration audit write failed")


def _rate_limit(request: Request, p: Principal) -> None:
    """Test sends and connection checks make the server call out; cap them per tenant."""
    limiter, key = platform_of(request).integration_test_limiter, f"tenant|{p.tenant_id}"
    wait = limiter.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many tests in a short time. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    limiter.hit(key)


def _connection_view(conn: dict | None) -> dict:
    if not conn:
        return {"status": "disconnected", "external_account": None, "last_error": None, "updated": None}
    return {k: conn.get(k) for k in ("status", "external_account", "last_error", "updated")}


def _provider_view(request: Request, p: Principal, cls, conn: dict | None) -> dict:
    env = platform_of(request).env
    view = cls.describe(env)
    if p.role in STAFF:
        view["missing_env"] = cls.missing_env(env)       # names of operator settings, never values
    view["connection"] = _connection_view(conn)
    return view


def _detail(request: Request, p: Principal, cls) -> dict:
    inst = _instance(request, p, cls)
    conn = inst.connection()
    view = _provider_view(request, p, cls, conn)
    try:
        saved = inst.secrets() if conn and conn.get("has_secret") else {}
        readable = True
    except NotConnected:
        saved, readable = {}, False
    view["config"] = (conn or {}).get("config") or {}
    view["secrets_saved"] = sorted(f.name for f in cls.config_fields if f.secret and saved.get(f.name))
    view["signed_in"] = bool((saved.get(OAUTH_SECRET_KEY) or {}).get("access_token"))
    view["secrets_readable"] = readable
    return view


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{base_url(request)}/api/integrations/{provider}/oauth/callback"


def _check_events(names: list[str]) -> list[str]:
    bad = [e for e in names if e != "*" and e not in EVENTS]
    if bad:
        raise HTTPException(422, f"Unknown event(s): {', '.join(bad[:5])}. Known: {', '.join(EVENTS)} or *")
    return sorted(set(names))


def _hook_view(request: Request, hook: dict) -> dict:
    box = platform_of(request).secret_box
    try:
        hint = webhooks.url_hint(box.decrypt_text(hook["url"], webhook_context(hook["tenant_id"])))
    except DecryptError:
        hint = "(can't be read on this server: enter the URL again)"
    return {"id": hook["id"], "name": hook["name"], "kind": hook["kind"], "events": hook["events"],
            "enabled": hook["enabled"], "created": hook["created"], "last_status": hook["last_status"],
            "last_sent": hook["last_sent"], "url_hint": hint, "signed": hook["has_signing_secret"]}


def _hook_or_404(request: Request, p: Principal, webhook_id: int, with_secret: bool = False) -> dict:
    hook = platform_of(request).store.get_webhook(p.tenant_id, webhook_id, with_secret=with_secret)
    if not hook:
        raise HTTPException(404, "Webhook not found")
    return hook


def _validated_url(request: Request, url: str, kind: str) -> str:
    with platform_of(request).http() as http:
        try:
            return webhooks.validate_url(url, kind, http)
        except ValueError as e:
            raise HTTPException(422, str(e))


# ---------------------------------------------------------------------------- catalog
@router.get("")
def list_integrations(request: Request, p: Principal = Depends(anyone)):
    """Every installed connector with its operator status (configured on this server?) and this tenant's
    connection, plus the event catalog."""
    store = platform_of(request).store
    conns = {c["provider"]: c for c in store.list_connections(p.tenant_id)}
    out = {"providers": [_provider_view(request, p, cls, conns.get(cls.provider)) for cls in registry.all_connectors()],
           "events": [{"name": k, "description": v} for k, v in EVENTS.items()]}
    if p.role in STAFF:
        out["load_errors"] = sorted(registry.import_errors)
    return out


@router.get("/events")
def list_events(p: Principal = Depends(anyone)):
    return {"events": [{"name": k, "description": v} for k, v in EVENTS.items()]}


# ---------------------------------------------------------------------------- product catalog (before /{provider})
@router.get("/catalog")
def integrations_catalog(request: Request, p: Principal = Depends(anyone)):
    """The integrations catalog (tradecomply/data/integrations.json): every product a contractor may use with
    TradeComply and how (built in, alongside in an AI assistant, import/export, reference), with this server's own
    MCP URL (the catalog file holds no server address: every install points its users at itself). The same for every
    signed-in user and free of secrets, so it may be cached (the app keeps a copy for offline use)."""
    cat = integrations_guide.load_catalog()
    body = {**cat, "tradecomply": {**cat["tradecomply"], "mcp_url": f"{base_url(request)}/mcp"},
            "config_ready_ids": integrations_guide.config_ready_ids()}
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=300"})


@router.get("/catalog/mcp-config")
def catalog_mcp_config(request: Request, ids: str = Query(..., max_length=1000), p: Principal = Depends(anyone)):
    """Ready-to-paste MCP client settings joining this server's /mcp with the chosen catalog products' confirmed
    remote MCP endpoints (`ids` comma-separated, e.g. box,docusign). Same output as the mcp_client_config MCP tool."""
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    if not wanted:
        raise HTTPException(422, "Choose at least one product")
    try:
        result = integrations_guide.mcp_client_config(wanted, f"{base_url(request)}/mcp", trusted_url=True)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return result.model_dump()


# ---------------------------------------------------------------------------- webhooks (before /{provider})
@router.get("/webhooks")
def list_webhooks(request: Request, p: Principal = Depends(office_up)):
    return [_hook_view(request, h) for h in platform_of(request).store.list_webhooks(p.tenant_id)]


@router.post("/webhooks", status_code=201)
def create_webhook(body: WebhookIn, request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)
    require_entitled(platform, p)
    if len(platform.store.list_webhooks(p.tenant_id)) >= MAX_WEBHOOKS:
        # Every event is sent to every webhook: without a cap one company could make this server fan each event
        # out to thousands of copies of one URL.
        raise HTTPException(409, f"A company can have at most {MAX_WEBHOOKS} webhooks. Delete one you no longer use.")
    events = _check_events(body.events)
    url = _validated_url(request, body.url, body.kind)
    ctx = webhook_context(p.tenant_id)
    secret = webhooks.new_signing_secret() if body.kind == "generic" else None
    hook = platform.store.create_webhook(
        p.tenant_id, name=body.name.strip(), url=platform.secret_box.encrypt(url, ctx), kind=body.kind, events=events,
        signing_secret_blob=platform.secret_box.encrypt(secret, ctx) if secret else None, enabled=body.enabled)
    _audit(request, p, "webhook_create", {"webhook_id": hook["id"], "kind": body.kind, "events": events})
    out = _hook_view(request, hook)
    if secret:
        out["signing_secret"] = secret
        out["message"] = "Copy the signing secret now. It won't be shown again."
    return out


@router.get("/webhooks/{webhook_id}")
def get_webhook(webhook_id: int, request: Request, p: Principal = Depends(office_up)):
    return _hook_view(request, _hook_or_404(request, p, webhook_id))


@router.patch("/webhooks/{webhook_id}")
def patch_webhook(webhook_id: int, body: WebhookPatch, request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)
    hook = _hook_or_404(request, p, webhook_id, with_secret=True)
    ctx, box = webhook_context(p.tenant_id), platform.secret_box
    changes, secret = {}, None
    kind = body.kind or hook["kind"]
    if body.name is not None:
        changes["name"] = body.name.strip()
    if body.events is not None:
        changes["events"] = _check_events(body.events)
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.url is not None or (body.kind and body.kind != hook["kind"]):
        if body.url is not None:
            url = body.url
        else:
            try:
                url = box.decrypt_text(hook["url"], ctx)
            except DecryptError:
                raise HTTPException(422, "The saved URL can't be read on this server; send the URL again")
        changes["url"] = box.encrypt(_validated_url(request, url, kind), ctx)
    if body.kind is not None:
        changes["kind"] = kind
        if kind == "generic" and not hook.get("signing_secret_blob"):
            secret = webhooks.new_signing_secret()
            changes["signing_secret_blob"] = box.encrypt(secret, ctx)
        elif kind != "generic":
            changes["signing_secret_blob"] = None
    hook = platform.store.update_webhook(p.tenant_id, webhook_id, **changes)
    _audit(request, p, "webhook_update", {"webhook_id": webhook_id, "fields": sorted(changes)})
    out = _hook_view(request, hook)
    if secret:
        out["signing_secret"] = secret
        out["message"] = "Copy the signing secret now. It won't be shown again."
    return out


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(webhook_id: int, request: Request, p: Principal = Depends(office_up)):
    if not platform_of(request).store.delete_webhook(p.tenant_id, webhook_id):
        raise HTTPException(404, "Webhook not found")
    _audit(request, p, "webhook_delete", {"webhook_id": webhook_id})
    return {"ok": True}


@router.post("/webhooks/{webhook_id}/rotate-secret")
def rotate_webhook_secret(webhook_id: int, request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)
    hook = _hook_or_404(request, p, webhook_id)
    if hook["kind"] != "generic":
        raise HTTPException(409, "Only generic webhooks are signed")
    secret = webhooks.new_signing_secret()
    hook = platform.store.update_webhook(p.tenant_id, webhook_id, signing_secret_blob=platform.secret_box.encrypt(
        secret, webhook_context(p.tenant_id)))
    _audit(request, p, "webhook_rotate_secret", {"webhook_id": webhook_id})
    return {**_hook_view(request, hook), "signing_secret": secret,
            "message": "Copy the new signing secret now. The old one stops working immediately."}


@router.post("/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: int, request: Request, p: Principal = Depends(office_up)):
    """Send a test message now (not through the queue) and report what the receiver answered."""
    platform = platform_of(request)
    require_entitled(platform, p)
    hook = _hook_or_404(request, p, webhook_id, with_secret=True)
    _rate_limit(request, p)
    ctx, box = webhook_context(p.tenant_id), platform.secret_box
    try:
        url = box.decrypt_text(hook["url"], ctx)
        secret = box.decrypt_text(hook["signing_secret_blob"], ctx) if hook.get("signing_secret_blob") else None
    except DecryptError:
        raise HTTPException(409, "The saved URL can't be read on this server; enter it again")
    payload = {"title": "Test message from TradeComply", "severity": "info",
               "summary": f"If you can read this, the webhook “{hook['name']}” works.", "link": "/#/settings"}
    created = now_iso()
    with platform.http(retries=0) as http:
        result = webhooks.send(http, hook["kind"], url, TEST_EVENT, payload, created=created, signing_secret=secret,
                               msg_id=f"msg_test_{hook['id']}_{created[:19].replace('-', '').replace(':', '')}",
                               app_url=platform.settings.app_url or base_url(request))
    changes = {"last_status": result.summary, "last_sent": created}
    if result.disable:
        changes["enabled"] = False
    platform.store.update_webhook(p.tenant_id, webhook_id, **changes)
    _audit(request, p, "webhook_test", {"webhook_id": webhook_id, "status": result.status}, ok=result.ok)
    return {"ok": result.ok, "status": result.status, "message": result.message,
            "disabled": bool(result.disable)}


@router.get("/outbound")
def list_outbound(request: Request, p: Principal = Depends(office_up), status: Optional[str] = None,
                  limit: int = Query(100, ge=1, le=500), before_id: Optional[int] = None):
    """The delivery log: recent events and where each went, newest first."""
    if status is not None and status not in OUTBOUND_STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(OUTBOUND_STATUSES)}")
    rows = platform_of(request).store.list_outbound(p.tenant_id, status=status, limit=limit, before_id=before_id)
    return [{"id": r["id"], "event": r["event"], "target": r["target"], "status": r["status"],
             "attempts": r["attempts"], "next_attempt": r["next_attempt"], "created": r["created"],
             "last_error": r["last_error"], "title": (r.get("payload") or {}).get("title")} for r in rows]


# ---------------------------------------------------------------------------- one provider
@router.get("/{provider}")
def get_integration(provider: str, request: Request, p: Principal = Depends(office_up)):
    return _detail(request, p, _connector(provider))


@router.get("/{provider}/config")
def get_config(provider: str, request: Request, p: Principal = Depends(office_up)):
    cls = _connector(provider)
    d = _detail(request, p, cls)
    return {"provider": provider, "config": d["config"], "secrets_saved": d["secrets_saved"],
            "config_schema": d["config_schema"]}


def _save_config(request: Request, p: Principal, cls, values: dict) -> None:
    inst = _instance(request, p, cls)
    conn = inst.connection()
    try:
        current = inst.secrets() if conn and conn.get("has_secret") else {}
    except NotConnected:
        current = {}            # unreadable (key changed): start over with what was submitted
    try:
        config, secret_fields = cls.clean_config(values, (conn or {}).get("config"), current)
    except ConfigError as e:
        raise HTTPException(422, str(e))
    secrets = {**secret_fields, **({OAUTH_SECRET_KEY: current[OAUTH_SECRET_KEY]} if OAUTH_SECRET_KEY in current
                                   else {})}
    inst.save(config=config, secrets=secrets or None,
              **({} if conn else {"status": "disconnected"}))
    _audit(request, p, "config", {"provider": cls.provider, "fields": sorted(values)})


@router.put("/{provider}/config")
def put_config(provider: str, body: ConfigIn, request: Request, p: Principal = Depends(office_up)):
    """Save this tenant's settings. Secret fields are encrypted; a blank secret field keeps the saved value."""
    cls = _connector(provider)
    _save_config(request, p, cls, body.config)
    return _detail(request, p, cls)


@router.post("/{provider}/connect")
def connect(provider: str, request: Request, body: Optional[ConnectIn] = None, p: Principal = Depends(office_up)):
    """Settings-based connectors: save the settings (if sent), check them with the provider, mark connected.
    OAuth connectors: returns {"authorize_url"} to send the browser to (same as /oauth/start)."""
    platform = platform_of(request)
    require_entitled(platform, p)
    cls = _connector(provider)
    if cls.oauth:
        return oauth_start(provider, request, p)
    _rate_limit(request, p)
    if body and body.config is not None:
        _save_config(request, p, cls, body.config)
    inst = _instance(request, p, cls)
    try:
        result = inst.connect()
    except Exception as e:      # noqa: BLE001 - mapped to a plain message
        _audit(request, p, "connect", {"provider": provider}, ok=False)
        if not isinstance(e, (ConnectorError, SafeHTTPError)):
            log.exception("connector %s connect() failed", provider)
        raise _http_error(e)
    _audit(request, p, "connect", {"provider": provider}, ok=result.get("status") == "connected")
    return result


@router.post("/{provider}/disconnect")
def disconnect(provider: str, request: Request, p: Principal = Depends(office_up)):
    cls = _connector(provider)
    try:
        result = _instance(request, p, cls).disconnect()
    except Exception as e:      # noqa: BLE001
        if not isinstance(e, (ConnectorError, SafeHTTPError)):
            log.exception("connector %s disconnect() failed", provider)
        raise _http_error(e)
    _audit(request, p, "disconnect", {"provider": provider})
    return result


@router.post("/{provider}/test")
def test_connection(provider: str, request: Request, p: Principal = Depends(office_up)):
    """Run the connector's read-only check with the saved credentials. A connected connection that fails moves to
    `error` (and back to `connected` when it passes again)."""
    platform = platform_of(request)
    require_entitled(platform, p)
    cls = _connector(provider)
    _rate_limit(request, p)
    inst = _instance(request, p, cls)
    conn = inst.connection()
    try:
        inst.require_operator()
        result = inst.test()
    except (ConnectorError, SafeHTTPError) as e:
        if isinstance(e, ConnectorError) and e.status_code in (409, 503):
            raise _http_error(e)
        result = TestResult(False, str(e))
    except NotImplementedError:
        raise HTTPException(501, f"{cls.display_name} has no connection test")
    if conn and conn["status"] in ("connected", "error"):
        if result.ok:
            inst.save(status="connected", last_error=None,
                      **({"external_account": result.account} if result.account else {}))
        else:
            inst.save(status="error", last_error=result.message)
    _audit(request, p, "test", {"provider": provider}, ok=result.ok)
    return {**result.to_dict(), "connection": _connection_view(inst.connection())}


# ---------------------------------------------------------------------------- OAuth
@router.post("/{provider}/oauth/start")
def oauth_start(provider: str, request: Request, p: Principal = Depends(office_up)):
    """Begin an OAuth sign-in: returns the provider's authorization URL (with a signed single-use state and a PKCE
    challenge). The browser goes there; the provider sends it back to /oauth/callback."""
    platform = platform_of(request)
    require_entitled(platform, p)
    cls = _connector(provider)
    if not cls.oauth:
        raise HTTPException(409, f"{cls.display_name} doesn't use sign-in; save its settings and connect instead")
    if p.user_id is None:
        raise HTTPException(403, "Sign in to the TradeComply app to connect an account (API keys can't)")
    inst = _instance(request, p, cls)
    redirect_uri = _redirect_uri(request, provider)
    state, verifier = issue_oauth_state(platform, p.tenant_id, p.user_id, provider, redirect_uri)
    try:
        url = inst.authorization_url(redirect_uri, state, code_challenge(verifier) if cls.oauth.pkce else None)
    except ConnectorError as e:
        raise _http_error(e)
    conn = inst.connection()
    if not conn or conn["status"] in ("disconnected", "error"):
        inst.save(status="pending")
    _audit(request, p, "oauth_start", {"provider": provider})
    return {"authorize_url": url, "redirect_uri": redirect_uri}


def _back(request: Request, provider: str, result: str, message: str | None = None) -> RedirectResponse:
    """Into the app's settings page. A message goes in a cookie only this server can set (auth.set_notice), never
    in the URL, so a link can't put its own words on the page as if TradeComply said them."""
    resp = RedirectResponse("/#/settings?" + urlencode({"integration": provider, "result": result}), status_code=303)
    if message:
        set_notice(resp, request, message)
    return resp


@router.get("/{provider}/oauth/callback")
def oauth_callback(provider: str, request: Request, code: Optional[str] = Query(None, max_length=4096),
                   state: Optional[str] = Query(None, max_length=8192),
                   error: Optional[str] = Query(None, max_length=200)):
    """Where the provider sends the browser back. Always answers with a redirect into the app's settings page
    (?integration=<provider>&result=connected|error; a message in the tc_notice cookie)."""
    platform = platform_of(request)
    cls = registry.get(provider)
    if cls is None or not cls.oauth:
        return _back(request, provider, "error", "Unknown integration")
    p, _ = principal_from_session(platform, request.cookies.get(SESSION_COOKIE))
    if p is None:
        return RedirectResponse("/#/login", status_code=303)
    if p.role not in STAFF:
        return _back(request, provider, "error", "Only the owner or office staff can connect integrations")
    try:
        st = verify_oauth_state(platform, state, tenant_id=p.tenant_id, user_id=p.user_id, provider=provider)
    except OAuthError as e:
        _audit(request, p, "oauth_callback", {"provider": provider, "error": e.code}, ok=False)
        return _back(request, provider, "error", str(e))
    inst = _instance(request, p, cls)
    if error or not code:
        reason = error if error and error.replace("_", "").isalnum() else "no authorization code"
        inst.save(status="error", last_error=f"Sign-in was not completed ({reason})")
        _audit(request, p, "oauth_callback", {"provider": provider, "error": reason}, ok=False)
        return _back(request, provider, "error", f"{cls.display_name} sign-in was not completed ({reason})")
    try:
        inst.complete_oauth(code, st["redirect_uri"], st["code_verifier"])
    except Exception as e:      # noqa: BLE001 - every failure becomes a message on the settings page
        if not isinstance(e, (ConnectorError, SafeHTTPError)):
            log.exception("connector %s OAuth completion failed", provider)
        message = str(e) if isinstance(e, (ConnectorError, SafeHTTPError)) else "Connecting failed unexpectedly"
        inst.save(status="error", last_error=message)
        _audit(request, p, "oauth_callback", {"provider": provider}, ok=False)
        return _back(request, provider, "error", message)
    _audit(request, p, "oauth_callback", {"provider": provider})
    return _back(request, provider, "connected")
