"""California public works (prevailing wage) compliance: Labor Code 1720-1861, 8 CCR 16000-16461 and 230-230.2,
Public Contract Code 4100-4114 (Subletting and Subcontracting Fair Practices Act), 7107 and 7201 (retention).

  public_works_readiness     project facts -> does prevailing wage apply (the $1,000 / small-project / $30,000 tiers),
                             DIR registration (status, fee, renewal), PWC-100, weekly certified payroll and eCPR
                             uploads with penalties, apprenticeship (DAS 140, DAS 142, 1:5 hours, training fund),
                             subcontractor listing (PCC 4104), retention cap and release, record retention, job-site
                             postings, and a dated checklist
  apprentice_hours_required  straight-time journeyman hours -> required apprentice hours (1 per 5), shortfall and
                             the minimum DAS 142 dispatch request
  prevailing_wage_penalties  violation facts -> LC 1775, 1813, 1776(h), 1771.4, 1771.1 and 1777.7 penalty exposure

Every legal number is read from the rulebook with core.rule(id).param(key) at call time. California law only:
federal Davis-Bacon / Copeland Act duties on federally funded jobs are not computed.
"""
import calendar
import math
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
PW_DEF, PW_FUNDS, PW_DEMIN, PW_RES = ("pw-definition-public-works", "pw-paid-with-public-funds-definition",
                                      "pw-de-minimis-public-subsidy", "pw-private-residential-exclusion")
PW_1000, SMALL, SMALL_MEASURE = ("pw-prevailing-wage-threshold-1000", "small-project-exemption-25000-15000",
                                 "small-project-exemption-measured-on-prime-contract")
COVERAGE_DET, FED_ONLY = "pw-coverage-determination", "dir-registration-who-must-register"
REG_PROOF, REG_FEE, REG_FY, REG_PEN, REG_GRACE, REG_TIME = (
    "registration-proof-before-bid-or-subcontract", "dir-registration-fee", "dir-registration-fiscal-year-renewal",
    "dir-registration-penalty-2000", "dir-registration-late-renewal-grace", "dir-registration-processing-time")
UNREG, UNREG_SUB, LIST_CURE = ("unregistered-contractor-penalty", "unregistered-sub-penalty-higher-tier",
                               "bid-listing-unregistered-sub-cure")
PWC100, PWC100_PEN, FINAL_HOLD = ("pwc100-project-registration-30-days", "awarding-body-pwc100-penalty",
                                  "awarding-body-withhold-final-payment-30-days")
POSTINGS, RATES, WC_CERT, SAFE_HARBOR = ("jobsite-notices-posting", "awarding-body-specify-rates-in-bid",
                                         "workers-comp-certification-1861", "prime-safe-harbor-subcontract-clauses")
ECPR, ECPR_PEN, ECPR_EXEMPT = "ecpr-monthly-submission", "ecpr-late-penalty", "ecpr-exemptions"
CPR_REQ, CPR_PEN, CPR_LOC, CPR_DEBAR = ("cpr-request-10-days", "cpr-late-penalty-100-per-worker-day",
                                        "cpr-records-location-notice", "cpr-failure-debarment")
CPR_KEEP, CSLB_KEEP, CWPA = "cpr-record-retention", "records-retention-5-years", "cwpa-18-month-limit"
OT, PEN_1775, PEN_1813 = "pw-overtime-8-40-time-and-half", "penalty-1775-underpayment", "penalty-1813-overtime"
APP_MIN, DAS140, DAS142, DAS142_SIZE, RATIO = (
    "apprenticeship-threshold-30000", "das140-contract-award-deadline", "das142-dispatch-request-72-hours",
    "das142-minimum-request-size", "apprentice-ratio-1-to-5")
TRAIN, HOURS_STMT, APP_PRIME, PEN_1777_7 = (
    "training-fund-contributions", "apprentice-hours-verified-statement-60-days",
    "apprenticeship-prime-responsibility-stipulation", "apprenticeship-penalty-1777-7")
DEBAR_WILLFUL, DEBAR_APP = "debarment-fraud-and-willful", "debarment-apprenticeship-serious-violation"
LIQ, CWPA_REVIEW = "liquidated-damages-60-days", "cwpa-request-review-60-days"
LISTING, LIST_24H, SUBLET_PEN = "sub-listing-threshold", "sub-listing-24-hour-corrections", "subletting-act-penalties"
RET_CAP, RET_RELEASE, RET_STATE = ("public-retention-5pct-cap", "public-retention-release-60-days",
                                   "pcc-7107-public-retention-60-days")
SB909, AB1198 = "sb909-pending-penalty-increases", "ab1198-mid-project-rate-changes-35m"
PW_NO_AWS, PW_SPLIT = "pw-no-alternative-workweek", "pw-no-project-splitting"
APP_REGISTERED, STOP_ORDER, REG_AT_AWARD, PRIV_RET = (
    "apprentice-must-be-registered", "unregistered-stop-order", "registration-jv-and-federal-bids-at-award",
    "private-retention-5pct-cap")
SUB_INFO = "sub-listing-required-information"
RULE_IDS = (PW_DEF, PW_FUNDS, PW_DEMIN, PW_RES, PW_1000, SMALL, SMALL_MEASURE, COVERAGE_DET, FED_ONLY, REG_PROOF,
            REG_FEE, REG_FY, REG_PEN, REG_GRACE, REG_TIME, UNREG, UNREG_SUB, LIST_CURE, PWC100, PWC100_PEN, FINAL_HOLD,
            POSTINGS, RATES, WC_CERT, SAFE_HARBOR, ECPR, ECPR_PEN, ECPR_EXEMPT, CPR_REQ, CPR_PEN, CPR_LOC, CPR_DEBAR,
            CPR_KEEP, CSLB_KEEP, CWPA, OT, PEN_1775, PEN_1813, APP_MIN, DAS140, DAS142, DAS142_SIZE, RATIO, TRAIN,
            HOURS_STMT, APP_PRIME, PEN_1777_7, DEBAR_WILLFUL, DEBAR_APP, LIQ, CWPA_REVIEW, LISTING, LIST_24H, SUBLET_PEN,
            RET_CAP, RET_RELEASE, RET_STATE, SB909, AB1198, PW_NO_AWS, PW_SPLIT, APP_REGISTERED, STOP_ORDER,
            REG_AT_AWARD, PRIV_RET, SUB_INFO)

# Every value this module computes with has a rulebook rule, so nothing is defined here. A result that relies on
# an unchecked or unverifiable rule cites it, which sets verified=False and lists it in unverified_rule_ids.
UNVERIFIED: dict = {}

# Name spellings (not law) that point at the legacy labor compliance programs listed in rule ecpr-exemptions.
_LCP_ALIASES = {"lausd": "Los Angeles Unified School District",
                "california department of transportation": "Caltrans",
                "sacramento county": "County of Sacramento"}
DUE_SOON_DAYS = 14          # checklist display window, not a legal number

AwardingBody = Literal["state_agency", "city", "county", "school_district", "community_college_district",
                       "special_district", "uc_csu", "joint_powers_authority", "private_developer", "federal_agency"]
WorkType = Literal["construction", "alteration", "demolition", "installation", "repair", "maintenance",
                   "street_highway_bridge"]
Role = Literal["prime", "subcontractor"]
UnderpaymentCategory = Literal["good_faith_corrected", "standard", "prior_violation_3_years", "willful"]
PUBLIC_BODIES = {"state_agency", "city", "county", "school_district", "community_college_district",
                 "special_district", "uc_csu", "joint_powers_authority"}


def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _ok(rule_ids) -> bool:
    return all(core.rule(r).verified for r in rule_ids)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _d(value, field: str) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2027-01-11, got {value!r}") from None


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _plus_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                      # Feb 29 -> Feb 28
        return d.replace(year=d.year + n, day=28)


def _plus_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _business_days_before(d: date, n: int) -> date:
    """n weekdays before d (Saturdays and Sundays skipped; holidays are NOT skipped)."""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _das142_send_by(needed: date, hours: float) -> tuple[date, date]:
    """8 CCR 230.1(a): notice 'at least 72 hours (excluding Saturdays, Sundays and holidays) before the date' apprentices
    are required. Counted back from 12:00 a.m. on that date, 72 weekday hours end at the START of the 3rd weekday
    before, so the notice must go out by the end of the weekday before that. -> (send-by date, cutoff date)."""
    cutoff = _business_days_before(needed, math.ceil(hours / 24))
    return _business_days_before(cutoff, 1), cutoff


def _sb909_operative() -> date:
    """Assumed SB 909 operative date, read from the rule ('2026 non-urgency statutes default to 2027-01-01')."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(P(SB909, "operative_date")))
    if not m:
        raise KeyError(f"rule {SB909} operative_date has no ISO date")
    return date.fromisoformat(m.group(0))


def _fy_end(d: date) -> date:
    """June 30 that ends the DIR registration fiscal year containing d (8 CCR 16410(b))."""
    mm, dd = (int(x) for x in P(REG_FY, "period_end").split("-"))
    end = date(d.year, mm, dd)
    return end if d <= end else date(d.year + 1, mm, dd)


def _next(today: date, *dates: Optional[date]) -> Optional[date]:
    """The first milestone still ahead of today, else the last one (already passed -> shows as overdue)."""
    ds = sorted(d for d in dates if d)
    return next((d for d in ds if d >= today), ds[-1] if ds else None)


def _fiscal_years(a: date, b: date) -> int:
    return max(1, _fy_end(b).year - _fy_end(a).year + 1)


def _ratio(text: str) -> tuple[float, float]:
    a, b = (float(x) for x in str(text).split(":"))
    return a, b


def _legacy_lcp(name: Optional[str]) -> Optional[str]:
    if not name:
        return None

    def norm(s: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())
    n = norm(name)
    for alias, body in _LCP_ALIASES.items():
        if alias in n:
            n = norm(body)
    return next((body for body in P(ECPR_EXEMPT, "exempt_legacy_lcps") if norm(body) in n), None)


# ---------------------------------------------------------------- models
class Subcontractor(BaseModel):
    name: str = Field(description="Subcontractor business name")
    license_number: Optional[str] = Field(None, description="CSLB license number")
    dir_number: Optional[str] = Field(None, description="DIR public works contractor registration (PWCR) number")
    bid_amount_usd: float = Field(ge=0, description="Subcontract / sub bid amount in dollars")
    portion_of_work: Optional[str] = Field(None, description="Portion of work, e.g. 'electrical', 'concrete'")
    dir_registration_expiry: Optional[date] = Field(None, description="ISO date the sub's DIR registration expires")


class Line(BaseModel):
    item: str
    value: Optional[float] = None
    unit: Literal["usd", "days", "hours", "percent", "count", "fiscal_years"] = "usd"
    math: str
    rule_ids: list[str]
    verified: bool = True


class Task(BaseModel):
    task: str
    who: str
    due: Optional[str] = None
    due_rule: str
    days_left: Optional[int] = None
    status: Literal["done", "overdue", "due_soon", "upcoming", "recurring", "on_request", "no_date"]
    licensed_human: Optional[str] = None
    rule_ids: list[str]
    verified: bool = True


class Coverage(BaseModel):
    public_work: Optional[bool] = Field(description="None = the facts given cannot settle it; get a coverage "
                                                    "determination and plan as if covered")
    prevailing_wage_applies: Optional[bool]
    tier: Literal["not_public_work", "1000_or_less", "small_project", "full"]
    measured_on_usd: float
    small_project_limit_usd: Optional[float] = None
    small_project_exempt: bool = False
    apprenticeship_applies: bool = False
    reasons: list[str] = Field(default_factory=list)


class DirRegistration(BaseModel):
    required: bool
    registered: bool
    status: Literal["not_required", "ok", "renew_before_completion", "expires_before_start",
                    "expired_grace_period", "expired", "not_registered"]
    expiry: Optional[str] = None
    current_fiscal_year_ends: str
    fiscal_years_to_buy: int = 0
    fee_usd: float = Field(0, description="Fee for the next registration or renewal (0 if nothing to buy)")
    fee_due_by: Optional[str] = None
    penalty_low_usd: float = 0
    penalty_high_usd: float = 0
    portal: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class PayrollReporting(BaseModel):
    certified_payroll_required: bool
    ecpr_upload_required: bool
    ecpr_exempt_reason: Optional[str] = None
    record_period: str = ("One certified payroll record per payroll week (each worker's straight-time and overtime "
                          "hours each day and week, classification and pay). Upload each week as you run payroll.")
    max_days_between_uploads: Optional[int] = None
    first_upload_due: Optional[str] = None
    penalty_possible_after: Optional[str] = None
    final_upload_due: Optional[str] = None
    ecpr_late_penalty_per_day_usd: Optional[float] = None
    ecpr_late_penalty_max_per_project_usd: Optional[float] = None
    cpr_request_response_days: Optional[int] = None
    cpr_late_penalty_per_worker_per_day_usd: Optional[float] = None
    overtime_rule: Optional[str] = None


class CraftHours(BaseModel):
    craft: str
    journeyman_hours: float
    required_apprentice_hours: float
    das142_min_request_hours: Optional[float] = None
    math: str


class Apprenticeship(BaseModel):
    applies: bool
    reason: str
    ratio: Optional[str] = None
    das140_due: Optional[str] = None
    das142_request_by: Optional[str] = None
    training_fund_first_due: Optional[str] = None
    hours_statement_due: Optional[str] = None
    crafts: list[CraftHours] = Field(default_factory=list)


class SubCheck(BaseModel):
    name: str
    bid_amount_usd: float
    pct_of_prime_bid: Optional[float] = None
    must_list: Optional[bool] = None
    listing_violation_penalty_max_usd: Optional[float] = None
    dir_registration_needed: bool
    problems: list[str] = Field(default_factory=list)
    ok: bool


class SubListing(BaseModel):
    applies: bool
    prime_bid_usd: Optional[float] = None
    threshold_usd: Optional[float] = None
    threshold_math: Optional[str] = None
    subs: list[SubCheck] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Retention(BaseModel):
    applies: bool
    max_retention_pct: Optional[float] = None
    max_total_retention_usd: Optional[float] = None
    release_days_after_completion: Optional[int] = None
    release_due: Optional[str] = None
    pass_through_days_after_receipt: Optional[int] = None
    disputed_withhold_max_pct: Optional[float] = None
    final_payment_hold_days_after_sub_list: Optional[int] = None
    notes: list[str] = Field(default_factory=list)


class RecordRetention(BaseModel):
    keep_until: Optional[str] = None
    items: list[str] = Field(default_factory=list)


class ReadinessResult(core.Result):
    as_of: str
    your_role: str
    summary: str
    coverage: Coverage
    dir_registration: DirRegistration
    pwc100_required: bool
    pwc100_due: Optional[str] = None
    payroll: PayrollReporting
    apprenticeship: Apprenticeship
    subcontractor_listing: SubListing
    retention: Retention
    record_retention: RecordRetention
    jobsite_postings: list[str] = Field(default_factory=list)
    checklist: list[Task] = Field(default_factory=list)
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ApprenticeHoursResult(core.Result):
    craft: Optional[str] = None
    journeyman_hours: float
    excluded_overtime_hours: float
    counted_journeyman_hours: float
    ratio: str
    required_apprentice_hours: float
    apprentice_hours_worked: Optional[float] = None
    shortfall_hours: Optional[float] = None
    das142_min_request_hours: Optional[float] = None
    applies: Optional[bool] = None
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PenaltyLine(BaseModel):
    item: str
    section: str
    low_usd: float
    high_usd: float
    math: str
    rule_ids: list[str]
    verified: bool = True


class PenaltyResult(core.Result):
    as_of: str
    lines: list[PenaltyLine] = Field(default_factory=list)
    penalties_low_usd: float = 0
    penalties_high_usd: float = 0
    back_wages_usd: float = 0
    liquidated_damages_usd: float = 0
    total_low_usd: float = 0
    total_high_usd: float = 0
    debarment_risk: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- shared helpers
class _Ctx:
    """Collects cited rules, line items and checklist tasks for one call."""

    def __init__(self, today: date):
        self.today, self.cited, self.lines, self.tasks, self.notes = today, set(), [], [], []

    def use(self, *ids: str) -> list[str]:
        self.cited.update(ids)
        return list(ids)

    def line(self, item, value, unit, math_, *ids):
        self.lines.append(Line(item=item, value=value, unit=unit, math=math_, rule_ids=self.use(*ids),
                               verified=_ok(ids)))

    def task(self, task, who, due: Optional[date], due_rule, ids, status=None, licensed_human=None):
        """Add a checklist item; status comes from the due date unless given."""
        days = (due - self.today).days if due else None
        if status is None:
            if due is None:
                status = "no_date"
            elif days < 0:
                status = "overdue"
            elif days <= DUE_SOON_DAYS:
                status = "due_soon"
            else:
                status = "upcoming"
        self.tasks.append(Task(task=task, who=who, due=_iso(due), due_rule=due_rule, days_left=days, status=status,
                               licensed_human=licensed_human, rule_ids=self.use(*ids), verified=_ok(ids)))


def _sb909(ctx: _Ctx, what: str, reaches_operative: bool) -> None:
    """SB 909 (Ch. 660, Stats. 2026) amends LC 1725.5, 1775, 1776 and 1777.7; its amounts and operative date were not
    verified. When the dates involved reach the assumed operative date, the affected items cite the rule
    (verified=False) and this note says why; otherwise the note only warns."""
    operative = _sb909_operative()
    sections = ", ".join(P(SB909, "sections_amended_per_chaptered_title"))
    if reaches_operative:
        ctx.use(SB909)
        ctx.notes.append(f"SB 909 ({P(SB909, 'chapter')}) amends {sections} and probably takes effect "
                         f"{operative.isoformat()} (operative date not verified). The {what} shown are the pre-SB 909 "
                         "amounts; items that fall on or after that date are marked unverified. Confirm the current "
                         "figures on leginfo or with DIR before relying on them.")
    else:
        ctx.notes.append(f"SB 909 ({P(SB909, 'chapter')}) amends {sections}. The {what} shown are current law as "
                         f"checked; re-check them for anything on or after {operative.isoformat()}.")


def _apprentice_calc(journeyman_hours: float, project_journeyman_hours: Optional[float] = None
                     ) -> tuple[float, Optional[float], str, str]:
    """-> (required apprentice hours, minimum DAS 142 request hours or None, ratio text, math).
    The ratio is per craft; the under-40-hour exemption from the minimum request size is per PROJECT (8 CCR 230.1(a):
    'projects with less than 40 hours of journeyman work'), so it is tested on project_journeyman_hours when given."""
    ratio = P(RATIO, "ratio_apprentice_to_journeyman_hours")
    a, j = _ratio(ratio)
    required = _money(journeyman_hours * a / j)
    math_ = f"{journeyman_hours:,.2f} journeyman h x {a:g}/{j:g} = {required:,.2f} apprentice h"
    floor = P(DAS142_SIZE, "exempt_if_journeyman_hours_below")
    project = journeyman_hours if project_journeyman_hours is None else project_journeyman_hours
    if project < floor:
        return required, None, ratio, math_ + (f"; project under {floor:g} journeyman h ({project:,.2f}), no minimum "
                                               "DAS 142 request size")
    per_app = P(DAS142_SIZE, "min_hours_per_apprentice_request")
    pct = P(DAS142_SIZE, "min_pct_of_estimated_apprentice_hours")
    minimum = _money(max(per_app, required * pct / 100))
    return required, minimum, ratio, (math_ + f"; DAS 142 request at least the greater of {per_app:g} h per apprentice "
                                      f"or {pct:g}% x {required:,.2f} = {_money(required * pct / 100):,.2f} h "
                                      f"-> {minimum:,.2f} h")


def _listing_threshold(bid: float, work_type: str) -> tuple[float, str]:
    """PCC 4104(a)(1): list subs 'in excess of' 1/2 of 1% of the prime's total bid; streets/highways/bridges: in excess
    of the greater of 1/2 of 1% or $10,000."""
    if work_type == "street_highway_bridge":
        pct = P(LISTING, "streets_highways_bridges.pct_exclusive")
        floor = P(LISTING, "streets_highways_bridges.usd_exclusive")
        threshold = _money(max(bid * pct / 100, floor))
        return threshold, (f"streets/highways/bridges: greater of {pct:g}% x ${bid:,.2f} = ${_money(bid * pct / 100):,.2f}"
                           f" or ${floor:,.0f} -> list subs over ${threshold:,.2f}")
    pct = P(LISTING, "general_listing_threshold_pct_of_total_bid_exclusive")
    threshold = _money(bid * pct / 100)
    return threshold, f"{pct:g}% x ${bid:,.2f} prime bid = ${threshold:,.2f}; list subs over that"


# ---------------------------------------------------------------- readiness
def public_works_readiness(project_value_usd: float, awarding_body_type: AwardingBody, is_public_funds: bool,
                           work_type: WorkType, start_date, contractor_dir_registered: bool,
                           dir_registration_expiry=None, subcontractors: Optional[list] = None,
                           prime_bid_usd: Optional[float] = None, as_of=None, *, your_role: Role = "prime",
                           award_date=None, contract_execution_date=None, bid_opening_date=None,
                           estimated_completion_date=None, has_employees: bool = True,
                           journeyman_hours_by_craft: Optional[dict[str, float]] = None,
                           public_subsidy_usd: Optional[float] = None, all_single_family: bool = False,
                           awarding_body_name: Optional[str] = None,
                           worked_or_bid_while_unregistered: bool = False,
                           your_contract_usd: Optional[float] = None) -> ReadinessResult:
    today = _d(as_of, "as_of") or logic.today_ca()
    start = _d(start_date, "start_date")
    if start is None:
        raise ValueError("start_date is required (first day workers will be on the job)")
    award, executed = _d(award_date, "award_date"), _d(contract_execution_date, "contract_execution_date")
    bid_open, completion = _d(bid_opening_date, "bid_opening_date"), _d(estimated_completion_date,
                                                                          "estimated_completion_date")
    if project_value_usd < 0 or any(x is not None and x < 0 for x in (prime_bid_usd, your_contract_usd,
                                                                         public_subsidy_usd)):
        raise ValueError("project_value_usd, prime_bid_usd, your_contract_usd and public_subsidy_usd must be 0 or more")
    if completion and completion < start:
        raise ValueError("estimated_completion_date is before start_date")
    if awarding_body_type not in PUBLIC_BODIES | {"private_developer", "federal_agency"}:
        raise ValueError(f"unknown awarding_body_type {awarding_body_type!r}")
    subs = [s if isinstance(s, Subcontractor) else Subcontractor.model_validate(s) for s in subcontractors or []]
    value, bid = float(project_value_usd), float(prime_bid_usd if prime_bid_usd is not None else project_value_usd)
    ctx, maint, prime = _Ctx(today), work_type == "maintenance", your_role == "prime"
    you = "you (prime)" if prime else "you (subcontractor)"

    # ---- coverage: is it a public work?
    reasons: list[str] = []
    public_work: Optional[bool]
    if awarding_body_type == "federal_agency" and not is_public_funds:
        ctx.use(FED_ONLY, APP_MIN)
        public_work = False
        reasons.append("A federal agency controls the contract and no state or local money is involved: California "
                       "DIR registration, eCPR and apprenticeship rules do not apply (DIR FAQ). Federal Davis-Bacon "
                       "rules usually do; they are not computed here.")
    elif awarding_body_type == "private_developer":
        if not is_public_funds:
            ctx.use(PW_DEF)
            public_work = False
            reasons.append("Private project with no public funds or subsidy: not a public work under LC 1720(a)(1) on "
                           "these facts. If a public agency will lease or buy the building, get a coverage "
                           "determination.")
        else:
            ctx.use(PW_DEF, PW_FUNDS, PW_RES)
            reasons.append("A private project paid in part with public money or subsidies (cash, below-market land or "
                           "loans, waived or reduced fees, credits) counts as 'paid for out of public funds' (LC "
                           "1720(b)).")
            reasons.append("Private residential projects on private property are excluded unless built under an "
                           "agreement with a state agency, redevelopment/successor agency or local public housing "
                           "authority (LC 1720(c)(1)).")
            if public_subsidy_usd is None:
                public_work = None
                reasons.append("Give public_subsidy_usd (and use the total project cost as project_value_usd) to run "
                               "the de minimis subsidy test of LC 1720(c)(3).")
            else:
                ctx.use(PW_DEMIN)
                pct = public_subsidy_usd / value * 100 if value else 100.0
                cap_usd = P(PW_DEMIN, "max_subsidy_usd_exclusive")
                if all_single_family:
                    cap_pct = P(PW_DEMIN, "single_family_only_projects.max_subsidy_pct_exclusive")
                    de_min = pct < cap_pct
                    test = f"{pct:.2f}% < {cap_pct:g}% (all single-family: no dollar cap)"
                else:
                    cap_pct = P(PW_DEMIN, "max_subsidy_pct_of_total_project_cost_exclusive")
                    de_min = public_subsidy_usd < cap_usd and pct < cap_pct
                    test = (f"${public_subsidy_usd:,.0f} < ${cap_usd:,.0f} and {pct:.2f}% < {cap_pct:g}% "
                            "(both must be true)")
                ctx.line("Public subsidy as % of total project cost", round(pct, 2), "percent",
                         f"${public_subsidy_usd:,.0f} / ${value:,.0f} = {pct:.2f}%; de minimis if {test} -> "
                         f"{'de minimis' if de_min else 'NOT de minimis'}", PW_DEMIN)
                demin_from = _d(P(PW_DEMIN, "not_applicable_if_advertised_or_awarded_before"), "de minimis date")
                first_step = min((d for d in (bid_open, award) if d), default=None)
                if first_step and first_step < demin_from:
                    reasons.append(f"The dollar/percent de minimis test applies to projects advertised or awarded on "
                                   f"or after {demin_from}; this one dates from {first_step}, so get a coverage "
                                   "determination.")
                if de_min:
                    public_work = False
                    reasons.append("The public subsidy is de minimis, so the project is not a public work (LC "
                                   "1720(c)(3)).")
                elif all_single_family:
                    public_work = None
                    reasons.append("The subsidy is not de minimis, but a private residential project on private "
                                   "property is a public work only if built under an agreement with a state agency, "
                                   "a redevelopment/successor agency or a local public housing authority (LC "
                                   "1720(c)(1)). Check the subsidy agreement; planned below as if covered.")
                else:
                    public_work = True
                    reasons.append("The public subsidy is not de minimis, so the project is a public work (if it is "
                                   "private residential work on private property, see the LC 1720(c)(1) exclusion "
                                   "above).")
    else:
        ctx.use(PW_DEF)
        if is_public_funds:
            public_work = True
            label = "Street/highway/bridge construction" if work_type == "street_highway_bridge" else work_type.title()
            reasons.append(f"{label} work done under contract for a public awarding body and paid in whole or in part "
                           "out of public funds is a public work (LC 1720(a)(1)); maintenance is covered too (LC "
                           "1771).")
            if awarding_body_type == "federal_agency":
                reasons.append("Federal agency plus state or local money: California law can apply alongside "
                               "Davis-Bacon. Only the California duties are computed here.")
        else:
            ctx.use(COVERAGE_DET)
            public_work = None
            reasons.append("A public awarding body but no public funds: LC 1720(a)(1) needs public funds, but other "
                           "LC 1720 categories can still cover the job. Ask DIR for a written coverage determination "
                           f"({P(COVERAGE_DET, 'request_to')}) or an attorney; planned below as if covered.")

    pw_min = P(PW_1000, "threshold_usd")
    small_max = P(SMALL, "maintenance_max_usd_inclusive" if maint else "construction_max_usd_inclusive")
    app_min = P(APP_MIN, "threshold_usd")
    covered = public_work is not False
    if covered:
        ctx.line("Prevailing wage threshold (LC 1771)", value, "usd",
                 f"${value:,.2f} {'>' if value > pw_min else '<='} ${pw_min:,.0f} -> prevailing wage "
                 f"{'required' if value > pw_min else 'not required'}", PW_1000)
    if not covered or value <= pw_min:
        duty = "none"
    elif value <= small_max:
        duty = "small"
    else:
        duty = "full"
    if duty != "none":
        ctx.line("Small-project exemption (LC 1771.1(n))", value, "usd",
                 f"{'maintenance' if maint else 'construction'} limit ${small_max:,.0f} (inclusive), measured on the "
                 f"whole prime contract: ${value:,.2f} {'<=' if duty == 'small' else '>'} ${small_max:,.0f} -> "
                 + ("exempt from DIR registration, eCPR and PWC-100" if duty == "small" else "not exempt"),
                 SMALL, SMALL_MEASURE)
        if not prime:
            reasons.append("Thresholds are measured on the prime contract (awarding body to prime), not on your "
                           "subcontract; enter the prime contract amount as project_value_usd.")
    apprentice = duty == "full" and value >= app_min and has_employees
    if duty == "full":
        ctx.line("Apprenticeship threshold (LC 1777.5(o))", value, "usd",
                 f"${value:,.2f} {'>=' if value >= app_min else '<'} ${app_min:,.0f} -> apprenticeship "
                 f"{'applies' if value >= app_min else 'does not apply'}"
                 + ("" if has_employees else "; no employees, so no apprentices are required"), APP_MIN)
    tier = "not_public_work" if not covered else ("1000_or_less" if duty == "none" else
                                                  "small_project" if duty == "small" else "full")
    pw_applies = (duty != "none") if public_work is not None or duty == "none" else None
    if covered and duty in ("none", "small"):
        ctx.use(PW_SPLIT)
        reasons.append("Jobs may not be split into smaller work orders to stay under the "
                       + ", ".join(f"${x:,.0f}" for x in P(PW_SPLIT, "thresholds_affected_usd"))
                       + " thresholds (8 CCR 16100(b)(6)); yearly work for the same agency and vendor adds up.")
    coverage = Coverage(public_work=public_work, prevailing_wage_applies=pw_applies, tier=tier, measured_on_usd=value,
                        small_project_limit_usd=small_max if covered else None, small_project_exempt=duty == "small",
                        apprenticeship_applies=apprentice, reasons=reasons)
    if public_work is None and duty != "none":
        ctx.task("Get a written coverage determination before bidding without prevailing wages", you, bid_open or start,
                 "before you bid or start", [COVERAGE_DET], licensed_human="attorney (public works / prevailing wage)")

    reg_required, pw_on = duty == "full", duty != "none"
    need_until = completion or start
    # SB 909 (unverified) amends LC 1725.5 (registration fees) and LC 1776 (records penalty): amounts that fall due on
    # or after its assumed operative date cite it.
    sb_op = _sb909_operative()

    def sb(*ids: str, on: Optional[date] = None) -> tuple[str, ...]:
        return ids + ((SB909,) if (on or today) >= sb_op else ())

    # ---- DIR contractor registration
    fy_now = _fy_end(today)
    reg = DirRegistration(required=reg_required, registered=contractor_dir_registered, status="not_required",
                          current_fiscal_year_ends=fy_now.isoformat())
    exp = _d(dir_registration_expiry, "dir_registration_expiry")
    if reg_required:
        ids = ctx.use(REG_PROOF, REG_FEE, REG_FY, REG_TIME)
        reg.portal = P(ECPR, "portal")
        ctx.use(ECPR)
        options = P(REG_FEE, "multi_year_options_usd")
        max_years = max(int(k) for k in options)

        def buy(first_day: date, pay_by: date, why: str) -> None:
            n = _fiscal_years(first_day, need_until)
            if n > max_years:
                reg.notes.append(f"The project runs {n} fiscal years; DIR sells at most {max_years} at once, so renew "
                                 "again before the last one ends.")
                n = max_years
            reg.fiscal_years_to_buy, reg.fee_usd = n, float(options[str(n)])
            first_fy = _fy_end(first_day)
            last_fy = date(first_fy.year + n - 1, first_fy.month, first_fy.day)
            span = f"fiscal year ending {first_fy}" if n == 1 else f"fiscal years ending {first_fy} through {last_fy}"
            ctx.line(f"DIR registration fee ({why})", reg.fee_usd, "usd",
                     f"${P(REG_FEE, 'fee_per_fiscal_year_usd'):,.0f} per fiscal year x {n} ({span}) = "
                     f"${reg.fee_usd:,.0f}; nonrefundable; paid by {pay_by}", *sb(REG_FEE, REG_FY, on=pay_by))

        bid_or_sign = min(d for d in (bid_open, executed, start) if d)
        bid_unregistered = worked_or_bid_while_unregistered or start <= today or bool(
            prime and bid_open and bid_open <= today)
        speed = (f"Pay by credit card: registration posts in about {P(REG_TIME, 'credit_card_processing_hours')} "
                 f"hours; other payment methods can take up to {P(REG_TIME, 'other_methods_max_weeks')} weeks.")
        stop_order = (f"Working unregistered: the Labor Commissioner issues a stop order barring you from all public "
                      f"works until you register, and you must pay idled workers their prevailing rate for up to "
                      f"{P(STOP_ORDER, 'idled_worker_pay_max_days')} days (LC 1771.1(j)).")
        if contractor_dir_registered:
            if exp is None:
                exp = fy_now
                reg.notes.append(f"No expiry given; assumed it ends with the current fiscal year ({fy_now}). DIR "
                                 "registrations always end June 30.")
            elif exp != _fy_end(exp):
                reg.notes.append(f"DIR registrations end June 30; {exp} looks wrong, check it on the DIR site.")
            reg.expiry = exp.isoformat()
            if today > exp:
                ctx.use(REG_GRACE)
                grace = P(REG_GRACE, "grace_period_days")
                lapsed = (today - exp).days
                # Penalties apply only if you bid, were listed or worked while lapsed (8 CCR 16412(a)(4)-(5)).
                worked_lapsed = worked_or_bid_while_unregistered or (start <= today and (completion or today) > exp) \
                    or bool(prime and bid_open and exp < bid_open <= today)
                buy(today, today, "re-register")
                reg.fee_due_by = today.isoformat()
                if lapsed <= grace:
                    reg.status = "expired_grace_period"
                    renew_by = exp + timedelta(days=grace)
                    reg.notes.append(f"DIR describes the grace window as {P(REG_GRACE, 'grace_window')}; the due date "
                                     f"here is {grace} days after expiry ({renew_by}), the earlier reading.")
                    if worked_lapsed:
                        reg.penalty_low_usd = float(P(REG_GRACE, "penalty_accidental_lapse_usd"))
                        reg.penalty_high_usd = float(P(REG_GRACE, "penalty_non_accidental_lapse_usd"))
                        ctx.line("Late-renewal penalty (8 CCR 16412(a)(5))", reg.penalty_low_usd, "usd",
                                 f"lapsed {lapsed} days ago (<= {grace}-day grace) and you bid or worked since: "
                                 f"${reg.penalty_low_usd:,.0f} if the lapse was inadvertent, "
                                 f"${reg.penalty_high_usd:,.0f} if not, plus the renewal fee", *sb(REG_GRACE))
                        what = (f"Renew DIR registration now (grace period, retroactive) and pay ${reg.fee_usd:,.0f} "
                                f"fee + ${reg.penalty_low_usd:,.0f} penalty")
                    else:
                        what = (f"Renew DIR registration now and pay ${reg.fee_usd:,.0f}; do not bid or work on public "
                                "works until it posts (doing so adds a "
                                f"${P(REG_GRACE, 'penalty_accidental_lapse_usd'):,.0f}-"
                                f"${P(REG_GRACE, 'penalty_non_accidental_lapse_usd'):,.0f} penalty)")
                    ctx.task(what, you, renew_by, f"within {grace} days after the {exp} expiry", [REG_GRACE, REG_FEE])
                else:
                    reg.status = "expired"
                    if worked_lapsed:
                        ctx.use(REG_PEN, STOP_ORDER)
                        reg.penalty_low_usd = reg.penalty_high_usd = float(P(REG_PEN, "penalty_usd"))
                        ctx.line("Penalty registration fee (8 CCR 16412(a)(4))", reg.penalty_low_usd, "usd",
                                 f"lapsed {lapsed} days (> {grace}-day grace) and you bid or worked since: "
                                 f"${reg.penalty_low_usd:,.0f} on top of the ${reg.fee_usd:,.0f} fee", *sb(REG_PEN))
                        if start <= today:
                            reg.notes.append(stop_order)
                    ctx.task(f"Registration lapsed more than {grace} days ago: register again (${reg.fee_usd:,.0f}"
                             + (f" + ${reg.penalty_low_usd:,.0f} penalty" if reg.penalty_low_usd else "") + ")",
                             you, today, "now; you may not bid, be listed or work until registered", ids)
            elif exp < start:
                reg.status = "expires_before_start"
                buy(exp + timedelta(days=1), exp, "renewal")
                reg.fee_due_by = exp.isoformat()
                ctx.task(f"Renew DIR registration (${reg.fee_usd:,.0f}) before it expires", you, exp,
                         f"by June 30 ({exp}); the job starts {start}", ids)
            elif exp < need_until:
                reg.status = "renew_before_completion"
                buy(exp + timedelta(days=1), exp, "renewal")
                reg.fee_due_by = exp.isoformat()
                ctx.task(f"Renew DIR registration (${reg.fee_usd:,.0f}) before it expires mid-project", you, exp,
                         f"by June 30 ({exp}) to avoid a lapse during the job", ids)
            else:
                reg.status = "ok"
                ctx.task("DIR registration current through the job", you, exp, f"expires {exp}", ids, status="done")
                if not completion:
                    reg.notes.append(f"Checked only through the start date; give estimated_completion_date to see "
                                     f"whether the {exp} expiry falls mid-job.")
        else:
            reg.status = "not_registered"
            buy(today, bid_or_sign if bid_or_sign > today else today, "new registration")
            reg.fee_due_by = bid_or_sign.isoformat()
            if bid_unregistered:
                ctx.use(REG_PEN, UNREG)
                reg.penalty_low_usd = float(P(REG_PEN, "penalty_usd"))
                last = min(today, completion) if completion else today
                days = max(0, (last - start).days + 1)     # calendar days on the job so far (upper bound)
                daily = min(P(UNREG, "per_day_usd") * days, P(UNREG, "max_usd"))
                reg.penalty_high_usd = reg.penalty_low_usd + daily
                ctx.line("Penalty exposure for bidding/working unregistered", reg.penalty_high_usd, "usd",
                         f"${reg.penalty_low_usd:,.0f} penalty registration fee + up to ${P(UNREG, 'per_day_usd'):,.0f}"
                         f"/day of work x {days} calendar days {start} to {last} (cap ${P(UNREG, 'max_usd'):,.0f}) = "
                         f"${reg.penalty_low_usd:,.0f} to ${reg.penalty_high_usd:,.0f}", *sb(REG_PEN, UNREG))
                if start <= today:
                    ctx.use(STOP_ORDER)
                    reg.notes.append(stop_order)
            ctx.task(f"Register with DIR as a public works contractor and pay ${reg.fee_usd:,.0f}", you, bid_or_sign,
                     "before you bid, are listed in a bid, or sign the contract (LC 1771.1)", ids)
            if bid_or_sign >= today:
                ctx.use(REG_AT_AWARD)
                reg.notes.append("Exception: a joint-venture bid (B&P 7029.1) or a bid on a federally funded job under "
                                 "PCC 10164 or 20103.5 may be submitted unregistered if you are registered by the "
                                 f"{P(REG_AT_AWARD, 'register_by')} (LC 1771.1(a)).")
        reg.notes.append(speed)
    elif covered and duty == "small":
        reg.notes.append("Small project: DIR registration is not required (LC 1771.1(n)), but prevailing wages and "
                         "certified payroll records still are.")

    # ---- project registration (PWC-100)
    pwc_due = None
    if reg_required:
        ctx.use(PWC100, PWC100_PEN)
        pwc_days = P(PWC100, "deadline_days_after_award")
        pwc_due = min(award + timedelta(days=pwc_days), start) if award else start
        ctx.task("Awarding body files the PWC-100 project registration; get the DIR project ID (eCPRs cannot be "
                 "filed until the project is registered)", "awarding body (you confirm)", pwc_due,
                 f"within {pwc_days} days after award, no later than the first day workers are on the job",
                 [PWC100])
        ctx.notes.append(f"A missed PWC-100 costs the awarding body ${P(PWC100_PEN, 'per_day_usd'):,.0f}/day up to "
                         f"${P(PWC100_PEN, 'max_per_project_usd'):,.0f}; contractors are not liable and may not be "
                         "made to indemnify it (LC 1773.3(h)). Strike any such clause.")

    # ---- prevailing wage basics, certified payroll and eCPR
    pay = PayrollReporting(certified_payroll_required=pw_on, ecpr_upload_required=False)
    if pw_on:
        ctx.use(RATES, OT, PW_NO_AWS, CPR_REQ, CPR_PEN, CPR_LOC)
        # LC 1776 is amended by SB 909; with no completion date the job may run past its operative date
        cpr_ids = sb(CPR_REQ, CPR_PEN, on=max(today, completion) if completion else max(today, sb_op))
        ctx.task("Get DIR prevailing wage determinations for every craft on the job (from the call for bids, the "
                 "contract, or the awarding body's office) and set pay rates", you, _next(today, bid_open, start),
                 "before you bid (to price labor) and before work starts (to pay it)", [RATES])
        daily, weekly, mult = (P(OT, "daily_hours_threshold"), P(OT, "weekly_hours_threshold"),
                               P(OT, "min_multiplier"))
        pay.overtime_rule = (f"Pay at least {mult:g}x the basic prevailing rate for hours over {daily:g} in a day or "
                             f"{weekly:g} in a week, or the determination's higher overtime rate. A 4/10 or other "
                             "alternative workweek schedule does not avoid daily overtime on public works; pay the "
                             "overtime the wage determination specifies (DLSE; LC 1810-1815).")
        pay.cpr_request_response_days = P(CPR_REQ, "deadline_days")
        pay.cpr_late_penalty_per_worker_per_day_usd = float(P(CPR_PEN, "per_worker_per_day_usd"))
        ctx.task(f"Answer any written request for certified payroll within {pay.cpr_request_response_days} days "
                 f"(late: ${pay.cpr_late_penalty_per_worker_per_day_usd:,.0f} per worker per calendar day)", you, None,
                 f"within {pay.cpr_request_response_days} days after you receive the written request",
                 list(cpr_ids), status="on_request")
        ctx.task("Tell the awarding body the street address, city and county where payroll records are kept", you,
                 start, f"when work starts (the statute sets no first date); any change within "
                 f"{P(CPR_LOC, 'change_notice_working_days')} working days", [CPR_LOC])
        lcp = _legacy_lcp(awarding_body_name) if duty == "full" else None
        if duty == "full" and lcp:
            ctx.use(ECPR_EXEMPT)
            pay.ecpr_exempt_reason = (f"{lcp} runs a legacy labor compliance program: certified payroll goes to its "
                                      "program, not DIR's eCPR system (confirm in the contract).")
        elif duty == "full":
            ctx.use(ECPR, ECPR_PEN)
            interval, final_days = P(ECPR, "max_interval_days"), P(ECPR, "final_submission_within_days_after_last_work")
            grace = P(ECPR_PEN, "grace_days_after_due")
            first = start + timedelta(days=interval)
            pay.ecpr_upload_required = True
            pay.max_days_between_uploads = interval
            pay.first_upload_due = first.isoformat()
            pay.penalty_possible_after = (first + timedelta(days=grace)).isoformat()
            pay.ecpr_late_penalty_per_day_usd = float(P(ECPR_PEN, "per_day_usd"))
            pay.ecpr_late_penalty_max_per_project_usd = float(P(ECPR_PEN, "max_per_project_usd"))
            ctx.task("Upload certified payroll (eCPR) to DIR - weekly recommended, never more than "
                     f"{interval} days apart", you, first, f"first upload within {interval} days of the first day of "
                     f"work ({start}); then at least every {interval} days", [ECPR])
            if completion:
                final = completion + timedelta(days=final_days)
                pay.final_upload_due = final.isoformat()
                ctx.task("Upload the final eCPR and mark it final", you, final,
                         f"within {final_days} days after the last day of work", [ECPR])
            ctx.line("Late eCPR penalty", pay.ecpr_late_penalty_per_day_usd, "usd",
                     f"${pay.ecpr_late_penalty_per_day_usd:,.0f}/day, max "
                     f"${pay.ecpr_late_penalty_max_per_project_usd:,.0f} per project; not levied until {grace} days "
                     "past due", ECPR_PEN)
        else:
            pay.ecpr_exempt_reason = ("Small project: no eCPR upload to DIR (LC 1771.4(a)(4)), but keep weekly "
                                      "certified payroll and produce it on request.")
        ctx.line("Late certified payroll after a written request (LC 1776(h))",
                 pay.cpr_late_penalty_per_worker_per_day_usd, "usd",
                 f"${pay.cpr_late_penalty_per_worker_per_day_usd:,.0f} x each worker x each calendar day after the "
                 f"{pay.cpr_request_response_days}-day deadline", *cpr_ids)

    # ---- apprenticeship
    if apprentice:
        ctx.use(APP_MIN, DAS140, DAS142, RATIO, TRAIN, HOURS_STMT, APP_PRIME)
        d140 = P(DAS140, "deadline_days_after_contract_execution")
        das140 = min(executed + timedelta(days=d140), start) if executed else start
        hours = P(DAS142, "advance_notice_hours")
        das142, cutoff142 = _das142_send_by(start, hours)
        tday = P(TRAIN, "due_day_of_following_month")
        train = _plus_months(start.replace(day=1), 1).replace(day=tday)
        stmt = completion + timedelta(days=P(HOURS_STMT, "deadline_days_after_work_concluded")) if completion else None
        crafts = []
        by_craft = journeyman_hours_by_craft or {}
        if any(jh < 0 for jh in by_craft.values()):
            raise ValueError("journeyman hours per craft must be 0 or more")
        total_jh = float(sum(by_craft.values()))      # your journeyman hours on the project, all crafts
        for craft, jh in by_craft.items():
            req, minimum, ratio, m = _apprentice_calc(float(jh), total_jh)
            ctx.use(DAS142_SIZE)
            crafts.append(CraftHours(craft=craft, journeyman_hours=float(jh), required_apprentice_hours=req,
                                     das142_min_request_hours=minimum, math=m))
            ctx.line(f"Apprentice hours required: {craft}", req, "hours", m, RATIO, DAS142_SIZE)
        appr = Apprenticeship(applies=True, ratio=P(RATIO, "ratio_apprentice_to_journeyman_hours"),
                              reason=f"Prime contract ${value:,.0f} >= ${app_min:,.0f}; every contractor and "
                                     "subcontractor employing an apprenticeable craft must comply, even on "
                                     "subcontracts under the threshold.",
                              das140_due=das140.isoformat(), das142_request_by=das142.isoformat(),
                              training_fund_first_due=train.isoformat(), hours_statement_due=_iso(stmt), crafts=crafts)
        ctx.task("Send DAS 140 (contract award information) to the apprenticeship committee(s) for each apprenticeable "
                 "craft - not to DAS", you, das140, f"within {d140} days after the contract is signed, no later than "
                 "the first day workers are on the job", [DAS140])
        ctx.task("Send DAS 142 apprentice dispatch request(s) to each committee for the craft (mail, fax or email; "
                 "keep proof)", you, das142, f"at least {hours:g} hours before the date apprentices are needed, not "
                 f"counting Saturdays, Sundays or holidays: counted back from 12:00 a.m. on {start}, the {hours:g} "
                 f"hours start at 12:00 a.m. on {cutoff142}, so send it by the end of {das142}. Holidays are not "
                 "checked here; send a day earlier for each holiday in the window", [DAS142])
        ctx.task("Pay apprenticeship training fund contributions for every hour worked in apprenticeable crafts (CAC-2 "
                 "or approved program)", you, train, f"by the {tday}th of each month for the prior month's work",
                 [TRAIN])
        if stmt:
            ctx.task("Send the verified statement of journeyman and apprentice hours to the apprenticeship program",
                     you, stmt, f"within {P(HOURS_STMT, 'deadline_days_after_work_concluded')} days after your work "
                     "ends", [HOURS_STMT])
        if prime:
            ctx.notes.append("The contract makes the prime responsible for apprenticeship compliance of all "
                             "apprenticeable crafts (LC 1777.5(n)); collect each sub's DAS 140/142 copies.")
        if not crafts:
            ctx.notes.append("Give journeyman_hours_by_craft (or use apprentice_hours_required) to get the 1:5 "
                             "apprentice hours per craft.")
    else:
        if not covered or duty == "none":
            why = "Prevailing wage law does not apply, so neither does apprenticeship."
        elif duty == "small" or value < app_min:
            ctx.use(APP_MIN, TRAIN)
            why = (f"Prime contract ${value:,.0f} < ${app_min:,.0f}: LC 1777.5 apprenticeship duties do not apply. "
                   "The training amount on the wage determination is still owed: pay it to each worker as part of "
                   "the prevailing wage, not to the apprenticeship council (DAS FAQ).")
        else:
            ctx.use(APP_MIN)
            why = "No employees on the job, so no apprentices are required (a sole owner working alone)."
        appr = Apprenticeship(applies=False, reason=why)

    # ---- workers' comp certification, postings, subcontract clauses
    postings: list[str] = []
    if pw_on:
        ctx.use(POSTINGS)
        postings = list(P(POSTINGS, "postings"))
        ctx.task("Post at the job site: " + "; ".join(postings),
                 "awarding body, or you (prime) if the contract says so" if prime else
                 "awarding body or the prime (confirm the posting is up before your crew starts)",
                 start, "before work starts", [POSTINGS])
        if prime or subs:
            ctx.use(SAFE_HARBOR)
            secs = ", ".join(P(SAFE_HARBOR, "sections_to_attach"))
            ctx.task(f"Put a copy of Labor Code {secs} in every subcontract (if you sub out any work)", you, start,
                     "before each subcontract is signed", [SAFE_HARBOR])
            ctx.task("Review subs' certified payroll as it comes in; before final payment get each sub's affidavit "
                     "(penalty of perjury) that prevailing wages and LC 1813 amounts were paid", you, None,
                     "periodically, and before each sub's final payment", [SAFE_HARBOR], status="recurring")

    if covered and prime:                        # LC 1861 applies to every public works contract, any amount
        ctx.use(WC_CERT)
        ctx.task("Sign and file the LC 1861 workers' compensation certification with the awarding body", you,
                 start, "before performing work under the contract", [WC_CERT])

    # ---- subcontractors: DIR registration and PCC 4104 listing
    public_body = awarding_body_type in PUBLIC_BODIES
    listing = SubListing(applies=prime and public_body and covered)
    threshold = None
    if listing.applies:
        ctx.use(LISTING, SUBLET_PEN)
        threshold, listing.threshold_math = _listing_threshold(bid, work_type)
        listing.prime_bid_usd, listing.threshold_usd = bid, threshold
        ctx.line("Subcontractor listing threshold (PCC 4104)", threshold, "usd", listing.threshold_math, LISTING)
        if sum(s.bid_amount_usd for s in subs) > bid:
            listing.notes.append("Sub amounts add up to more than the prime bid; check the figures.")
    elif not prime and public_body and covered:
        listing.notes.append("The prime lists subcontractors in its bid. If your subcontract is over the prime's "
                             "listing threshold, make sure the prime listed you with your license and DIR numbers.")
        if your_contract_usd is not None:
            ctx.use(LISTING)
            t, m = _listing_threshold(bid, work_type)
            listing.prime_bid_usd, listing.threshold_usd, listing.threshold_math = bid, t, m
            listed = your_contract_usd > t
            ctx.line("Must the prime list you? (PCC 4104)", t, "usd",
                     f"{m}; your ${your_contract_usd:,.2f} {'>' if listed else '<='} ${t:,.2f} -> "
                     + ("the prime must list you" if listed else "listing not required"), LISTING)
    pct_sub = P(SUBLET_PEN, "max_penalty_pct_of_subcontract") if listing.applies else None
    for s in subs:
        problems: list[str] = []
        chk = SubCheck(name=s.name, bid_amount_usd=s.bid_amount_usd, dir_registration_needed=reg_required, ok=True)
        if listing.applies:
            chk.pct_of_prime_bid = round(s.bid_amount_usd / bid * 100, 3) if bid else None
            chk.must_list = s.bid_amount_usd > threshold
            if chk.must_list:
                chk.listing_violation_penalty_max_usd = _money(s.bid_amount_usd * pct_sub / 100)
                if not s.license_number:
                    ctx.use(LIST_24H, SUB_INFO)
                    hours = P(LIST_24H, "license_or_pwcr_correction_hours_after_bid_opening")
                    problems.append("No CSLB license number: the bid list needs it (an inadvertent error can be "
                                    f"corrected within {hours} hours after bid opening).")
                if not s.portion_of_work:
                    ctx.use(SUB_INFO)
                    problems.append("Name the portion of work this sub will do (list one sub per portion).")
        if reg_required:
            ctx.use(REG_PROOF, UNREG_SUB)
            sub_exp = s.dir_registration_expiry
            if not s.dir_number:
                if chk.must_list:
                    ctx.use(LIST_CURE, REG_PEN)
                problems.append("No DIR registration number: you may not list this sub or sign its subcontract "
                                "until it is registered"
                                + (f" (an inadvertently listed unregistered sub can still cure within "
                                   f"{P(LIST_CURE, 'cure_window_hours_after_bid_opening')} hours after bid opening by "
                                   f"registering and paying the ${P(REG_PEN, 'penalty_usd'):,.0f} penalty registration "
                                   "fee, or you substitute a registered sub under PCC 4107)"
                                   if chk.must_list else "")
                                + f"; using it costs you ${P(UNREG_SUB, 'per_day_usd'):,.0f}/day up to "
                                  f"${P(UNREG_SUB, 'max_usd'):,.0f}.")
            elif sub_exp and sub_exp < need_until:
                problems.append(f"DIR registration expires {sub_exp}, before the job ends; get proof of renewal.")
        chk.problems, chk.ok = problems, not problems
        listing.subs.append(chk)
    if subs and reg_required:
        bad = [c.name for c in listing.subs if not c.ok]
        ctx.task("Verify every subcontractor's DIR registration (DIR contractor search) and CSLB license before "
                 "listing or signing" + (f" - open problems: {', '.join(bad)}" if bad else ""), you,
                 _next(today, bid_open, executed, start),
                 "before the bid is submitted and before each subcontract is signed", [REG_PROOF],
                 licensed_human="each subcontractor must hold a CSLB license in the right classification")
    must = [c for c in listing.subs if c.must_list]
    if listing.applies and bid_open and bid_open >= today:
        ctx.use(LIST_24H)
        ctx.task(f"List in the bid every sub over ${threshold:,.2f}: name, place of business, CSLB license number, DIR "
                 f"registration number and portion of work ({len(must)} of {len(subs)} entered subs)", you, bid_open,
                 "with the bid", [LISTING, LIST_24H, SUB_INFO])
    if listing.applies and bid_open and must:
        h = P(LIST_24H, "license_or_pwcr_correction_hours_after_bid_opening")
        fix = bid_open + timedelta(days=math.ceil(h / 24))
        if fix >= today or any(not c.ok for c in must):
            ctx.use(LIST_24H)
            ctx.task("Correct any inadvertent license or DIR-number error in the sub list", you, fix,
                     f"within {h} hours after bid opening", [LIST_24H])
    if listing.applies and any(c.must_list for c in listing.subs):
        listing.notes.append(f"Unlisted or improperly substituted subs over the threshold: the awarding authority may "
                             f"cancel the contract or charge up to {pct_sub:g}% of the subcontract (PCC 4110-4111); "
                             "substitutions need its consent (PCC 4107).")

    # ---- retention and final payment
    ret = Retention(applies=public_body and covered)
    if ret.applies:
        ctx.use(RET_CAP, RET_RELEASE)
        cap = P(RET_CAP, "max_retention_percent")
        base = value if prime else your_contract_usd       # the cap is on YOUR contract price
        ret.max_retention_pct = float(cap)
        ret.release_days_after_completion = P(RET_RELEASE, "deadline_days")
        ret.pass_through_days_after_receipt = P(RET_RELEASE, "prime_to_sub_passthrough_days_per_companion_research")
        ret.disputed_withhold_max_pct = float(P(RET_RELEASE, "max_withhold_percent_of_disputed"))
        if base is not None:
            ret.max_total_retention_usd = _money(base * cap / 100)
            ctx.line("Maximum retention (PCC 7201)", ret.max_total_retention_usd, "usd",
                     f"{cap:g}% x ${base:,.2f} {'prime contract' if prime else 'subcontract'} = "
                     f"${ret.max_total_retention_usd:,.2f} total; also no more than {cap:g}% of any single payment, "
                     "and sub retention may not exceed the prime's percentage", RET_CAP)
        else:
            ret.notes.append(f"Give your_contract_usd (your subcontract price) to get your retention cap: {cap:g}% of "
                             "each payment and of your subcontract price, never more than the prime's percentage.")
        ret.notes.append("Exceptions: a pre-bid 'substantially complex' finding stated in the bid documents, or a "
                         "sub that will not furnish bonds after written notice. No one may require a waiver.")
        if completion and prime:
            rel = completion + timedelta(days=ret.release_days_after_completion)
            ret.release_due = rel.isoformat()
            ctx.task("Awarding body releases retention to you (up to "
                     f"{ret.disputed_withhold_max_pct:g}% of disputed amounts may be held)", "awarding body",
                     rel, f"within {ret.release_days_after_completion} days after {P(RET_RELEASE, 'trigger')} "
                     f"(estimated {completion})", [RET_RELEASE])
        elif not prime:
            ret.notes.append(f"The awarding body releases retention to the prime within "
                             f"{ret.release_days_after_completion} days after {P(RET_RELEASE, 'trigger')} (the whole "
                             "project, not your scope); the prime then has "
                             f"{ret.pass_through_days_after_receipt} days to pay your share.")
        ctx.task("Pass retention through to subs", "you (prime)" if prime else "the prime (to you)", None,
                 f"within {ret.pass_through_days_after_receipt} days after the prime receives it", [RET_RELEASE],
                 status="recurring")
        if awarding_body_type == "state_agency":
            ctx.use(RET_STATE)
            ret.notes.append("State agencies: one reading of PCC 7107(c) allows "
                             f"{P(RET_STATE, 'state_agency_alternative')}. Not verified; do not treat a state "
                             "agency as late before checking the statute.")
        if reg_required:
            ctx.use(FINAL_HOLD)
            ret.final_payment_hold_days_after_sub_list = P(FINAL_HOLD, "withhold_final_payment_min_days_after_info")
            ret.notes.append(f"Final payment is held until at least {ret.final_payment_hold_days_after_sub_list} days "
                             "after the complete subcontractor list is in DIR's system (LC 1773.3(d)); add every "
                             "sub to the DIR project early.")
    elif awarding_body_type == "private_developer":
        ctx.use(PRIV_RET)
        ret.notes.append("Private-owner job: PCC 7107/7201 do not apply; private-works retention rules do. For "
                         f"contracts entered on or after {P(PRIV_RET, 'applies_to_contracts_entered_on_or_after')}, "
                         f"retention may not exceed {P(PRIV_RET, 'max_retention_percent_of_each_payment'):g}% of any "
                         f"payment or {P(PRIV_RET, 'max_total_retention_percent_of_contract_price'):g}% of the contract "
                         "price (Civil Code 8811).")

    # ---- record retention
    rr = RecordRetention()
    if covered:
        ctx.use(CSLB_KEEP, CPR_KEEP, CWPA)
        yrs = P(CSLB_KEEP, "retention_years_min")
        if completion:
            rr.keep_until = _plus_years(completion, yrs).isoformat()
        rr.items.append(f"Contracts, certified payroll, time cards, receipts and disbursements: at least {yrs} years "
                        "after the project is completed (B&P 7111)" + (f" -> {rr.keep_until}" if rr.keep_until else ""))
        rr.items.append(f"Wage assessments can be served up to {P(CWPA, 'months')} months after the later of the "
                        "notice of completion or acceptance (LC 1741), longer if requested payroll is late.")
        if duty == "small":
            rr.items.append(f"Small project: keep LC 1776 payroll records at least {P(SMALL, 'record_retention_years')}"
                            " years after completion (LC 1771.4(a)(4)).")
        if completion:
            ctx.task("Keep all project and payroll records", you, _plus_years(completion, yrs),
                     f"at least {yrs} years after completion", [CSLB_KEEP])

    if subs and duty == "full":
        ctx.notes.append("Each subcontractor has its own DIR registration, weekly certified payroll / eCPR, DAS "
                         "140/142 and training fund duties for its workers; collect proof as the job runs.")

    # ---- notes that change with law in flux
    if reg_required or pw_on:
        _sb909(ctx, "LC 1776 records penalty and LC 1725.5 registration fees", SB909 in ctx.cited)
    ab_op = _d(P(AB1198, "operative"), "AB 1198 operative")
    if covered and value >= P(AB1198, "prime_contract_min_usd") and not (award and award < ab_op):
        ctx.use(AB1198)
        big = P(AB1198, "prime_contract_min_usd")
        ctx.notes.append(f"Reported but unverified ({P(AB1198, 'bill')}): from {P(AB1198, 'operative')}, prevailing "
                         f"wage changes apply mid-project on prime contracts of ${big:,.0f}"
                         " or more advertised or awarded after that date. Price wage escalation into the bid.")
    if not pw_on and covered:
        ctx.notes.append("No prevailing-wage duties at this contract amount.")

    ctx.tasks.sort(key=lambda t: (t.due is None, t.due or ""))
    overdue = sum(t.status == "overdue" for t in ctx.tasks)
    soon = sum(t.status == "due_soon" for t in ctx.tasks)
    if not covered:
        head = "Not a public work on these facts: no prevailing wage duties."
    elif duty == "none":
        head = f"Public work of ${value:,.0f} (${pw_min:,.0f} or less): prevailing wage not required."
    else:
        head = ("Prevailing wage applies" if public_work else "Coverage uncertain; planned as a public work") + (
            ": small project (no DIR registration, eCPR or PWC-100; certified payroll still kept)." if duty == "small"
            else ": full compliance (DIR registration, PWC-100, eCPR" + (", apprenticeship)." if apprentice else ")."))
    summary = f"{head} {overdue} checklist item(s) overdue, {soon} due within {DUE_SOON_DAYS} days."

    res = ReadinessResult(as_of=today.isoformat(), your_role=your_role, summary=summary, coverage=coverage,
                          dir_registration=reg, pwc100_required=reg_required, pwc100_due=_iso(pwc_due), payroll=pay,
                          apprenticeship=appr, subcontractor_listing=listing, retention=ret, record_retention=rr,
                          jobsite_postings=postings, checklist=ctx.tasks, line_items=ctx.lines, notes=ctx.notes)
    res.cite(*sorted(ctx.cited))
    res.unverified_rule_ids = sorted(r for r in ctx.cited if not core.rule(r).verified)
    if res.unverified_rule_ids:
        res.notes.append("verified=false because the result relies on rules not checked against official text: "
                         + ", ".join(res.unverified_rule_ids) + ". Line items and checklist entries carry their own "
                         "verified flag.")
    return res


# ---------------------------------------------------------------- apprentice hours
def apprentice_hours_required(journeyman_hours: float, excluded_overtime_hours: float = 0,
                              apprentice_hours_worked: Optional[float] = None, craft: Optional[str] = None,
                              prime_contract_usd: Optional[float] = None,
                              project_journeyman_hours: Optional[float] = None, as_of=None) -> ApprenticeHoursResult:
    if any(x is not None and x < 0 for x in (journeyman_hours, excluded_overtime_hours, apprentice_hours_worked,
                                             project_journeyman_hours, prime_contract_usd)):
        raise ValueError("hours and amounts must be 0 or more")
    if excluded_overtime_hours > journeyman_hours:
        raise ValueError("excluded_overtime_hours cannot exceed journeyman_hours")
    ctx = _Ctx(_d(as_of, "as_of") or logic.today_ca())
    counted = _money(journeyman_hours - excluded_overtime_hours)
    ex = P(RATIO, "exclude_journeyman_hours_over")
    if excluded_overtime_hours:
        ctx.line("Journeyman hours counted", counted, "hours",
                 f"{journeyman_hours:,.2f} total - {excluded_overtime_hours:,.2f} hours over {ex['daily']:g}/day or "
                 f"{ex['weekly']:g}/week = {counted:,.2f} straight-time hours", RATIO)
    # the under-40-hour exemption is per project; without the project total, the craft's own hours stand in
    project = journeyman_hours if project_journeyman_hours is None else max(project_journeyman_hours, journeyman_hours)
    required, minimum, ratio, m = _apprentice_calc(counted, project)
    ctx.line("Apprentice hours required", required, "hours", m, RATIO, DAS142_SIZE)
    shortfall = None
    if apprentice_hours_worked is not None:
        ctx.use(APP_REGISTERED)
        shortfall = _money(max(0.0, required - apprentice_hours_worked))
        ctx.line("Shortfall", shortfall, "hours",
                 f"max(0, {required:,.2f} required - {apprentice_hours_worked:,.2f} worked) = {shortfall:,.2f}; only "
                 "hours of apprentices registered in a DAS-approved program count", RATIO, APP_REGISTERED)
        ctx.notes.append("Only apprentices in a DAS-approved program with a written apprentice agreement count (LC "
                         "1777.5(c)); anyone else doing the craft's work is paid the journeyman rate. Check each one "
                         "in the DAS registered-apprentice search.")
    applies = None
    if prime_contract_usd is not None:
        app_min = P(APP_MIN, "threshold_usd")
        applies = prime_contract_usd >= app_min
        ctx.line("Apprenticeship threshold", prime_contract_usd, "usd",
                 f"prime contract ${prime_contract_usd:,.2f} {'>=' if applies else '<'} ${app_min:,.0f} -> ratio "
                 f"{'required' if applies else 'not required'}", APP_MIN)
    if minimum is None and project_journeyman_hours is None and journeyman_hours > 0:
        ctx.notes.append(f"The no-minimum-request exemption is for PROJECTS under "
                         f"{P(DAS142_SIZE, 'exempt_if_journeyman_hours_below'):g} journeyman hours; give "
                         "project_journeyman_hours (all crafts) if other crafts work on the job.")
    ctx.use(DAS142)
    ctx.notes += [
        f"Measured per craft at the end of your contract or subcontract; crafts cannot be combined, and journeyman "
        f"hours over {ex['daily']:g} a day or {ex['weekly']:g} a week do not count. Schedule apprentices while "
        "journeymen are on site.",
        f"Short on apprentices? Send written DAS 142 requests ({P(DAS142, 'advance_notice_hours'):g} hours ahead, not "
        "counting weekends and holidays) to the committees for the craft, and to every other committee in the area "
        "if one does not dispatch (8 CCR 230.1(a)). Keep proof of sending.",
    ]
    res = ApprenticeHoursResult(craft=craft, journeyman_hours=journeyman_hours,
                                excluded_overtime_hours=excluded_overtime_hours, counted_journeyman_hours=counted,
                                ratio=ratio, required_apprentice_hours=required,
                                apprentice_hours_worked=apprentice_hours_worked, shortfall_hours=shortfall,
                                das142_min_request_hours=minimum, applies=applies, line_items=ctx.lines,
                                notes=ctx.notes)
    res.cite(*sorted(ctx.cited))
    res.unverified_rule_ids = sorted(r for r in ctx.cited if not core.rule(r).verified)
    return res


# ---------------------------------------------------------------- penalties
_MIN_KEY = {"good_faith_corrected": "good_faith_corrected_min_usd", "standard": "min_per_worker_per_day_usd",
            "prior_violation_3_years": "min_if_prior_penalties_3yrs_usd", "willful": "min_if_willful_usd"}


def prevailing_wage_penalties(underpaid_worker_days: int = 0,
                              underpayment_category: UnderpaymentCategory = "standard",
                              unpaid_wages_usd: float = 0, unpaid_60_days_after_assessment: bool = False,
                              overtime_violation_worker_days: int = 0, cpr_workers: int = 0,
                              cpr_request_received_date=None, cpr_produced_date=None,
                              cpr_days_late: Optional[int] = None, ecpr_days_late: int = 0,
                              unregistered_work_days: int = 0, unregistered_sub_work_days: int = 0,
                              apprenticeship_noncompliance_days: int = 0,
                              apprenticeship_repeat_within_3_years: bool = False, as_of=None) -> PenaltyResult:
    today = _d(as_of, "as_of") or logic.today_ca()
    counts = [underpaid_worker_days, unpaid_wages_usd, overtime_violation_worker_days, cpr_workers, ecpr_days_late,
              unregistered_work_days, unregistered_sub_work_days, apprenticeship_noncompliance_days, cpr_days_late or 0]
    if any(c < 0 for c in counts):
        raise ValueError("counts, days and amounts must be 0 or more")
    if underpayment_category not in _MIN_KEY:
        raise ValueError(f"underpayment_category must be one of {sorted(_MIN_KEY)}")
    ctx = _Ctx(today)
    lines: list[PenaltyLine] = []
    debar: list[str] = []
    sb_op = _sb909_operative()

    def sb(*ids: str, on: Optional[date] = None) -> tuple[str, ...]:
        """SB 909 amends LC 1725.5, 1775, 1776 and 1777.7: those lines cite it from its assumed operative date."""
        return ids + ((SB909,) if (on or today) >= sb_op else ())

    def add(item, section, low, high, math_, *ids):
        lines.append(PenaltyLine(item=item, section=section, low_usd=_money(low), high_usd=_money(high), math=math_,
                                 rule_ids=ctx.use(*ids), verified=_ok(ids)))

    if underpaid_worker_days:
        mx, mn = P(PEN_1775, "max_per_worker_per_day_usd"), P(PEN_1775, _MIN_KEY[underpayment_category])
        n = underpaid_worker_days
        add("Prevailing wage underpayment penalty", "LC 1775(a)", mn * n, mx * n,
            f"{n} worker-days x ${mn:,.0f} minimum ({underpayment_category.replace('_', ' ')}) to ${mx:,.0f} maximum "
            f"= ${mn * n:,.0f} to ${mx * n:,.0f}", *sb(PEN_1775))
        if underpayment_category == "willful":
            ctx.use(DEBAR_WILLFUL)
            debar.append(f"Willful violations: {P(DEBAR_WILLFUL, 'willful_count')} or more separate willful violations "
                         f"within {P(DEBAR_WILLFUL, 'willful_window_years')} years can bar you from public works for "
                         f"up to {P(DEBAR_WILLFUL, 'willful_max_years')} years (LC 1777.1).")
    if unpaid_wages_usd:
        ctx.use(PEN_1775, LIQ, CWPA_REVIEW)
        ctx.notes.append("Back wages go to the workers first, before any penalty is paid (LC 1775).")
        ld_days = P(LIQ, "days_after_service")
        if not unpaid_60_days_after_assessment:
            ctx.notes.append(f"If the ${unpaid_wages_usd:,.2f} is still unpaid {ld_days} days after a wage assessment "
                             f"is served, liquidated damages equal to it are added (LC 1742.1). Avoid that by paying "
                             f"or depositing the full assessment with DIR within {ld_days} days.")
    if overtime_violation_worker_days:
        fixed, n = P(PEN_1813, "per_worker_per_day_usd"), overtime_violation_worker_days
        add("Overtime violation penalty (fixed, not waivable)", "LC 1813", fixed * n, fixed * n,
            f"{n} worker-days x ${fixed:,.0f} = ${fixed * n:,.0f}", PEN_1813)

    if cpr_request_received_date or cpr_days_late is not None:
        ctx.use(CPR_REQ, CPR_PEN, CPR_DEBAR)
        window = P(CPR_REQ, "deadline_days")
        if cpr_request_received_date:
            got = _d(cpr_request_received_date, "cpr_request_received_date")
            deadline = got + timedelta(days=window)
            produced = _d(cpr_produced_date, "cpr_produced_date")
            end = produced or today
            days = max(0, (end - deadline).days)
            status = f"produced {produced}" if produced else f"not produced as of {today}"
            how = f"request received {got}, due {deadline} ({window} days); {status} -> {days} days late"
        else:
            days, how, end = cpr_days_late, f"{cpr_days_late} days past the {window}-day deadline", today
        rate = P(CPR_PEN, "per_worker_per_day_usd")
        amt = rate * days * cpr_workers
        if not cpr_workers:
            ctx.notes.append("Give cpr_workers (workers on the requested records) to compute the LC 1776(h) amount.")
        add("Late certified payroll after a written request", "LC 1776(h)", amt, amt,
            f"{how}; ${rate:,.0f} x {cpr_workers} workers x {days} days = ${amt:,.0f}", *sb(CPR_PEN, CPR_REQ, on=end))
        if days:
            debar.append(f"Records still missing {P(CPR_DEBAR, 'cure_days_after_notice')} days after the Labor "
                         f"Commissioner's debarment notice: debarment for {P(CPR_DEBAR, 'debarment_min_years')}-"
                         f"{P(CPR_DEBAR, 'debarment_max_years')} years (LC 1777.1(c)).")
        ctx.notes.append("A prime is not penalized under LC 1776(h) for its subcontractor's late records.")

    if ecpr_days_late:
        rate, cap, grace = (P(ECPR_PEN, "per_day_usd"), P(ECPR_PEN, "max_per_project_usd"),
                            P(ECPR_PEN, "grace_days_after_due"))
        n = ecpr_days_late
        low = min(rate * max(0, n - grace), cap) if n > grace else 0
        high = min(rate * n, cap) if n > grace else 0
        add("Late eCPR upload to DIR", "LC 1771.4(a)(3)", low, high,
            f"{n} days late; no penalty until {grace} days past due"
            + (f"; ${rate:,.0f}/day x {n - grace} days after the grace (${low:,.0f}) to x {n} days from the due date "
               f"(${high:,.0f}), cap ${cap:,.0f} per project" if n > grace else ""), ECPR_PEN)
        if n > grace:
            ctx.notes.append(f"The statute does not say whether the {grace} grace days count once the penalty is "
                             "levied; the range shows both readings.")

    if unregistered_work_days:
        rate, cap, fee = P(UNREG, "per_day_usd"), P(UNREG, "max_usd"), P(REG_PEN, "penalty_usd")
        n = unregistered_work_days
        amt = min(rate * n, cap)
        add("Working on public works while unregistered", "LC 1771.1(g)", amt, amt,
            f"${rate:,.0f} x {n} days of work = ${rate * n:,.0f}, cap ${cap:,.0f} -> ${amt:,.0f}", UNREG)
        add("Penalty registration fee to register after working unregistered", "LC 1725.5; 8 CCR 16412(a)(4)",
            fee, fee, f"${fee:,.0f} on top of the ${P(REG_PEN, 'in_addition_to_fee_usd'):,.0f} annual fee",
            *sb(REG_PEN))
    if unregistered_sub_work_days:
        rate, cap, n = P(UNREG_SUB, "per_day_usd"), P(UNREG_SUB, "max_usd"), unregistered_sub_work_days
        amt = min(rate * n, cap)
        add("Using an unregistered subcontractor (upper tier pays; no indemnity from the sub)", "LC 1771.1(h)",
            amt, amt, f"${rate:,.0f} x {n} days the sub worked = ${rate * n:,.0f}, cap ${cap:,.0f} -> ${amt:,.0f}",
            UNREG_SUB)
        ctx.notes.append(f"LC 1771.1(h) can be waived if the violation was {P(UNREG_SUB, 'waivable')}. There is no "
                         "liability if the sub's registration was later revoked.")
    if apprenticeship_noncompliance_days:
        key = "max_per_day_repeat_usd" if apprenticeship_repeat_within_3_years else "max_per_day_usd"
        rate, n = P(PEN_1777_7, key), apprenticeship_noncompliance_days
        add("Apprenticeship violation (DAS 140, DAS 142/ratio, training funds)", "LC 1777.7", 0, rate * n,
            f"up to ${rate:,.0f} x {n} full calendar days = ${rate * n:,.0f}; only for knowing violations and "
            "reducible if disproportionate, so the low end is $0", *sb(PEN_1777_7))
        if apprenticeship_repeat_within_3_years:
            ctx.notes.append(f"The ${rate:,.0f}/day tier needs a knowing repeat violation within "
                             f"{P(PEN_1777_7, 'repeat_window_years')} years that left apprentices untrained.")
        ctx.use(DEBAR_APP)
        debar.append(f"Knowing serious apprenticeship violations: debarment up to "
                     f"{P(DEBAR_APP, 'first_violation_max_years')} year (first) or "
                     f"{P(DEBAR_APP, 'subsequent_max_years')} years (later) (LC 1777.1(d)).")

    pen_low, pen_high = _money(sum(l.low_usd for l in lines)), _money(sum(l.high_usd for l in lines))
    ld = _money(unpaid_wages_usd) if unpaid_60_days_after_assessment else 0.0
    if ld:
        ctx.use(LIQ)
    if lines or unpaid_wages_usd:
        ctx.use(CWPA_REVIEW)
        ctx.notes.append(f"Served with a Civil Wage and Penalty Assessment? Request review in writing within "
                         f"{P(CWPA_REVIEW, 'request_review_within_days')} days of service or it becomes final; have "
                         "a public works attorney file it.")
    if lines or unpaid_wages_usd:
        _sb909(ctx, "LC 1775, 1776, 1777.7 and 1725.5 amounts", SB909 in ctx.cited)
    res = PenaltyResult(as_of=today.isoformat(), lines=lines, penalties_low_usd=pen_low, penalties_high_usd=pen_high,
                        back_wages_usd=_money(unpaid_wages_usd), liquidated_damages_usd=ld,
                        total_low_usd=_money(pen_low + unpaid_wages_usd + ld),
                        total_high_usd=_money(pen_high + unpaid_wages_usd + ld), debarment_risk=debar, notes=ctx.notes)
    if not ctx.cited:
        ctx.use(PEN_1775)
        res.notes.append("No violations entered, so no penalty was computed.")
    res.cite(*sorted(ctx.cited))
    res.unverified_rule_ids = sorted(r for r in ctx.cited if not core.rule(r).verified)
    return res


# ---------------------------------------------------------------- MCP tools
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="public_works_readiness", annotations=ro)
    def _public_works_readiness(
        project_value_usd: Annotated[float, Field(ge=0, description=(
            "Prime contract amount (awarding body to prime) - thresholds are measured on this even if you are a sub. "
            "For a private development with public money, the total project cost"))],
        awarding_body_type: Annotated[AwardingBody, Field(description=(
            "Who awards/pays: state_agency, city, county, school_district, community_college_district, "
            "special_district, uc_csu, joint_powers_authority, private_developer (private owner using public money "
            "or subsidies), federal_agency"))],
        is_public_funds: Annotated[bool, Field(description=(
            "True if paid in whole or part with public money or subsidies (cash, below-market land or loans, waived "
            "fees, credits). For federal_agency: true only if state or local money is involved"))],
        work_type: Annotated[WorkType, Field(description=(
            "construction, alteration, demolition, installation, repair, maintenance (routine upkeep: lower small-"
            "project limit), or street_highway_bridge (construction of streets/highways/bridges: different sub "
            "listing threshold)"))],
        start_date: Annotated[str, Field(description="ISO date workers will first be on the job")],
        contractor_dir_registered: Annotated[bool, Field(description=(
            "Are you currently registered with DIR as a public works contractor?"))],
        dir_registration_expiry: Annotated[Optional[str], Field(description=(
            "ISO date your DIR registration expires (always a June 30)"))] = None,
        subcontractors: Annotated[Optional[list[Subcontractor]], Field(description=(
            "Your subcontractors: [{name, license_number, dir_number, bid_amount_usd, portion_of_work, "
            "dir_registration_expiry}]"))] = None,
        prime_bid_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Prime's total bid for the listing threshold; defaults to project_value_usd"))] = None,
        as_of: Annotated[Optional[str], Field(description=(
            "ISO date to evaluate as of; default today in California"))] = None,
        your_role: Annotated[Role, Field(description="prime or subcontractor")] = "prime",
        award_date: Annotated[Optional[str], Field(description=(
            "ISO date the contract was awarded (PWC-100 clock)"))] = None,
        contract_execution_date: Annotated[Optional[str], Field(description=(
            "ISO date your contract/subcontract was signed (DAS 140 clock)"))] = None,
        bid_opening_date: Annotated[Optional[str], Field(description=(
            "ISO bid opening date (past or upcoming); drives bid-stage deadlines"))] = None,
        estimated_completion_date: Annotated[Optional[str], Field(description=(
            "ISO date your work / the project completes"))] = None,
        has_employees: Annotated[bool, Field(description=(
            "False if you are a sole owner working alone (no apprentices needed)"))] = True,
        journeyman_hours_by_craft: Annotated[Optional[dict[str, float]], Field(description=(
            "Estimated straight-time journeyman hours per apprenticeable craft, e.g. {'electrician': 1250}"))] = None,
        public_subsidy_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Private development only: total public subsidy, for the de minimis test"))] = None,
        all_single_family: Annotated[bool, Field(description=(
            "Private development consisting entirely of single-family homes"))] = False,
        awarding_body_name: Annotated[Optional[str], Field(description=(
            "Agency name, e.g. 'Caltrans' (detects legacy labor compliance programs)"))] = None,
        worked_or_bid_while_unregistered: Annotated[bool, Field(description=(
            "True if you bid, were listed or worked on public works unregistered in the last 12 months"))] = False,
        your_contract_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Subcontractor role: your subcontract price (for your 5% retention cap and whether the prime must list "
            "you). Ignored for the prime"))] = None,
    ) -> ReadinessResult:
        """California public works readiness check (Labor Code 1720-1861, 8 CCR 16000+/230, PCC 4104, 7107, 7201).
        Returns: coverage (is it a public work; prevailing wage over $1,000; small-project exemption at $25,000
        construction / $15,000 maintenance measured on the prime contract; apprenticeship at $30,000+), DIR contractor
        registration status with fee (per fiscal year, 1-3 years) and lapse/unregistered penalties, PWC-100 due date,
        certified payroll and eCPR upload dates (weekly recommended, max 30 days apart, final within 30 days) with
        late penalties ($/day eCPR; $/worker/day after a 10-day records request), apprenticeship (DAS 140 and DAS 142
        dates, 1:5 apprentice hours per craft, training fund due date, hours statement), PCC 4104 subcontractor
        listing threshold and per-sub problems (missing DIR/license numbers), retention cap and release date, record
        retention date, job-site postings, a checklist sorted by due date with status and the rule behind each item,
        line items showing the math, citations, and verified flags. verified is false whenever coverage rests on the
        unchecked LC 1720 definition or an amount falls on/after SB 909's assumed operative date; unverified_rule_ids
        names the rules, and each line item / checklist entry has its own verified flag."""
        return public_works_readiness(project_value_usd, awarding_body_type, is_public_funds, work_type, start_date,
                                      contractor_dir_registered, dir_registration_expiry, subcontractors,
                                      prime_bid_usd, as_of, your_role=your_role, award_date=award_date,
                                      contract_execution_date=contract_execution_date,
                                      bid_opening_date=bid_opening_date,
                                      estimated_completion_date=estimated_completion_date,
                                      has_employees=has_employees, journeyman_hours_by_craft=journeyman_hours_by_craft,
                                      public_subsidy_usd=public_subsidy_usd, all_single_family=all_single_family,
                                      awarding_body_name=awarding_body_name,
                                      worked_or_bid_while_unregistered=worked_or_bid_while_unregistered,
                                      your_contract_usd=your_contract_usd)

    @mcp.tool(name="apprentice_hours_required", annotations=ro)
    def _apprentice_hours_required(
        journeyman_hours: Annotated[float, Field(ge=0, description=(
            "Journeyman hours in ONE apprenticeable craft on this contract"))],
        excluded_overtime_hours: Annotated[float, Field(ge=0, description=(
            "Hours included above that were over 8/day or 40/week (they do not count)"))] = 0,
        apprentice_hours_worked: Annotated[Optional[float], Field(ge=0, description=(
            "Registered-apprentice hours already worked in that craft, to get the shortfall"))] = None,
        craft: Annotated[Optional[str], Field(description="Craft name, e.g. 'carpenter'")] = None,
        prime_contract_usd: Annotated[Optional[float], Field(ge=0, description=(
            "Prime contract amount, to check the $30,000 threshold"))] = None,
        project_journeyman_hours: Annotated[Optional[float], Field(ge=0, description=(
            "Total journeyman hours on the project, all crafts (the under-40-hour DAS 142 exemption is per project); "
            "defaults to this craft's hours"))] = None,
        as_of: Annotated[Optional[str], Field(description=(
            "ISO date to evaluate as of; default today in California"))] = None,
    ) -> ApprenticeHoursResult:
        """California public works apprentice ratio (LC 1777.5(g)-(h)): required apprentice hours = 1 hour per 5
        straight-time journeyman hours in the craft, measured at the end of the contract. Returns counted journeyman
        hours, required apprentice hours, shortfall against registered-apprentice hours worked, the minimum DAS 142
        dispatch request (the greater of 8 hours per apprentice or 20% of the apprentice hours, unless the PROJECT has
        under 40 journeyman hours), whether the ratio applies at the given prime contract amount, the math, notes,
        citations and unverified_rule_ids."""
        return apprentice_hours_required(journeyman_hours, excluded_overtime_hours, apprentice_hours_worked, craft,
                                         prime_contract_usd, project_journeyman_hours, as_of)

    @mcp.tool(name="prevailing_wage_penalties", annotations=ro)
    def _prevailing_wage_penalties(
        underpaid_worker_days: Annotated[int, Field(ge=0, description=(
            "Sum over workers of calendar days each was paid below prevailing wage"))] = 0,
        underpayment_category: Annotated[UnderpaymentCategory, Field(description=(
            "good_faith_corrected (honest mistake promptly fixed), standard, prior_violation_3_years (penalized on "
            "another contract in the last 3 years), willful"))] = "standard",
        unpaid_wages_usd: Annotated[float, Field(ge=0, description="Back wages owed (prevailing minus paid)")] = 0,
        unpaid_60_days_after_assessment: Annotated[bool, Field(description=(
            "True if wages are still unpaid 60+ days after a wage assessment was served"))] = False,
        overtime_violation_worker_days: Annotated[int, Field(ge=0, description=(
            "Worker-days with work over 8 h/day or 40 h/week not paid overtime"))] = 0,
        cpr_workers: Annotated[int, Field(ge=0, description=(
            "Number of workers on the requested certified payroll records"))] = 0,
        cpr_request_received_date: Annotated[Optional[str], Field(description=(
            "ISO date you received a written certified payroll request"))] = None,
        cpr_produced_date: Annotated[Optional[str], Field(description=(
            "ISO date you produced the records (blank = not yet)"))] = None,
        cpr_days_late: Annotated[Optional[int], Field(ge=0, description=(
            "Instead of dates: days past the 10-day deadline"))] = None,
        ecpr_days_late: Annotated[int, Field(ge=0, description="Days an eCPR upload to DIR is past due")] = 0,
        unregistered_work_days: Annotated[int, Field(ge=0, description=(
            "Days you worked on public works without DIR registration"))] = 0,
        unregistered_sub_work_days: Annotated[int, Field(ge=0, description=(
            "Days an unregistered lower-tier sub worked for you"))] = 0,
        apprenticeship_noncompliance_days: Annotated[int, Field(ge=0, description=(
            "Full calendar days of apprenticeship noncompliance (e.g. DAS 140 not sent)"))] = 0,
        apprenticeship_repeat_within_3_years: Annotated[bool, Field(description=(
            "Knowing repeat apprenticeship violation within 3 years"))] = False,
        as_of: Annotated[Optional[str], Field(description=(
            "ISO date to evaluate as of; default today in California"))] = None,
    ) -> PenaltyResult:
        """California public works penalty exposure. Returns one line per violation with low/high dollars, the math
        and citation: LC 1775 underpayment ($40-$200 per worker per day; $0 minimum for a corrected good-faith
        mistake, $80 with a prior violation, $120 if willful), LC 1813 overtime ($25 per worker per day, fixed), LC
        1776(h) late certified payroll ($100 per worker per calendar day after the 10-day deadline), LC 1771.4 late
        eCPR ($100/day after a 14-day grace, max $5,000 per project), LC 1771.1 unregistered work ($100/day up to
        $8,000 plus the $2,000 penalty registration fee) and unregistered subs ($100/day up to $10,000), LC 1777.7
        apprenticeship (up to $100/day, $300 for knowing repeats). Also back wages, liquidated damages, totals,
        debarment risks, notes and verified flags. Lines under sections SB 909 amends (LC 1725.5, 1775, 1776, 1777.7)
        are marked unverified on/after its assumed operative date (2027-01-01), since the new amounts are not
        verified."""
        return prevailing_wage_penalties(underpaid_worker_days, underpayment_category, unpaid_wages_usd,
                                         unpaid_60_days_after_assessment, overtime_violation_worker_days, cpr_workers,
                                         cpr_request_received_date, cpr_produced_date, cpr_days_late, ecpr_days_late,
                                         unregistered_work_days, unregistered_sub_work_days,
                                         apprenticeship_noncompliance_days, apprenticeship_repeat_within_3_years,
                                         as_of)
