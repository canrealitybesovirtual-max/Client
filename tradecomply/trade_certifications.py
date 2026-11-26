"""Trade certifications for California construction: who must hold which credential, whether a crew can legally
start, how an electrician gets certified, and which jobsite equipment needs a DOSH permit to operate.

  certifications_required          tasks + your CSLB classes + the building -> every credential the work needs:
                                   DIR electrician certification (5 types, trainee / apprentice / qualifier routes),
                                   EPA RRP certified renovator + firm certification, CDPH lead certification at the
                                   50 ug/m3 exposure trigger, crane operator certification + employer evaluation,
                                   elevator mechanic (CCCM) and company (CQCC) certification, a licensed blaster on
                                   site, personnel hoist and pressure-vessel permits; with cost, proof to keep on site
                                   and the next step
  check_crew_certifications        crew list [{name, task, credentials}] -> per worker: allowed to do that task?
                                   expired, due or overdue for renewal (renew-by dates)? plus crew checks (one trainee
                                   per certified electrician, a certified renovator / licensed blaster on site, crane
                                   trainees with a certified trainer) and the exposure
  electrician_certification_path   certification type + hours by work area -> hours counted under the 8 CCR 291.1
                                   area caps, shortfall, fees, exam window and earliest retake, trainee route
  equipment_operating_permits      air tanks, LPG tanks, boilers, personnel hoists, elevators and lifts -> DOSH permit to
                                   operate needed? the exemption math, permit term, next inspection, fees

Every legal number, threshold and date comes from core.rule(id).param(key) at call time, and every rule a result
relies on is cited through core.Result.cite(), so `verified` is True only when all of them were verified. The pack is
research/web_trade-certs.verified.json (rule ids "cert-..."); RRP applicability, minor repair and firm certification
come from research/web_permits-building.verified.json (rrp-...). Its rrp-firm-certification rule is "unchecked", so a
result that needs EPA firm certification is verified=False, which is correct.

Not legal values (so not in the rulebook): the task, credential, building, area and equipment spellings below, which
only map a user's words onto the rules (including _WINDOW_REPLACEMENT, _DELIVERY and _OIT_TASK_WORDS); DUE_SOON_DAYS
(a display window); MAX_AREA_HOURS (an input sanity bound); and the US gallon (231 cubic inches).

Second-pass legal review (2026-11-25), each change read in the official text: 8 CCR 5006.2 exceptions (derricks,
sideboom cranes, equipment rated 2,000 lb or less, delivery knuckle-booms under 25 ft / 15,000 lb, digger derricks)
and operator-in-training task limits; 5-year cap on a crane certificate; 8 CCR 753 boiler definitions (safety valve
setting, hot-water boilers); 461(h) indefinite air-tank permits are not declared overdue when the stamp is unknown;
40 CFR 745.83 electrical work in pre-1978 housing above 6 / 20 sq ft is an RRP renovation; 745.103 senior housing and
0-bedroom dwellings are not target housing unless a child under 6 lives there; NEC Article 517 (care facilities)
flagged for a Residential Electrician; 344.30(a)(16) screw elevators and (g) the permit processing fee.
"""
import calendar
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError

from . import core, logic

# ---------------------------------------------------------------- rule ids
# electrician certification (Labor Code 108-108.5; 8 CCR 290-296)
E_C10, E_DEF, E_C7, E_UTIL = ("cert-elec-c10-workers-must-be-certified", "cert-elec-electrician-definition-100-va",
                              "cert-elec-exempt-c7-c45-scope-work", "cert-elec-exempt-utility-high-voltage-workers")
E_APPR, E_QUAL, E_TYPES = ("cert-elec-exempt-registered-apprentices", "cert-elec-c10-qualifier-exempt-others-not",
                           "cert-elec-five-certification-types")
E_RES, E_SPEC = "cert-elec-residential-electrician-scope", "cert-elec-specialty-technician-scopes"
E_HOURS, E_CAPS, E_NICET = ("cert-elec-experience-hours-by-type", "cert-elec-experience-hours-area-caps",
                            "cert-elec-fls-nicet-level-ii-alternative")
E_OOS, E_NOGRACE, E_SSA = ("cert-elec-other-state-license-and-military-credit", "cert-elec-no-grace-period-out-of-state",
                           "cert-elec-application-ssa-earnings-report")
E_FEE, E_FEE_RENEW, E_FEE_LAPSED = ("cert-elec-fee-application-and-exam-175", "cert-elec-fee-renewal-100",
                                    "cert-elec-fee-lapsed-renewal-200")
E_FEE_TRAINEE = "cert-elec-fee-trainee-registration-25"
E_RESP, E_WINDOW, E_EXAM = ("cert-elec-application-response-30-days", "cert-elec-exam-within-1-year-of-eligibility",
                            "cert-elec-exam-passing-score-70")
E_RENEW, E_FILE, E_LAPSE = ("cert-elec-renewal-3-years-32-ce-2000-hours", "cert-elec-renewal-filing-deadline",
                            "cert-elec-lapse-means-retest")
E_TRAINEE, E_RATIO, E_BARRED = ("cert-elec-trainee-registration", "cert-elec-trainee-supervision-ratio-1-to-1",
                                "cert-elec-employer-barred-from-trainees")
E_T_RENEW, E_T_150, E_T_WITHDRAW = ("cert-elec-trainee-annual-renewal", "cert-elec-trainee-150-classroom-hours-per-year",
                                    "cert-elec-trainee-withdrawal-notice")
E_T_PATH, E_NRLT_T = "cert-elec-trainee-path-to-exam", "cert-elec-nonresidential-lighting-trainee"
E_DISC, E_PRESUME = "cert-elec-cslb-discipline-grounds", "cert-elec-cslb-60-day-action-and-presumption"
E_FALSE, E_APPEAL = "cert-elec-false-claim-7-year-bar", "cert-elec-denial-appeal-30-days"
E_ADDR, E_AB1707 = "cert-elec-address-change-notice", "cert-elec-ecu-transfer-to-cslb-ab1707"
# lead: Cal/OSHA / CDPH (8 CCR 1532.1(l)) and EPA RRP (40 CFR 745 Subpart E)
L_CDPH, L_DEF = "cert-lead-cdph-certification-50-ugm3", "cert-lead-related-construction-definition"
L_PROVIDER, L_SUP = "cert-lead-training-by-cdph-accredited-provider", "cert-lead-supervisor-must-be-certified"
L_RECORDS = "cert-lead-certification-records-on-request"
R_APPLY, R_MINOR, R_FIRM = "rrp-applicability-pre-1978", "rrp-minor-repair-6-20-sqft", "rrp-firm-certification"
R_REFRESH, R_ONSITE, R_TRAINED = ("cert-rrp-renovator-refresher-5-or-3-years", "cert-rrp-renovator-onsite-duties",
                                  "cert-rrp-workers-trained-by-certified-renovator")
R_RECERT, R_AMEND, R_FEE = ("cert-rrp-firm-recertify-90-days-before-expiry", "cert-rrp-firm-amend-within-90-days",
                            "cert-rrp-firm-fee-300")
R_INT, R_EXT, R_CLEAN = ("cert-rrp-interior-containment-6-feet", "cert-rrp-exterior-containment-10-and-20-feet",
                         "cert-rrp-cleaning-verification-procedure")
R_DUST, R_EMERG = "cert-rrp-dust-clearance-action-levels-2026", "cert-rrp-emergency-renovation-exemption"
R_COF, R_COF_NOTICE, R_PEN = ("cert-rrp-child-occupied-facility-definition", "cert-rrp-child-occupied-facility-notice",
                              "cert-rrp-penalty-tsca-49772")
R_TRADES, R_EXCL = "cert-rrp-minor-repair-includes-trade-work", "cert-rrp-target-housing-exclusions-defined"
# cranes (8 CCR 5006.1, 5006.2)
C_EVAL, C_EXAM, C_GOV = ("cert-crane-operator-employer-evaluation", "cert-crane-certification-exam-components",
                         "cert-crane-government-license-option")
C_OIT, C_GI = "cert-crane-operator-in-training-supervision", "cert-crane-general-industry-5006-1"
C_EXC = "cert-crane-5006-2-exceptions"
# elevators and other conveyances (8 CCR 3000-3001, 344.30), personnel hoists (8 CCR 1604)
V_PERMIT, V_2YR, V_NEW = ("cert-elev-permit-to-operate-required", "cert-elev-two-year-permit-full-maintenance",
                          "cert-elev-new-installation-plans-and-acceptance-inspection")
V_REINSP, V_TIMES = "cert-elev-annual-reinspection", "cert-elev-permit-renewal-processing-times"
V_FEE_NEW, V_FEE_HOUR, V_FEE_PERIODIC = ("cert-elev-fees-new-installation-permit", "cert-elev-fees-inspection-hourly",
                                         "cert-elev-fees-periodic-reinspection")
V_FEE_ALT, V_LATE = "cert-elev-fees-alteration-permit", "cert-elev-late-fee-100-percent-and-opu"
V_CCCM_ONLY, V_CCCM, V_CCCM_RENEW = ("cert-elev-only-cccm-of-cqcc-may-work", "cert-elev-cccm-requirements-and-fee",
                                     "cert-elev-cccm-renewal-8-hours-ce")
V_CQCC, V_TEMP, V_EXCL = ("cert-elev-cqcc-company-certification", "cert-elev-temporary-mechanic-tcccm",
                          "cert-elev-eso-exclusions-private-homes-construction-elevators")
H_PERMIT, H_INSPECT, H_PERM_ELEV = ("cert-hoist-personnel-hoist-permit-to-operate",
                                    "cert-hoist-acceptance-and-periodic-inspections",
                                    "cert-hoist-permanent-elevator-construction-use")
# pressure vessels (8 CCR 344.1-344.2, 461-462, 470, 770-780)
PV_AIR, PV_AIR_SMALL, PV_AIR_TERM = ("cert-pv-air-tank-permit-required", "cert-pv-air-tank-small-exemption",
                                     "cert-pv-air-tank-permit-term")
PV_AIR_INDEF, PV_RENTAL, PV_LPG = ("cert-pv-air-tank-indefinite-permit", "cert-pv-air-tank-rental-and-posting",
                                   "cert-pv-lpg-tank-permit-125-gallons")
PV_BOILER, PV_BOILER_SMALL = "cert-pv-boiler-permit-and-annual-inspection", "cert-pv-boiler-small-exemption"
PV_FEES, PV_FOLLOWUP, PV_LA = ("cert-pv-permit-and-inspection-fees", "cert-pv-follow-up-inspection-15-days",
                               "cert-pv-los-angeles-city-permits")
# blasting (8 CCR 5238, 344.20-344.22)
B_ONSITE, B_QUAL, B_RENEW = ("cert-blaster-licensed-blaster-on-site", "cert-blaster-license-qualifications-and-classes",
                             "cert-blaster-license-5-year-renewal")

DUE_SOON_DAYS = 60                  # display window for "renew soon", not a legal number
MAX_AREA_HOURS = 100_000            # input sanity bound (about 48 years of full-time work), not a legal number
GALLONS_PER_CU_FT = 1728 / 231      # US gallon = 231 cubic inches (a unit definition, not law)

Status = Literal["required", "conditional", "not_required"]
Trade = Literal["electrical", "lead_rrp", "lead_cdph", "crane", "elevator", "personnel_hoist", "blasting",
                "pressure_vessel"]


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _n(x) -> str:
    """Number for display: thousands separators, at most 2 decimals, no trailing zeros."""
    return f"{float(x):,.2f}".rstrip("0").rstrip(".")


def _usd(x) -> str:
    return "$" + (f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}")


def _join(items, last: str = "and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def _words(text) -> str:
    """Lowercase, punctuation to spaces, single spaces: 'Fire/Life-Safety (FLS)' -> 'fire life safety fls'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", str(text or "").lower())).strip()


def _cit(ids) -> str:
    return "; ".join(dict.fromkeys(core.rule(i).citation_text or i for i in ids))


def _ok(ids) -> bool:
    return bool(ids) and all(core.rule(i).verified for i in ids)


def _plus_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                      # Feb 29 -> Feb 28
        return d.replace(year=d.year + n, day=28)


def _plus_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _date(value, field_name: str) -> Optional[date]:
    """ISO date (2027-03-31), ISO datetime, or US style 3/31/2027; None / '' stay None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        if _US_DATE.fullmatch(s):
            mo, d, y = (int(g) for g in _US_DATE.fullmatch(s).groups())
            return date(y, mo, d)
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"{field_name} must be a date like 2027-03-31, got {value!r}") from None


def _as_of(value) -> date:
    return _date(value, "as_of") or logic.today_ca()


def _num(name: str, value, lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    """A number, or text like '1.2 cu ft', '125 psi', '62 ug/m3' (the unit words are ignored); None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.match(r"^\s*(-?\d*\.?\d+)\s*([a-zµμ/%'^³3 .]*)\s*$", str(value).replace(",", "").lower())
        if not m:
            raise ValueError(f"{name} must be a number, got {value!r}")
        v = float(m.group(1))
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if lo is not None and v < lo:
        raise ValueError(f"{name} cannot be below {lo:g}, got {value!r}")
    if hi is not None and v > hi:
        raise ValueError(f"{name} cannot be above {hi:g}, got {value!r}")
    return v


def _int(name: str, value, lo: int, hi: Optional[int] = None) -> Optional[int]:
    v = _num(name, value, lo, hi)
    if v is None:
        return None
    if v != int(v):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return int(v)


def _bool(name: str, value) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("yes", "y", "true", "t", "1"):
        return True
    if s in ("no", "n", "false", "f", "0"):
        return False
    if s in ("", "unknown", "not sure", "?", "none"):
        return None
    raise ValueError(f"{name} must be true or false, got {value!r}")


# ---------------------------------------------------------------- spellings (not law)
def classes_from(value) -> Optional[set[str]]:
    """'C10', 'c-10 electrical', ['B', 'C-7'], 'Electrical contractor' -> {'C-10'} ...; None = not given."""
    if value is None:
        return None
    items = list(value) if isinstance(value, (list, tuple, set)) else [value]
    out: set[str] = set()
    for raw in items:
        s = str(raw or "").strip()
        if not s:
            continue
        found = set()
        for m in re.finditer(r"(?<![a-z0-9])([abcd])\s*-?\s*(\d{1,2})(?![0-9])", s.lower()):
            found.add(f"{m.group(1).upper()}-{int(m.group(2))}")
        w = _words(s)
        for code, pat in (("C-10", r"\belectrical\b|\belectricians?\b"), ("C-7", r"\blow voltage\b"),
                          ("C-45", r"\b(electric )?signs?\b"), ("C-11", r"\belevators?\b"),
                          ("B", r"\bgeneral building\b|\bgeneral contractor\b|\bgc\b"),
                          ("A", r"\bgeneral engineering\b")):
            if re.search(pat, w):
                found.add(code)
        for piece in re.split(r"[,;/&+]| and ", s.lower()):
            m = re.fullmatch(r"\s*(?:class\s+)?([ab])(?:\s+(?:license|lic))?\s*", piece)
            if m:
                found.add(m.group(1).upper())
        if not found:
            raise ValueError(f"employer_license_classes: could not read {raw!r}; use CSLB classification codes like "
                             "'C-10', 'C-7', 'C-45', 'B' or 'A'")
        out |= found
    return out


_BUILDINGS = (
    ("child_occupied", r"day ?care|child ?care|preschool|pre school|kindergarten|nursery school|head start"),
    # 40 CFR 745.103: housing for the elderly / persons with disabilities and 0-bedroom dwellings are not target
    # housing unless a child under 6 lives there; care facilities also raise the NEC Article 517 question for a
    # Residential Electrician (8 CCR 290.1)
    ("senior_or_care", r"senior (housing|apartments?|living|community|communities|center)|retirement (homes?|"
                       r"community|communities|housing)|housing for (the )?(elderly|disabled|persons with "
                       r"disabilities)|elderly housing|\b(55|62) ?(\+|plus)|assisted living|nursing (homes?|"
                       r"facility|facilities)|skilled nursing|board and care|residential care|memory care|"
                       r"group homes?"),
    ("zero_bedroom", r"studio (apartments?|units?)|efficienc(y|ies) (apartments?|units?|studios?)|\bsro\b|"
                     r"single room occupancy|dorm\w*|barracks|rooming house|room rentals?|rented rooms?"),
    ("school", r"\bschools?\b|elementary|college|university|campus|classrooms?"),
    ("two_unit", r"duplex|two unit|2 unit|two family|2 family"),
    ("multifamily", r"multi ?family|apartments?|condos?|condominiums?|triplex|fourplex|[34] ?plex|multi unit"),
    ("hotel_motel", r"hotels?|motels?|lodging"),
    ("single_family", r"single family|\bsfr\b|\bsfd\b|\bhouse\b|\bhome\b|residence|townhouse|townhome|\badu\b|"
                      r"mobile home|manufactured home"),
    ("residential", r"residential|housing|dwelling"),
    ("industrial", r"industrial|factory|refinery|manufacturing|power plant|substation"),
    ("public_or_commercial", r"commercial|office|retail|store|restaurant|mall|shopping|hospital|clinic|medical|"
                             r"government|city hall|courthouse|library|museum|airport|church|public|bank|theater|"
                             r"theatre|convention|tenant improvement"),
)
BUILDING_TYPES = tuple(k for k, _ in _BUILDINGS)
_HOUSING = ("single_family", "two_unit", "multifamily", "residential")
_NOT_TARGET_UNLESS_CHILD = ("senior_or_care", "zero_bedroom")       # 40 CFR 745.103
_RESIDENTIAL = _HOUSING + ("hotel_motel", "zero_bedroom")            # Residential Electrician occupancies


def building_from(value) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    w = _words(value).replace(" ", "_")
    if w in BUILDING_TYPES:
        return w
    w = _words(value)
    for key, pat in _BUILDINGS:
        if re.search(pat, w):
            return key
    raise ValueError(f"building_type {value!r} not recognized; use one of {', '.join(BUILDING_TYPES)} (or words like "
                     "'house', 'apartment', 'daycare', 'office', 'factory')")


_LA_NAMES = {"los angeles", "la", "l a", "lacity", "ladbs", "los angles", "losangeles", "la city"}


def is_city_of_los_angeles(city) -> Optional[bool]:
    """'LA', 'L.A.', 'City of Los Angeles', 'Los Angeles, CA 90012' -> True; 'LA County' -> False; None -> None."""
    if city is None or not str(city).strip():
        return None
    w = _words(city)
    w = re.sub(r"(\s+\d{5}(\s+\d{4})?)$", "", w)
    for _ in range(2):
        w = re.sub(r"\s+(ca|calif|california|usa|us)$", "", w).strip()
    w = re.sub(r"^the\s+", "", w)
    if re.search(r"\bcounty\b|\bunincorporated\b", w):
        return False
    return re.sub(r"^city of\s+", "", w) in _LA_NAMES


# task words -> (trade, subtype). Each pattern's match is blanked before the next is tried, so 'construction
# elevator' is a personnel hoist (not an elevator) and 'lighting retrofit' is not also 'lighting'.
_TASK_PATTERNS = (
    ("lead", "abrasive", r"\b(sand|abrasive|media|soda|bead|shot|grit|hydro|water|dry ice|vapor|wet) ?blast\w*|"
                         r"sandblast\w*"),
    ("personnel_hoist", "permanent_elevator",
     r"\b(permanent|building|passenger|freight) elevators? (used )?(as (a )?)?(construction|hoist|for construction)\w*"
     r"|(use|using|run|running) (the )?(permanent|building) elevators?"),
    ("personnel_hoist", None, r"\b(personnel hoists?|construction elevators?|man ?hoists?|buck ?hoists?|alimak|"
                              r"construction hoists?|worker hoists?)\b"),
    ("elevator", None, r"\b(elevators?(?! (shafts?|pits?|hoistways?|lobby|lobbies|cores?|doors? frames?))|escalators?|dumbwaiters?|moving walks?|moving walkways?|wheelchair lifts?|"
                       r"stair ?lifts?|stairway (chair )?lifts?|platform lifts?|conveyances?|vrcs?|"
                       r"vertical reciprocating conveyors?|material lifts?)\b"),
    ("blasting", None, r"\b(blast|blasts|blasting|explosives?|dynamite|detonat\w*|implosion)\b"),
    # 8 CCR 5006.2 Exceptions: digger derricks, derricks / sideboom cranes, small delivery knuckle-booms
    ("crane", "digger_derrick", r"\b(digger derricks?|(electric )?line trucks?)\b"),
    ("crane", "derrick_sideboom", r"\b(derricks?|side ?booms?( cranes?)?)\b"),
    ("crane", "knuckle_boom", r"\b(knuckle ?booms?( cranes?| trucks?)?|articulating (boom )?cranes?)\b"),
    ("crane", None, r"\b(cranes?|boom trucks?)\b"),
    ("pressure_vessel", "air", r"\b(air compressors?|compressors? (tanks?|receivers?)|air tanks?|air receivers?|"
                               r"receiver tanks?|compressed air)\b"),
    ("pressure_vessel", "lpg", r"\b(propane( tanks?)?|lpg|lp gas|liquefied petroleum)\b"),
    ("pressure_vessel", "boiler", r"\b(boilers?)\b"),
    ("electrical", "utility_hv", r"\b(utility (line|lines|transmission|distribution)\w*|transmission lines?|"
                                 r"distribution lines?|high voltage (lines?|transmission|distribution)|line work|"
                                 r"lineman|linemen)\b"),
    ("electrical", "fire_alarm", r"\b(fire alarms?|fire life safety|fls|nurse call|smoke detection|"
                                 r"mass notification|(nec|article) 760)\b"),
    ("electrical", "low_voltage", r"\b(low voltage|voice data|data (cabling|lines?|drops?)|structured cabling|cabling|"
                                  r"cat ?5e?|cat ?6a?|fiber optic\w*|network cabling|telecom\w*|security cameras?|"
                                  r"cctv|access control|audio visual|intercoms?|vdv|antennas?)\b"),
    ("electrical", "lighting_retrofit", r"\b(lighting retrofits?|retrofit (light|lighting|fixtures?)\w*|led retrofits?|"
                                        r"relamp\w*|ballasts?|lighting maintenance|fixture maintenance)\b"),
    ("electrical", "sign", r"\b(electric(al)? signs?|sign (installation|install|wiring|lighting)|signage|neon signs?|"
                           r"illuminated signs?|channel letters?)\b"),
    ("electrical", "general", r"\b(electrical|electric|electrician|wiring|rewire|rewiring|wire pull\w*|"
                              r"pull\w* (the )?(wire|wires|conductors?)|"
                              r"(electrical|main|service) panels?|panel (upgrade|replacement|change)\w*|panelboards?|"
                              r"sub ?panels?|load centers?|switchgear|service upgrades?|conduit|outlets?|receptacles?|"
                              r"switches|light switch\w*|circuits?|circuit breakers?|light fixtures?|lighting|"
                              r"ev chargers?|ev charging|solar|pv|photovoltaic|battery storage|transformers?|"
                              r"feeders?|generator hookup|motor hookup)\b"),
    ("lead", None, r"\b(paint\w*|repaint\w*|sanding|scrape|scraping|stripping|demo|demolition|demolish\w*|"
                   r"remodel\w*|renovat\w*|window (replacement|replace\w*|install\w*)|replac\w* (the )?windows?|"
                   r"door replacement|drywall (repair|patch\w*|remov\w*|demo\w*|cut\w*|tear\w*|replac\w*)|"
                   r"(cut|cutting|patch|patching|open|opening) (into |up )?(the )?(drywall|walls?|ceilings?)|plaster\w*|lead (paint|based|abatement|safe|hazard|dust|removal|work)|"
                   r"siding|(door|window|wood|interior|exterior) trim|trim (carpentry|work|replacement)|abatement|"
                   r"tear out|tear off|gut)\b"),
)
_SUB_LABEL = {"general": "electrical work", "fire_alarm": "fire alarm (fire/life safety) work",
              "low_voltage": "low-voltage / voice-data-video work", "lighting_retrofit": "lighting retrofit and "
              "maintenance", "sign": "electric sign work", "utility_hv": "utility high-voltage line work",
              "air": "air tanks (compressor receivers)", "lpg": "LP-gas (propane) tanks", "boiler": "boilers"}


_DELIVERY = r"\b(deliver\w*|unload\w*|offload\w*|drop(ping)? off|stock(ing)?|boom (delivery|drop))\b"


def classify_task(task: str) -> list[tuple[str, Optional[str]]]:
    """'Rewire kitchen and demo plaster' -> [('electrical', 'general'), ('lead', None)]."""
    w, hits = _words(task), []
    whole = w
    for trade, sub, pat in _TASK_PATTERNS:
        if re.search(pat, w):
            if sub == "knuckle_boom" and re.search(_DELIVERY, whole):
                sub = "knuckle_boom_delivery"
            key = (trade, None if trade == "lead" else sub)
            if key not in hits:
                hits.append(key)
            w = re.sub(pat, " ", w)
    return hits


# ---------------------------------------------------------------- credentials (spellings, not law)
GE, RE, VDV, FLS, NRLT = ("general_electrician", "residential_electrician", "voice_data_video_technician",
                          "fire_life_safety_technician", "nonresidential_lighting_technician")
TRAINEE, NRLT_TRAINEE, APPRENTICE, QUALIFIER, ELEC_UNSPEC = (
    "electrician_trainee", "nonresidential_lighting_trainee", "registered_apprentice", "c10_qualifying_person",
    "electrician_type_not_given")
RENOVATOR, RRP_TRAINED = "epa_certified_renovator", "rrp_trained_worker"
CDPH_WORKER, CDPH_SUP = "cdph_lead_worker", "cdph_lead_supervisor"
CRANE_CERT, CRANE_GOV, CRANE_OIT = ("crane_operator_certificate", "crane_operator_government_license",
                                    "crane_operator_in_training")
CCCM, TCCCM, BLASTER = "cccm", "tcccm", "blaster_license"
ELECTRICIAN_TYPES = (GE, RE, VDV, FLS, NRLT)
_TYPE_WORD = {GE: "general", RE: "residential", VDV: "voice data video", FLS: "fire/life safety",
              NRLT: "nonresidential lighting"}

_CRED_PATTERNS = (
    (TCCCM, r"\btcccm\b|temporary (certified )?(competent )?(conveyance|elevator) mechanic"),
    (CCCM, r"\bcccm\b|conveyance mechanic|elevator mechanic|certified competent conveyance"),
    (CRANE_OIT, r"operator in training|crane (operator )?trainee|\boit\b"),
    (NRLT_TRAINEE, r"lighting trainee|\bcalt\b|nalmco|lighting (ojt|on the job)"),
    (TRAINEE, r"\btrainee\b|\bt number\b|\bt ?\d{3,}\b"),
    (APPRENTICE, r"apprentice"),
    (QUALIFIER, r"qualifier|qualifying (person|individual)|\brme\b|\brmo\b"),
    (GE, r"general electrician|\bge\b|general (cert\w*)"),
    (RE, r"residential electrician|residential (cert\w*)"),
    (FLS, r"fire life safety|\bfls\b|fire alarm tech\w*"),
    (VDV, r"voice data video|\bvdv\b"),
    (NRLT, r"non ?residential lighting|\bnrlt\b|lighting tech\w*"),
    (RRP_TRAINED, r"trained by (a |the )?(epa )?(certified )?renovator|rrp trained|rrp ojt|"
                  r"on the job (rrp|lead safe) training|lead safe trained"),
    (RENOVATOR, r"certified renovator|\brrp\b|epa renovator|renovator|lead safe (certified|renovator)"),
    (CDPH_SUP, r"cdph.*supervisor|lead (related construction )?supervisor"),
    (CDPH_WORKER, r"\bcdph\b|lead (related construction )?worker"),
    (CRANE_GOV, r"(city|county|government|state|municipal) (crane )?(operator )?licen[cs]e|crane (operator )?licen[cs]e"),
    (CRANE_CERT, r"nccco|\bccco\b|crane institute|\bcic\b|\bnccer\b|crane (operator )?cert\w*|crane operator"),
    (BLASTER, r"blaster|blasting licen[cs]e|explosives? licen[cs]e"),
    (ELEC_UNSPEC, r"electrician|journeyman|electrical cert\w*"),
)


def credential_kind(text: str) -> Optional[str]:
    w = _words(text)
    for kind, pat in _CRED_PATTERNS:
        if re.search(pat, w):
            return kind
    return None


def _etype(kind: str) -> str:
    """Official DIR name of an electrician certification type, read from the rulebook."""
    word = _TYPE_WORD[kind]
    return next(t for t in P(E_TYPES, "types") if t.lower().startswith(word))


def _kind_label(kind: str) -> str:
    if kind in ELECTRICIAN_TYPES:
        return f"{_etype(kind)} certification"
    return {TRAINEE: "registered electrician trainee", NRLT_TRAINEE: "nonresidential lighting trainee (DAS-approved "
            "on-the-job program)", APPRENTICE: "registered apprentice", QUALIFIER: "C-10 qualifying person",
            ELEC_UNSPEC: "electrician certification (type not given)", RENOVATOR: "EPA certified renovator",
            RRP_TRAINED: "trained by the certified renovator (RRP on-the-job training)", CDPH_WORKER: "CDPH lead "
            "worker certificate", CDPH_SUP: "CDPH lead supervisor certificate", CRANE_CERT: "crane operator "
            "certificate (accredited certifying entity)", CRANE_GOV: "government crane operator license",
            CRANE_OIT: "crane operator-in-training", CCCM: "Certified Competent Conveyance Mechanic (CCCM)",
            TCCCM: "Temporary Certified Competent Conveyance Mechanic (TCCCM)", BLASTER: "California Blaster's "
            "License"}[kind]


# ---------------------------------------------------------------- shared result pieces
class _Acc:
    """Collects, for one call, the rules relied on, the math and the notes."""

    def __init__(self):
        self.ids: list[str] = []
        self.math: list[str] = []
        self.notes: list[str] = []

    def use(self, *ids: str) -> list[str]:
        for i in ids:
            core.rule(i)                                    # unknown id -> KeyError, never a silent miss
            if i not in self.ids:
                self.ids.append(i)
        return list(ids)

    def note(self, text: str, *ids: str) -> None:
        self.use(*ids)
        if text not in self.notes:
            self.notes.append(text)

    def finish(self, res):
        res.notes = self.notes + [n for n in res.notes if n not in self.notes]
        res.math = list(dict.fromkeys(self.math + list(res.math)))
        res.cite(*self.ids)
        res.unverified_rule_ids = [i for i in self.ids if not core.rule(i).verified]
        for i in res.unverified_rule_ids:
            r = core.rule(i)
            why = (r.notes or r.correction or "The primary text could not be read.") if r.status == "unverifiable" \
                else "This rule was not independently checked against the official text."
            res.notes.append(f"NOT VERIFIED ({r.status}): {r.title} [{r.citation_text}]. {why[:600]} Confirm with "
                             "the official text before relying on it.")
        return res


class _Base(core.Result):
    summary: str = ""
    next_step: str = ""
    next_steps: list[str] = Field(default_factory=list)
    math: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _ab1707(acc: _Acc, *days: Optional[date]) -> None:
    """AB 1707 (unverifiable: enactment not confirmed) would move electrician certification to CSLB. A result with an
    electrician date on or after its transfer date cites it."""
    start = date.fromisoformat(P(E_AB1707, "transfer_effective"))
    if any(d and d >= start for d in days):
        acc.note(f"A date here is on or after {start}: if AB 1707 (2026) was enacted, electrician certification moves "
                 f"from the {P(E_AB1707, 'from_agency')} to the {P(E_AB1707, 'to_agency')} on that date (subject to "
                 "a budget appropriation), and renewals may be filed with CSLB instead. Enactment is not verified.",
                 E_AB1707)


# ---------------------------------------------------------------- job context and what each trade needs
@dataclass
class _Ctx:
    classes: Optional[set]
    building: Optional[str]
    year_built: Optional[int]
    pre1978: Optional[bool]
    exposure: Optional[float]
    lead_free: bool
    minor: Optional[bool]
    minor_why: str
    emergency: bool
    dust_clearance: bool
    crane_gi: bool
    out_of_state: bool
    city_la: Optional[bool]
    day: date
    interior: bool = True
    exterior: bool = True


@dataclass
class _Need:
    trade: str
    sub: Optional[str]
    status: str
    why: str
    ids: list
    accept: list = field(default_factory=list)          # credential kinds that cover a worker
    alt: list = field(default_factory=list)             # kinds that cover a worker only with supervision
    maybe: list = field(default_factory=list)           # kinds that cover only if a fact we lack is true
    anyone_supervised: bool = False                     # anyone may work under the credential holder's supervision


_WINDOW_REPLACEMENT = (r"\bwindows? (replacement|replac\w*|install\w*|change ?out)|"
                       r"\b(replac\w*|install\w*|swap\w*) (the |new |all )*windows?\b(?! (trim|sills?|casings?|"
                       r"frames?|stools?|aprons?))")


def _context(employer_license_classes, year_built, built_before_1978, building_type, lead_exposure_ug_m3, day,
             lead_free_determination=False, interior_painted_sqft_per_room=None, exterior_painted_sqft=None,
             lead_tasks=(), emergency_renovation=False, dust_clearance_testing=False, crane_outside_construction=False,
             out_of_state_workers=False, city=None, acc: Optional[_Acc] = None) -> _Ctx:
    acc = acc or _Acc()
    cutoff = P(R_APPLY, "built_before_year")
    year = _int("year_built", year_built, 1600, day.year + 5)
    flag = _bool("built_before_1978", built_before_1978)
    pre = None if year is None else year < cutoff
    if year is not None:
        acc.math.append(f"year built {year} {'<' if pre else '>='} {cutoff} -> "
                        + ("built before the RRP cutoff" if pre else "not pre-1978 housing"))
    if flag is not None and pre is not None and flag != pre:
        raise ValueError(f"year_built {year} and built_before_1978={flag} disagree; give one of them")
    pre = flag if pre is None else pre
    exposure = _num("lead_exposure_ug_m3", lead_exposure_ug_m3, 0)
    interior = _num("interior_painted_sqft_per_room", interior_painted_sqft_per_room, 0)
    exterior = _num("exterior_painted_sqft", exterior_painted_sqft, 0)
    minor, minor_why = None, ""
    if interior is not None or exterior is not None:
        never = []                          # judged task by task: 'paint window trim' + 'install drywall' is not
        for t in lead_tasks:                # a window replacement
            tw = _words(t)
            if re.search(_WINDOW_REPLACEMENT, tw) and "window replacement" not in never:
                never.append("window replacement")
            if re.search(r"\bdemo\b|demolition|demolish", tw) and "demolition of painted surfaces" not in never:
                never.append("demolition of painted surfaces")
        if never:
            minor, minor_why = False, (f"{_join(never)} is never minor repair and maintenance "
                                       f"({_join(P(R_MINOR, 'never_minor_if'), 'or')} disqualify it)")
        else:
            imax, emax = P(R_MINOR, "interior_max_sqft_per_room"), P(R_MINOR, "exterior_max_sqft")
            parts, minor = [], True
            if interior is not None:
                parts.append(f"interior {_n(interior)} sq ft per room {'<=' if interior <= imax else '>'} {_n(imax)}")
                minor = minor and interior <= imax
            if exterior is not None:
                parts.append(f"exterior {_n(exterior)} sq ft {'<=' if exterior <= emax else '>'} {_n(emax)}")
                minor = minor and exterior <= emax
            minor_why = (f"{_join(parts)} -> " + ("minor repair and maintenance, not a renovation" if minor else
                                                  "a renovation, not minor repair"))
        acc.math.append(minor_why)
    return _Ctx(classes=classes_from(employer_license_classes), building=building_from(building_type),
                year_built=year, pre1978=pre, exposure=exposure,
                lead_free=bool(_bool("lead_free_determination", lead_free_determination)), minor=minor,
                minor_why=minor_why, emergency=bool(_bool("emergency_renovation", emergency_renovation)),
                dust_clearance=bool(_bool("dust_clearance_testing", dust_clearance_testing)),
                crane_gi=bool(_bool("crane_outside_construction", crane_outside_construction)),
                out_of_state=bool(_bool("out_of_state_workers", out_of_state_workers)),
                city_la=is_city_of_los_angeles(city), day=day,
                interior=(interior is None and exterior is None) or bool(interior),
                exterior=(interior is None and exterior is None) or bool(exterior))


def _electrical_need(sub: str, ctx: _Ctx) -> _Need:
    va = _n(P(E_DEF, "covered_min_volt_amperes"))
    cls = ctx.classes
    c10 = cls is not None and "C-10" in cls
    if (sub == "low_voltage" and cls and "C-7" in cls) or (sub == "sign" and cls and "C-45" in cls):
        lic = "C-7 low voltage" if sub == "low_voltage" else "C-45 electric sign"
        return _Need("electrical", sub, "not_required", (
            f"Work done for your {lic} license and within its scope (including incidental and supplemental work, "
            f"B&P {P(E_C7, 'includes_incidental_and_supplemental_work_per_bp')}) needs no electrician certification"
            + (", even though you also hold C-10" if c10 else "") + ". Work outside that scope done for a C-10 still "
            "does."), [E_C7])
    if cls is not None and not c10:
        return _Need("electrical", sub, "not_required", (
            "Electrician certification covers only people doing electrical work for a C-10 electrical contractor; "
            f"your classes: {', '.join(sorted(cls))}. Make sure the electrical work itself is within your license "
            "classification (classification_for_scope)."), [E_C10])
    residential = ctx.building in _RESIDENTIAL
    accept = {"general": [GE], "sign": [GE], "utility_hv": [GE], "fire_alarm": [FLS, GE], "low_voltage": [VDV, GE],
              "lighting_retrofit": [GE] if residential else [NRLT, GE]}[sub]
    maybe = []
    if residential:
        accept = accept + [RE]
    elif ctx.building in (None, "senior_or_care"):        # unknown building, or a care facility (NEC Art. 517?)
        maybe = [RE]
    ids = [E_C10, E_DEF, E_TYPES] + ([E_RES] if (RE in accept or maybe) else []) \
        + ([E_SPEC] if set(accept) & {FLS, VDV, NRLT} else []) + [E_APPR, E_TRAINEE, E_RATIO, E_QUAL]
    alt = [TRAINEE, APPRENTICE] + ([NRLT_TRAINEE] if NRLT in accept else [])
    if NRLT in accept:
        ids.append(E_NRLT_T)
    if sub == "utility_hv":
        exempt = _join(P(E_UTIL, "exempt_employers"), "or")
        return _Need("electrical", sub, "conditional", (
            f"Work on a {P(E_UTIL, 'exempt_system')} needs no certification when the workers are employed by {exempt}. "
            f"Other C-10 work making connections of {va} volt-amperes or more does."), ids + [E_UTIL], accept, alt,
            maybe)
    if cls is None:
        return _Need("electrical", sub, "conditional", (
            f"Required if your company is a C-10 electrical contractor: anyone making electrical connections of {va} "
            "volt-amperes or more for a C-10 must be certified (C-7 low-voltage and C-45 sign work within those "
            "license scopes is exempt). Give employer_license_classes to settle it."), ids + [E_C7], accept, alt,
            maybe)
    foremen = (", including foremen who sometimes work with the tools" if
               P(E_DEF, "foremen_who_use_tools_must_be_certified") else "")
    return _Need("electrical", sub, "required", (
        f"Your company holds C-10, so anyone making electrical connections of {va} volt-amperes or more for it must be "
        f"certified{foremen}. Connections under {va} VA are not covered."), ids, accept, alt, maybe)


def _rrp_need(ctx: _Ctx) -> _Need:
    cutoff = P(R_APPLY, "built_before_year")
    ids = [R_APPLY]

    def no(why, *more):
        return _Need("lead_rrp", None, "not_required", why, ids + list(more))
    if ctx.building == "industrial":
        return no("Industrial buildings are not target housing or child-occupied facilities.")
    if ctx.lead_free:
        return no("A written determination that the components are lead-free ("
                  + _join(P(R_APPLY, "exemption_methods"), "or") + f"; lead-free means below "
                  f"{_n(P(R_APPLY, 'lead_free_threshold_mg_per_cm2'))} mg/cm2 or "
                  f"{_n(P(R_APPLY, 'lead_free_threshold_percent_by_weight'))}% by weight) takes the job out of RRP. "
                  "Keep it with the job records.")
    if ctx.pre1978 is False:
        return no(f"Built {ctx.year_built or f'in or after {cutoff}'}: not pre-{cutoff} target housing or a "
                  "child-occupied facility.")
    if ctx.minor is True:
        return no(f"{ctx.minor_why[0].upper() + ctx.minor_why[1:]}. Jobs in the same room within "
                  f"{P(R_MINOR, 'aggregation_window_days')} days count as one job, and using a prohibited practice "
                  "makes it a renovation.", R_MINOR)
    more = [R_MINOR] if ctx.minor is False else []
    excluded = _join(P(R_APPLY, "excluded"), "and")
    if ctx.building in _HOUSING:
        base, where = "required", f"pre-{cutoff} housing (target housing; excluded: {excluded})"
    elif ctx.building == "child_occupied":
        base, where, more = "required", f"a pre-{cutoff} child-occupied facility", more + [R_COF]
    elif ctx.building in _NOT_TARGET_UNLESS_CHILD:
        what = (f"housing for the elderly (reserved for households with someone {P(R_EXCL, 'elderly_housing_min_age_at_initial_occupancy')}"
                " or older at initial occupancy) or for persons with disabilities"
                if ctx.building == "senior_or_care" else
                f"a 0-bedroom dwelling ({_join(P(R_EXCL, 'zero_bedroom_examples'))})")
        base, more = "conditional", more + [R_EXCL, R_COF]
        where = (f"pre-{cutoff} target housing. {what[0].upper() + what[1:]} is not target housing unless a child "
                 f"under {P(R_EXCL, 'child_age_lt')} lives or is expected to live there; it is still covered where "
                 "part of it is a child-occupied facility")
    elif ctx.building in ("school", "public_or_commercial", "hotel_motel"):
        d = (f"visited by the same child under {P(R_COF, 'child_age_lt')} on at least {P(R_COF, 'min_days_per_week')} "
             f"days a week, {P(R_COF, 'min_hours_per_visit_day')} hours a day, {P(R_COF, 'min_hours_per_week')} hours "
             f"a week and {P(R_COF, 'min_hours_per_year')} hours a year")
        base, more = "conditional", more + [R_COF]
        where = (f"pre-{cutoff} space that is a child-occupied facility ({d})"
                 + (" or is used as housing" if ctx.building == "hotel_motel" else ""))
    else:
        base, where = "conditional", f"pre-{cutoff} housing or a child-occupied facility"
    status = base if ctx.pre1978 else "conditional"
    why = (f"EPA's RRP rule covers paid renovation, repair or painting that disturbs paint in {where}."
           + ("" if ctx.pre1978 else f" Give year_built: it applies only if the building was built before {cutoff}.")
           + (f" {ctx.minor_why[0].upper() + ctx.minor_why[1:]}." if ctx.minor is False else ""))
    return _Need("lead_rrp", None, status, why, ids + more + [R_REFRESH, R_ONSITE, R_TRAINED],
                 accept=[RENOVATOR], alt=[RRP_TRAINED])


def _cdph_need(ctx: _Ctx) -> _Need:
    lim = P(L_CDPH, "exposure_twa_8hr_ug_per_m3_gte")
    ids = [L_CDPH, L_DEF]
    rel = P(L_CDPH, "related_values_ug_per_m3")
    accept = [CDPH_WORKER, CDPH_SUP]
    if ctx.building == "industrial":
        return _Need("lead_cdph", None, "not_required", (
            f"CDPH certification covers lead-related construction in {_join(P(L_DEF, 'covered_buildings'), 'and')} "
            f"buildings; {_join(P(L_DEF, 'excluded_buildings'))} buildings are excluded. Cal/OSHA's lead standard "
            f"(PEL {_n(rel['cal_osha_pel_8hr'])} ug/m3) still applies."), ids)
    if ctx.exposure is None:
        return _Need("lead_cdph", None, "conditional", (
            f"Required for each worker and supervisor shown to be exposed to lead at or above {_n(lim)} ug/m3 (8-hour "
            "TWA) on lead-related construction in a residential or public building. No exposure result was given: "
            f"assess exposure first (Cal/OSHA action level {_n(rel['cal_osha_action_level_8hr'])}, PEL "
            f"{_n(rel['cal_osha_pel_8hr'])} ug/m3)."), ids, accept)
    if ctx.exposure >= lim:
        where = "" if ctx.building else " (unless the building is industrial)"
        return _Need("lead_cdph", None, "required", (
            f"Exposure {_n(ctx.exposure)} ug/m3 >= {_n(lim)} ug/m3 (8-hour TWA) on lead-related construction in a "
            f"residential or public building{where}: every exposed worker and supervisor needs CDPH certification."),
            ids, accept)
    return _Need("lead_cdph", None, "not_required", (
        f"Exposure {_n(ctx.exposure)} ug/m3 < {_n(lim)} ug/m3 (8-hour TWA): CDPH certification is not triggered. "
        f"Cal/OSHA's lead training program still applies to employees "
        f"{P(L_CDPH, 'calosha_training_program_trigger').split(' (')[0]} "
        f"({_n(rel['cal_osha_action_level_8hr'])} ug/m3)."), ids)


def _crane_need(ctx: _Ctx, sub: Optional[str] = None) -> _Need:
    still = ("; the training and qualification rules of 8 CCR 5006 still apply" if
             P(C_EXC, "section_5006_training_still_applies") else "")
    if sub == "derrick_sideboom":
        return _Need("crane", sub, "not_required", (
            f"8 CCR 5006.2 operator certification and evaluation do not apply to operators of "
            f"{_join(P(C_EXC, 'exempt_equipment'))}{still}."), [C_EXC])
    if sub == "digger_derrick" and P(C_EXC, "digger_derricks_exempt"):
        return _Need("crane", sub, "not_required", (
            "Operators of electric line trucks (digger derricks) regulated by the High Voltage Electrical Safety "
            f"Orders are outside 8 CCR 5006.2. This does not cover {P(C_EXC, 'digger_derrick_exception_excludes')}: "
            "their operators need certification."), [C_EXC])
    ids = [C_EXAM, C_GOV, C_EVAL, C_OIT, C_EXC] + ([C_GI] if ctx.crane_gi else [])
    why = ("Each crane operator in construction must hold a certificate from an accredited certifying entity for the "
           "equipment type, or a qualifying government license, AND be evaluated by the employer. Not required for "
           f"equipment rated {_n(P(C_EXC, 'exempt_if_rated_capacity_lb_lte'))} lb or less, or for derricks and "
           f"sideboom cranes{still}.")
    if sub == "knuckle_boom_delivery":
        lim = P(C_EXC, "delivery_knuckle_boom_exempt_if")
        return _Need("crane", sub, "conditional", (
            f"An articulating/knuckle-boom crane {P(C_EXC, 'delivery_knuckle_boom_condition')} needs no certified "
            f"operator if its boom is under {_n(lim['boom_ft_lt'])} ft or its rated capacity is under "
            f"{_n(lim['or_capacity_lb_lt'])} lb (check the data plate). Larger, or used for anything other than "
            "delivery: " + why), ids, accept=[CRANE_CERT, CRANE_GOV], alt=[CRANE_OIT])
    if ctx.crane_gi:
        why += (f" Outside construction (8 CCR 5006.1) the certificate is valid up to "
                f"{P(C_GI, 'certificate_max_validity_years')} years; mobile cranes with a boom under "
                f"{_n(P(C_GI, 'exempt_mobile_crane_if_boom_ft_lt'))} ft or a rated capacity under "
                f"{_n(P(C_GI, 'exempt_mobile_crane_if_capacity_lb_lt'))} lb are exempt there.")
    return _Need("crane", None, "required", why, ids, accept=[CRANE_CERT, CRANE_GOV], alt=[CRANE_OIT])


def _elevator_need(ctx: _Ctx) -> _Need:
    if ctx.building == "single_family":
        return _Need("elevator", None, "not_required", (
            "The Elevator Safety Orders do not cover an elevator in a single-unit private home that is not accessible "
            "to the public, so DOSH conveyance certification and permits do not apply (other license law still "
            "does: elevator work is C-11)."), [V_EXCL])
    ids = [V_CCCM_ONLY, V_CCCM, V_CQCC, V_TEMP] + ([V_EXCL] if ctx.building in ("two_unit", "residential") else [])
    why = (f"DOSH states that {_join(P(V_CCCM_ONLY, 'covered_work'))} on any elevator, escalator, lift or other "
           "conveyance covered by the Elevator Safety Orders may be done only by a Certified Competent Conveyance "
           "Mechanic (CCCM) working for a Certified Qualified Conveyance Company (CQCC). New installations are filed "
           "and inspected through the installing elevator company.")
    if ctx.building == "residential":
        why += " If it is a single-unit private home not open to the public, the Elevator Safety Orders do not apply."
    return _Need("elevator", None, "required", why, ids, accept=[CCCM, TCCCM])


def _blasting_need(ctx: _Ctx) -> _Need:
    return _Need("blasting", None, "required", (
        "No blasting may happen unless a blaster with a current California Blaster's License from Cal/OSHA is "
        "physically on site doing or directing it; others may work under the blaster's direct supervision."),
        [B_ONSITE, B_QUAL, B_RENEW], accept=[BLASTER], anyone_supervised=True)


def _needs(trade: str, sub: Optional[str], ctx: _Ctx) -> list[_Need]:
    if trade == "electrical":
        return [_electrical_need(sub, ctx)]
    if trade == "lead":
        return [_rrp_need(ctx), _cdph_need(ctx)]
    if trade == "crane":
        return [_crane_need(ctx, sub)]
    if trade == "elevator":
        return [_elevator_need(ctx)]
    if trade == "blasting":
        return [_blasting_need(ctx)]
    if trade == "personnel_hoist":
        ids = [H_PERMIT, H_INSPECT] + ([H_PERM_ELEV] if sub == "permanent_elevator" else []) + [V_EXCL]
        return [_Need("personnel_hoist", sub, "required", "The hoist needs a DOSH permit to operate; the operator "
                      "needs no state credential.", ids)]
    if trade == "pressure_vessel":
        ids = {"air": [PV_AIR, PV_AIR_SMALL], "lpg": [PV_LPG], "boiler": [PV_BOILER, PV_BOILER_SMALL]}[sub]
        return [_Need("pressure_vessel", sub, "conditional", "The equipment may need a permit to operate; the "
                      "operator needs no state credential.", ids + ([PV_LA] if ctx.city_la else []))]
    raise KeyError(trade)


# ================================================================ certifications_required
class CertRequirement(BaseModel):
    trade: Trade
    credential: str = Field(description="The certification, license or permit")
    who: str = Field(description="Who must hold it: each worker doing the work, one person on site, the firm, the "
                                 "equipment")
    status: Status
    tasks: list[str] = Field(default_factory=list, description="Your tasks that triggered it")
    issuer: Optional[str] = None
    acceptable: list[str] = Field(default_factory=list, description="Credentials that satisfy it")
    alternatives: list[str] = Field(default_factory=list, description="Lawful alternatives (usually supervised)")
    proof_on_site: Optional[str] = None
    cost_usd: Optional[float] = Field(None, description="Official fee to get it, per person or firm, when fixed")
    cost_detail: Optional[str] = None
    why: str = ""
    how_to_get: Optional[str] = None
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


class CertificationsRequiredResult(_Base):
    requirements: list[CertRequirement] = Field(default_factory=list)
    required_count: int = 0
    conditional_count: int = 0
    unrecognized_tasks: list[str] = Field(default_factory=list)
    exposure: list[str] = Field(default_factory=list, description="What it costs to get it wrong, with citations")
    employer_license_classes: list[str] = Field(default_factory=list)
    building_type: Optional[str] = None
    pre_1978: Optional[bool] = None


def _req(acc: _Acc, trade, credential, who, status, tasks, ids, **kw) -> CertRequirement:
    acc.use(*ids)
    ids = list(dict.fromkeys(ids))
    return CertRequirement(trade=trade, credential=credential, who=who, status=status, tasks=list(tasks),
                           rule_ids=ids, citation=_cit(ids), verified=_ok(ids), **kw)


def _accept_label(kind: str) -> str:
    if kind == GE:
        return f"{_etype(GE)} (covers all categories)"
    if kind == RE:
        return (f"{_etype(RE)} (only {_join(P(E_RES, 'occupancies'))} whose primary occupancy is residential and whose "
                f"utility service is no more than {P(E_RES, 'max_utility_service')}; no tenant improvements; no "
                f"NEC Article {_join(P(E_RES, 'excluded_nec_articles'))} occupancies; panels and feeders for "
                f"commercial space only if under {_n(P(E_RES, 'max_first_floor_commercial_pct_lt'))}% of the first "
                "floor is commercial)")
    if kind == FLS:
        return f"{_etype(FLS)} (NEC Article {_join(P(E_SPEC, 'fire_life_safety_nec_articles'))} systems)"
    if kind == VDV:
        return f"{_etype(VDV)} (NEC Articles {_join(P(E_SPEC, 'voice_data_video_nec_articles'))})"
    if kind == NRLT:
        return (f"{_etype(NRLT)} (repair, service and maintain existing nonresidential fixtures and install retrofit "
                "fixtures; no branch-circuit work except reconnecting to existing power within "
                f"{_n(P(E_SPEC, 'nonresidential_lighting_reconnect_max_ft'))} ft)")
    return _kind_label(kind)


def _re_maybe_text(building: Optional[str]) -> str:
    """Why a Residential Electrician certification is only possibly enough (8 CCR 290.1)."""
    if building == "senior_or_care":
        return (f"only if this senior or care building is not an occupancy under NEC Article "
                f"{_join(P(E_RES, 'excluded_nec_articles'))}, which a Residential Electrician may not work in; "
                "confirm against the electrical plans")
    return "only if the building is residential (give building_type)"


def _electrical_reqs(acc, need: _Need, tasks, ctx) -> list[CertRequirement]:
    label = f"DIR electrician certification for {_SUB_LABEL[need.sub]}"
    if need.status == "not_required":
        return [_req(acc, "electrical", label, "each worker doing the work", "not_required", tasks, need.ids,
                     why=need.why)]
    app, exam, total = (P(E_FEE, "application_fee_usd"), P(E_FEE, "exam_fee_usd"), P(E_FEE, "total_initial_usd"))
    t_fee, ratio = P(E_FEE_TRAINEE, "trainee_registration_fee_usd"), P(E_RATIO, "max_trainees_per_certified_electrician")
    acc.math.append(f"certification cost per worker = {_usd(app)} application + {_usd(exam)} exam = {_usd(total)}")
    alts = [f"Registered apprentice in a state- or federally-approved program (the C-10 must supervise them; an "
            f"apprentice within {P(E_APPR, 'may_take_exam_within_months_of_completion')} months of finishing may take "
            "the exam)",
            f"Registered electrician trainee ({_usd(t_fee)}, enrolled in a DIR-approved curriculum) under direct, "
            f"on-site supervision of a certified electrician who supervises no more than {ratio} trainee "
            "(apprentices do not count toward that limit)",
            "The C-10's qualifying person (the one who passed the CSLB exam). Partners and officers who are not the "
            "qualifier, and C-10 licensees working as another C-10's employee, must be certified"]
    if NRLT in need.accept:
        alts.append(f"Nonresidential lighting trainee {P(E_NRLT_T, 'conditions')[0]}, supervised on site by a "
                    "certified Nonresidential Lighting Technician")
    acceptable = [_accept_label(k) for k in need.accept + need.maybe]
    if need.maybe:
        acceptable[-1] += " - " + _re_maybe_text(ctx.building)
    ids = need.ids + [E_PRESUME, E_FEE, E_FEE_TRAINEE] + ([E_NOGRACE] if ctx.out_of_state else [])
    why = need.why + (" Out-of-state electricians must be California-certified before they start: there is no grace "
                      "period, even for a short job." if ctx.out_of_state else "")
    return [_req(acc, "electrical", label, "each worker doing the work", need.status, tasks, ids,
                 issuer=P(E_C10, "certifying_agency"), acceptable=acceptable, alternatives=alts,
                 proof_on_site=("Each worker's DIR certification card (or trainee T-number letter and card, or "
                                "apprentice registration). Keep copies on file and at the jobsite: without evidence "
                                "of certification or trainee status a violation is presumed, and CSLB must start any "
                                f"discipline within {P(E_PRESUME, 'cslb_discipline_initiation_days_after_referral')} "
                                "days of a referral."),
                 cost_usd=total, cost_detail=(f"{_usd(app)} application + {_usd(exam)} exam = {_usd(total)} per worker "
                                              f"(each retake {_usd(P(E_FEE, 'retake_exam_fee_usd'))}); trainee "
                                              f"registration {_usd(t_fee)}"),
                 why=why, how_to_get=("Collect a copy of each electrician's DIR card before they start. Workers who "
                                      "are not certified: run electrician_certification_path with their hours by work "
                                      "area; until certified they may work only as registered trainees (1 per "
                                      "certified electrician) or apprentices."))]


def _rrp_reqs(acc, need: _Need, tasks, ctx) -> list[CertRequirement]:
    if need.status == "not_required":
        return [_req(acc, "lead_rrp", "EPA RRP certification (renovator and firm)", "the firm and its renovators",
                     "not_required", tasks, need.ids, why=need.why)]
    due, hands, no_hands = (P(R_REFRESH, "refresher_due_years_after_initial"),
                            P(R_REFRESH, "certification_years_with_hands_on_refresher"),
                            P(R_REFRESH, "certification_years_without_hands_on_refresher"))
    present = _join(P(R_ONSITE, "must_be_physically_present_during"))
    steps = []
    ids = list(need.ids)
    if ctx.interior:
        steps.append(f"interior: cover the floor at least {_n(P(R_INT, 'floor_cover_beyond_work_surfaces_ft'))} ft "
                     "beyond the work surfaces (farther if needed), close and cover ducts and doors")
        ids.append(R_INT)
    if ctx.exterior:
        steps.append(f"exterior: close doors and windows within {_n(P(R_EXT, 'close_doors_windows_within_ft'))} ft, "
                     f"cover the ground at least {_n(P(R_EXT, 'ground_cover_beyond_work_surfaces_ft'))} ft beyond the "
                     "work, vertical containment if within "
                     f"{_n(P(R_EXT, 'vertical_containment_if_within_property_line_ft'))} ft of the property line")
        ids.append(R_EXT)
    steps.append(f"after the work the certified renovator does cleaning verification: clean "
                 f"{_n(P(R_CLEAN, 'clean_beyond_work_area_ft'))} ft beyond the work area, wipe in sections under "
                 f"{_n(P(R_CLEAN, 'max_section_sqft_lt'))} sq ft against the EPA verification card, signs come down "
                 "only after it passes")
    ids.append(R_CLEAN)
    emergency = ""
    if ctx.emergency:
        ids.append(R_EMERG)
        emergency = (" Emergency renovation: the sign, containment, waste, training and certification rules are "
                     "waived only to the extent needed to handle the emergency (interim controls for a child's "
                     "elevated blood lead get only the pamphlet exemption); cleaning, cleaning verification by a "
                     "certified renovator and the emergency records are still required.")
    out = [_req(acc, "lead_rrp", "EPA certified renovator assigned to the job", "at least one person, assigned to "
                "this job", need.status, tasks, ids,
                issuer="EPA-accredited training provider (course certificate)",
                acceptable=[f"Initial EPA renovator course certificate, refreshed within {due} years ({hands} years if "
                            f"the refresher had hands-on training, {no_hands} without)"],
                proof_on_site=f"Copies of the renovator's {_join(P(R_ONSITE, 'documents_at_worksite'))} at the job.",
                why=need.why + emergency,
                how_to_get=(f"Name the certified renovator. They must be on site during {present}, reachable on site "
                            "or by phone the rest of the time, and personally do the cleaning verification. Set-up: "
                            + "; ".join(steps) + ".")),
           _req(acc, "lead_rrp", "Every other worker trained by the certified renovator (or certified)",
                "each worker disturbing paint", need.status, tasks, [R_TRAINED] + need.ids[:1],
                acceptable=list(P(R_TRAINED, "worker_options")),
                proof_on_site=f"Job records with {P(R_TRAINED, 'documentation_includes')}.",
                why="Everyone doing the renovation must be a certified renovator or trained on the job by the "
                    "certified renovator in the lead-safe practices they will use, and the firm documents it.",
                how_to_get="Have the certified renovator train each uncertified worker before they start and record "
                           "the topics per worker.")]
    fee, combo = P(R_FEE, "firm_certification_usd"), P(R_FEE, "combined_renovation_and_lbp_activities_firm_usd")
    yrs, timely = P(R_RECERT, "recertification_interval_years"), P(R_RECERT, "timely_if_days_before_expiration_gte")
    out.append(_req(acc, "lead_rrp", "EPA RRP firm certification", "the firm", need.status, tasks,
                    [R_FIRM, R_FEE, R_RECERT, R_AMEND] + need.ids[:1], issuer="U.S. EPA (CDX)",
                    cost_usd=fee, cost_detail=(f"{_usd(fee)} initial, {_usd(P(R_FEE, 'firm_recertification_usd'))} every "
                                               f"{yrs} years to recertify; {_usd(combo)} combined renovation and "
                                               "lead-based paint activities firm"),
                    why=(f"A firm may not perform or offer renovations in pre-{P(R_APPLY, 'built_before_year')} target "
                         "housing or child-occupied facilities without EPA firm certification."),
                    how_to_get=(f"Check the firm certificate's expiration date; file recertification electronically at "
                                f"least {timely} days before it (then it stays valid while EPA decides), and amend "
                                f"within {P(R_AMEND, 'amend_within_days_of_change')} days of a change to the "
                                "application information.")))
    if ctx.building == "child_occupied":
        out.append(_req(acc, "lead_rrp", "Child-occupied facility notices", "the firm", need.status, tasks,
                        [R_COF_NOTICE],
                        why=(f"No more than {P(R_COF_NOTICE, 'max_days_before_start')} days before work, give the "
                             f"Renovate Right pamphlet to {_join(P(R_COF_NOTICE, 'recipients'))}."),
                        how_to_get="Deliver the pamphlet (signed receipt, or certificate of mailing at least "
                                   f"{P(R_COF_NOTICE, 'certificate_of_mailing_min_days_before')} days before), "
                                   "inform parents by mail, hand delivery or posted signs, and sign a statement of the "
                                   "steps taken."))
    if ctx.dust_clearance:
        lv = P(R_DUST, "action_levels_ug_per_sqft_from_2026_01_12") \
            if ctx.day >= date.fromisoformat(P(R_DUST, "change_date")) else \
            P(R_DUST, "action_levels_ug_per_sqft_before_2026_01_12")
        out.append(_req(acc, "lead_rrp", "Dust clearance sampling by a certified professional", "a certified "
                        "inspector, risk assessor or dust sampling technician", need.status, tasks, [R_DUST],
                        acceptable=P(R_DUST, "samplers_allowed"),
                        why=("Dust clearance sampling replaces cleaning verification only when the contract or "
                             "another law requires it; re-clean until results are below the action levels: "
                             + ", ".join(f"{k.replace('_', ' ')} {v} ug/sq ft" for k, v in lv.items()) + "."),
                        how_to_get=("Book the sampler; give the owner and occupant the report within "
                                    f"{P(R_DUST, 'report_to_owner_and_occupant_within_days')} days.")))
    return out


def _cdph_req(acc, need: _Need, tasks) -> CertRequirement:
    kw = {}
    ids = list(need.ids)
    if need.status != "not_required":
        ids += [L_PROVIDER, L_SUP, L_RECORDS]
        kw = dict(issuer=P(L_CDPH, "certifying_agency"),
                  acceptable=["CDPH lead-related construction certificate (worker or supervisor)"],
                  proof_on_site=("Each exposed worker's and supervisor's CDPH certificate; provide training and "
                                 f"certification records on request to {_join(P(L_RECORDS, 'provide_to'))}."),
                  how_to_get=(f"Training must come from a CDPH-accredited provider ({P(L_PROVIDER, 'regulation')}); "
                              "EPA-only training does not satisfy this. Supervisors are those who can spot lead "
                              "hazards and fix them promptly. CDPH fees and renewal terms are not in this rulebook "
                              "(CDPH's pages could not be read)."))
    return _req(acc, "lead_cdph", "CDPH lead certification", "each exposed worker and supervisor", need.status, tasks,
                ids, why=need.why, **kw)


def _oit_limits() -> str:
    return ("an operator-in-training may not run the crane, unless certified, for: "
            + _join(P(C_OIT, "trainee_prohibited_unless_certified"), "or"))


def _crane_req(acc, need: _Need, tasks) -> CertRequirement:
    gov = P(C_GOV, "max_validity_years")
    if need.status == "not_required":
        return _req(acc, "crane", "Crane operator certification (8 CCR 5006.2)", "each operator", "not_required",
                    tasks, need.ids, why=need.why)
    yrs = P(C_EXAM, "option1_certificate_max_validity_years")
    return _req(acc, "crane", "Crane operator certification and employer evaluation", "each crane operator",
                need.status, tasks, need.ids, issuer="Accredited certifying entity (Option 1) or a government "
                "licensing office (Option 2)",
                acceptable=[f"Certificate from an accredited certifying entity for the equipment type (after "
                            f"{_join(P(C_EXAM, 'required_components'))}); portable statewide; valid at most {yrs} "
                            f"years, then recertify every {P(C_EXAM, 'recertify_every_years')} years",
                            f"Government license: valid only within the issuing jurisdiction and for at most {gov} "
                            "years"],
                alternatives=["Operator-in-training after formal and practical instruction, continuously monitored "
                              "on site by a trainer who holds a valid certificate for that crane type, stays within "
                              "sight and communication, and has no other duties while observing; " + _oit_limits()],
                cost_detail=("The employer must provide the certification or license at no cost to the employee."
                             if P(C_EXAM, "employer_provides_at_no_cost_to_employee") else None),
                proof_on_site=(f"The employer's written evaluation ({_join(P(C_EVAL, 'record_contents'))}) kept at the "
                               "worksite while the operator is employed, plus the certificate."),
                why=need.why, how_to_get=("Copy each operator's certificate and confirm it lists the crane type; "
                                          "have a qualified evaluator (your employee or agent) evaluate them on the "
                                          "equipment and lifts they will do, and re-evaluate after any retraining. "
                                          "The crane's own annual certification and any tower crane permit are "
                                          "covered by generate_safety_programs and check_cal_osha_permits."))


def _elevator_reqs(acc, need: _Need, tasks) -> list[CertRequirement]:
    if need.status == "not_required":
        return [_req(acc, "elevator", "DOSH conveyance mechanic and company certification", "the installing "
                     "company and its mechanics", "not_required", tasks, need.ids, why=need.why)]
    yrs, fee, exam = P(V_CCCM, "min_experience_years"), P(V_CCCM, "application_fee_usd"), P(V_CCCM, "exam_fee_usd")
    gl, pd = (P(V_CQCC, "min_general_liability_bodily_injury_per_occurrence_usd"),
              P(V_CQCC, "min_property_damage_per_occurrence_usd"))
    return [_req(acc, "elevator", "Certified Competent Conveyance Mechanic (CCCM)", "each mechanic working on the "
                 "conveyance", need.status, tasks, need.ids, issuer="Cal/OSHA (DOSH) Elevator Unit",
                 acceptable=[f"CCCM: {yrs} years of conveyance experience verified by the CQCC employer plus "
                             f"{_join(P(V_CCCM, 'qualifying_routes'), 'or')}; valid {P(V_CCCM, 'validity_years')} "
                             "years"],
                 alternatives=[f"Temporary mechanic (TCCCM) when the employer cannot find enough CCCMs: "
                               f"{_usd(P(V_TEMP, 'fee_usd'))}, first term up to {P(V_TEMP, 'initial_term_months')} "
                               "months"],
                 cost_usd=fee, cost_detail=f"{_usd(fee)} application (+{_usd(exam)} if taking the DOSH exam)",
                 proof_on_site="Each mechanic's CCCM (or TCCCM) certificate.", why=need.why,
                 how_to_get="Use only an elevator company that is a CQCC and whose mechanics hold current CCCM cards; "
                            "other trades (electricians, GC staff) must not work on the conveyance equipment."),
            _req(acc, "elevator", "Certified Qualified Conveyance Company (CQCC)", "the elevator company",
                 need.status, tasks, [V_CQCC, V_NEW, V_PERMIT], issuer="Cal/OSHA (DOSH) Elevator Unit",
                 cost_usd=P(V_CQCC, "initial_fee_usd"),
                 cost_detail=(f"{_usd(P(V_CQCC, 'initial_fee_usd'))} initial, {_usd(P(V_CQCC, 'renewal_fee_usd'))} "
                              f"renewal every {P(V_CQCC, 'certification_term_years')} years"),
                 why=(f"The company needs a current C-11 license, workers' comp, and liability insurance of at least "
                      f"{_usd(gl)} per occurrence (injury/death) and {_usd(pd)} (property damage)."),
                 how_to_get=("The installing CQCC files plans or a notice of intent and requests the acceptance "
                             "inspection; the device needs a DOSH permit to operate before service (run "
                             "equipment_operating_permits for fees and timing)."))]


def _trade_work_rrp_text() -> str:
    imax, emax = P(R_TRADES, "interior_max_sqft_per_room"), P(R_TRADES, "exterior_max_sqft")
    return (f"EPA counts {_join(P(R_TRADES, 'trades_named'))} as exempt minor repair and maintenance only while it "
            f"disturbs {_n(imax)} sq ft or less of painted surface per room inside and {_n(emax)} sq ft or less "
            "outside" + (" (a removed painted component counts in full)" if
                         P(R_TRADES, "removed_component_counts_entire_area") else "")
            + ", with no window replacement, demolition of painted surfaces or prohibited practices. Cutting into "
            "painted walls or ceilings beyond that is a renovation under the RRP rule.")


def certifications_required(tasks, employer_license_classes=None, year_built=None, building_type=None,
                            built_before_1978=None, lead_exposure_ug_m3=None, lead_free_determination=False,
                            interior_painted_sqft_per_room=None, exterior_painted_sqft=None,
                            emergency_renovation=False, dust_clearance_testing=False, crane_outside_construction=False,
                            out_of_state_workers=False, city=None, as_of=None) -> CertificationsRequiredResult:
    if isinstance(tasks, str):
        tasks = [tasks]
    tasks = [str(t).strip() for t in (tasks or []) if t is not None and str(t).strip()]
    if not tasks:
        raise ValueError("tasks is required: list the work, e.g. ['rough wiring', 'scrape and repaint trim', "
                         "'crane pick of HVAC units']")
    day = _as_of(as_of)
    acc = _Acc()
    groups: dict = {}
    unrecognized = []
    for t in tasks:
        hits = classify_task(t)
        if not hits:
            unrecognized.append(t)
        for h in hits:
            groups.setdefault(h, []).append(t)
    lead_tasks = groups.get(("lead", None), [])
    ctx = _context(employer_license_classes, year_built, built_before_1978, building_type, lead_exposure_ug_m3, day,
                   lead_free_determination, interior_painted_sqft_per_room, exterior_painted_sqft, lead_tasks,
                   emergency_renovation, dust_clearance_testing, crane_outside_construction, out_of_state_workers, city,
                   acc)
    order = ["electrical", "lead", "crane", "elevator", "personnel_hoist", "blasting", "pressure_vessel"]
    reqs: list[CertRequirement] = []
    for (trade, sub), ts in sorted(groups.items(), key=lambda kv: order.index(kv[0][0])):
        for need in _needs(trade, sub, ctx):
            if need.trade == "electrical":
                reqs += _electrical_reqs(acc, need, ts, ctx)
            elif need.trade == "lead_rrp":
                reqs += _rrp_reqs(acc, need, ts, ctx)
            elif need.trade == "lead_cdph":
                reqs.append(_cdph_req(acc, need, ts))
            elif need.trade == "crane":
                reqs.append(_crane_req(acc, need, ts))
            elif need.trade == "elevator":
                reqs += _elevator_reqs(acc, need, ts)
            elif need.trade == "blasting":
                q = P(B_QUAL, "classes")
                reqs.append(_req(acc, "blasting", "Licensed blaster physically on site", "at least one person on "
                                 "site during blasting", "required", ts, need.ids, issuer=P(B_ONSITE, "license_issuer"),
                                 acceptable=["California Blaster's License, class " + ", ".join(
                                     f"{k} ({v})" for k, v in q.items())],
                                 alternatives=["Others may do blasting work under the licensed blaster's direct "
                                               "supervision to gain experience"],
                                 proof_on_site="The blaster's license, shown to Cal/OSHA or the local authority on "
                                               "request (licenses are not transferable).",
                                 why=need.why,
                                 how_to_get=(f"Licensed blasters must be at least {P(B_QUAL, 'min_age_years')}, have "
                                             f"{P(B_QUAL, 'min_experience_years_in_category')} years of experience in "
                                             "the class, and pass DOSH's exam; the license is good for "
                                             f"{P(B_RENEW, 'validity_years')} years. Local explosives permits were "
                                             "not researched: ask the fire authority.")))
            elif need.trade == "personnel_hoist":
                interval = P(H_INSPECT, "periodic_inspection_interval_months_max")
                extra = (" A permanent building elevator may serve as a construction hoist only if it meets the "
                         "personnel hoist rules, and may run on automatic operation only after DOSH issues a permit."
                         if need.sub == "permanent_elevator" else "")
                reqs.append(_req(acc, "personnel_hoist", "DOSH permit to operate for the personnel hoist",
                                 "the hoist (GC or hoist subcontractor)", "required", ts, need.ids,
                                 issuer="Cal/OSHA (DOSH) Elevator Unit",
                                 proof_on_site="The permit, posted in the car; the latest signed inspection record.",
                                 why=("No personnel hoist may carry workers without a valid DOSH permit after the "
                                      "acceptance inspection (construction elevators are not under the Elevator "
                                      "Safety Orders but under these hoist rules)." + extra),
                                 how_to_get=(f"Request the DOSH acceptance inspection before use and after moves or "
                                             f"major alterations; then have a qualified person inspect and test it at "
                                             f"least every {interval} months. Run equipment_operating_permits for "
                                             "fees and dates.")))
            elif need.trade == "pressure_vessel":
                issuer = ("Los Angeles Department of Building and Safety" if ctx.city_la else
                          "Cal/OSHA (DOSH) Pressure Vessel Unit")
                why = {"air": (f"Air tanks need a permit unless they are {_n(P(PV_AIR_SMALL, 'exempt_volume_cuft_lte'))}"
                               f" cu ft or less AND have a safety valve set at no more than "
                               f"{_n(P(PV_AIR_SMALL, 'exempt_safety_valve_set_psi_lte'))} psi; operating without one "
                               "is a misdemeanor each day."),
                       "lpg": (f"LP-gas tanks over {_n(P(PV_LPG, 'permit_required_if_capacity_gallons_gt'))} gallons "
                               "(not DOT cylinders) need a permit, and no one may fill one until it is posted."),
                       "boiler": ("Boilers subject to the Boiler Safety Orders need a permit and yearly inspection; "
                                  "small, low-pressure, miniature and high-temperature water boilers are exempt.")}
                reqs.append(_req(acc, "pressure_vessel", f"Permit to operate for {_SUB_LABEL[need.sub]}",
                                 "the equipment (owner or user)", "conditional", ts, need.ids, issuer=issuer,
                                 why=why[need.sub] + (" In the City of Los Angeles permits come from LADBS, not the "
                                                      "state." if ctx.city_la else ""),
                                 how_to_get="Run equipment_operating_permits with the tank size, safety valve "
                                            "setting and type to see whether a permit is needed and what it costs."))
    # electrical work in a pre-1978 building: 40 CFR 745.83 counts it as minor repair only up to 6 / 20 sq ft of
    # painted surface, so above that it is an RRP renovation even with no painting task on the list
    elec_tasks = list(dict.fromkeys(t for (trade, _), ts in groups.items() if trade == "electrical" for t in ts))
    if elec_tasks and ("lead", None) not in groups and ctx.pre1978:
        need = _rrp_need(ctx)
        if need.status != "not_required" or ctx.minor is True:
            need.ids = list(need.ids) + [R_TRADES]
            if need.status != "not_required" and ctx.minor is None:
                need.status = "conditional"
                need.why = (_trade_work_rrp_text() + " Give interior_painted_sqft_per_room / exterior_painted_sqft to "
                            "settle it. " + need.why)
            at = max((i for i, r in enumerate(reqs) if r.trade == "electrical"), default=-1) + 1
            reqs[at:at] = _rrp_reqs(acc, need, elec_tasks, ctx)
    res = CertificationsRequiredResult(employer_license_classes=sorted(ctx.classes or []), building_type=ctx.building,
                                       pre_1978=ctx.pre1978, unrecognized_tasks=unrecognized)
    res.requirements = reqs
    res.required_count = sum(r.status == "required" for r in reqs)
    res.conditional_count = sum(r.status == "conditional" for r in reqs)
    # exposure
    if any(r.trade == "electrical" and r.status != "not_required" for r in reqs):
        res.exposure.append(f"C-10 license: CSLB discipline up to {P(E_DISC, 'max_sanction')} for willfully "
                            f"employing even {P(E_DISC, 'threshold_uncertified_workers')} uncertified person as an "
                            "electrician, or willfully failing to supervise trainees or apprentices "
                            f"[{_cit([E_DISC])}].")
        acc.use(E_DISC)
    if any(r.trade == "lead_rrp" and r.status != "not_required" for r in reqs):
        on = date.fromisoformat(P(R_PEN, "applies_to_penalties_assessed_on_or_after"))
        amount = P(R_PEN, "max_civil_penalty_usd_per_violation") if day >= on else P(R_PEN, "prior_max_usd")
        res.exposure.append(f"EPA: civil penalties up to {_usd(amount)} per RRP violation (certification, work "
                            f"practices, pamphlet, records), plus possible criminal sanctions [{_cit([R_PEN])}].")
        acc.use(R_PEN)
    if any(PV_AIR in r.rule_ids for r in reqs):
        res.exposure.append(f"Air tanks: operating one that needs a permit without it is a "
                            f"{P(PV_AIR, 'offense_class')}, each day a separate offense [{_cit([PV_AIR])}].")
    _ab1707(acc, day if any(r.trade == "electrical" and r.status != "not_required" for r in reqs) else None)
    # summary and steps
    req_names = [r.credential for r in reqs if r.status == "required"]
    cond = [r for r in reqs if r.status == "conditional"]
    res.summary = (f"{len(req_names)} required" + (f": {_join(req_names)}" if req_names else "")
                   + (f"; {len(cond)} depend on facts to confirm" if cond else "")
                   + (f"; tasks not recognized: {_join(unrecognized)}" if unrecognized else "") + ".")
    seen: set = set()
    for r in reqs:                          # one step per distinct action (electrical subtypes share theirs)
        if r.status == "required" and r.how_to_get and r.how_to_get not in seen:
            seen.add(r.how_to_get)
            res.next_steps.append(f"{r.credential}: {r.how_to_get}")
    res.next_steps += [f"Confirm ({r.credential}): {r.why}" for r in cond]
    if unrecognized:
        res.next_steps.append("Describe these tasks in trade words (e.g. 'panel upgrade', 'scrape and paint', 'crane "
                              "pick', 'blasting'): " + _join(unrecognized))
    res.next_step = (res.next_steps[0] if res.next_steps else
                     "No trade certification in this rulebook is needed for these tasks; keep normal license and "
                     "Cal/OSHA training records.")
    if req_names or cond:
        res.next_steps.append("Then check each worker's cards with check_crew_certifications before mobilizing.")
    return acc.finish(res)


# ================================================================ check_crew_certifications
class Credential(BaseModel):
    type: str = Field(min_length=1, description=(
        "What it is: 'General Electrician', 'Residential Electrician', 'Fire/Life Safety', 'VDV', 'NRLT', "
        "'electrician trainee', 'apprentice', 'EPA certified renovator' (or 'RRP refresher'), 'trained by "
        "certified renovator', 'CDPH lead worker', 'CDPH lead supervisor', 'NCCCO crane operator', 'city crane "
        "license', 'crane operator in training', 'CCCM', 'TCCCM', 'blaster license class D'"))
    expires: Optional[str] = Field(None, description="Expiration date printed on the card, e.g. 2027-03-31")
    issued: Optional[str] = Field(None, description=(
        "Issue or course-completion date (used when there is no expiration date, e.g. an RRP course certificate)"))
    hands_on: Optional[bool] = Field(None, description="RRP refresher only: did it include hands-on training?")
    number: Optional[str] = Field(None, description="Certificate or T-number, for your records")
    jurisdiction: Optional[str] = Field(None, description="Government crane license only: the issuing city/county")


class CrewMember(BaseModel):
    name: str = Field(min_length=1)
    task: str = Field(min_length=1, description="What they will do on this job, e.g. 'pull wire and set panels'")
    credentials: list[Union[Credential, str]] = Field(default_factory=list, description=(
        "Their cards: objects {type, expires, issued, hands_on} or text like 'General Electrician exp 2027-05-31'"))
    is_c10_qualifier: bool = Field(False, description="This person is the C-10 license's qualifying individual")
    supervised_by: Optional[str] = Field(None, description=(
        "Trainees / crane trainees: name of the crew member supervising them on site"))
    employer_evaluated: Optional[bool] = Field(None, description=(
        "Crane operators: has your company documented the operator evaluation (8 CCR 5006.2(f))?"))


CredStatus = Literal["valid", "renew_soon", "renewal_overdue", "expired", "missing_info", "not_recognized"]
WorkerStatus = Literal["not_qualified", "expired", "missing_info", "confirm", "renewal_overdue", "renew_soon",
                       "ok_with_supervision", "ok", "not_required", "unrecognized_task"]
_SEVERITY = ("not_qualified", "expired", "missing_info", "confirm", "renewal_overdue", "renew_soon",
             "ok_with_supervision", "ok", "not_required", "unrecognized_task")


class CredentialCheck(BaseModel):
    given: str
    kind: Optional[str] = None
    label: Optional[str] = None
    status: CredStatus
    expires: Optional[str] = None
    renew_by: Optional[str] = Field(None, description="Act on or before this date to avoid a lapse")
    renewal_window_opens: Optional[str] = None
    days_left: Optional[int] = Field(None, description="Days from as_of to expiration (negative = expired)")
    renewal: Optional[str] = Field(None, description="What renewing takes and costs")
    issues: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)


class WorkerCheck(BaseModel):
    name: str
    task: str
    trades: list[str] = Field(default_factory=list)
    status: WorkerStatus
    can_do_task: Optional[bool] = Field(None, description="True allowed, False not allowed, None not determined")
    covered_by: Optional[str] = Field(None, description="The credential that lets them do it")
    credentials: list[CredentialCheck] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    next_step: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class CrewFinding(BaseModel):
    item: str
    status: Literal["ok", "problem", "confirm"]
    detail: str
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


class CrewCheckResult(_Base):
    as_of: str
    workers: list[WorkerCheck] = Field(default_factory=list)
    crew_findings: list[CrewFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict, description="Workers per status")
    cleared_to_start: bool = Field(False, description=(
        "True when no worker is blocked or unknown and no crew check failed (items to confirm are listed in "
        "next_steps)"))
    exposure: list[str] = Field(default_factory=list)


def _cred_from(raw, i: int) -> Credential:
    if isinstance(raw, Credential):
        return raw
    if isinstance(raw, dict):
        try:
            return Credential.model_validate(raw)
        except ValidationError as e:
            raise ValueError(f"credential {i + 1}: {e.errors()[0]['msg']} ({raw!r})") from None
    s = str(raw or "").strip()
    if not s:
        raise ValueError(f"credential {i + 1} is empty")
    m = _ISO_DATE.search(s) or _US_DATE.search(s)
    expires = None
    if m:
        expires = m.group(0)
        s = (s[:m.start()] + s[m.end():]).strip()
        s = re.sub(r"\b(exp(ires|iration|\.)?|valid (through|thru|until)|thru|until)\W*$", "", s, flags=re.I).strip(
            " ,;:-")
    return Credential(type=s or str(raw), expires=expires)


def _member(m, i: int) -> CrewMember:
    if isinstance(m, CrewMember):
        return m
    if not isinstance(m, dict):
        raise ValueError(f"crew member {i + 1} must be an object like {{'name': 'Ana', 'task': 'pull wire', "
                         f"'credentials': ['General Electrician exp 2027-05-31']}}, got {m!r}")
    try:
        return CrewMember.model_validate(m)
    except ValidationError as e:
        err = e.errors()[0]
        raise ValueError(f"crew member {i + 1} ({m.get('name') or 'no name'}): {'.'.join(map(str, err['loc']))} "
                         f"{err['msg']}") from None


def _deadline(cred: Credential, kind: Optional[str], day: date, acc: _Acc) -> CredentialCheck:
    """Status and renew-by date for one credential."""
    chk = CredentialCheck(given=cred.type + (f" (exp {cred.expires})" if cred.expires else ""), kind=kind,
                          label=_kind_label(kind) if kind else None, status="missing_info")
    if kind is None:
        chk.status = "not_recognized"
        chk.issues.append(f"Credential {cred.type!r} not recognized; name it like 'General Electrician', 'EPA certified "
                          "renovator', 'NCCCO crane operator', 'CCCM' or 'blaster license'.")
        return chk
    expires, issued = _date(cred.expires, "expires"), _date(cred.issued, "issued")
    ids: list[str] = []
    renew_by = window = None
    if kind in ELECTRICIAN_TYPES:
        ids = [E_RENEW, E_FILE, E_FEE_RENEW]
        if expires is None and issued:
            expires = _plus_years(issued, P(E_RENEW, "renewal_interval_years"))
            acc.math.append(f"{cred.type}: issued {issued} + {P(E_RENEW, 'renewal_interval_years')} years = expires "
                            f"{expires}")
        if expires:
            renew_by = expires - timedelta(days=P(E_FILE, "conservative_days_before_expiration"))
            window = _plus_years(expires, -P(E_RENEW, "may_renew_up_to_years_before_expiration"))
            chk.renewal = (f"File with {P(E_RENEW, 'ce_hours_required')} hours of continuing education "
                           f"({P(E_RENEW, 'ce_must_be_from')}) and a sworn statement of "
                           f"{_n(P(E_RENEW, 'industry_hours_required_in_prior_3_years'))} industry hours in the last "
                           f"{P(E_RENEW, 'renewal_interval_years')} years; fee {_usd(P(E_FEE_RENEW, 'renewal_fee_usd'))}. "
                           f"To renew without a lapse, 8 CCR 291.5(c) requires the renewal to be postmarked or "
                           f"received {P(E_FILE, 'regulation_days_before_expiration')} days before the certification "
                           f"period ends ({renew_by}); DIR's FAQ says postmarked on or before {expires}. Use the "
                           f"earlier date. Renewals are accepted from {window}.")
            acc.math.append(f"{cred.type}: renew by {expires} - {P(E_FILE, 'conservative_days_before_expiration')} "
                            f"days = {renew_by}")
            _ab1707(acc, renew_by)
            if day > expires:
                ids += [E_LAPSE, E_FEE_LAPSED]
                chk.issues.append(f"Lapsed on {expires}: working for a C-10 with an expired card is working "
                                  f"uncertified. To renew the worker must retake and pass the exam; fee "
                                  f"{_usd(P(E_FEE_LAPSED, 'lapsed_renewal_fee_usd'))}, no continuing education needed.")
    elif kind == TRAINEE:
        ids = [E_T_RENEW, E_T_150]
        yrs = P(E_T_RENEW, "renewal_interval_years")
        if expires is None and issued:
            expires = _plus_years(issued, yrs)
            acc.math.append(f"{cred.type}: registered {issued} + {yrs} year = renew by {expires}")
        if expires:
            renew_by = expires
            ids.append(E_T_WITHDRAW)
            chk.renewal = (f"Renew every {yrs} year (fee {_usd(P(E_T_RENEW, 'renewal_fee_usd'))}) with "
                           f"{_join(P(E_T_RENEW, 'renewal_attachments'))}; DIR has up to "
                           f"{P(E_T_RENEW, 'agency_response_days')} days to answer, so file early. The trainee must "
                           f"complete at least {P(E_T_150, 'min_classroom_hours_per_year')} classroom hours a year; "
                           "a trainee who stops attending must tell DIR, and the registration is cancelled.")
            if day > expires:
                chk.issues.append(f"Trainee registration lapsed {expires}: not legal to do certified work for a C-10 "
                                  "until renewed.")
    elif kind == RENOVATOR:
        ids = [R_REFRESH]
        if expires is None and issued:
            refresher = "refresher" in _words(cred.type)
            if refresher and cred.hands_on is not True:
                yrs = P(R_REFRESH, "certification_years_without_hands_on_refresher")
                why = "refresher without hands-on training" if cred.hands_on is False else (
                    "refresher; hands-on not stated, so the shorter term is assumed")
            else:
                yrs = (P(R_REFRESH, "certification_years_with_hands_on_refresher") if refresher else
                       P(R_REFRESH, "refresher_due_years_after_initial"))
                why = "refresher with hands-on training" if refresher else "initial course"
            expires = _plus_years(issued, yrs)
            acc.math.append(f"{cred.type}: course {issued} + {yrs} years ({why}) = expires {expires}")
        if expires:
            renew_by = expires
            chk.renewal = (f"Take a renovator refresher before {expires} (one with hands-on training gives "
                           f"{P(R_REFRESH, 'certification_years_with_hands_on_refresher')} years, one without gives "
                           f"{P(R_REFRESH, 'certification_years_without_hands_on_refresher')} and the next must be "
                           "hands-on).")
            if day > expires:
                chk.issues.append(f"Expired {expires}: {P(R_REFRESH, 'missed_deadline')} before working as the "
                                  "certified renovator.")
    elif kind in (CDPH_WORKER, CDPH_SUP):
        ids = [L_CDPH]
        if expires and day > expires:
            chk.issues.append(f"CDPH certificate expired {expires}.")
        if expires:
            renew_by = expires
            chk.renewal = "Renew with CDPH before it expires (CDPH renewal terms are not in this rulebook)."
    elif kind in (CRANE_CERT, CRANE_GOV):
        ids = [C_EXAM] + ([C_GOV] if kind == CRANE_GOV else [])
        if expires is None and issued:          # Option 1 (5006.2(d)(1)) and Option 2 (5006.2(e)(3)(B)) caps
            yrs = (P(C_GOV, "max_validity_years") if kind == CRANE_GOV else
                   P(C_EXAM, "option1_certificate_max_validity_years"))
            expires = _plus_years(issued, yrs)
            acc.math.append(f"{cred.type}: issued {issued} + at most {yrs} years = valid no later than {expires} "
                            "(use the card's own date if it is earlier)")
        if expires:
            renew_by = expires
            chk.renewal = "Recertify with the certifying entity (or licensing office) before it expires."
        if kind == CRANE_GOV:
            chk.issues.append("A government license counts only within the issuing jurisdiction"
                              + (f" ({cred.jurisdiction}); make sure this job is there." if cred.jurisdiction else
                                 "; give jurisdiction and confirm the job is there."))
    elif kind in (CCCM, TCCCM):
        if kind == CCCM:
            ids = [V_CCCM, V_CCCM_RENEW]
            if expires is None and issued:
                expires = _plus_years(issued, P(V_CCCM, "validity_years"))
                acc.math.append(f"{cred.type}: issued {issued} + {P(V_CCCM, 'validity_years')} years = expires {expires}")
            if expires:
                chk.renewal = (f"Renew every {P(V_CCCM_RENEW, 'renewal_interval_years')} years for "
                               f"{_usd(P(V_CCCM_RENEW, 'renewal_fee_usd'))} with at least "
                               f"{P(V_CCCM_RENEW, 'ce_hours_required')} hours of approved continuing education taken "
                               f"in the {P(V_CCCM_RENEW, 'ce_window_years_before_renewal')} year before renewal "
                               f"(on or after {_plus_years(expires, -P(V_CCCM_RENEW, 'ce_window_years_before_renewal'))}), "
                               "work history and the CQCC employer's signature.")
        else:
            ids = [V_TEMP]
            if expires is None and issued:
                expires = _plus_months(issued, P(V_TEMP, "initial_term_months"))
                acc.math.append(f"{cred.type}: issued {issued} + up to {P(V_TEMP, 'initial_term_months')} months = "
                                f"{expires}")
            if expires:
                chk.renewal = (f"Later terms are {P(V_TEMP, 'subsequent_term')}.")
        renew_by = expires
    elif kind == BLASTER:
        ids = [B_RENEW]
        if expires is None and issued:
            expires = _plus_years(issued, P(B_RENEW, "validity_years"))
            acc.math.append(f"{cred.type}: issued {issued} + {P(B_RENEW, 'validity_years')} years = expires {expires}")
        if expires:
            renew_by = expires
            chk.renewal = (f"Renewal is handled like a new application and the exam is retaken every "
                           f"{P(B_RENEW, 'exam_every_years')} years.")
    elif kind in (APPRENTICE, QUALIFIER, RRP_TRAINED, CRANE_OIT, NRLT_TRAINEE, ELEC_UNSPEC):
        # no expiry of their own: they work only under someone else's credential (checked at crew level)
        ids = {APPRENTICE: [E_APPR], QUALIFIER: [E_QUAL], RRP_TRAINED: [R_TRAINED], CRANE_OIT: [C_OIT],
               NRLT_TRAINEE: [E_NRLT_T], ELEC_UNSPEC: [E_TYPES]}[kind]
        chk.status = "valid" if kind != ELEC_UNSPEC else "missing_info"
        if kind == ELEC_UNSPEC:
            chk.issues.append("Give the DIR certification type (" + _join(P(E_TYPES, "types"), "or") + ") and the "
                              "expiration date on the card.")
        acc.use(*ids)
        chk.rule_ids = ids
        return chk
    acc.use(*ids)
    chk.rule_ids = ids
    chk.expires = expires.isoformat() if expires else None
    chk.renew_by = renew_by.isoformat() if renew_by else None
    chk.renewal_window_opens = window.isoformat() if window else None
    if expires is None:
        chk.status = "missing_info"
        chk.issues.append("Give the expiration date printed on the card (or the issue/course date).")
        return chk
    chk.days_left = (expires - day).days
    if day > expires:
        chk.status = "expired"
        if not chk.issues:
            chk.issues.append(f"Expired {expires}" + (" (from the issue date: the longest the credential can last)"
                                                      if cred.expires is None else "")
                              + ": it no longer covers this work until renewed.")
    elif renew_by and day > renew_by:
        chk.status = "renewal_overdue"
        chk.issues.append(f"Past the renew-by date {renew_by}: file the renewal today.")
    elif renew_by and (renew_by - day).days <= DUE_SOON_DAYS:
        chk.status = "renew_soon"
    else:
        chk.status = "valid"
    return chk


_CRED_RANK = {"valid": 0, "renew_soon": 1, "renewal_overdue": 2, "missing_info": 3, "expired": 4,
              "not_recognized": 5}


def _best(checks: list[CredentialCheck], kinds) -> Optional[CredentialCheck]:
    have = [c for c in checks if c.kind in kinds]
    return min(have, key=lambda c: _CRED_RANK[c.status]) if have else None


def _worker_status_for(chk: CredentialCheck) -> str:
    return {"valid": "ok", "renew_soon": "renew_soon", "renewal_overdue": "renewal_overdue", "expired": "expired",
            "missing_info": "missing_info"}[chk.status]


def check_crew_certifications(crew, employer_license_classes=None, year_built=None, building_type=None,
                              built_before_1978=None, lead_exposure_ug_m3=None, firm_rrp_certification_expires=None,
                              as_of=None) -> CrewCheckResult:
    if not isinstance(crew, (list, tuple)) or not crew:
        raise ValueError("crew must be a non-empty list of {name, task, credentials}")
    day = _as_of(as_of)
    members = [_member(m, i) for i, m in enumerate(crew)]
    names = [m.name.strip() for m in members]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"crew names must be unique (used for supervised_by); repeated: {', '.join(sorted(dupes))}")
    acc = _Acc()
    lead_tasks = [m.task for m in members if ("lead", None) in classify_task(m.task)]
    ctx = _context(employer_license_classes, year_built, built_before_1978, building_type, lead_exposure_ug_m3, day,
                   lead_tasks=lead_tasks, acc=acc)
    res = CrewCheckResult(as_of=day.isoformat())
    by_name: dict[str, tuple[CrewMember, WorkerCheck, list]] = {}
    trainee_of: dict[str, list[str]] = {}          # supervisor -> trainees (electrical)
    unsup_trainees, nrlt_trainees, oits, renovator_job, blast_job, cert_electricians = [], [], [], [], [], []
    elevator_job, hoist_job, pv_job, rrp_trained = False, False, [], []
    rrp_status = None
    for m in members:
        checks = [_deadline(c, credential_kind(c.type), day, acc)
                  for c in (_cred_from(r, i) for i, r in enumerate(m.credentials))]
        hits = classify_task(m.task)
        w = WorkerCheck(name=m.name.strip(), task=m.task, credentials=checks, status="unrecognized_task",
                        trades=[t if s is None or t == "lead" else f"{t}:{s}" for t, s in hits])
        ids: list[str] = []
        statuses, covered = [], []
        qualifier = m.is_c10_qualifier or any(c.kind == QUALIFIER for c in checks)
        if qualifier:
            ids.append(E_QUAL)
        if any(c.kind in ELECTRICIAN_TYPES and c.status in ("valid", "renew_soon", "renewal_overdue")
               for c in checks) or qualifier:
            cert_electricians.append(w.name)
        as_trainee = as_nrlt_trainee = False
        for trade, sub in hits:
            for need in _needs(trade, sub, ctx):
                ids += need.ids
                if need.status == "not_required":
                    statuses.append("not_required")
                    continue
                if trade == "personnel_hoist":
                    hoist_job = True
                    statuses.append("not_required")
                    continue
                if trade == "pressure_vessel":
                    pv_job.append(sub)
                    statuses.append("not_required")
                    continue
                if need.trade == "lead_rrp":
                    renovator_job.append(w.name)
                    rrp_status = "required" if need.status == "required" or rrp_status == "required" else "conditional"
                if need.trade == "blasting":
                    blast_job.append(w.name)
                if need.trade == "elevator":
                    elevator_job = True
                st, cov, issue = _cover(need, m, checks, qualifier, ctx.building)
                if issue and issue not in w.issues:
                    w.issues.append(issue)
                if cov:
                    covered.append(cov)
                if need.trade == "electrical" and st == "ok_with_supervision":
                    as_trainee |= cov == _kind_label(TRAINEE)
                    as_nrlt_trainee |= cov == _kind_label(NRLT_TRAINEE)
                if need.trade == "crane" and cov == _kind_label(CRANE_OIT) and w.name not in [o[0] for o in oits]:
                    oits.append((w.name, m.supervised_by))
                if need.trade == "lead_rrp" and cov == _kind_label(RRP_TRAINED) and w.name not in rrp_trained:
                    rrp_trained.append(w.name)
                statuses.append(st)
        if as_trainee:
            if m.supervised_by and m.supervised_by.strip():
                trainee_of.setdefault(m.supervised_by.strip(), []).append(w.name)
            else:
                unsup_trainees.append(w.name)
        if as_nrlt_trainee:
            nrlt_trainees.append(w.name)
        if hits:
            w.status = min(statuses, key=_SEVERITY.index) if statuses else "not_required"
        w.can_do_task = {"not_qualified": False, "expired": False, "missing_info": None, "confirm": None,
                         "renewal_overdue": True, "renew_soon": True, "ok_with_supervision": True, "ok": True,
                         "not_required": True, "unrecognized_task": None}[w.status]
        w.covered_by = _join(dict.fromkeys(covered)) or None
        for c in checks:
            ids += c.rule_ids
            w.issues += [f"{c.label or c.given}: {x}" for x in c.issues if f"{c.label or c.given}: {x}" not in w.issues]
        if not hits:
            w.issues.append("Task not recognized; describe it in trade words (e.g. 'pull wire', 'scrape paint', "
                            "'crane operator', 'blasting').")
        w.rule_ids = list(dict.fromkeys(ids))
        acc.use(*w.rule_ids)
        w.verified = _ok(w.rule_ids)
        w.next_step = _worker_next_step(w)
        by_name[w.name] = (m, w, checks)
        res.workers.append(w)
    # ---- crew checks
    findings = res.crew_findings

    def finding(item, status, detail, *rids):
        acc.use(*rids)
        rids = list(dict.fromkeys(rids))
        findings.append(CrewFinding(item=item, status=status, detail=detail, rule_ids=rids, citation=_cit(rids),
                                    verified=_ok(rids)))
    ratio = P(E_RATIO, "max_trainees_per_certified_electrician")
    named = sum(trainee_of.values(), [])
    if named or unsup_trainees:
        problems = []
        for sup, ts in trainee_of.items():
            if sup not in by_name:
                problems.append(f"{_join(ts)} name supervisor {sup!r}, who is not on this crew")
            elif sup not in cert_electricians:
                problems.append(f"{sup} supervises {_join(ts)} but holds no current electrician certification")
            elif len(ts) > ratio:
                problems.append(f"{sup} supervises {len(ts)} trainees ({_join(ts)}); the limit is {ratio}")
        free = [c for c in cert_electricians if c not in trainee_of]
        capacity = len(free) * ratio
        if unsup_trainees:
            acc.math.append(f"trainees without a named supervisor {len(unsup_trainees)} vs certified electricians "
                            f"without a trainee {len(free)} x {ratio} = {capacity}")
            if len(unsup_trainees) > capacity:
                problems.append(f"{len(unsup_trainees)} trainee(s) ({_join(unsup_trainees)}) but only {capacity} "
                                f"certified electrician slot(s) free on this crew")
        detail = (f"Each electrician trainee needs direct, on-site supervision by a certified electrician who "
                  f"supervises no more than {ratio} trainee (apprentices do not count). ")
        if problems:
            finding("Electrician trainee supervision", "problem", detail + "; ".join(problems) + ". Add certified "
                    "electricians or take trainees off certified work.", E_RATIO, E_BARRED, E_DISC)
        else:
            finding("Electrician trainee supervision", "ok", detail + f"{len(named) + len(unsup_trainees)} trainee(s), "
                    f"{len(cert_electricians)} certified electrician(s): within the limit.", E_RATIO)
    if nrlt_trainees:
        nrlt = [n for n, (_, w, ch) in by_name.items()
                if any(c.kind == NRLT and c.status in ("valid", "renew_soon", "renewal_overdue") for c in ch)]
        finding("Nonresidential lighting trainees", "ok" if nrlt else "problem",
                f"{_join(nrlt_trainees)} must be supervised on site by a certified Nonresidential Lighting Technician"
                + (f" ({_join(nrlt)} on this crew)." if nrlt else "; none on this crew."), E_NRLT_T)
    elec_workers = [w.name for w in res.workers if any(t.startswith("electrical") for t in w.trades)]
    if elec_workers and not renovator_job and ctx.pre1978 and _rrp_need(ctx).status != "not_required":
        finding("EPA RRP for electrical work in a pre-1978 building", "confirm",
                f"{_join(elec_workers)}: {_trade_work_rrp_text()} If the work goes past that, the RRP work practices "
                "apply and a certified renovator must be on site for the signs, containment and cleaning (run "
                "certifications_required with the painted areas).", R_TRADES, R_APPLY, R_ONSITE)
    if renovator_job:
        ren = [n for n, (_, w, ch) in by_name.items()
               if any(c.kind == RENOVATOR and c.status in ("valid", "renew_soon", "renewal_overdue") for c in ch)]
        st = "ok" if ren else ("problem" if rrp_status == "required" else "confirm")
        finding("EPA certified renovator assigned", st,
                (f"{_join(ren)} can be the assigned certified renovator" if ren else
                 "No current certified renovator on this crew") + " (must be on site during "
                + _join(P(R_ONSITE, "must_be_physically_present_during")) + ")."
                + (f" {_join(rrp_trained)}: keep the training record for each." if rrp_trained else "")
                + ("" if rrp_status == "required" else " Applies only if the building is pre-"
                   f"{P(R_APPLY, 'built_before_year')} housing or a child-occupied facility."),
                R_ONSITE, R_TRAINED, R_APPLY)
        firm_exp = _date(firm_rrp_certification_expires, "firm_rrp_certification_expires")
        if firm_exp:
            timely = firm_exp - timedelta(days=P(R_RECERT, "timely_if_days_before_expiration_gte"))
            acc.math.append(f"firm RRP recertification timely by {firm_exp} - "
                            f"{P(R_RECERT, 'timely_if_days_before_expiration_gte')} days = {timely}")
            if day > firm_exp:
                finding("EPA RRP firm certification", "problem" if rrp_status == "required" else "confirm",
                        f"Expired {firm_exp}: the firm may not do "
                        "renovations until EPA approves recertification.", R_FIRM, R_RECERT)
            else:
                late = day > timely
                finding("EPA RRP firm certification",
                        ("problem" if rrp_status == "required" else "confirm") if late else "ok",
                        (f"Expires {firm_exp}; recertification filed by {timely} is timely and keeps it valid while "
                         "EPA decides." + (" That date has passed: file now; if EPA has not approved it by "
                                           f"{firm_exp} the firm must stop renovations." if late else "")
                         + f" Fee {_usd(P(R_FEE, 'firm_recertification_usd'))}."), R_FIRM, R_RECERT, R_FEE)
        else:
            finding("EPA RRP firm certification", "confirm", "Confirm the firm's EPA certification is current "
                    "(give firm_rrp_certification_expires for the recertification date).", R_FIRM)
    if blast_job:
        bl = [n for n, (_, w, ch) in by_name.items()
              if any(c.kind == BLASTER and c.status in ("valid", "renew_soon", "renewal_overdue") for c in ch)]
        finding("Licensed blaster on site", "ok" if bl else "problem",
                (f"{_join(bl)} holds a current Blaster's License" if bl else "No current licensed blaster on this "
                 "crew: no blasting may happen") + "; the blaster must be physically present doing or directing it.",
                B_ONSITE)
    for name, sup in oits:
        trainer = by_name.get((sup or "").strip())
        ok = bool(trainer) and any(c.kind in (CRANE_CERT, CRANE_GOV) and c.status in ("valid", "renew_soon",
                                                                                      "renewal_overdue")
                                   for c in trainer[2])
        finding(f"Crane operator-in-training {name}", "ok" if ok else "problem",
                (f"Trainer {sup} holds a current crane certificate" if ok else
                 (f"Trainer {sup!r} is not on this crew or has no current crane certificate" if sup else
                  "No trainer named (supervised_by)")) + "; the trainer must monitor continuously on site, within "
                "sight and communication, with no other duties.", C_OIT)
    if elevator_job:
        finding("Elevator company is a CQCC", "confirm", "Mechanics must be employed by a Certified Qualified "
                "Conveyance Company; confirm your company's CQCC certificate is current (it needs a C-11 license).",
                V_CQCC, V_CCCM_ONLY)
    if hoist_job:
        finding("Personnel hoist permit", "confirm", "The hoist needs a DOSH permit to operate posted in the car "
                "(run equipment_operating_permits).", H_PERMIT)
    if pv_job:
        finding("Pressure vessel permits", "confirm", "Air tanks, LP-gas tanks and boilers may need DOSH permits to "
                "operate (run equipment_operating_permits).", *({"air": PV_AIR, "lpg": PV_LPG, "boiler": PV_BOILER}[s]
                                                                   for s in dict.fromkeys(pv_job)))
    if any(w.trades and any(t.startswith("electrical") for t in w.trades) and w.status != "not_required"
           for w in res.workers):
        finding("Proof of certification at the jobsite", "confirm", "Keep a copy of every electrician's DIR card or "
                "trainee letter on file and at the jobsite: without it a violation is presumed.", E_PRESUME)
    # ---- exposure
    elec_bad = [w.name for w in res.workers if any(t.startswith("electrical") for t in w.trades)
                and w.status in ("not_qualified", "expired")]
    if elec_bad and ctx.classes and "C-10" in ctx.classes:
        res.exposure.append(f"C-10 license: CSLB discipline up to {P(E_DISC, 'max_sanction')} for willfully "
                            f"employing even {P(E_DISC, 'threshold_uncertified_workers')} uncertified person as an "
                            f"electrician ({_join(elec_bad)} here) [{_cit([E_DISC])}].")
        acc.use(E_DISC)
    lapsed = [w.name for w in res.workers if any(c.kind in ELECTRICIAN_TYPES and c.status == "expired"
                                                 for c in w.credentials)]
    if lapsed:
        res.exposure.append(f"A person who claims to be certified when they are not is barred for "
                            f"{P(E_FALSE, 'bar_years')} years from the exam and trainee registration: {_join(lapsed)} "
                            f"must not present an expired card as current [{_cit([E_FALSE])}].")
        acc.use(E_FALSE)
    if renovator_job and any(f.status == "problem" for f in findings if "RRP" in f.item or "renovator" in f.item):
        on = date.fromisoformat(P(R_PEN, "applies_to_penalties_assessed_on_or_after"))
        res.exposure.append(f"EPA: up to {_usd(P(R_PEN, 'max_civil_penalty_usd_per_violation') if day >= on else P(R_PEN, 'prior_max_usd'))}"
                            f" per RRP violation [{_cit([R_PEN])}].")
        acc.use(R_PEN)
    # ---- summary
    res.counts = {s: sum(w.status == s for w in res.workers) for s in _SEVERITY if any(w.status == s
                                                                                         for w in res.workers)}
    blockers = [w for w in res.workers if w.status in ("not_qualified", "expired")]
    unknown = [w for w in res.workers if w.status in ("missing_info", "confirm", "unrecognized_task")]
    problems = [f for f in findings if f.status == "problem"]
    confirm = [f for f in findings if f.status == "confirm"]
    res.cleared_to_start = not blockers and not unknown and not problems
    res.summary = (f"{len(res.workers)} worker(s): "
                   + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in res.counts.items())
                   + (f"; {len(problems)} crew problem(s)" if problems else "")
                   + (". Cleared to start" + (f" once you confirm {len(confirm)} item(s) below." if confirm else ".")
                      if res.cleared_to_start else ". Not cleared yet."))
    order = sorted(res.workers, key=lambda w: _SEVERITY.index(w.status))
    res.next_steps = ([f"{w.name}: {w.next_step}" for w in order if w.status not in ("ok", "not_required")]
                      + [f"{f.item}: {f.detail}" for f in findings if f.status != "ok"])
    res.next_step = res.next_steps[0] if res.next_steps else "Crew is cleared; keep copies of every card at the jobsite."
    return acc.finish(res)


def _cover(need: _Need, m: CrewMember, checks: list[CredentialCheck], qualifier: bool,
           building: Optional[str] = None) -> tuple[str, Optional[str], str]:
    """-> (worker status for this need, credential that covers it, issue)."""
    cond = need.status == "conditional"
    best = _best(checks, need.accept)
    if best and best.status != "expired":
        st = _worker_status_for(best)
        if need.trade == "crane":
            if m.employer_evaluated is False:
                return "not_qualified", best.label, ("Certification alone is not enough: the employer must evaluate "
                                                     "the operator before they run the crane (8 CCR 5006.2(f)).")
            if m.employer_evaluated is None:
                return ("confirm" if _SEVERITY.index(st) > _SEVERITY.index("confirm") else st), best.label, (
                    "Confirm the employer evaluation is documented and kept at the worksite.")
        return st, best.label, ""
    if need.trade == "electrical":
        if qualifier:
            return "ok", _kind_label(QUALIFIER), ""
        maybe = _best(checks, need.maybe)
        if maybe and maybe.status != "expired":
            return "confirm", maybe.label, f"{_etype(RE)} covers this {_re_maybe_text(building)}."
        alt = _best(checks, need.alt)
        if alt and alt.status not in ("expired", "missing_info"):
            if alt.kind == APPRENTICE:
                return "ok_with_supervision", alt.label, "Registered apprentice: the C-10 must supervise them."
            return "ok_with_supervision", alt.label, ""
        if alt and alt.status == "missing_info":
            return "missing_info", alt.label, ""
        unspec = _best(checks, [ELEC_UNSPEC])
        if unspec:
            return "missing_info", None, (f"Which DIR certification is it? This work needs "
                                          f"{_join([_kind_label(k) for k in need.accept], 'or')}.")
    elif need.alt:
        alt = _best(checks, need.alt)
        if alt and alt.status not in ("expired", "missing_info"):
            if need.trade == "crane":
                hard, rigging = _oit_restricted(m.task)
                if hard:
                    return "not_qualified", alt.label, (f"An operator-in-training may not run the crane for "
                                                        f"{_join(hard, 'or')} unless certified (8 CCR 5006.2(b)(3)): "
                                                        "assign a certified operator.")
                if rigging:
                    return "confirm", alt.label, (f"{rigging[0][0].upper() + rigging[0][1:]}: allowed for an "
                                                  "operator-in-training only if the trainer has determined the "
                                                  "trainee's skills are sufficient; record that determination.")
            return "ok_with_supervision", alt.label, ""
    if need.anyone_supervised:
        return "ok_with_supervision", None, "May work only under the licensed blaster's direct supervision."
    if best and best.status == "expired":
        return ("confirm" if cond else "expired"), best.label, ""
    other = [c.label for c in checks if c.kind in ELECTRICIAN_TYPES] if need.trade == "electrical" else []
    names = _join([_kind_label(k) for k in need.accept + need.alt], "or")
    if other:
        msg = f"{_join(other)} does not cover {_SUB_LABEL[need.sub]} here; needs {names}."
    else:
        msg = f"Needs {names}."
    if cond:
        return "confirm", None, msg + " " + need.why
    return "not_qualified", None, msg


# task words for the 8 CCR 5006.2(b)(3)(A)-(E) operator-in-training limits, in the rule's list order (spellings only)
_OIT_TASK_WORDS = (r"power ?lines?|energized (lines?|conductors?)|overhead (power |electric )?lines?|utility lines?",
                   r"personnel (lift\w*|hoist\w*|baskets?|platforms?)|man ?baskets?|(hoist\w*|lift\w*|fly\w*) "
                   r"(personnel|workers|people|crews?)",
                   r"tandem|multi(ple)? ?(crane|equipment)|dual crane|(two|2) cranes?",
                   r"\bshafts?\b|cofferdams?|tank farms?",
                   r"christmas tree\w*|multiple ?lift rigging|multi ?lift rigging|\bmlr\b")


def _oit_restricted(task: str) -> tuple[list[str], list[str]]:
    """-> (circumstances an operator-in-training may never work in, multiple-lift rigging if named)."""
    w = _words(task)
    limits = P(C_OIT, "trainee_prohibited_unless_certified")
    hits = [limits[i] for i, pat in enumerate(_OIT_TASK_WORDS) if re.search(pat, w)]
    rigging = [h for h in hits if h == limits[-1]]
    return [h for h in hits if h != limits[-1]], [r.split(" (")[0] for r in rigging]


def _worker_next_step(w: WorkerCheck) -> str:
    first = next((i for i in w.issues), "")
    return {
        "not_qualified": f"Do not assign {w.name} to this task yet. {first}",
        "expired": f"Take {w.name} off this work until renewed. {first}",
        "missing_info": f"Get {w.name}'s card details. {first}",
        "confirm": f"Confirm before {w.name} starts. {first}",
        "renewal_overdue": f"File {w.name}'s renewal today. {first}",
        "renew_soon": f"Renew {w.name}'s card by "
                      + (next((c.renew_by for c in w.credentials if c.status == 'renew_soon' and c.renew_by), '')
                         or "the renew-by date") + ".",
        "ok_with_supervision": f"{w.name} may work only under the required on-site supervision. {first}".strip(),
        "ok": f"No action; keep a copy of {w.name}'s {w.covered_by or 'card'} at the jobsite.",
        "not_required": "No state trade credential needed for this task.",
        "unrecognized_task": f"Describe {w.name}'s task in trade words.",
    }[w.status].strip()


# ================================================================ electrician_certification_path
_CERT_TYPE_WORDS = ((FLS, r"fire|fls|life safety|alarm"), (VDV, r"voice|data|video|vdv|low voltage"),
                    (NRLT, r"lighting|nrlt"), (RE, r"residential|\bre\b|resi"), (GE, r"general|\bge\b|journeyman"))
_AREA_CONCEPTS = (
    ("nurse_call", r"nurse call"),
    ("fixture_maintenance", r"(maint\w*|service) of (lighting )?fixtures?|fixtures? maint\w*|lighting maint\w*"),
    ("proprietary", r"proprietary"),
    ("training", r"training"),
    ("retrofit", r"retrofit\w*"),
    ("stock", r"stock|material handling|materials handling"),
    ("underground", r"underground|\bug\b"),
    ("troubleshooting", r"trouble ?shoot\w*|repair\w*"),
    ("maintenance", r"maint\w*|service"),
    ("finish", r"finish|fixtures?|trim out"),
    ("fls", r"fire|fls|life safety"),
    ("vdv", r"voice|data|video|vdv|low voltage|telecom"),
    ("installations", r"install\w*|wire pull\w*|terminations?|control panels?"),
    ("residential", r"residential|resi|homes?|house"),
    ("commercial", r"commercial|comm"),
    ("industrial", r"industrial|\bind\b"),
)


def cert_type_from(value) -> str:
    w = _words(value).replace(" ", "_")
    if w in ELECTRICIAN_TYPES:
        return w
    w = _words(value)
    for kind, pat in _CERT_TYPE_WORDS:
        if re.search(pat, w):
            return kind
    raise ValueError(f"certification_type {value!r} not recognized; use one of {_join(P(E_TYPES, 'types'), 'or')}")


def _caps_key(kind: str) -> str:
    return kind.replace("_electrician", "").replace("_technician", "") + "_caps_hours"


def _area_key(kind: str, area: str, caps: dict) -> Optional[str]:
    k = _words(area).replace(" ", "_")
    if k in caps:
        return k
    w = _words(area)
    concept = next((c for c, pat in _AREA_CONCEPTS if re.search(pat, w)), None)
    if concept is None:
        return None
    keywords = {"stock": ["stock"], "residential": ["residential"], "commercial": ["commercial"],
                "industrial": ["industrial"], "underground": ["underground"], "troubleshooting": ["troubleshooting"],
                "maintenance": ["maintenance_of_fixtures"] if kind == NRLT else ["troubleshooting"],
                "finish": ["finish"], "nurse_call": ["nurse_call"], "installations": ["installation"],
                "training": ["training"], "proprietary": ["proprietary"],
                "fixture_maintenance": ["maintenance_of_fixtures"], "retrofit": ["retrofit"],
                "vdv": ["voice_data_video"] + (["installations"] if kind == VDV else []),
                "fls": ["fire_life_safety"] + (["installations"] if kind == FLS else [])}[concept]
    for kw in keywords:
        hit = next((key for key in caps if kw in key), None)
        if hit:
            return hit
    return None


class AreaHours(BaseModel):
    area: str = Field(description="8 CCR 291.1 work area (rulebook key), or your label if it is not a listed area")
    given_as: list[str] = Field(default_factory=list)
    hours_given: float
    cap: Optional[float] = None
    counted: float
    not_counted: float
    listed: bool


class Fee(BaseModel):
    item: str
    amount_usd: Optional[float] = None
    when: Literal["one_time", "per_hour", "recurring", "if_needed"] = "one_time"
    basis: str = ""
    rule_id: str


class ElectricianPathResult(_Base):
    certification_type: str
    as_of: str
    required_hours: float
    route: Literal["apprenticeship_completed", "apprentice_final_year", "nicet", "on_the_job_hours",
                   "out_of_state_license_review", "not_yet_eligible"]
    experience_met: Optional[bool] = Field(None, description="None = DIR decides (out-of-state license review)")
    hours: list[AreaHours] = Field(default_factory=list)
    counted_hours: float = 0
    shortfall_hours: float = 0
    areas_with_counted_hours: int = 0
    min_areas: Optional[int] = None
    fees: list[Fee] = Field(default_factory=list)
    total_to_apply_usd: float = 0
    exam: dict = Field(default_factory=dict)
    exam_window_ends: Optional[str] = None
    exam_window_expired: Optional[bool] = None
    next_exam_allowed_on: Optional[str] = None
    documents: list[str] = Field(default_factory=list)
    trainee_route: Optional[str] = None
    after_certification: str = ""


def electrician_certification_path(certification_type, hours_by_area=None, completed_apprenticeship=False,
                                   months_left_in_apprenticeship=None, nicet_fire_alarm_level=None,
                                   out_of_state_license=False, military_or_c7_experience=False,
                                   eligibility_notice_date=None, last_exam_date=None,
                                   as_of=None) -> ElectricianPathResult:
    kind = cert_type_from(certification_type)
    name = _etype(kind)
    day = _as_of(as_of)
    acc = _Acc()
    acc.use(E_TYPES, E_HOURS, E_CAPS, E_FEE, E_EXAM, E_SSA, E_RESP, E_RENEW)
    required = float(P(E_HOURS, "required_hours")[name])
    caps = P(E_CAPS, _caps_key(kind))
    # hours by area
    if hours_by_area is None:
        items = []
    elif isinstance(hours_by_area, dict):
        items = list(hours_by_area.items())
    elif isinstance(hours_by_area, (list, tuple)):
        items = []
        for i, row in enumerate(hours_by_area):
            if not isinstance(row, dict) or "area" not in row or "hours" not in row:
                raise ValueError(f"hours_by_area item {i + 1} must be {{'area': 'commercial wiring', 'hours': 4000}}")
            items.append((row["area"], row["hours"]))
    else:
        raise ValueError("hours_by_area must be like {'commercial wiring': 4000, 'residential wiring': 2500}")
    sums: dict[str, list] = {}
    for area, hrs in items:
        h = _num(f"hours for {area!r}", hrs, 0, MAX_AREA_HOURS)
        key = _area_key(kind, str(area), caps) or f"not listed: {str(area).strip()}"
        sums.setdefault(key, [0.0, []])
        sums[key][0] += h
        sums[key][1].append(str(area))
    rows, counted = [], 0.0
    for key, (h, given) in sums.items():
        cap = caps.get(key)
        c = min(h, cap) if cap is not None else 0.0
        counted += c
        rows.append(AreaHours(area=key, given_as=given, hours_given=h, cap=cap, counted=c, not_counted=h - c,
                              listed=cap is not None))
        if cap is None:
            acc.math.append(f"{_join(given)}: {_n(h)} h not counted (not a listed area for {name})")
        else:
            acc.math.append(f"{key}: min({_n(h)}, cap {_n(cap)}) = {_n(c)} h")
    areas = sum(r.counted > 0 for r in rows)
    min_areas = P(E_CAPS, "general_min_areas") if kind == GE else None
    shortfall = max(0.0, required - counted)
    if items:
        acc.math.append(f"counted {' + '.join(_n(r.counted) for r in rows if r.listed) or '0'} = {_n(counted)} h vs "
                        f"{_n(required)} h required -> " + (f"short {_n(shortfall)} h" if shortfall else "enough hours")
                        + (f"; {areas} area(s) with counted hours vs at least {min_areas}" if min_areas else ""))
    hours_ok = bool(items) and shortfall == 0 and (min_areas is None or areas >= min_areas)
    months = _num("months_left_in_apprenticeship", months_left_in_apprenticeship, 0)
    nicet = _int("nicet_fire_alarm_level", nicet_fire_alarm_level, 1, 4)
    route, met = "not_yet_eligible", False
    notes_route = ""
    if _bool("completed_apprenticeship", completed_apprenticeship):
        route, met = "apprenticeship_completed", True
        notes_route = f"Completed apprenticeship: {P(E_HOURS, 'alternative')} meets the experience requirement."
    elif months is not None and months <= P(E_APPR, "may_take_exam_within_months_of_completion"):
        acc.use(E_APPR)
        route, met = "apprentice_final_year", True
        notes_route = (f"{_n(months)} months left <= {P(E_APPR, 'may_take_exam_within_months_of_completion')}: the "
                       f"apprentice may take the exam now and is certified upon {P(E_APPR, 'certified_upon')}.")
        acc.math.append(notes_route)
    elif kind == FLS and nicet is not None and nicet >= P(E_NICET, "min_nicet_level"):
        acc.use(E_NICET)
        route, met = "nicet", True
        notes_route = (f"NICET {P(E_NICET, 'alternative_credential')} Level {nicet} >= Level "
                       f"{P(E_NICET, 'min_nicet_level')}: replaces the {_n(required)} hours.")
        acc.math.append(notes_route)
    elif hours_ok:
        route, met = "on_the_job_hours", True
    elif _bool("out_of_state_license", out_of_state_license):
        acc.use(E_OOS)
        route, met = "out_of_state_license_review", None
        notes_route = ("DIR may treat a comparable out-of-state electrician license as meeting the experience "
                       "requirement; the exam is still required.")
    if kind == FLS and nicet is not None and nicet < P(E_NICET, "min_nicet_level"):
        acc.use(E_NICET)
        acc.math.append(f"NICET Level {nicet} < Level {P(E_NICET, 'min_nicet_level')}: does not replace the hours")
    if _bool("military_or_c7_experience", military_or_c7_experience):
        acc.note(f"On request DIR may credit {_join(P(E_OOS, 'credit_sources'))} toward some or all of the hours; "
                 "submit it with the application.", E_OOS)
    # fees and exam
    app, exam_fee, total, retake = (P(E_FEE, "application_fee_usd"), P(E_FEE, "exam_fee_usd"),
                                    P(E_FEE, "total_initial_usd"), P(E_FEE, "retake_exam_fee_usd"))
    fees = [Fee(item="Application", amount_usd=app, basis="per application", rule_id=E_FEE),
            Fee(item="Exam", amount_usd=exam_fee, basis="first sitting", rule_id=E_FEE),
            Fee(item="Each retake", amount_usd=retake, when="if_needed", basis="per sitting", rule_id=E_FEE)]
    acc.math.append(f"to apply: {_usd(app)} + {_usd(exam_fee)} = {_usd(total)}")
    exam = {"questions": P(E_EXAM, "questions")[name], "time_hours": P(E_EXAM, "time_hours")[name],
            "passing_score_pct": P(E_EXAM, "passing_score_pct"), "languages": P(E_EXAM, "languages"),
            "open_book": P(E_EXAM, "open_book")}
    res = ElectricianPathResult(certification_type=name, as_of=day.isoformat(), required_hours=required, route=route,
                                experience_met=met, hours=rows, counted_hours=counted,
                                shortfall_hours=shortfall if items else required, areas_with_counted_hours=areas,
                                min_areas=min_areas, fees=fees, total_to_apply_usd=total, exam=exam)
    res.documents = [f"{P(E_SSA, 'required_attachment')} (form {P(E_SSA, 'ssa_form')}; you may black out the SSN)",
                     "Application signed under penalty of perjury"] + (
        ["Apprenticeship completion certificate"] if route == "apprenticeship_completed" else []) + (
        ["NICET certificate"] if route == "nicet" else [])
    if route == "apprentice_final_year":
        res.documents.append("Proof of apprenticeship status and expected completion date")
    # exam window and retakes
    notice = _date(eligibility_notice_date, "eligibility_notice_date")
    last = _date(last_exam_date, "last_exam_date")
    if last and last > day:
        raise ValueError(f"last_exam_date {last} is after as_of {day}")
    if notice and last and last < notice:
        raise ValueError(f"last_exam_date {last} is before eligibility_notice_date {notice}")
    if notice:
        acc.use(E_WINDOW)
        end = _plus_years(notice, P(E_WINDOW, "eligibility_window_years"))
        res.exam_window_ends, res.exam_window_expired = end.isoformat(), day > end
        acc.math.append(f"exam window: notice {notice} + {P(E_WINDOW, 'eligibility_window_years')} year = {end}")
        _ab1707(acc, end)
    if last:
        acc.use(E_WINDOW)
        nxt = last + timedelta(days=P(E_WINDOW, "min_days_between_attempts"))
        res.next_exam_allowed_on = nxt.isoformat()
        acc.math.append(f"next sitting: {last} + {P(E_WINDOW, 'min_days_between_attempts')} days = {nxt}")
        if res.exam_window_ends and nxt > date.fromisoformat(res.exam_window_ends):
            acc.notes.append(f"The next allowed sitting ({nxt}) is after the exam window closes "
                             f"({res.exam_window_ends}): a new application and {_usd(total)} will be needed.")
    # trainee route and after certification
    t_fee = P(E_FEE_TRAINEE, "trainee_registration_fee_usd")
    if met is False:
        acc.use(E_TRAINEE, E_T_150, E_RATIO, E_T_PATH)
        res.trainee_route = (f"Until certified, the worker may do certified work for a C-10 only as a registered "
                             f"electrician trainee: {_usd(t_fee)}, enrolled in a DIR-approved curriculum with at least "
                             f"{P(E_T_150, 'min_classroom_hours_per_year')} classroom hours a year, under direct "
                             f"on-site supervision of a certified electrician who supervises no more than "
                             f"{P(E_RATIO, 'max_trainees_per_certified_electrician')} trainee. Finishing the curriculum "
                             "allows the exam, but certification still needs the hours.")
    res.after_certification = (f"Certification lasts {P(E_RENEW, 'renewal_interval_years')} years; renewing takes "
                               f"{P(E_RENEW, 'ce_hours_required')} hours of continuing education and "
                               f"{_n(P(E_RENEW, 'industry_hours_required_in_prior_3_years'))} industry hours in the "
                               "previous three years.")
    # summary
    if met is True:
        if res.exam_window_expired:
            res.summary = (f"Experience met for {name}, but the exam window closed {res.exam_window_ends}: file a new "
                           f"application ({_usd(total)}).")
            res.next_step = f"File a new {name} application with {_usd(total)} and the SSA earnings report."
        elif notice:
            res.summary = (f"Eligible: take the {name} exam by {res.exam_window_ends}"
                           + (f" (next sitting allowed {res.next_exam_allowed_on})" if last else "") + ".")
            res.next_step = (f"Schedule the exam on or after {res.next_exam_allowed_on}" if last else
                             "Schedule the exam") + f" and no later than {res.exam_window_ends}; bring a valid state " \
                                                   "photo ID or the fees are forfeited."
        else:
            how = {"apprenticeship_completed": "completed apprenticeship", "apprentice_final_year": "apprentice in "
                   "the final year", "nicet": "NICET certification", "on_the_job_hours": "documented hours"}[route]
            res.summary = (f"Experience met for {name} ({how}). Apply: {_usd(total)} "
                           f"({_usd(app)} application + {_usd(exam_fee)} exam).")
            res.next_step = (f"Mail the {name} application with the SSA earnings report and {_usd(total)} payable to "
                             f"{P(E_FEE, 'payable_to')}; DIR answers within {P(E_RESP, 'agency_response_days')} days.")
    elif met is None:
        res.summary = f"{name}: DIR decides whether the out-of-state license is comparable; the exam is still required."
        res.next_step = (f"Apply with the out-of-state license and {_usd(total)}; DIR answers within "
                         f"{P(E_RESP, 'agency_response_days')} days. Until certified, work only as a registered trainee.")
    else:
        why = ("no hours given" if not items else
               f"{_n(counted)} of {_n(required)} hours count (short {_n(shortfall)})" if shortfall else
               f"hours come from {areas} area(s); {name} needs at least {min_areas}")
        res.summary = f"Not yet eligible for {name}: {why}."
        res.next_step = (f"Register as an electrician trainee ({_usd(t_fee)}) to keep working legally"
                         + (f" and log {_n(shortfall)} more hours in listed areas" if items and shortfall else "")
                         + "; re-run this with updated hours.")
    if notes_route:
        acc.notes.append(notes_route)
    acc.note(f"Fees are non-refundable once the application is accepted; a deficient application is returned with "
             f"all fees, and missing the exam without valid photo ID forfeits them. If DIR denies the application, "
             f"appeal in writing within {P(E_APPEAL, 'appeal_days')} days of service.", E_APPEAL)
    acc.note("Tell DIR when your mailing address changes: denial and other notices go to the address on file.", E_ADDR)
    _ab1707(acc, day)
    res.next_steps = [res.next_step] + ([res.trainee_route] if res.trainee_route else []) + [res.after_certification]
    return acc.finish(res)


# ================================================================ equipment_operating_permits
class EquipmentItem(BaseModel):
    kind: str = Field(min_length=1, description=(
        "air tank / air compressor, propane (LPG) tank, boiler, personnel hoist, permanent elevator used for "
        "construction, elevator, escalator / moving walk, dumbwaiter / material lift / VRC / sidewalk elevator, "
        "wheelchair or stair lift"))
    label: Optional[str] = Field(None, description="Your name for it, e.g. 'Compressor #2'")
    volume_cu_ft: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description="Tank volume, cubic feet")
    volume_gallons: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description=(
        "Tank volume / water capacity, gallons (air tanks or LPG tanks)"))
    safety_valve_psi: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description=(
        "Safety valve set pressure, psi (air tank; or a steam boiler's steam safety valve)"))
    mawp_psi: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description="Maximum allowable working pressure "
                                                                                     "on the code plate")
    portable: Optional[bool] = Field(None, description="Air tank: portable (e.g. a towable or wheeled compressor)?")
    asme_stamped: Optional[bool] = Field(None, description="Air tank: has the ASME Code stamp?")
    scba: bool = Field(False, description="Breathing-air (SCBA) cylinder built and maintained to DOT rules")
    dot_cylinder: bool = Field(False, description="LPG: a DOT cylinder (e.g. forklift or BBQ bottle)")
    lpg_use: Optional[str] = Field(None, description="LPG: storage, dispensing, skid, trap, transport or mobile fuel")
    boiler_type: Optional[str] = Field(None, description=(
        "Boiler: steam, power, hot water, low-pressure, miniature or high-temperature water"))
    steam_psig: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description="Boiler steam pressure, psig")
    water_psi: Optional[float] = Field(None, ge=0, allow_inf_nan=False, description="Hot-water boiler: water pressure, psi")
    water_temp_f: Optional[float] = Field(None, ge=0, le=1500, allow_inf_nan=False, description=(
        "Hot-water boiler: water temperature, degrees F"))
    heating_surface_sqft: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    steam_drum_inside_diameter_in: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    water_capacity_gallons: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    burner_input_btu_per_hr: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    drive: Optional[str] = Field(None, description="Elevator: hydraulic, traction/cable, screw or rack and pinion")
    stories: Optional[int] = Field(None, ge=1, le=200, description="Elevator: stories served (fees are per story)")
    units: int = Field(1, ge=1, le=100, description="How many similar units in the same building")
    alterations: Optional[int] = Field(None, ge=1, le=50, description=(
        "Existing conveyance being altered: number of alterations to one unit (alteration permit instead of new)"))
    inspection_hours: Optional[float] = Field(None, gt=0, le=200, allow_inf_nan=False, description=(
        "Estimated DOSH inspection hours, to price the hourly inspection fee"))
    bridge: bool = Field(False, description="Personnel hoist: a bridge construction elevator")
    full_maintenance_contract: bool = Field(False, description=(
        "Elevator: covered for the whole permit term by a full maintenance contract with a C-11 company"))
    rented: bool = Field(False, description="Air tank: you rent it out (or rent it) for use at a workplace")
    last_inspected_on: Optional[str] = Field(None, description=(
        "Date of the last DOSH/qualified inspection or permit (for the next-due date)"))


EquipKind = Literal["air_tank", "lpg_tank", "boiler", "personnel_hoist", "permanent_elevator_construction",
                    "elevator", "escalator", "dumbwaiter_material_lift", "wheelchair_stair_lift"]
_EQUIP_WORDS = (
    ("permanent_elevator_construction", r"(permanent|building|passenger|freight) elevators? (used )?(as |for )?(a )?"
                                        r"(construction|hoist)"),
    ("personnel_hoist", r"personnel hoist|construction elevator|man ?hoist|buck ?hoist|alimak|construction hoist"),
    ("dumbwaiter_material_lift", r"dumbwaiter|material lift|\bvrc\b|vertical reciprocating|sidewalk elevator"),
    ("wheelchair_stair_lift", r"wheelchair|stair ?lift|stairway|platform lift|hand elevator"),
    ("escalator", r"escalator|moving walk"),
    ("elevator", r"elevator"),
    ("lpg_tank", r"propane|\blpg\b|lp gas|liquefied petroleum"),
    ("boiler", r"boiler"),
    ("air_tank", r"air tank|compressor|air receiver|receiver|compressed air"),
)


class EquipmentPermit(BaseModel):
    label: str
    kind: EquipKind
    status: Literal["permit_required", "exempt", "inspection_only", "not_covered", "need_info"]
    permit_required: Optional[bool] = None
    issuer: Optional[str] = None
    why: str = ""
    math: list[str] = Field(default_factory=list)
    permit_term: Optional[str] = None
    permit_expires_by: Optional[str] = None
    next_inspection_due: Optional[str] = None
    overdue: bool = False
    before_use: Optional[str] = None
    posting: Optional[str] = None
    fees: list[Fee] = Field(default_factory=list)
    one_time_fees_usd: Optional[float] = Field(None, description="Sum of the fixed one-time fees listed (hourly "
                                                                  "inspection only when inspection_hours is given)")
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


class EquipmentPermitsResult(_Base):
    as_of: str
    items: list[EquipmentPermit] = Field(default_factory=list)
    permits_required: int = 0
    one_time_fees_total_usd: float = 0


def _equip_item(raw, i: int) -> EquipmentItem:
    if isinstance(raw, EquipmentItem):
        return raw
    if isinstance(raw, str):
        raw = {"kind": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"equipment item {i + 1} must be an object like {{'kind': 'air compressor', "
                         f"'volume_gallons': 30, 'safety_valve_psi': 150}}")
    try:
        return EquipmentItem.model_validate(raw)
    except ValidationError as e:
        err = e.errors()[0]
        raise ValueError(f"equipment item {i + 1} ({raw.get('kind')}): {'.'.join(map(str, err['loc']))} "
                         f"{err['msg']}") from None


def _equip_kind(text: str) -> str:
    w = _words(text).replace("_", " ")
    for kind, pat in _EQUIP_WORDS:
        if re.search(pat, w) or w == kind.replace("_", " "):
            return kind
    raise ValueError(f"equipment kind {text!r} not recognized; use air tank, propane tank, boiler, personnel hoist, "
                     "elevator, escalator, dumbwaiter, material lift or wheelchair lift")


def _pv_late_days() -> tuple[int, float]:
    """(days, penalty %) for unpaid pressure-vessel fees; the day count is part of the rulebook key name."""
    key = next(k for k in core.rule(PV_FEES).parameters if k.startswith("late_penalty_pct_after_"))
    return int(re.search(r"after_(\d+)_days", key).group(1)), P(PV_FEES, key)


def _pv_fees(it: EquipmentItem, item_math: list) -> list[Fee]:
    fee, rate, inc = (P(PV_FEES, "permit_processing_fee_usd"), P(PV_FEES, "dosh_inspection_usd_per_hour"),
                      P(PV_FEES, "billing_increment_hours"))
    out = [Fee(item="Permit to operate processing", amount_usd=fee, basis="per permit", rule_id=PV_FEES)]
    if it.inspection_hours:
        billed = math.ceil(round(it.inspection_hours / inc, 9)) * inc
        out.append(Fee(item="DOSH inspection", amount_usd=round(billed * rate, 2), basis=(
            f"{_n(it.inspection_hours)} h billed as {_n(billed)} h x {_usd(rate)}/h"), rule_id=PV_FEES))
        item_math.append(f"inspection {_n(it.inspection_hours)} h -> {_n(billed)} h (in {_n(inc)} h steps) x "
                         f"{_usd(rate)} = {_usd(billed * rate)}")
    else:
        out.append(Fee(item="DOSH inspection", amount_usd=rate, when="per_hour", basis=(
            f"per hour incl. travel, billed in {_n(inc)} h steps (or use a qualified insurance-company inspector)"),
            rule_id=PV_FEES))
    return out


def _elev_drive(text: Optional[str]) -> Optional[str]:
    w = _words(text)
    if not w:
        return None
    if re.search(r"rack", w):
        return "rack_and_pinion"
    if re.search(r"screw", w):
        return "screw"
    if re.search(r"hydraulic|holeless|plunger|piston", w):
        return "hydraulic"
    if re.search(r"traction|cable|roped|geared|gearless|mrl|machine room", w):
        return "cable"
    raise ValueError(f"elevator drive {text!r} not recognized; use hydraulic, traction (cable), screw or rack and "
                     "pinion")


def _cable_tier_key(stories: int) -> str:
    """Periodic-fee key for a cabled elevator; the story bands are read from the rulebook keys."""
    for key in P(V_FEE_PERIODIC, "usd_per_unit"):
        if not key.startswith("cabled_"):
            continue
        if m := re.fullmatch(r"cabled_up_to_(\d+)_stories", key):
            lo, hi = 1, int(m.group(1))
        elif m := re.fullmatch(r"cabled_(\d+)_to_(\d+)_stories", key):
            lo, hi = int(m.group(1)), int(m.group(2))
        elif m := re.fullmatch(r"cabled_(\d+)_plus_stories", key):
            lo, hi = int(m.group(1)), math.inf
        else:
            continue
        if lo <= stories <= hi:
            return key
    raise KeyError(f"no cabled periodic fee band for {stories} stories")


def _air_tank(it, p: EquipmentPermit, ids, day, city_la):
    vol_lim, psi_lim = P(PV_AIR_SMALL, "exempt_volume_cuft_lte"), P(PV_AIR_SMALL, "exempt_safety_valve_set_psi_lte")
    vol = it.volume_cu_ft
    if vol is None and it.volume_gallons is not None:
        vol = it.volume_gallons / GALLONS_PER_CU_FT
        p.math.append(f"{_n(it.volume_gallons)} gal / {GALLONS_PER_CU_FT:.4f} gal per cu ft = {vol:.3f} cu ft")
    ids += [PV_AIR, PV_AIR_SMALL]
    if it.scba:
        scba = P(PV_AIR_SMALL, "scba_exempt_volume_cuft_lte")
        if vol is not None and vol <= scba:
            p.status, p.permit_required = "exempt", False
            p.why = (f"SCBA breathing-air cylinder of {vol:.3f} cu ft <= {_n(scba)} cu ft built and maintained to DOT "
                     "rules: no permit.")
            return
    if vol is None:
        p.status, p.why = "need_info", "Give volume_cu_ft or volume_gallons (from the tank's data plate)."
        return
    if vol <= vol_lim:
        if it.safety_valve_psi is None:
            p.status, p.why = "need_info", (f"{vol:.3f} cu ft <= {_n(vol_lim)} cu ft: exempt only if the safety valve "
                                            f"opens at no more than {_n(psi_lim)} psi. Give safety_valve_psi.")
            return
        if it.safety_valve_psi <= psi_lim:
            p.status, p.permit_required = "exempt", False
            p.math.append(f"{vol:.3f} cu ft <= {_n(vol_lim)} and valve {_n(it.safety_valve_psi)} psi <= {_n(psi_lim)} "
                          "-> exempt")
            p.why = ("Small air tank: no permit to operate, but the construction and other Unfired Pressure Vessel "
                     "rules still apply.")
            return
        p.math.append(f"{vol:.3f} cu ft <= {_n(vol_lim)} but valve {_n(it.safety_valve_psi)} psi > {_n(psi_lim)} -> "
                      "permit required")
    else:
        p.math.append(f"{vol:.3f} cu ft > {_n(vol_lim)} cu ft -> permit required")
    p.status, p.permit_required = "permit_required", True
    ids += [PV_AIR_TERM, PV_RENTAL, PV_FEES, PV_FOLLOWUP] + ([PV_LA] if city_la else [])
    p.why = (f"No one may operate it without a permit (except while a permit request is pending); each day is a "
             f"separate {P(PV_AIR, 'offense_class')}.")
    p.posting = (f"Post the permit {_join(P(PV_RENTAL, 'posting'), 'or')}; show it to inspectors.")
    p.before_use = "Inspection by DOSH or a qualified insurance-company inspector, then the permit."
    indef_mawp, indef_vol = P(PV_AIR_INDEF, "max_mawp_psi_lte"), P(PV_AIR_INDEF, "max_volume_cuft_lte")
    last = _date(it.last_inspected_on, "last_inspected_on")
    if (it.mawp_psi is not None and it.mawp_psi <= indef_mawp and vol <= indef_vol and it.asme_stamped):
        ids.append(PV_AIR_INDEF)
        p.math.append(f"MAWP {_n(it.mawp_psi)} psi <= {_n(indef_mawp)} and {vol:.3f} cu ft <= {_n(indef_vol)} with "
                      "ASME stamp -> indefinite permit")
        p.permit_term = (f"{P(PV_AIR_INDEF, 'permit_term')} after the inspection when placed in service; new "
                         f"inspection and permit on {_join(P(PV_AIR_INDEF, 'reinspection_triggers'), 'or')}")
    else:
        # portability unknown -> the shorter (portable) term, so a due date is never late
        yrs = P(PV_AIR_TERM, "other_max_years" if it.portable is False else "portable_max_years")
        what = {True: "portable", False: "non-portable", None: "portable assumed: the shorter term; give portable"}
        p.permit_term = (f"up to {yrs} years from inspection ({what[it.portable]}); expires early on "
                         f"{_join(P(PV_AIR_TERM, 'expires_early_on'), 'or')}")
        # 461(h) cannot be ruled out: a tank of 25 cu ft or less may hold an indefinite permit, so no due date is
        # declared overdue until the code plate and stamp are known
        maybe_indef = (vol <= indef_vol and it.asme_stamped is not False
                       and (it.mawp_psi is None or it.mawp_psi <= indef_mawp))
        if maybe_indef:
            ids.append(PV_AIR_INDEF)
            missing = _join([x for x, v in (("mawp_psi", it.mawp_psi), ("asme_stamped", it.asme_stamped)) if v is None])
            p.permit_term = (f"indefinite if the code plate shows a maximum allowable working pressure of "
                             f"{_n(indef_mawp)} psi or less and the tank is ASME Code stamped (or DOSH-accepted "
                             f"equivalent); otherwise {p.permit_term}. Give {missing} to settle it")
            p.math.append(f"{vol:.3f} cu ft <= {_n(indef_vol)} cu ft: indefinite permit possible (8 CCR 461(h)); "
                          f"{missing} not given")
        if last:
            exp = _plus_years(last, yrs)
            if maybe_indef:
                p.math.append(f"if it does not qualify for an indefinite permit: last inspection {last} + {yrs} years "
                              f"= {exp}" + (" (past)" if day > exp else ""))
            else:
                p.permit_expires_by, p.next_inspection_due, p.overdue = exp.isoformat(), exp.isoformat(), day > exp
                p.math.append(f"last inspection {last} + {yrs} years = {exp}")
    if it.rented:
        p.why += " A tank that needs a permit may not be rented out for workplace use until the permit is issued."
    if not city_la:
        p.fees = _pv_fees(it, p.math)


def _lpg_tank(it, p, ids, day, city_la):
    lim = P(PV_LPG, "permit_required_if_capacity_gallons_gt")
    ids.append(PV_LPG)
    gal = it.volume_gallons
    if gal is None and it.volume_cu_ft is not None:
        gal = it.volume_cu_ft * GALLONS_PER_CU_FT
        p.math.append(f"{_n(it.volume_cu_ft)} cu ft x {GALLONS_PER_CU_FT:.4f} = {gal:.1f} gal")
    if it.dot_cylinder:
        p.status, p.permit_required, p.why = "exempt", False, "DOT cylinders used and maintained to DOT rules need no permit."
        return
    if gal is None:
        p.status, p.why = "need_info", "Give volume_gallons (water capacity on the tank's data plate)."
        return
    if gal <= lim:
        p.status, p.permit_required = "exempt", False
        p.math.append(f"{_n(gal)} gal <= {_n(lim)} gal -> no permit")
        p.why = f"LP-gas tanks of {_n(lim)} gallons or less need no permit to operate."
        return
    p.math.append(f"{_n(gal)} gal > {_n(lim)} gal -> permit required")
    p.status, p.permit_required = "permit_required", True
    ids += [PV_FEES, PV_FOLLOWUP] + ([PV_LA] if city_la else [])
    yrs_by = P(PV_LPG, "permit_max_years")
    use = _words(it.lpg_use)
    key = ("dispensing_units_trap_tanks_skid_tanks" if re.search(r"dispens|trap|skid", use) else
           "transportation_mobile_fuel_storage_tanks" if re.search(r"stor|transport|mobile|fuel|cargo", use) else None)
    if key:
        yrs = yrs_by[key]
        p.permit_term = f"up to {yrs} years ({key.replace('_', ' ')}); expires on {_join(P(PV_LPG, 'expires_on'), 'or')}"
        last = _date(it.last_inspected_on, "last_inspected_on")
        if last:
            exp = _plus_years(last, yrs)
            p.permit_expires_by, p.overdue = exp.isoformat(), day > exp
            p.math.append(f"permit {last} + up to {yrs} years = {exp}")
    else:
        p.permit_term = ("up to " + ", ".join(f"{v} years for {k.replace('_', ' ')}" for k, v in yrs_by.items())
                         + " (give lpg_use)")
    p.why = "No one may fill it with LP-gas until the permit is issued and posted (except while a request is pending)."
    p.before_use = "DOSH (or insurance-company) inspection and permit before the first fill."
    if not city_la:
        p.fees = _pv_fees(it, p.math)


def _boiler(it, p, ids, day, city_la):
    ids += [PV_BOILER, PV_BOILER_SMALL]
    exempt_types = P(PV_BOILER_SMALL, "exempt_types")
    limits = P(PV_BOILER_SMALL, "exempt_if_all_lte")
    dfn = P(PV_BOILER_SMALL, "definitions_8_ccr_753")
    still = "construction, installation and automatic-control rules still apply"
    btype = _words(it.boiler_type)
    hit = next((t for t in exempt_types if btype and _words(t).replace(" boilers", "").replace(" boiler", "") in btype),
               None)

    def exempt(why):
        p.status, p.permit_required = "exempt", False
        p.why = why

    if hit:
        return exempt(f"{hit[0].upper() + hit[1:]} need no permit or annual inspection (8 CCR 771), but {still}.")
    vals = {k: getattr(it, k) for k in limits}
    if all(v is not None for v in vals.values()):
        within = all(vals[k] <= limits[k] for k in limits)
        p.math.append(", ".join(f"{k.replace('_', ' ')} {_n(vals[k])} {'<=' if vals[k] <= limits[k] else '>'} "
                                f"{_n(limits[k])}" for k in limits) + (" -> small boiler, exempt" if within else ""))
        if within:
            return exempt(f"Within every small-boiler limit: no permit or annual inspection (8 CCR 771); {still}.")
    steam_named = bool(re.search(r"steam|power|high pressure", btype))
    hot_named = bool(re.search(r"hot water|water heat\w*|hydronic|heating water|\bhw\b", btype))
    lp_steam = dfn["low_pressure_steam_max_psi_lte"]
    if hot_named and not steam_named and it.steam_psig is None:
        psi, temp = it.water_psi, it.water_temp_f
        lp_psi, lp_t = dfn["low_pressure_hot_water_max_psi_lte"], dfn["low_pressure_hot_water_max_temp_f_lte"]
        h_min, h_psi, h_t = (dfn["high_temperature_water_min_temp_f_gt"], dfn["high_temperature_water_if_psi_gt"],
                             dfn["high_temperature_water_if_temp_f_gt"])
        if psi is None or temp is None:
            p.status, p.why = "need_info", (
                f"Give water_psi and water_temp_f. A hot-water boiler at {_n(lp_psi)} psi or less and {_n(lp_t)} F or "
                f"less is a low-pressure boiler, and one heating water above {_n(h_min)} F at over {_n(h_psi)} psi or "
                f"above {_n(h_t)} F is a high-temperature water boiler (8 CCR 753); neither needs a permit or annual "
                "inspection (8 CCR 771).")
            return
        if psi <= lp_psi and temp <= lp_t:
            p.math.append(f"water {_n(psi)} psi <= {_n(lp_psi)} and {_n(temp)} F <= {_n(lp_t)} F -> low-pressure "
                          "(hot water heating) boiler")
            return exempt(f"Low-pressure hot-water heating boiler (8 CCR 753): no permit or annual inspection "
                          f"(8 CCR 771); {still}.")
        if temp > h_min and (psi > h_psi or temp > h_t):
            p.math.append(f"water {_n(temp)} F > {_n(h_min)} F at {_n(psi)} psi (over {_n(h_psi)} psi or "
                          f"{_n(h_t)} F) -> high-temperature water boiler")
            return exempt(f"High-temperature water boiler (8 CCR 753): no permit or annual inspection (8 CCR 771); "
                          f"{still}.")
        p.math.append(f"water {_n(psi)} psi > {_n(lp_psi)} psi at {_n(temp)} F (not above {_n(h_min)} F): neither "
                      "low-pressure nor high-temperature water -> subject to annual inspection")
    elif it.steam_psig is not None and it.steam_psig <= lp_steam:     # the 753 numbers decide, not the label
        valve = it.safety_valve_psi
        if valve is None:
            p.status, p.why = "need_info", (
                f"Steam {_n(it.steam_psig)} psig <= {_n(lp_steam)} psi: a low-pressure steam boiler (no permit) only "
                f"if its steam safety valve is also set at {_n(lp_steam)} psi or less (8 CCR 753). Give "
                "safety_valve_psi from the valve's nameplate.")
            return
        if valve <= lp_steam:
            p.math.append(f"steam {_n(it.steam_psig)} psig and safety valve {_n(valve)} psi <= {_n(lp_steam)} psi -> "
                          "low-pressure steam boiler")
            return exempt(f"Low-pressure steam boiler (8 CCR 753): no permit or annual inspection (8 CCR 771); "
                          f"{still}.")
        p.math.append(f"steam {_n(it.steam_psig)} psig but safety valve {_n(valve)} psi > {_n(lp_steam)} psi -> not a "
                      "low-pressure boiler -> subject to annual inspection")
    elif it.steam_psig is not None and it.steam_psig > dfn["power_boiler_steam_psi_gt"]:
        p.math.append(f"steam {_n(it.steam_psig)} psig > {_n(dfn['power_boiler_steam_psi_gt'])} psi -> power boiler "
                      "(8 CCR 753)")
    elif re.search(r"power|high pressure", btype):
        p.math.append(f"described as a {it.boiler_type} -> subject to annual inspection (not low-pressure, miniature "
                      "or high-temperature water)")
    else:
        p.status, p.why = "need_info", (
            (f"Give steam_psig and safety_valve_psi: a steam boiler at {_n(lp_steam)} psi or less with its safety valve "
             f"set at {_n(lp_steam)} psi or less is a low-pressure boiler (no permit); above that it is a power boiler "
             "(permit and yearly inspection)." if steam_named else
             "Give boiler_type (steam, power, hot water, low-pressure, miniature), steam_psig and safety_valve_psi, "
             "water_psi and water_temp_f, or all five size limits ("
             + ", ".join(k.replace('_', ' ') for k in limits) + ")."))
        return
    p.status, p.permit_required = "permit_required", True
    ids += [PV_FEES, PV_FOLLOWUP] + ([PV_LA] if city_la else [])
    yrs = P(PV_BOILER, "inspection_interval_years")
    p.why = "May not be operated until a permit to operate is issued and posted under glass on or near the boiler."
    p.before_use = ("Internal and external inspection by DOSH or a qualified insurance-company inspector, then the "
                    "permit posted, before it is operated.")
    p.permit_term = (f"inspected internally and externally every {yrs} year; permit expires on "
                     f"{P(PV_BOILER, 'permit_expires_on')}; temporary permits up to "
                     f"{P(PV_BOILER, 'temporary_permit_max_days')} days for repairs")
    p.posting = "Under glass on or near the boiler."
    last = _date(it.last_inspected_on, "last_inspected_on")
    if last:
        nxt = _plus_years(last, yrs)
        p.next_inspection_due, p.overdue = nxt.isoformat(), day > nxt
        p.math.append(f"last inspection {last} + {yrs} year = {nxt}")
    if not city_la:
        p.fees = _pv_fees(it, p.math)


def _hoist(it, p, ids, day, permanent: bool):
    ids += [H_PERMIT, H_INSPECT] + ([H_PERM_ELEV] if permanent else [])
    p.status, p.permit_required, p.issuer = "permit_required", True, "Cal/OSHA (DOSH) Elevator Unit"
    p.why = ("No personnel hoist may operate without a valid permit; DOSH inspects it before it carries workers (it "
             "may let a safe hoist run while the permit is processed)."
             + (" A permanent elevator used as a construction hoist must meet the personnel hoist rules and may run "
                "on automatic operation only after DOSH issues a permit." if permanent else ""))
    p.permit_term = f"{P(H_PERMIT, 'permit_term_text')}; revocable if periodic inspections show it is unsafe"
    p.posting = "Conspicuously in the car."
    trig = _join(P(H_INSPECT, "acceptance_inspection_triggers"), "or")
    p.before_use = f"DOSH acceptance inspection and tests on {trig} (a tower jump is not a major alteration)."
    last = _date(it.last_inspected_on, "last_inspected_on")
    if it.bridge:
        days = P(H_INSPECT, "bridge_construction_elevator_interval_days_max")
        if last:
            nxt = last + timedelta(days=days)
            p.math.append(f"last inspection {last} + {days} days = {nxt}")
        extra = f"; also after winds over {_n(P(H_INSPECT, 'bridge_wind_reinspection_mph_gt'))} mph"
        interval = f"every {days} days{extra}"
    else:
        months = P(H_INSPECT, "periodic_inspection_interval_months_max")
        if last:
            nxt = _plus_months(last, months)
            p.math.append(f"last inspection {last} + {months} months = {nxt}")
        interval = f"every {months} months"
    if last:
        p.next_inspection_due, p.overdue = nxt.isoformat(), day > nxt
    p.why += (f" After acceptance, a qualified person designated by the employer inspects and tests it at least "
              f"{interval}; keep the signed record of the latest one at the jobsite.")
    rate = P(H_PERMIT, "new_installation_inspection_fee_usd_per_hour")
    if it.inspection_hours:
        hrs = math.ceil(round(it.inspection_hours, 9))
        p.fees.append(Fee(item="DOSH inspection of the new installation", amount_usd=hrs * rate,
                          basis=f"{_n(it.inspection_hours)} h billed as {hrs} h x {_usd(rate)}", rule_id=H_PERMIT))
    else:
        p.fees.append(Fee(item="DOSH inspection of the new installation", amount_usd=rate, when="per_hour",
                          basis="per hour or fraction", rule_id=H_PERMIT))
    p.fees.append(Fee(item="Periodic reinspection", amount_usd=P(H_PERMIT, "reinspection_fee_usd_per_unit") * it.units,
                      when="recurring", basis=f"{_usd(P(H_PERMIT, 'reinspection_fee_usd_per_unit'))} per unit",
                      rule_id=H_PERMIT))
    old = P(H_PERMIT, "conflicting_fee_table_still_in_1604_29_b_usd")
    p.math.append("note: 8 CCR 1604.29(b) still prints an older fee table (" + ", ".join(
        f"{_usd(v)} for {k.replace('_', ' ')}" for k, v in old.items()) + "); the 344.30 fees are used")


def _conveyance(it, p, ids, day, kind, building):
    rate = P(V_FEE_HOUR, "new_installation_inspection_usd_per_hour")
    if building == "single_family":
        ids.append(V_EXCL)
        p.status, p.permit_required = "not_covered", False
        p.why = ("In a single-unit private home not accessible to the public, the Elevator Safety Orders (and DOSH "
                 "permits) do not apply.")
        return
    if building == "two_unit":
        ids += [V_EXCL, V_FEE_HOUR]
        p.status, p.permit_required = "inspection_only", False
        p.why = (f"Building with no more than {P(V_EXCL, 'max_dwelling_units_for_limited_rule')} dwelling units not "
                 "open to the public: DOSH inspects it when installed or altered, before use; no annual permit.")
        p.fees.append(Fee(item="DOSH inspection", amount_usd=rate, when="per_hour", basis="per hour or fraction",
                          rule_id=V_FEE_HOUR))
        return
    ids += [V_PERMIT, V_NEW, V_REINSP, V_TIMES, V_FEE_HOUR, V_FEE_PERIODIC, V_LATE]
    p.status, p.permit_required, p.issuer = "permit_required", True, "Cal/OSHA (DOSH) Elevator Unit"
    t = P(V_TIMES, "historical_processing_days")
    p.why = ("No elevator or other covered conveyance may operate without a current DOSH permit. The installer files "
             "erection plans (or, as a recognized elevator company, a notice of intent) and the "
             f"{P(V_NEW, 'inspection_request_by')} requests the inspection when ready. DOSH's historical processing "
             f"time was {t['minimum']} days minimum, {t['median']} median: request early.")
    p.before_use = "DOSH inspection and permit to operate before service (the inspector may allow a safe device to " \
                   "run while the permit is processed)."
    p.posting = (P(V_PERMIT, "posting")["passenger_freight_incline_elevators"] if kind == "elevator" else
                 P(V_PERMIT, "posting")["other_devices"])
    std, mx = P(V_PERMIT, "max_permit_years_standard"), P(V_PERMIT, "max_permit_years_with_full_maintenance_contract")
    yrs = mx if it.full_maintenance_contract else std
    if it.full_maintenance_contract:
        ids.append(V_2YR)
        p.permit_term = (f"up to {mx} years with a full maintenance contract with a "
                         f"{P(V_2YR, 'required_contractor_license')} company (service at least "
                         f"{P(V_2YR, 'min_service_frequency')}; documents within "
                         f"{P(V_2YR, 'submit_documents_within_days_of_notice')} days of DOSH's notice)")
    else:
        p.permit_term = f"up to {std} year (up to {mx} with a full maintenance contract)"
    last = _date(it.last_inspected_on, "last_inspected_on")
    if last:
        exp = _plus_years(last, yrs)
        req_by = exp - timedelta(days=P(V_REINSP, "request_inspection_days_before_permit_expiration"))
        p.permit_expires_by, p.next_inspection_due, p.overdue = exp.isoformat(), exp.isoformat(), day > exp
        p.math.append(f"last inspection {last} + {yrs} year(s) = reinspection due by {exp}; request it from {req_by} "
                      f"({P(V_REINSP, 'request_inspection_days_before_permit_expiration')} days before)")
    # one-time permit fee
    drive = _elev_drive(it.drive) if kind == "elevator" else None
    units = it.units
    if it.alterations:
        ids.append(V_FEE_ALT)
        base_key = ("one_or_two_alterations_single_unit_usd" if it.alterations <= 2 else
                    "three_or_more_alterations_single_unit_usd")
        base, add = P(V_FEE_ALT, base_key), P(V_FEE_ALT, "additional_similar_unit_same_building_usd")
        amt = base + add * (units - 1)
        p.math.append(f"alteration permit: {_usd(base)} ({it.alterations} alteration(s) to one unit) + {_usd(add)} x "
                      f"{units - 1} similar unit(s) = {_usd(amt)}")
        p.fees.append(Fee(item="Alteration permit incl. plan review", amount_usd=amt, rule_id=V_FEE_ALT))
        hour_key = "alteration_inspection_usd_per_hour"
    else:
        ids.append(V_FEE_NEW)
        hour_key = "new_installation_inspection_usd_per_hour"
        amt = None
        if kind == "elevator":
            if drive is None or it.stories is None:
                p.math.append("give drive and stories to price the installation permit")
            else:
                hyd = drive == "hydraulic"
                per_story = P(V_FEE_NEW, "first_hydraulic_usd_per_story" if hyd else
                              "first_cable_screw_rack_pinion_usd_per_story")
                per_unit = P(V_FEE_NEW, "additional_similar_hydraulic_usd_per_unit" if hyd else
                             "additional_similar_cable_usd_per_unit")
                amt = per_story * it.stories + per_unit * (units - 1)
                p.math.append(f"installation permit: first {drive.replace('_', ' ')} elevator {_usd(per_story)} x "
                              f"{it.stories} stories + {_usd(per_unit)} x {units - 1} additional = {_usd(amt)}")
        elif kind == "escalator":
            first, add = P(V_FEE_NEW, "first_escalator_moving_walk_usd"), P(V_FEE_NEW, "additional_escalator_moving_walk_usd")
            amt = first + add * (units - 1)
            p.math.append(f"installation permit: {_usd(first)} first + {_usd(add)} x {units - 1} = {_usd(amt)}")
        else:
            key = ("dumbwaiter_material_lift_vrc_sidewalk_usd_per_unit" if kind == "dumbwaiter_material_lift" else
                   "wheelchair_stair_lift_hand_elevator_usd_per_unit")
            amt = P(V_FEE_NEW, key) * units
            p.math.append(f"installation permit: {_usd(P(V_FEE_NEW, key))} x {units} unit(s) = {_usd(amt)}")
        if amt is not None:
            p.fees.append(Fee(item="Erection/installation permit incl. plan approval", amount_usd=amt,
                              rule_id=V_FEE_NEW))
            if units > 1 or kind in ("elevator", "escalator"):
                p.math.append("assumes the first unit is the first of its kind in the building")
    hr = P(V_FEE_HOUR, hour_key)
    if it.inspection_hours:
        hrs = math.ceil(round(it.inspection_hours, 9))
        p.fees.append(Fee(item="DOSH inspection", amount_usd=hrs * hr, basis=(
            f"{_n(it.inspection_hours)} h billed as {hrs} h (hour or fraction) x {_usd(hr)}"), rule_id=V_FEE_HOUR))
    else:
        p.fees.append(Fee(item="DOSH inspection", amount_usd=hr, when="per_hour", basis="per hour or fraction",
                          rule_id=V_FEE_HOUR))
    per = P(V_FEE_PERIODIC, "usd_per_unit")
    pkey = None
    if kind == "elevator" and drive == "hydraulic":
        pkey = "hydraulic_elevator"
    elif kind == "elevator" and drive == "cable" and it.stories:
        pkey = _cable_tier_key(it.stories)
    elif kind == "elevator" and drive == "rack_and_pinion":
        pkey = "rack_and_pinion_elevator"
    elif kind == "elevator" and drive == "screw":
        pkey = "screw_type_elevator"
    elif kind == "escalator":
        pkey = "escalator_moving_walk"
    elif kind == "wheelchair_stair_lift":
        pkey = "wheelchair_lift"
    elif kind == "dumbwaiter_material_lift":
        pkey = "power_dumbwaiter_material_lift"
    if pkey:
        p.fees.append(Fee(item="Periodic reinspection", amount_usd=per[pkey] * units, when="recurring",
                          basis=f"{_usd(per[pkey])} per unit ({pkey.replace('_', ' ')}), at most "
                                f"{P(V_FEE_PERIODIC, 'max_charged_inspections_per_permit_year')} charged per permit year",
                          rule_id=V_FEE_PERIODIC))
    mail = P(V_FEE_PERIODIC, "permit_processing_and_mailing_fee_max_usd")
    p.fees.append(Fee(item="Permit processing and mailing", amount_usd=mail, when="recurring",
                      basis=f"actual cost, at most {_usd(mail)} per permit issued", rule_id=V_FEE_PERIODIC))
    p.math.append(f"unpaid fees double after {P(V_LATE, 'payment_due_days')} days; an inspection that ends in an "
                  f"Order Prohibiting Use adds {_usd(P(V_LATE, 'order_prohibiting_use_fee_usd'))}")


def equipment_operating_permits(equipment, city=None, building_type=None, as_of=None) -> EquipmentPermitsResult:
    if isinstance(equipment, (str, dict, EquipmentItem)):
        equipment = [equipment]
    if not equipment:
        raise ValueError("equipment is required, e.g. [{'kind': 'air compressor', 'volume_gallons': 30, "
                         "'safety_valve_psi': 150}]")
    day = _as_of(as_of)
    city_la = is_city_of_los_angeles(city)
    building = building_from(building_type)
    acc = _Acc()
    res = EquipmentPermitsResult(as_of=day.isoformat())
    for i, raw in enumerate(equipment):
        it = _equip_item(raw, i)
        kind = _equip_kind(it.kind)
        p = EquipmentPermit(label=it.label or f"{it.kind} #{i + 1}", kind=kind, status="need_info")
        ids: list[str] = []
        if kind in ("air_tank", "lpg_tank", "boiler"):
            p.issuer = ("Los Angeles Department of Building and Safety (Pressure Vessel desk)" if city_la else
                        "Cal/OSHA (DOSH) Pressure Vessel Unit")
            {"air_tank": _air_tank, "lpg_tank": _lpg_tank, "boiler": _boiler}[kind](it, p, ids, day, city_la)
            if city_la and p.permit_required:
                p.why += (f" In the City of Los Angeles the permit comes from LADBS ({P(PV_LA, 'phone')}, "
                          f"{P(PV_LA, 'website')}); LADBS fees are not in this rulebook.")
        elif kind in ("personnel_hoist", "permanent_elevator_construction"):
            _hoist(it, p, ids, day, kind == "permanent_elevator_construction")
        else:
            _conveyance(it, p, ids, day, kind, building)
        one = [f.amount_usd for f in p.fees if f.when == "one_time" and f.amount_usd is not None]
        p.one_time_fees_usd = round(sum(one), 2) if one else None
        acc.use(*ids)
        p.rule_ids = list(dict.fromkeys(ids))
        p.citation, p.verified = _cit(p.rule_ids), _ok(p.rule_ids)
        if p.overdue:
            p.math.append(f"OVERDUE as of {day}")
        res.items.append(p)
    res.permits_required = sum(p.permit_required is True for p in res.items)
    res.one_time_fees_total_usd = round(sum(p.one_time_fees_usd or 0 for p in res.items), 2)
    if any(PV_FEES in p.rule_ids for p in res.items):
        prop = P(PV_FEES, "proposed_2026")
        acc.note(f"DOSH proposed (notice {prop['notice_date']}) raising the permit fee to "
                 f"{_usd(prop['permit_processing_fee_usd'])} and inspections to "
                 f"{_usd(prop['dosh_inspection_usd_per_hour'])}/h, effective {prop['effective']}; the current "
                 f"{_usd(P(PV_FEES, 'permit_processing_fee_usd'))} and {_usd(P(PV_FEES, 'dosh_inspection_usd_per_hour'))}"
                 f"/h are used. Unpaid fees carry a {_n(_pv_late_days()[1])}% penalty after {_pv_late_days()[0]} "
                 "days. A follow-up inspection is free if you correct a "
                 f"preliminary order and tell DOSH within {P(PV_FOLLOWUP, 'grace_days_after_compliance_date')} days of "
                 "the compliance date.", PV_FEES, PV_FOLLOWUP)
    need = [p for p in res.items if p.status == "need_info"]
    over = [p for p in res.items if p.overdue]
    res.summary = (f"{res.permits_required} of {len(res.items)} item(s) need a permit to operate"
                   + (f"; {len(need)} need more facts" if need else "")
                   + (f"; overdue: {_join([p.label for p in over])}" if over else "")
                   + (f"; fixed one-time fees {_usd(res.one_time_fees_total_usd)}" if res.one_time_fees_total_usd else "")
                   + ".")
    res.next_steps = ([f"{p.label}: inspection/permit overdue since {p.next_inspection_due or p.permit_expires_by}; "
                       "stop using it until inspected." for p in over]
                      + [f"{p.label}: {p.why}" for p in need]
                      + [f"{p.label}: {p.before_use}" for p in res.items if p.status == "permit_required" and p.before_use
                         and not p.overdue])
    res.next_step = (res.next_steps[0] if res.next_steps else
                     "Keep each permit to operate posted and renew it before it expires." if res.permits_required else
                     "No permit to operate is needed for this equipment.")
    return acc.finish(res)


RULE_IDS = tuple(dict.fromkeys(
    v for k, v in list(globals().items())
    if k.isupper() and isinstance(v, str) and (v.startswith("cert-") or v.startswith("rrp-"))))


# ================================================================ MCP
def _tool_docs() -> dict:
    va = _n(P(E_DEF, "covered_min_volt_amperes"))
    lead = _n(P(L_CDPH, "exposure_twa_8hr_ug_per_m3_gte"))
    cutoff = P(R_APPLY, "built_before_year")
    ratio = P(E_RATIO, "max_trainees_per_certified_electrician")
    hrs = P(E_HOURS, "required_hours")
    return {
        "required": (
            "Which trade certifications a California construction job needs, from a list of tasks (e.g. ['panel "
            "upgrade', 'scrape and repaint trim', 'crane pick', 'fire alarm']). Covers DIR electrician certification "
            f"for anyone making connections of {va} VA or more for a C-10 contractor (General, Residential, Fire/Life "
            "Safety, Voice Data Video, Nonresidential Lighting; trainee, apprentice and qualifier routes; C-7/C-45 "
            f"exemptions), EPA RRP certified renovator and firm certification for pre-{cutoff} housing and "
            f"child-occupied facilities (with the minor-repair test), CDPH lead certification at {lead} ug/m3, crane "
            "operator certification plus employer evaluation (not for derricks, sideboom cranes, 2,000 lb equipment "
            "or small delivery knuckle-booms), elevator CCCM/CQCC certification, a licensed blaster "
            "on site, and personnel hoist / pressure vessel permits. Returns each requirement as required / "
            "conditional / not_required with who must hold it, acceptable credentials, cost, proof to keep on site, "
            "why, citations, the exposure and next steps."),
        "crew": (
            "Check a crew before mobilizing: for each worker {name, task, credentials} says whether they may do "
            "that task (General vs Residential vs specialty electrician types, trainees, apprentices, RRP renovator "
            "or trained worker, CDPH lead, crane certificate + employer evaluation, CCCM, blaster), whether each card "
            f"is valid, due soon, past its renew-by date or expired (electrician renewals due "
            f"{P(E_FILE, 'conservative_days_before_expiration')} days before expiration, RRP renovator "
            f"{P(R_REFRESH, 'certification_years_without_hands_on_refresher')} or "
            f"{P(R_REFRESH, 'certification_years_with_hands_on_refresher')} years from the course, CCCM "
            f"{P(V_CCCM, 'validity_years')} years, blaster {P(B_RENEW, 'validity_years')} years), and crew checks: "
            f"no more than {ratio} trainee per certified electrician, a current certified renovator and licensed "
            "blaster on site, crane trainees with a certified trainer. Credentials can be text like 'General "
            "Electrician exp 2027-05-31'."),
        "path": (
            "How a worker gets DIR electrician certification: give the type and hours by work area (e.g. "
            "{'commercial wiring': 5000, 'residential wiring': 3500, 'stock room': 500}). Applies the 8 CCR 291.1 "
            "area caps and minimum areas, required hours ("
            + ", ".join(f"{k} {_n(v)}" for k, v in hrs.items())
            + f"), apprenticeship / NICET / out-of-state routes, fees ({_usd(P(E_FEE, 'total_initial_usd'))} to "
              f"apply), the {P(E_WINDOW, 'eligibility_window_years')}-year exam window after DIR's eligibility "
              f"notice and the {P(E_WINDOW, 'min_days_between_attempts')}-day retake wait, plus the trainee route "
              "until certified."),
        "equipment": (
            "DOSH permits to operate for jobsite equipment: air tanks (exempt at "
            f"{_n(P(PV_AIR_SMALL, 'exempt_volume_cuft_lte'))} cu ft or less with a safety valve at "
            f"{_n(P(PV_AIR_SMALL, 'exempt_safety_valve_set_psi_lte'))} psi or less), LP-gas tanks over "
            f"{_n(P(PV_LPG, 'permit_required_if_capacity_gallons_gt'))} gal, boilers, construction personnel hoists, "
            "elevators, escalators, dumbwaiters, material lifts and wheelchair lifts. Returns permit required / "
            "exempt / inspection only with the math, permit term, next inspection due, posting, fees (e.g. first "
            "elevator per story, additional units per unit, hourly inspections) and the City of Los Angeles (LADBS) "
            "exception for pressure vessels."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    nn = {"ge": 0, "allow_inf_nan": False}
    cutoff = P(R_APPLY, "built_before_year")
    lead = _n(P(L_CDPH, "exposure_twa_8hr_ug_per_m3_gte"))
    classes_doc = "Your CSLB classifications, e.g. ['C-10'] or 'B, C-7' (decides whether electrician rules apply)"
    building_doc = ("single_family, two_unit, multifamily, residential, hotel_motel, senior_or_care (senior housing, "
                    "assisted living, nursing home), zero_bedroom (studios, dorms, SROs), child_occupied, school, "
                    "public_or_commercial or industrial (words like 'house', 'apartment', 'daycare', 'office' work)")

    @mcp.tool(name="certifications_required", annotations=ro, description=docs["required"])
    def _certifications_required(
        tasks: Annotated[list[str], Field(min_length=1, description=(
            "The work, e.g. ['rough wiring', 'fire alarm devices', 'scrape and repaint trim', 'crane pick']"))],
        employer_license_classes: Annotated[Optional[Union[str, list[str]]], Field(description=classes_doc)] = None,
        year_built: Annotated[Optional[int], Field(ge=1600, le=2100, description="Year the building was built")] = None,
        building_type: Annotated[Optional[str], Field(description=building_doc)] = None,
        built_before_1978: Annotated[Optional[bool], Field(description=(
            f"Built before {cutoff}? (use when the exact year is unknown)"))] = None,
        lead_exposure_ug_m3: Annotated[Optional[float], Field(**nn, description=(
            f"Measured lead exposure, ug/m3 8-hour TWA (CDPH certification at {lead} or more)"))] = None,
        lead_free_determination: Annotated[bool, Field(description=(
            "A certified inspector/risk assessor or certified renovator has determined the components lead-free"))]
        = False,
        interior_painted_sqft_per_room: Annotated[Optional[float], Field(**nn, description=(
            "Painted area disturbed per room inside, sq ft (RRP minor-repair test)"))] = None,
        exterior_painted_sqft: Annotated[Optional[float], Field(**nn, description=(
            "Painted area disturbed outside, sq ft (RRP minor-repair test)"))] = None,
        emergency_renovation: Annotated[bool, Field(description=(
            "Unplanned emergency work after a sudden event (RRP emergency exemption)"))] = False,
        dust_clearance_testing: Annotated[bool, Field(description=(
            "The contract or another law requires dust clearance sampling instead of cleaning verification"))] = False,
        crane_outside_construction: Annotated[bool, Field(description=(
            "Crane work in a yard, shop or plant (general industry, 8 CCR 5006.1)"))] = False,
        out_of_state_workers: Annotated[bool, Field(description="Bringing in electricians from another state")]
        = False,
        city: Annotated[Optional[str], Field(description=(
            "Jobsite city ('LA', 'City of Los Angeles' sends pressure-vessel permits to LADBS)"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> CertificationsRequiredResult:
        return certifications_required(tasks, employer_license_classes, year_built, building_type, built_before_1978,
                                       lead_exposure_ug_m3, lead_free_determination, interior_painted_sqft_per_room,
                                       exterior_painted_sqft, emergency_renovation, dust_clearance_testing,
                                       crane_outside_construction, out_of_state_workers, city, as_of)

    @mcp.tool(name="check_crew_certifications", annotations=ro, description=docs["crew"])
    def _check_crew_certifications(
        crew: Annotated[list[CrewMember], Field(min_length=1, max_length=300, description=(
            "[{name, task, credentials: ['General Electrician exp 2027-05-31' or {type, expires, issued, hands_on}], "
            "is_c10_qualifier, supervised_by, employer_evaluated}]"))],
        employer_license_classes: Annotated[Optional[Union[str, list[str]]], Field(description=classes_doc)] = None,
        year_built: Annotated[Optional[int], Field(ge=1600, le=2100, description="Year the building was built")] = None,
        building_type: Annotated[Optional[str], Field(description=building_doc)] = None,
        built_before_1978: Annotated[Optional[bool], Field(description=f"Built before {cutoff}?")] = None,
        lead_exposure_ug_m3: Annotated[Optional[float], Field(**nn, description=(
            "Measured lead exposure, ug/m3 8-hour TWA"))] = None,
        firm_rrp_certification_expires: Annotated[Optional[str], Field(description=(
            "Your EPA RRP firm certificate's expiration date (for the timely recertification date)"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> CrewCheckResult:
        return check_crew_certifications(crew, employer_license_classes, year_built, building_type, built_before_1978,
                                         lead_exposure_ug_m3, firm_rrp_certification_expires, as_of)

    @mcp.tool(name="electrician_certification_path", annotations=ro, description=docs["path"])
    def _electrician_certification_path(
        certification_type: Annotated[str, Field(min_length=1, description=(
            "General Electrician, Residential Electrician, Fire/Life Safety, Voice Data Video or Nonresidential "
            "Lighting ('GE', 'FLS', 'VDV', 'NRLT' work)"))],
        hours_by_area: Annotated[Optional[dict[str, float]], Field(description=(
            "Documented hours for a C-10 contractor by work area, e.g. {'commercial wiring': 5000, 'residential "
            "wiring': 3500, 'stock room': 500}"))] = None,
        completed_apprenticeship: Annotated[bool, Field(description=(
            "Completed a state or federally approved apprenticeship in this classification"))] = False,
        months_left_in_apprenticeship: Annotated[Optional[float], Field(**nn, description=(
            "Registered apprentice: months until the apprenticeship term ends"))] = None,
        nicet_fire_alarm_level: Annotated[Optional[int], Field(ge=1, le=4, description=(
            "NICET Fire Alarm Systems level held (Fire/Life Safety only)"))] = None,
        out_of_state_license: Annotated[bool, Field(description="Holds an electrician license from another state")]
        = False,
        military_or_c7_experience: Annotated[bool, Field(description=(
            "Has military electrical experience or work for a C-7 low voltage contractor"))] = False,
        eligibility_notice_date: Annotated[Optional[str], Field(description=(
            "Date of DIR's notice of eligibility to take the exam"))] = None,
        last_exam_date: Annotated[Optional[str], Field(description="Date of the last exam attempt")] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> ElectricianPathResult:
        return electrician_certification_path(certification_type, hours_by_area, completed_apprenticeship,
                                              months_left_in_apprenticeship, nicet_fire_alarm_level,
                                              out_of_state_license, military_or_c7_experience,
                                              eligibility_notice_date, last_exam_date, as_of)

    @mcp.tool(name="equipment_operating_permits", annotations=ro, description=docs["equipment"])
    def _equipment_operating_permits(
        equipment: Annotated[list[EquipmentItem], Field(min_length=1, max_length=200, description=(
            "[{kind: 'air compressor', volume_gallons: 30, safety_valve_psi: 150, portable: true}, {kind: 'elevator', "
            "drive: 'hydraulic', stories: 4, units: 2}, {kind: 'personnel hoist', last_inspected_on: '2026-09-01'}]"))],
        city: Annotated[Optional[str], Field(description=(
            "Jobsite city ('LA', 'City of Los Angeles' -> pressure-vessel permits from LADBS)"))] = None,
        building_type: Annotated[Optional[str], Field(description=(
            "For elevators: single_family (not covered), two_unit (inspection only) or any other type"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> EquipmentPermitsResult:
        return equipment_operating_permits(equipment, city, building_type, as_of)
