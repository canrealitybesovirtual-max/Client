"""Outbound webhooks: message formats, signing and one delivery attempt. Used by the event bus (app/events.py) and by
the "send test" route. Retries are the event queue's job, so a send here is a single attempt that says whether a
retry makes sense.

Formats, each from the vendor's documentation:
- slack: a Slack incoming webhook. POST JSON {"text": "..."} to the https://hooks.slack.com/services/... URL; success
  is HTTP 200 with the body "ok"; errors come back as 400/403/404/410 with a short code (invalid_payload,
  action_prohibited, no_service, channel_is_archived, ...). The URL contains a secret. Text uses Slack mrkdwn:
  *bold*, <url|label> links, and &, <, > escaped as &amp; &lt; &gt;.
  (docs.slack.dev: "Sending messages using incoming webhooks" and "Formatting message text".)
- teams: a Microsoft Teams "Workflows" webhook (the "When a Teams webhook request is received" trigger; Office 365
  connector webhooks are being retired). POST {"type": "message", "attachments": [{"contentType":
  "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content": <Adaptive Card>}]}. The URL is generated
  by the flow (hosts vary, e.g. *.logic.azure.com) and works as a credential.
  (learn.microsoft.com/en-us/connectors/teams, "Microsoft Teams Webhook".)
- generic: Standard Webhooks (standardwebhooks.com spec). Body {"type": <event>, "timestamp": <ISO 8601>, "data":
  <payload>}; headers webhook-id (the same on every retry of one message), webhook-timestamp (unix seconds of this
  attempt) and webhook-signature "v1,<base64 HMAC-SHA256 of '{id}.{timestamp}.{body}'>" with the secret
  "whsec_<base64>" (24-64 random bytes; 32 here). 2xx = delivered; 410 = the receiver wants no more (the webhook is
  disabled); 429/502/504 and Retry-After = slow down; redirects are not followed.

Webhook URLs and signing secrets are stored encrypted (SecretBox, webhook_context(tenant_id)); only a hint
(scheme and host) is ever shown or logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .safe_http import ResponseTooLarge, SafeHTTPError, UnsafeURLError, UpstreamError, retry_after_seconds

SLACK_HOSTS = ("hooks.slack.com",)
SIGNING_SECRET_PREFIX = "whsec_"
TOLERANCE_S = 300            # Standard Webhooks receivers reject timestamps more than 5 minutes off
SLACK_DISABLED_CODES = ("no_service", "no_active_hooks", "channel_is_archived", "team_disabled")


@dataclass
class SendResult:
    ok: bool
    status: int | None          # HTTP status, None if no response
    retry: bool                 # worth retrying later
    disable: bool               # the receiver said it wants no more (410, or Slack says the hook is gone)
    message: str                # safe to show: no URL path, no secret
    retry_after: float | None = None

    @property
    def summary(self) -> str:
        return f"{self.status} {self.message}".strip() if self.status else self.message


# ---------------------------------------------------------------------------- secrets and signatures
def new_signing_secret() -> str:
    return SIGNING_SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()


def _secret_bytes(secret: str) -> bytes:
    if not secret.startswith(SIGNING_SECRET_PREFIX):
        raise ValueError("signing secret must start with whsec_")
    return base64.b64decode(secret[len(SIGNING_SECRET_PREFIX):])


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """The webhook-signature value for one attempt (Standard Webhooks, symmetric v1)."""
    mac = hmac.new(_secret_bytes(secret), f"{msg_id}.{int(timestamp)}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(secret: str, headers, body: bytes, now: float | None = None) -> bool:
    """Receiver-side check (for tests, and the docs we give customers): any v1 signature matches and the timestamp
    is within 5 minutes."""
    msg_id, ts, sigs = headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature")
    if not (msg_id and ts and sigs and ts.isdigit()):
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > TOLERANCE_S:
        return False
    want = sign(secret, msg_id, int(ts), body).split(",", 1)[1]
    return any(hmac.compare_digest(want, s.split(",", 1)[1]) for s in sigs.split() if s.startswith("v1,"))


# ---------------------------------------------------------------------------- URLs
def url_hint(url: str) -> str:
    """scheme://host/... : enough to recognize a webhook, never its secret path or query."""
    try:
        u = httpx.URL(url)
        return f"{u.scheme}://{u.host}/…" if u.host else "(hidden)"
    except (httpx.InvalidURL, TypeError, ValueError):
        return "(hidden)"


def validate_url(url: str, kind: str, http) -> str:
    """Raise ValueError with a plain message unless the URL suits this kind and passes the SSRF checks (`http` is
    a SafeHTTP, whose resolver is used)."""
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise ValueError("Enter the webhook URL (up to 2000 characters)")
    try:
        u = http.check(url)
    except UnsafeURLError as e:
        raise ValueError(str(e)) from None
    if kind == "slack" and (u.host or "").lower() not in SLACK_HOSTS:
        raise ValueError("A Slack webhook URL starts with https://hooks.slack.com/services/ (create one under "
                         "Incoming Webhooks in your Slack app's settings)")
    return url


# ---------------------------------------------------------------------------- message bodies
def absolute_link(link: str | None, app_url: str | None) -> str | None:
    """Payload links are app paths ("/#/projects/3"); they become absolute only with a known APP_URL, so alerts
    never point anywhere but this app."""
    if not link or not app_url or not str(link).startswith("/") or str(link).startswith("//"):
        return None
    return app_url.rstrip("/") + quote(str(link), safe="/#?&=%-._~:")


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_body(event: str, payload: dict, app_url: str | None = None) -> dict:
    lines = [f"*{_slack_escape(payload.get('title') or event)}*"]
    if payload.get("summary"):
        lines.append(_slack_escape(payload["summary"]))
    if payload.get("due_date"):
        lines.append(f"Due: {_slack_escape(payload['due_date'])}")
    link = absolute_link(payload.get("link"), app_url)
    if link:
        lines.append(f"<{link.replace('|', '%7C').replace('>', '%3E')}|Open in TradeComply>")
    return {"text": "\n".join(lines)}


def teams_body(event: str, payload: dict, app_url: str | None = None) -> dict:
    body = [{"type": "TextBlock", "text": payload.get("title") or event, "weight": "bolder", "size": "medium",
             "wrap": True}]
    if payload.get("summary"):
        body.append({"type": "TextBlock", "text": payload["summary"], "wrap": True})
    if payload.get("due_date"):
        body.append({"type": "TextBlock", "text": f"Due: {payload['due_date']}", "wrap": True})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.2", "body": body}
    link = absolute_link(payload.get("link"), app_url)
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in TradeComply", "url": link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def generic_body(event: str, payload: dict, created: str) -> bytes:
    return json.dumps({"type": event, "timestamp": created, "data": payload}, separators=(",", ":"),
                      ensure_ascii=False, sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------- one attempt
def _classify(kind: str, resp: httpx.Response) -> SendResult:
    code = resp.status_code
    if 200 <= code < 300:
        return SendResult(True, code, False, False, "delivered")
    text = resp.text.strip()[:60] if kind == "slack" else ""
    reason = text if text and all(c.isalnum() or c == "_" for c in text) else ""
    if code == 410:
        return SendResult(False, code, False, True, "the receiver says this webhook is gone; it has been turned off")
    if code == 429 or code == 408 or code >= 500:
        return SendResult(False, code, True, False, reason or "the receiver is busy or failing; will retry",
                          retry_after_seconds(resp))
    if 300 <= code < 400:
        return SendResult(False, code, False, False, "the URL redirects; save the final URL instead")
    if kind == "slack":
        return SendResult(False, code, False, reason in SLACK_DISABLED_CODES, reason or "Slack refused the message")
    if kind == "teams":
        return SendResult(False, code, False, False, "Teams refused the message")
    return SendResult(False, code, True, False, "the receiver refused the message; will retry")


def send(http, kind: str, url: str, event: str, payload: dict, *, msg_id: str, created: str,
         signing_secret: str | None = None, app_url: str | None = None, timeout: float = 10.0) -> SendResult:
    """One delivery attempt with a SafeHTTP client. Never raises for network or HTTP problems."""
    headers = {"content-type": "application/json"}
    if kind == "slack":
        content = json.dumps(slack_body(event, payload, app_url), ensure_ascii=False).encode()
    elif kind == "teams":
        content = json.dumps(teams_body(event, payload, app_url), ensure_ascii=False).encode()
    elif kind == "generic":
        content = generic_body(event, payload, created)
        ts = int(time.time())
        headers.update({"webhook-id": msg_id, "webhook-timestamp": str(ts)})
        if signing_secret:
            headers["webhook-signature"] = sign(signing_secret, msg_id, ts, content)
    else:
        return SendResult(False, None, False, False, f"unknown webhook kind {kind!r}")
    try:
        resp = http.post(url, content=content, headers=headers, retries=0, follow_redirects=False, timeout=timeout,
                         max_bytes=64 * 1024)
    except UnsafeURLError as e:
        return SendResult(False, None, False, False, str(e))
    except ResponseTooLarge:
        return SendResult(False, None, False, False, "the receiver answered with an oversized response")
    except UpstreamError as e:
        return SendResult(False, None, True, False, str(e))
    except SafeHTTPError as e:
        return SendResult(False, None, True, False, str(e))
    return _classify(kind, resp)
