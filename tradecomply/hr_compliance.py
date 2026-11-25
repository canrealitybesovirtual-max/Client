"""California HR mandates for construction employers, on top of the verified pack research/web_hr-mandates.

  jobsite_minimum_wage     city + date -> state vs City of San Diego minimum wage, the San Diego traffic control
                           (flagger) wage on City permits, the San Diego living wage on City service contracts, the
                           exempt salary threshold, the 2x hand-tool rate, upcoming rate changes and a pay check
  employer_size_mandates   headcounts -> which HR mandates apply (leaves and accommodations, lactation, pay
                           transparency, hiring rules, COBRA, FMLA, ACA, postings, San Diego ordinance and fee)
  aca_employer_status      monthly hours of service -> ACA applicable large employer test (full-time + FTEs), seasonal
                           exception, 95% offer rule, 4980H payment formulas and Form 1094-C/1095-C dates
  cobra_deadlines          qualifying event -> COBRA notice, election, payment and maximum-coverage dates and the
                           102% premium cap
  leave_request_check      one time-off request -> is it protected at your size, hours/days left under the caps,
                           FMLA eligibility, notice deadlines and who enforces it

Every legal number, date and threshold is read from the rulebook with core.rule(id).param(key) at call time, and every
rule a result relies on is cited, so `verified` comes only from Result.cite(). Several protections in Labor Code 230
and 230.1 moved to FEHA (Gov. Code 12945.8, Civil Rights Department) and could not be read; results that rely on
those rules are verified=False by design and never route those claims to the Labor Commissioner. Federal amounts the
IRS indexes each year (4980H payments, affordability percentage, 6721/6722 penalties, mileage rate) are not in the
rulebook: the ACA tool takes them as inputs and otherwise shows the formula without dollars.
"""
import csv
import io
import math
import re
from datetime import date as Date, datetime, timedelta
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from . import core, logic

# ---------------------------------------------------------------- rule ids
MW26, MW27, MW_LOCAL = "ca-minimum-wage-2026", "hr-state-minimum-wage-2027", "local-minimum-wage-overrides-state"
EX26, EX27 = "exempt-salary-threshold-2026", "hr-exempt-salary-threshold-2027"
TOOLS2X, HAND_TOOLS = "tools-equipment-employer-provides", "hr-2802-hand-tools-definition"
POST_REPLACE, POST_SPANISH = "hr-posting-replace-when-content-changes", "hr-posting-spanish-minimum-wage-notice"
POST_WO4, POST_BINDER = "hr-posting-wage-order-4-for-office-staff", "hr-posting-binder-jobsite-trailer"
SD26, SD27 = "hr-local-mw-san-diego-2026", "hr-local-mw-san-diego-2027"
SD_ESL, SD_ESL_USE, SD_ESL_NOTICE, SD_ESL_SAFE = ("hr-sd-earned-sick-leave-accrual", "hr-sd-esl-use-after-90-days",
                                                  "hr-sd-esl-advance-notice-max-7-days", "hr-sd-esl-safe-time")
SD_POST, SD_REC, SD_PEN, SD_FEE = ("hr-sd-mw-esl-posting-and-hire-notice", "hr-sd-esl-records-3-years",
                                   "hr-sd-mw-penalty-1000", "hr-sd-minimum-wage-enforcement-fee")
TCW, TCW_EXC, TCW_NOTICE, TCW_FEE = ("hr-sd-traffic-control-worker-wage", "hr-sd-traffic-control-worker-coverage-exceptions",
                                     "hr-sd-traffic-control-worker-notice",
                                     "hr-sd-traffic-control-permit-enforcement-fee-proposed")
LW, LW_LEAVE = "hr-sd-living-wage-fy2027", "hr-sd-living-wage-leave-notice-records"
# expenses, deductions, hiring
EXP, MILEAGE, DEDUCT, BREAKAGE = ("hr-2802-reimburse-necessary-expenses", "hr-2802-mileage-irs-rate",
                                  "hr-wage-deductions-only-three-kinds", "hr-no-deduction-breakage-shortage")
BOND_PHOTO, MED_EXAM, DL_PHYSICAL = ("hr-employer-pays-required-bond-photo", "hr-medical-exam-costs-employer-pays",
                                     "hr-driver-license-physical-employer-pays")
ARREST, MARIJUANA, CREDIT, EVERIFY = ("hr-arrest-record-inquiry-ban", "hr-marijuana-conviction-over-2-years",
                                      "hr-credit-report-ban", "hr-e-verify-misuse-penalty-10000")
# leaves and accommodations
JURY, VICTIM_COURT, VICTIM_LEAVE, VICTIM_ACCOM, VICTIM_CERT = (
    "hr-jury-duty-time-off", "hr-crime-victim-court-time-off", "hr-crime-victim-leave-25-employees",
    "hr-crime-victim-accommodation-any-size", "hr-crime-victim-absence-self-certification")
SCHOOL_TEACHER, SCHOOL_ACT = "hr-school-appearance-teacher-request", "hr-school-activities-leave-25-employees"
EMERG_DUTY, EMERG_TRAIN = "hr-emergency-responder-duty-time-off", "hr-emergency-responder-training-50-employees-14-days"
REHAB, LITERACY, DONOR = ("hr-alcohol-drug-rehab-accommodation-25", "hr-adult-literacy-accommodation-25",
                          "hr-organ-bone-marrow-donor-paid-leave")
LAC_POLICY, LAC_ROOM, LAC_GC, LAC_U50, LAC_NODOC = (
    "hr-lactation-written-policy", "hr-lactation-room-requirements", "hr-lactation-multiemployer-worksite-2-business-days",
    "hr-lactation-under-50-undue-hardship", "hr-lactation-no-documentation")
HARASS, LAC_PAY = "harassment-prevention-training", "lactation-accommodation"
RETAL_DEADLINE, RETAL_PEN = "hr-retaliation-complaint-deadlines", "hr-retaliation-98-6-civil-penalty-10000"
# pay transparency
PAY_POST, PAY_REQ, SAL_HIST, PAY_PEN, PAY_REC = (
    "hr-pay-scale-third-party-postings-and-count", "hr-pay-scale-on-request", "hr-salary-history-ban",
    "hr-pay-transparency-civil-penalty", "hr-pay-equity-records-retention")
# ACA
ALE, FTE, SEASONAL, NEW_ALE, FT30 = ("hr-aca-ale-definition-50-ftes", "hr-aca-fte-calculation-120-hours",
                                     "hr-aca-seasonal-worker-exception-120-days", "hr-aca-new-employer-ale-status",
                                     "hr-aca-full-time-employee-30-hours-130-monthly")
HOURS_DEF, PEN_A, OFFER95, PEN_B = ("hr-aca-hours-of-service-definition", "hr-aca-4980h-a-no-offer-penalty",
                                    "hr-aca-95-percent-offer-rule", "hr-aca-4980h-b-unaffordable-coverage-penalty")
APRIL1, LOOKBACK, REHIRE13, AFFORD = ("hr-aca-first-year-ale-april-1-offer", "hr-aca-look-back-measurement-periods",
                                      "hr-aca-rehire-13-week-break", "hr-aca-affordability-safe-harbors")
FILE_1094, FURNISH_1095, EFILE10, LATE_RET = ("hr-aca-1094c-1095c-filing-deadline", "hr-aca-1095c-furnish-deadline",
                                              "hr-aca-efile-10-return-threshold", "hr-info-return-late-filing-penalty-base")
WAIT90 = "hr-group-health-90-day-waiting-period"
# COBRA
COBRA20, COBRA_ADMIN30, COBRA_ELECT_NOTICE, COBRA_GENERAL = (
    "hr-cobra-small-employer-exception-20", "hr-cobra-employer-notice-to-administrator-30-days",
    "hr-cobra-election-notice-14-or-44-days", "hr-cobra-general-notice-90-days")
COBRA_ELECT60, COBRA_MAX, COBRA_PREM, COBRA_PAY = (
    "hr-cobra-election-period-60-days", "hr-cobra-maximum-coverage-18-29-36-months", "hr-cobra-premium-102-percent",
    "hr-cobra-payment-grace-30-45-days")
# FMLA
FMLA_COV, FMLA_ELIG, FMLA_SITE, FMLA_12, FMLA_26, FMLA_NOTICE, FMLA_POSTER = (
    "hr-fmla-covered-employer-50", "hr-fmla-eligible-employee-12-months-1250-hours-75-miles",
    "hr-fmla-construction-worksite-home-base", "hr-fmla-12-workweeks", "hr-fmla-26-workweeks-military-caregiver",
    "hr-fmla-notices-5-business-days", "hr-fmla-poster-penalty-216")

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str, default=KeyError):
    r = core.rule(rule_id)
    return r.param(key) if default is KeyError else r.param(key, default)


def _verified(*rule_ids: str) -> bool:
    return all(core.rule(r).verified for r in rule_ids)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _num(x: float) -> str:
    """12.0 -> '12', 12.5 -> '12.5' for math lines."""
    return f"{round(x, 4):g}"


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")


def _date(value, field: str) -> Date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    s = re.sub(r"\s+", " ", str(value or "").strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{field} must be a date like 2026-11-25 or 11/25/2026, got {value!r}")


def _opt_date(value, field: str) -> Optional[Date]:
    return None if value is None or (isinstance(value, str) and not value.strip()) else _date(value, field)


def _today(as_of, field: str = "as_of") -> Date:
    return _date(as_of, field) if as_of not in (None, "") else logic.today_ca()


def _iso(value: str) -> Date:
    return Date.fromisoformat(str(value)[:10])


def _count(value, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number, got {value!r}")
    try:
        f = float(str(value).strip().replace(",", "")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number, got {value!r}") from None
    if not math.isfinite(f) or f != int(f) or f < minimum:
        raise ValueError(f"{field} must be a whole number of at least {minimum}, got {value!r}")
    return int(f)


def _amount(value, field: str, minimum: float = 0.0, strict: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, got {value!r}")
    try:
        f = float(str(value).strip().replace(",", "").lstrip("$")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}") from None
    if not math.isfinite(f) or f < minimum or (strict and f == minimum):
        raise ValueError(f"{field} must be a finite number {'greater than' if strict else 'at least'} {minimum:g}, "
                         f"got {value!r}")
    return f


def _opt_count(value, field: str) -> Optional[int]:
    return None if value is None else _count(value, field)


def _opt_amount(value, field: str, strict: bool = False) -> Optional[float]:
    return None if value is None else _amount(value, field, strict=strict)


def _add_months(d: Date, months: int) -> Date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    nxt = Date(y + (m == 12), m % 12 + 1, 1)
    return Date(y, m, min(d.day, (nxt - timedelta(days=1)).day))


def _roll_weekend(d: Date) -> Date:
    """26 CFR 301.7503-1: a due date on a Saturday or Sunday moves to the next weekday (holidays not modelled)."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _add_business_days(d: Date, n: int) -> Date:
    """n Monday-Friday days after d. Holidays are not skipped, so the date is never later than the real deadline."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _month_day(text: str) -> tuple[int, int]:
    """'February 28 of the following year' -> (2, 28): due dates are read from the rule text, not typed here."""
    m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
                  r"(\d{1,2})\b", str(text))
    if not m:
        raise RuntimeError(f"rulebook text {text!r} has no month-day date")
    return datetime.strptime(m[1], "%B").month, int(m[2])


def _isos(text: str) -> list[Date]:
    return [_iso(s) for s in re.findall(r"\d{4}-\d{2}-\d{2}", str(text))]


def _norm_words(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower().replace("_", " ").replace("-", " "))
    return re.sub(r"\s+", " ", s).strip()


class Deadline(BaseModel):
    item: str
    due_date: Optional[str] = Field(description="ISO date; null when it depends on a date you did not give")
    weekday: Optional[str] = None
    days_left: Optional[int] = Field(None, description="Days from as_of to the due date (negative = past)")
    status: Literal["upcoming", "due_today", "overdue", "done_on_time", "done_late", "info"]
    math: str
    rule_ids: list[str]


def _deadline(item: str, due: Optional[Date], as_of: Date, math_: str, rule_ids: list[str],
              done_on: Optional[Date] = None, info: bool = False) -> Deadline:
    if due is None:
        return Deadline(item=item, due_date=None, status="info", math=math_, rule_ids=rule_ids)
    left = (due - as_of).days
    if done_on is not None:
        status = "done_on_time" if done_on <= due else "done_late"
    elif info:
        status = "info"
    else:
        status = "overdue" if left < 0 else "due_today" if left == 0 else "upcoming"
    return Deadline(item=item, due_date=due.isoformat(), weekday=due.strftime("%A"), days_left=left, status=status,
                    math=math_, rule_ids=rule_ids)


# ================================================================ 1. jobsite minimum wage
JURIS_SD, JURIS_SD_COUNTY, JURIS_OTHER = "city_of_san_diego", "san_diego_county_outside_city", "other_california"
# The other 17 incorporated cities of San Diego County (geography, not law). Rule hr-local-mw-san-diego-2026 says the
# state minimum wage applies in them and in the unincorporated county.
SD_COUNTY_CITIES = frozenset({
    "carlsbad", "chula vista", "coronado", "del mar", "el cajon", "encinitas", "escondido", "imperial beach",
    "la mesa", "lemon grove", "national city", "oceanside", "poway", "san marcos", "santee", "solana beach", "vista"})
_SD_NAMES = {"san diego", "sd", "sandiego", "s d", "cosd"}


def normalize_city(city: str) -> tuple[str, str]:
    """'SD', 'City of San Diego', 'san diego, ca' -> ('City of San Diego', 'city_of_san_diego'); 'San Diego County',
    'unincorporated SD county' -> county; 'Chula Vista' -> other San Diego County city; anything else -> other."""
    raw = str(city or "").strip()
    t = _norm_words(raw)
    if not t:
        raise ValueError("city is required, e.g. 'San Diego' (the city where the hours are worked)")
    for _ in range(2):
        t = re.sub(r"\s+(?:ca|calif|california|usa|us)$", "", t).strip()
    t = re.sub(r"^the\s+", "", t)
    county = False
    if re.match(r"^unincorporated\b", t):
        county, t = True, re.sub(r"^unincorporated\s+(?:area\s+of\s+)?", "", t)
    if re.match(r"^county\s+of\s+", t):
        county, t = True, re.sub(r"^county\s+of\s+", "", t)
    t = re.sub(r"^(?:city|town)\s+of\s+", "", t)
    if t.endswith(" county"):
        county, t = True, t[: -len(" county")].strip()
    if t.endswith(" unincorporated"):
        county, t = True, t[: -len(" unincorporated")].strip()
    if t.endswith(" city") and t[: -len(" city")] in _SD_NAMES:
        t = t[: -len(" city")]
    if t in _SD_NAMES:
        return ("San Diego County, outside the City of San Diego", JURIS_SD_COUNTY) if county else \
            ("City of San Diego", JURIS_SD)
    if t in SD_COUNTY_CITIES:
        return f"{t.title()} (San Diego County)", JURIS_SD_COUNTY
    return raw, JURIS_OTHER


def _state_timeline() -> list[tuple[Date, float, str]]:
    r26, r27 = core.rule(MW26), core.rule(MW27)
    tl = {_iso(k): (float(v), MW26) for k, v in r26.param("prior_rates_usd").items()}
    tl[_iso(r26.param("effective"))] = (float(r26.param("hourly_usd")), MW26)
    tl[_iso(r27.param("effective"))] = (float(r27.param("hourly_usd")), MW27)
    return sorted((d, usd, rid) for d, (usd, rid) in tl.items())


def _sd_timeline() -> list[tuple[Date, float, str]]:
    tl = {_iso(k): (float(v), SD26) for k, v in P(SD26, "history_usd").items()}
    tl[_iso(P(SD26, "effective"))] = (float(P(SD26, "hourly_usd")), SD26)
    tl[_iso(P(SD27, "effective"))] = (float(P(SD27, "hourly_usd")), SD27)
    return sorted((d, usd, rid) for d, (usd, rid) in tl.items())


def _rate_on(timeline, d: Date):
    hit = [x for x in timeline if x[0] <= d]
    return hit[-1] if hit else None


def state_minimum_wage_on(d: Date) -> tuple[float, Date, str]:
    """(hourly, effective date, rule id). The state rate changes each January 1 (rule hr-posting-replace-when-content-
    changes), so the newest verified rate is good through December 31 of its year and no further."""
    tl = _state_timeline()
    last = tl[-1][0]
    if d < tl[0][0] or d > Date(last.year, 12, 31):
        raise ValueError(f"no verified California minimum wage for {d.isoformat()}: the rulebook has rates from "
                         f"{tl[0][0].isoformat()} through {last.year}-12-31 (the rate changes each January 1)")
    eff, usd, rid = _rate_on(tl, d)
    return usd, eff, rid


class RateLine(BaseModel):
    name: str
    hourly_usd: Optional[float]
    effective: Optional[str] = None
    applies: bool
    why: str
    rule_ids: list[str]


class SpecialRate(BaseModel):
    name: str
    applies: bool
    basic_hourly_usd: Optional[float] = Field(None, description="Cash wage part of the rate")
    benefits_hourly_usd: Optional[float] = Field(None, description="Employer payments (fringe / health benefits) part")
    total_hourly_usd: Optional[float] = Field(None, description="Full rate if all of it is paid as cash")
    overtime_saturday_usd: Optional[float] = None
    sunday_holiday_usd: Optional[float] = None
    required_cash_hourly_usd: Optional[float] = Field(
        None, description="Cash per hour you must pay given the benefits you said you pay per hour")
    period: Optional[str] = None
    why: str
    rule_ids: list[str]


class ChangeAhead(BaseModel):
    date: str
    days_away: int
    what: str
    hourly_usd: Optional[float] = None
    rule_ids: list[str]


class ExemptSalary(BaseModel):
    year: int
    annual_usd: float
    monthly_usd: float
    weekly_usd: float
    math: str
    rule_ids: list[str]


class PayCheck(BaseModel):
    hourly_rate_paid_usd: float
    benefits_paid_usd_per_hour: float
    required_hourly_usd: float = Field(description="Cash per hour the binding rate requires")
    compliant: bool
    shortfall_per_hour_usd: float
    hours: Optional[float] = None
    shortfall_usd: Optional[float] = Field(None, description="Shortfall x hours, when hours were given")
    math: str


class JobsiteWageResult(core.Result):
    city_input: str
    jurisdiction: str
    jurisdiction_kind: Literal["city_of_san_diego", "san_diego_county_outside_city", "other_california"]
    on_date: str
    state_minimum_wage_usd: float
    state_effective: str
    local_minimum_wage_usd: Optional[float] = None
    local_status: Literal["applies", "not_covered", "no_local_ordinance", "not_in_rulebook"]
    required_hourly_usd: Optional[float] = Field(description=(
        "General minimum wage owed per hour for this work (higher of state and local); null when the local rate is "
        "unknown, the worker is exempt salaried, or the work is inside a federal enclave"))
    binding_rate: str = Field(description="Which rate controls the pay for this work")
    binding_hourly_usd: Optional[float] = Field(description="Cash per hour owed under the controlling rate")
    rates: list[RateLine]
    special_rates: list[SpecialRate] = Field(default_factory=list)
    changes_ahead: list[ChangeAhead] = Field(default_factory=list)
    exempt_salary: Optional[ExemptSalary] = None
    hand_tools_threshold_hourly_usd: float = Field(description=(
        "Workers paid less than this must be given all tools; at or above it they may be required to bring "
        "customary hand tools (never power tools or a vehicle)"))
    pay_check: Optional[PayCheck] = None
    postings: list[str] = Field(default_factory=list)
    math: list[str] = Field(default_factory=list)
    next_step: str
    notes: list[str] = Field(default_factory=list)


def _exempt_salary(year: int) -> Optional[ExemptSalary]:
    if year == _iso(P(EX27, "effective")).year:
        a, m, w = P(EX27, "annual_usd"), P(EX27, "derived_monthly_usd"), P(EX27, "derived_weekly_usd")
        return ExemptSalary(year=year, annual_usd=a, monthly_usd=m, weekly_usd=w, rule_ids=[EX27, MW27],
                            math=f"{P(EX27, 'formula')} = {_usd(a)}/year; /12 = {_usd(m)}/month; /52 = {_usd(w)}/week")
    if year == _iso(P(MW26, "effective")).year:
        d = P(EX26, "derived_2026_usd")
        k, h = P(EX26, "multiplier_of_state_min_wage"), P(EX26, "full_time_hours_per_week")
        return ExemptSalary(year=year, annual_usd=d["annual"], monthly_usd=d["monthly"], weekly_usd=d["weekly"],
                            rule_ids=[EX26, MW26],
                            math=f"{_num(k)} x {_usd(P(MW26, 'hourly_usd'))} x {_num(h)} h x 52 weeks = "
                                 f"{_usd(d['annual'])}/year ({_usd(d['monthly'])}/month, {_usd(d['weekly'])}/week)")
    return None


def _tcw_rate(permit_day: Date):
    """(basic, benefits, total, ot_sat, sun_hol, period text) for a permit issued on permit_day, or None."""
    w = core.rule(TCW)
    cur = w.param("rates_effective")
    if _iso(cur["start"]) <= permit_day <= _iso(cur["end"]):
        return (w.param("basic_hourly_usd"), w.param("employer_payments_usd"), w.param("total_hourly_usd"),
                w.param("overtime_saturday_usd"), w.param("sunday_holiday_usd"), f"{cur['start']} to {cur['end']}")
    for h in w.param("history"):
        a, b = h["period"].split("/")
        if _iso(a) <= permit_day <= _iso(b):
            return h["basic"], h["employer_payments"], h["total"], None, None, f"{a} to {b}"
    inc = w.param("predetermined_increase")
    if permit_day >= _iso(inc["effective"]):
        total = _money(w.param("total_hourly_usd") + inc["usd_per_hour"])
        return None, None, total, None, None, f"from {inc['effective']} (end date not published in the rulebook)"
    return None


def jobsite_minimum_wage(city: str, on_date=None, hours_in_city_this_week: Optional[float] = None,
                         exempt_salaried: bool = False, inside_federal_enclave: bool = False,
                         traffic_control_permit: bool = False, permit_issue_date=None,
                         single_family_or_duplex: bool = False, project_cost_usd: Optional[float] = None,
                         city_living_wage_contract: bool = False, hourly_rate_paid_usd: Optional[float] = None,
                         benefits_paid_usd_per_hour: float = 0.0) -> JobsiteWageResult:
    d = _today(on_date, "on_date")
    name, kind = normalize_city(city)
    hours = _opt_amount(hours_in_city_this_week, "hours_in_city_this_week")
    if hours is not None and hours > 168:
        raise ValueError(f"hours_in_city_this_week cannot exceed 168 (hours in a week), got {hours:g}")
    paid = _opt_amount(hourly_rate_paid_usd, "hourly_rate_paid_usd")
    benefits = _amount(benefits_paid_usd_per_hour or 0, "benefits_paid_usd_per_hour")
    cost = _opt_amount(project_cost_usd, "project_cost_usd")
    if traffic_control_permit and kind != JURIS_SD:
        raise ValueError("traffic_control_permit is for City of San Diego traffic control permits; set city to "
                         "'San Diego' (the permitted work is inside the city)")
    state_usd, state_eff, state_rid = state_minimum_wage_on(d)
    cite = {state_rid, MW_LOCAL}
    rates = [RateLine(name="California minimum wage", hourly_usd=state_usd, effective=state_eff.isoformat(),
                      applies=not exempt_salaried and not inside_federal_enclave,
                      why=("Not an hourly test for exempt salaried staff: see exempt_salary" if exempt_salaried else
                           "Inside a federal enclave state law may not apply (not covered by this rulebook)"
                           if inside_federal_enclave else "Every California employer, any size, for all hours worked"),
                      rule_ids=[state_rid])]
    math_, notes, postings = [], [], []
    local_usd, local_status = None, "not_in_rulebook"
    if kind == JURIS_SD:
        sd = _rate_on(_sd_timeline(), d)
        min_h = P(SD26, "coverage_min_hours_in_city_per_week")
        cite |= {SD26, SD_POST}
        if sd is None:
            local_status = "no_local_ordinance"
            notes.append(f"The City of San Diego minimum wage began {_sd_timeline()[0][0].isoformat()}; none applied "
                         f"on {d.isoformat()}.")
        else:
            sd_eff, local_usd, sd_rid = sd
            cite.add(sd_rid)
            excluded = None
            if inside_federal_enclave:
                excluded = "work inside a federal enclave (e.g., a military base)"
            elif exempt_salaried:
                excluded = "employees not entitled to the California minimum wage (exempt salaried staff)"
            elif hours is not None and hours < min_h:
                excluded = f"fewer than {_num(min_h)} hours worked in the city this week ({_num(hours)} h)"
            local_status = "not_covered" if excluded else "applies"
            rates.append(RateLine(
                name="City of San Diego minimum wage", hourly_usd=local_usd, effective=sd_eff.isoformat(),
                applies=excluded is None, rule_ids=list(dict.fromkeys([sd_rid, SD26])),
                why=(f"Not covered: {excluded}" if excluded else
                     f"Hours worked inside San Diego city limits by an employee who works at least {_num(min_h)} h in "
                     f"a week in the city; any employer size (small business exemption: "
                     f"{'yes' if P(SD26, 'small_business_exemption') else 'none'}); no tip credit")))
            if hours is None and not excluded:
                notes.append(f"The City rate covers employees who work at least {_num(min_h)} hours in a week inside "
                             f"city limits; give hours_in_city_this_week to test it.")
            postings.append(f"City of San Diego yearly minimum wage and earned sick leave notices at every San Diego "
                            f"jobsite, plus a written notice at hire (languages: "
                            f"{', '.join(P(SD_POST, 'languages_available'))}) [{SD_POST}]")
    elif kind == JURIS_SD_COUNTY:
        local_status = "no_local_ordinance"
        cite.add(SD26)
        notes.append(f"{name}: {P(SD26, 'unincorporated_or_other_cities_in_san_diego_county')} (the City of San Diego "
                     f"ordinance covers only hours worked inside San Diego city limits) [{SD26}].")
    else:
        notes.append(f"{name}: this rulebook has verified local minimum wages only for the City of San Diego. Many "
                     f"California cities and counties set a higher rate; check the official city or county page "
                     f"for the jobsite and pay the higher rate [{MW_LOCAL}].")

    if inside_federal_enclave:
        required = None
        notes.append("Federal enclave: the City of San Diego ordinance does not reach work there (OLSE FAQ). Whether "
                     "the state minimum wage applies inside a particular enclave is not covered by this rulebook; "
                     "check the federal contract's wage terms.")
    elif exempt_salaried:
        required = None
    elif local_status == "applies":
        required = max(state_usd, local_usd)
        math_.append(f"required = max(state {_usd(state_usd)}, City of San Diego {_usd(local_usd)}) = {_usd(required)}"
                     f" [{MW_LOCAL}]")
    elif kind == JURIS_OTHER:
        required = None
        math_.append(f"state floor {_usd(state_usd)}; local rate for {name} not in the rulebook, so the required "
                     f"rate is unknown (it is at least {_usd(state_usd)})")
    else:
        required = state_usd
        math_.append(f"required = state rate {_usd(state_usd)} (no local ordinance for this location)")

    # ---- special rates
    special: list[SpecialRate] = []
    if required is not None:
        binding = "California minimum wage" if required == state_usd else "City of San Diego minimum wage"
    elif exempt_salaried:
        binding = "exempt salary threshold (not an hourly rate)"
    elif inside_federal_enclave:
        binding = "federal contract wage terms (not in the rulebook)"
    else:
        binding = f"local minimum wage for {name} (not in the rulebook)"
    binding_usd = required
    if traffic_control_permit:
        pday = _opt_date(permit_issue_date, "permit_issue_date") or d
        exc = {e["type"]: e for e in P(TCW_EXC, "exceptions")}
        max_cost = exc["project cost"]["max_project_cost_usd"]
        cite |= {TCW, TCW_EXC, TCW_NOTICE}
        why_not = None
        if single_family_or_duplex:
            why_not = "single-family home or duplex construction, renovation or repair is excepted"
        elif cost is not None and cost <= max_cost:
            why_not = f"project cost {_usd(cost)} is {_usd(max_cost)} or less"
        rate = _tcw_rate(pday)
        if rate is None:
            special.append(SpecialRate(name="San Diego traffic control worker wage", applies=why_not is None,
                                       why=f"No rate in the rulebook for a permit issued {pday.isoformat()}",
                                       rule_ids=[TCW, TCW_EXC]))
        else:
            basic, ben, total, ot, sun, period = rate
            req_cash = None
            if why_not is None:
                req_cash = _money(total - min(benefits, ben)) if ben is not None else _money(total - benefits)
                if basic is not None:
                    req_cash = max(req_cash, basic)
                binding, binding_usd = "San Diego traffic control worker wage", req_cash
                credit = min(benefits, ben) if ben is not None else benefits
                math_.append(f"flagger rate (permit issued {pday.isoformat()}, period {period}): total "
                             f"{_usd(total)}" + (f" = basic {_usd(basic)} + benefits {_usd(ben)}" if basic else "")
                             + f"; cash owed = {_usd(total)} - benefits paid {_usd(credit)} = {_usd(req_cash)}"
                             + (f" (never below basic {_usd(basic)})" if basic else "") + f" [{TCW}]")
            special.append(SpecialRate(
                name="San Diego traffic control worker wage", applies=why_not is None, basic_hourly_usd=basic,
                benefits_hourly_usd=ben, total_hourly_usd=total, overtime_saturday_usd=ot, sunday_holiday_usd=sun,
                required_cash_hourly_usd=req_cash, period=period, rule_ids=[TCW, TCW_EXC],
                why=(f"Not covered: {why_not}" if why_not else
                     f"Traffic control work required by a City of San Diego traffic control permit; "
                     f"{P(TCW, 'basis')}; {P(TCW, 'rate_locked_at')}")))
            if why_not is None:
                postings.append(f"Post the flagger wage rates (with effective dates and scheduled increases) at each "
                                f"job site, or give each affected worker a written notice [{TCW_NOTICE}]")
                inc = P(TCW, "predetermined_increase")
                if pday < _iso(inc["effective"]):
                    notes.append(f"A predetermined increase of {_usd(inc['usd_per_hour'])}/hour ({inc['allocation']}) "
                                 f"takes effect {inc['effective']}. The City notice fixes the rate 'including any "
                                 f"predetermined increase' at permit issuance, so a permit still active on "
                                 f"{inc['effective']} may owe it from that date; confirm with OLSE [{TCW}].")
                notes.append(f"Other exceptions (not tested here): gas or utility crews doing "
                             f"{_num(exc['gas or utility company']['max_traffic_control_hours_per_workday_or_shift'])} "
                             f"hour or less of traffic control a shift, public works projects requiring a higher "
                             f"rate, and funding sources that prohibit the rate [{TCW_EXC}].")
                notes.append(f"Proposed City fee: {_usd(P(TCW_FEE, 'proposed_fee_usd_per_permit'))} per approved "
                             f"permit, paid by the permittee, {P(TCW_FEE, 'status')} [{TCW_FEE}].")
                cite.add(TCW_FEE)
    if city_living_wage_contract:
        cite |= {LW, LW_LEAVE}
        per = P(LW, "period")
        where = P(LW, "within_city") if kind == JURIS_SD else P(LW, "outside_city")
        label = "work inside the city" if kind == JURIS_SD else "work outside the city"
        if _iso(per["start"]) <= d <= _iso(per["end"]):
            credit = min(benefits, where["health_benefit_usd"])
            req = _money(where["full_cash_wage_usd"] - credit)
            floor = max(state_usd, local_usd if local_status == "applies" else 0)
            req = max(req, floor)
            math_.append(f"living wage ({label}): {_usd(where['full_cash_wage_usd'])} - health benefits credited "
                         f"{_usd(credit)} (max {_usd(where['health_benefit_usd'])}) = {_usd(req)}; floor "
                         f"{_usd(floor)} [{LW}]")
            if binding_usd is None or req > binding_usd:
                binding, binding_usd = "San Diego living wage (City contract)", req
            special.append(SpecialRate(
                name="San Diego living wage (City service contracts)", applies=True,
                basic_hourly_usd=where["cash_wage_usd"], benefits_hourly_usd=where["health_benefit_usd"],
                total_hourly_usd=where["full_cash_wage_usd"], required_cash_hourly_usd=req,
                period=f"{per['start']} to {per['end']}", rule_ids=[LW, LW_LEAVE],
                why=f"Covered City service, financial assistance or facility agreement ({label}); {P(LW, 'floor')}"))
            notes.append(f"Living wage contracts also require {_num(P(LW_LEAVE, 'paid_leave_hours_per_year'))} hours "
                         f"of paid leave a year plus {_num(P(LW_LEAVE, 'additional_unpaid_leave_hours'))} unpaid "
                         f"hours, the yearly notice with the first paycheck after July 1, and subcontractor "
                         f"certification within {P(LW_LEAVE, 'subcontractor_certification_days')} days [{LW_LEAVE}].")
            notes.append("Construction is not among the living wage covered services listed by the City; prevailing "
                         f"wage governs public works [{LW}].")
        else:
            special.append(SpecialRate(name="San Diego living wage (City service contracts)", applies=True,
                                       why=f"The rulebook has the living wage for {per['start']} to {per['end']} "
                                           f"only; {P(LW, 'annual_adjustment')}", rule_ids=[LW]))

    # ---- changes ahead
    changes = []
    for eff, usd, rid in _state_timeline():
        if eff > d:
            changes.append(ChangeAhead(date=eff.isoformat(), days_away=(eff - d).days, hourly_usd=usd, rule_ids=[rid],
                                       what=f"California minimum wage becomes {_usd(usd)}/hour"))
            cite.add(rid)
    if kind == JURIS_SD and local_status in ("applies", "not_covered"):
        for eff, usd, rid in _sd_timeline():
            if eff > d:
                changes.append(ChangeAhead(date=eff.isoformat(), days_away=(eff - d).days, hourly_usd=usd,
                                           rule_ids=[rid], what=f"City of San Diego minimum wage becomes {_usd(usd)}/hour"))
                cite.add(rid)
    if traffic_control_permit and any(s.applies and s.total_hourly_usd for s in special):
        inc = P(TCW, "predetermined_increase")
        if _iso(inc["effective"]) > d:
            changes.append(ChangeAhead(date=inc["effective"], days_away=(_iso(inc["effective"]) - d).days,
                                       hourly_usd=_money(P(TCW, "total_hourly_usd") + inc["usd_per_hour"]),
                                       rule_ids=[TCW], what=f"Flagger rate rises {_usd(inc['usd_per_hour'])}/hour for "
                                                            f"permits issued from this date ({inc['allocation']})"))
    changes.sort(key=lambda c: c.date)

    # ---- exempt salary, hand tools
    ex = _exempt_salary(d.year)
    if ex:
        cite |= set(ex.rule_ids)
    elif exempt_salaried:
        notes.append(f"The exempt salary threshold for {d.year} is not in the rulebook.")
    k = P(TOOLS2X, "hand_tool_threshold_multiplier")
    tools = _money(k * state_usd)
    cite |= {TOOLS2X, HAND_TOOLS}
    math_.append(f"hand-tool threshold = {_num(k)} x state minimum {_usd(state_usd)} = {_usd(tools)}/hour "
                 f"[{TOOLS2X}]")

    # ---- pay check
    check = None
    if paid is not None and binding_usd is not None:
        short = _money(max(0.0, binding_usd - paid))
        check = PayCheck(hourly_rate_paid_usd=paid, benefits_paid_usd_per_hour=benefits,
                         required_hourly_usd=binding_usd, compliant=short == 0, shortfall_per_hour_usd=short,
                         hours=hours, shortfall_usd=_money(short * hours) if hours is not None else None,
                         math=f"{binding}: required {_usd(binding_usd)} - paid {_usd(paid)} = shortfall "
                              f"{_usd(short)}/hour" + (f" x {_num(hours)} h = {_usd(short * hours)}" if hours else ""))
    elif paid is not None:
        notes.append("hourly_rate_paid_usd was not checked: the controlling rate is unknown for this work.")

    # ---- postings
    if not exempt_salaried:
        postings.insert(0, f"State minimum wage notice; post it in Spanish too if any worker reads only Spanish "
                           f"[{POST_SPANISH}]. Replace postings when their content changes (the minimum wage notice "
                           f"changes every January 1) [{POST_REPLACE}]")
        cite |= {POST_SPANISH, POST_REPLACE}
    if exempt_salaried and ex:
        notes.append("Exempt status also needs the duties test: " + (P(EX27, "also_required") if EX27 in ex.rule_ids else
                     f"primarily (= {P(EX26, 'primarily_means')}) exempt duties") + ".")

    # ---- next step
    if binding.startswith("San Diego traffic control"):
        nxt = [c for c in changes if c.what.startswith("Flagger")]
    elif binding.startswith("San Diego living wage"):
        nxt = []
        notes.append(f"Living wage: {P(LW, 'annual_adjustment')} [{LW}].")
    else:
        nxt = [c for c in changes if c.hourly_usd is not None and "minimum wage" in c.what]
    if exempt_salaried:
        step = (f"Pay this exempt employee at least {_usd(ex.annual_usd)} a year ({_usd(ex.monthly_usd)}/month) in "
                f"{ex.year} and confirm the duties test." if ex else "Confirm the exempt salary threshold for this "
                "year from DIR before relying on exempt status.")
    elif binding_usd is None:
        step = ("Check the jobsite's official city or county minimum wage page and pay the higher of that rate and "
                f"the state {_usd(state_usd)}." if kind == JURIS_OTHER else
                "Check the federal contract's wage terms for work inside the enclave.")
    else:
        step = f"Pay at least {_usd(binding_usd)}/hour for these hours ({binding})."
        if check and not check.compliant:
            step = (f"Raise pay by {_usd(check.shortfall_per_hour_usd)}/hour to at least {_usd(binding_usd)} "
                    f"({binding})" + (f" and pay the {_usd(check.shortfall_usd)} owed for this week" if
                                      check.shortfall_usd else "") + ".")
    if nxt and not exempt_salaried and binding.startswith("San Diego traffic control"):
        c = nxt[0]
        step += (f" Permits issued from {c.date} ({c.days_away} days) carry {_usd(c.hourly_usd)}/hour in total; a "
                 f"permit still active then may owe the increase from that date (confirm with OLSE).")
    elif nxt and not exempt_salaried:
        top = [c for c in nxt if c.date == nxt[0].date]
        step += (f" Update payroll before {top[0].date} ({top[0].days_away} days): "
                 + "; ".join(c.what for c in top) + ".")
    if kind == JURIS_SD and local_status == "applies":
        step += " Keep the City notices posted at every San Diego jobsite."

    if not exempt_salaried and not inside_federal_enclave:
        notes.append(f"Minimum wage is owed for each hour; pay at the location's rate for the hours worked there "
                     f"[{MW_LOCAL}].")
    res = JobsiteWageResult(
        city_input=str(city), jurisdiction=name, jurisdiction_kind=kind, on_date=d.isoformat(),
        state_minimum_wage_usd=state_usd, state_effective=state_eff.isoformat(), local_minimum_wage_usd=local_usd,
        local_status=local_status, required_hourly_usd=required, binding_rate=binding, binding_hourly_usd=binding_usd,
        rates=rates, special_rates=special, changes_ahead=changes, exempt_salary=ex,
        hand_tools_threshold_hourly_usd=tools, pay_check=check, postings=postings, math=math_, next_step=step,
        notes=notes)
    return res.cite(*sorted(cite))


# ================================================================ 2. employer size mandates
class Mandate(BaseModel):
    topic: str
    applies: Literal["yes", "no", "check"] = Field(description="check = a fact you did not give decides it")
    threshold: str
    your_count: Optional[str] = None
    why: str
    what_to_do: str
    rule_ids: list[str]
    verified: bool
    size_triggered: bool = Field(False, description="True when your headcount is what makes this apply")


class MandatesResult(core.Result):
    as_of: str
    employee_count: int
    mandates: list[Mandate]
    applies_count: int
    check_count: int
    san_diego_enforcement_fee_usd: Optional[float] = Field(None, description=(
        "Yearly City of San Diego minimum wage enforcement fee (in-force rate x employees in the city)"))
    omitted_unverified: list[str] = Field(default_factory=list, description=(
        "Topics left out because include_unverified=false (their rules are unverified)"))
    next_step: str
    notes: list[str] = Field(default_factory=list)


def _m(topic, applies, threshold, why, what, rule_ids, your_count=None, sized=False) -> Mandate:
    return Mandate(topic=topic, applies=applies, threshold=threshold, your_count=your_count, why=why,
                   what_to_do=what, rule_ids=list(dict.fromkeys(rule_ids)), verified=_verified(*rule_ids),
                   size_triggered=sized and applies == "yes")


def _cmp(n: Optional[int], k: int, label: str) -> str:
    return f"{label} {n} {'>=' if n >= k else '<'} {k}" if n is not None else f"{label} not given"


def _cutover(rule_id: str) -> Date:
    """Last violation date still under the Labor Code version (the protection then moved to FEHA)."""
    return max(_isos(P(rule_id, "labor_code_version_applies_to_violations_through")))


def employer_size_mandates(employee_count: int, employees_in_california: Optional[int] = None,
                           employees_at_largest_location: Optional[int] = None, private_employer: bool = True,
                           has_group_health_plan: bool = False,
                           cobra_under_20_on_half_of_business_days_last_year: Optional[bool] = None,
                           fmla_workweeks_with_50_or_more: Optional[int] = None,
                           aca_average_ft_plus_fte_last_year: Optional[float] = None,
                           employees_in_san_diego_city: int = 0, posts_job_openings: bool = True,
                           has_office_staff: bool = False, spanish_only_readers: bool = False,
                           general_contractor: bool = False, include_unverified: bool = True,
                           as_of=None) -> MandatesResult:
    d = _today(as_of)
    n = _count(employee_count, "employee_count", 1)
    ca = n if employees_in_california is None else _count(employees_in_california, "employees_in_california")
    loc = _opt_count(employees_at_largest_location, "employees_at_largest_location")
    sd_n = _count(employees_in_san_diego_city or 0, "employees_in_san_diego_city")
    fmla_wk = _opt_count(fmla_workweeks_with_50_or_more, "fmla_workweeks_with_50_or_more")
    aca_avg = _opt_amount(aca_average_ft_plus_fte_last_year, "aca_average_ft_plus_fte_last_year")
    if ca > n:
        raise ValueError(f"employees_in_california ({ca}) cannot exceed employee_count ({n})")
    if loc is not None and loc > n:
        raise ValueError(f"employees_at_largest_location ({loc}) cannot exceed employee_count ({n})")
    if sd_n > n:
        raise ValueError(f"employees_in_san_diego_city ({sd_n}) cannot exceed employee_count ({n})")
    if fmla_wk is not None and fmla_wk > 53:
        raise ValueError(f"fmla_workweeks_with_50_or_more must be 0-53 (weeks in a year), got {fmla_wk}")
    out: list[Mandate] = []
    omitted: list[str] = []
    notes: list[str] = []
    yn = lambda b: "yes" if b else "no"            # noqa: E731

    # ---- pay and expenses (any size)
    out.append(_m("Expense reimbursement and paycheck deductions", "yes", "any size",
                  "Labor Code 2802 and the deduction rules apply to every employer",
                  "Reimburse necessary work expenses (required uniforms, business use of a personal vehicle at the "
                  "IRS mileage rate for the period, required direct-deposit bank costs, required insurance). Deduct "
                  "only taxes/garnishments, items the worker authorized in writing (insurance, benefit plans) or "
                  "CBA health/welfare/pension contributions; never for accidental breakage or lost equipment. Pay "
                  "for any bond, photo, required medical exam or license physical you require.",
                  [EXP, MILEAGE, DEDUCT, BREAKAGE, BOND_PHOTO, MED_EXAM, DL_PHYSICAL]))
    # ---- hiring
    out.append(_m("Hiring and background checks", "yes", "any size",
                  "These hiring rules apply to every employer",
                  f"Do not ask about or use arrests without conviction, diversion, or dismissed/sealed convictions; "
                  f"do not ask about marijuana possession convictions more than "
                  f"{_num(P(MARIJUANA, 'conviction_age_years_more_than'))} years old; no consumer credit reports "
                  f"except listed positions; use E-Verify only after an offer or when federal law requires it (up to "
                  f"{_usd(P(EVERIFY, 'penalty_max_usd_per_unlawful_use'))} per unlawful use).",
                  [ARREST, MARIJUANA, CREDIT, EVERIFY]))
    # ---- pay transparency
    pmin = P(PAY_POST, "min_employees")
    pay_keep = next(x["years_after_employment"] for x in P(PAY_REC, "retention") if "years_after_employment" in x)
    posting_applies = n >= pmin and ca >= 1
    out.append(_m("Pay transparency", "yes", f"pay scale in job postings at {pmin}+ employees (at least one in "
                  f"California); on-request and salary-history rules at any size",
                  f"{_cmp(n, pmin, 'employees')}; {ca} in California",
                  ("Put the pay scale (good-faith hourly or salary range) in every job posting, including ones "
                   "recruiters and job boards run for you. " if posting_applies and posts_job_openings else
                   "Posting the pay scale is not required below the headcount, but it is allowed. ")
                  + f"Give an employee their position's pay scale on request; never ask applicants for salary "
                    f"history. Keep job title and wage-rate history for employment plus "
                    f"{_num(pay_keep)} years. Penalty "
                    f"{_usd(P(PAY_PEN, 'civil_penalty_min_usd_per_violation'))} to "
                    f"{_usd(P(PAY_PEN, 'civil_penalty_max_usd_per_violation'))} per violation.",
                  [PAY_POST, PAY_REQ, SAL_HIST, PAY_PEN, PAY_REC], your_count=str(n),
                  sized=posting_applies and posts_job_openings))
    # ---- lactation
    lac_k = P(LAC_U50, "employee_count_below")
    lac_rules = [LAC_POLICY, LAC_ROOM, LAC_NODOC, LAC_U50] + ([LAC_GC] if general_contractor else [])
    out.append(_m("Lactation accommodation", "yes", f"any size (undue-hardship exception below {lac_k})",
                  f"{_cmp(n, lac_k, 'employees')}",
                  "Adopt a written lactation policy (hand it out at hire and when someone asks about parental leave; "
                  "put it in the handbook) stating: " + "; ".join(P(LAC_POLICY, "required_contents")) + ". Provide a "
                  "private space that is not a bathroom, near the work area, with a seat, a surface and power, and a "
                  "sink and refrigerator nearby. Never require a doctor's note."
                  + (f" Under {lac_k} employees you may claim undue hardship for break time only for significant "
                     f"difficulty or expense; still make reasonable efforts to provide a private place other than a "
                     f"toilet stall." if n < lac_k else "")
                  + (f" As GC coordinating a multiemployer site, provide lactation space (or a safe spot for the "
                     f"sub to set one up) within {P(LAC_GC, 'deadline_business_days')} business days of a "
                     f"subcontractor's written request." if general_contractor else ""),
                  lac_rules, your_count=str(n)))
    # ---- harassment training (unverified rule)
    if include_unverified:
        hk = P(HARASS, "min_employees")
        out.append(_m("Harassment prevention training", yn(n >= hk), f"{hk}+ employees (rule unverified)",
                      _cmp(n, hk, "employees"),
                      f"Train supervisors {_num(P(HARASS, 'supervisor_hours'))} hours and other employees "
                      f"{_num(P(HARASS, 'nonsupervisor_hours'))} hour every {_num(P(HARASS, 'frequency_years'))} "
                      f"years (Civil Rights Department free course). This rule is not verified against the CRD's own "
                      f"text; confirm timing for new hires and new supervisors with the CRD." if n >= hk else
                      "Not required below the threshold (rule unverified; confirm with the CRD).",
                      [HARASS], your_count=str(n), sized=True))
    else:
        omitted.append("Harassment prevention training")
    # ---- FEHA-moved protections (unverifiable rules)
    if include_unverified:
        cut_jury, cut_court = _cutover(JURY), _cutover(VICTIM_COURT)
        out.append(_m("Jury duty, court witness and crime victim protections", "yes", "any size (no size stated)",
                      f"Moved from the Labor Code to FEHA (Gov. Code 12945.8): violations after {cut_jury.isoformat()}"
                      f" (jury, witness, victim accommodation) and after {cut_court.isoformat()} (victim court "
                      f"proceedings); the current FEHA text was not read",
                      "Let workers take time off for jury service and subpoenaed testimony with reasonable notice, let "
                      "crime victims and their family attend court proceedings, provide reasonable safety "
                      "accommodations to victims of domestic violence, sexual assault or stalking, and accept a "
                      "signed statement as proof of a victim absence. Complaints now go to the Civil Rights "
                      "Department, not the Labor Commissioner.",
                      [JURY, VICTIM_COURT, VICTIM_ACCOM, VICTIM_CERT]))
        vk = P(VICTIM_LEAVE, "min_employees")
        out.append(_m("Crime victim leave (medical care, counseling, safety planning)", yn(n >= vk),
                      f"{vk}+ employees under the old Labor Code 230.1 (FEHA threshold not read)",
                      _cmp(n, vk, "employees") + "; now under FEHA, threshold unverified",
                      "Allow victims time off for medical attention, victim services, counseling or safety planning; "
                      "confirm current FEHA rules with the Civil Rights Department." if n >= vk else
                      "Below the old Labor Code threshold; the FEHA threshold was not read, so confirm with the CRD.",
                      [VICTIM_LEAVE], your_count=str(n), sized=True))
    else:
        omitted += ["Jury duty, court witness and crime victim protections",
                    "Crime victim leave (medical care, counseling, safety planning)"]
    # ---- school
    out.append(_m("School appearance at a teacher's request", "yes", "no size threshold stated",
                  "Labor Code 230.7 states no employer size",
                  "Let a parent or guardian go to school when the child's teacher asks, after reasonable notice.",
                  [SCHOOL_TEACHER]))
    sk = P(SCHOOL_ACT, "min_employees")
    if loc is not None:
        s_app, s_why = yn(loc >= sk), _cmp(loc, sk, "employees at your largest location")
    elif n < sk:
        s_app, s_why = "no", f"total employees {n} < {sk}, so no location can reach {sk}"
    else:
        s_app, s_why = "check", f"{n} employees in total; give employees_at_largest_location (the count is per location)"
    out.append(_m("School activities and child care leave", s_app, f"{sk}+ employees working at the same location",
                  s_why, f"Allow up to {_num(P(SCHOOL_ACT, 'max_hours_per_year'))} hours a year, no more than "
                         f"{_num(P(SCHOOL_ACT, 'max_hours_per_calendar_month'))} in a calendar month (no monthly cap "
                         f"for school or child care emergencies), for school activities, enrolling a child in school "
                         f"or licensed child care, and emergencies, after reasonable notice.",
                  [SCHOOL_ACT], your_count=str(loc if loc is not None else n), sized=True))
    # ---- emergency responders
    ek = P(EMERG_TRAIN, "min_employees")
    out.append(_m("Volunteer firefighter / reserve officer / emergency rescue duty", "yes", "no size threshold stated",
                  "Labor Code 230.3 states no employer size",
                  "Do not discipline workers who take time off for emergency duty as volunteer firefighters, "
                  "reserve peace officers or emergency rescue personnel.", [EMERG_DUTY]))
    out.append(_m("Emergency responder training leave", yn(n >= ek), f"{ek}+ employees", _cmp(n, ek, "employees"),
                  f"Allow up to {_num(P(EMERG_TRAIN, 'max_days_per_calendar_year'))} days per calendar year for "
                  f"{P(EMERG_TRAIN, 'purpose')}." if n >= ek else "Not required below the threshold.",
                  [EMERG_TRAIN], your_count=str(n), sized=True))
    # ---- rehab / literacy (private employers, regularly employing)
    for rid, topic, what in ((REHAB, "Alcohol or drug rehabilitation accommodation",
                              "Reasonably accommodate a worker who voluntarily enters rehab (usually time off, unpaid, "
                              "but accrued sick leave may be used) unless it is an undue hardship; keep it private."),
                             (LITERACY, "Adult literacy program accommodation",
                              "Reasonably accommodate and assist a worker who asks for help enrolling in an adult "
                              "literacy program; keep it private; time off need not be paid.")):
        k = P(rid, "min_employees")
        app = n >= k and private_employer
        out.append(_m(topic, yn(app), f"private employers regularly employing {k}+",
                      _cmp(n, k, "employees") + ("" if private_employer else "; not a private employer"),
                      what if app else "Not required (below the threshold or not a private employer).", [rid],
                      your_count=str(n), sized=True))
    out.append(_m("Organ and bone marrow donor leave", "check", "size threshold not in the rulebook",
                  "The leave is paid and retaliation is prohibited (Labor Code 1508-1513); the employer size and day "
                  "limits are in Labor Code 1508-1511, which was not read",
                  "Check Labor Code 1508-1511 for your size before denying a donor leave request; never retaliate.",
                  [DONOR]))
    # ---- COBRA / health plan
    if has_group_health_plan:
        ck = P(COBRA20, "exempt_if_fewer_than_employees")
        if cobra_under_20_on_half_of_business_days_last_year is None:
            c_app = "check"
            c_why = (f"{n} employees now; the test is {P(COBRA20, 'test')} "
                     + (f"(likely covered at {n})" if n >= ck else f"(likely exempt at {n})"))
        else:
            c_app = yn(not cobra_under_20_on_half_of_business_days_last_year)
            c_why = ("fewer than " if cobra_under_20_on_half_of_business_days_last_year else f"{ck} or more ") + \
                    (f"{ck} employees on at least half of last year's typical business days"
                     if cobra_under_20_on_half_of_business_days_last_year else
                     "employees on more than half of last year's typical business days")
        out.append(_m("Federal COBRA continuation coverage", c_app, f"{ck}+ employees (normally employed last year)",
                      c_why, "Give the COBRA general notice to new enrollees and notify the plan administrator of "
                             "qualifying events on time (run cobra_deadlines for an event). Smaller plans may fall under "
                             "Cal-COBRA, whose rules are not in this rulebook.", [COBRA20, COBRA_GENERAL],
                      your_count=str(n), sized=True))
        out.append(_m("Group health plan waiting period", "yes", "any employer with a group health plan",
                      "You said you sponsor a group health plan",
                      f"Coverage must be able to start no later than {P(WAIT90, 'max_waiting_period_days')} days "
                      f"after an employee becomes eligible; an orientation period may not exceed "
                      f"{P(WAIT90, 'orientation_period_max').split(' (')[0]}.", [WAIT90]))
    # ---- FMLA
    fk, fw = P(FMLA_COV, "min_employees"), P(FMLA_COV, "min_workweeks")
    if fmla_wk is not None:
        f_app, f_why = yn(fmla_wk >= fw), f"{fmla_wk} workweeks with {fk}+ employees {'>=' if fmla_wk >= fw else '<'} {fw}"
    elif n < fk:
        f_app, f_why = "no", f"{n} employees < {fk} (unless you had {fk}+ for {fw} workweeks last year)"
    else:
        f_app, f_why = "check", (f"{n} employees now; covered if {fk}+ on each working day of {fw}+ calendar workweeks "
                                 f"this year or last (give fmla_workweeks_with_50_or_more)")
    out.append(_m("Federal FMLA", f_app, f"{fk}+ employees for {fw}+ workweeks in the current or preceding year", f_why,
                  f"Post the FMLA poster even if nobody is eligible yet (willful failure up to "
                  f"{_usd(P(FMLA_POSTER, 'civil_money_penalty_max_usd_per_offense'))} per offense), answer leave "
                  f"requests with eligibility and designation notices within "
                  f"{P(FMLA_NOTICE, 'eligibility_notice_business_days')} business days, and test each worker's "
                  f"eligibility ({P(FMLA_ELIG, 'min_months_employed')} months, "
                  f"{P(FMLA_ELIG, 'min_hours_prior_12_months'):,} hours, "
                  f"{P(FMLA_ELIG, 'min_employees_within_75_miles')} employees within "
                  f"{P(FMLA_ELIG, 'radius_miles')} miles of their home base). CFRA (state) has its own rules, not in "
                  f"this rulebook." if f_app != "no" else
                  "Not covered by federal FMLA. California CFRA has different coverage rules that are not in this "
                  "rulebook; check them before denying family or medical leave.",
                  [FMLA_COV, FMLA_POSTER, FMLA_NOTICE, FMLA_ELIG], your_count=str(n), sized=True))
    # ---- ACA
    ak = P(ALE, "min_full_time_employees_including_ftes")
    if aca_avg is not None:
        whole = math.floor(aca_avg + 1e-9)
        a_app, a_why = yn(whole >= ak), f"average {_num(aca_avg)} rounds down to {whole} {'>=' if whole >= ak else '<'} {ak}"
    elif n < ak:
        a_app, a_why = "no", (f"{n} employees < {ak}: each person counts as at most one full-time employee or FTE, so "
                              f"you are not an ALE if your headcount also stayed under {ak} every month last year")
    else:
        a_app, a_why = "check", f"{n} employees now; run aca_employer_status with last year's monthly hours"
    out.append(_m("ACA employer mandate (applicable large employer)", a_app,
                  f"average {ak}+ full-time employees including FTEs in the preceding calendar year", a_why,
                  f"Offer affordable, minimum-value coverage to all but "
                  f"{_num(P(OFFER95, 'max_not_offered_fraction') * 100)}% (or "
                  f"{P(OFFER95, 'max_not_offered_min_count')}, if more) of full-time employees and their children, "
                  "and file Forms 1094-C/1095-C (run aca_employer_status for the math and dates)." if a_app != "no"
                  else "Not an ALE for this year; recheck each year with last year's hours.", [ALE, FTE],
                  your_count=str(n), sized=True))
    if a_app != "no":
        out[-1].rule_ids.append(OFFER95)
        out[-1].verified = _verified(*out[-1].rule_ids)
    # ---- postings
    post_rules = [POST_BINDER, POST_REPLACE] + ([POST_WO4] if has_office_staff else []) + \
                 ([POST_SPANISH] if spanish_only_readers else [])
    out.append(_m("Workplace postings", "yes", "any size", "Every employer must post the required notices",
                  "Post IWC Wage Order 16 for field crews" + (" and Wage Order 4 for your office staff" if
                                                             has_office_staff else "")
                  + "; at a jobsite with only a trailer, a binder is allowed if workers know where it is and can reach "
                    "it without asking; replace a posting when its content changes (minimum wage every January 1)"
                  + ("; post the minimum wage notice in Spanish for workers who read only Spanish" if
                     spanish_only_readers else "") + ".", post_rules))
    # ---- San Diego
    fee = None
    if sd_n > 0:
        fee_rate = P(SD_FEE, "fee_usd_per_employee_per_year")
        fee = _money(fee_rate * sd_n)
        prop = P(SD_FEE, "proposed_fee_usd_per_employee_per_year")
        try:
            sd_rate = _rate_on(_sd_timeline(), d)
            sd_txt = f"pay at least {_usd(sd_rate[1])}/hour for hours worked in the city" if sd_rate else ""
            sd_rid = [sd_rate[2]] if sd_rate else []
        except ValueError:
            sd_txt, sd_rid = "", []
        acc = P(SD_ESL, "accrual_hours_per_hours_worked")
        out.append(_m("City of San Diego minimum wage, earned sick leave and fee", "yes",
                      f"any size; employees working {_num(P(SD26, 'coverage_min_hours_in_city_per_week'))}+ hours a "
                      f"week in the city", f"{sd_n} employees work in the City of San Diego",
                      (sd_txt + "; " if sd_txt else "")
                      + f"sick leave accrues {acc['earned']} hour per {acc['per_hours_worked_in_city']} hours worked in "
                        f"the city (cap {_num(P(SD_ESL, 'accrual_cap_hours'))} h accrued, "
                        f"{_num(P(SD_ESL, 'annual_use_cap_hours'))} h used per year); post the City notices at every "
                        f"jobsite and give the hire notice; keep wage and sick leave records "
                        f"{P(SD_REC, 'retention_years')} years; pay the enforcement fee with your Business Tax "
                        f"Certificate renewal: {_usd(fee_rate)} x {sd_n} = {_usd(fee)} a year (proposed "
                        f"{_usd(prop)} x {sd_n} = {_usd(prop * sd_n)} if adopted; adoption not verified). Penalties "
                        f"up to {_usd(P(SD_PEN, 'penalty_max_usd_per_violation'))} per violation.",
                      [SD26, *sd_rid, SD_ESL, SD_POST, SD_REC, SD_FEE, SD_PEN], your_count=str(sd_n)))
        notes.append(f"The San Diego fee counts {P(SD_FEE, 'employee_count_basis')} [{SD_FEE}].")
    if not has_group_health_plan:
        notes.append("COBRA and the group health plan waiting-period rule apply only if you sponsor a group health plan.")

    notes.append(f"Each law counts employees its own way (regularly employed, at one location, within "
                 f"{P(FMLA_ELIG, 'radius_miles')} miles, on each working day of {fw} workweeks, full-time "
                 f"equivalents); enter the counts each item asks for.")
    yes = [m for m in out if m.applies == "yes"]
    chk = [m for m in out if m.applies == "check"]
    step = f"{len(yes)} of {len(out)} topics apply to you"
    if chk:
        step += f"; {len(chk)} need a fact you did not give ({', '.join(m.topic for m in chk)})"
    sized = [m for m in yes if m.size_triggered]
    if sized:
        step += f". Size-triggered duties you now have: {', '.join(m.topic for m in sized)}"
    step += ". Start with the written lactation policy, postings and pay-scale practices, then the size-triggered items."
    res = MandatesResult(as_of=d.isoformat(), employee_count=n, mandates=out, applies_count=len(yes),
                         check_count=len(chk), san_diego_enforcement_fee_usd=fee, omitted_unverified=omitted,
                         next_step=step, notes=notes)
    return res.cite(*[r for m in out for r in m.rule_ids])


# ================================================================ 3. ACA applicable large employer
def _month_index(key) -> Optional[int]:
    k = _norm_words(key)
    if re.fullmatch(r"\d{1,2}", k) and 1 <= int(k) <= 12:
        return int(k) - 1
    m = re.fullmatch(r"(?:\d{4} )?(\d{1,2})|(\d{1,2}) \d{4}", k)
    if m:
        v = int(m[1] or m[2])
        return v - 1 if 1 <= v <= 12 else None
    for i, mon in enumerate(MONTHS):
        if k.startswith(mon) or re.fullmatch(rf"\d{{4}} {mon}\w*", k) or re.fullmatch(rf"{mon}\w* \d{{4}}", k):
            return i
    return None


class EmployeeMonths(BaseModel):
    name: str = Field(min_length=1, description="Employee name or ID")
    hours: list[float] = Field(description="12 monthly hours of service, January to December (paid leave included)")

    @model_validator(mode="before")
    @classmethod
    def _months_as_keys(cls, v):
        if isinstance(v, dict) and "hours" not in v:
            name = next((v[k] for k in v if _norm_words(k) in ("name", "employee", "employee name", "worker", "id")),
                        None)
            hours = [0.0] * 12
            seen = False
            for k, val in v.items():
                i = _month_index(k)
                if i is not None:
                    hours[i] = 0.0 if val in (None, "") else val
                    seen = True
            if seen:
                return {"name": name, "hours": hours}
        if isinstance(v, dict) and isinstance(v.get("hours"), dict):
            hours = [0.0] * 12
            for k, val in v["hours"].items():
                i = _month_index(k)
                if i is None:
                    raise ValueError(f"month {k!r} is not a month name or number 1-12")
                hours[i] = 0.0 if val in (None, "") else val
            return {**v, "hours": hours}
        return v

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return str(v).strip() if isinstance(v, (str, int)) else v

    @field_validator("hours")
    @classmethod
    def _twelve(cls, v):
        if len(v) != 12:
            raise ValueError(f"hours must have 12 monthly values (January-December), got {len(v)}")
        for h in v:
            if not math.isfinite(h) or h < 0 or h > 744:
                raise ValueError(f"monthly hours must be between 0 and 744 (hours in a 31-day month), got {h:g}")
        return v


def parse_employee_hours(text: str) -> list[EmployeeMonths]:
    """CSV / tab text: a header row with a name column and 12 month columns (Jan..Dec, 1..12 or 2025-01..)."""
    lines = [ln for ln in str(text).strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError("employee_hours text needs a header row and at least one employee row")
    delim = max(("\t", ",", ";"), key=lines[0].count)
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    header = [h.strip() for h in rows[0]]
    month_cols = {i: _month_index(h) for i, h in enumerate(header)}
    month_cols = {i: m for i, m in month_cols.items() if m is not None}
    name_col = next((i for i, h in enumerate(header) if i not in month_cols), None)
    if name_col is None or len(set(month_cols.values())) != 12:
        raise ValueError(f"employee_hours header must have a name column and 12 month columns, got {header}")
    out = []
    for r, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        hours = [0.0] * 12
        for i, m in month_cols.items():
            cell = row[i].strip().replace(",", "") if i < len(row) else ""
            try:
                hours[m] = float(cell) if cell else 0.0
            except ValueError:
                raise ValueError(f"employee_hours row {r}: {cell!r} is not a number of hours") from None
        out.append(EmployeeMonths(name=row[name_col], hours=hours))
    return out


class AcaMonth(BaseModel):
    month: str
    full_time_employees: int = Field(description="Employees who meet the monthly full-time hours test")
    non_full_time_hours_counted: float = Field(description="Hours of the others, capped per person (see math)")
    ftes: float
    total: float = Field(description="Full-time employees + FTEs")
    over_50: bool
    math: str


class PaymentFormula(BaseModel):
    section: Literal["4980H(a)", "4980H(b)"]
    trigger: str
    formula: str
    monthly_usd: Optional[float] = Field(None, description="Only when you supplied the IRS indexed amount")
    annual_usd: Optional[float] = None
    math: str
    rule_ids: list[str]


class AcaResult(core.Result):
    measurement_year: int
    status_year: int
    applicable_large_employer: bool
    months: list[AcaMonth]
    sum_of_monthly_totals: float
    average: float
    average_rounded_down: int
    threshold: int
    seasonal_exception_applied: bool = False
    months_over_50: list[str] = Field(default_factory=list)
    full_time_employees_for_offer: Optional[int] = None
    may_fail_to_offer_at_most: Optional[int] = Field(None, description=(
        "Most full-time employees you can leave without an offer and still meet the offer rule"))
    must_offer_at_least: Optional[int] = None
    payments: list[PaymentFormula] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    must_file_electronically: Optional[bool] = None
    math: list[str] = Field(default_factory=list)
    next_step: str
    notes: list[str] = Field(default_factory=list)


def aca_employer_status(measurement_year: int, employee_hours: Union[list, str, None] = None,
                        monthly_full_time_counts: Optional[list] = None,
                        monthly_non_full_time_hours: Optional[list] = None,
                        excess_over_50_were_seasonal: bool = False, new_employer: bool = False,
                        first_year_as_ale: Optional[bool] = None, current_full_time_employees: Optional[int] = None,
                        indexed_4980h_a_annual_usd: Optional[float] = None,
                        indexed_4980h_b_annual_usd: Optional[float] = None,
                        employees_with_premium_tax_credit: Optional[int] = None,
                        information_returns_count: Optional[int] = None, as_of=None) -> AcaResult:
    d = _today(as_of)
    year = _count(measurement_year, "measurement_year")
    first = _iso(core.rule(ALE).effective).year
    if not first - 1 <= year <= d.year:
        raise ValueError(f"measurement_year must be between {first - 1} and {d.year} (the year whose hours you give), "
                         f"got {year}")
    status_year = year if new_employer else year + 1
    ft_k = P(FT30, "monthly_hours_equivalent")
    cap, div = P(FTE, "max_hours_counted_per_non_full_time_employee_per_month"), P(FTE, "divisor")
    ale_k = P(ALE, "min_full_time_employees_including_ftes")
    notes, math_ = [], []
    # ---- monthly table
    per_emp = None
    if employee_hours is not None and (monthly_full_time_counts is not None or monthly_non_full_time_hours is not None):
        raise ValueError("give employee_hours OR monthly_full_time_counts + monthly_non_full_time_hours, not both")
    if employee_hours is not None:
        per_emp = parse_employee_hours(employee_hours) if isinstance(employee_hours, str) else \
            [e if isinstance(e, EmployeeMonths) else EmployeeMonths.model_validate(e) for e in employee_hours]
        if not per_emp:
            raise ValueError("employee_hours is empty")
        names = [e.name for e in per_emp]
        dup = sorted({x for x in names if names.count(x) > 1})
        if dup:
            raise ValueError(f"employee_hours lists these employees more than once: {dup}")
        ft = [sum(1 for e in per_emp if e.hours[m] >= ft_k) for m in range(12)]
        part = [sum(min(e.hours[m], cap) for e in per_emp if e.hours[m] < ft_k) for m in range(12)]
    elif monthly_full_time_counts is not None and monthly_non_full_time_hours is not None:
        if len(monthly_full_time_counts) != 12 or len(monthly_non_full_time_hours) != 12:
            raise ValueError("monthly_full_time_counts and monthly_non_full_time_hours need 12 values each "
                             "(January-December)")
        ft = [_count(v, f"monthly_full_time_counts[{i + 1}]") for i, v in enumerate(monthly_full_time_counts)]
        part = [_amount(v, f"monthly_non_full_time_hours[{i + 1}]") for i, v in enumerate(monthly_non_full_time_hours)]
        notes.append(f"With aggregate hours, each non-full-time employee's hours must already be capped at {cap} per "
                     f"month before adding them up [{FTE}].")
    else:
        raise ValueError("give employee_hours (per employee, 12 months) or monthly_full_time_counts and "
                         "monthly_non_full_time_hours (12 values each)")
    months = []
    for m in range(12):
        fte = part[m] / div
        tot = ft[m] + fte
        months.append(AcaMonth(month=f"{year}-{m + 1:02d}", full_time_employees=ft[m],
                               non_full_time_hours_counted=round(part[m], 2), ftes=round(fte, 2), total=round(tot, 2),
                               over_50=tot > ale_k,
                               math=f"{ft[m]} FT + {_num(part[m])} h / {div} = {ft[m]} + {fte:.2f} = {tot:.2f}"))
    total_sum = sum(ft[m] + part[m] / div for m in range(12))
    avg = total_sum / 12
    whole = math.floor(avg + 1e-9)
    math_.append(f"average = {total_sum:.2f} / 12 = {avg:.2f}, rounded down = {whole}; ALE if {whole} >= {ale_k} "
                 f"[{ALE}]")
    ale = whole >= ale_k
    cite = [ALE, FTE, FT30, HOURS_DEF]
    over = [mo.month for mo in months if mo.over_50]
    seasonal_applied = False
    if new_employer:
        cite.append(NEW_ALE)
        notes.append(f"New employer: you are an ALE for {year} only if you reasonably expected to and actually do "
                     f"average {P(NEW_ALE, 'min_full_time_employees_including_ftes')}+ in {year}; the hours given are "
                     f"treated as this year's [{NEW_ALE}].")
    if ale and excess_over_50_were_seasonal:
        cite.append(SEASONAL)
        k_mo = P(SEASONAL, "alternative_months")
        if len(over) <= k_mo:
            ale, seasonal_applied = False, True
            math_.append(f"seasonal exception: over {ale_k} in {len(over)} month(s) <= {k_mo} months "
                         f"({P(SEASONAL, 'max_days_over_50')} days), excess were seasonal workers -> not an ALE "
                         f"[{SEASONAL}]")
        else:
            math_.append(f"seasonal exception not available: over {ale_k} in {len(over)} months > {k_mo} "
                         f"[{SEASONAL}]")
    # ---- consequences
    payments, deadlines = [], []
    offer_ft = may_miss = must_offer = None
    efile = None
    if ale:
        cite += [OFFER95, PEN_A, PEN_B, FILE_1094, FURNISH_1095, EFILE10, LATE_RET, LOOKBACK, REHIRE13, AFFORD]
        harbors = ", ".join(k.replace("_", " ").replace("form w2", "Form W-2") for k in P(AFFORD, "safe_harbors"))
        notes.append(f"Affordability safe harbors: {harbors} (cost of the "
                     f"{P(AFFORD, 'coverage_tested')}). The regulation text says "
                     f"{_num(P(AFFORD, 'regulatory_text_percent'))}%, but the IRS adjusts it for each plan year and "
                     f"the {status_year} percentage is not in the rulebook; confirm it before relying on it [{AFFORD}].")
        if current_full_time_employees is not None:
            offer_ft = _count(current_full_time_employees, "current_full_time_employees")
        else:
            offer_ft = ft[-1]
            notes.append(f"full_time_employees_for_offer uses December {year} ({offer_ft}); pass "
                         f"current_full_time_employees for this year's count.")
        frac, min_n = P(OFFER95, "max_not_offered_fraction"), P(OFFER95, "max_not_offered_min_count")
        may_miss = min(offer_ft, math.floor(max(min_n, frac * offer_ft) + 1e-9))
        must_offer = offer_ft - may_miss
        math_.append(f"offer rule: may miss max({min_n}, {_num(frac * 100)}% x {offer_ft} = {_num(frac * offer_ft)}) "
                     f"-> {may_miss}; offer at least {must_offer} of {offer_ft} full-time employees and their children "
                     f"[{OFFER95}]")
        red = P(PEN_A, "employee_reduction")
        a_amt = _opt_amount(indexed_4980h_a_annual_usd, "indexed_4980h_a_annual_usd", strict=True)
        b_amt = _opt_amount(indexed_4980h_b_annual_usd, "indexed_4980h_b_annual_usd", strict=True)
        ptc = _opt_count(employees_with_premium_tax_credit, "employees_with_premium_tax_credit")
        base_a = P(PEN_A, "annual_base_usd_per_employee_before_indexing")
        a_month = None
        if a_amt is not None:
            a_month = _money(a_amt / 12 * max(0, offer_ft - red))
            a_math = (f"{_usd(a_amt)} / 12 x ({offer_ft} - {red}) = {_usd(a_month)} a month; x 12 = "
                      f"{_usd(a_month * 12)} if no offer all year")
        else:
            a_math = (f"(IRS indexed amount / 12) x ({offer_ft} - {red}); the base before indexing is {_usd(base_a)} "
                      f"a year; the {status_year} indexed amount is not in the rulebook (pass "
                      f"indexed_4980h_a_annual_usd from the IRS)")
        payments.append(PaymentFormula(section="4980H(a)", trigger=P(PEN_A, "trigger"), formula=P(PEN_A, "formula"),
                                       monthly_usd=a_month, annual_usd=_money(a_month * 12) if a_month is not None
                                       else None, math=a_math, rule_ids=[PEN_A]))
        base_b = P(PEN_B, "annual_base_usd_per_subsidized_employee_before_indexing")
        b_month = None
        if b_amt is not None and ptc is not None:
            raw = b_amt / 12 * ptc
            b_month = _money(min(raw, a_amt / 12 * max(0, offer_ft - red)) if a_amt is not None else raw)
            b_math = (f"{_usd(b_amt)} / 12 x {ptc} subsidized = {_usd(raw)} a month"
                      + (f", capped at the 4980H(a) amount {_usd(a_month)} -> {_usd(b_month)}" if a_amt is not None
                         else " (cap at the 4980H(a) amount not checked: pass indexed_4980h_a_annual_usd)"))
        else:
            b_math = (f"(IRS indexed amount / 12) x full-time employees with a premium tax credit, never more than the "
                      f"4980H(a) amount; the base before indexing is {_usd(base_b)} a year (indexed amount not in the "
                      f"rulebook)")
        payments.append(PaymentFormula(section="4980H(b)", trigger="coverage offered but unaffordable or not minimum "
                                       "value, and a full-time employee gets a premium tax credit",
                                       formula=f"monthly = indexed_amount / 12 x subsidized employees, cap {P(PEN_B, 'cap')}",
                                       monthly_usd=b_month, annual_usd=_money(b_month * 12) if b_month is not None
                                       else None, math=b_math, rule_ids=[PEN_B]))
        # forms for the status year, filed the next year
        fy = status_year + 1
        pm, pd = _month_day(P(FILE_1094, "paper_due"))
        em, ed = _month_day(P(FILE_1094, "electronic_due"))
        bm, bd = _month_day(P(FURNISH_1095, "base_due"))
        ext = P(FURNISH_1095, "automatic_extension_days")
        paper, elec = _roll_weekend(Date(fy, pm, pd)), _roll_weekend(Date(fy, em, ed))
        base = Date(fy, bm, bd)
        furnish = _roll_weekend(base + timedelta(days=ext))
        n_returns = _opt_count(information_returns_count, "information_returns_count")
        n_1095 = sum(1 for e in per_emp if any(h >= ft_k for h in e.hours)) if per_emp else max(ft)
        if n_returns is not None:
            efile = n_returns >= P(EFILE10, "min_returns_triggering_efile")
            math_.append(f"e-file: {n_returns} information returns {'>=' if efile else '<'} "
                         f"{P(EFILE10, 'min_returns_triggering_efile')} [{EFILE10}]")
        elif n_1095 >= P(EFILE10, "min_returns_triggering_efile"):
            efile = True
            math_.append(f"e-file: at least {n_1095} Forms 1095-C (one per full-time employee) >= "
                         f"{P(EFILE10, 'min_returns_triggering_efile')} information returns [{EFILE10}]")
        deadlines.append(_deadline(f"Furnish Form 1095-C for {status_year} to each full-time employee", furnish, d,
                                   f"{base.isoformat()} + {ext}-day automatic extension = "
                                   f"{(base + timedelta(days=ext)).isoformat()}"
                                   + (" -> next weekday" if furnish != base + timedelta(days=ext) else ""),
                                   [FURNISH_1095]))
        deadlines.append(_deadline(f"E-file Forms 1094-C/1095-C for {status_year} with the IRS"
                                   + ("" if efile else " (if you file electronically)"), elec, d,
                                   f"{P(FILE_1094, 'electronic_due')} = {Date(fy, em, ed).isoformat()}"
                                   + (" -> next weekday" if elec != Date(fy, em, ed) else ""), [FILE_1094]))
        if efile is not True:
            deadlines.append(_deadline(f"Paper-file Forms 1094-C/1095-C for {status_year} (only if under "
                                       f"{P(EFILE10, 'min_returns_triggering_efile')} information returns)", paper, d,
                                       f"{P(FILE_1094, 'paper_due')} = {Date(fy, pm, pd).isoformat()}"
                                       + (" -> next weekday" if paper != Date(fy, pm, pd) else ""), [FILE_1094]))
        if first_year_as_ale:
            cite.append(APRIL1)
            relief = P(APRIL1, "offer_deadline")
            deadlines.append(_deadline(f"First ALE year: offer coverage by {relief.split(' of ')[0]}, {status_year} "
                                       f"to avoid January-March payments", Date(status_year, *_month_day(relief)), d,
                                       f"{P(APRIL1, 'offer_deadline')}; coverage must be minimum value for 4980H(b) "
                                       f"relief; one time only", [APRIL1]))
        notes.append(f"Late or wrong information returns cost {_usd(P(LATE_RET, 'per_return_usd_base'))} per return "
                     f"before IRS inflation adjustment ({_usd(P(LATE_RET, 'corrected_within_30_days')['per_return_usd_base'])}"
                     f" if fixed within 30 days) [{LATE_RET}].")
        notes.append(f"Variable-hour crews: a look-back measurement period of "
                     f"{P(LOOKBACK, 'standard_measurement_period_months')['min']}-"
                     f"{P(LOOKBACK, 'standard_measurement_period_months')['max']} months can set full-time status; a "
                     f"recalled worker is a new hire only after {P(REHIRE13, 'break_weeks_required')} straight weeks "
                     f"with no hours [{LOOKBACK}, {REHIRE13}].")
        step = (f"You are an applicable large employer for {status_year}. Offer coverage to at least {must_offer} of "
                f"{offer_ft} full-time employees (and their children) every month, check affordability with an IRS "
                f"safe harbor, and calendar Form 1095-C by {furnish.isoformat()} and "
                + (f"e-filing by {elec.isoformat()}." if efile else
                   f"filing by {paper.isoformat()} (paper) or {elec.isoformat()} (electronic)."))
    else:
        step = (f"Not an applicable large employer for {status_year}: no 4980H payment and no ALE Form 1094-C/1095-C "
                f"filing for {status_year}. Recheck with {status_year} hours for {status_year + 1}"
                + (" and note that the seasonal exception can only be used when the excess workers were seasonal."
                   if seasonal_applied else "."))
        if whole >= ale_k - 5 and not seasonal_applied:
            step += f" You are close ({whole} of {ale_k}); track monthly hours now."
    notes.append(f"Count every hour paid, including vacation, holiday, sick, jury duty and layoff pay; leave out "
                 f"bona fide volunteer hours [{HOURS_DEF}]. Related companies under common control are counted "
                 f"together [{ALE}].")
    res = AcaResult(measurement_year=year, status_year=status_year, applicable_large_employer=ale, months=months,
                    sum_of_monthly_totals=round(total_sum, 2), average=round(avg, 2), average_rounded_down=whole,
                    threshold=ale_k, seasonal_exception_applied=seasonal_applied, months_over_50=over,
                    full_time_employees_for_offer=offer_ft, may_fail_to_offer_at_most=may_miss,
                    must_offer_at_least=must_offer, payments=payments, deadlines=deadlines,
                    must_file_electronically=efile, math=math_, next_step=step, notes=notes)
    return res.cite(*cite)


# ================================================================ 4. COBRA
CobraEvent = Literal["termination", "reduction_in_hours", "death", "medicare_entitlement",
                     "divorce_or_legal_separation", "dependent_child_loss"]
_COBRA_ALIASES = {
    "termination": ("termination", "terminated", "fired", "discharged", "discharge", "laid off", "layoff", "lay off",
                    "quit", "resigned", "resignation", "end of project", "termination of employment", "separated",
                    "reduction in force", "rif"),
    "reduction_in_hours": ("reduction in hours", "reduction of hours", "hours reduced", "hours cut", "reduced hours",
                           "cut hours", "cut to part time", "moved to part time"),
    "death": ("death", "died", "deceased", "death of employee"),
    "medicare_entitlement": ("medicare", "medicare entitlement", "entitled to medicare"),
    "divorce_or_legal_separation": ("divorce", "legal separation", "divorced", "divorce or legal separation"),
    "dependent_child_loss": ("dependent child loss", "child aging out", "aged out", "child aged out", "turns 26",
                             "dependent child", "child loses dependent status", "loss of dependent status"),
}
# Which events the employer reports (keyword that must appear in the rule's employer_reported_events list).
_EMPLOYER_REPORTED_KEY = {"termination": "termination", "reduction_in_hours": "reduction in hours", "death": "death",
                          "medicare_entitlement": "medicare"}


def normalize_cobra_event(value: str) -> str:
    t = _norm_words(value)
    for key, aliases in _COBRA_ALIASES.items():
        if t == key.replace("_", " ") or t in aliases:
            return key
    raise ValueError(f"qualifying_event must be one of {list(_COBRA_ALIASES)} (or e.g. 'laid off', 'hours cut', "
                     f"'divorce'), got {value!r}")


class CobraResult(core.Result):
    qualifying_event: str
    federal_cobra_applies: bool
    event_date: str
    clock_start_date: str = Field(description="Date the notice clocks and coverage period run from")
    employer_reports_event: bool
    deadlines: list[Deadline] = Field(default_factory=list)
    maximum_coverage_months: Optional[int] = None
    coverage_may_end: Optional[str] = Field(None, description="Last month of maximum COBRA coverage (ends on this date)")
    max_monthly_premium_usd: Optional[float] = None
    max_monthly_premium_disability_months_usd: Optional[float] = None
    math: list[str] = Field(default_factory=list)
    next_step: str
    notes: list[str] = Field(default_factory=list)


def cobra_deadlines(qualifying_event: str, event_date, employer_is_plan_administrator: bool = False,
                    coverage_loss_date=None, plan_measures_from_loss_of_coverage: bool = False,
                    administrator_notified_date=None, election_notice_date=None, election_date=None,
                    monthly_plan_cost_usd: Optional[float] = None, disability_extension: bool = False,
                    gross_misconduct: bool = False, employer_normally_under_20: bool = False,
                    as_of=None) -> CobraResult:
    ev = normalize_cobra_event(qualifying_event)
    d = _today(as_of)
    e_day = _date(event_date, "event_date")
    loss = _opt_date(coverage_loss_date, "coverage_loss_date") or e_day
    if loss < e_day:
        raise ValueError(f"coverage_loss_date {loss} cannot be before event_date {e_day}")
    admin_on = _opt_date(administrator_notified_date, "administrator_notified_date")
    notice_on = _opt_date(election_notice_date, "election_notice_date")
    elect_on = _opt_date(election_date, "election_date")
    cost = _opt_amount(monthly_plan_cost_usd, "monthly_plan_cost_usd", strict=True)
    for nm, v in (("administrator_notified_date", admin_on), ("election_notice_date", notice_on),
                  ("election_date", elect_on)):
        if v is not None and v < e_day:
            raise ValueError(f"{nm} {v} cannot be before event_date {e_day}")
    if elect_on is not None and notice_on is not None and elect_on < notice_on:
        raise ValueError("election_date cannot be before election_notice_date")
    if disability_extension and ev not in ("termination", "reduction_in_hours"):
        raise ValueError("disability_extension only extends COBRA after a termination or reduction in hours")
    start = loss if plan_measures_from_loss_of_coverage else e_day
    reported_list = " ".join(P(COBRA_ADMIN30, "employer_reported_events")).lower()
    employer_reports = ev in _EMPLOYER_REPORTED_KEY and _EMPLOYER_REPORTED_KEY[ev] in reported_list
    notes, math_ = [], []
    base = dict(qualifying_event=ev, event_date=e_day.isoformat(), clock_start_date=start.isoformat(),
                employer_reports_event=employer_reports)
    if employer_normally_under_20:
        res = CobraResult(**base, federal_cobra_applies=False,
                          next_step="Federal COBRA does not apply to this plan. Check whether the insurer's Cal-COBRA "
                                    "rules apply (not in this rulebook) and tell the employee in writing.",
                          notes=[f"Exempt: {P(COBRA20, 'test')} [{COBRA20}]. Smaller California employers may be "
                                 f"covered by Cal-COBRA instead; its rules could not be read."])
        return res.cite(COBRA20)
    if gross_misconduct and ev == "termination":
        res = CobraResult(**base, federal_cobra_applies=False,
                          next_step="A termination for gross misconduct is not a COBRA qualifying event. Gross "
                                    "misconduct is a high bar; get legal advice before denying COBRA on that basis.",
                          notes=[f"Employer-reported events: {'; '.join(P(COBRA_ADMIN30, 'employer_reported_events'))} "
                                 f"[{COBRA_ADMIN30}]."])
        return res.cite(COBRA_ADMIN30, COBRA20)
    cite = [COBRA20, COBRA_ELECT_NOTICE, COBRA_ELECT60, COBRA_MAX, COBRA_PREM, COBRA_PAY, COBRA_GENERAL]
    deadlines = []
    clock = "loss of coverage" if plan_measures_from_loss_of_coverage else "qualifying event"
    if employer_is_plan_administrator:
        k44 = P(COBRA_ELECT_NOTICE, "employer_is_administrator_deadline_days")
        notice_due = start + timedelta(days=k44)
        if not employer_reports:
            notice_due = None
            notes.append("The employee or family member must tell the plan about a divorce, legal separation or a "
                         "child losing dependent status (29 CFR 2590.606-3; that deadline is not in this rulebook). "
                         f"Once told, send the election notice within "
                         f"{P(COBRA_ELECT_NOTICE, 'administrator_deadline_days_after_notice')} days.")
        deadlines.append(_deadline("Send the COBRA election notice (you are the plan administrator)", notice_due, d,
                                   f"{clock} {start.isoformat()} + {k44} days" if notice_due else
                                   "starts when the beneficiary notifies the plan", [COBRA_ELECT_NOTICE],
                                   done_on=notice_on))
    else:
        k14 = P(COBRA_ELECT_NOTICE, "administrator_deadline_days_after_notice")
        if employer_reports:
            cite.append(COBRA_ADMIN30)
            k30 = P(COBRA_ADMIN30, "deadline_days")
            admin_due = start + timedelta(days=k30)
            deadlines.append(_deadline("Notify the plan administrator of the qualifying event", admin_due, d,
                                       f"{clock} {start.isoformat()} + {k30} days", [COBRA_ADMIN30], done_on=admin_on))
            received = admin_on or admin_due
            notice_due = received + timedelta(days=k14)
            deadlines.append(_deadline("Administrator sends the COBRA election notice", notice_due, d,
                                       f"administrator notified {received.isoformat()}"
                                       + ("" if admin_on else " (latest allowed)") + f" + {k14} days",
                                       [COBRA_ELECT_NOTICE], done_on=notice_on))
        else:
            notice_due = None
            notes.append("The employee or family member must tell the plan administrator about a divorce, legal "
                         "separation or a child losing dependent status (29 CFR 2590.606-3; that deadline is not in "
                         f"this rulebook). The administrator then has {k14} days to send the election notice.")
            deadlines.append(_deadline("Administrator sends the COBRA election notice", None, d,
                                       f"{k14} days after the administrator receives the beneficiary's notice",
                                       [COBRA_ELECT_NOTICE], done_on=notice_on))
    k60 = P(COBRA_ELECT60, "min_election_days")
    notice_basis = notice_on or notice_due
    if notice_basis is not None:
        later = max(loss, notice_basis)
        deadlines.append(_deadline("Election window may not close before", later + timedelta(days=k60), d,
                                   f"later of coverage loss {loss.isoformat()} and election notice "
                                   f"{notice_basis.isoformat()}" + ("" if notice_on else " (latest allowed)")
                                   + f" = {later.isoformat()}; + {k60} days", [COBRA_ELECT60], info=True))
    if elect_on is not None:
        k45 = P(COBRA_PAY, "first_payment_min_days_after_election")
        deadlines.append(_deadline("First premium payment cannot be required before", elect_on + timedelta(days=k45),
                                   d, f"elected {elect_on.isoformat()} + {k45} days", [COBRA_PAY], info=True))
    months = (P(COBRA_MAX, "with_disability_extension_months") if disability_extension else
              P(COBRA_MAX, "termination_or_hours_reduction_months") if ev in ("termination", "reduction_in_hours")
              else P(COBRA_MAX, "other_events_or_second_qualifying_event_months"))
    end = _add_months(start, months)
    math_.append(f"maximum coverage: {clock} {start.isoformat()} + {months} months = {end.isoformat()} [{COBRA_MAX}]")
    prem = prem_dis = None
    if cost is not None:
        pct, pct_d = P(COBRA_PREM, "max_percent_of_applicable_premium"), P(COBRA_PREM, "disability_extension_max_percent")
        prem = _money(cost * pct / 100)
        math_.append(f"max premium = {_usd(cost)} x {pct}% = {_usd(prem)} a month [{COBRA_PREM}]")
        if disability_extension:
            prem_dis = _money(cost * pct_d / 100)
            first_dis = P(COBRA_MAX, "termination_or_hours_reduction_months") + 1
            math_.append(f"months {first_dis}-{months} (disability extension): {_usd(cost)} x {pct_d}% = "
                         f"{_usd(prem_dis)} a month [{COBRA_PREM}]")
    notes.append(f"Each later premium is on time if paid within {P(COBRA_PAY, 'grace_period_days')} days after the "
                 f"start of its coverage period [{COBRA_PAY}].")
    notes.append(f"A second qualifying event (death, divorce) during an {P(COBRA_MAX, 'termination_or_hours_reduction_months')}"
                 f"-month period can extend coverage to {P(COBRA_MAX, 'other_events_or_second_qualifying_event_months')}"
                 f" months [{COBRA_MAX}]. New enrollees get the general notice within "
                 f"{P(COBRA_GENERAL, 'deadline_days_after_coverage_begins')} days after coverage begins "
                 f"[{COBRA_GENERAL}]. Dates are calendar days; no weekend or holiday extension is applied (none is stated "
                 f"in the rules read).")
    open_items = [x for x in deadlines if x.status in ("upcoming", "due_today", "overdue")]
    if open_items:
        x = open_items[0]
        when = f"OVERDUE by {-x.days_left} days" if x.status == "overdue" else f"{x.days_left} days left"
        step = f"{x.item} by {x.due_date} ({when})."
    elif any(x.status == "done_late" for x in deadlines):
        step = "A COBRA notice went out late; document why and make sure the election window runs a full period."
    else:
        step = "Notices are on track; keep proof of mailing and track the election window and premium dates."
    step += f" COBRA can run until {end.isoformat()}" + (f" at up to {_usd(prem)} a month." if prem else ".")
    res = CobraResult(**base, federal_cobra_applies=True, deadlines=deadlines, maximum_coverage_months=months,
                      coverage_may_end=end.isoformat(), max_monthly_premium_usd=prem,
                      max_monthly_premium_disability_months_usd=prem_dis, math=math_, next_step=step, notes=notes)
    return res.cite(*cite)


# ================================================================ 5. leave request check
LEAVE_TYPES = ("jury_duty", "court_witness", "crime_victim_court", "crime_victim_leave", "victim_safety_accommodation",
               "school_teacher_request", "school_activities", "emergency_duty", "emergency_training", "rehab",
               "adult_literacy", "organ_donor", "lactation", "fmla")
_LEAVE_ALIASES = {
    "jury_duty": ("jury", "jury service", "juror", "jury summons"),
    "court_witness": ("witness", "subpoena", "subpoenaed", "testify", "court witness", "witness subpoena"),
    "crime_victim_court": ("victim court", "crime victim court proceedings", "victim court proceedings",
                           "court proceedings", "sentencing", "victim court date"),
    "crime_victim_leave": ("victim leave", "domestic violence leave", "dv leave", "domestic violence",
                           "sexual assault", "stalking", "safety planning", "victim counseling"),
    "victim_safety_accommodation": ("victim accommodation", "safety accommodation", "victim safety"),
    "school_teacher_request": ("teacher request", "school appearance", "teacher asked", "school suspension",
                               "called to school", "teacher"),
    "school_activities": ("school", "school activity", "school activities", "school event", "child care",
                          "school enrollment", "enroll child", "school emergency", "child care emergency"),
    "emergency_duty": ("volunteer firefighter", "firefighter", "reserve officer", "reserve peace officer",
                       "emergency duty", "emergency rescue", "fire call"),
    "emergency_training": ("firefighter training", "emergency training", "reserve officer training",
                           "emergency rescue training", "fire training", "volunteer firefighter training",
                           "reserve peace officer training", "law enforcement training"),
    "rehab": ("rehabilitation", "alcohol rehab", "drug rehab", "substance abuse", "alcohol", "drug treatment"),
    "adult_literacy": ("literacy", "reading class", "adult literacy program", "learn to read"),
    "organ_donor": ("organ donation", "organ donor", "bone marrow", "bone marrow donor", "marrow", "kidney donor",
                    "liver donor"),
    "lactation": ("pumping", "breast milk", "breastfeeding", "nursing", "lactation break", "lactation room"),
    "fmla": ("family medical leave", "family and medical leave", "medical leave", "baby bonding", "bonding",
             "serious health condition", "military caregiver", "fmla leave", "servicemember caregiver"),
}
_FEHA_MOVED = {"jury_duty": JURY, "court_witness": VICTIM_COURT, "crime_victim_court": VICTIM_COURT,
               "crime_victim_leave": VICTIM_LEAVE, "victim_safety_accommodation": VICTIM_ACCOM}


def normalize_leave_type(value: str) -> str:
    """Exact name or alias first; then the longest alias found inside the text ('jury duty summons' -> jury_duty).
    Bare words that could mean something else ('training', 'donor') are not aliases on their own."""
    t = _norm_words(value)
    for key in LEAVE_TYPES:
        if t == key.replace("_", " ") or t in _LEAVE_ALIASES.get(key, ()):
            return key
    padded = f" {t} "
    hits = [(len(a), key) for key in LEAVE_TYPES for a in (key.replace("_", " "), *_LEAVE_ALIASES.get(key, ()))
            if f" {a} " in padded]
    if hits:
        key = max(hits)[1]
        return "emergency_training" if key == "emergency_duty" and " training " in padded else key
    raise ValueError(f"leave_type must be one of {list(LEAVE_TYPES)} (or e.g. 'jury', 'school event', 'pumping', "
                     f"'volunteer firefighter'), got {value!r}")


class LeaveResult(core.Result):
    leave_type: str
    protected: Literal["yes", "no", "check"] = Field(description="check = a fact you did not give, or unread law, decides it")
    employer_covered: Optional[bool] = None
    threshold: str
    paid: str
    limits: dict = Field(default_factory=dict)
    remaining: Optional[float] = Field(None, description="Hours, days or workweeks still available under the cap")
    can_approve_now: Optional[float] = Field(None, description="How much of the request is protected under the caps")
    unit: Optional[Literal["hours", "days", "workweeks"]] = None
    employee_must: list[str] = Field(default_factory=list)
    employer_must: list[str] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    enforced_by: str
    complaint_deadline: str
    math: list[str] = Field(default_factory=list)
    next_step: str
    notes: list[str] = Field(default_factory=list)


def leave_request_check(leave_type: str, employee_count: int, employees_at_location: Optional[int] = None,
                        private_employer: bool = True, hours_requested: Optional[float] = None,
                        hours_used_this_year: float = 0, hours_used_this_month: float = 0,
                        school_emergency: bool = False, days_requested: Optional[float] = None,
                        days_used_this_year: float = 0, months_employed: Optional[float] = None,
                        hours_worked_last_12_months: Optional[float] = None,
                        employees_within_75_miles: Optional[int] = None, fmla_covered_workweeks: Optional[int] = None,
                        weeks_requested: Optional[float] = None, weeks_used_last_12_months: float = 0,
                        military_caregiver: bool = False, subcontractor_written_request: bool = False,
                        request_date=None, as_of=None) -> LeaveResult:
    lt = normalize_leave_type(leave_type)
    if lt == "fmla" and re.search(r"\b(?:military caregiver|servicemember)\b", _norm_words(leave_type)):
        military_caregiver = True
    d = _today(as_of)
    req_day = _opt_date(request_date, "request_date") or d
    n = _count(employee_count, "employee_count", 1)
    loc = _opt_count(employees_at_location, "employees_at_location")
    if loc is not None and loc > n:
        raise ValueError(f"employees_at_location ({loc}) cannot exceed employee_count ({n})")
    hr_req = _opt_amount(hours_requested, "hours_requested", strict=True)
    hr_year = _amount(hours_used_this_year or 0, "hours_used_this_year")
    hr_month = _amount(hours_used_this_month or 0, "hours_used_this_month")
    if hr_month > hr_year:
        raise ValueError("hours_used_this_month cannot exceed hours_used_this_year")
    day_req = _opt_amount(days_requested, "days_requested", strict=True)
    day_used = _amount(days_used_this_year or 0, "days_used_this_year")
    if day_used > 366:
        raise ValueError("days_used_this_year cannot exceed 366")
    retal_years = P(RETAL_DEADLINE, "default_filing_deadline_years")
    lc_complaint = (f"generally {retal_years} year after the retaliatory act, with the Labor Commissioner (or sue in "
                    f"court) [{RETAL_DEADLINE}]")
    lc_enf = "Labor Commissioner (DLSE)"
    retal_note = (f"Retaliating against a worker for using this Labor Code right can cost up to "
                  f"{_usd(P(RETAL_PEN, 'civil_penalty_max_usd_per_violation'))} per violation, paid to the worker, "
                  f"plus reinstatement and back pay [{RETAL_PEN}].")
    out = dict(leave_type=lt, limits={}, employee_must=[], employer_must=[], deadlines=[], math=[], notes=[])
    cite: list[str] = []

    if lt in _FEHA_MOVED:
        rid = _FEHA_MOVED[lt]
        cite += [rid, VICTIM_CERT] if lt != "jury_duty" else [rid]
        cuts = _isos(P(rid, "labor_code_version_applies_to_violations_through"))
        cut = min(cuts) if lt == "court_witness" else max(cuts)     # 230(b) moved first; 230.2(b)/230.5 a year later
        k = P(rid, "min_employees")
        covered = None if k is None else (n >= k)
        out.update(threshold=("no size threshold stated" if k is None else
                              f"{k}+ employees under the old Labor Code (FEHA threshold not read)"),
                   protected=("yes" if k is None or n >= k else "check"), employer_covered=covered,
                   paid=str(P(rid, "pay_required", "not stated in the sources read")),
                   enforced_by="Civil Rights Department (FEHA, Gov. Code 12945.8)",
                   complaint_deadline=("not verified: the Labor Code one-year deadline applies only to violations "
                                       f"through {cut.isoformat()}; the FEHA deadline was not read"))
        if lt == "jury_duty":
            out["employee_must"].append(P(JURY, "employee_must_give"))
        elif lt in ("crime_victim_court", "court_witness"):
            out["limits"]["covered_persons"] = P(VICTIM_COURT, "covered")
            out["limits"]["related"] = P(VICTIM_COURT, "related")
        elif lt == "crime_victim_leave":
            out["limits"]["purposes"] = P(VICTIM_LEAVE, "purposes")
            out["limits"]["covered_victims"] = P(VICTIM_LEAVE, "covered_victims")
        else:
            out["limits"]["examples"] = P(VICTIM_ACCOM, "accommodation_examples_per_dlse")
        if lt != "jury_duty":
            out["employer_must"].append("Accept proof within a reasonable time after an unscheduled absence; a signed "
                                        "written statement from the worker counts: "
                                        + "; ".join(P(VICTIM_CERT, "accepted_certification")))
        out["employer_must"].append("Do not discharge, discipline or retaliate for the time off.")
        out["notes"].append(f"This protection moved from the Labor Code to FEHA (Gov. Code 12945.8) for violations after "
                            f"{cut.isoformat()}; the current text could not be read, so this answer is unverified. Do "
                            f"not send these complaints to the Labor Commissioner [{rid}].")
        step = ("Approve the time off, do not penalize it, and confirm current requirements with the Civil Rights "
                "Department." if out["protected"] == "yes" else
                f"You are below the old Labor Code threshold of {k}; the current FEHA threshold was not read, so check "
                f"with the Civil Rights Department before denying it.")
    elif lt == "school_teacher_request":
        cite.append(SCHOOL_TEACHER)
        out.update(threshold="no size threshold stated", protected="yes", employer_covered=True,
                   paid="not stated in the sources read", enforced_by=lc_enf, complaint_deadline=lc_complaint)
        out["employee_must"].append(P(SCHOOL_TEACHER, "employee_must_give"))
        out["employer_must"].append("Let the parent or guardian go when the child's teacher asks them to come to "
                                    "school; do not retaliate.")
        cite += [RETAL_DEADLINE, RETAL_PEN]
        out["notes"].append(retal_note)
        step = "Approve the time off if the worker gave reasonable notice before taking it."
    elif lt == "school_activities":
        cite.append(SCHOOL_ACT)
        k = P(SCHOOL_ACT, "min_employees")
        yr_cap, mo_cap = P(SCHOOL_ACT, "max_hours_per_year"), P(SCHOOL_ACT, "max_hours_per_calendar_month")
        emerg_monthly = P(SCHOOL_ACT, "monthly_cap_applies_to_school_emergencies")
        covered = (loc >= k) if loc is not None else (False if n < k else None)
        out.update(threshold=f"{k}+ employees working at the same location",
                   protected={True: "yes", False: "no", None: "check"}[covered], employer_covered=covered,
                   paid="not stated in the sources read (the statute's pay rules were not read)",
                   enforced_by=lc_enf, complaint_deadline=lc_complaint, unit="hours",
                   limits={"max_hours_per_year": yr_cap, "max_hours_per_calendar_month": mo_cap,
                           "monthly_cap_applies_to_emergencies": emerg_monthly,
                           "purposes": P(SCHOOL_ACT, "purposes"),
                           "eligible_relationships": P(SCHOOL_ACT, "eligible_relationships")})
        left_year = max(0.0, yr_cap - hr_year)
        left_month = max(0.0, mo_cap - hr_month)
        monthly_applies = not (school_emergency and not emerg_monthly)
        left = min(left_year, left_month) if monthly_applies else left_year
        out["remaining"] = round(left, 2)
        out["math"].append(f"year: {_num(yr_cap)} - {_num(hr_year)} used = {_num(left_year)} h"
                           + (f"; month: {_num(mo_cap)} - {_num(hr_month)} used = {_num(left_month)} h; available "
                              f"= min = {_num(left)} h" if monthly_applies else
                              "; school/child care emergency: no monthly cap"))
        if hr_req is not None:
            out["can_approve_now"] = round(min(hr_req, left), 2)
            out["math"].append(f"protected now = min(requested {_num(hr_req)}, available {_num(left)}) = "
                               f"{_num(out['can_approve_now'])} h")
        notice = P(SCHOOL_ACT, "notice")
        out["employee_must"].append("Give " + (notice["emergencies"] if school_emergency else
                                               notice["activities_and_enrollment"]))
        if covered:
            cite += [RETAL_DEADLINE, RETAL_PEN]
            out["notes"].append(retal_note)
        if covered is None:
            step = f"Count the employees at this worker's location: the leave is protected at {k} or more."
        elif not covered:
            step = (f"Not required under Labor Code 230.8 ({loc if loc is not None else n} < {k} at the location); "
                    f"you may still allow it.")
        elif hr_req is not None and out["can_approve_now"] < hr_req:
            step = (f"Approve {_num(out['can_approve_now'])} of the {_num(hr_req)} hours as protected leave; the rest "
                    f"is over the cap (you may still allow it).")
        else:
            step = f"Approve it; {_num(left - (hr_req or 0))} protected hours remain after this request."
    elif lt in ("emergency_duty", "emergency_training"):
        if lt == "emergency_duty":
            cite.append(EMERG_DUTY)
            out.update(threshold="no size threshold stated", protected="yes", employer_covered=True,
                       paid="not stated in the sources read", enforced_by=lc_enf, complaint_deadline=lc_complaint,
                       limits={"covered": P(EMERG_DUTY, "covered")})
            out["employee_must"].append("Health care workers: " + P(EMERG_DUTY, "health_care_provider_notice"))
            step = "Do not discipline the worker for responding to an emergency in a covered volunteer role."
        else:
            cite.append(EMERG_TRAIN)
            k, cap = P(EMERG_TRAIN, "min_employees"), P(EMERG_TRAIN, "max_days_per_calendar_year")
            covered = n >= k
            left = max(0.0, cap - day_used)
            out.update(threshold=f"{k}+ employees", protected="yes" if covered else "no", employer_covered=covered,
                       paid="not stated in the sources read", enforced_by=lc_enf, complaint_deadline=lc_complaint,
                       unit="days", remaining=round(left, 2),
                       limits={"max_days_per_calendar_year": cap, "purpose": P(EMERG_TRAIN, "purpose"),
                               "covered": P(EMERG_TRAIN, "covered")})
            out["math"].append(f"{_num(cap)} days - {_num(day_used)} used = {_num(left)} days left in {d.year}")
            if day_req is not None:
                out["can_approve_now"] = round(min(day_req, left), 2) if covered else 0.0
                out["math"].append(f"protected now = min(requested {_num(day_req)}, left {_num(left)}) = "
                                   f"{_num(min(day_req, left))} days")
            step = (f"Approve up to {_num(min(day_req, left) if day_req else left)} days of training leave."
                    if covered else f"Not required below {k} employees ({n}); you may still allow it.")
        cite += [RETAL_DEADLINE, RETAL_PEN]
        out["notes"].append(retal_note)
    elif lt in ("rehab", "adult_literacy"):
        rid = REHAB if lt == "rehab" else LITERACY
        cite.append(rid)
        k = P(rid, "min_employees")
        covered = n >= k and private_employer
        out.update(threshold=f"private employers regularly employing {k}+", protected="yes" if covered else "no",
                   employer_covered=covered, enforced_by=lc_enf, complaint_deadline=lc_complaint,
                   paid=("unpaid, but the worker may use accrued sick leave" if lt == "rehab" and
                         P(rid, "may_use_accrued_sick_leave") else "unpaid") if not P(rid, "time_off_paid") else "paid")
        out["employer_must"] += ["Reasonably accommodate unless it would be an undue hardship.",
                                 "Keep the worker's enrollment private."]
        if lt == "adult_literacy":
            out["employer_must"].append("Do not fire a worker solely for revealing a literacy problem if their work "
                                        "is satisfactory.")
        out["math"].append(f"{n} employees {'>=' if n >= k else '<'} {k}"
                           + ("" if private_employer else "; public employer: not covered"))
        if covered:
            cite += [RETAL_DEADLINE, RETAL_PEN]
            out["notes"].append(retal_note)
        step = ("Grant a reasonable accommodation (usually time off) and keep it confidential." if covered else
                "Not required for your size or employer type; you may still allow it.")
    elif lt == "organ_donor":
        cite.append(DONOR)
        out.update(threshold="size threshold and day limits not in the rulebook (Labor Code 1508-1511 not read)",
                   protected="check", employer_covered=None, paid="paid leave (per DLSE manual)",
                   enforced_by=lc_enf, complaint_deadline=lc_complaint)
        out["employer_must"].append("Do not retaliate for taking donor leave.")
        cite += [RETAL_DEADLINE]
        step = "Check Labor Code 1508-1511 for your size and the number of paid days before answering the request."
    elif lt == "lactation":
        cite += [LAC_POLICY, LAC_ROOM, LAC_NODOC, LAC_U50, LAC_PAY]
        k = P(LAC_U50, "employee_count_below")
        out.update(threshold=f"any size (undue-hardship exception for break time below {k} employees)",
                   protected="yes", employer_covered=True,
                   paid=f"extra break time: {P(LAC_PAY, 'extra_break_time_paid')} [{LAC_PAY}]",
                   enforced_by=lc_enf, complaint_deadline=lc_complaint)
        out["employer_must"] += ["Provide break time each time it is needed and a private space that is not a "
                                 "bathroom: " + ", ".join(P(LAC_ROOM, "must_contain")) + "; nearby "
                                 + " and ".join(P(LAC_ROOM, "nearby")) + ".",
                                 "Do not require a doctor's note or other documentation."]
        if n < k:
            out["notes"].append(f"Under {k} employees you may refuse break time only if it would cause significant "
                                f"difficulty or expense; still make reasonable efforts to provide a private place other "
                                f"than a toilet stall [{LAC_U50}].")
        if subcontractor_written_request:
            cite.append(LAC_GC)
            k_bd = P(LAC_GC, "deadline_business_days")
            due = _add_business_days(req_day, k_bd)
            out["deadlines"].append(_deadline("GC: provide lactation space (or a safe spot for the sub to set one up)",
                                              due, d, f"written request {req_day.isoformat()} + {k_bd} business days "
                                                      f"(Mon-Fri; holidays not skipped)", [LAC_GC]))
        cite += [RETAL_DEADLINE, RETAL_PEN]
        out["notes"].append(retal_note)
        step = ("Set up the lactation space within the deadline below and confirm it in writing to the subcontractor."
                if subcontractor_written_request else
                "Grant the break time and provide a compliant private space; give the worker your written policy.")
    else:   # fmla
        cite += [FMLA_COV, FMLA_ELIG, FMLA_SITE, FMLA_12, FMLA_NOTICE, FMLA_POSTER]
        fk, fw = P(FMLA_COV, "min_employees"), P(FMLA_COV, "min_workweeks")
        m_k, h_k = P(FMLA_ELIG, "min_months_employed"), P(FMLA_ELIG, "min_hours_prior_12_months")
        r_k, miles = P(FMLA_ELIG, "min_employees_within_75_miles"), P(FMLA_ELIG, "radius_miles")
        wk = _opt_count(fmla_covered_workweeks, "fmla_covered_workweeks")
        mo = _opt_amount(months_employed, "months_employed")
        hrs = _opt_amount(hours_worked_last_12_months, "hours_worked_last_12_months")
        if hrs is not None and hrs > 8784:
            raise ValueError("hours_worked_last_12_months cannot exceed 8,784 (hours in a leap year)")
        near = _opt_count(employees_within_75_miles, "employees_within_75_miles")
        if near is not None and near > n:
            raise ValueError(f"employees_within_75_miles ({near}) cannot exceed employee_count ({n})")
        used = _amount(weeks_used_last_12_months or 0, "weeks_used_last_12_months")
        wreq = _opt_amount(weeks_requested, "weeks_requested", strict=True)
        if wk is not None:
            covered, cov_txt = wk >= fw, f"{wk} workweeks with {fk}+ employees vs {fw}"
        elif n < fk:
            covered, cov_txt = False, (f"{n} employees < {fk} (assuming you did not have {fk}+ on each working day of "
                                       f"{fw} workweeks this year or last)")
        else:
            covered, cov_txt = None, (f"{n} employees >= {fk}; covered if that held on each working day of {fw}+ "
                                      f"workweeks this year or last (give fmla_covered_workweeks)")
        tests = [("employer covered", covered, cov_txt),
                 ("months employed", None if mo is None else mo >= m_k, f"{_num(mo) if mo is not None else '?'} vs {m_k} "
                  f"(need not be consecutive)"),
                 ("hours in the last 12 months", None if hrs is None else hrs >= h_k,
                  f"{_num(hrs) if hrs is not None else '?'} vs {h_k:,}"),
                 (f"employees within {miles} miles of the worksite", None if near is None else near >= r_k,
                  f"{near if near is not None else '?'} vs {r_k} (construction: the home base or reporting site)")]
        for name_, ok, txt in tests:
            out["math"].append(f"{name_}: {txt} -> {'pass' if ok else 'fail' if ok is False else 'unknown'}")
        results = [t[1] for t in tests]
        if any(r is False for r in results):
            prot = "no"
        elif all(r is True for r in results):
            prot = "yes"
        else:
            prot = "check"
        cap = P(FMLA_26, "max_workweeks") if military_caregiver else P(FMLA_12, "max_workweeks")
        if military_caregiver:
            cite.append(FMLA_26)
        left = max(0.0, cap - used)
        out.update(threshold=f"employer {fk}+ employees for {fw}+ workweeks; employee {m_k} months, {h_k:,} hours, "
                             f"{r_k} employees within {miles} miles",
                   protected=prot, employer_covered=covered,
                   paid="not in this rulebook (the FMLA pay and paid-leave substitution rules were not read)",
                   enforced_by="U.S. Department of Labor, Wage and Hour Division",
                   complaint_deadline="not in this rulebook", unit="workweeks", remaining=round(left, 2),
                   limits={"max_workweeks": cap, "period": P(FMLA_26 if military_caregiver else FMLA_12, "period"),
                           "reasons": P(FMLA_12, "reasons")})
        out["math"].append(f"{_num(cap)} workweeks - {_num(used)} used = {_num(left)} left")
        if wreq is not None:
            out["can_approve_now"] = round(min(wreq, left), 2) if prot == "yes" else None
            out["math"].append(f"protected now = min(requested {_num(wreq)}, left {_num(left)}) = {_num(min(wreq, left))}")
        k5 = P(FMLA_NOTICE, "eligibility_notice_business_days")
        if covered is not False:
            out["deadlines"].append(_deadline("Give the FMLA eligibility notice with the rights and responsibilities "
                                              "notice", _add_business_days(req_day, k5), d,
                                              f"request {req_day.isoformat()} + {k5} business days (Mon-Fri; holidays "
                                              f"not skipped)", [FMLA_NOTICE]))
            out["employer_must"].append(f"Designate the leave as FMLA (or not) within "
                                        f"{P(FMLA_NOTICE, 'designation_notice_business_days')} business days after you "
                                        f"have enough information.")
        out["notes"].append(f"Construction workers: the {miles}-mile count uses {P(FMLA_SITE, 'no_fixed_worksite_rule')}; "
                            f"measure by road ({P(FMLA_SITE, 'distance_measure')}) [{FMLA_SITE}].")
        out["notes"].append("California CFRA has its own coverage and eligibility rules that are not in this rulebook; "
                            "a worker can be CFRA-protected even when FMLA does not apply.")
        missing = [t[0] for t in tests if t[1] is None]
        if prot == "no":
            failed = [t[0] for t in tests if t[1] is False]
            step = f"Not FMLA-protected ({', '.join(failed)} not met). Check CFRA before denying the leave."
        elif prot == "check":
            step = f"Give the eligibility notice by the deadline below, and get these facts: {', '.join(missing)}."
        else:
            step = (f"Eligible: give the eligibility and rights notice by the deadline below; "
                    f"{_num(left)} workweeks remain" + (f", so {_num(min(wreq, left))} of {_num(wreq)} requested are "
                                                        f"protected." if wreq else "."))

    res = LeaveResult(**out, next_step=step)
    return res.cite(*cite)


# ================================================================ MCP registration
def _tool_docs() -> dict[str, str]:
    """Tool descriptions built from the rulebook, so the numbers an agent reads match the numbers the tool uses."""
    sd_h = _num(P(SD26, "coverage_min_hours_in_city_per_week"))
    fk, fw = P(FMLA_COV, "min_employees"), P(FMLA_COV, "min_workweeks")
    cobra_k = P(COBRA20, "exempt_if_fewer_than_employees")
    tcw_cost = next(e["max_project_cost_usd"] for e in P(TCW_EXC, "exceptions") if "max_project_cost_usd" in e)
    return {
        "wage": (
            "California and City of San Diego minimum wage for a jobsite on a date: the state rate, the San Diego city "
            f"rate (any employer size, for employees working {sd_h}+ hours in a week in the city; not inside federal "
            "enclaves; not for exempt salaried staff), the required rate (higher of the two), the San Diego traffic "
            "control (flagger) wage on City permits (basic + benefits, overtime/Saturday and Sunday/holiday rates, "
            f"locked at permit issuance; single-family/duplex and {_usd(tcw_cost)}-or-less project exceptions), the "
            "San Diego living wage on City service contracts, upcoming rate changes with days away, the exempt salary "
            f"threshold for the year, the {_num(P(TOOLS2X, 'hand_tool_threshold_multiplier'))}x hand-tool threshold, "
            "an optional pay check with the shortfall, postings, math, next step, notes and citations. Other cities: "
            "returns the state floor and says the local rate is not in the verified rulebook (no guess)."),
        "mandates": (
            "Which California and federal HR mandates apply at your size, as a checklist: expense reimbursement and "
            "deductions, hiring and background-check limits, pay transparency (pay scale in job postings at "
            f"{P(PAY_POST, 'min_employees')}+), lactation (policy and room; undue-hardship exception under "
            f"{P(LAC_U50, 'employee_count_below')}; GC {P(LAC_GC, 'deadline_business_days')}-business-day duty), "
            f"harassment training ({P(HARASS, 'min_employees')}+, unverified), jury duty and crime victim protections "
            "(moved to FEHA, unverified), school appearance and school activities leave "
            f"({P(SCHOOL_ACT, 'min_employees')}+ at one location), emergency responder duty and training leave "
            f"({P(EMERG_TRAIN, 'min_employees')}+), rehab and adult literacy accommodation "
            f"({P(REHAB, 'min_employees')}+, private), donor leave, COBRA ({cobra_k}+), the "
            f"{P(WAIT90, 'max_waiting_period_days')}-day waiting period, FMLA ({fk}+ for {fw} workweeks), the ACA "
            f"large-employer test ({P(ALE, 'min_full_time_employees_including_ftes')}+ incl. FTEs), postings (Wage "
            "Orders 16 and 4, jobsite binder, Spanish notice) and the City of San Diego minimum wage, sick leave and "
            "enforcement fee (rate x employees). Each item says yes/no/check, the threshold, your count and math, "
            "what to do, whether your size triggers it, and its rules."),
        "aca": (
            "ACA employer mandate test (26 CFR 54.4980H): from last year's monthly hours of service, counts full-time "
            f"employees ({P(FT30, 'monthly_hours_equivalent')}+ hours) and FTEs (others' hours capped at "
            f"{P(FTE, 'max_hours_counted_per_non_full_time_employee_per_month')} each, divided by {P(FTE, 'divisor')}) "
            f"per month, averages the 12 months, rounds down and compares with "
            f"{P(ALE, 'min_full_time_employees_including_ftes')}; applies the seasonal-worker exception "
            f"({P(SEASONAL, 'alternative_months')} months); for an ALE gives the offer count (all but "
            f"{_num(P(OFFER95, 'max_not_offered_fraction') * 100)}% or {P(OFFER95, 'max_not_offered_min_count')}), the "
            "4980H(a) and (b) payment formulas (dollars only when you pass the IRS indexed amounts), Form 1095-C "
            "furnishing and Form 1094-C/1095-C filing dates (weekend rule), whether you must e-file "
            f"({P(EFILE10, 'min_returns_triggering_efile')}+ returns), the first-year relief, a monthly table, math, "
            "next step and citations."),
        "cobra": (
            "Federal COBRA calendar for one qualifying event: employer notice to the plan administrator "
            f"({P(COBRA_ADMIN30, 'deadline_days')} days), election notice "
            f"({P(COBRA_ELECT_NOTICE, 'administrator_deadline_days_after_notice')} days after the administrator is "
            f"told, or {P(COBRA_ELECT_NOTICE, 'employer_is_administrator_deadline_days')} days if you are the "
            f"administrator), the earliest the {P(COBRA_ELECT60, 'min_election_days')}-day election window may close, "
            f"the {P(COBRA_PAY, 'first_payment_min_days_after_election')}-day first-payment rule and "
            f"{P(COBRA_PAY, 'grace_period_days')}-day grace periods, the maximum coverage end "
            f"({P(COBRA_MAX, 'termination_or_hours_reduction_months')}, "
            f"{P(COBRA_MAX, 'with_disability_extension_months')} with disability, "
            f"{P(COBRA_MAX, 'other_events_or_second_qualifying_event_months')} months), the "
            f"{P(COBRA_PREM, 'max_percent_of_applicable_premium')}%/{P(COBRA_PREM, 'disability_extension_max_percent')}"
            f"% premium caps in dollars, status and days left for each date, the small-employer (under {cobra_k}) "
            "exemption, math, next step and citations."),
        "leave": (
            "Is this time-off request protected at your size, and what must you do? Covers jury duty, court witness "
            "and crime victim leave and accommodations (moved to FEHA: flagged unverified and routed to the Civil "
            "Rights Department), school appearance and school activities leave (hours left under the "
            f"{_num(P(SCHOOL_ACT, 'max_hours_per_year'))}-hour yearly and "
            f"{_num(P(SCHOOL_ACT, 'max_hours_per_calendar_month'))}-hour monthly caps), volunteer firefighter/reserve "
            f"officer duty and training leave ({_num(P(EMERG_TRAIN, 'max_days_per_calendar_year'))} days a year), "
            "rehab and adult literacy accommodation, organ/bone marrow donor leave, lactation (including the GC "
            "deadline) and federal FMLA (employer coverage, "
            f"{P(FMLA_ELIG, 'min_months_employed')} months / {P(FMLA_ELIG, 'min_hours_prior_12_months'):,} hours / "
            f"{P(FMLA_ELIG, 'min_employees_within_75_miles')} employees within {P(FMLA_ELIG, 'radius_miles')} miles, "
            "workweeks left, notice deadline). Returns protected yes/no/check, limits, remaining, how much can be "
            "approved, pay, duties, deadlines, enforcing agency, complaint deadline, math, next step and citations."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    D = "ISO date, e.g. 2026-11-25 (11/25/2026 also works); default today in California"
    sd_min_h = _num(P(SD26, "coverage_min_hours_in_city_per_week"))
    fk = P(FMLA_COV, "min_employees")
    miles = P(FMLA_ELIG, "radius_miles")
    cobra_k = P(COBRA20, "exempt_if_fewer_than_employees")
    ft_h = P(FT30, "monthly_hours_equivalent")
    fte_cap = P(FTE, "max_hours_counted_per_non_full_time_employee_per_month")
    school_cap = (f"{_num(P(SCHOOL_ACT, 'max_hours_per_year'))} h/year, "
                  f"{_num(P(SCHOOL_ACT, 'max_hours_per_calendar_month'))} h/month")
    lac_bd = P(LAC_GC, "deadline_business_days")

    @mcp.tool(name="jobsite_minimum_wage", annotations=ro, description=docs["wage"])
    def _jobsite_minimum_wage(
        city: Annotated[str, Field(description=(
            "City where the hours are worked, e.g. 'San Diego', 'SD', 'City of San Diego', 'Chula Vista', "
            "'unincorporated San Diego County'. Use 'San Diego' only if the jobsite is inside City of San Diego "
            "limits (a San Diego mailing address is not enough)"))],
        on_date: Annotated[Optional[str], Field(description=f"Date the hours are worked. {D}")] = None,
        hours_in_city_this_week: Annotated[Optional[float], Field(ge=0, le=168, description=(
            f"Hours this employee works inside the city this week (the San Diego rate covers {sd_min_h}+ h/week)"))] = None,
        exempt_salaried: Annotated[bool, Field(description=(
            "True for exempt salaried staff (then the exempt salary threshold applies, not the hourly rate)"))] = False,
        inside_federal_enclave: Annotated[bool, Field(description=(
            "True for work inside a federal enclave such as a military base (the City ordinance does not reach it)"))] = False,
        traffic_control_permit: Annotated[bool, Field(description=(
            "True for flaggers/traffic control work required by a City of San Diego traffic control permit"))] = False,
        permit_issue_date: Annotated[Optional[str], Field(description=(
            "Date the City issued the traffic control permit (the flagger rate is locked at issuance); default "
            "on_date"))] = None,
        single_family_or_duplex: Annotated[bool, Field(description=(
            "Traffic control for single-family home or duplex work (excepted from the flagger wage)"))] = False,
        project_cost_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Project cost, for the small-project flagger exception"))] = None,
        city_living_wage_contract: Annotated[bool, Field(description=(
            "True for work on a City of San Diego service contract or agreement covered by the Living Wage "
            "Ordinance"))] = False,
        hourly_rate_paid_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Cash hourly rate you pay, to check it"))] = None,
        benefits_paid_usd_per_hour: Annotated[float, Field(ge=0, description=(
            "Employer-paid fringe or health benefits per hour (credited toward the flagger or living wage rate)"))] = 0.0,
    ) -> JobsiteWageResult:
        return jobsite_minimum_wage(city, on_date, hours_in_city_this_week, exempt_salaried, inside_federal_enclave,
                                    traffic_control_permit, permit_issue_date, single_family_or_duplex,
                                    project_cost_usd, city_living_wage_contract, hourly_rate_paid_usd,
                                    benefits_paid_usd_per_hour)

    @mcp.tool(name="employer_size_mandates", annotations=ro, description=docs["mandates"])
    def _employer_size_mandates(
        employee_count: Annotated[int, Field(ge=1, description=(
            "Employees on payroll now, all locations, full- and part-time (not independent contractors)"))],
        employees_in_california: Annotated[Optional[int], Field(ge=0, description=(
            "Of those, how many work in California (default all)"))] = None,
        employees_at_largest_location: Annotated[Optional[int], Field(ge=0, description=(
            "Employees working at your largest single location (school activities leave counts per location)"))] = None,
        private_employer: Annotated[bool, Field(description="False for a public agency")] = True,
        has_group_health_plan: Annotated[bool, Field(description="You sponsor a group health plan")] = False,
        cobra_under_20_on_half_of_business_days_last_year: Annotated[Optional[bool], Field(description=(
            f"True if you had fewer than {cobra_k} employees on at least half of last year's typical business "
            "days"))] = None,
        fmla_workweeks_with_50_or_more: Annotated[Optional[int], Field(ge=0, le=53, description=(
            f"Calendar workweeks (this year or last, whichever is more) with {fk}+ employees on every working "
            "day"))] = None,
        aca_average_ft_plus_fte_last_year: Annotated[Optional[float], Field(ge=0, description=(
            "Last year's average full-time employees + FTEs per month, if known (else run aca_employer_status)"))] = None,
        employees_in_san_diego_city: Annotated[int, Field(ge=0, description=(
            "Average employees working inside City of San Diego limits over 12 months (0 if none)"))] = 0,
        posts_job_openings: Annotated[bool, Field(description="You post job openings")] = True,
        has_office_staff: Annotated[bool, Field(description=(
            "You employ office or clerical staff (bookkeepers, estimators)"))] = False,
        spanish_only_readers: Annotated[bool, Field(description="Some workers read only Spanish")] = False,
        general_contractor: Annotated[bool, Field(description="You coordinate multiemployer jobsites as GC")] = False,
        include_unverified: Annotated[bool, Field(description=(
            "Include topics whose rules are unverified (harassment training; jury and crime-victim protections that "
            "moved to FEHA). They make verified=false"))] = True,
        as_of: Annotated[Optional[str], Field(description=D)] = None,
    ) -> MandatesResult:
        return employer_size_mandates(employee_count, employees_in_california, employees_at_largest_location,
                                      private_employer, has_group_health_plan,
                                      cobra_under_20_on_half_of_business_days_last_year,
                                      fmla_workweeks_with_50_or_more, aca_average_ft_plus_fte_last_year,
                                      employees_in_san_diego_city, posts_job_openings, has_office_staff,
                                      spanish_only_readers, general_contractor, include_unverified, as_of)

    @mcp.tool(name="aca_employer_status", annotations=ro, description=docs["aca"])
    def _aca_employer_status(
        measurement_year: Annotated[int, Field(description=(
            "Calendar year whose hours you are giving (the preceding year: 2025 hours decide 2026 status)"))],
        employee_hours: Annotated[Optional[Union[list[EmployeeMonths], str]], Field(description=(
            "Per employee: [{name, hours:[12 monthly hours Jan-Dec]}] or {name, jan, feb, ...}, or CSV/tab text with "
            "a header like name,jan,...,dec. Hours of service include paid leave"))] = None,
        monthly_full_time_counts: Annotated[Optional[list[int]], Field(description=(
            f"Alternative: 12 counts of employees with {ft_h}+ hours in each month"))] = None,
        monthly_non_full_time_hours: Annotated[Optional[list[float]], Field(description=(
            f"Alternative: 12 monthly totals of the other employees' hours, each person capped at {fte_cap}"))] = None,
        excess_over_50_were_seasonal: Annotated[bool, Field(description=(
            "True if the workers above the threshold in the peak months were seasonal workers"))] = False,
        new_employer: Annotated[bool, Field(description="True if the business did not exist last year")] = False,
        first_year_as_ale: Annotated[Optional[bool], Field(description=(
            "True if this is your first year as an applicable large employer (first-quarter relief)"))] = None,
        current_full_time_employees: Annotated[Optional[int], Field(ge=0, description=(
            "Full-time employees now, for the offer count and payment formula (default December of the year "
            "given)"))] = None,
        indexed_4980h_a_annual_usd: Annotated[Optional[float], Field(gt=0, description=(
            "IRS inflation-indexed 4980H(a) annual amount for the status year, to compute dollars"))] = None,
        indexed_4980h_b_annual_usd: Annotated[Optional[float], Field(gt=0, description=(
            "IRS inflation-indexed 4980H(b) annual amount for the status year"))] = None,
        employees_with_premium_tax_credit: Annotated[Optional[int], Field(ge=0, description=(
            "Full-time employees who got a Marketplace premium tax credit (for 4980H(b))"))] = None,
        information_returns_count: Annotated[Optional[int], Field(ge=0, description=(
            "W-2s + 1099s + 1095-Cs and other information returns you file in a year (e-file test)"))] = None,
        as_of: Annotated[Optional[str], Field(description=D)] = None,
    ) -> AcaResult:
        return aca_employer_status(measurement_year, employee_hours, monthly_full_time_counts,
                                   monthly_non_full_time_hours, excess_over_50_were_seasonal, new_employer,
                                   first_year_as_ale, current_full_time_employees, indexed_4980h_a_annual_usd,
                                   indexed_4980h_b_annual_usd, employees_with_premium_tax_credit,
                                   information_returns_count, as_of)

    @mcp.tool(name="cobra_deadlines", annotations=ro, description=docs["cobra"])
    def _cobra_deadlines(
        qualifying_event: Annotated[str, Field(description=(
            "termination (also 'laid off', 'quit', 'fired'), reduction_in_hours ('hours cut'), death, "
            "medicare_entitlement, divorce_or_legal_separation, dependent_child_loss ('aged out')"))],
        event_date: Annotated[str, Field(description="Date of the qualifying event (ISO or MM/DD/YYYY)")],
        employer_is_plan_administrator: Annotated[bool, Field(description=(
            "True if you administer the plan yourself (no outside COBRA administrator)"))] = False,
        coverage_loss_date: Annotated[Optional[str], Field(description=(
            "Date health coverage ends, if after the event"))] = None,
        plan_measures_from_loss_of_coverage: Annotated[bool, Field(description=(
            "True if the plan document runs COBRA periods from the loss of coverage"))] = False,
        administrator_notified_date: Annotated[Optional[str], Field(description=(
            "Date you notified the administrator, if done"))] = None,
        election_notice_date: Annotated[Optional[str], Field(description=(
            "Date the election notice was sent, if sent"))] = None,
        election_date: Annotated[Optional[str], Field(description="Date COBRA was elected, if elected")] = None,
        monthly_plan_cost_usd: Annotated[Optional[float], Field(gt=0, description=(
            "Full monthly cost of the coverage (employer + employee share), to cap the COBRA premium"))] = None,
        disability_extension: Annotated[bool, Field(description=(
            "A qualified beneficiary has a disability extension"))] = False,
        gross_misconduct: Annotated[bool, Field(description="The termination was for gross misconduct")] = False,
        employer_normally_under_20: Annotated[bool, Field(description=(
            f"True if you had fewer than {cobra_k} employees on at least half of last year's typical business "
            "days"))] = False,
        as_of: Annotated[Optional[str], Field(description=D)] = None,
    ) -> CobraResult:
        return cobra_deadlines(qualifying_event, event_date, employer_is_plan_administrator, coverage_loss_date,
                               plan_measures_from_loss_of_coverage, administrator_notified_date, election_notice_date,
                               election_date, monthly_plan_cost_usd, disability_extension, gross_misconduct,
                               employer_normally_under_20, as_of)

    @mcp.tool(name="leave_request_check", annotations=ro, description=docs["leave"])
    def _leave_request_check(
        leave_type: Annotated[str, Field(description=(
            "One of " + ", ".join(LEAVE_TYPES) + " (aliases like 'jury', 'school event', 'pumping', 'volunteer "
            "firefighter', 'rehab', 'FMLA' work)"))],
        employee_count: Annotated[int, Field(ge=1, description="Employees on payroll, all locations")],
        employees_at_location: Annotated[Optional[int], Field(ge=0, description=(
            f"Employees at this worker's location (school activities leave: {school_cap})"))] = None,
        private_employer: Annotated[bool, Field(description="False for a public agency")] = True,
        hours_requested: Annotated[Optional[float], Field(gt=0, description="Hours requested (school activities)")] = None,
        hours_used_this_year: Annotated[float, Field(ge=0, description=(
            "Hours of this leave already used this year"))] = 0,
        hours_used_this_month: Annotated[float, Field(ge=0, description=(
            "Hours of this leave already used this calendar month"))] = 0,
        school_emergency: Annotated[bool, Field(description="School or child care emergency (no monthly cap)")] = False,
        days_requested: Annotated[Optional[float], Field(gt=0, description="Days requested (emergency training)")] = None,
        days_used_this_year: Annotated[float, Field(ge=0, description=(
            "Training days already used this calendar year"))] = 0,
        months_employed: Annotated[Optional[float], Field(ge=0, description=(
            "FMLA: months employed in total (need not be consecutive)"))] = None,
        hours_worked_last_12_months: Annotated[Optional[float], Field(ge=0, description=(
            "FMLA: hours worked in the 12 months before the leave"))] = None,
        employees_within_75_miles: Annotated[Optional[int], Field(ge=0, description=(
            f"FMLA: employees within {miles} road miles of the worker's home base or reporting site"))] = None,
        fmla_covered_workweeks: Annotated[Optional[int], Field(ge=0, le=53, description=(
            f"FMLA: calendar workweeks with {fk}+ employees on every working day, this year or last"))] = None,
        weeks_requested: Annotated[Optional[float], Field(gt=0, description="FMLA: workweeks requested")] = None,
        weeks_used_last_12_months: Annotated[float, Field(ge=0, description=(
            "FMLA: workweeks already used in the 12-month period"))] = 0,
        military_caregiver: Annotated[bool, Field(description="FMLA: care for a covered servicemember")] = False,
        subcontractor_written_request: Annotated[bool, Field(description=(
            f"Lactation: a subcontractor asked you (the GC) in writing for space ({lac_bd} business days)"))] = False,
        request_date: Annotated[Optional[str], Field(description=f"Date of the request (starts notice clocks). {D}")] = None,
        as_of: Annotated[Optional[str], Field(description=D)] = None,
    ) -> LeaveResult:
        return leave_request_check(leave_type, employee_count, employees_at_location, private_employer,
                                   hours_requested, hours_used_this_year, hours_used_this_month, school_emergency,
                                   days_requested, days_used_this_year, months_employed, hours_worked_last_12_months,
                                   employees_within_75_miles, fmla_covered_workweeks, weeks_requested,
                                   weeks_used_last_12_months, military_caregiver, subcontractor_written_request,
                                   request_date, as_of)
