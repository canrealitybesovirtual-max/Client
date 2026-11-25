"""Company compliance calendar and labor-burden estimator for California contractors.

  compliance_calendar    the year's dated obligations: EDD/IRS payroll returns and deposits, new-hire and contractor
                         reports, W-2/1099/940 year-end filings, CSLB renewal, bonds, LLC insurance, workers' comp,
                         workplace postings, Cal/OSHA Log 300A, city business tax reminders. Each item has the date,
                         who files, form, penalty for being late and its citations.
  labor_burden_estimate  employer cost of one worker for a year: employer FICA, FUTA with California's credit
                         reduction, CA UI and ETT, workers' comp premium, optional paid sick leave; SDI shown as an
                         employee withholding. Every line shows its math.

Every legal number comes from the rulebook (core.rule(id).param(key)) when the function runs. Values the rulebook does
not hold live in UNVERIFIED with a source URL, and any item or result that uses one has verified=False.
"""
import calendar
import re
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
        "note": "5 U.S.C. 6103 holidays (Saturday holidays observed Friday, Sunday holidays Monday). Used only to move "
                "an IRS/EDD due date that lands on one to the next business day. DC Emancipation Day and "
                "California-only state holidays are not applied.",
    },
    "futa_deposit_threshold": {
        "usd": 500,
        "source": "https://www.irs.gov/instructions/i940",
        "note": "Deposit FUTA tax for a quarter once the undeposited liability is more than $500, by the last day of "
                "the month after the quarter; $500 or less carries to the next quarter (and can be paid with Form "
                "940 after Q4).",
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
                "own schedule, which depends on your federal deposit schedule and how much PIT you withheld. Check "
                "EDD's deposit chart; it is not in the verified rulebook.",
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


def P(rule_id: str, key: str, default=KeyError):
    return core.rule(rule_id).param(key, default)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def _pct(x: float) -> str:
    return f"{x:g}%"


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


# ---------------------------------------------------------------- business-day math (IRS / EDD weekend roll)
def _nth(year: int, month: int, weekday: int, n: int) -> Date:
    if n > 0:
        d = Date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
    d = Date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _holidays(year: int) -> set[Date]:
    fixed = [Date(year, m, d) for m, d in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25))]
    observed = {d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d for d in fixed}
    floating = {_nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _nth(year, 5, 0, -1), _nth(year, 9, 0, 1),
                _nth(year, 10, 0, 2), _nth(year, 11, 3, 4)}
    return observed | floating


def _roll(d: Date) -> tuple[Date, list[str]]:
    """Next business day if `d` is a weekend or federal holiday (26 CFR 301.7503-1 / EDD next-business-day rule)."""
    hol = _holidays(d.year) | _holidays(d.year + 1)
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


def payroll_deposit_schedule(quarterly_payroll_usd: float, quarterly_income_tax_withheld_usd: Optional[float] = None,
                             new_employer: bool = False) -> DepositSchedule:
    """Federal deposit schedule basis (monthly vs semiweekly vs pay with the return) from a steady quarterly payroll."""
    if quarterly_payroll_usd < 0 or (quarterly_income_tax_withheld_usd or 0) < 0:
        raise ValueError("quarterly payroll and income tax withheld cannot be negative")
    ss, _, med = _fica_rates()
    combined = round(2 * (ss + med), 6)
    fica = _money(quarterly_payroll_usd * combined / 100)
    fit = quarterly_income_tax_withheld_usd
    tax = _money(fica + (fit or 0))
    lookback_limit, de_min = P(DEP, "lookback_threshold_usd"), P(DEP_MIN, "threshold_usd")
    lookback = 0.0 if new_employer else _money(4 * tax)
    math = [f"Social Security + Medicare, employer and employee halves: {_usd(quarterly_payroll_usd)} x "
            f"2 x ({_pct(ss)} + {_pct(med)}) = {_usd(quarterly_payroll_usd)} x {_pct(combined)} = {_usd(fica)}"]
    if fit is not None:
        math.append(f"Plus federal income tax withheld {_usd(fit)} = {_usd(tax)} per quarter")
    notes = []
    if new_employer:
        math.append("New employer: no lookback history, so the lookback total is $0")
    else:
        math.append(f"Lookback (12 months ending the prior June 30), assuming a steady payroll: 4 x {_usd(tax)} = "
                    f"{_usd(lookback)}")
    if tax < de_min:
        schedule = "pay_with_quarterly_return"
        math.append(f"{_usd(tax)} is under {_usd(de_min)} for the quarter, so the tax may be paid with a timely "
                    "Form 941 instead of deposits")
        notes.append(f"Only while the quarter (or the prior quarter) stays under {_usd(de_min)}; otherwise you deposit "
                     "on the monthly schedule.")
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
    notes.append("The IRS mails your deposit schedule each year; that notice controls over this estimate.")
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
        reg_due = start + timedelta(days=P(EDD_REG_DAYS, "deadline_days_after_threshold_crossed"))
        items.append(_ob(
            "Register as an employer with EDD (payroll tax account)", "payroll_tax", [EDD_REG, EDD_REG_DAYS, TAX_DEBT],
            reg_due, ["edd_late_penalties"], form="e-Services for Business (or DE 1)", agency="EDD",
            who_files="Owner or bookkeeper", how="Online through EDD e-Services for Business",
            covers=f"Wages first paid {start.isoformat()}",
            trigger=f"Paying more than {_usd(P(EDD_REG, 'wage_threshold_usd_per_calendar_quarter'))} in total wages "
                    "in a calendar quarter",
            math=[f"{start.isoformat()} + {P(EDD_REG_DAYS, 'deadline_days_after_threshold_crossed')} days = "
                  f"{reg_due.isoformat()} (assumes your first payroll put the quarter over "
                  f"{_usd(P(EDD_REG, 'wage_threshold_usd_per_calendar_quarter'))})"],
            penalty_if_late=edd_late,
            notes=[f"The {P(EDD_REG_DAYS, 'deadline_days_after_threshold_crossed')} days run from the date the "
                   "quarter's total wages first pass "
                   f"{_usd(P(EDD_REG, 'wage_threshold_usd_per_calendar_quarter'))}, not from each employee's pay.",
                   "Not an IRS/EDD return date, so it is not moved for weekends; act by the prior business day."]))
        ein_due = start + timedelta(days=P(EIN, "days_after_first_wage_payment"))
        items.append(_ob(
            "Get a federal EIN (if you don't have one)", "payroll_tax", [EIN], ein_due, form="IRS Form SS-4",
            agency="IRS", who_files="Owner", how="IRS online EIN application or Form SS-4",
            math=[f"{start.isoformat()} + {P(EIN, 'days_after_first_wage_payment')} days = {ein_due.isoformat()}"],
            penalty_if_late="Without an EIN you cannot make deposits or file Form 941/W-2s on time.",
            covers="Needed for deposits, Form 941 and W-2s"))

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
            unv + ["edd_late_penalties"], form="DE 9 (Quarterly Contribution Return) + DE 9C (wage detail)",
            agency="EDD", who_files="Employer or its payroll service", how="Electronically (e-Services for Business)",
            covers=f"Wages paid Q{q} {qy}", math=[f"Last day of the month after Q{q}: {raw.isoformat()}"] + moved,
            penalty_if_late=f"{edd_late} {paper}",
            notes=[f"EDD lists it as due on the 1st of the month after the quarter and delinquent if not filed by "
                   f"{due.isoformat()}.", "File every quarter once registered, even with no payroll."]))
        ext = _int_in(P(F941, "extended_due_if_timely_deposits"), r"(\d+)\w* day", F941)
        items.append(_ob(
            f"IRS Form 941 for Q{q} {qy}", "payroll_tax", [F941], due, unv + ["irs_failure_to_file"],
            form="Form 941", agency="IRS", who_files="Employer or its payroll service",
            how="E-file or mail", covers=f"Federal income tax withheld, Social Security and Medicare, Q{q} {qy}",
            math=[f"Last day of the month after Q{q}: {raw.isoformat()}"] + moved,
            penalty_if_late=UNVERIFIED["irs_failure_to_file"]["note"],
            notes=[f"If every deposit for the quarter was on time and in full you have until the {ext}th day of the "
                   "second month after the quarter."]))

    deposit = None
    if q_payroll is not None:
        deposit = payroll_deposit_schedule(q_payroll, fit, new_employer)
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
                due, unv = _roll(raw)
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
    return items, deposit


def _futa_deposits(year: int, q_payroll: float, n_emp: int, start: Optional[Date]) -> list[Obligation]:
    rate, _, rules, rate_notes = _futa_rate(year)
    base = P(FUTA, "taxable_wage_limit_per_employee")
    limit = UNVERIFIED["futa_deposit_threshold"]["usd"]
    per_emp = q_payroll / n_emp
    items, carried, math, k = [], 0.0, [], 0
    for q in range(1, 5):
        q_end = Date(year, q * 3, calendar.monthrange(year, q * 3)[1])
        if start and q_end < start:
            continue
        k += 1          # quarters with payroll so far this year
        taxable = n_emp * (min(k * per_emp, base) - min((k - 1) * per_emp, base))
        tax = _money(taxable * rate / 100)
        carried = _money(carried + tax)
        math.append(f"Q{q}: {n_emp} employees x taxable wages {_usd(_money(taxable / n_emp))} each (first "
                    f"{_usd(base)} per employee per year) x {_pct(round(rate, 4))} = {_usd(tax)}; undeposited "
                    f"{_usd(carried)}")
        if carried > limit:
            raw = _month_after_end(year, q * 3)
            due, unv = _roll(raw)
            items.append(_ob(
                f"FUTA deposit for Q{q} {year}", "payroll_tax", rules + [F940], due,
                unv + ["futa_deposit_threshold", "irs_failure_to_deposit"], form="EFTPS (Form 940 tax)",
                agency="IRS", who_files="Employer or its payroll service", how="Electronic funds transfer",
                covers=f"FUTA on wages paid through Q{q} {year}", math=list(math) + [
                    f"{_usd(carried)} is more than {_usd(limit)}: deposit by {due.isoformat()}"],
                penalty_if_late=UNVERIFIED["irs_failure_to_deposit"]["note"],
                notes=rate_notes + [UNVERIFIED["futa_deposit_threshold"]["note"],
                                    "Assumes the same employees all year at an even quarterly payroll."]))
            carried, math = 0.0, []
    if carried:
        note = (f"FUTA left undeposited at year end: {_usd(carried)} (not over {_usd(limit)}), so it can be paid "
                f"with the {year} Form 940.")
        items.append(_ob(f"FUTA for {year}: pay the {_usd(carried)} balance with Form 940", "payroll_tax",
                         rules + [F940], None, ["futa_deposit_threshold"], status="event_driven",
                         trigger="With the annual Form 940", form="Form 940", agency="IRS",
                         who_files="Employer or its payroll service", math=math, notes=rate_notes + [note]))
    return items


def _year_end_items(year: int, has_employees: bool, pays_ics: bool, start: Optional[Date]) -> list[Obligation]:
    items = []
    for wy in (year - 1, year):
        if start and start.year > wy:
            continue
        if has_employees:
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
            limit = limits["payments_before_2026"] if wy < 2026 else limits["payments_in_2026"]
            raw = _month_day(P(NEC_DUE, "file_with_irs_by"), wy + 1, NEC_DUE)
            due, unv = _roll(raw)
            notes = ["Not needed for payments to corporations (except attorneys and medical/health care); collect a "
                     "W-9 from every sub before the first payment.",
                     f"No W-9 / no TIN: withhold {_pct(P(BACKUP, 'rate_percent'))} backup withholding."]
            if wy > 2026:
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
                ic_dates: list[Date]) -> list[Obligation]:
    items = []
    de34_days = P(DE34, "deadline_calendar_days_after_start_of_work")
    de34_pen = (f"{_usd(P(DE34, 'penalty_usd_per_failure'))} per unreported hire, or "
                f"{_usd(P(DE34, 'penalty_usd_per_failure_conspiracy_or_false_report'))} if the failure is a "
                "conspiracy with the employee or a false/incomplete report (waived for good cause).")
    de34_kw = dict(form="DE 34 (Report of New Employee(s)) or e-Services", agency="EDD", who_files="Employer",
                   penalty_if_late=de34_pen,
                   notes=["Include part-time and temporary workers.",
                          f"Rehires: report {P(DE34, 'includes_rehires')}."])
    if has_employees:
        items.append(_ob("Report each new hire to EDD", "new_hire", [DE34],
                         trigger=f"Within {de34_days} days of the employee's first day of work for pay", **de34_kw))
        i9_days = P(I9, "section_2_deadline_business_days_after_hire")
        items.append(_ob(
            "New-hire paperwork (I-9, wage notice, workers' comp and EDD pamphlets)", "new_hire",
            [I9, NTE, WC_HIRE, PAMPHLETS],
            trigger="Each hire: Form I-9 Section 1 on day one and Section 2 within "
                    f"{i9_days} business days; Labor Code 2810.5 Notice to Employee at hire (changes within "
                    f"{P(NTE, 'change_notice_calendar_days')} days); workers' comp rights notice by the end of the "
                    "first pay period; EDD pamphlets DE 2515 and DE 2511",
            who_files="Employer (office manager)", form="I-9, DLSE-NTE, DWC time-of-hire pamphlet, DE 2515, DE 2511",
            agency="USCIS / Labor Commissioner / DWC / EDD",
            penalty_if_late="I-9 paperwork violations are fined per form; see the I-9 rule.",
            notes=["Give notices in Spanish too if you have Spanish-speaking employees."]))
    for h in hire_dates:
        if h.year not in (year, year - 1):
            continue
        due = h + timedelta(days=de34_days)
        if due.year != year and h.year != year:
            continue
        items.append(_ob(f"DE 34 new-hire report for employee who started {h.isoformat()}", "new_hire", [DE34], due,
                         math=[f"{h.isoformat()} + {de34_days} days = {due.isoformat()}"], **de34_kw))
        i9_due, unv = _add_business_days(h, P(I9, "section_2_deadline_business_days_after_hire"))
        items.append(_ob(f"Form I-9 Section 2 for employee who started {h.isoformat()}", "new_hire", [I9], i9_due,
                         unv, form="Form I-9 Section 2", agency="USCIS (keep on file)", who_files="Employer",
                         math=[f"{h.isoformat()} + {P(I9, 'section_2_deadline_business_days_after_hire')} business "
                               f"days = {i9_due.isoformat()}"],
                         penalty_if_late="I-9 paperwork penalties per form (see rule form-i9-civil-penalties)."))
    if pays_ics:
        days, limit = P(DE542, "days"), P(DE542, "threshold_usd")
        de542_kw = dict(form="DE 542 (Report of Independent Contractor(s))", agency="EDD",
                        who_files="Business that hires the subcontractor",
                        penalty_if_late=f"{_usd(P(DE542, 'penalty_per_failure_usd_unverified'))} per unreported "
                                        "contractor (older EDD figure; unverified for 2026).",
                        notes=["Applies to individuals and sole proprietors you must issue a 1099 to; not to "
                               "corporations.", f"EDD still uses {_usd(limit)} even though the federal 1099-NEC "
                               "threshold is higher for 2026 payments.",
                               "Report each contractor once per year; check 'Ongoing' if the work continues."])
        items.append(_ob("Report subcontractors (sole proprietors) to EDD", "contractor_reporting", [DE542],
                         trigger=f"Within {days} days after you pay {_usd(limit)} or more in the year, or sign a "
                                 f"contract for {_usd(limit)} or more (labor and materials count), whichever is first",
                         **de542_kw))
        for d in ic_dates:
            due = d + timedelta(days=days)
            if year in (d.year, due.year):
                items.append(_ob(f"DE 542 for subcontractor paid/contracted {_usd(limit)}+ on {d.isoformat()}",
                                 "contractor_reporting", [DE542], due,
                                 math=[f"{d.isoformat()} + {days} days = {due.isoformat()}"], **de542_kw))
    return items


def _norm_class(c: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(c).upper().split("/")[-1])


def _license_items(year: int, entity: str, is_llc: bool, has_employees: bool, n_emp: int, exp: Optional[Date],
                   classes: list[str], bond_date: Optional[Date], wc_date: Optional[Date],
                   liab_date: Optional[Date], personnel: Optional[int]) -> tuple[list[Obligation], list[str]]:
    items, notes = [], []
    sole = entity == "sole_owner"
    renew = P(FEES, "renewal_active_sole_owner_usd" if sole else "renewal_active_non_sole_owner_usd")
    late = P(FEES, "renewal_active_delinquent_sole_owner_usd" if sole else "renewal_active_delinquent_non_sole_owner_usd")
    pct = P(DELINQ, "delinquency_percent_of_renewal_fee")
    norm = {_norm_class(c) for c in classes}
    c10 = "C10" in norm
    mandatory = sorted({_norm_class(c) for c in P(WC_MANDATORY, "classifications")} & norm)
    math = [f"Active renewal fee ({'sole owner' if sole else 'non-sole owner'}): {_usd(renew)}"
            + (f" + C-10 surcharge {_usd(P(FEES, 'c10_active_renewal_surcharge_usd'))}" if c10 else ""),
            f"Late: {_usd(renew)} + {pct}% delinquency fee = {_usd(_money(renew * (1 + pct / 100)))} "
            f"(CSLB schedule: {_usd(late)})"]
    lic_kw = dict(form="CSLB license renewal application (CSLB mails it before expiration) or online renewal",
                  agency="CSLB", who_files="Licensee (owner, officer or qualifier)",
                  how="Postmark or deliver to CSLB on or before the expiration date",
                  penalty_if_late=(f"Delinquent fee ({_usd(late)} instead of {_usd(renew)}). Renewed within "
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
    elif exp.year == year:
        items.append(_ob(f"CSLB license renewal (expires {exp.isoformat()})", "license", lic_rules, exp, **lic_kw))
    else:
        notes.append(f"Your CSLB license expires {exp.isoformat()}, outside {year}; no renewal is due this year.")

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
        if bond_date.year == year:
            items.append(_ob("Bond renewal / premium due", "insurance_bond", bond_rules, bond_date,
                             remind_on=bond_date - timedelta(weeks=lead), **bond_kw))
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
                             "claims-made or occurrence."])
        if liab_date:
            if liab_date.year == year:
                items.append(_ob("LLC liability insurance renewal (certificate to CSLB)", "insurance_bond",
                                 [LLC_INS, LLC_REINSTATE], liab_date, **ins_kw))
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
        wc_math = [f"CSLB minimum civil penalty ({'sole owner' if sole else 'entity'}): {_usd(min_cslb)} per violation"]
        if n_emp:
            wc_math.append(f"Labor Code 3722 assessment: greater of 2 x the premium you skipped or {_usd(per_emp)} x "
                           f"{n_emp} employees = at least {_usd(per_emp * n_emp)}")
        why = ("you have employees" if has_employees else f"class {', '.join(mandatory)} must carry it even without "
               "employees" if mandatory else "the no-employee exemption ends")
        wc_kw = dict(form="Certificate of Workers' Compensation Insurance", agency="CSLB (filed by your insurer)",
                     who_files="Your workers' comp insurer files the certificate; you confirm it on the CSLB record",
                     penalty_if_late=("License automatically suspended on the lapse date; DLSE stop order on all "
                                      "employee labor; uninsured-employer penalties."), math=wc_math,
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
            if wc_date.year == year:
                items.append(_ob("Workers' comp policy renewal (certificate to CSLB)", "insurance_bond", wc_rules,
                                 wc_date, **wc_kw))
        else:
            items.append(_ob("Workers' comp policy in force (certificate on file with CSLB)", "insurance_bond",
                             wc_rules, status="needs_date", trigger="Enter wc_policy_renewal_date from your policy",
                             **wc_kw))
    else:
        op = Date.fromisoformat(P(WC_2028, "operative_date"))
        items.append(_ob(
            "Workers' comp exemption on file with CSLB (no employees)", "insurance_bond",
            [WC_RECERT, WC_FIRST_HIRE, WC_2028], op if op.year == year + 1 else None,
            status="upcoming" if op.year == year + 1 else "event_driven",
            trigger="Recertify at every license renewal. The exemption becomes invalid the day you hire anyone.",
            form="CSLB exemption certificate / renewal form", agency="CSLB", who_files="Licensee",
            penalty_if_late=(f"Hiring while exempt: coverage is required from the first day and CSLB must get proof "
                             f"within {P(WC_FIRST_HIRE, 'days_to_file_proof_after_hire')} days, or the license is "
                             "suspended and you face uninsured-employer penalties."),
            notes=[f"From {op.isoformat()} every active license needs a workers' comp policy (only no-employee joint "
                   "ventures stay exempt). Budget for it now."]))
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
                         f"(minimum proposed {_usd(P(CALOSHA_POSTER, 'penalty_min_proposed_usd'))}). Workers' comp "
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
            math=[f"Exposure if missed: up to {_usd(per)} x {n_emp} employees = {_usd(per * n_emp)}"],
            penalty_if_late=f"Up to {_usd(per)} per employee."))
    vote = next((p for p in postings if p["posting"].lower().startswith("time off to vote")), None)
    elections = UNVERIFIED["statewide_election_dates"]["dates"].get(str(year), [])
    if vote and elections:
        days = _int_in(vote.get("timing", ""), r"(\d+) days", POSTINGS)
        for e in elections:
            ed = Date.fromisoformat(e)
            due = ed - timedelta(days=days)
            if ed < first:
                continue
            items.append(_ob(f"Post the time-off-to-vote notice for the {ed.isoformat()} statewide election",
                             "posting", [POSTINGS], due, ["statewide_election_dates"], form="Time off to vote notice",
                             agency="Secretary of State", who_files="Employer",
                             math=[f"{ed.isoformat()} - {days} days = {due.isoformat()}"],
                             penalty_if_late="No penalty amount in the rulebook."))
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
            math=[f"{day}th day of the {month}th month of the tax year = {due.isoformat()}"],
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


def _city_items(cities: list[str], renewal_dates: dict[str, str], year: int) -> list[Obligation]:
    items, dates = [], {k.strip().lower(): v for k, v in (renewal_dates or {}).items()}
    for city in dict.fromkeys(c.strip() for c in cities if c and c.strip()):
        d = dates.get(city.lower())
        d = _date(d, f"city_tax_renewal_dates[{city}]") if d else None
        if d and d.year != year:
            continue
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
                   f"Check {city} for a local minimum wage above the state rate."]))
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
                        city_tax_renewal_dates: Optional[dict[str, str]] = None) -> ComplianceCalendarResult:
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    if not 2000 <= int(year) <= 2100:
        raise ValueError("year must be a 4-digit year like 2026")
    if employee_count < 0:
        raise ValueError("employee_count cannot be negative")
    if not has_employees and employee_count > 0:
        raise ValueError("employee_count is above 0 but has_employees is false; fix one of them")
    if entity_type not in Entity.__args__:
        raise ValueError(f"entity_type must be one of {', '.join(Entity.__args__)}")
    notes: list[str] = []
    n_emp = employee_count
    if has_employees and n_emp == 0:
        n_emp = 1
        notes.append("has_employees is true but employee_count is 0; counted as 1 employee.")
    is_llc = llc or entity_type == "llc"
    if llc and entity_type != "llc":
        notes.append(f"llc=true with entity_type={entity_type}: LLC rules were applied.")
    start = _date(started_paying_wages_on, "started_paying_wages_on") if started_paying_wages_on else None

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
    items += _hire_items(year, has_employees, pays_independent_contractors,
                         [_date(x, "new_hire_dates") for x in new_hire_dates or []],
                         [_date(x, "independent_contractor_600_dates") for x in independent_contractor_600_dates or []])
    lic, lic_notes = _license_items(
        year, entity_type, is_llc, has_employees, n_emp if has_employees else 0,
        _date(license_expiration_date, "license_expiration_date") if license_expiration_date else None,
        license_classifications or [],
        _date(bond_renewal_date, "bond_renewal_date") if bond_renewal_date else None,
        _date(wc_policy_renewal_date, "wc_policy_renewal_date") if wc_policy_renewal_date else None,
        _date(liability_policy_renewal_date, "liability_policy_renewal_date") if liability_policy_renewal_date else None,
        personnel_of_record_count)
    items += lic
    notes += lic_notes
    items += _posting_items(year, has_employees, n_emp, start)
    items += _safety_items(year, has_employees, n_emp, start)
    items += _entity_items(year, entity_type, is_llc)
    if entity_type == "corporation" and not is_llc:
        notes.append("Corporations also owe FTB's annual minimum franchise tax and estimated payments; those are not "
                     "in the verified rulebook, so ask your CPA for the dates.")
    items += _city_items(cities_worked or [], city_tax_renewal_dates or {}, year)

    for it in items:
        if it.due_date:
            it.days_until_due = (Date.fromisoformat(it.due_date) - today).days
            it.status = ("passed" if it.days_until_due < 0 else
                         "due_soon" if it.days_until_due <= DUE_SOON_DAYS else "upcoming")
        elif it.status != "needs_date":
            it.status = "event_driven"
    items.sort(key=lambda i: (i.due_date is None, i.due_date or "", i.title))
    upcoming = [i for i in items if i.due_date and i.status != "passed"]
    if year != 2026:
        notes.append(f"Dollar amounts and thresholds are the verified 2026 values; check for {year} changes.")
    notes.append("IRS and EDD return and deposit dates are moved to the next business day when they land on a weekend "
                 "or federal holiday; other dates are not, so act by the prior business day.")
    counts: dict[str, int] = {}
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
    used = sorted({k for it in items for k in it.unverified_values})
    result = ComplianceCalendarResult(
        year=year, as_of=today.isoformat(), entity_type=entity_type, is_llc=is_llc, has_employees=has_employees,
        employee_count=n_emp if has_employees else 0, items=items, next_due=upcoming[0] if upcoming else None,
        counts=counts, deposit_schedule=deposit, notes=notes,
        unverified_values_used={k: UNVERIFIED[k] for k in used})
    result.cite(*[r for it in items for r in it.rule_ids], *(deposit.rule_ids if deposit else []))
    if used or year != 2026:
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


def labor_burden_estimate(hourly_wage: float, annual_hours: float, wc_rate_per_100: float,
                          ui_rate: Optional[float] = None, as_of: Optional[str] = None, *,
                          experience_mod: float = 1.0, wc_class_code: Optional[str] = None,
                          include_paid_sick_leave: bool = False, ett_applies: bool = True) -> LaborBurdenResult:
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    if hourly_wage <= 0 or annual_hours <= 0:
        raise ValueError("hourly_wage and annual_hours must be greater than 0")
    if annual_hours > 366 * 24:
        raise ValueError("annual_hours is more hours than a year has")
    if wc_rate_per_100 < 0 or experience_mod <= 0:
        raise ValueError("wc_rate_per_100 cannot be negative and experience_mod must be above 0")
    if ui_rate is not None and ui_rate < 0:
        raise ValueError("ui_rate cannot be negative")
    year = today.year
    notes: list[str] = []
    rules_used: list[str] = []

    def line(item, paid_by, rules, annual, math, rate=None, taxable=None) -> BurdenLine:
        rules_used.extend(rules)
        return BurdenLine(item=item, paid_by=paid_by, rate_percent=rate, taxable_wages_usd=taxable,
                          annual_usd=annual, per_hour_usd=_money(annual / annual_hours), math=math, rule_ids=rules,
                          verified=all(core.rule(r).verified for r in rules))

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
                          f"{_usd(hourly_wage)} = {_usd(sick)}"))
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
    if ui_rate is None:
        ui_pct = P(UI, "new_employer_rate_percent")
        notes.append(f"CA UI uses the new-employer rate {_pct(ui_pct)} (first {P(UI, 'new_employer_rate_duration_years')}"
                     " years). After that use the rate on your EDD DE 2088 notice.")
    else:
        ui_pct = ui_rate
        if ui_rate < 1:
            notes.append(f"ui_rate is a percent: you passed {ui_rate}, used as {ui_rate}%. If your DE 2088 shows "
                         f"{ui_rate} as a decimal, pass {round(ui_rate * 100, 4):g} instead.")
    lines.append(line("CA Unemployment Insurance (UI)", "employer", [UI], _money(uw * ui_pct / 100),
                      f"{_pct(ui_pct)} x {_usd(uw)} (first {_usd(ui_base)})", ui_pct, uw))
    ett_base = P(ETT, "taxable_wage_limit_per_employee")
    ew = min(wages, ett_base)
    ett_pct = P(ETT, "rate_percent") if ett_applies else 0.0
    lines.append(line("CA Employment Training Tax (ETT)", "employer", [ETT], _money(ew * ett_pct / 100),
                      f"{_pct(ett_pct)} x {_usd(ew)} (first {_usd(ett_base)})"
                      + ("" if ett_applies else " - no ETT (negative-reserve employer)"), ett_pct, ew))
    wc = _money(wages / 100 * wc_rate_per_100 * experience_mod)
    lines.append(line("Workers' compensation premium", "employer", [WC_EST, WC_ONE], wc,
                      f"{_usd(wages)} / 100 x {_usd(wc_rate_per_100)} per $100 x {experience_mod:g} experience mod = "
                      f"{_usd(wc)}", taxable=wages))
    notes.append(f"Workers' comp rates are set by your insurer by class code; the state average advisory pure premium "
                 f"for policies from Sept 1, 2026 is {_usd(P(WC_EST, 'statewide_average_advisory_pure_premium_per_100_payroll_2026_09_01'))} "
                 "per $100, and construction classes run much higher. Policy fees and surcharges are not included.")

    if wc_class_code:
        code = re.sub(r"\D", "", wc_class_code)
        thr = P(WC_DUAL, "carpentry_5403_5432_threshold_eff_2026_09_01")
        rules_used.append(WC_DUAL)
        if code in ("5403", "5432"):
            right = "5432" if hourly_wage >= thr else "5403"
            notes.append(f"Carpentry dual-wage classes: {_usd(hourly_wage)}/h is "
                         f"{'at or above' if right == '5432' else 'below'} the {_usd(thr)} threshold "
                         "(regular hourly wage, threshold effective 2026-09-01), so payroll belongs in "
                         f"{right}"
                         + (f", not {code}; use the {right} rate." if right != code else "."))
        else:
            notes.append(f"Class {wc_class_code}: if it is one of the construction dual-wage classes, check its "
                         "current wage threshold (thresholds changed Sept 1, 2026).")

    sdi_pct = P(SDI, "employee_withholding_rate_percent")
    employee = [
        line("CA SDI (employee-paid, you withhold it)", "employee", [SDI], _money(wages * sdi_pct / 100),
             f"{_pct(sdi_pct)} x {_usd(wages)} (no wage cap)", sdi_pct, wages),
        line(f"Employee Social Security ({_pct(ss)})", "employee", [FICA], _money(ss_wages * ss / 100),
             f"{_pct(ss)} x {_usd(ss_wages)}", ss, ss_wages),
        line(f"Employee Medicare ({_pct(med)})", "employee", [FICA], _money(wages * med / 100),
             f"{_pct(med)} x {_usd(wages)}", med, wages),
    ]
    notes.append("SDI and the employee halves of Social Security and Medicare come out of the worker's pay; they are "
                 "not part of your labor burden. Income tax withholding is not shown.")
    mw = P(MIN_WAGE, "hourly_usd")
    rules_used.append(MIN_WAGE)
    if hourly_wage < mw:
        notes.append(f"{_usd(hourly_wage)}/h is below the {year if year == 2026 else 2026} California minimum wage of "
                     f"{_usd(mw)}; local minimums can be higher.")
        rules_used.append(LOCAL_WAGE)
    notes.append("Not included: health/welfare, vacation, union fringes, liability insurance, overtime premium. On "
                 "public works use the DIR prevailing wage determination for wage and fringes.")
    if year != 2026:
        notes.append(f"Rates are the verified 2026 values; {year} rates are not in the rulebook.")

    taxes = _money(sum(x.annual_usd for x in lines) - sick)
    total = _money(wages + taxes)
    result = LaborBurdenResult(
        tax_year=year, hourly_wage=hourly_wage, annual_hours=annual_hours, base_wages_usd=base,
        paid_sick_leave_usd=sick, taxable_wages_usd=wages, employer_lines=lines, employee_withholding_lines=employee,
        employer_taxes_and_insurance_usd=taxes, total_annual_cost_usd=total,
        burdened_hourly_rate_usd=_money(total / annual_hours),
        burden_percent_of_base_wages=_money((total - base) / base * 100), notes=notes)
    result.cite(*rules_used)
    if year != 2026:
        result.verified = False
    return result


# ---------------------------------------------------------------- MCP registration
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="compliance_calendar", annotations=ro)
    def _compliance_calendar(
        year: Annotated[int, Field(description="Calendar year to build, e.g. 2026")],
        entity_type: Annotated[Entity, Field(description="How the license is held: sole_owner, partnership, "
                                                         "corporation, llc or joint_venture")] = "sole_owner",
        has_employees: Annotated[bool, Field(description="True if you pay any W-2 employees (even one part-time)")] = True,
        employee_count: Annotated[int, Field(ge=0, description=(
            "Most employees on payroll at any one time last year (drives Log 300A, OSHA ITA, poster and training "
            "rules and penalty exposure)"))] = 0,
        license_expiration_date: Annotated[Optional[str], Field(description="ISO CSLB license expiration date")] = None,
        llc: Annotated[bool, Field(description="True if the licensee is an LLC (same as entity_type='llc')")] = False,
        quarterly_payroll_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Typical gross wages per quarter, all employees (for the deposit schedule and FUTA deposits)"))] = None,
        pays_independent_contractors: Annotated[bool, Field(description=(
            "True if you pay unincorporated subs/individuals (1099-NEC and EDD DE 542)"))] = False,
        cities_worked: Annotated[Optional[list[str]], Field(description=(
            "Cities where you hold or need a business tax certificate, e.g. ['San Diego', 'Chula Vista']"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to measure days left from; default today in California")] = None,
        quarterly_income_tax_withheld_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Federal income tax withheld per quarter, if known (makes the deposit schedule exact)"))] = None,
        new_employer: Annotated[bool, Field(description="True if you had no payroll in the IRS lookback period")] = False,
        started_paying_wages_on: Annotated[Optional[str], Field(description=(
            "ISO date you first paid wages, if that happened this year (adds EDD registration and EIN deadlines)"))] = None,
        new_hire_dates: Annotated[Optional[list[str]], Field(description="ISO first-work dates of new hires (adds dated DE 34 and I-9 items)")] = None,
        independent_contractor_600_dates: Annotated[Optional[list[str]], Field(description=(
            "ISO dates you first paid or contracted $600+ with a sole-proprietor sub this year (adds dated DE 542 items)"))] = None,
        license_classifications: Annotated[Optional[list[str]], Field(description="CSLB classes held, e.g. ['B', 'C-39']")] = None,
        bond_renewal_date: Annotated[Optional[str], Field(description="ISO bond renewal / premium date")] = None,
        wc_policy_renewal_date: Annotated[Optional[str], Field(description="ISO workers' comp policy expiration date")] = None,
        liability_policy_renewal_date: Annotated[Optional[str], Field(description="ISO liability policy expiration (LLCs)")] = None,
        personnel_of_record_count: Annotated[Optional[int], Field(ge=0, description="LLC personnel of record on the CSLB license")] = None,
        city_tax_renewal_dates: Annotated[Optional[dict[str, str]], Field(description=(
            "Renewal date per city from your certificates, e.g. {'San Diego': '2026-12-31'}"))] = None,
    ) -> ComplianceCalendarResult:
        """California contractor compliance calendar for one year. Returns a list of obligations sorted by date, each
        with due_date (or a trigger for event-driven items), who files, form, agency, penalty if late, the math,
        status (passed / due_soon / upcoming / event_driven / needs_date), days until due and rule citations:
        EDD registration (15 days after a quarter's wages pass $100) and EIN, EDD DE 9/DE 9C and IRS 941 for each
        quarter (last day of the month after the quarter, moved past weekends/holidays), monthly or semiweekly
        payroll tax deposits and FUTA deposits, W-2/W-3, 1099-NEC and Form 940, new-hire DE 34 (20 days), I-9, DE 542
        for subs ($600, 20 days), CSLB license renewal with fees, contractor/qualifier/LLC bonds, LLC liability
        insurance, workers' comp certificate or exemption, annual posters, Know Your Rights notice (Feb 1),
        time-off-to-vote notices, Cal/OSHA Form 300A posting (Feb 1-Apr 30) and OSHA ITA submission, harassment
        training, FTB/SOS entity filings, and a city business tax reminder per city (no city rates). Also returns
        the federal deposit schedule basis with its math, next_due, counts by status and notes."""
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
                                   city_tax_renewal_dates=city_tax_renewal_dates)

    @mcp.tool(name="labor_burden_estimate", annotations=ro)
    def _labor_burden_estimate(
        hourly_wage: Annotated[float, Field(gt=0, description="Straight-time hourly wage, e.g. 35")],
        annual_hours: Annotated[float, Field(gt=0, description="Hours worked per year, e.g. 2000")],
        wc_rate_per_100: Annotated[float, Field(ge=0, description=(
            "Your workers' comp rate per $100 of payroll for this worker's class code (from your policy)"))],
        ui_rate: Annotated[Optional[float], Field(ge=0, description=(
            "Your CA UI rate in percent from EDD notice DE 2088, e.g. 2.9. Leave empty for the new-employer rate"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; its year picks the tax year (default today)")] = None,
        experience_mod: Annotated[float, Field(gt=0, description="Workers' comp experience modification, e.g. 0.92")] = 1.0,
        wc_class_code: Annotated[Optional[str], Field(description="WC class code, e.g. '5403' (checks the carpentry dual-wage threshold)")] = None,
        include_paid_sick_leave: Annotated[bool, Field(description="Add the cost of the paid sick leave the worker can use")] = False,
        ett_applies: Annotated[bool, Field(description="False if EDD shows ETT 0.0% (negative-reserve employer)")] = True,
    ) -> LaborBurdenResult:
        """Employer labor burden for one California worker for a year. Returns each employer line with rate, taxable
        wages, annual and per-hour cost and its math: Social Security 6.2% to the wage base, Medicare 1.45%, FUTA
        0.6% plus California's credit reduction on the first $7,000, CA UI (your rate or the 3.4% new-employer rate)
        on the first $7,000, ETT 0.1% on the first $7,000, workers' comp (payroll / 100 x rate x experience mod),
        optional paid sick leave; plus employee withholdings (SDI 1.3%, employee FICA) shown separately. Also
        returns total annual cost, burdened hourly rate, burden percent, notes (dual-wage WC class check, minimum
        wage) and rule citations. Rates are the rulebook's 2026 values."""
        return labor_burden_estimate(hourly_wage, annual_hours, wc_rate_per_100, ui_rate, as_of,
                                     experience_mod=experience_mod, wc_class_code=wc_class_code,
                                     include_paid_sick_leave=include_paid_sick_leave, ett_applies=ett_applies)
