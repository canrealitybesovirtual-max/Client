"""Federal projects module: hand-computed Davis-Bacon / CWHSSA / Miller Act / Buy America / DBE / dual-coverage
scenarios. Every expected number is traced to the rule text in a comment."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, federal_projects as fp

AS_OF = "2026-11-25"


def req(result, start):
    return next(r for r in result.requirements if r.item.startswith(start))


def line(result, start):
    return next(x for x in result.line_items if x.item.startswith(start))


def kdate(result, start):
    return next(d for d in result.key_dates if d.what.startswith(start))


def caltrans_local_job(**kw):
    """$480,000 City of San Diego federal-aid (FHWA) street job, bid 12/1/2026, $40,000 specialty items, FHWA funds
    obligated 10/15/2026."""
    args = dict(prime_contract_value_usd=480_000, funding_source="FHWA", awarding_agency="City of San Diego",
                bid_opening_date="2026-12-01", specialty_items_usd=40_000, fhwa_obligation_date="2026-10-15",
                as_of=AS_OF)
    args.update(kw)
    return fp.federal_project_requirements(**args)


def usace_job(**kw):
    """$2,400,000 fixed-price USACE job awarded 11/20/2026 to an other-than-small prime."""
    args = dict(prime_contract_value_usd=2_400_000, funding_source="USACE",
                awarding_agency="US Army Corps of Engineers, Los Angeles District", award_date="2026-11-20",
                as_of=AS_OF)
    args.update(kw)
    return fp.federal_project_requirements(**args)


# ================================================================ rulebook wiring
def test_every_pack_rule_is_used_or_explained():
    book = {r.id: r for r in core.all_rules()}
    consts = {v for k, v in vars(fp).items() if k.isupper() and isinstance(v, str) and "-" in v and " " not in v}
    assert consts == set(fp.RULE_IDS)
    assert set(fp.RULE_IDS) <= set(book)
    pack = {i for i, r in book.items() if r.pack == "web_federal-works"}
    # 130 verified rules + 2 added in the 2026-11-25 legal review (2 CFR 200.317, 49 CFR 661.5), both confirmed
    assert len(pack) == 132 and {fp.STATE_PROC, fp.FTA_BA} <= pack
    used = {i for i in fp.RULE_IDS if i.startswith("fed-")}
    assert used | set(fp.NOT_USED) == pack and not used & set(fp.NOT_USED)
    # the verifier's summary: 115 confirmed + 16 corrected, 1 unverifiable (EO 14026 status)
    assert [i for i in pack if not book[i].verified] == [fp.EO14026]
    assert book[fp.EO14026].status == "unverifiable"
    # California rules borrowed from other packs are verified
    assert all(book[i].verified for i in (fp.ECPR, fp.BP7108, fp.PW_OT, fp.PEN_1813, fp.PCC_10262))


def test_corrected_values_are_the_ones_used():
    # fed-cwhssa-far-clause-200k (corrected): $200,000 since 2025-10-01, $150,000 before
    assert core.rule(fp.CWH_FAR).status == "corrected"
    assert (core.rule(fp.CWH_FAR).param("far_clause_excluded_at_or_below_usd"),
            core.rule(fp.CWH_FAR).param("prior_value_usd")) == (200_000, 150_000)
    # fed-dba-fringe-cash-or-plan (corrected): total-only test, no federal cash floor
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, coverage="federal_only", cash_rate_paid_usd=10,
                                        fringe_paid_usd=18.20)
    assert r.required_cash_rate_min_usd is None and r.pay_check.complies       # 10 + 18.20 = 28.20 >= 28.20


# ================================================================ normalizers (user words, not law)
@pytest.mark.parametrize("raw,key", [
    ("FHWA", "federal_aid_highway"), ("Caltrans federal-aid", "federal_aid_highway"),
    ("  federal aid highway ", "federal_aid_highway"), ("FHWA via Caltrans", "federal_aid_highway"),
    ("FTA", "federal_transit"), ("faa aip", "federal_airport"), ("Airport Improvement Program", "federal_airport"),
    ("USACE", "direct_federal"), ("U.S. Army Corps of Engineers", "direct_federal"), ("GSA", "direct_federal"),
    ("va", "direct_federal"), ("NAVFAC", "direct_federal"), ("direct_federal", "direct_federal"),
    ("HUD CDBG grant", "federal_grant"), ("EPA SRF", "federal_grant"), ("fema", "federal_grant"),
    ("state only", "none"), ("No federal money", "none"),
])
def test_funding_aliases(raw, key):
    assert fp.normalize_funding(raw) == key


@pytest.mark.parametrize("raw", ["federal", "Federally funded", "yes"])
def test_ambiguous_funding_is_an_error(raw):
    with pytest.raises(ValueError, match="ambiguous"):
        fp.normalize_funding(raw)


def test_unknown_funding_lists_the_choices():
    with pytest.raises(ValueError, match="direct_federal, federal_aid_highway"):
        fp.normalize_funding("bitcoin")
    with pytest.raises(ValueError, match="required"):
        fp.normalize_funding("  ")


@pytest.mark.parametrize("raw,kind,name", [
    ("SD", "california_public_agency", "City of San Diego"),
    ("  city of san diego, ca ", "california_public_agency", "City of San Diego"),
    ("City of LA", "california_public_agency", "City of Los Angeles"),
    ("sacto", "california_public_agency", "City of Sacramento"),
    ("Caltrans District 11", "california_public_agency", "Caltrans District 11"),
    ("LA Metro", "california_public_agency", "LA Metro"),
    ("San Diego Unified School District", "california_public_agency", "San Diego Unified School District"),
    ("US Army Corps of Engineers, Los Angeles District", "federal_agency",
     "US Army Corps of Engineers, Los Angeles District"),
    ("VA San Diego Healthcare System", "federal_agency", "VA San Diego Healthcare System"),
    # CalVet is a state agency although "Veterans Affairs" is also a federal name
    ("California Department of Veterans Affairs", "california_public_agency",
     "California Department of Veterans Affairs"),
    ("U.S. Department of Veterans Affairs", "federal_agency", "U.S. Department of Veterans Affairs"),
    ("Naval Facilities Engineering Systems Command Southwest", "federal_agency",
     "Naval Facilities Engineering Systems Command Southwest"),
    ("Acme Homes LLC", "other", "Acme Homes LLC"),
    (None, "not_given", None),
])
def test_agency_classification(raw, kind, name):
    assert fp.classify_agency(raw) == (kind, name)


@pytest.mark.parametrize("raw,key", [("remodel", "alteration"), ("Tenant Improvement", "alteration"),
                                     ("demo", "demolition"), ("EV chargers", "installation"),
                                     ("Material supplier", "material_supply"), (None, "construction"),
                                     ("painting and decorating", "painting_decorating")])
def test_work_type_aliases(raw, key):
    assert fp.normalize_work_type(raw) == key


# ================================================================ federal_project_requirements
def test_city_of_san_diego_federal_aid_job_dual_coverage():
    r = caltrans_local_job()
    assert r.verified and r.unverified_rule_ids == []
    assert (r.funding_source, r.awarding_agency_kind, r.awarding_agency) == (
        "federal_aid_highway", "california_public_agency", "City of San Diego")
    assert r.davis_bacon_applies is True and r.california_prevailing_wage_also_applies is True
    # 29 CFR 5.5(a) / FAR 22.403-1: over $2,000
    assert "$480,000 > $2,000" in req(r, "Davis-Bacon prevailing wages").why
    # 29 CFR 5.5(b)(1) assisted: over $100,000; LD $33/worker/day (29 CFR 5.5(b)(2), 2025-01-15 amount)
    ot = req(r, "CWHSSA weekly overtime")
    assert ot.applies and "$480,000 > $100,000" in ot.why and "$33 per worker per" in ot.what_to_do
    # 8 CCR 16001(b): state rate where higher; DLSE Manual 4.2.2: cash >= DIR basic; LC 1815: 8/day, 40/week, 1.5x
    ca = req(r, "California prevailing wage also applies")
    assert "over 8 hours a day or 40 a week at 1.5x" in ca.what_to_do
    # Copeland 29 CFR 3.4(a): 7 days after payday; LC 1771.4: eCPR at least every 30 days, final within 30 days
    two = req(r, "Two payroll filings")
    assert "within 7 days after each payday" in two.what_to_do and "at least every 30 days" in two.what_to_do
    # 2 CFR 200.326 over the FAR SAT ($350,000 from 2025-10-01): 5% bid guarantee, 100% / 100% bonds
    # 5% x $480,000 = $24,000; 100% x $480,000 = $480,000
    b = r.bonds
    assert (b.regime, b.bid_guarantee_usd, b.performance_bond_usd, b.payment_bond_usd) == (
        "2_cfr_200_326", 24_000, 480_000, 480_000)
    assert "$480,000 > $350,000" in req(r, "Federal minimum bonding").why
    # 23 CFR 635.116: 30% x ($480,000 - $40,000 specialty) = $132,000
    assert line(r, "Own-forces minimum").value == 132_000
    assert "$132,000" in req(r, "Perform 30% with your own forces").what_to_do
    # 23 CFR 635.410(b)(4): max(0.1% x $480,000 = $480, $2,500) = $2,500
    assert line(r, "Foreign steel/iron minimal-use allowance").value == 2_500
    # 23 CFR 635.410(c): obligated 2026-10-15 >= 2026-10-01 -> made in U.S. AND > 55% U.S. components
    assert "over 55% U.S. component cost" in req(r, "FHWA Buy America").what_to_do
    # 2 CFR 25: contractors under a federal award need no SAM registration; 23 CFR 635.110(e): no excluded firms
    assert req(r, "SAM.gov registration").applies is False
    assert req(r, "Not excluded in SAM.gov").applies is True
    # 49 CFR 26.51(h)/26.111: goals paused since 2025-10-03, UCP deadline 2026-12-24 (+ up to 90 days)
    dbe = req(r, "DBE contract goals and counting are paused")
    assert dbe.applies is False and "2025-10-03" in dbe.why and "2026-12-24" in dbe.why and "90 days" in dbe.why
    # B&P 7108.5: 7 days (stricter than 49 CFR 26.29's 30 days); retainage back within 30 days
    pay = req(r, "Pay subcontractors within 7 days")
    assert "outer limit is 30 days" in pay.what_to_do and "within 30 days after its work" in pay.what_to_do
    # 2 CFR 200 App. II at $480,000: termination (>$10,000), breach (>$350,000), clean air/water (>$150,000),
    # anti-lobbying (>$100,000) all included
    assert "Not required at this value: none" in req(r, "Required federal grant clauses").what_to_do
    # 23 CFR 633 removed 2026-06-10: FHWA-1273 now "follow your contract"
    assert req(r, "Form FHWA-1273").applies is None
    # 29 CFR 1.6(c)(2): cutoff 12/1 - 10 = 11/21 has passed -> re-check at bid opening (6 days left, due soon);
    # award window 12/1 + 90 days = 2027-03-01; DBE milestones 2026-12-24 and 2027-03-24
    wd = kdate(r, "Re-check the wage determination")
    assert (wd.due, wd.days_left, wd.status, wd.kind) == ("2026-12-01", 6, "due_soon", "deadline")
    assert "2026-11-21" in wd.what
    assert kdate(r, "Award window").due == "2027-03-01" and kdate(r, "Award window").kind == "milestone"
    assert kdate(r, "CUCP DBE re-evaluation").due == "2026-12-24"
    assert kdate(r, "Last day for a non-responding DBE").due == "2027-03-24"
    assert r.next_step.startswith("By 2026-12-01: Re-check the wage determination")
    assert "davis_bacon_vs_ca_prevailing" in r.next_step and "$24,000 bid guarantee" in r.next_step
    # every requirement carries its rules and the result cites them all
    cited = {c.rule_id for c in r.citations}
    assert all(set(q.rule_ids) <= cited for q in r.requirements)
    assert {fp.ECPR, fp.BP7108, fp.PW_OT, fp.CA_HIGHER, fp.CA_BASIC} <= cited
    assert r.disclaimer == core.DISCLAIMER


def test_wage_determination_cutoff_still_ahead_is_a_deadline():
    # bid 2027-01-15: cutoff 2027-01-15 - 10 days = 2027-01-05
    r = caltrans_local_job(bid_opening_date="2027-01-15")
    wd = kdate(r, "Re-check the wage determination")
    assert (wd.due, wd.status) == ("2027-01-05", "upcoming")
    # bid already opened: no re-check step, only the award window (2026-10-01 + 90 = 2026-12-30)
    r = caltrans_local_job(bid_opening_date="2026-10-01")
    assert not any(d.what.startswith("Re-check") for d in r.key_dates)
    assert kdate(r, "Award window").due == "2026-12-30"


def test_usace_direct_federal_job():
    r = usace_job()
    assert r.awarding_agency_kind == "federal_agency" and r.funding_source == "direct_federal"
    assert r.davis_bacon_applies and r.california_prevailing_wage_also_applies is False
    assert "complete control of the federal government" in req(r, "California prevailing wage").why
    # FAR 22.305: clause in contracts over $200,000 (since 2025-10-01)
    assert "$2,400,000 > $200,000" in req(r, "CWHSSA weekly overtime").why
    # Miller Act (FAR 28.102-1): over $150,000 -> 100% performance + 100% payment = $2,400,000 each
    # FAR 28.101-2 / 52.228-1(c): min(20% x $2,400,000 = $480,000, $3,000,000) = $480,000
    b = r.bonds
    assert (b.regime, b.performance_bond_usd, b.payment_bond_usd, b.bid_guarantee_usd) == (
        "miller_act", 2_400_000, 2_400_000, 480_000)
    assert "within 10 days after you receive the forms" in req(r, "Miller Act performance").what_to_do
    assert "SF 25" in req(r, "Miller Act performance").what_to_do
    # FAR 19.702: other-than-small construction over $2,000,000 (was $1,500,000)
    assert req(r, "Small business subcontracting plan").applies
    # FAR 36.501: fixed-price over $2,000,000 -> ordinarily >= 12%: 12% x $2,400,000 = $288,000
    assert line(r, "Self-performance").value == 288_000
    # FAR 52.222-54: over $150,000 -> enroll within 30 days of award: 11/20 + 30 = 12/20/2026
    assert kdate(r, "Enroll in E-Verify").due == "2026-12-20"
    # FAR 52.222-11(d): SF 1413 within 14 days: 11/20 + 14 = 12/4/2026 (9 days left -> due soon)
    sf = kdate(r, "Deliver SF 1413")
    assert (sf.due, sf.days_left, sf.status) == ("2026-12-04", 9, "due_soon")
    assert req(r, "SF 1413").due == "2026-12-04"
    assert r.next_step.startswith("By 2026-12-04: Deliver SF 1413")
    # FAR 3.808: anti-lobbying over $200,000; FAR 52.232-27: 7 days, interest from day 8; FAR 52.232-5: 10%
    assert req(r, "Anti-lobbying certification").applies
    assert "day 8" in req(r, "Pay subcontractors within 7 days").what_to_do
    assert "up to 10%" in req(r, "Retainage").what_to_do
    # FAR 52.225-9: 65% for items delivered 2024-2028, 75% from 2029 (by delivery year, not today's year);
    # FAR 52.225-11 accepts designated-country material
    ba = req(r, "Buy American").what_to_do
    assert "over 65% for items delivered in 2024-2028; over 75% for items delivered from 2029" in ba
    assert "FAR 52.225-11" in ba and "designated-country" in ba
    # EO 13706: 1 h per 30 h, 56 h cap
    assert "1 hour of paid sick leave per 30 hours" in req(r, "Federal paid sick leave").what_to_do
    # the unverifiable EO 14026 status item makes the whole result unverified ...
    eo = req(r, "Federal contractor minimum wage")
    assert eo.applies is None and not eo.verified and eo.why.startswith("UNVERIFIED")
    assert r.verified is False and r.unverified_rule_ids == [fp.EO14026]
    # ... and leaving it out makes it verified
    r2 = usace_job(include_unverified=False)
    assert r2.verified is True and not any(q.item.startswith("Federal contractor minimum") for q in r2.requirements)


def test_bid_guarantee_cap_and_solicitation_figures():
    # min(20% x $20,000,000 = $4,000,000, $3,000,000) = $3,000,000
    assert usace_job(prime_contract_value_usd=20_000_000).bonds.bid_guarantee_usd == 3_000_000
    # solicitation says 10% or $1,000,000: min(10% x $2,400,000 = $240,000, $1,000,000) = $240,000, flagged
    r = usace_job(bid_guarantee_pct=10, bid_guarantee_cap_usd=1_000_000)
    assert r.bonds.bid_guarantee_usd == 240_000
    assert any("outside the FAR 28.101-2(b) range" in n for n in r.notes)
    # bid price differs from contract: 20% x $2,500,000 bid = $500,000
    assert usace_job(bid_price_usd="$2,500,000").bonds.bid_guarantee_usd == 500_000


@pytest.mark.parametrize("value,regime,protection", [
    (35_000, "none", None),            # not more than $35,000: nothing (FAR 28.102-1(b))
    (35_000.01, "payment_protection", 35_000.01),
    (150_000, "payment_protection", 150_000),   # "> $35,000 and <= $150,000": 100% payment protection
    (150_000.01, "miller_act", None),           # over $150,000: Miller Act bonds
])
def test_miller_act_tiers(value, regime, protection):
    r = usace_job(prime_contract_value_usd=value, include_unverified=False)
    assert r.bonds.regime == regime and r.bonds.payment_protection_usd == protection


@pytest.mark.parametrize("value,applies", [(2_000, False), (2_000.01, True)])
def test_davis_bacon_threshold_is_over_2000(value, applies):
    r = usace_job(prime_contract_value_usd=value, include_unverified=False)
    assert r.davis_bacon_applies is applies
    # SF 1413 and EO 13706 sick leave come with the Davis-Bacon clause only
    assert any(q.item.startswith("SF 1413") for q in r.requirements) is applies
    assert any(q.item.startswith("Federal paid sick leave") for q in r.requirements) is applies


def test_far_thresholds_follow_the_as_of_date():
    # Before 2025-10-01: CWHSSA clause above $150,000, self-performance above $1,500,000, SAT $250,000
    old = usace_job(prime_contract_value_usd=1_800_000, as_of="2025-06-01", award_date=None, include_unverified=False)
    assert req(old, "Self-performance").applies and "$1,500,000" in req(old, "Self-performance").why
    assert "$2,000,000 from 2025-10-01" in req(old, "Self-performance").why
    new = usace_job(prime_contract_value_usd=1_800_000, award_date=None, include_unverified=False)
    assert req(new, "Self-performance").applies is False
    small = usace_job(prime_contract_value_usd=180_000, award_date=None, include_unverified=False)
    assert req(small, "CWHSSA weekly overtime").applies is False                 # $180,000 <= $200,000
    old_small = usace_job(prime_contract_value_usd=180_000, as_of="2025-06-01", award_date=None,
                          include_unverified=False)
    assert req(old_small, "CWHSSA weekly overtime").applies is True              # $180,000 > $150,000
    # assisted bonding: $300,000 > $250,000 SAT before 2025-10-01, but not > $350,000 after
    a_old = caltrans_local_job(prime_contract_value_usd=300_000, as_of="2025-06-01", bid_opening_date=None)
    a_new = caltrans_local_job(prime_contract_value_usd=300_000, bid_opening_date=None)
    assert a_old.bonds.regime == "2_cfr_200_326" and a_new.bonds.regime == "none"


def test_recipient_sat_lower_than_far():
    # a city SAT of $200,000 triggers the 2 CFR 200.326 bonds on a $300,000 contract
    r = caltrans_local_job(prime_contract_value_usd=300_000, recipient_sat_usd=200_000, bid_opening_date=None)
    assert r.bonds.regime == "2_cfr_200_326" and r.bonds.bid_guarantee_usd == 15_000     # 5% x $300,000
    with pytest.raises(ValueError, match="cannot exceed the FAR simplified acquisition threshold"):
        caltrans_local_job(recipient_sat_usd=400_000)


def test_subcontractor_on_transit_job():
    r = fp.federal_project_requirements(3_000_000, "FTA", "LA Metro", is_prime=False, your_contract_value_usd=18_000,
                                        as_of=AS_OF)
    assert r.your_role == "subcontractor" and r.davis_bacon_applies
    assert "covered at any amount" in req(r, "Davis-Bacon prevailing wages").why
    assert "Send yours through the prime" in req(r, "Weekly certified payroll").what_to_do
    # 2 CFR 180.220: lower tier covered at $25,000+; $18,000 is below
    assert req(r, "Not excluded in SAM.gov").applies is False
    # bonds belong to the prime; subs get no bond requirement items
    assert not any(q.area == "Bonds" for q in r.requirements)
    assert r.bonds.notes[0].startswith("These are the prime contractor's bonds")
    # FTA jobs use 49 CFR 661.5 (every component of a manufactured product of U.S. origin), not BABA's 55% test
    assert not any(q.item.startswith("Build America") for q in r.requirements)
    fta = req(r, "FTA Buy America")
    assert fta.applies is True and fp.FTA_BA in fta.rule_ids and fta.verified
    assert "all of its components of U.S. origin" in fta.what_to_do and "BABA 55% test" in fta.what_to_do


def test_grant_with_unknown_program_and_city_alias():
    r = fp.federal_project_requirements("900000", "HUD CDBG", "SD", as_of=AS_OF)
    assert r.awarding_agency == "City of San Diego"
    assert r.davis_bacon_applies is None and r.california_prevailing_wage_also_applies is True
    assert "Davis-Bacon Related Act" in r.next_step
    assert req(r, "Build America, Buy America").applies is None        # infrastructure not stated
    r2 = fp.federal_project_requirements(900_000, "HUD CDBG", "SD", davis_bacon_related_act=True,
                                         infrastructure_project=True, as_of=AS_OF)
    assert r2.davis_bacon_applies is True and req(r2, "Build America, Buy America").applies is True
    r3 = fp.federal_project_requirements(900_000, "HUD CDBG", "SD", davis_bacon_related_act=False, as_of=AS_OF)
    assert r3.davis_bacon_applies is False and r3.california_prevailing_wage_also_applies is None
    # 29 CFR 5.5(b): the CWHSSA clause goes in "in addition to" the Davis-Bacon clauses, and App. II(E) says "where
    # applicable", so on a grant that is not a Davis-Bacon Related Act coverage is not settled
    ot3 = req(r3, "CWHSSA weekly overtime")
    assert ot3.applies is None and "29 CFR 5.5(b)" in ot3.why and "confirm with the agency" in ot3.why
    assert req(r, "CWHSSA weekly overtime").applies is None                  # DBRA status unknown
    assert req(r2, "CWHSSA weekly overtime").applies is True                 # DBRA program


def test_fema_disaster_demolition():
    r = fp.federal_project_requirements(500_000, "FEMA", "County of Los Angeles", work_type="demolition",
                                        stafford_act_disaster_funds=True, as_of=AS_OF)
    assert req(r, "Build America, Buy America").applies is False                # Stafford Act exempt (184.8)
    assert "Demolition is covered when" in req(r, "Davis-Bacon prevailing wages").why


def test_material_supplier_and_no_federal_money():
    r = fp.federal_project_requirements(3_000_000, "faa aip", "San Diego County Regional Airport Authority",
                                        work_type="material supply", is_prime=False, your_contract_value_usd=60_000,
                                        as_of=AS_OF)
    assert r.davis_bacon_applies is False and "material supplier" in req(r, "Davis-Bacon").why
    assert not any(q.area == "Overtime" for q in r.requirements)
    assert "delivery only" in r.next_step
    n = fp.federal_project_requirements(250_000, "none", "City of Fresno", as_of=AS_OF)
    assert n.davis_bacon_applies is False and "public_works_readiness" in n.next_step
    assert [q.item for q in n.requirements] == ["Davis-Bacon prevailing wages"] and n.verified


def test_dual_coverage_assumed_for_usdot_money_without_an_agency():
    r = fp.federal_project_requirements(480_000, "FHWA", as_of=AS_OF)
    assert r.california_prevailing_wage_also_applies is True
    assert "Assumed" in req(r, "California prevailing wage also applies").why
    # a private owner on a grant job: not settled
    g = fp.federal_project_requirements(480_000, "federal grant", "Acme Homes LLC", davis_bacon_related_act=True,
                                        as_of=AS_OF)
    assert g.california_prevailing_wage_also_applies is None


def test_dbe_goal_rules_before_the_2025_pause():
    # as_of 2025-06-01 (before 2025-10-03): DBE commitments within 5 days after bid opening: 6/10 + 5 = 6/15
    r = caltrans_local_job(as_of="2025-06-01", bid_opening_date="2025-06-10", fhwa_obligation_date=None)
    assert req(r, "DBE contract goal commitments").applies is None
    assert kdate(r, "Latest DBE commitment").due == "2025-06-15"
    # FHWA-1273 still required before 2026-06-10
    assert req(r, "Form FHWA-1273").applies is True


def test_appendix_ii_clause_thresholds():
    # $120,000: termination (> $10,000) and anti-lobbying (> $100,000) in; clean air/water (> $150,000) and
    # breach remedies (> $350,000) out
    r = caltrans_local_job(prime_contract_value_usd=120_000, bid_opening_date=None)
    w = req(r, "Required federal grant clauses").what_to_do
    must, not_req = w.split("Not required at this value: ")
    assert "termination for cause and convenience (over $10,000)" in must and "anti-lobbying" in must
    assert "Clean Air Act" in not_req and "remedies for breach (over $350,000)" in not_req


def test_caltrans_prime_pays_subs_under_pcc_10262():
    # B&P 7108.5(e): applies to public works "except where Section 10262 of the Public Contract Code applies";
    # PCC 10262: on a state agency contract the prime pays subs within 7 days of each progress payment
    r = caltrans_local_job(awarding_agency="Caltrans District 11")
    pay = req(r, "Pay subcontractors within 7 days")
    assert pay.rule_ids[0] == fp.PCC_10262 and fp.BP7108 not in pay.rule_ids
    assert "PCC 10262" in pay.what_to_do and "Contractors State License Board" in pay.what_to_do
    assert r.verified
    # a city is not a state agency (B&P 7108.5), and a sub paying its own subs stays under B&P 7108.5
    assert fp.BP7108 in req(caltrans_local_job(), "Pay subcontractors").rule_ids
    sub = caltrans_local_job(awarding_agency="Caltrans", is_prime=False, your_contract_value_usd=50_000)
    assert fp.BP7108 in req(sub, "Pay subcontractors").rule_ids
    assert fp._is_state_agency("State of California DGS") and not fp._is_state_agency("UC San Diego")


def test_breach_remedies_follow_the_sat_in_effect():
    # 2 CFR 200 App. II(A): remedies for breach over the simplified acquisition threshold "which is the inflation
    # adjusted amount determined by the [FAR] Councils": $250,000 before 2025-10-01, $350,000 after (FAR 2.101).
    # An agency's own lower SAT (2 CFR 200.1) moves the 200.326 bonding threshold, not this clause.
    old = caltrans_local_job(prime_contract_value_usd=300_000, as_of="2025-06-01", bid_opening_date=None)
    assert "remedies for breach (over $250,000)" in req(old, "Required federal grant clauses").what_to_do.split(
        "Not required")[0]                                                     # $300,000 > $250,000
    new = caltrans_local_job(prime_contract_value_usd=300_000, bid_opening_date=None)
    assert "remedies for breach (over $350,000)" in req(new, "Required federal grant clauses").what_to_do.split(
        "Not required")[1]                                                     # $300,000 <= $350,000
    city = caltrans_local_job(prime_contract_value_usd=300_000, recipient_sat_usd=200_000, bid_opening_date=None)
    w = req(city, "Required federal grant clauses")
    assert "remedies for breach (over $350,000)" in w.what_to_do.split("Not required")[1]   # still the FAR SAT
    assert "Appendix II(A)" in w.why and fp.FAR_THRESH in w.rule_ids
    assert core.rule(fp.APPX_II).param("breach_remedies_threshold_is").startswith("the simplified acquisition")
    # ... while the bonds use the agency's own $200,000 threshold: $300,000 > $200,000
    assert req(city, "Federal minimum bonding").applies is True and city.bonds.regime == "2_cfr_200_326"


def test_subcontractor_anti_lobbying_goes_tier_by_tier():
    # 2 CFR 200 App. II(I): each tier certifies to the tier above on a bid/award over $100,000
    big = caltrans_local_job(is_prime=False, your_contract_value_usd=150_000)
    assert "your $150,000 subcontract is over $100,000, so you must certify" in req(
        big, "Required federal grant clauses").why
    small = caltrans_local_job(is_prime=False, your_contract_value_usd=100_000)     # $100,000 is not over $100,000
    assert "is not over $100,000, so you need not certify" in req(small, "Required federal grant clauses").why


def test_davis_bacon_related_act_flag_only_counts_for_grants():
    # a federal agency's own contract is covered by the Davis-Bacon Act itself; FHWA money is DBRA money
    u = usace_job(davis_bacon_related_act=False, include_unverified=False)
    assert u.davis_bacon_applies is True and any("davis_bacon_related_act was ignored" in n for n in u.notes)
    h = caltrans_local_job(davis_bacon_related_act=False)
    assert h.davis_bacon_applies is True and any("Related Act money" in n for n in h.notes)
    assert usace_job(recipient_sat_usd=100_000, include_unverified=False).bonds.regime == "miller_act"


def test_e_verify_flow_down_needs_the_prime_clause():
    # FAR 22.1803: the prime contract carries 52.222-54 only over $150,000 (and 120+ days); 52.222-54(e) then
    # reaches subcontracts over $3,500. $140,000 prime: nothing to flow down even to a $20,000 sub
    s = fp.federal_project_requirements(140_000, "USACE", is_prime=False, your_contract_value_usd=20_000,
                                        as_of=AS_OF, include_unverified=False)
    ev = req(s, "E-Verify flow-down")
    assert ev.applies is False and "$140,000 is not over $150,000" in ev.why and fp.EV_PRIME in ev.rule_ids
    s2 = fp.federal_project_requirements(400_000, "USACE", is_prime=False, your_contract_value_usd=20_000,
                                         as_of=AS_OF, include_unverified=False)
    assert req(s2, "E-Verify flow-down").applies is True                        # $20,000 > $3,500
    s3 = fp.federal_project_requirements(400_000, "USACE", is_prime=False, as_of=AS_OF, include_unverified=False)
    assert req(s3, "E-Verify flow-down").applies is None                        # subcontract amount not given


def test_design_build_and_e_verify_short_job():
    r = caltrans_local_job(design_build=True)
    assert req(r, "Perform 30% with your own forces").applies is False
    e = usace_job(period_of_performance_days=90, include_unverified=False)
    assert req(e, "E-Verify").applies is False and "under 120 days" in req(e, "E-Verify").why
    s = fp.federal_project_requirements(2_400_000, "USACE", is_prime=False, your_contract_value_usd=3_000,
                                        as_of=AS_OF, include_unverified=False)
    assert req(s, "E-Verify flow-down").applies is False                           # $3,000 <= $3,500


@pytest.mark.parametrize("kw,match", [
    (dict(prime_contract_value_usd=-1), "finite number above 0"),
    (dict(prime_contract_value_usd="$0"), "finite number above 0"),
    (dict(prime_contract_value_usd=float("inf")), "finite number"),
    (dict(prime_contract_value_usd="lots"), "must be a number"),
    (dict(funding_source="direct federal"), "California public agency, so its contract is not a direct federal"),
    (dict(awarding_agency="USACE"), "Use funding_source='direct_federal'"),
    (dict(your_contract_value_usd=10_000), "is_prime is true"),
    (dict(is_prime=False, your_contract_value_usd=600_000), "cannot exceed the prime contract"),
    (dict(bid_opening_date="2026-12-10", award_date="2026-12-01"), "before bid_opening_date"),
    (dict(bid_opening_date="next Tuesday"), "must be a date"),
    (dict(work_type="landscaping maintenance"), "Unknown work_type"),
    (dict(specialty_items_usd=500_000), "specialty_items_usd cannot exceed"),
    (dict(period_of_performance_days=12.5), "whole number"),
])
def test_requirements_bad_input(kw, match):
    with pytest.raises(ValueError, match=match):
        caltrans_local_job(**kw)


# ================================================================ davis_bacon_vs_ca_prevailing
def test_dlse_manual_example_dual_coverage():
    # DLSE Manual 4.2.2.1: BHR $25 + employer payments $15 = $40; federal 29 CFR 5.31 example $21.93 + $6.27 = $28.20
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, 25, 15, classification="Laborer", cash_rate_paid_usd=25,
                                        fringe_paid_usd=15)
    assert r.verified and r.coverage == "dual"
    assert (r.federal_total_usd, r.state_total_usd) == (28.20, 40)
    # cash floor = DIR basic $25; total = max($40, $28.20) = $40 (state controls)
    assert (r.required_cash_rate_min_usd, r.required_total_min_usd, r.total_controlled_by) == (25, 40, "state")
    # overtime: 1.5 x max($21.93, $25, $25) = $37.50 cash; + $6.27 fringe at straight time = $43.77
    assert (r.overtime_cash_min_usd, r.overtime_total_min_usd) == (37.50, 43.77)
    assert r.pay_check.complies and r.next_step.startswith("Your rates meet the test")
    assert "the DIR determination's overtime rate controls if higher" in r.explanation


def test_dual_coverage_cash_below_dir_basic_and_overtime_short():
    # $22 cash + $18 fringe = $40 total meets $40, but cash $22 < DIR basic $25: raise cash $3/h
    # back pay: $3 x 40 h = $120; overtime cash min $37.50 - $33 paid = $4.50/h x 6 h = $27; total $147
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, 25, state_total_rate_usd=40, cash_rate_paid_usd=22,
                                        fringe_paid_usd=18, straight_time_hours=40, overtime_hours=6,
                                        overtime_cash_rate_paid_usd=33)
    c = r.pay_check
    assert (c.cash_short_per_hour_usd, c.total_short_per_hour_usd, c.raise_cash_by_usd) == (3, 0, 3)
    assert (c.straight_time_shortfall_usd, c.overtime_short_per_hour_usd, c.overtime_shortfall_usd) == (120, 4.5, 27)
    assert not c.complies
    assert "raise the straight-time cash rate by $3 to $25 an hour" in r.next_step and "$147" in r.next_step
    assert any("checked against the federal total and the California cash floor only" in n for n in r.notes)


def test_total_package_short():
    # $30 cash + $5 fringe = $35 < $40: raise cash by $5
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, 25, 15, cash_rate_paid_usd=30, fringe_paid_usd=5)
    assert (r.pay_check.cash_short_per_hour_usd, r.pay_check.total_short_per_hour_usd) == (0, 5)
    assert r.pay_check.raise_cash_by_usd == 5


def test_federal_rate_higher_than_state():
    # federal $48.10 + $30.25 = $78.35 > state $45 + $25 = $70 -> total $78.35 (federal controls), cash floor $45
    # overtime base max($48.10, $45) = $48.10: 1.5 x 48.10 = $72.15 cash; + $30.25 = $102.40 total
    r = fp.davis_bacon_vs_ca_prevailing("48.10", "$30.25", 45, 25)
    assert (r.required_cash_rate_min_usd, r.required_total_min_usd, r.total_controlled_by) == (45, 78.35, "federal")
    assert (r.overtime_cash_min_usd, r.overtime_total_min_usd) == (72.15, 102.40)
    assert "federal overtime base is the federal rate" in r.explanation
    # with the DIR overtime total listed ($105.00 > $102.40), it controls
    assert fp.davis_bacon_vs_ca_prevailing(48.10, 30.25, 45, 25, state_overtime_rate_usd=105).overtime_total_min_usd \
        == 105


def test_federal_only_dol_example_and_its_arithmetic():
    # 29 CFR 5.31(b): basic $21.93 + fringe $6.27. They add to $28.20; the regulation's text says $28.60.
    ex = core.rule(fp.FRINGE).param("dol_example")
    assert (ex["basic_hourly_rate_usd"], ex["fringe_rate_usd"], ex["all_cash_total_usd"]) == (21.93, 6.27, 28.6)
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, coverage="Davis-Bacon only", cash_rate_paid_usd=28.20)
    assert r.required_total_min_usd == 28.20 and r.pay_check.complies
    assert r.required_cash_rate_min_usd is None
    assert any("states a $28.60 total for $21.93 + $6.27, but they add up to $28.20" in n for n in r.notes)
    # all-cash pay: the $6.27 above the basic rate is presumed paid in place of fringes and left out of the OT base
    # (29 CFR 5.32): 1.5 x $21.93 = $32.895 -> $32.90 cash; + $6.27 = $39.165 -> $39.17, the rule's own example
    assert "39.165" in core.rule(fp.OT_BASE).param("example_overtime_hour_usd.overtime_hour_minimum")
    assert (r.overtime_cash_min_usd, r.overtime_total_min_usd) == (32.90, 39.17)
    assert any("Presumed $6.27" in n for n in r.notes)
    # told that none of the cash is in place of fringes: OT base $30 -> $45 cash, $51.27 total
    r2 = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, coverage="federal only", cash_rate_paid_usd=30,
                                         cash_in_lieu_of_fringe_usd=0)
    assert (r2.overtime_cash_min_usd, r2.overtime_total_min_usd) == (45, 51.27)


def test_dual_all_cash_presumes_employer_payments_in_lieu():
    # $40 all cash on a $25 + $15 DIR rate: $15 presumed in lieu -> OT base $25 -> $37.50
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, 25, 15, cash_rate_paid_usd=40)
    assert r.pay_check.complies and r.overtime_cash_min_usd == 37.50
    assert any("Presumed $15" in n for n in r.notes)


@pytest.mark.parametrize("kw,match", [
    (dict(), "coverage 'dual' needs the DIR determination"),
    (dict(state_basic_rate_usd=25), "coverage 'dual' needs the DIR determination"),
    (dict(state_basic_rate_usd=25, state_employer_payments_usd=15, state_total_rate_usd=41), "not the state total"),
    (dict(state_basic_rate_usd=25, state_total_rate_usd=20), "below state_basic_rate_usd"),
    (dict(state_basic_rate_usd=25, state_total_rate_usd=40, fringe_paid_usd=10), "give cash_rate_paid_usd"),
    (dict(state_basic_rate_usd=25, state_total_rate_usd=40, cash_rate_paid_usd=20, cash_in_lieu_of_fringe_usd=21),
     "cannot exceed cash_rate_paid_usd"),
    (dict(state_basic_rate_usd=25, state_total_rate_usd=40, coverage="maybe"), "Unknown coverage"),
    (dict(state_basic_rate_usd=-25, state_total_rate_usd=40), "finite number"),
])
def test_wage_comparison_bad_input(kw, match):
    with pytest.raises(ValueError, match=match):
        fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, **kw)


def test_federal_only_ignores_state_rates_with_a_note():
    r = fp.davis_bacon_vs_ca_prevailing(21.93, 6.27, 25, 15, coverage="direct federal")
    assert r.state_basic_rate_usd is None and any("ignored" in n for n in r.notes)


# ================================================================ federal_overtime_exposure
def crew_week():
    """Week of Mon 11/2/2026. Ana 10 h Mon-Fri, no OT paid. Ben 8 h Mon-Sat, no OT paid. Cruz 12 h Mon with 4 OT
    hours paid, then 8 h Tue-Fri."""
    ts = [{"worker": "Ana", "date": f"2026-11-0{d}", "hours": 10} for d in range(2, 7)]
    ts += [{"worker": "Ben", "date": f"2026-11-0{d}", "hours": 8} for d in range(2, 8)]
    ts += [{"worker": "Cruz", "date": "2026-11-02", "hours": 12, "overtime_hours_paid": 4}]
    ts += [{"worker": "Cruz", "date": f"2026-11-0{d}", "hours": 8} for d in range(3, 7)]
    return ts


def test_overtime_exposure_dual_coverage_crew():
    r = fp.federal_overtime_exposure(crew_week(), basic_rate_usd=40, prime_contract_value_usd=480_000,
                                     funding_source="FHWA", awarding_agency="Caltrans", as_of=AS_OF)
    assert r.verified and r.cwhssa_applies and r.california_public_works
    w = {x.worker: x for x in r.weeks}
    # Ana 50 h: federal OT 50 - 40 = 10 h (all on Friday, hours 41-50) -> 1 LD day;
    # California: 2 h over 8 each day x 5 = 10 h, unpaid every day -> 5 LC 1813 days
    assert (w["Ana"].federal_overtime_hours, w["Ana"].california_overtime_hours) == (10, 10)
    assert w["Ana"].liquidated_damages_days == ["2026-11-06"] and len(w["Ana"].lc1813_days) == 5
    # Ben 48 h: Saturday hours 41-48 are federal OT and California weekly OT -> 1 LD day, 1 LC 1813 day
    assert (w["Ben"].liquidated_damages_days, w["Ben"].lc1813_days) == (["2026-11-07"], ["2026-11-07"])
    # Cruz 44 h: 4 OT hours paid on Monday (California daily) cover the 4 federal weekly OT hours -> nothing owed
    assert (w["Cruz"].unpaid_overtime_hours, w["Cruz"].liquidated_damages_days, w["Cruz"].lc1813_days) == (0, [], [])
    # LD: 2 worker-days x $33 = $66; LC 1813: 6 worker-days x $25 = $150
    assert (r.liquidated_damages_worker_days, r.liquidated_damages_usd) == (2, 66)
    assert (r.lc1813_worker_days, r.lc1813_penalty_usd) == (6, 150)
    # back wages: (10 + 8) unpaid OT h x 0.5 premium x $40 = $360; total $360 + $66 + $150 = $576
    assert (r.unpaid_overtime_hours, r.back_wages_usd, r.total_exposure_usd) == (18, 360, 576)
    # $66 <= $500: the agency head may waive inadvertent violations
    assert r.agency_may_waive
    assert r.next_step.startswith("Pay the 18 unpaid overtime hours ($360) to 2 workers (Ana, Ben)")
    assert "DIR eCPR" in r.next_step


def test_overtime_exposure_csv_direct_federal():
    csv_text = ("Name,Day,Hrs\nAna,11/02/2026,10\nAna,11/03/2026,10\nAna,11/04/2026,10\nAna,11/05/2026,10\n"
                "Ana,11/06/2026,10\n")
    r = fp.federal_overtime_exposure(csv_text, basic_rate_usd="$45.10", prime_contract_value_usd=2_400_000,
                                     funding_source="USACE", as_of=AS_OF)
    # $2,400,000 > $200,000; no California check on a federal agency's own contract
    assert r.cwhssa_applies and not r.california_public_works and r.lc1813_per_worker_day_usd is None
    # 10 h x 0.5 x $45.10 = $225.50; 1 LD day x $33
    assert (r.back_wages_usd, r.liquidated_damages_usd) == (225.50, 33)
    # no straight time paid at all for the OT hours: 10 h x 1.5 x $45.10 = $676.50
    r2 = fp.federal_overtime_exposure(csv_text, basic_rate_usd=45.10, straight_time_paid_for_all_hours=False,
                                      as_of=AS_OF)
    assert r2.back_wages_usd == 676.50 and r2.cwhssa_applies is None


def test_overtime_below_cwhssa_threshold_has_no_liquidated_damages():
    # direct federal $150,000 <= $200,000: no FAR overtime clause
    r = fp.federal_overtime_exposure(crew_week(), basic_rate_usd=40, prime_contract_value_usd=150_000,
                                     funding_source="GSA", as_of=AS_OF)
    assert r.cwhssa_applies is False and r.liquidated_damages_usd == 0 and r.liquidated_damages_worker_days == 0
    assert any("CWHSSA does not apply" in n for n in r.notes)


def test_workweek_start_changes_the_weeks():
    # Sun 11/1 - Sat 11/7 workweek: Mon-Sat 8 h = 48 h in one week; a Monday start puts Sun 11/8 in the next week
    ts = [{"worker": "Dee", "date": f"2026-11-0{d}", "hours": 8} for d in range(2, 9)]
    sun = fp.federal_overtime_exposure(ts, workweek_starts_on="Sun", as_of=AS_OF)
    mon = fp.federal_overtime_exposure(ts, workweek_starts_on="monday", as_of=AS_OF)
    # Sunday start: week of 11/1 holds Mon-Sat (48 h, 8 OT) and Sun 11/8 starts a new week
    assert [w.hours for w in sun.weeks] == [48, 8] and sun.unpaid_overtime_hours == 8
    # Monday start: 11/2-11/8 = 56 h -> 16 OT h, on Sat and Sun -> 2 LD days
    assert [w.hours for w in mon.weeks] == [56] and mon.liquidated_damages_worker_days == 2


def test_liquidated_damages_amount_by_date():
    # 29 CFR 5.8(a) (eCFR point-in-time copies): $29 from 2022-01-18, $31 from 2023-01-15, $32 from 2024-01-15,
    # $33 from 2025-01-15 - each change is on its date, not on January 1
    hist = core.rule(fp.CWH_LD).param("history_by_effective_date_usd")
    assert hist == {"2022-01-18": 29, "2023-01-15": 31, "2024-01-15": 32, "2025-01-15": 33}
    r = fp.federal_overtime_exposure(crew_week(), as_of="2024-06-01")
    assert r.liquidated_damages_per_worker_day_usd == 32
    assert any("priced at the $32 amount in effect from 2024-01-15" in n for n in r.notes)
    for as_of, amount in (("2024-01-14", 31), ("2024-01-15", 32), ("2023-01-14", 29), ("2023-01-15", 31),
                          ("2025-01-14", 32), ("2025-01-15", 33), ("2022-06-01", 29)):
        assert fp.federal_overtime_exposure(crew_week(), as_of=as_of).liquidated_damages_per_worker_day_usd == amount
    with pytest.raises(ValueError, match="before the earliest"):
        fp.federal_overtime_exposure(crew_week(), as_of="2022-01-17")


@pytest.mark.parametrize("ts,match", [
    ([{"worker": "A", "date": "2026-11-02", "hours": 8, "overtime_hours_paid": 9}], "more than the hours worked"),
    ([{"worker": "A", "date": "2026-11-02", "hours": 16}, {"worker": "A", "date": "2026-11-02", "hours": 10}],
     "a day has 24"),
    ([{"worker": "A", "date": "someday", "hours": 8}], "must be a date"),
    ([{"worker": "A", "date": "2026-11-02", "hours": -1}], "greater than or equal to 0"),
    ([], "non-empty list"),
    ("worker,date,hours\nA,2026-11-02,eight\n", "timesheet line 2"),
])
def test_overtime_bad_timesheets(ts, match):
    with pytest.raises(ValueError, match=match):
        fp.federal_overtime_exposure(ts, as_of=AS_OF)


def test_overtime_bad_settings():
    with pytest.raises(ValueError, match="weekday name"):
        fp.federal_overtime_exposure(crew_week(), workweek_starts_on="xx", as_of=AS_OF)
    with pytest.raises(ValueError, match="direct federal contract is under federal control"):
        fp.federal_overtime_exposure(crew_week(), funding_source="USACE", california_public_works=True, as_of=AS_OF)


# ================================================================ buy_america_check
PANEL = [{"name": "motor", "cost_usd": 4000, "domestic": False},
         {"name": "enclosure", "cost_usd": 3000, "domestic": True, "iron_steel": True},
         {"name": "controller", "cost_usd": "$3,000", "domestic": True}]


def test_baba_manufactured_product_55_percent():
    # 2 CFR 184.5: ($3,000 + $3,000) / $10,000 = 60% > 55%
    r = fp.buy_america_check("BABA", "manufactured product", "pump control panel", components=PANEL,
                             manufactured_in_us=True)
    assert r.verified and r.complies and r.domestic_content_pct == 60 and r.required_pct_greater_than == 55
    r2 = fp.buy_america_check("baba", "manufactured", domestic_component_cost_usd=5500, total_component_cost_usd=10000,
                              manufactured_in_us=True)
    assert r2.complies is False                                               # 55% is not "greater than 55%"
    r3 = fp.buy_america_check("baba", "manufactured", components=PANEL)
    assert r3.complies is None and "manufactured_in_us" in r3.missing


@pytest.mark.parametrize("program,extra", [("baba", {}), ("fhwa", {"fhwa_obligation_date": "2026-10-15"})])
def test_exactly_55_percent_fails_despite_float_noise(program, extra):
    # 2 CFR 184.5 and 23 CFR 635.410(c) both need MORE than 55%. $2,750 / $5,000 is exactly 55%, although
    # 2750 / 5000 * 100 evaluates to 55.00000000000001 in binary floating point.
    comps = [{"name": "housing", "cost_usd": 2750, "domestic": True}, {"name": "drive", "cost_usd": 2250,
                                                                       "domestic": False}]
    r = fp.buy_america_check(program, "manufactured product", components=comps, manufactured_in_us=True, **extra)
    assert r.domestic_content_pct == 55 and r.complies is False
    assert "= 55% <= 55%" in line(r, "Domestic component cost share").math


def test_far_manufactured_item_with_iron_components_gets_a_hint():
    # FAR 52.225-9 tests material that is wholly or predominantly iron/steel by its foreign iron and steel content;
    # the module flags the iron/steel share so the user can re-run as iron_steel. ($3,000 + $3,000) / $10,000 = 60%
    r = fp.buy_america_check("FAR", "manufactured product", components=PANEL, manufactured_in_us=True,
                             delivery_date="2026-12-01")
    assert r.complies is False                                                   # 60% is not > 65%
    assert any("Iron and steel are 30% of this item's component cost" in n for n in r.notes)


@pytest.mark.parametrize("delivered,dom,ok,threshold", [
    ("2026-12-01", 6500, False, 65),   # 65% is not > 65% (items delivered 2024-2028)
    ("2026-12-01", 6600, True, 65),
    ("2029-02-01", 7000, False, 75),   # 75% from 2029
    ("2023-06-01", 6100, True, 60),    # 60% for items delivered before 2024
])
def test_far_buy_american_delivery_year_bands(delivered, dom, ok, threshold):
    r = fp.buy_america_check("Buy American", "manufactured", domestic_component_cost_usd=dom,
                             total_component_cost_usd=10000, manufactured_in_us=True, delivery_date=delivered)
    assert r.complies is ok and r.required_pct_greater_than == threshold


def test_far_iron_steel_and_cost_exception():
    # FAR 52.225-9: foreign iron/steel under 5% of all component cost: $400 / $10,000 = 4%
    r = fp.buy_america_check("FAR", "steel", foreign_iron_steel_component_cost_usd=400, total_component_cost_usd=10000,
                             manufactured_in_us=True, cost_if_domestic_usd=125_000, cost_if_foreign_usd=100_000)
    assert r.complies and r.foreign_iron_steel_pct == 4
    # domestic $125,000 vs foreign $100,000 = +25% > 20% -> unreasonable cost, ask the contracting officer
    assert r.exception_possible and "+25% > 20%" in r.exception_test
    r2 = fp.buy_america_check("FAR", "steel", foreign_iron_steel_component_cost_usd=500,
                              total_component_cost_usd=10000, manufactured_in_us=True,
                              cost_if_domestic_usd=115_000, cost_if_foreign_usd=100_000)
    assert r2.complies is False and r2.exception_possible is False            # 5% is not < 5%; 15% <= 20%
    assert "ask the contracting officer for an exception" in r2.next_step
    r3 = fp.buy_america_check("FAR", "steel", foreign_iron_steel_component_cost_usd=0, total_component_cost_usd=1,
                              manufactured_in_us=True, critical_item=True, cost_if_domestic_usd=130,
                              cost_if_foreign_usd=100)
    assert r3.exception_possible is None and "FAR 25.105" in r3.exception_test
    # COTS: content test waived, U.S. manufacture still needed
    assert fp.buy_america_check("FAR", "manufactured", cots=True, manufactured_in_us=True).complies is True


def test_fhwa_steel_minimal_use():
    # 23 CFR 635.410(b)(4): max(0.1% x $4,800,000 = $4,800, $2,500) = $4,800; $4,000 used -> $800 left
    r = fp.buy_america_check("FHWA", "rebar", total_contract_cost_usd=4_800_000,
                             contract_foreign_iron_steel_cost_usd=4_000, all_manufacturing_in_us=False)
    assert r.complies and (r.minimal_use_allowance_usd, r.minimal_use_remaining_usd) == (4_800, 800)
    assert "minimal-use allowance" in r.next_step and "$800 left" in r.next_step
    # max(0.1% x $480,000 = $480, $2,500) = $2,500 < $4,000 used
    r2 = fp.buy_america_check("Caltrans federal-aid", "steel", total_contract_cost_usd=480_000,
                              contract_foreign_iron_steel_cost_usd=4_000, all_manufacturing_in_us=False)
    assert r2.complies is False and r2.minimal_use_allowance_usd == 2_500
    assert "$1,500 over" in line(r2, "Foreign steel/iron used").math
    # alternate bids (635.410(b)(3)): $1.3M domestic vs $1.0M foreign = +30% > 25% -> foreign bid wins
    r3 = fp.buy_america_check("fhwa", "steel", all_manufacturing_in_us=True, cost_if_domestic_usd=1_300_000,
                              cost_if_foreign_usd=1_000_000)
    assert r3.complies and r3.exception_possible and "foreign-steel bid" in r3.exception_test
    r4 = fp.buy_america_check("fhwa", "steel", all_manufacturing_in_us=True, cost_if_domestic_usd=1_200_000,
                              cost_if_foreign_usd=1_000_000)
    assert r4.exception_possible is False and "domestic bid" in r4.exception_test


@pytest.mark.parametrize("obligated,applies,threshold", [
    ("2026-10-15", True, 55),      # on/after 2026-10-01: made in U.S. + > 55% U.S. components
    ("2025-11-01", True, None),    # on/after 2025-10-01: made in U.S. only
    ("2025-06-01", False, None),   # before 2025-10-01: not covered by the rulebook's rule
])
def test_fhwa_manufactured_products_by_obligation_date(obligated, applies, threshold):
    r = fp.buy_america_check("FHWA", "manufactured product", components=PANEL, manufactured_in_us=True,
                             fhwa_obligation_date=obligated)
    assert r.applies is applies and r.required_pct_greater_than == threshold
    if applies:
        assert r.complies is True and any("precast concrete" in n for n in r.notes)


def test_baba_iron_steel_classification_and_waiver():
    # iron/steel 70% of component cost (> 50%): BABA treats the "manufactured product" as an iron or steel product
    comps = [{"name": "frame", "cost_usd": 7000, "domestic": True, "iron_steel": True},
             {"name": "electronics", "cost_usd": 3000, "domestic": False}]
    r = fp.buy_america_check("BABA", "manufactured product", components=comps, all_manufacturing_in_us=False,
                             cost_if_domestic_usd=1_300_000, cost_if_foreign_usd=1_000_000)
    assert r.item_type == "iron_steel" and r.complies is False
    # 2 CFR 184.7: +30% overall project cost > 25% -> unreasonable cost waiver can be requested; 15-day comment
    assert r.exception_possible and "at least 15 days" in r.exception_test
    assert "ask the awarding agency to request a waiver" in r.next_step


@pytest.mark.parametrize("item_type,name,category", [
    ("lumber", None, "lumber"), ("construction material", "PVC pipe", "plastic and polymer-based products"),
    ("Sheetrock", None, "drywall"), ("aluminum", None, "non-ferrous metals"),
])
def test_baba_construction_materials(item_type, name, category):
    r = fp.buy_america_check("baba", item_type, name, all_manufacturing_in_us=True)
    assert r.item_type == "construction_material" and r.construction_material_category == category and r.complies


def test_baba_exclusions_and_missing_facts():
    r = fp.buy_america_check("baba", "aggregate")
    assert r.applies is False and "aggregates" in r.test and r.complies is None
    s = fp.buy_america_check("baba", "manufactured", stafford_act_disaster_funds=True)
    assert s.applies is False and "Stafford" in s.test
    m = fp.buy_america_check("baba", "construction material")
    assert m.complies is None and any("item_name" in x for x in m.missing)
    assert m.next_step.startswith("Get from the supplier or agency")


@pytest.mark.parametrize("kw,match", [
    (dict(program="Buy America", item_type="steel"), "ambiguous"),
    (dict(program="baba", item_type="unobtanium"), "Unknown item_type"),
    (dict(program="nope", item_type="steel"), "Unknown program"),
    (dict(program="baba", item_type="manufactured", components=PANEL, total_component_cost_usd=10), "not both"),
    (dict(program="baba", item_type="manufactured", domestic_component_cost_usd=11, total_component_cost_usd=10),
     "cannot exceed the total"),
    (dict(program="baba", item_type="manufactured", cost_if_domestic_usd=10), "give both"),
    (dict(program="baba", item_type="manufactured", total_component_cost_usd=0), "more than 0"),
])
def test_buy_america_bad_input(kw, match):
    with pytest.raises(ValueError, match=match):
        fp.buy_america_check(**kw)


# ================================================================ MCP
def test_tool_descriptions_carry_rulebook_numbers():
    docs = fp._tool_docs()
    assert f"over ${core.rule(fp.DBA).param('prime_contract_value_greater_than_usd'):,.0f}" in docs["requirements"]
    assert "$33 per worker per calendar day" in docs["overtime"]
    assert "over 55% U.S. components" in docs["buy_america"]
    assert "$25 per worker per day" in docs["overtime"]


def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    fp.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            reqs = await c.call_tool("federal_project_requirements", {
                "prime_contract_value_usd": "$480,000", "funding_source": " fhwa ", "awarding_agency": "SD",
                "specialty_items_usd": 40_000, "as_of": AS_OF})
            wages = await c.call_tool("davis_bacon_vs_ca_prevailing", {
                "federal_basic_rate_usd": "21.93", "federal_fringe_rate_usd": 6.27, "state_basic_rate_usd": 25,
                "state_total_rate_usd": "$40.00", "cash_rate_paid_usd": 22, "fringe_paid_usd": 18,
                "straight_time_hours": 40})
            ot = await c.call_tool("federal_overtime_exposure", {
                "timesheet": crew_week(), "basic_rate_usd": 40, "prime_contract_value_usd": 480_000,
                "funding_source": "Caltrans federal aid", "awarding_agency": "City of San Diego", "as_of": AS_OF})
            ba = await c.call_tool("buy_america_check", {
                "program": "FHWA", "item_type": "rebar", "total_contract_cost_usd": 4_800_000,
                "contract_foreign_iron_steel_cost_usd": "4,000", "all_manufacturing_in_us": False})
            bad = [await c.call_tool(name, args, raise_on_error=False) for name, args in [
                ("federal_project_requirements", {"prime_contract_value_usd": "Infinity", "funding_source": "FHWA"}),
                ("federal_project_requirements", {"prime_contract_value_usd": 1, "funding_source": "federal"}),
                ("davis_bacon_vs_ca_prevailing", {"federal_basic_rate_usd": 0, "federal_fringe_rate_usd": 1}),
                ("federal_overtime_exposure", {"timesheet": [{"worker": "A", "date": "x", "hours": 8}]}),
                ("buy_america_check", {"program": "Buy America", "item_type": "steel"}),
            ]]
            return tools, reqs.structured_content, wages.structured_content, ot.structured_content, \
                ba.structured_content, bad
    tools, reqs, wages, ot, ba, bad = asyncio.run(run())
    assert set(tools) == {"federal_project_requirements", "davis_bacon_vs_ca_prevailing", "federal_overtime_exposure",
                          "buy_america_check"}
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.open_world_hint is False
    assert "$150,000" in tools["federal_project_requirements"].description            # Miller Act, from the rulebook
    # same numbers as the direct calls
    assert reqs["awarding_agency"] == "City of San Diego" and reqs["california_prevailing_wage_also_applies"] is True
    assert reqs["bonds"]["bid_guarantee_usd"] == 24_000 and reqs["verified"] is True    # 5% x $480,000
    assert any(x["item"].startswith("Own-forces") and x["value"] == 132_000 for x in reqs["line_items"])
    assert wages["pay_check"]["raise_cash_by_usd"] == 3 and wages["pay_check"]["straight_time_shortfall_usd"] == 120
    assert (ot["liquidated_damages_usd"], ot["lc1813_penalty_usd"], ot["back_wages_usd"]) == (66, 150, 360)
    assert ba["complies"] is True and ba["minimal_use_remaining_usd"] == 800
    assert all(x["disclaimer"] == core.DISCLAIMER for x in (reqs, wages, ot, ba))
    assert all(b.is_error for b in bad)
    assert "ambiguous" in bad[1].content[0].text and "ambiguous" in bad[4].content[0].text


# ================================================================ 2026-11-25 legal review regressions
def test_state_agency_contract_is_outside_2cfr200_326_bonds():
    # 2 CFR 200.317: "a State ... must follow the same policies and procedures it uses for procurements with
    # non-Federal funds"; 2 CFR 200.1 "State" includes any agency or instrumentality of the State (Caltrans), not
    # local governments. So the 5% / 100% / 100% federal minimum of 200.326 is a local-agency rule.
    state = caltrans_local_job(awarding_agency="Caltrans District 11")
    b = req(state, "Federal minimum bonding")
    assert b.applies is False and "2 CFR 200.317" in b.why and fp.STATE_PROC in b.rule_ids and b.verified
    assert state.bonds.regime == "none" and state.bonds.bid_guarantee_usd is None
    assert not any(x.item.endswith("(2 CFR 200.326)") for x in state.line_items)
    assert "Line up your surety" not in state.next_step
    assert core.rule(fp.STATE_PROC).param("state_must_also_comply_with") == ["200.321", "200.322", "200.323",
                                                                               "200.327"]
    # App. II clauses still reach the State's contract (200.327 is on the must-comply list)
    assert req(state, "Required federal grant clauses").applies is True
    # a city on the same money is a local government: the federal minimum applies ($480,000 > $350,000)
    city = caltrans_local_job()
    assert req(city, "Federal minimum bonding").applies is True and city.bonds.bid_guarantee_usd == 24_000
    # a sub on the Caltrans job gets no bond item and no prime bond amounts
    sub = caltrans_local_job(awarding_agency="Caltrans", is_prime=False, your_contract_value_usd=50_000)
    assert sub.bonds.regime == "none" and not any(q.area == "Bonds" for q in sub.requirements)
    assert fp.STATE_PROC in {c.rule_id for c in sub.citations}


def test_fta_buy_america_needs_every_component_us_origin():
    # 49 CFR 661.5(d): all manufacturing processes in the U.S. AND "All of the components of the product must be of
    # U.S. origin" - stricter than BABA's > 55%. A 60% product passes BABA but fails FTA.
    baba = fp.buy_america_check("baba", "manufactured", domestic_component_cost_usd=60, total_component_cost_usd=100,
                                manufactured_in_us=True)
    fta = fp.buy_america_check("FTA", "manufactured", domestic_component_cost_usd=60, total_component_cost_usd=100,
                               manufactured_in_us=True)
    assert baba.complies is True
    assert fta.program == "fta_buy_america" and fta.complies is False and fta.verified
    assert any("would pass the BABA 55% test" in n for n in fta.notes)
    assert "FTA waiver" in fta.next_step and line(fta, "U.S.-origin component share").value == 60
    ok = fp.buy_america_check("transit", "manufactured", components=[
        {"name": "frame", "cost_usd": 700, "domestic": True}, {"name": "drive", "cost_usd": 300, "domestic": True}],
        manufactured_in_us=True)
    assert ok.complies is True
    assert fp.buy_america_check("fta", "manufactured", domestic_component_cost_usd=100, total_component_cost_usd=100,
                                manufactured_in_us=False).complies is False          # final manufacture abroad
    # steel: all processes in the U.S.; asks for the fact when it is missing
    steel = fp.buy_america_check("federal transit", "rebar")
    assert steel.complies is None and steel.missing[0].startswith("all_manufacturing_in_us")
    assert "refinement of steel additives" in steel.test
    # a construction material FTA's rule does not classify: not settled, never a silent pass
    lumber = fp.buy_america_check("FTA", "lumber", all_manufacturing_in_us=True)
    assert lumber.applies is True and lumber.complies is None and "cannot settle" in lumber.next_step
    assert any("49 CFR 661.11" in n for n in lumber.notes)                            # rolling stock not tested


@pytest.mark.parametrize("dom,foreign,waiver", [(125, 100, False), (125.01, 100, True), (124.99, 100, False)])
def test_fta_price_differential_waiver_boundary(dom, foreign, waiver):
    # 49 CFR 661.7(d): granted "if the amount of the lowest ... bid offering the item ... not produced in the United
    # States multiplied by 1.25 is less than the amount of the lowest ... bid offering the item ... produced in the
    # United States": $100 x 1.25 = $125, so $125 exactly is no waiver.
    r = fp.buy_america_check("FTA", "steel", all_manufacturing_in_us=False, cost_if_domestic_usd=dom,
                             cost_if_foreign_usd=foreign)
    assert r.exception_possible is waiver and "x 1.25" in r.exception_test


def test_faa_is_not_silently_tested_as_baba():
    for name in ("FAA", "faa aip", "AIP", "federal_airport", "Airport Improvement Program"):
        with pytest.raises(ValueError, match="FAA's own Buy American terms"):
            fp.buy_america_check(name, "manufactured", domestic_component_cost_usd=60, total_component_cost_usd=100,
                                 manufactured_in_us=True)
    r = fp.federal_project_requirements(5_000_000, "FAA AIP", "San Diego County Regional Airport Authority",
                                        as_of=AS_OF)
    baba = req(r, "Build America, Buy America")
    assert baba.applies is None and "FAA's own Buy American terms" in baba.why and "airport sponsor" in baba.what_to_do


def test_far_self_performance_clause_is_optional_at_or_below_2m():
    # FAR 36.501(b): required over $2 million; "may insert the clause" at $2 million or less
    r = usace_job(prime_contract_value_usd=2_000_000)
    sp = req(r, "Self-performance clause")
    assert sp.applies is False and "lets the contracting officer include it" in sp.what_to_do
    assert core.rule(fp.SELF_PERF).param("may_insert_at_or_below_threshold") is True
    assert req(usace_job(), "Self-performance clause").applies is True             # $2,400,000


@pytest.mark.parametrize("name,kind", [
    ("Salvation Army", "other"), ("Federal Express", "other"), ("U.S. Army Fort Irwin", "federal_agency"),
    ("Department of the Army", "federal_agency"), ("US Army Corps of Engineers", "federal_agency"),
    ("California Army National Guard", "california_public_agency"), ("Federal Aviation Administration",
                                                                      "federal_agency"),
    ("California Department of Veterans Affairs", "california_public_agency"), ("VA Palo Alto", "federal_agency"),
])
def test_agency_words_are_not_too_broad(name, kind):
    assert fp.classify_agency(name)[0] == kind


def test_nonprofit_grant_subrecipient_is_not_called_federal():
    r = fp.federal_project_requirements(400_000, "HUD", "Salvation Army", as_of=AS_OF)
    assert r.awarding_agency_kind == "other" and r.funding_source == "federal_grant"


@pytest.mark.parametrize("value", ["Caltrans", "City of LA", "County of San Diego"])
def test_funding_source_given_as_an_agency_gets_a_hint(value):
    with pytest.raises(ValueError, match="names an agency, not where the money comes from"):
        fp.federal_project_requirements(400_000, value, as_of=AS_OF)


def test_string_flags_are_parsed_not_truthy():
    # bool("false") is True in Python; a form that posts "false" must not become a prime contractor
    r = fp.federal_project_requirements(400_000, "USACE", is_prime="false", your_contract_value_usd=100_000,
                                        as_of=AS_OF)
    assert r.your_role == "subcontractor"
    assert fp.federal_project_requirements(400_000, "USACE", is_prime="No", as_of=AS_OF).your_role == "subcontractor"
    assert fp.federal_project_requirements(900_000, "HUD", "SD", davis_bacon_related_act="yes",
                                           as_of=AS_OF).davis_bacon_applies is True
    with pytest.raises(ValueError, match="is_prime must be true or false"):
        fp.federal_project_requirements(400_000, "USACE", is_prime="maybe", as_of=AS_OF)
    assert fp.buy_america_check("baba", "manufactured", domestic_component_cost_usd=60, total_component_cost_usd=100,
                                manufactured_in_us="yes").complies is True
    ot = fp.federal_overtime_exposure(crew_week(), funding_source="FHWA", awarding_agency="City of San Diego",
                                      california_public_works="false", as_of=AS_OF)
    assert ot.california_public_works is False


def test_absurd_amounts_and_bad_components_get_clear_errors():
    with pytest.raises(ValueError, match="not a realistic figure"):
        fp.federal_project_requirements(1e15, "USACE", as_of=AS_OF)
    with pytest.raises(ValueError, match="not a realistic figure"):
        fp.davis_bacon_vs_ca_prevailing(1e13, 1, coverage="federal_only")
    with pytest.raises(ValueError, match="component 1: cost_usd"):
        fp.buy_america_check("baba", "manufactured", components=[{"name": "x", "cost_usd": -1, "domestic": True}])
    with pytest.raises(ValueError, match="component 2: cost_usd"):
        fp.buy_america_check("baba", "manufactured", components=[{"name": "a", "cost_usd": 1, "domestic": True},
                                                                  {"name": "x", "cost_usd": 1e13, "domestic": True}])
    with pytest.raises(ValueError, match="components must be a list"):
        fp.buy_america_check("baba", "manufactured", components={"name": "x"})


def test_overtime_tool_grant_coverage_follows_davis_bacon():
    # 29 CFR 5.5(b): on grant money the CWHSSA clause rides with the Davis-Bacon clauses; still priced as if it applies
    r = fp.federal_overtime_exposure(crew_week(), prime_contract_value_usd=500_000, funding_source="HUD CDBG",
                                     as_of=AS_OF)
    assert r.cwhssa_applies is None and "29 CFR 5.5(b)" in r.cwhssa_reason and r.liquidated_damages_usd > 0
    hw = fp.federal_overtime_exposure(crew_week(), prime_contract_value_usd=500_000, funding_source="FHWA",
                                      as_of=AS_OF)
    assert hw.cwhssa_applies is True


def test_leave_and_minimum_wage_items_do_not_overstate_california_law():
    r = usace_job()
    assert "check whether it also reaches this site" in req(r, "Federal paid sick leave").what_to_do
    assert "California minimum" not in req(r, "Federal contractor minimum wage").what_to_do


def test_fta_and_faa_through_mcp_client():
    mcp = FastMCP("t")
    fp.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            fta = await c.call_tool("buy_america_check", {
                "program": "FTA", "item_type": "manufactured product", "domestic_component_cost_usd": "$60",
                "total_component_cost_usd": 100, "manufactured_in_us": True})
            faa = await c.call_tool("buy_america_check", {"program": "FAA", "item_type": "steel"},
                                    raise_on_error=False)
            sub = await c.call_tool("federal_project_requirements", {
                "prime_contract_value_usd": 480_000, "funding_source": "FHWA", "awarding_agency": "Caltrans",
                "as_of": AS_OF})
            return tools, fta.structured_content, faa, sub.structured_content
    tools, fta, faa, state = asyncio.run(run())
    assert "fta_buy_america" in tools["buy_america_check"].description
    assert fta["program"] == "fta_buy_america" and fta["complies"] is False
    assert faa.is_error and "FAA's own Buy American terms" in faa.content[0].text
    assert state["bonds"]["regime"] == "none"


def test_ignored_inputs_are_reported():
    r = caltrans_local_job(awarding_agency="Caltrans", recipient_sat_usd=100_000, stafford_act_disaster_funds=True,
                           infrastructure_project=False)
    assert r.bonds.regime == "none"
    assert any(n.startswith("recipient_sat_usd was ignored for bonding") for n in r.notes)
    assert any(n.startswith("stafford_act_disaster_funds was ignored") for n in r.notes)
    assert any(n.startswith("infrastructure_project was ignored") for n in r.notes)
    assert req(r, "FHWA Buy America").applies is True                  # not switched off by the stray flags
