"""Trade certifications: certifications_required, check_crew_certifications, electrician_certification_path and
equipment_operating_permits.

Expected values are worked by hand from the rule text in research/web_trade-certs.verified.json (and the rrp- rules
of research/web_permits-building.verified.json); the arithmetic is in the comments. Numbers are never read back from
the module under test.
"""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, trade_certifications as tc

AS_OF = "2026-11-25"           # Wednesday


def ids(result):
    return {c.rule_id for c in result.citations}


def req(result, credential_prefix):
    return next(r for r in result.requirements if r.credential.startswith(credential_prefix))


def worker(result, name):
    return next(w for w in result.workers if w.name == name)


def finding(result, prefix):
    return next(f for f in result.crew_findings if f.item.startswith(prefix))


def item(result, label):
    return next(p for p in result.items if p.label == label)


# ---------------------------------------------------------------- rulebook wiring
def test_module_rules_exist_and_statuses_match_the_verified_pack():
    book = {r.id: r for r in core.all_rules()}
    assert set(tc.RULE_IDS) <= set(book)
    pack = {r.id for r in book.values() if r.pack == "web_trade-certs"}
    assert len(pack) == 108                                   # 105 + 3 added in the 2026-11-25 second-pass review
    used = set(tc.RULE_IDS) & pack
    assert len(used) >= 99                                    # 99 of the 108 pack rules drive a code path
    # the pack's 3 unverifiable rules; the 2 used ones make a result verified=False when cited
    assert book[tc.E_AB1707].status == "unverifiable" and book[tc.V_CCCM_ONLY].status == "unverifiable"
    assert book["cert-lead-blood-lead-20-triggers-calosha"].status == "unverifiable"
    assert book[tc.R_FIRM].status == "unchecked"              # web_permits-building rule, not verified
    for corrected in (tc.E_FEE, tc.E_WINDOW, tc.E_RENEW, tc.E_PRESUME, tc.E_APPEAL, tc.R_CLEAN, tc.R_EMERG,
                      tc.H_PERMIT, tc.PV_AIR, tc.V_CCCM_RENEW):
        assert book[corrected].status == "corrected" and book[corrected].verified
    assert all(book[i].verified for i in tc.RULE_IDS if i not in (tc.E_AB1707, tc.V_CCCM_ONLY, tc.R_FIRM))


def test_rulebook_values_match_the_regulation_text():
    # 8 CCR 292.0(a)-(b): $75 application + $100 exam = $175
    assert core.rule(tc.E_FEE).param("total_initial_usd") == 75 + 100
    # 8 CCR 291.5(a) corrected: "one year" before expiration (not 365 days); 291.2(e)/291.4: one year to test
    assert core.rule(tc.E_RENEW).param("may_renew_up_to_years_before_expiration") == 1
    assert core.rule(tc.E_WINDOW).param("eligibility_window_years") == 1
    # 8 CCR 1604.29(c) corrected: no 1-year cap, the literal term is kept
    assert "max_permit_years" not in core.rule(tc.H_PERMIT).parameters


# ---------------------------------------------------------------- spellings (not law)
@pytest.mark.parametrize("task,expected", [
    ("Panel upgrade and rewire kitchen", [("electrical", "general")]),
    ("pull wire", [("electrical", "general")]),
    ("install fire alarm devices", [("electrical", "fire_alarm")]),
    ("pull CAT6 data cabling", [("electrical", "low_voltage")]),
    ("LED lighting retrofit", [("electrical", "lighting_retrofit")]),     # not also generic 'lighting'
    ("scrape and repaint window trim", [("lead", None)]),
    ("sandblasting old paint", [("lead", None)]),                         # abrasive blasting is not explosives
    ("rock blasting for footings", [("blasting", None)]),
    ("Alimak construction elevator for crews", [("personnel_hoist", None)]),   # not an elevator
    ("install new hydraulic elevator", [("elevator", None)]),
    ("crane pick of RTUs", [("crane", None)]),
    ("air compressor for nailers", [("pressure_vessel", "air")]),
    ("propane tank for heaters", [("pressure_vessel", "lpg")]),
    ("lead carpenter framing", []),                                       # 'lead carpenter' is not lead work
    ("pour concrete footings", []),
])
def test_classify_task(task, expected):
    assert tc.classify_task(task) == expected


@pytest.mark.parametrize("value,expected", [
    ("C-10", {"C-10"}), ("c10, B", {"C-10", "B"}), (["C 7", "c-45"], {"C-7", "C-45"}),
    ("Electrical contractor", {"C-10"}), ("General Building", {"B"}), ("class A", {"A"}),
    ("C-61/D-28", {"C-61", "D-28"}),
])
def test_license_classes(value, expected):
    assert tc.classes_from(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("single family home", "single_family"), ("Apartment building", "multifamily"), ("duplex", "two_unit"),
    ("home daycare", "child_occupied"), ("high school", "school"), ("office building", "public_or_commercial"),
    ("Refinery", "industrial"), ("nursing home", "senior_or_care"), ("public_or_commercial", "public_or_commercial"),
])
def test_building_types(value, expected):
    assert tc.building_from(value) == expected


@pytest.mark.parametrize("city,expected", [
    ("LA", True), ("L.A.", True), (" City of Los Angeles ", True), ("los angeles, ca 90012", True),
    ("LA County", False), ("County of Los Angeles", False), ("SD", False), ("City of San Diego", False), (None, None),
])
def test_city_of_los_angeles(city, expected):
    assert tc.is_city_of_los_angeles(city) is expected


# ---------------------------------------------------------------- certifications_required: electrical
def test_c10_remodel_of_a_1962_house():
    r = tc.certifications_required(["Panel upgrade and rewire kitchen", "scrape and repaint window trim",
                                    "fire alarm devices"], employer_license_classes="C-10, B", year_built=1962,
                                   building_type="single family home", as_of=AS_OF)
    elec = req(r, "DIR electrician certification for electrical work")
    assert elec.status == "required" and elec.who == "each worker doing the work"
    # LC 108(c): connections of 100 VA or more; 8 CCR 290.1 lets a Residential Electrician work in a house
    assert "100 volt-amperes" in elec.why and "foremen" in elec.why
    assert elec.acceptable[0].startswith("General Electrician") and elec.acceptable[1].startswith("Residential")
    assert "120/208 or 120/240 volts" in elec.acceptable[1] and "Article 517" in elec.acceptable[1]
    # 8 CCR 292.0(a)-(b): $75 + $100 = $175 per worker; trainee registration $25 (292.0(d))
    assert elec.cost_usd == 175 and "$75 application + $100 exam = $175" in elec.cost_detail
    assert "$25" in elec.cost_detail and "each retake $100" in elec.cost_detail
    assert any("no more than 1 trainee" in a for a in elec.alternatives)
    assert "violation is presumed" in elec.proof_on_site and "60 days" in elec.proof_on_site
    fa = req(r, "DIR electrician certification for fire alarm")
    assert fa.acceptable[0].startswith("Fire/Life Safety Technician (NEC Article 760")
    # RRP: 1962 < 1978 -> pre-1978 target housing
    assert "year built 1962 < 1978 -> built before the RRP cutoff" in r.math
    ren = req(r, "EPA certified renovator")
    assert ren.status == "required" and r.pre_1978 is True
    # 745.90(a): refresher within 5 years; hands-on 5 years, without 3
    assert "refreshed within 5 years (5 years if the refresher had hands-on training, 3 without)" in ren.acceptable[0]
    # 745.85(a)(2): interior 6 ft floor cover; exterior 20 ft doors/windows, 10 ft ground cover; (b) 2 ft, < 40 sq ft
    assert "at least 6 ft beyond" in ren.how_to_get and "within 20 ft" in ren.how_to_get
    assert "at least 10 ft beyond" in ren.how_to_get and "clean 2 ft beyond" in ren.how_to_get
    assert "sections under 40 sq ft" in ren.how_to_get
    firm = req(r, "EPA RRP firm certification")
    # 745.92(b): $300 firm, $300 recertification every 5 years, $550 combined; 745.89(b): timely if >= 90 days
    assert firm.cost_usd == 300 and "$300 every 5 years" in firm.cost_detail and "$550" in firm.cost_detail
    assert "at least 90 days" in firm.how_to_get and firm.verified is False
    assert req(r, "CDPH lead certification").status == "conditional"
    # the only unverified rule is the unchecked firm-certification rule -> verified False
    assert r.verified is False and r.unverified_rule_ids == [tc.R_FIRM]
    assert any("$49,772 per RRP violation" in e for e in r.exposure)                    # 40 CFR 19.4 Table 1
    assert any("suspension or revocation" in e and "even 1 uncertified" in e for e in r.exposure)  # LC 108.2(h)
    assert r.required_count == 5 and r.conditional_count == 1
    assert r.next_step.startswith("DIR electrician certification for electrical work: Collect a copy")
    assert sum("Collect a copy of each electrician's DIR card" in s for s in r.next_steps) == 1   # deduplicated


def test_office_building_needs_general_not_residential():
    r = tc.certifications_required(["rough wiring"], "C-10", building_type="office", as_of=AS_OF)
    assert [a.split(" (")[0] for a in req(r, "DIR electrician").acceptable] == ["General Electrician"]
    assert r.verified is True and tc.E_RES not in ids(r)


def test_lighting_retrofit_in_a_store_accepts_nonresidential_lighting_technician():
    r = tc.certifications_required(["LED lighting retrofit"], "C-10", building_type="retail store", as_of=AS_OF)
    q = req(r, "DIR electrician")
    # 8 CCR 290.1: no branch-circuit work except reconnecting within 3 ft
    assert q.acceptable[0].startswith("Nonresidential Lighting Technician") and "within 3 ft" in q.acceptable[0]
    assert any(a.startswith("Nonresidential lighting trainee enrolled in DAS-approved") for a in q.alternatives)
    assert tc.E_NRLT_T in q.rule_ids


@pytest.mark.parametrize("classes,task,status,rule", [
    ("B", "rewire outlets", "not_required", tc.E_C10),               # LC 108.2(a): C-10 employers only
    (["C-10", "C-7"], "pull CAT6 data cabling", "not_required", tc.E_C7),   # 108.2(b)(2), even with C-10
    ("C-10, C-45", "neon sign installation", "not_required", tc.E_C7),
    (None, "install EV charger", "conditional", tc.E_C7),
    ("C-10", "distribution line work", "conditional", tc.E_UTIL),     # 108.2(b)(3)
])
def test_electrical_scope_and_license_exemptions(classes, task, status, rule):
    r = tc.certifications_required([task], classes, as_of=AS_OF)
    q = req(r, "DIR electrician")
    assert q.status == status and rule in q.rule_ids


def test_out_of_state_electricians_have_no_grace_period():
    r = tc.certifications_required(["wiring"], "C-10", out_of_state_workers=True, as_of=AS_OF)
    assert "no grace period" in req(r, "DIR electrician").why and tc.E_NOGRACE in ids(r)


def test_ab1707_is_cited_only_on_or_after_its_transfer_date():
    before = tc.certifications_required(["wiring"], "C-10", as_of="2028-06-30")
    after = tc.certifications_required(["wiring"], "C-10", as_of="2028-07-01")    # transfer_effective 2028-07-01
    assert tc.E_AB1707 not in ids(before) and before.verified is True
    assert tc.E_AB1707 in ids(after) and after.verified is False and tc.E_AB1707 in after.unverified_rule_ids


# ---------------------------------------------------------------- certifications_required: lead
@pytest.mark.parametrize("year,status", [(1977, "required"), (1978, "not_required")])   # "built before 1978"
def test_rrp_cutoff_year(year, status):
    r = tc.certifications_required(["repaint"], "B", year, "house", as_of=AS_OF)
    assert r.requirements[0].status == status


@pytest.mark.parametrize("interior,exterior,status", [
    (6, None, "not_required"),      # 40 CFR 745.83: 6 sq ft or less per room inside -> minor repair
    (6.5, None, "required"),
    (None, 20, "not_required"),     # 20 sq ft or less outside
    (4, 21, "required"),
])
def test_rrp_minor_repair_limits(interior, exterior, status):
    r = tc.certifications_required(["patch and repaint"], "B", 1960, "apartment", as_of=AS_OF,
                                   interior_painted_sqft_per_room=interior, exterior_painted_sqft=exterior)
    assert r.requirements[0].status == status and tc.R_MINOR in ids(r)


def test_window_replacement_is_never_minor_repair():
    r = tc.certifications_required(["window replacement"], "B", 1960, "house", interior_painted_sqft_per_room=2,
                                   as_of=AS_OF)
    assert req(r, "EPA certified renovator").status == "required"
    assert any("window replacement is never minor repair" in m for m in r.math)


def test_lead_free_determination_and_industrial_buildings():
    r = tc.certifications_required(["scraping"], "B", 1950, "house", lead_free_determination=True, as_of=AS_OF)
    assert r.requirements[0].status == "not_required" and "1 mg/cm2 or 0.5% by weight" in r.requirements[0].why
    ind = tc.certifications_required(["paint stripping"], "B", 1950, "refinery", lead_exposure_ug_m3=80, as_of=AS_OF)
    assert [q.status for q in ind.requirements] == ["not_required", "not_required"]   # 1532.1(l)(3): not industrial


@pytest.mark.parametrize("exposure,status", [(50, "required"), (49.9, "not_required"), (None, "conditional")])
def test_cdph_certification_at_50_ug_m3(exposure, status):
    # 8 CCR 1532.1(l)(3): "at or above 50 ug/m3 as an 8-hour TWA"
    r = tc.certifications_required(["demolition of plaster"], "B", 1985, "office", lead_exposure_ug_m3=exposure,
                                   as_of=AS_OF)
    q = req(r, "CDPH lead certification")
    assert q.status == status
    if status == "required":
        assert q.issuer == "California Department of Public Health (CDPH)" and "EPA-only" in q.how_to_get


def test_child_occupied_facility_with_dust_clearance_and_emergency():
    r = tc.certifications_required(["remodel classroom"], "B", 1970, "preschool", dust_clearance_testing=True,
                                   emergency_renovation=True, as_of=AS_OF)
    notice = req(r, "Child-occupied facility notices")
    # 745.84(c): no more than 60 days before; certificate of mailing at least 7 days before
    assert "No more than 60 days" in notice.why and "at least 7 days before" in notice.how_to_get
    dust = req(r, "Dust clearance sampling")
    # 745.85(c) / 745.227(e)(8)(viii): from 2026-01-12 floors 5, sills 40, troughs 100 ug/sq ft
    assert "floors 5 ug/sq ft, interior window sills 40 ug/sq ft, window troughs 100 ug/sq ft" in dust.why
    # 745.82(b) corrected: interim controls for a child's blood lead get only the pamphlet exemption
    assert "only the pamphlet exemption" in req(r, "EPA certified renovator").why


def test_dust_clearance_levels_before_2026_01_12_and_tsca_penalty_dates():
    old = tc.certifications_required(["remodel"], "B", 1970, "house", dust_clearance_testing=True, as_of="2025-12-01")
    assert "floors 10 ug/sq ft, interior window sills 100 ug/sq ft, window troughs 400" in req(old, "Dust").why
    early = tc.certifications_required(["remodel"], "B", 1970, "house", as_of="2025-01-07")
    late = tc.certifications_required(["remodel"], "B", 1970, "house", as_of="2025-01-08")
    assert any("$48,512" in e for e in early.exposure) and any("$49,772" in e for e in late.exposure)


# ---------------------------------------------------------------- certifications_required: other trades
def test_crane_elevator_blasting_hoist_and_pressure_vessels():
    r = tc.certifications_required(["crane pick", "install passenger elevator", "rock blasting",
                                    "buck hoist for crews", "propane tank for heaters"], "A, B, C-11",
                                   building_type="office", city="L.A.", crane_outside_construction=True, as_of=AS_OF)
    crane = req(r, "Crane operator")
    assert "at most 5 years" in crane.acceptable[1]                         # 5006.2(e)(3)(B)
    assert "under 25 ft" in crane.why and "under 15,000 lb" in crane.why   # 5006.1 exceptions
    cccm = req(r, "Certified Competent Conveyance Mechanic")
    assert cccm.cost_usd == 210 and "+$100" in cccm.cost_detail and "3 years" in cccm.acceptable[0]
    cqcc = req(r, "Certified Qualified Conveyance Company")
    assert cqcc.cost_usd == 700 and "$140 renewal every 2 years" in cqcc.cost_detail
    assert "$1,000,000 per occurrence" in cqcc.why and "$500,000" in cqcc.why
    blast = req(r, "Licensed blaster")
    assert "at least 21" in blast.how_to_get and "3 years" in blast.how_to_get and "5 years" in blast.how_to_get
    assert "D (Demolition)" in blast.acceptable[0]
    assert "every 3 months" in req(r, "DOSH permit to operate for the personnel hoist").how_to_get
    lpg = req(r, "Permit to operate for LP-gas")
    assert lpg.issuer == "Los Angeles Department of Building and Safety" and "over 125 gallons" in lpg.why
    # LC 7311.2 (only a CCCM of a CQCC may work) could not be read -> unverifiable -> whole result unverified
    assert tc.V_CCCM_ONLY in r.unverified_rule_ids and r.verified is False


def test_home_elevator_is_outside_the_elevator_safety_orders():
    r = tc.certifications_required(["install home elevator"], "C-11", building_type="single family", as_of=AS_OF)
    assert r.requirements[0].status == "not_required" and r.verified is True and tc.V_CCCM_ONLY not in ids(r)


def test_unrecognized_tasks_are_listed():
    r = tc.certifications_required(["pour footings", "wiring"], "C-10", as_of=AS_OF)
    assert r.unrecognized_tasks == ["pour footings"] and "tasks not recognized: pour footings" in r.summary


@pytest.mark.parametrize("kwargs,msg", [
    ({"tasks": []}, "tasks is required"),
    ({"tasks": ["   "]}, "tasks is required"),
    ({"employer_license_classes": "zz9"}, "could not read 'zz9'"),
    ({"building_type": "spaceship"}, "building_type 'spaceship' not recognized"),
    ({"year_built": 1950, "built_before_1978": False}, "disagree"),
    ({"lead_exposure_ug_m3": -1}, "cannot be below 0"),
    ({"lead_exposure_ug_m3": "lots"}, "must be a number"),
    ({"year_built": 2999}, "cannot be above"),
    ({"as_of": "next tuesday"}, "as_of must be a date"),
])
def test_certifications_required_rejects_bad_inputs(kwargs, msg):
    args = {"tasks": ["wiring"]} | kwargs
    with pytest.raises(ValueError, match=msg):
        tc.certifications_required(**args)


# ---------------------------------------------------------------- check_crew_certifications: electricians
def test_electrician_renewal_dates_and_statuses():
    crew = [
        {"name": "Ana", "task": "pull wire", "credentials": ["General Electrician exp 2027-05-31"]},
        {"name": "Bo", "task": "pull wire", "credentials": ["General Electrician exp 01/15/2027"]},
        {"name": "Cy", "task": "pull wire", "credentials": [{"type": "GE", "expires": "2026-12-10"}]},
        {"name": "Di", "task": "pull wire", "credentials": [{"type": "General Electrician", "expires": "2026-11-01"}]},
        {"name": "Ed", "task": "pull wire", "credentials": [{"type": "General Electrician", "issued": "2024-01-15"}]},
    ]
    r = tc.check_crew_certifications(crew, "C-10", building_type="office", as_of=AS_OF)
    ana = worker(r, "Ana").credentials[0]
    # 8 CCR 291.5(b): received 30 days before the end -> 2027-05-31 - 30 days = 2027-05-01;
    # FAQ (291.5(a) corrected): renewals accepted 1 year before -> from 2026-05-31
    assert (ana.status, ana.renew_by, ana.renewal_window_opens) == ("valid", "2027-05-01", "2026-05-31")
    assert "32 hours" in ana.renewal and "2,000 industry hours" in ana.renewal and "$100" in ana.renewal
    assert worker(r, "Ana").status == "ok" and worker(r, "Ana").can_do_task is True
    # 2027-01-15 - 30 days = 2026-12-16, 21 days after as_of (within the 60-day display window)
    bo = worker(r, "Bo")
    assert (bo.status, bo.credentials[0].renew_by, bo.can_do_task) == ("renew_soon", "2026-12-16", True)
    # 2026-12-10 - 30 = 2026-11-10 < as_of, card still valid until 2026-12-10
    cy = worker(r, "Cy")
    assert (cy.status, cy.credentials[0].renew_by, cy.can_do_task) == ("renewal_overdue", "2026-11-10", True)
    assert cy.next_step.startswith("File Cy's renewal today")
    # 291.5(c) lapse -> retest; DIR fee page: $100 exam + $100 renewal = $200, no CE
    di = worker(r, "Di")
    assert (di.status, di.can_do_task) == ("expired", False)
    assert any("$200" in i and "retake and pass the exam" in i for i in di.issues)
    # issued 2024-01-15 + 3 years (291.5(a)) = 2027-01-15
    assert worker(r, "Ed").credentials[0].expires == "2027-01-15"
    assert any("Di here" in e for e in r.exposure)                         # LC 108.2(h)
    assert any("barred for 7 years" in e for e in r.exposure)               # 8 CCR 294.0
    assert r.cleared_to_start is False and r.counts["expired"] == 1


def test_residential_electrician_depends_on_the_building():
    crew = [{"name": "Rae", "task": "rough wiring", "credentials": ["Residential Electrician exp 2028-01-01"]}]
    house = tc.check_crew_certifications(crew, "C-10", building_type="house", as_of=AS_OF)
    office = tc.check_crew_certifications(crew, "C-10", building_type="office", as_of=AS_OF)
    unknown = tc.check_crew_certifications(crew, "C-10", as_of=AS_OF)
    assert worker(house, "Rae").status == "ok"
    assert worker(office, "Rae").status == "not_qualified" and worker(office, "Rae").can_do_task is False
    assert "does not cover electrical work here" in worker(office, "Rae").issues[0]
    assert worker(unknown, "Rae").status == "confirm"


def test_fire_alarm_tech_cannot_do_general_wiring():
    crew = [{"name": "Flo", "task": "fire alarm devices and rewire the panel",
             "credentials": ["Fire/Life Safety exp 2028-01-01"]}]
    r = tc.check_crew_certifications(crew, "C-10", building_type="office", as_of=AS_OF)
    assert worker(r, "Flo").status == "not_qualified"            # FLS covers the alarm part only (8 CCR 290.1)


def test_trainee_supervision_one_to_one():
    base = [{"name": "Gil", "task": "pull wire", "credentials": ["General Electrician exp 2028-01-01"]}]
    t1 = {"name": "Tia", "task": "pull wire", "credentials": [{"type": "electrician trainee", "issued": "2026-02-01"}],
          "supervised_by": "Gil"}
    t2 = {"name": "Tom", "task": "pull wire", "credentials": ["trainee T12345 exp 2027-06-01"], "supervised_by": "Gil"}
    ok = tc.check_crew_certifications(base + [t1], "C-10", as_of=AS_OF)
    tia = worker(ok, "Tia")
    # 8 CCR 296.1(e): annual renewal -> 2026-02-01 + 1 year = 2027-02-01
    assert tia.status == "ok_with_supervision" and tia.credentials[0].renew_by == "2027-02-01"
    assert "150 classroom hours" in tia.credentials[0].renewal and "registration is cancelled" in \
        tia.credentials[0].renewal
    assert finding(ok, "Electrician trainee supervision").status == "ok"
    bad = tc.check_crew_certifications(base + [t1, t2], "C-10", as_of=AS_OF)
    f = finding(bad, "Electrician trainee supervision")
    # 296.3(a): a certified electrician supervises no more than 1 trainee -> Gil with 2 is over
    assert f.status == "problem" and "Gil supervises 2 trainees" in f.detail and "the limit is 1" in f.detail
    assert tc.E_BARRED in f.rule_ids and bad.cleared_to_start is False


def test_unassigned_trainees_are_counted_against_free_certified_electricians():
    crew = [{"name": "Gil", "task": "wiring", "credentials": ["General Electrician exp 2028-01-01"]},
            {"name": "Ap", "task": "wiring", "credentials": ["registered apprentice"]},
            {"name": "T1", "task": "wiring", "credentials": ["trainee exp 2027-06-01"]},
            {"name": "T2", "task": "wiring", "credentials": ["trainee exp 2027-06-01"]}]
    r = tc.check_crew_certifications(crew, "C-10", as_of=AS_OF)
    # 2 trainees vs 1 certified electrician x 1 = 1 slot (the apprentice does not count either way)
    assert "trainees without a named supervisor 2 vs certified electricians without a trainee 1 x 1 = 1" in r.math
    assert finding(r, "Electrician trainee supervision").status == "problem"
    assert worker(r, "Ap").status == "ok_with_supervision"


def test_qualifier_and_non_c10_employers():
    crew = [{"name": "Q", "task": "panel change", "credentials": [], "is_c10_qualifier": True},
            {"name": "N", "task": "panel change", "credentials": []}]
    r = tc.check_crew_certifications(crew, "C-10", as_of=AS_OF)
    assert worker(r, "Q").status == "ok" and worker(r, "N").status == "not_qualified"     # LC 108.2(g)
    b = tc.check_crew_certifications(crew[1:], "B", as_of=AS_OF)
    assert worker(b, "N").status == "not_required" and b.cleared_to_start is True


# ---------------------------------------------------------------- check_crew_certifications: other trades
def test_rrp_renovator_terms_and_crew_checks():
    crew = [{"name": "Ren", "task": "scrape paint", "credentials": [{"type": "EPA certified renovator",
                                                                       "issued": "2022-03-01"}]},
            {"name": "R3", "task": "scrape paint", "credentials": [{"type": "RRP refresher", "issued": "2024-06-01",
                                                                      "hands_on": False}]},
            {"name": "R5", "task": "scrape paint", "credentials": [{"type": "RRP refresher", "issued": "2024-06-01",
                                                                      "hands_on": True}]},
            {"name": "R?", "task": "scrape paint", "credentials": [{"type": "RRP refresher", "issued": "2024-06-01"}]},
            {"name": "Hel", "task": "sanding trim", "credentials": ["trained by certified renovator"]}]
    r = tc.check_crew_certifications(crew, "B", 1955, "house", lead_exposure_ug_m3=12, as_of=AS_OF,
                                     firm_rrp_certification_expires="2027-03-15")
    # 745.90(a): initial + 5 years = 2027-03-01; refresher without hands-on 3 years, with hands-on 5 years
    assert worker(r, "Ren").credentials[0].expires == "2027-03-01"
    assert worker(r, "R3").credentials[0].expires == "2027-06-01"
    assert worker(r, "R5").credentials[0].expires == "2029-06-01"
    assert worker(r, "R?").credentials[0].expires == "2027-06-01"          # hands-on not stated -> shorter term
    assert any("hands-on not stated" in m for m in r.math)
    assert worker(r, "Hel").status == "ok_with_supervision"
    assert finding(r, "EPA certified renovator").status == "ok"
    # 745.89(b)(i): timely if >= 90 days before 2027-03-15 -> 2026-12-15
    firm = finding(r, "EPA RRP firm certification")
    assert firm.status == "ok" and "filed by 2026-12-15 is timely" in firm.detail
    assert r.cleared_to_start is True and r.verified is False            # rrp-firm-certification is unchecked


def test_rrp_job_without_a_certified_renovator_and_late_firm_recertification():
    crew = [{"name": "Hel", "task": "sanding trim", "credentials": ["trained by certified renovator"]}]
    r = tc.check_crew_certifications(crew, "B", 1955, "house", lead_exposure_ug_m3=12, as_of=AS_OF,
                                     firm_rrp_certification_expires="2027-01-31")
    assert finding(r, "EPA certified renovator").status == "problem"
    # 2027-01-31 - 90 days = 2026-11-02, already past on 2026-11-25
    assert finding(r, "EPA RRP firm certification").status == "problem"
    assert "firm RRP recertification timely by 2027-01-31 - 90 days = 2026-11-02" in r.math
    assert any("$49,772" in e for e in r.exposure) and r.cleared_to_start is False


def test_crane_operators_evaluation_and_trainees():
    crew = [{"name": "Op", "task": "crane operator", "employer_evaluated": True,
             "credentials": [{"type": "NCCCO mobile crane", "expires": "2029-06-30"}]},
            {"name": "NoEval", "task": "crane", "employer_evaluated": False, "credentials": ["NCCCO exp 2029-01-01"]},
            {"name": "Unk", "task": "crane", "credentials": ["NCCCO exp 2029-01-01"]},
            {"name": "Gov", "task": "crane", "employer_evaluated": True,
             "credentials": [{"type": "city crane license", "issued": "2023-01-01", "jurisdiction": "Los Angeles"}]},
            {"name": "Oit", "task": "crane", "credentials": ["crane operator in training"], "supervised_by": "Op"},
            {"name": "Oit2", "task": "crane", "credentials": ["operator-in-training"]}]
    r = tc.check_crew_certifications(crew, "A", as_of=AS_OF)
    assert worker(r, "Op").status == "ok"
    assert worker(r, "NoEval").status == "not_qualified"                     # 5006.2(f)
    assert worker(r, "Unk").status == "confirm"
    # 5006.2(e)(3)(B): no longer than 5 years -> 2023-01-01 + 5 = 2028-01-01
    gov = worker(r, "Gov")
    assert gov.credentials[0].expires == "2028-01-01" and "only within the issuing jurisdiction" in gov.issues[0]
    assert finding(r, "Crane operator-in-training Oit").status == "ok"
    assert finding(r, "Crane operator-in-training Oit2").status == "problem"


def test_conveyance_mechanics_and_blasters():
    crew = [{"name": "Mech", "task": "elevator install", "credentials": [{"type": "CCCM", "issued": "2025-06-01"}]},
            {"name": "Temp", "task": "elevator install", "credentials": [{"type": "TCCCM", "issued": "2026-08-01"}]},
            {"name": "Helper", "task": "rock blasting", "credentials": []}]
    r = tc.check_crew_certifications(crew, "C-11, A", as_of=AS_OF)
    mech = worker(r, "Mech").credentials[0]
    # CCCM valid 2 years: 2025-06-01 + 2 = 2027-06-01; renewal $140 with 8 h CE in the 1 year before (on/after
    # 2026-06-01)
    assert mech.expires == "2027-06-01" and "$140" in mech.renewal and "on or after 2026-06-01" in mech.renewal
    # TCCCM: first term up to 6 months -> 2026-08-01 + 6 months = 2027-02-01
    assert worker(r, "Temp").credentials[0].expires == "2027-02-01"
    assert worker(r, "Helper").status == "ok_with_supervision"
    assert finding(r, "Licensed blaster").status == "problem"               # 8 CCR 5238(a)
    assert finding(r, "Elevator company is a CQCC").status == "confirm"
    lic = tc.check_crew_certifications(crew[2:] + [{"name": "Bl", "task": "blasting",
                                                     "credentials": [{"type": "blaster license class D",
                                                                      "issued": "2023-04-01"}]}], "A", as_of=AS_OF)
    # 344.21(a): valid five years -> 2023-04-01 + 5 = 2028-04-01
    assert worker(lic, "Bl").credentials[0].expires == "2028-04-01"
    assert finding(lic, "Licensed blaster").status == "ok" and lic.cleared_to_start is True


@pytest.mark.parametrize("crew,msg", [
    ([], "non-empty list"),
    ([{"name": "A"}], "crew member 1 .*task"),
    (["Ana"], "must be an object"),
    ([{"name": "A", "task": "wiring"}, {"name": "A", "task": "wiring"}], "unique"),
    ([{"name": "A", "task": "wiring", "credentials": [{"type": "GE", "expires": "soon"}]}], "expires must be a date"),
    ([{"name": "A", "task": "wiring", "credentials": [""]}], "credential 1 is empty"),
])
def test_crew_rejects_bad_inputs(crew, msg):
    with pytest.raises(ValueError, match=msg):
        tc.check_crew_certifications(crew, "C-10", as_of=AS_OF)


def test_unrecognized_credentials_and_tasks():
    r = tc.check_crew_certifications([{"name": "Z", "task": "pour concrete", "credentials": ["OSHA 10"]},
                                      {"name": "Y", "task": "wiring", "credentials": ["journeyman"]}], "C-10",
                                     as_of=AS_OF)
    assert worker(r, "Z").status == "unrecognized_task" and worker(r, "Z").credentials[0].status == "not_recognized"
    assert worker(r, "Y").status == "missing_info"                         # type of DIR certification not given


# ---------------------------------------------------------------- electrician_certification_path
def test_general_electrician_hours_with_area_caps():
    r = tc.electrician_certification_path("General", {"commercial wiring": 5200, "Residential": 3500,
                                                      "stock room": 500, "fire alarm": 200}, as_of=AS_OF)
    # 8 CCR 291.1(a)(2): 5,200 + min(3,500, 3,000) + min(500, 300) + min(200, 600) = 8,700 >= 8,000; 4 areas >= 2
    assert r.counted_hours == 8700 and r.shortfall_hours == 0 and r.experience_met is True
    assert r.route == "on_the_job_hours" and r.min_areas == 2 and r.areas_with_counted_hours == 4
    res = next(h for h in r.hours if h.area == "residential_wiring")
    assert (res.cap, res.counted, res.not_counted) == (3000, 3000, 500)
    assert r.total_to_apply_usd == 175 and [f.amount_usd for f in r.fees] == [75, 100, 100]
    # 8 CCR 291.3: General 100 questions, 4 hours, 70%
    assert r.exam["questions"] == 100 and r.exam["time_hours"] == 4 and r.exam["passing_score_pct"] == 70
    assert "Social Security Administration employment history report" in r.documents[0]
    assert r.verified is True and r.next_step.startswith("Mail the General Electrician application")


def test_general_electrician_needs_two_areas_and_the_caps_bind():
    one = tc.electrician_certification_path("GE", {"commercial": 9000}, as_of=AS_OF)
    # min(9,000, 6,000) = 6,000 < 8,000 -> short 2,000; and only 1 area
    assert one.counted_hours == 6000 and one.shortfall_hours == 2000 and one.experience_met is False
    assert one.trainee_route and "$25" in one.trainee_route and "150 classroom hours" in one.trainee_route
    exact = tc.electrician_certification_path("GE", {"commercial": 6000, "industrial": 2000}, as_of=AS_OF)
    assert exact.counted_hours == 8000 and exact.experience_met is True        # 6,000 + 2,000 = 8,000 exactly


def test_residential_electrician_does_not_count_commercial_hours():
    r = tc.electrician_certification_path("Residential Electrician", {"residential wiring": 4000, "commercial": 3000,
                                                                      "underground": 500, "troubleshooting": 800},
                                          eligibility_notice_date="2026-03-01", last_exam_date="2026-10-20",
                                          as_of=AS_OF)
    # 4,000 + min(500, 300) + min(800, 600) = 4,900 >= 4,800; commercial is not a listed Residential area
    assert r.counted_hours == 4900 and r.experience_met is True
    assert not next(h for h in r.hours if h.given_as == ["commercial"]).listed
    # 291.4: one year after the 2026-03-01 notice -> 2027-03-01; 60 days after 2026-10-20 -> 2026-12-19
    assert (r.exam_window_ends, r.next_exam_allowed_on, r.exam_window_expired) == ("2027-03-01", "2026-12-19", False)
    assert r.exam["questions"] == 80 and r.exam["time_hours"] == 3


@pytest.mark.parametrize("ctype,hours,counted,met", [
    ("VDV", {"low voltage installs": 3800, "trade specific training": 400}, 4100, True),   # 3,800 + min(400, 300)
    ("Nonresidential Lighting", {"maintenance of lighting fixtures": 1750, "retrofit": 400}, 2150, True),
    ("Fire/Life Safety", {"installations": 3500, "nurse call": 500}, 3800, False),         # 3,500 + min(500, 300)
])
def test_specialty_hours(ctype, hours, counted, met):
    r = tc.electrician_certification_path(ctype, hours, as_of=AS_OF)
    assert r.counted_hours == counted and r.experience_met is met


@pytest.mark.parametrize("kwargs,route,met", [
    ({"certification_type": "FLS", "nicet_fire_alarm_level": 2}, "nicet", True),        # 291.1: Level II or above
    ({"certification_type": "FLS", "nicet_fire_alarm_level": 1}, "not_yet_eligible", False),
    ({"certification_type": "GE", "months_left_in_apprenticeship": 12}, "apprentice_final_year", True),  # LC 108.2(d)
    ({"certification_type": "GE", "months_left_in_apprenticeship": 13}, "not_yet_eligible", False),
    ({"certification_type": "GE", "completed_apprenticeship": True}, "apprenticeship_completed", True),
    ({"certification_type": "GE", "out_of_state_license": True}, "out_of_state_license_review", None),
])
def test_alternative_routes(kwargs, route, met):
    r = tc.electrician_certification_path(as_of=AS_OF, **kwargs)
    assert r.route == route and r.experience_met is met


def test_exam_window_expired_and_leap_day():
    r = tc.electrician_certification_path("GE", completed_apprenticeship=True, eligibility_notice_date="2025-10-01",
                                          as_of=AS_OF)
    assert r.exam_window_ends == "2026-10-01" and r.exam_window_expired is True and "new application" in r.summary
    leap = tc.electrician_certification_path("GE", completed_apprenticeship=True, eligibility_notice_date="2024-02-29",
                                             as_of="2024-06-01")
    assert leap.exam_window_ends == "2025-02-28"         # one calendar year, Feb 29 -> Feb 28 (not 365 days)


def test_path_cites_ab1707_after_the_transfer_date():
    r = tc.electrician_certification_path("GE", completed_apprenticeship=True, as_of="2028-07-01")
    assert tc.E_AB1707 in r.unverified_rule_ids and r.verified is False


@pytest.mark.parametrize("kwargs,msg", [
    ({"certification_type": "plumber"}, "not recognized"),
    ({"certification_type": "GE", "hours_by_area": {"commercial": -5}}, "cannot be below 0"),
    ({"certification_type": "GE", "hours_by_area": "lots"}, "hours_by_area must be like"),
    ({"certification_type": "GE", "last_exam_date": "2026-12-01"}, "after as_of"),
    ({"certification_type": "GE", "eligibility_notice_date": "2026-06-01", "last_exam_date": "2026-05-01"},
     "before eligibility_notice_date"),
])
def test_path_rejects_bad_inputs(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        tc.electrician_certification_path(as_of=AS_OF, **kwargs)


# ---------------------------------------------------------------- equipment_operating_permits
def test_air_tanks():
    r = tc.equipment_operating_permits([
        {"kind": "air compressor", "label": "Towable", "volume_gallons": 30, "safety_valve_psi": 150, "portable": True,
         "mawp_psi": 200, "last_inspected_on": "2023-10-01", "inspection_hours": 1.1},
        {"kind": "pancake compressor", "label": "Pancake", "volume_gallons": 6, "safety_valve_psi": 135},
        {"kind": "air tank", "label": "Edge", "volume_cu_ft": 1.5, "safety_valve_psi": 150},
        {"kind": "air tank", "label": "HiValve", "volume_cu_ft": 1.2, "safety_valve_psi": 200},
        {"kind": "air tank", "label": "NoValve", "volume_cu_ft": 1.0},
        {"kind": "air receiver", "label": "Stationary", "volume_cu_ft": 20, "mawp_psi": 150, "asme_stamped": True},
        {"kind": "air receiver", "label": "Fixed", "volume_cu_ft": 40, "portable": False,
         "last_inspected_on": "2023-10-01"},
        {"kind": "air receiver", "label": "Unsure", "volume_cu_ft": 40, "last_inspected_on": "2023-10-01"},
    ], city="San Diego", as_of=AS_OF)
    tow = item(r, "Towable")
    # 30 gal / (1728/231) = 4.010 cu ft > 1.5 (461(f)); MAWP 200 > 150 so no indefinite permit (461(h)); portable
    # permit <= 3 years from inspection (461(d)): 2023-10-01 + 3 = 2026-10-01 < as_of -> overdue
    assert tow.status == "permit_required" and tow.permit_expires_by == "2026-10-01" and tow.overdue is True
    assert any("4.010 cu ft > 1.5 cu ft" in m for m in tow.math)
    # 344.2(b) $45 permit + 344.1(a) $150/h billed in 0.25 h steps: 1.1 h -> 1.25 h x $150 = $187.50
    assert [(f.item, f.amount_usd) for f in tow.fees] == [("Permit to operate processing", 45),
                                                          ("DOSH inspection", 187.5)]
    assert tow.one_time_fees_usd == 232.5
    assert item(r, "Pancake").status == "exempt"                         # 6 gal = 0.802 cu ft, 135 <= 150 psi
    assert item(r, "Edge").status == "exempt"                            # "1 1/2 cubic feet or less", "not more than 150"
    assert item(r, "HiValve").status == "permit_required"                # corrected rule: both conditions needed
    assert item(r, "NoValve").status == "need_info"
    stat = item(r, "Stationary")
    assert stat.status == "permit_required" and stat.permit_term.startswith("indefinite")   # 461(h)
    # 461(d): non-portable up to 5 years -> 2023-10-01 + 5 = 2028-10-01; portability unknown -> shorter 3 years
    assert item(r, "Fixed").permit_expires_by == "2028-10-01" and item(r, "Unsure").permit_expires_by == "2026-10-01"
    assert "portable assumed" in item(r, "Unsure").permit_term
    assert r.verified is True and any("$55" in n and "$180" in n for n in r.notes)          # proposed, not filed
    assert r.next_step.startswith("Towable: inspection/permit overdue since 2026-10-01")


def test_lpg_tanks_and_boilers():
    r = tc.equipment_operating_permits([
        {"kind": "propane", "label": "125", "volume_gallons": 125},
        {"kind": "propane tank", "label": "126", "volume_gallons": 126, "lpg_use": "storage",
         "last_inspected_on": "2024-01-10"},
        {"kind": "LPG", "label": "skid", "volume_gallons": 500, "lpg_use": "skid tank"},
        {"kind": "propane", "label": "DOT", "volume_gallons": 400, "dot_cylinder": True},
        {"kind": "boiler", "label": "power", "steam_psig": 125, "last_inspected_on": "2025-12-01"},
        {"kind": "boiler", "label": "lowp", "steam_psig": 15, "safety_valve_psi": 15},
        {"kind": "boiler", "label": "lowp-novalve", "steam_psig": 15},
        {"kind": "boiler", "label": "lowp-hivalve", "steam_psig": 12, "safety_valve_psi": 30},
        {"kind": "boiler", "label": "mini", "boiler_type": "miniature"},
        {"kind": "boiler", "label": "small", "heating_surface_sqft": 100, "steam_drum_inside_diameter_in": 16,
         "mawp_psi": 100, "water_capacity_gallons": 35, "burner_input_btu_per_hr": 400000},
        {"kind": "boiler", "label": "unknown"},
    ], as_of=AS_OF)
    assert item(r, "125").status == "exempt"                             # 470: "125 gallons capacity or less"
    tank = item(r, "126")
    # storage tanks up to 5 years: 2024-01-10 + 5 = 2029-01-10
    assert tank.status == "permit_required" and tank.permit_expires_by == "2029-01-10"
    assert item(r, "skid").permit_term.startswith("up to 3 years")
    assert item(r, "DOT").status == "exempt"
    power = item(r, "power")
    # 770(a): yearly inspection: 2025-12-01 + 1 year = 2026-12-01 (not yet overdue)
    assert power.status == "permit_required" and power.next_inspection_due == "2026-12-01" and not power.overdue
    # 8 CCR 753: low-pressure = steam pressure AND steam safety valve setting not exceeding 15 psi
    assert item(r, "lowp").status == "exempt"
    assert item(r, "lowp-novalve").status == "need_info" and "safety_valve_psi" in item(r, "lowp-novalve").why
    assert item(r, "lowp-hivalve").status == "permit_required"
    assert item(r, "mini").status == "exempt" and item(r, "small").status == "exempt"   # 771(a), all limits "<="
    assert item(r, "unknown").status == "need_info"
    assert r.permits_required == 4


def test_personnel_hoists():
    r = tc.equipment_operating_permits([
        {"kind": "personnel hoist", "label": "Hoist", "last_inspected_on": "2026-09-01", "inspection_hours": 3.5},
        {"kind": "construction elevator", "label": "Bridge", "bridge": True, "last_inspected_on": "2026-11-20"},
    ], as_of=AS_OF)
    h = item(r, "Hoist")
    # 1604.26(b): every 3 months -> 2026-12-01; 344.30(b): $225/h "or any fraction": 3.5 h -> 4 h x 225 = $900
    assert h.next_inspection_due == "2026-12-01" and h.overdue is False
    assert h.fees[0].amount_usd == 900 and h.fees[1].amount_usd == 225 and h.fees[1].when == "recurring"
    assert "one year or for the length of time" in h.permit_term                     # corrected 1604.29(c)
    assert any("$25 for 4 to 8 landings" in m for m in h.math)
    # bridge construction elevators: weekly -> 2026-11-20 + 7 days = 2026-11-27; winds over 35 mph
    b = item(r, "Bridge")
    assert b.next_inspection_due == "2026-11-27" and "35 mph" in b.why


def test_elevator_fees():
    r = tc.equipment_operating_permits([
        {"kind": "elevator", "label": "Hyd", "drive": "hydraulic", "stories": 4, "units": 2},
        {"kind": "elevator", "label": "Trac", "drive": "traction", "stories": 12, "inspection_hours": 6.2},
        {"kind": "escalator", "label": "Esc", "units": 3},
        {"kind": "dumbwaiter", "label": "DW", "units": 2},
        {"kind": "wheelchair lift", "label": "WL"},
        {"kind": "elevator", "label": "Alt3", "drive": "hydraulic", "alterations": 3, "units": 2},
        {"kind": "elevator", "label": "Alt2", "drive": "hydraulic", "alterations": 2},
        {"kind": "elevator", "label": "NoDrive"},
    ], building_type="office", as_of=AS_OF)
    # 344.30(i)(1)-(2): $225 x 4 stories + $225 per additional similar unit = $1,125
    hyd = item(r, "Hyd")
    assert hyd.fees[0].amount_usd == 1125 and hyd.one_time_fees_usd == 1125
    assert next(f for f in hyd.fees if f.item == "Periodic reinspection").amount_usd == 450       # 2 x $225
    # (i)(3): $350 x 12 = $4,200; (b): 6.2 h -> 7 h x $225 = $1,575; (a): cabled 11-20 stories $450
    trac = item(r, "Trac")
    # (g): permit processing and mailing, actual cost up to $15 per permit (recurring, not in the one-time total)
    assert [f.amount_usd for f in trac.fees] == [4200, 1575, 450, 15] and trac.one_time_fees_usd == 5775
    # (i)(7)-(8): $900 + 2 x $225 = $1,350; (a): $675 x 3 = $2,025
    assert [f.amount_usd for f in item(r, "Esc").fees] == [1350, 225, 2025, 15]
    assert item(r, "DW").fees[0].amount_usd == 1100                      # (i)(5): $550 x 2
    assert item(r, "WL").fees[0].amount_usd == 450                       # (i)(6)
    # (j)(3)-(4): $900 + $225 = $1,125; (j)(1): one or two alterations $450
    assert item(r, "Alt3").fees[0].amount_usd == 1125 and item(r, "Alt2").fees[0].amount_usd == 450
    assert item(r, "NoDrive").one_time_fees_usd is None and item(r, "NoDrive").status == "permit_required"
    # 1 + 1 hydraulic... totals: 1,125 + 5,775 + 1,350 + 1,100 + 450 + 1,125 + 450 = 11,375
    assert r.one_time_fees_total_usd == 11375
    assert r.verified is True


def test_elevator_permit_terms_and_residential_exclusions():
    r = tc.equipment_operating_permits([
        {"kind": "elevator", "label": "FM", "drive": "hydraulic", "stories": 3, "full_maintenance_contract": True,
         "last_inspected_on": "2026-01-10"}], as_of=AS_OF)
    fm = item(r, "FM")
    # 3001(c)(3): 2-year permit with a full maintenance contract -> due 2028-01-10; 3001(b)(4): request 30 days
    # before -> 2027-12-11
    assert fm.next_inspection_due == "2028-01-10" and "C-11" in fm.permit_term and "monthly" in fm.permit_term
    assert any("request it from 2027-12-11" in m for m in fm.math)
    home = tc.equipment_operating_permits([{"kind": "elevator", "drive": "hydraulic", "stories": 2}],
                                          building_type="single family home", as_of=AS_OF)
    assert home.items[0].status == "not_covered" and home.permits_required == 0
    duplex = tc.equipment_operating_permits([{"kind": "elevator"}], building_type="duplex", as_of=AS_OF)
    assert duplex.items[0].status == "inspection_only"


def test_city_of_los_angeles_pressure_vessels_go_to_ladbs():
    r = tc.equipment_operating_permits([{"kind": "air compressor", "volume_gallons": 60, "safety_valve_psi": 175}],
                                       city=" city of los angeles ", as_of=AS_OF)
    p = r.items[0]
    assert p.issuer.startswith("Los Angeles Department of Building and Safety") and p.fees == []
    assert "(213) 202-9852" in p.why and tc.PV_LA in p.rule_ids
    county = tc.equipment_operating_permits([{"kind": "air compressor", "volume_gallons": 60,
                                              "safety_valve_psi": 175}], city="LA County", as_of=AS_OF)
    assert county.items[0].issuer == "Cal/OSHA (DOSH) Pressure Vessel Unit" and county.items[0].fees


@pytest.mark.parametrize("equipment,msg", [
    ([], "equipment is required"),
    ([{"kind": "forklift"}], "not recognized"),
    ([{"kind": "elevator", "drive": "magic"}], "drive 'magic' not recognized"),
    ([{"kind": "air tank", "volume_cu_ft": -1}], "volume_cu_ft"),
    ([{"kind": "elevator", "alterations": 0}], "alterations"),
    ([{"kind": "boiler", "last_inspected_on": "yesterday", "steam_psig": 100}], "last_inspected_on must be a date"),
])
def test_equipment_rejects_bad_inputs(equipment, msg):
    with pytest.raises(ValueError, match=msg):
        tc.equipment_operating_permits(equipment, as_of=AS_OF)


# ---------------------------------------------------------------- MCP
def test_register_and_call_every_tool_through_an_mcp_client():
    mcp = FastMCP("t")
    tc.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            out = {
                "req": await c.call_tool("certifications_required", {
                    "tasks": ["panel upgrade", "scrape paint"], "employer_license_classes": "c10",
                    "year_built": 1962, "building_type": "House", "as_of": AS_OF}),
                "crew": await c.call_tool("check_crew_certifications", {
                    "crew": [{"name": "Ana", "task": "pull wire", "credentials": ["General Electrician exp 2027-05-31"]},
                             {"name": "Tia", "task": "pull wire", "supervised_by": "Ana",
                              "credentials": [{"type": "trainee", "expires": "2027-02-01"}]}],
                    "employer_license_classes": ["C-10"], "building_type": "office", "as_of": AS_OF}),
                "path": await c.call_tool("electrician_certification_path", {
                    "certification_type": "GE", "hours_by_area": {"commercial": 6000, "industrial": 2000},
                    "as_of": AS_OF}),
                "equip": await c.call_tool("equipment_operating_permits", {
                    "equipment": [{"kind": "elevator", "drive": "hydraulic", "stories": 4, "units": 2},
                                  {"kind": "air compressor", "volume_gallons": 6, "safety_valve_psi": 135}],
                    "city": "SD", "as_of": AS_OF}),
            }
            return tools, {k: v.structured_content for k, v in out.items()}
    tools, out = asyncio.run(run())
    assert set(tools) == {"certifications_required", "check_crew_certifications", "electrician_certification_path",
                          "equipment_operating_permits"}
    for t in tools.values():
        a = t.annotations.model_dump()
        assert a.get("readOnlyHint", a.get("read_only_hint")) is True
        assert a.get("openWorldHint", a.get("open_world_hint")) is False
    assert "100 VA" in tools["certifications_required"].description              # numbers come from the rulebook
    assert "General Electrician 8,000" in tools["electrician_certification_path"].description
    assert "1.5 cu ft" in tools["equipment_operating_permits"].description
    assert out["req"]["required_count"] == 4 and out["req"]["verified"] is False
    assert out["req"]["unverified_rule_ids"] == [tc.R_FIRM]
    assert out["crew"]["cleared_to_start"] is True
    assert [w["status"] for w in out["crew"]["workers"]] == ["ok", "ok_with_supervision"]
    assert out["path"]["counted_hours"] == 8000 and out["path"]["experience_met"] is True
    assert out["equip"]["one_time_fees_total_usd"] == 1125 and out["equip"]["items"][1]["status"] == "exempt"
    assert out["equip"]["citations"] and out["equip"]["disclaimer"].startswith("Compliance information")


def test_mcp_rejects_bad_values_with_clear_errors():
    mcp = FastMCP("t")
    tc.register(mcp)

    async def run():
        async with Client(mcp) as c:
            errors = []
            for name, args in [
                ("certifications_required", {"tasks": ["wiring"], "building_type": "spaceship"}),
                ("certifications_required", {"tasks": []}),
                ("check_crew_certifications", {"crew": [{"name": "A", "task": "wiring"},
                                                        {"name": "A", "task": "wiring"}]}),
                ("electrician_certification_path", {"certification_type": "plumber"}),
                ("equipment_operating_permits", {"equipment": [{"kind": "forklift"}]}),
            ]:
                try:
                    await c.call_tool(name, args)
                    errors.append(None)
                except Exception as e:                         # fastmcp raises ToolError
                    errors.append(str(e))
            return errors
    errors = asyncio.run(run())
    assert all(errors), errors
    assert "spaceship" in errors[0] and "unique" in errors[2] and "plumber" in errors[3] and "forklift" in errors[4]


# ================================================================ 2026-11-25 second-pass legal review regressions
# Each value below was read in the official text re-fetched on 2026-11-25 (8 CCR 5006.2, 753, 771, 344.30, 291.5,
# 1532.1 on www.dir.ca.gov; 40 CFR 745.83 and 745.103 via the eCFR versioner API); the arithmetic is in the comments.
def test_second_pass_rules_are_verified_and_carry_the_regulation_numbers():
    exc = core.rule(tc.C_EXC)
    assert exc.verified and exc.status == "confirmed"
    # 5006.2 Exception (1): "2,000 pounds or less"; Exception (2): boom "less than 25 feet" or "less than 15,000 pounds"
    assert exc.param("exempt_if_rated_capacity_lb_lte") == 2000
    assert exc.param("delivery_knuckle_boom_exempt_if") == {"boom_ft_lt": 25, "or_capacity_lb_lt": 15000}
    assert exc.param("exempt_equipment") == ["derricks", "sideboom cranes"]
    # 745.103: elderly = 62 or older at initial occupancy; child under 6
    excl = core.rule(tc.R_EXCL)
    assert excl.verified and excl.param("elderly_housing_min_age_at_initial_occupancy") == 62
    assert excl.param("child_age_lt") == 6
    trades = core.rule(tc.R_TRADES)
    assert trades.verified and "electrical work" in trades.param("trades_named")
    assert (trades.param("interior_max_sqft_per_room"), trades.param("exterior_max_sqft")) == (6, 20)
    # 8 CCR 753 definitions: low-pressure steam <= 15 psi (pressure and valve); hot water <= 160 psi and <= 250 F
    d = core.rule(tc.PV_BOILER_SMALL).param("definitions_8_ccr_753")
    assert (d["low_pressure_steam_max_psi_lte"], d["low_pressure_hot_water_max_psi_lte"],
            d["low_pressure_hot_water_max_temp_f_lte"], d["high_temperature_water_min_temp_f_gt"]) == (15, 160, 250, 212)
    # 344.30(a)(16) screw type elevator $225; (g) processing and mailing "up to a maximum of $15.00"
    assert core.rule(tc.V_FEE_PERIODIC).param("usd_per_unit")["screw_type_elevator"] == 225
    assert core.rule(tc.V_FEE_PERIODIC).param("permit_processing_and_mailing_fee_max_usd") == 15
    # 5006.2(d)(1) certificates "valid for a maximum of five (5) years"; (c)(1) "at no cost to employees"
    assert core.rule(tc.C_EXAM).param("option1_certificate_max_validity_years") == 5
    assert core.rule(tc.C_EXAM).param("employer_provides_at_no_cost_to_employee") is True
    for rid in (tc.C_OIT, tc.C_EXAM, tc.PV_BOILER_SMALL, tc.V_FEE_PERIODIC, tc.L_CDPH):
        assert core.rule(rid).status == "corrected" and core.rule(rid).verified


@pytest.mark.parametrize("task,expected", [
    ("derrick operation for steel", [("crane", "derrick_sideboom")]),
    ("sideboom pipe laying", [("crane", "derrick_sideboom")]),
    ("digger derrick setting poles", [("crane", "digger_derrick")]),
    ("knuckle boom truck delivering drywall", [("crane", "knuckle_boom_delivery")]),
    ("knuckle boom crane setting trusses", [("crane", "knuckle_boom")]),
    ("boom truck pick", [("crane", None)]),
    ("hang drywall", []),                                                 # new drywall disturbs no paint
    ("drywall repair", [("lead", None)]),
    ("cut into walls for new circuits", [("electrical", "general"), ("lead", None)]),
    ("frame elevator shaft", []),                                         # shaft framing is not conveyance work
    ("crane over the elevator shaft", [("crane", None)]),
])
def test_classify_second_pass(task, expected):
    assert tc.classify_task(task) == expected


def test_derricks_sideboom_and_digger_derricks_need_no_5006_2_certification():
    r = tc.certifications_required(["derrick operation", "sideboom crane", "digger derrick for poles"], as_of=AS_OF)
    assert [q.status for q in r.requirements] == ["not_required", "not_required"]
    assert all(q.verified and tc.C_EXC in q.rule_ids for q in r.requirements)
    assert "8 CCR 5006 still apply" in r.requirements[0].why
    assert "ASME B30.5" in r.requirements[1].why                          # truck cranes are not excepted
    assert r.required_count == 0


def test_delivery_knuckle_boom_is_conditional_and_other_cranes_list_the_exceptions():
    r = tc.certifications_required(["knuckle boom truck delivering drywall", "crane pick of RTUs"], as_of=AS_OF)
    kb, crane = r.requirements
    assert kb.status == "conditional" and "under 25 ft" in kb.why and "under 15,000 lb" in kb.why
    assert crane.status == "required" and "2,000 lb or less" in crane.why and "derricks and sideboom" in crane.why
    assert "no cost to the employee" in crane.cost_detail                 # 5006.2(c)(1)
    assert "valid at most 5 years" in crane.acceptable[0]                 # 5006.2(d)(1)
    assert "hoisting personnel" in crane.alternatives[0] and "20 ft" in crane.alternatives[0]
    assert kb.verified and crane.verified


@pytest.mark.parametrize("task,status", [
    ("crane personnel lift", "not_qualified"), ("crane pick near power lines", "not_qualified"),
    ("tandem crane pick", "not_qualified"), ("crane over the elevator shaft", "not_qualified"),
    ("crane christmas tree rigging", "confirm"),                           # (b)(3)(E): trainer may allow it
    ("crane pick of RTUs", "ok_with_supervision"),
])
def test_operator_in_training_task_limits(task, status):
    r = tc.check_crew_certifications([
        {"name": "O", "task": task, "credentials": ["crane operator in training"], "supervised_by": "C"},
        {"name": "C", "task": "crane operator", "credentials": ["NCCCO crane operator exp 2028-01-01"],
         "employer_evaluated": True}], as_of=AS_OF)
    w = worker(r, "O")
    assert w.status == status
    if status == "not_qualified":
        assert "8 CCR 5006.2(b)(3)" in w.issues[0] and w.can_do_task is False
        assert r.cleared_to_start is False


def test_crane_certificate_without_expiry_is_capped_at_5_years_from_issue():
    r = tc.check_crew_certifications([
        {"name": "Old", "task": "crane operator", "credentials": [{"type": "NCCCO crane operator",
                                                                    "issued": "2020-01-01"}], "employer_evaluated": True},
        {"name": "New", "task": "crane operator", "credentials": [{"type": "NCCCO crane operator",
                                                                    "issued": "2023-06-15"}], "employer_evaluated": True},
    ], as_of=AS_OF)
    old, new = worker(r, "Old"), worker(r, "New")
    # 2020-01-01 + 5 = 2025-01-01 < 2026-11-25 -> expired; 2023-06-15 + 5 = 2028-06-15 -> valid
    assert old.credentials[0].expires == "2025-01-01" and old.status == "expired" and old.can_do_task is False
    assert "Expired 2025-01-01" in old.issues[0]                          # an expired card always says why
    assert new.credentials[0].expires == "2028-06-15" and new.status == "ok"


def test_electrical_work_in_a_pre_1978_house_flags_rrp():
    base = dict(employer_license_classes="C-10", year_built=1950, building_type="house", as_of=AS_OF)
    r = tc.certifications_required(["rewire bedrooms"], **base)
    ren = req(r, "EPA certified renovator")
    # 745.83: electrical work is minor repair only up to 6 sq ft per room inside / 20 sq ft outside
    assert ren.status == "conditional" and tc.R_TRADES in ren.rule_ids
    assert "6 sq ft or less" in ren.why and "20 sq ft or less" in ren.why and "electrical work" in ren.why
    assert [q.trade for q in r.requirements][:2] == ["electrical", "lead_rrp"]
    # 4 sq ft <= 6 -> minor repair: RRP not required; 8 sq ft > 6 -> renovation: required
    small = tc.certifications_required(["rewire bedrooms"], interior_painted_sqft_per_room=4, **base)
    assert req(small, "EPA RRP certification").status == "not_required"
    big = tc.certifications_required(["rewire bedrooms"], interior_painted_sqft_per_room=8, **base)
    assert req(big, "EPA certified renovator").status == "required"
    # not triggered for a 1990 house or an industrial building
    for kw in (dict(base, year_built=1990), dict(base, building_type="factory")):
        assert all(q.trade == "electrical" for q in tc.certifications_required(["rewire bedrooms"], **kw).requirements)
    crew = tc.check_crew_certifications([{"name": "G", "task": "rewire bedrooms",
                                          "credentials": ["General Electrician exp 2028-01-01"]}], **base)
    f = finding(crew, "EPA RRP for electrical work")
    assert f.status == "confirm" and f.verified and crew.cleared_to_start is True


@pytest.mark.parametrize("building,expected", [
    ("senior housing", "senior_or_care"), ("assisted living", "senior_or_care"), ("55+ community", "senior_or_care"),
    ("studio apartment", "zero_bedroom"), ("college dorm", "zero_bedroom"), ("SRO hotel", "zero_bedroom"),
    ("apartment building", "multifamily"),
])
def test_senior_and_zero_bedroom_buildings(building, expected):
    assert tc.building_from(building) == expected


@pytest.mark.parametrize("building,status", [("senior housing", "conditional"), ("studio apartment", "conditional"),
                                             ("apartment building", "required")])
def test_rrp_target_housing_exclusions(building, status):
    r = tc.certifications_required(["scrape and repaint"], year_built=1960, building_type=building, as_of=AS_OF)
    ren = req(r, "EPA certified renovator")
    assert ren.status == status
    if status == "conditional":
        # 745.103: not target housing "unless any child who is less than 6 years of age resides"
        assert tc.R_EXCL in ren.rule_ids and "child under 6" in ren.why


def test_residential_electrician_in_a_care_facility_must_be_confirmed():
    r = tc.certifications_required(["pull wire"], employer_license_classes="C-10", building_type="nursing home",
                                   as_of=AS_OF)
    assert "NEC Article 517" in r.requirements[0].acceptable[-1] and "confirm" in r.requirements[0].acceptable[-1]
    crew = tc.check_crew_certifications([{"name": "R", "task": "pull wire", "credentials": [
        "Residential Electrician exp 2028-01-01"]}], employer_license_classes="C-10", building_type="assisted living",
        as_of=AS_OF)
    assert worker(crew, "R").status == "confirm" and "Article 517" in worker(crew, "R").issues[0]


def test_window_replacement_is_judged_task_by_task():
    base = dict(year_built=1960, building_type="house", interior_painted_sqft_per_room=3, as_of=AS_OF)
    # 'paint window trim' + 'patch plaster' is not a window replacement: 3 sq ft <= 6 -> minor repair
    r = tc.certifications_required(["paint window trim", "patch plaster"], **base)
    assert req(r, "EPA RRP certification").status == "not_required"
    r = tc.certifications_required(["replace windows", "patch plaster"], **base)
    assert req(r, "EPA certified renovator").status == "required"        # window replacement is never minor


def test_boilers_follow_the_8_ccr_753_definitions():
    r = tc.equipment_operating_permits([
        {"kind": "boiler", "label": "hw-low", "boiler_type": "hot water heating", "water_psi": 30, "water_temp_f": 180},
        {"kind": "boiler", "label": "hw-htw", "boiler_type": "hot water", "water_psi": 200, "water_temp_f": 300},
        {"kind": "boiler", "label": "hw-gap", "boiler_type": "hot water", "water_psi": 200, "water_temp_f": 200},
        {"kind": "boiler", "label": "hw-?", "boiler_type": "hydronic"},
        {"kind": "boiler", "label": "steam-?", "boiler_type": "steam"},
        {"kind": "boiler", "label": "power-10", "boiler_type": "power boiler", "steam_psig": 10, "safety_valve_psi": 10},
        {"kind": "boiler", "label": "power", "boiler_type": "power boiler"},
    ], as_of=AS_OF)
    # 30 psi <= 160 and 180 F <= 250 F -> low-pressure; 300 F > 212 F at 200 psi > 160 -> high-temperature water
    assert item(r, "hw-low").status == "exempt" and item(r, "hw-htw").status == "exempt"
    # 200 psi > 160 at 200 F (not above 212 F): neither -> annual inspection and permit (770(a), 780)
    assert item(r, "hw-gap").status == "permit_required"
    assert item(r, "hw-?").status == "need_info" and "water_temp_f" in item(r, "hw-?").why
    assert item(r, "steam-?").status == "need_info" and "safety_valve_psi" in item(r, "steam-?").why
    assert item(r, "power-10").status == "exempt"                       # the 753 numbers decide, not the label
    pw = item(r, "power")
    assert pw.status == "permit_required" and pw.before_use
    assert r.verified is True


def test_a_boiler_that_needs_a_permit_never_says_no_permit_is_needed():
    r = tc.equipment_operating_permits([{"kind": "boiler", "steam_psig": 125}], as_of=AS_OF)
    assert r.permits_required == 1 and "No permit" not in r.next_step and "inspection" in r.next_step


def test_air_tank_that_may_hold_an_indefinite_permit_is_not_called_overdue():
    r = tc.equipment_operating_permits([
        {"kind": "compressor", "label": "maybe", "volume_gallons": 20, "mawp_psi": 150,
         "last_inspected_on": "2023-11-24"},
        {"kind": "compressor", "label": "no-stamp", "volume_gallons": 20, "mawp_psi": 150, "asme_stamped": False,
         "last_inspected_on": "2023-11-24"},
    ], as_of=AS_OF)
    maybe = item(r, "maybe")
    # 2.674 cu ft <= 25 and MAWP 150 <= 150, stamp unknown -> 461(h) indefinite permit possible: no overdue claim
    assert maybe.overdue is False and maybe.permit_expires_by is None and "asme_stamped" in maybe.permit_term
    assert tc.PV_AIR_INDEF in maybe.rule_ids and any("2026-11-24 (past)" in m for m in maybe.math)
    # not stamped -> 461(d) portable (assumed) 3 years: 2023-11-24 + 3 = 2026-11-24 < 2026-11-25 -> overdue
    assert item(r, "no-stamp").overdue is True and item(r, "no-stamp").permit_expires_by == "2026-11-24"


def test_screw_elevator_periodic_fee_and_permit_processing_fee():
    p = tc.equipment_operating_permits([{"kind": "elevator", "drive": "screw", "stories": 3}], building_type="office",
                                       as_of=AS_OF).items[0]
    # (i)(3): $350 x 3 stories = $1,050; (a)(16): $225 periodic; (g): processing and mailing at most $15
    assert [(f.item, f.amount_usd, f.when) for f in p.fees] == [
        ("Erection/installation permit incl. plan approval", 1050, "one_time"), ("DOSH inspection", 225, "per_hour"),
        ("Periodic reinspection", 225, "recurring"), ("Permit processing and mailing", 15, "recurring")]
    assert p.one_time_fees_usd == 1050


def test_renewal_lead_training_and_cccm_wording_match_the_text():
    crew = tc.check_crew_certifications([{"name": "G", "task": "pull wire", "credentials": [
        "General Electrician exp 2027-06-30"]}], employer_license_classes="C-10", as_of=AS_OF)
    renewal = worker(crew, "G").credentials[0].renewal
    # 8 CCR 291.5(c): "postmarked or received at DAS 30 days before the end of the certification period"
    assert "291.5(c)" in renewal and "postmarked or received 30 days" in renewal and "2027-05-31" in renewal
    assert "291.5(b)" not in renewal
    r = tc.certifications_required(["demo plaster"], lead_exposure_ug_m3=20, as_of=AS_OF)
    # 1532.1(l)(1)(C)1.: training for employees exposed "at or above the action level on any day"
    assert "at or above the action level on any day" in req(r, "CDPH").why
    e = tc.certifications_required(["elevator repair"], building_type="office", as_of=AS_OF)
    cccm = req(e, "Certified Competent Conveyance Mechanic")
    # DOSH FAQ: "maintenance, repairs, alterations, or replacements" (installation is not in that list)
    assert "maintenance, repairs, alterations, and replacements" in cccm.why and "Installing" not in cccm.why


def test_path_rejects_absurd_hours():
    with pytest.raises(ValueError, match="cannot be above 100000"):
        tc.electrician_certification_path("GE", hours_by_area={"commercial": 1e30}, as_of=AS_OF)


def test_mcp_accepts_the_new_boiler_and_building_fields():
    mcp = FastMCP("t")
    tc.register(mcp)

    async def run():
        async with Client(mcp) as c:
            eq = await c.call_tool("equipment_operating_permits", {"equipment": [
                {"kind": "boiler", "boiler_type": "hot water", "water_psi": 30, "water_temp_f": 180}],
                "as_of": AS_OF})
            cr = await c.call_tool("certifications_required", {
                "tasks": ["scrape paint"], "year_built": 1960, "building_type": "senior apartments", "as_of": AS_OF})
            return eq.structured_content, cr.structured_content
    eq, cr = asyncio.run(run())
    assert eq["items"][0]["status"] == "exempt"
    assert cr["building_type"] == "senior_or_care" and cr["requirements"][0]["status"] == "conditional"
