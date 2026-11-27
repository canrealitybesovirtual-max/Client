"""Calendar feed: every compliance deadline as a private iCalendar (.ics) subscription link for Google Calendar,
Outlook and Apple Calendar.

What is in the feed (one all-day VEVENT per deadline, dated in California, i.e. America/Los_Angeles):
  - per project: the 20-day preliminary notice (and the construction lender's copy), the last day to record a
    mechanics lien and the foreclosure-suit deadline (app.dashboard.project_deadlines -> tradecomply.logic
    .lien_deadlines, the calculate_lien_deadline tool), and the last day to give a stop payment notice
    (tradecomply.payments.stop_payment_notice_deadline, the stop_payment_notice_deadline tool; public projects too);
  - safety training: the next Cal/OSHA toolbox/tailgate meeting per active project (app.dashboard.toolbox_item, every
    10 working days after the last one logged);
  - license and insurance expirations: the company's CSLB license, each sub's CSLB license and insurance certificate
    (COI), and the company's own workers' comp, liability and bond renewal dates when entered in this connector's
    settings;
  - filings (opt-in, from this connector's settings): the dated items of tradecomply.business_ops.compliance_calendar
    (the compliance_calendar tool) for this year and next: payroll tax returns, W-2/1099, postings, training.

How the link works: a random 256-bit token in the path, /cal/<token>.ics. Only its SHA-256 is stored
(calendar_tokens table, like API keys), so the link is shown once, when it is made, and a database leak doesn't
reveal it. A tenant may hold several links (one per person or device) and revoke each, or rotate (revoke all, make
one). The access log is filtered so the token never reaches it (see _RedactFeedPath). No sign-in is possible: Google
fetches subscribed calendars from its own servers, so the link itself is the credential.

Format, from RFC 5545 (https://www.rfc-editor.org/rfc/rfc5545), re-read this session through Context7's index of
rfc-editor.org (the site itself was not reachable from the sandbox):
  - 3.1 content lines end in CRLF and are folded so no line is longer than 75 octets (CRLF + one space; a UTF-8
    character is never split);
  - 3.3.11 TEXT escapes backslash, semicolon and comma with a backslash and writes a line break as \\n;
  - 3.6.1 VEVENT carries UID and DTSTAMP; an all-day event is DTSTART;VALUE=DATE with DTEND;VALUE=DATE the next day
    (DTEND is non-inclusive);
  - 3.6.6 a DISPLAY VALARM has ACTION, TRIGGER and DESCRIPTION; 3.8.6.3 a relative TRIGGER counts from DTSTART
    (negative = before), here 9:00 local time N days before (TRIGGER:-P6DT15H is a week before);
  - 3.8.7.2 DTSTAMP is UTC (with METHOD present it is when the object was created);
  - 3.7 VERSION:2.0 and PRODID are required; METHOD:PUBLISH, CALSCALE:GREGORIAN.
RFC 7986 (https://www.rfc-editor.org/rfc/rfc7986), re-read the same way: calendar NAME, DESCRIPTION and
REFRESH-INTERVAL;VALUE=DURATION (a suggested minimum polling interval), and its UID guidance (no host names, IP
addresses or other identifying data; random UUIDs recommended). UIDs here are name-based UUIDs of the tenant and the
deadline's identity (what the deadline is: project and kind, sub and kind), never its date, so a moved date updates
the same event instead of adding a new one; only recurring filings carry their period, since each quarter's return
is its own deadline.
Dates: every event is an all-day DATE (floating, no time zone: RFC 5545 3.3.4), computed from California's calendar
day (America/Los_Angeles, tradecomply.logic.today_ca). X-WR-CALNAME, X-WR-TIMEZONE and X-PUBLISHED-TTL are de facto
extensions (not in an RFC) that Apple, Google and Microsoft clients are commonly reported to read; see `unconfirmed`.

HTTP API (`router`; app/main.py must include it before the catch-all MCP mount at "/", as for the Docusign and Procore
routers):
  GET    /cal/{token}.ics                                  the feed (no sign-in; 404 for an unknown or revoked link)
  GET    /api/integrations/calendar_feed/links              active links (ids and dates only; never the URL) [staff]
  POST   /api/integrations/calendar_feed/links              make a link; the URL is in this response only [staff]
  POST   /api/integrations/calendar_feed/links/rotate       revoke every link and make one [staff]
  DELETE /api/integrations/calendar_feed/links/{link_id}    revoke one link [staff]
  DELETE /api/integrations/calendar_feed/links              revoke every link [staff]
  GET    /api/integrations/calendar_feed/events             what the feed holds, as JSON [any role]
  GET    /api/integrations/calendar_feed/calendar.ics       the same feed with the session or an API key [any role]
The generic /api/integrations/calendar_feed/{config,connect,test,disconnect} routes work as for every connector:
connect makes the first link (returned once as feed_url), disconnect revokes every link.

No vendor API, no outbound HTTP and no operator credentials: required_env is empty, so the connector is always
"configured on this server".
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets as _secrets
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from tradecomply import business_ops, logic, payments

from .. import dashboard
from ..auth import Principal, RateLimiter, anyone, client_ip, office_up, platform_of, refuse_cross_site
from ..billing import base_url, entitlement, require_entitled
from . import ConfigField, Connector, ConnectorContext, ConnectorError, TestResult

log = logging.getLogger("tradecomply.connectors.calendar_feed")

TOKEN_BYTES = 32                                   # 256 bits; token_urlsafe(32) is 43 characters
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
LINK_ID_LEN = 12                                   # first hex digits of the token's hash: names a link in the UI
LINK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_LINKS = 20                                     # active links per tenant
PAST_DAYS = 180                                    # events older than this drop out of the feed
MAX_EVENTS = 3000
REFRESH = "PT4H"                                   # RFC 7986 REFRESH-INTERVAL hint (clients decide)
ALARM_HOUR = 9                                     # reminders at 9:00 local time
PRODID = "-//TradeComply CA//Compliance deadlines 2.0//EN"
UID_NAMESPACE = uuid.UUID("77ce439c-97a8-4220-aa04-0a6ab24c0139")   # fixed: changing it changes every UID
FEED_PATH = "/cal/{token}.ics"
CA_TZID = "America/Los_Angeles"                    # whose calendar day the all-day dates are

REMINDER_CHOICES = (("7,1", "A week before and the day before (9 AM)"),
                    ("14,7,1", "Two weeks, one week and the day before"),
                    ("30,7,1", "30 days, one week and the day before"),
                    ("1", "The day before only"),
                    ("none", "No reminders"))
ENTITY_CHOICES = tuple((e, e.replace("_", " ").capitalize()) for e in business_ops.Entity.__args__)
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
DEADLINE_KINDS = ("prelim_notice", "lien_recording", "lien_foreclosure", "stop_payment_notice", "license_expiry",
                  "coi_expiry", "policy_renewal", "filing")

_miss_limiter = RateLimiter(limit=30, window_s=10 * 60)       # unknown/revoked tokens per IP
_feed_limiter = RateLimiter(limit=120, window_s=60 * 60)      # fetches per link per hour


# ---------------------------------------------------------------------------- access-log redaction
class _RedactFeedPath(logging.Filter):
    """The token is in the path (/cal/<token>.ics), which uvicorn's access log prints; app.main's RedactTokens only
    covers query parameters. This filter turns the path into /cal/[redacted].ics."""
    _tc_calendar_redact = True
    PATTERN = re.compile(r"(/cal/)[^/?\s\"]+(\.ics)")

    def filter(self, record):
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(self.PATTERN.sub(r"\1[redacted]\2", a) if isinstance(a, str) else a
                                for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = self.PATTERN.sub(r"\1[redacted]\2", record.msg)
        return True


def install_log_filter(logger_name: str = "uvicorn.access") -> None:
    logger = logging.getLogger(logger_name)
    if not any(getattr(f, "_tc_calendar_redact", False) for f in logger.filters):
        logger.addFilter(_RedactFeedPath())


install_log_filter()


# ---------------------------------------------------------------------------- tokens
def new_token() -> str:
    return _secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def feed_url(base: str | None, token: str) -> str:
    return f"{(base or '').rstrip('/')}{FEED_PATH.format(token=token)}"


def webcal_url(url: str) -> str | None:
    """The same link with the webcal:// scheme (a de facto convention for "subscribe", not a registered scheme)."""
    return "webcal://" + url.split("://", 1)[1] if url.startswith("https://") else None


# ---------------------------------------------------------------------------- iCalendar writing (RFC 5545)
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def escape_text(value) -> str:
    """RFC 5545 3.3.11 TEXT: backslash, semicolon and comma escaped, line breaks as \\n, other control characters
    (not allowed in TEXT) dropped."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text.replace("\t", " "))
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """RFC 5545 3.1: at most 75 octets per line (the CRLF not counted); a continuation starts with one space, which
    counts toward its 75. A multi-octet UTF-8 character is never split."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts, cur, size, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            parts.append("".join(cur))
            cur, size, limit = [], 0, 74
        cur.append(ch)
        size += n
    parts.append("".join(cur))
    return "\r\n ".join(parts)


def ics_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def ics_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def trigger_before(days: int, hour: int = ALARM_HOUR) -> str:
    """A relative TRIGGER (RFC 5545 3.8.6.3, dur-value 3.3.6) firing at `hour`:00 local time `days` days before an
    all-day event (whose start is local midnight): -P6DT15H is 9 AM a week before, -PT15H 9 AM the day before."""
    hours = days * 24 - hour
    if hours <= 0:
        return f"PT{-hours}H" if hours else "PT0H"
    d, h = divmod(hours, 24)
    return "-P" + (f"{d}D" if d else "") + (f"T{h}H" if h else "")


@dataclass
class FeedItem:
    identity: str                     # stable: what the deadline is, never its date (UID source)
    kind: str
    title: str
    day: date
    detail: str = ""
    category: str = "Deadline"
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    sub_id: Optional[int] = None
    sub_name: Optional[str] = None
    citations: list[str] = field(default_factory=list)
    verified: bool = False
    checked_on: Optional[str] = None
    alarms: tuple[int, ...] = ()
    path: Optional[str] = None        # app page, e.g. /#/project/3

    def summary(self) -> str:
        where = " / ".join(filter(None, [self.project_name, self.sub_name]))
        return self.title + (f" - {where}" if where else "")

    def description(self, app_url: str | None) -> str:
        lines = [self.detail] if self.detail else []
        if self.citations:
            lines.append("Source: " + "; ".join(dict.fromkeys(self.citations)))
        lines.append(("Verified against the official source" + (f" on {self.checked_on}" if self.checked_on else ""))
                     if self.verified else "Not yet verified against the official source")
        lines.append("From TradeComply. Not legal advice: confirm deadlines with your attorney.")
        if app_url and self.path:
            lines.append(f"Open in TradeComply: {app_url}{self.path}")
        return "\n".join(lines)


def event_uid(tenant_id: str, identity: str) -> str:
    """UUID derived from the tenant and the deadline's identity (RFC 7986: no host names or identifying data)."""
    return str(uuid.uuid5(UID_NAMESPACE, f"{tenant_id}|{identity}"))


def render_ics(items: list[FeedItem], *, tenant_id: str, company_name: str, stamp: datetime,
               app_url: str | None = None) -> str:
    name = escape_text(f"TradeComply: {company_name}")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"NAME:{name}", f"X-WR-CALNAME:{name}", f"X-WR-TIMEZONE:{CA_TZID}",
             "DESCRIPTION:" + escape_text("California compliance deadlines from TradeComply. Not legal advice."),
             f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}", f"X-PUBLISHED-TTL:{REFRESH}"]
    dtstamp = ics_utc(stamp)
    for it in items:
        summary = escape_text(it.summary())
        lines += ["BEGIN:VEVENT", f"UID:{event_uid(tenant_id, it.identity)}", f"DTSTAMP:{dtstamp}",
                  f"DTSTART;VALUE=DATE:{ics_date(it.day)}",
                  f"DTEND;VALUE=DATE:{ics_date(it.day + timedelta(days=1))}",
                  f"SUMMARY:{summary}", f"DESCRIPTION:{escape_text(it.description(app_url))}",
                  f"CATEGORIES:TradeComply,{escape_text(it.category)}", "TRANSP:TRANSPARENT"]
        if app_url and it.path and app_url.startswith("https://"):
            lines.append(f"URL:{app_url}{it.path}")
        for days in it.alarms:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{summary}", f"TRIGGER:{trigger_before(days)}",
                      "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def etag_for(body: str) -> str:
    """Weak validator over the content without DTSTAMP (which is the generation time), so an unchanged calendar
    answers If-None-Match with 304."""
    stable = "\n".join(line for line in body.split("\r\n") if not line.startswith("DTSTAMP:"))
    return 'W/"' + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32] + '"'


# ---------------------------------------------------------------------------- what goes in the feed
def _d(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _cites(raw) -> list[str]:
    out = []
    for c in raw or []:
        text = c.get("citation") if isinstance(c, dict) else getattr(c, "citation", c)
        if text:
            out.append(str(text))
    return out


def reminder_days(config: dict) -> tuple[int, ...]:
    choice = (config or {}).get("reminders") or "7,1"
    if choice == "none":
        return ()
    try:
        return tuple(sorted({int(x) for x in str(choice).split(",") if x.strip()}, reverse=True))
    except ValueError:
        return (7, 1)


def _from_dashboard(it: dict, alarms: tuple[int, ...], identity: str, category: str) -> FeedItem | None:
    day = _d(it.get("date"))
    if not day:
        return None
    pid, sid = it.get("project_id"), it.get("sub_id")
    return FeedItem(identity=identity, kind=it["kind"], title=it["title"], day=day, detail=it.get("detail") or "",
                    category=category, project_id=pid, project_name=it.get("project_name"), sub_id=sid,
                    sub_name=it.get("sub_name"), citations=_cites(it.get("citations")),
                    verified=bool(it.get("verified")), checked_on=it.get("checked_on"), alarms=alarms,
                    path=f"/#/project/{pid}" if pid else ("/#/subs" if sid else "/#/dashboard"))


def spn_item(project: dict, today: date, alarms: tuple[int, ...]) -> FeedItem | None:
    """Last day to give a stop payment notice (private or public works), from the stop_payment_notice_deadline tool.
    None when it doesn't apply (no dates yet, a direct contractor with no construction lender, a public-works prime,
    a design professional)."""
    if project.get("status") == "closed":
        return None
    role = (project.get("extra") or {}).get("claimant_role") or "direct_contractor"
    if role not in payments.Claimant.__args__:
        return None
    completion, noc, last = (project.get("completion_date") or None, project.get("noc_date") or None,
                             project.get("last_furnishing_date") or None)
    if not (completion or noc or last):
        return None
    kind = "public" if project.get("public_or_private") == "public" else "private"
    r = payments.stop_payment_notice_deadline(
        kind, role, completion_date=completion, noc_recorded_date=noc, last_furnishing_date=last,
        first_furnishing_date=project.get("first_furnishing_date") or None, as_of=today,
        construction_lender=bool(project.get("lender_name")))
    day = _d(r.deadline)
    if not r.applicable or not day:
        return None
    detail = (f"Rule applied: {r.deadline_rule_applied}. Give the notice before this date, not on it.")
    if r.act_by_date and r.act_by_date != r.deadline:
        detail += f" {r.deadline} is a weekend or holiday: give it by {r.act_by_date}."
    if r.may_give_to:
        detail += " Give it to: " + ", ".join(r.may_give_to) + "."
    pid = project["id"]
    return FeedItem(identity=f"stop_payment_notice:p{pid}", kind="stop_payment_notice",
                    title="Last day to give a stop payment notice", day=day, detail=detail, category="Lien and payment",
                    project_id=pid, project_name=project.get("name"), citations=_cites(r.citations),
                    verified=bool(r.verified), alarms=alarms, path=f"/#/project/{pid}")


def filing_items(config: dict, today: date, alarms: tuple[int, ...]) -> tuple[list[FeedItem], list[str]]:
    """Dated items of compliance_calendar for this year and next, when the tenant opted in and gave its business
    profile in the connector settings."""
    if not config.get("include_filings"):
        return [], []
    entity = config.get("entity_type")
    if entity not in business_ops.Entity.__args__:
        return [], ["Filings are on, but the business type isn't set in the calendar settings."]
    has_employees = config.get("has_employees")
    has_employees = True if has_employees is None else bool(has_employees)
    try:
        count = max(0, int(config.get("employee_count") or 0))
    except (TypeError, ValueError):
        count = 0
    if not has_employees:
        count = 0
    out, notes, seen = [], [], set()
    for year in (today.year, today.year + 1):
        try:
            cal = business_ops.compliance_calendar(
                year, entity, has_employees, count, llc=entity == "llc",
                pays_independent_contractors=bool(config.get("pays_independent_contractors")),
                as_of=today.isoformat(), public_works=bool(config.get("public_works")))
        except ValueError as e:
            notes.append(f"Filings for {year}: {e}")
            continue
        for ob in cal.items:
            day = _d(ob.due_date)
            if not day or ob.category in ("license", "insurance_bond"):     # those come from their own dates
                continue
            ident = f"filing:{re.sub(r'[^a-z0-9]+', '-', ob.title.lower()).strip('-')[:80]}:{day.isoformat()}"
            if ident in seen:
                continue
            seen.add(ident)
            detail = " ".join(filter(None, [ob.covers, f"Form: {ob.form}." if ob.form else "",
                                            f"Agency: {ob.agency}." if ob.agency else "", ob.how]))
            out.append(FeedItem(identity=ident, kind="filing", title=ob.title, day=day, detail=detail[:1500],
                                category=ob.category.replace("_", " ").capitalize(),
                                verified=bool(ob.verified), alarms=alarms, path="/#/dashboard"))
    return out, notes


POLICY_FIELDS = (("wc_policy_renewal_date", "Workers' comp policy renews"),
                 ("liability_policy_renewal_date", "General liability policy renews"),
                 ("bond_renewal_date", "Contractor's bond renews"))


def build_items(store, tenant: dict, config: dict | None = None, today: date | None = None) -> tuple[list[FeedItem],
                                                                                                    list[str]]:
    """Every deadline for one tenant, oldest first. Each source is computed on its own, so a project with bad data
    can't take the rest of the feed down. Returns (items, notes)."""
    today = today or logic.today_ca()
    config = config or {}
    alarms = reminder_days(config)
    tid = tenant["id"]
    items: list[FeedItem] = []
    notes: list[str] = []

    def guard(what: str, fn):
        try:
            return fn()
        except Exception as e:      # noqa: BLE001 - one bad record must not break the whole feed
            log.warning("calendar feed: %s skipped for tenant %s (%s)", what, tid, type(e).__name__)
            notes.append(f"Skipped {what}: it could not be computed.")
            return None

    def company_license():
        card = dashboard.license_card(tenant.get("cslb_license"))
        lic = card.get("license") or {}
        day = _d(lic.get("expiration_date")) if lic.get("found") else None
        if day:
            cites, _, checked = dashboard._rule_citation(dashboard.LICENSE_RULE)
            items.append(FeedItem(identity="license_expiry:company", kind="license_expiry",
                                  title="Your CSLB license expires", day=day,
                                  detail="Renew with CSLB before this date.",
                                  category="License", citations=_cites(cites), verified=bool(lic.get("verified")),
                                  checked_on=checked, alarms=tuple(sorted(set(alarms) | ({30} if alarms else set()),
                                                                          reverse=True)),
                                  path="/#/settings"))
    guard("your license", company_license)

    for name, label in POLICY_FIELDS:
        day = _d(config.get(name))
        if day:
            items.append(FeedItem(identity=f"policy_renewal:{name}", kind="policy_renewal", title=label, day=day,
                                  detail="Get the renewal certificate on file (and with CSLB for workers' comp) before "
                                         "this date.", category="Insurance", alarms=alarms, path="/#/settings"))

    projects = store.list("projects", tid, order="id asc", limit=5000)
    last_talks = store.last_field_log_dates(tid, "toolbox_talk")
    for p in projects:
        pid = p["id"]

        def lien(p=p, pid=pid):
            its, _ = dashboard.project_deadlines(p, today)
            for it in its:
                if it["kind"] not in ("prelim_notice", "lien_recording", "lien_foreclosure"):
                    continue          # data_problem items are dated "today" and move daily: not calendar events
                lender = it["kind"] == "prelim_notice" and "lender" in it["title"].lower()
                fi = _from_dashboard(it, alarms, f"{it['kind']}:p{pid}" + (":lender" if lender else ""),
                                     "Lien and payment")
                if fi:
                    items.append(fi)
        guard(f"project {pid} lien dates", lien)

        def spn(p=p, pid=pid):
            fi = spn_item(p, today, alarms)
            if fi:
                items.append(fi)
        guard(f"project {pid} stop payment notice", spn)

        def toolbox(p=p, pid=pid):
            it = dashboard.toolbox_item(p, last_talks.get(pid), today)
            if it and it["kind"] == "toolbox_talk":
                fi = _from_dashboard(it, (1,) if alarms else (), f"toolbox_talk:p{pid}", "Safety training")
                if fi:
                    items.append(fi)
        guard(f"project {pid} toolbox talk", toolbox)

    for s in store.list("subs", tid, order="name asc", limit=5000):
        sid = s["id"]

        def coi(s=s, sid=sid):
            day = _d(s.get("coi_expiry"))
            if day:
                items.append(FeedItem(identity=f"coi_expiry:s{sid}", kind="coi_expiry",
                                      title="Sub insurance certificate (COI) expires", day=day,
                                      detail="Get a current certificate of liability insurance before they keep "
                                             "working.", category="Insurance", sub_id=sid, sub_name=s.get("name"),
                                      alarms=alarms, path="/#/subs"))
        guard(f"sub {sid} insurance", coi)

        def sub_license(s=s, sid=sid):
            if not s.get("license_number"):
                return
            chk = dashboard.sub_check(s)
            lic = chk.get("license") or {}
            day = _d(lic.get("expiration_date")) if lic.get("found") else None
            if day:
                cites, _, checked = dashboard._rule_citation(dashboard.LICENSE_RULE)
                items.append(FeedItem(identity=f"license_expiry:s{sid}", kind="license_expiry",
                                      title="Sub's CSLB license expires", day=day,
                                      detail="An expired license means they can't legally do the work.",
                                      category="License", sub_id=sid, sub_name=s.get("name"),
                                      citations=_cites(cites), verified=bool(chk.get("verified")),
                                      checked_on=checked, alarms=alarms, path="/#/subs"))
        guard(f"sub {sid} license", sub_license)

    filings = guard("filings", lambda: filing_items(config, today, alarms))
    if filings:
        items.extend(filings[0])
        notes.extend(filings[1])

    oldest = today - timedelta(days=PAST_DAYS)
    items = [i for i in items if oldest <= i.day < date.max - timedelta(days=1)]
    seen: dict[str, int] = {}
    for it in items:                                   # identities are unique per tenant (UID collisions merge events)
        n = seen.get(it.identity, 0)
        seen[it.identity] = n + 1
        if n:
            it.identity = f"{it.identity}#{n + 1}"
    items.sort(key=lambda i: (i.day, i.kind, i.identity))
    if len(items) > MAX_EVENTS:
        notes.append(f"Only the first {MAX_EVENTS} deadlines are in the feed.")
        items = items[:MAX_EVENTS]
    return items, notes


def event_view(tenant_id: str, it: FeedItem) -> dict:
    return {"uid": event_uid(tenant_id, it.identity), "date": it.day.isoformat(), "kind": it.kind,
            "summary": it.summary(), "category": it.category, "project_id": it.project_id, "sub_id": it.sub_id,
            "reminders_days_before": list(it.alarms), "verified": it.verified}


# ---------------------------------------------------------------------------- the connector
class CalendarFeedConnector(Connector):
    provider = "calendar_feed"
    display_name = "Calendar feed (Google Calendar, Outlook, Apple Calendar)"
    catalog_id = "calendar-feed"
    description = ("A private calendar link with every deadline TradeComply tracks: preliminary notices, lien and stop "
                   "payment notice dates, license and insurance expirations, toolbox talks and, if you turn them on, "
                   "payroll and tax filings. Add it once in your calendar app; it stays up to date.")
    category = "calendar"
    docs_url = "https://www.rfc-editor.org/rfc/rfc5545"
    required_env = ()
    config_fields = (
        ConfigField("reminders", "Reminders", type="select", options=REMINDER_CHOICES, default="7,1",
                    help="Built into each deadline. Some calendar apps ignore reminders in subscribed calendars; set "
                         "notifications for the calendar there too."),
        ConfigField("include_filings", "Add payroll, tax and posting filing dates", type="boolean", default=False,
                    help="From the compliance calendar for this year and next. Fill in the business details below."),
        ConfigField("entity_type", "Business type", type="select", options=ENTITY_CHOICES),
        ConfigField("has_employees", "We have employees", type="boolean", default=True),
        ConfigField("employee_count", "Number of employees", type="number"),
        ConfigField("pays_independent_contractors", "We pay independent contractors $600 or more a year",
                    type="boolean", default=False),
        ConfigField("public_works", "We do public works (prevailing wage) jobs", type="boolean", default=False),
        ConfigField("wc_policy_renewal_date", "Workers' comp policy renewal date", pattern=DATE_PATTERN,
                    placeholder="YYYY-MM-DD", max_length=10),
        ConfigField("liability_policy_renewal_date", "Liability policy renewal date", pattern=DATE_PATTERN,
                    placeholder="YYYY-MM-DD", max_length=10),
        ConfigField("bond_renewal_date", "Contractor's bond renewal date", pattern=DATE_PATTERN,
                    placeholder="YYYY-MM-DD", max_length=10),
    )
    unconfirmed = (
        "How often Google Calendar, Outlook and Apple Calendar re-read a subscribed calendar (often reported as hours "
        "to more than a day). The feed suggests 4 hours (REFRESH-INTERVAL, X-PUBLISHED-TTL); clients decide. "
        "support.google.com and support.microsoft.com could not be read for this connector.",
        "Whether each calendar app shows the reminders (VALARM) of a subscribed calendar; Google Calendar is commonly "
        "reported to ignore them in favor of the calendar's own notification settings.",
        "X-WR-CALNAME, X-WR-TIMEZONE and X-PUBLISHED-TTL are de facto extensions, not standards; webcal:// is a "
        "convention, not a registered URI scheme. Use the https:// link if webcal doesn't open.",
        "The steps to subscribe (Google Calendar: Other calendars > From URL; Outlook: Add calendar > Subscribe from "
        "web) are as commonly described; Google's and Microsoft's help pages could not be read for this connector.",
        "Render's own HTTP request logs, if turned on for the service, may record request paths, which hold the link's "
        "secret. TradeComply's access log redacts it.",
    )

    # ------------------------------------------------------------------ links
    def active_links(self) -> list[dict]:
        return [{"id": t["token_hash"][:LINK_ID_LEN], "created": t["created"]}
                for t in self.ctx.store.list_calendar_tokens(self.ctx.tenant_id)]

    def _sync_status(self) -> None:
        active = len(self.ctx.store.list_calendar_tokens(self.ctx.tenant_id))
        conn = self.connection()
        if active:
            if not conn or conn["status"] != "connected":
                self.save(status="connected", last_error=None)
        elif conn and conn["status"] == "connected":
            self.save(status="disconnected")

    def create_link(self) -> tuple[str, dict]:
        """A new link's token (to show once) and its record. Raises ConnectorError at MAX_LINKS."""
        if len(self.ctx.store.list_calendar_tokens(self.ctx.tenant_id)) >= MAX_LINKS:
            raise ConnectorError(f"You already have {MAX_LINKS} calendar links. Revoke one you no longer use first.")
        token = new_token()
        row = self.ctx.store.add_calendar_token(self.ctx.tenant_id, token_hash(token))
        self._sync_status()
        return token, {"id": row["token_hash"][:LINK_ID_LEN], "created": row["created"]}

    def revoke_link(self, link_id: str) -> bool:
        if not LINK_ID_RE.fullmatch(str(link_id or "")):
            return False
        for t in self.ctx.store.list_calendar_tokens(self.ctx.tenant_id):
            if t["token_hash"].startswith(link_id):
                ok = self.ctx.store.revoke_calendar_token(self.ctx.tenant_id, t["token_hash"])
                self._sync_status()
                return ok
        return False

    def revoke_all(self) -> int:
        n = self.ctx.store.revoke_calendar_tokens(self.ctx.tenant_id)
        self._sync_status()
        return n

    # ------------------------------------------------------------------ feed
    def build(self, today: date | None = None) -> tuple[list[FeedItem], list[str]]:
        tenant = self.ctx.store.get_tenant(self.ctx.tenant_id)
        if not tenant:
            raise ConnectorError("Account not found")
        return build_items(self.ctx.store, tenant, self.config(), today)

    def render(self, app_url: str | None = None, now: datetime | None = None) -> tuple[str, int]:
        tenant = self.ctx.store.get_tenant(self.ctx.tenant_id)
        if not tenant:
            raise ConnectorError("Account not found")
        items, _ = build_items(self.ctx.store, tenant, self.config())
        body = render_ics(items, tenant_id=tenant["id"], company_name=tenant.get("company_name") or "",
                          stamp=now or datetime.now(timezone.utc), app_url=app_url)
        return body, len(items)

    # ------------------------------------------------------------------ lifecycle
    def test(self) -> TestResult:
        items, notes = self.build()
        links = self.active_links()
        by_kind: dict[str, int] = {}
        for it in items:
            by_kind[it.kind] = by_kind.get(it.kind, 0) + 1
        msg = f"The feed has {len(items)} deadline{'' if len(items) == 1 else 's'}. "
        msg += (f"{len(links)} private link{'' if len(links) == 1 else 's'} active." if links
                else "No private link yet: make one to subscribe.")
        return TestResult(True, msg, details={"events": len(items), "by_kind": by_kind, "active_links": len(links),
                                              "notes": notes[:10]})

    def connect(self) -> dict:
        """Turns the feed on: makes the first private link when there is none (returned once as feed_url)."""
        self.require_operator()
        token = None
        if not self.active_links():
            token, _ = self.create_link()
        result = self.test()
        self.save(status="connected", last_error=None)
        out = {**self.status(), "test": result.to_dict()}
        if token:
            url = feed_url(self.ctx.platform.settings.app_url, token)
            out.update(feed_url=url, webcal_url=webcal_url(url),
                       message="Copy this calendar link now. It is shown only once; make a new link if you lose it.")
        return out

    def disconnect(self) -> dict:
        """Revokes every link (existing subscriptions stop updating) and turns the feed off."""
        self.ctx.store.revoke_calendar_tokens(self.ctx.tenant_id)
        return super().disconnect()


# ---------------------------------------------------------------------------- HTTP routes
router = APIRouter(tags=["integrations"])
PREFIX = "/api/integrations/calendar_feed"


def _instance(request: Request, p: Principal) -> CalendarFeedConnector:
    return CalendarFeedConnector(ConnectorContext(platform_of(request), p.tenant_id, p.user_id, p.label))


def _audit(request: Request, p: Principal, action: str, args: dict, ok: bool = True) -> None:
    try:
        platform_of(request).store.audit(p.tenant_id, p.label, f"integrations.calendar_feed_{action}", args, None,
                                         ok=ok, source="integrations")
    except Exception:      # noqa: BLE001 - the change already happened
        log.exception("calendar feed: audit write failed")


def _link_response(request: Request, token: str, link: dict, **extra) -> dict:
    url = feed_url(base_url(request), token)
    return {**link, "url": url, "webcal_url": webcal_url(url), **extra,
            "message": "Copy this calendar link now. It is shown only once; make a new link if you lose it. Anyone "
                       "with the link can see your deadlines and project names."}


def _ics_response(request: Request, body: str, filename: str = "tradecomply.ics") -> Response:
    tag = etag_for(body)
    headers = {"ETag": tag, "Cache-Control": "private, max-age=300", "X-Robots-Tag": "noindex, nofollow",
               "Content-Disposition": f'inline; filename="{filename}"'}
    sent = request.headers.get("if-none-match") or ""
    if tag in [v.strip() for v in sent.split(",")]:
        return Response(status_code=304, headers=headers)
    # For HEAD the server sends the headers only.
    return Response(body, media_type="text/calendar; charset=utf-8", headers=headers)


@router.api_route(FEED_PATH, methods=["GET", "HEAD"], include_in_schema=False)
async def calendar_feed(token: str, request: Request):
    """The private feed. The token is the credential (calendar apps can't sign in); it is never logged."""
    platform = platform_of(request)
    hashed = token_hash(token) if TOKEN_RE.fullmatch(token or "") else None
    tid = await anyio.to_thread.run_sync(platform.store.calendar_token_tenant, hashed) if hashed else None
    tenant = await anyio.to_thread.run_sync(platform.store.get_tenant, tid) if tid else None
    if not tenant:
        # Only misses count against the address: Google and Microsoft fetch everyone's calendars from shared
        # servers, so a valid link must never be blocked because of someone else's revoked one.
        key = f"ip|{client_ip(request)}"
        wait = _miss_limiter.retry_after(key)
        _miss_limiter.hit(key)
        if wait:
            raise HTTPException(429, "Too many requests", headers={"Retry-After": str(wait)})
        raise HTTPException(404, "Calendar not found")
    link_key = f"link|{hashed[:LINK_ID_LEN]}"
    wait = _feed_limiter.retry_after(link_key)
    if wait:
        raise HTTPException(429, "This calendar link is being fetched too often", headers={"Retry-After": str(wait)})
    _feed_limiter.hit(link_key)
    if not entitlement(tenant)["ok"]:
        raise HTTPException(402, "Subscription required")
    inst = CalendarFeedConnector(ConnectorContext(platform, tid))
    app_url = platform.settings.app_url or base_url(request)
    try:
        body, count = await anyio.to_thread.run_sync(lambda: inst.render(app_url))
    except ConnectorError:
        raise HTTPException(404, "Calendar not found") from None
    log.info("calendar feed served for tenant %s (%d events)", tid, count)
    return _ics_response(request, body)


@router.get(f"{PREFIX}/links")
def list_links(request: Request, p: Principal = Depends(office_up)):
    """Active links: ids (the first hex digits of the stored hash) and creation times. The URLs themselves are
    shown only when a link is made."""
    inst = _instance(request, p)
    links = inst.active_links()
    return {"links": links, "active": len(links), "max_links": MAX_LINKS, "status": inst.status()["status"],
            "message": None if links else "No calendar link yet. Make one to subscribe."}


@router.post(f"{PREFIX}/links", status_code=201)
def create_link(request: Request, p: Principal = Depends(office_up)):
    require_entitled(platform_of(request), p)
    inst = _instance(request, p)
    try:
        token, link = inst.create_link()
    except ConnectorError as e:
        raise HTTPException(409, str(e))
    _audit(request, p, "link_create", {"link_id": link["id"]})
    return _link_response(request, token, link)


@router.post(f"{PREFIX}/links/rotate")
def rotate_links(request: Request, p: Principal = Depends(office_up)):
    """Revoke every link and make one new one (use it when a link may have leaked)."""
    require_entitled(platform_of(request), p)
    inst = _instance(request, p)
    revoked = inst.ctx.store.revoke_calendar_tokens(p.tenant_id)
    token, link = inst.create_link()
    _audit(request, p, "link_rotate", {"revoked": revoked, "link_id": link["id"]})
    return _link_response(request, token, link, revoked=revoked)


@router.delete(f"{PREFIX}/links/{{link_id}}")
def revoke_link(link_id: str, request: Request, p: Principal = Depends(office_up)):
    inst = _instance(request, p)
    if not inst.revoke_link(link_id):
        raise HTTPException(404, "Calendar link not found")
    _audit(request, p, "link_revoke", {"link_id": link_id})
    return {"ok": True, "active": len(inst.active_links())}


@router.delete(f"{PREFIX}/links")
def revoke_all_links(request: Request, p: Principal = Depends(office_up)):
    inst = _instance(request, p)
    revoked = inst.revoke_all()
    _audit(request, p, "link_revoke_all", {"revoked": revoked})
    return {"ok": True, "revoked": revoked, "active": 0}


@router.get(f"{PREFIX}/events")
def list_events(request: Request, p: Principal = Depends(anyone)):
    """What the feed holds right now, as JSON (dates, titles, reminders), for the settings page and checks."""
    platform = platform_of(request)
    require_entitled(platform, p)
    items, notes = _instance(request, p).build()
    return {"today": logic.today_ca().isoformat(), "count": len(items), "notes": notes,
            "events": [event_view(p.tenant_id, i) for i in items]}


@router.get(f"{PREFIX}/calendar.ics")
def download_ics(request: Request, p: Principal = Depends(anyone)):
    """The same calendar, signed in (session or API key): for a one-time download or import."""
    if p.via == "cookie":
        refuse_cross_site(request, "Download the calendar from TradeComply itself, not from a link on another site.")
    platform = platform_of(request)
    require_entitled(platform, p)
    body, _ = _instance(request, p).render(platform.settings.app_url or base_url(request))
    return _ics_response(request, body)
