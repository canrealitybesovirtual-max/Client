"""California wage-and-hour checks for construction payroll (IWC Wage Order 16-2001 + Labor Code).

  check_payroll           time entries -> regular / overtime / double time, meal and rest premiums, minimum wage,
                          estimated penalty exposure and fixes, per employee and per workday
  final_paycheck_deadline when final wages are due (LC 201-202) and the waiting-time penalty if late (LC 203)
  sick_leave_status       paid sick leave accrual, use limit and caps (LC 246 as amended by SB 616)

Every legal number is read from the rulebook with core.rule(id).param(key) at call time; the two values with no
rule (split-shift screen, LC 1194.2 liquidated damages) live in UNVERIFIED and make the result verified=False.
Scope: nonexempt hourly employees, one hourly rate each, standard 8/40 schedule (no alternative workweek, no union
CBA exemption, no piece rate). Anything outside that is flagged in the notes, not computed. Rest breaks need not be
recorded, so a missing rest-break count never creates a premium; missing or late meals in the time records do.
"""
import csv
import io
import re
from collections import defaultdict
from datetime import date as Date, datetime, timedelta
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from . import core, logic

# ---------------------------------------------------------------- rule ids
MW, MW_LOCAL, MW_PERIOD, MW_CPI = ("ca-minimum-wage-2026", "local-minimum-wage-overrides-state",
                                   "minimum-wage-each-payroll-period-all-pay-bases", "minimum-wage-annual-cpi-adjustment")
OT, DT, REG_RATE = "daily-weekly-overtime-1-5x", "double-time-over-12-and-7th-day", "regular-rate-of-pay-calculation"
OT_DEADLINE, REST_DAY, CBA = "overtime-payment-deadline", "one-day-rest-in-seven", "cba-hours-meal-exemption-30-percent"
MEAL1, MEAL2, MEAL_PREM = "first-meal-period-5-hours", "second-meal-period-10-hours", "meal-period-premium-pay"
REST, REST_PREM = "rest-period-10-minutes-per-4-hours", "rest-period-premium-pay"
WAIT, LATE_PAY, WO16_PEN, STUB_PEN = ("waiting-time-penalty", "late-payment-penalty-210", "wage-order-16-civil-penalties",
                                      "wage-statement-penalties-226e")
PW_OT, PW_PEN, SEMI = "pw-overtime-8-40-time-and-half", "penalty-1813-overtime", "semimonthly-paydays-26th-and-10th"
WEEKLY_PAY, ON_DUTY_MEAL = "weekly-biweekly-payroll-7-days", "on-duty-meal-written-agreement"
FINAL_PAY = {"discharged": "final-pay-discharge-or-layoff-immediate", "laid_off": "final-pay-discharge-or-layoff-immediate",
             "quit_with_72h_notice": "final-pay-quit-with-72-hours-notice",
             "quit_without_notice": "final-pay-quit-without-notice-72-hours"}
OIL_LAYOFF = "final-pay-oil-drilling-layoff-24-hours"
SICK_USE, SICK_ELIG, SICK_ACCRUAL, SICK_CAPS = ("paid-sick-leave-40-hours-5-days", "paid-sick-leave-eligibility",
                                                "paid-sick-leave-accrual", "paid-sick-leave-caps")
SICK_INCR, SICK_PAY, SICK_SEP = ("paid-sick-leave-minimum-increment", "paid-sick-leave-pay-timing-rate",
                                 "paid-sick-leave-separation-rehire")

# Values with no rule in the rulebook. A result that uses one is marked verified=False.
UNVERIFIED = {
    "split_shift_screen": {
        "flag_unpaid_gap_over_minutes": 60,
        "source": "https://www.dir.ca.gov/IWC/IWCArticle16.pdf",
        "note": "Screening threshold only, not law. The rulebook has no Wage Order 16 split-shift rule, so no "
                "split-shift premium is computed; an unpaid gap between work periods longer than this is flagged "
                "for review against Wage Order 16 and DLSE guidance.",
    },
    "minimum_wage_liquidated_damages": {
        "multiplier_of_unpaid_minimum_wages": 1,
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=LAB&sectionNum=1194.2",
        "note": "Labor Code 1194.2(a): in a claim for pay below the minimum wage the employee may also recover "
                "liquidated damages equal to the unpaid minimum wages plus interest (the court or Labor Commissioner "
                "may reduce them if the employer shows good faith, LC 1194.2(b)). Not in the rulebook; statute not "
                "read in this pass. Applies to the minimum-wage part only, not to overtime.",
    },
}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PayFrequency = Literal["weekly", "biweekly", "semimonthly", "monthly"]
SeparationType = Literal["discharged", "laid_off", "quit_with_72h_notice", "quit_without_notice"]


def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _h(minutes: float) -> float:
    return round(minutes / 60, 2)


def _as_date(value, field: str = "date") -> Date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{field} must be a date like 2026-11-02 or 11/02/2026, got {value!r}")


_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(?:([ap])\.?m?\.?)?", re.I)


def _clock(value, field: str) -> int:
    """Clock time -> minutes after midnight. Accepts 06:30, 6:30 AM, 6pm, 2026-11-02T06:30."""
    s = str(value or "").strip()
    if "T" in s:
        s = s.split("T", 1)[1]
    m = _CLOCK.fullmatch(s)
    if not m:
        raise ValueError(f"{field} must be a clock time like 06:30 or 6:30 AM, got {value!r}")
    h, mi, ap = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f"{field}: {value!r} is not a valid 12-hour time")
        h = h % 12 + (12 if ap == "p" else 0)
    if mi > 59 or h > 24 or (h == 24 and mi):
        raise ValueError(f"{field}: {value!r} is not a valid time")
    return h * 60 + mi


# ---------------------------------------------------------------- input / output models
class Break(BaseModel):
    start: str = Field(description="Clock time the meal break started, e.g. '11:00' or '11:00 AM'")
    end: str = Field(description="Clock time the meal break ended")


class TimeEntry(BaseModel):
    employee: str = Field(description="Employee name or ID (must match a key in hourly_rates)")
    date: Date = Field(description="Workday the shift started (ISO 2026-11-02 or 11/02/2026)")
    start: str = Field(description="Clock-in time, e.g. '06:00' or '6:00 AM'")
    end: str = Field(description="Clock-out time; earlier than start means the shift ran past midnight")
    meal_breaks: list[Break] = Field(default_factory=list, description="Unpaid meal breaks taken inside this entry")
    rest_breaks_taken: Optional[int] = Field(None, ge=0, description=(
        "Number of 10-minute paid rest breaks authorized and taken. Leave empty if not recorded (Wage Order 16 does "
        "not require recording rest breaks): no rest premium is then computed"))
    on_call: bool = Field(False, description="True if the worker had to stay on call or on duty during breaks "
                                             "(radio on, watch equipment, return if called)")
    first_meal_waived: bool = Field(False, description="Employee agreed to waive the first meal (only valid if the "
                                                       "day is 6 hours or less)")
    second_meal_waived: bool = Field(False, description="Employee agreed to waive the second meal (only valid if the "
                                                        "day is 12 hours or less and the first meal was not waived)")

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, v):
        return _as_date(v)


class LineItem(BaseModel):
    item: str
    value: float
    unit: Literal["usd", "hours", "days"] = "usd"
    math: str
    rule_ids: list[str]


class ExposureItem(BaseModel):
    item: str
    low_usd: float
    high_usd: float
    math: str
    rule_ids: list[str]
    verified: bool = True


class DayLine(BaseModel):
    date: str
    weekday: str
    hours_worked: float
    regular_hours: float
    overtime_hours: float = Field(description="Hours at 1.5x the regular rate")
    double_time_hours: float = Field(description="Hours at 2x the regular rate")
    seventh_consecutive_day: bool = False
    regular_rate_usd: float
    wages_due_usd: float
    meal_issues: list[str] = Field(default_factory=list)
    meal_premium_hours: float = 0
    rest_breaks_required: int
    rest_breaks_counted: Optional[int] = Field(description="Rest breaks counted; null when not recorded")
    rest_premium_hours: float = 0
    premiums_usd: float = 0
    minimum_wage_shortfall_usd: float = 0
    explanation: list[str] = Field(default_factory=list)


class EmployeePayroll(BaseModel):
    employee: str
    hourly_rate_usd: float
    days: list[DayLine]
    total_hours: float
    regular_hours: float
    overtime_hours: float
    double_time_hours: float
    wages_due_usd: float = Field(description="Gross wages owed for the hours, at the correct OT/DT rates")
    overtime_premium_usd: float = Field(description="Extra owed over straight time for OT/DT hours (what is lost if "
                                                    "all hours were paid at the straight rate)")
    meal_premium_hours: float
    rest_premium_hours: float
    premiums_usd: float
    minimum_wage_shortfall_usd: float
    gross_paid_usd: Optional[float] = None
    amount_owed_usd: float = Field(description="Premiums + minimum wage shortfall, or wages+premiums minus "
                                               "gross_paid_usd when that was given")
    pay_periods_affected: int = 0
    line_items: list[LineItem] = Field(default_factory=list)
    exposure: list[ExposureItem] = Field(default_factory=list)
    exposure_low_usd: float = 0
    exposure_high_usd: float = 0
    issues: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)


class PayrollResult(core.Result):
    as_of: str
    workweek_starts_on: str
    pay_frequency: str
    employees: list[EmployeePayroll]
    total_wages_due_usd: float
    total_premiums_owed_usd: float
    total_minimum_wage_shortfall_usd: float
    total_amount_owed_usd: float
    exposure: list[ExposureItem] = Field(default_factory=list)
    exposure_low_usd: float = 0
    exposure_high_usd: float = 0
    fixes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- CSV
_COLS = {"employee": ("employee", "name", "employee_name", "worker", "employee_id"),
         "date": ("date", "work_date", "day"),
         "start": ("start", "in", "time_in", "clock_in", "start_time"),
         "end": ("end", "out", "time_out", "clock_out", "end_time"),
         "meal_breaks": ("meal_breaks", "meals", "meal_periods"),
         "meal_start": ("meal_start", "lunch_out", "lunch_start", "meal_out"),
         "meal_end": ("meal_end", "lunch_in", "lunch_end", "meal_in"),
         "rest_breaks_taken": ("rest_breaks_taken", "rest_breaks", "breaks"),
         "on_call": ("on_call",), "first_meal_waived": ("first_meal_waived", "meal_waived"),
         "second_meal_waived": ("second_meal_waived",),
         "rate": ("hourly_rate", "rate", "hourly_rate_usd", "pay_rate")}


def _bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "x")


def parse_csv(text: str) -> tuple[list[TimeEntry], dict[str, float]]:
    """CSV time entries -> (entries, rates from an optional rate column). meal_breaks like '11:00-11:30;17:00-17:30',
    or lunch_out/lunch_in columns."""
    reader = csv.DictReader(io.StringIO(text.strip()))
    col = {}
    for h in reader.fieldnames or []:
        norm = re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")
        for name, aliases in _COLS.items():
            if norm in aliases and name not in col:
                col[name] = h
    missing = [c for c in ("employee", "date", "start", "end") if c not in col]
    if missing:
        raise ValueError(f"CSV is missing column(s) {missing}. Expected a header like: "
                         "employee,date,start,end,meal_breaks,rest_breaks_taken,on_call,hourly_rate")
    entries, rates = [], {}
    for line, row in enumerate(reader, 2):
        def get(name):
            return (row.get(col[name]) or "").strip() if name in col else ""
        try:
            meals = [dict(zip(("start", "end"), (x.strip() for x in part.split("-", 1))))
                     for part in re.split(r"[;|]", get("meal_breaks")) if part.strip()]
            if get("meal_start") and get("meal_end"):
                meals.append({"start": get("meal_start"), "end": get("meal_end")})
            rest = get("rest_breaks_taken")
            e = TimeEntry(employee=get("employee"), date=get("date"), start=get("start"), end=get("end"),
                          meal_breaks=meals, rest_breaks_taken=int(float(rest)) if rest else None,
                          on_call=_bool(get("on_call")), first_meal_waived=_bool(get("first_meal_waived")),
                          second_meal_waived=_bool(get("second_meal_waived")))
            if get("rate"):
                rates[e.employee] = float(get("rate").replace("$", "").replace(",", ""))
        except Exception as exc:
            raise ValueError(f"CSV line {line}: {exc}") from None
        entries.append(e)
    if not entries:
        raise ValueError("CSV has no data rows")
    return entries, rates


# ---------------------------------------------------------------- one workday
def _analyze_day(entries: list[TimeEntry]) -> dict:
    """Work segments and off-duty breaks (in minutes from the first clock-in day's midnight)."""
    spans, notes = [], []
    for e in entries:
        s, en = _clock(e.start, "start"), _clock(e.end, "end")
        if en % 1440 == s % 1440:
            raise ValueError(f"{e.employee} {e.date}: start and end are both {e.start}; enter the real clock-out time")
        if en < s:
            en += 1440
            notes.append(f"Shift {e.start}-{e.end} crosses midnight; all of it is counted in the {e.date} workday "
                         "(the day it started). If your workday starts at midnight, enter the after-midnight hours "
                         "as a separate entry on the next date.")
        spans.append((s, en, e))
    spans.sort(key=lambda t: t[0])
    work, offs, on_duty_meals = [], [], 0
    for i, (s, en, e) in enumerate(spans):
        if i and s < spans[i - 1][1]:
            raise ValueError(f"{e.employee} {e.date}: time entries overlap")
        if i:
            offs.append({"start": spans[i - 1][1], "end": s, "kind": "gap"})
        cur = last = s
        for bs, be, b in sorted((*(t + 1440 if t < s else t for t in (_clock(b.start, "meal start"),
                                                                          _clock(b.end, "meal end"))), b)
                                for b in e.meal_breaks):
            if not (last <= bs < be <= en):
                raise ValueError(f"{e.employee} {e.date}: meal break {b.start}-{b.end} is outside the "
                                 f"{e.start}-{e.end} shift or overlaps another break")
            last = be
            if e.on_call:
                on_duty_meals += 1          # not relieved of duty: paid work time, not a meal period
            else:
                work.append((cur, bs))
                offs.append({"start": bs, "end": be, "kind": "meal"})
                cur = be
        work.append((cur, en))
    for o in offs:
        o["minutes"] = o["end"] - o["start"]
        o["worked_before"] = sum(max(0, min(b, o["start"]) - a) for a, b in work)
    # Rest breaks need not be recorded (WO16 §6(A)(1)): unknown unless every entry records them or was on call.
    rest_known = all(e.on_call or e.rest_breaks_taken is not None for e in entries)
    return {"minutes": sum(b - a for a, b in work), "offs": sorted(offs, key=lambda o: o["start"]),
            "on_duty_meals": on_duty_meals, "on_call": any(e.on_call for e in entries),
            "rest_taken": (sum(e.rest_breaks_taken or 0 for e in entries if not e.on_call) if rest_known else None),
            "waive1": any(e.first_meal_waived for e in entries), "waive2": any(e.second_meal_waived for e in entries),
            "entries": ", ".join(f"{e.start}-{e.end}" for _, _, e in spans), "notes": notes}


def _meal_issues(f: dict) -> tuple[list[str], set[str]]:
    """Meal violations for one workday and any extra rule ids the text relied on. A premium is owed only when a
    compliant meal was not provided, so a waiver flag or an on-call break never creates a violation on its own."""
    worked, min_len = f["minutes"], P(MEAL1, "min_minutes")
    h1, h2 = P(MEAL1, "trigger_work_period_hours_gt"), P(MEAL2, "trigger_hours_gt")
    first_by, second_by = h1 * 60, h2 * 60
    meals = [o for o in f["offs"] if o["minutes"] >= min_len]
    short = [o for o in f["offs"] if o["kind"] == "meal" and o["minutes"] < min_len]
    w1, w2 = P(MEAL1, "waivable_if_total_day_hours_lte"), P(MEAL2, "waivable_if_total_day_hours_lte")
    issues, extra, hrs = [], set(), _h(worked)
    too_short = (f"; a {short[0]['minutes']}-minute break does not count (under {min_len} minutes)" if short else "")
    on_call = (f"; the meal taken while on call was not duty-free, so it is paid work time, not a meal period "
               f"(an on-duty meal is lawful only if the work prevents all relief and there is a written, revocable "
               f"on-duty meal agreement, which is rare on a construction site) [{ON_DUTY_MEAL}]"
               if f["on_duty_meals"] else "")
    if worked > first_by:
        waivable = worked <= w1 * 60
        if meals:
            if meals[0]["worked_before"] > first_by:
                issues.append(f"First meal started after {_h(meals[0]['worked_before'])} h of work; it must start by "
                              f"the end of hour {h1}{too_short}{on_call} [{MEAL1}]")
        elif f["waive1"] and waivable:
            pass                                                 # valid mutual waiver, day of 6 h or less
        elif f["waive1"]:
            issues.append(f"No {min_len}-minute meal period, and a first-meal waiver is not allowed on a {hrs} h day "
                          f"(only for days of {w1} h or less){too_short}{on_call} [{MEAL1}]")
        else:
            issues.append(f"No {min_len}-minute meal period on a {hrs} h day (required after {h1} h of work)"
                          f"{too_short}{on_call}"
                          + (f". A day of {w1} h or less allows a mutual waiver: if the employee agreed, set "
                             "first_meal_waived" if waivable else "") + f" [{MEAL1}]")
    if worked > second_by:
        waivable = worked <= w2 * 60 and not (f["waive1"] and not meals)
        if not (f["waive2"] and waivable):
            if len(meals) < 2:
                issues.append(f"No second {min_len}-minute meal period on a {hrs} h day (required after {h2} h of "
                              "work" + (f"; a mutual waiver is allowed up to {w2} h if the first meal was not waived"
                                        if waivable else "") + f"){'' if issues else on_call} [{MEAL2}]")
            elif meals[1]["worked_before"] > second_by:
                issues.append(f"Second meal started after {_h(meals[1]['worked_before'])} h of work; it must start "
                              f"by the end of hour {h2} [{MEAL2}]")
    if issues and on_call:
        extra.add(ON_DUTY_MEAL)
    return issues, extra


def _rest_required(minutes: int) -> int:
    r = core.rule(REST)
    if minutes < r.param("no_break_if_daily_hours_lt") * 60:
        return 0
    per, major = r.param("per_hours_worked") * 60, r.param("major_fraction_means_more_than_hours") * 60
    n = int(minutes // per)
    return n + (1 if minutes - n * per > major else 0)


def _state_min_wage(d: Date) -> tuple[float, list[str], Optional[str]]:
    r = core.rule(MW)
    eff = Date.fromisoformat(r.param("effective"))
    if d >= eff.replace(year=eff.year + 1):
        return (r.param("hourly_usd"), [MW, MW_CPI],
                f"The {d.year} state minimum wage is not in the rulebook; the {eff.year} rate "
                f"${r.param('hourly_usd'):.2f} was used. Check https://www.dir.ca.gov/dlse/faq_minimumwage.htm")
    if d >= eff:
        return r.param("hourly_usd"), [MW], None
    for start, rate in sorted(r.param("prior_rates_usd").items(), reverse=True):
        if d >= Date.fromisoformat(start):
            return rate, [MW], None
    raise ValueError(f"No California minimum wage in the rulebook for {d}")


def _period_key(d: Date, freq: str, week_start: Date, anchor: Date):
    if freq == "weekly":
        return week_start
    if freq == "biweekly":
        return (week_start - anchor).days // 14
    if freq == "semimonthly":                                    # LC 204: 1st-15th, 16th-last day
        first_half_ends = int(re.findall(r"\d+", P(SEMI, "period_1.earned"))[-1])
        return d.year, d.month, 1 if d.day <= first_half_ends else 2
    return d.year, d.month


# ---------------------------------------------------------------- check_payroll
def check_payroll(records: Union[str, list], hourly_rates: Optional[dict[str, float]] = None,
                  local_minimum_wage_usd: Optional[float] = None, as_of: Optional[Date] = None,
                  workweek_starts_on: Weekday = "monday", pay_frequency: PayFrequency = "weekly",
                  gross_paid_usd: Optional[dict[str, float]] = None, public_works: bool = False,
                  include_unverified_penalties: bool = True) -> PayrollResult:
    as_of = _as_date(as_of, "as_of") if as_of else logic.today_ca()
    if workweek_starts_on not in WEEKDAYS:
        raise ValueError(f"workweek_starts_on must be one of {WEEKDAYS}")
    if isinstance(records, str):
        entries, csv_rates = parse_csv(records)
    else:
        entries, csv_rates = [r if isinstance(r, TimeEntry) else TimeEntry.model_validate(r) for r in records], {}
    if not entries:
        raise ValueError("No time entries given")
    rates = {k.strip().casefold(): float(v) for k, v in {**csv_rates, **(hourly_rates or {})}.items()}
    paid = {k.strip().casefold(): float(v) for k, v in (gross_paid_usd or {}).items()}
    if missing := sorted({e.employee for e in entries if e.employee.strip().casefold() not in rates}):
        raise ValueError(f"No hourly rate for: {', '.join(missing)}. Pass hourly_rates={{name: rate}} or a rate column.")
    if bad := sorted(k for k, v in rates.items() if v <= 0):
        raise ValueError(f"Hourly rate must be greater than 0 for: {', '.join(bad)}")
    if local_minimum_wage_usd is not None and local_minimum_wage_usd <= 0:
        raise ValueError("local_minimum_wage_usd must be greater than 0")

    cited = {MW, MW_LOCAL, MW_PERIOD, OT, DT, MEAL1, MEAL2, MEAL_PREM, REST, REST_PREM}
    notes, fixes, unverified_used, split_flagged, rest_unrecorded = [], [], False, False, 0
    wk0 = WEEKDAYS.index(workweek_starts_on)
    week_of = lambda d: d - timedelta(days=(d.weekday() - wk0) % 7)
    anchor = min(week_of(e.date) for e in entries)
    ot_x, dt_x = P(OT, "multiplier"), P(DT, "multiplier")
    day_cap, band_cap, week_cap = (P(OT, "daily_threshold_hours") * 60, P(DT, "daily_threshold_hours") * 60,
                                   P(OT, "weekly_threshold_hours") * 60)
    seventh_cap = P(DT, "seventh_consecutive_day_threshold_hours") * 60
    meal_ph, rest_ph = P(MEAL_PREM, "premium_hours"), P(REST_PREM, "premium_hours_per_workday")
    gap_flag = UNVERIFIED["split_shift_screen"]["flag_unpaid_gap_over_minutes"]

    by_emp: dict[str, dict[Date, list[TimeEntry]]] = defaultdict(lambda: defaultdict(list))
    names = {}
    for e in entries:
        key = e.employee.strip().casefold()
        names.setdefault(key, e.employee.strip())
        by_emp[key][e.date].append(e)

    employees = []
    for key, days in sorted(by_emp.items()):
        name, rate = names[key], rates[key]
        facts = {d: _analyze_day(es) for d, es in days.items()}
        weeks = defaultdict(list)
        for d in facts:
            weeks[week_of(d)].append(d)
        lines, issues, periods_owed, all_periods, pw_days = [], [], set(), set(), 0
        for ws in sorted(weeks):
            worked_all_7 = all(facts.get(ws + timedelta(i), {}).get("minutes", 0) > 0 for i in range(7))
            straight_cum, pw_cum = 0, 0
            for d in sorted(weeks[ws]):
                f, why = facts[d], []
                m = f["minutes"]
                seventh = worked_all_7 and d == ws + timedelta(6)
                if seventh:
                    reg, ot, dt = 0, min(m, seventh_cap), max(0, m - seventh_cap)
                    why.append(f"7th consecutive day worked in this workweek: first "
                               f"{P(DT, 'seventh_consecutive_day_threshold_hours')} h at {ot_x}x, the rest at {dt_x}x "
                               f"[{OT}, {DT}]")
                    cited.add(REST_DAY)
                    issues.append(f"{d}: worked all 7 days of the workweek")
                else:
                    reg, ot, dt = min(m, day_cap), min(max(0, m - day_cap), band_cap - day_cap), max(0, m - band_cap)
                    over = max(0, straight_cum + reg - week_cap)
                    if over:
                        reg, ot = reg - over, ot + over
                        why.append(f"{_h(over)} h moved to {ot_x}x because straight-time hours passed "
                                   f"{P(OT, 'weekly_threshold_hours')} this workweek (daily overtime hours are not "
                                   f"counted twice) [{OT}]")
                    straight_cum += reg
                pw_cum += m
                if public_works and (m > P(PW_OT, "daily_hours_threshold") * 60
                                     or pw_cum > P(PW_OT, "weekly_hours_threshold") * 60):
                    pw_days += 1
                state_mw, mw_rules, mw_note = _state_min_wage(d)
                cited.update(mw_rules)
                if mw_note and mw_note not in notes:
                    notes.append(mw_note)
                mw = max(state_mw, local_minimum_wage_usd or 0)
                rr = max(rate, mw)
                units = reg + ot_x * ot + dt_x * dt
                wages = _money(rr * units / 60)
                shortfall = _money((rr - rate) * units / 60) if rate < mw else 0.0
                meal, meal_rules = _meal_issues(f)
                cited.update(meal_rules)
                rest_req, rest_got = _rest_required(m), f["rest_taken"]
                mp, rp = (meal_ph if meal else 0), (rest_ph if rest_got is not None and rest_got < rest_req else 0)
                rest_unrecorded += rest_got is None and rest_req > 0
                prem = _money(rr * (mp + rp))
                if rate < mw and (ot or dt or mp or rp):
                    cited.add(REG_RATE)
                why.insert(0, f"{_h(m)} h worked ({f['entries']}): {_h(reg)} h regular, {_h(ot)} h at {ot_x}x, "
                              f"{_h(dt)} h at {dt_x}x [{OT}, {DT}]")
                why.append("Wages: " + " + ".join(f"{_h(mins)} h x ${rr * x:.2f}" for mins, x in
                                                  ((reg, 1), (ot, ot_x), (dt, dt_x)) if mins) + f" = ${wages:,.2f}")
                if rate < mw:
                    why.append(f"Rate ${rate:.2f} is below the ${mw:.2f} minimum wage for this day: pay at least "
                               f"${mw:.2f}; shortfall ${shortfall:,.2f} [{MW}, {MW_LOCAL}, {MW_PERIOD}]")
                why.extend(meal)
                if mp:
                    why.append(f"Meal premium: {meal_ph} h x ${rr:.2f} = ${rr * meal_ph:,.2f} (one per workday) "
                               f"[{MEAL_PREM}]")
                why.append(f"Rest breaks: {rest_req} required for {_h(m)} h "
                           f"({P(REST, 'minutes_per_break')} min per {P(REST, 'per_hours_worked')} h or major "
                           f"fraction), " + (f"{rest_got} counted" if rest_got is not None else
                                             "not recorded, so no rest premium was computed (Wage Order 16 does not "
                                             "require recording rest breaks; confirm they were authorized and "
                                             "permitted)")
                           + (" (breaks on call do not count)" if f["on_call"] else "") + f" [{REST}]")
                if rp:
                    why.append(f"Rest premium: {rest_ph} h x ${rr:.2f} = ${rr * rest_ph:,.2f} (one per workday, not "
                               f"per missed break) [{REST_PREM}]")
                for o in f["offs"]:
                    kind = "meal break" if o["kind"] == "meal" else "gap between work periods"
                    if o["minutes"] > gap_flag:
                        unverified_used = split_flagged = True
                        why.append(f"Unpaid {kind} of {_h(o['minutes'])} h: possible split shift. "
                                   f"{UNVERIFIED['split_shift_screen']['note']}")
                    elif o["minutes"] < P(MEAL1, "min_minutes"):
                        why.append(f"Unpaid {kind} of {o['minutes']} min is too short to be a meal period "
                                   f"(under {P(MEAL1, 'min_minutes')} min). If it was a rest break, it must be paid: "
                                   f"rest breaks are paid time, not deducted [{REST}, {MEAL1}]")
                why.extend(f["notes"])
                if d > as_of and (late := f"{name} has time entries after the as-of date {as_of}.") not in notes:
                    notes.append(late)
                pk = _period_key(d, pay_frequency, ws, anchor)
                all_periods.add(pk)
                if prem or shortfall:
                    periods_owed.add(pk)
                if meal:
                    issues.append(f"{d}: meal period violation")
                if rp:
                    issues.append(f"{d}: rest break(s) missed ({rest_got} of {rest_req})")
                lines.append(DayLine(
                    date=d.isoformat(), weekday=WEEKDAYS[d.weekday()], hours_worked=_h(m), regular_hours=_h(reg),
                    overtime_hours=_h(ot), double_time_hours=_h(dt), seventh_consecutive_day=seventh,
                    regular_rate_usd=rr, wages_due_usd=wages, meal_issues=meal, meal_premium_hours=mp,
                    rest_breaks_required=rest_req, rest_breaks_counted=rest_got, rest_premium_hours=rp,
                    premiums_usd=prem, minimum_wage_shortfall_usd=shortfall, explanation=why))
        employees.append(_employee(name, rate, lines, issues, periods_owed, all_periods, pw_days, paid.get(key),
                                   public_works, include_unverified_penalties))

    # roll-up
    exposure: dict[str, ExposureItem] = {}
    for emp in employees:
        for x in emp.exposure:
            agg = exposure.setdefault(x.item, ExposureItem(item=x.item, low_usd=0, high_usd=0, math="",
                                                           rule_ids=x.rule_ids, verified=x.verified))
            agg.low_usd, agg.high_usd = _money(agg.low_usd + x.low_usd), _money(agg.high_usd + x.high_usd)
            agg.math = (agg.math + "; " if agg.math else "") + f"{emp.employee}: {x.math}"
        fixes.extend(f for f in emp.fixes if f not in fixes)
    if any(x.rule_ids == [STUB_PEN] for x in exposure.values()):
        cited.add(STUB_PEN)
    if any(not x.verified and not x.rule_ids for x in exposure.values()):
        unverified_used = True                                   # an UNVERIFIED value (no rule) was used
        notes.append("Minimum-wage liquidated damages (LC 1194.2) are not in the rulebook: "
                     f"{UNVERIFIED['minimum_wage_liquidated_damages']['source']}")
    if exposure:
        cited.update({WAIT, LATE_PAY, WO16_PEN})
    if pay_frequency == "monthly":
        cited.add(SEMI)
        fixes.append(f"Monthly pay is not allowed for nonexempt employees: pay at least "
                     f"{P(SEMI, 'min_paydays_per_calendar_month')} times each calendar month on regular paydays "
                     f"(wages for the {P(SEMI, 'period_1.earned')} by the {P(SEMI, 'period_1.pay_no_later_than')}, "
                     f"the {P(SEMI, 'period_2.earned')} by the {P(SEMI, 'period_2.pay_no_later_than')}). Only exempt "
                     f"executive, administrative and professional employees may be paid monthly (they "
                     f"{P(SEMI, 'exempt_EAP_option')}) (LC 204) [{SEMI}]")
    elif pay_frequency == "semimonthly":
        cited.add(SEMI)
        notes.append(f"Semimonthly paydays: wages for the {P(SEMI, 'period_1.earned')} are due by the "
                     f"{P(SEMI, 'period_1.pay_no_later_than')}; the {P(SEMI, 'period_2.earned')} by the "
                     f"{P(SEMI, 'period_2.pay_no_later_than')} [{SEMI}].")
    else:
        cited.add(WEEKLY_PAY)
        notes.append(f"{pay_frequency.title()} payroll: pay within "
                     f"{P(WEEKLY_PAY, 'deadline_calendar_days_after_period_end')} calendar days after the pay period "
                     f"ends [{WEEKLY_PAY}].")
    if public_works:
        cited.update({PW_OT, PW_PEN})
        notes.append(f"Public works: overtime over {P(PW_OT, 'daily_hours_threshold')} h/day or "
                     f"{P(PW_OT, 'weekly_hours_threshold')} h/week is at least {P(PW_OT, 'min_multiplier')}x the "
                     "basic prevailing rate, and the wage determination's overtime rate controls when higher (e.g. "
                     "double time, weekend rates). Use the basic prevailing rate as hourly_rate. Missing overtime "
                     f"costs ${P(PW_PEN, 'per_worker_per_day_usd')} per worker per calendar day (LC 1813, not "
                     f"waivable) [{PW_OT}, {PW_PEN}]")
    if any(e.overtime_hours or e.double_time_hours for e in employees):
        cited.add(OT_DEADLINE)
        fixes.append(f"Pay overtime and double time no later than the payday for the next regular payroll period "
                     f"(LC 204(b)) [{OT_DEADLINE}]")
    if REST_DAY in cited:
        fixes.append(f"Give {P(REST_DAY, 'rest_days_per_7_days')} day's rest in seven. Rest days may be accumulated "
                     f"{P(REST_DAY, 'accumulation_allowed')}, but each calendar month must include the equivalent of "
                     f"one day in seven, and no one may be disciplined for refusing the 7th day [{REST_DAY}]")
    cba_x = P(CBA, "min_regular_rate_multiplier_of_state_min_wage")
    state_now, state_rules, _ = _state_min_wage(as_of)
    cba_floor = _money(cba_x * state_now)
    if any(e.hourly_rate_usd >= cba_floor for e in employees):
        cited.update({CBA, *state_rules})
        notes.append(f"Union crews: if a valid collective bargaining agreement sets wages, hours and conditions, pays "
                     f"premium rates for all overtime and a regular rate of at least ${cba_floor:.2f} ({cba_x}x the "
                     "state minimum), Wage Order 16 overtime and meal sections do not apply; this check assumes no "
                     f"CBA [{CBA}]" + (". That exemption is from Wage Order 16 only: public works overtime is "
                                       f"under separate rules (Labor Code 1810-1815, see the public works note) "
                                       f"[{OT}, {PW_OT}]" if public_works else ""))
    notes.append(f"Assumes nonexempt hourly employees on a standard {P(OT, 'daily_threshold_hours')} h/day, "
                 f"{P(OT, 'weekly_threshold_hours')} h/week schedule with one rate each: no alternative workweek, "
                 "piece rate, bonuses or shift differentials (those change the regular rate). Have a California "
                 "employment attorney review before relying on an alternative workweek, an exemption, or a "
                 "settlement of back wages.")
    notes.append(f"Workweek = 7 days starting {workweek_starts_on.title()}; each entry counts in the workday of its "
                 f"date. Weekly ({P(OT, 'weekly_threshold_hours')} h) and 7th-day overtime only see the days in this "
                 f"data, so send complete workweeks [{OT}].")
    if rest_unrecorded:
        notes.append(f"Rest breaks were not recorded for {rest_unrecorded} workday(s), so no rest premium was computed "
                     "for them. Wage Order 16 does not require recording rest breaks, but you must authorize and "
                     f"permit them; pass rest_breaks_taken to check them [{REST}].")
    if local_minimum_wage_usd is None:
        notes.append("No local minimum wage given, so the state rate was used. Many cities and counties are higher; "
                     f"pass local_minimum_wage_usd for the jobsite city [{MW_LOCAL}].")
    sol_years = P(MEAL_PREM, "statute_of_limitations_years")
    try:
        sol = as_of.replace(year=as_of.year - sol_years)
    except ValueError:                                           # Feb 29
        sol = as_of.replace(year=as_of.year - sol_years, day=28)
    if any(e.date < sol for e in entries):
        notes.append(f"Some entries are before {sol}: meal/rest premium claims generally have a {sol_years}-year "
                     f"limit [{MEAL_PREM}].")
    if exposure:
        notes.append("Exposure is an estimate of what a Labor Commissioner claim could add: 'low' = amounts owed + "
                     f"first-violation penalties; 'high' adds willful/subsequent penalties, a {P(WAIT, 'max_days')}-day "
                     "waiting-time penalty (only if the employee leaves before being paid) and, when included, "
                     "unverified pay-stub penalties and minimum-wage liquidated damages. Paying the amounts owed "
                     "promptly, shown on the wage statement, limits further exposure; it does not by itself cancel "
                     "penalties for the late payment. Premiums are owed when you did not provide the meal or "
                     "authorize and permit the rest break; a break the employee freely chose to skip is not a "
                     f"violation, but you must be able to show it was provided [{MEAL_PREM}, {REST_PREM}].")

    if split_flagged:
        notes.append(f"Split-shift screen uses an unverified threshold ({UNVERIFIED['split_shift_screen']['source']}).")
    res = PayrollResult(
        as_of=as_of.isoformat(), workweek_starts_on=workweek_starts_on, pay_frequency=pay_frequency,
        employees=employees, total_wages_due_usd=_money(sum(e.wages_due_usd for e in employees)),
        total_premiums_owed_usd=_money(sum(e.premiums_usd for e in employees)),
        total_minimum_wage_shortfall_usd=_money(sum(e.minimum_wage_shortfall_usd for e in employees)),
        total_amount_owed_usd=_money(sum(e.amount_owed_usd for e in employees)),
        exposure=list(exposure.values()), exposure_low_usd=_money(sum(x.low_usd for x in exposure.values())),
        exposure_high_usd=_money(sum(x.high_usd for x in exposure.values())), fixes=fixes, notes=notes)
    res.cite(*sorted(cited))
    if unverified_used:
        res.verified = False
    return res


def _employee(name, rate, lines, issues, periods_owed, all_periods, pw_days, gross_paid, public_works,
              include_unverified) -> EmployeePayroll:
    tot = lambda attr: round(sum(getattr(x, attr) for x in lines), 2)
    ot_x, dt_x = P(OT, "multiplier"), P(DT, "multiplier")
    wages, prem, short = _money(tot("wages_due_usd")), _money(tot("premiums_usd")), _money(tot("minimum_wage_shortfall_usd"))
    ot_prem = _money(sum(x.regular_rate_usd * ((ot_x - 1) * x.overtime_hours + (dt_x - 1) * x.double_time_hours)
                         for x in lines))
    rr = lines[-1].regular_rate_usd
    same_rate = len({x.regular_rate_usd for x in lines}) == 1
    at = lambda h, mult: f"{h} h x ${rr * mult:.2f}" if same_rate else f"{h} h (rates by day, see days)"
    items = [LineItem(item="Regular hours", value=tot("regular_hours"), unit="hours", math=at(tot("regular_hours"), 1),
                      rule_ids=[OT]),
             LineItem(item=f"Overtime hours ({ot_x}x)", value=tot("overtime_hours"), unit="hours",
                      math=at(tot("overtime_hours"), ot_x), rule_ids=[OT]),
             LineItem(item=f"Double-time hours ({dt_x}x)", value=tot("double_time_hours"), unit="hours",
                      math=at(tot("double_time_hours"), dt_x), rule_ids=[DT]),
             LineItem(item="Gross wages due", value=wages, math="sum of the daily wage lines", rule_ids=[OT, DT, MW]),
             LineItem(item="Overtime premium over straight time", value=ot_prem,
                      math=f"OT h x {ot_x - 1:g} x rate + DT h x {dt_x - 1:g} x rate (owed if you paid straight time "
                           "for every hour)", rule_ids=[OT, DT])]
    mp, rp = tot("meal_premium_hours"), tot("rest_premium_hours")
    if mp:
        items.append(LineItem(item="Meal period premiums", value=_money(sum(x.regular_rate_usd * x.meal_premium_hours
                                                                            for x in lines)),
                              math=f"{mp:g} premium hour(s) at the regular rate (one per workday)",
                              rule_ids=[MEAL_PREM]))
    if rp:
        items.append(LineItem(item="Rest period premiums", value=_money(sum(x.regular_rate_usd * x.rest_premium_hours
                                                                            for x in lines)),
                              math=f"{rp:g} premium hour(s) at the regular rate (one per workday)",
                              rule_ids=[REST_PREM]))
    if short:
        items.append(LineItem(item="Minimum wage shortfall", value=short,
                              math=f"(minimum wage - ${rate:.2f}) x hours (OT/DT multipliers included)",
                              rule_ids=[MW, MW_LOCAL, MW_PERIOD]))
    if gross_paid is not None:
        owed, periods = _money(max(0.0, wages + prem - gross_paid)), len(all_periods)
        owed_math = f"${wages:,.2f} wages + ${prem:,.2f} premiums - ${gross_paid:,.2f} paid"
    else:
        owed, periods = _money(prem + short), len(periods_owed)
        owed_math = f"${prem:,.2f} premiums + ${short:,.2f} minimum wage shortfall"
    fixes = []
    if mp:
        h1, h2 = P(MEAL1, "trigger_work_period_hours_gt"), P(MEAL2, "trigger_hours_gt")
        fixes.append(f"{name}: pay {mp:g} meal premium hour(s) on the next payday as its own wage-statement line. "
                     f"Start the first {P(MEAL1, 'min_minutes')}-minute meal no later than the end of hour {h1} "
                     f"worked (a 6:00 start with no earlier unpaid break means lunch starts by {6 + h1}:00), start a "
                     f"second no later than the end of hour {h2} on days over {h2} h, and relieve the crew of all "
                     f"duty [{MEAL1}, {MEAL2}, {MEAL_PREM}]")
    if rp:
        sched = ", ".join(f"{s['breaks']} for {s['hours']} h" for s in P(REST, "schedule"))
        fixes.append(f"{name}: pay {rp:g} rest premium hour(s). Authorize a paid {P(REST, 'minutes_per_break')}-minute "
                     f"break per {P(REST, 'per_hours_worked')} h or major fraction ({sched}); breaks taken on call do "
                     f"not count [{REST}, {REST_PREM}]")
    if short:
        fixes.append(f"{name}: raise the rate from ${rate:.2f} to at least ${max(x.regular_rate_usd for x in lines):.2f} "
                     f"and pay the ${short:,.2f} shortfall. Exception: an apprentice regularly indentured under the "
                     f"Division of Apprenticeship Standards is exempt from the minimum wage (the apprenticeship "
                     f"standards set the rate) [{MW}, {MW_PERIOD}]")
    exposure = []
    if owed > 0:
        periods = max(1, periods)
        days = len(lines)
        avg_reg = round(sum(min(x.hours_worked, P(OT, "daily_threshold_hours")) for x in lines) / days, 2)
        daily = _money(rr * avg_reg)
        p210 = (P(LATE_PAY, "initial_usd_per_employee_per_failure"),
                P(LATE_PAY, "subsequent_or_willful_usd_per_employee_per_failure"),
                P(LATE_PAY, "subsequent_plus_percent_of_wages_withheld"))
        p558 = (P(WO16_PEN, "initial_usd_per_underpaid_employee_per_pay_period"),
                P(WO16_PEN, "subsequent_usd_per_underpaid_employee_per_pay_period"))
        wait_days = P(WAIT, "max_days")
        exposure = [
            ExposureItem(item="Unpaid wages and premiums owed to employees", low_usd=owed, high_usd=owed,
                         math=owed_math, rule_ids=[MEAL_PREM, REST_PREM, MW_PERIOD]
                         + ([OT, DT] if gross_paid is not None else [])),
            ExposureItem(item="LC 210 late/short pay penalty", low_usd=_money(p210[0] * periods),
                         high_usd=_money(p210[1] * periods + p210[2] / 100 * owed),
                         math=f"low ${p210[0]} x {periods} pay period(s); high ${p210[1]} x {periods} + {p210[2]}% of "
                              f"${owed:,.2f}", rule_ids=[LATE_PAY]),
            ExposureItem(item="LC 558 / Wage Order 16 civil penalty", low_usd=_money(p558[0] * periods),
                         high_usd=_money(p558[1] * periods),
                         math=f"${p558[0]} (first) to ${p558[1]} (subsequent) x {periods} pay period(s)",
                         rule_ids=[WO16_PEN]),
            ExposureItem(item="LC 203 waiting-time penalty (only if the employee leaves before being paid)",
                         low_usd=0, high_usd=_money(daily * wait_days),
                         math=f"daily rate ${rr:.2f} x {avg_reg} regular h/day = ${daily:,.2f} x {wait_days} days max",
                         rule_ids=[WAIT])]
        if include_unverified:
            a, b, cap = (P(STUB_PEN, "initial_pay_period_usd"), P(STUB_PEN, "subsequent_pay_period_usd"),
                         P(STUB_PEN, "aggregate_cap_per_employee_usd"))
            exposure.append(ExposureItem(
                item="LC 226(e) inaccurate wage statement (knowing and intentional, unverified rule)", low_usd=0,
                high_usd=_money(min(cap, a + b * (periods - 1))), verified=False, rule_ids=[STUB_PEN],
                math=f"${a} first + ${b} x {periods - 1} later pay period(s), max ${cap:,}"))
            # LC 1194.2 liquidated damages: state minimum-wage part only (hours x (state minimum - rate)); not OT and
            # not a local-ordinance rate (LC 1194.2 covers minimums fixed by statute or IWC order).
            mw_base = _money(sum(max(0.0, _state_min_wage(Date.fromisoformat(x.date))[0] - rate) * x.hours_worked
                                 for x in lines))
            if mw_base:
                ld = UNVERIFIED["minimum_wage_liquidated_damages"]
                k = ld["multiplier_of_unpaid_minimum_wages"]
                exposure.append(ExposureItem(
                    item="LC 1194.2 minimum-wage liquidated damages (unverified, no rule in the rulebook)", low_usd=0,
                    high_usd=_money(k * mw_base), verified=False, rule_ids=[],
                    math=f"{k:g} x ${mw_base:,.2f} unpaid state minimum wages (hours worked x (state minimum - "
                         f"${rate:.2f})), plus interest not computed; local-ordinance remedies not included; source "
                         f"{ld['source']}"))
        if public_works and pw_days:
            per = P(PW_PEN, "per_worker_per_day_usd")
            exposure.append(ExposureItem(item="LC 1813 public works overtime penalty (if overtime was not paid)",
                                         low_usd=0, high_usd=_money(per * pw_days), rule_ids=[PW_PEN],
                                         math=f"${per} x {pw_days} worker-day(s) over "
                                              f"{P(PW_OT, 'daily_hours_threshold')} h/day or "
                                              f"{P(PW_OT, 'weekly_hours_threshold')} h/week"))
    return EmployeePayroll(
        employee=name, hourly_rate_usd=rate, days=lines, total_hours=tot("hours_worked"),
        regular_hours=tot("regular_hours"), overtime_hours=tot("overtime_hours"),
        double_time_hours=tot("double_time_hours"), wages_due_usd=wages, overtime_premium_usd=ot_prem,
        meal_premium_hours=mp, rest_premium_hours=rp, premiums_usd=prem, minimum_wage_shortfall_usd=short,
        gross_paid_usd=gross_paid, amount_owed_usd=owed, pay_periods_affected=periods if owed > 0 else 0,
        line_items=items, exposure=exposure, exposure_low_usd=_money(sum(x.low_usd for x in exposure)),
        exposure_high_usd=_money(sum(x.high_usd for x in exposure)), issues=issues, fixes=fixes)


# ---------------------------------------------------------------- final paycheck
class FinalPayResult(core.Result):
    separation_type: str
    separation_date: str
    due_date: str
    due_rule: str
    place_of_payment: Optional[str] = None
    must_include: list[str] = Field(default_factory=list)
    daily_wage_usd: float
    penalty_per_day_usd: float
    max_penalty_days: int
    max_penalty_usd: float
    penalty_stops_growing_on: str
    paid_date: Optional[str] = None
    status: Literal["not_yet_due", "due_today", "paid_on_time", "paid_late", "late_unpaid", "late_unpaid_max_reached"]
    days_late: int = 0
    penalty_accrued_usd: float = 0
    line_items: list[LineItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def final_paycheck_deadline(separation_type: SeparationType, separation_date, daily_wage_usd: float,
                            paid_date=None, as_of: Optional[Date] = None,
                            separation_time: Optional[str] = None) -> FinalPayResult:
    if separation_type not in FINAL_PAY:
        raise ValueError(f"separation_type must be one of {list(FINAL_PAY)}")
    if daily_wage_usd <= 0:
        raise ValueError("daily_wage_usd must be greater than 0 (regular hours per day x hourly rate)")
    sep, as_of = _as_date(separation_date, "separation_date"), (_as_date(as_of, "as_of") if as_of else logic.today_ca())
    paid = _as_date(paid_date, "paid_date") if paid_date else None
    rid, r = FINAL_PAY[separation_type], core.rule(FINAL_PAY[separation_type])
    notes, extra = [], []
    if separation_type == "quit_without_notice":
        hours = r.param("deadline_hours_after_quit")
        due = sep + timedelta(hours=hours)
        due_rule = f"Within {hours} hours of quitting"
        if separation_time:
            t = _clock(separation_time, "separation_time")
            due_dt = datetime.combine(sep, datetime.min.time()) + timedelta(minutes=t, hours=hours)
            due_rule += f": by {due_dt:%Y-%m-%d %H:%M}"
        place = r.param("place_if_not_mailed")
        notes.append("If the employee asks, mail the check; the mailing date counts as the payment date "
                     f"[{rid}].")
    else:
        due, due_rule = sep, r.param("deadline")[0].upper() + r.param("deadline")[1:]
        if separation_type == "quit_with_72h_notice":
            # LC 208: an employee who quits is paid at the employer's office in the county where the work was done.
            extra.append(FINAL_PAY["quit_without_notice"])
            place = P(FINAL_PAY["quit_without_notice"], "place_if_not_mailed")
            notes.append(f"Applies when the employee gave at least {r.param('notice_hours_min')} hours' notice and "
                         f"quits on the day given. If you end employment before the notice date, it becomes a "
                         f"discharge: pay immediately [{rid}, {FINAL_PAY['discharged']}].")
        else:
            place = r.param("place_of_payment")
        if separation_type == "laid_off":
            extra.append(OIL_LAYOFF)
            notes.append(f"A layoff, including the end of a project, is treated like a discharge: pay at the time of "
                         f"layoff [{rid}]. Exception: an oil drilling employee who is laid off may be paid within "
                         f"{P(OIL_LAYOFF, 'deadline_hours')} hours, not counting "
                         f"{', '.join(P(OIL_LAYOFF, 'excluded_days'))} [{OIL_LAYOFF}].")
    includes = [*P(FINAL_PAY["discharged"], "includes"), "any unpaid meal or rest period premiums (they are wages)"]
    extra += [MEAL_PREM, REST_PREM]
    max_days = P(WAIT, "max_days")
    end = paid or as_of
    days_late = max(0, (end - due).days)
    pen_days = min(days_late, max_days)
    if paid:
        status = "paid_late" if days_late else "paid_on_time"
    elif as_of < due:
        status = "not_yet_due"
    elif as_of == due:
        status = "due_today"
    else:
        status = "late_unpaid_max_reached" if days_late >= max_days else "late_unpaid"
    items = [LineItem(item="Waiting-time penalty per day", value=_money(daily_wage_usd),
                      math=f"daily rate = {P(WAIT, 'daily_rate')}",
                      rule_ids=[WAIT]),
             LineItem(item="Maximum waiting-time penalty", value=_money(daily_wage_usd * max_days),
                      math=f"${daily_wage_usd:,.2f} x {max_days} calendar days", rule_ids=[WAIT])]
    if days_late:
        items.append(LineItem(item="Penalty accrued so far" if not paid else "Penalty for late payment",
                              value=_money(daily_wage_usd * pen_days),
                              math=f"${daily_wage_usd:,.2f} x min({days_late} days late, {max_days})", rule_ids=[WAIT]))
    notes += [f"Days count as {P(WAIT, 'day_type')}; the penalty stops growing after {max_days} days or on "
              f"{' or '.join(P(WAIT, 'stops_accruing'))} [{WAIT}].",
              f"The penalty requires a {P(WAIT, 'requires')}; defense: {P(WAIT, 'defense')}. Pay any undisputed "
              f"amount now. Have a California employment attorney review before withholding a disputed amount "
              f"[{WAIT}].",
              f"Unused paid sick leave does not have to be paid out (unless it is part of a PTO/vacation policy); "
              f"restore it if you rehire within {P(SICK_SEP, 'rehire_restoration_window_months')} months "
              f"[{SICK_SEP}]."]
    res = FinalPayResult(
        separation_type=separation_type, separation_date=sep.isoformat(), due_date=due.isoformat(), due_rule=due_rule,
        place_of_payment=place, must_include=includes, daily_wage_usd=_money(daily_wage_usd),
        penalty_per_day_usd=_money(daily_wage_usd), max_penalty_days=max_days,
        max_penalty_usd=_money(daily_wage_usd * max_days),
        penalty_stops_growing_on=(due + timedelta(days=max_days)).isoformat(),
        paid_date=paid.isoformat() if paid else None, status=status, days_late=days_late,
        penalty_accrued_usd=_money(daily_wage_usd * pen_days), line_items=items, notes=notes)
    return res.cite(rid, FINAL_PAY["discharged"], WAIT, SICK_SEP, *extra)


# ---------------------------------------------------------------- paid sick leave
class SickLeaveResult(core.Result):
    as_of: str
    hire_date: str
    plan: str
    days_employed: int
    covered: bool
    can_use_now: bool
    usable_from: str
    accrued_hours: float
    carryover_hours: float
    hours_used: float
    balance_hours: float
    accrual_cap_hours: float
    at_accrual_cap: bool
    annual_use_minimum_hours: float = Field(description="Employer must let the employee use at least this much a year")
    use_cap_hours: float = Field(description="Employer may limit yearly use to this much")
    usable_now_hours: float
    next_milestone: Optional[str] = None
    line_items: list[LineItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _frontload_steps() -> list[tuple[int, float, int]]:
    """(day, hours, days-of-shift) minimums for new hires, read from the rulebook text."""
    steps = [(int(day), float(h), int(d)) for h, d, day in
             re.findall(r"(\d+)h/(\d+) days by day (\d+)", P(SICK_ACCRUAL, "frontload_option"))]
    if not steps:
        raise RuntimeError("rulebook paid-sick-leave-accrual.frontload_option no longer parses")
    return sorted(steps)


def sick_leave_status(hours_worked: float, hours_used: float, hire_date, as_of: Optional[Date] = None,
                      normal_shift_hours: float = 8.0, carryover_hours: float = 0.0, days_worked: Optional[int] = None,
                      plan: Literal["accrual", "frontload"] = "accrual",
                      frontloaded_hours: Optional[float] = None) -> SickLeaveResult:
    hire, as_of = _as_date(hire_date, "hire_date"), (_as_date(as_of, "as_of") if as_of else logic.today_ca())
    if hire > as_of:
        raise ValueError("hire_date is after as_of")
    if min(hours_worked, hours_used, carryover_hours) < 0 or normal_shift_hours <= 0:
        raise ValueError("hours must be 0 or more and normal_shift_hours greater than 0")
    shift = normal_shift_hours
    days_employed = (as_of - hire).days + 1                      # hire date = day 1
    wait = P(SICK_ELIG, "waiting_period_days_before_use")
    usable_from = hire + timedelta(days=wait - 1)
    min_days = P(SICK_ELIG, "min_days_worked_within_year")
    covered = (days_worked if days_worked is not None else days_employed) >= min_days
    can_use = as_of >= usable_from and covered
    use_min = max(P(SICK_USE, "min_use_hours_per_year"), P(SICK_USE, "min_use_days_per_year") * shift)
    use_cap = max(P(SICK_CAPS, "use_cap_hours_per_year"), P(SICK_CAPS, "use_cap_days_per_year") * shift)
    acc_cap = max(P(SICK_CAPS, "accrual_cap_hours"), P(SICK_CAPS, "accrual_cap_days") * shift)
    notes, milestone = [], None
    if plan == "accrual":
        earn, per = (float(x) for x in re.findall(r"\d+(?:\.\d+)?", P(SICK_ACCRUAL, "accrual_rate"))[:2])
        accrued = round(hours_worked * earn / per, 2)
        raw = carryover_hours + accrued - hours_used
        balance = round(min(raw, acc_cap), 2)
        items = [LineItem(item="Accrued this year", value=accrued, unit="hours",
                          math=f"{hours_worked:g} h worked x {earn:g}/{per:g}", rule_ids=[SICK_ACCRUAL]),
                 LineItem(item="Balance", value=balance, unit="hours",
                          math=f"{carryover_hours:g} carryover + {accrued:g} accrued - {hours_used:g} used"
                               + (f", capped at {acc_cap:g} (the lowest accrual cap the law allows; with no cap in your "
                                  f"policy the balance is {raw:g})" if raw > acc_cap else ""),
                          rule_ids=[SICK_ACCRUAL, SICK_CAPS])]
        notes.append("Unused accrued sick leave carries over to the next year (accrual may be capped at "
                     f"{acc_cap:g} h) [{SICK_CAPS}].")
        if raw > acc_cap:
            notes.append("Balance computed as if it reached the cap after the hours used; if it hit the cap earlier, "
                         "accrual paused then and the true balance may be lower.")
    else:
        required, steps = use_min, _frontload_steps()
        first_year = days_employed <= steps[-1][0]
        how = (f"greater of {P(SICK_USE, 'min_use_hours_per_year')} h or {P(SICK_USE, 'min_use_days_per_year')} x "
               f"{shift:g} h shift at the start of each 12-month period")
        if first_year:
            due = [(day, max(h, d * shift)) for day, h, d in steps]
            required = max([h for day, h in due if days_employed >= day], default=0.0)
            how = "new hire: " + ", ".join(f"{h:g} h by day {day}" for day, h in due)
            if nxt := next(((day, h) for day, h in due if days_employed < day), None):
                milestone = (f"Have at least {nxt[1]:g} h available by day {nxt[0]} of employment "
                             f"({hire + timedelta(days=nxt[0] - 1)})")
        granted = required if frontloaded_hours is None else frontloaded_hours
        if granted < required:
            notes.append(f"Frontloaded {granted:g} h is below the {required:g} h required by now ({how}) "
                         f"[{SICK_ACCRUAL}].")
        accrued, raw = granted, carryover_hours + granted - hours_used
        balance = round(raw, 2)
        items = [LineItem(item="Minimum frontload required by now", value=required, unit="hours", math=how,
                          rule_ids=[SICK_ACCRUAL, SICK_USE]),
                 LineItem(item="Balance", value=balance, unit="hours",
                          math=f"{carryover_hours:g} carryover + {granted:g} frontloaded - {hours_used:g} used",
                          rule_ids=[SICK_ACCRUAL])]
    if raw < 0:
        notes.append("More sick leave was used than accrued (an advance); the balance is negative.")
    usable = round(max(0.0, min(balance, use_cap - hours_used)), 2) if can_use else 0.0
    items += [LineItem(item="Yearly use the employer must allow", value=use_min, unit="hours",
                       math=f"greater of {P(SICK_USE, 'min_use_hours_per_year')} h or "
                            f"{P(SICK_USE, 'min_use_days_per_year')} days x {shift:g} h", rule_ids=[SICK_USE]),
              LineItem(item="Usable now", value=usable, unit="hours",
                       math=(f"min(balance {balance:g}, use cap {use_cap:g} - {hours_used:g} used)" if can_use else
                             f"0 until day {wait} of employment ({usable_from})" if covered else
                             f"0: not covered until {min_days} days worked"), rule_ids=[SICK_CAPS, SICK_ELIG])]
    if not covered:
        notes.append(f"Not covered yet: paid sick leave applies once the employee works {min_days} days for you in "
                     f"California within a year from hire; accrued hours cannot be used before then [{SICK_ELIG}].")
    if days_worked is None:
        notes.append(f"days_worked not given; coverage assumed from days employed ({days_employed}).")
    notes += [f"Employee chooses how much to use; you may set a minimum increment of no more than "
              f"{P(SICK_INCR, 'max_minimum_increment_hours')} h. Requests may be oral or written; you may not require "
              f"a replacement worker [{SICK_INCR}].",
              f"Pay sick leave by the {P(SICK_PAY, 'pay_deadline')}, at either: "
              f"{'; or '.join(P(SICK_PAY, 'nonexempt_rate_methods'))} [{SICK_PAY}].",
              f"No payout at separation is required; restore the balance if rehired within "
              f"{P(SICK_SEP, 'rehire_restoration_window_months')} months [{SICK_SEP}].",
              f"Local ordinances: {P(SICK_USE, 'local_ordinance')} [{SICK_USE}].",
              "Union construction crews covered by a CBA that meets Labor Code 245.5 may be exempt; that exemption "
              "is not applied here (criteria not verified)."]
    res = SickLeaveResult(
        as_of=as_of.isoformat(), hire_date=hire.isoformat(), plan=plan, days_employed=days_employed, covered=covered,
        can_use_now=can_use, usable_from=usable_from.isoformat(), accrued_hours=accrued,
        carryover_hours=carryover_hours, hours_used=hours_used, balance_hours=balance, accrual_cap_hours=acc_cap,
        at_accrual_cap=plan == "accrual" and balance >= acc_cap, annual_use_minimum_hours=use_min,
        use_cap_hours=use_cap, usable_now_hours=usable, next_milestone=milestone, line_items=items, notes=notes)
    return res.cite(SICK_USE, SICK_ELIG, SICK_ACCRUAL, SICK_CAPS, SICK_INCR, SICK_PAY, SICK_SEP)


# ---------------------------------------------------------------- MCP tools
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="check_payroll", annotations=ro)
    def _check_payroll(
        records: Annotated[Union[list[TimeEntry], str], Field(description=(
            "Time entries as a list of {employee, date, start, end, meal_breaks:[{start,end}], rest_breaks_taken, "
            "on_call, first_meal_waived, second_meal_waived}, or CSV text with a header such as "
            "employee,date,start,end,meal_breaks,rest_breaks_taken,on_call,hourly_rate (meal_breaks like "
            "'11:00-11:30;17:00-17:30', or lunch_out/lunch_in columns). One row per clock-in/clock-out. Leave "
            "rest_breaks_taken out if rest breaks are not tracked: rest premiums are then not computed."))],
        hourly_rates: Annotated[Optional[dict[str, float]], Field(description=(
            "Straight-time hourly rate per employee name, e.g. {'Ana Lopez': 32.5}. Optional if the CSV has a "
            "rate column"))] = None,
        local_minimum_wage_usd: Annotated[Optional[float], Field(description=(
            "City/county minimum wage for the jobsite if higher than the state rate"))] = None,
        workweek_starts_on: Annotated[Weekday, Field(description="First day of your fixed payroll workweek")] = "monday",
        pay_frequency: Annotated[PayFrequency, Field(description="How often you pay (counts pay periods for penalties)")] = "weekly",
        gross_paid_usd: Annotated[Optional[dict[str, float]], Field(description=(
            "Gross pay actually paid per employee for these entries, to compute the real shortfall"))] = None,
        public_works: Annotated[bool, Field(description=(
            "True for a prevailing-wage public works job (adds LC 1810-1815 overtime notes and the LC 1813 "
            "penalty); use the basic prevailing rate as the hourly rate"))] = False,
        include_unverified_penalties: Annotated[bool, Field(description=(
            "Include the LC 226(e) pay-stub penalty and LC 1194.2 minimum-wage liquidated damages in the high "
            "exposure; neither is verified in the rulebook (makes verified=false when they apply)"))] = True,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> PayrollResult:
        """California construction payroll check (IWC Wage Order 16 + Labor Code 510, 512, 226.7, 1182.12) for
        nonexempt hourly employees. For each employee and workday returns hours split into regular, overtime (1.5x
        over 8 h/day, over 40 straight-time h/week, first 8 h of the 7th consecutive day) and double time (2x over
        12 h/day, over 8 h on the 7th day); meal violations (first 30-min meal must start by the end of the 5th hour,
        second by the end of the 10th hour, waivers only for days <=6 h / <=12 h; on-call meals are paid time, not
        meals); rest breaks required vs taken (no rest premium when rest breaks are not recorded); premiums (1 h at
        the regular rate per day per type); minimum wage shortfall; wages due; possible split shifts and unpaid
        short breaks flagged. Also returns totals, estimated penalty exposure (low/high: LC 210, LC 558 / WO16 §18,
        LC 203 waiting time, LC 1813 on public works, and unverified LC 226(e) and LC 1194.2), payday-timing notes
        (monthly pay is flagged as not allowed), fixes, notes and rule citations. Every daily line shows its math.
        Does not compute alternative workweeks, union CBA exemptions, split-shift or reporting-time pay."""
        return check_payroll(records, hourly_rates, local_minimum_wage_usd, as_of, workweek_starts_on, pay_frequency,
                             gross_paid_usd, public_works, include_unverified_penalties)

    @mcp.tool(name="final_paycheck_deadline", annotations=ro)
    def _final_paycheck_deadline(
        separation_type: Annotated[SeparationType, Field(description=(
            "discharged (fired), laid_off (incl. end of project), quit_with_72h_notice, quit_without_notice"))],
        separation_date: Annotated[str, Field(description="ISO date of the last day / quit / discharge")],
        daily_wage_usd: Annotated[float, Field(gt=0, description=(
            "Employee's daily rate: regular hours per day x hourly rate, e.g. 8 x 32 = 256"))],
        paid_date: Annotated[Optional[str], Field(description="ISO date the final check was paid or mailed, if paid")] = None,
        separation_time: Annotated[Optional[str], Field(description="Clock time they quit (for the exact 72-hour deadline)")] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> FinalPayResult:
        """When a California employee's final wages (including accrued vacation/PTO) are due under Labor Code
        201-202: immediately on discharge or layoff, on the last day after 72+ hours' notice, or within 72 hours of
        quitting without notice. Returns the due date and rule, place of payment (LC 208: place of discharge, or the
        employer's office in the county of the work for a quit), what must be included (wages, vacation/PTO, unpaid
        meal/rest premiums), the LC 203 waiting-time penalty per day (one daily wage per calendar day late) and its
        30-day maximum, days late and penalty accrued so far, status, notes and citations."""
        return final_paycheck_deadline(separation_type, separation_date, daily_wage_usd, paid_date, as_of,
                                       separation_time)

    @mcp.tool(name="sick_leave_status", annotations=ro)
    def _sick_leave_status(
        hours_worked: Annotated[float, Field(ge=0, description="Hours worked in the current sick-leave year (since hire if hired this year)")],
        hours_used: Annotated[float, Field(ge=0, description="Sick hours used in the current year")],
        hire_date: Annotated[str, Field(description="ISO hire date")],
        normal_shift_hours: Annotated[float, Field(gt=0, description="Normal shift length (10 for 4x10 crews)")] = 8.0,
        carryover_hours: Annotated[float, Field(ge=0, description="Unused balance carried over from earlier years")] = 0.0,
        days_worked: Annotated[Optional[int], Field(ge=0, description=(
            "Days actually worked for you in California within a year from hire (coverage needs 30); default "
            "assumes every day since hire"))] = None,
        plan: Annotated[Literal["accrual", "frontload"], Field(description="accrual (1 h per 30 worked) or frontload")] = "accrual",
        frontloaded_hours: Annotated[Optional[float], Field(ge=0, description=(
            "Frontload plan only: hours you granted at the start of this 12-month period"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> SickLeaveResult:
        """California paid sick leave status under Labor Code 246 (SB 616 levels): coverage (30 days worked within a
        year from hire), first usable day (90th day of employment, counting the hire date as day 1; nothing is usable
        until covered), hours accrued (1 per 30 worked) or frontloaded, balance, the
        accrual cap (greater of 80 h or 10 shifts), the yearly use the employer must allow (greater of 40 h or 5
        shifts) and may cap at, hours usable now, new-hire frontload milestones, line items with math, and the
        increment, pay timing and separation rules."""
        return sick_leave_status(hours_worked, hours_used, hire_date, as_of, normal_shift_hours, carryover_hours,
                                 days_worked, plan, frontloaded_hours)
