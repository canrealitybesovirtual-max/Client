"""Event bus: emit(tenant_id, event, payload) records one outbound_events row per subscriber and returns at once;
a background thread delivers them with retries. A request never waits for a delivery.

Subscribers of an event, resolved when it is emitted (one row = one target):
- "webhook:<id>"        the tenant's enabled webhooks listing the event (or "*")      -> app/webhooks.py
- "connector:<provider>" connectors whose `subscribes` lists it and that this tenant has connected
                          -> Connector.handle_event(event, payload)
- "handler:<name>"      in-process functions registered with @subscribe(...)          -> fn(platform, tenant_id,
                                                                                          event, payload)

Delivery: the worker claims due rows (row locks, so several instances never deliver one row twice), delivers each,
and marks it delivered, failed (can never succeed: PermanentError, a 4xx Slack/Teams refusal, retries used up) or
skipped (the webhook was deleted or disabled, the connector disconnected). Anything else is retried on the Standard
Webhooks example schedule (5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h, 24 h: ten attempts over about three days),
with jitter, and never sooner than a Retry-After the receiver sent. A worker that dies mid-delivery leaves the row
`sending` with a lease; it is picked up again when the lease (5 min) runs out.

Payload convention (validated by emit):
    title     required, plain text, at most 200 characters ("Sub's COI expires in 7 days")
    summary   optional plain text, at most 2000
    severity  "info" (default) | "warning" | "critical"
    link      optional app path starting with "/" ("/#/projects/3"); made absolute with APP_URL in messages
    due_date  optional YYYY-MM-DD
    any other keys: JSON values, passed through to generic webhooks and connectors (whole payload <= 32 KB)
Use dedupe_key for events a scan finds again and again (e.g. f"coi:{sub_id}:{expiry}"): a target gets at most one
delivery per key (outbound rows are purged after 30 days, after which the key could fire again).

From a plain `def` handler or a thread: platform.events.emit(...). From an `async def` handler:
await platform.events.aemit(...) (emit talks to the database, which must never run on the event loop). The
module-level emit() uses the bus of the running app.
"""
from __future__ import annotations

import logging
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from . import connectors as registry
from . import webhooks
from .crypto import DecryptError, webhook_context
from .store import iso_in, now_iso

log = logging.getLogger("tradecomply.events")

EVENTS: dict[str, str] = {
    "deadline_due_soon": "A tracked deadline (lien notice, COI or license expiry, toolbox talk, ...) is coming up "
                         "or has passed.",
    "license_problem_found": "A CSLB license check found a problem with a license (status, classification, bond "
                             "or workers' comp on file).",
    "sub_noncompliant": "A subcontractor failed a compliance check (license, insurance certificate, workers' comp, "
                        "W-9 or DIR registration).",
    "contract_generated": "A contract, waiver or notice was generated and saved as a document.",
    "injury_report_due": "A workplace injury or illness needs a report by a deadline (the payload says which "
                         "report and when).",
    "payroll_issue": "A payroll or wage-rule check found an issue.",
}
TEST_EVENT = "test"
SEVERITIES = ("info", "warning", "critical")
RETRY_DELAYS_S = (5, 300, 1800, 7200, 18000, 36000, 50400, 72000, 86400)
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1
PURGE_AFTER_DAYS = 30
MAX_PAYLOAD = 32_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HANDLER_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")

_handlers: dict[str, tuple[frozenset, Callable]] = {}
_handlers_lock = threading.Lock()
_default_bus: "EventBus | None" = None


def register_event(name: str, description: str) -> None:
    """Add an event name (for modules that need one not listed above). Names: lowercase words joined by _."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,59}", name or "") or name == TEST_EVENT:
        raise ValueError(f"bad event name {name!r}")
    EVENTS.setdefault(name, description)


def subscribe(events: str | Iterable[str], name: str | None = None):
    """Decorator: deliver these events (or "*" for all) to fn(platform, tenant_id, event, payload), in the
    background, with the same retries as webhooks. Raise connectors.PermanentError to stop retrying."""
    wanted = frozenset([events] if isinstance(events, str) else events)
    unknown = [e for e in wanted if e != "*" and e not in EVENTS]
    if unknown:
        raise ValueError(f"unknown event(s) {unknown}")

    def deco(fn):
        key = name or f"{fn.__module__}.{fn.__qualname__}"
        if not HANDLER_NAME_RE.match(key):
            raise ValueError(f"bad handler name {key!r}")
        with _handlers_lock:
            _handlers[key] = (wanted, fn)
        fn.subscription_name = key
        return fn
    return deco


def unsubscribe(name: str) -> None:
    with _handlers_lock:
        _handlers.pop(name, None)


def validate_payload(event: str, payload) -> dict:
    """The payload as it will be stored, or ValueError (a programming error in the emitter)."""
    import json
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; known: {sorted(EVENTS)}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    out = dict(payload)
    title = out.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("payload['title'] is required: plain text, 1-200 characters")
    out["title"] = title.strip()
    if out.get("summary") is not None and (not isinstance(out["summary"], str) or len(out["summary"]) > 2000):
        raise ValueError("payload['summary'] must be text of at most 2000 characters")
    out["severity"] = out.get("severity") or "info"
    if out["severity"] not in SEVERITIES:
        raise ValueError(f"payload['severity'] must be one of {SEVERITIES}")
    link = out.get("link")
    if link is not None and (not isinstance(link, str) or not link.startswith("/") or link.startswith("//")
                             or len(link) > 500):
        raise ValueError("payload['link'] must be an app path starting with '/' (e.g. '/#/projects/3')")
    if out.get("due_date") is not None and not (isinstance(out["due_date"], str) and DATE_RE.match(out["due_date"])):
        raise ValueError("payload['due_date'] must be YYYY-MM-DD")
    try:
        size = len(json.dumps(out, separators=(",", ":")))
    except (TypeError, ValueError):
        raise ValueError("payload must be JSON-serializable (str, int, float, bool, None, list, dict)") from None
    if size > MAX_PAYLOAD:
        raise ValueError(f"payload is too large ({size} bytes; max {MAX_PAYLOAD})")
    return out


class _Retry(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class _Fail(Exception):
    pass


class _Skip(Exception):
    pass


def message_id(row: dict) -> str:
    """Stable across retries of one row, unique even if SQLite reuses an id after a purge, and without "." (the
    Standard Webhooks signature uses "." as its delimiter)."""
    stamp = re.sub(r"[^0-9]", "", str(row.get("created") or ""))[:14]
    return f"msg_{row['id']}_{stamp}"


class EventBus:
    def __init__(self, platform, *, background: bool = True, poll_s: float = 10.0, batch: int = 20,
                 workers: int = 4, lease_s: float = 300.0):
        self.platform = platform
        self.background, self.poll_s, self.batch = background, poll_s, batch
        self.workers, self.lease_s = workers, lease_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._last_purge = 0.0

    # ------------------------------------------------------------------ emitting
    def targets_for(self, tenant_id: str, event: str) -> list[str]:
        store = self.platform.store
        targets = [f"webhook:{w['id']}" for w in store.webhooks_for_event(tenant_id, event)]
        subs = registry.subscribers_of(event)
        if subs:
            connected = {c["provider"] for c in store.list_connections(tenant_id) if c["status"] == "connected"}
            targets += [f"connector:{c.provider}" for c in subs if c.provider in connected]
        with _handlers_lock:
            targets += [f"handler:{name}" for name, (wanted, _) in sorted(_handlers.items())
                        if event in wanted or "*" in wanted]
        return targets

    def emit(self, tenant_id: str, event: str, payload: dict, *, dedupe_key: str | None = None) -> list[int]:
        """Record the event for every subscriber and wake the worker. Returns the outbound row ids ([] when nobody
        subscribes). Raises ValueError for an unknown event or a bad payload; a database failure is logged and
        swallowed (returns []), so an alert can never break the request that raised it."""
        clean = validate_payload(event, payload)
        try:
            targets = self.targets_for(tenant_id, event)
            if not targets:
                return []
            ids = self.platform.store.enqueue_outbound(tenant_id, event, clean, targets, dedupe_key=dedupe_key)
        except Exception:      # noqa: BLE001 - never fail the caller because of an alert
            log.exception("could not record event %s for tenant %s", event, tenant_id)
            return []
        if ids:
            self.wake()
        return ids

    async def aemit(self, tenant_id: str, event: str, payload: dict, *, dedupe_key: str | None = None) -> list[int]:
        import anyio
        return await anyio.to_thread.run_sync(lambda: self.emit(tenant_id, event, payload, dedupe_key=dedupe_key))

    # ------------------------------------------------------------------ the worker
    def start(self) -> None:
        """Start the delivery thread (no-op when background=False or already running). Never touches the
        database itself, so it is safe to call from the app's startup on the event loop."""
        if not self.background or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="tc-deliver")
        self._thread = threading.Thread(target=self._loop, name="tc-events", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=False)
            self._pool = None

    def wake(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        import time
        while not self._stop.is_set():
            self._wake.clear()           # cleared before the claim: an emit during delivery wakes the next round
            try:
                n = self.run_once(concurrent=True)
                if time.monotonic() - self._last_purge > 3600:
                    self._last_purge = time.monotonic()
                    self.platform.store.purge_outbound(datetime.now(timezone.utc) - timedelta(days=PURGE_AFTER_DAYS))
                    from . import assistant         # local import: app.assistant imports the API, which imports this
                    assistant.retention(self.platform)     # Ask TradeComply chats, also when nobody uses the helper
            except Exception:      # noqa: BLE001 - e.g. the database is briefly down; try again next round
                log.exception("event delivery round failed")
                n = 0
            if n < self.batch:
                self._wake.wait(self.poll_s)

    def run_once(self, now: datetime | None = None, concurrent: bool = False) -> int:
        """Claim the due rows and deliver them. Returns how many were claimed. Tests call this (or drain())."""
        rows = self.platform.store.claim_outbound(limit=self.batch, lease_s=self.lease_s, now=now)
        if concurrent and self._pool and len(rows) > 1:
            list(self._pool.map(self.deliver, rows))
        else:
            for row in rows:
                self.deliver(row)
        return len(rows)

    def drain(self, max_rounds: int = 100, now: datetime | None = None) -> int:
        """Deliver everything that is due now (retries scheduled later stay queued)."""
        total = 0
        for _ in range(max_rounds):
            n = self.run_once(now=now)
            total += n
            if not n:
                break
        return total

    # ------------------------------------------------------------------ one delivery
    def deliver(self, row: dict) -> str:
        """Deliver one claimed row and record the outcome. Returns the new status."""
        store, tenant_id = self.platform.store, row["tenant_id"]
        kind, _, ident = row["target"].partition(":")
        try:
            if kind == "webhook":
                self._deliver_webhook(row, ident)
            elif kind == "connector":
                self._deliver_connector(row, ident)
            elif kind == "handler":
                self._deliver_handler(row, ident)
            else:
                raise _Fail(f"unknown target {row['target']}")
            store.finish_outbound(tenant_id, row["id"], "delivered")
            return "delivered"
        except _Skip as e:
            store.finish_outbound(tenant_id, row["id"], "skipped", last_error=str(e))
            return "skipped"
        except _Fail as e:
            store.finish_outbound(tenant_id, row["id"], "failed", last_error=str(e))
            return "failed"
        except _Retry as e:
            return self._retry(row, str(e), e.retry_after)
        except registry.PermanentError as e:
            store.finish_outbound(tenant_id, row["id"], "failed", last_error=str(e))
            return "failed"
        except registry.NotConnected as e:
            store.finish_outbound(tenant_id, row["id"], "failed", last_error=str(e))
            return "failed"
        except registry.ConnectorError as e:
            return self._retry(row, str(e))
        except Exception as e:      # noqa: BLE001 - a bug or outage in a subscriber; retried, message kept short
            log.warning("delivery of %s to %s failed (%s)", row["event"], row["target"], type(e).__name__)
            return self._retry(row, f"{type(e).__name__}")

    def _retry(self, row: dict, message: str, retry_after: float | None = None) -> str:
        attempts = int(row.get("attempts") or 1)
        if attempts >= MAX_ATTEMPTS:
            self.platform.store.finish_outbound(row["tenant_id"], row["id"], "failed",
                                                last_error=f"gave up after {attempts} attempts: {message}"[:1000])
            return "failed"
        delay = RETRY_DELAYS_S[min(attempts - 1, len(RETRY_DELAYS_S) - 1)] * (0.9 + random.random() * 0.2)
        if retry_after:
            delay = max(delay, min(float(retry_after), 86400.0))
        self.platform.store.finish_outbound(row["tenant_id"], row["id"], "pending", next_attempt=iso_in(delay),
                                            last_error=message[:1000])
        return "pending"

    def _deliver_webhook(self, row: dict, ident: str) -> None:
        store, box, tenant_id = self.platform.store, self.platform.secret_box, row["tenant_id"]
        hook = store.get_webhook(tenant_id, int(ident), with_secret=True) if ident.isdigit() else None
        if not hook:
            raise _Skip("webhook was deleted")
        if not hook["enabled"]:
            raise _Skip("webhook is turned off")
        try:
            url = box.decrypt_text(hook["url"], webhook_context(tenant_id))
            secret = (box.decrypt_text(hook["signing_secret_blob"], webhook_context(tenant_id))
                      if hook.get("signing_secret_blob") else None)
        except DecryptError:
            raise _Fail("the saved webhook URL can't be read on this server (encryption key changed); re-enter it") \
                from None
        with self.platform.http(retries=0) as http:
            result = webhooks.send(http, hook["kind"], url, row["event"], row["payload"], msg_id=message_id(row),
                                   created=row["created"], signing_secret=secret,
                                   app_url=self.platform.settings.app_url)
        changes = {"last_status": result.summary, "last_sent": now_iso()}
        if result.disable:
            changes["enabled"] = False
        store.update_webhook(tenant_id, hook["id"], **changes)
        if result.ok:
            return
        if result.retry:
            raise _Retry(result.summary, result.retry_after)
        raise _Fail(result.summary)

    def _deliver_connector(self, row: dict, provider: str) -> None:
        cls = registry.get(provider)
        if cls is None:
            raise _Skip(f"connector {provider} is not installed")
        conn = self.platform.store.get_connection(row["tenant_id"], provider)
        if not conn or conn["status"] != "connected":
            raise _Skip(f"{cls.display_name} is not connected")
        ctx = registry.ConnectorContext(self.platform, row["tenant_id"], user_label="event delivery")
        cls(ctx).handle_event(row["event"], row["payload"])

    def _deliver_handler(self, row: dict, name: str) -> None:
        with _handlers_lock:
            entry = _handlers.get(name)
        if entry is None:
            raise _Skip(f"handler {name} is not registered")
        entry[1](self.platform, row["tenant_id"], row["event"], row["payload"])


# ---------------------------------------------------------------------------- module-level convenience
def set_default(bus: EventBus | None) -> None:
    global _default_bus
    _default_bus = bus


def default_bus() -> EventBus | None:
    return _default_bus


def emit(tenant_id: str, event: str, payload: dict, *, dedupe_key: str | None = None) -> list[int]:
    """emit() on the running app's bus (the most recently created platform). Prefer platform.events.emit in code
    that has the platform at hand (tests create several apps)."""
    bus = _default_bus
    if bus is None:
        validate_payload(event, payload)
        log.warning("event %s emitted before the app started; dropped", event)
        return []
    return bus.emit(tenant_id, event, payload, dedupe_key=dedupe_key)
