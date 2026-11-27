"""Docusign connector (app/connectors/docusign.py): operator configuration, Confidential Authorization Code Grant with
PKCE and Basic client authentication, /oauth/userinfo account discovery (once per sign-in), sending the generated home
improvement contract (with the right-to-cancel notice and both Notice of Cancellation copies) as an HTML document with
anchor tabs, envelope status polling at most every 20 minutes, the HMAC-verified Connect listener, rate limits, tenant
isolation and log hygiene.

FakeDocusign answers in the shapes of Docusign's official docs: POST /oauth/token (platform/auth/reference/
obtain-access-token: access_token, token_type Bearer, refresh_token, expires_in, scope), GET /oauth/userinfo
(platform/auth/reference/user-info: the documented example's sub, name, accounts[] {account_id, is_default,
account_name, base_uri}), Envelopes:create 201 {envelopeId, uri, statusDateTime, status} (how-to/
request-signature-multiple-delivery-methods), Envelopes:get, X-RateLimit-* / X-BurstLimit-* headers and
{errorCode, message} errors (esign101/rules-and-limits, error-codes), and Connect JSON SIM messages {event, data
{accountId, envelopeId}} signed with X-DocuSign-Signature-N (platform/webhooks/connect). Every credential here is made
up."""
import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import connectors as registry
from app.auth import RateLimiter
from app.connectors import ConnectorContext, NotConnected, validate_connector_class
from app.connectors import docusign
from app.connectors.docusign import DocusignConnector, clean_base_uri, parse_time, render_html, verify_connect_hmac
from tests.support import (backend, browser, json_response, join, make_app, mock_http, redirect_query,  # noqa: F401
                           signup)

IK = "7c2b8d7e-0000-4000-8000-cda8a50dd73f"            # made-up integration key (GUID like the docs' example)
SECRET = "d7014634-0000-4000-8000-6842b7aa8861"        # made-up secret key
ENV = {"DOCUSIGN_INTEGRATION_KEY": IK, "DOCUSIGN_SECRET": SECRET, "DOCUSIGN_ENV": "demo"}
APP_URL = "https://app.tradecomply.test"
HMAC_KEY = "Kq3vM0nTfX1pLr8sZy4wB2dE6gH9jQ0uA7cV5bN1mI4="  # made-up Connect key
ACCT_DEFAULT = "a4ec37d6-0000-4000-8000-143885c220e1"
ACCT_OTHER = "a4ec37d6-0000-4000-8000-143885c333aa"
AUTH_HOSTS = {"demo": "account-d.docusign.com", "production": "account.docusign.com"}


def userinfo_accounts(env="demo"):
    """The documented example's two accounts; in demo both live on demo.docusign.net."""
    main = "https://demo.docusign.net" if env == "demo" else "https://na3.docusign.net"
    other = "https://demo.docusign.net" if env == "demo" else "https://eu.docusign.net"
    return [{"account_id": ACCT_OTHER, "is_default": False, "account_name": "Example Europe Ltd", "base_uri": other},
            {"account_id": ACCT_DEFAULT, "is_default": True, "account_name": "Example Corporation", "base_uri": main,
             "organization": {"organization_id": "9c5fb8e1-0000-4000-8000-054ff8a249bf", "links": [
                 {"rel": "self", "href": "https://account.domain.example.com/organizations/9c5fb8e1"}]}}]


class FakeDocusign:
    """account-d.docusign.com (or account.docusign.com) plus the account base URIs. Each sign-in code belongs to one
    Docusign user; each token pair to that user."""

    def __init__(self, env="demo"):
        self.env, self.auth_host = env, AUTH_HOSTS[env]
        self.codes = {"code-1": "user-1", "code-2": "user-2"}
        self.accounts = {"user-1": userinfo_accounts(env), "user-2": userinfo_accounts(env)[:1]}
        self.access, self.refresh_tokens = {}, {}
        self.issued = 0
        self.expires_in = 28800                      # 8 hours (platform/auth/authcode)
        self.expect_challenge = None
        self.token_requests, self.userinfo_calls, self.api_calls = [], [], []
        self.envelopes: dict[str, dict] = {}
        self.scripted: list = []
        self.remaining = 2999
        self.reset = int(time.time()) + 3600
        self.lock = threading.Lock()

    def rate_headers(self):
        self.remaining = max(0, self.remaining - 1)
        return {"X-RateLimit-Limit": "3000", "X-RateLimit-Remaining": str(self.remaining),
                "X-RateLimit-Reset": str(self.reset), "X-BurstLimit-Limit": "200", "X-BurstLimit-Remaining": "199"}

    def issue(self, user):
        self.issued += 1
        at, rt = f"ds-at-{self.issued}", f"ds-rt-{self.issued}"
        self.access[at], self.refresh_tokens[rt] = user, user
        return json_response(200, {"access_token": at, "token_type": "Bearer", "refresh_token": rt,
                                   "expires_in": self.expires_in, "scope": "signature extended"})

    def user(self, request):
        auth = request.headers.get("authorization", "")
        return self.access.get(auth[7:]) if auth.startswith("Bearer ") else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        for i, fn in enumerate(self.scripted):
            resp = fn(request)
            if resp is not None:
                del self.scripted[i]
                return resp
        url = request.url
        if url.host == self.auth_host and url.path == "/oauth/token":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            with self.lock:
                self.token_requests.append({"form": form, "authorization": request.headers.get("authorization")})
                want = "Basic " + base64.b64encode(f"{IK}:{SECRET}".encode()).decode()
                if request.headers.get("authorization") != want:
                    return json_response(401, {"error": "invalid_client"})
                if form.get("grant_type") == "authorization_code":
                    user = self.codes.pop(form.get("code"), None)          # single use
                    if not user:
                        return json_response(400, {"error": "invalid_grant"})
                    if self.expect_challenge is not None:
                        verifier = form.get("code_verifier", "")
                        got = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
                        if got.decode() != self.expect_challenge:
                            return json_response(400, {"error": "invalid_grant"})
                    return self.issue(user)
                if form.get("grant_type") == "refresh_token":
                    user = self.refresh_tokens.pop(form.get("refresh_token"), None)
                    return self.issue(user) if user else json_response(400, {"error": "invalid_grant"})
            return json_response(400, {"error": "unsupported_grant_type"})
        if url.host == self.auth_host and url.path == "/oauth/userinfo":
            self.userinfo_calls.append(request)
            user = self.user(request)
            if not user:
                return json_response(401, {"error": "invalid_token"})
            return json_response(200, {"sub": "4799e5e9-0000-4000-8000-cf4713bbcacc", "name": "First Last",
                                       "given_name": "First", "family_name": "Last",
                                       "created": "2015-08-13T22:03:03.45", "email": "first.last@example.com",
                                       "accounts": self.accounts[user]})
        if url.path.startswith("/restapi/v2.1/accounts/"):
            return self.api(request)
        return json_response(404, {"errorCode": "NOT_FOUND", "message": "not found"})

    def api(self, request):
        self.api_calls.append(request)
        user = self.user(request)
        if not user:
            return json_response(401, {"errorCode": "USER_AUTHENTICATION_FAILED",
                                       "message": "One or both of Username and Password are invalid."},
                                 self.rate_headers())
        parts = request.url.path.split("/")[4:]          # [accountId, ...]
        account = next((a for a in self.accounts[user] if a["account_id"] == parts[0]), None)
        if account is None or urlsplit(account["base_uri"]).hostname != request.url.host:
            return json_response(400, {"errorCode": "USER_LACKS_MEMBERSHIP", "message": "no membership"},
                                 self.rate_headers())
        if len(parts) == 1 and request.method == "GET":
            return json_response(200, {"accountIdGuid": account["account_id"], "accountName": account["account_name"],
                                       "planName": "DEVCENTER_DEMO_April2013"}, self.rate_headers())
        if parts[1:] == ["envelopes"] and request.method == "POST":
            return self.create(request, account)
        if len(parts) == 3 and parts[1] == "envelopes" and request.method == "GET":
            env = self.envelopes.get(parts[2])
            if not env or env["account"] != account["account_id"]:
                return json_response(404, {"errorCode": "ENVELOPE_DOES_NOT_EXIST", "message": "not found"},
                                     self.rate_headers())
            return json_response(200, {k: v for k, v in env.items() if k not in ("account", "body", "html")},
                                 self.rate_headers())
        return json_response(404, {"errorCode": "NOT_FOUND", "message": "not found"}, self.rate_headers())

    def create(self, request, account):
        body = json.loads(request.content)
        doc = base64.b64decode(body["documents"][0]["documentBase64"]).decode("ascii")
        for signer in body["recipients"]["signers"]:          # anchorIgnoreIfNotPresent "false": must be there
            for kind in ("signHereTabs", "dateSignedTabs", "initialHereTabs"):
                for tab in signer["tabs"].get(kind, []):
                    assert tab["anchorIgnoreIfNotPresent"] == "false" and tab["anchorString"] in doc
        eid = str(uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
        self.envelopes[eid] = {"envelopeId": eid, "status": body["status"], "sentDateTime": now,
                               "statusChangedDateTime": now, "emailSubject": body["emailSubject"],
                               "account": account["account_id"], "body": body, "html": doc}
        return json_response(201, {"envelopeId": eid, "uri": f"/envelopes/{eid}", "statusDateTime": now,
                                   "status": body["status"]}, self.rate_headers())

    def complete(self, eid, when: datetime):
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
        self.envelopes[eid].update(status="completed", completedDateTime=stamp, statusChangedDateTime=stamp,
                                   deliveredDateTime=stamp)


# ---------------------------------------------------------------------------- fixtures and helpers
def mount_docusign_routes(app):
    """What app/main.py needs: include docusign.router before the catch-all MCP mount at '/'."""
    app.include_router(docusign.router)
    routes = app.router.routes
    catch_all = [r for r in routes if isinstance(r, Mount) and r.path == ""]
    for r in catch_all:
        routes.remove(r)
    routes.extend(catch_all)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    monkeypatch.setattr(docusign, "_pause", {})
    monkeypatch.setattr(docusign, "_send_limiter", RateLimiter(limit=30, window_s=3600))
    monkeypatch.setattr(docusign, "_connect_limiter", RateLimiter(limit=2000, window_s=3600))


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=APP_URL)
    mount_docusign_routes(a)
    a.state.platform.env = dict(ENV)
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def fake(platform):
    f = FakeDocusign()
    f.seen = mock_http(platform, f)
    return f


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def start(c) -> dict:
    r = c.post("/api/integrations/docusign/oauth/start")
    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["authorize_url"])
    return {"host": url.netloc, "path": url.path, **{k: v[0] for k, v in parse_qs(url.query).items()}}


def connect(c, fake=None, code="code-1") -> dict:
    q = start(c)
    if fake is not None:
        fake.expect_challenge = q["code_challenge"]
    r = c.get(f"/api/integrations/docusign/oauth/callback?code={code}&state={q['state']}", follow_redirects=False)
    assert r.status_code == 303
    return redirect_query(r)


def inst(platform, tenant_id) -> DocusignConnector:
    return DocusignConnector(ConnectorContext(platform, tenant_id))


def today():
    return docusign._today_ca()


def contract(**over):
    t = today()
    base = {"contractor_name": "Acme Builders Inc", "contractor_business_address": "1 Main St, Fresno, CA 93721",
            "contractor_license_number": "123456", "contractor_email": "office@acme.test",
            "contractor_phone": "559-555-0100", "buyer_names": ["Pat Homeowner", "Sam Homeowner"],
            "project_address": "9 Oak Ave, Fresno, CA 93704", "contract_price_usd": 25000,
            "project_description": "Kitchen remodel", "materials_and_equipment": "Oak cabinets, quartz counters",
            "down_payment_usd": 1000,
            "progress_payments": [{"amount_usd": 12000, "for_work": "Cabinets installed"},
                                  {"amount_usd": 12000, "for_work": "Final walkthrough"}],
            "start_date": (t + timedelta(days=30)).isoformat(), "completion_date": (t + timedelta(days=90)).isoformat(),
            "substantial_commencement": "demolition begins at the project address", "cgl_insurer_name": "Ins Co",
            "cgl_insurer_phone": "800-555-0100"}
    return {**base, **over}


ADDITIONAL_TERMS = ("[[ATTORNEY: warranty; permits and inspections and who obtains and pays for them; permissible "
                    "delays; allowances; insurance; termination; dispute resolution; attorney fees.]]")
FILL = {ADDITIONAL_TERMS: "Warranty: one year on workmanship.\nThe contractor obtains and pays for permits."}


def send_body(project_id, **over):
    body = {"project_id": project_id, "contract": contract(), "buyer_emails": ["pat@home.test", "sam@home.test"],
            "contractor_signer_name": "Alex Builder", "contractor_signer_email": "alex@acme.test", "fill_ins": FILL}
    return {**body, **over}


def new_project(platform, tenant_id, name="Oak Ave kitchen"):
    return platform.store.insert("projects", tenant_id, {"name": name, "status": "active"})["id"]


def save_hmac_key(c, key=HMAC_KEY):
    r = c.put("/api/integrations/docusign/config", json={"config": {"connect_hmac_key": key}})
    assert r.status_code == 200, r.text


def sign(body: bytes, key=HMAC_KEY) -> str:
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def sim(event, envelope_id, account=ACCT_DEFAULT, when=None) -> bytes:
    """A JSON SIM message (json-sim-event-model: event + standard data + data {accountId, userId, envelopeId})."""
    when = when or datetime.now(timezone.utc)
    return json.dumps({"event": event, "apiVersion": "v2.1", "uri": f"/restapi/v2.1/accounts/{account}/envelopes/"
                       f"{envelope_id}", "retryCount": 0, "configurationId": 10418,
                       "generatedDateTime": when.strftime("%Y-%m-%dT%H:%M:%S.%f0Z"),
                       "data": {"accountId": account, "userId": "4799e5e9-0000-4000-8000-cf4713bbcacc",
                                "envelopeId": envelope_id}}).encode()


def listener_path(platform, tenant_id):
    return f"/api/integrations/docusign/connect-listener/{tenant_id}/{docusign.connect_tag(platform, tenant_id)}"


# ---------------------------------------------------------------------------- registry and operator setup
def test_discovered_and_valid():
    registry.discover(force=True)
    assert "docusign" not in registry.import_errors
    assert registry.get("docusign") is DocusignConnector and validate_connector_class(DocusignConnector) == []
    d = DocusignConnector.describe({})
    assert d["auth"] == "oauth2" and d["operator_configured"] is False and d["unconfirmed"]
    assert d["docs_url"].startswith("https://developers.docusign.com/") and d["catalog_id"] == "docusign"
    assert DocusignConnector.oauth.scopes == ("signature", "extended")


def test_not_configured_without_operator_env(owner, platform, fake):
    platform.env = {}
    body = owner.get("/api/integrations").json()
    view = {p["provider"]: p for p in body["providers"]}["docusign"]
    assert view["operator_configured"] is False
    assert view["missing_env"] == ["DOCUSIGN_INTEGRATION_KEY", "DOCUSIGN_SECRET", "DOCUSIGN_ENV"]
    assert "docusign" not in body["load_errors"]
    r = owner.post("/api/integrations/docusign/oauth/start")
    assert r.status_code == 503 and "DOCUSIGN_INTEGRATION_KEY" in r.json()["detail"]
    assert owner.get("/api/integrations/docusign").status_code == 200
    pid = new_project(platform, owner.tenant_id)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 503, r.text
    assert owner.post("/api/integrations/docusign/test").status_code == 503
    assert owner.get(f"/api/integrations/docusign/projects/{pid}/envelopes").json()["envelopes"] == []
    r = owner.post("/api/integrations/docusign/preview", json=send_body(pid))     # local only: still works
    assert r.status_code == 200 and r.json()["ready"] is True
    assert fake.seen == []                                                       # nothing was sent anywhere


def test_bad_docusign_env_is_reported_as_not_configured(owner, platform, fake):
    platform.env = {**ENV, "DOCUSIGN_ENV": "staging"}
    view = {p["provider"]: p for p in owner.get("/api/integrations").json()["providers"]}["docusign"]
    assert view["operator_configured"] is False and view["missing_env"] == ["DOCUSIGN_ENV"]
    r = owner.post("/api/integrations/docusign/oauth/start")
    assert r.status_code == 503 and "DOCUSIGN_ENV" in r.json()["detail"] and SECRET not in r.text
    assert fake.seen == []


# ---------------------------------------------------------------------------- OAuth
def test_oauth_sign_in_follows_the_docusign_docs(owner, platform, fake):
    q = start(owner)
    assert (q["host"], q["path"]) == ("account-d.docusign.com", "/oauth/auth")
    assert set(q) - {"host", "path"} == {"response_type", "client_id", "redirect_uri", "state", "scope",
                                         "code_challenge", "code_challenge_method"}
    assert q["response_type"] == "code" and q["client_id"] == IK and q["scope"] == "signature extended"
    assert q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == f"{APP_URL}/api/integrations/docusign/oauth/callback"
    assert platform.store.get_connection(owner.tenant_id, "docusign")["status"] == "pending"
    fake.expect_challenge = q["code_challenge"]
    r = owner.get(f"/api/integrations/docusign/oauth/callback?code=code-1&state={q['state']}", follow_redirects=False)
    assert r.headers["location"] == "/#/settings?integration=docusign&result=connected"
    tr = fake.token_requests[0]
    assert tr["authorization"] == "Basic " + base64.b64encode(f"{IK}:{SECRET}".encode()).decode()
    assert set(tr["form"]) == {"grant_type", "code", "code_verifier"}          # the documented body (+ PKCE)
    assert tr["form"]["grant_type"] == "authorization_code" and tr["form"]["code"] == "code-1"
    assert len(fake.userinfo_calls) == 1 and fake.userinfo_calls[0].headers["authorization"] == "Bearer ds-at-1"
    conn = platform.store.get_connection(owner.tenant_id, "docusign", with_secret=True)
    assert conn["status"] == "connected" and conn["external_account"] == "Example Corporation (demo)"
    assert [a["account_id"] for a in conn["config"]["accounts"]] == [ACCT_OTHER, ACCT_DEFAULT]
    assert conn["config"]["environment"] == "demo"
    assert "ds-at-1" not in conn["secret_blob"] and "ds-rt-1" not in conn["secret_blob"]
    assert "first.last@example.com" not in json.dumps(conn["config"])            # the login's email isn't kept
    tok = inst(platform, owner.tenant_id).secrets()["oauth"]
    assert tok["access_token"] == "ds-at-1" and tok["refresh_token"] == "ds-rt-1"
    assert 28700 < tok["expires_at"] - time.time() <= 28800
    detail = owner.get("/api/integrations/docusign").text
    assert "ds-at-1" not in detail and "ds-rt-1" not in detail and SECRET not in detail


def test_production_environment_uses_the_production_auth_host(owner, platform):
    platform.env = {**ENV, "DOCUSIGN_ENV": "production"}
    fake = FakeDocusign("production")
    seen = mock_http(platform, fake)
    q = start(owner)
    assert q["host"] == "account.docusign.com"
    r = owner.get(f"/api/integrations/docusign/oauth/callback?code=code-1&state={q['state']}", follow_redirects=False)
    assert r.headers["location"].endswith("result=connected")
    assert owner.post("/api/integrations/docusign/test").json()["ok"] is True
    assert {r.url.host for r in seen} == {"account.docusign.com", "na3.docusign.net"}
    view = {p["provider"]: p for p in owner.get("/api/integrations").json()["providers"]}["docusign"]
    assert view["docusign_environment"] == "production"
    assert inst(platform, owner.tenant_id).status()["external_account"] == "Example Corporation"


def test_wrong_client_secret_fails_the_sign_in_cleanly(owner, platform, fake):
    platform.env = {**ENV, "DOCUSIGN_SECRET": "not-the-secret"}
    params = connect(owner)
    assert params["result"] == "error" and "invalid_client" in params["message"]
    assert "not-the-secret" not in params["message"]
    assert platform.store.get_connection(owner.tenant_id, "docusign")["status"] == "error"


def test_refresh_uses_basic_auth_and_skips_userinfo(owner, platform, fake):
    connect(owner, fake)
    i = inst(platform, owner.tenant_id)
    sec = i.secrets()
    sec["oauth"]["expires_at"] = int(time.time()) - 10
    i.save(secrets=sec)
    r = owner.post("/api/integrations/docusign/test")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    tr = fake.token_requests[-1]
    assert tr["form"] == {"grant_type": "refresh_token", "refresh_token": "ds-rt-1"}
    assert tr["authorization"] == "Basic " + base64.b64encode(f"{IK}:{SECRET}".encode()).decode()
    tok = i.secrets()["oauth"]
    assert (tok["access_token"], tok["refresh_token"]) == ("ds-at-2", "ds-rt-2")
    assert len(fake.userinfo_calls) == 1          # "do not call it when a refresh token is used"


def test_connection_test_reads_the_account(owner, platform, fake):
    connect(owner, fake)
    r = owner.post("/api/integrations/docusign/test").json()
    assert r["ok"] is True and r["account"] == "Example Corporation"
    assert r["details"]["account_id"] == ACCT_DEFAULT and r["details"]["environment"] == "demo"
    assert {a["account_name"] for a in r["details"]["accounts"]} == {"Example Corporation", "Example Europe Ltd"}
    assert r["details"]["connect_webhook"] is False
    call = fake.api_calls[-1]
    assert call.method == "GET" and str(call.url) == f"https://demo.docusign.net/restapi/v2.1/accounts/{ACCT_DEFAULT}"
    assert call.headers["authorization"] == "Bearer ds-at-1"


def test_chosen_account_and_unknown_account(owner, platform, fake):
    connect(owner, fake)
    r = owner.put("/api/integrations/docusign/config", json={"config": {"account_id": ACCT_OTHER}})
    assert r.status_code == 200 and r.json()["config"]["accounts"]          # the account list survives a save
    assert owner.post("/api/integrations/docusign/test").json()["account"] == "Example Europe Ltd"
    unknown = "ffffffff-0000-4000-8000-000000000000"
    owner.put("/api/integrations/docusign/config", json={"config": {"account_id": unknown}})
    r = owner.post("/api/integrations/docusign/test").json()
    assert r["ok"] is False and "account ID" in r["message"]


def test_base_uri_must_be_a_docusign_host():
    assert clean_base_uri("https://na3.docusign.net") == "https://na3.docusign.net"
    assert clean_base_uri("https://demo.docusign.net/") == "https://demo.docusign.net"
    for bad in ("http://na3.docusign.net", "https://evil.example.com", "https://docusign.net.evil.com",
                "https://na3.docusign.net/restapi", "https://user:pw@na3.docusign.net", "https://na3.docusign.net:8443",
                None, 7):
        assert clean_base_uri(bad) is None, bad


def test_userinfo_account_on_a_foreign_host_is_skipped(owner, platform, fake):
    fake.accounts["user-1"] = [{"account_id": ACCT_DEFAULT, "is_default": True, "account_name": "X",
                                "base_uri": "https://attacker.example.com"}]
    assert connect(owner, fake)["result"] == "connected"
    assert inst(platform, owner.tenant_id).config()["accounts"] == []
    r = owner.post("/api/integrations/docusign/test").json()
    assert r["ok"] is False and "no account" in r["message"]
    assert all(r.url.host != "attacker.example.com" for r in fake.seen)


def test_server_switching_environment_requires_reconnect(owner, platform, fake):
    connect(owner, fake)
    platform.env = {**ENV, "DOCUSIGN_ENV": "production"}
    r = owner.post("/api/integrations/docusign/test")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]


def test_disconnect_forgets_tokens_without_a_revoke_call(owner, platform, fake):
    connect(owner, fake)
    before = len(fake.seen)
    r = owner.post("/api/integrations/docusign/disconnect")
    assert r.status_code == 200 and r.json()["status"] == "disconnected"
    assert len(fake.seen) == before                       # no revocation endpoint is documented
    assert inst(platform, owner.tenant_id).secrets() == {}


# ---------------------------------------------------------------------------- the document
def test_render_html_escapes_and_keeps_structure():
    md = ("# Home Improvement\n\n<!-- PRINT: at least 10-pt bold (B&P 7159(d)(3)) -->\n\n**Bold** <script>x</script>"
          "\n\n| # | Work | Amount |\n|---|---|---|\n| 1 | A & B | $1.00 |\n\n- item\n\n---\n\n## Next"
          "\n\nSign \x02s1\x03___")
    out = render_html(md, {"s1": "/tcabcds1/"}, "T")
    assert "<script>" not in out and "&lt;script&gt;" in out and "PRINT" not in out
    assert "<strong>Bold</strong>" in out and "A &amp; B" in out and "<th" in out and "<li>item</li>" in out
    assert 'page-break-before:always' in out and '<span style="color:#ffffff">/tcabcds1/</span>' in out
    assert out.isascii()


def test_preview_has_the_cancellation_notice_and_anchors(owner, platform):
    r = owner.post("/api/integrations/docusign/preview", json=send_body(1))
    assert r.status_code == 200, r.text
    body = r.json()
    page = body["html"]
    assert body["cancellation"]["kind"] == "three_day" and body["cancellation"]["business_days"] == 3
    assert "Three-Day Right to Cancel" in page and "within three business days" in page
    assert page.count("Notice of Cancellation (copy") == 2 and page.count("I hereby cancel this transaction.") == 2
    assert "Owner acknowledges receipt of this notice." in page
    assert "[[" not in page and "Warranty: one year on workmanship.<br>The contractor obtains" in page
    assert [s["role"] for s in body["signers"]] == ["contractor", "buyer", "buyer"]
    assert body["signers"][1]["email"] == "p***@home.test"
    assert re.search(r"font-size:12pt", page) and re.search(r"<h1 style=\"font-size:16pt;font-weight:bold", page)


@pytest.mark.parametrize("over, notice", [({"buyer_age_65_or_older": True}, "Five-Day Right to Cancel"),
                                          ({"disaster_repair": True}, "Seven-Day Right to Cancel")])
def test_senior_and_disaster_notices(owner, over, notice):
    r = owner.post("/api/integrations/docusign/preview", json=send_body(1, contract=contract(**over)))
    assert r.status_code == 200, r.text
    assert notice in r.json()["html"]


@pytest.mark.parametrize("over, field", [
    ({"contract": contract(negotiated_at_contractor_place_of_business=True)},
     "negotiated_at_contractor_place_of_business"),
    ({"contract": contract(service_and_repair=True)}, "service_and_repair"),
    ({"buyer_emails": ["pat@home.test"]}, "buyer_emails"),
    ({"buyer_emails": ["pat@home.test", "ALEX@acme.test"]}, "buyer_emails"),
    ({"buyer_emails": ["pat@home.test", "not-an-email"]}, "buyer_emails"),
    ({"contract": contract(buyer_names=["Pat Homeowner", "pat homeowner"])}, "buyer_names"),
    ({"contract": contract(signing_date="2000-01-01")}, "signing_date"),
    ({"contract": contract(unknown_field=1)}, "unknown_field"),
    ({"contractor_signer_email": None}, "contractor_signer_email"),
    ({"fill_ins": {"[[NOT: in this contract]]": "x"}}, "fill_ins"),
])
def test_bad_input_is_refused_before_anything_is_sent(owner, platform, fake, over, field):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    before = len(fake.api_calls)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid, **over))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == field
    assert len(fake.api_calls) == before


def test_unfilled_placeholders_and_problems_block_sending(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid, fill_ins={}))
    assert r.status_code == 422
    assert r.json()["detail"]["unfilled_placeholders"] == [ADDITIONAL_TERMS]
    early = contract(start_date=today().isoformat())             # inside the cancellation period
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid, contract=early))
    assert r.status_code == 422 and any("cancellation period" in p for p in r.json()["detail"]["problems"])
    r = owner.post("/api/integrations/docusign/envelopes",
                   json=send_body(pid, fill_ins={ADDITIONAL_TERMS: "see [[other]] terms"}))
    assert r.status_code == 422
    assert fake.envelopes == {}


# ---------------------------------------------------------------------------- sending
def test_send_creates_the_envelope_as_documented(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 201, r.text
    out = r.json()
    eid = out["envelope"]["envelope_id"]
    call = next(c for c in fake.api_calls if c.method == "POST")
    assert str(call.url) == f"https://demo.docusign.net/restapi/v2.1/accounts/{ACCT_DEFAULT}/envelopes"
    assert call.headers["authorization"] == "Bearer ds-at-1"
    body = json.loads(call.content)
    assert body["status"] == "sent" and len(body["emailSubject"]) <= 100
    assert body["emailSubject"] == "Home improvement contract: 9 Oak Ave, Fresno, CA 93704"
    assert "right to cancel within 3 business days" in body["emailBlurb"]
    assert body["documents"][0]["fileExtension"] == "html" and body["documents"][0]["documentId"] == "1"
    assert "eventNotification" not in body                       # no Connect key saved
    signers = body["recipients"]["signers"]
    assert [(s["name"], s["email"], s["recipientId"], s["routingOrder"]) for s in signers] == [
        ("Alex Builder", "alex@acme.test", "1", "1"), ("Pat Homeowner", "pat@home.test", "2", "2"),
        ("Sam Homeowner", "sam@home.test", "3", "2")]
    page = fake.envelopes[eid]["html"]
    anchors = {}
    for s in signers:
        assert set(s["tabs"]) == {"signHereTabs", "dateSignedTabs"}
        for kind in ("signHereTabs", "dateSignedTabs"):
            tab = s["tabs"][kind][0]
            assert tab["anchorUnits"] == "pixels" and tab["anchorIgnoreIfNotPresent"] == "false"
            assert f'<span style="color:#ffffff">{tab["anchorString"]}</span>' in page
            anchors[(s["recipientId"], kind)] = tab["anchorString"]
    assert len(set(anchors.values())) == len(anchors)               # one anchor per recipient and tab
    assert page.count(anchors[("2", "signHereTabs")]) == 2           # signatures + right-to-cancel acknowledgment
    assert page.count(anchors[("1", "signHereTabs")]) == 1
    contract_part, _, attachments = page.partition("Attachment A-1")
    assert anchors[("2", "signHereTabs")] not in attachments          # the Notice of Cancellation stays unsigned
    assert "Three-Day Right to Cancel" in contract_part and attachments.count("I hereby cancel this transaction.") == 2
    # stored on the project, contract saved as a document
    rec = platform.store.get("projects", owner.tenant_id, pid)["extra"]["docusign"]["envelopes"][0]
    assert rec["envelope_id"] == eid and rec["status"] == "sent" and rec["account_id"] == ACCT_DEFAULT
    assert rec["signing_date"] == today().isoformat() and rec["cancellation_kind"] == "three_day"
    assert rec["document_sha256"] == hashlib.sha256(page.encode()).hexdigest()
    assert [s["email"] for s in rec["signers"]] == ["a***@acme.test", "p***@home.test", "s***@home.test"]
    doc = platform.store.get("documents", owner.tenant_id, out["document_id"])
    assert doc["project_id"] == pid and doc["kind"] == "contract"
    assert "Three-Day Right to Cancel" in doc["body_markdown"]
    assert "\x02" not in doc["body_markdown"] and "[[" not in doc["body_markdown"]
    listed = owner.get(f"/api/integrations/docusign/projects/{pid}/envelopes").json()["envelopes"]
    assert [e["envelope_id"] for e in listed] == [eid]
    rows = owner.get("/api/audit").json()
    sent = [r for r in rows if r["tool"] == "integrations.docusign_send"]
    assert len(sent) == 1 and "pat@home.test" not in json.dumps(rows) and eid in json.dumps(sent)


def test_contractor_defaults_come_from_settings(owner, platform, fake):
    connect(owner, fake)
    owner.put("/api/integrations/docusign/config", json={"config": {"contractor_signer_name": "Office Signer",
                                                                     "contractor_signer_email": "sign@acme.test"}})
    pid = new_project(platform, owner.tenant_id)
    body = send_body(pid)
    body.pop("contractor_signer_name"), body.pop("contractor_signer_email")
    r = owner.post("/api/integrations/docusign/envelopes", json=body)
    assert r.status_code == 201, r.text
    signer = json.loads(next(c for c in fake.api_calls if c.method == "POST").content)["recipients"]["signers"][0]
    assert (signer["name"], signer["email"]) == ("Office Signer", "sign@acme.test")


def test_arbitration_clause_gets_initial_tabs(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    arb = ("[[ATTORNEY: arbitration provision: scope of disputes, provider and rules, location, costs, whether "
           "discovery and appeal rights are kept.]]")
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(
        pid, contract=contract(include_arbitration_clause=True), fill_ins={**FILL, arb: "Arbitration by AAA rules."}))
    assert r.status_code == 201, r.text
    signers = json.loads(next(c for c in fake.api_calls if c.method == "POST").content)["recipients"]["signers"]
    assert all("initialHereTabs" in s["tabs"] for s in signers)


def test_project_of_another_tenant_is_not_found(app, platform, fake):
    with TestClient(app) as a, browser(a) as b:
        tid_a = signup(a)["tenant_id"]
        signup(b, email="owner@other.test", company="Other Co")
        connect(b, fake, code="code-2")
        pid_a = new_project(platform, tid_a)
        r = b.post("/api/integrations/docusign/envelopes", json=send_body(pid_a))
        assert r.status_code == 404
        assert fake.envelopes == {}


def test_send_needs_a_connection(owner, platform, fake):
    pid = new_project(platform, owner.tenant_id)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 409 and "Connect Docusign" in r.json()["detail"]


def test_rate_limit_429_pauses_the_account(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    reset = int(time.time()) + 1200
    fake.scripted.append(lambda req: json_response(
        429, {"errorCode": "HOURLY_APIINVOCATION_LIMIT_EXCEEDED", "message": "limit"},
        {"X-RateLimit-Limit": "3000", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)})
        if req.method == "POST" and req.url.path.endswith("/envelopes") else None)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 429 and 1100 < int(r.headers["retry-after"]) <= 1201
    calls = len(fake.api_calls)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 429 and len(fake.api_calls) == calls       # paused: Docusign isn't called again
    extra = platform.store.get("projects", owner.tenant_id, pid)["extra"] or {}
    assert not extra.get("docusign", {}).get("envelopes")             # nothing recorded for a refused send


def test_remaining_zero_header_pauses_before_the_next_call(owner, platform, fake):
    connect(owner, fake)
    fake.remaining = 1                              # the next answer says 0 remaining until the reset
    assert owner.post("/api/integrations/docusign/test").json()["ok"] is True
    calls = len(fake.api_calls)
    r = owner.post("/api/integrations/docusign/test").json()
    assert r["ok"] is False and "API limit" in r["message"] and len(fake.api_calls) == calls


def test_401_refreshes_once_then_reconnect(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    fake.access.pop("ds-at-1")                      # revoked early at Docusign
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 201, r.text             # refreshed (ds-at-2) and retried
    assert fake.token_requests[-1]["form"]["grant_type"] == "refresh_token"
    fake.access.clear()
    fake.refresh_tokens.clear()
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    assert platform.store.get_connection(owner.tenant_id, "docusign")["status"] == "error"


# ---------------------------------------------------------------------------- status polling
def test_polling_respects_the_20_minute_rule_and_records_completion(owner, platform, fake, monkeypatch):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    gets = lambda: [c for c in fake.api_calls if c.method == "GET" and c.url.path.endswith(eid)]   # noqa: E731
    r = owner.get(f"/api/integrations/docusign/envelopes/{eid}").json()
    assert r["checked"] is True and r["status"] == "sent" and len(gets()) == 1
    assert str(gets()[0].url) == f"https://demo.docusign.net/restapi/v2.1/accounts/{ACCT_DEFAULT}/envelopes/{eid}"
    r = owner.get(f"/api/integrations/docusign/envelopes/{eid}").json()
    assert r["checked"] is False and len(gets()) == 1 and r["next_check_after"]          # not again within 20 min
    signed_at = datetime.now(timezone.utc)
    fake.complete(eid, signed_at)
    real = time.time()
    monkeypatch.setattr(docusign, "_now", lambda: real + 21 * 60)
    r = owner.get(f"/api/integrations/docusign/envelopes/{eid}").json()
    assert r["checked"] is True and r["status"] == "completed" and len(gets()) == 2
    received = docusign._ca_date(signed_at).isoformat()
    assert r["cancellation"]["received_signed_copy_on"] == received
    assert r["cancellation"]["kind"] == "three_day" and r["cancellation"]["last_day_to_cancel"] > received
    assert r["notice_dates_match"] is (received == r["signing_date"])
    monkeypatch.setattr(docusign, "_now", lambda: real + 60 * 60)
    r = owner.get(f"/api/integrations/docusign/envelopes/{eid}").json()
    assert r["checked"] is False and len(gets()) == 2                                    # completed: never again


def test_completion_on_a_later_day_asks_for_a_corrected_notice(owner, platform, fake, monkeypatch):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    later = datetime.now(timezone.utc) + timedelta(days=3)
    fake.complete(eid, later)
    real = time.time()
    monkeypatch.setattr(docusign, "_now", lambda: real + 3 * 86400)
    r = owner.get(f"/api/integrations/docusign/envelopes/{eid}").json()
    assert r["status"] == "completed" and r["notice_dates_match"] is False
    assert "corrected Notice of Cancellation" in r["warning"]


def test_deadline_event_reaches_a_webhook_after_completion(owner, platform, fake, monkeypatch):
    connect(owner, fake)
    hook = owner.post("/api/integrations/webhooks", json={"name": "ops", "url": "https://hooks.example.com/tc",
                                                          "kind": "generic", "events": ["deadline_due_soon",
                                                                                        "contract_generated"]})
    assert hook.status_code == 201, hook.text
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    fake.complete(eid, datetime.now(timezone.utc))
    real = time.time()
    monkeypatch.setattr(docusign, "_now", lambda: real + 21 * 60)
    owner.get(f"/api/integrations/docusign/envelopes/{eid}")
    rows = platform.store.list_outbound(owner.tenant_id)
    events = sorted(r["event"] for r in rows)
    assert events == ["contract_generated", "deadline_due_soon"]
    due = next(r for r in rows if r["event"] == "deadline_due_soon")["payload"]
    assert due["envelope_id"] == eid and due["due_date"] and due["project_id"] == pid


# ---------------------------------------------------------------------------- Connect webhook
def test_connect_url_and_hmac_verified_events(owner, platform, fake):
    connect(owner, fake)
    save_hmac_key(owner)
    pid = new_project(platform, owner.tenant_id)
    r = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid))
    assert r.status_code == 201, r.text
    eid = r.json()["envelope"]["envelope_id"]
    body = json.loads(next(c for c in fake.api_calls if c.method == "POST").content)
    note = body["eventNotification"]
    assert note["url"] == APP_URL + listener_path(platform, owner.tenant_id)
    assert (note["requireAcknowledgment"], note["includeHMAC"], note["loggingEnabled"]) == ("true", "true", "true")
    assert note["events"] == ["envelope-sent", "envelope-delivered", "envelope-completed", "envelope-declined",
                              "envelope-voided"]
    assert note["eventData"] == {"version": "restv2.1", "format": "json"}
    assert HMAC_KEY not in json.dumps(body)
    path = listener_path(platform, owner.tenant_id)
    with browser(owner) as docusign_side:                     # no cookies: Docusign calls the URL itself
        msg = sim("envelope-delivered", eid)
        r = docusign_side.post(path, content=msg, headers={"content-type": "application/json",
                                                           "X-DocuSign-Signature-1": sign(msg)})
        assert r.status_code == 200 and r.json()["result"] == "applied"
        done = sim("envelope-completed", eid)
        r = docusign_side.post(path, content=done, headers={"X-DocuSign-Signature-1": sign(done, "old-key"),
                                                            "X-DocuSign-Signature-2": sign(done)})   # second key
        assert r.status_code == 200 and r.json()["result"] == "applied"
        late = sim("envelope-sent", eid)                      # a retried earlier event can't undo completion
        r = docusign_side.post(path, content=late, headers={"X-DocuSign-Signature-1": sign(late)})
        assert r.status_code == 200
    rec = owner.get(f"/api/integrations/docusign/envelopes/{eid}?refresh=false").json()
    assert rec["status"] == "completed" and rec["source"] == "connect" and rec["cancellation"]["last_day_to_cancel"]
    assert [e["event"] for e in rec["events"]] == ["envelope-delivered", "envelope-completed", "envelope-sent"]
    gets = [c for c in fake.api_calls if c.method == "GET" and c.url.path.endswith(eid)]
    assert gets == []                                          # Connect made polling unnecessary


def test_connect_refuses_bad_signatures_and_unknown_urls(owner, platform, fake):
    connect(owner, fake)
    save_hmac_key(owner)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    path = listener_path(platform, owner.tenant_id)
    msg = sim("envelope-completed", eid)
    with browser(owner) as other:
        assert other.post(path, content=msg).status_code == 401                                  # unsigned
        assert other.post(path, content=msg, headers={"X-DocuSign-Signature-1": sign(msg, "guess")}).status_code == 401
        tampered = msg.replace(b"envelope-completed", b"envelope-voided")
        assert other.post(path, content=tampered, headers={"X-DocuSign-Signature-1": sign(msg)}).status_code == 401
        wrong_tag = path[:-64] + "0" * 64
        assert other.post(wrong_tag, content=msg, headers={"X-DocuSign-Signature-1": sign(msg)}).status_code == 404
        unknown = "/api/integrations/docusign/connect-listener/t_0000000000000000/" + \
            docusign.connect_tag(platform, "t_0000000000000000")
        assert other.post(unknown, content=msg, headers={"X-DocuSign-Signature-1": sign(msg)}).status_code == 404
        r = other.post(path, content=b"not json", headers={"X-DocuSign-Signature-1": sign(b"not json")})
        assert r.status_code == 200 and r.json()["result"].startswith("ignored")
        foreign = sim("envelope-completed", eid, account=ACCT_OTHER)
        r = other.post(path, content=foreign, headers={"X-DocuSign-Signature-1": sign(foreign)})
        assert r.json()["result"] == "ignored: another account"
    assert owner.get(f"/api/integrations/docusign/envelopes/{eid}?refresh=false").json()["status"] == "sent"


def test_connect_without_a_saved_key_is_refused(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    msg = sim("envelope-completed", eid)
    with browser(owner) as other:
        r = other.post(listener_path(platform, owner.tenant_id), content=msg,
                       headers={"X-DocuSign-Signature-1": sign(msg)})
    assert r.status_code == 401


def test_connect_messages_stay_inside_their_tenant(app, platform, fake):
    with TestClient(app) as a, browser(a) as b:
        tid_a = signup(a)["tenant_id"]
        tid_b = signup(b, email="owner@other.test", company="Other Co")["tenant_id"]
        connect(a, fake, code="code-1")
        connect(b, fake, code="code-2")
        save_hmac_key(a)
        save_hmac_key(b, "b-tenant-key")
        pid = new_project(platform, tid_a)
        eid = a.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
        msg = sim("envelope-completed", eid)
        # tenant B's own URL and key, carrying tenant A's envelope id: B has no such envelope
        r = b.post(listener_path(platform, tid_b), content=msg, headers={"X-DocuSign-Signature-1": sign(msg,
                                                                                                       "b-tenant-key")})
        assert r.status_code == 200 and r.json()["result"] == "ignored: unknown envelope"
        # tenant A's URL signed with B's key
        r = b.post(listener_path(platform, tid_a), content=msg, headers={"X-DocuSign-Signature-1": sign(msg,
                                                                                                       "b-tenant-key")})
        assert r.status_code == 401
        assert a.get(f"/api/integrations/docusign/envelopes/{eid}?refresh=false").json()["status"] == "sent"
        assert b.get(f"/api/integrations/docusign/envelopes/{eid}?refresh=false").status_code == 404
        assert b.get(f"/api/integrations/docusign/projects/{pid}/envelopes").status_code == 404


# ---------------------------------------------------------------------------- roles, helpers, logs
def test_field_role_cannot_send_or_poll(app, platform, fake):
    with TestClient(app) as owner_c, browser(owner_c) as crew:
        tid = signup(owner_c)["tenant_id"]
        connect(owner_c, fake)
        join(crew, owner_c, "crew@acme.test", "field")
        pid = new_project(platform, tid)
        eid = owner_c.post("/api/integrations/docusign/envelopes",
                           json=send_body(pid)).json()["envelope"]["envelope_id"]
        assert crew.post("/api/integrations/docusign/envelopes", json=send_body(pid)).status_code == 403
        assert crew.post("/api/integrations/docusign/preview", json=send_body(pid)).status_code == 403
        assert crew.get(f"/api/integrations/docusign/envelopes/{eid}").status_code == 403
        assert len(fake.envelopes) == 1


def test_parse_time_and_hmac_helper():
    assert parse_time("2023-04-06T19:38:30.6530000Z") == datetime(2023, 4, 6, 19, 38, 30, 653000, timezone.utc)
    assert parse_time("2023-04-06T19:38:30") == datetime(2023, 4, 6, 19, 38, 30, tzinfo=timezone.utc)
    assert parse_time("2023-04-06T12:38:30-07:00") == datetime(2023, 4, 6, 19, 38, 30, tzinfo=timezone.utc)
    assert parse_time("yesterday") is None and parse_time(None) is None
    body = b'{"event":"envelope-sent"}\r\n'
    assert verify_connect_hmac(HMAC_KEY, body, {"x-docusign-signature-3": sign(body)})
    assert verify_connect_hmac('"' + HMAC_KEY + '"', body, {"x-docusign-signature-1": sign(body)})   # quotes removed
    assert not verify_connect_hmac(HMAC_KEY, body + b" ", {"x-docusign-signature-1": sign(body)})
    assert not verify_connect_hmac("", body, {"x-docusign-signature-1": sign(body, "")})


def test_logs_never_hold_tokens_secrets_or_emails(owner, platform, fake, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    connect(owner, fake)
    save_hmac_key(owner)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    msg = sim("envelope-completed", eid)
    with browser(owner) as other:
        other.post(listener_path(platform, owner.tenant_id), content=msg, headers={"X-DocuSign-Signature-1": sign(msg)})
        other.post(listener_path(platform, owner.tenant_id), content=msg, headers={"X-DocuSign-Signature-1": "bad"})
    text = caplog.text
    for secret in ("ds-at-1", "ds-rt-1", SECRET, HMAC_KEY, sign(msg), "pat@home.test", "sam@home.test",
                   base64.b64encode(f"{IK}:{SECRET}".encode()).decode()):
        assert secret not in text, secret
    assert eid in text                              # the envelope id is logged so a lost response can be traced


def test_store_isolated_find_envelope(owner, platform, fake):
    connect(owner, fake)
    pid = new_project(platform, owner.tenant_id)
    eid = owner.post("/api/integrations/docusign/envelopes", json=send_body(pid)).json()["envelope"]["envelope_id"]
    assert inst(platform, owner.tenant_id).find_envelope(eid)[0] == pid
    assert inst(platform, "t_ffffffffffffffff").find_envelope(eid) is None
    assert inst(platform, owner.tenant_id).find_envelope("../../etc") is None
    with pytest.raises(NotConnected):
        inst(platform, "t_ffffffffffffffff").send_contract(project_id=pid, contract=contract(), buyer_emails=[])
