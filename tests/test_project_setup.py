"""Project setup checklist, risk score and rulebook tools: realistic California projects with hand-computed dates/amounts.

Calendar used below: 2027-01-11, 2026-12-28, 2027-02-01 and 2027-03-01 are Mondays; 2026-11-25 (as_of) is a Wednesday.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

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
            "NO_HOLIDAYS", "HOLIDAYS_LATER", "GIVEN_HOLIDAYS")]
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
    # Retention: Civil Code 8811(b)(3) exempts residential, non-mixed-use projects of <= 4 stories (1 story here)
    ret = item(r, ps.CONTRACT, "Retention")
    assert ret.title == "Retention: the 5% private cap does not apply to this residential project"
    assert "private-retention-cap-exemptions" in ret.rule_ids and "1 story <= 4 -> exempt" in ret.math
    assert r.private_retention_cap_applies is False
    hic = item(r, ps.CONTRACT, "Use a compliant home improvement contract")
    assert "performance and payment bond" in hic.why and "hic-right-to-require-bond-notice" in hic.rule_ids
    assert hic.verified and "home-improvement-contract-definition" not in hic.rule_ids    # homeowner: no reliance
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
    # Residential framing fall protection at 6 ft (1716.2), 7.5 ft elsewhere (1670); water: 6 x 8 h x 1 qt = 48 qt
    fall = item(r, ps.CALOSHA, "Fall protection")
    assert fall.title == "Fall protection: 6 ft on residential framing, above 7.5 ft elsewhere"
    assert {"fall-protection-7-5-ft", "residential-framing-fall-6-ft"} <= set(fall.rule_ids)
    assert "48 quarts (12 gallons)" in item(r, ps.CALOSHA, "Heat Illness").math
    # Toilets: ceil(6/20) = 1
    assert "ceil(6 / 20) = 1" in item(r, ps.SITE, "Toilets").math


def test_home_addition_closeout_dates():
    r = oakland_addition()
    # NOC: completion 5/28/2027 + 15 = 6/12/2027; no-NOC lien deadline 5/28 + 90 = 8/26/2027 (direct: NOC + 60)
    noc = item(r, ps.CLOSEOUT, "Check for a recorded notice of completion")
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
    assert any("not over $500.00" in n for n in phase(oakland_addition(contract_price_usd=500, valuation_usd=500),
                                                        ps.CONTRACT).notes)
    # Company-owned rental house: B&P 7151.2 covers contracts with an owner or tenant, so HIC rules still apply
    co = oakland_addition(owner_type="private_company")
    assert co.home_improvement_contract
    assert "home-improvement-contract-definition" in item(co, ps.CONTRACT, "Use a compliant home improvement").rule_ids
    assert item(co, ps.CONTRACT, "Cap the down payment").title == "Cap the down payment at $1,000.00"


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
    # Closeout: final eCPR 8/31 + 30 = 9/30/2027; apprentice statement 8/31 + 60 = 10/30; retention 8/31 + 60 = 10/30
    assert item(r, ps.CLOSEOUT, "Final eCPR").due_date == "2027-09-30"
    assert item(r, ps.CLOSEOUT, "Apprentice hours statement").due_date == "2027-10-30"
    rel = item(r, ps.CLOSEOUT, "Retention release: agency pays within 60 days")
    assert rel.due_date == "2027-10-30" and not rel.verified and "90 days" in rel.not_confirmed
    # Public agency: plan approval authority (DSA for schools) and accessibility come from UNVERIFIED; the asbestos
    # consultant rule now comes from the rulebook (B&P 7180), not UNVERIFIED
    keys = {u["key"] for u in r.unverified_sources}
    assert {"public-agency-plan-approval", "accessibility-path-of-travel", "design-professional-stamp"} <= keys
    assert "asbestos-consultant" not in ps.UNVERIFIED
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
    # Retention cap on the subcontract (nonresidential, so SB 61 applies): 5% x $180,000 = $9,000
    ret = item(r, ps.CONTRACT, "Retention")
    assert ret.title == "Retention: no more than 5% ($9,000.00)" and "on your subcontract" in ret.math
    # Sub's retention: owner pays GC by 9/30 + 45 = 11/14; GC pays sub 10 days after receipt -> 11/24/2027
    assert item(r, ps.CLOSEOUT, "Your retention").due_date == "2027-11-24"
    # 22 ft > 20 ft: engineer-designed protective system (UNVERIFIED rule) and Cal/OSHA permit by 3/1 - 5 wd = 2/22
    exc = item(r, ps.CALOSHA, "Excavation protection for 22 ft")
    assert "registered professional engineer" in exc.requires_licensed_professional and not exc.verified
    assert item(r, ps.CALOSHA, "Cal/OSHA trench/excavation permit").due_date == "2027-02-22"
    # Los Angeles County -> DigAlert; sub's lien deadline 9/30 + 90 = 12/29/2027 (30 days after a NOC)
    assert any(t.endswith("(DigAlert / USA South)") for t in titles(r, ps.SITE))
    noc = item(r, ps.CLOSEOUT, "Check for a recorded notice of completion")
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
    assert clock.due_date == "2027-02-01" and clock.verified and clock.rule_ids == ["post-entitlement-permit-completeness-review"]
    # AB 253 private plan check: 30 business days confirmed, which projects qualify not confirmed -> separate item
    ab253 = item(r, ps.PERMITS, "Option: private plan check if the city's review estimate exceeds 30 business days")
    assert not ab253.verified and "unit and height" in ab253.not_confirmed
    # AB 1308 final inspection: 10 business days after Fri 10/29/2027 = Fri 11/12/2027
    assert item(r, ps.CLOSEOUT, "Final inspection within 10 business days").due_date == "2027-11-12"
    # New construction: CALGreen 65% waste plan; no asbestos/RRP (no existing building); 811 assumed for footings
    assert "Construction waste management plan: divert 65% of C&D waste" in titles(r, ps.PERMITS)
    assert not any(t.startswith(("Asbestos", "Lead-safe")) for t in titles(r, ps.ENVIRONMENT))
    assert any(t.startswith("Mark the dig area") for t in titles(r, ps.SITE))
    # 2-story single-family (residential, not mixed-use, <= 4 stories): exempt from the 5% private retention cap
    assert item(r, ps.CONTRACT, "Retention").title.startswith("Retention: the 5% private cap does not apply")
    # AB 1308: deadline confirmed, 10-unit scope not confirmed -> item unverified and listed
    ab = item(r, ps.CLOSEOUT, "Final inspection within 10 business days")
    assert not ab.verified and "10-unit" in ab.not_confirmed and r.not_confirmed and not r.verified
    # EV-capable applies to homes with an attached private garage
    assert "attached private garage" in item(r, ps.PERMITS, "New home: solar PV").why


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


# ---------------------------------------------------------------- reviewer fixes (California law accuracy)
def apartments(**kw):
    """Private 5-story, 40-unit apartment building: $2,000,000 contract, start Mon 3/1/2027."""
    args = dict(project_name="Elm apartments", city="Sacramento", county="Sacramento", owner_type="private_company",
                is_home_improvement=False, residential_units=40, contract_price_usd=2_000_000, stories=5,
                height_ft=58, start_date="2027-03-01", as_of=AS_OF)
    args.update(kw)
    return ps.project_setup_checklist(**args)


def test_private_retention_cap_exemption_boundaries():
    # Civil Code 8811(b)(3): exempt only if residential AND not mixed-use AND <= 4 stories
    five = apartments()                                                   # 5 > 4 stories -> cap applies
    assert five.private_retention_cap_applies is True
    assert item(five, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($100,000.00)"  # 5% x $2M
    four = apartments(stories=4, height_ft=46)                            # 4 <= 4 -> exempt
    assert four.private_retention_cap_applies is False
    assert item(four, ps.CONTRACT, "Retention").title.startswith("Retention: the 5% private cap does not apply")
    mixed = apartments(stories=3, height_ft=38, mixed_use=True)           # mixed-use -> cap applies at any height
    assert item(mixed, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($100,000.00)"
    # Public owner: PCC 7201, not the private rule (no residential exemption)
    pub = fresno_school()
    assert item(pub, ps.CONTRACT, "Retention").rule_ids == ["public-retention-5pct-cap"]
    assert pub.private_retention_cap_applies is None


def test_mixed_use_small_building_is_not_treated_as_a_house():
    # 3 units over a shop, 2 stories, alteration of a 1985 building, valuation $1,000,000
    r = ps.project_setup_checklist(
        project_name="Main St mixed-use", city="Fresno", county="Fresno", owner_type="private_company",
        is_home_improvement=False, residential_units=3, mixed_use=True, contract_price_usd=1_000_000, stories=2,
        year_built=1985, scope="alteration", start_date="2027-03-01", as_of=AS_OF)
    # SMIP Category 2: $1,000,000 x 0.00028 = $280.00; ceil(1,000,000 / 25,000) = 40 -> $40.00
    assert item(r, ps.PERMITS, "State fees").title == "State fees on the permit: SMIP $280.00 + building standards $40.00"
    assert "Plans prepared and stamped by a licensed architect or engineer" in titles(r, ps.PERMITS)
    # Mixed-use is a NESHAP facility even with <= 4 units -> renovation notice if RACM thresholds are met
    assert "If the survey finds regulated asbestos, file a renovation notice" in titles(r, ps.ENVIRONMENT)
    # Same building without the shop: woodframe-exempt plans, no NESHAP renovation notice
    home = ps.project_setup_checklist(
        project_name="Triplex", city="Fresno", county="Fresno", owner_type="private_company",
        is_home_improvement=False, residential_units=3, contract_price_usd=1_000_000, stories=2, year_built=1985,
        scope="alteration", start_date="2027-03-01", as_of=AS_OF)
    assert "If the survey finds regulated asbestos, file a renovation notice" not in titles(home, ps.ENVIRONMENT)
    assert item(home, ps.PERMITS, "State fees").title.startswith("State fees on the permit: SMIP $130.00")  # x 0.00013


def test_public_funds_on_private_property_keep_private_lien_retention_and_closeout():
    r = fresno_school(owner_type="private_company", public_funds=True)
    assert r.public_work and not r.public_owner
    # Prevailing wage side applies ...
    assert "Pay prevailing wages to every worker" in titles(r, ps.PUBLIC)
    assert item(r, ps.CLOSEOUT, "Final eCPR").due_date == "2027-09-30"                 # 8/31 + 30
    # ... but not the public-entity rules: no Civil Code 9550 bond, no PCC 4104 listing, no PCC 7201 retention
    assert not any(t.startswith(("Payment bond", "Check delay damages")) for t in titles(r, ps.PUBLIC))
    assert "Subcontractors: registered, with the labor code clauses" in titles(r, ps.PUBLIC)
    assert item(r, ps.CONTRACT, "Retention").rule_ids == ["private-retention-5pct-cap", "private-retention-cap-exemptions"]
    assert any("private mechanics lien rules still apply" in n for n in phase(r, ps.LIEN).notes)
    # Private retention release: 8/31/2027 + 45 days = 10/15/2027; lien deadline without NOC 8/31 + 90 = 11/29
    assert item(r, ps.CLOSEOUT, "Retention release: owner pays within 45 days").due_date == "2027-10-15"
    assert "2027-11-29" in item(r, ps.CLOSEOUT, "Check for a recorded notice of completion").math
    assert not any(t.startswith("Stop payment notice") for t in titles(r, ps.CLOSEOUT))


def test_public_works_subcontractor_small_subcontract_and_retention():
    # $900 sub on the $2.4M school: the $1,000 prevailing wage test is on the project, not the subcontract
    r = fresno_school(contractor_role="subcontractor", contract_price_usd=900, valuation_usd=900)
    assert "Pay prevailing wages to every worker" in titles(r, ps.PUBLIC)
    assert any("whole project" in a for a in r.assumptions)
    pre = item(r, ps.LIEN, "Public works preliminary notice")
    assert "prelim-notice-20-day-deadline" in pre.rule_ids and pre.due_date == "2027-01-31"   # 1/11 + 20 days
    # Public subcontract retention: 5% x $900 = $45.00 (PCC 7201)
    assert item(r, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($45.00)"
    closeout = titles(r, ps.CLOSEOUT)
    assert "Stop payment notice deadline" in closeout and "Apprentice hours statement" in closeout
    assert "Your retention: the prime pays your share within 7 days of receiving it" in closeout


def test_small_public_project_has_no_final_ecpr_or_apprentice_statement():
    # $18,000 <= $25,000 small project (no eCPR at all); < $30,000 no apprenticeship
    r = fresno_school(contract_price_usd=18_000, valuation_usd=18_000, demolition=False, excavation_depth_ft=0,
                      disturbed_acres=0.1)
    assert not any(t.startswith(("Final eCPR", "Apprentice hours")) for t in titles(r, ps.CLOSEOUT))
    # 0.1 acre: under 1 acre, but a common plan of development can still need CGP coverage
    assert any("common plan of development" in n for n in phase(r, ps.ENVIRONMENT).notes)


def test_workers_comp_exemption_with_employees_and_unknown_license():
    # 555000 = C-10 with a WC exemption; C-10 is not a must-carry class, but 6 workers on site -> blocker
    assert item(oakland_addition(contractor_license_number="555000"), ps.LICENSING,
                "Workers' comp certificate").severity == "blocker"
    assert item(oakland_addition(contractor_license_number="555000", peak_crew_size=None), ps.LICENSING,
                "Workers' comp certificate").severity == "warning"
    # Number not in the (demo) data: not proof of no license -> warning, not blocker
    r = oakland_addition(contractor_license_number="123123")
    assert r.license.found is False and item(r, ps.LICENSING, "Hold an active license").severity == "warning"


def _fake_license(monkeypatch, **rec):
    base = dict(license_number="424242", business_name="Test Co", status="active", expiration_date="2029-06-30",
                workers_comp="on_file", contractor_bond_on_file=True)
    base.update(rec)
    monkeypatch.setattr(ps.licenses, "find", lambda n: (dict(base), "demo fixtures"))


def test_combined_tree_class_and_b2_scope(monkeypatch):
    # C-61/D-49 tree service with a WC exemption: D-49 must carry WC regardless (B&P 7125)
    assert ps._classes(["C-61/D-49 Tree Service"]) == {"C61", "D49"}
    _fake_license(monkeypatch, classifications=["C-61/D-49 Tree Service"], workers_comp="exempt")
    assert item(oakland_addition(contractor_license_number="424242", peak_crew_size=None), ps.LICENSING,
                "Workers' comp certificate").severity == "blocker"
    # B-2 residential remodeler as prime on a new house: outside B-2 scope (existing wood-frame only) -> warning
    _fake_license(monkeypatch, classifications=["B-2 Residential Remodeling"])
    new_home = oakland_addition(contractor_license_number="424242", scope="new_building", year_built=None)
    lic = item(new_home, ps.LICENSING, "Hold an active license")
    assert lic.severity == "warning" and "b2-residential-remodeling-scope" in lic.rule_ids
    remodel = oakland_addition(contractor_license_number="424242", scope="alteration")
    assert item(remodel, ps.LICENSING, "Hold an active license").severity == "ok"
    assert "b2-no-structural-load-bearing-work" in item(remodel, ps.LICENSING, "Hold an active license").rule_ids


def test_calosha_permit_holders_and_demolition_height():
    # Framing sub on a 75 ft building: the GC holds the project permit; the sub needs an annual permit
    sub = ps.project_setup_checklist(
        project_name="Tower framing", city="San Diego", county="San Diego", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=900_000, stories=6, height_ft=75,
        scope="new_building", contractor_role="subcontractor", start_date="2027-03-01", as_of=AS_OF)
    assert item(sub, ps.CALOSHA, "Cal/OSHA building/structure permit").owner_role.startswith(
        "Your company only if your crew does this work (annual permit)")
    assert item(sub, ps.CALOSHA, "Cal/OSHA scaffolding/falsework permit").owner_role.startswith(
        "The employer erecting the scaffolding")
    # Teardown + new 60 ft building: height_ft is the NEW building, so no demolition permit is assumed (note only)
    tear = apartments(demolition=True, year_built=1960, scope="new_building", height_ft=60)
    assert "Cal/OSHA demolition permit" not in titles(tear, ps.CALOSHA)
    assert any("demolition project permit" in n for n in phase(tear, ps.CALOSHA).notes)
    # Demolishing an existing 50 ft building (> 36 ft): demolition permit, apply by 3/1 - 5 working days = 2/22
    demo = apartments(demolition=True, year_built=1960, scope="demolition_only", height_ft=50)
    assert item(demo, ps.CALOSHA, "Cal/OSHA demolition permit").due_date == "2027-02-22"
    # Trench permit only where someone must enter the 5 ft+ excavation
    assert "anyone must enter" in item(fresno_school(), ps.CALOSHA, "Cal/OSHA trench/excavation permit").why


def test_environmental_fixes_asbestos_scaqmd_racm_and_child_occupied_facility():
    school = fresno_school()
    survey = item(school, ps.ENVIRONMENT, "Asbestos survey")
    assert {"asbestos-consultant-dosh-certification-required", "owner-must-hire-certified-asbestos-consultant",
            "asbestos-contractor-bulk-sampling-limit-12"} <= set(survey.rule_ids)
    # NESHAP facility demolition: notice even below the RACM thresholds -> RACM rule cited
    assert "neshap-racm-thresholds" in item(school, ps.ENVIRONMENT, "File the demolition notice").rule_ids
    # 1958 elementary school: may be a child-occupied facility under EPA RRP
    assert any(t.startswith("If children under 6 use the building") for t in titles(school, ps.ENVIRONMENT))
    # SCAQMD notice covers ANY demolition, not only > 100 sq ft of asbestos
    la = ps.project_setup_checklist(
        project_name="Pasadena rebuild", city="Pasadena", county="Los Angeles", owner_type="private_individual",
        is_home_improvement=False, residential_units=1, contract_price_usd=900_000, stories=2, demolition=True,
        year_built=1948, scope="new_building", start_date="2027-03-01", as_of=AS_OF)
    assert "SCAQMD (Rule 1403) for any demolition and for removal of more than 100 sq ft" in \
        item(la, ps.ENVIRONMENT, "File the demolition notice").why


def test_school_fee_adu_and_plumbing_fixture_triggers():
    # Detached ADU for a homeowner: < 500 sq ft -> no Level 1 fee (Gov Code 66311.5)
    small = oakland_addition(scope="new_building", sq_ft=450)
    assert not any(t.startswith("Pay school developer fees") for t in titles(small, ps.PERMITS))
    assert any("450 sq ft" in n and "no Level 1 fee" in n for n in phase(small, ps.PERMITS).notes)
    # 750 sq ft ADU: 750 x $5.38 = $4,035.00
    big = item(oakland_addition(scope="new_building", sq_ft=750), ps.PERMITS, "Pay school developer fees")
    assert big.math.startswith("750 sq ft x $5.38 = $4,035.00") and "adu-school-fee-exempt-under-500" in big.rule_ids
    # Pre-1994 commercial alteration at $120,000 (<= $150,000): fixtures in any room altered still must be replaced
    office = ps.project_setup_checklist(
        project_name="Suite 200", city="Fresno", county="Fresno", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=120_000, year_built=1985,
        scope="tenant_improvement", start_date="2027-03-01", as_of=AS_OF)
    fx = item(office, ps.PERMITS, "Water-conserving plumbing fixtures (built 1985)")
    assert "$150,000.00 or less" in fx.why and "any room you alter" in fx.why


def test_tenant_improvement_sub_gets_nonresponsibility_warning():
    r = ps.project_setup_checklist(
        project_name="Cafe TI", city="Oakland", county="Alameda", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=60_000, year_built=2001,
        scope="tenant_improvement", contractor_role="subcontractor", start_date="2027-03-01", as_of=AS_OF)
    assert "Tenant improvement: the landlord can post a notice of nonresponsibility" in titles(r, ps.LIEN)


# ---------------------------------------------------------------- code + realism review regressions
@pytest.mark.parametrize("kw, field", [
    (dict(contract_price_usd=float("nan")), "contract_price_usd"),
    (dict(contract_price_usd=float("inf")), "contract_price_usd"),
    (dict(valuation_usd=-100), "valuation_usd"),
    (dict(valuation_usd=0), "valuation_usd"),
    (dict(sq_ft=-620), "sq_ft"),                       # used to print "a residential addition of -620 sq ft is exempt"
    (dict(height_ft=0), "height_ft"),
    (dict(excavation_depth_ft=float("nan")), "excavation_depth_ft"),
    (dict(disturbed_acres=-1), "disturbed_acres"),
    (dict(peak_crew_size=-4), "peak_crew_size"),
    (dict(peak_crew_size=2.5), "peak_crew_size"),
    (dict(residential_units=1.5), "residential_units"),
    (dict(scope="remodel"), "scope must be one of"),   # used to surface as a pydantic error on the result model
    (dict(year_built=2031), "year_built 2031 is after"),
    (dict(residential_units=0), "residential_units >= 1"),   # home improvement on a building with no dwellings
    (dict(holidays=["Christmas"]), "holidays"),
    (dict(contract_price_usd="eighty thousand"), "contract_price_usd"),
])
def test_invalid_input_raises_a_plain_error_naming_the_field(kw, field):
    with pytest.raises(ValueError, match=field):
        oakland_addition(**kw)


def test_blank_and_prefixed_city_county_and_license():
    r = oakland_addition(city="City of Oakland", county="Alameda County", contractor_license_number="   ",
                         demolition=True)
    assert r.city == "Oakland" and r.county == "Alameda" and r.license is None
    assert "Get the building permit from the City of Oakland before any work" in titles(r, ps.PERMITS)
    text = " ".join(i.why for p in r.phases for i in p.items)
    assert "County County" not in text and "Check the Alameda County air district's rule" in text
    blank = oakland_addition(city="", county="", demolition=True)
    assert "City or county business tax certificate" in titles(blank, ps.LICENSING)
    assert "Get the building permit from the city or county before any work" in titles(blank, ps.PERMITS)
    assert "Check the local air district's rule" in item(blank, ps.ENVIRONMENT, "File the demolition notice").why


def test_demolition_only_scope_implies_demolition():
    # scope says demolition but the flag was left False: the air district notice must still appear (1/11 - 10 wd)
    r = oakland_addition(scope="demolition_only", demolition=False)
    assert item(r, ps.ENVIRONMENT, "File the demolition notice").due_date == "2026-12-28"
    assert any("demolition_only" in a for a in r.assumptions)


def test_holidays_move_working_day_deadlines_but_not_the_neshap_notice():
    # Fresno school, start Tue 1/5/2027; Fri 1/1/2027 is a holiday
    plain = fresno_school(start_date="2027-01-05")
    hol = fresno_school(start_date="2027-01-05", holidays=["2026-12-25", "2027-01-01"])
    # 811: 3 working days back (call day + 2 full days). Without: 1/4, 1/1, 12/31 -> 12/31; with: 1/4, 12/31, 12/30
    assert item(plain, ps.SITE, "Mark the dig area").due_date == "2026-12-31"
    dig = item(hol, ps.SITE, "Mark the dig area")
    assert dig.due_date == "2026-12-30" and "skipping 2027-01-01" in dig.math and "holidays you listed" in dig.why
    # Cal/OSHA trench permit, 5 working days: without 1/4,1/1,12/31,12/30,12/29 -> 12/29; with 12/28
    assert item(plain, ps.CALOSHA, "Cal/OSHA trench/excavation permit").due_date == "2026-12-29"
    assert item(hol, ps.CALOSHA, "Cal/OSHA trench/excavation permit").due_date == "2026-12-28"
    # DAS 142, 72 hours = 3 business days: 12/31 without, 12/30 with
    assert item(plain, ps.PUBLIC, "Request apprentices on DAS 142").due_date == "2026-12-31"
    assert item(hol, ps.PUBLIC, "Request apprentices on DAS 142").due_date == "2026-12-30"
    # NESHAP counts holidays as working days (40 CFR 61.141): 10 back from 1/5 = 12/22 either way
    for r in (plain, hol):
        assert item(r, ps.ENVIRONMENT, "File the demolition notice").due_date == "2026-12-22"
    assert hol.holidays == ["2026-12-25", "2027-01-01"]
    # a comma-separated string is accepted too
    assert fresno_school(start_date="2027-01-05", holidays="2026-12-25, 2027-01-01").holidays == hol.holidays


def test_caps_round_down_to_the_cent_and_fees_round_half_up():
    # Public subcontract retention: 5% x $900.10 = $45.005 -> $45.00 (float formatting showed $45.01, over the cap)
    sub = fresno_school(contractor_role="subcontractor", contract_price_usd=900.10, valuation_usd=900.10)
    assert item(sub, ps.CONTRACT, "Retention").title == "Retention: no more than 5% ($45.00)"
    # Down payment: 10% x $6,005.05 = $600.505 -> $600.50
    assert item(oakland_addition(contract_price_usd=6_005.05, valuation_usd=6_005.05), ps.CONTRACT,
                "Cap the down payment").title == "Cap the down payment at $600.50"
    # School fee: 600.25 sq ft x $5.38 = $3,229.345 -> $3,229.35 (float formatting gave $3,229.34)
    assert item(oakland_addition(sq_ft=600.25), ps.PERMITS, "Pay school developer fees").math.startswith(
        "600.25 sq ft x $5.38 = $3,229.35")
    # SMIP: $19,500 x 0.00013 = $2.535 -> $2.54 (round() gave $2.53); ceil(19,500 / 25,000) = 1 -> $1.00
    assert item(oakland_addition(valuation_usd=19_500), ps.PERMITS, "State fees").title == \
        "State fees on the permit: SMIP $2.54 + building standards $1.00"


def test_adu_gets_the_adu_permit_clock_not_ab2234():
    # Homeowner's new detached ADU, start Mon 1/11/2027: complete application by 1/11 - 60 days = 11/12/2026
    r = oakland_addition(scope="new_building", sq_ft=750)
    adu = item(r, ps.PERMITS, "ADU permit: submit a complete application at least 60 days before start")
    assert adu.due_date == "2026-11-12" and adu.timing == "overdue" and adu.verified
    assert adu.rule_ids == ["adu-60-day-deemed-approved", "adu-completeness-15-business-days"]
    assert not any(t.startswith("Submit a complete housing permit application") for t in titles(r, ps.PERMITS))
    assert any("treated as an ADU" in a for a in r.assumptions)


def test_subcontractor_output_drops_prime_only_text():
    r = ps.project_setup_checklist(
        project_name="Cafe TI", city="Oakland", county="Alameda", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=60_000, year_built=2001,
        scope="tenant_improvement", contractor_role="subcontractor", start_date="2027-03-01", as_of=AS_OF)
    lic = item(r, ps.LICENSING, "Hold an active license")
    assert "B general building prime contract" not in lic.why and "b-prime-contract-two-trade-rule" not in lic.rule_ids
    assert not any("state permit fees" in a for a in r.assumptions)      # subs get no state fee line


def test_closeout_lien_item_shows_earliest_deadline_and_ab1308_math():
    r = oakland_addition()
    noc = item(r, ps.CLOSEOUT, "Check for a recorded notice of completion")
    # completion 5/28/2027; owner's NOC window ends 5/28 + 15 = 6/12; earliest lien deadline 5/28 + 60 = 7/27
    assert noc.due_date == "2027-06-12" and "earliest possible: 2027-05-28 + 60 = 2027-07-27" in noc.math
    home = ps.project_setup_checklist(
        project_name="Lot 14 new home", city="Clovis", county="Fresno", owner_type="private_individual",
        is_home_improvement=False, residential_units=1, contract_price_usd=720_000, stories=2,
        start_date="2027-04-05", estimated_completion_date="2027-10-29", as_of=AS_OF,
        holidays=["2027-11-11"])
    ab = item(home, ps.CLOSEOUT, "Final inspection within 10 business days")
    # Fri 10/29 + 10 business days, skipping Thu 11/11 (Veterans Day) = Mon 11/15/2027
    assert ab.due_date == "2027-11-15" and "skipping 2027-11-11" in ab.math
    assert ab.owner_role == "You give notice; the building department inspects"


def test_asbestos_survey_is_due_before_the_renovation_notice():
    # 1985 office TI (NESHAP facility): renovation notice 3/1 - 10 working days = 2/15, so the survey is due 2/15 too
    r = ps.project_setup_checklist(
        project_name="Suite 200", city="Fresno", county="Fresno", owner_type="private_company",
        is_home_improvement=False, residential_units=0, contract_price_usd=120_000, year_built=1985,
        scope="tenant_improvement", start_date="2027-03-01", as_of=AS_OF)
    assert item(r, ps.ENVIRONMENT, "Asbestos survey").due_date == "2027-02-15" == \
        item(r, ps.ENVIRONMENT, "If the survey finds regulated asbestos").due_date
    assert "Insurance certificates for the owner" in titles(r, ps.LICENSING)        # no lender named
    assert "Insurance certificates for the owner and lender" in titles(
        oakland_addition(has_construction_lender=True), ps.LICENSING)


def test_far_future_start_warns_about_the_next_code_edition():
    r = oakland_addition(start_date="2030-01-07", estimated_completion_date="2030-06-28")
    code = item(r, ps.PERMITS, "Design to the 2025 California Building Standards Code")
    assert "(or the edition in effect when you submit)" in code.title and "2028 edition" in code.why
    assert "edition in effect" not in item(oakland_addition(), ps.PERMITS, "Design to the 2025").title


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
                "excavation_depth_ft": 2, "start_date": "2027-01-11", "scope": "addition", "as_of": AS_OF,
                "mixed_use": False})
            risk = await c.call_tool("project_risk_score", {
                "project_name": "Oak St addition", "city": "Oakland", "county": "Alameda",
                "owner_type": "private_individual", "is_home_improvement": True, "residential_units": 1,
                "contract_price_usd": 85000, "start_date": "2027-01-11", "as_of": AS_OF})
            found = await c.call_tool("search_california_rules", {"query": "trench permit 5 feet", "limit": 3})
            one = await c.call_tool("get_california_rule", {"rule_id": "hic-downpayment-cap"})
            # holidays and numbers sent as strings (lax JSON from an agent) still work
            hol = await c.call_tool("project_setup_checklist", {
                "project_name": "Oak St addition", "city": "Oakland", "county": "Alameda",
                "owner_type": "private_individual", "is_home_improvement": True, "residential_units": "1",
                "contract_price_usd": "85000", "year_built": 1962, "excavation_depth_ft": 2, "scope": "addition",
                "start_date": "2027-01-05", "as_of": AS_OF, "holidays": ["2027-01-01"]})
            errors = []
            for bad in ({"contract_price_usd": -5}, {"contract_price_usd": "inf"}, {"sq_ft": -1},
                        {"residential_units": 0}, {"start_date": "Jan 11"}):
                try:
                    await c.call_tool("project_risk_score", {
                        "project_name": "x", "city": "Oakland", "county": "Alameda", "owner_type": "private_individual",
                        "is_home_improvement": True, "residential_units": 1, "contract_price_usd": 85000, **bad})
                except ToolError as e:
                    errors.append(str(e))
            schema = next(t for t in await c.list_tools() if t.name == "project_setup_checklist").input_schema
            return (names, chk.structured_content, risk.structured_content, found.structured_content,
                    one.structured_content, hol.structured_content, errors, schema)
    names, chk, risk, found, one, hol, errors, schema = asyncio.run(run())
    assert {"project_setup_checklist", "project_risk_score", "search_california_rules", "get_california_rule"} <= names
    contract = next(p for p in chk["phases"] if p["name"] == "Contract")
    assert any(i["title"] == "Cap the down payment at $1,000.00" for i in contract["items"])
    assert chk["disclaimer"] and chk["citations"]
    assert risk["overall"] in ("red", "amber", "green") and len(risk["phases"]) == 9
    assert any(r["id"] == "permit-trench-excavation-5ft" for r in found["result"])
    assert one["parameters"]["max_downpayment_dollars"] == 1000 and one["verified"] is True
    # 811 for a Tue 1/5/2027 start with 1/1 a holiday: call by Wed 12/30/2026
    site = next(p for p in hol["phases"] if p["name"] == "Site setup & postings")
    assert next(i for i in site["items"] if i["title"].startswith("Mark the dig area"))["due_date"] == "2026-12-30"
    assert hol["holidays"] == ["2027-01-01"]
    # every hostile call is rejected with a message naming the problem
    assert len(errors) == 5 and "greater than 0" in errors[0] and "finite" in errors[1].lower()
    assert "residential_units >= 1" in errors[3] and "ISO date" in errors[4]
    props = schema["properties"]
    assert props["contract_price_usd"]["exclusiveMinimum"] == 0 and "holidays" in props
    assert all(p.get("description") for p in props.values())
