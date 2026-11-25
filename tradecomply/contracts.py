"""Home improvement contracts: validate, generate, and check change orders (California B&P 7159-7159.14, 7191).

Every legal value comes from the verified rulebook (core.rule(...)). The one rule the rulebook does not hold is the
Civil Code 1689.5(e) definition of "business day" that the cancellation deadline counts in; it lives in UNVERIFIED
below and any result that uses it has verified=False.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import core, logic

R = core.rule
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
CENT = Decimal("0.01")
MAX_USD = Decimal("1000000000")  # input sanity limit ($1 billion), not a legal value
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
STRICT = ConfigDict(extra="forbid")  # a misspelled checklist key must fail loudly, not read as "not checked"

UNVERIFIED = {
    "business_day": {
        "citation": "Civil Code 1689.5(e) (business day for the home solicitation cancellation rules that "
                    "B&P 7159(c)(3)(A) points to)",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1689.5",
        "text": "\"Business day\" means any calendar day except Sunday, or the following business holidays: New "
                "Year's Day, Washington's Birthday, Memorial Day, Independence Day, Labor Day, Columbus Day, "
                "Veterans' Day, Thanksgiving Day, and Christmas Day.",
        "non_business_weekdays": [6],  # Python weekday(): 6 = Sunday. Saturdays ARE business days.
        "holidays": [
            {"name": "New Year's Day", "month": 1, "day": 1},
            {"name": "Washington's Birthday", "month": 2, "weekday": 0, "nth": 3},
            {"name": "Memorial Day", "month": 5, "weekday": 0, "nth": -1},
            {"name": "Independence Day", "month": 7, "day": 4},
            {"name": "Labor Day", "month": 9, "weekday": 0, "nth": 1},
            {"name": "Columbus Day", "month": 10, "weekday": 0, "nth": 2},
            {"name": "Veterans' Day", "month": 11, "day": 11},
            {"name": "Thanksgiving Day", "month": 11, "weekday": 3, "nth": 4},
            {"name": "Christmas Day", "month": 12, "day": 25},
        ],
        "holiday_dates_source": "https://www.law.cornell.edu/uscode/text/5/6103",
        "conservative_observed_days": True,
        "why_unverified": "Civil Code 1689.5 is not printed in the 2026 CSLB Law Book and leginfo blocks automated "
                          "reading, so this definition was not checked against official text.",
    },
}

# ---- values from the rulebook (each one has a citation)
HIC_THRESHOLD_USD = Decimal(str(R("hic-contract-price-threshold-500").param("threshold_dollars")))
SR_MAX_USD = Decimal(str(R("service-repair-contract-definition").param("max_contract_amount_dollars")))
DOWN_CAP_USD = Decimal(str(R("downpayment-cap-1000-or-10-percent").param("max_downpayment_dollars")))
DOWN_CAP_PCT = Decimal(str(R("downpayment-cap-1000-or-10-percent").param("max_downpayment_percent")))
HIC_DAYS = {"three_day": R("three-day-right-to-cancel-notice").param("cancel_business_days_standard"),
            "five_day_senior": R("senior-five-day-right-to-cancel").param("cancel_business_days"),
            "seven_day_disaster": R("seven-day-right-to-cancel-disaster").param("cancel_business_days")}
SR_DAYS = {"standard": R("service-repair-right-to-cancel-notice").param("post_start_cancel_business_days_standard"),
           "senior": R("service-repair-right-to-cancel-notice").param("post_start_cancel_business_days_senior"),
           "disaster": R("service-repair-right-to-cancel-notice").param("post_start_cancel_business_days_disaster")}
SENIOR_AGE = R("senior-five-day-right-to-cancel").param("buyer_age_min")
SENIOR_FROM = date.fromisoformat(R("senior-five-day-right-to-cancel").param("applies_to_contracts_entered_on_or_after"))
SR_SENIOR_FROM = date.fromisoformat(R("service-repair-right-to-cancel-notice").param("senior_applies_to_contracts_on_or_after"))
REFUND_DAYS = R("cancellation-refund-10-days-goods-20-days").param("refund_deadline_days")
PICKUP_DAYS = R("cancellation-refund-10-days-goods-20-days").param("goods_pickup_deadline_days")
ARB_MAX_UNITS = R("residential-arbitration-clause-title-and-format").param("max_units")
NO_SECURITY_MAX_USD = Decimal(str(R("no-real-property-security-5000-or-less").param("threshold_dollars")))
FINE_MIN = R("hic-payment-violations-misdemeanor").param("fine_min_dollars")
FINE_MAX = R("hic-payment-violations-misdemeanor").param("fine_max_dollars")
JAIL_YEARS = R("hic-payment-violations-misdemeanor").param("jail_max_years")
SR_REFUND_DAYS = R("service-repair-right-to-cancel-notice").param("refund_days")
SR_PICKUP_DAYS = R("service-repair-right-to-cancel-notice").param("goods_pickup_days")
# type sizes (points) used in labels and print notes
BODY_PT = R("hic-legibility-typeface-minimums").param("min_body_font_pt")
HEAD_PT = R("hic-legibility-typeface-minimums").param("min_heading_font_pt")
HI_HEAD_PT = R("hic-heading-home-improvement").param("min_font_pt")
COPY_PT = R("hic-entitled-to-copy-statement").param("min_font_pt")
DOWN_PT = R("hic-downpayment-disclosure").param("min_font_pt")
PROG_PT = R("hic-progress-payment-schedule").param("statement_min_font_pt")
CSLB_PT = R("hic-cslb-information-notice").param("min_font_pt")
CANCEL_PT = R("right-to-cancel-notice-format").param("min_font_pt")
SR_HEAD_PT = R("service-repair-heading").param("min_font_pt")
SR_NOTICE_PT = R("service-repair-buyer-requirements-notice").param("min_font_pt")
SR_COPY_PT = R("service-repair-entitled-to-copy-notice").param("min_font_pt")
SR_CANCEL_PT = R("service-repair-right-to-cancel-notice").param("min_font_pt")
ARB_PT = R("residential-arbitration-clause-title-and-format").param("min_font_pt_black")
ARB_RED_PT = R("residential-arbitration-clause-title-and-format").param("min_font_pt_red")
DOWN_STATEMENT = R("hic-downpayment-disclosure").param("required_text")
# B&P 7125 (version repealed 2028-01-01): these classifications can never use the no-employee exemption
WC_ALWAYS_CLASSES = R("workers-comp-exemption-until-2028").param("classifications_always_require_wc")
WC_EXEMPTION_ENDS = date.fromisoformat(R("wc-exemption-ends-2028").param("operative_date"))


def _effective(rule_id: str) -> Optional[date]:
    m = re.match(r"\d{4}-\d{2}-\d{2}", R(rule_id).effective or "")
    return date.fromisoformat(m.group()) if m else None


AB1327_FROM = _effective("hic-contractor-email-required")          # first-page email + help phone
SB517_FROM = _effective("hic-subcontractor-disclosure-on-request")   # subcontractor Yes/No + disclaimer

ATTORNEY = ("California-licensed attorney: review the contract before use and write the terms the statute does not "
            "supply (warranty, permits and who pays for them, delays, allowances, termination, dispute resolution).")
ATTORNEY_ARB = ("California-licensed attorney: draft the arbitration provision itself. B&P 7191 fixes only its title, "
                "type and the NOTICE; a defective clause may not be enforceable against anyone but you.")

Status = Literal["ok", "violation", "warning", "not_checked", "not_applicable"]


# ---------------------------------------------------------------- models
class Finding(BaseModel):
    item: str
    status: Status
    detail: str
    rule_id: str
    citation: Optional[str] = None
    verified: bool = False


class ProgressPayment(BaseModel):
    model_config = STRICT
    amount_usd: float = Field(ge=0, description="Payment amount in dollars and cents")
    for_work: str = Field("", description="The work, services, materials or equipment this payment is for, e.g. "
                                          "'Rough plumbing complete and inspected'")
    value_of_work_usd: Optional[float] = Field(None, ge=0, description="Optional: your value of the work/materials "
                                               "finished in this phase, to test that payments never run ahead of work")


class PaymentLine(BaseModel):
    number: int
    amount_usd: float
    for_work: str
    cumulative_progress_paid_usd: float
    cumulative_value_of_work_usd: Optional[float] = None
    status: Status
    detail: str


class DownPaymentCheck(BaseModel):
    applies: bool
    contract_price_usd: float
    percent_cap_usd: float
    fixed_cap_usd: float
    max_allowed_usd: float
    down_payment_usd: float
    excess_usd: float
    ok: bool
    math: str


class CancellationPeriod(BaseModel):
    applies: bool
    kind: Literal["three_day", "five_day_senior", "seven_day_disaster", "service_and_repair", "none"]
    notice_title: Optional[str] = None
    business_days: Optional[int] = None
    received_signed_copy_on: Optional[str] = None
    last_day_to_cancel: Optional[str] = Field(None, description="The buyer may cancel until midnight at the end of this date")
    earliest_safe_start_date: Optional[str] = Field(None, description="First date work can start without the "
                                                    "cancellation risk (service and repair: the day the buyer has the "
                                                    "signed copy)")
    open_as_of: Optional[bool] = None
    days_counted: list[str] = Field(default_factory=list)
    days_skipped: list[str] = Field(default_factory=list)
    refund_due_days_after_notice: int = REFUND_DAYS
    goods_pickup_days_after_notice: int = PICKUP_DAYS
    explanation: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    uses_unverified_business_day_rule: bool = False


class CancellationResult(core.Result):
    cancellation: CancellationPeriod
    unverified_sources: list[dict] = Field(default_factory=list)
    as_of: str


class ContractIncludes(BaseModel):
    """What the written contract contains. True = present, False = missing, None/omitted = not checked."""
    model_config = STRICT
    type_size_10pt_text_and_bold_headings: Optional[bool] = Field(None, description=f"Legible; text >= {BODY_PT}-pt, headings >= {HEAD_PT}-pt bold")
    signed_dated_copy_to_buyer_before_work: Optional[bool] = Field(None, description="Buyer gets a copy signed and dated by both sides before work starts")
    first_page_buyer_sign_date: Optional[bool] = Field(None, description="First page shows the date the buyer signed")
    first_page_cancellation_address_and_email: Optional[bool] = Field(None, description="First page: contractor name, address AND email for the Notice of Cancellation, right after a statement that it may be sent there")
    first_page_cancellation_help_phone: Optional[bool] = Field(None, description="First page: phone number to help the buyer with the Notice of Cancellation")
    contractor_name_address_license: Optional[bool] = Field(None, description="Contractor name, business address and license number")
    heading_home_improvement: Optional[bool] = Field(None, description=f"Heading 'Home Improvement' in >= {HI_HEAD_PT}-pt bold")
    entitled_to_copy_statement: Optional[bool] = Field(None, description=f"{COPY_PT}-pt bold 'You are entitled to a completely filled in copy...' statement")
    contract_price_heading: Optional[bool] = Field(None, description="'Contract Price' heading with the amount in dollars and cents")
    finance_charge_heading: Optional[bool] = Field(None, description="'Finance Charge' heading, separate from the price (only if financed)")
    description_heading: Optional[bool] = Field(None, description="Project description heading with description of significant materials and equipment")
    pool_plan_and_scale_drawing: Optional[bool] = Field(None, description="Swimming pools only: plan and scale drawing with shape, size, dimensions, construction and equipment specs")
    downpayment_heading_and_amount: Optional[bool] = Field(None, description="'Downpayment' heading and the amount")
    downpayment_statement: Optional[bool] = Field(None, description=f"{DOWN_PT}-pt bold '{DOWN_STATEMENT}' statement")
    progress_schedule_heading: Optional[bool] = Field(None, description="'Schedule of Progress Payments' heading")
    progress_payment_statement: Optional[bool] = Field(None, description=f"{PROG_PT}-pt bold 'IT IS AGAINST THE LAW FOR A CONTRACTOR TO COLLECT PAYMENT FOR WORK NOT YET COMPLETED...' statement")
    substantial_commencement_statement: Optional[bool] = Field(None, description="Statement of what counts as substantial commencement of work")
    approximate_start_date_heading: Optional[bool] = Field(None, description="'Approximate Start Date' heading and date")
    approximate_completion_date_heading: Optional[bool] = Field(None, description="'Approximate Completion Date' heading and date")
    incorporated_documents_list: Optional[bool] = Field(None, description="'List of Documents to be Incorporated into the Contract' (needed if plans/specs are part of the deal)")
    note_about_extra_work: Optional[bool] = Field(None, description="'Note About Extra Work and Change Orders' with the statutory statement")
    subcontractor_yes_no: Optional[bool] = Field(None, description="Subcontractors [__] Yes [__] No checkbox")
    subcontractor_disclaimer: Optional[bool] = Field(None, description="If Yes: the verbatim subcontractor-list disclaimer")
    lien_release_statement: Optional[bool] = Field(None, description="Statement that an unconditional lien release follows each satisfactory payment")
    change_order_form: Optional[bool] = Field(None, description="Change-order form incorporated into the contract")
    bond_right_notice: Optional[bool] = Field(None, description="Near the signatures: owner may require a performance and payment bond")
    cgl_notice: Optional[bool] = Field(None, description="'Commercial General Liability Insurance (CGL)' notice with the true statement")
    workers_comp_notice: Optional[bool] = Field(None, description="'Workers' Compensation Insurance' notice with the true statement")
    extra_work_buyer_notice: Optional[bool] = Field(None, description="Notice to buyer about extra/change-order work (B&P 7159(e)(3))")
    mechanics_lien_warning: Optional[bool] = Field(None, description="Verbatim 'Mechanics Lien Warning'")
    cslb_information_notice: Optional[bool] = Field(None, description=f"'Information about the Contractors State License Board (CSLB)' notice in >= {CSLB_PT}-pt")
    right_to_cancel_notice: Optional[bool] = Field(None, description=f"Right-to-cancel notice: {CANCEL_PT}-pt bold, next to owner's signature, signed and dated by owner, in the sales-pitch language")
    notice_of_cancellation_form: Optional[bool] = Field(None, description="Completed 'Notice of Cancellation' form in duplicate, attached and easily detachable")
    cgl_disclosure_form_872: Optional[bool] = Field(None, description="CSLB 'Information About Commercial General Liability Insurance' form with the bid and contract (16 CCR 872(a))")
    # service and repair (B&P 7159.10) only
    written_same_language: Optional[bool] = Field(None, description="Service & repair: in writing, same language as the sales presentation")
    date_signed: Optional[bool] = Field(None, description="Service & repair: date the contract was signed")
    heading_service_and_repair: Optional[bool] = Field(None, description=f"Service & repair: 'Service and Repair' in >= {SR_HEAD_PT}-pt bold")
    service_repair_requirements_notice: Optional[bool] = Field(None, description=f"Service & repair: {SR_NOTICE_PT}-pt bold 'Notice to the Buyer' with the four requirements, signed and dated by buyer")
    service_repair_entitled_to_copy_notice: Optional[bool] = Field(None, description=f"Service & repair: {SR_COPY_PT}-pt bold 'entitled to a completely filled in and signed copy' notice")
    estimated_contract_price_tm: Optional[bool] = Field(None, description="Service & repair, time and materials: 'Estimated Contract Price', set rate, estimated materials, time increments and the not-to-exceed statement")
    replaced_parts_statement_and_checkbox: Optional[bool] = Field(None, description="Service & repair: replaced-parts statement and 'OK for contractor to take replaced parts' checkbox")
    service_charge_heading: Optional[bool] = Field(None, description="Service & repair: 'Amount of Service Charge' and the one-service-charge statement (if charged)")


class ContractValidation(core.Result):
    contract_type: Literal["home_improvement", "service_and_repair", "under_7159_threshold"]
    compliant: bool
    violation_count: int
    warning_count: int
    not_checked_count: int
    summary: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    cancellation: CancellationPeriod
    down_payment: Optional[DownPaymentCheck] = None
    payment_schedule: list[PaymentLine] = Field(default_factory=list)
    required_professionals: list[str] = Field(default_factory=list)
    unverified_sources: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    as_of: str


# ---------------------------------------------------------------- helpers
DATE_RANGE = (date(1990, 1, 1), date(2100, 12, 31))  # input sanity limit, not a legal value


def _dt(value, field: str) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        value = value.date()
    d = value if isinstance(value, date) else logic._date(str(value), field)
    if not DATE_RANGE[0] <= d <= DATE_RANGE[1]:
        raise ValueError(f"{field} must be between {DATE_RANGE[0]} and {DATE_RANGE[1]}, got {d}")
    return d


def _money(x, field: str = "amount") -> Decimal:
    """Dollars to cents, half up (the usual commercial rounding). Rejects NaN, infinity and absurd sizes."""
    d = Decimal(str(x))
    if not d.is_finite() or abs(d) > MAX_USD:
        raise ValueError(f"{field} must be a dollar amount up to {MAX_USD:,}, got {x!r}")
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _usd(x) -> str:
    d = _money(x)
    return f"{'-' if d < 0 else ''}${abs(d):,.2f}"


def _long(d: Optional[date]) -> Optional[str]:
    """'November 27, 2026': how dates are written on the homeowner's copy (no platform-specific strftime flags)."""
    return f"{d:%B} {d.day}, {d.year}" if d else None


def _f(item: str, status: Status, detail: str, rule_id: str) -> Finding:
    if rule_id.startswith("UNVERIFIED:"):
        u = UNVERIFIED[rule_id.split(":", 1)[1]]
        return Finding(item=item, status=status, detail=detail, rule_id=rule_id, citation=u["citation"])
    r = R(rule_id)
    return Finding(item=item, status=status, detail=detail, rule_id=rule_id, citation=r.citation_text,
                   verified=r.verified)


def _finish(res: core.Result, rule_ids, unverified: bool) -> core.Result:
    res.cite(*dict.fromkeys(i for i in rule_ids if not i.startswith("UNVERIFIED:")))
    if unverified:
        res.verified = False
    return res


# ---------------------------------------------------------------- business days and cancellation
def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    if nth > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=nth - 1)
    d = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _holidays(year: int) -> dict[date, str]:
    spec, out = UNVERIFIED["business_day"], {}
    for h in spec["holidays"]:
        d = date(year, h["month"], h["day"]) if "day" in h else _nth_weekday(year, h["month"], h["weekday"], h["nth"])
        out.setdefault(d, h["name"])
        if spec["conservative_observed_days"] and "day" in h and d.weekday() in (5, 6):
            obs = d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1)
            out.setdefault(obs, f"{h['name']} (observed; skipped to be safe)")
    return out


def non_business_reason(d: date) -> Optional[str]:
    """Why d is not a business day under Civil Code 1689.5(e) (UNVERIFIED), or None if it is one."""
    if d.weekday() in UNVERIFIED["business_day"]["non_business_weekdays"]:
        return "Sunday"
    for y in (d.year - 1, d.year, d.year + 1):  # observed days can cross a year boundary
        if d in (h := _holidays(y)):
            return h[d]
    return None


def add_business_days(start: date, n: int) -> tuple[date, list[str], list[str]]:
    """The nth business day AFTER start (start itself never counts), with the days counted and skipped."""
    counted, skipped, d = [], [], start
    while len(counted) < n:
        d += timedelta(days=1)
        why = non_business_reason(d)
        if why:
            skipped.append(f"{d.isoformat()} {d:%a} ({why})")
        else:
            counted.append(f"{d.isoformat()} {d:%a} (business day {len(counted) + 1})")
    return d, counted, skipped


def _cancellation(received: Optional[date], senior: bool, disaster: bool, at_place_of_business: bool,
                  service_and_repair: bool, today: date, contract_date: Optional[date] = None) -> CancellationPeriod:
    contract_date = contract_date or received or today
    refund, pickup, refund_rule = REFUND_DAYS, PICKUP_DAYS, "cancellation-refund-10-days-goods-20-days"
    if service_and_repair:
        kind, rid = "service_and_repair", "service-repair-right-to-cancel-notice"
        refund, pickup, refund_rule = SR_REFUND_DAYS, SR_PICKUP_DAYS, rid
        senior_ok = senior and contract_date >= SR_SENIOR_FROM
        days = SR_DAYS["disaster" if disaster else "senior" if senior_ok else "standard"]
        p = CancellationPeriod(applies=True, kind=kind, notice_title="YOUR RIGHTS TO CANCEL BEFORE WORK BEGINS",
                               business_days=days, rule_ids=[rid], refund_due_days_after_notice=refund,
                               goods_pickup_days_after_notice=pickup)
        p.explanation += [
            "Before work starts, the buyer may cancel until BOTH (1) they have a copy signed and dated by both sides "
            "and (2) you start work.",
            f"After work starts, the buyer may cancel within {days} business days only if a listed reason occurred: "
            f"price over {_usd(SR_MAX_USD)}, the buyer did not call you, you sold more than the problem needed, or "
            "payment was due or taken before the work was complete. The notice counts from signing in one place and "
            "from receipt of the signed copy in another; we count from receipt, the later date."]
        if senior and not disaster:
            p.explanation.append(f"A buyer is {SENIOR_AGE} or older: 'three' becomes 'five' in the notice."
                                 if senior_ok else "The five-day senior period applies only to contracts entered into "
                                 f"on or after {SR_SENIOR_FROM}; this contract is older.")
    elif disaster:
        kind, rid, days = "seven_day_disaster", "seven-day-right-to-cancel-disaster", HIC_DAYS["seven_day_disaster"]
        p = CancellationPeriod(applies=True, kind=kind, notice_title="Seven-Day Right to Cancel", business_days=days,
                               rule_ids=[rid, "right-to-cancel-notice-format", "notice-of-cancellation-form-duplicate"])
        p.explanation.append("Repair or restoration of a home damaged in a declared state or local emergency: the "
                             f"{days}-business-day notice applies, even if the deal was signed at your office.")
    elif at_place_of_business:
        return CancellationPeriod(
            applies=False, kind="none", rule_ids=["three-day-right-to-cancel-notice"],
            explanation=["Negotiated at your place of business, so B&P 7159(e)(6) does not require the Three-Day "
                         "(or Five-Day) Right to Cancel notice. Keep proof of where it was negotiated."])
    else:
        senior_ok = senior and contract_date >= SENIOR_FROM
        kind = "five_day_senior" if senior_ok else "three_day"
        days = HIC_DAYS[kind]
        p = CancellationPeriod(applies=True, kind=kind, business_days=days,
                               notice_title="Five-Day Right to Cancel" if senior_ok else "Three-Day Right to Cancel",
                               rule_ids=["three-day-right-to-cancel-notice", "right-to-cancel-notice-format",
                                         "notice-of-cancellation-form-duplicate"]
                               + (["senior-five-day-right-to-cancel"] if senior else []))
        if senior_ok:
            p.explanation.append(f"A buyer is {SENIOR_AGE} or older, so the period is {days} business days. With "
                                 "several buyers and one a senior, use the five-day notice (conservative reading).")
        elif senior:
            p.explanation.append(f"The five-day senior period applies only to contracts entered into on or after "
                                 f"{SENIOR_FROM.isoformat()}; this contract is older.")
    if refund_rule not in p.rule_ids:
        p.rule_ids.append(refund_rule)
    if received:
        last, counted, skipped = add_business_days(received, days)
        p.received_signed_copy_on = received.isoformat()
        p.last_day_to_cancel = last.isoformat()
        p.earliest_safe_start_date = (last + timedelta(days=1)).isoformat()
        p.open_as_of = today <= last
        p.days_counted, p.days_skipped = counted, skipped
        p.uses_unverified_business_day_rule = True
        p.explanation.append(
            f"Count {days} business days starting the day after the buyer received the signed copy "
            f"({received.isoformat()}). Business days are every day except Sundays and the nine Civil Code 1689.5(e) "
            "holidays, so Saturdays count. When a fixed-date holiday falls on a weekend we also skip the nearby "
            f"weekday, which can only make the deadline later. Deadline: midnight at the end of {last.isoformat()}.")
        refund_text = (f"If the buyer cancels on time you must return everything they paid within {refund} days of "
                       "receiving the notice; if they make delivered goods available and you do not pick them up "
                       f"within {pickup} days of the date of their notice, they may keep them.")
        if service_and_repair:
            # 7159.10(e)(12)(A): the before-work right ends once the buyer has the signed copy AND work starts
            p.earliest_safe_start_date = received.isoformat()
            p.explanation.append(
                f"You may start work as soon as the buyer has the signed copy ({received.isoformat()}); starting "
                f"ends the before-work right to cancel. The after-start right through {last.isoformat()} exists only "
                "if one of the four reasons above occurred, which means the job was not a true service and repair "
                f"job. {refund_text}")
        else:
            p.explanation.append(
                f"{refund_text} Starting work or delivering goods before {p.earliest_safe_start_date} puts that at "
                "risk; Civil Code 1689.5-1689.15 (not in our rulebook) govern early starts, so ask your attorney "
                "before starting early.")
        p.explanation.append("Before you mobilize, check email, fax and mail: a notice the buyer sent by the deadline "
                             "can reach you after it (when a mailed notice takes effect is a Civil Code question not "
                             "in our rulebook).")
    else:
        p.explanation.append("Give the date the buyer received the signed and dated copy to compute the deadline.")
    return p


def cancellation_deadline(received_signed_copy_on: Optional[str] = None, buyer_age_65_or_older: bool = False,
                          disaster_repair: bool = False, negotiated_at_contractor_place_of_business: bool = False,
                          service_and_repair: bool = False, as_of=None) -> CancellationResult:
    today = _dt(as_of, "as_of") or logic.today_ca()
    p = _cancellation(_dt(received_signed_copy_on, "received_signed_copy_on"), buyer_age_65_or_older,
                      disaster_repair, negotiated_at_contractor_place_of_business, service_and_repair, today)
    res = CancellationResult(cancellation=p, as_of=today.isoformat(),
                             unverified_sources=[UNVERIFIED["business_day"]] if p.uses_unverified_business_day_rule else [])
    return _finish(res, p.rule_ids, p.uses_unverified_business_day_rule)


# ---------------------------------------------------------------- money rules
def down_payment_limit(contract_price_usd: float, down_payment_usd: float = 0.0,
                       bonded_or_joint_control: bool = False) -> DownPaymentCheck:
    """B&P 7159.5(a)(3): down payment <= the lesser of the fixed cap or the percentage of the contract price."""
    price, down = _money(contract_price_usd, "contract_price_usd"), _money(down_payment_usd, "down_payment_usd")
    if price < 0 or down < 0:
        raise ValueError("contract_price_usd and down_payment_usd must not be negative")
    pct = (price * DOWN_CAP_PCT / 100).quantize(CENT, rounding=ROUND_DOWN)  # round down: never overstate the cap
    cap = min(DOWN_CAP_USD, pct)
    excess = max(Decimal(0), down - cap)
    math = (f"{DOWN_CAP_PCT}% of {_usd(price)} = {_usd(pct)}; the lesser of {_usd(DOWN_CAP_USD)} and {_usd(pct)} "
            f"is {_usd(cap)}. Down payment {_usd(down)}" + (f" is {_usd(excess)} over the cap." if excess else
                                                            " is within the cap."))
    if bonded_or_joint_control:
        math += " Not applied: a contractor furnishing a qualifying bond or joint control is exempt (B&P 7159.5(a)(8))."
    return DownPaymentCheck(applies=not bonded_or_joint_control, contract_price_usd=float(price),
                            percent_cap_usd=float(pct), fixed_cap_usd=float(DOWN_CAP_USD), max_allowed_usd=float(cap),
                            down_payment_usd=float(down), excess_usd=float(excess),
                            ok=bonded_or_joint_control or not excess, math=math)


# Wording that reads like money before the work. Not "sign-off"/"signed off" (an inspector's approval), not
# "start-up" (equipment commissioning), not "change order"/"work order", and "before"/"prior to" only before the work.
_ADVANCE = re.compile(r"\b(sign(?:ing|ed|ature|s)?(?![\s-]*off)|execut\w*|start(?![\s-]?up\b)\w*|commenc\w*|mobiliz\w*|"
                      r"(?:before|prior to)\s+(?:any\s+|the\s+)?(?:work|start|job|project|delivery|install\w*)|"
                      r"in advance|advance|deposit|up ?front|retainer|(?<!change )(?<!change-)(?<!work )order\w*|booking|reserv\w*)\b",
                      re.I)
_CALENDAR = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(/\d{2,4})?|january|february|march|april|may \d{1,2}|june|"
                       r"july|august|september|october|november|december|week\w*|month\w*|days? after|every)\b", re.I)
# A date next to a finished-work milestone ("10 days after final completion") is still tied to the work.
_WORK_DONE = re.compile(r"\b(complet\w*|finish\w*|installed|delivered|inspect\w*|passed|done|signed off|sign-?off)\b",
                        re.I)


def check_payment_schedule(contract_price_usd: float, down_payment_usd: float, payments: list[ProgressPayment],
                           bonded_or_joint_control: bool = False) -> tuple[list[PaymentLine], list[Finding]]:
    """B&P 7159(d)(9) and 7159.5(a)(4)-(5): each payment in dollars tied to work, never ahead of the work."""
    price, down = _money(contract_price_usd), _money(down_payment_usd)
    findings, lines, cum_paid, cum_value = [], [], Decimal(0), Decimal(0)
    have_values = bool(payments) and all(p.value_of_work_usd is not None for p in payments)
    if not bonded_or_joint_control and not have_values and any(p.value_of_work_usd is not None for p in payments):
        findings.append(_f("Payments vs. value of work", "not_checked", "value_of_work_usd is given for only some "
                           "payments; give it for every payment to test that payments never run ahead of the work.",
                           "no-payment-ahead-of-work"))
    if bonded_or_joint_control and payments:
        findings.append(_f("Progress payments", "not_applicable", "You furnish a qualifying bond or joint control, so "
                           "the schedule and no-payment-ahead-of-work rules do not apply and you may take payment "
                           "before completion. The contractor may not have any interest in the joint control.",
                           "bonded-contractor-payment-exemption"))
    for i, p in enumerate(payments, 1):
        amt = _money(p.amount_usd, f"progress payment #{i} amount_usd")
        cum_paid += amt
        status, why = "ok", []
        work = (p.for_work or "").strip()
        if not bonded_or_joint_control:
            if not work:
                status = "violation"
                why.append("does not say what work, services or materials it pays for (B&P 7159(d)(9)(B))")
            elif _ADVANCE.search(work):
                status = "warning"
                why.append(f"'{work}' reads like payment before the work is done or materials are delivered; tie it "
                           "to finished work or delivered materials")
            elif _CALENDAR.search(work) and not _WORK_DONE.search(work):
                status = "warning"
                why.append(f"'{work}' is tied to a date, not to finished work")
            if have_values:
                cum_value += _money(p.value_of_work_usd, f"progress payment #{i} value_of_work_usd")
                if cum_paid > cum_value:
                    status = "violation"
                    why.append(f"progress payments through #{i} total {_usd(cum_paid)} but the work finished by then "
                               f"is worth {_usd(cum_value)}: {_usd(cum_paid - cum_value)} ahead of the work")
        lines.append(PaymentLine(number=i, amount_usd=float(amt), for_work=work,
                                 cumulative_progress_paid_usd=float(cum_paid),
                                 cumulative_value_of_work_usd=float(cum_value) if have_values else None,
                                 status="not_applicable" if bonded_or_joint_control else status,
                                 detail="; ".join(why) or ("exempt (bonded)" if bonded_or_joint_control else "tied to work")))
        if status != "ok" and not bonded_or_joint_control:
            rid = "hic-progress-payment-schedule" if not work else "no-payment-ahead-of-work"
            findings.append(_f(f"Progress payment #{i}", status, "; ".join(why), rid))
    if payments and not bonded_or_joint_control:
        total = down + cum_paid
        if total > price:
            findings.append(_f("Payments vs. contract price", "violation",
                               f"Down payment {_usd(down)} + progress payments {_usd(cum_paid)} = {_usd(total)}, which "
                               f"is {_usd(total - price)} more than the contract price {_usd(price)}. The price is the "
                               "value of all the work, so the excess is payment for work that will never be done.",
                               "no-payment-ahead-of-work"))
        elif total < price:
            findings.append(_f("Payments vs. contract price", "warning",
                               f"Down payment {_usd(down)} + progress payments {_usd(cum_paid)} = {_usd(total)}; "
                               f"{_usd(price - total)} of the {_usd(price)} price is not scheduled. CSLB's worksheet "
                               "expects the schedule to add up to price minus down payment; list the final payment "
                               "and the work it is for.", "hic-payment-schedule-worksheet-check"))
        else:
            findings.append(_f("Payments vs. contract price", "ok",
                               f"{_usd(down)} down + {_usd(cum_paid)} progress = {_usd(price)}, the contract price.",
                               "hic-progress-payment-schedule"))
    return lines, findings


# ---------------------------------------------------------------- validator
_HIC_ITEMS = [  # (includes key, label, rule id, condition)
    ("type_size_10pt_text_and_bold_headings", f"Legible; text >= {BODY_PT}-pt and headings >= {HEAD_PT}-pt bold", "hic-legibility-typeface-minimums", None),
    ("signed_dated_copy_to_buyer_before_work", "Copy signed and dated by both sides to buyer before work", "lawbook_p381-430:hic-signed-copy-before-work", None),
    ("first_page_buyer_sign_date", "First page: date the buyer signed", "hic-first-page-cancellation-contact", None),
    ("first_page_cancellation_address_and_email", "First page: name, address and email for the Notice of Cancellation, preceded by the 'may be sent to' statement", "hic-contractor-email-required", "ab1327_addr"),
    ("first_page_cancellation_help_phone", "First page: phone number to help with the Notice of Cancellation", "hic-notice-of-cancellation-phone", "ab1327"),
    ("contractor_name_address_license", "Contractor name, business address, license number", "hic-contractor-and-salesperson-identification", None),
    ("heading_home_improvement", f"Heading 'Home Improvement' ({HI_HEAD_PT}-pt bold)", "hic-heading-home-improvement", None),
    ("entitled_to_copy_statement", f"{COPY_PT}-pt bold 'You are entitled to a completely filled in copy...'", "hic-entitled-to-copy-statement", None),
    ("contract_price_heading", "'Contract Price' in dollars and cents", "hic-contract-price-heading", None),
    ("finance_charge_heading", "'Finance Charge' set out separately", "hic-finance-charge-separate", "finance"),
    ("description_heading", "'Description of the Project and Description of the Significant Materials to be Used and Equipment to be Installed'", "hic-project-description-heading", None),
    ("pool_plan_and_scale_drawing", "Pool plan and scale drawing (shape, size, dimensions, construction and equipment specs)", "hic-project-description-heading", "pool"),
    ("downpayment_heading_and_amount", "'Downpayment' heading and amount", "hic-downpayment-disclosure", "down"),
    ("downpayment_statement", f"{DOWN_PT}-pt bold '{DOWN_STATEMENT}'", "hic-downpayment-disclosure", "down_unbonded"),
    ("progress_schedule_heading", "'Schedule of Progress Payments' heading", "hic-progress-payment-schedule", "progress_unbonded"),
    ("progress_payment_statement", f"{PROG_PT}-pt bold progress-payment statement", "hic-progress-payment-schedule", "progress_unbonded"),
    ("substantial_commencement_statement", "What counts as substantial commencement of work", "hic-commencement-and-start-date", None),
    ("approximate_start_date_heading", "'Approximate Start Date' and date", "hic-commencement-and-start-date", None),
    ("approximate_completion_date_heading", "'Approximate Completion Date' and date", "hic-approximate-completion-date", None),
    ("incorporated_documents_list", "'List of Documents to be Incorporated into the Contract'", "hic-list-of-incorporated-documents", "docs"),
    ("note_about_extra_work", "'Note About Extra Work and Change Orders' statement", "hic-note-about-extra-work", None),
    ("subcontractor_yes_no", "Subcontractors [__] Yes [__] No", "hic-subcontractor-disclosure-checkbox", "sb517"),
    ("subcontractor_disclaimer", "Subcontractor-list disclaimer (verbatim)", "hic-subcontractor-disclosure-checkbox", "subs_yes"),
    ("lien_release_statement", "Unconditional lien release after each satisfactory payment", "hic-lien-release-statement", None),
    ("change_order_form", "Change-order form incorporated", "hic-change-order-form-incorporated", None),
    ("bond_right_notice", "Bond-right notice near the signatures", "hic-bond-right-notice", None),
    ("cgl_notice", "Commercial General Liability Insurance (CGL) notice", "hic-cgl-insurance-notice", None),
    ("workers_comp_notice", "Workers' Compensation Insurance notice", "hic-workers-comp-notice", None),
    ("extra_work_buyer_notice", "Notice to buyer about extra/change-order work", "hic-change-order-buyer-notice", None),
    ("mechanics_lien_warning", "Mechanics Lien Warning (verbatim)", "hic-mechanics-lien-warning", "unbonded"),
    ("cslb_information_notice", f"CSLB information notice ({CSLB_PT}-pt)", "hic-cslb-information-notice", None),
    ("right_to_cancel_notice", f"Right-to-cancel notice ({CANCEL_PT}-pt bold, by owner's signature, signed and dated, sales-pitch language)", "right-to-cancel-notice-format", "cancel"),
    ("notice_of_cancellation_form", "Completed Notice of Cancellation in duplicate, easily detachable", "notice-of-cancellation-form-duplicate", "cancel"),
    ("cgl_disclosure_form_872", "16 CCR 872(a) 'Information About Commercial General Liability Insurance' form", "cgl-insurance-disclosure-home-improvement", None),
]
_SR_ITEMS = [
    ("written_same_language", "In writing, same language as the sales presentation", "service-repair-writing-language-copy", None),
    ("type_size_10pt_text_and_bold_headings", f"Legible; text >= {BODY_PT}-pt and headings >= {HEAD_PT}-pt bold", "service-repair-writing-language-copy", None),
    ("signed_dated_copy_to_buyer_before_work", "Copy signed and dated by buyer and contractor before work", "service-repair-writing-language-copy", None),
    ("contractor_name_address_license", "Contractor name, business address, license number", "service-repair-contractor-info-and-date", None),
    ("date_signed", "Date the contract was signed", "service-repair-contractor-info-and-date", None),
    ("cgl_notice", "Commercial General Liability Insurance (CGL) notice", "service-repair-cgl-wc-notices", None),
    ("workers_comp_notice", "Workers' Compensation Insurance notice", "service-repair-cgl-wc-notices", None),
    ("heading_service_and_repair", f"'Service and Repair' ({SR_HEAD_PT}-pt bold)", "service-repair-heading", None),
    ("service_repair_requirements_notice", f"{SR_NOTICE_PT}-pt bold 'Notice to the Buyer' (four requirements), signed and dated by buyer", "service-repair-buyer-requirements-notice", None),
    ("service_repair_entitled_to_copy_notice", f"{SR_COPY_PT}-pt bold 'entitled to a completely filled in and signed copy' notice", "service-repair-entitled-to-copy-notice", None),
    ("incorporated_documents_list", "'List of Documents to be Incorporated into the Contract'", "service-repair-price-headings", "docs"),
    ("contract_price_heading", "'Contract Price' (fixed price)", "service-repair-price-headings", "fixed"),
    ("finance_charge_heading", "'Finance Charge' set out separately", "service-repair-price-headings", "finance"),
    ("estimated_contract_price_tm", "'Estimated Contract Price', rate, est. materials, time increments, not-to-exceed statement", "service-repair-time-and-materials", "tm"),
    ("description_heading", "'Description of the Project and Materials to be Used and Equipment to be Installed'", "service-repair-description-heading", None),
    ("replaced_parts_statement_and_checkbox", "Replaced-parts statement and checkbox", "service-repair-replaced-parts", None),
    ("service_charge_heading", "'Amount of Service Charge' and one-charge statement", "service-repair-one-service-charge", "service_charge"),
    ("right_to_cancel_notice", f"'YOUR RIGHTS TO CANCEL BEFORE WORK BEGINS' ({SR_CANCEL_PT}-pt bold, by buyer's signature, signed and dated)", "service-repair-right-to-cancel-notice", None),
    ("cgl_disclosure_form_872", "16 CCR 872(a) 'Information About Commercial General Liability Insurance' form", "cgl-insurance-disclosure-home-improvement", "sr872"),
]


def _checklist(items, inc: ContractIncludes, cond: dict[str, tuple]) -> list[Finding]:
    """cond[c] = (applies, why_not_applicable[, soft]): when `soft` is set, a missing item is a warning, not a violation."""
    out = []
    for key, label, rid, c in items:
        val = getattr(inc, key)
        applies, na_text, soft = (tuple(cond[c]) + (None,))[:3] if c else (True, "", None)
        if not applies:
            out.append(_f(label, "not_applicable", na_text, rid))
        elif val is True:
            out.append(_f(label, "ok", "present", rid))
        elif val is False and soft:
            out.append(_f(label, "warning", soft, rid))
        elif val is False:
            out.append(_f(label, "violation", "Missing from the contract.", rid))
        else:
            out.append(_f(label, "not_checked", "Not confirmed. Check the contract and set this item.", rid))
    return out


def _license_codes(classifications) -> set[str]:
    """'C-39 Roofing', 'c39', 'C 39', 'C-61/D-49' -> {'C-39'}, {'C-39'}, {'C-39'}, {'C-61', 'D-49'}."""
    return {f"{m.group(1).upper()}-{int(m.group(2))}"
            for c in classifications or [] for m in re.finditer(r"\b([abcd])[-\s]?(\d{1,2})\b", str(c), re.I)}


def _wc_exemption_problem(classifications, on: date) -> Optional[tuple[str, str]]:
    """Why a 'no employees and is exempt' workers' comp statement would be false, as (message, rule id), or None."""
    if on >= WC_EXEMPTION_ENDS:
        return (f"The no-employee workers' comp exemption ended {WC_EXEMPTION_ENDS} for every license except a "
                "no-employee joint venture: a workers' comp certificate must be on file and the contract must say "
                "you carry workers' compensation.", "wc-exemption-ends-2028")
    bad = sorted(_license_codes(classifications) & set(WC_ALWAYS_CLASSES))
    if bad:
        return (f"License classification {', '.join(bad)} can never use the no-employee workers' comp exemption "
                f"(only licenses without {', '.join(WC_ALWAYS_CLASSES)} can): carry workers' comp and use the "
                "'carries workers' compensation insurance' statement.", "workers-comp-exemption-until-2028")
    return None


def _sr_total_problem(price: Decimal, service_charge_usd: float) -> Optional[str]:
    """B&P 7159.14(a)(3): the service and repair amount is the entire cost, so price + service charge must fit."""
    total = price + _money(service_charge_usd, "service_charge_usd")
    if total <= SR_MAX_USD:
        return None
    return (f"{_usd(price)} + {_usd(service_charge_usd)} service charge = {_usd(total)}, over {_usd(SR_MAX_USD)}. "
            "The contract amount must include the entire cost (B&P 7159.14(a)(3)); if the service charge is billed on "
            "top of the price, the job may not qualify as service and repair. Include it in the price or use a full "
            "home improvement contract.")


def validate_home_improvement_contract(
        contract_price_usd: float, down_payment_usd: float = 0.0,
        progress_payments: Optional[list[ProgressPayment]] = None, buyer_age_65_or_older: bool = False,
        has_salesperson: bool = False, salesperson_his_number: Optional[str] = None,
        includes_arbitration_clause: bool = False, arbitration_formatted_per_7191: bool = False,
        contractor_has_payment_bond_or_joint_control: bool = False, includes: Optional[ContractIncludes] = None,
        is_service_and_repair: bool = False, residential_units: int = 1, start_date: Optional[str] = None,
        completion_date: Optional[str] = None, as_of=None, *, buyer_received_signed_copy_on: Optional[str] = None,
        finance_charge_usd: float = 0.0, subcontractors_will_be_used: Optional[bool] = None,
        disaster_repair: bool = False, negotiated_at_contractor_place_of_business: bool = False,
        is_swimming_pool: bool = False, buyer_initiated_contact: Optional[bool] = None,
        only_work_needed_for_the_problem: Optional[bool] = None, time_and_materials: bool = False,
        service_charge_usd: float = 0.0, contract_signed_on: Optional[str] = None,
        workers_comp_notice_says_no_employees: bool = False,
        license_classifications: Optional[list[str]] = None) -> ContractValidation:
    """Check a California home improvement (or service and repair) contract against B&P 7159-7159.14 and 7191."""
    amounts = {k: _money(v, k) for k, v in (("contract_price_usd", contract_price_usd),
                                            ("down_payment_usd", down_payment_usd),
                                            ("finance_charge_usd", finance_charge_usd),
                                            ("service_charge_usd", service_charge_usd))}
    if negative := [k for k, v in amounts.items() if v < 0]:
        raise ValueError(f"{', '.join(negative)} must not be negative")
    price, down = amounts["contract_price_usd"], amounts["down_payment_usd"]
    if price == 0:
        raise ValueError("contract_price_usd must be more than $0.00: give the contract price in dollars and cents")
    if residential_units < 1:
        raise ValueError("residential_units must be at least 1 (home improvement rules cover residential property)")
    today = _dt(as_of, "as_of") or logic.today_ca()
    received = _dt(buyer_received_signed_copy_on, "buyer_received_signed_copy_on")
    start, finish = _dt(start_date, "start_date"), _dt(completion_date, "completion_date")
    signed = _dt(contract_signed_on, "contract_signed_on")
    if signed and received and received < signed:
        raise ValueError(f"buyer_received_signed_copy_on ({received}) is before contract_signed_on ({signed}): the "
                         "buyer cannot receive a signed copy before the contract is signed; check both dates")
    contract_date = signed or received or today
    payments = [p if isinstance(p, ProgressPayment) else ProgressPayment(**p) for p in progress_payments or []]
    inc = includes if isinstance(includes, ContractIncludes) else ContractIncludes(**(includes or {}))
    bonded = contractor_has_payment_bond_or_joint_control
    findings, notes, rids = [], [], []
    pros = [ATTORNEY]

    # ---- which contract rules apply
    ctype = "home_improvement"
    if is_service_and_repair:
        tests = [(price <= SR_MAX_USD, f"Price {_usd(price)} must be {_usd(SR_MAX_USD)} or less"),
                 (buyer_initiated_contact is True, "The buyer must have contacted you to request the work"
                  + ("" if buyer_initiated_contact is False else " (not confirmed)")),
                 (only_work_needed_for_the_problem is True, "You may sell only what the problem needs"
                  + ("" if only_work_needed_for_the_problem is False else " (not confirmed)")),
                 (not down and not payments, "No payment may be due or accepted until the work is completed")]
        for ok, text in tests:
            findings.append(_f("Service and repair condition", "ok" if ok else "violation", text,
                               "service-repair-contract-definition"))
        if all(ok for ok, _ in tests):
            ctype = "service_and_repair"
            findings.append(_f("Contract form", "ok", "All four conditions met: only the B&P 7159.10 notices apply "
                               "and B&P 7159 does not.", "hic-requirements-exemptions"))
        else:
            findings.append(_f("Contract form", "violation", "Presented as service and repair but a condition fails, "
                               "so the full home improvement rules of B&P 7159(c)-(e), including cancellation rights, "
                               "apply regardless of price.", "service-repair-nonconforming-full-hic-rules"))
    elif price <= HIC_THRESHOLD_USD:
        ctype = "under_7159_threshold"
        findings.append(_f("Contract form", "not_applicable",
                           f"Price {_usd(price)} does not exceed {_usd(HIC_THRESHOLD_USD)}, so the B&P 7159 contract "
                           "form is not required. Prices on all contracts for the same job are added together, so "
                           "splitting a job does not avoid it.", "hic-contract-price-threshold-500"))
        findings.append(_f("Payment rules still apply", "ok", "B&P 7159.5 covers every home improvement contract as "
                           "defined in B&P 7151.2, which has no price floor, so the down payment cap and the "
                           "no-payment-ahead-of-work rule still apply.", "home-improvement-contract-definition"))
        notes.append("B&P 7159.5(a)(1) also covers small jobs: put the contract in writing with the agreed amount in "
                     "dollars and cents (profit, labor and materials included, finance charges excluded).")
        rids.append("hic-contract-price-heading")
        notes.append("Below the B&P 7159 threshold the statutory cancellation notice is not required, but Civil Code "
                     "1689.5-1689.14 home-solicitation cancellation rights (not in our rulebook) may still apply to "
                     "sales made at the home; the period shown is computed as if they do.")

    cond = {
        "finance": (finance_charge_usd > 0, "No finance charge."),
        "pool": (is_swimming_pool, "Not a swimming pool."),
        "down": (down > 0, "No down payment."),
        "down_unbonded": (down > 0 and not bonded, "No down payment." if not down else
                          "Bonded/joint-control contractor may omit this statement (B&P 7159.5(a)(8))."),
        "progress_unbonded": (bool(payments) and not bonded, "No progress payments listed." if not payments else
                              "Bonded/joint-control contractor may omit the progress-payment details (B&P 7159.5(a)(8))."),
        "docs": (True, "", "Missing. Required whenever plans, specs or other documents are part of the contract; "
                 "list them."),
        "ab1327": (not AB1327_FROM or contract_date >= AB1327_FROM,
                   f"Added by AB 1327 for contracts from {AB1327_FROM}; this contract is dated earlier."),
        "ab1327_addr": (True, "", None if not AB1327_FROM or contract_date >= AB1327_FROM else
                        f"AB 1327 added the email address for contracts from {AB1327_FROM}, so a missing email is not "
                        "a violation on this older contract. The earlier text (not in our rulebook) still required "
                        "the contractor's name and address for the Notice of Cancellation on the first page, after a "
                        "statement that it may be sent there: confirm those are present."),
        "sb517": (not SB517_FROM or contract_date >= SB517_FROM,
                  f"Added by SB 517 for contracts from {SB517_FROM}; this contract is dated earlier."),
        "subs_yes": (subcontractors_will_be_used is not False and (not SB517_FROM or contract_date >= SB517_FROM),
                     "No subcontractors will be used." if subcontractors_will_be_used is False else
                     f"Added by SB 517 for contracts from {SB517_FROM}.",
                     "Required only if the subcontractor box is checked Yes; say whether subcontractors will be "
                     "used." if subcontractors_will_be_used is None else None),
        "sr872": (True, "", "16 CCR 872(a) requires this form with every home improvement contract as defined in "
                  "B&P 7151.2, which includes service and repair work; B&P 7159.10(c) limits only the B&P 7159 "
                  "notices, so attach the form to be safe."),
        "unbonded": (not bonded, "Bonded/joint-control contractor may omit the Mechanics Lien Warning (B&P 7159.5(a)(8))."),
        "cancel": (disaster_repair or not negotiated_at_contractor_place_of_business,
                   f"Negotiated at your place of business: the {HIC_DAYS['three_day']}/{HIC_DAYS['five_day_senior']}-"
                   "day notice is not required."),
        "fixed": (not time_and_materials, "Time-and-materials contract: uses 'Estimated Contract Price' instead."),
        "tm": (time_and_materials, "Fixed-price contract."),
        "service_charge": (service_charge_usd > 0, "No service charge."),
    }

    cancel = _cancellation(received, buyer_age_65_or_older, disaster_repair, negotiated_at_contractor_place_of_business,
                           ctype == "service_and_repair", today, contract_date)
    if ctype == "under_7159_threshold" and cancel.applies:
        cancel.explanation.insert(0, "B&P 7159 does not require its cancellation notice at this price. The period "
                                  "below assumes the Civil Code home-solicitation right to cancel applies (a sale made "
                                  "away from your place of business); confirm with your attorney.")
    dp, lines = None, []
    if ctype == "home_improvement":
        findings += _checklist(_HIC_ITEMS, inc, cond)
        rids.append("hic-subcontractor-disclosure-on-request")  # source of the SB 517 effective date
        if subcontractors_will_be_used is None and cond["sb517"][0]:
            notes.append("Say whether subcontractors will be used: if Yes, the disclaimer must be in the contract and "
                         "on every change order, and you must hand over the sub list (name, contact, license number, "
                         "classification) on request.")
        if has_salesperson:
            ok = bool((salesperson_his_number or "").strip())
            findings.append(_f("Salesperson name and HIS registration number", "ok" if ok else "violation",
                               "present" if ok else "A home improvement salesperson solicited or negotiated this "
                               "contract, so their name and CSLB registration number must be on it.",
                               "hic-salesperson-name-registration"))
            notes.append("If the salesperson's commission is paid out of the contract price, pay it pro rata with the "
                         "payments you receive (B&P 7159.5(a)(7)).")
            rids.append("salesperson-commission-pro-rata")
        if not start:
            findings.append(_f("Approximate start date", "warning", "No start date given to check; the contract must "
                               "show the approximate date work will begin.", "hic-commencement-and-start-date"))
        if not finish:
            findings.append(_f("Approximate completion date", "warning", "No completion date given to check; the "
                               "contract must show the approximate completion date.", "hic-approximate-completion-date"))
        if start and finish and finish < start:
            findings.append(_f("Dates", "violation", f"Completion {finish} is before start {start}.",
                               "hic-approximate-completion-date"))
        has_copy = received or signed
        if start and has_copy and start < has_copy:
            findings.append(_f("Work before the buyer has the signed copy", "violation",
                               f"Work is set to start {start}, before the buyer "
                               + ("received the signed copy" if received else "signed") + f" ({has_copy}). No work "
                               "may start until the buyer has a copy signed and dated by both of you"
                               + (f"; to also clear the cancellation period, start on or after "
                                  f"{cancel.earliest_safe_start_date}." if cancel.earliest_safe_start_date else "."),
                               "lawbook_p381-430:hic-signed-copy-before-work"))
        elif start and cancel.last_day_to_cancel and start.isoformat() <= cancel.last_day_to_cancel:
            findings.append(_f("Start date vs. cancellation period", "warning",
                               f"Work is set to start {start} while the buyer can still cancel (through "
                               f"{cancel.last_day_to_cancel}). Start on or after {cancel.earliest_safe_start_date}.",
                               "cancellation-refund-10-days-goods-20-days"))
        if is_swimming_pool:
            keys = ["lien_release_statement", "change_order_form", "description_heading", "pool_plan_and_scale_drawing"]
            keys += ["downpayment_heading_and_amount", "downpayment_statement"] if down and not bonded else []
            keys += ["progress_schedule_heading", "progress_payment_statement"] if payments and not bonded else []
            missing = [k for k in keys if getattr(inc, k) is False]
            findings.append(_f("Swimming pool contract (B&P 7167)", "violation" if missing else "ok",
                               ("Missing " + ", ".join(missing) + ": a pool contract that does not substantially "
                                "comply is void and unenforceable by you.") if missing else
                               "Pool contracts are void if the lien release statement, change-order form, description "
                               "with plan and scale drawing, downpayment disclosure or payment schedule is missing; "
                               "none of the checked items is missing.", "pool-contract-void-noncompliance"))
        if finance_charge_usd > 0 and price <= NO_SECURITY_MAX_USD:
            notes.append(f"Financed contract of {_usd(price)}: at {_usd(NO_SECURITY_MAX_USD)} or less it may not take a "
                         "security interest in the home other than a mechanics lien (B&P 7159.2(a)).")
            rids.append("no-real-property-security-5000-or-less")
    elif ctype == "service_and_repair":
        findings += _checklist(_SR_ITEMS, inc, cond)
        findings.append(_f("Payment timing", "ok", "Payment is due only after the work is complete (conditions fixed "
                           "and, if needed, the building department approved it). On request after payment, give an "
                           "unconditional lien release.", "service-repair-substantive-rules"))
        if time_and_materials:
            notes.append("Time and materials: the final bill may not exceed the estimate without the buyer's written "
                         f"authorization, and going over {_usd(SR_MAX_USD)} takes the job out of service and repair.")
            rids.append("service-repair-time-and-materials")
        if service_charge_usd > 0:
            notes.append("Only one service charge (trip and inspection fees included); disclose it in every ad and "
                         "state the amount when the customer calls.")
            rids.append("service-repair-one-service-charge")
            if over := _sr_total_problem(price, service_charge_usd):
                findings.append(_f("Price plus service charge", "warning", over, "service-repair-substantive-rules"))
    if ctype != "under_7159_threshold" and workers_comp_notice_says_no_employees:
        why = _wc_exemption_problem(license_classifications, contract_date)
        if why:
            findings.append(_f("Workers' comp statement is not correct", "violation", why[0] + " The notice must use "
                               "whichever statement is correct.", why[1]))
            rids.append("hic-workers-comp-notice" if ctype == "home_improvement" else "service-repair-cgl-wc-notices")
        elif finish and finish >= WC_EXEMPTION_ENDS:
            findings.append(_f("Workers' comp exemption ends during the job", "warning",
                               f"The job runs to {finish}, past {WC_EXEMPTION_ENDS}, when the no-employee exemption "
                               "ends; get a workers' comp policy on file before then.", "wc-exemption-ends-2028"))
        if license_classifications is None and not why:
            notes.append("The no-employee exemption is not available to " + ", ".join(WC_ALWAYS_CLASSES) +
                         " licensees; give license_classifications to check it.")
        rids.append("wc-exemption-ends-2028")
        if contract_date < WC_EXEMPTION_ENDS:
            rids.append("workers-comp-exemption-until-2028")

    if ctype != "service_and_repair":
        dp = down_payment_limit(float(price), float(down), bonded)
        rids.append("downpayment-cap-1000-or-10-percent")
        if bonded:
            findings.append(_f("Down payment cap", "not_applicable", dp.math, "bonded-contractor-payment-exemption"))
        elif down:
            findings.append(_f("Down payment cap", "ok" if dp.ok else "violation", dp.math,
                               "downpayment-cap-1000-or-10-percent"))
        lines, pf = check_payment_schedule(float(price), float(down), payments, bonded)
        findings += pf
        if any(f.status == "violation" and f.rule_id in ("downpayment-cap-1000-or-10-percent", "no-payment-ahead-of-work")
               for f in findings):
            notes.append(f"Requesting or taking an oversized down payment or payment ahead of the work is a "
                         f"misdemeanor (B&P 7159.5(b)(1)): fine of {_usd(FINE_MIN)} to {_usd(FINE_MAX)} and/or up to "
                         f"{JAIL_YEARS} year in county jail. In a place damaged by a natural disaster with a Governor "
                         "state of emergency or a Presidential emergency or major-disaster declaration, the court "
                         "must impose the maximum fine.")
            rids.append("hic-payment-violations-misdemeanor")

    if includes_arbitration_clause:
        if residential_units > ARB_MAX_UNITS:
            findings.append(_f("Arbitration clause (B&P 7191)", "not_applicable", f"{residential_units} units: B&P "
                               f"7191 covers residential property with {ARB_MAX_UNITS} or fewer units.",
                               "residential-arbitration-clause-title-and-format"))
        elif arbitration_formatted_per_7191:
            findings.append(_f("Arbitration clause (B&P 7191)", "ok", "Titled ARBITRATION OF DISPUTES, statutory "
                               "NOTICE and acknowledgment right before the initials, in the required type.",
                               "residential-arbitration-notice-and-initials"))
            rids.append("residential-arbitration-clause-title-and-format")
        else:
            findings.append(_f("Arbitration clause (B&P 7191)", "violation", "Needs the title ARBITRATION OF DISPUTES, "
                               f"at least {ARB_PT}-pt roman bold (or {ARB_RED_PT}-pt contrasting red roman bold; capitals "
                               "if typed), and the verbatim NOTICE and 'WE HAVE READ AND UNDERSTAND...' statement right "
                               "before the initials. As written it may not be enforceable against anyone but you.",
                               "noncompliant-arbitration-clause-unenforceable"))
            rids += ["residential-arbitration-clause-title-and-format", "residential-arbitration-notice-and-initials"]
        notes.append("Agreeing to private arbitration means the dispute cannot go to CSLB arbitration (B&P 7085(a)(6)).")
        rids.append("private-arbitration-requires-bp7191-notice")
        pros.append(ATTORNEY_ARB)

    if ctype == "home_improvement":
        findings.append(_f("Prime contractor responsibility", "ok", "You (the prime/direct contractor) answer to CSLB "
                           "for completing the job per the contract, plans and specs; a missing item is cause for "
                           "discipline.", "hic-noncompliance-discipline-prime-responsible"))
    unverified = cancel.uses_unverified_business_day_rule
    counts = {s: sum(f.status == s for f in findings) for s in ("violation", "warning", "not_checked")}
    res = ContractValidation(
        contract_type=ctype, compliant=counts["violation"] == 0, violation_count=counts["violation"],
        warning_count=counts["warning"], not_checked_count=counts["not_checked"],
        summary=[f"{f.status.upper()}: {f.item}: {f.detail} [{f.citation}]" for f in findings
                 if f.status in ("violation", "warning")],
        findings=findings, cancellation=cancel, down_payment=dp, payment_schedule=lines,
        required_professionals=pros, notes=notes, as_of=today.isoformat(),
        unverified_sources=[UNVERIFIED["business_day"]] if unverified else [])
    if counts["not_checked"]:
        res.summary.append(f"NOT CHECKED: {counts['not_checked']} items were not confirmed (status not_checked in "
                           "findings), so compliant covers only what was checked. Confirm each one (most are "
                           "`includes` items: true/false) for a full check.")
    if not (signed or received):
        res.notes.append(f"No contract date given: date-dependent rules (AB 1327 email and phone, SB 517 subcontractor "
                         f"box, senior five-day period) were applied as if the contract was signed {today}. Give "
                         "contract_signed_on for an older contract.")
    return _finish(res, [f.rule_id for f in findings] + cancel.rule_ids + rids, unverified)


# ---------------------------------------------------------------- generator
class ContractFields(BaseModel):
    model_config = STRICT
    contractor_name: Text = Field(description="Business name exactly as on the CSLB license")
    contractor_business_address: Text
    contractor_license_number: Text
    contractor_email: Text = Field(description="Email where the buyer may send the Notice of Cancellation")
    contractor_phone: Text = Field(description="Phone the buyer can call for help with the Notice of Cancellation")
    buyer_names: list[Text] = Field(min_length=1, description="Every buyer who signs; each gets a signature line")
    project_address: Text
    contract_price_usd: float = Field(gt=0, description="Entire price incl. profit, labor and materials; no finance charges")
    project_description: Text
    materials_and_equipment: Text = Field(description="Significant materials to be used and equipment to be installed (brand/model/grade)")
    down_payment_usd: float = Field(0, ge=0)
    progress_payments: list[ProgressPayment] = Field(default_factory=list)
    finance_charge_usd: float = Field(0, ge=0)
    start_date: Optional[str] = Field(None, description="Approximate start date (ISO)")
    completion_date: Optional[str] = Field(None, description="Approximate completion date (ISO)")
    substantial_commencement: Optional[str] = Field(None, description="What counts as substantial commencement, e.g. 'demolition begins on site'")
    incorporated_documents: list[str] = Field(default_factory=list, description="Plans, specs, bids made part of the contract")
    cgl_status: Literal["none", "carries", "self_insured", "llc"] = "carries"
    cgl_insurer_name: Optional[str] = None
    cgl_insurer_phone: Optional[str] = None
    workers_comp_status: Literal["carries", "no_employees_exempt"] = "carries"
    license_classifications: list[str] = Field(default_factory=list, description="CSLB classifications on the license, "
                                               f"e.g. ['B', 'C-39'] ({', '.join(WC_ALWAYS_CLASSES)} can never claim "
                                               "the no-employee workers' comp exemption)")
    subcontractors_will_be_used: bool = False
    buyer_age_65_or_older: bool = False
    salesperson_name: Optional[str] = None
    salesperson_his_number: Optional[str] = None
    contractor_has_payment_bond_or_joint_control: bool = False
    include_arbitration_clause: bool = False
    residential_units: int = Field(1, ge=1)
    disaster_repair: bool = Field(False, description="Repairing damage from a declared state or local emergency "
                                  f"({HIC_DAYS['seven_day_disaster']}-business-day right to cancel)")
    negotiated_at_contractor_place_of_business: bool = False
    sales_language: str = Field("English", description="Language principally used in the oral sales presentation")
    signing_date: Optional[str] = Field(None, description="Date of transaction / date the buyer gets the signed copy "
                                        "(ISO); starts the cancellation clock")
    is_swimming_pool: bool = False
    service_and_repair: bool = Field(False, description="Ask for a B&P 7159.10 service and repair form (used only if all four conditions hold)")
    buyer_initiated_contact: Optional[bool] = None
    only_work_needed_for_the_problem: Optional[bool] = None
    time_and_materials: bool = False
    hourly_rate_usd: Optional[float] = Field(None, ge=0, description="Service and repair, time and materials: set rate per hour")
    estimated_materials_usd: Optional[float] = Field(None, ge=0, description="Service and repair, time and materials: estimated materials cost")
    time_increment: Optional[str] = Field(None, description="e.g. 'quarter hours'")
    service_charge_usd: float = Field(0, ge=0)


class TypeSpec(BaseModel):
    section: str
    minimum: str
    rule_id: str
    citation: Optional[str] = None


class GeneratedContract(core.Result):
    contract_type: Literal["home_improvement", "service_and_repair"]
    markdown: str
    placeholders: list[str] = Field(default_factory=list)
    type_size_notes: list[TypeSpec] = Field(default_factory=list)
    cancellation: CancellationPeriod
    problems: list[str] = Field(default_factory=list)
    required_professionals: list[str] = Field(default_factory=list)
    unverified_sources: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    as_of: str


_NAME_SLOT = re.compile(r"\(The name on the license or [‘']This contractor[’']\)")


def _fill_notice(text: str, name: str, insurer: Optional[str] = None, phone: Optional[str] = None) -> str:
    text = _NAME_SLOT.sub(name, text)
    text = text.replace("(the insurance company or trust company or bank)", insurer or "[[FILL IN: insurer, trust company or bank]]")
    text = text.replace("(the insurance company)", insurer or "[[FILL IN: insurance company]]")
    text = text.replace("(insurance company)", insurer or "[[FILL IN: insurance company]]")
    return re.sub(r"_{3,}", phone or "[[FILL IN: insurer phone]]", text)


def _cgl_text(f: ContractFields, sr: bool) -> str:
    key = {"none": "A", "carries": "B", "self_insured": "C", "llc": "D"}[f.cgl_status]
    if sr:
        opt = R("service-repair-cgl-wc-notices").param("cgl_options_exact")[key]
    else:
        opt = R("hic-cgl-insurance-notice").param("options")[{"A": "A_no_cgl", "B": "B_carries_cgl",
                                                              "C": "C_self_insured", "D": "D_llc"}[key]]
    return _fill_notice(opt, f.contractor_name, f.cgl_insurer_name, f.cgl_insurer_phone)


def _wc_text(f: ContractFields, sr: bool) -> str:
    key = "A" if f.workers_comp_status == "no_employees_exempt" else "B"
    opt = (R("service-repair-cgl-wc-notices").param("wc_options_exact")[key] if sr else
           R("hic-workers-comp-notice").param("options")["A_no_employees" if key == "A" else "B_carries_wc"])
    return _fill_notice(opt, f.contractor_name)


def _form_872(f: ContractFields) -> str:
    t = R("cgl-insurance-disclosure-home-improvement").param("template_text")
    t = t.replace("Pursuant to", "\n\nPursuant to", 1).replace("For more information", "\n\nFor more information", 1)
    for q in ("Did your contractor tell you whether he or she carries Commercial General Liability Insurance?",
              "What does this insurance cover?", "Is this insurance required?",
              "How can you make sure the contractor is insured?", "What about a contractor who is self-insured?"):
        t = t.replace(q, f"\n\n**{q}**", 1)
    head, _, rest = t.partition("\n\n")
    t = f"**{head.strip()}**\n\n{rest}"
    carries = f.cgl_status == "carries" or (f.cgl_status == "llc" and bool(f.cgl_insurer_name))
    t = t.replace("■", "\n\n■").replace("■", "[ ]" if carries else "[X]", 1).replace("■", "[X]" if carries else "[ ]", 1)
    t = t.replace("_______________ does not", f"{f.contractor_name} does not", 1)
    t = t.replace("_______________ carries", f"{f.contractor_name} carries", 1).replace(" (CONTRACTOR’S NAME)", "")
    if carries:
        t = t.replace("____________________________ (COMPANY NAME)", (f.cgl_insurer_name or "[[FILL IN: insurer]]") + ".", 1)
        t = t.replace("___________________ (TELEPHONE NUMBER)", f.cgl_insurer_phone or "[[FILL IN: insurer phone]]", 1)
    return t.strip()


def _notice_of_cancellation(f: ContractFields, kind: str, signed: Optional[date], deadline: Optional[date]) -> str:
    r = R("notice-of-cancellation-form-duplicate")
    t = r.param("required_text")
    if kind == "five_day_senior":
        t = t.replace("three", "five")
    elif kind == "seven_day_disaster":
        t = t.replace("three business days", "seven business days")
    t = (t.replace("[enter date of transaction]", _long(signed) or "[[FILL IN: date of transaction]]")
         .replace("[name of seller]", f.contractor_name)
         .replace("[address of seller’s place of business]", f.contractor_business_address)
         .replace("midnight of [Date]", f"midnight of {_long(deadline) or '[[FILL IN: last day to cancel]]'}")
         .replace("I hereby cancel this transaction. [Date]", "I hereby cancel this transaction. Date: ____________")
         .replace("[Buyer’s signature]", "Buyer’s signature: ______________________________"))
    return t


def _cancel_notice_text(kind: str) -> str:
    if kind == "seven_day_disaster":
        return R("seven-day-right-to-cancel-disaster").param("required_text")
    t = R("three-day-right-to-cancel-notice").param("required_text")
    if kind == "five_day_senior":
        t = t.replace("Three-Day", "Five-Day").replace("three", "five").replace("third", "fifth")
    return t


def _paras(text: str, bold: bool = False) -> str:
    return "\n\n".join(f"**{x.strip()}**" if bold else x.strip() for x in text.split("\n") if x.strip())


def _bold_lines(text: str) -> str:
    return _paras(text, bold=True)


def _sign_lines(f: "ContractFields", what: str = "Signature") -> str:
    return "\n\n".join(f"Buyer {name}: {what} ______________________  Date: ________" for name in f.buyer_names)


def _pc(rule_id: str, what: str) -> str:
    return f"<!-- PRINT: {what} ({R(rule_id).citation_text}) -->"


def generate_home_improvement_contract(fields: ContractFields, as_of=None) -> GeneratedContract:
    """Markdown home improvement (B&P 7159) or service and repair (B&P 7159.10) contract with every required item."""
    f = fields if isinstance(fields, ContractFields) else ContractFields(**fields)
    today = _dt(as_of, "as_of") or logic.today_ca()
    signed = _dt(f.signing_date, "signing_date")
    start, finish = _dt(f.start_date, "start_date"), _dt(f.completion_date, "completion_date")
    price, down = _money(f.contract_price_usd, "contract_price_usd"), _money(f.down_payment_usd, "down_payment_usd")
    fin = _money(f.finance_charge_usd, "finance_charge_usd")
    bonded = f.contractor_has_payment_bond_or_joint_control
    problems, notes, rids, types = [], [], [], []
    pros = [ATTORNEY]

    def ts(section, minimum, rid):
        types.append(TypeSpec(section=section, minimum=minimum, rule_id=rid, citation=R(rid).citation_text))
        rids.append(rid)

    sr = False
    if f.service_and_repair:
        fails = [t for ok, t in [
            (price <= SR_MAX_USD, f"price {_usd(price)} is over {_usd(SR_MAX_USD)}"),
            (f.buyer_initiated_contact is True, "buyer-initiated contact not confirmed"),
            (f.only_work_needed_for_the_problem is True, "'only what the problem needs' not confirmed"),
            (not down and not f.progress_payments, "a payment is due before completion")] if not ok]
        rids += ["service-repair-contract-definition", "service-repair-nonconforming-full-hic-rules"]
        if fails:
            problems.append("Service and repair form not allowed (" + "; ".join(fails) + "), so a full home "
                            "improvement contract was generated (B&P 7159.10(b)).")
        else:
            sr = True
    cancel = _cancellation(signed, f.buyer_age_65_or_older, f.disaster_repair,
                           f.negotiated_at_contractor_place_of_business, sr, today)
    rids += cancel.rule_ids
    wc_issue = (_wc_exemption_problem(f.license_classifications, signed or today)
                if f.workers_comp_status == "no_employees_exempt" else None)

    def wc_block(sr_form: bool) -> list[str]:
        out = ["## Workers’ Compensation Insurance", _wc_text(f, sr_form)]
        if wc_issue:
            out.append(f"[[FIX BEFORE USE: {wc_issue[0]}]]")
            rids.append(wc_issue[1])
        return out

    lic_line = f"**{f.contractor_name}** · {f.contractor_business_address} · CSLB License No. {f.contractor_license_number}"
    buyers = ", ".join(f.buyer_names)
    md: list[str] = []

    if sr:
        wl = R("service-repair-writing-language-copy")
        ts("All text / headings", f"{wl.param('min_body_font_pt')}-pt text, {wl.param('min_heading_font_pt')}-pt bold "
           "headings", wl.id)
        heading = R("service-repair-heading")
        md += [f"# {heading.param('required_heading')}", _pc("service-repair-heading", f"at least {heading.param('min_font_pt')}-pt bold"),
               lic_line, f"**Buyer(s):** {buyers} · **Service address:** {f.project_address}",
               f"**Date this contract was signed:** {_long(signed) or '[[FILL IN: date signed]]'}"]
        rids += ["service-repair-contractor-info-and-date"]
        n = R("service-repair-buyer-requirements-notice")
        md += [_pc(n.id, f"at least {n.param('min_font_pt')}-pt bold"), f"**{n.param('required_text')}**",
               _sign_lines(f)]
        ts("Notice to the Buyer (four requirements)", f"{n.param('min_font_pt')}-pt bold, signed and dated by buyer", n.id)
        c = R("service-repair-entitled-to-copy-notice")
        md += [_pc(c.id, f"at least {c.param('min_font_pt')}-pt bold"), f"**{c.param('required_text')}**"]
        ts("Entitled-to-copy notice", f"{c.param('min_font_pt')}-pt bold", c.id)
        h = R("service-repair-price-headings").param("headings")
        if f.incorporated_documents:
            md += [f"## {h['documents']}"] + [f"- {d}" for d in f.incorporated_documents]
        if f.time_and_materials:
            tm = R("service-repair-time-and-materials")
            md += [f"## {tm.param('required_heading')}", _usd(price),
                   f"Rate: {_usd(f.hourly_rate_usd) + ' per hour' if f.hourly_rate_usd is not None else '[[FILL IN: set rate]]'} · "
                   f"Estimated materials: {_usd(f.estimated_materials_usd) if f.estimated_materials_usd is not None else '[[FILL IN: estimated cost of materials]]'} · "
                   f"Time is computed in increments of {f.time_increment or '[[FILL IN: e.g. quarter hours]]'}.",
                   tm.param("required_text")]
            rids.append(tm.id)
        else:
            md += [f"## {h['fixed_price']}", _usd(price)]
        if fin:
            md += [f"## {h['finance']}", f"{_usd(fin)} (separate from the contract price)"]
        rids.append("service-repair-price-headings")
        d = R("service-repair-description-heading")
        md += [f"## {d.param('required_heading')}", f.project_description, f"Materials and equipment: {f.materials_and_equipment}"]
        p = R("service-repair-replaced-parts")
        md += [p.param("required_text"), f"[ ] {p.param('checkbox_label')}  Buyer's initials: ______"]
        rids += [d.id, p.id]
        if f.service_charge_usd:
            s = R("service-repair-one-service-charge")
            md += [f"## {s.param('required_heading')}", _usd(f.service_charge_usd), s.param("required_text")]
            rids.append(s.id)
            if over := _sr_total_problem(price, f.service_charge_usd):
                problems.append(over)
                rids.append("service-repair-substantive-rules")
        md += ["## Commercial General Liability Insurance (CGL)", _cgl_text(f, True)] + wc_block(True)
        rids.append("service-repair-cgl-wc-notices")
        md += ["## Additional Terms", "[[ATTORNEY: warranty, permits and inspections, and other terms. Keep them "
               "consistent with the four service and repair requirements above.]]",
               "## Signatures", _sign_lines(f) + f"\n\n{f.contractor_name}, by: ______________________  "
               "Date: ________  (contractor or contractor's representative)"]
        rc = R("service-repair-right-to-cancel-notice")
        text = rc.param("required_text")
        if f.buyer_age_65_or_older and (signed or today) >= SR_SENIOR_FROM:
            text = text.replace("three", "five")
        md += [_pc(rc.id, f"at least {rc.param('min_font_pt')}-pt bold, next to the buyer's signature"),
               _bold_lines(text), _sign_lines(f)]
        ts("YOUR RIGHTS TO CANCEL BEFORE WORK BEGINS", f"{rc.param('min_font_pt')}-pt bold, signed and dated by buyer", rc.id)
    else:
        body = R("hic-legibility-typeface-minimums")
        ts("All text / headings", f"{body.param('min_body_font_pt')}-pt text, {body.param('min_heading_font_pt')}-pt "
           "bold headings", body.id)
        hh = R("hic-heading-home-improvement")
        md += [f"# {hh.param('required_heading')}", _pc(hh.id, f"at least {hh.param('min_font_pt')}-pt bold"), lic_line]
        ts("'Home Improvement' heading", f"{hh.param('min_font_pt')}-pt bold", hh.id)
        rids.append("hic-contractor-and-salesperson-identification")
        if f.salesperson_name or f.salesperson_his_number:
            md.append(f"**Home improvement salesperson:** {f.salesperson_name or '[[FILL IN: salesperson name]]'}, "
                      f"HIS Registration No. {f.salesperson_his_number or '[[FILL IN: HIS registration number]]'}")
            if not f.salesperson_his_number:
                problems.append("Salesperson named without an HIS registration number (B&P 7159(d)(2)).")
            rids.append("hic-salesperson-name-registration")
        md += [f"**Buyer(s):** {buyers} · **Project address:** {f.project_address}",
               _pc("hic-first-page-cancellation-contact", "first page, no smaller than the body text"),
               f"**Date buyer signed this contract:** {_long(signed) or '[[FILL IN: date buyer signed]]'}",
               "The “Notice of Cancellation” may be sent to the contractor at the address or email address noted "
               "below:", f"{f.contractor_name}, {f.contractor_business_address}, {f.contractor_email}",
               f"For help locating and filling out the “Notice of Cancellation,” call {f.contractor_phone}."]
        ts("First-page cancellation contact block", "no smaller than the body text, on page 1",
           "hic-first-page-cancellation-contact")
        rids += ["hic-contractor-email-required", "hic-notice-of-cancellation-phone"]
        e = R("hic-entitled-to-copy-statement")
        md += [_pc(e.id, f"at least {e.param('min_font_pt')}-pt bold"), f"**{e.param('required_text')}**"]
        ts("Entitled-to-copy statement", f"{e.param('min_font_pt')}-pt bold", e.id)
        md += [f"## {R('hic-contract-price-heading').param('required_heading')}", _usd(price)]
        rids.append("hic-contract-price-heading")
        if fin:
            md += [f"## {R('hic-finance-charge-separate').param('required_heading')}",
                   f"{_usd(fin)} (set out separately from the contract price)"]
            rids.append("hic-finance-charge-separate")
            if price <= NO_SECURITY_MAX_USD:
                notes.append(f"At {_usd(price)} the contract may not take a security interest in the home other than "
                             "a mechanics lien (B&P 7159.2(a)).")
                rids.append("no-real-property-security-5000-or-less")
        d = R("hic-project-description-heading")
        md += [f"## {d.param('required_heading')}", f.project_description,
               f"**Significant materials and equipment:** {f.materials_and_equipment}"]
        if f.is_swimming_pool:
            md.append("[[FILL IN: attach the plan and scale drawing showing shape, size, dimensions, and construction "
                      "and equipment specifications. A pool contract missing it is void and unenforceable by you.]]")
            rids.append("pool-contract-void-noncompliance")
        rids.append(d.id)
        if down:
            dp = down_payment_limit(float(price), float(down), bonded)
            dd = R("hic-downpayment-disclosure")
            md += [f"## {dd.param('required_heading')}", _usd(down)]
            if not dp.ok:
                md.append(f"[[FIX BEFORE USE: {dp.math}]]")
                problems.append("Down payment over the cap: " + dp.math)
            if not bonded:
                md += [_pc(dd.id, f"at least {dd.param('min_font_pt')}-pt bold"), f"**{dd.param('required_text')}**"]
                ts("Downpayment statement", f"{dd.param('min_font_pt')}-pt bold", dd.id)
            rids += [dd.id, "downpayment-cap-1000-or-10-percent"]
        lines, pf = check_payment_schedule(float(price), float(down), f.progress_payments, bonded)
        problems += [f"{x.item}: {x.detail}" for x in pf if x.status in ("violation", "warning")]
        rids += [x.rule_id for x in pf]
        pp = R("hic-progress-payment-schedule")
        if f.progress_payments:
            md.append(f"## {pp.param('required_heading')}")
            if not bonded:
                md += [_pc(pp.id, f"at least {pp.param('statement_min_font_pt')}-pt bold"), _bold_lines(pp.param("required_text"))]
                ts("Progress-payment statement", f"{pp.param('statement_min_font_pt')}-pt bold", pp.id)
            md.append("\n".join(["| # | Work, services, materials and equipment | Amount |", "|---|---|---|"] + [
                f"| {ln.number} | {ln.for_work or '[[FILL IN: work this payment is for]]'} | {_usd(ln.amount_usd)} |"
                for ln in lines]))
            rids.append(pp.id)
        sc = R("hic-commencement-and-start-date")
        md += ["## Substantial Commencement of Work", "Substantial commencement of work under this contract means: "
               + (f.substantial_commencement or "[[FILL IN: e.g. 'the day demolition or other physical work begins "
                                                "at the project address']]"),
               "## Approximate Start Date", _long(start) or "[[FILL IN: approximate start date]]",
               f"## {R('hic-approximate-completion-date').param('required_heading')}",
               _long(finish) or "[[FILL IN: approximate completion date]]"]
        rids += [sc.id, "hic-approximate-completion-date"]
        if start and finish and finish < start:
            problems.append("Completion date is before the start date.")
        if start and signed and start < signed:
            problems.append(f"Start date {start} is before the signing date {signed}: no work may start until the "
                            "buyer has a copy signed and dated by both of you.")
            rids.append("lawbook_p381-430:hic-signed-copy-before-work")
        if start and cancel.earliest_safe_start_date and start.isoformat() < cancel.earliest_safe_start_date:
            problems.append(f"Start date {start} is inside the cancellation period; start on or after "
                            f"{cancel.earliest_safe_start_date}.")
        if f.incorporated_documents:
            md += [f"## {R('hic-list-of-incorporated-documents').param('required_heading')}"]
            md += [f"- {x}" for x in f.incorporated_documents]
            rids.append("hic-list-of-incorporated-documents")
        ne = R("hic-note-about-extra-work")
        md += [f"## {ne.param('required_heading')}", ne.param("required_text")]
        sub = R("hic-subcontractor-disclosure-checkbox")
        yes, no = ("[X]", "[__]") if f.subcontractors_will_be_used else ("[__]", "[X]")
        md += ["## Subcontractors", f"Will a subcontractor be used on this project?  {yes} Yes   {no} No"]
        if f.subcontractors_will_be_used:
            md.append(sub.param("if_yes_required_text"))
        lr = R("hic-lien-release-statement")
        md += ["## Lien Releases", "Upon satisfactory payment being made for any portion of the work performed, the "
               "contractor, prior to any further payment being made, shall furnish to the person contracting for the "
               "home improvement a full and unconditional release from any potential lien claimant claim or mechanics "
               f"lien authorized pursuant to Sections {lr.param('lien_law').replace('Civil Code ', '')} of the Civil "
               "Code for that portion of the work for which payment has been made.",
               "## Change Orders", "The change-order form in Attachment B is incorporated into this contract. A "
               "change order becomes part of this contract only if it is in writing and signed by the parties before "
               "any work covered by it begins."]
        rids += [ne.id, sub.id, lr.id, "hic-change-order-form-incorporated"]
        md += ["## Commercial General Liability Insurance (CGL)", _cgl_text(f, False)] + wc_block(False)
        fields_ = R("change-order-enforceability").param("required_change_order_fields")
        md += ["## Extra Work and Change Orders: Notice to Buyer",
               "You, the buyer, may not require the contractor to perform extra or change-order work without providing "
               "written authorization before the work covered by the new change order begins. Extra work or a change "
               "order is not enforceable against you unless the change order also identifies all of the following in "
               "writing before that work begins: " + "; ".join(f"({n}) the {x}" for n, x in
                                                               zip(("i", "ii", "iii"), fields_)) + ". "
               "The contractor's failure to comply with these requirements does not preclude the recovery of "
               "compensation for work performed based upon legal or equitable remedies designed to prevent unjust "
               "enrichment."]
        rids += ["hic-cgl-insurance-notice", "hic-workers-comp-notice", "hic-change-order-buyer-notice",
                 "change-order-enforceability"]
        if not bonded:
            md += ["## Mechanics Lien Warning", _paras(R("hic-mechanics-lien-warning").param("required_text"))]
            rids.append("hic-mechanics-lien-warning")
        else:
            notes.append("Bonded/joint-control contractor: the downpayment statement, progress-payment details and "
                         "Mechanics Lien Warning were left out (B&P 7159.5(a)(8)); everything else still applies.")
            rids.append("bonded-contractor-payment-exemption")
        cs = R("hic-cslb-information-notice")
        md += [_pc(cs.id, f"at least {cs.param('min_font_pt')}-pt"), _paras(cs.param("required_text"))]
        ts("CSLB information notice", f"{cs.param('min_font_pt')}-pt", cs.id)
        md += ["## Additional Terms", "[[ATTORNEY: warranty; permits and inspections and who obtains and pays for "
               "them; permissible delays; allowances; insurance; termination; dispute resolution; attorney fees.]]"]
        if f.include_arbitration_clause:
            arb, arbn = R("residential-arbitration-clause-title-and-format"), R("residential-arbitration-notice-and-initials")
            md += [f"## {arb.param('required_title')}",
                   _pc(arb.id, f"title, provision and notice in at least {arb.param('min_font_pt_black')}-pt roman bold, "
                       f"or contrasting red at least {arb.param('min_font_pt_red')}-pt roman bold; capitals if typed"),
                   "**[[ATTORNEY: arbitration provision: scope of disputes, provider and rules, location, costs, "
                   "whether discovery and appeal rights are kept.]]**",
                   f"**{arbn.param('notice_text')}**", f"**{arbn.param('acknowledgment_text')}**",
                   "Buyer's initials: ______    Contractor's initials: ______"]
            ts("ARBITRATION OF DISPUTES", f"{arb.param('min_font_pt_black')}-pt roman bold (or "
               f"{arb.param('min_font_pt_red')}-pt red roman bold); capitals if typed", arb.id)
            rids += [arbn.id, "private-arbitration-requires-bp7191-notice"]
            pros.append(ATTORNEY_ARB)
            if f.residential_units > ARB_MAX_UNITS:
                notes.append(f"B&P 7191 covers {ARB_MAX_UNITS} or fewer units; the notice is included anyway.")
        md += ["## Performance and Payment Bond", "**Notice:** The owner or tenant has the right to require the "
               "contractor to have a performance and payment bond.",
               "## Signatures", _sign_lines(f) + f"\n\n{f.contractor_name}, by: ______________________  "
               "Date: ________"]
        rids.append("hic-bond-right-notice")
        if price <= HIC_THRESHOLD_USD:
            notes.append(f"At {_usd(price)} (not over {_usd(HIC_THRESHOLD_USD)}) the B&P 7159 form is not required; "
                         "using it anyway is fine.")
            rids.append("hic-contract-price-threshold-500")
        if not cancel.applies:
            notes += cancel.explanation
        if cancel.applies:
            fmt = R("right-to-cancel-notice-format")
            md += [_pc(fmt.id, f"at least {fmt.param('min_font_pt')}-pt bold, immediately next to the owner's "
                       f"signature, in {f.sales_language}"), _bold_lines(_cancel_notice_text(cancel.kind)),
                   "Owner acknowledges receipt of this notice.\n\n" + _sign_lines(f)]
            ts(cancel.notice_title, f"{fmt.param('min_font_pt')}-pt bold, owner signs and dates", fmt.id)
    if f.workers_comp_status == "no_employees_exempt":
        rids += ["wc-exemption-ends-2028", "workers-comp-exemption-until-2028"]
        if wc_issue:
            problems.append(wc_issue[0])
        elif finish and finish >= WC_EXEMPTION_ENDS:
            problems.append(f"The job runs to {finish}, past {WC_EXEMPTION_ENDS}, when the no-employee workers' comp "
                            "exemption ends for every license except a no-employee joint venture: get a workers' comp "
                            "certificate on file before then and issue a corrected notice.")
        else:
            notes.append(f"The no-employees workers' comp exemption ends {WC_EXEMPTION_ENDS} for every license except "
                         "a no-employee joint venture; you will need a workers' comp certificate on file.")
        if not f.license_classifications:
            notes.append("The no-employee exemption is never available to " + ", ".join(WC_ALWAYS_CLASSES) +
                         " licensees; list license_classifications to check it.")
    if f.sales_language.strip().lower() != "english":
        problems.append(f"Sales presentation in {f.sales_language}: the contract and cancellation notices must be "
                        f"in {f.sales_language}. This draft is English; translate it before use.")
        md.insert(0, f"[[TRANSLATE: this contract and its cancellation notices must be in {f.sales_language}]]")
        pros.append(f"Qualified {f.sales_language} translator, with the attorney's review of the translation.")

    # ---- attachments
    if not sr and cancel.applies:
        noc = _notice_of_cancellation(f, cancel.kind, signed, _dt(cancel.last_day_to_cancel, "last_day_to_cancel"))
        for i in (1, 2):
            md += ["---", f"# Attachment A-{i}: Notice of Cancellation (copy {i} of 2, detach and keep)",
                   "\n\n".join(x for x in noc.split("\n") if x.strip())]
        notes.append("The Notice of Cancellation uses the signing date as both the date of transaction and the date "
                     "the buyer received the signed copy. If the buyer gets the copy later, regenerate with that date.")
    if not sr:
        sub_disc = R("hic-subcontractor-disclosure-checkbox").param("if_yes_required_text")
        md += ["---", "# Attachment B: Change Order Form", "Change order No. ____  Date: ________  Contract dated: ________",
               "Scope of the extra work or change: ______________________________",
               "Amount added to (+) or subtracted from (−) the contract: $__________",
               "Revised contract price: $__________",
               "Effect on the schedule of progress payments: ______________________________",
               "Effect on the completion date: ______________________________"]
        if f.subcontractors_will_be_used:
            md.append(sub_disc)
        md.append("Signed by both parties BEFORE any work covered by this change order begins.\n\n"
                  + _sign_lines(f) + f"\n\n{f.contractor_name}, by: ______________________  Date: ________")
        rids.append("hic-subcontractor-disclaimer-on-each-change-order")
    form = R("cgl-insurance-disclosure-home-improvement")
    typo = form.param("typography")
    md += ["---", "# Attachment C: Information About Commercial General Liability Insurance",
           _pc(form.id, f"heading at least {typo['heading_min_point']}-pt, questions at least "
               f"{typo['questions_min_point']}-pt, comments in italics at least {typo['comments_min_point']}-pt; also "
               "give this with the bid"), _form_872(f)]
    ts("Attachment C (16 CCR 872(a) form)", f"heading {typo['heading_min_point']}-pt, questions "
       f"{typo['questions_min_point']}-pt, comments {typo['comments_min_point']}-pt italics", form.id)
    if sr:
        notes.append("16 CCR 872(a) covers every home improvement contract (B&P 7151.2); it is attached to this "
                     "service and repair contract to be safe.")
    markdown = "\n\n".join(md) + "\n"
    unverified = cancel.uses_unverified_business_day_rule
    res = GeneratedContract(
        contract_type="service_and_repair" if sr else "home_improvement", markdown=markdown,
        placeholders=sorted(set(re.findall(r"\[\[[^\]]+\]\]", markdown))), type_size_notes=types, cancellation=cancel,
        problems=problems, required_professionals=pros, notes=notes, as_of=today.isoformat(),
        unverified_sources=[UNVERIFIED["business_day"]] if unverified else [])
    return _finish(res, rids, unverified)


# ---------------------------------------------------------------- change orders
class ChangeOrderResult(core.Result):
    enforceable_against_buyer: bool
    compliant: bool
    findings: list[Finding] = Field(default_factory=list)
    revised_contract_price_usd: Optional[float] = None
    math: Optional[str] = None
    notes: list[str] = Field(default_factory=list)
    as_of: str


def check_change_order(
        scope_of_work: str = "", amount_change_usd: Optional[float] = None,
        effect_on_payments_or_completion: str = "", buyer_signed_on: Optional[str] = None,
        contractor_signed_on: Optional[str] = None, changed_work_starts_on: Optional[str] = None,
        original_contract_price_usd: Optional[float] = None, prior_change_orders_total_usd: float = 0.0,
        contract_says_subcontractors_used: Optional[bool] = None, includes_subcontractor_disclaimer: bool = False,
        in_writing: bool = True, payment_requested_before_work_usd: float = 0.0,
        contractor_has_payment_bond_or_joint_control: bool = False, is_service_and_repair: bool = False,
        as_of=None) -> ChangeOrderResult:
    """B&P 7159.6 / 7159(c)(5), (c)(7): is a home improvement change order enforceable and compliant?"""
    today = _dt(as_of, "as_of") or logic.today_ca()
    change = _money(amount_change_usd, "amount_change_usd") if amount_change_usd is not None else None
    prior = _money(prior_change_orders_total_usd, "prior_change_orders_total_usd")
    early = _money(payment_requested_before_work_usd, "payment_requested_before_work_usd")
    original = (_money(original_contract_price_usd, "original_contract_price_usd")
                if original_contract_price_usd is not None else None)
    if original is not None and original <= 0:
        raise ValueError(f"original_contract_price_usd must be more than $0.00, got {original_contract_price_usd!r}")
    if early < 0:
        raise ValueError("payment_requested_before_work_usd must not be negative")
    scope_of_work, effect_on_payments_or_completion = scope_of_work or "", effect_on_payments_or_completion or ""
    b, c = _dt(buyer_signed_on, "buyer_signed_on"), _dt(contractor_signed_on, "contractor_signed_on")
    work = _dt(changed_work_starts_on, "changed_work_starts_on")
    co = "change-order-enforceability"
    fs = [_f("In writing with the buyer's written authorization", "ok" if in_writing else "violation",
             "in writing" if in_writing else "Oral change orders are not enforceable; the buyer cannot require extra "
             "work without written authorization.", co),
          _f("Scope of work", "ok" if scope_of_work.strip() else "violation",
             "stated" if scope_of_work.strip() else "The change order must state the scope of work it covers.", co),
          _f("Amount added or subtracted", "ok" if change is not None else "violation",
             _usd(change) if change is not None else "State the dollar amount added or subtracted (write $0.00 if "
             "none).", co),
          _f("Effect on progress payments or completion date", "ok" if effect_on_payments_or_completion.strip()
             else "violation", "stated" if effect_on_payments_or_completion.strip() else "State how the order changes "
             "the progress payments or completion date (say 'no change' if none).", co)]
    if not (b and c):
        fs.append(_f("Signed by both parties", "violation", "Both the buyer and contractor must sign before the work "
                     "covered by the change order begins.", "hic-change-order-form-incorporated"))
    elif not work:
        fs.append(_f("Signed before the changed work begins", "not_checked", f"Signed {max(b, c)}; give the date the "
                     "changed work starts to check the timing.", "hic-change-order-form-incorporated"))
    else:
        ok = max(b, c) <= work
        fs.append(_f("Signed before the changed work begins", "ok" if ok else "violation",
                     f"Last signature {max(b, c)}, work starts {work}." + ("" if ok else " The order did not become "
                     "part of the contract because the work started before both parties signed."),
                     "hic-change-order-form-incorporated"))
    enforceable = all(x.status == "ok" for x in fs)
    co_date = max(filter(None, [b, c]), default=today)
    if SB517_FROM and co_date < SB517_FROM:
        fs.append(_f("Subcontractor disclaimer", "not_applicable", f"Required on change orders from {SB517_FROM}.",
                     "hic-subcontractor-disclaimer-on-each-change-order"))
    elif contract_says_subcontractors_used is None:
        fs.append(_f("Subcontractor disclaimer", "not_checked", "If the contract's subcontractor box is Yes, the same "
                     "disclaimer must be on this change order.", "hic-subcontractor-disclaimer-on-each-change-order"))
    elif contract_says_subcontractors_used:
        fs.append(_f("Subcontractor disclaimer", "ok" if includes_subcontractor_disclaimer else "violation",
                     "present" if includes_subcontractor_disclaimer else "The contract says subcontractors will be "
                     "used, so the subcontractor-list disclaimer must be repeated on this change order.",
                     "hic-subcontractor-disclaimer-on-each-change-order"))
    if early > 0:
        if contractor_has_payment_bond_or_joint_control:
            fs.append(_f("Payment before the changed work", "not_applicable", "Bonded/joint-control contractor may "
                         "accept payment before completion.", "bonded-contractor-payment-exemption"))
        else:
            fs.append(_f("Payment before the changed work", "violation",
                         f"{_usd(early)} requested before the changed work is done. Bill "
                         "the change order as the work is performed or materials are delivered.",
                         "no-payment-ahead-of-work"))
    revised = math = None
    if original is not None and change is not None:
        revised = original + prior + change
        math = (f"{_usd(original)} original + {_usd(prior)} prior change orders + {_usd(change)} this order = "
                f"{_usd(revised)}")
        if revised < 0:
            raise ValueError(f"revised contract price would be {_usd(revised)} ({math}): check the amounts; a "
                             "deductive change order cannot take the price below $0.00")
        if is_service_and_repair and revised > SR_MAX_USD:
            fs.append(_f("Service and repair limit", "violation", f"Revised price {_usd(revised)} is over "
                         f"{_usd(SR_MAX_USD)}: the job no longer qualifies as service and repair and the full home "
                         "improvement rules apply regardless of price.", "service-repair-nonconforming-full-hic-rules"))
    notes = ["Keep a change-order log: number, date, amount and running contract price, signed copies on file."]
    if not enforceable:
        notes.append("Not enforceable against the buyer as written. You may still recover for work actually "
                     "performed under unjust-enrichment remedies (B&P 7159.6(c)), but only if you were properly "
                     "licensed the whole time (B&P 7031).")
    res = ChangeOrderResult(enforceable_against_buyer=enforceable, compliant=all(x.status != "violation" for x in fs),
                            findings=fs, revised_contract_price_usd=float(revised) if revised is not None else None,
                            math=math, notes=notes, as_of=today.isoformat())
    return _finish(res, [x.rule_id for x in fs] + ["hic-subcontractor-disclosure-on-request"]
                   + ([] if enforceable else ["unlicensed-no-right-to-sue-disgorgement"]), False)


# ---------------------------------------------------------------- MCP tools
_DAYS = (f"{HIC_DAYS['three_day']} business days, {HIC_DAYS['five_day_senior']} if a buyer is {SENIOR_AGE}+, "
         f"{HIC_DAYS['seven_day_disaster']} for declared-disaster repairs")
VALIDATE_DOC = (
    "Check a California home improvement contract (B&P 7159, 7159.5) or service and repair contract (B&P 7159.10) "
    "against every required heading, notice and payment rule, plus B&P 7191 arbitration format. Returns `summary` "
    "(violations, warnings and a count of unchecked items: read this first), `findings` (ok / violation / warning / "
    "not_checked / not_applicable, each with its citation), the down payment cap math (lesser of "
    f"{_usd(DOWN_CAP_USD)} or {DOWN_CAP_PCT}% of price), a payment-by-payment schedule check, the cancellation "
    f"period with the exact last day to cancel ({_DAYS}), whether a no-employee workers' comp statement is allowed "
    f"(never for {', '.join(WC_ALWAYS_CLASSES)}; for no one but joint ventures from {WC_EXEMPTION_ENDS}) and the "
    "licensed professionals the contract needs. Items left out of `includes` are reported as not_checked, so "
    "compliant=true with unchecked items is not a clean bill of health. verified is false whenever a deadline uses "
    "the unverified Civil Code business-day definition. Invalid dates (not ISO) or amounts give an error naming the "
    "field.")
CANCEL_DOC = (
    "Last day a California home improvement buyer can cancel (midnight at the end of last_day_to_cancel): "
    f"{_DAYS}, counted from the day after the buyer received the signed copy, skipping Sundays and the Civil Code "
    "1689.5 holidays (Saturdays count). Lists each day counted and skipped, the earliest safe start date, whether "
    f"the period is still open, and the refund ({REFUND_DAYS} days) and goods pickup ({PICKUP_DAYS} days) periods. "
    "For service and repair (service_and_repair=true) work may start once the buyer has the signed copy; the "
    "deadline is the after-start window that applies only if a disqualifying reason occurred. verified is false: the "
    "business-day definition is not yet checked against official text.")


def register(mcp) -> None:
    @mcp.tool(name="validate_home_improvement_contract", annotations=READ_ONLY, description=VALIDATE_DOC)
    def _validate(
        contract_price_usd: Annotated[float, Field(gt=0, description="Contract price: entire cost incl. profit, labor and materials, EXCLUDING finance charges")],
        down_payment_usd: Annotated[float, Field(ge=0, description="Down payment in the contract (0 if none)")] = 0.0,
        progress_payments: Annotated[Optional[list[ProgressPayment]], Field(description="Payments due before completion, each with amount_usd, for_work (what finished work/delivered materials it pays for) and optional value_of_work_usd")] = None,
        buyer_age_65_or_older: Annotated[bool, Field(description=f"Any buyer is {SENIOR_AGE} or older ({HIC_DAYS['five_day_senior']}-business-day cancellation)")] = False,
        has_salesperson: Annotated[bool, Field(description="A home improvement salesperson solicited or negotiated the deal")] = False,
        salesperson_his_number: Annotated[Optional[str], Field(description="Salesperson's CSLB HIS registration number")] = None,
        includes_arbitration_clause: Annotated[bool, Field(description="Contract has an arbitration clause")] = False,
        arbitration_formatted_per_7191: Annotated[bool, Field(description="Clause titled ARBITRATION OF DISPUTES in required type with the statutory NOTICE before the initials")] = False,
        contractor_has_payment_bond_or_joint_control: Annotated[bool, Field(description="You furnish a registrar-approved performance and payment bond, lien and completion bond, bond equivalent or joint control covering full performance and payment")] = False,
        includes: Annotated[Optional[ContractIncludes], Field(description="Checklist of what the written contract contains; true=present, false=missing, omit=not checked")] = None,
        is_service_and_repair: Annotated[bool, Field(description="Contract is presented as a service and repair contract (B&P 7159.10)")] = False,
        residential_units: Annotated[int, Field(ge=1, description=f"Dwelling units in the building (B&P 7191 arbitration rules cover 1-{ARB_MAX_UNITS})")] = 1,
        start_date: Annotated[Optional[str], Field(description="Approximate start date in the contract (ISO)")] = None,
        completion_date: Annotated[Optional[str], Field(description="Approximate completion date in the contract (ISO)")] = None,
        buyer_received_signed_copy_on: Annotated[Optional[str], Field(description="Date the buyer received the signed, dated copy (ISO); starts the cancellation clock")] = None,
        finance_charge_usd: Annotated[float, Field(ge=0, description="Finance charge, if any")] = 0.0,
        subcontractors_will_be_used: Annotated[Optional[bool], Field(description="Will subcontractors be used?")] = None,
        disaster_repair: Annotated[bool, Field(description="Repairing damage from a declared state or local emergency")] = False,
        negotiated_at_contractor_place_of_business: Annotated[bool, Field(description="Deal negotiated at your place of business")] = False,
        is_swimming_pool: Annotated[bool, Field(description="Primary purpose is building a swimming pool")] = False,
        buyer_initiated_contact: Annotated[Optional[bool], Field(description="Service and repair: the buyer called you to request the work")] = None,
        only_work_needed_for_the_problem: Annotated[Optional[bool], Field(description="Service and repair: you sell only what the problem needs")] = None,
        time_and_materials: Annotated[bool, Field(description="Service and repair priced by time and materials")] = False,
        service_charge_usd: Annotated[float, Field(ge=0, description="Service and repair: service/trip/inspection charge")] = 0.0,
        contract_signed_on: Annotated[Optional[str], Field(description=f"Date the contract was signed (ISO); decides which version of the law applies (AB 1327 items from {AB1327_FROM}, SB 517 items from {SB517_FROM}, senior five-day from {SENIOR_FROM}). Defaults to the received date, else as_of")] = None,
        workers_comp_notice_says_no_employees: Annotated[bool, Field(description="The Workers' Compensation notice says the contractor has no employees and is exempt")] = False,
        license_classifications: Annotated[Optional[list[str]], Field(description="CSLB classifications on the license, e.g. ['B', 'C-39']; used to check a no-employee workers' comp claim")] = None,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date (default today in California)")] = None,
    ) -> ContractValidation:
        return validate_home_improvement_contract(
            contract_price_usd, down_payment_usd, progress_payments, buyer_age_65_or_older, has_salesperson,
            salesperson_his_number, includes_arbitration_clause, arbitration_formatted_per_7191,
            contractor_has_payment_bond_or_joint_control, includes, is_service_and_repair, residential_units,
            start_date, completion_date, as_of, buyer_received_signed_copy_on=buyer_received_signed_copy_on,
            finance_charge_usd=finance_charge_usd, subcontractors_will_be_used=subcontractors_will_be_used,
            disaster_repair=disaster_repair, negotiated_at_contractor_place_of_business=negotiated_at_contractor_place_of_business,
            is_swimming_pool=is_swimming_pool, buyer_initiated_contact=buyer_initiated_contact,
            only_work_needed_for_the_problem=only_work_needed_for_the_problem, time_and_materials=time_and_materials,
            service_charge_usd=service_charge_usd, contract_signed_on=contract_signed_on,
            workers_comp_notice_says_no_employees=workers_comp_notice_says_no_employees,
            license_classifications=license_classifications)

    @mcp.tool(name="generate_home_improvement_contract", annotations=READ_ONLY)
    def _generate(
        contract: Annotated[ContractFields, Field(description="Contract facts: contractor, buyer, price, payments, dates, insurance, options")],
        as_of: Annotated[Optional[str], Field(description="Generate as of this ISO date (default today in California)")] = None,
    ) -> GeneratedContract:
        """Draft a California home improvement contract (B&P 7159) as Markdown with every required heading,
        statement and notice in statutory order, using the verbatim statutory text (Mechanics Lien Warning, CSLB
        notice, right-to-cancel notice, Notice of Cancellation in duplicate, B&P 7191 arbitration notice, 16 CCR 872
        CGL form, change-order form). Produces a service and repair form (B&P 7159.10) instead when requested and all
        four conditions hold. [[ATTORNEY: ...]] marks text a California attorney must supply; [[FILL IN: ...]] marks
        facts still needed; HTML comments and type_size_notes give the minimum print sizes. `problems` lists what
        must be fixed before the buyer signs (e.g. down payment over the cap, start date inside the cancellation
        period); [[FIX BEFORE USE: ...]] marks the same spots in the text. Also returns the computed cancellation
        deadline. Dates go in as ISO (2026-11-23) and print as 'November 23, 2026'; unknown keys are rejected."""
        return generate_home_improvement_contract(contract, as_of)

    @mcp.tool(name="check_change_order", annotations=READ_ONLY)
    def _change_order(
        scope_of_work: Annotated[str, Field(description="Scope of the extra work or change")] = "",
        amount_change_usd: Annotated[Optional[float], Field(description="Amount added (+) or subtracted (-); omit if the order states none")] = None,
        effect_on_payments_or_completion: Annotated[str, Field(description="What the order says about its effect on progress payments or the completion date")] = "",
        buyer_signed_on: Annotated[Optional[str], Field(description="Date the buyer signed (ISO)")] = None,
        contractor_signed_on: Annotated[Optional[str], Field(description="Date the contractor signed (ISO)")] = None,
        changed_work_starts_on: Annotated[Optional[str], Field(description="Date the changed work starts or started (ISO)")] = None,
        original_contract_price_usd: Annotated[Optional[float], Field(gt=0, description="Original contract price, to compute the revised price")] = None,
        prior_change_orders_total_usd: Annotated[float, Field(description="Net total of earlier change orders")] = 0.0,
        contract_says_subcontractors_used: Annotated[Optional[bool], Field(description="The contract's subcontractor box is Yes")] = None,
        includes_subcontractor_disclaimer: Annotated[bool, Field(description="This change order repeats the subcontractor-list disclaimer")] = False,
        in_writing: Annotated[bool, Field(description="The change order is written")] = True,
        payment_requested_before_work_usd: Annotated[float, Field(ge=0, description="Money requested for this change before the work is done")] = 0.0,
        contractor_has_payment_bond_or_joint_control: Annotated[bool, Field(description="You furnish a qualifying bond or joint control")] = False,
        is_service_and_repair: Annotated[bool, Field(description="The base contract is a service and repair contract")] = False,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date")] = None,
    ) -> ChangeOrderResult:
        """Check a home improvement change order under B&P 7159.6 and 7159(c)(5), (c)(7): written, scope, dollar
        amount added or subtracted, effect on progress payments or completion date, signed by both parties before the
        changed work starts, subcontractor disclaimer, and no payment ahead of the work. Returns
        enforceable_against_buyer, compliant, each finding with its citation, and the revised contract price math
        (original + prior change orders + this order). A change that would take the price below $0 is an error."""
        return check_change_order(scope_of_work, amount_change_usd, effect_on_payments_or_completion, buyer_signed_on,
                                  contractor_signed_on, changed_work_starts_on, original_contract_price_usd,
                                  prior_change_orders_total_usd, contract_says_subcontractors_used,
                                  includes_subcontractor_disclaimer, in_writing, payment_requested_before_work_usd,
                                  contractor_has_payment_bond_or_joint_control, is_service_and_repair, as_of)

    @mcp.tool(name="home_improvement_cancellation_deadline", annotations=READ_ONLY, description=CANCEL_DOC)
    def _cancel(
        received_signed_copy_on: Annotated[str, Field(description="Date the buyer received the signed, dated contract (ISO)")],
        buyer_age_65_or_older: Annotated[bool, Field(description=f"Any buyer is {SENIOR_AGE} or older")] = False,
        disaster_repair: Annotated[bool, Field(description="Repairing damage from a declared state or local emergency")] = False,
        negotiated_at_contractor_place_of_business: Annotated[bool, Field(description="Deal negotiated at your place of business")] = False,
        service_and_repair: Annotated[bool, Field(description="Conforming service and repair contract (B&P 7159.10)")] = False,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date")] = None,
    ) -> CancellationResult:
        return cancellation_deadline(received_signed_copy_on, buyer_age_65_or_older, disaster_repair,
                                     negotiated_at_contractor_place_of_business, service_and_repair, as_of)
