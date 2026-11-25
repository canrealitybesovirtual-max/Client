"""Public works module: hand-computed California prevailing wage / DIR / apprenticeship / listing scenarios."""
import asyncio
from datetime import date

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
    # DAS 142: 72 weekday hours before 12:00 a.m. Mon 1/11 = Fri 1/8 + Thu 1/7 + Wed 1/6 (24 h each), so the window
    # opens at 12:00 a.m. Wed 1/6 -> send by the end of Tue 1/5 (8 CCR 230.1(a): "before the date")
    # Training fund: 15th of the month after January -> 2/15/2027; hours statement 8/31 + 60 = 10/30/2027
    a = r.apprenticeship
    assert (a.das140_due, a.das142_request_by, a.training_fund_first_due, a.hours_statement_due) == (
        "2026-12-11", "2027-01-05", "2027-02-15", "2027-10-30")
    # Electricians 1,250 h / 5 = 250 apprentice h; DAS 142 >= max(8, 20% x 250 = 50) = 50 h
    # Laborers 30 h / 5 = 6 h; the under-40-hour exemption is per PROJECT (1,250 + 30 = 1,280 h), so the request must
    # still be at least max(8, 20% x 6 = 1.2) = 8 h
    crafts = {x.craft: x for x in a.crafts}
    assert (crafts["electrician"].required_apprentice_hours, crafts["electrician"].das142_min_request_hours) == (250, 50)
    assert (crafts["laborer"].required_apprentice_hours, crafts["laborer"].das142_min_request_hours) == (6, 8)
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
    # Unverified: the LC 1720 definition (unchecked), the no-4/10 overtime statement (unchecked), and SB 909 because
    # the job runs past 2027-01-01: the renewal fee (paid by 6/30/2027, LC 1725.5) and the LC 1776(h) records
    # penalty during the job may be SB 909 amounts. Every other line and task is verified.
    assert r.verified is False and r.unverified_rule_ids == ["pw-definition-public-works", "pw-no-alternative-workweek",
                                                             "sb909-pending-penalty-increases"]
    assert sorted(x.item for x in r.line_items if not x.verified) == [
        "DIR registration fee (renewal)", "Late certified payroll after a written request (LC 1776(h))"]
    assert [t.task[:30] for t in r.checklist if not t.verified] == ["Answer any written request for"]
    assert any("SB 909" in n and "2027-01-01" in n for n in r.notes)
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
    # All single-family homes: only the 2% test. $700k -> 1.75% de minimis; $800k -> exactly 2.00%, not "less than",
    # so not de minimis - but private residential work on private property is still excluded unless built under an
    # agreement with a state agency / redevelopment agency / housing authority (LC 1720(c)(1)) -> uncertain
    c = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=700_000, all_single_family=True)
    d = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=800_000, all_single_family=True)
    assert c.coverage.public_work is False and d.coverage.public_work is None and d.coverage.tier == "full"
    assert any("1720(c)(1)" in x for x in d.coverage.reasons) and d.summary.startswith("Coverage uncertain")
    # Private owner: Civil Code 8811 retention (5%), not PCC 7201
    assert any("Civil Code 8811" in n and "5%" in n for n in b.retention.notes)
    assert "private-retention-5pct-cap" in {x.rule_id for x in b.citations}
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
    # Working unregistered also brings a stop order and up to 10 days' pay for idled workers (LC 1771.1(j))
    assert any("stop order" in n and "10 days" in n for n in reg.notes)
    # eCPR due 11/2 + 30 = 12/2; DAS 142: window opens 3 weekdays before Mon 11/2 (Wed 10/28) -> send by Tue 10/27;
    # training fund 12/15
    assert r.payroll.first_upload_due == "2026-12-02"
    assert (r.apprenticeship.das142_request_by, r.apprenticeship.training_fund_first_due) == ("2026-10-27", "2026-12-15")
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
    # Expired 6/30/2026; on 8/15/2026 that is 46 days (<= 90). The job starts 9/8, so nothing was bid or worked while
    # lapsed: no penalty, just the $400 renewal fee, by 6/30 + 90 days = 9/28/2026
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-09-08", True, "2026-06-30",
                                  as_of="2026-08-15", estimated_completion_date="2026-12-31")
    reg = r.dir_registration
    assert (reg.status, reg.penalty_low_usd, reg.penalty_high_usd, reg.fee_usd) == ("expired_grace_period", 0, 0, 400)
    t = task(r, "Renew DIR registration now")
    assert t.due == "2026-09-28" and "do not bid or work" in t.task
    assert any("07-01 to 09-30" in n for n in reg.notes)
    # Same lapse, but the crew has been on the job since 7/6 (worked while lapsed): $400 (inadvertent) to $2,000
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-07-06", True, "2026-06-30",
                                  as_of="2026-08-15", estimated_completion_date="2026-12-31")
    reg = r.dir_registration
    assert (reg.status, reg.penalty_low_usd, reg.penalty_high_usd) == ("expired_grace_period", 400, 2000)
    assert "$400 penalty" in task(r, "Renew DIR registration now").task
    # 10/15/2026 is 107 days after expiry: past the grace; worked since 9/8 while lapsed -> $2,000 penalty
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-09-08", True, "2026-06-30",
                                  as_of="2026-10-15", estimated_completion_date="2026-12-31")
    assert (r.dir_registration.status, r.dir_registration.penalty_low_usd) == ("expired", 2000)
    assert line(r, "Penalty registration fee").value == 2000
    # A job that ended 6/15, before the 6/30 lapse: no work while lapsed -> no penalty to re-register
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-03-02", True, "2026-06-30",
                                  as_of="2026-10-15", estimated_completion_date="2026-06-15")
    assert (r.dir_registration.status, r.dir_registration.penalty_low_usd) == ("expired", 0)


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
    assert "sub-listing-required-information" in lst.rule_ids and not lst.verified     # PCC 4104(b) fields: unchecked
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


def test_sb909_flag_follows_job_dates():
    # A job that starts and ends in 2026 (before SB 909's assumed 2027-01-01 operative date): amounts are current law
    short = pw.public_works_readiness(150_000, "city", True, "construction", "2026-12-01", True, "2027-06-30",
                                      as_of=AS_OF, estimated_completion_date="2026-12-23")
    assert "sb909-pending-penalty-increases" not in short.unverified_rule_ids
    assert line(short, "Late certified payroll").verified and any("re-check" in n for n in short.notes)
    # Same start with no completion date: the job may run into 2027, so the LC 1776(h) amount is not verified
    open_ended = pw.public_works_readiness(150_000, "city", True, "construction", "2026-12-01", True, "2027-06-30",
                                           as_of=AS_OF)
    assert not line(open_ended, "Late certified payroll").verified
    assert any("estimated_completion_date" in n for n in open_ended.dir_registration.notes)
    # The school remodel runs in 2027: the LC 1776(h) amount is flagged even when asked in 2026
    before = school_remodel()
    after = school_remodel(as_of="2027-01-04", award_date=None, bid_opening_date=None)
    for r in (before, after):
        assert "sb909-pending-penalty-increases" in r.unverified_rule_ids
        assert not line(r, "Late certified payroll").verified and line(r, "Late eCPR penalty").verified
    assert any("pre-SB 909" in n for n in after.notes)


def test_input_validation():
    with pytest.raises(ValueError, match="start_date .* is after estimated_completion_date"):
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
    # records were produced (11/5), so no LC 1777.1(c) records debarment; the apprenticeship one stays
    assert r.verified is True and len(r.debarment_risk) == 1 and "apprenticeship" in r.debarment_risk[0]


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


# ---------------------------------------------------------------- review fixes
def test_das142_send_by_counts_back_from_the_start_of_the_day():
    # Thu 2/4/2027: 72 weekday hours before 12:00 a.m. Thu = Wed 2/3 + Tue 2/2 + Mon 2/1 -> window opens 12:00 a.m.
    # Mon 2/1, so the last day to send is the weekday before: Fri 1/29
    assert pw._das142_send_by(date(2027, 2, 4), 72) == (date(2027, 1, 29), date(2027, 2, 1), [])
    # Tue 3/2/2027: Mon 3/1 + Fri 2/26 + Thu 2/25 -> window opens Thu 2/25; send by Wed 2/24
    assert pw._das142_send_by(date(2027, 3, 2), 72) == (date(2027, 2, 24), date(2027, 2, 25), [])
    r = school_remodel()
    assert "send it by the end of 2027-01-05" in task(r, "Send DAS 142").due_rule


def test_das142_minimum_request_exemption_is_per_project():
    # 30 laborer hours and nothing else on the project: under 40 journeyman hours -> no minimum request size
    alone = pw.apprentice_hours_required(30, as_of=AS_OF)
    assert alone.das142_min_request_hours is None and any("PROJECTS under 40" in n for n in alone.notes)
    # Same 30 h on a 500-journeyman-hour project: 30 / 5 = 6 h required; minimum max(8, 20% x 6 = 1.2) = 8 h
    r = pw.apprentice_hours_required(30, project_journeyman_hours=500, as_of=AS_OF)
    assert (r.required_apprentice_hours, r.das142_min_request_hours) == (6, 8)


def test_shortfall_relies_on_registered_apprentice_rule():
    # 500 / 5 = 100 required - 60 registered-apprentice hours = 40 short; who counts as an apprentice is LC 1777.5(c)
    # (unchecked), so the shortfall is not verified; the plain ratio is
    r = pw.apprentice_hours_required(500, apprentice_hours_worked=60, as_of=AS_OF)
    assert r.shortfall_hours == 40 and r.verified is False
    assert r.unverified_rule_ids == ["apprentice-must-be-registered"] and not line(r, "Shortfall").verified
    assert pw.apprentice_hours_required(500, as_of=AS_OF).verified is True


def test_unregistered_listed_sub_cure_needs_the_penalty_fee():
    subs = [{"name": "Unreg Concrete", "license_number": "5", "bid_amount_usd": 80_000, "portion_of_work": "concrete"}]
    r = pw.public_works_readiness(1_000_000, "city", True, "construction", "2027-02-01", True, "2027-06-30", subs,
                                  as_of=AS_OF, bid_opening_date="2026-12-10")
    p = r.subcontractor_listing.subs[0].problems[0]
    # LC 1771.1(c)(2): register AND pay the $2,000 penalty registration fee within 24 hours, or substitute (PCC 4107)
    assert "24 hours" in p and "$2,000 penalty registration fee" in p and "PCC 4107" in p


def test_unregistered_exposure_stops_at_completion_and_award_exception():
    # Worked 9/1-10/15/2026 unregistered: 30 + 15 = 45 calendar days -> $2,000 + $100 x 45 = $6,500 (cap $8,000)
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2026-09-01", False, as_of=AS_OF,
                                  estimated_completion_date="2026-10-15")
    assert r.dir_registration.penalty_high_usd == 6500
    assert "2026-09-01 to 2026-10-15" in line(r, "Penalty exposure").math
    # Bid opens next month, not yet registered: JV and federal-funds bids may register by award (LC 1771.1(a))
    r = pw.public_works_readiness(150_000, "city", True, "construction", "2027-02-01", False, as_of=AS_OF,
                                  bid_opening_date="2026-12-15")
    assert any("time of contract award" in n for n in r.dir_registration.notes)
    assert "registration-jv-and-federal-bids-at-award" in {c.rule_id for c in r.citations}


def test_under_30000_training_amount_splitting_and_lc1861():
    # $28,000 construction: full tier (> $25,000) but < $30,000 -> no LC 1777.5; the training amount goes to the worker
    r = pw.public_works_readiness(28_000, "city", True, "construction", "2027-02-01", True, "2027-06-30", as_of=AS_OF)
    assert r.coverage.tier == "full" and not r.apprenticeship.applies
    assert "pay it to each worker" in r.apprenticeship.reason
    assert "training-fund-contributions" in {c.rule_id for c in r.citations}
    # $900 repair: no prevailing wage, but no splitting to stay under thresholds, and LC 1861 still applies
    small = pw.public_works_readiness(900, "city", True, "repair", "2027-02-01", False, as_of=AS_OF)
    assert "pw-no-project-splitting" in small.unverified_rule_ids and any("split" in x for x in small.coverage.reasons)
    assert task(small, "Sign and file the LC 1861").due == "2027-02-01"
    assert "4/10" in school_remodel().payroll.overtime_rule


def test_ab1198_only_for_contracts_awarded_on_or_after_its_date():
    # $40M prime awarded 11/17/2026, before AB 1198's reported 7/1/2027 start: not flagged; award unknown: flagged
    a = pw.public_works_readiness(40_000_000, "city", True, "construction", "2027-03-01", True, "2027-06-30",
                                  as_of=AS_OF, award_date="2026-11-17")
    b = pw.public_works_readiness(40_000_000, "city", True, "construction", "2027-09-01", True, "2027-06-30",
                                  as_of=AS_OF)
    assert "ab1198-mid-project-rate-changes-35m" not in a.unverified_rule_ids
    assert "ab1198-mid-project-rate-changes-35m" in b.unverified_rule_ids


def test_subcontractor_retention_and_listing_use_the_subcontract():
    # $45,000 sub on a $2,000,000 prime: cap 5% x 45,000 = $2,250; listing 0.5% x 2,000,000 = $10,000 < 45,000
    r = pw.public_works_readiness(2_000_000, "city", True, "construction", "2027-02-01", True, "2027-06-30",
                                  as_of=AS_OF, your_role="subcontractor", estimated_completion_date="2027-05-28",
                                  your_contract_usd=45_000)
    assert r.retention.max_total_retention_usd == 2250
    assert r.retention.release_due is None and any("whole project" in n for n in r.retention.notes)
    assert "the prime must list you" in line(r, "Must the prime list you").math
    assert task(r, "Post at the job site").who.startswith("awarding body or the prime")
    # Without the subcontract price no dollar cap is guessed from the prime contract
    r = pw.public_works_readiness(2_000_000, "city", True, "construction", "2027-02-01", True, "2027-06-30",
                                  as_of=AS_OF, your_role="subcontractor")
    assert r.retention.max_total_retention_usd is None and any("your_contract_usd" in n for n in r.retention.notes)


def test_de_minimis_numbers_flag_pre_july_2021_projects():
    r = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=500_000, bid_opening_date="2021-03-01")
    assert any("2021-07-01" in x for x in r.coverage.reasons)


def test_penalties_sb909_flag_is_per_section():
    # In 2027 only lines under sections SB 909 amends (LC 1775, 1776, 1777.7, 1725.5) are unverified
    r = pw.prevailing_wage_penalties(10, overtime_violation_worker_days=4, ecpr_days_late=20, unregistered_work_days=5,
                                     as_of="2027-02-01")
    by = {x.section: x for x in r.lines}
    assert not by["LC 1775(a)"].verified and not by["LC 1725.5; 8 CCR 16412(a)(4)"].verified
    assert by["LC 1813"].verified and by["LC 1771.4(a)(3)"].verified and by["LC 1771.1(g)"].verified
    assert by["LC 1813"].high_usd == 100                                               # 4 x $25
    # Request received 11/1/2026, due 11/11, produced 11/16: 5 days x 2 workers x $100 = $1,000, all before 2027
    r = pw.prevailing_wage_penalties(cpr_workers=2, cpr_request_received_date="2026-11-01",
                                     cpr_produced_date="2026-11-16", as_of="2027-02-01")
    assert r.lines[0].high_usd == 1000 and r.verified is True
    assert pw.prevailing_wage_penalties(overtime_violation_worker_days=4, as_of="2027-02-01").verified is True


# ---------------------------------------------------------------- code / realism review fixes
CITY = dict(project_value_usd=200_000, awarding_body_type="city", is_public_funds=True, work_type="construction",
            start_date="2027-02-01", contractor_dir_registered=True, dir_registration_expiry="2027-06-30", as_of=AS_OF)


@pytest.mark.parametrize("kw,msg", [
    (dict(project_value_usd=float("nan")), "finite"),
    (dict(project_value_usd=float("inf")), "finite"),
    (dict(prime_bid_usd=0), "prime_bid_usd must be more than 0"),
    (dict(work_type="painting"), "work_type must be one of"),
    (dict(your_role="gc"), "your_role must be one of"),
    (dict(awarding_body_type="town"), "awarding_body_type must be one of"),
    (dict(bid_opening_date="2027-03-01"), r"bid_opening_date \(2027-03-01\) is after start_date"),
    (dict(bid_opening_date="2026-12-20", award_date="2026-12-10"), "bid_opening_date .* is after award_date"),
    (dict(contract_execution_date="2027-09-01", estimated_completion_date="2027-05-01"),
     "contract_execution_date .* is after estimated_completion_date"),
    # craft hours are checked even when the apprentice ratio does not apply ($20,000 < $30,000)
    (dict(project_value_usd=20_000, journeyman_hours_by_craft={"laborer": -5}), "journeyman hours for 'laborer'"),
    (dict(your_role="subcontractor", your_contract_usd=500_000), "PRIME contract amount"),
    (dict(awarding_body_type="private_developer", public_subsidy_usd=500_000), "total project cost"),
    (dict(awarding_body_type="private_developer", project_value_usd=0, public_subsidy_usd=0), "more than 0"),
])
def test_readiness_rejects_impossible_input(kw, msg):
    with pytest.raises(ValueError, match=msg):
        pw.public_works_readiness(**{**CITY, **kw})


def test_das142_skips_holidays_and_marks_the_date_unverified():
    # Tue 9/8/2026: Mon 9/7 is Labor Day, so 72 working-day hours = Fri 9/4 + Thu 9/3 + Wed 9/2 -> window opens
    # 12:00 a.m. Wed 9/2 -> send by Tue 9/1
    assert pw._das142_send_by(date(2026, 9, 8), 72) == (date(2026, 9, 1), date(2026, 9, 2), ["2026-09-07 Labor Day"])
    # Tue 7/7/2026: July 4 is a Saturday, observed Fri 7/3 -> Mon 7/6 + Thu 7/2 + Wed 7/1 -> send by Tue 6/30
    assert pw._das142_send_by(date(2026, 7, 7), 72)[0] == date(2026, 6, 30)
    # Mon 11/30/2026: Thanksgiving Thu 11/26 and Fri 11/27 skipped -> Wed 11/25 + Tue 11/24 + Mon 11/23 -> Fri 11/20
    assert pw._das142_send_by(date(2026, 11, 30), 72)[0] == date(2026, 11, 20)
    # Job starting Mon 1/4/2027: Fri 1/1 New Year's Day skipped -> Thu 12/31 + Wed 12/30 + Tue 12/29 -> Mon 12/28
    r = school_remodel(start_date="2027-01-04")
    t = task(r, "Send DAS 142")
    assert (r.apprenticeship.das142_request_by, t.due) == ("2026-12-28", "2026-12-28")
    assert "skipping 2027-01-01 New Year's Day" in t.due_rule and not t.verified
    assert r.unverified_values == ["holidays"] and r.verified is False
    assert any("UNVERIFIED" in n and "holidays" in n for n in r.notes)
    # No holiday in the window (the 1/11 start): the DAS 142 date stays verified and nothing is listed
    r = school_remodel()
    assert task(r, "Send DAS 142").verified and r.unverified_values == []
    assert pw.UNVERIFIED["holidays"]["source"].startswith("https://")


def test_weekend_and_holiday_due_dates_get_a_finish_early_tip():
    r = school_remodel()
    # Training fund: 15th of the month after the January start = Mon 2/15/2027, Presidents' Day. The legal date stays.
    fund = task(r, "Pay apprenticeship training fund")
    assert fund.due == "2027-02-15" and "Presidents' Day (a holiday): finish it the working day before" in fund.due_rule
    # Hours statement: 8/31/2027 + 60 = Sat 10/30/2027
    assert "falls on a Saturday" in task(r, "Send the verified statement").due_rule
    # The awarding body's own retention date and the 5-year record date get no tip
    assert "falls on" not in task(r, "Awarding body releases").due_rule
    assert "falls on" not in task(r, "Keep all project").due_rule


def test_zero_hour_craft_needs_no_dispatch_request_and_unused_crafts_are_noted():
    # Electricians 0 h -> 0 apprentice h, no DAS 142; laborers 500 h / 5 = 100 h, request max(8, 20 = 20%) = 20 h
    r = pw.public_works_readiness(**{**CITY, "journeyman_hours_by_craft": {"electrician": 0, "laborer": 500}})
    crafts = {c.craft: c for c in r.apprenticeship.crafts}
    elec = crafts["electrician"]
    assert (elec.required_apprentice_hours, elec.das142_min_request_hours) == (0, None)
    assert (crafts["laborer"].required_apprentice_hours, crafts["laborer"].das142_min_request_hours) == (100, 20)
    # $28,000 job: ratio does not apply, so the craft hours given are reported as unused
    r = pw.public_works_readiness(**{**CITY, "project_value_usd": 28_000, "journeyman_hours_by_craft": {"laborer": 80}})
    assert any("journeyman_hours_by_craft was not used" in n for n in r.notes)


def test_unregistered_bid_before_work_starts_shows_no_daily_exposure():
    # Bid opened 11/10 unregistered, work starts 2/1/2027: $2,000 penalty fee, no work days yet -> $2,000 to $2,000
    r = pw.public_works_readiness(**{**CITY, "contractor_dir_registered": False, "dir_registration_expiry": None,
                                     "bid_opening_date": "2026-11-10"})
    x = line(r, "Penalty exposure")
    assert (r.dir_registration.penalty_low_usd, r.dir_registration.penalty_high_usd) == (2000, 2000)
    assert "no days worked yet (work starts 2027-02-01" in x.math and "2027-02-01 to 2026-11-25" not in x.math
    # An expiry date given with contractor_dir_registered=False is not silently dropped
    r = pw.public_works_readiness(**{**CITY, "contractor_dir_registered": False,
                                     "dir_registration_expiry": "2026-06-30"})
    assert any("2026-06-30 was ignored" in n for n in r.dir_registration.notes)


def test_bid_stage_tasks_move_to_the_next_milestone_once_the_bid_has_opened():
    subs = [{"name": "No Number Electric", "license_number": "9", "bid_amount_usd": 40_000,
             "portion_of_work": "electrical"}]
    # Public body, no public funds, bid opened 11/10: the coverage determination is due before work starts (2/1)
    r = pw.public_works_readiness(**{**CITY, "is_public_funds": False, "bid_opening_date": "2026-11-10",
                                     "subcontractors": subs})
    assert task(r, "Get a written coverage determination").due == "2027-02-01"
    # The 24-hour correction window closed 11/11, so no "correct the sub list" task; the sub's problem is still shown
    assert not any(t.task.startswith("Correct any inadvertent") for t in r.checklist)
    assert "No DIR registration number" in r.subcontractor_listing.subs[0].problems[0]


def test_retention_pass_through_wording_by_role():
    p = pw.public_works_readiness(**CITY)
    s = pw.public_works_readiness(**{**CITY, "your_role": "subcontractor", "your_contract_usd": 30_000})
    assert task(p, "Pass each sub's share").who == "you (prime)"
    t = task(s, "Collect your retention share")
    assert t.who.startswith("the prime (to you)") and "7 days" in t.due_rule
    # 5% x $30,000 subcontract = $1,500
    assert s.retention.max_total_retention_usd == 1500


def test_de_minimis_percent_just_under_the_cap_is_not_printed_as_the_cap():
    # $799,999 / $40,000,000 = 1.9999975% < 2% (all single-family) -> de minimis; 2 decimals would print "2.00%"
    r = pw.public_works_readiness(40_000_000, "private_developer", True, "construction", "2027-03-01", False,
                                  as_of=AS_OF, public_subsidy_usd=799_999, all_single_family=True)
    m = line(r, "Public subsidy").math
    assert r.coverage.public_work is False and "2.00%" not in m and "1.99999" in m


def test_apprentice_hours_input_checks_and_threshold_note():
    with pytest.raises(ValueError, match="total for ALL crafts"):
        pw.apprentice_hours_required(100, project_journeyman_hours=50, as_of=AS_OF)
    with pytest.raises(ValueError, match="finite"):
        pw.apprentice_hours_required(float("nan"), as_of=AS_OF)
    # $20,000 < $30,000: 100 / 5 = 20 h still shown, flagged as not applying
    r = pw.apprentice_hours_required(100, prime_contract_usd=20_000, as_of=AS_OF)
    assert r.applies is False and r.required_apprentice_hours == 20
    assert any("do not apply to this job" in n for n in r.notes)
    # 10 h all overtime -> 0 counted, 0 required: no "under 40 hours" exemption note
    r = pw.apprentice_hours_required(10, 10, as_of=AS_OF)
    assert r.required_apprentice_hours == 0 and r.das142_min_request_hours is None
    assert not any("PROJECTS under" in n for n in r.notes)


@pytest.mark.parametrize("kw,msg", [
    (dict(cpr_workers=3, cpr_request_received_date="2026-11-01", cpr_days_late=5), "not both"),
    (dict(cpr_workers=3, cpr_produced_date="2026-11-10"), "needs cpr_request_received_date"),
    (dict(cpr_workers=3, cpr_request_received_date="2026-11-10", cpr_produced_date="2026-11-01"),
     "cpr_request_received_date .* is after cpr_produced_date"),
    (dict(cpr_workers=3, cpr_request_received_date="2026-12-01"), "is after as_of"),
    (dict(underpaid_worker_days=2.5), "whole number"),
    (dict(unpaid_wages_usd=float("nan")), "finite"),
    (dict(ecpr_days_late=-1), "finite number 0 or more"),
])
def test_penalties_reject_impossible_input(kw, msg):
    with pytest.raises(ValueError, match=msg):
        pw.prevailing_wage_penalties(**kw, as_of=AS_OF)


def test_penalty_notes_for_half_given_facts_and_records_debarment():
    # cpr_workers alone: no request date or days late, so no LC 1776(h) line, and a note says why
    r = pw.prevailing_wage_penalties(cpr_workers=4, as_of=AS_OF)
    assert r.lines == [] and any("cpr_workers was given without" in n for n in r.notes)
    r = pw.prevailing_wage_penalties(unpaid_60_days_after_assessment=True, as_of=AS_OF)
    assert any("needs unpaid_wages_usd" in n for n in r.notes)
    # Still missing (received 11/1, due 11/11, 14 days late on 11/25): records debarment risk shown
    missing = pw.prevailing_wage_penalties(cpr_workers=3, cpr_request_received_date="2026-11-01", as_of=AS_OF)
    assert any("1777.1(c)" in d for d in missing.debarment_risk)
    # Produced 11/16 (5 days late): $100 x 3 x 5 = $1,500, no records debarment risk
    done = pw.prevailing_wage_penalties(cpr_workers=3, cpr_request_received_date="2026-11-01",
                                        cpr_produced_date="2026-11-16", as_of=AS_OF)
    assert done.penalties_high_usd == 1500 and done.debarment_risk == []


def test_tool_descriptions_come_from_the_rulebook():
    docs = pw._tool_docs()
    cap = core.rule("ecpr-late-penalty").param("max_per_project_usd")
    assert f"${cap:,.0f} per project" in docs["penalties"] and f"${cap:,.0f}" in docs["penalties"]
    fee = core.rule("dir-registration-fee").param("fee_per_fiscal_year_usd")
    assert f"${fee:,.0f} per fiscal year, 1-3 years" in docs["readiness"]
    assert "1:5" in docs["hours"] and "under 40 journeyman hours" in docs["hours"]


# ---------------------------------------------------------------- rulebook wiring
def test_every_rule_id_exists_in_rulebook():
    consts = {v for k, v in vars(pw).items() if k.isupper() and isinstance(v, str) and "-" in v and " " not in v}
    assert consts == set(pw.RULE_IDS) and len(pw.RULE_IDS) > 60
    for rid in pw.RULE_IDS:
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
            hours = await c.call_tool("apprentice_hours_required", {"journeyman_hours": 30,
                                                                    "project_journeyman_hours": 1280})
            pen = await c.call_tool("prevailing_wage_penalties", {"underpaid_worker_days": 48, "as_of": AS_OF})
            # hostile input comes back as a tool error, not a crash
            base = {"project_value_usd": 200_000, "awarding_body_type": "city", "is_public_funds": True,
                    "work_type": "construction", "start_date": "2027-02-01", "contractor_dir_registered": True}
            bad = [await c.call_tool(name, args, raise_on_error=False) for name, args in [
                ("public_works_readiness", {**base, "project_value_usd": "Infinity"}),
                ("public_works_readiness", {**base, "journeyman_hours_by_craft": {"laborer": -5}}),
                ("public_works_readiness", {**base, "start_date": "next Monday"}),
                ("prevailing_wage_penalties", {"underpaid_worker_days": 4.5}),
                ("prevailing_wage_penalties", {"cpr_workers": 2, "cpr_produced_date": "2026-11-10"}),
            ]]
            # strings that are numbers are accepted (lax JSON clients)
            lax = await c.call_tool("apprentice_hours_required", {"journeyman_hours": "1000", "as_of": AS_OF})
            tool = next(t for t in await c.list_tools() if t.name == "prevailing_wage_penalties")
            return (names, ready.structured_content, hours.structured_content, pen.structured_content, bad,
                    lax.structured_content, tool.description)
    names, ready, hours, pen, bad, lax, desc = asyncio.run(run())
    assert all(r.is_error for r in bad)
    assert "ISO date" in bad[2].content[0].text and "needs cpr_request_received_date" in bad[4].content[0].text
    assert lax["required_apprentice_hours"] == 200                                          # 1,000 / 5
    assert "$5,000 per project" in desc                                                     # from the rulebook
    assert {"public_works_readiness", "apprentice_hours_required", "prevailing_wage_penalties"} <= names
    assert ready["pwc100_due"] == "2026-12-17" and ready["subcontractor_listing"]["threshold_usd"] == 2400
    assert ready["retention"]["max_total_retention_usd"] == 24_000
    assert (hours["required_apprentice_hours"], hours["das142_min_request_hours"]) == (6, 8)   # 30/5; max(8, 1.2)
    assert (pen["penalties_low_usd"], pen["penalties_high_usd"]) == (1920, 9600)
