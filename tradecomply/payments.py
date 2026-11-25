"""Getting paid on California construction jobs: prompt payment, retention, stop payment notices and lien waivers.

  prompt_payment_check          who pays whom, private or public -> the statute that sets the deadline (Civ 8800, 8812,
                                8814; B&P 7108.5; PCC 10262, 20104.50, 10261.5, 7107), the due date, days late, the late
                                charge (2% per month penalty or legal-rate interest) with the arithmetic, and what to send
  retention_cap_check           contract price + retention % -> SB 61 (Civ 8811) or PCC 7201 5% cap, exemptions,
                                flow-down to subcontracts and the most retention allowed on a pay application
  select_lien_waiver            progress|final x conditional|unconditional -> Civ 8132/8134/8136/8138 form, when it may
                                be signed, and the warnings that go with it
  stop_payment_notice_deadline  private or public -> last day to give a stop payment notice, preliminary notice check,
                                bonded-notice amount, the window to sue on it, and the payment bond alternative

Every legal number comes from the rulebook (core.rule(id).param(key)) at call time. The few rules the rulebook does not
hold are in UNVERIFIED below with their official source; any result that uses one has verified=False.
"""
import calendar
import math
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
CIV_8800, CIV_8800_DISPUTE, CIV_8800_PEN = ("private-owner-progress-payment-30-days", "private-owner-progress-dispute-150",
                                            "private-owner-progress-penalty-2pct")
BP_7108_5, BP_DISPUTE, BP_PEN, BP_CITATION = ("bp-7108-5-pay-subs-7-days", "bp-7108-5-dispute-150",
                                              "bp-7108-5-penalty-2pct", "civpen-7108-5-subcontractor-payment")
PCC_10262, PCC_10262_DISPUTE, PCC_20104_50, PCC_10261_5 = (
    "state-contract-pay-subs-7-days", "pcc-10262-state-subs", "pcc-20104-50-local-progress-30-days",
    "pcc-10261-5-state-progress-30-days")
CIV_8812, CIV_8814, CIV_RET_DISPUTE, CIV_8818 = ("private-retention-release-owner-45-days",
                                                 "private-retention-passthrough-gc-to-sub-10-days",
                                                 "private-retention-dispute-150", "private-retention-penalty-2pct")
PCC_7107, PCC_7107_PASS, PCC_7107_PEN, PCC_7107_DONE, PCC_7107_STATE = (
    "public-retention-release-60-days", "pcc-7107-passthrough-7-days", "pcc-7107-penalty-2pct",
    "pcc-7107-completion-definition", "pcc-7107-public-retention-60-days")
COMPLETION, NOC_15, NOC_COPY = "completion-definition-private", "noc-recording-15-days", "noc-copy-to-claimants-10-days"
CIV_8811, CIV_8811_EX, CIV_8820, PCC_7201 = ("private-retention-5pct-cap", "private-retention-cap-exemptions",
                                             "private-retention-no-waiver", "public-retention-5pct-cap")
WAIVER_FORMS = {("progress", "conditional"): "waiver-form-conditional-progress",
                ("progress", "unconditional"): "waiver-form-unconditional-progress",
                ("final", "conditional"): "waiver-form-conditional-final",
                ("final", "unconditional"): "waiver-form-unconditional-final"}
WAIVER_REQUIRED, WAIVER_EVIDENCE, WAIVER_CO, WAIVER_7034 = ("waiver-statutory-form-required",
                                                           "waiver-conditional-evidence-of-payment",
                                                           "waiver-covers-executed-change-orders", "no-illegal-lien-waivers")
HIC_RELEASE, HIC_CLAUSE = "hic-lien-release-on-payment", "hic-contract-lien-release-clause"
BP_7120 = "failure-to-pay-suppliers-and-services"
LIEN_DC, LIEN_OTHER = "lien-deadline-direct-contractor", "lien-deadline-subcontractor-supplier"
SPN_WHO, SPN_BOND, SPN_SUIT, SPN_WITHHOLD = ("spn-who-may-give-owner", "spn-bonded-lender-125", "spn-enforcement-timing",
                                             "spn-owner-must-withhold")
SPN_SERVICE, SPN_CONTENTS, SPN_SURETY = "spn-private-service", "spn-private-contents", "spn-serve-surety-if-bond"
SERVICE_METHODS, PROOF = "notice-service-methods", "notice-proof-declaration"
PRELIM, PRELIM_LATE, PRELIM_REQ, PRELIM_WHO = ("prelim-notice-20-day-deadline", "prelim-notice-late-lookback",
                                               "prelim-notice-prerequisite-to-lien", "prelim-notice-who-is-exempt")
BOND_NOTICE, BOND_SUIT = "private-bond-claim-notice", "private-bond-action-6-months"
PUB_SPN, PUB_PRELIM, PUB_FILE, PUB_SUIT = ("public-spn-deadline", "public-prelim-notice", "public-spn-filing-location",
                                           "public-spn-enforcement-timing")
PUB_BOND_NOTICE, PUB_BOND_SUIT, PUB_NO_LIEN, PUB_BOND_REQ = (
    "public-bond-claim-notice", "public-bond-action-6-months", "web_lien-payment:public-works-no-mechanics-lien",
    "public-payment-bond-required-25000")

# Law the rulebook does not hold. Any result that uses an entry lists it in unverified_sources and has verified=False.
UNVERIFIED = {
    "ccp-685.010-legal-rate": {
        "citation": "Code of Civil Procedure 685.010(a)", "rate_percent_per_year": 10,
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CCP&sectionNum=685.010",
        "note": "PCC 20104.50 and 10261.5 charge interest at the legal rate in CCP 685.010(a): the general rate is 10% "
                "per year simple. (A lower rate added in 2023 for certain medical and personal-debt judgments does not "
                "fit a contractor's payment claim.) Not in the rulebook; confirm the current rate on leginfo."},
    "civ-8508-spn-timing": {
        "citation": "Civil Code 8508",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=8508",
        "note": "A private-works stop payment notice is effective only if given before the time to record a claim of "
                "lien expires (Civil Code 8412/8414), so its deadline is the claimant's lien recording deadline."},
    "civ-8530-lender-spn": {
        "citation": "Civil Code 8530",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=8530",
        "note": "Any claimant, including a direct contractor, may give a stop payment notice to the construction "
                "lender."},
    "civ-9100-public-claimant": {
        "citation": "Civil Code 9100",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=9100",
        "note": "On public works, stop payment notice and payment bond rights belong to people whose work was "
                "authorized by the direct contractor or a subcontractor; the direct contractor's own claim against the "
                "public entity is a contract claim."},
    "ccp-12a-holidays": {
        "citation": "Code of Civil Procedure 12a, 135; Government Code 6700",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CCP&sectionNum=12a",
        "note": "When the last day to act falls on a Saturday, Sunday or holiday, CCP 12a extends the period to the next "
                "day that is not one. Holidays used here (California court holidays, observed Friday/Monday when they "
                "fall on a weekend): New Year's Day, Martin Luther King Jr. Day, Lincoln's Birthday (Feb 12), "
                "Presidents' Day, Cesar Chavez Day (Mar 31), Memorial Day, Juneteenth, Independence Day, Labor Day, "
                "Native American Day (4th Friday in September), Veterans Day, Thanksgiving and the day after, Christmas. "
                "County recorders and local agencies can close on other days. Not in the rulebook; this tool never "
                "relies on the extension for a deadline, only to warn."},
}

ATTORNEY = ("A California-licensed construction attorney should review any demand letter and must handle any "
            "collection suit, lien foreclosure or stop payment notice enforcement action.")

Project = Literal["private", "public"]
Payer = Literal["owner", "direct_contractor", "subcontractor"]
Payee = Literal["direct_contractor", "subcontractor", "supplier"]
Agency = Literal["local", "state"]
Claimant = Literal["subcontractor", "supplier", "equipment_lessor", "laborer", "direct_contractor"]
Exemption = Literal["residential_not_mixed_use", "sub_failed_to_furnish_bond_after_written_notice",
                    "substantially_complex_finding"]
Status = Literal["not_yet_due", "paid_on_time", "paid_late", "overdue", "due_date_unknown"]


def P(rule_id: str, key: str, default=KeyError):
    return core.rule(rule_id).param(key, default)


def _choice(value, field: str, allowed) -> str:
    options = allowed.__args__ if hasattr(allowed, "__args__") else tuple(allowed)
    if value not in options:
        raise ValueError(f"{field} must be one of {', '.join(options)}; got {value!r}")
    return value


def _num(value, field: str) -> Optional[float]:
    """A finite, non-negative number (or None). Accepts numeric strings like '12500.50'."""
    if value is None or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number like 12500.50 (no $ or commas), got {value!r}") from None
    if not math.isfinite(x):
        raise ValueError(f"{field} must be a finite number, got {value!r}")
    if x < 0:
        raise ValueError(f"{field} cannot be negative")
    return x


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    return f"${x:,.2f}"


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
        raise ValueError(f"{field} must be an ISO date like 2026-11-25, got {value!r}") from None


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _title(heading: str) -> str:
    return " ".join(w if w in ("and", "on") else w.capitalize() for w in heading.lower().split())


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    """n-th (1-based) weekday of a month; n=-1 is the last one."""
    if n > 0:
        first = date(y, m, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(y, m, calendar.monthrange(y, m)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


@lru_cache(maxsize=None)
def _holidays(y: int) -> dict[date, str]:
    """California court holidays for year y as listed in UNVERIFIED['ccp-12a-holidays'] (weekend dates observed on the
    Friday before / Monday after)."""
    out = {}
    for (m, day), name in {(1, 1): "New Year's Day", (2, 12): "Lincoln's Birthday", (3, 31): "Cesar Chavez Day",
                           (6, 19): "Juneteenth", (7, 4): "Independence Day", (11, 11): "Veterans Day",
                           (12, 25): "Christmas Day"}.items():
        d = date(y, m, day)
        shift = {5: -1, 6: 1}.get(d.weekday(), 0)
        out[d + timedelta(days=shift)] = name + (" (observed)" if shift else "")
    thanksgiving = _nth_weekday(y, 11, 3, 4)
    out.update({_nth_weekday(y, 1, 0, 3): "Martin Luther King Jr. Day", _nth_weekday(y, 2, 0, 3): "Presidents' Day",
                _nth_weekday(y, 5, 0, -1): "Memorial Day", _nth_weekday(y, 9, 0, 1): "Labor Day",
                _nth_weekday(y, 9, 4, 4): "Native American Day", thanksgiving: "Thanksgiving Day",
                thanksgiving + timedelta(days=1): "the day after Thanksgiving"})
    return out


def _holiday(d: date) -> Optional[str]:
    return (_holidays(d.year) | _holidays(d.year + 1)).get(d)   # Jan 1 on a Saturday is observed Dec 31


def _business_day(d: date, step: int) -> tuple[date, bool]:
    """Nearest day before (step=-1) or after (step=1) d that is not a weekend or holiday; True if a holiday was hit."""
    hit = False
    d += timedelta(days=step)
    while d.weekday() >= 5 or _holiday(d):
        hit = hit or bool(_holiday(d))
        d += timedelta(days=step)
    return d, hit


def _closed_day_note(ctx: "_Ctx", d: Optional[date], what: str,
                     late_end: Optional[date] = None) -> tuple[Optional[str], bool, Optional[date]]:
    """Warn when a deadline falls on a weekend or California holiday. With late_end (the payment was late), also give
    the payer's CCP 12a argument that the period ran to the next business day. Returns (note, whether the unverified
    holiday/CCP 12a source was used, the business day to act by)."""
    if not d:
        return None, False, None
    name = _holiday(d)
    if d.weekday() < 5 and not name:
        return None, False, d
    prior, hit = _business_day(d, -1)
    day = f"{name}, a California court holiday" if name else f"a {d.strftime('%A')}"
    text = (f"{what} ({d.isoformat()}) falls on {day}. This tool does not move dates: act by the prior business day, "
            f"{prior.isoformat()}.")
    used = bool(name or hit)
    if late_end:
        nxt, _ = _business_day(d, 1)
        text += (f" The payer may argue the period ran to the next business day, {nxt.isoformat()} (CCP 12a), which "
                 f"would make it {max(0, (late_end - nxt).days)} days late; the figures here use {d.isoformat()}.")
        used = True
    if used:
        src = ctx.unverified("ccp-12a-holidays")
        text += f" (Holidays and CCP 12a: {src['citation']}, not in the rulebook: {src['source']})"
    return text, used, prior


# ---------------------------------------------------------------- shared models
class LineItem(BaseModel):
    item: str
    value: str
    math: str = ""
    rule_id: Optional[str] = None
    verified: bool = False


class _Ctx:
    """Collects the rules and unverified sources a result relied on, plus its line items."""

    def __init__(self):
        self.rules: list[str] = []
        self.unv: list[str] = []
        self.items: list[LineItem] = []
        self.notes: list[str] = []

    def use(self, *rule_ids: str) -> None:
        for r in rule_ids:
            if r and r not in self.rules:
                self.rules.append(r)

    def unverified(self, key: str) -> dict:
        if key not in self.unv:
            self.unv.append(key)
        return UNVERIFIED[key]

    def item(self, item: str, value, math: str = "", rule_id: Optional[str] = None) -> None:
        if rule_id and rule_id in UNVERIFIED:
            self.unverified(rule_id)
            ok, rid = False, f"UNVERIFIED:{rule_id}"
        else:
            self.use(rule_id)
            ok, rid = (core.rule(rule_id).verified if rule_id else False), rule_id
        self.items.append(LineItem(item=item, value=str(value), math=math, rule_id=rid, verified=ok))

    def note(self, text: Optional[str]) -> None:
        if text and text not in self.notes:
            self.notes.append(text)


class _PayResult(core.Result):
    as_of: str
    line_items: list[LineItem] = Field(default_factory=list)
    licensed_professional: str = ATTORNEY
    notes: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    unverified_sources: list[dict] = Field(default_factory=list)


def _finish(res: _PayResult, ctx: _Ctx) -> _PayResult:
    res.cite(*ctx.rules)
    res.line_items, res.notes = ctx.items, ctx.notes
    res.unverified_rule_ids = sorted(r for r in ctx.rules if not core.rule(r).verified)
    res.unverified_sources = [{"key": k, **UNVERIFIED[k]} for k in ctx.unv]
    if ctx.unv:
        res.verified = False
    if not res.verified:
        res.notes.append("Not fully verified: relies on " + ", ".join(res.unverified_rule_ids + ctx.unv)
                         + ". Confirm those against the official text (leginfo.legislature.ca.gov) before relying on "
                           "the dates or dollar amounts.")
    return res


# ---------------------------------------------------------------- late-charge math
def _penalty(base: float, pct: float, due: date, end: date) -> tuple[float, float, str]:
    """pct% per month from the day after `due` through `end`: whole calendar months plus the partial month prorated
    by its actual length. Also returns the reading where every started month counts in full."""
    n = 0
    while _add_months(due, n + 1) <= end:
        n += 1
    anchor, nxt = _add_months(due, n), _add_months(due, n + 1)
    extra, month_len = (end - anchor).days, (nxt - anchor).days
    months = n + extra / month_len
    charge = _money(base * pct / 100 * months)
    full = _money(base * pct / 100 * (n + (1 if extra else 0)))
    math = (f"{n} full month(s) + {extra}/{month_len} of a month = {months:.4f} months; "
            f"{_usd(base)} x {pct:g}% x ({n} + {extra}/{month_len}) months = {_usd(charge)}")
    return charge, full, math


def _interest(base: float, rate: float, days: int) -> tuple[float, str]:
    charge = _money(base * rate / 100 * days / 365)
    return charge, f"{_usd(base)} x {rate:g}% per year x {days}/365 days = {_usd(charge)}"


# ---------------------------------------------------------------- prompt payment
class Obligation(BaseModel):
    kind: Literal["progress_payment", "retention"]
    payer: str
    payee: str
    governing_law: str
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False
    trigger: str = ""
    trigger_date: Optional[str] = None
    statutory_due_date: Optional[str] = None
    contract_due_date: Optional[str] = None
    due_date: Optional[str] = None
    act_by_date: Optional[str] = None
    due_date_basis: str = ""
    amount_due_usd: float = 0.0
    disputed_amount_usd: float = 0.0
    max_withhold_usd: float = 0.0
    max_withhold_basis: str = ""
    must_pay_by_due_date_usd: float = 0.0
    paid_date: Optional[str] = None
    status: Status = "due_date_unknown"
    days_late: int = 0
    days_until_due: Optional[int] = None
    late_charge_type: Literal["penalty_percent_per_month", "interest_legal_rate", "none"] = "none"
    late_charge_rate: Optional[str] = None
    late_charge_usd: float = 0.0
    late_charge_if_each_started_month_counts_usd: Optional[float] = None
    late_charge_accrued_through: Optional[str] = None
    late_charge_math: str = ""
    what_to_send: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PromptPaymentResult(_PayResult):
    project_type: str
    payer_role: str
    payee_role: str
    obligations: list[Obligation] = Field(default_factory=list)
    total_late_charges_usd: float = 0.0
    what_to_send: list[str] = Field(default_factory=list)


def _label(role: str, project: str) -> str:
    return {"owner": "public entity" if project == "public" else "owner", "direct_contractor": "direct contractor",
            "subcontractor": "subcontractor", "supplier": "supplier"}[role]


def _obligation(ctx: _Ctx, kind: str, payer: str, payee: str, law: str, *, as_of: date, amount: float,
                days_rule: Optional[str] = None, days_key: str = "deadline_days", trigger: str = "",
                trigger_date: Optional[date] = None, contract_due: Optional[date] = None,
                contract_may_override: bool = False, disputed: float = 0.0, dispute_rule: Optional[str] = None,
                dispute_key: str = "max_withhold_percent_of_disputed", dispute_excluded: bool = False,
                charge: Optional[str] = None, charge_rule: Optional[str] = None,
                paid: Optional[date] = None, extra_rules: tuple = ()) -> Obligation:
    ob = Obligation(kind=kind, payer=payer, payee=payee, governing_law=law, trigger=trigger,
                    trigger_date=_iso(trigger_date), contract_due_date=_iso(contract_due), amount_due_usd=_money(amount),
                    disputed_amount_usd=_money(disputed), paid_date=_iso(paid))
    rids = [r for r in (days_rule, *extra_rules) if r]
    label = "Retention" if kind == "retention" else "Progress payment"

    statutory_due = None
    if days_rule and trigger_date:
        days = P(days_rule, days_key)
        statutory_due = trigger_date + timedelta(days=days)
        ctx.item(f"{label}: statutory due date", statutory_due.isoformat(),
                 f"{trigger_date.isoformat()} ({trigger}) + {days} days", days_rule)
    ob.statutory_due_date = _iso(statutory_due)
    due = statutory_due
    if contract_due and (contract_may_override or not days_rule):
        due = contract_due
        ob.due_date_basis = ("date agreed in writing (the statute applies 'unless otherwise agreed in writing')"
                             if days_rule else "contract or purchase-order terms (no statutory deadline)")
        if days_rule:
            ob.notes.append("The rulebook does not say whether the statutory late charge applies when a written "
                            "agreement sets its own date; the charge below runs from the agreed date. Confirm with "
                            "an attorney.")
    elif statutory_due:
        ob.due_date_basis = f"{P(days_rule, days_key)} days after {trigger}"
        if contract_due and contract_due != statutory_due:
            ob.notes.append(f"The contract date {contract_due.isoformat()} differs from the statutory date "
                            f"{statutory_due.isoformat()}; the statutory late charge runs from the statutory date.")
    ob.due_date = _iso(due)

    withhold = 0.0
    if disputed > amount:
        raise ValueError(f"disputed amount {_usd(disputed)} cannot exceed the amount due {_usd(amount)}")
    if disputed and dispute_rule:
        pct = P(dispute_rule, dispute_key)
        withhold = _money(min(amount, disputed * pct / 100))
        rids.append(dispute_rule)
        ob.max_withhold_basis = f"statutory limit: {pct:g}% of the amount in good-faith dispute"
        ctx.item(f"{label}: most that may be withheld for the good-faith dispute", _usd(withhold),
                 f"min({_usd(amount)}, {pct:g}% x {_usd(disputed)})", dispute_rule)
    elif disputed and dispute_excluded:
        withhold = _money(disputed)
        ob.max_withhold_basis = "disputed amount left out (statute covers undisputed requests only)"
        ob.notes.append("The statute covers undisputed payment requests, so the disputed portion is left out of the "
                        "amount that earns interest. The rulebook has no percentage-of-dispute withholding rule for this payment.")
    elif disputed:
        withhold = _money(disputed)
        ob.max_withhold_basis = "disputed amount left out (no statutory dispute rule)"
        ob.notes.append(f"No statutory dispute-withholding rule applies here (none in the rulebook): the {_usd(disputed)} "
                        "in dispute is left out of the amount shown as due and is settled under the contract or "
                        "purchase order.")
    ob.max_withhold_usd = withhold
    must_pay = _money(amount - withhold)
    ob.must_pay_by_due_date_usd = must_pay

    if due is None:
        ob.status = "due_date_unknown"
    elif paid:
        ob.status = "paid_on_time" if paid <= due else "paid_late"
    else:
        ob.status = "not_yet_due" if as_of <= due else "overdue"
    end = paid or as_of
    if due:
        ob.days_late = max(0, (end - due).days)
        if ob.status == "not_yet_due":
            ob.days_until_due = (due - as_of).days
    late = ob.status in ("paid_late", "overdue")
    if late:
        ctx.item(f"{label}: days late", ob.days_late, f"{end.isoformat()} - {due.isoformat()}")
    day_note, used_holidays, act_by = _closed_day_note(ctx, due, f"{label} due date", end if late else None)
    ob.notes.append(day_note or "")
    ob.act_by_date = _iso(act_by)

    if charge and late and must_pay > 0:
        rids.append(charge_rule)
        ob.late_charge_accrued_through = end.isoformat()
        if charge == "penalty":
            pct = P(charge_rule, "penalty_percent_per_month")
            ob.late_charge_type, ob.late_charge_rate = "penalty_percent_per_month", f"{pct:g}% per month"
            ob.late_charge_usd, ob.late_charge_if_each_started_month_counts_usd, ob.late_charge_math = _penalty(
                must_pay, pct, due, end)
            ctx.item(f"{label}: late penalty", _usd(ob.late_charge_usd), ob.late_charge_math, charge_rule)
            ob.notes.append(f"The statute says {pct:g}% 'per month' without saying how a partial month counts. The "
                            "penalty prorates the partial month by its days; if every started month counts in full it "
                            f"is {_usd(ob.late_charge_if_each_started_month_counts_usd)}.")
        else:
            src = ctx.unverified("ccp-685.010-legal-rate")
            rate = src["rate_percent_per_year"]
            ob.late_charge_type, ob.late_charge_rate = "interest_legal_rate", f"{rate:g}% per year simple"
            ob.late_charge_usd, ob.late_charge_math = _interest(must_pay, rate, ob.days_late)
            ctx.item(f"{label}: interest at the legal rate", _usd(ob.late_charge_usd), ob.late_charge_math,
                     "ccp-685.010-legal-rate")
            ob.notes.append(f"Interest rate from {src['citation']} (not in the rulebook): {src['source']}")
    ctx.use(*rids)
    ob.rule_ids = list(dict.fromkeys(rids))
    ob.verified = (bool(rids) and all(core.rule(r).verified for r in rids) and ob.late_charge_type != "interest_legal_rate"
                   and not used_holidays)
    ob.notes = [n for n in ob.notes if n]
    return ob


def _fees(charge_rule: Optional[str]) -> bool:
    if not charge_rule:
        return False
    return any(P(charge_rule, k, False) for k in ("prevailing_party_fees", "prevailing_party_fees_and_costs"))


def _citation_range(as_of: date) -> tuple[float, float]:
    """CSLB citation civil-penalty range for a B&P 7108.5 violation; the higher minimum is operative from the date in
    the rule's `effective` field."""
    r = core.rule(BP_CITATION)
    lo, hi = r.param("min_usd"), r.param("max_usd")
    m = re.search(r"operative (\d{4}-\d{2}-\d{2})", r.effective or "")
    if m and as_of < date.fromisoformat(m.group(1)):
        lo = r.param("min_usd_before_2026_07_01", lo)
    return lo, hi


def _actions(ob: Obligation, charge_rule: Optional[str], project: str, ctx: _Ctx, as_of: date) -> list[str]:
    out, law = [], ob.governing_law
    payer, payee, Payer_ = f"the {ob.payer}", f"the {ob.payee}", f"The {ob.payer}"
    payer_do, payee_do = ob.payer.capitalize(), ob.payee.capitalize()     # "Owner: pay ...", "Subcontractor: send ..."
    what = "retention" if ob.kind == "retention" else "progress payment"
    if ob.max_withhold_usd and ob.status != "due_date_unknown":
        by = f"by {ob.due_date}" if ob.status == "not_yet_due" else f"(due {ob.due_date})"
        if ob.max_withhold_basis.startswith("statutory"):
            out.append(f"{Payer_} may hold back at most {_usd(ob.max_withhold_usd)} for the good-faith dispute and must "
                       f"pay the other {_usd(ob.must_pay_by_due_date_usd)} {by}. Put the dispute and the amount held in "
                       "writing.")
        else:
            out.append(f"The {_usd(ob.max_withhold_usd)} in dispute is outside the deadline; {payer} must pay the "
                       f"undisputed {_usd(ob.must_pay_by_due_date_usd)} {by}. Put the dispute in writing.")
    if ob.status == "not_yet_due":
        out.append(f"{Payer_} pays {payee} {_usd(ob.must_pay_by_due_date_usd)} by {ob.due_date} "
                   f"({ob.days_until_due} days from now) under {law}."
                   + (f" That day is a weekend or holiday: pay by {ob.act_by_date}." if ob.act_by_date != ob.due_date
                      else ""))
    elif ob.status == "paid_on_time":
        out.append(f"Paid on time under {law}; no statutory late charge.")
    elif ob.status == "due_date_unknown":
        out.append(f"Due date unknown: give the missing date (see notes) to compute the {what} deadline.")
    else:
        owed = _usd(ob.must_pay_by_due_date_usd)
        charge = (f" plus the {_usd(ob.late_charge_usd)} late charge accrued through {ob.late_charge_accrued_through}"
                  if ob.late_charge_usd else "")
        if ob.status == "overdue":
            out.append(f"{payer_do}: pay {payee} {owed}{charge} now; the charge keeps accruing until paid.")
            out.append(f"{payee_do}: send {payer} a written demand for {owed}{charge}, citing {law}. Keep proof of "
                       "delivery.")
        elif ob.late_charge_usd:
            out.append(f"{payee_do}: send {payer} a written demand for the {_usd(ob.late_charge_usd)} late charge "
                       f"({ob.days_late} days late), citing {law}.")
        if _fees(charge_rule):
            out.append(f"In a lawsuit to collect, the prevailing party recovers attorney's fees ({law}).")
        if charge_rule == BP_PEN and P(BP_PEN, "disciplinary_action"):
            ctx.use(BP_CITATION)
            lo, hi = _citation_range(as_of)
            out.append("Late payment to a subcontractor is also cause for CSLB discipline; a CSLB citation carries a "
                       f"civil penalty of {_usd(lo)} to {_usd(hi)}. The subcontractor may file a complaint with the "
                       "CSLB.")
        if ob.status == "overdue" and ob.payee == "direct contractor" and project == "public":
            src = ctx.unverified("civ-9100-public-claimant")
            out.append("Unpaid direct contractor on public works: there is no mechanics lien on public property and the "
                       f"stop payment notice and payment bond protect the people below you, not you ({src['citation']}, "
                       "not in the rulebook). Use the claim procedure in your contract and have a construction "
                       "attorney review it.")
        elif ob.status == "overdue" and ob.payee == "direct contractor":
            out.append("Unpaid: your remedies are a mechanics lien (calculate_lien_deadline) and, if the job has a "
                       "construction lender, a bonded stop payment notice to the lender (stop_payment_notice_deadline, "
                       "claimant_role=direct_contractor).")
        elif ob.status == "overdue":
            out.append("Unpaid: check your remaining remedies now with stop_payment_notice_deadline"
                       + (" (stop payment notice to the public entity, payment bond claim)" if project == "public"
                          else " and calculate_lien_deadline (mechanics lien, stop payment notice, payment bond)") + ".")
    if ob.kind == "progress_payment":
        out.append("Exchange statutory lien waivers for this payment: conditional before payment, unconditional only "
                   "after it clears (select_lien_waiver, payment_type=progress).")
    else:
        out.append("Retention is usually the last payment: if it pays your whole remaining balance, use "
                   "select_lien_waiver with payment_type=final; if money is still owed, use the progress forms.")
    return out


def prompt_payment_check(project_type: Project, payer_role: Payer, payee_role: Payee,
                         amount_due_usd: Optional[float] = None, payment_due_date=None,
                         payment_received_by_payer_date=None, paid_date=None, retention_amount_usd: Optional[float] = None,
                         completion_date=None, noc_recorded_date=None, disputed_amount_usd: float = 0.0, as_of=None, *,
                         payment_request_date=None, retention_received_by_payer_date=None, retention_paid_date=None,
                         retention_disputed_amount_usd: float = 0.0, public_agency: Agency = "local"
                         ) -> PromptPaymentResult:
    """Which California prompt-payment statute governs a payment, when it is due, and what is owed if it is late."""
    as_of = _d(as_of, "as_of") or logic.today_ca()
    _choice(project_type, "project_type", Project)
    _choice(payer_role, "payer_role", Payer)
    _choice(payee_role, "payee_role", Payee)
    _choice(public_agency, "public_agency", Agency)
    if payer_role == "owner" and payee_role != "direct_contractor":
        raise ValueError("The owner (or public entity) pays the direct contractor. Subcontractors and suppliers are paid "
                         "by the direct contractor or a subcontractor; use payer_role=direct_contractor or subcontractor.")
    if payer_role != "owner" and payee_role == "direct_contractor":
        raise ValueError("A direct contractor is paid by the owner: use payer_role=owner.")
    amount_due_usd, retention_amount_usd = _num(amount_due_usd, "amount_due_usd"), _num(retention_amount_usd,
                                                                                          "retention_amount_usd")
    disputed = _num(disputed_amount_usd, "disputed_amount_usd") or 0.0
    ret_disputed = _num(retention_disputed_amount_usd, "retention_disputed_amount_usd") or 0.0
    if not amount_due_usd and not retention_amount_usd:
        raise ValueError("Give amount_due_usd (a progress payment) and/or retention_amount_usd (retention to release).")
    dates = {k: _d(v, k) for k, v in dict(
        payment_due_date=payment_due_date, payment_received_by_payer_date=payment_received_by_payer_date,
        paid_date=paid_date, completion_date=completion_date, noc_recorded_date=noc_recorded_date,
        payment_request_date=payment_request_date, retention_received_by_payer_date=retention_received_by_payer_date,
        retention_paid_date=retention_paid_date).items()}
    for amount, extra in ((amount_due_usd, {"disputed_amount_usd": disputed, "paid_date": dates["paid_date"]}),
                          (retention_amount_usd, {"retention_disputed_amount_usd": ret_disputed,
                                                  "retention_paid_date": dates["retention_paid_date"]})):
        given = [k for k, v in extra.items() if v]
        if given and not amount:
            raise ValueError(f"{', '.join(given)} given without the payment it belongs to: progress payment fields go "
                             "with amount_due_usd, retention fields (retention_*) with retention_amount_usd.")
    for k in ("paid_date", "retention_paid_date"):
        if dates[k] and dates[k] > as_of:
            raise ValueError(f"{k} {dates[k].isoformat()} is after as_of {as_of.isoformat()}. For a planned payment "
                             f"date, leave {k} empty and set as_of to that date.")
    ctx = _Ctx()
    payer, payee = _label(payer_role, project_type), _label(payee_role, project_type)
    res = PromptPaymentResult(as_of=as_of.isoformat(), project_type=project_type, payer_role=payer_role,
                              payee_role=payee_role)
    obligations: list[tuple[Obligation, Optional[str]]] = []

    if amount_due_usd:
        ob, cr = _progress(ctx, project_type, payer_role, payee_role, payer, payee, amount_due_usd, disputed, dates,
                           public_agency, as_of)
        obligations.append((ob, cr))
    if retention_amount_usd:
        ob, cr = _retention(ctx, project_type, payer_role, payee_role, payer, payee, retention_amount_usd,
                            ret_disputed, dates, public_agency, as_of)
        obligations.append((ob, cr))

    for ob, cr in obligations:
        ob.what_to_send = _actions(ob, cr, project_type, ctx, as_of)
        res.what_to_send.extend(a for a in ob.what_to_send if a not in res.what_to_send)
    res.obligations = [ob for ob, _ in obligations]
    res.total_late_charges_usd = _money(sum(ob.late_charge_usd for ob in res.obligations))
    return _finish(res, ctx)


def _progress(ctx, project, payer_role, payee_role, payer, payee, amount, disputed, d, agency, as_of):
    common = dict(as_of=as_of, amount=amount, disputed=disputed, contract_due=d["payment_due_date"],
                  paid=d["paid_date"])
    if payee_role == "supplier":
        ctx.use(BP_7108_5)
        ob = _obligation(ctx, "progress_payment", payer, payee, "purchase-order / credit terms", **common)
        ob.notes.append("B&P 7108.5 by its terms covers payments to subcontractors, not material suppliers; the "
                        "supplier's due date comes from its purchase order or credit terms, and the rulebook has no "
                        "statutory late charge for it. An unpaid supplier's remedies are its lien, stop payment notice "
                        "and payment bond rights (stop_payment_notice_deadline).")
        if not d["payment_due_date"]:
            ob.notes.append("Give payment_due_date (the date on the purchase order or credit terms).")
        ctx.use(BP_7120)
        ob.notes.append("A licensee's willful failure to pay for materials when due, when it can pay or has been paid "
                        "for that job, is cause for CSLB discipline (B&P 7120).")
        return ob, None

    if payer_role == "owner" and project == "private":
        ob = _obligation(ctx, "progress_payment", payer, payee, "Civil Code 8800", days_rule=CIV_8800,
                         trigger="the direct contractor's notice demanding payment under the contract (pay application)",
                         trigger_date=d["payment_request_date"], contract_may_override=P(CIV_8800,
                                                                                          "waivable_by_written_agreement"),
                         dispute_rule=CIV_8800_DISPUTE, charge="penalty", charge_rule=CIV_8800_PEN, **common)
        if not (d["payment_request_date"] or d["payment_due_date"]):
            ob.notes.append(f"Give payment_request_date: the {P(CIV_8800, 'deadline_days')} days run from the day the "
                            "direct contractor gave the owner its pay application / notice demanding payment.")
        ob.notes.append("Civil Code 8802 lists situations where 8800 does not apply; those exclusions are not in the "
                        "verified rulebook. Check them with an attorney before claiming the penalty.")
        if P(CIV_8800_PEN, "in_lieu_of_interest", False):
            ob.notes.append(f"The {P(CIV_8800_PEN, 'penalty_percent_per_month'):g}% per month penalty is in place of "
                            "any interest otherwise due.")
        return ob, CIV_8800_PEN

    if payer_role == "owner":   # public entity -> direct contractor
        rid, law = (PCC_10261_5, "Public Contract Code 10261.5") if agency == "state" else (
            PCC_20104_50, "Public Contract Code 20104.50")
        ob = _obligation(ctx, "progress_payment", payer, payee, law, days_rule=rid, days_key="pay_within_days",
                         trigger="the agency's receipt of an undisputed, properly submitted payment request",
                         trigger_date=d["payment_request_date"], dispute_excluded=True, charge="interest",
                         charge_rule=rid, **common)
        if not d["payment_request_date"]:
            ob.notes.append("Give payment_request_date: the day the agency received your properly submitted payment "
                            "request.")
        ob.notes.append("If the agency returned the request as improper, the 30 days run from its receipt of a proper "
                        "request. Interest starts only on an undisputed, properly submitted request.")
        if agency == "state":
            ob.notes.append(f"A state agency must return an improper payment request within "
                            f"{P(PCC_10261_5, 'return_improper_request_within_days')} days with written reasons.")
        else:
            ob.notes.append("The retention portion of the final payment is not a progress payment under PCC 20104.50; "
                            "retention is released under PCC 7107.")
        return ob, None

    if project == "public" and agency == "state" and payer_role == "direct_contractor":   # PCC 10262 displaces 7108.5
        ob = _obligation(ctx, "progress_payment", payer, payee, "Public Contract Code 10262", days_rule=PCC_10262,
                         days_key="deadline_days_after_progress_payment",
                         trigger=f"the {payer}'s receipt of the progress payment",
                         trigger_date=d["payment_received_by_payer_date"], dispute_rule=PCC_10262_DISPUTE, **common)
        ob.notes.append("On state agency contracts PCC 10262 applies instead of B&P 7108.5, so the B&P 7108.5 "
                        f"{P(BP_PEN, 'penalty_percent_per_month'):g}% per month penalty is not computed. A late-payment "
                        "penalty for state contracts may be in PCC 10262.5, which is not in the rulebook, so no late "
                        "charge is computed here; check https://leginfo.legislature.ca.gov/faces/codes_displaySection"
                        ".xhtml?lawCode=PCC&sectionNum=10262.5 before demanding one.")
        ob.notes.append("An underpaid subcontractor must notify, in writing: "
                        + " and ".join(P(PCC_10262, "sub_must_notify_of_underpayment")) + ".")
        if not d["payment_received_by_payer_date"]:
            ob.notes.append(f"Give payment_received_by_payer_date: the day the {payer} received the progress payment.")
        return ob, None

    ob = _obligation(ctx, "progress_payment", payer, payee, "Business and Professions Code 7108.5",
                     days_rule=BP_7108_5, trigger=f"the {payer}'s receipt of the progress payment",
                     trigger_date=d["payment_received_by_payer_date"],
                     contract_may_override=P(BP_7108_5, "waivable_by_written_agreement"), dispute_rule=BP_DISPUTE,
                     charge="penalty", charge_rule=BP_PEN, **common)
    if not (d["payment_received_by_payer_date"] or d["payment_due_date"]):
        ob.notes.append(f"Give payment_received_by_payer_date: the day the {payer} received the progress payment that "
                        f"included the {payee}'s work. If it has not been paid yet, the B&P 7108.5 clock has not "
                        "started.")
    if project == "public" and agency == "state":
        ob.notes.append("State agency job: PCC 10262 governs the prime contractor's payments to its subcontractors; a "
                        "subcontractor paying a lower tier is under B&P 7108.5.")
    return ob, BP_PEN


def _retention(ctx, project, payer_role, payee_role, payer, payee, amount, disputed, d, agency, as_of):
    common = dict(as_of=as_of, amount=amount, disputed=disputed, paid=d["retention_paid_date"])
    if payee_role == "supplier":
        ob = _obligation(ctx, "retention", payer, payee, "purchase-order / credit terms", **common)
        ob.notes.append("The rulebook's retention release statutes (Civil Code 8812/8814, PCC 7107) run to contractors "
                        "and subcontractors; a supplier's retention follows its purchase order (no due date computed).")
        return ob, None

    if payer_role == "owner":
        completion, noc = d["completion_date"], d["noc_recorded_date"]
        trigger = "completion of the work of improvement"
        if not completion and noc:
            completion = noc
            trigger = (f"completion (not given: it happened on or before the notice of completion recorded "
                       f"{noc.isoformat()}, so this is the LATEST possible due date)")
        if project == "private":
            ob = _obligation(ctx, "retention", payer, payee, "Civil Code 8812", days_rule=CIV_8812, trigger=trigger,
                             trigger_date=completion, dispute_rule=CIV_RET_DISPUTE, charge="penalty",
                             charge_rule=CIV_8818, extra_rules=(COMPLETION,), **common)
            ob.notes.append("Completion means the earliest of: " + "; ".join(P(COMPLETION, "triggers")) + ".")
            if not completion:
                ob.notes.append("Give completion_date (or noc_recorded_date) to compute the retention due date.")
            if not d["completion_date"] and noc:
                early = noc - timedelta(days=P(NOC_15, "deadline_days")) + timedelta(days=P(CIV_8812, "deadline_days"))
                ctx.use(NOC_15)
                ob.notes.append(f"A notice of completion is recorded within {P(NOC_15, 'deadline_days')} days after "
                                f"completion, so retention may have been due as early as {early.isoformat()}. Enter the "
                                "completion date stated in the notice for an exact date.")
            ob.notes.append("A contract clause waiving these retention rules is void (rule private-retention-no-waiver).")
            ctx.use(CIV_8820)
            return ob, CIV_8818
        extra = (PCC_7107_DONE, PCC_7107_STATE) if agency == "state" else (PCC_7107_DONE,)
        ob = _obligation(ctx, "retention", payer, payee, "Public Contract Code 7107(c)", days_rule=PCC_7107,
                         trigger=trigger, trigger_date=completion, dispute_rule=PCC_7107, charge="penalty",
                         charge_rule=PCC_7107_PEN, extra_rules=extra, **common)
        ob.notes.append("Completion for public retention means the earliest of: "
                        + "; ".join(P(PCC_7107_DONE, "triggers")) + ".")
        if agency == "state":
            ob.notes.append("State agencies: " + P(PCC_7107_STATE, "state_agency_alternative")
                            + ". Do not claim a state agency is late between day 60 and day 90 without checking this.")
        if P(PCC_7107_PEN, "in_lieu_of_interest", False):
            ob.notes.append(f"The {P(PCC_7107_PEN, 'penalty_percent_per_month'):g}% per month charge is in place of "
                            "any interest otherwise due.")
        if not completion:
            ob.notes.append("Give completion_date (or noc_recorded_date) to compute the retention due date.")
        return ob, PCC_7107_PEN

    # direct contractor / subcontractor passing retention down
    rec = d["retention_received_by_payer_date"]
    trigger = f"the {payer}'s receipt of all or part of the retention"
    if project == "private":
        ob = _obligation(ctx, "retention", payer, payee, "Civil Code 8814", days_rule=CIV_8814, trigger=trigger,
                         trigger_date=rec, dispute_rule=CIV_RET_DISPUTE, charge="penalty", charge_rule=CIV_8818,
                         **common)
        if payer_role == "subcontractor":
            ob.notes.append("The rulebook's Civil Code 8814 rule names the direct contractor as payer; it is applied to "
                            "a subcontractor paying a lower tier here. Confirm with an attorney.")
        cr = CIV_8818
    else:
        ob = _obligation(ctx, "retention", payer, payee, "Public Contract Code 7107(d)", days_rule=PCC_7107_PASS,
                         trigger=trigger, trigger_date=rec, dispute_rule=PCC_7107_PASS, charge="penalty",
                         charge_rule=PCC_7107_PEN, **common)
        if payer_role == "subcontractor":
            ob.notes.append("The rulebook's PCC 7107(d) rule names the original (prime) contractor as payer; it is "
                            "applied to a subcontractor paying a lower tier here. Confirm with an attorney.")
        cr = PCC_7107_PEN
    if not rec:
        ob.notes.append(f"Give retention_received_by_payer_date: the {payer} must pass retention through within days "
                        "of receiving it, not of completion.")
    return ob, cr


# ---------------------------------------------------------------- retention cap
class RetentionCapResult(_PayResult):
    project_type: str
    contract_tier: str
    contract_date: str
    contract_price_usd: float
    retention_pct: float
    cap_applies: bool
    cap_pct: Optional[float] = None
    exemptions_applied: list[str] = Field(default_factory=list)
    exemptions_rejected: list[str] = Field(default_factory=list)
    compliant: bool
    max_total_retention_usd: Optional[float] = None
    requested_total_retention_usd: float
    excess_retention_usd: float = 0.0
    max_retention_this_payment_usd: Optional[float] = None
    requested_retention_this_payment_usd: Optional[float] = None
    flowdown_ok: Optional[bool] = None
    what_to_do: list[str] = Field(default_factory=list)


def retention_cap_check(contract_price_usd: float, retention_pct: float, contract_date, project_type: Project,
                        exemptions: Optional[list[str]] = None, as_of=None, *, stories: Optional[int] = None,
                        contract_tier: Literal["prime_contract", "subcontract"] = "prime_contract",
                        prime_contract_retention_pct: Optional[float] = None, payment_amount_usd: Optional[float] = None,
                        retention_held_to_date_usd: float = 0.0) -> RetentionCapResult:
    """Is a retention percentage legal under the SB 61 private cap (Civ 8811) or the public cap (PCC 7201)?"""
    as_of = _d(as_of, "as_of") or logic.today_ca()
    signed = _d(contract_date, "contract_date")
    if not signed:
        raise ValueError("contract_date is required (ISO date the contract or subcontract was entered into)")
    contract_price_usd, retention_pct = _num(contract_price_usd, "contract_price_usd"), _num(retention_pct,
                                                                                             "retention_pct")
    if not contract_price_usd:
        raise ValueError("contract_price_usd must be greater than 0")
    prime_contract_retention_pct = _num(prime_contract_retention_pct, "prime_contract_retention_pct")
    if retention_pct is None:
        raise ValueError("retention_pct is required (e.g. 5 for 5%)")
    for name, pct in (("retention_pct", retention_pct), ("prime_contract_retention_pct", prime_contract_retention_pct)):
        if pct is not None and pct > 100:
            raise ValueError(f"{name} must be between 0 and 100 (e.g. 5 for 5%)")
    _choice(project_type, "project_type", Project)
    _choice(contract_tier, "contract_tier", ("prime_contract", "subcontract"))
    if stories is not None and (not isinstance(stories, int) or stories < 1):
        raise ValueError(f"stories must be a whole number of 1 or more, got {stories!r}")
    payment_amount_usd = _num(payment_amount_usd, "payment_amount_usd")
    held = _num(retention_held_to_date_usd, "retention_held_to_date_usd") or 0.0
    exemptions = list(dict.fromkeys([exemptions] if isinstance(exemptions, str) else exemptions or []))
    bad = [ex for ex in exemptions if ex not in Exemption.__args__]
    if bad:
        raise ValueError(f"unknown exemption {bad[0]!r}; use one of {', '.join(Exemption.__args__)}")
    ctx = _Ctx()
    rid = CIV_8811 if project_type == "private" else PCC_7201
    ctx.use(rid)
    start = _d(P(rid, "applies_to_contracts_entered_on_or_after"), "effective")
    requested_total = _money(contract_price_usd * retention_pct / 100)
    res = RetentionCapResult(as_of=as_of.isoformat(), project_type=project_type, contract_tier=contract_tier,
                             contract_date=signed.isoformat(), contract_price_usd=_money(contract_price_usd),
                             retention_pct=retention_pct, cap_applies=False, compliant=True,
                             requested_total_retention_usd=requested_total)
    if signed > as_of:
        ctx.note(f"contract_date {signed.isoformat()} is after {as_of.isoformat()}; checked as a contract you are about "
                 "to sign.")
    law = "Civil Code 8811 (SB 61)" if project_type == "private" else "Public Contract Code 7201"

    if signed < start:
        ctx.item("Statutory retention cap", "none for this contract",
                 f"contract entered {signed.isoformat()}, before {start.isoformat()}", rid)
        ctx.note(f"The {law} cap applies to contracts entered on or after {start.isoformat()}. This contract's agreed "
                 "retention governs; the release deadlines (prompt_payment_check) still apply.")
        res.what_to_do.append("No statutory cap: the contract's retention terms govern. Any new contract or subcontract "
                              f"signed on or after {start.isoformat()} is capped.")
        return _finish(res, ctx)

    for ex in exemptions:
        ok, why = _exemption(ctx, ex, project_type, contract_tier, stories)
        (res.exemptions_applied if ok else res.exemptions_rejected).append(ex)
        ctx.note(why)
    if res.exemptions_applied:
        ctx.item("Statutory retention cap", "does not apply (exemption)", ", ".join(res.exemptions_applied),
                 CIV_8811_EX if project_type == "private" else PCC_7201)
        res.what_to_do.append(f"Exempt from the {law} cap: keep the written basis for the exemption in the job file "
                              "(bond-requirement notice and the sub's failure to bond, the stories/use of the project, "
                              "or the agency's published 'substantially complex' finding).")
        if "substantially_complex_finding" in res.exemptions_applied:
            res.what_to_do.append("The retention withheld may not exceed the amount stated in the bid documents.")
        return _finish(res, ctx)

    per_payment = P(rid, "max_retention_percent_of_each_payment")
    total_pct = P(rid, "max_total_retention_percent_of_contract_price")
    cap_total = _money(contract_price_usd * total_pct / 100)
    res.cap_applies, res.cap_pct, res.max_total_retention_usd = True, float(per_payment), cap_total
    ctx.item("Retention cap per payment", f"{per_payment:g}% of each payment", "", rid)
    ctx.item("Retention cap in total", _usd(cap_total), f"{_usd(contract_price_usd)} x {total_pct:g}%", rid)
    ctx.item("Retention requested in total", _usd(requested_total), f"{_usd(contract_price_usd)} x {retention_pct:g}%")
    over = retention_pct > per_payment or requested_total > cap_total
    res.excess_retention_usd = _money(max(0.0, requested_total - cap_total))
    res.compliant = not over

    over_held = _money(max(0.0, held - cap_total))
    if over_held:
        res.compliant = False
        ctx.item("Retention already held above the total cap", _usd(over_held), f"{_usd(held)} held - {_usd(cap_total)}",
                 rid)
    if payment_amount_usd is not None:
        room = max(0.0, cap_total - held)
        res.max_retention_this_payment_usd = _money(min(payment_amount_usd * per_payment / 100, room))
        res.requested_retention_this_payment_usd = _money(payment_amount_usd * retention_pct / 100)
        ctx.item("Most retention allowed on this payment", _usd(res.max_retention_this_payment_usd),
                 f"min({per_payment:g}% x {_usd(payment_amount_usd)}, {_usd(cap_total)} cap - "
                 f"{_usd(held)} already held)", rid)
        if res.requested_retention_this_payment_usd > res.max_retention_this_payment_usd + 0.005:
            res.compliant = False

    if contract_tier == "subcontract" and prime_contract_retention_pct is not None:
        res.flowdown_ok = retention_pct <= prime_contract_retention_pct
        ctx.item("Flow-down: subcontract % may not exceed the prime contract %",
                 "ok" if res.flowdown_ok else "VIOLATION", f"{retention_pct:g}% vs {prime_contract_retention_pct:g}%",
                 rid)
        res.compliant = res.compliant and res.flowdown_ok
    elif contract_tier == "subcontract":
        ctx.note("Give prime_contract_retention_pct to check the flow-down rule: a subcontract may not withhold a "
                 "higher percentage than the owner-direct contractor (or public entity-prime) contract.")
    elif prime_contract_retention_pct is not None:
        ctx.note("prime_contract_retention_pct was ignored: the flow-down limit applies to subcontracts "
                 "(contract_tier=subcontract).")

    if res.compliant:
        res.what_to_do.append(f"{retention_pct:g}% is within the {law} cap.")
    else:
        if over or res.flowdown_ok is False:
            res.what_to_do.append(f"Reduce retention to {per_payment:g}% or less of each payment and {_usd(cap_total)} "
                                  "in total" + (" (and no more than the prime contract percentage)."
                                                if contract_tier == "subcontract" else "."))
        if over_held:
            res.what_to_do.append(f"You already hold {_usd(held)}, {_usd(over_held)} over the {_usd(cap_total)} total "
                                  "cap: release the excess and withhold no more retention on this contract.")
        elif payment_amount_usd is not None and res.requested_retention_this_payment_usd > \
                res.max_retention_this_payment_usd + 0.005:
            res.what_to_do.append(f"On this payment withhold no more than {_usd(res.max_retention_this_payment_usd)} "
                                  f"(not {_usd(res.requested_retention_this_payment_usd)}).")
        if project_type == "private":
            ctx.use(CIV_8820)
            res.what_to_do.append("A contract clause that sets a higher retention does not avoid the cap: the retention "
                                  "rules cannot be waived by contract (rule private-retention-no-waiver).")
            res.what_to_do.append(f"Attorney's fees in an action to enforce the cap: {P(CIV_8811, 'attorney_fees')} "
                                  "(Civil Code 8811(c)).")
        elif P(PCC_7201, "waiver_prohibited", False):
            res.what_to_do.append("No party may require a waiver of PCC 7201.")
    if project_type == "public":
        ctx.note("PCC 7201 does not limit " + P(PCC_7201, "does_not_limit") + ".")
    return _finish(res, ctx)


def _exemption(ctx: _Ctx, ex: str, project: str, tier: str, stories: Optional[int]) -> tuple[bool, str]:
    if ex == "residential_not_mixed_use":
        if project != "private":
            return False, "The residential exemption is in Civil Code 8811 (private works) only; not applied."
        ctx.use(CIV_8811_EX)
        cap = P(CIV_8811_EX, "exemption_residential.max_stories")
        if stories is None:
            return False, (f"Residential exemption not applied: give stories (it covers residential projects that are "
                           f"not mixed-use and do not exceed {cap} stories).")
        if stories > cap:
            return False, f"Residential exemption not applied: {stories} stories exceeds {cap}."
        return True, f"Residential exemption applies: residential, not mixed-use, {stories} stories (limit {cap})."
    if ex == "sub_failed_to_furnish_bond_after_written_notice":
        rid = CIV_8811_EX if project == "private" else PCC_7201
        ctx.use(rid)
        if tier != "subcontract":
            return False, "The bond-failure exemption applies to a subcontract only; not applied to the prime contract."
        cond = P(rid, "exemption_sub_bond_failure" if project == "private" else "exception_sub_bond_failure")
        return True, f"Bond-failure exemption applies only if: {cond}."
    # substantially_complex_finding (exemption names are validated by the caller)
    if project != "public":
        return False, "The 'substantially complex' exemption is a public works (PCC 7201) rule; not applied."
    ctx.use(PCC_7201)
    return True, "Substantially complex exemption applies only with a " + P(PCC_7201, "exception") + "."


# ---------------------------------------------------------------- lien waivers
class LienWaiverResult(_PayResult):
    payment_type: str
    payment_status: str
    form_name: str
    civil_code_section: str
    form_rule_id: str
    when_to_use: str
    takes_effect: str
    safe_to_sign_now: Optional[bool] = None
    recommended_form_instead: Optional[str] = None
    signed_by: str
    fields_to_fill: list[str] = Field(default_factory=list)
    exceptions_on_form: list[str] = Field(default_factory=list)
    notice_text_excerpt: str = ""
    full_text_in_rulebook: bool = False
    official_form_url: str
    sequence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def select_lien_waiver(payment_type: Literal["progress", "final"], payment_status: Literal["conditional", "unconditional"],
                       payment_received: Optional[bool] = None, open_change_order_claims_usd: Optional[float] = None,
                       home_improvement: bool = False, as_of=None) -> LienWaiverResult:
    """Pick the California statutory lien waiver and release form (Civil Code 8132/8134/8136/8138)."""
    as_of = _d(as_of, "as_of") or logic.today_ca()
    key = (payment_type, payment_status)
    if key not in WAIVER_FORMS:
        raise ValueError("payment_type must be progress|final and payment_status conditional|unconditional")
    open_change_order_claims_usd = _num(open_change_order_claims_usd, "open_change_order_claims_usd")
    rid = WAIVER_FORMS[key]
    form = core.rule(rid)
    ctx = _Ctx()
    ctx.use(rid, WAIVER_REQUIRED, WAIVER_CO, WAIVER_7034)
    section = form.citation_text
    res = LienWaiverResult(
        as_of=as_of.isoformat(), payment_type=payment_type, payment_status=payment_status,
        form_name=_title(form.param("heading")), civil_code_section=section, form_rule_id=rid,
        when_to_use=form.plain_english or "",
        takes_effect=(f"On {form.param('effective_on')}" if payment_status == "conditional"
                      else "As soon as the claimant signs and delivers it, even if payment never arrives"),
        signed_by=f"The {P(WAIVER_REQUIRED, 'must_be_signed_by')}", fields_to_fill=form.param("fields"),
        exceptions_on_form=form.param("standard_exceptions"), notice_text_excerpt=form.quote or "",
        official_form_url=form.source or "",
        licensed_professional=("No license is needed to sign; have a California construction attorney review any "
                               "waiver that someone has altered or that you are asked to sign before payment."))
    ctx.item("Form", f"{res.form_name} ({section})", "", rid)

    res.warnings.append(f"Use the statutory form exactly as printed in {section}: a waiver and release is effective "
                        "only if it follows one of the four statutory forms (Civil Code 8132, 8134, 8136, 8138). "
                        "Do not retype, add to, or edit the headings, notice or exceptions.")
    res.warnings.append(f"The rulebook holds only an excerpt of this form, not its full text. Download the official form "
                        f"from {res.official_form_url} and fill in the blanks.")
    if payment_status == "unconditional":
        res.safe_to_sign_now = bool(payment_received) if payment_received is not None else None
        res.warnings.append("This document is enforceable against the claimant once signed, even if the claimant has "
                            "not been paid. Sign only after the payment has actually cleared the bank.")
        if payment_received is False:
            alt = core.rule(WAIVER_FORMS[(payment_type, "conditional")])
            res.recommended_form_instead = f"{_title(alt.param('heading'))} ({alt.citation_text})"
            res.warnings.insert(0, f"DO NOT SIGN: payment has not been received. Give the "
                                   f"{res.recommended_form_instead} instead.")
    else:
        ctx.use(WAIVER_EVIDENCE)
        res.safe_to_sign_now = True
        res.warnings.append("Safe to sign before payment only if the check details on the form (maker, amount, "
                            "payable to) match the payment you are actually expecting.")
        if payment_received:
            res.warnings.append("Payment has already cleared: the payer will usually ask for the unconditional form "
                                "now; a conditional form is still safe to sign.")
        res.warnings.append("A conditional waiver binds the claimant only on evidence of payment: "
                            + " or ".join(P(WAIVER_EVIDENCE, "evidence_of_payment")) + ". A person should not rely on "
                            "it until satisfied the claimant has been paid.")
    res.warnings.append("Rights under written change orders fully executed before the date you sign are waived unless "
                        "listed as an exception on the form.")
    if open_change_order_claims_usd:
        if payment_type == "final":
            res.warnings.append(f"Write {_usd(open_change_order_claims_usd)} on the 'Disputed claims for extras' line; "
                                "the final forms preserve nothing else.")
        else:
            res.warnings.append(f"Your {_usd(open_change_order_claims_usd)} of unpaid extras stays protected by the "
                                "'Extras for which the claimant has not received payment' exception; list any change "
                                "order executed before signing.")
    elif payment_type == "final":
        res.warnings.append("A final waiver releases every claim for the job except the 'Disputed claims for extras' "
                            "amount you write on it. Enter open change-order claims before signing.")
    if payment_type == "final":
        res.warnings.append("Use a final form only for the payment of your whole remaining balance, retention included. "
                            "Once that payment is made it releases everything not listed as disputed extras, so for a "
                            "partial payment (or a retention release with money still owed) use the progress forms.")
    if payment_type == "progress":
        res.warnings.append("Progress waivers do not waive retention (it is a listed exception).")
    res.warnings.append("A licensed contractor may not require a subcontractor, employee or supplier to waive lien rights "
                        "in violation of Civil Code 8122 (B&P 7034(b)).")
    cond = core.rule(WAIVER_FORMS[(payment_type, "conditional")])
    unc = core.rule(WAIVER_FORMS[(payment_type, "unconditional")])
    res.sequence = [f"1. With the pay request: {_title(cond.param('heading'))} ({cond.citation_text}).",
                    "2. Payment is made and clears the bank.",
                    f"3. After it clears: {_title(unc.param('heading'))} ({unc.citation_text})."]
    if home_improvement:
        ctx.use(HIC_RELEASE, HIC_CLAUSE)
        res.warnings.append("Home improvement: on request after each payment the contractor must furnish full "
                            "unconditional releases for the work paid for, and the homeowner may withhold all further "
                            "payments until they are furnished (B&P 7159.5(a)(6)); the contract must say so "
                            "(B&P 7159(c)(4)).")
    return _finish(res, ctx)


# ---------------------------------------------------------------- stop payment notices
class PrelimCheck(BaseModel):
    required: bool
    recipients: list[str] = Field(default_factory=list)
    deadline: Optional[str] = None
    served_date: Optional[str] = None
    on_time: Optional[bool] = None
    covers_work_from: Optional[str] = None
    covers_any_of_your_work: Optional[bool] = None
    serve_today_covers_work_from: Optional[str] = None
    note: str = ""


class StopPaymentNoticeResult(_PayResult):
    project_type: str
    claimant_role: str
    applicable: bool = True
    may_give_to: list[str] = Field(default_factory=list)
    completion_date_used: Optional[str] = None
    notice_of_completion_effective: Optional[bool] = None
    deadline: Optional[str] = None
    act_by_date: Optional[str] = None
    deadline_rule_applied: str = ""
    days_remaining: Optional[int] = None
    risk_level: Optional[str] = None
    preliminary_notice: Optional[PrelimCheck] = None
    bonded_notice_amount_usd: Optional[float] = None
    enforcement_suit_earliest: Optional[str] = None
    enforcement_suit_latest: Optional[str] = None
    notify_public_entity_within_days_of_filing: Optional[int] = None
    payment_bond: list[str] = Field(default_factory=list)
    where_to_serve: list[str] = Field(default_factory=list)
    service_methods: list[str] = Field(default_factory=list)
    required_contents: list[str] = Field(default_factory=list)
    what_to_send: list[str] = Field(default_factory=list)


def stop_payment_notice_deadline(project_type: Project, claimant_role: Claimant, completion_date=None,
                                 noc_recorded_date=None, last_furnishing_date=None, as_of=None, *,
                                 cessation_of_labor_date=None, first_furnishing_date=None,
                                 preliminary_notice_served_date=None, claim_amount_usd: Optional[float] = None,
                                 construction_lender: bool = False, payment_bond_recorded: bool = False,
                                 spn_given_date=None, noc_copy_received: Optional[bool] = None,
                                 public_agency: Agency = "local",
                                 residential_4_or_fewer_units: Optional[bool] = None) -> StopPaymentNoticeResult:
    """Last day to give a California stop payment notice (private or public works) and what goes with it."""
    as_of = _d(as_of, "as_of") or logic.today_ca()
    _choice(project_type, "project_type", Project)
    _choice(claimant_role, "claimant_role", Claimant)
    _choice(public_agency, "public_agency", Agency)
    claim_amount_usd = _num(claim_amount_usd, "claim_amount_usd")
    d = {k: _d(v, k) for k, v in dict(completion_date=completion_date, noc_recorded_date=noc_recorded_date,
                                      last_furnishing_date=last_furnishing_date,
                                      cessation_of_labor_date=cessation_of_labor_date,
                                      first_furnishing_date=first_furnishing_date,
                                      preliminary_notice_served_date=preliminary_notice_served_date,
                                      spn_given_date=spn_given_date).items()}
    if d["first_furnishing_date"] and d["last_furnishing_date"] and d["first_furnishing_date"] > d["last_furnishing_date"]:
        raise ValueError("first_furnishing_date is after last_furnishing_date")
    ctx = _Ctx()
    res = StopPaymentNoticeResult(as_of=as_of.isoformat(), project_type=project_type, claimant_role=claimant_role)
    if project_type == "public" and claimant_role == "direct_contractor":
        src = ctx.unverified("civ-9100-public-claimant")
        ctx.use(PUB_NO_LIEN)
        res.applicable = False
        res.deadline_rule_applied = "not applicable"
        ctx.note(src["note"] + f" ({src['citation']}, not in the rulebook: {src['source']}). No mechanics lien is "
                 "available on public property either. Follow the claim procedure in your contract and have a "
                 "California construction attorney review it.")
        res.what_to_send = ["No stop payment notice or payment bond claim is available to you as the prime on public "
                            "works: follow the claim procedure in your contract with the public entity, with a "
                            "construction attorney."]
        return _finish(res, ctx)
    if not (d["completion_date"] or d["noc_recorded_date"] or d["last_furnishing_date"] or d["cessation_of_labor_date"]):
        raise ValueError("Give completion_date, noc_recorded_date, cessation_of_labor_date or last_furnishing_date.")
    if project_type == "private":
        _spn_private(ctx, res, claimant_role, d, claim_amount_usd, construction_lender, payment_bond_recorded,
                     noc_copy_received, residential_4_or_fewer_units)
    else:
        _spn_public(ctx, res, claimant_role, d, public_agency)
    _prelim(ctx, res, claimant_role, d, project_type == "public", construction_lender, as_of)

    deadline = _d(res.deadline, "deadline")
    res.days_remaining = (deadline - as_of).days
    ctx.note("All periods are calendar days; weekends and holidays are not skipped. Give the notice before the "
             "deadline, not on it.")
    res.risk_level = logic._risk(res.days_remaining)
    day_note, _, act_by = _closed_day_note(ctx, deadline, "The stop payment notice deadline")
    ctx.note(day_note)
    res.act_by_date = act_by.isoformat()
    used = _d(res.completion_date_used, "completion")
    if used and used > as_of:
        ctx.note(f"The completion date used ({used.isoformat()}) is after {as_of.isoformat()}: the deadline assumes the "
                 "job is completed that day. Run this again when it is actually completed or a notice is recorded.")
    expired = res.days_remaining < 0
    given_ok = bool(d["spn_given_date"] and d["spn_given_date"] <= deadline)
    if d["spn_given_date"] and not given_ok:
        ctx.note("The notice was given after the deadline, so it is likely not effective.")
    if expired and given_ok:
        ctx.note(f"The stop payment notice deadline passed {-res.days_remaining} days ago; your notice was given "
                 f"{d['spn_given_date'].isoformat()}, in time.")
    elif expired:
        ctx.note(f"The stop payment notice deadline passed {-res.days_remaining} days ago; a late notice is not "
                 "effective. Ask an attorney about any remaining payment bond or contract claims.")
    give_lines, res.what_to_send = res.what_to_send, []      # the branch's "give/file the notice by ..." lines
    if not res.applicable:                                   # private direct contractor with no construction lender
        res.what_to_send.append(
            "No stop payment notice is available to you: a direct contractor cannot give one to the owner, and this "
            f"job has no construction lender. Record your mechanics lien before {deadline.isoformat()} "
            "(calculate_lien_deadline gives the exact window) and pursue your contract claim. If a construction lender "
            f"turns up (check the recorded deed of trust), give it a bonded stop payment notice by {deadline.isoformat()}.")
        return _finish(res, ctx)
    pn, last = res.preliminary_notice, d["last_furnishing_date"]
    bond_hint = " See payment_bond for any bond claim still open." if res.payment_bond else ""
    if expired and not given_ok:
        res.what_to_send.append(
            f"Too late for a stop payment notice: the last day was {deadline.isoformat()} ({-res.days_remaining} days "
            f"ago) and a notice given now is not effective.{bond_hint} Ask a construction attorney about your "
            "remaining payment bond and contract claims.")
        return _finish(res, ctx)
    if pn and pn.covers_any_of_your_work is False:
        res.what_to_send.append(f"No valid stop payment notice: your preliminary notice protects only work furnished on "
                                f"or after {pn.covers_work_from}, after your last furnishing ({last.isoformat()}), so it "
                                f"covers none of your work.{bond_hint} "
                                "Ask a construction attorney about your remaining payment bond and contract claims.")
        return _finish(res, ctx)

    if pn and pn.required and not pn.served_date:
        if last and date.fromisoformat(pn.serve_today_covers_work_from) > last:
            res.what_to_send.append(
                "If you have not served a preliminary notice, you have no valid stop payment notice: one served today "
                f"covers only work furnished on or after {pn.serve_today_covers_work_from}, after your last furnishing "
                f"({last.isoformat()}). Ask a construction attorney about payment bond and contract claims.")
        else:
            res.what_to_send.append(
                f"If you have not served a preliminary notice, serve it today on {', '.join(pn.recipients)}: it is a "
                "prerequisite to a valid stop payment notice and covers only work furnished on or after "
                f"{pn.serve_today_covers_work_from}.")
    if pn and pn.on_time is False and pn.covers_any_of_your_work:
        give_lines.append(f"Your preliminary notice was late: claim only for work furnished on or after "
                          f"{pn.covers_work_from}.")
    if not given_ok:
        res.what_to_send.extend(give_lines)
        if act_by != deadline:
            res.what_to_send.append(f"{deadline.isoformat()} is a weekend or holiday (see notes): give the notice by "
                                    f"{act_by.isoformat()}.")
    suit_rule = SPN_SUIT if project_type == "private" else PUB_SUIT
    latest = deadline + timedelta(days=P(suit_rule, "latest_days_after_notice_period_expires"))
    res.enforcement_suit_latest = latest.isoformat()
    if d["spn_given_date"]:
        earliest = d["spn_given_date"] + timedelta(days=P(suit_rule, "earliest_days_after_notice"))
        res.enforcement_suit_earliest = earliest.isoformat()
        ctx.item("Earliest day to sue on the notice", earliest.isoformat(),
                 f"{d['spn_given_date'].isoformat()} (notice given) + {P(suit_rule, 'earliest_days_after_notice')} days",
                 suit_rule)
    ctx.item("Last day to sue on the notice", latest.isoformat(),
             f"{deadline.isoformat()} (end of the notice period) + "
             f"{P(suit_rule, 'latest_days_after_notice_period_expires')} days", suit_rule)
    suit = (f"File suit to enforce the notice no earlier than {P(suit_rule, 'earliest_days_after_notice')} days after "
            "giving it" + (f" ({res.enforcement_suit_earliest})" if res.enforcement_suit_earliest else "")
            + f" and no later than {latest.isoformat()}, or the notice lapses and the funds are released.")
    if project_type == "public":
        res.notify_public_entity_within_days_of_filing = P(PUB_SUIT, "notify_entity_within_days_of_filing")
        suit += (f" Notify the public entity within {res.notify_public_entity_within_days_of_filing} days after "
                 "filing.")
    if latest < as_of:
        suit = (f"The last day to sue on the notice was {latest.isoformat()}: a notice not sued on by then has lapsed "
                "and the withheld funds are released. Ask a construction attorney about any remaining claims.")
    else:
        ctx.note(_closed_day_note(ctx, latest, "The last day to sue on the notice")[0])
    res.what_to_send.append(suit)
    res.what_to_send.append("Keep a signed proof-of-service declaration for every notice, with the USPS registered/"
                            "certified/express receipt or the carrier's tracking record attached.")
    ctx.use(PROOF)
    return _finish(res, ctx)


def _prelim(ctx: _Ctx, res: StopPaymentNoticeResult, role: str, d: dict, public: bool, lender: bool,
            as_of: date) -> None:
    first, served, last = d["first_furnishing_date"], d["preliminary_notice_served_date"], d["last_furnishing_date"]
    rule_deadline, rule_late = (PUB_PRELIM, PUB_PRELIM) if public else (PRELIM, PRELIM_LATE)
    exempt = P(PUB_PRELIM, "exempt") if public else P(PRELIM_WHO, "exempt")
    ctx.use(PUB_PRELIM if public else PRELIM_WHO)
    if role in exempt:
        res.preliminary_notice = PrelimCheck(required=False, note="Laborers do not give a preliminary notice.")
        return
    note = ""
    if role == "direct_contractor":     # private only: a public prime returns before this
        recipients = P(PRELIM_WHO, "direct_contractor_must_notify")
        note = ("A direct contractor gives a preliminary notice only to the construction lender, if there is one"
                + ("; it is needed for a stop payment notice to the lender. " if lender else "; this job has none."))
        if not lender:
            res.preliminary_notice = PrelimCheck(required=False, recipients=recipients, served_date=_iso(served),
                                                 note=note)
            return
    else:
        recipients = P(PUB_PRELIM, "recipients") if public else P(PRELIM, "recipients")
    pc = PrelimCheck(required=True, recipients=recipients, served_date=_iso(served))
    ctx.use(rule_deadline, rule_late, PUB_PRELIM if public else PRELIM_REQ)
    days, look = P(rule_deadline, "lookback_days" if public else "deadline_days"), P(rule_late, "lookback_days")
    if first:
        dl = first + timedelta(days=days)
        pc.deadline = dl.isoformat()
        ctx.item("Preliminary notice deadline for full coverage", dl.isoformat(),
                 f"{first.isoformat()} (first furnishing) + {days} days", rule_deadline)
        if served:
            pc.on_time = served <= dl
    if served and pc.on_time is not True:
        start = served - timedelta(days=look)
        pc.covers_work_from = start.isoformat()
        ctx.item("Late preliminary notice covers work from" if pc.on_time is False
                 else "If served late, the preliminary notice covers work from", pc.covers_work_from,
                 f"{served.isoformat()} (served) - {look} days", rule_late)
        if last and start <= last:
            pc.covers_any_of_your_work = True
        elif last and pc.on_time is False:
            pc.covers_any_of_your_work = False
    elif served:
        pc.covers_any_of_your_work = True
    if not served:
        start = as_of - timedelta(days=look)
        pc.serve_today_covers_work_from = start.isoformat()
        ctx.item("A preliminary notice served today covers work from", start.isoformat(),
                 f"{as_of.isoformat()} (today) - {look} days", rule_late)
        note += "No preliminary notice date given. If you never served one, the stop payment notice is not valid. "
        if last and start > last:
            note += (f"One served today covers only work furnished on or after {start.isoformat()}, after your last "
                     f"furnishing ({last.isoformat()}), so it would cover none of your work.")
        else:
            note += (f"Serve it today on {', '.join(recipients)}: it covers work furnished on or after "
                     f"{start.isoformat()}.")
        if public:
            note += (" The rulebook notes (unverified) that a claimant in direct contract with the direct contractor "
                     "may be exempt on public works: serve one anyway, and if you did not, ask an attorney before "
                     "giving up the stop payment notice.")
    elif pc.covers_any_of_your_work is False:
        note += (f"Served late: it protects only work furnished on or after {pc.covers_work_from}, which is after your "
                 f"last furnishing ({last.isoformat()}), so it covers none of your work.")
    elif pc.on_time is False:
        note += f"Served late: the notice protects only work furnished on or after {pc.covers_work_from}."
    elif pc.on_time:
        note += "Served on time: covers all your work on the job."
    else:
        note += (f"Give first_furnishing_date to check whether it was on time; if it was late it covers only work "
                 f"furnished on or after {pc.covers_work_from}.")
    pc.note = note.strip()
    res.preliminary_notice = pc


def _spn_private(ctx, res, role, d, claim, lender, bond, noc_copy, small_residential):
    direct = role == "direct_contractor"
    lien_rule = LIEN_DC if direct else LIEN_OTHER
    src = ctx.unverified("civ-8508-spn-timing")
    ctx.use(lien_rule, COMPLETION, SPN_WITHHOLD, SPN_SERVICE, SPN_CONTENTS, SERVICE_METHODS)
    ctx.note(f"{src['note']} ({src['citation']}, not in the rulebook: {src['source']})")
    if direct:
        lsrc = ctx.unverified("civ-8530-lender-spn")
        ctx.use(SPN_WHO)
        res.may_give_to = ["construction lender (bonded notice)"] if lender else []
        ctx.note(f"A direct contractor may not give the owner a stop payment notice (only a {P(SPN_WHO, 'eligible')} "
                 f"may). {lsrc['note']} ({lsrc['citation']}: {lsrc['source']})")
        if not lender:
            res.applicable = False
            ctx.note("No construction lender: a direct contractor's remedies are its mechanics lien "
                     "(calculate_lien_deadline) and its contract. The date below is the outside date for a lender "
                     "notice if a lender is later identified.")
    else:
        ctx.use(SPN_WHO)
        res.may_give_to = ["owner (or the owner's architect)"] + (["construction lender (bonded notice)"] if lender else [])

    # completion of the whole work (Civil Code 8180): the earliest of the events we know about
    completion, basis = d["completion_date"], "completion"
    cess, noc = d["cessation_of_labor_date"], d["noc_recorded_date"]
    min_cess = P(COMPLETION, "cessation_days_before_notice_of_cessation")
    # a notice recorded at least `min_cess` days after labor stopped may be a notice of cessation (Civil Code 8180)
    cessation_notice = bool(cess and noc and noc >= cess + timedelta(days=min_cess))
    if cess:
        auto = cess + timedelta(days=P(COMPLETION, "cessation_days_auto_completion"))
        ctx.item("Completion by cessation of labor", auto.isoformat(),
                 f"{cess.isoformat()} (labor stopped) + {P(COMPLETION, 'cessation_days_auto_completion')} continuous "
                 "days", COMPLETION)
        if not completion or auto < completion:
            completion, basis = auto, f"completion ({P(COMPLETION, 'cessation_days_auto_completion')} days of cessation)"
        if cessation_notice and noc < completion:
            completion, basis = noc, "completion (notice of cessation recorded)"
            ctx.item("Completion by a recorded notice of cessation", noc.isoformat(),
                     f"notice recorded {noc.isoformat()}, at least {min_cess} days after labor stopped "
                     f"{cess.isoformat()}", COMPLETION)
    base = completion or d["last_furnishing_date"]
    if not completion and base:
        basis = "your last furnishing (completion unknown; this can only make the deadline earlier)"
        ctx.note("Completion date not given; your last furnishing date was used, which is the safe (earlier) choice.")
    res.completion_date_used = _iso(completion)
    last = d["last_furnishing_date"]
    if completion and last and last > completion:
        ctx.note(f"Your last furnishing ({last.isoformat()}) is after the completion date used "
                 f"({completion.isoformat()}). The deadline runs from {completion.isoformat()}; check that date against "
                 "the owner's records (a recorded notice of completion states it).")
    no_notice_days = P(lien_rule, "days_after_completion_if_no_notice")
    noc_days = P(lien_rule, "days_after_recorded_noc_or_cessation")
    cands = []
    if base:
        cands.append((base + timedelta(days=no_notice_days), f"{no_notice_days} days after {basis}"))
        ctx.item("Deadline if no valid notice of completion", cands[-1][0].isoformat(),
                 f"{base.isoformat()} + {no_notice_days} days", lien_rule)
    timing_ok = True   # recorded on or within the allowed days after completion (Civil Code 8182)
    if noc:
        ctx.use(NOC_15)
        window = P(NOC_15, "deadline_days")
        short = noc + timedelta(days=noc_days)
        effective: Optional[bool] = True
        if not completion and last and noc < last:
            effective = None
            ctx.note(f"You furnished work after the notice was recorded ({noc.isoformat()}), so it may have been recorded "
                     f"before completion; a premature notice is ineffective, which would leave the {no_notice_days}-day "
                     f"rule ({cands[0][0].isoformat()}). The shorter {noc_days}-day date ({short.isoformat()}) is used "
                     "to be safe; have an attorney confirm before relying on the later one.")
        elif completion and noc < completion:
            effective = None
            ctx.note(f"The notice was recorded {noc.isoformat()}, before the completion date used "
                     f"({completion.isoformat()}). A notice of completion recorded before completion is ineffective, "
                     f"which would leave the {no_notice_days}-day rule ({cands[0][0].isoformat()}). But if it is a "
                     "valid notice of cessation, or the owner's completion date is the right one, the shorter "
                     f"{noc_days}-day date ({short.isoformat()}) applies. The shorter date is used to be safe; have an "
                     "attorney confirm before relying on the later one.")
        elif completion and noc > completion + timedelta(days=window) and cessation_notice:
            effective = None
            ctx.note(f"The notice was recorded more than {window} days after completion. It is treated as a notice of "
                     f"cessation (labor had stopped at least {min_cess} days before), so the shorter date "
                     f"{short.isoformat()} is used to be safe. If it is a notice of completion it was recorded late "
                     f"and is ineffective, and the {no_notice_days}-day rule ({cands[0][0].isoformat()}) applies; "
                     "confirm with counsel.")
        elif completion and noc > completion + timedelta(days=window):
            effective, timing_ok = False, False
            ctx.note(f"The notice of completion was recorded more than {window} days after completion, so it is "
                     f"ineffective and the {no_notice_days}-day rule was used. If the completion date you entered is "
                     f"wrong (true completion within {window} days before recording), the notice is effective and the "
                     f"deadline is {short.isoformat()}: meet that date if you can.")
        if effective is not False and noc_copy is False:
            ctx.use(NOC_COPY)
            entitled = direct or (role != "laborer" and d["preliminary_notice_served_date"] is not None)
            if not entitled:
                ctx.note("The owner must send a copy of the notice only to the direct contractor and to claimants that "
                         f"gave it a preliminary notice ({', '.join(P(NOC_COPY, 'recipients'))}). You gave none, so "
                         "the missing copy does not extend your deadline.")
            elif small_residential is not False:
                ctx.note(f"The copy rule does not apply to an {P(NOC_COPY, 'exempt')} (the rulebook flags its exact "
                         "scope as unsettled: the CSLB Law Book says any residential property of four or fewer units). "
                         + ("This property is one, so" if small_residential else "It is not known whether this property "
                            "is one, so") + " the missing copy is not relied on and the shorter date is kept. Give "
                         "residential_4_or_fewer_units=false if the property is not a residence of four or fewer "
                         "units.")
            else:
                effective = False
                ctx.note(f"The owner did not send you a copy within {P(NOC_COPY, 'deadline_days')} days of recording, so "
                         f"the notice does not shorten your deadline and the {no_notice_days}-day rule was used. The "
                         f"owner will dispute this if it says it mailed a copy: meet {short.isoformat()} if you can, "
                         "and confirm with counsel.")
        elif effective is not False and noc_copy is None:
            ctx.use(NOC_COPY)
            ctx.note(f"The shorter deadline applies to you only if the owner sent you a copy of the notice within "
                     f"{P(NOC_COPY, 'deadline_days')} days of recording; it is used here to be safe.")
        res.notice_of_completion_effective = effective
        if effective is not False:
            cands.append((short, f"{noc_days} days after the notice of completion/cessation was recorded"))
            ctx.item("Deadline after a recorded notice of completion/cessation", short.isoformat(),
                     f"{noc.isoformat()} + {noc_days} days", lien_rule)
    if not cands:
        raise ValueError("The notice of completion is not effective and no completion or last furnishing date was "
                         "given; add completion_date or last_furnishing_date.")
    deadline, rule_txt = min(cands)
    res.deadline, res.deadline_rule_applied = deadline.isoformat(), rule_txt
    ctx.item("Last day to give the stop payment notice", deadline.isoformat(), f"earliest of: {rule_txt}",
             "civ-8508-spn-timing")
    if last and last > deadline:
        ctx.note("Your last furnishing date is after the computed deadline. Check the dates.")

    res.where_to_serve = [*(["Owner: " + "; ".join(P(SPN_SERVICE, "owner_delivery"))] if not direct else []),
                          *(["Construction lender: " + P(SPN_SERVICE, "lender_delivery")] if lender else [])]
    res.service_methods = P(SERVICE_METHODS, "methods")
    if not P(SERVICE_METHODS, "first_class_mail_alone_allowed", True):
        ctx.note("Plain first-class mail alone is not an allowed service method.")
    res.required_contents = ["signed and verified by the claimant"] + P(SPN_CONTENTS, "fields")
    if lender:
        ctx.use(SPN_BOND)
        pct = P(SPN_BOND, "bond_percent_of_claim")
        if claim:
            res.bonded_notice_amount_usd = _money(claim * pct / 100)
            ctx.item("Bond to attach to the lender's notice", _usd(res.bonded_notice_amount_usd),
                     f"{_usd(claim)} claim x {pct:g}%", SPN_BOND)
        res.what_to_send.append(f"Construction lender: give a BONDED stop payment notice (bond {pct:g}% of the claim"
                                + (f" = {_usd(res.bonded_notice_amount_usd)}" if claim else "")
                                + f") by {deadline.isoformat()}; an unbonded notice does not bind the lender.")
    if not direct:
        res.what_to_send.insert(0, f"Owner: give a signed, verified stop payment notice by {deadline.isoformat()} "
                                   "(the owner must then withhold enough to pay the claim, or risk paying it directly).")
    if bond and direct:
        ctx.note("A payment bond given by the direct contractor protects the subcontractors and suppliers below it, "
                 "not the direct contractor, so no payment bond deadlines are given.")
    elif bond:
        ctx.use(SPN_SURETY, BOND_NOTICE, BOND_SUIT)
        b = ["A payment bond is recorded: if no mechanics lien has been recorded, serve the notice on the surety."]
        if completion:
            suit = _add_months(completion, P(BOND_SUIT, "months_after_completion"))
            b.append(f"Suit on the recorded payment bond: no later than {suit.isoformat()} "
                     f"({P(BOND_SUIT, 'months_after_completion')} months after completion; condition: "
                     f"{P(BOND_SUIT, 'condition')}).")
            ctx.item("Payment bond suit deadline", suit.isoformat(),
                     f"{completion.isoformat()} + {P(BOND_SUIT, 'months_after_completion')} months", BOND_SUIT)
        if not d["preliminary_notice_served_date"] and role != "laborer":
            b.append(_bond_notice_line(ctx, BOND_NOTICE, noc if timing_ok else None, completion))
        res.payment_bond = b


def _bond_notice_line(ctx, rule_id, noc, completion) -> str:
    if noc:
        dl, why = noc + timedelta(days=P(rule_id, "days_after_recorded_noc")), \
            f"{noc.isoformat()} + {P(rule_id, 'days_after_recorded_noc')} days"
    elif completion:
        dl, why = completion + timedelta(days=P(rule_id, "days_after_completion_if_no_noc")), \
            f"{completion.isoformat()} + {P(rule_id, 'days_after_completion_if_no_noc')} days"
    else:
        return "Without a preliminary notice you can still claim on the payment bond by written notice to the surety " \
               "and bond principal; give completion_date or noc_recorded_date for the deadline."
    ctx.item("Payment bond claim notice (no preliminary notice given)", dl.isoformat(), why, rule_id)
    return (f"No preliminary notice: you can still claim on the payment bond by written notice to the surety and the "
            f"bond principal by {dl.isoformat()}.")


def _spn_public(ctx, res, role, d, agency):
    ctx.use(PUB_SPN, PUB_FILE, PUB_NO_LIEN, SERVICE_METHODS, SPN_CONTENTS)
    res.required_contents = ["signed and verified by the claimant"] + P(SPN_CONTENTS, "fields")
    res.may_give_to = ["the public entity (filed with its disbursing officer)"]
    ctx.note("No mechanics lien is available on public property; the stop payment notice and the payment bond are the "
             "remedies.")
    cands, base_txt = [], None
    acceptance, cess = d["completion_date"], d["cessation_of_labor_date"]
    completion = base = min([x for x in (acceptance, cess) if x], default=None)
    if completion:
        base_txt = "completion/acceptance" if completion == acceptance else "cessation of labor"
        res.completion_date_used = completion.isoformat()
        ctx.note("Public works completion means acceptance by the public entity (or cessation of labor). The rulebook "
                 "does not define cessation for public works; the date you gave was used.")
    elif d["last_furnishing_date"]:
        base, base_txt = d["last_furnishing_date"], "your last furnishing (completion unknown; safe, earlier choice)"
    days90 = P(PUB_SPN, "days_after_completion_or_cessation_if_no_notice")
    if base:
        cands.append((base + timedelta(days=days90), f"{days90} days after {base_txt}"))
        ctx.item("Deadline if no notice is recorded", cands[-1][0].isoformat(), f"{base.isoformat()} + {days90} days",
                 PUB_SPN)
    noc = d["noc_recorded_date"]
    if noc:
        days30 = P(PUB_SPN, "days_after_recorded_notice")
        cands.append((noc + timedelta(days=days30), f"{days30} days after the notice of "
                                                    + "/".join(P(PUB_SPN, "notice_types")) + " was recorded"))
        ctx.item("Deadline after a recorded notice", cands[-1][0].isoformat(), f"{noc.isoformat()} + {days30} days",
                 PUB_SPN)
        res.notice_of_completion_effective = None
        ctx.note("The rulebook has no rule on when a public entity's notice of completion is ineffective; the earliest "
                 "applicable deadline is used.")
    deadline, rule_txt = min(cands)
    res.deadline, res.deadline_rule_applied = deadline.isoformat(), rule_txt
    ctx.item("Last day to give the stop payment notice", deadline.isoformat(), f"earliest of: {rule_txt}", PUB_SPN)
    office = P(PUB_FILE, "state_project") if agency == "state" else "; or ".join(P(PUB_FILE, "other_public_entity"))
    res.where_to_serve = [f"File with: {office}"]
    res.service_methods = [f"{P(PUB_FILE, 'mail_ok')} (same effect as personal service)", "personal delivery"]
    res.what_to_send.insert(0, f"File a signed, verified stop payment notice with the {office} by {deadline.isoformat()}.")
    ctx.use(PUB_BOND_NOTICE, PUB_BOND_SUIT, PUB_BOND_REQ)
    ctx.note(f"A payment bond is required only on public works contracts with an expenditure in excess of "
             f"{_usd(P(PUB_BOND_REQ, 'threshold_dollars'))} (rule {PUB_BOND_REQ}). Ask the public entity for a copy of "
             "the direct contractor's payment bond and the surety's name and address.")
    suit = _add_months(deadline, P(PUB_BOND_SUIT, "months_after_stop_notice_period"))
    ctx.item("Payment bond suit deadline", suit.isoformat(),
             f"{deadline.isoformat()} (end of the notice period) + {P(PUB_BOND_SUIT, 'months_after_stop_notice_period')}"
             " months", PUB_BOND_SUIT)
    b = [f"Payment bond: sue the surety no later than {suit.isoformat()}."]
    if not d["preliminary_notice_served_date"] and role != "laborer":
        b.append(_bond_notice_line(ctx, PUB_BOND_NOTICE, noc, completion))
    res.payment_bond = b


# ---------------------------------------------------------------- MCP tools
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    D = "ISO date, e.g. 2026-11-25"

    @mcp.tool(name="prompt_payment_check", annotations=ro)
    def _prompt_payment_check(
        project_type: Annotated[Project, Field(description="private or public (a public entity is the owner)")],
        payer_role: Annotated[Payer, Field(description="Who pays: owner (or public entity), direct_contractor, subcontractor")],
        payee_role: Annotated[Payee, Field(description="Who is paid: direct_contractor (paid by the owner), subcontractor, supplier")],
        amount_due_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Progress payment owed to the payee for this billing (include any disputed part)")] = None,
        payment_due_date: Annotated[Optional[str], Field(description=f"{D}. Due date AGREED IN WRITING in the contract/subcontract/PO, if any. Overrides the statutory default where the statute allows (Civ 8800, B&P 7108.5)")] = None,
        payment_received_by_payer_date: Annotated[Optional[str], Field(description=f"{D}. Day the paying contractor received the progress payment covering the payee's work (starts the 7-day B&P 7108.5 / PCC 10262 clock)")] = None,
        paid_date: Annotated[Optional[str], Field(description=f"{D}. Day the progress payment was actually paid; omit if unpaid")] = None,
        retention_amount_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Retention held from the payee that is to be released")] = None,
        completion_date: Annotated[Optional[str], Field(description=f"{D}. Completion of the whole work of improvement (public: acceptance). Starts the owner's retention clock")] = None,
        noc_recorded_date: Annotated[Optional[str], Field(description=f"{D}. Day a notice of completion was recorded (used when completion_date is unknown)")] = None,
        disputed_amount_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Part of the progress payment in good-faith dispute (0 or omit if none)")] = 0.0,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Evaluate as of this date; default today in California")] = None,
        payment_request_date: Annotated[Optional[str], Field(description=f"{D}. Owner payments only: day the direct contractor gave its pay application / payment demand (private Civ 8800) or the agency received a proper payment request (PCC 20104.50/10261.5)")] = None,
        retention_received_by_payer_date: Annotated[Optional[str], Field(description=f"{D}. Pass-through only: day the paying contractor received the retention from above")] = None,
        retention_paid_date: Annotated[Optional[str], Field(description=f"{D}. Day the retention was actually paid; omit if unpaid")] = None,
        retention_disputed_amount_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Part of the retention in good-faith dispute (0 or omit if none)")] = 0.0,
        public_agency: Annotated[Agency, Field(description="Public jobs: local (city, county, district: PCC 20104.50) or state (state agency: PCC 10261.5, 10262)")] = "local",
    ) -> PromptPaymentResult:
        """California prompt payment and retention release check for one payer -> payee relationship.

        Picks the governing statute: private owner -> direct contractor progress payments (Civil Code 8800, 30 days
        after the payment demand), retention (Civil Code 8812, 45 days after completion); contractor -> subcontractor
        progress payments (B&P 7108.5, 7 days after the payer receives payment; PCC 10262 on state contracts) and
        retention pass-through (Civil Code 8814, 10 days private; PCC 7107(d), 7 days public); public entity ->
        prime progress payments (PCC 20104.50 local / 10261.5 state, 30 days, legal-rate interest) and retention
        (PCC 7107(c), 60 days after completion). Give amount_due_usd and/or retention_amount_usd with the dates you
        know. Returns one obligation per payment with: governing_law, due_date and its basis, the most that may be
        withheld for a good-faith dispute (150% where the statute says), status (not_yet_due / paid_on_time /
        paid_late / overdue / due_date_unknown), days_late, the late charge (2% per month penalty prorated by day,
        plus the every-started-month reading, or legal-rate interest) with the arithmetic, and what_to_send. A due
        date on a weekend or California court holiday gets a note with the prior business day (and, if late, the
        payer's next-business-day argument). paid_date / retention_paid_date may not be after as_of; disputed and paid
        fields need their amount. Also line_items with a rule citation each, verified, unverified_rule_ids and notes.
        Not legal advice."""
        return prompt_payment_check(project_type, payer_role, payee_role, amount_due_usd, payment_due_date,
                                    payment_received_by_payer_date, paid_date, retention_amount_usd, completion_date,
                                    noc_recorded_date, disputed_amount_usd, as_of, payment_request_date=payment_request_date,
                                    retention_received_by_payer_date=retention_received_by_payer_date,
                                    retention_paid_date=retention_paid_date,
                                    retention_disputed_amount_usd=retention_disputed_amount_usd,
                                    public_agency=public_agency)

    @mcp.tool(name="retention_cap_check", annotations=ro)
    def _retention_cap_check(
        contract_price_usd: Annotated[float, Field(gt=0, allow_inf_nan=False, description="Contract or subcontract price")],
        retention_pct: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, description="Retention percentage withheld, e.g. 10 for 10%")],
        contract_date: Annotated[str, Field(description=f"{D}. Day the contract/subcontract was entered into")],
        project_type: Annotated[Project, Field(description="private (Civil Code 8811, SB 61) or public (PCC 7201)")],
        exemptions: Annotated[Optional[list[Exemption]], Field(description=(
            "Exemptions you claim: residential_not_mixed_use (private; give stories), "
            "sub_failed_to_furnish_bond_after_written_notice (subcontract only), substantially_complex_finding (public)"))] = None,
        stories: Annotated[Optional[int], Field(ge=1, description="Number of stories (for the residential exemption)")] = None,
        contract_tier: Annotated[Literal["prime_contract", "subcontract"], Field(description="prime_contract (owner/public entity -> direct contractor) or subcontract")] = "prime_contract",
        prime_contract_retention_pct: Annotated[Optional[float], Field(ge=0, le=100, allow_inf_nan=False, description="Subcontracts: retention % in the prime contract (flow-down check)")] = None,
        payment_amount_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="This pay application's amount, to compute the most retention allowed on it")] = None,
        retention_held_to_date_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Retention already withheld on this contract (checked against the total cap)")] = 0.0,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
    ) -> RetentionCapResult:
        """Check a retention percentage against California's 5% caps: private contracts entered on/after 2026-01-01
        (Civil Code 8811, SB 61) and public works (PCC 7201). Applies the exemptions you claim only when their
        conditions are met (residential not mixed-use within the story limit; subcontractor failed to furnish a bond
        after written notice; agency's pre-bid 'substantially complex' finding). Returns cap_applies, cap_pct,
        compliant, max_total_retention_usd, requested_total_retention_usd, excess_retention_usd, the most retention
        allowed on this payment (5% of it, limited by what is left under the total cap after retention_held_to_date_usd;
        holding more than the total cap is flagged non-compliant), the subcontract flow-down result, what_to_do,
        line_items with citations and verified. Not legal advice."""
        return retention_cap_check(contract_price_usd, retention_pct, contract_date, project_type, exemptions, as_of,
                                   stories=stories, contract_tier=contract_tier,
                                   prime_contract_retention_pct=prime_contract_retention_pct,
                                   payment_amount_usd=payment_amount_usd,
                                   retention_held_to_date_usd=retention_held_to_date_usd)

    @mcp.tool(name="select_lien_waiver", annotations=ro)
    def _select_lien_waiver(
        payment_type: Annotated[Literal["progress", "final"], Field(description="progress (a pay app) or final (last payment / closeout)")],
        payment_status: Annotated[Literal["conditional", "unconditional"], Field(description="conditional (payment not yet received) or unconditional (payment received and cleared)")],
        payment_received: Annotated[Optional[bool], Field(description="Has the claimant actually received cleared funds? False with unconditional -> do not sign")] = None,
        open_change_order_claims_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Unpaid or disputed extras/change-order claims to preserve")] = None,
        home_improvement: Annotated[bool, Field(description="Home improvement contract with a homeowner (adds B&P 7159 release rules)")] = False,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
    ) -> LienWaiverResult:
        """Choose the California statutory lien waiver and release: Conditional/Unconditional Waiver and Release on
        Progress/Final Payment (Civil Code 8132, 8134, 8136, 8138). Returns the form name and section, when it may be
        used, when it takes effect, whether it is safe to sign now (and the form to use instead if not), the blanks
        to fill, the exceptions printed on the form, a verbatim excerpt of its notice, the official CSLB form URL
        (the rulebook does not hold the full form text: use the official form verbatim), the conditional ->
        payment -> unconditional sequence, and warnings. Not legal advice."""
        return select_lien_waiver(payment_type, payment_status, payment_received, open_change_order_claims_usd,
                                  home_improvement, as_of)

    @mcp.tool(name="stop_payment_notice_deadline", annotations=ro)
    def _stop_payment_notice_deadline(
        project_type: Annotated[Project, Field(description="private or public works")],
        claimant_role: Annotated[Claimant, Field(description="subcontractor, supplier, equipment_lessor, laborer or direct_contractor")],
        completion_date: Annotated[Optional[str], Field(description=f"{D}. Completion of the whole job (public: acceptance by the entity)")] = None,
        noc_recorded_date: Annotated[Optional[str], Field(description=f"{D}. Day a notice of completion/cessation (public: completion, acceptance or cessation) was recorded")] = None,
        last_furnishing_date: Annotated[Optional[str], Field(description=f"{D}. Day you last furnished labor or materials (safe fallback if completion is unknown)")] = None,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
        cessation_of_labor_date: Annotated[Optional[str], Field(description=f"{D}. Day all labor on the job stopped, if it stopped")] = None,
        first_furnishing_date: Annotated[Optional[str], Field(description=f"{D}. Day you FIRST furnished (preliminary notice check)")] = None,
        preliminary_notice_served_date: Annotated[Optional[str], Field(description=f"{D}. Day you served your preliminary notice; omit if never served")] = None,
        claim_amount_usd: Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description="Amount you are owed (for the bonded lender notice)")] = None,
        construction_lender: Annotated[bool, Field(description="Private: is there a construction lender (a recorded construction deed of trust)? A direct contractor's only stop payment notice is to the lender")] = False,
        payment_bond_recorded: Annotated[bool, Field(description="Private: has a payment bond been recorded?")] = False,
        spn_given_date: Annotated[Optional[str], Field(description=f"{D}. Day you gave the stop payment notice, if already given")] = None,
        noc_copy_received: Annotated[Optional[bool], Field(description="Private: did the owner send you a copy of the recorded notice within the statutory time after recording? Unknown -> assumed yes (safe). The copy is owed only to the direct contractor and claimants that gave the owner a preliminary notice")] = None,
        public_agency: Annotated[Agency, Field(description="Public: state (file with the department director) or local")] = "local",
        residential_4_or_fewer_units: Annotated[Optional[bool], Field(description="Private: is the property a residence with four or fewer units? Its owner may be exempt from the copy rule, so a missing copy only extends your deadline when this is false. Unknown -> treated as possibly exempt (safe)")] = None,
    ) -> StopPaymentNoticeResult:
        """California stop payment notice deadline. Private works: the notice must be given before the claimant's
        lien recording period ends (30 days after a valid recorded notice of completion/cessation, or 90 days after
        completion; 60/90 for a direct contractor's lender notice). Public works (Civil Code 9356): 30 days after a
        recorded notice of completion/acceptance/cessation, or 90 days after completion/cessation. When a notice's
        validity is uncertain (recorded before the completion date you give, possibly a notice of cessation, or a
        missing owner copy that may not count) the earlier date is used and the later one is given in notes. Returns the
        deadline, days_remaining, risk_level, who it may be given to, where and how to serve it, required contents,
        the preliminary-notice check (deadline, on time, covers_work_from, covers_any_of_your_work, and the work a notice
        served today would cover), the bonded lender notice amount (125%), the window to sue on the notice, payment
        bond deadlines, what_to_send, line_items with citations and verified. applicable=false means no stop payment
        notice is available (direct contractor on public works, or on private works with no construction lender);
        what_to_send then says what to do instead. When the deadline has passed, or the preliminary notice covers none
        of the claimant's work, what_to_send says so instead of listing notices to give. A deadline on a weekend or
        California court holiday gets a note with the prior business day. Most Civil Code rules here could not be
        checked against leginfo, so results are usually verified=false: act well before the date. Not legal
        advice."""
        return stop_payment_notice_deadline(project_type, claimant_role, completion_date, noc_recorded_date,
                                            last_furnishing_date, as_of, cessation_of_labor_date=cessation_of_labor_date,
                                            first_furnishing_date=first_furnishing_date,
                                            preliminary_notice_served_date=preliminary_notice_served_date,
                                            claim_amount_usd=claim_amount_usd, construction_lender=construction_lender,
                                            payment_bond_recorded=payment_bond_recorded, spn_given_date=spn_given_date,
                                            noc_copy_received=noc_copy_received, public_agency=public_agency,
                                            residential_4_or_fewer_units=residential_4_or_fewer_units)
