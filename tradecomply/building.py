"""Building permits, Title 24 energy (HERS), CALGreen, stormwater and ADU rules for California contractors.

  permit_required              work + size/height/valuation -> building permit required or exempt (CBC 105.2 /
                               CRC R105.2 as the rulebook holds them; local amendments override), who may do the work
                               (B&P 7048 minor work, B&P 7044 owner-builder), smoke/CO alarm retrofit on permits over
                               $1,000, pre-1994 plumbing fixture retrofit, lead RRP / asbestos / pool flags, permit
                               counter documents, state SMIP + BSASRF fees and school fees with the math, and the
                               start-work-by (permit issue + 12 months) and demolition notice send-by dates
  energy_compliance_triggers   project type + scope -> 2025 Energy Code documents (CF1R/CF2R/CF3R or NRCC/NRCI), HERS
                               verifications (ducted or ductless), and the new single-family PV / battery-ready /
                               heat-pump baseline
  calgreen_and_stormwater      CALGreen 65% C&D diversion (with the math), low-disposal alternative, EV-capable,
                               nonresidential addition/alteration thresholds, Construction General Permit at 1 acre
  adu_rules                    ADU/JADU size, height and setback floors, 15-business-day completeness (skipping any
                               agency closures given) and 60-day approval clocks, impact-fee and school-fee exemptions
                               with the Level 1 estimate

Every legal number comes from the rulebook (core.rule(id).param(key)) at call time. The few rules the rulebook does not
hold are in UNVERIFIED below with their official source; any result that uses one has verified=False.
"""
import math
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
EXEMPT, EDITION, EXPIRE = ("permit-required-exemptions-thresholds", "title24-2025-edition-effective-2026-01-01",
                           "permit-expiration-12-months-state-exception")
MINOR_PERMIT, MINOR_DQ, MINOR_SPLIT, MINOR_ADS = ("minor-work-exemption-requires-no-permit",
                                                  "web_business-tax-insurance:minor-work-exemption-1000",
                                                  "minor-work-no-splitting", "unlicensed-advertising-under-1000-disclaimer")
WILLFUL, DISCIPLINE = "web_permits-building:unpermitted-work-presumed-willful", "discipline-guideline-no-permit"
LIC_STATEMENT, WC_DECL, HIC_PERMITS = ("permit-applicant-license-statement", "workers-comp-declaration-on-permit",
                                       "hic-identify-permits-and-payer")
OB_SELF, OB_TRADES, OB_SALE, OB_DUTIES, OB_CONSULT = ("owner-builder-self-performed", "owner-builder-licensed-trades",
                                                      "owner-builder-sale-presumptions",
                                                      "owner-builder-permit-responsibilities",
                                                      "owner-builder-consultant-requires-license")
ALARMS, FIXTURES = "smoke-co-alarms-on-permits-over-1000", "plumbing-fixture-upgrade-pre-1994"
RRP, RRP_MINOR = "rrp-applicability-pre-1978", "rrp-minor-repair-6-20-sqft"
DEMO_NOTICE, AIR_DISTRICT = ("demolition-permit-requires-air-district-notice",
                             "air-district-demolition-notice-10-working-days")
POOL, SMIP, BSASRF = "pool-spa-two-drowning-prevention-features", "smip-seismic-fee", "bsasrf-green-building-fee"
SCHOOL_RATE, SCHOOL_ADD, SCHOOL_CERT, SCHOOL_ADU = ("school-fee-level1-2026-rates", "school-fee-residential-addition-500sqft",
                                                    "school-fee-certificate-before-permit", "adu-school-fee-exempt-under-500")
ENERGY_DATE, CF1R, CF2R, CF3R = ("energy-code-2025-enforcement-date", "energy-cf1r-certificate-of-compliance",
                                 "energy-cf2r-certificate-of-installation", "energy-cf3r-hers-verification")
PV, BESS, HEAT_PUMP, HVAC = ("energy-sf-new-solar-pv-required", "energy-sf-battery-ready-125a",
                             "energy-sf-heat-pump-baseline", "energy-hvac-changeout-hers")
CG_WASTE, CG_LOW, CG_NONRES, CG_EV = ("calgreen-construction-waste-65-percent", "calgreen-waste-low-disposal-alternative",
                                      "calgreen-nonres-addition-alteration-trigger", "calgreen-ev-capable-one-two-family")
SW_DEF, SW_WAIVER = "stormwater-small-construction-1-acre-definition", "stormwater-erosivity-waiver"
CGP_NOI, CGP_QSD, CGP_QSP, CGP_QPE, CGP_NOT = ("cgp-noi-smarts-before-construction", "cgp-swppp-by-qsd",
                                               "cgp-qsp-implementation-inspections", "cgp-qualifying-precipitation-event",
                                               "cgp-notice-of-termination")
ADU_HIC, ADU_LIC, ADU_ISSUER = "adu-is-home-improvement-contract", "adu-license-classification-b-or-c47", "adu-permit-issuer"
ADU_60, ADU_15, ADU_MIN, ADU_CAP, ADU_HT = ("adu-60-day-deemed-approved", "adu-completeness-15-business-days",
                                            "adu-minimum-allowed-size", "adu-detached-max-1200-attached-50pct",
                                            "adu-height-and-setbacks")
ADU_IMPACT, JADU, AB1308 = ("adu-impact-fee-exempt-750", "jadu-owner-occupancy-ab1154",
                            "final-inspection-10-business-days-ab1308")
HOMEOWNER, SFD_CONTRACT, CANCEL = ("homeowner-principal-residence-exemption", "sfd-construction-contract-requirements",
                                   "three-day-right-to-cancel-notice")
ASB_LIC, ASB_DOSH, NESHAP_DAYS = ("asbestos-100-sqft-c22-or-certification", "asbestos-dosh-registration-100sqft",
                                  "neshap-notification-10-working-days")

ICC_CA = "https://codes.iccsafe.org/codes/california (2025 CBC/CRC Chapter 1 Div. II); https://www.dgs.ca.gov/BSC/Codes"
# Law the rulebook does not hold. Any result that uses an entry lists it in unverified_sources and has verified=False.
UNVERIFIED = {
    "cbc-105.2-finish-work": {
        "citation": "CBC 105.2 Building item 7; CRC R105.2 Building item 6 (model-code text)", "source": ICC_CA,
        "note": "Painting, papering, tiling, carpeting, cabinets, counter tops and similar finish work are exempt from "
                "a building permit. Cities may amend Section 105.2."},
    "cbc-105.2-sidewalks-driveways": {
        "citation": "CBC 105.2 Building item 6; CRC R105.2 Building item 5 (model-code text)", "source": ICC_CA,
        "max_height_above_grade_in": 30,
        "note": "Sidewalks and driveways not more than 30 inches above adjacent grade, not over any basement or story "
                "below and (CBC) not part of an accessible route are exempt. The CRC item may be broader."},
    "crc-r105.2-decks": {
        "citation": "CRC R105.2 Building item 10 (model-code text)", "source": ICC_CA,
        "max_sqft": 200, "max_height_above_grade_in": 30,
        "note": "Decks not over 200 sq ft, not more than 30 inches above grade at any point, not attached to a dwelling "
                "and not serving the required exit door (R311.4) are exempt. One- and two-family dwellings only; "
                "many California cities amend or delete this item."},
    "crc-r105.2-playground": {
        "citation": "CRC R105.2 Building item 8; CBC 105.2 Building item 11 (model-code text)", "source": ICC_CA,
        "note": "Swings and other playground equipment accessory to one- and two-family dwellings are exempt."},
    "cbc-105.2.2-ordinary-repairs": {
        "citation": "CBC 105.2.2 / CRC R105.2.2 Repairs (model-code text)", "source": ICC_CA,
        "note": "Ordinary repairs need no permit, but not if they cut away a wall or partition, remove or cut a "
                "structural beam or load-bearing support, change required egress, or add, alter, replace or relocate "
                "water, sewer, drainage, gas, waste or vent piping, electric wiring or mechanical work."},
    "cbc-105.1-mep-permits": {
        "citation": "CBC 105.1 / CRC R105.1 (model-code text)", "source": ICC_CA,
        "note": "A permit is needed to install, enlarge, alter, repair, remove, convert or replace any electrical, gas, "
                "mechanical or plumbing system regulated by the code (issued as electrical, plumbing or mechanical "
                "permits in most cities)."},
    "hsc-19211-water-heater-strapping": {
        "citation": "Health & Safety Code 19211",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=HSC&sectionNum=19211",
        "note": "New and replacement water heaters must be braced, anchored or strapped to resist falling or "
                "horizontal displacement in an earthquake; inspectors check this at final."},
    "civ-1101.5-d-fixture-triggers": {
        "citation": "Civil Code 1101.5(d)(1)(A)-(C) (multifamily and commercial real property)",
        "source": "https://law.justia.com/codes/california/code-civ/division-2/part-4/title-4/chapter-2/article-1-4/section-1101-5",
        "addition_floor_area_increase_gt_percent": 10,
        "note": "Pre-1994 multifamily/commercial buildings: (A) additions whose concurrent permits by the same applicant "
                "increase the building's floor area by more than 10 percent -> replace all noncompliant fixtures in "
                "the building; (B) alterations or improvements with an estimated permit construction cost over "
                "$150,000 -> replace the noncompliant fixtures serving the area of the work; (C) any permitted "
                "alteration of a room that contains noncompliant fixtures -> replace those fixtures in that room."},
    "gov-65995-b1-assessable-space": {
        "citation": "Gov Code 65995(b)(1)",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=GOV&sectionNum=65995",
        "note": "Residential school fees are charged on assessable space: the square footage within the perimeter of "
                "the residential structure, not counting a carport, walkway, garage, overhang, patio, enclosed patio, "
                "detached accessory structure or similar structure."},
    "gov-66313-jadu-definition": {
        "citation": "Gov Code 66313(d) (formerly 65852.22(h)(1))",
        "source": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=GOV&sectionNum=66313",
        "note": "A junior accessory dwelling unit is a unit of no more than 500 sq ft contained entirely within a "
                "single-family residence; it may have its own bathroom or share the residence's."},
    "calgreen-301.1.1-residential-additions": {
        "citation": "CALGreen (Title 24 Part 11) 301.1.1", "source": "https://www.dgs.ca.gov/BSC/CALGreen",
        "note": "For existing residential buildings, CALGreen mandatory measures apply to additions or alterations "
                "that increase the building's conditioned area, volume or size, and only to the added or altered "
                "portion."},
}

ProjectType = Literal["single_family", "multifamily_low_rise", "multifamily_high_rise", "nonresidential"]
WorkType = Literal[
    "detached_accessory_structure", "fence", "retaining_wall", "finish_work", "sidewalk_or_driveway", "deck",
    "playground_equipment", "ordinary_repair", "equipment_repair_or_identical_part", "new_building", "addition",
    "alteration", "structural_repair", "demolition", "pool_or_spa", "reroof", "window_replacement",
    "change_of_occupancy", "hvac_changeout", "water_heater", "electrical", "plumbing", "solar_pv", "other"]
Determination = Literal["not_required", "required", "need_more_info", "check_with_building_department"]
OwnerBuilderWork = Literal["owner_or_own_employees", "licensed_contractors", "unlicensed_workers_paid_by_job"]
EnergyScope = Literal["new_construction", "addition", "alteration", "hvac_changeout", "duct_alteration", "water_heater",
                      "reroof", "window_replacement", "lighting_alteration"]
GreenScope = Literal["new_construction", "addition", "alteration"]
AduType = Literal["detached", "attached", "conversion", "jadu"]
LotType = Literal["single_family", "multifamily"]

RESIDENTIAL = ("single_family", "multifamily_low_rise", "multifamily_high_rise")
MEP_TYPES = {"hvac_changeout": "mechanical", "water_heater": "plumbing", "electrical": "electrical",
             "plumbing": "plumbing", "solar_pv": "electrical"}
ALTERATIONS = {"alteration", "structural_repair", "reroof", "window_replacement", "change_of_occupancy",
               "hvac_changeout", "water_heater", "electrical", "plumbing", "solar_pv"}
PAINT_DISTURBING = {"addition", "alteration", "structural_repair", "demolition", "window_replacement", "finish_work",
                    "reroof", "ordinary_repair"}
# Regexes matched at a word start in the job description. Kept broad for fixtures (a missed plumbing permit is worse
# than an extra flag) but not on carpentry/site words: "plumb and level", "wood paneling", "fence panel", "gasket",
# "French drain", "site drainage" and "ductless" do not count.
KEYWORDS = {"electrical": (r"electric", r"outlets?\b", r"receptacles?\b", r"wiring", r"circuits?\b",
                           r"(?:electrical|main|service|sub|breaker|solar) ?panels?\b", r"panel (?:upgrade|replace|swap|change)",
                           r"ev charg", r"lighting\b", r"light fixtures?\b", r"solar pv\b", r"photovoltaic"),
            "plumbing": (r"plumbing", r"plumber", r"water heaters?\b", r"sewer", r"(?<!french )drains?\b", r"toilets?\b",
                         r"sinks?\b", r"showers?\b", r"(?:bath)?tubs?\b", r"water lines?\b", r"re-?pip"),
            "mechanical": (r"hvac", r"furnace", r"duct(?:s|work)?\b", r"air condition", r"heat pumps?\b", r"mini[- ]?splits?\b",
                           r"(?:exhaust|bath) fans?\b", r"range hoods?\b"),
            "gas": (r"gas\b",)}
# "no power", "without electrical or plumbing", "no new gas": drop the negated words before matching.
NEGATED = re.compile(r"\b(?:no|without)\s+(?:new\s+)?\w+(?:\s*(?:,|/|\bor\b|\band\b)\s*\w+){0,3}")
ENGINEER = ("California-licensed civil or structural engineer (expect the building department to require stamped "
            "calculations/plans)")


# ---------------------------------------------------------------- helpers
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
        raise ValueError(f"{field} must be an ISO date like 2026-11-25, got {value!r}") from None


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _g(x) -> str:
    return f"{x:,g}" if isinstance(x, (int, float)) else str(x)


def _round2(x: float, up: bool) -> float:
    """2-decimal display rounded toward the failing side of a limit, so 64.996% shows 64.99% (< 65%), never 65%."""
    return (math.ceil(x * 100 - 1e-9) if up else math.floor(x * 100 + 1e-9)) / 100


def _num(pattern: str, text: str) -> float:
    """Pull a number the rulebook states inside a text parameter (e.g. '25 ft or ...'). Fails loudly if it moved."""
    m = re.search(pattern, str(text))
    if not m:
        raise ValueError(f"rulebook text {text!r} no longer matches {pattern!r}; update tradecomply/building.py")
    return float(m.group(1))


def _check(**values) -> None:
    """Reject negative, NaN and infinite numbers with the parameter's name (None means 'not given')."""
    for name, v in values.items():
        if v is not None and (not math.isfinite(v) or v < 0):
            raise ValueError(f"{name} must be a finite number of 0 or more, got {v!r}")


def _add_business_days(d: date, n: int, closed: frozenset = frozenset()) -> date:
    """Move n Mon-Fri days forward (n < 0: back), skipping `closed` dates. Holidays are skipped only if listed."""
    step = 1 if n >= 0 else -1
    n = abs(n)
    while n > 0:
        d += timedelta(days=step)
        if d.weekday() < 5 and d not in closed:
            n -= 1
    return d


def _add_months(d: date, months: int) -> date:
    """Same day `months` later; a day the month lacks (Feb 29/30/31) becomes that month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return min(date(y, m, 1) + timedelta(days=d.day - 1), nxt - timedelta(days=1))


class LineItem(BaseModel):
    item: str
    value: str
    math: str = ""
    rule_id: Optional[str] = None
    verified: bool = False


class Step(BaseModel):
    task: str
    who: str
    when: str = ""
    rule_id: Optional[str] = None
    verified: bool = False


class _Ctx:
    """Collects the rules and unverified sources a result relied on, plus its line items and checklist."""

    def __init__(self):
        self.rules: list[str] = []
        self.unv: list[str] = []
        self.items: list[LineItem] = []
        self.steps: list[Step] = []
        self.pros: list[str] = []
        self.notes: list[str] = []
        self.warnings: list[str] = []

    def use(self, *rule_ids: str) -> None:
        for r in rule_ids:
            if r and r not in self.rules:
                core.rule(r)
                self.rules.append(r)

    def P(self, rule_id: str, key: str, default=KeyError):
        self.use(rule_id)
        return core.rule(rule_id).param(key, default)

    def U(self, key: str) -> dict:
        if key not in self.unv:
            self.unv.append(key)
        return UNVERIFIED[key]

    def _ref(self, rule_id: Optional[str]) -> tuple[Optional[str], bool]:
        if rule_id in UNVERIFIED:
            self.U(rule_id)
            return f"UNVERIFIED:{rule_id}", False
        if rule_id:
            self.use(rule_id)
            return rule_id, core.rule(rule_id).verified
        return None, False

    def item(self, item: str, value, math: str = "", rule_id: Optional[str] = None) -> None:
        rid, ok = self._ref(rule_id)
        self.items.append(LineItem(item=item, value=str(value), math=math, rule_id=rid, verified=ok))

    def step(self, task: str, who: str, when: str = "", rule_id: Optional[str] = None) -> None:
        rid, ok = self._ref(rule_id)
        self.steps.append(Step(task=task, who=who, when=when, rule_id=rid, verified=ok))

    def pro(self, text: str) -> None:
        if text not in self.pros:
            self.pros.append(text)

    def note(self, text: Optional[str], rule_id: Optional[str] = None) -> None:
        self._ref(rule_id)
        if text and text not in self.notes:
            self.notes.append(text)


class _Result(core.Result):
    as_of: str
    line_items: list[LineItem] = Field(default_factory=list)
    checklist: list[Step] = Field(default_factory=list)
    licensed_professionals: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    unverified_sources: list[dict] = Field(default_factory=list)


def _finish(res: _Result, ctx: _Ctx) -> _Result:
    res.cite(*ctx.rules)
    res.line_items, res.checklist, res.licensed_professionals = ctx.items, ctx.steps, ctx.pros
    res.notes = ctx.notes
    res.unverified_rule_ids = sorted(r for r in ctx.rules if not core.rule(r).verified)
    res.unverified_sources = [{"key": k, **UNVERIFIED[k]} for k in ctx.unv]
    if ctx.unv:
        res.verified = False
    if not res.verified:
        res.notes.append("Not fully verified: relies on " + ", ".join(res.unverified_rule_ids + ctx.unv)
                         + ". Confirm those with the building department or the official text before relying on them.")
    return res


def _school_fee(ctx: _Ctx, sq_ft: float, use: Literal["residential", "commercial"], on: date,
                district_rate: Optional[float] = None, label: str = "School fee") -> float:
    """Level 1 school developer fee: sq ft x rate. The State Allocation Board cap changes on sab_adjustment_date."""
    adj = date.fromisoformat(ctx.P(SCHOOL_RATE, "sab_adjustment_date"))
    key = ("residential_max_usd_per_sqft" if use == "residential" else "commercial_industrial_max_usd_per_sqft")
    if on < adj:
        key = "previous_residential_usd_per_sqft" if use == "residential" else "previous_commercial_usd_per_sqft"
    cap = ctx.P(SCHOOL_RATE, key)
    rate = cap if district_rate is None else district_rate
    fee = _money(sq_ft * rate)
    basis = (f"district rate you entered (Level 1 cap {_usd(cap)}/sq ft)" if district_rate is not None
             else f"Level 1 {use} cap {'after' if on >= adj else 'before'} the {adj.isoformat()} adjustment")
    ctx.item(label, _usd(fee), f"{_g(sq_ft)} sq ft x {_usd(rate)}/sq ft ({basis}) = {_usd(fee)}", SCHOOL_RATE)
    if district_rate is not None and district_rate > cap:
        ctx.note(f"The district rate you entered ({_usd(district_rate)}/sq ft) is above the Level 1 cap "
                 f"({_usd(cap)}/sq ft); that is lawful only under a Level 2/3 fee adoption. Ask the district.")
    ctx.note("School fees are paid to the school district, not the building department. The rate is a maximum: each "
             "district adopts its own rate (Level 2/3 can be higher), and new caps take effect only after the "
             "district board acts. Get the district's current rate sheet.", SCHOOL_RATE)
    ctx.step("Pay the school district fee (or get its exemption letter) and bring the district's certificate to the "
             "permit counter", "Owner or permit applicant", "Before building permit issuance", SCHOOL_CERT)
    return fee


# ================================================================ permit_required
class PermitRequiredResult(_Result):
    work_description: str
    structure_type: str
    project_type: str
    code_section: str
    code_edition: int
    determination: Determination
    building_permit_required: Optional[bool] = None
    basis: str = ""
    separate_trade_permits_likely: list[str] = Field(default_factory=list)
    license_required: Optional[bool] = None
    license_basis: str = ""
    minor_work_exemption_available: Optional[bool] = None
    smoke_alarms_required: Optional[bool] = None
    co_alarms_required: Optional[bool] = None
    plumbing_fixture_retrofit_required: Optional[bool] = None
    lead_rrp_applies: Optional[bool] = None
    owner_builder_warnings: list[str] = Field(default_factory=list)
    state_fees_usd: dict[str, float] = Field(default_factory=dict)
    state_fees_total_usd: Optional[float] = None
    school_fee_estimate_usd: Optional[float] = None
    start_work_by: Optional[str] = None
    demolition_notice_send_by: Optional[str] = None


def _exemption(ctx: _Ctx, t: str, ptype: str, sq_ft, fence_h, wall_h, one_story_detached, surcharge, liquids,
               pool_barrier, h_in, deck_attached, affects) -> tuple[Determination, str]:
    crc = ptype == "single_family"
    code = "CRC R105.2" if crc else "CBC 105.2"
    if t == "detached_accessory_structure":
        max_sf = ctx.P(EXEMPT, "detached_accessory_structure_max_sqft")
        max_st = ctx.P(EXEMPT, "detached_accessory_structure_max_stories")
        if one_story_detached is False:
            return "required", (f"Not a {max_st}-story detached accessory structure, so the {code} shed exemption "
                                "does not apply.")
        if sq_ft is None or one_story_detached is None:
            return "need_more_info", (f"Give sq_ft and is_one_story_detached_accessory: {code} exempts only a "
                                      f"{max_st}-story detached accessory structure of {max_sf} sq ft or less.")
        ok = sq_ft <= max_sf
        ctx.item("Accessory structure floor area vs exemption", f"{_g(sq_ft)} sq ft",
                 f"{_g(sq_ft)} {'<=' if ok else '>'} {max_sf} sq ft, {max_st} story, detached -> "
                 f"{'exempt' if ok else 'permit required'}", EXEMPT)
        return ("not_required", f"{code}: one-story detached accessory structure (tool shed, playhouse) of "
                                f"{_g(sq_ft)} sq ft is within the {max_sf} sq ft exemption.") if ok else \
               ("required", f"{code}: {_g(sq_ft)} sq ft is over the {max_sf} sq ft shed/playhouse exemption.")
    if t == "fence":
        max_h, excl = ctx.P(EXEMPT, "fence_max_height_ft"), ctx.P(EXEMPT, "fence_exemption_excludes")
        if pool_barrier:
            return "required", f"{code}: the fence exemption excludes {excl}; a pool barrier fence needs a permit."
        if fence_h is None:
            return "need_more_info", f"Give fence_height_ft: {code} exempts fences not over {max_h} ft."
        ok = fence_h <= max_h
        ctx.item("Fence height vs exemption", f"{_g(fence_h)} ft",
                 f"{_g(fence_h)} {'<=' if ok else '>'} {max_h} ft -> {'exempt' if ok else 'permit required'}", EXEMPT)
        return ("not_required", f"{code}: fence of {_g(fence_h)} ft is not over {max_h} ft.") if ok else \
               ("required", f"{code}: fence of {_g(fence_h)} ft is over the {max_h} ft exemption.")
    if t == "retaining_wall":
        max_h, how = ctx.P(EXEMPT, "retaining_wall_max_height_ft"), ctx.P(EXEMPT, "retaining_wall_measurement")
        void = ctx.P(EXEMPT, "retaining_wall_exemption_void_if")
        if surcharge or liquids:
            ctx.pro(ENGINEER)
            return "required", (f"{code}: the retaining wall exemption is void if the wall "
                                f"{' or '.join(void)}; this wall does, so it needs a permit at any height.")
        if wall_h is None:
            return "need_more_info", (f"Give retaining_wall_height_ft measured {how}: {code} exempts walls not over "
                                      f"{max_h} ft.")
        ok = wall_h <= max_h
        ctx.item("Retaining wall height vs exemption", f"{_g(wall_h)} ft ({how})",
                 f"{_g(wall_h)} {'<=' if ok else '>'} {max_h} ft, no surcharge -> "
                 f"{'exempt' if ok else 'permit required'}", EXEMPT)
        if not ok:
            ctx.pro(ENGINEER)
            return "required", f"{code}: {_g(wall_h)} ft ({how}) is over the {max_h} ft retaining wall exemption."
        return "not_required", f"{code}: retaining wall of {_g(wall_h)} ft ({how}) with no surcharge is exempt."
    if t == "finish_work":
        u = ctx.U("cbc-105.2-finish-work")
        if affects:
            ctx.use(EXEMPT)
            return "required", ("Work that cuts walls, changes structure or egress, or moves plumbing, wiring or gas "
                                "is an alteration, not finish work; alterations need a permit.")
        ctx.item("Finish work exemption", "exempt", u["note"], "cbc-105.2-finish-work")
        return "not_required", f"{u['citation']}: finish work (painting, tile, carpet, cabinets, counters) is exempt."
    if t == "sidewalk_or_driveway":
        u = ctx.U("cbc-105.2-sidewalks-driveways")
        max_in = u["max_height_above_grade_in"]
        assumed = h_in is None
        if assumed:
            ctx.note(f"Assumed the slab is at grade (not more than {max_in} in above adjacent grade) and not over a "
                     "basement or story below; give height_above_grade_in if it is raised.")
            h_in = 0
        ok = h_in <= max_in
        ctx.item("Sidewalk/driveway height above adjacent grade", "at grade (assumed)" if assumed else f"{_g(h_in)} in",
                 f"{_g(h_in)} {'<=' if ok else '>'} {max_in} in -> {'exempt' if ok else 'permit required'}",
                 "cbc-105.2-sidewalks-driveways")
        ctx.note("Driveway approaches, curb cuts and work in the public sidewalk need the city's public works "
                 "(encroachment / right-of-way) permit even when no building permit is needed. Not in the rulebook; "
                 "ask the city's public works department.")
        if not crc:
            ctx.note("CBC: the sidewalk exemption does not cover a walk that is part of an accessible route.")
        return ("not_required" if ok else "required"), u["citation"] + ": " + u["note"]
    if t == "deck":
        if not crc:
            ctx.use(EXEMPT)
            return "required", ("The deck exemption is a CRC (one- and two-family dwelling) item; the CBC has no deck "
                                "exemption, so this deck needs a permit.")
        u = ctx.U("crc-r105.2-decks")
        if deck_attached:
            return "required", "CRC R105.2: a deck attached to the dwelling or serving the required exit door is not exempt."
        if sq_ft is None or h_in is None:
            return "need_more_info", (f"Give sq_ft and height_above_grade_in: CRC exempts detached decks up to "
                                      f"{u['max_sqft']} sq ft and {u['max_height_above_grade_in']} in above grade.")
        ok = sq_ft <= u["max_sqft"] and h_in <= u["max_height_above_grade_in"]
        ctx.item("Deck size and height vs exemption", f"{_g(sq_ft)} sq ft, {_g(h_in)} in above grade",
                 f"{_g(sq_ft)} {'<=' if sq_ft <= u['max_sqft'] else '>'} {u['max_sqft']} sq ft and {_g(h_in)} "
                 f"{'<=' if h_in <= u['max_height_above_grade_in'] else '>'} {u['max_height_above_grade_in']} in -> "
                 f"{'exempt' if ok else 'permit required'}", "crc-r105.2-decks")
        return ("not_required" if ok else "required"), u["citation"] + ": " + u["note"]
    if t == "playground_equipment":
        u = ctx.U("crc-r105.2-playground")
        if crc:
            return "not_required", u["citation"] + ": " + u["note"]
        return "check_with_building_department", ("The playground equipment exemption covers equipment accessory to "
                                                  "one- and two-family dwellings only.")
    if t == "ordinary_repair":
        u = ctx.U("cbc-105.2.2-ordinary-repairs")
        if affects:
            ctx.use(EXEMPT)
            return "required", ("Repairs that cut walls, touch structure or egress, or alter piping, wiring or "
                                "mechanical work are not ordinary repairs; a permit is required.")
        return "not_required", u["citation"] + ": " + u["note"]
    if t == "equipment_repair_or_identical_part":
        return "check_with_building_department", ("Equipment repair and like-for-like part replacement are not in the "
                                                  "Section 105.2 building exemptions held in the rulebook; the "
                                                  "building department decides whether a mechanical, plumbing or "
                                                  "electrical permit is needed.")
    if t in MEP_TYPES:
        u = ctx.U("cbc-105.1-mep-permits")
        if t == "hvac_changeout":
            ctx.use(HVAC)
        return "required", f"{u['citation']}: {u['note']}"
    if t == "other":
        ctx.use(EXEMPT)
        return "check_with_building_department", ("Not one of the exemptions the rulebook holds. A permit is required to "
                                                  "construct, alter, repair, move, demolish or change occupancy unless "
                                                  "Section 105.2 (as amended locally) exempts it.")
    ctx.use(EXEMPT)
    what = {"new_building": "construct", "addition": "enlarge (addition)", "alteration": "alter",
            "structural_repair": "repair", "demolition": "demolish", "pool_or_spa": "construct (pool/spa)",
            "reroof": "repair/alter (reroof)", "window_replacement": "alter (window replacement)",
            "change_of_occupancy": "change the occupancy of"}[t]
    return "required", (f"CBC 105.1 / CRC R105.1: a building permit is required to {what} a building or structure; "
                        "no Section 105.2 exemption applies.")


def permit_required(work_description: str, structure_type: WorkType, project_type: ProjectType = "single_family",
                    valuation_usd: Optional[float] = None, sq_ft: Optional[float] = None,
                    fence_height_ft: Optional[float] = None, retaining_wall_height_ft: Optional[float] = None,
                    is_one_story_detached_accessory: Optional[bool] = None,
                    retaining_wall_supports_surcharge: bool = False,
                    retaining_wall_impounds_flammable_liquids: bool = False, is_pool_barrier: bool = False,
                    height_above_grade_in: Optional[float] = None, deck_attached_or_serves_exit_door: bool = False,
                    affects_structure_egress_or_utilities: bool = False,
                    includes_electrical_plumbing_or_gas: bool = False, stories: int = 1,
                    year_built: Optional[int] = None,
                    has_fuel_burning_appliance_or_attached_garage: Optional[bool] = None,
                    owner_builder: bool = False, owner_builder_work_by: OwnerBuilderWork = "owner_or_own_employees",
                    owner_intends_to_sell: bool = False, performer_advertises_or_has_employees: bool = False,
                    part_of_larger_project: bool = False, permit_application_date=None,
                    as_of=None, existing_building_sq_ft: Optional[float] = None,
                    owner_lived_in_home_12_months_before_completion: Optional[bool] = None,
                    permit_issued_date=None, demolition_start_date=None) -> PermitRequiredResult:
    _check(valuation_usd=valuation_usd, sq_ft=sq_ft, fence_height_ft=fence_height_ft,
           retaining_wall_height_ft=retaining_wall_height_ft, height_above_grade_in=height_above_grade_in,
           existing_building_sq_ft=existing_building_sq_ft)
    if stories < 1:
        raise ValueError("stories must be at least 1")
    today = _d(as_of, "as_of") or logic.today_ca()
    applied = _d(permit_application_date, "permit_application_date")
    issued = _d(permit_issued_date, "permit_issued_date")
    demo_start = _d(demolition_start_date, "demolition_start_date")
    if issued and applied and issued < applied:
        raise ValueError(f"permit_issued_date {issued} is before permit_application_date {applied}")
    app = applied or issued or today
    if year_built is not None and not 1700 <= year_built <= today.year:
        raise ValueError(f"year_built must be the existing building's year (1700-{today.year}), got {year_built}")
    ctx, t, val = _Ctx(), structure_type, valuation_usd
    dwelling = project_type in RESIDENTIAL

    # -- code edition
    eff = date.fromisoformat(ctx.P(EDITION, "effective_date"))
    edition = ctx.P(EDITION, "edition") if app >= eff else ctx.P(EDITION, "prior_edition")
    ctx.item("Code edition", f"{edition} California Building Standards Code",
             f"permit application {app.isoformat()}{' (issue date; application date not given)' if not applied and issued else ''} "
             f"{'>=' if app >= eff else '<'} {eff.isoformat()}", EDITION)
    if not applied and issued and app >= eff:
        ctx.note(f"The code edition follows the application date, not the issue date: if the application went in before "
                 f"{eff.isoformat()}, the {ctx.P(EDITION, 'prior_edition')} codes apply. Give permit_application_date.")

    # -- building permit
    det, basis = _exemption(ctx, t, project_type, sq_ft, fence_height_ft, retaining_wall_height_ft,
                            is_one_story_detached_accessory, retaining_wall_supports_surcharge,
                            retaining_wall_impounds_flammable_liquids, is_pool_barrier, height_above_grade_in,
                            deck_attached_or_serves_exit_door, affects_structure_egress_or_utilities)
    required = {"required": True, "not_required": False}.get(det)
    variations = ctx.P(EXEMPT, "local_variations")
    ctx.note("Local amendments control: cities and counties may amend Section 105.2 (for example: "
             + "; ".join(variations) + "). Confirm with the local building department before skipping a permit.")
    if det == "not_required":
        ctx.note("Exempt work must still meet the code, zoning setbacks, HOA rules and utility rules; exemption from a "
                 "building permit does not exempt electrical, plumbing or mechanical work from its own permit.", EXEMPT)

    # -- trade permits
    text = NEGATED.sub(" ", work_description.lower())
    trades = [trade for trade, pats in KEYWORDS.items() if any(re.search(r"\b" + p, text) for p in pats)]
    if t in MEP_TYPES:
        trades.insert(0, MEP_TYPES[t])
    if includes_electrical_plumbing_or_gas and not trades:
        trades = ["electrical, plumbing or gas (as the work includes)"]
    trades = list(dict.fromkeys(trades))
    if trades:
        u = ctx.U("cbc-105.1-mep-permits")
        ctx.note(f"Separate {', '.join(trades)} permit(s) are likely for this work. {u['note']}")

    # -- who may do the work (B&P 7048 / 7044)
    lt = ctx.P(MINOR_PERMIT, "aggregate_contract_price_lt_usd")
    dq = []
    if performer_advertises_or_has_employees:
        ctx.P(MINOR_DQ, "advertising_as_contractor_disqualifies")
        dq.append("the person advertises as a contractor or employs helpers")
    if part_of_larger_project:
        ctx.P(MINOR_SPLIT, "disqualifiers")
        dq.append("the work is part of a larger operation or split into smaller contracts")
    lic, lic_basis, minor = None, "", None
    if owner_builder:
        lic, lic_basis = _owner_builder(ctx, owner_builder_work_by, owner_intends_to_sell, required, dwelling, t,
                                        owner_lived_in_home_12_months_before_completion, val, lt, dq)
    elif required:
        ctx.P(MINOR_PERMIT, "permit_required_means_license_required")
        lic, minor = True, False
        lic_basis = ("B&P 7048: work that needs a building permit needs a licensed contractor at any price; the "
                     f"under-{_usd(lt)} minor work exemption does not apply.")
    elif val is None:
        lic_basis = (f"Give valuation_usd: no license is needed only if the whole job (labor, materials and everything "
                     f"else) is under {_usd(lt)} and needs no permit (B&P 7048).")
        ctx.use(MINOR_PERMIT)
    else:
        under = val < lt
        ctx.item("Minor work exemption price test (B&P 7048)", _usd(val),
                 f"{_usd(val)} {'<' if under else '>='} {_usd(lt)} (labor + materials + all other items, all contracts "
                 "on the project, materials counted even if the owner buys them)", MINOR_PERMIT)
        if under and not dq and det == "not_required" and trades:
            lic = minor = None
            lic_basis = (f"Under {_usd(lt)} and exempt from a building permit, but the {', '.join(trades)} part likely "
                         "needs its own permit; if it does, a licensed contractor is required at any price (B&P 7048). "
                         "Ask the building department before using an unlicensed person.")
        elif under and not dq:
            lic, minor = (False, True) if det == "not_required" else (None, None)
            lic_basis = (f"Under {_usd(lt)} and no permit needed: an unlicensed person may do it as minor work (B&P "
                         "7048), but only without employees, without advertising as a contractor, and not as part of "
                         "a larger job." if det == "not_required" else
                         f"Under {_usd(lt)}: no license is needed only if the building department confirms no permit "
                         "is required (B&P 7048).")
            if det == "not_required":
                ctx.note("An unlicensed person who advertises small jobs must state in the ad that they are not "
                         "licensed by the CSLB.", MINOR_ADS)
        else:
            lic, minor = True, False
            why = [f"the job is {_usd(val)}, not under {_usd(lt)}"] if not under else []
            lic_basis = "A CSLB license is required (B&P 7048): " + "; ".join(why + dq) + "."
    if required and not owner_builder:
        rep, mn = ctx.P(WILLFUL, "exception_repair_value_lt_usd"), ctx.P(DISCIPLINE, "minimum")
        ctx.note(f"Pull the permit before starting. CSLB presumes work done without a required permit is willful (B&P "
                 f"7090); the disciplinary guideline runs from a {mn['suspension_days']}-day stayed suspension with "
                 f"{mn['probation_years']} year of probation up to {ctx.P(DISCIPLINE, 'maximum')}. The only carve-out "
                 f"is equipment repair under {_usd(rep)} or an identical replacement part.", WILLFUL)
        ctx.use(DISCIPLINE)
    if t == "equipment_repair_or_identical_part":
        rep = ctx.P(WILLFUL, "exception_repair_value_lt_usd")
        if val is not None:
            ctx.item("B&P 7090 equipment repair carve-out", _usd(val),
                     f"{_usd(val)} {'<' if val < rep else '>='} {_usd(rep)} labor or materials -> CSLB "
                     f"{'will not' if val < rep else 'may'} discipline for skipping a permit", WILLFUL)
        ctx.note(f"B&P 7090 only spares the license from discipline for unpermitted equipment repair, maintenance or "
                 f"adjustment under {_usd(rep)} (or an identical replacement part); the city may still require a "
                 "permit.", WILLFUL)

    # -- smoke / CO alarms on dwelling permits over $1,000
    smoke = co = None
    if required and dwelling and (t == "addition" or t in ALTERATIONS):
        gt = ctx.P(ALARMS, "permit_valuation_gt_usd")
        devices = ctx.P(ALARMS, "devices")
        if val is None:
            ctx.note(f"Give valuation_usd: smoke and CO alarms must be brought up to code when the permit valuation is "
                     f"over {_usd(gt)}.")
        else:
            smoke = val > gt
            ctx.item("Smoke/CO alarm retrofit trigger", _usd(val),
                     f"{_usd(val)} {'>' if smoke else '<='} {_usd(gt)} permit valuation -> "
                     f"{'alarms required before final' if smoke else 'not triggered'}", ALARMS)
            if smoke:
                co = has_fuel_burning_appliance_or_attached_garage
                co_text = devices[1].split(" (")[0] if co else devices[1]
                ctx.step(f"Install/verify {devices[0]} throughout the existing dwelling"
                         + (f" and {co_text}" if co is not False else "")
                         + "; owner signs the alarm certificate", "Contractor and owner", "Before final inspection",
                         ALARMS)
                if co is None:
                    ctx.note("CO alarms are required if the dwelling has a fuel-burning appliance or an attached "
                             "garage; set has_fuel_burning_appliance_or_attached_garage.")
                ctx.note("Hard-wired, interconnected alarms are generally required only where wall or ceiling finishes "
                         "are removed; battery alarms usually satisfy the retrofit otherwise (local practice).")
            else:
                ctx.note(f"At or under {_usd(gt)} the alarm retrofit is not triggered by the valuation rule, but the CRC "
                         "R314.2.2 text has no dollar figure and some building departments ask for smoke/CO alarms on "
                         "any permitted alteration; check the inspection card.", ALARMS)

    # -- pre-1994 plumbing fixtures (Civil Code 1101.4 / 1101.5)
    fixtures = None
    if required and year_built is not None and t in {"addition", "alteration", "change_of_occupancy"}:
        cutoff = date.fromisoformat(ctx.P(FIXTURES, "building_built_on_or_before"))
        if year_built > cutoff.year:
            fixtures = False
        else:
            if year_built == cutoff.year:
                ctx.note(f"Built in {year_built}: the retrofit applies only if the building was built on or before "
                         f"{cutoff.isoformat()}.")
            if project_type == "single_family":
                fixtures = year_built < cutoff.year or None
                ctx.step(f"Replace {ctx.P(FIXTURES, 'single_family_scope')} with water-conserving fixtures",
                         "Plumbing contractor (C-36) or GC", ctx.P(FIXTURES, "timing"), FIXTURES)
            elif t == "addition":
                key = "civ-1101.5-d-fixture-triggers"
                u = ctx.U(key)
                lim = u["addition_floor_area_increase_gt_percent"]
                if sq_ft and existing_building_sq_ft:
                    over = sq_ft * 100 > lim * existing_building_sq_ft + 1e-9  # exact test; display is rounded
                    inc = _round2(sq_ft / existing_building_sq_ft * 100, up=True)
                    fixtures = (over and year_built < cutoff.year) or None
                    ctx.item("Pre-1994 fixture retrofit addition floor-area test", f"{inc:g}%",
                             f"{_g(sq_ft)} added / {_g(existing_building_sq_ft)} existing sq ft x 100 = {inc:g}% "
                             f"{'>' if over else '<='} {_g(lim)}% -> "
                             f"{'replace all noncompliant fixtures in the building' if over else 'building-wide trigger not met'}",
                             key)
                else:
                    ctx.note(f"Give sq_ft and existing_building_sq_ft: a multifamily/commercial addition triggers the "
                             f"building-wide fixture retrofit when it adds more than {_g(lim)}% to the floor area "
                             "(count all concurrent permits by the same applicant).")
                ctx.note(u["note"])
            else:
                trig = ctx.P(FIXTURES, "multifamily_commercial_valuation_trigger_usd")
                if val is not None:
                    over = val > trig
                    fixtures = (over and year_built < cutoff.year) or None
                    ctx.item("Pre-1994 fixture retrofit valuation trigger", _usd(val),
                             f"{_usd(val)} {'>' if over else '<='} {_usd(trig)} -> "
                             f"{'replace fixtures serving the altered area' if over else 'area trigger not met'}",
                             FIXTURES)
                ctx.note(f"Multifamily/commercial scope: {ctx.P(FIXTURES, 'multifamily_commercial_scope')}. Even under "
                         f"{_usd(trig)}, every noncompliant fixture in a room the permit alters must be replaced, so "
                         "plan for it in any altered restroom or kitchen.", FIXTURES)
        ctx.item(f"Built on or before {cutoff.isoformat()}?", str(year_built),
                 f"{year_built} vs {cutoff.isoformat()} -> {'yes' if year_built < cutoff.year else 'no' if year_built > cutoff.year else 'check'}",
                 FIXTURES)
    elif required and year_built is not None and t in ALTERATIONS:
        cutoff = date.fromisoformat(ctx.P(FIXTURES, "building_built_on_or_before"))
        if year_built <= cutoff.year:
            ctx.note("Pre-1994 building: some building departments also require the water-conserving fixture "
                     "retrofit on this kind of permit; ask plan check.", FIXTURES)

    # -- lead RRP (pre-1978 housing) and demolition / pool / other flags
    rrp = None
    if dwelling and year_built is not None and t in PAINT_DISTURBING:
        before = ctx.P(RRP, "built_before_year")
        rrp = year_built < before
        ctx.item("Lead RRP target housing (built before 1978)?", str(year_built),
                 f"{year_built} {'<' if rrp else '>='} {before} -> {'EPA RRP applies' if rrp else 'not target housing'}",
                 RRP)
        if rrp:
            ints, ext = ctx.P(RRP_MINOR, "interior_max_sqft_per_room"), ctx.P(RRP_MINOR, "exterior_max_sqft")
            ctx.step(f"Use an EPA-certified RRP firm with a certified renovator directing the work, give the Renovate "
                     f"Right pamphlet, and follow lead-safe practices (unless painted surfaces are shown lead-free or "
                     f"the work disturbs no more than {ints} sq ft per room inside / {ext} sq ft outside; window "
                     "replacement and demolition never qualify as minor)", "Contractor (EPA-certified renovator)",
                     "Before disturbing paint", RRP)
            ctx.use(RRP_MINOR)
            ctx.pro("EPA-certified renovator (lead RRP)")
            if owner_builder and owner_builder_work_by == "owner_or_own_employees":
                ctx.note("EPA RRP covers renovations performed for compensation, so an owner working on their own home "
                         "is outside it; anyone paid to do the work (including the owner's employees) should be "
                         "treated as covered. Use lead-safe practices either way.", RRP)
    send_by = None
    if t == "demolition":
        days = ctx.P(AIR_DISTRICT, "lead_time_working_days")
        count = (f"federal NESHAP working days: {ctx.P(NESHAP_DAYS, 'working_day_definition')}; the district's own "
                 "rule controls if stricter")
        when = f"At least {days} working days before demolition ({count})"
        if demo_start:
            # NESHAP working days include holidays, so only weekends are skipped. SCAQMD also states a calendar-day
            # figure; the earlier of the two dates satisfies both.
            cal = ctx.P(AIR_DISTRICT, "scaqmd_equivalent_calendar_days")
            by_wd = _add_business_days(demo_start, -days)
            by_cal = demo_start - timedelta(days=cal)
            send = min(by_wd, by_cal)
            send_by = send.isoformat()
            ctx.item("Asbestos/demolition notice: postmark or deliver by", send_by,
                     f"{demo_start.isoformat()} - {days} working days (Mon-Fri, holidays count) = {by_wd.isoformat()}; "
                     f"- {cal} calendar days (SCAQMD) = {by_cal.isoformat()} -> earlier = {send_by}", NESHAP_DAYS)
            ctx.use(AIR_DISTRICT)
            when = (f"Postmarked or delivered no later than {send_by} for a {demo_start.isoformat()} start; send it "
                    f"earlier ({count})")
            if send < today <= demo_start:
                ctx.note(f"The notice date {send_by} has passed: demolition cannot start on {demo_start.isoformat()}; "
                         f"send the notice now and move the start to {days} working days after it is delivered.",
                         NESHAP_DAYS)
        ctx.step("Get an asbestos survey and send the asbestos/demolition notification to the local air district",
                 "Owner or demolition contractor", when, AIR_DISTRICT)
        ctx.use(NESHAP_DAYS)
        asb = ctx.P(ASB_LIC, "threshold_sq_ft")
        ctx.step(f"If the survey finds {asb} sq ft or more of asbestos-containing material, the abatement must be done "
                 "by a contractor holding the C-22 Asbestos Abatement classification (or whose qualifier passed the "
                 "asbestos certification exam) and registered with Cal/OSHA for that work "
                 f"({ctx.P(ASB_DOSH, 'surface_area_sqft_gte')} sq ft or more)",
                 "Asbestos abatement contractor (C-22 or certified, Cal/OSHA-registered)", "Before abatement starts",
                 ASB_LIC)
        ctx.use(ASB_DOSH)
        ctx.pro(f"Asbestos abatement contractor (C-22, or a qualifier who passed the asbestos certification exam, "
                f"registered with Cal/OSHA) if the survey finds {asb} sq ft or more of asbestos-containing material")
        ctx.step(f"Give the building department a copy of the air district notification, or an {ctx.P(DEMO_NOTICE, 'alternative')}",
                 "Permit applicant", "Before the demolition permit is issued", DEMO_NOTICE)
        ctx.pro("Asbestos consultant for the pre-demolition survey (air districts commonly require a Cal/OSHA Certified "
                "Asbestos Consultant; confirm with the district)")
    if t == "pool_or_spa" and project_type == "single_family":
        ctx.step(f"Provide at least {ctx.P(POOL, 'min_features')} of the {ctx.P(POOL, 'feature_list_size')} "
                 "drowning-prevention safety features (Swimming Pool Safety Act)", "Pool contractor",
                 "Checked at final inspection", POOL)
    if t == "water_heater":
        u = ctx.U("hsc-19211-water-heater-strapping")
        ctx.step("Brace, anchor or strap the water heater for earthquake motion", "Plumbing contractor",
                 "Checked at final", "hsc-19211-water-heater-strapping")
        ctx.note(u["note"])
    if t == "hvac_changeout":
        ctx.note("HVAC changeouts are Energy Code alterations: expect CF1R/CF2R and HERS verification; run "
                 "energy_compliance_triggers for the list.", HVAC)

    # -- permit counter documents, permit life, contract
    fees, school, start_by = {}, None, None
    if required:
        ctx.step(f"Sign the license statement: license number and that it is in full force, or the basis for an "
                 f"exemption (penalty up to {_usd(ctx.P(LIC_STATEMENT, 'civil_penalty_max_usd'))})",
                 "Permit applicant (licensee or authorized agent; owner-builder states B&P 7044)", "At application",
                 LIC_STATEMENT)
        ctx.step("Sign the workers' compensation declaration under penalty of perjury: "
                 + "; or ".join(ctx.P(WC_DECL, "options")), "Permit applicant", "Before permit issuance", WC_DECL)
        months, ext = (ctx.P(EXPIRE, "state_statutory_commencement_window_months"),
                       ctx.P(EXPIRE, "extension_max_days_each"))
        gap = ctx.P(EXPIRE, "cbc_105_5_suspension_or_abandonment_days")
        ctx.note(f"Start work within {months} months of issuance or the permit expires (H&S 18938.6); extensions are "
                 f"in writing, up to {ext} days each, for justifiable cause. Once started, keep inspections moving: "
                 f"locally adopted CBC/CRC 105.5 suspends or expires a permit after {gap} days without work or an "
                 "approved inspection (check the local ordinance).", EXPIRE)
        if issued:
            start_by = _add_months(issued, months)
            ctx.item("Permit: start work by", start_by.isoformat(),
                     f"issued {issued.isoformat()} + {months} months = {start_by.isoformat()}", EXPIRE)
            ctx.step(f"Start the permitted work (get a first inspection on record) before {start_by.isoformat()}, or "
                     f"request a written extension (up to {ext} days) before then", "Contractor or owner-builder",
                     f"Before {start_by.isoformat()}", EXPIRE)
        hires = not owner_builder or owner_builder_work_by == "licensed_contractors"
        if hires and t == "new_building" and project_type == "single_family":
            ctx.step("Put the construction contract and every change in writing, signed by owner and contractor, with "
                     + ", ".join(ctx.P(SFD_CONTRACT, "required_fields")) + " (new home the owner keeps at least "
                     f"{ctx.P(SFD_CONTRACT, 'min_owner_retention_years')} year)", "Contractor", "Before work starts",
                     SFD_CONTRACT)
        elif hires and dwelling and t != "new_building":
            ctx.step("List each required permit in the home improvement contract and say who obtains and who pays for "
                     "it", "Contractor", "At contract signing", HIC_PERMITS)
        if val is not None and val > 0:
            fees = _state_fees(ctx, val, project_type, stories)
        if sq_ft and t in ("new_building", "addition"):
            school = _permit_school_fee(ctx, t, project_type, sq_ft, today)

    res = PermitRequiredResult(
        as_of=today.isoformat(), work_description=work_description, structure_type=t, project_type=project_type,
        code_section="CRC R105.2" if project_type == "single_family" else "CBC 105.2", code_edition=edition,
        determination=det, building_permit_required=required, basis=basis, separate_trade_permits_likely=trades,
        license_required=lic, license_basis=lic_basis, minor_work_exemption_available=minor,
        smoke_alarms_required=smoke, co_alarms_required=co, plumbing_fixture_retrofit_required=fixtures,
        lead_rrp_applies=rrp, owner_builder_warnings=ctx.warnings, state_fees_usd=fees,
        state_fees_total_usd=_money(sum(fees.values())) if fees else None, school_fee_estimate_usd=school,
        start_work_by=start_by.isoformat() if start_by else None, demolition_notice_send_by=send_by)
    return _finish(res, ctx)


def _owner_builder(ctx: _Ctx, work_by: str, sell: bool, required: Optional[bool], dwelling: bool, t: str,
                   lived_12: Optional[bool], val: Optional[float], lt: float,
                   dq: list[str]) -> tuple[Optional[bool], str]:
    def w(text: str, rule_id: str) -> None:
        ctx.use(rule_id)
        ctx.warnings.append(f"{text} [{rule_id}]")

    yrs, conclusive = ctx.P(OB_SALE, "window_after_completion_years"), ctx.P(OB_SALE,
                                                                             "conclusive_presumption_threshold_structures")
    w(f"Selling or offering the structure within {yrs} year after completion is presumed to mean it was built for sale "
      f"(rebuttable); {conclusive} or more structures in that window is conclusive.", OB_SALE)
    duties = ctx.P(OB_DUTIES, "responsibilities")
    w("Signing as owner-builder makes the owner the responsible party: " + "; ".join(duties) + ".", OB_DUTIES)
    # B&P 7044(a)(3) is a separate exemption for a homeowner improving their own principal residence; it is not a
    # condition of (a)(1) or (a)(2).
    a3 = dwelling and t != "new_building"
    months, mx, look = (ctx.P(HOMEOWNER, "residency_months_before_completion"), ctx.P(HOMEOWNER, "max_structures"),
                        ctx.P(HOMEOWNER, "lookback_years")) if a3 else (None, None, None)
    a3_terms = (f"B&P 7044(a)(3): a homeowner improving their principal residence is exempt even if they sell later, "
                f"if the work is done before the sale, they actually lived there for the {months} months before the "
                f"work is completed, and they have not used this exemption on more than {mx} structures more than "
                f"once in any {look}-year period.") if a3 else ""
    if a3:
        w(a3_terms, HOMEOWNER)
    w("Anyone who bids the job or schedules and oversees the trades for the owner is a contractor and must be licensed "
      "(construction consultant rule).", OB_CONSULT)
    if work_by == "owner_or_own_employees":
        ctx.use(OB_SELF)
        if sell:
            w("B&P 7044(a)(1) requires that none of the work is intended or offered for sale; with a planned sale the "
              "owner needs the (a)(3) homeowner exemption, a license, or licensed trades under (a)(2).", OB_SELF)
            if a3 and lived_12:
                return False, ("B&P 7044(a)(1) fails (work intended for sale), but the homeowner exemption applies: "
                               + a3_terms + " Confirm the structure count.")
            if a3 and lived_12 is None:
                return None, ("B&P 7044(a)(1) fails (work intended for sale). Exempt only under the homeowner rule; set "
                              "owner_lived_in_home_12_months_before_completion. " + a3_terms)
            return True, ("Owner-builder exemption fails: self-performed work intended for sale (B&P 7044(a)(1))"
                          + (f", and the owner has not lived in the home the {months} months the (a)(3) homeowner "
                             "exemption needs." if a3 else "."))
        w("If the owner uses employees, the owner is their employer: payroll, withholding and workers' compensation "
          "apply. Helpers paid by the job are not employees.", OB_SELF)
        return False, "Owner-builder exemption (B&P 7044(a)(1)): owner does the work personally or with wage employees, not for sale."
    if work_by == "licensed_contractors":
        cap = ctx.P(OB_TRADES, "sfr_max_for_sale_per_calendar_year")
        w(f"B&P 7044(a)(2): the owner must contract directly with a licensee for each trade; no more than {cap} "
          f"single-family homes may be built for sale in a calendar year unless {ctx.P(OB_TRADES, 'sfr_limit_waived_if')}.",
          OB_TRADES)
        return False, "Owner-builder exemption (B&P 7044(a)(2)): the owner hires licensed contractors for each trade; each of them must hold the right license."
    ctx.use(MINOR_PERMIT)
    if required is False and val is not None and val < lt and not dq:
        return False, (f"No permit and under {_usd(lt)}: the workers may do it as minor work (B&P 7048) if each works "
                       "alone, does not advertise as a contractor and it is not part of a larger job.")
    w("Paying unlicensed workers by the job is unlicensed contracting by them unless the job needs no permit and is "
      f"under {_usd(lt)} (B&P 7048); work that needs a permit needs a licensed contractor at any price, and the owner "
      "may become their employer for workers' compensation.", MINOR_PERMIT)
    if required is None:
        return None, (f"Depends on the permit: unlicensed workers may not do permitted work, or any job of {_usd(lt)} "
                      "or more (B&P 7048).")
    return True, "Unlicensed workers paid by the job may not do this work; hire licensed trades or employ them on payroll."


def _state_fees(ctx: _Ctx, val: float, ptype: str, stories: int) -> dict[str, float]:
    scope1 = ctx.P(SMIP, "category_1_scope")
    max_res_stories = int(_num(r"(\d+)\s*stories", scope1))
    # multifamily_high_rise means 4+ stories by definition (see ProjectType), so it is always Category 2.
    cat1 = ptype in ("single_family", "multifamily_low_rise") and stories <= max_res_stories
    if ptype == "multifamily_high_rise" and 1 < stories <= max_res_stories:
        ctx.note(f"project_type multifamily_high_rise but stories={stories}: SMIP uses Category 2 for high-rise; if the "
                 f"building is really {max_res_stories} stories or fewer, use multifamily_low_rise.", SMIP)
    rate = ctx.P(SMIP, "category_1_rate" if cat1 else "category_2_rate")
    smin = ctx.P(SMIP, "minimum_fee_usd")
    raw = _money(val * rate)
    smip = max(smin, raw)
    ctx.item("State SMIP (strong motion) fee", _usd(smip),
             f"Category {1 if cat1 else 2} ({scope1 if cat1 else ctx.P(SMIP, 'category_2_scope')}): {_usd(val)} x "
             f"{rate:g} = {_usd(raw)}; minimum {_usd(smin)} -> {_usd(smip)}", SMIP)
    per = ctx.P(BSASRF, "usd_per_25000_valuation_or_fraction")
    step = min(int(k.split("-")[1]) for k, v in ctx.P(BSASRF, "schedule_examples").items() if v == per)
    bmin = ctx.P(BSASRF, "minimum_fee_usd")
    units = math.ceil(val / step)
    bs = _money(max(bmin, units * per))
    ctx.item("State BSASRF (building standards) fee", _usd(bs),
             f"ceil({_usd(val)} / {_usd(step)}) = {units} x {_usd(per)} = {_usd(units * per)}; minimum {_usd(bmin)} -> "
             f"{_usd(bs)}", BSASRF)
    ctx.note("State SMIP and BSASRF fees are collected by every building department on top of its own plan check and "
             "permit fees (not estimated here; for San Diego use estimate_permit_fees). Local rounding varies by a few "
             "cents.")
    return {"smip_seismic": smip, "bsasrf_building_standards": bs}


def _permit_school_fee(ctx: _Ctx, t: str, ptype: str, sq_ft: float, on: date) -> Optional[float]:
    if ptype in RESIDENTIAL:
        if t == "addition":
            lim = ctx.P(SCHOOL_ADD, "addition_exempt_max_sqft")
            over = sq_ft > lim
            ctx.item("School fee: residential addition size test", f"{_g(sq_ft)} sq ft",
                     f"{_g(sq_ft)} {'>' if over else '<='} {lim} sq ft -> "
                     f"{'fee on the entire ' + _g(sq_ft) + ' sq ft' if over else 'no school fee'}", SCHOOL_ADD)
            if not over:
                return 0.0
        ctx.note("Enter living area only: " + ctx.U("gov-65995-b1-assessable-space")["note"])
        return _school_fee(ctx, sq_ft, "residential", on, label="School fee estimate (Level 1 maximum)")
    if t == "new_building":
        return _school_fee(ctx, sq_ft, "commercial", on, label="School fee estimate (Level 1 commercial maximum)")
    ctx.note("Nonresidential additions may owe the commercial/industrial school fee on new covered and enclosed space; "
             "ask the school district.", SCHOOL_RATE)
    return None


# ================================================================ energy_compliance_triggers
class EnergyDoc(BaseModel):
    form: str
    phase: str
    signed_by: str
    required: Literal["yes", "if_hers_measures", "usually"]
    registry_upload: str = ""
    rule_id: str
    verified: bool = False


class HersCheck(BaseModel):
    measure: str
    status: Literal["required", "likely", "possible", "not_triggered"]
    why: str
    rule_id: str
    verified: bool = False


class EnergyResult(_Result):
    project_type: str
    scope: str
    climate_zone: Optional[int] = None
    energy_code_edition: int
    permit_required: bool = True
    documents: list[EnergyDoc] = Field(default_factory=list)
    hers_verifications: list[HersCheck] = Field(default_factory=list)
    hers_rater_needed: Optional[bool] = None
    solar_pv_required: Optional[bool] = None
    pv_kwdc_after_battery_credit: Optional[float] = None
    battery_ready_required: Optional[bool] = None
    electric_ready_required: Optional[bool] = None
    duct_system_treated_as: Optional[Literal["entirely_new", "altered"]] = None


def _hers(ctx: _Ctx, out: list, measure: str, status: str, why: str, rule_id: str) -> None:
    ctx.use(rule_id)
    out.append(HersCheck(measure=measure, status=status, why=why, rule_id=rule_id, verified=core.rule(rule_id).verified))


def energy_compliance_triggers(project_type: ProjectType, scope: EnergyScope, climate_zone: Optional[int] = None,
                               ducts_added_or_replaced_ft: Optional[float] = None,
                               percent_of_duct_system_replaced: Optional[float] = None,
                               new_or_replacement_hvac_equipment: Optional[bool] = None,
                               electrical_service_amps: Optional[int] = None, battery_storage_installed: bool = False,
                               pv_kwdc_required: Optional[float] = None, gas_appliances_planned: bool = False,
                               homes_in_subdivision: Optional[int] = None, permit_application_date=None,
                               as_of=None, ductless_system: bool = False) -> EnergyResult:
    _check(ducts_added_or_replaced_ft=ducts_added_or_replaced_ft, pv_kwdc_required=pv_kwdc_required,
           electrical_service_amps=electrical_service_amps, percent_of_duct_system_replaced=percent_of_duct_system_replaced)
    if percent_of_duct_system_replaced is not None and percent_of_duct_system_replaced > 100:
        raise ValueError("percent_of_duct_system_replaced must be 0-100")
    if climate_zone is not None and not 1 <= climate_zone <= 16:
        raise ValueError(f"climate_zone must be a California building climate zone 1-16, got {climate_zone}")
    if homes_in_subdivision is not None and homes_in_subdivision < 1:
        raise ValueError("homes_in_subdivision must be at least 1")
    if ductless_system and (scope == "duct_alteration" or ducts_added_or_replaced_ft):
        raise ValueError("ductless_system=True means no ducts: do not combine it with duct_alteration or "
                         "ducts_added_or_replaced_ft")
    today = _d(as_of, "as_of") or logic.today_ca()
    app = _d(permit_application_date, "permit_application_date") or today
    ctx, hers, docs = _Ctx(), [], []
    sf, nonres = project_type == "single_family", project_type == "nonresidential"

    start = date.fromisoformat(ctx.P(ENERGY_DATE, "enforcement_start"))
    edition = ctx.P(EDITION, "edition") if app >= start else ctx.P(EDITION, "prior_edition")
    ctx.item("Energy Code edition", f"{edition} Title 24 Part 6",
             f"permit application {app.isoformat()} {'>=' if app >= start else '<'} {start.isoformat()}", ENERGY_DATE)
    ctx.note("Compliance documents must be the version for the code year of the permit application, made with "
             "CEC-approved compliance software for that edition.", ENERGY_DATE)

    if nonres:
        ctx.note("Nonresidential: HERS does not apply; acceptance testing (NRCA forms) is not in the rulebook yet. "
                 "Ask the energy consultant and plan check which acceptance tests apply.")
    if scope == "new_construction":
        if not nonres:
            for m in ctx.P(CF3R, "typical_measures"):
                _hers(ctx, hers, m, "likely", "Typical for new homes; the registered CF1R from the compliance software "
                                              "lists the exact measures.", CF3R)
    elif scope in ("hvac_changeout", "addition", "alteration", "duct_alteration") and not nonres:
        hvac_measures = ctx.P(HVAC, "hers_measures_possible")
        equip = True if scope == "hvac_changeout" else new_or_replacement_hvac_equipment
        if scope != "duct_alteration" or equip:
            for m in hvac_measures:
                if m == "duct leakage":
                    continue
                # The rule names refrigerant charge/airflow as the typical equipment tests; fan efficacy depends more
                # on the system type, so it stays "possible".
                st = ("likely" if m in ("refrigerant charge", "airflow") else "possible") if equip else \
                     "possible" if equip is None else "not_triggered"
                why = ("New or replacement space-conditioning equipment triggers HERS checks of these items depending "
                       "on equipment type and climate zone (2025 thresholds not in the rulebook).")
                if ductless_system and m in ("airflow", "fan efficacy"):
                    st, why = "not_triggered", "Airflow and fan efficacy are tests of ducted forced-air systems; this one is ductless."
                _hers(ctx, hers, m, st, why, HVAC)
        if ductless_system:
            _hers(ctx, hers, "duct leakage", "not_triggered", "Ductless system: there are no ducts to seal or test.", HVAC)
        elif ducts_added_or_replaced_ft is None and "duct leakage" in hvac_measures:
            if equip:
                st, why = "likely", ("Replacing heating/cooling equipment normally means sealing and HERS-testing the "
                                     "ducts connected to it, unless the small-duct exception applies (its 2025 length "
                                     "is not verified); give ducts_added_or_replaced_ft for new ducts.")
            else:
                st = "not_triggered" if equip is False and scope == "alteration" else "possible"
                why = ("Applies when ducts are new, extended or altered (an addition usually extends the existing "
                       "ducts); give ducts_added_or_replaced_ft.")
            _hers(ctx, hers, "duct leakage", st, why, HVAC)

    duct_as = None
    if ducts_added_or_replaced_ft is not None and not nonres and scope != "new_construction":
        dtext = ctx.P(HVAC, "duct_length_threshold_ft")
        form_ft, old_ft = _num(r">\s*(\d+)\s*ft", dtext), _num(r"used\s*(\d+)\s*ft", dtext)
        L = ducts_added_or_replaced_ft
        # Replacing equipment brings in the existing ducts connected to it, so a short duct extension cannot lower
        # the changeout's "likely" duct test.
        changeout = scope == "hvac_changeout" or new_or_replacement_hvac_equipment is True
        st = "likely" if L > form_ft or changeout else "possible"
        ctx.item("Duct length added/replaced vs small-duct threshold", f"{_g(L)} ft",
                 f"{_g(L)} {'>' if L > form_ft else '<='} {_g(form_ft)} ft (2025 CF1R-ALT question); "
                 f"{'>' if L > old_ft else '<='} {_g(old_ft)} ft (2019 form)"
                 + ("; equipment is replaced, so the existing ducts are tested too" if changeout and L <= form_ft else "")
                 + f" -> duct leakage test {st}", HVAC)
        _hers(ctx, hers, "duct leakage", st,
              f"{_g(L)} ft of new/replaced duct {'exceeds' if L > form_ft else 'is within'} the {_g(form_ft)} ft question "
              f"on the 2025 CF1R-ALT form (the 2019 form used {_g(old_ft)} ft); the 2025 threshold is not verified, so "
              "confirm on the CF1R." + (" New or replacement equipment normally means sealing and testing the existing "
                                        "ducts connected to it, whatever the new duct length." if changeout else ""), HVAC)
    if percent_of_duct_system_replaced is not None and not nonres:
        defn = ctx.P(HVAC, "altered_duct_system_definition")
        pct = _num(r"<\s*(\d+)\s*%", defn)
        duct_as = "altered" if percent_of_duct_system_replaced < pct else "entirely_new"
        ctx.item("Duct system classification", f"{_g(percent_of_duct_system_replaced)}% new/replaced",
                 f"{_g(percent_of_duct_system_replaced)}% {'<' if duct_as == 'altered' else '>='} {_g(pct)}% -> "
                 f"{duct_as.replace('_', ' ')} duct system ({defn}); entirely new duct systems meet the stricter "
                 "new-system leakage target", HVAC)
    if scope in ("hvac_changeout", "duct_alteration"):
        ctx.note("A permit is required for HVAC equipment replacement and new or altered ducts; do not compute "
                 "pass/fail leakage percentages without the 2025 Reference Appendices.", HVAC)

    if scope == "water_heater":
        u = ctx.U("hsc-19211-water-heater-strapping")
        ctx.note("The rulebook has no verified 2025 Energy Code requirement for a like-for-like water heater "
                 "replacement; whether a CF1R-ALT/CF2R is needed is set by the alteration rules (150.2(b)). Ask plan "
                 f"check. {u['note']}")
        ctx.step("Strap/brace the water heater for earthquake motion", "Plumbing contractor", "Checked at final",
                 "hsc-19211-water-heater-strapping")
    if scope in ("reroof", "window_replacement", "lighting_alteration"):
        ctx.note(f"{scope.replace('_', ' ').capitalize()} is an Energy Code alteration. The prescriptive values (cool "
                 "roof reflectance, window U-factor/SHGC by climate zone, lighting controls) are not in the rulebook; "
                 "take them from the 2025 compliance manual or software.")

    # -- documents
    needs_hers = any(h.status in ("required", "likely", "possible") for h in hers)
    likely_hers = any(h.status in ("required", "likely") for h in hers)
    alt = scope != "new_construction"
    if nonres:
        docs.append(EnergyDoc(form=ctx.P(CF1R, "nonresidential_form"), phase="plan check / permit application",
                              signed_by="Responsible designer / documentation author", required="yes",
                              rule_id=CF1R, verified=core.rule(CF1R).verified))
        docs.append(EnergyDoc(form=ctx.P(CF2R, "nonresidential_form"), phase="construction",
                              signed_by=ctx.P(CF2R, "signed_by"), required="yes", registry_upload="",
                              rule_id=CF2R, verified=core.rule(CF2R).verified))
    else:
        f1 = ctx.P(CF1R, "residential_form") + ("-ALT" if alt and scope != "addition" else "")
        docs.append(EnergyDoc(form=f1, phase=ctx.P(CF1R, "phase"),
                              signed_by="Responsible designer / documentation author (compliance software output)",
                              required="yes" if scope in ("new_construction", "addition", "hvac_changeout",
                                                          "duct_alteration", "alteration") else "usually",
                              registry_upload=(f"Registered with the {ctx.P(CF1R, 'registry')} when HERS verification "
                                               "is required" if needs_hers or not alt else ""),
                              rule_id=CF1R, verified=core.rule(CF1R).verified))
        docs.append(EnergyDoc(form=ctx.P(CF2R, "residential_form"), phase="installation (kept on site for the inspector)",
                              signed_by=ctx.P(CF2R, "signed_by"), required="yes" if docs[0].required == "yes" else "usually",
                              registry_upload="Submitted to the same data registry when HERS verification is required",
                              rule_id=CF2R, verified=core.rule(CF2R).verified))
        if needs_hers:
            docs.append(EnergyDoc(form=ctx.P(CF3R, "form"), phase=ctx.P(CF3R, "gate"),
                                  signed_by=ctx.P(CF3R, "prepared_by"), required="if_hers_measures",
                                  registry_upload=ctx.P(CF3R, "registry"), rule_id=CF3R,
                                  verified=core.rule(CF3R).verified))
            ctx.pro("Certified HERS (ECC) rater, independent of the installer, for the CF3R field verification")
            ctx.step("Schedule the HERS rater after the installer registers the CF2R; the permit is not finaled until "
                     "the registered CF3R is in", "Contractor", "Before final inspection", CF3R)
        if not sf:
            ctx.note("Multifamily: the 2025 Energy Code has its own multifamily sections and forms; the rulebook's "
                     "CF1R/CF2R/CF3R rules describe single-family/low-rise residential. Confirm form names with the "
                     "energy consultant.")
    ctx.step("Installer completes and signs the certificate of installation for each energy feature installed",
             "Installing contractor", "During construction, before inspection", CF2R)

    # -- new single-family baseline
    pv_req = pv_after = bess = elec = None
    if scope == "new_construction" and sf:
        pv_req = True
        cut = ctx.P(PV, "pv_size_reduction_with_battery_percent")
        ctx.item("Solar PV", "required", f"Size per Equation {ctx.P(PV, 'sizing_equation')}; "
                                         f"{ctx.P(PV, 'exceptions_count')} exceptions apply", PV)
        if pv_kwdc_required is not None and battery_storage_installed:
            pv_after = round(pv_kwdc_required * (1 - cut / 100) + 1e-9, 3)
            ctx.item("PV size with battery credit", f"{pv_after:g} kWdc",
                     f"{_g(pv_kwdc_required)} kWdc x (1 - {cut:g}%) = {pv_after:g} kWdc", PV)
            ctx.note("The battery credit counts only if the battery meets Joint Appendix JA12 (CEC: minimum 7.5 kWh "
                     "cycling capacity); confirm the battery model with the energy consultant.", PV)
        gt = ctx.P(BESS, "bess_ready_service_amps_gt")
        if battery_storage_installed and ctx.P(BESS, "bess_ready_waived_if_bess_installed"):
            bess = False
            ctx.item("Battery-ready", "waived", "a battery is installed", BESS)
        elif electrical_service_amps is not None:
            bess = electrical_service_amps > gt
            ctx.item("Battery-storage-ready (service size)", f"{electrical_service_amps} A",
                     f"{electrical_service_amps} {'>' if bess else '<='} {gt} A -> "
                     f"{'BESS-ready required' if bess else 'not required'}", BESS)
        else:
            ctx.note(f"Give electrical_service_amps: homes with service over {gt} A must be battery-ready unless a "
                     "battery is installed.", BESS)
        if homes_in_subdivision is not None:
            mn = ctx.P(BESS, "solar_ready_subdivision_min_units")
            ctx.item("Solar-ready (subdivision size)", f"{homes_in_subdivision} homes",
                     f"{homes_in_subdivision} {'>=' if homes_in_subdivision >= mn else '<'} {mn} -> "
                     f"{'solar-ready rules apply' if homes_in_subdivision >= mn else 'not triggered'} (the {mn}-unit "
                     "figure was not separately checked)", BESS)
        base = ctx.P(HEAT_PUMP, "baseline_space_heating")
        ctx.item("Prescriptive baseline", f"{base} space heating and {ctx.P(HEAT_PUMP, 'baseline_water_heating')} "
                                          "water heating", f"gas allowed via {ctx.P(HEAT_PUMP, 'gas_allowed_via')}",
                 HEAT_PUMP)
        elec = bool(gas_appliances_planned)
        if elec:
            ctx.note("Gas appliances planned: the design must make up the difference through the performance path, "
                     "and electric-ready provisions (circuits, panel space) are triggered at those appliances.",
                     HEAT_PUMP)
        ctx.note("New detached ADUs are newly constructed buildings: check the PV exceptions with the energy "
                 "consultant.", PV)
    if scope == "new_construction" and not sf:
        ctx.note("The rulebook holds the solar PV rule for new single-family homes only. The Energy Code also requires "
                 "PV (and for some buildings battery storage) on many new multifamily and nonresidential buildings; "
                 "the energy consultant's compliance run sets the size.")
    if scope == "addition" and sf:
        ctx.note("Single-family additions: gas tankless water heaters are no longer a prescriptive option; a heat pump "
                 "water heater or the performance path is needed (2025 150.2(a)).", HEAT_PUMP)
    if climate_zone is not None:
        ctx.note(f"Climate zone {climate_zone}: climate-zone-specific prescriptive values are not in the rulebook; the "
                 "compliance software applies them.")

    res = EnergyResult(as_of=today.isoformat(), project_type=project_type, scope=scope, climate_zone=climate_zone,
                       energy_code_edition=edition, documents=docs, hers_verifications=hers,
                       # None = depends on the registered compliance run; the tool never gives a firm "no rater".
                       hers_rater_needed=True if likely_hers else None,
                       solar_pv_required=pv_req,
                       pv_kwdc_after_battery_credit=pv_after, battery_ready_required=bess,
                       electric_ready_required=elec, duct_system_treated_as=duct_as)
    return _finish(res, ctx)


# ================================================================ calgreen_and_stormwater
class CalgreenResult(_Result):
    project_type: str
    scope: str
    calgreen_mandatory_measures_apply: Optional[bool] = None
    waste_diversion_required: Optional[bool] = None
    diversion_min_percent: Optional[float] = None
    diversion_achieved_percent: Optional[float] = None
    diversion_meets_minimum: Optional[bool] = None
    diversion_shortfall: Optional[float] = None
    waste_requirement_met: Optional[bool] = None
    low_disposal_max_lbs_per_sqft: Optional[float] = None
    low_disposal_actual_lbs_per_sqft: Optional[float] = None
    low_disposal_meets_cap: Optional[bool] = None
    ev_capable_required: Optional[bool] = None
    construction_general_permit_required: Optional[bool] = None
    cgp_basis: str = ""
    erosivity_waiver_possible: Optional[bool] = None


def calgreen_and_stormwater(project_type: ProjectType, scope: GreenScope = "new_construction",
                            valuation_usd: Optional[float] = None, sq_ft: Optional[float] = None,
                            disturbed_acres: Optional[float] = None, part_of_larger_common_plan: bool = False,
                            increases_conditioned_area_volume_or_size: Optional[bool] = None,
                            has_attached_private_garage: Optional[bool] = None, ev_40a_circuit_installed: bool = False,
                            cd_waste_generated_tons: Optional[float] = None,
                            cd_waste_diverted_tons: Optional[float] = None,
                            landfill_disposal_lbs: Optional[float] = None,
                            rainfall_erosivity_r_factor: Optional[float] = None, as_of=None) -> CalgreenResult:
    _check(valuation_usd=valuation_usd, sq_ft=sq_ft, disturbed_acres=disturbed_acres,
           cd_waste_generated_tons=cd_waste_generated_tons, cd_waste_diverted_tons=cd_waste_diverted_tons,
           landfill_disposal_lbs=landfill_disposal_lbs, rainfall_erosivity_r_factor=rainfall_erosivity_r_factor)
    if cd_waste_generated_tons is not None and cd_waste_diverted_tons is not None \
            and cd_waste_diverted_tons > cd_waste_generated_tons:
        raise ValueError("cd_waste_diverted_tons cannot exceed cd_waste_generated_tons")
    today = _d(as_of, "as_of") or logic.today_ca()
    ctx = _Ctx()
    res_bldg = project_type in RESIDENTIAL
    applies = waste = None

    # -- does CALGreen apply
    if scope == "new_construction":
        applies = waste = True
        ctx.use(CG_WASTE)
    elif res_bldg:
        u = ctx.U("calgreen-301.1.1-residential-additions")
        applies = waste = increases_conditioned_area_volume_or_size
        ctx.item("Residential addition/alteration scope", {True: "applies", False: "does not apply", None: "check"}[applies],
                 u["note"], "calgreen-301.1.1-residential-additions")
        if applies is None:
            ctx.note("Set increases_conditioned_area_volume_or_size: residential additions/alterations are covered "
                     "only when they increase conditioned area, volume or size.")
    else:
        waste = True
        ctx.P(CG_NONRES, "waste_diversion_5_408_applies_to")
        if scope == "addition":
            mn = ctx.P(CG_NONRES, "addition_min_sqft")
            if sq_ft is not None:
                applies = sq_ft >= mn
                ctx.item("Nonresidential addition size vs CALGreen trigger", f"{_g(sq_ft)} sq ft",
                         f"{_g(sq_ft)} {'>=' if applies else '<'} {_g(mn)} sq ft -> mandatory measures "
                         f"{'apply' if applies else 'do not apply'}", CG_NONRES)
            else:
                ctx.note(f"Give sq_ft: nonresidential additions of {_g(mn)} sq ft or more get the full mandatory "
                         "measures.", CG_NONRES)
        else:
            mn = ctx.P(CG_NONRES, "alteration_min_permit_valuation_usd")
            if valuation_usd is not None:
                applies = valuation_usd >= mn
                ctx.item("Nonresidential alteration valuation vs CALGreen trigger", _usd(valuation_usd),
                         f"{_usd(valuation_usd)} {'>=' if applies else '<'} {_usd(mn)} permit valuation -> mandatory "
                         f"measures {'apply' if applies else 'do not apply'}", CG_NONRES)
            else:
                ctx.note(f"Give valuation_usd: nonresidential alterations with a permit valuation of {_usd(mn)} or more "
                         "get the full mandatory measures.", CG_NONRES)
        ctx.note(f"Nonresidential: the {_g(ctx.P(CG_WASTE, 'min_diversion_percent'))}% construction waste diversion "
                 "applies to every addition or alteration that needs a permit, even below the size/valuation triggers.",
                 CG_NONRES)

    # -- waste diversion and low-disposal alternative
    need = gen_pct = ok = short = None
    if waste:
        need = float(ctx.P(CG_WASTE, "min_diversion_percent"))
        ctx.step(f"Recycle/salvage at least {need:g}% of nonhazardous C&D waste (or meet a stricter local ordinance) "
                 "via: " + "; ".join(ctx.P(CG_WASTE, "compliance_methods")),
                 "General contractor", "Plan before permit (many cities require it); "
                 + ctx.P(CG_WASTE, "documentation") + " before final", CG_WASTE)
        if cd_waste_generated_tons and cd_waste_diverted_tons is not None:
            div = cd_waste_diverted_tons
            ok = div * 100 + 1e-9 >= need * cd_waste_generated_tons       # exact test; display is rounded
            gen_pct = _round2(div / cd_waste_generated_tons * 100, up=False)
            target = cd_waste_generated_tons * need / 100
            short = 0.0 if ok else _round2(target - div, up=True)
            ctx.item("C&D waste diversion rate", f"{gen_pct:g}%",
                     f"{_g(div)} diverted / {_g(cd_waste_generated_tons)} generated x 100 = {gen_pct:g}% "
                     f"{'>=' if ok else '<'} {need:g}%" + ("" if ok else f"; divert {_g(short)} more (target "
                                                                          f"{_g(cd_waste_generated_tons)} x {need:g}% = "
                                                                          f"{_g(round(target, 2))})"), CG_WASTE)
        elif cd_waste_generated_tons or cd_waste_diverted_tons is not None:
            ctx.note("Give both cd_waste_generated_tons (more than 0) and cd_waste_diverted_tons to compute the diversion "
                     "rate; a missing figure is not treated as zero.", CG_WASTE)
    low = low_act = low_ok = None
    if waste:
        key = ("low_rise_residential_max_disposal_lbs_per_sqft" if project_type in ("single_family", "multifamily_low_rise")
               else "high_rise_residential_max_disposal_lbs_per_sqft" if project_type == "multifamily_high_rise"
               else "nonresidential_max_disposal_lbs_per_sqft")
        low = float(ctx.P(CG_LOW, key))
        if landfill_disposal_lbs is not None and sq_ft:
            low_ok = landfill_disposal_lbs <= low * sq_ft + 1e-9          # exact test; display is rounded
            low_act = _round2(landfill_disposal_lbs / sq_ft, up=True)
            ctx.item("Low-disposal alternative", f"{low_act:g} lbs/sq ft",
                     f"{_g(landfill_disposal_lbs)} lbs landfilled / {_g(sq_ft)} sq ft = {low_act:g} lbs/sq ft "
                     f"{'<=' if low_ok else '>'} {low:g} cap", CG_LOW)
        else:
            ctx.note(f"Alternative to {need:g}% diversion: landfill no more than {low:g} lbs of C&D waste per sq ft of "
                     "building area (give landfill_disposal_lbs and sq_ft to check).", CG_LOW)

    # Either path satisfies the requirement, so it fails only when both were checked and both fail.
    met = True if (ok or low_ok) else (False if (ok is False and low_ok is False) else None)
    if ok is False and low_ok:
        ctx.note("Diversion is under the minimum, but the project meets the low-disposal alternative, which satisfies "
                 "the waste requirement on its own.", CG_LOW)
    elif met is False:
        ctx.note(f"Not compliant on either path: divert at least {need:g}% or get landfilled waste down to {low:g} "
                 "lbs/sq ft before final.", CG_WASTE)
    elif ok is False or low_ok is False:
        ctx.note(f"The path checked falls short; the other path ({need:g}% diversion or the {low:g} lbs/sq ft disposal "
                 "cap) was not checked, so compliance is undetermined. Give the missing inputs or divert more before "
                 "final.", CG_WASTE)

    # -- EV capable
    ev = None
    if scope == "new_construction" and project_type == "single_family":
        if has_attached_private_garage is None:
            ctx.note("Set has_attached_private_garage: EV-capable rules apply to new one- and two-family homes and "
                     "townhouses with attached private garages.", CG_EV)
        else:
            ev = has_attached_private_garage
        if ev:
            amps, volts, label = ctx.P(CG_EV, "circuit_amps"), ctx.P(CG_EV, "circuit_voltage"), ctx.P(CG_EV, "label")
            ctx.step(f"Provide a listed raceway for a dedicated {volts} branch circuit plus panel capacity and a reserved "
                     f"breaker space for a {amps} A {volts} circuit, labeled \"{label}\""
                     + (f" (raceway waived: a {amps} A circuit is installed)" if ev_40a_circuit_installed else ""),
                     "Electrical contractor (C-10)", "Rough electrical inspection", CG_EV)
            ctx.note("Many cities' reach codes require EV-ready (an installed circuit) instead of EV-capable.", CG_EV)
    elif scope == "new_construction":
        ctx.note("Multifamily and nonresidential EV charging space percentages are not in the rulebook yet; check the "
                 "2025 CALGreen tables and local reach codes.")

    # -- Construction General Permit
    cgp, basis, waiver = None, "", None
    small_ac = ctx.P(SW_DEF, "small_construction_min_acres")
    if disturbed_acres is None and part_of_larger_common_plan:
        cgp = True
        basis = (f"Part of a larger common plan of development disturbing {_g(small_ac)}+ acre: construction stormwater "
                 "coverage applies to this lot whatever its own disturbed area.")
        ctx.note("Give disturbed_acres for the erosivity waiver test and the SMARTS/SWPPP/QSP steps.", SW_DEF)
    elif disturbed_acres is None and scope != "alteration":
        ctx.note(f"Give disturbed_acres (clearing, grading, excavating, stockpiles) to check the Construction General "
                 f"Permit: it applies at {_g(small_ac)} acre or more, or on any lot in a larger common plan of that "
                 "size.", SW_DEF)
    if disturbed_acres is not None:
        small = ctx.P(SW_DEF, "small_construction_min_acres")
        large = ctx.P(SW_DEF, "large_construction_min_acres")
        hit = disturbed_acres >= small or part_of_larger_common_plan
        ctx.item("Land disturbance vs Construction General Permit threshold", f"{_g(disturbed_acres)} acres",
                 f"{_g(disturbed_acres)} {'>=' if disturbed_acres >= small else '<'} {_g(small)} acre"
                 + (f"; part of a larger common plan disturbing {_g(small)}+ acre" if part_of_larger_common_plan else "")
                 + f" -> {'CGP coverage required' if hit else 'CGP not required'}", SW_DEF)
        if hit:
            cgp = True
            basis = (f"Disturbs {_g(disturbed_acres)} acres" + (" as part of a larger common plan of development"
                                                                 if part_of_larger_common_plan else "")
                     + f": construction stormwater coverage applies at {_g(small)} acre or more "
                       f"({'large construction, ' + _g(large) + '+ acres' if disturbed_acres >= large else 'small construction'}).")
            wmax, rmax = ctx.P(SW_WAIVER, "max_disturbed_acres_lt"), ctx.P(SW_WAIVER, "r_factor_lt")
            if part_of_larger_common_plan and disturbed_acres < wmax:
                ctx.note(f"Part of a larger common plan: the waiver's under-{_g(wmax)}-acre test applies to the whole "
                         f"plan's disturbance; if the common plan disturbs {_g(wmax)} acres or more it is large "
                         "construction and no erosivity waiver is available.", SW_DEF)
            if disturbed_acres < wmax and rainfall_erosivity_r_factor is not None:
                waiver = rainfall_erosivity_r_factor < rmax
                ctx.item("Erosivity waiver test", f"R = {_g(rainfall_erosivity_r_factor)}",
                         f"{_g(disturbed_acres)} < {_g(wmax)} acres and R {_g(rainfall_erosivity_r_factor)} "
                         f"{'<' if waiver else '>='} {_g(rmax)} -> waiver {'possible' if waiver else 'not available'}",
                         SW_WAIVER)
            elif disturbed_acres >= wmax:
                waiver = False
                ctx.item("Erosivity waiver test", f"{_g(disturbed_acres)} acres",
                         f"{_g(disturbed_acres)} >= {_g(wmax)} acres -> no erosivity waiver", SW_WAIVER)
            else:
                ctx.note(f"Sites under {_g(wmax)} acres with a construction-period rainfall erosivity R factor under "
                         f"{_g(rmax)} may file an erosivity waiver instead ({ctx.P(SW_WAIVER, 'calculation')}); give "
                         "rainfall_erosivity_r_factor to check.",
                         SW_WAIVER)
            if waiver:
                ctx.note("With a waiver, file it in SMARTS before work; if the schedule slips past the waiver period, "
                         "full CGP coverage is needed.", SW_WAIVER)
            else:
                order = ctx.P(CGP_NOI, "permit_order")
                ctx.step(f"File the Notice of Intent and Permit Registration Documents ({', '.join(ctx.P(CGP_NOI, 'documents'))}) "
                         f"in {ctx.P(CGP_NOI, 'system')} under Order {order} and get the {ctx.P(CGP_NOI, 'identifier_issued')}",
                         ctx.P(CGP_NOI, "filer"), ctx.P(CGP_NOI, "timing"), CGP_NOI)
                ctx.step("Prepare and certify the site-specific SWPPP (" + ", ".join(ctx.P(CGP_QSD, "contents")) + ")",
                         ctx.P(CGP_QSD, "preparer"), "Submitted with the PRDs, kept current on site", CGP_QSD)
                ctx.step(f"Implement the SWPPP, run {ctx.P(CGP_QSP, 'inspection_frequency')} inspections and sampling",
                         ctx.P(CGP_QSP, "role"), "Throughout construction", CGP_QSP)
                q = lambda k: ctx.P(CGP_QPE, k)  # noqa: E731
                ctx.step(f"Inspect before, during and after each Qualifying Precipitation Event ({q('forecast_source')} "
                         f"forecast of {q('qpe_min_probability_percent')}%+ chance of {q('qpe_trigger_inches_24h')} in+ in "
                         f"24 h): pre-QPE within {q('pre_qpe_inspection_within_hours')} h before (up to "
                         f"{q('pre_qpe_inspection_max_hours_if_extended_forecast')} h with extended forecasts), post-QPE "
                         f"within {q('post_qpe_inspection_within_hours')} h if {q('post_qpe_inspection_required_if_measured_inches_gte')} "
                         "in+ is measured on site", "Qualified SWPPP Practitioner (QSP)", "Each forecast storm", CGP_QPE)
                ctx.step("File the Notice of Termination with " + ", ".join(ctx.P(CGP_NOT, "attachments"))
                         + f"; annual fees continue until: {ctx.P(CGP_NOT, 'fees_continue_until')}",
                         "Owner (Legally Responsible Person) with the QSP", "At final stabilization", CGP_NOT)
                ctx.pro("Qualified SWPPP Developer (QSD) to write and certify the SWPPP")
                ctx.pro("Qualified SWPPP Practitioner (QSP) to implement, inspect and sample (the GC usually provides)")
        else:
            cgp = False
            basis = f"Disturbs {_g(disturbed_acres)} acres, under {_g(small)} acre and not part of a larger common plan."
            ctx.note(f"Under {_g(small)} acre the state CGP does not apply, but the city's stormwater (MS4) ordinance still "
                     "requires erosion and sediment BMPs on every site; not in the rulebook, ask the city.")
    res = CalgreenResult(as_of=today.isoformat(), project_type=project_type, scope=scope,
                         calgreen_mandatory_measures_apply=applies, waste_diversion_required=waste,
                         diversion_min_percent=need, diversion_achieved_percent=gen_pct, diversion_meets_minimum=ok,
                         diversion_shortfall=short, waste_requirement_met=met, low_disposal_max_lbs_per_sqft=low,
                         low_disposal_actual_lbs_per_sqft=low_act, low_disposal_meets_cap=low_ok, ev_capable_required=ev,
                         construction_general_permit_required=cgp, cgp_basis=basis, erosivity_waiver_possible=waiver)
    return _finish(res, ctx)


# ================================================================ adu_rules
class AduResult(_Result):
    lot_type: str
    adu_type: str
    sq_ft: float
    size_status: str
    city_max_size_cannot_be_below_sqft: Optional[float] = None
    guaranteed_sqft: Optional[float] = None
    state_default_max_sqft: Optional[float] = None
    min_height_city_must_allow_ft: Optional[float] = None
    max_side_rear_setback_city_may_require_ft: Optional[float] = None
    completeness_deadline: Optional[str] = None
    approval_deadline: Optional[str] = None
    approval_deadline_basis: str = ""
    approval_clock_may_be_tolled: bool = False
    impact_fee_exempt: bool = False
    impact_fee_estimate_usd: Optional[float] = None
    school_fee_exempt: bool = False
    school_fee_estimate_usd: Optional[float] = None
    owner_occupancy_required: Optional[bool] = None
    max_down_payment_usd: Optional[float] = None
    cancellation_business_days: Optional[int] = None


def adu_rules(adu_type: AduType, sq_ft: float, lot_type: LotType = "single_family", bedrooms: int = 1,
              primary_dwelling_sq_ft: Optional[float] = None, proposed_height_ft: Optional[float] = None,
              near_major_transit: bool = False, lot_has_multistory_multifamily: bool = False,
              primary_zone_height_limit_ft: Optional[float] = None,
              proposed_side_rear_setback_ft: Optional[float] = None, jadu_shares_bathroom: Optional[bool] = None,
              application_submitted_date=None, application_complete_date=None,
              submitted_with_new_primary_dwelling: bool = False,
              impact_fee_for_primary_dwelling_usd: Optional[float] = None,
              school_district_rate_usd_per_sqft: Optional[float] = None, contract_price_usd: Optional[float] = None,
              buyer_65_or_older: bool = False, is_manufactured_home: bool = False, as_of=None,
              agency_closed_dates: Optional[list] = None) -> AduResult:
    _check(sq_ft=sq_ft, primary_dwelling_sq_ft=primary_dwelling_sq_ft, proposed_height_ft=proposed_height_ft,
           primary_zone_height_limit_ft=primary_zone_height_limit_ft,
           proposed_side_rear_setback_ft=proposed_side_rear_setback_ft,
           impact_fee_for_primary_dwelling_usd=impact_fee_for_primary_dwelling_usd,
           school_district_rate_usd_per_sqft=school_district_rate_usd_per_sqft, contract_price_usd=contract_price_usd)
    if sq_ft <= 0:
        raise ValueError("sq_ft must be greater than 0")
    if bedrooms < 0:
        raise ValueError("bedrooms cannot be negative")
    today = _d(as_of, "as_of") or logic.today_ca()
    sub = _d(application_submitted_date, "application_submitted_date")
    comp = _d(application_complete_date, "application_complete_date")
    if sub and comp and comp < sub:
        raise ValueError(f"application_complete_date {comp} is before application_submitted_date {sub}")
    closed = frozenset(filter(None, (_d(x, "agency_closed_dates") for x in agency_closed_dates or [])))
    ctx = _Ctx()
    ctx.note(f"ADU square footage is measured as {ctx.P(ADU_MIN, 'measurement_basis_from_2026')}.", ADU_MIN)
    if lot_type == "multifamily":
        ctx.note("Multifamily lots: the number of detached and converted ADUs allowed is not in the rulebook; ask "
                 "planning.")
        if adu_type == "jadu":
            ctx.note("A JADU must be inside a single-family home, so it is not available on a multifamily lot; use "
                     "conversion (existing non-livable space) or detached instead. "
                     + ctx.U("gov-66313-jadu-definition")["note"])

    # -- size
    floor = guaranteed = default = None
    if adu_type == "jadu":
        mx = ctx.P(JADU, "jadu_max_sqft")
        status = "within_jadu_max" if sq_ft <= mx else "exceeds_jadu_max"
        ctx.item("JADU size", f"{_g(sq_ft)} sq ft", f"{_g(sq_ft)} {'<=' if sq_ft <= mx else '>'} {mx} sq ft JADU "
                                                     "maximum", JADU)
        default = float(mx)
        if sq_ft > mx:
            ctx.note(f"Over {mx} sq ft this cannot be a JADU; design it as an ADU instead.", JADU)
    elif adu_type == "conversion":
        status = "conversion_no_state_size_cap"
        ctx.item("Conversion size cap", "none", f"conversion_size_cap = {ctx.P(ADU_CAP, 'conversion_size_cap')} "
                                                "(converting existing space has no fixed state cap)", ADU_CAP)
    else:
        guaranteed = float(ctx.P(ADU_MIN, "guaranteed_adu_sqft"))
        floor = float(ctx.P(ADU_MIN, "min_max_size_sqft_studio_or_1br" if bedrooms <= 1
                            else "min_max_size_sqft_2br_plus"))
        if adu_type == "detached":
            default = float(ctx.P(ADU_CAP, "detached_max_sqft"))
            dmath = f"detached default {_g(default)} sq ft"
        elif primary_dwelling_sq_ft:
            pct = ctx.P(ADU_CAP, "attached_max_percent_of_primary")
            half = primary_dwelling_sq_ft * pct / 100
            default = max(floor, half)
            dmath = (f"attached: {_g(primary_dwelling_sq_ft)} x {pct}% = {_g(half)} sq ft, not below the {_g(floor)} "
                     f"sq ft floor -> {_g(default)} sq ft")
        else:
            dmath = f"attached: give primary_dwelling_sq_ft for the {ctx.P(ADU_CAP, 'attached_max_percent_of_primary')}% cap"
        if sq_ft <= guaranteed:
            status = "protected_800_sqft"
        elif sq_ft <= floor:
            status = "protected_size_floor"
        elif default is not None and sq_ft <= default:
            status = "within_state_default_max"
        elif default is not None:
            status = "exceeds_state_default_max"
        else:
            status = "depends_on_local_ordinance"
        ctx.item("ADU size vs state floors and caps", f"{_g(sq_ft)} sq ft, {bedrooms} bedroom(s)",
                 f"guaranteed {_g(guaranteed)} sq ft (with {_g(ctx.P(ADU_MIN, 'max_side_rear_setback_ft'))}-ft setbacks, "
                 f"despite FAR/lot coverage); city max may not "
                 f"be below {_g(floor)} sq ft ({'studio/1 BR' if bedrooms <= 1 else '2+ BR'}); {dmath} -> {status}",
                 ADU_MIN)
        if status == "exceeds_state_default_max":
            ctx.note("Larger than the state default: allowed only if the local ADU ordinance permits it.", ADU_CAP)

    # -- height and setbacks
    min_h = setback = None
    if adu_type == "detached":
        min_h = float(ctx.P(ADU_HT, "min_allowed_height_ft"))
        why = "base"
        if near_major_transit:
            min_h, why = float(ctx.P(ADU_HT, "detached_near_transit_height_ft")), ctx.P(ADU_HT, "detached_near_transit_condition")
        elif lot_has_multistory_multifamily:
            min_h, why = float(ctx.P(ADU_HT, "detached_on_multifamily_multistory_lot_height_ft")), "multistory multifamily lot"
        ctx.item("Height the city must allow (detached)", f"{_g(min_h)} ft", why, ADU_HT)
    elif adu_type == "attached":
        rule_txt = ctx.P(ADU_HT, "attached_height_ft")
        base_h = _num(r"(\d+)\s*ft", rule_txt)
        min_h = base_h if primary_zone_height_limit_ft is None else min(base_h, primary_zone_height_limit_ft)
        ctx.item("Height the city must allow (attached)", f"{_g(min_h)} ft",
                 rule_txt + (f": min({_g(base_h)}, {_g(primary_zone_height_limit_ft)})"
                             if primary_zone_height_limit_ft is not None else ""), ADU_HT)
    if min_h is not None and proposed_height_ft is not None:
        ctx.item("Proposed height", f"{_g(proposed_height_ft)} ft",
                 f"{_g(proposed_height_ft)} {'<=' if proposed_height_ft <= min_h else '>'} {_g(min_h)} ft -> "
                 f"{'protected by state law' if proposed_height_ft <= min_h else 'needs local approval of the extra height'}",
                 ADU_HT)
    if adu_type in ("detached", "attached"):
        setback = float(ctx.P(ADU_HT, "max_side_rear_setback_ft"))
        if proposed_side_rear_setback_ft is not None:
            ok = proposed_side_rear_setback_ft >= setback
            ctx.item("Side/rear setback", f"{_g(proposed_side_rear_setback_ft)} ft",
                     f"{_g(proposed_side_rear_setback_ft)} {'>=' if ok else '<'} {_g(setback)} ft -> "
                     f"{'city cannot require more' if ok else 'city may require up to ' + _g(setback) + ' ft'}", ADU_HT)
    elif adu_type == "conversion":
        ctx.note("Conversions of existing structures need no additional setback.", ADU_HT)

    # -- approval clocks
    cdl = adl = None
    basis = ""
    eff15 = date.fromisoformat(core.rule(ADU_15).effective)
    if sub and sub < eff15:
        ctx.note(f"Received {sub.isoformat()}, before the {eff15.isoformat()} start of the "
                 f"{ctx.P(ADU_15, 'completeness_deadline_business_days')}-business-day completeness clock (SB 543), so "
                 "no completeness deadline is computed; give application_complete_date for the approval clock.", ADU_15)
    elif sub:
        n = ctx.P(ADU_15, "completeness_deadline_business_days")
        cdl = _add_business_days(sub, n, closed)
        skipped = sorted(d.isoformat() for d in closed if sub < d <= cdl and d.weekday() < 5)
        ctx.item("Completeness determination due", cdl.isoformat(),
                 f"{sub.isoformat()} + {n} business days (Mon-Fri"
                 + (f", skipping agency closures {', '.join(skipped)}" if skipped else "") + f") = {cdl.isoformat()}; "
                 f"if no written determination by then: {ctx.P(ADU_15, 'consequence_if_missed')}", ADU_15)
        if not closed:
            ctx.note("Business days here skip weekends only; each state or local holiday the agency is closed in the "
                     "window moves the deadline one business day later. List them in agency_closed_dates, and confirm "
                     "before treating the application as deemed complete.")
        if comp and comp > cdl:
            late = cdl + timedelta(days=ctx.P(ADU_60, "deadline_days"))
            ctx.note(f"The complete date {comp.isoformat()} is after the {cdl.isoformat()} completeness deadline. Unless "
                     f"the agency sent a written incompleteness notice by {cdl.isoformat()}, the missed deadline applies "
                     f"that day ({ctx.P(ADU_15, 'consequence_if_missed')}) and the approval deadline is "
                     f"{late.isoformat()}, not the later date computed from {comp.isoformat()}.", ADU_15)
    if comp or cdl:
        days = ctx.P(ADU_60, "deadline_days")
        start = comp or cdl
        adl = start + timedelta(days=days)
        basis = ("from the date the application was complete" if comp else
                 "latest case: from the completeness deadline, if the agency is silent until then")
        ctx.item("Approve/deny deadline", adl.isoformat(),
                 f"{start.isoformat()} + {days} calendar days = {adl.isoformat()} ({basis}); if missed: "
                 f"{ctx.P(ADU_60, 'consequence_if_missed')}", ADU_60)
        ctx.note(f"The {days}-day clock can be tolled at the applicant's request ({ctx.P(ADU_60, 'tolling')}).", ADU_60)
        if adl < today:
            ctx.note(f"The {adl.isoformat()} approve/deny deadline has passed. If the agency has not acted and the clock "
                     f"was not tolled, the application is {ctx.P(ADU_60, 'consequence_if_missed')}: ask the agency in "
                     "writing to issue the permit.", ADU_60)
        ctx.note(f"If the agency finds the application incomplete it must give {ctx.P(ADU_15, 'notice_form')}; the "
                 "applicant can appeal in writing and the agency must issue a final written determination within "
                 f"{ctx.P(ADU_15, 'appeal_decision_business_days')} business days of the appeal.", ADU_15)
    tolled = bool(submitted_with_new_primary_dwelling)
    if tolled:
        ctx.note("Submitted with a new primary dwelling: the ADU clock may be delayed until the agency acts on the "
                 "primary dwelling permit.", ADU_60)

    # -- impact fees
    imp_lim = ctx.P(ADU_IMPACT, "jadu_impact_fee_exempt_max_sqft" if adu_type == "jadu"
                    else "adu_impact_fee_exempt_max_sqft")
    imp_exempt = sq_ft <= imp_lim
    imp = 0.0 if imp_exempt else None
    ctx.item("Impact fee exemption", "exempt" if imp_exempt else "fees may be charged (proportionate)",
             f"{_g(sq_ft)} {'<=' if imp_exempt else '>'} {imp_lim} sq ft", ADU_IMPACT)
    if not imp_exempt:
        if impact_fee_for_primary_dwelling_usd is not None and primary_dwelling_sq_ft:
            imp = _money(impact_fee_for_primary_dwelling_usd * sq_ft / primary_dwelling_sq_ft)
            ctx.item("Proportionate impact fee", _usd(imp),
                     f"{_usd(impact_fee_for_primary_dwelling_usd)} x {_g(sq_ft)} / {_g(primary_dwelling_sq_ft)} sq ft "
                     f"primary = {_usd(imp)}", ADU_IMPACT)
        else:
            ctx.note("Give impact_fee_for_primary_dwelling_usd and primary_dwelling_sq_ft to compute the proportionate "
                     "fee (fee x ADU sq ft / primary dwelling sq ft).", ADU_IMPACT)
    ctx.note("Utility connection fees and capacity charges follow separate proportionality rules (not in the "
             "rulebook); ask the water and sewer agencies.")

    # -- school fees
    s_lim = ctx.P(SCHOOL_ADU, "school_fee_exempt_lt_sqft")
    s_exempt = sq_ft < s_lim
    ctx.item("School fee exemption", "exempt" if s_exempt else "Level 1 fee applies",
             f"{_g(sq_ft)} {'<' if s_exempt else '>='} {s_lim} sq ft (exempt only if less than {s_lim})", SCHOOL_ADU)
    school = 0.0 if s_exempt else _school_fee(ctx, sq_ft, "residential", today, school_district_rate_usd_per_sqft,
                                             "School fee estimate")
    if not s_exempt and adu_type in ("conversion", "jadu"):
        ctx.note("The estimate assumes the converted area is new assessable space (for example a garage). "
                 + ctx.U("gov-65995-b1-assessable-space")["note"] + " Space that is already living area adds none; "
                 "ask the district whether any fee is due.")

    # -- occupancy and use
    occ = None
    if adu_type == "jadu":
        if jadu_shares_bathroom is None:
            ctx.note("Set jadu_shares_bathroom: owner occupancy is required only if the JADU shares the bathroom with "
                     "the main house.", JADU)
        else:
            occ = bool(jadu_shares_bathroom)
            ctx.item("JADU owner occupancy", "required" if occ else "not required",
                     f"{ctx.P(JADU, 'owner_occupancy_required_if')}: {'yes' if occ else 'no'}", JADU)
        if not ctx.P(JADU, "short_term_rental_allowed"):
            ctx.note("No short-term rentals: JADU rentals must be for terms longer than 30 days.", JADU)

    # -- contracting
    down = cancel = None
    if contract_price_usd is not None:
        cap, pct = ctx.P(ADU_HIC, "max_down_payment_usd"), ctx.P(ADU_HIC, "max_down_payment_percent_of_contract")
        down = _money(min(cap, contract_price_usd * pct / 100))
        ctx.item("Maximum down payment", _usd(down),
                 f"lesser of {_usd(cap)} or {pct}% x {_usd(contract_price_usd)} = "
                 f"{_usd(contract_price_usd * pct / 100)} -> {_usd(down)}", ADU_HIC)
        cancel = ctx.P(ADU_HIC, "cancel_business_days_senior_65_plus" if buyer_65_or_older
                       else "cancel_business_days_standard")
        ctx.step(f"Use a written home improvement contract; buyer may cancel within {cancel} business days; progress "
                 "payments may not get ahead of work done", "Contractor", "At signing", ADU_HIC)
        ctx.note(f"The {cancel}-business-day cancel right runs to midnight of the last business day after the buyer "
                 "gets a signed copy with the notice; it does not apply if the contract was "
                 f"{ctx.P(CANCEL, 'exceptions')[0]}.", CANCEL)
        prime = ctx.P(ADU_LIC, "prime_allowed")
        ctx.pro(f"Prime contractor: {prime[0]} General Building" + (f", or {prime[1]}" if is_manufactured_home else "")
                + " (specialty C classes only as subcontractors for their trade)")
        ctx.use(ADU_LIC)
    if is_manufactured_home:
        ctx.note(f"Manufactured-home ADU: permits may come from {ctx.P(ADU_ISSUER, 'issuer_manufactured_home_possible')} "
                 f"instead of the {ctx.P(ADU_ISSUER, 'issuer_site_built')}.", ADU_ISSUER)
    n_bd = ctx.P(AB1308, "deadline_business_days")
    ctx.note(f"At the end, notify the building department the permitted work is complete: it must inspect within "
             f"{n_bd} business days (H&S 17970.3; scope limits not confirmed).", AB1308)

    res = AduResult(as_of=today.isoformat(), lot_type=lot_type, adu_type=adu_type, sq_ft=sq_ft, size_status=status,
                    city_max_size_cannot_be_below_sqft=floor, guaranteed_sqft=guaranteed, state_default_max_sqft=default,
                    min_height_city_must_allow_ft=min_h, max_side_rear_setback_city_may_require_ft=setback,
                    completeness_deadline=cdl.isoformat() if cdl else None,
                    approval_deadline=adl.isoformat() if adl else None, approval_deadline_basis=basis,
                    approval_clock_may_be_tolled=tolled, impact_fee_exempt=imp_exempt, impact_fee_estimate_usd=imp,
                    school_fee_exempt=s_exempt, school_fee_estimate_usd=school, owner_occupancy_required=occ,
                    max_down_payment_usd=down, cancellation_business_days=cancel)
    return _finish(res, ctx)


# ================================================================ MCP tools
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    D = "ISO date, e.g. 2026-11-25"
    PT = ("single_family (one-/two-family dwelling or townhouse, CRC), multifamily_low_rise (3 stories or fewer), "
          "multifamily_high_rise, or nonresidential (CBC)")

    @mcp.tool(name="permit_required", annotations=ro)
    def _permit_required(
        work_description: Annotated[str, Field(description="Plain description of the job, e.g. '8x12 storage shed, no power'")],
        structure_type: Annotated[WorkType, Field(description="Kind of work. Exemption types: detached_accessory_structure (shed/playhouse), fence, retaining_wall, finish_work, sidewalk_or_driveway, deck, playground_equipment, ordinary_repair, equipment_repair_or_identical_part; everything else needs a permit")],
        project_type: Annotated[ProjectType, Field(description=PT)] = "single_family",
        valuation_usd: Annotated[Optional[float], Field(ge=0, description="Total job price / permit valuation: labor, materials (even owner-supplied) and everything else")] = None,
        sq_ft: Annotated[Optional[float], Field(ge=0, description="Floor area of the shed, deck or addition (new building: gross area)")] = None,
        fence_height_ft: Annotated[Optional[float], Field(ge=0, description="Fence height in feet")] = None,
        retaining_wall_height_ft: Annotated[Optional[float], Field(ge=0, description="Retaining wall height in feet, bottom of footing to top of wall")] = None,
        is_one_story_detached_accessory: Annotated[Optional[bool], Field(description="Shed/playhouse is one story and detached from other buildings")] = None,
        retaining_wall_supports_surcharge: Annotated[bool, Field(description="Wall holds back a slope, driveway, building or other surcharge")] = False,
        retaining_wall_impounds_flammable_liquids: Annotated[bool, Field(description="Wall impounds Class I, II or IIIA liquids")] = False,
        is_pool_barrier: Annotated[bool, Field(description="Fence serves as a swimming pool barrier")] = False,
        height_above_grade_in: Annotated[Optional[float], Field(ge=0, description="Deck or walk height above grade in inches (highest point)")] = None,
        deck_attached_or_serves_exit_door: Annotated[bool, Field(description="Deck is attached to the house or serves the required exit door")] = False,
        affects_structure_egress_or_utilities: Annotated[bool, Field(description="Repair/finish work cuts walls, touches structure or exits, or moves plumbing, wiring or gas")] = False,
        includes_electrical_plumbing_or_gas: Annotated[bool, Field(description="Job includes any electrical, plumbing, gas or mechanical work")] = False,
        stories: Annotated[int, Field(ge=1, description="Stories of the building (SMIP fee category)")] = 1,
        year_built: Annotated[Optional[int], Field(ge=1700, le=2100, description="Year the existing building was built (pre-1994 fixtures, pre-1978 lead)")] = None,
        has_fuel_burning_appliance_or_attached_garage: Annotated[Optional[bool], Field(description="Dwelling has a gas/fuel appliance, fireplace or attached garage (CO alarms)")] = None,
        owner_builder: Annotated[bool, Field(description="The property owner will pull the permit as owner-builder")] = False,
        owner_builder_work_by: Annotated[OwnerBuilderWork, Field(description="Owner-builder only: who does the work")] = "owner_or_own_employees",
        owner_intends_to_sell: Annotated[bool, Field(description="Owner-builder only: owner plans to sell or offer the property for sale")] = False,
        performer_advertises_or_has_employees: Annotated[bool, Field(description="The person doing an under-$1,000 job advertises as a contractor or has helpers")] = False,
        part_of_larger_project: Annotated[bool, Field(description="Job is part of a bigger project or one of several small contracts")] = False,
        permit_application_date: Annotated[Optional[str], Field(description=f"{D}. Sets the code edition; default as_of")] = None,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Evaluate as of this date; default today in California")] = None,
        existing_building_sq_ft: Annotated[Optional[float], Field(ge=0, description="Existing floor area before an addition (pre-1994 multifamily/commercial fixture retrofit 10% test)")] = None,
        owner_lived_in_home_12_months_before_completion: Annotated[Optional[bool], Field(description="Owner-builder only: the home is the owner's principal residence and they will have lived there 12 months before the work is completed (B&P 7044(a)(3))")] = None,
        permit_issued_date: Annotated[Optional[str], Field(description=f"{D}. Day the permit was issued; returns start_work_by (12-month start deadline)")] = None,
        demolition_start_date: Annotated[Optional[str], Field(description=f"{D}. Demolition only: planned start; returns demolition_notice_send_by for the air district asbestos/demolition notice")] = None,
    ) -> PermitRequiredResult:
        """Is a building permit required in California for this job, and who may do it?

        Applies the CBC 105.2 / CRC R105.2 exemptions the rulebook holds (one-story detached shed/playhouse up to the
        sq ft limit, fences not over the height limit except pool barriers, retaining walls not over the height limit
        measured bottom of footing to top with no surcharge) plus flagged model-code items (finish work, flat
        sidewalks/driveways, small detached decks, playground equipment, ordinary repairs). Local amendments can
        override. Returns determination (not_required / required / need_more_info / check_with_building_department),
        basis, likely separate electrical/plumbing/mechanical permits, license_required (True / False / None = depends)
        with the B&P 7048 under-$1,000 minor work test or the B&P 7044 owner-builder analysis ((a)(1) self-performed
        not for sale, (a)(2) licensed trades, (a)(3) homeowner improving a principal residence lived in 12 months;
        owner_builder_warnings), smoke/CO alarm retrofit on dwelling permits over $1,000, pre-1994 water-conserving
        plumbing fixture retrofit (single-family; multifamily/commercial $150,000 alteration or 10% addition tests),
        lead RRP for pre-1978 housing, demolition asbestos-notice and C-22 abatement steps, pool safety steps, permit
        counter documents (license statement, workers' comp declaration), contract steps, state SMIP and BSASRF fees
        and school fees with the math, the licensed professionals needed, line_items and checklist with a rule citation
        each, verified and unverified_sources. Due dates: start_work_by (permit_issued_date + 12 months) and
        demolition_notice_send_by (demolition_start_date minus 10 working days / SCAQMD 14 calendar days, whichever is
        earlier); tell the user to act before these dates. Not legal advice."""
        return permit_required(work_description, structure_type, project_type, valuation_usd, sq_ft, fence_height_ft,
                               retaining_wall_height_ft, is_one_story_detached_accessory,
                               retaining_wall_supports_surcharge, retaining_wall_impounds_flammable_liquids,
                               is_pool_barrier, height_above_grade_in, deck_attached_or_serves_exit_door,
                               affects_structure_egress_or_utilities, includes_electrical_plumbing_or_gas, stories,
                               year_built, has_fuel_burning_appliance_or_attached_garage, owner_builder,
                               owner_builder_work_by, owner_intends_to_sell, performer_advertises_or_has_employees,
                               part_of_larger_project, permit_application_date, as_of, existing_building_sq_ft,
                               owner_lived_in_home_12_months_before_completion, permit_issued_date,
                               demolition_start_date)

    @mcp.tool(name="energy_compliance_triggers", annotations=ro)
    def _energy(
        project_type: Annotated[ProjectType, Field(description=PT)],
        scope: Annotated[EnergyScope, Field(description="new_construction, addition, alteration, hvac_changeout, duct_alteration, water_heater, reroof, window_replacement or lighting_alteration")],
        climate_zone: Annotated[Optional[int], Field(ge=1, le=16, description="California building climate zone (CEC map), echoed for the energy consultant")] = None,
        ducts_added_or_replaced_ft: Annotated[Optional[float], Field(ge=0, description="Length of duct added or replaced, in feet")] = None,
        percent_of_duct_system_replaced: Annotated[Optional[float], Field(ge=0, le=100, description="Share of the whole duct system that is new or replaced, %")] = None,
        new_or_replacement_hvac_equipment: Annotated[Optional[bool], Field(description="Addition/alteration installs new or replacement heating/cooling equipment")] = None,
        electrical_service_amps: Annotated[Optional[int], Field(ge=0, description="New home: main electrical service size in amps")] = None,
        battery_storage_installed: Annotated[bool, Field(description="New home: a battery energy storage system is installed")] = False,
        pv_kwdc_required: Annotated[Optional[float], Field(ge=0, description="New home: PV size from the compliance software (Equation 150.1-C), kWdc")] = None,
        gas_appliances_planned: Annotated[bool, Field(description="New home: gas or propane appliances are planned")] = False,
        homes_in_subdivision: Annotated[Optional[int], Field(ge=1, description="New home: number of homes in the subdivision")] = None,
        permit_application_date: Annotated[Optional[str], Field(description=f"{D}. Sets the Energy Code edition; default as_of")] = None,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
        ductless_system: Annotated[bool, Field(description="The new/replacement system is ductless (mini-split heads): no duct, airflow or fan efficacy tests")] = False,
    ) -> EnergyResult:
        """California Title 24 Part 6 Energy Code triggers for a permit (2025 code for applications from 2026-01-01).

        Returns energy_code_edition; documents (CF1R or CF1R-ALT certificate of compliance at plan check, registered
        with the HERS/ECC data registry when field verification is needed; CF2R certificate of installation signed by
        the installer; CF3R certificate of verification by an independent certified HERS rater before final;
        nonresidential NRCC/NRCI); hers_verifications with status required/likely/possible/not_triggered (duct
        leakage, refrigerant charge, airflow, fan efficacy, ventilation, QII) including the duct-length and 75%-new
        duct system tests (an equipment changeout keeps duct leakage "likely" because the connected ducts are tested;
        ductless_system drops the duct, airflow and fan efficacy tests); for new single-family homes: solar PV required, PV size after the 25% battery credit,
        battery-ready over 125 A service, heat-pump baseline and electric-ready for gas appliances. Includes
        line_items, checklist, licensed_professionals, notes on gaps (climate-zone values, nonresidential acceptance
        tests), verified and citations. Not legal advice."""
        return energy_compliance_triggers(project_type, scope, climate_zone, ducts_added_or_replaced_ft,
                                          percent_of_duct_system_replaced, new_or_replacement_hvac_equipment,
                                          electrical_service_amps, battery_storage_installed, pv_kwdc_required,
                                          gas_appliances_planned, homes_in_subdivision, permit_application_date, as_of,
                                          ductless_system)

    @mcp.tool(name="calgreen_and_stormwater", annotations=ro)
    def _calgreen(
        project_type: Annotated[ProjectType, Field(description=PT)],
        scope: Annotated[GreenScope, Field(description="new_construction, addition or alteration")] = "new_construction",
        valuation_usd: Annotated[Optional[float], Field(ge=0, description="Permit valuation (nonresidential alteration trigger)")] = None,
        sq_ft: Annotated[Optional[float], Field(ge=0, description="Building area (addition: added area); used for the addition trigger and lbs/sq ft")] = None,
        disturbed_acres: Annotated[Optional[float], Field(ge=0, description="Land disturbed by clearing, grading, excavating, in acres")] = None,
        part_of_larger_common_plan: Annotated[bool, Field(description="Site is part of a larger common plan of development disturbing 1+ acre")] = False,
        increases_conditioned_area_volume_or_size: Annotated[Optional[bool], Field(description="Residential addition/alteration increases conditioned area, volume or size")] = None,
        has_attached_private_garage: Annotated[Optional[bool], Field(description="New one-/two-family home or townhouse has an attached private garage")] = None,
        ev_40a_circuit_installed: Annotated[bool, Field(description="A 40 A 208/240 V EV branch circuit will actually be installed")] = False,
        cd_waste_generated_tons: Annotated[Optional[float], Field(ge=0, description="Total nonhazardous C&D waste generated (tons, or cubic yards if both inputs use them)")] = None,
        cd_waste_diverted_tons: Annotated[Optional[float], Field(ge=0, description="Part of it recycled or salvaged (same unit)")] = None,
        landfill_disposal_lbs: Annotated[Optional[float], Field(ge=0, description="C&D waste sent to landfill, lbs (low-disposal alternative)")] = None,
        rainfall_erosivity_r_factor: Annotated[Optional[float], Field(ge=0, description="RUSLE R factor for the construction period (erosivity waiver)")] = None,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
    ) -> CalgreenResult:
        """CALGreen (Title 24 Part 11) and state construction stormwater triggers for a California project.

        Returns whether CALGreen mandatory measures apply (nonresidential additions 1,000+ sq ft or alterations with a
        $200,000+ permit valuation; residential additions that add conditioned area), the 65% C&D waste diversion
        requirement with the diversion math and shortfall, the lbs/sq ft low-disposal alternative, EV-capable raceway
        and 40 A panel space for new homes with attached garages, and whether the Construction General Permit applies
        (1+ acre disturbed or part of a common plan; erosivity waiver under 5 acres with R < 5) with the steps: NOI and
        PRDs in SMARTS by the owner (WDID), SWPPP by a QSD, QSP inspections around Qualifying Precipitation Events, and
        the Notice of Termination. Includes line_items, checklist, licensed_professionals, verified and citations.
        Not legal advice."""
        return calgreen_and_stormwater(project_type, scope, valuation_usd, sq_ft, disturbed_acres,
                                       part_of_larger_common_plan, increases_conditioned_area_volume_or_size,
                                       has_attached_private_garage, ev_40a_circuit_installed, cd_waste_generated_tons,
                                       cd_waste_diverted_tons, landfill_disposal_lbs, rainfall_erosivity_r_factor, as_of)

    @mcp.tool(name="adu_rules", annotations=ro)
    def _adu(
        adu_type: Annotated[AduType, Field(description="detached (new), attached (new), conversion (existing space) or jadu")],
        sq_ft: Annotated[float, Field(gt=0, description="ADU interior livable square feet")],
        lot_type: Annotated[LotType, Field(description="single_family or multifamily lot")] = "single_family",
        bedrooms: Annotated[int, Field(ge=0, description="Bedrooms in the ADU (0 = studio)")] = 1,
        primary_dwelling_sq_ft: Annotated[Optional[float], Field(ge=0, description="Primary home square feet (attached 50% cap, proportionate fees)")] = None,
        proposed_height_ft: Annotated[Optional[float], Field(ge=0, description="Proposed ADU height, ft")] = None,
        near_major_transit: Annotated[bool, Field(description="Within 1/2 mile walking distance of a major transit stop or high-quality transit corridor")] = False,
        lot_has_multistory_multifamily: Annotated[bool, Field(description="Lot has an existing or proposed multistory multifamily dwelling")] = False,
        primary_zone_height_limit_ft: Annotated[Optional[float], Field(ge=0, description="Zoning height limit for the primary dwelling (attached ADU cap)")] = None,
        proposed_side_rear_setback_ft: Annotated[Optional[float], Field(ge=0, description="Proposed side/rear setback, ft")] = None,
        jadu_shares_bathroom: Annotated[Optional[bool], Field(description="JADU shares the bathroom with the main house")] = None,
        application_submitted_date: Annotated[Optional[str], Field(description=f"{D}. Day the agency received the ADU application")] = None,
        application_complete_date: Annotated[Optional[str], Field(description=f"{D}. Day it was determined (or deemed) complete")] = None,
        submitted_with_new_primary_dwelling: Annotated[bool, Field(description="ADU submitted together with a new primary dwelling permit")] = False,
        impact_fee_for_primary_dwelling_usd: Annotated[Optional[float], Field(ge=0, description="The agency's impact fee for a primary dwelling, USD")] = None,
        school_district_rate_usd_per_sqft: Annotated[Optional[float], Field(ge=0, description="School district's adopted rate per sq ft; default the state Level 1 cap")] = None,
        contract_price_usd: Annotated[Optional[float], Field(ge=0, description="Home improvement contract price for the ADU (down payment cap)")] = None,
        buyer_65_or_older: Annotated[bool, Field(description="Homeowner buyer is 65 or older (longer cancel period)")] = False,
        is_manufactured_home: Annotated[bool, Field(description="ADU is a manufactured home built off site")] = False,
        as_of: Annotated[Optional[str], Field(description=f"{D}. Default today in California")] = None,
        agency_closed_dates: Annotated[Optional[list[str]], Field(description="ISO dates the permitting agency is closed (holidays, furlough days) inside the 15-business-day window; skipped like weekends")] = None,
    ) -> AduResult:
        """California state ADU/JADU rules for one unit (Gov Code 66310+, as amended by SB 543 for 2026).

        Returns size_status against the state floors and caps (800 sq ft guaranteed with 4-ft setbacks; the city's
        maximum may not be below 850 sq ft studio/1 BR or 1,000 sq ft 2+ BR; default caps 1,200 sq ft detached and 50%
        of the primary home attached; JADU 500 sq ft), the height the city must allow (16/18/25 ft rules), the 4-ft
        setback cap, the 15-business-day completeness deadline and 60-day deemed-approved date, impact-fee exemption
        (750 sq ft ADU / 500 sq ft JADU) or the proportionate fee, the school-fee exemption under 500 sq ft or the Level
        1 estimate (2026 rate), JADU owner-occupancy, and with contract_price_usd the home improvement contract down
        payment cap, cancel period and license class (B, or C-47 for manufactured). Business days skip weekends and
        any agency_closed_dates given (not holidays otherwise). Notes say when the 60-day deadline has passed (deemed
        approved) or when a late completeness finding means the application was deemed complete earlier. Includes
        line_items with the math, checklist, verified and citations. Local ordinances can be more generous, never
        stricter than these floors. Not legal advice."""
        return adu_rules(adu_type, sq_ft, lot_type, bedrooms, primary_dwelling_sq_ft, proposed_height_ft,
                         near_major_transit, lot_has_multistory_multifamily, primary_zone_height_limit_ft,
                         proposed_side_rear_setback_ft, jadu_shares_bathroom, application_submitted_date,
                         application_complete_date, submitted_with_new_primary_dwelling,
                         impact_fee_for_primary_dwelling_usd, school_district_rate_usd_per_sqft, contract_price_usd,
                         buyer_65_or_older, is_manufactured_home, as_of, agency_closed_dates)
