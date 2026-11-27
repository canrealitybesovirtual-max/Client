"""Runtime settings (from environment variables) and the per-app platform context shared by every request."""
import logging
import os
import re
from dataclasses import dataclass, field

from .auth import RateLimiter, Signer, load_secret_key
from .crypto import SecretBox
from .mailer import Mailer
from .store import Store, database_url_from_env, default_data_dir

log = logging.getLogger("tradecomply.config")


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


SUPPORT_EMAIL_RE = re.compile(r"^[^@\s<>\"'`]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?\.[A-Za-z]{2,63}$")
SUPPORT_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[^\s<>\"'`]*)?$")


def _proxy_hops(value: str | None) -> int:
    """TRUSTED_PROXY_HOPS: how many proxies in front of the app each append the address they saw to
    X-Forwarded-For (auth.client_ip). Unset or invalid: 0, the first entry (which a client can set)."""
    v = (value or "").strip()
    if not v:
        return 0
    if not v.isdigit() or not 0 <= int(v) <= 5:
        log.warning("TRUSTED_PROXY_HOPS must be a whole number from 0 to 5; ignored")
        return 0
    return int(v)


def _support_email(value: str | None) -> str | None:
    """The operator's support address. There is no built-in one: unset means the app tells customers to ask whoever
    runs their TradeComply server. A malformed value is ignored with a warning instead of being shown."""
    v = (value or "").strip()
    if not v:
        return None
    if len(v) > 254 or not SUPPORT_EMAIL_RE.match(v):
        log.warning("SUPPORT_EMAIL is not a plain email address; ignored")
        return None
    return v


def _support_url(value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if len(v) > 500 or not SUPPORT_URL_RE.match(v):
        log.warning("SUPPORT_URL must be an http(s):// address with no spaces or quotes; ignored")
        return None
    return v


@dataclass
class Settings:
    data_dir: str = field(default_factory=default_data_dir)
    database_url: str | None = None       # postgres:// or postgresql:// selects PostgreSQL; unset = SQLite in data_dir
    mcp_require_key: bool = False         # see mcp_governance.resolve_mcp_access (on Render: on unless MCP_PUBLIC_DEMO=1)
    mcp_public_demo: bool = False
    mcp_access_reason: str = "default"
    mcp_access_warnings: tuple = ()
    mcp_allowed_origins: tuple = ()        # extra browser origins allowed to call /mcp (MCP_ALLOWED_ORIGINS)
    stripe_secret_key: str | None = None
    stripe_price_id: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_api_base: str = "https://api.stripe.com"
    app_url: str | None = None            # public base URL, e.g. https://app.your-company.example
    support_email: str | None = None      # operator's support contact shown to customers (SUPPORT_EMAIL)
    support_url: str | None = None        # operator's support/contact page (SUPPORT_URL, https:// or http://)
    cookie_secure: bool = False           # force the Secure cookie flag (it is also set automatically over https)
    local_mode: bool = False              # python -m app.local: one company on a laptop
    trusted_proxy_hops: int = 0           # TRUSTED_PROXY_HOPS: proxies that append to X-Forwarded-For (auth.client_ip)
    trial_days: int = 14
    max_body_bytes: int = 8 * 1024 * 1024
    events_background: bool = True        # deliver integration events on a background thread (EVENTS_WORKER=0)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        from .mcp_governance import parse_origins, resolve_mcp_access
        access = resolve_mcp_access(os.environ)
        s = cls(
            data_dir=default_data_dir(),
            database_url=database_url_from_env(),
            mcp_require_key=access.require_key,
            mcp_public_demo=access.public_demo,
            mcp_access_reason=access.reason,
            mcp_access_warnings=access.warnings,
            mcp_allowed_origins=parse_origins(os.getenv("MCP_ALLOWED_ORIGINS")),
            stripe_secret_key=os.getenv("STRIPE_SECRET_KEY") or None,
            stripe_price_id=os.getenv("STRIPE_PRICE_ID") or None,
            stripe_webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET") or None,
            stripe_api_base=os.getenv("STRIPE_API_BASE") or "https://api.stripe.com",
            app_url=(os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/") or None,
            support_email=_support_email(os.getenv("SUPPORT_EMAIL")),
            support_url=_support_url(os.getenv("SUPPORT_URL")),
            cookie_secure=_flag("COOKIE_SECURE"),
            local_mode=_flag("TC_LOCAL"),
            trusted_proxy_hops=_proxy_hops(os.getenv("TRUSTED_PROXY_HOPS")),
            events_background=(os.getenv("EVENTS_WORKER") or "1").strip().lower() not in ("0", "false", "no",
                                                                                         "off"),
        )
        for k, v in overrides.items():
            setattr(s, k, v)
        if "mcp_require_key" in overrides and "mcp_public_demo" not in overrides:
            s.mcp_public_demo = not s.mcp_require_key and s.mcp_public_demo
            s.mcp_access_reason = "set by the caller"
        return s

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_price_id)

    @property
    def support_contact(self) -> dict | None:
        """{"email", "url"} from SUPPORT_EMAIL / SUPPORT_URL for customer-facing "contact us" text, or None when the
        operator set neither (never a built-in address)."""
        if not (self.support_email or self.support_url):
            return None
        return {"email": self.support_email, "url": self.support_url}


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
        self.mcp_key_limiter = RateLimiter(limit=600, window_s=60)        # /mcp tool calls per API key (default)
        self.mcp_tenant_limiter = RateLimiter(limit=1200, window_s=60)    # all of a company's keys together
        self.mcp_reject_log_limiter = RateLimiter(limit=30, window_s=60)  # denied/rate-limited rows logged per key/IP
        self.mcp_export_limiter = RateLimiter(limit=20, window_s=10 * 60)  # MCP log downloads per company
        # OAuth (app/oauth.py). Claude makes its registration, token and refresh calls from Anthropic's servers, a
        # few addresses shared by every customer, so the per-IP ceilings are high (keyed by auth.ip_key, which a
        # caller can't point at someone else's address); the global cap, the per-app limit and the size limits do
        # the real bounding. Discovery is never limited (a 429 there makes MCP clients give up).
        self.oauth_register_limiter = RateLimiter(limit=600, window_s=60 * 60)         # app registrations per IP
        self.oauth_register_global_limiter = RateLimiter(limit=2000, window_s=60 * 60)  # all registrations
        self.oauth_ip_limiter = RateLimiter(limit=1200, window_s=60)      # authorize/token/revoke calls per IP
        self.oauth_client_limiter = RateLimiter(limit=60, window_s=60)    # successful token calls per app
        self.oauth_purged_at = float("-inf")                              # last cleanup of expired OAuth rows
        self.mcp_audit_pending = 0                                       # audit rows waiting for the database
        self.mcp_audit_dropped = 0
        self.mcp_tool_index = None
        self.local_login_token: str | None = None                        # one-time link printed by app.local
        self.local_user_id: int | None = None
        self.tool_cache: tuple[float, list] | None = None
        self.anon_audit_writes = 0
        self.integration_test_limiter = RateLimiter(limit=30, window_s=10 * 60)   # test sends/checks per tenant
        # One-time links by email (app/auth_links.py, app/mailer.py)
        self.forgot_ip_limiter = RateLimiter(limit=10, window_s=15 * 60)        # password reset requests per IP
        self.forgot_global_limiter = RateLimiter(limit=300, window_s=60 * 60)   # all of them (IPs can be spoofed)
        self.forgot_email_limiter = RateLimiter(limit=3, window_s=15 * 60)      # emails per address: over it, none
        self.link_limiter = RateLimiter(limit=60, window_s=10 * 60)             # link checks and uses per IP
        self.mail_tenant_limiter = RateLimiter(limit=50, window_s=60 * 60)      # invite emails a company sends
        # People a company adds (POST /api/users): each holds an email address until they join or are removed
        self.people_hour_limiter = RateLimiter(limit=100, window_s=60 * 60)     # any company, an hour
        self.trial_people_day_limiter = RateLimiter(limit=60, window_s=24 * 3600)   # a company on its free trial
        self.mail_user_limiter = RateLimiter(limit=5, window_s=60 * 60)         # confirm emails per person
        # Ask TradeComply (app/assistant.py): the SDK client (made on first use; tests set assistant_client), the
        # person whose answer is being written now -> (chat id, cancel scope for Stop), and the last retention purge.
        # In memory, like the limiters: one instance.
        self.assistant_client = None
        self.assistant_sdk: tuple[str, object] | None = None
        self.assistant_running: dict[tuple[str, int], tuple[int, object]] = {}
        self.assistant_purged_at = float("-inf")
        # Integrations: operator env (tests replace it with a dict), and the outbound HTTP hooks tests swap for an
        # httpx.MockTransport and a fake resolver (see tests/support.py). Production: None = the guarded transport
        # and system DNS.
        self.env = os.environ
        self.http_transport = None
        self.http_resolver = None
        self.http_allow_localhost = False
        self.mailer = Mailer(self)                       # reads self.env when it sends (tests replace env)
        self.events = events.EventBus(self, background=settings.events_background)
        events.set_default(self.events)

    def http(self, **overrides):
        """A SafeHTTP client for outbound calls (connectors, webhooks). Use as a context manager."""
        from .safe_http import SafeHTTP
        kw = {"transport": self.http_transport, "resolver": self.http_resolver,
              "allow_localhost": self.http_allow_localhost}
        kw.update(overrides)
        return SafeHTTP(**kw)
