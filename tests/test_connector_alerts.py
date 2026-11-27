"""Alert channels (app/connectors/alerts.py: Slack Block Kit, Teams Adaptive Card, signed JSON webhook), the daily
deadline scan (deadline_due_soon at 14/7/3/1 days, no duplicates) and Twilio SMS alerts (app/connectors/
twilio_sms.py: operator env, consent records, STOP/START from Twilio's signed inbound webhook, message length).

Every vendor call goes to httpx.MockTransport (tests.support.mock_http) answering in the vendors' documented shapes:
Slack incoming webhooks answer 200 "ok" or a 4xx/410 error code in plain text; a Teams Workflows trigger answers
202; Twilio answers CreateMessage with 201 and the Message resource from its OpenAPI example, errors as
{code, message, more_info, status}. All credentials are made up. Each test runs on SQLite and, with
TC_TEST_DATABASE_URL set, on PostgreSQL."""
import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import connectors as registry
from app.connectors import ConnectorContext, validate_connector_class
from app.connectors import alerts, twilio_sms
from app.connectors.alerts import SignedWebhookAlerts, SlackAlerts, TeamsAlerts
from app.connectors.twilio_sms import TwilioSmsConnector
from app.events import EVENTS
from tests.support import backend, browser, json_response, join, make_app, mock_http, signup  # noqa: F401
from tradecomply import integrations_guide

# Built at runtime so secret scanners do not mistake this fake URL for a real Slack webhook.
SLACK_URL = "https://hooks.slack.com/services/" + "T00000000/B00000000/" + "X" * 24
TEAMS_URL = "https://prod-00.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SIGSECRET"
HOOK_URL = "https://hooks.receiver.example/catch/123/abcsecretpath/"
HOOK_SECRET = "test-signing-secret-0123456789abcdef-XYZ"
APP_URL = "https://tc.example"
ACCOUNT = "AC" + "a" * 32
SERVICE = "MG" + "b" * 32
API_KEY = "SK" + "c" * 32
AUTH_TOKEN = "test-auth-token-not-real"
API_SECRET = "test-api-secret-not-real"
TWILIO_ENV = {"TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": AUTH_TOKEN, "TWILIO_MESSAGING_SERVICE_SID": SERVICE}
PHONE = "+14155550100"
PHONE2 = "+14155550111"


def mount_routes(app):
    """What app/main.py needs: include both routers before the catch-all MCP mount at '/'."""
    app.include_router(alerts.router)
    app.include_router(twilio_sms.router)
    routes = app.router.routes
    catch_all = [r for r in routes if isinstance(r, Mount) and r.path == ""]
    for r in catch_all:
        routes.remove(r)
    routes.extend(catch_all)


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=APP_URL)
    mount_routes(a)
    a.state.platform.env = {}
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


@pytest.fixture
def other(owner):
    with browser(owner) as c:
        c.tenant_id = signup(c, email="boss@other.test", company="Other Co")["tenant_id"]
        yield c


class Receivers:
    """Slack, Teams, a generic receiver and Twilio, in their documented response shapes."""

    def __init__(self):
        self.slack = []           # queued (status, text) answers; default 200 "ok"
        self.teams = []
        self.hook = []
        self.twilio = {}          # phone -> list of queued (status, json) answers; default 201 Message
        self.service = {"us_app_to_person_registered": True}
        self.service_status = 200
        self.numbers = [{"phone_number": "+14155559999", "capabilities": {"voice": True, "sms": True,
                                                                         "mms": True, "fax": False}}]
        self.seen = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        host, path = request.url.host, request.url.path
        if host == "hooks.slack.com" and path.startswith("/services/"):
            status, text = self.slack.pop(0) if self.slack else (200, "ok")
            return httpx.Response(status, text=text, headers={"content-type": "text/html"})
        if host.endswith(".logic.azure.com"):
            status = self.teams.pop(0) if self.teams else 202
            return httpx.Response(status)
        if host == "hooks.receiver.example":
            status, headers = self.hook.pop(0) if self.hook else (200, {})
            return httpx.Response(status, json={"ok": status < 300}, headers=headers)
        if host == "api.twilio.com" and path == f"/2010-04-01/Accounts/{ACCOUNT}/Messages.json":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            queued = self.twilio.get(form["To"]) or []
            if queued:
                status, body = queued.pop(0)
                return json_response(status, body)
            return json_response(201, {   # the CreateMessage example in Twilio's OpenAPI spec (twilio_api_v2010)
                "account_sid": ACCOUNT, "api_version": "2010-04-01", "body": form.get("Body"),
                "date_created": "Thu, 24 Aug 2023 05:01:45 +0000", "date_sent": "Thu, 24 Aug 2023 05:01:45 +0000",
                "date_updated": "Thu, 24 Aug 2023 05:01:45 +0000", "direction": "outbound-api", "error_code": None,
                "error_message": None, "from": "+14155552345", "num_media": "0", "num_segments": "1",
                "price": None, "price_unit": None, "messaging_service_sid": SERVICE,
                "sid": "SM" + hashlib.md5(request.content).hexdigest(), "status": "queued",
                "subresource_uris": {"media": "/2010-04-01/Accounts/x/Messages/y/Media.json"}, "to": form["To"],
                "uri": "/2010-04-01/Accounts/x/Messages/y.json"})
        if host == "messaging.twilio.com" and path == f"/v1/Services/{SERVICE}":
            if self.service_status != 200:
                return json_response(self.service_status, {"code": 20003, "message": "Authenticate",
                                                           "more_info": "https://www.twilio.com/docs/errors/20003",
                                                           "status": self.service_status})
            return json_response(200, {"account_sid": ACCOUNT, "sid": SERVICE, "friendly_name": "Alerts",
                                       "inbound_request_url": f"{APP_URL}/api/integrations/twilio-sms/inbound",
                                       "inbound_method": "POST", "usecase": "notifications",
                                       **self.service})
        if host == "api.twilio.com" and path.endswith("/IncomingPhoneNumbers.json"):
            return json_response(200, {"end": 0, "first_page_uri": "/x", "incoming_phone_numbers": self.numbers,
                                       "next_page_uri": None, "page": 0, "page_size": 50, "previous_page_uri": None,
                                       "start": 0, "uri": "/x"})
        return json_response(404, {"error": "not mocked"})

    def to(self, host):
        return [r for r in self.seen if r.url.host == host]


@pytest.fixture
def rx(platform):
    r = Receivers()
    mock_http(platform, r)
    return r


def ctx(platform, tenant_id):
    return ConnectorContext(platform, tenant_id, None, "test")


def connect(platform, cls, tenant_id, **secrets_and_config):
    """Store a connected channel directly (the API path is tested separately)."""
    inst = cls(ctx(platform, tenant_id))
    secret_names = {f.name for f in cls.config_fields if f.secret}
    inst.save(status="connected", secrets={k: v for k, v in secrets_and_config.items() if k in secret_names},
              config={k: v for k, v in secrets_and_config.items() if k not in secret_names})
    return inst


def outbound(platform, tenant_id):
    return platform.store.list_outbound(tenant_id)


PAYLOAD = {"title": "Sub's COI expires in 7 days", "summary": "ABC Electric: policy <GL-1> & umbrella",
           "severity": "warning", "link": "/#/subs", "due_date": "2026-12-03", "project_name": "Main St Remodel",
           "sub_name": "ABC Electric"}


# ---------------------------------------------------------------------------- registry
def test_connectors_register_and_map_to_catalog_cards():
    registry.discover(force=True)
    assert registry.import_errors == {}
    ids = {e["id"] for e in integrations_guide.entries()}
    for cls in (SlackAlerts, TeamsAlerts, SignedWebhookAlerts, TwilioSmsConnector):
        assert validate_connector_class(cls) == []
        assert registry.get(cls.provider) is cls
        assert cls.catalog_id in ids
        assert set(cls.subscribes) == set(EVENTS)
    assert registry.get("") is None and not any(c.__name__ == "_AlertChannel" for c in registry.all_connectors())


# ---------------------------------------------------------------------------- message formats
def test_slack_blocks_follow_block_kit_limits_and_escape_mrkdwn():
    msg = alerts.slack_message("sub_noncompliant", PAYLOAD, APP_URL, company="Acme & Sons")
    assert msg["text"] == "Sub's COI expires in 7 days (due Dec 3, 2026)"
    header, summary, fields, link, context = msg["blocks"]
    assert header == {"type": "header", "text": {"type": "plain_text", "text": PAYLOAD["title"]}}
    assert summary["text"] == {"type": "mrkdwn", "text": "ABC Electric: policy &lt;GL-1&gt; &amp; umbrella"}
    assert [f["text"] for f in fields["fields"]] == ["*Due*\nDec 3, 2026", "*Project*\nMain St Remodel",
                                                     "*Subcontractor*\nABC Electric", "*Severity*\nWarning"]
    assert link["text"]["text"] == "<https://tc.example/#/subs|Open in TradeComply>"
    assert context == {"type": "context", "elements": [{"type": "mrkdwn", "text":
                                                        "Subcontractor compliance · Acme &amp; Sons · TradeComply"}]}
    # limits: header 150, section 3000, 10 fields of 2000; no link without a known app URL
    long = alerts.slack_message("payroll_issue", {"title": "T" * 300, "summary": "&" * 2000}, None)
    assert len(long["blocks"][0]["text"]["text"]) == 150
    assert len(long["blocks"][1]["text"]["text"]) <= 3000 and not long["blocks"][1]["text"]["text"].endswith("&am…")
    assert all(b["type"] != "section" or "<http" not in json.dumps(b) for b in long["blocks"])
    assert len(json.dumps(long)) < 40_000


def test_teams_card_is_the_documented_workflows_envelope():
    msg = alerts.teams_message("deadline_due_soon", {**PAYLOAD, "severity": "critical"}, APP_URL, "Acme")
    assert msg["type"] == "message" and len(msg["attachments"]) == 1
    att = msg["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive" and att["contentUrl"] is None
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.2"
    assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    title = card["body"][0]
    assert title["type"] == "TextBlock" and title["weight"] == "bolder" and title["color"] == "attention"
    facts = next(b for b in card["body"] if b["type"] == "FactSet")["facts"]
    assert {"title": "Due", "value": "Dec 3, 2026"} in facts
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open in TradeComply",
                                "url": "https://tc.example/#/subs"}]
    big = alerts.teams_message("payroll_issue", {"title": "x", "summary": "é" * 2000}, None)
    assert len(json.dumps(big, ensure_ascii=False).encode()) < alerts.TEAMS_MAX_BYTES
    assert "actions" not in big["attachments"][0]["content"]


def test_signature_matches_an_independent_hmac_and_rejects_replays_and_tampering():
    body = alerts.signed_body("deadline_due_soon", PAYLOAD, delivery="tc_abc", created="2026-11-26T00:00:00Z")
    ts = 1_790_000_000
    headers = alerts.signed_headers(HOOK_SECRET, "deadline_due_soon", "tc_abc", body, timestamp=ts)
    expected = base64.b64encode(hmac.new(HOOK_SECRET.encode(), f"{ts}.".encode() + body,
                                         hashlib.sha256).digest()).decode()
    assert headers["X-TradeComply-Signature"] == "v1=" + expected
    assert headers["X-TradeComply-Timestamp"] == str(ts) and headers["X-TradeComply-Delivery"] == "tc_abc"
    assert alerts.verify_signature(HOOK_SECRET, headers, body, now=ts + 10)
    assert not alerts.verify_signature(HOOK_SECRET, headers, body, now=ts + 301)          # replayed later
    assert not alerts.verify_signature(HOOK_SECRET, headers, body + b" ", now=ts)        # body changed
    assert not alerts.verify_signature("another-secret-0123456789abcdef0123", headers, body, now=ts)
    lower = {k.lower(): v for k, v in headers.items()}
    assert alerts.verify_signature(HOOK_SECRET, lower, body, now=ts)                      # header case
    doc = json.loads(body)
    assert doc == {"id": "tc_abc", "type": "deadline_due_soon", "created": "2026-11-26T00:00:00Z", "company": None,
                   "url": None, "data": PAYLOAD}


# ---------------------------------------------------------------------------- channels through the API and bus
def test_slack_connect_posts_blocks_and_never_returns_the_url(owner, platform, rx):
    r = owner.post("/api/integrations/slack_alerts/connect", json={"config": {"webhook_url": SLACK_URL}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected"
    sent = json.loads(rx.to("hooks.slack.com")[0].content)
    assert sent["blocks"][0]["text"]["text"] == "Test message from TradeComply" and sent["text"]
    detail = owner.get("/api/integrations/slack_alerts").json()
    assert detail["secrets_saved"] == ["webhook_url"] and "XXXXXXXX" not in json.dumps(detail)
    conn = platform.store.get_connection(owner.tenant_id, "slack_alerts", with_secret=True)
    assert "hooks.slack.com" not in conn["secret_blob"] and "hooks.slack.com" not in json.dumps(conn["config"])
    bad = owner.put("/api/integrations/slack_alerts/config",
                    json={"config": {"webhook_url": "https://example.com/services/x"}})
    assert bad.status_code == 422 and "hooks.slack.com" in bad.text
    bad = owner.put("/api/integrations/slack_alerts/config", json={"config": {"events": "deadline_due_soon, nope"}})
    assert bad.status_code == 422 and "nope" in bad.text


def test_event_bus_delivers_to_each_channel_in_its_format(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL)
    connect(platform, TeamsAlerts, tid, webhook_url=TEAMS_URL)
    connect(platform, SignedWebhookAlerts, tid, webhook_url=HOOK_URL, signing_secret=HOOK_SECRET)
    ids = platform.events.emit(tid, "sub_noncompliant", PAYLOAD)
    assert len(ids) == 3
    assert platform.events.drain() == 3
    assert {r["status"] for r in outbound(platform, tid)} == {"delivered"}
    slack = json.loads(rx.to("hooks.slack.com")[0].content)
    assert slack["blocks"][0]["type"] == "header" and "Acme Builders" in json.dumps(slack["blocks"][-1])
    teams = json.loads(rx.to("prod-00.westus.logic.azure.com")[0].content)
    assert teams["attachments"][0]["content"]["actions"][0]["url"] == "https://tc.example/#/subs"
    hook = rx.to("hooks.receiver.example")[0]
    assert hook.headers["content-type"] == "application/json"
    assert hook.headers["x-tradecomply-event"] == "sub_noncompliant"
    assert alerts.verify_signature(HOOK_SECRET, hook.headers, hook.content)
    doc = json.loads(hook.content)
    assert doc["data"] == {**PAYLOAD} and doc["company"] == "Acme Builders" and doc["id"] == \
        hook.headers["x-tradecomply-delivery"]


def test_severity_and_event_filters(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL, min_severity="critical")
    connect(platform, TeamsAlerts, tid, webhook_url=TEAMS_URL, events="injury_report_due")
    platform.events.emit(tid, "sub_noncompliant", PAYLOAD)              # warning: below Slack's floor, not Teams'
    platform.events.drain()
    assert rx.seen == []
    platform.events.emit(tid, "injury_report_due", {"title": "Report to Cal/OSHA", "severity": "critical"})
    platform.events.drain()
    assert len(rx.to("hooks.slack.com")) == 1 and len(rx.to("prod-00.westus.logic.azure.com")) == 1


def test_slack_errors_retry_fail_or_turn_the_channel_off(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL)
    rx.slack = [(429, "rate_limited"), (200, "ok")]
    platform.events.emit(tid, "payroll_issue", {"title": "Overtime missing"})
    platform.events.drain()
    row = outbound(platform, tid)[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    platform.events.drain(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    assert outbound(platform, tid)[0]["status"] == "delivered"

    rx.slack = [(404, "channel_not_found")]
    platform.events.emit(tid, "payroll_issue", {"title": "Second"})
    platform.events.drain()
    assert outbound(platform, tid)[0]["status"] == "failed"
    assert platform.store.get_connection(tid, "slack_alerts")["status"] == "connected"

    rx.slack = [(410, "channel_is_archived")]
    platform.events.emit(tid, "payroll_issue", {"title": "Third"})
    platform.events.drain()
    conn = platform.store.get_connection(tid, "slack_alerts")
    assert outbound(platform, tid)[0]["status"] == "failed"
    assert conn["status"] == "error" and "turned this webhook off" in conn["last_error"]
    platform.events.emit(tid, "payroll_issue", {"title": "Fourth"})       # no longer a target
    assert [r["title"] for r in owner.get("/api/integrations/outbound").json()][0] == "Third"


def test_signed_webhook_keeps_its_delivery_id_across_retries_and_stops_on_410(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SignedWebhookAlerts, tid, webhook_url=HOOK_URL, signing_secret=HOOK_SECRET)
    rx.hook = [(503, {"Retry-After": "1"}), (200, {}), (302, {"location": "https://elsewhere.example/"}), (410, {})]
    platform.events.emit(tid, "contract_generated", {"title": "Contract for 12 Oak St"})
    platform.events.drain()
    platform.events.drain(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    first, second = rx.to("hooks.receiver.example")
    assert first.headers["x-tradecomply-delivery"] == second.headers["x-tradecomply-delivery"]
    assert outbound(platform, tid)[0]["status"] == "delivered"
    platform.events.emit(tid, "contract_generated", {"title": "Redirected"})
    platform.events.drain()
    assert outbound(platform, tid)[0]["status"] == "failed"                      # redirects aren't followed
    assert not rx.to("elsewhere.example")
    platform.events.emit(tid, "contract_generated", {"title": "Gone"})
    platform.events.drain()
    assert platform.store.get_connection(tid, "signed_webhook")["status"] == "error"


def test_private_addresses_are_refused(owner, platform):
    r = Receivers()
    mock_http(platform, r, hosts={"hooks.receiver.example": "10.0.0.5"})
    connect(platform, SignedWebhookAlerts, owner.tenant_id, webhook_url=HOOK_URL, signing_secret=HOOK_SECRET)
    result = SignedWebhookAlerts(ctx(platform, owner.tenant_id)).test()
    assert not result.ok and "private" in result.message
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "x"})
    platform.events.drain()
    assert outbound(platform, owner.tenant_id)[0]["status"] == "failed" and r.seen == []


def test_channels_are_tenant_isolated_and_secrets_stay_out_of_logs(owner, other, platform, rx, caplog):
    caplog.set_level(logging.DEBUG)
    connect(platform, SignedWebhookAlerts, owner.tenant_id, webhook_url=HOOK_URL, signing_secret=HOOK_SECRET)
    platform.events.emit(other.tenant_id, "payroll_issue", {"title": "Other company's alert"})
    platform.events.drain()
    assert rx.seen == []
    assert other.get("/api/integrations/signed_webhook").json()["connection"]["status"] == "disconnected"
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "Ours"})
    platform.events.drain()
    assert len(rx.seen) == 1
    logs = caplog.text
    assert HOOK_SECRET not in logs and "abcsecretpath" not in logs


# ---------------------------------------------------------------------------- deadline scan
@pytest.mark.parametrize("days,bucket", [(15, None), (14, 14), (13, 14), (8, 14), (7, 7), (4, 7), (3, 3), (2, 3),
                                         (1, 1), (0, 1), (-1, None)])
def test_reminder_buckets(days, bucket):
    assert alerts.reminder_bucket(days) == bucket


def _project(c, **fields):
    r = c.post("/api/projects", json={"name": "Main St Remodel", "claimant_role": "subcontractor", **fields})
    assert r.status_code == 201, r.text
    return r.json()


def test_scan_sends_each_reminder_once_at_14_7_3_and_1_days(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL)
    p = _project(owner, first_furnishing_date="2026-12-01")            # 20-day preliminary notice: 2026-12-21
    due = date(2026, 12, 21)
    sent = []
    for offset in range(16, -2, -1):                                   # every day from 16 days out to 1 day after
        result = alerts.scan_tenant(platform, tid, due - timedelta(days=offset))
        assert result.get("skipped") is None
        alerts.scan_tenant(platform, tid, due - timedelta(days=offset))   # a second run the same day adds nothing
        platform.events.drain()
        for req in rx.to("hooks.slack.com")[len(sent):]:
            sent.append((offset, json.loads(req.content)["blocks"][0]["text"]["text"]))
    assert [o for o, _ in sent] == [14, 7, 3, 1]
    assert sent[0][1] == "Serve 20-day preliminary notice: due in 14 days (Main St Remodel)"
    assert sent[-1][1] == "Serve 20-day preliminary notice: due tomorrow (Main St Remodel)"
    rows = platform.store.list_outbound(tid)
    assert {r["dedupe_key"] for r in rows} == {f"deadline:prelim_notice:{p['id']}::2026-12-21:{b}" for b in
                                               (14, 7, 3, 1)}
    assert {r["payload"]["severity"] for r in rows} == {"info", "warning", "critical"}
    assert all(r["payload"]["link"] == f"/#/project/{p['id']}" for r in rows)


def test_scan_catches_up_a_missed_day_without_stale_reminders(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL)
    _project(owner, first_furnishing_date="2026-12-01")
    alerts.scan_tenant(platform, tid, date(2026, 12, 19))               # first run 2 days out: only the 3-day one
    platform.events.drain()
    assert len(rx.seen) == 1 and json.loads(rx.seen[0].content)["blocks"][0]["text"]["text"].endswith(
        "due in 2 days (Main St Remodel)")


def test_scan_covers_sub_license_and_coi_expirations(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SignedWebhookAlerts, tid, webhook_url=HOOK_URL, signing_secret=HOOK_SECRET)
    r = owner.post("/api/subs", json={"name": "Solo Electric", "license_number": "555000",   # expires 2026-12-31
                                      "coi_expiry": "2027-01-03", "w9_on_file": True})
    assert r.status_code == 201, r.text
    result = alerts.scan_tenant(platform, tid, date(2026, 12, 24))
    assert result["emitted"] == 2
    platform.events.drain()
    kinds = {json.loads(q.content)["data"]["kind"]: json.loads(q.content)["data"] for q in rx.seen}
    assert kinds["license_expiry"]["title"] == "Sub's CSLB license expires: due in 7 days (Solo Electric)"
    assert kinds["license_expiry"]["reminder_days"] == 7 and kinds["license_expiry"]["link"] == "/#/subs"
    assert kinds["coi_expiry"]["days_left"] == 10 and kinds["coi_expiry"]["reminder_days"] == 14


def test_scan_uses_compliance_calendar_only_with_a_saved_profile(owner, platform, rx):
    tid = owner.tenant_id
    connect(platform, SlackAlerts, tid, webhook_url=SLACK_URL)
    reminders, notes = alerts.due_reminders(platform.store, platform.store.get_tenant(tid), date(2027, 1, 20))
    assert reminders == [] and any("compliance_calendar skipped" in n for n in notes)
    r = owner.patch("/api/tenant", json={"settings": {"compliance_profile": {
        "entity_type": "sole_owner", "has_employees": False, "pays_independent_contractors": True,
        "favorite_color": "blue"}}})
    assert r.status_code == 200, r.text
    reminders, notes = alerts.due_reminders(platform.store, platform.store.get_tenant(tid), date(2027, 1, 20))
    titles = [x["payload"]["title"] for x in reminders]
    assert titles == ["Forms 1099-NEC for 2026 payments to subs: due in 12 days"]
    assert reminders[0]["payload"]["kind"] == "compliance_calendar" and "favorite_color" in notes[0]
    owner.patch("/api/tenant", json={"settings": {"compliance_profile": {"entity_type": "spaceship"}}})
    reminders, notes = alerts.due_reminders(platform.store, platform.store.get_tenant(tid), date(2027, 1, 20))
    assert reminders == [] and "refused" in notes[0]


def test_scan_skips_tenants_without_subscribers_or_subscription(owner, other, platform, rx):
    _project(owner, first_furnishing_date="2026-12-01")
    assert alerts.scan_tenant(platform, owner.tenant_id, date(2026, 12, 7))["skipped"].startswith("nothing")
    connect(platform, SlackAlerts, owner.tenant_id, webhook_url=SLACK_URL)
    platform.store.update_tenant(owner.tenant_id, status="canceled")
    assert alerts.scan_tenant(platform, owner.tenant_id, date(2026, 12, 7))["skipped"].startswith("no active")
    totals = alerts.scan_all(platform, date(2026, 12, 7))
    assert totals["tenants"] == 2 and totals["scanned"] == 0 and totals["emitted"] == 0


def test_scan_all_survives_one_broken_tenant(owner, other, platform, rx, monkeypatch):
    for c in (owner, other):
        connect(platform, SlackAlerts, c.tenant_id, webhook_url=SLACK_URL)
        _project(c, first_furnishing_date="2026-12-01")
    real = alerts.due_reminders

    def flaky(store, tenant, today=None):
        if tenant["id"] == owner.tenant_id:
            raise RuntimeError("bad data")
        return real(store, tenant, today)
    monkeypatch.setattr(alerts, "due_reminders", flaky)
    totals = alerts.scan_all(platform, date(2026, 12, 7))
    assert totals == {"tenants": 2, "scanned": 1, "emitted": 1, "failed": 1}


def test_scheduler_runs_once_a_day_across_instances(owner, platform, rx, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "scan_all", lambda p, day: calls.append(day) or {"emitted": 0})
    a, b = alerts.DeadlineScheduler(platform), alerts.DeadlineScheduler(platform)
    early = datetime(2026, 12, 7, 13, 0, tzinfo=timezone.utc)          # 5 a.m. in California
    later = datetime(2026, 12, 7, 15, 0, tzinfo=timezone.utc)          # 7 a.m.
    assert a.run_due(early) is None and calls == []
    assert a.run_due(later) == {"emitted": 0} and calls == [date(2026, 12, 7)]
    assert a.run_due(later) is None and b.run_due(later) is None and len(calls) == 1
    assert b.run_due(later + timedelta(days=1)) is not None and calls[-1] == date(2026, 12, 8)
    platform.env = {"DEADLINE_SCAN": "0"}
    assert alerts.start_scheduler(platform) is None
    platform.env = {}
    sched = alerts.start_scheduler(platform)
    try:
        assert sched is platform.deadline_scheduler and sched._thread.is_alive()
    finally:
        alerts.stop_scheduler(platform)
    assert sched._thread is None


def test_scan_routes_preview_and_run_for_staff_only(app, owner, platform, rx):
    connect(platform, SlackAlerts, owner.tenant_id, webhook_url=SLACK_URL)
    owner.post("/api/subs", json={"name": "Late COI", "license_number": "123456",
                                  "coi_expiry": (date.today() + timedelta(days=3)).isoformat()})
    pre = owner.get("/api/integrations/deadline-reminders/preview").json()
    assert pre["reminder_days"] == [14, 7, 3, 1] and pre["subscribers"] == ["connector:slack_alerts"]
    assert any(r["kind"] == "coi_expiry" for r in pre["reminders"])
    assert owner.post("/api/integrations/deadline-reminders/scan").json()["emitted"] >= 1
    assert owner.post("/api/integrations/deadline-reminders/scan").json()["emitted"] == 0      # no duplicates
    with browser(owner) as field:
        join(field, owner, "field@acme.test", "field")
        assert field.get("/api/integrations/deadline-reminders/preview").status_code == 403
    no_csrf = owner.headers.pop("x-csrf-token")
    assert owner.post("/api/integrations/deadline-reminders/scan").status_code == 403
    owner.headers["x-csrf-token"] = no_csrf


# ---------------------------------------------------------------------------- Twilio: operator configuration
def test_twilio_without_operator_env_says_not_configured(owner, platform):
    assert TwilioSmsConnector.missing_env({}) == [
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN (or TWILIO_API_KEY and TWILIO_API_SECRET)",
        "TWILIO_MESSAGING_SERVICE_SID (or TWILIO_FROM)"]
    assert TwilioSmsConnector.missing_env({**TWILIO_ENV, "TWILIO_ACCOUNT_SID": "not-a-sid"}) == ["TWILIO_ACCOUNT_SID"]
    assert TwilioSmsConnector.missing_env({"TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_API_KEY": API_KEY,
                                           "TWILIO_API_SECRET": API_SECRET, "TWILIO_FROM": "+14155559999"}) == []
    view = next(p for p in owner.get("/api/integrations").json()["providers"] if p["provider"] == "twilio_sms")
    assert view["operator_configured"] is False and len(view["missing_env"]) == 3
    assert "A2P 10DLC" in view["description"] and "A2P 10DLC" in view["config_schema"][0]["help"]
    r = owner.post("/api/integrations/twilio_sms/connect", json={"config": {}})
    assert r.status_code == 503 and "isn't set up on this server" in r.json()["detail"]
    listing = owner.get("/api/integrations/twilio_sms/recipients").json()
    assert listing["operator_configured"] is False and "A2P 10DLC" in listing["a2p_10dlc_note"]
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "x", "severity": "critical"})
    assert platform.events.drain() == 0                     # not connected: not a target


def test_twilio_connect_checks_the_messaging_service_and_reports_10dlc(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    r = owner.post("/api/integrations/twilio_sms/connect", json={"config": {"min_severity": "warning"}})
    assert r.json()["status"] == "connected", r.text
    assert "registered for US A2P 10DLC" in r.json()["test"]["message"]
    req = rx.to("messaging.twilio.com")[0]
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"{ACCOUNT}:{AUTH_TOKEN}".encode()).decode()
    rx.service = {"us_app_to_person_registered": False}
    t = owner.post("/api/integrations/twilio_sms/test").json()
    assert t["ok"] and "will block these texts" in t["message"] and t["details"]["a2p_10dlc_registered"] is False
    rx.service_status = 401
    t = owner.post("/api/integrations/twilio_sms/test").json()
    assert not t["ok"] and "operator" in t["message"] and t["connection"]["status"] == "error"


def test_twilio_from_number_and_api_key_auth(owner, platform, rx):
    platform.env = {"TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_API_KEY": API_KEY, "TWILIO_API_SECRET": API_SECRET,
                    "TWILIO_FROM": "+14155559999"}
    r = owner.post("/api/integrations/twilio_sms/connect", json={"config": {}})
    assert r.json()["status"] == "connected", r.text
    req = rx.to("api.twilio.com")[0]
    assert req.url.params["PhoneNumber"] == "+14155559999"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    rx.numbers = [{"phone_number": "+14155559999", "capabilities": {"sms": False}}]
    assert not owner.post("/api/integrations/twilio_sms/test").json()["ok"]


# ---------------------------------------------------------------------------- Twilio: recipients and consent
def _add(c, phone="(415) 555-0100", **kw):
    return c.post("/api/integrations/twilio_sms/recipients",
                  json={"phone": phone, "name": "Pat Foreman", "consent": True, **kw})


def test_adding_a_phone_records_consent_and_sends_a_confirmation(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    assert _add(owner, consent=False).status_code == 422
    assert _add(owner, phone="+44 20 7946 0958").status_code == 422
    r = _add(owner)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["recipient"]["phone"] == "•••-•••-0100" and "4155550100" not in r.text
    assert out["confirmation"] == {"sent": True, "message": "Confirmation text sent."}
    form = {k: v[0] for k, v in parse_qs(rx.to("api.twilio.com")[0].content.decode()).items()}
    assert form["To"] == PHONE and form["MessagingServiceSid"] == SERVICE and "From" not in form
    assert form["Body"].startswith("TradeComply (Acme Builders): you're signed up") and "STOP" in form["Body"]
    assert twilio_sms.segments(form["Body"]) <= 2
    book, blob, _ = twilio_sms.load_book(platform, owner.tenant_id)
    rec = book["recipients"][0]
    assert rec["consent"]["by"] == "owner@acme.test" and rec["consent"]["user_id"] and rec["consent"]["at"]
    assert "agreed to receive TradeComply compliance alert texts for Acme Builders" in rec["consent"]["text"]
    assert rec["history"][0]["action"] == "opt_in" and rec["last_status"] == "queued"
    assert "4155550100" not in blob                                            # encrypted at rest
    assert _add(owner).status_code == 409                                      # already on the list
    audit = [a for a in owner.get("/api/audit").json() if a["tool"] == "integrations.sms_recipient_add"]
    assert audit and "4155550100" not in json.dumps(audit)


def test_api_keys_cannot_attest_consent(owner, app, platform):
    key = owner.post("/api/keys", json={"name": "zap"}).json()["key"]
    with browser(owner) as c:
        r = c.post("/api/integrations/twilio_sms/recipients", headers={"authorization": f"Bearer {key}"},
                   json={"phone": "4155550100", "name": "Pat", "consent": True})
        assert r.status_code == 403


def test_alerts_are_texted_once_per_phone_with_retries_and_limits(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    owner.post("/api/integrations/twilio_sms/connect", json={"config": {"max_per_day": 2}})
    _add(owner, send_confirmation=False)
    _add(owner, phone="415-555-0111", send_confirmation=False)
    rx.twilio[PHONE2] = [(429, {"code": 20429, "message": "Too Many Requests",
                                "more_info": "https://www.twilio.com/docs/errors/20429", "status": 429})]
    payload = {"title": "Report the injury to Cal/OSHA", "severity": "critical", "due_date": "2026-11-27",
               "summary": "Serious injury reported at Main St.", "link": "/#/dashboard"}
    platform.events.emit(owner.tenant_id, "injury_report_due", payload)
    platform.events.drain()
    msgs = [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in rx.to("api.twilio.com")]
    assert [m["To"] for m in msgs] == [PHONE, PHONE2]
    assert outbound(platform, owner.tenant_id)[0]["status"] == "pending"
    platform.events.drain(now=datetime.now(timezone.utc) + timedelta(minutes=10))
    msgs = [{k: v[0] for k, v in parse_qs(r.content.decode()).items()} for r in rx.to("api.twilio.com")]
    assert [m["To"] for m in msgs] == [PHONE, PHONE2, PHONE2]                  # the first phone isn't texted twice
    assert outbound(platform, owner.tenant_id)[0]["status"] == "delivered"
    body = msgs[0]["Body"]
    assert body.startswith("TradeComply (Acme Builders): Report the injury to Cal/OSHA. Due Nov 27, 2026.")
    assert "https://tc.example/#/dashboard" in body and body.endswith("Reply STOP to opt out.")
    # below the floor (warning by default): no text; over the daily cap: skipped
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "FYI", "severity": "info"})
    for n in range(3):
        platform.events.emit(owner.tenant_id, "payroll_issue", {"title": f"Problem {n}", "severity": "warning"})
    platform.events.drain()
    to_first = [m for m in (parse_qs(r.content.decode()) for r in rx.to("api.twilio.com")) if m["To"][0] == PHONE]
    assert len(to_first) == 2


def test_twilio_21610_marks_the_phone_opted_out(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    owner.post("/api/integrations/twilio_sms/connect", json={"config": {}})
    _add(owner, send_confirmation=False)
    rx.twilio[PHONE] = [(400, {"code": 21610, "message": "Attempt to send to unsubscribed recipient",
                               "more_info": "https://www.twilio.com/docs/errors/21610", "status": 400})]
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "x", "severity": "critical"})
    platform.events.drain()
    assert outbound(platform, owner.tenant_id)[0]["status"] == "delivered"
    rec = owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]
    assert rec["status"] == "opted_out" and rec["opted_out_via"] == "twilio_21610"
    platform.events.emit(owner.tenant_id, "payroll_issue", {"title": "y", "severity": "critical"})
    platform.events.drain()
    assert len(rx.to("api.twilio.com")) == 1                                    # one attempt, then never again
    assert _add(owner).status_code == 409                                       # staff can't re-add an opt-out


def test_remove_and_test_text(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    rid = _add(owner, send_confirmation=False).json()["recipient"]["id"]
    t = owner.post(f"/api/integrations/twilio_sms/recipients/{rid}/test").json()
    assert t == {"ok": True, "message": "Test text sent."}
    assert owner.delete(f"/api/integrations/twilio_sms/recipients/{rid}").json() == {"ok": True}
    assert owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"] == []
    assert owner.post(f"/api/integrations/twilio_sms/recipients/{rid}/test").status_code == 404
    again = _add(owner, send_confirmation=False).json()["recipient"]
    assert again["id"] == rid and again["status"] == "active"


# ---------------------------------------------------------------------------- Twilio: inbound STOP/START
def test_signature_matches_twilios_documented_example_and_sdk():
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234", "From": "+14158675310",
              "To": "+18005551212"}
    assert twilio_sms.compute_signature("12345", url, params) == "L/OH5YylLD5NRKLltdqwSvS0BnU="   # usage/security
    assert twilio_sms.valid_signature("12345", [url], list(params.items()), "L/OH5YylLD5NRKLltdqwSvS0BnU=")
    validator = pytest.importorskip("twilio.request_validator")
    v = validator.RequestValidator("12345")
    assert v.compute_signature(url, params) == twilio_sms.compute_signature("12345", url, params)
    sig = v.compute_signature("https://tc.example:443/x", params)
    assert twilio_sms.valid_signature("12345", ["https://tc.example/x"], params, sig)


def _inbound(c, body, *, from_=PHONE, account=ACCOUNT, token=AUTH_TOKEN, sig=None, extra=None, message_sid=None):
    sid = message_sid or "SM" + secrets.token_hex(16)          # every real message has its own MessageSid
    params = {"MessageSid": sid, "SmsSid": sid, "AccountSid": account,
              "MessagingServiceSid": SERVICE, "From": from_, "To": "+14155559999", "Body": body, "NumMedia": "0",
              "NumSegments": "1", "ApiVersion": "2010-04-01", **(extra or {})}
    url = f"{APP_URL}/api/integrations/twilio-sms/inbound"
    signature = sig or twilio_sms.compute_signature(token, url, params)
    return c.post("/api/integrations/twilio-sms/inbound", content=urlencode(params),
                  headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": signature})


def test_a_replayed_signed_reply_changes_nothing(owner, platform, rx):
    """X-Twilio-Signature has no timestamp: a captured START replayed after a later STOP must not opt the phone back
    in (each MessageSid is applied once), and a reply without a MessageSid is not applied."""
    platform.env = dict(TWILIO_ENV)
    _add(owner, send_confirmation=False)
    status = lambda: owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]["status"]  # noqa
    stop1, start1, stop2 = ("SM" + secrets.token_hex(16) for _ in range(3))
    assert _inbound(owner, "STOP", message_sid=stop1).status_code == 200 and status() == "opted_out"
    assert _inbound(owner, "START", message_sid=start1).status_code == 200 and status() == "active"
    assert _inbound(owner, "STOP", message_sid=stop2).status_code == 200 and status() == "opted_out"
    replay = _inbound(owner, "START", message_sid=start1)            # the same signed request again
    assert replay.status_code == 200 and replay.text.endswith("<Response></Response>")
    assert status() == "opted_out"
    assert _inbound(owner, "START", message_sid="not-a-sid").status_code == 200 and status() == "opted_out"
    history = twilio_sms.load_book(platform, owner.tenant_id)[0]["recipients"][0]["history"]
    assert [h["action"] for h in history] == ["opt_in", "opt_out", "opt_in", "opt_out"]


def test_a_failed_reply_can_be_retried_by_twilio(owner, platform, rx, monkeypatch):
    platform.env = dict(TWILIO_ENV)
    _add(owner, send_confirmation=False)
    sid = "SM" + secrets.token_hex(16)

    def broken(*_a, **_k):
        raise RuntimeError("database briefly down")
    monkeypatch.setattr(twilio_sms, "apply_reply", broken)
    c = TestClient(owner.app, raise_server_exceptions=False)
    assert _inbound(c, "STOP", message_sid=sid).status_code == 500
    monkeypatch.undo()
    assert _inbound(owner, "STOP", message_sid=sid).status_code == 200      # Twilio's retry is applied
    assert owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]["status"] == "opted_out"


def test_hand_triggered_texts_are_limited_per_phone(owner, other, platform, rx):
    """Removing and re-adding a number (in one company or several) can't be used to flood a phone with
    confirmation texts at the operator's expense."""
    platform.env = dict(TWILIO_ENV)
    sent = lambda: sum(1 for r in rx.seen if r.url.path.endswith("/Messages.json"))  # noqa: E731
    for _ in range(twilio_sms.MANUAL_TEXTS_PER_PHONE_DAY - 1):
        rid = _add(owner).json()["recipient"]["id"]
        assert owner.delete(f"/api/integrations/twilio_sms/recipients/{rid}").status_code == 200
    assert _add(other).status_code == 201                               # another company, the same phone
    before = sent()
    assert before == twilio_sms.MANUAL_TEXTS_PER_PHONE_DAY
    r = _add(owner)
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert sent() == before
    assert all(x["status"] != "active" for x in owner.get("/api/integrations/twilio_sms/recipients").json()[
        "recipients"])                                                   # a refused add changes nothing
    assert _add(owner, send_confirmation=False).status_code == 201      # adding without a text still works
    rid = owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]["id"]
    assert owner.post(f"/api/integrations/twilio_sms/recipients/{rid}/test").status_code == 429
    assert sent() == before


def test_inbound_stop_and_start_apply_to_every_company_with_that_phone(owner, other, platform, rx):
    platform.env = dict(TWILIO_ENV)
    for c in (owner, other):
        assert _add(c, send_confirmation=False).status_code == 201
    _add(owner, phone="4155550111", send_confirmation=False)
    r = _inbound(owner, "Stop")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/xml")
    assert r.text == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    for c in (owner, other):
        recs = {x["phone"]: x for x in c.get("/api/integrations/twilio_sms/recipients").json()["recipients"]}
        assert recs["•••-•••-0100"]["status"] == "opted_out" and recs["•••-•••-0100"]["opted_out_via"] == "reply"
    assert {x["phone"]: x["status"] for x in owner.get("/api/integrations/twilio_sms/recipients").json()[
        "recipients"]}["•••-•••-0111"] == "active"
    _inbound(owner, "help")
    _inbound(owner, "We'll be there at 3", extra={"OptOutType": "START"})       # Advanced Opt-Out says START
    for c in (owner, other):
        assert c.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]["status"] == "active"
    history = twilio_sms.load_book(platform, other.tenant_id)[0]["recipients"][0]["history"]
    assert [h["action"] for h in history] == ["opt_in", "opt_out", "opt_in"]


def test_inbound_refuses_bad_signatures_other_accounts_and_missing_token(owner, platform, rx):
    platform.env = dict(TWILIO_ENV)
    _add(owner, send_confirmation=False)
    assert _inbound(owner, "STOP", sig="AAAAAAAAAAAAAAAAAAAAAAAAAAA=").status_code == 403
    assert _inbound(owner, "STOP", token="someone-elses-token").status_code == 403
    assert _inbound(owner, "STOP", account="AC" + "f" * 32).status_code == 403
    no_sig = owner.post("/api/integrations/twilio-sms/inbound", content="Body=STOP",
                        headers={"content-type": "application/x-www-form-urlencoded"})
    assert no_sig.status_code == 403
    assert owner.post("/api/integrations/twilio-sms/inbound", json={"Body": "STOP"}).status_code == 415
    assert owner.get("/api/integrations/twilio_sms/recipients").json()["recipients"][0]["status"] == "active"
    platform.env = {k: v for k, v in TWILIO_ENV.items() if k != "TWILIO_AUTH_TOKEN"}
    assert _inbound(owner, "STOP").status_code == 503


@pytest.mark.parametrize("body,kind", [("STOP", "stop"), (" stop. ", "stop"), ("Unsubscribe", "stop"),
                                       ("please stop", "stop"), ("STOPALL", "stop"), ("quit", "stop"),
                                       ("Start", "start"), ("YES", "start"), ("yes please", None),
                                       ("HELP", "help"), ("Running late, there at 3", None), ("", None)])
def test_reply_keywords(body, kind):
    assert twilio_sms.classify_reply(body) == kind


# ---------------------------------------------------------------------------- Twilio: message length
def test_texts_fit_two_segments_in_gsm7_or_ucs2():
    long = {"title": "Last day to record a mechanics lien: due in 3 days (Main St Remodel)", "due_date": "2026-12-03",
            "summary": "Rule applied: 90 days after completion. Record before this date, not on it. " * 20,
            "link": "/#/project/3"}
    text = twilio_sms.alert_text("deadline_due_soon", long, "Acme “Best” Builders", APP_URL)
    assert twilio_sms.encoding(text) == "GSM-7" and twilio_sms.segments(text) <= 2 and len(text) <= 306
    assert text.startswith('TradeComply (Acme "Best" Builders): Last day') and text.endswith("Reply STOP to opt out.")
    uni = twilio_sms.alert_text("payroll_issue", {"title": "工资问题 " * 30}, "Acme", None)
    assert twilio_sms.encoding(uni) == "UCS-2" and twilio_sms.segments(uni) <= 2 and uni.endswith("opt out.")
    assert twilio_sms.segments("a" * 160) == 1 and twilio_sms.segments("a" * 161) == 2
    assert twilio_sms.segments("€" * 80) == 1 and twilio_sms.segments("€" * 81) == 2     # extension: 2 septets
    assert twilio_sms.segments("é" * 160) == 1 and twilio_sms.segments("ê" * 70) == 1 and \
        twilio_sms.segments("ê" * 71) == 2


def test_twilio_secrets_and_phones_stay_out_of_logs(owner, platform, rx, caplog):
    caplog.set_level(logging.DEBUG)
    platform.env = {"TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_API_KEY": API_KEY, "TWILIO_API_SECRET": API_SECRET,
                    "TWILIO_AUTH_TOKEN": AUTH_TOKEN, "TWILIO_MESSAGING_SERVICE_SID": SERVICE}
    owner.post("/api/integrations/twilio_sms/connect", json={"config": {}})
    _add(owner)
    platform.events.emit(owner.tenant_id, "injury_report_due", {"title": "Report it", "severity": "critical"})
    platform.events.drain()
    _inbound(owner, "STOP")
    logs = caplog.text
    for secret in (API_SECRET, AUTH_TOKEN, "4155550100", "5550100"):
        assert secret not in logs
