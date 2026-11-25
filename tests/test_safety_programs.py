"""Safety programs module: Cal/OSHA written programs, 8 CCR 341 permits and injury reporting, with hand-computed values."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, safety_programs as sp

AS_OF = "2026-11-25"           # Wednesday


def prog(result, key):
    return next(p for p in result.programs if p.key == key)


def item(result, activity):
    return next(i for i in result.items if i.activity == activity)


def dl(result, prefix):
    return next(d for d in result.deadlines if d.action.startswith(prefix))


def ids(result):
    return {c.rule_id for c in result.citations}


# ---------------------------------------------------------------- generate_safety_programs
def test_mid_size_concrete_contractor_core_programs_and_math():
    # 18 employees, 12 on the biggest site, outdoor, public access, 10-hour shifts, Spanish-speaking crew.
    r = sp.generate_safety_programs("Acme Concrete Inc", 18, True, outdoor_work=True, as_of=AS_OF,
                                    max_employees_at_one_site=12, majority_language="Spanish", shift_hours=10,
                                    last_toolbox_meeting="2026-11-20")
    assert {"iipp", "code_of_safe_practices", "heat_illness_outdoor", "wvpp", "hazcom", "emergency"} <= {
        p.key for p in r.programs if p.status == "required"}
    heat = prog(r, "heat_illness_outdoor")
    water = heat.elements[0].requirement
    # 12 workers x 1 qt x 10 h = 120 quarts = 30 gallons per site per shift
    assert "12 workers x 1 qt x 10 h = 120 quarts (30 gallons)" in water
    # one observer per 20 or fewer: ceil(12 / 20) = 1
    assert "at least 1 observer(s)" in heat.elements[3].requirement
    assert "English and Spanish" in heat.elements[-1].requirement
    assert "Designated person to call emergency medical services at each worksite" in heat.named_people
    # Toolbox: Fri 2026-11-20 + 10 working days (11/23-27, 11/30-12/4) = Fri 2026-12-04
    tb = next(t for t in r.training if t.item.startswith("Toolbox"))
    assert tb.due == "2026-12-04" and "10 working days" in tb.detail
    # Log 300: peak 18 > 10 -> required; 300A window after Apr 30 2026 -> Feb 1-Apr 30 2027; 18 < 20 -> no ITA
    assert r.log300_required is True
    post = next(x for x in r.recordkeeping if x.item == "Post 300A")
    assert post.due == "2027-02-01" and "2027-04-30" in post.detail
    assert "no electronic submission" in next(x for x in r.recordkeeping if x.item.startswith("Electronic")).detail
    assert any(p.item.startswith("Cal/OSHA 'Safety and Health") for p in r.postings)
    assert any(p.item == "Post 300A" for p in r.postings)
    assert r.verified is False                                   # IIPP rule itself is 'unchecked'
    assert {"iipp-written-program-required", "toolbox-tailgate-meetings-10-working-days",
            "heat-water-one-quart-per-hour", "log300-size-exemption-10-or-fewer"} <= ids(r)


def test_iipp_outline_is_a_fill_in_document_with_citations():
    r = sp.generate_safety_programs("Baker Framing LLC", 25, True, as_of=AS_OF)
    iipp = prog(r, "iipp")
    md = iipp.document_outline_md
    assert md.startswith("# Injury and Illness Prevention Program (IIPP)")
    assert "Baker Framing LLC" in md and "__________" in md and "IIPP Administrator" in md
    assert "_Authority: 8 CCR 3203(a); 8 CCR 1509(a); Labor Code 6401.7_" in md     # duplicate 1509(a) collapsed
    # 8 required elements from the rulebook + records; 25 employees -> no reduced-documentation element (>= 20)
    assert len(core.rule("iipp-written-program-required").param("required_elements")) == 8
    assert len(iipp.elements) == 9
    assert "oral-only exception is not available" in iipp.elements[2].requirement
    assert "no later than 5 business days after a request" in md
    assert "penalty-no-iipp-no-reduction" in ids(r)
    csp = prog(r, "code_of_safe_practices")
    assert "every 10 working days" in csp.document_outline_md and "Plate A-3" in csp.document_outline_md


def test_small_employer_oral_communication_and_reduced_documentation_check():
    r = sp.generate_safety_programs("Tiny Tile", 8, False, as_of=AS_OF)
    iipp = prog(r, "iipp")
    assert "8 employees, which is under 10" in iipp.elements[2].requirement
    reduced = iipp.elements[-1]
    assert reduced.item.startswith("Reduced documentation") and "EMR is 1.1 or less" in reduced.requirement
    assert reduced.verified is True                              # 3203(b) Exception No. 3 is confirmed
    assert r.log300_required is False                            # peak 8 <= 10
    assert "still report serious injuries" in r.recordkeeping[0].detail


@pytest.mark.parametrize("total,site,public,status", [
    (8, None, False, "exempt"),       # 8 < 10 and no public access
    (8, None, True, "required"),      # public access defeats the exemption
    (30, 6, False, "exempt"),         # company of 30 but never 10+ at one place
    (30, 10, False, "required"),      # 10 is not "fewer than 10"
])
def test_workplace_violence_small_site_exemption(total, site, public, status):
    r = sp.generate_safety_programs("Co", total, public, as_of=AS_OF, max_employees_at_one_site=site)
    w = prog(r, "wvpp")
    assert w.status == status
    assert bool(w.document_outline_md) == (status == "required")
    assert any(t.item == "Workplace violence training" for t in r.training) == (status == "required")


def test_silica_respirator_programs_name_the_competent_person():
    r = sp.generate_safety_programs("Cut Crew", 15, True, silica_tasks=True, respirators_used=True, as_of=AS_OF)
    sil, resp = prog(r, "silica"), prog(r, "respiratory")
    assert sil.status == resp.status == "required"
    assert "Competent person (8 CCR 1532.3(g)(4))" in sil.named_people
    assert "25 ug/m3" in sil.elements[0].requirement and "50 ug/m3 PEL" in sil.elements[1].requirement
    assert "30 or more days a year: baseline within 30 days" in sil.elements[-1].requirement
    assert "at least every 12 months" in resp.elements[1].requirement
    assert resp.verified is False                                # uses UNVERIFIED 5144(c)(1) element list
    assert any("5144" in n for n in r.notes)
    fit = next(t for t in r.training if t.item == "Respirator fit test")
    assert fit.verified is True                                  # 5144(f)(2) is confirmed
    # silica without respirators -> warn that a respirator program may be triggered
    r2 = sp.generate_safety_programs("Cut Crew", 15, True, silica_tasks=True, as_of=AS_OF)
    assert all(p.key != "respiratory" for p in r2.programs)
    assert any("Respiratory Protection Program (8 CCR 5144) becomes required" in n for n in r2.notes)


def test_fall_protection_plan_only_when_conventional_protection_infeasible():
    r = sp.generate_safety_programs("Roof Co", 12, True, max_fall_height_ft=22, as_of=AS_OF)
    fp = prog(r, "fall_protection_plan")
    assert fp.status == "not_required" and "more than 7.5 ft" in fp.why and "22 ft" in fp.why
    assert fp.verified is True and fp.document_outline_md == ""
    r = sp.generate_safety_programs("Roof Co", 12, True, max_fall_height_ft=22,
                                    conventional_fall_protection_infeasible=True, as_of=AS_OF)
    fp = prog(r, "fall_protection_plan")
    assert fp.status == "required"
    assert fp.named_people == ["Qualified person who prepares and approves the plan",
                               "Competent person who supervises it"]
    assert "controlled access zone" in fp.document_outline_md


@pytest.mark.parametrize("stories,height,depth,triggered", [
    (5, None, 0, True),       # 5 floors >= 5
    (4, 48, 0, True),         # 48 ft >= 48
    (4, 47, 0, False),        # 4 floors, 47 ft
    (1, None, 48, True),      # 48 ft below ground
])
def test_emergency_two_way_voice_system_threshold(stories, height, depth, triggered):
    r = sp.generate_safety_programs("Co", 20, True, building_stories=stories, building_height_ft=height,
                                    excavation_max_depth_ft=depth, workers_enter_trench=bool(depth), as_of=AS_OF)
    el = next(e for e in prog(r, "emergency").elements if e.item.startswith("Emergency communication"))
    assert ("REQUIRED for this profile" in el.requirement) == triggered
    assert "two way voice emergency system and a basket litter with straps and two blankets" in el.requirement


def test_first_aid_kit_tier_from_crew_size():
    # 16 at one site -> '16-200' tier of the 8 CCR 1512 table; 5 -> '1-5'
    r = sp.generate_safety_programs("Co", 40, True, max_employees_at_one_site=16, as_of=AS_OF)
    kit = next(e for e in prog(r, "emergency").elements if e.item == "First-aid kit")
    assert "'16-200' tier" in kit.requirement
    assert sp._first_aid_tier(5) == "1-5" and sp._first_aid_tier(6) == "6-15" and sp._first_aid_tier(201) == "over 200"


def test_deep_excavation_names_engineer_and_screens_permit():
    r = sp.generate_safety_programs("Dig Co", 20, True, excavation_max_depth_ft=22, workers_enter_trench=True,
                                    as_of=AS_OF)
    ex = prog(r, "excavation")
    assert "REQUIRED at this depth" in ex.elements[1].requirement                    # 22 >= 5
    assert "Registered professional engineer (excavations deeper than 20 ft)" in ex.named_people
    assert ex.verified is False                                                        # engineer rule is UNVERIFIED
    assert r.permits_to_check == ["Trench / excavation: project"]
    shallow = prog(sp.generate_safety_programs("Dig Co", 20, True, excavation_max_depth_ft=4, as_of=AS_OF),
                   "excavation")
    assert "Competent person exam required" in shallow.elements[1].requirement
    assert all("engineer" not in n for n in shallow.named_people)


def test_log300_electronic_submission_tiers():
    # peak 34 in 20-249 -> 300A by next March 2 (2027-03-02 since as_of is after 2026-03-02)
    r = sp.generate_safety_programs("Co", 34, True, as_of=AS_OF)
    ita = next(x for x in r.recordkeeping if x.item.startswith("Electronic"))
    assert ita.due == "2027-03-02" and "Form 300 and 301" not in ita.detail
    # peak 120 in NAICS 2381xx (Appendix I) -> also 300/301 data
    r = sp.generate_safety_programs("Co", 120, True, naics_code="238120", as_of=AS_OF)
    assert "also submit Form 300 and 301" in next(x for x in r.recordkeeping if x.item.startswith("Electronic")).detail
    # as_of before Feb 1: the window is this year's
    r = sp.generate_safety_programs("Co", 34, True, as_of="2027-01-15")
    assert next(x for x in r.recordkeeping if x.item == "Post 300A").due == "2027-02-01"


def test_lead_asbestos_and_crane_programs():
    r = sp.generate_safety_programs("Reno Co", 22, True, asbestos_or_lead=True, crane_use=True, as_of=AS_OF)
    la = prog(r, "lead_asbestos")
    assert la.status == "conditional" and "every 6 months" in la.elements[0].requirement
    assert "CSLB C-22 or asbestos-certified contractor (100 sq ft+)" in la.named_people
    cr = prog(r, "cranes")
    assert "over 3 tons" in cr.elements[0].requirement and "Certified crane operator" in cr.named_people


def test_employee_count_must_be_positive():
    with pytest.raises(ValueError):
        sp.generate_safety_programs("Co", 0, True, as_of=AS_OF)


# ---------------------------------------------------------------- check_cal_osha_permits
def test_gc_four_story_building_trench_and_scaffold_fees_and_dates():
    r = sp.check_cal_osha_permits(excavation_max_depth_ft=7, workers_enter_trench=True, building_stories=4,
                                  building_height_ft=48, scaffold_or_falsework_height_ft=40,
                                  planned_start="2027-01-11T07:00", as_of=AS_OF)
    assert r.permits_required == ["Trench / excavation", "Building / structure over 36 ft",
                                  "Scaffold / falsework / vertical shoring"]
    # $50 trench + $50 structure + $50 scaffold (8 CCR 341.3) = $150
    assert r.total_fees_usd == 150.0
    assert r.fee_math.endswith("= $150")
    # Project permit issued within 5 working days: Mon 1/11 back 5 weekdays (1/8,7,6,5,4) = Mon 2027-01-04
    assert item(r, "Trench / excavation").deadline == "2027-01-04"
    assert item(r, "Building / structure over 36 ft").holder == "project administrator"
    # 811: call day does not count, so 2 full working days between call and dig: Wed 1/6 call, Thu, Fri, dig Mon.
    usa = item(r, "811 underground utility locate")
    assert usa.deadline == "2027-01-06" and "Call between 2026-12-28 and 2027-01-06" in usa.why   # 14 days back
    scaf = item(r, "Scaffold / falsework / vertical shoring")
    assert "California-registered civil engineer (falsework/shoring design)" in scaf.named_people
    assert "minimum proposed penalty $1,250" in r.penalties[0]
    assert r.verified is False and any("Labor Code 6500" in n for n in r.notes)    # 4 stories > 3 is UNVERIFIED


@pytest.mark.parametrize("depth,enter,exemption,required", [
    (5, True, "none", True),                 # 5 ft is ">= 5"
    (4.9, True, "none", False),
    (6, False, "none", False),               # nobody descends
    (8, True, "swimming_pool_excavation", False),
    (8, True, "government_agency", False),
])
def test_trench_permit_threshold_and_exemptions(depth, enter, exemption, required):
    r = sp.check_cal_osha_permits(excavation_max_depth_ft=depth, workers_enter_trench=enter, exemption=exemption,
                                  as_of=AS_OF)
    assert item(r, "Trench / excavation").required is required
    assert item(r, "811 underground utility locate").required is True             # 811 for any digging


def test_annual_permit_holder_pays_nothing_and_gives_24_hour_notice():
    r = sp.check_cal_osha_permits(excavation_max_depth_ft=8, workers_enter_trench=True, has_annual_permit=True,
                                  planned_start="2026-12-01T07:00", as_of=AS_OF)
    t = item(r, "Trench / excavation")
    assert t.permit_type == "annual (held)" and t.fee_usd == 0
    assert t.deadline == "2026-11-30T07:00"                                         # 24 h before start
    assert r.total_fees_usd == 0 and r.fee_math == "No Cal/OSHA fees."
    assert t.verified is False                                 # 8 CCR 341(d)(5)(A) rule is 'unchecked'
    assert item(r, "811 underground utility locate").verified is True              # all three 811 rules confirmed


def test_verified_when_only_confirmed_rules_are_used():
    # Lead notice only: 8 CCR 1532.1(p) and the 336 permit penalty are both confirmed.
    r = sp.check_cal_osha_permits(lead_disturbed_sqft=150, as_of=AS_OF)
    assert r.verified is True and ids(r) == {"lead-pre-job-notification-24h", "permit-violation-min-penalty-1250"}


@pytest.mark.parametrize("height,self_performs,required,fee", [
    (40, True, True, 100.0),      # framer on a 40 ft building -> annual permit $100
    (36, True, False, 0.0),       # exactly 36 ft is not "more than 36"
    (40, False, False, 0.0),      # sub that does no covered structural work
])
def test_subcontractor_structure_permit(height, self_performs, required, fee):
    r = sp.check_cal_osha_permits(building_height_ft=height, role="subcontractor",
                                  self_performs_structural_work=self_performs, as_of=AS_OF)
    s = item(r, "Building / structure over 36 ft")
    assert s.required is required and r.total_fees_usd == fee
    if required:
        assert s.permit_type == "annual"


def test_annual_permit_fee_counted_once_and_project_permits_suggested_onto_it():
    # Sub framing a 40 ft building and demolishing a 45 ft one: one $100 annual permit, plus a $50 trench project permit
    r = sp.check_cal_osha_permits(excavation_max_depth_ft=6, workers_enter_trench=True, building_height_ft=40,
                                  demolition=True, demolition_height_ft=45, role="subcontractor", as_of=AS_OF)
    assert item(r, "Demolition over 36 ft").fee_usd == 0
    assert r.total_fees_usd == 150.0                                                # 100 + 50
    assert any("ask the district office to list your trench" in n for n in r.notes)


def test_demolition_permit_and_neshap_notice():
    # GC demolishing a 40 ft building, start Mon 2027-01-11: NESHAP 10 working days (holidays count) -> Mon 2026-12-28
    r = sp.check_cal_osha_permits(demolition=True, demolition_height_ft=40, planned_start="2027-01-11", as_of=AS_OF)
    d = item(r, "Demolition over 36 ft")
    assert d.required and d.permit_type == "project" and d.fee_usd == 50.0
    assert item(r, "Air district / NESHAP demolition notice").deadline == "2026-12-28"
    assert any("deadlines assume work starts at 00:00" in n for n in r.notes)
    # 2-story, 30 ft house: no Cal/OSHA permit, but the air district notice is still required
    r = sp.check_cal_osha_permits(demolition=True, building_stories=2, building_height_ft=30, as_of=AS_OF)
    assert item(r, "Demolition over 36 ft").required is False
    assert item(r, "Air district / NESHAP demolition notice").required is True
    assert all(i.activity != "Building / structure over 36 ft" for i in r.items)    # building_* = demolished one


def test_tower_crane_permit():
    r = sp.check_cal_osha_permits(tower_crane=True, planned_start="2027-02-01T06:00", as_of=AS_OF)
    tc = item(r, "Tower crane erection / climbing / dismantling")
    assert tc.fee_usd == 350.0 and tc.deadline == "2027-01-31T06:00" and r.total_fees_usd == 350.0


def test_asbestos_registration_lead_time_and_late_flag():
    # 45 business days (15 completeness + 30 decision) before Mon 2027-03-01 = 9 weeks back = Mon 2026-12-28
    r = sp.check_cal_osha_permits(asbestos_sqft=250, planned_start="2027-03-01", as_of=AS_OF)
    reg = item(r, "Asbestos registration (Cal/OSHA)")
    assert reg.required and reg.fee_usd == 350.0 and reg.deadline == "2026-12-28" and not reg.deadline_passed
    assert "CSLB C-22 contractor or asbestos-certified qualifier" in reg.named_people
    # start Mon 2027-01-11 -> apply by 2026-11-09, already past on 2026-11-25
    r = sp.check_cal_osha_permits(asbestos_sqft=250, planned_start="2027-01-11", as_of=AS_OF)
    assert item(r, "Asbestos registration (Cal/OSHA)").deadline_passed is True
    assert any("Already past the latest date" in n for n in r.notes)
    # 99 sq ft: below the 100 sq ft registration threshold
    r = sp.check_cal_osha_permits(asbestos_sqft=99, as_of=AS_OF)
    assert item(r, "Asbestos registration (Cal/OSHA)").required is False and r.total_fees_usd == 0


def test_lead_notice_24_hours():
    r = sp.check_cal_osha_permits(lead_disturbed_sqft=150, planned_start="2027-01-11T07:00", as_of=AS_OF)
    assert item(r, "Lead work notice to district office").deadline == "2027-01-10T07:00"
    r = sp.check_cal_osha_permits(lead_disturbed_sqft=80, as_of=AS_OF)
    assert item(r, "Lead work notice to district office").required is False


def test_stories_alone_trigger_the_statutory_permit_and_mark_unverified():
    r = sp.check_cal_osha_permits(scaffold_or_falsework_height_stories=4, as_of=AS_OF)
    s = item(r, "Scaffold / falsework / vertical shoring")
    assert s.required and s.unverified == ["lc6500_three_stories"] and s.verified is False
    r = sp.check_cal_osha_permits(scaffold_or_falsework_height_stories=3, as_of=AS_OF)
    assert item(r, "Scaffold / falsework / vertical shoring").required is False


# ---------------------------------------------------------------- injury_reporting_deadlines
def test_hospitalized_worker_full_deadline_set():
    r = sp.injury_reporting_deadlines("2026-11-24T15:30", hospitalized=True, peak_employees_last_year=34,
                                      as_of="2026-11-24T20:00")
    assert r.reportable_to_cal_osha and r.recordable and r.log300_required
    dosh = dl(r, "Report to Cal/OSHA")
    # 15:30 + 8 h = 23:30; at 20:00 that leaves 3.5 h
    assert dosh.due == "2026-11-24T23:30" and dosh.hours_left == 3.5 and dosh.status == "due_today"
    assert dosh.verified is False                               # serious-injury definition rule is 'unchecked'
    assert dl(r, "Give the employee a DWC-1").due == "2026-11-25"                 # Tue + 1 working day = Wed
    assert dl(r, "File Form 5020").due == "2026-11-29"                            # 11/24 + 5 days
    assert dl(r, "Enter case on Cal/OSHA Form 300").due == "2026-12-01"           # 11/24 + 7 days
    post = dl(r, "Post certified Form 300A")
    assert post.due == "2027-02-01" and "2027-04-30" in post.who_and_how
    assert dl(r, "Submit 2026 Form 300A").due == "2027-03-02"                     # peak 34 in 20-249
    assert dl(r, "Keep Forms").due == "2031-12-31"                                # end of 2026 + 5 years
    assert len(r.report_contents) == 10 and "whether the scene was altered" in r.report_contents[-1]
    pen = {p.item: p for p in r.penalties}
    assert pen["Late or missing Cal/OSHA serious injury/death report"].min_usd == 5000
    assert pen["Log 300 / 300A recordkeeping or posting failures"].max_usd == 16285
    assert pen["CSLB license discipline"].min_usd == 500 and pen["CSLB license discipline"].max_usd == 8000


def test_exigent_circumstances_extend_to_24_hours():
    r = sp.injury_reporting_deadlines("2026-11-24T15:30", serious=True, exigent_circumstances=True,
                                      as_of="2026-11-24T20:00")
    assert dl(r, "Report to Cal/OSHA").due == "2026-11-25T15:30"


def test_clock_starts_when_employer_learns_and_overdue_status():
    # Weekend injury, foreman told Monday 08:00: due Mon 16:00; at 17:00 it is 1 h overdue.
    r = sp.injury_reporting_deadlines("2026-11-21T09:00", serious=True, employer_knew_at="2026-11-23T08:00",
                                      as_of="2026-11-23T17:00")
    d = dl(r, "Report to Cal/OSHA")
    assert d.due == "2026-11-23T16:00" and d.hours_left == -1.0 and d.status == "overdue"
    assert dl(r, "Give the employee a DWC-1").due == "2026-11-24"                 # Mon + 1 working day


def test_date_only_as_of_marks_yesterdays_8_hour_deadline_overdue():
    r = sp.injury_reporting_deadlines("2026-11-24T15:30", hospitalized=True, as_of=AS_OF)
    d = dl(r, "Report to Cal/OSHA")
    assert d.status == "overdue" and d.days_left == -1 and d.hours_left is None


def test_friday_knowledge_rolls_dwc1_to_monday_and_workplace_crime_notice():
    r = sp.injury_reporting_deadlines("2026-11-20T10:00", workplace_crime=True, peak_employees_last_year=8,
                                      as_of=AS_OF)
    assert dl(r, "Give the employee a DWC-1").due == "2026-11-23"
    assert dl(r, "Give the employee a DWC-1").status == "overdue"
    assert dl(r, "File Form 5020").status == "due_today"                          # 11/20 + 5 = 11/25
    assert dl(r, "Written workers' comp eligibility notice").due == "2026-11-23"
    assert dl(r, "Enter the incident on the violent incident log").status == "do_now"
    # size-exempt (peak 8 <= 10): no Log 300
    assert not r.log300_required and dl(r, "Enter case on Cal/OSHA Form 300").status == "not_required"
    assert dl(r, "Report to Cal/OSHA").required is False


def test_first_aid_only_case():
    r = sp.injury_reporting_deadlines("2026-11-24T09:00", beyond_first_aid_or_lost_time=False, as_of=AS_OF)
    assert not r.recordable and not r.log300_required
    assert dl(r, "File Form 5020").required is False
    assert dl(r, "Give the employee a DWC-1").required is True


def test_fatality_on_highway_outside_construction_zone_and_criminal_exposure():
    r = sp.injury_reporting_deadlines("2026-11-24T09:00", fatality=True, public_road_outside_construction_zone=True,
                                      peak_employees_last_year=10, as_of=AS_OF)
    assert r.reportable_to_cal_osha is False and "outside a construction zone" in dl(r, "Report to Cal/OSHA").how_computed
    assert dl(r, "File Form 5020").required is True and r.log300_required is False   # 10 <= 10 exempt
    r = sp.injury_reporting_deadlines("2026-11-24T09:00", fatality=True, as_of=AS_OF)
    crim = next(p for p in r.penalties if p.item.startswith("Criminal"))
    assert "$250,000" in crim.detail and crim.max_usd == 1_500_000
    assert any("assumed required" in d.how_computed.lower() for d in r.deadlines)  # peak unknown -> assume Log


def test_knew_before_incident_is_rejected():
    with pytest.raises(ValueError):
        sp.injury_reporting_deadlines("2026-11-24T09:00", serious=True, employer_knew_at="2026-11-23T09:00")
    with pytest.raises(ValueError):
        sp.injury_reporting_deadlines("not a date")


def test_every_cited_rule_exists_and_unverified_has_sources():
    for k, v in sp.UNVERIFIED.items():
        assert v["source"].startswith("https://"), k
    r = sp.generate_safety_programs("Co", 60, True, excavation_max_depth_ft=25, workers_enter_trench=True,
                                    building_stories=6, building_height_ft=70, demolition=True,
                                    scaffold_or_falsework_height_stories=5, silica_tasks=True, crane_use=True,
                                    asbestos_or_lead=True, respirators_used=True, heat_exposure=True,
                                    conventional_fall_protection_infeasible=True, as_of=AS_OF)
    for c in r.citations:
        assert core.rule(c.rule_id).citation_text == c.citation
    assert r.verified is False and len(r.required_programs) >= 12


# ---------------------------------------------------------------- MCP
def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    sp.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            gen = await c.call_tool("generate_safety_programs", {
                "company_name": "Acme", "employee_count": 18, "worksites_have_public_access": True,
                "max_employees_at_one_site": 12, "as_of": AS_OF})
            per = await c.call_tool("check_cal_osha_permits", {
                "excavation_max_depth_ft": 7, "workers_enter_trench": True, "building_height_ft": 48,
                "planned_start": "2027-01-11T07:00", "as_of": AS_OF})
            inj = await c.call_tool("injury_reporting_deadlines", {
                "incident_datetime": "2026-11-24T15:30", "hospitalized": True, "as_of": "2026-11-24T20:00"})
            return names, gen.structured_content, per.structured_content, inj.structured_content
    names, gen, per, inj = asyncio.run(run())
    assert {"generate_safety_programs", "check_cal_osha_permits", "injury_reporting_deadlines"} <= names
    assert gen["log300_required"] is True and "Code of Safe Practices" in gen["required_programs"]
    assert per["total_fees_usd"] == 100.0                                           # $50 trench + $50 structure
    assert inj["deadlines"][0]["due"] == "2026-11-24T23:30"
