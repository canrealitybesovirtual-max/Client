"""GET /api/suggest/vocab and POST /api/suggest (spec 3.5, 3.6, 8.2.2, 8.2.3, 5.12), and Store.candidates."""
import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import search
from app.auth import RateLimiter
from tests.support import backend, browser, join, make_app, oauth_connect, signup  # noqa: F401
from tradecomply import core
from tradecomply import search as engine


SITE = "https://tradecomply.example"         # https, so "Sign in with TradeComply" (OAuth) is available


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path, app_url=SITE)


@pytest.fixture
def owner(app):
    with TestClient(app, base_url=SITE) as c:
        c.tenant_id = signup(c, company="Sierra Build Co", license_number="1012345")["tenant_id"]
        yield c


def vocab(c) -> dict:
    r = c.get("/api/suggest/vocab")
    assert r.status_code == 200, r.text
    return r.json()


def sug(c, source, q="", **kw):
    r = c.post("/api/suggest", json={"source": source, "q": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------------------- the vocabulary
def test_vocab_shape_caching_and_etag(owner):
    r = owner.get("/api/suggest/vocab", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-cache"
    assert r.headers.get("content-encoding") == "gzip"              # the same for everyone, so compressed
    v = r.json()
    assert re.fullmatch(r"2026-\d\d-\d\d\.[0-9a-f]{12}", v["version"]) and v["rules_built_on"] == "2026-11-26"
    assert r.headers["etag"] == f'"{v["version"]}"'
    for key in ("sources", "fields", "app_fields", "company_sources", "remote_sources", "tool_keys", "glossary",
                "page_starters"):
        assert key in v, key
    assert v["company_sources"] == search.COMPANY_SOURCES and v["remote_sources"] == search.REMOTE_SOURCES
    assert len(r.content) <= 160_000
    etag = r.headers["etag"]
    for inm in (etag, f"W/{etag}", f'"other", {etag}', f'W/"x", W/{etag}'):
        again = owner.get("/api/suggest/vocab", headers={"if-none-match": inm})
        assert again.status_code == 304 and again.headers["etag"] == etag and not again.content
    assert owner.get("/api/suggest/vocab", headers={"if-none-match": '"stale"'}).status_code == 200
    for item in v["sources"]["county"]["items"]:
        assert set(item) <= {"value", "label", "aliases", "detail", "verified", "rule_id", "group"}


def test_vocab_holds_no_rule_text_and_no_company_data(owner):
    owner.post("/api/projects", json={"name": "Elm St ADU", "city": "Oakland", "county": "Alameda",
                                      "owner_name": "Pat Quintana", "lender_name": "Golden Valley Bank"})
    owner.post("/api/subs", json={"name": "Ace Roofing", "license_number": "1023456"})
    raw = owner.get("/api/suggest/vocab").text
    for secret in ("Sierra Build Co", "Elm St ADU", "Pat Quintana", "Golden Valley Bank", "Ace Roofing", "1012345"):
        assert secret not in raw
    for r in core.all_rules():
        if r.plain_english and len(r.plain_english) > 80:
            assert r.plain_english not in raw, r.id


def test_every_mapped_field_exists(owner, app):
    from app import api
    specs = {s["name"]: s for s in asyncio.run(api.tool_specs(app.state.platform))}
    v = vocab(owner)
    assert len(v["fields"]) >= 60
    for key, f in v["fields"].items():
        tool, field = key.split(".", 1)
        assert field in specs[tool]["input_schema"]["properties"], key
        assert f["source"] in v["sources"] or f["source"] in v["company_sources"] + v["remote_sources"], key
        if f["chips"]:
            assert f["strict"] and not f["multiple"] and len(v["sources"][f["source"]]["items"]) <= 6
    assert v["fields"]["cobra_deadlines.qualifying_event"] == {"source": "qualifying_event", "strict": True,
                                                               "multiple": False, "chips": True}
    assert v["fields"]["construction_hours.city"]["source"] == "city_covered"
    assert v["fields"]["check_subcontractor_compliance.license_number"]["source"] == "sub_license"
    for key in v["app_fields"].values():
        assert key["source"] in v["sources"] or key["source"] in v["company_sources"] + v["remote_sources"]


def test_counties_cities_and_classes(owner):
    from tradecomply import licensing, professionals
    v = vocab(owner)
    s = v["sources"]
    assert [i["value"] for i in s["county"]["items"]] == professionals.counties() and len(s["county"]["items"]) == 58
    assert s["county"]["official"]["url"] == "https://www.dir.ca.gov/databases/das/pwaddrstart.asp"
    sd = next(i for i in s["county"]["items"] if i["value"] == "San Diego")
    assert "SD" in sd["aliases"] and "San Diego County" in sd["aliases"]
    assert set(s["county"]["ask_which"]) == {"SB", "SC", "SJ", "SM"}
    assert s["county"]["ask_which"]["SB"] == ["San Bernardino", "Santa Barbara"]
    cities = {i["value"]: i for i in s["city_covered"]["items"]}
    assert len(cities) == 8 and cities["San Diego"]["verified"] and cities["Los Angeles"]["verified"]
    assert not cities["Oakland"]["verified"] and cities["Oakland"]["detail"] == "City rules not yet verified"
    c_codes = sorted(licensing.class_table())
    d = engine.cslb_d_subcategories()["items"]
    still = [i["page_code"] for i in d if engine.d_still_issued(i)]
    needed = [i["value"] for i in s["class_needed"]["items"]]
    held = [i["value"] for i in s["class_held"]["items"]]
    assert needed == ["A", "B", "B-2"] + list(licensing.class_table()) + still and len(still) == 28
    assert held[:3 + len(c_codes)] == needed[:3 + len(c_codes)]
    assert sorted(held[3 + len(c_codes):]) == sorted(i["page_code"] for i in d) and len(held) == 3 + 43 + 64
    d1 = next(i for i in s["class_held"]["items"] if i["value"] == "D-1")
    assert d1["label"] == "D-1 Architectural Porcelain" and d1["detail"] == "No longer issued · Now under D-64"
    d3 = next(i for i in s["class_needed"]["items"] if i["value"] == "D-3")
    assert d3["detail"] == "C-61 Limited Specialty subcategory" and "D-03" in d3["aliases"]
    c39 = next(i for i in s["class_needed"]["items"] if i["value"] == "C-39")
    assert c39["rule_id"] == "class-c39-roofing-scope" and "roofer" not in c39["label"]
    assert "reroof" in c39["aliases"] and c39["detail"] == core.rule("class-c39-roofing-scope").citation_text
    for sid in ("class_needed", "class_held", "craft", "hazard", "city_tenant"):
        assert s[sid]["strict"] is False, sid
    for sid in ("county", "city_covered", "equipment", "qualifying_event"):
        assert s[sid]["strict"] is True, sid


# ---------------------------------------------------------------------------- strictness, probed through MCP
BASE = {   # arguments that make each check run, so only the probed field can make it fail
    "valley_fever_requirements": {}, "jobsite_hazard_checklist": {"tasks": ["trenching"]},
    "find_licensed_professional": {"professional_type": "attorney"},
    "who_must_sign": {"tool_name": "calculate_lien_deadline",
                      "arguments": {"claimant_role": "subcontractor", "last_furnishing_date": "2026-10-01"}},
    "construction_hours": {"when": "2026-11-30T08:00"}, "local_jobsite_requirements": {},
    "construction_debris_plan": {"project_type": "residential_alteration"},
    "overhead_line_clearance": {"voltage_kv": 12},
    "leave_request_check": {"employee_count": 30}, "cobra_deadlines": {"event_date": "2026-11-01"},
    "federal_project_requirements": {"funding_source": "direct_federal", "prime_contract_value_usd": 500000},
    "buy_america_check": {"program": "baba", "item_type": "iron_steel"}, "truck_compliance": {"gvwr_lbs": 30000},
    "portable_engine_requirements": {"engine_bhp": 100}, "offroad_fleet_obligations": {"total_fleet_hp": 500},
    "offroad_contracting_requirements": {},
    "confined_space_classification": {"space": "vault", "can_bodily_enter": True, "limited_entry_or_exit": True,
                                      "designed_for_continuous_occupancy": False},
    "search_california_rules": {"query": "lien"},
}
OFF_LIST = {"county": "Atlantis", "city_covered": "Bakersfield"}


def call_tools(calls):
    from fastmcp import Client

    from server import mcp

    async def go():
        out = []
        async with Client(mcp) as c:
            for name, args in calls:
                r = await c.call_tool(name, args, raise_on_error=False)
                out.append(bool(r.is_error))
        return out
    return asyncio.run(go())


def test_strict_fields_are_exactly_the_ones_the_tools_enforce(owner):
    v = vocab(owner)
    strict = {k: f for k, f in v["fields"].items() if f["strict"]}
    assert len(strict) >= 20
    calls = []
    for key, f in strict.items():
        tool, field = key.split(".", 1)
        assert tool in BASE, f"add {tool} to BASE to probe {key}"
        good = v["sources"][f["source"]]["items"][0]["value"]
        bad = OFF_LIST.get(f["source"], "zzz")
        calls += [(tool, {**BASE[tool], field: good}), (tool, {**BASE[tool], field: bad})]
    results = call_tools(calls)
    for n, key in enumerate(strict):
        assert results[2 * n] is False, f"{key}: the probe's on-list call failed"
        assert results[2 * n + 1] is True, f"{key}: the tool accepts an off-list value, so it must not be strict"
    # the probe cases of spec 3.5: accepted off-list values, so these stay suggestions only
    loose = call_tools([("check_subcontractor_compliance", {"license_number": "1000001",
                                                            "required_classification": "D-99"}),
                        ("check_subcontractor_compliance", {"license_number": "1000001",
                                                            "required_classification": "plumber"}),
                        ("lookup_safety_standard", {"hazard": "roof fall protection"}),
                        ("apprentice_hours_required", {"craft": "carpenter", "journeyman_hours": 1000})])
    assert loose == [False, False, False, False]
    for key in ("check_subcontractor_compliance.required_classification", "lookup_safety_standard.hazard",
                "apprentice_hours_required.craft", "company_license_obligations.classifications"):
        assert v["fields"][key]["strict"] is False, key


def test_strict_alias_values_round_trip_through_the_tools_normalisers(owner):
    from tradecomply import equipment_emissions as ee
    from tradecomply import federal_projects as fp
    from tradecomply import hr_compliance as hr
    from tradecomply import jobsite_safety as js
    norm = {"leave_type": hr.normalize_leave_type, "qualifying_event": hr.normalize_cobra_event,
            "funding_source": fp.normalize_funding, "work_type_federal": fp.normalize_work_type,
            "buy_america_program": fp.normalize_program,
            "buy_america_item": lambda x: fp._classify_item(x, None)[0], "fuel": ee._fuel,
            "fleet_owner_type": ee._owner, "offroad_role": ee._role, "cs_role": js._cs_role, "me_role": js._me_role,
            "equipment": lambda x: js._equipment(x)[0], "county": lambda x: ee._county(x)[0]}
    s = vocab(owner)["sources"]
    for sid, fn in norm.items():
        assert s[sid]["strict"] is True, sid
        for item in s[sid]["items"]:
            assert fn(item["value"]) == item["value"], (sid, item["value"])
            for alias in item.get("aliases", []):
                if sid == "county" and alias.upper() == alias and len(alias) <= 3:
                    continue                                    # SD/LA/OC/SF/SLO: checked by the tool's own table
                assert fn(alias) == item["value"], (sid, alias)


# ---------------------------------------------------------------------------- company sources
def seed(c):
    pid = c.post("/api/projects", json={"name": "Elm St ADU", "city": "Oakland", "county": "Alameda",
                                        "address": "12 Elm St", "owner_name": "Pat Quintana",
                                        "lender_name": "Golden Valley Bank"}).json()["id"]
    c.post("/api/projects", json={"name": "Kern Solar Yard", "city": "Bakersfield", "county": "Kern"})
    c.post("/api/subs", json={"name": "Ace Roofing", "license_number": "1023456", "scope": "Roofing",
                              "email": "ace@roof.test", "phone": "555-0100"})
    c.post("/api/subs", json={"name": "Peña Concrete", "license_number": "1034567"})
    c.post("/api/documents", json={"title": "Elm St safety program", "body_markdown": "x"})
    for kind, topic, names in (("toolbox_talk", "Trench safety", ["José Peña", "Ana Ruiz"]),
                               ("inspection", "Daily scaffold check", ["Bo Lee"]),
                               ("incident", "Fall from ladder", ["Injured Ivan", "Witness Wanda"])):
        r = c.post("/api/field_logs", json={"kind": kind, "date": "2026-11-20", "topic": topic, "project_id": pid,
                                            "attendees": [{"name": n, "acknowledged": True} for n in names]})
        assert r.status_code == 201, r.text
    return pid


def test_company_sources(owner):
    seed(owner)
    assert [i["value"] for i in sug(owner, "project")] == ["Kern Solar Yard", "Elm St ADU"]      # newest first
    elm = sug(owner, "project", "elm")[0]
    assert elm == {"value": "Elm St ADU", "label": "Elm St ADU", "detail": "Oakland, Alameda County",
                   "source_label": "Your projects"}
    assert [i["value"] for i in sug(owner, "project_city")] == ["Bakersfield", "Oakland"]
    assert sug(owner, "project_county", "ala")[0]["value"] == "Alameda"
    assert sug(owner, "owner_name", "pat")[0]["value"] == "Pat Quintana"
    assert sug(owner, "lender_name", "gold")[0]["value"] == "Golden Valley Bank"
    assert sug(owner, "project_address", "12")[0]["value"] == "12 Elm St"
    lic = sug(owner, "sub_license", "ace")
    assert lic[0] == {"value": "1023456", "label": "1023456 · Ace Roofing", "detail": "Roofing",
                      "source_label": "Your subcontractors"}
    own = sug(owner, "sub_license", "", tool="project_setup_checklist", field="contractor_license_number")
    assert own[0]["value"] == "1012345" and own[0]["label"] == "1012345 · Sierra Build Co (your license)"
    assert sug(owner, "sub_license", "")[-1]["value"] == "1012345"
    assert sug(owner, "sub", "pe")[0]["value"] == "Peña Concrete"
    assert sug(owner, "document", "safety")[0]["value"] == "Elm St safety program"
    raw = json.dumps([sug(owner, s, "") for s in search.COMPANY_SOURCES])
    assert "ace@roof.test" not in raw and "555-0100" not in raw


def test_accent_and_case_folding(owner):
    seed(owner)
    assert sug(owner, "crew_name", "jose pena")[0]["value"] == "José Peña"
    assert sug(owner, "crew_name", "PEÑA")[0]["value"] == "José Peña"
    assert sug(owner, "sub", "pena")[0]["value"] == "Peña Concrete"
    assert sug(owner, "project", "ELM ST")[0]["value"] == "Elm St ADU"


def test_crew_names_and_topics_never_come_from_incident_reports(owner, app):
    seed(owner)
    with browser(owner) as field:
        join(field, owner, "foreman@acme.test", role="field", name="Fran Foreman")
        with browser(owner) as office:
            join(office, owner, "office@acme.test", role="office", name="Olive Office")
            office_names = {i["value"]: i["source_label"] for i in sug(office, "crew_name")}
            assert office_names["Fran Foreman"] == "Your team" and office_names["José Peña"] == "Earlier logs"
        field_names = {i["value"] for i in sug(field, "crew_name")}
        assert field_names == {"José Peña", "Ana Ruiz", "Bo Lee"}           # no team list for field users
        for who in (owner, field):
            names = json.dumps(sug(who, "crew_name")) + json.dumps(sug(who, "crew_name", "i"))
            assert "Injured Ivan" not in names and "Witness Wanda" not in names
            topics = [i["value"] for i in sug(who, "field_topic")]
            assert topics == ["Daily scaffold check", "Trench safety"]


def test_field_log_reads_select_only_three_columns(owner, app, monkeypatch):
    seed(owner)
    seen = []
    real = app.state.platform.store.candidates

    def spy(tenant_id, table, columns, limit=2000, where=None):
        seen.append((table, tuple(columns), where))
        return real(tenant_id, table, columns, limit=limit, where=where)
    monkeypatch.setattr(app.state.platform.store, "candidates", spy)
    sug(owner, "crew_name")
    sug(owner, "field_topic")
    logs = [x for x in seen if x[0] == "field_logs"]
    assert logs and all(cols == ("kind", "topic", "attendees") for _, cols, _ in logs)


def test_companies_are_isolated(owner, app):
    seed(owner)
    with browser(owner) as other:
        signup(other, email="b@other.test", company="Other Co", license_number="1099999")
        for source in search.COMPANY_SOURCES:
            raw = json.dumps(sug(other, source, "")) + json.dumps(sug(other, source, "e"))
            for name in ("Elm St ADU", "Kern Solar", "Ace Roofing", "Pat Quintana", "Golden Valley", "José Peña",
                         "Trench safety", "safety program", "1023456"):
                assert name not in raw, (source, name)


def test_store_candidates_allow_list_and_index(owner, app):
    st = app.state.platform.store
    with pytest.raises(ValueError):
        st.candidates(owner.tenant_id, "subs", ("email",))
    with pytest.raises(ValueError):
        st.candidates(owner.tenant_id, "field_logs", ("signatures",))
    with pytest.raises(ValueError):
        st.candidates(owner.tenant_id, "audit", ("id",))
    with pytest.raises(ValueError):
        st.candidates(owner.tenant_id, "projects", ("name",), where="1=1")
    seed(owner)
    kinds = {r["kind"] for r in st.candidates(owner.tenant_id, "field_logs", ("kind", "topic", "attendees"))}
    assert kinds == {"toolbox_talk", "inspection"}
    if st.backend == "sqlite":
        with st.connect() as con:
            for table, extra in (("projects", ""), ("subs", ""), ("documents", ""), ("users", ""),
                                 ("field_logs", " and kind in ('toolbox_talk', 'inspection')")):
                plan = " ".join(str(tuple(r)) for r in con.execute(
                    f"explain query plan select id from {table} where tenant_id = ?{extra} order by id desc limit 5",
                    ("t",)).fetchall())
                assert "SEARCH" in plan and "tenant_id=?" in plan, (table, plan)     # an index, never a scan


# ---------------------------------------------------------------------------- search-backed sources
def test_scope_phrases_needs_and_rules(owner, app):
    search.build_now(app.state.platform)
    wh = sug(owner, "scope_phrase", "water he")
    assert wh and wh[0]["value"].startswith("water heater") and wh[0]["detail"].startswith("C-36")
    need = sug(owner, "need", "stamp")
    assert need and all(i.get("type_id") for i in need)
    rules = sug(owner, "rule", "prelim")
    assert rules and rules[0]["value"].startswith("prelim") and isinstance(rules[0]["verified"], bool)


def test_rule_source_is_empty_without_a_subscription(owner, app):
    search.build_now(app.state.platform)
    app.state.platform.store.update_tenant(owner.tenant_id, status="canceled")
    assert sug(owner, "rule", "prelim") == []


# ---------------------------------------------------------------------------- who and how
def test_people_only_and_errors(owner, app):
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    with browser(owner) as bot:
        h = {"authorization": f"Bearer {key}"}
        for method, path, body in (("GET", "/api/suggest/vocab", None),
                                   ("POST", "/api/suggest", {"source": "project", "q": "e"})):
            r = bot.request(method, path, json=body, headers=h)
            assert r.status_code == 403 and r.json()["detail"] == "Quick help and search are for people signed in " \
                                                                  "to TradeComply."
    tokens = oauth_connect(owner)["tokens"]
    with browser(owner) as app_client:
        r = app_client.post("/api/suggest", json={"source": "project", "q": "e"},
                            headers={"authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 401
        anon = app_client.post("/api/suggest", json={"source": "project", "q": "e"})
        assert anon.status_code == 401
    assert owner.post("/api/suggest", json={"source": "subs_email", "q": ""}).status_code == 400
    assert owner.post("/api/suggest", json={"source": "project", "q": "", "extra": 1}).status_code == 422
    assert owner.post("/api/suggest", json={"source": "project", "q": "x" * 101}).status_code == 422
    assert owner.post("/api/suggest", json={"source": "project", "limit": 21}).status_code == 422
    assert owner.get("/api/suggest?source=project&q=elm").status_code == 405
    assert owner.get("/api/search?q=elm").status_code == 405
    assert owner.get("/api/help?question=x").status_code == 405


def test_limits(owner, app):
    platform = app.state.platform
    platform.search_user_limiter = RateLimiter(limit=3, window_s=60)
    for _ in range(3):
        assert owner.post("/api/suggest", json={"source": "project"}).status_code == 200
    r = owner.post("/api/suggest", json={"source": "project"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert owner.post("/api/search", json={"q": "elm"}).status_code == 429        # the same budget
    platform.search_user_limiter = RateLimiter(limit=900, window_s=60)
    platform.search_tenant_limiter = RateLimiter(limit=2, window_s=60)
    platform.search_tenant_limiter.hit(f"t|{owner.tenant_id}")
    platform.search_tenant_limiter.hit(f"t|{owner.tenant_id}")
    assert owner.post("/api/suggest", json={"source": "project"}).status_code == 429
    platform.search_tenant_limiter = RateLimiter(limit=1800, window_s=60)
    platform.search_global_limiter = RateLimiter(limit=1, window_s=60)
    platform.search_global_limiter.hit("all")
    assert owner.post("/api/suggest", json={"source": "project"}).status_code == 429
