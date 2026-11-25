"""Public works module: hand-computed California prevailing wage / DIR / apprenticeship / listing scenarios."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, public_works as pw

AS_OF = "2026-11-25"


def task(result, start):
    return next(t for t in result.checklist if t.task.startswith(start))


def line(result, start):
    return next(x for x in result.line_items if x.item.startswith(start))


def school_remodel(**kw):
    """$480,000 school district modernization, bid 11/10/2026, awarded 11/17, signed 12/1, crew starts Mon 1/11/2027,
    done 8/31/2027. Prime registered with DIR through 6/30/2027."""
    args = dict(project_value_usd=480_000, awarding_body_type="school_district", is_public_funds=True,
                work_type="alteration", start_date="2027-01-11", contractor_dir_registered=True,
                dir_registration_expiry="2027-06-30",
                subcontractors=[
                    {"name": "Volt Electric", "license_number": "1012345", "dir_number": "1000045678",
                     "bid_amount_usd": 62_000, "portion_of_work": "electrical"},
                    {"name": "Brush Painting", "license_number": "998877", "bid_amount_usd": 2_100,
                     "portion_of_work": "painting"}],
                as_of=AS_OF, award_date="2026-11-17", contract_execution_date="2026-12-01",
                bid_opening_date="2026-11-10", estimated_completion_date="2027-08-31",
                journeyman_hours_by_craft={"electrician": 1250, "laborer": 30})
    args.update(kw)
    return pw.public_works_readiness(**args)


# ---------------------------------------------------------------- full-compliance project
def test_school_remodel_full_compliance_dates_and_amounts():
    r = school_remodel()
    c = r.coverage
    # $480,000 > $1,000 (prevailing wage), > $25,000 (no small-project exemption), >= $30,000 (apprenticeship)
    assert (c.public_work, c.prevailing_wage_applies, c.tier, c.apprenticeship_applies) == (True, True, "full", True)
    assert c.small_project_limit_usd == 25_000 and not c.small_project_exempt
    # PWC-100: award 11/17 + 30 days = 12/17/2026, earlier than the 1/11 start
    assert r.pwc100_required and r.pwc100_due == "2026-12-17"
    # eCPR: first upload start 1/11 + 30 = 2/10/2027; penalty only after +14 = 2/24; final: 8/31 + 30 = 9/30/2027
    p = r.payroll
    assert p.ecpr_upload_required and p.max_days_between_uploads == 30
    assert (p.first_upload_due, p.penalty_possible_after, p.final_upload_due) == ("2027-02-10", "2027-02-24",
                                                                                    "2027-09-30")
    assert (p.ecpr_late_penalty_per_day_usd, p.ecpr_late_penalty_max_per_project_usd) == (100, 5000)
    assert (p.cpr_request_response_days, p.cpr_late_penalty_per_worker_per_day_usd) == (10, 100)
    # Apprenticeship: DAS 140 = min(signed 12/1 + 10 = 12/11, start 1/11) = 12/11/2026
    # DAS 142: 72 h = 3 weekdays before Mon 1/11 -> Fri 1/8, Thu 1/7, Wed 1/6
    # Training fund: 15th of the month after January -> 2/15/2027; hours statement 8/31 + 60 = 10/30/2027
    a = r.apprenticeship
    assert (a.das140_due, a.das142_request_by, a.training_fund_first_due, a.hours_statement_due) == (
        "2026-12-11", "2027-01-06", "2027-02-15", "2027-10-30")
    # Electricians 1,250 h / 5 = 250 apprentice h; DAS 142 >= max(8, 20% x 250 = 50) = 50 h
    # Laborers 30 h / 5 = 6 h; under 40 journeyman h -> no minimum request size
    crafts = {x.craft: x for x in a.crafts}
    assert (crafts["electrician"].required_apprentice_hours, crafts["electrician"].das142_min_request_hours) == (250, 50)
    assert (crafts["laborer"].required_apprentice_hours, crafts["laborer"].das142_min_request_hours) == (6, None)
    # Listing: 0.5% x $480,000 = $2,400. Electric $62,000 must be listed; 10% x 62,000 = $6,200 max penalty.
    s = r.subcontractor_listing
    assert s.applies and s.threshold_usd == 2400
    volt, brush = s.subs
    assert volt.must_list and volt.ok and volt.listing_violation_penalty_max_usd == 6200
    assert brush.must_list is False and not brush.ok            # $2,100 < $2,400 but still needs DIR registration
    assert "No DIR registration number" in brush.problems[0] and "$10,000" in brush.problems[0]
    # Retention: 5% x $480,000 = $24,000; release 8/31 + 60 = 10/30/2027; pass-through 7 days; final pay hold 30 days
    t = r.retention
    assert (t.max_retention_pct, t.max_total_retention_usd, t.release_due) == (5, 24_000, "2027-10-30")
    assert (t.pass_through_days_after_receipt, t.disputed_withhold_max_pct, t.final_payment_hold_days_after_sub_list) \
        == (7, 150, 30)
    # Records: B&P 7111 5 years after 8/31/2027 completion
    assert r.record_retention.keep_until == "2032-08-31"
    assert len(r.jobsite_postings) == 3 and any("CMU" in x for x in r.jobsite_postings)


def test_school_remodel_registration_renewal_and_checklist():
    r = school_remodel()
    reg = r.dir_registration
    # Expires 6/30/2027, job runs to 8/31/2027: renew 1 fiscal year (FY ending 6/30/2028) x $400 = $400 by 6/30/2027
    assert (reg.required, reg.status, reg.fiscal_years_to_buy, reg.fee_usd, reg.fee_due_by) == (
        True, "renew_before_completion", 1, 400, "2027-06-30")
    assert reg.portal == "https://services.dir.ca.gov/pw"
    assert "fiscal year ending 2028-06-30" in line(r, "DIR registration fee").math
    # Bid already opened 11/10; sub checks move to the next milestone (contract signing 12/1 = 6 days out)
    v = task(r, "Verify every subcontractor")
    assert (v.due, v.status, v.days_left) == ("2026-12-01", "due_soon", 6) and "Brush Painting" in v.task
    assert task(r, "Send DAS 140").due == "2026-12-11"
    assert task(r, "Renew DIR registration").due == "2027-06-30"
    dues = [t.due for t in r.checklist if t.due]
    assert dues == sorted(dues)                                  # sorted by due date, undated last
    assert r.checklist[-1].due is None
    assert "0 checklist item(s) overdue, 1 due within 14 days" in r.summary
    # Only the LC 1720 definition is unchecked; every number used is verified
    assert r.verified is False and r.unverified_rule_ids == ["pw-definition-public-works"]
    assert all(x.verified for x in r.line_items) and all(t.verified for t in r.checklist)
    assert {"dir-registration-fee", "ecpr-late-penalty", "sub-listing-threshold", "public-retention-5pct-cap",
            "apprentice-ratio-1-to-5"} <= {c.rule_id for c in r.citations}


# ---------------------------------------------------------------- coverage tiers
def test_small_repair_job_skips_registration_ecpr_and_pwc100_but_keeps_payroll():
    # City repair $18,500 <= $25,000: prevailing wage yes (> $1,000), no DIR registration/eCPR/PWC-100, no apprentices
    r = pw.public_works_readiness(18_500, "city", True, "repair", "2026-12-07", False, as_of=AS_OF,
                                  estimated_completion_date="2027-01-15")
    assert (r.coverage.tier, r.coverage.prevailing_wage_applies, r.coverage.small_project_exempt) == (
        "small_project", True, True)
    assert r.dir_registration.status == "not_required" and r.dir_registration.fee_usd == 0
    assert not r.pwc100_required and not r.payroll.ecpr_upload_required and r.payroll.certified_payroll_required
    assert not r.apprenticeship.applies and "$30,000" in r.apprenticeship.reason
    # small project: keep payroll records at least 3 years (LC 1771.4(a)(4)); CSLB 5 years -> 1/15/2032
    assert any("at least 3 years" in x for x in r.record_retention.items)
    assert r.record_retention.keep_until == "2032-01-15"
    assert not any(t.task.startswith("Register with DIR") for t in r.checklist)


def test_maintenance_uses_the_15000_limit():
    # $20,000: maintenance > $15,000 -> full; construction <= $25,000 -> small. Both under $30,000: no apprentices.
    m = pw.public_works_readiness(20_000, "county", True, "maintenance", "2026-12-07", True, "2027-06-30", as_of=AS_OF)
    c = pw.public_works_readiness(20_000, "county", True, "construction", "2026-12-07", True, "2027-06-30", as_of=AS_OF)
    assert (m.coverage.tier, m.coverage.small_project_limit_usd, m.dir_registration.status) == ("full", 15_000, "ok")
    assert m.payroll.ecpr_upload_required and not m.apprenticeship.applies
    assert c.coverage.tier == "small_project"
    # No award date: PWC-100 due no later than the first day workers are on site
    assert m.pwc100_due == "2026-12-07"


@pytest.mark.parametrize("value,work,tier,apprentices", [
    (1_000, "repair", "1000_or_less", False),         # "$1,000 or less" is exempt
    (1_000.01, "repair", "small_project", False),
    (25_000, "construction", "small_project", False),  # $25,000 or less is a small project
    (25_000.01, "construction", "full", False),        # full, but under $30,000: no apprenticeship
    (15_000, "maintenance", "small_project", False),
    (15_000.01, "maintenance", "full", False),
    (29_999.99, "construction", "full", False),
    (30_000, "construction", "full", True),            # $30,000 or more: apprenticeship applies
])
def test_threshold_boundaries(value, work, tier, apprentices):
    r = pw.public_works_readiness(value, "city", True, work, "2027-02-01", True, "2027-06-30", as_of=AS_OF)
    assert r.coverage.tier == tier and r.coverage.apprenticeship_applies is apprentices
    assert r.coverage.prevailing_wage_applies is (tier != "1000_or_less")


def test_sole_owner_without_employees_needs_no_apprentices():
    r = pw.public_works_readiness(90_000, "city", True, "installation", "2027-02-01", True, "2027-06-30",
                                  as_of=AS_OF, has_employees=False)
    assert r.coverage.tier == "full" and not r.apprenticeship.applies and "No employees" in r.apprenticeship.reason


def test_private_development_de_minimis_subsidy():
    # $40M project, $500k subsidy = 1.25%: < $600,000 and < 2% -> de minimis, not a public work
    a = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=500_000)
    assert a.coverage.public_work is False and a.coverage.tier == "not_public_work"
    assert line(a, "Public subsidy").value == 1.25 and not a.retention.applies and not a.subcontractor_listing.applies
    # $700k = 1.75%: under 2% but not under $600,000 -> public work
    b = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=700_000)
    assert b.coverage.public_work is True and b.coverage.tier == "full" and b.dir_registration.status == "not_registered"
    # All single-family homes: only the 2% test. $700k -> 1.75% de minimis; $800k -> exactly 2.00%, not "less than"
    c = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=700_000, all_single_family=True)
    d = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=800_000, all_single_family=True)
    assert c.coverage.public_work is False and d.coverage.public_work is True
    # Coverage rests on LC 1720(b)/(c)(1) (unchecked) -> verified False; a $35M+ prime adds the AB 1198 flag
    assert {"pw-paid-with-public-funds-definition", "ab1198-mid-project-rate-changes-35m"} <= set(b.unverified_rule_ids)
    # Subsidy amount unknown -> cannot tell; planned as covered
    e = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF)
    assert e.coverage.public_work is None and e.coverage.prevailing_wage_applies is None


def test_federal_agency_without_state_money_is_not_california_public_work():
    r = pw.public_works_readiness(2_000_000, "federal_agency", False, "construction", "2027-03-01", False, as_of=AS_OF)
    assert r.coverage.public_work is False and not r.dir_registration.required and not r.checklist
    assert "Davis-Bacon" in r.coverage.reasons[0] and r.verified is False


def test_public_body_without_public_funds_is_uncertain_and_planned_as_covered():
    r = pw.public_works_readiness(200_000, "city", False, "construction", "2027-03-01", True, "2027-06-30", as_of=AS_OF)
    assert r.coverage.public_work is None and r.coverage.prevailing_wage_applies is None and r.coverage.tier == "full"
    t = task(r, "Get a written coverage determination")
    assert t.licensed_human.startswith("attorney") and "pw-coverage-determination" in r.unverified_rule_ids
    assert r.summary.startswith("Coverage uncertain")


# ---------------------------------------------------------------- DIR registration
def test_working_unregistered_after_start_shows_penalty_exposure():
    # Started Mon 11/2/2026, today 11/25: 24 calendar days on the job. Fee: FY ending 6/30/2027 only = $400.
    # Exposure: $2,000 penalty registration fee + up to $100 x 24 = $2,400 (cap $8,000) -> $2,000 to $4,400
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-11-02", False, as_of=AS_OF,
                                  estimated_completion_date="2027-03-31")
    reg = r.dir_registration
    assert (reg.status, reg.fee_usd, reg.penalty_low_usd, reg.penalty_high_usd) == ("not_registered", 400, 2000, 4400)
    t = task(r, "Register with DIR")
    assert (t.due, t.status) == ("2026-11-02", "overdue")
    # eCPR due 11/2 + 30 = 12/2; DAS 142 3 weekdays before Mon 11/2 = Wed 10/28; training fund 12/15
    assert r.payroll.first_upload_due == "2026-12-02"
    assert (r.apprenticeship.das142_request_by, r.apprenticeship.training_fund_first_due) == ("2026-10-28", "2026-12-15")
    overdue = sum(t.status == "overdue" for t in r.checklist)
    assert overdue >= 5 and f"{overdue} checklist item(s) overdue" in r.summary


def test_multi_year_registration_fee_and_three_year_cap():
    # Register 11/25/2026 for a job ending 9/30/2028: FYs ending 6/30/2027, 2028, 2029 -> 3 x $400 = $1,200
    r = pw.public_works_readiness(3_000_000, "county", True, "construction", "2027-04-05", False, as_of=AS_OF,
                                  bid_opening_date="2026-12-15", estimated_completion_date="2028-09-30")
    assert (r.dir_registration.fiscal_years_to_buy, r.dir_registration.fee_usd) == (3, 1200)
    t = task(r, "Register with DIR")
    assert (t.due, t.status) == ("2026-12-15", "upcoming")      # before bidding (20 days out)
    assert r.dir_registration.penalty_low_usd == 0               # nothing done unregistered yet
    # Ending 9/30/2029 needs 4 FYs; DIR sells at most 3 at once -> $1,200 now and a renewal note
    r = pw.public_works_readiness(3_000_000, "county", True, "construction", "2027-04-05", False, as_of=AS_OF,
                                  estimated_completion_date="2029-09-30")
    assert r.dir_registration.fee_usd == 1200 and any("at most 3" in n for n in r.dir_registration.notes)


def test_lapsed_registration_grace_period_and_after():
    # Expired 6/30/2026; on 8/15/2026 that is 46 days (<= 90): $400 (inadvertent) to $2,000 penalty + $400 fee,
    # renew by 6/30 + 90 days = 9/28/2026
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-09-08", True, "2026-06-30",
                                  as_of="2026-08-15", estimated_completion_date="2026-12-31")
    reg = r.dir_registration
    assert (reg.status, reg.penalty_low_usd, reg.penalty_high_usd, reg.fee_usd) == ("expired_grace_period", 400,
                                                                                   2000, 400)
    assert task(r, "Renew DIR registration now").due == "2026-09-28"
    # 10/15/2026 is 107 days after expiry: past the grace; worked while lapsed -> $2,000 penalty to re-register
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-09-08", True, "2026-06-30",
                                  as_of="2026-10-15", estimated_completion_date="2026-12-31")
    assert (r.dir_registration.status, r.dir_registration.penalty_low_usd) == ("expired", 2000)


def test_registration_expiring_before_start_and_bad_expiry_date():
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2027-08-02", True, "2027-06-30", as_of=AS_OF)
    assert r.dir_registration.status == "expires_before_start" and r.dir_registration.fee_due_by == "2027-06-30"
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2027-02-01", True, "2027-05-15", as_of=AS_OF)
    assert any("end June 30" in n for n in r.dir_registration.notes)


# ---------------------------------------------------------------- subcontractor listing
def test_streets_and_highways_listing_threshold_is_greater_of_half_percent_or_10000():
    subs = [{"name": "A Paving", "license_number": "1", "dir_number": "1000000001", "bid_amount_usd": 10_000,
             "portion_of_work": "paving"},
            {"name": "B Striping", "license_number": "2", "dir_number": "1000000002", "bid_amount_usd": 10_000.01,
             "portion_of_work": "striping"}]
    # $1.2M bid: 0.5% = $6,000 < $10,000 -> threshold $10,000; "in excess of", so exactly $10,000 is not listed
    r = pw.public_works_readiness(1_200_000, "county", True, "street_highway_bridge", "2027-03-01", True,
                                  "2027-06-30", subs, as_of=AS_OF)
    assert r.subcontractor_listing.threshold_usd == 10_000
    assert [s.must_list for s in r.subcontractor_listing.subs] == [False, True]
    # $3M bid: 0.5% = $15,000 > $10,000
    r = pw.public_works_readiness(3_000_000, "county", True, "street_highway_bridge", "2027-03-01", True,
                                  "2027-06-30", subs, as_of=AS_OF)
    assert r.subcontractor_listing.threshold_usd == 15_000
    # Same $1.2M as a building job: plain 0.5% = $6,000, both listed
    r = pw.public_works_readiness(1_200_000, "county", True, "construction", "2027-03-01", True, "2027-06-30", subs,
                                  as_of=AS_OF)
    assert r.subcontractor_listing.threshold_usd == 6_000 and all(s.must_list for s in r.subcontractor_listing.subs)


def test_listing_uses_prime_bid_and_flags_sub_problems_before_bid_opening():
    subs = [{"name": "No License Roofing", "dir_number": "1000000003", "bid_amount_usd": 40_000},
            {"name": "Lapsing HVAC", "license_number": "777", "dir_number": "1000000004", "bid_amount_usd": 90_000,
             "portion_of_work": "HVAC", "dir_registration_expiry": "2027-06-30"}]
    # Prime bid $1,000,000 -> 0.5% = $5,000; both listed. Job runs to 10/29/2027, past the HVAC sub's 6/30 expiry.
    r = pw.public_works_readiness(1_000_000, "city", True, "construction", "2027-02-01", True, "2028-06-30", subs,
                                  prime_bid_usd=1_000_000, as_of=AS_OF, bid_opening_date="2026-12-03",
                                  estimated_completion_date="2027-10-29")
    roof, hvac = r.subcontractor_listing.subs
    assert roof.must_list and any("CSLB license" in p for p in roof.problems) and any(
        "portion of work" in p for p in roof.problems)
    assert any("expires 2027-06-30" in p for p in hvac.problems)
    # 10% x $40,000 = $4,000 and 10% x $90,000 = $9,000 maximum Subletting Act penalties
    assert (roof.listing_violation_penalty_max_usd, hvac.listing_violation_penalty_max_usd) == (4000, 9000)
    lst = task(r, "List in the bid every sub over $5,000.00")
    assert (lst.due, lst.status) == ("2026-12-03", "due_soon") and "2 of 2" in lst.task
    assert task(r, "Correct any inadvertent").due == "2026-12-04"            # 24 hours after bid opening
    # Sub amounts over the prime bid are flagged
    r = pw.public_works_readiness(100_000, "city", True, "construction", "2027-02-01", True, "2027-06-30", subs,
                                  as_of=AS_OF)
    assert any("more than the prime bid" in n for n in r.subcontractor_listing.notes)


def test_subcontractor_role_measures_on_prime_contract_and_skips_listing():
    # A $45,000 electrical sub on a $2,000,000 prime contract: thresholds use the $2M prime contract
    r = pw.public_works_readiness(2_000_000, "city", True, "construction", "2027-02-01", True, "2027-06-30",
                                  as_of=AS_OF, your_role="subcontractor", contract_execution_date="2027-01-20",
                                  estimated_completion_date="2027-05-28")
    assert r.coverage.tier == "full" and r.apprenticeship.applies
    assert not r.subcontractor_listing.applies and "prime lists" in r.subcontractor_listing.notes[0]
    assert any("prime contract" in x for x in r.coverage.reasons)
    # DAS 140: min(signed 1/20 + 10 = 1/30, start 2/1) = 1/30/2027
    assert r.apprenticeship.das140_due == "2027-01-30"
    assert not any("1861" in t.task for t in r.checklist)                    # LC 1861 certificate is the prime's
    assert task(r, "DIR registration current").status == "done"


# ---------------------------------------------------------------- special cases
def test_legacy_lcp_and_state_agency_retention_flag():
    r = pw.public_works_readiness(1_200_000, "state_agency", True, "street_highway_bridge", "2027-03-01", True,
                                  "2027-06-30", as_of=AS_OF, awarding_body_name="Caltrans District 7")
    assert not r.payroll.ecpr_upload_required and "Caltrans" in r.payroll.ecpr_exempt_reason
    assert r.payroll.certified_payroll_required and r.pwc100_required
    assert {"ecpr-exemptions", "pcc-7107-public-retention-60-days"} <= set(r.unverified_rule_ids)
    assert any("90 days" in n for n in r.retention.notes)
    # A different agency is not a legacy LCP
    r = pw.public_works_readiness(1_200_000, "county", True, "street_highway_bridge", "2027-03-01", True,
                                  "2027-06-30", as_of=AS_OF, awarding_body_name="Los Angeles County Public Works")
    assert r.payroll.ecpr_upload_required


def test_sb909_flag_after_operative_date():
    before = school_remodel()
    after = school_remodel(as_of="2027-01-04", award_date=None, bid_opening_date=None)
    assert "sb909-pending-penalty-increases" not in before.unverified_rule_ids
    assert "sb909-pending-penalty-increases" in after.unverified_rule_ids
    assert any("pre-SB 909" in n for n in after.notes)


def test_input_validation():
    with pytest.raises(ValueError, match="before start_date"):
        pw.public_works_readiness(100_000, "city", True, "construction", "2027-02-01", True,
                                  estimated_completion_date="2027-01-01", as_of=AS_OF)
    with pytest.raises(ValueError, match="ISO date"):
        pw.public_works_readiness(100_000, "city", True, "construction", "next week", True, as_of=AS_OF)
    with pytest.raises(ValueError):
        pw.public_works_readiness(-5, "city", True, "construction", "2027-02-01", True, as_of=AS_OF)


# ---------------------------------------------------------------- apprentice hours
@pytest.mark.parametrize("jh,ot,expected,das142", [
    (1000, 0, 200, 40),       # 1,000/5 = 200; request >= max(8, 20% x 200 = 40) = 40
    (100, 0, 20, 8),          # DAS FAQ: 100 -> 20; 20% x 20 = 4 < 8 -> 8-hour day floor
    (750, 0, 150, 30),        # DLSE manual: 750 -> 150
    (1000, 60, 188, 37.6),    # (1,000 - 60 OT) = 940 / 5 = 188; 20% = 37.6
    (35, 0, 7, None),         # under 40 journeyman hours: no minimum request size
    (0, 0, 0, None),
])
def test_apprentice_hours(jh, ot, expected, das142):
    r = pw.apprentice_hours_required(jh, ot)
    assert r.required_apprentice_hours == expected and r.das142_min_request_hours == das142
    assert r.ratio == "1:5" and r.verified is True


def test_apprentice_shortfall_and_threshold():
    # 940 counted -> 188 required; 150 worked -> 38 short. $450,000 >= $30,000 -> applies
    r = pw.apprentice_hours_required(1000, 60, 150, "carpenter", 450_000)
    assert (r.counted_journeyman_hours, r.shortfall_hours, r.applies) == (940, 38, True)
    assert "940.00 journeyman h x 1/5 = 188.00" in line(r, "Apprentice hours required").math
    assert pw.apprentice_hours_required(500, apprentice_hours_worked=120).shortfall_hours == 0   # 100 needed
    assert pw.apprentice_hours_required(500, prime_contract_usd=25_000).applies is False
    with pytest.raises(ValueError):
        pw.apprentice_hours_required(10, 20)


# ---------------------------------------------------------------- penalties
def test_penalties_combined_exposure():
    r = pw.prevailing_wage_penalties(
        underpaid_worker_days=48, unpaid_wages_usd=3450, overtime_violation_worker_days=6, cpr_workers=9,
        cpr_request_received_date="2026-10-20", cpr_produced_date="2026-11-05", ecpr_days_late=40,
        unregistered_work_days=30, unregistered_sub_work_days=12, apprenticeship_noncompliance_days=50, as_of=AS_OF)
    by = {x.section: x for x in r.lines}
    assert (by["LC 1775(a)"].low_usd, by["LC 1775(a)"].high_usd) == (1920, 9600)      # 48 x $40 .. 48 x $200
    assert by["LC 1813"].high_usd == 150                                               # 6 x $25
    # Request 10/20 -> due 10/30; produced 11/5 = 6 days late; $100 x 9 workers x 6 = $5,400
    assert by["LC 1776(h)"].low_usd == 5400
    # eCPR 40 days late: $100 x (40 - 14) = $2,600 .. $100 x 40 = $4,000 (cap $5,000)
    assert (by["LC 1771.4(a)(3)"].low_usd, by["LC 1771.4(a)(3)"].high_usd) == (2600, 4000)
    assert by["LC 1771.1(g)"].low_usd == 3000                                          # $100 x 30
    assert by["LC 1725.5; 8 CCR 16412(a)(4)"].low_usd == 2000
    assert by["LC 1771.1(h)"].low_usd == 1200                                          # $100 x 12
    assert (by["LC 1777.7"].low_usd, by["LC 1777.7"].high_usd) == (0, 5000)            # up to $100 x 50
    # low = 1,920+150+5,400+2,600+3,000+2,000+1,200+0 = 16,270; high = 9,600+150+5,400+4,000+3,000+2,000+1,200+5,000
    assert (r.penalties_low_usd, r.penalties_high_usd) == (16_270, 30_350)
    assert (r.back_wages_usd, r.liquidated_damages_usd, r.total_low_usd, r.total_high_usd) == (3450, 0, 19_720, 33_800)
    assert r.verified is True and len(r.debarment_risk) == 2


def test_penalty_tiers_caps_and_liquidated_damages():
    # 10 worker-days: good faith corrected $0..$2,000; prior violation $800..; willful $1,200..$2,000
    assert pw.prevailing_wage_penalties(10, "good_faith_corrected", as_of=AS_OF).penalties_low_usd == 0
    assert pw.prevailing_wage_penalties(10, "prior_violation_3_years", as_of=AS_OF).penalties_low_usd == 800
    w = pw.prevailing_wage_penalties(10, "willful", as_of=AS_OF)
    assert (w.penalties_low_usd, w.penalties_high_usd) == (1200, 2000) and "willful" in w.debarment_risk[0]
    # Caps: eCPR 70 days -> min(7,000, 5,000) both ends; unregistered 95 days -> $8,000; unregistered sub 150 days
    # -> $10,000; repeat apprenticeship 50 days x $300 = $15,000
    r = pw.prevailing_wage_penalties(ecpr_days_late=70, unregistered_work_days=95, unregistered_sub_work_days=150,
                                     apprenticeship_noncompliance_days=50, apprenticeship_repeat_within_3_years=True,
                                     as_of=AS_OF)
    by = {x.section: x for x in r.lines}
    assert (by["LC 1771.4(a)(3)"].low_usd, by["LC 1771.4(a)(3)"].high_usd) == (5000, 5000)
    assert by["LC 1771.1(g)"].high_usd == 8000 and by["LC 1771.1(h)"].high_usd == 10_000
    assert by["LC 1777.7"].high_usd == 15_000
    # eCPR 10 days late is inside the 14-day grace: $0
    assert pw.prevailing_wage_penalties(ecpr_days_late=10, as_of=AS_OF).penalties_high_usd == 0
    # Wages still unpaid 60 days after the assessment: liquidated damages = back wages ($2,500)
    ld = pw.prevailing_wage_penalties(5, unpaid_wages_usd=2500, unpaid_60_days_after_assessment=True, as_of=AS_OF)
    # 5 x $40 = 200 .. 5 x $200 = 1,000; + 2,500 wages + 2,500 LD -> 5,200 .. 6,000
    assert (ld.liquidated_damages_usd, ld.total_low_usd, ld.total_high_usd) == (2500, 5200, 6000)


def test_cpr_penalty_runs_until_produced_and_sb909_flag():
    # Received 11/1, due 11/11, still missing on 11/25 -> 14 days x 3 workers x $100 = $4,200
    r = pw.prevailing_wage_penalties(cpr_workers=3, cpr_request_received_date="2026-11-01", as_of=AS_OF)
    assert r.lines[0].high_usd == 4200 and "not produced" in r.lines[0].math
    # Produced on the 10th day: no penalty
    assert pw.prevailing_wage_penalties(cpr_workers=3, cpr_request_received_date="2026-11-01",
                                        cpr_produced_date="2026-11-11", as_of=AS_OF).penalties_high_usd == 0
    later = pw.prevailing_wage_penalties(10, as_of="2027-02-01")
    assert later.verified is False and later.unverified_rule_ids == ["sb909-pending-penalty-increases"]
    none = pw.prevailing_wage_penalties(as_of=AS_OF)
    assert none.lines == [] and none.total_high_usd == 0 and any("No violations" in n for n in none.notes)
    with pytest.raises(ValueError):
        pw.prevailing_wage_penalties(-1, as_of=AS_OF)


# ---------------------------------------------------------------- rulebook wiring
def test_every_rule_id_exists_in_rulebook():
    ids = [v for k, v in vars(pw).items() if k.isupper() and isinstance(v, str) and v.count("-") >= 2]
    assert len(ids) > 40
    for rid in ids:
        core.rule(rid)


# ---------------------------------------------------------------- MCP
def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    pw.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            ready = await c.call_tool("public_works_readiness", {
                "project_value_usd": 480_000, "awarding_body_type": "school_district", "is_public_funds": True,
                "work_type": "alteration", "start_date": "2027-01-11", "contractor_dir_registered": True,
                "dir_registration_expiry": "2027-06-30", "as_of": AS_OF, "award_date": "2026-11-17",
                "subcontractors": [{"name": "Volt Electric", "license_number": "1012345", "dir_number": "1000045678",
                                    "bid_amount_usd": 62_000, "portion_of_work": "electrical"}]})
            hours = await c.call_tool("apprentice_hours_required", {"journeyman_hours": 1250})
            pen = await c.call_tool("prevailing_wage_penalties", {"underpaid_worker_days": 48, "as_of": AS_OF})
            return names, ready.structured_content, hours.structured_content, pen.structured_content
    names, ready, hours, pen = asyncio.run(run())
    assert {"public_works_readiness", "apprentice_hours_required", "prevailing_wage_penalties"} <= names
    assert ready["pwc100_due"] == "2026-12-17" and ready["subcontractor_listing"]["threshold_usd"] == 2400
    assert ready["retention"]["max_total_retention_usd"] == 24_000
    assert hours["required_apprentice_hours"] == 250
    assert (pen["penalties_low_usd"], pen["penalties_high_usd"]) == (1920, 9600)
