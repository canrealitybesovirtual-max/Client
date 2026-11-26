"""Equipment emissions module: CARB off-road fleets, contracting duties, heavy trucks, jobsite dust and portable engines.

Expected values are worked by hand from the rule text in research/web_air-equipment.verified.json (the arithmetic is
in the comments), never read back from the module.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, equipment_emissions as ee

AS_OF = "2026-11-25"           # Wednesday


def ids(result):
    return {c.rule_id for c in result.citations}


def req(result, prefix):
    return next(r for r in result.requirements if r.item.startswith(prefix))


def exact(result, item):
    return next(r for r in result.requirements if r.item == item)


def has_req(result, prefix):
    return any(r.item.startswith(prefix) for r in result.requirements)


def veh(result, name):
    return next(v for v in result.vehicles if v.name == name)


# ---------------------------------------------------------------- rulebook wiring
def test_module_uses_exactly_the_verified_air_pack():
    book = {r.id: r for r in core.all_rules()}
    pack = {r.id for r in book.values() if r.pack == "web_air-equipment"}
    assert len(pack) == 138 and set(ee.RULE_IDS) == pack and len(ee.RULE_IDS) == len(set(ee.RULE_IDS))
    assert all(book[i].verified for i in ee.RULE_IDS)          # 107 confirmed + 31 corrected, none unverifiable
    assert book[ee.ORD_ADD_ONROAD].status == "corrected"       # optional_500hp_or_less moved from 2035 to 2028
    assert book[ee.ORD_ADD_ONROAD].param("effective_by_fleet_size.optional_500hp_or_less") == "2028-01-01"


def test_district_county_tables_match_the_rules_own_scope_text():
    # The county lists are spelling help; every name must appear in the rule's applies_to (or SJV parameters).
    for key, (full, partial) in ee._SCOPE_COUNTIES.items():
        text = core.rule(ee._SCOPE_RULE[key]).applies_to
        for c in full:
            assert c in text, (key, c)
        for c, part in partial.items():
            assert c in text and part.replace("the ", "") in text, (key, c, part)
    sjv = core.rule(ee.SJV_COUNTIES)
    assert sjv.param("counties") == ["San Joaquin", "Stanislaus", "Merced", "Madera", "Fresno", "Kings", "Tulare"]
    assert sjv.param("partial").startswith("Kern")


def test_every_pack_rule_is_cited_by_some_call():
    calls = [
        ee.offroad_fleet_obligations(6000, vehicles=[
            {"name": "a", "hp": 100, "engine_tier": "Tier 0", "designation": "permanent low-use", "annual_hours": 90},
            {"name": "b", "hp": 300, "engine_tier": "Tier 2", "annual_hours": 120}],
            operating_counties=["Fresno"], rents_out_equipment=True, new_fleet=True, vehicle_added_on="2026-11-20",
            as_of=AS_OF),
        ee.offroad_fleet_obligations(1000, as_of=AS_OF), ee.offroad_fleet_obligations(3000, as_of=AS_OF),
        ee.offroad_fleet_obligations(450, uses_optional_500hp_schedule=True,
                                     vehicles=[{"hp": 150, "engine_tier": "Tier 2"}], as_of=AS_OF),
        ee.offroad_contracting_requirements("prime", first_vehicle_operates_on="2027-01-04", contract_date="2027-01-01",
                                            project_completion_date="2027-09-30",
                                            noncompliance_discovered_on=AS_OF, hires_haul_trucks=True, as_of=AS_OF),
        ee.offroad_contracting_requirements("awarding body", emergency_operations=True, as_of=AS_OF),
        ee.truck_compliance(33000, 2008, registration_expires="2027-03-15", leased=True, selling=True,
                            acquired_on="2026-11-01", vehicle_model_year=2007, as_of=AS_OF),
        ee.truck_compliance(30000, 2005, ca_registered=False, operating_counties=["Humboldt"], has_pm_filter=True,
                            owner_type="city", as_of=AS_OF),
        ee.dust_control_requirements("South Coast", 60, daily_import_export_cy=400, wind_gust_mph=30,
                                     coachella_valley=True, as_of=AS_OF),
        ee.dust_control_requirements("Fresno", 12, demolition=True, vehicle_trips_per_day=200, urban_area=True,
                                     earthmoving_start="2027-02-01", as_of=AS_OF),
        ee.dust_control_requirements("Fresno", existing_building_sqft=1000, added_building_sqft=100, as_of=AS_OF),
        ee.dust_control_requirements("Fresno", 3, project_type="residential", earthmoving_start="2027-01-06",
                                     as_of=AS_OF),
        ee.dust_control_requirements("Bay Area", 2, near_sensitive_receptors=True, as_of=AS_OF),
        ee.portable_engine_requirements(300, "Tier 4 Final", months_at_location=6, registration_expires="2027-06-30",
                                        fleet_total_bhp=600, annual_hours=100, project_registered_bhp_on_site=3000,
                                        extreme_ozone_district=True, engines_to_register=1, as_of=AS_OF),
        ee.portable_engine_requirements(300, propels_vehicle=True, as_of=AS_OF),
    ]
    cited = set().union(*(ids(c) for c in calls))
    assert cited == set(ee.RULE_IDS), sorted(set(ee.RULE_IDS) - cited)
    assert all(c.verified for c in calls)


def test_an_unverifiable_rule_turns_verified_off(monkeypatch):
    monkeypatch.setattr(core.rule(ee.CTC_FEE), "status", "unverifiable")
    r = ee.truck_compliance(33000, 2016, as_of=AS_OF)
    assert r.verified is False and r.unverified_rule_ids == [ee.CTC_FEE]
    assert any(n.startswith("NOT VERIFIED (unverifiable)") for n in r.notes)


# ---------------------------------------------------------------- off-road fleet size (13 CCR 2449(c)(28))
@pytest.mark.parametrize("hp,size", [(2500, "small"), (2501, "medium"), (5000, "medium"), (5001, "large")])
def test_fleet_size_boundaries(hp, size):
    # small <= 2,500 hp; large > 5,000 hp; medium = neither.
    assert ee.offroad_fleet_obligations(hp, as_of=AS_OF).fleet_size == size


@pytest.mark.parametrize("owner,hp,size", [
    ("State of California", 300, "large"), ("federal agency", 300, "large"),       # always_large
    ("training center", 9000, "small"), ("low population county", 8000, "small"),  # always_small
    ("City", 3000, "medium"), ("non-profit", 2000, "small"), ("LLC", 6000, "large"),
])
def test_owner_types(owner, hp, size):
    assert ee.offroad_fleet_obligations(hp, fleet_owner_type=owner, as_of=AS_OF).fleet_size == size


def test_captive_attainment_area_fleet_is_small_green_and_exempt_from_renewable_diesel():
    # 2449(c)(6): every operating county listed -> captive -> always small; EIN white on green; 2449.1(f)(2)(A).
    r = ee.offroad_fleet_obligations(7000, operating_counties=["Humboldt County", "del norte"], as_of=AS_OF)
    assert r.captive_attainment_area is True and r.fleet_size == "small"
    assert "white on green" in req(r, "EIN label").detail
    assert r.renewable_diesel_required is False and ee.ORD_CAPTIVE in exact(r, "Renewable diesel").rule_ids
    assert "certify no vehicle operated outside" in req(r, "ROAR").detail
    # Sonoma counts only for its North Coast Air Basin part
    assert ee.offroad_fleet_obligations(7000, operating_counties=["Humboldt", "Sonoma"],
                                        as_of=AS_OF).captive_attainment_area is None
    assert ee.offroad_fleet_obligations(7000, operating_counties=["Humboldt", "northern Sonoma"],
                                        as_of=AS_OF).captive_attainment_area is True
    out = ee.offroad_fleet_obligations(7000, operating_counties="Humboldt, Fresno", as_of=AS_OF)
    assert out.captive_attainment_area is False and out.fleet_size == "large"
    assert "Fresno not in the 2449(c)(6) list" in " ".join(out.math)
    # a state fleet stays large even in the listed counties
    assert ee.offroad_fleet_obligations(300, "state agency", operating_counties=["Humboldt"],
                                        as_of=AS_OF).fleet_size == "large"


def test_fleet_hp_from_the_machine_list():
    # 2449(b)(1): 25 hp or more; 2449(c)(64): permanent low-use, snow-removal and emergency-only left out.
    # Counted: 158 + 93 + 250 = 501 hp (24 hp machine, electric machine and the low-use 300 hp machine left out).
    r = ee.offroad_fleet_obligations(vehicles=[
        {"name": "ex", "hp": 158, "engine_tier": "Tier 2"}, {"name": "bh", "hp": 93, "engine_tier": "T1"},
        {"name": "ld", "hp": 250, "engine_tier": "tier-3"}, {"name": "mini", "hp": 24, "engine_tier": "T4f"},
        {"name": "ev", "hp": 60, "engine_tier": "battery electric"},
        {"name": "lu", "hp": 300, "engine_tier": "Tier 3", "designation": "low use"}], as_of=AS_OF)
    assert r.total_fleet_hp == 501 and r.fleet_size == "small"
    assert "Fleet hp = 158 + 93 + 250 = 501 hp" in r.math
    assert veh(r, "mini").status == "not_subject" and veh(r, "ev").status == "not_subject"
    assert veh(r, "lu").counted_in_fleet_hp is False and veh(r, "lu").subject is True
    assert "Room before medium: 2,500 - 501 = 1,999 hp" in r.math
    assert "Adding more than 1,999 hp" in req(r, "If the fleet grows").detail
    # given total wins (common ownership), with a note
    r2 = ee.offroad_fleet_obligations(3000, vehicles=[{"hp": 158, "engine_tier": "Tier 2"}], as_of=AS_OF)
    assert r2.fleet_size == "medium" and any("158 counted hp" in n for n in r2.notes)


# ---------------------------------------------------------------- phase-out and adding (2449(d)(6), 2449.1(c))
def test_large_fleet_phase_out_per_machine():
    # 2449.1(c)(1): Tier 0 / MY<=1994 from 2024-01-01; Tier 1 / MY<=1999 from 2026-01-01; Tier 2 / MY<=2003 from
    # 2028-01-01. As of 2026-11-25 the first two are in force.
    r = ee.offroad_fleet_obligations(6000, vehicles=[
        {"name": "t0", "hp": 100, "engine_tier": "Tier 0"}, {"name": "t1", "hp": 200, "engine_tier": "Tier 1"},
        {"name": "t2", "hp": 300, "engine_tier": "Tier 2"}, {"name": "or98", "hp": 400, "engine_tier": "on-road 1998"},
        {"name": "or03", "hp": 400, "engine_tier": "On-Road MY2003"},
        {"name": "or04", "hp": 400, "engine_tier": "onroad 2004"}, {"name": "t4f", "hp": 400, "engine_tier": "T4F"}],
        as_of=AS_OF)
    assert r.fleet_size == "large"
    assert veh(r, "t0").status == "may_not_operate" and veh(r, "t0").may_not_operate_from == "2024-01-01"
    assert veh(r, "t1").status == "may_not_operate" and veh(r, "t1").may_not_operate_from == "2026-01-01"
    t2 = veh(r, "t2")
    assert t2.status == "phase_out_scheduled" and t2.may_not_operate_from == "2028-01-01"
    assert t2.designate_by == "2027-12-31"               # 2449.1(c): Dec 31 of the year before
    assert t2.zev_delay_to == "2030-01-01"               # 2449.1(d): 2028-01-01 + 2 years
    assert "(402 days)" in t2.detail                     # 2026-11-25 -> 2028-01-01 = 36 + 366 = 402 days
    assert veh(r, "or98").may_not_operate_from == "2026-01-01"   # 1998 <= 1999 (Tier 1 row)
    assert veh(r, "or03").may_not_operate_from == "2028-01-01"   # 2003 <= 2003 (Tier 2 row)
    assert veh(r, "or04").status == "no_phase_out_date" and veh(r, "t4f").status == "no_phase_out_date"
    assert r.may_add_now == "Tier 4 final or higher"     # (d)(6)(E): large/medium from 2024-01-01
    assert r.may_add_onroad_engine_model_year_min == 2007   # (d)(6)(G): large/medium from 2024-01-01
    assert has_req(r, "Written idling policy")           # medium and large fleets only
    assert r.next_step.startswith("Stop operating t0, t1, and or98 in California now")
    assert any("fleet average / BACT" in n for n in r.notes)


def test_medium_and_small_schedules():
    # 2449.1(c)(2): medium Tier 0 2026, Tier 1 2028, Tier 2 2030. (c)(3): small Tier 0 2028, Tier 1 2030, Tier 2 2032.
    m = ee.offroad_fleet_obligations(3000, vehicles=[{"name": "t0", "hp": 100, "engine_tier": "T0"},
                                                     {"name": "t2", "hp": 100, "engine_tier": "T2"}], as_of=AS_OF)
    assert [(s.date, s.tier, s.in_effect) for s in m.phase_out_schedule] == [
        ("2026-01-01", "Tier 0", True), ("2028-01-01", "Tier 1", False), ("2030-01-01", "Tier 2", False)]
    assert veh(m, "t0").status == "may_not_operate" and veh(m, "t2").may_not_operate_from == "2030-01-01"
    s = ee.offroad_fleet_obligations(1000, vehicles=[{"name": "t0", "hp": 100, "engine_tier": "uncontrolled"}],
                                     as_of=AS_OF)
    assert [x.date for x in s.phase_out_schedule] == ["2028-01-01", "2030-01-01", "2032-01-01"]
    assert veh(s, "t0").designate_by == "2027-12-31" and not has_req(s, "Written idling policy")
    # small fleets may still add Tier 4 interim until 2027-12-31 ((d)(6)(F): small from 2028-01-01)
    assert s.may_add_now == "Tier 4 interim or higher" and s.may_add_onroad_engine_model_year_min is None
    assert any(c.startswith("From 2028-01-01: only Tier 4 final") for c in s.adding_changes)
    later = ee.offroad_fleet_obligations(1000, as_of="2028-01-01")
    assert later.may_add_now == "Tier 4 final or higher" and later.may_add_onroad_engine_model_year_min == 2007


def test_optional_500hp_schedule():
    # 2449(e)(16) Table 1: 75% of hp Tier 2 or better from 2026-01-01, 100% from 2029-01-01.
    # 150 (T2) + 150 (T3) = 300 of 450 hp = 66.7% < 75%; 75% x 450 = 337.5 hp -> short 37.5 hp.
    v = [{"hp": 150, "engine_tier": "Tier 2"}, {"hp": 150, "engine_tier": "Tier 3"}, {"hp": 150, "engine_tier": "T1"}]
    r = ee.offroad_fleet_obligations(450, vehicles=v, uses_optional_500hp_schedule=True, as_of=AS_OF)
    c = r.optional_schedule_check
    assert (c.percent_tier2_or_better, c.required_percent, c.required_since) == (66.7, 75.0, "2026-01-01")
    assert c.meets_current_requirement is False and (c.next_date, c.next_required_percent) == ("2029-01-01", 100.0)
    assert "short 37.5 hp" in " ".join(r.math) and "at least 37.5 hp" in r.next_step
    # 2449.1(c)(5): Tier 2 phase-out moves to 2036; (d)(6)(F): Tier 4 interim adds until 2034-12-31
    assert [x.date for x in r.phase_out_schedule] == ["2028-01-01", "2030-01-01", "2036-01-01"]
    assert r.may_add_now == "Tier 4 interim or higher"
    assert any(c.startswith("From 2035-01-01") for c in r.adding_changes)
    # the on-road 2006-or-earlier ban has no 500 hp exception (corrected rule): small fleet date 2028-01-01
    assert any(c.startswith("From 2028-01-01: an on-road-engine vehicle") for c in r.adding_changes)
    # offered (not applied) when the fleet qualifies but did not elect it
    assert has_req(ee.offroad_fleet_obligations(450, as_of=AS_OF), "Optional schedule for fleets of 500 hp")
    with pytest.raises(ValueError, match="only for fleets of 500 hp or less"):
        ee.offroad_fleet_obligations(501, uses_optional_500hp_schedule=True, as_of=AS_OF)


def test_low_use_and_special_provision_machines():
    # 2449.1(c)(4): designated permanent low-use Tier 0 may not operate from 2036-01-01; low use is < 200 h/yr.
    r = ee.offroad_fleet_obligations(1000, vehicles=[
        {"name": "lu0", "hp": 100, "engine_tier": "Tier 0", "designation": "Permanent Low-Use", "annual_hours": 250},
        {"name": "snow", "hp": 100, "engine_tier": "Tier 1", "designation": "snow removal"},
        {"name": "t1", "hp": 100, "engine_tier": "Tier 1", "annual_hours": 150}], as_of=AS_OF)
    lu = veh(r, "lu0")
    assert lu.status == "special_provision" and lu.may_not_operate_from == "2036-01-01"
    assert lu.designate_by == "2027-12-31"               # small Tier 0 date 2028-01-01 -> Dec 31, 2027
    assert "250 hours is NOT under 200" in lu.detail and lu.counted_in_fleet_hp is False
    assert veh(r, "snow").status == "special_provision" and veh(r, "snow").counted_in_fleet_hp is False
    assert "could qualify as permanent low-use" in veh(r, "t1").detail      # 150 < 200
    meter = req(r, "Low-use hour meter readings")
    assert "within 30 days of December 31" in meter.detail and meter.due == "2027-03-01"


# ---------------------------------------------------------------- reporting, labels, fuel (2449(f)-(h), 2449.1(f))
def test_reporting_dates_and_ein_deadlines():
    # 2449(g)(2): due March 1, vehicles as of Dec 31 of the prior year; small fleets report 2018-2036.
    r = ee.offroad_fleet_obligations(1000, new_fleet=True, vehicle_added_on="2026-11-20", as_of=AS_OF)
    assert (r.next_annual_report_due, r.report_vehicles_as_of) == ("2027-03-01", "2026-12-31")
    assert req(r, "ROAR").due == "2027-03-01" and req(r, "Renewable diesel affirmation").due == "2027-03-01"
    # 2449(f)(1)(A) and (d)(4)(C): 2026-11-20 + 30 days = 2026-12-20
    assert req(r, "EIN for each added machine").due == "2026-12-20" and req(r, "Report the new fleet").due == "2026-12-20"
    assert "white on red" in req(r, "EIN label").detail and "3 in tall and 1.5 in wide" in req(r, "EIN label").detail
    assert ee.offroad_fleet_obligations(1000, as_of="2027-03-01").next_annual_report_due == "2027-03-01"
    done = ee.offroad_fleet_obligations(1000, as_of="2036-03-02")             # 2037 > 2036: no more reports
    assert done.next_annual_report_due is None and any("2018-2036" in n for n in done.notes)
    rec = req(r, "Records").detail
    assert "5 business days" in rec and "2037-01-01" in rec              # 2449(h), (h)(11)
    assert "3 calendar years" in req(r, "Fuel records").detail             # 2449(h)(10)
    sale = req(r, "When you sell a machine").detail
    assert "within 30 days" in sale and "In-Use Off-road Diesel Vehicle Regulation" in sale


def test_renewable_diesel_rules():
    r = ee.offroad_fleet_obligations(1000, as_of=AS_OF)
    assert r.renewable_diesel_required is True
    rd = req(r, "Renewable diesel (R99/R100)").detail
    assert "R99 or R100" in rd and "below 20 F" in rd and "Nov, Dec, Jan, and Feb" in rd and "October 15" in rd
    assert "April 30" in req(r, "Cold-weather exemption reports").detail
    # 2449.1(f)(2)(B): a fleet made up only of Tier 4 final / MY2010+ on-road engines is exempt
    clean = ee.offroad_fleet_obligations(vehicles=[{"hp": 300, "engine_tier": "Tier 4 Final"},
                                                   {"hp": 300, "engine_tier": "on-road 2012"}], as_of=AS_OF)
    assert clean.renewable_diesel_required is False
    rent = ee.offroad_fleet_obligations(1000, rents_out_equipment=True, as_of=AS_OF)
    assert "within 5 days" in req(rent, "Your rental contracts").detail
    assert {ee.ORD_RENTAL_IDLE, ee.ORD_RENTAL_RD} <= ids(rent)


def test_no_covered_machines():
    r = ee.offroad_fleet_obligations(vehicles=[{"hp": 20, "engine_tier": "Tier 2"},
                                               {"hp": 80, "engine_tier": "electric"}], as_of=AS_OF)
    assert r.subject is False and r.fleet_size is None and "does not apply" in r.summary
    assert ids(r) == {ee.ORD_APPLIES, ee.ORD_HP_EXCL, ee.ORD_COMMON} and r.verified is True


@pytest.mark.parametrize("kwargs,msg", [
    ({"total_fleet_hp": -5}, "cannot be below 0"),
    ({"total_fleet_hp": "lots"}, "must be a number"),
    ({}, "give total_fleet_hp"),
    ({"vehicles": [{"hp": 100, "engine_tier": "Tier 4"}]}, "ambiguous"),
    ({"vehicles": [{"hp": 100, "engine_tier": "on-road"}]}, "needs its model year"),
    ({"vehicles": [{"hp": 100, "engine_tier": "Tier 9"}]}, "engine_tier must be"),
    ({"vehicles": [{"hp": 100, "engine_tier": "T2", "designation": "museum"}]}, "designation must be"),
    ({"total_fleet_hp": 100, "fleet_owner_type": "alien"}, "fleet_owner_type must be"),
    ({"total_fleet_hp": 100, "operating_counties": ["Gotham"]}, "not a California county"),
    ({"total_fleet_hp": 100, "as_of": "25/11/2026"}, "ISO date"),     # day-first: month 25 does not exist
    ({"total_fleet_hp": 100, "as_of": "next Tuesday"}, "ISO date"),
])
def test_fleet_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        ee.offroad_fleet_obligations(**kwargs)


# ---------------------------------------------------------------- contracting (2449(i)-(j))
def test_prime_contractor_dates():
    r = ee.offroad_contracting_requirements(
        "GC", first_vehicle_operates_on="2027-01-04", expected_operating_days=60, contract_date="2026-12-15",
        project_completion_date="2027-09-30", noncompliance_discovered_on=AS_OF, as_of=AS_OF)
    assert r.role == "prime_contractor" and r.duties_apply and r.certificates_needed_before_contract
    assert r.certificates_due == "2026-12-15"
    # (j)(5): 8+ days -> sign by calendar day 8; day 1 = 2027-01-04 -> day 8 = 2027-01-11
    assert r.sign_required is True and r.sign_post_by == "2027-01-11"
    assert "larger than 14-point" in req(r, "CARB off-road jobsite sign").detail
    # (j)(3): 5 business days from Wed 2026-11-25: Thu 26, Fri 27, Mon 30, Tue 1, Wed 2 -> 2026-12-02
    assert r.noncompliance_report_due == "2026-12-02"
    assert "dieselcomplaints@arb.ca.gov" in req(r, "Report uncertified").detail
    # (i)(3): 2027-09-30 + 3 years = 2030-09-30
    assert r.keep_certificates_until == "2030-09-30"
    # (j)(1): next March 1 - June 1 window after 2026-11-25
    assert (r.annual_certificate_window_start, r.annual_certificate_window_end) == ("2027-03-01", "2027-06-01")
    assert r.next_step.startswith("Report the fleet or machine to CARB by 2026-12-02")
    assert r.verified is True and {ee.ORD_CRC_BEFORE, ee.ORD_PRIME_SIGN, ee.ORD_PRIME_REPORT} <= ids(r)


def test_contracting_edges():
    # Friday 2026-11-27 + 5 business days: Mon 30, Tue 1, Wed 2, Thu 3, Fri 4 -> 2026-12-04
    assert ee.offroad_contracting_requirements(noncompliance_discovered_on="2026-11-27",
                                               as_of=AS_OF).noncompliance_report_due == "2026-12-04"
    # sign: 7 calendar days or less exempt, 8 or more required
    seven = ee.offroad_contracting_requirements(expected_operating_days=7, as_of=AS_OF)
    assert seven.sign_required is False and "exempt" in req(seven, "CARB off-road jobsite sign").detail
    assert ee.offroad_contracting_requirements(expected_operating_days=8, as_of=AS_OF).sign_required is True
    assert ee.offroad_contracting_requirements(as_of=AS_OF).sign_required is None     # unknown length
    # annual window: inside it -> this year's; after June 1 -> next year's
    assert ee.offroad_contracting_requirements(as_of="2027-04-10").annual_certificate_window_end == "2027-06-01"
    assert ee.offroad_contracting_requirements(as_of="2027-06-02").annual_certificate_window_start == "2028-03-01"
    # homeowner's own residence -> not a 'prime contractor' (2449(b)(2)(E))
    home = ee.offroad_contracting_requirements("prime", homeowner_personal_residence=True, as_of=AS_OF)
    assert home.duties_apply is False and ee.ORD_PRIME_DEF in ids(home)
    # emergency operations: no certificates, keep emergency records (2449(i)(4))
    em = ee.offroad_contracting_requirements("prime", emergency_operations=True, as_of=AS_OF)
    assert em.certificates_needed_before_contract is False and "attestation" in req(em, "Emergency project").detail
    # awarding body: (i) duties only
    ab = ee.offroad_contracting_requirements("school district", project_completion_date="2027-06-30", as_of=AS_OF)
    assert ab.role == "public_works_awarding_body" and ab.keep_certificates_until == "2030-06-30"
    assert not has_req(ab, "CARB off-road jobsite sign") and ab.annual_certificate_window_start is None
    sub = ee.offroad_contracting_requirements("sub", as_of=AS_OF)
    assert sub.duties_apply is False and "DOORS" in sub.next_step
    none = ee.offroad_contracting_requirements(offroad_vehicles_on_project=False, hires_haul_trucks=True, as_of=AS_OF)
    assert none.duties_apply is False and {ee.TB_HIRE, ee.CTC_HIRE} <= ids(none)


def test_hired_trucks():
    r = ee.offroad_contracting_requirements(hires_haul_trucks=True, as_of=AS_OF)
    ctc = req(r, "Hired heavy trucks: Clean Truck Check").detail
    assert "within 12 months" in ctc and "5 years" in ctc and "72 hours" in ctc    # 2197(a), 2197.3
    assert "bills of lading" in req(r, "Hired diesel trucks: Truck and Bus").detail  # 2025(s)(4)


@pytest.mark.parametrize("kwargs,msg", [
    ({"role": "owner's rep"}, "role must be"),
    ({"first_vehicle_operates_on": "2027-05-01", "project_completion_date": "2027-01-01"}, "is before"),
    ({"expected_operating_days": -1}, "cannot be below 0"),
    ({"expected_operating_days": 2.5}, "whole number"),
    ({"contract_date": "soon"}, "ISO date"),
])
def test_contracting_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        ee.offroad_contracting_requirements(**kwargs)


# ---------------------------------------------------------------- trucks (13 CCR 2025, 2195-2199, 2485)
def test_old_diesel_truck_is_noncompliant_with_clean_truck_check_dates():
    r = ee.truck_compliance("33,000 lb", engine_model_year=2008, registration_expires="2027-03-15",
                            annual_ca_miles=25000, as_of=AS_OF)
    # 2025(f)-(g): 2010+ engine since 2023-01-01; heavier table '2007 or newer' -> 2023-01-01
    assert r.truck_and_bus_status == "noncompliant" and r.original_replacement_deadline == "2023-01-01"
    assert "25,000 California miles a year vs < 1,000 -> does not qualify" in " ".join(r.math)
    # 2196.1(d): 2 tests a year for now; 2195.1: deadlines on the expiration day in months 6 and 12
    assert r.tests_per_year == 2
    assert [(d.deadline, d.earliest_test) for d in r.next_deadlines] == [
        ("2027-03-15", "2026-12-15"),          # 2027-03-15 - 90 days (15 + 28 + 31 + 16)
        ("2027-09-15", "2027-06-17")]          # 2027-09-15 - 90 days (15 + 31 + 31 + 13)
    assert r.dmv_compliance_window_start == "2026-12-15"     # 2196.1(g): 90 days before registration
    assert r.idling_limit_applies and r.clean_truck_check_applies and not r.advanced_clean_fleets_applies
    assert "$30.00" in req(r, "Clean Truck Check fee").detail and "not in the rulebook" in req(
        r, "Clean Truck Check fee").detail
    assert "$1,000.00 a day" in req(r, "Idling").detail
    assert "The DMV will not register it" in r.next_step
    assert any("Vehicle Code 4000.15" in n for n in r.notes)


def test_truck_and_bus_options_and_tables():
    # 2025(p): low-use < 1,000 CA miles; report by January 31
    lu = ee.truck_compliance(33000, 2008, annual_ca_miles=800, as_of=AS_OF)
    assert lu.truck_and_bus_status == "compliant_low_use" and req(lu, "Truck and Bus low-use").due == "2027-01-31"
    assert ee.truck_compliance(33000, 2008, annual_ca_miles=1000, as_of=AS_OF).truck_and_bus_status == "noncompliant"
    # stationary equipment: also < 100 engine/PTO hours
    assert ee.truck_compliance(33000, 2008, annual_ca_miles=500, powers_stationary_equipment=True,
                               annual_pto_hours=120, as_of=AS_OF).truck_and_bus_status == "noncompliant"
    assert ee.truck_compliance(33000, 2008, annual_ca_miles=500, powers_stationary_equipment=True,
                               annual_pto_hours=90, as_of=AS_OF).truck_and_bus_status == "compliant_low_use"
    # NOx exempt area: PM filter and only the listed counties
    nox = ee.truck_compliance(33000, 2005, operating_counties=["Humboldt", "Del Norte"], has_pm_filter=True,
                              as_of=AS_OF)
    assert nox.truck_and_bus_status == "compliant_nox_exempt_area"
    assert "'NE' on both doors in 3-inch letters" in req(nox, "Truck and Bus NOx exempt area").detail
    assert ee.truck_compliance(33000, 2005, operating_counties=["Humboldt"], has_pm_filter=False,
                               as_of=AS_OF).truck_and_bus_status == "noncompliant"
    # lighter table (26,000 lb or less, 'Compliance Date as of January 1'): '2021-01-01': '2004-2006';
    # heavier: '1994-1995' -> 2016-01-01
    assert ee.truck_compliance(20000, 2005, as_of=AS_OF).original_replacement_deadline == "2021-01-01"
    assert ee.truck_compliance(52000, 1995, as_of=AS_OF).original_replacement_deadline == "2016-01-01"
    ok = ee.truck_compliance(52000, 2016, as_of=AS_OF)
    assert ok.truck_and_bus_status == "compliant_2010_engine" and ok.original_replacement_deadline is None
    assert ee.truck_compliance(52000, as_of=AS_OF).truck_and_bus_status == "unknown_engine_year"
    # DMV check schedule by vehicle model year: heavier '2006-2007' -> 2022-01-01
    dmv = ee.truck_compliance(52000, 2016, vehicle_model_year=2006, as_of=AS_OF)
    assert "for model year 2006 (vehicle) since 2022-01-01" in req(dmv, "DMV registration check").detail


@pytest.mark.parametrize("gvwr,fuel,tb,ctc,idle", [
    (14000, "diesel", False, False, True),      # not over 14,000 (2025(b), 2195.1); over 10,000 (2485)
    (14001, "diesel", True, True, True),
    (10000, "diesel", False, False, False),
    (20000, "gasoline", False, False, False),   # Clean Truck Check is non-gasoline only
    (30000, "CNG", False, True, False),
    (30000, "electric", False, False, False),   # zero-emission excluded
    (30000, "R99", True, True, True),           # alternative diesel
])
def test_truck_applicability_boundaries(gvwr, fuel, tb, ctc, idle):
    r = ee.truck_compliance(gvwr, 2016, fuel=fuel, as_of=AS_OF)
    assert (r.truck_and_bus_applies, r.clean_truck_check_applies, r.idling_limit_applies) == (tb, ctc, idle)


def test_clean_truck_check_schedules():
    # short month: Aug 31 - 6 months = Feb 28 (2195.1 'last day of month')
    r = ee.truck_compliance(52000, 2016, registration_expires="2027-08-31", as_of=AS_OF)
    assert [d.deadline for d in r.next_deadlines] == ["2027-02-28", "2027-08-31"]
    assert r.next_step.startswith("Book the Clean Truck Check test between 2026-11-30 and 2027-02-28")
    # OBD trucks go quarterly from 2028 (CARB fact sheet); deadlines every 3 months
    q = ee.truck_compliance(52000, 2016, registration_expires="2028-08-15", obd_equipped=True, as_of="2028-02-01")
    assert q.tests_per_year == 4 and [d.deadline for d in q.next_deadlines] == ["2028-02-15", "2028-05-15"]
    assert "2024-10-01 + 3 years = 2027-10-01" in " ".join(q.math)
    # motor homes and agricultural vehicles: once a year
    mh = ee.truck_compliance(26000, 2016, vehicle_use="motorhome", registration_expires="2027-05-10", as_of=AS_OF)
    assert mh.tests_per_year == 1 and [d.deadline for d in mh.next_deadlines] == ["2027-05-10", "2028-05-10"]
    # transfers (2196.1(a)): used 2026-11-01 - 90 days = 2026-08-03; new 2026-11-01 + 30 days = 2026-12-01
    used = ee.truck_compliance(33000, 2016, acquired_on="2026-11-01", as_of=AS_OF)
    assert "since 2026-08-03" in req(used, "Used truck purchase").detail
    new = ee.truck_compliance(33000, 2026, acquired_on="2026-11-01", new_vehicle=True, as_of=AS_OF)
    assert req(new, "New truck").due == "2026-12-01"
    out = ee.truck_compliance(33000, 2016, ca_registered=False, as_of=AS_OF)
    assert "seven" not in req(out, "Five-Day Pass").detail and "at least 7 business days" in req(
        out, "Five-Day Pass").detail and out.next_deadlines == []


def test_government_fleets_and_advanced_clean_fleets():
    gov = ee.truck_compliance(9000, 2020, owner_type="county", as_of=AS_OF)
    assert gov.advanced_clean_fleets_applies is True and not gov.truck_and_bus_applies   # > 8,500 lb (2013)
    priv = ee.truck_compliance(33000, 2020, as_of=AS_OF)
    assert priv.advanced_clean_fleets_applies is False and any("only state and local government" in n
                                                               for n in priv.notes)


@pytest.mark.parametrize("kwargs,msg", [
    ({"gvwr_lbs": None}, "gvwr_lbs is required"),
    ({"gvwr_lbs": 0}, "more than 0"),
    ({"gvwr_lbs": -1}, "cannot be below 0"),
    ({"gvwr_lbs": "heavy"}, "must be a number"),
    ({"gvwr_lbs": 33000, "fuel": "coal"}, "fuel must be"),
    ({"gvwr_lbs": 33000, "engine_model_year": 1800}, "cannot be below 1900"),
    ({"gvwr_lbs": 33000, "vehicle_use": "spaceship"}, "vehicle_use must be"),
    ({"gvwr_lbs": 33000, "owner_type": "pirate"}, "owner_type must be"),
])
def test_truck_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        ee.truck_compliance(**kwargs)


# ---------------------------------------------------------------- dust: district names
@pytest.mark.parametrize("value,key,basis", [
    ("South Coast AQMD", "south_coast", "district"), ("SCAQMD", "south_coast", "district"),
    ("aqmd", "south_coast", "district"), ("Orange County", "south_coast", "county"),
    ("LA", "south_coast", "county_partial"), ("Valley Air", "san_joaquin_valley", "district"),
    ("County of Fresno", "san_joaquin_valley", "county"), ("kern co.", "san_joaquin_valley", "county_partial"),
    ("Bay Area", "bay_area", "district"), ("SF", "bay_area", "county"), ("Sonoma", "bay_area", "county_partial"),
    ("City of San Diego", None, "county"), ("San Diego APCD", None, "county"), ("Sacramento", None, "county"),
])
def test_district_names(value, key, basis):
    r = ee.dust_control_requirements(value, 2, as_of=AS_OF)
    assert (r.air_district, r.district_basis) == (key, basis)
    assert r.covered is (key is not None) and r.verified is (key is not None)


def test_uncovered_district_says_so_without_citations():
    r = ee.dust_control_requirements("SD", 3, as_of=AS_OF)
    assert r.county == "San Diego" and r.covered is False and r.citations == [] and r.verified is False
    assert "San Diego County" in r.next_step and any("read as San Diego County" in n for n in r.notes)


@pytest.mark.parametrize("value,msg", [
    ("Bakersfield", "not a California county or air district"), ("", "required"), ("SB", "ambiguous"),
    ("South Coast and Bay Area", "more than one air district"),
])
def test_bad_district_names(value, msg):
    with pytest.raises(ValueError, match=msg):
        ee.dust_control_requirements(value, 2, as_of=AS_OF)


# ---------------------------------------------------------------- dust: South Coast Rules 403 / 403.1
@pytest.mark.parametrize("acres,cy,need", [(4.99, 99, False), (5, 0, True), (1, 100, True)])
def test_south_coast_exit_controls(acres, cy, need):
    # 403(d)(5): 5 or more acres, or 100 cy/day or more imported/exported
    r = ee.dust_control_requirements("South Coast", acres, daily_import_export_cy=cy, as_of=AS_OF)
    assert r.trackout_control_required is need and has_req(r, "Track-out control at every exit") is need


@pytest.mark.parametrize("acres,cy,days,large", [
    (50, None, None, True), (49, 5000, 3, True), (49, 5000, 2, False), (49, 5000, None, None), (49, 4999, 5, False),
])
def test_south_coast_large_operation(acres, cy, days, large):
    # 403(c)(21): 50+ acres, or 5,000 cy/day (3,850 m3) three times in 365 days
    r = ee.dust_control_requirements("SCAQMD", acres, daily_earthmoving_cy=cy, earthmoving_days_at_that_volume=days,
                                     earthmoving_start="2027-01-15", as_of=AS_OF)
    assert r.large_operation is large
    if large:
        assert "within 7 days" in req(r, "Large Operation Notification").detail
        assert req(r, "Large operation: project signs").due == "2027-01-15"
        assert "30 minutes" in req(r, "Large operation: dust control supervisor").detail
        assert "12% moisture" in req(r, "Large operation: Table 2").detail
        assert r.next_step.startswith("File Form 403 N within 7 days")


def test_south_coast_wind_and_coachella_valley():
    # 403(g)(2)(A): gusts over 25 mph
    assert not has_req(ee.dust_control_requirements("SCAQMD", 2, wind_gust_mph=25, as_of=AS_OF), "High wind now")
    hot = ee.dust_control_requirements("SCAQMD", 2, wind_gust_mph=26, as_of=AS_OF)
    assert "Gusts of 26 mph are over 25 mph" in req(hot, "High wind now").detail
    # 403.1(e): more than 5,000 sq ft; 0.2 ac x 43,560 = 8,712 sq ft; submit 30 days before 2027-01-15 = 2026-12-16
    cv = ee.dust_control_requirements("Coachella Valley", 0.2, earthmoving_start="2027-01-15", as_of=AS_OF)
    assert cv.coachella_valley and cv.plan_required and cv.plan_submit_by == "2026-12-16"
    assert "0.2 ac x 43,560 = 8,712 sq ft" in " ".join(cv.math)
    assert {ee.CV_PLAN, ee.SC_VISIBLE, ee.SC_BACM} <= ids(cv)       # 403.1 is supplemental to 403
    assert ee.dust_control_requirements("coachella", "5,000 sq ft", as_of=AS_OF).plan_required is False
    assert ee.dust_control_requirements("Riverside", "5001 sf", coachella_valley=True, as_of=AS_OF).plan_required
    riv = ee.dust_control_requirements("Riverside County", 1, as_of=AS_OF)
    assert any("pass coachella_valley=true" in n for n in riv.notes)
    with pytest.raises(ValueError, match="coachella_valley applies only"):
        ee.dust_control_requirements("Fresno", 1, coachella_valley=True, as_of=AS_OF)


# ---------------------------------------------------------------- dust: San Joaquin Valley
@pytest.mark.parametrize("acres,ptype,plan,notice48", [
    (10, "residential", True, False), (9.9, "residential", False, True),          # 8021 6.3.1 / 6.4
    (5, "commercial", True, False), (4.9, "industrial", False, True),
    (0.9, "residential", False, False), (5, None, True, False),                   # None -> stricter non-residential
])
def test_san_joaquin_plan_thresholds(acres, ptype, plan, notice48):
    r = ee.dust_control_requirements("Fresno", acres, project_type=ptype, as_of=AS_OF)
    assert r.plan_required is plan and has_req(r, "48-hour notice") is notice48


def test_san_joaquin_dates_fees_and_volume():
    # 8021 6.3.2 guidance: submit 30 days ahead: 2027-02-01 - 30 = 2027-01-02; notice within 10 days before.
    r = ee.dust_control_requirements("Fresno County", 12, project_type="commercial", earthmoving_start="2027-02-01",
                                     vehicle_trips_per_day=180, urban_area=True, as_of=AS_OF)
    assert r.plan_required and r.plan_submit_by == "2027-01-02" and r.fees_usd == 493.0     # Rule 3135
    assert "between 2027-01-22 and 2027-02-01" in req(r, "Written notice before earthmoving").detail
    assert r.trackout_control_required and has_req(r, "Track-out reaching 50 ft")
    assert r.next_step.startswith("Submit the Dust Control Plan with the $493.00 fee by 2027-01-02")
    # 48-hour notice: start 2027-01-06 -> 2027-01-04
    small = ee.dust_control_requirements("Tulare", 3, project_type="residential", earthmoving_start="2027-01-06",
                                         as_of=AS_OF)
    assert req(small, "48-hour notice").due == "2027-01-04"
    # bulk material: more than 2,500 cy/day on at least 3 days
    assert ee.dust_control_requirements("Merced", 1, daily_earthmoving_cy=2501, earthmoving_days_at_that_volume=3,
                                        as_of=AS_OF).plan_required is True
    assert ee.dust_control_requirements("Merced", 1, daily_earthmoving_cy=2500, earthmoving_days_at_that_volume=9,
                                        as_of=AS_OF).plan_required is False
    assert ee.dust_control_requirements("Merced", 1, daily_earthmoving_cy=3000,
                                        as_of=AS_OF).plan_required is None
    # the $493 fee is effective 2026-07-01; before that it is not in the rulebook
    early = ee.dust_control_requirements("Fresno", 12, as_of="2026-06-30")
    assert early.fees_usd is None and "not in the rulebook" in req(early, "Dust Control Plan fee").detail


def test_san_joaquin_exemptions_and_trackout():
    # 8021 4.0: increase not more than 50% or 10,000 sq ft, whichever is less
    # 30,000 existing: min(15,000, 10,000) = 10,000; 8,000 existing: min(4,000, 10,000) = 4,000
    ex = ee.dust_control_requirements("Fresno", existing_building_sqft=30000, added_building_sqft=10000, as_of=AS_OF)
    assert ex.plan_required is False and has_req(ex, "Rule 8021 does not apply")
    assert "added 10,000 sq ft vs the lesser of 50% x 30,000 = 15,000 and 10,000 sq ft = 10,000 -> exempt" in ex.math[0]
    assert not has_req(ee.dust_control_requirements("Fresno", existing_building_sqft=30000,
                                                    added_building_sqft=10001, as_of=AS_OF), "Rule 8021 does not")
    assert has_req(ee.dust_control_requirements("Fresno", existing_building_sqft=8000, added_building_sqft=4000,
                                                as_of=AS_OF), "Rule 8021 does not apply")
    assert has_req(ee.dust_control_requirements("Fresno", 2, single_family_addition=True, as_of=AS_OF),
                   "Rule 8021 does not apply")
    # 8041 5.2-5.3: 150+ trips/day or 20+ trips by 3+-axle vehicles
    assert ee.dust_control_requirements("Kings", 1, vehicle_trips_per_day=150, as_of=AS_OF).trackout_control_required
    assert ee.dust_control_requirements("Kings", 1, vehicle_trips_per_day=149, three_axle_trips_per_day=19,
                                        as_of=AS_OF).trackout_control_required is False
    assert ee.dust_control_requirements("Kings", 1, three_axle_trips_per_day=20,
                                        as_of=AS_OF).trackout_control_required is True
    # 8041 5.4-5.5: rural projects of 10+ acres
    assert has_req(ee.dust_control_requirements("Kings", 10, urban_area=False, as_of=AS_OF), "Track-out reaching 50")
    assert not has_req(ee.dust_control_requirements("Kings", 9, urban_area=False, as_of=AS_OF),
                       "Track-out reaching 50")
    demo = ee.dust_control_requirements("Kings", 1, demolition=True, as_of=AS_OF)
    assert "within 1 hour" in req(demo, "Demolition").detail and "100 ft" in req(demo, "Demolition").detail
    with pytest.raises(ValueError, match="give both"):
        ee.dust_control_requirements("Fresno", existing_building_sqft=1000, as_of=AS_OF)


# ---------------------------------------------------------------- dust: Bay Area
def test_bay_area():
    # 6-6-101/208: more than 1 acre
    assert ee.dust_control_requirements("BAAQMD", "43,560 sq ft", as_of=AS_OF).trackout_control_required is False
    r = ee.dust_control_requirements("Alameda", 1.01, near_sensitive_receptors=True, average_wind_mph=21,
                                     as_of=AS_OF)
    assert r.trackout_control_required is True
    assert "within 4 hours" in req(r, "Rule 6-6 track-out").detail and "1 quart" in req(r, "Rule 6-6 track-out").detail
    assert "2 times every workday" in req(r, "Rule 6-6 monitoring").detail and "2 years" in req(
        r, "Rule 6-6 monitoring").detail
    assert "nonbinding" in req(r, "CEQA basic dust measures").detail
    assert "Average wind 21 mph > 20 mph" in req(r, "Wind now over the limit").detail
    assert "50% porosity" in req(r, "CEQA enhanced").detail
    assert not has_req(ee.dust_control_requirements("Marin", 2, average_wind_mph=20, as_of=AS_OF), "Wind now")


@pytest.mark.parametrize("kwargs,msg", [
    ({"disturbed_acres": -1}, "cannot be below 0"), ({"disturbed_acres": "big"}, "must be a number"),
    ({"project_type": "spaceport"}, "project_type must be"), ({"vehicle_trips_per_day": 1.5}, "whole number"),
    ({"earthmoving_start": "next week"}, "ISO date"),
])
def test_dust_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        ee.dust_control_requirements("Fresno", **({"disturbed_acres": 2} | kwargs))


# ---------------------------------------------------------------- portable engines (PERP, Portable Engine ATCM)
@pytest.mark.parametrize("bhp,tier,ok", [
    (300, "Tier 4 Final", True), (300, "Tier 3", False), (300, "tier 3 flex", True), (750, "T3 flex", True),
    (800, "Tier 3 flex", False), (800, "Tier 2 flex", True), (800, "T4i flex", True), (300, "Tier 4 interim", False),
])
def test_perp_eligibility(bhp, tier, ok):
    # 13 CCR 2451 / FAQ: 50-750 bhp: T4F, T4i flex, T3 flex; over 750 bhp: T4F, T4i flex, T2 flex
    assert ee.portable_engine_requirements(bhp, tier, as_of=AS_OF).perp_eligible_new_registration is ok


def test_perp_thresholds_dates_and_fees():
    assert ee.portable_engine_requirements(49, as_of=AS_OF).subject_to_atcm is False        # under 50 bhp
    assert ee.portable_engine_requirements(50, as_of=AS_OF).subject_to_atcm is True
    assert ee.portable_engine_requirements(300, months_at_location=12, as_of=AS_OF).portable is True
    away = ee.portable_engine_requirements(300, months_at_location=13, as_of=AS_OF)
    assert away.portable is False and "stationary-source permit" in away.summary
    assert ee.portable_engine_requirements(300, on_foundation=True, as_of=AS_OF).portable is False
    assert ee.portable_engine_requirements(300, fleet_total_bhp=750, as_of=AS_OF).atcm_fleet_size == "small"
    assert ee.portable_engine_requirements(300, fleet_total_bhp=751, as_of=AS_OF).atcm_fleet_size == "large"
    assert ee.portable_engine_requirements(300, annual_hours=200, as_of=AS_OF).low_use is True       # "not more than"
    assert ee.portable_engine_requirements(300, annual_hours=201, as_of=AS_OF).low_use is False
    # 2462(b): 2027-06-30 - 2 months = 2027-04-30; invoice 2027-06-30 - 120 days = 2027-03-02 (30+31+30+29);
    # 2458(k): reading within 12 months of expiration -> on or after 2026-06-30
    r = ee.portable_engine_requirements(300, "T4F", registration_expires="2027-06-30", engines_to_register=2,
                                        equipment_units_to_register=1, as_of=AS_OF)
    assert r.renewal_return_by == "2027-04-30"
    assert "about 2027-03-02" in req(r, "PERP renewal").detail and "on or after 2026-06-30" in req(
        r, "PERP renewal").detail
    assert r.fees_usd == 2100.0                     # 2461.1: 2 x $805 + 1 x $490 = $2,100
    renew = ee.portable_engine_requirements(300, engines_to_register=2, equipment_units_to_register=1, renewal=True,
                                            as_of=AS_OF)
    assert renew.fees_usd == 1890.0                 # 2 x $735 + 1 x $420 = $1,890
    # 2455(a): more than 2,500 bhp in an extreme ozone district: 14 days before startup (2027-01-20 -> 2027-01-06)
    p = ee.portable_engine_requirements(300, project_registered_bhp_on_site=2501, extreme_ozone_district=True,
                                        project_start="2027-01-20", as_of=AS_OF)
    assert p.project_notice_required is True and p.project_notice_due == "2027-01-06"
    assert p.next_step == "Send the district the written large-project notice by 2027-01-06."
    assert ee.portable_engine_requirements(300, project_registered_bhp_on_site=2500,
                                           as_of=AS_OF).project_notice_required is False
    assert ee.portable_engine_requirements(300, project_registered_bhp_on_site=3000,
                                           as_of=AS_OF).project_notice_required is None
    assert ee.portable_engine_requirements(300, project_registered_bhp_on_site=3000, extreme_ozone_district=False,
                                           as_of=AS_OF).project_notice_required is False


def test_perp_not_subject_cases():
    drive = ee.portable_engine_requirements(300, propels_vehicle=True, as_of=AS_OF)
    assert drive.subject_to_atcm is False and ee.ORD_APPLIES in ids(drive)
    assert any("offroad_fleet_obligations" in n for n in drive.notes)
    assert ee.portable_engine_requirements(300, fuel="gasoline", as_of=AS_OF).subject_to_atcm is False


@pytest.mark.parametrize("kwargs,msg", [
    ({"engine_bhp": None}, "engine_bhp is required"), ({"engine_bhp": -3}, "cannot be below 0"),
    ({"engine_bhp": 300, "engine_tier": "Tier 4"}, "ambiguous"), ({"engine_bhp": 300, "engine_tier": "T9"}, "must be"),
    ({"engine_bhp": 300, "annual_hours": 9000}, "cannot be above"),
    ({"engine_bhp": 300, "engines_to_register": 1.5}, "whole number"),
])
def test_perp_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        ee.portable_engine_requirements(**kwargs)


# ---------------------------------------------------------------- MCP
NAMES = {"offroad_fleet_obligations", "offroad_contracting_requirements", "truck_compliance",
         "dust_control_requirements", "portable_engine_requirements"}


def test_register_and_call_every_tool_through_an_mcp_client():
    mcp = FastMCP("t")
    ee.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            out = {
                "fleet": await c.call_tool("offroad_fleet_obligations", {
                    "total_fleet_hp": "3,200 hp", "fleet_owner_type": "LLC", "as_of": AS_OF,
                    "vehicles": [{"name": "CAT 320", "hp": 158, "engine_tier": "Tier-2"}]}),
                "contract": await c.call_tool("offroad_contracting_requirements", {
                    "role": "GC", "first_vehicle_operates_on": "2027-01-04", "expected_operating_days": 30,
                    "as_of": AS_OF}),
                "truck": await c.call_tool("truck_compliance", {
                    "gvwr_lbs": "33,000 lb", "engine_model_year": 2008, "fuel": "Diesel",
                    "registration_expires": "2027-03-15", "as_of": AS_OF}),
                "dust": await c.call_tool("dust_control_requirements", {
                    "air_district": "County of Fresno", "disturbed_acres": "12 acres", "project_type": "commercial",
                    "earthmoving_start": "2027-02-01", "as_of": AS_OF}),
                "perp": await c.call_tool("portable_engine_requirements", {
                    "engine_bhp": "300 bhp", "engine_tier": "T4F", "registration_expires": "2027-06-30",
                    "as_of": AS_OF}),
            }
            return tools, {k: v.structured_content for k, v in out.items()}
    tools, out = asyncio.run(run())
    assert set(tools) == NAMES
    for t in tools.values():
        a = t.annotations.model_dump()
        assert a.get("readOnlyHint", a.get("read_only_hint")) is True
        assert a.get("openWorldHint", a.get("open_world_hint")) is False
    assert "small <= 2,500 hp" in tools["offroad_fleet_obligations"].description     # numbers from the rulebook
    assert "$493.00 fee" in tools["dust_control_requirements"].description
    assert "14,000 lb GVWR" in tools["truck_compliance"].description
    f = out["fleet"]
    assert f["fleet_size"] == "medium" and f["vehicles"][0]["may_not_operate_from"] == "2030-01-01"
    assert f["verified"] is True and f["citations"] and f["disclaimer"].startswith("Compliance information")
    assert out["contract"]["sign_post_by"] == "2027-01-11" and out["contract"]["role"] == "prime_contractor"
    assert out["truck"]["truck_and_bus_status"] == "noncompliant"
    assert out["truck"]["next_deadlines"][0] == {"deadline": "2027-03-15", "earliest_test": "2026-12-15"}
    d = out["dust"]
    assert d["air_district"] == "san_joaquin_valley" and d["plan_required"] is True
    assert d["plan_submit_by"] == "2027-01-02" and d["fees_usd"] == 493.0
    assert out["perp"]["renewal_return_by"] == "2027-04-30" and out["perp"]["perp_eligible_new_registration"] is True


def test_mcp_rejects_bad_values_with_clear_errors():
    mcp = FastMCP("t")
    ee.register(mcp)

    async def run():
        async with Client(mcp) as c:
            errors = []
            for name, args in [
                ("offroad_fleet_obligations", {"total_fleet_hp": -5}),
                ("offroad_fleet_obligations", {"vehicles": [{"hp": 100, "engine_tier": "Tier 4"}]}),
                ("offroad_contracting_requirements", {"role": "owner's rep"}),
                ("truck_compliance", {"gvwr_lbs": "heavy"}),
                ("dust_control_requirements", {"air_district": "Bakersfield"}),
                ("portable_engine_requirements", {"engine_bhp": 300, "engine_tier": "Tier 9"}),
            ]:
                try:
                    await c.call_tool(name, args)
                    errors.append(None)
                except Exception as e:                         # fastmcp raises ToolError
                    errors.append(str(e))
            return errors
    errors = asyncio.run(run())
    assert all(errors), errors
    assert "cannot be below 0" in errors[0] and "ambiguous" in errors[1] and "role must be" in errors[2]
    assert "must be a number" in errors[3] and "not a California county" in errors[4]


# ================================================================ adversarial review 2026-11-25 (regressions)
# Each expectation below was worked by hand from the official text re-read on 2026-11-25 (froa-1.pdf 2449-2449.1,
# tbfinalreg.pdf 2025, the HD I/M order 2195-2196.1, 13 CCR 2485, SCAQMD Rules 403/403.1, SJVAPCD Rules
# 8011/8021/8041, the PERP FAQ of Sept. 29, 2026 and CARB's July 2025 enforcement notice).
def test_review_corrected_rules_carry_the_added_text():
    r = core.rule(ee.TB_APPLIES)
    assert r.status == "corrected" and r.verified
    assert "dedicated snow-removal vehicles" in r.param("excluded")
    assert any(e.startswith("two-engine on-road vehicles subject to 13 CCR 2449") for e in r.param("excluded"))
    assert "2022.1(a)(1)" in r.quote and "(13) Except for two-engine sweepers" in r.quote
    assert core.rule(ee.IDLE_FINE).citation_text.startswith("CARB truck idling fact sheet")
    assert "2480" not in core.rule(ee.IDLE_FINE).citation_text           # school bus idling is not the source
    assert core.rule(ee.CV_PLAN).param("daily_records_retention_years") == 3
    assert core.rule(ee.TB_2010).param("schedule_lighter_26000_or_less")["2021-01-01"] == "2004-2006"


def test_state_and_federal_fleets_are_never_captive_attainment_area_fleets():
    # 2449(c)(6): fleets of the United States or the State of California are large fleets and never captive, even
    # when every vehicle stays in the listed counties; 2449.1(f)(2)(A) still excuses R99 while operating there.
    st = ee.offroad_fleet_obligations(800, "state agency", operating_counties=["Humboldt"], as_of=AS_OF)
    assert st.fleet_size == "large" and st.captive_attainment_area is False
    assert "white on red" in req(st, "EIN label").detail
    assert "captive" not in req(st, "ROAR").detail
    assert st.renewable_diesel_required is False and "Not required while the machines operate" in exact(
        st, "Renewable diesel").detail
    biz = ee.offroad_fleet_obligations(800, "business", operating_counties=["Humboldt"], as_of=AS_OF)
    assert biz.fleet_size == "small" and biz.captive_attainment_area is True
    assert "white on green" in req(biz, "EIN label").detail
    assert ee.offroad_fleet_obligations(800, "federal", operating_counties="Humboldt",
                                        as_of=AS_OF).captive_attainment_area is False


def test_r99_exemptions_name_the_county_vehicle_exemption():
    r = ee.offroad_fleet_obligations(900, operating_counties=["Kern"], as_of=AS_OF)
    d = req(r, "Renewable diesel (R99/R100)").detail
    assert r.renewable_diesel_required is True
    assert "any machine while it operates in one of the 2449(c)(6) counties" in d and "Humboldt" in d


def test_medium_fleet_label_and_sanity_limits():
    # 2449(c)(28)(B): medium = neither small (<= 2,500) nor large (> 5,000); 2,500.5 hp is medium
    r = ee.offroad_fleet_obligations(2500.5, as_of=AS_OF)
    assert r.fleet_size == "medium" and "2,500 hp < 2,500.5 hp <= 5,000 hp" in r.summary
    with pytest.raises(ValueError, match="cannot be above 10,000,000"):
        ee.offroad_fleet_obligations(1e300, as_of=AS_OF)
    with pytest.raises(ValueError, match="less than or equal to 20000"):
        ee.offroad_fleet_obligations(vehicles=[{"hp": 25000, "engine_tier": "T4f"}], as_of=AS_OF)


def test_us_style_dates_are_read_month_first():
    a = ee.offroad_fleet_obligations(900, as_of="11/25/2026")
    b = ee.offroad_fleet_obligations(900, as_of=AS_OF)
    assert a.next_annual_report_due == b.next_annual_report_due == "2027-03-01"


def test_new_fleet_tier_phase_out_applies_at_once_and_low_use_reporting_continues():
    # 2449(d)(4)(A)-(C): idling, adding-vehicle limits and Tier phase-out apply immediately to a new fleet
    r = ee.offroad_fleet_obligations(900, new_fleet=True, vehicle_added_on="2026-11-01", as_of=AS_OF)
    assert "tier phase-out" in req(r, "Report the new fleet").detail and req(r, "Report the new fleet").due == "2026-12-01"
    # 2449(g)(2): fleet reporting ends 2036, but low-use vehicles are reported for as long as the fleet has them
    late = ee.offroad_fleet_obligations(vehicles=[{"hp": 300, "engine_tier": "Tier 3"},
                                                  {"name": "Old dozer", "hp": 200, "engine_tier": "Tier 1",
                                                   "designation": "low use"}], as_of="2037-03-02")
    assert late.next_annual_report_due is None
    assert any("permanent low-use vehicles keep being reported" in n for n in late.notes)
    assert req(late, "Low-use hour meter readings").due == "2038-03-01"
    assert "three consecutive years" in veh(late, "Old dozer").detail      # 2449(c)(43)(F) rolling average


def test_optional_schedule_counts_on_road_engines_by_the_tier_pairing():
    # (e)(16): all vehicles count. On-road MY2008 > MY1999 (Tier 1 pairing) counts as Tier 2 or better:
    # (150 Tier 3 + 100 on-road MY2008) / 400 = 62.5% < 75% since 2026-01-01; 75% x 400 = 300 -> short 50 hp
    r = ee.offroad_fleet_obligations(vehicles=[{"hp": 150, "engine_tier": "Tier 1"}, {"hp": 150, "engine_tier": "Tier 3"},
                                               {"hp": 100, "engine_tier": "on-road 2008"}],
                                     uses_optional_500hp_schedule=True, as_of=AS_OF)
    c = r.optional_schedule_check
    assert (c.counted_hp, c.tier2_or_better_hp, c.percent_tier2_or_better) == (400, 250, 62.5)
    assert c.meets_current_requirement is False and "at least 50 hp" in r.next_step
    assert "optional 500 hp schedule" in r.summary
    # an old on-road engine (MY1995 <= 1999) is not Tier 2 or better: 150 / 450 = 33.3%, no longer silently passing
    old = ee.offroad_fleet_obligations(vehicles=[{"hp": 150, "engine_tier": "Tier 2"},
                                                 {"hp": 300, "engine_tier": "on-road 1995"}],
                                       uses_optional_500hp_schedule=True, as_of=AS_OF)
    assert old.optional_schedule_check.percent_tier2_or_better == 33.3
    assert old.optional_schedule_check.meets_current_requirement is False


def test_contracting_private_owner_report_route_and_sign_wording():
    o = ee.offroad_contracting_requirements("developer", hires_haul_trucks=True, as_of=AS_OF)
    assert o.role == "private_owner" and o.duties_apply is False and "no 13 CCR 2449(i)-(j)" in o.summary
    assert has_req(o, "Hired diesel trucks")
    p = ee.offroad_contracting_requirements("GC", noncompliance_discovered_on="2026-11-27", as_of=AS_OF)
    d = req(p, "Report uncertified").detail
    assert "dieselcomplaints@arb.ca.gov" in d and "diesel.complaints@arb.ca.gov" in d and "web form" in d
    assert ee.ORD_ENFORCE in req(p, "Report uncertified").rule_ids
    s = ee.offroad_contracting_requirements("GC", as_of=AS_OF)
    assert req(s, "CARB off-road jobsite sign").detail.startswith("If machines will operate 8 calendar days or more")


@pytest.mark.parametrize("use,phrase", [
    ("snow plow", "dedicated snow-removal vehicles"),
    ("two engine crane", "offroad_fleet_obligations"),
    ("motor home", "run it as vehicle_use='truck'"),
])
def test_truck_and_bus_exclusions_of_2025c(use, phrase):
    r = ee.truck_compliance(40000, 2005, vehicle_use=use, as_of=AS_OF)
    assert r.truck_and_bus_applies is False and r.truck_and_bus_status == "not_subject"
    assert r.clean_truck_check_applies is True                      # 2195(b) has no such exclusions
    assert any(phrase in n for n in r.notes)


def test_pickup_is_outside_the_truck_idling_rule_and_crane_must_be_explicit():
    # 2485(h)(6): a pickup truck (Vehicle Code 471) is not a 'commercial motor vehicle'
    p = ee.truck_compliance(11500, 2018, vehicle_use="pickup truck", as_of=AS_OF)
    assert p.idling_limit_applies is False and any("2485(h)(6)" in n for n in p.notes)
    assert ee.truck_compliance(11500, 2018, vehicle_use="truck", as_of=AS_OF).idling_limit_applies is True
    with pytest.raises(ValueError, match="two_engine_crane"):
        ee.truck_compliance(40000, 2016, vehicle_use="crane", as_of=AS_OF)


def test_truck_paths_government_note_and_nox_area_travel():
    r = ee.truck_compliance(40000, 2007, owner_type="city", as_of=AS_OF)
    assert any("2022.1(a)(1)" in n for n in r.notes)
    assert "VDECS retrofit that achieves 2010 model year equivalent emissions" in req(r, "Truck and Bus: NOT").detail
    assert "VDECS" in r.next_step
    nox = ee.truck_compliance(60000, 2004, operating_counties="Humboldt, Del Norte", has_pm_filter=True, as_of=AS_OF)
    assert "only for repairs or other services" in req(nox, "Truck and Bus NOx").detail
    assert ee.TB_NOX in req(nox, "Truck and Bus NOx").rule_ids


def test_obd_status_is_read_from_the_engine_model_year():
    # 2195.1: OBD-equipped = diesel MY2013+, alternative fuel MY2018+
    assert any("MY2015 >= MY2013" in m and "-> yes" in m for m in ee.truck_compliance(60000, 2015, as_of=AS_OF).math)
    cng = ee.truck_compliance(60000, 2015, fuel="CNG", as_of=AS_OF)
    assert any("MY2015 < MY2018" in m for m in cng.math)
    assert "not OBD-equipped, so it stays at 2" in req(cng, "Clean Truck Check testing").detail
    assert any("-> no" in m for m in ee.truck_compliance(60000, 2015, obd_equipped=False, as_of=AS_OF).math) is False


def test_clean_truck_check_deadlines_use_month_and_day_only():
    # an old registration date keeps its month and day: deadlines Mar 15 and Sep 15 (months 12 and 6)
    old = ee.truck_compliance(60000, 2015, registration_expires="2005-03-15", as_of=AS_OF)
    assert [d.deadline for d in old.next_deadlines] == ["2027-03-15", "2027-09-15"]
    assert any("has already passed" in n for n in old.notes)
    assert exact(old, "Registration renewal").due == "2027-03-15"
    # Aug 31 -> month 6 is February: last day of the month (2027-02-28)
    aug = ee.truck_compliance(60000, 2015, registration_expires="2027-08-31", as_of=AS_OF)
    assert [d.deadline for d in aug.next_deadlines] == ["2027-02-28", "2027-08-31"]
    assert aug.next_deadlines[0].earliest_test == "2026-11-30"          # 2027-02-28 - 90 days
    with pytest.raises(ValueError, match="more than a year away"):
        ee.truck_compliance(60000, 2015, registration_expires="2090-03-15", as_of=AS_OF)


def test_vin_sets_deadlines_for_trucks_registered_outside_california():
    # 2195.1 'Compliance year' (1): VIN ending 9 -> compliance year ends on the last day of July; semiannual deadlines
    # on the last day of months 6 and 12 -> January 31 and July 31 (the fact sheet's own example)
    r = ee.truck_compliance(80000, 2019, ca_registered=False, vin="1FUJGLDR9KLKY1239", as_of=AS_OF)
    assert [(d.deadline, d.earliest_test) for d in r.next_deadlines] == [("2027-01-31", "2026-11-02"),
                                                                         ("2027-07-31", "2027-05-02")]
    assert "VIN" not in r.next_step and "2027-01-31" in r.next_step
    no_vin = ee.truck_compliance(80000, 2019, ca_registered=False, as_of=AS_OF)
    assert not no_vin.next_deadlines and "Give the VIN" in no_vin.next_step
    # an OBD truck whose next deadlines pass the regulation's 3-year mark (2024-10-01 + 3 years) gets a caveat
    q = ee.truck_compliance(80000, 2019, ca_registered=False, vin="3", as_of="2027-11-01")
    assert any("2027-10-01" in n and "quarterly" in n for n in q.notes)
    with pytest.raises(ValueError, match="vin must be"):
        ee.truck_compliance(80000, 2019, ca_registered=False, vin="ABCDEFG", as_of=AS_OF)


def test_idling_fine_is_stated_as_carbs_figure():
    d = req(ee.truck_compliance(40000, 2016, as_of=AS_OF), "Idling").detail
    assert "CARB says" in d and "$1,000.00 a day" in d and "Health and Safety Code" in d


def test_coachella_plan_city_county_route_records_and_riverside_scope():
    r = ee.dust_control_requirements("Riverside", coachella_valley=True, disturbed_acres="8,000 sq ft",
                                     earthmoving_start="2027-01-15", as_of=AS_OF)
    plan = req(r, "Coachella Valley fugitive dust control plan")
    assert r.plan_submit_by == "2026-12-16" and "Rule 403.1(i)(4)" in plan.detail
    assert "Coachella Valley Fugitive Dust Control Class" in plan.detail
    assert exact(r, "Coachella Valley daily records").detail.endswith("at least 3 years.")
    assert "city or county" in r.next_step
    assert not any(n.startswith("Only the non-desert part") for n in r.notes)
    assert any(n.startswith("Riverside County, Coachella Valley") for n in r.notes)
    plain = ee.dust_control_requirements("Riverside", disturbed_acres=2, as_of=AS_OF)
    assert any("coachella_valley=true" in n for n in plain.notes)


def test_large_operation_exemptions_of_rule_403g7_g8():
    r = ee.dust_control_requirements("SCAQMD", disturbed_acres=60, as_of=AS_OF)
    assert r.large_operation is True
    assert any("Rule 403(g)(7)" in n and "Rule 403(g)(8)" in n for n in r.notes)


def test_san_joaquin_notice_uses_site_area_and_other_review_fixes():
    # 8021 §6.4.1: residential site 1 to < 10 acres in area -> 48-hour notice, even with 0.8 acre disturbed
    r = ee.dust_control_requirements("Fresno", disturbed_acres=0.8, site_acres=3, project_type="residential",
                                     earthmoving_start="2027-01-11", as_of=AS_OF)
    assert r.plan_required is False and exact(r, "48-hour notice to the District").due == "2027-01-09"
    unknown = ee.dust_control_requirements("Fresno", disturbed_acres=0.8, project_type="residential", as_of=AS_OF)
    assert not has_req(unknown, "48-hour") and any("give site_acres" in n for n in unknown.notes)
    # 8041 §5.6: paved interior roads -> track-out device; §5.5: rural project size 12 acres -> 50 ft rule
    t = ee.dust_control_requirements("Tulare", disturbed_acres=4, site_acres=12, urban_area=False,
                                     paved_interior_roads=True, as_of=AS_OF)
    assert t.trackout_control_required is True
    assert req(t, "Track-out reaching 50 ft").detail.startswith("Prevent track-out")
    # 8011 §4.3: 3,000 ft or higher is outside Regulation VIII
    hi = ee.dust_control_requirements("Kern", disturbed_acres=20, site_elevation_ft="3,000 ft", as_of=AS_OF)
    assert hi.plan_required is False and has_req(hi, "Regulation VIII does not apply")
    lo = ee.dust_control_requirements("Kern", disturbed_acres=20, site_elevation_ft=2999, as_of=AS_OF)
    assert lo.plan_required is True
    with pytest.raises(ValueError, match="cannot be smaller than disturbed_acres"):
        ee.dust_control_requirements("Fresno", disturbed_acres=5, site_acres=2, as_of=AS_OF)


def test_perp_seasonal_source_categories_and_fleet_average_opt_in():
    # FAQ Q4: at one location 2+ years and run 3+ months a year -> seasonal source, not portable
    s = ee.portable_engine_requirements(300, years_at_same_location=2, months_operated_per_year=3, as_of=AS_OF)
    assert s.portable is False and "seasonal source" in s.summary and "stationary source" in s.next_step
    assert ee.portable_engine_requirements(300, years_at_same_location=2, months_operated_per_year=2.5,
                                           as_of=AS_OF).portable is None
    # FAQ Q14: an engine owned by an agricultural source is outside the ATCM
    ag = ee.portable_engine_requirements(300, special_category="agricultural", as_of=AS_OF)
    assert ag.subject_to_atcm is False and "agricultural source" in ag.summary
    with pytest.raises(ValueError, match="ambiguous"):
        ee.portable_engine_requirements(300, special_category="emergency", as_of=AS_OF)
    big = ee.portable_engine_requirements(300, fleet_total_bhp=2000, as_of=AS_OF)
    assert "only if you opted into it by 2019-06-30" in req(big, "Portable engine fleet").detail


def test_mcp_accepts_the_new_inputs():
    mcp = FastMCP("t")
    ee.register(mcp)

    async def run():
        async with Client(mcp) as c:
            return [(await c.call_tool(n, a)).structured_content for n, a in [
                ("truck_compliance", {"gvwr_lbs": "80,000 lb", "engine_model_year": 2019, "ca_registered": False,
                                      "vin": "9", "as_of": AS_OF}),
                ("dust_control_requirements", {"air_district": "Valley Air", "disturbed_acres": "0.8 ac",
                                               "site_acres": "3 acres", "project_type": "homes",
                                               "earthmoving_start": "01/11/2027", "daily_import_export_cy": "10 cy",
                                               "as_of": AS_OF}),
                ("portable_engine_requirements", {"engine_bhp": 300, "special_category": "harbor craft",
                                                  "as_of": AS_OF}),
                ("offroad_contracting_requirements", {"role": "owner", "as_of": AS_OF}),
                ("offroad_fleet_obligations", {"total_fleet_hp": 900, "operating_counties": "Humboldt, Lake",
                                               "as_of": AS_OF}),
            ]]
    t, d, p, o, f = asyncio.run(run())
    assert t["next_deadlines"][0]["deadline"] == "2027-01-31"
    assert any(r["item"] == "48-hour notice to the District" and r["due"] == "2027-01-09" for r in d["requirements"])
    assert p["subject_to_atcm"] is False and o["role"] == "private_owner" and f["captive_attainment_area"] is True


def test_non_list_inputs_raise_clear_errors_instead_of_crashing():
    # found by fuzzing: operating_counties=2010 or vehicles=-1 used to raise TypeError
    with pytest.raises(ValueError, match="operating_counties must be a list"):
        ee.offroad_fleet_obligations(900, operating_counties=2010, as_of=AS_OF)
    with pytest.raises(ValueError, match="vehicles must be a list"):
        ee.offroad_fleet_obligations(vehicles=-1, as_of=AS_OF)
    with pytest.raises(ValueError, match="operating_counties must be a list"):
        ee.truck_compliance(40000, 2004, operating_counties=True, as_of=AS_OF)
    one = ee.offroad_fleet_obligations(vehicles={"hp": 158, "engine_tier": "Tier 2"}, as_of=AS_OF)
    assert one.total_fleet_hp == 158 and len(one.vehicles) == 1
