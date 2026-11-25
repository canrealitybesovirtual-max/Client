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
    # FUTA: 3 employees x $10,000/quarter: Q1 taxes the first $7,000 each = 21,000 x 2.1% = 441, not over $500,
    # nothing left to tax after Q1, so the $441 is paid with Form 940 and no FUTA deposit is due.
    none(r, "FUTA deposit")
    bal = one(r, "FUTA for 2026")
    assert "$441" in bal.title and bal.status == "event_driven"


def test_semiweekly_depositor_when_lookback_over_50000():
    r = cal(employee_count=12, quarterly_payroll_usd=180000, quarterly_income_tax_withheld_usd=18000)
    d = r.deposit_schedule
    # 180,000 x 15.3% = 27,540 + 18,000 income tax = 45,540 a quarter; lookback 4 x 45,540 = 182,160 > 50,000
    assert (d.schedule, d.estimated_quarterly_federal_tax_usd, d.estimated_lookback_usd) == ("semiweekly", 45540.0, 182160.0)
    none(r, "Payroll tax deposit for")
    semi = one(r, "Semiweekly payroll tax deposits")
    assert semi.due_date is None and "following Wednesday" in semi.trigger
    # FUTA: 12 x 7,000 = 84,000 x 2.1% = 1,764 in Q1 -> deposit by Apr 30
    futa = one(r, "FUTA deposit for Q1 2026")
    assert futa.due_date == "2026-04-30" and "$1,764" in futa.math[-1]


def test_small_payroll_pays_with_return_and_new_employer_is_monthly():
    small = cal(employee_count=1, quarterly_payroll_usd=10000)
    # 10,000 x 15.3% = 1,530 < 2,500: may pay with the timely Form 941
    assert small.deposit_schedule.schedule == "pay_with_quarterly_return"
    assert small.deposit_schedule.estimated_quarterly_fica_usd == 1530.0
    none(small, "Payroll tax deposit for")
    new = cal(employee_count=8, quarterly_payroll_usd=120000, new_employer=True)
    # 120,000 x 15.3% = 18,360 a quarter, but a new employer has a $0 lookback -> monthly
    assert (new.deposit_schedule.schedule, new.deposit_schedule.estimated_lookback_usd) == ("monthly", 0.0)
    # FUTA: 8 x 7,000 = 56,000 x 2.1% = 1,176 > 500 -> one Q1 deposit
    assert [i.due_date for i in find(new, "FUTA deposit")] == ["2026-04-30"]


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
    #   Q2: 25 x 2,400 = 60,000 x 2.1% = 1,260 -> due Jul 31
    #   Q3: cumulative 4,800 each, taxable 2,400 -> 1,260 -> due Oct 31 (Sat) -> Nov 2
    #   Q4: cumulative 7,200 hits the $7,000 cap, taxable 2,200 x 25 = 55,000 x 2.1% = 1,155 -> Jan 31 2027 (Sun) -> Feb 1
    futa = find(r, "FUTA deposit")
    assert [(f.due_date, f.math[-2].split("= ")[1].split(";")[0]) for f in futa] == [
        ("2026-07-31", "$1,260"), ("2026-11-02", "$1,260"), ("2027-02-01", "$1,155")]
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
    assert any("2027 rates are not in the rulebook" in n for n in later.notes)
    low = b.labor_burden_estimate(15, 1000, 5, ui_rate=0.034, as_of=AS_OF)
    assert any("below the 2026 California minimum wage of $16.90" in n for n in low.notes)
    assert any("pass 3.4 instead" in n for n in low.notes)
    with pytest.raises(ValueError):
        b.labor_burden_estimate(0, 2000, 9.5)
    with pytest.raises(ValueError):
        b.labor_burden_estimate(35, 9000, 9.5)


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
                "cities_worked": ["San Diego"], "as_of": AS_OF})
            burden = await c.call_tool("labor_burden_estimate", {"hourly_wage": 35, "annual_hours": 2000,
                                                                 "wc_rate_per_100": 9.5, "as_of": AS_OF})
            return names, calendar_.structured_content, burden.structured_content
    names, cal_out, burden = asyncio.run(run())
    assert {"compliance_calendar", "labor_burden_estimate"} <= names
    assert cal_out["deposit_schedule"]["schedule"] == "monthly" and cal_out["is_llc"] is True
    assert any(i["title"] == "EDD DE 9 and DE 9C for Q4 2026" and i["due_date"] == "2027-02-01" for i in cal_out["items"])
    assert burden["total_annual_cost_usd"] == 82397.0 and burden["burdened_hourly_rate_usd"] == 41.2
