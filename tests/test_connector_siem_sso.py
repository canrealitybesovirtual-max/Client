"""Splunk HEC export (app/connectors/splunk.py) and enterprise OIDC SSO (app/sso.py).

Every vendor call is answered by fakes that copy the documented shapes:
- Splunk HTTP Event Collector: POST /services/collector/event, "Authorization: Splunk <token>", stacked JSON event
  objects; answers {"text":"Success","code":0} and the documented error table (e.g. 403 {"text":"Invalid
  token","code":4}, 400 {"text":"Incorrect index","code":7}, 503 {"text":"Server is busy","code":9}).
- OpenID Providers: a discovery document, a JWKS, a token endpoint that checks the PKCE verifier and the client
  authentication, a UserInfo endpoint; ID tokens are signed here with throwaway keys generated for the test run.
  Okta (org authorization server paths /oauth2/v1/...), Microsoft Entra ID (tenant-specific v2.0 issuer, email +
  xms_edov) and Google (the discovery example in Google's OpenID Connect reference) are modelled.
No credential here is real.
"""
import base64
import csv
import hashlib
import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import sso
from app.connectors import splunk
from app.connectors.splunk import SplunkHECConnector
from tests.support import PW, backend, json_response, join, login, make_app, mock_http, redirect_query, signup  # noqa: F401,E501

APP_URL = "https://tc.example.com"
HEC_HOST = "http-inputs-acme.splunkcloud.com"
HEC_URL = f"https://{HEC_HOST}"
HEC_TOKEN = "12345678-1234-1234-1234-1234567890AB"         # the placeholder from Splunk's own docs examples
CLIENT_ID = "0oa1testclientid"
CLIENT_SECRET = "test-client-secret-not-real-0123456789abcdef"
OKTA = "https://acme.okta.com"
ENTRA_TID = "aaaabbbb-0000-cccc-1111-dddd2222eeee"
ENTRA = f"https://login.microsoftonline.com/{ENTRA_TID}/v2.0"
GOOGLE = "https://accounts.google.com"
KEY1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
LATER = lambda: datetime.now(timezone.utc) + timedelta(minutes=5)          # past the export's settle window


def stacked(body: bytes) -> list[dict]:
    """HEC's batch format: event objects one after another."""
    text, dec, i, out = body.decode(), json.JSONDecoder(), 0, []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        out.append(obj)
    return out


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_jwk(key, kid: str, alg: str = "RS256") -> dict:
    if isinstance(key, rsa.RSAPrivateKey):
        d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    else:
        d = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "use": "sig", "alg": alg}


# ---------------------------------------------------------------------------- fake Splunk HEC
class FakeHEC:
    def __init__(self):
        self.posts: list[dict] = []          # {"events", "auth", "url"}
        self.events: list[dict] = []
        self.queue: list[tuple[int, dict]] = []
        self.reject = None                   # event -> True to answer "Invalid data format"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/services/collector/event"
        events = stacked(request.content)
        self.posts.append({"events": events, "auth": request.headers.get("authorization"), "url": str(request.url)})
        if self.queue:
            status, body = self.queue.pop(0)
            if status == 0:
                raise httpx.ConnectError("connection refused", request=request)
            return json_response(status, body)
        if not request.headers.get("authorization"):
            return json_response(401, {"text": "Token is required", "code": 2})
        if request.headers["authorization"] != f"Splunk {HEC_TOKEN}":
            return json_response(403, {"text": "Invalid token", "code": 4})
        if self.reject and any(self.reject(e) for e in events):
            return json_response(400, {"text": "Invalid data format", "code": 6, "invalid-event-number": 0})
        self.events.extend(events)
        return json_response(200, {"text": "Success", "code": 0})


# ---------------------------------------------------------------------------- fake OpenID Providers
class FakeIdP:
    def __init__(self, issuer: str, kind: str = "okta"):
        self.issuer, self.kind = issuer, kind
        self.host = urlsplit(issuer).hostname
        self.client_id, self.client_secret = CLIENT_ID, CLIENT_SECRET
        if kind == "okta":           # Okta org authorization server
            p = issuer
            self.doc = {"issuer": issuer, "authorization_endpoint": f"{p}/oauth2/v1/authorize",
                        "token_endpoint": f"{p}/oauth2/v1/token", "userinfo_endpoint": f"{p}/oauth2/v1/userinfo",
                        "jwks_uri": f"{p}/oauth2/v1/keys", "response_types_supported": ["code", "id_token",
                                                                                         "code id_token"],
                        "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                        "scopes_supported": ["openid", "email", "profile", "groups", "offline_access"],
                        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post",
                                                                  "client_secret_jwt", "private_key_jwt", "none"],
                        "code_challenge_methods_supported": ["S256"]}
        elif kind == "entra":
            base = f"https://login.microsoftonline.com/{ENTRA_TID}"
            self.doc = {"issuer": issuer, "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                        "token_endpoint": f"{base}/oauth2/v2.0/token",
                        "userinfo_endpoint": "https://graph.microsoft.com/oidc/userinfo",
                        "jwks_uri": f"{base}/discovery/v2.0/keys",
                        "response_types_supported": ["code", "id_token", "code id_token", "id_token token"],
                        "subject_types_supported": ["pairwise"], "id_token_signing_alg_values_supported": ["RS256"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post", "private_key_jwt",
                                                                  "client_secret_basic"],
                        "scopes_supported": ["openid", "profile", "email", "offline_access"]}
        else:                        # Google: the example document in Google's OpenID Connect reference
            self.doc = {"issuer": "https://accounts.google.com",
                        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                        "device_authorization_endpoint": "https://oauth2.googleapis.com/device/code",
                        "token_endpoint": "https://oauth2.googleapis.com/token",
                        "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
                        "revocation_endpoint": "https://oauth2.googleapis.com/revoke",
                        "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                        "response_types_supported": ["code", "token", "id_token"],
                        "response_modes_supported": ["query", "fragment", "form_post"],
                        "authorization_response_iss_parameter_supported": True,
                        "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                        "scopes_supported": ["openid", "email", "profile"],
                        "claims_supported": ["iss", "aud", "sub", "email", "email_verified", "name", "picture",
                                             "given_name", "family_name", "iat", "exp"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
                        "code_challenge_methods_supported": ["plain", "S256"],
                        "grant_types_supported": ["authorization_code", "refresh_token",
                                                  "urn:ietf:params:oauth:grant-type:device_code"]}
        self.keys = [public_jwk(KEY1, "k1")]
        self.sign_key, self.kid, self.alg = KEY1, "k1", "RS256"
        self.codes: dict[str, dict] = {}
        self.userinfo_body: dict | None = None
        self.token_requests: list[httpx.Request] = []
        self.jwks_fetches = 0
        self.discovery_fetches = 0
        self.raw_id_token: str | None = None

    def handles(self, request: httpx.Request) -> bool:
        return str(request.url).split("?")[0] in {self.issuer.rstrip("/") + "/.well-known/openid-configuration",
                                                  self.doc["jwks_uri"], self.doc["token_endpoint"],
                                                  self.doc.get("userinfo_endpoint")}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == self.issuer.rstrip("/") + "/.well-known/openid-configuration":
            self.discovery_fetches += 1
            return json_response(200, self.doc)
        if url == self.doc["jwks_uri"]:
            self.jwks_fetches += 1
            return json_response(200, {"keys": self.keys}, {"cache-control": "public, max-age=3600"})
        if url == self.doc["token_endpoint"]:
            return self.token(request)
        if url == self.doc.get("userinfo_endpoint"):
            assert request.headers.get("authorization", "").startswith("Bearer at-")
            return json_response(200, self.userinfo_body or {})
        return json_response(404, {"error": "not_found"})

    def authorize(self, q: dict, email: str | None, **claims) -> str:
        """The provider's sign-in page: the user signs in, the provider redirects back with a code."""
        assert q["response_type"] == "code" and q["client_id"] == self.client_id
        assert q["scope"] == "openid email profile" and q["code_challenge_method"] == "S256"
        assert len(q["nonce"]) >= 22 and q["redirect_uri"] == APP_URL + "/api/auth/sso/callback"
        code = "code-" + hashlib.sha256(json.dumps([q["state"], email]).encode()).hexdigest()[:16]
        self.codes[code] = {"q": q, "email": email, "claims": claims}
        return code

    def mint(self, claims: dict) -> str:
        if self.raw_id_token:
            return self.raw_id_token
        return jwt.encode(claims, self.sign_key, algorithm=self.alg,
                          headers={"kid": self.kid} if self.kid else None)

    def token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append(request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
            cid, secret = unquote(cid), unquote(secret)
        else:
            cid, secret = form.get("client_id"), form.get("client_secret")
        if (cid, secret) != (self.client_id, self.client_secret):
            return json_response(401, {"error": "invalid_client"})
        pending = self.codes.pop(form.get("code"), None)
        if form.get("grant_type") != "authorization_code" or not pending:
            return json_response(400, {"error": "invalid_grant"})
        q = pending["q"]
        verifier = form.get("code_verifier") or ""
        if b64url(hashlib.sha256(verifier.encode()).digest()) != q["code_challenge"] or \
                form.get("redirect_uri") != q["redirect_uri"]:
            return json_response(400, {"error": "invalid_grant"})
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": "sub-" + hashlib.sha256((pending["email"] or "").encode()).hexdigest()[:12],
                  "aud": self.client_id, "iat": now, "exp": now + 3600, "nonce": q["nonce"]}
        if pending["email"]:
            claims.update(email=pending["email"], email_verified=True)
        for k, v in pending["claims"].items():
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        return json_response(200, {"token_type": "Bearer", "expires_in": 3600, "access_token": "at-" + now.__str__(),
                                   "scope": "openid email profile", "id_token": self.mint(claims)})


class World:
    """Routes every outbound call to the fake that owns the URL."""

    def __init__(self):
        self.hec = FakeHEC()
        self.idps: list[FakeIdP] = []

    def add(self, idp: FakeIdP) -> FakeIdP:
        self.idps.append(idp)
        return idp

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/collector/event":
            return self.hec(request)
        for idp in self.idps:
            if idp.handles(request):
                return idp(request)
        return json_response(404, {"error": "not found"})


# ---------------------------------------------------------------------------- fixtures
def mount(app):
    """What app/main.py needs: include splunk.router and sso.router before the catch-all MCP mount at '/'."""
    app.include_router(splunk.router)
    app.include_router(sso.router)
    routes = app.router.routes
    catch_all = [r for r in routes if isinstance(r, Mount) and r.path == ""]
    for r in catch_all:
        routes.remove(r)
    routes.extend(catch_all)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(splunk, "_sleep", slept.append)
    return slept


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=APP_URL)
    mount(a)
    a.state.platform.env = {}
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def world(platform):
    w = World()
    w.seen = mock_http(platform, w)
    return w


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c, email="olivia@acme.com", company="Acme Builders")["tenant_id"]
        yield c


def invite(owner, email: str, role: str = "office") -> TestClient:
    c = TestClient(owner.app)
    join(c, owner, email, role)          # they set the password PW through the invite link
    return c


def other_company(app, email="bob@acme.com", company="Other Co") -> TestClient:
    c = TestClient(app)
    c.tenant_id = signup(c, email=email, company=company, license_number="654321")["tenant_id"]
    return c


def connect_splunk(c, **config) -> dict:
    body = {"hec_url": HEC_URL, "hec_token": HEC_TOKEN, **config}
    r = c.post("/api/integrations/splunk/connect", json={"config": body})
    assert r.status_code == 200, r.text
    return r.json()


def configure_sso(c, issuer=OKTA, **extra) -> dict:
    body = {"issuer": issuer, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "allowed_domains": ["acme.com"], **extra}
    r = c.put("/api/sso/config", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def start(c, email: str) -> dict:
    r = c.post("/api/auth/sso/start", json={"email": email})
    assert r.status_code == 200, r.text
    u = urlsplit(r.json()["authorize_url"])
    return {"url": f"{u.scheme}://{u.netloc}{u.path}", **{k: v[0] for k, v in parse_qs(u.query).items()}}


def sso_login(app, idp: FakeIdP, email: str, client: TestClient | None = None, claim_email: str | None = "same",
              extra_query: str = "", **claims):
    c = client or TestClient(app)
    q = start(c, email)
    code = idp.authorize(q, email if claim_email == "same" else claim_email, **claims)
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}{extra_query}", follow_redirects=False)
    return c, r


def error_of(r) -> str:
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith("/#/login?sso=error"), loc
    return redirect_query(r)["message"]


def audit_rows(platform, tenant_id, tool):
    return [a for a in platform.store.list_audit(tenant_id, 1000) if a["tool"] == tool]


# ============================================================================ Splunk HEC
def test_splunk_needs_no_operator_settings_and_lists_cleanly(owner, platform):
    desc = SplunkHECConnector.describe({})
    assert desc["operator_configured"] is True and SplunkHECConnector.missing_env({}) == []
    view = next(p for p in owner.get("/api/integrations").json()["providers"] if p["provider"] == "splunk")
    assert view["operator_configured"] is True and view["missing_env"] == [] and view["catalog_id"] == "splunk"
    assert view["connection"]["status"] == "disconnected"
    assert splunk.start_exporter(type("P", (), {"env": {"SPLUNK_EXPORT": "0"}})()) is None


def test_connect_sends_one_test_event_with_the_documented_header_and_keeps_the_token_secret(owner, platform, world):
    out = connect_splunk(owner, index="tc_audit")
    assert out["status"] == "connected" and out["external_account"] == HEC_HOST
    post = world.hec.posts[0]
    assert post["auth"] == f"Splunk {HEC_TOKEN}" and post["url"] == f"{HEC_URL}/services/collector/event"
    ev = post["events"][0]
    assert ev["sourcetype"] == "tradecomply:test" and ev["index"] == "tc_audit" and isinstance(ev["time"], float)
    assert ev["event"]["tc_id"].startswith("test:") and ev["host"] == "tc.example.com"
    detail = owner.get("/api/integrations/splunk").json()
    assert detail["secrets_saved"] == ["hec_token"] and HEC_TOKEN not in json.dumps(detail)
    blob = platform.store.get_connection(owner.tenant_id, "splunk", with_secret=True)["secret_blob"]
    assert blob.startswith("v1.") and HEC_TOKEN not in blob


def test_refused_token_and_index_set_the_connection_to_error_with_the_reason(owner, world):
    world.hec.queue.append((403, {"text": "Invalid token", "code": 4}))
    out = connect_splunk(owner)
    assert out["status"] == "error" and "didn't accept the HEC token" in out["last_error"]
    assert "Invalid token, code 4" in out["last_error"]
    world.hec.queue.append((400, {"text": "Incorrect index", "code": 7}))
    out = connect_splunk(owner, index="nope")
    assert out["status"] == "error" and "refused the index “nope”" in out["last_error"]
    world.hec.queue.append((400, {"text": "Data channel is missing", "code": 10}))
    out = connect_splunk(owner, index=None)
    assert out["status"] == "error" and "indexer acknowledgment" in out["last_error"]


@pytest.mark.parametrize("url,stored", [
    ("https://http-inputs-acme.splunkcloud.com/services/collector/event", HEC_URL),
    ("https://splunk.example.com:8088", "https://splunk.example.com:8088"),
    ("https://http-inputs.acme.splunkcloud.com:443/", "https://http-inputs.acme.splunkcloud.com"),
])
def test_hec_url_is_normalized(url, stored):
    assert splunk.normalize_hec_url(url) == stored


@pytest.mark.parametrize("url", ["http://splunk.example.com:8088", "https://splunk.example.com:22",
                                 "https://10.0.0.5:8088", "https://x:y@splunk.example.com",
                                 "https://splunk.example.com/en-US/app/search", "https://splunk.example.com/?token=x",
                                 "https://localhost:8088"])
def test_bad_hec_urls_are_refused_when_saved(owner, url):
    r = owner.put("/api/integrations/splunk/config", json={"config": {"hec_url": url, "hec_token": HEC_TOKEN}})
    assert r.status_code == 422, (url, r.text)


def test_port_8088_is_used_and_a_private_address_is_refused_at_send_time(owner, platform):
    hec = FakeHEC()
    seen = mock_http(platform, hec, hosts={"splunk.corp-example.com": "10.1.2.3"})
    out = connect_splunk(owner, hec_url="https://splunk.example.com:8088")
    assert out["status"] == "connected" and seen[0].url.port == 8088
    out = connect_splunk(owner, hec_url="https://splunk.corp-example.com:8088")
    assert out["status"] == "error" and "private or reserved address" in out["last_error"]
    assert len(seen) == 1


def seed_logs(platform, tenant_id, n_activity=3, n_mcp=2, tool="check_payroll"):
    for i in range(n_activity):
        platform.store.audit(tenant_id, "olivia@acme.com", tool, {"i": i}, i % 2 == 0, ok=True, source="api")
    for i in range(n_mcp):
        platform.store.log_mcp_call(tenant_id, "verify_contractor_license", "ok", api_key_id=7,
                                    args_sha256=hashlib.sha256(str(i).encode()).hexdigest(), duration_ms=12,
                                    ip="203.0.113.9")


def tc_ids(store, tenant_id):
    act = {f"activity:{a['id']}" for a in store.list_audit(tenant_id, 1000)}
    mcp = {f"mcp:{m['id']}" for m in store.list_mcp_audit(tenant_id, limit=1000)["items"]}
    return act, mcp


def test_export_sends_both_logs_of_this_company_only_then_only_new_rows(owner, app, platform, world):
    other = other_company(app)
    seed_logs(platform, other.tenant_id, 4, 4, tool="other_company_tool")
    seed_logs(platform, owner.tenant_id)
    connect_splunk(owner)
    world.hec.events.clear()
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["sent"] > 0 and "error" not in r
    got = {e["event"]["tc_id"] for e in world.hec.events}
    act, mcp = tc_ids(platform.store, owner.tenant_id)
    assert got == act | mcp
    assert all(e["event"]["tenant_id"] == owner.tenant_id for e in world.hec.events)
    assert not any(e["event"].get("tool") == "other_company_tool" for e in world.hec.events)
    one_act = next(e for e in world.hec.events if e["event"]["log"] == "activity" and e["event"]["tool"] ==
                   "check_payroll")
    assert one_act["sourcetype"] == "tradecomply:audit" and one_act["source"] == "tradecomply:activity"
    assert one_act["event"]["company"] == "Acme Builders" and isinstance(one_act["event"]["args"], dict)
    one_mcp = next(e for e in world.hec.events if e["event"]["log"] == "mcp")
    assert one_mcp["sourcetype"] == "tradecomply:mcp_audit" and one_mcp["event"]["ip"] == "203.0.113.9"
    assert one_mcp["event"]["args_sha256"] and "args" not in one_mcp["event"]
    # nothing new: nothing sent; one new row: just that row
    posts = len(world.hec.posts)
    assert splunk.export_tenant(platform, owner.tenant_id, now=LATER())["sent"] == 0 and len(world.hec.posts) == posts
    platform.store.audit(owner.tenant_id, "olivia@acme.com", "prevailing_wage_penalties", {}, True)
    splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert [e["event"]["tool"] for e in world.hec.posts[-1]["events"]] == ["prevailing_wage_penalties"]
    status = owner.get("/api/integrations/splunk/export").json()
    assert status["pending"] == {"activity": 0, "mcp": 0} and status["sent_total"] >= len(got) + 1


def test_rows_younger_than_the_settle_window_wait(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 1, 0)
    assert splunk.export_tenant(platform, owner.tenant_id, now=datetime.now(timezone.utc) - timedelta(minutes=1)
                                )["sent"] == 0
    assert splunk.export_tenant(platform, owner.tenant_id, now=LATER())["sent"] > 0


def test_large_backlogs_are_batched(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 230, 0)
    world.hec.posts.clear()
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["batches"] == len(world.hec.posts) >= 3
    assert max(len(p["events"]) for p in world.hec.posts) == splunk.MAX_BATCH_EVENTS
    ids = [e["event"]["id"] for p in world.hec.posts for e in p["events"] if e["event"]["log"] == "activity"]
    assert ids == sorted(ids) and len(ids) == len(set(ids))


def test_without_history_only_new_rows_are_sent(owner, platform, world):
    seed_logs(platform, owner.tenant_id, 3, 3, tool="old_row")
    connect_splunk(owner, send_history=False)
    platform.store.audit(owner.tenant_id, "olivia@acme.com", "new_row", {}, None)
    world.hec.events.clear()
    splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    tools = {e["event"]["tool"] for e in world.hec.events}
    assert "new_row" in tools and "old_row" not in tools


def test_busy_splunk_keeps_the_rows_and_backs_off(owner, platform, world, no_real_sleep):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 2, 0)
    world.hec.queue += [(503, {"text": "Server is busy", "code": 9})] * 3        # first try + 2 SafeHTTP retries
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["sent"] == 0 and r["retry_in_s"] == 60 and "busy" in r["error"]
    assert len(no_real_sleep) == 2
    conn = platform.store.get_connection(owner.tenant_id, "splunk")
    assert conn["status"] == "connected" and "Server is busy, code 9" in conn["last_error"]
    assert splunk.export_tenant(platform, owner.tenant_id, now=LATER())["skipped"].startswith("waiting")
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER(), force=True)
    assert r["sent"] > 0 and platform.store.get_connection(owner.tenant_id, "splunk")["last_error"] is None


def test_an_unreachable_splunk_is_retried_later_with_a_plain_message(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 1, 0)
    world.hec.queue += [(0, {})] * 3
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["sent"] == 0 and "Could not connect to http-inputs-acme.splunkcloud.com" in r["error"]
    assert "None" not in r["error"]
    assert platform.store.get_connection(owner.tenant_id, "splunk")["status"] == "connected"


def test_a_revoked_token_stops_the_export_until_fixed(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 2, 0)
    world.hec.queue.append((403, {"text": "Token disabled", "code": 1}))
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["stopped"] == "auth" and "disabled" in r["error"]
    assert platform.store.get_connection(owner.tenant_id, "splunk")["status"] == "error"
    assert splunk.export_all(platform)["tenants"] == 0          # only connected companies are exported
    assert owner.post("/api/integrations/splunk/test").json()["ok"] is True
    assert platform.store.get_connection(owner.tenant_id, "splunk")["status"] == "connected"
    assert splunk.export_tenant(platform, owner.tenant_id, now=LATER())["sent"] > 0


def test_one_malformed_row_is_skipped_not_blocking(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 3, 0)
    bad = platform.store.list_audit(owner.tenant_id, 1)[0]["id"] - 1
    world.hec.reject = lambda e: e["event"].get("id") == bad and e["event"].get("log") == "activity"
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert r["skipped_rows"] == 1 and "error" not in r
    sent = {e["event"]["id"] for e in world.hec.events if e["event"]["log"] == "activity"}
    assert bad not in sent and bad + 1 in sent


def test_export_all_runs_each_company_once_per_round(owner, app, platform, world):
    other = other_company(app, email="bob@other.com")
    connect_splunk(owner)
    seed_logs(platform, other.tenant_id, 2, 0)
    totals = splunk.export_all(platform, slot=1, now=LATER())
    assert totals["tenants"] == 1 and totals["exported"] == 1 and totals["sent"] > 0
    assert splunk.export_all(platform, slot=1) == {"tenants": 1, "exported": 0, "sent": 0, "failed": 0}


def test_export_now_route_and_roles(owner, platform, world):
    assert owner.post("/api/integrations/splunk/export").status_code == 409
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 1, 0)
    field = invite(owner, "fred@acme.com", "field")
    assert field.get("/api/integrations/splunk/export").status_code == 403
    r = owner.post("/api/integrations/splunk/export")
    assert r.status_code == 200 and "sent" in r.json()
    assert audit_rows(platform, owner.tenant_id, "integrations.splunk_export")


def test_alerts_go_to_splunk_through_the_event_bus(owner, platform, world):
    connect_splunk(owner)
    world.hec.events.clear()
    ids = platform.events.emit(owner.tenant_id, "deadline_due_soon",
                               {"title": "Preliminary notice due", "severity": "warning", "link": "/#/projects/3",
                                "due_date": "2026-12-03"})
    assert ids
    platform.events.drain()
    ev = next(e for e in world.hec.events if e["sourcetype"] == "tradecomply:alert")
    assert ev["event"]["event"] == "deadline_due_soon" and ev["event"]["title"] == "Preliminary notice due"
    assert ev["event"]["link"] == APP_URL + "/#/projects/3" and ev["event"]["tc_id"].startswith("alert:")
    assert ev["source"] == "tradecomply:events"
    # a refused token fails the delivery for good and flags the connection
    world.hec.queue.append((403, {"text": "Invalid token", "code": 4}))
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "Overtime missing"})
    platform.events.drain()
    row = platform.store.list_outbound(owner.tenant_id, limit=1)[0]
    assert row["status"] == "failed" and "HEC token" in row["last_error"]
    assert platform.store.get_connection(owner.tenant_id, "splunk")["status"] == "error"


def test_alerts_can_be_turned_off(owner, platform, world):
    connect_splunk(owner, send_alerts=False)
    world.hec.posts.clear()
    platform.events.emit(owner.tenant_id, "sub_noncompliant", {"title": "COI expired"})
    platform.events.drain()
    assert world.hec.posts == []
    assert platform.store.list_outbound(owner.tenant_id, limit=1)[0]["status"] == "delivered"


def test_the_token_never_reaches_the_log(owner, platform, world, caplog):
    caplog.set_level(logging.DEBUG)
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 2, 1)
    splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    world.hec.queue.append((403, {"text": "Invalid token", "code": 4}))
    splunk.export_tenant(platform, owner.tenant_id, now=LATER(), force=True)
    assert HEC_TOKEN not in caplog.text and "Splunk 1234" not in caplog.text


def test_disconnect_wipes_the_token_and_the_cursor(owner, platform, world):
    connect_splunk(owner)
    seed_logs(platform, owner.tenant_id, 1, 0)
    splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert owner.post("/api/integrations/splunk/disconnect").json()["status"] == "disconnected"
    conn = platform.store.get_connection(owner.tenant_id, "splunk", with_secret=True)
    assert conn["secret_blob"] is None and conn["config"]["hec_url"] == HEC_URL
    assert splunk.export_tenant(platform, owner.tenant_id, now=LATER())["skipped"] == "Splunk is not connected"


# ---------------------------------------------------------------------------- owner download
def test_owner_downloads_the_activity_log_as_csv_with_formulas_neutralized(owner, app, platform):
    other = other_company(app, email="bob@other.com")
    platform.store.audit(other.tenant_id, "bob@other.com", "secret_other_tool", {}, None)
    platform.store.audit(owner.tenant_id, "@evil", "=HYPERLINK(\"https://x.example\")", {"note": "+1"}, False)
    r = owner.get("/api/audit-export?log=activity&format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="tradecomply-activity-log-')
    assert r.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(splunk.ACTIVITY_COLUMNS)
    evil = next(x for x in rows[1:] if "HYPERLINK" in x[3])
    assert evil[2] == "'@evil" and evil[3].startswith("'=") and evil[6] == "false"
    assert "secret_other_tool" not in r.text
    assert audit_rows(platform, owner.tenant_id, "audit.export")


def test_download_json_mcp_log_and_date_range(owner, platform):
    seed_logs(platform, owner.tenant_id, 0, 3)
    r = owner.get("/api/audit-export?log=mcp&format=json")
    body = r.json()
    assert r.status_code == 200 and body["log"] == "mcp" and body["row_count"] == 3 and body["truncated"] is False
    assert set(body["rows"][0]) == set(splunk.MCP_COLUMNS)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    assert owner.get(f"/api/audit-export?log=mcp&format=json&start={future}").json()["row_count"] == 0
    today = datetime.now(timezone.utc).date().isoformat()
    assert owner.get(f"/api/audit-export?log=mcp&format=json&start={today}&end={future}").json()["row_count"] == 3
    assert owner.get("/api/audit-export?start=yesterday").status_code == 422
    assert owner.get(f"/api/audit-export?start={future}&end={today}").status_code == 422
    assert owner.get("/api/audit-export?format=xml").status_code == 422


def test_download_is_for_owners_only(owner, platform):
    office = invite(owner, "pat@acme.com", "office")
    assert office.get("/api/audit-export").status_code == 403
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    assert TestClient(owner.app).get("/api/audit-export", headers={"authorization": f"Bearer {key}"}
                                     ).status_code == 403
    assert TestClient(owner.app).get("/api/audit-export").status_code == 401


def test_csv_safe():
    assert splunk.csv_safe("=1+1") == "'=1+1" and splunk.csv_safe("-x") == "'-x" and splunk.csv_safe("\tx") == "'\tx"
    assert splunk.csv_safe(-5) == "-5" and splunk.csv_safe(None) == "" and splunk.csv_safe({"a": 1}) == '{"a":1}'


# ============================================================================ SSO
def test_sso_is_not_set_up_without_a_public_app_url(tmp_path):
    a = make_app(tmp_path, app_url=None)
    mount(a)
    with TestClient(a) as c:
        signup(c, email="olivia@acme.com")
        view = c.get("/api/sso/config").json()
        assert view["operator_configured"] is False and view["missing_env"] == ["APP_URL"]
        assert view["redirect_uri"] is None and view["configured"] is False
        r = c.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                           "allowed_domains": ["acme.com"]})
        assert r.status_code == 503 and "isn't set up on this server" in r.json()["detail"]
        assert c.post("/api/auth/sso/start", json={"email": "olivia@acme.com"}).status_code == 503
        # password login is untouched
        c2 = TestClient(a)
        login(c2, "olivia@acme.com")


def test_configure_reads_discovery_and_keeps_the_secret_encrypted(owner, platform, world):
    idp = world.add(FakeIdP(OKTA))
    view = configure_sso(owner)
    assert view["issuer"] == OKTA and view["client_id"] == CLIENT_ID and view["has_secret"] is True
    assert view["redirect_uri"] == APP_URL + "/api/auth/sso/callback" and view["verified"] is False
    assert view["login_url"] == f"{APP_URL}/api/auth/sso/start?company={owner.tenant_id}"
    assert CLIENT_SECRET not in json.dumps(view) and "client_secret" not in view
    assert idp.discovery_fetches == 1 and idp.jwks_fetches == 1
    blob = platform.store.get_sso_config(owner.tenant_id, with_secret=True)["secret_blob"]
    assert blob.startswith("v1.") and CLIENT_SECRET not in blob
    # a blank secret keeps the saved one; a new client ID needs a new secret
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "allowed_domains": ["acme.com"]})
    assert r.status_code == 200 and r.json()["has_secret"] is True
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": "0oaOTHER", "allowed_domains": ["acme.com"]})
    assert r.status_code == 422 and "client secret" in r.json()["detail"]
    assert audit_rows(platform, owner.tenant_id, "sso.config")
    assert CLIENT_SECRET not in json.dumps(platform.store.list_audit(owner.tenant_id, 1000))


@pytest.mark.parametrize("issuer,doc_issuer,detail", [
    (OKTA, "https://evil.example.com", "different issuer"),
    ("https://login.microsoftonline.com/{tenantid}/v2.0", None, "not a template"),
    ("http://acme.okta.com", None, "https"),
    ("https://acme.okta.com/.well-known/openid-configuration", None, "without /.well-known"),
])
def test_bad_issuers_are_refused(owner, world, issuer, doc_issuer, detail):
    idp = world.add(FakeIdP(OKTA))
    if doc_issuer:
        idp.doc["issuer"] = doc_issuer
    r = owner.put("/api/sso/config", json={"issuer": issuer, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                           "allowed_domains": ["acme.com"]})
    assert r.status_code == 422 and detail in r.json()["detail"], r.text


def test_a_trailing_slash_difference_takes_the_documents_issuer(owner, world):
    world.add(FakeIdP(OKTA))
    assert configure_sso(owner, issuer=OKTA + "/")["issuer"] == OKTA


def test_provider_without_a_usable_algorithm_or_client_auth_is_refused(owner, world):
    idp = world.add(FakeIdP(OKTA))
    idp.doc["id_token_signing_alg_values_supported"] = ["HS256", "none"]
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                           "allowed_domains": ["acme.com"]})
    assert r.status_code == 422 and "algorithms" in r.json()["detail"]
    idp.doc["id_token_signing_alg_values_supported"] = ["RS256"]
    idp.doc["token_endpoint_auth_methods_supported"] = ["private_key_jwt"]
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                           "allowed_domains": ["acme.com"]})
    assert r.status_code == 422 and "client secret" in r.json()["detail"]


def test_only_owners_manage_sso(owner, world):
    world.add(FakeIdP(OKTA))
    office = invite(owner, "pat@acme.com", "office")
    assert office.get("/api/sso/config").status_code == 403
    assert office.put("/api/sso/config", json={"issuer": OKTA, "client_id": "x", "allowed_domains": ["acme.com"]}
                      ).status_code == 403
    assert office.delete("/api/sso/config").status_code == 403


def test_okta_sign_in_end_to_end_with_pkce_state_and_nonce(owner, app, platform, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    invite(owner, "pat@acme.com", "office")
    c = TestClient(app)
    q = start(c, "pat@acme.com")
    assert q["url"] == OKTA + "/oauth2/v1/authorize" and q["login_hint"] == "pat@acme.com"
    binding = c.cookies.get("tc_sso")
    assert binding
    code = idp.authorize(q, "pat@acme.com")
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/dashboard", r.headers.get("location")
    me = c.get("/api/me").json()
    assert me["user"]["email"] == "pat@acme.com" and me["user"]["role"] == "office" and me["via"] == "cookie"
    req = idp.token_requests[0]
    assert req.headers["authorization"].startswith("Basic ")         # client_secret_basic, listed first by Okta
    form = parse_qs(req.content.decode())
    assert form["grant_type"] == ["authorization_code"] and "client_secret" not in form
    row = audit_rows(platform, owner.tenant_id, "sso.login")[0]
    assert row["ok"] and row["args"]["issuer"] == OKTA and row["user"] == "pat@acme.com"
    # the same state can't be used twice, even from the same browser
    c.cookies.set("tc_sso", binding, path="/api/auth/sso")
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert "already used" in error_of(r)


def test_keys_are_cached_and_refreshed_on_rotation(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert r.headers["location"] == "/#/dashboard"
    fetched = idp.jwks_fetches
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert r.headers["location"] == "/#/dashboard" and idp.jwks_fetches == fetched        # cached
    idp.keys = [public_jwk(KEY2, "k2")]                                                  # the provider rotates
    idp.sign_key, idp.kid = KEY2, "k2"
    cache = sso._cache(app.state.platform)
    for entry in cache.jwks.values():
        entry["fetched"] -= sso.JWKS_REFRESH_MIN_S + 1
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert r.headers["location"] == "/#/dashboard" and idp.jwks_fetches == fetched + 1


def test_an_es256_provider_works(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    idp.doc["id_token_signing_alg_values_supported"] = ["RS256", "ES256"]
    idp.keys = [public_jwk(KEY1, "k1"), public_jwk(EC_KEY, "e1", "ES256")]
    configure_sso(owner)
    idp.sign_key, idp.kid, idp.alg = EC_KEY, "e1", "ES256"
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert r.headers["location"] == "/#/dashboard"


@pytest.mark.parametrize("claims,expect", [
    ({"aud": "someone-else"}, "different application"),
    ({"aud": [CLIENT_ID, "another-app"]}, "other applications"),
    ({"azp": "another-app"}, "azp"),
    ({"iss": "https://evil.example.com"}, "different issuer"),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 400}, "expired"),
    ({"iat": int(time.time()) - 3600}, "too old"),
    ({"nonce": "not-the-nonce-that-was-sent"}, "nonce"),
    ({"nonce": None}, "nonce"),
    ({"email_verified": False}, "verified"),
    ({"email_verified": None}, "verified"),
    ({"sub": None}, "not valid"),
])
def test_id_token_claims_are_validated(owner, app, platform, world, claims, expect):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    if claims.get("email_verified", 1) is None:
        idp.userinfo_body = {"sub": "sub-" + hashlib.sha256(b"olivia@acme.com").hexdigest()[:12],
                             "email": "olivia@acme.com"}
    c, r = sso_login(app, idp, "olivia@acme.com", **claims)
    assert expect in error_of(r)
    assert c.get("/api/me").status_code == 401
    assert audit_rows(platform, owner.tenant_id, "sso.login")[0]["ok"] is False


def test_signatures_must_come_from_the_providers_keys(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    idp.sign_key = KEY2                                  # same kid, wrong key
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert "signature" in error_of(r)
    idp.kid = "unknown-kid"
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert "doesn't publish" in error_of(r)


def test_hmac_and_none_algorithms_are_refused(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    idp.alg, idp.sign_key = "HS256", CLIENT_SECRET       # the classic algorithm-confusion attempt
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert "algorithm" in error_of(r)
    now = int(time.time())
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64url(json.dumps({"iss": OKTA, "sub": "x", "aud": CLIENT_ID, "iat": now, "exp": now + 60,
                              "email": "olivia@acme.com", "email_verified": True}).encode())
    idp.raw_id_token = f"{header}.{body}."
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert "algorithm" in error_of(r)


def test_okta_thin_id_token_uses_userinfo_with_the_same_subject(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    sub = "sub-" + hashlib.sha256(b"").hexdigest()[:12]
    idp.userinfo_body = {"sub": sub, "name": "Olivia", "email": "olivia@acme.com", "email_verified": True}
    _, r = sso_login(app, idp, "olivia@acme.com", claim_email=None)
    assert r.headers["location"] == "/#/dashboard"
    idp.userinfo_body = {"sub": "someone-else", "email": "olivia@acme.com", "email_verified": True}
    _, r = sso_login(app, idp, "olivia@acme.com", claim_email=None)
    assert "different person" in error_of(r)


def test_entra_needs_xms_edov_because_its_email_claim_is_not_verified(owner, app, world):
    idp = world.add(FakeIdP(ENTRA, "entra"))
    configure_sso(owner, issuer=ENTRA)
    _, r = sso_login(app, idp, "olivia@acme.com", email_verified=None, xms_edov=True, tid=ENTRA_TID)
    assert r.headers["location"] == "/#/dashboard"
    req = idp.token_requests[-1]
    assert req.headers["authorization"].startswith("Basic ")
    sub = "sub-" + hashlib.sha256(b"olivia@acme.com").hexdigest()[:12]
    idp.userinfo_body = {"sub": sub, "email": "olivia@acme.com"}          # Graph's UserInfo has no verification
    _, r = sso_login(app, idp, "olivia@acme.com", email_verified=None)
    assert "xms_edov" in error_of(r)


def test_client_secret_post_when_it_is_the_only_method(owner, app, world):
    idp = world.add(FakeIdP(ENTRA, "entra"))
    idp.doc["token_endpoint_auth_methods_supported"] = ["client_secret_post", "private_key_jwt"]
    configure_sso(owner, issuer=ENTRA)
    _, r = sso_login(app, idp, "olivia@acme.com", email_verified=None, xms_edov=True)
    assert r.headers["location"] == "/#/dashboard"
    req = idp.token_requests[-1]
    form = parse_qs(req.content.decode())
    assert "authorization" not in req.headers and form["client_secret"] == [CLIENT_SECRET]
    assert form["client_id"] == [CLIENT_ID] and form["code_verifier"][0]


def test_google_accepts_both_documented_issuers_and_checks_hd(owner, app, world):
    idp = world.add(FakeIdP(GOOGLE, "google"))
    configure_sso(owner, issuer=GOOGLE)
    _, r = sso_login(app, idp, "olivia@acme.com", hd="acme.com", extra_query=f"&iss={quote(GOOGLE)}")
    assert r.headers["location"] == "/#/dashboard"
    _, r = sso_login(app, idp, "olivia@acme.com", hd="acme.com", iss="accounts.google.com",
                     extra_query=f"&iss={quote(GOOGLE)}")
    assert r.headers["location"] == "/#/dashboard"
    _, r = sso_login(app, idp, "olivia@acme.com", extra_query=f"&iss={quote(GOOGLE)}")        # no hd claim
    assert "Google Workspace" in error_of(r)
    # the RFC 9207 iss on the redirect: a different one is refused, a missing one is accepted
    _, r = sso_login(app, idp, "olivia@acme.com", hd="acme.com")
    assert r.headers["location"] == "/#/dashboard"
    _, r = sso_login(app, idp, "olivia@acme.com", hd="acme.com", extra_query="&iss=https%3A%2F%2Fevil.example.com")
    assert "different identity provider" in error_of(r)


def test_the_email_must_be_an_existing_user_of_this_company_only(owner, app, platform, world):
    """Company A's identity provider asserting the email of company B's user gets nobody's session."""
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    other = other_company(app, email="bob@acme.com", company="Other Co")    # same domain, other company
    users_before = len(platform.store.list_users(owner.tenant_id))
    c = TestClient(app)
    q_owner = start(c, "olivia@acme.com")                                   # a sign-in into company A ...
    code = idp.authorize(q_owner, "bob@acme.com")                           # ... answered with B's user
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q_owner['state'])}", follow_redirects=False)
    assert "no TradeComply user with this email in this company" in error_of(r)
    assert c.get("/api/me").status_code == 401
    _, r = sso_login(app, idp, "olivia@acme.com", claim_email="newperson@acme.com")
    assert "no TradeComply user" in error_of(r)
    assert len(platform.store.list_users(owner.tenant_id)) == users_before       # nobody is created
    # bob's own start uses bob's company, which has no SSO
    assert TestClient(app).post("/api/auth/sso/start", json={"email": "bob@acme.com"}).status_code == 404
    assert other.get("/api/sso/config").json()["configured"] is False


def test_allowed_domains_limit_who_can_use_sso(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner, allowed_domains=["acme-builders.com"])
    r = TestClient(app).post("/api/auth/sso/start", json={"email": "olivia@acme.com"})
    assert r.status_code == 404
    view = owner.get("/api/sso/config").json()
    assert view["users_without_sso"] == ["olivia@acme.com"]
    configure_sso(owner, allowed_domains=["acme.com"])
    _, r = sso_login(app, idp, "olivia@acme.com", claim_email="olivia@acme-builders.com")
    assert "can't sign in to this company" in error_of(r)


def test_state_is_bound_to_the_browser_and_cannot_be_forged(owner, app, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    victim, attacker = TestClient(app), TestClient(app)
    q = start(attacker, "olivia@acme.com")
    code = idp.authorize(q, "olivia@acme.com")
    r = victim.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert "different browser" in error_of(r) and victim.get("/api/me").status_code == 401
    r = victim.get("/api/auth/sso/callback?code=x&state=forged.state", follow_redirects=False)
    assert "expired or was changed" in error_of(r)


def test_provider_errors_and_changed_settings_end_the_sign_in(owner, app, platform, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    c = TestClient(app)
    q = start(c, "olivia@acme.com")
    r = c.get(f"/api/auth/sso/callback?error=access_denied&state={quote(q['state'])}", follow_redirects=False)
    assert "(access_denied)" in error_of(r)
    q = start(c, "olivia@acme.com")
    code = idp.authorize(q, "olivia@acme.com")
    idp.client_secret = "rotated-at-the-provider"
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert "(invalid_client)" in error_of(r)
    idp.client_secret = CLIENT_SECRET
    q = start(c, "olivia@acme.com")
    code = idp.authorize(q, "olivia@acme.com")
    world.add(FakeIdP("https://acme2.okta.com"))
    configure_sso(owner, issuer="https://acme2.okta.com")
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert "settings changed" in error_of(r)
    failures = [a for a in audit_rows(platform, owner.tenant_id, "sso.login") if not a["ok"]]
    assert {a["args"]["error"] for a in failures} >= {"idp_access_denied", "token_invalid_client", "config_changed"}


def _continue_link(page: str) -> str:
    import html as _html
    import re as _re
    m = _re.search(r'<a class="btn" href="([^"]+)">', page)
    assert m, page
    return _html.unescape(m.group(1))


def test_company_link_start_redirects_and_checks_iss(owner, app, world):
    world.add(FakeIdP(OKTA))
    configure_sso(owner)
    c = TestClient(app)
    page = c.get(f"/api/auth/sso/start?company={owner.tenant_id}", follow_redirects=False)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Acme Builders" in page.text and "acme.okta.com" in page.text and "tc_sso" not in c.cookies
    assert "samesite=strict" in page.headers["set-cookie"].lower()
    r = c.get(_continue_link(page.text), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(OKTA + "/oauth2/v1/authorize?")
    assert "tc_sso" in c.cookies
    assert c.get(_continue_link(page.text), follow_redirects=False).status_code == 200    # the go cookie is used up
    r = c.get(f"/api/auth/sso/start?company={owner.tenant_id}&iss={quote('https://evil.example.com')}",
              follow_redirects=False)
    assert "different identity provider" in error_of(r)
    r = c.get("/api/auth/sso/start?company=t_0000000000000000", follow_redirects=False)
    assert "isn't set up" in error_of(r)


def test_company_link_is_no_open_redirect_and_no_login_csrf(app, platform, world):
    """A company link on another site (a phishing mail, an attacker's page) can't bounce a visitor straight to the
    authorization_endpoint some company configured, and can't run the sign-in in the visitor's browser: only the
    confirmation page's own Continue click (go + the SameSite=Strict cookie it set) redirects."""
    evil = world.add(FakeIdP("https://idp.evil.example"))
    evil.doc["authorization_endpoint"] = "https://phish.evil.example/login"
    with TestClient(app) as attacker:
        tid = signup(attacker, email="mallory@evil.example", company='<img src=x onerror="alert(1)">')["tenant_id"]
        configure_sso(attacker, issuer="https://idp.evil.example", allowed_domains=["evil.example"])
        page = attacker.get(f"/api/auth/sso/start?company={tid}", follow_redirects=False)
        forged = _continue_link(page.text)                       # the attacker's own valid link...
    victim = TestClient(app)
    for url in (f"/api/auth/sso/start?company={tid}", forged, f"/api/auth/sso/start?company={tid}&go=x"):
        r = victim.get(url, follow_redirects=False)              # ...is useless without the victim's own cookie
        assert r.status_code == 200 and "location" not in r.headers, url
        assert "tc_sso" not in victim.cookies and "phish.evil.example/login?" not in r.text
        assert "<img" not in r.text and "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in r.text
        assert "phish.evil.example" in r.text                    # the page names where Continue goes
    assert "tc_sso_go" in victim.cookies                         # the victim's own page set its own nonce...
    r = victim.get(forged, follow_redirects=False)               # ...which the attacker's go doesn't match
    assert r.status_code == 200 and "location" not in r.headers and "tc_sso" not in victim.cookies
    # The login page's start is JSON only: a cross-site form post (text/plain or urlencoded) is refused.
    for ctype, body in (("text/plain", '{"email":"mallory@evil.example"}'),
                        ("application/x-www-form-urlencoded", "email=mallory%40evil.example")):
        r = victim.post("/api/auth/sso/start", content=body, headers={"content-type": ctype})
        assert r.status_code == 422 and "authorize_url" not in r.text and "tc_sso" not in victim.cookies


def test_enforcement_requires_a_verified_sign_in_then_blocks_passwords_except_owners(owner, app, platform, world):
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    office = invite(owner, "pat@acme.com", "office")
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "allowed_domains": ["acme.com"],
                                           "enforced": True})
    assert r.status_code == 409 and "Sign in with SSO once" in r.json()["detail"]
    _, r = sso_login(app, idp, "olivia@acme.com")
    assert r.headers["location"] == "/#/dashboard"
    assert owner.get("/api/sso/config").json()["verified"] is True
    r = owner.put("/api/sso/config", json={"issuer": OKTA, "client_id": CLIENT_ID, "allowed_domains": ["acme.com"],
                                           "enforced": True})
    assert r.status_code == 200 and r.json()["enforced"] is True and r.json()["sessions_ended"] == 1
    assert office.get("/api/me").status_code == 401                   # the office user's password session ended
    assert owner.get("/api/me").status_code == 200                    # the owner's didn't
    r = TestClient(app).post("/api/auth/login", json={"email": "pat@acme.com", "password": PW})
    assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
    r = TestClient(app).post("/api/auth/login", json={"email": "pat@acme.com", "password": "wrong-password-123"})
    assert r.status_code == 401                                       # a wrong password still says just that
    c = TestClient(app)
    login(c, "olivia@acme.com", PW)                                   # break-glass: the owner's password works
    assert audit_rows(platform, owner.tenant_id, "sso.break_glass_login")
    _, r = sso_login(app, idp, "pat@acme.com")                         # and the office user signs in with SSO
    assert r.headers["location"] == "/#/dashboard"


def test_cli_unenforce(owner, app, platform, world, monkeypatch, capsys):
    world.add(FakeIdP(OKTA))
    configure_sso(owner)
    platform.store.save_sso_config(owner.tenant_id, issuer=OKTA, client_id=CLIENT_ID, allowed_domains=["acme.com"],
                                   enforced=True)
    monkeypatch.setattr("app.store.default_data_dir", lambda: platform.store.data_dir)
    monkeypatch.setattr("app.store.database_url_from_env", lambda: getattr(platform.store, "database_url", None))
    assert sso.main(["unenforce", owner.tenant_id]) == 0
    assert json.loads(capsys.readouterr().out.strip())["enforced"] is False
    assert platform.store.get_sso_config(owner.tenant_id, with_secret=True)["secret_blob"]          # secret kept
    assert audit_rows(platform, owner.tenant_id, "sso.unenforce")
    assert sso.main(["unenforce"]) == 2


def test_sso_test_route_and_delete(owner, platform, world):
    idp = world.add(FakeIdP(OKTA))
    assert owner.post("/api/sso/test").status_code == 409
    configure_sso(owner)
    r = owner.post("/api/sso/test").json()
    assert r["ok"] is True and r["token_auth"] == "client_secret_basic" and r["signing_keys"] == 1
    idp.keys = []
    r = owner.post("/api/sso/test").json()
    assert r["ok"] is False and r["code"] == "no_keys"
    assert owner.delete("/api/sso/config").json() == {"ok": True}
    assert owner.delete("/api/sso/config").status_code == 404


def test_no_secret_code_or_token_reaches_the_log(owner, app, world, caplog):
    caplog.set_level(logging.DEBUG)
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    c = TestClient(app)
    q = start(c, "olivia@acme.com")
    code = idp.authorize(q, "olivia@acme.com")
    r = c.get(f"/api/auth/sso/callback?code={code}&state={quote(q['state'])}", follow_redirects=False)
    assert r.headers["location"] == "/#/dashboard"
    text = "\n".join(rec.getMessage() for rec in caplog.records if rec.name.startswith("tradecomply"))
    assert "sso sign-in for tenant" in text
    for secret in (CLIENT_SECRET, code, q["state"], q["nonce"]):
        assert secret not in text
    assert "eyJ" not in text                                          # no JWT (ID token) in the log
    # the server's access log (uvicorn) redacts the callback's code and state (app/main.py RedactTokens)
    from app.main import RedactTokens
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', (
        "203.0.113.9", "GET", f"/api/auth/sso/callback?code={code}&state={q['state']}", "1.1", 303), None)
    RedactTokens().filter(rec)
    assert code not in rec.getMessage() and q["state"] not in rec.getMessage()


# ============================================================================ security review regressions
def test_userinfo_cannot_vouch_for_a_different_email_than_the_id_token(owner, app, platform, world):
    """The ID token says olivia@ without email_verified; UserInfo (same sub) says someone else's address is verified.
    That flag must not verify olivia@."""
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    sub = "sub-" + hashlib.sha256(b"olivia@acme.com").hexdigest()[:12]
    idp.userinfo_body = {"sub": sub, "email": "it-admin@acme.com", "email_verified": True}
    c, r = sso_login(app, idp, "olivia@acme.com", email_verified=None)
    assert "different email address" in error_of(r) and c.get("/api/me").status_code == 401
    assert audit_rows(platform, owner.tenant_id, "sso.login")[0]["args"]["error"] == "userinfo_email"
    idp.userinfo_body = {"sub": sub, "email": "Olivia@Acme.com", "email_verified": True}     # the same address
    _, r = sso_login(app, idp, "olivia@acme.com", email_verified=None)
    assert r.headers["location"] == "/#/dashboard"


def test_issuer_is_matched_exactly_even_if_the_jwt_library_does_not(owner, app, world, monkeypatch):
    """PyJWT 2.10.0 (CVE-2024-53861) accepted an iss that is a substring of the expected issuer. Simulate a library
    that skips the iss check: TradeComply's own exact comparison still refuses the token."""
    idp = world.add(FakeIdP(OKTA))
    configure_sso(owner)
    real = sso.jwt.decode
    monkeypatch.setattr(sso.jwt, "decode", lambda token, **kw: real(token, **{**kw, "issuer": None}))
    _, r = sso_login(app, idp, "olivia@acme.com", iss="https://acme.okta")
    assert "different issuer" in error_of(r)


def test_pyjwt_requirement_excludes_the_vulnerable_release():
    import os
    from packaging.requirements import Requirement
    from packaging.version import Version
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")
    lines = [ln.split(" #")[0].strip() for ln in open(path, encoding="utf-8")]
    req = next(Requirement(ln) for ln in lines if ln.lower().startswith("pyjwt"))
    assert not req.specifier.contains("2.10.0") and req.specifier.contains("2.10.1")
    assert Version(jwt.__version__) != Version("2.10.0")


def test_changing_the_issuer_needs_the_client_secret_again(owner, world):
    """Otherwise an edit of the issuer alone would send the saved client secret to another server's token endpoint
    on the next sign-in."""
    world.add(FakeIdP(OKTA))
    world.add(FakeIdP("https://attacker-idp.example.com"))
    configure_sso(owner)
    body = {"client_id": CLIENT_ID, "allowed_domains": ["acme.com"]}
    r = owner.put("/api/sso/config", json={**body, "issuer": "https://attacker-idp.example.com"})
    assert r.status_code == 422 and "client secret" in r.json()["detail"]
    assert owner.get("/api/sso/config").json()["issuer"] == OKTA
    assert owner.put("/api/sso/config", json={**body, "issuer": OKTA + "/"}).status_code == 200   # same issuer
    r = owner.put("/api/sso/config", json={**body, "issuer": "https://attacker-idp.example.com",
                                           "client_secret": "a-new-secret-for-the-new-provider"})
    assert r.status_code == 200 and r.json()["issuer"] == "https://attacker-idp.example.com"


def test_changing_the_hec_url_needs_the_token_again(owner, platform, world):
    """Otherwise anyone who may edit settings (office user, API key) could point the URL at their own server and
    receive the saved HEC token with the next test, without ever seeing it."""
    connect_splunk(owner)
    posts = len(world.hec.posts)
    evil = "https://collector.attacker.example.com"
    for route, method in (("config", "put"), ("connect", "post")):
        r = getattr(owner, method)(f"/api/integrations/splunk/{route}", json={"config": {"hec_url": evil}})
        assert r.status_code == 422 and "HEC token" in r.json()["detail"], r.text
        r = getattr(owner, method)(f"/api/integrations/splunk/{route}",
                                   json={"config": {"hec_url": evil, "hec_token": ""}})
        assert r.status_code == 422
    assert len(world.hec.posts) == posts and not any(evil in s for s in map(str, (q.url for q in world.seen)))
    assert platform.store.get_connection(owner.tenant_id, "splunk")["config"]["hec_url"] == HEC_URL
    # the same URL, another index: no token needed; a new URL with the token typed again: fine
    assert owner.put("/api/integrations/splunk/config", json={"config": {"index": "main"}}).status_code == 200
    r = owner.put("/api/integrations/splunk/config", json={"config": {"hec_url": HEC_URL + "/services/collector"}})
    assert r.status_code == 200                                         # normalizes to the same URL
    r = owner.put("/api/integrations/splunk/config", json={"config": {"hec_url": "https://splunk.example.com:8088",
                                                                      "hec_token": HEC_TOKEN}})
    assert r.status_code == 200



def test_a_stored_lone_surrogate_never_stalls_the_feed(owner, platform, world):
    """Final review web-01: a tool argument with a lone UTF-16 surrogate ("\\ud800", valid JSON) stored in the
    activity log made every export round fail on that row, so nothing after it (nor the MCP log) ever reached
    Splunk. New requests are refused at the edge (422); a row stored before that is sent repaired."""
    connect_splunk(owner)
    platform.store.audit(owner.tenant_id, "olivia@acme.com", "search_california_rules", {"query": "lien \ud800"},
                         None, source="api")
    seed_logs(platform, owner.tenant_id, 2, 1)
    r = splunk.export_tenant(platform, owner.tenant_id, now=LATER())
    assert "error" not in r and not r.get("skipped_rows"), r
    activity = [e["event"] for e in world.hec.events if e["event"]["log"] == "activity"]
    assert any(e["args"] == {"query": "lien \ufffd"} for e in activity)
    assert {e["id"] for e in activity} == {a["id"] for a in platform.store.list_audit(owner.tenant_id, 1000)}
    assert [e for e in world.hec.events if e["event"]["log"] == "mcp"]
    download = owner.get("/api/audit-export?log=activity&format=json")
    rows = json.loads(download.text)["rows"]               # the whole file: valid JSON to the end
    assert download.status_code == 200 and {"query": "lien \ufffd"} in [r["args"] for r in rows]
    csv_text = owner.get("/api/audit-export?log=activity&format=csv").text
    assert "lien \ufffd" in csv_text and len(list(csv.reader(io.StringIO(csv_text)))) == len(rows) + 2   # header, this download's own row
