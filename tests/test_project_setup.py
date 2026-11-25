"""Project setup checklist, risk score and rulebook tools: realistic California projects with hand-computed dates/amounts.

Calendar used below: 2027-01-11, 2026-12-28, 2027-02-01 and 2027-03-01 are Mondays; 2026-11-25 (as_of) is a Wednesday.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, project_setup as ps, rulebook_tools as rt

AS_OF = "2026-11-25"


def phase(r, name):
    return next(p for p in r.phases if p.name == name)


def item(r, phase_name, start):
    return next(i for i in phase(r, phase_name).items if i.title.startswith(start))


def titles(r, phase_name):
    return [i.title for i in phase(r, phase_name).items]


def oakland_addition(**kw):
    """Homeowner addition in Oakland: $85,000 contract, 620 sq ft, 1962 house, 2 ft footings, start Mon 1/11/2027."""
    args = dict(project_name="Oak St addition", city="Oakland", county="Alameda", owner_type="private_individual",
                is_home_improvement=True, residential_units=1, contract_price_usd=85_000, valuation_usd=80_000,
                sq_ft=620, stories=1, year_built=1962, excavation_depth_ft=2, start_date="2027-01-11",
                estimated_completion_date="2027-05-28", scope="addition", peak_crew_size=6, as_of=AS_OF)
    args.update(kw)
    return ps.project_setup_checklist(**args)


def fresno_school(**kw):
    """$2.4M public school modernization with demolition, 6 ft trenches, 1.6 acres, 25 workers."""
    args = dict(project_name="Lincoln Elementary modernization", city="Fresno", county="Fresno",
                owner_type="public_agency", is_home_improvement=False, residential_units=0,
                contract_price_usd=2_400_000, valuation_usd=2_250_000, sq_ft=18_000, stories=2, height_ft=30,
                demolition=True, year_built=1958, excavation_depth_ft=6, disturbed_acres=1.6,
                start_date="2027-01-11", estimated_completion_date="2027-08-31", scope="alteration",
                peak_crew_size=25, as_of=AS_OF)
    args.update(kw)
    return ps.project_setup_checklist(**args)


# ---------------------------------------------------------------- rule ids
def test_every_rule_id_constant_exists_in_rulebook():
    ids = [v for k, v in vars(ps).items() if k.isupper() and isinstance(v, str) and k not in
           ("CONTRACT", "LICENSING", "PERMITS", "ENVIRONMENT", "CALOSHA", "LIEN", "PUBLIC", "SITE", "CLOSEOUT",
            "NO_HOLIDAYS")]
    assert len(ids) > 150
    for rid in ids:
        core.rule(rid)                                   # raises KeyError if missing
    for entry in ps.UNVERIFIED.values():
        assert entry["source"].startswith("https://") and entry["citation"]


# ---------------------------------------------------------------- home improvement addition
def test_home_addition_contract_math():
    r = oakland_addition()
    assert r.home_improvement_contract and not r.public_work and r.scope == "addition"
    # Down payment: 10% x $85,000 = $8,500; lesser of $1,000 and $8,500 = $1,000
    down = item(r, ps.CONTRACT, "Cap the down payment")
    assert down.title == "Cap the down payment at $1,000.00" and "$8,500.00" in down.math and down.verified
    # Retention (private, contracts on/after 2026-01-01): 5% x $85,000 = $4,250
    assert item(r, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($4,250.00)"
    cancel = item(r, ps.CONTRACT, "Give the right-to-cancel notice")
    assert "3 business days; 5 if the buyer is 65+" in cancel.title
    # HIC, so the non-HIC prime contract statement is not used
    assert not any(t.startswith("Print the CSLB") for t in titles(r, ps.CONTRACT))


def test_home_addition_permit_fees_and_triggers():
    r = oakland_addition()
    # School fee: residential addition 620 sq ft > 500 -> 620 x $5.38 = $3,335.60 (Level 1 maximum)
    school = item(r, ps.PERMITS, "Pay school developer fees")
    assert school.math.startswith("620 sq ft x $5.38 = $3,335.60")
    # SMIP residential 1-3 stories: $80,000 x 0.00013 = $10.40; building standards: ceil(80,000/25,000) = 4 -> $4.00
    fees = item(r, ps.PERMITS, "State fees on the permit")
    assert fees.title == "State fees on the permit: SMIP $10.40 + building standards $4.00" and fees.verified
    # 1962 house: smoke/CO alarms ($80,000 > $1,000) and water-conserving fixtures (built before 1994)
    assert "Smoke and CO alarms before final" in titles(r, ps.PERMITS)
    assert "Water-conserving plumbing fixtures (built 1962)" in titles(r, ps.PERMITS)
    stamp = item(r, ps.PERMITS, "Plans: architect/engineer stamp not required")
    assert not stamp.verified and "B&P 5537" in stamp.citation        # from UNVERIFIED, not the rulebook


def test_home_addition_small_addition_has_no_school_fee():
    r = oakland_addition(sq_ft=480)
    assert not any(t.startswith("Pay school developer fees") for t in titles(r, ps.PERMITS))
    assert any("480 sq ft (<= 500) is exempt" in n for n in phase(r, ps.PERMITS).notes)


def test_home_addition_environmental_and_site_dates():
    r = oakland_addition()
    # RRP pamphlet: start 1/11/2027 - 60 days = 11/12/2026; mail by 1/11 - 7 = 1/4/2027
    rrp = item(r, ps.ENVIRONMENT, "Lead-safe renovation")
    assert "2026-11-12" in rrp.due and "2027-01-04" in rrp.due
    assert rrp.requires_licensed_professional.startswith("EPA-certified renovator")
    survey = item(r, ps.ENVIRONMENT, "Asbestos survey")
    assert survey.requires_licensed_professional == "Cal/OSHA Certified Asbestos Consultant (CAC)"
    # 811: call day not counted; 2 full working days (Thu 1/7, Fri 1/8) -> call by Wed 1/6; no earlier than 1/11-14 = 12/28
    call = item(r, ps.SITE, "Mark the dig area in white paint and call 811 (USA North 811)")
    assert call.due_date == "2027-01-06" and "2026-12-28" in call.math and "2027-02-03" in call.math  # 1/6 + 28 days
    # Toolbox meetings: 1/11 + 10 working days = Mon 1/25/2027
    assert item(r, ps.CALOSHA, "Toolbox safety meetings").due_date == "2027-01-25"
    # Lead (pre-1978): Cal/OSHA notice 24 hours before trigger tasks -> 1/10/2027
    assert item(r, ps.CALOSHA, "Lead: written compliance program").due_date == "2027-01-10"
    # Residential framing fall protection at 6 ft; water: 6 workers x 8 h x 1 qt = 48 qt (12 gal)
    assert "Fall protection at 6 ft" in titles(r, ps.CALOSHA)
    assert "48 quarts (12 gallons)" in item(r, ps.CALOSHA, "Heat Illness").math
    # Toilets: ceil(6/20) = 1
    assert "ceil(6 / 20) = 1" in item(r, ps.SITE, "Toilets").math


def test_home_addition_closeout_dates():
    r = oakland_addition()
    # NOC: completion 5/28/2027 + 15 = 6/12/2027; no-NOC lien deadline 5/28 + 90 = 8/26/2027 (direct: NOC + 60)
    noc = item(r, ps.CLOSEOUT, "Notice of completion")
    assert noc.due_date == "2027-06-12" and "2027-08-26" in noc.math and "D + 60 days" in noc.math
    # Retention release 5/28 + 45 = 7/12/2027; records 5 years -> 2032-05-28
    assert item(r, ps.CLOSEOUT, "Retention release").due_date == "2027-07-12"
    assert item(r, ps.CLOSEOUT, "Keep the project file").due_date == "2032-05-28"
    t24 = item(r, ps.CLOSEOUT, "Title 24 energy documents")
    assert t24.requires_licensed_professional.startswith("Certified HERS rater")
    assert not phase(r, ps.PUBLIC).applies
    assert phase(r, ps.LIEN).notes == ["No construction lender: as direct contractor you owe no preliminary notice."]


def test_hic_threshold_lender_and_small_downpayment():
    # $6,000 job: 10% = $600 < $1,000 -> $600; lender-funded over $5,000 -> joint check / escrow item
    r = oakland_addition(contract_price_usd=6_000, valuation_usd=6_000, has_construction_lender=True, sq_ft=None)
    assert item(r, ps.CONTRACT, "Cap the down payment").title == "Cap the down payment at $600.00"
    assert "Set up lender disbursement by joint check or escrow" in titles(r, ps.CONTRACT)
    # Direct contractor with a lender: preliminary notice to the lender, 1/11 + 20 = 1/31/2027 (unverifiable rule)
    pre = item(r, ps.LIEN, "Serve a preliminary notice on the construction lender")
    assert pre.due_date == "2027-01-31" and not pre.verified
    # Exactly $500 is not "more than $500": no home improvement contract rules
    assert not oakland_addition(contract_price_usd=500, valuation_usd=500).home_improvement_contract
    # Company owner: HIC rules protect natural persons -> not applied, note instead
    co = oakland_addition(owner_type="private_company")
    assert not co.home_improvement_contract and any("natural persons" in n for n in phase(co, ps.CONTRACT).notes)


# ---------------------------------------------------------------- public school
def test_public_school_demolition_and_calosha_dates():
    r = fresno_school()
    assert r.public_work and not r.verified
    # NESHAP facility: notice 10 working days (holidays count) before Mon 1/11/2027 -> Mon 12/28/2026
    notice = item(r, ps.ENVIRONMENT, "File the demolition notice")
    assert notice.due_date == "2026-12-28" and notice.verified
    assert item(r, ps.PERMITS, "Demolition permit").due_date == "2026-12-28"
    assert item(r, ps.ENVIRONMENT, "Asbestos survey").due_date == "2026-12-28"
    # Cal/OSHA trench permit (6 ft >= 5 ft): issued within 5 working days -> apply by Mon 1/4/2027
    trench = item(r, ps.CALOSHA, "Cal/OSHA trench/excavation permit")
    assert trench.due_date == "2027-01-04" and "$50.00" in trench.why
    assert "Post the Cal/OSHA permit at the jobsite" in titles(r, ps.SITE)
    # 30 ft building: no 36 ft structure permit; 6 ft trench: no engineer (only over 20 ft)
    assert not any("building/structure" in t for t in titles(r, ps.CALOSHA))
    exc = item(r, ps.CALOSHA, "Excavation protection for 6 ft")
    assert "engineer" not in exc.requires_licensed_professional
    # Nonresidential: fall protection above 7.5 ft; 25 workers -> ceil(25/20) = 2 toilets; 25 x 8 x 1 = 200 qt
    assert "Fall protection above 7.5 ft" in titles(r, ps.CALOSHA)
    assert "ceil(25 / 20) = 2" in item(r, ps.SITE, "Toilets").math
    assert "200 quarts (50 gallons)" in item(r, ps.CALOSHA, "Heat Illness").math
    # 1.6 acres -> Construction General Permit with QSD/QSP; under 5 acres -> erosivity waiver mentioned
    cgp = item(r, ps.ENVIRONMENT, "Stormwater")
    assert cgp.requires_licensed_professional.startswith("QSD") and "erosivity waiver" in cgp.why
    assert "Stormwater Notice of Termination" in titles(r, ps.CLOSEOUT)


def test_public_school_fees_bonds_and_public_works_dates():
    r = fresno_school()
    # SMIP nonresidential: $2,250,000 x 0.00028 = $630.00; building standards: ceil(2,250,000/25,000) = 90 -> $90.00
    assert item(r, ps.PERMITS, "State fees").title == "State fees on the permit: SMIP $630.00 + building standards $90.00"
    # Public retention 5% x $2,400,000 = $120,000; payment bond 100% = $2,400,000 (contract > $25,000)
    assert item(r, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($120,000.00)"
    assert item(r, ps.PUBLIC, "Payment bond").title == "Payment bond for 100% of the contract ($2,400,000.00)"
    # DAS 142: 72 hours = 3 business days before Mon 1/11 -> Wed 1/6; eCPR 1/11 + 30 = 2/10; training fund 2/15
    assert item(r, ps.PUBLIC, "Request apprentices on DAS 142").due_date == "2027-01-06"
    assert item(r, ps.PUBLIC, "Electronic certified payroll").due_date == "2027-02-10"
    assert item(r, ps.PUBLIC, "Training fund").due_date == "2027-02-15"
    # DIR registration renews by the next June 30 (2027-06-30), before completion 8/31/2027
    dir_item = item(r, ps.PUBLIC, "DIR public works registration current ($400.00 per fiscal year)")
    assert dir_item.due_date == "2027-06-30" and "before this project ends" in dir_item.why
    # Closeout: final eCPR 8/31 + 30 = 9/30/2027; apprentice statement + 60 = 10/30; retention 8/31 + 60 = 10/30
    final = item(r, ps.CLOSEOUT, "Final eCPR")
    assert final.due_date == "2027-09-30" and "2027-10-30" in final.math
    assert item(r, ps.CLOSEOUT, "Retention release: agency pays within 60 days").due_date == "2027-10-30"
    # Public agency: plan approval authority (DSA for schools) and accessibility come from UNVERIFIED
    keys = {u["key"] for u in r.unverified_sources}
    assert {"public-agency-plan-approval", "accessibility-path-of-travel", "design-professional-stamp",
            "asbestos-consultant"} <= keys
    assert not any(t.startswith("Pay school developer fees") for t in titles(r, ps.PERMITS))
    assert any("architect or civil/structural engineer (PE/SE)" in p for p in r.licensed_professionals)


def test_small_public_job_is_exempt_from_dir_registration_but_pays_prevailing_wage():
    # $18,000 <= $25,000 small project; > $1,000 prevailing wage; < $30,000 no apprenticeship; no payment bond
    r = fresno_school(contract_price_usd=18_000, valuation_usd=18_000, demolition=False, excavation_depth_ft=0,
                      disturbed_acres=0.1)
    t = titles(r, ps.PUBLIC)
    assert "Pay prevailing wages to every worker" in t
    assert "Small project: no DIR registration or eCPR, but keep certified payroll" in t
    assert not any(x.startswith(("DIR public works registration", "Electronic certified payroll", "Payment bond",
                                 "Apprenticeship")) for x in t)


def test_private_project_with_public_funds_flags_coverage():
    r = fresno_school(owner_type="private_company", public_funds=True)
    cov = item(r, ps.PUBLIC, "Confirm whether public funds make this a public work")
    assert cov.severity == "warning" and "$600,000.00" in cov.why and "2.0%" in cov.why


# ---------------------------------------------------------------- subcontractor
def test_subcontractor_prelim_notice_retention_and_deep_trench():
    # Excavation sub on a private office building with a construction loan: $180,000, start Mon 3/1/2027, 22 ft deep
    r = ps.project_setup_checklist(
        project_name="Harbor office - site utilities", city="Long Beach", county="Los Angeles",
        owner_type="private_company", is_home_improvement=False, residential_units=0, contract_price_usd=180_000,
        stories=3, excavation_depth_ft=22, uses_subcontractors=False, has_construction_lender=True,
        start_date="2027-03-01", estimated_completion_date="2027-09-30", contractor_role="subcontractor",
        scope="new_building", as_of=AS_OF)
    # Preliminary notice: 3/1 + 20 = 3/21/2027; subcontract > $400 -> CSLB discipline rule cited
    pre = item(r, ps.LIEN, "Serve your preliminary notice")
    assert pre.due_date == "2027-03-21" and "prelim-notice-sub-discipline-400" in pre.rule_ids and not pre.verified
    # Retention cap on the subcontract: 5% x $180,000 = $9,000
    assert "= $9,000.00" in item(r, ps.CONTRACT, "Check the retention clause").math
    # 22 ft > 20 ft: engineer-designed protective system (UNVERIFIED rule) and Cal/OSHA permit by 3/1 - 5 wd = 2/22
    exc = item(r, ps.CALOSHA, "Excavation protection for 22 ft")
    assert "registered professional engineer" in exc.requires_licensed_professional and not exc.verified
    assert item(r, ps.CALOSHA, "Cal/OSHA trench/excavation permit").due_date == "2027-02-22"
    # Los Angeles County -> DigAlert; sub's lien deadline 9/30 + 90 = 12/29/2027 (30 days after a NOC)
    assert any(t.endswith("(DigAlert / USA South)") for t in titles(r, ps.SITE))
    noc = item(r, ps.CLOSEOUT, "Notice of completion")
    assert "2027-12-29" in noc.math and "D + 30 days" in noc.math
    # Subs don't pull the building permit, pay the permit fees or write the HIC
    assert titles(r, ps.PERMITS) == ["See the building permit before you start; pull any trade permit for your scope"]
    assert r.home_improvement_contract is False


# ---------------------------------------------------------------- new single-family home
def test_new_single_family_home_contract_energy_and_review_clock():
    r = ps.project_setup_checklist(
        project_name="Lot 14 new home", city="Clovis", county="Fresno", owner_type="private_individual",
        is_home_improvement=False, residential_units=1, contract_price_usd=720_000, valuation_usd=650_000,
        sq_ft=2_400, stories=2, start_date="2027-04-05", estimated_completion_date="2027-10-29", as_of=AS_OF)
    assert r.scope == "new_building" and any("assumed 'new_building'" in a for a in r.assumptions)
    assert "Single-family home contract: written contract, Mechanics Lien Warning and CGL statement" in titles(r, ps.CONTRACT)
    assert "Print the CSLB complaint-limits statement on the prime contract" in titles(r, ps.CONTRACT)
    assert "New home: solar PV, battery-ready and EV-capable" in titles(r, ps.PERMITS)
    # School fees: 2,400 sq ft x $5.38 = $12,912.00
    assert item(r, ps.PERMITS, "Pay school developer fees").math.startswith("2,400 sq ft x $5.38 = $12,912.00")
    # SMIP 2-story residential: $650,000 x 0.00013 = $84.50; ceil(650,000/25,000) = 26 -> $26.00
    assert item(r, ps.PERMITS, "State fees").title == "State fees on the permit: SMIP $84.50 + building standards $26.00"
    # AB 2234 (1 unit <= 25): 15 + 30 = 45 business days before Mon 4/5/2027 = 9 weeks earlier = Mon 2/1/2027
    clock = item(r, ps.PERMITS, "Submit a complete housing permit application")
    assert clock.due_date == "2027-02-01" and clock.verified
    # AB 1308 final inspection: 10 business days after Fri 10/29/2027 = Fri 11/12/2027
    assert item(r, ps.CLOSEOUT, "Final inspection within 10 business days").due_date == "2027-11-12"
    # New construction: CALGreen 65% waste plan; no asbestos/RRP (no existing building); 811 assumed for footings
    assert "Construction waste management plan: divert 65% of C&D waste" in titles(r, ps.PERMITS)
    assert not any(t.startswith(("Asbestos", "Lead-safe")) for t in titles(r, ps.ENVIRONMENT))
    assert any(t.startswith("Mark the dig area") for t in titles(r, ps.SITE))
    assert item(r, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($36,000.00)"


def test_teardown_of_old_house_uses_air_district_rule_not_rrp():
    # Demolish a 1950 house, build a new one: start Mon 3/1/2027 -> notice by 3/1 - 10 working days = Mon 2/15/2027
    r = ps.project_setup_checklist(
        project_name="Walnut Creek rebuild", city="Walnut Creek", county="Contra Costa",
        owner_type="private_individual", is_home_improvement=False, residential_units=1, contract_price_usd=1_150_000,
        sq_ft=3_100, stories=2, demolition=True, year_built=1950, scope="new_building", start_date="2027-03-01",
        as_of=AS_OF)
    notice = item(r, ps.ENVIRONMENT, "File the demolition notice")
    assert notice.due_date == "2027-02-15" and "BAAQMD requires notice for every demolition" in notice.why
    assert notice.rule_ids == ["air-district-demolition-notice-10-working-days",
                               "neshap-facility-excludes-4-or-fewer-units"]
    assert "Asbestos survey of every material you will disturb" in titles(r, ps.ENVIRONMENT)
    assert not any(t.startswith("Lead-safe renovation") for t in titles(r, ps.ENVIRONMENT))   # teardown, not RRP
    assert any(t.startswith("Lead: written compliance program") for t in titles(r, ps.CALOSHA))


def test_tall_commercial_building_permits_and_engineers():
    # 4-story, 48 ft office: Cal/OSHA structure + scaffold permits (> 36 ft), emergency call system (>= 48 ft),
    # falsework by a civil engineer (> 14 ft)
    r = ps.project_setup_checklist(
        project_name="Main St office", city="Sacramento", county="Sacramento", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=9_500_000, stories=4, height_ft=48,
        excavation_depth_ft=3, start_date="2027-03-01", as_of=AS_OF)
    t = titles(r, ps.CALOSHA)
    assert "Cal/OSHA building/structure permit" in t and "Cal/OSHA scaffolding/falsework permit" in t
    assert "Cal/OSHA trench/excavation permit" not in t                         # 3 ft < 5 ft
    assert "Two-way emergency call system and basket litter" in t
    assert item(r, ps.CALOSHA, "Falsework/shoring design").requires_licensed_professional == \
        "California registered civil engineer"
    assert "Statement of Special Inspections with the permit set" in titles(r, ps.PERMITS)
    # 3 ft trench: no egress item (egress starts at 4 ft)
    assert not any(x.startswith("Excavation protection") for x in t)


def test_no_start_date_gives_relative_deadlines():
    r = oakland_addition(start_date=None, estimated_completion_date=None)
    assert r.timeline == [] and all(i.due_date is None for p in r.phases for i in p.items)
    assert item(r, ps.ENVIRONMENT, "Lead-safe renovation").due == "no more than 60 days before work"


def test_deterministic_and_input_validation():
    assert oakland_addition().model_dump() == oakland_addition().model_dump()
    with pytest.raises(ValueError):
        oakland_addition(estimated_completion_date="2026-12-01")              # before start
    with pytest.raises(ValueError):
        oakland_addition(contract_price_usd=0)
    with pytest.raises(ValueError):
        oakland_addition(start_date="next monday")


# ---------------------------------------------------------------- license lookup (demo fixtures)
def test_license_blockers_from_cslb_record():
    # 999999 = expired, no bond on file -> both blockers
    r = oakland_addition(contractor_license_number="999999")
    assert r.license.found and r.license.status == "expired"
    assert item(r, ps.LICENSING, "Hold an active license").severity == "blocker"
    assert item(r, ps.LICENSING, "Keep the $25,000.00 contractor bond").severity == "blocker"
    # 777000 = C-39 roofer with a workers' comp exemption: C-39 must carry workers' comp regardless -> blocker
    r = oakland_addition(contractor_license_number="777000")
    assert item(r, ps.LICENSING, "Workers' comp certificate").severity == "blocker"
    # 555000 expires 2026-12-31, before the 1/11/2027 start -> blocker
    r = oakland_addition(contractor_license_number="555000")
    assert item(r, ps.LICENSING, "Hold an active license").severity == "blocker"


# ---------------------------------------------------------------- risk score
def test_risk_score_red_when_demolition_notice_already_late():
    # Start Mon 12/7/2026: notice due 12/7 - 10 working days = Mon 11/23/2026 (Thanksgiving 11/26 counts), but
    # as_of is 11/25 -> 2 days late = red. Roofer 777000 as prime: workers' comp blocker -> licensing red.
    k = ps.project_risk_score(
        project_name="Warehouse teardown and rebuild", city="Ontario", county="San Bernardino",
        owner_type="private_company", is_home_improvement=False, residential_units=0, contract_price_usd=3_100_000,
        demolition=True, year_built=1971, scope="new_building", start_date="2026-12-07",
        contractor_license_number="777000", as_of=AS_OF)
    by = {p.phase: p for p in k.phases}
    assert k.overall == "red"
    env = by[ps.ENVIRONMENT]
    assert env.rating == "red" and env.overdue >= 2
    assert "Past due 2026-11-23: File the demolition notice with the air district 10 working days ahead" in env.reasons
    assert by[ps.LICENSING].rating == "red" and by[ps.LICENSING].blockers == 1
    assert by[ps.CONTRACT].rating == "amber"                 # subcontracts due 12/7, 12 days out (<= 14)
    assert by[ps.PUBLIC].rating == "green" and not by[ps.PUBLIC].applies
    assert k.next_actions[0].startswith("2026-11-23 [overdue]")


def test_risk_score_green_when_everything_is_far_out():
    k = ps.project_risk_score(
        project_name="Garage conversion", city="Pasadena", county="Los Angeles", owner_type="private_individual",
        is_home_improvement=True, residential_units=1, contract_price_usd=120_000, year_built=1995,
        scope="alteration", excavation_depth_ft=0, start_date="2027-06-07", estimated_completion_date="2027-10-01",
        as_of=AS_OF)
    assert k.overall == "green" and all(p.rating == "green" for p in k.phases)
    assert k.next_actions and not k.verified           # several cited rules are unchecked/unverifiable


def test_risk_score_amber_for_public_funds_question():
    k = ps.project_risk_score(
        project_name="Affordable housing podium", city="San Jose", county="Santa Clara", owner_type="private_company",
        is_home_improvement=False, residential_units=80, contract_price_usd=42_000_000, public_funds=True,
        stories=5, height_ft=62, start_date="2027-09-06", as_of=AS_OF)
    assert {p.phase: p.rating for p in k.phases}[ps.PUBLIC] == "amber"


# ---------------------------------------------------------------- rulebook tools
def test_search_and_get_rules():
    hits = rt.search_rules("preliminary notice 20 days first furnishing", limit=5)
    assert 1 <= len(hits) <= 5 and "prelim-notice-20-day-deadline" in [h.id for h in hits]
    verified = rt.search_rules("preliminary notice", limit=10, verified_only=True)
    assert verified and all(h.verified and h.status in ("confirmed", "corrected") for h in verified)
    pens = rt.search_rules("workers compensation", limit=5, category="penalty")
    assert pens and all(h.category == "penalty" for h in pens)
    with pytest.raises(ValueError):
        rt.search_rules("x", category="nonsense")
    with pytest.raises(ValueError):
        rt.search_rules("   ")
    d = rt.get_rule("neshap-notification-10-working-days")
    assert d.status == "confirmed" and d.parameters["lead_time_working_days"] == 10 and d.citation.startswith("40 CFR")
    assert d.notes and d.source
    with pytest.raises(ValueError, match="closest"):
        rt.get_rule("neshap notification ten days")


# ---------------------------------------------------------------- MCP
def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    ps.register(mcp)
    rt.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            chk = await c.call_tool("project_setup_checklist", {
                "project_name": "Oak St addition", "city": "Oakland", "county": "Alameda",
                "owner_type": "private_individual", "is_home_improvement": True, "residential_units": 1,
                "contract_price_usd": 85000, "valuation_usd": 80000, "sq_ft": 620, "year_built": 1962,
                "excavation_depth_ft": 2, "start_date": "2027-01-11", "scope": "addition", "as_of": AS_OF})
            risk = await c.call_tool("project_risk_score", {
                "project_name": "Oak St addition", "city": "Oakland", "county": "Alameda",
                "owner_type": "private_individual", "is_home_improvement": True, "residential_units": 1,
                "contract_price_usd": 85000, "start_date": "2027-01-11", "as_of": AS_OF})
            found = await c.call_tool("search_california_rules", {"query": "trench permit 5 feet", "limit": 3})
            one = await c.call_tool("get_california_rule", {"rule_id": "hic-downpayment-cap"})
            return names, chk.structured_content, risk.structured_content, found.structured_content, one.structured_content
    names, chk, risk, found, one = asyncio.run(run())
    assert {"project_setup_checklist", "project_risk_score", "search_california_rules", "get_california_rule"} <= names
    contract = next(p for p in chk["phases"] if p["name"] == "Contract")
    assert any(i["title"] == "Cap the down payment at $1,000.00" for i in contract["items"])
    assert chk["disclaimer"] and chk["citations"]
    assert risk["overall"] in ("red", "amber", "green") and len(risk["phases"]) == 9
    assert any(r["id"] == "permit-trench-excavation-5ft" for r in found["result"])
    assert one["parameters"]["max_downpayment_dollars"] == 1000 and one["verified"] is True
