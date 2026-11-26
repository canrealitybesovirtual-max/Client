"""Licensed professionals (tradecomply/professionals.py, tradecomply/data/handoffs.json): the handoff map that says
which tool results need a licensed or certified person, the three MCP tools (find_licensed_professional,
who_must_sign, list_professional_types), the `handoffs` field /api/tools/{name} adds to results, and
/api/professionals/*. Checks: every type has an official link (or is marked as having none), county validation,
real tool results that need an attorney, a QSD/QSP or an engineer or architect, the hook never breaking a tool call,
and API-key tool scopes holding when who_must_sign runs a tool. Runs on SQLite and, with TC_TEST_DATABASE_URL, on
PostgreSQL."""
import asyncio
import json
import logging
import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from tradecomply import professionals as pro
from tradecomply.core import all_rules
from tests.support import backend, make_app, signup  # noqa: F401  (backend is an autouse fixture: both stores)

RULES = {r.id: r for r in all_rules()}
CONTRACT = {"type_id": str, "name": str, "why": str, "legal_basis": str, "verified": bool, "county_filtered": bool}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
# Hosts whose pages were fetched on 2026-11-26 for a "confirmed" legal basis of an extra type.
EXTRA_READ_HOSTS = {"www.dir.ca.gov", "www.ecfr.gov", "www.aqmd.gov", "ww2.valleyair.org", "www.sandiego.gov"}
AGGREGATORS = re.compile(r"yelp|angi|thumbtack|homeadvisor|houzz|avvo|justia|findlaw|martindale|superlawyers|"
                         r"lawyers\.com|bbb\.org|buildzoom|porch|google\.com/maps", re.I)
# Developer notes that must never reach the screen.
INTERNAL = re.compile(r"rulebook rule|UNVERIFIED|research/|\bmodule\b|Same as|\(unchecked\)|_[a-z]+\b|[a-z]+-[a-z]+-"
                      r"[a-z]+-[a-z]+")
OVERCLAIM = re.compile(r"\b(we recommend|recommended (firm|provider|lawyer|contractor)|top-rated|best (lawyer|engineer"
                       r"|firm|contractor)|guarantee[sd]? compliance|replaces? your (attorney|lawyer))\b", re.I)
SWPPP = {"project_type": "nonresidential", "scope": "new_construction", "disturbed_acres": 2.5}
LIEN = {"claimant_role": "subcontractor", "last_furnishing_date": "2026-10-01", "first_furnishing_date": "2026-06-01"}
BIG_JOB = {"project_name": "T", "city": "San Diego", "county": "San Diego", "owner_type": "private_company",
           "is_home_improvement": False, "contract_price_usd": 5000000, "start_date": "2027-03-01",
           "valuation_usd": 5000000, "sq_ft": 40000, "stories": 4, "height_ft": 50, "demolition": True,
           "year_built": 1965, "excavation_depth_ft": 22, "disturbed_acres": 2, "scope": "new_building",
           "residential_units": 40}
HIC = {"contract_price_usd": 20000, "down_payment_usd": 1000, "includes_arbitration_clause": True,
       "has_salesperson": True}
PUBLIC_WORK = {"project_value_usd": 500000, "awarding_body_type": "city", "is_public_funds": True,
               "work_type": "construction", "start_date": "2027-02-01", "contractor_dir_registered": True,
               "award_date": "2026-12-01", "journeyman_hours_by_craft": {"carpenter": 1000}}

CASES = {   # real inputs: together they make every handoff condition fire at least once
    'adu_c': ('adu_rules',
        {'adu_type': 'detached', 'sq_ft': 800, 'lot_type': 'single_family', 'contract_price_usd': 250000}),
    'aph': ('apprentice_hours_required',
        {'journeyman_hours': 1000,
         'apprentice_hours_worked': 50,
         'craft': 'carpenter',
         'prime_contract_usd': 500000}),
    'at': ('accessibility_triggers', {'project_type': 'alteration', 'building_use': 'retail'}),
    'bulk': ('bulk_check_subcontractors', {'rows': [{'license_number': '0000001'}]}),
    'cal': ('compliance_calendar', {'year': 2027, 'entity_type': 'llc', 'has_employees': True, 'employee_count': 10}),
    'casp': ('casp_lease_disclosure', {'role': 'landlord'}),
    'ccc': ('check_crew_certifications',
        {'crew': [{'name': 'A', 'task': 'pull wire and set panels', 'credentials': []},
                  {'name': 'B', 'task': 'operate crane', 'credentials': []}],
         'employer_license_classes': ['C-10']}),
    'ccc2': ('check_crew_certifications',
        {'crew': [{'name': 'C', 'task': 'scrape lead paint', 'credentials': []},
                  {'name': 'D', 'task': 'elevator install', 'credentials': []},
                  {'name': 'E', 'task': 'blasting', 'credentials': []}],
         'building_type': 'commercial',
         'built_before_1978': True,
         'lead_exposure_ug_m3': 60}),
    'cfs_asb': ('classification_for_scope',
        {'scope_of_work': 'asbestos abatement and fire sprinkler work', 'asbestos_sq_ft': 200}),
    'cfs_haz': ('classification_for_scope',
        {'scope_of_work': 'remove underground storage tank and contaminated soil'}),
    'cgs': ('calgreen_and_stormwater',
        {'project_type': 'nonresidential', 'scope': 'new_construction', 'disturbed_acres': 2.5}),
    'cgs_waiver': ('calgreen_and_stormwater',
        {'project_type': 'nonresidential', 'scope': 'new_construction', 'disturbed_acres': 2.5,
         'rainfall_erosivity_r_factor': 3}),
    'clo_noq': ('company_license_obligations',
        {'entity_type': 'llc', 'employee_count': 3, 'has_qualifier': False, 'classifications': ['B']}),
    'cmmc': ('cmmc_requirements',
        {'handles_cui': True, 'handles_fci_only': False, 'dod_contract': True, 'contract_award_date': '2027-01-15'}),
    'coc': ('check_cal_osha_permits',
        {'scaffold_or_falsework_height_ft': 40,
         'tower_crane': True,
         'asbestos_sqft': 150,
         'excavation_max_depth_ft': 6,
         'workers_enter_trench': True,
         'planned_start': '2027-02-01'}),
    'cr': ('certifications_required',
        {'tasks': ['panel upgrade', 'scrape and repaint trim', 'crane pick', 'blasting', 'elevator install',
                   'personnel hoist'],
         'built_before_1978': True,
         'building_type': 'single_family',
         'employer_license_classes': ['C-10', 'B'],
         'lead_exposure_ug_m3': 60,
         'dust_clearance_testing': True}),
    'cr_elev': ('certifications_required', {'tasks': ['elevator install'], 'building_type': 'commercial'}),
    'cs': ('confined_space_classification',
        {'space': 'vault',
         'can_bodily_enter': True,
         'limited_entry_or_exit': True,
         'designed_for_continuous_occupancy': False,
         'toxic_could_exceed_pel': True,
         'workers_will_enter': True}),
    'csc': ('check_subcontractor_compliance', {'license_number': '0000001'}),
    'dust': ('dust_control_requirements',
        {'air_district': 'South Coast', 'disturbed_acres': 60, 'daily_earthmoving_cy': 6000}),
    'dust_sjv': ('dust_control_requirements',
        {'air_district': 'San Joaquin Valley', 'disturbed_acres': 12, 'project_type': 'nonresidential'}),
    'ect': ('energy_compliance_triggers',
        {'project_type': 'single_family', 'scope': 'hvac_changeout', 'climate_zone': 10}),
    'fpd': ('final_paycheck_deadline',
        {'separation_type': 'discharged', 'separation_date': '2026-11-20', 'daily_wage_usd': 300}),
    'fpr': ('federal_project_requirements',
        {'prime_contract_value_usd': 2000000, 'funding_source': 'direct_federal', 'awarding_agency': 'USACE'}),
    'ghic': ('generate_home_improvement_contract',
        {'contract': {'contractor_name': 'X Co',
                      'contractor_business_address': '1 A St',
                      'contractor_license_number': '123456',
                      'contractor_email': 'a@example.com',
                      'contractor_phone': '555-0100',
                      'buyer_names': ['Y'],
                      'project_address': '1 Main',
                      'contract_price_usd': 20000,
                      'project_description': 'Kitchen remodel',
                      'down_payment_usd': 1000,
                      'progress_payments': [{'amount_usd': 19000, 'for_work': 'all work'}],
                      'materials_and_equipment': 'cabinets',
                      'start_date': '2027-01-10',
                      'completion_date': '2027-03-10',
                      'sales_language': 'Spanish',
                      'include_arbitration_clause': True}}),
    'gsp': ('generate_safety_programs',
        {'company_name': 'X',
         'employee_count': 25,
         'worksites_have_public_access': True,
         'crane_use': True,
         'silica_tasks': True,
         'respirators_used': True,
         'excavation_max_depth_ft': 22,
         'workers_enter_trench': True,
         'asbestos_or_lead': True,
         'conventional_fall_protection_infeasible': True,
         'max_fall_height_ft': 20}),
    'ird': ('injury_reporting_deadlines', {'incident_datetime': '2026-11-20T10:00', 'serious': True}),
    'jhc': ('jobsite_hazard_checklist', {'tasks': ['demolition', 'scissor lift', 'forklift', 'flagging']}),
    'lien': ('calculate_lien_deadline',
        {'claimant_role': 'subcontractor',
         'last_furnishing_date': '2026-10-01',
         'first_furnishing_date': '2026-06-01'}),
    'ljr': ('local_jobsite_requirements',
        {'city': 'San Diego',
         'start_date': '2027-02-01',
         'disturbs_survey_monument': True,
         'work_in_public_right_of_way': True}),
    'ohl': ('overhead_line_clearance', {'voltage_kv': 12, 'equipment': 'crane'}),
    'payroll': ('check_payroll',
        {'records': [{'employee': 'A',
                      'date': '2026-11-02',
                      'start': '06:00',
                      'end': '17:00',
                      'meal_breaks': [{'start': '11:00', 'end': '11:30'}]}],
         'hourly_rates': {'A': 30}}),
    'ppc': ('prompt_payment_check',
        {'project_type': 'private',
         'payer_role': 'direct_contractor',
         'payee_role': 'subcontractor',
         'amount_due_usd': 10000,
         'payment_received_by_payer_date': '2026-10-01'}),
    'pr_demo': ('permit_required',
        {'work_description': 'demolish garage',
         'structure_type': 'demolition',
         'project_type': 'single_family',
         'year_built': 1960,
         'demolition_start_date': '2027-01-15'}),
    'pr_ob': ('permit_required',
        {'work_description': 'kitchen remodel with new wiring',
         'project_type': 'single_family',
         'owner_builder': True,
         'owner_builder_work_by': 'licensed_contractors',
         'structure_type': 'alteration'}),
    'pr_wall': ('permit_required',
        {'work_description': 'retaining wall',
         'structure_type': 'retaining_wall',
         'retaining_wall_height_ft': 5,
         'project_type': 'single_family'}),
    'psc': ('project_setup_checklist',
        {'project_name': 'T',
         'city': 'San Diego',
         'county': 'San Diego',
         'owner_type': 'private_company',
         'is_home_improvement': False,
         'contract_price_usd': 5000000,
         'start_date': '2027-03-01',
         'valuation_usd': 5000000,
         'sq_ft': 40000,
         'stories': 4,
         'height_ft': 50,
         'demolition': True,
         'year_built': 1965,
         'excavation_depth_ft': 22,
         'disturbed_acres': 2,
         'scope': 'new_building',
         'residential_units': 40}),
    'psc_pub': ('project_setup_checklist',
        {'project_name': 'S',
         'city': 'Fresno',
         'county': 'Fresno',
         'owner_type': 'public_agency',
         'is_home_improvement': False,
         'contract_price_usd': 3000000,
         'start_date': '2027-03-01',
         'scope': 'alteration',
         'public_funds': True,
         'year_built': 1970,
         'stories': 1,
         'residential_units': 0}),
    'psc_wood': ('project_setup_checklist',
        {'project_name': 'W',
         'city': 'San Diego',
         'county': 'San Diego',
         'owner_type': 'private_individual',
         'is_home_improvement': True,
         'residential_units': 1,
         'contract_price_usd': 150000,
         'start_date': '2027-03-01',
         'scope': 'addition',
         'stories': 2,
         'year_built': 1960}),
    'pwp': ('prevailing_wage_penalties', {'underpaid_worker_days': 10, 'unpaid_wages_usd': 5000}),
    'pwr_unk': ('public_works_readiness',
        {'project_value_usd': 500000,
         'awarding_body_type': 'city',
         'is_public_funds': False,
         'work_type': 'construction',
         'start_date': '2027-02-01',
         'contractor_dir_registered': True,
         'subcontractors': [{'name': 'Sub A', 'license_number': '1000002', 'bid_amount_usd': 50000}],
         'prime_bid_usd': 500000}),
    'ret': ('retention_cap_check',
        {'contract_price_usd': 1000000,
         'retention_pct': 10,
         'contract_date': '2026-03-01',
         'project_type': 'private'}),
    'slw_u': ('select_lien_waiver',
        {'payment_type': 'progress', 'payment_status': 'unconditional', 'payment_received': False}),
    'spn': ('stop_payment_notice_deadline',
        {'project_type': 'private', 'claimant_role': 'subcontractor', 'completion_date': '2026-10-01'}),
    'uce': ('unlicensed_contracting_exposure',
        {'contract_price_usd': 5000, 'scope': 'kitchen remodel', 'has_license': False}),
    'vcl': ('verify_contractor_license', {'license_number': '999999'}),          # demo fixture: expired
    'vhic': ('validate_home_improvement_contract',
        {'contract_price_usd': 20000,
         'down_payment_usd': 1000,
         'includes_arbitration_clause': True,
         'has_salesperson': True}),
    'ws': ('wildfire_smoke_requirements', {'aqi_pm25': 600, 'hours_exposed': 2}),
}


def run(tool: str, args: dict) -> dict:
    import server

    async def go():
        async with Client(server.mcp) as c:
            r = await c.call_tool(tool, args, raise_on_error=True)
            return r.structured_content
    return asyncio.run(go())


def by_role(items) -> dict:
    return {(h["role_id"] if isinstance(h, dict) else h.role_id): h for h in items}


# ---------------------------------------------------------------------------- data files
def test_handoff_file_is_versioned_and_every_entry_is_complete():
    data = pro.handoff_data()
    assert data["version"] == 1 and data["checked"] == "2026-11-26"
    ids = [e["id"] for e in pro.handoffs()]
    assert len(ids) == len(set(ids)) == 103
    assert set(pro._compiled()) == set(ids)                     # every test passes the restricted evaluator
    assert set(pro._compiled_conditional_if()) == {e["id"] for e in pro.handoffs() if e.get("conditional_if")}
    assert all(e["requirement"] == "required" for e in pro.handoffs() if e.get("conditional_if"))
    for e in pro.handoffs():
        assert e["requirement"] in pro.REQUIREMENT_ORDER, e["id"]
        assert e["why"].strip() and e["when_plain"].strip(), e["id"]
        assert pro.aliases().get(e["professional_type_id"]) in pro.types(), e["id"]
        assert e["professional_type_id"] in data["type_labels"], e["id"]
        assert all(r in RULES for r in e["rule_ids"]), (e["id"], [r for r in e["rule_ids"] if r not in RULES])
        shown = e.get("legal_basis_plain") or e["legal_basis"]
        assert not INTERNAL.search(shown), (e["id"], shown)
        assert not OVERCLAIM.search(e["why"] + shown), e["id"]
        if e.get("prefer_find"):
            assert e["prefer_find"] in {f["url"] for f in pro.types()[pro.aliases()[e["professional_type_id"]]]["find"]}
    for role, url in data["role_find"].items():
        assert url in {f["url"] for f in pro.types()[pro.aliases()[role]]["find"]}, role
    every_url = {f["url"] for t in pro.types().values() for f in t["find"]}
    assert set(data["link_scopes"]) <= every_url


def test_extra_types_follow_the_registry_rules():
    """The handoff types professionals.json does not have yet: same fields, official links only, a confirmed legal
    basis only from a page that was fetched, no recommending, no contact details."""
    extra = pro.handoff_data()["extra_types"]
    registry_ids = {t["id"] for t in pro.registry()["types"]}
    assert {t["id"] for t in extra} == {"physician_or_plhcp", "state_fire_marshal_licensee", "utility_line_operator",
                                        "certified_conveyance_mechanic", "licensed_blaster", "c3pao",
                                        "air_district_dust_control_supervisor", "traffic_control_supervisor_certified"}
    read = {urlsplit(s["url"]).hostname for s in pro.handoff_data()["extra_types_sources_read"]}
    for t in extra:
        for field in ("id", "name", "plain_description", "find", "verify_license_how", "bring_or_ask", "never_say"):
            assert t.get(field) is not None and field in t, (t["id"], field)
        assert t["id"] not in registry_ids or pro.types()[t["id"]] is not t       # the registry wins if it adds one
        assert any("recommend" in s.lower() for s in t["never_say"]), t["id"]
        for f in t["find"]:
            assert f["url"].startswith("https://") and not AGGREGATORS.search(f["url"]), t["id"]
            assert f["official_owner"].strip() and (f["status"] == "reachable" or f["status"].startswith("blocked:"))
            if f["status"] != "reachable":
                assert f["search_by_confirmed"] is False and f["url_pattern_with_county"] is None and f["url_source"]
        for b in t["when_required"]:
            assert b["status"] == "confirmed" and urlsplit(b["source_url"]).hostname in EXTRA_READ_HOSTS & read
            assert all(r in RULES for r in b.get("rule_ids") or []), t["id"]
    raw = json.dumps(extra)
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", raw)
    assert not re.search(r"\(?\b[2-9]\d{2}\)?[ .-]\d{3}[ .-]\d{4}\b", raw)
    assert not OVERCLAIM.search(json.dumps([{k: v for k, v in t.items() if k != "never_say"} for t in extra]))


def test_every_type_has_an_official_link_or_is_marked():
    listed = {t.type_id: t for t in pro.list_professional_types().types}
    assert set(listed) == set(pro.types())
    for tid, t in pro.types().items():
        status = pro.directory_status(tid)
        assert listed[tid].directory_status == status
        if status == "checked":
            assert any(f["status"] == "reachable" and f["url"].startswith("https://") for f in t["find"]), tid
        elif status == "unchecked":                  # official address known, page not opened: marked as such
            assert t["find"] and all(f["status"].startswith("blocked:") and f.get("url_source") for f in t["find"])
            assert all(d.link_checked is False and "could not open" in d.how_to_search for d in pro.directories(tid))
        else:                                        # no public directory: says how to check instead
            assert t["find"] == [] and t["verify_license_how"].strip(), tid
    assert sum(pro.directory_status(t) == "checked" for t in pro.types()) >= 10


# ---------------------------------------------------------------------------- the restricted evaluator
@pytest.mark.parametrize("expr", ["__import__('os').system('true')", "result.__class__", "open('/etc/passwd')",
                                  "(lambda: 1)()", "result.x.format(1)", "1 + 1", "json.loads('1')",
                                  "result.items[0].__dict__", "[x for x in result.a].pop()", "globals()"])
def test_handoff_tests_cannot_run_code(expr):
    with pytest.raises((ValueError, SyntaxError)):
        pro.compile_test(expr)


def test_handoff_tests_follow_python_semantics_without_running_python():
    def check(expr, result, args=None):
        return pro._run(pro.compile_test(expr), {"json": pro._Json, "result": pro._wrap(result),
                                                  "args": pro._wrap(args or {}), **pro._FUNCS})
    assert check("result.a is None or result.missing", {"a": None}) is True          # short-circuit, like Python
    assert check("any(r['x'] > 1 for r in result.rows)", {"rows": [{"x": 2}, {"no_x": 1}]}) is True    # lazy
    assert check("any(p.startswith(('A', 'B')) for p in result.names)", {"names": ["c", "Bob"]}) is True
    assert check("'C-22' in result.classes and len(result.classes) == 2", {"classes": ["B", "C-22"]}) is True
    assert check("any(i['item'] == 'x' and i.get('applies') for i in result['items'])",
                 {"items": [{"item": "x", "applies": True}]}) is True
    assert check("any('broker' in json.dumps(i) for i in result['items'])", {"items": [{"a": "insurance broker"}]})
    assert check("any(i['t'] == 'a' for ph in result.phases for i in ph['items'])",
                 {"phases": [{"items": [{"t": "b"}]}, {"items": [{"t": "a"}]}]}) is True
    assert check("result.n > 0 and args.craft == 'x'", {"n": 1}, {"craft": "x"}) is True
    with pytest.raises(pro.MissingField):
        check("result.a.b", {"a": "text"})


def test_a_missing_field_is_reported_not_guessed():
    entry = next(e for e in pro.handoffs() if e["id"] == "stormwater-qsd-qsp")
    with pytest.raises(pro.MissingField):
        pro.evaluate(entry, {"something_else": True})
    assert pro.evaluate(entry, {"construction_general_permit_required": True}) is True
    items, mapped, skipped = pro.evaluate_tool("calgreen_and_stormwater", {"unexpected": 1})
    assert items == [] and mapped == 2 and skipped == ["stormwater-qsd-qsp", "stormwater-qsd-qsp-waiver"]


def test_every_handoff_fires_on_a_real_tool_result():
    """Each condition is read from real output of its tool (missing fields are errors), and fires at least once."""
    fired, errors = set(), []
    for name, (tool, args) in CASES.items():
        result = run(tool, args)
        for e in pro.handoffs():
            if e["tool"] != tool:
                continue
            try:
                if pro.evaluate(e, result, args):
                    fired.add(e["id"])
            except Exception as ex:                 # noqa: BLE001 - collected and asserted below
                errors.append((name, e["id"], repr(ex)))
    assert errors == []
    assert fired == {e["id"] for e in pro.handoffs()}


# ---------------------------------------------------------------------------- counties
def test_county_names_are_validated_against_the_58_counties():
    names = pro.counties()
    assert len(names) == len(set(names)) == 58
    assert all(pro.normalize_county(c) == c for c in names)
    assert pro.normalize_county("  san   diego ") == "San Diego"
    assert pro.normalize_county("Fresno County") == pro.normalize_county("county of fresno") == "Fresno"
    with pytest.raises(ValueError, match="Did you mean Los Angeles"):
        pro.normalize_county("Los Angelos")
    with pytest.raises(ValueError, match="58 counties.*put a city in `city`"):
        pro.normalize_county("Oakland")
    with pytest.raises(ValueError, match="county is required"):
        pro.normalize_county("  ")
    with pytest.raises(ValueError, match="San Bernardino"):
        pro.find_licensed_professional("attorney", "San Bernadino")
    with pytest.raises(ValueError, match="58 counties"):
        pro.who_must_sign("calgreen_and_stormwater", {"construction_general_permit_required": True}, county="Mars")
    with pytest.raises(ValueError, match="city must be a city name"):
        pro.find_licensed_professional("attorney", "Fresno", city="<script>")


# ---------------------------------------------------------------------------- find_licensed_professional
def test_find_by_type_alias_and_plain_words():
    a = pro.find_licensed_professional("attorney", "Los Angeles", city="Pasadena")
    assert a.type_id == "attorney" and a.county == "Los Angeles" and a.city == "Pasadena"
    assert a.when_required and all(b.status in ("confirmed", "unconfirmed") for b in a.when_required)
    assert a.neutral_note == pro.NEUTRAL_NOTE and "does not recommend" in a.neutral_note
    assert a.verify_license_how and a.bring_or_ask and any("recommend" in s.lower() for s in a.do_not_say)
    assert {d.official_owner for d in a.directories} == {"The State Bar of California"}
    referral = next(d for d in a.directories if "referral" in d.label.lower())
    assert not referral.link_checked and referral.status.startswith("blocked:") and not referral.county_filtered
    assert "Los Angeles County" in referral.how_to_search
    assert a.verified is False                      # the attorney rule it cites is not yet checked in the rulebook
    pe = pro.find_licensed_professional("professional_engineer_civil", "Fresno")
    assert pe.type_id == "professional_engineer" and "professional_engineer_civil" in \
        next(t.also_called for t in pro.list_professional_types().types if t.type_id == "professional_engineer")
    assert pro.find_licensed_professional(need="a lawyer to foreclose our mechanics lien", county="Kern").type_id \
        == "attorney"
    assert pro.find_licensed_professional(need="someone to write our SWPPP", county="Kern").type_id == "qsd_qsp"
    assert pro.find_licensed_professional(need="get a document notarized", county="Kern").type_id == "notary_public"
    stamped = pro.find_licensed_professional(need="stamped structural plans", county="Kern")
    assert stamped.type_id in ("professional_engineer", "architect")
    assert pro.find_licensed_professional(need="respirator medical evaluation").type_id == "physician_or_plhcp"
    assert pro.find_licensed_professional("attorney").county is None          # county is optional: statewide links
    with pytest.raises(ValueError, match="Did you mean architect"):
        pro.find_licensed_professional("architekt", "Kern")
    with pytest.raises(ValueError, match="professional_type .* or need"):
        pro.find_licensed_professional(None, "Kern")
    with pytest.raises(ValueError, match="No professional type matches"):
        pro.find_licensed_professional(need="zzzz qqqq", county="Kern")


def test_county_filtered_links_only_where_the_official_site_takes_the_county():
    f = pro.find_licensed_professional("apprenticeship_committee", "Fresno", craft="carpenter")
    das = f.directories[0]
    assert das.county_filtered and das.url == ("https://www.dir.ca.gov/databases/das/results_pwadd.asp?"
                                               "varCounty=FRESNOD&varType=10&Submit=Search")
    assert das.search_page_url == "https://www.dir.ca.gov/databases/das/pwaddrstart.asp"
    la = pro.find_licensed_professional("apprenticeship_committee", "Los Angeles", craft="Electrical & Electronic")
    assert la.directories[0].url.endswith("varCounty=LOS+ANGELESE&varType=22&Submit=Search")
    # no craft: DIR's results page needs one, so the search page and what to choose
    plain = pro.find_licensed_professional("apprenticeship_committee", "Fresno").directories[0]
    assert not plain.county_filtered and plain.url == plain.search_page_url and "Fresno County" in plain.how_to_search
    # no other type has a tested county pattern: never a guessed filter
    for tid in pro.types():
        if tid != "apprenticeship_committee":
            assert not any(d.county_filtered for d in pro.directories(tid, "Fresno", "Fresno", "carpenter")), tid
    assert pro.craft_code("plumber", pro.types()["apprenticeship_committee"]["das_craft_codes"]) == "58"
    assert pro.craft_code("basket weaving", pro.types()["apprenticeship_committee"]["das_craft_codes"]) is None


def test_city_pages_are_never_the_link_for_a_job_elsewhere():
    sd = pro.find_licensed_professional("building_department", "San Diego", city="San Diego")
    la_page = next(d for d in sd.directories if "lacity" in d.url)
    assert la_page.applies_here is False and la_page.only_for == "City of Los Angeles jobs only"
    assert sd.directories[-1] is la_page or not sd.directories[-1].applies_here     # listed last
    assert pro.primary_directory("special_inspector", "San Diego") is None           # LADBS roster: LA only
    assert pro.primary_directory("special_inspector", "Los Angeles").url.startswith("https://www.ladbsservices2")


# ---------------------------------------------------------------------------- who_must_sign: real scenarios
def test_lien_deadline_needs_an_attorney_with_the_foreclosure_date():
    result = run("calculate_lien_deadline", LIEN)
    out = pro.who_must_sign("calculate_lien_deadline", result, LIEN, county="Sacramento")
    att = by_role(out.handoffs)["attorney"]
    assert att.type_id == "attorney" and att.requirement == "conditional"
    assert att.requirement_label == "Required by law if this applies (legal basis not yet verified)"
    assert att.due_date == result["foreclosure_deadline_if_recorded_on_last_day"]
    assert att.verified is False and "not yet confirmed" in att.legal_basis       # Civil Code 8460 was not read
    assert att.find_url.startswith("https://www.calbar.ca.gov/") and not att.county_filtered
    assert out.verified is False and out.county == "Sacramento" and out.mapped_conditions == 1


def test_swppp_needs_a_qsd_or_qsp():
    out = pro.who_must_sign("calgreen_and_stormwater", run("calgreen_and_stormwater", SWPPP))
    (qsd,) = out.handoffs
    assert qsd.type_id == "qsd_qsp" and qsd.requirement == "required" and "QSD" in qsd.why
    assert qsd.find_url.startswith("https://www.waterboards.ca.gov/") and qsd.find_link_checked is False
    small = pro.who_must_sign("calgreen_and_stormwater",
                              run("calgreen_and_stormwater", {**SWPPP, "disturbed_acres": 0.2}))
    assert small.handoffs == []


def test_big_building_needs_an_architect_and_an_engineer():
    out = pro.who_must_sign("project_setup_checklist", run("project_setup_checklist", BIG_JOB), BIG_JOB)
    roles = by_role(out.handoffs)
    assert {"architect", "professional_engineer_civil", "special_inspector", "qsd_qsp",
            "certified_asbestos_consultant", "competent_person"} <= set(roles)
    pe = roles["professional_engineer_civil"]
    assert pe.type_id == "professional_engineer" and pe.requirement == "required"
    assert {a.handoff_id for a in pe.also} >= {"ps-excavation-engineer", "ps-falsework-engineer"}
    assert roles["architect"].find_url == "https://www.cab.ca.gov/cons/archs/lic_search.shtml"
    assert roles["special_inspector"].find_url is None and "building department" in roles["special_inspector"].check_how
    assert out.county == "San Diego"
    levels = [pro.REQUIREMENT_ORDER.index(h.requirement) for h in out.handoffs]
    assert levels == sorted(levels)                 # required first; one item per person
    assert len(roles) == len(out.handoffs)


def test_home_improvement_contract_suggests_an_attorney_and_needs_a_registered_salesperson():
    out = pro.who_must_sign("validate_home_improvement_contract", run("validate_home_improvement_contract", HIC))
    roles = by_role(out.handoffs)
    att = roles["attorney"]
    assert att.requirement == "recommended" and att.requirement_label == "Suggested, not required by law"
    assert {att.handoff_id, *[a.handoff_id for a in att.also]} == {"vhic-attorney-terms", "vhic-arbitration"}
    his = roles["home_improvement_salesperson"]
    assert his.requirement == "required" and his.type_id == "licensed_contractor"
    assert his.find_url == "https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx"
    assert his.find_link_checked is True


def test_public_work_links_the_county_and_craft_list_when_both_are_known():
    out = pro.who_must_sign("public_works_readiness", run("public_works_readiness", PUBLIC_WORK), PUBLIC_WORK,
                            county="Fresno")
    (app_c,) = out.handoffs
    assert app_c.county_filtered and "varCounty=FRESNOD&varType=10" in app_c.find_url     # craft from the inputs
    assert app_c.verified is True and app_c.requirement_label == "Required by law"
    assert app_c.due_date == run("public_works_readiness", PUBLIC_WORK)["apprenticeship"]["das140_due"]


def test_who_must_sign_input_checks():
    assert pro.who_must_sign("check_change_order", {"anything": 1}).handoffs == []
    assert "No licensed-professional handoffs are mapped" in pro.who_must_sign("not_mapped", {}).note
    assert pro.who_must_sign("calgreen_and_stormwater",
                             json.dumps({"construction_general_permit_required": True})).handoffs
    for bad, msg in [("../etc", "tool_name"), ("", "tool_name")]:
        with pytest.raises(ValueError, match=msg):
            pro.who_must_sign(bad, {})
    with pytest.raises(ValueError, match="JSON output"):
        pro.who_must_sign("calgreen_and_stormwater", "not json")
    with pytest.raises(ValueError, match="too long"):
        pro.who_must_sign("calgreen_and_stormwater", "x" * (pro.MAX_RESULT_JSON + 1))


# ---------------------------------------------------------------------------- MCP tools
def test_mcp_tools_are_registered_read_only_and_run_a_tool_from_its_inputs():
    import server
    swppp_result = run("calgreen_and_stormwater", SWPPP)

    async def go():
        async with Client(server.mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            listed = (await c.call_tool("list_professional_types", {})).structured_content
            found = (await c.call_tool("find_licensed_professional",
                                       {"need": "lawyer for a lien lawsuit", "county": "Orange"})).structured_content
            ran = (await c.call_tool("who_must_sign", {"tool_name": "calgreen_and_stormwater",
                                                        "arguments": SWPPP})).structured_content
            given = (await c.call_tool("who_must_sign", {"tool_name": "calgreen_and_stormwater",
                                                          "result": swppp_result}))
            bad = await c.call_tool("find_licensed_professional", {"professional_type": "attorney",
                                                                   "county": "Atlantis"}, raise_on_error=False)
            loop = await c.call_tool("who_must_sign", {"tool_name": "who_must_sign", "arguments": {}},
                                     raise_on_error=False)
            return tools, listed, found, ran, given.structured_content, bad, loop
    tools, listed, found, ran, given, bad, loop = asyncio.run(go())
    for name in ("find_licensed_professional", "who_must_sign", "list_professional_types"):
        ann = tools[name].annotations.model_dump(by_alias=True, exclude_none=True)
        assert ann["readOnlyHint"] is True and ann["openWorldHint"] is False, name
    assert "never recommend" in tools["find_licensed_professional"].description.lower()
    assert len(listed["counties"]) == 58 and len(listed["types"]) == len(pro.types())
    assert found["type_id"] == "attorney" and found["county"] == "Orange"
    assert ran["ran_tool"] is True and given["ran_tool"] is False
    assert [h["role_id"] for h in ran["handoffs"]] == [h["role_id"] for h in given["handoffs"]] == ["qsd_qsp"]
    assert bad.is_error and "58 counties" in bad.content[0].text
    assert loop.structured_content is None or loop.structured_content["handoffs"] == []     # not mapped: nothing run


def test_who_must_sign_runs_only_read_only_tools():
    mcp = FastMCP("t")

    @mcp.tool(name="calgreen_and_stormwater")              # a mapped name, but this one may change data
    def writes(disturbed_acres: float) -> dict:
        return {"construction_general_permit_required": True}
    pro.register(mcp)

    async def go():
        async with Client(mcp) as c:
            return await c.call_tool("who_must_sign", {"tool_name": "calgreen_and_stormwater",
                                                       "arguments": {"disturbed_acres": 2}}, raise_on_error=False)
    r = asyncio.run(go())
    assert r.is_error and "not a read-only tool" in r.content[0].text


# ---------------------------------------------------------------------------- the web app
@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        signup(c)
        yield c


def new_key(owner, **body) -> dict:
    r = owner.post("/api/keys", json={"name": body.pop("name", "agent"), **body})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(key):
    return {"authorization": f"Bearer {key['key']}"}


def test_tool_results_carry_handoffs_without_changing_the_result(app, owner):
    r = owner.post("/api/tools/calgreen_and_stormwater", json=SWPPP)
    assert r.status_code == 200
    body = r.json()
    assert body["structured"] == run("calgreen_and_stormwater", SWPPP)         # the result itself is untouched
    (h,) = body["handoffs"]
    for field, kind in CONTRACT.items():
        assert isinstance(h[field], kind), field
    assert h["find_url"] is None or h["find_url"].startswith("https://")
    assert h["type_id"] == "qsd_qsp" and h["county_filtered"] is False
    # the result signature still covers exactly the result, so saving it keeps the provenance check working
    from app.api import result_signed
    assert result_signed(app.state.platform, "calgreen_and_stormwater", body["structured"], body["result_sig"])
    saved = {"tool": "calgreen_and_stormwater", "result": body["structured"], "result_sig": body["result_sig"]}
    doc = owner.post("/api/documents/from_result", json=saved)
    assert doc.status_code == 201 and doc.json()["source_tool"] == "calgreen_and_stormwater"
    # a result that needs nobody gets no field at all
    none = owner.post("/api/tools/calgreen_and_stormwater", json={**SWPPP, "disturbed_acres": 0.1}).json()
    assert "handoffs" not in none
    lic = owner.post("/api/tools/lookup_safety_standard", json={"hazard": "trench"}).json()
    assert "handoffs" not in lic


def test_more_scenarios_through_the_web_app(owner):
    lien = owner.post("/api/tools/calculate_lien_deadline", json=LIEN).json()
    assert [h["type_id"] for h in lien["handoffs"]] == ["attorney"]
    job = owner.post("/api/tools/project_setup_checklist", json=BIG_JOB).json()
    types = {h["type_id"] for h in job["handoffs"]}
    assert {"architect", "professional_engineer", "qsd_qsp"} <= types
    assert all(h["find_url"] is None or "lacity" not in h["find_url"] for h in job["handoffs"])   # a San Diego job


def test_the_hook_never_breaks_a_tool_call(owner, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("handoff map broken")
    monkeypatch.setattr(pro, "handoffs_for", boom)
    with caplog.at_level(logging.ERROR, logger="tradecomply.api"):
        r = owner.post("/api/tools/calgreen_and_stormwater", json=SWPPP)
    assert r.status_code == 200 and "handoffs" not in r.json() and r.json()["structured"]["verified"] in (True, False)
    assert "handoffs for calgreen_and_stormwater could not be computed" in caplog.text
    monkeypatch.setattr(pro, "handoffs_for", lambda *a, **k: [])
    assert "handoffs" not in owner.post("/api/tools/calgreen_and_stormwater", json=SWPPP).json()
    monkeypatch.undo()
    # a result of an unexpected shape is skipped condition by condition, never an error
    assert pro.handoffs_for("calgreen_and_stormwater", {"result": [1, 2]}) == []
    assert pro.handoffs_for("calgreen_and_stormwater", None) == []


def test_professionals_routes(app, owner):
    with TestClient(app) as anon:
        assert anon.get("/api/professionals/types").status_code == 401
        assert anon.get("/api/professionals/find?type=attorney").status_code == 401
    t = owner.get("/api/professionals/types")
    assert t.status_code == 200 and t.json() == pro.list_professional_types().model_dump()
    assert t.headers["cache-control"] == "private, max-age=300"
    f = owner.get("/api/professionals/find", params={"type": "architect", "county": "san diego", "city": "Chula Vista"})
    assert f.status_code == 200
    assert f.json() == pro.find_licensed_professional("architect", "San Diego", "Chula Vista").model_dump()
    assert owner.get("/api/professionals/find", params={"type_id": "cpa"}).json()["type_id"] == "cpa"
    assert owner.get("/api/professionals/find", params={"need": "elevator mechanic"}).json()["type_id"] == \
        "certified_conveyance_mechanic"
    bad = owner.get("/api/professionals/find", params={"type": "attorney", "county": "Los Angelos"})
    assert bad.status_code == 422 and "Did you mean Los Angeles?" in bad.json()["detail"]
    assert owner.get("/api/professionals/find", params={"type": "wizard"}).status_code == 422
    assert owner.get("/api/professionals/find", params={"need": "x" * 501}).status_code == 422
    key = new_key(owner)
    with TestClient(app) as bot:
        assert bot.get("/api/professionals/find?type=attorney&county=Kern", headers=bearer(key)).status_code == 200
    tools_only = new_key(owner, scopes={"rest_api": False})
    with TestClient(app) as bot:
        assert bot.get("/api/professionals/types", headers=bearer(tools_only)).status_code == 403
        assert bot.post("/api/tools/list_professional_types", json={}, headers=bearer(tools_only)).status_code == 200


def test_api_key_scope_holds_when_who_must_sign_runs_a_tool(app, owner):
    narrow = new_key(owner, scopes={"tools": ["who_must_sign"]})
    both = new_key(owner, scopes={"tools": ["who_must_sign", "calgreen_and_stormwater"]})
    body = {"tool_name": "calgreen_and_stormwater", "arguments": SWPPP}
    with TestClient(app) as bot:
        r = bot.post("/api/tools/who_must_sign", json=body, headers=bearer(narrow))
        assert r.status_code == 422 and "not allowed to use the tool 'calgreen_and_stormwater'" in r.json()["detail"]
        given = {"tool_name": "calgreen_and_stormwater", "result": run("calgreen_and_stormwater", SWPPP)}
        ok = bot.post("/api/tools/who_must_sign", json=given, headers=bearer(narrow))
        assert ok.status_code == 200 and ok.json()["structured"]["handoffs"][0]["type_id"] == "qsd_qsp"
        ran = bot.post("/api/tools/who_must_sign", json=body, headers=bearer(both))
        assert ran.status_code == 200 and ran.json()["structured"]["ran_tool"] is True
    # people signed in to the web app may run any tool through it
    assert owner.post("/api/tools/who_must_sign", json=body).json()["structured"]["ran_tool"] is True
    assert pro.RUN_GUARD.get() is None                          # reset after each call


def sse_json(text: str):
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return json.loads(text)


def test_api_key_scope_holds_over_mcp(app, owner):
    def session(c, key):
        h = {**MCP_HEADERS, **bearer(key)}
        r = c.post("/mcp", json=INIT, headers=h)
        assert r.status_code == 200, r.text
        sid = r.headers.get("mcp-session-id")
        h2 = {**h, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"} if sid else h
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h2)
        return h2

    def who(c, h, rid):
        return sse_json(c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {
            "name": "who_must_sign", "arguments": {"tool_name": "calgreen_and_stormwater", "arguments": SWPPP}}}).text)
    narrow = new_key(owner, scopes={"tools": ["who_must_sign"]})
    by_category = new_key(owner, scopes={"tools": ["who_must_sign"], "categories": ["building"]})
    default = new_key(owner)
    with TestClient(app) as bot:
        refused = who(bot, session(bot, narrow), 2)["result"]
        assert refused["isError"] is True and "not allowed to use the tool" in refused["content"][0]["text"]
        for key in (by_category, default):
            ok = who(bot, session(bot, key), 3)["result"]
            assert not ok.get("isError") and ok["structuredContent"]["ran_tool"] is True
            assert ok["structuredContent"]["handoffs"][0]["type_id"] == "qsd_qsp"


# ---------------------------------------------------------------------------- security review regressions
def test_a_huge_result_passed_in_is_refused_not_walked():
    """who_must_sign walks a caller's result with every handoff test of the tool: 65,000 small items took ~4 s of
    CPU per call. Results passed in are capped (values at any depth); results this server made are not."""
    items = [{"title": "Plans prepared and stamped", "why": ""} for _ in range(pro.MAX_RESULT_NODES // 3 + 10)]
    for given in ({"phases": [{"items": items}]}, json.dumps({"phases": [{"items": items}]}), [items]):
        with pytest.raises(ValueError, match="too large"):
            pro.who_must_sign("project_setup_checklist", given)
    deep: dict = {}
    for _ in range(pro.MAX_RESULT_NODES + 1):          # deep nesting counts too (and is never recursed into)
        deep = {"a": deep}
    with pytest.raises(ValueError, match="too large"):
        pro.who_must_sign("project_setup_checklist", deep)
    with pytest.raises(ValueError, match="JSON output"):
        pro.who_must_sign("project_setup_checklist", "[" * 100000 + "]" * 100000)
    # this server's own result (who_must_sign ran the tool from its arguments) is not capped
    assert pro.who_must_sign("project_setup_checklist", {"phases": [{"items": items}]}, ran_tool=True).tool
    # real results are far below the cap
    assert not pro._too_big(run(*CASES["cal"]))


def test_who_must_sign_over_mcp_works_off_the_event_loop(monkeypatch):
    import threading
    import server
    seen = []
    real = pro.who_must_sign

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)
    monkeypatch.setattr(pro, "who_must_sign", spy)

    async def go():
        loop_thread = threading.get_ident()
        async with Client(server.mcp) as c:
            r = await c.call_tool("who_must_sign", {"tool_name": "calgreen_and_stormwater",
                                                     "result": {"construction_general_permit_required": True}})
        return loop_thread, r.structured_content
    loop_thread, out = asyncio.run(go())
    assert seen and all(t != loop_thread for t in seen)
    assert [h["role_id"] for h in out["handoffs"]] == ["qsd_qsp"]


def test_only_plain_https_registry_links_are_ever_returned(monkeypatch):
    """Defense in depth for the data file: a link that isn't plain https is left out, and a county-filtered link
    that would leave the official search page's site falls back to the search page."""
    t = json.loads(json.dumps(pro.types()["apprenticeship_committee"]))
    das = next(f for f in t["find"] if f.get("url_pattern_with_county"))
    das["url_pattern_with_county"] = das["url_pattern_with_county"].replace("https://www.dir.ca.gov/",
                                                                            "https://evil.example/")
    t["find"] = [{"label": "Bad", "url": "javascript:alert(1)", "status": "reachable"},
                 {"label": "Plain http", "url": "http://www.dir.ca.gov/", "status": "reachable"}, *t["find"]]
    fake = {**pro.types(), "apprenticeship_committee": t}
    monkeypatch.setattr(pro, "types", lambda: fake)
    dirs = pro.directories("apprenticeship_committee", "Fresno", None, "carpenter")
    assert dirs and all(d.url.startswith("https://www.dir.ca.gov/") for d in dirs)
    assert not any(d.county_filtered for d in dirs)
    listed = next(x for x in pro.list_professional_types().types if x.type_id == "apprenticeship_committee")
    assert listed.directories and all(pro.official_url(d.url) for d in listed.directories)
    for bad in ("javascript:alert(1)", "http://x.gov/", "https://x.gov/a b", 'https://x.gov/"x', "https://x.gov\\y",
                "data:text/html,hi", None, 5):
        assert not pro.official_url(bad), bad
    # every link in the real data is a plain https address
    assert all(pro.official_url(f["url"]) for tt in pro.registry()["types"] for f in tt.get("find", []))
