"""Payroll module: hand-computed California Wage Order 16 / Labor Code scenarios for construction crews."""
import asyncio
from datetime import date, timedelta

import pytest
from fastmcp import Client, FastMCP

from tradecomply import core, payroll as p

AS_OF = "2026-11-25"
MON = date(2026, 11, 2)            # Monday


def day(emp, d, start, end, meals=(), rest=2, **kw):   # rest=None: rest breaks not recorded
    return dict(employee=emp, date=str(d), start=start, end=end, rest_breaks_taken=rest,
                meal_breaks=[{"start": a, "end": b} for a, b in meals], **kw)


def one(emp, rate, *recs, **kw):
    r = p.check_payroll(list(recs), {emp: rate}, as_of=AS_OF, **kw)
    return r, r.employees[0]


def exposure(result, prefix):
    return next(x for x in result.exposure if x.item.startswith(prefix))


# ---------------------------------------------------------------- daily work
def test_compliant_8_hour_day_is_clean_and_verified():
    # 06:00-14:30 minus 30-min lunch = 8.0 h; lunch starts after 5.0 h (on time); 8 h needs 2 rest breaks.
    r, e = one("Ana", 30, day("Ana", MON, "06:00", "14:30", [("11:00", "11:30")]))
    d = e.days[0]
    assert (d.hours_worked, d.regular_hours, d.overtime_hours, d.double_time_hours) == (8.0, 8.0, 0.0, 0.0)
    assert e.wages_due_usd == 240.0                          # 8 x 30
    assert d.meal_issues == [] and d.rest_breaks_required == 2 and e.premiums_usd == 0
    assert e.amount_owed_usd == 0 and r.exposure == [] and r.exposure_high_usd == 0
    assert r.verified is True
    ids = {c.rule_id for c in r.citations}
    assert {"daily-weekly-overtime-1-5x", "first-meal-period-5-hours", "rest-period-10-minutes-per-4-hours",
            "ca-minimum-wage-2026"} <= ids


def test_long_day_late_meal_no_second_meal_missed_rest():
    # 05:00-19:00 minus 30 min = 13.5 h -> 8 regular, 4 at 1.5x (8-12), 1.5 at 2x (>12).
    # Lunch at 11:30 = after 6.5 h worked (late). No second meal on a 13.5 h day (>12 h, not waivable).
    # Rest: floor(13.5/4)=3, remainder 1.5 is not a major fraction -> 3 required, 1 taken.
    r, e = one("Ben", 40, day("Ben", MON + timedelta(1), "05:00", "19:00", [("11:30", "12:00")], rest=1))
    d = e.days[0]
    assert (d.regular_hours, d.overtime_hours, d.double_time_hours) == (8.0, 4.0, 1.5)
    assert e.wages_due_usd == 680.0                          # 8x40 + 4x60 + 1.5x80 = 320+240+120
    assert len(d.meal_issues) == 2 and d.meal_premium_hours == 1     # two violations, ONE premium per workday
    assert d.rest_breaks_required == 3 and d.rest_premium_hours == 1
    assert e.premiums_usd == 80.0                            # (1 meal + 1 rest) x $40
    assert e.overtime_premium_usd == 140.0                   # 4x0.5x40 + 1.5x1x40 = 80+60
    # Exposure (1 weekly pay period, owed $80):
    #   low  = 80 + LC210 $100 + LC558 $50                        = 230
    #   high = 80 + (200 + 25% x 80 = 220) + 100 + LC203 (40 x 8 h x 30 = 9,600) + 226(e) $50 = 10,050
    assert r.exposure_low_usd == 230.0 and r.exposure_high_usd == 10050.0
    assert exposure(r, "LC 203").high_usd == 9600.0
    assert r.verified is False                               # 226(e) rule is unverifiable
    r2, _ = one("Ben", 40, day("Ben", MON + timedelta(1), "05:00", "19:00", [("11:30", "12:00")], rest=1),
                include_unverified_penalties=False)
    assert r2.exposure_high_usd == 10000.0 and r2.verified is True
    assert any("overtime-payment-deadline" in f for f in r.fixes)


# ---------------------------------------------------------------- weekly / 7th day
def carla_week():
    # Mon-Fri 06:00-16:30 with lunch = 10 h each; Saturday 06:00-12:00 = 6 h, meal waived (day <= 6 h).
    recs = [day("Carla", MON + timedelta(i), "06:00", "16:30", [("11:00", "11:30")]) for i in range(5)]
    return recs + [day("Carla", MON + timedelta(5), "06:00", "12:00", rest=1, first_meal_waived=True)]


def test_weekly_overtime_does_not_double_count_daily_overtime():
    # Daily: 5 x (8 reg + 2 OT) = 40 reg + 10 OT. Saturday's 6 h would be regular, but straight time already hit 40,
    # so all 6 move to 1.5x. Totals 40 reg / 16 OT (not 50+ OT). 40x25 + 16x37.50 = 1,000 + 600 = 1,600.
    r, e = one("Carla", 25, *carla_week())
    assert (e.regular_hours, e.overtime_hours, e.double_time_hours) == (40.0, 16.0, 0.0)
    assert e.wages_due_usd == 1600.0 and e.premiums_usd == 0
    sat = e.days[-1]
    assert (sat.regular_hours, sat.overtime_hours) == (0.0, 6.0) and sat.meal_issues == []
    assert e.days[0].rest_breaks_required == 2               # 10 h: floor(10/4)=2, remainder 2 is not > 2


def test_gross_paid_straight_time_shows_real_shortfall():
    # Paid straight time 56 h x 25 = 1,400 vs 1,600 due -> owes 200.
    # LC 210 high = 200 + 25% x 200 = 250. LC 203 daily rate = 25 x avg(8,8,8,8,8,6)=7.67 h = 191.75 -> x30 = 5,752.50
    r, e = one("Carla", 25, *carla_week(), gross_paid_usd={"Carla": 1400})
    assert e.amount_owed_usd == 200.0 and e.pay_periods_affected == 1
    assert exposure(r, "LC 210").high_usd == 250.0
    assert exposure(r, "LC 203").high_usd == 5752.5


def dan_week():
    # Mon-Sat 8 h days (06:00-14:30, lunch 11:00), Sunday 10 h (06:00-16:30, lunch 11:00).
    recs = [day("Dan", MON + timedelta(i), "06:00", "14:30", [("11:00", "11:30")]) for i in range(6)]
    return recs + [day("Dan", MON + timedelta(6), "06:00", "16:30", [("11:00", "11:30")])]


def test_seventh_consecutive_day_in_monday_workweek():
    # Mon-Fri 40 reg; Sat 8 h -> weekly OT (8 at 1.5x); Sunday = 7th consecutive day: first 8 at 1.5x, 2 at 2x.
    # 40x30 + 16x45 + 2x60 = 1,200 + 720 + 120 = 2,040
    r, e = one("Dan", 30, *dan_week())
    assert (e.regular_hours, e.overtime_hours, e.double_time_hours) == (40.0, 16.0, 2.0)
    assert e.wages_due_usd == 2040.0 and e.days[-1].seventh_consecutive_day
    assert "one-day-rest-in-seven" in {c.rule_id for c in r.citations}


def test_same_days_split_across_sunday_workweeks_have_no_seventh_day():
    # Sunday start: week 1 = Sun 11/1-Sat 11/7 holds Mon-Sat (40 reg + 8 weekly OT); Sunday 11/8 opens week 2
    # (8 reg + 2 OT). 48x30 + 10x45 = 1,440 + 450 = 1,890
    r, e = one("Dan", 30, *dan_week(), workweek_starts_on="sunday")
    assert (e.regular_hours, e.overtime_hours, e.double_time_hours) == (48.0, 10.0, 0.0)
    assert e.wages_due_usd == 1890.0 and not any(d.seventh_consecutive_day for d in e.days)


# ---------------------------------------------------------------- meal and rest edge cases
def test_meal_waiver_only_for_days_of_six_hours_or_less():
    # 5.5 h with no meal: violation unless waived; 7 h with a "waiver": waiver invalid -> violation.
    _, e = one("Ivy", 30, day("Ivy", MON, "06:00", "11:30", rest=1))
    assert e.days[0].meal_premium_hours == 1 and "mutual waiver" in e.days[0].meal_issues[0]
    _, e = one("Ivy", 30, day("Ivy", MON, "06:00", "11:30", rest=1, first_meal_waived=True))
    assert e.days[0].meal_issues == [] and e.premiums_usd == 0
    _, e = one("Ivy", 30, day("Ivy", MON, "06:00", "13:00", rest=2, first_meal_waived=True))
    assert e.days[0].meal_premium_hours == 1 and "not allowed" in e.days[0].meal_issues[0]
    # Exactly 5.0 h: no meal required; rest floor(5/4)=1, remainder 1 -> 1 break.
    _, e = one("Ivy", 30, day("Ivy", MON, "06:00", "11:00", rest=1))
    assert e.days[0].meal_issues == [] and e.days[0].rest_breaks_required == 1


def test_short_meal_does_not_count():
    # 20-minute lunch on an 8.33 h day: no compliant 30-minute meal -> premium.
    _, e = one("Jo", 30, day("Jo", MON, "06:00", "14:40", [("11:00", "11:20")]))
    assert e.days[0].hours_worked == 8.33 and e.days[0].meal_premium_hours == 1
    assert "20-minute break does not count" in e.days[0].meal_issues[0]


def test_second_meal_waiver_up_to_12_hours():
    # 06:00-18:00 with lunch = 11.5 h: second meal needed (>10 h) but waivable (<=12 h, first not waived).
    # Rest: floor(11.5/4)=2, remainder 3.5 > 2 -> 3 required.
    _, e = one("Kim", 30, day("Kim", MON, "06:00", "18:00", [("11:00", "11:30")], rest=3, second_meal_waived=True))
    d = e.days[0]
    assert d.meal_issues == [] and d.rest_breaks_required == 3 and (d.regular_hours, d.overtime_hours) == (8.0, 3.5)
    _, e = one("Kim", 30, day("Kim", MON, "06:00", "18:00", [("11:00", "11:30")], rest=3))
    assert e.days[0].meal_premium_hours == 1
    # 12.5 h: waiver not allowed; 0.5 h at double time.
    _, e = one("Kim", 30, day("Kim", MON, "06:00", "19:00", [("11:00", "11:30")], rest=3, second_meal_waived=True))
    assert e.days[0].meal_premium_hours == 1 and e.days[0].double_time_hours == 0.5


def test_second_meal_taken_on_time_is_compliant():
    # 05:00-17:00 with lunches 10:00-10:30 (after 5 h) and 15:00-15:30 (after 9.5 h) = 11 h worked.
    _, e = one("Lee", 30, day("Lee", MON, "05:00", "17:00", [("10:00", "10:30"), ("15:00", "15:30")], rest=3))
    assert e.days[0].hours_worked == 11.0 and e.days[0].meal_issues == [] and e.premiums_usd == 0


def test_on_call_breaks_are_paid_time_and_not_breaks():
    # On call during lunch: the 30 min is hours worked -> 8.5 h (0.5 OT); no duty-free meal; rest breaks don't count.
    # Wages 8x30 + 0.5x45 = 262.50; premiums 2 x 30 = 60.
    _, e = one("Frank", 30, day("Frank", MON, "06:00", "14:30", [("11:00", "11:30")], on_call=True))
    d = e.days[0]
    assert (d.hours_worked, d.overtime_hours, d.wages_due_usd) == (8.5, 0.5, 262.5)
    assert d.meal_premium_hours == 1 and d.rest_breaks_counted == 0 and d.rest_premium_hours == 1
    assert e.premiums_usd == 60.0


def test_rest_break_formula_boundaries():
    # minutes -> breaks: <3.5 h none; 3.5-6 -> 1; over 6-10 -> 2; over 10-14 -> 3
    cases = {204: 0, 210: 1, 360: 1, 361: 2, 600: 2, 601: 3, 840: 3, 841: 4}
    assert {m: p._rest_required(m) for m in cases} == cases


def test_lunch_punch_gap_counts_as_meal_and_split_shift_is_flagged_unverified():
    # Two punches 06:00-10:00 and 13:00-17:00 = 8 h; the 3 h unpaid gap is a valid meal (after 4 h) but may be a
    # split shift. No split-shift rule exists in the rulebook -> flagged, verified=False, no premium invented.
    r = p.check_payroll([day("Gus", MON, "06:00", "10:00", rest=1), day("Gus", MON, "13:00", "17:00", rest=1)],
                        {"Gus": 30}, as_of=AS_OF)
    e = r.employees[0]
    assert e.total_hours == 8.0 and e.days[0].meal_issues == [] and e.premiums_usd == 0
    assert any("split shift" in x for x in e.days[0].explanation) and r.verified is False
    # A normal 30-minute lunch punch is not flagged.
    r = p.check_payroll([day("Gus", MON, "06:00", "11:00", rest=1), day("Gus", MON, "11:30", "14:30", rest=1)],
                        {"Gus": 30}, as_of=AS_OF)
    assert r.verified is True and r.employees[0].total_hours == 8.0


def test_overnight_shift_counts_in_start_day():
    # Night paving 21:00-05:30 with lunch 01:00-01:30 = 8 h; lunch after 4 h worked.
    _, e = one("Hal", 35, day("Hal", MON, "21:00", "05:30", [("01:00", "01:30")]))
    assert e.total_hours == 8.0 and e.days[0].meal_issues == [] and e.wages_due_usd == 280.0


# ---------------------------------------------------------------- minimum wage
def test_minimum_wage_shortfall_state_and_local():
    # $16.00 rate, 8 h. State 2026 $16.90 -> due 135.20, shortfall 0.90x8 = 7.20.
    rec = day("Eva", MON, "07:00", "15:30", [("11:00", "11:30")])
    _, e = one("Eva", 16.0, rec)
    assert e.wages_due_usd == 135.2 and e.minimum_wage_shortfall_usd == 7.2
    # Local $18.00 overrides: due 144, shortfall 2x8 = 16.
    r, e = one("Eva", 16.0, rec, local_minimum_wage_usd=18.0)
    assert e.wages_due_usd == 144.0 and e.minimum_wage_shortfall_usd == 16.0 and e.amount_owed_usd == 16.0
    assert any("$18.00" in f for f in e.fixes)
    assert "regular-rate-of-pay-calculation" not in {c.rule_id for c in r.citations}
    assert core.rule("ca-minimum-wage-2026").param("hourly_usd") == 16.9


def test_overtime_on_sub_minimum_rate_uses_minimum_wage_and_is_unverified():
    # 10 h at $16 listed: due 8x16.90 + 2x25.35 = 135.20 + 50.70 = 185.90; listed 8x16 + 2x24 = 176 -> 9.90 short.
    r, e = one("Eva", 16.0, day("Eva", MON, "06:00", "16:30", [("11:00", "11:30")]), include_unverified_penalties=False)
    assert e.wages_due_usd == 185.9 and e.minimum_wage_shortfall_usd == 9.9
    assert "regular-rate-of-pay-calculation" in {c.rule_id for c in r.citations} and r.verified is False


def test_2025_entry_uses_2025_minimum_wage():
    # 2025 rate $16.50 from the rulebook's prior rates: 8 h x (16.50-16.00) = 4.00 short.
    _, e = one("Eva", 16.0, day("Eva", "2025-06-02", "07:00", "15:30", [("11:00", "11:30")]))
    assert e.wages_due_usd == 132.0 and e.minimum_wage_shortfall_usd == 4.0


# ---------------------------------------------------------------- pay periods, public works, CSV
def test_pay_periods_drive_per_period_penalties():
    # Ben misses meal+rest on two Tuesdays one week apart: owed 2 x 80 = 160.
    recs = [day("Ben", MON + timedelta(k), "05:00", "19:00", [("11:30", "12:00")], rest=1) for k in (1, 8)]
    r = p.check_payroll(recs, {"Ben": 40}, as_of=AS_OF)                         # weekly: 2 periods
    assert r.employees[0].pay_periods_affected == 2
    assert exposure(r, "LC 210").low_usd == 200.0 and exposure(r, "LC 210").high_usd == 440.0   # 2x200 + 25%x160
    assert exposure(r, "LC 558").high_usd == 200.0
    assert exposure(r, "LC 226(e)").high_usd == 150.0                                       # 50 + 100
    r = p.check_payroll(recs, {"Ben": 40}, as_of=AS_OF, pay_frequency="biweekly")         # one period
    assert r.employees[0].pay_periods_affected == 1 and exposure(r, "LC 210").low_usd == 100.0


def test_semimonthly_periods_split_on_the_15th():
    # LC 204 semimonthly periods: 1st-15th and 16th-end. 11/13 and 11/16 fall in two periods; 11/13-11/14 in one.
    recs = lambda *ds: [day("Ben", d, "05:00", "19:00", [("11:30", "12:00")], rest=1) for d in ds]
    r = p.check_payroll(recs("2026-11-13", "2026-11-16"), {"Ben": 40}, as_of=AS_OF, pay_frequency="semimonthly")
    assert r.employees[0].pay_periods_affected == 2
    r = p.check_payroll(recs("2026-11-13", "2026-11-14"), {"Ben": 40}, as_of=AS_OF, pay_frequency="semimonthly")
    assert r.employees[0].pay_periods_affected == 1


def test_public_works_adds_lc_1813_penalty():
    # 13.5 h day on a public works job -> 1 worker-day over 8 h -> $25.
    r, _ = one("Ben", 40, day("Ben", MON, "05:00", "19:00", [("11:30", "12:00")], rest=1), public_works=True)
    assert exposure(r, "LC 1813").high_usd == 25.0
    assert {"pw-overtime-8-40-time-and-half", "penalty-1813-overtime"} <= {c.rule_id for c in r.citations}


def test_csv_timeclock_export_with_lunch_columns_and_rates():
    text = ("Employee,Date,In,Lunch Out,Lunch In,Out,Rest Breaks,Hourly Rate\n"
            "Ana Lopez,11/02/2026,6:00 AM,11:00 AM,11:30 AM,2:30 PM,2,$32.50\n"
            "Ben Ortiz,11/02/2026,5:00 AM,,,7:00 PM,1,40\n")
    r = p.check_payroll(text, as_of=AS_OF)
    ana, ben = r.employees
    assert ana.wages_due_usd == 260.0                         # 8 x 32.50
    assert ben.total_hours == 14.0 and ben.days[0].meal_premium_hours == 1
    # Ben: 8x40 + 4x60 + 2x80 = 320 + 240 + 160 = 720; premiums (meal+rest) 80
    assert ben.wages_due_usd == 720.0 and ben.premiums_usd == 80.0
    assert r.total_premiums_owed_usd == 80.0


def test_csv_meal_breaks_column():
    text = "employee,date,start,end,meal_breaks,rest_breaks_taken\nLee,2026-11-02,05:00,17:00,10:00-10:30;15:00-15:30,3\n"
    r = p.check_payroll(text, {"Lee": 30}, as_of=AS_OF)
    assert r.employees[0].total_hours == 11.0 and r.employees[0].premiums_usd == 0


def test_bad_inputs_raise_clear_errors():
    with pytest.raises(ValueError, match="No hourly rate"):
        p.check_payroll([day("Zed", MON, "06:00", "14:30")], {}, as_of=AS_OF)
    with pytest.raises(ValueError, match="overlap"):
        p.check_payroll([day("Zed", MON, "06:00", "10:00"), day("Zed", MON, "09:00", "12:00")], {"Zed": 30})
    with pytest.raises(ValueError, match="outside"):
        p.check_payroll([day("Zed", MON, "06:00", "10:00", [("10:30", "11:00")])], {"Zed": 30})
    with pytest.raises(ValueError, match="missing column"):
        p.check_payroll("name,day\nZed,2026-11-02\n", {"Zed": 30})
    with pytest.raises(ValueError, match="clock time"):
        p.check_payroll([day("Zed", MON, "six", "14:30")], {"Zed": 30})


# ---------------------------------------------------------------- final paycheck
def test_discharge_paid_four_days_late():
    # Due immediately on 11/20; paid 11/24 = 4 days late x $256 (8 h x $32) = $1,024; max 30 x 256 = $7,680.
    r = p.final_paycheck_deadline("discharged", "2026-11-20", 256, paid_date="2026-11-24", as_of=AS_OF)
    assert r.due_date == "2026-11-20" and r.status == "paid_late" and r.days_late == 4
    assert r.penalty_accrued_usd == 1024.0 and r.max_penalty_usd == 7680.0 and r.verified is True


def test_quit_without_notice_has_72_hours():
    # Quit Fri 11/20 15:00 -> due Mon 11/23 15:00; unpaid as of 11/25 -> 2 days x 256 = 512 and growing.
    r = p.final_paycheck_deadline("quit_without_notice", "2026-11-20", 256, separation_time="15:00", as_of=AS_OF)
    assert r.due_date == "2026-11-23" and "2026-11-23 15:00" in r.due_rule
    assert r.status == "late_unpaid" and r.penalty_accrued_usd == 512.0


def test_quit_with_notice_due_last_day_and_not_yet_due():
    r = p.final_paycheck_deadline("quit_with_72h_notice", "2026-11-27", 300, as_of=AS_OF)
    assert r.due_date == "2026-11-27" and r.status == "not_yet_due" and r.penalty_accrued_usd == 0
    r = p.final_paycheck_deadline("laid_off", "2026-11-25", 300, paid_date="2026-11-25", as_of=AS_OF)
    assert r.status == "paid_on_time" and "Labor Code 208" in r.place_of_payment


def test_waiting_time_penalty_caps_at_30_days():
    # Discharged 9/1, still unpaid 11/25 = 85 days late -> capped at 30 x 256 = 7,680.
    r = p.final_paycheck_deadline("discharged", "2026-09-01", 256, as_of=AS_OF)
    assert r.days_late == 85 and r.penalty_accrued_usd == 7680.0 and r.status == "late_unpaid_max_reached"
    assert r.penalty_stops_growing_on == "2026-10-01"


# ---------------------------------------------------------------- sick leave
def test_sick_leave_accrual_balance_and_use_cap():
    # Hired 1/5/2026, 1,500 h worked -> 1,500/30 = 50 h accrued; 16 used -> 34 balance.
    # Use cap 40 h/yr -> 40-16 = 24 usable. Usable from the 90th day: 1/5 + 89 days = 4/4/2026.
    r = p.sick_leave_status(1500, 16, "2026-01-05", as_of=AS_OF)
    assert (r.accrued_hours, r.balance_hours, r.usable_now_hours) == (50.0, 34.0, 24.0)
    assert r.usable_from == "2026-04-04" and r.can_use_now and r.days_employed == 325 and r.verified is True


def test_sick_leave_new_hire_waits_until_90th_day():
    # Hired 10/1/2026: day 56 on 11/25; 90th day = 12/29/2026. 100 h -> 3.33 h accrued but not usable yet.
    r = p.sick_leave_status(100, 0, "2026-10-01", as_of=AS_OF)
    assert r.days_employed == 56 and not r.can_use_now and r.usable_from == "2026-12-29"
    assert r.accrued_hours == 3.33 and r.usable_now_hours == 0


def test_sick_leave_caps_scale_with_10_hour_shifts():
    # 4x10 crew: use minimum/cap = max(40, 5x10) = 50; accrual cap = max(80, 10x10) = 100.
    # 3,000 h -> 100 accrued + 20 carryover - 0 used = 120 -> capped at 100.
    r = p.sick_leave_status(3000, 0, "2025-01-06", as_of=AS_OF, normal_shift_hours=10, carryover_hours=20)
    assert (r.use_cap_hours, r.annual_use_minimum_hours, r.accrual_cap_hours) == (50.0, 50.0, 100.0)
    assert r.balance_hours == 100.0 and r.at_accrual_cap and r.usable_now_hours == 50.0
    # 8 h shifts: cap 80
    r = p.sick_leave_status(3000, 0, "2025-01-06", as_of=AS_OF)
    assert r.balance_hours == 80.0 and r.use_cap_hours == 40.0


def test_sick_leave_frontload_new_hire_milestones():
    # Hired 8/1/2026 -> day 117 on 11/25. New-hire frontload minimum: 24 h by day 120 (11/28), 40 h by day 200.
    r = p.sick_leave_status(0, 0, "2026-08-01", as_of=AS_OF, plan="frontload", frontloaded_hours=40)
    assert r.days_employed == 117 and "2026-11-28" in r.next_milestone and r.usable_now_hours == 40.0
    # Day 210 with only 24 h frontloaded: below the 40 h required by day 200.
    r = p.sick_leave_status(0, 0, "2026-04-30", as_of=AS_OF, plan="frontload", frontloaded_hours=24)
    assert r.days_employed == 210 and any("below the 40 h" in n for n in r.notes)


# ---------------------------------------------------------------- reviewer fixes (California law accuracy)
def test_waiver_flag_with_a_compliant_meal_is_not_a_violation():
    # 06:00-13:30 with lunch 11:00-11:30 = 7.0 h; meal starts after 5.0 h (on time). The "waived" flag is wrong for a
    # 7 h day, but the meal WAS provided, so no premium is owed (LC 226.7 premium only when the meal is not provided).
    _, e = one("Ivy", 30, day("Ivy", MON, "06:00", "13:30", [("11:00", "11:30")], first_meal_waived=True))
    assert e.days[0].hours_worked == 7.0 and e.days[0].meal_issues == [] and e.premiums_usd == 0


def test_on_call_meal_on_short_day_owes_no_meal_premium():
    # 07:00-11:30 with an on-call "lunch" 09:00-09:30: all 4.5 h are paid work (meal counted as work). 4.5 h is not
    # more than 5 h, so no meal is required -> no meal premium. Rest: 4.5 h needs 1, on call counts 0 -> 1 rest premium.
    _, e = one("Frank", 30, day("Frank", MON, "07:00", "11:30", [("09:00", "09:30")], rest=1, on_call=True))
    d = e.days[0]
    assert d.hours_worked == 4.5 and d.meal_issues == [] and d.meal_premium_hours == 0
    assert d.rest_premium_hours == 1 and e.premiums_usd == 30.0             # 1 x $30
    # On-call 8.5 h day: meal premium owed, and the on-duty meal exception (unchecked rule) is cited -> unverified.
    r, e = one("Frank", 30, day("Frank", MON, "06:00", "14:30", [("11:00", "11:30")], on_call=True))
    assert e.days[0].meal_premium_hours == 1 and "written, revocable" in e.days[0].meal_issues[0]
    assert "on-duty-meal-written-agreement" in {c.rule_id for c in r.citations} and r.verified is False


def test_unrecorded_rest_breaks_are_not_violations():
    # 05:00-19:00, lunch 11:30-12:00 = 13.5 h; rest breaks not recorded (WO16 does not require recording them).
    # Meal premium only: 1 x $40 = $40 (late first meal + no second meal = ONE premium). No rest premium.
    _, e = one("Rob", 40, day("Rob", MON, "05:00", "19:00", [("11:30", "12:00")], rest=None))
    d = e.days[0]
    assert d.rest_breaks_required == 3 and d.rest_breaks_counted is None and d.rest_premium_hours == 0
    assert e.premiums_usd == 40.0 and any("not recorded" in x for x in d.explanation)
    # Timeclock export with no rest column: clean 8 h day, no premiums, verified, note explains.
    text = ("Employee,Date,In,Lunch Out,Lunch In,Out,Rate\n"
            "Ana Lopez,11/02/2026,6:00 AM,11:00 AM,11:30 AM,2:30 PM,32.50\n")
    r = p.check_payroll(text, as_of=AS_OF)
    assert r.total_premiums_owed_usd == 0 and r.verified is True
    assert any("Rest breaks were not recorded for 1 workday" in n for n in r.notes)


def test_start_equal_to_end_is_rejected_not_a_24_hour_shift():
    with pytest.raises(ValueError, match="start and end are both"):
        p.check_payroll([day("Zed", MON, "06:00", "06:00")], {"Zed": 30}, as_of=AS_OF)


def test_long_meal_inside_one_entry_is_screened_for_split_shift():
    # 06:00-15:30 with a 90-minute lunch 10:00-11:30 = 8.0 h; lunch after 4 h is a valid meal, but an unpaid break
    # over 60 min gets the same (unverified) split-shift flag as two separate punches.
    r, e = one("Gus", 30, day("Gus", MON, "06:00", "15:30", [("10:00", "11:30")]))
    assert e.total_hours == 8.0 and e.premiums_usd == 0
    assert any("possible split shift" in x for x in e.days[0].explanation) and r.verified is False


def test_short_unpaid_gap_is_flagged_as_possible_unpaid_rest_break():
    # Punches 06:00-10:00 and 10:15-14:30 = 4 + 4.25 = 8.25 h; the 15-min unpaid gap is not a meal (<30 min), so
    # there is no meal on an 8.25 h day -> 1 meal premium ($30); 0.25 h at 1.5x. The gap is flagged: rest is paid time.
    r = p.check_payroll([day("Gus", MON, "06:00", "10:00", rest=1), day("Gus", MON, "10:15", "14:30", rest=1)],
                        {"Gus": 30}, as_of=AS_OF)
    d = r.employees[0].days[0]
    assert (d.hours_worked, d.overtime_hours, d.meal_premium_hours) == (8.25, 0.25, 1)
    assert any("15 min is too short" in x and "must be paid" in x for x in d.explanation)


def test_monthly_pay_is_flagged_and_weekly_pay_deadline_noted():
    rec = day("Ana", MON, "06:00", "14:30", [("11:00", "11:30")])
    r, _ = one("Ana", 30, rec, pay_frequency="monthly")
    assert any("Monthly pay is not allowed" in f for f in r.fixes)
    assert "semimonthly-paydays-26th-and-10th" in {c.rule_id for c in r.citations}
    r, _ = one("Ana", 30, rec)                                   # weekly: pay within 7 calendar days
    assert any("within 7 calendar days" in n for n in r.notes) and r.verified is True
    assert "weekly-biweekly-payroll-7-days" in {c.rule_id for c in r.citations}


def test_minimum_wage_liquidated_damages_in_high_exposure_only_when_unverified_included():
    # $16.00 for 8 h vs $16.90: shortfall 0.90 x 8 = 7.20 (1 weekly period).
    #   low  = 7.20 + LC210 100 + LC558 50                                            = 157.20
    #   high = 7.20 + (200 + 25% x 7.20 = 201.80) + 100 + LC203 (16.90 x 8 = 135.20 x 30 = 4,056)
    #          + 226(e) 50 + LC 1194.2 liquidated damages 1 x 7.20                       = 4,422.20
    rec = day("Eva", MON, "07:00", "15:30", [("11:00", "11:30")])
    r, _ = one("Eva", 16.0, rec)
    assert r.exposure_low_usd == 157.2 and r.exposure_high_usd == 4422.2
    assert exposure(r, "LC 1194.2").high_usd == 7.2 and r.verified is False
    r, _ = one("Eva", 16.0, rec, include_unverified_penalties=False)
    assert r.exposure_high_usd == 4365.0                         # 4,422.20 - 50 - 7.20
    assert not any(x.item.startswith("LC 1194.2") for x in r.exposure)
    # Overtime is not minimum wage: 10 h at $16 -> shortfall 9.90 (incl. OT) but liquidated damages on 0.90 x 10 = 9.00
    r, e = one("Eva", 16.0, day("Eva", MON, "06:00", "16:30", [("11:00", "11:30")]))
    assert e.minimum_wage_shortfall_usd == 9.9 and exposure(r, "LC 1194.2").high_usd == 9.0
    assert any("apprentice" in f for f in e.fixes)
    # Local $18.00 ordinance: shortfall 2.00 x 8 = 16.00 is owed, but LC 1194.2 covers the STATE minimum only:
    # (16.90 - 16.00) x 8 = 7.20.
    r, e = one("Eva", 16.0, rec, local_minimum_wage_usd=18.0)
    assert e.minimum_wage_shortfall_usd == 16.0 and exposure(r, "LC 1194.2").high_usd == 7.2


def test_union_scale_note_on_public_works_keeps_public_works_overtime():
    r, _ = one("Ben", 40, day("Ben", MON, "05:00", "19:00", [("11:30", "12:00")], rest=1), public_works=True)
    assert any("Wage Order 16 only" in n for n in r.notes)
    assert "8 h/day or 40 h/week" in exposure(r, "LC 1813").math


def test_as_of_2027_cba_floor_cites_cpi_rule():
    # CBA floor uses the as-of year's state rate; 2027 is not in the rulebook -> CPI rule cited -> unverified.
    r = p.check_payroll([day("Ana", MON, "06:00", "14:30", [("11:00", "11:30")])], {"Ana": 30}, as_of="2027-02-01")
    assert "minimum-wage-annual-cpi-adjustment" in {c.rule_id for c in r.citations} and r.verified is False


def test_final_pay_place_for_quit_with_notice_and_layoff_notes():
    r = p.final_paycheck_deadline("quit_with_72h_notice", "2026-11-20", 300, paid_date="2026-11-20", as_of=AS_OF)
    assert r.status == "paid_on_time" and "county" in r.place_of_payment and "Labor Code 208" in r.place_of_payment
    assert any("premiums" in x for x in r.must_include) and r.verified is True
    r = p.final_paycheck_deadline("laid_off", "2026-11-20", 300, paid_date="2026-11-20", as_of=AS_OF)
    assert any("oil drilling" in n and "24 hours" in n for n in r.notes)
    assert "final-pay-oil-drilling-layoff-24-hours" in {c.rule_id for c in r.citations}


def test_sick_leave_not_usable_until_covered_even_after_90th_day():
    # Hired 8/1/2026; 90th day = 8/1 + 89 = 10/29/2026. As of 11/25 (day 117) but only 20 days actually worked:
    # not covered (needs 30), so nothing is usable. 160 h / 30 = 5.33 h accrued.
    r = p.sick_leave_status(160, 0, "2026-08-01", as_of=AS_OF, days_worked=20)
    assert r.usable_from == "2026-10-29" and not r.covered and not r.can_use_now
    assert r.accrued_hours == 5.33 and r.usable_now_hours == 0
    r = p.sick_leave_status(160, 0, "2026-08-01", as_of=AS_OF, days_worked=30)
    assert r.covered and r.can_use_now and r.usable_now_hours == 5.33


# ---------------------------------------------------------------- MCP
def test_register_and_call_through_mcp_client():
    mcp = FastMCP("t")
    p.register(mcp)

    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
            pay = await c.call_tool("check_payroll", {
                "records": [day("Ben", MON + timedelta(1), "05:00", "19:00", [("11:30", "12:00")], rest=1)],
                "hourly_rates": {"Ben": 40}, "as_of": AS_OF})
            fin = await c.call_tool("final_paycheck_deadline", {"separation_type": "discharged",
                                                                "separation_date": "2026-11-20", "daily_wage_usd": 256,
                                                                "paid_date": "2026-11-24", "as_of": AS_OF})
            sick = await c.call_tool("sick_leave_status", {"hours_worked": 1500, "hours_used": 16,
                                                           "hire_date": "2026-01-05", "as_of": AS_OF})
            return names, pay.structured_content, fin.structured_content, sick.structured_content
    names, pay, fin, sick = asyncio.run(run())
    assert {"check_payroll", "final_paycheck_deadline", "sick_leave_status"} <= names
    assert pay["total_premiums_owed_usd"] == 80.0 and pay["employees"][0]["wages_due_usd"] == 680.0
    assert fin["penalty_accrued_usd"] == 1024.0 and sick["usable_now_hours"] == 24.0
