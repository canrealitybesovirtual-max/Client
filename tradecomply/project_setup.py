"""'Before you break ground' project setup for California contractors.

  project_setup_checklist  project profile -> ordered, dated checklist in nine phases (contract, licensing &
                           insurance, permits & plan check, environmental/stormwater/asbestos, Cal/OSHA permits &
                           programs, lien & payment protection, public works, site setup & postings, closeout
                           preview). Each item says why, when (a date or a relative deadline), who does it, which
                           licensed human the law requires, the math, and the citation.
  project_risk_score       same profile -> red / amber / green per phase with the reasons.

Every legal number is read from the rulebook with core.rule(id).param(key) at call time. The few rules that are not
in the rulebook live in UNVERIFIED below with a source URL, and any result that uses one has verified=False. Where the
verifier confirmed a rule's numbers but not part of its scope, the item carries not_confirmed and is verified=False.
"Public work" (prevailing wage) and "public owner" (public works lien, retention, bond and bid-listing law) are
separate: public money on a private owner's job brings prevailing wage but not the public-entity rules.
Working/business-day math skips Saturdays, Sundays and any `holidays` dates the caller gives; other holidays are not
skipped (each item says so). The NESHAP asbestos notice always counts holidays as working days (40 CFR 61.141).
Money: caps (retention, down payment) are rounded down to the cent so the figure never exceeds the cap; fees are
rounded half up.
"""
import decimal
import math
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, licenses, logic
from .cslb import norm_class

# ---------------------------------------------------------------- rule ids
# Contract
HIC_500, HIC_WRITTEN, HIC_CONTENTS = ("hic-contract-price-threshold-500", "hic-written-and-signed",
                                      "hic-required-contract-contents")
HIC_DOWN, HIC_SCHED, HIC_AHEAD = ("hic-downpayment-cap", "hic-payment-schedule-required",
                                  "web_project-management:hic-no-payment-ahead-of-work")
HIC_CANCEL, HIC_LIEN_WARN, HIC_CGL, HIC_WC = ("three-day-right-to-cancel-notice", "web_lien-payment:hic-mechanics-lien-warning",
                                              "hic-cgl-insurance-notice", "hic-workers-comp-notice")
HIC_SUBS, HIC_CGL_BID, HIC_EXTRA, HIC_PERMITS = ("hic-subcontractor-disclosure-checkbox", "ccr-872-cgl-disclosure-with-bids",
                                                 "hic-note-about-extra-work-clause", "hic-identify-permits-and-payer")
HIC_LENDER, HIC_RELEASES = "mortgage-loan-funded-contractor-payment-methods", "hic-lien-release-each-payment"
HIC_DEF, HIC_BOND = "home-improvement-contract-definition", "hic-right-to-require-bond-notice"
RET_EXEMPT = "private-retention-cap-exemptions"
SFD_CONTRACT, SFD_LIEN_WARN, SFD_CGL = ("sfd-contract-and-changes-in-writing", "sfd-construction-mechanics-lien-warning",
                                        "sfd-cgl-disclosure")
PRIME_STMT, LIC_NUMBER = "prime-contract-cslb-statement-4-10-years", "license-number-in-contracts-bids-ads"
RET_PRIV, RET_PUB, PAY_SUBS = "private-retention-5pct-cap", "public-retention-5pct-cap", "pay-subs-within-7-days-of-progress-payment"
# Licensing & insurance
MINOR, CLASS_LIMIT, B_TWO_TRADES = ("minor-work-exemption-1000", "classification-policy-no-work-outside-class",
                                    "b-prime-contract-two-trade-rule")
BOND, WC_ON_FILE, WC_CLASSES, WC_2028 = ("contractor-bond-25000", "workers-comp-certificate-on-file-at-all-times",
                                         "wc-required-classifications-even-without-employees",
                                         "wc-required-all-licensees-from-2028")
CGL_LLC, COI, LOOKUP, BIZ_TAX = ("cgl-required-only-for-llc", "coi-and-additional-insured-practice",
                                 "license-lookup-verification", "business-tax-proof-before-permit")
B2_SCOPE, B2_TRADES, B2_STRUCT = ("b2-residential-remodeling-scope", "b2-three-trade-minimum",
                                  "b2-no-structural-load-bearing-work")
# Permits & plan check
NO_PERMIT, PERMIT_LIC, PERMIT_WC = ("web_permits-building:unpermitted-work-presumed-willful",
                                    "web_permits-building:permit-applicant-license-statement",
                                    "workers-comp-declaration-on-permit")
CODE_2025, PERMIT_LIFE = "title24-2025-edition-effective-2026-01-01", "permit-expiration-12-months-state-exception"
SPECIAL_INSP, SPECIAL_FINAL = "special-inspection-statement-before-permit", "special-inspection-reports-and-final-report"
ENERGY_2025, CF1R, CF2R, CF3R = ("energy-code-2025-enforcement-date", "energy-cf1r-certificate-of-compliance",
                                 "energy-cf2r-certificate-of-installation", "energy-cf3r-hers-verification")
SOLAR, BATTERY, EV = "energy-sf-new-solar-pv-required", "energy-sf-battery-ready-125a", "calgreen-ev-capable-one-two-family"
WASTE, WASTE_NONRES = "calgreen-construction-waste-65-percent", "calgreen-nonres-addition-alteration-trigger"
SCHOOL, SCHOOL_ADD, SCHOOL_CERT = ("school-fee-level1-2026-rates", "school-fee-residential-addition-500sqft",
                                   "school-fee-certificate-before-permit")
SCHOOL_ADU = "adu-school-fee-exempt-under-500"
SMIP, BSASRF = "smip-seismic-fee", "bsasrf-green-building-fee"
AB2234, AB253 = "post-entitlement-permit-completeness-review", "private-plan-check-ab253"
ADU_COMPLETE, ADU_60 = "adu-completeness-15-business-days", "adu-60-day-deemed-approved"
ALARMS, FIXTURES, DEMO_PERMIT = ("smoke-co-alarms-on-permits-over-1000", "plumbing-fixture-upgrade-pre-1994",
                                 "demolition-permit-requires-air-district-notice")
# Environmental, stormwater & asbestos
ASB_SURVEY, ASB_DISCLOSE = "neshap-asbestos-inspection-before-demo", "web_permits-building:asbestos-inspection-conflict-disclosure"
ASB_CAC, ASB_OWNER_CAC, ASB_SAMPLES = ("asbestos-consultant-dosh-certification-required",
                                       "owner-must-hire-certified-asbestos-consultant",
                                       "asbestos-contractor-bulk-sampling-limit-12")
NESHAP_FAC, NESHAP_RACM, NESHAP_10, AIR_DISTRICT = ("neshap-facility-excludes-4-or-fewer-units", "neshap-racm-thresholds",
                                                    "neshap-notification-10-working-days",
                                                    "air-district-demolition-notice-10-working-days")
ASB_CERT, ASB_REG, ASB_JOB = ("asbestos-c22-or-certification-100sqft", "asbestos-dosh-registration-100sqft",
                              "asbestos-job-notification")
RRP, RRP_PAMPHLET, RRP_FIRM, RRP_RECORDS = ("rrp-applicability-pre-1978", "rrp-renovate-right-pamphlet-60-days",
                                            "rrp-firm-certification", "rrp-records-3-years")
SW_1ACRE, SW_WAIVER, CGP_NOI = ("stormwater-small-construction-1-acre-definition", "stormwater-erosivity-waiver",
                                "cgp-noi-smarts-before-construction")
CGP_QSD, CGP_QSP, CGP_NOT = "cgp-swppp-by-qsd", "cgp-qsp-implementation-inspections", "cgp-notice-of-termination"
# Cal/OSHA permits & programs
IIPP, IIPP_1509, IIPP_ORAL = "iipp-written-program-required", "iipp-1509-construction-iipp", "iipp-small-employer-oral-communication"
HEAT_PLAN, HEAT_WATER, HEAT_SHADE, HEAT_HIGH, INDOOR_HEAT = (
    "heat-written-plan-outdoor", "heat-water-one-quart-per-hour", "heat-shade-above-80f", "heat-high-heat-procedures-95f",
    "indoor-heat-scope-82f")
WVPP, WVPP_EXEMPT, HAZCOM = "wvpp-written-plan-required", "wvpp-exemptions", "hazcom-written-program"
EMS_PLAN, FIRST_AID, FIRST_AID_KIT = "emergency-medical-written-plan", "first-aid-trained-persons", "first-aid-kit-weatherproof"
HAZARD_SURVEY, TOOLBOX = "pre-job-site-hazard-survey", "toolbox-tailgate-meetings-10-working-days"
PERMIT_TRENCH, PERMIT_STRUCT, PERMIT_DEMO, PERMIT_SCAFFOLD = (
    "permit-trench-excavation-5ft", "permit-structure-36ft-erection", "permit-demolition-36ft",
    "permit-scaffold-falsework-36ft")
PERMIT_ISSUE, PERMIT_ANNUAL_NOTICE, PERMIT_FEES, PERMIT_POST = (
    "permit-project-issuance-5-working-days", "permit-annual-24h-new-site-notice", "permit-fees", "permit-posting")
PERMIT_TYPES = "permit-annual-vs-project"
EXC_PROTECT, EXC_DAILY, EXC_EGRESS, COMPETENT = (
    "excavation-protective-system-5-ft", "excavation-daily-competent-person-inspection", "excavation-egress-4-ft-25-ft",
    "competent-person-definition")
FALL_GENERAL, FALL_RES_FRAMING, FALL_PLAN = "fall-protection-7-5-ft", "residential-framing-fall-6-ft", "fall-protection-plan"
SILICA_PLAN, SILICA_CP, SILICA_PEL = "silica-written-exposure-control-plan", "silica-competent-person", "silica-table1-or-pel-50"
LEAD_NOTICE, LEAD_PLAN = "lead-pre-job-notification-24h", "lead-written-compliance-program"
FALSEWORK, EMERGENCY_CALL = "falsework-engineer-design", "emergency-call-system-5-floors-48-ft"
# Lien & payment protection
PRELIM, PRELIM_WHO, PRELIM_LATE = "prelim-notice-20-day-deadline", "prelim-notice-who-is-exempt", "prelim-notice-late-lookback"
PRELIM_CONTENTS, PRELIM_DISCIPLINE, NOTICE_SERVICE = ("prelim-notice-contents", "prelim-notice-sub-discipline-400",
                                                      "notice-service-methods")
PUB_PRELIM, PUB_NO_LIEN, NONRESP = ("public-prelim-notice", "web_lien-payment:public-works-no-mechanics-lien",
                                    "notice-of-nonresponsibility")
WAIVER_FORMS, WAIVER_PROGRESS, WAIVER_EVIDENCE = ("waiver-statutory-form-required", "conditional-progress-waiver-each-payment",
                                                  "waiver-conditional-evidence-of-payment")
OWNER_PAY_30 = "private-owner-progress-payment-30-days"
# Public works
PW_DEF, PW_FUNDS, PW_DEMIN, PW_RES, PW_COVERAGE = (
    "pw-definition-public-works", "pw-paid-with-public-funds-definition", "pw-de-minimis-public-subsidy",
    "pw-private-residential-exclusion", "pw-coverage-determination")
PW_1000, SMALL, SMALL_MEASURE, PW_RATES = (
    "pw-prevailing-wage-threshold-1000", "small-project-exemption-25000-15000",
    "small-project-exemption-measured-on-prime-contract", "pwd-rates-fixed-at-bid-advertisement")
DIR_PROOF, DIR_FEE, DIR_FY, DEBARRED = ("registration-proof-before-bid-or-subcontract", "dir-registration-fee",
                                        "dir-registration-fiscal-year-renewal", "no-debarred-subcontractors")
PWC100, PW_WC_CERT, PW_POSTINGS, PW_CLAUSES = ("pwc100-project-registration-30-days", "workers-comp-certification-1861",
                                               "jobsite-notices-posting", "prime-safe-harbor-subcontract-clauses")
PAY_BOND, PAY_BOND_AMT, BOND_BASICS = ("public-payment-bond-required-25000", "public-payment-bond-amount-100pct",
                                       "performance-payment-bond-basics")
APP_MIN, DAS140, DAS142, APP_RATIO, TRAIN_FUND, APP_HOURS = (
    "apprenticeship-threshold-30000", "das140-contract-award-deadline", "das142-dispatch-request-72-hours",
    "apprentice-ratio-1-to-5", "training-fund-contributions", "apprentice-hours-verified-statement-60-days")
ECPR, CPR_REQ, SUB_LIST, SUB_SUBST, STW, DELAY_LD = (
    "ecpr-monthly-submission", "cpr-request-10-days", "sub-listing-threshold", "sub-substitution-grounds",
    "stw-when-it-applies", "delay-damages-must-be-liquidated")
# Site setup & postings
POSTER, CSP_POST, EMERG_NUMBERS, WC_POSTER = ("calosha-poster-posting", "code-of-safe-practices-posting",
                                              "emergency-numbers-posting", "workers-comp-notice-to-employees-poster")
WAGE_ORDER, PAYDAY, SICK_POSTER = "wage-order-16-posting", "payday-notice-posting", "paid-sick-leave-poster-and-notice"
TOILETS, WASHING, INSPECTIONS, VEHICLE = ("toilets-1-per-20", "washing-stations-1-per-20", "inspections-required-sequence",
                                          "vehicle-signage-other-contractors")
USA, USA_TICKET, USA_OWN, USA_WHITE, USA_CENTERS = (
    "usa-811-notice-2-working-days", "web_permits-building:usa-ticket-valid-28-days", "usa-each-contractor-own-ticket",
    "web_permits-building:usa-white-paint-delineation", "usa-regional-centers-by-county")
# Closeout preview
AB1308, NOC, LIEN_DIRECT, LIEN_SUB = ("final-inspection-10-business-days-ab1308", "noc-recording-15-days",
                                      "lien-deadline-direct-contractor", "lien-deadline-subcontractor-supplier")
RET_REL_PRIV, RET_PASS_PRIV = "private-retention-release-owner-45-days", "private-retention-passthrough-gc-to-sub-10-days"
RET_REL_PUB, RET_PASS_PUB = "public-retention-release-60-days", "pcc-7107-passthrough-7-days"
FINAL_WAIVERS, RECORDS, PUB_SPN = ("final-waivers-at-closeout", "web_project-management:records-retention-5-years",
                                   "public-spn-deadline")

# Law this module relies on that is NOT in the rulebook. Any item that uses one is verified=False and the result lists
# the source so a person can check it.
UNVERIFIED: dict = {
    "design-professional-stamp": {
        "rule": "Plans must be prepared by a California-licensed architect or engineer unless the building is exempt: "
                "woodframe single-family dwellings and woodframe multiple dwellings of no more than 4 units, of not more "
                "than 2 stories and basement, and their woodframe garages. The building official may still require a "
                "licensed professional for any part that departs from conventional woodframe construction. "
                "Nonstructural storefronts and interior alterations are also exempt.",
        "max_exempt_stories": 2, "max_exempt_units": 4,
        "citation": "B&P 5537, 5538 (Architects Practice Act); B&P 6737.1 (Professional Engineers Act)",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=BPC&sectionNum=5537"},
    "excavation-engineer-over-20-ft": {
        "rule": "Sloping, benching, shoring and shield systems for excavations deeper than 20 feet must be designed by a "
                "registered professional engineer.",
        "max_depth_without_engineer_ft": 20,
        "citation": "8 CCR 1541.1",
        "source": "https://www.dir.ca.gov/title8/1541_1.html"},
    "public-agency-plan-approval": {
        "rule": "Public agency projects may be plan-checked and permitted by the agency itself or by a state agency "
                "instead of the city: public K-12 schools and community colleges by the Division of the State "
                "Architect (Field Act, with a DSA inspector of record), state hospitals and many health facilities by "
                "HCAI.",
        "citation": "Education Code 17280 et seq. (Field Act); Health & Safety Code 129675 et seq.",
        "source": "https://www.dgs.ca.gov/DSA"},
    "accessibility-path-of-travel": {
        "rule": "New construction, additions, alterations and tenant improvements of public accommodations and commercial "
                "facilities must meet CBC Chapter 11B. When the adjusted construction cost of an alteration exceeds DSA's "
                "annual valuation threshold, the path of travel to the altered area must also be upgraded, up to 20% of "
                "the adjusted construction cost. A CASp inspection is voluntary but gives the owner legal protections.",
        "disproportionality_percent": 20,
        "citation": "CBC (Title 24 Part 2) 11B-202.4 Exception 8; Civil Code 55.53 (CASp)",
        "source": "https://www.dgs.ca.gov/DSA"},
}

PHASES = ("Contract", "Licensing & insurance", "Permits & plan check", "Environmental, stormwater & asbestos",
          "Cal/OSHA permits & programs", "Lien & payment protection", "Public works", "Site setup & postings",
          "Closeout preview")
CONTRACT, LICENSING, PERMITS, ENVIRONMENT, CALOSHA, LIEN, PUBLIC, SITE, CLOSEOUT = PHASES
DUE_SOON_DAYS = 14          # amber display window, not a legal number
SHIFT_HOURS = 8             # assumed shift length for the drinking-water math, not a legal number
NO_HOLIDAYS = ("Weekends skipped; state/federal holidays are not (list them in `holidays`), so move it earlier if one "
               "falls in the window.")
HOLIDAYS_LATER = ("Weekends skipped; holidays are not (list them in `holidays`), so the real deadline is a day later for "
                  "each holiday in the window.")
GIVEN_HOLIDAYS = "Weekends and the holidays you listed are skipped; any other closure in the window moves it {}."

OwnerType = Literal["private_individual", "private_company", "public_agency"]
ContractorRole = Literal["direct_contractor", "subcontractor"]
Scope = Literal["new_building", "addition", "alteration", "tenant_improvement", "demolition_only"]
Timing = Literal["overdue", "due_soon", "upcoming", "relative"]
Rating = Literal["red", "amber", "green"]


def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


# ---------------------------------------------------------------- models
class ChecklistItem(BaseModel):
    title: str
    why: str
    due: str = Field(description="ISO date with the rule behind it, or a relative deadline")
    due_date: Optional[str] = None
    timing: Timing = "relative"
    owner_role: str
    requires_licensed_professional: Optional[str] = None
    math: Optional[str] = None
    severity: Literal["ok", "warning", "blocker"] = "ok"
    citation: str
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool
    not_confirmed: Optional[str] = Field(None, description="Part of this item the verifier could not confirm in the "
                                                           "official text (item is then verified=False)")


class Phase(BaseModel):
    name: str
    applies: bool = True
    items: list[ChecklistItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TimelineEntry(BaseModel):
    date: str
    phase: str
    title: str
    timing: Timing


class LicenseSnapshot(BaseModel):
    license_number: str
    found: bool
    business_name: Optional[str] = None
    status: Optional[str] = None
    classifications: list[str] = Field(default_factory=list)
    expiration_date: Optional[str] = None
    workers_comp: Optional[str] = None
    contractor_bond_on_file: Optional[bool] = None
    data_source: str


class ProjectSetupResult(core.Result):
    project_name: str
    city: str
    county: str
    as_of: str
    start_date: Optional[str]
    estimated_completion_date: Optional[str]
    scope: Scope
    contractor_role: ContractorRole
    owner_type: OwnerType
    public_work: bool = Field(description="Prevailing wage project (public owner, or public funds on a private job)")
    public_owner: bool = Field(False, description="Owner is a public agency: public works lien, retention, bond and "
                                                  "bid-listing rules apply instead of the private ones")
    home_improvement_contract: bool
    private_retention_cap_applies: Optional[bool] = None
    phases: list[Phase]
    timeline: list[TimelineEntry] = Field(default_factory=list)
    licensed_professionals: list[str] = Field(default_factory=list)
    license: Optional[LicenseSnapshot] = None
    assumptions: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    unverified_sources: list[dict] = Field(default_factory=list)
    not_confirmed: list[str] = Field(default_factory=list, description="Items whose scope the verifier flagged as "
                                                                       "not confirmed in the official text")
    holidays: list[str] = Field(default_factory=list, description="Dates skipped as non-working days in working/"
                                                                  "business-day counts")


class PhaseRisk(BaseModel):
    phase: str
    applies: bool
    rating: Rating
    reasons: list[str] = Field(default_factory=list)
    items: int = 0
    overdue: int = 0
    due_soon: int = 0
    blockers: int = 0
    warnings: int = 0
    unverified_items: int = 0


class ProjectRiskResult(core.Result):
    project_name: str
    as_of: str
    start_date: Optional[str]
    overall: Rating
    phases: list[PhaseRisk]
    next_actions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rating_rules: str = (f"red = an item is past due or a blocker (e.g. license or workers' comp problem); amber = an "
                         f"item is due within {DUE_SOON_DAYS} days or needs attention; green = nothing due soon.")


# ---------------------------------------------------------------- date helpers
def _d(value, field: str) -> Optional[date]:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2027-01-11, got {value!r}") from None


def _weekdays_before(d: date, n: int, skip=()) -> date:
    """n working days before d: Saturdays, Sundays and the dates in `skip` do not count."""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d not in skip:
            n -= 1
    return d


def _weekdays_after(d: date, n: int, skip=()) -> date:
    """n working days after d: Saturdays, Sundays and the dates in `skip` do not count."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in skip:
            n -= 1
    return d


def _plus_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                      # Feb 29
        return d.replace(year=d.year + n, day=28)


def _day_of_next_month(d: date, day: int) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, day)


def _md(text: str) -> tuple[int, int]:
    mm, dd = (int(x) for x in str(text).split("-"))
    return mm, dd


def _ints(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", str(text))]


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _sf(x: float) -> str:
    """Area for display: 620 -> '620', 620.5 -> '620.5'."""
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}".rstrip("0")


def _times(amount: float, factor: float, *, percent: bool = False, cap: bool = False) -> float:
    """amount x factor (or factor %) in exact decimal, to the cent: rounded down for a legal cap (so the figure never
    exceeds it), half up for a fee."""
    v = decimal.Decimal(str(amount)) * decimal.Decimal(str(factor)) / (100 if percent else 1)
    return float(v.quantize(decimal.Decimal("0.01"), decimal.ROUND_DOWN if cap else decimal.ROUND_HALF_UP))


def _classes(codes) -> set[str]:
    """CSLB classes as normalized codes; a combined class like 'C-61/D-49' counts as both C61 and D49."""
    return {norm_class(p) for c in codes or [] for p in (str(c).split() or [""])[0].split("/") if p.strip()}


def _num(value, field: str, *, positive: bool = False, integer: bool = False):
    """Validated number (None passes through): finite, >= 0 (> 0 if positive), whole if integer."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}") from None
    if isinstance(value, bool) or not math.isfinite(x) or x < 0 or (positive and x == 0) \
            or (integer and x != int(x)):
        raise ValueError(f"{field} must be a finite {'whole ' if integer else ''}number "
                         f"{'greater than 0' if positive else '0 or more'}, got {value!r}")
    return int(x) if integer else x


# ---------------------------------------------------------------- the sheet that collects items
class _Sheet:
    def __init__(self, **kw):
        self.items: dict[str, list[ChecklistItem]] = {p: [] for p in PHASES}
        self.notes: dict[str, list[str]] = {p: [] for p in PHASES}
        self.unverified_used: list[str] = []
        self.assumptions: list[str] = []
        self.not_confirmed: list[str] = []
        self.license: Optional[LicenseSnapshot] = None
        self.license_rules: list[str] = []
        self.license_why: str = ""
        self.cal_osha_permit = self.pw_applies = self.pw_small = self.apprenticeship = self.adu = False
        self.holidays: frozenset = frozenset()
        self.__dict__.update(kw)

    def add(self, phase: str, title: str, why: str, rules=(), *, due: Optional[date] = None, when: str = "",
            owner: Optional[str] = None, pro: Optional[str] = None, math: Optional[str] = None,
            severity: str = "ok", unverified=(), not_confirmed: Optional[str] = None) -> ChecklistItem:
        """not_confirmed: a part of the rule the verifier flagged as not confirmed; the item is then unverified."""
        rules = list(dict.fromkeys(rules))
        cites = [core.rule(r).citation_text or r for r in rules]
        cites += [UNVERIFIED[k]["citation"] + " (not in rulebook; unverified)" for k in unverified]
        for k in unverified:
            if k not in self.unverified_used:
                self.unverified_used.append(k)
        if due is None:
            timing, text, iso = "relative", when or "before work starts", None
        else:
            days = (due - self.as_of).days
            timing = "overdue" if days < 0 else "due_soon" if days <= DUE_SOON_DAYS else "upcoming"
            text, iso = (f"{due.isoformat()} ({when})" if when else due.isoformat()), due.isoformat()
        if not_confirmed:
            self.not_confirmed.append(f"{title}: {not_confirmed}")
        item = ChecklistItem(title=title, why=why, due=text, due_date=iso, timing=timing, owner_role=owner or self.you,
                             requires_licensed_professional=pro, math=math, severity=severity,
                             citation="; ".join(cites), rule_ids=rules, not_confirmed=not_confirmed,
                             verified=bool(cites) and not unverified and not not_confirmed
                             and all(core.rule(r).verified for r in rules))
        self.items[phase].append(item)
        return item

    @property
    def before_start(self) -> str:
        return f"before work starts on {self.start.isoformat()}" if self.start else "before work starts"

    def days_note(self, later: bool = False) -> str:
        """How holidays were handled in a working-day count (later=True for a count forward from an event)."""
        if self.holidays:
            return GIVEN_HOLIDAYS.format("later" if later else "earlier")
        return HOLIDAYS_LATER if later else NO_HOLIDAYS

    def skipped(self, a: date, b: date) -> str:
        """' (skipping 2027-01-01)' for the caller's weekday holidays between a and b, for the math strings."""
        hits = sorted(h for h in self.holidays if min(a, b) <= h <= max(a, b) and h.weekday() < 5)
        return f" (skipping {', '.join(h.isoformat() for h in hits)})" if hits else ""

    @property
    def jurisdiction(self) -> str:
        return (f"the City of {self.city}" if self.city else f"{self.county} County" if self.county
                else "the city or county")


# ---------------------------------------------------------------- phases
def _woodframe_exempt(s: _Sheet) -> bool:
    """B&P 5537 (UNVERIFIED): woodframe dwellings of <= 4 units and <= 2 stories need no architect/engineer stamp."""
    u = UNVERIFIED["design-professional-stamp"]
    return (s.residential and not s.mixed_use and s.units <= u["max_exempt_units"]
            and s.stories <= u["max_exempt_stories"])


def _private_cap_applies(s: _Sheet) -> bool:
    """Civil Code 8811(b)(3): the 5% private retention cap does not apply to residential, non-mixed-use projects of
    four stories or less."""
    return not (s.residential and not s.mixed_use and s.stories <= P(RET_EXEMPT, "exemption_residential.max_stories"))


def _retention_item(s: _Sheet, sub: bool) -> None:
    price, who = s.price, "your subcontract" if sub else "the contract"
    if s.public_owner:
        pct = P(RET_PUB, "max_retention_percent")
        cap = _times(price, pct, percent=True, cap=True)
        s.add(CONTRACT, f"Retention: no more than {pct}% ({_usd(cap)})",
              f"Public works retention is capped at {pct}% of each payment and of the contract price (unless the agency "
              "made a pre-bid 'substantially complex' finding stated in the bid documents), and subcontract retention "
              "can't exceed the prime contract's rate. A sub that was told before bid that bonds are required and then "
              "can't furnish them loses the cap. No one may require a waiver.", [RET_PUB],
              due=s.start if sub else None, when="before you sign" if sub else "at signing",
              math=f"{pct}% x {_usd(price)} = {_usd(cap)} maximum retention on {who} (rounded down to the cent)")
        return
    after, stories = P(RET_PRIV, "applies_to_contracts_entered_on_or_after"), P(RET_EXEMPT, "exemption_residential.max_stories")
    if not _private_cap_applies(s):
        s.add(CONTRACT, f"Retention: the {P(RET_PRIV, 'max_total_retention_percent_of_contract_price')}% private cap "
              "does not apply to this residential project",
              f"Civil Code 8811 caps private retention at {P(RET_PRIV, 'max_total_retention_percent_of_contract_price')}% "
              f"on contracts signed on or after {after}, but exempts residential projects that are not mixed-use and do "
              f"not exceed {stories} stories. Retention is whatever {who} says, so negotiate it in writing."
              + (" On a home improvement contract you still may not take payment ahead of work performed."
                 if s.hic and not sub else ""), [RET_PRIV, RET_EXEMPT], due=s.start if sub else None,
              when="before you sign" if sub else "at signing",
              math=f"residential, not mixed-use, {s.stories} stor{'y' if s.stories == 1 else 'ies'} <= {stories} -> exempt")
        return
    pct = P(RET_PRIV, "max_total_retention_percent_of_contract_price")
    cap = _times(price, pct, percent=True, cap=True)
    s.add(CONTRACT, f"Retention: no more than {pct}% ({_usd(cap)})",
          f"Private contracts signed on or after {after}: retention is capped at "
          f"{P(RET_PRIV, 'max_retention_percent_of_each_payment')}% of each payment and {pct}% of the contract price, "
          "and a subcontract can't use a higher rate than the owner's contract with the direct contractor. The cap "
          "is lost only by a sub that was told in writing before bid that a performance and payment bond is required "
          f"and then fails to furnish one. (Residential, non-mixed-use projects of {stories} stories or less are exempt; "
          "this one is not.)", [RET_PRIV, RET_EXEMPT], due=s.start if sub else None,
          when="before you sign" if sub else "at signing",
          math=f"{pct}% x {_usd(price)} = {_usd(cap)} maximum retention on {who} (rounded down to the cent)")


def _contract(s: _Sheet) -> None:
    price = s.price
    if s.role == "subcontractor":
        s.add(CONTRACT, "Sign a written subcontract and get the job information sheet",
              "Put your license number on the subcontract and bid. Get the owner, direct contractor and construction "
              "lender names and addresses and the site description now: your preliminary notice needs them.",
              [LIC_NUMBER, PRELIM_CONTENTS], due=s.start, when=s.before_start)
        _retention_item(s, sub=True)
        s.add(CONTRACT, "Know your payment clock",
              f"The direct contractor must pay you within {P(PAY_SUBS, 'deadline_days')} days after it receives each "
              f"progress payment unless you agreed otherwise in writing; late payment carries "
              f"{P(PAY_SUBS, 'late_penalty_percent_per_month')}% per month."
              + (" On state agency contracts PCC 10262 sets the clock instead." if s.public_owner else ""),
              [PAY_SUBS], when="each progress payment")
        if s.is_home_improvement:
            s.notes[CONTRACT].append("Home improvement contract rules govern the direct contractor's contract with the "
                                     "homeowner; your subcontract is a regular subcontract.")
        return

    if s.hic:
        s.add(CONTRACT, "Use a compliant home improvement contract (signed before any work or payment)",
              "Any contract with an owner or tenant of residential property (any number of units) for repair, "
              "remodeling or additions is a home improvement contract. It and every change order must be in writing "
              "and signed by the buyer and you before the work it covers starts, with the required headings (Home "
              "Improvement, Contract Price, Description of the Project..., Approximate Start Date, Approximate "
              "Completion Date, Note About Extra Work and Change Orders) and, near the signatures, the notice that "
              "the owner or tenant may require a performance and payment bond. CSLB also expects the permits and who "
              "pays for them to be listed. Have an attorney review your form once.",
              ([HIC_DEF] if s.owner_type != "private_individual" else []) +
              [HIC_500, HIC_WRITTEN, HIC_CONTENTS, HIC_EXTRA, HIC_BOND, HIC_PERMITS], due=s.start,
              when=s.before_start,
              math=f"Contract price {_usd(price)} > {_usd(P(HIC_500, 'threshold_dollars'))}, so the home improvement "
                   "contract rules apply")
        pct, cap = P(HIC_DOWN, "max_downpayment_percent"), P(HIC_DOWN, "max_downpayment_dollars")
        ten = _times(price, pct, percent=True, cap=True)
        s.add(CONTRACT, f"Cap the down payment at {_usd(min(cap, ten))}",
              f"The down payment may not exceed {_usd(cap)} or {pct}% of the contract price, whichever is less, and "
              "the contract must print that statement in bold (unless you furnish qualifying bonds or joint "
              "control). Swimming pool contracts have extra content rules (B&P 7167): confirm the pool down "
              "payment limit before using this figure.", [HIC_DOWN], when="at signing",
              math=f"{pct}% x {_usd(price)} = {_usd(ten)} (rounded down to the cent); lesser of {_usd(cap)} and "
                   f"{_usd(ten)} = {_usd(min(cap, ten))}")
        s.add(CONTRACT, "Write the Schedule of Progress Payments in dollars and cents",
              "Each payment must describe the phase of work it pays for, and you may never request or take payment "
              "ahead of work performed and materials delivered.", [HIC_SCHED, HIC_AHEAD], when="at signing")
        std, senior, age = (P(HIC_CANCEL, k) for k in ("cancel_business_days_standard", "cancel_business_days_senior",
                                                        "senior_age_min"))
        s.add(CONTRACT, f"Give the right-to-cancel notice ({std} business days; {senior} if the buyer is {age}+)",
              f"The buyer can cancel until midnight of business day {std} ({senior} if age {age} or older) after "
              f"receiving the signed contract; you must refund within {P(HIC_CANCEL, 'refund_deadline_days_after_notice')}"
              " days of a cancellation. The notice goes next to the buyer's signature. Not required if the contract "
              "was negotiated at your place of business (disaster repairs use a 7-day notice).", [HIC_CANCEL],
              when="at signing")
        s.add(CONTRACT, "Attach the Mechanics Lien Warning, CGL and workers' comp notices and the subcontractor Yes/No",
              "These notices must be in the contract (the CGL disclosure must also go with the bid/estimate); the "
              "subcontractor checkbox repeats on every change order. Use the home improvement version of the CSLB "
              f"notice, not the prime contract statement: {P(PRIME_STMT, 'hic_alternative')}.",
              [HIC_LIEN_WARN, HIC_CGL, HIC_WC, HIC_CGL_BID, HIC_SUBS, PRIME_STMT], when="with the bid and the contract")
        s.add(CONTRACT, "Promise and furnish lien releases for each paid portion",
              "The contract must say you will furnish a full and unconditional release for each portion of work paid "
              "for before further payment; the owner may withhold payment until you do.", [HIC_RELEASES],
              when="each payment")
        if s.has_lender and price > P(HIC_LENDER, "threshold_dollars"):
            s.add(CONTRACT, "Set up lender disbursement by joint check or escrow",
                  f"On a mortgage-funded home improvement over {_usd(P(HIC_LENDER, 'threshold_dollars'))} the lender "
                  "may not pay you alone: it pays the borrower, the borrower and you jointly, or a third-party escrow "
                  "agreed in writing.", [HIC_LENDER],
                  due=s.start, when=s.before_start)
    elif s.is_home_improvement and s.owner_type == "public_agency":
        s.notes[CONTRACT].append("Home improvement contract rules cover contracts with an owner or tenant of "
                                 "residential property; with a public agency owner the agency's public works contract "
                                 "governs. Ask an attorney whether B&P 7159 also applies.")
    elif s.is_home_improvement and price <= P(HIC_500, "threshold_dollars"):
        s.notes[CONTRACT].append(f"Contract price {_usd(price)} is not over {_usd(P(HIC_500, 'threshold_dollars'))}: the "
                                 "B&P 7159 home improvement contract rules do not apply. Put it in writing anyway.")
    if not s.hic and not s.is_home_improvement and s.owner_type != "public_agency" and s.scope == "new_building" \
            and s.units == 1:
        s.add(CONTRACT, "Single-family home contract: written contract, Mechanics Lien Warning and CGL statement",
              "If the owner will keep the home for at least a year, the contract and every change must be in writing "
              "with start and substantial completion dates, legal description, the 7164 Mechanics Lien Warning (not "
              "the HIC version), the CGL statement and bond notice; give the owner a signed copy before work starts.",
              [SFD_CONTRACT, SFD_LIEN_WARN, SFD_CGL], due=s.start, when=s.before_start)
    if not s.hic:
        s.add(CONTRACT, "Print the CSLB complaint-limits statement on the prime contract",
              "Every written prime contract that is not a home improvement contract must carry the CSLB statement in "
              f"{P(PRIME_STMT, 'min_point_size')}-point type or larger.", [PRIME_STMT], when="at signing")
    s.add(CONTRACT, "Put your license number on the contract, every subcontract and every bid", "Required on all "
          "construction contracts, subcontracts, calls for bid and advertising.", [LIC_NUMBER], when="at signing")
    _retention_item(s, sub=False)
    if s.uses_subs:
        s.add(CONTRACT, "Sign written subcontracts that match the prime contract",
              f"Include license numbers and retention at or below your rate. You must pay each sub within "
              f"{P(PAY_SUBS, 'deadline_days')} days after you receive a progress payment unless agreed otherwise in "
              f"writing; you may withhold no more than {P(PAY_SUBS, 'max_withhold_percent_of_disputed_amount')}% of a "
              "disputed amount.", [LIC_NUMBER, PAY_SUBS], due=s.start, when="before each sub starts")


def _licensing(s: _Sheet) -> None:
    sev, prime = _license_checks(s), s.role == "direct_contractor"
    s.add(LICENSING, "Hold an active license in the right classification for every day of the work",
          f"A license is required for work of {_usd(P(MINOR, 'max_aggregate_contract_price_usd_exclusive'))} or more "
          "(labor and materials) or any work needing a permit, and you may only contract in classifications you hold."
          + (f" A B general building prime contract needs "
             f"{P(B_TWO_TRADES, 'min_unrelated_trades_excluding_framing_carpentry')} or more unrelated trades besides "
             "framing/carpentry, or the specialty license or a licensed specialty sub for the work." if prime else "")
          + s.license_why, [MINOR, CLASS_LIMIT] + ([B_TWO_TRADES] if prime else []) + s.license_rules, due=s.start,
          when=s.before_start, severity=sev)
    s.add(LICENSING, f"Keep the {_usd(P(BOND, 'amount_usd'))} contractor bond on file with CSLB",
          "Required for an active license.", [BOND], due=s.start, when=s.before_start,
          severity="blocker" if s.license and s.license.found and s.license.contractor_bond_on_file is False else "ok")
    mandatory = sorted(_classes(P(WC_CLASSES, "classifications_requiring_wc_regardless")))
    wc_2028 = _d(P(WC_2028, "operative_date"), "operative_date")
    why = (f"Keep a current workers' comp certificate on file with CSLB. An exemption means you may have no employees, "
           f"and classes {', '.join(mandatory)} must carry workers' comp even without employees.")
    wc_sev = "ok"
    if s.license and s.license.found and s.license.workers_comp != "on_file":
        held = _classes(s.license.classifications)
        if s.license.workers_comp == "exempt" and held & set(mandatory):
            wc_sev, why = "blocker", why + f" Your license shows an exemption but holds {', '.join(sorted(held & set(mandatory)))}."
        elif s.license.workers_comp == "exempt" and s.crew:
            wc_sev, why = "blocker", why + (f" Your license shows an exemption but you plan {s.crew} workers on site: "
                                            "if any are your employees, get a policy on file before they start.")
        else:
            wc_sev, why = "warning", why + f" Your license shows workers' comp '{s.license.workers_comp}'."
    if (s.end or s.start or s.as_of) >= wc_2028:
        why += f" From {wc_2028.isoformat()} every licensee needs workers' comp (only no-employee joint ventures are exempt)."
        if wc_sev == "ok" and s.license and s.license.workers_comp == "exempt":
            wc_sev = "warning"
    s.add(LICENSING, "Workers' comp certificate on file (or a valid exemption)", why, [WC_ON_FILE, WC_CLASSES, WC_2028],
          due=s.start, when=s.before_start, severity=wc_sev)
    s.add(LICENSING, ("Insurance certificates for the owner" + (" and lender" if s.has_lender else "")) if prime else
          "Insurance certificates for the GC",
          "License law requires liability insurance only for LLC licensees, but owners, lenders and GCs routinely "
          "require CGL, auto and workers' comp certificates with additional-insured endorsements.", [CGL_LLC, COI],
          due=s.start, when=s.before_start)
    place = s.city or (f"{s.county} County" if s.county else "City or county")
    s.add(LICENSING, f"{place} business tax certificate",
          "Cities (and counties, outside city limits) commonly require a local business tax certificate to work "
          "there, and may refuse any permit until it is paid"
          + (f" ({s.city or 'unincorporated'}, {s.county} County)." if s.county else "."),
          [BIZ_TAX], when="before the first permit application or day of work there")
    if s.uses_subs and prime:
        s.add(LICENSING, "Verify every subcontractor's license, classification, bond and workers' comp",
              f"Check each sub on the CSLB site before they sign; a sub in {', '.join(mandatory)} with a workers' comp "
              "exemption is a red flag.", [LOOKUP, WC_CLASSES], due=s.start, when="before each subcontract is signed")


def _license_checks(s: _Sheet) -> str:
    """Looks the license up (CSLB data or demo fixtures) and returns the severity for the license item."""
    if not s.license_number:
        s.notes[LICENSING].append("No contractor_license_number given, so the license was not checked.")
        return "ok"
    rec, source = licenses.find(s.license_number)
    if not rec:
        s.license = LicenseSnapshot(license_number=s.license_number, found=False, data_source=source)
        if source != "CSLB master file":       # partial copy, still loading, or no CSLB data at all
            s.notes[LICENSING].append(f"License not found in the data available here ({source}); this does not mean "
                                      "it is unlicensed. Confirm it on the CSLB site.")
            return "warning"
        s.notes[LICENSING].append("No license record found in CSLB's current data. Confirm the number on the CSLB site "
                                  "before you bid or sign.")
        return "blocker"
    s.license = LicenseSnapshot(license_number=rec["license_number"], found=True, business_name=rec.get("business_name"),
                                status=rec.get("status"), classifications=rec.get("classifications", []),
                                expiration_date=rec.get("expiration_date"), workers_comp=rec.get("workers_comp"),
                                contractor_bond_on_file=rec.get("contractor_bond_on_file"), data_source=source)
    if source == "demo fixtures":
        s.notes[LICENSING].append("License data is a demo record, not live CSLB data.")
    sev = "ok"
    exp = _d(rec.get("expiration_date"), "expiration_date")
    if rec.get("status") != "active":
        s.notes[LICENSING].append(f"License status is {rec.get('status')}: you may not contract or work.")
        sev = "blocker"
    elif exp and exp < (s.start or s.as_of):
        s.notes[LICENSING].append(f"License expires {exp.isoformat()}, before the work starts.")
        sev = "blocker"
    elif exp and s.end and exp < s.end:
        s.notes[LICENSING].append(f"License expires {exp.isoformat()}, before estimated completion: renew in time.")
        sev = "warning"
    held = _classes(rec.get("classifications", []))
    if s.role == "direct_contractor" and "B2" in held and not held & {"A", "B"}:
        n = P(B2_TRADES, "min_unrelated_trades_per_contract")
        s.license_rules += [B2_SCOPE, B2_TRADES, B2_STRUCT]
        s.license_why = (f" A B-2 residential remodeling license covers only improvements to an existing residential "
                         f"wood-frame structure, needs {n}+ unrelated trades in the contract, and may not make structural "
                         "changes to load-bearing parts (footings, foundations, bearing walls, roof structure).")
        if s.scope in ("new_building", "addition", "demolition_only") or not s.residential:
            s.notes[LICENSING].append(f"B-2 license on a {s.scope.replace('_', ' ')}"
                                      f"{'' if s.residential else ' of a nonresidential building'}: outside or at the "
                                      "edge of B-2 scope; a B license (or a B-licensed prime) may be needed.")
            sev = "warning" if sev == "ok" else sev
    elif s.role == "direct_contractor" and not held & {"A", "B"} and s.scope != "demolition_only":
        s.notes[LICENSING].append(f"You hold only specialty classes ({', '.join(sorted(held))}); as prime you may take "
                                  "only work in those trades plus incidental and supplemental work.")
        sev = "warning" if sev == "ok" else sev
    return sev


def _permits(s: _Sheet) -> None:
    if s.role == "subcontractor":
        s.add(PERMITS, "See the building permit before you start; pull any trade permit for your scope",
              "Working without a required permit is presumed willful and is cause for license discipline (the only "
              "carve-out is repair, maintenance or adjustment of equipment under "
              f"{_usd(P(NO_PERMIT, 'exception_repair_value_lt_usd'))}, or swapping in an identical part). The direct "
              "contractor pulls the building permit.",
              [NO_PERMIT, PERMIT_LIC, PERMIT_WC], due=s.start, when=s.before_start)
        return
    juris = s.jurisdiction
    if s.owner_type == "public_agency":
        s.add(PERMITS, f"Confirm who approves the plans and issues the permit ({juris}, the agency, or a state agency)",
              UNVERIFIED["public-agency-plan-approval"]["rule"] + " Working without a required permit or approval is "
              "cause for license discipline.", [NO_PERMIT], due=s.start, when="approval in hand " + s.before_start,
              owner="Awarding agency; you confirm", unverified=["public-agency-plan-approval"])
    else:
        s.add(PERMITS, f"Get the building permit from {juris} before any work",
              "Working without a required permit is presumed willful and is cause for license discipline. The "
              "applicant signs a license statement and a workers' comp declaration on the permit application.",
              [NO_PERMIT, PERMIT_LIC, PERMIT_WC], due=s.start, when="permit issued " + s.before_start,
              owner="You (or the owner as applicant)")
    edition, eff = P(CODE_2025, "edition"), _d(P(CODE_2025, "effective_date"), "effective_date")
    nxt = P(CODE_2025, "next_triennial_edition_expected")
    late = (s.start or s.as_of).year > nxt
    s.add(PERMITS, f"Design to the {edition} California Building Standards Code"
          + (" (or the edition in effect when you submit)" if late else ""),
          f"Applications submitted on or after {eff.isoformat()} use the {edition} codes"
          + (f"; the {nxt} edition is expected to replace it, so confirm the edition in effect on your submission "
             "date with the building department" if late else "")
          + f". The permit stays valid if work starts within "
          f"{P(PERMIT_LIFE, 'state_statutory_commencement_window_months')} months of issuance; extensions are up to "
          f"{P(PERMIT_LIFE, 'extension_max_days_each')} days each on written request.",
          [CODE_2025, PERMIT_LIFE], when="at plan check submission", owner="Architect/engineer and you")
    if s.scope != "demolition_only":
        u = UNVERIFIED["design-professional-stamp"]
        exempt = _woodframe_exempt(s)
        interior = s.scope in ("alteration", "tenant_improvement")
        if exempt:
            s.add(PERMITS, "Plans: architect/engineer stamp not required for conventional woodframe",
                  f"{s.units}-unit residential, {s.stories} stor{'y' if s.stories == 1 else 'ies'}: exempt if "
                  "conventional woodframe; the building official can still require engineered calcs for beams, "
                  "shear walls or foundations.", when="before plan check submission", owner="You or designer",
                  pro="California-licensed civil/structural engineer if the building official requires engineering",
                  unverified=["design-professional-stamp"])
        else:
            s.add(PERMITS, "Plans prepared and stamped by a licensed architect or engineer",
                  "This building is outside the woodframe exemption (more than "
                  f"{u['max_exempt_units']} units, more than {u['max_exempt_stories']} stories, mixed-use or "
                  "nonresidential)"
                  + ("; purely nonstructural interior alterations are exempt." if interior else "."),
                  when="before plan check submission", owner="Owner hires; you coordinate",
                  pro="California-licensed architect or civil/structural engineer (PE/SE)",
                  unverified=["design-professional-stamp"])
            if s.scope in ("new_building", "addition"):
                s.add(PERMITS, "Statement of Special Inspections with the permit set",
                      "Structural work needing special inspections (concrete, steel, welding, high-strength bolting, "
                      "soils) needs the statement as a condition of the permit; the owner hires the special inspector.",
                      [SPECIAL_INSP], when="before permit issuance", owner="Design professional; owner hires inspector",
                      pro="Registered design professional in responsible charge; approved special inspector")
        s.add(PERMITS, "Title 24 energy compliance (CF1R / NRCC) at plan check",
              f"The {edition} Energy Code is enforced from {P(ENERGY_2025, 'enforcement_start')}. Submit the "
              f"{'CF1R' if s.residential else 'NRCC'} certificate of compliance; it must be registered when HERS "
              "verification is required."
              + (" Multifamily buildings may use the multifamily or nonresidential form sets instead; your energy "
                 "consultant confirms which." if s.units >= 3 or s.mixed_use else ""),
              [ENERGY_2025, CF1R], when="at plan check submission", owner="Energy consultant / designer")
        if s.scope == "new_building" and s.residential and s.units <= 2:
            s.add(PERMITS, "New home: solar PV, battery-ready and EV-capable",
                  f"New single-family homes need solar PV (a few exceptions apply); one- and two-family homes with over "
                  f"{P(BATTERY, 'bess_ready_service_amps_gt')} A service must be battery-ready unless a battery is "
                  "installed; with an attached private garage they need an EV-capable raceway plus panel capacity and "
                  f"space for a {P(EV, 'circuit_amps')} A {P(EV, 'circuit_voltage')} circuit labeled 'EV CAPABLE' "
                  "(no raceway if the circuit is installed). Local reach codes may require more.",
                  [SOLAR, BATTERY, EV], when="in the permit set")
        _waste(s)
        if s.owner_type == "public_agency":
            s.notes[PERMITS].append("Ask the school district whether developer fees apply to this public agency "
                                    "project.")
        else:
            _school_fees(s)
        if s.scope == "new_building" and s.residential and s.adu:
            c, d60 = P(ADU_COMPLETE, "completeness_deadline_business_days"), P(ADU_60, "deadline_days")
            by = s.start - timedelta(days=d60) if s.start else None
            s.add(PERMITS, f"ADU permit: submit a complete application at least {d60} days before start",
                  f"The city must approve or deny a complete ADU application within {d60} days or it is deemed "
                  f"approved, and must say within {c} business days whether it is complete (or it is deemed "
                  f"complete). The {d60} days run only from a complete application, so submit earlier if you can.",
                  [ADU_60, ADU_COMPLETE], due=by, when=f"{d60} days before start",
                  math=f"{s.start.isoformat()} - {d60} days = {by.isoformat()}" if by else None)
        elif s.scope == "new_building" and s.residential:
            c = P(AB2234, "completeness_determination_business_days")
            keys = [k for k in core.rule(AB2234).parameters if k.startswith("approval_deadline_business_days_")]
            few, more = (next(k for k in keys if k.endswith(end)) for end in ("_or_fewer", "_or_more"))
            a = P(AB2234, few if s.units <= _ints(few)[0] else more)
            by = _weekdays_before(s.start, c + a, s.holidays) if s.start else None
            s.add(PERMITS, "Submit a complete housing permit application early enough for the state review clock",
                  f"The city has {c} business days to say the application is complete and {a} business days after "
                  f"that to approve or return a full set of comments ({s.units} units); resubmittals add time. "
                  f"{s.days_note()}", [AB2234], due=by,
                  when=f"{c} + {a} business days before start",
                  math=(f"{s.start.isoformat()} minus {c + a} business days{s.skipped(by, s.start)} = {by.isoformat()}"
                        if by else None))
            n253 = P(AB253, "private_option_if_agency_estimate_exceeds_business_days")
            s.add(PERMITS, f"Option: private plan check if the city's review estimate exceeds {n253} business days",
                  f"Under AB 253 the city must give a review estimate once the residential application is complete; "
                  f"if it exceeds {n253} business days you may hire a private professional provider (licensed engineer "
                  "or architect with a residential plans examiner certification), and the city then has "
                  f"{P(AB253, 'agency_days_to_act_on_private_report_business_days')} business days to act on the "
                  "provider's report.", [AB253], when="after the application is deemed complete",
                  pro="Private professional provider: licensed engineer or architect with a residential plans "
                      "examiner certification",
                  not_confirmed="which residential projects qualify (unit and height limits) was not confirmed")
        if s.existing and s.scope in ("addition", "alteration", "tenant_improvement"):
            _existing_building_triggers(s)
        if not s.residential or s.owner_type == "public_agency":
            u = UNVERIFIED["accessibility-path-of-travel"]
            s.add(PERMITS, "Accessibility (CBC 11B) and path-of-travel upgrades",
                  f"{u['rule']}", when="in the permit set", owner="Architect; owner decides on CASp",
                  pro="Licensed architect; CASp (Certified Access Specialist) recommended, not required by state law",
                  unverified=["accessibility-path-of-travel"])
    _state_fees(s)
    if s.demolition:
        s.add(PERMITS, "Demolition permit: attach the air district asbestos notification",
              "The city may not issue a demolition permit without a copy of the asbestos notification (or your "
              "declaration that none is required).", [DEMO_PERMIT], due=s.notice_by, when="with the demolition permit")


def _waste(s: _Sheet) -> None:
    pct = P(WASTE, "min_diversion_percent")
    if s.scope == "new_building" or (not s.residential and s.scope in ("addition", "alteration", "tenant_improvement")):
        s.add(PERMITS, f"Construction waste management plan: divert {pct}% of C&D waste",
              f"CALGreen requires {pct}% diversion on new construction and on every permitted nonresidential addition "
              "or alteration; many cities collect a deposit or plan before the permit issues. Keep hauler receipts.",
              [WASTE, WASTE_NONRES] if not s.residential else [WASTE], when="before permit issuance")
    elif s.scope in ("addition", "alteration"):
        s.notes[PERMITS].append("Ask the building department whether its CALGreen waste diversion plan applies to this "
                                "residential addition/alteration.")


def _school_fees(s: _Sheet) -> None:
    res, com = P(SCHOOL, "residential_max_usd_per_sqft"), P(SCHOOL, "commercial_industrial_max_usd_per_sqft")
    rules = [SCHOOL, SCHOOL_CERT]
    if s.scope == "new_building" and s.adu:
        adu = P(SCHOOL_ADU, "school_fee_exempt_lt_sqft")
        if s.sq_ft is not None and s.sq_ft < adu:
            s.notes[PERMITS].append(f"School fees: an ADU/JADU of {_sf(s.sq_ft)} sq ft interior livable space (< {adu}) "
                                    "pays no Level 1 fee (Gov Code 66311.5).")
            return
        rate, kind, rules = res, f"an ADU of {adu} sq ft or more", rules + [SCHOOL_ADU]
    elif s.scope == "new_building":
        rate, kind = (res, "residential") if s.residential else (com, "commercial/industrial")
        if s.mixed_use:
            s.notes[PERMITS].append(f"Mixed-use: the residential rate ({_usd(res)}) applies to the residential floor "
                                    f"area and the commercial rate ({_usd(com)}) to the rest; the amount shown uses the "
                                    "residential rate for all sq_ft.")
    elif s.scope == "addition" and s.residential:
        limit = P(SCHOOL_ADD, "addition_exempt_max_sqft")
        if s.sq_ft is not None and s.sq_ft <= limit:
            s.notes[PERMITS].append(f"School fees: a residential addition of {_sf(s.sq_ft)} sq ft (<= {limit}) is exempt.")
            return
        rate, kind, rules = res, "a residential addition (fee on the whole added area)", rules + [SCHOOL_ADD]
    else:
        if s.scope == "addition":
            s.notes[PERMITS].append("Commercial/industrial addition: ask the school district whether its developer fee "
                                    "applies to the added floor area.")
        return
    m = (f"{_sf(s.sq_ft)} sq ft x {_usd(rate)} = {_usd(_times(s.sq_ft, rate))} (Level 1 maximum)" if s.sq_ft else
         f"sq_ft x {_usd(rate)} (give sq_ft for the amount)")
    s.add(PERMITS, "Pay school developer fees and bring the certificate to the permit counter",
          f"Level 1 cap is {_usd(rate)}/sq ft for {kind}; the district may charge less, or more under Level 2/3. The "
          "district's certificate is required before the permit issues.", rules, when="before permit issuance",
          owner="Owner or you (per contract)", math=m)


def _state_fees(s: _Sheet) -> None:
    if s.role == "subcontractor":
        return
    val = s.valuation
    one_to = max(_ints(P(SMIP, "category_1_scope")))            # "residential 1-3 stories"
    cat1 = s.residential and not s.mixed_use and s.stories <= one_to
    rate = P(SMIP, "category_1_rate") if cat1 else P(SMIP, "category_2_rate")
    smip = max(P(SMIP, "minimum_fee_usd"), _times(val, rate))
    block = _ints(next(iter(P(BSASRF, "schedule_examples"))))[1]   # "1-25000" -> 25000
    blocks = max(1, math.ceil(val / block))
    bsa = max(P(BSASRF, "minimum_fee_usd"), blocks * P(BSASRF, "usd_per_25000_valuation_or_fraction"))
    s.add(PERMITS, f"State fees on the permit: SMIP {_usd(smip)} + building standards {_usd(bsa)}",
          "The city or county adds the strong-motion (SMIP) fee and the building standards fee to every building "
          "permit with a valuation."
          + (" If a state agency (e.g. DSA) issues the approval instead of the city or county, confirm which fees "
             "apply." if s.public_owner else ""),
          [SMIP, BSASRF], when="at permit issuance", owner="Permit applicant",
          math=(f"SMIP Category {1 if cat1 else 2} ({'residential 1-3 stories' if cat1 else 'all other buildings'}"
                f"{', mixed-use counted here; confirm with the building department' if s.mixed_use else ''}): "
                f"{_usd(val)} x {rate} = {_usd(_times(val, rate))} (min {_usd(P(SMIP, 'minimum_fee_usd'))}) -> "
                f"{_usd(smip)}; "
                f"building standards: ceil({_usd(val)} / {block:,}) = {blocks} x "
                f"{_usd(P(BSASRF, 'usd_per_25000_valuation_or_fraction'))} = {_usd(bsa)}"))


def _existing_building_triggers(s: _Sheet) -> None:
    gt = P(ALARMS, "permit_valuation_gt_usd")
    if s.residential and s.valuation > gt:
        s.add(PERMITS, "Smoke and CO alarms before final", f"Residential permit work over {_usd(gt)} triggers smoke "
              "alarms (and CO alarms with fuel-burning appliances or an attached garage) before the permit is signed "
              "off.", [ALARMS], when="before final inspection")
    cutoff = _d(P(FIXTURES, "building_built_on_or_before"), "building_built_on_or_before")
    if s.year_built is not None and s.year_built < cutoff.year:
        trig = P(FIXTURES, "multifamily_commercial_valuation_trigger_usd")
        if s.residential and s.units == 1 and not s.mixed_use:
            why = "Single-family: replace all noncompliant plumbing fixtures in the building before final."
        elif s.valuation > trig:
            why = (f"Multifamily/commercial with permit valuation over {_usd(trig)}: replace noncompliant fixtures "
                   "serving the altered area, and all of them in any room you alter, before final.")
        else:
            why = (f"Multifamily/commercial with permit valuation of {_usd(trig)} or less: replace noncompliant "
                   "fixtures in any room you alter that needs a permit, before final.")
        s.add(PERMITS, f"Water-conserving plumbing fixtures (built {s.year_built})",
              why + f" Applies to buildings built on or before {cutoff.isoformat()}; repairs and maintenance alone do "
              "not trigger it.", [FIXTURES], when="before final inspection")


def _environmental(s: _Sheet) -> None:
    if s.existing:
        n_samples = P(ASB_SAMPLES, "max_bulk_samples")
        s.add(ENVIRONMENT, "Asbestos survey of every material you will disturb",
              "Federal NESHAP requires a thorough asbestos inspection before demolition or renovation of a regulated "
              "facility, and air districts require it before demolition. Whoever the owner hires to survey must be a "
              "Cal/OSHA (DOSH) certified asbestos consultant; many air districts insist on one for the pre-demolition "
              f"survey. On a residential dwelling job a licensed contractor may take up to {n_samples} bulk samples itself "
              "for bid preparation or its own employees' exposure (sampler must be an AHERA-certified building "
              "inspector). An inspector whose report is needed for a permit must disclose any ties to the abatement "
              "firm.", [ASB_SURVEY, ASB_CAC, ASB_OWNER_CAC, ASB_SAMPLES, ASB_DISCLOSE],
              due=s.notice_by if s.demolition or s.neshap_facility else s.start,
              when=("before the air district notice is due" if s.demolition else
                    "in time for the renovation notice if asbestos is found" if s.neshap_facility else s.before_start),
              owner="Owner hires (you if the contract says so)" if s.role == "direct_contractor" else
              "Owner/GC: get the report before you disturb anything", pro="Cal/OSHA Certified Asbestos Consultant (CAC)")
    if s.demolition:
        n = P(s.notice_rule, "lead_time_working_days")
        if s.neshap_facility:
            rules = [NESHAP_10, NESHAP_RACM] + ([NESHAP_FAC] if s.residential else [])
            why = ("This building is a NESHAP facility, so every demolition needs written notice to the local air "
                   "district even with little or no asbestos. Working days are "
                   f"{P(NESHAP_10, 'working_day_definition')} (40 CFR 61.141).")
        else:
            rules = [AIR_DISTRICT, NESHAP_FAC]
            why = (f"Homes of {P(NESHAP_FAC, 'excluded_residential_max_units')} or fewer units are outside federal "
                   "NESHAP, but air districts still regulate them: BAAQMD requires notice for every demolition; "
                   "SCAQMD (Rule 1403) for any demolition and for removal of more than "
                   f"{P(AIR_DISTRICT, 'scaqmd_removal_trigger_sqft')} sq ft of asbestos (it states the lead time as "
                   f"{n} working days or {P(AIR_DISTRICT, 'scaqmd_equivalent_calendar_days')} calendar days). Check "
                   f"the {s.county + ' County' if s.county else 'local'} air district's rule. The date shown counts "
                   "holidays as working days, as NESHAP does; if your district does not, file a day earlier for each "
                   "holiday in the window.")
        s.add(ENVIRONMENT, f"File the demolition notice with the air district {n} working days ahead",
              why + " The district's notification fee applies.", rules,
              due=s.notice_by, when=f"{n} working days before demolition",
              math=(f"{s.start.isoformat()} minus {n} working days = {s.notice_by.isoformat()}" if s.start else None))
    elif s.existing and s.neshap_facility:
        s.add(ENVIRONMENT, "If the survey finds regulated asbestos, file a renovation notice",
              f"Renovations disturbing at least {P(NESHAP_RACM, 'pipe_linear_ft')} linear ft, "
              f"{P(NESHAP_RACM, 'other_components_sqft')} sq ft or {P(NESHAP_RACM, 'off_component_cuft')} cu ft of "
              f"regulated asbestos need notice {P(NESHAP_10, 'lead_time_working_days')} working days before removal.",
              [NESHAP_RACM, NESHAP_10], due=s.notice_by,
              when=f"{P(NESHAP_10, 'lead_time_working_days')} working days before removal, if triggered",
              owner="Owner/operator with the abatement contractor")
    if s.existing:
        sq = P(ASB_REG, "surface_area_sqft_gte")
        s.add(ENVIRONMENT, f"If asbestos work is {sq}+ sq ft: certified, registered abatement contractor",
              f"Asbestos work of {sq} sq ft or more needs a C-22 (or CSLB asbestos-certified) contractor registered "
              "with Cal/OSHA, and Cal/OSHA must be notified before each job.", [ASB_CERT, ASB_REG, ASB_JOB],
              when="before abatement starts", owner="Abatement contractor",
              pro="C-22 or CSLB asbestos-certified contractor with Cal/OSHA registration")
    if s.rrp:
        lo, mail = P(RRP_PAMPHLET, "max_days_before_start"), P(RRP_PAMPHLET, "certificate_of_mailing_min_days_before")
        win = (s.start - timedelta(days=lo), s.start - timedelta(days=mail)) if s.start else None
        s.add(ENVIRONMENT, "Lead-safe renovation (EPA RRP): certified firm and Renovate Right pamphlet",
              f"Paid renovation of housing built before {P(RRP, 'built_before_year')} needs an EPA-certified firm with a "
              f"certified renovator directing the work, unless a certified inspector or renovator test shows the "
              "components are lead-free. Give the owner (and an adult occupant if the owner does not live there) the "
              f"pamphlet no more than {lo} days before work (if mailed, at least {mail} days before).",
              [RRP, RRP_PAMPHLET, RRP_FIRM], due=s.start,
              when=(f"deliver between {win[0].isoformat()} and the start; mail by {win[1].isoformat()}"
                    if win else f"no more than {lo} days before work"),
              pro="EPA-certified renovator (firm EPA RRP-certified)",
              math=(f"{s.start.isoformat()} - {lo} days = {win[0].isoformat()}; - {mail} days = {win[1].isoformat()}"
                    if win else None))
    elif s.existing and s.pre1978 and not s.residential and s.scope != "demolition_only":
        s.add(ENVIRONMENT, "If children under 6 use the building (preschool, kindergarten, day care): EPA RRP applies",
              f"A building constructed before {P(RRP, 'built_before_year')} that young children visit regularly is a "
              "child-occupied facility: renovation there needs an EPA-certified firm and a certified renovator unless "
              "testing shows the components are lead-free.", [RRP, RRP_FIRM], due=s.start, when=s.before_start,
              pro="EPA-certified renovator (firm EPA RRP-certified)")
    acres, one = s.acres, P(SW_1ACRE, "small_construction_min_acres")
    if acres is not None and acres >= one:
        waiver = (acres < P(SW_WAIVER, "max_disturbed_acres_lt"))
        s.add(ENVIRONMENT, "Stormwater: Construction General Permit (NOI, SWPPP, WDID) before grading",
              f"{acres:g} acres disturbed (>= {one}) needs CGP coverage: the "
              "owner files the NOI in SMARTS with a SWPPP and gets a WDID number before construction; a QSP runs the "
              "BMPs and inspections." + (f" Under {P(SW_WAIVER, 'max_disturbed_acres_lt')} acres you may qualify for "
                                          f"an erosivity waiver if the R factor is under {P(SW_WAIVER, 'r_factor_lt')}."
                                          if waiver else ""),
              [SW_1ACRE, CGP_NOI, CGP_QSD, CGP_QSP] + ([SW_WAIVER] if waiver else []), due=s.start,
              when=s.before_start, owner="Owner (Legally Responsible Person); you provide the QSP",
              pro="QSD (Qualified SWPPP Developer) prepares the SWPPP; QSP (Qualified SWPPP Practitioner) implements")
    elif acres is None and s.scope in ("new_building", "addition"):
        s.notes[ENVIRONMENT].append(f"Give disturbed_acres: {one} acre or more (or a lot in a larger common plan of "
                                    "development that disturbs that much) needs Construction General Permit coverage.")
    elif acres is not None:
        s.notes[ENVIRONMENT].append(f"Under {one} acre disturbed: no state stormwater permit unless the site is part of a "
                                    f"larger common plan of development (e.g. a subdivision) disturbing {one} acre or "
                                    "more; the city's erosion and sediment control rules still apply.")


def _calosha(s: _Sheet) -> None:
    crew = s.crew
    oral = P(IIPP_ORAL, "employee_count_lt")
    s.add(CALOSHA, "Written IIPP, updated for this site",
          "Every construction employer needs a written Injury and Illness Prevention Program." +
          (f" With fewer than {oral} employees you may communicate it orally." if crew is not None and crew < oral else ""),
          [IIPP, IIPP_1509] + ([IIPP_ORAL] if crew is not None and crew < oral else []), due=s.start, when=s.before_start,
          owner="Each employer on site")
    s.add(CALOSHA, "Survey the site for hazards before your crew arrives", "Required before employees are on site.",
          [HAZARD_SURVEY], due=s.start, when=s.before_start)
    if s.scope == "tenant_improvement":
        s.add(CALOSHA, "Indoor heat illness plan", f"The indoor heat standard applies to work areas at "
              f"{P(INDOOR_HEAT, 'scope_temperature_f_gte')}F or more.", [INDOOR_HEAT], due=s.start, when=s.before_start)
    else:
        q = P(HEAT_WATER, "quarts_per_employee_per_hour")
        s.add(CALOSHA, "Heat Illness Prevention Plan, water and shade",
              f"Written plan in English and the crew's language; {q} quart of water per employee per hour; shade up "
              f"above {P(HEAT_SHADE, 'temperature_f_gt')}F; high-heat procedures at {P(HEAT_HIGH, 'temperature_f_gte')}F.",
              [HEAT_PLAN, HEAT_WATER, HEAT_SHADE, HEAT_HIGH], due=s.start, when=s.before_start,
              math=(f"{crew} workers x {SHIFT_HOURS} h (assumed shift) x {q} qt = {crew * SHIFT_HOURS * q} quarts "
                    f"({crew * SHIFT_HOURS * q / 4:g} gallons) at shift start, or less with an effective refill "
                    "plan" if crew else None))
    small = P(WVPP_EXEMPT, "small_site_exempt_if_employees_at_any_time_lt")
    s.add(CALOSHA, "Workplace Violence Prevention Plan", "Required for construction employers since "
          f"{P(WVPP, 'effective_enforceable')} (may be part of the IIPP); the only site exemption is fewer than {small} "
          "employees there at any time AND not accessible to the public AND in compliance with the IIPP rule; Cal/OSHA "
          "can still order a plan.", [WVPP, WVPP_EXEMPT], due=s.start,
          when=s.before_start)
    s.add(CALOSHA, "Hazard communication program and SDS for this site",
          "Written program kept at the workplace, including how you share hazard information with other employers.",
          [HAZCOM], due=s.start, when=s.before_start)
    s.add(CALOSHA, "Emergency medical plan, trained first-aid person and first-aid kit",
          "Written emergency medical services plan, first-aid trained persons available, weatherproof kit sized to the "
          "crew.", [EMS_PLAN, FIRST_AID, FIRST_AID_KIT], due=s.start, when=s.before_start)
    n = P(TOOLBOX, "max_interval_working_days")
    nxt = _weekdays_after(s.start, n, s.holidays) if s.start else None
    s.add(CALOSHA, f"Toolbox safety meetings at least every {n} working days",
          "Supervisors hold them with their crews; a first one on day one is good practice, and a sign-in sheet "
          "documents the training.", [TOOLBOX], due=nxt,
          when="next meeting at the latest, counting from a day-one meeting",
          math=(f"{s.start.isoformat()} + {n} working days{s.skipped(s.start, nxt)} = {nxt.isoformat()}"
                if nxt else None))
    _calosha_permits(s)
    depth = s.depth or 0
    if depth >= P(EXC_EGRESS, "trench_depth_ft_gte"):
        rules, unv, pro = [EXC_EGRESS, COMPETENT], [], "Competent person (designated by you) inspects daily"
        why = (f"Trenches {P(EXC_EGRESS, 'trench_depth_ft_gte')} ft or deeper need a ladder or ramp within "
               f"{P(EXC_EGRESS, 'max_lateral_travel_ft')} ft.")
        if depth >= P(EXC_PROTECT, "depth_ft_gte_requires_protective_system"):
            rules += [EXC_PROTECT, EXC_DAILY]
            why += (f" At {P(EXC_PROTECT, 'depth_ft_gte_requires_protective_system')} ft or deeper: sloping, shoring or "
                    "a shield, inspected daily by a competent person.")
            u = UNVERIFIED["excavation-engineer-over-20-ft"]
            if depth > u["max_depth_without_engineer_ft"]:
                unv, pro = ["excavation-engineer-over-20-ft"], pro + "; registered professional engineer designs the system"
                why += f" {u['rule']}"
        s.add(CALOSHA, f"Excavation protection for {depth:g} ft", why, rules, due=s.start, when=s.before_start,
              pro=pro, unverified=unv)
    gen, frame = P(FALL_GENERAL, "fall_height_ft_gt"), P(FALL_RES_FRAMING, "fall_height_ft_gte")
    framing = s.scope in ("new_building", "addition") or (s.residential and s.scope == "alteration")
    if s.scope != "tenant_improvement" or s.stories > 1:
        title = (f"Fall protection: {frame:g} ft on residential framing, above {gen:g} ft elsewhere" if s.residential
                 else f"Fall protection above {gen:g} ft")
        s.add(CALOSHA, title,
              f"Guardrails, nets or personal fall arrest/restraint for falls of more than {gen:g} ft (edges, openings, "
              f"roofs steeper than {P(FALL_GENERAL, 'roof_slope_steeper_than')})"
              + (f"; work on top plates, joists, rafters, trusses or beams {frame:g} ft or more up (residential and "
                 f"light commercial framing) needs protection at {frame:g} ft" if framing else "")
              + ". A written site-specific fall protection plan by a qualified person only if conventional protection "
              "is infeasible.", [FALL_GENERAL] + ([FALL_RES_FRAMING] if framing else []) + [FALL_PLAN], due=s.start,
              when=s.before_start, pro="Qualified person (writes any fall protection plan); competent person supervises it")
    s.add(CALOSHA, "Silica exposure control plan (cutting, drilling or grinding concrete, masonry, tile or stone)",
          f"Follow Table 1 controls or keep exposure under {P(SILICA_PEL, 'pel_ug_m3_8hr_twa')} ug/m3; written plan and "
          "a designated competent person.", [SILICA_PEL, SILICA_PLAN, SILICA_CP], due=s.start, when=s.before_start,
          pro="Competent person for silica")
    if s.existing and s.pre1978:
        h = P(LEAD_NOTICE, "advance_notice_hours")
        due = s.start - timedelta(days=math.ceil(h / 24)) if s.start else None
        s.add(CALOSHA, f"Lead: written compliance program and Cal/OSHA notice {h} hours before trigger tasks",
              f"Built {f'before {s.rrp_year}' if s.year_built else 'in an unknown year'}: assume lead paint until "
              "tested (screening assumption). Lead "
              f"trigger tasks on {P(LEAD_NOTICE, 'exempt_if_area_sqft_or_linear_ft_lt')}+ sq ft/linear ft need written "
              "notice to Cal/OSHA and a written compliance program before the job.", [LEAD_NOTICE, LEAD_PLAN],
              due=due, when=f"{h} hours before lead trigger tasks")
    if s.height is not None and s.scope in ("new_building", "addition") and s.height > P(FALSEWORK, "engineer_required_if_height_ft_gt"):
        s.add(CALOSHA, "Falsework/shoring design by a California civil engineer (if you use falsework or shoring)",
              f"Falsework or vertical shoring over {P(FALSEWORK, 'engineer_required_if_height_ft_gt')} ft high (sills to "
              f"soffit) or "
              f"{P(FALSEWORK, 'engineer_required_if_span_ft_gt')} ft span (or with traffic through it) needs an engineered "
              "design kept on site and a pre-pour certification.", [FALSEWORK], when="before erecting falsework",
              pro="California registered civil engineer")
    floors, tall = P(EMERGENCY_CALL, "floors_gte"), P(EMERGENCY_CALL, "height_or_depth_ft_gte")
    if s.stories >= floors or (s.height or 0) >= tall or depth >= tall:
        s.add(CALOSHA, "Two-way emergency call system and basket litter",
              f"Required at {floors} floors or {tall} ft height/depth.", [EMERGENCY_CALL], due=s.start, when=s.before_start)


def _calosha_permits(s: _Sheet) -> None:
    admin = s.role == "direct_contractor"
    gc_owner = "You (project administrator: project permit)" if admin else \
        "Your company only if your crew does this work (annual permit); the GC holds the project permit"
    needed = []      # (kind, rule, fee, owner, why)
    if (s.depth or 0) >= P(PERMIT_TRENCH, "depth_ft_gte"):
        needed.append(("trench/excavation", PERMIT_TRENCH, P(PERMIT_FEES, "excavation_trench_usd"),
                       "The employer digging the trench",
                       f"Required before digging a trench or excavation {P(PERMIT_TRENCH, 'depth_ft_gte')} ft or deeper "
                       "that anyone must enter (annual or project permit)."))
    h = P(PERMIT_STRUCT, "height_ft_gt")
    tall = s.height > h if s.height is not None else s.stories >= 4
    if s.height is None and s.stories >= 4:
        s.assumptions.append(f"height_ft not given; {s.stories} stories assumed to exceed {h} ft (Cal/OSHA permits).")
    fee = P(PERMIT_FEES, "construction_building_structure_scaffold_falsework_usd")
    if tall and s.scope in ("new_building", "addition"):
        needed.append(("building/structure", PERMIT_STRUCT, fee, gc_owner,
                       f"On a structure intended to be more than {h} ft high, the project administrator (usually the GC) "
                       "holds a project permit and every other employer erecting steel or structural members, decking, "
                       "curtain walls/precast/fascia, placing concrete on steel, or structural/roof framing holds an "
                       "annual permit."))
        needed.append(("scaffolding/falsework", PERMIT_SCAFFOLD, fee,
                       "The employer erecting the scaffolding, shoring or falsework",
                       f"Erecting scaffolding, vertical shoring or falsework intended to be more than {h} ft high needs "
                       "an annual or project permit."))
    if s.demolition:
        demo_tall = None if s.scope == "new_building" else tall      # finished height = existing height
        if demo_tall is None:
            s.notes[CALOSHA].append(f"If the structure you demolish is more than {h} ft high, the project administrator "
                                    "needs a Cal/OSHA demolition project permit and each demolition employer an annual "
                                    "permit (height_ft describes the new building).")
        elif demo_tall:
            needed.append(("demolition", PERMIT_DEMO, P(PERMIT_FEES, "demolition_usd"), gc_owner,
                           f"Demolishing a structure more than {P(PERMIT_DEMO, 'height_ft_gt')} ft high: the project "
                           "administrator holds a project permit and every other demolition employer an annual permit."))
    if not needed:
        return
    days = P(PERMIT_ISSUE, "issuance_working_days")
    apply_by = _weekdays_before(s.start, days, s.holidays) if s.start else None
    for kind, rid, fee, owner, why in needed:
        s.add(CALOSHA, f"Cal/OSHA {kind} permit", f"{why} A project permit ({_usd(fee)}) comes from the district office "
              f"within {days} working days of complete materials; an annual permit "
              f"({_usd(P(PERMIT_FEES, 'annual_permit_usd'))}) holder notifies the district "
              f"{P(PERMIT_ANNUAL_NOTICE, 'advance_notice_hours')} hours before starting at this site. {s.days_note()}",
              [rid, PERMIT_TYPES, PERMIT_ISSUE, PERMIT_ANNUAL_NOTICE, PERMIT_FEES], due=apply_by,
              when=f"apply {days} working days before start",
              math=(f"{s.start.isoformat()} minus {days} working days{s.skipped(apply_by, s.start)} = "
                    f"{apply_by.isoformat()}" if apply_by else None),
              owner=owner)
    s.cal_osha_permit = True


def _lien(s: _Sheet) -> None:
    first = s.start
    days = P(PRELIM, "deadline_days")
    if first:
        s.assumptions.append("First furnishing of labor or materials assumed to be the start date; if materials are "
                             f"delivered earlier, the {days} days run from that delivery.")
    due = first + timedelta(days=days) if first else None
    m = f"{first.isoformat()} + {days} days = {due.isoformat()}" if due else None
    if s.public_owner:
        s.notes[LIEN].append("No mechanics lien on public property: remedies are the stop payment notice and the "
                             "payment bond.")
        if s.role == "subcontractor":
            s.add(LIEN, "Public works preliminary notice to the agency and the direct contractor",
                  f"Serve within {days} days of first furnishing to keep stop payment notice and payment bond rights; a "
                  f"late notice covers only work in the {P(PUB_PRELIM, 'lookback_days')} days before it and after.",
                  [PUB_PRELIM, PRELIM, PUB_NO_LIEN, NOTICE_SERVICE],
                  due=due, when=f"{days} days after first furnishing", math=m)
        else:
            s.add(LIEN, "Track subs' preliminary notices and stop payment notices", "On public works your subs and "
                  "suppliers protect themselves with preliminary notices to you and the agency, stop payment notices "
                  "and claims on your payment bond.", [PUB_NO_LIEN, PUB_PRELIM], when="throughout the job")
        return
    if s.public_funds:
        s.notes[LIEN].append("Public funds on a private owner's property: private mechanics lien rules still apply.")
    if s.role == "subcontractor":
        late = P(PRELIM_LATE, "lookback_days")
        lender = "the construction lender" if s.has_lender else "any construction lender"
        why = (f"Serve the owner, the direct contractor and {lender} within {days} days of first "
               f"furnishing (serving it before you start is best). Without it you lose lien, stop payment notice and "
               f"bond rights; a late notice covers only work in the {late} days before it.")
        rules = [PRELIM, PRELIM_LATE, PRELIM_CONTENTS, NOTICE_SERVICE]
        if s.price > P(PRELIM_DISCIPLINE, "threshold_dollars"):
            why += (f" Subcontract over {_usd(P(PRELIM_DISCIPLINE, 'threshold_dollars'))}: skipping the notice is also "
                    "cause for CSLB discipline.")
            rules.append(PRELIM_DISCIPLINE)
        s.add(LIEN, "Serve your preliminary notice", why, rules, due=due, when=f"{days} days after first furnishing", math=m)
        s.add(LIEN, "Sign only conditional waivers until you are actually paid", "A conditional waiver binds you only "
              "once the check clears; use the four statutory forms.", [WAIVER_EVIDENCE, WAIVER_FORMS], when="each payment")
    elif s.has_lender:
        s.add(LIEN, "Serve a preliminary notice on the construction lender",
              f"As direct contractor you give no preliminary notice to the owner, but you must give one to the "
              f"construction lender within {days} days of first furnishing to protect your lien and stop payment "
              "notice rights against the loan.",
              [PRELIM_WHO, PRELIM, NOTICE_SERVICE], due=due, when=f"{days} days after first furnishing", math=m)
    else:
        s.notes[LIEN].append("No construction lender: as direct contractor you owe no preliminary notice.")
    if s.scope == "tenant_improvement":
        s.add(LIEN, "Tenant improvement: the landlord can post a notice of nonresponsibility",
              f"A landlord who posts and records the notice within {P(NONRESP, 'deadline_days')} days of learning of "
              "the work keeps its interest free of your lien. Get a payment guarantee, landlord consent or a joint "
              "check agreement before you start.", [NONRESP], due=s.start, when=s.before_start)
    if s.role == "subcontractor":
        return
    if s.uses_subs:
        s.add(LIEN, "Collect conditional lien waivers with every pay application",
              "Use the four statutory forms; collect each sub's and supplier's conditional progress waiver before you "
              "pay and the unconditional waiver after the check clears.", [WAIVER_FORMS, WAIVER_PROGRESS],
              when="each pay application")
    if not s.hic:
        s.add(LIEN, "Owner's progress payment clock", f"Unless the contract says otherwise, the owner must pay a "
              f"progress payment within {P(OWNER_PAY_30, 'deadline_days')} days of your written demand.",
              [OWNER_PAY_30], when="each pay application")


def _public_works(s: _Sheet) -> None:
    if not s.public_work:
        s.notes[PUBLIC].append("Private owner, no public funds: prevailing wage and DIR rules do not apply.")
        return
    price, dc = s.price, s.role == "direct_contractor"
    if not s.public_owner:
        single = P(PW_DEMIN, "single_family_only_projects.max_subsidy_pct_exclusive")
        s.add(PUBLIC, "Confirm whether public funds make this a public work",
              f"Private work paid in whole or part with public funds (including fee waivers, below-market loans or "
              f"land) is a public work, unless the subsidy is under {_usd(P(PW_DEMIN, 'max_subsidy_usd_exclusive'))} "
              f"and under {P(PW_DEMIN, 'max_subsidy_pct_of_total_project_cost_exclusive')}% of total project cost "
              f"(only the under-{single}% test for a project of single-family homes only). "
              "Private residential projects on private property are excluded unless built under an agreement with a "
              "state agency, redevelopment successor or housing authority. When unsure, ask DIR for a coverage "
              "determination.", [PW_FUNDS, PW_DEMIN, PW_RES, PW_COVERAGE], due=s.start, when="before bidding",
              severity="warning", owner="Owner/developer; you confirm")
    pw_min = P(PW_1000, "threshold_usd")
    if dc and price <= pw_min:
        s.notes[PUBLIC].append(f"Contract {_usd(price)} is not over {_usd(pw_min)}: prevailing wage does not apply.")
        return
    if not dc and price <= pw_min:
        s.assumptions.append(f"The {_usd(pw_min)} prevailing wage test is on the whole project, not your subcontract; "
                             "the project is assumed to be over it.")
    s.pw_applies = True
    s.add(PUBLIC, "Pay prevailing wages to every worker", f"Public works over {_usd(pw_min)} "
          "require the DIR rates in effect on the bid advertisement date, for you and every sub.", [PW_1000, PW_DEF,
                                                                                                    PW_RATES],
          due=s.start, when="from day one",
          math=f"{_usd(price)} > {_usd(pw_min)}" if dc else "project assumed over the threshold")
    limit = P(SMALL, "construction_max_usd_inclusive")
    s.pw_small = dc and price <= limit
    if not dc:
        s.assumptions.append("Small-project exemption is measured on the prime contract (not given); assumed not exempt.")
    if s.pw_small:
        s.add(PUBLIC, "Small project: no DIR registration or eCPR, but keep certified payroll",
              f"Prime contract {_usd(price)} <= {_usd(limit)}: exempt from DIR registration, eCPR and PWC-100, but you "
              "still pay prevailing wage and must produce certified payroll on request.", [SMALL, SMALL_MEASURE],
              when="throughout the job")
    else:
        fee = P(DIR_FEE, "fee_per_fiscal_year_usd")
        mm, dd = _md(P(DIR_FY, "period_end"))
        renew = date(s.as_of.year, mm, dd)
        renew = renew if renew >= s.as_of else date(s.as_of.year + 1, mm, dd)
        s.add(PUBLIC, f"DIR public works registration current ({_usd(fee)} per fiscal year)",
              f"You (and every sub) must be registered before bidding or signing; registration runs "
              f"{P(DIR_FY, 'period_start')} to {P(DIR_FY, 'period_end')} (month-day). Renew by {renew.isoformat()}"
              + (", before this project ends." if s.end and s.end > renew else "."),
              [DIR_PROOF, DIR_FEE, DIR_FY], due=renew, when="fiscal-year renewal")
        if dc:
            s.add(PUBLIC, "Confirm the project is registered with DIR (PWC-100)",
                  f"The awarding body must register within {P(PWC100, 'deadline_days_after_award')} days of award and "
                  "no later than the first day workers are on site; eCPRs can't be filed until it is.", [PWC100],
                  due=s.start, when="first day workers are employed at the latest",
                  owner="Awarding body (you confirm)" if s.public_owner else "Awarding body/developer (you confirm)")
        iv = P(ECPR, "max_interval_days")
        first = s.start + timedelta(days=iv) if s.start else None
        s.add(PUBLIC, f"Electronic certified payroll (eCPR) to DIR at least every {iv} days",
              f"Weekly certified payroll records, uploaded at least every {iv} days; produce copies within "
              f"{P(CPR_REQ, 'deadline_days')} days of a written request.", [ECPR, CPR_REQ], due=first,
              when="first upload at the latest", math=(f"{s.start.isoformat()} + {iv} days = {first.isoformat()}"
                                                       if first else None))
    if s.uses_subs and dc:
        if s.public_owner:
            s.add(PUBLIC, "Subcontractors: registered, not debarred, listed, with the labor code clauses",
                  f"Check each sub's DIR registration and the debarment list before signing; use the subs listed in your "
                  f"bid (over {P(SUB_LIST, 'general_listing_threshold_pct_of_total_bid_exclusive')}% of the bid) and "
                  "substitute only on PCC 4107 grounds with the agency's consent; attach Labor Code "
                  f"{', '.join(P(PW_CLAUSES, 'sections_to_attach'))} to each subcontract.",
                  [DIR_PROOF, DEBARRED, SUB_LIST, SUB_SUBST, PW_CLAUSES], due=s.start, when="before each subcontract")
        else:
            s.add(PUBLIC, "Subcontractors: registered, with the labor code clauses",
                  "Check each sub's DIR registration before signing and attach Labor Code "
                  f"{', '.join(P(PW_CLAUSES, 'sections_to_attach'))} to each subcontract.",
                  [DIR_PROOF, PW_CLAUSES], due=s.start, when="before each subcontract")
    if dc:
        s.add(PUBLIC, "Sign the workers' compensation certification", "Labor Code 1861 certification filed with the "
              "awarding body before performing the work.", [PW_WC_CERT], due=s.start, when=s.before_start)
    if dc and s.public_owner:
        if price > P(PAY_BOND, "threshold_dollars"):
            pct = P(PAY_BOND_AMT, "min_percent_of_contract")
            s.add(PUBLIC, f"Payment bond for {pct}% of the contract ({_usd(price * pct / 100)})",
                  f"Public works over {_usd(P(PAY_BOND, 'threshold_dollars'))} need a payment bond from an admitted "
                  "surety before work starts; agencies normally require a performance bond too (premium typically "
                  f"{'-'.join(str(x) for x in P(BOND_BASICS, 'typical_premium_pct_of_contract'))}%).",
                  [PAY_BOND, PAY_BOND_AMT, BOND_BASICS], due=s.start, when=s.before_start,
                  math=f"{pct}% x {_usd(price)} = {_usd(price * pct / 100)}")
        s.add(PUBLIC, "Check delay damages and skilled & trained workforce terms in the contract",
              "Delay damages are enforceable only if liquidated in the contract; skilled & trained workforce rules "
              "apply only when a statute or the bid documents require them.", [DELAY_LD, STW], when="at signing")
    app_min = P(APP_MIN, "threshold_usd")
    if not dc or price >= app_min:
        s.apprenticeship = True
        if not dc:
            s.assumptions.append(f"Apprenticeship rules reach subs of any size when the prime contract is "
                                 f"{_usd(app_min)} or more; the prime contract is assumed to be.")
        ratio = P(APP_RATIO, "ratio_apprentice_to_journeyman_hours")
        hrs = P(DAS142, "advance_notice_hours")
        das142 = _weekdays_before(s.start, hrs // 24, s.holidays) if s.start else None
        fund = _day_of_next_month(s.start, P(TRAIN_FUND, "due_day_of_following_month")) if s.start else None
        d140 = P(DAS140, "deadline_days_after_contract_execution")
        s.add(PUBLIC, f"Apprenticeship: DAS 140 within {d140} days of signing, before work starts",
              f"Contracts of {_usd(app_min)} or more: send DAS 140 to the apprenticeship committees within "
              f"{d140} days of contract execution and no later than the "
              f"first day of work; employ apprentices at {ratio} apprentice to journeyman hours per craft.",
              [APP_MIN, DAS140, APP_RATIO], due=s.start, when=f"{d140} days after signing, and before work starts")
        s.add(PUBLIC, f"Request apprentices on DAS 142 at least {hrs} hours ahead",
              f"Written request {hrs} hours before you need them, excluding weekends and holidays. {s.days_note()}",
              [DAS142], due=das142, when=f"{hrs} hours ({hrs // 24} business days) before start",
              math=(f"{s.start.isoformat()} minus {hrs // 24} business days{s.skipped(das142, s.start)} = "
                    f"{das142.isoformat()}" if das142 else None))
        s.add(PUBLIC, "Training fund contributions", f"Due by the {P(TRAIN_FUND, 'due_day_of_following_month')}th of "
              "the month after the work, for every hour in apprenticeable crafts.", [TRAIN_FUND], due=fund,
              when="first monthly payment")
    s.add(PUBLIC, "Post the wage determinations, DIR compliance notice and payday notice", "Required job site postings "
          "on public works (one location readily available to all workers).", [PW_POSTINGS], due=s.start,
          when=s.before_start, owner="Awarding body or you" if dc else
          "Awarding body or the direct contractor (check they are up)")


def _site(s: _Sheet) -> None:
    s.add(SITE, "Post the Cal/OSHA 'Safety and Health Protection on the Job' poster",
          "At each location where employees report each day.", [POSTER], due=s.start, when=s.before_start,
          owner="Each employer")
    s.add(SITE, "Post the Code of Safe Practices and emergency phone numbers",
          "Code of Safe Practices at the jobsite office; physician, hospital, ambulance and fire numbers near the job "
          "phone.", [CSP_POST, EMERG_NUMBERS], due=s.start, when=s.before_start)
    s.add(SITE, "Labor posters: workers' comp Notice to Employees, Wage Order 16, payday notice, sick leave",
          "The workers' comp poster must show your current carrier (Spanish too if you have Spanish-speaking "
          "employees); post Wage Order 16 next to the minimum wage order or keep it available at remote sites.",
          [WC_POSTER, WAGE_ORDER, PAYDAY, SICK_POSTER], due=s.start, when=s.before_start, owner="Each employer")
    if s.cal_osha_permit:
        s.add(SITE, "Post the Cal/OSHA permit at the jobsite", "At or near each place of employment (excavation permits "
              "may be kept available on site).", [PERMIT_POST], due=s.start, when=s.before_start)
    per, wash = P(TOILETS, "employees_per_toilet"), P(WASHING, "employees_per_washing_station")
    if s.crew:
        m = (f"toilets: ceil({s.crew} / {per}) = {math.ceil(s.crew / per)} if all one sex; with men and women, "
             f"ceil(men / {per}) + ceil(women / {per}); washing stations: ceil({s.crew} / {wash}) = "
             f"{math.ceil(s.crew / wash)}")
    else:
        m = f"toilets = ceil(employees of each sex / {per}); washing stations = ceil(employees / {wash})"
    s.add(SITE, "Toilets and hand-washing stations", f"One toilet per {per} employees of each sex (or fraction), one "
          f"washing station per {wash}.", [TOILETS, WASHING], due=s.start, when=s.before_start,
          owner="You (GC usually provides for all trades)" if s.role == "direct_contractor" else "GC or you", math=m)
    if s.digging:
        n, mx = P(USA, "min_working_days_before"), P(USA, "max_calendar_days_before")
        center = _811_center(s.county)
        if s.start:
            latest = _weekdays_before(s.start, n + 1, s.holidays)
            earliest = s.start - timedelta(days=mx)
            valid = P(USA_TICKET, "ticket_validity_calendar_days")
            m = (f"call no earlier than {s.start.isoformat()} - {mx} days = {earliest.isoformat()}; the call day does "
                 f"not count, so call by {latest.isoformat()} to leave {n} full working days before digging"
                 f"{s.skipped(latest, s.start)}; ticket "
                 f"valid {valid} days -> renew by {(latest + timedelta(days=valid)).isoformat()} if you call on "
                 f"{latest.isoformat()}")
        else:
            latest, m = None, None
        s.add(SITE, f"Mark the dig area in white paint and call 811 ({center})",
              f"At least {n} working days (not counting the day you call) and no more than {mx} calendar days before "
              f"digging; every excavating contractor and sub needs its own ticket. {s.days_note()}",
              [USA, USA_TICKET, USA_OWN, USA_WHITE, USA_CENTERS], due=latest, when="811 call deadline", math=m,
              owner="Each excavating contractor")
    if s.role == "direct_contractor":
        s.add(SITE, "Keep the permit and approved plans on site; call inspections in sequence",
              "Do not cover work before it is inspected (foundation, under-floor, rough MEP, frame, drywall, "
              "waterproofing, special inspections, final).", [INSPECTIONS], due=s.start, when=s.before_start)
    s.add(SITE, "Business name and license number on your commercially registered work vehicles",
          f"At least {P(VEHICLE, 'min_font_point')}-point type or {P(VEHICLE, 'min_height_and_width_inches')} inch "
          "letters (C-36, C-45 and C-57 have their own rules).", [VEHICLE], due=s.start, when=s.before_start)


def _closeout(s: _Sheet) -> None:
    end = s.end

    def at(days):
        return end + timedelta(days=days) if end else None
    if s.role == "direct_contractor":
        if s.scope == "new_building" and s.residential and s.units <= P(AB1308, "max_units_in_building"):
            n = P(AB1308, "deadline_business_days")
            by = _weekdays_after(end, n, s.holidays) if end else None
            s.add(CLOSEOUT, f"Final inspection within {n} business days of your completion notice (AB 1308)",
                  f"For new housing the building department must inspect the permitted work within {n} business days "
                  "after you give notice the work is complete; a miss is a Housing Accountability Act violation. "
                  f"Give the notice in writing and keep a copy. {s.days_note(later=True)}", [AB1308], due=by,
                  when="city's deadline if you give notice on the completion date",
                  owner="You give notice; the building department inspects",
                  math=(f"{end.isoformat()} + {n} business days{s.skipped(end, by)} = {by.isoformat()}" if by else None),
                  not_confirmed=f"the {P(AB1308, 'max_units_in_building')}-unit scope limit comes from summaries, not "
                                "the statute text")
        if s.special_inspections:
            s.add(CLOSEOUT, "Special inspector's final report", "Needed before final approval / certificate of "
                  "occupancy.", [SPECIAL_FINAL], when="before final", pro="Approved special inspector")
    if s.scope != "demolition_only":
        res = s.residential
        s.add(CLOSEOUT, "Title 24 energy documents before final: " + ("CF2R and CF3R" if res else "NRCI/NRCA"),
              "Installers sign the certificate of installation; HERS-verified measures need the HERS rater's "
              "certificate of verification in the same registry before the permit is finaled." if res else
              "Installers sign the certificate of installation (NRCI) for permitted energy measures.",
              [CF2R, CF3R] if res else [CF2R], when="before final inspection",
              owner=("Installing contractor" + (" and HERS rater" if res else "")) if s.role == "direct_contractor"
              else "You, if your scope installs energy measures (HVAC, insulation, windows, lighting, water heating)",
              pro="Certified HERS rater (independent of the installer)" if res else None)
    if not s.public_owner:
        nd = P(NOC, "deadline_days")
        lien = LIEN_DIRECT if s.role == "direct_contractor" else LIEN_SUB
        after_noc, no_noc = P(lien, "days_after_recorded_noc_or_cessation"), P(lien, "days_after_completion_if_no_notice")
        s.add(CLOSEOUT, "Check for a recorded notice of completion, then calendar your lien deadline",
              f"The owner may record a notice of completion within {nd} days after completion; it cuts your lien "
              f"deadline to {after_noc} days after recording (otherwise {no_noc} days after completion), whichever is "
              "earlier. Check the county recorder by the date shown; record any lien before the deadline, not on it.",
              [NOC, lien], due=at(nd), when=f"owner's last day to record a notice of completion ({nd} days after "
              "completion)", owner="Owner records; you check and track",
              math=(f"completion {end.isoformat()} + {no_noc} days = {at(no_noc).isoformat()} lien deadline without a "
                    f"notice; with a notice recorded on day D: D + {after_noc} days (earliest possible: "
                    f"{end.isoformat()} + {after_noc} = {at(after_noc).isoformat()})" if end else None))
        r, pass_days = P(RET_REL_PRIV, "deadline_days"), P(RET_PASS_PRIV, "deadline_days")
        if s.role == "direct_contractor":
            s.add(CLOSEOUT, f"Retention release: owner pays within {r} days of completion",
                  f"Pass each sub's share within {pass_days} days after you receive it.",
                  [RET_REL_PRIV, RET_PASS_PRIV] if s.uses_subs else [RET_REL_PRIV], due=at(r),
                  when=f"{r} days after completion", owner="Owner pays you; you pay subs",
                  math=f"{end.isoformat()} + {r} days = {at(r).isoformat()}" if end else None)
        else:
            s.add(CLOSEOUT, f"Your retention: the direct contractor pays your share within {pass_days} days of receiving it",
                  f"The owner owes the direct contractor its retention within {r} days after completion; your share is "
                  f"due {pass_days} days after the direct contractor receives it.", [RET_REL_PRIV, RET_PASS_PRIV],
                  due=at(r + pass_days), when=f"by about {r} + {pass_days} days after completion",
                  owner="Direct contractor pays you; you track",
                  math=(f"{end.isoformat()} + {r} + {pass_days} days = {at(r + pass_days).isoformat()} if the owner "
                        f"pays on day {r}; otherwise {pass_days} days after the direct contractor is paid" if end else None))
    else:
        r, pass_days = P(RET_REL_PUB, "deadline_days"), P(RET_PASS_PUB, "deadline_days")
        if s.role == "direct_contractor":
            s.add(CLOSEOUT, f"Retention release: agency pays within {r} days of completion",
                  f"Pass each sub's share within {pass_days} days after you receive it.",
                  [RET_REL_PUB, RET_PASS_PUB] if s.uses_subs else [RET_REL_PUB], due=at(r),
                  when=f"{r} days after completion", owner="Awarding body pays you; you pay subs",
                  math=f"{end.isoformat()} + {r} days = {at(r).isoformat()}" if end else None,
                  not_confirmed="one PCC 7107 excerpt gives some state agencies 90 days; not checked")
        else:
            a, b = P(PUB_SPN, "days_after_recorded_notice"), P(PUB_SPN, "days_after_completion_or_cessation_if_no_notice")
            s.add(CLOSEOUT, "Stop payment notice deadline", f"Give it within {a} days after the agency records a "
                  f"notice of completion, acceptance or cessation, otherwise within {b} days after completion.",
                  [PUB_SPN], due=at(b), when=f"{b} days after completion if no notice is recorded")
            s.add(CLOSEOUT, f"Your retention: the prime pays your share within {pass_days} days of receiving it",
                  f"The agency releases retention within {r} days after completion; the prime then has {pass_days} days.",
                  [RET_REL_PUB, RET_PASS_PUB], when="after the agency releases retention",
                  owner="Prime contractor pays you; you track")
    if s.pw_applies and not s.pw_small:
        f = P(ECPR, "final_submission_within_days_after_last_work")
        s.add(CLOSEOUT, "Final eCPR (marked final)", f"Final certified payroll to DIR within {f} days after the last "
              "day of work.", [ECPR], due=at(f), when=f"{f} days after the last work",
              math=f"{end.isoformat()} + {f} days = {at(f).isoformat()}" if end else None)
    if s.apprenticeship:
        h = P(APP_HOURS, "deadline_days_after_work_concluded")
        s.add(CLOSEOUT, "Apprentice hours statement", f"Verified journeyman and apprentice hours statement to the "
              f"apprenticeship program (and the awarding body if it asks) within {h} days after your work ends.",
              [APP_HOURS], due=at(h), when=f"{h} days after your work ends",
              math=f"{end.isoformat()} + {h} days = {at(h).isoformat()}" if end else None)
    s.add(CLOSEOUT, "Final lien waivers", "Conditional final waiver before final payment, unconditional after the check "
          "clears; list any disputed extras on the final waiver.", [FINAL_WAIVERS], when="at final payment")
    if s.cgp:
        s.add(CLOSEOUT, "Stormwater Notice of Termination", "File the NOT in SMARTS once the site is stabilized; annual "
              "fees continue until it is accepted.", [CGP_NOT], when="after final stabilization",
              owner="Owner (LRP) with your QSP", pro="QSP final inspection report")
    yrs = P(RECORDS, "retention_years_after_project_completion")
    s.add(CLOSEOUT, f"Keep the project file {yrs} years", "Contracts, change orders, pay applications, waivers, daily "
          "logs and receipts must be kept and produced to CSLB on request" + (
              f"; RRP records {P(RRP_RECORDS, 'retention_years_after_completion')} years." if s.rrp else "."),
          [RECORDS] + ([RRP_RECORDS] if s.rrp else []), due=_plus_years(end, yrs) if end else None,
          when=f"{yrs} years after completion")


def _811_center(county: str) -> str:
    c = (county or "").strip().lower().replace(" county", "")
    if c in {x.lower() for x in P(USA_CENTERS, "usa_south_digalert_counties")}:
        return "DigAlert / USA South"
    if c in {x.lower() for x in P(USA_CENTERS, "usa_north_counties")}:
        return "USA North 811"
    return "USA North 811 or DigAlert"


# ---------------------------------------------------------------- public functions
def project_setup_checklist(
        project_name: str, city: str, county: str, owner_type: OwnerType, is_home_improvement: bool,
        residential_units: int, contract_price_usd: float, valuation_usd: Optional[float] = None,
        sq_ft: Optional[float] = None, stories: int = 1, height_ft: Optional[float] = None, demolition: bool = False,
        year_built: Optional[int] = None, excavation_depth_ft: Optional[float] = None,
        disturbed_acres: Optional[float] = None, uses_subcontractors: bool = True, has_construction_lender: bool = False,
        public_funds: bool = False, start_date=None, contractor_role: ContractorRole = "direct_contractor",
        contractor_license_number: Optional[str] = None, as_of=None, scope: Optional[Scope] = None,
        peak_crew_size: Optional[int] = None, estimated_completion_date=None,
        mixed_use: bool = False, holidays=None) -> ProjectSetupResult:
    """Ordered, dated 'before you break ground' checklist for a California project (see module docstring).
    Raises ValueError with a plain message for invalid input (negative or non-finite numbers, bad dates, unknown
    choices, a home improvement contract on a building with no dwelling units)."""
    for name, value, allowed in (("owner_type", owner_type, OwnerType.__args__),
                                 ("contractor_role", contractor_role, ContractorRole.__args__),
                                 ("scope", scope, Scope.__args__ + (None,))):
        if value not in allowed:
            raise ValueError(f"{name} must be one of {', '.join(a for a in allowed if a)}; got {value!r}")
    contract_price_usd = _num(contract_price_usd, "contract_price_usd", positive=True)
    if contract_price_usd is None:
        raise ValueError("contract_price_usd is required")
    valuation_usd = _num(valuation_usd, "valuation_usd", positive=True)
    sq_ft, height_ft = _num(sq_ft, "sq_ft"), _num(height_ft, "height_ft", positive=True)
    excavation_depth_ft = _num(excavation_depth_ft, "excavation_depth_ft")
    disturbed_acres = _num(disturbed_acres, "disturbed_acres")
    residential_units = _num(residential_units, "residential_units", integer=True)
    stories = _num(stories, "stories", positive=True, integer=True)
    peak_crew_size = _num(peak_crew_size, "peak_crew_size", integer=True)
    year_built = _num(year_built, "year_built", positive=True, integer=True)
    if residential_units is None or stories is None:
        raise ValueError("residential_units (0 or more) and stories (1 or more) are required")
    if is_home_improvement and not residential_units:
        raise ValueError("is_home_improvement=True needs residential_units >= 1: home improvement contract rules cover "
                         "residential property only. For commercial work set is_home_improvement=False.")
    today = _d(as_of, "as_of") or logic.today_ca()
    start, end = _d(start_date, "start_date"), _d(estimated_completion_date, "estimated_completion_date")
    if start and end and end < start:
        raise ValueError(f"estimated_completion_date {end.isoformat()} is before start_date {start.isoformat()}")
    if year_built and year_built > (start or today).year:
        raise ValueError(f"year_built {year_built} is after the year work starts; omit it for a vacant lot")
    if isinstance(holidays, str):
        holidays = holidays.split(",")
    skip = frozenset(_d(h, "holidays") for h in holidays or [] if str(h).strip())
    city = re.sub(r"(?i)^city of\s+", "", (city or "").strip())
    county = re.sub(r"(?i)^(city and )?county of\s+|\s+county$", "", (county or "").strip())
    license_number = (contractor_license_number or "").strip() or None
    assumptions = []
    if scope is None:
        scope = "new_building" if year_built is None else "alteration"
        assumptions.append(f"scope not given; assumed '{scope}' because year_built is "
                           f"{'not given (vacant lot)' if year_built is None else year_built}.")
    if scope == "demolition_only" and not demolition:
        demolition = True
        assumptions.append("scope is demolition_only, so demolition is assumed (asbestos notice, demolition permit).")
    existing = scope != "new_building" or demolition
    rrp_year = P(RRP, "built_before_year")
    pre1978 = existing and (year_built is None or year_built < rrp_year)
    if existing and year_built is None:
        assumptions.append(f"Existing building's year built unknown: treated as built before {rrp_year} (lead) until "
                           "tested.")
    if valuation_usd is None:
        valuation_usd = contract_price_usd
        if contractor_role == "direct_contractor":
            assumptions.append("valuation_usd not given; the contract price is used for the state permit fees.")
    digging = (excavation_depth_ft or 0) > 0 or (excavation_depth_ft is None and scope in ("new_building", "addition"))
    if excavation_depth_ft is None and digging:
        assumptions.append("excavation_depth_ft not given; footing excavation assumed (811 call), depth under 4 ft.")
    residential = residential_units > 0
    mixed_use = bool(mixed_use and residential)
    neshap_facility = not (residential and not mixed_use
                           and residential_units <= P(NESHAP_FAC, "excluded_residential_max_units"))
    notice_rule = NESHAP_10 if neshap_facility else AIR_DISTRICT     # NESHAP: holidays count as working days
    if demolition and scope == "new_building":
        assumptions.append("The building being demolished is assumed to have the same use and unit count as the new "
                           "one (asbestos NESHAP coverage).")
    adu = bool(is_home_improvement and residential and scope == "new_building")
    if adu:
        assumptions.append("New residential building for a home's owner or tenant treated as an ADU (ADU permit clock, "
                           "ADU school fee rule).")
    # B&P 7151.2: a home improvement contract is with an owner OR tenant of residential property, any number of units.
    hic = (is_home_improvement and contractor_role == "direct_contractor" and owner_type != "public_agency"
           and contract_price_usd > P(HIC_500, "threshold_dollars"))
    public_owner = owner_type == "public_agency"
    public_work = public_owner or public_funds
    s = _Sheet(as_of=today, start=start, end=end, scope=scope, role=contractor_role, owner_type=owner_type,
               price=float(contract_price_usd), valuation=float(valuation_usd), sq_ft=sq_ft, stories=stories,
               height=height_ft, demolition=demolition, year_built=year_built, depth=excavation_depth_ft,
               acres=disturbed_acres, uses_subs=uses_subcontractors, has_lender=has_construction_lender,
               public_work=public_work, public_owner=public_owner, public_funds=public_funds, mixed_use=mixed_use,
               hic=hic, is_home_improvement=is_home_improvement, units=residential_units,
               residential=residential, existing=existing, pre1978=pre1978, neshap_facility=neshap_facility,
               digging=digging, crew=peak_crew_size, city=city, county=county, adu=adu, holidays=skip,
               license_number=license_number,
               you="You (direct contractor)" if contractor_role == "direct_contractor" else "You (subcontractor)",
               notice_rule=notice_rule,
               notice_by=_weekdays_before(start, P(notice_rule, "lead_time_working_days")) if start else None,
               cgp=disturbed_acres is not None and disturbed_acres >= P(SW_1ACRE, "small_construction_min_acres"),
               rrp=residential and pre1978 and scope in ("addition", "alteration", "tenant_improvement"),
               rrp_year=rrp_year)
    s.assumptions.extend(assumptions)
    s.special_inspections = scope in ("new_building", "addition") and not _woodframe_exempt(s)
    if start is None:
        s.assumptions.append("start_date not given: deadlines are shown relative to the start.")
    elif start < today:
        s.assumptions.append(f"start_date {start.isoformat()} is before as_of {today.isoformat()}: pre-start items show "
                             "as overdue.")

    for build in (_contract, _licensing, _permits, _environmental, _calosha, _lien, _public_works, _site, _closeout):
        build(s)

    phases = []
    for name in PHASES:
        items = sorted(s.items[name], key=lambda i: (i.due_date is None, i.due_date or ""))
        phases.append(Phase(name=name, applies=bool(items), items=items, notes=s.notes[name]))
    all_items = [(p.name, i) for p in phases for i in p.items]
    timeline = sorted((TimelineEntry(date=i.due_date, phase=n, title=i.title, timing=i.timing)
                       for n, i in all_items if i.due_date), key=lambda t: t.date)
    pros = list(dict.fromkeys(f"{i.requires_licensed_professional} - {i.title}" for _, i in all_items
                              if i.requires_licensed_professional))
    res = ProjectSetupResult(
        project_name=project_name, city=city, county=county, as_of=today.isoformat(),
        holidays=[d.isoformat() for d in sorted(skip)],
        start_date=start.isoformat() if start else None, estimated_completion_date=end.isoformat() if end else None,
        scope=scope, contractor_role=contractor_role, owner_type=owner_type, public_work=public_work,
        public_owner=public_owner, home_improvement_contract=hic,
        private_retention_cap_applies=None if public_owner else _private_cap_applies(s),
        phases=phases, timeline=timeline, licensed_professionals=pros,
        license=s.license, assumptions=s.assumptions, not_confirmed=s.not_confirmed,
        unverified_sources=[{"key": k, "citation": UNVERIFIED[k]["citation"], "source": UNVERIFIED[k]["source"]}
                            for k in s.unverified_used])
    res.cite(*dict.fromkeys(r for _, i in all_items for r in i.rule_ids))
    res.unverified_rule_ids = [c.rule_id for c in res.citations if not c.verified]
    if s.unverified_used or s.not_confirmed:
        res.verified = False
    return res


def project_risk_score(**kwargs) -> ProjectRiskResult:
    """Grades each phase of project_setup_checklist red / amber / green (same arguments)."""
    chk = project_setup_checklist(**kwargs)
    order = {"green": 0, "amber": 1, "red": 2}
    out = []
    for p in chk.phases:
        r = PhaseRisk(phase=p.name, applies=p.applies, rating="green", items=len(p.items),
                      overdue=sum(i.timing == "overdue" for i in p.items),
                      due_soon=sum(i.timing == "due_soon" for i in p.items),
                      blockers=sum(i.severity == "blocker" for i in p.items),
                      warnings=sum(i.severity == "warning" for i in p.items),
                      unverified_items=sum(not i.verified for i in p.items))
        for i in p.items:
            if i.severity == "blocker":
                r.reasons.append(f"BLOCKER: {i.title}")
            if i.timing == "overdue":
                r.reasons.append(f"Past due {i.due_date}: {i.title}")
            elif i.timing == "due_soon":
                r.reasons.append(f"Due {i.due_date}: {i.title}")
            if i.severity == "warning":
                r.reasons.append(f"Needs attention: {i.title}")
        if r.blockers or r.overdue:
            r.rating = "red"
        elif r.due_soon or r.warnings:
            r.rating = "amber"
        if p.name == LICENSING and r.rating != "green":
            r.reasons += [f"Note: {n}" for n in p.notes]
        out.append(r)
    overall = max((r.rating for r in out), key=order.get)
    upcoming = [i for p in chk.phases for i in p.items if i.due_date]
    upcoming.sort(key=lambda i: i.due_date)
    nxt = [f"{i.due_date} [{i.timing}] {i.title}" for i in upcoming if i.timing != "upcoming"][:8]
    nxt += [f"{i.due_date} {i.title}" for i in upcoming if i.timing == "upcoming"][:max(0, 5 - len(nxt))]
    return ProjectRiskResult(project_name=chk.project_name, as_of=chk.as_of, start_date=chk.start_date,
                             overall=overall, phases=out, next_actions=nxt, assumptions=chk.assumptions,
                             citations=chk.citations, verified=chk.verified)


# ---------------------------------------------------------------- MCP tools
# One Annotated alias per parameter, shared by both tools so their schemas and descriptions stay identical.
_Name = Annotated[str, Field(description="Project name shown in the report")]
_City = Annotated[str, Field(description="City the site is in (permit jurisdiction, business tax), e.g. 'Oakland'; "
                                         "'' if unincorporated")]
_County = Annotated[str, Field(description="County the site is in, e.g. 'Alameda' (picks the 811 center and the air "
                                           "district wording)")]
_Owner = Annotated[OwnerType, Field(description="Who owns the property: private_individual (homeowner), "
                                                "private_company, or public_agency (city, county, school district, "
                                                "state)")]
_HIC = Annotated[bool, Field(description="True if you contract with the owner or tenant of an existing residential "
                                         "property to repair, remodel or add to it, including an ADU on that lot. "
                                         "Needs residential_units >= 1")]
_Units = Annotated[int, Field(ge=0, description="Dwelling units in the building (0 = nonresidential; an ADU = 1)")]
_Price = Annotated[float, Field(gt=0, allow_inf_nan=False, description="Your contract price in USD (your "
                                                                       "subcontract price if you are a sub)")]
_Start = Annotated[Optional[str], Field(description="ISO date your work starts (first labor or material delivery), "
                                                    "e.g. 2027-01-11. Omit to get relative deadlines")]
_Valuation = Annotated[Optional[float], Field(gt=0, allow_inf_nan=False, description=(
    "Permit valuation in USD; defaults to the contract price (drives the state SMIP and building standards fees)"))]
_SqFt = Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description=(
    "New or added floor area in sq ft (school developer fees); omit if none"))]
_Stories = Annotated[int, Field(ge=1, description="Stories above grade (retention cap exemption, SMIP category, plan "
                                                  "stamp, Cal/OSHA permits)")]
_Height = Annotated[Optional[float], Field(gt=0, allow_inf_nan=False, description=(
    "Finished height in ft; over 36 ft triggers Cal/OSHA structure and scaffold permits. Omit to estimate from "
    "stories"))]
_Demo = Annotated[bool, Field(description="Is any structure demolished (asbestos notice, demolition permit)?")]
_Year = Annotated[Optional[int], Field(ge=1, description="Year the existing building was built (lead, asbestos, "
                                                         "plumbing fixture rules); omit for a vacant lot")]
_Depth = Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description=(
    "Deepest trench or excavation in ft (0 = no digging; omitted = footings assumed for new buildings and "
    "additions)"))]
_Acres = Annotated[Optional[float], Field(ge=0, allow_inf_nan=False, description=(
    "Acres of soil disturbed (1 acre or more needs Construction General Permit stormwater coverage)"))]
_Subs = Annotated[bool, Field(description="Will you hire subcontractors?")]
_Lender = Annotated[bool, Field(description="Is a construction loan funding the work?")]
_Funds = Annotated[bool, Field(description="Any public money in the project: grant, below-market loan, fee waiver, "
                                           "donated land or other subsidy (may make it a prevailing-wage public "
                                           "work)")]
_Role = Annotated[ContractorRole, Field(description="Your role: direct_contractor (contract with the owner) or "
                                                    "subcontractor")]
_License = Annotated[Optional[str], Field(description="Your CSLB license number; looked up in the CSLB data on this "
                                                      "server (status, classes, bond, workers' comp)")]
_AsOf = Annotated[Optional[str], Field(description="ISO date to compute from (default: today in California); items "
                                                   "due before it show as overdue")]
_Scope = Annotated[Optional[Scope], Field(description="new_building, addition, alteration, tenant_improvement or "
                                                      "demolition_only. Omit to infer (no year_built = new_building)")]
_Crew = Annotated[Optional[int], Field(ge=0, description="Most of YOUR employees on site at once (toilets, water, "
                                                         "IIPP; any employees with a workers' comp exemption is a "
                                                         "blocker)")]
_End = Annotated[Optional[str], Field(description="ISO date the whole project should be complete (notice of "
                                                  "completion, lien, retention and closeout dates)")]
_Mixed = Annotated[bool, Field(description="Residential units combined with commercial space in the same building "
                                           "(affects the 5% private retention cap, NESHAP, SMIP, plan stamps)")]
_Holidays = Annotated[Optional[list[str]], Field(description="ISO dates that are not working days (state/federal "
                                                             "holidays, agency closures) near the start or completion, "
                                                             "e.g. ['2026-12-25', '2027-01-01']; skipped like weekends "
                                                             "in the 811, Cal/OSHA permit, DAS 142, permit-review and "
                                                             "inspection counts (the NESHAP asbestos notice always "
                                                             "counts holidays)")]


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}

    @mcp.tool(name="project_setup_checklist", annotations=ro)
    def project_setup_checklist_tool(
        project_name: _Name, city: _City, county: _County, owner_type: _Owner, is_home_improvement: _HIC,
        residential_units: _Units, contract_price_usd: _Price, start_date: _Start = None,
        valuation_usd: _Valuation = None, sq_ft: _SqFt = None, stories: _Stories = 1, height_ft: _Height = None,
        demolition: _Demo = False, year_built: _Year = None, excavation_depth_ft: _Depth = None,
        disturbed_acres: _Acres = None, uses_subcontractors: _Subs = True, has_construction_lender: _Lender = False,
        public_funds: _Funds = False, contractor_role: _Role = "direct_contractor",
        contractor_license_number: _License = None, as_of: _AsOf = None, scope: _Scope = None,
        peak_crew_size: _Crew = None, estimated_completion_date: _End = None, mixed_use: _Mixed = False,
        holidays: _Holidays = None,
    ) -> ProjectSetupResult:
        """Turn a California project profile into an ordered, dated 'before you break ground' checklist.

        Returns nine phases in order - Contract (home improvement contract rules for any owner or tenant of
        residential property over $500; the 5% private retention cap, which exempts residential non-mixed-use
        projects of 4 stories or less; public retention); Licensing & insurance; Permits & plan check (state review
        clocks, ADU 60-day clock, state fees, school fees); Environmental, stormwater & asbestos (survey,
        10-working-day air district demolition notice, lead RRP, Construction General Permit); Cal/OSHA permits &
        programs (trench/36 ft permits, IIPP, heat, WVPP, excavation, fall, silica); Lien & payment protection
        (preliminary notice dated from first furnishing, lender copy; public-owner jobs get stop payment notice rules
        instead); Public works (prevailing wage, DIR, eCPR, DAS 140/142 if public owner or public funds; bid listing,
        payment bond and public retention only with a public owner); Site setup & postings (posters, toilets, 811
        call window); Closeout preview (notice of completion, lien deadline, retention, Title 24 CF2R/CF3R).
        Each item has title, why, due (ISO date with the rule, or relative), due_date, timing (overdue/due_soon/
        upcoming/relative), owner_role, requires_licensed_professional (architect, PE/SE, QSD/QSP, HERS rater, CAC,
        competent person, CASp...), math, severity (ok/warning/blocker), citation, rule_ids, verified and
        not_confirmed. Also returns a dated timeline, the licensed professionals to line up, the license lookup,
        assumptions, unverified rules/sources and not_confirmed items. Working-day counts skip weekends and the
        `holidays` you pass; other holidays are not skipped. Invalid input (negative numbers, bad dates, a home
        improvement job with 0 dwelling units) returns an error naming the field. Not legal advice."""
        return project_setup_checklist(**locals())

    @mcp.tool(name="project_risk_score", annotations=ro)
    def project_risk_score_tool(
        project_name: _Name, city: _City, county: _County, owner_type: _Owner, is_home_improvement: _HIC,
        residential_units: _Units, contract_price_usd: _Price, start_date: _Start = None,
        valuation_usd: _Valuation = None, sq_ft: _SqFt = None, stories: _Stories = 1, height_ft: _Height = None,
        demolition: _Demo = False, year_built: _Year = None, excavation_depth_ft: _Depth = None,
        disturbed_acres: _Acres = None, uses_subcontractors: _Subs = True, has_construction_lender: _Lender = False,
        public_funds: _Funds = False, contractor_role: _Role = "direct_contractor",
        contractor_license_number: _License = None, as_of: _AsOf = None, scope: _Scope = None,
        peak_crew_size: _Crew = None, estimated_completion_date: _End = None, mixed_use: _Mixed = False,
        holidays: _Holidays = None,
    ) -> ProjectRiskResult:
        """Grade project readiness per phase: red / amber / green with reasons, plus the next actions by date.

        Builds the same checklist as project_setup_checklist (same arguments) and rates each phase: red if any
        item is past due or a blocker (license not active or not in CSLB's data, bond missing, workers' comp
        exemption on a class that must carry it or with employees on site); amber if an item is due within 14 days
        of as_of or needs attention (e.g. license expires before completion, public-funds coverage unclear); green
        otherwise. Returns overall rating, per-phase counts (items, overdue, due_soon, blockers, warnings,
        unverified_items), reasons and next_actions (overdue and due-soon items first, then the next dated ones).
        Call project_setup_checklist for the full items. Not legal advice."""
        return project_risk_score(**locals())
