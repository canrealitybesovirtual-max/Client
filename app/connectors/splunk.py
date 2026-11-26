"""Splunk HTTP Event Collector (HEC): send a company's TradeComply activity log, its MCP call log and its compliance
alerts to the company's own Splunk (Splunk Cloud Platform or Splunk Enterprise), plus a CSV/JSON download of the
same logs for owners.

What is sent (one HEC event per row, JSON event format, batched):

    activity log   every row of the tenant's `audit` table (what /api/audit shows): tool runs from the app and the
                   API, MCP tool calls (source "mcp"), integration and SSO changes. sourcetype tradecomply:audit,
                   source tradecomply:activity
    MCP call log   every row of `mcp_audit_log` (tool, status, duration, API key id, SHA-256 of the arguments, client
                   IP). sourcetype tradecomply:mcp_audit, source tradecomply:mcp
    alerts         the platform events (deadline_due_soon, sub_noncompliant, ...) through the event bus, when "Also
                   send compliance alerts" is on. sourcetype tradecomply:alert, source tradecomply:events
    test           "Save and connect" / "Test connection" send ONE small event, sourcetype tradecomply:test. HEC has
                   no read-only call that checks a token (the health endpoint takes the token only in the query
                   string, which leaks into proxy logs), so a test event is the check.

The sourcetype and source names are TradeComply's own naming, not Splunk-defined. Every event carries `tc_id`
("activity:<id>", "mcp:<id>", "alert:<sha256>"): delivery is at least once (a batch whose answer is lost is sent
again), so de-duplicate in searches with `| dedup tc_id`.

Per-tenant settings (Settings > Integrations > Splunk), from the customer's Splunk admin; no operator env vars:
    hec_url        https://http-inputs-<stack>.splunkcloud.com (Splunk Cloud Platform, port 443; GCP, Azure and
                   AWS GovCloud stacks use "http-inputs." instead of "http-inputs-"), https://<host>:8088 (Splunk
                   Enterprise default port and Splunk Cloud free trials). The path /services/collector/event is
                   added here; a URL that already ends in it is accepted. Ports 443, 8088 and 8443 only; the host
                   must be on the public internet (SafeHTTP refuses private addresses), so an on-premises HEC must
                   be reachable from the internet over HTTPS.
    hec_token      the HEC token (encrypted at rest; sent only as `Authorization: Splunk <token>`, never in a URL)
    index          optional; must be in the token's allowed indexes. Blank = the token's default index.
    send_history   also send rows written before the connection (default yes)
    send_alerts    also send the compliance alerts (default yes)
Turn indexer acknowledgment OFF on the token: TradeComply does not poll /services/collector/ack, and Splunk Cloud
Platform supports acknowledgment only for Amazon Data Firehose.

Delivery: HecExporter (a background thread started by start_exporter(platform) in app/main.py's lifespan;
SPLUNK_EXPORT=0 turns it off, SPLUNK_EXPORT_INTERVAL sets the seconds between rounds, default 60) or
`python -m app.connectors.splunk export` (a Render Cron Job against the same DATABASE_URL). Each round, for each
connected and entitled tenant: rows after the tenant's cursor (kept inside the connection's encrypted secrets, which
only the server writes, so nobody can move it with a settings save to hide rows), created at least SETTLE_S seconds
ago, oldest first, at most MAX_BATCH_EVENTS events or MAX_BATCH_BYTES per POST, at most MAX_BATCHES_PER_ROUND
POSTs. The cursor moves only after Splunk answers {"text":"Success","code":0}. SafeHTTP retries 429/5xx twice with
backoff; a busy Splunk (codes 8, 9, 18-20, 23, 26, 27) keeps the rows for the next round with an exponential pause
(1 minute doubling to 1 hour); a refused token, index, URL or acknowledgment setting (codes 1-4, 7, 10, 11, 14, 16,
21, 22, HTTP 401/403/404) sets the connection to `error` with the reason and stops until the customer fixes it and
tests again; a batch Splunk calls malformed (codes 5, 6, 12, 13, 15, HTTP 413) is resent one event at a time and an
event refused on its own is skipped (logged by id) so one bad row can't stop the export. One instance per tenant per
round (store.claim_nonce). Tenants whose trial or subscription ended are skipped until they subscribe again; their
rows stay and are sent then.

HTTP API (`router`; app/main.py must include it before the catch-all MCP mount at "/", as for the Procore and
Docusign routers):
    GET  /api/integrations/splunk/export    staff: cursor, backlog, last send, last error
    POST /api/integrations/splunk/export    staff, subscribed: run one export round for this company now
    GET  /api/audit-export?log=activity|mcp&format=csv|json&start=&end=    OWNER only: download the log for
         [start, end) (YYYY-MM-DD or ISO timestamps, UTC), at most MAX_DOWNLOAD_ROWS rows, oldest first. CSV cells
         that start with = + - @ tab or CR are prefixed with ' (OWASP CSV injection guidance), since the log holds
         text users typed.
The generic /api/integrations/splunk routes (config, connect, test, disconnect) work as for every connector.

Sources (read 2026-11-26 through Context7's index of the official help.splunk.com pages; direct fetches of
help.splunk.com are refused by this build environment's network, so re-check on the live pages):
- Format events for HTTP Event Collector (splunk-enterprise/get-data-in/collect-http-event-data/
  format-events-for-http-event-collector): metadata keys time (UNIX `<sec>.<ms>`), host, source, sourcetype, index
  (must be in the token's allowed indexes), fields (flat, index time, /collector/event only); `event` holds a string,
  number or object; batches are event objects stacked one after another ("Both concatenated JSON objects and JSON
  arrays ... are accepted"); token in the header "Authorization: Splunk <hec_token>" (Basic auth with the token as
  the password and ?token= also exist; query-string auth must be enabled per token).
- Troubleshoot HTTP Event Collector, "Possible error codes": the full table used in HEC_CODES below (0 Success 200
  ... 27 HEC ACK channel at capacity 429).
- REST API reference, input endpoints: success body {"text":"Success","code":0}; services/collector/health and its
  ?token= form; services/collector/event/1.0.
- Set up and use HTTP Event Collector in Splunk Web, "Send data to HTTP Event Collector on Splunk Cloud Platform":
  <protocol>://http-inputs-<host>.splunkcloud.com:<port>/<endpoint>; "http-inputs." on GCP, Azure and AWS
  GovCloud; port 443 by default on Splunk Cloud Platform, 8088 on free trials.
- About HTTP Event Collector indexer acknowledgment: X-Splunk-Request-Channel, {"ackID":..}, /services/collector/ack,
  and "Amazon Data Firehose is the only supported sending client for HEC tokens with indexer acknowledgement
  activated in the Splunk Cloud Platform".
- inputs.conf reference: maxEventSize, default 5MB per event.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import secrets as _secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import ConfigError, ConfigField, Connector, ConnectorContext, ConnectorError, NotConnected, PermanentError, \
    TestResult
from ..auth import Principal, RateLimiter, office_up, owner_only, platform_of
from ..billing import entitlement, require_entitled
from ..events import EVENTS
from ..safe_http import ResponseTooLarge, SafeHTTPError, UnsafeURLError, check_url, retry_after_seconds
from ..store import now_iso, to_store_time

log = logging.getLogger("tradecomply.connectors.splunk")

PROVIDER = "splunk"
EVENT_PATH = "/services/collector/event"
HEC_PORTS = (443, 8088, 8443)                       # 443 Splunk Cloud Platform, 8088 Enterprise default and trials
ACCEPTED_PATHS = ("", "/", "/services/collector", "/services/collector/", EVENT_PATH, EVENT_PATH + "/",
                  EVENT_PATH + "/1.0")
TOKEN_FIELD = "hec_token"
STATE_KEY = "_export"                               # server-owned export progress inside the encrypted secrets
SOURCETYPE = {"activity": "tradecomply:audit", "mcp": "tradecomply:mcp_audit", "alert": "tradecomply:alert",
              "test": "tradecomply:test"}
SOURCE = {"activity": "tradecomply:activity", "mcp": "tradecomply:mcp", "alert": "tradecomply:events",
          "test": "tradecomply:test"}
TABLE = {"activity": "audit", "mcp": "mcp_audit_log"}
LOGS = ("activity", "mcp")
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 256_000             # well under HEC's per-event default (maxEventSize 5MB); request cap unconfirmed
MAX_BATCHES_PER_ROUND = 20
SETTLE_S = 10                         # rows younger than this wait a round (late commits on PostgreSQL)
BACKOFF_S = (60, 120, 240, 480, 960, 1920, 3600)
MAX_DOWNLOAD_ROWS = 200_000
DOWNLOAD_PAGE = 500
RESPONSE_CAP = 64 * 1024              # HEC answers are tiny JSON
INDEX_RE = r"[A-Za-z0-9_][A-Za-z0-9_-]{0,79}"
TOKEN_RE = r"[A-Za-z0-9-]{16,128}"     # Splunk shows tokens as GUIDs; no spaces, so nothing can break the header
_sleep = time.sleep                   # tests replace it (SafeHTTP's retry pauses)

# Troubleshoot HTTP Event Collector, "Possible error codes": status code -> (what TradeComply does, status message)
HEC_CODES: dict[int, tuple[str, str]] = {
    0: ("ok", "Success"), 1: ("auth", "Token disabled"), 2: ("auth", "Token is required"),
    3: ("auth", "Invalid authorization"), 4: ("auth", "Invalid token"), 5: ("data", "No data"),
    6: ("data", "Invalid data format"), 7: ("index", "Incorrect index"), 8: ("busy", "Internal server error"),
    9: ("busy", "Server is busy"), 10: ("ack", "Data channel is missing"), 11: ("ack", "Invalid data channel"),
    12: ("data", "Event field is required"), 13: ("data", "Event field cannot be blank"),
    14: ("ack", "ACK is disabled"), 15: ("data", "Error in handling indexed fields"),
    16: ("auth", "Query string authorization is not enabled"), 17: ("ok", "HEC is healthy"),
    18: ("busy", "HEC is unhealthy, queues are full"), 19: ("busy", "HEC is unhealthy, ack service unavailable"),
    20: ("busy", "HEC is unhealthy, queues are full, ack service unavailable"), 21: ("auth", "Invalid token"),
    22: ("auth", "Token disabled"), 23: ("busy", "Server is shutting down"),
    24: ("ok", "HEC queue is approaching its capacity limit"), 25: ("ok", "HEC ACK is approaching its capacity limit"),
    26: ("busy", "HEC queue is at capacity and cannot process any more requests"),
    27: ("busy", "HEC ACK channel is at capacity and cannot process any more requests"),
}
PERMANENT_KINDS = ("auth", "index", "ack", "url")


# ---------------------------------------------------------------------------- URL and answers
def normalize_hec_url(value: str) -> str:
    """The HEC base URL as stored: https://host[:port], no path. Raises ConfigError with a plain message."""
    text = str(value or "").strip()
    try:
        u = check_url(text, resolver=None, allowed_ports=HEC_PORTS)
    except UnsafeURLError as e:
        raise ConfigError(f"HEC URL: {e}", "hec_url") from None
    parts = urlsplit(text)
    if parts.query or parts.fragment:
        raise ConfigError("HEC URL: leave out anything after ? or # (the token goes in its own field)", "hec_url")
    if parts.path not in ACCEPTED_PATHS:
        raise ConfigError("HEC URL: enter the collector's address only, e.g. https://http-inputs-yourstack."
                          "splunkcloud.com or https://splunk.example.com:8088", "hec_url")
    host = (u.host or "").lower().rstrip(".")
    port = u.port
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


class HecAnswer:
    """What Splunk said to one POST. kind: ok | auth | index | ack | url | data | too_large | busy."""
    __slots__ = ("kind", "status", "code", "text", "retry_after")

    def __init__(self, kind: str, status: int | None, code: int | None, text: str, retry_after: float | None = None):
        self.kind, self.status, self.code, self.text, self.retry_after = kind, status, code, text, retry_after

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def permanent(self) -> bool:
        return self.kind in PERMANENT_KINDS

    def message(self, host: str, index: str | None = None) -> str:
        """A customer-facing explanation. Uses Splunk's documented status message for the code, never the body."""
        if self.code is not None:
            said = f"{self.text}, code {self.code}"
        else:
            said = f"HTTP {self.status}" if self.status else (self.text or "no answer")
        if self.kind == "ok":
            return f"Splunk accepted the event ({said})."
        if self.kind == "auth":
            if self.code in (1, 22):
                return (f"Splunk says this HEC token is disabled ({said}). Enable it in Splunk under Settings > Data "
                        "inputs > HTTP Event Collector, or enter another token.")
            return (f"Splunk didn't accept the HEC token ({said}). Copy the token again from Splunk (Settings > Data "
                    "inputs > HTTP Event Collector) and save it here.")
        if self.kind == "index":
            which = f" “{index}”" if index else ""
            return (f"Splunk refused the index{which} ({said}): this HEC token isn't allowed to write there. Leave "
                    "Index blank to use the token's default index, or add the index to the token's allowed indexes.")
        if self.kind == "ack":
            return (f"This HEC token has indexer acknowledgment turned on ({said}). TradeComply doesn't use "
                    "acknowledgment: turn it off on the token (Splunk Cloud Platform supports it only for Amazon "
                    "Data Firehose).")
        if self.kind == "url":
            if self.status == 404:
                return (f"No HTTP Event Collector answered at {host} (HTTP 404). Check the HEC URL: Splunk Cloud "
                        "Platform uses https://http-inputs-<stack>.splunkcloud.com, Splunk Enterprise usually "
                        "https://<host>:8088.")
            return f"{host} didn't answer like a Splunk HTTP Event Collector ({said}). Check the HEC URL."
        if self.kind in ("data", "too_large"):
            return f"Splunk refused the data ({said})."
        return f"Splunk is busy or unavailable ({said}). TradeComply keeps the events and tries again; nothing is lost."


def classify(resp: httpx.Response) -> HecAnswer:
    """Read an HEC response: the JSON status code when there is one (documented table), else the HTTP status."""
    code = None
    try:
        body = resp.json()
        if isinstance(body, dict) and isinstance(body.get("code"), int) and not isinstance(body.get("code"), bool):
            code = body["code"]
    except ValueError:
        pass
    status = resp.status_code
    retry = retry_after_seconds(resp)
    if code is not None and code in HEC_CODES:
        kind, text = HEC_CODES[code]
        if kind == "ok" and status != 200:
            kind = "url"
        return HecAnswer(kind, status, code, text, retry)
    if status in (401, 403):
        return HecAnswer("auth", status, None, "", retry)
    if status == 413:
        return HecAnswer("too_large", status, None, "", retry)
    if status == 429 or status >= 500:
        return HecAnswer("busy", status, None, "", retry)
    return HecAnswer("url", status, code, "unexpected answer" if code is not None else "", retry)


def _epoch(created) -> float:
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return round(time.time(), 3)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round(dt.timestamp(), 3)


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def _row_event(log_name: str, row: dict, tenant_id: str, company: str | None) -> dict:
    """The HEC `event` object for one audit or MCP log row (the tenant's own data, as stored)."""
    if log_name == "activity":
        try:
            args = json.loads(row.get("args_json") or "{}")
        except ValueError:
            args = row.get("args_json")
        verified = row.get("verified")
        return {"tc_id": f"activity:{row['id']}", "log": "activity", "id": row["id"], "tenant_id": tenant_id,
                "company": company, "user": row.get("user"), "tool": row.get("tool"), "args": args,
                "verified": None if verified is None else bool(verified), "ok": bool(row.get("ok")),
                "source": row.get("source"), "created": row.get("created")}
    return {"tc_id": f"mcp:{row['id']}", "log": "mcp", "id": row["id"], "tenant_id": tenant_id, "company": company,
            "tool": row.get("tool"), "status": row.get("status"), "duration_ms": row.get("duration_ms"),
            "api_key_id": row.get("api_key_id"), "args_sha256": row.get("args_sha256"), "ip": row.get("ip"),
            "created": row.get("created")}


def _fetch_rows(store, log_name: str, tenant_id: str, after_id: int, until: str, limit: int) -> list[dict]:
    """This tenant's rows of one log after `after_id`, created at or before `until`, oldest first. Always filtered
    by tenant_id (the store has no forward-paging method for these tables)."""
    table = TABLE[log_name]
    with store.connect() as con:
        rows = con.execute(f"select * from {table} where tenant_id = ? and id > ? and created <= ? "
                           "order by id limit ?", (tenant_id, int(after_id), until, int(limit))).fetchall()
    return [dict(r) for r in rows]


def _max_id(store, log_name: str, tenant_id: str) -> int:
    with store.connect() as con:
        row = con.execute(f"select max(id) from {TABLE[log_name]} where tenant_id = ?", (tenant_id,)).fetchone()
    return int(row[0] or 0)


def _count_after(store, log_name: str, tenant_id: str, after_id: int) -> int:
    with store.connect() as con:
        row = con.execute(f"select count(*) from {TABLE[log_name]} where tenant_id = ? and id > ?",
                          (tenant_id, int(after_id))).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------- the connector
class SplunkHECConnector(Connector):
    provider = PROVIDER
    display_name = "Splunk"
    catalog_id = "splunk"
    description = ("Sends your company's TradeComply activity log, AI-assistant (MCP) call log and compliance alerts "
                   "to your own Splunk through its HTTP Event Collector, for an independent, searchable copy. Your "
                   "Splunk admin gives you the HEC URL and token. Testing the connection sends one small test event.")
    category = "other"
    docs_url = ("https://help.splunk.com/en/splunk-enterprise/get-data-in/collect-http-event-data/"
                "format-events-for-http-event-collector")
    required_env = ()                     # nothing on the server: each company brings its own HEC URL and token
    config_fields = (
        ConfigField("hec_url", "HEC URL", required=True, max_length=300, pattern=r"https://\S+",
                    placeholder="https://http-inputs-yourstack.splunkcloud.com",
                    help="Splunk Cloud Platform: https://http-inputs-<your stack>.splunkcloud.com (port 443). "
                         "Splunk Enterprise: https://<your host>:8088. Ports 443, 8088 and 8443; the collector must "
                         "be reachable from the internet over HTTPS."),
        ConfigField("hec_token", "HEC token", type="password", secret=True, required=True, max_length=128,
                    pattern=TOKEN_RE, help="From Splunk: Settings > Data inputs > HTTP Event Collector. Turn indexer "
                                           "acknowledgment off for this token."),
        ConfigField("index", "Index (optional)", max_length=80, pattern=INDEX_RE,
                    help="Leave blank to use the token's default index. It must be one of the token's allowed "
                         "indexes."),
        ConfigField("send_history", "Also send activity from before this connection", type="boolean", default=True),
        ConfigField("send_alerts", "Also send compliance alerts (deadlines, subcontractor and payroll issues)",
                    type="boolean", default=True),
    )
    subscribes = tuple(EVENTS)
    unconfirmed = (
        "The largest request body Splunk Cloud Platform's HEC accepts (batches are kept under 256 KB and 100 events).",
        "Whether Splunk Cloud Platform requires the sending server's IP addresses to be allow-listed for HEC.",
        "Read from Splunk's documentation through an index of help.splunk.com, not the live pages.",
    )

    # ------------------------------------------------------------------ settings
    @classmethod
    def clean_config(cls, values, existing_config=None, existing_secrets=None):
        config, secret = super().clean_config(values, existing_config, existing_secrets)
        if config.get("hec_url"):
            config["hec_url"] = normalize_hec_url(config["hec_url"])
        # The saved token goes to the saved URL. A new URL keeps it only when the token is entered again: otherwise
        # anyone who may edit settings (an office user, an API key) could point the URL at a server of theirs and
        # receive the company's HEC token with the next test or export, although the token is never shown to them.
        old_url = (existing_config or {}).get("hec_url")
        retyped = isinstance(values, dict) and str(values.get(TOKEN_FIELD) or "").strip()
        if old_url and config.get("hec_url") != old_url and (existing_secrets or {}).get(TOKEN_FIELD) \
                and not retyped:
            raise ConfigError("HEC token: enter the token again when you change the HEC URL (the saved token is "
                              "only ever sent to the URL it was saved with).", TOKEN_FIELD)
        return config, secret

    def _settings(self) -> tuple[str, str, str | None, dict]:
        """(base URL, token, index, secrets). Raises NotConnected when nothing usable is saved."""
        conn = self.connection(with_secret=True)
        secrets = self._decrypt((conn or {}).get("secret_blob"))
        config = (conn or {}).get("config") or {}
        token = secrets.get(TOKEN_FIELD)
        if not (config.get("hec_url") and token):
            raise NotConnected("Splunk isn't set up for this company yet: enter the HEC URL and token.")
        return config["hec_url"], token, (config.get("index") or None), secrets

    def _host(self) -> str:
        url = self.ctx.platform.settings.app_url
        return (urlsplit(url).hostname if url else None) or "tradecomply"

    def _envelope(self, kind: str, event: dict, when: float, index: str | None) -> dict:
        out = {"time": when, "host": self._host(), "source": SOURCE[kind], "sourcetype": SOURCETYPE[kind],
               "event": event}
        if index:
            out["index"] = index
        return out

    # ------------------------------------------------------------------ one POST
    def post_events(self, events: list[dict], base: str, token: str) -> HecAnswer:
        """POST stacked event objects to /services/collector/event. Raises SafeHTTPError (unreachable, refused
        address, oversized answer)."""
        body = b"".join(_dumps(e) for e in events)
        headers = {"authorization": f"Splunk {token}", "content-type": "application/json",
                   "accept": "application/json"}
        with self.ctx.http(allowed_ports=HEC_PORTS, follow_redirects=False, sleep=lambda s: _sleep(s)) as http:
            resp = http.post(base + EVENT_PATH, content=body, headers=headers, retries=2, idempotent=True,
                             max_bytes=RESPONSE_CAP)
        answer = classify(resp)
        log.info("splunk HEC %s -> %s (%s, %d event(s))", urlsplit(base).hostname, resp.status_code,
                 answer.code if answer.code is not None else "no code", len(events))
        return answer

    # ------------------------------------------------------------------ lifecycle
    def test(self) -> TestResult:
        base, token, index, _ = self._settings()
        host = urlsplit(base).hostname
        event = {"tc_id": f"test:{_secrets.token_hex(8)}", "log": "test", "tenant_id": self.ctx.tenant_id,
                 "message": "TradeComply connection test. You can ignore this event.",
                 "sent_by": self.ctx.user_label}
        try:
            answer = self.post_events([self._envelope("test", event, round(time.time(), 3), index)], base, token)
        except UnsafeURLError as e:
            return TestResult(False, f"TradeComply can't send to this HEC URL: {e}")
        except ResponseTooLarge:
            return TestResult(False, f"{host} didn't answer like a Splunk HTTP Event Collector. Check the HEC URL.")
        except SafeHTTPError as e:
            return TestResult(False, f"Could not reach Splunk at {host}: {e}")
        details = {"hec_host": host, "index": index or "the token's default index", "code": answer.code,
                   "sourcetype": SOURCETYPE["test"]}
        if answer.ok:
            return TestResult(True, f"Splunk accepted a test event (sourcetype {SOURCETYPE['test']}).", account=host,
                              details=details)
        return TestResult(False, answer.message(host, index), details=details)

    def connect(self) -> dict:
        result = super().connect()
        if result.get("status") == "connected":
            self._init_cursor()
            result = {**self.status(), "test": result.get("test")}
        return result

    def _init_cursor(self) -> None:
        """Where a new connection starts: the beginning (send_history) or the current end of each log."""
        conn = self.connection(with_secret=True)
        secrets = self._decrypt((conn or {}).get("secret_blob"))
        if not secrets.get(TOKEN_FIELD) or isinstance(secrets.get(STATE_KEY), dict):
            return
        history = (conn.get("config") or {}).get("send_history", True) is not False
        store = self.ctx.store
        state = {name: (0 if history else _max_id(store, name, self.ctx.tenant_id)) for name in LOGS}
        state.update(sent=0, last_sent=None, started=now_iso())
        self.save(secrets={**secrets, STATE_KEY: state}, expect_secret_blob=conn["secret_blob"])

    def handle_event(self, event: str, payload: dict) -> None:
        """A compliance alert from the event bus, as one HEC event. A refused token/index/URL sets the connection to
        error and is not retried; a busy Splunk is retried by the bus."""
        if self.config().get("send_alerts", True) is False:
            return
        base, token, index, _ = self._settings()
        host = urlsplit(base).hostname
        company = (self.ctx.store.get_tenant(self.ctx.tenant_id) or {}).get("company_name")
        digest = hashlib.sha256(_dumps({"e": event, "p": payload})).hexdigest()
        data = {"tc_id": f"alert:{digest[:32]}", "log": "alert", "event": event, "tenant_id": self.ctx.tenant_id,
                "company": company, **{k: v for k, v in (payload or {}).items() if k not in ("tc_id", "log")}}
        link = data.get("link")
        app_url = self.ctx.platform.settings.app_url
        if isinstance(link, str) and link.startswith("/") and app_url:
            data["link"] = app_url + link
        try:
            answer = self.post_events([self._envelope("alert", data, round(time.time(), 3), index)], base, token)
        except UnsafeURLError as e:
            self.save(status="error", last_error=f"TradeComply can't send to this HEC URL: {e}")
            raise PermanentError(f"TradeComply can't send to this HEC URL: {e}") from None
        if answer.ok:
            return
        message = answer.message(host, index)
        if answer.permanent:
            self.save(status="error", last_error=message)
            raise PermanentError(message)
        if answer.kind in ("data", "too_large"):
            raise PermanentError(message)
        raise ConnectorError(message)          # busy: the event bus retries on its schedule


# ---------------------------------------------------------------------------- export of the logs
_tenant_locks: dict[str, threading.Lock] = {}
_tenant_locks_guard = threading.Lock()


def _tenant_lock(tenant_id: str) -> threading.Lock:
    with _tenant_locks_guard:
        return _tenant_locks.setdefault(tenant_id, threading.Lock())


def _backoff_table(platform) -> dict:
    table = getattr(platform, "_splunk_backoff", None)
    if table is None:
        table = platform._splunk_backoff = {}
    return table


def _save_progress(inst: SplunkHECConnector, used_token: str, used_url: str, cursor: dict, sent: int) -> bool:
    """Move the cursor (compare-and-set on the secrets blob). If the token or URL changed meanwhile, the rows went
    to the old destination and the new settings keep their own cursor: nothing is written."""
    for _ in range(4):
        conn = inst.connection(with_secret=True)
        if not conn or not conn.get("secret_blob"):
            return False
        current = inst._decrypt(conn["secret_blob"])
        if current.get(TOKEN_FIELD) != used_token or (conn.get("config") or {}).get("hec_url") != used_url:
            return False
        state = dict(current.get(STATE_KEY) or {})
        for name in LOGS:
            state[name] = max(int(state.get(name) or 0), int(cursor.get(name) or 0))
        state["sent"] = int(state.get("sent") or 0) + sent
        state["last_sent"] = now_iso()
        if inst.save(secrets={**current, STATE_KEY: state}, expect_secret_blob=conn["secret_blob"]) is not None:
            return True
    return False


def export_tenant(platform, tenant_id: str, *, now: datetime | None = None,
                  max_batches: int = MAX_BATCHES_PER_ROUND, force: bool = False) -> dict:
    """Send this tenant's new activity-log and MCP-log rows to its Splunk. Safe to repeat. Returns a summary."""
    lock = _tenant_lock(tenant_id)
    if not lock.acquire(blocking=False):
        return {"tenant_id": tenant_id, "skipped": "an export for this company is already running", "sent": 0}
    try:
        return _export_locked(platform, tenant_id, now=now, max_batches=max_batches, force=force)
    finally:
        lock.release()


def _export_locked(platform, tenant_id: str, *, now, max_batches: int, force: bool) -> dict:
    store = platform.store
    tenant = store.get_tenant(tenant_id)
    if not tenant:
        return {"tenant_id": tenant_id, "skipped": "no such company", "sent": 0}
    if not entitlement(tenant)["ok"]:
        return {"tenant_id": tenant_id, "skipped": "no active trial or subscription", "sent": 0}
    inst = SplunkHECConnector(ConnectorContext(platform, tenant_id, user_label="splunk export"))
    conn = inst.connection()
    if not conn or conn["status"] != "connected":
        return {"tenant_id": tenant_id, "skipped": "Splunk is not connected", "sent": 0}
    backoff = _backoff_table(platform)
    wake, failures = backoff.get(tenant_id, (0.0, 0))
    if not force and wake > time.time():
        return {"tenant_id": tenant_id, "skipped": "waiting after Splunk was busy", "sent": 0}
    try:
        base, token, index, secrets = inst._settings()
        if not isinstance(secrets.get(STATE_KEY), dict):     # connected through Test rather than Connect
            inst._init_cursor()
            base, token, index, secrets = inst._settings()
    except NotConnected as e:
        return {"tenant_id": tenant_id, "skipped": str(e), "sent": 0}
    host = urlsplit(base).hostname
    state = secrets.get(STATE_KEY) if isinstance(secrets.get(STATE_KEY), dict) else {}
    cursor = {name: int(state.get(name) or 0) for name in LOGS}
    until = to_store_time((now or datetime.now(timezone.utc)) - timedelta(seconds=SETTLE_S))
    company = tenant.get("company_name")
    sent = skipped = batches = 0
    result = {"tenant_id": tenant_id}

    def finish(**extra) -> dict:
        result.update(sent=sent, skipped_rows=skipped, batches=batches, cursor=dict(cursor), **extra)
        return result

    for name in LOGS:
        while batches < max_batches:
            rows = _fetch_rows(store, name, tenant_id, cursor[name], until, MAX_BATCH_EVENTS)
            if not rows:
                break
            chunk, size = [], 0
            for row in rows:
                ev = inst._envelope(name, _row_event(name, row, tenant_id, company), _epoch(row["created"]), index)
                n = len(_dumps(ev))
                if chunk and size + n > MAX_BATCH_BYTES:
                    break
                chunk.append((row["id"], ev))
                size += n
            batches += 1
            bad = 0
            try:
                answer = inst.post_events([ev for _, ev in chunk], base, token)
                if answer.kind in ("data", "too_large"):
                    answer, bad = _send_one_by_one(inst, chunk, base, token)
            except UnsafeURLError as e:
                inst.save(status="error", last_error=f"TradeComply can't send to this HEC URL: {e}")
                return finish(error=str(e), stopped="url")
            except SafeHTTPError as e:
                answer = HecAnswer("busy", None, None, str(e))
            if not answer.ok:
                message = answer.message(host, index)
                if answer.permanent:
                    inst.save(status="error", last_error=message)
                    log.warning("splunk export for %s stopped: %s (code %s)", tenant_id, answer.kind, answer.code)
                    return finish(error=message, stopped=answer.kind)
                failures += 1
                delay = BACKOFF_S[min(failures, len(BACKOFF_S)) - 1]
                if answer.retry_after:
                    delay = max(delay, min(answer.retry_after, BACKOFF_S[-1]))
                backoff[tenant_id] = (time.time() + delay, failures)
                inst.save(last_error=message)
                return finish(error=message, retry_in_s=int(delay))
            cursor[name] = chunk[-1][0]
            sent += len(chunk) - bad
            skipped += bad
            if not _save_progress(inst, token, base, {name: cursor[name]}, len(chunk) - bad):
                return finish(stopped="settings changed during the export")
            if len(rows) < MAX_BATCH_EVENTS and len(chunk) == len(rows):
                break
    backoff.pop(tenant_id, None)
    if sent and inst.connection().get("last_error"):
        inst.save(last_error=None)
    return finish()


def _send_one_by_one(inst, chunk, base, token) -> tuple[HecAnswer, int]:
    """Splunk called the batch malformed: send each event alone and skip the ones it refuses on their own."""
    bad = 0
    last = HecAnswer("ok", 200, 0, "Success")
    for row_id, ev in chunk:
        answer = inst.post_events([ev], base, token)
        if answer.kind in ("data", "too_large"):
            bad += 1
            log.warning("splunk export: Splunk refused event %s (%s); skipped", ev["event"].get("tc_id"),
                        answer.code if answer.code is not None else answer.status)
            continue
        if not answer.ok:
            return answer, bad          # busy or refused: stop here, the cursor stays before this batch
        last = answer
    return last, bad


def _connected_tenants(store) -> list[str]:
    return [c["tenant_id"] for c in store.connections_for_provider(PROVIDER, status="connected")]


def export_all(platform, *, interval_s: float = 60.0, now: datetime | None = None, slot: int | None = None) -> dict:
    """OPERATOR ONLY: one export round for every connected tenant. One instance per tenant per round (`slot`, by
    default the current interval number)."""
    totals = {"tenants": 0, "exported": 0, "sent": 0, "failed": 0}
    slot = int(time.time() // max(1.0, interval_s)) if slot is None else int(slot)
    for tid in _connected_tenants(platform.store):
        totals["tenants"] += 1
        if not platform.store.claim_nonce(f"splunk-export:{tid}:{slot}", int(time.time() + 2 * interval_s + 60)):
            continue                    # another instance has this tenant's round
        try:
            r = export_tenant(platform, tid, now=now)
        except Exception as e:      # noqa: BLE001 - one tenant's failure never stops the others; no row contents
            totals["failed"] += 1
            log.warning("splunk export failed for tenant %s (%s)", tid, type(e).__name__)
            continue
        if "skipped" not in r:
            totals["exported"] += 1
        totals["sent"] += r.get("sent", 0)
    return totals


class HecExporter:
    """Runs export_all every `interval_s` seconds in a daemon thread (no database call on the event loop)."""

    def __init__(self, platform, interval_s: float = 60.0):
        self.platform, self.interval_s = platform, max(15.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                export_all(self.platform, interval_s=self.interval_s)
            except Exception:      # noqa: BLE001 - e.g. the database is briefly down; next round tries again
                log.exception("splunk export round failed")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tc-splunk-export", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def start_exporter(platform) -> HecExporter | None:
    """For app/main.py's lifespan. SPLUNK_EXPORT=0 turns the background export off."""
    env = platform.env
    if str(env.get("SPLUNK_EXPORT") or "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    try:
        interval = float(env.get("SPLUNK_EXPORT_INTERVAL") or 60)
    except ValueError:
        interval = 60.0
    exporter = getattr(platform, "splunk_exporter", None) or HecExporter(platform, interval)
    platform.splunk_exporter = exporter
    exporter.start()
    return exporter


def stop_exporter(platform) -> None:
    exporter = getattr(platform, "splunk_exporter", None)
    if exporter:
        exporter.stop()


# ---------------------------------------------------------------------------- HTTP API
router = APIRouter(tags=["integrations"])


def _limiter(platform, name: str, limit: int, window_s: int) -> RateLimiter:
    lim = getattr(platform, name, None)
    if lim is None:
        lim = RateLimiter(limit=limit, window_s=window_s)
        setattr(platform, name, lim)
    return lim


def _hit(platform, name: str, key: str, limit: int, window_s: int, what: str) -> None:
    lim = _limiter(platform, name, limit, window_s)
    wait = lim.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many {what} in a short time. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    lim.hit(key)


def _audit(platform, p: Principal, tool: str, args: dict, ok: bool = True) -> None:
    try:
        platform.store.audit(p.tenant_id, p.label, tool, args, None, ok=ok, source="integrations")
    except Exception:      # noqa: BLE001 - never turn the action into a 500 because of the log
        log.exception("audit write failed")


@router.get("/api/integrations/splunk/export")
def export_status(request: Request, p: Principal = Depends(office_up)):
    """Where this company's Splunk export stands: cursor, rows not yet sent, last send, last error."""
    platform = platform_of(request)
    inst = SplunkHECConnector(ConnectorContext(platform, p.tenant_id, p.user_id, p.label))
    status = inst.status()
    try:
        secrets = inst.secrets()
    except NotConnected:
        secrets = {}
    state = secrets.get(STATE_KEY) if isinstance(secrets.get(STATE_KEY), dict) else {}
    cursor = {name: int(state.get(name) or 0) for name in LOGS}
    pending = {name: _count_after(platform.store, name, p.tenant_id, cursor[name]) for name in LOGS} \
        if status["status"] == "connected" else None
    exporter = getattr(platform, "splunk_exporter", None)
    return {"connection": {k: status.get(k) for k in ("status", "external_account", "last_error", "updated")},
            "cursor": cursor, "pending": pending, "sent_total": int(state.get("sent") or 0),
            "last_sent": state.get("last_sent"), "background_export": bool(exporter and exporter.running),
            "sourcetypes": SOURCETYPE}


@router.post("/api/integrations/splunk/export")
def export_now(request: Request, p: Principal = Depends(office_up)):
    """Run one export round for this company now (the background export does the same every minute)."""
    platform = platform_of(request)
    require_entitled(platform, p)
    _hit(platform, "_splunk_export_limiter", f"tenant|{p.tenant_id}", 30, 600, "exports")
    result = export_tenant(platform, p.tenant_id, force=True)
    if result.get("skipped") == "Splunk is not connected":
        raise HTTPException(409, "Connect Splunk first (Settings > Integrations > Splunk).")
    _audit(platform, p, "integrations.splunk_export", {"sent": result.get("sent", 0)}, ok="error" not in result)
    return result


# ---------------------------------------------------------------------------- owner download of the logs
ACTIVITY_COLUMNS = ("id", "created", "user", "tool", "source", "ok", "verified", "args")
MCP_COLUMNS = ("id", "created", "tool", "status", "duration_ms", "api_key_id", "args_sha256", "ip")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """A CSV cell that a spreadsheet won't run as a formula (OWASP CSV injection: prefix with a single quote)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = value if isinstance(value, str) else (json.dumps(value, separators=(",", ":"), ensure_ascii=False)
                                                 if isinstance(value, (dict, list)) else str(value))
    if isinstance(value, str) and text.startswith(_FORMULA_START):
        return "'" + text
    return text


def _download_row(log_name: str, row: dict) -> dict:
    if log_name == "activity":
        try:
            args = json.loads(row.get("args_json") or "{}")
        except ValueError:
            args = row.get("args_json")
        v = row.get("verified")
        return {"id": row["id"], "created": row["created"], "user": row.get("user"), "tool": row.get("tool"),
                "source": row.get("source"), "ok": bool(row.get("ok")), "verified": None if v is None else bool(v),
                "args": args}
    return {k: row.get(k) for k in MCP_COLUMNS}


def _parse_bound(value: Optional[str], name: str) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    try:
        return to_store_time(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a date (YYYY-MM-DD) or an ISO timestamp")


def _rows_between(store, log_name: str, tenant_id: str, start: str | None, end: str | None):
    """This tenant's rows of one log with created in [start, end), oldest first, paged by id."""
    table = TABLE[log_name]
    where, params = ["tenant_id = ?", "id > ?"], [tenant_id, 0]
    if start:
        where.append("created >= ?")
        params.append(start)
    if end:
        where.append("created < ?")
        params.append(end)
    after, total = 0, 0
    while total < MAX_DOWNLOAD_ROWS:
        params[1] = after
        with store.connect() as con:
            rows = [dict(r) for r in con.execute(f"select * from {table} where {' and '.join(where)} order by id "
                                                 "limit ?", (*params, DOWNLOAD_PAGE)).fetchall()]
        if not rows:
            return
        for r in rows[:MAX_DOWNLOAD_ROWS - total]:
            yield r
        total += len(rows)
        after = rows[-1]["id"]
        if len(rows) < DOWNLOAD_PAGE:
            return


@router.get("/api/audit-export")
def audit_export(request: Request, p: Principal = Depends(owner_only),
                 log_name: str = Query("activity", alias="log", pattern="^(activity|mcp)$"),
                 fmt: str = Query("csv", alias="format", pattern="^(csv|json)$"),
                 start: Optional[str] = Query(None, max_length=40), end: Optional[str] = Query(None, max_length=40)):
    """Download this company's activity log or MCP call log (owner only). [start, end) in UTC; oldest first."""
    platform = platform_of(request)
    start_s, end_s = _parse_bound(start, "start"), _parse_bound(end, "end")
    if start_s and end_s and start_s >= end_s:
        raise HTTPException(422, "start must be before end")
    _hit(platform, "_audit_export_limiter", f"tenant|{p.tenant_id}", 20, 600, "downloads")
    _audit(platform, p, "audit.export", {"log": log_name, "format": fmt, "start": start_s, "end": end_s})
    store, tid = platform.store, p.tenant_id
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"tradecomply-{log_name}-log-{stamp}.{fmt}"
    columns = ACTIVITY_COLUMNS if log_name == "activity" else MCP_COLUMNS

    def csv_chunks():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        n = 0
        for row in _rows_between(store, log_name, tid, start_s, end_s):
            d = _download_row(log_name, row)
            w.writerow([csv_safe(d.get(c)) for c in columns])
            n += 1
            if n % 200 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    def json_chunks():
        yield json.dumps({"log": log_name, "start": start_s, "end": end_s, "generated": now_iso()})[:-1] + ',"rows":['
        n = 0
        for row in _rows_between(store, log_name, tid, start_s, end_s):
            yield ("," if n else "") + json.dumps(_download_row(log_name, row), separators=(",", ":"),
                                                  ensure_ascii=False, default=str)
            n += 1
        yield f'],"row_count":{n},"truncated":{"true" if n >= MAX_DOWNLOAD_ROWS else "false"}}}'

    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/json"
    return StreamingResponse(csv_chunks() if fmt == "csv" else json_chunks(), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                      "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- CLI (Render Cron Job)
def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["export"]:
        print("usage: python -m app.connectors.splunk export")
        return 2
    from ..config import Settings
    from ..main import create_app
    app = create_app(Settings.from_env(events_background=False))
    platform = app.state.platform
    try:
        from .. import connectors
        connectors.discover()
        print(json.dumps(export_all(platform)))
    finally:
        platform.store.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
