"""Procore connector (app/connectors/procore.py): operator configuration, OAuth sign-in with body client
authentication and rotating single-use refresh tokens, the company picker, project and vendor-directory import
(idempotent), the CSLB compliance report, pagination, rate limits, tenant isolation, revocation and log hygiene.

FakeProcore answers in the shapes of Procore's official docs (github.com/procore/documentation): /oauth/token and
/oauth/revoke (oauth/oauth_endpoints.md, oauth/oauth_auth_grant_flow.md), List Projects (oauth/
oauth_client_credentials.md), the vendor object (erp_integration/erp_events_dictionary.md), Link/Total/Per-Page
pagination headers (plan_your_app/pagination.md) and X-Rate-Limit-* headers (plan_your_app/rate_limiting.md). Every
credential here is made up."""
import json
import logging
import math
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import connectors as registry
from app.connectors import ConnectorContext, ConnectorError, NotConnected, validate_connector_class
from app.connectors import procore
from app.connectors.procore import ProcoreAPI, ProcoreConnector, cslb_number
from app.crypto import DecryptError, connection_context
from tests.support import backend, json_response, join, make_app, mock_http, signup  # noqa: F401

CLIENT_ID = "test-procore-client-id"
CLIENT_SECRET = "test-procore-client-secret"
ENV = {"PROCORE_CLIENT_ID": CLIENT_ID, "PROCORE_CLIENT_SECRET": CLIENT_SECRET}
HOSTS = {"production": ("login.procore.com", "api.procore.com"),
         "sandbox": ("login-sandbox.procore.com", "sandbox.procore.com")}


def project(pid, name, state="CA", county="Santa Barbara", active=True, number=None):
    """A project in the List Projects response shape (oauth_client_credentials.md)."""
    return {"id": pid, "name": name, "display_name": name, "project_number": number, "address": "5678 First Street",
            "city": "Anytown", "state_code": state, "country_code": "US", "zip": "93013", "county": county,
            "latitude": 34.385045633646, "longitude": -119.490841957738, "stage": "Course of Construction",
            "phone": "", "created_at": "2016-08-22T20:18:55Z", "updated_at": "2017-10-23T21:19:34Z",
            "active": active, "origin_id": None, "origin_data": None, "company": {"id": 7656, "name": "Acme"}}


def vendor(vid, name, license_number, trade="", active=True, email=None):
    """A vendor in the shape of the vendor object in erp_events_dictionary.md (create_vendor)."""
    return {"id": vid, "name": name, "abbreviated_name": None, "address": "", "authorized_bidder": True,
            "business_phone": "805-555-0100", "city": "", "company": name, "country_code": "US",
            "created_at": "2020-07-10T19:41:59Z", "email_address": email, "fax_number": "", "is_active": active,
            "labor_union": None, "license_number": license_number, "logo": None, "mobile_phone": None,
            "non_union_prevailing_wage": False, "notes": None, "origin_code": None, "origin_data": None,
            "origin_id": None, "prequalified": False, "state_code": "CA", "synced_to_erp": False,
            "trade_name": trade, "union_member": False, "updated_at": "2020-07-10T19:42:17Z", "website": None,
            "zip": "", "business_register": None, "primary_contact": None, "attachments": [], "vendor_group": None}


class FakeProcore:
    """login.procore.com + api.procore.com (or the sandbox pair). Users sign in with an authorization code; each
    token pair belongs to one user, who can see a set of companies."""

    def __init__(self, env="production"):
        self.login_host, self.api_host = HOSTS[env]
        self.codes = {"code-u1": "u1", "code-u2": "u2", "code-multi": "multi"}
        self.user_companies = {"u1": [7656], "u2": [8800], "multi": [7656, 8800]}
        self.companies = {7656: "Acme Builders Inc", 8800: "Coastal Constructors LLC"}
        self.projects = {7656: [project(101, "Harbor Lofts"), project(102, "Mesa Clinic", number="MC-2"),
                                project(103, "Reno Depot", state="NV"), project(104, "Old Job", active=False)],
                         8800: [project(201, "Coastal Office", county=None)]}
        self.vendors = {7656: [vendor(501, "Example Plumbing Inc.", "123456", "Plumbing", email="pm@plumb.test"),
                               vendor(502, "Lapsed Builders LLC", "CSLB #999999", "General Building"),
                               vendor(503, "Solo Roofing", "C-39 777000", "Roofing"),
                               vendor(504, "Lumber Supply Co", None),
                               vendor(505, "Gone Electric", "555000", active=False)],
                        8800: [vendor(601, "Coastal Plumbing", "123456", "Plumbing")]}
        self.access, self.refresh_tokens = {}, {}          # token -> user
        self.pairs = {}                                    # refresh -> access
        self.issued = 0
        self.expires_in = 5400
        self.token_forms, self.revoked, self.api_calls = [], [], []
        self.scripted: list = []          # callables(request) -> Response | None, tried first, used once
        self.remaining = 600
        self.reset = int(time.time()) + 3600
        self.lock = threading.Lock()

    def rate_headers(self):
        self.remaining = max(0, self.remaining - 1)
        return {"X-Rate-Limit-Limit": "600", "X-Rate-Limit-Remaining": str(self.remaining),
                "X-Rate-Limit-Reset": str(self.reset)}

    def issue(self, user):
        self.issued += 1
        at, rt = f"procore-at-{self.issued}", f"procore-rt-{self.issued}"
        self.access[at], self.refresh_tokens[rt], self.pairs[rt] = user, user, at
        return json_response(200, {"access_token": at, "token_type": "bearer", "expires_in": self.expires_in,
                                   "refresh_token": rt, "created_at": int(time.time())})

    def form(self, request):
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        for i, fn in enumerate(self.scripted):
            resp = fn(request)
            if resp is not None:
                del self.scripted[i]
                return resp
        url = request.url
        if url.host == self.login_host and url.path == "/oauth/token":
            form = self.form(request)
            with self.lock:
                self.token_forms.append(form)
                if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
                    return json_response(401, {"error": "invalid_client"})
                if not form.get("redirect_uri"):
                    return json_response(400, {"error": "invalid_request"})
                if form["grant_type"] == "authorization_code":
                    user = self.codes.pop(form.get("code"), None)          # single use
                    return self.issue(user) if user else json_response(400, {"error": "invalid_grant"})
                if form["grant_type"] == "refresh_token":
                    user = self.refresh_tokens.pop(form.get("refresh_token"), None)    # single use
                    if not user:
                        return json_response(400, {"error": "invalid_grant"})
                    self.access.pop(self.pairs.pop(form["refresh_token"], None), None)
                    return self.issue(user)
            return json_response(400, {"error": "unsupported_grant_type"})
        if url.host == self.login_host and url.path == "/oauth/revoke":
            form = self.form(request)
            self.revoked.append(form)
            if form.get("client_id") == CLIENT_ID and form.get("client_secret") == CLIENT_SECRET:
                self.access.pop(form.get("token"), None)
                for rt, at in list(self.pairs.items()):
                    if at == form.get("token"):
                        self.refresh_tokens.pop(rt, None)
            return httpx.Response(200)          # 200 even for an invalid token (oauth_endpoints.md)
        if url.host == self.api_host:
            return self.api(request)
        return json_response(404, {"error": "not found"})

    def api(self, request):
        self.api_calls.append(request)
        auth = request.headers.get("authorization", "")
        user = self.access.get(auth[7:]) if auth.startswith("Bearer ") else None
        if not user:
            return json_response(401, {"errors": "unauthorized"}, self.rate_headers())    # v1.0 body not documented
        path, q = request.url.path, request.url.params
        if path == "/rest/v1.0/companies":
            items = [{"id": c, "name": self.companies[c], "is_active": True} for c in self.user_companies[user]]
            return self.page(request, items)
        if path in ("/rest/v1.0/projects", "/rest/v1.0/vendors"):
            if "procore-company-id" not in request.headers:
                return httpx.Response(400, text="Missing Procore-Company-Id header!", headers=self.rate_headers())
            cid = int(q["company_id"])
            assert int(request.headers["procore-company-id"]) == cid
            if cid not in self.user_companies[user]:
                return json_response(403, {"errors": "forbidden"}, self.rate_headers())
            return self.page(request, (self.projects if path.endswith("projects") else self.vendors)[cid])
        return json_response(404, {"errors": "not found"}, self.rate_headers())

    def page(self, request, items):
        """pagination.md: page/per_page, with Link (first, prev, last, next), Total and Per-Page headers."""
        per, page = int(request.url.params.get("per_page", 100)), int(request.url.params.get("page", 1))
        last = max(1, math.ceil(len(items) / per))
        link = lambda n: str(request.url.copy_set_param("page", n))    # noqa: E731
        rels = [(1, "first")] + ([(page - 1, "prev")] if page > 1 else []) + [(last, "last")] + \
            ([(page + 1, "next")] if page < last else [])
        headers = {"Link": ", ".join(f'<{link(n)}>; rel="{rel}"' for n, rel in rels), "Total": str(len(items)),
                   "Per-Page": str(per), **self.rate_headers()}
        return json_response(200, items[(page - 1) * per: page * per], headers)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(procore, "_sleep", slept.append)
    return slept


def mount_procore_routes(app):
    """What app/main.py needs: include procore.router before the catch-all MCP mount at '/'."""
    app.include_router(procore.router)
    routes = app.router.routes
    catch_all = [r for r in routes if isinstance(r, Mount) and r.path == ""]
    for r in catch_all:
        routes.remove(r)
    routes.extend(catch_all)


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    mount_procore_routes(a)
    a.state.platform.env = dict(ENV)
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def fake(platform):
    f = FakeProcore()
    f.seen = mock_http(platform, f)
    return f


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def start(c) -> dict:
    r = c.post("/api/integrations/procore/oauth/start")
    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["authorize_url"])
    return {"host": url.netloc, "path": url.path, **{k: v[0] for k, v in parse_qs(url.query).items()}}


def connect(c, code="code-u1") -> dict:
    q = start(c)
    r = c.get(f"/api/integrations/procore/oauth/callback?code={code}&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303
    return {k: v[0] for k, v in parse_qs(r.headers["location"].split("?", 1)[1]).items()}


def inst(platform, tenant_id) -> ProcoreConnector:
    return ProcoreConnector(ConnectorContext(platform, tenant_id))


def expire(i):
    sec = i.secrets()
    sec["oauth"]["expires_at"] = int(time.time()) - 10
    i.save(secrets=sec)


# ---------------------------------------------------------------------------- registry and operator setup
def test_discovered_and_valid():
    registry.discover(force=True)
    assert "procore" not in registry.import_errors
    assert registry.get("procore") is ProcoreConnector and validate_connector_class(ProcoreConnector) == []
    d = ProcoreConnector.describe({})
    assert d["auth"] == "oauth2" and d["operator_configured"] is False and d["unconfirmed"]
    assert d["docs_url"].startswith("https://github.com/procore/documentation/")


def test_not_configured_without_operator_env(owner, platform, fake):
    platform.env = {}
    body = owner.get("/api/integrations").json()
    view = {p["provider"]: p for p in body["providers"]}["procore"]
    assert view["operator_configured"] is False and view["missing_env"] == ["PROCORE_CLIENT_ID",
                                                                            "PROCORE_CLIENT_SECRET"]
    assert "procore" not in body["load_errors"]
    r = owner.post("/api/integrations/procore/oauth/start")
    assert r.status_code == 503 and "PROCORE_CLIENT_ID" in r.json()["detail"]
    assert owner.get("/api/integrations/procore").status_code == 200
    for method, path in (("get", "/companies"), ("post", "/sync"), ("post", "/test")):
        r = getattr(owner, method)(f"/api/integrations/procore{path}")
        assert r.status_code == 503, (path, r.text)
    assert owner.get("/api/integrations/procore/report").json() == {"last_sync": None, "document": None}
    assert fake.seen == []                                     # nothing was sent anywhere


def test_bad_procore_env_is_reported_as_not_configured(owner, platform, fake):
    platform.env = {**ENV, "PROCORE_ENV": "staging"}
    view = {p["provider"]: p for p in owner.get("/api/integrations").json()["providers"]}["procore"]
    assert view["operator_configured"] is False and view["missing_env"] == ["PROCORE_ENV"]
    r = owner.post("/api/integrations/procore/oauth/start")
    assert r.status_code == 503 and "PROCORE_ENV" in r.json()["detail"] and CLIENT_SECRET not in r.text


# ---------------------------------------------------------------------------- OAuth
def test_oauth_sign_in_follows_the_procore_docs(owner, platform, fake):
    q = start(owner)
    assert (q["host"], q["path"]) == ("login.procore.com", "/oauth/authorize")
    assert set(q) - {"host", "path"} == {"client_id", "response_type", "redirect_uri", "state"}    # no scope/PKCE
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://testserver/api/integrations/procore/oauth/callback"
    assert platform.store.get_connection(owner.tenant_id, "procore")["status"] == "pending"
    r = owner.get(f"/api/integrations/procore/oauth/callback?code=code-u1&state={q['state']}", follow_redirects=False)
    assert r.headers["location"] == "/#/settings?integration=procore&result=connected"
    form = fake.token_forms[0]
    assert form == {"grant_type": "authorization_code", "code": "code-u1", "redirect_uri": q["redirect_uri"],
                    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    token_call = next(r for r in fake.seen if r.url.path == "/oauth/token")
    assert "authorization" not in token_call.headers                   # client credentials go in the body
    companies_call = next(r for r in fake.seen if r.url.path == "/rest/v1.0/companies")
    assert companies_call.headers["authorization"] == "Bearer procore-at-1"
    assert "procore-company-id" not in companies_call.headers           # not needed for /companies (tutorial_mpz)
    conn = platform.store.get_connection(owner.tenant_id, "procore", with_secret=True)
    assert conn["status"] == "connected" and conn["external_account"] == "Acme Builders Inc"
    assert conn["config"]["company_id"] == "7656"                       # the only company is selected for you
    assert "procore-at-1" not in conn["secret_blob"] and "procore-rt-1" not in conn["secret_blob"]
    tok = inst(platform, owner.tenant_id).secrets()["oauth"]
    assert tok["access_token"] == "procore-at-1" and tok["refresh_token"] == "procore-rt-1"
    assert 5300 < tok["expires_at"] - time.time() <= 5400
    detail = owner.get("/api/integrations/procore").text
    assert "procore-at-1" not in detail and "procore-rt-1" not in detail and CLIENT_SECRET not in detail


def test_sandbox_environment_uses_the_sandbox_hosts(owner, platform):
    platform.env = {**ENV, "PROCORE_ENV": "sandbox"}
    fake = FakeProcore("sandbox")
    seen = mock_http(platform, fake)
    q = start(owner)
    assert q["host"] == "login-sandbox.procore.com"
    r = owner.get(f"/api/integrations/procore/oauth/callback?code=code-u1&state={q['state']}", follow_redirects=False)
    assert r.headers["location"].endswith("result=connected")
    assert {r.url.host for r in seen} == {"login-sandbox.procore.com", "sandbox.procore.com"}
    view = {p["provider"]: p for p in owner.get("/api/integrations").json()["providers"]}["procore"]
    assert view["procore_environment"] == "sandbox"


def test_refresh_tokens_rotate_and_are_spent_once(owner, platform, fake):
    connect(owner)
    i = inst(platform, owner.tenant_id)
    expire(i)
    r = owner.post("/api/integrations/procore/test")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    form = fake.token_forms[-1]
    assert form == {"grant_type": "refresh_token", "refresh_token": "procore-rt-1", "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "redirect_uri": "http://testserver/api/integrations/procore/oauth/callback"}
    tok = i.secrets()["oauth"]
    assert (tok["access_token"], tok["refresh_token"]) == ("procore-at-2", "procore-rt-2")
    assert "procore-rt-1" not in fake.refresh_tokens                    # the old one is gone at Procore
    expire(i)
    assert i.access_token() == "procore-at-3" and i.secrets()["oauth"]["refresh_token"] == "procore-rt-3"


def test_concurrent_refreshes_spend_the_refresh_token_once(owner, platform, fake):
    connect(owner)
    expire(inst(platform, owner.tenant_id))
    n = len(fake.token_forms)
    got, gate = [], threading.Barrier(4)

    def go():
        gate.wait()
        got.append(inst(platform, owner.tenant_id).access_token())
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == ["procore-at-2"] * 4 and len(fake.token_forms) == n + 1


def test_a_spent_refresh_token_asks_the_customer_to_reconnect(owner, platform, fake):
    connect(owner)
    i = inst(platform, owner.tenant_id)
    expire(i)
    fake.refresh_tokens.clear()                     # e.g. the response to an earlier refresh was lost
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    assert platform.store.get_connection(owner.tenant_id, "procore")["status"] == "error"


def test_a_401_refreshes_once_then_retries(owner, platform, fake):
    connect(owner)
    fake.access.pop("procore-at-1")                 # revoked at Procore before it expired
    r = owner.post("/api/integrations/procore/test")
    assert r.json()["ok"] is True
    assert fake.token_forms[-1]["grant_type"] == "refresh_token"
    fake.access.clear()
    fake.refresh_tokens.clear()
    r = owner.post("/api/integrations/procore/test")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]


def test_disconnect_revokes_at_procore_and_forgets_the_tokens(owner, platform, fake):
    connect(owner)
    r = owner.post("/api/integrations/procore/disconnect")
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    assert fake.revoked == [{"token": "procore-at-1", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}]
    assert "procore-at-1" not in fake.access and "procore-rt-1" not in fake.refresh_tokens
    assert platform.store.get_connection(owner.tenant_id, "procore", with_secret=True)["secret_blob"] is None


# ---------------------------------------------------------------------------- company picker
def test_company_picker_when_the_login_has_several_companies(owner, platform, fake):
    assert connect(owner, "code-multi")["result"] == "connected"
    conn = platform.store.get_connection(owner.tenant_id, "procore")
    assert "company_id" not in conn["config"] and conn["external_account"] == "2 Procore companies: choose one"
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 422 and "Choose" in r.json()["detail"]
    t = owner.post("/api/integrations/procore/test").json()
    assert t["ok"] is True and [c["id"] for c in t["details"]["companies"]] == [7656, 8800]
    picker = owner.get("/api/integrations/procore/companies").json()
    assert picker == {"companies": [{"id": 7656, "name": "Acme Builders Inc", "active": True},
                                    {"id": 8800, "name": "Coastal Constructors LLC", "active": True}],
                      "company_id": None}
    assert owner.post("/api/integrations/procore/company", json={"company_id": 9999}).status_code == 422
    r = owner.post("/api/integrations/procore/company", json={"company_id": 8800})
    assert r.status_code == 200 and r.json()["company"]["name"] == "Coastal Constructors LLC"
    conn = platform.store.get_connection(owner.tenant_id, "procore")
    assert conn["config"]["company_id"] == "8800" and conn["external_account"] == "Coastal Constructors LLC"
    # the generic settings route can change it too; the connector's own keys survive a settings save
    r = owner.put("/api/integrations/procore/config", json={"config": {"company_id": "7656",
                                                                       "california_only": False}})
    assert r.status_code == 200 and r.json()["config"]["company_id"] == "7656"
    assert owner.put("/api/integrations/procore/config", json={"config": {"last_sync": {}}}).status_code == 422
    t = owner.post("/api/integrations/procore/test").json()
    assert t["ok"] and t["account"] == "Acme Builders Inc"
    assert platform.store.get_connection(owner.tenant_id, "procore")["external_account"] == "Acme Builders Inc"


def test_test_fails_when_the_selected_company_is_no_longer_visible(owner, platform, fake):
    connect(owner)
    fake.user_companies["u1"] = [8800]
    r = owner.post("/api/integrations/procore/test").json()
    assert r["ok"] is False and "can't see company 7656" in r["message"]
    assert platform.store.get_connection(owner.tenant_id, "procore")["status"] == "error"


# ---------------------------------------------------------------------------- import + report
def test_import_projects_and_vendors_with_a_compliance_report(owner, platform, fake, monkeypatch):
    monkeypatch.setattr(procore, "PER_PAGE", 2)              # make the fake paginate
    connect(owner)
    owner.put("/api/integrations/procore/config", json={"config": {"alert_on_problems": False}})
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["company"] == {"id": 7656, "name": "Acme Builders Inc", "active": True}
    p = out["projects"]
    assert (p["fetched"], p["created"], p["updated"]) == (4, 2, 0)
    assert sorted(s["reason"] for s in p["skipped"]) == ["inactive in Procore", "not in California (NV)"]
    rows = {x["name"]: x for x in platform.store.list("projects", owner.tenant_id)}
    assert set(rows) == {"Harbor Lofts", "Mesa Clinic"}
    lofts = rows["Harbor Lofts"]
    assert (lofts["address"], lofts["city"], lofts["county"], lofts["status"]) == \
        ("5678 First Street", "Anytown", "Santa Barbara", "active")
    assert lofts["extra"]["procore"]["project_id"] == 101 and lofts["extra"]["procore"]["company_id"] == 7656
    assert rows["Mesa Clinic"]["extra"]["procore"]["project_number"] == "MC-2"
    v = out["vendors"]
    assert (v["fetched"], v["created"], v["updated"], v["inactive_skipped"], v["without_license"]) == (5, 3, 0, 1, 1)
    assert v["no_license"] == [{"procore_vendor_id": 504, "name": "Lumber Supply Co", "reason": "no license number"}]
    subs = {s["license_number"]: s for s in platform.store.list("subs", owner.tenant_id)}
    assert set(subs) == {"123456", "999999", "777000"}
    assert subs["123456"]["name"] == "Example Plumbing Inc." and subs["123456"]["email"] == "pm@plumb.test"
    assert subs["123456"]["scope"] == "Plumbing" and subs["777000"]["phone"] == "805-555-0100"
    rep = out["report"]
    assert rep["total"] == 3 and sum(rep["counts_by_risk"].values()) == 3
    by_vendor = {x["procore_vendor_id"]: x for x in rep["rows"]}
    assert by_vendor[502]["risk"] == "do_not_use" and by_vendor[502]["sub_id"] == subs["999999"]["id"]
    assert by_vendor[502]["procore_license_number"] == "CSLB #999999"
    assert by_vendor[503]["risk"] in ("do_not_use", "high") and not by_vendor[503]["workers_comp_ok"]
    assert by_vendor[501]["license_status"] == "active" and by_vendor[501]["classification_match"] is True
    assert rep["disclaimer"] and rep["table_markdown"].startswith("|")
    # every company-scoped request carried the Procore-Company-Id header and was paginated as documented
    scoped = [q for q in fake.api_calls if q.url.path in ("/rest/v1.0/projects", "/rest/v1.0/vendors")]
    assert all(q.headers["procore-company-id"] == "7656" and q.url.params["company_id"] == "7656" for q in scoped)
    assert sorted((q.url.path, q.url.params["page"]) for q in scoped) == [
        ("/rest/v1.0/projects", "1"), ("/rest/v1.0/projects", "2"),
        ("/rest/v1.0/vendors", "1"), ("/rest/v1.0/vendors", "2"), ("/rest/v1.0/vendors", "3")]
    assert all(q.url.params["per_page"] == "2" for q in scoped)
    assert all(q.method == "GET" for q in fake.api_calls)               # read-only toward Procore
    assert out["procore_rate_limit"]["limit"] == 600
    # the report is saved as a document, and the summary is kept
    doc = platform.store.get("documents", owner.tenant_id, out["report_document_id"])
    assert doc["kind"] == "compliance_report" and "Lapsed Builders LLC" in doc["body_markdown"]
    assert "Lumber Supply Co" in doc["body_markdown"]
    last = owner.get("/api/integrations/procore/report").json()
    assert last["document"]["id"] == doc["id"] and last["last_sync"]["counts_by_risk"] == rep["counts_by_risk"]
    assert last["last_sync"]["projects"]["created"] == 2


def test_import_is_idempotent_and_keeps_tradecomply_edits(owner, platform, fake):
    connect(owner)
    owner.post("/api/integrations/procore/sync")
    store, tid = platform.store, owner.tenant_id
    lofts = next(x for x in store.list("projects", tid) if x["name"] == "Harbor Lofts")
    store.update("projects", tid, lofts["id"], {"public_or_private": "public", "contract_price": 250000,
                                                "extra": {**lofts["extra"], "claimant_role": "subcontractor"}})
    plumber = store.sub_by_license(tid, "123456")
    store.update("subs", tid, plumber["id"], {"coi_expiry": "2030-01-01", "w9_on_file": True})
    fake.projects[7656][0]["name"] = "Harbor Lofts Phase 2"
    fake.vendors[7656][0]["name"] = "Example Plumbing, Inc."
    out = owner.post("/api/integrations/procore/sync").json()
    assert (out["projects"]["created"], out["projects"]["updated"]) == (0, 2)
    assert (out["vendors"]["created"], out["vendors"]["updated"]) == (0, 3)
    assert len(store.list("projects", tid)) == 2 and len(store.list("subs", tid)) == 3
    lofts = store.get("projects", tid, lofts["id"])
    assert lofts["name"] == "Harbor Lofts Phase 2" and lofts["public_or_private"] == "public"
    assert lofts["contract_price"] == 250000 and lofts["extra"]["claimant_role"] == "subcontractor"
    plumber = store.get("subs", tid, plumber["id"])
    assert plumber["name"] == "Example Plumbing, Inc." and plumber["coi_expiry"] == "2030-01-01"
    row = next(x for x in out["report"]["rows"] if x["procore_vendor_id"] == 501)
    assert row["coi_status"] == "current" and row["risk"] == "low"       # the COI entered in TradeComply counts


def test_all_vendors_scope_imports_unlicensed_vendors_once(owner, platform, fake):
    connect(owner)
    owner.put("/api/integrations/procore/config", json={"config": {"vendor_scope": "all"}})
    owner.post("/api/integrations/procore/sync", json={"projects": False})
    owner.post("/api/integrations/procore/sync", json={"projects": False})
    names = sorted(s["name"] for s in platform.store.list("subs", owner.tenant_id))
    assert names == ["Example Plumbing Inc.", "Lapsed Builders LLC", "Lumber Supply Co", "Solo Roofing"]
    assert platform.store.list("projects", owner.tenant_id) == []
    before = owner.get("/api/integrations/procore/report").json()["last_sync"]
    owner.post("/api/integrations/procore/sync", json={"vendors": False})     # projects only: the report stays
    after = owner.get("/api/integrations/procore/report").json()
    assert after["last_sync"]["report_document_id"] == before["report_document_id"] and after["document"]
    assert after["last_sync"]["vendors"] == before["vendors"] and after["last_sync"]["projects"]["created"] == 2


def test_a_sub_entered_without_a_license_gets_the_procore_number(owner, platform, fake):
    store, tid = platform.store, owner.tenant_id
    manual = store.insert("subs", tid, {"name": "Solo Roofing", "coi_expiry": "2030-06-30"})
    connect(owner)
    owner.post("/api/integrations/procore/sync", json={"projects": False})
    roofers = [s for s in store.list("subs", tid) if s["name"] == "Solo Roofing"]
    assert [s["id"] for s in roofers] == [manual["id"]] and roofers[0]["license_number"] == "777000"
    assert roofers[0]["coi_expiry"] == "2030-06-30"


def test_odd_procore_records_are_cleaned_or_skipped(owner, platform, fake):
    fake.vendors[7656] = ["junk", {"id": None, "name": "No id"}, {**vendor(701, "Tab\tCo\x00 <b>Inc</b>", 777000)},
                          {**vendor(702, "", "123456"), "name": None}, {**vendor(703, "Dup", "555000")},
                          {**vendor(703, "Dup again", "555000")}]
    fake.projects[7656] = [{"id": "301", "name": "  Spaced  ", "state_code": "ca"}, {"name": "no id"}, "junk"]
    connect(owner)
    out = owner.post("/api/integrations/procore/sync").json()
    assert out["projects"]["created"] == 1 and out["vendors"]["created"] == 3
    subs = {s["license_number"]: s for s in platform.store.list("subs", owner.tenant_id)}
    assert subs["777000"]["name"] == "Tab Co <b>Inc</b>"            # one line, control characters removed
    assert subs["123456"]["name"] == "Procore vendor 702" and subs["555000"]["name"] == "Dup"
    assert platform.store.list("projects", owner.tenant_id)[0]["name"] == "Spaced"


def test_large_directories_keep_the_saved_summary_small(owner, platform, fake):
    fake.projects[7656] += [project(1000 + n, f"Nevada job {n}", state="NV") for n in range(400)]
    connect(owner)
    out = owner.post("/api/integrations/procore/sync").json()
    assert out["projects"]["skipped_total"] == 402 and len(out["projects"]["skipped"]) == 402
    last = owner.get("/api/integrations/procore/report").json()["last_sync"]
    assert last["projects"]["skipped"] == 402
    assert len(json.dumps(platform.store.get_connection(owner.tenant_id, "procore")["config"])) < 5000


def test_do_not_use_vendors_raise_one_alert_each(owner, platform, fake):
    r = owner.post("/api/integrations/webhooks", json={"name": "alerts", "url": "https://hooks.example.test/x",
                                                       "kind": "generic", "events": ["license_problem_found"]})
    assert r.status_code == 201, r.text
    connect(owner)
    first = owner.post("/api/integrations/procore/sync").json()
    queued = platform.store.list_outbound(owner.tenant_id)
    do_not_use = [x for x in first["report"]["rows"] if x["risk"] == "do_not_use"]
    assert first["alerts_sent"] == len(do_not_use) == len(queued) >= 1
    assert queued[-1]["event"] == "license_problem_found" and queued[-1]["payload"]["source"] == "procore"
    second = owner.post("/api/integrations/procore/sync").json()
    assert second["alerts_sent"] == 0 and len(platform.store.list_outbound(owner.tenant_id)) == len(queued)


def test_field_role_cannot_import_and_writes_need_csrf(app, owner, fake):
    connect(owner)
    field = TestClient(app)
    join(field, owner, "field@acme.test", "field")
    assert field.post("/api/integrations/procore/sync").status_code == 403
    assert field.get("/api/integrations/procore/companies").status_code == 403
    no_csrf = TestClient(app, cookies=owner.cookies)
    assert no_csrf.post("/api/integrations/procore/sync").status_code == 403


# ---------------------------------------------------------------------------- rate limits, errors
def rate_limited(path, wait_s, retry_after=None):
    def fn(request):
        if request.url.path != path:
            return None
        headers = {"X-Rate-Limit-Limit": "600", "X-Rate-Limit-Remaining": "0",
                   "X-Rate-Limit-Reset": str(int(time.time() + wait_s))}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return json_response(429, {"errors": "Too Many Requests"}, headers)
    return fn


def test_a_429_pauses_until_the_reset_time_then_retries(owner, platform, fake, no_real_sleep):
    connect(owner)
    fake.scripted.append(rate_limited("/rest/v1.0/vendors", 5))
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 200, r.text
    assert len(no_real_sleep) == 1 and 4 <= no_real_sleep[0] <= 7          # until after X-Rate-Limit-Reset + jitter
    assert r.json()["vendors"]["created"] == 3


def test_retry_after_is_honoured_when_longer(owner, platform, fake, no_real_sleep):
    connect(owner)
    fake.scripted.append(rate_limited("/rest/v1.0/projects", 1, retry_after=12))
    assert owner.post("/api/integrations/procore/sync").status_code == 200
    assert 12 <= no_real_sleep[0] <= 13.5


def test_remaining_zero_pauses_before_the_next_request(owner, platform, fake, no_real_sleep, monkeypatch):
    monkeypatch.setattr(procore, "PER_PAGE", 2)
    connect(owner)
    fake.remaining = 2                # the next response reports 1 left, the one after 0
    fake.reset = int(time.time()) + 3
    assert owner.post("/api/integrations/procore/sync").status_code == 200
    assert no_real_sleep and 2 <= no_real_sleep[0] <= 5


def test_a_long_rate_limit_returns_429_and_imports_nothing(owner, platform, fake, no_real_sleep):
    connect(owner)
    fake.scripted.append(rate_limited("/rest/v1.0/vendors", 1800))
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 429 and 1790 <= int(r.headers["retry-after"]) <= 1801
    assert "Nothing was changed" in r.json()["detail"] and no_real_sleep == []
    assert platform.store.list("projects", owner.tenant_id) == []           # projects were read first, not written
    assert platform.store.list("subs", owner.tenant_id) == []


def test_server_errors_are_retried(owner, platform, fake):
    connect(owner)
    fake.scripted.append(lambda q: json_response(503, {"errors": "unavailable"})
                         if q.url.path == "/rest/v1.0/projects" else None)
    assert owner.post("/api/integrations/procore/sync").status_code == 200
    for _ in range(3):
        fake.scripted.append(lambda q: json_response(502, {}) if q.url.path == "/rest/v1.0/projects" else None)
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 502 and "HTTP 502" in r.json()["detail"]


def test_permission_errors_are_explained(owner, platform, fake):
    connect(owner)
    fake.scripted.append(lambda q: json_response(403, {"errors": "forbidden"})
                         if q.url.path == "/rest/v1.0/vendors" else None)
    r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 502 and "HTTP 403" in r.json()["detail"] and "permission" in r.json()["detail"]


def test_missing_company_header_is_reported(owner, platform, fake):
    connect(owner)
    i = inst(platform, owner.tenant_id)
    with platform.http() as http:
        api = ProcoreAPI(i, http)
        with pytest.raises(ConnectorError, match="company id header"):
            api.list("/rest/v1.0/vendors", {"company_id": 7656}, what="vendors")    # no company_id= header
        assert len(api.list("/rest/v1.0/vendors", {"company_id": 7656}, company_id=7656, what="vendors")) == 5


def test_pages_follow_total_without_a_link_header(owner, platform, fake, monkeypatch):
    monkeypatch.setattr(procore, "PER_PAGE", 2)
    connect(owner)
    original = fake.page

    def no_link(request, items):
        resp = original(request, items)
        del resp.headers["link"]
        return resp
    fake.page = no_link
    i = inst(platform, owner.tenant_id)
    with platform.http() as http:
        got = ProcoreAPI(i, http).list("/rest/v1.0/vendors", {"company_id": 7656}, company_id=7656, what="v")
    assert [v["id"] for v in got] == [501, 502, 503, 504, 505]


def test_imports_run_one_at_a_time_per_account(owner, platform, fake):
    connect(owner)
    lock = procore._sync_lock(owner.tenant_id)
    with lock:
        r = owner.post("/api/integrations/procore/sync")
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    assert owner.post("/api/integrations/procore/sync").status_code == 200


# ---------------------------------------------------------------------------- isolation and hygiene
def test_tenants_are_isolated(app, owner, platform, fake):
    connect(owner, "code-u1")
    other = TestClient(app)
    other.tenant_id = signup(other, email="boss@coastal.test", company="Coastal Constructors")["tenant_id"]
    connect(other, "code-u2")
    assert owner.post("/api/integrations/procore/sync").status_code == 200
    assert platform.store.list("projects", other.tenant_id) == []
    assert platform.store.list("subs", other.tenant_id) == []
    assert other.get("/api/integrations/procore/report").json() == {"last_sync": None, "document": None}
    out = other.post("/api/integrations/procore/sync").json()
    assert out["company"]["id"] == 8800 and [p["name"] for p in out["projects"]["items"]] == ["Coastal Office"]
    assert {s["name"] for s in platform.store.list("subs", owner.tenant_id)} >= {"Example Plumbing Inc."}
    assert [s["name"] for s in platform.store.list("subs", other.tenant_id)] == ["Coastal Plumbing"]
    # each tenant's calls carried its own token; one tenant's blob can't be read as the other's
    by_company = {}
    for q in fake.api_calls:
        if q.url.path == "/rest/v1.0/projects":
            by_company.setdefault(q.headers["procore-company-id"], set()).add(fake.access.get(
                q.headers["authorization"][7:], q.headers["authorization"]))
    assert by_company == {"7656": {"u1"}, "8800": {"u2"}}
    blob = platform.store.get_connection(owner.tenant_id, "procore", with_secret=True)["secret_blob"]
    with pytest.raises(DecryptError):
        platform.secret_box.decrypt_json(blob, connection_context(other.tenant_id, "procore"))
    # tenant B can't pick tenant A's company
    assert other.post("/api/integrations/procore/company", json={"company_id": 7656}).status_code == 422


def test_no_tokens_or_secrets_in_the_logs(owner, platform, fake, caplog):
    caplog.set_level(logging.DEBUG)
    connect(owner)
    expire(inst(platform, owner.tenant_id))
    fake.scripted.append(rate_limited("/rest/v1.0/projects", 2))
    owner.post("/api/integrations/procore/sync")
    owner.post("/api/integrations/procore/disconnect")
    # the test client's own request log (it prints the callback URL it sent) is not the app's log
    app_records = [r for r in caplog.records if "http://testserver/" not in r.getMessage()]
    text = "\n".join(r.getMessage() for r in app_records) + json.dumps([r.args for r in app_records], default=str)
    for secret in (CLIENT_SECRET, "procore-at-1", "procore-rt-1", "procore-at-2", "procore-rt-2", "code-u1"):
        assert secret not in text
    assert "rate limited (429)" in caplog.text


def test_not_connected_tenants_get_409(owner, fake):
    assert owner.get("/api/integrations/procore/companies").status_code == 409
    assert owner.post("/api/integrations/procore/sync").status_code == 409


def test_cslb_number_parsing():
    assert cslb_number("1012345") == "1012345"
    assert cslb_number("CSLB #1012345") == "1012345"
    assert cslb_number("C-10 123456") == "123456"
    assert cslb_number("123456 (C-10, C-7)") == "123456"
    assert cslb_number("C10/D49 #555000") == "555000"
    assert cslb_number("Lic 123456 exp 06/30/2027") == "123456"
    for bad in (None, "", "N/A", "pending", "123-456", "123456 / 654321", "12345678901"):
        assert cslb_number(bad) is None


def test_not_connected_errors_are_not_connector_crashes(platform):
    i = inst(platform, "t_nobody")
    with pytest.raises(NotConnected):
        i.sync()
