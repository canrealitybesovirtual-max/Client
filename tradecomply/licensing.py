"""Contractor licensing engine on CSLB data (B&P 7000-7191, 16 CCR 830-834).

  bulk_check_subcontractors        a sub list (CSV text or rows) -> per-sub risk table, counts by risk, top actions
  classification_for_scope         which CSLB classes (A / B / B-2 / C-xx) can perform a scope, with the rule text
  company_license_obligations      bonds, LLC insurance, workers' comp, renewal fees and 90-day CSLB notices
  unlicensed_contracting_exposure  B&P 7028 penalties, 7031 disgorgement, 7048 minor-work exemption

Every legal number is read from the rulebook with core.rule(id).param(key). License records come from
logic.check_subcontractor / logic.verify_license (CSLB master file snapshot, else a CSV, else demo fixtures).
A license that is missing from a partial copy of CSLB data is never reported as unlicensed.
"""
import csv
import io
import re
from datetime import date as Date, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import core, logic
from .cslb import norm_class

# ---------------------------------------------------------------- rule ids
R_7118, R_7118_PEN, R_7118_DISC = ("contracting-with-unlicensed-contractor", "civpen-7118-contracting-with-unlicensed",
                                   "discipline-guideline-bp-7118")
R_7117_6, R_830, R_831, R_7059A, R_834 = ("work-only-in-classifications-held", "classification-policy-no-work-outside-class",
                                          "incidental-and-supplemental-definition", "specialty-incidental-supplemental-work",
                                          "limitation-of-classification")
R_CLASSES, R_A, R_B, R_B_PRIME, R_B_SUB, R_B_FIRE_WELL = (
    "specialty-classification-list", "general-engineering-contractor-def", "general-building-contractor-def",
    "b-prime-contract-two-trade-rule", "b-subcontract-two-trade-rule", "b-fire-protection-well-drilling-restriction")
R_B2, R_B2_MIN, R_B2_MEP, R_SOLAR_AB = ("b2-residential-remodeling-scope", "b2-three-trade-minimum",
                                       "b2-mep-system-restriction", "solar-work-within-class-a-b-c61")
R_B2_STRUCT, R_B2_RESTRICTED, R_C38 = ("b2-no-structural-load-bearing-work", "b2-restricted-trades-c16-c22-c57",
                                       "class-c38-refrigeration-50f-threshold")
R_C7, R_C46, R_C61, R_FIRE, R_ASB, R_ASB_B, R_WELL, R_TREE, R_POOL_C27, R_ADU, R_PW_MAJORITY, R_HAZ, R_UST = (
    "class-c07-low-voltage-91-volt-limit", "class-c46-solar-battery-80kwh-limit", "class-c61-limited-specialty-scope",
    "fire-protection-system-installer", "asbestos-100-sqft-c22-or-certification", "asbestos-gc-must-hold-c22-or-subcontract",
    "water-well-c57-license-required", "tree-work-requires-license", "landscape-c27-prime-pool-spa",
    "adu-license-classification-b-or-c47", "public-works-specialty-majority-rule",
    "hazardous-substance-removal-certification", "underground-storage-tank-haz-certification")
R_BOND, R_BOND_CANCEL, R_QBOND, R_QBOND_EXEMPT, R_LLC_BOND = (
    "contractor-bond-25000", "bond-cancellation-30-day-suspension", "qualifier-bond-25000",
    "qualifier-bond-10-percent-ownership-exemption", "llc-employee-worker-bond-100000")
R_LLC_INS, R_CGL, R_HIC_CGL, R_INACTIVE, R_SOS, R_LLC_PERSONAL = (
    "llc-liability-insurance-aggregate", "cgl-required-only-for-llc", "hic-cgl-insurance-notice",
    "inactive-license-no-bond-qualifier-wc", "sos-good-standing-or-cslb-suspension", "llc-suspended-personal-liability-1m")
R_WC_EXEMPT, R_WC_2028, R_WC_ONE, R_WC_RME, R_WC_HIRE, R_WC_INACTIVE, R_WC_LAPSE = (
    "workers-comp-exemption-until-2028", "wc-exemption-ends-2028", "wc-required-even-one-employee",
    "wc-exemption-barred-rme-or-employees", "wc-proof-within-90-days-of-hire", "workers-comp-not-required-inactive-license",
    "wc-lapse-automatic-suspension")
R_TERM, R_FEES, R_DELINQ, R_POSTMARK, R_RETRO, R_5YR, R_GRACE = (
    "license-expiration-two-year-cycle", "cslb-renewal-fees", "fees-delinquency-50-percent",
    "license-renewal-postmark-by-expiration", "retroactive-renewal-90-days", "expired-license-renewal-5-year-window",
    "incomplete-timely-renewal-30-day-grace")
R_NOTIFY, R_NOTIFY_PEN, R_MAIL, R_Q_NOTIFY, R_Q_REPLACE, R_Q_EXT, R_DISSOLVE, R_CONVICT = (
    "notify-changes-90-days", "civpen-7083-change-notification", "address-change-30-days",
    "qualifier-disassociation-notice-90-days", "qualifier-replacement-90-days", "qualifier-disassociation-90-days",
    "corp-llc-dissolution-merger-notice", "web_business-tax-insurance:report-convictions-90-days")
R_APP_FEES, R_NAME_FEE, R_FINGERPRINTS, R_RME_HOURS = ("cslb-application-fees", "cslb-business-name-change-fee",
                                                       "fingerprints-required-applicants", "rme-actively-engaged-32-hours")
R_MINOR, R_MINOR_SPLIT, R_MINOR_ADS_EMPLOYEES, R_AD_UNDER = (
    "minor-work-exemption-1000", "minor-work-no-splitting", "web_business-tax-insurance:minor-work-exemption-1000",
    "unlicensed-advertising-under-1000")
R_7028_1, R_7028_2, R_7028_3, R_7028_SOL = ("unlicensed-contracting-first-offense-penalty",
                                            "unlicensed-contracting-second-offense-penalty",
                                            "unlicensed-contracting-third-offense-penalty",
                                            "unlicensed-contracting-prosecution-deadline")
R_CITE, R_CITE_OLD, R_7031, R_7031B, R_SUBST, R_DISASTER, R_PUBLIC_BID, R_DOUBLE_BOND, R_AD_FINE, R_2750_5 = (
    "web_business-tax-insurance:unlicensed-citation-civil-penalty", "unlicensed-citation-civil-penalty-pre-july-2026",
    "unlicensed-no-right-to-sue-disgorgement", "owner-disgorgement-of-payments", "substantial-compliance-doctrine",
    "disaster-unlicensed-contracting-penalty", "unlicensed-public-agency-bid", "contractor-bond-double-for-prior-unlicensed",
    "unlicensed-advertising-fine", "lc-2750-5-license-required-for-ic-status")

# Operational reminder window for "expiring soon" flags. A scheduling setting, not a legal value.
EXPIRING_SOON_DAYS = 30

RISK_ORDER = {"do_not_use": 0, "high": 1, "medium": 2, "low": 3}
EntityType = Literal["sole_owner", "partnership", "corporation", "llc"]
ProjectType = Literal["residential_existing", "residential_new", "commercial", "public_works", "civil_infrastructure",
                      "adu", "manufactured_home", "other"]
ChangeType = Literal["business_address", "mailing_address", "business_name", "personnel", "qualifier_left",
                     "qualifier_bond_exemption", "multiple_license_exemption", "dissolution_or_merger",
                     "criminal_conviction"]


def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _day(value, field: str = "date") -> Optional[Date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{field} must be a date like 2026-11-25 or 11/25/2026, got {value!r}")


def _as_of(value) -> Date:
    return _day(value, "as_of") or logic.today_ca()


def _add_years(d: Date, years: int) -> Date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:                       # Feb 29 -> Feb 28
        return d.replace(year=d.year + years, day=28)


def _usd(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _rule_text(rule_id: str, limit: int = 420) -> str:
    r = core.rule(rule_id)
    text = (r.quote or r.plain_english or r.title or "").strip()
    return f"{r.citation_text}: {text[:limit]}{'...' if len(text) > limit else ''}"


def _formula_numbers(rule_id: str, key: str) -> list[float]:
    """Numbers in a rulebook formula string, e.g. 'max(10000, 0.20 * contract_price, ...)' -> [10000.0, 0.2, ...]."""
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", P(rule_id, key))]


def _code(c: str) -> str:
    """'C-10', 'c10', 'C10 Electrical', 'C-08' -> 'C10' (for comparing with CSLB records)."""
    n = norm_class(c)
    return re.sub(r"^([A-Z]+)0+(\d)", r"\1\2", n)


def _code_key(code: str):
    m = re.match(r"([A-Z]+)-?(\d*)", code)
    return (m[1], int(m[2] or 0)) if m else (code, 0)


# ================================================================ classification for scope
# Contractor vocabulary for each 16 CCR 832 class. The table below adds the rulebook's own scope phrases at import.
ALIASES: dict[str, tuple[str, ...]] = {
    "C-2": ("insulation", "insulate", "blown in insulation", "spray foam insulation", "acoustical ceiling",
            "acoustic ceiling", "acoustical tile"),
    "C-4": ("boiler", "hydronic", "hydronic heating", "steam fitting", "steam piping", "radiant heating", "radiator"),
    "C-5": ("framing", "rough carpentry", "formwork", "subfloor", "sub floor", "sheathing", "roof deck", "truss",
            "stud wall", "joist", "siding", "exterior stair", "garage door", "deck framing", "shear wall",
            "load bearing wall", "bearing wall", "beam", "header", "rafter", "structural framing",
            "seismic retrofit"),
    "C-6": ("cabinet", "cabinetry", "millwork", "finish carpentry", "trim", "baseboard", "crown molding", "casework",
            "interior door", "closet shelving", "built in", "vanity"),
    "C-7": ("low voltage", "data cabling", "network cabling", "structured cabling", "cat6", "cat 6", "cat5",
            "ethernet", "telephone system", "phone system", "sound system", "speaker", "audio visual", "av system",
            "cctv", "security camera", "camera system", "satellite dish", "cable tv", "intercom",
            "landscape lighting"),
    "C-8": ("concrete", "slab", "flatwork", "sidewalk", "concrete driveway", "curb gutter", "curb", "footing",
            "foundation", "stamped concrete", "concrete patio"),
    "C-9": ("drywall", "sheetrock", "gypsum board", "wallboard", "taping", "texture", "metal stud"),
    "C-10": ("electrical", "electric", "electrician", "wiring", "rewire", "outlet", "receptacle", "light switch",
             "dimmer", "electrical panel", "main panel", "service upgrade", "panel upgrade", "subpanel", "circuit",
             "breaker", "lighting", "light fixture", "recessed light", "can light", "ceiling fan", "ev charger",
             "ev charging", "car charger", "standby generator", "conduit", "landscape lighting", "battery",
             "battery storage", "energy storage", "bess", "powerwall",
             # 832.07 and 832.28 exclude fire alarms; 7026.12 excludes electrical alarms from fire protection.
             "fire alarm", "low voltage fire alarm", "fire alarm system"),
    "C-11": ("elevator", "dumbwaiter"),
    "C-12": ("grading", "excavation", "excavate", "earthwork", "trenching", "trench", "backfill", "cut fill",
             "asphalt", "paving", "blacktop", "site prep", "compaction"),
    "C-13": ("fence", "fencing", "chain link", "guard rail", "guardrail", "playground equipment", "flagpole",
             "backstop", "corral", "dog run"),
    "C-15": ("flooring", "carpet", "vinyl plank", "lvp", "laminate floor", "hardwood floor", "wood floor",
             "floor refinishing", "refinish floor", "linoleum", "vinyl floor"),
    "C-16": ("fire sprinkler", "fire suppression", "standpipe", "fire protection system", "hood suppression"),
    "C-17": ("glass", "glazing", "window replacement", "replacement window", "window", "storefront", "mirror",
             "shower door", "skylight"),
    "C-20": ("hvac", "air conditioning", "air conditioner", "ac unit", "furnace", "heat pump", "ductwork",
             "duct work", "duct", "mini split", "ventilation", "condenser"),
    "C-21": ("demolition", "demolish", "tear down", "teardown", "building moving", "house moving", "house raising"),
    "C-22": ("asbestos", "asbestos abatement", "acm removal"),
    "C-23": ("ornamental iron", "wrought iron", "ornamental metal", "metal railing", "iron railing", "iron gate"),
    "C-27": ("landscaping", "landscape", "irrigation", "drip system", "lawn", "sod", "planting", "hardscape",
             "garden", "artificial turf", "synthetic turf"),
    "C-28": ("lock", "locksmith", "door hardware", "access control", "card reader", "built in safe", "vault",
             "panic hardware", "panic bar", "door closer", "automatic gate", "gate operator", "security door"),
    "C-29": ("masonry", "brick", "block wall", "cmu", "cinder block", "stone veneer", "stonework",
             "masonry chimney", "paver", "retaining wall"),
    "C-31": ("traffic control", "flagging", "lane closure", "flagger"),
    "C-32": ("striping", "pavement marking", "parking lot striping", "wheel stop", "seal coat", "sealcoat",
             "slurry seal"),
    "C-33": ("painting", "paint", "repaint", "staining", "stain", "wallpaper", "wallcovering", "wall covering",
             "varnish"),
    "C-34": ("pipeline", "gas main", "water main", "directional boring"),
    "C-35": ("plaster", "plastering", "stucco", "lath", "lathing"),
    "C-36": ("plumbing", "plumber", "repipe", "re pipe", "water heater", "tankless", "faucet", "toilet", "sink",
             "shower valve", "drain line", "drain", "gas line", "sewer lateral", "backflow", "water line",
             "garbage disposal", "bathtub", "tub"),
    "C-38": ("refrigeration", "walk in cooler", "walk in freezer", "cold storage", "refrigerated case",
             "ice machine"),
    "C-39": ("roof", "roofing", "reroof", "re roof", "shingle", "tile roof", "torch down", "roof membrane",
             "tpo roof", "roof repair", "roof leak"),
    "C-42": ("septic", "septic tank", "leach field", "cesspool", "storm drain", "sewer line", "sanitary sewer"),
    "C-43": ("sheet metal", "rain gutter", "gutter", "downspout", "flashing", "metal flue", "kitchen hood"),
    "C-45": ("signage", "sign installation", "monument sign", "channel letter", "electric sign", "pylon sign"),
    "C-46": ("solar", "photovoltaic", "pv system", "solar panel", "solar water heating", "solar thermal",
             "solar pool heating", "solar hot water"),
    "C-47": ("manufactured home", "mobile home", "mobilehome"),
    "C-49": ("tree removal", "tree trimming", "tree pruning", "stump grinding", "stump removal", "palm trimming",
             "tree service"),
    "C-50": ("rebar", "reinforcing steel", "reinforcing bar"),
    "C-51": ("structural steel", "steel erection", "steel beam", "steel frame", "moment frame", "steel column"),
    "C-53": ("swimming pool", "pool", "spa", "hot tub", "pool remodel", "pool replaster"),
    "C-54": ("tile", "tiling", "backsplash", "ceramic tile", "porcelain tile", "shower tile", "stone tile",
             "granite countertop", "quartz countertop", "stone countertop", "countertop", "stone slab", "marble"),
    "C-55": ("water softener", "water filtration", "water conditioning", "reverse osmosis",
             "water treatment system"),
    "C-57": ("well drilling", "water well", "drill a well", "monitoring well", "geothermal well", "well pump"),
    "C-60": ("welding", "weld", "welder"),
}
# Rulebook scope phrases that are too generic (or belong to another trade in plain speech) to use as keywords.
_SKIP_SCOPE_PHRASES = {"associated equipment", "other finish carpentry", "signal systems", "temperature insulation",
                       "air conditioning units", "kitchen equipment", "protective coatings", "surface preparation"}
_PRE = {"a/c": "air conditioning", "w/": "with ", "curb and gutter": "curb gutter", "cut and fill": "cut fill",
        "tape and texture": "taping texture"}
_SPLIT = re.compile(r"[,;:\n+&()]|\band\b|\bplus\b|\bwith\b|\bincluding\b|\balso\b")
CIVIL_WORDS = ("road", "street", "highway", "bridge", "overpass", "culvert", "levee", "dam", "railroad", "tunnel",
               "airport", "runway", "sewer main", "water main", "storm drain system", "pipeline", "utility",
               "underground utilities", "site work", "sitework", "earthmoving", "land leveling", "flood control",
               "harbor", "wharf", "power plant", "refinery", "park", "playground")
WHOLE_BUILDING_WORDS = ("adu", "accessory dwelling", "granny flat", "new house", "new home", "custom home",
                        "room addition", "addition", "new construction", "ground up", "garage conversion")
REMODEL_WORDS = ("remodel", "renovation", "tenant improvement", "build out", "buildout")
BATTERY_WORDS = ("battery", "battery storage", "energy storage", "bess", "powerwall")
SOLAR_THERMAL_WORDS = ("solar water heating", "solar thermal", "solar pool heating", "solar hot water")
MEP_CLASSES = {"C-4", "C-10", "C-20", "C-36", "C-38"}
# B&P 7057.5(c)(2): a B-2 may not contract structural changes to load-bearing portions (no subcontract exception).
STRUCTURAL_WORDS = ("load bearing", "bearing wall", "structural", "beam", "header", "footing", "foundation",
                    "shear wall", "rafter", "truss", "joist", "roof structure", "roof framing", "seismic retrofit",
                    "underpinning", "underpin")
_NON_STRUCTURAL = re.compile(r"non ?(?:load )?(?:bearing|structural)")
FIXTURE_WORDS = ("faucet", "toilet", "sink", "tub", "bathtub", "fixture", "ceiling fan", "exhaust fan", "fan",
                 "switch", "outlet", "receptacle", "dimmer", "air filter", "grill", "register", "garbage disposal")
SYSTEM_WORDS = ("add", "new", "relocate", "move", "extend", "reroute", "rewire", "repipe", "re pipe", "upgrade",
                "panel", "subpanel", "circuit", "service", "duct", "ductwork", "furnace", "heat pump",
                "water heater", "tankless", "line", "sewer", "condenser", "system")
A_ISH = {"C-8", "C-12", "C-32", "C-34", "C-42"}      # trades that fall in B&P 7056's list when tied to fixed works
_GENERIC = set("a an the of in on at to for new install installation installing replace replacement replacing repair "
               "repairs remodel renovation existing house home room kitchen bath bathroom work project job complete "
               "full small minor upgrade upgrades misc miscellaneous labor material materials only per plans "
               "remove removal build building construct construction sq ft square feet foot detached attached story "
               "unit units approx about all rough finish out trim-out".split())


def _norm_text(s: str) -> str:
    s = (s or "").lower()
    for a, b in _PRE.items():
        s = s.replace(a, b)
    s = re.sub(r"[-_/]", " ", s)
    return re.sub(r"[ \t]+", " ", s)


@lru_cache(maxsize=None)
def _kw_regex(kw: str) -> re.Pattern:
    words = _norm_text(kw).split()
    if words[-1].endswith("s") and len(words[-1]) > 4:
        words[-1] = words[-1][:-1]
    return re.compile(r"\b" + r"\s+".join(map(re.escape, words)) + r"(?:s|es)?\b")


def _has(text: str, words) -> list[str]:
    return [w for w in words if _kw_regex(w).search(text)]


class ClassEntry(BaseModel):
    code: str
    name: str
    rule_id: str
    scope: list[str]
    keywords: list[str]


@lru_cache(maxsize=1)
def class_table() -> dict[str, ClassEntry]:
    """16 CCR 832 specialty classes from the rulebook: name, governing rule, scope list and match keywords."""
    names = P(R_CLASSES, "classes")
    by_code: dict[str, list[core.Rule]] = {}
    for r in core.all_rules():
        code = r.parameters.get("class_code")
        if code in names and r.id.startswith("class-"):
            by_code.setdefault(code, []).append(r)
    table = {}
    for code, name in names.items():
        rules = sorted(by_code.get(code, []), key=lambda r: (not r.id.endswith("-scope"), r.id))
        scope = [s for r in rules for s in r.parameters.get("scope_includes", [])]
        scope += [m for r in rules for m in r.parameters.get("materials", [])]
        derived = []
        for s in scope:
            s2 = _norm_text(re.sub(r"\(.*?\)", "", s)).strip()
            if 2 <= len(s2.split()) <= 3 and not re.search(r"[,.]|\band\b", s2) and s2 not in _SKIP_SCOPE_PHRASES:
                derived.append(s2)
        kws = list(dict.fromkeys([_norm_text(k) for k in ALIASES.get(code, ())] + derived))
        table[code] = ClassEntry(code=code, name=name, rule_id=rules[0].id if rules else R_CLASSES, scope=scope,
                                 keywords=kws)
    return table


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[str, str, re.Pattern]]:
    return [(code, kw, _kw_regex(kw)) for code, e in class_table().items() for kw in e.keywords]


class TradeMatch(BaseModel):
    segment: str
    classes: list[str] = Field(description="Best-matching class(es) for this piece of work")
    matched_keywords: list[str]
    also_possible: list[str] = Field(default_factory=list, description="Weaker matches, not counted as trades")
    battery: bool = False
    solar_thermal: bool = False
    mep_system_work: bool = False
    structural: bool = Field(False, description="Mentions load-bearing/structural work (B&P 7057.5(c)(2))")
    civil_work: list[str] = Field(default_factory=list)


def _detect(scope: str) -> tuple[list[TradeMatch], list[str], bool, bool]:
    """Split a scope into pieces of work and match each to 16 CCR 832 classes (longest keyword wins)."""
    text = _norm_text(scope)
    segs, unmatched = [], []
    whole, remodel = bool(_has(text, WHOLE_BUILDING_WORDS)), bool(_has(text, REMODEL_WORDS))
    hint_words = set(" ".join(WHOLE_BUILDING_WORDS + REMODEL_WORDS).split())
    for raw in _SPLIT.split(text):
        seg = raw.strip(" .")
        if not seg:
            continue
        best: dict[str, tuple[int, str]] = {}
        for code, kw, pat in _patterns():
            if pat.search(seg) and len(kw) > best.get(code, (0, ""))[0]:
                best[code] = (len(kw), kw)
        civil = _has(seg, CIVIL_WORDS)
        structural = bool(_has(_NON_STRUCTURAL.sub(" ", seg), STRUCTURAL_WORDS))
        if not best:
            if civil:
                segs.append(TradeMatch(segment=seg, classes=[], matched_keywords=[], civil_work=civil))
            elif {w for w in re.findall(r"[a-z]+", seg)} - _GENERIC - hint_words:
                unmatched.append(seg)
            continue
        top = max(v[0] for v in best.values())
        primary = sorted((c for c, v in best.items() if v[0] == top), key=_code_key)
        mep = bool(set(primary) & MEP_CLASSES) and (bool(_has(seg, SYSTEM_WORDS)) or not _has(seg, FIXTURE_WORDS))
        segs.append(TradeMatch(
            segment=seg, classes=primary, matched_keywords=sorted({best[c][1] for c in primary}),
            also_possible=sorted((c for c in best if c not in primary), key=_code_key),
            battery="C-10" in primary and bool(_has(seg, BATTERY_WORDS)),
            solar_thermal=bool(_has(seg, SOLAR_THERMAL_WORDS)), mep_system_work=mep, structural=structural,
            civil_work=civil))
    return segs, unmatched, whole, remodel


def _groups(segs: list[TradeMatch]) -> list[list[str]]:
    """Distinct trades in the scope. Classes tied on one piece of work (e.g. 'metal roofing': C-39 or C-43) are
    alternatives for the same trade, so they merge into one group and count once toward B&P 7057 / 7057.5."""
    groups: list[set[str]] = []
    for s in segs:
        cs = set(s.classes)
        if not cs:
            continue
        for g in [g for g in groups if g & cs]:
            groups.remove(g)
            cs |= g
        groups.append(cs)
    return sorted((sorted(g, key=_code_key) for g in groups), key=lambda g: _code_key(g[0]))


def _label(group: list[str]) -> str:
    return " or ".join(group)


class ClassOption(BaseModel):
    code: str = Field(description="A, B, B-2 or a C-xx specialty class")
    name: str
    status: Literal["yes", "conditional", "no"]
    basis: str
    conditions: list[str] = Field(default_factory=list)
    rule_ids: list[str]
    rule_text: str


class ClassificationResult(core.Result):
    scope_of_work: str
    project_type: str
    contract_role: str
    trades: list[TradeMatch]
    trade_classes: list[str]
    trade_groups: list[list[str]] = Field(description="Distinct trades. Classes in one group are alternatives for the "
                                                      "same piece of work and count as one trade")
    unrelated_trades_excluding_framing_carpentry: int
    options: list[ClassOption]
    can_perform: list[str] = Field(description="Classes that can take this contract as described")
    can_perform_with_conditions: list[str]
    unmatched_segments: list[str] = Field(default_factory=list)
    answer: str
    licensed_humans: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _carpentry_codes() -> set[str]:
    """Classes whose 16 CCR 832 name is carpentry/framing: B&P 7057(b) never counts them toward the two trades."""
    return {c for c, e in class_table().items() if "carpentry" in e.name.lower() or "framing" in e.name.lower()}


def _non_carpentry(groups: list[list[str]]) -> list[list[str]]:
    """Trades that count toward B&P 7057(b). Work a carpentry class could do (alone or as an alternative) never counts."""
    carp = _carpentry_codes()
    return [g for g in groups if not set(g) & carp]


def _opt(code, name, status, basis, conditions, rule_ids) -> ClassOption:
    return ClassOption(code=code, name=name, status=status, basis=basis, conditions=conditions,
                       rule_ids=list(dict.fromkeys(rule_ids)), rule_text=_rule_text(rule_ids[0]))


def _names(codes) -> str:
    t = class_table()
    return ", ".join(f"{c} {t[c].name}" if c in t else c for c in codes)


def _gnames(groups: list[list[str]]) -> str:
    t = class_table()
    return "; ".join(" or ".join(f"{c} {t[c].name}" if c in t else c for c in g) for g in groups)


def _covers(code: str, seg: TradeMatch) -> bool:
    """True if `code` can do this piece of work under its own class. C-10 also covers low-voltage (C-7) and PV
    (C-46, not solar thermal) work: 16 CCR 832.10 covers electrical work 'in any form', incl. PV systems."""
    if code in seg.classes:
        return True
    return code == "C-10" and ("C-7" in seg.classes or ("C-46" in seg.classes and not seg.solar_thermal))


def _specialty_option(code: str, segs: list[TradeMatch], role: str, project_type: str, battery_kwh: Optional[float],
                      asbestos_sq_ft: Optional[float], whole: bool, standalone: bool = False) -> ClassOption:
    e = class_table()[code]
    uncovered = [] if standalone else [s for s in segs if s.classes and not _covers(code, s)]
    conds, rules = [], [e.rule_id]
    basis = f"{code} {e.name} scope (16 CCR 832): {', '.join(e.scope[:6]) or e.name}."
    battery_only = any(s.battery for s in segs) and all(s.battery for s in segs if "C-10" in s.classes)
    if code == "C-10":
        if any(_covers(code, s) and code not in s.classes for s in segs):
            basis += " C-10 covers electrical work 'in any form or for any purpose', incl. low-voltage and PV systems."
    elif code == "C-7":
        basis += (f" Limited to energy-limited systems of {P(R_C7, 'max_voltage_v')} volts or less; low-voltage fire "
                  "alarm systems are excluded (they are C-10 work).")
    elif code == "C-38":
        basis += (f" Only for air, liquid or gas temperatures below {P(R_C38, 'temperature_threshold_f')} F "
                  f"({P(R_C38, 'temperature_threshold_c')} C); comfort heating and cooling is C-20 work.")
        rules.append(R_C38)
    elif code == "C-46":
        kwh_max = P(R_C46, "bess_max_rating_kwh")
        basis += (" A C-46 may do other trades only when required to install the solar system (832.46(a)). The "
                  "amended 832.10/832.46 battery text was stayed when the 2026 Law Book was printed; confirm its "
                  "status with CSLB.")
        bats = [s for s in uncovered if s.battery and s.classes == ["C-10"]]
        if bats and battery_only:
            uncovered = [s for s in uncovered if s not in bats]
            if battery_kwh is None:
                conds.append(f"Battery storage only as incidental to the PV install and only if rated {kwh_max} kWh or "
                             "less (16 CCR 832.46(c)); give battery_kwh to check.")
            elif battery_kwh <= kwh_max:
                basis += f" The {battery_kwh:g} kWh battery is within the {kwh_max} kWh C-46 limit (832.46(c))."
            else:
                conds.append(f"The {battery_kwh:g} kWh battery is over the {kwh_max} kWh C-46 limit, so the C-46 may not "
                             "contract for it: a C-10 must contract for and install it separately, or a C-10 or B "
                             "takes the whole job (16 CCR 832.46(b)-(c)).")
    elif code == "C-16":
        basis = ("Fire protection systems (other than electrical alarm systems) may be installed only by a contractor "
                 "holding the fire protection classification, or a qualifying owner-builder (B&P 7026.12).")
        rules.insert(0, R_FIRE)
    elif code == "C-22":
        sq = P(R_ASB, "threshold_sq_ft")
        basis = (f"Asbestos work on {sq} sq ft or more of asbestos-containing material needs C-22 or a qualifier who "
                 "passed the asbestos certification exam (B&P 7058.5); C-22 is a stand-alone class (16 CCR 833(a)).")
        rules.insert(0, R_ASB)
        if asbestos_sq_ft is not None and asbestos_sq_ft < sq:
            basis += f" At {asbestos_sq_ft:g} sq ft that requirement is not triggered."
    elif code == "C-57":
        basis = "Only a C-57 may drill, deepen, reperforate or destroy a water or monitoring well (Water Code 13750.5)."
        rules.insert(0, R_WELL)
    elif code == "C-49":
        basis += " Tree removal, pruning, stump removal and guying are contracting work (B&P 7026.1(a)(4))."
        rules.append(R_TREE)
    elif code == "C-53" and uncovered:
        basis += " C-53 scope includes the trades necessary to build the pool or spa."
    if code == "C-27" and any("C-53" in s.classes for s in uncovered):
        uncovered = [s for s in uncovered if "C-53" not in s.classes]
        conds.append("A C-27 prime may include a pool/spa only if it is part of the landscape project and the pool is "
                     "subcontracted to a single licensed C-53 (or the C-27 also holds C-53) (B&P 7027.5(b)(1))."
                     if role == "prime" else
                     "B&P 7027.5(b) lets a C-27 include a pool/spa only in a prime contract: as a subcontractor the "
                     "C-27 must also hold C-53, or the pool goes to a C-53 under its own contract.")
        rules.append(R_POOL_C27)
    if code == "C-47" and project_type == "adu":
        conds.append("Only for a manufactured home built off site and transported to the lot (CSLB ADU guidance).")
        rules.append(R_ADU)
    if whole and code != "C-47":
        return _opt(code, e.name, "no", basis + " A whole building or addition is a multi-trade B job; a specialty "
                    "contractor may take only its own trade on it, as a subcontract.",
                    [], rules + [R_ADU if project_type == "adu" else R_834])
    others = _groups(uncovered)
    if others and project_type == "public_works" and role == "prime":
        # B&P 7059(b)(1) replaces the private-work 834(c) bar: an authorized specialty prime subs out the rest.
        conds.append(f"Public works: the awarding agency decides which class may bid, and may not award the prime to a "
                     f"specialty contractor whose class is less than a majority of the project (B&P 7059(b)(1)). "
                     f"The other work ({_gnames(others)}) must be performed by licensed subcontractors unless it is "
                     f"incidental and supplemental to the {code} work (B&P 7059(a); 16 CCR 831).")
        rules += [R_PW_MAJORITY, R_831, R_7059A]
    elif others:
        conds.append(f"The other work ({_gnames(others)}) must be incidental and supplemental, meaning essential to "
                     f"accomplish the {code} work (16 CCR 831). Otherwise {code} may not contract for it at all, even "
                     "with a subcontractor (16 CCR 834(c)): use separate contracts or a B prime.")
        rules += [R_831, R_7059A, R_834]
    return _opt(code, e.name, "conditional" if conds else "yes", basis, conds, rules)


def _b_option(groups, segs, role, project_type, whole, remodel) -> ClassOption:
    trades = sorted({c for g in groups for c in g}, key=_code_key)
    non_carp = _non_carpentry(groups)
    solar = "C-46" in trades
    rule = R_B_PRIME if role == "prime" else R_B_SUB
    min_n = P(rule, "min_unrelated_trades_excluding_framing_carpentry")
    rules, conds = [rule, R_B], []
    civil = project_type == "civil_infrastructure" or (any(s.civil_work for s in segs) and set(trades) <= A_ISH)
    if civil and not solar:
        return _opt("B", "General Building", "no", "Class B covers structures for the shelter or enclosure of people, "
                    "animals or property (B&P 7057(a)); civil fixed works such as roads, utilities and grading are "
                    "Class A (B&P 7056).", [], [R_B, R_A, R_834])
    if solar:
        basis, status = "An active solar energy system counts as more than two unrelated building trades (16 CCR " \
                        "832.62(b)).", "yes"
        rules.append(R_SOLAR_AB)
    elif len(non_carp) >= min_n:
        basis, status = (f"{len(non_carp)} unrelated trades besides framing/carpentry ({_gnames(non_carp)}); B&P "
                         f"7057(b) needs at least {min_n}."), "yes"
    elif whole and not trades:
        basis, status = "A whole building or addition uses two or more unrelated building trades (B&P 7057(a)).", "yes"
    elif (whole or remodel) and not trades:
        basis, status = "A remodel normally uses several trades; list them to confirm the B&P 7057(b) count.", \
                        "conditional"
        conds.append(f"Only if the contract involves {min_n}+ unrelated trades besides framing/carpentry.")
    elif trades and not non_carp:
        basis, status = "Framing or carpentry alone: a B may take it as a prime or a subcontract (B&P 7057(b)).", "yes"
    else:
        one = _label(non_carp[0])
        basis = (f"Only {len(non_carp)} trade besides framing/carpentry ({_gnames(non_carp)}); as a {role} B&P 7057(b) "
                 f"needs at least {min_n}.")
        if role == "prime":
            status = "conditional"
            conds.append(f"The B must hold {one} itself or subcontract the {one} work to a licensed {one} contractor.")
        else:
            status = "no"
            basis += (f" A B may take this subcontract only if it also holds {one} (it then works under that class); "
                      "subcontracting the work out does not cure this for a subcontract.")
    for code, rid, what, cite in (("C-16", R_B_FIRE_WELL, "a fire protection system", "B&P 7057(c)"),
                                  ("C-57", R_B_FIRE_WELL, "well drilling", "B&P 7057(c)"),
                                  ("C-22", R_ASB_B, "asbestos abatement", "16 CCR 833(b)")):
        if code in trades:
            conds.append(f"The project includes {what}: the B must hold {code}"
                         + (" (or the asbestos certification plus DOSH registration)" if code == "C-22" else "")
                         + f" or subcontract it to a licensed {code} ({cite}).")
            rules.append(rid)
            if status == "yes":
                status = "conditional"
    if project_type == "adu":
        basis += " A B may build a site-built ADU and subcontract the specialty trades (CSLB ADU guidance)."
        rules.append(R_ADU)
    return _opt("B", "General Building", status, basis, conds, rules)


def _b2_option(groups, segs, project_type) -> ClassOption:
    min_n = P(R_B2_MIN, "min_unrelated_trades_per_contract")
    trades = {c for g in groups for c in g}
    if project_type != "residential_existing":
        return _opt("B-2", "Residential Remodeling", "no", "B-2 covers only improvements to an existing residential "
                    "wood-frame structure (B&P 7057.5); set project_type to residential_existing if that applies.",
                    [], [R_B2])
    structural = [s.segment for s in segs if s.structural]
    if structural:
        return _opt("B-2", "Residential Remodeling", "no", f"Structural work on load-bearing parts ({'; '.join(structural)})"
                    ": a B-2 may not contract structural changes to footings, foundations, load-bearing walls, "
                    "partitions or roof structures, and subcontracting does not cure it (B&P 7057.5(c)(2)). Use a B.",
                    [], [R_B2_STRUCT, R_B2])
    if len(groups) < min_n:
        return _opt("B-2", "Residential Remodeling", "no", f"{len(groups)} unrelated trade(s) found; a B-2 contract "
                    f"needs at least {min_n} (B&P 7057.5(b)(2)).", [], [R_B2_MIN, R_B2])
    conds, rules = ["Existing residential wood-frame structure only; no new construction."], [R_B2_MIN, R_B2]
    mep = sorted({c for s in segs if s.mep_system_work for c in s.classes if c in MEP_CLASSES}, key=_code_key)
    if mep:
        conds.append(f"Installing, replacing, substantially altering or extending {_names(mep)} systems needs that "
                     "license or a licensed subcontractor; a B-2 may only make minor alterations to replace fixtures "
                     "(B&P 7057.5(c)(3)).")
        rules.append(R_B2_MEP)
    for code in ("C-16", "C-22", "C-57"):
        if code in trades:
            conds.append(f"{code} work: the B-2 must hold {code} or subcontract it to a licensed {code} contractor "
                         "(B&P 7057.5(c)(1)).")
            rules.append(R_B2_RESTRICTED)
    return _opt("B-2", "Residential Remodeling", "conditional" if len(conds) > 1 else "yes",
                f"{len(groups)} unrelated trades in one contract ({_gnames(groups)}); B-2 needs at least {min_n}.",
                conds, rules)


def _a_option(trades, segs, project_type) -> ClassOption:
    civil = sorted({w for s in segs for w in s.civil_work})
    solar = "C-46" in trades
    if project_type == "civil_infrastructure" or civil:
        conds = []
        building = [t for t in trades if t not in A_ISH and t not in ("C-31", "C-46", "C-50", "C-51", "C-60")]
        if building:
            conds.append(f"Class A may not take building trades outside B&P 7056 fixed works ({_names(building)}) "
                         "(16 CCR 834(a)).")
        return _opt("A", "General Engineering", "conditional" if conds else "yes",
                    "Fixed works requiring specialized engineering knowledge" + (f" ({', '.join(civil)})" if civil else "")
                    + " are Class A work (B&P 7056).", conds, [R_A, R_834])
    if solar:
        building = [t for t in trades if t not in ("C-46", "C-10") and t not in A_ISH]
        conds = [f"Class A may not take the building trades in this scope ({_names(building)}) (16 CCR 834(a)): "
                 "separate contracts or a B prime."] if building else []
        return _opt("A", "General Engineering", "conditional" if conds else "yes", "An active solar energy system is a "
                    "fixed work within Class A (16 CCR 832.62(a)).", conds,
                    [R_SOLAR_AB, R_A] + ([R_834] if building else []))
    if trades and set(trades) <= A_ISH:
        return _opt("A", "General Engineering", "conditional", f"{_names(trades)} work is Class A work when it is "
                    "part of B&P 7056 fixed works (site work, roads, utilities, grading).",
                    ["Only in connection with fixed works listed in B&P 7056."], [R_A, R_834])
    return _opt("A", "General Engineering", "no", "Class A is limited to fixed works requiring specialized engineering "
                "knowledge (B&P 7056); this scope is building or specialty work.", [], [R_A, R_834])


def classification_for_scope(scope_of_work: str, project_type: ProjectType = "other",
                             contract_role: Literal["prime", "subcontract"] = "prime",
                             battery_kwh: Optional[float] = None,
                             asbestos_sq_ft: Optional[float] = None) -> ClassificationResult:
    if not (scope_of_work or "").strip():
        raise ValueError("scope_of_work must describe the work, e.g. 'replace water heater, add 2 outlets'")
    segs, unmatched, strong, remodel = _detect(scope_of_work)
    groups = _groups(segs)
    trades = sorted({c for g in groups for c in g}, key=_code_key)
    whole = (strong and (not trades or len(groups) >= 2)) or (project_type == "adu" and not trades)
    hard_no = whole and contract_role == "prime"         # a sub's contract covers only its own trade
    n_unrelated = len(_non_carpentry(groups))
    args = (segs, contract_role, project_type, battery_kwh, asbestos_sq_ft, hard_no)
    options = [_specialty_option(c, *args) for c in trades]
    if "C-10" not in trades and any(_covers("C-10", s) for s in segs):
        options.append(_specialty_option("C-10", *args))
    if project_type in ("adu", "manufactured_home") and "C-47" not in trades:
        options.append(_specialty_option("C-47", *args, standalone=True))
    if trades or whole or remodel or any(s.civil_work for s in segs):
        options += [_b_option(groups, segs, contract_role, project_type, whole, remodel),
                    _b2_option(groups, segs, project_type), _a_option(trades, segs, project_type)]
    notes = ["Keyword matching against 16 CCR 832 class scopes. CSLB decides which trades are 'unrelated' and what is "
             "incidental and supplemental case by case; confirm borderline scopes with CSLB.",
             "The class must be on an active license (B&P 7117.6)."]
    if any(len(g) > 1 for g in groups):
        notes.append("Classes listed together (" + "; ".join(_label(g) for g in groups if len(g) > 1) + ") can each do "
                     "that piece of work, so it counts as one trade.")
    if unmatched:
        notes.append(f"Not recognized: {', '.join(unmatched)}. If no listed class covers the work, it may be C-61 "
                     "Limited Specialty, whose endorsed scope is printed on the license (16 CCR 832.61).")
    text = _norm_text(scope_of_work)
    haz = bool(_has(text, ("hazardous substance", "contaminated soil", "remediation")))
    ust = bool(_has(text, ("underground storage tank",)))
    if haz:
        notes.append("Hazardous substance removal at a listed site also needs a qualifier who passed the HAZ "
                     "certification exam (B&P 7058.7).")
    if ust:
        notes.append("Installing or removing an underground storage tank needs the HAZ certification (B&P 7058.7(e)).")
    options.sort(key=lambda o: ({"yes": 0, "conditional": 1, "no": 2}[o.status], _code_key(o.code)))
    yes = [o.code for o in options if o.status == "yes"]
    cond = [o.code for o in options if o.status == "conditional"]
    if not options:
        answer = ("No trade recognized. Describe the work by trade (e.g. 'electrical', 'plumbing', 'roofing') or by "
                  "task (e.g. 'replace water heater').")
    else:
        answer = (f"Can take this contract as described: {', '.join(yes) or 'none without conditions'}."
                  + (f" With conditions: {', '.join(cond)}." if cond else ""))
    humans = ["A CSLB-licensed contractor whose active license holds the class; its qualifying individual must be "
              "qualified in that class."]
    if "C-22" in trades:
        humans.append("DOSH-registered asbestos abatement contractor; the owner must use a DOSH-certified asbestos "
                      "consultant for asbestos consulting or site surveillance (B&P 7180.5).")
    if "C-16" in trades:
        humans.append("C-16 fire protection contractor for the sprinkler or suppression system.")
    res = ClassificationResult(
        scope_of_work=scope_of_work, project_type=project_type, contract_role=contract_role, trades=segs,
        trade_classes=trades, trade_groups=groups, unrelated_trades_excluding_framing_carpentry=n_unrelated,
        options=options, can_perform=yes, can_perform_with_conditions=cond, unmatched_segments=unmatched,
        answer=answer, licensed_humans=humans, notes=notes)
    res.cite(R_CLASSES, R_830, R_7117_6, *[r for o in options for r in o.rule_ids])
    for flag, rid in (("C-22" in trades, "owner-must-hire-certified-asbestos-consultant"), (haz, R_HAZ),
                      (ust, R_UST), (bool(unmatched), R_C61)):
        if flag:
            res.cite(rid)
    return res


# ================================================================ bulk subcontractor check
class SubInput(BaseModel):
    license_number: str = Field("", description="CSLB license number")
    name: Optional[str] = Field(None, description="Company name as you know it (optional)")
    scope_of_work: Optional[str] = Field(None, description="What the sub will do, e.g. 'electrical rough and finish'")
    required_classification: Optional[str] = Field(None, description="Class the work needs, e.g. 'C-10'. If blank, "
                                                                     "it is inferred from scope_of_work")
    coi_expiry: Optional[str] = Field(None, description="Liability insurance certificate expiry (ISO or MM/DD/YYYY)")
    wc_required: bool = Field(True, description="True if the sub will have employees on your job (workers' comp "
                                                "needed). False only for an owner-operator working alone")
    project_type: ProjectType = Field("other", description="Used only when the class is inferred from scope")

    @field_validator("wc_required", mode="before")
    @classmethod
    def _bool(cls, v):
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("", "none"):
                return True
            if s in ("y", "yes", "true", "1", "t", "required"):
                return True
            if s in ("n", "no", "false", "0", "f", "not required"):
                return False
            raise ValueError(f"wc_required must be yes/no, got {v!r}")
        return True if v is None else v

    @field_validator("license_number", "scope_of_work", "required_classification", "coi_expiry", "name",
                     mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("project_type", mode="before")
    @classmethod
    def _pt(cls, v):
        return "other" if v in (None, "") else v


class SubRiskRow(BaseModel):
    row: int
    license_number: str
    business_name: str
    scope_of_work: Optional[str] = None
    license_status: str
    license_lookup: Literal["found", "not_in_partial_copy", "not_in_complete_cslb_file", "no_license_data_loaded",
                            "no_license_number"]
    data_source: str
    classifications: list[str] = Field(default_factory=list)
    required_classification: Optional[str] = None
    classification_source: Optional[Literal["given", "inferred_from_scope"]] = None
    classification_match: Optional[bool] = None
    acceptable_classifications: list[str] = Field(default_factory=list)
    license_expiration_date: Optional[str] = None
    license_expires_in_days: Optional[int] = None
    coi_expiry: Optional[str] = None
    coi_status: Literal["current", "expiring_soon", "expired", "missing"]
    workers_comp: str
    workers_comp_ok: bool
    contractor_bond_on_file: bool
    risk: Literal["do_not_use", "high", "medium", "low"]
    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    action: str


class BulkSubResult(core.Result):
    as_of: str
    total: int
    counts_by_risk: dict[str, int]
    rows: list[SubRiskRow]
    top_actions: list[str]
    license_data_official: bool = Field(description="True only if every row came from CSLB's published data")
    table_markdown: str
    notes: list[str] = Field(default_factory=list)


_HEADER_ALIASES = {"license": "license_number", "license_no": "license_number", "lic": "license_number",
                   "lic_no": "license_number", "cslb": "license_number", "cslb_license": "license_number",
                   "license_num": "license_number", "license_number_": "license_number",
                   "scope": "scope_of_work", "trade": "scope_of_work", "work": "scope_of_work",
                   "classification": "required_classification", "class": "required_classification",
                   "required_class": "required_classification", "license_class": "required_classification",
                   **{k: "coi_expiry" for k in ("coi", "coi_exp", "coi_expiration", "coi_expires", "coi_exp_date",
                                                "coi_expiration_date", "insurance_expiry", "insurance_expiration",
                                                "liability_insurance_expiry", "gl_expiry", "gl_expiration")},
                   **{k: "wc_required" for k in ("will_have_employees_on_site", "employees", "has_employees", "wc",
                                                 "workers_comp", "workers_comp_required", "wc_needed")},
                   "company": "name", "business_name": "name", "sub": "name", "subcontractor": "name"}


def parse_sub_csv(csv_text: str) -> list[SubInput]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        raise ValueError("csv_text has no header row")
    keys = {}
    for h in reader.fieldnames:
        k = re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")
        keys[h] = _HEADER_ALIASES.get(k, k)
    if "license_number" not in keys.values():
        raise ValueError("csv_text needs a license_number column")
    fields = set(SubInput.model_fields)
    return [SubInput(**{keys[h]: v for h, v in row.items() if h in keys and keys[h] in fields and v is not None})
            for row in reader if any((v or "").strip() for v in row.values() if isinstance(v, str))]


def _check_one(i: int, s: SubInput, as_of: Date, cite: set) -> SubRiskRow:
    if not s.license_number:
        return SubRiskRow(row=i, license_number="", business_name=s.name or "unknown", scope_of_work=s.scope_of_work,
                          license_status="unknown", license_lookup="no_license_number", data_source="none",
                          coi_expiry=s.coi_expiry, coi_status="missing", workers_comp="unknown",
                          workers_comp_ok=False, contractor_bond_on_file=False, risk="high",
                          problems=["No license number given"],
                          action="Get the sub's CSLB license number and check it before award.")
    # The insurance date is evaluated here against as_of; the far-future value keeps check_subcontractor neutral.
    sub = logic.check_subcontractor(s.license_number, s.required_classification, "9999-12-31", s.wc_required)
    lic = sub.license
    problems, warnings = list(sub.problems), []
    lookup = "found" if lic.found else {"CSLB master file (partial copy)": "not_in_partial_copy",
                                        "CSLB master file": "not_in_complete_cslb_file"}.get(lic.data_source,
                                                                                            "no_license_data_loaded")
    if not lic.found:      # bond/WC flags are meaningless without a record; say only what the lookup means
        problems = [{
            "not_in_partial_copy": "Not in our partial copy of CSLB data. This does NOT mean the sub is unlicensed: "
                                   "check the number on the CSLB site",
            "no_license_data_loaded": "No CSLB data loaded and the number is not in the demo/CSV records. This does "
                                      "NOT mean the sub is unlicensed: check the number on the CSLB site",
            "not_in_complete_cslb_file": "Not in CSLB's current master file (which omits cancelled, revoked and "
                                         "long-expired licenses). Treat as not licensed until confirmed on the CSLB "
                                         "site"}[lookup]]
    blocking = (sub.overall_risk == "do_not_use" and lic.found) or lookup == "not_in_complete_cslb_file"
    hold = lookup in ("not_in_partial_copy", "no_license_data_loaded")
    exp_days = None
    exp = _day(lic.expiration_date, "expiration_date") if lic.expiration_date else None
    if exp:
        exp_days = (exp - as_of).days
        if exp_days < 0 and lic.status == "active":
            problems.append(f"License expired {exp.isoformat()} (as of {as_of.isoformat()})")
            blocking = True
        elif 0 <= exp_days <= EXPIRING_SOON_DAYS:
            warnings.append(f"License expires {exp.isoformat()} ({exp_days} days): confirm renewal before it lapses")
    # Classification: given, else inferred from scope.
    req, src, match, acceptable = s.required_classification, None, sub.classification_match if lic.found else None, []
    needs: list[str] = [req] if req else []
    if req:
        src, acceptable = "given", [req]
        cite.add(R_7117_6)
    elif s.scope_of_work and lic.found:
        cl = classification_for_scope(s.scope_of_work, s.project_type, "subcontract")
        cite.update(c.rule_id for c in cl.citations)
        held = {_code(c) for c in lic.classifications}
        yes = [c for c in cl.can_perform]
        cond = [c for c in cl.can_perform_with_conditions]
        if cl.trade_classes:
            src, acceptable = "inferred_from_scope", yes + cond
            if held & {_code(c) for c in yes}:
                match = True
            elif held & {_code(c) for c in cond}:
                match = None
                ok = next(o for o in cl.options if _code(o.code) in held and o.status == "conditional")
                warnings.append(f"{ok.code} can do this scope only if: {' '.join(ok.conditions)}")
            else:
                match, needs = False, yes or cond
                problems.append(f"License classes ({', '.join(lic.classifications) or 'none'}) do not appear to cover "
                                f"'{s.scope_of_work}' (needs {' or '.join(needs)})")
                if any(c.startswith("D") for c in held):
                    warnings.append("D-codes are C-61 limited specialty subcategories; check the endorsed scope on "
                                    "the CSLB license record")
        else:
            warnings.append("Could not infer the class from the scope; give required_classification")
    # Insurance certificate vs as_of.
    coi = _day(s.coi_expiry, "coi_expiry") if s.coi_expiry else None
    if not coi:
        coi_status = "missing"
        problems.append("No liability insurance certificate provided")
    elif coi <= as_of:
        coi_status = "expired"
        problems.append(f"Liability insurance expired {coi.isoformat()}")
    elif (coi - as_of).days <= EXPIRING_SOON_DAYS:
        coi_status = "expiring_soon"
        warnings.append(f"Liability insurance expires {coi.isoformat()} ({(coi - as_of).days} days): request the "
                        "renewal certificate")
    else:
        coi_status = "current"
    # Workers' comp exemption ends 2028-01-01 for everyone but no-employee joint ventures.
    wc_ok = sub.workers_comp_ok
    if lic.found and lic.workers_comp == "exempt":
        change = _day(P(R_WC_2028, "operative_date"))
        cite.add(R_WC_2028)
        if as_of >= change:
            problems.append(f"Workers' comp exemption is no longer valid from {change.isoformat()}: every active "
                            "licensee needs a certificate (B&P 7125, current version)")
            wc_ok = False
        else:
            warnings.append(f"Relies on a workers' comp exemption, which ends {change.isoformat()} "
                            f"({(change - as_of).days} days)")
    if not lic.contractor_bond_on_file and lic.found:
        cite.add(R_BOND)
    if not wc_ok and lic.found:
        cite.add(R_WC_EXEMPT)
    if blocking:
        risk = "do_not_use"
    elif hold or not wc_ok or sub.overall_risk == "high" or len(problems) >= 2:
        risk = "high"
    elif problems:
        risk = "medium"
    else:
        risk = "low"
    if risk == "do_not_use":
        action = ("Do not use. " + (sub.recommended_action.split(". ", 1)[-1] if sub.overall_risk == "do_not_use"
                                    else "The license is missing, expired or wrong for this work."))
    elif hold:
        action = "Hold. Confirm the license on the CSLB site before award (missing from our data is not unlicensed)."
    elif not wc_ok:
        action = "Hold. Get a workers' comp certificate before they start."
    elif match is False:
        action = (("Hold. " if risk == "high" else "Before award: ") + "confirm the license covers this scope"
                  + (f", or use a sub holding {' or '.join(needs[:3])}." if needs else "."))
    elif risk in ("high", "medium"):
        action = ("Hold. " if risk == "high" else "") + "Fix before award: " + "; ".join(problems[:3]) + "."
    else:
        action = "Cleared for the bid list, subject to verifying the source records."
    return SubRiskRow(
        row=i, license_number=lic.license_number, business_name=lic.business_name if lic.found else (s.name or "unknown"),
        scope_of_work=s.scope_of_work, license_status=lic.status, license_lookup=lookup, data_source=lic.data_source,
        classifications=lic.classifications, required_classification=req, classification_source=src,
        classification_match=match, acceptable_classifications=acceptable, license_expiration_date=lic.expiration_date,
        license_expires_in_days=exp_days, coi_expiry=coi.isoformat() if coi else s.coi_expiry, coi_status=coi_status,
        workers_comp=lic.workers_comp, workers_comp_ok=wc_ok, contractor_bond_on_file=lic.contractor_bond_on_file,
        risk=risk, problems=problems, warnings=warnings, action=action)


def _who(r: SubRiskRow) -> str:
    return f"#{r.license_number or '?'} {r.business_name}".strip()


def _top_actions(rows: list[SubRiskRow]) -> list[str]:
    def group(label, pick):
        hit = [r for r in rows if pick(r)]
        if hit:
            names = "; ".join(_who(r) for r in hit[:8]) + (f"; +{len(hit) - 8} more" if len(hit) > 8 else "")
            out.append(f"{label} ({len(hit)}): {names}")
    out: list[str] = []
    group("Do not use or pay until fixed (license missing, inactive, expired or wrong class)",
          lambda r: r.risk == "do_not_use")
    group("Confirm on the CSLB site before award (not in our data; NOT a finding of unlicensed)",
          lambda r: r.license_lookup in ("not_in_partial_copy", "no_license_data_loaded"))
    group("Get the license number", lambda r: r.license_lookup == "no_license_number")
    group("Get a workers' comp certificate before they start",
          lambda r: r.license_lookup == "found" and not r.workers_comp_ok and r.risk != "do_not_use")
    group("Collect a current certificate of liability insurance",
          lambda r: r.coi_status in ("missing", "expired") and r.risk != "do_not_use")
    group("Check the license class against the scope", lambda r: r.classification_match is False and r.risk != "do_not_use")
    group(f"Calendar renewals expiring within {EXPIRING_SOON_DAYS} days (license or insurance)",
          lambda r: r.coi_status == "expiring_soon" or (r.license_expires_in_days is not None
                                                        and 0 <= r.license_expires_in_days <= EXPIRING_SOON_DAYS))
    group(f"Budget for workers' comp: their exemption ends {P(R_WC_2028, 'operative_date')}",
          lambda r: any("exemption, which ends" in w for w in r.warnings))
    return out or ["No action needed: every sub is low risk."]


def _markdown(rows: list[SubRiskRow]) -> str:
    head = "| # | License | Business | Status | Classes | COI | WC | Risk | Action |\n|---|---|---|---|---|---|---|---|---|"
    lines = [f"| {r.row} | {r.license_number} | {r.business_name} | {r.license_status} | "
             f"{', '.join(r.classifications) or '-'} | {r.coi_status} | {r.workers_comp} | {r.risk} | {r.action} |"
             for r in rows]
    return "\n".join([head, *lines])


def bulk_check_subcontractors(csv_text: Optional[str] = None, rows: Optional[list] = None, as_of=None) -> BulkSubResult:
    if bool(csv_text and csv_text.strip()) == bool(rows):
        raise ValueError("Give exactly one of csv_text or rows")
    subs = parse_sub_csv(csv_text) if csv_text else [r if isinstance(r, SubInput) else SubInput.model_validate(r)
                                                      for r in rows]
    if not subs:
        raise ValueError("No subcontractor rows found")
    day = _as_of(as_of)
    cite: set[str] = {R_7118, R_7118_PEN}
    out = [_check_one(i, s, day, cite) for i, s in enumerate(subs, 1)]
    out.sort(key=lambda r: (RISK_ORDER[r.risk], r.row))
    counts = {k: sum(r.risk == k for r in out) for k in RISK_ORDER}
    official = all(r.data_source.startswith("CSLB master file") for r in out)
    lo, hi = P(R_7118_PEN, "min_usd"), P(R_7118_PEN, "max_usd")
    notes = [f"Contracting with an unlicensed contractor is cause for discipline (B&P 7118); CSLB citations run "
             f"{_usd(lo)}-{_usd(hi)}. Check license status at award and again before each payment.",
             "License status is CSLB's status in the data snapshot; as_of drives the expiration, insurance and "
             "2028 workers' comp checks."]
    dupes = sorted({r.license_number for r in out if r.license_number and
                    sum(x.license_number == r.license_number for x in out) > 1})
    if dupes:
        notes.append(f"Listed more than once: {', '.join(dupes)}.")
    if any(r.data_source == "demo fixtures" for r in out):
        notes.append("Some rows use demo license records, not CSLB data.")
    res = BulkSubResult(as_of=day.isoformat(), total=len(out), counts_by_risk=counts, rows=out,
                        top_actions=_top_actions(out), license_data_official=official, table_markdown=_markdown(out),
                        notes=notes)
    res.cite(*sorted(cite))
    res.verified = res.verified and official
    return res


# ================================================================ company license obligations
class ChangeEvent(BaseModel):
    change: ChangeType = Field(description="What changed on the license record")
    date: Date = Field(description="Date of the change (ISO)")

    @field_validator("date", mode="before")
    @classmethod
    def _d(cls, v):
        return _day(v, "date")


class Obligation(BaseModel):
    item: str
    required: bool
    amount_usd: Optional[float] = None
    math: str = ""
    detail: str
    rule_ids: list[str]


class Deadline(BaseModel):
    item: str
    due_date: str
    days_remaining: int
    status: Literal["overdue", "due_soon", "upcoming"]
    consequence: str
    rule_ids: list[str]


class WorkersCompStatus(BaseModel):
    requirement: Literal["certificate_required", "exemption_allowed", "not_required_while_inactive"]
    reasons: list[str]
    mandatory_classes_held: list[str] = Field(default_factory=list)
    exemption_ends_on: str
    days_until_exemption_ends: int
    actions: list[str] = Field(default_factory=list)
    rule_ids: list[str]


class RenewalInfo(BaseModel):
    expiration_date: Optional[str] = None
    days_until_expiration: Optional[int] = None
    status: Literal["current", "expired_retroactive_window", "expired_gap", "expired_over_5_years", "unknown"]
    renewal_fee_usd: float
    c10_surcharge_usd: float = 0.0
    on_time_total_usd: float
    delinquency_fee_usd: float
    late_total_usd: float
    renew_by: Optional[str] = Field(None, description="Postmark or hand-deliver the renewal by this date")
    retroactive_renewal_deadline: Optional[str] = None
    renewal_window_ends: Optional[str] = None
    next_expiration_if_renewed: Optional[str] = None
    math: list[str] = Field(default_factory=list)
    rule_ids: list[str]


class LicenseObligations(core.Result):
    as_of: str
    entity_type: str
    license_status: str
    bonds: list[Obligation]
    total_bonds_usd: float
    insurance: list[Obligation]
    workers_comp: WorkersCompStatus
    renewal: RenewalInfo
    deadlines: list[Deadline]
    licensed_humans: list[str]
    actions: list[str]
    notes: list[str] = Field(default_factory=list)


def _deadline(item, due: Date, as_of: Date, consequence, rule_ids) -> Deadline:
    days = (due - as_of).days
    status = "overdue" if days < 0 else ("due_soon" if days <= EXPIRING_SOON_DAYS else "upcoming")
    return Deadline(item=item, due_date=due.isoformat(), days_remaining=days, status=status, consequence=consequence,
                    rule_ids=rule_ids)


def _wc_status(entity, employees, classes, active, rme, his, as_of, cite) -> WorkersCompStatus:
    change = _day(P(R_WC_2028, "operative_date"))
    mandatory = P(R_WC_EXEMPT, "classifications_always_require_wc")
    held = [c for c in mandatory if _code(c) in {_code(x) for x in classes}]
    base = dict(exemption_ends_on=change.isoformat(), days_until_exemption_ends=(change - as_of).days,
                mandatory_classes_held=held)
    if not active:
        cite.add(R_WC_INACTIVE)
        return WorkersCompStatus(requirement="not_required_while_inactive", reasons=[
            "No workers' comp certificate or exemption is required while the license is inactive (B&P 7125)."],
            actions=["Before reactivating, file a workers' comp certificate (or a valid exemption)."],
            rule_ids=[R_WC_INACTIVE], **base)
    reasons, rules, actions = [], [R_WC_EXEMPT, R_WC_2028], []
    if as_of >= change:
        reasons.append(f"From {change.isoformat()} every active licensee must file a workers' comp or self-insurance "
                       "certificate; only a joint venture with no employees may still file an exemption.")
    if employees > 0:
        reasons.append(f"{employees} employee(s): any employer, even with one part-time employee, must carry "
                       "workers' comp (Labor Code 3700).")
        rules.append(R_WC_ONE)
    if held:
        reasons.append(f"{', '.join(held)} must carry workers' comp even with no employees (B&P 7125).")
    if rme:
        reasons.append("The license is qualified by an RME, who is an employee, so CSLB will not accept an exemption.")
        rules.append(R_WC_RME)
    if his:
        reasons.append("Employing home improvement salespersons rules out the exemption.")
        rules.append(R_WC_RME)
    cite.update(rules)
    if reasons:
        actions.append("Keep a current Certificate of Workers' Compensation Insurance (filed by your insurer) or a "
                       "DIR self-insurance certificate on file with CSLB in the license's business name.")
        actions.append("If coverage lapses, the license is suspended automatically as of the lapse date (B&P 7125.2).")
        cite.add(R_WC_LAPSE)
        return WorkersCompStatus(requirement="certificate_required", reasons=reasons, actions=actions,
                                 rule_ids=rules + [R_WC_LAPSE], **base)
    hire_days = P(R_WC_HIRE, "days_to_file_proof_after_hire")
    cite.add(R_WC_HIRE)
    actions += [f"File the exemption statement on CSLB's form. It becomes invalid the day you hire anyone: coverage "
                f"must start that day and CSLB must receive proof within {hire_days} days of the hire.",
                f"Budget for a policy: the exemption ends {change.isoformat()} ({(change - as_of).days} days)."]
    if entity in ("corporation", "llc"):
        actions.append("Corporate officers and LLC members can be employees under the Labor Code; confirm with your "
                       "workers' comp broker before claiming no employees.")
    return WorkersCompStatus(requirement="exemption_allowed", reasons=[
        f"No employees and no mandatory class: the no-employee exemption is available until {change.isoformat()} "
        "(B&P 7125(b)-(c))."], actions=actions, rule_ids=rules + [R_WC_HIRE], **base)


def _renewal(entity, active, classes, exp: Optional[Date], as_of: Date, cite) -> RenewalInfo:
    who = "individual" if entity == "sole_owner" else "entity"
    fee = float(P(R_FEES, f"{who}_{'active' if active else 'inactive'}_usd"))
    c10 = float(P(R_FEES, "c10_active_renewal_surcharge_usd")) if active and "C10" in {_code(c) for c in classes} else 0.0
    pct = P(R_DELINQ, "delinquency_percent_of_renewal_fee")
    delinquency = _money(fee * pct / 100)
    math = [f"Renewal fee ({'sole owner' if who == 'individual' else entity}, {'active' if active else 'inactive'}): "
            f"{_usd(fee)}" + (f" + C-10 surcharge {_usd(c10)}" if c10 else "") + f" = {_usd(fee + c10)} on time",
            f"Late: + delinquency fee {pct}% x {_usd(fee)} = {_usd(delinquency)} -> {_usd(fee + c10 + delinquency)}"]
    rules = [R_FEES, R_DELINQ, R_TERM, R_POSTMARK]
    out = dict(renewal_fee_usd=fee, c10_surcharge_usd=c10, on_time_total_usd=_money(fee + c10),
               delinquency_fee_usd=delinquency, late_total_usd=_money(fee + c10 + delinquency), math=math)
    if not exp:
        cite.update(rules)
        return RenewalInfo(status="unknown", rule_ids=rules, **out)
    term = P(R_TERM, "term_years")
    retro, window = P(R_RETRO, "window_days"), P(R_5YR, "renewal_window_years_after_expiration")
    days = (exp - as_of).days
    nxt = _add_years(exp, term)
    while nxt <= as_of:
        nxt = _add_years(nxt, term)
    if days >= 0:
        status = "current"
    elif -days <= retro:
        status = "expired_retroactive_window"
    elif as_of <= _add_years(exp, window):
        status = "expired_gap"
    else:
        status = "expired_over_5_years"
    math.append(f"Expires {exp.isoformat()}; the next term runs {term} years from that date to "
                f"{_add_years(exp, term).isoformat()} (B&P 7140); a late renewal does not restart the {term}-year "
                "clock (B&P 7141(c)).")
    rules += [R_RETRO, R_5YR, R_GRACE, "late-renewal-keeps-original-cycle"]
    cite.update(rules)
    return RenewalInfo(expiration_date=exp.isoformat(), days_until_expiration=days, status=status,
                       renew_by=exp.isoformat(), retroactive_renewal_deadline=(exp + timedelta(days=retro)).isoformat(),
                       renewal_window_ends=_add_years(exp, window).isoformat(), next_expiration_if_renewed=nxt.isoformat(),
                       rule_ids=rules, **out)


_CHANGE_TEXT = {
    "business_address": "Business address change", "mailing_address": "Mailing address change",
    "business_name": "Business name change", "personnel": "Personnel of record change (officer, member, partner)",
    "qualifier_bond_exemption": "Qualifier bond exemption change (e.g. ownership fell below 10%)",
    "multiple_license_exemption": "Multiple-license qualifier exemption change",
    "dissolution_or_merger": "Dissolution, merger or surrender of the right to do business",
    "criminal_conviction": "Felony or contracting-related conviction (from when you learned of it)",
}


def _change_deadlines(changes: list[ChangeEvent], entity: str, as_of: Date, cite) -> tuple[list[Deadline], list[str]]:
    out, notes = [], []
    lo, hi = P(R_NOTIFY_PEN, "min_usd"), P(R_NOTIFY_PEN, "max_usd")
    late = (f"Late notice: the change takes effect only when CSLB receives it, and is grounds for discipline "
            f"(citation {_usd(lo)}-{_usd(hi)}).")
    for ch in changes:
        d = ch.date
        if ch.change == "qualifier_left":
            n, r = P(R_Q_NOTIFY, "deadline_days"), P(R_Q_REPLACE, "deadline_days")
            fee = P(R_APP_FEES, "replace_qualifier_7068_2_usd")
            out.append(_deadline("Notify CSLB in writing that the qualifier left", d + timedelta(days=n), as_of,
                                 "If late, suspension/class removal and qualifier removal are effective the day CSLB "
                                 "receives notice; grounds for discipline.", [R_Q_NOTIFY]))
            out.append(_deadline(f"File the application to replace the qualifier (fee {_usd(fee)})",
                                 d + timedelta(days=r), as_of, "License automatically suspended (or the class removed) "
                                 f"at the end of the {r} days.", [R_Q_REPLACE, R_APP_FEES]))
            notes.append(f"One {P(R_Q_EXT, 'extension_days')}-day extension is possible only if you dispute the "
                         "departure date, the qualifier died, or an agency processing delay occurred, and the petition "
                         f"arrives within {P(R_Q_EXT, 'extension_petition_within_days')} days with a replacement "
                         "application on file (B&P 7068.2(e)).")
            cite.update([R_Q_NOTIFY, R_Q_REPLACE, R_APP_FEES, R_Q_EXT])
            continue
        if ch.change == "mailing_address":
            days, rules = P(R_MAIL, "notify_within_days"), [R_MAIL, R_NOTIFY]
            cons = (f"B&P 136 requires mailing address changes within {days} days (citation and fine); B&P 7083 "
                    f"allows {P(R_NOTIFY, 'deadline_days')} for other record changes. Use the earlier date.")
        elif ch.change == "dissolution_or_merger":
            days, rules = P(R_DISSOLVE, "notice_deadline_days"), [R_DISSOLVE]
            cons = ("The corporation/LLC license is cancelled; if notice is late, cancellation is effective when CSLB "
                    "receives it, and it is grounds for discipline.")
            if entity not in ("corporation", "llc"):
                notes.append("Dissolution/merger notice applies to corporations and LLCs (B&P 7076(h)-(i)); "
                             "partnership changes follow B&P 7076 partner rules.")
        elif ch.change == "criminal_conviction":
            days, rules = P(R_CONVICT, "days"), [R_CONVICT]
            cons = "Report in writing to CSLB; failure is grounds for discipline."
        else:
            days, rules, cons = P(R_NOTIFY, "deadline_days"), [R_NOTIFY, R_NOTIFY_PEN], late
            if ch.change == "business_name":
                cons += f" Name change fee {_usd(P(R_NAME_FEE, 'business_name_change_usd'))}."
                rules.append(R_NAME_FEE)
            if ch.change == "personnel":
                cons += (f" Adding personnel: application fee {_usd(P(R_APP_FEES, 'add_non_qualifying_personnel_usd'))}"
                         " and fingerprints for each new person.")
                rules += [R_APP_FEES, R_FINGERPRINTS]
        out.append(_deadline(f"Notify CSLB: {_CHANGE_TEXT[ch.change]} on {d.isoformat()}", d + timedelta(days=days),
                             as_of, cons, rules))
        cite.update(rules)
    return out, notes


def company_license_obligations(
        entity_type: EntityType, employee_count: int = 0, member_count_for_llc: Optional[int] = None,
        classifications: Optional[list[str]] = None, license_expiration_date=None, has_qualifier: bool = True,
        qualifier_is_rme_or_rmo: bool = False, as_of=None, *, license_status: Literal["active", "inactive"] = "active",
        qualifier_is_employee_rme: bool = False, qualifier_ownership_percent: Optional[float] = None,
        employs_home_improvement_salespersons: bool = False,
        changes: Optional[list] = None) -> LicenseObligations:
    if entity_type not in ("sole_owner", "partnership", "corporation", "llc"):
        raise ValueError("entity_type must be sole_owner, partnership, corporation or llc")
    if employee_count < 0 or (member_count_for_llc is not None and member_count_for_llc < 0):
        raise ValueError("counts cannot be negative")
    day = _as_of(as_of)
    classes = [c for c in (classifications or []) if c and c.strip()]
    active = license_status == "active"
    exp = _day(license_expiration_date, "license_expiration_date")
    evs = [c if isinstance(c, ChangeEvent) else ChangeEvent.model_validate(c) for c in (changes or [])]
    cite: set[str] = set()
    notes, actions = [], []
    # Who qualifies the license. A sole owner or partnership that is not self-qualified uses an RME.
    rme = qualifier_is_employee_rme or (qualifier_is_rme_or_rmo and entity_type in ("sole_owner", "partnership"))
    not_owner = qualifier_is_rme_or_rmo or qualifier_is_employee_rme
    if entity_type in ("corporation", "llc") and has_qualifier and not not_owner:
        not_owner = True
        notes.append("Corporations and LLCs qualify through an RMO, RME or LLC responsible managing member/manager, "
                     "not an owner; treated as an RMO.")

    # ---- bonds
    bonds = []
    amt = float(P(R_BOND, "amount_usd"))
    bonds.append(Obligation(item="Contractor's bond", required=active, amount_usd=amt if active else 0.0,
                            math=f"{_usd(amt)} flat" if active else "not required while inactive",
                            detail="Must be on file to renew or keep the license; not needed while inactive. If the "
                                   f"surety cancels, the license is suspended {P(R_BOND_CANCEL, 'days')} days after "
                                   "CSLB receives the notice unless a replacement bond is filed.",
                            rule_ids=[R_BOND, R_BOND_CANCEL]))
    cite.update([R_BOND, R_BOND_CANCEL])
    q_amt = float(P(R_QBOND, "amount_usd"))
    pct = P(R_QBOND_EXEMPT, "corporation_min_voting_stock_percent" if entity_type == "corporation"
            else "llc_min_membership_interest_percent")
    q_required, q_math = False, "not required: the qualifier is the owner or a general partner"
    if active and has_qualifier and not_owner:
        q_required, q_math = True, f"{_usd(q_amt)}, separate from the contractor's bond"
        if not rme and entity_type in ("corporation", "llc"):
            if qualifier_ownership_percent is not None and qualifier_ownership_percent >= pct:
                q_required = False
                q_math = (f"exempt: qualifier owns {qualifier_ownership_percent:g}% (>= {pct}%) and must certify it on "
                          "CSLB's Bond of Qualifying Individual Exemption Certification")
            elif qualifier_ownership_percent is None:
                q_math += f" unless the qualifier owns {pct}% or more and certifies it (give qualifier_ownership_percent)"
        cite.add(R_QBOND_EXEMPT)
    elif not active:
        q_math = "not required while inactive"
    bonds.append(Obligation(item="Bond of qualifying individual", required=q_required,
                            amount_usd=q_amt if q_required else 0.0, math=q_math,
                            detail="Required when the qualifier is neither the sole owner, a general partner nor a joint "
                                   "licensee. Each RME/RMO qualifying the license needs one. An RME can never use the "
                                   "10% ownership exemption.", rule_ids=[R_QBOND, R_QBOND_EXEMPT]))
    cite.add(R_QBOND)
    if entity_type == "llc":
        llc_amt = float(P(R_LLC_BOND, "amount_usd"))
        bonds.append(Obligation(item="LLC employee/worker bond", required=active,
                                amount_usd=llc_amt if active else 0.0,
                                math=f"{_usd(llc_amt)} in addition to the {_usd(amt)} contractor's bond" if active
                                else "not required while inactive",
                                detail="Protects workers against unpaid wages, interest and fringe benefits.",
                                rule_ids=[R_LLC_BOND]))
        cite.add(R_LLC_BOND)
    if not active:
        cite.add(R_INACTIVE)
    total_bonds = sum(b.amount_usd or 0 for b in bonds)

    # ---- insurance
    insurance = []
    if entity_type == "llc":
        base, thr = P(R_LLC_INS, "base_aggregate_usd"), P(R_LLC_INS, "personnel_threshold")
        per, cap = P(R_LLC_INS, "additional_per_person_usd"), P(R_LLC_INS, "max_aggregate_usd")
        n = member_count_for_llc
        extra = max(0, (n or 0) - thr)
        req = min(cap, base + per * extra)
        math = (f"{_usd(base)} for up to {thr} personnel of record"
                + (f" + {extra} more x {_usd(per)} = {_usd(base + per * extra)}" if extra else "")
                + (f", capped at {_usd(cap)}" if base + per * extra > cap else "") + " aggregate per policy period")
        detail = ("Liability insurance for the LLC's contracting work, in addition to its bonds; file a Certificate of "
                  "Liability Insurance with CSLB. If claims deplete the limit, restore it by the next policy period "
                  "or the license is suspended.")
        if n is None:
            detail += f" member_count_for_llc not given: assumed {thr} or fewer personnel of record."
        elif n > thr:
            literal = min(cap, base + per * n)
            detail += (f" CSLB reads the statute as {_usd(per)} per person above {thr}; the literal text ('for each "
                       f"person listed') would require {_usd(literal)}. Ask your broker to confirm which CSLB applies.")
        insurance.append(Obligation(item="LLC liability insurance (aggregate limit)", required=active,
                                    amount_usd=req if active else 0.0, math=math if active else "not required while inactive",
                                    detail=detail, rule_ids=[R_LLC_INS, "llc-insurance-reinstate-limit"]))
        cite.update([R_LLC_INS, "llc-insurance-reinstate-limit"])
    else:
        insurance.append(Obligation(item="Commercial general liability (CGL)", required=False, math="not required by "
                                    "license law", detail="License law requires CGL only for LLCs. Home improvement "
                                    "contracts must still say whether you carry CGL, and owners/GCs usually require it "
                                    "by contract.", rule_ids=[R_CGL, R_HIC_CGL]))
        cite.update([R_CGL, R_HIC_CGL])

    # ---- workers' comp
    wc = _wc_status(entity_type, employee_count, classes, active, rme, employs_home_improvement_salespersons, day, cite)
    if any(_code(c) == "C49" for c in classes):
        notes.append("C-49 Tree and Palm is not named in the B&P 7125 mandatory list (which names D-49 Tree Service); "
                     "confirm with CSLB whether C-49 must carry workers' comp without employees.")

    # ---- renewal
    renewal = _renewal(entity_type, active, classes, exp, day, cite)
    deadlines: list[Deadline] = []
    if exp:
        if renewal.status == "current":
            deadlines.append(_deadline("Renew the license (postmark or hand-deliver application and fee)", exp, day,
                                       f"After this date the renewal is delinquent: + {_usd(renewal.delinquency_fee_usd)}"
                                       " and the license shows expired; contracting while expired is unlicensed.",
                                       [R_POSTMARK, R_FEES, R_DELINQ]))
        elif renewal.status == "expired_retroactive_window":
            deadlines.append(_deadline("Late renewal with no break in licensure (renewal + delinquency fee)",
                                       _day(renewal.retroactive_renewal_deadline), day,
                                       "After this date renewal is effective only on filing; you are unlicensed for the "
                                       "gap.", [R_RETRO, R_DELINQ]))
        elif renewal.status == "expired_gap":
            deadlines.append(_deadline("Last day to renew the expired license", _day(renewal.renewal_window_ends), day,
                                       f"After {P(R_5YR, 'renewal_window_years_after_expiration')} years you must "
                                       "apply for a new license and meet all original-license "
                                       "requirements. Until renewed you are unlicensed.", [R_5YR]))
            actions.append("The license is expired past the 90-day retroactive window: stop contracting until the "
                           "renewal is filed; work in the gap is unlicensed.")
        else:
            actions.append(f"The license expired more than {P('not-renewed-5-years-new-license', 'years_after_expiration')}"
                           " years ago: apply for a new license (B&P 7141(d)).")
            cite.add("not-renewed-5-years-new-license")
    if active and wc.requirement == "exemption_allowed" and day < _day(wc.exemption_ends_on):
        deadlines.append(_deadline("Workers' comp exemption ends: have a policy and certificate on file",
                                   _day(wc.exemption_ends_on), day, "Without a certificate an active license is "
                                   "suspended.", [R_WC_2028]))

    # ---- qualifier and record changes
    ch_deadlines, ch_notes = _change_deadlines(evs, entity_type, day, cite)
    deadlines += ch_deadlines
    notes += ch_notes
    if not has_qualifier and not any(e.change == "qualifier_left" for e in evs):
        actions.insert(0, "No qualifier: the license must be qualified by an owner/general partner, RMO or RME. If your "
                          f"qualifier left, notify CSLB and file a replacement within {P(R_Q_REPLACE, 'deadline_days')} "
                          "days of the departure date "
                          "(add a qualifier_left change with that date for exact deadlines).")
        cite.update([R_Q_NOTIFY, R_Q_REPLACE])
    if rme and has_qualifier:
        hrs, pct80 = P(R_RME_HOURS, "min_hours_per_week"), P(R_RME_HOURS, "alt_percent_of_business_hours")
        notes.append(f"An RME must be a permanent employee actively engaged {hrs} hours a week, or {pct80}% of the "
                     "business's weekly operating hours if less (B&P 7068(c)).")
        cite.add(R_RME_HOURS)
    if entity_type in ("corporation", "llc") and active:
        notes.append(f"Stay in good standing with the Secretary of State and FTB: CSLB suspends the license "
                     f"{P(R_SOS, 'suspension_days_after_notice')} days after notice if the entity is not.")
        cite.add(R_SOS)
        if entity_type == "llc":
            notes.append(f"While an LLC license is suspended for that reason, each officer, manager and member can be "
                         f"personally liable up to {_usd(P(R_LLC_PERSONAL, 'personal_liability_cap_per_person_usd'))} "
                         "for third-party damages.")
            cite.add(R_LLC_PERSONAL)
    if wc.requirement == "certificate_required" and active:
        actions.append("Confirm CSLB shows a current workers' comp certificate in the license's exact business name.")
    if any(b.required for b in bonds):
        actions.append(f"Keep {_usd(total_bonds)} of bonds on file: " + ", ".join(
            f"{b.item} {_usd(b.amount_usd)}" for b in bonds if b.required) + ". Order replacements about "
                       f"{P(R_BOND_CANCEL, 'recommended_lead_time_new_bond_weeks')} weeks "
                       "before a bond's cancellation or expiry date.")
    deadlines.sort(key=lambda d: d.due_date)
    humans = ["Qualifying individual (owner, general partner, RMO, RME or LLC responsible managing member/manager) "
              "who passed the CSLB exam for each class and is responsible for supervising the work."]
    if active:
        humans.append("Admitted surety (through a bond agent) for each bond; licensed California workers' comp "
                      "insurer for the WC certificate" + ("; admitted or surplus-line insurer for the LLC policy."
                                                          if entity_type == "llc" else "."))
    res = LicenseObligations(as_of=day.isoformat(), entity_type=entity_type, license_status=license_status,
                             bonds=bonds, total_bonds_usd=_money(total_bonds), insurance=insurance, workers_comp=wc,
                             renewal=renewal, deadlines=deadlines, licensed_humans=humans, actions=actions, notes=notes)
    return res.cite(*sorted(cite))


# ================================================================ unlicensed contracting exposure
class ExemptionTest(BaseModel):
    test: str
    passed: Optional[bool] = Field(description="True/False, or null when the input was not given")
    detail: str
    rule_ids: list[str]


class Exposure(BaseModel):
    item: str
    kind: Literal["civil", "administrative", "criminal", "license"]
    low_usd: float
    high_usd: float
    math: str
    rule_ids: list[str]


class UnlicensedExposureResult(core.Result):
    as_of: str
    perspective: str
    contract_price_usd: float
    amount_paid_usd: Optional[float]
    scope: str
    has_license: bool
    license_required: Optional[bool] = Field(description="null = depends on an input not given (e.g. permit)")
    minor_work_exemption: Literal["applies", "does_not_apply", "depends", "not_applicable_licensed"]
    exemption_tests: list[ExemptionTest]
    exposure: list[Exposure]
    total_low_usd: float
    total_high_usd: float
    other_consequences: list[str]
    prosecution_deadline: Optional[str] = None
    licensed_humans: list[str]
    actions: list[str]
    notes: list[str] = Field(default_factory=list)


def _minor_work_tests(price, permit, larger, advertises, employs) -> list[ExemptionTest]:
    limit = P(R_MINOR, "max_aggregate_contract_price_usd_exclusive")
    return [
        ExemptionTest(test=f"Aggregate price under {_usd(limit)} (labor, materials and everything else, all contracts "
                           "on the project)", passed=price < limit,
                      detail=f"{_usd(price)} {'<' if price < limit else '>='} {_usd(limit)}"
                             + ("; exactly the limit is not 'less than' it" if price == limit else "")
                             + ". Materials count even if the customer buys them.", rule_ids=[R_MINOR]),
        ExemptionTest(test="No building permit required", passed=None if permit is None else not permit,
                      detail="unknown: ask the building department" if permit is None else
                      ("a permit is required, so a license is required at any price" if permit else "no permit"),
                      rule_ids=[R_MINOR]),
        ExemptionTest(test="Not part of a larger operation and not split into smaller contracts", passed=not larger,
                      detail="part of a larger job or split" if larger else "stand-alone job",
                      rule_ids=[R_MINOR_SPLIT]),
        ExemptionTest(test="Does not advertise as a contractor", passed=not advertises,
                      detail="advertises as a contractor" if advertises else "no contractor advertising",
                      rule_ids=[R_MINOR_ADS_EMPLOYEES]),
        ExemptionTest(test="Works alone (employs no one to do or help with the work)", passed=not employs,
                      detail="has helpers/employees" if employs else "works alone", rule_ids=[R_MINOR_ADS_EMPLOYEES]),
    ]


def unlicensed_contracting_exposure(
        contract_price_usd: float, scope: str, has_license: bool, amount_paid_usd: Optional[float] = None,
        requires_building_permit: Optional[bool] = None, part_of_larger_project_or_split: bool = False,
        advertises_as_contractor: bool = False, has_employees_or_helpers: bool = False,
        prior_unlicensed_convictions: int = 0, declared_disaster_repair: bool = False, public_agency_bid: bool = False,
        previously_licensed_in_california: bool = False, license_lapsed_during_work: bool = False,
        perspective: Literal["unlicensed_party", "hiring_contractor"] = "unlicensed_party",
        last_activity_date=None, as_of=None) -> UnlicensedExposureResult:
    if contract_price_usd < 0 or (amount_paid_usd is not None and amount_paid_usd < 0) or prior_unlicensed_convictions < 0:
        raise ValueError("amounts and counts cannot be negative")
    if amount_paid_usd is not None and amount_paid_usd > contract_price_usd:
        raise ValueError("amount_paid_usd is more than contract_price_usd")
    day = _as_of(as_of)
    price, paid = float(contract_price_usd), amount_paid_usd
    cite: set[str] = set()
    tests = _minor_work_tests(price, requires_building_permit, part_of_larger_project_or_split,
                              advertises_as_contractor, has_employees_or_helpers)
    cite.update(r for t in tests for r in t.rule_ids)
    if any(t.passed is False for t in tests):
        exemption, required = "does_not_apply", True
    elif any(t.passed is None for t in tests):
        exemption, required = "depends", None
    else:
        exemption, required = "applies", False
    unlicensed = not has_license or license_lapsed_during_work
    if has_license and not license_lapsed_during_work:
        exemption = "not_applicable_licensed"
    notes, other, actions, exposure = [], [], [], []
    humans = ["Construction attorney: to evaluate B&P 7031 claims, substantial compliance and any criminal referral."]
    deadline = None
    if exemption == "applies" and perspective == "unlicensed_party":
        notes.append("Minor work exemption applies: no license needed for this job as described. Ads for it must say "
                     "you are not licensed (B&P 7027.2).")
        cite.add(R_AD_UNDER)
    exposed = unlicensed and exemption != "applies"
    if exemption == "depends" and unlicensed:
        notes.append("Exposure below assumes a license is required (e.g. the work needs a building permit).")
    cite_date = _day(P(R_CITE, "operative_date"))         # SB 779 minimums operative 2026-07-01
    if exposed and perspective == "unlicensed_party":
        # B&P 7031: can't collect the unpaid balance; customer can recover everything paid.
        if paid is None:
            exposure.append(Exposure(item="Contract value at risk (B&P 7031): payments must be returned and unpaid "
                                          "amounts can't be collected", kind="civil", low_usd=_money(price),
                                     high_usd=_money(price), math=f"paid + unpaid = full contract price {_usd(price)}",
                                     rule_ids=[R_7031, R_7031B]))
        else:
            exposure.append(Exposure(item="Unpaid balance you cannot sue to collect (B&P 7031(a))", kind="civil",
                                     low_usd=_money(price - paid), high_usd=_money(price - paid),
                                     math=f"{_usd(price)} - {_usd(paid)} paid = {_usd(price - paid)}",
                                     rule_ids=[R_7031]))
            exposure.append(Exposure(item="Payments the customer can sue to recover (B&P 7031(b))", kind="civil",
                                     low_usd=_money(paid), high_usd=_money(paid),
                                     math=f"all compensation paid: {_usd(paid)}", rule_ids=[R_7031B]))
        cite.update([R_7031, R_7031B])
        other.append("Mechanics liens and other security for payment can't be enforced (B&P 7031).")
        # CSLB administrative citation (B&P 7028.7)
        rid = R_CITE if day >= cite_date else R_CITE_OLD
        lo, hi = P(rid, "civil_penalty_min_usd"), P(rid, "civil_penalty_max_usd")
        exposure.append(Exposure(item="CSLB citation civil penalty (B&P 7028.7) with order to stop", kind="administrative",
                                 low_usd=lo, high_usd=hi, math=f"{_usd(lo)}-{_usd(hi)} per citation", rule_ids=[rid]))
        cite.add(rid)
        # Criminal fine (B&P 7028), by prior convictions
        pay = paid if paid is not None else price
        if prior_unlicensed_convictions == 0:
            hi1 = P(R_7028_1, "fine_max_usd")
            exposure.append(Exposure(item="Criminal fine if convicted, first offense (B&P 7028(b))", kind="criminal",
                                     low_usd=0.0, high_usd=hi1, math=f"up to {_usd(hi1)}", rule_ids=[R_7028_1]))
            other.append(f"First conviction: up to {P(R_7028_1, 'jail_max_months')} months in county jail, with or "
                         "without the fine.")
            cite.add(R_7028_1)
        elif prior_unlicensed_convictions == 1:
            pct, floor = P(R_7028_2, "fine_percent"), P(R_7028_2, "fine_floor_usd")
            fine = max(price * pct / 100, pay * pct / 100, floor)
            exposure.append(Exposure(item="Criminal fine if convicted, second offense (B&P 7028(c))", kind="criminal",
                                     low_usd=_money(fine), high_usd=_money(fine),
                                     math=f"greater of {pct}% x {_usd(price)} price = {_usd(price * pct / 100)}, {pct}% "
                                          f"x {_usd(pay)} paid = {_usd(pay * pct / 100)}, or {_usd(floor)} -> {_usd(fine)}",
                                     rule_ids=[R_7028_2]))
            other.append(f"Second conviction: at least {P(R_7028_2, 'min_jail_days')} days in county jail unless the "
                         "court states reasons for less.")
            cite.add(R_7028_2)
        else:
            lo3 = P(R_7028_3, "fine_min_usd")
            floor3, pct3 = _formula_numbers(R_7028_3, "fine_max_formula")[:2]   # max(10000, 0.20 * price, ...)
            cap3 = max(floor3, price * pct3, pay * pct3)
            exposure.append(Exposure(item="Criminal fine if convicted, third or later offense (B&P 7028(d))",
                                     kind="criminal", low_usd=lo3, high_usd=_money(cap3),
                                     math=f"{_usd(lo3)} up to the greater of {_usd(floor3)}, {pct3:.0%} x {_usd(price)} "
                                          f"price or {pct3:.0%} x {_usd(pay)} paid "
                                          f"-> {_usd(cap3)}", rule_ids=[R_7028_3]))
            other.append(f"Third or later conviction: {P(R_7028_3, 'jail_min_days')} days to "
                         f"{P(R_7028_3, 'jail_max_years')} year in county jail.")
            cite.add(R_7028_3)
        if advertises_as_contractor:
            lo_a, hi_a = P(R_AD_FINE, "fine_min_usd"), P(R_AD_FINE, "fine_max_usd")
            exposure.append(Exposure(item="Fine for advertising without a license (B&P 7027.1)", kind="criminal",
                                     low_usd=lo_a, high_usd=hi_a, math=f"{_usd(lo_a)}-{_usd(hi_a)}, on top of other "
                                     "penalties", rule_ids=[R_AD_FINE]))
            cite.add(R_AD_FINE)
        if declared_disaster_repair:
            hi_d = P(R_DISASTER, "felony_option.fine_max_usd")
            exposure.append(Exposure(item="Unlicensed repair of declared-disaster damage (B&P 7028.16)", kind="criminal",
                                     low_usd=0.0, high_usd=hi_d, math=f"fine up to {_usd(hi_d)}",
                                     rule_ids=[R_DISASTER]))
            other.append("Disaster repair without a license is a felony-level offense: "
                         + ", ".join(P(R_DISASTER, "felony_option.imprisonment_penal_code_1170h"))
                         + " under Penal Code 1170(h), with or without the fine.")
            cite.add(R_DISASTER)
        if public_agency_bid:
            other.append("A public-agency bid without a license is nonresponsive and a contract awarded is void "
                         "(B&P 7028.15).")
            cite.add(R_PUBLIC_BID)
        dbl = P(R_DOUBLE_BOND, "amount_usd")
        other.append(f"If you later apply for a license, CSLB may require a {_usd(dbl)} bond (double) until first "
                     "renewal when the violation caused substantial injury (B&P 7071.6(d)).")
        cite.add(R_DOUBLE_BOND)
        last = _day(last_activity_date, "last_activity_date")
        years = P(R_7028_SOL, "years")
        cite.add(R_7028_SOL)
        if last:
            deadline = _add_years(last, years).isoformat()
            notes.append(f"Prosecution window: {years} years from the latest of proposal, contract, completion or "
                         f"abandonment -> {deadline}.")
        else:
            notes.append(f"Prosecution window: {years} years from the latest of proposal, contract, completion or "
                         "abandonment (give last_activity_date for the date).")
        if previously_licensed_in_california or license_lapsed_during_work:
            notes.append("Substantial compliance may excuse a lapse only if you were licensed in California before the "
                         "work, acted reasonably and in good faith to stay licensed, and fixed it promptly once you "
                         "knew; a court decides after a hearing (B&P 7031(e)). Never-licensed contractors can't use it.")
            cite.add(R_SUBST)
        actions += ["Stop taking new work that needs a license until licensed.",
                    "Do not record or foreclose a lien for this job without an attorney's advice."]
    if exposed and perspective == "hiring_contractor":
        lo, hi = P(R_7118_PEN, "min_usd" if day >= cite_date else "min_usd_before_2026_07_01"), P(R_7118_PEN, "max_usd")
        exposure.append(Exposure(item="CSLB citation against your license for contracting with an unlicensed "
                                      "contractor (B&P 7118)", kind="administrative", low_usd=lo, high_usd=hi,
                                 math=f"{_usd(lo)}-{_usd(hi)} per citation", rule_ids=[R_7118_PEN, R_7118]))
        disc = P(R_7118_DISC, "minimum_penalty.text")
        other.append(f"Disciplinary guideline for B&P 7118: minimum {disc}; maximum revocation.")
        other.append("You can recover what you paid an unlicensed sub (B&P 7031(b)), but the owner may also look to "
                     "you for the unlicensed work.")
        other.append("An unlicensed worker doing licensed work is presumed your employee (Labor Code 2750.5): payroll "
                     "taxes, wage-and-hour and workers' comp exposure follow.")
        cite.update([R_7118_PEN, R_7118, R_7118_DISC, R_7031B, R_2750_5])
        actions += ["Do not sign with or pay this sub until it holds an active license in the right class.",
                    "If work already started, stop it, document payments and call a construction attorney."]
    if has_license and not license_lapsed_during_work:
        notes.append("Licensed: keep the license active, bonded and insured for the whole job (any lapse is "
                     "unlicensed time under B&P 7031) and work only in the classes you hold (B&P 7117.6). Use "
                     "classification_for_scope to check the class.")
        cite.update([R_7031, R_7117_6])
    if not exposed and exemption == "applies" and perspective == "hiring_contractor":
        notes.append("The minor work exemption applies, so this person needs no license for this job as described.")
    low = _money(sum(e.low_usd for e in exposure))
    high = _money(sum(e.high_usd for e in exposure))
    res = UnlicensedExposureResult(
        as_of=day.isoformat(), perspective=perspective, contract_price_usd=price, amount_paid_usd=paid, scope=scope,
        has_license=has_license, license_required=required, minor_work_exemption=exemption, exemption_tests=tests,
        exposure=exposure, total_low_usd=low, total_high_usd=high, other_consequences=other, prosecution_deadline=deadline, licensed_humans=humans,
        actions=actions, notes=notes)
    return res.cite(*sorted(cite))


# ================================================================ MCP tools
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def register(mcp) -> None:
    @mcp.tool(name="bulk_check_subcontractors", annotations=READ_ONLY)
    def _bulk(
        csv_text: Annotated[Optional[str], Field(description="CSV with a header row. Columns: license_number "
                            "(required), scope_of_work, required_classification, coi_expiry, wc_required (yes/no), "
                            "name, project_type")] = None,
        rows: Annotated[Optional[list[SubInput]], Field(description="Same data as a list of objects instead of CSV")] = None,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date (default today in California)")] = None,
    ) -> BulkSubResult:
        """Prequalify a whole subcontractor list against CSLB license data. For each sub: license status, class vs
        scope (given, or inferred from scope_of_work), liability insurance expiry, workers' comp, bond, and the 2028
        workers' comp change. Returns a per-sub risk table (do_not_use / high / medium / low, sorted worst first)
        with problems and an action for each, counts by risk, grouped top actions and a markdown table. A license
        missing from a partial copy of CSLB data is reported as 'confirm on CSLB site', never as unlicensed."""
        return bulk_check_subcontractors(csv_text, rows, as_of)

    @mcp.tool(name="classification_for_scope", annotations=READ_ONLY)
    def _classify(
        scope_of_work: Annotated[str, Field(description="Plain description of the work, e.g. 'replace water heater, "
                                 "add 2 outlets, tile shower'")],
        project_type: Annotated[ProjectType, Field(description="Kind of project; residential_existing enables B-2, "
                                "civil_infrastructure points to Class A, adu/manufactured_home add C-47")] = "other",
        contract_role: Annotated[Literal["prime", "subcontract"], Field(description="Prime contract with the owner, "
                                 "or a subcontract under another contractor (changes the B two-trade rule)")] = "prime",
        battery_kwh: Annotated[Optional[float], Field(ge=0, description="Battery storage rating in kWh, if any")] = None,
        asbestos_sq_ft: Annotated[Optional[float], Field(ge=0, description="Square feet of asbestos-containing material, if any")] = None,
    ) -> ClassificationResult:
        """Which CSLB license classifications can perform a scope of work in California: each specialty C-class the
        scope touches (16 CCR 832), B General Building under the B&P 7057 two-unrelated-trades rule (framing and
        carpentry don't count; prime vs subcontract differ; fire sprinklers, wells and asbestos must be held or
        subbed), B-2 Residential Remodeling (3+ trades, existing wood-frame homes), and A General Engineering. Each
        option is yes / conditional / no with its conditions and the controlling rule text. Includes 7059
        incidental-and-supplemental limits for specialty contractors."""
        return classification_for_scope(scope_of_work, project_type, contract_role, battery_kwh, asbestos_sq_ft)

    @mcp.tool(name="company_license_obligations", annotations=READ_ONLY)
    def _obligations(
        entity_type: Annotated[EntityType, Field(description="How the license is held")],
        employee_count: Annotated[int, Field(ge=0, description="Employees (any, incl. part-time)")] = 0,
        member_count_for_llc: Annotated[Optional[int], Field(ge=0, description="LLC only: persons listed as personnel "
                                        "of record (officers, managers, members, qualifiers)")] = None,
        classifications: Annotated[Optional[list[str]], Field(description="Classes held, e.g. ['B', 'C-10']")] = None,
        license_expiration_date: Annotated[Optional[str], Field(description="Expiration date on the license (ISO)")] = None,
        has_qualifier: Annotated[bool, Field(description="A qualifying individual is currently on the license")] = True,
        qualifier_is_rme_or_rmo: Annotated[bool, Field(description="The qualifier is an RME or RMO (or LLC "
                                           "responsible managing member/manager), not the owner or a general partner")] = False,
        qualifier_is_employee_rme: Annotated[bool, Field(description="The qualifier is specifically an RME (employee)")] = False,
        qualifier_ownership_percent: Annotated[Optional[float], Field(ge=0, le=100, description="RMO's % of voting "
                                               "stock (corporation) or LLC qualifier's % membership interest")] = None,
        license_status: Annotated[Literal["active", "inactive"], Field(description="License status")] = "active",
        employs_home_improvement_salespersons: Annotated[bool, Field(description="Employs registered HIS salespeople")] = False,
        changes: Annotated[Optional[list[ChangeEvent]], Field(description="Recent record changes with dates, for the "
                           "CSLB notice deadlines (qualifier_left, business_address, mailing_address, business_name, "
                           "personnel, ...)")] = None,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date (default today in California)")] = None,
    ) -> LicenseObligations:
        """What a California contractor license must keep on file and when: contractor's bond, qualifier bond (with
        the 10% owner exemption), LLC $100,000 worker bond, LLC liability insurance aggregate (per-person rule, with
        the math), workers' comp certificate vs exemption (mandatory classes, RME, the 2028 end of the exemption),
        renewal date and fees (on time, late, 90-day retroactive window, 5-year limit), and CSLB change-notice
        deadlines (90 days; 30 for mailing address; qualifier replacement). Returns line items, a sorted deadline
        list with days remaining, actions and the licensed people involved."""
        return company_license_obligations(
            entity_type, employee_count, member_count_for_llc, classifications, license_expiration_date,
            has_qualifier, qualifier_is_rme_or_rmo, as_of, license_status=license_status,
            qualifier_is_employee_rme=qualifier_is_employee_rme, qualifier_ownership_percent=qualifier_ownership_percent,
            employs_home_improvement_salespersons=employs_home_improvement_salespersons, changes=changes)

    @mcp.tool(name="unlicensed_contracting_exposure", annotations=READ_ONLY)
    def _exposure(
        contract_price_usd: Annotated[float, Field(ge=0, description="Total price for labor, materials and all other "
                                      "items on the project (all contracts combined)")],
        scope: Annotated[str, Field(description="What the work is")],
        has_license: Annotated[bool, Field(description="The person doing the work holds an active CSLB license")],
        amount_paid_usd: Annotated[Optional[float], Field(ge=0, description="Amount already paid to them")] = None,
        requires_building_permit: Annotated[Optional[bool], Field(description="Does the work need a building permit? "
                                            "Leave empty if unknown")] = None,
        part_of_larger_project_or_split: Annotated[bool, Field(description="Part of a bigger job, or split into "
                                                   "smaller contracts")] = False,
        advertises_as_contractor: Annotated[bool, Field(description="Advertises (cards, signs, online) as a contractor")] = False,
        has_employees_or_helpers: Annotated[bool, Field(description="Uses employees or helpers on the work")] = False,
        prior_unlicensed_convictions: Annotated[int, Field(ge=0, description="Prior convictions under B&P 7028")] = 0,
        declared_disaster_repair: Annotated[bool, Field(description="Repairing damage from a declared disaster")] = False,
        public_agency_bid: Annotated[bool, Field(description="Bid to a public agency")] = False,
        previously_licensed_in_california: Annotated[bool, Field(description="Held a California license before")] = False,
        license_lapsed_during_work: Annotated[bool, Field(description="Licensed, but the license was expired or "
                                              "suspended for part of the job")] = False,
        perspective: Annotated[Literal["unlicensed_party", "hiring_contractor"], Field(description="unlicensed_party "
                               "= the person doing the work; hiring_contractor = a licensee who hired them")] = "unlicensed_party",
        last_activity_date: Annotated[Optional[str], Field(description="Latest of proposal, contract, completion or "
                                      "abandonment (ISO), for the 4-year prosecution window")] = None,
        as_of: Annotated[Optional[str], Field(description="Evaluate as of this ISO date (default today in California)")] = None,
    ) -> UnlicensedExposureResult:
        """Exposure from contracting without a California license: the B&P 7048 minor-work exemption test (under
        $1,000 total, no permit, not split, no ads, no helpers), then B&P 7031 (can't collect, customer recovers
        everything paid), the CSLB citation range (B&P 7028.7), criminal fines by prior convictions (B&P 7028),
        advertising, disaster and public-bid add-ons, and the 4-year prosecution window. With
        perspective=hiring_contractor it returns the hiring licensee's B&P 7118 citation range and related risks.
        Returns line items with low/high dollars and the math, totals, non-money consequences and actions."""
        return unlicensed_contracting_exposure(
            contract_price_usd, scope, has_license, amount_paid_usd, requires_building_permit,
            part_of_larger_project_or_split, advertises_as_contractor, has_employees_or_helpers,
            prior_unlicensed_convictions, declared_disaster_repair, public_agency_bid,
            previously_licensed_in_california, license_lapsed_during_work, perspective, last_activity_date, as_of)
