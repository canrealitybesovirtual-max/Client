"""Accessibility module: CBC 11B path of travel, accessible parking, field checks, which rules apply, CASp leases.

Expected values are worked by hand from the rule text in research/web_accessibility.verified.json (the arithmetic is
in the comments), never read back from the module.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import accessibility as acc, core

AS_OF = "2026-11-25"
SD_TI = dict(construction_cost_usd=150_000, permit_application_date="2026-12-01", jurisdiction="SD",
             element_costs_usd={"entrance": 12_000, "route": 9_000, "restroom": 28_000, "telephones": 0,
                                "drinking fountains": 3_500, "signs": 800})


def ids(result):
    return {c.rule_id for c in result.citations}


def check(result, item):
    return next(c for c in result.checks if c.item == item)


def regime(result, prefix):
    return next(r for r in result.regimes if r.code.startswith(prefix))


# ---------------------------------------------------------------- rulebook wiring
def test_rule_ids_exist_are_unique_and_come_from_the_accessibility_pack():
    consts = {v for k, v in vars(acc).items() if k.isupper() and isinstance(v, str) and v.startswith("ada-")}
    assert consts == set(acc.RULE_IDS) and len(acc.RULE_IDS) == len(set(acc.RULE_IDS)) > 80
    book = {r.id: r for r in core.all_rules()}
    assert all(book[i].pack == "web_accessibility" for i in acc.RULE_IDS)
    # the pack's only unverifiable rule is the "no less than 20%" hardship spend above the threshold
    assert [i for i in acc.RULE_IDS if not book[i].verified] == [acc.ABOVE]
    assert book[acc.ABOVE].status == "unverifiable"
    assert book[acc.A_VAN8].status == "corrected" and book[acc.FHA_HUD].status == "corrected"


def test_threshold_and_cap_come_from_the_rulebook_not_the_code(monkeypatch):
    # A 2027 submission is "threshold unknown" today; if the rulebook gains a 2027 figure the tool uses it.
    assert acc.path_of_travel_obligation(150_000, "2027-02-01").california.status == "threshold_unknown_for_year"
    real = core.rule

    def patched(rid):
        r = real(rid)
        if rid == acc.THRESHOLD:
            p = dict(r.parameters, calendar_year=2027, valuation_threshold_usd=215_000.0)
            return r.model_copy(update={"parameters": p})
        return r
    monkeypatch.setattr(core, "rule", patched)
    ca = acc.path_of_travel_obligation(150_000, "2027-02-01").california
    assert (ca.status, ca.valuation_threshold_usd, ca.threshold_year) == ("capped_20_percent", 215_000, 2027)


# ---------------------------------------------------------------- path of travel (CBC 11B-202.4 Exception 8)
def test_san_diego_150k_tenant_improvement_matches_the_rules_worked_example():
    # Rule ada-cbc-11b-202-4-exc8-20-percent-cap worked example: $150,000 adjusted cost -> up to $30,000.
    # Adjusted = C 150,000 - 0 path-of-travel work + B 0 = 150,000 <= 2026 threshold 209,208 -> Exception 8.
    # Full cost = 12,000 + 9,000 + 28,000 + 0 + 3,500 + 800 = 53,300; required = min(53,300, 30,000) = 30,000.
    r = acc.path_of_travel_obligation(**SD_TI)
    ca, fed = r.california, r.federal
    assert (ca.status, ca.adjusted_construction_cost_usd, ca.valuation_threshold_usd) == ("capped_20_percent",
                                                                                          150_000, 209_208)
    assert (ca.max_required_spend_usd, ca.required_spend_usd, r.full_path_of_travel_cost_usd) == (30_000, 30_000,
                                                                                                  53_300)
    # Federal 28 CFR 36.403(f)(1): 20% x (150,000 primary function alteration + 0 prior) = 30,000
    assert (fed.status, fed.disproportionality_limit_usd, fed.required_spend_usd) == ("capped_20_percent", 30_000,
                                                                                      30_000)
    assert r.combined_budget_usd == 30_000 and r.combined_required_spend_usd == 30_000
    # Priority order: entrance 12,000 (18,000 left), route 9,000 (9,000 left), restroom 9,000 of 28,000 (0 left),
    # telephones already comply, fountains and other items beyond the cap.
    plan = [(p.priority, p.status, p.funded_usd) for p in r.spending_plan]
    assert plan == [(1, "fund_fully", 12_000), (2, "fund_fully", 9_000), (3, "fund_partly", 9_000),
                    (4, "already_complies", 0), (5, "beyond_cap", 0), (6, "beyond_cap", 0)]
    assert r.priority_order[0] == "accessible entrance" and "restroom for each sex" in r.priority_order[2]
    assert [f.item for f in ca.forms] == ["City of San Diego Form DS-2123"]
    assert "DS-2123 (January 2026)" in ca.forms[0].detail and "Owner, Architect, or Design Professional" in \
        ca.forms[0].detail
    assert r.jurisdiction.startswith("City of San Diego")
    assert {acc.THRESHOLD, acc.CAP20, acc.ADJ_EXCL, acc.LOOKBACK, acc.PRIORITY, acc.SD_FORM, acc.F_20,
            acc.F_PRIORITY, acc.PLAN_CHECK} <= ids(r)
    assert acc.ABOVE not in ids(r) and r.verified is True and r.unverified_rule_ids == []
    assert "Adjusted construction cost = C $150,000 this project - $0 path-of-travel work inside it + B $0 prior " \
           "3-year alterations = $150,000" in r.math
    assert r.next_step.startswith("Fill in City of San Diego Form DS-2123")
    assert "$203,611" in " ".join(r.notes)              # the stale LADBS counter-sheet figure is flagged


def test_adjusted_cost_with_prior_work_window_and_title_ii():
    # 3-year look-back from 2026-12-01 starts 2023-12-01 (ada-cbc-11b-202-4-exc8-three-year-lookback):
    #   2024-02-10 $40,000 counted; 2023-11-30 $30,000 is one day too early; a job that provided an accessible path
    #   of travel and a job on a different path are not added.
    # Adjusted = 120,000 - 10,000 path-of-travel work inside it + 40,000 = 150,000 -> cap 20% = 30,000.
    # Title II (28 CFR 35.151(b)(4)(iii)): 20% x (110,000 + 40,000) = 30,000.
    # DS-2123 compliance % = A 31,000 / 150,000 x 100 = 20.666... -> 20.67; 31,000 >= 30,000.
    r = acc.path_of_travel_obligation(
        120_000, "2026-12-01", facility="City Hall", jurisdiction="Pasadena", path_of_travel_costs_included_usd="10k",
        planned_accessibility_spend_usd="$31,000",
        prior_alterations=[{"date": "2024-02-10", "cost_usd": "40,000"},
                           {"date": "2023-11-30", "cost_usd": 30_000},
                           {"date": "2025-05-01", "cost_usd": 25_000, "provided_accessible_path_of_travel": True},
                           {"date": "2025-08-01", "cost_usd": 15_000, "same_path_of_travel": False}])
    assert [(p.counted_california, p.counted_federal) for p in r.prior_alterations] == [
        (True, True), (False, False), (False, False), (False, False)]
    assert "outside the 3-year look-back" in r.prior_alterations[1].reason
    assert r.california.adjusted_construction_cost_usd == 150_000 and r.california.max_required_spend_usd == 30_000
    f = r.federal
    assert f.law.startswith("ADA Title II") and f.primary_function_alteration_cost_usd == 110_000
    assert (f.counted_prior_cost_usd, f.disproportionality_limit_usd) == (40_000, 30_000)
    assert r.compliance_percent == 20.67 and r.planned_spend_meets_requirement is True
    assert {acc.T2_20, acc.T2_SERIES, acc.T2_2010_DATE} <= ids(r) and acc.F_20 not in ids(r)
    assert r.california.forms == [] and r.verified is True       # no LADBS/San Diego form for Pasadena
    assert "highway rest stops" in " ".join(r.notes)


def test_threshold_boundary_is_at_or_below():
    # "less than or equal to the current valuation threshold" -> $209,208 itself is capped: 20% x 209,208 = 41,841.60
    at = acc.path_of_travel_obligation(209_208, "2026-03-01")
    assert at.california.status == "capped_20_percent" and at.california.max_required_spend_usd == 41_841.60
    over = acc.path_of_travel_obligation(209_208.01, "2026-03-01")
    assert over.california.status == "full_compliance" and over.california.max_required_spend_usd is None


def test_above_threshold_full_compliance_and_the_unverified_hardship_minimum():
    # $240,000 > $209,208 -> no cap: every path-of-travel element must comply (required = full 95,000).
    r = acc.path_of_travel_obligation("$240,000", "2026-06-01", jurisdiction="City of Los Angeles",
                                      full_path_of_travel_cost_usd="95k")
    assert r.california.status == "full_compliance" and r.california.required_spend_usd == 95_000
    assert r.combined_required_spend_usd == 95_000 and r.combined_budget_usd is None
    assert r.verified is True and acc.ABOVE not in ids(r)
    assert "hardship_requested=true" in r.california.explanation
    # Federal still computed: 20% x 240,000 = 48,000
    assert r.federal.disproportionality_limit_usd == 48_000
    # Asking about hardship relies on the unverifiable rule: min 20% x 240,000 = 48,000 and verified=False
    h = acc.path_of_travel_obligation("$240,000", "2026-06-01", jurisdiction="LA", full_path_of_travel_cost_usd="95k",
                                      hardship_requested="yes")
    assert h.california.hardship_min_spend_usd == 48_000
    assert h.verified is False and h.unverified_rule_ids == [acc.ABOVE]
    assert any(n.startswith("NOT VERIFIED (unverifiable)") for n in h.notes)


def test_san_diego_hardship_needs_ds_2123_and_appeals_ratification():
    r = acc.path_of_travel_obligation(300_000, "2026-09-15", jurisdiction="City of San Diego", hardship_requested=True)
    assert [f.item for f in r.california.forms] == ["City of San Diego Form DS-2123", "San Diego appeals ratification"]
    assert "Appeals Action Ratification" in r.california.forms[1].detail
    assert {acc.SD_FORM, acc.SD_RATIFY, acc.ABOVE} <= ids(r) and r.verified is False


def test_los_angeles_summary_of_upgrades_form_only_when_not_everything_complies():
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", jurisdiction="LADBS")
    assert [f.item for f in r.california.forms] == ["LADBS Summary of Accessibility Upgrades"]
    assert "IB/P/BC 2026-076" in r.california.forms[0].detail and "Unreasonable Hardship" in \
        r.california.forms[0].detail
    assert r.next_step.startswith("File LADBS's Summary of Accessibility Upgrades form")
    # cap 20% x 100,000 = 20,000 covers a 15,000 full upgrade -> everything complies, no form needed
    ok = acc.path_of_travel_obligation(100_000, "2026-05-01", jurisdiction="LA", full_path_of_travel_cost_usd=15_000)
    assert ok.california.forms == [] and ok.california.required_spend_usd == 15_000
    assert ok.next_step.startswith("All path-of-travel elements fit inside the cap")


def test_2027_submission_never_guesses_the_threshold():
    r = acc.path_of_travel_obligation(150_000, "2027-01-15", jurisdiction="LA",
                                      element_costs_usd={"entrance": 12_000, "route": 9_000})
    ca = r.california
    assert ca.status == "threshold_unknown_for_year" and ca.valuation_threshold_usd is None
    assert ca.max_required_spend_usd is None and ca.forms == []
    assert ca.twenty_percent_of_adjusted_cost_usd == 30_000                  # shown as the conditional cap
    assert "latest on file is $209,208 for 2026" in ca.explanation
    assert r.combined_budget_usd == 30_000                                   # federal limit only
    assert "2027 valuation threshold" in r.next_step
    assert r.verified is True                                                # no unverified value was used


@pytest.mark.parametrize("work, ca_status, fed_status, fed_rule", [
    ("restripe parking lot", "not_triggered", "not_evaluated", None),
    ("Readily achievable barrier removal", "capped_20_percent", "not_triggered", acc.F_BARRIER_NO_POT),
    ("reroofing", "capped_20_percent", "not_triggered", acc.F_WHAT),
    ("new door hardware", "capped_20_percent", "not_triggered", acc.F_MINOR),
    ("addition", "capped_20_percent", "not_evaluated", None),
])
def test_work_types(work, ca_status, fed_status, fed_rule):
    r = acc.path_of_travel_obligation(50_000, "2026-12-01", work_type=work)
    assert (r.california.status, r.federal.status) == (ca_status, fed_status)
    if fed_rule:
        assert fed_rule in ids(r)
    if ca_status == "not_triggered":
        assert acc.RESTRIPE in ids(r) and r.spending_plan == [] and "accessible_parking_requirements" in r.next_step
    if work in ("reroofing", "new door hardware", "Readily achievable barrier removal"):
        assert r.california.other_exceptions_unchecked and "another CBC 11B-202.4 exception" in r.summary


def test_federal_trigger_needs_a_primary_function_area_and_tenant_note():
    r = acc.path_of_travel_obligation(80_000, "2026-12-01", affects_primary_function_area=False,
                                      tenant_only_alteration=True)
    assert r.federal.status == "not_triggered" and "janitorial closets" in r.federal.explanation
    assert {acc.F_PRIMARY, acc.F_TENANT} <= ids(r)
    assert "landlord" in " ".join(r.notes)
    assert r.california.status == "capped_20_percent"       # California plan check still applies 11B-202.4


def test_publicly_funded_multifamily_and_safe_harbor():
    r = acc.path_of_travel_obligation(90_000, "2026-12-01", facility="affordable housing")
    assert r.facility == "publicly_funded_multifamily" and acc.PUB_MF in ids(r) and acc.S_RES_EXC in ids(r)
    assert r.federal.status == "not_evaluated"
    s = acc.path_of_travel_obligation(90_000, "2026-12-01", path_elements_built_to_1991_standards=True)
    assert acc.F_SAFE in ids(s) and "1991 ADA Standards" in " ".join(s.notes)


@pytest.mark.parametrize("value, key", [
    ("LADBS", "los_angeles"), ("City of Los Angeles", "los_angeles"), ("L.A.", "los_angeles"), (" la ", "los_angeles"),
    ("SD", "san_diego"), ("City of San Diego DSD", "san_diego"), ("san diego, ca", "san_diego"),
    ("LA County", "other"), ("County of San Diego", "other"), ("Pasadena", "other"), (None, "other"),
])
def test_jurisdiction_aliases(value, key):
    assert acc._jurisdiction(value)[0] == key
    if value and "County" in value:
        assert "is not the City" in acc._jurisdiction(value)[2]


@pytest.mark.parametrize("kwargs, msg", [
    ({"construction_cost_usd": 0}, "more than 0"),
    ({"construction_cost_usd": -5}, "cannot be negative"),
    ({"construction_cost_usd": "lots"}, "dollar amount"),
    ({"construction_cost_usd": float("inf")}, "finite"),
    ({"path_of_travel_costs_included_usd": 200_000}, "cannot be more than construction_cost_usd"),
    ({"element_costs_usd": {"entrance": 50_000}, "full_path_of_travel_cost_usd": 10_000}, "add up to"),
    ({"element_costs_usd": {"lobby chandelier": 5}}, "not a path-of-travel element"),
    ({"prior_alterations_cost_usd": 5_000, "prior_alterations": [{"date": "2025-01-01", "cost_usd": 1}]}, "not both"),
    ({"prior_alterations": [{"date": "2027-01-01", "cost_usd": 1}]}, "must be before permit_application_date"),
    ({"permit_application_date": "next week"}, "date like"),
    ({"work_type": "program access"}, "Title II duty"),
    ({"facility": "city", "work_type": "barrier removal"}, "Title III duty"),
    ({"facility": "apartments"}, "CBC Chapter 11A"),
    ({"work_type": "landscaping"}, "work_type must be"),
])
def test_path_of_travel_rejects_bad_input(kwargs, msg):
    args = {"construction_cost_usd": 100_000, "permit_application_date": "2026-12-01"} | kwargs
    with pytest.raises(ValueError, match=msg):
        acc.path_of_travel_obligation(**args)


# ---------------------------------------------------------------- parking (CBC 11B-208, ADA Table 208.2, CBC 11A)
def test_parking_per_facility_table_208_ev_spaces_and_van_spaces():
    # North lot: 120 - 6 EV charging spaces (not parking spaces, 11B-228.3) = 114 -> 101-150 row -> 5;
    #            van = max(1, ceil(5/6)) = 1.
    # Garage: 1,150 -> over 1,000 row: 20 + 1 per 100 or fraction over 1,000 = 20 + ceil(150/100) = 22;
    #         van = ceil(22/6) = ceil(3.67) = 4.
    r = acc.accessible_parking_requirements(facilities=[{"name": "North lot", "total_spaces": "120",
                                                         "ev_charging_spaces": 6},
                                                        {"name": "Garage", "total_spaces": 1150}])
    north, garage = r.facilities
    assert (north.counted_spaces, north.required_accessible, north.van_accessible) == (114, 5, 1)
    assert (garage.required_accessible, garage.van_accessible) == (22, 4)
    assert (r.total_required_accessible, r.total_van_accessible) == (27, 5)
    assert {acc.PK_FACILITY, acc.PK_TABLE, acc.S_PK_TABLE, acc.PK_VAN6, acc.PK_STALL, acc.PK_SIGN, acc.PK_TOW,
            acc.EVCS} <= ids(r)
    assert r.verified is True
    text = " ".join(x.detail for x in r.dimensions + r.signs_and_markings)
    for s in ("9 ft wide x 18 ft long", "12 ft wide with a 5 ft aisle", "9 ft wide with an 8 ft aisle",
              "8 feet 2 inches (98 in)", "Minimum Fine $250", "17 x 22 in", "36 x 36 in", "'NO PARKING'"):
        assert s in text, s
    # CBC Table 11B-208.2 (LADBS P/BC 2026-084) is now read and matches the federal table
    assert "CBC Table 11B-208.2 (as LADBS restates it for the 2025 CBC) and federal Table 208.2 give the same" in \
        " ".join(r.notes)
    # 6 EV charging spaces: Table 11B-228.3.2.1 row 5-25 -> 1 van accessible, 1 standard accessible, 0 ambulatory
    ev = north.accessible_evcs
    assert (ev.van_accessible, ev.standard_accessible, ev.ambulatory) == (1, 1, 0) and garage.accessible_evcs is None


@pytest.mark.parametrize("spaces, required", [
    (0, 0), (1, 1), (25, 1), (26, 2), (100, 4), (150, 5), (151, 6), (500, 9),
    (501, 11),          # 2% x 501 = 10.02 -> 11
    (550, 11),          # 2% x 550 = 11 exactly (no float round-up to 12)
    (1000, 20),         # 2% x 1,000 = 20
    (1001, 21),         # 20 + ceil(1/100) = 21
    (1100, 21), (1101, 22),
])
def test_table_208_rows(spaces, required):
    assert acc.accessible_parking_requirements(total_spaces=spaces).total_required_accessible == required


def test_hospital_and_rehab_percentages():
    # Hospital outpatient: 10% x 75 = 7.5 -> 8; van ceil(8/6) = 2.  Rehab / outpatient PT: 20% x 40 = 8; van 2.
    h = acc.accessible_parking_requirements(total_spaces=75, use="hospital")
    assert (h.total_required_accessible, h.total_van_accessible) == (8, 2) and h.use == "hospital_outpatient"
    p = acc.accessible_parking_requirements(total_spaces=40, use="physical therapy")
    assert (p.total_required_accessible, p.total_van_accessible) == (8, 2)
    mixed = acc.accessible_parking_requirements(facilities=[{"name": "Patients", "total_spaces": 75,
                                                             "use": "hospital"},
                                                            {"name": "Staff", "total_spaces": 75}])
    assert [f.required_accessible for f in mixed.facilities] == [8, 3]          # staff: 51-75 row -> 3
    assert "staff parking" in " ".join(mixed.notes)


def test_multifamily_11a_rates():
    # 88 covered units: 2% x 88 = 1.76 -> 2; 2 facility types -> at least 2.
    # Podium garage, 96 assigned: 2% x 96 = 1.92 -> 2.  Guest lot, 12 unassigned: 5% x 12 = 0.6 -> 1.
    # Total = max(2, 2 + 1) = 3; van one in every eight: 3/8 -> literal max(1, 0) = 1, rounded up 1.
    r = acc.accessible_parking_requirements(facilities=[
        {"name": "Podium", "total_spaces": 96, "kind": "parking garage", "assigned": True},
        {"name": "Guest", "total_spaces": 12, "kind": "surface lot", "assigned": False}],
        use="apartments", covered_dwelling_units="88 units")
    assert r.code == "CBC 11A (covered multifamily)" and r.covered_dwelling_units == 88
    assert [f.required_accessible for f in r.facilities] == [2, 1]
    assert (r.total_required_accessible, r.total_van_accessible, r.total_van_accessible_literal_minimum) == (3, 1, 1)
    assert {acc.A_PK, acc.A_VAN8, acc.A_PK_DIM} <= ids(r) and acc.S_PK_TABLE not in ids(r)
    assert "14 ft wide minimum" in r.dimensions[0].detail and "17 ft wide total" in r.dimensions[1].detail
    assert "2.083%" in r.dimensions[0].detail
    assert r.dimensions[2].detail.startswith("98 in (8 ft 2 in) minimum")     # 98 in = 8 ft 2 in
    assert r.verified is True


def test_multifamily_van_rounding_is_flagged_not_guessed():
    # 200 units, one unassigned lot of 200: 2% x 200 = 4; 5% x 200 = 10 -> total 10.
    # Van: 10/8 = 1.25 -> literal minimum 1 (floor), rounded up 2; CBC 1109A.8.6 does not state the rounding.
    r = acc.accessible_parking_requirements(total_spaces=200, use="multifamily", covered_dwelling_units=200)
    assert r.total_required_accessible == 10
    assert (r.total_van_accessible_literal_minimum, r.total_van_accessible) == (1, 2)
    notes = " ".join(r.notes)
    assert "does not say how to count a partial group of 8" in notes
    assert "counted as unassigned" in notes                        # assigned was not given -> the 5% rate


def test_restriping_cites_the_no_path_of_travel_rule():
    r = acc.accessible_parking_requirements(total_spaces=60, work_type="Slurry seal and restripe")
    assert r.work_type == "resurfacing_restriping" and acc.RESTRIPE in ids(r)
    assert r.total_required_accessible == 3                          # 51-75 row


@pytest.mark.parametrize("kwargs, msg", [
    ({}, "give total_spaces"),
    ({"total_spaces": 10, "facilities": [{"total_spaces": 5}]}, "not both"),
    ({"total_spaces": -1}, "cannot be negative"),
    ({"total_spaces": "12.5"}, "whole number"),
    ({"facilities": [{"total_spaces": 5, "ev_charging_spaces": 6}]}, "cannot exceed total_spaces"),
    ({"total_spaces": 40, "use": "covered_multifamily"}, "covered_dwelling_units"),
    ({"total_spaces": 40, "covered_dwelling_units": 30}, "covered_multifamily"),
    ({"facilities": [{"total_spaces": 5, "kind": "helipad"}], "use": "multifamily", "covered_dwelling_units": 9},
     "kind must be"),
    ({"total_spaces": 40, "use": "stadium of dreams"}, "use must be"),
    ({"total_spaces": 40, "work_type": "demolition"}, "work_type must be"),
])
def test_parking_rejects_bad_input(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        acc.accessible_parking_requirements(**kwargs)


# ---------------------------------------------------------------- field check (CBC 11B as restated by LADBS)
def test_field_check_doors_levels_counters_and_parking():
    r = acc.accessibility_field_check(door_clear_width='31 1/2"', level_change_height="3/8 in",
                                      level_change_beveled="no", counter_height=36, counter_length="3 ft",
                                      parking_stall_width=9, parking_aisle_width="8 ft", van_space=True,
                                      parking_vertical_clearance="8 ft 0 in", parking_sign_bottom_height=60,
                                      sign_in_circulation_path=False)
    assert check(r, "Door clear width").result == "fail"                  # 31.5 < 32 (11B-404.2.3)
    lvl = check(r, "Change in level")
    assert lvl.result == "fail" and "1:2" in lvl.fix                      # 1/4-1/2 in must be beveled 1:2
    assert check(r, "Sales/service counter height").result == "fail"      # 36 > 34
    assert check(r, "Accessible counter length").result == "pass"         # 36 = 36
    assert check(r, "Van space width").result == "pass"                   # 9 ft space + 8 ft aisle
    clr = check(r, "Parking vertical clearance")
    assert clr.result == "fail" and clr.measured.startswith("96 in")      # 8 ft = 96 < 98 in (8 ft 2 in)
    assert check(r, "Parking sign height").result == "pass"               # 60 >= 60
    assert (r.passed, r.failed, r.needs_info) == (3, 4, 0)
    assert r.next_step.startswith("Fix before the inspection: Door clear width") and r.verified is True


@pytest.mark.parametrize("width, depth, result", [
    (32, None, "pass"), (31.99, None, "fail"), (32, 24, "pass"),           # 24 in deep is not "more than 24"
    (34, 25, "fail"), (36, 25, "pass"),                                    # over 24 in deep -> 36 in
    ("2'-8\"", None, "pass"), ("2 ft 8 in", None, "pass"),
])
def test_door_clear_width(width, depth, result):
    assert check(acc.accessibility_field_check(door_clear_width=width, door_opening_depth=depth),
                 "Door clear width").result == result


@pytest.mark.parametrize("height, beveled, result", [
    (0.25, None, "pass"), ("1/4", None, "pass"),                          # 1/4 in max may be vertical
    ("3/8", True, "pass"), ("1/2 in", None, "needs_info"),               # 1/4-1/2 in beveled at 1:2
    (0.51, True, "fail"), ("1-1/2 in", None, "fail"),                    # over 1/2 in must be ramped
])
def test_change_in_level(height, beveled, result):
    assert check(acc.accessibility_field_check(level_change_height=height, level_change_beveled=beveled),
                 "Change in level").result == result


def test_ramps_slopes_and_rise_per_run():
    # 30 in rise over 360 in = 1:12 exactly -> pass; 30 in over 359 in is steeper -> fail.
    assert check(acc.accessibility_field_check(ramp_rise=30, ramp_run=360), "Ramp running slope").result == "pass"
    steep = acc.accessibility_field_check(ramp_rise=24, ramp_run="22 ft", ramp_clear_width="4 ft",
                                          ramp_cross_slope="2%")
    s = check(steep, "Ramp running slope")
    assert s.result == "fail" and s.measured == "9.091% (1:11)"            # 24 / 264
    assert "at least 288 in (24 ft)" in s.fix                              # 24 in x 12 = 288 in for 1:12
    assert check(steep, "Ramp rise per run").result == "pass"              # 24 <= 30
    assert check(steep, "Ramp clear width").result == "pass"               # 48 in
    assert check(steep, "Ramp cross slope").result == "pass"               # 2% <= 1:48 (2.083%)
    assert check(acc.accessibility_field_check(ramp_rise=31, ramp_run=400), "Ramp rise per run").result == "fail"
    for slope, result in (("1:12", "pass"), (8.33, "pass"), ("8.34%", "fail"), ("1 in 10", "fail")):
        assert check(acc.accessibility_field_check(ramp_running_slope=slope), "Ramp running slope").result == result
    # curb ramps: 1:12 running, 1:10 flares
    c = acc.accessibility_field_check(curb_ramp_running_slope="8%", curb_ramp_flare_slope="1:9")
    assert check(c, "Curb ramp running slope").result == "pass" and check(c, "Curb ramp flare slope").result == "fail"


def test_existing_building_steeper_ramp_note_only_when_the_federal_table_fits():
    # 5 in rise over 55 in = 1:11: steeper than 1:12 but not 1:10, rise <= 6 in -> federal Table 405.2 row applies
    fits = acc.accessibility_field_check(ramp_rise=5, ramp_run=55, existing_building=True)
    assert acc.S_STEEP in check(fits, "Ramp running slope").rule_ids
    assert "up to 1:10 for a rise of 6 in or less" in check(fits, "Ramp running slope").fix
    # 24 in rise is over every row's maximum rise -> no federal allowance offered
    big = acc.accessibility_field_check(ramp_rise=24, ramp_run=264, existing_building=True)
    assert acc.S_STEEP not in check(big, "Ramp running slope").rule_ids


def test_walkway_counter_turning_and_car_stall():
    r = acc.accessibility_field_check(walkway_clear_width=47, walkway_running_slope=5.5, walkway_cross_slope=2,
                                      checkout_counter=True, counter_height=38, turning_space_diameter=58,
                                      parking_stall_width="8'-6\"", parking_stall_length=18)
    assert check(r, "Site walkway width").result == "fail"                 # 47 < 48
    run = check(r, "Site walkway running slope")
    assert run.result == "fail" and acc.RAMP in run.rule_ids               # 5.5% > 5% -> regrade or build a ramp
    assert check(r, "Site walkway cross slope").result == "pass"           # 2% <= 2%
    assert check(r, "Checkout counter height").result == "pass"            # 38 <= 38
    assert check(r, "Turning space").result == "fail"                      # 58 < 60
    assert check(r, "Car space width").result == "fail"                    # 8.5 ft < 9 ft
    assert check(r, "Accessible space length").result == "pass"            # 18 ft


@pytest.mark.parametrize("width, aisle, result", [
    (12, 5, "pass"), (9, 8, "pass"), (10, None, "needs_info"), (12, None, "needs_info"), (8, 8, "fail"),
    (10, 6, "fail"),            # 10 ft is short of 12 ft, and a 6 ft aisle is short of 8 ft
])
def test_van_space_layouts(width, aisle, result):
    r = acc.accessibility_field_check(parking_stall_width=width, parking_aisle_width=aisle, van_space=True)
    assert check(r, "Van space width").result == result


@pytest.mark.parametrize("kwargs, msg", [
    ({}, "at least one measurement"),
    ({"door_clear_width": "wide"}, "must be a length"),
    ({"door_clear_width": -3}, "0 or more"),
    ({"door_opening_depth": 30}, "needs door_clear_width"),
    ({"ramp_run": 100}, "needs ramp_rise"),
    ({"ramp_rise": 10, "ramp_run": 0}, "more than 0"),
    ({"ramp_rise": 10, "ramp_run": 120, "ramp_running_slope": "1:20"}, "does not match"),
    ({"ramp_running_slope": "steep"}, "percent"),
    ({"level_change_height": 0.4, "level_change_beveled": "maybe"}, "true or false"),
])
def test_field_check_rejects_bad_input(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        acc.accessibility_field_check(**kwargs)


# ---------------------------------------------------------------- which rules apply
def test_triggers_new_walk_up_apartments():
    # 24 units >= 4 (24 CFR 100.201), no elevator -> the 8 ground-floor units are FHA-covered.
    # CBC 1102A.3.1: max(1, ceil(10% x 6 multistory units = 0.6)) = 1.
    r = acc.accessibility_triggers("new construction", "apartments", dwelling_units=24, has_elevator=False,
                                   ground_floor_units=8, multistory_units=6, as_of=AS_OF)
    assert regime(r, "CBC Chapter 11B").applies is False
    assert regime(r, "CBC Chapter 11A").applies is None                    # CBC unit-count definition not read
    fha = regime(r, "Fair Housing")
    assert fha.applies is True and "8 ground-floor" in fha.why and "after 1991-03-13" in fha.why
    assert r.fha_covered_units == 8 and r.cbc_11a_accessible_multistory_units == 1
    assert {acc.A_GROUND, acc.A_IMPRACT, acc.A_MULTI10, acc.FHA_ENTRANCE, acc.FHA_UNIT, acc.FHA_HUD,
            acc.FHA_DOJ} <= ids(r)
    pen = " ".join(p.detail for p in r.penalties)
    assert "$26,262" in pen and "$65,653" in pen and "$131,308" in pen and "$262,614" in pen
    assert "(one prior in 5 years)" in pen and "(two or more in 7 years)" in pen and "from 2025-07-14" in pen
    assert "accessible_parking_requirements" in r.other_tools and r.verified is True


@pytest.mark.parametrize("units, elevator, covered, applies", [
    (3, True, None, False),          # fewer than 4 units: not a covered building
    (4, True, 4, True),              # elevator building: every unit
    (12, None, None, True),          # elevator unknown -> no unit count
])
def test_triggers_fha_unit_threshold(units, elevator, covered, applies):
    r = acc.accessibility_triggers("new", "condos", dwelling_units=units, has_elevator=elevator)
    assert regime(r, "Fair Housing").applies is applies and r.fha_covered_units == covered


@pytest.mark.parametrize("first_occupied, applies", [("1990-05-01", False), ("1991-03-13", False),
                                                      ("1995-06-01", True), (None, None)])
def test_triggers_existing_apartments_fha_first_occupancy(first_occupied, applies):
    r = acc.accessibility_triggers("remodel", "apartments", dwelling_units=40, has_elevator=True,
                                   first_occupied_date=first_occupied)
    assert regime(r, "Fair Housing").applies is applies
    assert acc.A_MAINTAIN in ids(r)


@pytest.mark.parametrize("stories, sqft, special, shops, exempt", [
    (2, "4,500 sq ft", None, None, True),       # 2 < 3 stories
    (3, 2500, None, None, True),                # 2,500 < 3,000 sq ft per story
    (3, 3000, None, None, False),               # neither test met
    (2, 4500, None, 5, False),                  # 5+ sales establishments = shopping center: no exemption
    (2, 4500, None, 4, True),
    (2, 4500, "dental office", None, False),    # professional office of a health care provider
])
def test_triggers_federal_elevator_exemption(stories, sqft, special, shops, exempt):
    r = acc.accessibility_triggers("TI", "retail", stories=stories, sq_ft_per_story=sqft, special_facility=special,
                                   sales_or_rental_establishments=shops)
    assert r.federal_elevator_exempt is exempt and acc.F_ELEV in ids(r)


def test_triggers_retail_ti_points_to_path_of_travel():
    r = acc.accessibility_triggers("Office TI", "restaurant", as_of=AS_OF)
    assert r.project_type == "alteration" and r.building_use == "public_accommodation"
    assert regime(r, "CBC Chapter 11B").applies and regime(r, "ADA Title III").applies
    assert {acc.ALTERED, acc.POT_REQ, acc.F_ALTER, acc.F_POT, acc.F_PENALTY, acc.PLAN_CHECK, acc.S_SITES} <= ids(r)
    assert r.other_tools[0] == "path_of_travel_obligation"
    assert "$118,225" in r.penalties[0].detail and "$236,451" in r.penalties[0].detail


def test_triggers_new_public_housing_unit_counts():
    # 60 units: mobility max(1, ceil(5% x 60 = 3)) = 3; communication max(1, ceil(2% x 60 = 1.2)) = 2;
    # 60 >= 3 units -> adaptable units too. Public entrances: ceil(60% x 3 = 1.8) = 2.
    r = acc.accessibility_triggers("new", "public housing", dwelling_units=60, has_elevator=True, public_entrances=3,
                                   as_of=AS_OF)
    assert (r.public_housing_mobility_units, r.public_housing_communication_units) == (3, 2)
    assert r.accessible_public_entrances_min == 2 and r.fha_covered_units == 60
    assert regime(r, "ADA Title II").applies and regime(r, "CBC Chapter 11B").applies
    assert "adaptable units are also required" in regime(r, "CBC Chapter 11B").requirements[0].detail
    assert {acc.PH_UNITS, acc.PH_DEF, acc.PUB_MF, acc.S_ENTRANCES} <= ids(r)


def test_triggers_barrier_removal_is_for_public_accommodations_only():
    r = acc.accessibility_triggers("barrier removal", "warehouse")
    assert acc.F_BARRIER not in ids(r) and "commercial facility not open to the public" in \
        regime(r, "ADA Title III").why
    p = acc.accessibility_triggers("barrier removal", "store")
    assert {acc.F_BARRIER, acc.F_BARRIER_NO_POT} <= ids(p)


def test_triggers_word_matching():
    # "new" alone is new construction; "new restrooms" inside a remodel is not
    assert acc.accessibility_triggers("new", "office").project_type == "new_construction"
    assert acc.accessibility_triggers("remodel with new restrooms", "office").project_type == "alteration"
    assert acc.accessibility_triggers("ground-up", "Mixed-Use", dwelling_units=10).building_use == "mixed_use"


@pytest.mark.parametrize("kwargs, msg", [
    ({"building_use": "retail", "dwelling_units": 4}, "dwelling_units is for"),
    ({"building_use": "apartments"}, "at least 1"),
    ({"building_use": "apartments", "dwelling_units": 4, "ground_floor_units": 5}, "cannot exceed"),
    ({"building_use": "retail", "stories": 0}, "at least 1"),
    ({"building_use": "spaceport"}, "building_use must be"),
    ({"building_use": "retail", "project_type": "vibes"}, "project_type must be"),
    ({"building_use": "retail", "special_facility": "casino"}, "special_facility must be"),
])
def test_triggers_reject_bad_input(kwargs, msg):
    args = {"project_type": "new construction"} | kwargs
    with pytest.raises(ValueError, match=msg):
        acc.accessibility_triggers(**args)


# ---------------------------------------------------------------- CASp lease and business license
def test_landlord_lease_clauses_follow_the_city_documents():
    r = acc.casp_lease_disclosure("lessor", premises_inspected_by_casp="no", lessor_name="Acme Properties LLC")
    status, stmt = r.lease_clauses
    assert status.text == ("In accordance with California Civil Code section 1938, Acme Properties LLC hereby states "
                           "that the Premises has not been inspected by a Certified Access Specialist (CASp).")
    assert stmt.text == core.rule(acc.LEASE_STMT).param("statement_text_as_cited_to_1938e")
    assert "The parties must mutually agree" in stmt.text                  # the version cited to 1938(e)
    assert "'shall'" in stmt.caution and "'must'" in stmt.caution
    assert r.business_license_fee_usd == 4
    assert "the scheduled drop to $1 on 2024-01-01 was repealed by AB 2164 (2022)" in r.business_license[0].detail
    assert {acc.LEASE_STATUS, acc.LEASE_STMT, acc.CASP, acc.FEE4, acc.AB3002} <= ids(r) and r.verified is True


def test_unknown_casp_status_gives_both_versions_and_inspected_adds_report_warning():
    r = acc.casp_lease_disclosure("landlord")
    assert "Use ONE of" in r.lease_clauses[0].text and "has been inspected" in r.lease_clauses[0].text
    i = acc.casp_lease_disclosure("landlord", premises_inspected_by_casp=True)
    assert "hereby states that the Premises has been inspected" in i.lease_clauses[0].text
    assert "CASp report exists" in " ".join(i.notes)


def test_tenant_in_san_diego_with_ti_gets_fee_example_and_path_of_travel_point():
    # San Diego mail application: $34 business tax + $4 state fee = $38; 8 employees x $1.47 = 11.76 -> $49.76,
    # x $2.82 = 22.56 -> $60.56 (the two City pages conflict).
    r = acc.casp_lease_disclosure("tenant", business_license_city="City of San Diego", employees="8",
                                  tenant_improvements_planned=True)
    sd = r.san_diego_example
    assert (sd.business_tax_usd, sd.state_disability_access_fee_usd, sd.total_before_wage_fee_usd) == (34, 4, 38)
    assert sd.minimum_wage_enforcement_fee_per_employee_usd == [1.47, 2.82]
    assert sd.estimated_total_usd == [49.76, 60.56]
    assert acc.F_TENANT in ids(r) and "path_of_travel_obligation" in r.rights_and_duties[-1].detail
    big = acc.casp_lease_disclosure("tenant", business_license_city="SD", employees=20)
    assert big.san_diego_example.estimated_total_usd is None and "Treasurer" in " ".join(big.notes)
    county = acc.casp_lease_disclosure("tenant", business_license_city="County of San Diego")
    assert county.san_diego_example is None and "City of San Diego" in " ".join(county.notes)


def test_residential_rental_gets_reasonable_modification_rules():
    r = acc.casp_lease_disclosure("owner", property_type="apartment")
    assert r.property_type == "residential" and r.lease_clauses == []
    assert acc.FHA_MODS in ids(r) and "required building permits obtained" in r.rights_and_duties[0].detail
    assert acc.LEASE_STMT not in ids(r)


@pytest.mark.parametrize("kwargs, msg", [
    ({"role": "broker"}, "role must be"),
    ({"property_type": "boat slip"}, "property_type must be"),
    ({"premises_inspected_by_casp": "perhaps"}, "true or false"),
    ({"employees": -2}, "cannot be negative"),
])
def test_lease_rejects_bad_input(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        acc.casp_lease_disclosure(**kwargs)


# ---------------------------------------------------------------- tool descriptions
def test_tool_descriptions_quote_rulebook_numbers():
    d = acc._tool_docs()
    assert "$209,208 for 2026" in d["pot"] and "need not exceed 20%" in d["pot"]
    assert "1 in 6" in d["parking"] and "van 1 in 8" in d["parking"] and "8 feet 2 inches" in d["parking"]
    assert "32 in" in d["field"] and "1:12" in d["field"] and "34 in" in d["field"]
    assert "4+ units first occupied after 1991-03-13" in d["triggers"] and "$118,225" in d["triggers"]
    assert "$4 state disability-access fee" in d["lease"]


# ---------------------------------------------------------------- MCP
def test_register_and_call_every_tool_through_an_mcp_client():
    mcp = FastMCP("t")
    acc.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            out = {
                "pot": await c.call_tool("path_of_travel_obligation", {
                    "construction_cost_usd": "$150,000", "permit_application_date": "12/01/2026",
                    "jurisdiction": "City of San Diego",
                    "element_costs_usd": {"Entrance": "12k", "route": 9000, "restroom": 28000},
                    "prior_alterations": [{"date": "2024-02-10", "cost_usd": "40,000"}]}),
                "park": await c.call_tool("accessible_parking_requirements", {
                    "facilities": [{"name": "North lot", "total_spaces": "120", "ev_charging_spaces": 6}]}),
                "field": await c.call_tool("accessibility_field_check", {
                    "door_clear_width": "2 ft 8 in", "ramp_running_slope": "1:12", "parking_stall_width": "9 ft"}),
                "trig": await c.call_tool("accessibility_triggers", {
                    "project_type": "TI", "building_use": "Restaurant", "stories": "2", "sq_ft_per_story": 4500}),
                "lease": await c.call_tool("casp_lease_disclosure", {"role": "Landlord",
                                                                     "premises_inspected_by_casp": "no"}),
            }
            bad = [await c.call_tool(name, args, raise_on_error=False) for name, args in [
                ("path_of_travel_obligation", {"construction_cost_usd": "lots"}),
                ("path_of_travel_obligation", {"construction_cost_usd": 100000, "permit_application_date": "soon"}),
                ("accessible_parking_requirements", {"total_spaces": "12.5"}),
                ("accessibility_field_check", {}),
                ("accessibility_triggers", {"project_type": "new", "building_use": "apartments"}),
                ("casp_lease_disclosure", {"role": "broker"}),
            ]]
            return tools, {k: v.structured_content for k, v in out.items()}, bad
    tools, out, bad = asyncio.run(run())
    assert set(tools) == {"path_of_travel_obligation", "accessible_parking_requirements", "accessibility_field_check",
                          "accessibility_triggers", "casp_lease_disclosure"}
    for t in tools.values():
        a = t.annotations.model_dump()
        assert a.get("readOnlyHint", a.get("read_only_hint")) is True
        assert a.get("openWorldHint", a.get("open_world_hint")) is False
    assert "$209,208" in tools["path_of_travel_obligation"].description
    # $150,000 + $40,000 prior = $190,000 <= $209,208 -> cap 20% = $38,000
    pot = out["pot"]
    assert pot["california"]["adjusted_construction_cost_usd"] == 190_000
    assert pot["california"]["max_required_spend_usd"] == 38_000 and pot["permit_application_date"] == "2026-12-01"
    assert pot["spending_plan"][0]["funded_usd"] == 12_000 and pot["verified"] is True
    assert out["park"]["total_required_accessible"] == 5 and out["park"]["total_van_accessible"] == 1
    assert out["field"]["passed"] == 3 and out["field"]["failed"] == 0
    assert out["trig"]["federal_elevator_exempt"] is True and out["trig"]["building_use"] == "public_accommodation"
    assert "has not been inspected" in out["lease"]["lease_clauses"][0]["text"]
    assert out["lease"]["citations"] and out["lease"]["disclaimer"].startswith("Compliance information")
    assert all(b.is_error for b in bad)
    texts = [b.content[0].text for b in bad]
    assert "dollar amount" in texts[0] and "date like" in texts[1] and "whole number" in texts[2]
    assert "at least one measurement" in texts[3] and "at least 1" in texts[4] and "role must be" in texts[5]


# ================================================================ regression tests from the 2026-11-25 review
# Each expected value is worked by hand from the rule text re-read on 2026-11-25 (LADBS P/BC 2026-084, CL-BC-2026-026,
# -027, -036, eCFR 28 CFR 35.151 / 36.402-36.404, 24 CFR 100.205).
def test_review_rules_are_in_the_pack_and_verified():
    book = {r.id: r for r in core.all_rules()}
    for rid in (acc.PK_TABLE, acc.PK_TRUCK, acc.PK_MEASURE, acc.PK_SLOPE, acc.PK_VERT80, acc.PK_SIGN80, acc.EVCS,
                acc.ENTR_ALL, acc.PH_ALTER, acc.A_SIGNS, acc.T2_MINOR):
        assert book[rid].pack == "web_accessibility" and book[rid].status == "confirmed", rid
    # the 96-inch van aisle conflict and the 10% / 20% scope were corrected
    assert book[acc.PK_VAN6].status == "corrected" and "aisle_width_conflict" in book[acc.PK_VAN6].parameters
    assert book[acc.PK_FACILITY].status == "corrected"
    assert "specializing in treating conditions that affect mobility" in book[acc.PK_FACILITY].param("rehab_scope")
    # CBC Table 11B-208.2 has exactly the federal rows
    assert book[acc.PK_TABLE].param("table") == book[acc.S_PK_TABLE].param("table")


# ---------------------------------------------------------------- path of travel
def test_primary_function_cost_cannot_exceed_the_job():
    with pytest.raises(ValueError, match="cannot be more than this job's cost less its path-of-travel work"):
        acc.path_of_travel_obligation(100_000, "2026-05-01", primary_function_alteration_cost_usd=500_000)
    # 100,000 - 10,000 path-of-travel work = 90,000 is the most it can be: 20% x 90,000 = 18,000
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", path_of_travel_costs_included_usd=10_000,
                                      primary_function_alteration_cost_usd=90_000)
    assert r.federal.disproportionality_limit_usd == 18_000


@pytest.mark.parametrize("value, ok", [
    ("2026-05-01T10:00", True), ("2026-05-01 10:00:00", True), ("2026-05-01T10:00:00Z", True),
    ("2026-05-01garbage", False), ("2026-05-01 next week", False), ("1992-01-26", False),
])
def test_permit_date_parsing_and_pre_ada_dates(value, ok):
    if ok:
        assert acc.path_of_travel_obligation(100_000, value).permit_application_date == "2026-05-01"
    else:
        with pytest.raises(ValueError, match="date like|not after 1992-01-26"):
            acc.path_of_travel_obligation(100_000, value)


def test_dollar_amounts_have_a_sanity_bound():
    with pytest.raises(ValueError, match="check the number"):
        acc.path_of_travel_obligation(1e308, "2026-05-01")


def test_maintenance_work_gets_conditional_spend_instructions():
    # Reroofing is not an alteration federally (28 CFR 36.402(b)(1)); the CBC exceptions besides Exception 8 were not
    # read, so the California 20% x 100,000 = 20,000 figure is only "if plan check treats it as an alteration".
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", work_type="REROOF", full_path_of_travel_cost_usd=5_000)
    assert r.california.other_exceptions_unchecked and r.federal.status == "not_triggered"
    assert "If it is an alteration, plan at least $5,000" in r.summary and "Plan at least" not in r.summary
    assert r.next_step.startswith("First ask plan check whether this maintenance work is an alteration")
    assert r.california.explanation.startswith("Only if plan check treats this maintenance work as an alteration")
    # an ordinary alteration keeps the unconditional instruction
    plain = acc.path_of_travel_obligation(100_000, "2026-05-01", full_path_of_travel_cost_usd=5_000)
    assert "Plan at least $5,000" in plain.summary and not plain.next_step.startswith("First ask")


def test_commercial_facility_access_work_is_an_alteration_federally():
    # 28 CFR 36.304(a): the barrier-removal duty is a public accommodation's. An office (commercial facility) doing
    # "ADA upgrades" is altering, so 36.403 applies: 20% x 100,000 = 20,000.
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", facility="Office", work_type="ADA upgrades")
    assert (r.work_type, r.facility) == ("barrier_removal", "commercial_facility")
    assert r.federal.status == "capped_20_percent" and r.federal.disproportionality_limit_usd == 20_000
    assert acc.F_BARRIER in ids(r) and acc.F_BARRIER_NO_POT not in ids(r)
    assert "commercial facility not open to the public does not have it" in " ".join(r.notes)
    store = acc.path_of_travel_obligation(100_000, "2026-05-01", facility="store", work_type="ADA upgrades")
    assert store.federal.status == "not_triggered" and acc.F_BARRIER_NO_POT in ids(store)


def test_title_ii_minor_work_does_not_trigger_path_of_travel():
    # 28 CFR 35.151(b)(4)(i)(B): windows, hardware, controls, electrical outlets and signage
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", facility="public school", work_type="new door hardware")
    assert r.facility == "public_entity" and r.federal.law.startswith("ADA Title II")
    assert r.federal.status == "not_triggered" and acc.T2_MINOR in ids(r) and "signage" in r.federal.explanation


def test_hardship_flag_below_threshold_and_zero_adjusted_cost_are_explained():
    r = acc.path_of_travel_obligation(100_000, "2026-05-01", hardship_requested=True)
    assert "hardship_requested was ignored" in " ".join(r.notes) and acc.ABOVE not in ids(r) and r.verified
    z = acc.path_of_travel_obligation(100_000, "2026-05-01", path_of_travel_costs_included_usd=100_000)
    assert z.california.adjusted_construction_cost_usd == 0 and "re-run with work_type barrier_removal" in \
        " ".join(z.notes)


def test_federal_prior_work_counts_only_primary_function_areas_note():
    r = acc.path_of_travel_obligation(100_000, "2026-05-01",
                                      prior_alterations=[{"date": "2025-01-10", "cost_usd": 20_000}])
    assert r.federal.counted_prior_cost_usd == 20_000
    assert "only earlier alterations to primary function areas" in " ".join(r.notes)


@pytest.mark.parametrize("value, fac", [
    ("medical office", "public_accommodation"), ("Dental Office", "public_accommodation"),
    ("public school", "public_entity"), ("fire station", "public_entity"), ("warehouse", "commercial_facility"),
])
def test_facility_aliases(value, fac):
    assert acc.path_of_travel_obligation(100_000, "2026-05-01", facility=value).facility == fac


@pytest.mark.parametrize("value, key, assumed", [
    ("Los Angeles, CA 90012", "los_angeles", True), ("LA", "los_angeles", True), ("City of Los Angeles", "los_angeles",
                                                                                 False),
    ("LADBS", "los_angeles", False), ("San Diego 92101", "san_diego", True), ("City of San Diego", "san_diego", False),
])
def test_jurisdiction_zip_codes_and_the_city_assumption_note(value, key, assumed):
    k, _, note = acc._jurisdiction(value)
    assert k == key and (note is not None) is assumed
    if assumed:
        assert "unincorporated" in note


# ---------------------------------------------------------------- parking
def test_evcs_are_counted_separately_with_table_11b_228_3_2_1():
    # 150 EV spaces (over 100): van 1 + ceil(50/300) = 2; standard 3 + ceil(50/60) = 4; ambulatory 3 + ceil(50/50) = 4.
    # Parking: 300 - 150 = 150 counted -> 101-150 row -> 5.
    r = acc.accessible_parking_requirements(facilities=[{"total_spaces": 300, "ev_charging_spaces": 150}])
    f = r.facilities[0]
    assert (f.counted_spaces, f.required_accessible) == (150, 5)
    assert (f.accessible_evcs.van_accessible, f.accessible_evcs.standard_accessible, f.accessible_evcs.ambulatory) == \
        (2, 4, 4)
    assert acc.EVCS in ids(r) and "one charging level and connector type" in " ".join(r.notes)
    assert "Accessible EV charging stations (CBC 11B-228.3): Facility 1: 2 van accessible, 4 standard accessible, " \
           "4 ambulatory." in r.summary
    for n, want in ((4, (1, 0, 0)), (25, (1, 1, 0)), (50, (1, 1, 1)), (75, (1, 2, 2)), (100, (1, 3, 3)),
                    (101, (2, 4, 4))):
        e = acc._evcs(n)
        assert (e.van_accessible, e.standard_accessible, e.ambulatory) == want, n


def test_bus_truck_delivery_spaces_are_exempt_from_the_11b_count():
    # 60 - 10 truck - 30 EV = 20 counted -> 1-25 row -> 1 (CBC 11B-208.1 Exception 1)
    r = acc.accessible_parking_requirements(facilities=[{"total_spaces": 60, "bus_truck_delivery_spaces": 10,
                                                         "ev_charging_spaces": 30}])
    assert (r.facilities[0].counted_spaces, r.total_required_accessible) == (20, 1)
    assert acc.PK_TRUCK in ids(r) and "passenger drop-off and loading zone" in " ".join(r.notes)
    with pytest.raises(ValueError, match="cannot exceed total_spaces"):
        acc.accessible_parking_requirements(facilities=[{"total_spaces": 60, "bus_truck_delivery_spaces": 40,
                                                         "ev_charging_spaces": 30}])
    with pytest.raises(ValueError, match="CBC 11B exception"):
        acc.accessible_parking_requirements(facilities=[{"total_spaces": 60, "bus_truck_delivery_spaces": 5}],
                                            use="apartments", covered_dwelling_units=40)


@pytest.mark.parametrize("use, outcome", [
    ("rehab", "ambiguous"), ("drug rehab center", "ambiguous"), ("outpatient clinic", "ambiguous"),
    ("outpatient physical therapy", 20), ("physical rehabilitation", 20), ("Hospital Outpatient", 10),
    ("medical office", 4), ("dental office", 4),          # 100 spaces: 76-100 row -> 4
])
def test_hospital_and_rehab_rates_need_the_right_facility(use, outcome):
    if outcome == "ambiguous":
        with pytest.raises(ValueError, match="ambiguous"):
            acc.accessible_parking_requirements(total_spaces=100, use=use)
    else:          # 10% x 100 = 10; 20% x 100 = 20
        assert acc.accessible_parking_requirements(total_spaces=100, use=use).total_required_accessible == outcome


def test_multifamily_zero_spaces_cap_and_signs():
    none = acc.accessible_parking_requirements(total_spaces=0, use="apartments", covered_dwelling_units=10)
    assert none.total_required_accessible == 0 and none.facilities == []
    # 500 units: 2% = 10, but only 5 spaces exist -> all 5, with a note
    cap = acc.accessible_parking_requirements(total_spaces=5, use="apartments", covered_dwelling_units=500)
    assert cap.total_required_accessible == 5 and "only 5 spaces are provided" in " ".join(cap.notes)
    vis = acc.accessible_parking_requirements(total_spaces=40, use="condos", covered_dwelling_units=40)
    sign = next(s for s in vis.signs_and_markings if s.item == "Accessible space sign")
    assert acc.A_SIGNS in sign.rule_ids and "70 sq in" in sign.detail and "80 in" in sign.detail
    assert "Required at the unassigned" in sign.detail
    mine = acc.accessible_parking_requirements(facilities=[{"total_spaces": 40, "assigned": True}],
                                               use="multifamily", covered_dwelling_units=40)
    assert "not required" in next(s for s in mine.signs_and_markings if s.item == "Accessible space sign").detail
    assert mine.next_step.startswith("Show 1 accessible space (1 van)")


def test_11b_parking_details_from_the_2026_bulletin():
    r = acc.accessible_parking_requirements(total_spaces=40, work_type="tenant improvement")
    text = " ".join(x.detail for x in r.dimensions + r.signs_and_markings)
    for s in ("80 in", "1:48 (2.083%)", "centerline of the markings", "within a walk or other circulation path"):
        assert s in text, s
    assert {acc.PK_VERT80, acc.PK_SLOPE, acc.PK_MEASURE, acc.PK_SIGN80} <= ids(r)
    with pytest.raises(ValueError, match="work_type must be"):
        acc.accessible_parking_requirements(total_spaces=40, work_type="repave")


# ---------------------------------------------------------------- field check
@pytest.mark.parametrize("value, result", [
    (0.0833, "ambiguous"), ("0.10", "ambiguous"), (0.5, "ambiguous"),
    ("0.5%", "pass"), ("1/12", "pass"), ("1/10", "fail"), (8.33, "pass"), (0, "pass"), ("-2%", "error"),
])
def test_slope_decimals_are_not_silently_read_as_percents(value, result):
    if result == "ambiguous":
        with pytest.raises(ValueError, match="ambiguous"):
            acc.accessibility_field_check(ramp_running_slope=value)
    elif result == "error":
        with pytest.raises(ValueError):
            acc.accessibility_field_check(ramp_running_slope=value)
    else:
        assert check(acc.accessibility_field_check(ramp_running_slope=value), "Ramp running slope").result == result


@pytest.mark.parametrize("kwargs, item, result", [
    ({"parking_stall_width": "108 in"}, "Car space width", "pass"),           # 108 in = 9 ft
    ({"parking_stall_width": "8.99"}, "Car space width", "fail"),
    ({"counter_height": "3 ft"}, "Sales/service counter height", "fail"),     # 36 in > 34 in
    ({"door_clear_width": "813 mm"}, "Door clear width", "pass"),             # 813 / 25.4 = 32.01 in
    ({"door_clear_width": "81 cm"}, "Door clear width", "fail"),              # 810 / 25.4 = 31.89 in
    ({"door_clear_width": "32 in."}, "Door clear width", "pass"),
    ({"door_clear_width": "2ft8in"}, "Door clear width", "pass"),
])
def test_length_units(kwargs, item, result):
    assert check(acc.accessibility_field_check(**kwargs), item).result == result


@pytest.mark.parametrize("kwargs, msg", [
    ({"parking_stall_width": 108}, "does not look like feet"),
    ({"parking_stall_length": "216"}, "does not look like feet"),
    ({"counter_height": 3}, "does not look like inches"),
    ({"door_clear_width": "-32 in"}, "0 or more"),
])
def test_implausible_bare_numbers_are_rejected(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        acc.accessibility_field_check(**kwargs)


@pytest.mark.parametrize("height, in_path, result", [
    (60, None, "needs_info"), (79, None, "needs_info"), (80, None, "pass"), (59, None, "fail"),
    (70, True, "fail"), (80, True, "pass"), (60, False, "pass"), (59.5, False, "fail"),
])
def test_parking_sign_height_in_a_circulation_path(height, in_path, result):
    # CBC 11B-502.6: 60 in to the bottom; Exception: 80 in within a circulation path
    c = check(acc.accessibility_field_check(parking_sign_bottom_height=height, sign_in_circulation_path=in_path),
              "Parking sign height")
    assert c.result == result and acc.PK_SIGN80 in c.rule_ids


@pytest.mark.parametrize("clear, garage, van, result", [
    (90, True, False, "pass"),     # existing multistory garage, car space: 80 in minimum
    (79, True, False, "fail"),
    (90, True, True, "fail"),      # the exception is not for van spaces: 98 in
    (90, False, False, "fail"),    # not an existing garage: 98 in
])
def test_existing_garage_vertical_clearance_exception(clear, garage, van, result):
    c = check(acc.accessibility_field_check(parking_vertical_clearance=clear, existing_parking_structure=garage,
                                            van_space=van), "Parking vertical clearance")
    assert c.result == result and (acc.PK_VERT80 in c.rule_ids) is garage


@pytest.mark.parametrize("slope, result", [("2%", "pass"), ("1:48", "pass"), ("2.5%", "fail"), ("1:20", "fail")])
def test_parking_surface_slope(slope, result):
    assert check(acc.accessibility_field_check(parking_surface_slope=slope), "Parking space / aisle slope").result == \
        result


def test_van_12_plus_5_layout_flags_the_ladbs_96_inch_statement():
    r = acc.accessibility_field_check(parking_stall_width=12, parking_aisle_width=5, van_space=True)
    c = check(r, "Van space width")
    assert c.result == "pass" and acc.PK_VAN6 in c.rule_ids and "In Los Angeles confirm with plan check" in \
        " ".join(r.notes)
    nine = acc.accessibility_field_check(parking_stall_width=9, parking_aisle_width=8, van_space=True)
    assert acc.PK_VAN6 not in check(nine, "Van space width").rule_ids
    assert "centerline of the markings" in " ".join(nine.notes)


@pytest.mark.parametrize("kwargs, expect", [
    ({"ramp_running_slope": "1:11"}, "up to 1:10 for a rise of 6 in"),        # band 1:12-1:10, rise unknown
    ({"ramp_rise": 2, "ramp_run": 18}, "up to 1:8 for a rise of 3 in"),       # 1:9 is in the 1:10-1:8 band
    ({"ramp_rise": 5, "ramp_run": 45}, None),                                 # 1:9 band allows 3 in only
    ({"ramp_running_slope": "1:7"}, None),                                    # steeper than 1:8 is prohibited
])
def test_existing_ramp_uses_the_table_405_2_band_the_slope_is_in(kwargs, expect):
    c = check(acc.accessibility_field_check(existing_building=True, **kwargs), "Ramp running slope")
    assert c.result == "fail"
    if expect:
        assert expect in c.fix and acc.S_STEEP in c.rule_ids
    else:
        assert acc.S_STEEP not in c.rule_ids


# ---------------------------------------------------------------- triggers
def test_health_care_office_has_no_federal_elevator_exemption():
    # 36.401(d)(2) / 36.404(a): the exemption is not available for the professional office of a health care provider
    r = acc.accessibility_triggers("new", "dental office", stories=2, sq_ft_per_story=2000)
    assert r.building_use == "public_accommodation" and r.federal_elevator_exempt is False
    assert "read as special_facility health_care_office" in " ".join(r.notes)


def test_single_story_building_is_not_told_it_needs_an_elevator():
    r = acc.accessibility_triggers("new", "strip mall", stories=1, sq_ft_per_story=9000)
    assert r.federal_elevator_exempt is None
    assert any(q.detail.startswith("Single-story building") for x in r.regimes for q in x.requirements)


def test_new_building_entrances_california_is_stricter_than_60_percent():
    r = acc.accessibility_triggers("new", "retail", public_entrances=5, as_of=AS_OF)
    assert r.accessible_public_entrances_min == 3                          # ceil(60% x 5 = 3)
    assert acc.ENTR_ALL in ids(r) and "plan for all 5" in " ".join(g.detail for g in r.general)
    assert any(q.item.startswith("Entrances (CBC 11B-206.4.1") for q in regime(r, "CBC Chapter 11B").requirements)


@pytest.mark.parametrize("first, permit, applies, covered", [
    ("1991-06-01", "1990-05-01", False, None),    # last permit on or before 1990-06-15 (24 CFR 100.205(a))
    ("1991-06-01", "1990-06-15", False, None),
    ("1991-06-01", "1990-06-16", True, 10),
    ("1991-06-01", None, True, 10),
])
def test_fha_june_15_1990_permit_rule(first, permit, applies, covered):
    r = acc.accessibility_triggers("remodel", "apartments", dwelling_units=10, has_elevator=True,
                                   first_occupied_date=first, last_building_permit_date=permit, as_of=AS_OF)
    fha = regime(r, "Fair Housing")
    assert fha.applies is applies and r.fha_covered_units == covered
    assert ("FHA-covered" in r.summary) is applies
    if permit is None:
        assert "give last_building_permit_date" in fha.why


@pytest.mark.parametrize("kwargs, msg", [
    ({"first_occupied_date": "2030-01-01"}, "is after"),
    ({"first_occupied_date": "1995-01-01", "last_building_permit_date": "1996-01-01"}, "after first_occupied_date"),
])
def test_existing_building_dates_are_checked(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        acc.accessibility_triggers("remodel", "apartments", dwelling_units=10, has_elevator=True, as_of=AS_OF, **kwargs)


def test_public_housing_alterations_vacated_building_rule():
    # 40 > 15 units: mobility ceil(5% x 40 = 2) = 2; communication ceil(2% x 40 = 0.8) = 1
    r = acc.accessibility_triggers("alteration", "public housing", dwelling_units=40)
    req = next(q for q in regime(r, "CBC Chapter 11B").requirements if "233.3.4" in q.item)
    assert "at least 2 unit(s) need mobility features" in req.detail and "1 communication" in req.detail
    assert acc.PH_ALTER in ids(r)
    small = acc.accessibility_triggers("alteration", "public housing", dwelling_units=12)
    assert "does not apply to 12 unit(s)" in next(q for q in regime(small, "CBC Chapter 11B").requirements
                                                   if "233.3.4" in q.item).detail


def test_title_ii_building_is_not_given_the_title_iii_elevator_exemption():
    r = acc.accessibility_triggers("new", "city hall", stories=2, sq_ft_per_story=2000)
    assert r.federal_elevator_exempt is None and "was not applied" in " ".join(r.notes)


# ---------------------------------------------------------------- lease
def test_lease_property_type_words_and_limits():
    assert acc.casp_lease_disclosure(property_type="retail space").property_type == "commercial"
    assert acc.casp_lease_disclosure(property_type="single family house").property_type == "residential"
    with pytest.raises(ValueError, match="mixes commercial and residential"):
        acc.casp_lease_disclosure(property_type="apartment and retail")
    with pytest.raises(ValueError, match="200 at most"):
        acc.casp_lease_disclosure(lessor_name="X" * 201)
    r = acc.casp_lease_disclosure("tenant", business_license_city="San Diego, CA 92101", employees=3)
    # $38 + 3 x $1.47 = $42.41; $38 + 3 x $2.82 = $46.46
    assert r.san_diego_example.estimated_total_usd == [42.41, 46.46]
    assert "City of San Diego business tax certificate" in " ".join(r.notes)


# ---------------------------------------------------------------- MCP: the new parameters and errors
def test_new_parameters_and_errors_through_an_mcp_client():
    mcp = FastMCP("t")
    acc.register(mcp)

    async def run():
        async with Client(mcp) as c:
            ok = {
                "field": await c.call_tool("accessibility_field_check", {
                    "parking_surface_slope": "2.5%", "parking_vertical_clearance": 90,
                    "existing_parking_structure": True, "parking_sign_bottom_height": "70 in",
                    "sign_in_circulation_path": True}),
                "trig": await c.call_tool("accessibility_triggers", {
                    "project_type": "remodel", "building_use": "apartments", "dwelling_units": 10,
                    "has_elevator": True, "first_occupied_date": "1991-06-01",
                    "last_building_permit_date": "1990-05-01", "as_of": AS_OF}),
                "park": await c.call_tool("accessible_parking_requirements", {
                    "facilities": [{"total_spaces": 60, "bus_truck_delivery_spaces": 10, "ev_charging_spaces": 30}]}),
            }
            bad = [await c.call_tool(n, a, raise_on_error=False) for n, a in [
                ("accessibility_field_check", {"ramp_running_slope": 0.0833}),
                ("accessibility_field_check", {"parking_stall_width": 108}),
                ("accessible_parking_requirements", {"total_spaces": 25, "use": "rehab"}),
                ("path_of_travel_obligation", {"construction_cost_usd": 100000,
                                               "primary_function_alteration_cost_usd": 500000}),
            ]]
            return {k: v.structured_content for k, v in ok.items()}, bad
    ok, bad = asyncio.run(run())
    results = {c["item"]: c["result"] for c in ok["field"]["checks"]}
    assert results == {"Parking space / aisle slope": "fail", "Parking vertical clearance": "pass",
                       "Parking sign height": "fail"}
    fha = next(r for r in ok["trig"]["regimes"] if r["code"].startswith("Fair Housing"))
    assert fha["applies"] is False and ok["trig"]["fha_covered_units"] is None
    assert ok["park"]["facilities"][0]["counted_spaces"] == 20 and ok["park"]["total_required_accessible"] == 1
    assert ok["park"]["facilities"][0]["accessible_evcs"]["van_accessible"] == 1    # 30 EV: 26-50 row -> 1/1/1
    assert all(b.is_error for b in bad)
    texts = [b.content[0].text for b in bad]
    assert "ambiguous" in texts[0] and "does not look like feet" in texts[1] and "ambiguous" in texts[2]
    assert "cannot be more than this job's cost" in texts[3]
