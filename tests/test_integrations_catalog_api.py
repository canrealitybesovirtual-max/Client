"""GET /api/integrations/catalog and /api/integrations/catalog/mcp-config: the integrations catalog for signed-in
users (every role, session or API key), this server's MCP URL, ready-to-paste assistant settings; plus the
reserved `catalog` provider id. Runs on SQLite and, with TC_TEST_DATABASE_URL, on PostgreSQL."""
import gzip
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import connectors as registry
from app.connectors import Connector, TestResult, validate_connector_class
from tradecomply import integrations_guide
from tests.support import backend, login, make_app, signup  # noqa: F401


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        signup(c)
        yield c


def test_catalog_needs_a_sign_in(app):
    with TestClient(app) as c:
        assert c.get("/api/integrations/catalog").status_code == 401
        assert c.get("/api/integrations/catalog/mcp-config?ids=box").status_code == 401


def test_catalog_for_a_signed_in_owner(owner):
    r = owner.get("/api/integrations/catalog")
    assert r.status_code == 200
    body = r.json()
    cat = integrations_guide.load_catalog()
    assert [e["id"] for e in body["entries"]] == [e["id"] for e in cat["entries"]]
    assert body["entries"] == cat["entries"] and body["categories"] == cat["categories"]
    assert body["tradecomply"]["mcp_url"] == "http://testserver/mcp"              # this server, not the hosted one
    assert body["tradecomply"]["hosted_mcp_url"] == cat["tradecomply"]["mcp_url"]
    assert cat["tradecomply"]["mcp_url"] == "https://missed-call-recovery-n1c3.onrender.com/mcp"   # cache untouched
    assert body["config_ready_ids"] == integrations_guide.config_ready_ids() and "box" in body["config_ready_ids"]
    assert r.headers["cache-control"] == "private, max-age=300"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_catalog_is_gzipped_and_holds_no_secrets(owner):
    r = owner.get("/api/integrations/catalog", headers={"accept-encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip"
    raw = owner.get("/api/integrations/catalog", headers={"accept-encoding": "identity"}).content
    assert len(gzip.compress(raw)) < len(raw) / 3
    text = raw.decode()
    assert "tc_csrf" not in text
    assert not re.search(r"tck_[A-Za-z0-9_-]{12,}|whsec_[A-Za-z0-9+/=]{12,}|dg_live_[A-Za-z0-9]", text)


def test_every_role_and_api_keys_can_read_the_catalog(app, owner):
    r = owner.post("/api/users", json={"email": "field@acme.test", "role": "field"})
    field = TestClient(app)
    login(field, "field@acme.test", r.json()["temporary_password"])
    assert field.get("/api/integrations/catalog").status_code == 200
    assert field.get("/api/integrations/catalog/mcp-config?ids=slack").status_code == 200
    key = owner.post("/api/keys", json={"name": "Workato"}).json()["key"]
    with TestClient(app) as bot:
        r = bot.get("/api/integrations/catalog", headers={"authorization": f"Bearer {key}"})
        assert r.status_code == 200 and len(r.json()["entries"]) == len(integrations_guide.entries())


def test_mcp_config_uses_this_servers_mcp_url(owner):
    r = owner.get("/api/integrations/catalog/mcp-config", params={"ids": "box, docusign,nope"})
    assert r.status_code == 200
    body = r.json()
    servers = body["claude_code_mcp_json"]["mcpServers"]
    assert list(servers) == ["tradecomply", "box", "docusign"]
    assert servers["tradecomply"]["url"] == "http://testserver/mcp"
    assert servers["tradecomply"]["headers"] == {"Authorization": "Bearer ${TRADECOMPLY_API_KEY}"}
    assert json.loads(body["claude_code_mcp_json_text"]) == body["claude_code_mcp_json"]
    assert [s["id"] for s in body["skipped"]] == ["nope"]
    assert owner.get("/api/integrations/catalog/mcp-config", params={"ids": " , "}).status_code == 422
    too_many = ",".join(f"x{i}" for i in range(30))
    r = owner.get("/api/integrations/catalog/mcp-config", params={"ids": too_many})
    assert r.status_code == 422 and "at most" in r.json()["detail"]
    assert owner.get("/api/integrations/catalog/mcp-config", params={"ids": "a" * 1001}).status_code == 422


def test_catalog_routes_win_over_the_provider_routes(owner):
    # /api/integrations/{provider} would answer 404 "No such integration" for "catalog"
    assert owner.get("/api/integrations/catalog").json()["schema_version"] == 1
    assert "catalog" in registry.RESERVED_IDS

    class CatalogConnector(Connector):
        provider = "catalog"
        display_name = "Catalog"
        catalog_id = "catalog"
        description = "d"
        docs_url = "https://example.com/docs"

        def test(self):
            return TestResult(True, "ok")

    assert any("reserved" in e for e in validate_connector_class(CatalogConnector))


def test_installed_connectors_map_to_a_catalog_entry(owner):
    """The Integrations page puts a connector's Connect panel on the catalog card whose id equals its catalog_id,
    or whose builtin.providers lists its provider id. A connector matching neither still works (it is listed under
    "More connections on this server"), but should get a catalog entry: this test names it."""
    registry.discover(force=True)
    by_id = {e["id"]: e for e in integrations_guide.entries()}
    listed = {p for e in by_id.values() if e["builtin"] for p in e["builtin"]["providers"]}
    for cls in registry.all_connectors():
        if cls.__module__.startswith("app.connectors.") and not cls.__module__.endswith("._template"):
            assert cls.catalog_id in by_id or cls.provider in listed, (
                f"connector {cls.provider!r} (catalog_id {cls.catalog_id!r}) has no entry in "
                "tradecomply/data/integrations.json")
    procore = next(p for p in owner.get("/api/integrations").json()["providers"] if p["provider"] == "procore") \
        if registry.get("procore") else None
    assert procore is None or procore["catalog_id"] == "procore"
