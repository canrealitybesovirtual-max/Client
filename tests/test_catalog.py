"""app/catalog.py and its use in GET /api/tools (spec 2.10, 5.6, 5.11, 6.1, 8.2.7)."""
import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import api, assistant, catalog
from tests.support import backend, browser, make_app, signup  # noqa: F401  (backend: every test on each database)
from tradecomply import core
from tradecomply import search as engine


@pytest.fixture(scope="module")
def live():
    """{name: tool} as the MCP server lists them."""
    from fastmcp import Client

    from server import mcp

    async def go():
        async with Client(mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    return asyncio.run(go())


ADVANCED = re.compile(r"^(as_of|today|evaluation_date)$")       # app.js: the "Advanced" fields, never in a step


def schema(t) -> dict:
    return api._attr(t, "input_schema", "inputSchema")


# ---------------------------------------------------------------------------- the catalog itself
def test_every_tool_once(live):
    in_groups = [n for g in catalog.GROUPS for n in g["tools"]]
    hidden = [n for n, c in catalog.TOOLS.items() if c["hidden"]]
    assert len(in_groups) == len(set(in_groups))
    assert sorted(in_groups + hidden) == sorted(catalog.TOOLS) == sorted(live)
    assert len(catalog.TOOLS) == 74 and len(catalog.GROUPS) == 12
    assert set(hidden) == {"mcp_client_config", "integrations_for_task", "list_professional_types", "who_must_sign"}
    for n, c in catalog.TOOLS.items():
        assert c["group"] in catalog.GROUP_LABELS, n
        assert 0 < len(c["title"]) <= 60 and 0 < len(c["summary"]) <= 110, n
    for role, names in catalog.COMMON.items():
        assert role in ("owner", "office", "field") and set(names) <= set(live) and len(names) >= 5


def test_titles_pinned_by_other_tests_stay():
    assert catalog.TOOLS["calculate_lien_deadline"]["title"] == "Calculate lien deadline"
    assert catalog.TOOLS["verify_contractor_license"]["title"] == "Verify contractor license"
    assert catalog.TOOLS["adu_rules"]["title"] == "ADU rules"


def test_layouts_match_the_schemas(live):
    assert set(catalog.LAYOUTS) == {"validate_home_improvement_contract", "permit_required", "accessibility_field_check",
                                    "generate_safety_programs", "project_setup_checklist", "project_risk_score"}
    for name, lay in catalog.LAYOUTS.items():
        props = schema(live[name])["properties"]
        required = set(schema(live[name]).get("required", []))
        fields = [f for st in lay["steps"] for f in st["fields"]]
        shown = [f for f in props if not ADVANCED.match(f)]               # as_of stays the form's advanced field
        assert sorted(fields) == sorted(shown), name                      # every field exactly once
        assert lay["layout"] in ("steps", "sections") and lay["final_button"]
        for dep, cond in lay["show_if"].items():
            assert dep in props and cond["field"] in props and dep != cond["field"], (name, dep)
            assert dep not in required, (name, dep)                       # a hidden field is never required
            assert set(cond) <= {"field", "op", "value"} and cond["op"] in ("eq", "in", "truthy")
            other = props[cond["field"]]
            enum = other.get("enum") or next((x["enum"] for x in other.get("anyOf", []) if x.get("enum")), None)
            if enum and cond["op"] == "eq":
                assert cond["value"] in enum, (name, dep)
            if enum and cond["op"] == "in":
                assert set(cond["value"]) <= set(enum), (name, dep)
            if cond["op"] == "truthy":
                assert "value" not in cond
            why = catalog.BECAUSE[name][dep]
            assert why.lower() in (props[dep].get("description") or "").lower(), (name, dep, why)
    subjects = " ".join(props.get("description") or "" for name in catalog.LAYOUTS
                        for props in schema(live[name])["properties"].values())
    for subject in ("Service and repair", "Owner-builder only", "Fence", "Wall", "Deck", "Shed", "trench"):
        assert subject.lower() in subjects.lower()


def test_labels_name_real_fields(live):
    fields = {f for t in live.values() for f in schema(t)["properties"]}
    assert set(catalog.LABELS) <= fields


ROLES = {   # spec 1.4 / 1.5
    "account": "owner office field", "display": "owner office field", "apps": "owner office field",
    "company": "owner office", "team": "owner office", "billing": "owner", "ask": "owner", "mcp": "owner office",
    "calendar": "owner office", "email": "owner", "integrations": "owner office", "activity": "owner office",
    "mcp-log": "owner", "data": "owner",
}


def test_settings_sections_and_pages_carry_roles():
    assert {s["key"]: " ".join(s["roles"]) for s in catalog.SETTINGS_SECTIONS} == ROLES
    assert catalog.SETTINGS_SECTIONS[-1]["label"] == "Account and data"
    for p in catalog.PAGES:
        assert p["roles"] and set(p["roles"]) <= {"owner", "office", "field"} and p["href"].startswith("#/")
        m = re.match(r"#/settings\?section=([\w-]+)$", p["href"])
        if m:
            assert tuple(p["roles"]) == catalog.SECTION_ROLES[m.group(1)]
    by = catalog.PAGE_BY_ID
    assert by["project_new"]["roles"] == ("owner", "office") and by["subs_import"]["roles"] == ("owner", "office")
    assert by["settings-billing"]["roles"] == ("owner",) and by["settings-display"]["roles"] == ALL
    field_pages = {p["id"] for p in catalog.pages_for("field")}
    assert "settings-billing" not in field_pages and "settings-team" not in field_pages
    assert {"dashboard", "field", "tools", "settings-account", "settings-display", "more"} <= field_pages


ALL = ("owner", "office", "field")


def test_glossary_points_at_real_rules_and_real_words():
    ids = {r.id for r in core.all_rules()}
    text = " ".join(" ".join(str(getattr(r, k) or "") for k in ("title", "plain_english", "applies_to", "quote",
                                                                    "notes", "correction", "citation_text"))
                    for r in core.all_rules()).lower()
    with open(engine.os.path.join(engine.DATA, "professionals.json"), encoding="utf-8") as f:
        text += f.read().lower()
    terms = engine.glossary()
    assert len(terms) >= 29 and len({t["term"] for t in terms}) == len(terms)
    for t in terms:
        assert t["rule_id"] in ids, t
        if "expansion" in t:
            assert t["expansion"].lower() in text, t
        assert set(t) <= {"term", "aliases", "expansion", "rule_id"}
    assert "811" in {t["term"] for t in terms} and "DigAlert" not in {t["term"] for t in terms}


# ---------------------------------------------------------------------------- GET /api/tools
@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def dump(x) -> bytes:
    return json.dumps(x, sort_keys=True, default=str).encode()


def test_api_tools_adds_the_catalog_on_a_copy(app):
    with TestClient(app) as c:
        signup(c)
        platform = app.state.platform
        specs = asyncio.run(api.tool_specs(platform))
        before = (dump(specs), dump(assistant.claude_tools(specs)))
        r = c.get("/api/tools")
        assert r.status_code == 200
        body = r.json()
        specs_after = asyncio.run(api.tool_specs(platform))
        assert specs_after is specs                                          # same cached objects
        assert (dump(specs_after), dump(assistant.claude_tools(specs_after))) == before
        assert "x-label" not in before[0].decode() and "x-label" not in before[1].decode()
        tools = {t["name"]: t for t in body["tools"]}
        assert body["count"] == len(tools) == 74 and body["modules"]
        lien = tools["calculate_lien_deadline"]
        assert lien["title"] == "Calculate lien deadline" and lien["group"] == "paid"
        assert lien["group_label"] == "Get paid" and lien["hidden"] is False and lien["layout"] is None
        assert lien["summary"] == catalog.TOOLS["calculate_lien_deadline"]["summary"]
        assert lien["input_schema"]["properties"]["claimant_role"]["x-label"] == "Your role on this job"
        assert tools["select_lien_waiver"]["title"] == "Choose a lien waiver"
        assert tools["who_must_sign"]["hidden"] is True
        permit = tools["permit_required"]["layout"]
        assert permit["layout"] == "steps" and permit["final_button"] == "Check if a permit is needed"
        assert permit["show_if"]["fence_height_ft"] == {"field": "structure_type", "op": "eq", "value": "fence"}
        assert [g["id"] for g in body["groups"]] == [g["id"] for g in catalog.GROUPS]
        assert body["groups"][1]["starters"] == catalog.GROUP_STARTERS["paid"]
        assert set(body["groups"][0]) == {"id", "label", "icon", "purpose", "tools", "starters"}
        assert body["common"] == catalog.COMMON
        # the module list (older app versions read it) carries the same annotated tools
        in_modules = {t["name"]: t["title"] for m in body["modules"] for t in m["tools"]}
        assert in_modules["select_lien_waiver"] == "Choose a lien waiver"


def test_mcp_titles_are_unchanged(live):
    changed = {n: c["title"] for n, c in catalog.TOOLS.items()
               if c["title"] != api.humanize(n) and c["title"] != (live[n].title or "")}
    assert len(changed) >= 20
    for n in changed:
        assert live[n].title != catalog.TOOLS[n]["title"], n


def test_api_key_sees_only_its_tools_in_groups_and_common(app):
    with TestClient(app) as c:
        signup(c)
        r = c.post("/api/keys", json={"name": "agent", "scopes": {"tools": ["calculate_lien_deadline",
                                                                            "construction_hours"]}})
        key = r.json()["key"]
        with browser(c) as bot:
            body = bot.get("/api/tools", headers={"authorization": f"Bearer {key}"}).json()
        assert {t["name"] for t in body["tools"]} == {"calculate_lien_deadline", "construction_hours"}
        assert {n for g in body["groups"] for n in g["tools"]} == {"calculate_lien_deadline", "construction_hours"}
        assert body["common"]["field"] == ["construction_hours"]
        assert body["common"]["owner"] == ["calculate_lien_deadline", "construction_hours"]
