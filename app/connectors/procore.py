"""Procore: per-tenant OAuth 2.0 sign-in, a company picker, import of projects and the company vendor directory into
TradeComply, and a CSLB license compliance report on the imported vendors. Read-only toward Procore.

Sources (Procore's official developer documentation: developers.procore.com is built from the
github.com/procore/documentation repository; the pages below were read on 2026-11-26 through Context7's index of that
repository, because developers.procore.com itself is not reachable from the build environment):

- oauth/oauth_auth_grant_flow.md and oauth/oauth_endpoints.md: GET /oauth/authorize with client_id,
  response_type=code, redirect_uri, state (no scope parameter, no PKCE parameters documented); the code is single use
  and expires in 10 minutes; POST /oauth/token form fields grant_type, client_id, client_secret, code, redirect_uri,
  refresh_token (client credentials go in the body, so client_auth="body"); response {access_token,
  token_type: "bearer", expires_in (5400 in the example), refresh_token, created_at}; "The response includes a new
  access_token and a new refresh_token. The previous refresh token is invalidated as soon as it's used";
  POST /oauth/revoke with token (the access token), client_id, client_secret "revokes both the Access Token and
  Refresh Token" and answers 200 even for an invalid token.
- api_essentials/troubleshooting.md: refresh tokens are single use, even when the response is lost, so the new pair
  must be saved atomically (the base class's compare-and-set refresh does that).
- oauth/oauth_keys.md: redirect URIs must be registered for the app; dynamic URIs are not supported.
- platform_concepts/development_environments.md: production login https://login.procore.com, API
  https://api.procore.com; Developer Sandbox login https://login-sandbox.procore.com, API https://sandbox.procore.com,
  with the sandbox OAuth credentials. Tokens are not shared between environments.
- tutorials/tutorial_mpz.md and api_essentials/integration_health.md: every company-scoped call carries the
  Procore-Company-Id header (even when company_id is also a query parameter); GET /rest/v1.0/me and
  GET /rest/v1.0/companies don't need it with the authorization code grant. Without it Procore answers
  "Missing Procore-Company-Id header!".
- oauth/oauth_client_credentials.md: GET /rest/v1.0/projects?company_id=... and the project fields (id, name,
  display_name, project_number, address, city, state_code, zip, county, stage, active, company{id, name}).
- erp_integration/erp_events_dictionary.md (create_vendor payload) and tutorials/filtering_on_list_actions.md:
  the vendor object (id, name, license_number, trade_name, is_active, email_address, business_phone, state_code,
  prequalified, ...) and GET /rest/v1.0/vendors?company_id=... (List Company Vendors).
- plan_your_app/pagination.md: page and per_page parameters; Link (first/prev/next/last), Total and Per-Page
  headers; keep per_page at or below 2,000.
- plan_your_app/rate_limiting.md and api_essentials/error_reference.md: an hourly and a spike limit; headers
  X-Rate-Limit-Limit, X-Rate-Limit-Remaining, X-Rate-Limit-Reset (Unix seconds); "When X-Rate-Limit-Remaining
  reaches 0, pause requests until after X-Rate-Limit-Reset"; on 429 read X-Rate-Limit-Reset (and Retry-After), pause
  until then and retry with backoff and jitter; never assume a fixed limit.
- building_applications/building_data_connection_apps.md: the app needs the "User Level Authentication" data
  connector component (OAuth 2.0 authorization code flow on behalf of a user).

Operator setup: create an app in the Procore Developer Portal, add the User Level Authentication component, register
the redirect URI <APP_URL>/api/integrations/procore/oauth/callback (set APP_URL so it is stable), then set
PROCORE_CLIENT_ID and PROCORE_CLIENT_SECRET, and PROCORE_ENV=sandbox when those are the sandbox credentials
(default production). The Monthly and On-Demand sandboxes are not offered.

Why no write back to Procore: the documented way to put a file in a project's documents is several calls (POST a
unified upload, PUT to the presigned URL, PATCH to complete, poll with GET until available, then associate the upload
with a Documents or Document Management record), and the association call for the Documents tool was not confirmed.
That is not "straightforward", so the compliance report stays in TradeComply as a document.

HTTP API (`router`, mount it next to app.integrations_api.router): GET /api/integrations/procore/companies,
POST /api/integrations/procore/company, POST /api/integrations/procore/sync, GET /api/integrations/procore/report.
The generic /api/integrations/procore/... routes (config, test, oauth/start, oauth/callback, disconnect) work as for
every connector; test() also returns the company list in details["companies"].
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import replace
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import (OAUTH_SECRET_KEY, ConfigError, ConfigField, Connector, ConnectorContext, ConnectorError, NotConfigured,
               NotConnected, OAuth2Spec, TestResult)
from ..auth import Principal, RateLimiter, office_up, platform_of
from ..billing import require_entitled
from ..safe_http import SafeHTTPError, UpstreamError, retry_after_seconds
from ..store import now_iso

log = logging.getLogger("tradecomply.connectors.procore")

ENVIRONMENTS = {      # development_environments.md
    "production": {"login": "https://login.procore.com", "api": "https://api.procore.com"},
    "sandbox": {"login": "https://login-sandbox.procore.com", "api": "https://sandbox.procore.com"},
}
DEFAULT_ENV = "production"
PER_PAGE = 100                  # pagination.md: "keep per_page at or below 2,000"; no per-endpoint maximum is stated
MAX_PAGES = 500                 # 50,000 records per list: a runaway guard, not a Procore limit
MAX_RATE_WAIT_S = 30.0          # longer pauses are handed back to the caller (HTTP 429 + Retry-After), not slept
MAX_429_RETRIES = 3
MAX_5XX_RETRIES = 2
FORCE_REFRESH = 10 ** 9         # access_token(leeway=FORCE_REFRESH) refreshes even an unexpired token (after a 401)
MAX_LOCAL_ROWS = 100_000
MAX_ALERTS = 50
RETRY_STATUSES = (500, 502, 503, 504)

# Indirections so tests can run the rate-limit pauses instantly.
_sleep = time.sleep
_now = time.time


class ProcoreRateLimited(ConnectorError):
    """Procore's rate limit is used up for longer than this request should wait. Nothing was imported."""
    status_code = 429

    def __init__(self, wait_s: float):
        self.retry_after = max(1, int(math.ceil(wait_s)))
        when = time.strftime("%H:%M UTC", time.gmtime(_now() + self.retry_after))
        super().__init__(f"Procore's API limit for this app is used up until about {when}. Nothing was changed in "
                         "TradeComply; try again after that.")


class SyncBusy(ConnectorError):
    status_code = 409


# ---------------------------------------------------------------------------- small helpers
def environment_name(env) -> str | None:
    """'production' or 'sandbox' from PROCORE_ENV (unset = production); None for any other value."""
    value = (env.get("PROCORE_ENV") or "").strip().lower() or DEFAULT_ENV
    return value if value in ENVIRONMENTS else None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _text(value, limit: int) -> str | None:
    """Procore text as stored in TradeComply (names, addresses, phone numbers: one line): a string with control
    characters and runs of whitespace turned into single spaces, trimmed, length-capped."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = _SPACE.sub(" ", str(value)).strip()
    return text[:limit].strip() or None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CLASS_CODE = re.compile(r"(?i)(?<![A-Z0-9])[A-D]\s*-?\s*\d{1,2}(?![0-9])")
_DIGIT_RUN = re.compile(r"\d+")


def cslb_number(text) -> str | None:
    """The CSLB license number in Procore's free-text license_number field, or None when there isn't exactly one.

    Accepts '1012345', 'CSLB #1012345', 'C-10 123456', '123456 (C-10, C-7)', 'Lic 123456 exp 06/30/2027'. Class codes
    (A, B-2, C-10, D-49...) are removed first; the number is the only run of 5-8 digits left. Anything else (no
    number, two numbers) returns None and the vendor is reported as 'license number not recognized'."""
    if text is None:
        return None
    rest = _CLASS_CODE.sub(" ", str(text))
    runs = [r for r in _DIGIT_RUN.findall(rest) if 5 <= len(r) <= 8]
    return runs[0] if len(runs) == 1 else None


def _next_page(resp: httpx.Response, page: int) -> int | None:
    """The page number of rel="next" in the Link header (pagination.md), or None when there is no next page. The
    number is taken from the link and the request is rebuilt with our own parameters, so a link that leaves out a
    filter (the docs' example link drops project_id) can't widen the query, and the token never follows a URL that
    came from a response."""
    nxt = (resp.links or {}).get("next") or {}
    url = nxt.get("url")
    if not url:
        return None
    try:
        n = _int(httpx.URL(url).params.get("page"))
    except (httpx.InvalidURL, ValueError, TypeError):
        n = None
    return n if n is not None and n > page else page + 1


_sync_locks: dict[str, threading.Lock] = {}
_sync_guard = threading.Lock()


def _sync_lock(tenant_id: str) -> threading.Lock:
    with _sync_guard:
        return _sync_locks.setdefault(tenant_id, threading.Lock())


# ---------------------------------------------------------------------------- the Procore REST client
class ProcoreAPI:
    """Read-only calls to one Procore environment for one tenant's connection: bearer token (refreshed by the
    connector), the Procore-Company-Id header on company-scoped calls, pagination, and the documented rate-limit
    behaviour. Never logs a token or a response body."""

    def __init__(self, connector: "ProcoreConnector", http, token: str | None = None):
        self.connector, self.http, self.token = connector, http, token
        self.base = connector.api_base()
        self.pause_until = 0.0
        self.requests = 0
        self.rate: dict[str, int] = {}

    def _headers(self, company_id) -> dict:
        if self.token is None:
            self.token = self.connector.access_token()
        headers = {"authorization": f"Bearer {self.token}", "accept": "application/json"}
        if company_id is not None:
            headers["Procore-Company-Id"] = str(company_id)
        return headers

    def _pace(self) -> None:
        """rate_limiting.md: once X-Rate-Limit-Remaining reaches 0, pause until after X-Rate-Limit-Reset."""
        wait = self.pause_until - _now()
        if wait <= 0:
            return
        if wait > MAX_RATE_WAIT_S:
            raise ProcoreRateLimited(wait)
        _sleep(wait + random.uniform(0.1, 1.0))
        self.pause_until = 0.0

    def _note_limits(self, resp: httpx.Response) -> None:
        values = {k: _int(resp.headers.get(f"x-rate-limit-{k}")) for k in ("limit", "remaining", "reset")}
        if values["remaining"] is None:
            return
        self.rate = {k: v for k, v in values.items() if v is not None}
        if values["remaining"] <= 0 and values["reset"]:
            self.pause_until = max(self.pause_until, float(values["reset"]))

    def _wait_429(self, resp: httpx.Response, attempt: int) -> float:
        reset = _int(resp.headers.get("x-rate-limit-reset"))
        waits = [w for w in (retry_after_seconds(resp), (reset - _now()) if reset else None) if w is not None]
        wait = max(waits) if waits else 0.0
        return wait if wait > 0 else min(MAX_RATE_WAIT_S, 2.0 ** attempt)      # no hint: 1 s, 2 s, 4 s

    def get(self, path: str, params: dict | None = None, *, company_id=None) -> httpx.Response:
        url = self.base + path
        refreshed, n429, n5xx = False, 0, 0
        while True:
            self._pace()
            try:
                resp = self.http.get(url, params=params, headers=self._headers(company_id), retries=0,
                                     follow_redirects=False)
            except UpstreamError as e:
                if n5xx < MAX_5XX_RETRIES:
                    _sleep(0.5 * 2 ** n5xx * random.uniform(0.75, 1.25))
                    n5xx += 1
                    continue
                raise ConnectorError(f"Could not reach Procore: {e}") from None
            self.requests += 1
            self._note_limits(resp)
            if resp.status_code == 429:
                wait = self._wait_429(resp, n429)
                log.warning("procore: rate limited (429) on %s, waiting %.0f s (attempt %d)", path, wait, n429 + 1)
                if wait > MAX_RATE_WAIT_S or n429 >= MAX_429_RETRIES:
                    raise ProcoreRateLimited(wait)
                _sleep(wait + random.uniform(0.1, 1.0))
                self.pause_until = 0.0
                n429 += 1
                continue
            if resp.status_code in RETRY_STATUSES and n5xx < MAX_5XX_RETRIES:
                _sleep(0.5 * 2 ** n5xx * random.uniform(0.75, 1.25))
                n5xx += 1
                continue
            if resp.status_code == 401:
                if not refreshed:            # revoked or expired early: refresh once (spends the refresh token)
                    refreshed = True
                    self.token = self.connector.access_token(leeway=FORCE_REFRESH)
                    continue
                self.connector.lost_access()
                raise NotConnected("Procore no longer accepts this connection. Reconnect Procore to continue.")
            return resp

    def get_json(self, path: str, params: dict | None = None, *, company_id=None, what: str):
        resp = self.get(path, params, company_id=company_id)
        self.check(resp, what)
        try:
            return resp, resp.json()
        except ValueError:
            raise ConnectorError(f"Procore sent {what} in a form TradeComply couldn't read.") from None

    @staticmethod
    def check(resp: httpx.Response, what: str) -> None:
        status = resp.status_code
        if status == 200:
            return
        if status == 403:
            raise ConnectorError(f"Procore refused access to {what} (HTTP 403). The Procore user who connected needs "
                                 "permission for it in this company, and a company admin may need to install the "
                                 "TradeComply app (Company Admin > App Management).")
        if status == 404:
            raise ConnectorError(f"Procore could not find {what} (HTTP 404). Check the selected company.")
        if status == 400 and b"procore-company-id" in resp.content[:2000].lower():
            raise ConnectorError(f"Procore refused the request for {what}: the company id header was missing.")
        raise ConnectorError(f"Procore answered HTTP {status} for {what}.")

    def list(self, path: str, params: dict | None = None, *, company_id=None, what: str) -> list[dict]:
        """Every page of a list endpoint (pagination.md): page/per_page, follow rel="next", bounded by Total."""
        items: list[dict] = []
        page = 1
        for _ in range(MAX_PAGES):
            resp, data = self.get_json(path, {**(params or {}), "page": page, "per_page": PER_PAGE},
                                       company_id=company_id, what=what)
            if not isinstance(data, list):
                raise ConnectorError(f"Procore sent {what} in a form TradeComply couldn't read.")
            items.extend(x for x in data if isinstance(x, dict))
            total = _int(resp.headers.get("total"))
            nxt = _next_page(resp, page)
            if nxt is None and total is not None and len(items) < total and data:
                nxt = page + 1                   # Total says there is more but no Link header was sent
            if nxt is None or not data or (total is not None and len(items) >= total):
                return items
            page = nxt
        raise ConnectorError(f"Procore returned more than {MAX_PAGES} pages of {what}; stopped.")


# ---------------------------------------------------------------------------- the connector
class ProcoreConnector(Connector):
    provider = "procore"
    display_name = "Procore"
    catalog_id = "procore"
    description = ("Sign in with Procore to import your projects and company directory, then check every vendor's "
                   "California contractor license (CSLB) and get a compliance report. TradeComply only reads from "
                   "Procore.")
    category = "project_management"
    docs_url = "https://github.com/procore/documentation/blob/main/oauth/oauth_auth_grant_flow.md"
    required_env = ("PROCORE_CLIENT_ID", "PROCORE_CLIENT_SECRET")
    oauth = OAuth2Spec(
        authorize_url=ENVIRONMENTS[DEFAULT_ENV]["login"] + "/oauth/authorize",
        token_url=ENVIRONMENTS[DEFAULT_ENV]["login"] + "/oauth/token",
        client_id_env="PROCORE_CLIENT_ID", client_secret_env="PROCORE_CLIENT_SECRET",
        scopes=(), pkce=False, client_auth="body",
        revoke_url=ENVIRONMENTS[DEFAULT_ENV]["login"] + "/oauth/revoke")
    config_fields = (
        ConfigField("company_id", "Procore company ID", pattern=r"[0-9]{1,15}", max_length=15,
                    help="The Procore company to import from. Set for you when your Procore login has one company; "
                         "otherwise pick one from the company list."),
        ConfigField("california_only", "Only import California projects", type="boolean", default=True,
                    help="Skip Procore projects whose state is not CA."),
        ConfigField("vendor_scope", "Vendors to import", type="select", default="licensed",
                    options=(("licensed", "Vendors with a license number"), ("all", "All active vendors"))),
        ConfigField("alert_on_problems", "Alert on license problems", type="boolean", default=True,
                    help="Send a license_problem_found event (to your webhooks and alerts) for each vendor the check "
                         "says not to use."),
    )
    INTERNAL_CONFIG = ("company_name", "last_sync")      # written by the connector; kept across settings saves
    unconfirmed = (
        "Procore describes `state` as an alphanumeric value; TradeComply's signed state also contains '.', '-' and "
        "'_'. Not confirmed that Procore returns every such value unchanged.",
        "List Companies: only the `id` and `name` fields are used; the reference page's field list was not read.",
        "List Company Vendors: the fields used (id, name, license_number, trade_name, is_active, email_address, "
        "business_phone, state_code) come from the vendor object in Procore's ERP events dictionary; the List "
        "Company Vendors reference page itself was not read.",
        "No per-endpoint per_page maximum is documented for these v1.0 list endpoints; 100 is used.",
        "The HTTP status of the 'Missing Procore-Company-Id header!' error is not documented (the header is always "
        "sent).",
        "oauth_endpoints.md lists redirect_uri as required on every /oauth/token call, the troubleshooting page "
        "leaves it out of the refresh example; it is sent on refresh too.",
        "The error code for a spent or invalid refresh token is assumed to be RFC 6749 `invalid_grant`.",
        "Whether a company admin must install the app (App Management) before a user-level sign-in can read that "
        "company's data.",
        "Vendor insurance (COI) records are not imported: the Company Vendor Insurances endpoint path was not "
        "confirmed. Enter COI expiry dates on the sub in TradeComply.",
    )

    def __init__(self, ctx):
        super().__init__(ctx)
        urls = ENVIRONMENTS[environment_name(ctx.env) or DEFAULT_ENV]
        self.oauth = replace(type(self).oauth, authorize_url=urls["login"] + "/oauth/authorize",
                             token_url=urls["login"] + "/oauth/token", revoke_url=urls["login"] + "/oauth/revoke")

    # ------------------------------------------------------------------ operator configuration
    @classmethod
    def missing_env(cls, env=None) -> list[str]:
        env = os.environ if env is None else env
        missing = super().missing_env(env)
        if environment_name(env) is None:
            missing.append("PROCORE_ENV")
        return missing

    @classmethod
    def describe(cls, env=None) -> dict:
        env = os.environ if env is None else env
        return {**super().describe(env), "procore_environment": environment_name(env)}

    def require_operator(self) -> None:
        if environment_name(self.ctx.env) is None:
            raise NotConfigured("Procore isn't set up on this server yet (PROCORE_ENV must be 'production' or "
                                "'sandbox').")
        super().require_operator()

    def environment(self) -> str:
        return environment_name(self.ctx.env) or DEFAULT_ENV

    def api_base(self) -> str:
        return ENVIRONMENTS[self.environment()]["api"]

    @classmethod
    def clean_config(cls, values, existing_config=None, existing_secrets=None):
        config, secret = super().clean_config(values, existing_config, existing_secrets)
        for key in cls.INTERNAL_CONFIG:
            if key in (existing_config or {}):
                config[key] = existing_config[key]
        if "company_id" in values and (existing_config or {}).get("company_id") != config.get("company_id"):
            config.pop("company_name", None)          # the name belonged to the previous company
        return config, secret

    def _update_config(self, **changes) -> None:
        config = {**self.config(), **changes}
        self.save(config={k: v for k, v in config.items() if v is not None})

    # ------------------------------------------------------------------ OAuth specifics
    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        tokens = super().exchange_code(code, redirect_uri, None)
        tokens["redirect_uri"] = redirect_uri          # sent again on every refresh (oauth_endpoints.md)
        return tokens

    def refresh(self, refresh_token: str) -> dict:
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        redirect_uri = (self.secrets().get(OAUTH_SECRET_KEY) or {}).get("redirect_uri")
        if redirect_uri:
            form["redirect_uri"] = redirect_uri
        return self._token_request(form)

    def revoke(self) -> None:
        """POST /oauth/revoke with the access token, which revokes the access and the refresh token. A valid access
        token is fetched first (refreshing if needed) so both really are revoked."""
        tok = self.secrets().get(OAUTH_SECRET_KEY) or {}
        try:
            token = self.access_token()
        except (ConnectorError, SafeHTTPError):
            token = tok.get("access_token")
        if not token:
            return
        form = {"token": token}
        headers = self._client_auth_headers(form)
        with self.ctx.http() as http:
            r = http.post(self.oauth.revoke_url, data=form, headers=headers, retries=0, follow_redirects=False)
        log.info("procore: revoke answered %s", r.status_code)

    def lost_access(self) -> None:
        self.save(status="error", last_error="Procore no longer accepts this connection. Reconnect to continue.")

    def on_connected(self, tokens: dict) -> str | None:
        """After sign-in: list the user's companies and select the company when there is only one."""
        with self.ctx.http() as http:
            companies = self.companies(ProcoreAPI(self, http, token=tokens["access_token"]))
        if len(companies) == 1:
            self._update_config(company_id=str(companies[0]["id"]), company_name=companies[0]["name"])
            return companies[0]["name"]
        current = _int(self.config().get("company_id"))
        match = next((c for c in companies if c["id"] == current), None)
        if match:
            self._update_config(company_name=match["name"])
            return match["name"]
        return f"{len(companies)} Procore companies: choose one" if companies else "No Procore companies found"

    # ------------------------------------------------------------------ reads
    def companies(self, api: ProcoreAPI | None = None) -> list[dict]:
        """The companies the connected Procore user can see: [{"id", "name", "active"}]."""
        if api is None:
            with self.ctx.http() as http:
                return self.companies(ProcoreAPI(self, http))
        out, seen = [], set()
        for c in api.list("/rest/v1.0/companies", what="your Procore companies"):
            cid = _int(c.get("id"))
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "name": _text(c.get("name"), 200) or f"Company {cid}",
                        "active": c.get("is_active") if isinstance(c.get("is_active"), bool) else None})
        return sorted(out, key=lambda c: (c["name"].lower(), c["id"]))

    def selected_company_id(self) -> int | None:
        return _int(self.config().get("company_id"))

    def select_company(self, company_id) -> dict:
        """Choose the company to import from; it must be one the connected user can see."""
        cid = _int(company_id)
        if cid is None or cid <= 0:
            raise ConfigError("Choose a company from the list", "company_id")
        match = next((c for c in self.companies() if c["id"] == cid), None)
        if not match:
            raise ConfigError("Your Procore login can't see that company. Choose one from the list.", "company_id")
        self._update_config(company_id=str(cid), company_name=match["name"])
        self.save(external_account=match["name"][:300])
        return match

    def test(self) -> TestResult:
        companies = self.companies()
        cid = self.selected_company_id()
        details = {"companies": companies, "company_id": cid, "environment": self.environment()}
        if not companies:
            return TestResult(False, "Connected, but your Procore login doesn't belong to any company.",
                              details=details)
        if cid is None:
            return TestResult(True, f"Connected. Choose which of your {len(companies)} Procore companies to import "
                                    "from.", details=details)
        match = next((c for c in companies if c["id"] == cid), None)
        if not match:
            return TestResult(False, f"Your Procore login can't see company {cid} any more. Choose another company.",
                              details=details)
        return TestResult(True, f"Connected to {match['name']}.", account=match["name"], details=details)

    # ------------------------------------------------------------------ import + report
    def sync(self, *, projects: bool = True, vendors: bool = True, user_label: str | None = None) -> dict:
        """Import the selected company's projects and vendor directory and check every imported vendor's license.
        Everything is read from Procore first and written afterwards, so a rate limit or an error part way leaves
        TradeComply unchanged. Running it again updates what it imported before (projects by Procore id, vendors by
        CSLB license number, then by name), never duplicating."""
        self.require_operator()
        conn = self.connection()
        if not conn or conn.get("status") not in ("connected", "error"):
            raise NotConnected("Connect Procore first.")
        cid = self.selected_company_id()
        if cid is None:
            raise ConfigError("Choose which Procore company to import from first.", "company_id")
        lock = _sync_lock(self.ctx.tenant_id)
        if not lock.acquire(blocking=False):
            raise SyncBusy("A Procore import is already running for this account. Wait for it to finish.")
        try:
            return self._sync(cid, projects, vendors, user_label)
        finally:
            lock.release()

    def _sync(self, cid: int, want_projects: bool, want_vendors: bool, user_label: str | None) -> dict:
        cfg = self.config()
        started = now_iso()
        with self.ctx.http() as http:
            api = ProcoreAPI(self, http)
            company = next((c for c in self.companies(api) if c["id"] == cid), None)
            if company is None:
                raise ConfigError(f"Your Procore login can't see company {cid} any more. Choose another company.",
                                  "company_id")
            raw_projects = api.list("/rest/v1.0/projects", {"company_id": cid}, company_id=cid,
                                    what="the company's projects") if want_projects else []
            raw_vendors = api.list("/rest/v1.0/vendors", {"company_id": cid}, company_id=cid,
                                   what="the company directory (vendors)") if want_vendors else []
            requests, rate = api.requests, dict(api.rate)
        projects = self._import_projects(cid, raw_projects, cfg) if want_projects else None
        vendors = self._import_vendors(raw_vendors, cfg) if want_vendors else None
        report = self._report(vendors["entries"]) if vendors is not None else None
        doc = self._save_report(company, projects, vendors, report, user_label) if report is not None else None
        alerts = self._alert(cid, report) if report and cfg.get("alert_on_problems", True) is not False else 0
        prev = cfg.get("last_sync") if isinstance(cfg.get("last_sync"), dict) else {}
        if prev.get("company_id") != cid:
            prev = {}
        summary = {"at": started, "company_id": cid, "company_name": company["name"],      # small: config <= 20 KB
                   "projects": {"fetched": projects["fetched"], "created": projects["created"],
                                "updated": projects["updated"], "skipped": projects["skipped_total"], "at": started}
                   if projects is not None else prev.get("projects"),
                   "vendors": {**{k: vendors[k] for k in ("fetched", "created", "updated", "inactive_skipped",
                                                          "without_license")}, "at": started}
                   if vendors is not None else prev.get("vendors"),
                   "counts_by_risk": report["counts_by_risk"] if report else prev.get("counts_by_risk"),
                   "report_document_id": doc["id"] if doc else prev.get("report_document_id"),
                   "alerts": alerts, "procore_requests": requests}
        self._update_config(company_name=company["name"], last_sync=summary)
        self.save(status="connected", last_error=None, external_account=company["name"][:300])
        log.info("procore: tenant %s imported %d project(s), %d vendor(s) in %d request(s)", self.ctx.tenant_id,
                 len(raw_projects), len(raw_vendors), requests)
        return {"company": company, "environment": self.environment(), "synced_at": started,
                "projects": projects, "vendors": None if vendors is None else
                {k: v for k, v in vendors.items() if k != "entries"},
                "report": report, "report_document_id": doc["id"] if doc else None, "alerts_sent": alerts,
                "procore_requests": requests, "procore_rate_limit": rate}

    def _import_projects(self, cid: int, rows: list[dict], cfg: dict) -> dict:
        store, tid = self.ctx.store, self.ctx.tenant_id
        ca_only = cfg.get("california_only", True) is not False
        linked: dict[tuple[str, str], dict] = {}
        for p in store.list("projects", tid, order="id asc", limit=MAX_LOCAL_ROWS):
            link = (p.get("extra") or {}).get("procore") if isinstance(p.get("extra"), dict) else None
            if isinstance(link, dict) and link.get("project_id") is not None:
                linked.setdefault((str(link.get("company_id")), str(link["project_id"])), p)
        created = updated = 0
        skipped, items, seen = [], [], set()
        for raw in rows:
            pid = _int(raw.get("id"))
            name = _text(raw.get("name"), 200) or _text(raw.get("display_name"), 200)
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            state = (_text(raw.get("state_code"), 10) or "").upper()
            if raw.get("active") is False:
                skipped.append({"procore_project_id": pid, "name": name, "reason": "inactive in Procore"})
                continue
            if ca_only and state and state != "CA":
                skipped.append({"procore_project_id": pid, "name": name, "reason": f"not in California ({state})"})
                continue
            link = {"project_id": pid, "company_id": cid, "project_number": _text(raw.get("project_number"), 100),
                    "stage": _text(raw.get("stage"), 100), "state_code": state or None,
                    "zip": _text(raw.get("zip"), 20), "synced": now_iso()}
            fields = {k: v for k, v in {"name": name or f"Procore project {pid}",
                                        "address": _text(raw.get("address"), 2000),
                                        "city": _text(raw.get("city"), 2000),
                                        "county": _text(raw.get("county"), 2000)}.items() if v}
            row = linked.get((str(cid), str(pid)))
            if row:
                def change(current, fields=fields, link=link):
                    extra = dict(current.get("extra") or {}) if isinstance(current.get("extra"), dict) else {}
                    extra["procore"] = {**(extra.get("procore") or {}), **link}
                    return {**fields, "extra": extra}
                saved = store.modify("projects", tid, row["id"], change)
                if saved is None:            # deleted meanwhile: import it again
                    row = None
                else:
                    updated += 1
            if not row:
                saved = store.insert("projects", tid, {**fields, "status": "active", "extra": {"procore": link}})
                linked[(str(cid), str(pid))] = saved
                created += 1
            items.append({"id": saved["id"], "procore_project_id": pid, "name": saved["name"],
                          "created": not row})
        return {"fetched": len(rows), "created": created, "updated": updated, "skipped": skipped[:500],
                "skipped_total": len(skipped), "items": items}

    def _import_vendors(self, rows: list[dict], cfg: dict) -> dict:
        store, tid = self.ctx.store, self.ctx.tenant_id
        import_all = cfg.get("vendor_scope") == "all"
        by_license: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        for s in store.list("subs", tid, order="id asc", limit=MAX_LOCAL_ROWS):
            if s.get("license_number"):
                by_license.setdefault(s["license_number"], s)
            elif s.get("name"):
                by_name.setdefault(s["name"].strip().lower(), s)
        created = updated = inactive = 0
        entries, no_license, seen = [], [], set()
        for raw in rows:
            vid = _int(raw.get("id"))
            if vid is None or vid in seen:
                continue
            seen.add(vid)
            if raw.get("is_active") is False:
                inactive += 1
                continue
            name = _text(raw.get("name"), 200) or f"Procore vendor {vid}"
            given = _text(raw.get("license_number"), 100)
            lic = cslb_number(given)
            vendor = {"id": vid, "name": name, "license_number": given, "trade_name": _text(raw.get("trade_name"), 200),
                      "state_code": _text(raw.get("state_code"), 10), "prequalified": raw.get("prequalified")
                      if isinstance(raw.get("prequalified"), bool) else None}
            if not lic:
                no_license.append({"procore_vendor_id": vid, "name": name,
                                   "reason": "license number not recognized" if given else "no license number"})
                if not import_all:
                    continue
            email = _text(raw.get("email_address"), 254)
            email = email if email and _EMAIL.match(email) else None
            phone = _text(raw.get("business_phone"), 40)
            scope = _text(raw.get("trade_name"), 2000)
            # the same license, else a sub of the same name entered without a license (which gets the number)
            existing = (by_license.get(lic) if lic else None) or by_name.get(name.lower())
            if existing:
                data = {"name": name, **({"email": email} if email else {}), **({"phone": phone} if phone else {})}
                if lic and not existing.get("license_number"):
                    data["license_number"] = lic
                    by_name.pop((existing.get("name") or "").strip().lower(), None)
                if scope and not existing.get("scope"):
                    data["scope"] = scope
                sub = store.update("subs", tid, existing["id"], data)
                if sub is None:              # deleted meanwhile
                    existing = None
                else:
                    updated += 1
            if not existing:
                sub = store.insert("subs", tid, {k: v for k, v in {"name": name, "license_number": lic, "scope": scope,
                                                                   "email": email, "phone": phone}.items()
                                                 if v is not None})
                created += 1
            if lic:
                by_license[lic] = sub
            else:
                by_name[name.lower()] = sub
            entries.append({"sub": sub, "vendor": vendor})
        return {"fetched": len(rows), "created": created, "updated": updated, "inactive_skipped": inactive,
                "without_license": len(no_license), "no_license": no_license[:200], "entries": entries}

    @staticmethod
    def _check_input(sub: dict):
        from tradecomply.licensing import SubInput
        from pydantic import ValidationError
        row = {"license_number": sub.get("license_number") or "", "name": sub.get("name"),
               "scope_of_work": sub.get("scope"), "required_classification": sub.get("required_class"),
               "coi_expiry": sub.get("coi_expiry"), "wc_required": sub.get("wc_required", True)}
        try:
            return SubInput.model_validate(row)
        except ValidationError:          # a hand-entered COI date or class TradeComply can't read: check without it
            row.update(coi_expiry=None, required_classification=None)
            return SubInput.model_validate(row)

    def _report(self, entries: list[dict]) -> dict:
        """bulk_check_subcontractors (tradecomply.licensing, which runs logic.check_subcontractor per row) on the
        imported vendors as they are now stored in TradeComply (so a COI date or required class entered on the sub
        counts), with each row tied back to its TradeComply sub and Procore vendor."""
        from tradecomply.licensing import RISK_ORDER, bulk_check_subcontractors
        if not entries:
            return {"as_of": None, "total": 0, "counts_by_risk": {k: 0 for k in RISK_ORDER}, "rows": [],
                    "top_actions": [], "license_data_official": False, "table_markdown": "", "notes": [],
                    "citations": [], "verified": False, "disclaimer": None}
        res = bulk_check_subcontractors(rows=[self._check_input(e["sub"]) for e in entries]).model_dump()
        rows = []
        for r in res["rows"]:
            e = entries[r["row"] - 1]
            rows.append({**r, "sub_id": e["sub"]["id"], "procore_vendor_id": e["vendor"]["id"],
                         "procore_name": e["vendor"]["name"], "procore_license_number": e["vendor"]["license_number"],
                         "procore_trade_name": e["vendor"]["trade_name"],
                         "procore_prequalified": e["vendor"]["prequalified"]})
        return {"as_of": res["as_of"], "total": res["total"], "counts_by_risk": res["counts_by_risk"], "rows": rows,
                "top_actions": res["top_actions"], "license_data_official": res["license_data_official"],
                "table_markdown": res["table_markdown"], "notes": res["notes"], "citations": res["citations"],
                "verified": res["verified"], "disclaimer": res["disclaimer"]}

    def _save_report(self, company: dict, projects, vendors: dict, report: dict, user_label: str | None) -> dict:
        counts = report["counts_by_risk"]
        lines = [f"# Procore vendor compliance report: {company['name']}", "",
                 f"Procore company {company['id']} ({self.environment()}). Checked {report['as_of'] or now_iso()[:10]} "
                 "against TradeComply's CSLB license data"
                 + (" (official CSLB data)." if report["license_data_official"] else " (not all rows from official "
                                                                                     "CSLB data)."), "",
                 f"**{report['total']} vendor(s) checked:** {counts.get('do_not_use', 0)} do not use, "
                 f"{counts.get('high', 0)} high, {counts.get('medium', 0)} medium, {counts.get('low', 0)} low risk."]
        if projects:
            lines += ["", f"Projects: {projects['created']} added, {projects['updated']} updated, "
                          f"{projects['skipped_total']} skipped."]
        if report["top_actions"]:
            lines += ["", "## Top actions", ""] + [f"- {a}" for a in report["top_actions"]]
        if report["table_markdown"]:
            lines += ["", "## Vendors", "", report["table_markdown"]]
        missing = vendors["no_license"]
        if missing:
            lines += ["", "## Not checked", "",
                      f"{vendors['without_license']} active vendor(s) have no CSLB license number in Procore:"]
            lines += [f"- {m['name']} ({m['reason']})" for m in missing[:50]]
            if vendors["without_license"] > 50:
                lines.append(f"- and {vendors['without_license'] - 50} more")
        notes = list(report["notes"]) + ["Insurance certificates kept in Procore are not imported. Enter COI expiry "
                                         "dates on each sub in TradeComply to have them checked."]
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
        if report["disclaimer"]:
            lines += ["", f"_{report['disclaimer']}_"]
        return self.ctx.store.insert("documents", self.ctx.tenant_id, {
            "title": f"Procore vendor compliance report, {company['name']}"[:300], "kind": "compliance_report",
            "body_markdown": "\n".join(lines) + "\n", "created_by": (user_label or "Procore import")[:200]})

    def _alert(self, cid: int, report: dict) -> int:
        """One license_problem_found event per vendor the check says not to use, once per distinct problem."""
        sent = 0
        for r in report["rows"]:
            if r["risk"] != "do_not_use" or sent >= MAX_ALERTS:
                continue
            sig = hashlib.sha256("|".join(r["problems"]).encode()).hexdigest()[:16]
            payload = {"title": f"{r['procore_name']}: do not use"[:200],
                       "summary": ("; ".join(r["problems"]) + f". {r['action']}")[:2000], "severity": "critical",
                       "link": "/#/subs", "sub_id": r["sub_id"], "license_number": r["license_number"] or None,
                       "source": "procore", "procore_company_id": cid, "procore_vendor_id": r["procore_vendor_id"]}
            try:
                ids = self.ctx.platform.events.emit(self.ctx.tenant_id, "license_problem_found", payload,
                                                    dedupe_key=f"procore:{cid}:{r['procore_vendor_id']}:{sig}")
            except ValueError:
                log.exception("procore: bad license_problem_found payload")
                continue
            sent += 1 if ids else 0
        return sent

    def last_report(self) -> dict:
        cfg = self.config()
        last = cfg.get("last_sync") if isinstance(cfg.get("last_sync"), dict) else None
        doc = None
        if last and last.get("report_document_id"):
            doc = self.ctx.store.get("documents", self.ctx.tenant_id, last["report_document_id"])
        return {"last_sync": last, "document": doc}


# ---------------------------------------------------------------------------- HTTP routes
router = APIRouter(prefix="/api/integrations/procore", tags=["integrations"])
_sync_limiter = RateLimiter(limit=10, window_s=60 * 60)       # imports per tenant per hour


class CompanyIn(BaseModel):
    company_id: int = Field(gt=0, lt=10 ** 15)


class SyncIn(BaseModel):
    projects: bool = True
    vendors: bool = True


def _instance(request: Request, p: Principal) -> ProcoreConnector:
    return ProcoreConnector(ConnectorContext(platform_of(request), p.tenant_id, p.user_id, p.label))


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, ProcoreRateLimited):
        return HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after)})
    if isinstance(e, ConnectorError):
        return HTTPException(e.status_code, str(e))
    if isinstance(e, SafeHTTPError):
        return HTTPException(502, str(e))
    log.exception("procore: unexpected failure")
    return HTTPException(500, "The Procore integration failed unexpectedly")


def _audit(request: Request, p: Principal, action: str, args: dict, ok: bool = True) -> None:
    try:
        platform_of(request).store.audit(p.tenant_id, p.label, f"integrations.procore_{action}", args, None, ok=ok,
                                         source="integrations")
    except Exception:      # noqa: BLE001 - the action already happened
        log.exception("procore: audit write failed")


@router.get("/companies")
def list_companies(request: Request, p: Principal = Depends(office_up)):
    """The Procore companies the connected user can see (the company picker), and the selected one."""
    require_entitled(platform_of(request), p)
    inst = _instance(request, p)
    try:
        inst.require_operator()
        companies = inst.companies()
    except Exception as e:      # noqa: BLE001 - mapped to a plain message
        raise _fail(e)
    return {"companies": companies, "company_id": inst.selected_company_id()}


@router.post("/company")
def choose_company(body: CompanyIn, request: Request, p: Principal = Depends(office_up)):
    """Select the company to import from (one of /companies)."""
    require_entitled(platform_of(request), p)
    inst = _instance(request, p)
    try:
        inst.require_operator()
        company = inst.select_company(body.company_id)
    except Exception as e:      # noqa: BLE001
        raise _fail(e)
    _audit(request, p, "company", {"company_id": company["id"]})
    return {"company": company, "connection": inst.status()}


@router.post("/sync")
def sync(request: Request, body: Optional[SyncIn] = None, p: Principal = Depends(office_up)):
    """Import projects and vendors from the selected Procore company and return the license compliance report."""
    platform = platform_of(request)
    require_entitled(platform, p)
    body = body or SyncIn()
    key = f"tenant|{p.tenant_id}"
    wait = _sync_limiter.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many Procore imports in an hour. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    inst = _instance(request, p)
    try:
        inst.require_operator()
        _sync_limiter.hit(key)
        result = inst.sync(projects=body.projects, vendors=body.vendors, user_label=p.label)
    except Exception as e:      # noqa: BLE001
        _audit(request, p, "sync", {"projects": body.projects, "vendors": body.vendors}, ok=False)
        raise _fail(e)
    _audit(request, p, "sync", {"company_id": result["company"]["id"],
                                "report_document_id": result["report_document_id"]})
    return result


@router.get("/report")
def report(request: Request, p: Principal = Depends(office_up)):
    """The last import's summary and its saved compliance report document (no call to Procore)."""
    return _instance(request, p).last_report()
