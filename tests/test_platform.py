"""Hosted platform: accounts, sessions, CSRF, tenant isolation, API keys, /mcp gate, billing, tools, dashboard,
offline field-log sync, CSV import. Uses the demo license fixtures (conftest sets LICENSE_DB to a missing file).

Every test runs twice: on SQLite, and on PostgreSQL when TC_TEST_DATABASE_URL points at a database the tests may
use (each test gets its own new schema, dropped afterwards); without it the Postgres variant is skipped. E.g.
    TC_TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/tc_test python -m pytest -q tests/test_platform.py
"""
import json
import re
import os
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tc-test-"))   # module-level app in app.main
os.environ.pop("DATABASE_URL", None)       # never a real database: Postgres only through TC_TEST_DATABASE_URL
PG_TEST_URL = (os.getenv("TC_TEST_DATABASE_URL") or "").strip() or None

from fastapi.testclient import TestClient  # noqa: E402

from app import billing  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tradecomply import logic  # noqa: E402
from tests.support import browser  # noqa: E402  (another browser on a running app)

PW = "correct-horse-battery"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


_db = {"backend": "sqlite", "url": None, "apps": []}


def with_search_path(url: str, schema: str) -> str:
    """The same database URL, with every connection using `schema` (libpq `options=-c search_path=...`)."""
    return url + ("&" if "?" in url else "?") + "options=" + quote(f"-csearch_path={schema}", safe="")


@pytest.fixture(params=["sqlite", "postgres"], autouse=True)
def backend(request):
    """Run the test on SQLite, then on PostgreSQL in a fresh schema (skipped unless TC_TEST_DATABASE_URL is set)."""
    schema = None
    if request.param == "postgres":
        if not PG_TEST_URL:
            pytest.skip("set TC_TEST_DATABASE_URL to run the PostgreSQL variant")
        import psycopg
        schema = "tc_t_" + secrets.token_hex(6)
        with psycopg.connect(PG_TEST_URL, autocommit=True) as con:
            con.execute(f'create schema "{schema}"')
        _db.update(backend="postgres", url=with_search_path(PG_TEST_URL, schema), apps=[])
    else:
        _db.update(backend="sqlite", url=None, apps=[])
    try:
        yield request.param
    finally:
        for a in _db["apps"]:
            a.state.platform.store.close()
        _db.update(backend="sqlite", url=None, apps=[])
        if schema:
            import psycopg
            with psycopg.connect(PG_TEST_URL, autocommit=True) as con:
                con.execute(f'drop schema "{schema}" cascade')


def make_app(tmp_path, **overrides):
    overrides.setdefault("database_url", _db["url"])
    a = create_app(Settings.from_env(data_dir=str(tmp_path), **overrides))
    _db["apps"].append(a)
    return a


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def signup(c, email="owner@acme.test", company="Acme Builders", license_number="123456"):
    r = c.post("/api/auth/signup", json={"company_name": company, "email": email, "password": PW,
                                         "cslb_license": license_number})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
    return r.json()


def new_client(app):
    return TestClient(app)


def mcp_session(c, headers=None):
    h = {**MCP_HEADERS, **(headers or {})}
    r = c.post("/mcp", json=INIT, headers=h)
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    h2 = {**h, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"} if sid else h
    c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
    return h2


def sse_json(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


# ---------------------------------------------------------------------------- health + MCP endpoint
def test_health_and_mcp_initialize(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["server"] == "TradeComply CA"
    r = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 200
    assert sse_json(r.text)["result"]["serverInfo"]["name"] == "TradeComply CA"
    assert client.get("/").status_code == 200 and "text/html" in client.get("/").headers["content-type"]


def test_anonymous_mcp_tool_call_is_audited_but_not_visible_to_tenants(client, app):
    h = mcp_session(client)
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "verify_contractor_license", "arguments": {"license_number": "123456"}}})
    assert r.status_code == 200
    assert sse_json(r.text)["result"]["structuredContent"]["business_name"] == "Example Plumbing Inc."
    store = app.state.platform.store
    with store.connect() as con:
        rows = con.execute('select tenant_id, "user", tool, verified, source from audit').fetchall()
    assert [tuple(r) for r in rows] == [(None, "anonymous (MCP)", "verify_contractor_license", 0, "mcp")]
    signup(client)
    assert client.get("/api/audit").json() == []


def test_health_reports_storage(tmp_path, backend, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    with TestClient(make_app(tmp_path / "laptop")) as c:
        h = c.get("/health").json()
        assert h["ok"] is True and h["schema"] >= 3 and "license_data" in h
        assert h["storage"] == backend and h["storage_persistent"] is True
    # on Render, SQLite is only kept if DATA_DIR is on a mounted persistent disk; Postgres always is
    import app.store as store_mod
    monkeypatch.setenv("RENDER", "true")
    for on_disk in (False, True):
        monkeypatch.setattr(store_mod, "disk_is_persistent", lambda path, v=on_disk: v)
        with TestClient(make_app(tmp_path / f"render-{on_disk}")) as c:
            h = c.get("/health").json()
            assert h["storage"] == backend and h["storage_persistent"] is (backend == "postgres" or on_disk)


def test_generated_signing_key_survives_restarts(tmp_path, backend, monkeypatch):
    """Without SECRET_KEY, sessions must survive a restart. On Postgres the key lives in the database, so a second
    instance with its own (empty) disk accepts the same cookie; on SQLite it lives next to the database file."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    first = make_app(tmp_path / "a")
    with TestClient(first) as c:
        signup(c)
        cookie = c.cookies.get("tc_session")
    second = make_app(tmp_path / ("b" if backend == "postgres" else "a"))
    assert second.state.platform.signer.mac("x", "y") == first.state.platform.signer.mac("x", "y")
    with TestClient(second) as c2:
        c2.cookies.set("tc_session", cookie)
        assert c2.get("/api/me").json()["user"]["email"] == "owner@acme.test"
    if backend == "postgres":
        assert not os.path.exists(tmp_path / "b" / "secret_key") and not os.path.exists(tmp_path / "b" /
                                                                                        "tradecomply.sqlite")


# ---------------------------------------------------------------------------- accounts and sessions
def test_signup_login_logout(client):
    out = signup(client)
    assert out["ok"] and out["tenant_id"].startswith("t_")
    me = client.get("/api/me").json()
    assert me["user"]["role"] == "owner" and me["tenant"]["status"] == "trialing"
    assert me["entitlement"]["ok"] and me["entitlement"]["trial_days_left"] == 14
    assert me["tenant"]["cslb_license"] == "123456"
    set_cookie = client.post("/api/auth/logout").headers.get_list("set-cookie")
    assert any("tc_session" in h for h in set_cookie)
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "owner@acme.test", "password": "wrong-password"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "OWNER@acme.test", "password": PW})
    assert r.status_code == 200
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("tc_session="))
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
    assert client.get("/api/me").status_code == 200


def test_signup_validation_and_duplicates(client):
    r = client.post("/api/auth/signup", json={"company_name": "X", "email": "a@b.test", "password": "short"})
    assert r.status_code == 422
    signup(client, email="dup@acme.test")
    r = client.post("/api/auth/signup", json={"company_name": "Y", "email": "dup@acme.test", "password": PW})
    assert r.status_code == 409


def test_tampered_or_expired_session_rejected(client, app):
    signup(client)
    token = client.cookies.get("tc_session")
    body, sig = token.split(".")
    client.cookies.set("tc_session", body + "." + sig[:-2] + ("AA" if not sig.endswith("AA") else "BB"))
    assert client.get("/api/me").status_code == 401
    expired = app.state.platform.signer.sign("session", {"u": 1, "t": "x", "g": 0, "n": "n", "e": int(time.time()) - 5})
    client.cookies.set("tc_session", expired)
    assert client.get("/api/me").status_code == 401


def test_password_change_ends_other_sessions(app):
    with TestClient(app) as a, browser(a) as b:
        signup(a)
        b.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW})
        assert b.get("/api/me").status_code == 200
        r = a.post("/api/auth/password", json={"current_password": PW, "new_password": "another-long-password"})
        assert r.status_code == 200
        a.headers["x-csrf-token"] = a.cookies.get("tc_csrf")
        assert a.get("/api/me").status_code == 200
        assert b.get("/api/me").status_code == 401


def test_login_rate_limited(client):
    signup(client)
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": "owner@acme.test", "password": "nope-nope-nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_csrf_required_for_cookie_writes(client):
    signup(client)
    token = client.headers.pop("x-csrf-token")
    assert client.post("/api/projects", json={"name": "No token"}).status_code == 403
    assert client.post("/api/projects", json={"name": "Wrong"}, headers={"x-csrf-token": "0" * 64}).status_code == 403
    assert client.post("/api/projects", json={"name": "OK"}, headers={"x-csrf-token": token}).status_code == 201
    assert client.get("/api/projects").status_code == 200          # reads don't need the token


# ---------------------------------------------------------------------------- tenant isolation
def test_tenant_isolation(app):
    with TestClient(app) as a, browser(a) as b:
        signup(a, email="a@one.test", company="One")
        signup(b, email="b@two.test", company="Two")
        pid = a.post("/api/projects", json={"name": "A's job"}).json()["id"]
        sid = a.post("/api/subs", json={"name": "A's sub", "license_number": "123456"}).json()["id"]
        did = a.post("/api/documents", json={"title": "A's doc", "body_markdown": "# secret"}).json()["id"]
        a.post("/api/tools/verify_contractor_license", json={"license_number": "123456"})
        aid = a.get("/api/audit").json()[0]["id"]
        for path in (f"/api/projects/{pid}", f"/api/subs/{sid}", f"/api/documents/{did}", f"/api/audit/{aid}",
                     f"/api/projects/{pid}/deadlines", f"/api/subs/{sid}/check"):
            assert b.get(path).status_code == 404, path
        assert b.patch(f"/api/projects/{pid}", json={"name": "hijack"}).status_code == 404
        assert b.delete(f"/api/subs/{sid}").status_code == 404
        assert b.get("/api/projects").json() == [] and b.get("/api/subs").json() == []
        assert b.get("/api/documents").json() == [] and b.get("/api/audit").json() == []
        # B can't attach its records to A's project either
        assert b.post("/api/subs", json={"name": "x", "project_id": pid}).status_code == 422
        assert b.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "2026-11-20",
                                               "project_id": pid}).status_code == 422
        assert a.get(f"/api/projects/{pid}").json()["name"] == "A's job"


def test_field_role_can_only_log(app):
    with TestClient(app) as owner, browser(owner) as field:
        signup(owner)
        pid = owner.post("/api/projects", json={"name": "Job"}).json()["id"]
        inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()
        assert "temporary_password" not in inv and inv["invite_path"].startswith("/#/invite/tcl_")
        r = field.post("/api/auth/invite/accept", json={"token": inv["invite_path"].rsplit("/", 1)[1],
                                                        "password": PW})
        assert r.status_code == 200
        field.headers["x-csrf-token"] = field.cookies.get("tc_csrf")
        assert field.get("/api/me").json()["user"]["must_change_password"] is False
        assert field.post("/api/projects", json={"name": "nope"}).status_code == 403
        assert field.post("/api/subs", json={"name": "nope"}).status_code == 403
        assert field.post("/api/documents", json={"title": "nope"}).status_code == 403
        assert field.post("/api/keys", json={"name": "nope"}).status_code == 403
        assert field.get("/api/projects").status_code == 200
        r = field.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "2026-11-20", "project_id": pid,
                                                "topic": "Ladders", "attendees": [{"name": "Ana", "acknowledged": True}]})
        assert r.status_code == 201 and r.json()["created_by"] == "crew@acme.test"


def test_people_added_with_a_temporary_password_before_invite_links_still_sign_in(app):
    """Backward compatibility: someone an owner added before invite links has a temporary password and
    must_change_password. They can still log in, are asked to change it, and can be sent an invite link instead."""
    from app.auth import hash_password
    with TestClient(app) as owner, browser(owner) as legacy:
        tid = signup(owner)["tenant_id"]
        store = app.state.platform.store
        u = store.create_user(tid, "legacy@acme.test", hash_password("temp-pass-123"), "field",
                              must_change_password=True)
        r = legacy.post("/api/auth/login", json={"email": "legacy@acme.test", "password": "temp-pass-123"})
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        legacy.headers["x-csrf-token"] = legacy.cookies.get("tc_csrf")
        assert legacy.get("/api/me").json()["user"]["must_change_password"] is True
        assert owner.post(f"/api/users/{u['id']}/invite").status_code == 409      # they signed in: already joined
        v = store.create_user(tid, "never@acme.test", hash_password("temp-pass-456"), "field",
                              must_change_password=True)
        inv = owner.post(f"/api/users/{v['id']}/invite").json()                  # never signed in: a new link
        assert inv["invite_path"].startswith("/#/invite/")
        r = TestClient(app).post("/api/auth/login", json={"email": "never@acme.test", "password": "temp-pass-456"})
        assert r.status_code == 401                          # the old temporary password stopped working


# ---------------------------------------------------------------------------- API keys on /api and /mcp
def test_api_key_auth_and_revocation(tmp_path):
    app = make_app(tmp_path, mcp_require_key=True)
    with TestClient(app) as c, browser(c) as bot:
        signup(c)
        made = c.post("/api/keys", json={"name": "Claude Desktop"}).json()
        key = made["key"]
        assert key.startswith("tck_") and "key" not in c.get("/api/keys").json()[0]
        assert "key_hash" not in c.get("/api/keys").json()[0]
        auth = {"authorization": f"Bearer {key}"}
        # /api with a Bearer key: no cookie, no CSRF token needed
        assert bot.get("/api/projects", headers=auth).status_code == 200
        assert bot.post("/api/projects", json={"name": "Via key"}, headers=auth).status_code == 201
        assert bot.get("/api/projects", headers={"authorization": "Bearer tck_wrong"}).status_code == 401
        # /mcp requires a key when MCP_REQUIRE_KEY=1
        assert bot.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        h = mcp_session(bot, auth)
        r = bot.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "lookup_safety_standard", "arguments": {"hazard": "fall protection"}}})
        assert sse_json(r.text)["result"]["structuredContent"]["topic"] == "fall protection"
        rows = c.get("/api/audit").json()
        assert rows[0]["tool"] == "lookup_safety_standard" and rows[0]["source"] == "mcp"
        assert rows[0]["verified"] is True and rows[0]["user"].startswith("api key tck_")
        # revoke: both /api and /mcp reject it
        assert c.delete(f"/api/keys/{made['id']}").status_code == 200
        assert bot.get("/api/projects", headers=auth).status_code == 401
        assert bot.post("/mcp", json=INIT, headers={**MCP_HEADERS, **auth}).status_code == 401
        assert c.get("/api/keys").json()[0]["revoked"] is True


def test_mcp_key_of_lapsed_tenant_rejected(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as c:
        out = signup(c)
        key = c.post("/api/keys", json={"name": "agent"}).json()["key"]
        app.state.platform.store.update_tenant(out["tenant_id"], status="canceled")
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {key}"})
        assert r.status_code == 402
        c.cookies.clear()
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200     # anonymous demo unaffected


# ---------------------------------------------------------------------------- billing
def test_trial_expired_returns_402(client, app):
    out = signup(client)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    app.state.platform.store.update_tenant(out["tenant_id"], trial_ends=past)
    me = client.get("/api/me").json()
    assert me["entitlement"]["ok"] is False and me["entitlement"]["reason"] == "trial_expired"
    r = client.post("/api/tools/verify_contractor_license", json={"license_number": "123456"})
    assert r.status_code == 402 and r.json()["detail"]["subscribe"] is True
    assert client.get("/api/dashboard").status_code == 402
    assert client.get("/api/projects").status_code == 200       # their data stays readable
    assert client.post("/api/billing/checkout").status_code == 503   # Stripe not configured: clear message
    assert "not set up" in client.post("/api/billing/checkout").json()["detail"]


def test_stripe_signature_scheme():
    secret, body = "whsec_test", b'{"id":"evt_1"}'
    now = 1_800_000_000
    billing.verify_signature(body, billing.sign_payload(body, secret, now), secret, now=now + 299)
    header = billing.sign_payload(body, secret, now)
    # several v1 entries (secret rotation): any match is accepted
    billing.verify_signature(body, f"t={now},v1={'0' * 64},{header.split(',')[1]}", secret, now=now)
    for bad in (None, "garbage", f"t={now}", billing.sign_payload(body, "whsec_other", now),
                billing.sign_payload(body + b" ", secret, now)):
        with pytest.raises(billing.SignatureError):
            billing.verify_signature(body, bad, secret, now=now)
    with pytest.raises(billing.SignatureError):
        billing.verify_signature(body, header, secret, now=now + 301)


def test_stripe_webhook_updates_tenant(tmp_path):
    secret = "whsec_test_secret"
    app = make_app(tmp_path, stripe_webhook_secret=secret)
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        store = app.state.platform.store

        def post(event, ts=None, sig_secret=secret):
            payload = json.dumps(event).encode()
            return c.post("/api/billing/webhook", content=payload, headers={
                "stripe-signature": billing.sign_payload(payload, sig_secret, ts), "content-type": "application/json"})

        done = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
            "client_reference_id": tid, "mode": "subscription", "customer": "cus_1", "subscription": "sub_1",
            "payment_status": "paid"}}}
        assert post(done, sig_secret="whsec_wrong").status_code == 400
        assert post(done, ts=int(time.time()) - 600).status_code == 400            # outside 5-minute tolerance
        assert store.get_tenant(tid)["status"] == "trialing"
        r = post(done)
        assert r.status_code == 200 and r.json()["tenant_updated"] is True
        t = store.get_tenant(tid)
        assert (t["status"], t["stripe_customer_id"], t["stripe_subscription_id"]) == ("active", "cus_1", "sub_1")
        assert post(done).json()["duplicate"] is True
        post({"id": "evt_2", "type": "invoice.payment_failed",
              "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}})
        assert store.get_tenant(tid)["status"] == "past_due"
        assert c.get("/api/me").json()["entitlement"]["ok"] is True          # access continues during retries
        post({"id": "evt_3", "type": "customer.subscription.updated",
              "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "active"}}})
        assert store.get_tenant(tid)["status"] == "active"
        post({"id": "evt_4", "type": "customer.subscription.deleted",
              "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
        assert store.get_tenant(tid)["status"] == "canceled"
        assert c.post("/api/tools/verify_contractor_license", json={"license_number": "123456"}).status_code == 402


def test_stripe_checkout_session(tmp_path, monkeypatch):
    import httpx
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(billing.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    app = make_app(tmp_path, stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000")
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        r = c.post("/api/billing/checkout")
        assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
        f = seen["form"]
        assert seen["path"] == "/v1/checkout/sessions" and f["mode"] == "subscription"
        assert f["client_reference_id"] == tid and f["line_items[0][price]"] == "price_core_1000"
        assert f["customer_email"] == "owner@acme.test" and seen["auth"].startswith("Basic ")
        assert c.post("/api/billing/portal").status_code == 409            # no customer until checkout completes


# ---------------------------------------------------------------------------- tools
def test_tool_listing_run_and_audit(client):
    signup(client)
    listing = client.get("/api/tools").json()
    names = {t["name"] for t in listing["tools"]}
    assert {"verify_contractor_license", "calculate_lien_deadline"} <= names and listing["count"] == len(names)
    spec = next(t for t in listing["tools"] if t["name"] == "calculate_lien_deadline")
    assert spec["input_schema"]["properties"]["claimant_role"] and spec["module"] == "payments"
    assert all(m["label"] and m["tools"] for m in listing["modules"])
    r = client.post("/api/tools/check_subcontractor_compliance", json={
        "license_number": "123456", "required_classification": "C-36", "liability_insurance_expiry": "2099-01-01"})
    assert r.status_code == 200 and r.json()["structured"]["overall_risk"] == "low"
    bad = client.post("/api/tools/calculate_lien_deadline", json={"claimant_role": "subcontractor",
                                                                   "last_furnishing_date": "May 1"})
    assert bad.status_code == 422
    assert client.post("/api/tools/no_such_tool", json={}).status_code == 404
    rows = client.get("/api/audit").json()
    assert [r["tool"] for r in rows[:2]] == ["calculate_lien_deadline", "check_subcontractor_compliance"]
    assert rows[1]["ok"] is True and rows[1]["args"]["license_number"] == "123456" and rows[0]["ok"] is False
    # save the result as a document
    res = r.json()["structured"]
    d = client.post("/api/documents/from_result", json={"tool": "check_subcontractor_compliance", "result": res,
                                                        "title": "Prequal: Example Plumbing"}).json()
    doc = client.get(f"/api/documents/{d['id']}").json()
    assert doc["title"] == "Prequal: Example Plumbing" and "Not yet verified" in doc["body_markdown"]
    assert res["disclaimer"] in doc["body_markdown"]


def test_markdown_result_fields_saved_verbatim(client):
    signup(client)
    md = "# Injury and Illness Prevention Program\n\n- Responsible person: Jane"
    d = client.post("/api/documents/from_result", json={"tool": "generate_iipp", "result": {
        "program_markdown": md, "verified": True, "disclaimer": "Not legal advice."}}).json()
    body = client.get(f"/api/documents/{d['id']}").json()["body_markdown"]
    # this result was typed by the client, not returned by a tool on this server, so its "verified" claim is dropped
    assert body.startswith(md) and "Not legal advice." in body and d["verified"] is None
    # markdown inside list items (e.g. one outline per safety program); one item can be saved on its own
    result = {"verified": False, "programs": [{"name": "IIPP", "document_outline_md": "# IIPP\n\n- a"},
                                              {"name": "Heat plan", "document_outline_md": "# Heat\n\n- b"}]}
    both = client.post("/api/documents/from_result", json={"tool": "generate_safety_programs", "result": result}).json()
    one = client.post("/api/documents/from_result", json={"tool": "generate_safety_programs", "result": result,
                                                         "field": "programs.1.document_outline_md", "title": "Heat"}).json()
    assert "# IIPP" in both["body_markdown"] and "# Heat" in both["body_markdown"]
    assert one["body_markdown"].startswith("# Heat") and "IIPP" not in one["body_markdown"]


# ---------------------------------------------------------------------------- dashboard + calendar
def test_dashboard_project_and_subs(client):
    signup(client, license_number="123456")
    today = logic.today_ca()
    first = (today - timedelta(days=5)).isoformat()
    soon = (today + timedelta(days=10)).isoformat()
    pid = client.post("/api/projects", json={
        "name": "Main St TI", "first_furnishing_date": first, "start_date": first,
        "completion_date": (today - timedelta(days=1)).isoformat(), "claimant_role": "subcontractor"}).json()["id"]
    client.post("/api/subs", json={"name": "Example Plumbing", "license_number": "123456",
                                   "required_class": "C-36", "coi_expiry": "2099-01-01", "w9_on_file": True})
    client.post("/api/subs", json={"name": "Lapsed Builders", "license_number": "999999", "coi_expiry": "2099-01-01"})
    client.post("/api/subs", json={"name": "Solo Roofing", "license_number": "777000", "required_class": "C-39",
                                   "coi_expiry": soon, "wc_required": False, "w9_on_file": True})
    d = client.get("/api/dashboard").json()
    assert d["company"]["rag"] == "green" and d["company"]["license"]["business_name"] == "Example Plumbing Inc."
    rag = {s["name"]: s["rag"] for s in d["subs"]}
    assert rag == {"Example Plumbing": "green", "Lapsed Builders": "red", "Solo Roofing": "red"}
    kinds = {i["kind"] for i in d["items"]}
    assert {"prelim_notice", "lien_recording", "lien_foreclosure", "coi_expiry", "toolbox_talk"} <= kinds
    prelim = next(i for i in d["items"] if i["kind"] == "prelim_notice")
    assert prelim["date"] == (today + timedelta(days=15)).isoformat() and prelim["rag"] == "amber"
    assert prelim["verified"] is False and prelim["project_id"] == pid           # lien rules not yet verified
    lien = next(i for i in d["items"] if i["kind"] == "lien_recording")
    assert lien["date"] == (today - timedelta(days=1) + timedelta(days=90)).isoformat()
    coi = next(i for i in d["items"] if i["kind"] == "coi_expiry")
    assert coi["sub_name"] == "Solo Roofing" and coi["rag"] == "amber" and coi["days_left"] == 10
    tb = next(i for i in d["items"] if i["kind"] == "toolbox_talk")
    assert tb["verified"] is True and tb["citations"][0]["citation"] == "8 CCR 1509(e)"
    assert [i["date"] for i in d["items"]] == sorted(i["date"] for i in d["items"])
    assert sum(d["counts"].values()) == len(d["items"])
    assert d["overall"]["red"] == d["counts"]["red"] + 2
    cal = client.get("/api/calendar.ics")
    assert cal.status_code == 200 and cal.text.startswith("BEGIN:VCALENDAR\r\n") and "BEGIN:VEVENT" in cal.text
    assert "Serve 20-day preliminary notice" in cal.text
    feed = client.get("/api/calendar/feed").json()["url"]
    anon = TestClient(client.app)
    path = feed.split("testserver", 1)[1]
    assert anon.get(path).status_code == 200
    assert anon.get(path[:-3] + "xyz").status_code == 404


def test_toolbox_overdue_and_field_log_sync_idempotent(client):
    signup(client)
    today = logic.today_ca()
    start = (today - timedelta(days=40)).isoformat()
    pid = client.post("/api/projects", json={"name": "Warehouse", "start_date": start}).json()["id"]
    tb = next(i for i in client.get("/api/dashboard").json()["items"] if i["kind"] == "toolbox_talk")
    assert tb["rag"] == "red" and tb["title"] == "No toolbox talk logged yet"
    logs = [{"client_uuid": "c0ffee00-0000-4000-8000-000000000001", "project_id": pid, "kind": "toolbox_talk",
             "date": today.isoformat(), "topic": "Heat illness", "attendees": [
                 {"name": "Luis", "signature": "data:image/png;base64,iVBORw0KGgo=", "signed_at": "2026-11-25T08:00"},
                 {"name": "Ana", "acknowledged": True}]},
            {"client_uuid": "c0ffee00-0000-4000-8000-000000000002", "project_id": pid, "kind": "inspection",
             "date": today.isoformat(), "topic": "Excavation daily inspection", "notes": "Shoring OK"},
            {"client_uuid": "bad-0000-0001", "kind": "incident", "date": "not a date"}]
    r = client.post("/api/field_logs/sync", json={"logs": logs}).json()
    assert (r["created"], r["duplicates"], r["errors"]) == (2, 0, 1)
    r2 = client.post("/api/field_logs/sync", json={"logs": logs[:2]}).json()
    assert (r2["created"], r2["duplicates"]) == (0, 2)
    assert [x["id"] for x in r2["results"]] == [x["id"] for x in r["results"][:2]]
    rows = client.get("/api/field_logs").json()
    assert len(rows) == 2
    full = client.get(f"/api/field_logs/{r['results'][0]['id']}").json()
    assert full["signatures"][0]["name"] == "Luis" and full["attendees"][1]["signed"] is True
    tb = next(i for i in client.get("/api/dashboard").json()["items"] if i["kind"] == "toolbox_talk")
    assert tb["rag"] == "green" and tb["title"] == "Next toolbox talk due"


# ---------------------------------------------------------------------------- CSV import
def test_csv_import_with_bom_and_blank_lines(client):
    signup(client)
    csv_text = ("﻿Subcontractor,License #,Scope,Required Class,COI Expiration,W-9\r\n"
                "\r\n"
                "Example Plumbing,123-456,Plumbing,C-36,12/31/2099,yes\r\n"
                ",,,,,\r\n"
                "Lapsed Builders,999999,Framing,B,2099-01-01,no\r\n"
                ",777000,Roofing,C-39,bad-date,\r\n"
                "\r\n")
    r = client.post("/api/subs/import", files={"file": ("subs.csv", csv_text.encode("utf-8"), "text/csv")})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 2 and out["updated"] == 0 and len(out["skipped"]) == 1
    assert "not a date" in out["skipped"][0]["reason"]
    subs = {s["name"]: s for s in client.get("/api/subs").json()}
    assert subs["Example Plumbing"]["license_number"] == "123456"
    assert subs["Example Plumbing"]["coi_expiry"] == "2099-12-31" and subs["Example Plumbing"]["w9_on_file"] is True
    assert {x["name"]: x["rag"] for x in out["report"]} == {"Example Plumbing": "green", "Lapsed Builders": "red"}
    # re-import updates instead of duplicating; a missing name is filled from CSLB data
    again = client.post("/api/subs/import", content="license_number,scope\n123456,Plumbing and gas\n777000,Roof\n",
                        headers={"content-type": "text/csv"}).json()
    assert again["updated"] == 1 and again["created"] == 1
    subs = {s["license_number"]: s for s in client.get("/api/subs").json()}
    assert len(subs) == 3 and subs["123456"]["scope"] == "Plumbing and gas"
    assert subs["777000"]["name"] == "Solo Roofing"
    bad = client.post("/api/subs/import", json={"csv": "foo,bar\n1,2\n"}).json()
    assert bad["created"] == 0 and "No 'name'" in bad["skipped"][0]["reason"]


def test_web_form_nulls_accepted(client):
    """The web app sends null for blank optional fields; length limits must not trip on them."""
    signup(client)
    r = client.post("/api/projects", json={"name": "Blank fields", "address": None, "city": "", "county": None,
                                           "owner_name": None, "lender_name": None, "contract_price": None,
                                           "first_furnishing_date": None, "status": "active"})
    assert r.status_code == 201 and r.json()["city"] is None
    pid = r.json()["id"]
    r = client.patch(f"/api/projects/{pid}", json={"name": "Renamed", "lender_name": None, "claimant_role": "supplier"})
    assert r.status_code == 200 and r.json()["extra"]["claimant_role"] == "supplier"
    assert client.post("/api/subs", json={"name": "S", "email": None, "phone": None, "dir_number": None,
                                          "coi_expiry": None, "project_id": None}).status_code == 201
    r = client.post("/api/field_logs/sync", json={"logs": [{"client_uuid": "abcdef0123456789", "kind": "inspection",
                                                            "date": "2026-11-20", "topic": None, "notes": None,
                                                            "project_id": None, "attendees": []}]})
    assert r.json()["created"] == 1
    assert client.post("/api/projects", json={"name": "x" * 201}).status_code == 422


def test_static_shell_and_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors 'none'" in r.headers[
        "content-security-policy"]
    assert client.get("/sw.js").status_code == 200 and client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/config").json()["mcp_url"].endswith("/mcp")


# ============================================================================ security regression tests (adversarial review)
def _login_temp(app, owner, email, role):
    """The owner invites someone, who joins through the invite link."""
    inv = owner.post("/api/users", json={"email": email, "role": role}).json()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/auth/invite/accept", json={"token": inv["invite_path"].rsplit("/", 1)[1], "password": PW})
    assert r.status_code == 200
    c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
    return c, inv


def test_deleted_users_cookie_does_not_become_the_next_user(app, backend):
    """SQLite reuses the highest rowid after a delete; a fired worker's cookie must not log in as the new hire."""
    with TestClient(app) as owner:
        signup(owner)
        fired, inv = _login_temp(app, owner, "fired@acme.test", "field")
        assert owner.delete(f"/api/users/{inv['id']}").status_code == 200
        hire = owner.post("/api/users", json={"email": "office@acme.test", "role": "office"}).json()
        if backend == "sqlite":
            assert hire["id"] == inv["id"]        # the id really is reused
        else:
            assert hire["id"] != inv["id"]        # Postgres identity columns never reuse an id
        assert fired.get("/api/me").status_code == 401
        assert fired.get("/api/keys").status_code == 401


def test_logout_revokes_the_session_server_side(client, app):
    signup(client)
    token = client.cookies.get("tc_session")
    assert client.post("/api/auth/logout").status_code == 200
    stolen = TestClient(app)
    stolen.cookies.set("tc_session", token)
    assert stolen.get("/api/me").status_code == 401


def test_removed_or_demoted_users_api_keys_are_revoked(app):
    with TestClient(app) as owner:
        signup(owner)
        office, inv = _login_temp(app, owner, "office@acme.test", "office")
        key = office.post("/api/keys", json={"name": "my agent"}).json()["key"]
        bot = TestClient(app)
        auth = {"authorization": f"Bearer {key}"}
        assert bot.get("/api/projects", headers=auth).status_code == 200
        assert owner.patch(f"/api/users/{inv['id']}", json={"role": "field"}).status_code == 200
        assert bot.get("/api/projects", headers=auth).status_code == 401
        office2, inv2 = _login_temp(app, owner, "office2@acme.test", "office")
        key2 = office2.post("/api/keys", json={"name": "agent 2"}).json()["key"]
        assert owner.delete(f"/api/users/{inv2['id']}").status_code == 200
        assert bot.get("/api/projects", headers={"authorization": f"Bearer {key2}"}).status_code == 401


def test_stripe_events_out_of_order_and_stale_subscriptions(tmp_path):
    app = make_app(tmp_path, stripe_webhook_secret="whsec_test")
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        store = app.state.platform.store

        def send(ev):
            body = json.dumps(ev).encode()
            r = c.post("/api/billing/webhook", content=body,
                       headers={"stripe-signature": billing.sign_payload(body, "whsec_test")})
            assert r.status_code == 200, r.text

        send({"id": "evt_1", "type": "checkout.session.completed", "created": 1000, "data": {"object": {
            "client_reference_id": tid, "customer": "cus_1", "subscription": "sub_1", "mode": "subscription",
            "payment_status": "paid"}}})
        send({"id": "evt_3", "type": "customer.subscription.deleted", "created": 3000, "data": {"object": {
            "id": "sub_1", "customer": "cus_1", "status": "canceled"}}})
        assert store.get_tenant(tid)["status"] == "canceled"
        # a delayed, older "active" update must not undo the cancellation
        send({"id": "evt_2", "type": "customer.subscription.updated", "created": 2000, "data": {"object": {
            "id": "sub_1", "customer": "cus_1", "status": "active"}}})
        assert store.get_tenant(tid)["status"] == "canceled"
        # resubscribe; then a stale subscription of the same customer is deleted: the new one stays active
        send({"id": "evt_4", "type": "checkout.session.completed", "created": 4000, "data": {"object": {
            "client_reference_id": tid, "customer": "cus_1", "subscription": "sub_2", "mode": "subscription",
            "payment_status": "paid"}}})
        send({"id": "evt_5", "type": "customer.subscription.deleted", "created": 5000, "data": {"object": {
            "id": "sub_old", "customer": "cus_1", "status": "canceled"}}})
        send({"id": "evt_6", "type": "invoice.payment_failed", "created": 5100, "data": {"object": {
            "customer": "cus_1", "subscription": "sub_old"}}})
        t = store.get_tenant(tid)
        assert t["status"] == "active" and t["stripe_subscription_id"] == "sub_2"


def test_mcp_batch_cannot_flood_the_audit_log(client, app):
    h = mcp_session(client)
    call = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "verify_contractor_license",
                                                                  "arguments": {"license_number": "1"}}}
    r = client.post("/mcp", headers=h, json=[{**call, "id": i} for i in range(2, 2002)])
    assert r.status_code in (400, 413, 429)
    with app.state.platform.store.connect() as con:
        assert con.execute("select count(*) from audit").fetchone()[0] <= 25


def test_anonymous_mcp_tool_calls_are_rate_limited(client, app):
    app.state.platform.mcp_anon_limiter.limit = 3
    h = mcp_session(client)
    codes = []
    for i in range(5):
        r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {
            "name": "verify_contractor_license", "arguments": {"license_number": "123456"}}})
        codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


def test_chunked_api_body_is_capped(tmp_path):
    app = make_app(tmp_path, max_body_bytes=64 * 1024)
    with TestClient(app, raise_server_exceptions=False) as c:
        signup(c)

        def chunks():
            for _ in range(64):
                yield b"x" * 4096                     # no Content-Length: streamed
        r = c.post("/api/subs/import", content=chunks(), headers={"content-type": "text/csv"})
        assert r.status_code == 413


def test_out_of_range_dates_rejected_and_dashboard_survives(client, app):
    """A field-role user must not be able to break the whole company's dashboard with a year-9999 log."""
    signup(client)
    pid = client.post("/api/projects", json={"name": "Job", "status": "active", "start_date": "2026-11-02"}).json()["id"]
    assert client.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "9999-12-31",
                                                "project_id": pid}).status_code == 422
    assert client.post("/api/projects", json={"name": "Old", "start_date": "0001-01-02"}).status_code == 422
    # bad data already in the database (e.g. from an old version) still can't crash the dashboard or calendar
    app.state.platform.store.insert("field_logs", client.get("/api/me").json()["tenant"]["id"],
                                    {"kind": "toolbox_talk", "date": "9999-12-31", "project_id": pid})
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/calendar.ics").status_code == 200


def test_tenant_settings_cannot_touch_reserved_keys(client):
    signup(client)
    first = client.get("/api/calendar/feed").json()["url"]
    rotated = client.post("/api/calendar/feed/rotate").json()["url"]
    assert first != rotated
    # an office user can't revive the old (leaked) feed link or break rotation through settings
    r = client.patch("/api/tenant", json={"settings": {"ics_gen": 0, "theme": "dark"}})
    assert r.status_code == 200 and r.json()["settings"].get("theme") == "dark"
    assert client.get("/api/calendar/feed").json()["url"] == rotated
    client.patch("/api/tenant", json={"settings": {"ics_gen": "zzz"}})
    assert client.post("/api/calendar/feed/rotate").status_code == 200
    old_path = "/" + first.split("://", 1)[1].split("/", 1)[1]
    assert TestClient(client.app).get(old_path).status_code == 404


def test_verified_badge_cannot_be_forged(client):
    signup(client)
    forged = client.post("/api/documents", json={"title": "fake", "body_markdown": "x", "verified": True,
                                                 "source_tool": "verify_contractor_license"}).json()
    assert forged["verified"] is None and forged["source_tool"] is None
    r = client.post("/api/documents/from_result", json={"tool": "verify_contractor_license",
                                                        "result": {"verified": True, "business_name": "Fake Co"}})
    assert r.status_code == 201 and r.json()["verified"] is None
    assert "Verified against official source" not in r.json()["body_markdown"]
    # a real result from the server keeps its flag, and editing the text drops it
    out = client.post("/api/tools/lookup_safety_standard", json={"hazard": "fall protection"}).json()
    assert out["structured"]["verified"] is True and out["result_sig"]
    doc = client.post("/api/documents/from_result", json={"tool": "lookup_safety_standard", "result": out["structured"],
                                                          "result_sig": out["result_sig"]}).json()
    assert doc["verified"] is True and doc["source_tool"] == "lookup_safety_standard"
    tampered = {**out["structured"], "summary": "No fall protection needed."}
    bad = client.post("/api/documents/from_result", json={"tool": "lookup_safety_standard", "result": tampered,
                                                          "result_sig": out["result_sig"]}).json()
    assert bad["verified"] is None
    edited = client.patch(f"/api/documents/{doc['id']}", json={"body_markdown": "# edited"}).json()
    assert edited["verified"] is None


def test_malformed_imports_get_422_not_500(client):
    signup(client)
    raw = TestClient(client.app, raise_server_exceptions=False)
    raw.cookies.update(client.cookies)
    raw.headers.update(client.headers)
    assert raw.post("/api/subs/import", json=[1, 2]).status_code == 422
    assert raw.post("/api/subs/import", json={"csv": "name\nA\n", "project_id": {"x": 1}}).status_code == 422
    assert raw.post("/api/subs/import", files={"file": ("a.csv", b"name\nx\n")},
                    data={"project_id": "abc"}).status_code == 422
    big_field = b"name,license\n" + b"a" * 200_000 + b",1\n"
    assert raw.post("/api/subs/import", content=big_field, headers={"content-type": "text/csv"}).status_code == 422


def test_sync_errors_do_not_leak_internals(client, monkeypatch):
    signup(client)
    store = client.app.state.platform.store

    def boom(*a, **k):
        raise RuntimeError("sqlite3 at /var/data/tradecomply.sqlite: disk I/O error")
    monkeypatch.setattr(store, "insert_field_log_once", boom)
    r = client.post("/api/field_logs/sync", json={"logs": [{"client_uuid": "abcdefgh1", "kind": "toolbox_talk",
                                                            "date": "2026-11-20"}]})
    assert r.status_code == 200
    err = r.json()["results"][0]["error"]
    assert "/var/data" not in err and "sqlite" not in err


def test_password_change_attempts_rate_limited(client):
    signup(client)
    codes = [client.post("/api/auth/password", json={"current_password": "wrong-wrong-wrong",
                                                     "new_password": "another-long-password"}).status_code
             for _ in range(7)]
    assert codes[:5] == [403] * 5 and 429 in codes[5:]


def test_rate_limiter_lockout_survives_key_flooding():
    from app.auth import RateLimiter
    rl = RateLimiter(limit=3, window_s=900, max_keys=100)
    for _ in range(3):
        rl.hit("email|victim@acme.test")
    for i in range(500):                         # attacker sprays junk keys to evict the lockout
        rl.hit(f"email|junk{i}@x.test")
    assert rl.retry_after("email|victim@acme.test") > 0
    assert len(rl._hits) <= 101


def test_short_secret_key_refused(tmp_path, monkeypatch):
    from app.auth import load_secret_key
    monkeypatch.setenv("SECRET_KEY", "changeme")
    with pytest.raises(RuntimeError):
        load_secret_key(str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    assert load_secret_key(str(tmp_path)) == b"k" * 40


def test_access_log_redacts_feed_and_login_tokens():
    import logging
    from app.main import RedactTokens
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/calendar.ics?tenant=t_1&token=abcdef0123456789", "1.1", 200), None)
    RedactTokens().filter(rec)
    assert "abcdef0123456789" not in rec.getMessage() and "tenant=t_1" in rec.getMessage()


def test_hsts_on_https(client):
    r = client.get("/", headers={"x-forwarded-proto": "https"})
    assert "max-age=" in r.headers.get("strict-transport-security", "")


def test_mcp_path_variants_all_gated(tmp_path):
    app = make_app(tmp_path, mcp_require_key=True)
    with TestClient(app) as c:
        for path in ("/mcp", "/mcp/", "/%6dcp", "/./mcp", "///mcp", "/mcp?x=1"):
            assert c.post(path, json=INIT, headers=MCP_HEADERS).status_code == 401, path
        for path in ("/MCP", "//mcp", "/mcp;x", "/Mcp/"):
            assert c.post(path, json=INIT, headers=MCP_HEADERS).status_code in (401, 404, 405), path
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/..%2fapp%2fconfig.py"):
            assert c.get(path).status_code == 404, path


def _node_md(payloads):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "app.js")
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const s = src.indexOf('  const esc = ');
const a = src.indexOf('  function inlineMd(');
const b = src.indexOf('  // ========', a + 10);
const md = new Function(src.slice(s, src.indexOf('\n', s)) + '\n' + src.slice(a, b) + '\nreturn md;')();
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(input.map((p) => md(p))));
"""
    out = subprocess.run([node, "-e", script, src], input=json.dumps(payloads), capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_markdown_renderer_never_emits_attributes_or_tags_from_input():
    import re as _re
    payloads = [
        "[a](https://b(https://x/autofocus/onfocus=alert)",
        "[a](https://b(https://x\"/onclick=alert)",
        "- [z](https://q(https://w/autofocus/onfocus=alert)",
        "| a | b |\n|---|---|\n| <script>alert(1)</script> | [q](https://a(https://b/x=1) |",
        "`[a](https://b(https://c)`", "**[a](https://b(https://c)**", "[x](javascript:alert(1))",
        "<img src=x onerror=alert(1)>", "[ https://evil.test](https://good.test)", "a \u00000\u0000 b",
        "[a](mailto:x(https://y/z)", "_[a](https://x/_y_)_ https://ok.test/_z_",
    ]
    allowed_tag = _re.compile(r'^</?(p|code|strong|em|h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|pre|blockquote|hr|br)>$'
                              r'|^<a href="(https?://|mailto:)[^"<>]*" target="_blank" rel="noopener noreferrer">$|^</a>$')
    for p, html in zip(payloads, _node_md(payloads)):
        for tag in _re.findall(r"<[^>]*>", html):
            assert allowed_tag.match(tag), (p, tag, html)
        assert html.count("<a ") == html.count("</a>"), (p, html)
        assert not _re.search(r"<a [^>]*>[^<]*<a ", html), (p, html)      # no nested anchors
    assert "https://good.test" in _node_md(["[ https://evil.test](https://good.test)"])[0]


def test_anonymous_audit_rows_are_trimmed(app):
    store = app.state.platform.store
    for i in range(30):
        store.audit(None, "anonymous (MCP)", "t", {"i": i}, None, source="mcp")
    keep = store.create_tenant("Keep Co", None, None)["id"]        # a company's rows are never trimmed
    store.audit(keep, "x", "t", {}, None)
    assert store.trim_anonymous_audit(10) == 20
    with store.connect() as con:
        assert con.execute("select count(*) from audit where tenant_id is null").fetchone()[0] == 10
        assert con.execute("select count(*) from audit where tenant_id = ?", (keep,)).fetchone()[0] == 1


def test_api_docs_page_is_same_origin(client):
    r = client.get("/api/docs")
    assert r.status_code == 200 and "/static/docs.js" in r.text and "cdn." not in r.text
    assert client.get("/api/openapi.json").json()["paths"]


def test_every_cookie_write_route_requires_csrf(app):
    """Walk the real route table: any state-changing /api route reached with a valid session cookie but no CSRF
    header must be refused (403), except the few that are deliberately cookie-less."""
    exempt = {"/api/auth/signup", "/api/auth/login", "/api/auth/logout", "/api/billing/webhook",
              "/api/auth/sso/start",                   # starts a login, like /api/auth/login (no session yet)
              # one-time links (app/auth_links.py): no session yet; JSON bodies only, so no other site can post them
              "/api/auth/forgot", "/api/auth/link", "/api/auth/reset", "/api/auth/invite/accept",
              "/api/integrations/twilio-sms/inbound"}  # called by Twilio, authenticated by X-Twilio-Signature
    with TestClient(app, raise_server_exceptions=False) as c:
        signup(c)
        c.headers.pop("x-csrf-token")
        checked = 0
        for route_path, ops in app.openapi()["paths"].items():      # every included route, any FastAPI version
            if not route_path.startswith("/api/") or route_path in exempt:
                continue
            for method in set(ops) - {"get", "head", "options"}:
                path = re.sub(r"\{[^}]+\}", "1", route_path)
                r = c.request(method.upper(), path, json={})
                assert r.status_code == 403, (method, route_path, r.status_code)
                checked += 1
        assert checked >= 20


def test_cross_tenant_ids_on_logs_keys_and_users(app):
    with TestClient(app) as a, browser(a) as b:
        signup(a, email="a@one.test", company="One")
        signup(b, email="b@two.test", company="Two")
        lid = a.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "2026-11-20"}).json()["id"]
        kid = a.post("/api/keys", json={"name": "A key"}).json()["id"]
        uid = a.post("/api/users", json={"email": "crew@one.test", "role": "field"}).json()["id"]
        did = a.post("/api/documents", json={"title": "A doc"}).json()["id"]
        assert b.get(f"/api/field_logs/{lid}").status_code == 404
        assert b.delete(f"/api/field_logs/{lid}").status_code == 404
        assert b.delete(f"/api/keys/{kid}").status_code == 404
        assert b.patch(f"/api/users/{uid}", json={"role": "owner"}).status_code == 404
        assert b.delete(f"/api/users/{uid}").status_code == 404
        assert b.patch(f"/api/documents/{did}", json={"title": "x"}).status_code == 404
        assert b.delete(f"/api/documents/{did}").status_code == 404
        assert b.get("/api/field_logs").json() == [] and b.get("/api/users").json()[0]["email"] == "b@two.test"
        assert not a.get("/api/keys").json()[0]["revoked"]
        assert a.get(f"/api/field_logs/{lid}").status_code == 200
        # B's sync can't attach logs to A's project; B can't read A's calendar with its own feed token
        pid = a.post("/api/projects", json={"name": "A job"}).json()["id"]
        r = b.post("/api/field_logs/sync", json={"logs": [{"client_uuid": "zzzzzzzz1", "kind": "toolbox_talk",
                                                           "date": "2026-11-20", "project_id": pid}]})
        assert r.json()["results"][0]["status"] == "error"
        b_feed = b.get("/api/calendar/feed").json()["url"]
        a_tid = a.get("/api/me").json()["tenant"]["id"]
        token = b_feed.split("token=")[1]
        assert TestClient(app).get(f"/api/calendar.ics?tenant={a_tid}&token={token}").status_code == 404


# ---------------------------------------------------------------------------- readability (UX review)
def test_tool_errors_read_plainly(client):
    signup(client)
    missing = client.post("/api/tools/calculate_lien_deadline", json={"claimant_role": "subcontractor"})
    assert missing.status_code == 422
    msg = missing.json()["detail"]
    assert msg == "Check these inputs. Last furnishing date: required."
    assert "pydantic" not in msg and "type=" not in msg and "Error calling tool" not in msg
    bad = client.post("/api/tools/calculate_lien_deadline", json={"claimant_role": "subcontractor",
                                                                   "last_furnishing_date": "May 1"}).json()["detail"]
    assert not bad.startswith("Error calling tool") and "last_furnishing_date" in bad


def test_saved_result_reads_like_a_report(client):
    signup(client)
    from app.api import result_to_markdown
    body = result_to_markdown("check_subcontractor_compliance", {
        "overall_risk": "high", "recommended_action": "Hold.", "insurance_current": False, "total_hours": 11.5,
        "bond_amount_usd": 25000.0, "status": "do_not_use", "expiration_date": "2027-01-31",
        "official_lookup_url": "https://www.cslb.ca.gov/x", "rule_ids": ["a-b-c"], "unverified": ["x_y"],
        "notes": ["Confirm data_source daily [daily-weekly-overtime-1-5x]."],
        "exposure": [{"item": "LC 210", "low_usd": 100, "rule_ids": ["late-payment-penalty-210"], "verified": True}],
        "license": {"verified": True, "disclaimer": "nested", "classifications": ["C10"]},
        "data_source": "CSLB master file (snapshot 2026-11-24)", "verified": False, "disclaimer": "Not legal advice."})
    assert "- **Risk:** High risk" in body and "- **What to do:** Hold." in body
    assert "**Insurance current:** No" in body and "True" not in body and "False" not in body
    assert "**Total hours:** 11.5" in body and "**Bond amount:** $25,000" in body
    assert "Jan 31, 2027" in body and "[cslb.ca.gov](https://www.cslb.ca.gov/x)" in body
    assert "rule" not in body.lower().replace("rules", "") and "daily-weekly" not in body and "x_y" not in body
    assert "“Data source”" in body and "as of Nov 24, 2026" in body
    assert "| Item | Low | Verified |" in body and "| LC 210 | $100 | Yes |" in body
    assert "nested" not in body and "**Classifications:** C10" in body and body.count("Not legal advice.") == 1


def test_gzip_only_on_public_responses(client):
    signup(client)
    gz = {"accept-encoding": "gzip"}
    assert client.get("/api/tools", headers=gz).headers.get("content-encoding") == "gzip"
    assert client.get("/static/app.js", headers=gz).headers.get("content-encoding") == "gzip"
    for path in ("/api/me", "/api/keys", "/api/dashboard", "/api/calendar/feed"):
        assert client.get(path, headers=gz).headers.get("content-encoding") is None, path
    key = client.post("/api/keys", json={"name": "x" * 600}, headers=gz)
    assert key.status_code in (200, 201, 422) and key.headers.get("content-encoding") is None


# ---------------------------------------------------------------------------- storage: same behaviour on both backends
def test_nul_characters_do_not_break_saving_login_or_sync(app):
    """Postgres text can't hold NUL (psycopg refuses it), SQLite can. A NUL pasted into a note, a company name or a
    login form must not be a 500 on one backend only: an offline field log with one would never sync."""
    with TestClient(app) as c:
        signup(c, company="Acme\x00Builders")
        r = c.post("/api/projects", json={"name": "Main\x00St", "address": "1 A\x00 St"})
        assert r.status_code == 201, r.text
        assert c.get(f"/api/projects/{r.json()['id']}").json()["name"] in ("Main\x00St", "Main�St")
        log = {"client_uuid": "nul-log-0001", "kind": "inspection", "date": "2026-11-20", "notes": "crack\x00ed",
               "topic": "Scaffold\x00", "attendees": [{"name": "Ana\x00", "acknowledged": True}]}
        assert c.post("/api/field_logs/sync", json={"logs": [log]}).json()["created"] == 1
        assert c.post("/api/field_logs/sync", json={"logs": [log]}).json()["duplicates"] == 1
        assert c.post("/api/documents", json={"title": "T\x00", "body_markdown": "x\x00y"}).status_code == 201
        assert c.patch("/api/tenant", json={"company_name": "Acme\x00 Inc", "settings": {"k\x00": "v\x00"}}
                       ).status_code == 200
    anon = TestClient(app)
    r = anon.post("/api/auth/login", json={"email": "owner\x00@acme.test", "password": PW})
    assert r.status_code == 401, r.text
    assert anon.post("/api/auth/signup", json={"company_name": "X", "email": "a\x00@b.test", "password": PW}
                     ).status_code in (200, 409, 422)
    assert anon.get("/api/calendar.ics", params={"tenant": "t_\x00", "token": "x"}).status_code == 404
    # and the NUL can't be used to reach a different account: owner\x00@acme.test is not owner@acme.test
    assert anon.post("/api/auth/login", json={"email": "owner@acme.test\x00", "password": PW}).status_code == 401


def test_signup_double_submit_makes_one_company(app):
    """Two sign-ups with the same email at the same moment both pass the "already exists" check. The company and
    its owner are created in one transaction, so the loser gets a 409 (not a 500) and leaves no ownerless company."""
    import threading
    store = app.state.platform.store
    n, codes = 6, []
    gate = threading.Barrier(n)

    def go(i):
        c = TestClient(app)
        gate.wait()
        codes.append(c.post("/api/auth/signup", json={"company_name": f"Co {i}", "password": PW,
                                                       "email": "Same@acme.test" if i % 2 else "same@acme.test"}
                            ).status_code)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] + [409] * (n - 1)
    assert store.count_tenants() == 1
    # the same race made deterministic: the pre-check misses the account created a moment ago
    real = store.user_by_email
    store.user_by_email = lambda email: None
    try:
        r = TestClient(app).post("/api/auth/signup", json={"company_name": "Late", "email": "SAME@acme.test",
                                                           "password": PW})
    finally:
        store.user_by_email = real
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    assert store.count_tenants() == 1
    with store.connect() as con:
        assert con.execute("select count(*) from tenants t where not exists "
                           "(select 1 from users u where u.tenant_id = t.id)").fetchone()[0] == 0


def test_invite_race_is_409_not_500(client, app, monkeypatch):
    signup(client)
    assert client.post("/api/users", json={"email": "crew@acme.test"}).status_code == 200
    monkeypatch.setattr(app.state.platform.store, "user_by_email", lambda email: None)
    r = client.post("/api/users", json={"email": "CREW@acme.test", "role": "office"})
    assert r.status_code == 409, r.text
    assert [u["email"] for u in client.get("/api/users").json()] == ["owner@acme.test", "crew@acme.test"]


def test_equal_dates_and_names_list_in_id_order(client):
    """Both backends return ties in the same order (newest first for dates, oldest first for names)."""
    signup(client)
    ids = [client.post("/api/field_logs", json={"kind": "inspection", "date": "2026-11-20", "topic": str(i)}
                       ).json()["id"] for i in range(6)]
    ids.append(client.post("/api/field_logs", json={"kind": "inspection", "date": "2026-11-21"}).json()["id"])
    got = [r["id"] for r in client.get("/api/field_logs").json()]
    assert got == [ids[-1]] + sorted(ids[:-1], reverse=True)
    subs = [client.post("/api/subs", json={"name": "Same Co"}).json()["id"] for _ in range(5)]
    first = client.post("/api/subs", json={"name": "Alpha"}).json()["id"]
    assert [s["id"] for s in client.get("/api/subs").json()] == [first] + sorted(subs)


def test_ids_beyond_64_bits_are_not_found(client):
    signup(client)
    big = 2 ** 63
    for path in ("projects", "subs", "documents", "field_logs", "audit"):
        assert client.get(f"/api/{path}/{big}").status_code == 404, path
    assert client.patch(f"/api/projects/{big}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/projects/{big}").status_code == 404
    assert client.patch(f"/api/users/{big}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/users/{big}").status_code == 404
    assert client.delete(f"/api/keys/{big}").status_code == 404
    assert client.post("/api/subs", json={"name": "X", "project_id": big}).status_code == 422
    assert client.get(f"/api/subs?project_id={big}").json() == []
    assert client.get(f"/api/documents?project_id={big}").json() == []
    r = client.post("/api/field_logs/sync", json={"logs": [{"client_uuid": "bigproject1", "kind": "inspection",
                                                            "date": "2026-11-20", "project_id": big}]})
    assert r.json()["results"][0]["error"] == "project not found"


def test_stale_stripe_event_cannot_overwrite_a_newer_status(app):
    """Two webhook deliveries handled at once both read the tenant before either writes. The older event's
    status must still lose: the "not older than the last applied event" check is made in the UPDATE itself."""
    store = app.state.platform.store
    tid = store.create_tenant("Acme", None, None)["id"]
    stale = store.get_tenant(tid)                                   # read by the delivery of the older event
    billing._set_status(store, store.get_tenant(tid), 200, status="canceled")
    billing._set_status(store, stale, 100, status="active", stripe_customer_id="cus_1")
    t = store.get_tenant(tid)
    assert (t["status"], t["stripe_event_created"], t["stripe_customer_id"]) == ("canceled", 200, "cus_1")
    billing._set_status(store, stale, 200, status="past_due")       # same second: applied, as before
    billing._set_status(store, stale, 0, status="active")           # no timestamp: applied, orders nothing
    t = store.get_tenant(tid)
    assert (t["status"], t["stripe_event_created"]) == ("active", 200)


def test_mcp_call_still_answers_when_the_audit_write_fails(client, app, monkeypatch):
    h = mcp_session(client)

    def down(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(app.state.platform.store, "audit", down)
    r = client.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "verify_contractor_license", "arguments": {"license_number": "123456"}}})
    assert r.status_code == 200
    assert sse_json(r.text)["result"]["structuredContent"]["business_name"] == "Example Plumbing Inc."


def test_health_is_503_when_storage_is_down(client, app, monkeypatch):
    def down():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(app.state.platform.store, "schema_version", down)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["ok"] is False and r.json()["error"] == "storage unavailable"
    assert "refused" not in r.text


def test_database_is_never_called_on_the_event_loop(tmp_path):
    """On Postgres every store call is a network round trip that may also wait for a pooled connection. Made on the
    event loop it stalls every other request (/health and /mcp too), so each one must run on a worker thread."""
    import asyncio
    import traceback
    secret = "whsec_loop"
    app = make_app(tmp_path, stripe_webhook_secret=secret)
    store = app.state.platform.store
    real, on_loop = store.connect, []

    def connect():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            on_loop.append(" < ".join(f.name for f in reversed(traceback.extract_stack()[-6:-1])))
        return real()
    store.connect = connect
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        signup(c)
        c.post("/api/auth/logout")
        assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}).status_code == 200
        c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
        c.get("/api/me")
        c.patch("/api/tenant", json={"company_name": "Acme 2"})
        uid = c.post("/api/users", json={"email": "crew@acme.test"}).json()["id"]
        c.patch(f"/api/users/{uid}", json={"role": "office"})
        c.get("/api/users")
        key = c.post("/api/keys", json={"name": "agent"}).json()
        c.get("/api/keys")
        pid = c.post("/api/projects", json={"name": "Main St", "last_furnishing_date": "2026-11-01"}).json()["id"]
        c.get("/api/projects")
        c.get(f"/api/projects/{pid}")
        c.patch(f"/api/projects/{pid}", json={"city": "Fresno"})
        c.get(f"/api/projects/{pid}/deadlines")
        sid = c.post("/api/subs", json={"name": "Sub", "license_number": "123456", "project_id": pid}).json()["id"]
        c.get("/api/subs?check=true")
        c.get(f"/api/subs/{sid}")
        c.get(f"/api/subs/{sid}/check")
        c.patch(f"/api/subs/{sid}", json={"scope": "plumbing"})
        c.post("/api/subs/import", json={"csv": "name,license_number\nCo,654321\n", "project_id": pid})
        out = c.post("/api/tools/verify_contractor_license", json={"license_number": "123456"}).json()
        did = c.post("/api/documents/from_result", json={"tool": "verify_contractor_license",
                                                         "result": out["structured"],
                                                         "result_sig": out.get("result_sig")}).json()["id"]
        c.post("/api/documents", json={"title": "Note"})
        c.get("/api/documents")
        c.get(f"/api/documents/{did}")
        c.patch(f"/api/documents/{did}", json={"title": "Renamed"})
        lid = c.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "2026-11-20", "project_id": pid}
                     ).json()["id"]
        c.post("/api/field_logs/sync", json={"logs": [{"client_uuid": "loop-sync-1", "kind": "inspection",
                                                        "date": "2026-11-20"}]})
        c.get("/api/field_logs")
        c.get(f"/api/field_logs/{lid}")
        c.get("/api/dashboard")
        feed = c.get("/api/calendar/feed").json()["url"]
        c.get("/api/calendar.ics")
        TestClient(app).get(feed.split("testserver", 1)[1])
        feed = c.post("/api/calendar/feed/rotate").json()["url"]
        c.get("/api/audit")
        c.get("/api/billing/status")
        c.post("/api/billing/checkout")
        c.post("/api/billing/portal")
        payload = json.dumps({"id": "evt_loop", "type": "customer.subscription.updated",
                              "data": {"object": {"id": "sub_x", "customer": "cus_x", "status": "active"}}}).encode()
        c.post("/api/billing/webhook", content=payload, headers={
            "stripe-signature": billing.sign_payload(payload, secret), "content-type": "application/json"})
        h = mcp_session(c, {"authorization": f"Bearer {key['key']}"})
        c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
            "name": "verify_contractor_license", "arguments": {"license_number": "123456"}}})
        c.get("/api/keys", headers={"authorization": f"Bearer {key['key']}"})
        c.delete(f"/api/field_logs/{lid}")
        c.delete(f"/api/documents/{did}")
        c.delete(f"/api/subs/{sid}")
        c.delete(f"/api/projects/{pid}")
        c.delete(f"/api/keys/{key['id']}")
        c.delete(f"/api/users/{uid}")
        assert c.post("/api/auth/password", json={"current_password": PW, "new_password": PW + "!"}
                      ).status_code == 200
    store.connect = real
    assert on_loop == []


def test_read_modify_writes_lock_the_row(app):
    """Handlers run on worker threads, so two requests can change one row at the same moment. Settings (including
    the calendar link generation) and a project's `extra` are merged under a row lock: the second change waits for
    the first and builds on it instead of writing back what it read before."""
    import threading
    store = app.state.platform.store
    tid = store.create_tenant("A", None, None)["id"]
    pid = store.insert("projects", tid, {"name": "Job", "extra": {"x": 0}})["id"]
    for modify, first, second, want in (
            (lambda f: store.modify_tenant_settings(tid, f),
             lambda cur: {**cur, "theme": "dark"},
             lambda cur: {**cur, "ics_gen": int(cur.get("ics_gen", 0)) + 1},
             lambda: store.get_tenant(tid)["settings"] == {"theme": "dark", "ics_gen": 1}),
            (lambda f: store.modify("projects", tid, pid, f),
             lambda row: {"extra": {**row["extra"], "claimant_role": "subcontractor"}},
             lambda row: {"extra": {**row["extra"], "x": row["extra"]["x"] + 1}},
             lambda: store.get("projects", tid, pid)["extra"] == {"x": 1, "claimant_role": "subcontractor"})):
        inside, release = threading.Event(), threading.Event()

        def slow(cur, first=first):
            inside.set()
            assert release.wait(10)
            return first(cur)
        t1 = threading.Thread(target=modify, args=(slow,))
        t1.start()
        assert inside.wait(10)
        t2 = threading.Thread(target=modify, args=(second,))
        t2.start()
        t2.join(0.5)
        assert t2.is_alive()          # waiting for the first change's lock, not working from a stale read
        release.set()
        t1.join(10)
        t2.join(10)
        assert want()


def test_concurrent_settings_saves_and_feed_rotations_all_land(app):
    import threading
    with TestClient(app) as owner:
        signup(owner)
        cookies, csrf = dict(owner.cookies), owner.headers["x-csrf-token"]
    errors = []

    def go(i):
        c = TestClient(app, cookies=cookies, headers={"x-csrf-token": csrf})
        r = (c.post("/api/calendar/feed/rotate") if i % 3 == 0 else
             c.patch("/api/tenant", json={"settings": {f"k{i}": i}}))
        if r.status_code != 200:
            errors.append(r.text)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    settings = app.state.platform.store.get_tenant(signup_tid(app))["settings"]
    assert {k: v for k, v in settings.items() if k != "ics_gen"} == {f"k{i}": i for i in range(15) if i % 3}
    assert settings["ics_gen"] == 5


def signup_tid(app) -> str:
    return app.state.platform.store.first_owner()["tenant_id"]
