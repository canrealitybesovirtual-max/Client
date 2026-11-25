"""Company compliance calendar and labor-burden estimator for California contractors.

  compliance_calendar    the year's dated obligations: EDD/IRS payroll returns and deposits, new-hire and contractor
                         reports, W-2/1099/940 year-end filings, CSLB renewal, bonds, LLC insurance, workers' comp,
                         workplace postings, Cal/OSHA Log 300A, workplace violence plan, DIR public works
                         registration, city business tax reminders. Each item has the date, who files, form, penalty
                         for being late and its citations.
  labor_burden_estimate  employer cost of one worker for a year: employer FICA, FUTA with California's credit
                         reduction, CA UI and ETT, workers' comp premium, optional paid sick leave; SDI shown as an
                         employee withholding. Every line shows its math.

Every legal number comes from the rulebook (core.rule(id).param(key)) when the function runs. Values the rulebook does
not hold live in UNVERIFIED with a source URL, and any item or result that uses one has verified=False.
"""
import calendar
import re
from collections import Counter
from datetime import date as Date, timedelta
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
FICA, FUTA, FUTA_2026 = "federal-fica-employer-2026", "federal-futa-ca-credit-reduction", "futa-california-credit-reduction-2026"
UI, ETT, SDI, MIN_WAGE, LOCAL_WAGE = ("ca-ui-tax-2026", "ca-ett-2026", "ca-sdi-2026", "ca-minimum-wage-2026",
                                      "local-minimum-wage-overrides-state")
WC_EST, WC_DUAL, WC_ONE = "wc-premium-estimate", "wc-construction-dual-wage-thresholds", "workers-comp-mandatory-one-employee"
SICK_USE, SICK_ACCRUAL, SICK_CAPS = "paid-sick-leave-40-hours-5-days", "paid-sick-leave-accrual", "paid-sick-leave-caps"
EDD_REG, EDD_REG_DAYS, EIN = ("edd-employer-registration-100-per-quarter", "edd-registration-15-day-deadline",
                              "federal-ein-ss4-within-7-days")
DE9, EFILE, TAX_DEBT = "edd-de9-de9c-quarterly-filing", "edd-efile-epay-mandate-penalties", "outstanding-tax-liabilities-license-suspension"
F941, DEP, DEP_100K, DEP_MIN = ("form-941-quarterly-due-dates", "federal-deposit-schedule-monthly-semiweekly",
                                "federal-deposit-100k-next-day", "federal-deposit-de-minimis-2500")
W2, F940 = "w2-furnish-and-file-jan-31", "form-940-futa-annual-due"
NEC_LIMIT, NEC_DUE, NEC_CORP, BACKUP = ("form-1099-nec-threshold-2000", "form-1099-nec-due-jan-31",
                                        "form-1099-not-required-for-corporations", "backup-withholding-24-percent")
DE542, DE34 = "edd-de542-independent-contractor-report", "new-hire-report-de34-20-days"
I9, NTE, WC_HIRE, PAMPHLETS = ("form-i9-completion-deadlines", "notice-to-employee-2810-5", "workers-comp-new-hire-notice",
                               "edd-required-pamphlets")
LIC_TERM, LIC_POSTMARK, LIC_LATE, FEES, DELINQ = ("license-renewal-every-2-years", "license-renewal-postmark-by-expiration",
                                                  "license-late-renewal-rules", "cslb-fee-schedule-2026",
                                                  "fees-delinquency-50-percent")
WC_CODES, WC_RECERT = "workers-comp-classification-codes-at-renewal", "wc-renewal-recertify-exemption"
BOND, QUAL_BOND, LLC_BOND = ("web_business-tax-insurance:contractor-license-bond-25000", "qualifier-bond-25000",
                             "llc-employee-worker-bond-100000")
BOND_90, BOND_CANCEL = "bond-received-within-90-days", "bond-cancellation-30-day-suspension"
LLC_INS, LLC_REINSTATE = "web_project-management:llc-liability-insurance-minimum", "llc-insurance-reinstate-limit"
WC_CERT_90, WC_LAPSE, WC_MANDATORY = ("wc-certificate-accepted-within-90-days", "wc-lapse-automatic-suspension",
                                      "web_business-tax-insurance:wc-mandatory-classifications")
WC_FIRST_HIRE, WC_2028, WC_CSLB_PEN, WC_LC3722, STOP_ORDER, WC_POSTER = (
    "wc-proof-within-90-days-of-hire", "wc-required-all-licensees-from-2028", "wc-uninsured-licensee-civil-penalties",
    "uninsured-employer-penalties-lc-3722", "stop-order-no-employee-labor", "workers-comp-notice-to-employees-poster")
POSTINGS, CALOSHA_POSTER, WO16_POST, PAYDAY_POST = ("required-workplace-postings", "calosha-poster-posting",
                                                    "wage-order-16-posting", "payday-notice-posting")
KYR, LOG_SIZE, LOG_POST, LOG_MAIL, LOG_CERT, ITA = (
    "workplace-know-your-rights-annual-notice", "log300-size-exemption-10-or-fewer", "log300a-posting-feb1-apr30",
    "log300a-mail-to-field-employees", "log300a-annual-summary-certification", "osha-ita-electronic-submission-march-2")
HARASS, LOG_RETAIN = "harassment-prevention-training", "log300-retention-5-years"
CITY_STMT, CITY_PERMIT = "city-business-license-cslb-statement", "business-tax-proof-before-permit"
FTB_LLC, SOI_LLC, SOI_CORP, SOI_PEN, SOS_GOOD = ("ftb-llc-annual-tax-800", "sos-statement-of-information-llc",
                                                 "sos-statement-of-information-corporation",
                                                 "sos-statement-of-information-penalty-250",
                                                 "sos-good-standing-or-cslb-suspension")
EST_TAX = "estimated-income-tax-owners"
NO_SUE = "unlicensed-no-right-to-sue-disgorgement"
EDD_REG_LB, I9_PEN = "lawbook-edd-registration-100-per-quarter", "form-i9-civil-penalties"
WVPP_PLAN, WVPP_TRAIN, WVPP_REVIEW, WVPP_EXEMPT, WVPP_RECORDS = (
    "wvpp-written-plan-required", "wvpp-training-initial-and-annual", "wvpp-plan-review-annual", "wvpp-exemptions",
    "wvpp-records-retention")
PW_REG, PW_FEE, PW_LATE, PW_UNREG, PW_REQ = ("dir-registration-fiscal-year-renewal", "dir-registration-fee",
                                             "dir-registration-late-renewal-grace", "unregistered-contractor-penalty",
                                             "dir-registration-required-to-bid-or-work")

# Values with no rule in the rulebook. An item or result that uses one is marked verified=False.
UNVERIFIED = {
    "federal_legal_holidays": {
        "source": "https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/",
        "note": "5 U.S.C. 6103 holidays (Saturday holidays observed Friday, Sunday holidays Monday), plus DC "
                "Emancipation Day (April 16) for IRS dates (IRC 7503 counts District of Columbia legal holidays). "
                "Used to move an IRS/EDD due date that lands on one to the next business day and to skip them when "
                "counting Form I-9 business days. California-only state holidays are not applied.",
    },
    "futa_deposit_threshold": {
        "usd": 500,
        "source": "https://www.irs.gov/instructions/i940",
        "note": "Figure each quarter's FUTA liability at the 0.6% net rate; the California credit reduction is added "
                "to the 4th-quarter liability (Form 940 Part 5, line 16d). Deposit once the undeposited liability is "
                "more than $500, by the last day of the month after the quarter; $500 or less carries to the next "
                "quarter, and a 4th-quarter balance of $500 or less may be paid with Form 940 by January 31.",
    },
    "ca_ui_rate_range": {
        "min_percent": 1.5, "max_percent": 6.2,
        "source": "https://edd.ca.gov/en/payroll_taxes/rates_and_withholding/",
        "note": "California has been on UI rate Schedule F+ (rates 1.5% to 6.2%) while its UI fund is insolvent, so "
                "no employer has a UI rate under 1%. Used only to read a ui_rate below 1 (e.g. 0.034) as a decimal "
                "and to flag a rate outside that range.",
    },
    "additional_medicare_tax": {
        "rate_percent": 0.9, "wage_threshold_usd": 200000,
        "source": "https://www.irs.gov/taxtopics/tc560",
        "note": "Additional Medicare Tax (IRC 3101(b)(2), 3102(f)): the employer must withhold an extra 0.9% from an "
                "employee's wages above $200,000 in the calendar year. Employee-only; there is no employer match.",
    },
    "statewide_election_dates": {
        "dates": {"2026": ["2026-06-02", "2026-11-03"]},
        "source": "https://www.sos.ca.gov/elections/upcoming-elections",
        "note": "California statewide primary and general election dates for 2026. Special statewide elections are "
                "not listed; check the Secretary of State.",
    },
    "edd_late_penalties": {
        "source": "https://edd.ca.gov/siteassets/files/pdf_pub_ctr/de44.pdf",
        "note": "EDD charges penalties and interest for late DE 9/DE 9C returns and late deposits (California "
                "Employer's Guide DE 44). The amounts are not in the verified rulebook.",
    },
    "edd_de88_deposit_schedule": {
        "source": "https://edd.ca.gov/en/payroll_taxes/Due_Dates_Calendar/",
        "note": "California income tax (PIT) and SDI withheld are deposited with EDD (DE 88, electronically) on EDD's "
                "own schedule, which depends on your federal deposit schedule and how much PIT you withheld. UI and "
                "ETT are paid quarterly on DE 88 by the same date as the DE 9. Check EDD's deposit chart; it is not "
                "in the verified rulebook.",
    },
    "irs_failure_to_deposit": {
        "source": "https://www.irs.gov/payments/failure-to-deposit-penalty",
        "note": "IRS failure-to-deposit penalty (IRC 6656): a percentage of the late deposit that rises with the days "
                "late. Amounts not in the verified rulebook.",
    },
    "irs_failure_to_file": {
        "source": "https://www.irs.gov/payments/failure-to-file-penalty",
        "note": "IRS failure-to-file and failure-to-pay penalties (IRC 6651) plus interest. Amounts not in the "
                "verified rulebook.",
    },
    "irs_information_return_penalties": {
        "source": "https://www.irs.gov/government-entities/federal-state-local-governments/information-return-penalties",
        "note": "Per-form penalties for late or missing W-2s and 1099s (IRC 6721/6722); they grow with lateness and "
                "are adjusted every year. Amounts not in the verified rulebook.",
    },
}

Entity = Literal["sole_owner", "partnership", "corporation", "llc", "joint_venture"]
Category = Literal["payroll_tax", "year_end", "new_hire", "contractor_reporting", "license", "insurance_bond",
                   "posting", "safety_records", "training", "entity", "local_tax"]
Status = Literal["passed", "due_soon", "upcoming", "event_driven", "needs_date"]
DUE_SOON_DAYS = 30          # display threshold for "due_soon", not a legal number
MAX_EXPERIENCE_MOD = 10     # input sanity bound (a mod of 92 is a typo for 0.92), not a legal number


def P(rule_id: str, key: str, default=KeyError):
    return core.rule(rule_id).param(key, default)


RATE_YEAR = P(FICA, "year")  # the year the rulebook's payroll rates and dollar amounts are for


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def _rate(x: float) -> str:
    """Dollar rate (hourly wage, WC rate per $100) shown with up to 4 decimals so the math reads exactly."""
    return _usd(x) if abs(round(x, 2) - x) < 1e-9 else f"${x:,.4f}".rstrip("0")


def _pct(x: float) -> str:
    return f"{x:g}%"


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _int_in(text, pattern: str, rid: str) -> int:
    """Read a number the rulebook states in words (e.g. '15th day of following month')."""
    m = re.search(pattern, str(text))
    if not m:
        raise ValueError(f"rule {rid} text changed, cannot read {pattern!r} from {text!r}")
    return int(m.group(1))


def _month_day(text: str, year: int, rid: str) -> Date:
    """'02-01', '2026-02-01' or 'January 31 ...' from a rule parameter -> that month/day in `year`."""
    s = str(text)
    m = re.search(r"(?:\d{4}-)?(\d{2})-(\d{2})", s)
    if m:
        return Date(year, int(m[1]), int(m[2]))
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})", s)
    if not m:
        raise ValueError(f"rule {rid}: cannot read a month and day from {text!r}")
    return Date(year, list(calendar.month_name).index(m[1]), int(m[2]))


def _date(value, field: str) -> Date:
    if isinstance(value, Date):
        return value
    try:
        return Date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2026-03-15, got {value!r}") from None


def _opt_date(value, field: str) -> Optional[Date]:
    """None for a missing or blank value, else the parsed date."""
    return _date(value, field) if value is not None and str(value).strip() else None


def _dates(values, field: str) -> list[Date]:
    """Parse a list of ISO dates, skipping blank entries."""
    return [d for d in (_opt_date(v, field) for v in values or []) if d]


# ---------------------------------------------------------------- business-day math (IRS / EDD weekend roll)
def _nth(year: int, month: int, weekday: int, n: int) -> Date:
    if n > 0:
        d = Date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    d = Date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _holidays(year: int, irs: bool = False) -> set[Date]:
    fixed = [Date(year, m, d) for m, d in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25))]
    if irs:
        fixed.append(Date(year, 4, 16))         # DC Emancipation Day counts for IRS deadlines (IRC 7503)
    observed = {d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d for d in fixed}
    floating = {_nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _nth(year, 5, 0, -1), _nth(year, 9, 0, 1),
                _nth(year, 10, 0, 2), _nth(year, 11, 3, 4)}
    return observed | floating


def _roll(d: Date, irs: bool = False) -> tuple[Date, list[str]]:
    """Next business day if `d` is a weekend or federal holiday (26 CFR 301.7503-1 / EDD next-business-day rule)."""
    hol = _holidays(d.year, irs) | _holidays(d.year + 1, irs)
    hit = False
    while d.weekday() >= 5 or d in hol:
        hit = hit or d in hol
        d += timedelta(days=1)
    return d, (["federal_legal_holidays"] if hit else [])


def _add_business_days(d: Date, n: int) -> tuple[Date, list[str]]:
    hol = _holidays(d.year) | _holidays(d.year + 1)
    hit = False
    while n > 0:
        d += timedelta(days=1)
        if d in hol and d.weekday() < 5:
            hit = True
        if d.weekday() < 5 and d not in hol:
            n -= 1
    return d, (["federal_legal_holidays"] if hit else [])


def _month_after_end(year: int, month: int) -> Date:
    """Last day of the month after `month` (DE 9/941 rule: last day of the month after the quarter)."""
    y, m = (year + 1, 1) if month == 12 else (year, month + 1)
    return Date(y, m, calendar.monthrange(y, m)[1])


# ---------------------------------------------------------------- models
class Obligation(BaseModel):
    title: str
    category: Category
    due_date: Optional[str] = Field(None, description="Act by this date (ISO). None for event-driven items or when "
                                                       "the date depends on your own policy/certificate")
    window_end: Optional[str] = Field(None, description="For posting periods: keep posted until this date")
    remind_on: Optional[str] = None
    covers: str = ""
    form: str = ""
    agency: str = ""
    who_files: str = ""
    how: str = ""
    trigger: Optional[str] = Field(None, description="For event-driven items: what starts the clock")
    penalty_if_late: str = ""
    days_until_due: Optional[int] = None
    status: Status = "upcoming"
    math: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    unverified_values: list[str] = Field(default_factory=list, description="Keys of UNVERIFIED used by this item")
    verified: bool = False


class DepositSchedule(BaseModel):
    schedule: Literal["monthly", "semiweekly", "pay_with_quarterly_return"]
    quarterly_payroll_usd: float
    estimated_quarterly_fica_usd: float
    quarterly_income_tax_withheld_usd: Optional[float] = None
    estimated_quarterly_federal_tax_usd: float
    estimated_lookback_usd: float
    math: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)


class ComplianceCalendarResult(core.Result):
    year: int
    as_of: str
    entity_type: str
    is_llc: bool
    has_employees: bool
    employee_count: int
    items: list[Obligation] = Field(default_factory=list)
    next_due: Optional[Obligation] = None
    counts: dict[str, int] = Field(default_factory=dict)
    deposit_schedule: Optional[DepositSchedule] = None
    notes: list[str] = Field(default_factory=list)
    unverified_values_used: dict[str, dict] = Field(default_factory=dict)


def _ob(title: str, category: str, rules, due: Optional[Date] = None, unv=(), **kw) -> Obligation:
    rules = list(dict.fromkeys(rules))
    unv = list(dict.fromkeys(unv))
    for k in ("window_end", "remind_on"):
        if isinstance(kw.get(k), Date):
            kw[k] = kw[k].isoformat()
    return Obligation(title=title, category=category, due_date=due.isoformat() if due else None, rule_ids=rules,
                      unverified_values=unv, verified=all(core.rule(r).verified for r in rules) and not unv, **kw)


# ---------------------------------------------------------------- shared tax math
def _fica_rates() -> tuple[float, float, float]:
    """(social security %, Social Security wage base, Medicare %) - same rate for employer and employee."""
    return P(FICA, "social_security_rate_percent"), P(FICA, "social_security_wage_base"), P(FICA, "medicare_rate_percent")


def _semiweekly() -> str:
    return "; ".join(f"{k} -> {v}" for k, v in P(DEP, "semiweekly_rules").items())


def _futa_rate(year: int) -> tuple[float, float, list[str], list[str]]:
    """(net FUTA %, CA credit reduction %, rule ids, notes) for wages paid in `year`."""
    net = P(FUTA, "net_rate_without_reduction_percent")
    notes = []
    red = P(FUTA, f"ca_credit_reduction_{year}_percent", None)
    rules = [FUTA]
    if red is None:
        red = P(FUTA_2026, f"ca_credit_reduction_percent.{year}", None)
        rules.append(FUTA_2026)
        if red is None:
            red = P(FUTA_2026, "ca_credit_reduction_percent.2026")
            notes.append(f"The {year} California FUTA credit reduction is not in the rulebook; the 2026 figure "
                         f"({_pct(red)}) was used. It usually grows each year California's federal loan is unpaid.")
        else:
            notes.append(f"California's {year} FUTA credit reduction of {_pct(red)} is EDD's projection from the "
                         "draft Form 940 Schedule A; the U.S. Department of Labor confirms the final rate after "
                         "November 10. Check Schedule A before filing Form 940.")
    return net + red, red, rules, notes


def _lookback_quarter_ends(year: int) -> list[Date]:
    """Quarter-end dates in the Form 941 lookback period that sets the deposit schedule for `year`
    (the rulebook's '12 months ended preceding June 30': July 1 two years back through June 30 last year)."""
    text = P(DEP, "lookback_period_form_941")
    end = _month_day(text, year - 1, DEP)
    ends = []
    for i in range(_int_in(text, r"(\d+) months", DEP) // 3):
        y, m = divmod(end.year * 12 + end.month - 1 - 3 * i, 12)
        ends.append(Date(y, m + 1, calendar.monthrange(y, m + 1)[1]))
    return ends


def payroll_deposit_schedule(quarterly_payroll_usd: float, quarterly_income_tax_withheld_usd: Optional[float] = None,
                             new_employer: bool = False, lookback_quarters: Optional[int] = None) -> DepositSchedule:
    """Federal deposit schedule basis (monthly vs semiweekly vs pay with the return) from a steady quarterly payroll.
    `lookback_quarters`: how many lookback quarters had payroll (default all of them; 0 = new employer)."""
    if quarterly_payroll_usd < 0 or (quarterly_income_tax_withheld_usd or 0) < 0:
        raise ValueError("quarterly payroll and income tax withheld cannot be negative")
    ss, _, med = _fica_rates()
    combined = round(2 * (ss + med), 6)
    fica = _money(quarterly_payroll_usd * combined / 100)
    fit = quarterly_income_tax_withheld_usd
    tax = _money(fica + (fit or 0))
    lookback_limit, de_min = P(DEP, "lookback_threshold_usd"), P(DEP_MIN, "threshold_usd")
    period = P(DEP, "lookback_period_form_941")
    total_q = _int_in(period, r"(\d+) months", DEP) // 3
    n_q = 0 if new_employer else total_q if lookback_quarters is None else max(0, min(lookback_quarters, total_q))
    lookback = _money(n_q * tax)
    math = [f"Social Security + Medicare, employer and employee halves: {_usd(quarterly_payroll_usd)} x "
            f"2 x ({_pct(ss)} + {_pct(med)}) = {_usd(quarterly_payroll_usd)} x {_pct(combined)} = {_usd(fica)}"]
    if fit is not None:
        math.append(f"Plus federal income tax withheld {_usd(fit)} = {_usd(tax)} per quarter")
    notes = []
    if n_q == 0:
        math.append(f"New employer: no payroll in the lookback period ({period}), so the lookback total is $0")
    elif n_q < total_q:
        math.append(f"Lookback ({period}): payroll in {n_q} of its {total_q} quarters, assuming a steady payroll: "
                    f"{n_q} x {_usd(tax)} = {_usd(lookback)}")
    else:
        math.append(f"Lookback ({period}), assuming a steady payroll: {total_q} x {_usd(tax)} = {_usd(lookback)}")
    if tax < de_min:
        schedule = "pay_with_quarterly_return"
        math.append(f"{_usd(tax)} is under {_usd(de_min)} for the quarter, so the tax may be paid with a timely "
                    "Form 941 instead of deposits")
        notes.append(f"Only while the quarter (or the prior quarter) stays under {_usd(de_min)} and no "
                     f"{_usd(P(DEP_100K, 'threshold_usd'))} day occurs; otherwise you deposit on your monthly or "
                     "semiweekly schedule.")
    elif lookback <= lookback_limit:
        schedule = "monthly"
        math.append(f"{_usd(lookback)} <= {_usd(lookback_limit)}: monthly depositor (deposit each month's tax by the "
                    f"{P(DEP, 'monthly_due')})")
    else:
        schedule = "semiweekly"
        math.append(f"{_usd(lookback)} > {_usd(lookback_limit)}: semiweekly depositor ({_semiweekly()})")
    if fit is None:
        notes.append("Federal income tax withheld was not given, so this counts only Social Security and Medicare. "
                     f"Add the income tax you withhold: it can push you over the {_usd(de_min)}-a-quarter or "
                     f"{_usd(lookback_limit)} lookback line.")
    notes.append(f"If you ever owe {_usd(P(DEP_100K, 'threshold_usd'))} or more on one day, deposit by the next "
                 "business day; you then become a semiweekly depositor for the rest of the year and the next year.")
    notes.append(f"You must work out your schedule before each calendar year from the tax actually reported on your "
                 f"Forms 941 for the lookback period ({period}); this is an estimate from a steady payroll.")
    return DepositSchedule(schedule=schedule, quarterly_payroll_usd=quarterly_payroll_usd,
                           estimated_quarterly_fica_usd=fica, quarterly_income_tax_withheld_usd=fit,
                           estimated_quarterly_federal_tax_usd=tax, estimated_lookback_usd=lookback, math=math,
                           notes=notes, rule_ids=[FICA, DEP, DEP_MIN, DEP_100K])


# ---------------------------------------------------------------- calendar sections
def _payroll_items(year: int, start: Optional[Date], q_payroll: Optional[float], n_emp: int,
                   fit: Optional[float], new_employer: bool) -> tuple[list[Obligation], Optional[DepositSchedule]]:
    items: list[Obligation] = []
    edd_late = ("EDD late-filing/late-payment penalties and interest (amounts: EDD DE 44). Unpaid final EDD "
                f"liabilities can also block CSLB renewal or suspend your license after a "
                f"{P(TAX_DEBT, 'preliminary_notice_days_min')}-day notice (B&P 7145.5).")
    paper = (f"Filing on paper instead of e-Services: {_usd(P(EFILE, 'paper_de9_penalty_usd'))} per DE 9 and "
             f"{_usd(P(EFILE, 'paper_de9c_penalty_per_wage_item_usd'))} per wage item on a paper DE 9C.")

    def active(period_end: Date) -> bool:
        return start is None or period_end >= start

    if start:
        reg_days = P(EDD_REG_DAYS, "deadline_days_after_threshold_crossed")
        reg_limit = _usd(P(EDD_REG, "wage_threshold_usd_per_calendar_quarter"))
        reg_due = start + timedelta(days=reg_days)
        if reg_due.year >= year:        # registration that fell due in an earlier year is not on this calendar
            items.append(_ob(
                "Register as an employer with EDD (payroll tax account)", "payroll_tax",
                [EDD_REG, EDD_REG_LB, EDD_REG_DAYS, TAX_DEBT],
                reg_due, ["edd_late_penalties"], form="e-Services for Business (or DE 1)", agency="EDD",
                who_files="Owner or bookkeeper", how="Online through EDD e-Services for Business",
                covers=f"Wages first paid {start.isoformat()}",
                trigger=f"Paying more than {reg_limit} in total wages in a calendar quarter",
                math=[f"{start.isoformat()} + {reg_days} days = {reg_due.isoformat()} (assumes your first payroll put "
                      f"the quarter over {reg_limit})"],
                penalty_if_late=edd_late,
                notes=[f"The {reg_days} days run from the date the quarter's total wages first pass {reg_limit}, not "
                       "from each employee's pay.",
                       "Not an IRS/EDD return date, so it is not moved for weekends; act by the prior business day."]))
        ein_due = start + timedelta(days=P(EIN, "days_after_first_wage_payment"))
        if ein_due.year >= year:
            items.append(_ob(
                "Get a federal EIN (if you don't have one)", "payroll_tax", [EIN], ein_due, form="IRS Form SS-4",
                agency="IRS", who_files="Owner", how="IRS online EIN application or Form SS-4",
                math=[f"{start.isoformat()} + {P(EIN, 'days_after_first_wage_payment')} days = {ein_due.isoformat()}"],
                penalty_if_late="Without an EIN you cannot make deposits or file Form 941/W-2s on time.",
                covers="Needed for deposits, Form 941 and W-2s"))

    deposit = None
    if q_payroll is not None:
        # wages that began during or after the lookback period shorten it (a new employer's lookback is $0)
        lookback_q = None if start is None else sum(e >= start for e in _lookback_quarter_ends(year))
        deposit = payroll_deposit_schedule(q_payroll, fit, new_employer, lookback_q)

    # quarterly returns: Q4 of last year (due this year) through Q4 of this year
    for qy, q in [(year - 1, 4)] + [(year, i) for i in range(1, 5)]:
        q_end_month = q * 3
        q_end = Date(qy, q_end_month, calendar.monthrange(qy, q_end_month)[1])
        if not active(q_end):
            continue
        raw = _month_after_end(qy, q_end_month)
        due, unv = _roll(raw)
        moved = [f"{raw.isoformat()} is a {raw.strftime('%A')}{' or holiday' if unv else ''}; moves to "
                 f"{due.isoformat()}"] if due != raw else []
        items.append(_ob(
            f"EDD DE 9 and DE 9C for Q{q} {qy}", "payroll_tax", [DE9, EFILE, TAX_DEBT], due,
            unv + ["edd_late_penalties", "edd_de88_deposit_schedule"],
            form="DE 9 (Quarterly Contribution Return) + DE 9C (wage detail)",
            agency="EDD", who_files="Employer or its payroll service", how="Electronically (e-Services for Business)",
            covers=f"Wages paid Q{q} {qy}", math=[f"Last day of the month after Q{q}: {raw.isoformat()}"] + moved,
            penalty_if_late=f"{edd_late} {paper}",
            notes=[f"EDD lists it as due on the 1st of the month after the quarter and delinquent if not filed by "
                   f"{due.isoformat()}.", "File every quarter once registered, even with no payroll.",
                   "Pay the quarter's UI and ETT (DE 88, electronically) by the same date. California income tax "
                   "and SDI withheld go on EDD's own deposit schedule (see unverified_values_used)."]))
        ext = _int_in(P(F941, "extended_due_if_timely_deposits"), r"(\d+)\w* day", F941)
        due, unv = _roll(raw, irs=True)
        f941_notes = [f"If every deposit for the quarter was on time and in full you have until the {_ord(ext)} day "
                      "of the second month after the quarter."]
        if deposit is not None and deposit.schedule == "pay_with_quarterly_return":
            f941_notes.insert(0, f"Your estimated tax is under {_usd(P(DEP_MIN, 'threshold_usd'))} a quarter, so you "
                                 "may pay it with this timely filed return instead of depositing it (only while this "
                                 "or the prior quarter stays under that amount; see deposit_schedule).")
        items.append(_ob(
            f"IRS Form 941 for Q{q} {qy}", "payroll_tax", [F941] + ([DEP_MIN] if len(f941_notes) > 1 else []), due,
            unv + ["irs_failure_to_file"],
            form="Form 941", agency="IRS", who_files="Employer or its payroll service",
            how="E-file or mail", covers=f"Federal income tax withheld, Social Security and Medicare, Q{q} {qy}",
            math=[f"Last day of the month after Q{q}: {raw.isoformat()}"] + moved,
            penalty_if_late=UNVERIFIED["irs_failure_to_file"]["note"], notes=f941_notes))

    if deposit is not None:
        dep_pen = (f"{UNVERIFIED['irs_failure_to_deposit']['note']} EDD: "
                   f"{_pct(P(EFILE, 'non_electronic_payment_penalty_percent'))} of any payment not made "
                   "electronically, plus late penalties and interest.")
        if deposit.schedule == "monthly":
            day = _int_in(P(DEP, "monthly_due"), r"(\d+)", DEP)
            est = _money(deposit.estimated_quarterly_federal_tax_usd / 3)
            for my, m in [(year - 1, 12)] + [(year, i) for i in range(1, 13)]:
                m_end = Date(my, m, calendar.monthrange(my, m)[1])
                if not active(m_end):
                    continue
                ny, nm = (my + 1, 1) if m == 12 else (my, m + 1)
                raw = Date(ny, nm, day)
                due, unv = _roll(raw, irs=True)
                items.append(_ob(
                    f"Payroll tax deposit for {calendar.month_name[m]} {my}", "payroll_tax",
                    [DEP, F941, FICA, EFILE], due, unv + ["irs_failure_to_deposit", "edd_de88_deposit_schedule"],
                    form="EFTPS federal deposit (941 taxes); EDD DE 88 for CA PIT/SDI", agency="IRS / EDD",
                    who_files="Employer or its payroll service", how="Electronic funds transfer (EFTPS) / EDD e-Services",
                    covers=f"Federal taxes on wages paid in {calendar.month_name[m]} {my}",
                    math=[f"{P(DEP, 'monthly_due')}: {raw.isoformat()}"
                          + (f" -> next business day {due.isoformat()}" if due != raw else ""),
                          f"Estimated deposit: {_usd(deposit.estimated_quarterly_federal_tax_usd)} / 3 = {_usd(est)}"],
                    penalty_if_late=dep_pen, notes=[UNVERIFIED["edd_de88_deposit_schedule"]["note"]]))
        elif deposit.schedule == "semiweekly":
            items.append(_ob(
                "Semiweekly payroll tax deposits (every payday)", "payroll_tax", [DEP, DEP_100K, EFILE],
                None, ["irs_failure_to_deposit", "edd_de88_deposit_schedule"], form="EFTPS; EDD DE 88",
                agency="IRS / EDD", who_files="Employer or its payroll service", how="Electronic funds transfer",
                trigger=f"Each payday ({_semiweekly()})", covers=f"All {year} paydays", penalty_if_late=dep_pen,
                notes=[UNVERIFIED["edd_de88_deposit_schedule"]["note"]]))
        if n_emp and q_payroll > 0:
            items.extend(_futa_deposits(year, q_payroll, n_emp, start))
            if start.year < year if start else not new_employer:      # wages were paid last year
                # last year's Q4 FUTA (with its credit reduction) is due in January of this year
                for it in _futa_deposits(year - 1, q_payroll, n_emp, start):
                    if it.due_date and it.due_date[:4] == str(year):
                        it.notes.append(f"Assumes the same quarterly payroll and headcount in {year - 1}"
                                        + (f" from {start.isoformat()}." if start and start.year == year - 1 else "."))
                        items.append(it)
    return items, deposit


def _futa_deposits(year: int, q_payroll: float, n_emp: int, start: Optional[Date]) -> list[Obligation]:
    """FUTA deposits for wages paid in `year`: each quarter at the 0.6% net rate, the California credit reduction
    added to the 4th quarter (IRS Form 940 instructions), deposited once the undeposited total is over $500."""
    total, red, rules, rate_notes = _futa_rate(year)
    net = P(FUTA, "net_rate_without_reduction_percent")
    base = P(FUTA, "taxable_wage_limit_per_employee")
    limit = UNVERIFIED["futa_deposit_threshold"]["usd"]
    per_emp = q_payroll / n_emp
    notes = rate_notes + [UNVERIFIED["futa_deposit_threshold"]["note"],
                          "Assumes the same employees all year at an even quarterly payroll."]
    items, carried, math, k, year_taxable = [], 0.0, [], 0, 0.0
    for q in range(1, 5):
        q_end = Date(year, q * 3, calendar.monthrange(year, q * 3)[1])
        if start and q_end < start:
            continue
        k += 1          # quarters with payroll so far this year
        taxable = n_emp * (min(k * per_emp, base) - min((k - 1) * per_emp, base))
        year_taxable += taxable
        tax = _money(taxable * net / 100)
        carried = _money(carried + tax)
        math.append(f"Q{q}: {_n(n_emp, 'employee')} x taxable wages {_usd(_money(taxable / n_emp))} each (first "
                    f"{_usd(base)} per employee per year) x {_pct(net)} = {_usd(tax)}; undeposited {_usd(carried)}")
        if q == 4 and red:
            cr = _money(year_taxable * red / 100)
            carried = _money(carried + cr)
            math.append(f"Q4 adds the California credit reduction for the whole year: {_usd(_money(year_taxable))} "
                        f"taxable FUTA wages x {_pct(red)} = {_usd(cr)}; undeposited {_usd(carried)}")
        due, unv = _roll(_month_after_end(year, q * 3), irs=True)
        common = dict(agency="IRS", who_files="Employer or its payroll service", notes=list(notes),
                      penalty_if_late=UNVERIFIED["irs_failure_to_deposit"]["note"])
        if carried > limit:
            items.append(_ob(
                f"FUTA deposit for Q{q} {year}", "payroll_tax", rules + [F940], due,
                unv + ["futa_deposit_threshold", "irs_failure_to_deposit"], form="EFTPS (Form 940 tax)",
                how="Electronic funds transfer", covers=f"FUTA on wages paid through Q{q} {year}",
                math=math + [f"{_usd(carried)} is more than {_usd(limit)}: deposit by {due.isoformat()}"], **common))
            carried, math = 0.0, []
        elif q == 4 and carried:
            items.append(_ob(
                f"FUTA for {year}: pay the {_usd(carried)} balance with Form 940", "payroll_tax", rules + [F940], due,
                unv + ["futa_deposit_threshold", "irs_failure_to_deposit"], form="Form 940 (or an EFTPS deposit)",
                covers=f"FUTA on {year} wages not yet deposited",
                math=math + [f"{_usd(carried)} is not over {_usd(limit)}: deposit it or pay it with the {year} Form 940 "
                             f"by {due.isoformat()}"], **common))
    return items


def _year_end_items(year: int, has_employees: bool, pays_ics: bool, start: Optional[Date]) -> list[Obligation]:
    items = []
    for wy in (year - 1, year):
        if has_employees and not (start and start.year > wy):     # no W-2/940 for a year before wages began
            raw = _month_day(P(W2, "furnish_to_employee_by"), wy + 1, W2)
            due, unv = _roll(raw)
            items.append(_ob(
                f"Forms W-2 / W-3 for {wy} wages", "year_end", [W2, F941], due,
                unv + ["irs_information_return_penalties"], form="W-2 (each employee) + W-3 transmittal",
                agency="SSA / IRS", who_files="Employer or its payroll service",
                how="Give each employee a W-2 and file with the Social Security Administration (e-file)",
                covers=f"Wages paid in {wy}",
                math=[raw.isoformat() + (f" -> next business day {due.isoformat()}" if due != raw else "")],
                penalty_if_late=UNVERIFIED["irs_information_return_penalties"]["note"]))
            raw = _month_day(P(F940, "due"), wy + 1, F940)
            due, unv = _roll(raw)
            rate, red, rules, rate_notes = _futa_rate(wy)
            items.append(_ob(
                f"IRS Form 940 (FUTA) for {wy}", "year_end", [F940, F941] + rules, due,
                unv + ["irs_failure_to_file"], form="Form 940 + Schedule A (California credit reduction)",
                agency="IRS", who_files="Employer or its payroll service", how="E-file or mail",
                covers=f"Federal unemployment tax on {wy} wages",
                math=[raw.isoformat() + (f" -> next business day {due.isoformat()}" if due != raw else ""),
                      f"FUTA rate for {wy}: {_pct(P(FUTA, 'net_rate_without_reduction_percent'))} + California "
                      f"credit reduction {_pct(red)} = {_pct(round(rate, 4))} on the first "
                      f"{_usd(P(FUTA, 'taxable_wage_limit_per_employee'))} per employee"],
                penalty_if_late=UNVERIFIED["irs_failure_to_file"]["note"],
                notes=[f"You have until {P(F940, 'extended_due_if_timely_deposits')} if all FUTA tax was deposited "
                       "on time."] + rate_notes))
        if pays_ics:
            limits = P(NEC_LIMIT, "threshold_usd")
            limit = limits["payments_before_2026"] if wy < 2026 else limits["payments_in_2026"]   # rule's own keys
            raw = _month_day(P(NEC_DUE, "file_with_irs_by"), wy + 1, NEC_DUE)
            due, unv = _roll(raw)
            notes = ["Not needed for payments to corporations (except attorneys and medical/health care); collect a "
                     "W-9 from every sub before the first payment.",
                     f"No W-9 / no TIN: withhold {_pct(P(BACKUP, 'rate_percent'))} backup withholding."]
            if wy > P(NEC_LIMIT, "inflation_adjusted_after"):
                notes.append(f"The 1099-NEC threshold is inflation-adjusted after 2026; {_usd(limit)} (the 2026 "
                             "figure) is shown. Check the current IRS instructions.")
            items.append(_ob(
                f"Forms 1099-NEC for {wy} payments to subs", "year_end", [NEC_LIMIT, NEC_DUE, NEC_CORP, BACKUP], due,
                unv + ["irs_information_return_penalties"], form="1099-NEC (+ 1096 if filing on paper)",
                agency="IRS", who_files="Business that paid the subcontractor",
                how="File with the IRS and give each recipient a copy",
                covers=f"Nonemployee pay of {_usd(limit)} or more per payee in {wy}",
                math=[raw.isoformat() + (f" -> next business day {due.isoformat()}" if due != raw else ""),
                      f"Threshold for {wy} payments: {_usd(limit)}"],
                penalty_if_late=UNVERIFIED["irs_information_return_penalties"]["note"], notes=notes))
    return items


def _hire_items(year: int, has_employees: bool, pays_ics: bool, hire_dates: list[Date],
                ic_dates: list[Date]) -> tuple[list[Obligation], list[str]]:
    items, skipped = [], []
    de34_days = P(DE34, "deadline_calendar_days_after_start_of_work")
    de34_pen = (f"{_usd(P(DE34, 'penalty_usd_per_failure'))} per unreported hire, or "
                f"{_usd(P(DE34, 'penalty_usd_per_failure_conspiracy_or_false_report'))} if the failure is a "
                "conspiracy with the employee or a false/incomplete report (waived for good cause).")
    i9_pen = (f"I-9 paperwork violations: {_usd(P(I9_PEN, 'paperwork_violation_per_individual_usd.min'))} to "
              f"{_usd(P(I9_PEN, 'paperwork_violation_per_individual_usd.max'))} per form (federal, inflation-adjusted).")
    de34_kw = dict(form="DE 34 (Report of New Employee(s)) or e-Services", agency="EDD", who_files="Employer",
                   penalty_if_late=de34_pen,
                   notes=["Include part-time and temporary workers.",
                          f"Rehires: report {P(DE34, 'includes_rehires')}.",
                          f"Reporting electronically instead: {P(DE34, 'electronic_or_magnetic_filers')}."])
    if has_employees:
        items.append(_ob("Report each new hire to EDD", "new_hire", [DE34],
                         trigger=f"Within {de34_days} days of the employee's first day of work for pay", **de34_kw))
        i9_days = P(I9, "section_2_deadline_business_days_after_hire")
        items.append(_ob(
            "New-hire paperwork (I-9, wage notice, workers' comp and EDD pamphlets)", "new_hire",
            [I9, I9_PEN, NTE, WC_HIRE, PAMPHLETS],
            trigger="Each hire: Form I-9 Section 1 on day one and Section 2 within "
                    f"{i9_days} business days (a job shorter than {i9_days} business days: both sections on day "
                    "one); Labor Code 2810.5 Notice to Employee at hire (changes within "
                    f"{P(NTE, 'change_notice_calendar_days')} days); workers' comp rights notice by the end of the "
                    "first pay period; EDD pamphlets DE 2515 and DE 2511",
            who_files="Employer (office manager)", form="I-9, DLSE-NTE, DWC time-of-hire pamphlet, DE 2515, DE 2511",
            agency="USCIS / Labor Commissioner / DWC / EDD",
            penalty_if_late=i9_pen,
            notes=["Give notices in Spanish too if you have Spanish-speaking employees."]))
    for h in hire_dates:
        due = h + timedelta(days=de34_days)
        if year not in (h.year, due.year):
            skipped.append(h.isoformat())
            continue
        items.append(_ob(f"DE 34 new-hire report for employee who started {h.isoformat()}", "new_hire", [DE34], due,
                         math=[f"{h.isoformat()} + {de34_days} days = {due.isoformat()}"], **de34_kw))
        i9_due, unv = _add_business_days(h, P(I9, "section_2_deadline_business_days_after_hire"))
        items.append(_ob(f"Form I-9 Section 2 for employee who started {h.isoformat()}", "new_hire", [I9, I9_PEN], i9_due,
                         unv, form="Form I-9 Section 2", agency="USCIS (keep on file)", who_files="Employer",
                         math=[f"{h.isoformat()} + {P(I9, 'section_2_deadline_business_days_after_hire')} business "
                               f"days (Monday-Friday, skipping federal holidays) = {i9_due.isoformat()}"],
                         notes=["Business days are the days your business is open: if you work Saturdays or stay "
                                "open on a holiday, count those days and the deadline comes sooner."],
                         penalty_if_late=i9_pen))
    if pays_ics:
        days, limit = P(DE542, "days"), P(DE542, "threshold_usd")
        de542_kw = dict(form="DE 542 (Report of Independent Contractor(s))", agency="EDD",
                        who_files="Business that hires the subcontractor",
                        penalty_if_late=f"{_usd(P(DE542, 'penalty_per_failure_usd_unverified'))} per unreported "
                                        "contractor (older EDD figure; unverified for 2026).",
                        notes=["Applies to individuals and sole proprietors you must issue a 1099 to; not to "
                               "corporations.",
                               f"EDD's pages still show {_usd(limit)} although the federal 1099-NEC threshold is "
                               f"{_usd(P(NEC_LIMIT, 'threshold_usd.payments_in_2026'))} for 2026 payments; whether "
                               f"EDD now follows the federal figure is not confirmed, so report at {_usd(limit)}.",
                               "Report each contractor once per year; check 'Ongoing' if the work continues."])
        items.append(_ob("Report subcontractors (sole proprietors) to EDD", "contractor_reporting", [DE542, NEC_LIMIT],
                         trigger=f"Within {days} days after you pay {_usd(limit)} or more in the year, or sign a "
                                 f"contract for {_usd(limit)} or more (labor and materials count), whichever is first",
                         **de542_kw))
        for d in ic_dates:
            due = d + timedelta(days=days)
            if year not in (d.year, due.year):
                skipped.append(d.isoformat())
                continue
            items.append(_ob(f"DE 542 for subcontractor paid/contracted {_usd(limit)}+ on {d.isoformat()}",
                             "contractor_reporting", [DE542, NEC_LIMIT], due,
                             math=[f"{d.isoformat()} + {days} days = {due.isoformat()}"], **de542_kw))
    notes = [f"Dates with no {year} deadline were left out: {', '.join(skipped)}. Build that year's calendar for them."
             ] if skipped else []
    return items, notes


def _norm_class(c: str) -> str:
    """'C-10 Electrical', 'c10' -> 'C10'; 'C-61/D-49' -> 'D49' (the last class named); 'Class B' -> 'B'."""
    s = str(c).upper().split("/")[-1]
    m = re.search(r"\b([ABCD])\s*-?\s*(\d{0,2})\b", s)
    return m[1] + m[2] if m else re.sub(r"[^A-Z0-9]", "", s)


def _date_notes(d: Date, year: int, today: Date, what: str) -> list[str]:
    """Notes for a user-entered renewal date that is outside the calendar year or already past."""
    if d < today:
        return [f"This date has passed. If you renewed, enter the new {what} date; if not, act now "
                "(see penalty_if_late)."]
    return [f"{d.isoformat()} is not in {year}; shown so this stays on your list."] if d.year != year else []


def _license_items(year: int, today: Date, entity: str, is_llc: bool, has_employees: bool, n_emp: int,
                   exp: Optional[Date], classes: list[str], bond_date: Optional[Date], wc_date: Optional[Date],
                   liab_date: Optional[Date], personnel: Optional[int]) -> tuple[list[Obligation], list[str]]:
    items, notes = [], []
    sole = entity == "sole_owner" and not is_llc       # an LLC pays the non-sole-owner fee and penalty
    renew = P(FEES, "renewal_active_sole_owner_usd" if sole else "renewal_active_non_sole_owner_usd")
    late = P(FEES, "renewal_active_delinquent_sole_owner_usd" if sole else "renewal_active_delinquent_non_sole_owner_usd")
    pct = P(DELINQ, "delinquency_percent_of_renewal_fee")
    norm = {_norm_class(c) for c in classes}
    c10 = "C10" in norm
    mandatory = sorted({_norm_class(c) for c in P(WC_MANDATORY, "classifications")} & norm)
    surcharge = P(FEES, "c10_active_renewal_surcharge_usd") if c10 else 0
    math = [f"Active renewal fee ({'sole owner' if sole else 'non-sole owner'}): {_usd(renew)}"
            + (f" + C-10 surcharge {_usd(surcharge)} = {_usd(renew + surcharge)}" if c10 else ""),
            f"Late: {_usd(renew)} + {pct}% delinquency fee = {_usd(_money(renew * (1 + pct / 100)))} "
            f"(CSLB schedule: {_usd(late)})" + (f", plus the {_usd(surcharge)} C-10 surcharge" if c10 else "")]
    lic_kw = dict(form="CSLB license renewal application (CSLB mails it before expiration) or online renewal",
                  agency="CSLB", who_files="Licensee (owner, officer or qualifier)",
                  how="Postmark or deliver to CSLB on or before the expiration date",
                  penalty_if_late=(f"Late renewal costs {_usd(late)} instead of {_usd(renew)}. Renewed within "
                                   f"{P(LIC_LATE, 'retroactive_window_days')} days after expiration: retroactive, no "
                                   "gap. Later: you were unlicensed in the gap and cannot sue to collect for work "
                                   f"done then; after {P(LIC_LATE, 'max_years_to_renew')} years you must reapply."),
                  math=math, notes=[
                      f"License term: {P(LIC_TERM, 'term_years')} years. Renew before expiration and keep proof of "
                      "the submission date.",
                      f"The renewal form must list your top {P(WC_CODES, 'codes_required_count')} workers' comp "
                      "class codes by payroll, or recertify your no-employee exemption."])
    lic_rules = [LIC_TERM, LIC_POSTMARK, LIC_LATE, FEES, DELINQ, WC_CODES, NO_SUE]
    if exp is None:
        items.append(_ob("CSLB license renewal", "license", lic_rules, status="needs_date",
                         trigger="Enter license_expiration_date (on your pocket card / CSLB license detail)", **lic_kw))
    elif exp < today:
        # an expiration date already behind us: renewed (stale date) or the license is expired right now
        window = P(LIC_LATE, "retroactive_window_days")
        retro = exp + timedelta(days=window)
        if today <= retro:
            what = (f"If not, it is expired: renew by {retro.isoformat()} ({window} days after expiration, paying "
                    f"{_usd(late)}) and the renewal is retroactive with no gap in licensure. Renew now; do not "
                    "start new work until CSLB shows the license active.")
        else:
            what = (f"If not, it has been expired since {exp.isoformat()}: more than {window} days have passed, so a "
                    "renewal takes effect only when CSLB accepts it, work in the gap was unlicensed (no right to sue "
                    f"for payment), and after {P(LIC_LATE, 'max_years_to_renew')} years you must apply for a new "
                    "license. Stop contracting and renew now.")
        notes.append(f"Your CSLB license expiration date ({exp.isoformat()}) has passed: see the 'CSLB license "
                     "EXPIRED' item. If you already renewed, enter the new date.")
        items.append(_ob(f"CSLB license EXPIRED {exp.isoformat()}: renew now", "license", lic_rules, retro,
                         **{**lic_kw, "notes": [f"The expiration date you entered ({exp.isoformat()}) has passed. If "
                                                "you already renewed, enter the new expiration date (check CSLB "
                                                "license detail). " + what] + lic_kw["notes"],
                            "math": lic_kw["math"] + [f"Retroactive renewal window: {exp.isoformat()} + {window} days "
                                                      f"= {retro.isoformat()}"]}))
    elif exp.year == year:
        items.append(_ob(f"CSLB license renewal (expires {exp.isoformat()})", "license", lic_rules, exp, **lic_kw))
    else:
        notes.append(f"Your CSLB license expires {exp.isoformat()}, after {year}; no renewal is due in {year}."
                     if exp.year > year else
                     f"Your CSLB license expires {exp.isoformat()}, before {year}: renew it by then (it is on the "
                     f"{exp.year} calendar).")

    # bonds
    bond_amt = P(BOND, "amount_usd")
    lead = P(BOND_CANCEL, "recommended_lead_time_new_bond_weeks")
    bond_rules = [BOND, QUAL_BOND, BOND_90, BOND_CANCEL]
    lines = [f"Contractor's bond: {_usd(bond_amt)}",
             f"Qualifier bond: {_usd(P(QUAL_BOND, 'amount_usd'))} more for each qualifier who is: "
             + "; ".join(P(QUAL_BOND, "required_when_qualifier_is"))]
    if is_llc:
        bond_rules.append(LLC_BOND)
        lines.append(f"LLC worker bond: {_usd(P(LLC_BOND, 'amount_usd'))} (wages and fringe benefits), on top of the "
                     f"{_usd(bond_amt)} bond")
    bond_kw = dict(form="Surety bond(s) on file with CSLB", agency="CSLB (filed by your surety)",
                   who_files="Your surety / bond agent; you confirm it reached CSLB",
                   penalty_if_late=(f"If the surety cancels, your license is suspended {P(BOND_CANCEL, 'days')} days "
                                    "after CSLB gets the notice unless a replacement reaches CSLB; work while "
                                    "suspended is unlicensed."), math=lines,
                   notes=[f"CSLB accepts a bond back to its effective date only if it arrives within "
                          f"{P(BOND_90, 'days')} days of that date.",
                          "Bond and license renewal dates usually differ; match the new bond's effective date to the "
                          "old one's cancellation date."])
    if bond_date:
        items.append(_ob("Bond renewal / premium due", "insurance_bond", bond_rules, bond_date,
                         remind_on=bond_date - timedelta(weeks=lead),
                         **{**bond_kw, "notes": bond_kw["notes"] + _date_notes(bond_date, year, today, "bond")}))
    else:
        items.append(_ob("Keep contractor bond(s) in force", "insurance_bond", bond_rules, status="needs_date",
                         trigger="Enter bond_renewal_date from your bond or premium invoice", **bond_kw))

    # LLC liability insurance
    if is_llc:
        base, step = P(LLC_INS, "base_aggregate_limit_dollars"), P(LLC_INS, "additional_per_person_dollars")
        free, cap = P(LLC_INS, "base_applies_max_personnel"), P(LLC_INS, "max_required_dollars")
        if personnel is not None:
            need = min(cap, base + step * max(0, personnel - free))
            ins_math = [f"Required aggregate: min({_usd(cap)}, {_usd(base)} + {_usd(step)} x max(0, {personnel} - "
                        f"{free})) = {_usd(need)}"]
        else:
            ins_math = [f"Required aggregate: {_usd(base)} for up to {free} personnel of record, plus {_usd(step)} for "
                        f"each additional person, capped at {_usd(cap)} (give personnel_of_record_count)"]
        ins_kw = dict(form="Certificate of Liability Insurance", agency="CSLB",
                      who_files="Your liability insurer's authorized agent signs and files it",
                      penalty_if_late="Coverage must stay continuous. If claims use up the aggregate, the full limit "
                                      "must be back by the start of the next policy period or the license is "
                                      "suspended by law.", math=ins_math,
                      notes=[f"Policy period may not exceed {P(LLC_INS, 'designated_period_max_months')} months; "
                             "claims-made or occurrence.",
                             f"Over {free} personnel this follows CSLB's reading ({_usd(step)} for each additional "
                             f"person). B&P 7071.19(b)(2) literally says {_usd(step)} for each person listed, which "
                             "would require more; have your insurance broker or an attorney confirm the limit."])
        if liab_date:
            items.append(_ob("LLC liability insurance renewal (certificate to CSLB)", "insurance_bond",
                             [LLC_INS, LLC_REINSTATE], liab_date,
                             **{**ins_kw, "notes": ins_kw["notes"] + _date_notes(liab_date, year, today, "policy")}))
        else:
            items.append(_ob("LLC liability insurance (certificate on file with CSLB)", "insurance_bond",
                             [LLC_INS, LLC_REINSTATE], status="needs_date",
                             trigger="Enter liability_policy_renewal_date from your policy", **ins_kw))

    # workers' comp
    wc_required = has_employees or bool(mandatory) or (year >= Date.fromisoformat(P(WC_2028, "operative_date")).year
                                                       and entity != "joint_venture")
    if wc_required:
        min_cslb = P(WC_CSLB_PEN, "min_civil_penalty_sole_owner_per_violation" if sole
                     else "min_civil_penalty_entity_per_violation")
        per_emp = P(WC_LC3722, "per_employee_basic_dollars")
        wc_math = [f"CSLB minimum civil penalty ({'sole owner' if sole else 'entity'}): {_usd(min_cslb)} per violation"
                   + (" (B&P 7125.4 names partnerships, corporations, LLCs and tribal businesses, not joint "
                      "ventures; the entity amount is shown)" if entity == "joint_venture" else "")]
        if n_emp:
            wc_math.append(f"Labor Code 3722 assessment: greater of 2 x the premium you skipped or {_usd(per_emp)} x "
                           f"{_n(n_emp, 'employee')} = at least {_usd(per_emp * n_emp)}")
        why = ("you have employees" if has_employees else f"class {', '.join(mandatory)} must carry it even without "
               "employees" if mandatory else "the no-employee exemption ends")
        wc_kw = dict(form="Certificate of Workers' Compensation Insurance", agency="CSLB (filed by your insurer)",
                     who_files="Your workers' comp insurer files the certificate; you confirm it on the CSLB record",
                     penalty_if_late=("License automatically suspended on the lapse date; the Labor Commissioner "
                                      "can issue a stop order on all employee labor; uninsured-employer penalties."),
                     math=wc_math,
                     notes=[f"Required because {why}.",
                            f"A certificate reaching CSLB within {P(WC_CERT_90, 'days')} days of its effective date "
                            "is accepted back to that date.",
                            "When the carrier or policy changes, replace the workers' comp Notice to Employees "
                            "poster (it must show the current carrier).",
                            "Employees may not be charged any part of the premium."])
        wc_rules = [WC_ONE, WC_LAPSE, WC_CERT_90, WC_CSLB_PEN, WC_LC3722, STOP_ORDER, WC_POSTER]
        if mandatory:
            wc_rules.append(WC_MANDATORY)
        if wc_date:
            items.append(_ob("Workers' comp policy renewal (certificate to CSLB)", "insurance_bond", wc_rules,
                             wc_date, **{**wc_kw, "notes": wc_kw["notes"] + _date_notes(wc_date, year, today, "policy")}))
        else:
            items.append(_ob("Workers' comp policy in force (certificate on file with CSLB)", "insurance_bond",
                             wc_rules, status="needs_date", trigger="Enter wc_policy_renewal_date from your policy",
                             **wc_kw))
    else:
        op = Date.fromisoformat(P(WC_2028, "operative_date"))
        ends = op.year == year + 1 and entity != "joint_venture"
        items.append(_ob(
            "Workers' comp exemption on file with CSLB (no employees)", "insurance_bond",
            [WC_RECERT, WC_FIRST_HIRE, WC_2028], op if ends else None, status="upcoming" if ends else "event_driven",
            trigger="Recertify at every license renewal. The exemption becomes invalid the day you hire anyone.",
            form="CSLB exemption certificate / renewal form", agency="CSLB", who_files="Licensee",
            penalty_if_late=(f"Hiring while exempt: coverage is required from the first day and CSLB must get proof "
                             f"within {P(WC_FIRST_HIRE, 'days_to_file_proof_after_hire')} days, or the license is "
                             "suspended and you face uninsured-employer penalties."),
            notes=[f"From {op.isoformat()} a joint venture with no employees can stay exempt by filing CSLB's "
                   "statement; every other active license needs a workers' comp policy." if entity == "joint_venture"
                   else f"From {op.isoformat()} every active license needs a workers' comp policy (only no-employee "
                   "joint ventures stay exempt). Budget for it now."]))
    return items, notes


def _posting_items(year: int, has_employees: bool, n_emp: int, start: Optional[Date]) -> list[Obligation]:
    if not has_employees:
        return []
    first = start if start and start.year == year else Date(year, 1, 1)
    items = []
    postings = P(POSTINGS, "postings")
    always, sized, if_applies = [], [], []
    for p in postings:
        who = p.get("who", "")
        rng = re.search(r"(\d+) to (\d+)", who)
        more = re.search(r"(\d+) or more", who)
        if rng or more:
            if (rng and int(rng[1]) <= n_emp <= int(rng[2])) or (more and n_emp >= int(more[1])):
                sized.append(p["posting"])
        elif who.startswith("all employers") or who.startswith("listed for all"):
            always.append(p["posting"])
        else:
            if_applies.append(f"{p['posting']} ({who})")
    regulatory = P(CALOSHA_POSTER, "penalty_max_usd_per_violation")
    items.append(_ob(
        f"Replace workplace posters with {year} versions", "posting",
        [POSTINGS, CALOSHA_POSTER, WC_POSTER, WO16_POST, PAYDAY_POST], first,
        form="State and federal workplace posters", agency="DIR / Cal/OSHA / CRD / EDD / DOL",
        who_files="Employer (office manager)",
        how="Post where employees can read them during the workday, including at each jobsite where crews report; "
            "remote sites may keep Wage Order 16 available on request",
        covers=f"All of {year}",
        penalty_if_late=(f"Cal/OSHA poster missing: Regulatory violation up to {_usd(regulatory)} "
                         f"(minimum proposed {_usd(P(CALOSHA_POSTER, 'penalty_min_proposed_usd'))}; penalty schedule "
                         f"operative {P(CALOSHA_POSTER, 'penalty_schedule_operative')}, adjusted yearly). Workers' comp "
                         "notice and payday notice missing: misdemeanors."),
        math=[f"Required for every employer: {'; '.join(always)}"]
             + ([f"Required at {n_emp} employees: {'; '.join(sized)}"] if sized else [])
             + [f"If it applies to you: {'; '.join(if_applies)}"],
        notes=["The minimum wage poster changes every January 1; check your city for a local minimum wage poster."]))
    due = _month_day(P(KYR, "first_deadline"), year, KYR)
    if year >= _date(P(KYR, "first_deadline"), KYR).year and first <= due:
        per = P(KYR, "penalty_max_usd_per_employee")
        items.append(_ob(
            "Give every employee the Workplace Know Your Rights notice", "posting", [KYR], due,
            form=f"DLSE Know Your Rights notice (template in {len(P(KYR, 'template_languages'))} languages)",
            agency="Labor Commissioner",
            who_files="Employer", how="Give it to each employee in the language you normally use with them",
            covers=f"Annual notice for {year}",
            math=[f"Exposure if missed: up to {_usd(per)} x {_n(n_emp, 'employee')} = {_usd(per * n_emp)}"],
            penalty_if_late=f"Up to {_usd(per)} per employee.",
            notes=["Secondary sources say new hires must also get it at hire; the rulebook has not confirmed that, "
                   "but handing it out with the new-hire packet is a safe practice."]))
    vote = next((p for p in postings if p["posting"].lower().startswith("time off to vote")), None)
    elections = UNVERIFIED["statewide_election_dates"]["dates"].get(str(year), [])
    if vote:
        days = _int_in(vote.get("timing", ""), r"(\d+) days", POSTINGS)
        vote_kw = dict(form="Time off to vote notice", agency="Secretary of State", who_files="Employer",
                       penalty_if_late="No penalty amount in the rulebook.")
        for e in elections:
            ed = Date.fromisoformat(e)
            due = ed - timedelta(days=days)
            if ed < first:
                continue
            items.append(_ob(f"Post the time-off-to-vote notice for the {ed.isoformat()} statewide election",
                             "posting", [POSTINGS], due, ["statewide_election_dates"],
                             math=[f"{ed.isoformat()} - {days} days = {due.isoformat()}"], **vote_kw))
        if not elections:
            items.append(_ob("Post the time-off-to-vote notice before each statewide election", "posting", [POSTINGS],
                             status="event_driven",
                             trigger=f"At least {days} days before every statewide election (this tool has no {year} "
                                     "election dates; check the Secretary of State's election calendar)", **vote_kw))
    return items


def _safety_items(year: int, has_employees: bool, n_emp: int, start: Optional[Date]) -> list[Obligation]:
    if not has_employees:
        return []
    had_staff_last_year = not (start and start.year >= year)
    items = []
    exempt_max = P(LOG_SIZE, "exempt_if_peak_employees_lte")
    regulatory = P(CALOSHA_POSTER, "penalty_max_usd_per_violation")
    if n_emp > exempt_max and had_staff_last_year:
        post = _month_day(P(LOG_POST, "post_by"), year, LOG_POST)
        end = _month_day(P(LOG_POST, "keep_posted_until"), year, LOG_POST)
        roles = P(LOG_CERT, "certifier_roles")
        items.append(_ob(
            f"Post the certified Cal/OSHA Form 300A summary for {year - 1}", "safety_records",
            [LOG_SIZE, LOG_POST, LOG_MAIL, LOG_CERT, LOG_RETAIN, CALOSHA_POSTER], post, window_end=end,
            form="Cal/OSHA Form 300A (from your Form 300 log)",
            agency=f"Cal/OSHA (keep Forms 300, 300A, 301 for {P(LOG_RETAIN, 'retention_years')} years)",
            who_files=f"Certified by a company executive: {'; '.join(roles)}",
            how="Post where employee notices go; hand or mail a copy to employees who don't report weekly to that "
                "location (field crews)", covers=f"Injuries and illnesses in {year - 1}",
            math=[f"{n_emp} employees > {exempt_max}: Log 300 records required",
                  f"Post {post.isoformat()} through {end.isoformat()}"],
            penalty_if_late=(f"Posting and recordkeeping failures are Cal/OSHA Regulatory violations, up to "
                             f"{_usd(regulatory)} each."),
            notes=["Size is the company's peak employment at any time last year. Enter zeros if there were no cases."]))
    lo, hi = P(ITA, "form_300a_construction_range")
    if n_emp >= lo and had_staff_last_year:
        due = _month_day(P(ITA, "submit_by"), year, ITA)
        items.append(_ob(
            f"Submit {year - 1} Form 300A data to OSHA's Injury Tracking Application", "safety_records", [ITA], due,
            form="OSHA ITA electronic submission (with EIN)", agency="Federal OSHA (ITA website)",
            who_files="Employer (safety manager or office manager)", covers=f"{year - 1} injuries and illnesses",
            math=[f"{n_emp} employees is within {lo}-{hi} (construction) or {hi + 1}+: submission required"],
            penalty_if_late=f"Recordkeeping/reporting failures are Regulatory violations, up to {_usd(regulatory)} each.",
            notes=["Counted per establishment (peak last year, part-time and temporary workers each count); this uses "
                   "your company-wide count.",
                   f"{P(ITA, 'form_300_301_if_employees_gte')}+ employees in NAICS "
                   f"{', '.join(P(ITA, 'form_300_301_construction_naics_in_appendix_i'))} must also send Form 300 "
                   "and 301 data."]))
    if n_emp >= P(HARASS, "min_employees"):
        items.append(_ob(
            "Harassment prevention training", "training", [HARASS], status="event_driven",
            trigger=f"Every {P(HARASS, 'frequency_years')} years: {P(HARASS, 'supervisor_hours')} hours for "
                    f"supervisors, {P(HARASS, 'nonsupervisor_hours')} hour for everyone else; new hires and new "
                    "supervisors soon after they start",
            form="Training records (CRD offers a free online course)", agency="Civil Rights Department",
            who_files="Employer", penalty_if_late="No penalty amount in the rulebook."))
    small = P(WVPP_EXEMPT, "small_site_exempt_if_employees_at_any_time_lt")
    items.append(_ob(
        "Workplace Violence Prevention Plan: annual training and plan review", "training",
        [WVPP_PLAN, WVPP_TRAIN, WVPP_REVIEW, WVPP_EXEMPT, WVPP_RECORDS], status="event_driven",
        trigger=f"At least every {P(WVPP_TRAIN, 'training_interval_months_max')} months train every employee and "
                f"review the plan (also after any workplace violence incident, when a deficiency shows up, and when "
                "the plan changes or a new hazard is found)",
        form="Written WVPP (stand-alone or a section of your IIPP), training records, violent incident log",
        agency="Cal/OSHA", who_files="The person(s) your plan names as responsible (often the safety manager)",
        penalty_if_late="Cal/OSHA citation; penalty amounts for WVPP violations are not in the rulebook.",
        notes=[f"Exempt only at a place with fewer than {small} employees at any time that is not accessible to the "
               "public and meets the IIPP rule (8 CCR 3203); most jobsites do not qualify.",
               f"Keep training records {P(WVPP_RECORDS, 'training_records_years_min')} year and the violent incident "
               f"log and investigations {P(WVPP_RECORDS, 'violent_incident_log_years_min')} years."]))
    return items


def _entity_items(year: int, entity: str, is_llc: bool) -> list[Obligation]:
    items = []
    if is_llc:
        day, month = (_int_in(P(FTB_LLC, "annual_tax_due"), r"(\d+)\w* day", FTB_LLC),
                      _int_in(P(FTB_LLC, "annual_tax_due"), r"(\d+)\w* month", FTB_LLC))
        due = Date(year, month, day)
        items.append(_ob(
            f"LLC {_usd(P(FTB_LLC, 'annual_tax_usd'))} annual tax for {year}", "entity", [FTB_LLC, SOS_GOOD], due,
            form=P(FTB_LLC, "annual_tax_form"), agency="Franchise Tax Board", who_files="LLC (or its CPA)",
            covers=f"Tax year {year} (calendar-year LLC)",
            math=[f"{_ord(day)} day of the {_ord(month)} month of the tax year = {due.isoformat()}"],
            penalty_if_late=(f"FTB penalties and interest. An FTB or SOS suspension leads CSLB to suspend the license "
                             f"{P(SOS_GOOD, 'suspension_days_after_notice')} days after notice, and members can be "
                             f"personally liable up to {_usd(P(SOS_GOOD, 'llc_personal_liability_per_person_usd'))} "
                             "each while suspended."),
            notes=["If the date falls on a weekend or holiday, pay by the prior business day to be safe."]))
        fday = _int_in(P(FTB_LLC, "fee_estimate_due"), r"(\d+)\w* day", FTB_LLC)
        fmonth = _int_in(P(FTB_LLC, "fee_estimate_due"), r"(\d+)\w* month", FTB_LLC)
        items.append(_ob(
            "LLC fee estimate (only if total California income is "
            f"{_usd(P(FTB_LLC, 'fee_threshold_total_ca_income_usd'))} or more)", "entity", [FTB_LLC],
            Date(year, fmonth, fday), form=P(FTB_LLC, "fee_estimate_form"), agency="Franchise Tax Board",
            who_files="LLC (or its CPA)", covers=f"Tax year {year}",
            penalty_if_late="FTB underpayment penalty; fee tier amounts are not in the rulebook (ask your CPA)."))
        items.append(_ob(
            f"LLC Statement of Information (every {P(SOI_LLC, 'frequency_years')} years)", "entity",
            [SOI_LLC, SOI_PEN, SOS_GOOD], status="needs_date",
            trigger=f"Every {P(SOI_LLC, 'frequency_years')} years in your filing period (based on the month the LLC "
                    "registered); check bizfileOnline", form=f"Statement of Information ({_usd(P(SOI_LLC, 'fee_usd'))})",
            agency="Secretary of State", who_files="LLC manager or member",
            penalty_if_late=(f"{_usd(P(SOI_PEN, 'penalty_usd'))} penalty after a "
                             f"{P(SOI_PEN, 'cure_days_after_delinquency_notice')}-day notice; SOS suspension leads to "
                             "CSLB license suspension.")))
    elif entity == "corporation":
        items.append(_ob(
            "Corporation Statement of Information (every year)", "entity", [SOI_CORP, SOI_PEN, SOS_GOOD],
            status="needs_date", trigger="Every year in your filing period (based on the month of incorporation)",
            form=f"Statement of Information ({_usd(P(SOI_CORP, 'fee_usd'))})", agency="Secretary of State",
            who_files="Corporate officer",
            penalty_if_late=(f"{_usd(P(SOI_PEN, 'penalty_usd'))} penalty after a "
                             f"{P(SOI_PEN, 'cure_days_after_delinquency_notice')}-day notice; SOS suspension leads to "
                             "CSLB license suspension.")))
    elif entity in ("sole_owner", "partnership"):
        items.append(_ob(
            "Owner estimated income tax payments (federal and California)", "entity", [EST_TAX], status="event_driven",
            trigger="Quarterly; get the dates and amounts from your tax preparer", form="IRS 1040-ES / FTB 540-ES",
            agency="IRS / FTB", who_files="Owner or partners (or their CPA)",
            penalty_if_late="Underpayment penalties; amounts not in the rulebook."))
    return items


def _public_works_items(year: int) -> list[Obligation]:
    renew = _month_day(P(PW_REG, "renew_by"), year, PW_REG)
    fee, opts = P(PW_FEE, "fee_per_fiscal_year_usd"), P(PW_FEE, "multi_year_options_usd")
    grace = P(PW_LATE, "grace_period_days")
    g0, g1 = (_month_day(x, year, PW_LATE) for x in P(PW_LATE, "grace_window").split(" to "))
    return [_ob(
        f"DIR public works contractor registration for fiscal year {year}-{str(year + 1)[-2:]}", "license",
        [PW_REG, PW_FEE, PW_LATE, PW_UNREG, PW_REQ], renew,
        form="DIR Public Works Contractor Registration (online)", agency="Department of Industrial Relations",
        who_files="Owner or office manager", how="Renew and pay online; registration counts only once payment clears",
        covers=f"July 1, {year} - June 30, {year + 1} (needed to bid, be listed in a bid or work on public works)",
        math=[f"Fee {_usd(fee)} per fiscal year; " + ", ".join(f"{k} yr {_usd(v)}" for k, v in opts.items()),
              f"Renew by {renew.isoformat()}"],
        penalty_if_late=(f"Lapsed but still working: renew within {grace} days ({g0.isoformat()} to "
                         f"{g1.isoformat()}) with a "
                         f"{_usd(P(PW_LATE, 'penalty_accidental_lapse_usd'))} penalty plus the fee "
                         f"({_usd(P(PW_LATE, 'penalty_non_accidental_lapse_usd'))} if the lapse was not accidental). "
                         f"Working unregistered: {_usd(P(PW_UNREG, 'per_day_usd'))} per day up to "
                         f"{_usd(P(PW_UNREG, 'max_usd'))}, plus the {_usd(P(PW_UNREG, 'plus_registration_penalty_usd'))}"
                         " registration penalty."),
        notes=["Only if you bid or work on public works. The fee does not change with how many days remain in the "
               "fiscal year.",
               "SB 909 (Stats. 2026, ch. 660) amends Labor Code 1725.5; its fee changes and operative date are not "
               "verified here, so check DIR's registration page before renewing."])]


def _city_items(cities: list[str], renewal_dates: dict[str, str], year: int, today: Date) -> list[Obligation]:
    """One reminder per city (cities_worked plus any city given only in renewal_dates), deduplicated ignoring case."""
    dates = {k.strip().lower(): v for k, v in (renewal_dates or {}).items() if k and k.strip()}
    named = {}
    for c in [*cities, *(renewal_dates or {})]:
        if c and c.strip():
            named.setdefault(c.strip().lower(), c.strip())
    items = []
    for key, city in named.items():
        d = _opt_date(dates.get(key), f"city_tax_renewal_dates[{city}]")
        items.append(_ob(
            f"{city} business tax certificate renewal", "local_tax", [CITY_STMT, CITY_PERMIT, LOCAL_WAGE], d,
            status="upcoming" if d else "needs_date",
            trigger=None if d else f"Enter the renewal date from your {city} business tax certificate",
            form=f"{city} business tax / business license renewal", agency=f"City of {city}",
            who_files="Owner or bookkeeper",
            how="Renew with the city; file the signed statement that your CSLB license is in force",
            penalty_if_late=("Set by the city's ordinance (not in the rulebook). The city or county may refuse your "
                             "building permits until you are current."),
            notes=["Rates and due dates vary by city; this tool does not estimate city tax.",
                   f"Check {city} for a local minimum wage above the state rate."]
                  + (_date_notes(d, year, today, "renewal") if d else [])))
    return items


# ---------------------------------------------------------------- tool 1: compliance calendar
def compliance_calendar(year: int, entity_type: Entity = "sole_owner", has_employees: bool = True,
                        employee_count: int = 0, license_expiration_date: Optional[str] = None,
                        llc: bool = False, quarterly_payroll_usd: Optional[float] = None,
                        pays_independent_contractors: bool = False, cities_worked: Optional[list[str]] = None,
                        as_of: Optional[str] = None, *, quarterly_income_tax_withheld_usd: Optional[float] = None,
                        new_employer: bool = False, started_paying_wages_on: Optional[str] = None,
                        new_hire_dates: Optional[list[str]] = None,
                        independent_contractor_600_dates: Optional[list[str]] = None,
                        license_classifications: Optional[list[str]] = None, bond_renewal_date: Optional[str] = None,
                        wc_policy_renewal_date: Optional[str] = None,
                        liability_policy_renewal_date: Optional[str] = None,
                        personnel_of_record_count: Optional[int] = None,
                        city_tax_renewal_dates: Optional[dict[str, str]] = None,
                        public_works: bool = False) -> ComplianceCalendarResult:
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    try:
        year = int(year)
    except (TypeError, ValueError):
        raise ValueError(f"year must be a 4-digit year like 2026, got {year!r}") from None
    if not 2000 <= year <= 2100:
        raise ValueError(f"year must be a 4-digit year like 2026, got {year}")
    if employee_count < 0:
        raise ValueError("employee_count cannot be negative")
    if (quarterly_payroll_usd or 0) < 0 or (quarterly_income_tax_withheld_usd or 0) < 0:
        raise ValueError("quarterly_payroll_usd and quarterly_income_tax_withheld_usd cannot be negative")
    if not has_employees and employee_count > 0:
        raise ValueError("employee_count is above 0 but has_employees is false; fix one of them")
    if entity_type not in Entity.__args__:
        raise ValueError(f"entity_type must be one of {', '.join(Entity.__args__)}")
    start = _opt_date(started_paying_wages_on, "started_paying_wages_on")
    if start and start.year > year:
        raise ValueError(f"started_paying_wages_on ({start.isoformat()}) is after {year}, so you had no employees in "
                         f"{year}: set has_employees=false for {year}, or build the {start.year} calendar")
    hires = _dates(new_hire_dates, "new_hire_dates")
    if hires and not has_employees:
        raise ValueError("new_hire_dates were given but has_employees is false; a new hire is an employee")
    ic_dates = _dates(independent_contractor_600_dates, "independent_contractor_600_dates")
    notes: list[str] = []
    if quarterly_income_tax_withheld_usd is not None and quarterly_payroll_usd is None:
        notes.append("quarterly_income_tax_withheld_usd is used only together with quarterly_payroll_usd.")
    if ic_dates and not pays_independent_contractors:
        pays_independent_contractors = True
        notes.append("independent_contractor_600_dates were given, so pays_independent_contractors was treated as "
                     "true.")
    n_emp = employee_count
    if has_employees and n_emp == 0:
        n_emp = 1
        notes.append("has_employees is true but employee_count is 0; counted as 1 employee.")
    is_llc = llc or entity_type == "llc"
    if llc and entity_type != "llc":
        notes.append(f"llc=true with entity_type={entity_type}: LLC rules were applied.")
    liab = _opt_date(liability_policy_renewal_date, "liability_policy_renewal_date")
    if liab and not is_llc:
        notes.append("liability_policy_renewal_date is used only for LLCs (CSLB requires a liability policy on file "
                     "only for LLC licensees), so it was ignored.")

    items: list[Obligation] = []
    deposit = None
    if has_employees:
        threshold = P(EDD_REG, "wage_threshold_usd_per_calendar_quarter")
        if quarterly_payroll_usd is not None and quarterly_payroll_usd <= threshold:
            notes.append(f"Quarterly payroll {_usd(quarterly_payroll_usd)} is not over {_usd(threshold)}: EDD "
                         "registration is not triggered yet, but it is the moment a quarter goes over.")
        pay, deposit = _payroll_items(year, start, quarterly_payroll_usd, n_emp, quarterly_income_tax_withheld_usd,
                                      new_employer)
        items += pay
        if quarterly_payroll_usd is None:
            notes.append("Give quarterly_payroll_usd to get your deposit schedule and the dated deposit list.")
        if start is None:
            notes.append("Assumes you are already registered with EDD and have an EIN. If you start paying wages, "
                         "give started_paying_wages_on for those deadlines.")
    else:
        if quarterly_payroll_usd:
            notes.append("quarterly_payroll_usd was ignored because has_employees is false.")
        items.append(_ob(
            "If you hire: register with EDD", "payroll_tax", [EDD_REG, EDD_REG_DAYS], status="event_driven",
            trigger=f"Within {P(EDD_REG_DAYS, 'deadline_days_after_threshold_crossed')} days after total wages in a "
                    f"calendar quarter first go over {_usd(P(EDD_REG, 'wage_threshold_usd_per_calendar_quarter'))}",
            form="e-Services for Business (or DE 1)", agency="EDD", who_files="Owner",
            penalty_if_late="EDD penalties and interest (amounts not in the rulebook).", unv=["edd_late_penalties"]))
    items += _year_end_items(year, has_employees, pays_independent_contractors, start)
    hire, hire_notes = _hire_items(year, has_employees, pays_independent_contractors, hires, ic_dates)
    items += hire
    notes += hire_notes
    lic, lic_notes = _license_items(
        year, today, entity_type, is_llc, has_employees, n_emp if has_employees else 0,
        _opt_date(license_expiration_date, "license_expiration_date"), license_classifications or [],
        _opt_date(bond_renewal_date, "bond_renewal_date"), _opt_date(wc_policy_renewal_date, "wc_policy_renewal_date"),
        liab, personnel_of_record_count)
    items += lic
    notes += lic_notes
    items += _posting_items(year, has_employees, n_emp, start)
    items += _safety_items(year, has_employees, n_emp, start)
    items += _entity_items(year, entity_type, is_llc)
    if entity_type == "corporation" and not is_llc:
        notes.append("Corporations also owe FTB's annual minimum franchise tax and estimated payments; those are not "
                     "in the verified rulebook, so ask your CPA for the dates.")
    items += _city_items(cities_worked or [], city_tax_renewal_dates or {}, year, today)
    if public_works:
        items += _public_works_items(year)

    for it in items:
        if it.due_date:
            it.days_until_due = (Date.fromisoformat(it.due_date) - today).days
            it.status = ("passed" if it.days_until_due < 0 else
                         "due_soon" if it.days_until_due <= DUE_SOON_DAYS else "upcoming")
        elif it.status != "needs_date":
            it.status = "event_driven"
    items.sort(key=lambda i: (i.due_date is None, i.due_date or "", i.title))
    upcoming = [i for i in items if i.due_date and i.status != "passed"]
    if year != RATE_YEAR:
        notes.append(f"Dollar amounts and thresholds are the verified {RATE_YEAR} values; check for {year} changes.")
    notes.append("IRS and EDD return and deposit dates are moved to the next business day when they land on a weekend "
                 "or federal holiday; other dates are not, so act by the prior business day.")
    counts = dict(Counter(it.status for it in items))
    used = sorted({k for it in items for k in it.unverified_values})
    result = ComplianceCalendarResult(
        year=year, as_of=today.isoformat(), entity_type=entity_type, is_llc=is_llc, has_employees=has_employees,
        employee_count=n_emp if has_employees else 0, items=items, next_due=upcoming[0] if upcoming else None,
        counts=counts, deposit_schedule=deposit, notes=notes,
        unverified_values_used={k: UNVERIFIED[k] for k in used})
    result.cite(*[r for it in items for r in it.rule_ids], *(deposit.rule_ids if deposit else []))
    if used or year != RATE_YEAR:
        result.verified = False
    return result


# ---------------------------------------------------------------- tool 2: labor burden
class BurdenLine(BaseModel):
    item: str
    paid_by: Literal["employer", "employee"]
    rate_percent: Optional[float] = None
    taxable_wages_usd: Optional[float] = None
    annual_usd: float
    per_hour_usd: float
    math: str
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class LaborBurdenResult(core.Result):
    tax_year: int
    hourly_wage: float
    annual_hours: float
    base_wages_usd: float
    paid_sick_leave_usd: float = 0.0
    taxable_wages_usd: float
    employer_lines: list[BurdenLine] = Field(default_factory=list)
    employee_withholding_lines: list[BurdenLine] = Field(default_factory=list)
    employer_taxes_and_insurance_usd: float
    total_annual_cost_usd: float
    burdened_hourly_rate_usd: float
    burden_percent_of_base_wages: float
    notes: list[str] = Field(default_factory=list)
    unverified_values_used: dict[str, dict] = Field(default_factory=dict)


def _carpentry_class_notes(wage: float, code: str, today: Date, policy_start: Optional[Date]) -> list[str]:
    """5403/5432 split: the threshold in force when the WC policy started (USRP changes apply at policy inception)."""
    steps = sorted((Date(int(m[1]), int(m[2]), int(m[3])), P(WC_DUAL, k)) for k in core.rule(WC_DUAL).parameters
                   if (m := re.fullmatch(r"carpentry_5403_5432_threshold_eff_(\d{4})_(\d{2})_(\d{2})", k)))
    latest_eff = steps[-1][0]
    if policy_start is None and today >= latest_eff and len(steps) > 1:
        options = [(f"a policy that started on or after {latest_eff.isoformat()}", steps[-1][1]),
                   (f"a policy that started before {latest_eff.isoformat()}", steps[-2][1])]
    else:
        d = policy_start or today
        thr = next((t for eff, t in reversed(steps) if d >= eff), None)
        if thr is None:
            return [f"Carpentry dual-wage classes: a policy starting {d.isoformat()} predates the thresholds in the "
                    f"rulebook (earliest {steps[0][0].isoformat()}); check the threshold on your policy."]
        options = [(f"a policy {'starting' if policy_start else 'in force on'} {d.isoformat()}", thr)]
    out, codes = [], set()
    for when, thr in options:
        right = "5432" if wage >= thr else "5403"
        codes.add(right)
        out.append(f"Carpentry dual-wage classes (regular hourly wage test): for {when} the threshold is {_usd(thr)}, "
                   f"and {_rate(wage)}/h is {'at or above' if right == '5432' else 'below'} it, so payroll belongs in "
                   f"{right}" + (f", not {code}; use the {right} rate." if right != code else "."))
    if len(codes) > 1:
        out.append("The answer depends on when your current policy started; give wc_policy_start_date.")
    return out


def _min_wage_on(d: Date) -> tuple[Optional[Date], Optional[float]]:
    """(effective date, hourly rate) of the California minimum wage in force on `d`, from the rulebook."""
    rates = {_date(P(MIN_WAGE, "effective"), MIN_WAGE): P(MIN_WAGE, "hourly_usd")}
    rates.update({_date(k, MIN_WAGE): v for k, v in P(MIN_WAGE, "prior_rates_usd").items()})
    return max(((eff, r) for eff, r in rates.items() if eff <= d), default=(None, None))


def labor_burden_estimate(hourly_wage: float, annual_hours: float, wc_rate_per_100: float,
                          ui_rate: Optional[float] = None, as_of: Optional[str] = None, *,
                          experience_mod: float = 1.0, wc_class_code: Optional[str] = None,
                          include_paid_sick_leave: bool = False, ett_applies: bool = True,
                          wc_policy_start_date: Optional[str] = None) -> LaborBurdenResult:
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    if hourly_wage <= 0 or annual_hours <= 0:
        raise ValueError("hourly_wage and annual_hours must be greater than 0")
    if annual_hours > 366 * 24:
        raise ValueError("annual_hours is more hours than a year has")
    if wc_rate_per_100 < 0 or experience_mod <= 0:
        raise ValueError("wc_rate_per_100 cannot be negative and experience_mod must be above 0")
    if experience_mod > MAX_EXPERIENCE_MOD:
        raise ValueError(f"experience_mod {experience_mod:g} looks like a percent; pass the factor, e.g. 0.92 for a "
                         "92% mod")
    if ui_rate is not None and ui_rate < 0:
        raise ValueError("ui_rate cannot be negative")
    policy_start = _opt_date(wc_policy_start_date, "wc_policy_start_date")
    year = today.year
    notes: list[str] = []
    rules_used: list[str] = []
    unv_used: list[str] = []

    def line(item, paid_by, rules, annual, math, rate=None, taxable=None, unv=()) -> BurdenLine:
        rules_used.extend(rules)
        unv_used.extend(unv)
        return BurdenLine(item=item, paid_by=paid_by, rate_percent=rate, taxable_wages_usd=taxable,
                          annual_usd=annual, per_hour_usd=_money(annual / annual_hours), math=math, rule_ids=rules,
                          verified=all(core.rule(r).verified for r in rules) and not unv)

    base = _money(hourly_wage * annual_hours)
    lines: list[BurdenLine] = []
    sick = 0.0
    if include_paid_sick_leave:
        per = _int_in(P(SICK_ACCRUAL, "accrual_rate"), r"per (\d+) hours", SICK_ACCRUAL)
        cap = P(SICK_CAPS, "use_cap_hours_per_year")
        hours = min(annual_hours / per, cap)
        sick = _money(hours * hourly_wage)
        lines.append(line("Paid sick leave (if fully used)", "employer", [SICK_USE, SICK_ACCRUAL, SICK_CAPS], sick,
                          f"min({annual_hours:g} h / {per}, {cap} h use cap) = {round(hours, 2):g} h x "
                          f"{_rate(hourly_wage)} = {_usd(sick)}"))
        notes.append(f"Sick leave assumes the worker uses the full {cap} hours (or "
                     f"{P(SICK_CAPS, 'use_cap_days_per_year')} days, if shifts are longer) the law lets you cap use "
                     "at; sick pay is taxed like wages.")
    wages = _money(base + sick)

    ss, ss_base, med = _fica_rates()
    ss_wages = min(wages, ss_base)
    lines.append(line(f"Social Security (employer {_pct(ss)})", "employer", [FICA],
                      _money(ss_wages * ss / 100),
                      f"{_pct(ss)} x {_usd(ss_wages)}" + (f" (capped at the {_usd(ss_base)} wage base)"
                                                           if wages > ss_base else ""), ss, ss_wages))
    lines.append(line(f"Medicare (employer {_pct(med)})", "employer", [FICA], _money(wages * med / 100),
                      f"{_pct(med)} x {_usd(wages)} (no wage cap)", med, wages))
    futa_rate, red, futa_rules, futa_notes = _futa_rate(year)
    futa_base = P(FUTA, "taxable_wage_limit_per_employee")
    fw = min(wages, futa_base)
    lines.append(line("FUTA (federal unemployment, incl. California credit reduction)", "employer", futa_rules,
                      _money(fw * futa_rate / 100),
                      f"({_pct(P(FUTA, 'net_rate_without_reduction_percent'))} + {_pct(red)} CA credit reduction) = "
                      f"{_pct(round(futa_rate, 4))} x {_usd(fw)} (first {_usd(futa_base)})", round(futa_rate, 4), fw))
    notes += futa_notes
    ui_base = P(UI, "taxable_wage_limit_per_employee")
    uw = min(wages, ui_base)
    ui_unv: list[str] = []
    if ui_rate is None:
        ui_pct = P(UI, "new_employer_rate_percent")
        notes.append(f"CA UI uses the new-employer rate {_pct(ui_pct)} (first {P(UI, 'new_employer_rate_duration_years')}"
                     " years). After that use the rate on your EDD DE 2088 notice.")
    else:
        ui_pct = ui_rate
        rng = UNVERIFIED["ca_ui_rate_range"]
        lo, hi = rng["min_percent"], rng["max_percent"]
        if 0 < ui_rate < 1 and lo <= ui_rate * 100 <= hi:      # 0.034 is a decimal for 3.4%
            ui_pct = round(ui_rate * 100, 4)
            ui_unv.append("ca_ui_rate_range")
            notes.append(f"ui_rate {ui_rate:g} was read as a decimal, so {ui_pct:g}% was used: California UI rates "
                         f"run {lo}% to {hi}% (Schedule F+), never under 1%. Pass the percent (e.g. 3.4) to avoid this.")
        elif not lo <= ui_pct <= hi:
            ui_unv.append("ca_ui_rate_range")
            notes.append(f"A {ui_pct:g}% UI rate is outside California's {lo}%-{hi}% range; it was used as given, but "
                         "check the rate on your EDD DE 2088 notice.")
    lines.append(line("CA Unemployment Insurance (UI)", "employer", [UI], _money(uw * ui_pct / 100),
                      f"{_pct(ui_pct)} x {_usd(uw)} (first {_usd(ui_base)})", ui_pct, uw, ui_unv))
    ett_base = P(ETT, "taxable_wage_limit_per_employee")
    ew = min(wages, ett_base)
    ett_pct = P(ETT, "rate_percent") if ett_applies else 0.0
    lines.append(line("CA Employment Training Tax (ETT)", "employer", [ETT], _money(ew * ett_pct / 100),
                      f"{_pct(ett_pct)} x {_usd(ew)} (first {_usd(ett_base)})"
                      + ("" if ett_applies else " - no ETT (negative-reserve employer)"), ett_pct, ew))
    wc = _money(wages / 100 * wc_rate_per_100 * experience_mod)
    lines.append(line("Workers' compensation premium", "employer", [WC_EST, WC_ONE], wc,
                      f"{_usd(wages)} / 100 x {_rate(wc_rate_per_100)} per $100 x {experience_mod:g} experience mod = "
                      f"{_usd(wc)}", taxable=wages))
    notes.append(f"Workers' comp rates are set by your insurer by class code; the state average advisory pure premium "
                 f"for policies from Sept 1, 2026 is {_usd(P(WC_EST, 'statewide_average_advisory_pure_premium_per_100_payroll_2026_09_01'))} "
                 "per $100, and construction classes run much higher. Policy fees and surcharges are not included.")

    if wc_class_code:
        code = re.sub(r"\D", "", wc_class_code)
        rules_used.append(WC_DUAL)
        if code in ("5403", "5432"):
            notes += _carpentry_class_notes(hourly_wage, code, today, policy_start)
        else:
            notes.append(f"Class {wc_class_code}: if it is one of the construction dual-wage classes, check its "
                         "current wage threshold (thresholds changed Sept 1, 2026).")
    elif policy_start:
        notes.append("wc_policy_start_date is used only with wc_class_code 5403 or 5432 (carpentry dual-wage check).")

    sdi_pct = P(SDI, "employee_withholding_rate_percent")
    employee = [
        line("CA SDI (employee-paid, you withhold it)", "employee", [SDI], _money(wages * sdi_pct / 100),
             f"{_pct(sdi_pct)} x {_usd(wages)} (no wage cap)", sdi_pct, wages),
        line(f"Employee Social Security ({_pct(ss)})", "employee", [FICA], _money(ss_wages * ss / 100),
             f"{_pct(ss)} x {_usd(ss_wages)}", ss, ss_wages),
        line(f"Employee Medicare ({_pct(med)})", "employee", [FICA], _money(wages * med / 100),
             f"{_pct(med)} x {_usd(wages)}", med, wages),
    ]
    amt = UNVERIFIED["additional_medicare_tax"]
    if wages > amt["wage_threshold_usd"]:
        over = _money(wages - amt["wage_threshold_usd"])
        employee.append(line(f"Additional Medicare Tax ({_pct(amt['rate_percent'])}, employee only)", "employee",
                             [FICA], _money(over * amt["rate_percent"] / 100),
                             f"{_pct(amt['rate_percent'])} x ({_usd(wages)} - {_usd(amt['wage_threshold_usd'])}) = "
                             f"{_pct(amt['rate_percent'])} x {_usd(over)}", amt["rate_percent"], over,
                             ["additional_medicare_tax"]))
    notes.append("SDI and the employee halves of Social Security and Medicare come out of the worker's pay; they are "
                 "not part of your labor burden. Income tax withholding is not shown.")
    mw_eff, mw = _min_wage_on(today)
    rules_used.append(MIN_WAGE)
    if mw is None:
        notes.append(f"No California minimum wage for {today.isoformat()} is in the rulebook; check the wage yourself.")
    elif hourly_wage < mw:
        notes.append(f"{_rate(hourly_wage)}/h is below the California minimum wage of {_usd(mw)} (in force from "
                     f"{mw_eff.isoformat()}); local minimums can be higher.")
        rules_used.append(LOCAL_WAGE)
    notes.append("Not included: health/welfare, vacation, union fringes, liability insurance, overtime premium. On "
                 "public works use the DIR prevailing wage determination for wage and fringes.")
    if year != RATE_YEAR:
        notes.append(f"Rates are the verified {RATE_YEAR} values; {year} rates"
                     + (f" (including the {year} minimum wage)" if mw_eff is None or mw_eff.year < year else "")
                     + " are not in the rulebook.")

    taxes = _money(sum(x.annual_usd for x in lines) - sick)
    total = _money(wages + taxes)
    result = LaborBurdenResult(
        tax_year=year, hourly_wage=hourly_wage, annual_hours=annual_hours, base_wages_usd=base,
        paid_sick_leave_usd=sick, taxable_wages_usd=wages, employer_lines=lines, employee_withholding_lines=employee,
        employer_taxes_and_insurance_usd=taxes, total_annual_cost_usd=total,
        burdened_hourly_rate_usd=_money(total / annual_hours),
        burden_percent_of_base_wages=_money((total - base) / base * 100), notes=notes)
    result.unverified_values_used = {k: UNVERIFIED[k] for k in sorted(set(unv_used))}
    result.cite(*rules_used)
    if year != RATE_YEAR or unv_used:
        result.verified = False
    return result


# ---------------------------------------------------------------- MCP registration
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="compliance_calendar", annotations=ro)
    def _compliance_calendar(
        year: Annotated[int, Field(ge=2000, le=2100, description="Calendar year to build, e.g. 2026")],
        entity_type: Annotated[Entity, Field(description="How the license is held: sole_owner, partnership, "
                                                         "corporation, llc or joint_venture")] = "sole_owner",
        has_employees: Annotated[bool, Field(description="True if you pay any W-2 employees (even one part-time)")] = True,
        employee_count: Annotated[int, Field(ge=0, description=(
            "Most employees on payroll at any one time last year (drives Log 300A, OSHA ITA, poster and training "
            "rules and penalty exposure)"))] = 0,
        license_expiration_date: Annotated[Optional[str], Field(description=(
            "CSLB license expiration date, YYYY-MM-DD (pocket card or CSLB license detail). A past date is flagged "
            "as an expired license"))] = None,
        llc: Annotated[bool, Field(description="True if the licensee is an LLC (same as entity_type='llc')")] = False,
        quarterly_payroll_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Typical gross wages per quarter, all employees (for the deposit schedule and FUTA deposits)"))] = None,
        pays_independent_contractors: Annotated[bool, Field(description=(
            "True if you pay unincorporated subs/individuals (1099-NEC and EDD DE 542)"))] = False,
        cities_worked: Annotated[Optional[list[str]], Field(description=(
            "Cities where you hold or need a business tax certificate, e.g. ['San Diego', 'Chula Vista']"))] = None,
        as_of: Annotated[Optional[str], Field(description="YYYY-MM-DD date to measure days left from; default today in California")] = None,
        quarterly_income_tax_withheld_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Federal income tax withheld per quarter, if known (makes the deposit schedule exact)"))] = None,
        new_employer: Annotated[bool, Field(description=(
            "True if you had no payroll in the IRS lookback period (July 1 two years ago to June 30 last year). Not "
            "needed when started_paying_wages_on is given"))] = False,
        started_paying_wages_on: Annotated[Optional[str], Field(description=(
            "YYYY-MM-DD you first paid wages, if recent (adds EDD registration and EIN deadlines and shortens the "
            "deposit lookback). Must not be after `year`"))] = None,
        new_hire_dates: Annotated[Optional[list[str]], Field(description=(
            "YYYY-MM-DD first-work dates of new hires (adds dated DE 34 and I-9 items); requires has_employees"))] = None,
        independent_contractor_600_dates: Annotated[Optional[list[str]], Field(description=(
            "YYYY-MM-DD dates you first paid or contracted $600+ with a sole-proprietor sub (adds dated DE 542 "
            "items)"))] = None,
        license_classifications: Annotated[Optional[list[str]], Field(description=(
            "CSLB classes held, e.g. ['B', 'C-39', 'C-10 Electrical']. C-8, C-20, C-22, C-39 and D-49 must carry "
            "workers' comp; C-10 adds a renewal surcharge"))] = None,
        bond_renewal_date: Annotated[Optional[str], Field(description="YYYY-MM-DD bond renewal / premium date")] = None,
        wc_policy_renewal_date: Annotated[Optional[str], Field(description="YYYY-MM-DD workers' comp policy expiration date")] = None,
        liability_policy_renewal_date: Annotated[Optional[str], Field(description=(
            "YYYY-MM-DD liability policy expiration (used for LLCs only)"))] = None,
        personnel_of_record_count: Annotated[Optional[int], Field(ge=0, description="LLC personnel of record on the CSLB license")] = None,
        city_tax_renewal_dates: Annotated[Optional[dict[str, str]], Field(description=(
            "Renewal date per city from your certificates, e.g. {'San Diego': '2026-12-31'}; a city named here is "
            "added even if it is not in cities_worked"))] = None,
        public_works: Annotated[bool, Field(description=(
            "True if you bid or work on public works (adds the DIR contractor registration renewal, due June 30)"))] = False,
    ) -> ComplianceCalendarResult:
        """Build a California contractor's compliance calendar for one year: every dated filing, payment, renewal,
        posting and training obligation, so the office knows what to do and by when.

        Returns `items` sorted by due_date, each with: title, due_date (YYYY-MM-DD; null for event-driven items or
        when you have not given the date, see `trigger`), status (passed / due_soon within 30 days / upcoming /
        event_driven / needs_date), days_until_due, who_files, form, agency, how, penalty_if_late, math, notes,
        rule_ids and verified. Covers: EDD registration and EIN (if started_paying_wages_on), EDD DE 9/DE 9C and IRS
        Form 941 each quarter (moved past weekends and federal holidays), monthly or semiweekly payroll tax
        deposits, FUTA deposits (California credit reduction added to Q4), W-2/W-3, 1099-NEC, Form 940, DE 34 and
        I-9 per new hire, DE 542 per sub, CSLB license renewal with fees (an already-past expiration date is flagged
        EXPIRED with the 90-day retroactive window), contractor/qualifier/LLC bonds, LLC liability insurance,
        workers' comp certificate or exemption, posters, Know Your Rights notice, time-off-to-vote notices, Cal/OSHA
        Form 300A posting and OSHA ITA submission, harassment training, Workplace Violence Prevention Plan,
        FTB/SOS entity filings, DIR public works registration (if public_works) and a business tax reminder per
        city (no city rates). Also returns next_due (first item not yet passed), counts by status,
        deposit_schedule (monthly / semiweekly / pay_with_quarterly_return with its math), notes and
        unverified_values_used. verified is true only if every rule used is verified. Dates you enter outside the
        year stay on the list with a note. Contradictory inputs (employees with has_employees=false, wages starting
        after the year) return an error that says what to fix."""
        return compliance_calendar(year, entity_type, has_employees, employee_count, license_expiration_date, llc,
                                   quarterly_payroll_usd, pays_independent_contractors, cities_worked, as_of,
                                   quarterly_income_tax_withheld_usd=quarterly_income_tax_withheld_usd,
                                   new_employer=new_employer, started_paying_wages_on=started_paying_wages_on,
                                   new_hire_dates=new_hire_dates,
                                   independent_contractor_600_dates=independent_contractor_600_dates,
                                   license_classifications=license_classifications,
                                   bond_renewal_date=bond_renewal_date, wc_policy_renewal_date=wc_policy_renewal_date,
                                   liability_policy_renewal_date=liability_policy_renewal_date,
                                   personnel_of_record_count=personnel_of_record_count,
                                   city_tax_renewal_dates=city_tax_renewal_dates, public_works=public_works)

    @mcp.tool(name="labor_burden_estimate", annotations=ro)
    def _labor_burden_estimate(
        hourly_wage: Annotated[float, Field(gt=0, description="Straight-time hourly wage, e.g. 35")],
        annual_hours: Annotated[float, Field(gt=0, le=366 * 24, description="Hours worked per year, e.g. 2000")],
        wc_rate_per_100: Annotated[float, Field(ge=0, description=(
            "Your workers' comp rate per $100 of payroll for this worker's class code (from your policy)"))],
        ui_rate: Annotated[Optional[float], Field(ge=0, description=(
            "Your CA UI rate in percent from EDD notice DE 2088, e.g. 2.9 (a decimal like 0.029 is also read as "
            "2.9%). Leave empty for the new-employer rate"))] = None,
        as_of: Annotated[Optional[str], Field(description="YYYY-MM-DD; its year picks the tax year (default today)")] = None,
        experience_mod: Annotated[float, Field(gt=0, le=MAX_EXPERIENCE_MOD, description=(
            "Workers' comp experience modification as a factor, e.g. 0.92 (not 92)"))] = 1.0,
        wc_class_code: Annotated[Optional[str], Field(description="WC class code, e.g. '5403' (checks the carpentry dual-wage threshold)")] = None,
        include_paid_sick_leave: Annotated[bool, Field(description="Add the cost of the paid sick leave the worker can use")] = False,
        ett_applies: Annotated[bool, Field(description="False if EDD shows ETT 0.0% (negative-reserve employer)")] = True,
        wc_policy_start_date: Annotated[Optional[str], Field(description=(
            "YYYY-MM-DD start (inception or renewal) date of your current workers' comp policy; with wc_class_code "
            "5403/5432 it picks the dual-wage threshold in force for that policy"))] = None,
    ) -> LaborBurdenResult:
        """Estimate what one California worker costs the employer for a year beyond the wage (for bids and
        billing rates).

        Returns employer_lines, each with rate_percent, taxable_wages_usd, annual_usd, per_hour_usd and its math:
        employer Social Security (to the wage base) and Medicare, FUTA with California's credit reduction, CA UI
        (your DE 2088 rate, or the new-employer rate if ui_rate is empty) and ETT on the first $7,000, workers' comp
        (payroll / 100 x rate x experience mod) and optional paid sick leave. employee_withholding_lines (SDI,
        employee Social Security and Medicare, Additional Medicare Tax over $200,000) are shown separately and are
        not part of the burden. Also returns base_wages_usd, employer_taxes_and_insurance_usd,
        total_annual_cost_usd, burdened_hourly_rate_usd, burden_percent_of_base_wages, notes (carpentry 5403/5432
        dual-wage class check, minimum wage check, rate caveats), unverified_values_used and citations. Uses the
        rulebook's 2026 rates; the as_of year picks the FUTA credit reduction. Errors on impossible input (hours
        over a year, experience_mod given as a percent)."""
        return labor_burden_estimate(hourly_wage, annual_hours, wc_rate_per_100, ui_rate, as_of,
                                     experience_mod=experience_mod, wc_class_code=wc_class_code,
                                     include_paid_sick_leave=include_paid_sick_leave, ett_applies=ett_applies,
                                     wc_policy_start_date=wc_policy_start_date)
