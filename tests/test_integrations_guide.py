"""Integrations catalog (tradecomply/data/integrations.json) and the two read-only MCP tools built on it:
integrations_for_task and mcp_client_config."""
import asyncio
import json
import re

import pytest
from fastmcp import Client, FastMCP

from tradecomply import integrations_guide as g

BUILTIN_IDS = {"procore", "docusign", "slack", "microsoft-teams", "generic-webhooks", "twilio", "calendar-feed",
               "oracle-primavera", "microsoft-project", "splunk", "oidc-sso"}
PANELS = {"connector", "webhooks", "calendar_feed", "schedule_import"}
THIS_SERVER = "https://tradecomply.example.test"       # stands in for the operator's APP_URL


@pytest.fixture(autouse=True)
def operator_env(monkeypatch):
    """Every test runs as a server whose operator set APP_URL; tests of the other cases change it."""
    monkeypatch.setenv("APP_URL", THIS_SERVER)
    for name in ("RENDER_EXTERNAL_URL", "SUPPORT_EMAIL", "SUPPORT_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def server_tools():
    import server

    async def run():
        async with Client(server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(run())


# ---------------------------------------------------------------------------- catalog shape
def test_catalog_entries_are_complete_and_consistent(server_tools):
    cat = g.load_catalog()
    assert cat["checked_on"] == "2026-11-26" and cat["tradecomply"]["mcp_path"] == "/mcp"
    assert "mcp_url" not in cat["tradecomply"]                 # no server address in the product: each install is its own
    cats = {c["id"] for c in cat["categories"]}
    kinds = {k["id"] for k in cat["integration_types"]}
    assert kinds == {"builtin", "ai_assistant", "import_export", "reference"}
    ids = [e["id"] for e in cat["entries"]]
    assert len(ids) == len(set(ids)) and len(ids) >= 30
    for e in cat["entries"]:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", e["id"]), e["id"]
        assert e["category"] in cats and e["integration"] in kinds, e["id"]
        assert e["name"] and e["vendor"] and e["summary"] and e["tradecomply_role"], e["id"]
        assert e["official_url"].startswith("https://"), e["id"]
        assert all(link["url"].startswith("https://") for link in e["links"]), e["id"]
        assert e["sources"] and all((s["url"] or "").startswith("https://") or s.get("path") for s in e["sources"])
        assert e["uses"] and all(u["text"] for u in e["uses"]), e["id"]
        assert e["status"]["label"] and isinstance(e["caveats"], list), e["id"]
        # every TradeComply tool a use names really exists, and `tools` is their union
        union = []
        for u in e["uses"]:
            assert set(u["tools"]) <= server_tools, (e["id"], set(u["tools"]) - server_tools)
            assert all(t in u["text"] for t in u["tools"])
            union += [t for t in u["tools"] if t not in union]
        assert e["tools"] == union, e["id"]
        for s in e["mcp"]["servers"]:
            for k in ("url", "url_template"):
                assert s[k] is None or s[k].startswith("https://"), (e["id"], s[k])
            assert isinstance(s["confirmed"], bool) and isinstance(s["auth_needed"], bool)
            assert all(re.fullmatch(r"[^${}]*(\$\{[A-Z][A-Z0-9_]*\}[^${}]*)*", v) for v in s["headers"].values())
        if e["gov"]:
            assert e["gov"]["supported"] in ("yes", "partial", "no", "unconfirmed", "not_applicable")
            assert e["gov"]["label"] and e["gov"]["note"], e["id"]
        if not e["recommended"]:
            assert e["not_recommended_reason"], e["id"]


def test_builtin_connectors_being_built_are_in_the_catalog():
    by_id = {e["id"]: e for e in g.entries()}
    assert BUILTIN_IDS <= set(by_id)
    for i in BUILTIN_IDS:
        e = by_id[i]
        assert e["integration"] == "builtin" and e["builtin"]["panel"] in PANELS and e["builtin"]["what_it_does"]
    assert by_id["slack"]["builtin"]["webhook_kind"] == "slack"
    assert by_id["microsoft-teams"]["builtin"]["webhook_kind"] == "teams"
    assert by_id["generic-webhooks"]["builtin"]["webhook_kind"] == "generic"
    assert by_id["calendar-feed"]["builtin"]["panel"] == "calendar_feed"
    assert by_id["oracle-primavera"]["builtin"]["panel"] == by_id["microsoft-project"]["builtin"]["panel"] == "schedule_import"
    assert "procore" in by_id["procore"]["builtin"]["providers"]
    # non-builtin entries carry no connect panel
    assert all(e["builtin"] is None for e in g.entries() if e["integration"] != "builtin")


def test_one_entry_per_product_with_gov_variants_folded_in():
    names = [e["name"].lower() for e in g.entries()]
    for product in ("box", "docusign", "splunk", "twilio", "slack"):
        assert sum(1 for n in names if n.startswith(product)) == 1, product
    by_id = {e["id"]: e for e in g.entries()}
    assert {s["key"] for s in by_id["docusign"]["mcp"]["servers"]} == {"docusign", "docusign-demo"}
    assert {s["key"] for s in by_id["google-workspace"]["mcp"]["servers"]} >= {"gmail", "drive"}
    assert by_id["slack"]["gov"]["name"] == "GovSlack" and by_id["slack"]["gov"]["supported"] == "no"
    assert by_id["azure"]["gov"]["supported"] == "yes" and "AzureUSGovernment" in by_id["azure"]["gov"]["note"]
    assert by_id["box"]["gov"]["url"] == "https://www.box.com/fedramp"


def test_unconfirmed_and_self_reported_claims_stay_out():
    text = json.dumps(g.load_catalog())
    for claim in ("91.8%", "Fortune 500", "7,500", "400+ tools", "1,000+", "100+ tools", "HIPAA"):
        assert claim not in text, claim
    assert not re.search(r"\bI (could|did|found|read)\b|this session|user_claims", text)
    # the third-party Ivalua URL is never offered as an endpoint
    ivalua = g.get_entry("ivalua")
    assert ivalua["mcp"]["servers"] == [] and "portal.ivalua.com/mcp" not in ivalua["mcp"]["note"]


def test_confirmed_endpoints_are_the_documented_ones():
    urls = {s["url"] for e in g.entries() for s in e["mcp"]["servers"] if g.usable_remote(s)}
    for expected in ("https://mcp.box.com", "https://mcp.docusign.com/mcp", "https://mcp-d.docusign.com/mcp",
                     "https://gmailmcp.googleapis.com/mcp/v1", "https://drivemcp.googleapis.com/mcp/v1",
                     "https://mcp.slack.com/mcp", "https://mcp.zoom.us/mcp/zoom/streamable",
                     "https://mcp.twilio.com/docs", "https://mcp.atlassian.com/v2/mcp",
                     "https://app.procore.com/rest/v1.0/mcp"):
        assert expected in urls, expected
    # unconfirmed or per-account endpoints never count as ready
    assert "https://mcp.secureframe.com/" not in urls
    assert "https://service.api.aisecurity.paloaltonetworks.com/mcp" not in urls
    assert all("<" not in u and "{" not in u for u in urls)


# ---------------------------------------------------------------------------- integrations_for_task
def test_task_search_finds_the_right_products():
    r = g.integrations_for_task("send home improvement contracts for signature")
    assert r.matches[0].id == "docusign" and r.matches[0].integration == "builtin"
    assert "generate_home_improvement_contract" in r.matches[0].tradecomply_tools
    assert any("Integrations page" in s for s in r.next_steps)
    assert r.checked_on == "2026-11-26" and "unconfirmed" in r.note
    assert g.integrations_for_task("check the subs in our Procore directory").matches[0].id == "procore"
    assert [m.id for m in g.integrations_for_task("import our P6 schedule").matches][:2] == \
        ["oracle-primavera", "microsoft-project"]
    assert g.integrations_for_task("text my foremen when wildfire smoke is bad").matches[0].id == "twilio"
    assert {m.id for m in g.integrations_for_task("single sign-on with Okta").matches} >= {"oidc-sso", "okta"}
    assert g.integrations_for_task("put lien deadlines on outlook calendar").matches[0].id == "calendar-feed"
    assert g.integrations_for_task("search jira tickets").matches[0].id == "atlassian-rovo"
    m = g.integrations_for_task("check the subs in our Procore directory").matches[0]
    assert m.uses and all(set(u.tradecomply_tools) <= set(g.get_entry("procore")["tools"]) for u in m.uses)
    assert any(s.url == "https://app.procore.com/rest/v1.0/mcp" for s in m.mcp_servers)


def test_task_search_limits_and_errors():
    assert len(g.integrations_for_task("alerts", limit=2).matches) <= 2
    r = g.integrations_for_task("zzzz qqqq")
    assert r.matches == [] and "No catalog product matched" in r.next_steps[0]
    with pytest.raises(ValueError, match="must not be empty"):
        g.integrations_for_task("   ")
    with pytest.raises(ValueError, match="too long"):
        g.integrations_for_task("x" * 2001)


def test_not_recommended_products_are_never_suggested():
    for task in ("fedramp documentation", "agent security guardrails python library", "fedramp 20x"):
        ids = {m.id for m in g.integrations_for_task(task, limit=15).matches}
        assert not ids & {"bulwark-agent-security", "fedramp-docs-mcp", "fedramp-20x-mcp"}, task


def test_gov_cloud_filter_excludes_products_without_a_government_version():
    r = g.integrations_for_task("post alerts to slack", gov_cloud_required=True)
    assert "slack" not in {m.id for m in r.matches}
    slack = next(x for x in r.excluded_for_gov_cloud if x.id == "slack")
    assert "GovSlack" in slack.reason
    assert all(m.gov_cloud and m.gov_cloud.supported in ("yes", "partial", "not_applicable") for m in r.matches)
    assert any("FedRAMP" in s for s in r.next_steps)
    r = g.integrations_for_task("Azure government enclave key vault", gov_cloud_required=True)
    assert r.matches[0].id == "azure"


# ---------------------------------------------------------------------------- mcp_client_config
def test_config_combines_tradecomply_with_confirmed_remote_endpoints():
    r = g.mcp_client_config(["box", "docusign"])
    servers = r.claude_code_mcp_json["mcpServers"]
    assert list(servers) == ["tradecomply", "box", "docusign"]
    assert servers["tradecomply"] == {"type": "http", "url": THIS_SERVER + "/mcp",
                                      "headers": {"Authorization": "Bearer ${TRADECOMPLY_API_KEY}"}}
    assert servers["box"] == {"type": "http", "url": "https://mcp.box.com"}
    assert servers["docusign"]["url"] == "https://mcp.docusign.com/mcp"          # production, not demo
    assert json.loads(r.claude_code_mcp_json_text) == r.claude_code_mcp_json
    assert "docusign/docusign-demo (Docusign MCP Server (demo))" in r.also_available
    assert all(s.auth_needed for s in r.servers) and {s.client_registration for s in r.servers} == {"admin_issued"}
    assert all("--client-id <CLIENT_ID>" in s.notes for s in r.servers)
    assert r.claude_code_commands[0].startswith("claude mcp add --transport http tradecomply https://")
    assert "$TRADECOMPLY_API_KEY" in r.claude_code_commands[0] and not any("${" in c for c in r.claude_code_commands)
    assert [c["url"] for c in r.claude_desktop_connectors] == ["https://mcp.box.com", "https://mcp.docusign.com/mcp"]
    desk = r.claude_desktop_config["mcpServers"]["tradecomply"]
    assert desk["command"] == "npx" and "mcp-remote" in desk["args"]
    assert desk["env"]["TC_AUTH"] == "Bearer <YOUR_TRADECOMPLY_API_KEY>"
    assert [v.name for v in r.env_vars] == ["TRADECOMPLY_API_KEY"] and r.skipped == []


def test_config_header_auth_uses_env_references_never_values():
    r = g.mcp_client_config(["procore", "google-cloud", "twilio"])
    servers = r.claude_code_mcp_json["mcpServers"]
    assert servers["procore-ai-edge"]["headers"] == {"Authorization": "Bearer ${DATAGRID_API_KEY}"}
    assert servers["google-maps-grounding-lite"]["headers"] == {"X-Goog-Api-Key": "${GOOGLE_MAPS_API_KEY}"}
    env = {v.name for v in r.env_vars}
    assert {"TRADECOMPLY_API_KEY", "DATAGRID_API_KEY", "GOOGLE_MAPS_API_KEY", "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_OAUTH_ACCESS_TOKEN"} <= env
    referenced = set(re.findall(r"\$\{([A-Z0-9_]+)\}", r.claude_code_mcp_json_text))
    assert referenced <= env
    twilio = next(s for s in r.servers if s.url == "https://mcp.twilio.com/docs")
    assert twilio.auth_needed is False
    assert any('--header "X-Goog-Api-Key: $GOOGLE_MAPS_API_KEY"' in c for c in r.claude_code_commands)
    # the Twilio Alpha server needs credentials on its command line: never put into a config
    assert "twilio-alpha" not in r.claude_code_mcp_json_text


def test_config_explains_every_selection_it_cannot_add():
    r = g.mcp_client_config(["servicenow", "okta", "kahua", "secureframe", "bulwark-agent-security", "google",
                             "docusign/nope", "salesforce/sobject-all", "box", "box"])
    skipped = {s.id: s for s in r.skipped}
    assert skipped["servicenow/quickstart"].url_template.startswith("https://<instance>.service-now.com/")
    assert skipped["okta/okta-oss"].command == "uvx okta-mcp-server"
    assert "No confirmed remote MCP endpoint" in skipped["kahua"].reason
    assert "not confirmed" in skipped["secureframe/secureframe"].reason
    assert skipped["bulwark-agent-security"].reason.startswith("Not recommended")
    assert "google-workspace" in skipped["google"].reason and "google-cloud" in skipped["google"].reason
    assert "docusign/docusign" in skipped["docusign/nope"].reason
    names = list(r.claude_code_mcp_json["mcpServers"])
    assert names == ["tradecomply", "salesforce-sobject-all", "box"]       # duplicates collapse


def test_config_only_tradecomply_when_nothing_selectable_and_url_checks():
    r = g.mcp_client_config(["kahua"])
    assert list(r.claude_code_mcp_json["mcpServers"]) == ["tradecomply"]
    assert r.notes[0].startswith("Only TradeComply was added")
    local = g.mcp_client_config([], tradecomply_mcp_url="http://localhost:8000/mcp")
    assert local.claude_code_mcp_json["mcpServers"]["tradecomply"]["url"] == "http://localhost:8000/mcp"
    for bad in ("javascript:alert(1)", "http://example.com/mcp", "https://a b/mcp", "ftp://x/mcp"):
        with pytest.raises(ValueError, match="tradecomply_mcp_url"):
            g.mcp_client_config(["box"], tradecomply_mcp_url=bad)
    with pytest.raises(ValueError, match="at most"):
        g.mcp_client_config([f"x{i}" for i in range(30)])
    with pytest.raises(ValueError, match="list"):
        g.mcp_client_config("box")


def test_default_tradecomply_url_is_this_server_never_a_built_in_one(monkeypatch):
    def url(**kw):
        return g.mcp_client_config(["box"], **kw).claude_code_mcp_json["mcpServers"]["tradecomply"]["url"]

    assert url() == THIS_SERVER + "/mcp"
    monkeypatch.setenv("APP_URL", "http://192.168.1.20:8000/")          # operator-set LAN address: http allowed
    assert url() == "http://192.168.1.20:8000/mcp"
    monkeypatch.delenv("APP_URL")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://tc-render.example.test")
    assert url() == "https://tc-render.example.test/mcp"                # Render's own address when APP_URL is unset
    monkeypatch.delenv("RENDER_EXTERNAL_URL")
    # stdio / a laptop with no APP_URL: no guessing, the caller must say where their server is
    with pytest.raises(ValueError, match="Pass tradecomply_mcp_url") as e:
        g.mcp_client_config(["box"])
    assert "example" in str(e.value) and "onrender" not in str(e.value)
    assert url(tradecomply_mcp_url="https://mine.example.test/mcp") == "https://mine.example.test/mcp"
    token = g.SERVER_MCP_URL.set("http://lan-host:8000/mcp")           # set by the web app around /api/tools runs
    try:
        assert url() == "http://lan-host:8000/mcp"
    finally:
        g.SERVER_MCP_URL.reset(token)
    with pytest.raises(ValueError) as e:
        g.mcp_client_config(["box"], tradecomply_mcp_url="ftp://x/mcp")
    assert "your-tradecomply-server.example" in str(e.value)


def test_mcp_tool_over_stdio_without_app_url_asks_for_the_address(monkeypatch):
    monkeypatch.delenv("APP_URL")
    mcp = FastMCP("t")
    g.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            r = await c.call_tool("mcp_client_config", {"ids": ["box"]}, raise_on_error=False)
            return tools, r

    tools, r = asyncio.run(run())
    assert r.is_error and "Pass tradecomply_mcp_url" in r.content[0].text
    desc = tools["mcp_client_config"].input_schema["properties"]["tradecomply_mcp_url"]["description"]
    assert "this server's own /mcp" in desc and "hosted" not in desc


def test_gov_cloud_step_names_no_built_in_contact(monkeypatch):
    step = next(s for s in g.integrations_for_task("post alerts to slack", gov_cloud_required=True).next_steps
                if "FedRAMP" in s)
    assert "whoever runs your TradeComply server" in step and "support:" not in step
    monkeypatch.setenv("SUPPORT_EMAIL", "help@tc-operator.example.test")
    step = next(s for s in g.integrations_for_task("post alerts to slack", gov_cloud_required=True).next_steps
                if "FedRAMP" in s)
    assert "(support: help@tc-operator.example.test)" in step


def test_every_config_ready_id_contributes_a_server():
    ready = g.config_ready_ids()
    assert {"box", "docusign", "slack", "zoom", "google-workspace", "atlassian-rovo"} <= set(ready)
    for i in ready:
        r = g.mcp_client_config([i])
        assert len(r.claude_code_mcp_json["mcpServers"]) >= 2 and not r.skipped, i


# ---------------------------------------------------------------------------- MCP tools
def test_mcp_tools_are_registered_read_only_and_return_structured_results():
    mcp = FastMCP("t")
    g.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            a = await c.call_tool("integrations_for_task", {"task": "e-sign lien waivers", "limit": 3})
            b = await c.call_tool("mcp_client_config", {"ids": ["slack", "zoom"]})
            bad = await c.call_tool("integrations_for_task", {"task": ""}, raise_on_error=False)
            empty = await c.call_tool("mcp_client_config", {"ids": []}, raise_on_error=False)
            return tools, a.structured_content, b.structured_content, bad, empty

    tools, a, b, bad, empty = asyncio.run(run())
    assert set(tools) == {"integrations_for_task", "mcp_client_config"}
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.open_world_hint is False
    assert "box" in tools["mcp_client_config"].input_schema["properties"]["ids"]["description"]
    assert a["matches"][0]["id"] == "docusign" and len(a["matches"]) <= 3
    assert set(b["claude_code_mcp_json"]["mcpServers"]) == {"tradecomply", "slack", "zoom"}
    assert bad.is_error and "must not be empty" in bad.content[0].text
    assert empty.is_error
