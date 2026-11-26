"""Runtime settings (from environment variables) and the per-app platform context shared by every request."""
import os
from dataclasses import dataclass, field

from .auth import RateLimiter, Signer, load_secret_key
from .crypto import SecretBox
from .store import Store, database_url_from_env, default_data_dir


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: str = field(default_factory=default_data_dir)
    database_url: str | None = None       # postgres:// or postgresql:// selects PostgreSQL; unset = SQLite in data_dir
    mcp_require_key: bool = False
    stripe_secret_key: str | None = None
    stripe_price_id: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_api_base: str = "https://api.stripe.com"
    app_url: str | None = None            # public base URL, e.g. https://tradecomply.onrender.com
    cookie_secure: bool = False           # force the Secure cookie flag (it is also set automatically over https)
    local_mode: bool = False              # python -m app.local: one company on a laptop
    trial_days: int = 14
    max_body_bytes: int = 8 * 1024 * 1024
    events_background: bool = True        # deliver integration events on a background thread (EVENTS_WORKER=0)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls(
            data_dir=default_data_dir(),
            database_url=database_url_from_env(),
            mcp_require_key=_flag("MCP_REQUIRE_KEY"),
            stripe_secret_key=os.getenv("STRIPE_SECRET_KEY") or None,
            stripe_price_id=os.getenv("STRIPE_PRICE_ID") or None,
            stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET") or None,
            stripe_api_base=os.getenv("STRIPE_API_BASE") or "https://api.stripe.com",
            app_url=(os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/") or None,
            cookie_secure=_flag("COOKIE_SECURE"),
            local_mode=_flag("TC_LOCAL"),
            events_background=(os.getenv("EVENTS_WORKER") or "1").strip().lower() not in ("0", "false", "no",
                                                                                         "off"),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)


def signing_key(store: Store) -> bytes:
    """SECRET_KEY if set (validated by load_secret_key). Otherwise a generated key that outlives the process: on
    PostgreSQL it is kept in the database (table app_secrets), so it survives redeploys of a server with no
    persistent disk and every instance shares it; on SQLite it is kept in DATA_DIR/secret_key, next to the database."""
    if os.getenv("SECRET_KEY") or store.backend == "sqlite":
        return load_secret_key(store.data_dir)
    return store.shared_secret("secret_key").encode()


class Platform:
    """Everything a request handler needs: settings, the database, the signing key, rate limiters, the MCP server,
    the integration secret box, outbound HTTP and the event bus."""

    def __init__(self, settings: Settings, mcp):
        from . import events
        self.settings = settings
        self.store = Store(settings.data_dir, database_url=settings.database_url)
        key = signing_key(self.store)
        self.signer = Signer(key)
        self.secret_box = SecretBox.from_env(key)          # integration secrets at rest (app/crypto.py)
        self.mcp = mcp
        self.login_limiter = RateLimiter(limit=5, window_s=15 * 60)       # failures per IP + email
        self.email_limiter = RateLimiter(limit=20, window_s=15 * 60)      # failures per email, any IP
        self.signup_limiter = RateLimiter(limit=10, window_s=60 * 60)     # signups per IP
        self.signup_global_limiter = RateLimiter(limit=500, window_s=60 * 60)   # all signups (IPs can be spoofed)
        self.mcp_anon_limiter = RateLimiter(limit=60, window_s=60)        # anonymous /mcp tool calls per IP
        self.mcp_anon_global_limiter = RateLimiter(limit=300, window_s=60)    # all anonymous calls (IP spoofing)
        self.mcp_key_limiter = RateLimiter(limit=600, window_s=60)        # /mcp tool calls per API key
        self.local_login_token: str | None = None                        # one-time link printed by app.local
        self.local_user_id: int | None = None
        self.tool_cache: tuple[float, list] | None = None
        self.anon_audit_writes = 0
        self.integration_test_limiter = RateLimiter(limit=30, window_s=10 * 60)   # test sends/checks per tenant
        # Integrations: operator env (tests replace it with a dict), and the outbound HTTP hooks tests swap for an
        # httpx.MockTransport and a fake resolver (see tests/support.py). Production: None = the guarded transport
        # and system DNS.
        self.env = os.environ
        self.http_transport = None
        self.http_resolver = None
        self.http_allow_localhost = False
        self.events = events.EventBus(self, background=settings.events_background)
        events.set_default(self.events)

    def http(self, **overrides):
        """A SafeHTTP client for outbound calls (connectors, webhooks). Use as a context manager."""
        from .safe_http import SafeHTTP
        kw = {"transport": self.http_transport, "resolver": self.http_resolver,
              "allow_localhost": self.http_allow_localhost}
        kw.update(overrides)
        return SafeHTTP(**kw)
