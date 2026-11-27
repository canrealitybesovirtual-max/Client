"""Regression tests for the final pre-launch review (2026-11-27). Each test names the finding it covers. Every test
runs on SQLite and, with TC_TEST_DATABASE_URL, on PostgreSQL."""
import json
import logging
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import safe_json
from app.auth import forwarded_ip
from tests.support import PW, backend, join, login, make_app, signup  # noqa: F401  (backend: autouse)

REPO = pathlib.Path(__file__).resolve().parents[1]
JSON = {"content-type": "application/json"}


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        signup(c)
        c.tenant_id = c.get("/api/me").json()["tenant"]["id"]
        yield c


def tools_of(c) -> list:
    return [a["tool"] for a in c.get("/api/audit").json()]


def audit_row(c, tool) -> dict:
    rows = [a for a in c.get("/api/audit").json() if a["tool"] == tool]
    assert rows, f"no {tool} row in {tools_of(c)}"
    return rows[0]


# ---------------------------------------------------------------------------- web-01, web-02: bodies that can't be
# written out again (a lone surrogate, a non-finite number) are refused before anything runs or is stored
BAD_BODIES = [
    ("POST", "/api/tools/search_california_rules", '{"query": "lien \\ud800"}'),
    ("POST", "/api/tools/search_california_rules", '{"arguments": {"query": "x", "\\udfff": 1}}'),
    ("PATCH", "/api/tenant", '{"settings": {"note": 1e400}}'),
    ("PATCH", "/api/tenant", '{"settings": {"note": NaN}}'),
    ("PATCH", "/api/tenant", '{"company_name": "Acme \\ud800"}'),
    ("POST", "/api/projects", '{"name": "Tower", "contract_price": 1e400}'),
    ("POST", "/api/projects", '{"name": "Tower", "contract_price": -Infinity}'),
    ("POST", "/api/projects", '{"name": "Tower", "extra": {"x": "\\ud800"}}'),
    ("POST", "/api/auth/login", '{"email": "someone@example.com", "password": "NOT-A-REAL-PW-\\ud800"}'),
]


@pytest.mark.parametrize("method,path,body", BAD_BODIES)
def test_unwritable_json_is_a_422_and_nothing_is_stored(owner, method, path, body):
    before = owner.get("/api/audit").json()
    r = owner.request(method, path, content=body.encode(), headers=JSON)
    assert r.status_code == 422 and r.json()["detail"] == safe_json.PROBLEM, r.text
    assert "NOT-A-REAL-PW" not in r.text
    assert owner.get("/api/audit").json() == before                         # the check never ran
    assert owner.get("/api/me").status_code == 200 and owner.get("/api/projects").json() == []
    assert owner.get("/api/me").json()["tenant"]["company_name"] == "Acme Builders"


def test_a_field_member_cannot_break_the_owners_activity_log_and_exports(app, owner):
    with TestClient(app) as crew:
        join(crew, owner, "crew@acme.test", "field")
        r = crew.post("/api/tools/search_california_rules", content=b'{"query":"lien \\ud800"}', headers=JSON)
        assert r.status_code == 422
        assert crew.post("/api/tools/search_california_rules", json={"query": "lien"}).status_code == 200
    assert owner.get("/api/audit").status_code == 200
    export = owner.get("/api/tenant/export")
    assert export.status_code == 200 and json.loads(export.text)["complete"] is True


def test_rows_stored_before_the_check_stay_readable(app, platform, owner):
    """A lone surrogate already in the activity log, infinity in the company settings and in a project (what the
    review stored on live) no longer turn GET /api/audit, /api/me, /api/projects and the export into 500s or
    truncated files: they are repaired on the way out."""
    store, tid = platform.store, owner.tenant_id
    store.audit(tid, "crew@acme.test", "search_california_rules", {"query": "lien \ud800"}, None, source="api")
    store.modify_tenant_settings(tid, lambda cur: {**cur, "note": float("inf")})
    store.insert("projects", tid, {"name": "Tower", "contract_price": float("inf"), "extra": {"x": "\ud800"}})
    assert any(a["args"] == {"query": "lien \ufffd"} for a in owner.get("/api/audit").json())
    me = owner.get("/api/me")
    assert me.status_code == 200 and me.json()["tenant"]["settings"]["note"] is None
    projects = owner.get("/api/projects")
    assert projects.status_code == 200
    assert [(p["name"], p["contract_price"], p["extra"]["x"]) for p in projects.json()] == [("Tower", None, "\ufffd")]
    export = owner.get("/api/tenant/export")
    data = json.loads(export.text)                       # valid JSON to the last byte
    assert data["complete"] is True and "Infinity" not in export.text
    for fmt in ("csv", "json"):
        r = owner.get(f"/api/audit-export?log=activity&format={fmt}")
        assert r.status_code == 200 and "lien \ufffd" in r.text
    assert owner.get("/api/mcp/audit/export?format=json").status_code == 200


def test_valid_unicode_and_large_finite_numbers_still_work(owner):
    r = owner.post("/api/projects", json={"name": "Torre \U0001F3D7 Ni\u00f1o", "contract_price": 1e300})
    assert r.status_code == 201, r.text
    assert owner.request("PATCH", "/api/tenant", content='{"settings": {"emoji": "\\ud83c\\udfd7"}}'.encode(),
                         headers=JSON).status_code == 200        # a proper surrogate pair is one character
    assert owner.get("/api/me").json()["tenant"]["settings"]["emoji"] == "\U0001F3D7"
    assert owner.post("/api/subs/import", content=b"name,license_number\nAcme Plumbing,1e400\n",
                      headers={"content-type": "text/csv"}).status_code == 200   # CSV is not JSON


def test_safe_json_helpers():
    assert safe_json.fix_str("a\ud800b") == "a\ufffdb" and safe_json.fix_str("\ud83c\udfd7") == "\U0001F3D7"
    assert safe_json.problem({"k": [1, {"x": float("nan")}]}) and safe_json.problem({"\udc00": 1})
    assert not safe_json.problem({"k": ["ok", 1e308, None, True]})
    assert safe_json.clean({"a": [float("-inf"), "x\udfff"]}) == {"a": [None, "x\ufffd"]}
    deep = current = []
    for _ in range(5000):                                  # deeper than the recursion limit: no crash
        current.append([])
        current = current[0]
    assert safe_json.problem(deep) is False


# ---------------------------------------------------------------------------- web-05: 422s never echo what was sent
def test_validation_errors_do_not_echo_the_submitted_values(owner, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Hunter2-NOT-A-REAL-PW-" + "x" * 300
    r = owner.post("/api/auth/login", json={"email": "someone@example.com", "password": secret})
    assert r.status_code == 422 and "Hunter2" not in r.text
    detail = r.json()["detail"]
    assert detail and set(detail[0]) == {"loc", "msg", "type"} and detail[0]["loc"] == ["body", "password"]
    r = owner.post("/api/auth/login", content=b'{"email": "a@example.com", "password": "Hunter2-\\ud800"}',
                   headers=JSON)
    assert r.status_code == 422
    assert "Hunter2" not in caplog.text


# ---------------------------------------------------------------------------- auth-xff-trusted-proxy-hops
class _S:
    local_mode = False

    def __init__(self, hops):
        self.trusted_proxy_hops = hops


def test_three_trusted_hops_pick_the_address_cloudflare_saw():
    """Render's header (measured live): '<what the client sent>, <client address Cloudflare saw>, <Cloudflare's
    address>, <a Render proxy address>'. With 3 the client address wins whatever the client sends."""
    e = "203.0.113.7"
    for cf, v in (("162.158.1.2", "10.163.118.229"), ("172.70.9.8", "10.218.118.150")):
        assert forwarded_ip(_S(3), f"{e}, {cf}, {v}", "10.0.0.1") == e
        assert forwarded_ip(_S(3), f"198.51.100.11, 198.51.100.12, {e}, {cf}, {v}", "10.0.0.1") == e
        assert forwarded_ip(_S(3), f"not-an-ip, {e}, {cf}, {v}", "10.0.0.1") == e
        assert forwarded_ip(_S(2), f"{e}, {cf}, {v}", "10.0.0.1") == cf     # why not 2: Cloudflare's address
        assert forwarded_ip(_S(1), f"{e}, {cf}, {v}", "10.0.0.1") == v      # why not 1: Render's proxy
    assert forwarded_ip(_S(0), f"198.51.100.11, {e}, 10.1.1.1", "10.0.0.1") == "198.51.100.11"   # why not 0


def test_render_blueprint_trusts_three_proxy_hops():
    blueprint = (REPO / "render.yaml").read_text()
    i = blueprint.index("- key: TRUSTED_PROXY_HOPS")
    assert blueprint[i:i + 80].split("value:")[1].split()[0] == '"3"'


def test_login_lockout_holds_behind_rendered_proxies(tmp_path):
    a = make_app(tmp_path, trusted_proxy_hops=3)
    with TestClient(a) as c:
        signup(c)
        for i in range(5):
            hdr = {"x-forwarded-for": f"198.51.100.{i}, 203.0.113.7, 162.158.{i}.4, 10.0.{i}.9"}
            assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": "wrong-password"},
                          headers=hdr).status_code == 401
        hdr = {"x-forwarded-for": "192.0.2.99, 203.0.113.7, 172.70.1.1, 10.9.9.9"}           # a new forged entry
        assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": "wrong-password"},
                      headers=hdr).status_code == 429


# ---------------------------------------------------------------------------- data-audit-gaps
def test_team_company_and_account_changes_are_in_the_activity_log(app, owner):
    with TestClient(app) as crew:
        join(crew, owner, "crew@acme.test", "field")
        uid = crew.get("/api/me").json()["user"]["id"]
    assert owner.patch(f"/api/users/{uid}", json={"role": "owner"}).status_code == 200
    assert audit_row(owner, "user.role_change")["args"] == {"user_id": uid, "email": "crew@acme.test",
                                                           "from": "field", "to": "owner"}
    assert owner.patch(f"/api/users/{uid}", json={"role": "field", "name": "Crew Lead"}).status_code == 200
    assert audit_row(owner, "user.role_change")["args"]["to"] == "field"
    assert audit_row(owner, "user.update")["args"] == {"user_id": uid, "fields": ["name"]}
    assert owner.delete(f"/api/users/{uid}").status_code == 200
    assert audit_row(owner, "user.remove")["args"] == {"user_id": uid, "email": "crew@acme.test", "role": "field",
                                                       "was_pending": False}
    assert owner.patch("/api/tenant", json={"company_name": "Acme Two", "settings": {"x": 1}}).status_code == 200
    assert audit_row(owner, "tenant.update")["args"] == {"fields": ["company_name", "settings.x"]}
    assert owner.post("/api/calendar/feed/rotate").status_code == 200
    assert "calendar.feed_rotate" in tools_of(owner)
    r = owner.post("/api/auth/password", json={"current_password": PW, "new_password": PW + "-new"})
    assert r.status_code == 200
    owner.headers["x-csrf-token"] = owner.cookies.get("tc_csrf")
    assert audit_row(owner, "auth.password_changed")["user"] == "owner@acme.test"
    n = len(tools_of(owner))
    assert owner.patch(f"/api/users/{owner.get('/api/me').json()['user']['id']}", json={"name": None}).status_code \
        == 200 and len(tools_of(owner)) == n                       # nothing changed: no row


def test_deleting_records_leaves_a_trace(app, owner):
    with TestClient(app) as office:
        join(office, owner, "office@acme.test", "office")
        pid = office.post("/api/projects", json={"name": "Tower"}).json()["id"]
        sid = office.post("/api/subs", json={"name": "Acme Plumbing", "license_number": "123456"}).json()["id"]
        did = office.post("/api/documents", json={"title": "Payroll notes", "body_markdown": "rates"}).json()["id"]
        log = office.post("/api/field_logs", json={"kind": "incident", "date": "2026-11-20", "topic": "Fall",
                                                    "notes": "private injury details"}).json()
        for path in (f"/api/field_logs/{log['id']}", f"/api/documents/{did}", f"/api/subs/{sid}",
                     f"/api/projects/{pid}"):
            assert office.delete(path).status_code == 200, path
    rows = {a["tool"]: a for a in owner.get("/api/audit").json()}
    assert rows["project.delete"]["args"] == {"id": pid, "name": "Tower", "status": rows["project.delete"]["args"]
                                              ["status"]} and rows["project.delete"]["user"] == "office@acme.test"
    assert rows["sub.delete"]["args"] == {"id": sid, "name": "Acme Plumbing", "license_number": "123456"}
    assert rows["document.delete"]["args"] == {"id": did, "title": "Payroll notes", "kind": "note"}
    assert rows["field_log.delete"]["args"] == {"id": log["id"], "kind": "incident", "date": "2026-11-20",
                                                "project_id": None}
    assert "private injury details" not in json.dumps(rows)
    assert owner.delete(f"/api/projects/{pid}").status_code == 404        # already gone: no second row
    assert tools_of(owner).count("project.delete") == 1


# ---------------------------------------------------------------------------- auth-api-key-survives-account-recovery
def test_a_key_made_with_a_stolen_session_ends_with_the_password(app, owner):
    with TestClient(app) as office, TestClient(app) as thief:
        join(office, owner, "office@acme.test", "office")
        thief.cookies.update(office.cookies)
        thief.headers["x-csrf-token"] = office.headers["x-csrf-token"]
        key = thief.post("/api/keys", json={"name": "mine"}).json()["key"]
        bearer = {"authorization": f"Bearer {key}"}
        assert thief.get("/api/projects", headers=bearer).status_code == 200
        r = office.post("/api/auth/password", json={"current_password": PW, "new_password": PW + "-new"})
        assert r.status_code == 200 and r.json()["api_keys_revoked"] == 1
        assert thief.get("/api/projects", headers=bearer).status_code == 401
        row = audit_row(owner, "api_key.revoke")
        assert row["args"]["reason"] == "password_changed" and row["args"]["keys"] == 1


def test_a_reset_link_also_turns_off_the_persons_keys(app, owner):
    with TestClient(app) as office, TestClient(app) as anon:
        inv = join(office, owner, "office@acme.test", "office")
        key = office.post("/api/keys", json={"name": "mine"}).json()["key"]
        link = owner.post(f"/api/users/{inv['id']}/reset-link").json()["reset_path"].rsplit("/", 1)[1]
        r = anon.post("/api/auth/reset", json={"token": link, "new_password": PW + "-reset"})
        assert r.status_code == 200 and r.json()["api_keys_revoked"] == 1
        assert anon.get("/api/projects", headers={"authorization": f"Bearer {key}"}).status_code == 401
    assert audit_row(owner, "api_key.revoke")["args"]["reason"] == "password_reset"


# ---------------------------------------------------------------------------- auth/data-email-squatting
def test_a_stranger_adding_someone_does_not_lock_their_address(app, owner):
    """A trial company adding someone it doesn't employ (who never joins) no longer keeps that address from their
    real employer or from signing up: the pending row gives way, and its company sees why."""
    with TestClient(app) as stranger, TestClient(app) as employer, TestClient(app) as pat:
        signup(stranger, email="boss@stranger.test", company="Stranger Co", license_number="111111")
        squat = stranger.post("/api/users", json={"email": "pat@builder.test"})
        assert squat.status_code == 200
        signup(employer, email="ceo@builder.test", company="Builder Co", license_number="222222")
        real = employer.post("/api/users", json={"email": "pat@builder.test", "role": "office"})
        assert real.status_code == 200, real.text
        assert stranger.post("/api/auth/invite/accept", json={"token": squat.json()["invite_path"].rsplit("/", 1)[1],
                                                              "password": PW}).status_code == 404   # link ended
        released = audit_row(stranger, "user.invite_released")
        assert released["args"]["reason"] == "invited_by_another_company" and released["user"] == \
            "TradeComply (automatic)"
        assert "pat@builder.test" not in [u["email"] for u in stranger.get("/api/users").json()]
        # Pat joins the real employer; from then on the address is Pat's
        token = real.json()["invite_path"].rsplit("/", 1)[1]
        assert pat.post("/api/auth/invite/accept", json={"token": token, "password": PW}).status_code == 200
        assert stranger.post("/api/users", json={"email": "pat@builder.test"}).status_code == 409
        assert pat.post("/api/auth/signup", json={"company_name": "X", "email": "pat@builder.test",
                                                  "password": PW}).status_code == 409


def test_signing_up_frees_an_address_a_stranger_added(app):
    with TestClient(app) as stranger, TestClient(app) as ceo:
        signup(stranger, email="boss@stranger.test", company="Stranger Co", license_number="111111")
        assert stranger.post("/api/users", json={"email": "ceo@realbuilder.test"}).status_code == 200
        signup(ceo, email="ceo@realbuilder.test", company="Real Builder", license_number="333333")
        assert audit_row(stranger, "user.invite_released")["args"]["reason"] == "signed_up"


def test_who_keeps_an_address(app, owner):
    with TestClient(app) as crew, TestClient(app) as other:
        join(crew, owner, "crew@acme.test", "field")                   # joined: theirs
        pending = owner.post("/api/users", json={"email": "new@acme.test"})
        assert owner.post("/api/users", json={"email": "new@acme.test"}).status_code == 409   # same company
        signup(other, email="b@bravo.test", company="Bravo", license_number="654321")
        assert other.post("/api/users", json={"email": "crew@acme.test"}).status_code == 409
        assert other.post("/api/users", json={"email": "owner@acme.test"}).status_code == 409
        assert pending.status_code == 200 and other.post("/api/users", json={"email": "new@acme.test"}).status_code \
            == 200                                                    # another company's never-joined invite


def test_a_trial_company_can_add_only_so_many_people_a_day(platform, owner):
    platform.trial_people_day_limiter.limit = 3
    for i in range(3):
        assert owner.post("/api/users", json={"email": f"p{i}@acme.test"}).status_code == 200
    r = owner.post("/api/users", json={"email": "p9@acme.test"})
    assert r.status_code == 429 and "free trial" in r.json()["detail"] and r.headers["retry-after"]


# ---------------------------------------------------------------------------- auth-signup-enumeration
def test_asking_signup_whether_an_address_exists_counts_against_its_limit(app, platform, owner):
    with TestClient(app) as probe:
        for _ in range(platform.signup_limiter.limit - 1):           # the owner's own sign-up used one
            r = probe.post("/api/auth/signup", json={"company_name": "X", "email": "owner@acme.test",
                                                     "password": PW})
            assert r.status_code == 409
        r = probe.post("/api/auth/signup", json={"company_name": "X", "email": "owner@acme.test", "password": PW})
        assert r.status_code == 429
        r = probe.post("/api/auth/signup", json={"company_name": "X", "email": "new@acme.test", "password": PW})
        assert r.status_code == 429


def test_a_malformed_address_does_not_use_up_signups(app, platform):
    with TestClient(app) as c:
        for _ in range(platform.signup_limiter.limit + 2):
            assert c.post("/api/auth/signup", json={"company_name": "X", "email": "not-an-address",
                                                    "password": PW}).status_code == 422
        signup(c)


# ---------------------------------------------------------------------------- auth-cross-site-log-download
@pytest.mark.parametrize("path", ["/api/audit-export?log=activity&format=csv", "/api/audit-export?log=mcp&format=json",
                                  "/api/mcp/audit/export?format=csv", "/api/integrations/calendar_feed/calendar.ics",
                                  "/api/calendar.ics", "/api/tenant/export"])
def test_another_site_cannot_start_a_signed_in_download(owner, path):
    n = len(tools_of(owner))
    for site in ("cross-site", "same-site"):
        r = owner.get(path, headers={"sec-fetch-site": site})
        assert r.status_code == 403 and "TradeComply" in r.json()["detail"], (path, r.status_code)
    assert len(tools_of(owner)) == n                                  # no download rows
    r = owner.get(path, headers={"sec-fetch-site": "same-origin"})
    assert r.status_code == 200, (path, r.text[:200])
    assert owner.get(path).status_code == 200                         # API clients send no such header


def test_the_private_calendar_link_still_opens_from_anywhere(owner):
    url = owner.post("/api/calendar/feed/rotate").json()["url"]
    path = url.split("://", 1)[1].split("/", 1)[1]
    with TestClient(owner.app) as anon:
        assert anon.get("/" + path, headers={"sec-fetch-site": "cross-site"}).status_code == 200


# ---------------------------------------------------------------------------- web-03: no words from the URL
def test_sign_in_errors_travel_in_a_cookie_not_the_url(owner):
    r = owner.get("/api/integrations/not-a-thing/oauth/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 303 and "message" not in r.headers["location"]
    from urllib.parse import unquote
    assert unquote(r.cookies.get("tc_notice").strip('"')) == "Unknown integration"
    app_js = (REPO / "app/static/app.js").read_text()
    assert "query.get('message')" not in app_js and "takeNotice()" in app_js


# ---------------------------------------------------------------------------- ux-owner-password-recovery-dead-end
def test_forgot_password_tells_an_owner_who_to_contact(tmp_path):
    with TestClient(make_app(tmp_path / "a", support_email="help@operator.example.test")) as c:
        r = c.post("/api/auth/forgot", json={"email": "owner@acme.test"})
        assert r.json()["message"].endswith("If you are the owner, contact support at help@operator.example.test.")
    with TestClient(make_app(tmp_path / "b")) as c:
        r = c.post("/api/auth/forgot", json={"email": "owner@acme.test"})
        assert r.json()["message"].endswith("If you are the owner, ask whoever runs this TradeComply server.")


def test_an_oversized_chunked_json_body_is_still_a_413(tmp_path):
    """The JSON check reads the body before the route does; SecurityHeaders' cap still answers 413, not a 500."""
    with TestClient(make_app(tmp_path, max_body_bytes=1000)) as c:
        signup(c)

        def chunks():
            for _ in range(10):
                yield b'{"settings": {"a": "' + b"x" * 200 + b'"}}'
        r = c.patch("/api/tenant", content=chunks(), headers=JSON)
        assert r.status_code == 413 and r.json()["detail"] == "Request too large"


def test_mcp_argument_fingerprints_survive_a_lone_surrogate():
    import hashlib

    from app import mcp_governance as governance
    assert governance.args_sha256({"q": "lien"}) == hashlib.sha256(b'{"q":"lien"}').hexdigest()   # unchanged
    assert len(governance.args_sha256({"q": "lien \ud800"})) == 64        # was a UnicodeEncodeError: no audit row


# ---------------------------------------------------------------------------- data-audit-gaps: every write route is
# either in the activity log (the action it writes) or listed here with why not. A new route must be added here.
LOGGED = "logged"
WRITE_ROUTES = {
    "POST /api/auth/signup": "not logged: creates the company; its log starts with it",
    "POST /api/auth/login": "not logged: password sign-ins (SSO and owner break-glass sign-ins are)",
    "POST /api/auth/logout": "not logged: ends this session only",
    "POST /api/auth/password": (LOGGED, "auth.password_changed"),
    "POST /api/auth/forgot": "not logged: answers the same for every address",
    "POST /api/auth/link": "not logged: only reads what a link is for",
    "POST /api/auth/reset": (LOGGED, "auth.password_reset"),
    "POST /api/auth/invite/accept": (LOGGED, "user.joined"),
    "POST /api/auth/email/confirm": (LOGGED, "auth.email_confirmed"),
    "POST /api/auth/email/resend": "not logged: an email to the person's own address",
    "POST /api/auth/email/change": "logged when confirmed (auth.email_changed); until then nothing changes",
    "POST /api/auth/email-test": "not logged: a test email to the owner's own address",
    "POST /api/billing/checkout": "not logged: opens Stripe; the subscription arrives by Stripe's signed event",
    "POST /api/billing/portal": "not logged: opens Stripe's billing page",
    "POST /api/billing/webhook": "not logged: Stripe's signed events (Stripe keeps their history)",
    "DELETE /api/tenant": "not logged: deletes the activity log with the company; the receipt and server log record it",
    "PATCH /api/tenant": (LOGGED, "tenant.update"),
    "POST /api/users": (LOGGED, "user.invite"),
    "POST /api/users/{user_id}/invite": (LOGGED, "user.invite_resent"),
    "POST /api/users/{user_id}/reset-link": (LOGGED, "user.reset_link"),
    "PATCH /api/users/{user_id}": (LOGGED, "user.role_change"),
    "DELETE /api/users/{user_id}": (LOGGED, "user.remove"),
    "POST /api/keys": (LOGGED, "api_key.create"),
    "PATCH /api/keys/{key_id}": (LOGGED, "api_key.update"),
    "DELETE /api/keys/{key_id}": (LOGGED, "api_key.revoke"),
    "POST /api/tools/{name}": "logged under the tool's own name",
    "POST /api/projects": "not logged: records keep their own created time; deleting is logged",
    "PATCH /api/projects/{project_id}": "not logged: records keep their updated time; deleting is logged",
    "DELETE /api/projects/{project_id}": (LOGGED, "project.delete"),
    "POST /api/subs": "not logged: records keep their own created time; deleting is logged",
    "PATCH /api/subs/{sub_id}": "not logged: records keep their updated time; deleting is logged",
    "DELETE /api/subs/{sub_id}": (LOGGED, "sub.delete"),
    "POST /api/subs/import": "not logged: adds or updates subs, which keep their own times",
    "POST /api/documents": "not logged: records keep their own created time; deleting is logged",
    "POST /api/documents/from_result": "not logged: records keep their own created time; deleting is logged",
    "PATCH /api/documents/{doc_id}": "not logged: records keep their updated time; deleting is logged",
    "DELETE /api/documents/{doc_id}": (LOGGED, "document.delete"),
    "POST /api/field_logs": "not logged: the log itself is the record (who, when, signatures)",
    "DELETE /api/field_logs/{log_id}": (LOGGED, "field_log.delete"),
    "POST /api/field_logs/sync": "not logged: the logs themselves are the record",
    "POST /api/calendar/feed/rotate": (LOGGED, "calendar.feed_rotate"),
    "POST /api/assistant/conversations": "not logged: a private chat; the checks it runs are logged",
    "DELETE /api/assistant/conversations/{conversation_id}": "not logged: a private chat",
    "POST /api/assistant/conversations/{conversation_id}/stop": "not logged: a private chat",
    "POST /api/assistant/conversations/{conversation_id}/messages": "not logged: a private chat; its checks are",
    "PUT /api/assistant/settings": (LOGGED, "assistant.settings"),
    "POST /api/integrations/docusign/preview": "not logged: shows what would be sent",
    "POST /api/integrations/docusign/envelopes": (LOGGED, "integrations.docusign_"),
    "POST /api/integrations/procore/company": (LOGGED, "integrations.procore_"),
    "POST /api/integrations/procore/sync": (LOGGED, "integrations.procore_"),
    "POST /api/integrations/deadline-reminders/scan": (LOGGED, "integrations.deadline_scan"),
    "POST /api/integrations/twilio_sms/recipients": (LOGGED, "sms_recipient_add"),
    "DELETE /api/integrations/twilio_sms/recipients/{rid}": (LOGGED, "sms_recipient_remove"),
    "POST /api/integrations/twilio_sms/recipients/{rid}/test": (LOGGED, "sms_recipient_test"),
    "POST /api/integrations/twilio-sms/inbound": "not logged: Twilio's signed STOP/START replies (kept on the number)",
    "POST /api/integrations/calendar_feed/links": (LOGGED, "link_create"),
    "DELETE /api/integrations/calendar_feed/links": (LOGGED, "link_revoke_all"),
    "POST /api/integrations/calendar_feed/links/rotate": (LOGGED, "link_rotate"),
    "DELETE /api/integrations/calendar_feed/links/{link_id}": (LOGGED, "link_revoke"),
    "POST /api/integrations/schedule_import/projects/{project_id}/upload": (LOGGED, "integrations.schedule_import"),
    "POST /api/integrations/splunk/export": (LOGGED, "integrations.splunk_export"),
    "PUT /api/sso/config": (LOGGED, "sso.config"),
    "DELETE /api/sso/config": (LOGGED, "sso.delete"),
    "POST /api/sso/test": "not logged: tries the settings, changes nothing",
    "POST /api/auth/sso/start": "not logged: starts a sign-in (sso.login is logged when it ends)",
    "POST /api/integrations/webhooks": (LOGGED, "webhook_create"),
    "PATCH /api/integrations/webhooks/{webhook_id}": (LOGGED, "webhook_update"),
    "DELETE /api/integrations/webhooks/{webhook_id}": (LOGGED, "webhook_delete"),
    "POST /api/integrations/webhooks/{webhook_id}/rotate-secret": (LOGGED, "webhook_rotate_secret"),
    "POST /api/integrations/webhooks/{webhook_id}/test": (LOGGED, "webhook_test"),
    "PUT /api/integrations/{provider}/config": (LOGGED, '"config"'),
    "POST /api/integrations/{provider}/connect": (LOGGED, '"connect"'),
    "POST /api/integrations/{provider}/disconnect": (LOGGED, '"disconnect"'),
    "POST /api/integrations/{provider}/test": (LOGGED, '"test"'),
    "POST /api/integrations/{provider}/oauth/start": (LOGGED, "oauth_start"),
    "POST /api/oauth/requests/{request_id}/approve": (LOGGED, "oauth.connect"),
    "POST /api/oauth/requests/{request_id}/deny": (LOGGED, "oauth.deny"),
    "DELETE /api/oauth/connections/{grant_id}": (LOGGED, "oauth.disconnect"),
}


def test_every_write_route_is_logged_or_says_why_not(app):
    paths = app.openapi()["paths"]
    routes = {f"{m.upper()} {p}" for p, ops in paths.items() for m in ops if m.upper() not in ("GET", "HEAD", "OPTIONS")}
    assert routes == set(WRITE_ROUTES), {"new, classify them": sorted(routes - set(WRITE_ROUTES)),
                                         "gone": sorted(set(WRITE_ROUTES) - routes)}
    source = "".join(p.read_text() for p in (REPO / "app").rglob("*.py"))
    for route, why in WRITE_ROUTES.items():
        if isinstance(why, tuple):
            assert why[1] in source, (route, why)
