"""Account data rights (app/account.py): GET /api/tenant/export and DELETE /api/tenant, Store.export_rows and
Store.delete_tenant, and the operator CLI (python -m app.account). Every test runs on SQLite and, with
TC_TEST_DATABASE_URL, on PostgreSQL (tests/support.py)."""
import base64
import hashlib
import json
import logging
import os
import stat
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import account
from app import connectors as registry
from app.connectors import ConfigField, Connector, ConnectorContext, OAuth2Spec, TestResult
from app.crypto import connection_context, sso_context, webhook_context
from app.store import (APP_TABLES, SECRET_BLOB_COLUMNS, SHARED_TABLES, TENANT_DELETE_ORDER, TENANT_EXPORT,
                       TENANT_EXPORT_OMITTED)
from tests import support
from tests.support import (PW, backend, browser, login, make_app, mock_http, oauth_connect,  # noqa: F401 - autouse
                           signup)

SIGNATURE = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6"
             "kgAAAABJRU5ErkJggg==")
SECTION_KEYS = [key for key, _, _ in account.SECTIONS]


# ---------------------------------------------------------------------------- helpers
def columns(con, table: str) -> list[str]:
    cur = con.execute(f"select * from {table} where 1 = 0")
    return [getattr(d, "name", None) or d[0] for d in cur.description]


def tables_here(store) -> list[str]:
    """APP_TABLES as they exist on this backend (schema_version is a table on PostgreSQL only)."""
    return [t for t in APP_TABLES if not (t == "schema_version" and store.backend == "sqlite")]


def delete(c, name="Acme Builders", password=PW, **extra):
    return c.request("DELETE", "/api/tenant", json={"confirm_company_name": name, "password": password, **extra})


def populate(c, platform, tid: str, tag: str) -> dict:
    """A row in every company table, through the API where there is one, marked with `tag`."""
    store, box = platform.store, platform.secret_box
    low = tag.lower()
    p = c.post("/api/projects", json={"name": f"{tag} project", "city": "San Diego", "owner_name": f"{tag} owner"})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    assert c.post("/api/subs", json={"name": f"{tag} sub", "license_number": "123456", "project_id": pid,
                                     "email": f"sub@{low}.example.com", "phone": "555-0100"}).status_code == 201
    assert c.post("/api/documents", json={"title": f"{tag} doc", "body_markdown": f"{tag} body",
                                          "project_id": pid}).status_code == 201
    r = c.post("/api/field_logs", json={"kind": "toolbox_talk", "date": "2026-11-02", "topic": f"{tag} talk",
                                        "project_id": pid,
                                        "attendees": [{"name": f"{tag} worker", "signature": SIGNATURE}]})
    assert r.status_code == 201, r.text
    key = c.post("/api/keys", json={"name": f"{tag} key"}).json()
    office_email = f"office@{low}.example.com"
    invited = c.post("/api/users", json={"email": office_email, "role": "office"}).json()
    field_email = f"field@{low}.example.com"
    field = c.post("/api/users", json={"email": field_email, "role": "field"}).json()
    for inv in (invited, field):                 # both join through their invite links, with the password PW
        r = TestClient(c.app).post("/api/auth/invite/accept", json={"token": inv["invite_path"].rsplit("/", 1)[1],
                                                                    "password": PW})
        assert r.status_code == 200, r.text
    assert c.post(f"/api/users/{invited['id']}/reset-link").status_code == 200    # a one-time link left open
    store.save_connection(tid, "acme_crm", status="connected", config={"account": f"{tag} CRM"},
                          external_account=f"{tag} CRM account",
                          secret_blob=box.encrypt_json({"api_key": f"{tag}-crm-secret"},
                                                       connection_context(tid, "acme_crm")))
    hook = store.create_webhook(tid, name=f"{tag} hook", kind="generic", events=["*"],
                                url=box.encrypt(f"https://hooks.example.com/{low}/secret-path", webhook_context(tid)),
                                signing_secret_blob=box.encrypt(f"{tag}-signing-secret", webhook_context(tid)))
    store.enqueue_outbound(tid, "deadline_due_soon", {"title": f"{tag} alert"}, [f"webhook:{hook['id']}"])
    store.log_mcp_call(tid, "verify_contractor_license", "ok", api_key_id=key["id"],
                       args_sha256=hashlib.sha256(tag.encode()).hexdigest(), ip="203.0.113.7", duration_ms=12)
    store.save_sso_config(tid, issuer=f"https://login.{low}.example.com", client_id=f"{tag}-client",
                          secret_blob=box.encrypt(f"{tag}-sso-secret", sso_context(tid)),
                          allowed_domains=[f"{low}.example.com"])
    store.add_calendar_token(tid, hashlib.sha256(f"{tag}-calendar-token".encode()).hexdigest())
    owner_id = c.get("/api/me").json()["user"]["id"]                # an Ask TradeComply chat, a turn and its usage
    chat = store.create_assistant_conversation(tid, owner_id, 50)
    store.append_assistant_turn(tid, owner_id, chat["id"], messages=[{"role": "user", "content": f"{tag} raw"}],
                                prompt=f"{tag} question", reply=f"{tag} answer", checked=[], outcome="answered",
                                title=f"{tag} question")
    store.add_assistant_usage(tid, store.start_assistant_usage(tid, owner_id, "claude-opus-5-5"), input_tokens=10,
                              output_tokens=5)
    connected = oauth_connect(c)                     # a connected app (app/oauth.py): a grant and its tokens
    app_tokens = connected["tokens"]
    return {"key": key["key"], "key_id": key["id"], "office_email": office_email, "app_tokens": app_tokens,
            "app_client_id": connected["client"]["client_id"],
            "office_password": PW, "field_email": field_email, "field_password": PW,
            "plain_secrets": [key["key"], f"{tag}-crm-secret", f"https://hooks.example.com/{low}/secret-path",
                              f"{tag}-signing-secret", f"{tag}-sso-secret", app_tokens["access_token"],
                              app_tokens["refresh_token"]]}


def stored_secrets(store, tid: str) -> list[str]:
    """Every hash and encrypted blob this company has in the database, read directly."""
    out = []
    with store.connect() as con:
        for table, cols in (("users", ("pw_hash",)), ("api_keys", ("key_hash",)),
                            ("calendar_tokens", ("token_hash",)), ("oauth_tokens", ("token_hash",)),
                            ("oauth_grants", ("code_challenge",)), ("auth_links", ("token_hash",)),
                            *SECRET_BLOB_COLUMNS.items()):
            for col in cols:
                out += [r[0] for r in con.execute(f"select {col} from {table} where tenant_id = ?", (tid,)).fetchall()
                        if r[0]]
    return out


def walk_keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_keys(v)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path, app_url="https://testserver")      # an https APP_URL: needed for connected apps


@pytest.fixture
def two(app):
    """Two companies with a row in every table: (platform, (client, tenant id, info) for Acme and for Bravo)."""
    platform = app.state.platform
    with TestClient(app) as a, browser(a) as b:
        ta = signup(a)["tenant_id"]
        tb = signup(b, email="owner@bravo.test", company="Bravo Electric", license_number="654321")["tenant_id"]
        yield platform, (a, ta, populate(a, platform, ta, "Alpha")), (b, tb, populate(b, platform, tb, "Bravo"))


# ---------------------------------------------------------------------------- the export
def test_export_has_this_company_rows_and_no_secrets(two):
    platform, (a, ta, ia), (b, tb, ib) = two
    store = platform.store
    r = a.get("/api/tenant/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"].startswith('attachment; filename="tradecomply-company-data-')
    assert r.headers["cache-control"] == "no-store" and "content-encoding" not in r.headers
    text = r.text
    data = json.loads(text)
    assert data["format"] == account.FORMAT and data["complete"] is True and data["company_id"] == ta
    assert data["company"]["id"] == ta and data["company"]["company_name"] == "Acme Builders"
    assert data["exported_by"] == "owner@acme.test"
    # every section is there, holds exactly this company's rows, and its count matches the database
    db = store.tenant_row_counts(ta)
    for key, table, _ in account.SECTIONS:
        assert isinstance(data[key], list) and len(data[key]) == data["row_counts"][key] == db[table] > 0, key
    assert {u["email"] for u in data["team_members"]} == {"owner@acme.test", ia["office_email"], ia["field_email"]}
    assert [p["name"] for p in data["projects"]] == ["Alpha project"]
    assert [s["name"] for s in data["subcontractors"]] == ["Alpha sub"]
    assert data["field_logs"][0]["signatures"][0]["image"] == SIGNATURE            # their data, in full
    assert data["field_logs"][0]["signatures"][0]["name"] == "Alpha worker"
    assert data["integrations"][0]["config"] == {"account": "Alpha CRM"} and data["integrations"][0]["has_secret"]
    assert data["webhooks"][0]["has_signing_secret"] is True and data["webhooks"][0]["events"] == ["*"]
    assert data["single_sign_on"][0]["allowed_domains"] == ["alpha.example.com"]
    assert data["calendar_links"] == [{"created": data["calendar_links"][0]["created"], "revoked": False}]
    assert data["ai_assistant_calls"][0]["ip"] == "203.0.113.7"
    assert data["api_keys"][0]["prefix"] == ia["key"][:12] and data["api_keys"][0]["revoked"] is False
    assert any(row["tool"] == "tenant.export" for row in data["activity_log"])          # the download is logged
    # nothing of the other company
    for marker in ("Bravo", "bravo", tb, ib["key"][:12]):
        assert marker not in text, marker
    # no credentials: no hash or encrypted blob (not even the calendar link id), no plaintext secret, no such field
    secrets = stored_secrets(store, ta)
    assert len(secrets) >= 9
    for value in secrets + [s[:12] for s in secrets if len(s) == 64] + ia["plain_secrets"]:
        assert value not in text
    rows = [row for key in SECTION_KEYS for row in data[key]] + [data["company"]]
    forbidden = {"pw_hash", "session_gen", "key_hash", "secret_blob", "signing_secret_blob", "url", "token_hash",
                 "tenant_id", "stripe_event_created"}
    assert not forbidden & set(walk_keys(rows))
    assert "pw_hash" in data["about"]["fields_left_out"]["team_members"]          # and the file says what's left out


def test_export_of_the_other_company_is_isolated_too(two):
    platform, (a, ta, ia), (b, tb, ib) = two
    data = b.get("/api/tenant/export").json()
    assert data["company_id"] == tb and [p["name"] for p in data["projects"]] == ["Bravo project"]
    text = json.dumps(data)
    for marker in ("Alpha", "alpha", "Acme", ta, ia["key"][:12]):
        assert marker not in text, marker


def test_every_column_is_exported_or_left_out_on_purpose(app):
    store = app.state.platform.store
    with store.connect() as con:
        for table, cols in TENANT_EXPORT.items():
            actual = set(columns(con, table))
            omitted = set(TENANT_EXPORT_OMITTED.get(table, {}))
            assert not set(cols) & omitted, table
            assert actual == set(cols) | omitted, f"{table}: classify {sorted(actual ^ (set(cols) | omitted))}"
    for table, cols in SECRET_BLOB_COLUMNS.items():
        assert set(cols) <= set(TENANT_EXPORT_OMITTED[table])
    for table, col in (("users", "pw_hash"), ("users", "session_gen"), ("api_keys", "key_hash"),
                       ("calendar_tokens", "token_hash"), ("oauth_tokens", "token_hash"),
                       ("oauth_grants", "code_challenge"), ("auth_links", "token_hash"),
                       ("auth_links", "session_gen")):
        assert col in TENANT_EXPORT_OMITTED[table]
    # every table is either the company's own (deleted with it) or shared and company-free
    assert set(TENANT_DELETE_ORDER) | set(SHARED_TABLES) | {"tenants"} == set(APP_TABLES)


def test_a_canceled_or_expired_company_can_still_export_and_delete(app):
    """Data stays readable and exportable after a trial or subscription ends (app/billing.py)."""
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        store.update_tenant(tid, status="canceled")
        assert c.post("/api/tools/verify_contractor_license", json={"license_number": "123456"}).status_code == 402
        assert c.get("/api/tenant/export").json()["company"]["status"] == "canceled"
        store.update_tenant(tid, status="trialing", trial_ends="2020-01-01T00:00:00+00:00")
        assert c.get("/api/tenant/export").status_code == 200
        assert delete(c).status_code == 200 and store.get_tenant(tid) is None


def test_export_pages_through_large_tables(app):
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        with store.connect() as con:
            for i in range(1205):
                con.execute('insert into audit (tenant_id, "user", tool, args_json, verified, ok, source, created) '
                            "values (?,?,?,?,?,?,?,?)", (tid, "x", f"tool.{i}", "{}", None, 1, "api", "2026-01-01"))
        for i in range(45):
            store.insert("field_logs", tid, {"kind": "inspection", "date": "2026-11-01", "topic": f"t{i}"})
        data = c.get("/api/tenant/export").json()
        counts = store.tenant_row_counts(tid)
        assert len(data["activity_log"]) == counts["audit"] >= 1206
        assert len(data["field_logs"]) == 45
        for key in ("activity_log", "field_logs"):
            ids = [row["id"] for row in data[key]]
            assert ids == sorted(set(ids))                                  # oldest first, no repeats


def test_export_is_owner_session_only_and_rate_limited(two):
    platform, (a, ta, ia), _ = two
    with browser(a) as office, browser(a) as field, browser(a) as anon:
        login(office, ia["office_email"], ia["office_password"])
        login(field, ia["field_email"], ia["field_password"])
        assert office.get("/api/tenant/export").status_code == 403
        assert field.get("/api/tenant/export").status_code == 403
        assert anon.get("/api/tenant/export").status_code == 401
        r = anon.get("/api/tenant/export", headers={"authorization": f"Bearer {ia['key']}"})
        assert r.status_code == 403 and "API key" in r.json()["detail"]
    for _ in range(account.EXPORT_LIMIT):
        assert a.get("/api/tenant/export").status_code == 200
    r = a.get("/api/tenant/export")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    exports = [row for row in platform.store.list_audit(ta, 1000) if row["tool"] == "tenant.export"]
    assert len(exports) == account.EXPORT_LIMIT and exports[0]["user"] == "owner@acme.test"


# ---------------------------------------------------------------------------- deleting the company
def test_delete_refuses_wrong_name_wrong_password_missing_csrf_and_non_owners(two):
    platform, (a, ta, ia), (b, tb, _) = two
    store = platform.store
    before = store.tenant_row_counts(ta)
    r = delete(a, name="Acme Builder")
    assert r.status_code == 422 and "Nothing was deleted" in r.json()["detail"]
    assert delete(a, name="acme builders").status_code == 422                     # exact, not case-insensitive
    assert delete(a, name="Bravo Electric").status_code == 422                     # another company's name
    r = delete(a, password="not-the-password")
    assert r.status_code == 403 and "Nothing was deleted" in r.json()["detail"]
    assert delete(a, extra_field=1).status_code == 422
    assert a.request("DELETE", "/api/tenant", json={"confirm_company_name": "Acme Builders"}).status_code == 422
    with browser(a) as no_csrf:                                # the owner's cookies, but no CSRF header
        no_csrf.cookies.update(a.cookies)
        r = delete(no_csrf)
        assert r.status_code == 403 and "CSRF" in r.json()["detail"]
        no_csrf.headers["x-csrf-token"] = "forged"
        assert delete(no_csrf).status_code == 403
    with browser(a) as office, browser(a) as anon:
        login(office, ia["office_email"], ia["office_password"])
        assert delete(office, password=ia["office_password"]).status_code == 403
        assert delete(anon).status_code == 401
        r = anon.request("DELETE", "/api/tenant", headers={"authorization": f"Bearer {ia['key']}"},
                         json={"confirm_company_name": "Acme Builders", "password": PW})
        assert r.status_code == 403 and "API key" in r.json()["detail"]
    assert store.tenant_row_counts(ta) == {**before, "audit": store.tenant_row_counts(ta)["audit"]}
    assert store.get_tenant(ta) and a.get("/api/me").status_code == 200


def test_wrong_passwords_lock_the_deletion_out(app):
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        for _ in range(5):
            assert delete(c, password="wrong-wrong-wrong").status_code == 403
        r = delete(c)                                  # even the right password, until the window passes
        assert r.status_code == 429 and r.headers["retry-after"]
        assert app.state.platform.store.get_tenant(tid)


def test_delete_removes_every_company_row_and_leaves_the_other_company(two):
    platform, (a, ta, ia), (b, tb, ib) = two
    store = platform.store
    with browser(a) as office, browser(a) as anon:
        login(office, ia["office_email"], ia["office_password"])
        feed = urlsplit(a.get("/api/calendar/feed").json()["url"])
        assert anon.get(f"{feed.path}?{feed.query}").status_code == 200
        b_export = b.get("/api/tenant/export").json()
        before_a, before_b = store.tenant_row_counts(ta), store.tenant_row_counts(tb)
        assert all(before_a.values()) and all(before_b.values())          # a row in every company table

        r = delete(a)
        assert r.status_code == 200, r.text
        receipt = r.json()
        assert receipt["ok"] and receipt["deleted"] and receipt["company_id"] == ta and receipt["signed_out"]
        assert receipt["company_name"] == "Acme Builders" and receipt["billing"]["result"] == "no_subscription"
        assert receipt["removed"]["company"] == 1 and receipt["removed"]["team_members"] == 3
        assert receipt["integrations"] == [{"provider": "acme_crm", "name": "acme_crm", "result": "not_supported",
                                            "message": receipt["integrations"][0]["message"]}]
        cookies = r.headers.get_list("set-cookie")
        assert any(h.startswith("tc_session=") and "Max-Age=0" in h for h in cookies)
        assert any(h.startswith("tc_csrf=") and "Max-Age=0" in h for h in cookies)

        # not one row of the company is left in any table, whether or not it has ON DELETE CASCADE
        with store.connect() as con:
            for table in tables_here(store):
                cols = columns(con, table)
                if table == "tenants":
                    n = con.execute("select count(*) from tenants where id = ?", (ta,)).fetchone()[0]
                elif "tenant_id" in cols:
                    n = con.execute(f"select count(*) from {table} where tenant_id = ?", (ta,)).fetchone()[0]
                else:                    # shared tables: nothing in them names the company
                    assert table in SHARED_TABLES
                    n = sum(ta in json.dumps([str(v) for v in row]) for row in
                            con.execute(f"select * from {table}").fetchall())
                assert n == 0, table
        assert not any(store.tenant_row_counts(ta).values())

        # the other company is untouched, row for row
        assert store.tenant_row_counts(tb) == before_b
        assert [p["name"] for p in b.get("/api/projects").json()] == ["Bravo project"]
        after = b.get("/api/tenant/export").json()
        for key in SECTION_KEYS:
            if key != "activity_log":            # the second download adds its own activity-log row
                assert after[key] == b_export[key], key

        # every way in is closed: sessions (the owner's and a team member's), API keys, the calendar feed link
        assert a.get("/api/me").status_code == 401
        assert office.get("/api/me").status_code == 401
        assert anon.get("/api/projects", headers={"authorization": f"Bearer {ia['key']}"}).status_code == 401
        assert anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"authorization": f"Bearer {ia['app_tokens']['access_token']}",
                                  "accept": "application/json, text/event-stream"}).status_code == 401
        r = anon.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": ia["app_client_id"],
                                            "refresh_token": ia["app_tokens"]["refresh_token"]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
        assert anon.get(f"{feed.path}?{feed.query}").status_code == 404
        # the email address is free: the old password no longer works and the address can sign up again
        assert anon.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}).status_code == 401
        assert anon.post("/api/auth/signup", json={"company_name": "Acme Builders", "email": "owner@acme.test",
                                                   "password": PW}).status_code == 200


def stripe_mock(monkeypatch, handler) -> list:
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    real = httpx.AsyncClient
    monkeypatch.setattr(account.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(wrapped), **kw))
    return seen


def subscribed(app, c, status="active") -> str:
    tid = signup(c)["tenant_id"]
    app.state.platform.store.update_tenant(tid, stripe_customer_id="cus_123", stripe_subscription_id="sub_123",
                                           status=status)
    return tid


def test_delete_cancels_the_stripe_subscription_when_stripe_is_set_up(tmp_path, monkeypatch):
    seen = stripe_mock(monkeypatch, lambda req: httpx.Response(200, json={"id": "sub_123", "object": "subscription",
                                                                          "status": "canceled"}))
    app = make_app(tmp_path, stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000")
    with TestClient(app) as c:
        tid = subscribed(app, c)
        r = delete(c)
        assert r.status_code == 200, r.text
        assert r.json()["billing"]["result"] == "canceled"
        assert any("Stripe keeps its own records" in k for k in r.json()["kept_elsewhere"])
    assert [(q.method, q.url.host, q.url.path) for q in seen] == [
        ("DELETE", "api.stripe.com", "/v1/subscriptions/sub_123")]
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"sk_test_123:").decode()
    assert app.state.platform.store.get_tenant(tid) is None


@pytest.mark.parametrize("failure", ["unreachable", "http_500", "invalid_url"])
def test_a_stripe_outage_never_blocks_the_deletion(tmp_path, monkeypatch, caplog, failure):
    def handler(request):
        if failure == "unreachable":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "invalid_url":            # not an httpx.HTTPError: e.g. a malformed STRIPE_API_BASE
            raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")
        return httpx.Response(500, json={"error": {"message": "internal"}})
    seen = stripe_mock(monkeypatch, handler)
    app = make_app(tmp_path, stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000",
                   support_email="help@example.com")
    caplog.set_level(logging.WARNING, logger="tradecomply.account")
    with TestClient(app) as c:
        tid = subscribed(app, c)
        r = delete(c)
        assert r.status_code == 200, r.text
        billing = r.json()["billing"]
        assert billing["result"] == "failed"
        assert billing["reason"] == ("stripe_http_500" if failure == "http_500" else "stripe_unreachable")
        assert "Invalid" not in json.dumps(r.json()) and "internal" not in json.dumps(r.json())   # no error text
        assert "help@example.com" in billing["message"] and "charged again" in billing["message"]
    assert len(seen) == 1 and app.state.platform.store.get_tenant(tid) is None
    logged = " ".join(rec.getMessage() for rec in caplog.records)
    assert "sub_123" in logged and "not canceled" in logged and tid in logged       # recorded for the operator


def test_stripe_says_already_canceled_or_is_not_set_up(tmp_path, monkeypatch):
    seen = stripe_mock(monkeypatch, lambda req: httpx.Response(404, json={"error": {"code": "resource_missing"}}))
    app = make_app(tmp_path, stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000")
    with TestClient(app) as c:
        subscribed(app, c, status="canceled")
        assert delete(c).json()["billing"]["result"] == "already_canceled"
    assert len(seen) == 1

    seen.clear()
    app2 = make_app(tmp_path / "nostripe")                                  # no STRIPE_SECRET_KEY on this server
    with TestClient(app2) as c:
        subscribed(app2, c)
        billing = delete(c).json()["billing"]
        assert billing["result"] == "not_set_up" and "whoever runs this TradeComply server" in billing["message"]
    assert seen == []

    app3 = make_app(tmp_path / "trial", stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000")
    with TestClient(app3) as c:
        signup(c)                                                           # a trial: no subscription at all
        assert delete(c).json()["billing"]["result"] == "no_subscription"
    assert seen == []


def test_a_failed_database_deletion_deletes_nothing_and_says_so(tmp_path, monkeypatch):
    stripe_mock(monkeypatch, lambda req: httpx.Response(200, json={"id": "sub_123", "status": "canceled"}))
    app = make_app(tmp_path, stripe_secret_key="sk_test_123", stripe_price_id="price_core_1000")
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = subscribed(app, c)
        before = store.tenant_row_counts(tid)

        def down(_tid):
            raise RuntimeError("database went away")
        monkeypatch.setattr(store, "delete_tenant", down)
        r = delete(c)
        assert r.status_code == 503
        assert "nothing was deleted" in r.json()["detail"] and "already canceled at Stripe" in r.json()["detail"]
        assert store.tenant_row_counts(tid) == before and c.get("/api/me").status_code == 200   # still signed in


def test_receipt_mentions_webhooks_left_in_other_products(two):
    _, (a, _, _), _ = two
    kept = delete(a).json()["kept_elsewhere"]
    assert any("Webhook addresses" in k for k in kept)


class RevokeOAuth(Connector):
    provider = "acct_oauth_test"
    display_name = "Revoke Test"
    catalog_id = "acct-oauth-test"
    description = "OAuth test connector with a revocation endpoint."
    docs_url = "https://docs.example.com/oauth"
    required_env = ("ACCT_CLIENT_ID", "ACCT_CLIENT_SECRET")
    oauth = OAuth2Spec(authorize_url="https://login.example.com/oauth/authorize",
                       token_url="https://login.example.com/oauth/token", client_id_env="ACCT_CLIENT_ID",
                       client_secret_env="ACCT_CLIENT_SECRET", revoke_url="https://login.example.com/oauth/revoke")

    def test(self):
        return TestResult(True, "ok")


class KeyOnly(Connector):
    provider = "acct_key_test"
    display_name = "Key Test"
    catalog_id = "acct-key-test"
    description = "API-key test connector (no way to revoke from here)."
    docs_url = "https://docs.example.com/keys"
    config_fields = (ConfigField("api_key", "API key", type="password", secret=True),)

    def test(self):
        return TestResult(True, "ok")


class Broken(KeyOnly):
    provider = "acct_broken_test"
    display_name = "Broken Test"
    catalog_id = "acct-broken-test"

    def revoke(self):
        raise RuntimeError("provider down")


@pytest.fixture
def test_connectors():
    for cls in (RevokeOAuth, KeyOnly, Broken):
        registry.register(cls)
    yield
    for cls in (RevokeOAuth, KeyOnly, Broken):
        registry.unregister(cls.provider)


def test_delete_revokes_integration_access_best_effort(app, test_connectors):
    platform = app.state.platform
    platform.env = {"ACCT_CLIENT_ID": "cid-1", "ACCT_CLIENT_SECRET": "csecret-1"}
    seen = mock_http(platform, lambda req: httpx.Response(200))
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        ctx = ConnectorContext(platform, tid)
        RevokeOAuth(ctx).save(status="connected", secrets={"oauth": {"access_token": "at-9", "refresh_token": "rt-9"}})
        KeyOnly(ctx).save(status="connected", secrets={"api_key": "k-123"})
        Broken(ctx).save(status="connected", secrets={"api_key": "k-456"})
        platform.store.save_connection(tid, "acct_plain_test", status="connected")      # nothing secret: skipped
        r = delete(c)
        assert r.status_code == 200, r.text
        results = {i["provider"]: i["result"] for i in r.json()["integrations"]}
        assert results == {"acct_oauth_test": "revoke_requested", "acct_key_test": "not_supported",
                           "acct_broken_test": "failed"}
        assert all("TradeComply" in i["message"] for i in r.json()["integrations"])
    assert len(seen) == 1 and seen[0].method == "POST" and str(seen[0].url) == "https://login.example.com/oauth/revoke"
    form = {k: v[0] for k, v in parse_qs(seen[0].content.decode()).items()}
    assert form == {"token": "rt-9", "token_type_hint": "refresh_token"}
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"cid-1:csecret-1").decode()
    assert platform.store.list_connections(tid) == []


def test_store_delete_tenant_needs_an_id_and_sweeps_leftovers(app):
    store = app.state.platform.store
    for bad in (None, "", "   ", 5):
        with pytest.raises(ValueError):
            store.delete_tenant(bad)
        with pytest.raises(ValueError):
            store.export_rows("projects", bad)
    with pytest.raises(ValueError):
        store.export_rows("app_secrets", "t_x")
    keep = store.create_tenant("Keep Co", None, None)["id"]
    store.audit(keep, "k", "tool", {}, None)
    store.audit(None, "anonymous (MCP)", "tool", {}, None)
    # rows of a company whose row is already gone (written by a version before Store.audit checked the company,
    # or straight into the database): store.audit itself no longer writes them
    store.audit("t_gone", "x", "late.write", {}, None)
    assert store.tenant_row_counts("t_gone")["audit"] == 0
    with store.connect() as con:
        con.execute('insert into audit (tenant_id, "user", tool, args_json, created) values (?,?,?,?,?)',
                    ("t_gone", "x", "late.write", "{}", "2026-11-26T00:00:00+00:00"))
    removed = store.delete_tenant("t_gone")
    assert removed["audit"] == 1 and removed["tenants"] == 0
    assert store.tenant_row_counts(keep)["audit"] == 1
    with store.connect() as con:
        assert con.execute("select count(*) from audit where tenant_id is null").fetchone()[0] == 1


# ---------------------------------------------------------------------------- operator CLI
def test_cli_delete_needs_yes(app, capsys):
    platform = app.state.platform
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        c.post("/api/projects", json={"name": "P"})
    before = platform.store.tenant_row_counts(tid)
    assert account.main(["delete", "--tenant", tid], platform=platform) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["would_delete"]["rows"]["projects"] == 1 and "--yes" in out.err
    assert platform.store.tenant_row_counts(tid) == before
    assert account.main(["delete", "--tenant", tid, "--yes"], platform=platform) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["deleted"] and receipt["removed"]["projects"] == 1
    assert not any(platform.store.tenant_row_counts(tid).values())
    assert account.main(["delete", "--tenant", tid, "--yes"], platform=platform) == 1       # nothing left
    assert account.main(["delete"], platform=platform) == 2                               # usage


def test_cli_uses_the_server_settings(tmp_path, monkeypatch, capsys):
    """Run as an operator would: the store comes from DATA_DIR / DATABASE_URL, as for the server."""
    app = make_app(tmp_path)
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        c.post("/api/projects", json={"name": "CLI project"})
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if support._db["url"]:
        monkeypatch.setenv("DATABASE_URL", support._db["url"])
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    out_file = tmp_path / "export.json"
    assert account.main(["export", "--tenant", tid, "--out", str(out_file)]) == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["company_id"] == tid and [p["name"] for p in data["projects"]] == ["CLI project"]
    assert stat.S_IMODE(os.stat(out_file).st_mode) == 0o600
    capsys.readouterr()
    assert account.main(["delete", "--tenant", tid, "--yes", "--keep-billing"]) == 0
    assert json.loads(capsys.readouterr().out)["billing"]["result"] == "skipped"
    assert app.state.platform.store.get_tenant(tid) is None
    assert account.main(["export", "--tenant", tid]) == 1


# ---------------------------------------------------------------------------- security review regressions
def concurrently(c, requests: list[dict]) -> list[int]:
    """Send every request at the same moment through the app (one event loop, like the server) with c's cookies
    and CSRF header; returns the status codes."""
    import asyncio

    async def go():
        transport = httpx.ASGITransport(app=c.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=dict(c.cookies),
                                     headers={k: v for k, v in c.headers.items() if k == "x-csrf-token"}) as ac:
            rs = await asyncio.gather(*[ac.request(**r) for r in requests])
        return [r.status_code for r in rs]
    return c.portal.call(go)


def test_password_guesses_sent_all_at_once_still_stop_at_five(app):
    """The deletion's password check (and the password change and login sharing the lockout) counts an attempt
    before checking it: 30 wrong passwords sent at the same moment get 5 checks, not 30."""
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)
        tid = tid["tenant_id"]
        codes = concurrently(c, [{"method": "DELETE", "url": "/api/tenant",
                                  "json": {"confirm_company_name": "Acme Builders", "password": f"wrong-{i}"}}
                                 for i in range(30)])
        assert codes.count(403) == 5 and codes.count(429) == 25, codes
        assert delete(c).status_code == 429 and store.get_tenant(tid)          # locked out, nothing deleted
    with TestClient(app) as c:
        signup(c, email="owner@bravo.test", company="Bravo Electric", license_number="654321")
        codes = concurrently(c, [{"method": "POST", "url": "/api/auth/password",
                                  "json": {"current_password": f"wrong-{i}", "new_password": "a-new-password-1"}}
                                 for i in range(30)])
        assert codes.count(403) == 5 and codes.count(429) == 25, codes
    with TestClient(app) as anon:
        codes = concurrently(anon, [{"method": "POST", "url": "/api/auth/login",
                                     "json": {"email": "owner@acme.test", "password": f"wrong-{i}"}}
                                    for i in range(30)])
        assert codes.count(401) == 5 and codes.count(429) == 25, codes


def test_right_passwords_do_not_use_up_the_lockout(app):
    with TestClient(app) as c:
        signup(c)
        for _ in range(25):                    # more than the per-email limit (20): right answers aren't counted
            assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}).status_code == 200
        for _ in range(4):
            assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": "nope-nope"}).status_code \
                == 401
        login(c, "owner@acme.test")             # the 5th try is right: allowed, and the count starts again
        for _ in range(4):
            assert delete(c, password="wrong-wrong-wrong").status_code == 403
        assert delete(c, name="Acme Builder").status_code == 422             # a wrong name isn't a password try
        r = delete(c)
        assert r.status_code == 200, r.text


def test_a_second_deletion_at_the_same_time_is_refused(app):
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        assert account._begin_closing(tid)             # as if the first click's deletion were still running
        try:
            r = delete(c)
            assert r.status_code == 409 and "already being deleted" in r.json()["detail"]
            assert store.get_tenant(tid)
        finally:
            account._end_closing(tid)
        assert delete(c).status_code == 200 and store.get_tenant(tid) is None
        assert tid not in account._closing


def test_another_site_cannot_start_the_download(app):
    """A top-level navigation from another site carries the SameSite=Lax session cookie: refused before it uses a
    download or writes the activity log. The app's own fetch (same-origin) and a typed address (none) work."""
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
        for site in ("cross-site", "same-site", "Cross-Site"):
            r = c.get("/api/tenant/export", headers={"sec-fetch-site": site})
            assert r.status_code == 403 and "Settings" in r.json()["detail"]
        assert not [a for a in store.list_audit(tid) if a["tool"] == "tenant.export"]
        assert c.get("/api/tenant/export", headers={"sec-fetch-site": "same-origin"}).status_code == 200
        assert c.get("/api/tenant/export", headers={"sec-fetch-site": "none"}).status_code == 200
        assert c.get("/api/tenant/export").status_code == 200                # older browsers send no header
        assert len([a for a in store.list_audit(tid) if a["tool"] == "tenant.export"]) == 3


def test_no_activity_log_row_outlives_the_company(app):
    """A request already running when its company is deleted (a tool call, an SSO sign-in that started before)
    writes its activity-log row after the deletion: the activity log has no foreign key, so the store refuses it."""
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
    store.delete_tenant(tid)
    store.audit(tid, "owner@acme.test", "calculate_lien_deadline", {"claimant_role": "subcontractor"}, True)
    store.audit(tid, "sso", "sso.login", {"error": "config_changed"}, None, ok=False, source="sso")
    assert store.log_mcp_calls(
        [{"tenant_id": tid, "api_key_id": 1, "tool": "verify_contractor_license", "status": "ok"}],
        [{"tenant_id": tid, "user": "api key tc_x…", "tool": "verify_contractor_license", "source": "mcp"}]) == 0
    assert not any(store.tenant_row_counts(tid).values())
    store.audit(None, "anonymous (MCP)", "verify_contractor_license", {}, None)       # anonymous rows still go in
    assert store.log_mcp_calls([{"tenant_id": None, "tool": "verify_contractor_license", "status": "ok"}]) == 1
    with store.connect() as con:
        assert con.execute("select count(*) from audit where tenant_id is null").fetchone()[0] == 1


def test_an_activity_log_write_during_the_deletion_waits_and_writes_nothing(app, monkeypatch):
    """The write starts while delete_tenant holds the company row: it waits for the deletion (Postgres: FOR KEY
    SHARE against FOR UPDATE; SQLite: the write lock), then finds no company and writes nothing."""
    import threading
    import time as _time
    from app.store import Store
    store = app.state.platform.store
    with TestClient(app) as c:
        tid = signup(c)["tenant_id"]
    locked, go = threading.Event(), threading.Event()
    real = Store._read_for_update

    def slow(self, con, sql, params):
        row = real(self, con, sql, params)
        if "from tenants" in sql:
            locked.set()
            go.wait(10)
        return row
    monkeypatch.setattr(Store, "_read_for_update", slow)
    deleter = threading.Thread(target=store.delete_tenant, args=(tid,))
    deleter.start()
    assert locked.wait(10)
    writer = threading.Thread(target=store.audit, args=(tid, "owner@acme.test", "late.write", {}, None))
    writer.start()
    _time.sleep(0.3)
    assert writer.is_alive()                      # waiting for the deletion, not writing past it
    go.set()
    deleter.join(10)
    writer.join(20)
    assert not deleter.is_alive() and not writer.is_alive()
    assert not any(store.tenant_row_counts(tid).values())
