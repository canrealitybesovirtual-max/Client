"""tradecomply.building: permits, Title 24 energy, CALGreen/stormwater and ADU rules. Expected values are hand-computed
from the rulebook parameters (shown in the comments)."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import building as b
from tradecomply import core

AS_OF = "2026-11-25"


def ids(res):
    return {c.rule_id for c in res.citations}


def item(res, name):
    return next(li for li in res.line_items if li.item.startswith(name))


# ---------------------------------------------------------------- permit_required: exemptions
def test_shed_96_sqft_is_exempt_but_a_3500_job_still_needs_a_license():
    r = b.permit_required("8x12 storage shed, no power", "detached_accessory_structure", valuation_usd=3500, sq_ft=96,
                          is_one_story_detached_accessory=True, as_of=AS_OF)
    assert r.determination == "not_required" and r.building_permit_required is False
    assert r.code_section == "CRC R105.2" and r.code_edition == 2025
    assert r.license_required is True and r.minor_work_exemption_available is False   # $3,500 is not under $1,000
    assert r.state_fees_usd == {} and r.separate_trade_permits_likely == []
    assert r.verified is True and b.EXEMPT in ids(r)
    assert "96 <= 120 sq ft" in item(r, "Accessory structure").math


def test_shed_121_sqft_needs_permit_and_state_fees():
    r = b.permit_required("11x11 shed", "detached_accessory_structure", valuation_usd=6000, sq_ft=121,
                          is_one_story_detached_accessory=True, as_of=AS_OF)
    assert r.determination == "required" and r.license_required is True
    # SMIP Category 1: 6,000 x 0.00013 = 0.78 (> $0.50 minimum); BSASRF: ceil(6,000 / 25,000) = 1 x $1 = $1.00
    assert r.state_fees_usd == {"smip_seismic": 0.78, "bsasrf_building_standards": 1.0}
    assert r.state_fees_total_usd == 1.78
    assert r.smoke_alarms_required is None          # a new shed is not an alteration of the dwelling
    assert b.WC_DECL in r.unverified_rule_ids and r.verified is False


def test_shed_missing_size_or_two_story():
    r = b.permit_required("shed", "detached_accessory_structure", valuation_usd=900, as_of=AS_OF)
    assert r.determination == "need_more_info" and r.building_permit_required is None
    assert r.license_required is None                # depends on whether a permit is needed
    r2 = b.permit_required("two-story playhouse", "detached_accessory_structure", sq_ft=100,
                           is_one_story_detached_accessory=False, as_of=AS_OF)
    assert r2.determination == "required"


def test_fence_height_and_pool_barrier():
    assert b.permit_required("fence", "fence", fence_height_ft=7, as_of=AS_OF).determination == "not_required"  # 7 <= 7
    assert b.permit_required("fence", "fence", fence_height_ft=7.5, as_of=AS_OF).determination == "required"    # 7.5 > 7
    pool = b.permit_required("5 ft pool fence", "fence", fence_height_ft=5, is_pool_barrier=True, as_of=AS_OF)
    assert pool.determination == "required" and "swimming pool barriers" in pool.basis


def test_retaining_wall_height_surcharge_and_engineer():
    ok = b.permit_required("block wall", "retaining_wall", retaining_wall_height_ft=4, as_of=AS_OF)
    assert ok.determination == "not_required" and ok.licensed_professionals == []
    tall = b.permit_required("block wall", "retaining_wall", retaining_wall_height_ft=4.5, as_of=AS_OF)
    assert tall.determination == "required" and any("engineer" in p for p in tall.licensed_professionals)
    sur = b.permit_required("wall below driveway", "retaining_wall", retaining_wall_height_ft=3,
                            retaining_wall_supports_surcharge=True, as_of=AS_OF)
    assert sur.determination == "required" and "surcharge" in sur.basis
    liq = b.permit_required("tank berm wall", "retaining_wall", retaining_wall_height_ft=2,
                            retaining_wall_impounds_flammable_liquids=True, as_of=AS_OF)
    assert liq.determination == "required"


def test_finish_work_sidewalk_deck_and_playground_use_flagged_model_code():
    tile = b.permit_required("retile bathroom floor", "finish_work", valuation_usd=4000, as_of=AS_OF)
    assert tile.determination == "not_required" and tile.verified is False
    assert tile.unverified_sources[0]["key"] == "cbc-105.2-finish-work"
    moved = b.permit_required("remodel moving the shower drain", "finish_work", affects_structure_egress_or_utilities=True,
                              as_of=AS_OF)
    assert moved.determination == "required" and "plumbing" in moved.separate_trade_permits_likely
    walk = b.permit_required("new concrete driveway", "sidewalk_or_driveway", as_of=AS_OF)
    assert walk.determination == "not_required" and item(walk, "Sidewalk").value == "at grade (assumed)"
    raised = b.permit_required("raised walk", "sidewalk_or_driveway", height_above_grade_in=36, as_of=AS_OF)
    assert raised.determination == "required"                                         # 36 > 30 in
    deck = b.permit_required("detached deck", "deck", sq_ft=180, height_above_grade_in=24, as_of=AS_OF)
    assert deck.determination == "not_required" and deck.verified is False           # 180 <= 200, 24 <= 30
    assert b.permit_required("deck", "deck", sq_ft=250, height_above_grade_in=24, as_of=AS_OF).determination == "required"
    assert b.permit_required("deck", "deck", sq_ft=150, height_above_grade_in=20, deck_attached_or_serves_exit_door=True,
                             as_of=AS_OF).determination == "required"
    assert b.permit_required("patio deck", "deck", project_type="nonresidential", sq_ft=150, height_above_grade_in=20,
                             as_of=AS_OF).determination == "required"
    assert b.permit_required("swing set", "playground_equipment", as_of=AS_OF).determination == "not_required"


# ---------------------------------------------------------------- permit_required: B&P 7048 minor work
def test_minor_work_under_1000_no_permit_no_helpers():
    r = b.permit_required("6 ft wood fence repair", "fence", fence_height_ft=6, valuation_usd=950, as_of=AS_OF)
    assert r.license_required is False and r.minor_work_exemption_available is True
    assert b.MINOR_ADS in ids(r) and r.verified is True
    assert "$950.00 < $1,000.00" in item(r, "Minor work").math
    exact = b.permit_required("fence", "fence", fence_height_ft=6, valuation_usd=1000, as_of=AS_OF)
    assert exact.license_required is True                                              # 1,000 is not < 1,000
    crew = b.permit_required("fence", "fence", fence_height_ft=6, valuation_usd=950,
                             performer_advertises_or_has_employees=True, as_of=AS_OF)
    assert crew.license_required is True and "employs helpers" in crew.license_basis
    split = b.permit_required("fence", "fence", fence_height_ft=6, valuation_usd=600, part_of_larger_project=True,
                              as_of=AS_OF)
    assert split.license_required is True and b.MINOR_SPLIT in ids(split)


def test_permitted_work_needs_license_at_any_price():
    r = b.permit_required("replace water heater", "water_heater", valuation_usd=900, as_of=AS_OF)
    assert r.determination == "required" and r.license_required is True
    assert "plumbing" in r.separate_trade_permits_likely
    keys = {u["key"] for u in r.unverified_sources}
    assert {"cbc-105.1-mep-permits", "hsc-19211-water-heater-strapping"} <= keys
    assert any("willful" in n for n in r.notes) and b.DISCIPLINE in ids(r)
    assert r.smoke_alarms_required is False                                           # 900 <= 1,000


def test_equipment_repair_under_500_carve_out():
    r = b.permit_required("replace AC capacitor", "equipment_repair_or_identical_part", valuation_usd=350, as_of=AS_OF)
    assert r.determination == "check_with_building_department" and r.building_permit_required is None
    assert "$350.00 < $500.00" in item(r, "B&P 7090").math
    assert r.license_required is None


# ---------------------------------------------------------------- permit_required: alarms, fixtures, lead, fees
def test_kitchen_remodel_in_1965_house():
    r = b.permit_required("Kitchen remodel, move sink, new gas range", "alteration", valuation_usd=45000,
                          year_built=1965, has_fuel_burning_appliance_or_attached_garage=True, as_of=AS_OF)
    assert r.determination == "required"
    assert r.smoke_alarms_required is True and r.co_alarms_required is True          # 45,000 > 1,000
    assert r.plumbing_fixture_retrofit_required is True                              # 1965 < 1994, single-family
    assert r.lead_rrp_applies is True                                                # 1965 < 1978
    # SMIP: 45,000 x 0.00013 = 5.85; BSASRF: ceil(45,000 / 25,000) = 2 -> $2.00; total 7.85
    assert r.state_fees_usd == {"smip_seismic": 5.85, "bsasrf_building_standards": 2.0}
    assert r.state_fees_total_usd == 7.85
    assert set(r.separate_trade_permits_likely) >= {"plumbing", "gas"}
    assert {b.ALARMS, b.FIXTURES} <= set(r.unverified_rule_ids) and r.verified is False
    assert any("EPA-certified renovator" in p for p in r.licensed_professionals)


def test_alarm_trigger_is_strictly_over_1000():
    at = b.permit_required("bath fan", "alteration", valuation_usd=1000, as_of=AS_OF)
    over = b.permit_required("bath fan", "alteration", valuation_usd=1000.01, as_of=AS_OF)
    assert at.smoke_alarms_required is False and over.smoke_alarms_required is True
    assert over.co_alarms_required is None                                         # appliance/garage unknown
    elec = b.permit_required("panel upgrade", "electrical", valuation_usd=3200,
                             has_fuel_burning_appliance_or_attached_garage=False, as_of=AS_OF)
    assert elec.smoke_alarms_required is True and elec.co_alarms_required is False


def test_commercial_ti_pre_1994_fixture_trigger_and_category_2_smip():
    r = b.permit_required("office tenant improvement", "alteration", project_type="nonresidential",
                          valuation_usd=180000, year_built=1988, stories=2, as_of=AS_OF)
    assert r.plumbing_fixture_retrofit_required is True                              # 180,000 > 150,000
    assert r.smoke_alarms_required is None                                           # not a dwelling
    # SMIP Category 2: 180,000 x 0.00028 = 50.40; BSASRF: ceil(7.2) = 8 -> $8.00; total 58.40
    assert r.state_fees_usd == {"smip_seismic": 50.4, "bsasrf_building_standards": 8.0}
    assert r.state_fees_total_usd == 58.4
    assert r.code_section == "CBC 105.2"
    at = b.permit_required("TI", "alteration", project_type="nonresidential", valuation_usd=150000, year_built=1988,
                           as_of=AS_OF)
    assert at.plumbing_fixture_retrofit_required is None                             # 150,000 is not > 150,000
    newer = b.permit_required("TI", "alteration", project_type="nonresidential", valuation_usd=400000, year_built=1995,
                              as_of=AS_OF)
    assert newer.plumbing_fixture_retrofit_required is False


def test_smip_category_by_stories_and_minimum_fees():
    low = b.permit_required("3-story apartments", "new_building", project_type="multifamily_low_rise", stories=3,
                            valuation_usd=1_000_000, as_of=AS_OF)
    # residential 1-3 stories -> Category 1: 1,000,000 x 0.00013 = 130.00; BSASRF 1,000,000 / 25,000 = 40 -> $40
    assert low.state_fees_usd == {"smip_seismic": 130.0, "bsasrf_building_standards": 40.0}
    high = b.permit_required("5-story apartments", "new_building", project_type="multifamily_high_rise", stories=5,
                             valuation_usd=2_000_000, as_of=AS_OF)
    # Category 2: 2,000,000 x 0.00028 = 560.00; BSASRF 80 -> $80
    assert high.state_fees_usd == {"smip_seismic": 560.0, "bsasrf_building_standards": 80.0}
    tiny = b.permit_required("reroof garage", "reroof", valuation_usd=3000, as_of=AS_OF)
    # 3,000 x 0.00013 = 0.39 -> $0.50 minimum; BSASRF ceil(0.12) = 1 -> $1.00
    assert tiny.state_fees_usd == {"smip_seismic": 0.5, "bsasrf_building_standards": 1.0}


def test_residential_addition_school_fee_500_sqft_line():
    small = b.permit_required("500 sq ft family room", "addition", valuation_usd=90000, sq_ft=500, as_of=AS_OF)
    assert small.school_fee_estimate_usd == 0.0                                      # 500 is not > 500
    big = b.permit_required("501 sq ft addition", "addition", valuation_usd=90000, sq_ft=501, as_of=AS_OF)
    assert big.school_fee_estimate_usd == 2695.38                                    # 501 x $5.38 = 2,695.38
    store = b.permit_required("new retail building", "new_building", project_type="nonresidential", sq_ft=10000,
                              valuation_usd=2_500_000, as_of=AS_OF)
    assert store.school_fee_estimate_usd == 8700.0                                   # 10,000 x $0.87


# ---------------------------------------------------------------- permit_required: owner-builder, demo, pool, edition
def test_owner_builder_addition_for_sale():
    r = b.permit_required("650 sq ft bedroom addition", "addition", valuation_usd=120000, sq_ft=650, owner_builder=True,
                          owner_intends_to_sell=True, as_of=AS_OF)
    assert r.license_required is True and "7044(a)(1)" in r.license_basis
    assert any("presumed" in w for w in r.owner_builder_warnings)
    assert {b.OB_SALE, b.OB_SELF, b.OB_DUTIES, b.OB_CONSULT} <= ids(r)
    assert r.school_fee_estimate_usd == 3497.0                                       # 650 x $5.38
    # SMIP 120,000 x 0.00013 = 15.60; BSASRF ceil(4.8) = 5 -> $5.00
    assert r.state_fees_usd == {"smip_seismic": 15.6, "bsasrf_building_standards": 5.0}
    assert not any("home improvement contract" in s.task for s in r.checklist)       # owner self-performs


def test_owner_builder_licensed_trades_and_unlicensed_helpers():
    trades = b.permit_required("garage conversion", "alteration", valuation_usd=60000, owner_builder=True,
                               owner_builder_work_by="licensed_contractors", as_of=AS_OF)
    assert trades.license_required is False and any("no more than 4" in w for w in trades.owner_builder_warnings)
    helpers = b.permit_required("garage conversion", "alteration", valuation_usd=60000, owner_builder=True,
                                owner_builder_work_by="unlicensed_workers_paid_by_job", as_of=AS_OF)
    assert helpers.license_required is True


def test_demolition_and_pool_steps():
    demo = b.permit_required("demolish detached garage", "demolition", valuation_usd=8000, as_of=AS_OF)
    tasks = {s.rule_id: s for s in demo.checklist}
    assert "10 working days" in tasks[b.AIR_DISTRICT].when and b.DEMO_NOTICE in tasks
    pool = b.permit_required("new gunite pool", "pool_or_spa", valuation_usd=75000, as_of=AS_OF)
    assert any("at least 2 of the 7" in s.task for s in pool.checklist)


def test_code_edition_follows_application_date():
    r = b.permit_required("addition", "addition", valuation_usd=50000, permit_application_date="2025-12-15", as_of=AS_OF)
    assert r.code_edition == 2022
    assert b.permit_required("addition", "addition", valuation_usd=50000, as_of=AS_OF).code_edition == 2025


def test_bad_input():
    with pytest.raises(ValueError):
        b.permit_required("fence", "fence", fence_height_ft=-1)
    with pytest.raises(ValueError):
        b.permit_required("fence", "fence", fence_height_ft=6, as_of="11/25/2026")


# ---------------------------------------------------------------- energy_compliance_triggers
def test_new_single_family_with_battery():
    r = b.energy_compliance_triggers("single_family", "new_construction", climate_zone=10, electrical_service_amps=200,
                                     battery_storage_installed=True, pv_kwdc_required=4.0, gas_appliances_planned=True,
                                     as_of=AS_OF)
    assert r.energy_code_edition == 2025 and r.solar_pv_required is True
    assert r.pv_kwdc_after_battery_credit == 3.0                                     # 4.0 x (1 - 25%) = 3.0
    assert r.battery_ready_required is False                                         # waived: battery installed
    assert r.electric_ready_required is True
    assert [d.form for d in r.documents] == ["CF1R", "CF2R", "CF3R"] and r.hers_rater_needed is True
    assert {h.measure for h in r.hers_verifications} == set(core.rule(b.CF3R).param("typical_measures"))
    assert b.HVAC not in ids(r)


def test_battery_ready_strictly_over_125_amps():
    over = b.energy_compliance_triggers("single_family", "new_construction", electrical_service_amps=200, as_of=AS_OF)
    at = b.energy_compliance_triggers("single_family", "new_construction", electrical_service_amps=125, as_of=AS_OF)
    unknown = b.energy_compliance_triggers("single_family", "new_construction", as_of=AS_OF)
    assert over.battery_ready_required is True and at.battery_ready_required is False
    assert unknown.battery_ready_required is None and over.pv_kwdc_after_battery_credit is None
    sub = b.energy_compliance_triggers("single_family", "new_construction", homes_in_subdivision=12, as_of=AS_OF)
    assert "12 >= 10" in item(sub, "Solar-ready").math


def test_hvac_changeout_hers():
    r = b.energy_compliance_triggers("single_family", "hvac_changeout", climate_zone=12, as_of=AS_OF)
    st = {h.measure: h.status for h in r.hers_verifications}
    assert st == {"refrigerant charge": "likely", "airflow": "likely", "fan efficacy": "likely",
                  "duct leakage": "possible"}
    assert r.documents[0].form == "CF1R-ALT" and r.hers_rater_needed is True
    assert r.verified is False and b.HVAC in r.unverified_rule_ids


def test_duct_length_and_new_duct_system_tests():
    long_ = b.energy_compliance_triggers("single_family", "duct_alteration", ducts_added_or_replaced_ft=30,
                                         percent_of_duct_system_replaced=80, as_of=AS_OF)
    # 30 > 25 ft (2025 form) but <= 40 ft (2019 form); 80% >= 75% -> entirely new
    assert {h.measure: h.status for h in long_.hers_verifications} == {"duct leakage": "likely"}
    assert "30 > 25 ft" in item(long_, "Duct length").math and "<= 40 ft" in item(long_, "Duct length").math
    assert long_.duct_system_treated_as == "entirely_new"
    short = b.energy_compliance_triggers("single_family", "duct_alteration", ducts_added_or_replaced_ft=20,
                                         percent_of_duct_system_replaced=60, as_of=AS_OF)
    assert short.hers_verifications[0].status == "possible" and short.duct_system_treated_as == "altered"
    with pytest.raises(ValueError):
        b.energy_compliance_triggers("single_family", "duct_alteration", percent_of_duct_system_replaced=120)


def test_energy_edition_nonresidential_and_water_heater():
    old = b.energy_compliance_triggers("single_family", "addition", permit_application_date="2025-11-01", as_of=AS_OF)
    assert old.energy_code_edition == 2022
    nr = b.energy_compliance_triggers("nonresidential", "new_construction", as_of=AS_OF)
    assert [d.form for d in nr.documents] == ["NRCC", "NRCI"] and nr.hers_verifications == []
    assert nr.hers_rater_needed is None
    wh = b.energy_compliance_triggers("single_family", "water_heater", as_of=AS_OF)
    assert wh.hers_rater_needed is None and wh.unverified_sources[0]["key"] == "hsc-19211-water-heater-strapping"


# ---------------------------------------------------------------- calgreen_and_stormwater
def test_new_home_waste_math_ev_and_cgp():
    r = b.calgreen_and_stormwater("single_family", "new_construction", sq_ft=2400, disturbed_acres=1.2,
                                  has_attached_private_garage=True, cd_waste_generated_tons=12,
                                  cd_waste_diverted_tons=7, landfill_disposal_lbs=7000, as_of=AS_OF)
    assert r.calgreen_mandatory_measures_apply is True and r.diversion_min_percent == 65
    assert r.diversion_achieved_percent == 58.33                                    # 7 / 12 x 100 = 58.33
    assert r.diversion_meets_minimum is False and r.diversion_shortfall == 0.8      # 12 x 65% = 7.8; 7.8 - 7 = 0.8
    assert r.low_disposal_actual_lbs_per_sqft == 2.92 and r.low_disposal_meets_cap is True   # 7,000 / 2,400 <= 3.4
    assert r.waste_requirement_met is True                                          # through the low-disposal path
    assert r.ev_capable_required is True
    assert r.construction_general_permit_required is True and r.erosivity_waiver_possible is None
    assert any("QSD" in p for p in r.licensed_professionals)
    assert {b.CGP_NOI, b.CGP_QSD, b.CGP_QPE, b.CGP_NOT} <= ids(r)


def test_diversion_passes_at_70_percent():
    r = b.calgreen_and_stormwater("single_family", "new_construction", cd_waste_generated_tons=20,
                                  cd_waste_diverted_tons=14, has_attached_private_garage=False, as_of=AS_OF)
    assert r.diversion_achieved_percent == 70.0 and r.diversion_meets_minimum is True   # 14 / 20 = 70%
    assert r.diversion_shortfall == 0.0 and r.ev_capable_required is False


def test_nonresidential_addition_and_alteration_triggers():
    alt = b.calgreen_and_stormwater("nonresidential", "alteration", valuation_usd=250000, as_of=AS_OF)
    assert alt.calgreen_mandatory_measures_apply is True and alt.waste_diversion_required is True   # >= 200,000
    small = b.calgreen_and_stormwater("nonresidential", "alteration", valuation_usd=199999, as_of=AS_OF)
    assert small.calgreen_mandatory_measures_apply is False and small.waste_diversion_required is True
    assert b.calgreen_and_stormwater("nonresidential", "addition", sq_ft=1000,
                                     as_of=AS_OF).calgreen_mandatory_measures_apply is True          # 1,000 >= 1,000
    assert b.calgreen_and_stormwater("nonresidential", "addition", sq_ft=999,
                                     as_of=AS_OF).calgreen_mandatory_measures_apply is False
    assert small.low_disposal_max_lbs_per_sqft == 2.0
    hr = b.calgreen_and_stormwater("multifamily_high_rise", "new_construction", as_of=AS_OF)
    assert hr.low_disposal_max_lbs_per_sqft == 2.0 and hr.ev_capable_required is None


def test_residential_alteration_scope_is_flagged_unverified():
    yes = b.calgreen_and_stormwater("single_family", "addition", increases_conditioned_area_volume_or_size=True,
                                    as_of=AS_OF)
    assert yes.calgreen_mandatory_measures_apply is True and yes.verified is False
    no = b.calgreen_and_stormwater("single_family", "alteration", increases_conditioned_area_volume_or_size=False,
                                   as_of=AS_OF)
    assert no.calgreen_mandatory_measures_apply is False and no.waste_diversion_required is False


def test_construction_general_permit_thresholds_and_erosivity_waiver():
    def cgp(**kw):
        return b.calgreen_and_stormwater("nonresidential", "new_construction", as_of=AS_OF, **kw)
    assert cgp(disturbed_acres=0.99).construction_general_permit_required is False
    assert cgp(disturbed_acres=0.99, part_of_larger_common_plan=True).construction_general_permit_required is True
    assert cgp(disturbed_acres=1.0).construction_general_permit_required is True                    # 1.0 >= 1
    w = cgp(disturbed_acres=3, rainfall_erosivity_r_factor=3.2)
    assert w.erosivity_waiver_possible is True and b.CGP_NOI not in ids(w)                        # 3 < 5, 3.2 < 5
    assert cgp(disturbed_acres=3, rainfall_erosivity_r_factor=5).erosivity_waiver_possible is False  # 5 is not < 5
    big = cgp(disturbed_acres=6, rainfall_erosivity_r_factor=2)
    assert big.erosivity_waiver_possible is False and "large construction" in big.cgp_basis
    with pytest.raises(ValueError):
        cgp(cd_waste_generated_tons=5, cd_waste_diverted_tons=6)


# ---------------------------------------------------------------- adu_rules
def test_detached_750_sqft_adu_fees_and_clocks():
    r = b.adu_rules("detached", 750, bedrooms=1, primary_dwelling_sq_ft=1600, impact_fee_for_primary_dwelling_usd=12000,
                    application_submitted_date="2026-03-02", application_complete_date="2026-03-20", as_of=AS_OF)
    assert r.size_status == "protected_800_sqft" and r.city_max_size_cannot_be_below_sqft == 850
    assert r.state_default_max_sqft == 1200 and r.min_height_city_must_allow_ft == 16
    assert r.impact_fee_exempt is True and r.impact_fee_estimate_usd == 0.0         # 750 <= 750
    assert r.school_fee_exempt is False and r.school_fee_estimate_usd == 4035.0     # 750 x $5.38
    # Mon 2026-03-02 + 15 weekdays = Mon 2026-03-23; complete 2026-03-20 + 60 days = 2026-05-19
    assert r.completeness_deadline == "2026-03-23" and r.approval_deadline == "2026-05-19"
    assert r.approval_deadline_basis.startswith("from the date")


def test_detached_1000_sqft_2br_near_transit():
    r = b.adu_rules("detached", 1000, bedrooms=2, primary_dwelling_sq_ft=2000, near_major_transit=True,
                    impact_fee_for_primary_dwelling_usd=15000, application_submitted_date="2026-03-02", as_of=AS_OF)
    assert r.size_status == "protected_size_floor" and r.city_max_size_cannot_be_below_sqft == 1000
    assert r.min_height_city_must_allow_ft == 18
    assert r.impact_fee_estimate_usd == 7500.0                                       # 15,000 x 1,000 / 2,000
    assert r.school_fee_estimate_usd == 5380.0                                       # 1,000 x $5.38
    assert r.approval_deadline == "2026-05-22"                                       # latest case: 03-23 + 60 days
    assert r.approval_deadline_basis.startswith("latest case")


def test_attached_caps_and_heights():
    r = b.adu_rules("attached", 900, bedrooms=1, primary_dwelling_sq_ft=1500, primary_zone_height_limit_ft=30,
                    as_of=AS_OF)
    # 1,500 x 50% = 750, floor 850 -> cap 850; 900 > 850
    assert r.state_default_max_sqft == 850 and r.size_status == "exceeds_state_default_max"
    assert r.min_height_city_must_allow_ft == 25                                     # min(25, 30)
    low = b.adu_rules("attached", 900, primary_dwelling_sq_ft=2000, primary_zone_height_limit_ft=20, as_of=AS_OF)
    assert low.size_status == "within_state_default_max" and low.min_height_city_must_allow_ft == 20   # 900 <= 1,000
    assert b.adu_rules("detached", 1300, bedrooms=2, as_of=AS_OF).size_status == "exceeds_state_default_max"
    assert b.adu_rules("attached", 900, as_of=AS_OF).size_status == "depends_on_local_ordinance"
    ok = b.adu_rules("detached", 600, proposed_height_ft=17, proposed_side_rear_setback_ft=3, as_of=AS_OF)
    assert "needs local approval" in item(ok, "Proposed height").math                # 17 > 16
    assert "city may require up to 4 ft" in item(ok, "Side/rear setback").math       # 3 < 4


def test_jadu_rules():
    j = b.adu_rules("jadu", 450, jadu_shares_bathroom=True, as_of=AS_OF)
    assert j.owner_occupancy_required is True and j.state_default_max_sqft == 500
    assert j.impact_fee_exempt is True and j.school_fee_exempt is True               # 450 <= 500; 450 < 500
    assert j.size_status == "within_jadu_max"
    assert b.adu_rules("jadu", 520, as_of=AS_OF).size_status == "exceeds_jadu_max"
    assert b.adu_rules("jadu", 400, jadu_shares_bathroom=False, as_of=AS_OF).owner_occupancy_required is False


def test_school_fee_edge_district_rate_and_prior_cap():
    assert b.adu_rules("detached", 499, as_of=AS_OF).school_fee_exempt is True       # 499 < 500
    r = b.adu_rules("detached", 500, as_of=AS_OF)
    assert r.school_fee_exempt is False and r.school_fee_estimate_usd == 2690.0     # 500 x $5.38
    dist = b.adu_rules("detached", 750, school_district_rate_usd_per_sqft=4.79, as_of=AS_OF)
    assert dist.school_fee_estimate_usd == 3592.5                                   # 750 x $4.79
    early = b.adu_rules("detached", 750, as_of="2026-01-15")
    assert early.school_fee_estimate_usd == 3877.5                                  # before 2026-01-28: 750 x $5.17


def test_adu_contract_down_payment_and_cancel_period():
    big = b.adu_rules("detached", 800, contract_price_usd=180000, as_of=AS_OF)
    assert big.max_down_payment_usd == 1000.0 and big.cancellation_business_days == 3   # min(1,000, 18,000)
    small = b.adu_rules("conversion", 400, contract_price_usd=8000, buyer_65_or_older=True, is_manufactured_home=False,
                        as_of=AS_OF)
    assert small.max_down_payment_usd == 800.0 and small.cancellation_business_days == 5   # 10% x 8,000
    assert small.size_status == "conversion_no_state_size_cap" and small.max_side_rear_setback_city_may_require_ft is None
    mh = b.adu_rules("detached", 800, contract_price_usd=150000, is_manufactured_home=True, as_of=AS_OF)
    assert any("C-47" in p for p in mh.licensed_professionals) and b.ADU_ISSUER in ids(mh)
    tolled = b.adu_rules("detached", 800, submitted_with_new_primary_dwelling=True, as_of=AS_OF)
    assert tolled.approval_clock_may_be_tolled is True


def test_every_cited_rule_exists_and_unverified_entries_have_sources():
    for key, u in b.UNVERIFIED.items():
        assert u["source"].startswith("https://") and u["citation"]
    r = b.adu_rules("detached", 450, as_of=AS_OF)           # < 500 sq ft: no school fee, so only verified rules
    assert all(core.rule(c.rule_id) for c in r.citations) and r.verified is True
    assert b.adu_rules("detached", 600, as_of=AS_OF).verified is False   # school certificate rule is unchecked


# ---------------------------------------------------------------- MCP wiring
def test_mcp_tools_round_trip():
    mcp = FastMCP("t")
    b.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            permit = await c.call_tool("permit_required", {
                "work_description": "11x11 shed", "structure_type": "detached_accessory_structure", "sq_ft": 121,
                "is_one_story_detached_accessory": True, "valuation_usd": 6000, "as_of": AS_OF})
            energy = await c.call_tool("energy_compliance_triggers", {
                "project_type": "single_family", "scope": "new_construction", "battery_storage_installed": True,
                "pv_kwdc_required": 4.0, "as_of": AS_OF})
            green = await c.call_tool("calgreen_and_stormwater", {
                "project_type": "single_family", "cd_waste_generated_tons": 20, "cd_waste_diverted_tons": 14,
                "disturbed_acres": 1.0, "as_of": AS_OF})
            adu = await c.call_tool("adu_rules", {"adu_type": "detached", "sq_ft": 1000, "bedrooms": 2,
                                                  "primary_dwelling_sq_ft": 2000,
                                                  "impact_fee_for_primary_dwelling_usd": 15000, "as_of": AS_OF})
            return (names, permit.structured_content, energy.structured_content, green.structured_content,
                    adu.structured_content)

    names, permit, energy, green, adu = asyncio.run(run())
    assert {"permit_required", "energy_compliance_triggers", "calgreen_and_stormwater", "adu_rules"} <= names
    assert permit["determination"] == "required" and permit["state_fees_total_usd"] == 1.78   # same as the shed test
    assert energy["pv_kwdc_after_battery_credit"] == 3.0
    assert green["diversion_achieved_percent"] == 70.0 and green["construction_general_permit_required"] is True
    assert adu["impact_fee_estimate_usd"] == 7500.0 and adu["school_fee_estimate_usd"] == 5380.0
