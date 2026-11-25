"""Cal/OSHA jobsite hazards for California construction, beyond the written programs in safety_programs.py.

  wildfire_smoke_requirements    PM2.5 AQI (or an on-site PM2.5 reading) + hours exposed -> does 8 CCR 5141.1 apply,
                                 exemptions, AQI category, voluntary vs mandatory respirators (with the minimum
                                 assigned protection factor above AQI 500), controls, training and the next step
  overhead_line_clearance        line voltage + equipment (+ planned closest distance) -> Table 1 or Table 2 minimum
                                 clearance (8 CCR 2946), warning sign (2947), spotter, utility notice (2948) and the
                                 qualified-electrical-worker exemption (2949)
  confined_space_classification  space facts + meter readings -> confined space? permit-required? entry allowed?
                                 (8 CCR 1950-1962 as amended effective 2026-01-01), with duties by role
  valley_fever_requirements      county (+ the CDPH case rate) -> highly endemic? training, dust controls and case
                                 reporting (Labor Code 6709 as described by Cal/OSHA)
  jobsite_hazard_checklist       list of tasks + your role -> requirements per hazard (lifts, forklifts, hot work,
                                 demolition, traffic control, noise, electrical, lockout, confined spaces, smoke,
                                 Valley Fever, PPE, fire protection), a Table N-1 noise dose, an extinguisher count
                                 and the multi-employer citation picture (8 CCR 336.10-336.11)

Every legal number, threshold and date comes from core.rule(id).param(key) at call time, and every rule a result
relies on is cited through core.Result.cite(), so `verified` is True only when all of them were verified. The pack is
research/web_calosha-construction-2.verified.json (rule ids "osha2-...").

Not legal values (so not in the rulebook): the equipment, task, role and county spellings below, which only map a
user's words onto the rules. County names are recognized with the county lists of rule usa-regional-centers-by-county;
that rule is used for spelling only and is not cited.
"""
import math
import re
from datetime import date
from difflib import get_close_matches
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from . import core, logic

# ---------------------------------------------------------------- rule ids
# wildfire smoke (8 CCR 5141.1)
SMOKE_SCOPE, SMOKE_EXEMPT, SMOKE_CHECK = ("osha2-smoke-scope-aqi-151", "osha2-smoke-exemptions",
                                          "osha2-smoke-check-aqi-each-shift")
SMOKE_COMM, SMOKE_TRAIN, SMOKE_CONTROLS = ("osha2-smoke-communication-system", "osha2-smoke-training-appendix-b",
                                           "osha2-smoke-engineering-and-administrative-controls")
SMOKE_VOL, SMOKE_MAND, SMOKE_EMERG = ("osha2-smoke-voluntary-respirators-151-500",
                                      "osha2-smoke-mandatory-respirators-over-500", "osha2-smoke-emergency-operations")
SMOKE_CATS, SMOKE_CONV, SMOKE_MED = ("osha2-smoke-aqi-categories", "osha2-smoke-pm25-to-aqi-conversion",
                                     "osha2-smoke-medical-treatment-no-reprisal")
SMOKE_EPA, RESP_APF = "osha2-smoke-epa-current-aqi-pm25-breakpoints", "osha2-resp-assigned-protection-factors"
# overhead high-voltage lines (8 CCR 2700, 2946-2949, 1518)
HV_DEF, HV_GUARD, HV_OVER = ("osha2-elec-high-voltage-600v", "osha2-elec-overhead-lines-guard-before-work",
                             "osha2-elec-overhead-no-work-over-lines")
HV_T1, HV_T2, HV_STORE = ("osha2-elec-overhead-table1-general-clearances",
                          "osha2-elec-overhead-table2-boom-storage-clearances", "osha2-elec-overhead-storage-near-lines")
HV_ENERGIZED, HV_SPOTTER, HV_SIGN = ("osha2-elec-overhead-assume-energized", "osha2-elec-overhead-spotter",
                                     "osha2-elec-overhead-warning-sign-equipment")
HV_NOTIFY, HV_QEW_EXEMPT, HV_QEW = ("osha2-elec-notify-line-operator", "osha2-elec-overhead-qualified-worker-exemption",
                                    "osha2-elec-qualified-electrical-worker-2-years")
HV_SURVEY, AWP_HV = "osha2-elec-pre-work-circuit-survey", "osha2-awp-instruction-and-assembly"
# confined spaces in construction (8 CCR 1950-1962)
CS_SCOPE, CS_DEF, CS_PERMIT_DEF = ("osha2-cs-scope-and-exceptions", "osha2-cs-confined-space-definition",
                                   "osha2-cs-permit-required-definition")
CS_LFL, CS_O2, CS_DUST, CS_TOXIC = ("osha2-cs-hazatm-flammable-10pct-lfl", "osha2-cs-hazatm-oxygen-19-5-23-5",
                                    "osha2-cs-hazatm-combustible-dust-20pct-mec", "osha2-cs-hazatm-toxic-over-pel")
CS_SURVEY, CS_ONGOING, CS_NOTIFY_NEW, CS_CLASSIFY = (
    "osha2-cs-initial-survey-competent-person", "osha2-cs-ongoing-inspection-new-spaces",
    "osha2-cs-notify-new-spaces-to-controlling-contractor", "osha2-cs-competent-person-classifies-permit-spaces")
CS_SIGN, CS_INFORM, CS_PREVENT, CS_PROGRAM = (
    "osha2-cs-danger-sign-permit-space", "osha2-cs-inform-reps-and-controlling-contractor",
    "osha2-cs-prevent-unauthorized-entry", "osha2-cs-written-permit-space-program")
CS_ALT, CS_ALT_CERT, CS_TEST_ORDER, CS_RECLASSIFY = (
    "osha2-cs-alternate-procedure-ventilation-only", "osha2-cs-alternate-procedure-written-certification",
    "osha2-cs-atmospheric-test-order", "osha2-cs-reclassify-non-permit-certification")
CS_EXCHANGE, CS_DEBRIEF, CS_NO_GC, CS_ELEMENTS = (
    "osha2-cs-host-controlling-entry-info-exchange", "osha2-cs-post-entry-debrief",
    "osha2-cs-no-controlling-contractor", "osha2-cs-permit-program-elements")
CS_EQUIP, CS_NO_ENTRY_LFL, CS_IGNITION, CS_SURVEIL = (
    "osha2-cs-equipment-at-no-cost", "osha2-cs-no-entry-above-10pct-lfl",
    "osha2-cs-no-ignition-sources-hazardous-atmosphere", "osha2-cs-surveillance-surrounding-area")
CS_ATTENDANT, CS_RESCUE_PROC, CS_REVIEW, CS_PERMIT = (
    "osha2-cs-attendant-required", "osha2-cs-rescue-and-emergency-medical-procedures", "osha2-cs-program-review-1-year",
    "osha2-cs-entry-permit-signed-posted")
CS_CANCEL, CS_RETAIN, CS_PERMIT_ITEMS = ("osha2-cs-permit-cancel-or-suspend", "osha2-cs-retain-canceled-permits-1-year",
                                         "osha2-cs-entry-permit-contents")
CS_TRAIN_WHEN, CS_TRAIN_CONTENT, CS_TRAIN_RECORDS = ("osha2-cs-training-when", "osha2-cs-training-content",
                                                     "osha2-cs-training-records")
CS_ENTRANT, CS_ATTENDANT_DUTIES, CS_SUPERVISOR = ("osha2-cs-entrant-duties", "osha2-cs-attendant-duties",
                                                  "osha2-cs-entry-supervisor-duties")
CS_RESCUE, CS_RESCUE_SELECT, CS_INHOUSE = ("osha2-cs-rescue-immediately-available", "osha2-cs-rescue-service-selection",
                                           "osha2-cs-inhouse-rescue-cpr-practice-12-months")
CS_RETRIEVAL, CS_SDS, CS_PARTICIPATION = ("osha2-cs-nonentry-retrieval-5ft", "osha2-cs-sds-to-medical-facility",
                                          "osha2-cs-employee-participation-and-records-access")
# Valley Fever (Labor Code 6709 as described by DOSH)
VF_TRAIN, VF_THRESHOLD, VF_MAY1 = ("osha2-valley-fever-training-endemic-counties",
                                   "osha2-valley-fever-highly-endemic-threshold", "osha2-valley-fever-annual-training-may-1")
VF_CONTROLS, VF_REPORT = "osha2-valley-fever-exposure-controls", "osha2-valley-fever-case-reporting"
# multi-employer worksites (8 CCR 336.10-336.11; Compliance Manual Ch. 14)
ME_FOUR, ME_CONTROLLING, ME_NO_EXPOSURE = ("osha2-multiemployer-four-citable-employers",
                                           "osha2-multiemployer-controlling-employer",
                                           "osha2-multiemployer-cited-without-own-exposure")
ME_PRACTICE, ME_SCHEDULE, ME_DEFENSES = ("osha2-multiemployer-control-by-actual-practice",
                                         "osha2-multiemployer-control-by-scheduling-or-information",
                                         "osha2-multiemployer-exposing-employer-five-defenses")
ME_FACTORS, ME_CONTRACTS, ME_PENALTY, ME_SOLE = ("osha2-multiemployer-responsibility-factors",
                                                 "osha2-multiemployer-contracts-collected",
                                                 "osha2-multiemployer-penalty-counts-all-exposed-workers",
                                                 "osha2-multiemployer-sole-proprietor-not-employee")
# electrical (low voltage), lockout
ELEC_ESO, ELEC_QUALIFIED, ELEC_ENERGIZED = ("osha2-elec-cso-incorporates-eso", "osha2-elec-qualified-persons-only",
                                            "osha2-elec-energized-work-conditions")
ELEC_GLOVES, ELEC_ARC, ELEC_LOCKOUT = ("osha2-elec-insulating-glove-retest-6-months", "osha2-elec-arc-flash-clothing",
                                       "osha2-elec-lockout-deenergized-equipment")
ELEC_LOCK_REMOVAL, ELEC_TAG, ELEC_CONDUCTIVE = ("osha2-elec-lock-removal-by-placer",
                                                "osha2-elec-accident-prevention-tag-contents",
                                                "osha2-elec-conductive-articles-and-tapes")
GFCI, AEGCP, AEGCP_TEST = ("osha2-elec-gfci-construction-receptacles", "osha2-elec-aegcp-written-program",
                           "osha2-elec-aegcp-inspection-test-intervals")
LOTO_SCOPE, LOTO_STOP, LOTO_REPAIR, LOTO_WRITTEN, LOTO_TAG = (
    "osha2-loto-scope", "osha2-loto-stop-and-block-for-servicing", "osha2-loto-lock-out-for-repair",
    "osha2-loto-written-energy-control-procedures", "osha2-loto-tagout-device-50-lb")
LOTO_GROUP, LOTO_SHIFT, LOTO_ANNUAL, LOTO_CONTRACTORS, LOTO_TRAIN = (
    "osha2-loto-group-lockout", "osha2-loto-shift-change", "osha2-loto-annual-inspection-certification",
    "osha2-loto-outside-contractors-follow-onsite-procedures", "osha2-loto-training")
# lifts (GISO Article 24)
AWP_SCOPE, AWP_MANUAL, AWP_PLATE, AWP_RECORDS, AWP_GUARDRAIL, AWP_NO_CLIMB = (
    "osha2-awp-scope-definitions", "osha2-awp-manual-on-unit", "osha2-awp-ansi-data-plate",
    "osha2-awp-inspection-repair-records-3-years", "osha2-awp-guardrail-42-inches",
    "osha2-awp-no-climbing-rails-or-planks")
EWP_EQUIP, EWP_DRIVE, EWP_PREUSE, EWP_WEATHER, EWP_TRAINING, EWP_MAST = (
    "osha2-ewp-platform-equipment", "osha2-ewp-drive-while-elevated", "osha2-ewp-preuse-inspection-and-load",
    "osha2-ewp-weather-and-traffic", "osha2-ewp-training-content", "osha2-ewp-mast-climbing-not-a-hoist")
AERIAL_OPS, AERIAL_TIEOFF, AERIAL_NO_BELT, AERIAL_SETUP, AERIAL_MOVE = (
    "osha2-aerial-authorized-operators-and-controls", "osha2-aerial-tie-off-required",
    "osha2-aerial-no-belting-to-adjacent-structures", "osha2-aerial-setup-brakes-chocks-outriggers",
    "osha2-aerial-move-with-boom-elevated")
# forklifts (GISO 3650, 3657, 3664, 3668)
PIT_TRAIN, PIT_METHOD, PIT_TOPICS, PIT_REFRESH, PIT_EVAL, PIT_CERT = (
    "osha2-pit-training-before-operating", "osha2-pit-training-method-and-trainer", "osha2-pit-training-topics",
    "osha2-pit-refresher-triggers", "osha2-pit-evaluation-every-3-years", "osha2-pit-certification-record")
PIT_AUTH, PIT_PRESHIFT, PIT_RULES, PIT_POST, PIT_PLATFORM, PIT_TELE = (
    "osha2-pit-authorized-trained-drivers-only", "osha2-pit-pre-shift-check", "osha2-pit-operating-rules-numbers",
    "osha2-pit-post-operating-rules", "osha2-pit-elevating-personnel-platform",
    "osha2-pit-telehandler-personnel-one-third-capacity")
# PPE and noise
PPE_USE, PPE_FIT, PPE_OWNED, PPE_HEAD, PPE_EYE = (
    "osha2-ppe-employer-require-use", "osha2-ppe-proper-fit", "osha2-ppe-employee-owned-maintained",
    "osha2-ppe-head-protection-classes", "osha2-ppe-eye-face-protection")
PPE_SHADES, PPE_HAND, PPE_FOOT, PPE_BODY, PPE_NOT_GISO = (
    "osha2-ppe-welding-filter-shades", "osha2-ppe-hand-protection", "osha2-ppe-foot-protection",
    "osha2-ppe-body-protection-clothing", "osha2-ppe-written-hazard-assessment-not-construction")
NOISE_TABLE, NOISE_CONTROLS, NOISE_IMPACT, NOISE_EXEMPT = (
    "osha2-noise-pel-table-n1", "osha2-noise-controls-then-hearing-protection", "osha2-noise-impact-140-db-peak",
    "osha2-noise-construction-exempt-hearing-conservation")
# demolition (CSO 1734-1736)
DEMO_SUPERV, DEMO_SURVEY, DEMO_UTIL, DEMO_ASB, DEMO_CORRECT, DEMO_TOPDOWN = (
    "osha2-demo-qualified-supervision", "osha2-demo-written-engineering-survey",
    "osha2-demo-utilities-hazardous-substances", "osha2-demo-asbestos-survey-materials",
    "osha2-demo-correct-deficiencies-and-inspect", "osha2-demo-top-down-sequence-floor-openings-25pct")
DEMO_CANOPY, DEMO_WALL, DEMO_FREEWALL, DEMO_ACCESS, DEMO_JOISTS = (
    "osha2-demo-entrance-canopy-8ft-150psf", "osha2-demo-wall-openings-42in-glass", "osha2-demo-freestanding-wall-12ft",
    "osha2-demo-access-stairs-elevators", "osha2-demo-no-walking-on-joists-beams")
DEMO_DUST, DEMO_SALVAGE, DEMO_DROP, DEMO_CHUTES, DEMO_RAGS = (
    "osha2-demo-dust-control-and-equipment-on-floors", "osha2-demo-no-salvage-over-3-stories",
    "osha2-demo-debris-drop-zone-barricades", "osha2-demo-chutes", "osha2-demo-oily-rags-flammables-containers")
# traffic control and flaggers (CSO 1598-1599)
TRAFFIC_MUTCD, TRAFFIC_HIVIS, TRAFFIC_NIGHT = ("osha2-traffic-control-ca-mutcd", "osha2-traffic-high-visibility-apparel",
                                               "osha2-traffic-night-retroreflective-1000ft")
FLAG_WHEN, FLAG_APPAREL, FLAG_TRAIN, FLAG_RECORDS = (
    "osha2-flagger-when-required", "osha2-flagger-apparel-and-night-station", "osha2-flagger-training-before-assignment",
    "osha2-flagger-training-records-and-trainer")
# hot work and fire protection
HOTWORK, WELD_EXHAUST, WELD_TOXIC, WELD_NO_GAS, WELD_COATED = (
    "osha2-hotwork-fire-prevention-procedure", "osha2-weld-indoor-local-exhaust-100fpm",
    "osha2-weld-toxic-metals-ventilation-respirators", "osha2-weld-no-welding-gases-for-ventilation",
    "osha2-weld-coated-metals")
WELD_10BC, WELD_SHUTOFF, WELD_STORAGE, WELD_CYL = (
    "osha2-weld-fuel-gas-extinguisher-10bc", "osha2-weld-shut-off-cylinders-warn-hot-metal",
    "osha2-weld-oxygen-fuel-storage-20ft", "osha2-weld-cylinder-handling")
FIRE_PROGRAM, FIRE_2A, FIRE_10B, FIRE_INSPECT = (
    "osha2-fire-protection-program", "osha2-fire-extinguisher-2a-per-3000-sqft",
    "osha2-fire-extinguisher-10b-flammables-50ft", "osha2-fire-extinguisher-monthly-inspection-annual-service")

# Spellings only (not law): recognizes county names; never cited.
_COUNTY_SPELLING_RULE = "usa-regional-centers-by-county"


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _n(x: float) -> str:
    """Number for display: thousands separators, at most 2 decimals, no trailing zeros."""
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _nx(x: float, ref: float) -> str:
    """x for display next to a threshold ref: more decimals when 2 would make it look equal (20.0001 vs 20)."""
    s = _n(x)
    if s == _n(ref) and x != ref:
        s = f"{x:,.6f}".rstrip("0").rstrip(".")
    return s


def _no_bool(v):
    """MCP numbers: pydantic would turn true/false into 1/0 (a 1 kV line, 1 hour of smoke); refuse them instead."""
    if isinstance(v, bool):
        raise ValueError("must be a number, not true/false")
    return v


NoBool = BeforeValidator(_no_bool)


def _ceil2(x: float) -> float:
    """Round up to 2 decimals (a minimum protection factor must never be rounded down)."""
    return math.ceil(round(x * 100, 9)) / 100


def _join(items, last: str = "and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def _words(text) -> str:
    """Lowercase, punctuation to spaces, single spaces: 'Scissor-Lift (Genie)' -> 'scissor lift genie'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", str(text or "").lower())).strip()


# Units a text value may carry, per kind of number, as {unit: factor to the field's own unit}. Not law: plain unit
# conversions (1 ft = 0.3048 m exactly). A unit not listed for the field is an error, never silently ignored, so
# '96 in' is never read as 96 ft and '5 ppm' is never read as 5% LFL.
_FT_PER_M = 1 / 0.3048
_UNITS = {
    "plain": {"": 1.0},
    "hours": {"": 1.0, "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0, "min": 1 / 60, "mins": 1 / 60,
              "minute": 1 / 60, "minutes": 1 / 60},
    "feet": {"": 1.0, "ft": 1.0, "feet": 1.0, "foot": 1.0, "'": 1.0, "in": 1 / 12, "inch": 1 / 12, "inches": 1 / 12,
             '"': 1 / 12, "m": _FT_PER_M, "meter": _FT_PER_M, "meters": _FT_PER_M, "metre": _FT_PER_M,
             "metres": _FT_PER_M},
    "percent": {"": 1.0, "%": 1.0, "percent": 1.0, "pct": 1.0},
    "ug_m3": {"": 1.0, "ug/m3": 1.0, "ugm3": 1.0, "ug": 1.0, "micrograms per cubic meter": 1.0,
              "mg/m3": 1000.0, "milligrams per cubic meter": 1000.0},
    "sq_ft": {"": 1.0, "sq ft": 1.0, "sqft": 1.0, "sf": 1.0, "ft2": 1.0, "square feet": 1.0, "square foot": 1.0,
              "sq feet": 1.0},
    "per_100k": {"": 1.0, "per 100k": 1.0, "per 100000": 1.0, "/100k": 1.0, "/100000": 1.0,
                 "cases per 100k": 1.0, "cases per 100000": 1.0, "per 100000 people": 1.0, "per 100k people": 1.0},
}
_UNIT_HINT = {"hours": "hours (e.g. '6', '6 h' or '90 min')", "feet": "feet (e.g. '8', '8 ft', '96 in' or '2.5 m')",
              "percent": "a percent reading (e.g. '19.5' or '19.5%'); ppm is not a percent",
              "ug_m3": "micrograms per cubic meter (e.g. '80' or '80 ug/m3')", "sq_ft": "square feet (e.g. '7500')",
              "per_100k": "cases per 100,000 people (e.g. '24.3')", "plain": "a plain number"}
# Words a meter reading may carry after the % sign ('19.5 % O2', '4 % LEL', '25% of MEC').
_PCT_SUFFIX = re.compile(r"^(?:%|percent|pct)?\s*(?:of\s+)?(?:the\s+)?(?:lel|lfl|mec|o2|oxygen|by volume|vol|"
                         r"by vol)?$")


def _unit_key(tail: str) -> str:
    t = tail.lower().replace("µ", "u").replace("μ", "u").replace("³", "3").replace("²", "2").replace("^", "")
    t = re.sub(r"\s+", " ", t).strip().rstrip(".").strip()
    return t.replace("sq. ft", "sq ft").replace("sq.ft", "sq ft")


def _num(name: str, value, lo: Optional[float] = None, hi: Optional[float] = None,
         kind: str = "plain") -> Optional[float]:
    """A number, or text with a unit that suits the field ('6 h', '90 min', '19.5%', '80 ug/m3', '96 in'), converted
    to the field's unit; None and blank stay None. Any other unit is an error."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.match(r"^\s*(-?\d*\.?\d+)\s*(.*?)\s*$", str(value).replace(",", ""))
        if not m:
            raise ValueError(f"{name} must be a number in {_UNIT_HINT.get(kind, 'its unit')}, got {value!r}")
        unit, units = _unit_key(m.group(2)), _UNITS[kind]
        if kind == "percent" and _PCT_SUFFIX.match(unit):
            unit = ""
        if unit not in units:
            raise ValueError(f"{name}: unit {m.group(2)!r} not understood; give {_UNIT_HINT.get(kind, 'a number')}, "
                             f"got {value!r}")
        v = float(m.group(1)) * units[unit]
        if units[unit] != 1.0:
            v = round(v, 9)                             # 60 in is exactly 5 ft, not 4.999999999999999
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if lo is not None and v < lo:
        raise ValueError(f"{name} cannot be below {lo:g}, got {value!r}")
    if hi is not None and v > hi:
        raise ValueError(f"{name} cannot be above {hi:g}, got {value!r}")
    return v


def _bool(name: str, value) -> Optional[bool]:
    """True/False, or 'yes'/'no'/'y'/'n'/'true'/'false'/'1'/'0'; None stays None."""
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


def _date(value, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2026-11-25, got {value!r}") from None


def _cit(ids) -> str:
    return "; ".join(dict.fromkeys(core.rule(i).citation_text or i for i in ids))


class Requirement(BaseModel):
    item: str
    detail: str
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


def _requirement(item: str, detail: str, ids) -> Requirement:
    ids = list(dict.fromkeys(ids))
    return Requirement(item=item, detail=detail, rule_ids=ids, citation=_cit(ids),
                       verified=bool(ids) and all(core.rule(i).verified for i in ids))


class _Acc:
    """Collects, for one call, the rules relied on, the requirements, the math and the notes."""

    def __init__(self):
        self.ids: list[str] = []
        self.reqs: list[Requirement] = []
        self.math: list[str] = []
        self.notes: list[str] = []

    def use(self, *ids: str) -> list[str]:
        for i in ids:
            core.rule(i)                                    # unknown id -> KeyError, never a silent miss
            if i not in self.ids:
                self.ids.append(i)
        return list(ids)

    def req(self, item: str, detail: str, *ids: str) -> Requirement:
        self.use(*ids)
        r = _requirement(item, detail, ids)
        self.reqs.append(r)
        return r

    def rule(self, rid: str, item: Optional[str] = None, extra: str = "") -> Requirement:
        """A requirement worded exactly as the rulebook's plain-English text for that rule."""
        r = core.rule(rid)
        return self.req(item or r.title or rid, (r.plain_english or "") + (f" {extra}" if extra else ""), rid)

    def finish(self, res):
        if "requirements" in type(res).model_fields:
            res.requirements = self.reqs + [r for r in res.requirements if r not in self.reqs]
        res.notes = self.notes + [n for n in res.notes if n not in self.notes]
        res.cite(*self.ids)
        res.unverified_rule_ids = [i for i in self.ids if not core.rule(i).verified]
        for i in res.unverified_rule_ids:
            r = core.rule(i)
            res.notes.append(f"NOT VERIFIED ({r.status}): {r.title} [{r.citation_text}]. "
                             + (r.notes or "The primary text could not be read.")
                             + " Confirm with the official text or a safety professional before relying on it.")
        res.math = self.math + [m for m in res.math if m not in self.math]
        return res


class _Base(core.Result):
    summary: str = ""
    next_step: str = ""
    math: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ================================================================ wildfire smoke (8 CCR 5141.1)
WorkSetting = Literal["outdoors", "enclosed_unfiltered", "enclosed_building_filtered", "enclosed_vehicle_filtered"]
SmokeLevel = Literal["below_151", "151_to_500", "over_500"]


# Free-text settings that defeat the filtered-enclosure exemptions (5141.1(a)(2)(A)-(B) need filtered air AND
# windows, doors and other openings kept closed): openings left open, or no filtration.
_OPENINGS_OPEN = re.compile(r"\b(propped|ajar|wide open|left open|(doors?|windows?|bays?|roll ?ups?|openings?)\s+"
                            r"(are |is |kept |left |stay |stays )?(open|down)|open (doors?|windows?|bays?)|"
                            r"windows? (rolled )?down)\b")
_NO_FILTER = re.compile(r"\b(unfiltered|no (hvac|filters?|filtration)|not filtered|without (a )?(filters?|"
                        r"filtration|hvac)|filters? (removed|missing|off))\b")
# A portable unit is not the building's "mechanical ventilation system" (DOSH's wildfire FAQ treats portable air
# cleaners as something added when the building system does not filter the air).
_PORTABLE_CLEANER = re.compile(r"\b(purifiers?|air cleaners?|air scrubbers?|negative air|portable)\b")


def _setting(value) -> tuple[str, Optional[str]]:
    """(setting, note): the note explains a free-text setting read as unfiltered on purpose."""
    s = _words(value or "outdoors").replace(" ", "_")
    if s in ("outdoors", "enclosed_unfiltered", "enclosed_building_filtered", "enclosed_vehicle_filtered"):
        return s, None
    w = _words(value)
    filtered = bool(re.search(r"\b(filter\w*|hvac|hepa|merv\s*\d*|cabin air)\b", w))
    vehicle = bool(re.search(r"\b(vehicles?|cabs?|trucks?|cars?|vans?|equipment cabs?)\b", w))
    building = bool(re.search(r"\b(buildings?|indoors?|inside|interior|offices?|trailers?|structures?|rooms?)\b",
                              w))
    if (vehicle or building) and (filtered or "enclosed" in w):
        if _OPENINGS_OPEN.search(w):
            return "enclosed_unfiltered", ("Read as unfiltered: the filtered-enclosure exemptions apply only while "
                                           "windows, doors and other openings are kept closed (except to enter or "
                                           "exit).")
        if _NO_FILTER.search(w):
            return "enclosed_unfiltered", None
        if building and not vehicle and _PORTABLE_CLEANER.search(w) and not re.search(r"\b(hvac|ventilation)\b", w):
            return "enclosed_unfiltered", ("Read as unfiltered: the building exemption needs the air filtered by the "
                                           "building's mechanical ventilation system; a portable air cleaner alone "
                                           "does not meet it. If the HVAC filters the air with openings closed, use "
                                           "enclosed_building_filtered.")
        if filtered and vehicle:
            return "enclosed_vehicle_filtered", None
        if filtered and building:
            return "enclosed_building_filtered", None
    if re.search(r"\b(outdoor|outside|open air|exterior|roof|yard|site)", w):
        return "outdoors", None
    if vehicle or building or "unfiltered" in w or "enclosed" in w:
        return "enclosed_unfiltered", None
    raise ValueError("work_setting must be outdoors, enclosed_unfiltered (a building or vehicle without filtered air "
                     "or with openings left open), enclosed_building_filtered or enclosed_vehicle_filtered; "
                     f"got {value!r}")


_PM25_SANITY_MAX = 1_000_000          # 1 g/m3: a physical sanity bound for a reading, not a legal value


def _parse_aqi(value) -> tuple[Optional[int], bool]:
    """'175' / 175 / 'AQI 175' -> (175, False); '500+' / '>500' / 'beyond the AQI' -> (501, True)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, False
    if isinstance(value, bool):
        raise ValueError(f"aqi_pm25 must be a number like 175, got {value!r}")
    if isinstance(value, (int, float)):
        v, plus = float(value), False
    else:
        s = str(value).strip().lower()
        if "beyond" in s:
            return int(P(SMOKE_MAND, "aqi_over")) + 1, True
        m = re.fullmatch(r"(?:current\s*)?(?:pm\s*2\.?5\s*)?(?:aqi)?\s*[:=]?\s*(>|over|above)?\s*(\d+(?:\.\d+)?)\s*(\+)?"
                         r"(?:\s*\(?[a-z ]*\)?)?", s)
        if not m:
            raise ValueError(f"aqi_pm25 must be a number like 175 (the current PM2.5 AQI from AirNow, CARB or your "
                             f"air district), got {value!r}")
        v, plus = float(m.group(2)), bool(m.group(1) or m.group(3))
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"aqi_pm25 must be 0 or more, got {value!r}")
    if v != int(v):
        raise ValueError(f"aqi_pm25 is a whole number on the AQI scale (e.g. 151), got {value!r}")
    return int(v) + (1 if plus else 0), plus


def _pm25_band(pm: float) -> tuple[Optional[dict], bool]:
    """Appendix A row for a PM2.5 concentration (None above the table). A value between two rows (e.g. 55.45) goes
    in the higher row, so the exposure is never underestimated. Returns (row, in_gap)."""
    rows = sorted(P(SMOKE_CONV, "conversion_table"), key=lambda r: r["pm25_ug_m3_min"])
    for r in rows:
        if pm <= r["pm25_ug_m3_max"]:
            return r, pm < r["pm25_ug_m3_min"]
    return None, False


def _category(aqi_lo: int) -> Optional[str]:
    for c in P(SMOKE_CATS, "categories"):
        if c["aqi_min"] <= aqi_lo <= c["aqi_max"]:
            return c["category"]
    return None


class WildfireSmokeResult(_Base):
    aqi_pm25: Optional[int] = Field(None, description="The reported current PM2.5 AQI you gave (501 means 'over 500')")
    pm25_ug_m3: Optional[float] = Field(None, description="Your on-site PM2.5 reading (ug/m3), if given")
    aqi_basis: Literal["reported_aqi", "onsite_pm25_measurement"]
    aqi_range: str = Field(description="AQI used for the decision, e.g. '175', '151-200' or 'over 500'")
    aqi_category: str
    level: SmokeLevel
    smoke_anticipated: bool
    covered: bool = Field(description="True when 8 CCR 5141.1 applies to these workers for this shift")
    exemption: Optional[str] = None
    hours_exposed: Optional[float] = None
    work_setting: str
    emergency_operation: bool = False
    respirators: Literal["not_required", "voluntary", "mandatory"]
    min_assigned_protection_factor: Optional[float] = Field(
        None, description="AQI over 500 only: smallest 8 CCR 5144 APF that keeps PM2.5 inside the respirator at AQI "
                          "under 151")
    apf_is_lower_bound: bool = Field(False, description="True when only 'AQI over 500' was known: the PM2.5 is "
                                                        "above EPA's AQI-500 concentration but otherwise unknown, so "
                                                        "the APF needed may be higher than shown; measure it")
    respirator_options: list[str] = Field(default_factory=list, description=(
        "AQI over 500: respirator types from 8 CCR 5144 Table 1 whose APF meets the minimum, each with the highest "
        "outside PM2.5 it covers (APF x 55.4 ug/m3)"))
    requirements: list[Requirement] = Field(default_factory=list)


def _respirator_options(min_apf: float) -> list[str]:
    """5144 Table 1 rows whose APF meets min_apf, air-purifying and powered air-purifying first (the practical ones for
    smoke); supplied-air and SCBA rows only when no air-purifying respirator is enough."""
    inside = P(SMOKE_MAND, "max_inside_pm25_ug_m3")
    rows = sorted(P(RESP_APF, "apf_table"), key=lambda r: (r["apf"], r["respirator"]))
    ok = [r for r in rows if r["apf"] >= min_apf]
    apr = [r for r in ok if r["class"] in ("air_purifying", "papr")]
    return [f"{r['respirator']}: APF {_n(r['apf'])}, covers PM2.5 up to {_n(r['apf'] * inside)} ug/m3"
            + (f" (APF {_n(r['apf_with_manufacturer_test_evidence'])} only with the manufacturer's test evidence)"
               if r.get("apf_with_manufacturer_test_evidence") else "")
            for r in (apr or ok)]


def wildfire_smoke_requirements(aqi_pm25=None, hours_exposed=None, work_setting: str = "outdoors",
                                pm25_ug_m3=None, emergency_operation: bool = False,
                                wildland_firefighting: bool = False,
                                smoke_anticipated: bool = True) -> WildfireSmokeResult:
    """What 8 CCR 5141.1 requires for this shift, from the current PM2.5 AQI or an on-site PM2.5 reading."""
    acc = _Acc()
    aqi, aqi_plus = _parse_aqi(aqi_pm25)
    pm = _num("pm25_ug_m3", pm25_ug_m3, lo=0, kind="ug_m3")
    if pm is not None and pm > _PM25_SANITY_MAX:
        raise ValueError(f"pm25_ug_m3 {pm25_ug_m3!r} is not a realistic PM2.5 reading (over {_n(_PM25_SANITY_MAX)} "
                         "ug/m3, i.e. 1 g/m3); check the monitor and the units (ug/m3, not mg/m3)")
    hours = _num("hours_exposed", hours_exposed, lo=0, hi=24, kind="hours")
    setting, setting_note = _setting(work_setting)
    if setting_note:
        acc.notes.append(setting_note)
    emergency = bool(_bool("emergency_operation", emergency_operation))
    firefighting = bool(_bool("wildland_firefighting", wildland_firefighting))
    anticipated = _bool("smoke_anticipated", smoke_anticipated)
    anticipated = True if anticipated is None else anticipated
    if aqi is None and pm is None:
        raise ValueError("Give aqi_pm25 (the current PM2.5 AQI from AirNow, CARB or your air district) or "
                         "pm25_ug_m3 (an on-site reading taken per Appendix A).")

    trigger = P(SMOKE_SCOPE, "aqi_trigger_min")
    vol_max = P(SMOKE_VOL, "aqi_max")
    mand_over = P(SMOKE_MAND, "aqi_over")
    max_inside = P(SMOKE_MAND, "max_inside_pm25_ug_m3")
    acc.use(SMOKE_SCOPE, SMOKE_CATS)

    # ---- which AQI decides: an on-site Appendix A measurement governs over the regional AQI (5141.1(a)(2)(C), (c))
    if pm is not None:
        acc.use(SMOKE_CONV)
        basis = "onsite_pm25_measurement"
        row, gap = _pm25_band(pm)
        if row is None:
            top = P(SMOKE_CONV, "aqi_500_upper_ug_m3")
            lo, hi, rng = mand_over + 1, None, f"over {mand_over}"
            acc.math.append(f"PM2.5 {_n(pm)} ug/m3 > {_n(top)} ug/m3 (top of the Appendix A table, AQI {vol_max}) "
                            f"-> AQI over {mand_over}")
        else:
            lo, hi = row["aqi_min"], row["aqi_max"]
            rng = f"{lo}-{hi}"
            acc.math.append(f"PM2.5 {_n(pm)} ug/m3 is in the Appendix A row {_n(row['pm25_ug_m3_min'])}-"
                            f"{_n(row['pm25_ug_m3_max'])} ug/m3 = AQI {lo}-{hi}"
                            + (" (the reading falls between two rows; the higher row is used so the exposure is not "
                               "underestimated)" if gap else ""))
        if aqi is not None:
            acc.notes.append(f"You gave both a reported AQI ({'over ' + str(aqi - 1) if aqi_plus else aqi}) and an "
                             f"on-site reading; the on-site reading (AQI {rng}) is used, because 5141.1 lets the "
                             "employer determine exposure by measuring PM2.5 at the worksite per Appendix A.")
        acc.notes.append("On-site readings count only if taken per Appendix A: " + core.rule(SMOKE_CONV).plain_english)
        epa500 = P(SMOKE_EPA, "aqi_500_pm25_ug_m3")
        if row is not None and row["aqi_max"] == vol_max and pm > epa500:
            acc.use(SMOKE_EPA)
            acc.notes.append(f"At {_n(pm)} ug/m3 AirNow (EPA's current AQI, where AQI 500 is {_n(epa500)} ug/m3 since "
                             f"2024) would show an AQI over {vol_max}, but 5141.1 Appendix A, which must be used to "
                             f"convert on-site readings, puts it at AQI {rng}: respirators stay voluntary under the "
                             f"rule. If you rely on a reported AQI over {vol_max} instead, they are mandatory.")
    else:
        basis = "reported_aqi"
        lo, hi = aqi, aqi
        rng = f"over {aqi - 1}" if aqi_plus else str(aqi)
    level: str = "over_500" if lo > mand_over else ("151_to_500" if lo >= trigger else "below_151")
    category = _category(lo) or f"beyond {vol_max}, the top of the 5141.1 AQI table"
    acc.math.append(f"AQI {rng} -> {category}; the rule starts at AQI {trigger}, respirators become mandatory above "
                    f"{mand_over}")

    # ---- coverage and exemptions (5141.1(a))
    exemptions = P(SMOKE_EXEMPT, "exemptions")

    def ex(word: str) -> str:
        return next(e for e in exemptions if word in e)

    max_h = P(SMOKE_EXEMPT, "exempt_max_exposure_hours_per_shift")
    exemption, covered = None, False
    if not anticipated:
        pass
    elif level == "below_151":
        if basis == "onsite_pm25_measurement":
            exemption = ex("measures")
            acc.use(SMOKE_EXEMPT)
    elif firefighting:
        exemption = ex("firefight")
    elif setting == "enclosed_building_filtered":
        exemption = ex("buildings")
    elif setting == "enclosed_vehicle_filtered":
        exemption = ex("vehicles")
    elif hours is not None and hours <= max_h:
        exemption = ex("one hour")
        acc.math.append(f"{_nx(hours, max_h)} h at AQI {trigger}+ this shift <= {_n(max_h)} h -> exempt (the hours "
                        "are a total for the shift)")
    else:
        covered = True
        if hours is not None:
            acc.use(SMOKE_EXEMPT)
            acc.math.append(f"{_nx(hours, max_h)} h at AQI {trigger}+ this shift > {_n(max_h)} h -> no hours "
                            "exemption")
    if exemption and level != "below_151":
        acc.use(SMOKE_EXEMPT)

    res = WildfireSmokeResult(aqi_pm25=aqi, pm25_ug_m3=pm, aqi_basis=basis, aqi_range=rng, aqi_category=category,
                              level=level, smoke_anticipated=anticipated, covered=covered, exemption=exemption,
                              hours_exposed=hours, work_setting=setting, emergency_operation=emergency,
                              respirators="not_required")
    check = core.rule(SMOKE_CHECK)
    when, sources = check.param("when"), check.param("sources")
    check_text = (f"Determine PM2.5 exposure at the {when[0]} and {when[1]}, from {_join(sources, 'or')}. Log the "
                  "reading, source and time for each shift.")

    if not anticipated:
        res.summary = ("8 CCR 5141.1 applies only where workers may reasonably be anticipated to be exposed to "
                       f"wildfire smoke; with no wildfire smoke expected it does not apply, whatever the AQI ({rng}).")
        res.next_step = ("No wildfire-smoke duties. If the haze could be wildfire smoke, treat it as smoke and run "
                         "this check again with smoke_anticipated=true.")
        return acc.finish(res)
    if level == "below_151":
        acc.req("Keep checking the AQI", check_text, SMOKE_CHECK)
        res.summary = (f"PM2.5 AQI {rng} ({category}) is below {trigger}: 8 CCR 5141.1 does not apply right now"
                       + (" (on-site measurement below the AQI-151 level, 5141.1(a)(2)(C))." if exemption else "."))
        res.next_step = (f"No wildfire-smoke duties right now. Recheck the PM2.5 AQI at the start of the next shift "
                         f"and during this one; the rule applies as soon as it reaches {trigger}.")
        return acc.finish(res)
    if exemption:
        acc.req("Exemption that applies", exemption, SMOKE_EXEMPT)
        res.summary = f"PM2.5 AQI {rng} ({category}), but these workers are exempt from 8 CCR 5141.1: {exemption}."
        if firefighting:
            res.next_step = "No 5141.1 duties for wildland firefighters; follow the fire agency's own procedures."
        elif setting.endswith("filtered"):
            res.next_step = ("Keep doors, windows and other openings closed and the filtration running. Anyone who "
                             "works outside the enclosure is covered: run this again for them with their hours.")
        else:
            res.next_step = (f"Track each worker's time at AQI {trigger}+: once anyone passes {_n(max_h)} hour in "
                             "total this shift, the full rule applies (respirators, controls, training). Recheck "
                             "the AQI during the shift.")
        acc.req("Keep checking the AQI", check_text, SMOKE_CHECK)
        return acc.finish(res)

    # ---- covered
    acc.req("Check the AQI every shift", check_text, SMOKE_CHECK)
    comm = core.rule(SMOKE_COMM)
    acc.req("Tell workers the AQI and the protections", comm.plain_english, SMOKE_COMM)
    train = core.rule(SMOKE_TRAIN)
    acc.req("Train workers (at least Appendix B)",
            f"{train.plain_english} Topics: {_join(train.param('appendix_b_topics'))}.", SMOKE_TRAIN)
    min_resp = P(SMOKE_VOL, "minimum_respirator")
    vol = core.rule(SMOKE_VOL)
    vol_text = (f"Provide enough respirators ({min_resp}) to all employees for voluntary use and encourage their use. "
                + ("No fit test is required for voluntary use of filtering facepieces. "
                   if not vol.param("fit_test_required_for_voluntary_filtering_facepiece") else "")
                + ("No medical evaluation is required for voluntary use of filtering facepieces. "
                   if not vol.param("medical_evaluation_required_for_voluntary_filtering_facepiece") else "")
                + f"Not acceptable: {_join(vol.param('not_acceptable'), 'or')}.")
    if emergency:
        em = core.rule(SMOKE_EMERG)
        res.respirators = "voluntary"
        acc.req("Emergency operations: voluntary respirators at any AQI of " + str(trigger) + "+",
                f"{vol_text} For emergency operations this applies at every AQI of {trigger} or more; not required: "
                f"{_join(em.param('not_required'))}.", SMOKE_EMERG, SMOKE_VOL)
        res.summary = (f"PM2.5 AQI {rng}: emergency operation under 8 CCR 5141.1(f)(4). Respirators for "
                       "voluntary use are required; engineering, administrative and mandatory-respirator rules are "
                       "not.")
        res.next_step = (f"Hand every worker a respirator ({min_resp}) for voluntary use, encourage its use, and "
                         "keep telling the crew the current AQI.")
        if level == "over_500":
            acc.notes.append(f"Even above AQI {mand_over}, emergency operations are exempt from the mandatory "
                             "respirator rule (5141.1(f)(4)); 'emergency operations' do not include post-fire debris "
                             "removal or rebuilding.")
    else:
        ctl = core.rule(SMOKE_CONTROLS)
        acc.req("Reduce exposure: engineering, then administrative controls",
                f"Whenever feasible, reduce exposure to below AQI {ctl.param('target_aqi_below')} with engineering "
                f"controls, e.g. {_join(ctl.param('engineering_controls_examples'))}. If that is not feasible or "
                f"not enough, use administrative controls if practicable: "
                f"{_join(ctl.param('administrative_controls_examples'), 'or')}.", SMOKE_CONTROLS)
        if level == "over_500":
            res.respirators = "mandatory"
            acc.use(RESP_APF)
            ff_apf = P(RESP_APF, "filtering_facepiece_apf")
            if pm is not None:
                apf = pm / max_inside
                res.min_assigned_protection_factor = _ceil2(apf)
                acc.math.append(f"Minimum APF = {_n(pm)} ug/m3 outside / {_n(max_inside)} ug/m3 allowed inside = "
                                f"{apf:.4f} -> at least {_n(res.min_assigned_protection_factor)}")
            else:
                # A reported AQI comes from EPA's current method (5141.1(b)), where AQI 500 is 325.4 ug/m3.
                epa500 = P(SMOKE_EPA, "aqi_500_pm25_ug_m3")
                res.min_assigned_protection_factor, res.apf_is_lower_bound = _ceil2(epa500 / max_inside), True
                acc.use(SMOKE_EPA)
                acc.math.append(f"A reported AQI over {mand_over} means PM2.5 above {_n(epa500)} ug/m3 on EPA's "
                                f"current AQI scale (40 CFR 58 App. G): minimum APF > {_n(epa500)} / {_n(max_inside)} = "
                                f"{epa500 / max_inside:.4f}. How far above is unknown: measure PM2.5 to size the "
                                "respirator.")
            mn = res.min_assigned_protection_factor
            res.respirator_options = _respirator_options(mn)
            ff_ok = ff_apf >= mn
            acc.math.append(f"N95 or other filtering facepiece = half mask, APF {_n(ff_apf)} (5144 Table 1) "
                            + (f">= {_n(mn)} -> enough" if ff_ok else f"< {_n(mn)} -> not enough")
                            + (" for the lower bound only" if res.apf_is_lower_bound else ""))
            acc.req(f"AQI over {mand_over}: respirators are mandatory",
                    f"Respirator use is required under {P(SMOKE_MAND, 'program')}. Use respirators whose assigned "
                    f"protection factor in 8 CCR 5144 Table 1 is at least {_n(mn)}"
                    + (" (a lower bound: the actual PM2.5 is unknown above that)" if res.apf_is_lower_bound else "")
                    + f", so PM2.5 inside stays at or below {_n(max_inside)} ug/m3 (AQI under {trigger}). "
                    + ("Types that qualify: " + "; ".join(res.respirator_options) + "."
                       if res.respirator_options else
                       f"No respirator in 5144 Table 1 has an APF of {_n(mn)}."), SMOKE_MAND, RESP_APF)
            if res.apf_is_lower_bound:
                res.summary = (f"PM2.5 AQI {rng}: 8 CCR 5141.1 applies and respirator use is MANDATORY under a full "
                               f"8 CCR 5144 program. The AQI alone does not give the PM2.5, so the minimum APF is "
                               f"only known to be above {_n(mn)}: measure PM2.5 on site.")
                res.next_step = (f"Before anyone works in this smoke: measure PM2.5 at the worksite (per Appendix A) "
                                 f"and pick respirators with APF of at least PM2.5 / {_n(max_inside)}; an N95 or other "
                                 f"half mask (APF {_n(ff_apf)}) covers only up to {_n(ff_apf * max_inside)} ug/m3. "
                                 "Only workers in your 8 CCR 5144 program (written program, medical evaluation, fit test) "
                                 "may work. Until you have a reading, keep the work in filtered enclosures or "
                                 "postpone it.")
            elif not res.respirator_options:
                res.summary = (f"PM2.5 AQI {rng}: 8 CCR 5141.1 applies and respirator use is MANDATORY, but no "
                               f"respirator in 8 CCR 5144 Table 1 reaches the minimum APF of {_n(mn)}.")
                res.next_step = (f"Do not work in this smoke: no listed respirator keeps PM2.5 inside at or below "
                                 f"{_n(max_inside)} ug/m3. Move the work into filtered enclosures or postpone it, and "
                                 "check the reading (and the monitor) again.")
            else:
                res.summary = (f"PM2.5 AQI {rng}: 8 CCR 5141.1 applies and respirator use is MANDATORY under a full "
                               f"8 CCR 5144 program (minimum APF {_n(mn)}; an N95 or other half mask, APF "
                               f"{_n(ff_apf)}, is {'enough' if ff_ok else 'NOT enough'}).")
                names = [o.split(", covers")[0] for o in res.respirator_options]       # 'type: APF n'
                picks = list(dict.fromkeys([names[0]] + [n for n in names if n.startswith("air-purifying")][:1]))
                res.next_step = ("Before anyone works in this smoke: only workers in your 8 CCR 5144 respirator "
                                 "program (written program, medical evaluation, fit test) may work, each wearing a "
                                 f"respirator with an APF of at least {_n(mn)} (for example {'; or '.join(picks)}). "
                                 "Move the work into filtered enclosures or postpone it for everyone else.")
        else:
            res.respirators = "voluntary"
            acc.req(f"AQI {trigger}-{vol_max}: respirators for voluntary use", vol_text, SMOKE_VOL)
            res.summary = (f"PM2.5 AQI {rng} ({category}): 8 CCR 5141.1 applies"
                           + (f" ({_n(hours)} h of exposure this shift)" if hours is not None else "")
                           + f". Provide respirators ({min_resp}) for voluntary use and encourage their use; "
                             "reduce exposure with filtered enclosures or schedule changes where feasible.")
            res.next_step = (f"Before the shift: hand every worker a respirator ({min_resp}), tell them the AQI is "
                             f"{rng} and encourage them to wear it; move work into filtered enclosures or reschedule "
                             "heavy tasks where feasible, and recheck the AQI during the shift.")
    acc.rule(SMOKE_MED, "Medical treatment, no reprisal")
    if hours is None:
        acc.use(SMOKE_EXEMPT)
        acc.notes.append(f"A worker whose total time at AQI {trigger}+ is {_n(max_h)} hour or less this shift is "
                         "exempt (5141.1(a)(2)(D)); give hours_exposed to check.")
    return acc.finish(res)


# ================================================================ overhead lines (8 CCR 2946-2949)
# (key, label, aliases, table basis, 2947 sign). Table basis: "table_2_rule_text" = storage, boom-type lifting or
# hoisting equipment, and any other boom-type equipment (2946(b)(2) last paragraph: "Operation of boom-type equipment
# shall conform to the minimum clearances set forth in Table 2"); "table_2_conservative" = a powered machine with no
# boom that raises a mast, leads, bed or platform toward the lines: the rule text puts it on Table 1 (machinery), and
# this tool applies the larger Table 2 distance; "table_1_rule_text" = people, tools, ladders, scaffolds, materials
# and structures. Sign: "required" = named in 2947; "likely_required" = 'similar apparatus'; "not_listed".
_EQUIPMENT = (
    ("storage", "Stored tools, equipment or materials", ("storage", "stored", "store", "storing", "laydown",
                                                          "stockpile", "staging"), "table_2_rule_text", "not_listed"),
    ("concrete_pump", "Concrete pump", ("concrete pump", "pump truck", "boom pump", "placing boom"),
     "table_2_rule_text", "likely_required"),
    ("boom_truck", "Boom truck", ("boom truck", "knuckle boom", "picker truck", "articulating crane", "service crane",
                                  "digger derrick"),
     "table_2_rule_text", "likely_required"),
    ("crane", "Crane or derrick", ("crane", "derrick", "carry deck"), "table_2_rule_text", "required"),
    ("hoist", "Hoisting equipment", ("hoist", "gin pole", "winch"), "table_2_rule_text", "likely_required"),
    ("aerial_lift", "Boom lift, bucket truck or other aerial device",
     ("boom lift", "aerial lift", "aerial device", "bucket truck", "cherry picker", "man lift", "manlift",
      "articulating boom", "telescopic boom", "mewp"), "table_2_rule_text", "not_listed"),
    ("excavator", "Excavator, backhoe or power shovel", ("excavator", "backhoe", "power shovel", "trackhoe"),
     "table_2_rule_text", "likely_required"),
    ("telehandler", "Telehandler (rough-terrain forklift with a boom)",
     ("telehandler", "lull", "skytrak", "rough terrain forklift", "reach forklift"), "table_2_rule_text",
     "not_listed"),
    ("drill_rig", "Drill rig", ("drill rig", "drilling rig", "auger", "boring rig", "well drill"),
     "table_2_conservative", "required"),
    ("pile_driver", "Pile driver", ("pile driver", "pile driving", "pile rig"), "table_2_conservative", "required"),
    ("forklift", "Forklift", ("forklift", "fork lift"), "table_2_conservative", "not_listed"),
    ("scissor_lift", "Scissor lift or other elevating work platform",
     ("scissor", "elevating work platform", "vertical lift", "mast climbing"), "table_2_conservative", "not_listed"),
    ("vehicle", "Dump truck, loader or other vehicle that raises a bed, bucket or load",
     ("dump truck", "end dump", "dump trailer", "mixer truck", "concrete truck", "water truck", "loader",
      "skid steer", "skidsteer", "bobcat", "track loader", "grader", "truck"), "table_2_conservative", "not_listed"),
    ("ladder", "Ladder", ("ladder",), "table_1_rule_text", "not_listed"),
    ("scaffold", "Scaffold", ("scaffold",), "table_1_rule_text", "not_listed"),
    ("material", "Materials being handled (rebar, pipe, conduit, siding, gutters, panels, lumber)",
     ("material", "rebar", "pipe", "conduit", "siding", "gutter", "panel", "lumber", "sheet metal", "flashing",
      "antenna"), "table_1_rule_text", "not_listed"),
    ("structure", "Structure being built or moved (framing, house moving)",
     ("structure", "framing", "house moving", "building move", "truss"), "table_1_rule_text", "not_listed"),
    ("worker", "Worker on foot, hand tools", ("worker", "person", "people", "employee", "laborer", "hand tool", "tool",
                                              "painter", "roofer", "framer", "body"), "table_1_rule_text", "not_listed"),
)
_MACHINE_CLASSES = {"table_2_rule_text", "table_2_conservative"}


def _equipment(value) -> tuple:
    w = _words(value)
    if not w:
        raise ValueError("equipment is required, e.g. 'crane', 'boom lift', 'ladder', 'worker' or 'stored material'")
    for e in _EQUIPMENT:
        if w == e[0].replace("_", " ") or any(re.search(rf"\b{re.escape(a)}", w) for a in e[2]):
            return e
    raise ValueError(f"equipment {value!r} not recognized; use one of: "
                     + ", ".join(e[0] for e in _EQUIPMENT))


def _volts(value) -> Optional[float]:
    """Nominal phase-to-phase voltage in volts. A bare number is kilovolts; a string may carry 'V' or 'kV'."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"voltage_kv must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        kv = float(value)
        if not math.isfinite(kv) or kv <= 0:
            raise ValueError(f"voltage_kv must be more than 0, got {value!r}")
        return kv * 1000
    s = str(value).strip().lower().replace(",", "")
    if s in ("", "unknown", "unk", "?", "not sure", "dont know", "don't know", "n/a", "na", "tbd"):
        return None
    # Utilities often write phase-to-phase/phase-to-ground, e.g. '12.47/7.2 kV' or '12.47Y/7.2 kV': the tables use
    # the phase-to-phase (larger) figure.
    pair = re.fullmatch(r"(\d*\.?\d+)\s*(kv|v)?\s*(?:grdy|y)?\s*/\s*(\d*\.?\d+)\s*(kv|kilovolts?|v|volts?)?\b"
                        r"([^0-9]*)", s)
    if pair:
        unit = pair.group(2) or pair.group(4) or ""
        x = max(float(pair.group(1)), float(pair.group(3)))
        if x <= 0:
            raise ValueError(f"voltage must be more than 0, got {value!r}")
        return x if unit.startswith("v") else x * 1000
    m = re.fullmatch(r"(\d*\.?\d+)\s*(kv|kilovolts?|v|volts?)?\b([^0-9]*)", s)
    if not m:
        raise ValueError(f"voltage_kv must be a number of kilovolts (e.g. 12.47) or text like '12,470 V', '69 kV' or "
                         f"'12.47/7.2 kV', or 'unknown'; got {value!r}")
    x = float(m.group(1))
    if x <= 0:
        raise ValueError(f"voltage must be more than 0, got {value!r}")
    return x if (m.group(2) or "").startswith("v") else x * 1000


def _row(table: list[dict], volts: float) -> Optional[dict]:
    return next((r for r in table if r["over_volts"] < volts <= r["up_to_volts"]), None)


def _v(volts: float) -> str:
    return f"{_n(volts)} V" if volts < 1000 else f"{_n(volts / 1000)} kV ({_n(volts)} V)"


class OverheadLineResult(_Base):
    voltage_volts: Optional[float] = None
    high_voltage: Optional[bool] = Field(None, description="More than 600 V; None if the voltage is unknown")
    equipment: str
    equipment_key: str
    table_used: Optional[Literal["Table 1", "Table 2"]] = None
    table_basis: str
    table_1_clearance_ft: Optional[float] = None
    table_2_clearance_ft: Optional[float] = None
    required_clearance_ft: Optional[float] = Field(None, description=(
        "Distance to keep. Equals legal_minimum_clearance_ft, except for machines with no boom (scissor lifts, "
        "forklifts, drill rigs, pile drivers, dump trucks), where this tool applies the larger Table 2 distance"))
    legal_minimum_clearance_ft: Optional[float] = Field(None, description=(
        "The minimum 8 CCR 2946 sets by its text for this equipment (Table 2 for boom-type equipment and storage, "
        "Table 1 otherwise)"))
    fallback_clearance_ft: Optional[float] = Field(
        None, description="Voltage unknown: a distance that meets the table for every voltage it lists")
    planned_closest_distance_ft: Optional[float] = None
    clearance_ok: Optional[bool] = Field(None, description="The planned distance meets legal_minimum_clearance_ft")
    shortfall_ft: Optional[float] = Field(None, description="Feet short of legal_minimum_clearance_ft")
    status: Literal["clear", "too_close", "meets_legal_minimum_only", "distance_not_given", "prohibited_over_lines",
                    "voltage_unknown", "not_high_voltage", "deenergized_and_grounded", "qualified_worker_exempt"]
    warning_sign: Literal["required", "likely_required", "not_listed"]
    warning_sign_text: Optional[str] = None
    spotter_required: Optional[bool] = None
    requirements: list[Requirement] = Field(default_factory=list)


def overhead_line_clearance(voltage_kv, equipment: str, planned_closest_distance_ft=None,
                            boom_lowered_in_transit_no_load: bool = False, work_over_lines: bool = False,
                            line_deenergized_and_grounded: bool = False,
                            qualified_electrical_worker_authorized_by_line_owner: bool = False,
                            operator_can_see_clearance: Optional[bool] = None) -> OverheadLineResult:
    """Minimum clearance from an energized overhead line for a machine, crew, ladder, scaffold or stored material."""
    acc = _Acc()
    volts = _volts(voltage_kv)
    key, label, _aliases, basis, sign = _equipment(equipment)
    dist = _num("planned_closest_distance_ft", planned_closest_distance_ft, lo=0, kind="feet")
    transit = bool(_bool("boom_lowered_in_transit_no_load", boom_lowered_in_transit_no_load))
    over = bool(_bool("work_over_lines", work_over_lines))
    dead = bool(_bool("line_deenergized_and_grounded", line_deenergized_and_grounded))
    qew = bool(_bool("qualified_electrical_worker_authorized_by_line_owner",
                     qualified_electrical_worker_authorized_by_line_owner))
    can_see = _bool("operator_can_see_clearance", operator_can_see_clearance)
    t1, t2 = P(HV_T1, "table"), P(HV_T2, "table")
    top = max(r["up_to_volts"] for r in t1 + t2)
    hv_over = P(HV_DEF, "high_voltage_above_volts")
    if volts is not None and volts > top:
        raise ValueError(f"{_v(volts)} is above {_v(top)}, the top of Tables 1 and 2 in 8 CCR 2946. A bare number "
                         "means kilovolts (12.47 = 12,470 V); for volts write e.g. '12470 V'. Confirm the voltage "
                         "with the line operator.")
    machine = basis in _MACHINE_CLASSES
    use_t1 = basis == "table_1_rule_text" or (machine and transit and key != "storage")
    table = "Table 1" if use_t1 else "Table 2"
    legal_t1 = use_t1 or basis == "table_2_conservative"          # the table the rule text itself sets
    if basis == "table_1_rule_text":
        why = "Table 1 covers people, tools, ladders, scaffolds, materials and structures (8 CCR 2946(b)(2))."
    elif machine and transit and key != "storage":
        why = ("Equipment travelling with its boom (or mast, bed or platform) lowered and no load uses Table 1 "
               "(8 CCR 2946(b)(2)).")
    elif key == "storage":
        why = "Table 2 covers storage under, by or near the lines (8 CCR 2946(b)(4))."
    elif basis == "table_2_rule_text":
        why = ("Table 2 applies by the rule text: 'Operation of boom-type equipment shall conform to the minimum "
               "clearances set forth in Table 2' (8 CCR 2946(b)(2)), and boom-type lifting or hoisting equipment may "
               "not come closer than Table 2 (2946(b)(3)).")
    else:
        why = ("This machine has no boom, so the rule text puts it on Table 1 (machinery, 8 CCR 2946(b)(2)): that "
               "is the legal minimum. This tool still applies Table 2's larger distance because the machine raises "
               "a mast, leads, bed or platform (and the people on it) toward the line. If the leads or load hang from "
               "a crane or excavator boom, that machine is boom-type equipment and Table 2 is the legal minimum.")
    res = OverheadLineResult(voltage_volts=volts, equipment=label, equipment_key=key, table_basis=why, status="clear",
                             warning_sign=sign, planned_closest_distance_ft=dist)
    table_rule = HV_T1 if use_t1 else HV_T2
    acc.use(HV_DEF)
    if key in ("aerial_lift", "scissor_lift"):
        acc.use(AWP_HV)                                 # lift work near HV lines follows Article 37
    # The 2947 sign goes on the machine whatever line it works near.
    if sign != "not_listed":
        res.warning_sign_text = P(HV_SIGN, "sign_text")
        legible = _n(P(HV_SIGN, "legible_at_ft"))
        if sign == "required":
            acc.req("High-voltage warning sign on the machine",
                    f"Post and maintain, in plain view of the operator, a durable sign legible at {legible} ft "
                    f"reading: \"{res.warning_sign_text}\"", HV_SIGN)
        else:
            acc.req("High-voltage warning sign (likely: 'similar apparatus')",
                    f"8 CCR 2947 names {_join(P(HV_SIGN, 'equipment'))}. Treat this machine as similar and post the "
                    f"sign legible at {legible} ft in view of the operator: \"{res.warning_sign_text}\"", HV_SIGN)

    if volts is not None:
        acc.math.append(f"Line voltage: {_v(volts)}, nominal phase to phase")
    # ---- exemptions and special cases, in order
    if qew:
        res.status = "qualified_worker_exempt"
        acc.rule(HV_QEW_EXEMPT, "Qualified electrical worker exemption")
        acc.rule(HV_QEW, "Who is a qualified electrical worker")
        res.summary = ("8 CCR 2946-2948 do not apply to building, rebuilding, maintaining or operating the "
                       "energized line itself (or its poles, towers and attachments) by qualified electrical workers "
                       "authorized by the line owner, or to work near the line by qualified persons using the "
                       "approved equipment and procedures of the High-Voltage Electrical Safety Orders. The "
                       "exemption covers that work only: other construction near the line keeps the clearances, "
                       "whoever does it.")
        res.next_step = (f"Confirm the work is on the line or its supporting structures, that each crew member has at "
                         f"least {P(HV_QEW, 'min_years_training_and_experience')} years of high-voltage training and "
                         "experience, and that the line owner authorized the crew in writing; for any other work "
                         "near the line (and for everyone else on site), run this again without the exemption.")
        return acc.finish(res)
    if dead:
        res.status = "deenergized_and_grounded"
        acc.rule(HV_ENERGIZED, "Treat the line as energized until verified")
        res.summary = ("The clearance tables apply to energized lines. A line counts as de-energized only when the "
                       "line's owner or operator verifies it is not energized AND it is visibly grounded at the work "
                       "site.")
        res.next_step = ("Before work: record who at the utility verified the line is de-energized and when, and "
                         "look for the visible grounds at the work site. If either is missing, keep the clearance "
                         "for an energized line.")
        return acc.finish(res)
    if volts is None:
        res.status = "voltage_unknown"
        rows = t1 if use_t1 else t2
        res.fallback_clearance_ft = float(max(r["min_clearance_ft"] for r in rows))
        res.table_used = table
        acc.use(table_rule, *((HV_T1,) if basis == "table_2_conservative" and not use_t1 else ()))
        acc.math.append(f"Voltage unknown: the largest {table} distance is {_n(res.fallback_clearance_ft)} ft "
                        f"(for lines up to {_v(top)}), so that distance meets {table} for any voltage it lists")
        acc.rule(HV_ENERGIZED, "Treat the line as energized")
        acc.rule(HV_NOTIFY, "Notify the line operator")
        acc.rule(HV_SURVEY, "Find out where energized circuits are before work")
        res.summary = (f"Line voltage unknown: the {table} clearance for a {label.lower()} cannot be picked yet. "
                       f"Staying {_n(res.fallback_clearance_ft)} ft away meets {table} at any voltage up to "
                       f"{_v(top)}."
                       + (f" (This tool applies Table 2 to a machine with no boom; the rule text's Table 1 tops out "
                          f"at {_n(max(r['min_clearance_ft'] for r in t1))} ft.)"
                          if basis == "table_2_conservative" and not use_t1 else ""))
        res.next_step = ("Call the line operator (utility) for the line's nominal voltage and run this again; until "
                         f"then keep every part of the {label.lower()} at least {_n(res.fallback_clearance_ft)} ft "
                         "from the line and treat it as energized.")
        return acc.finish(res)
    res.high_voltage = volts > hv_over
    if not res.high_voltage:
        res.status = "not_high_voltage"
        acc.math.append(f"{_n(volts)} V <= {_n(hv_over)} V -> not high voltage (8 CCR 2700)")
        acc.rule(HV_SURVEY, "Find out where energized circuits are before work")
        res.summary = (f"{_v(volts)} is not high voltage (high voltage means more than {_n(hv_over)} V), so the "
                       "Article 37 overhead-line clearance tables (8 CCR 2946) do not apply.")
        res.next_step = ("Treat the conductors as energized: survey and mark the circuits, warn the crew, and keep "
                         "people, tools and equipment off them; ask the utility to cover or de-energize them if work "
                         "must get close.")
        return acc.finish(res)
    r1, r2 = _row(t1, volts), _row(t2, volts)
    res.table_1_clearance_ft, res.table_2_clearance_ft = float(r1["min_clearance_ft"]), float(r2["min_clearance_ft"])
    res.table_used = table
    req = res.table_1_clearance_ft if use_t1 else res.table_2_clearance_ft
    legal = res.table_1_clearance_ft if legal_t1 else res.table_2_clearance_ft
    res.required_clearance_ft, res.legal_minimum_clearance_ft = req, legal
    extra_margin = req > legal                                   # a no-boom machine: Table 2 applied by this tool
    if extra_margin:
        acc.use(HV_T1)
    acc.math.append(f"{_n(volts)} V > {_n(hv_over)} V -> high voltage")
    acc.math.append(f"Table 1 row over {_v(r1['over_volts'])} to {_v(r1['up_to_volts'])}: "
                    f"{_n(res.table_1_clearance_ft)} ft; Table 2 row over {_v(r2['over_volts'])} to "
                    f"{_v(r2['up_to_volts'])}: {_n(res.table_2_clearance_ft)} ft -> "
                    + (f"legal minimum (Table 1) {_n(legal)} ft; this tool applies Table 2: {_n(req)} ft"
                       if extra_margin else f"{table} applies: {_n(req)} ft"))
    parts = {"storage": "every stored item, including while it is being handled or moved",
             "machine": f"every part of the {label.lower()} (boom, mast, platform, bed, load, rigging and tag lines) "
                        "and of anyone on it",
             "people": "every part of a worker's body and every tool, ladder, scaffold, material or structure they "
                       "handle"}["storage" if key == "storage" else "machine" if machine else "people"]
    if extra_margin:
        acc.req(f"Clearance: {_n(req)} ft applied by this tool (legal minimum {_n(legal)} ft, Table 1)",
                f"Keep {parts} at least {_n(req)} ft from the energized line. 8 CCR 2946(b)(2) sets Table 1's "
                f"{_n(legal)} ft for machinery with no boom; the Table 2 distance is applied here as a margin for a "
                "machine that raises people or loads toward the line. Line or structure movement (sway, sag) may "
                "not reduce the clearance.", HV_T1, HV_T2)
    else:
        acc.req(f"Minimum clearance: {_n(req)} ft ({table})",
                f"Keep {parts} at least {_n(req)} ft from the energized line. Line or structure movement (sway, sag) "
                "may not reduce it.", table_rule)
    acc.rule(HV_GUARD, "Guard against contact before work starts")
    acc.rule(HV_ENERGIZED, "Treat the line as energized")
    if key == "storage":
        acc.rule(HV_STORE, "No storage within reach of the lines")
    if machine and key != "storage":
        res.spotter_required = None if can_see is None else (not can_see)
        sp = core.rule(HV_SPOTTER)
        acc.req("Spotter", sp.plain_english + (" Required here: you said the operator cannot easily see the clearance."
                                               if res.spotter_required else
                                               " Optional devices (" + _join(sp.param("devices_optional"))
                                               + ") do not reduce the clearance."), HV_SPOTTER)
    if over:
        res.status = "prohibited_over_lines"
        res.clearance_ok = False
        ov = core.rule(HV_OVER)
        acc.req("No work over energized lines", f"{ov.plain_english} Exceptions: {_join(ov.param('exceptions'))}.",
                HV_OVER)
        res.summary = (f"Prohibited: handling or swinging anything over energized {_v(volts)} lines is not allowed, "
                       f"whatever the clearance ({table}: {_n(req)} ft).")
        res.next_step = ("Re-plan the pick or work so nothing passes over the line, or have the line operator "
                         "de-energize and visibly ground it first.")
        acc.rule(HV_NOTIFY, "Notify the line operator")
        return acc.finish(res)
    if dist is None:
        res.status = "distance_not_given"
        acc.rule(HV_NOTIFY, "Notify the line operator if work will come inside the clearance")
        res.summary = (f"{label}: minimum clearance {_n(req)} ft from the {_v(volts)} line ({table})."
                       if not extra_margin else
                       f"{label}: keep {_n(req)} ft from the {_v(volts)} line (Table 2, applied by this tool; the "
                       f"legal minimum for a machine with no boom is Table 1's {_n(legal)} ft).")
        res.next_step = ("Measure or plan the closest approach ("
                         + ("including boom swing, load and line sag" if machine and key != "storage" else
                            "including how the items will be handled" if key == "storage" else
                            "including long tools, ladders and materials being carried")
                         + f"); keep it at {_n(req)} ft or more, or call the line operator before work that would "
                           f"come closer than {_n(legal)} ft.")
        return acc.finish(res)
    res.clearance_ok = dist >= legal                               # meets what 2946 itself requires
    if dist >= req:
        acc.math.append(f"Planned {_n(dist)} ft >= required {_n(req)} ft -> clear by {_n(dist - req)} ft")
        res.status = "clear"
        res.summary = (f"{label}: planned {_n(dist)} ft meets the {_n(req)} ft minimum from the {_v(volts)} line "
                       f"({table}).")
        res.next_step = (f"Mark the {_n(req)} ft limit on site, brief "
                         + ("the operator and crew, " if machine and key != "storage" else "the crew, ")
                         + ("post a spotter, " if res.spotter_required else "")
                         + "and keep treating the line as energized.")
    elif dist >= legal:
        acc.math.append(f"Planned {_n(dist)} ft >= legal minimum {_n(legal)} ft (Table 1) but < {_n(req)} ft applied "
                        f"by this tool (Table 2) -> {_n(round(req - dist, 2))} ft inside the margin")
        res.status = "meets_legal_minimum_only"
        res.summary = (f"{label}: planned {_n(dist)} ft meets the legal minimum of {_n(legal)} ft (Table 1, machine "
                       f"with no boom) from the {_v(volts)} line, but not the {_n(req)} ft this tool applies "
                       f"(Table 2).")
        res.next_step = (f"Move the setup back to {_n(req)} ft if you can. If it must stay at {_n(dist)} ft, never let "
                         f"any part of the machine, its load or the people on it come inside {_n(legal)} ft: mark "
                         "the limit, "
                         + ("post a spotter, " if res.spotter_required else "use a spotter, ")
                         + "and keep treating the line as energized.")
    else:
        res.shortfall_ft = round(legal - dist, 6)
        acc.math.append(f"Planned {_nx(dist, legal)} ft < required {_n(legal)} ft -> {_nx(legal - dist, 0)} ft too "
                        "close")
        res.status = "too_close"
        acc.rule(HV_NOTIFY, "Notify the line operator before working inside the clearance")
        res.summary = (f"Too close: {label.lower()} planned at {_nx(dist, legal)} ft, but "
                       + (f"{table} requires {_n(legal)} ft" if not extra_margin else
                          f"even Table 1's legal minimum is {_n(legal)} ft (this tool applies {_n(req)} ft)")
                       + f" from the {_v(volts)} line ({_nx(res.shortfall_ft, 0)} ft short).")
        res.next_step = (f"Do not start. Call the line operator now to arrange de-energizing and visible grounding "
                         f"(or other safety measures) before any work inside {_n(legal)} ft (8 CCR 2948), or re-plan "
                         f"the setup to keep at least {_n(req)} ft.")
    return acc.finish(res)


# ================================================================ confined spaces (8 CCR 1950-1962)
SiteRole = Literal["subcontractor", "controlling_contractor", "host_employer"]
_CS_ROLES = {
    "controlling_contractor": ("gc", "general contractor", "general", "prime", "prime contractor", "controlling",
                               "construction manager", "cm", "builder", "design builder"),
    "host_employer": ("owner", "host", "property owner", "facility owner", "client", "developer", "owner builder"),
    "subcontractor": ("sub", "subcontractor", "trade", "trade contractor", "specialty contractor", "entry employer",
                      "contractor", "other employer", "employer"),
}


def _cs_role(value) -> str:
    w = _words(value or "subcontractor").removeprefix("the ")
    for role, aliases in _CS_ROLES.items():
        if w == role.replace("_", " ") or w in aliases:
            return role
    for role, aliases in _CS_ROLES.items():
        if any(re.search(rf"\b{re.escape(a)}\b", w) for a in aliases if len(a) > 3):
            return role
    raise ValueError(f"your_role must be subcontractor, controlling_contractor (the GC) or host_employer (the owner); "
                     f"got {value!r}")


class Reading(BaseModel):
    measure: str
    value: Optional[float] = None
    limit: str
    hazardous: Optional[bool] = None
    rule_id: str


EntryStatus = Literal["not_covered_by_article_37", "not_a_confined_space", "test_first", "non_permit_space",
                      "permit_space_keep_out", "entry_prohibited", "alternate_procedure_allowed", "permit_entry"]


class ConfinedSpaceResult(_Base):
    space: str
    your_role: SiteRole
    article_37_applies: bool
    confined_space: Optional[bool] = None
    permit_required: Optional[bool] = Field(None, description="None until the air has been tested")
    hazardous_atmosphere: Optional[bool] = None
    hazards: list[str] = Field(default_factory=list)
    readings: list[Reading] = Field(default_factory=list)
    entry_status: EntryStatus
    alternate_procedure_eligible: bool = False
    hot_work: Literal["not_planned", "prohibited", "confirm_atmosphere", "allowed_with_permit"] = "not_planned"
    mechanical_retrieval_device_required: Optional[bool] = None
    requirements: list[Requirement] = Field(default_factory=list)


def confined_space_classification(
        space: str, can_bodily_enter: bool, limited_entry_or_exit: bool, designed_for_continuous_occupancy: bool,
        oxygen_pct=None, flammable_pct_lfl=None, toxic_could_exceed_pel: Optional[bool] = None,
        combustible_dust_pct_mec=None, atmosphere_could_become_hazardous: bool = False,
        engulfment_hazard: bool = False, converging_walls_or_sloped_floor: bool = False,
        other_serious_hazard: Optional[str] = None, vertical_depth_ft=None, workers_will_enter: bool = True,
        your_role: str = "subcontractor", hot_work_planned: bool = False,
        ventilation_alone_keeps_safe: Optional[bool] = None,
        is_excavation_or_tunnel: bool = False) -> ConfinedSpaceResult:
    """Classify one space under 8 CCR 1950-1962 (as amended effective 2026-01-01) and list what must happen before
    and during entry."""
    acc = _Acc()
    name = str(space or "").strip()
    if not name:
        raise ValueError("space is required, e.g. 'sewer manhole MH-3' or 'crawl space under unit 4'")
    role = _cs_role(your_role)
    enter = _bool("can_bodily_enter", can_bodily_enter)
    limited = _bool("limited_entry_or_exit", limited_entry_or_exit)
    continuous = _bool("designed_for_continuous_occupancy", designed_for_continuous_occupancy)
    if enter is None or limited is None or continuous is None:
        raise ValueError("can_bodily_enter, limited_entry_or_exit and designed_for_continuous_occupancy must each be "
                         "true or false")
    o2 = _num("oxygen_pct", oxygen_pct, lo=0, hi=100, kind="percent")
    lfl = _num("flammable_pct_lfl", flammable_pct_lfl, lo=0, hi=100, kind="percent")
    dust = _num("combustible_dust_pct_mec", combustible_dust_pct_mec, lo=0, kind="percent")
    depth = _num("vertical_depth_ft", vertical_depth_ft, lo=0, kind="feet")
    toxic = _bool("toxic_could_exceed_pel", toxic_could_exceed_pel)
    potential = bool(_bool("atmosphere_could_become_hazardous", atmosphere_could_become_hazardous))
    engulf = bool(_bool("engulfment_hazard", engulfment_hazard))
    walls = bool(_bool("converging_walls_or_sloped_floor", converging_walls_or_sloped_floor))
    other = str(other_serious_hazard or "").strip() or None
    entering = bool(_bool("workers_will_enter", workers_will_enter))
    hot = bool(_bool("hot_work_planned", hot_work_planned))
    vent_ok = _bool("ventilation_alone_keeps_safe", ventilation_alone_keeps_safe)
    excav = bool(_bool("is_excavation_or_tunnel", is_excavation_or_tunnel))

    acc.use(CS_SCOPE)
    res = ConfinedSpaceResult(space=name, your_role=role, article_37_applies=True, entry_status="test_first")
    acc.notes.append("Reflects 8 CCR 1950-1962 as amended effective 2026-01-01 (OAL-approved text); the dir.ca.gov "
                     "Title 8 pages may still show the 2015 wording.")
    if excav:
        res.article_37_applies, res.entry_status = False, "not_covered_by_article_37"
        exc = P(CS_SCOPE, "exceptions")
        res.summary = f"Article 37 (confined spaces in construction) does not apply to: {_join(exc)}."
        res.next_step = ("Use the excavation rules (CSO Article 6) or the Tunnel Safety Orders for this work; see "
                         "check_cal_osha_permits and lookup_safety_standard('excavation'). A separate confined space "
                         "on the same project (vault, manhole, tank) is still covered: check it on its own.")
        return acc.finish(res)

    # ---- 1. is it a confined space? (all three criteria)
    crit = P(CS_DEF, "criteria")
    acc.use(CS_DEF)
    met = [enter, limited, not continuous]
    acc.math.append(f"Confined space test (all 3 needed): {crit[0]}: {'yes' if enter else 'no'}; {crit[1]}: "
                    f"{'yes' if limited else 'no'}; {crit[2]}: {'yes' if not continuous else 'no'} -> "
                    f"{sum(met)} of 3")
    acc.rule(CS_SURVEY, "Competent-person survey when work begins")
    acc.rule(CS_ONGOING, "Keep inspecting for new confined spaces")
    acc.rule(CS_NOTIFY_NEW, "Report new confined spaces")
    if not all(met):
        res.confined_space, res.permit_required, res.entry_status = False, False, "not_a_confined_space"
        res.summary = f"{name}: not a confined space under 8 CCR 1951 (it meets {sum(met)} of the 3 criteria)."
        res.next_step = ("Record the competent person's finding in the site survey, and look again if the work "
                         "changes the space (for example, once it is enclosed or its access is restricted).")
        return acc.finish(res)
    res.confined_space = True

    # ---- 2. atmosphere and other hazards
    acc.use(CS_PERMIT_DEF, CS_CLASSIFY, CS_O2, CS_LFL, CS_TOXIC)
    o2_lo, o2_hi = P(CS_O2, "oxygen_min_percent"), P(CS_O2, "oxygen_max_percent")
    lfl_max = P(CS_LFL, "hazardous_if_greater_than")
    rd = []
    if o2 is None:
        rd.append(Reading(measure="oxygen", limit=f"{_n(o2_lo)}-{_n(o2_hi)}% by volume", rule_id=CS_O2))
    else:
        bad = o2 < o2_lo or o2 > o2_hi
        rd.append(Reading(measure="oxygen", value=o2, limit=f"{_n(o2_lo)}-{_n(o2_hi)}% by volume", hazardous=bad,
                          rule_id=CS_O2))
        acc.math.append(f"Oxygen {_nx(o2, o2_lo if o2 < 21 else o2_hi)}%: "
                        + (f"below {_n(o2_lo)}% -> oxygen deficient (hazardous)" if o2 < o2_lo
                        else f"above {_n(o2_hi)}% -> oxygen enriched (hazardous)" if o2 > o2_hi
                        else f"within {_n(o2_lo)}-{_n(o2_hi)}% -> OK"))
    if lfl is None:
        rd.append(Reading(measure="flammable gas/vapor", limit=f"no more than {_n(lfl_max)}% of LFL", rule_id=CS_LFL))
    else:
        rd.append(Reading(measure="flammable gas/vapor", value=lfl, limit=f"no more than {_n(lfl_max)}% of LFL",
                          hazardous=lfl > lfl_max, rule_id=CS_LFL))
        acc.math.append(f"Flammables {_nx(lfl, lfl_max)}% LFL " + (f"> {_n(lfl_max)}% -> hazardous" if lfl > lfl_max
                                                         else f"<= {_n(lfl_max)}% -> not hazardous by this test"))
    rd.append(Reading(measure="toxic substance", limit="could not exceed its dose or PEL",
                      value=None, hazardous=toxic, rule_id=CS_TOXIC))
    if dust is not None:
        acc.use(CS_DUST)
        dmax = P(CS_DUST, "hazardous_if_greater_than")
        rd.append(Reading(measure="combustible dust", value=dust, limit=f"no more than {_n(dmax)}% of MEC",
                          hazardous=dust > dmax, rule_id=CS_DUST))
        acc.math.append(f"Combustible dust {_nx(dust, dmax)}% MEC " + (f"> {_n(dmax)}% -> hazardous" if dust > dmax
                                                                else f"<= {_n(dmax)}% -> not hazardous by this test"))
    res.readings = rd
    actual = any(r.hazardous for r in rd)
    complete = o2 is not None and lfl is not None and toxic is not None
    hazards = []
    if o2 is not None and o2 < o2_lo:
        hazards.append(f"oxygen deficient ({_nx(o2, o2_lo)}% < {_n(o2_lo)}%)")
    if o2 is not None and o2 > o2_hi:
        hazards.append(f"oxygen enriched ({_nx(o2, o2_hi)}% > {_n(o2_hi)}%)")
    if lfl is not None and lfl > lfl_max:
        hazards.append(f"flammables {_nx(lfl, lfl_max)}% LFL > {_n(lfl_max)}%")
    if dust is not None and dust > P(CS_DUST, "hazardous_if_greater_than"):
        hazards.append(f"combustible dust {_nx(dust, P(CS_DUST, 'hazardous_if_greater_than'))}% MEC > "
                       f"{_n(P(CS_DUST, 'hazardous_if_greater_than'))}%")
    if toxic:
        hazards.append("toxic substance could exceed its PEL")
    lfl_stop = P(CS_NO_ENTRY_LFL, "threshold_percent_LFL")
    # 1951 calls the air hazardous only "in excess of 10 percent" LFL, while 1953(a)(3)(D)1 bars entry into a permit
    # space until flammables are "below 10 percent". A reading AT the limit is not yet hazardous, but the space plainly
    # "has a potential to contain a hazardous atmosphere" (the permit-space definition), so it is treated as one.
    at_limit = lfl is not None and lfl_stop <= lfl <= lfl_max
    if at_limit:
        acc.use(CS_NO_ENTRY_LFL)
        hazards.append(f"flammables at {_n(lfl)}% LFL, the limit (potential hazardous atmosphere)")
        acc.notes.append(f"Flammables are at {_n(lfl)}% LFL: not yet a hazardous atmosphere (that needs more than "
                         f"{_n(lfl_max)}%), but any rise makes it one, so the space is treated as having a potential "
                         f"hazardous atmosphere (a permit space), and 8 CCR 1953(a)(3)(D)1 bars entry until "
                         f"flammables are below {_n(lfl_stop)}%.")
    if potential:
        hazards.append("potential hazardous atmosphere")
    physical = [h for h, on in (("engulfment hazard", engulf),
                                ("inwardly converging walls or tapering floor", walls),
                                (f"other serious hazard: {other}", bool(other))) if on]
    hazards += physical
    res.hazards = hazards
    res.hazardous_atmosphere = True if (actual or potential or at_limit) else (False if complete else None)
    if res.hazardous_atmosphere or physical:
        res.permit_required = True
    elif res.hazardous_atmosphere is False:
        res.permit_required = False
    missing = [m for m, v in (("oxygen", o2), ("flammable gases and vapors (% LFL)", lfl),
                              ("toxic gases and vapors", toxic)) if v is None]
    order = P(CS_TEST_ORDER, "test_sequence")
    no_entry = bool(res.permit_required) and lfl is not None and lfl >= lfl_stop
    flammable_or_enriched = (lfl is not None and lfl >= lfl_stop) or (o2 is not None and o2 > o2_hi)

    # ---- hot work (1953(a)(3)(I))
    if hot:
        acc.use(CS_IGNITION)
        if flammable_or_enriched:
            res.hot_work = "prohibited"
        elif potential or not complete:
            res.hot_work = "confirm_atmosphere"
        else:
            res.hot_work = "allowed_with_permit"

    # ---- 3. entry status
    if not entering:
        res.entry_status = ("permit_space_keep_out" if res.permit_required else
                            "non_permit_space" if res.permit_required is False else "test_first")
    elif no_entry:
        res.entry_status = "entry_prohibited"
    elif res.permit_required is None:
        res.entry_status = "test_first"
    elif res.permit_required is False:
        res.entry_status = "non_permit_space"
    else:
        res.alternate_procedure_eligible = (not physical) and vent_ok is True
        res.entry_status = "alternate_procedure_allowed" if res.alternate_procedure_eligible else "permit_entry"

    # ---- 4. requirements
    acc.rule(CS_CLASSIFY, "Competent person classifies the space (testing is required)")
    if entering or res.permit_required is None:
        acc.req("Test the air in order", f"Test {_join(order, 'then')}. "
                f"{core.rule(CS_TEST_ORDER).plain_english}", CS_TEST_ORDER)
    if res.permit_required:
        acc.req("Warn workers: DANGER sign", f"{core.rule(CS_SIGN).plain_english}", CS_SIGN)
        acc.rule(CS_INFORM, "Tell the GC and worker representatives (not just by sign)")
        acc.rule(CS_PREVENT, "Keep out anyone not authorized to enter")
    if res.hot_work == "prohibited":
        acc.req("Hot work prohibited in and next to this space", core.rule(CS_IGNITION).plain_english, CS_IGNITION)
    elif res.hot_work == "confirm_atmosphere":
        acc.req("Hot work: confirm the atmosphere first",
                core.rule(CS_IGNITION).plain_english + " Test (and keep monitoring) before any flame, arc or spark.",
                CS_IGNITION)
    if role == "controlling_contractor":
        acc.rule(CS_EXCHANGE, "Before entry: pass the owner's information to every entering sub")
        acc.rule(CS_DEBRIEF, "After entry: debrief each entering sub and report to the owner")
    elif role == "host_employer":
        acc.rule(CS_EXCHANGE, "Before entry: give the GC what you know about this space")
        acc.rule(CS_NO_GC, "No GC on site: you take the controlling contractor's role")
    elif entering and res.permit_required:
        acc.rule(CS_EXCHANGE, "Before entry: tell the GC which permit program you will follow")
        acc.rule(CS_DEBRIEF, "After entry: report the program used and hazards found to the GC")

    if res.entry_status == "entry_prohibited":
        ne = core.rule(CS_NO_ENTRY_LFL)
        acc.math.append(f"Flammables {_n(lfl)}% LFL >= {_n(lfl_stop)}% -> no entry until reduced below "
                        f"{_n(lfl_stop)}%")
        acc.req(f"No entry at {_n(lfl_stop)}% LFL or more",
                f"{ne.plain_english} Exception requires all of: {_join(ne.param('exception_requires_all'))}.",
                CS_NO_ENTRY_LFL)
    if res.entry_status == "alternate_procedure_allowed":
        alt = core.rule(CS_ALT)
        acc.req("Alternate (ventilation-only) procedure", f"{alt.plain_english} Conditions (all required): "
                f"{_join(alt.param('conditions_all_required'))}. Still required: "
                f"{_join(alt.param('still_required'))}.", CS_ALT)
        cert = core.rule(CS_ALT_CERT)
        acc.req("Written pre-entry certification", f"Before each entry, certify in writing ("
                f"{_join(cert.param('fields'))}) that the space is safe and the pre-entry steps were taken, and make "
                f"it available to {cert.param('available_to')}.", CS_ALT_CERT)
    if res.entry_status == "permit_entry":
        acc.rule(CS_PROGRAM, "Written permit-space program on site")
        el = core.rule(CS_ELEMENTS)
        acc.req(f"Program contents ({el.param('element_count')} elements)", _join(el.param("elements")) + ".",
                CS_ELEMENTS)
        pi = core.rule(CS_PERMIT_ITEMS)
        acc.rule(CS_PERMIT, "Entry permit: signed by the entry supervisor and posted")
        acc.req(f"Entry permit: {pi.param('item_count')} required items", _join(pi.param("items")) + ".",
                CS_PERMIT_ITEMS)
        acc.req(f"At least {P(CS_ATTENDANT, 'minimum_attendants')} attendant outside the whole time",
                core.rule(CS_ATTENDANT).plain_english, CS_ATTENDANT)
        acc.rule(CS_EQUIP, "Equipment at no cost to workers")
        acc.rule(CS_SURVEIL, "Watch the surrounding area")
        acc.rule(CS_RESCUE_PROC, "Rescue and emergency medical procedures")
        acc.rule(CS_RESCUE, "Rescue service immediately available")
        acc.rule(CS_RESCUE_SELECT, "Evaluate and select the rescue service")
        acc.req("If your own employees are the rescue team",
                f"{core.rule(CS_INHOUSE).plain_english} (Practice at least every "
                f"{P(CS_INHOUSE, 'practice_interval_months_max')} months.)", CS_INHOUSE)
        dmin = P(CS_RETRIEVAL, "mechanical_device_required_if_depth_greater_than_ft")
        if depth is not None:
            res.mechanical_retrieval_device_required = depth > dmin
            acc.math.append(f"Vertical depth {_nx(depth, dmin)} ft "
                            + (f"> {_n(dmin)} ft -> mechanical retrieval device required" if depth > dmin else
                               f"<= {_n(dmin)} ft -> no mechanical device required (harness and retrieval line "
                               "still are)"))
        acc.req("Non-entry rescue: harness, retrieval line"
                + (", mechanical retrieval device" if res.mechanical_retrieval_device_required else ""),
                core.rule(CS_RETRIEVAL).plain_english, CS_RETRIEVAL)
        acc.rule(CS_ENTRANT, "Entrant duties")
        acc.rule(CS_ATTENDANT_DUTIES, "Attendant duties")
        acc.rule(CS_SUPERVISOR, "Entry supervisor duties")
        acc.rule(CS_TRAIN_WHEN, "Training: when")
        acc.rule(CS_TRAIN_CONTENT, "Training: content")
        acc.rule(CS_TRAIN_RECORDS, "Training records")
        acc.rule(CS_CANCEL, "Cancel or suspend the permit")
        yrs = P(CS_RETAIN, "retention_years_min")
        acc.req(f"Keep canceled permits at least {yrs} year", core.rule(CS_RETAIN).plain_english, CS_RETAIN)
        ry = P(CS_REVIEW, "review_within_years_after_entry")
        acc.req(f"Review the program within {ry} year after each entry", core.rule(CS_REVIEW).plain_english,
                CS_REVIEW)
        acc.rule(CS_SDS, "Send the SDS with an injured entrant")
        acc.rule(CS_PARTICIPATION, "Employee participation and access to records")
        if hot and res.hot_work == "allowed_with_permit":
            acc.notes.append("Hot work: list the hot work permit on the entry permit (the 'additional permits' item), "
                             "and see jobsite_hazard_checklist(['welding']) for the welding and fire rules.")
    if res.permit_required and physical and res.hazardous_atmosphere is False:
        acc.req("Option: reclassify as a non-permit space", core.rule(CS_RECLASSIFY).plain_english
                + " Fields: " + _join(P(CS_RECLASSIFY, "fields")) + ".", CS_RECLASSIFY)

    # ---- 5. summary and next step
    rp = ("permit-required" if res.permit_required else "not permit-required" if res.permit_required is False
          else "not yet classified (air not fully tested)")
    res.summary = (f"{name}: a confined space, {rp}" + (f" ({_join(hazards)})" if hazards else "") + ". "
                   + {"entry_prohibited": f"NO ENTRY: flammables at {_n(lfl or 0)}% LFL, at or above "
                                          f"{_n(lfl_stop)}%.",
                      "test_first": "Test the air before anyone enters.",
                      "non_permit_space": "Entry without a permit program is allowed on these facts.",
                      "permit_space_keep_out": "Keep your workers out; post and report it.",
                      "alternate_procedure_allowed": "Ventilation-only alternate procedure allowed.",
                      "permit_entry": "Full permit-space entry rules apply."}[res.entry_status])
    if res.entry_status == "entry_prohibited":
        res.next_step = (f"Do not enter. Ventilate or purge and retest; entry is allowed only once flammables are "
                         f"below {_n(lfl_stop)}% LFL (or under the inerting exception with IDLH respirators and "
                         "every other hazard isolated).")
    elif res.entry_status == "test_first":
        res.next_step = (f"Before anyone enters, have the competent person test the air in this order: "
                         f"{_join(order, 'then')}. Missing: {_join(missing)}. Then run this check again with the "
                         "readings.")
    elif res.entry_status == "non_permit_space":
        res.next_step = ("Document the competent person's evaluation and test results. Re-evaluate before entry if "
                         "anything changes (coatings, solvents, hot work, new piping, a change in readings).")
        if hot:
            res.next_step += (" The planned hot work is such a change: welding fumes, shielding gas or a leaking "
                              "oxygen hose can create a hazardous atmosphere, so have the competent person "
                              "re-evaluate the space with the hot work running and keep monitoring the air.")
    elif res.entry_status == "permit_space_keep_out":
        res.next_step = (f"Post '{P(CS_SIGN, 'sample_sign_text')}' signs (or equal), tell the GC and your workers' "
                         "representatives where it is and what the danger is, and keep your crew out.")
    elif res.entry_status == "alternate_procedure_allowed":
        res.next_step = ("Before each entry: test the air (oxygen, flammables, toxics), run continuous forced-air "
                         "ventilation and let no one in until it has cleared any hazardous atmosphere, keep "
                         "monitoring, and sign the written pre-entry certification (date, location, signature) where "
                         "entrants can see it. If a hazard is detected during entry, everyone leaves at once.")
    elif res.entry_status == "permit_entry":
        retr = (f" with a mechanical retrieval device ready for this {_n(depth)} ft deep vertical space"
                if res.mechanical_retrieval_device_required else "")
        res.next_step = (f"Before entry: have your written permit-space program on site, fill in the "
                         f"{P(CS_PERMIT_ITEMS, 'item_count')}-item entry permit and have the entry supervisor sign "
                         f"and post it, station at least {P(CS_ATTENDANT, 'minimum_attendants')} attendant outside, "
                         f"and confirm a rescue service is immediately available{retr}.")
    if role == "controlling_contractor" and res.permit_required:
        res.next_step += (" As the controlling contractor, pass the owner's information about this space to every "
                          "entering sub before entry and debrief them afterward.")
    return acc.finish(res)


# ================================================================ Valley Fever (Labor Code 6709 as described by DOSH)
_COUNTY_ABBREV = {"la": "Los Angeles", "oc": "Orange", "sd": "San Diego", "sf": "San Francisco",
                  "slo": "San Luis Obispo"}
_COUNTY_AMBIGUOUS = {"sb": "San Bernardino or Santa Barbara", "sc": "Santa Clara or Santa Cruz",
                     "sj": "San Joaquin, or a city", "sm": "San Mateo, or a city"}


def _ca_counties() -> list[str]:
    names = list(P(VF_TRAIN, "counties"))
    try:
        sp = core.rule(_COUNTY_SPELLING_RULE)
        names += [c for k in ("usa_south_digalert_counties", "usa_north_counties") for c in sp.param(k, [])]
    except KeyError:
        pass
    return sorted(set(names))


def _county(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("county is required, e.g. 'Kern'")
    t = re.sub(r"\s+", " ", raw.lower().replace(".", "").replace("_", " ")).strip()
    t = re.sub(r"[\s,]*\b\d{5}(-\d{4})?$", "", t).strip()                   # trailing ZIP code
    t = re.sub(r"(,\s*|\s+)(ca|calif|california|usa)$", "", t).strip()
    parts = [p.strip() for p in t.split(",") if p.strip()]
    if len(parts) > 1:                                  # 'Bakersfield, Kern County': the part naming a county wins
        named = [p for p in parts if re.search(r"\b(county|co|cnty|cty)$", p) or re.match(r"(the )?county of ", p)]
        if len(named) == 1:
            t = named[0]
    t = re.sub(r"^(the )?(city and )?county of\s+", "", t)
    if re.match(r"^(the )?(city|town) of\b", t) or re.search(r"\bcity$", t):
        raise ValueError(f"{raw!r} is a city. Valley Fever training is set by county: give the county the jobsite is "
                         "in (e.g. 'Kern' for a jobsite in Kern County).")
    t = re.sub(r"\s+(county|co|cnty|cty)$", "", t).strip()
    counties = _ca_counties()
    by_lower = {c.lower(): c for c in counties}
    if t in by_lower:
        return by_lower[t]
    if t.replace(" ", "") in _COUNTY_ABBREV:
        return _COUNTY_ABBREV[t.replace(" ", "")]
    if t.replace(" ", "") in _COUNTY_AMBIGUOUS:
        raise ValueError(f"{raw!r} is ambiguous ({_COUNTY_AMBIGUOUS[t.replace(' ', '')]}); spell out the county.")
    close = get_close_matches(t, list(by_lower), n=3, cutoff=0.75)
    hint = (f" Did you mean {_join([by_lower[c] for c in close], 'or')}?" if close else
            " If it is a city, give the county the jobsite is in.")
    raise ValueError(f"{raw!r} is not a California county name.{hint}")


def _next_month_day(md: str, today: date) -> date:
    m, d = (int(x) for x in md.split("-"))
    nxt = date(today.year, m, d)
    return nxt if nxt >= today else date(today.year + 1, m, d)


class ValleyFeverResult(_Base):
    county: str
    as_of: str
    highly_endemic: Optional[bool] = Field(None, description="None = not on Cal/OSHA's list and no case rate given")
    basis: Literal["listed_by_calosha", "case_rate_over_threshold", "case_rate_at_or_below_threshold",
                   "not_listed_rate_unknown"]
    cases_per_100k: Optional[float] = None
    training_required: Optional[bool] = None
    soil_disturbing_work: bool
    next_annual_training_deadline: Optional[str] = Field(
        None, description="Next May 1 under the reported annual-training rule (NOT verified; see notes)")
    training_topics: list[str] = Field(default_factory=list)
    dust_controls: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)


def valley_fever_requirements(county: str, soil_disturbing_work: bool = True, county_cases_per_100k=None,
                              as_of: Optional[str] = None) -> ValleyFeverResult:
    """Valley Fever training and controls for a construction jobsite in one California county."""
    acc = _Acc()
    name = _county(county)
    soil = bool(_bool("soil_disturbing_work", soil_disturbing_work))
    rate = _num("county_cases_per_100k", county_cases_per_100k, lo=0, kind="per_100k")
    today = _date(as_of, "as_of") if as_of else logic.today_ca()
    listed = P(VF_TRAIN, "counties")
    over = P(VF_THRESHOLD, "cases_per_100000_per_year_over")
    acc.use(VF_TRAIN, VF_THRESHOLD)
    if name in listed:
        endemic, basis = True, "listed_by_calosha"
        acc.math.append(f"{name} is one of the {P(VF_TRAIN, 'county_count')} counties Cal/OSHA lists as highly "
                        "endemic")
        if rate is not None:
            acc.math.append(f"{_n(rate)} cases per 100,000 " + (f"> {_n(over)}" if rate > over else f"<= {_n(over)}")
                            + " (the county is on the list either way)")
    elif rate is not None:
        endemic = rate > over
        basis = "case_rate_over_threshold" if endemic else "case_rate_at_or_below_threshold"
        acc.math.append(f"{_nx(rate, over)} cases per 100,000 per year "
                        + (f"> {_n(over)} -> highly endemic" if endemic else f"<= {_n(over)} -> not highly endemic"))
    else:
        endemic, basis = None, "not_listed_rate_unknown"
    res = ValleyFeverResult(county=name, as_of=today.isoformat(), highly_endemic=endemic, basis=basis,
                            cases_per_100k=rate, soil_disturbing_work=soil)
    if not endemic:
        res.training_required = False if endemic is False else None
        acc.req("Highly endemic test", f"{core.rule(VF_THRESHOLD).plain_english} Cal/OSHA's list ("
                f"{_join(listed)}) is not exhaustive; more counties may be added as case rates change.",
                VF_TRAIN, VF_THRESHOLD)
        if endemic is False:
            res.summary = (f"{name} County is not on Cal/OSHA's highly endemic list and its rate "
                           f"({_nx(rate, over)} per "
                           f"100,000) is not over {_n(over)}: the Valley Fever construction training rule does not "
                           "apply on these facts.")
            res.next_step = (f"No Valley Fever training duty found. Recheck {name} County's rate each year (CDPH); "
                             f"over {_n(over)} cases per 100,000 makes it highly endemic.")
        else:
            res.summary = (f"{name} County is not on Cal/OSHA's list of highly endemic counties, but the list is not "
                           "exhaustive, so the case rate decides.")
            res.next_step = (f"Look up {name} County's latest annual Valley Fever rate from CDPH; if it is over "
                             f"{_n(over)} cases per 100,000, train your crews as required in highly endemic counties "
                             "(run this again with county_cases_per_100k).")
        return acc.finish(res)

    res.training_required = True
    train = core.rule(VF_TRAIN)
    res.training_topics = list(train.param("training_topics_named_by_dosh"))
    acc.req("Valley Fever training for construction workers",
            f"Train every worker on this jobsite (and supervisors) on Valley Fever prevention, covering at least: "
            f"{_join(res.training_topics)}.", VF_TRAIN)
    may = core.rule(VF_MAY1)
    nxt = _next_month_day(may.param("annual_deadline_month_day"), today)
    res.next_annual_training_deadline = nxt.isoformat()
    acc.math.append(f"Next {nxt:%B} {nxt.day} on or after {today.isoformat()}: {nxt.isoformat()} (reported deadline, "
                    "not verified)")
    acc.req("Training timing (NOT VERIFIED)",
            f"Reported: train each year by {nxt:%B} {nxt.day} (next: {nxt.isoformat()}) and before an employee "
            f"begins work {may.param('also_before').replace('employee begins work ', '')}; it may be part of IIPP "
            "training. Labor Code 6709 itself could not be read, so confirm the timing before relying on it.",
            VF_MAY1)
    if soil:
        ctl = core.rule(VF_CONTROLS)
        res.dust_controls = list(ctl.param("controls"))
        acc.req("Dust controls on soil-disturbing work", f"{ctl.plain_english} Respirator filters: "
                f"{_join(ctl.param('respirator_min_filter'), 'or')} (NIOSH-approved), under "
                f"{ctl.param('respirator_program_required')}.", VF_CONTROLS)
    acc.rule(VF_REPORT, "Suspected cases: report and record")
    res.summary = (f"{name} County is highly endemic for Valley Fever ("
                   + ("on Cal/OSHA's list" if basis == "listed_by_calosha" else f"{_nx(rate, over)} cases per "
                                                                                  f"100,000 > {_n(over)}")
                   + "): construction workers there must be trained on Valley Fever prevention"
                   + (", and soil-disturbing work needs dust controls." if soil else "."))
    res.next_step = ("Train every worker on the " + name + " County jobsite "
                     + ("before they start digging, grading or other dust-raising work, " if soil else "")
                     + f"and schedule the annual refresher before {nxt.isoformat()} (date not verified)."
                     + (" Set up water or stabilizers, upwind work and closed cabs before ground disturbance starts."
                        if soil else ""))
    return acc.finish(res)


# ================================================================ jobsite hazard checklist
# Task vocabulary (not law). A word ending in '*' matches as a prefix ('weld*' -> welding, welder); other words match
# as whole words or phrases.
_GROUPS = (
    dict(key="overhead_lines", label="Work near overhead power lines",
         words=("power line*", "powerline*", "overhead line*", "utility line*", "high voltage", "hv line*",
                "service drop*", "transmission line*", "distribution line*", "near lines", "energized line*"),
         rules=(HV_GUARD, HV_OVER, HV_T1, HV_T2, HV_ENERGIZED, HV_NOTIFY, HV_SPOTTER, HV_STORE, HV_QEW_EXEMPT),
         tools=("overhead_line_clearance",)),
    dict(key="confined_spaces", label="Confined spaces (manholes, vaults, tanks, pits, ducts, crawl spaces)",
         words=("confined", "manhole*", "vault*", "inside tank*", "inside the tank*", "inside a tank*", "into tank*",
                "into the tank*", "tank entry", "tank interior*", "tank cleaning", "tank lining*", "tank coating*",
                "elevator pit*", "escalator pit*", "pump pit*", "valve pit*", "equipment pit*", "sump pit*",
                "inside sewer*", "sewer manhole*", "inside storm drain*", "inside duct*", "inside the duct*",
                "duct entry", "duct cleaning", "crawl space*", "crawlspace*", "inside boiler*", "boiler entry",
                "boiler interior*", "silo*", "lift station*", "digester*", "cistern*", "wet well*", "plenum*",
                "pier column*", "drilled shaft*", "enclosed beam*"),
         rules=(CS_SURVEY, CS_ONGOING, CS_NOTIFY_NEW, CS_CLASSIFY, CS_SIGN, CS_PREVENT, CS_PROGRAM),
         tools=("confined_space_classification",)),
    dict(key="wildfire_smoke", label="Outdoor work during wildfire smoke",
         words=("wildfire*", "smoky", "smoke day*", "smoke event*", "heavy smoke", "smoke conditions", "aqi",
                "air quality", "fire season"),
         rules=(SMOKE_SCOPE, SMOKE_CHECK, SMOKE_VOL, SMOKE_MAND), tools=("wildfire_smoke_requirements",)),
    dict(key="demolition", label="Demolition",
         words=("demo", "demolition", "demolish*", "wreck*", "tear down", "teardown", "tear out", "tearout",
                "tear off", "gut", "gutting"),
         rules=(DEMO_SUPERV, DEMO_SURVEY, DEMO_UTIL, DEMO_ASB, DEMO_CORRECT, DEMO_TOPDOWN, DEMO_CANOPY, DEMO_WALL,
                DEMO_FREEWALL, DEMO_ACCESS, DEMO_JOISTS, DEMO_DUST, DEMO_SALVAGE, DEMO_DROP, DEMO_CHUTES, DEMO_RAGS),
         tools=("check_cal_osha_permits",)),
    dict(key="cranes_boom_equipment", label="Cranes, boom trucks, concrete pumps, drill rigs and pile drivers",
         words=("crane*", "boom truck*", "concrete pump*", "pump truck*", "boom pump*", "derrick*", "drill rig*",
                "drilling rig*", "pile driv*", "hoist*", "rigging", "picks"),
         rules=(HV_SIGN, HV_T2, HV_SPOTTER), tools=("overhead_line_clearance", "generate_safety_programs")),
    dict(key="soil_disturbance", label="Digging, grading and other soil-disturbing work (Valley Fever)",
         words=("dig", "digs", "digging", "dug", "grading", "grade", "regrad*", "excavat*", "trench*", "earthwork*",
                "earth moving",
                "earthmoving", "dirt", "soil*", "land clearing", "site clearing", "grubbing", "auger*", "boring",
                "backfill*", "site prep*", "mass grad*"),
         rules=(), tools=("valley_fever_requirements",)),
    dict(key="traffic_control", label="Work in or next to public streets: traffic control and flaggers",
         words=("flagger*", "flagging", "flagman", "flagmen", "flag person*", "traffic", "lane closure*", "street*",
                "road*", "highway*", "sidewalk*", "right of way",
                "work zone*", "curb*", "roadway*", "intersection*"),
         rules=(TRAFFIC_MUTCD, TRAFFIC_HIVIS, TRAFFIC_NIGHT, FLAG_WHEN, FLAG_APPAREL, FLAG_TRAIN, FLAG_RECORDS),
         tools=()),
    dict(key="hot_work", label="Welding, cutting, brazing and other hot work",
         words=("weld*", "torch*", "oxy*", "acetylene", "braz*", "solder*", "hot work", "plasma cut*",
                "cutting torch*", "burning bar*", "thermite"),
         rules=(HOTWORK, WELD_EXHAUST, WELD_TOXIC, WELD_NO_GAS, WELD_COATED, WELD_10BC, WELD_SHUTOFF, WELD_STORAGE,
                WELD_CYL, PPE_SHADES),
         tools=()),
    dict(key="electrical", label="Electrical work, temporary power and cords",
         words=("electric*", "wiring", "wire pull*", "electrical panel*", "service panel*", "breaker panel*",
                "subpanel*", "sub panel*", "panelboard*", "load center*", "switchboard*", "temporary power",
                "temp power", "extension cord*", "cord", "cords", "gfci*", "receptacle*", "energized", "circuit*",
                "generator*", "power tool*", "switchgear", "conduit", "solar panel*", "photovoltaic*", "pv system*",
                "pv array*", "pv panel*"),
         rules=(HV_SURVEY, GFCI, AEGCP, AEGCP_TEST, ELEC_ESO, ELEC_QUALIFIED, ELEC_ENERGIZED, ELEC_GLOVES, ELEC_ARC,
                ELEC_LOCKOUT, ELEC_LOCK_REMOVAL, ELEC_TAG, ELEC_CONDUCTIVE),
         tools=()),
    dict(key="lockout", label="Servicing, repairing or unjamming machinery (lockout/blockout)",
         words=("lockout", "lock out", "loto", "tagout", "tag out", "blockout", "servicing", "service equipment",
                "repair equipment", "repair machine*", "equipment repair", "equipment maintenance", "unjam*",
                "clear jam*", "jammed", "machine maintenance", "service machine*", "machinery repair*",
                "clean out mixer*", "clean mixer*", "repair mixer*", "repair conveyor*", "conveyor repair*"),
         rules=(LOTO_SCOPE, LOTO_STOP, LOTO_REPAIR, LOTO_WRITTEN, LOTO_TAG, LOTO_GROUP, LOTO_SHIFT, LOTO_ANNUAL,
                LOTO_CONTRACTORS, LOTO_TRAIN),
         tools=()),
    dict(key="scissor_lifts", label="Scissor lifts and other elevating work platforms",
         words=("scissor*", "elevating work platform*", "ewp", "vertical lift*", "mast climb*", "mewp*",
                "vertical tower*"),
         rules=(AWP_SCOPE, AWP_MANUAL, AWP_PLATE, AWP_HV, AWP_RECORDS, AWP_GUARDRAIL, EWP_EQUIP, EWP_DRIVE, EWP_PREUSE,
                AWP_NO_CLIMB, EWP_WEATHER, EWP_TRAINING, EWP_MAST),
         tools=()),
    dict(key="boom_lifts", label="Boom lifts, bucket trucks and other aerial devices",
         words=("boom lift*", "aerial lift*", "aerial device*", "bucket truck*", "cherry picker*", "manlift*",
                "man lift*", "articulating boom*", "telescopic boom*", "mewp*", "aerial ladder*"),
         rules=(AWP_SCOPE, AWP_MANUAL, AWP_PLATE, AWP_HV, AWP_RECORDS, AWP_GUARDRAIL, AERIAL_OPS, AERIAL_TIEOFF,
                AERIAL_NO_BELT, AERIAL_SETUP, AERIAL_MOVE, AWP_NO_CLIMB),
         tools=("overhead_line_clearance",)),
    dict(key="forklifts", label="Forklifts and telehandlers (powered industrial trucks)",
         words=("forklift*", "fork lift*", "telehandler*", "lull", "skytrak", "reach truck*", "powered industrial truck*",
                "rough terrain forklift*"),
         rules=(PIT_TRAIN, PIT_METHOD, PIT_TOPICS, PIT_REFRESH, PIT_EVAL, PIT_CERT, PIT_AUTH, PIT_PRESHIFT, PIT_RULES,
                PIT_POST, PIT_PLATFORM, PIT_TELE),
         tools=()),
    dict(key="noise", label="Noise (jackhammers, saws, breakers, pile driving, compressors)",
         words=("jackhammer*", "jack hammer*", "chipping", "saw", "saws", "sawing", "saw cut*", "sawcut*",
                "chop saw*", "cutoff saw*", "cut off saw*", "concrete saw*", "circular saw*", "table saw*", "noise",
                "noisy", "loud", "pile driv*", "pavement breaker*", "paving breaker*", "concrete breaker*",
                "hydraulic breaker*", "rock breaker*", "hoe ram*", "air compressor*", "powder actuated",
                "demolition hammer*", "rotary hammer*", "concrete cut*"),
         rules=(NOISE_TABLE, NOISE_CONTROLS, NOISE_IMPACT, NOISE_EXEMPT),
         tools=()),
)
# Always on every construction site.
_ALWAYS = (
    dict(key="ppe", label="Personal protective equipment (every site)",
         rules=(PPE_USE, PPE_FIT, PPE_OWNED, PPE_HEAD, PPE_EYE, PPE_HAND, PPE_FOOT, PPE_BODY, PPE_NOT_GISO)),
    dict(key="fire_protection", label="Fire protection and extinguishers (every site)",
         rules=(FIRE_PROGRAM, FIRE_2A, FIRE_10B, FIRE_INSPECT)),
)
# Tasks covered by other TradeComply tools (pointers only).
_OTHER_TOOLS = (
    (("roof*", "fall*", "edge", "leading edge", "ladder*", "scaffold*", "steel erection", "skylight*", "floor opening*",
      "fram*"), "lookup_safety_standard (fall protection, ladders, scaffolds) and generate_safety_programs"),
    (("trench*", "excavat*", "shoring", "shore"), "check_cal_osha_permits (trench permit) and "
                                                  "lookup_safety_standard('excavation')"),
    (("silica", "concrete cut*", "core drill*", "grind*", "tuckpoint*", "sawcut*", "saw cut*"),
     "lookup_safety_standard('silica') and generate_safety_programs (silica exposure control plan)"),
    (("heat",), "generate_safety_programs (heat illness prevention plan)"),
    (("asbestos", "abatement", "lead paint", "lead based", "lead abatement", "lead work"),
     "check_cal_osha_permits (asbestos registration, lead notices)"),
    (("crane*",), "generate_safety_programs (crane operator certification and crane program)"),
)


def _matches(text: str, words) -> bool:
    for w in words:
        if w.endswith("*"):
            if re.search(rf"(?<![a-z0-9]){re.escape(w[:-1])}", text):
                return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text):
            return True
    return False


SiteRoleME = Literal["general_contractor", "subcontractor", "owner", "sole_proprietor"]
_ME_ROLES = {
    "general_contractor": ("gc", "general contractor", "general", "prime", "prime contractor", "controlling",
                           "controlling contractor", "controlling employer", "construction manager", "cm", "builder",
                           "design builder"),
    "subcontractor": ("sub", "subcontractor", "sub contractor", "trade", "trade contractor", "specialty contractor",
                      "contractor"),
    "owner": ("owner", "property owner", "owner builder", "host", "host employer", "client", "developer"),
    "sole_proprietor": ("sole proprietor", "sole prop", "solo", "self employed", "owner operator", "one man shop"),
}


def _me_role(value, field: str = "your_role", strict: bool = True) -> Optional[str]:
    w = _words(value or "subcontractor")
    for role, aliases in _ME_ROLES.items():
        if w == role.replace("_", " ") or w in aliases or w.removeprefix("the ") in aliases:
            return role
    if not strict:
        return None
    # 'subcontractor (electrical)', 'self-employed electrician': a whole-word alias inside the text.
    hits = [role for role, aliases in _ME_ROLES.items()
            if any(re.search(rf"\b{re.escape(a)}\b", w) for a in aliases + (role.replace("_", " "),) if len(a) > 3)]
    if set(hits) == {"general_contractor", "subcontractor"} and not re.search(r"\bsub", w):
        hits = ["general_contractor"]                   # 'contractor' alone is the generic sub alias
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"{field} must be general_contractor, subcontractor, owner or sole_proprietor (aliases like "
                     f"'GC', 'sub', 'CM' work); got {value!r}")


def _created_by(value, role: str) -> str:
    w = _words(value)
    if not w or w in ("unknown", "not sure", "none"):
        return "unknown"
    if w in ("us", "we", "our", "ours", "our crew", "me", "my crew", "self", "ourselves", "my company",
             "our company", "our workers", "my workers", "you"):
        return "us"
    if w in ("another employer", "another_employer", "another", "other", "others", "other employer",
             "another sub", "another subcontractor", "someone else", "them", "other trade", "another trade"):
        return "another_employer"
    other_role = _me_role(w, strict=False)
    if other_role:
        return "us" if other_role == role else "another_employer"
    raise ValueError(f"hazard_created_by must be 'us', 'another_employer' or 'unknown' (or a role like 'GC'); "
                     f"got {value!r}")


class HazardGroup(BaseModel):
    key: str
    label: str
    matched_tasks: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list, description="Written documents these rules require")
    see_also_tools: list[str] = Field(default_factory=list)
    next_step: str = ""
    verified: bool = False


class NoiseExposure(BaseModel):
    dba: Annotated[float, NoBool, Field(ge=0, le=200, allow_inf_nan=False,
                                        description="Sound level in dBA (slow response)")]
    hours: Annotated[float, NoBool, Field(gt=0, le=24, allow_inf_nan=False,
                                          description="Hours at that level during the shift")]


class NoiseDose(BaseModel):
    dose: Optional[float] = Field(None, description="Sum of C/T over the levels in Table N-1; over 1 exceeds the limit")
    exceeds_limit: Optional[bool] = None
    levels_above_table: list[float] = Field(default_factory=list)
    math: list[str] = Field(default_factory=list)


class Extinguishers(BaseModel):
    floor_area_sq_ft: Optional[float] = None
    floors: int
    per_floor: Optional[int] = Field(None, description="None under the single-family dwelling exception")
    total_2a: int
    single_family_dwelling_exception: bool = False
    math: str


class MultiEmployer(BaseModel):
    your_role: SiteRoleME
    hazard_created_by: Literal["us", "another_employer", "unknown"]
    your_workers_exposed: Optional[bool] = None
    categories: list[str] = Field(default_factory=list, description="Citable employer categories you may fall in")
    citable_without_own_workers_exposed: bool = False
    exposing_employer_defenses: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    next_step: str = ""
    verified: bool = False


class JobsiteChecklistResult(_Base):
    tasks: list[str]
    your_role: SiteRoleME
    groups: list[HazardGroup] = Field(default_factory=list)
    covered_by_other_tools: dict[str, str] = Field(default_factory=dict)
    unmatched_tasks: list[str] = Field(default_factory=list)
    documents_to_prepare: list[str] = Field(default_factory=list)
    noise_dose: Optional[NoiseDose] = None
    fire_extinguishers: Optional[Extinguishers] = None
    multi_employer: MultiEmployer


def _noise_dose(exposures) -> NoiseDose:
    rows = {int(r["dba"]): float(r["hours"]) for r in P(NOISE_TABLE, "table")}
    lo, hi = min(rows), max(rows)
    out, dose = NoiseDose(), 0.0
    if sum(e.hours for e in exposures) > 24:
        raise ValueError("noise_exposures hours add up to more than 24 in one shift")
    for e in exposures:
        level = math.ceil(round(e.dba, 6))
        if e.dba < lo:
            out.math.append(f"{_n(e.dba)} dBA for {_n(e.hours)} h: below {lo} dBA, the lowest Table N-1 level (not "
                            "counted)")
            continue
        if level > hi:
            out.levels_above_table.append(e.dba)
            out.math.append(f"{_n(e.dba)} dBA for {_n(e.hours)} h: above {hi} dBA, the highest Table N-1 level "
                            f"({_n(rows[hi])} h); no permitted time is listed")
            continue
        t = rows[level]
        frac = e.hours / t
        dose += frac
        out.math.append(f"{_n(e.dba)} dBA" + (f" (uses the {level} dBA row)" if level != e.dba else "")
                        + f" for {_n(e.hours)} h: {_n(e.hours)} / {_n(t)} h = {frac:.2f}")
    out.dose = round(dose, 2)
    out.exceeds_limit = True if (dose > 1 or out.levels_above_table) else False
    out.math.append(f"Dose = {out.dose:.2f} " + ("> 1 -> over the Table N-1 limit" if dose > 1 else
                                                 "<= 1 -> within Table N-1" if not out.levels_above_table else
                                                 "plus exposure above the table -> treat as over the limit"))
    return out


def _extinguishers(area: Optional[float], floors: int, single_family: bool = False) -> Extinguishers:
    per = P(FIRE_2A, "sq_ft_per_extinguisher")
    minf = P(FIRE_2A, "min_per_floor")
    travel = P(FIRE_2A, "max_travel_distance_ft")
    if single_family:
        n = P(FIRE_2A, "single_family_dwelling_min_on_site")
        return Extinguishers(floor_area_sq_ft=area, floors=floors, per_floor=None, total_2a=n,
                             single_family_dwelling_exception=True, math=(
                                 f"Single-family dwelling (not apartments, lodging houses, hotels or dormitories): "
                                 f"the 8 CCR 1922(a)(1) exception needs at least {n} 2A extinguisher (or the "
                                 f"equivalent) available on the job site during working hours, instead of one per "
                                 f"{_n(per)} sq ft and one per floor"))
    per_floor = max(minf, math.ceil(round(area / per, 9)))
    total = per_floor * floors
    return Extinguishers(floor_area_sq_ft=area, floors=floors, per_floor=per_floor, total_2a=total, math=(
        f"ceil({_n(area)} sq ft / {_n(per)} sq ft) = {per_floor} per floor (at least {minf}) x {floors} floor(s) = "
        f"{total} 2A extinguishers; add more if any point is over {_n(travel)} ft of travel from one"
        + ("; on a multi-story building put one next to the stairway on each floor" if floors > 1 else "")
        + ". A single-family dwelling needs only one on site: pass single_family_dwelling=true"))


def _group_next(key: str, extra: dict) -> str:
    if key == "overhead_lines":
        return ("Get each line's nominal voltage from the utility, run overhead_line_clearance for every machine and "
                "crew that could get close, and notify the line operator before any work inside the clearance.")
    if key == "confined_spaces":
        return ("Have a competent person survey the work area for confined spaces now and as work changes, and run "
                "confined_space_classification for each space before anyone enters.")
    if key == "wildfire_smoke":
        return (f"Check the PM2.5 AQI at the start of each shift; at {P(SMOKE_SCOPE, 'aqi_trigger_min')} or more, run "
                "wildfire_smoke_requirements for the crew.")
    if key == "demolition":
        return ("Have a qualified person complete the written engineering survey before anyone starts demolition and "
                "keep it on site for the whole project; check the Cal/OSHA demolition permit with "
                "check_cal_osha_permits.")
    if key == "cranes_boom_equipment":
        return (f"Post the high-voltage warning sign (\"{P(HV_SIGN, 'sign_text')}\") in the operator's view on each "
                "crane, derrick, drill rig or pile driver, and check overhead lines with overhead_line_clearance.")
    if key == "soil_disturbance":
        return extra.get("valley_fever_next") or ("Give the jobsite county to check the Valley Fever training rules "
                                                  "(valley_fever_requirements).")
    if key == "traffic_control":
        return ("Lay out traffic control per the California MUTCD, put workers on foot in ANSI/ISEA 107 garments "
                f"(retroreflective, visible at {_n(P(TRAFFIC_NIGHT, 'min_visibility_ft'))} ft at night), and train "
                f"flaggers on all {P(FLAG_TRAIN, 'topic_count')} topics before assigning them.")
    if key == "hot_work":
        bar = P(WELD_STORAGE, "barrier_alternative")
        return (f"Set up the hot-work fire prevention procedure, keep a {P(WELD_10BC, 'min_extinguisher_rating')} "
                f"extinguisher at fuel-gas work, and store oxygen {_n(P(WELD_STORAGE, 'min_separation_ft'))} ft from "
                f"fuel gas or behind a {_n(bar['min_height_ft'])} ft, {_n(bar['min_fire_resistance_hours'])}-hour "
                "noncombustible barrier.")
    if key == "electrical":
        amps = "/".join(str(a) for a in P(GFCI, "gfci_required_amps"))
        return (f"Put every temporary {P(GFCI, 'gfci_required_volts')} V {amps} A receptacle on GFCI protection (or "
                "run a written assured equipment grounding program), and survey for energized circuits before work "
                "starts.")
    if key == "lockout":
        return ("Write machine-specific energy control procedures, give each worker a personal lock, and inspect "
                f"each procedure at least every {P(LOTO_ANNUAL, 'interval_months_max')} months.")
    if key == "scissor_lifts":
        return (f"Inspect each lift before use, keep the manual on the unit, and allow only trained operators; "
                f"guardrails {_n(P(AWP_GUARDRAIL, 'guardrail_height_in'))} in (+/- "
                f"{_n(P(AWP_GUARDRAIL, 'tolerance_in'))} in) with a midrail.")
    if key == "boom_lifts":
        return (f"Everyone in the basket ties off to the boom or basket (never to an adjacent structure), with fall "
                f"arrest limiting free fall to {_n(P(AERIAL_TIEOFF, 'fall_arrest_max_free_fall_ft'))} ft; test the "
                "controls before use.")
    if key == "forklifts":
        return ("Check each operator's training certificate (name, training date, evaluation date, trainer) and that "
                f"the last evaluation is within {P(PIT_EVAL, 'evaluation_interval_years_max')} years; do the "
                "pre-shift check every shift.")
    if key == "noise":
        nd = extra.get("noise_dose")
        if nd is not None and nd.exceeds_limit:
            return ("Noise is over the Table N-1 limit: cut exposure with engineering or administrative controls "
                    "first (quieter tools, shorter time at the loud task), then provide and require hearing "
                    "protection.")
        return (f"Measure loud tasks: above {P(NOISE_TABLE, 'criterion_level_dba')} dBA for 8 hours (time halves every "
                f"{P(NOISE_TABLE, 'exchange_rate_db')} dB) use engineering or administrative controls first, then "
                "hearing protection.")
    if key == "ppe":
        return ("For each task, require the head, eye and face, hand and foot protection its hazards call for, "
                "fitted to each worker and used as the manufacturer instructs.")
    if key == "fire_protection":
        ex = extra.get("extinguishers")
        if ex is not None and ex.single_family_dwelling_exception:
            return (f"Keep at least {ex.total_2a} 2A extinguisher (or the equivalent) on the job site during working "
                    "hours, inspect it monthly and have it serviced at least yearly by a State Fire Marshal licensee.")
        if ex is not None:
            return (f"Set out at least {ex.total_2a} 2A extinguishers ({ex.per_floor} per floor), inspect them "
                    "monthly and have them serviced at least yearly by a State Fire Marshal licensee.")
        return (f"Set out one 2A extinguisher per {_n(P(FIRE_2A, 'sq_ft_per_extinguisher'))} sq ft (at least one per "
                f"floor), within {_n(P(FIRE_2A, 'max_travel_distance_ft'))} ft of travel; inspect monthly.")
    return ""


def _multi_employer(role: str, created: str, exposed: Optional[bool]) -> tuple[MultiEmployer, list[str]]:
    acc = _Acc()
    me = MultiEmployer(your_role=role, hazard_created_by=created, your_workers_exposed=exposed)
    acc.rule(ME_FOUR, "Four kinds of employer can be cited")
    cats = []
    if exposed and role != "sole_proprietor":          # a sole proprietor working alone has no employees to expose
        cats.append("exposing")
    if created == "us":
        cats.append("creating")
    if role == "general_contractor":
        cats.append("controlling")
    elif role == "owner":
        cats.append("controlling (only if you control site safety by contract or in practice)")
    me.categories = cats
    me.citable_without_own_workers_exposed = any(c.split(" ")[0] in ("creating", "controlling") for c in cats)
    if role == "sole_proprietor":
        acc.rule(ME_SOLE, "Sole proprietor working alone")
    if me.citable_without_own_workers_exposed:
        acc.rule(ME_NO_EXPOSURE, "Citable even if only other companies' workers were exposed")
    if role in ("general_contractor", "owner"):
        acc.rule(ME_CONTROLLING, "Controlling employer")
        acc.rule(ME_PRACTICE, "Control proven by actual practice")
        acc.rule(ME_SCHEDULE, "Control through scheduling or information")
        acc.rule(ME_FACTORS, "How your responsibility is weighed")
        acc.rule(ME_CONTRACTS, "Inspectors collect the contracts")
    if exposed and created != "us" and role not in ("general_contractor", "sole_proprietor"):
        # a controlling GC cannot meet them; a sole proprietor working alone is not an exposing employer
        d = core.rule(ME_DEFENSES)
        me.exposing_employer_defenses = [x.replace("_", " ") for x in d.param("defenses")]
        acc.req(f"Exposing employer: all {d.param('defense_count')} defenses needed to avoid a citation",
                f"{d.plain_english} If the hazard is extreme: {d.param('extreme_hazard_step')}.", ME_DEFENSES)
    acc.rule(ME_PENALTY, "Penalties count every exposed worker")
    me.requirements = acc.reqs
    me.verified = all(r.verified for r in acc.reqs)
    if role == "general_contractor":
        me.next_step = ("Walk the site daily and keep a written log of hazards found and the correction orders you "
                        "gave each sub (Cal/OSHA weighs your awareness, foreseeability and the reasonable steps you "
                        "took), and keep every subcontract's safety clauses on file.")
    elif role == "sole_proprietor":
        me.next_step = ("A sole proprietor is not an employee: if you are the only person exposed to a hazard, "
                        "Cal/OSHA has no citable violation for that exposure. Other companies' workers exposed to the "
                        "same hazard still count, and their employers and the GC can be cited for it, so fix or guard "
                        "hazards you create and report others to the GC. If you have employees, run this again as "
                        "subcontractor or general_contractor.")
    elif created == "us":
        me.next_step = ("Fix or guard the hazard your crew created now: you can be cited even if only other trades' "
                        "workers are exposed.")
    elif exposed:
        me.next_step = ("Tell the GC (and whoever created it) about the hazard in writing, protect and instruct your "
                        "crew around it, and pull them off that work if it is extreme and there is no other way to "
                        "protect them.")
    elif role == "owner":
        me.next_step = ("If you direct safety on site in practice, Cal/OSHA can treat you as the controlling "
                        "employer; set out in the contract who controls safety and act consistently with it.")
    else:
        me.next_step = ("Report hazards you see to the GC in writing and keep your crew away from hazards you did not "
                        "create; fix any you create.")
    return me, acc.ids


def jobsite_hazard_checklist(tasks, your_role: str = "subcontractor", county: Optional[str] = None,
                             hazard_created_by: Optional[str] = None, your_workers_exposed: Optional[bool] = None,
                             noise_exposures=None, floor_area_sq_ft=None, floors: int = 1,
                             as_of: Optional[str] = None,
                             single_family_dwelling: bool = False) -> JobsiteChecklistResult:
    """Requirements for each hazard in a list of jobsite tasks, plus PPE, fire protection and multi-employer duties."""
    if isinstance(tasks, str):
        tasks = [t for t in re.split(r"[,;\n]+", tasks)]
    if not isinstance(tasks, (list, tuple)):
        raise ValueError("tasks must be a list of task descriptions, e.g. ['scissor lift', 'welding']")
    bad = [t for t in tasks if t is not None and not isinstance(t, (str, int, float))]
    if bad:
        raise ValueError(f"each task must be a short text like 'scissor lift work', got {bad[0]!r}")
    clean = list(dict.fromkeys(re.sub(r"\s+", " ", str(t)).strip() for t in tasks if str(t or "").strip()))
    if not clean:
        raise ValueError("tasks must name at least one task, e.g. ['scissor lift work', 'torch cutting']")
    role = _me_role(your_role)
    created = _created_by(hazard_created_by, role)
    exposed = _bool("your_workers_exposed", your_workers_exposed)
    sfd = bool(_bool("single_family_dwelling", single_family_dwelling))
    county_name = _county(county) if str(county or "").strip() else None     # a bad county is an error, not ignored
    if as_of:
        _date(as_of, "as_of")
    area = _num("floor_area_sq_ft", floor_area_sq_ft, lo=0, kind="sq_ft")
    nfl = _num("floors", floors, lo=1)
    if nfl is None or nfl != int(nfl):
        raise ValueError(f"floors must be a whole number of 1 or more, got {floors!r}")
    nfl = int(nfl)
    if isinstance(noise_exposures, (str, NoiseExposure, dict)):
        if isinstance(noise_exposures, str):
            raise ValueError("noise_exposures must be a list of measurements like [{'dba': 100, 'hours': 2}], got "
                             f"{noise_exposures!r}")
        noise_exposures = [noise_exposures]
    exposures = []
    for e in noise_exposures or []:
        try:
            if isinstance(e, NoiseExposure):
                exposures.append(e)
            elif isinstance(e, dict):
                exposures.append(NoiseExposure(**{k: e[k] for k in ("dba", "hours") if k in e}))
            elif isinstance(e, (list, tuple)) and len(e) == 2:
                exposures.append(NoiseExposure(dba=e[0], hours=e[1]))
            else:
                raise ValueError(f"each noise exposure must be {{'dba': 100, 'hours': 2}}, got {e!r}")
        except ValidationError:
            raise ValueError(f"each noise exposure needs dba from 0 to 200 and hours more than 0 and at most 24, "
                             f"got {e!r}") from None

    acc = _Acc()
    texts = {t: _words(t) for t in clean}
    matched_any: set[str] = set()
    extra: dict = {}
    groups: list[HazardGroup] = []

    def build(key: str, label: str, rules, matched, tools=()) -> HazardGroup:
        g = HazardGroup(key=key, label=label, matched_tasks=list(matched), see_also_tools=list(tools))
        for rid in rules:
            r = core.rule(rid)
            g.requirements.append(_requirement(r.title or rid, r.plain_english or "", [rid]))
            if r.category == "required_document":
                g.documents.append(r.title or rid)
        return g

    for spec in _GROUPS:
        hit = [t for t, w in texts.items() if _matches(w, spec["words"])]
        if not hit:
            continue
        matched_any.update(hit)
        g = build(spec["key"], spec["label"], spec["rules"], hit, spec["tools"])
        if spec["key"] == "soil_disturbance":
            if county_name:
                vf = valley_fever_requirements(county_name, True, as_of=as_of)
                g.requirements = list(vf.requirements)
                extra["valley_fever_next"] = vf.next_step
                g.label += f" - {vf.county} County"
                if vf.highly_endemic is not True:
                    g.requirements.insert(0, _requirement("Valley Fever status", vf.summary,
                                                          [VF_TRAIN, VF_THRESHOLD]))
            else:
                g.requirements.append(_requirement(
                    "Valley Fever: check the county",
                    f"{core.rule(VF_TRAIN).plain_english} {core.rule(VF_THRESHOLD).plain_english}",
                    [VF_TRAIN, VF_THRESHOLD]))
        if spec["key"] == "noise" and exposures:
            nd = _noise_dose(exposures)
            extra["noise_dose"] = nd
            acc.math.extend(nd.math)
        if spec["key"] == "boom_lifts" or spec["key"] == "scissor_lifts":
            g.see_also_tools = list(dict.fromkeys(g.see_also_tools + ["overhead_line_clearance"]))
        groups.append(g)
    if exposures and "noise_dose" not in extra:                 # dose asked for without a noisy task name
        nd = _noise_dose(exposures)
        extra["noise_dose"] = nd
        acc.math.extend(nd.math)
        spec = next(s for s in _GROUPS if s["key"] == "noise")
        groups.append(build(spec["key"], spec["label"], spec["rules"], [], spec["tools"]))
    keys = {g.key for g in groups}
    if sfd or (area is not None and area > 0):
        extra["extinguishers"] = _extinguishers(area, nfl, sfd)
        acc.math.append(extra["extinguishers"].math)
    for spec in _ALWAYS:
        g = build(spec["key"], spec["label"], spec["rules"], [])
        if spec["key"] == "ppe" and keys & {"overhead_lines", "electrical"}:
            rows = P(PPE_HEAD, "class_rules")
            hv = next((r for r in rows if "greater than" in r["exposure"]), None)
            if hv:
                g.requirements.insert(0, _requirement(
                    "Hard hats near energized conductors",
                    f"Near {hv['exposure']}: Class {'/'.join(hv['classes'])} only; near conductors less than 600 "
                    f"volts: Class {'/'.join(next(r for r in rows if 'less than' in r['exposure'])['classes'])}.",
                    [PPE_HEAD]))
        if spec["key"] == "fire_protection" and "extinguishers" in extra:
            ex = extra["extinguishers"]
            g.requirements.insert(0, _requirement(f"2A extinguishers: {ex.total_2a}", ex.math, [FIRE_2A]))
        groups.append(g)
    for g in groups:
        g.next_step = _group_next(g.key, extra)
        g.verified = bool(g.requirements) and all(r.verified for r in g.requirements)
        for r in g.requirements:
            acc.use(*r.rule_ids)

    other: dict[str, str] = {}
    for t, w in texts.items():
        hints = [tool for words, tool in _OTHER_TOOLS if _matches(w, words)]
        if hints:
            other[t] = "; ".join(dict.fromkeys(hints))
    unmatched = [t for t in clean if t not in matched_any and t not in other]

    me, me_ids = _multi_employer(role, created, exposed)
    acc.use(*me_ids)
    docs = list(dict.fromkeys(d for g in groups for d in g.documents))
    res = JobsiteChecklistResult(tasks=clean, your_role=role, groups=groups, covered_by_other_tools=other,
                                 unmatched_tasks=unmatched, documents_to_prepare=docs,
                                 noise_dose=extra.get("noise_dose"), fire_extinguishers=extra.get("extinguishers"),
                                 multi_employer=me)
    task_groups = [g for g in groups if g.matched_tasks or g.key == "noise" and extra.get("noise_dose")]
    res.summary = (f"{len(clean)} task(s) -> {len(task_groups)} hazard group(s)"
                   + (f" ({_join([g.label for g in task_groups])})" if task_groups else "")
                   + ", plus PPE and fire protection for every site"
                   + (f"; {len(other)} task(s) are covered by other tools" if other else "")
                   + (f"; not recognized: {_join(unmatched)}" if unmatched else "") + ".")
    first = task_groups[0] if task_groups else groups[0]
    res.next_step = (f"First: {first.next_step}"
                     + (f" Then work through the other {len(groups) - 1} groups below." if len(groups) > 1 else ""))
    if unmatched:
        acc.notes.append("Tasks not recognized here: " + _join(unmatched) + ". Try search_california_rules or "
                         "lookup_safety_standard for them.")
    return acc.finish(res)


RULE_IDS = tuple(dict.fromkeys(
    v for k, v in list(globals().items()) if k.isupper() and isinstance(v, str) and v.startswith("osha2-")))


# ================================================================ MCP
def _tool_docs() -> dict:
    trig, vmax = P(SMOKE_SCOPE, "aqi_trigger_min"), P(SMOKE_VOL, "aqi_max")
    t1, t2 = P(HV_T1, "table"), P(HV_T2, "table")
    return {
        "smoke": (
            "Cal/OSHA wildfire smoke rule (8 CCR 5141.1) for one crew and shift. Give the current PM2.5 AQI (AirNow, "
            "CARB or your air district) or an on-site PM2.5 reading in ug/m3 (converted with Appendix A). Returns "
            f"whether the rule applies (AQI {trig}+ with smoke reasonably anticipated), the exemption that applies "
            f"(filtered buildings or vehicles, {P(SMOKE_EXEMPT, 'exempt_max_exposure_hours_per_shift')} hour or less "
            "in total per shift, wildland firefighting, on-site measurement below the trigger), the AQI category, "
            f"respirators (voluntary from {trig} to {vmax}; mandatory under 8 CCR 5144 above "
            f"{P(SMOKE_MAND, 'aqi_over')}, with the minimum assigned protection factor = PM2.5 / "
            f"{_n(P(SMOKE_MAND, 'max_inside_pm25_ug_m3'))} and the 5144 Table 1 respirator types that meet it; an "
            "N95 is APF " + _n(P(RESP_APF, "filtering_facepiece_apf")) + "), controls, training, the math, a next "
            "step and citations. Above AQI 500 give an on-site reading: the AQI alone does not give the PM2.5."),
        "lines": (
            "Minimum clearance from an energized overhead power line (8 CCR 2946 Tables 1 and 2) for a machine, "
            "crew, ladder, scaffold or stored material. voltage_kv: a number in kilovolts (12.47), or text with a "
            "unit ('12,470 V', '69 kV'), or 'unknown'. Table 1 (people, tools, materials, scaffolds): "
            + ", ".join(f"{r['min_clearance_ft']} ft to {_n(r['up_to_volts'] / 1000)} kV" for r in t1)
            + ". Table 2 (all boom-type equipment: cranes, boom trucks, concrete pumps, boom lifts and bucket "
              "trucks, excavators, telehandlers; and storage): "
            + ", ".join(f"{r['min_clearance_ft']} ft to {_n(r['up_to_volts'] / 1000)} kV" for r in t2)
            + ". Machines with no boom (scissor lifts, forklifts, drill rigs, pile drivers, dump trucks) are on "
              "Table 1 by the rule text; this tool applies Table 2 to them as a margin and reports both. Give "
              "planned_closest_distance_ft to check a setup. Also returns the 2947 warning sign, spotter, utility "
              "notice (2948), the 2949 exemption for line work by qualified electrical workers, math, next step and "
              "citations."),
        "cs": (
            "Classify one space under Cal/OSHA's construction confined-space rules (8 CCR 1950-1962, as amended "
            "effective 2026-01-01): confined space (3-part test), permit-required (hazardous atmosphere, engulfment, "
            f"converging walls, other serious hazard), meter readings against the limits (oxygen "
            f"{_n(P(CS_O2, 'oxygen_min_percent'))}-{_n(P(CS_O2, 'oxygen_max_percent'))}%, flammables over "
            f"{_n(P(CS_LFL, 'hazardous_if_greater_than'))}% LFL, combustible dust over "
            f"{_n(P(CS_DUST, 'hazardous_if_greater_than'))}% MEC, toxics over the PEL), entry status (no entry at "
            f"{_n(P(CS_NO_ENTRY_LFL, 'threshold_percent_LFL'))}% LFL or more, test first, alternate ventilation-only "
            "procedure, full permit entry), hot work, the mechanical retrieval device (vertical spaces over "
            f"{_n(P(CS_RETRIEVAL, 'mechanical_device_required_if_depth_greater_than_ft'))} ft), duties for your role "
            "(sub, GC/controlling contractor, owner/host), a next step and citations."),
        "vf": (
            "Valley Fever rules for a construction jobsite in one California county. Cal/OSHA lists "
            + _join(P(VF_TRAIN, "counties"))
            + f" as highly endemic (not exhaustive; more than {_n(P(VF_THRESHOLD, 'cases_per_100000_per_year_over'))} "
              "cases per 100,000 a year counts). Returns whether training is required, the topics, dust controls "
              "for soil-disturbing work, case reporting, and the reported annual training date (NOT verified: Labor "
              "Code 6709 could not be read), with citations. Accepts 'Kern', 'Kern County', 'County of Kern', 'SLO'."),
        "check": (
            "Jobsite hazard checklist from a list of tasks (e.g. ['scissor lift', 'torch cutting', 'flagging', "
            "'jackhammer', 'trenching']). Returns one group per hazard with the Cal/OSHA requirements (elevating "
            "work platforms and boom lifts, forklifts and telehandlers, hot work, demolition, traffic control and "
            "flaggers, noise, electrical and temporary power, lockout, overhead lines, cranes, confined spaces, "
            "wildfire smoke, Valley Fever by county), plus PPE and fire protection for every site, the written "
            "documents to prepare, an optional Table N-1 noise dose and 2A extinguisher count (with the "
            "single-family dwelling exception), and the "
            "multi-employer picture for your role (8 CCR 336.10-336.11: who can be cited, the five exposing-employer "
            "defenses). Tasks covered by other tools are pointed to them."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    nn = {"ge": 0, "allow_inf_nan": False}

    @mcp.tool(name="wildfire_smoke_requirements", annotations=ro, description=docs["smoke"])
    def _wildfire_smoke_requirements(
        aqi_pm25: Annotated[Optional[Union[int, float, str]], NoBool, Field(description=(
            "Current AQI for PM2.5 (AirNow 'NowCast', CARB or the air district), e.g. 175 or '500+'"))] = None,
        hours_exposed: Annotated[Optional[float], NoBool, Field(ge=0, le=24, allow_inf_nan=False, description=(
            "Total hours each worker will be exposed at this AQI during the shift"))] = None,
        work_setting: Annotated[str, Field(description=(
            "outdoors, enclosed_unfiltered, enclosed_building_filtered (mechanically filtered, openings closed) or "
            "enclosed_vehicle_filtered (cabin air filter, windows closed)"))] = "outdoors",
        pm25_ug_m3: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "On-site PM2.5 reading in ug/m3 from a direct-reading monitor used per Appendix A (overrides the AQI)"))]
        = None,
        emergency_operation: Annotated[bool, Field(description=(
            "Rescue/evacuation, or utility, communications or medical work directly aiding emergency or "
            "firefighting operations (not post-fire debris removal or rebuilding)"))] = False,
        wildland_firefighting: Annotated[bool, Field(description="Firefighters engaged in wildland firefighting")]
        = False,
        smoke_anticipated: Annotated[bool, Field(description=(
            "Workers may reasonably be anticipated to be exposed to wildfire smoke (false if the PM2.5 is not "
            "from a wildfire)"))] = True,
    ) -> WildfireSmokeResult:
        return wildfire_smoke_requirements(aqi_pm25, hours_exposed, work_setting, pm25_ug_m3, emergency_operation,
                                           wildland_firefighting, smoke_anticipated)

    @mcp.tool(name="overhead_line_clearance", annotations=ro, description=docs["lines"])
    def _overhead_line_clearance(
        voltage_kv: Annotated[Optional[Union[float, str]], NoBool, Field(description=(
            "Nominal phase-to-phase line voltage: a number in kV (12.47), or text like '12,470 V', '69 kV' or "
            "'12.47/7.2 kV', or 'unknown'"))],
        equipment: Annotated[str, Field(min_length=1, description=(
            "What will get close: crane, boom_truck, concrete_pump, hoist, aerial_lift, excavator, telehandler, "
            "drill_rig, pile_driver, forklift, scissor_lift, vehicle (dump truck, loader), ladder, scaffold, "
            "material, structure, worker, storage"))],
        planned_closest_distance_ft: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "Closest planned approach of any part (boom tip, load, rigging, person) to the line, in feet"))] = None,
        boom_lowered_in_transit_no_load: Annotated[bool, Field(description=(
            "Boom equipment travelling with the boom lowered and no load (Table 1 applies)"))] = False,
        work_over_lines: Annotated[bool, Field(description="Anything will be handled or swung over the line")]
        = False,
        line_deenergized_and_grounded: Annotated[bool, Field(description=(
            "The line owner verified it is de-energized AND it is visibly grounded at the work site"))] = False,
        qualified_electrical_worker_authorized_by_line_owner: Annotated[bool, Field(description=(
            "The work is building, maintaining or operating the line itself (or its poles and attachments) by "
            "qualified electrical workers the line owner authorized (8 CCR 2949). Not for other work near the "
            "line"))] = False,
        operator_can_see_clearance: Annotated[Optional[bool], Field(description=(
            "Can the operator easily see and keep the clearance? false requires a spotter"))] = None,
    ) -> OverheadLineResult:
        return overhead_line_clearance(voltage_kv, equipment, planned_closest_distance_ft,
                                       boom_lowered_in_transit_no_load, work_over_lines, line_deenergized_and_grounded,
                                       qualified_electrical_worker_authorized_by_line_owner,
                                       operator_can_see_clearance)

    @mcp.tool(name="confined_space_classification", annotations=ro, description=docs["cs"])
    def _confined_space_classification(
        space: Annotated[str, Field(min_length=1, description="The space, e.g. 'sewer manhole MH-3', 'attic plenum'")],
        can_bodily_enter: Annotated[bool, Field(description="Big enough for a worker to get fully inside")],
        limited_entry_or_exit: Annotated[bool, Field(description="Limited or restricted ways in or out")],
        designed_for_continuous_occupancy: Annotated[bool, Field(description=(
            "Designed for people to occupy continuously (a finished room is; a vault or tank is not)"))],
        oxygen_pct: Annotated[Optional[float], NoBool, Field(ge=0, le=100, allow_inf_nan=False, description=(
            "Oxygen reading, % by volume"))] = None,
        flammable_pct_lfl: Annotated[Optional[float], NoBool, Field(ge=0, le=100, allow_inf_nan=False, description=(
            "Flammable gas/vapor reading, % of LFL (LEL)"))] = None,
        toxic_could_exceed_pel: Annotated[Optional[bool], Field(description=(
            "Could a toxic substance (H2S, CO, solvent vapors...) exceed its PEL? null = not tested"))] = None,
        combustible_dust_pct_mec: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "Airborne combustible dust, % of its minimum explosive concentration"))] = None,
        atmosphere_could_become_hazardous: Annotated[bool, Field(description=(
            "The air could turn hazardous (sewer gas, vapors from coatings, adhesives or welding)"))] = False,
        engulfment_hazard: Annotated[bool, Field(description="Material that could engulf a worker")] = False,
        converging_walls_or_sloped_floor: Annotated[bool, Field(description=(
            "Inwardly converging walls or a floor sloping down to a smaller cross-section"))] = False,
        other_serious_hazard: Annotated[Optional[str], Field(description=(
            "Any other serious hazard, e.g. 'live electrical', 'moving auger'"))] = None,
        vertical_depth_ft: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "Depth of a vertical space, ft (mechanical retrieval device test)"))] = None,
        workers_will_enter: Annotated[bool, Field(description="Will your workers enter?")] = True,
        your_role: Annotated[str, Field(description=(
            "subcontractor (default), controlling_contractor (the GC) or host_employer (the owner)"))]
        = "subcontractor",
        hot_work_planned: Annotated[bool, Field(description="Welding, cutting or other ignition sources planned")]
        = False,
        ventilation_alone_keeps_safe: Annotated[Optional[bool], Field(description=(
            "Monitoring data show continuous forced-air ventilation alone keeps the space safe"))] = None,
        is_excavation_or_tunnel: Annotated[bool, Field(description=(
            "The work is an excavation/trench or tunnel (covered by other rules)"))] = False,
    ) -> ConfinedSpaceResult:
        return confined_space_classification(
            space, can_bodily_enter, limited_entry_or_exit, designed_for_continuous_occupancy, oxygen_pct,
            flammable_pct_lfl, toxic_could_exceed_pel, combustible_dust_pct_mec, atmosphere_could_become_hazardous,
            engulfment_hazard, converging_walls_or_sloped_floor, other_serious_hazard, vertical_depth_ft,
            workers_will_enter, your_role, hot_work_planned, ventilation_alone_keeps_safe, is_excavation_or_tunnel)

    @mcp.tool(name="valley_fever_requirements", annotations=ro, description=docs["vf"])
    def _valley_fever_requirements(
        county: Annotated[str, Field(min_length=1, description="County of the jobsite, e.g. 'Kern' or 'County of Kern'")],
        soil_disturbing_work: Annotated[bool, Field(description=(
            "Digging, grading, trenching, earthmoving or driving on unpaved ground"))] = True,
        county_cases_per_100k: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "The county's annual Valley Fever rate per 100,000 (CDPH), for counties not on Cal/OSHA's list"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> ValleyFeverResult:
        return valley_fever_requirements(county, soil_disturbing_work, county_cases_per_100k, as_of)

    @mcp.tool(name="jobsite_hazard_checklist", annotations=ro, description=docs["check"])
    def _jobsite_hazard_checklist(
        tasks: Annotated[list[str], Field(min_length=1, description=(
            "Tasks planned on the site, e.g. ['scissor lift', 'torch cutting', 'flagging on Main St']"))],
        your_role: Annotated[str, Field(description=(
            "general_contractor, subcontractor, owner or sole_proprietor ('GC', 'sub', 'CM' work)"))]
        = "subcontractor",
        county: Annotated[Optional[str], Field(description="Jobsite county, for Valley Fever on soil work")] = None,
        hazard_created_by: Annotated[Optional[str], Field(description=(
            "For a specific hazard: 'us', 'another_employer' or 'unknown'"))] = None,
        your_workers_exposed: Annotated[Optional[bool], Field(description=(
            "Are your own workers exposed to that hazard?"))] = None,
        noise_exposures: Annotated[Optional[list[NoiseExposure]], Field(description=(
            "Measured noise for one worker's shift: [{'dba': 100, 'hours': 2}, ...]"))] = None,
        floor_area_sq_ft: Annotated[Optional[float], NoBool, Field(**nn, description=(
            "Floor area per floor, sq ft (for the 2A extinguisher count)"))] = None,
        floors: Annotated[int, NoBool, Field(ge=1, description="Number of floors under construction")] = 1,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
        single_family_dwelling: Annotated[bool, Field(description=(
            "A single-family dwelling (not apartments, lodging houses, hotels or dormitories): one 2A extinguisher "
            "on site meets 8 CCR 1922(a)(1)"))] = False,
    ) -> JobsiteChecklistResult:
        return jobsite_hazard_checklist(tasks, your_role, county, hazard_created_by, your_workers_exposed,
                                        noise_exposures, floor_area_sq_ft, floors, as_of, single_family_dwelling)
