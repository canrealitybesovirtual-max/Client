"""/api/integrations and the connector framework: registry and validation, provider list with operator status,
per-tenant settings (secrets encrypted, never returned), connect/test/disconnect, the OAuth authorization-code flow
with PKCE and a signed single-use state, token refresh, webhooks CRUD and test sends, roles, CSRF, entitlement.

The template connectors (app/connectors/_template.py) are registered for these tests, so the template itself is
proven to work. Their vendor is example.com (RFC 2606), mocked with httpx.MockTransport."""
import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors as registry
from app import webhooks
from app.connectors import (ConfigField, Connector, OAuth2Spec, TestResult, code_challenge,
                            validate_connector_class)
from app.connectors._template import ExampleApiKeyConnector, ExampleOAuthConnector
from app.crypto import connection_context, webhook_context
from tests.support import backend, json_response, join, make_app, mock_http, redirect_query, signup  # noqa: F401

OAUTH_ENV = {"EXAMPLE_CLIENT_ID": "cid-123", "EXAMPLE_CLIENT_SECRET": "csecret-456"}
SLACK_URL = "https://hooks.slack.com/services/" + "T0000/B0000/" + "X" * 24  # built at runtime for secret scanners
TEAMS_URL = "https://prod-00.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SIGSECRET"


@pytest.fixture(autouse=True)
def example_connectors():
    registry.register(ExampleApiKeyConnector)
    registry.register(ExampleOAuthConnector)
    yield
    registry.unregister(ExampleApiKeyConnector.provider)
    registry.unregister(ExampleOAuthConnector.provider)


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


class Vendor:
    """The placeholder example.com API and OAuth server, answering in RFC 6749 shapes."""

    def __init__(self):
        self.api_key = "sk-test-123"
        self.me_status = 200
        self.tokens = iter([{"access_token": "at-1", "token_type": "Bearer", "expires_in": 3600,
                             "refresh_token": "rt-1"},
                            {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600,
                             "refresh_token": "rt-2"},
                            {"access_token": "at-3", "token_type": "Bearer", "expires_in": 3600}])
        self.token_error = None
        self.challenge = None
        self.token_forms = []
        self.alerts = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "login.example.com" and url.path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            with self.lock:
                self.token_forms.append(form)
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            basic = base64.b64encode(b"cid-123:csecret-456").decode()
            if request.headers.get("authorization") != f"Basic {basic}":
                return json_response(401, {"error": "invalid_client"})
            if self.token_error:
                return json_response(400, {"error": self.token_error, "error_description": "nope"})
            if form["grant_type"] == "authorization_code":
                if form.get("code") != "good-code" or code_challenge(form.get("code_verifier", "")) != self.challenge:
                    return json_response(400, {"error": "invalid_grant"})
            elif form["grant_type"] == "refresh_token":
                time.sleep(0.05)       # long enough for a concurrent caller to pile up behind the lock
            with self.lock:
                return json_response(200, next(self.tokens), {"cache-control": "no-store"})
        if url.host in ("api.example.com", "eu.api.example.com") and url.path == "/v1/me":
            auth = request.headers.get("authorization")
            if self.me_status != 200 or auth not in (f"Bearer {self.api_key}", "Bearer at-1", "Bearer at-2",
                                                      "Bearer at-3"):
                return json_response(401, {"error": "unauthorized"})
            return json_response(200, {"company_name": "Acme Example"})
        if url.host == "api.example.com" and url.path == "/v1/alerts":
            self.alerts.append(json.loads(request.content))
            return json_response(201, {"id": len(self.alerts)})
        return json_response(404, {"error": "not_found"})


@pytest.fixture
def vendor(platform):
    v = Vendor()
    v.seen = mock_http(platform, v)
    return v


def field_and_office_clients(app, owner):
    out = {}
    for role in ("field", "office"):
        c = TestClient(app)
        join(c, owner, f"{role}@acme.test", role)
        out[role] = c
    return out


# ---------------------------------------------------------------------------- registry
def test_discovery_loads_every_connector_module_cleanly():
    registry.discover(force=True)
    assert registry.import_errors == {}
    assert ExampleApiKeyConnector.__module__.rsplit(".", 1)[1].startswith("_")      # templates aren't discovered


def test_template_connectors_validate():
    assert validate_connector_class(ExampleApiKeyConnector) == []
    assert validate_connector_class(ExampleOAuthConnector) == []


def _make(**attrs):
    base = {"provider": "acme_pm", "display_name": "Acme PM", "catalog_id": "acme-pm", "description": "d",
            "docs_url": "https://docs.example.com/api", "test": lambda self: TestResult(True, "ok")}
    base.update(attrs)
    return type("Bad", (Connector,), base)


@pytest.mark.parametrize("attrs,match", [
    ({"provider": "Acme"}, "provider"),
    ({"provider": "webhooks"}, "reserved"),
    ({"display_name": ""}, "display_name"),
    ({"catalog_id": ""}, "catalog_id"),
    ({"docs_url": "http://docs.example.com"}, "docs_url"),
    ({"docs_url": ""}, "docs_url"),
    ({"required_env": ("lower_case",)}, "required_env"),
    ({"subscribes": ("no_such_event",)}, "subscribes"),
    ({"subscribes": ("payroll_issue",)}, "handle_event"),
    ({"test": Connector.test}, "test"),
    ({"oauth": OAuth2Spec(authorize_url="https://a.example.com/auth", token_url="https://a.example.com/token",
                          client_id_env="ACME_ID", client_secret_env="ACME_SECRET")}, "required_env"),
    ({"oauth": OAuth2Spec(authorize_url="http://a.example.com/auth", token_url="https://a.example.com/token",
                          client_id_env="ACME_ID"), "required_env": ("ACME_ID",)}, "authorize_url"),
])
def test_invalid_connectors_are_rejected(attrs, match):
    with pytest.raises(ValueError, match=match):
        registry.register(_make(**attrs))


def test_provider_ids_are_unique():
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_make(provider="example_apikey"))


@pytest.mark.parametrize("kwargs", [
    {"name": "Bad Name", "label": "x"}, {"name": "oauth", "label": "x"}, {"name": "x", "label": "x", "type": "file"},
    {"name": "x", "label": "x", "type": "password"}, {"name": "x", "label": "x", "type": "select"},
    {"name": "x", "label": ""},
])
def test_config_field_declarations_are_checked(kwargs):
    with pytest.raises(ValueError):
        ConfigField(**kwargs)


def test_config_field_cleaning():
    num = ConfigField("n", "Number", type="number")
    assert num.clean("12") == 12 and num.clean("1.5") == 1.5 and num.clean("") is None
    with pytest.raises(registry.ConfigError):
        num.clean("nan")
    assert ConfigField("b", "Flag", type="boolean").clean("yes") is True
    with pytest.raises(registry.ConfigError):
        ConfigField("u", "Site", type="url").clean("http://intranet.local")
    assert ConfigField("u", "Site", type="url").clean(" https://sandbox.example.com ") == "https://sandbox.example.com"
    with pytest.raises(registry.ConfigError):
        ConfigField("e", "Email", type="email").clean("not-an-email")
    with pytest.raises(registry.ConfigError):
        ConfigField("t", "Text", max_length=3).clean("toolong")
    assert ConfigField("p", "Secret", type="password", secret=True).clean(" keep spaces ") == " keep spaces "


# ---------------------------------------------------------------------------- listing and roles
def test_provider_list_shows_operator_and_tenant_status(owner, platform):
    platform.env = {}
    body = owner.get("/api/integrations").json()
    by = {p["provider"]: p for p in body["providers"]}
    assert by["example_apikey"]["auth"] == "config" and by["example_apikey"]["operator_configured"] is True
    assert by["example_oauth"]["auth"] == "oauth2" and by["example_oauth"]["operator_configured"] is False
    assert by["example_oauth"]["missing_env"] == ["EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET"]
    assert by["example_apikey"]["connection"]["status"] == "disconnected"
    fields = {f["name"]: f for f in by["example_apikey"]["config_schema"]}
    assert fields["api_key"]["secret"] is True and fields["api_key"]["type"] == "password"
    assert fields["region"]["options"][0] == {"value": "us", "label": "United States"}
    assert {e["name"] for e in body["events"]} == {"deadline_due_soon", "license_problem_found", "sub_noncompliant",
                                                   "contract_generated", "injury_report_due", "payroll_issue"}
    assert body["load_errors"] == []
    platform.env = dict(OAUTH_ENV)
    assert {p["provider"]: p for p in owner.get("/api/integrations").json()["providers"]}[
        "example_oauth"]["operator_configured"] is True
    assert "csecret-456" not in owner.get("/api/integrations").text


def test_field_role_reads_the_list_but_changes_nothing(app, owner):
    clients = field_and_office_clients(app, owner)
    field = clients["field"]
    r = field.get("/api/integrations")
    assert r.status_code == 200 and "missing_env" not in r.json()["providers"][0] and "load_errors" not in r.json()
    for method, path, body in [("GET", "/api/integrations/example_apikey", None),
                               ("PUT", "/api/integrations/example_apikey/config", {"config": {}}),
                               ("POST", "/api/integrations/example_apikey/connect", {}),
                               ("POST", "/api/integrations/example_apikey/test", None),
                               ("POST", "/api/integrations/example_apikey/disconnect", None),
                               ("POST", "/api/integrations/example_oauth/oauth/start", None),
                               ("GET", "/api/integrations/webhooks", None),
                               ("POST", "/api/integrations/webhooks", {"name": "x", "url": SLACK_URL,
                                                                       "kind": "slack"}),
                               ("GET", "/api/integrations/outbound", None)]:
        assert field.request(method, path, json=body).status_code == 403, (method, path)
    assert clients["office"].get("/api/integrations/example_apikey").status_code == 200


def test_writes_need_the_csrf_header(owner):
    token = owner.headers.pop("x-csrf-token")
    assert owner.put("/api/integrations/example_apikey/config", json={"config": {}}).status_code == 403
    assert owner.post("/api/integrations/webhooks", json={"name": "x", "url": SLACK_URL}).status_code == 403
    owner.headers["x-csrf-token"] = token


def test_unknown_provider_is_404(owner):
    assert owner.get("/api/integrations/nope").status_code == 404
    assert owner.put("/api/integrations/nope/config", json={"config": {}}).status_code == 404


# ---------------------------------------------------------------------------- settings
def test_secret_settings_are_encrypted_and_never_returned(owner, platform, vendor):
    r = owner.put("/api/integrations/example_apikey/config",
                  json={"config": {"api_key": "sk-test-123", "region": "eu", "company_id": "42"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"] == {"region": "eu", "company_id": "42"} and body["secrets_saved"] == ["api_key"]
    assert "sk-test-123" not in r.text
    assert "sk-test-123" not in owner.get("/api/integrations/example_apikey/config").text
    row = platform.store.get_connection(owner.tenant_id, "example_apikey", with_secret=True)
    assert "sk-test-123" not in json.dumps(row)
    assert platform.secret_box.decrypt_json(row["secret_blob"], connection_context(owner.tenant_id, "example_apikey")) \
        == {"api_key": "sk-test-123"}
    # a blank secret keeps the saved one; other settings change
    r = owner.put("/api/integrations/example_apikey/config", json={"config": {"api_key": "", "region": "us"}})
    assert r.json()["secrets_saved"] == ["api_key"] and r.json()["config"]["region"] == "us"
    assert r.json()["config"]["company_id"] == "42"
    audit = owner.get("/api/audit").json()
    assert any(a["tool"] == "integrations.config" for a in audit) and "sk-test-123" not in json.dumps(audit)


@pytest.mark.parametrize("config,match", [
    ({"api_key": "k", "nope": 1}, "Unknown setting"),
    ({"api_key": "k", "region": "mars"}, "Region"),
    ({"api_key": "k", "company_id": "12ab"}, "Company ID"),
    ({"region": "us"}, "API key is required"),
])
def test_bad_settings_are_422(owner, config, match):
    r = owner.put("/api/integrations/example_apikey/config", json={"config": config})
    assert r.status_code == 422 and match in r.json()["detail"]


# ---------------------------------------------------------------------------- connect / test / disconnect
def test_api_key_connect_test_and_disconnect(owner, platform, vendor):
    r = owner.post("/api/integrations/example_apikey/connect", json={"config": {"api_key": "sk-test-123"}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected" and r.json()["external_account"] == "Acme Example"
    assert vendor.seen[-1].headers["authorization"] == "Bearer sk-test-123"
    assert owner.post("/api/integrations/example_apikey/test").json()["ok"] is True
    vendor.me_status = 401
    r = owner.post("/api/integrations/example_apikey/test").json()
    assert r["ok"] is False and r["connection"]["status"] == "error" and "rejected" in r["connection"]["last_error"]
    vendor.me_status = 200
    assert owner.post("/api/integrations/example_apikey/test").json()["connection"]["status"] == "connected"
    r = owner.post("/api/integrations/example_apikey/disconnect")
    assert r.json()["status"] == "disconnected" and r.json()["has_secret"] is False
    detail = owner.get("/api/integrations/example_apikey").json()
    assert detail["secrets_saved"] == [] and detail["config"]["region"] == "us"        # settings are kept
    tools = [a["tool"] for a in owner.get("/api/audit").json()]
    assert {"integrations.connect", "integrations.test", "integrations.disconnect"} <= set(tools)


def test_wrong_api_key_marks_the_connection_as_error(owner, vendor):
    r = owner.post("/api/integrations/example_apikey/connect", json={"config": {"api_key": "sk-wrong"}})
    assert r.status_code == 200 and r.json()["status"] == "error" and r.json()["test"]["ok"] is False


def test_lapsed_subscription_blocks_new_connections_but_not_disconnect(owner, platform, vendor):
    owner.post("/api/integrations/example_apikey/connect", json={"config": {"api_key": "sk-test-123"}})
    platform.store.update_tenant(owner.tenant_id, trial_ends="2000-01-01T00:00:00+00:00")
    assert owner.post("/api/integrations/example_apikey/connect", json={}).status_code == 402
    assert owner.post("/api/integrations/example_apikey/test").status_code == 402
    assert owner.post("/api/integrations/example_oauth/oauth/start").status_code == 402
    assert owner.post("/api/integrations/webhooks", json={"name": "x", "url": SLACK_URL,
                                                          "kind": "slack"}).status_code == 402
    assert owner.get("/api/integrations").status_code == 200
    assert owner.post("/api/integrations/example_apikey/disconnect").json()["status"] == "disconnected"


def test_tenants_never_see_each_others_connections(app, owner, vendor):
    owner.post("/api/integrations/example_apikey/connect", json={"config": {"api_key": "sk-test-123"}})
    with TestClient(app) as other:
        signup(other, email="b@two.test", company="Two")
        detail = other.get("/api/integrations/example_apikey").json()
        assert detail["connection"]["status"] == "disconnected" and detail["secrets_saved"] == []
        assert other.post("/api/integrations/example_apikey/test").status_code == 409      # nothing saved here


# ---------------------------------------------------------------------------- OAuth
def start_oauth(owner, vendor) -> dict:
    r = owner.post("/api/integrations/example_oauth/oauth/start")
    assert r.status_code == 200, r.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["authorize_url"]).query).items()}
    vendor.challenge = q.get("code_challenge")
    return q


def callback(c, query: str):
    return c.get(f"/api/integrations/example_oauth/oauth/callback?{query}", follow_redirects=False)


def result_of(r) -> dict:
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith("/#/settings?")
    return redirect_query(r)


def test_oauth_needs_the_operator_app_credentials(owner, platform):
    platform.env = {}
    r = owner.post("/api/integrations/example_oauth/oauth/start")
    assert r.status_code == 503 and "EXAMPLE_CLIENT_ID" in r.json()["detail"]
    assert owner.post("/api/integrations/example_apikey/oauth/start").status_code == 409     # not an OAuth one


def test_oauth_authorization_code_flow_with_pkce(owner, platform, vendor):
    platform.env = dict(OAUTH_ENV)
    q = start_oauth(owner, vendor)
    assert q["response_type"] == "code" and q["client_id"] == "cid-123" and q["scope"] == "read"
    assert q["redirect_uri"] == "http://testserver/api/integrations/example_oauth/oauth/callback"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert "csecret" not in json.dumps(q)
    assert platform.store.get_connection(owner.tenant_id, "example_oauth")["status"] == "pending"
    r = callback(owner, f"code=good-code&state={q['state']}")
    assert result_of(r) == {"integration": "example_oauth", "result": "connected"}
    form = vendor.token_forms[-1]
    assert form["grant_type"] == "authorization_code" and form["redirect_uri"] == q["redirect_uri"]
    assert code_challenge(form["code_verifier"]) == q["code_challenge"] and "client_secret" not in form
    conn = platform.store.get_connection(owner.tenant_id, "example_oauth", with_secret=True)
    assert conn["status"] == "connected" and conn["external_account"] == "Acme Example"
    assert "at-1" not in conn["secret_blob"] and "rt-1" not in conn["secret_blob"]
    detail = owner.get("/api/integrations/example_oauth").json()
    assert detail["signed_in"] is True and "at-1" not in json.dumps(detail)
    # the same state can't be used twice
    assert result_of(callback(owner, f"code=good-code&state={q['state']}"))["result"] == "error"


def test_oauth_state_is_bound_to_the_user_and_cannot_be_forged(app, owner, platform, vendor):
    platform.env = dict(OAUTH_ENV)
    clients = field_and_office_clients(app, owner)
    q = start_oauth(owner, vendor)
    r = result_of(callback(clients["office"], f"code=good-code&state={q['state']}"))
    assert r["result"] == "error" and "different user" in r["message"]
    assert result_of(callback(clients["field"], f"code=good-code&state={q['state']}"))["result"] == "error"
    body, sig = q["state"].split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["e"] += 3600
    fbody = base64.urlsafe_b64encode(json.dumps(forged).encode()).rstrip(b"=").decode()
    assert "expired or was tampered" in result_of(callback(owner, f"code=good-code&state={fbody}.{sig}"))["message"]
    assert result_of(callback(owner, "code=good-code&state=garbage"))["result"] == "error"
    anon = TestClient(app).get(f"/api/integrations/example_oauth/oauth/callback?code=good-code&state={q['state']}",
                               follow_redirects=False)
    assert anon.status_code == 303 and anon.headers["location"] == "/#/login"
    assert result_of(callback(owner, f"code=good-code&state={q['state']}"))["result"] == "connected"   # still valid
    assert not any(f.get("code") == "good-code" for f in vendor.token_forms[:-1])     # nothing exchanged before


def test_oauth_denied_or_rejected_sign_ins_are_reported(owner, platform, vendor):
    platform.env = dict(OAUTH_ENV)
    q = start_oauth(owner, vendor)
    r = result_of(callback(owner, f"error=access_denied&state={q['state']}"))
    assert r["result"] == "error" and "access_denied" in r["message"]
    assert platform.store.get_connection(owner.tenant_id, "example_oauth")["status"] == "error"
    q = start_oauth(owner, vendor)
    r = result_of(callback(owner, f"code=bad-code&state={q['state']}"))
    assert r["result"] == "error" and "invalid_grant" in r["message"]
    assert vendor.token_forms and not any("csecret" in json.dumps(f) for f in vendor.token_forms)


def connect_oauth(owner, platform, vendor):
    platform.env = dict(OAUTH_ENV)
    q = start_oauth(owner, vendor)
    assert result_of(callback(owner, f"code=good-code&state={q['state']}"))["result"] == "connected"
    return ExampleOAuthConnector(registry.ConnectorContext(platform, owner.tenant_id))


def expire(inst):
    sec = inst.secrets()
    sec["oauth"]["expires_at"] = int(time.time()) - 10
    inst.save(secrets=sec)


def test_expired_access_tokens_are_refreshed_and_rotated(owner, platform, vendor):
    inst = connect_oauth(owner, platform, vendor)
    assert inst.access_token() == "at-1"               # still fresh: no token call
    n = len(vendor.token_forms)
    expire(inst)
    assert owner.post("/api/integrations/example_oauth/test").json()["ok"] is True
    form = vendor.token_forms[-1]
    assert len(vendor.token_forms) == n + 1 and form == {"grant_type": "refresh_token", "refresh_token": "rt-1"}
    tok = inst.secrets()["oauth"]
    assert tok["access_token"] == "at-2" and tok["refresh_token"] == "rt-2" and tok["expires_at"] > time.time()
    expire(inst)
    assert inst.access_token() == "at-3" and inst.secrets()["oauth"]["refresh_token"] == "rt-2"   # not rotated: kept


def test_concurrent_refreshes_spend_the_refresh_token_once(owner, platform, vendor):
    inst = connect_oauth(owner, platform, vendor)
    expire(inst)
    n = len(vendor.token_forms)
    got, gate = [], threading.Barrier(4)

    def go():
        gate.wait()
        got.append(ExampleOAuthConnector(registry.ConnectorContext(platform, owner.tenant_id)).access_token())
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == ["at-2"] * 4 and len(vendor.token_forms) == n + 1


def test_revoked_refresh_token_asks_the_customer_to_reconnect(owner, platform, vendor):
    inst = connect_oauth(owner, platform, vendor)
    expire(inst)
    vendor.token_error = "invalid_grant"
    r = owner.post("/api/integrations/example_oauth/test")
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    conn = platform.store.get_connection(owner.tenant_id, "example_oauth")
    assert conn["status"] == "error" and "Reconnect" in conn["last_error"]


def test_api_keys_cannot_start_a_browser_sign_in(owner, platform):
    platform.env = dict(OAUTH_ENV)
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    r = TestClient(owner.app).post("/api/integrations/example_oauth/oauth/start",
                                   headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------- webhooks
def make_hook(owner, **body) -> dict:
    body.setdefault("name", "Ops")
    body.setdefault("events", ["*"])
    r = owner.post("/api/integrations/webhooks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("body,match", [
    ({"url": "https://example.com/services/T/B/X", "kind": "slack"}, "hooks.slack.com"),
    ({"url": "http://hooks.slack.com/services/T/B/X", "kind": "slack"}, "https"),
    ({"url": "https://internal.example.com/hook", "kind": "generic"}, "private"),
    ({"url": "https://169.254.169.254/latest/meta-data", "kind": "generic"}, "public"),
    ({"url": "https://hooks.example.com:9000/x", "kind": "generic"}, "Port"),
    ({"url": SLACK_URL, "kind": "slack", "events": ["not_an_event"]}, "Unknown event"),
])
def test_webhook_urls_and_events_are_validated(owner, platform, body, match):
    mock_http(platform, lambda r: httpx.Response(200), hosts={"internal.example.com": "10.1.1.1"})
    r = owner.post("/api/integrations/webhooks", json={"name": "x", **body})
    assert r.status_code == 422 and match in r.json()["detail"], r.text


def test_webhook_url_is_encrypted_and_only_a_hint_is_shown(owner, platform):
    mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    hook = make_hook(owner, url=SLACK_URL, kind="slack", events=["sub_noncompliant"])
    assert hook["url_hint"] == "https://hooks.slack.com/…" and "signing_secret" not in hook
    listing = owner.get("/api/integrations/webhooks")
    assert "XXXXXXXX" not in listing.text and listing.json()[0]["id"] == hook["id"]
    raw = platform.store.get_webhook(owner.tenant_id, hook["id"], with_secret=True)
    assert "XXXXXXXX" not in json.dumps(raw)
    assert platform.secret_box.decrypt_text(raw["url"], webhook_context(owner.tenant_id)) == SLACK_URL


def test_slack_test_send(owner, platform):
    seen = mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    hook = make_hook(owner, url=SLACK_URL, kind="slack")
    r = owner.post(f"/api/integrations/webhooks/{hook['id']}/test").json()
    assert r == {"ok": True, "status": 200, "message": "delivered", "disabled": False}
    sent = seen[-1]
    assert str(sent.url) == SLACK_URL and sent.headers["content-type"] == "application/json"
    text = json.loads(sent.content)["text"]
    assert text.startswith("*Test message from TradeComply*") and "<http://testserver/#/settings|Open in" in text
    assert owner.get(f"/api/integrations/webhooks/{hook['id']}").json()["last_status"] == "200 delivered"


def test_teams_test_send_is_an_adaptive_card(owner, platform):
    seen = mock_http(platform, lambda r: httpx.Response(202))
    hook = make_hook(owner, url=TEAMS_URL, kind="teams")
    assert hook["url_hint"] == "https://prod-00.westus.logic.azure.com/…" and "SIGSECRET" not in json.dumps(hook)
    assert owner.post(f"/api/integrations/webhooks/{hook['id']}/test").json()["ok"] is True
    body = json.loads(seen[-1].content)
    att = body["attachments"][0]
    assert body["type"] == "message" and att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["contentUrl"] is None and att["content"]["type"] == "AdaptiveCard" and att["content"]["version"] == "1.2"
    assert att["content"]["body"][0]["text"] == "Test message from TradeComply"
    assert att["content"]["actions"][0]["type"] == "Action.OpenUrl"


def test_generic_webhook_is_signed_per_standard_webhooks(owner, platform):
    seen = mock_http(platform, lambda r: httpx.Response(204))
    hook = make_hook(owner, url="https://receiver.example.com/tc", kind="generic", events=["payroll_issue"])
    secret = hook["signing_secret"]
    assert secret.startswith("whsec_") and len(base64.b64decode(secret[6:])) == 32
    assert "signing_secret" not in owner.get(f"/api/integrations/webhooks/{hook['id']}").json()
    assert owner.post(f"/api/integrations/webhooks/{hook['id']}/test").json()["ok"] is True
    req = seen[-1]
    assert webhooks.verify(secret, req.headers, req.content)
    assert not webhooks.verify(webhooks.new_signing_secret(), req.headers, req.content)
    body = json.loads(req.content)
    assert body["type"] == "test" and body["data"]["title"] == "Test message from TradeComply" and body["timestamp"]
    rotated = owner.post(f"/api/integrations/webhooks/{hook['id']}/rotate-secret").json()["signing_secret"]
    assert rotated != secret
    owner.post(f"/api/integrations/webhooks/{hook['id']}/test")
    assert webhooks.verify(rotated, seen[-1].headers, seen[-1].content)
    assert not webhooks.verify(secret, seen[-1].headers, seen[-1].content)


def test_a_410_turns_the_webhook_off(owner, platform):
    mock_http(platform, lambda r: httpx.Response(410))
    hook = make_hook(owner, url="https://receiver.example.com/tc", kind="generic")
    r = owner.post(f"/api/integrations/webhooks/{hook['id']}/test").json()
    assert r["ok"] is False and r["disabled"] is True
    assert owner.get(f"/api/integrations/webhooks/{hook['id']}").json()["enabled"] is False


def test_webhook_patch_rotate_delete_and_isolation(app, owner, platform):
    mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    hook = make_hook(owner, url=SLACK_URL, kind="slack")
    r = owner.patch(f"/api/integrations/webhooks/{hook['id']}", json={"name": "Renamed", "events": ["payroll_issue"]})
    assert r.json()["name"] == "Renamed" and r.json()["events"] == ["payroll_issue"]
    assert owner.patch(f"/api/integrations/webhooks/{hook['id']}", json={"events": ["bogus"]}).status_code == 422
    assert owner.post(f"/api/integrations/webhooks/{hook['id']}/rotate-secret").status_code == 409   # slack: unsigned
    r = owner.patch(f"/api/integrations/webhooks/{hook['id']}", json={"kind": "generic"}).json()
    assert r["kind"] == "generic" and r["signing_secret"].startswith("whsec_") and r["signed"] is True
    r = owner.patch(f"/api/integrations/webhooks/{hook['id']}", json={"kind": "slack"}).json()
    assert r["signed"] is False
    assert owner.patch(f"/api/integrations/webhooks/{hook['id']}",
                       json={"url": "https://receiver.example.com/x"}).status_code == 422     # not a Slack URL
    with TestClient(app) as other:
        signup(other, email="b@two.test", company="Two")
        hid = hook["id"]
        assert other.get(f"/api/integrations/webhooks/{hid}").status_code == 404
        assert other.patch(f"/api/integrations/webhooks/{hid}", json={"name": "x"}).status_code == 404
        assert other.post(f"/api/integrations/webhooks/{hid}/test").status_code == 404
        assert other.delete(f"/api/integrations/webhooks/{hid}").status_code == 404
        assert other.get("/api/integrations/webhooks").json() == []
    assert owner.delete(f"/api/integrations/webhooks/{hook['id']}").json() == {"ok": True}
    assert owner.get(f"/api/integrations/webhooks/{hook['id']}").status_code == 404


def test_test_sends_are_rate_limited(owner, platform):
    from app.auth import RateLimiter
    platform.integration_test_limiter = RateLimiter(limit=2, window_s=600)
    mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    hook = make_hook(owner, url=SLACK_URL, kind="slack")
    codes = [owner.post(f"/api/integrations/webhooks/{hook['id']}/test").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_webhooks_per_company_are_capped(app, owner, platform, monkeypatch):
    """Each event goes to every webhook, so an uncapped list would let one company make this server send thousands
    of copies of every event to one URL."""
    from app import integrations_api
    monkeypatch.setattr(integrations_api, "MAX_WEBHOOKS", 3)
    mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    for _ in range(3):
        make_hook(owner, url=SLACK_URL, kind="slack")
    r = owner.post("/api/integrations/webhooks", json={"name": "x", "url": SLACK_URL, "kind": "slack"})
    assert r.status_code == 409 and "at most 3" in r.json()["detail"]
    assert len(owner.get("/api/integrations/webhooks").json()) == 3
    with TestClient(app) as other:                     # the cap is per company
        signup(other, email="b@other.test", company="Other")
        assert other.post("/api/integrations/webhooks", json={"name": "x", "url": SLACK_URL,
                                                               "kind": "slack"}).status_code == 201


# ---------------------------------------------------------------------------- events through the API
def test_emitted_events_reach_webhooks_and_connectors(owner, platform, vendor):
    slack_seen = []

    def handler(r):
        if r.url.host == "hooks.slack.com":
            slack_seen.append(json.loads(r.content))
            return httpx.Response(200, text="ok")
        return vendor(r)
    mock_http(platform, handler)
    make_hook(owner, url=SLACK_URL, kind="slack", events=["sub_noncompliant"])
    owner.post("/api/integrations/example_apikey/connect", json={"config": {"api_key": "sk-test-123"}})
    ids = platform.events.emit(owner.tenant_id, "sub_noncompliant",
                               {"title": "Sub <Acme & Co> has no workers' comp on file", "severity": "warning",
                                "summary": "License 123456", "link": "/#/subs/4"})
    assert len(ids) == 2 and platform.events.drain() == 2
    assert slack_seen[0]["text"].startswith("*Sub &lt;Acme &amp; Co&gt; has no workers' comp on file*")
    assert vendor.alerts == [{"title": "Sub <Acme & Co> has no workers' comp on file", "body": "License 123456"}]
    log = owner.get("/api/integrations/outbound").json()
    assert {(r["target"].split(":")[0], r["status"]) for r in log} == {("webhook", "delivered"),
                                                                       ("connector", "delivered")}
    assert owner.get("/api/integrations/outbound?status=bogus").status_code == 422


def test_access_log_redacts_the_oauth_code_and_state():
    import logging
    from app.main import RedactTokens
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/api/integrations/x/oauth/callback?code=AUTHCODE123&state=STATE456",
                             "1.1", 303), None)
    RedactTokens().filter(rec)
    assert "AUTHCODE123" not in rec.getMessage() and "STATE456" not in rec.getMessage()


def test_reconnecting_after_a_key_change_replaces_unreadable_credentials(owner, platform, vendor):
    inst = connect_oauth(owner, platform, vendor)
    platform.store.update_connection(owner.tenant_id, "example_oauth", status="connected",
                                     secret_blob=platform.secret_box.encrypt("x", "some-other-context"))
    detail = owner.get("/api/integrations/example_oauth").json()
    assert detail["secrets_readable"] is False and detail["signed_in"] is False
    assert owner.post("/api/integrations/example_oauth/test").status_code == 409
    q = start_oauth(owner, vendor)
    assert result_of(callback(owner, f"code=good-code&state={q['state']}"))["result"] == "connected"
    assert inst.secrets()["oauth"]["access_token"] == "at-2"


def test_token_revocation_never_follows_a_redirect(owner, platform):
    """Security review: the RFC 7009 revocation form carries the token (and with client_auth="body" the client
    secret); a 307/308 from the revocation endpoint must not re-send it anywhere else."""
    from dataclasses import replace
    platform.env = dict(OAUTH_ENV)
    cls = type("RevokingOAuth", (ExampleOAuthConnector,), {
        "oauth": replace(ExampleOAuthConnector.oauth, revoke_url="https://login.example.com/oauth/revoke",
                         client_auth="body")})
    seen = []

    def vendor(request):
        seen.append(request)
        if request.url.host == "login.example.com":
            return httpx.Response(307, headers={"location": "https://collector.example.net/steal"})
        return httpx.Response(200)
    mock_http(platform, vendor)
    inst = cls(registry.ConnectorContext(platform, owner.tenant_id))
    inst.save(status="connected", secrets={"oauth": {"access_token": "at-secret", "refresh_token": "rt-secret"}})
    inst.revoke()
    assert [r.url.host for r in seen] == ["login.example.com"]
    assert "rt-secret" in seen[0].content.decode() and "csecret-456" in seen[0].content.decode()


def test_ids_of_one_company_are_not_found_by_another(app, owner, platform):
    """Security review sweep: every integration route that takes an id (webhook, calendar link, SMS recipient,
    project for a schedule upload, Docusign envelope) answers another company 404 and changes nothing."""
    mock_http(platform, lambda request: httpx.Response(200, text="ok"))
    hook = make_hook(owner, kind="slack", url=SLACK_URL)
    link = owner.post("/api/integrations/calendar_feed/links").json()
    rec = owner.post("/api/integrations/twilio_sms/recipients", json={"phone": "(415) 555-0100", "name": "Sam",
                                                                       "consent": True, "send_confirmation": False})
    assert rec.status_code == 201, rec.text
    rid = rec.json()["recipient"]["id"]
    pid = owner.post("/api/projects", json={"name": "A job"}).json()["id"]
    with TestClient(app) as other:
        signup(other, email="b@two.test", company="Two")
        for method, path in (("get", f"/api/integrations/webhooks/{hook['id']}"),
                             ("patch", f"/api/integrations/webhooks/{hook['id']}"),
                             ("delete", f"/api/integrations/webhooks/{hook['id']}"),
                             ("post", f"/api/integrations/webhooks/{hook['id']}/test"),
                             ("post", f"/api/integrations/webhooks/{hook['id']}/rotate-secret"),
                             ("delete", f"/api/integrations/calendar_feed/links/{link['id']}"),
                             ("delete", f"/api/integrations/twilio_sms/recipients/{rid}"),
                             ("get", "/api/integrations/docusign/envelopes/0aa1b2c3-d4e5-f6a7-b8c9-d0e1f2a3b4c5"
                                     "?refresh=false"),
                             ("get", f"/api/integrations/docusign/projects/{pid}/envelopes")):
            kw = {"json": {"enabled": False}} if method == "patch" else {}
            r = getattr(other, method)(path, **kw)
            assert r.status_code == 404, (method, path, r.status_code, r.text)
        r = other.post(f"/api/integrations/schedule_import/projects/{pid}/upload",
                       files={"file": ("s.xml", b"<Project xmlns='http://schemas.microsoft.com/project'/>")})
        assert r.status_code == 404
        assert other.get("/api/integrations/webhooks").json() == []
        assert other.get("/api/integrations/calendar_feed/links").json()["links"] == []
        assert other.get("/api/integrations/twilio_sms/recipients").json()["recipients"] == []
    assert owner.get(f"/api/integrations/webhooks/{hook['id']}").json()["enabled"] is True
    assert owner.get("/api/integrations/calendar_feed/links").json()["active"] == 1
    assert [r["status"] for r in owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"]] == ["active"]
