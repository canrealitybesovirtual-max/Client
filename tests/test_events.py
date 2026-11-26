"""app/events.py: emit records one outbound row per subscriber and returns at once; delivery (background thread or
drain()) with retries on the Standard Webhooks schedule, Retry-After, permanent failures, skips, dedupe, leases,
several workers, and an emit that never breaks its caller."""
import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors as registry
from app import events, webhooks
from app.connectors import PermanentError
from app.connectors._template import ExampleApiKeyConnector
from app.crypto import connection_context, webhook_context
from tests.support import backend, make_app, mock_http  # noqa: F401

EVENT = "sub_noncompliant"
PAYLOAD = {"title": "Sub has no workers' comp on file", "severity": "warning", "link": "/#/subs/1"}


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path, app_url="https://app.example.com")


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def tid(platform):
    return platform.store.create_tenant("Acme", None, None)["id"]


@pytest.fixture
def handlers():
    names = []

    def add(fn, evts=EVENT, name=None):
        events.subscribe(evts, name=name or f"test.h{len(names)}")(fn)
        names.append(fn.subscription_name)
        return fn
    yield add
    for n in names:
        events.unsubscribe(n)


def add_hook(platform, tid, url="https://receiver.example.com/hook", kind="generic", evts=(EVENT,), secret=None):
    box, ctx = platform.secret_box, webhook_context(tid)
    return platform.store.create_webhook(tid, name="hook", url=box.encrypt(url, ctx), kind=kind, events=list(evts),
                                         signing_secret_blob=box.encrypt(secret, ctx) if secret else None)


def later(seconds):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------- emitting
def test_no_subscribers_means_nothing_recorded(platform, tid):
    assert platform.events.emit(tid, EVENT, PAYLOAD) == []
    assert platform.store.list_outbound(tid) == []


@pytest.mark.parametrize("event,payload,match", [
    ("no_such_event", PAYLOAD, "unknown event"),
    ("test", PAYLOAD, "unknown event"),
    (EVENT, {}, "title"),
    (EVENT, {"title": "x" * 201}, "title"),
    (EVENT, {"title": "x", "severity": "panic"}, "severity"),
    (EVENT, {"title": "x", "link": "https://evil.example.com/"}, "link"),
    (EVENT, {"title": "x", "link": "//evil.example.com/"}, "link"),
    (EVENT, {"title": "x", "due_date": "12/01/2026"}, "due_date"),
    (EVENT, {"title": "x", "data": object()}, "JSON"),
    (EVENT, {"title": "x", "blob": "y" * 40000}, "too large"),
    (EVENT, ["title"], "dict"),
])
def test_bad_events_are_programming_errors(platform, tid, event, payload, match):
    with pytest.raises(ValueError, match=match):
        platform.events.emit(tid, event, payload)


def test_emit_fans_out_to_webhooks_connectors_and_handlers(platform, tid, handlers):
    got = []
    fn = handlers(lambda p, t, e, payload: got.append((p, t, e, payload["title"])))
    hook = add_hook(platform, tid)
    add_hook(platform, tid, evts=("payroll_issue",))                # not subscribed to this event
    registry.register(ExampleApiKeyConnector)
    try:
        inst = ExampleApiKeyConnector(registry.ConnectorContext(platform, tid))
        inst.save(status="connected", secrets={"api_key": "k"})
        ids = platform.events.emit(tid, EVENT, PAYLOAD)
        rows = {r["target"]: r for r in platform.store.list_outbound(tid)}
        assert len(ids) == 3 and set(rows) == {f"webhook:{hook['id']}", "connector:example_apikey",
                                               f"handler:{fn.subscription_name}"}
        assert all(r["status"] == "pending" and r["payload"]["severity"] == "warning" for r in rows.values())
        seen = mock_http(platform, lambda r: httpx.Response(201 if r.url.path == "/v1/alerts" else 204))
        assert platform.events.drain() == 3
        assert got == [(platform, tid, EVENT, PAYLOAD["title"])]
        assert {r.url.host for r in seen} == {"receiver.example.com", "api.example.com"}
        assert {r["status"] for r in platform.store.list_outbound(tid)} == {"delivered"}
    finally:
        registry.unregister("example_apikey")


def test_generic_delivery_body_headers_and_stable_message_id(platform, tid):
    secret = webhooks.new_signing_secret()
    add_hook(platform, tid, secret=secret)
    answers = iter([httpx.Response(503), httpx.Response(200)])
    seen = mock_http(platform, lambda r: next(answers))
    platform.events.emit(tid, EVENT, {**PAYLOAD, "sub_id": 1})
    platform.events.drain()
    platform.events.drain(now=later(60))
    first, second = seen
    assert first.headers["webhook-id"] == second.headers["webhook-id"]          # same message, retried
    assert webhooks.verify(secret, second.headers, second.content)
    body = json.loads(second.content)
    assert body["type"] == EVENT and body["data"]["sub_id"] == 1 and body["data"]["link"] == "/#/subs/1"
    assert "." not in first.headers["webhook-id"]


def test_slack_and_teams_messages_link_into_the_app(platform, tid):
    add_hook(platform, tid, url="https://hooks.slack.com/services/T/B/X", kind="slack")
    add_hook(platform, tid, url="https://prod-00.westus.logic.azure.com/workflows/x", kind="teams")
    seen = mock_http(platform, lambda r: httpx.Response(200, text="ok"))
    platform.events.emit(tid, EVENT, {**PAYLOAD, "due_date": "2026-12-01"})
    assert platform.events.drain() == 2
    slack = json.loads(next(r for r in seen if r.url.host == "hooks.slack.com").content)
    assert "<https://app.example.com/#/subs/1|Open in TradeComply>" in slack["text"]
    assert "Due: 2026-12-01" in slack["text"]
    teams = json.loads(next(r for r in seen if r.url.host != "hooks.slack.com").content)
    assert teams["attachments"][0]["content"]["actions"][0]["url"] == "https://app.example.com/#/subs/1"


# ---------------------------------------------------------------------------- retries and outcomes
def test_retry_schedule_then_success(platform, tid):
    add_hook(platform, tid)
    answers = iter([httpx.Response(500), httpx.Response(502), httpx.Response(200)])
    mock_http(platform, lambda r: next(answers))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    platform.events.drain()
    row = platform.store.get_outbound(tid, oid)
    assert row["status"] == "pending" and row["attempts"] == 1 and "500" in row["last_error"]
    wait = (datetime.fromisoformat(row["next_attempt"]) - datetime.now(timezone.utc)).total_seconds()
    assert 2 <= wait <= 7                                    # first retry after about 5 s
    assert platform.events.drain() == 0                      # not due yet
    platform.events.drain(now=later(10))
    row = platform.store.get_outbound(tid, oid)
    wait = (datetime.fromisoformat(row["next_attempt"]) - datetime.now(timezone.utc)).total_seconds()
    assert row["attempts"] == 2 and 260 <= wait <= 340      # then about 5 minutes
    platform.events.drain(now=later(400))
    assert platform.store.get_outbound(tid, oid)["status"] == "delivered"


def test_retry_after_is_respected(platform, tid):
    add_hook(platform, tid)
    mock_http(platform, lambda r: httpx.Response(429, headers={"retry-after": "120"}))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    platform.events.drain()
    row = platform.store.get_outbound(tid, oid)
    wait = (datetime.fromisoformat(row["next_attempt"]) - datetime.now(timezone.utc)).total_seconds()
    assert row["status"] == "pending" and wait >= 115


def test_gives_up_after_the_last_attempt(platform, tid):
    add_hook(platform, tid)
    mock_http(platform, lambda r: httpx.Response(503))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    for i in range(events.MAX_ATTEMPTS + 2):
        platform.events.drain(now=later(10 + sum(events.RETRY_DELAYS_S[:i]) * 1.2))
    row = platform.store.get_outbound(tid, oid)
    assert row["status"] == "failed" and row["attempts"] == events.MAX_ATTEMPTS and "gave up" in row["last_error"]


@pytest.mark.parametrize("kind,url,status,body,disabled", [
    ("slack", "https://hooks.slack.com/services/T/B/X", 404, "no_service", True),
    ("slack", "https://hooks.slack.com/services/T/B/X", 400, "invalid_payload", False),
    ("teams", "https://prod-00.westus.logic.azure.com/workflows/x", 400, "", False),
    ("generic", "https://receiver.example.com/hook", 410, "", True),
    ("generic", "https://receiver.example.com/hook", 301, "", False),
])
def test_permanent_refusals_fail_without_retry(platform, tid, kind, url, status, body, disabled):
    hook = add_hook(platform, tid, url=url, kind=kind)
    mock_http(platform, lambda r: httpx.Response(status, text=body, headers={"location": "https://x.example.com/"}))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    platform.events.drain()
    row = platform.store.get_outbound(tid, oid)
    assert row["status"] == "failed" and row["attempts"] == 1
    h = platform.store.get_webhook(tid, hook["id"])
    assert h["enabled"] is (not disabled) and h["last_status"].startswith(str(status))


def test_generic_4xx_is_retried_per_standard_webhooks(platform, tid):
    add_hook(platform, tid)
    mock_http(platform, lambda r: httpx.Response(400))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    platform.events.drain()
    assert platform.store.get_outbound(tid, oid)["status"] == "pending"


def test_unreachable_and_private_receivers(platform, tid):
    down = add_hook(platform, tid, url="https://down.example.com/hook")
    rebound = add_hook(platform, tid, url="https://rebound.example.com/hook")

    def handler(r):
        raise httpx.ConnectError("refused", request=r)
    mock_http(platform, handler, hosts={"rebound.example.com": "10.0.0.8"})   # its DNS now points inside
    platform.events.emit(tid, EVENT, PAYLOAD)
    platform.events.drain()
    by = {r["target"]: r for r in platform.store.list_outbound(tid)}
    unreachable, private = by[f"webhook:{down['id']}"], by[f"webhook:{rebound['id']}"]
    assert unreachable["status"] == "pending" and "Could not connect to down.example.com" in unreachable["last_error"]
    assert private["status"] == "failed" and "private" in private["last_error"]


def test_deleted_or_disabled_targets_are_skipped(platform, tid, handlers):
    h1, h2 = add_hook(platform, tid), add_hook(platform, tid)
    handlers(lambda *a: None, name="test.gone")
    platform.events.emit(tid, EVENT, PAYLOAD)
    platform.store.delete_webhook(tid, h1["id"])
    platform.store.update_webhook(tid, h2["id"], enabled=False)
    events.unsubscribe("test.gone")
    mock_http(platform, lambda r: httpx.Response(200))
    platform.events.drain()
    assert {r["status"] for r in platform.store.list_outbound(tid)} == {"skipped"}


def test_connector_outcomes(platform, tid):
    class Flaky(ExampleApiKeyConnector):
        provider = "flaky_test"
        calls = []

        def handle_event(self, event, payload):
            self.calls.append(payload["title"])
            if payload["title"] == "permanent":
                raise PermanentError("the job was deleted")
            if payload["title"] == "boom":
                raise RuntimeError("secret detail that must not be stored")
    registry.register(Flaky)
    try:
        Flaky(registry.ConnectorContext(platform, tid)).save(status="connected")
        a = platform.events.emit(tid, EVENT, {"title": "permanent"})
        b = platform.events.emit(tid, EVENT, {"title": "boom"})
        c = platform.events.emit(tid, EVENT, {"title": "fine"})
        platform.events.drain()
        st = platform.store
        assert st.get_outbound(tid, a[0])["status"] == "failed"
        retry = st.get_outbound(tid, b[0])
        assert retry["status"] == "pending" and retry["last_error"] == "RuntimeError"
        assert st.get_outbound(tid, c[0])["status"] == "delivered"
        Flaky(registry.ConnectorContext(platform, tid)).save(status="disconnected")
        platform.events.drain(now=later(30))
        assert st.get_outbound(tid, b[0])["status"] == "skipped"
        assert platform.events.emit(tid, EVENT, {"title": "after"}) == []      # no longer a subscriber
    finally:
        registry.unregister("flaky_test")


def test_dedupe_key_delivers_once(platform, tid):
    add_hook(platform, tid)
    assert len(platform.events.emit(tid, EVENT, PAYLOAD, dedupe_key="sub:1:wc")) == 1
    assert platform.events.emit(tid, EVENT, PAYLOAD, dedupe_key="sub:1:wc") == []
    assert len(platform.events.emit(tid, EVENT, PAYLOAD, dedupe_key="sub:2:wc")) == 1


def test_handler_permanent_error_and_retry(platform, tid, handlers):
    def h(p, t, e, payload):
        raise PermanentError("nope") if payload["title"] == "p" else ValueError("x")
    handlers(h)
    (a,) = platform.events.emit(tid, EVENT, {"title": "p"})
    (b,) = platform.events.emit(tid, EVENT, {"title": "r"})
    platform.events.drain()
    assert platform.store.get_outbound(tid, a)["status"] == "failed"
    assert platform.store.get_outbound(tid, b)["status"] == "pending"


# ---------------------------------------------------------------------------- never blocking, never breaking
def test_emit_survives_a_database_failure(platform, tid, caplog, monkeypatch):
    add_hook(platform, tid)

    def broken(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(platform.store, "enqueue_outbound", broken)
    with caplog.at_level(logging.ERROR, logger="tradecomply.events"):
        assert platform.events.emit(tid, EVENT, PAYLOAD) == []
    assert "could not record event" in caplog.text


def test_emit_returns_before_a_slow_receiver_answers(tmp_path):
    app = make_app(tmp_path, events_background=True)
    platform = app.state.platform
    tid = platform.store.create_tenant("A", None, None)["id"]
    add_hook(platform, tid)
    done = threading.Event()

    def slow(r):
        time.sleep(1.0)
        done.set()
        return httpx.Response(200)
    mock_http(platform, slow)
    with TestClient(app):                      # the app's startup starts the worker
        assert platform.events.running
        t0 = time.monotonic()
        (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
        assert time.monotonic() - t0 < 0.5
        assert done.wait(10)
        for _ in range(100):
            if platform.store.get_outbound(tid, oid)["status"] == "delivered":
                break
            time.sleep(0.05)
        assert platform.store.get_outbound(tid, oid)["status"] == "delivered"
    assert not platform.events.running         # shutdown stopped it


def test_two_workers_deliver_each_event_once(platform, tid, tmp_path, handlers):
    got, lock = [], threading.Lock()

    def h(p, t, e, payload):
        with lock:
            got.append(payload["title"])
    handlers(h)
    other = events.EventBus(platform, background=False)       # a second instance on the same database
    for i in range(40):
        platform.events.emit(tid, EVENT, {"title": f"e{i}"})
    gate = threading.Barrier(2)

    def run(bus):
        gate.wait()
        bus.drain()
    threads = [threading.Thread(target=run, args=(b,)) for b in (platform.events, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(f"e{i}" for i in range(40))


def test_a_crashed_delivery_is_retried_after_its_lease(platform, tid, handlers):
    got = []
    handlers(lambda p, t, e, payload: got.append(1))
    (oid,) = platform.events.emit(tid, EVENT, PAYLOAD)
    assert len(platform.store.claim_outbound(lease_s=300)) == 1         # a worker took it and died
    assert platform.events.drain() == 0
    platform.events.drain(now=later(400))
    assert got == [1] and platform.store.get_outbound(tid, oid)["attempts"] == 2


def test_aemit_from_async_code_and_the_module_level_emit(platform, tid, handlers):
    handlers(lambda *a: None)
    assert events.default_bus() is platform.events
    assert len(asyncio.run(platform.events.aemit(tid, EVENT, PAYLOAD))) == 1
    assert len(events.emit(tid, EVENT, PAYLOAD)) == 1


def test_register_event_and_subscribe_validation(handlers):
    with pytest.raises(ValueError):
        events.subscribe("no_such_event")
    with pytest.raises(ValueError):
        events.register_event("Bad Name", "x")
    events.register_event("permit_expiring_test", "A permit is about to expire.")
    try:
        handlers(lambda *a: None, evts="permit_expiring_test")
    finally:
        events.EVENTS.pop("permit_expiring_test", None)


def test_connector_secret_blob_never_reaches_the_queue(platform, tid):
    """Payloads are what the emitter passed: the queue never copies connection secrets."""
    registry.register(ExampleApiKeyConnector)
    try:
        ExampleApiKeyConnector(registry.ConnectorContext(platform, tid)).save(status="connected",
                                                                              secrets={"api_key": "sk-x"})
        platform.events.emit(tid, EVENT, PAYLOAD)
        blob = platform.store.get_connection(tid, "example_apikey", with_secret=True)["secret_blob"]
        assert platform.secret_box.decrypt_json(blob, connection_context(tid, "example_apikey")) == {"api_key": "sk-x"}
        assert "sk-x" not in json.dumps(platform.store.list_outbound(tid))
    finally:
        registry.unregister("example_apikey")
