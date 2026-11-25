"""Business ops: compliance calendar and labor burden, with hand-computed California 2026 scenarios."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from tradecomply import business_ops as b, core

AS_OF = "2026-11-25"


def cal(**kw):
    base = dict(year=2026, entity_type="sole_owner", has_employees=True, employee_count=3, as_of=AS_OF)
    base.update(kw)
    return b.compliance_calendar(**base)


def find(result, prefix):
    hits = [i for i in result.items if i.title.startswith(prefix)]
    assert hits, f"no item starting {prefix!r}: {[i.title for i in result.items]}"
    return hits


def one(result, prefix):
    hits = find(result, prefix)
    assert len(hits) == 1, [h.title for h in hits]
    return hits[0]


def none(result, prefix):
    assert not [i for i in result.items if i.title.startswith(prefix)]


# ---------------------------------------------------------------- quarterly and year-end dates
def test_de9_and_941_dates_roll_past_weekends_and_match_rulebook_2026_schedule():
    r = cal(quarterly_payroll_usd=30000)
    de9 = {i.covers: i.due_date for i in find(r, "EDD DE 9 and DE 9C")}
    # Last day of the month after each quarter: Jan 31 2026 is a Saturday -> Mon Feb 2; Apr 30 Thu; Jul 31 Fri;
    # Oct 31 2026 Saturday -> Mon Nov 2; Jan 31 2027 Sunday -> Mon Feb 1.
    assert de9 == {"Wages paid Q4 2025": "2026-02-02", "Wages paid Q1 2026": "2026-04-30",
                   "Wages paid Q2 2026": "2026-07-31", "Wages paid Q3 2026": "2026-11-02",
                   "Wages paid Q4 2026": "2027-02-01"}
    sched = core.rule("edd-de9-de9c-quarterly-filing").param("2026_schedule")
    assert [s["delinquent_after"] for s in sched] == [de9[f"Wages paid Q{q} 2026"] for q in range(1, 5)]
    f941 = {i.title[-7:]: i.due_date for i in find(r, "IRS Form 941")}
    assert {f"{q} 2026": d for q, d in core.rule("form-941-quarterly-due-dates").param("2026_adjusted_dates").items()} \
        == {k: v for k, v in f941.items() if k.endswith("2026")}
    q3, q4 = one(r, "EDD DE 9 and DE 9C for Q3 2026"), one(r, "EDD DE 9 and DE 9C for Q4 2026")
    assert q3.status == "passed" and q4.status == "upcoming" and q4.days_until_due == 68   # Nov 25 -> Feb 1
    assert "2026-10-31 is a Saturday" in q3.math[1]
    assert q4.verified is False and "edd-de9-de9c-quarterly-filing" in q4.rule_ids   # DE 9 rule is unverifiable
    assert "$50 per DE 9" in q4.penalty_if_late and "$20 per wage item" in q4.penalty_if_late


def test_year_end_w2_940_1099_dates_and_thresholds():
    r = cal(pays_independent_contractors=True)
    w2 = {i.covers: i.due_date for i in find(r, "Forms W-2")}
    assert w2 == {"Wages paid in 2025": "2026-02-02", "Wages paid in 2026": "2027-02-01"}
    assert w2["Wages paid in 2026"] == core.rule("w2-furnish-and-file-jan-31").param("2026_wages_due")[:10]
    f940 = one(r, "IRS Form 940 (FUTA) for 2026")
    assert f940.due_date == core.rule("form-940-futa-annual-due").param("2026_return_due") == "2027-02-01"
    # 2026 FUTA: 0.6% net + 1.5% California credit reduction = 2.1%; 2025: 0.6% + 1.2% = 1.8%
    assert "= 2.1% on the first $7,000" in f940.math[1]
    assert "= 1.8% on the first $7,000" in one(r, "IRS Form 940 (FUTA) for 2025").math[1]
    nec25, nec26 = one(r, "Forms 1099-NEC for 2025"), one(r, "Forms 1099-NEC for 2026")
    assert nec25.due_date == "2026-02-02" and "$600 or more" in nec25.covers
    assert nec26.due_date == "2027-02-01" and "$2,000 or more" in nec26.covers
    assert nec26.verified is False                                   # 1099 threshold rule is unverifiable
    assert "24% backup withholding" in " ".join(nec26.notes)


# ---------------------------------------------------------------- deposit schedule
def test_monthly_depositor_gets_13_dated_deposits_with_holiday_roll_and_futa_carryover():
    r = cal(quarterly_payroll_usd=30000)
    d = r.deposit_schedule
    # 30,000 x 2 x (6.2% + 1.45%) = 30,000 x 15.3% = 4,590 a quarter; lookback 4 x 4,590 = 18,360 <= 50,000
    assert (d.schedule, d.estimated_quarterly_fica_usd, d.estimated_lookback_usd) == ("monthly", 4590.0, 18360.0)
    deps = {i.title.removeprefix("Payroll tax deposit for "): i for i in find(r, "Payroll tax deposit for")}
    assert len(deps) == 13                                           # Dec 2025 through Dec 2026
    assert deps["December 2025"].due_date == "2026-01-15"
    # Feb 15 2026 is a Sunday and Mon Feb 16 is Presidents' Day -> Tue Feb 17 (holiday table is UNVERIFIED)
    assert deps["January 2026"].due_date == "2026-02-17"
    assert "federal_legal_holidays" in deps["January 2026"].unverified_values
    assert deps["February 2026"].due_date == "2026-03-16"           # Mar 15 Sunday -> Mon
    assert deps["November 2026"].due_date == "2026-12-15" and deps["November 2026"].status == "due_soon"
    assert deps["December 2026"].due_date == "2027-01-15"
    assert "$4,590 / 3 = $1,530" in deps["March 2026"].math[1]
    # FUTA (IRS 940 method): 3 employees x $10,000/quarter. Q1 taxes the first $7,000 each at 0.6%:
    # 21,000 x 0.6% = 126, not over $500, carried. Q4 adds the 1.5% credit reduction on the year's 21,000 = 315,
    # undeposited 126 + 315 = 441, not over $500, so it may be paid with Form 940 by Feb 1, 2027.
    # Last year (same payroll): 126 + 21,000 x 1.2% = 252 -> 378, paid with the 2025 Form 940 by Feb 2, 2026.
    none(r, "FUTA deposit")
    bal = one(r, "FUTA for 2026")
    assert "$441" in bal.title and bal.due_date == "2027-02-01" and bal.status == "upcoming"
    assert "= $126" in bal.math[0] and "x 1.5% = $315" in bal.math[-2]
    prior = one(r, "FUTA for 2025")
    assert "$378" in prior.title and prior.due_date == "2026-02-02" and prior.status == "passed"


def test_semiweekly_depositor_when_lookback_over_50000():
    r = cal(employee_count=12, quarterly_payroll_usd=180000, quarterly_income_tax_withheld_usd=18000)
    d = r.deposit_schedule
    # 180,000 x 15.3% = 27,540 + 18,000 income tax = 45,540 a quarter; lookback 4 x 45,540 = 182,160 > 50,000
    assert (d.schedule, d.estimated_quarterly_federal_tax_usd, d.estimated_lookback_usd) == ("semiweekly", 45540.0, 182160.0)
    none(r, "Payroll tax deposit for")
    semi = one(r, "Semiweekly payroll tax deposits")
    assert semi.due_date is None and "following Wednesday" in semi.trigger
    # FUTA: Q1 12 x 7,000 = 84,000 x 0.6% = 504 > 500 -> deposit by Apr 30.
    # Q4: credit reduction 84,000 x 1.5% = 1,260 > 500 -> deposit by Jan 31 2027 (Sun) -> Feb 1.
    # Last year's Q4: 84,000 x 1.2% = 1,008 -> deposit by Jan 31 2026 (Sat) -> Feb 2.
    futa = one(r, "FUTA deposit for Q1 2026")
    assert futa.due_date == "2026-04-30" and "$504 is more than $500" in futa.math[-1]
    q4 = one(r, "FUTA deposit for Q4 2026")
    assert q4.due_date == "2027-02-01" and "$84,000 taxable FUTA wages x 1.5% = $1,260" in q4.math[-2]
    q4_prior = one(r, "FUTA deposit for Q4 2025")
    assert q4_prior.due_date == "2026-02-02" and "x 1.2% = $1,008" in q4_prior.math[-2]


def test_small_payroll_pays_with_return_and_new_employer_is_monthly():
    small = cal(employee_count=1, quarterly_payroll_usd=10000)
    # 10,000 x 15.3% = 1,530 < 2,500: may pay with the timely Form 941
    assert small.deposit_schedule.schedule == "pay_with_quarterly_return"
    assert small.deposit_schedule.estimated_quarterly_fica_usd == 1530.0
    none(small, "Payroll tax deposit for")
    new = cal(employee_count=8, quarterly_payroll_usd=120000, new_employer=True)
    # 120,000 x 15.3% = 18,360 a quarter, but a new employer has a $0 lookback -> monthly
    assert (new.deposit_schedule.schedule, new.deposit_schedule.estimated_lookback_usd) == ("monthly", 0.0)
    # FUTA: Q1 8 x 7,000 = 56,000 x 0.6% = 336, carried; Q4 adds 56,000 x 1.5% = 840 -> 1,176 > 500 -> one deposit
    # due Jan 31 2027 (Sun) -> Feb 1. New employer: last year's Q4 FUTA is not estimated.
    futa = find(new, "FUTA deposit")
    assert [i.due_date for i in futa] == ["2027-02-01"] and "$1,176 is more than $500" in futa[0].math[-1]


# ---------------------------------------------------------------- new employer mid-year
def test_started_paying_wages_mid_year():
    r = cal(entity_type="corporation", employee_count=25, quarterly_payroll_usd=60000,
            started_paying_wages_on="2026-05-04", new_employer=True)
    assert one(r, "Register as an employer with EDD").due_date == "2026-05-19"   # May 4 + 15 days
    assert one(r, "Get a federal EIN").due_date == "2026-05-11"                  # May 4 + 7 days
    assert [i.covers for i in find(r, "EDD DE 9")] == ["Wages paid Q2 2026", "Wages paid Q3 2026", "Wages paid Q4 2026"]
    none(r, "Forms W-2 / W-3 for 2025")
    none(r, "Post the certified Cal/OSHA Form 300A")      # no employees last year
    none(r, "Submit 2025 Form 300A")
    none(r, "Give every employee the Workplace Know Your Rights")   # hired after the Feb 1 notice date
    assert one(r, "Replace workplace posters").due_date == "2026-05-04"
    assert find(r, "Payroll tax deposit for")[0].title.endswith("May 2026")
    # FUTA: $60,000 / 25 = $2,400 per employee per quarter, starting Q2.
    #   Q2: 25 x 2,400 = 60,000 x 0.6% = 360, not over $500 -> carried
    #   Q3: 60,000 x 0.6% = 360 -> undeposited 720 > 500 -> due Oct 31 (Sat) -> Nov 2
    #   Q4: cumulative 7,200 hits the $7,000 cap, taxable 2,200 x 25 = 55,000 x 0.6% = 330, plus the credit reduction
    #       on the year's 60,000 + 60,000 + 55,000 = 175,000 x 1.5% = 2,625 -> 2,955 -> Jan 31 2027 (Sun) -> Feb 1
    #   total 720 + 2,955 = 3,675 = 175,000 x 2.1%
    futa = find(r, "FUTA deposit")
    assert [(f.due_date, f.math[-1].split(" is more")[0]) for f in futa] == [("2026-11-02", "$720"),
                                                                           ("2027-02-01", "$2,955")]
    assert any("minimum franchise tax" in n for n in r.notes)


# ---------------------------------------------------------------- LLC with crew
def test_llc_roofer_with_crew_full_calendar():
    r = cal(entity_type="llc", employee_count=12, license_expiration_date="2026-12-31", quarterly_payroll_usd=180000,
            quarterly_income_tax_withheld_usd=18000, pays_independent_contractors=True,
            cities_worked=["San Diego", "Chula Vista"], license_classifications=["B", "C-39"],
            wc_policy_renewal_date="2026-12-01", personnel_of_record_count=7, bond_renewal_date="2026-12-20")
    assert r.is_llc and r.verified is False
    lic = one(r, "CSLB license renewal")
    assert lic.due_date == "2026-12-31" and lic.days_until_due == 36 and lic.status == "upcoming"
    assert "$700" in lic.math[0] and "$700 + 50% delinquency fee = $1,050 (CSLB schedule: $1,050)" in lic.math[1]
    assert lic.verified is True
    wc = one(r, "Workers' comp policy renewal")
    assert wc.due_date == "2026-12-01" and wc.status == "due_soon"
    assert r.next_due.title == wc.title
    # LC 3722: greater of 2 x premium or $1,500 x 12 employees = at least $18,000; CSLB entity minimum $20,000
    assert "at least $18,000" in wc.math[1] and "$20,000 per violation" in wc.math[0]
    assert "web_business-tax-insurance:wc-mandatory-classifications" in wc.rule_ids     # C-39
    bond = one(r, "Bond renewal")
    assert bond.remind_on == "2026-11-22"                        # Dec 20 - 4 weeks
    assert any("$100,000" in m for m in bond.math)               # LLC worker bond
    ins = one(r, "LLC liability insurance")
    assert ins.status == "needs_date" and "= $1,200,000" in ins.math[0]   # 1,000,000 + 100,000 x (7 - 5)
    assert one(r, "LLC $800 annual tax").due_date == "2026-04-15"   # 15th day of 4th month
    kyr = one(r, "Give every employee the Workplace Know Your Rights")
    assert kyr.due_date == "2026-02-01" and "$500 x 12 employees = $6,000" in kyr.math[0]
    log = one(r, "Post the certified Cal/OSHA Form 300A summary for 2025")
    assert (log.due_date, log.window_end) == ("2026-02-01", "2026-04-30")
    assert "corporate officer" in log.who_files                  # certifier is a company executive
    none(r, "Submit 2025 Form 300A")                             # 12 < 20 employees
    cities = find(r, "San Diego business tax") + find(r, "Chula Vista business tax")
    assert all(c.status == "needs_date" and c.due_date is None for c in cities)
    assert all("not in the rulebook" in c.penalty_if_late for c in cities)
    one(r, "LLC Statement of Information")
    assert {"edd_late_penalties", "irs_information_return_penalties"} <= set(r.unverified_values_used)
    assert all(i.status == "needs_date" for i in r.items if i.due_date is None and i.trigger and "Enter" in i.trigger)
    dated = [i.due_date for i in r.items if i.due_date]
    assert dated == sorted(dated)


def test_no_employee_sole_owner_2027_and_dated_contractor_report():
    r = b.compliance_calendar(2027, "sole_owner", False, 0, "2027-03-31", False, None, True, ["Oakland"], AS_OF,
                              city_tax_renewal_dates={"oakland": "2027-03-01"},
                              independent_contractor_600_dates=["2027-02-10"])
    none(r, "EDD DE 9")
    none(r, "Replace workplace posters")
    assert one(r, "If you hire: register with EDD").status == "event_driven"
    nec = one(r, "Forms 1099-NEC for 2026")
    assert nec.due_date == "2027-02-01" and "$2,000" in nec.covers
    assert one(r, "DE 542 for subcontractor").due_date == "2027-03-02"      # Feb 10 + 20 days
    assert one(r, "Oakland business tax").due_date == "2027-03-01"
    lic = one(r, "CSLB license renewal")
    assert lic.due_date == "2027-03-31" and "$450 + 50% delinquency fee = $675" in lic.math[1]
    ex = one(r, "Workers' comp exemption on file")
    assert ex.due_date == "2028-01-01"                                       # exemption ends Jan 1, 2028
    assert any("2026 values" in n for n in r.notes) and r.verified is False


def test_roofer_without_employees_still_needs_workers_comp():
    r = cal(has_employees=False, employee_count=0, license_classifications=["C-39"])
    wc = one(r, "Workers' comp policy in force")
    assert "C39 must carry it even without employees" in wc.notes[0]
    none(r, "Workers' comp exemption")


def test_postings_depend_on_company_size():
    big = cal(employee_count=60)
    posters = one(big, "Replace workplace posters")
    sized = next(m for m in posters.math if m.startswith("Required at 60"))
    assert "Family care and medical leave (CFRA)" in sized and "Log 300" in sized and "Pregnancy" not in sized
    assert one(big, "Submit 2025 Form 300A").due_date == "2026-03-02"       # 20-249 construction employees
    mid = cal(employee_count=8)
    sized = next(m for m in one(mid, "Replace workplace posters").math if m.startswith("Required at 8"))
    assert "Pregnancy disability leave" in sized and "Log 300" not in sized
    none(mid, "Post the certified Cal/OSHA Form 300A")                     # 8 <= 10: Log 300 exempt
    one(mid, "Harassment prevention training")
    none(cal(employee_count=4), "Harassment prevention training")


def test_new_hire_dates_and_time_off_to_vote():
    r = cal(new_hire_dates=["2026-11-20", "2026-12-24"])
    assert one(r, "DE 34 new-hire report for employee who started 2026-11-20").due_date == "2026-12-10"  # +20 days
    assert one(r, "DE 34 new-hire report for employee who started 2026-12-24").due_date == "2027-01-13"
    assert one(r, "Form I-9 Section 2 for employee who started 2026-11-20").due_date == "2026-11-25"     # Mon-Wed
    # Dec 24 (Thu) + 3 business days: Dec 25 holiday, 26-27 weekend -> Dec 28, 29, 30
    late = one(r, "Form I-9 Section 2 for employee who started 2026-12-24")
    assert late.due_date == "2026-12-30" and late.unverified_values == ["federal_legal_holidays"]
    vote = [i.due_date for i in find(r, "Post the time-off-to-vote notice")]
    assert vote == ["2026-05-23", "2026-10-24"]                             # 10 days before Jun 2 and Nov 3


def test_bad_inputs():
    with pytest.raises(ValueError):
        cal(has_employees=False, employee_count=3)
    with pytest.raises(ValueError):
        cal(license_expiration_date="12/31/2026")
    with pytest.raises(ValueError):
        cal(employee_count=-1)
    r = cal(employee_count=0)
    assert r.employee_count == 1 and any("counted as 1" in n for n in r.notes)


# ---------------------------------------------------------------- labor burden
def lines(r):
    return {l.item.split(" (")[0]: l.annual_usd for l in r.employer_lines}


def test_labor_burden_new_employer_journeyman():
    r = b.labor_burden_estimate(35, 2000, 9.50, as_of=AS_OF)
    # wages 35 x 2,000 = 70,000
    #   Social Security 6.2% x 70,000 = 4,340      Medicare 1.45% x 70,000 = 1,015
    #   FUTA (0.6% + 1.5%) x 7,000 = 147           CA UI 3.4% x 7,000 = 238      ETT 0.1% x 7,000 = 7
    #   WC 70,000 / 100 x 9.50 = 6,650
    #   total burden 12,397; cost 82,397; 82,397 / 2,000 = 41.1985 -> 41.20; 12,397 / 70,000 = 17.71%
    assert lines(r) == {"Social Security": 4340.0, "Medicare": 1015.0, "FUTA": 147.0,
                        "CA Unemployment Insurance": 238.0, "CA Employment Training Tax": 7.0,
                        "Workers' compensation premium": 6650.0}
    assert (r.employer_taxes_and_insurance_usd, r.total_annual_cost_usd) == (12397.0, 82397.0)
    assert (r.burdened_hourly_rate_usd, r.burden_percent_of_base_wages) == (41.2, 17.71)
    emp = {l.item.split(" (")[0]: l.annual_usd for l in r.employee_withholding_lines}
    assert emp["CA SDI"] == 910.0                                  # 1.3% x 70,000, employee-paid
    assert r.verified is False                                      # 2026 FUTA credit reduction is unverifiable
    futa = next(l for l in r.employer_lines if l.item.startswith("FUTA"))
    assert futa.verified is False and all(l.verified for l in r.employer_lines if l is not futa)
    assert any("DE 2088" in n for n in r.notes)


def test_labor_burden_high_earner_caps_social_security_and_uses_own_ui_rate():
    r = b.labor_burden_estimate(100, 2080, 4.00, ui_rate=2.9, as_of=AS_OF, experience_mod=0.85)
    # wages 208,000; SS capped: 6.2% x 184,500 = 11,439; Medicare 1.45% x 208,000 = 3,016; FUTA 147;
    # UI 2.9% x 7,000 = 203; ETT 7; WC 208,000 / 100 x 4.00 x 0.85 = 7,072
    # burden 21,884; cost 229,884 / 2,080 = 110.5212 -> 110.52; 21,884 / 208,000 = 10.52%
    got = lines(r)
    assert got["Social Security"] == 11439.0 == core.rule("federal-fica-employer-2026").param(
        "employer_max_social_security_per_employee")
    assert (got["Medicare"], got["CA Unemployment Insurance"], got["Workers' compensation premium"]) == (3016.0, 203.0, 7072.0)
    assert (r.employer_taxes_and_insurance_usd, r.burdened_hourly_rate_usd, r.burden_percent_of_base_wages) == \
        (21884.0, 110.52, 10.52)


def test_labor_burden_with_sick_leave_and_wrong_carpentry_class():
    r = b.labor_burden_estimate(30, 2000, 8.00, as_of=AS_OF, include_paid_sick_leave=True, wc_class_code="5432")
    # sick: min(2,000 / 30 = 66.67, 40-hour use cap) = 40 h x 30 = 1,200; taxable wages 60,000 + 1,200 = 61,200
    # SS 6.2% x 61,200 = 3,794.40; Medicare 887.40; FUTA 147; UI 238; ETT 7; WC 612 x 8 = 4,896
    # taxes+WC 9,969.80; cost 61,200 + 9,969.80 = 71,169.80 / 2,000 = 35.58; (71,169.80 - 60,000) / 60,000 = 18.62%
    assert r.paid_sick_leave_usd == 1200.0 and r.taxable_wages_usd == 61200.0
    assert lines(r)["Social Security"] == 3794.4 and lines(r)["Workers' compensation premium"] == 4896.0
    assert (r.employer_taxes_and_insurance_usd, r.total_annual_cost_usd) == (9969.8, 71169.8)
    assert (r.burdened_hourly_rate_usd, r.burden_percent_of_base_wages) == (35.58, 18.62)
    assert any("belongs in 5403, not 5432" in n for n in r.notes)    # $30 < $46 threshold
    assert "wc-construction-dual-wage-thresholds" in {c.rule_id for c in r.citations}


def test_labor_burden_edge_cases():
    no_ett = b.labor_burden_estimate(35, 2000, 9.50, as_of=AS_OF, ett_applies=False)
    assert no_ett.employer_taxes_and_insurance_usd == 12390.0       # 12,397 - 7 ETT
    later = b.labor_burden_estimate(35, 2000, 9.50, as_of="2027-03-01")
    assert later.tax_year == 2027 and later.total_annual_cost_usd == 82397.0 and later.verified is False
    assert any("2027 rates (including the 2027 minimum wage) are not in the rulebook" in n for n in later.notes)
    low = b.labor_burden_estimate(15, 1000, 5, ui_rate=0.034, as_of=AS_OF)
    assert any("below the California minimum wage of $16.90 (in force from 2026-01-01)" in n for n in low.notes)
    # ui_rate 0.034 is read as 3.4% (no California UI rate is under 1%): 3.4% x 7,000 = 238
    assert lines(low)["CA Unemployment Insurance"] == 238.0
    assert any("read as a decimal, so 3.4% was used" in n for n in low.notes)
    assert "ca_ui_rate_range" in low.unverified_values_used and low.verified is False
    with pytest.raises(ValueError):
        b.labor_burden_estimate(0, 2000, 9.5)
    with pytest.raises(ValueError):
        b.labor_burden_estimate(35, 9000, 9.5)


# ---------------------------------------------------------------- review fixes
def test_llc_flag_on_sole_owner_entity_pays_entity_fee_and_entity_wc_penalty():
    r = cal(entity_type="sole_owner", llc=True, license_expiration_date="2026-12-31")
    lic = one(r, "CSLB license renewal")
    # an LLC is not a sole owner: $700 active renewal, $700 x 1.5 = $1,050 late (16 CCR 811 / B&P 7137(a)(5))
    assert "Active renewal fee (non-sole owner): $700" in lic.math[0] and "= $1,050" in lic.math[1]
    assert "Late renewal costs $1,050 instead of $700" in lic.penalty_if_late
    wc = one(r, "Workers' comp policy in force")
    assert "(entity): $20,000 per violation" in wc.math[0]          # B&P 7125.4(a)(2), not the $10,000 sole owner
    assert "can issue a stop order" in wc.penalty_if_late
    jv = one(cal(entity_type="joint_venture"), "Workers' comp policy in force")
    assert "not joint ventures" in jv.math[0]
    # B&P 7125 (operative 2028-01-01): a no-employee joint venture keeps its exemption, so no 2028 end date
    jv_ex = one(cal(year=2027, entity_type="joint_venture", has_employees=False, employee_count=0),
                "Workers' comp exemption on file")
    assert jv_ex.due_date is None and "can stay exempt" in jv_ex.notes[0]
    none(cal(year=2028, entity_type="joint_venture", has_employees=False, employee_count=0), "Workers' comp policy")


def test_i9_penalty_and_short_jobs_and_de542_threshold_doubt():
    r = cal(pays_independent_contractors=True, new_hire_dates=["2026-11-20"])
    packet = one(r, "New-hire paperwork")
    assert "$288 to $2,861 per form" in packet.penalty_if_late and "form-i9-civil-penalties" in packet.rule_ids
    assert "a job shorter than 3 business days: both sections on day one" in packet.trigger
    assert "$288 to $2,861" in one(r, "Form I-9 Section 2 for employee").penalty_if_late
    de542 = one(r, "Report subcontractors")
    assert any("$2,000 for 2026 payments" in n and "report at $600" in n for n in de542.notes)
    assert de542.verified is False
    de34 = one(r, "Report each new hire to EDD")
    assert any("12 and not more than 16 days apart" in n for n in de34.notes)


def test_deposit_schedule_notes_and_de9_ui_ett_and_edd_registration_basis():
    r = cal(quarterly_payroll_usd=30000, started_paying_wages_on="2026-01-05")
    assert not any("IRS mails" in n for n in r.deposit_schedule.notes)
    assert any("12 months ended preceding June 30" in n for n in r.deposit_schedule.notes)
    de9 = one(r, "EDD DE 9 and DE 9C for Q1 2026")
    assert any("UI and ETT" in n for n in de9.notes) and "edd_de88_deposit_schedule" in de9.unverified_values
    reg = one(r, "Register as an employer with EDD")
    assert "lawbook-edd-registration-100-per-quarter" in reg.rule_ids     # $100 is total quarterly wages
    small = cal(employee_count=1, quarterly_payroll_usd=10000)
    assert any("no $100,000 day" in n for n in small.deposit_schedule.notes)


def test_irs_deposit_rolls_past_dc_emancipation_day():
    r = cal(year=2028, quarterly_payroll_usd=30000, as_of="2028-01-02")
    # March 2028 deposit: Apr 15 2028 is a Saturday, Apr 16 (Emancipation Day) a Sunday observed Mon Apr 17 -> Tue Apr 18
    march = one(r, "Payroll tax deposit for March 2028")
    assert march.due_date == "2028-04-18" and "federal_legal_holidays" in march.unverified_values
    # 2027: Apr 15 is a Thursday, nothing moves
    assert one(cal(year=2027, quarterly_payroll_usd=30000), "Payroll tax deposit for March 2027").due_date == "2027-04-15"


def test_llc_liability_insurance_flags_literal_statute_reading():
    r = cal(entity_type="llc", personnel_of_record_count=7)
    ins = one(r, "LLC liability insurance")
    assert "= $1,200,000" in ins.math[0]                          # 1,000,000 + 100,000 x (7 - 5), CSLB reading
    assert any("literally says $100,000 for each person listed" in n for n in ins.notes)


def test_workplace_violence_plan_and_know_your_rights_new_hire_note():
    r = cal()
    wvpp = one(r, "Workplace Violence Prevention Plan")
    assert wvpp.status == "event_driven" and "every 12 months" in wvpp.trigger
    assert {"wvpp-training-initial-and-annual", "wvpp-plan-review-annual", "wvpp-exemptions"} <= set(wvpp.rule_ids)
    assert any("fewer than 10 employees" in n for n in wvpp.notes) and any("1 year" in n for n in wvpp.notes)
    none(cal(has_employees=False, employee_count=0), "Workplace Violence Prevention Plan")
    kyr = one(r, "Give every employee the Workplace Know Your Rights")
    assert any("new hires" in n for n in kyr.notes)


def test_public_works_registration_renewal():
    r = cal(public_works=True)
    pw = one(r, "DIR public works contractor registration for fiscal year 2026-27")
    assert pw.due_date == "2026-06-30" and pw.status == "passed"
    assert pw.math[0] == "Fee $400 per fiscal year; 1 yr $400, 2 yr $800, 3 yr $1,200"
    # late: 90-day grace (Jul 1 - Sep 30) with $400 penalty; unregistered work $100/day up to $8,000 + $2,000
    assert "within 90 days (2026-07-01 to 2026-09-30)" in pw.penalty_if_late and "$400 penalty" in pw.penalty_if_late
    assert "$100 per day up to $8,000" in pw.penalty_if_late and "$2,000 registration penalty" in pw.penalty_if_late
    assert pw.verified is False                                   # required-to-bid rule is unchecked
    none(cal(), "DIR public works contractor registration")


def test_carpentry_dual_wage_uses_threshold_in_force_at_policy_start():
    # $44/h: at or above the $41 threshold (policies from 2024-09-01) but below $46 (policies from 2026-09-01)
    unknown = b.labor_burden_estimate(44, 2000, 8, as_of=AS_OF, wc_class_code="5432")
    dual = [n for n in unknown.notes if n.startswith("Carpentry")]
    assert any("on or after 2026-09-01 the threshold is $46" in n and "belongs in 5403, not 5432" in n for n in dual)
    assert any("before 2026-09-01 the threshold is $41" in n and "belongs in 5432." in n for n in dual)
    assert any("give wc_policy_start_date" in n for n in unknown.notes)
    old_policy = b.labor_burden_estimate(44, 2000, 8, as_of=AS_OF, wc_class_code="5432",
                                         wc_policy_start_date="2026-03-01")
    assert [n for n in old_policy.notes if n.startswith("Carpentry")] == [
        "Carpentry dual-wage classes (regular hourly wage test): for a policy starting 2026-03-01 the threshold is "
        "$41, and $44/h is at or above it, so payroll belongs in 5432."]
    summer = b.labor_burden_estimate(44, 2000, 8, as_of="2026-06-01", wc_class_code="5403")
    assert any("in force on 2026-06-01 the threshold is $41" in n and "belongs in 5432, not 5403" in n
               for n in summer.notes)
    ancient = b.labor_burden_estimate(44, 2000, 8, as_of=AS_OF, wc_class_code="5403", wc_policy_start_date="2024-01-01")
    assert any("predates the thresholds" in n for n in ancient.notes)
    odd = b.labor_burden_estimate(35, 2000, 9.5, ui_rate=7.5, as_of=AS_OF)
    assert any("outside California's 1.5%-6.2%" in n for n in odd.notes) and odd.verified is False


# ---------------------------------------------------------------- code/realism review regressions
def test_contradictory_inputs_give_clear_errors():
    with pytest.raises(ValueError, match="after 2026, so you had no employees in 2026"):
        cal(started_paying_wages_on="2027-03-01")
    with pytest.raises(ValueError, match="new_hire_dates were given but has_employees is false"):
        cal(has_employees=False, employee_count=0, new_hire_dates=["2026-11-20"])
    with pytest.raises(ValueError, match="cannot be negative"):
        cal(quarterly_payroll_usd=-1)
    with pytest.raises(ValueError, match="4-digit year"):
        cal(year="twenty")
    assert cal(year="2026").year == 2026


def test_wages_started_this_year_make_a_new_employer_monthly_depositor():
    r = cal(employee_count=30, quarterly_payroll_usd=400000, started_paying_wages_on="2026-02-01")
    d = r.deposit_schedule
    # 400,000 x 15.3% = 61,200 a quarter, but no payroll in Jul 2024 - Jun 2025, so the lookback is $0 -> monthly
    # (it was wrongly 4 x 61,200 = 244,800 -> semiweekly before)
    assert (d.schedule, d.estimated_quarterly_federal_tax_usd, d.estimated_lookback_usd) == ("monthly", 61200.0, 0.0)
    deps = find(r, "Payroll tax deposit for")
    assert len(deps) == 11 and deps[0].title.endswith("February 2026")      # Feb through Dec 2026
    assert deps[0].due_date == "2026-03-16"                                 # Mar 15 2026 is a Sunday
    assert one(r, "Register as an employer with EDD").due_date == "2026-02-16"   # Feb 1 + 15 days
    assert one(r, "Get a federal EIN").due_date == "2026-02-08"                  # Feb 1 + 7 days


def test_wages_started_inside_lookback_count_only_those_quarters():
    # 10 employees, $100,000 a quarter: 100,000 x 15.3% = 15,300 a quarter.
    # Started 2025-03-01: the 2026 lookback (Jul 2024 - Jun 2025) has payroll in Q1 and Q2 2025 only:
    # 2 x 15,300 = 30,600 <= 50,000 -> monthly.  Started 2024-01-01: 4 x 15,300 = 61,200 > 50,000 -> semiweekly.
    partial = cal(employee_count=10, quarterly_payroll_usd=100000, started_paying_wages_on="2025-03-01")
    assert (partial.deposit_schedule.schedule, partial.deposit_schedule.estimated_lookback_usd) == ("monthly", 30600.0)
    assert "payroll in 2 of its 4 quarters" in partial.deposit_schedule.math[-2]
    full = cal(employee_count=10, quarterly_payroll_usd=100000, started_paying_wages_on="2024-01-01")
    assert (full.deposit_schedule.schedule, full.deposit_schedule.estimated_lookback_usd) == ("semiweekly", 61200.0)
    none(full, "Register as an employer with EDD")          # due 2024-01-16: not a 2026 item any more
    none(full, "Get a federal EIN")
    # Last year's FUTA from 2025-03-01: Q1 10 x 7,000 = 70,000 x 0.6% = 420 (carried, not over $500); Q2-Q4 nothing
    # left under the $7,000 base; Q4 adds 70,000 x 1.2% (2025 credit reduction) = 840 -> 1,260 due Jan 31 2026 (Sat)
    # -> Mon Feb 2. (It used to be dropped whenever started_paying_wages_on was given.)
    prior = one(partial, "FUTA deposit for Q4 2025")
    assert prior.due_date == "2026-02-02" and "$1,260 is more than $500" in prior.math[-1]
    assert "from 2025-03-01" in prior.notes[-1]
    # new_employer=True with wages from 2025-08-01: same $1,260 (Q3 420 + Q4 840) is still owed for 2025
    aug = cal(employee_count=10, quarterly_payroll_usd=100000, started_paying_wages_on="2025-08-01", new_employer=True)
    assert "$1,260 is more than $500" in one(aug, "FUTA deposit for Q4 2025").math[-1]


def test_registration_items_only_when_due_in_the_calendar_year():
    early = cal(quarterly_payroll_usd=30000, started_paying_wages_on="2025-12-15")
    none(early, "Register as an employer with EDD")          # Dec 15 + 15 = Dec 30, 2025
    none(early, "Get a federal EIN")                         # Dec 15 + 7 = Dec 22, 2025
    late = cal(quarterly_payroll_usd=30000, started_paying_wages_on="2025-12-26")
    assert one(late, "Register as an employer with EDD").due_date == "2026-01-10"   # Dec 26 + 15
    assert one(late, "Get a federal EIN").due_date == "2026-01-02"                  # Dec 26 + 7


def test_1099_for_last_year_kept_when_wages_start_this_year():
    r = cal(started_paying_wages_on="2026-05-04", pays_independent_contractors=True)
    assert one(r, "Forms 1099-NEC for 2025").due_date == "2026-02-02"   # subs paid in 2025 still get 1099s
    none(r, "Forms W-2 / W-3 for 2025")                                   # but no 2025 employees


def test_expired_license_is_flagged_with_the_retroactive_window():
    recent = cal(license_expiration_date="2026-10-31")
    item = one(recent, "CSLB license EXPIRED 2026-10-31")
    # Oct 31 + 90 days = Jan 29, 2027 (30 to Nov 30, 31 to Dec 31, 29 to Jan 29)
    assert item.due_date == "2027-01-29" and "renew by 2027-01-29" in item.notes[0]
    assert "retroactive with no gap" in item.notes[0] and "paying $675" in item.notes[0]
    none(recent, "CSLB license renewal")
    old = cal(license_expiration_date="2025-06-30")
    item = one(old, "CSLB license EXPIRED 2025-06-30")
    assert item.due_date == "2025-09-28" and item.status == "passed"     # Jun 30 + 90 days
    assert "more than 90 days have passed" in item.notes[0] and "Stop contracting" in item.notes[0]
    assert any("has passed" in n for n in old.notes)
    assert not any("no renewal is due" in n for n in old.notes)          # used to say nothing was due


def test_renewal_dates_outside_the_year_stay_on_the_calendar():
    nxt = one(cal(wc_policy_renewal_date="2027-03-01"), "Workers' comp policy renewal")
    assert nxt.due_date == "2027-03-01" and any("not in 2026" in n for n in nxt.notes)   # used to vanish
    past = one(cal(wc_policy_renewal_date="2025-03-01"), "Workers' comp policy renewal")
    assert past.status == "passed" and any("This date has passed" in n for n in past.notes)
    bond = one(cal(bond_renewal_date="2027-01-10"), "Bond renewal")
    assert bond.due_date == "2027-01-10" and bond.remind_on == "2026-12-13"            # Jan 10 - 4 weeks
    r = cal(cities_worked=["San Diego", "san diego ", "", "Oakland"],
            city_tax_renewal_dates={"Oakland": "2027-03-01", "San Jose": "2026-12-31"})
    assert {i.title for i in find(r, "")} >= {"San Diego business tax certificate renewal",
                                              "Oakland business tax certificate renewal",
                                              "San Jose business tax certificate renewal"}
    one(r, "San Diego business tax")                                      # one item, not one per spelling
    assert one(r, "Oakland business tax").due_date == "2027-03-01"
    assert one(r, "San Jose business tax").due_date == "2026-12-31"      # named only in the dates dict


def test_license_class_names_are_read_and_c10_surcharge_totalled():
    r = cal(has_employees=False, employee_count=0, license_expiration_date="2026-12-31",
            license_classifications=["C-10 Electrical", "D-49 Tree Service"])
    lic = one(r, "CSLB license renewal")
    assert lic.math[0] == "Active renewal fee (sole owner): $450 + C-10 surcharge $20 = $470"
    wc = one(r, "Workers' comp policy in force")                        # D-49 must carry WC without employees
    assert "class D49 must carry it even without employees" in wc.notes[0]
    assert b._norm_class("Class B") == "B" and b._norm_class("C-61/D-49") == "D49" and b._norm_class("c39") == "C39"


def test_blank_and_out_of_year_dates_and_contractor_dates():
    r = cal(new_hire_dates=["", " ", "2026-11-20", "2024-05-01"], independent_contractor_600_dates=["2026-03-01"])
    one(r, "DE 34 new-hire report")                                      # blanks skipped, 2024 left out
    assert any("left out: 2024-05-01" in n for n in r.notes)
    assert one(r, "DE 542 for subcontractor").due_date == "2026-03-21"   # Mar 1 + 20 days
    assert any("pays_independent_contractors was treated as true" in n for n in r.notes)
    one(r, "Forms 1099-NEC for 2026")


def test_vote_notice_without_known_election_dates_and_941_pay_with_return():
    vote = one(cal(year=2027), "Post the time-off-to-vote notice")
    assert vote.status == "event_driven" and "At least 10 days before every statewide election" in vote.trigger
    small = cal(employee_count=1, quarterly_payroll_usd=10000)            # 1,530 a quarter < 2,500
    f941 = one(small, "IRS Form 941 for Q1 2026")
    assert "may pay it with this timely filed return" in f941.notes[0]
    assert "federal-deposit-de-minimis-2500" in f941.rule_ids
    assert "may pay" not in one(cal(quarterly_payroll_usd=30000), "IRS Form 941 for Q1 2026").notes[0]


def test_labor_burden_ui_decimal_only_when_it_is_a_real_rate():
    half = b.labor_burden_estimate(35, 2000, 9.5, ui_rate=0.5, as_of=AS_OF)
    # 0.5 is not a decimal for 50%: used as 0.5% x 7,000 = 35 and flagged (it used to become 50% = $3,500)
    assert lines(half)["CA Unemployment Insurance"] == 35.0
    assert any("0.5% UI rate is outside" in n for n in half.notes) and "ca_ui_rate_range" in half.unverified_values_used
    dec = b.labor_burden_estimate(35, 2000, 9.5, ui_rate=0.029, as_of=AS_OF)
    assert lines(dec)["CA Unemployment Insurance"] == 203.0              # 2.9% x 7,000
    ok = b.labor_burden_estimate(35, 2000, 9.5, ui_rate=2.9, as_of=AS_OF)
    assert ok.unverified_values_used == {} and lines(ok)["CA Unemployment Insurance"] == 203.0


def test_labor_burden_mod_typo_additional_medicare_and_exact_rates():
    with pytest.raises(ValueError, match="looks like a percent"):
        b.labor_burden_estimate(35, 2000, 9.5, experience_mod=92, as_of=AS_OF)
    r = b.labor_burden_estimate(110, 2080, 4, as_of=AS_OF)
    # wages 110 x 2,080 = 228,800. Employer: SS 11,439 (capped) + Medicare 1.45% x 228,800 = 3,317.60 + FUTA 147
    # + UI 238 + ETT 7 + WC 2,288 x 4 = 9,152 -> 24,300.60; total 253,100.60
    assert (r.employer_taxes_and_insurance_usd, r.total_annual_cost_usd) == (24300.6, 253100.6)
    # employee-only Additional Medicare Tax: 0.9% x (228,800 - 200,000) = 0.9% x 28,800 = 259.20
    extra = next(l for l in r.employee_withholding_lines if l.item.startswith("Additional Medicare"))
    assert extra.annual_usd == 259.2 and extra.verified is False
    assert "additional_medicare_tax" in r.unverified_values_used and r.verified is False
    assert not any(l.item.startswith("Additional Medicare") for l in
                   b.labor_burden_estimate(35, 2000, 9.5, as_of=AS_OF).employee_withholding_lines)
    odd = b.labor_burden_estimate(33.333, 2000, 7.777, as_of=AS_OF)
    wc = next(l for l in odd.employer_lines if l.item.startswith("Workers"))
    # 66,666 / 100 x 7.777 = 5,184.6148 -> 5,184.61; the rate is shown exactly, not rounded to $7.78
    assert wc.annual_usd == 5184.61 and "x $7.777 per $100" in wc.math


def test_minimum_wage_check_uses_rate_in_force_on_as_of():
    mid_2025 = b.labor_burden_estimate(16.75, 2000, 5, as_of="2025-06-01")
    assert not any("below the California minimum wage" in n for n in mid_2025.notes)   # 2025 rate was $16.50
    assert any(n == "Rates are the verified 2026 values; 2025 rates are not in the rulebook." for n in mid_2025.notes)
    y2026 = b.labor_burden_estimate(16.75, 2000, 5, as_of="2026-03-01")
    assert any("below the California minimum wage of $16.90" in n for n in y2026.notes)
    alone = b.labor_burden_estimate(35, 2000, 9.5, as_of=AS_OF, wc_policy_start_date="2026-10-01")
    assert any("used only with wc_class_code 5403 or 5432" in n for n in alone.notes)


def test_mcp_rejects_bad_arguments_with_clear_errors():
    from fastmcp.exceptions import ToolError
    mcp = FastMCP("t")
    b.register(mcp)

    async def call(name, args):
        async with Client(mcp) as c:
            return await c.call_tool(name, args)

    for name, args, msg in [
        ("compliance_calendar", {"year": 1999}, "greater than or equal to 2000"),
        ("compliance_calendar", {"year": 2026, "quarterly_payroll_usd": "abc"}, "valid number"),
        ("compliance_calendar", {"year": 2026, "license_expiration_date": "12/31/2026"}, "ISO date"),
        ("compliance_calendar", {"year": 2026, "started_paying_wages_on": "2027-01-05"}, "no employees in 2026"),
        ("labor_burden_estimate", {"hourly_wage": 35, "annual_hours": 2000, "wc_rate_per_100": 9.5,
                                   "experience_mod": 92}, "less than or equal to 10"),
        ("labor_burden_estimate", {"hourly_wage": 35, "annual_hours": 99999, "wc_rate_per_100": 9.5},
         "less than or equal to 8784"),
    ]:
        with pytest.raises(ToolError, match=msg):
            asyncio.run(call(name, args))
    ok = asyncio.run(call("labor_burden_estimate", {"hourly_wage": "35", "annual_hours": "2000",
                                                    "wc_rate_per_100": "9.5", "as_of": AS_OF}))
    assert ok.structured_content["total_annual_cost_usd"] == 82397.0        # numeric strings are coerced


# ---------------------------------------------------------------- MCP
def test_tools_through_mcp_client():
    mcp = FastMCP("t")
    b.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            calendar_ = await c.call_tool("compliance_calendar", {
                "year": 2026, "entity_type": "llc", "has_employees": True, "employee_count": 12,
                "license_expiration_date": "2026-12-31", "quarterly_payroll_usd": 30000,
                "cities_worked": ["San Diego"], "as_of": AS_OF, "public_works": True})
            burden = await c.call_tool("labor_burden_estimate", {"hourly_wage": 35, "annual_hours": 2000,
                                                                 "wc_rate_per_100": 9.5, "as_of": AS_OF,
                                                                 "wc_class_code": "5403",
                                                                 "wc_policy_start_date": "2026-10-01"})
            return names, calendar_.structured_content, burden.structured_content
    names, cal_out, burden = asyncio.run(run())
    assert {"compliance_calendar", "labor_burden_estimate"} <= names
    assert cal_out["deposit_schedule"]["schedule"] == "monthly" and cal_out["is_llc"] is True
    assert any(i["title"] == "EDD DE 9 and DE 9C for Q4 2026" and i["due_date"] == "2027-02-01" for i in cal_out["items"])
    assert any(i["title"].startswith("DIR public works contractor registration") for i in cal_out["items"])
    assert burden["total_annual_cost_usd"] == 82397.0 and burden["burdened_hourly_rate_usd"] == 41.2
    assert any("threshold is $46" in n and "belongs in 5403." in n for n in burden["notes"])   # $35 < $46
