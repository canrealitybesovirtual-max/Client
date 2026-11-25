"""Jobsite safety module: wildfire smoke, overhead lines, confined spaces, Valley Fever and the hazard checklist.

Expected values are worked by hand from the rule text in research/web_calosha-construction-2.verified.json (the
arithmetic is in the comments), never read back from the module.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, jobsite_safety as js

AS_OF = "2026-11-25"           # Wednesday


def ids(result):
    return {c.rule_id for c in result.citations}


def req(result, prefix):
    return next(r for r in result.requirements if r.item.startswith(prefix))


def group(result, key):
    return next(g for g in result.groups if g.key == key)


# ---------------------------------------------------------------- rulebook wiring
def test_module_rules_exist_and_statuses_match_the_verified_pack():
    book = {r.id: r for r in core.all_rules()}
    assert set(js.RULE_IDS) <= set(book)
    pack = {r.id for r in book.values() if r.pack == "web_calosha-construction-2"}
    assert set(js.RULE_IDS) == pack                            # every one of the pack's rules is used somewhere
    assert book[js.VF_MAY1].status == "unverifiable"           # the only unverifiable rule in the pack
    assert book[js.NOISE_TABLE].status == "corrected" and book[js.NOISE_TABLE].verified
    assert all(book[i].verified for i in js.RULE_IDS if i != js.VF_MAY1)


# ---------------------------------------------------------------- wildfire smoke (8 CCR 5141.1)
def test_smoke_aqi_175_for_six_hours_needs_voluntary_respirators():
    # 175 >= 151 (5141.1(a)(1)(A)) and <= 500 -> (f)(3)(A) voluntary respirators; 6 h > 1 h (a)(2)(D) -> covered.
    r = js.wildfire_smoke_requirements(175, 6)
    assert r.covered and r.level == "151_to_500" and r.respirators == "voluntary"
    assert r.aqi_category == "Unhealthy"                       # 151-200 row of the 5141.1(b) table
    assert r.exemption is None and r.min_assigned_protection_factor is None
    assert "6 h at AQI 151+ this shift > 1 h -> no hours exemption" in r.math
    assert {js.SMOKE_SCOPE, js.SMOKE_EXEMPT, js.SMOKE_CHECK, js.SMOKE_COMM, js.SMOKE_TRAIN, js.SMOKE_CONTROLS,
            js.SMOKE_VOL, js.SMOKE_MED} <= ids(r)
    assert js.SMOKE_MAND not in ids(r)
    vol = req(r, "AQI 151-500")
    assert "N95" in vol.detail and "No fit test" in vol.detail and "bandannas" in vol.detail
    assert "start of each shift" in req(r, "Check the AQI").detail and "AirNow" in req(r, "Check the AQI").detail
    assert r.verified is True and r.unverified_rule_ids == []
    assert r.next_step.startswith("Before the shift: hand every worker a respirator")


def test_smoke_boundaries_151_and_500():
    assert js.wildfire_smoke_requirements(150, 8).covered is False          # below 151
    assert js.wildfire_smoke_requirements(151, 8).respirators == "voluntary"  # "151 or greater"
    assert js.wildfire_smoke_requirements(500, 8).respirators == "voluntary"  # "does not exceed 500"
    assert js.wildfire_smoke_requirements(501, 8).respirators == "mandatory"  # "exceeds 500"
    low = js.wildfire_smoke_requirements(120)
    assert low.aqi_category == "Unhealthy for Sensitive Groups" and low.respirators == "not_required"
    assert low.next_step.startswith("No wildfire-smoke duties right now") and ids(low) >= {js.SMOKE_CHECK}


def test_smoke_over_500_minimum_apf_from_an_onsite_reading():
    # 600 ug/m3 > 500.4 (top of Appendix A) -> AQI over 500 -> mandatory under 5144.
    # APF >= 600 / 55.4 (top of the AQI 101-150 row) = 10.8303 -> rounded UP to 10.84.
    r = js.wildfire_smoke_requirements(pm25_ug_m3=600, hours_exposed=8)
    assert r.level == "over_500" and r.respirators == "mandatory" and r.aqi_basis == "onsite_pm25_measurement"
    assert r.min_assigned_protection_factor == 10.84 and r.apf_is_lower_bound is False
    assert any("600 ug/m3 outside / 55.4 ug/m3 allowed inside = 10.8303" in m for m in r.math)
    assert js.SMOKE_MAND in ids(r) and js.SMOKE_CONV in ids(r)
    assert "MANDATORY" in r.summary and "5144" in r.next_step


def test_smoke_aqi_500_plus_gives_a_lower_bound_apf():
    # "500+" = a reported (AirNow/EPA) AQI over 500. Since 2024-05-06, 40 CFR 58 App. G Table 2 puts AQI 500 at
    # 325.4 ug/m3, so PM2.5 > 325.4 -> APF > 325.4 / 55.4 = 5.8736 -> at least 5.88, a lower bound only.
    # (Appendix A's 500.4 ug/m3 is for converting on-site readings, not for a reported AQI.)
    r = js.wildfire_smoke_requirements("500+", 3)
    assert r.aqi_pm25 == 501 and r.aqi_range == "over 500" and r.respirators == "mandatory"
    assert r.min_assigned_protection_factor == 5.88 and r.apf_is_lower_bound is True
    assert js.SMOKE_EPA in ids(r) and js.RESP_APF in ids(r) and r.verified is True
    assert any("325.4 ug/m3 on EPA's current AQI scale" in m for m in r.math)
    assert "measure PM2.5 on site" in r.summary and "covers only up to 554 ug/m3" in r.next_step   # 10 x 55.4
    # Every type with APF >= 5.88 qualifies for the floor; the quarter mask (APF 5) does not.
    assert not any(o.startswith("air-purifying, quarter mask") for o in r.respirator_options)
    assert r.respirator_options[0].startswith("air-purifying, half mask (includes N95")
    assert js.wildfire_smoke_requirements("Beyond the AQI", 3).respirators == "mandatory"


def test_smoke_appendix_a_rows_and_the_gap_between_rows():
    r = js.wildfire_smoke_requirements(pm25_ug_m3=55.4, hours_exposed=8)       # top of 35.5-55.4 -> AQI 101-150
    assert r.covered is False and r.aqi_range == "101-150" and "measures" in r.exemption   # (a)(2)(C)
    r = js.wildfire_smoke_requirements(pm25_ug_m3=55.45, hours_exposed=8)      # between 55.4 and 55.5 -> higher row
    assert r.aqi_range == "151-200" and r.covered and any("higher row" in m for m in r.math)
    r = js.wildfire_smoke_requirements(pm25_ug_m3=150.5, hours_exposed=8)      # 150.5-250.4 -> AQI 201-300
    assert r.aqi_range == "201-300" and r.aqi_category == "Very Unhealthy"
    # A reported AQI and an on-site reading: the on-site reading governs (5141.1(c)).
    r = js.wildfire_smoke_requirements(220, 8, pm25_ug_m3=40)                  # 35.5-55.4 -> AQI 101-150
    assert r.covered is False and r.aqi_basis == "onsite_pm25_measurement" and any("on-site reading" in n
                                                                                    for n in r.notes)


def test_smoke_exemptions():
    # "for a total of one hour or less during a shift": exactly 1 h is exempt, 1.25 h is not.
    one = js.wildfire_smoke_requirements(180, 1)
    assert one.covered is False and "one hour" in one.exemption
    assert "1 h at AQI 151+ this shift <= 1 h -> exempt (the hours are a total for the shift)" in one.math
    assert js.wildfire_smoke_requirements(180, 1.25).covered is True
    cab = js.wildfire_smoke_requirements(220, 8, work_setting="excavator cab with cabin air filter, windows shut")
    assert cab.work_setting == "enclosed_vehicle_filtered" and "vehicles" in cab.exemption
    office = js.wildfire_smoke_requirements(220, 8, work_setting="inside the site office with HVAC filtration")
    assert office.work_setting == "enclosed_building_filtered" and "buildings" in office.exemption
    shell = js.wildfire_smoke_requirements(220, 8, work_setting="inside the unfinished building")
    assert shell.work_setting == "enclosed_unfiltered" and shell.covered
    assert "firefight" in js.wildfire_smoke_requirements(300, 8, wildland_firefighting=True).exemption
    dust = js.wildfire_smoke_requirements(300, 8, smoke_anticipated=False)
    assert dust.covered is False and "anticipated" in dust.summary


def test_smoke_emergency_operations_get_voluntary_respirators_even_above_500():
    # 5141.1(f)(4): comply with (f)(3)(A) at all AQI >= 151; (f)(1), (f)(2), (f)(3)(B) do not apply.
    r = js.wildfire_smoke_requirements(520, 5, emergency_operation=True)
    assert r.respirators == "voluntary" and r.level == "over_500"
    assert js.SMOKE_EMERG in ids(r) and js.SMOKE_MAND not in ids(r) and js.SMOKE_CONTROLS not in ids(r)
    assert any("post-fire debris removal" in n for n in r.notes)


def test_smoke_messy_inputs():
    assert js.wildfire_smoke_requirements("AQI 175", "6 hours").covered
    assert js.wildfire_smoke_requirements("175 (Unhealthy)", 6, work_setting="Outside").aqi_pm25 == 175
    assert js.wildfire_smoke_requirements(" pm2.5 aqi: 210 ", 6.0).aqi_category == "Very Unhealthy"
    assert js.wildfire_smoke_requirements(175.0, 6).aqi_pm25 == 175
    no_hours = js.wildfire_smoke_requirements(175)
    assert no_hours.covered and any("give hours_exposed" in n for n in no_hours.notes)


@pytest.mark.parametrize("kwargs,msg", [
    ({"aqi_pm25": -3}, "0 or more"),
    ({"aqi_pm25": "hazy"}, "must be a number"),
    ({"aqi_pm25": 150.5}, "whole number"),
    ({"aqi_pm25": True}, "must be a number"),
    ({}, "Give aqi_pm25"),
    ({"aqi_pm25": 175, "hours_exposed": 25}, "cannot be above 24"),
    ({"aqi_pm25": 175, "work_setting": "on the moon"}, "work_setting must be"),
    ({"pm25_ug_m3": -1}, "cannot be below 0"),
])
def test_smoke_rejects_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        js.wildfire_smoke_requirements(**kwargs)


# ---------------------------------------------------------------- overhead lines (8 CCR 2946-2949)
def test_crane_8_ft_from_a_12_47_kv_line_is_too_close():
    # 12.47 kV = 12,470 V > 600 V -> high voltage; Table 2 row "over 600 to 50,000 V" -> 10 ft (Table 1 would be 6).
    # 8 ft < 10 ft -> 2 ft short; 2948: notify the line operator before working inside the clearance.
    r = js.overhead_line_clearance(12.47, "Mobile Crane", 8)
    assert r.voltage_volts == 12470 and r.high_voltage and r.table_used == "Table 2"
    assert (r.table_1_clearance_ft, r.table_2_clearance_ft, r.required_clearance_ft) == (6, 10, 10)
    assert r.status == "too_close" and r.clearance_ok is False and r.shortfall_ft == 2
    assert r.warning_sign == "required" and "Within 10 Feet Of High-Voltage Lines of 50,000 Volts" in r.warning_sign_text
    assert "legible at 12 ft" in req(r, "High-voltage warning sign").detail
    assert {js.HV_T2, js.HV_NOTIFY, js.HV_SIGN, js.HV_GUARD, js.HV_ENERGIZED, js.HV_SPOTTER} <= ids(r)
    assert js.HV_T1 not in ids(r)
    assert r.next_step.startswith("Do not start. Call the line operator") and r.verified is True
    assert "Planned 8 ft < required 10 ft -> 2 ft too close" in r.math


def test_table_rows_at_their_boundaries():
    # Rows are "over X up to Y": exactly 50,000 V is still the first row.
    assert js.overhead_line_clearance(50, "crane").required_clearance_ft == 10
    assert js.overhead_line_clearance("50,001 V", "crane").required_clearance_ft == 11      # 50-75 kV row
    assert js.overhead_line_clearance(115, "crane").required_clearance_ft == 13             # 75-125 kV row
    assert js.overhead_line_clearance(500, "crane").required_clearance_ft == 27             # 370-550 kV row
    assert js.overhead_line_clearance(1000, "crane").required_clearance_ft == 42            # 550-1,000 kV row
    assert js.overhead_line_clearance(345, "worker").required_clearance_ft == 10            # Table 1 50-345 kV
    assert js.overhead_line_clearance(345.1, "worker").required_clearance_ft == 16          # Table 1 345-750 kV
    assert js.overhead_line_clearance("601 V", "ladder").required_clearance_ft == 6         # first Table 1 row


def test_people_and_tools_use_table_1_machines_with_booms_use_table_2():
    w = js.overhead_line_clearance(230, "roofer with hand tools", 12)
    # 230 kV: Table 1 row 50-345 kV -> 10 ft; Table 2 row 175-250 kV -> 17 ft; 12 >= 10 -> clear by 2.
    assert w.table_used == "Table 1" and w.required_clearance_ft == 10 and w.table_2_clearance_ft == 17
    assert w.status == "clear" and "clear by 2 ft" in w.math[-1] and w.spotter_required is None
    lift = js.overhead_line_clearance("69 kV", "bucket truck", 15)
    # 69 kV: Table 2 row 50-75 kV -> 11 ft. A bucket truck is boom-type equipment, and 2946(b)(2) says "Operation of
    # boom-type equipment shall conform to the minimum clearances set forth in Table 2": 11 ft is the legal minimum.
    assert lift.required_clearance_ft == 11 and lift.legal_minimum_clearance_ft == 11
    assert lift.table_1_clearance_ft == 10 and lift.status == "clear"
    assert "Operation of boom-type equipment" in lift.table_basis and js.AWP_HV in ids(lift)
    transit = js.overhead_line_clearance(12, "crane", boom_lowered_in_transit_no_load=True)
    assert transit.table_used == "Table 1" and transit.required_clearance_ft == 6


def test_not_high_voltage_unknown_voltage_and_storage():
    lv = js.overhead_line_clearance("480 V", "ladder")
    assert lv.high_voltage is False and lv.status == "not_high_voltage" and lv.required_clearance_ft is None
    assert js.overhead_line_clearance("600 V", "ladder").high_voltage is False     # "more than 600 volts"
    assert js.HV_T1 not in ids(lv) and js.HV_SURVEY in ids(lv)
    # Unknown voltage: the largest Table 2 distance (42 ft at 550-1,000 kV) meets Table 2 at every voltage listed.
    unk = js.overhead_line_clearance("unknown", "concrete pump")
    assert unk.status == "voltage_unknown" and unk.fallback_clearance_ft == 42 and unk.warning_sign == "likely_required"
    assert js.overhead_line_clearance(None, "ladder").fallback_clearance_ft == 20     # Table 1 maximum
    store = js.overhead_line_clearance(12, "rebar laydown / storage")
    assert store.equipment_key == "storage" and store.table_used == "Table 2" and store.required_clearance_ft == 10
    assert js.HV_STORE in ids(store) and store.status == "distance_not_given"


def test_prohibitions_exemptions_and_spotter():
    over = js.overhead_line_clearance(12, "crane", 25, work_over_lines=True)
    assert over.status == "prohibited_over_lines" and over.clearance_ok is False and js.HV_OVER in ids(over)
    dead = js.overhead_line_clearance(12, "crane", 2, line_deenergized_and_grounded=True)
    assert dead.status == "deenergized_and_grounded" and "visibly grounded" in dead.summary
    qew = js.overhead_line_clearance(12, "aerial lift", 2, qualified_electrical_worker_authorized_by_line_owner=True)
    assert qew.status == "qualified_worker_exempt" and "at least 2 years" in qew.next_step
    assert {js.HV_QEW_EXEMPT, js.HV_QEW} <= ids(qew)
    sp = js.overhead_line_clearance(12, "boom truck", 12, operator_can_see_clearance=False)
    assert sp.spotter_required is True and "Required here" in req(sp, "Spotter").detail
    assert "post a spotter" in sp.next_step


@pytest.mark.parametrize("args,msg", [
    ((1200, "crane"), "above 1,000 kV"),
    ((12000, "crane"), "A bare number means kilovolts"),
    ((0, "crane"), "more than 0"),
    (("twelve", "crane"), "voltage_kv must be"),
    ((12, "spaceship"), "not recognized"),
    ((12, ""), "equipment is required"),
    ((12, "crane", -3), "cannot be below 0"),
])
def test_overhead_rejects_bad_inputs(args, msg):
    with pytest.raises(ValueError, match=msg):
        js.overhead_line_clearance(*args)


# ---------------------------------------------------------------- confined spaces (8 CCR 1950-1962)
def test_sewer_manhole_with_low_oxygen_is_a_permit_space_with_retrieval_device():
    # 3-part test met; O2 18.9% < 19.5% -> hazardous atmosphere -> permit-required; LFL 4% <= 10% OK;
    # vertical depth 12 ft > 5 ft -> mechanical retrieval device (1960(d)).
    r = js.confined_space_classification("Sewer manhole MH-3", True, True, False, oxygen_pct=18.9,
                                         flammable_pct_lfl=4, toxic_could_exceed_pel=False, vertical_depth_ft=12)
    assert r.confined_space and r.permit_required and r.hazardous_atmosphere
    assert r.hazards == ["oxygen deficient (18.9% < 19.5%)"] and r.entry_status == "permit_entry"
    assert r.mechanical_retrieval_device_required is True
    assert "Vertical depth 12 ft > 5 ft -> mechanical retrieval device required" in r.math
    assert "Oxygen 18.9%: below 19.5% -> oxygen deficient (hazardous)" in r.math
    assert "16-item entry permit" in r.next_step and "at least 1 attendant" in r.next_step
    assert "Entry permit: 16 required items" in {q.item for q in r.requirements}
    assert "Program contents (14 elements)" in {q.item for q in r.requirements}
    assert "Keep canceled permits at least 1 year" in {q.item for q in r.requirements}
    assert "Review the program within 1 year after each entry" in {q.item for q in r.requirements}
    assert {js.CS_DEF, js.CS_O2, js.CS_RETRIEVAL, js.CS_PERMIT_ITEMS, js.CS_PROGRAM, js.CS_RESCUE} <= ids(r)
    assert r.verified is True
    shallow = js.confined_space_classification("vault", True, True, False, oxygen_pct=18, flammable_pct_lfl=0,
                                               toxic_could_exceed_pel=False, vertical_depth_ft=5)
    assert shallow.mechanical_retrieval_device_required is False             # 5 ft is not "more than 5 feet"


def test_flammables_at_or_above_10_percent_lfl():
    # 12% > 10% -> hazardous atmosphere; 1953(a)(3)(D)1: no entry at 10% LFL or more in a permit space.
    r = js.confined_space_classification("vault", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=12,
                                         toxic_could_exceed_pel=False)
    assert r.entry_status == "entry_prohibited" and js.CS_NO_ENTRY_LFL in ids(r)
    assert r.next_step.startswith("Do not enter") and "below 10% LFL" in r.next_step
    # Exactly 10%: not yet hazardous ("in excess of 10 percent"), but a space AT the limit "has a potential to
    # contain a hazardous atmosphere" (permit-space definition (1)), and 1953(a)(3)(D)1 bars entry until flammables
    # are "below 10 percent": permit space, no entry.
    at = js.confined_space_classification("vault", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=10,
                                          toxic_could_exceed_pel=False)
    assert at.permit_required is True and at.entry_status == "entry_prohibited"
    assert "flammables at 10% LFL, the limit (potential hazardous atmosphere)" in at.hazards
    assert any("not yet a hazardous atmosphere" in n for n in at.notes) and js.CS_NO_ENTRY_LFL in ids(at)
    assert at.readings[1].hazardous is False                   # the reading itself is not "in excess of 10 percent"
    # 9.99% is under the limit: not a permit space on these facts.
    below = js.confined_space_classification("vault", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=9.99,
                                             toxic_could_exceed_pel=False)
    assert below.permit_required is False and below.entry_status == "non_permit_space"
    # Exactly 10% in a space that is a permit space for another reason: entry barred.
    eng = js.confined_space_classification("hopper", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=10,
                                           toxic_could_exceed_pel=False, engulfment_hazard=True)
    assert eng.entry_status == "entry_prohibited"


def test_not_a_confined_space_test_first_and_non_permit():
    room = js.confined_space_classification("mechanical room", True, False, True)
    assert room.confined_space is False and room.entry_status == "not_a_confined_space" and "meets 1 of the 3 criteria" in room.summary
    crawl = js.confined_space_classification("crawl space", True, True, False)
    assert crawl.permit_required is None and crawl.entry_status == "test_first"
    assert "oxygen, combustible gases and vapors, then toxic gases and vapors" in crawl.next_step
    ok = js.confined_space_classification("attic plenum", "yes", "y", "no", oxygen_pct="20.9%",
                                          flammable_pct_lfl="0", toxic_could_exceed_pel="no")
    assert ok.permit_required is False and ok.entry_status == "non_permit_space"
    # Combustible dust 25% MEC > 20% (2026 amendment) -> hazardous atmosphere
    dust = js.confined_space_classification("silo", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=0,
                                            toxic_could_exceed_pel=False, combustible_dust_pct_mec=25)
    assert dust.permit_required and "combustible dust 25% MEC > 20%" in dust.hazards and js.CS_DUST in ids(dust)


def test_alternate_procedure_hot_work_and_roles():
    alt = js.confined_space_classification("tank T-1", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=2,
                                           toxic_could_exceed_pel=False, atmosphere_could_become_hazardous=True,
                                           ventilation_alone_keeps_safe=True)
    assert alt.entry_status == "alternate_procedure_allowed" and {js.CS_ALT, js.CS_ALT_CERT} <= ids(alt)
    assert "date, location of the space, and signature of certifier" in req(alt, "Written pre-entry").detail
    # A physical hazard rules out the ventilation-only procedure.
    phys = js.confined_space_classification("tank T-2", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=2,
                                            toxic_could_exceed_pel=False, atmosphere_could_become_hazardous=True,
                                            ventilation_alone_keeps_safe=True, engulfment_hazard=True)
    assert phys.entry_status == "permit_entry" and phys.alternate_procedure_eligible is False
    # O2 24% > 23.5% -> oxygen enriched: no ignition sources in or next to the space (1953(a)(3)(I)).
    hot = js.confined_space_classification("vault", True, True, False, oxygen_pct=24, flammable_pct_lfl=0,
                                           toxic_could_exceed_pel=False, hot_work_planned=True)
    assert hot.hot_work == "prohibited" and "oxygen enriched (24% > 23.5%)" in hot.hazards
    gc = js.confined_space_classification("vault", True, True, False, oxygen_pct=18, flammable_pct_lfl=0,
                                          toxic_could_exceed_pel=False, your_role="the GC")
    assert gc.your_role == "controlling_contractor" and {js.CS_EXCHANGE, js.CS_DEBRIEF} <= ids(gc)
    assert "As the controlling contractor" in gc.next_step
    owner = js.confined_space_classification("pump pit", True, True, False, oxygen_pct=20.9, flammable_pct_lfl=0,
                                             toxic_could_exceed_pel=False, engulfment_hazard=True,
                                             workers_will_enter=False, your_role="Property Owner")
    assert owner.your_role == "host_employer" and owner.entry_status == "permit_space_keep_out"
    assert js.CS_NO_GC in ids(owner) and "DANGER -- PERMIT-REQUIRED CONFINED SPACE, DO NOT ENTER" in owner.next_step
    assert js.CS_RECLASSIFY in ids(owner)        # only a physical hazard and clean air: reclassifying is an option
    ex = js.confined_space_classification("sewer trench", True, True, False, is_excavation_or_tunnel=True)
    assert ex.article_37_applies is False and ex.entry_status == "not_covered_by_article_37"


@pytest.mark.parametrize("kwargs,msg", [
    ({"space": ""}, "space is required"),
    ({"can_bodily_enter": "maybe"}, "must be true or false"),
    ({"can_bodily_enter": None}, "must each be true or false"),
    ({"oxygen_pct": 120}, "cannot be above 100"),
    ({"flammable_pct_lfl": -1}, "cannot be below 0"),
    ({"your_role": "astronaut"}, "your_role must be"),
])
def test_confined_space_rejects_bad_inputs(kwargs, msg):
    args = {"space": "vault", "can_bodily_enter": True, "limited_entry_or_exit": True,
            "designed_for_continuous_occupancy": False} | kwargs
    with pytest.raises(ValueError, match=msg):
        js.confined_space_classification(**args)


# ---------------------------------------------------------------- Valley Fever
def test_kern_county_training_with_unverified_may_1_date():
    r = js.valley_fever_requirements("Kern County", as_of=AS_OF)
    assert r.county == "Kern" and r.highly_endemic and r.basis == "listed_by_calosha" and r.training_required
    # Reported rule: by May 1 each year. Next May 1 on/after 2026-11-25 is 2027-05-01.
    assert r.next_annual_training_deadline == "2027-05-01"
    assert r.verified is False and r.unverified_rule_ids == [js.VF_MAY1]
    assert req(r, "Training timing (NOT VERIFIED)").verified is False
    assert any(n.startswith("NOT VERIFIED (unverifiable)") for n in r.notes)
    assert "recognizing symptoms" in r.training_topics and r.dust_controls
    assert "N95, N99, N100, P100, or HEPA" in req(r, "Dust controls").detail
    assert {js.VF_TRAIN, js.VF_THRESHOLD, js.VF_CONTROLS, js.VF_REPORT} <= ids(r)
    assert "Kern County jobsite" in r.next_step and "2027-05-01 (date not verified)" in r.next_step


def test_may_1_rollover():
    assert js.valley_fever_requirements("Fresno", as_of="2026-03-02").next_annual_training_deadline == "2026-05-01"
    assert js.valley_fever_requirements("Fresno", as_of="2026-05-01").next_annual_training_deadline == "2026-05-01"
    assert js.valley_fever_requirements("Fresno", as_of="2026-05-02").next_annual_training_deadline == "2027-05-01"


def test_counties_not_on_the_list_use_the_case_rate():
    # "more than 20 cases annually ... per 100,000": 24.3 > 20 -> highly endemic; exactly 20 -> not.
    la = js.valley_fever_requirements("County of Los Angeles", county_cases_per_100k=24.3, as_of=AS_OF)
    assert la.county == "Los Angeles" and la.highly_endemic and la.basis == "case_rate_over_threshold"
    assert "24.3 cases per 100,000 per year > 20 -> highly endemic" in la.math
    edge = js.valley_fever_requirements("Alameda", county_cases_per_100k=20, as_of=AS_OF)
    assert edge.highly_endemic is False and edge.training_required is False and edge.verified is True
    sd = js.valley_fever_requirements("SD", as_of=AS_OF)
    assert sd.county == "San Diego" and sd.highly_endemic is None and sd.basis == "not_listed_rate_unknown"
    assert sd.verified is True and js.VF_MAY1 not in ids(sd) and "CDPH" in sd.next_step


def test_county_spellings():
    for spelling in ("County of Kern", "KERN CO.", " kern county, CA ", "kern"):
        assert js.valley_fever_requirements(spelling, as_of=AS_OF).county == "Kern"
    assert js.valley_fever_requirements("SLO", as_of=AS_OF).county == "San Luis Obispo"
    assert js.valley_fever_requirements(" san luis obispo county, california", as_of=AS_OF).county == \
        "San Luis Obispo"
    assert js.valley_fever_requirements("City and County of San Francisco", as_of=AS_OF).county == "San Francisco"
    no_soil = js.valley_fever_requirements("Tulare", soil_disturbing_work=False, as_of=AS_OF)
    assert no_soil.training_required and no_soil.dust_controls == [] and js.VF_CONTROLS not in ids(no_soil)


@pytest.mark.parametrize("county,msg", [
    ("City of Bakersfield", "is a city"),
    ("Bakersfield", "If it is a city"),
    ("Fresmo", "Did you mean Fresno"),
    ("SB", "ambiguous"),
    ("", "county is required"),
])
def test_valley_fever_rejects_bad_counties(county, msg):
    with pytest.raises(ValueError, match=msg):
        js.valley_fever_requirements(county)


def test_valley_fever_rejects_bad_rate_and_date():
    with pytest.raises(ValueError, match="cannot be below 0"):
        js.valley_fever_requirements("Kern", county_cases_per_100k=-1)
    with pytest.raises(ValueError, match="ISO date"):
        js.valley_fever_requirements("Kern", as_of="11/25/2026")


# ---------------------------------------------------------------- jobsite hazard checklist
def test_gc_checklist_with_noise_dose_extinguishers_and_valley_fever():
    r = js.jobsite_hazard_checklist(
        ["Scissor lift for drywall", "torch cutting rebar", "flagging on Main St", "jackhammer slab",
         "trenching for sewer lateral", "roofing", "paint touch-up"],
        your_role="GC", county="Kern County", hazard_created_by="another sub", your_workers_exposed=True,
        noise_exposures=[{"dba": 100, "hours": 2}, {"dba": 95, "hours": 2}, (85, 4)],
        floor_area_sq_ft=7500, floors=2, as_of=AS_OF)
    keys = [g.key for g in r.groups]
    assert keys == ["soil_disturbance", "traffic_control", "hot_work", "scissor_lifts", "noise", "ppe",
                    "fire_protection"]
    # Table N-1: 100 dBA -> 2 h, 95 dBA -> 4 h; dose = 2/2 + 2/4 = 1.50 > 1; 85 dBA is below 90 and not counted.
    assert r.noise_dose.dose == 1.5 and r.noise_dose.exceeds_limit is True
    assert "85 dBA for 4 h: below 90 dBA, the lowest Table N-1 level (not counted)" in r.noise_dose.math
    assert group(r, "noise").next_step.startswith("Noise is over the Table N-1 limit")
    # 1922: one 2A per 3,000 sq ft: ceil(7,500 / 3,000) = 3 per floor x 2 floors = 6.
    assert (r.fire_extinguishers.per_floor, r.fire_extinguishers.total_2a) == (3, 6)
    assert group(r, "fire_protection").requirements[0].item == "2A extinguishers: 6"
    vf = group(r, "soil_disturbance")
    assert vf.label.endswith("Kern County") and vf.verified is False and "2027-05-01" in vf.next_step
    assert r.verified is False and r.unverified_rule_ids == [js.VF_MAY1]
    assert r.unmatched_tasks == ["paint touch-up"]
    assert "lookup_safety_standard" in r.covered_by_other_tools["roofing"]
    assert "check_cal_osha_permits" in r.covered_by_other_tools["trenching for sewer lateral"]
    assert "Hot work: written-basis fire prevention and suppression procedure per NFPA 51B and ANSI Z49.1" in \
        r.documents_to_prepare
    assert "9 topics" in group(r, "traffic_control").next_step                 # 1599(g) lists 9 flagger topics
    assert "42 in (+/- 3 in)" in group(r, "scissor_lifts").next_step
    me = r.multi_employer
    assert me.your_role == "general_contractor" and me.categories == ["exposing", "controlling"]
    assert me.citable_without_own_workers_exposed and me.exposing_employer_defenses == []
    assert {js.ME_CONTROLLING, js.ME_PRACTICE, js.ME_SCHEDULE, js.ME_NO_EXPOSURE} <= ids(r)
    assert r.next_step.startswith("First: Train every worker on the Kern County jobsite")


def test_checklist_without_county_is_fully_verified_and_points_to_the_tool():
    r = js.jobsite_hazard_checklist(["mass grading", "forklift unloading"], as_of=AS_OF)
    soil = group(r, "soil_disturbance")
    assert soil.requirements[0].item == "Valley Fever: check the county" and "valley_fever_requirements" in \
        soil.see_also_tools
    assert "3 years" in group(r, "forklifts").next_step                       # 3668(d)(2): every 3 years
    assert r.verified is True and r.unverified_rule_ids == []


def test_subcontractor_exposed_to_another_employers_hazard_gets_the_five_defenses():
    r = js.jobsite_hazard_checklist(["work near power lines"], your_role="sub", hazard_created_by="the GC",
                                    your_workers_exposed="yes")
    me = r.multi_employer
    assert me.hazard_created_by == "another_employer" and me.categories == ["exposing"]
    assert me.citable_without_own_workers_exposed is False and len(me.exposing_employer_defenses) == 5
    assert "all 5 defenses" in me.requirements[1].item and me.next_step.startswith("Tell the GC")
    # Near power lines, hard hats must be Class E (3381: conductors greater than 600 volts).
    assert group(r, "ppe").requirements[0].detail.startswith("Near conductors greater than 600 volts: Class E only")
    assert "overhead_line_clearance" in group(r, "overhead_lines").see_also_tools
    created = js.jobsite_hazard_checklist(["welding"], your_role="general contractor", hazard_created_by="GC")
    assert created.multi_employer.categories == ["creating", "controlling"]
    solo = js.jobsite_hazard_checklist("forklift, welding", your_role="sole proprietor")
    assert js.ME_SOLE in ids(solo) and solo.tasks == ["forklift", "welding"]


def test_noise_rounding_and_levels_above_the_table():
    # 92.3 dBA has no row; the next whole row up (93 dBA, 5.28 h) is used: 3 / 5.28 = 0.568 -> 0.57.
    r = js.jobsite_hazard_checklist(["saw cutting"], noise_exposures=[{"dba": 92.3, "hours": 3}])
    assert r.noise_dose.dose == 0.57 and r.noise_dose.exceeds_limit is False
    # 116 dBA is above the last Table N-1 row (115 dBA, 0.25 h): no permitted time listed -> treated as over.
    hi = js.jobsite_hazard_checklist(["pile driving"], noise_exposures=[(116, 0.1)])
    assert hi.noise_dose.levels_above_table == [116] and hi.noise_dose.exceeds_limit is True
    # A dose without a noisy task name still adds the noise group.
    assert "noise" in {g.key for g in js.jobsite_hazard_checklist(["layout"], noise_exposures=[(100, 1)]).groups}


@pytest.mark.parametrize("kwargs,msg", [
    ({"tasks": []}, "at least one task"),
    ({"tasks": 42}, "must be a list"),
    ({"your_role": "astronaut"}, "your_role must be"),
    ({"hazard_created_by": "aliens"}, "hazard_created_by must be"),
    ({"floors": 0}, "cannot be below 1"),
    ({"floors": 1.5}, "whole number"),
    ({"noise_exposures": [(100, 13), (95, 12)]}, "more than 24"),
    ({"noise_exposures": ["loud"]}, "each noise exposure"),
    ({"tasks": ["grading"], "county": "Fresmo"}, "Did you mean Fresno"),
])
def test_checklist_rejects_bad_inputs(kwargs, msg):
    args = {"tasks": ["welding"]} | kwargs
    with pytest.raises(ValueError, match=msg):
        js.jobsite_hazard_checklist(**args)


# ---------------------------------------------------------------- MCP
def test_register_and_call_every_tool_through_an_mcp_client():
    mcp = FastMCP("t")
    js.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            out = {
                "smoke": await c.call_tool("wildfire_smoke_requirements", {"aqi_pm25": "AQI 175", "hours_exposed": 6}),
                "lines": await c.call_tool("overhead_line_clearance", {"voltage_kv": "12,470 V",
                                                                       "equipment": "Mobile Crane",
                                                                       "planned_closest_distance_ft": 8}),
                "lines_num": await c.call_tool("overhead_line_clearance", {"voltage_kv": 69, "equipment": "boom lift"}),
                "cs": await c.call_tool("confined_space_classification", {
                    "space": "MH-3", "can_bodily_enter": True, "limited_entry_or_exit": True,
                    "designed_for_continuous_occupancy": False, "oxygen_pct": 18.9, "flammable_pct_lfl": 4,
                    "toxic_could_exceed_pel": False, "vertical_depth_ft": 12, "your_role": "GC"}),
                "vf": await c.call_tool("valley_fever_requirements", {"county": "County of Kern", "as_of": AS_OF}),
                "check": await c.call_tool("jobsite_hazard_checklist", {
                    "tasks": ["scissor lift", "jackhammer"], "your_role": "sub",
                    "noise_exposures": [{"dba": 100, "hours": 2}, {"dba": 95, "hours": 2}],
                    "floor_area_sq_ft": 7500, "floors": 2}),
            }
            return tools, {k: v.structured_content for k, v in out.items()}
    tools, out = asyncio.run(run())
    assert set(tools) == {"wildfire_smoke_requirements", "overhead_line_clearance", "confined_space_classification",
                          "valley_fever_requirements", "jobsite_hazard_checklist"}
    for t in tools.values():
        a = t.annotations.model_dump()
        assert a.get("readOnlyHint", a.get("read_only_hint")) is True
        assert a.get("openWorldHint", a.get("open_world_hint")) is False
    assert "151" in tools["wildfire_smoke_requirements"].description              # numbers come from the rulebook
    assert "42 ft to 1,000 kV" in tools["overhead_line_clearance"].description
    assert out["smoke"]["respirators"] == "voluntary" and out["smoke"]["verified"] is True
    assert out["lines"]["required_clearance_ft"] == 10 and out["lines"]["status"] == "too_close"
    assert out["lines_num"]["voltage_volts"] == 69000 and out["lines_num"]["required_clearance_ft"] == 11
    assert out["cs"]["entry_status"] == "permit_entry" and out["cs"]["mechanical_retrieval_device_required"] is True
    assert out["cs"]["your_role"] == "controlling_contractor"
    assert out["vf"]["county"] == "Kern" and out["vf"]["verified"] is False
    assert out["vf"]["next_annual_training_deadline"] == "2027-05-01"
    assert out["check"]["noise_dose"]["dose"] == 1.5 and out["check"]["fire_extinguishers"]["total_2a"] == 6
    assert out["check"]["citations"] and out["check"]["disclaimer"].startswith("Compliance information")


def test_mcp_rejects_bad_values_with_clear_errors():
    mcp = FastMCP("t")
    js.register(mcp)

    async def run():
        async with Client(mcp) as c:
            errors = []
            for name, args in [
                ("wildfire_smoke_requirements", {"aqi_pm25": "hazy"}),
                ("wildfire_smoke_requirements", {"aqi_pm25": 175, "pm25_ug_m3": -5}),
                ("overhead_line_clearance", {"voltage_kv": 1200, "equipment": "crane"}),
                ("confined_space_classification", {"space": "vault", "can_bodily_enter": True,
                                                   "limited_entry_or_exit": True,
                                                   "designed_for_continuous_occupancy": False, "oxygen_pct": 120}),
                ("valley_fever_requirements", {"county": "City of Fresno"}),
                ("jobsite_hazard_checklist", {"tasks": []}),
                ("jobsite_hazard_checklist", {"tasks": ["welding"], "floors": 0}),
            ]:
                try:
                    await c.call_tool(name, args)
                    errors.append(None)
                except Exception as e:                         # fastmcp raises ToolError
                    errors.append(str(e))
            return errors
    errors = asyncio.run(run())
    assert all(errors), errors
    assert "must be a number" in errors[0] and "1,000 kV" in errors[2] and "is a city" in errors[4]


# ---------------------------------------------------------------- regressions: legal-accuracy review 2026-11-25
def test_pack_corrections_and_new_rules_are_in_the_rulebook():
    t2 = core.rule(js.HV_T2)
    # 2946(b)(2), last paragraph: "Operation of boom-type equipment shall conform to ... Table 2".
    assert t2.status == "corrected" and t2.verified and "(b)(2)" in t2.citation_text
    assert "Operation of boom-type equipment" in t2.param("boom_type_equipment_rule_text")
    apf = {r["respirator"].split(" (")[0]: r["apf"] for r in core.rule(js.RESP_APF).param("apf_table")}
    # 8 CCR 5144 Table 1: APR quarter 5, half 10 (incl. filtering facepieces), full 50; PAPR loose-fitting 25,
    # full 1,000; pressure-demand SCBA full facepiece 10,000.
    assert (apf["air-purifying, quarter mask"], apf["air-purifying, half mask"], apf["air-purifying, full facepiece"],
            apf["PAPR, loose-fitting facepiece"], apf["PAPR, full facepiece"],
            apf["SCBA, pressure-demand, full facepiece"]) == (5, 10, 50, 25, 1000, 10000)
    assert core.rule(js.RESP_APF).verified and core.rule(js.RESP_APF).param("filtering_facepiece_apf") == 10
    epa = core.rule(js.SMOKE_EPA)
    # 40 CFR 58 App. G Table 2 (eCFR, 2024-05-06 version): AQI 151 starts at 55.5; AQI 500 is 325.4 ug/m3.
    assert epa.verified and epa.param("aqi_500_pm25_ug_m3") == 325.4 and epa.param("aqi_151_starts_at_pm25_ug_m3") == 55.5
    assert {js.SMOKE_EPA, js.RESP_APF} <= set(js.RULE_IDS)


@pytest.mark.parametrize("equipment,key", [("JLG 60 ft boom lift", "aerial_lift"), ("bucket truck", "aerial_lift"),
                                           ("mini excavator", "excavator"), ("backhoe", "excavator"),
                                           ("telehandler", "telehandler"), ("digger derrick", "boom_truck")])
def test_boom_type_equipment_is_on_table_2_by_the_rule_text(equipment, key):
    # 12 kV: Table 2 first row (over 600 to 50,000 V) = 10 ft; 8 ft is 2 ft short of the LEGAL minimum.
    r = js.overhead_line_clearance(12, equipment, 8)
    assert r.equipment_key == key and r.legal_minimum_clearance_ft == r.required_clearance_ft == 10
    assert r.status == "too_close" and r.clearance_ok is False and r.shortfall_ft == 2
    assert "2946(b)(2)" in r.table_basis and "Table 1's smaller distance is the legal minimum" not in r.table_basis


def test_machines_with_no_boom_report_table_1_as_the_legal_minimum():
    # Scissor lift at 8 ft from 12 kV: Table 1 (6 ft, machinery) is met; the tool's Table 2 margin (10 ft) is not.
    r = js.overhead_line_clearance(12, "scissor lift", 8)
    assert (r.legal_minimum_clearance_ft, r.required_clearance_ft) == (6, 10)
    assert r.status == "meets_legal_minimum_only" and r.clearance_ok is True and r.shortfall_ft is None
    assert "meets the legal minimum of 6 ft" in r.summary and "inside 6 ft" in r.next_step
    assert {js.HV_T1, js.HV_T2} <= ids(r) and js.HV_NOTIFY not in ids(r)     # outside the legal clearance: no 2948
    # Below Table 1 it is too close in law, and the shortfall is measured from the legal minimum (6 - 5 = 1).
    low = js.overhead_line_clearance(12, "forklift", 5)
    assert low.status == "too_close" and low.shortfall_ft == 1 and "even Table 1's legal minimum is 6 ft" in low.summary
    # A dump truck raising its bed is recognized (it used to be rejected).
    dump = js.overhead_line_clearance(12, "dump truck with bed raised", 12)
    assert dump.equipment_key == "vehicle" and dump.status == "clear"
    # Unknown voltage for a no-boom machine: Table 2 fallback (42 ft) and the Table 1 ceiling (20 ft) both shown.
    unk = js.overhead_line_clearance("unknown", "scissor lift")
    assert unk.fallback_clearance_ft == 42 and "Table 1 tops out at 20 ft" in unk.summary


def test_overhead_units_and_utility_voltage_notation():
    # 96 in = 8 ft (< 10 ft): it used to be read as 96 ft and called clear.
    assert js.overhead_line_clearance(12, "crane", "96 in").status == "too_close"
    m = js.overhead_line_clearance(12, "crane", "3.1 m")              # 3.1 / 0.3048 = 10.17 ft >= 10
    assert m.status == "clear" and m.planned_closest_distance_ft == pytest.approx(10.170603675)
    with pytest.raises(ValueError, match="unit 'yards' not understood"):
        js.overhead_line_clearance(12, "crane", "4 yards")
    # '12.47/7.2 kV' (phase-to-phase / phase-to-ground): the tables use phase to phase, the larger figure.
    assert js.overhead_line_clearance("12.47/7.2 kV", "crane").voltage_volts == 12470
    assert js.overhead_line_clearance("12.47Y/7.2kV", "ladder").voltage_volts == 12470
    # 9.99 ft from a 10 ft minimum is shown precisely, not rounded to "10 ft ... 0 ft short".
    close = js.overhead_line_clearance(12, "crane", 9.999)
    assert close.status == "too_close" and "9.999 ft" in close.summary and "0.001 ft short" in close.summary


def test_qualified_worker_exemption_is_limited_to_work_on_the_line():
    r = js.overhead_line_clearance(12, "aerial lift", 2, qualified_electrical_worker_authorized_by_line_owner=True)
    assert "the energized line itself" in r.summary and "other construction near the line keeps the clearances" \
        in r.summary and "run this again without the exemption" in r.next_step


def test_smoke_setting_open_doors_and_portable_purifiers_are_not_exempt():
    # 5141.1(a)(2)(A): filtered by a mechanical ventilation system AND openings kept closed.
    open_door = js.wildfire_smoke_requirements(220, 8, work_setting="site office with HVAC filters, door propped open")
    assert open_door.covered and open_door.work_setting == "enclosed_unfiltered"
    assert any("kept closed" in n for n in open_door.notes)
    purifier = js.wildfire_smoke_requirements(220, 8, work_setting="site office with a HEPA purifier")
    assert purifier.covered and any("portable air cleaner alone" in n for n in purifier.notes)
    windows = js.wildfire_smoke_requirements(220, 8, work_setting="loader cab with cabin air filter, windows down")
    assert windows.covered
    closed = js.wildfire_smoke_requirements(220, 8, work_setting="loader cab with cabin air filter, windows up")
    assert closed.covered is False and closed.work_setting == "enclosed_vehicle_filtered"


def test_smoke_units_and_threshold_display():
    assert js.wildfire_smoke_requirements(175, "90 min").hours_exposed == 1.5          # 90 / 60 = 1.5 h > 1 h
    assert js.wildfire_smoke_requirements(175, "45 min").covered is False              # 0.75 h <= 1 h
    # 0.08 mg/m3 = 80 ug/m3 -> Appendix A row 55.5-150.4 -> AQI 151-200.
    assert js.wildfire_smoke_requirements(pm25_ug_m3="0.08 mg/m3", hours_exposed=8).aqi_range == "151-200"
    with pytest.raises(ValueError, match="unit 'ppm' not understood"):
        js.wildfire_smoke_requirements(pm25_ug_m3="80 ppm", hours_exposed=8)
    with pytest.raises(ValueError, match="not a realistic PM2.5 reading"):
        js.wildfire_smoke_requirements(pm25_ug_m3=1e308, hours_exposed=8)
    r = js.wildfire_smoke_requirements(175, 1.0001)
    assert r.covered and "1.0001 h at AQI 151+ this shift > 1 h -> no hours exemption" in r.math     # not "1 h > 1 h"


def test_smoke_over_500_names_the_respirators_that_meet_the_apf():
    # 600 / 55.4 = 10.8303 -> 10.84 > 10 (N95/half mask): not enough. Full-facepiece APR (50) covers up to
    # 50 x 55.4 = 2,770 ug/m3; loose-fitting PAPR (25) up to 1,385.
    r = js.wildfire_smoke_requirements(pm25_ug_m3=600, hours_exposed=8)
    assert "NOT enough" in r.summary and js.RESP_APF in ids(r) and r.verified
    assert "air-purifying, full facepiece: APF 50, covers PM2.5 up to 2,770 ug/m3" in r.respirator_options
    assert "PAPR, loose-fitting facepiece: APF 25, covers PM2.5 up to 1,385 ug/m3" in r.respirator_options
    assert not any(o.startswith("air-purifying, half mask") for o in r.respirator_options)
    assert "air-purifying, full facepiece: APF 50" in r.next_step
    # 520 / 55.4 = 9.3863 -> 9.39 <= 10: an N95 is enough.
    assert "is enough" in js.wildfire_smoke_requirements(pm25_ug_m3=520, hours_exposed=8).summary
    # 600,000 / 55.4 = 10,830.3 > 10,000 (the highest APF in Table 1): no respirator qualifies.
    none = js.wildfire_smoke_requirements(pm25_ug_m3=600000, hours_exposed=8)
    assert none.respirator_options == [] and none.next_step.startswith("Do not work in this smoke")
    # 3,000 / 55.4 = 54.15 -> 54.16: only PAPR full facepiece (1,000) among APR/PAPR types.
    big = js.wildfire_smoke_requirements(pm25_ug_m3=3000, hours_exposed=8)
    assert [o.split(":")[0] for o in big.respirator_options] == ["PAPR, full facepiece"]


def test_onsite_reading_between_epa_and_appendix_a_aqi_500_is_flagged():
    # 400 ug/m3: Appendix A (mandatory for on-site readings) -> 250.5-500.4 = AQI 301-500 -> voluntary;
    # EPA's current scale (AQI 500 = 325.4) would report it over 500. The note explains the difference.
    r = js.wildfire_smoke_requirements(pm25_ug_m3=400, hours_exposed=8)
    assert r.respirators == "voluntary" and r.aqi_range == "301-500" and js.SMOKE_EPA in ids(r)
    assert any("AirNow" in n and "325.4" in n for n in r.notes)
    assert js.SMOKE_EPA not in ids(js.wildfire_smoke_requirements(pm25_ug_m3=300, hours_exposed=8))


def test_confined_space_units_and_procedure_wording():
    base = dict(space="vault", can_bodily_enter=True, limited_entry_or_exit=True,
                designed_for_continuous_occupancy=False)
    with pytest.raises(ValueError, match="ppm is not a percent"):
        js.confined_space_classification(**base, flammable_pct_lfl="5 ppm")
    ok = js.confined_space_classification(**base, oxygen_pct="20.9 % O2", flammable_pct_lfl="0% LEL",
                                          toxic_could_exceed_pel=False)
    assert ok.entry_status == "non_permit_space"
    deep = dict(base, oxygen_pct=18, flammable_pct_lfl=0, toxic_could_exceed_pel=False)
    # 1960(d): mechanical device for vertical permit spaces "more than 5 feet" deep. 60 in = 5 ft (no); 1.6 m = 5.25 ft.
    assert js.confined_space_classification(**deep, vertical_depth_ft="60 in").mechanical_retrieval_device_required \
        is False
    assert js.confined_space_classification(**deep, vertical_depth_ft="1.6 m").mechanical_retrieval_device_required
    # 1952(e)(2)(E)1 and (G): no entry until ventilation clears the air; everyone leaves if a hazard appears.
    alt = js.confined_space_classification(**dict(base, oxygen_pct=18, flammable_pct_lfl=0,
                                                  toxic_could_exceed_pel=False), ventilation_alone_keeps_safe=True)
    assert alt.entry_status == "alternate_procedure_allowed" and "let no one in until it has cleared" in alt.next_step
    assert "everyone leaves at once" in alt.next_step
    hot = js.confined_space_classification(**dict(base, oxygen_pct=20.9, flammable_pct_lfl=0,
                                                  toxic_could_exceed_pel=False), hot_work_planned=True)
    assert hot.entry_status == "non_permit_space" and "re-evaluate the space with the hot work running" in hot.next_step


def test_valley_fever_county_and_rate_parsing():
    assert js.valley_fever_requirements("Fresno, CA 93721", as_of=AS_OF).county == "Fresno"
    assert js.valley_fever_requirements("Bakersfield, Kern County", as_of=AS_OF).county == "Kern"
    assert js.valley_fever_requirements("Alameda", county_cases_per_100k="25 per 100,000",
                                        as_of=AS_OF).highly_endemic is True
    edge = js.valley_fever_requirements("Alameda", county_cases_per_100k=20.0001, as_of=AS_OF)
    assert edge.highly_endemic and "20.0001 cases per 100,000 > 20" in edge.summary       # not "20 > 20"


@pytest.mark.parametrize("task", ["install digital thermostats", "lay flagstone patio", "replace AC compressor",
                                  "set up sawhorses", "hang drywall panels", "paint machine room",
                                  "set propane tank", "build backyard fire pit"])
def test_checklist_does_not_match_look_alike_words(task):
    r = js.jobsite_hazard_checklist([task])
    assert r.unmatched_tasks == [task] and [g.key for g in r.groups] == ["ppe", "fire_protection"]


def test_checklist_still_matches_the_real_tasks():
    r = js.jobsite_hazard_checklist(["digging footings", "flagger at Main St", "replace circuit breakers",
                                     "air compressor and jackhammer", "inside tank T-2 relining", "elevator pit",
                                     "install solar panels"])
    keys = {g.key for g in r.groups}
    assert {"soil_disturbance", "traffic_control", "electrical", "noise", "confined_spaces"} <= keys
    assert "noise" not in {g.key for g in js.jobsite_hazard_checklist(["replace circuit breakers"]).groups}


def test_checklist_inputs_roles_and_sole_proprietor():
    with pytest.raises(ValueError, match="each task must be a short text"):
        js.jobsite_hazard_checklist([["welding"]])
    with pytest.raises(ValueError, match="noise_exposures must be a list"):
        js.jobsite_hazard_checklist(["welding"], noise_exposures="100 dBA for 2 h")
    with pytest.raises(ValueError, match="hours more than 0"):
        js.jobsite_hazard_checklist(["welding"], noise_exposures=[{"dba": 100, "hours": 0}])
    with pytest.raises(ValueError, match="Did you mean Fresno"):      # checked even with no soil-disturbing task
        js.jobsite_hazard_checklist(["welding"], county="Fresmo")
    assert js.jobsite_hazard_checklist(["welding"], your_role="subcontractor (electrical)").your_role == "subcontractor"
    assert js.jobsite_hazard_checklist(["welding"], your_role="general contractor (prime)").your_role == \
        "general_contractor"
    assert js.jobsite_hazard_checklist(["welding"], your_role="self-employed electrician").your_role == \
        "sole_proprietor"
    # A sole proprietor is not an employee (Compliance Manual Ch. 14): never an "exposing" employer, no defenses.
    solo = js.jobsite_hazard_checklist(["welding"], your_role="sole proprietor", your_workers_exposed=True,
                                       hazard_created_by="another sub")
    assert solo.multi_employer.categories == [] and solo.multi_employer.exposing_employer_defenses == []
    assert "no citable violation" in solo.multi_employer.next_step
    assert "every employer category applies" not in solo.multi_employer.next_step
    ppe = group(js.jobsite_hazard_checklist(["welding"]), "ppe")
    assert ppe.next_step.startswith("For each task, require the head, eye and face, hand and foot protection")


def test_single_family_dwelling_needs_one_extinguisher_on_site():
    # 1922(a)(1) EXCEPTION: single-family dwellings need one 2A (or equivalent) on site during working hours.
    sfd = js.jobsite_hazard_checklist(["framing"], floor_area_sq_ft=7500, floors=2, single_family_dwelling=True)
    ex = sfd.fire_extinguishers
    assert ex.total_2a == 1 and ex.per_floor is None and ex.single_family_dwelling_exception
    assert group(sfd, "fire_protection").next_step.startswith("Keep at least 1 2A extinguisher")
    # Without the flag: ceil(7,500 / 3,000) = 3 per floor x 2 = 6 ("or fraction thereof"), with a pointer.
    other = js.jobsite_hazard_checklist(["framing"], floor_area_sq_ft=7500, floors=2)
    assert other.fire_extinguishers.total_2a == 6 and "single_family_dwelling=true" in other.fire_extinguishers.math


def test_mcp_rejects_booleans_for_numbers_and_takes_the_new_parameter():
    mcp = FastMCP("t")
    js.register(mcp)

    async def run():
        async with Client(mcp) as c:
            errs = []
            for name, args in [("overhead_line_clearance", {"voltage_kv": True, "equipment": "crane"}),
                               ("wildfire_smoke_requirements", {"aqi_pm25": 175, "hours_exposed": True}),
                               ("wildfire_smoke_requirements", {"aqi_pm25": True})]:
                try:
                    await c.call_tool(name, args)
                    errs.append(None)
                except Exception as e:
                    errs.append(str(e))
            ok = await c.call_tool("jobsite_hazard_checklist", {"tasks": ["framing"], "single_family_dwelling": True})
            lift = await c.call_tool("overhead_line_clearance", {"voltage_kv": "12.47/7.2 kV", "equipment": "scissor lift",
                                                                  "planned_closest_distance_ft": 8})
            return errs, ok.structured_content, lift.structured_content
    errs, ok, lift = asyncio.run(run())
    assert all(errs) and "not true/false" in errs[0] and "not true/false" in errs[1]
    assert ok["fire_extinguishers"]["total_2a"] == 1
    assert lift["status"] == "meets_legal_minimum_only" and lift["legal_minimum_clearance_ft"] == 6
