"""Alert channels on the event bus (Slack Block Kit, Microsoft Teams Adaptive Card, a signed JSON webhook for Workato,
Zapier, ServiceNow and similar receivers) and the daily deadline scan that emits `deadline_due_soon` 14, 7, 3 and 1
days before each tracked deadline. SMS alerts are in app/connectors/twilio_sms.py.

Three connectors, one per destination, so a failure at one channel is retried without re-posting to the others:

    slack_alerts    a Slack incoming webhook, posted as Block Kit         (catalog card "slack")
    teams_alerts    a Teams Workflows webhook, posted as an Adaptive Card (catalog card "microsoft-teams")
    signed_webhook  any HTTPS endpoint, JSON signed with X-TradeComply-Signature (catalog card "generic-webhooks")

Each subscribes to every event in app/events.EVENTS and is delivered by the event bus only while the tenant's
connection is `connected` ("Save and connect" posts one test message; that is the only way to check an incoming
webhook, which has no read-only call). Per-tenant settings: the webhook URL (encrypted: Slack and Teams put the
secret in the URL), the lowest severity to send and an optional event list. They need no operator env vars.

These connectors sit next to the basic webhooks in app/webhooks.py (Settings > webhooks, `{"text": ...}` Slack posts
and Standard Webhooks signatures). `send()` below has the same signature and result as `webhooks.send()`, so the
event bus can switch to these formats in one line if the product wants one webhook feature instead of two.

Sources (read 2026-11-26; docs.slack.dev, learn.microsoft.com and the vendors' doc sites were read through
Context7's index of the official pages, because direct fetches from this build environment are blocked):

Slack, docs.slack.dev
- messaging/sending-messages-using-incoming-webhooks: POST JSON to the hooks.slack.com/services/... URL; a payload
  may carry `text` and `blocks` (the documented example sends both); channel, username and icon can't be overridden.
  Errors: invalid_payload (don't retry without correcting), action_prohibited, channel_is_archived, no_active_hooks,
  no_service, no_team, team_disabled, channel_not_found, user_not_found, posting_to_general_channel_denied.
  tools/python-slack-sdk/webhook: with blocks, "setting [text] as well is recommended as it works as fallback".
- reference/block-kit/blocks/header-block: plain_text, at most 150 characters. section-block: text 1-3000
  characters; `fields` at most 10 items of at most 2000 characters each. context-block: at most 10 elements, mrkdwn
  or plain_text. apis/web-api/rate-limits: incoming webhooks 1 message per second, short bursts allowed.
- block-kit button element: a `url` button still sends an interaction payload the app must acknowledge, and an
  incoming-webhook-only app has no interactivity endpoint, so the link is a mrkdwn <url|label> in a section instead.
- Formatting message text: escape &, < and > as &amp; &lt; &gt; (already used by app/webhooks.py).

Microsoft Teams, learn.microsoft.com
- connectors/teams, "When a Teams webhook request is received": POST {"type": "message", "attachments":
  [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content": <Adaptive Card>}]};
  the documented example card is version 1.2. "Known issues and limitations": messages are limited to about 28 KB.
- adaptive-cards/schema-explorer/text-block: TextBlock text, color (default, dark, light, accent, good, warning,
  attention), isSubtle, size (small, default, medium, large, extraLarge), weight (lighter, default, bolder), wrap
  (all version 1.0). FactSet {title, value} and Action.OpenUrl {title, url} appear in the cards reference samples
  (microsoftteams/platform/concepts/cards/cards-reference, adaptive-cards/samples).

Signed webhook receivers (how a customer verifies X-TradeComply-Signature)
- Workato: WEL `hmac_hex_sha256(body, secret)` is hex (docs.workato.com/en/wel/stdlib/crypto), so use the formula
  methods instead: `(timestamp + "." + raw_body).hmac_sha256(secret).encode_base64` (docs.workato.com/en/formulas/
  other-formulas: hmac_sha256, encode_base64). A Connector SDK trigger needs `webhook_payload_type: "raw"` to see the
  unparsed body (developing-connectors/sdk/guides/building-triggers/securing-webhooks).
- Zapier: in a Zapier integration's webhook `perform`, bundle.rawRequest.content is the raw body and headers are
  "prefixed with Http- and named in Camel-Case", e.g. Http-X-Tradecomply-Signature (docs.zapier.com/integrations/
  build/bundle). A no-code Zap (Catch Raw Hook + Code by Zapier) is NOT confirmed here: its help pages weren't read.
- ServiceNow: GlideCertificateEncryption / scoped CertificateEncryption generateMac(key, "HmacSHA256", data) returns
  the MAC in base64 and takes a base64 key (gs.base64Encode(secret)) (Yokohama API reference). How a Scripted REST
  resource reads the raw body and headers was not re-read this session.
  The base64 encoding was chosen because ServiceNow's generateMac only produces base64.

Signed webhook format (signed_webhook):
    POST <your URL>, Content-Type: application/json, body (UTF-8, compact JSON, keys sorted):
        {"id": "tc_<32 hex>", "type": "<event>", "created": "<ISO 8601 UTC>", "company": "<name>",
         "url": "<absolute link or null>", "data": {<event payload>}}
    X-TradeComply-Event:     <event>
    X-TradeComply-Delivery:  tc_<32 hex>  (the same on every retry of one alert: use it to drop duplicates)
    X-TradeComply-Timestamp: <unix seconds when this attempt was signed>
    X-TradeComply-Signature: v1=<base64 HMAC-SHA256(key = the signing secret as UTF-8 text,
                                                     message = "<timestamp>.<raw body bytes>")>
  To verify: recompute over the raw body exactly as received, compare in constant time, and reject if the timestamp
  is more than 300 seconds from your clock (replay protection). verify_signature() below is a reference receiver.
  Answer 2xx; 410 means "stop" (the connection is set to error); 429/5xx and other errors are retried on the event
  bus schedule (about three days); redirects are not followed.

Deadline scan (scan_tenant / scan_all, DeadlineScheduler, `python -m app.connectors.alerts scan`):
- For each tenant whose trial or subscription is active and that has at least one subscriber for
  deadline_due_soon, it reads app/dashboard.build (lien calendar: 20-day preliminary notice and mechanics lien
  recording deadline; CSLB license expirations of the company and of each sub; sub COI expirations) and, when the
  tenant saved a company profile under tenant settings "compliance_profile" (compliance_calendar's own keyword
  arguments, e.g. {"entity_type": "llc", "has_employees": true, "employee_count": 8}), the dated items of
  tradecomply.business_ops.compliance_calendar. With no profile, compliance_calendar is skipped rather than guessed.
- A deadline d days away is in reminder bucket min{t in (14, 7, 3, 1) : t >= d} for 0 <= d <= 14, and one event is
  emitted per bucket with dedupe_key "deadline:<item key>:<bucket>", so each target gets at most one 14-, 7-, 3- and
  1-day reminder per deadline (the outbound queue's unique index enforces it, even across instances), a missed day is
  caught up the next day without sending the stale buckets, and a moved date starts a new series.
- Severity: 14 days info, 7 warning, 3 and 1 critical. Toolbox talks (every 10 working days) are left out as too
  frequent for alerts; the "foreclosure if recorded on the last day" date is left out because it only applies once a
  lien is actually recorded. Both stay on the dashboard and in the calendar feed.
- DeadlineScheduler runs the scan once a day after SCAN_HOUR (6 a.m. California time) in a background thread; a
  single-use nonce per day (store.claim_nonce) keeps several instances from scanning twice. app/main.py has to start
  it (start_scheduler(platform) in the lifespan, stop_scheduler(platform) on shutdown) and include `router`;
  DEADLINE_SCAN=0 turns it off. On a Render instance that sleeps, the scan runs when it wakes; a Render Cron Job
  running `python -m app.connectors.alerts scan` against the same DATABASE_URL is the alternative.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import ClassVar

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from . import ConfigError, ConfigField, Connector, ConnectorError, NotConnected, PermanentError, TestResult
from .. import webhooks
from ..auth import Principal, office_up, platform_of
from ..billing import entitlement, require_entitled
from ..events import EVENTS, SEVERITIES, TEST_EVENT
from ..safe_http import ResponseTooLarge, SafeHTTPError, UnsafeURLError, UpstreamError, retry_after_seconds
from ..store import now_iso

log = logging.getLogger("tradecomply.connectors.alerts")

ALL_EVENTS = tuple(EVENTS)
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}          # info < warning < critical
SEVERITY_LABEL = {"info": "Info", "warning": "Warning", "critical": "Critical"}
EVENT_LABEL = {"deadline_due_soon": "Deadline reminder", "license_problem_found": "License problem",
               "sub_noncompliant": "Subcontractor compliance", "contract_generated": "Document generated",
               "injury_report_due": "Injury report due", "payroll_issue": "Payroll issue", TEST_EVENT: "Test message"}

# Slack Block Kit limits (docs.slack.dev reference/block-kit)
SLACK_HEADER_MAX = 150
SLACK_SECTION_MAX = 3000
SLACK_FIELD_MAX = 2000
SLACK_FIELDS_MAX = 10
# Teams Workflows webhook: messages are limited to about 28 KB (learn.microsoft.com/en-us/connectors/teams)
TEAMS_MAX_BYTES = 27_000
# Signed webhook
SIGNATURE_HEADER = "X-TradeComply-Signature"
TIMESTAMP_HEADER = "X-TradeComply-Timestamp"
EVENT_HEADER = "X-TradeComply-Event"
DELIVERY_HEADER = "X-TradeComply-Delivery"
SIGNATURE_VERSION = "v1"
TOLERANCE_S = 300
MIN_SECRET_LEN = 32
RESPONSE_CAP = 64 * 1024


# ---------------------------------------------------------------------------- text helpers
def _clip(text, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def human_date(iso: str | None) -> str | None:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return None
    return f"{d:%b} {d.day}, {d.year}"


def event_label(event: str) -> str:
    return EVENT_LABEL.get(event) or event.replace("_", " ").capitalize()


def _facts(payload: dict) -> list[tuple[str, str]]:
    """Label/value pairs every format shows: due date, project, sub, severity."""
    out = []
    due = human_date(payload.get("due_date"))
    if due:
        out.append(("Due", due))
    for key, label in (("project_name", "Project"), ("sub_name", "Subcontractor")):
        if payload.get(key):
            out.append((label, _clip(payload[key], 200)))
    out.append(("Severity", SEVERITY_LABEL.get(payload.get("severity") or "info", "Info")))
    return out


def delivery_id(tenant_id: str, event: str, payload: dict) -> str:
    """Stable for one alert (the event bus retries the same payload), different for different alerts."""
    raw = json.dumps([tenant_id, event, payload], sort_keys=True, separators=(",", ":"), default=str)
    return "tc_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------- Slack
def _mrkdwn(text: str, limit: int | None = None) -> str:
    """Slack mrkdwn with &, <, > escaped; cut to `limit` characters without leaving half an entity."""
    out = webhooks._slack_escape(str(text or ""))
    if limit is not None and len(out) > limit:
        out = re.sub(r"&[a-z]{0,3}$", "", out[: limit - 1]).rstrip() + "…"
    return out


def slack_message(event: str, payload: dict, app_url: str | None = None, company: str | None = None) -> dict:
    """An incoming-webhook body with Block Kit blocks and a plain fallback `text` (used in notifications)."""
    title = _clip(payload.get("title") or event_label(event), SLACK_HEADER_MAX)
    facts = _facts(payload)
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    if payload.get("summary"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": _mrkdwn(_clip(payload["summary"], 2000),
                                                                   SLACK_SECTION_MAX)}})
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*{label}*\n" + _mrkdwn(value, SLACK_FIELD_MAX - len(label) - 3)}
        for label, value in facts[:SLACK_FIELDS_MAX]]})
    link = webhooks.absolute_link(payload.get("link"), app_url)
    if link:
        safe = link.replace("|", "%7C").replace(">", "%3E")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"<{safe}|Open in TradeComply>"}})
    context = " · ".join(filter(None, [event_label(event), _mrkdwn(_clip(company, 100)) if company else None,
                                       "TradeComply"]))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    due = next((v for k, v in facts if k == "Due"), None)
    fallback = _mrkdwn(title) + (f" (due {due})" if due else "")
    return {"text": fallback, "blocks": blocks}


# ---------------------------------------------------------------------------- Microsoft Teams
TEAMS_COLOR = {"info": "default", "warning": "warning", "critical": "attention"}


def teams_message(event: str, payload: dict, app_url: str | None = None, company: str | None = None) -> dict:
    """A Teams Workflows webhook body: one Adaptive Card 1.2 attachment."""
    severity = payload.get("severity") or "info"
    body: list[dict] = [{"type": "TextBlock", "text": _clip(payload.get("title") or event_label(event), 200),
                         "weight": "bolder", "size": "medium", "wrap": True,
                         "color": TEAMS_COLOR.get(severity, "default")}]
    if payload.get("summary"):
        body.append({"type": "TextBlock", "text": _clip(payload["summary"], 2000), "wrap": True})
    body.append({"type": "FactSet", "facts": [{"title": label, "value": value} for label, value in _facts(payload)]})
    body.append({"type": "TextBlock", "text": " · ".join(filter(None, [event_label(event), _clip(company, 100)
                                                                        if company else None, "TradeComply"])),
                 "isSubtle": True, "size": "small", "wrap": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.2", "body": body}
    link = webhooks.absolute_link(payload.get("link"), app_url)
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in TradeComply", "url": link}]
    message = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                   "contentUrl": None, "content": card}]}
    if len(json.dumps(message, ensure_ascii=False).encode()) > TEAMS_MAX_BYTES and len(body) > 1:
        body[1]["text"] = _clip(body[1]["text"], 500)
    return message


# ---------------------------------------------------------------------------- signed JSON webhook
def signed_body(event: str, payload: dict, *, delivery: str, created: str, app_url: str | None = None,
                company: str | None = None) -> bytes:
    doc = {"id": delivery, "type": event, "created": created, "company": company,
           "url": webhooks.absolute_link(payload.get("link"), app_url), "data": payload}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The X-TradeComply-Signature value: v1=<base64 HMAC-SHA256 over "<timestamp>.<body>">."""
    mac = hmac.new(str(secret).encode("utf-8"), f"{int(timestamp)}.".encode() + body, hashlib.sha256).digest()
    return f"{SIGNATURE_VERSION}=" + base64.b64encode(mac).decode()


def signed_headers(secret: str, event: str, delivery: str, body: bytes, timestamp: int | None = None) -> dict:
    ts = int(time.time() if timestamp is None else timestamp)
    return {"content-type": "application/json", EVENT_HEADER: event, DELIVERY_HEADER: delivery,
            TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign(secret, ts, body)}


def verify_signature(secret: str, headers, body: bytes, now: float | None = None,
                     tolerance_s: int = TOLERANCE_S) -> bool:
    """Reference receiver check: a v1 signature over the raw body matches and the timestamp is within 5 minutes."""
    h = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    ts, sig = h.get(TIMESTAMP_HEADER.lower(), ""), h.get(SIGNATURE_HEADER.lower(), "")
    if not ts.isdigit() or not sig:
        return False
    if abs((time.time() if now is None else now) - int(ts)) > tolerance_s:
        return False
    want = sign(secret, int(ts), body)
    return any(hmac.compare_digest(want, part.strip()) for part in sig.split(","))


# ---------------------------------------------------------------------------- one delivery attempt
def _classify(kind: str, resp: httpx.Response) -> webhooks.SendResult:
    code = resp.status_code
    if 200 <= code < 300:
        return webhooks.SendResult(True, code, False, False, "delivered")
    text = resp.text.strip()[:60] if kind == "slack" else ""
    reason = text if text and all(c.isalnum() or c == "_" for c in text) else ""
    if code == 410:
        return webhooks.SendResult(False, code, False, True, "the receiver says this webhook is gone")
    if code in (408, 429) or code >= 500:
        return webhooks.SendResult(False, code, True, False, reason or "the receiver is busy or failing; will retry",
                                   retry_after_seconds(resp))
    if 300 <= code < 400:
        return webhooks.SendResult(False, code, False, False, "the URL redirects; save the final URL instead")
    if kind == "slack":
        return webhooks.SendResult(False, code, False, reason in webhooks.SLACK_DISABLED_CODES,
                                   reason or "Slack refused the message")
    if kind == "teams":
        return webhooks.SendResult(False, code, False, False, "Teams refused the message")
    return webhooks.SendResult(False, code, True, False, "the receiver refused the message; will retry")


def send(http, kind: str, url: str, event: str, payload: dict, *, msg_id: str, created: str,
         signing_secret: str | None = None, app_url: str | None = None, company: str | None = None,
         timeout: float = 10.0) -> webhooks.SendResult:
    """One attempt with a SafeHTTP client, formatted as this module's Slack blocks / Teams card / signed JSON. The
    same signature and result as app.webhooks.send. Never raises for network or HTTP problems."""
    headers = {"content-type": "application/json"}
    if kind == "slack":
        content = json.dumps(slack_message(event, payload, app_url, company), ensure_ascii=False).encode()
    elif kind == "teams":
        content = json.dumps(teams_message(event, payload, app_url, company), ensure_ascii=False).encode()
    elif kind == "generic":
        if not signing_secret:
            return webhooks.SendResult(False, None, False, False, "no signing secret is saved")
        content = signed_body(event, payload, delivery=msg_id, created=created, app_url=app_url, company=company)
        headers = signed_headers(signing_secret, event, msg_id, content)
    else:
        return webhooks.SendResult(False, None, False, False, f"unknown channel {kind!r}")
    try:
        resp = http.post(url, content=content, headers=headers, retries=0, follow_redirects=False, timeout=timeout,
                         max_bytes=RESPONSE_CAP)
    except UnsafeURLError as e:
        return webhooks.SendResult(False, None, False, False, str(e))
    except ResponseTooLarge:
        return webhooks.SendResult(False, None, False, False, "the receiver answered with an oversized response")
    except (UpstreamError, SafeHTTPError) as e:
        return webhooks.SendResult(False, None, True, False, str(e))
    return _classify(kind, resp)


# ---------------------------------------------------------------------------- filters (shared with twilio_sms)
def clean_event_list(value) -> str | None:
    """'' / None / '*' = every event; otherwise a comma list of known event names. Raises ConfigError."""
    names = [n.strip() for n in re.split(r"[,\s]+", str(value or "")) if n.strip()]
    if not names or "*" in names:
        return None
    bad = [n for n in names if n not in EVENTS]
    if bad:
        raise ConfigError(f"Unknown event(s): {', '.join(bad[:5])}. Known: {', '.join(EVENTS)}", "events")
    return ",".join(sorted(set(names)))


def wants(config: dict, event: str, payload: dict) -> bool:
    """Whether a channel with these settings should send this alert."""
    allowed = [n for n in str(config.get("events") or "").split(",") if n]
    if allowed and event not in allowed:
        return False
    floor = SEVERITY_RANK.get(config.get("min_severity") or "info", 0)
    return SEVERITY_RANK.get(payload.get("severity") or "info", 0) >= floor


def severity_field(default: str, extra_help: str = "") -> ConfigField:
    return ConfigField("min_severity", "Send alerts at or above", type="select", default=default,
                       options=(("info", "Info (everything)"), ("warning", "Warning and critical"),
                                ("critical", "Critical only")),
                       help=("Deadline reminders are Info 14 days ahead, Warning at 7 days and Critical at 3 and 1 "
                             "days. " + extra_help).strip())


EVENTS_FIELD = ConfigField(
    "events", "Only these alerts (optional)", pattern=r"[a-z_*, ]{0,500}",
    help="Leave blank for all alerts, or list event names separated by commas: " + ", ".join(EVENTS) + ".")


def company_name(platform, tenant_id: str) -> str | None:
    try:
        return (platform.store.get_tenant(tenant_id) or {}).get("company_name")
    except Exception:      # noqa: BLE001 - a name in the message is nice to have, never required
        return None


# ---------------------------------------------------------------------------- the channel connectors
class _AlertChannel(Connector):
    """Shared behavior; not registered itself (no provider id)."""
    kind: ClassVar[str] = ""
    channel: ClassVar[str] = ""
    category = "messaging"
    subscribes = ALL_EVENTS

    @classmethod
    def clean_config(cls, values, existing_config=None, existing_secrets=None):
        config, secret = super().clean_config(values, existing_config, existing_secrets)
        config["events"] = clean_event_list(config.get("events"))
        if secret.get("webhook_url"):
            cls.check_url_shape(secret["webhook_url"])
        return config, secret

    @classmethod
    def check_url_shape(cls, url: str) -> None:
        pass

    def _secret(self, name: str, what: str) -> str:
        value = self.secrets().get(name)
        if not value:
            raise NotConnected(f"Save the {what} first.")
        return value

    def deliver(self, event: str, payload: dict) -> webhooks.SendResult:
        url = self._secret("webhook_url", f"{self.channel} webhook URL")
        signing = self._secret("signing_secret", "signing secret") if self.kind == "generic" else None
        platform = self.ctx.platform
        with self.ctx.http(retries=0) as http:
            try:
                webhooks.validate_url(url, self.kind, http)
            except ValueError as e:
                return webhooks.SendResult(False, None, False, False, str(e))
            return send(http, self.kind, url, event, payload, msg_id=delivery_id(self.ctx.tenant_id, event, payload),
                        created=now_iso(), signing_secret=signing, app_url=platform.settings.app_url,
                        company=company_name(platform, self.ctx.tenant_id))

    def test(self) -> TestResult:
        payload = {"title": "Test message from TradeComply", "severity": "info", "link": "/#/integrations",
                   "summary": f"If you can read this, TradeComply alerts reach this {self.channel} destination."}
        r = self.deliver(TEST_EVENT, payload)
        if r.ok:
            return TestResult(True, f"Sent a test message to {self.channel}.", details={"status": r.status})
        return TestResult(False, f"{self.channel} didn't accept the test message: {r.summary}",
                          details={"status": r.status})

    def handle_event(self, event: str, payload: dict) -> None:
        if not wants(self.config(), event, payload):
            return
        r = self.deliver(event, payload)
        if r.ok:
            return
        if r.disable:
            self.save(status="error", last_error=f"{self.channel} turned this webhook off ({r.summary}). "
                                                 "Enter a new webhook URL and connect again.")
            raise PermanentError(r.summary)
        if r.retry:
            raise ConnectorError(r.summary)
        raise PermanentError(r.summary)


class SlackAlerts(_AlertChannel):
    provider = "slack_alerts"
    display_name = "Slack alerts (Block Kit)"
    catalog_id = "slack"
    description = ("Posts TradeComply alerts to one Slack channel through an incoming webhook, formatted with Block "
                   "Kit: title, summary, due date, project and a link back to TradeComply.")
    docs_url = "https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/"
    kind, channel = "slack", "Slack"
    config_fields = (
        ConfigField("webhook_url", "Slack webhook URL", type="url", secret=True, required=True, max_length=500,
                    placeholder="https://hooks.slack.com/services/…",
                    help="In your Slack app's settings open Incoming Webhooks, add one for the channel and paste its "
                         "URL. It is stored encrypted and never shown again."),
        severity_field("info"),
        EVENTS_FIELD,
    )
    unconfirmed = (
        "Slack's docs don't say how long a Block Kit message may be in total; each block is kept within its "
        "documented limit (header 150, section 3000, 10 fields of 2000 characters).",
    )

    @classmethod
    def check_url_shape(cls, url: str) -> None:
        try:
            u = httpx.URL(url)
        except (httpx.InvalidURL, TypeError, ValueError):
            raise ConfigError("Slack webhook URL: not a valid URL", "webhook_url") from None
        if (u.host or "").lower() not in webhooks.SLACK_HOSTS or not u.path.startswith("/services/"):
            raise ConfigError("Slack webhook URL: it starts with https://hooks.slack.com/services/", "webhook_url")


class TeamsAlerts(_AlertChannel):
    provider = "teams_alerts"
    display_name = "Microsoft Teams alerts (Adaptive Card)"
    catalog_id = "microsoft-teams"
    description = ("Posts TradeComply alerts to a Teams channel or chat through a Workflows webhook (\"When a Teams "
                   "webhook request is received\") as an Adaptive Card with a button back to TradeComply.")
    docs_url = "https://learn.microsoft.com/en-us/connectors/teams/"
    kind, channel = "teams", "Teams"
    config_fields = (
        ConfigField("webhook_url", "Workflows webhook URL", type="url", secret=True, required=True, max_length=1000,
                    placeholder="https://…",
                    help="In Teams open the channel's ••• menu > Workflows > \"Send webhook alerts to a channel\" and "
                         "paste the URL it gives you. Add a co-owner to the workflow so it keeps working if its "
                         "creator leaves."),
        severity_field("info"),
        EVENTS_FIELD,
    )
    unconfirmed = (
        "The HTTP status a Workflows webhook returns when its flow was deleted or turned off is not documented on "
        "the connector page; any 4xx is treated as permanent for that alert and 410 turns the connection off.",
    )


class SignedWebhookAlerts(_AlertChannel):
    provider = "signed_webhook"
    display_name = "Signed alert webhook (Workato, Zapier, ServiceNow)"
    catalog_id = "generic-webhooks"
    description = ("Posts each alert as JSON to your HTTPS endpoint with an X-TradeComply-Signature (HMAC-SHA256 "
                   "over the timestamp and raw body) and X-TradeComply-Timestamp header, so Workato, Zapier, "
                   "ServiceNow or your own code can check it came from TradeComply and reject replays.")
    docs_url = "https://docs.workato.com/en/formulas/other-formulas"
    kind, channel = "generic", "your endpoint"
    config_fields = (
        ConfigField("webhook_url", "Endpoint URL", type="url", secret=True, required=True, max_length=1000,
                    placeholder="https://your-system.example/webhooks/tradecomply",
                    help="An HTTPS URL that accepts POST with a JSON body. Stored encrypted (catch-hook URLs often "
                         "work as a password)."),
        ConfigField("signing_secret", "Signing secret", type="password", secret=True, required=True,
                    max_length=200, pattern=r"[\x21-\x7e]{32,200}",
                    help="Make up a random secret of at least 32 characters (no spaces), paste it here and in your "
                         "receiver. TradeComply signs v1=base64(HMAC-SHA256(secret, timestamp + '.' + raw body)); "
                         "reject messages whose X-TradeComply-Timestamp is more than 5 minutes old."),
        severity_field("info"),
        EVENTS_FIELD,
    )
    unconfirmed = (
        "Receiver recipes are from Workato's formula and WEL docs, Zapier's integration-builder docs and ServiceNow's "
        "GlideCertificateEncryption reference; a no-code Zap (Catch Raw Hook + Code by Zapier), ServiceNow's "
        "Scripted REST request object and whether Workato's encode_base64 adds line breaks were not checked.",
    )


# ---------------------------------------------------------------------------- deadline scan
REMINDER_DAYS = (14, 7, 3, 1)
BUCKET_SEVERITY = {14: "info", 7: "warning", 3: "critical", 1: "critical"}
SCAN_KINDS = ("prelim_notice", "lien_recording", "license_expiry", "coi_expiry")
SCAN_HOUR = 6                      # California time
PROFILE_KEY = "compliance_profile"
CA_TZ_NAME = "America/Los_Angeles"


def reminder_bucket(days_left: int) -> int | None:
    """The reminder a deadline `days_left` away belongs to (None outside 0..14)."""
    if days_left is None or days_left < 0 or days_left > max(REMINDER_DAYS):
        return None
    return min(t for t in REMINDER_DAYS if t >= days_left)


def _when(days: int) -> str:
    return "due today" if days == 0 else "due tomorrow" if days == 1 else f"due in {days} days"


def _calendar_kwargs() -> set[str]:
    from tradecomply.business_ops import compliance_calendar
    return {n for n in inspect.signature(compliance_calendar).parameters if n not in ("year", "as_of")}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:80]


def calendar_items(tenant: dict, today: date, company_license: dict | None) -> tuple[list[dict], list[str]]:
    """Dated compliance_calendar obligations within the reminder window, from the tenant's saved profile."""
    from tradecomply.business_ops import compliance_calendar
    profile = (tenant.get("settings") or {}).get(PROFILE_KEY)
    if not isinstance(profile, dict):
        return [], [f"compliance_calendar skipped: no company profile saved (tenant settings '{PROFILE_KEY}')."]
    allowed = _calendar_kwargs()
    kwargs = {k: v for k, v in profile.items() if k in allowed}
    ignored = sorted(set(profile) - allowed)
    notes = [f"compliance_profile keys ignored: {', '.join(ignored[:10])}"] if ignored else []
    lic_exp = (company_license or {}).get("expiration_date")
    items: dict[str, dict] = {}            # by key: two calendar years can list the same obligation
    end = today + timedelta(days=max(REMINDER_DAYS))
    for year in sorted({today.year, end.year}):
        try:
            result = compliance_calendar(year, as_of=today.isoformat(), **kwargs)
        except (TypeError, ValueError) as e:
            return [], [f"compliance_calendar skipped: the saved company profile was refused ({str(e)[:200]})."]
        for ob in result.items:
            if not ob.due_date:
                continue
            due = date.fromisoformat(ob.due_date)
            if not today <= due <= end:
                continue
            if ob.category == "license" and lic_exp and ob.due_date == str(lic_exp)[:10]:
                continue          # the dashboard already reminds about the CSLB license expiration
            key = f"calendar:{_slug(ob.title)}:{ob.due_date}"
            items.setdefault(key, {"key": key, "kind": "compliance_calendar", "title": ob.title,
                                   "date": ob.due_date, "days_left": (due - today).days,
                                   "detail": " ".join(filter(None, [ob.form and f"Form: {ob.form}.",
                                                                    ob.agency and f"Agency: {ob.agency}.", ob.how])),
                                   "project_id": None, "project_name": None, "sub_id": None, "sub_name": None,
                                   "verified": bool(ob.verified)})
    return list(items.values()), notes


def due_reminders(store, tenant: dict, today: date | None = None) -> tuple[list[dict], list[str]]:
    """The deadline_due_soon events this tenant is due for today: [{"payload", "dedupe_key"}], plus notes."""
    from tradecomply import logic

    from .. import dashboard
    today = today or logic.today_ca()
    board = dashboard.build(store, tenant, today=today, include_past=True)
    items = [i for i in board["items"] if i["kind"] in SCAN_KINDS]
    extra, notes = calendar_items(tenant, today, (board.get("company") or {}).get("license"))
    out = []
    for it in items + extra:
        days = (date.fromisoformat(it["date"]) - today).days
        bucket = reminder_bucket(days)
        if bucket is None:
            continue
        where = " / ".join(filter(None, [it.get("project_name"), it.get("sub_name")]))
        title = _clip(f"{it['title']}: {_when(days)}" + (f" ({where})" if where else ""), 200)
        verified = ("Checked against the official source." if it.get("verified") else
                    "Not yet verified against the official source.")
        summary = _clip(" ".join(filter(None, [it.get("detail"), f"Date: {human_date(it['date'])}.", verified,
                                                "Not legal advice."])), 2000)
        link = (f"/#/project/{it['project_id']}" if it.get("project_id") else
                "/#/subs" if it.get("sub_id") else "/#/dashboard")
        payload = {"title": title, "summary": summary, "severity": BUCKET_SEVERITY[bucket], "link": link,
                   "due_date": it["date"], "kind": it["kind"], "days_left": days, "reminder_days": bucket,
                   "project_id": it.get("project_id"), "project_name": it.get("project_name"),
                   "sub_id": it.get("sub_id"), "sub_name": it.get("sub_name"), "source": "deadline_scan"}
        out.append({"payload": payload, "dedupe_key": f"deadline:{it['key']}:{bucket}"[:200]})
    return out, notes


def scan_tenant(platform, tenant_id: str, today: date | None = None, *, force: bool = False) -> dict:
    """Emit today's deadline reminders for one tenant. Safe to repeat: dedupe keys stop duplicates."""
    store = platform.store
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return {"tenant_id": tenant_id, "skipped": "no such tenant", "emitted": 0}
    if not entitlement(tenant)["ok"] and not force:
        return {"tenant_id": tenant_id, "skipped": "no active trial or subscription", "emitted": 0}
    if not platform.events.targets_for(tenant_id, "deadline_due_soon"):
        return {"tenant_id": tenant_id, "skipped": "nothing subscribes to deadline_due_soon", "emitted": 0}
    reminders, notes = due_reminders(store, tenant, today)
    emitted = 0
    for r in reminders:
        emitted += len(platform.events.emit(tenant_id, "deadline_due_soon", r["payload"], dedupe_key=r["dedupe_key"]))
    return {"tenant_id": tenant_id, "due": len(reminders), "emitted": emitted, "notes": notes}


def _tenant_ids(store) -> list[str]:
    """OPERATOR ONLY: every tenant id (the scan's own read; the store has no listing method for this)."""
    with store.connect() as con:
        return [r[0] for r in con.execute("select id from tenants order by id").fetchall()]


def scan_all(platform, today: date | None = None) -> dict:
    """OPERATOR ONLY: scan every tenant. One tenant's failure is logged and doesn't stop the others."""
    totals = {"tenants": 0, "scanned": 0, "emitted": 0, "failed": 0}
    for tid in _tenant_ids(platform.store):
        totals["tenants"] += 1
        try:
            r = scan_tenant(platform, tid, today)
        except Exception as e:      # noqa: BLE001 - e.g. bad data on one tenant; logged without its contents
            totals["failed"] += 1
            log.warning("deadline scan failed for tenant %s (%s)", tid, type(e).__name__)
            continue
        if "skipped" not in r:
            totals["scanned"] += 1
        totals["emitted"] += r.get("emitted", 0)
    log.info("deadline scan: %(tenants)d tenants, %(scanned)d scanned, %(emitted)d reminders queued, "
             "%(failed)d failed", totals)
    return totals


class DeadlineScheduler:
    """Runs scan_all once a day after SCAN_HOUR California time. Cheap when idle: it wakes every `check_s`."""

    def __init__(self, platform, hour: int = SCAN_HOUR, check_s: float = 900.0):
        self.platform, self.hour, self.check_s = platform, hour, check_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_day: date | None = None

    def due(self, now: datetime | None = None) -> date | None:
        from zoneinfo import ZoneInfo
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(CA_TZ_NAME))
        if local.hour < self.hour or self.last_day == local.date():
            return None
        return local.date()

    def run_due(self, now: datetime | None = None) -> dict | None:
        """Scan if today's scan hasn't run here or on another instance. Returns the totals or None."""
        day = self.due(now)
        if day is None:
            return None
        self.last_day = day
        expires = int(time.time()) + 3 * 86400
        if not self.platform.store.claim_nonce(f"deadline-scan:{day.isoformat()}", expires):
            return None                       # another instance (or an earlier run here) took today's scan
        return scan_all(self.platform, day)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:      # noqa: BLE001 - e.g. the database is briefly down; try at the next check
                log.exception("deadline scan round failed")
                self.last_day = None
            self._stop.wait(self.check_s)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tc-deadline-scan", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None


def start_scheduler(platform) -> DeadlineScheduler | None:
    """For app/main.py's lifespan. Starts a thread only (no database call on the event loop)."""
    if str((platform.env.get("DEADLINE_SCAN") or "1")).strip().lower() in ("0", "false", "no", "off"):
        return None
    sched = getattr(platform, "deadline_scheduler", None) or DeadlineScheduler(platform)
    platform.deadline_scheduler = sched
    sched.start()
    return sched


def stop_scheduler(platform) -> None:
    sched = getattr(platform, "deadline_scheduler", None)
    if sched:
        sched.stop()


# ---------------------------------------------------------------------------- HTTP API
router = APIRouter(prefix="/api/integrations/deadline-reminders", tags=["integrations"])


@router.get("/preview")
def preview(request: Request, p: Principal = Depends(office_up)):
    """The deadline reminders this company is due for today (nothing is sent)."""
    platform = platform_of(request)
    tenant = platform.store.get_tenant(p.tenant_id)
    reminders, notes = due_reminders(platform.store, tenant)
    return {"reminder_days": list(REMINDER_DAYS), "subscribers": platform.events.targets_for(p.tenant_id,
                                                                                            "deadline_due_soon"),
            "reminders": [{**r["payload"], "dedupe_key": r["dedupe_key"]} for r in reminders], "notes": notes}


@router.post("/scan")
def scan_now(request: Request, p: Principal = Depends(office_up)):
    """Queue today's deadline reminders for this company now (the daily scan does the same; nothing is sent twice)."""
    platform = platform_of(request)
    require_entitled(platform, p)
    limiter, key = platform.integration_test_limiter, f"tenant|{p.tenant_id}"
    wait = limiter.retry_after(key)
    if wait:
        raise HTTPException(429, "Too many checks in a short time. Try again in a few minutes.",
                            headers={"Retry-After": str(wait)})
    limiter.hit(key)
    result = scan_tenant(platform, p.tenant_id)
    try:
        platform.store.audit(p.tenant_id, p.label, "integrations.deadline_scan",
                             {"emitted": result.get("emitted", 0)}, None, ok=True, source="integrations")
    except Exception:      # noqa: BLE001
        log.exception("audit write failed")
    return result


# ---------------------------------------------------------------------------- CLI (Render Cron Job)
def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "scan" or len(args) > 2:
        print("usage: python -m app.connectors.alerts scan [YYYY-MM-DD]")
        return 2
    day = date.fromisoformat(args[1]) if len(args) == 2 else None
    from ..config import Settings
    from ..main import create_app
    app = create_app(Settings.from_env(events_background=False))
    platform = app.state.platform
    try:
        from .. import connectors
        connectors.discover()
        print(json.dumps(scan_all(platform, day)))
        platform.events.drain()          # deliver what the scan queued before exiting
    finally:
        platform.store.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
