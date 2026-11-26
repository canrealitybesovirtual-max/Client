"""MCP governance of TradeComply's own /mcp server (app/auth.py MCPGate, app/mcp_governance.py, docs/mcp-security.md):
the MCP call log (SHA-256 of the arguments, never the arguments; written after the response), per-key tool scopes
and rate limits, key expiry, tool-calls-only keys, the require-key default on Render, the Origin check, the owner
view and download of the log, and the settings UI. Every test runs on SQLite and (with TC_TEST_DATABASE_URL) on
PostgreSQL."""
import csv
import hashlib
import io
import json
import os
import re

import anyio
import pytest
from fastapi.testclient import TestClient

from app import mcp_governance as gov
from app.auth import MCPGate, RateLimiter
from tests.support import PW, backend, login, make_app, signup  # noqa: F401  (backend is an autouse fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
EMPLOYEE = "Maria Lopez-Testcase"            # stands in for employee data an assistant passes as an argument


def sse_json(text: str):
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


def mcp_session(c, headers=None) -> dict:
    h = {**MCP_HEADERS, **(headers or {})}
    r = c.post("/mcp", json=INIT, headers=h)
    assert r.status_code == 200, r.text
    sid = r.headers.get("mcp-session-id")
    h2 = {**h, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"} if sid else h
    c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
    return h2


def call(c, h, name, args, rid=2):
    return c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                           "params": {"name": name, "arguments": args}})


def lic(c, h, rid=2):
    return call(c, h, "verify_contractor_license", {"license_number": "123456"}, rid)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def new_key(owner, **body) -> dict:
    r = owner.post("/api/keys", json={"name": body.pop("name", "agent"), **body})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(key: dict) -> dict:
    return {"authorization": f"Bearer {key['key']}"}


def mcp_rows(app, tenant_id=None):
    st = app.state.platform.store
    if tenant_id is None:
        return st.admin_mcp_audit(anonymous=True, limit=1000)["items"]
    return st.list_mcp_audit(tenant_id, limit=1000)["items"]


def all_log_text(app) -> str:
    with app.state.platform.store.connect() as con:
        rows = [dict(r) for r in con.execute("select * from audit").fetchall()]
        rows += [dict(r) for r in con.execute("select * from mcp_audit_log").fetchall()]
    return json.dumps(rows)


# ---------------------------------------------------------------------------- the MCP call log
def test_every_tool_call_is_logged_with_a_hash_of_the_arguments_never_the_arguments(owner, app):
    key = new_key(owner, name="Claude Desktop")
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        args = {"hazard": f"fall protection for {EMPLOYEE}"}
        r = call(bot, h, "lookup_safety_standard", args, rid=5)
        assert r.status_code == 200 and "result" in sse_json(r.text)
        assert lic(bot, h, rid=6).status_code == 200
    rows = mcp_rows(app, owner.tenant_id)
    assert [x["tool"] for x in rows] == ["verify_contractor_license", "lookup_safety_standard"]
    row = rows[1]
    want = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()
    assert row["args_sha256"] == want == gov.args_sha256(args)
    assert row["status"] == "ok" and row["api_key_id"] == key["id"] and row["tenant_id"] == owner.tenant_id
    assert isinstance(row["duration_ms"], int) and row["duration_ms"] >= 0 and row["ip"] == "testclient"
    # the activity log keeps its row, with argument NAMES and the same fingerprint instead of values
    act = [a for a in owner.get("/api/audit").json() if a["source"] == "mcp"]
    assert act[1]["tool"] == "lookup_safety_standard" and act[1]["args"] == {"fields": ["hazard"],
                                                                            "args_sha256": want}
    assert act[1]["user"].startswith("api key tck_") and act[1]["ok"] is True
    assert EMPLOYEE not in all_log_text(app) and "fall protection" not in all_log_text(app)


def test_hash_does_not_depend_on_argument_order():
    assert gov.args_sha256({"a": 1, "b": "é"}) == gov.args_sha256({"b": "é", "a": 1})
    assert gov.args_sha256(None) == gov.args_sha256({}) == hashlib.sha256(b"{}").hexdigest()
    assert gov.activity_args({"z": 1, "a": 2})["fields"] == ["a", "z"]


def test_anonymous_calls_are_logged_without_a_company(app):
    with TestClient(app) as c:
        h = mcp_session(c)
        assert lic(c, h).status_code == 200
        r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "no_such_tool", "arguments": {}}})
        assert r.status_code == 200
    rows = mcp_rows(app)
    assert [(x["tool"], x["status"], x["tenant_id"], x["api_key_id"]) for x in rows] == [
        ("no_such_tool", "error", None, None), ("verify_contractor_license", "ok", None, None)]


def test_audit_rows_are_written_after_the_response_is_sent(app):
    """The client gets its whole answer before the database is touched, so a slow database can't slow a call."""
    platform = app.state.platform
    order = []
    real = platform.store.log_mcp_calls

    def spy(entries, activity=()):
        order.append(("write", len(entries), len(activity)))
        return real(entries, activity)
    platform.store.log_mcp_calls = spy
    answer = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": [], "structuredContent": {
        "verified": True}}}).encode()

    async def downstream(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": answer[:10], "more_body": True})
        await send({"type": "http.response.body", "body": answer[10:]})

    gate = MCPGate(downstream, platform)
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "verify_contractor_license", "arguments": {"license_number": "123456"}}}).encode()

    async def run():
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(m):
            order.append(("send", m["type"], m.get("more_body", False)))
        await gate({"type": "http", "method": "POST", "path": "/mcp", "scheme": "https", "client": ("203.0.113.9", 1),
                    "headers": [(b"content-type", b"application/json")]}, receive, send)
    anyio.run(run)
    assert order == [("send", "http.response.start", False), ("send", "http.response.body", True),
                     ("send", "http.response.body", False), ("write", 1, 1)]
    row = mcp_rows(app)[0]
    assert (row["status"], row["ip"], row["tool"]) == ("ok", "203.0.113.9", "verify_contractor_license")


def test_a_failing_audit_write_never_breaks_the_call(owner, app, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(app.state.platform.store, "log_mcp_calls", down)
    with TestClient(app) as c:
        h = mcp_session(c)
        r = lic(c, h)
    assert r.status_code == 200 and sse_json(r.text)["result"]["structuredContent"]["business_name"]
    assert app.state.platform.mcp_audit_pending == 0


def test_a_full_backlog_drops_rows_and_says_so(app, caplog):
    platform = app.state.platform
    platform.mcp_audit_pending = gov.MAX_PENDING_ROWS
    anyio.run(gov.record, platform, [{"tool": "x", "status": "ok"}])
    assert platform.mcp_audit_dropped == 1 and "backlog full" in caplog.text
    platform.mcp_audit_pending = 0


def test_anonymous_mcp_rows_are_trimmed(app):
    store = app.state.platform.store
    tid = store.create_tenant("Keep", None, None)["id"]
    store.log_mcp_calls([{"tool": f"t{i}", "status": "ok"} for i in range(30)] +
                        [{"tenant_id": tid, "tool": "kept", "status": "ok"}])
    assert store.trim_anonymous_mcp_audit(10) == 20
    assert len(mcp_rows(app)) == 10 and len(mcp_rows(app, tid)) == 1


def test_store_refuses_bad_rows(app):
    store = app.state.platform.store
    with pytest.raises(ValueError):
        store.log_mcp_calls([{"tool": "x", "status": "maybe"}])
    with pytest.raises(ValueError):
        store.log_mcp_calls([{"tool": "x", "status": "ok", "args_sha256": "not-a-hash"}])
    assert mcp_rows(app) == []


# ---------------------------------------------------------------------------- tool scopes
def test_scoped_key_runs_only_its_tools_and_refusals_are_logged(owner, app):
    key = new_key(owner, scopes={"tools": ["verify_contractor_license"], "categories": ["payments"]})
    assert key["scope"]["tool_count"] == 6 and key["scope"]["summary"] == "Liens & getting paid; 1 named tool"
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        assert lic(bot, h).status_code == 200
        ok = call(bot, h, "calculate_lien_deadline", {"claimant_role": "subcontractor",
                                                      "last_furnishing_date": "2026-11-01"}, rid=3)
        assert "result" in sse_json(ok.text)
        r = call(bot, h, "lookup_safety_standard", {"hazard": "trench"}, rid=4)
        assert r.status_code == 200
        err = r.json()
        assert err["id"] == 4 and err["error"]["code"] == MCPGate.DENIED
        assert err["error"]["data"] == {"tool": "lookup_safety_standard", "reason": "not_in_key_scope"}
        assert "Settings > AI assistant (MCP)" in err["error"]["message"]
        # tools/list shows only what the key may call
        listed = sse_json(bot.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).text)
        assert sorted(t["name"] for t in listed["result"]["tools"]) == key["scope"]["allowed_tools"]
        # the REST tool routes follow the same scope
        assert bot.post("/api/tools/lookup_safety_standard", json={"hazard": "x"},
                        headers=bearer(key)).status_code == 403
        assert {t["name"] for t in bot.get("/api/tools", headers=bearer(key)).json()["tools"]} == \
            set(key["scope"]["allowed_tools"])
    rows = mcp_rows(app, owner.tenant_id)
    assert [(x["tool"], x["status"]) for x in rows] == [("lookup_safety_standard", "denied"),
                                                         ("calculate_lien_deadline", "ok"),
                                                         ("verify_contractor_license", "ok")]
    # a refused call never ran: no activity row for it
    assert "lookup_safety_standard" not in [a["tool"] for a in owner.get("/api/audit").json()]


def test_default_key_sees_and_runs_every_read_only_tool(owner, app):
    key = new_key(owner)
    total = len(owner.get("/api/tools").json()["tools"])          # 68 when this was written; other work adds more
    assert key["scopes"] is None and key["scope"]["default"] is True and key["scope"]["tool_count"] == total >= 68
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        listed = sse_json(bot.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).text)
        assert len(listed["result"]["tools"]) == total
        assert bot.get("/api/projects", headers=bearer(key)).status_code == 200      # REST API as before


def test_scope_rules_with_a_tool_that_changes_data():
    """A category grants only its read-only tools; a tool that is not read-only must be named, and the default
    scope never includes it."""
    index = {"read_a": {"category": "c", "read_only": True}, "write_b": {"category": "c", "read_only": False},
             "read_x": {"category": "x", "read_only": True}}
    assert gov.scope_allows(None, "read_a", index) and not gov.scope_allows(None, "write_b", index)
    assert gov.scope_allows(None, "unknown", index)        # the server itself answers "unknown tool"
    cat = {"categories": ["c"]}
    assert gov.scope_allows(cat, "read_a", index) and not gov.scope_allows(cat, "write_b", index)
    assert not gov.scope_allows(cat, "read_x", index) and not gov.scope_allows(cat, "unknown", index)
    assert gov.scope_allows({"tools": ["write_b"]}, "write_b", index)
    assert not gov.scope_allows({"tools": [], "categories": []}, "read_a", index)       # an empty allow-list
    assert gov.scope_allows({"rest_api": False}, "read_a", index)                      # default tools, no REST


def test_normalize_scopes():
    index = {"a": {"category": "c", "read_only": True}, "b": {"category": "d", "read_only": True}}
    assert gov.normalize_scopes(None, index) is None and gov.normalize_scopes({}, index) is None
    assert gov.normalize_scopes({"rest_api": True}, index) is None
    assert gov.normalize_scopes({"tools": ["b", "a", "a"], "rest_api": False}, index) == {"tools": ["a", "b"],
                                                                                          "rest_api": False}
    assert gov.normalize_scopes({"tools": []}, index) == {"tools": []}
    for bad, match in (({"tools": ["zz"]}, "No tool named 'zz'"), ({"categories": ["q"]}, "No category"),
                       ({"tools": "a"}, "list"), ({"verbs": []}, "Unknown scope field"), ([], "object"),
                       ({"rest_api": "no"}, "true or false")):
        with pytest.raises(ValueError, match=match):
            gov.normalize_scopes(bad, index)


def test_tool_calls_only_key_cannot_read_company_data(owner, app):
    key = new_key(owner, scopes={"rest_api": False})
    assert key["scope"]["rest_api"] is False and key["scope"]["tool_count"] == len(owner.get("/api/tools").json()["tools"])
    with TestClient(app) as bot:
        auth = bearer(key)
        assert bot.get("/api/projects", headers=auth).status_code == 403
        assert bot.get("/api/me", headers=auth).status_code == 403
        assert bot.post("/api/subs/import", json={"csv": "name\nX\n"}, headers=auth).status_code == 403
        assert bot.get("/api/tools", headers=auth).status_code == 200
        assert bot.get("/api/mcp/tool-hashes", headers=auth).status_code == 200
        assert bot.post("/api/tools/verify_contractor_license", json={"license_number": "123456"},
                        headers=auth).status_code == 200
        h = mcp_session(bot, auth)
        assert lic(bot, h).status_code == 200


def test_api_keys_cannot_create_change_or_revoke_keys(owner, app):
    key = new_key(owner, scopes={"tools": ["verify_contractor_license"]})
    with TestClient(app) as bot:
        auth = bearer(key)
        assert bot.post("/api/keys", json={"name": "wider"}, headers=auth).status_code == 403
        assert bot.patch(f"/api/keys/{key['id']}", json={"scopes": None}, headers=auth).status_code == 403
        assert bot.delete(f"/api/keys/{key['id']}", headers=auth).status_code == 403
    assert len(owner.get("/api/keys").json()) == 1


def test_owner_changes_a_keys_access_and_every_change_is_logged(owner, app):
    key = new_key(owner, name="Workato", expires_in_days=30)
    assert key["expires_at"] and key["expired"] is False
    r = owner.patch(f"/api/keys/{key['id']}", json={"scopes": {"categories": ["licensing"], "rest_api": False},
                                                    "rate_limit_per_min": 30})
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["scopes"] == {"categories": ["licensing"], "rest_api": False} and k["rate_limit_per_min"] == 30
    assert k["expires_at"] == key["expires_at"]                     # left out = unchanged
    assert k["scope"]["summary"] == "Licenses & subcontractors" and k["scope"]["rest_api"] is False
    for body, match in (({"scopes": {"tools": ["rm_rf"]}}, "No tool named"),
                        ({"scopes": {"categories": ["everything"]}}, "No category"),
                        ({"rate_limit_per_min": 601}, "1 to 600"), ({"rate_limit_per_min": 0}, "1 to 600"),
                        ({"expires_at": "2020-01-01"}, "future"), ({"expires_in_days": 99999}, "expires_in_days"),
                        ({"expires_at": "soon"}, "date")):
        bad = owner.patch(f"/api/keys/{key['id']}", json=body)
        assert bad.status_code == 422 and match in bad.json()["detail"], (body, bad.text)
    r = owner.patch(f"/api/keys/{key['id']}", json={"scopes": None, "rate_limit_per_min": None, "expires_at": None,
                                                    "name": "Workato recipes"})
    k = r.json()
    assert (k["scopes"], k["rate_limit_per_min"], k["expires_at"], k["name"]) == (None, None, None, "Workato recipes")
    acts = [a for a in owner.get("/api/audit").json() if a["tool"].startswith("api_key.")]
    assert [a["tool"] for a in acts] == ["api_key.update", "api_key.update", "api_key.create"]
    assert acts[1]["args"]["scope_after"] == "Licenses & subcontractors" and acts[0]["args"]["scope_after"] == \
        "All read-only tools"
    assert owner.delete(f"/api/keys/{key['id']}").status_code == 200
    assert owner.patch(f"/api/keys/{key['id']}", json={"name": "zombie"}).status_code == 404     # revoked
    assert owner.get("/api/audit").json()[0]["tool"] == "api_key.revoke"


def test_field_users_cannot_manage_keys_and_other_companies_keys_are_invisible(owner, app):
    key = new_key(owner)
    tmp = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()["temporary_password"]
    with TestClient(app) as field, TestClient(app) as other:
        login(field, "crew@acme.test", tmp)
        assert field.patch(f"/api/keys/{key['id']}", json={"name": "x"}).status_code == 403
        assert field.get("/api/mcp/policy").status_code == 403
        signup(other, email="b@other.test", company="Other Co")
        assert other.patch(f"/api/keys/{key['id']}", json={"name": "x"}).status_code == 404
        assert other.get("/api/keys").json() == []


# ---------------------------------------------------------------------------- rate limits
def test_per_key_rate_limit_and_the_company_limit(owner, app):
    slow = new_key(owner, name="slow", rate_limit_per_min=2)
    fast = new_key(owner, name="fast")
    with TestClient(app) as bot:
        hs, hf = mcp_session(bot, bearer(slow)), mcp_session(bot, bearer(fast))
        codes = [lic(bot, hs, rid=10 + i).status_code for i in range(3)]
        assert codes == [200, 200, 429]
        r = lic(bot, hs, rid=20)
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert lic(bot, hf, rid=21).status_code == 200                   # another key is not affected
        app.state.platform.mcp_tenant_limiter.limit = 3                  # ... until the company total (2 + 1) is reached
        assert lic(bot, hf, rid=22).status_code == 429
    statuses = [x["status"] for x in mcp_rows(app, owner.tenant_id) if x["api_key_id"] == slow["id"]]
    assert statuses == ["rate_limited", "rate_limited", "ok", "ok"]


def test_refused_calls_are_logged_at_most_30_a_minute(owner, app):
    app.state.platform.mcp_reject_log_limiter.limit = 2
    key = new_key(owner, scopes={"tools": ["verify_contractor_license"]})
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        for i in range(5):
            assert call(bot, h, "lookup_safety_standard", {"hazard": "x"}, rid=30 + i).json()["error"]
    assert [x["status"] for x in mcp_rows(app, owner.tenant_id)] == ["denied", "denied"]


def test_rate_limiter_per_key_override():
    lim = RateLimiter(limit=5, window_s=60)
    for _ in range(2):
        lim.hit("k")
    assert lim.retry_after("k", 2) > 0 and lim.retry_after("k") == 0 and lim.retry_after("k", 50) == 0


# ---------------------------------------------------------------------------- key expiry
def test_expired_key_is_refused_everywhere(owner, app):
    key = new_key(owner, expires_in_days=1)
    with TestClient(app) as bot:
        assert bot.post("/mcp", json=INIT, headers={**MCP_HEADERS, **bearer(key)}).status_code == 200
        with app.state.platform.store.connect() as con:
            con.execute("update api_keys set expires_at = ? where id = ?", ("2026-01-01T00:00:00+00:00", key["id"]))
        r = bot.post("/mcp", json=INIT, headers={**MCP_HEADERS, **bearer(key)})
        assert r.status_code == 401 and "expired" in r.json()["error"]["message"]
        assert bot.get("/api/projects", headers=bearer(key)).status_code == 401
    listed = owner.get("/api/keys").json()[0]
    assert listed["expired"] is True and listed["revoked"] is False


# ---------------------------------------------------------------------------- require-key default and 401 message
def _clear_access_env(monkeypatch):
    for name in ("MCP_REQUIRE_KEY", "MCP_PUBLIC_DEMO", "RENDER", "APP_URL", "RENDER_EXTERNAL_URL",
                 "MCP_ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("env,require,demo,warns", [
    ({}, False, False, 0),
    ({"MCP_REQUIRE_KEY": "1"}, True, False, 0),
    ({"MCP_REQUIRE_KEY": "0"}, False, False, 0),
    ({"RENDER": "true"}, True, False, 0),
    ({"RENDER": "true", "MCP_REQUIRE_KEY": "0"}, True, False, 1),
    ({"RENDER": "true", "MCP_PUBLIC_DEMO": "1"}, False, True, 0),
    ({"RENDER": "true", "MCP_PUBLIC_DEMO": "0"}, True, False, 0),
    ({"RENDER": "true", "MCP_PUBLIC_DEMO": "1", "MCP_REQUIRE_KEY": "1"}, True, False, 1),
    ({"MCP_PUBLIC_DEMO": "yes"}, False, True, 0),
])
def test_resolve_mcp_access(env, require, demo, warns):
    a = gov.resolve_mcp_access(env)
    assert (a.require_key, a.public_demo, len(a.warnings)) == (require, demo, warns)


def test_on_render_mcp_needs_a_key_and_the_401_says_how_to_get_one(tmp_path, monkeypatch):
    _clear_access_env(monkeypatch)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("APP_URL", "https://tradecomply.example")
    app = make_app(tmp_path)
    assert app.state.platform.settings.mcp_require_key is True
    with TestClient(app) as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="tradecomply"'
        err = r.json()["error"]
        assert "API key is required" in err["message"] and "Settings > AI assistant (MCP)" in err["message"]
        assert err["data"]["get_a_key"] == "https://tradecomply.example/#/settings"
        assert err["data"]["header"] == "Authorization: Bearer <API key>"
        assert c.get("/api/config").json()["mcp_require_key"] is True
        signup(c)
        key = new_key(c)
        h = mcp_session(c, bearer(key))
        assert lic(c, h).status_code == 200


def test_on_render_public_demo_must_be_switched_on_explicitly(tmp_path, monkeypatch, caplog):
    _clear_access_env(monkeypatch)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("MCP_REQUIRE_KEY", "0")
    app = make_app(tmp_path)
    with TestClient(app) as c:
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
    assert "MCP_REQUIRE_KEY=0 no longer opens /mcp on Render" in caplog.text
    monkeypatch.setenv("MCP_PUBLIC_DEMO", "1")
    app = make_app(tmp_path / "demo")
    with TestClient(app) as c:
        h = mcp_session(c)
        assert lic(c, h).status_code == 200
        assert c.get("/api/config").json()["mcp_public_demo"] is True


# ---------------------------------------------------------------------------- Origin and body checks
def test_origin_header_from_another_site_is_refused(tmp_path, monkeypatch):
    _clear_access_env(monkeypatch)
    app = make_app(tmp_path)
    with TestClient(app) as c:
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "origin": "https://evil.example"})
        assert r.status_code == 403 and "Origin not allowed" in r.json()["error"]["message"]
        assert c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "origin": "http://testserver"}).status_code == 200
        assert c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200          # no Origin: fine
        app.state.platform.settings.mcp_allowed_origins = gov.parse_origins("https://partner.example/, junk")
        assert app.state.platform.settings.mcp_allowed_origins == ("https://partner.example",)
        assert c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "origin": "https://partner.example"}
                      ).status_code == 200
        app.state.platform.settings.local_mode = True
        assert gov.origin_allowed(app.state.platform, {"origin": "http://localhost:6274"}, {})
        assert not gov.origin_allowed(app.state.platform, {"origin": "https://localhost.evil.example"}, {})


def test_unreadable_bodies_fail_closed(app):
    with TestClient(app) as c:
        h = mcp_session(c)
        r = c.post("/mcp", headers=h, content=b'{"jsonrpc": "2.0", "method": "tools/call"')
        assert r.status_code == 400 and r.json()["error"]["code"] == -32700
        r = c.post("/mcp", headers=h, content=b"[" * 200_000)
        assert r.status_code == 400
    assert mcp_rows(app) == []


def test_filter_tools_list_json_and_event_stream():
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "a"}, {"name": "b"}]}}
    out = gov.filter_tools_list(json.dumps(msg).encode(), "application/json", {"a"})
    assert [t["name"] for t in json.loads(out)["result"]["tools"]] == ["a"]
    sse = f"event: message\r\ndata: {json.dumps(msg)}\r\n\r\n".encode()
    out = gov.filter_tools_list(sse, "text/event-stream", {"b"}).decode()
    assert out.startswith("event: message\r\ndata: ") and out.endswith("\r\n\r\n")
    assert [t["name"] for t in sse_json(out)["result"]["tools"]] == ["b"]
    assert gov.filter_tools_list(json.dumps(msg).encode(), "application/json", {"a", "b"}) is None
    assert gov.filter_tools_list(b"not json", "application/json", {"a"}) is None


# ---------------------------------------------------------------------------- owner view and download
def test_owner_views_and_filters_the_mcp_log_only_for_their_company(owner, app):
    a = new_key(owner, name="A")
    b = new_key(owner, name="B", scopes={"tools": ["verify_contractor_license"]})
    with TestClient(app) as bot, TestClient(app) as other:
        ha, hb = mcp_session(bot, bearer(a)), mcp_session(bot, bearer(b))
        for i in range(3):
            assert lic(bot, ha, rid=40 + i).status_code == 200
        call(bot, hb, "lookup_safety_standard", {"hazard": "x"}, rid=50)
        signup(other, email="b@other.test", company="Other Co")
        okey = new_key(other, name="Other")
        ho = mcp_session(other, bearer(okey))
        assert call(other, ho, "lookup_safety_standard", {"hazard": "only-other"}, rid=60).status_code == 200
        assert len(other.get("/api/mcp/audit").json()["items"]) == 1
    page = owner.get("/api/mcp/audit").json()
    assert len(page["items"]) == 4 and {k["name"] for k in page["keys"]} == {"A", "B"}
    assert "lookup_safety_standard" in [i["tool"] for i in page["items"]]
    assert all(i["key_name"] in ("A", "B") for i in page["items"])
    denied = owner.get("/api/mcp/audit?status=denied").json()["items"]
    assert [(d["tool"], d["key_name"]) for d in denied] == [("lookup_safety_standard", "B")]
    assert len(owner.get(f"/api/mcp/audit?key_id={a['id']}").json()["items"]) == 3
    assert owner.get("/api/mcp/audit?tool=verify_contractor_license").json()["items"][0]["tool"] == \
        "verify_contractor_license"
    first = owner.get("/api/mcp/audit?limit=3").json()
    assert len(first["items"]) == 3 and first["next_cursor"]
    rest = owner.get(f"/api/mcp/audit?limit=3&cursor={first['next_cursor']}").json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in rest["items"]})
    for bad in ("status=maybe", "cursor=junk", "start=yesterday", "start=2026-12-01&end=2026-11-01"):
        assert owner.get(f"/api/mcp/audit?{bad}").status_code == 422, bad


def test_only_owners_see_the_mcp_log(owner, app):
    tmp = owner.post("/api/users", json={"email": "pat@acme.test", "role": "office"}).json()["temporary_password"]
    key = new_key(owner)
    with TestClient(app) as office, TestClient(app) as bot, TestClient(app) as anon:
        login(office, "pat@acme.test", tmp)
        assert office.get("/api/mcp/audit").status_code == 403
        assert office.get("/api/mcp/audit/export").status_code == 403
        assert office.get("/api/mcp/policy").status_code == 200
        assert bot.get("/api/mcp/audit", headers=bearer(key)).status_code == 403
        assert anon.get("/api/mcp/audit").status_code == 401


def test_owner_downloads_the_log_as_csv_and_json(owner, app):
    key = new_key(owner, name="=cmd|' /C calc'!A0")            # a formula in a key name is neutralized
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        lic(bot, h)
        call(bot, h, "lookup_safety_standard", {"hazard": EMPLOYEE}, rid=3)
    r = owner.get("/api/mcp/audit/export?format=csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert re.match(r'attachment; filename="tradecomply-mcp-calls-\d{4}-\d{2}-\d{2}\.csv"',
                    r.headers["content-disposition"]) and r.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(gov.EXPORT_COLUMNS) and len(rows) == 3
    assert [x[2] for x in rows[1:]] == ["verify_contractor_license", "lookup_safety_standard"]      # oldest first
    assert all(x[6].startswith("'=") for x in rows[1:]) and EMPLOYEE not in r.text
    j = owner.get("/api/mcp/audit/export?format=json&status=ok&tool=lookup_safety_standard").json()
    assert j["row_count"] == 1 and j["truncated"] is False and j["rows"][0]["args_sha256"] == \
        gov.args_sha256({"hazard": EMPLOYEE})
    assert set(j["rows"][0]) == set(gov.EXPORT_COLUMNS) and j["status"] == "ok"
    assert owner.get("/api/mcp/audit/export?format=xml").status_code == 422
    assert [a for a in owner.get("/api/audit").json() if a["tool"] == "mcp_audit.export"]
    app.state.platform.mcp_export_limiter.limit = 2
    assert owner.get("/api/mcp/audit/export").status_code == 429


# ---------------------------------------------------------------------------- policy and tool definition hashes
def test_policy_lists_every_tool_in_a_category(owner):
    pol = owner.get("/api/mcp/policy").json()
    assert pol["require_key"] is False and pol["key_rate_limit_per_min"] == 600 and pol["key_rate_limit_max"] == 600
    total = len(owner.get("/api/tools").json()["tools"])
    assert pol["tenant_rate_limit_per_min"] == 1200 and pol["read_only_tools"] == pol["tool_count"] == total >= 68
    names = [t["name"] for c in pol["categories"] for t in c["tools"]]
    assert len(names) == len(set(names)) == total
    assert all(c["label"] and c["label"][0].isupper() for c in pol["categories"])
    assert "HR compliance" in {c["label"] for c in pol["categories"]}


def test_tool_definition_hashes_match_what_tools_list_sends(owner, app):
    r = owner.get("/api/mcp/tool-hashes").json()
    assert r["tool_count"] == len(owner.get("/api/tools").json()["tools"]) and len(r["manifest_sha256"]) == 64
    with TestClient(app) as c:
        h = mcp_session(c)
        tools = sse_json(c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).text)
    for t in tools["result"]["tools"]:
        body = {k: t[k] for k in gov.TOOL_HASH_FIELDS if t.get(k) is not None}
        assert r["tools"][t["name"]] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                                  ensure_ascii=False).encode()).hexdigest()
    lines = "\n".join(f"{n}:{r['tools'][n]}" for n in sorted(r["tools"]))
    assert r["manifest_sha256"] == hashlib.sha256(lines.encode()).hexdigest()


# ---------------------------------------------------------------------------- tool definitions (OWASP MCP03, MCP05)
ZERO_WIDTH = re.compile("[​-‏‪-‮⁠﻿]")
POISON = re.compile(r"ignore (all |any )?previous instructions|do not tell the user|<!--|~/\.ssh|id_rsa|\.aws/credentials|"
                    r"/etc/passwd|\.env\b", re.I)


def test_tool_definitions_show_none_of_owasps_poisoning_indicators(owner):
    """OWASP MCP03 'Detection Indicators (Static Analysis)': model-directed imperatives, sensitive paths, hidden
    characters and comment-smuggled text in names, descriptions and parameter descriptions."""
    tools = owner.get("/api/tools").json()["tools"]
    assert len(tools) >= 68
    for t in tools:
        text = json.dumps([t["name"], t["title"], t["description"], t["input_schema"]], ensure_ascii=False)
        assert not ZERO_WIDTH.search(text), t["name"]
        assert not POISON.search(text), (t["name"], POISON.search(text).group(0))
        assert t["annotations"].get("readOnlyHint") is True and t["input_schema"].get("additionalProperties") is False


def test_tool_code_runs_no_shell_or_dynamic_code():
    """OWASP MCP05: no tool module builds commands or evaluates code."""
    files = [os.path.join(REPO, "server.py")] + [os.path.join(REPO, "tradecomply", f) for f in
                                                 os.listdir(os.path.join(REPO, "tradecomply")) if f.endswith(".py")]
    pattern = re.compile(r"\bsubprocess\b|os\.system\(|os\.popen\(|\beval\(|\bexec\(|shell=True|pickle\.loads?\(")
    hits = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if pattern.search(line) and not line.lstrip().startswith("#"):
                    hits.append(f"{os.path.basename(path)}:{n}: {line.strip()}")
    assert hits == []


# ---------------------------------------------------------------------------- web app, docs and deployment config
def test_settings_page_has_key_access_and_the_mcp_log():
    with open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8") as f:
        js = f.read()
    section = js[js.index("// ======================================================================== MCP governance"):
                 js.index("// ======================================================================== onboarding")]
    assert "/api/mcp/audit" in section and "/api/mcp/audit/export?" in section and "/api/mcp/policy" in section
    assert "method: 'PATCH'" in section and "editKeyAccess" in js and "mcpLogSection()" in js
    assert "innerHTML" not in section and "html:" not in section and "insertAdjacentHTML" not in section
    assert "if (can.owner()) sections.push(mcpLogSection());" in js
    with open(os.path.join(REPO, "app", "static", "styles.css"), encoding="utf-8") as f:
        assert ".scope-picker" in f.read()


def test_security_doc_covers_every_owasp_item_and_the_gateways():
    with open(os.path.join(REPO, "docs", "mcp-security.md"), encoding="utf-8") as f:
        doc = f.read()
    for n in range(1, 11):
        assert re.search(rf"MCP{n:02d}", doc), n
    assert doc.count("| Met") + doc.count("| Partial") + doc.count("| Not met") >= 30
    for gateway in ("Workato", "Arcade", "MintMCP", "Barndoor"):
        assert gateway in doc
    assert "MCP_PUBLIC_DEMO" in doc and "changes the live behavior" in doc


def test_render_blueprint_and_readme_document_the_new_default():
    with open(os.path.join(REPO, "render.yaml"), encoding="utf-8") as f:
        blueprint = f.read()
    assert "key: MCP_PUBLIC_DEMO" in blueprint and "key: MCP_REQUIRE_KEY" not in blueprint
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    assert "MCP_PUBLIC_DEMO" in readme and "MCP_ALLOWED_ORIGINS" in readme and "docs/mcp-security.md" in readme


def test_governance_routes_never_call_the_database_on_the_event_loop(tmp_path):
    """Same rule as tests/test_platform.py: every store call runs on a worker thread."""
    import asyncio
    import traceback
    app = make_app(tmp_path)
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
    with TestClient(app) as c, TestClient(app) as bot:
        signup(c)
        k = new_key(c, scopes={"tools": ["verify_contractor_license"]}, rate_limit_per_min=3, expires_in_days=9)
        c.get("/api/keys")
        c.patch(f"/api/keys/{k['id']}", json={"rate_limit_per_min": 2})
        h = mcp_session(bot, bearer(k))
        lic(bot, h)
        call(bot, h, "lookup_safety_standard", {"hazard": "x"}, rid=3)
        lic(bot, h, rid=4)
        lic(bot, h, rid=5)                                   # rate limited
        bot.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        bot.get("/api/tools", headers=bearer(k))
        c.get("/api/mcp/audit")
        c.get("/api/mcp/audit/export?format=csv")
        c.get("/api/mcp/audit/export?format=json")
        c.get("/api/mcp/policy")
        c.get("/api/mcp/tool-hashes")
        c.delete(f"/api/keys/{k['id']}")
    store.connect = real
    assert on_loop == []
    # the limit was lowered to 2 a minute: the refused call counts too, so the last two calls were rate limited
    assert [r["status"] for r in mcp_rows(app, k["tenant_id"])] == ["rate_limited", "rate_limited", "denied", "ok"]


# ---------------------------------------------------------------------------- schema 6
def test_upgrade_from_version_5_keeps_keys_with_the_default_policy(tmp_path):
    from app.store import SCHEMA_VERSION
    from tests.support import store_for
    s = store_for(tmp_path)
    tid = s.create_tenant("Old Co", None, None)["id"]
    kid = s.create_api_key(tid, "old key", "a" * 64, "tck_old", None)["id"]
    with s.connect() as con:
        for col in ("scopes_json", "rate_limit_per_min", "expires_at"):
            con.execute(f"alter table api_keys drop column {col}")
        if s.backend == "sqlite":
            con.execute("pragma user_version = 5")
        else:
            con.execute("update schema_version set version = 5")
    s.migrate()
    assert s.schema_version() == SCHEMA_VERSION == 6
    k = s.get_api_key(tid, kid)
    assert (k["name"], k["scopes"], k["rate_limit_per_min"], k["expires_at"]) == ("old key", None, None, None)
    s.migrate()                                       # running it again changes nothing
    assert s.api_key_by_hash("a" * 64)["id"] == kid


def test_store_key_policy_validation(tmp_path):
    from tests.support import store_for
    s = store_for(tmp_path)
    tid = s.create_tenant("Co", None, None)["id"]
    kid = s.create_api_key(tid, "k", "b" * 64, "tck_b", None, scopes={"tools": ["x"]}, rate_limit_per_min=5,
                           expires_at="2030-01-01")["id"]
    k = s.get_api_key(tid, kid)
    assert k["scopes"] == {"tools": ["x"]} and k["rate_limit_per_min"] == 5
    assert k["expires_at"] == "2030-01-01T00:00:00+00:00"
    for bad in ({"scopes": ["x"]}, {"rate_limit_per_min": 0}, {"rate_limit_per_min": True}, {"name": ""},
                {"expires_at": "never"}, {"owner": "me"}):
        with pytest.raises(ValueError):
            s.update_api_key(tid, kid, **bad)
    assert s.update_api_key("t_other", kid, name="x") is None
    assert s.revoke_api_key(tid, kid) and s.update_api_key(tid, kid, name="x") is None


def test_invalid_key_gets_401_with_how_to_get_a_new_one(app):
    with TestClient(app) as c:
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": "Bearer tck_not_a_real_key"})
        assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="tradecomply"'
        assert "Invalid, expired or revoked API key" in r.json()["error"]["message"]
        assert r.json()["error"]["data"]["get_a_key"] == "http://testserver/#/settings"


# ---------------------------------------------------------------------------- browser smoke (Playwright, if installed)
from tests.test_integrations_ui import live_server, playwright_env  # noqa: E402,F401  (fixtures)

SETTINGS_SMOKE = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, shot] = process.argv.slice(2);
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const out = {};
  await page.goto(base + '/#/login');
  out.signup = await page.evaluate(async () => (await fetch('/api/auth/signup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ company_name: 'Smoke Builders', email: 'smoke@example.test', password: 'correct-horse-battery', cslb_license: '123456' }) })).status);
  const key = await page.evaluate(async () => {
    const csrf = document.cookie.match(/tc_csrf=([^;]+)/)[1];
    const r = await fetch('/api/keys', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ name: 'Smoke agent' }) });
    return (await r.json()).key;
  });
  // MCP calls from Node, like a real MCP client (no Origin header)
  const H = { 'content-type': 'application/json', accept: 'application/json, text/event-stream', authorization: 'Bearer ' + key };
  const post = async (h, body) => { const r = await fetch(base + '/mcp', { method: 'POST', headers: h, body: JSON.stringify(body) }); const t = await r.text(); return [r, t]; };
  const [init] = await post(H, { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'smoke', version: '1' } } });
  const H2 = { ...H, 'mcp-session-id': init.headers.get('mcp-session-id'), 'mcp-protocol-version': '2025-06-18' };
  await post(H2, { jsonrpc: '2.0', method: 'notifications/initialized' });
  const [call] = await post(H2, { jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'verify_contractor_license', arguments: { license_number: '123456' } } });
  out.call = call.status;
  const [anon] = await post({ 'content-type': 'application/json', accept: 'application/json, text/event-stream', origin: 'https://evil.example' }, { jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  out.evilOrigin = anon.status;
  await page.goto(base + '/#/settings');
  await page.waitForSelector('#mcp-log table');
  out.logRows = await page.$$eval('#mcp-log tbody tr', (r) => r.length);
  out.logText = await page.innerText('#mcp-log tbody');
  out.csvHref = await page.getAttribute('#mcp-log a[download]', 'href');
  out.nullText = await page.evaluate(() => document.querySelector('main').innerText.split('\n').filter((l) => l.trim() === 'null').length);
  await page.click('#ai button:has-text("Edit access")');
  await page.waitForSelector('dialog .scope-picker', { state: 'attached' });
  await page.check('dialog input[value="pick"]');
  await page.check('dialog input[value="licensing"]');
  await page.uncheck('dialog input[id^="ka-rest"]');
  await page.fill('dialog input[name="rate_limit_per_min"]', '30');
  await page.click('dialog button[type=submit]');
  await page.waitForSelector('dialog', { state: 'detached' });
  await page.waitForFunction(() => document.querySelector('#ai').innerText.includes('tool calls only'));
  out.access = await page.innerText('#ai tbody');
  const [denied, deniedText] = await post(H2, { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'lookup_safety_standard', arguments: { hazard: 'trench' } } });
  out.denied = JSON.parse(deniedText).error.code;
  await page.click('#mcp-log button:has-text("Refresh")');
  await page.waitForFunction(() => document.querySelector('#mcp-log').innerText.includes('Not allowed'));
  await page.$eval('#mcp-log', (el) => el.scrollIntoView());
  await page.screenshot({ path: shot, fullPage: false });
  const phone = await browser.newContext({ viewport: { width: 375, height: 812 }, deviceScaleFactor: 1, storageState: await ctx.storageState() });
  const mp = await phone.newPage();
  await mp.goto(base + '/#/settings');
  await mp.waitForSelector('#mcp-log table');
  out.phoneOverflow = await mp.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  await mp.click('#ai button:has-text("Edit access")');
  await mp.waitForSelector('dialog .scope-picker');
  out.phoneDialogOverflow = await mp.$eval('dialog', (d) => d.scrollWidth > d.clientWidth + 1);
  await mp.check('dialog input[value="pick"]');
  await mp.click('dialog details summary');
  await mp.screenshot({ path: shot.replace(/\.png$/, '-phone.png') });
  out.errors = errors;
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_settings_page_key_access_and_mcp_log_in_a_real_browser(tmp_path, backend, playwright_env, live_server):
    if backend == "postgres":
        pytest.skip("the browser test runs once, against a SQLite server")
    import subprocess
    node, env = playwright_env
    script = tmp_path / "settings_smoke.js"
    script.write_text(SETTINGS_SMOKE)
    r = subprocess.run([node, str(script), live_server, str(tmp_path / "settings.png")], env=env,
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["signup"] == 200 and out["call"] == 200 and out["evilOrigin"] == 403
    assert out["logRows"] == 1 and "Verify contractor license" in out["logText"] and "OK" in out["logText"]
    assert out["csvHref"].startswith("/api/mcp/audit/export?") and "format=csv" in out["csvHref"]
    assert "Licenses & subcontractors · tool calls only" in out["access"] and "30" in out["access"]
    assert out["denied"] == MCPGate.DENIED
    assert out["phoneOverflow"] is False and out["phoneDialogOverflow"] is False and out["nullText"] == 0
    assert out["errors"] == []


def test_unknown_scope_fields_are_refused(owner):
    r = owner.post("/api/keys", json={"name": "x", "scopes": {"tools": [], "verbs": ["delete"]}})
    assert r.status_code == 422


def test_duplicate_json_keys_cannot_smuggle_a_tool_past_the_scope(owner, app):
    """The gate (json) and the MCP SDK (pydantic_core.from_json) both keep the LAST of duplicate keys, so the tool
    the gate checks is the tool that runs."""
    import pydantic_core
    raw = ('{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":"%s","arguments":%s,"name":"%s"}}')
    for text in (raw % (1, "a", "{}", "b"),):
        assert json.loads(text)["params"]["name"] == pydantic_core.from_json(text)["params"]["name"] == "b"
    key = new_key(owner, scopes={"tools": ["verify_contractor_license"]})
    with TestClient(app) as bot:
        h = mcp_session(bot, bearer(key))
        sneaky = raw % (7, "verify_contractor_license", '{"hazard":"x"}', "lookup_safety_standard")
        r = bot.post("/mcp", headers=h, content=sneaky.encode())
        assert r.json()["error"]["code"] == MCPGate.DENIED
        fine = raw % (8, "lookup_safety_standard", '{"license_number":"123456"}', "verify_contractor_license")
        assert "result" in sse_json(bot.post("/mcp", headers=h, content=fine.encode()).text)
    assert [(x["tool"], x["status"]) for x in mcp_rows(app, owner.tenant_id)] == [
        ("verify_contractor_license", "ok"), ("lookup_safety_standard", "denied")]
