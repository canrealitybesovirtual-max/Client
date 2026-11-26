"""ASGI entry point: `uvicorn app.main:app` (Render start command).

One app, three parts:
  /api/*   FastAPI JSON API (accounts, tools, projects, subs, documents, field logs, dashboard, billing,
           integrations: /api/integrations, see docs/integrations-dev.md)
  /        the offline-capable web app (app/static)
  /mcp     the FastMCP server from server.py (streamable HTTP), behind an API-key / OAuth gate, per-key tool scopes
           and rate limits, and the MCP call log (app/auth.py MCPGate, app/mcp_governance.py, docs/mcp-security.md)
  /oauth/*, /.well-known/oauth-*   "Sign in with TradeComply" for MCP clients such as Claude (app/oauth.py)
  /health  health check
The MCP app is created with path="/mcp" and mounted at the root (after every other route), so the endpoint is
exactly /mcp with no redirect, and custom routes registered on the MCP server keep working. Its lifespan (the MCP
session manager) is combined with the API's lifespan.
"""
import logging
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from tradecomply import cslb

from . import account, api, auth, billing, connectors, integrations_api, mcp_governance, oauth, sso
from .connectors import alerts, calendar_feed, docusign, procore, schedule_import, splunk, twilio_sms
from .config import Platform, Settings

log = logging.getLogger("tradecomply")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class SecurityHeaders:
    """Adds browser security headers to everything except /mcp, plus no-store on API responses (and HSTS over
    https), and caps request bodies: by Content-Length up front, and by counting the bytes actually received, so a
    chunked upload without a Content-Length can't stream an unbounded body into memory."""

    def __init__(self, app, max_body: int):
        self.app, self.max_body = app, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if auth._is_mcp(path):
            return await self.app(scope, receive, send)
        is_api = path.startswith("/api/")
        headers_in = dict(scope.get("headers", []))
        https = scope.get("scheme") == "https" or \
            headers_in.get(b"x-forwarded-proto", b"").split(b",")[0].strip() == b"https"
        # The body cap applies to every path except /mcp (which has its own): /api and any custom HTTP routes the
        # MCP server's modules register.
        v = headers_in.get(b"content-length", b"")
        if v.isdigit() and int(v) > self.max_body:
            body = b'{"detail":"Request too large"}'
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        received, limit, inner_receive = 0, self.max_body, receive

        async def receive():
            nonlocal received
            message = await inner_receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "Request too large")
            return message

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                         (b"x-frame-options", b"DENY"), (b"content-security-policy", CSP.encode()),
                         (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
                if is_api and b"cache-control" not in names:
                    extra.append((b"cache-control", b"no-store"))
                if https:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                headers += [h for h in extra if h[0] not in names]
                message = {**message, "headers": headers}
            await send(message)
        await self.app(scope, receive, wrapped)


class GZipPublic:
    """Compresses only responses that hold no secrets and are the same for everyone: the app shell, static files
    and the tool list (about 400 KB of JSON schemas, 10x smaller gzipped, which matters on a phone). Responses
    carrying keys, tokens or account data are never compressed, so BREACH-style length attacks have nothing to
    read. The integrations catalog (about 300 KB of JSON, the same for every signed-in user) is compressed too."""
    PATHS = {"/", "/index.html", "/sw.js", "/manifest.webmanifest", "/api/tools", "/api/docs", "/api/openapi.json",
             "/api/integrations/catalog"}

    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in ("GET", "HEAD") and (
                scope.get("path") in self.PATHS or scope.get("path", "").startswith("/static/")):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


class RedactTokens(logging.Filter):
    """Keeps secrets that travel in URLs (private calendar feed links, the local one-time login link, the OAuth
    authorization code and state on /api/integrations/*/oauth/callback, and a pending sign-in request's id in the
    path of /api/oauth/requests/<id>) out of the access log: `token=...` becomes `token=[redacted]`."""
    PATTERN = re.compile(r"((?:^|[?&])(?:token|key|api_key|access_token|code|state)=)[^&\s\"]+", re.I)
    PATH = re.compile(r"(/api/oauth/requests/)[^/?\s\"]+")

    def _redact(self, text: str) -> str:
        return self.PATH.sub(r"\1[redacted]", self.PATTERN.sub(r"\1[redacted]", text))

    def filter(self, record):
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(self._redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        return True


def combine_lifespans(*lifespans):
    """Enter each lifespan in order, exit in reverse (same as fastmcp.utilities.lifespan.combine_lifespans)."""
    @asynccontextmanager
    async def combined(app):
        async with AsyncExitStack() as stack:
            for ls in lifespans:
                await stack.enter_async_context(ls(app))
            yield
    return combined


def _static(name: str, media_type: str | None = None, cache: str = "no-cache"):
    path = os.path.join(STATIC_DIR, name)

    async def handler():
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache})
    return handler


class StaticNoCache(StaticFiles):
    """/static/* with Cache-Control: no-cache. Without it a browser treats the files as fresh for a while after
    they change (heuristic caching from Last-Modified), so a new release's service worker would copy the old
    app.js into its cache."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("cache-control", "no-cache")
        return response


SHELL_ASSETS = {"app.js": "text/javascript", "styles.css": "text/css"}


async def _release_asset(release: str, name: str):
    """The app shell's script and styles under a per-release path (/static/v<N>/app.js, the path index.html and
    sw.js name): an earlier release's service worker serves its cached /static/app.js (ignoring any ?query), but
    has nothing cached for a path it never saw, so the first page load after a deploy gets this release's code."""
    if not release.isdigit() or len(release) > 6 or name not in SHELL_ASSETS:
        raise HTTPException(404, "Not Found")
    return FileResponse(os.path.join(STATIC_DIR, name), media_type=SHELL_ASSETS[name],
                        headers={"Cache-Control": "no-cache"})


def create_app(settings: Settings | None = None, mcp_server=None) -> FastAPI:
    if mcp_server is None:
        from server import mcp as mcp_server
    settings = settings or Settings.from_env()
    platform = Platform(settings, mcp_server)
    mcp_app = mcp_server.http_app(path="/mcp")

    @asynccontextmanager
    async def api_lifespan(_app):
        store = platform.store
        log.info("TradeComply platform ready (storage %s, data dir %s, MCP key required: %s (%s), Stripe: %s)",
                 store.describe(), store.data_dir, settings.mcp_require_key, settings.mcp_access_reason,
                 settings.stripe_enabled)
        for warning in settings.mcp_access_warnings:
            log.warning("MCP access: %s", warning)
        found = connectors.discover()
        log.info("integrations: %d connector(s) registered%s", len(found),
                 f"; failed to load: {', '.join(sorted(connectors.import_errors))}" if connectors.import_errors else "")
        platform.events.start()           # starts a thread only; no database call on the event loop
        if settings.events_background:    # same switch as the event worker (tests drive both by hand)
            splunk.start_exporter(platform)   # background HEC export thread; idle until a company connects Splunk
        if not store.persistent:
            log.warning("STORAGE IS NOT PERSISTENT: SQLite at %s is on this instance's own filesystem, which Render "
                        "replaces on every deploy and restart, so accounts, projects and logs will be lost. Set "
                        "DATABASE_URL to a PostgreSQL database (README: Durable storage) or mount a persistent disk "
                        "at DATA_DIR.", store.path)
        try:
            yield
        finally:
            await anyio.to_thread.run_sync(splunk.stop_exporter, platform)   # thread join: off the event loop
            await anyio.to_thread.run_sync(platform.events.stop)    # joins the worker: off the event loop
            store.close()

    app = FastAPI(title="TradeComply CA", version="1.0", lifespan=combine_lifespans(api_lifespan, mcp_app.lifespan),
                  docs_url=None, openapi_url="/api/openapi.json", redoc_url=None)
    app.state.platform = platform
    app.add_middleware(SecurityHeaders, max_body=settings.max_body_bytes)
    app.add_middleware(GZipPublic)
    if settings.local_mode:      # DNS-rebinding guard for a server bound to 127.0.0.1
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    app.include_router(auth.router)
    app.include_router(billing.router)
    app.include_router(api.router)
    app.include_router(account.router)     # GET /api/tenant/export, DELETE /api/tenant (app/account.py)
    # Connector routers first: their fixed paths (/api/integrations/docusign/...) must win over the generic
    # /api/integrations/{provider}/... routes.
    for connector_router in (docusign.router, procore.router, alerts.router, twilio_sms.router,
                             calendar_feed.router, schedule_import.router, splunk.router):
        app.include_router(connector_router)
    app.include_router(sso.router)
    app.include_router(integrations_api.router)
    app.include_router(mcp_governance.router)
    app.include_router(oauth.router)       # discovery, /oauth/register, /authorize, /token, /revoke (app/oauth.py)
    app.include_router(oauth.api)          # /api/oauth: the consent screen and connected apps

    @app.get("/health", include_in_schema=False)
    async def health():
        store = platform.store
        try:
            schema = await anyio.to_thread.run_sync(store.schema_version)     # a database round trip on Postgres
        except Exception as e:     # noqa: BLE001 - reported as a failed health check, not a 500 traceback
            log.error("health check: storage %s is not answering (%s)", store.describe(), type(e).__name__)
            return JSONResponse({"ok": False, "server": "TradeComply CA", "storage": store.backend,
                                 "error": "storage unavailable"}, status_code=503)
        return JSONResponse({"ok": True, "server": "TradeComply CA", "schema": schema,
                             "license_data": cslb.snapshot_date() or "demo fixtures",
                             "storage": store.backend, "storage_persistent": store.persistent})

    # API reference: a same-origin page (FastAPI's Swagger UI loads scripts from a CDN, which the CSP blocks, and
    # third-party script on this origin could read the CSRF cookie anyway).
    app.add_api_route("/api/docs", _static("docs.html", "text/html"), include_in_schema=False)
    app.add_api_route("/", _static("index.html", "text/html"), include_in_schema=False)
    app.add_api_route("/index.html", _static("index.html", "text/html"), include_in_schema=False)
    app.add_api_route("/sw.js", _static("sw.js", "text/javascript"), include_in_schema=False)
    app.add_api_route("/manifest.webmanifest", _static("manifest.webmanifest", "application/manifest+json"),
                      include_in_schema=False)
    app.add_api_route("/icon.svg", _static("icon.svg", "image/svg+xml", "public, max-age=86400"),
                      include_in_schema=False)
    app.add_api_route("/static/v{release}/{name}", _release_asset, include_in_schema=False)
    app.mount("/static", StaticNoCache(directory=STATIC_DIR), name="static")
    app.mount("/", auth.MCPGate(mcp_app, platform))        # last: /mcp plus the MCP server's custom routes
    return app


logging.basicConfig(level=logging.INFO)
logging.getLogger("uvicorn.access").addFilter(RedactTokens())
if os.getenv("DATA_DIR") and not os.getenv("LICENSE_DB"):
    # Keep CSLB data on the same persistent disk as the app database.
    os.environ["LICENSE_DB"] = os.path.join(os.path.abspath(os.path.expanduser(os.environ["DATA_DIR"])),
                                            "cslb.sqlite")
if os.getenv("CSLB_AUTO_DOWNLOAD") == "1":
    cslb.start_auto_refresh()   # builds real license data on boot from the bundled seed, then refreshes daily

app = create_app()
