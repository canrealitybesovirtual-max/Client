"""HR mandates module: expected values are traced to the verified rule text (research/web_hr-mandates.verified.json
and the web_labor-hr rules it builds on). The arithmetic is written next to each assertion."""
import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, hr_compliance as hr

AS_OF = "2026-11-25"          # Wednesday


def ids(result):
    return {c.rule_id for c in result.citations}


def mandate(result, prefix):
    return next(m for m in result.mandates if m.topic.startswith(prefix))


def deadline(result, prefix):
    return next(x for x in result.deadlines if x.item.startswith(prefix))


# ================================================================ jobsite_minimum_wage
@pytest.mark.parametrize("city", ["San Diego", "SD", "City of San Diego", "  san diego, CA ", "San Diego City",
                                  "S.D.", "the city of san diego"])
def test_city_aliases_resolve_to_city_of_san_diego(city):
    r = hr.jobsite_minimum_wage(city, AS_OF)
    assert r.jurisdiction == "City of San Diego" and r.jurisdiction_kind == "city_of_san_diego"


def test_san_diego_2026_rate_shortfall_and_2027_changes():
    # hr-local-mw-san-diego-2026: "As of January 1, 2026, the minimum wage is $17.75 per hour"; state $16.90 (MW-2026).
    # required = max(16.90, 17.75) = 17.75. Paid 17.25 -> short 0.50/h x 40 h = $20.00.
    r = hr.jobsite_minimum_wage("SD", AS_OF, hours_in_city_this_week=40, hourly_rate_paid_usd=17.25)
    assert (r.state_minimum_wage_usd, r.local_minimum_wage_usd, r.required_hourly_usd) == (16.90, 17.75, 17.75)
    assert r.local_status == "applies" and r.binding_rate == "City of San Diego minimum wage"
    assert r.pay_check.shortfall_per_hour_usd == 0.5 and r.pay_check.shortfall_usd == 20.0
    assert not r.pay_check.compliant
    # 2027-01-01 is 37 days after 2026-11-25 (5 days left in November + 31 in December + 1).
    ch = {c.what: c for c in r.changes_ahead}
    assert ch["California minimum wage becomes $17.40/hour"].days_away == 37
    assert ch["City of San Diego minimum wage becomes $18.35/hour"].date == "2027-01-01"
    assert "before 2027-01-01 (37 days)" in r.next_step and "$20.00 owed" in r.next_step
    # Exempt 2026: 2 x 16.90 x 40 x 52 = 70,304; hand tools: 2 x 16.90 = 33.80.
    assert r.exempt_salary.annual_usd == 70304.0 and r.hand_tools_threshold_hourly_usd == 33.8
    assert r.verified is True
    assert {"hr-local-mw-san-diego-2026", "ca-minimum-wage-2026", "local-minimum-wage-overrides-state",
            "hr-state-minimum-wage-2027", "hr-local-mw-san-diego-2027"} <= ids(r)


def test_2027_rates_exempt_salary_and_hand_tools():
    # hr-state-minimum-wage-2027: $17.40 from 2027-01-01; hr-local-mw-san-diego-2027: $18.35.
    # hr-exempt-salary-threshold-2027: 2 x 17.40 x 40 x 52 = 72,384 a year; /12 = 6,032; /52 = 1,392.
    # Hand tools: 2 x 17.40 = 34.80 (the 2027 figure hr-state-minimum-wage-2027 also derives).
    r = hr.jobsite_minimum_wage("San Diego", "01/04/2027", hours_in_city_this_week=8)
    assert (r.state_minimum_wage_usd, r.required_hourly_usd) == (17.40, 18.35)
    assert r.state_effective == "2027-01-01"
    assert (r.exempt_salary.annual_usd, r.exempt_salary.monthly_usd, r.exempt_salary.weekly_usd) == (72384, 6032.0, 1392.0)
    assert r.hand_tools_threshold_hourly_usd == 34.8 == core.rule("hr-state-minimum-wage-2027").param(
        "derived_2x_minimum_wage_hourly_usd")
    assert r.changes_ahead == []                   # no verified 2028 rate yet
    assert {"hr-state-minimum-wage-2027", "hr-local-mw-san-diego-2027", "hr-exempt-salary-threshold-2027"} <= ids(r)


def test_historical_rates_come_from_the_rule_history():
    # hr-local-mw-san-diego-2026 history_usd: 2025-01-01 -> 17.25; ca-minimum-wage-2026 prior_rates: 2025 -> 16.50.
    r = hr.jobsite_minimum_wage("San Diego", "2025-06-01")
    assert (r.state_minimum_wage_usd, r.local_minimum_wage_usd, r.required_hourly_usd) == (16.50, 17.25, 17.25)
    assert r.exempt_salary is None                # only 2026 and 2027 thresholds are stated in the rulebook


@pytest.mark.parametrize("city", ["Chula Vista", "unincorporated San Diego County", "County of San Diego",
                                  "SD County", "National City", "Oceanside, CA"])
def test_rest_of_san_diego_county_uses_state_rate(city):
    # hr-local-mw-san-diego-2026: "unincorporated_or_other_cities_in_san_diego_county": "state minimum wage applies".
    r = hr.jobsite_minimum_wage(city, AS_OF)
    assert r.jurisdiction_kind == "san_diego_county_outside_city"
    assert r.local_status == "no_local_ordinance" and r.required_hourly_usd == 16.90
    assert r.binding_rate == "California minimum wage" and r.verified is True


def test_other_california_city_is_not_guessed():
    # local-minimum-wage-overrides-state: no local rates were read from official city sites in this pass.
    r = hr.jobsite_minimum_wage("Los Angeles", AS_OF, hourly_rate_paid_usd=17.00)
    assert r.jurisdiction_kind == "other_california" and r.local_status == "not_in_rulebook"
    assert r.required_hourly_usd is None and r.binding_hourly_usd is None and r.pay_check is None
    assert r.state_minimum_wage_usd == 16.90
    assert "official city or county" in r.next_step
    assert any("not checked" in n for n in r.notes)


def test_two_hour_weekly_coverage_test():
    # hr-local-mw-san-diego-2026: coverage_min_hours_in_city_per_week = 2. 1.5 h -> City ordinance does not apply.
    r = hr.jobsite_minimum_wage("San Diego", AS_OF, hours_in_city_this_week=1.5)
    assert r.local_status == "not_covered" and r.required_hourly_usd == 16.90
    r2 = hr.jobsite_minimum_wage("San Diego", AS_OF, hours_in_city_this_week=2)
    assert r2.local_status == "applies" and r2.required_hourly_usd == 17.75


def test_federal_enclave_and_exempt_staff_are_outside_the_city_ordinance():
    # hr-local-mw-san-diego-2026 not_covered: federal enclave; persons not entitled to the CA minimum wage.
    r = hr.jobsite_minimum_wage("San Diego", AS_OF, inside_federal_enclave=True)
    assert r.local_status == "not_covered" and r.required_hourly_usd is None
    assert any("federal enclave" in n.lower() for n in r.notes)
    ex = hr.jobsite_minimum_wage("San Diego", AS_OF, exempt_salaried=True)
    assert ex.local_status == "not_covered" and ex.required_hourly_usd is None
    assert "$70,304.00 a year ($5,858.67/month)" in ex.next_step     # exempt-salary-threshold-2026 derived values


def test_flagger_wage_on_city_permit():
    # hr-sd-traffic-control-worker-wage, 2026-10-09 to 2027-06-30: 49.20 + 27.67 = 76.87; OT/Sat 101.47; Sun 126.07.
    r = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, hourly_rate_paid_usd=60)
    tcw = r.special_rates[0]
    assert (tcw.basic_hourly_usd, tcw.benefits_hourly_usd, tcw.total_hourly_usd) == (49.20, 27.67, 76.87)
    assert (tcw.overtime_saturday_usd, tcw.sunday_holiday_usd) == (101.47, 126.07)
    # No benefits paid -> all 76.87 in cash; paid 60 -> short 16.87/h.
    assert r.binding_hourly_usd == 76.87 and r.pay_check.shortfall_per_hour_usd == 16.87
    # Benefits of 27.67/h paid to plans -> cash owed 76.87 - 27.67 = 49.20 (never below basic 49.20).
    r2 = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, benefits_paid_usd_per_hour=27.67)
    assert r2.binding_hourly_usd == 49.20
    # Benefits 10/h -> cash 76.87 - 10 = 66.87.
    r3 = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, benefits_paid_usd_per_hour=10)
    assert r3.binding_hourly_usd == 66.87
    # 2027-07-01 increase: 76.87 + 3.66 = 80.53 for permits issued from that date; 218 days away.
    inc = next(c for c in r.changes_ahead if c.what.startswith("Flagger"))
    assert (inc.date, inc.days_away, inc.hourly_usd) == ("2027-07-01", 218, 80.53)
    assert "Permits issued from 2027-07-01" in r.next_step
    assert {"hr-sd-traffic-control-worker-wage", "hr-sd-traffic-control-worker-notice"} <= ids(r)


def test_flagger_rate_is_locked_at_permit_issue_date():
    # History 2025-07-01/2026-10-08: 45.54 + 27.77 = 73.31. A permit issued 2026-08-01 keeps it on 2026-11-25.
    r = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, permit_issue_date="2026-08-01")
    assert r.special_rates[0].total_hourly_usd == 73.31 and r.binding_hourly_usd == 73.31
    # Permit issued 2027-07-15: 76.87 + 3.66 = 80.53 total; the wage/fringe split is not published.
    r2 = hr.jobsite_minimum_wage("San Diego", "2027-07-20", traffic_control_permit=True, permit_issue_date="2027-07-15")
    assert r2.special_rates[0].total_hourly_usd == 80.53 and r2.special_rates[0].basic_hourly_usd is None


def test_flagger_exceptions():
    # hr-sd-traffic-control-worker-coverage-exceptions: single-family/duplex; project cost max_project_cost_usd 1000.
    sf = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, single_family_or_duplex=True)
    assert sf.special_rates[0].applies is False and sf.binding_rate == "City of San Diego minimum wage"
    small = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, project_cost_usd=1000)
    assert small.special_rates[0].applies is False
    big = hr.jobsite_minimum_wage("San Diego", AS_OF, traffic_control_permit=True, project_cost_usd=1000.01)
    assert big.special_rates[0].applies is True and big.binding_hourly_usd == 76.87


def test_living_wage_on_city_contracts():
    # hr-sd-living-wage-fy2027 (2026-07-01 to 2027-06-30): in city 18.43 + 3.52 = 21.95; outside 18.18 + 3.52 = 21.70.
    none = hr.jobsite_minimum_wage("San Diego", AS_OF, city_living_wage_contract=True)
    assert none.binding_hourly_usd == 21.95
    full = hr.jobsite_minimum_wage("San Diego", AS_OF, city_living_wage_contract=True, benefits_paid_usd_per_hour=3.52)
    assert full.binding_hourly_usd == 18.43
    part = hr.jobsite_minimum_wage("San Diego", AS_OF, city_living_wage_contract=True, benefits_paid_usd_per_hour=2)
    assert part.binding_hourly_usd == 19.95                       # 21.95 - 2.00
    extra = hr.jobsite_minimum_wage("San Diego", AS_OF, city_living_wage_contract=True, benefits_paid_usd_per_hour=9)
    assert extra.binding_hourly_usd == 18.43                      # credit capped at the 3.52 health benefit
    out = hr.jobsite_minimum_wage("Chula Vista", AS_OF, city_living_wage_contract=True)
    assert out.binding_hourly_usd == 21.70
    later = hr.jobsite_minimum_wage("San Diego", "2027-08-02", city_living_wage_contract=True)
    assert later.special_rates[0].required_cash_hourly_usd is None    # FY2028 rate not in the rulebook
    assert "hr-sd-living-wage-leave-notice-records" in ids(none)


def test_jobsite_wage_bad_inputs():
    with pytest.raises(ValueError, match="city is required"):
        hr.jobsite_minimum_wage("  ", AS_OF)
    with pytest.raises(ValueError, match="no verified California minimum wage for 2028-02-01"):
        hr.jobsite_minimum_wage("San Diego", "2028-02-01")
    with pytest.raises(ValueError, match="no verified California minimum wage for 2022-12-31"):
        hr.jobsite_minimum_wage("San Diego", "2022-12-31")
    with pytest.raises(ValueError, match="on_date must be a date"):
        hr.jobsite_minimum_wage("San Diego", "next tuesday")
    with pytest.raises(ValueError, match="hourly_rate_paid_usd must be a finite number"):
        hr.jobsite_minimum_wage("San Diego", AS_OF, hourly_rate_paid_usd=-1)
    with pytest.raises(ValueError, match="cannot exceed 168"):
        hr.jobsite_minimum_wage("San Diego", AS_OF, hours_in_city_this_week=200)
    with pytest.raises(ValueError, match="City of San Diego traffic control permits"):
        hr.jobsite_minimum_wage("Los Angeles", AS_OF, traffic_control_permit=True)


# ================================================================ employer_size_mandates
def test_mandates_for_a_28_person_san_diego_contractor():
    r = hr.employer_size_mandates(28, employees_at_largest_location=26, has_group_health_plan=True,
                                  employees_in_san_diego_city=12, has_office_staff=True, general_contractor=True,
                                  as_of=AS_OF)
    # Pay transparency: hr-pay-scale-third-party-postings-and-count min_employees 15 -> 28 >= 15.
    pay = mandate(r, "Pay transparency")
    assert pay.applies == "yes" and pay.size_triggered and "28 >= 15" in pay.why
    assert "$100.00 to $10,000.00 per violation" in pay.what_to_do      # hr-pay-transparency-civil-penalty
    # School activities: 25+ at the same location -> 26 >= 25; 40 h/year, 8 h/month.
    school = mandate(r, "School activities")
    assert school.applies == "yes" and "26 >= 25" in school.why and "40 hours a year" in school.what_to_do
    # Training leave 50+ -> no; rehab and literacy 25+ (private) -> yes.
    assert mandate(r, "Emergency responder training").applies == "no"
    assert mandate(r, "Alcohol or drug").applies == "yes" and mandate(r, "Adult literacy").applies == "yes"
    # FMLA: 28 < 50 -> no; COBRA: 20-employee test needs last year's business days -> check (likely covered).
    assert mandate(r, "Federal FMLA").applies == "no"
    cobra = mandate(r, "Federal COBRA")
    assert cobra.applies == "check" and "likely covered at 28" in cobra.why
    assert mandate(r, "ACA employer mandate").applies == "no"
    # Lactation: under 50 -> undue-hardship text; GC duty within 2 business days.
    lac = mandate(r, "Lactation")
    assert "Under 50 employees" in lac.what_to_do and "within 2 business days" in lac.what_to_do
    assert "hr-lactation-multiemployer-worksite-2-business-days" in lac.rule_ids
    # San Diego fee: $1.47 x 12 employees = $17.64 a year (proposed $2.82 x 12 = $33.84, adoption not verified).
    assert r.san_diego_enforcement_fee_usd == 17.64
    sd = mandate(r, "City of San Diego")
    assert "$1.47 x 12 = $17.64" in sd.what_to_do and "$2.82 x 12 = $33.84" in sd.what_to_do
    assert "$17.75/hour" in sd.what_to_do
    assert "Wage Order 4" in mandate(r, "Workplace postings").what_to_do
    # Harassment training (unchecked) and the FEHA-moved protections (unverifiable) -> verified False.
    assert mandate(r, "Harassment").verified is False and mandate(r, "Jury duty").verified is False
    assert r.verified is False
    assert r.applies_count == sum(m.applies == "yes" for m in r.mandates)
    assert "Size-triggered duties you now have: Pay transparency" in r.next_step


def test_mandates_without_unverified_topics_are_verified():
    r = hr.employer_size_mandates(28, employees_at_largest_location=26, include_unverified=False, as_of=AS_OF)
    assert r.verified is True and all(m.verified for m in r.mandates)
    assert set(r.omitted_unverified) == {"Harassment prevention training",
                                         "Jury duty, court witness and crime victim protections",
                                         "Crime victim leave (medical care, counseling, safety planning)"}
    assert not ({"hr-jury-duty-time-off", "harassment-prevention-training"} & ids(r))


def test_mandates_for_a_4_person_shop():
    r = hr.employer_size_mandates(4, as_of=AS_OF)
    assert mandate(r, "Harassment").applies == "no"                  # 4 < 5
    pay = mandate(r, "Pay transparency")
    assert pay.applies == "yes" and not pay.size_triggered            # on-request duties apply at any size
    # Total 4 < 25, so no single location can reach 25.
    assert mandate(r, "School activities").applies == "no"
    assert mandate(r, "Crime victim leave").applies == "no"
    assert not any(m.topic.startswith("Federal COBRA") for m in r.mandates)   # no group health plan
    assert any("only if you sponsor a group health plan" in n for n in r.notes)
    assert r.san_diego_enforcement_fee_usd is None


def test_mandate_thresholds_at_the_edges():
    # Rehab/literacy: private employers only.
    pub = hr.employer_size_mandates(30, private_employer=False, as_of=AS_OF)
    assert mandate(pub, "Alcohol or drug").applies == "no" and "not a private employer" in mandate(pub, "Alcohol").why
    # School activities with no location count and 40 employees -> check.
    assert mandate(pub, "School activities").applies == "check"
    # ACA: average rounds down: 49.9 -> 49 < 50 (no); 50.0 -> yes.
    assert mandate(hr.employer_size_mandates(60, aca_average_ft_plus_fte_last_year=49.9, as_of=AS_OF),
                   "ACA").applies == "no"
    assert mandate(hr.employer_size_mandates(60, aca_average_ft_plus_fte_last_year=50, as_of=AS_OF),
                   "ACA").applies == "yes"
    # FMLA: 50+ employees for 20+ workweeks.
    assert mandate(hr.employer_size_mandates(60, fmla_workweeks_with_50_or_more=20, as_of=AS_OF), "Federal FMLA").applies == "yes"
    assert mandate(hr.employer_size_mandates(60, fmla_workweeks_with_50_or_more=19, as_of=AS_OF), "Federal FMLA").applies == "no"
    assert mandate(hr.employer_size_mandates(60, as_of=AS_OF), "Federal FMLA").applies == "check"
    # Training leave at exactly 50.
    assert mandate(hr.employer_size_mandates(50, as_of=AS_OF), "Emergency responder training").applies == "yes"
    # COBRA when the business-days answer is known.
    c = hr.employer_size_mandates(25, has_group_health_plan=True,
                                  cobra_under_20_on_half_of_business_days_last_year=True, as_of=AS_OF)
    assert mandate(c, "Federal COBRA").applies == "no"


def test_mandates_bad_inputs():
    with pytest.raises(ValueError, match="employee_count must be a whole number of at least 1"):
        hr.employer_size_mandates(0)
    with pytest.raises(ValueError, match="employee_count must be a whole number"):
        hr.employer_size_mandates(12.5)
    with pytest.raises(ValueError, match="cannot exceed employee_count"):
        hr.employer_size_mandates(10, employees_in_california=11)
    with pytest.raises(ValueError, match="cannot exceed employee_count"):
        hr.employer_size_mandates(10, employees_at_largest_location=12)
    with pytest.raises(ValueError, match="cannot exceed employee_count"):
        hr.employer_size_mandates(10, employees_in_san_diego_city=11)
    with pytest.raises(ValueError, match="0-53"):
        hr.employer_size_mandates(60, fmla_workweeks_with_50_or_more=60)


# ================================================================ aca_employer_status
def crew(ft=0, ft_hours=150, pt=0, pt_hours=90):
    return ([{"name": f"FT{i}", "hours": [ft_hours] * 12} for i in range(ft)]
            + [{"name": f"PT{i}", "hours": [pt_hours] * 12} for i in range(pt)])


def test_fte_example_from_the_regulation_makes_an_ale():
    # hr-aca-fte-calculation-120-hours note: 40 part-timers x 90 h = 3,600 h / 120 = 30 FTEs.
    # 25 full-time (150 h >= 130) + 30 FTEs = 55 each month; 660 / 12 = 55 -> ALE for 2026.
    r = hr.aca_employer_status(2025, crew(ft=25, pt=40), as_of=AS_OF)
    m = r.months[0]
    assert (m.full_time_employees, m.non_full_time_hours_counted, m.ftes, m.total) == (25, 3600.0, 30.0, 55.0)
    assert r.average == 55.0 and r.average_rounded_down == 55 and r.applicable_large_employer
    assert r.status_year == 2026 and r.threshold == 50
    # hr-aca-affordability-safe-harbors: regulatory 9.5% shown with the warning that the plan-year % is not in the book.
    assert "hr-aca-affordability-safe-harbors" in ids(r) and any("9.5%" in n and "not in the rulebook" in n
                                                                 for n in r.notes)
    assert r.verified is True


def test_hours_are_capped_at_120_and_130_makes_full_time():
    # 129 h is not full-time (< 130) and counts as only 120 toward FTEs; 130 h is full-time.
    r = hr.aca_employer_status(2025, [{"name": "A", "hours": [129] * 12}, {"name": "B", "hours": [130] * 12}],
                               as_of=AS_OF)
    assert r.months[0].full_time_employees == 1 and r.months[0].non_full_time_hours_counted == 120.0
    assert r.months[0].ftes == 1.0 and not r.applicable_large_employer


def test_average_is_rounded_down():
    # 40 FT + 1,198.8 h / 120 = 40 + 9.99 = 49.99 every month -> average 49.99, rounded down 49 < 50.
    r = hr.aca_employer_status(2025, monthly_full_time_counts=[40] * 12, monthly_non_full_time_hours=[1198.8] * 12,
                               as_of=AS_OF)
    assert r.average == 49.99 and r.average_rounded_down == 49 and not r.applicable_large_employer
    assert "close (49 of 50)" in r.next_step and r.payments == [] and r.deadlines == []


def test_seasonal_exception_four_months():
    # 40 FT all year, 70 in Jun-Sep (30 seasonal). 8 x 40 + 4 x 70 = 600 / 12 = 50 -> ALE by the average.
    # hr-aca-seasonal-worker-exception-120-days: over 50 in 4 months (alternative_months 4), seasonal -> not an ALE.
    counts = [40] * 5 + [70] * 4 + [40] * 3
    base = hr.aca_employer_status(2025, monthly_full_time_counts=counts, monthly_non_full_time_hours=[0] * 12,
                                  as_of=AS_OF)
    assert base.average == 50.0 and base.applicable_large_employer
    r = hr.aca_employer_status(2025, monthly_full_time_counts=counts, monthly_non_full_time_hours=[0] * 12,
                               excess_over_50_were_seasonal=True, as_of=AS_OF)
    assert not r.applicable_large_employer and r.seasonal_exception_applied
    assert r.months_over_50 == ["2025-06", "2025-07", "2025-08", "2025-09"]
    # Five peak months: 7 x 40 + 5 x 70 = 630 / 12 = 52.5 -> still an ALE (more than 4 months over 50).
    five = [40] * 5 + [70] * 5 + [40] * 2
    r5 = hr.aca_employer_status(2025, monthly_full_time_counts=five, monthly_non_full_time_hours=[0] * 12,
                                excess_over_50_were_seasonal=True, as_of=AS_OF)
    assert r5.applicable_large_employer and not r5.seasonal_exception_applied


def test_offer_rule_and_payment_formulas():
    # hr-aca-95-percent-offer-rule: not_offered <= max(5, 5% x FT). FT 130 -> max(5, 6.5) -> 6; offer 124.
    r = hr.aca_employer_status(2025, crew(ft=130), current_full_time_employees=130, as_of=AS_OF)
    assert (r.may_fail_to_offer_at_most, r.must_offer_at_least) == (6, 124)
    # FT 45 -> max(5, 2.25) = 5 -> offer 40.
    r45 = hr.aca_employer_status(2025, crew(ft=45, pt=20), as_of=AS_OF)
    assert (r45.full_time_employees_for_offer, r45.may_fail_to_offer_at_most, r45.must_offer_at_least) == (45, 5, 40)
    # Without the IRS indexed amount no dollars are shown, only the formula with the $2,000 base.
    a = next(p for p in r45.payments if p.section == "4980H(a)")
    assert a.monthly_usd is None and "$2,000.00" in a.math and "(45 - 30)" in a.math
    # The regulation's own example (hr-aca-4980h-a-no-offer-penalty note): 54 FT - 30 = 24 x $2,000 = $48,000.
    ex = hr.aca_employer_status(2025, crew(ft=54), indexed_4980h_a_annual_usd=2000, as_of=AS_OF)
    assert next(p for p in ex.payments if p.section == "4980H(a)").annual_usd == 48000.0
    # 4980H(b) with test amounts: 3,600 / 12 x 4 subsidized = 1,200 a month, under the (a) cap 2,400/12 x 15 = 3,000.
    b = hr.aca_employer_status(2025, crew(ft=45, pt=20), indexed_4980h_a_annual_usd=2400,
                               indexed_4980h_b_annual_usd=3600, employees_with_premium_tax_credit=4, as_of=AS_OF)
    pa, pb = (next(p for p in b.payments if p.section == s) for s in ("4980H(a)", "4980H(b)"))
    assert pa.monthly_usd == 3000.0 and pa.annual_usd == 36000.0 and pb.monthly_usd == 1200.0
    # 40 subsidized: 3,600 / 12 x 40 = 12,000 -> capped at 3,000.
    capped = hr.aca_employer_status(2025, crew(ft=45, pt=20), indexed_4980h_a_annual_usd=2400,
                                    indexed_4980h_b_annual_usd=3600, employees_with_premium_tax_credit=40, as_of=AS_OF)
    assert next(p for p in capped.payments if p.section == "4980H(b)").monthly_usd == 3000.0


def test_form_dates_for_tax_year_2026_match_the_rule():
    # hr-aca-1094c-1095c-filing-deadline tax_year_2026: paper 2027-03-01 (Feb 28 is a Sunday), electronic 2027-03-31.
    # hr-aca-1095c-furnish-deadline tax_year_2026: 2027-01-31 + 30 days = 2027-03-02 (Tuesday).
    r = hr.aca_employer_status(2025, crew(ft=55), information_returns_count=5, as_of=AS_OF)
    file26 = core.rule("hr-aca-1094c-1095c-filing-deadline").param("tax_year_2026")
    furn26 = core.rule("hr-aca-1095c-furnish-deadline").param("tax_year_2026")
    assert deadline(r, "Furnish").due_date == furn26["due_with_automatic_extension_derived"] == "2027-03-02"
    assert deadline(r, "Paper-file").due_date == file26["paper_due_after_weekend_rule"] == "2027-03-01"
    assert deadline(r, "Furnish").days_left == 97 and r.must_file_electronically is False
    # 55 Forms 1095-C alone reach the 10-return e-file threshold (hr-aca-efile-10-return-threshold).
    e = hr.aca_employer_status(2025, crew(ft=55), as_of=AS_OF)
    assert e.must_file_electronically is True and not any(x.item.startswith("Paper") for x in e.deadlines)
    assert deadline(e, "E-file").due_date == file26["electronic_due"] == "2027-03-31"


def test_form_dates_for_tax_year_2027():
    # 2026 hours -> 2027 status -> forms in 2028: Feb 28 2028 is a Monday, Mar 31 2028 a Friday,
    # Jan 31 2028 + 30 days = Mar 1 2028 (leap year), a Wednesday: no weekend moves.
    r = hr.aca_employer_status(2026, crew(ft=55), information_returns_count=9, as_of=AS_OF)
    assert r.status_year == 2027
    assert deadline(r, "Furnish").due_date == "2028-03-01"
    assert deadline(r, "Paper-file").due_date == "2028-02-28" and deadline(r, "E-file").due_date == "2028-03-31"


def test_first_year_relief_and_new_employer():
    # hr-aca-first-year-ale-april-1-offer: offer by April 1 of the first ALE year (2026) -> past on 2026-11-25.
    r = hr.aca_employer_status(2025, crew(ft=55), first_year_as_ale=True, as_of=AS_OF)
    april = deadline(r, "First ALE year")
    assert april.due_date == "2026-04-01" and april.status == "overdue"
    new = hr.aca_employer_status(2026, crew(ft=55), new_employer=True, as_of=AS_OF)
    assert new.status_year == 2026 and "hr-aca-new-employer-ale-status" in ids(new)


def test_employee_hours_accepts_csv_and_month_keys():
    csv_text = "Name\tJan\tFeb\tMar\tApr\tMay\tJun\tJul\tAug\tSep\tOct\tNov\tDec\n" + \
               "Ana Lopez\t" + "\t".join(["160"] * 12) + "\nBen Ortiz\t" + "\t".join(["60"] * 12) + "\n"
    r = hr.aca_employer_status("2025", csv_text, as_of=AS_OF)
    # Ana 160 >= 130 -> FT; Ben 60 -> 0.5 FTE -> 1.5 a month.
    assert r.months[0].total == 1.5 and r.average == 1.5
    keyed = hr.aca_employer_status(2025, [{"employee": "Ana", "January": 160, "Feb": 160, "march": 160, "4": 160,
                                           "May": 160, "Jun": 160, "Jul": 160, "Aug": 160, "Sep": 160, "Oct": 160,
                                           "Nov": 160, "Dec": 160}], as_of=AS_OF)
    assert keyed.months[3].full_time_employees == 1
    nested = hr.aca_employer_status(2025, [{"name": "Ana", "hours": {"2025-01": 140, "2025-12": 10}}], as_of=AS_OF)
    assert nested.months[0].full_time_employees == 1 and nested.months[11].ftes == round(10 / 120, 2)


def test_aca_bad_inputs():
    with pytest.raises(ValueError, match="12 monthly values"):
        hr.aca_employer_status(2025, [{"name": "A", "hours": [100] * 11}], as_of=AS_OF)
    with pytest.raises(ValueError, match="between 0 and 744"):
        hr.aca_employer_status(2025, [{"name": "A", "hours": [800] * 12}], as_of=AS_OF)
    with pytest.raises(ValueError, match="more than once"):
        hr.aca_employer_status(2025, [{"name": "A", "hours": [1] * 12}, {"name": "A", "hours": [2] * 12}], as_of=AS_OF)
    with pytest.raises(ValueError, match="not both"):
        hr.aca_employer_status(2025, crew(ft=1), monthly_full_time_counts=[1] * 12,
                               monthly_non_full_time_hours=[0] * 12, as_of=AS_OF)
    with pytest.raises(ValueError, match="give employee_hours"):
        hr.aca_employer_status(2025, as_of=AS_OF)
    with pytest.raises(ValueError, match="12 values each"):
        hr.aca_employer_status(2025, monthly_full_time_counts=[1] * 12, monthly_non_full_time_hours=[0] * 3,
                               as_of=AS_OF)
    with pytest.raises(ValueError, match="measurement_year must be between"):
        hr.aca_employer_status(2030, crew(ft=1), as_of=AS_OF)
    with pytest.raises(ValueError, match="12 month columns"):
        hr.aca_employer_status(2025, "name,jan,feb\nA,1,2", as_of=AS_OF)
    with pytest.raises(ValueError, match="not a number of hours"):
        hr.aca_employer_status(2025, "name," + ",".join(hr.MONTHS) + "\nA," + ",".join(["x"] * 12), as_of=AS_OF)


# ================================================================ cobra_deadlines
def test_cobra_layoff_with_outside_administrator():
    # hr-cobra-employer-notice-to-administrator-30-days: 2026-11-13 + 30 = 2026-12-13.
    # hr-cobra-election-notice-14-or-44-days: administrator notified 12-13 (latest allowed) + 14 = 2026-12-27.
    # hr-cobra-election-period-60-days: later of loss (11-13) and notice (12-27) + 60 = 2027-02-25.
    # hr-cobra-maximum-coverage-18-29-36-months: 2026-11-13 + 18 months = 2028-05-13.
    # hr-cobra-premium-102-percent: $850 x 102% = $867.00.
    r = hr.cobra_deadlines("Laid off", "2026-11-13", monthly_plan_cost_usd=850, as_of=AS_OF)
    assert r.qualifying_event == "termination" and r.federal_cobra_applies and r.employer_reports_event
    admin = deadline(r, "Notify the plan administrator")
    assert (admin.due_date, admin.days_left, admin.status) == ("2026-12-13", 18, "upcoming")
    assert deadline(r, "Administrator sends").due_date == "2026-12-27"
    assert deadline(r, "Election window").due_date == "2027-02-25"
    assert (r.maximum_coverage_months, r.coverage_may_end, r.max_monthly_premium_usd) == (18, "2028-05-13", 867.0)
    assert r.next_step.startswith("Notify the plan administrator of the qualifying event by 2026-12-13")
    assert r.verified is True


def test_cobra_employer_is_administrator_44_days():
    # 2026-11-13 + 44 days = 2026-12-27 (17 days to Nov 30 + 27).
    r = hr.cobra_deadlines("reduction in hours", "11/13/2026", employer_is_plan_administrator=True, as_of=AS_OF)
    assert r.qualifying_event == "reduction_in_hours"
    assert deadline(r, "Send the COBRA election notice").due_date == "2026-12-27"
    assert not any(x.item.startswith("Notify the plan administrator") for x in r.deadlines)


def test_cobra_actual_dates_late_notice_and_first_payment():
    # Administrator told 2026-12-20 (after the 12-13 deadline) -> done_late; election notice due 12-20 + 14 = 2027-01-03.
    r = hr.cobra_deadlines("termination", "2026-11-13", administrator_notified_date="2026-12-20",
                           election_notice_date="2026-12-28", election_date="2027-01-15", as_of="2027-01-20")
    assert deadline(r, "Notify the plan administrator").status == "done_late"
    sent = deadline(r, "Administrator sends")
    assert sent.due_date == "2027-01-03" and sent.status == "done_on_time"     # sent 12-28 <= 01-03
    # Window from the actual notice: 2026-12-28 + 60 = 2027-02-26. First premium: 2027-01-15 + 45 = 2027-03-01.
    assert deadline(r, "Election window").due_date == "2027-02-26"
    assert deadline(r, "First premium").due_date == "2027-03-01"
    assert "late" in r.next_step


def test_cobra_other_events_and_disability():
    # Divorce: the beneficiary reports it; max 36 months (hr-cobra-maximum-coverage-18-29-36-months).
    d = hr.cobra_deadlines("divorce", "2026-11-13", as_of=AS_OF)
    assert not d.employer_reports_event and d.maximum_coverage_months == 36 and d.coverage_may_end == "2029-11-13"
    assert deadline(d, "Administrator sends").status == "info"
    assert "hr-cobra-employer-notice-to-administrator-30-days" not in ids(d)
    # Disability extension: 29 months; premium in months 19-29 up to 150%: $850 x 150% = $1,275.
    dis = hr.cobra_deadlines("termination", "2026-11-13", disability_extension=True, monthly_plan_cost_usd=850,
                             as_of=AS_OF)
    assert dis.maximum_coverage_months == 29 and dis.coverage_may_end == "2029-04-13"
    assert dis.max_monthly_premium_disability_months_usd == 1275.0 and "months 19-29" in " ".join(dis.math)
    # Month-end clamp: 2026-08-31 + 18 months = 2028-02-29 (leap year).
    assert hr.cobra_deadlines("termination", "2026-08-31", as_of=AS_OF).coverage_may_end == "2028-02-29"
    # Plan measures from loss of coverage (11-30): admin notice 11-30 + 30 = 12-30; coverage to 2028-05-30.
    loss = hr.cobra_deadlines("termination", "2026-11-13", coverage_loss_date="2026-11-30",
                              plan_measures_from_loss_of_coverage=True, as_of=AS_OF)
    assert deadline(loss, "Notify").due_date == "2026-12-30" and loss.coverage_may_end == "2028-05-30"


def test_cobra_not_applicable_cases():
    small = hr.cobra_deadlines("termination", "2026-11-13", employer_normally_under_20=True, as_of=AS_OF)
    assert not small.federal_cobra_applies and small.deadlines == [] and "Cal-COBRA" in small.next_step
    gm = hr.cobra_deadlines("fired", "2026-11-13", gross_misconduct=True, as_of=AS_OF)
    assert not gm.federal_cobra_applies and "gross misconduct" in gm.next_step


def test_cobra_bad_inputs():
    with pytest.raises(ValueError, match="qualifying_event must be one of"):
        hr.cobra_deadlines("vacation", "2026-11-13")
    with pytest.raises(ValueError, match="cannot be before event_date"):
        hr.cobra_deadlines("termination", "2026-11-13", election_date="2026-11-01")
    with pytest.raises(ValueError, match="cannot be before event_date"):
        hr.cobra_deadlines("termination", "2026-11-13", coverage_loss_date="2026-11-01")
    with pytest.raises(ValueError, match="only extends COBRA after a termination"):
        hr.cobra_deadlines("death", "2026-11-13", disability_extension=True)
    with pytest.raises(ValueError, match="greater than 0"):
        hr.cobra_deadlines("termination", "2026-11-13", monthly_plan_cost_usd=0)
    with pytest.raises(ValueError, match="event_date must be a date"):
        hr.cobra_deadlines("termination", "soon")


# ================================================================ leave_request_check
def test_school_activities_caps():
    # hr-school-activities-leave-25-employees: 40 h/year, 8 h/calendar month.
    # Used 36 this year: 40 - 36 = 4 left; 6 requested -> 4 protected.
    r = hr.leave_request_check("school event", 40, employees_at_location=30, hours_requested=6,
                               hours_used_this_year=36, as_of=AS_OF)
    assert (r.leave_type, r.protected, r.remaining, r.can_approve_now) == ("school_activities", "yes", 4.0, 4.0)
    assert r.enforced_by.startswith("Labor Commissioner") and r.verified is True
    # Used 10 this year, 4 this month: year 30 left, month 8 - 4 = 4 -> 4 protected of 6.
    m = hr.leave_request_check("school_activities", 40, employees_at_location=30, hours_requested=6,
                               hours_used_this_year=10, hours_used_this_month=4, as_of=AS_OF)
    assert m.remaining == 4.0 and m.can_approve_now == 4.0
    # Emergencies have no monthly cap: 6 of 6 protected.
    e = hr.leave_request_check("school emergency", 40, employees_at_location=30, hours_requested=6,
                               hours_used_this_year=10, hours_used_this_month=4, school_emergency=True, as_of=AS_OF)
    assert e.remaining == 30.0 and e.can_approve_now == 6.0
    # Count is per location: 24 at the location -> not protected; no location given with 40 total -> check.
    assert hr.leave_request_check("school", 40, employees_at_location=24, as_of=AS_OF).protected == "no"
    assert hr.leave_request_check("school", 40, as_of=AS_OF).protected == "check"
    assert hr.leave_request_check("school", 20, as_of=AS_OF).protected == "no"


def test_emergency_training_days():
    # hr-emergency-responder-training-50-employees-14-days: 50+ employees, 14 days per calendar year.
    # Used 10: 14 - 10 = 4 left; 6 requested -> 4 protected.
    r = hr.leave_request_check("volunteer firefighter training", 60, days_requested=6, days_used_this_year=10,
                               as_of=AS_OF)
    assert (r.leave_type, r.protected, r.remaining, r.can_approve_now) == ("emergency_training", "yes", 4.0, 4.0)
    small = hr.leave_request_check("emergency_training", 49, days_requested=2, as_of=AS_OF)
    assert small.protected == "no" and small.can_approve_now == 0.0
    duty = hr.leave_request_check("Volunteer-Firefighter", 3, as_of=AS_OF)
    assert duty.leave_type == "emergency_duty" and duty.protected == "yes"


def test_feha_moved_protections_are_unverified_and_go_to_crd():
    # hr-jury-duty-time-off (unverifiable): moved to FEHA for violations after 2024-12-31.
    j = hr.leave_request_check(" Jury Duty ", 8, as_of=AS_OF)
    assert j.leave_type == "jury_duty" and j.protected == "yes" and j.verified is False
    assert j.enforced_by.startswith("Civil Rights Department") and "2024-12-31" in j.complaint_deadline
    assert "Labor Commissioner" not in j.enforced_by
    # Witness (230(b)) moved 2024-12-31; victim court proceedings (230.2(b), 230.5) moved 2025-12-31.
    assert "2024-12-31" in hr.leave_request_check("subpoena", 8, as_of=AS_OF).complaint_deadline
    assert "2025-12-31" in hr.leave_request_check("crime_victim_court", 8, as_of=AS_OF).complaint_deadline
    # Victim leave: 25+ under the old Labor Code 230.1; FEHA threshold unread -> 10 employees is "check".
    v = hr.leave_request_check("domestic violence leave", 10, as_of=AS_OF)
    assert v.leave_type == "crime_victim_leave" and v.protected == "check"
    assert any("signed written statement" in s for s in v.employer_must)
    assert hr.leave_request_check("victim leave", 30, as_of=AS_OF).protected == "yes"


def test_rehab_literacy_donor_and_teacher_request():
    assert hr.leave_request_check("rehab", 30, as_of=AS_OF).protected == "yes"
    assert hr.leave_request_check("rehab", 24, as_of=AS_OF).protected == "no"
    assert hr.leave_request_check("drug rehab", 30, private_employer=False, as_of=AS_OF).protected == "no"
    lit = hr.leave_request_check("literacy", 25, as_of=AS_OF)
    assert lit.protected == "yes" and lit.paid == "unpaid"
    rehab = hr.leave_request_check("alcohol rehab", 25, as_of=AS_OF)
    assert "accrued sick leave" in rehab.paid
    donor = hr.leave_request_check("bone marrow", 10, as_of=AS_OF)
    assert donor.protected == "check" and "1508-1511" in donor.next_step
    t = hr.leave_request_check("teacher request", 2, as_of=AS_OF)
    assert t.protected == "yes" and t.employee_must == ["reasonable notice before taking the time off"]


def test_lactation_gc_two_business_days():
    # hr-lactation-multiemployer-worksite-2-business-days: Friday 2026-11-27 + 2 business days = Tuesday 2026-12-01.
    r = hr.leave_request_check("pumping", 30, subcontractor_written_request=True, request_date="2026-11-27",
                               as_of=AS_OF)
    d = deadline(r, "GC: provide lactation space")
    assert (d.due_date, d.weekday) == ("2026-12-01", "Tuesday")
    assert any("Under 50 employees" in n for n in r.notes)            # 30 < 50 undue-hardship rule
    assert "lactation-accommodation" in ids(r) and r.verified is True


def test_fmla_eligibility_and_notice():
    # hr-fmla-eligible-employee-12-months-1250-hours-75-miles: 14 >= 12 months, 1,400 >= 1,250 h, 80 >= 50 nearby;
    # covered employer: 52 >= 20 workweeks. hr-fmla-12-workweeks: 12 - 8 used = 4 left; 6 requested -> 4 protected.
    # hr-fmla-notices-5-business-days: Wed 2026-11-25 + 5 business days = Wed 2026-12-02 (holidays not skipped).
    r = hr.leave_request_check("FMLA", 120, months_employed=14, hours_worked_last_12_months=1400,
                               employees_within_75_miles=80, fmla_covered_workweeks=52, weeks_requested=6,
                               weeks_used_last_12_months=8, request_date=AS_OF, as_of=AS_OF)
    assert (r.protected, r.remaining, r.can_approve_now) == ("yes", 4.0, 4.0)
    assert deadline(r, "Give the FMLA eligibility notice").due_date == "2026-12-02"
    assert r.verified is True
    # 1,249 hours -> fails the hours test.
    short = hr.leave_request_check("fmla", 120, months_employed=14, hours_worked_last_12_months=1249,
                                   employees_within_75_miles=80, fmla_covered_workweeks=52, as_of=AS_OF)
    assert short.protected == "no" and "hours in the last 12 months" in short.next_step
    # Missing facts -> check, with the notice clock still running.
    chk = hr.leave_request_check("medical leave", 120, as_of=AS_OF)
    assert chk.protected == "check" and chk.employer_covered is None and chk.deadlines
    # Under 50 employees (and no 20-workweek history given) -> not covered, no notice deadline.
    small = hr.leave_request_check("fmla", 40, months_employed=24, hours_worked_last_12_months=2000,
                                   employees_within_75_miles=40, as_of=AS_OF)
    assert small.protected == "no" and small.employer_covered is False and small.deadlines == []
    # Military caregiver: 26 - 8 = 18 workweeks.
    mil = hr.leave_request_check("military caregiver", 120, weeks_used_last_12_months=8, as_of=AS_OF)
    assert mil.remaining == 18.0 and "hr-fmla-26-workweeks-military-caregiver" in ids(mil)


@pytest.mark.parametrize("text,key", [
    ("jury duty summons", "jury_duty"), ("Teacher asked me to come to school", "school_teacher_request"),
    ("reserve peace officer training", "emergency_training"), ("fire call", "emergency_duty"),
    ("Kidney donor surgery", "organ_donor"), ("baby bonding", "fmla"), ("LACTATION_ROOM", "lactation"),
])
def test_leave_type_aliases(text, key):
    assert hr.normalize_leave_type(text) == key


@pytest.mark.parametrize("text", ["harassment training", "blood donor", "vacation", ""])
def test_leave_type_rejects_lookalikes(text):
    # A bare 'training' or 'donor' is not one of these protections (organ/bone marrow donors only).
    with pytest.raises(ValueError, match="leave_type must be one of"):
        hr.normalize_leave_type(text)


@pytest.mark.parametrize("text,key", [("Reduction in force", "termination"), ("RIF", "termination"),
                                      ("hours cut", "reduction_in_hours"), ("Aged out", "dependent_child_loss"),
                                      ("legal separation", "divorce_or_legal_separation")])
def test_cobra_event_aliases(text, key):
    assert hr.normalize_cobra_event(text) == key


def test_leave_bad_inputs():
    with pytest.raises(ValueError, match="leave_type must be one of"):
        hr.leave_request_check("vacation", 10)
    with pytest.raises(ValueError, match="hours_used_this_month cannot exceed hours_used_this_year"):
        hr.leave_request_check("school", 40, hours_used_this_year=2, hours_used_this_month=4)
    with pytest.raises(ValueError, match="cannot exceed employee_count"):
        hr.leave_request_check("school", 10, employees_at_location=12)
    with pytest.raises(ValueError, match="cannot exceed employee_count"):
        hr.leave_request_check("fmla", 60, employees_within_75_miles=61)
    with pytest.raises(ValueError, match="8,784"):
        hr.leave_request_check("fmla", 60, hours_worked_last_12_months=9000)
    with pytest.raises(ValueError, match="employee_count must be a whole number of at least 1"):
        hr.leave_request_check("jury", 0)


# ================================================================ rulebook linkage
def test_every_rule_id_used_exists_in_the_rulebook():
    used = {v for k, v in vars(hr).items() if k.isupper() and isinstance(v, str) and "-" in v}
    for rid in used:
        core.rule(rid)                              # raises KeyError for an unknown id
    assert {"hr-state-minimum-wage-2027", "hr-local-mw-san-diego-2027", "hr-aca-ale-definition-50-ftes"} <= used


# ================================================================ MCP
def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    hr.register(mcp)

    async def run():
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
            wage = await c.call_tool("jobsite_minimum_wage", {"city": "City of San Diego", "on_date": AS_OF,
                                                              "hours_in_city_this_week": "40",
                                                              "hourly_rate_paid_usd": "17.25"})
            size = await c.call_tool("employer_size_mandates", {"employee_count": "28",
                                                                "employees_at_largest_location": 26,
                                                                "employees_in_san_diego_city": 12, "as_of": AS_OF})
            aca = await c.call_tool("aca_employer_status", {"measurement_year": 2025,
                                                            "employee_hours": json.loads(json.dumps(crew(ft=25, pt=40))),
                                                            "as_of": AS_OF})
            cobra = await c.call_tool("cobra_deadlines", {"qualifying_event": "Laid off", "event_date": "11/13/2026",
                                                          "monthly_plan_cost_usd": 850, "as_of": AS_OF})
            leave = await c.call_tool("leave_request_check", {"leave_type": "School event", "employee_count": 40,
                                                              "employees_at_location": 30, "hours_requested": 6,
                                                              "hours_used_this_year": 36, "as_of": AS_OF})
            bad = await c.call_tool("jobsite_minimum_wage", {"city": "San Diego", "on_date": "2030-01-01"},
                                    raise_on_error=False)
            return tools, [x.structured_content for x in (wage, size, aca, cobra, leave)], bad
    tools, (wage, size, aca, cobra, leave), bad = asyncio.run(run())
    assert set(tools) == {"jobsite_minimum_wage", "employer_size_mandates", "aca_employer_status", "cobra_deadlines",
                          "leave_request_check"}
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.open_world_hint is False
    # Descriptions carry the rulebook numbers (built from params, not typed in).
    assert "2+ hours in a week" in tools["jobsite_minimum_wage"].description
    assert "1,250 hours" in tools["leave_request_check"].description
    assert wage["required_hourly_usd"] == 17.75 and wage["pay_check"]["shortfall_usd"] == 20.0   # 0.50 x 40
    assert size["san_diego_enforcement_fee_usd"] == 17.64 and size["verified"] is False          # 1.47 x 12
    assert aca["applicable_large_employer"] is True and aca["average"] == 55.0                    # 25 + 30 FTEs
    assert cobra["max_monthly_premium_usd"] == 867.0 and cobra["coverage_may_end"] == "2028-05-13"
    assert leave["can_approve_now"] == 4.0                                                        # 40 - 36
    assert all(r["disclaimer"] == core.DISCLAIMER for r in (wage, size, aca, cobra, leave))
    assert bad.is_error and "no verified California minimum wage" in bad.content[0].text
