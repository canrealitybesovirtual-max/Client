"""The search index lifecycle (app/search.py, spec 5.3): one per process, built in the background, /health status,
fallback while it builds, related checks per rule."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import search
from tests.support import make_app, signup


@pytest.fixture
def fresh():
    """A process with no index yet (restored to a built one afterwards, for the other test files)."""
    search.reset_for_tests()
    yield
    search.reset_for_tests()


def wait_for(status, timeout=60):
    end = time.time() + timeout
    while time.time() < end and search.status() not in status:
        time.sleep(0.05)
    return search.status()


def test_one_index_per_process(tmp_path, fresh):
    a, b = make_app(tmp_path / "a"), make_app(tmp_path / "b")
    first = search.build_now(a.state.platform)
    assert search.build_now(a.state.platform) is first            # building twice builds once
    assert search.build_now(b.state.platform) is first            # a second app shares it
    assert search.index() is first and search.ready()
    assert len(first.index.docs) >= 3200


def test_make_app_does_not_warm_and_nothing_builds_until_asked(tmp_path, fresh):
    app = make_app(tmp_path)
    assert app.state.platform.settings.search_warm is False
    with TestClient(app) as c:
        assert c.get("/health").json()["search"] == "off"
        signup(c)
        assert c.get("/health").json()["search"] == "off"
        r = c.post("/api/search", json={"q": "prelim"})              # the first search starts the build
        assert r.status_code == 200 and r.json()["ready"] is False
        assert r.json()["groups"]                                   # fallback: rules, checks and pages still found
        assert wait_for({"ready"}) == "ready"
        assert c.get("/health").json()["search"] == "ready"
        assert c.post("/api/search", json={"q": "prelim"}).json()["ready"] is True


def test_warm_at_startup_and_health_answers_before_it_is_ready(tmp_path, fresh, monkeypatch):
    gate = threading.Event()
    real = search._build

    def slow(specs, modules):
        gate.wait(30)
        return real(specs, modules)
    monkeypatch.setattr(search, "_build", slow)
    app = make_app(tmp_path, search_warm=True)
    with TestClient(app) as c:
        h = c.get("/health")
        assert h.status_code == 200 and h.json()["search"] == "building"
        signup(c)
        r = c.post("/api/help", json={"question": "When is my prelim due?"})
        assert r.status_code == 200
        body = r.json()
        assert body["route"] == "search" and [b["code"] for b in body["blocks"]] == ["INDEX_WARMING"]
        gate.set()
        assert wait_for({"ready"}) == "ready"
        assert c.get("/health").json()["search"] == "ready"


def test_the_build_runs_on_a_daemon_thread(tmp_path, fresh, monkeypatch):
    gate = threading.Event()

    def stuck(specs, modules):
        gate.wait(30)
        raise RuntimeError("released")
    monkeypatch.setattr(search, "_build", stuck)
    app = make_app(tmp_path, search_warm=True)
    t0 = time.perf_counter()
    with TestClient(app) as c:
        assert c.get("/health").json()["search"] == "building"
        assert search.STATE.thread.daemon is True
    assert time.perf_counter() - t0 < 10                            # shutdown never waits for the build
    gate.set()


def test_a_failed_build_is_logged_once_and_search_stays_in_fallback(tmp_path, fresh, monkeypatch, caplog):
    def boom(specs, modules):
        raise RuntimeError("disk full")
    monkeypatch.setattr(search, "_build", boom)
    app = make_app(tmp_path, search_warm=True)
    with TestClient(app) as c:
        assert wait_for({"failed"}) == "failed"
        assert c.get("/health").json()["search"] == "failed"
        signup(c)
        r = c.post("/api/search", json={"q": "trench permit"})
        assert r.status_code == 200 and r.json()["ready"] is False and r.json()["groups"]
        assert c.post("/api/help", json={"question": "When is my prelim due?"}).status_code == 200
        assert search.status() == "failed"                          # no retry loop
    errors = [r for r in caplog.records if r.name == "tradecomply.search" and r.levelname == "ERROR"]
    assert len(errors) == 1 and "RuntimeError" in errors[0].getMessage()


def test_related_checks(tmp_path):
    from tradecomply import search as engine
    app = make_app(tmp_path)
    built = search.build_now(app.state.platform)
    # named in payments.py and project_setup.py; anchored to the lien check (server.py, which names no rule ids) and
    # the setup checklist: only the setup checklist is left
    assert built.related["prelim-notice-20-day-deadline"] == ["project_setup_checklist"]
    assert built.related["adu-detached-max-1200-attached-50pct"] == ["adu_rules"]
    assert built.related["permit-trench-excavation-5ft"] == ["check_cal_osha_permits"]
    anchored = {}
    for g in engine.vocab()["groups"]:
        for rid in g["anchors"]:
            anchored.setdefault(rid, set()).update(g["tools"])
    for rid, tools in built.related.items():
        assert tools, rid
        assert not {"mcp_client_config", "who_must_sign", "integrations_for_task", "list_professional_types"} & set(
            tools)
        if rid in anchored:
            assert set(tools) <= anchored[rid], rid
    # an anchored rule whose modules hold none of its groups' checks gets no related checks at all
    assert "lien-deadline-subcontractor-supplier" not in built.related         # payments.py; lien group -> server.py
    assert 1000 < len(built.related) < 3056


def test_no_gc_freeze():
    import inspect
    assert "gc.freeze" not in inspect.getsource(search)
