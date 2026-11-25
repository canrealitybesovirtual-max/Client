"""Disabled access for California construction: CBC Chapter 11B and 11A (as restated by LADBS and the City of San
Diego for the 2025 CBC, effective 2026-01-01), the federal ADA (28 CFR 35.151, 36.304, 36.401-36.406, 2010 ADA
Standards in 36 CFR part 1191) and the Fair Housing Act design rules (24 CFR 100.200-100.205).

  accessibility_triggers           project facts -> which accessibility rules apply (CBC 11B, CBC 11A, ADA Title III,
                                   ADA Title II, Fair Housing Act, CBC 11B-233 public housing), the unit counts they
                                   require, the federal elevator exemption, the 60% public-entrance count, who reviews
                                   what, and the federal penalty ceilings
  path_of_travel_obligation        alteration cost (+ the last 3 years of work on the same path) -> adjusted
                                   construction cost, the CBC 11B-202.4 Exception 8 test against the DSA valuation
                                   threshold, the 20% cap, the federal 20% disproportionality limit, a spending plan in
                                   the legal priority order and the local form (LADBS, San Diego DS-2123)
  accessible_parking_requirements  spaces per lot or garage -> required accessible and van-accessible spaces (ADA Table
                                   208.2 / CBC 11B-208, hospital and rehab percentages, CBC 11A multifamily rates),
                                   stall sizes, signs and pavement markings
  accessibility_field_check        tape-measure readings -> pass/fail against CBC 11B (doors, changes in level, ramps,
                                   curb ramps, site walkways, counters, turning space, parking stalls, vertical
                                   clearance, parking sign height) with the fix for each failure
  casp_lease_disclosure            landlord or tenant -> the Civil Code 1938 CASp statements for a commercial lease,
                                   the tenant's CASp rights, the $4 state disability-access fee on business licenses
                                   and, for homes, the Fair Housing Act reasonable-modification rules

Every legal number, threshold and date comes from core.rule(id).param(key) at call time, and every rule a result
relies on is cited through core.Result.cite(), so `verified` is True only when all of them were verified. The pack is
research/web_accessibility.verified.json (rule ids "ada-..."). Its one unverifiable rule (the "no less than 20%"
spend when a hardship is granted above the valuation threshold) is used only when hardship_requested=True, and then
the result is verified=False.

Known gaps, stated in results instead of guessed: the CBC text itself was not readable (CBC values are the LADBS and
San Diego restatements); CBC Table 11B-208.2 was not read (the federal Table 208.2 is used as the floor); the CBC
"covered multifamily dwelling" unit count was not read; the valuation threshold is known only for the calendar years
the rulebook lists (2026: $209,208); the CBC 11B-202.4 exceptions other than Exception 8 were not read.

Not legal values (so not in the rulebook): the spellings below that map a user's words (work types, uses, cities,
parking facility kinds, element names) onto the rules.
"""
import math
import re
from datetime import date, datetime
from fractions import Fraction
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
# CBC 11B alterations and path of travel (11B-202.3, 11B-202.4 Exception 8)
ALTERED, POT_REQ, POT_ELEMENTS = ("ada-cbc-11b-202-3-altered-elements-comply",
                                  "ada-cbc-11b-202-4-path-of-travel-required",
                                  "ada-cbc-11b-202-4-primary-path-elements")
THRESHOLD, CAP20, ABOVE = ("ada-cbc-valuation-threshold-2026", "ada-cbc-11b-202-4-exc8-20-percent-cap",
                           "ada-cbc-11b-202-4-exc8-above-threshold")
ADJ_EXCL, LOOKBACK, PRIORITY = ("ada-cbc-11b-202-4-exc8-adjusted-cost-excludes-pot",
                                "ada-cbc-11b-202-4-exc8-three-year-lookback", "ada-cbc-11b-202-4-exc8-priority-order")
LA_FORM, SD_FORM, SD_RATIFY = ("ada-ladbs-summary-of-accessibility-upgrades-form", "ada-sd-ds-2123-exception-8-request",
                               "ada-sd-hardship-appeals-ratification")
ANNUAL, RESTRIPE, PUB_MF = ("ada-cbc-valuation-threshold-annual-adjustment",
                            "ada-cbc-parking-resurfacing-restriping-no-pot", "ada-cbc-publicly-funded-multifamily-11b")
# CBC 11B dimensions
LEVEL, TURN = "ada-cbc-11b-303-changes-in-level", "ada-cbc-11b-304-turning-space"
DOOR = "ada-cbc-11b-404-door-clear-width"
RAMP, WALK = "ada-cbc-11b-405-ramps", "ada-cbc-11b-403-site-walkway"
VERT200, COUNTER, CURB = ("ada-cbc-11b-206-2-3-2-vertical-access-200-feet", "ada-cbc-11b-904-4-sales-counter",
                          "ada-cbc-11b-406-curb-ramps")
# CBC 11B parking
PK_FACILITY, PK_VAN6, PK_STALL = ("ada-cbc-11b-208-parking-per-facility", "ada-cbc-11b-208-van-accessible-1-in-6",
                                  "ada-cbc-11b-502-stall-dimensions")
PK_AISLE_MARK, PK_SIGN, PK_TOW = ("ada-cbc-11b-502-3-access-aisle-marking",
                                  "ada-cbc-11b-502-6-parking-signs-minimum-fine", "ada-cbc-11b-502-8-tow-away-sign")
PK_VERT, PK_SURFACE = "ada-cbc-11b-502-5-vertical-clearance", "ada-cbc-11b-502-6-4-surface-isa"
# CBC 11B public housing
PH_UNITS, PH_DEF = "ada-cbc-11b-233-public-housing-units", "ada-cbc-public-housing-definition"
# CBC 11A covered multifamily
A_APPLY, A_MAINTAIN = "ada-cbc-11a-1101a-application", "ada-cbc-11a-1102a-2-maintain-as-built-standards"
A_MULTI10, A_MULTI_ELEV = ("ada-cbc-11a-1102a-3-1-multistory-no-elevator-10-percent",
                           "ada-cbc-11a-1102a-3-2-multistory-with-elevator")
A_GROUND, A_PK, A_PK_DIM = ("ada-cbc-11a-1104a-ground-floor-units-adaptable", "ada-cbc-11a-1109a-parking-percentages",
                            "ada-cbc-11a-1109a-8-parking-dimensions")
A_VAN8, A_DOORS, A_TEMP = ("ada-cbc-11a-1109a-8-6-van-1-in-8", "ada-cbc-11a-1126a-doors",
                           "ada-cbc-11a-1102a-6-temporary-restrictions")
A_SITE, A_IMPRACT = "ada-cbc-11a-1110a-site-accessible-route", "ada-cbc-11a-1150a-site-impracticality"
PLAN_CHECK = "ada-local-plan-check-not-ada-fha-review"
# CASp, Civil Code 1938, business license fee
CASP, LEASE_STATUS, LEASE_STMT = ("ada-casp-definition-and-services", "ada-civ-1938-casp-status-in-lease",
                                  "ada-civ-1938e-casp-statement")
FEE4, AB3002 = "ada-casp-fee-business-license-4-dollars", "ada-ab3002-business-license-access-notice"
# ADA Title III (28 CFR part 36)
F_ALTER, F_WHAT, F_POT = ("ada-28cfr36-402-altered-portions-accessible", "ada-28cfr36-402-what-is-an-alteration",
                          "ada-28cfr36-403-path-of-travel-obligation")
F_PRIMARY, F_MINOR, F_TENANT = ("ada-28cfr36-403b-primary-function-definition",
                                "ada-28cfr36-403c2-minor-work-no-trigger", "ada-28cfr36-403d-landlord-tenant")
F_20, F_COSTS, F_PRIORITY = ("ada-28cfr36-403f-20-percent-disproportionality", "ada-28cfr36-403f2-countable-costs",
                             "ada-28cfr36-403g-priority-order")
F_SERIES, F_2010_DATE, F_NEW = ("ada-28cfr36-403h-series-of-small-alterations",
                                "ada-28cfr36-406-2010-standards-compliance-date",
                                "ada-28cfr36-401-new-construction-first-occupancy")
F_IMPRACT, F_ELEV, F_MAX_FEAS = ("ada-28cfr36-401c-structural-impracticability", "ada-28cfr36-404-elevator-exemption",
                                 "ada-28cfr36-402c-maximum-extent-feasible")
F_BARRIER, F_BARRIER_NO_POT, F_PENALTY = ("ada-28cfr36-304-barrier-removal",
                                          "ada-28cfr36-304d-barrier-removal-no-path-of-travel",
                                          "ada-title-iii-civil-penalty-2025")
# ADA Title II (28 CFR 35.151)
T2_20, T2_SERIES, T2_PROGRAM = ("ada-28cfr35-151-title-ii-20-percent", "ada-28cfr35-151-title-ii-series-3-years",
                                "ada-28cfr35-151b2-program-access-no-pot")
T2_2010_DATE = "ada-28cfr35-151c-title-ii-2010-standards-date"
F_SAFE, T2_SAFE = "ada-28cfr36-403a2-1991-standards-safe-harbor", "ada-28cfr35-151-title-ii-safe-harbor"
# 2010 ADA Standards (36 CFR part 1191)
S_SITES, S_NO_REDUCE, S_RES_EXC = ("ada-2010-std-203-2-construction-sites-exempt",
                                   "ada-2010-std-202-3-1-no-reduction-in-access",
                                   "ada-2010-std-202-4-residential-exception")
S_PK_TABLE, S_VAN, S_PK_DIM = ("ada-2010-std-208-2-parking-table", "ada-2010-std-208-2-4-van-spaces",
                               "ada-2010-std-502-2-parking-dimensions")
S_ENTRANCES, S_STEEP = ("ada-2010-std-206-4-1-public-entrances-60-percent",
                         "ada-2010-std-405-2-existing-site-steeper-ramps")
# Fair Housing Act (24 CFR 100.200-100.205, 180.671; 28 CFR 85.5)
FHA_COVERED, FHA_FIRST_DEF, FHA_DATE = ("ada-fha-covered-multifamily-definition",
                                        "ada-fha-first-occupancy-and-ground-floor",
                                        "ada-fha-first-occupancy-after-1991-03-13")
FHA_ENTRANCE, FHA_COMMON, FHA_UNIT = ("ada-fha-accessible-building-entrance", "ada-fha-public-common-areas-and-doors",
                                      "ada-fha-adaptable-unit-features")
FHA_SAFE, FHA_LOCAL, FHA_MODS = ("ada-fha-safe-harbors", "ada-fha-local-approval-not-conclusive",
                                 "ada-fha-reasonable-modifications")
FHA_HUD, FHA_DOJ = "ada-fha-hud-civil-penalties", "ada-fha-doj-civil-penalties"

RULE_IDS = (ALTERED, POT_REQ, POT_ELEMENTS, THRESHOLD, CAP20, ABOVE, ADJ_EXCL, LOOKBACK, PRIORITY, LA_FORM, SD_FORM,
            SD_RATIFY, ANNUAL, RESTRIPE, PUB_MF, LEVEL, TURN, DOOR, RAMP, WALK, VERT200, COUNTER, CURB, PK_FACILITY,
            PK_VAN6, PK_STALL, PK_AISLE_MARK, PK_SIGN, PK_TOW, PK_VERT, PK_SURFACE, PH_UNITS, PH_DEF, A_APPLY,
            A_MAINTAIN, A_MULTI10, A_MULTI_ELEV, A_GROUND, A_PK, A_PK_DIM, A_VAN8, A_DOORS, A_TEMP, A_SITE, A_IMPRACT,
            PLAN_CHECK, CASP, LEASE_STATUS, LEASE_STMT, FEE4, AB3002, F_ALTER, F_WHAT, F_POT, F_PRIMARY, F_MINOR,
            F_TENANT, F_20, F_COSTS, F_PRIORITY, F_SERIES, F_2010_DATE, F_NEW, F_IMPRACT, F_ELEV, F_MAX_FEAS,
            F_BARRIER, F_BARRIER_NO_POT, F_PENALTY, T2_20, T2_SERIES, T2_PROGRAM, T2_2010_DATE, F_SAFE, T2_SAFE,
            S_SITES, S_NO_REDUCE,
            S_RES_EXC, S_PK_TABLE, S_VAN, S_PK_DIM, S_ENTRANCES, S_STEEP, FHA_COVERED, FHA_FIRST_DEF, FHA_DATE,
            FHA_ENTRANCE, FHA_COMMON, FHA_UNIT, FHA_SAFE, FHA_LOCAL, FHA_MODS, FHA_HUD, FHA_DOJ)

EPS = 1e-9                      # float comparison slack, not a legal value


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _n(x: float) -> str:
    """Number for display: thousands separators, at most 3 decimals, no trailing zeros (2.083, 8.333, 1.76)."""
    return f"{x:,.3f}".rstrip("0").rstrip(".")


def _ftin(inches: float) -> str:
    """98 -> '8 ft 2 in' (unit conversion only)."""
    ft, rest = divmod(round(inches, 3), 12)
    return f"{int(ft)} ft" + (f" {_n(rest)} in" if rest else "")


def _years_in(key: str) -> str:
    """'max_usd_one_prior_within_5_years' -> '5' (the look-back is part of the rulebook's parameter name)."""
    m = re.search(r"within_(\d+)_years", key)
    if not m:
        raise RuntimeError(f"no look-back years in parameter name {key!r}")
    return m.group(1)


def _cap(s: str) -> str:
    """Capitalize the first letter only ('no parking' stays as written elsewhere)."""
    return s[:1].upper() + s[1:]


def _usd_s(x: float) -> str:
    s = f"{x:,.2f}"
    return "$" + (s[:-3] if s.endswith(".00") else s)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _pct(fraction: float) -> str:
    return f"{_n(fraction * 100)}%"


def _join(items, last: str = "and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def _words(text) -> str:
    """Lowercase, punctuation to spaces, single spaces: 'City of L.A.' -> 'city of l a'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def _has(w: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", w) is not None


def _ceil_frac(fraction, n: int) -> int:
    """ceil(fraction x n) without float error (0.02 x 550 is exactly 11, not 11.000000000000002)."""
    return math.ceil(Fraction(str(fraction)) * n)


def _ratio(text: str) -> float:
    """'1:12' -> 1/12 (a rule's slope written as rise:run)."""
    a, b = (float(x) for x in str(text).split(":"))
    return a / b


def _divisor(formula: str) -> int:
    """The N in a rulebook formula like 'van_spaces = max(1, ceil(required_accessible / 6))'."""
    m = re.search(r"/\s*(\d+)", formula)
    if not m:
        raise RuntimeError(f"rulebook formula has no divisor: {formula!r}")
    return int(m.group(1))


def _plus_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:                      # Feb 29 -> Feb 28
        return d.replace(year=d.year + n, day=28)


def _date(value, field: str, default: Optional[date] = None) -> date:
    """ISO 2026-11-25 or US 11/25/2026; blank -> default (error if no default)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValueError(f"{field} is required (a date like 2026-11-25)")
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ValueError(f"{field} must be a date like 2026-11-25 (or 11/25/2026), got {value!r}") from None


def _num(name: str, value) -> Optional[float]:
    """A non-negative number from 4500, '4,500' or '4,500 sq ft' (unit words ignored); None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.fullmatch(r"\s*(-?\d*\.?\d+)\s*[a-z .]*", str(value).replace(",", "").lower())
        if not m:
            raise ValueError(f"{name} must be a number, got {value!r}")
        v = float(m.group(1))
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} must be a number of 0 or more, got {value!r}")
    return v


def _usd(name: str, value) -> Optional[float]:
    """Dollars from 150000, '$150,000', '150k', '1.2M'; None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a dollar amount, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        s = re.sub(r"[\s,$]|usd", "", str(value).strip().lower())
        m = re.fullmatch(r"(\d*\.?\d+)(k|m)?", s)
        if not m:
            raise ValueError(f"{name} must be a dollar amount like 150000, '$150,000' or '150k', got {value!r}")
        v = float(m.group(1)) * {"k": 1_000, "m": 1_000_000}.get(m.group(2) or "", 1)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite dollar amount, got {value!r}")
    if v < 0:
        raise ValueError(f"{name} cannot be negative, got {value!r}")
    return v


def _count(name: str, value) -> Optional[int]:
    """A whole number from 120, 120.0, '120' or '120 spaces'; None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[a-z .]*", str(value).replace(",", "").lower())
        if not m:
            raise ValueError(f"{name} must be a whole number, got {value!r}")
        v = float(m.group(1))
    if not math.isfinite(v) or v != int(v):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    if v < 0:
        raise ValueError(f"{name} cannot be negative, got {value!r}")
    return int(v)


def _bool(name: str, value) -> Optional[bool]:
    """True/False, or 'yes'/'no'/'y'/'n'/'true'/'false'/'1'/'0'; None (or 'unknown') stays None."""
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


def _numtok(t: str) -> Optional[float]:
    """'2.5' / '1 1/2' / '3/4' -> float, else None."""
    t = t.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)(?:\s+(\d+)\s*/\s*(\d+))?", t)
    if m:
        v = float(m.group(1))
        if m.group(2):
            if not int(m.group(3)):
                return None
            v += int(m.group(2)) / int(m.group(3))
        return v
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", t)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    return None


def _length(name: str, value, unit: str = "in") -> Optional[float]:
    """Inches from 32, '32 in', '32"', "2'-8\"", '2 ft 8 in', '3/4"', '1-1/2 in' or '9 ft'. Bare numbers are in `unit`
    ('in' or 'ft'); None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    bad = ValueError(f"{name} must be a length like 32, '32 in', '2\\'-8\"' or '9 ft', got {value!r}")
    if isinstance(value, bool):
        raise bad
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"{name} must be a length of 0 or more, got {value!r}")
        return v * (12 if unit == "ft" else 1)
    s = str(value).strip().lower()
    for a, b in (("”", '"'), ("“", '"'), ("″", '"'), ("’", "'"), ("‘", "'"), ("′", "'")):
        s = s.replace(a, b)
    s = s.replace(",", "")
    s = re.sub(r"\b(inches|inch|ins)\b", "in", s)
    s = re.sub(r"\b(feet|foot)\b", "ft", s)
    s = re.sub(r"(\d)-(\d+\s*/\s*\d+)", r"\1 \2", s)           # 1-1/2 -> 1 1/2
    feet, rest, has_ft = 0.0, s, False
    m = re.fullmatch(r"(.*?)\s*(?:'|\bft\b)\s*-?\s*(.*)", s)
    if m and m.group(1).strip():
        f = _numtok(m.group(1))
        if f is None:
            raise bad
        feet, rest, has_ft = f, m.group(2), True
    rest = rest.strip()
    has_in = bool(re.search(r'("|\bin)$', rest))
    rest = re.sub(r'\s*("|\bin)$', "", rest).strip()
    if not rest:
        if not has_ft:
            raise bad
        return feet * 12
    inches = _numtok(rest)
    if inches is None:
        raise bad
    if not has_ft and not has_in and unit == "ft":
        return inches * 12
    return feet * 12 + inches


def _slope(name: str, value) -> Optional[float]:
    """Rise/run from a percent (8.33, '8.33%') or a ratio ('1:12', '1 in 12'); None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    bad = ValueError(f"{name} must be a percent (8.33 or '8.33%') or a ratio like '1:12', got {value!r}")
    if isinstance(value, bool):
        raise bad
    if isinstance(value, (int, float)):
        v = float(value) / 100
    else:
        s = str(value).strip().lower()
        m = re.fullmatch(r"(\d*\.?\d+)\s*%?", s)
        r = re.fullmatch(r"(\d*\.?\d+)\s*(?::|in|to)\s*(\d*\.?\d+)", s)
        if m:
            v = float(m.group(1)) / 100
        elif r and float(r.group(2)) > 0 and float(r.group(1)) <= float(r.group(2)):
            v = float(r.group(1)) / float(r.group(2))
        else:
            raise bad
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} must be a slope of 0 or more, got {value!r}")
    return v


def _slope_s(v: float) -> str:
    return f"{_pct(v)} (1:{_n(1 / v)})" if v > 0 else "0% (level)"


# ---------------------------------------------------------------- input spellings (not law)
_LA = {"la", "l a", "los angeles", "losangeles", "ladbs", "la dbs", "lacity"}
_SD = {"sd", "s d", "san diego", "sandiego", "sddsd", "sd dsd"}
_FILLER = {"city", "of", "the", "ca", "california", "department", "dept", "building", "and", "safety", "development",
           "services", "service", "dsd", "permit", "permits", "plan", "check", "office"}


def _jurisdiction(value) -> tuple[str, str, Optional[str]]:
    """-> (key, label, county_note). key is 'los_angeles' (LADBS), 'san_diego' (City of San Diego DSD) or 'other'.
    'LA', 'L.A.', 'City of Los Angeles', 'LADBS' -> los_angeles; 'SD', 'City of San Diego' -> san_diego; a county
    ('LA County', 'County of San Diego') is not the city and gets 'other' with a note."""
    w = _words(value)
    if not w:
        return "other", "not given", None
    toks = w.split()
    county = any(t in ("county", "unincorporated") for t in toks)
    core_ = " ".join(t for t in toks if t not in _FILLER and t not in ("county", "unincorporated"))
    for key, names, city, agency in (("los_angeles", _LA, "Los Angeles", "LADBS"),
                                     ("san_diego", _SD, "San Diego", "Development Services Department")):
        if core_ in names:
            if county:
                return "other", f"County of {city}", (
                    f"County of {city} is not the City of {city}: the {agency} forms below apply only to work that "
                    f"agency permits. Use your building department's Exception 8 / unreasonable-hardship form.")
            return key, f"City of {city} ({agency})", None
    return "other", str(value).strip(), None


WorkType = Literal["alteration", "structural_repair", "addition", "barrier_removal", "program_access",
                   "parking_resurfacing_restriping", "minor_work_only", "maintenance_only"]
_WORK = (  # first match wins, so the most demanding reading of "remodel and repaint" is used
    ("alteration", ("alteration", "alterations", "alter", "remodel", "remodeling", "renovation", "renovate",
                    "rehabilitation", "rehab", "reconstruction", "tenant improvement", "tenant improvements", "ti",
                    "t i", "build out", "buildout", "historic restoration")),
    ("structural_repair", ("structural repair", "structural repairs", "structural")),
    ("addition", ("addition", "additions", "add on", "expansion")),
    ("barrier_removal", ("barrier removal", "barrier", "readily achievable")),
    ("program_access", ("program access", "program accessibility")),
    ("parking_resurfacing_restriping", ("resurfacing", "resurface", "restriping", "restripe", "re striping",
                                        "re stripe", "slurry seal", "parking lot")),
    ("minor_work_only", ("minor work", "minor", "windows", "window", "hardware", "controls", "electrical outlets",
                         "outlets", "signage")),
    ("maintenance_only", ("maintenance", "normal maintenance", "reroofing", "reroof", "re roof", "roofing",
                          "painting", "paint", "repaint", "wallpapering", "wallpaper", "asbestos removal",
                          "asbestos abatement", "mechanical", "electrical", "hvac")),
)


def _work_type(value) -> str:
    w = _words(value or "alteration")
    if w.replace(" ", "_") in {k for k, _ in _WORK}:
        return w.replace(" ", "_")
    for key, phrases in _WORK:
        if any(_has(w, p) for p in phrases):
            return key
    raise ValueError("work_type must be alteration (remodel, renovation, tenant improvement), structural_repair, "
                     "addition, barrier_removal, program_access, parking_resurfacing_restriping, minor_work_only "
                     "(windows, hardware, controls, outlets, signage) or maintenance_only (reroofing, painting, "
                     f"asbestos removal, mechanical/electrical); got {value!r}")


Facility = Literal["public_accommodation", "commercial_facility", "public_entity", "publicly_funded_multifamily"]
_FACILITY = (
    ("publicly_funded_multifamily", ("publicly funded multifamily", "publicly funded", "public housing",
                                     "affordable housing", "multifamily", "multi family", "apartments", "apartment",
                                     "condo", "condos")),
    ("public_entity", ("public entity", "public building", "government", "public agency", "title ii", "city hall",
                       "school district", "county building", "state building", "municipal")),
    ("public_accommodation", ("public accommodation", "place of public accommodation", "retail", "store", "shop",
                              "restaurant", "bank", "hotel", "title iii")),
    ("commercial_facility", ("commercial facility", "commercial", "office", "warehouse", "industrial", "factory")),
)


def _facility(value) -> str:
    w = _words(value or "public_accommodation")
    if w.replace(" ", "_") in {k for k, _ in _FACILITY}:
        return w.replace(" ", "_")
    if w in ("city", "county", "state", "public", "agency", "special district", "school district", "title 2"):
        return "public_entity"                              # whole answers only: "city" inside other words is not
    if any(_has(w, p) for p in ("multifamily", "multi family", "apartment", "apartments", "condo", "condos")) and not \
            any(_has(w, p) for p in ("public", "publicly", "funded", "affordable")):
        raise ValueError("Privately funded multifamily housing is under CBC Chapter 11A, not the CBC 11B-202.4 "
                         "path-of-travel rule: run accessibility_triggers. For publicly funded multifamily or public "
                         "housing use facility publicly_funded_multifamily.")
    for key, phrases in _FACILITY:
        if any(_has(w, p) for p in phrases):
            return key
    raise ValueError("facility must be public_accommodation (store, restaurant, bank, hotel...), commercial_facility "
                     "(office, warehouse, factory), public_entity (a state or local government building) or "
                     f"publicly_funded_multifamily; got {value!r}")


_SLOTS = ("entrance", "route", "restroom", "telephones", "drinking_fountains", "other")
_SLOT_ALIASES = {
    "entrance": ("entrance", "entry", "primary entrance", "front door", "entrances"),
    "route": ("route", "accessible route", "path", "route to the altered area", "corridor", "corridors", "doors",
              "hallway"),
    "restroom": ("restroom", "restrooms", "toilet", "toilets", "toilet rooms", "bathroom", "bathrooms", "unisex"),
    "telephones": ("telephone", "telephones", "phone", "phones", "public telephones"),
    "drinking_fountains": ("drinking fountain", "drinking fountains", "fountain", "fountains", "water fountain"),
    "other": ("other", "parking", "storage", "alarms", "alarm", "signs", "signage", "fire alarm"),
}


def _slot(key) -> str:
    w = _words(key)
    if w.replace(" ", "_") in _SLOTS:
        return w.replace(" ", "_")
    for slot, names in _SLOT_ALIASES.items():
        if w in names:
            return slot
    raise ValueError(f"element_costs_usd key {key!r} is not a path-of-travel element; use entrance, route, restroom, "
                     "telephones, drinking_fountains or other (parking, storage, alarms, signs)")


# ---------------------------------------------------------------- shared models
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

    def req(self, item: str, detail: str, *ids: str) -> Requirement:
        self.use(*ids)
        return _requirement(item, detail, ids)

    def rule(self, rid: str, item: Optional[str] = None, extra: str = "") -> Requirement:
        """A requirement worded as the rulebook's plain-English text for that rule."""
        r = core.rule(rid)
        return self.req(item or r.title or rid, (r.plain_english or "") + (f" {extra}" if extra else ""), rid)

    def finish(self, res):
        res.cite(*self.ids)
        res.unverified_rule_ids = [i for i in self.ids if not core.rule(i).verified]
        res.notes = self.notes + [n for n in res.notes if n not in self.notes]
        for i in res.unverified_rule_ids:
            r = core.rule(i)
            res.notes.append(f"NOT VERIFIED ({r.status}): {r.title} [{r.citation_text}]. "
                             + (r.notes or "The primary text could not be read.")
                             + " Confirm with the building department or your architect before relying on it.")
        res.math = self.math + [m for m in res.math if m not in self.math]
        return res


class _Base(core.Result):
    summary: str = ""
    next_step: str = ""
    math: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ================================================================ path of travel (11B-202.4 Exc. 8, 28 CFR 36.403)
def _thresholds() -> dict[int, float]:
    """Calendar year -> DSA valuation threshold, for every year the rulebook dates (the current year plus any dated
    prior value)."""
    r = core.rule(THRESHOLD)
    known = {int(r.param("calendar_year")): float(r.param("valuation_threshold_usd"))}
    for p in r.param("prior_values_seen_usd", []) or []:
        if isinstance(p, dict) and p.get("year") and p.get("value") is not None:
            known.setdefault(int(p["year"]), float(p["value"]))
    return known


class PriorAlteration(BaseModel):
    date: str = Field(description="ISO date of the earlier permit or work, e.g. 2025-03-10")
    cost_usd: Union[float, str] = Field(description="Construction cost of that earlier alteration")
    provided_accessible_path_of_travel: bool = Field(False, description=(
        "True if that job provided (paid for) an accessible path of travel; then it is not added"))
    same_path_of_travel: bool = Field(True, description=(
        "True if it altered this area or another area on the same path of travel"))
    description: Optional[str] = None


class PriorLine(BaseModel):
    date: str
    cost_usd: float
    counted_california: bool
    counted_federal: bool
    reason: str


class CaliforniaPathOfTravel(BaseModel):
    status: Literal["capped_20_percent", "full_compliance", "threshold_unknown_for_year", "not_triggered"]
    adjusted_construction_cost_usd: float
    valuation_threshold_usd: Optional[float] = None
    threshold_year: int
    cap_fraction: float
    twenty_percent_of_adjusted_cost_usd: float = Field(description=(
        "cap_fraction x adjusted construction cost: the Exception 8 cap when at or below the threshold"))
    max_required_spend_usd: Optional[float] = Field(None, description="The Exception 8 cap, when it applies")
    required_spend_usd: Optional[float] = Field(None, description=(
        "What must be spent: min(full path-of-travel cost, cap) under the cap, or the full cost above the threshold "
        "(None when the full cost was not given)"))
    hardship_min_spend_usd: Optional[float] = Field(None, description=(
        "Only with hardship_requested above the threshold (rule NOT verified)"))
    other_exceptions_unchecked: bool = Field(False, description=(
        "True when the work may fall under a CBC 11B-202.4 exception other than Exception 8 (not in the verified "
        "rulebook): ask plan check"))
    explanation: str
    forms: list[Requirement] = Field(default_factory=list)


class FederalPathOfTravel(BaseModel):
    law: Literal["ADA Title III (28 CFR 36.403)", "ADA Title II (28 CFR 35.151(b)(4))"]
    status: Literal["capped_20_percent", "not_triggered", "not_evaluated"]
    primary_function_alteration_cost_usd: Optional[float] = None
    counted_prior_cost_usd: float = 0
    disproportionality_fraction: Optional[float] = None
    disproportionality_limit_usd: Optional[float] = Field(None, description=(
        "Path-of-travel cost above this is 'disproportionate'; up to it must be spent"))
    required_spend_usd: Optional[float] = None
    standards: str = ""
    explanation: str


class ElementPlan(BaseModel):
    priority: int
    element: str
    estimated_cost_usd: Optional[float] = None
    funded_usd: float = 0
    status: Literal["fund_fully", "fund_partly", "beyond_cap", "already_complies", "not_listed",
                    "full_compliance_required", "no_cost_given", "undetermined"]


class PathOfTravelResult(_Base):
    permit_application_date: str
    work_type: str
    facility: str
    jurisdiction: str
    path_of_travel_elements: list[str] = Field(default_factory=list)
    prior_alterations: list[PriorLine] = Field(default_factory=list)
    california: CaliforniaPathOfTravel
    federal: FederalPathOfTravel
    full_path_of_travel_cost_usd: Optional[float] = None
    combined_budget_usd: Optional[float] = Field(None, description=(
        "The larger of the California cap and the federal limit (both must be met); None when full compliance is "
        "required or nothing is triggered"))
    combined_required_spend_usd: Optional[float] = None
    priority_order: list[str] = Field(default_factory=list)
    spending_plan: list[ElementPlan] = Field(default_factory=list)
    compliance_percent: Optional[float] = Field(None, description=(
        "Planned accessibility spend / adjusted construction cost x 100 (the San Diego DS-2123 'A / (B + C)' line)"))
    planned_spend_meets_requirement: Optional[bool] = None


def _element_costs(value) -> Optional[dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("element_costs_usd must be an object like {'entrance': 12000, 'restroom': 28000}")
    out: dict[str, float] = {}
    for k, v in value.items():
        slot = _slot(k)
        amt = _usd(f"element_costs_usd[{k!r}]", v)
        if amt is None:
            continue
        out[slot] = out.get(slot, 0.0) + amt
    return out


def _prior(acc: _Acc, app: date, total, items, title_ii: bool) -> tuple[float, float, list[PriorLine]]:
    """-> (California B, federal prior cost, lines). California: CBC 11B-202.4 Exc. 8 three-year lookback; federal:
    28 CFR 36.403(h) (alterations after 1992-01-26) or 35.151(b)(4)(v) (on or after 2011-03-15)."""
    ca_years = int(P(LOOKBACK, "lookback_years"))
    if title_ii:
        fed_years = int(P(T2_SERIES, "lookback_years"))
        fed_cut = _date(P(T2_SERIES, "counts_only_alterations_on_or_after"), "rule date")
        fed_ok, fed_words = (lambda d: d >= fed_cut), f"on or after {fed_cut}"
    else:
        fed_years = int(P(F_SERIES, "lookback_years"))
        fed_cut = _date(P(F_SERIES, "counts_only_alterations_after"), "rule date")
        fed_ok, fed_words = (lambda d: d > fed_cut), f"after {fed_cut}"
    if not items:
        amt = _usd("prior_alterations_cost_usd", total) or 0.0
        if amt:
            acc.math.append(f"B = prior alterations entered as one total = {_usd_s(amt)} (must be work in the "
                            f"{ca_years} years before {app} on this area or the same path of travel that did not "
                            "provide an accessible path of travel)")
        return amt, amt, []
    if not isinstance(items, (list, tuple)):
        raise ValueError("prior_alterations must be a list of {date, cost_usd, provided_accessible_path_of_travel}")
    ca_start, fed_start = _plus_years(app, -ca_years), _plus_years(app, -fed_years)
    lines: list[PriorLine] = []
    for i, it in enumerate(items):
        if isinstance(it, BaseModel):
            it = it.model_dump()
        if not isinstance(it, dict):
            raise ValueError(f"prior_alterations[{i}] must be an object with date and cost_usd, got {it!r}")
        d = _date(it.get("date"), f"prior_alterations[{i}].date")
        cost = _usd(f"prior_alterations[{i}].cost_usd", it.get("cost_usd"))
        if cost is None:
            raise ValueError(f"prior_alterations[{i}].cost_usd is required")
        if d >= app:
            raise ValueError(f"prior_alterations[{i}].date ({d}) must be before permit_application_date ({app})")
        provided = bool(_bool(f"prior_alterations[{i}].provided_accessible_path_of_travel",
                              it.get("provided_accessible_path_of_travel", False)))
        same = _bool(f"prior_alterations[{i}].same_path_of_travel", it.get("same_path_of_travel", True))
        same = True if same is None else same
        if provided:
            reason = "not added: it provided an accessible path of travel"
            ca = fed = False
        elif not same:
            reason = "not added: different area and not on the same path of travel"
            ca = fed = False
        else:
            ca = d >= ca_start
            fed = d >= fed_start and fed_ok(d)
            if ca and fed:
                reason = f"added: within the {ca_years} years before {app} ({ca_start} on)"
            elif ca:
                reason = f"added for California; federal counts only alterations {fed_words}"
            elif fed:
                reason = f"added for federal only (federal look-back {fed_years} years)"
            else:
                reason = f"not added: before {ca_start}, outside the {ca_years}-year look-back"
        lines.append(PriorLine(date=d.isoformat(), cost_usd=_money(cost), counted_california=ca, counted_federal=fed,
                               reason=reason))
    b_ca = _money(sum(x.cost_usd for x in lines if x.counted_california))
    b_fed = _money(sum(x.cost_usd for x in lines if x.counted_federal))
    added = [_usd_s(x.cost_usd) for x in lines if x.counted_california]
    acc.math.append("B = " + (" + ".join(added) + " = " if len(added) > 1 else "") + f"{_usd_s(b_ca)} (prior "
                    f"alterations since {ca_start} on the same path without an accessible path of travel)")
    return b_ca, b_fed, lines


def _plan(labels: list[str], costs: Optional[dict[str, float]], budget: Optional[float],
          full_required: bool) -> list[ElementPlan]:
    out, left = [], budget
    for i, (slot, label) in enumerate(zip(_SLOTS, labels), start=1):
        cost = None if costs is None else costs.get(slot)
        if costs is None:
            out.append(ElementPlan(priority=i, element=label,
                                   status="full_compliance_required" if full_required else "no_cost_given"))
            continue
        if cost is None:
            out.append(ElementPlan(priority=i, element=label, status="not_listed"))
        elif cost == 0:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=0, status="already_complies"))
        elif full_required:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=_money(cost), funded_usd=_money(cost),
                                   status="full_compliance_required"))
        elif left is None:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=_money(cost), status="undetermined"))
        elif left >= cost - EPS:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=_money(cost), funded_usd=_money(cost),
                                   status="fund_fully"))
            left -= cost
        elif left > EPS:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=_money(cost), funded_usd=_money(left),
                                   status="fund_partly"))
            left = 0.0
        else:
            out.append(ElementPlan(priority=i, element=label, estimated_cost_usd=_money(cost), status="beyond_cap"))
    return out


def _standards(acc: _Acc, title_ii: bool, d: date) -> str:
    if title_ii:
        acc.use(T2_2010_DATE)
        start = _date(P(T2_2010_DATE, "mandatory_2010_standards_from"), "rule date")
        return (f"2010 ADA Standards (physical construction starting on or after {start}; {d} used as the start)"
                if d >= start else f"Not the 2010 Standards: construction started before {start}")
    acc.use(F_2010_DATE)
    mandatory = _date(P(F_2010_DATE, "mandatory_2010_standards_from"), "rule date")
    opt_from = _date(P(F_2010_DATE, "optional_period.from"), "rule date")
    if d >= mandatory:
        return f"2010 ADA Standards (last permit application certified complete on or after {mandatory})"
    if d >= opt_from:
        return f"1991 or 2010 ADA Standards (permit application from {opt_from} to before {mandatory})"
    return f"1991 ADA Standards (permit application before {opt_from})"


def path_of_travel_obligation(construction_cost_usd, permit_application_date=None, prior_alterations_cost_usd=None,
                              prior_alterations=None, path_of_travel_costs_included_usd=None,
                              full_path_of_travel_cost_usd=None, element_costs_usd=None,
                              planned_accessibility_spend_usd=None, work_type: str = "alteration",
                              facility: str = "public_accommodation", affects_primary_function_area=True,
                              primary_function_alteration_cost_usd=None, jurisdiction: Optional[str] = None,
                              hardship_requested: bool = False, tenant_only_alteration: bool = False,
                              path_elements_built_to_1991_standards: bool = False) -> PathOfTravelResult:
    acc = _Acc()
    c = _usd("construction_cost_usd", construction_cost_usd)
    if c is None or c <= 0:
        raise ValueError(f"construction_cost_usd must be more than 0 (the permit valuation / construction cost of this "
                         f"alteration), got {construction_cost_usd!r}")
    app = _date(permit_application_date, "permit_application_date", default=logic.today_ca())
    x = _usd("path_of_travel_costs_included_usd", path_of_travel_costs_included_usd) or 0.0
    if x > c + EPS:
        raise ValueError(f"path_of_travel_costs_included_usd ({_usd_s(x)}) cannot be more than construction_cost_usd "
                         f"({_usd_s(c)}): it is the part of the construction cost that is path-of-travel work")
    full = _usd("full_path_of_travel_cost_usd", full_path_of_travel_cost_usd)
    costs = _element_costs(element_costs_usd)
    if costs:
        listed = _money(sum(costs.values()))
        if full is None:
            full = listed
            acc.math.append(f"Full path-of-travel cost = sum of element estimates = {_usd_s(listed)}")
        elif listed > full + 0.005:
            raise ValueError(f"element_costs_usd add up to {_usd_s(listed)}, more than full_path_of_travel_cost_usd "
                             f"({_usd_s(full)}); fix one of them")
    spend = _usd("planned_accessibility_spend_usd", planned_accessibility_spend_usd)
    work, fac = _work_type(work_type), _facility(facility)
    t2 = fac == "public_entity"
    if work == "program_access" and not t2:
        raise ValueError("work_type program_access is the ADA Title II duty of a public entity; use facility "
                         "public_entity, or barrier_removal for a private business")
    if work == "barrier_removal" and t2:
        raise ValueError("work_type barrier_removal is the ADA Title III duty of a private business; for a public "
                         "entity use program_access")
    affects = _bool("affects_primary_function_area", affects_primary_function_area)
    if affects is None:
        affects = True
        acc.notes.append("affects_primary_function_area was not known, so the federal test assumes the work affects "
                         "an area containing a primary function (the stricter reading).")
    base_override = _usd("primary_function_alteration_cost_usd", primary_function_alteration_cost_usd)
    if _usd("prior_alterations_cost_usd", prior_alterations_cost_usd) and prior_alterations:
        raise ValueError("give prior_alterations_cost_usd (one total) or prior_alterations (a list), not both")
    juris, juris_label, county_note = _jurisdiction(jurisdiction)
    if county_note:
        acc.notes.append(county_note)
    hardship = bool(_bool("hardship_requested", hardship_requested))
    tenant_only = bool(_bool("tenant_only_alteration", tenant_only_alteration))
    built_1991 = bool(_bool("path_elements_built_to_1991_standards", path_elements_built_to_1991_standards))

    b_ca, b_fed, prior_lines = _prior(acc, app, prior_alterations_cost_usd, prior_alterations, t2)
    adj = _money(c - x + b_ca)
    ca_years = int(P(LOOKBACK, "lookback_years"))
    acc.use(ADJ_EXCL, LOOKBACK)
    acc.math.append(f"Adjusted construction cost = C {_usd_s(c)} this project - {_usd_s(x)} path-of-travel work "
                    f"inside it + B {_usd_s(b_ca)} prior {ca_years}-year alterations = {_usd_s(adj)}")
    elements = list(P(POT_ELEMENTS, "path_of_travel_elements"))

    # ---------------- California (CBC 11B-202.4)
    thresholds = _thresholds()
    thr = thresholds.get(app.year)
    cap_frac = float(P(CAP20, "cap_fraction_of_adjusted_construction_cost"))
    cap = _money(cap_frac * adj)
    acc.math.append(f"{_pct(cap_frac)} x {_usd_s(adj)} = {_usd_s(cap)}")
    forms: list[Requirement] = []
    hardship_min = None
    other_exc = work in ("barrier_removal", "program_access", "minor_work_only", "maintenance_only")
    ca_required = ca_max = None
    if work == "parking_resurfacing_restriping":
        acc.use(RESTRIPE)
        ca_status = "not_triggered"
        ca_expl = ("Resurfacing and/or restriping an existing parking lot is limited to the actual scope of work and "
                   "does not trigger the CBC 11B-202.4 path-of-travel upgrades (CCDA guide). The restriped spaces "
                   "themselves must meet current accessible parking rules.")
    else:
        acc.use(POT_REQ, POT_ELEMENTS, THRESHOLD, ANNUAL, CAP20)
        if work in ("alteration", "structural_repair"):
            acc.use(ALTERED)
        if fac == "publicly_funded_multifamily":
            acc.use(PUB_MF)
        if thr is None:
            ca_status = "threshold_unknown_for_year"
            latest = max(thresholds)
            ca_expl = (f"The {app.year} DSA valuation threshold is not in the verified rulebook (each figure is valid "
                       f"for {P(ANNUAL, 'valid_for')}, reset {P(ANNUAL, 'reset_date')}; the latest on file is "
                       f"{_usd_s(thresholds[latest])} for {latest}). If the adjusted cost {_usd_s(adj)} is at or below "
                       f"the {app.year} threshold, path-of-travel work need not exceed {_pct(cap_frac)} = "
                       f"{_usd_s(cap)}; above it, full path-of-travel compliance is required.")
        elif adj <= thr + EPS:
            ca_status, ca_max = "capped_20_percent", cap
            ca_required = None if full is None else _money(min(full, cap))
            acc.math.append(f"Adjusted cost {_usd_s(adj)} <= {app.year} valuation threshold {_usd_s(thr)} -> Exception "
                            f"8 cap {_usd_s(cap)}"
                            + (f"; required = min(full {_usd_s(full)}, cap {_usd_s(cap)}) = {_usd_s(ca_required)}"
                               if full is not None else ""))
            ca_expl = (f"Adjusted construction cost {_usd_s(adj)} is at or below the {app.year} valuation threshold "
                       f"{_usd_s(thr)}, so CBC 11B-202.4 Exception 8 applies: path-of-travel work need not exceed "
                       f"{_pct(cap_frac)} of the adjusted cost = {_usd_s(cap)}, spent in the priority order"
                       + (f" (all of it here: the full path-of-travel cost {_usd_s(full)} is within the cap)."
                          if full is not None and full <= cap + EPS else "."))
        else:
            ca_status = "full_compliance"
            ca_required = full
            acc.math.append(f"Adjusted cost {_usd_s(adj)} > {app.year} valuation threshold {_usd_s(thr)} -> no "
                            f"{_pct(cap_frac)} cap")
            ca_expl = (f"Adjusted construction cost {_usd_s(adj)} is above the {app.year} valuation threshold "
                       f"{_usd_s(thr)}, so Exception 8's {_pct(cap_frac)} cap does not apply: every path-of-travel "
                       f"element ({_join(elements)}) must fully comply, unless the building department finds an "
                       "unreasonable hardship.")
            if hardship:
                acc.use(ABOVE)
                frac = float(P(ABOVE, "if_unreasonable_hardship_granted."
                                      "min_spend_fraction_of_adjusted_construction_cost"))
                hardship_min = _money(frac * adj)
                acc.math.append(f"Hardship minimum (NOT verified) = {_pct(frac)} x {_usd_s(adj)} = "
                                f"{_usd_s(hardship_min)}")
                ca_expl += (f" If the {P(ABOVE, 'decided_by')} grants a hardship, access is provided by "
                            f"{P(ABOVE, 'if_unreasonable_hardship_granted.method')}, and the San Diego guide (2013 CBC "
                            f"text, not verified for the current CBC) says the spend may not be less than "
                            f"{_pct(frac)} = {_usd_s(hardship_min)}.")
                if juris == "san_diego":
                    forms.append(acc.req("City of San Diego Form DS-2123", (
                        f"Request the hardship on {P(SD_FORM, 'form')}, listing each path-of-travel element, whether "
                        f"it complies, its cost and what you will spend, with {ca_years} years of prior work; signed "
                        f"by the {_join(P(SD_FORM, 'signers'), 'or')}."), SD_FORM))
                    forms.append(acc.req("San Diego appeals ratification", (
                        f"A granted hardship needs an {P(SD_RATIFY, 'step')}; submit a "
                        f"{P(SD_RATIFY, 'submit')}."), SD_RATIFY))
            else:
                ca_expl += " (Set hardship_requested=true to see what a hardship finding involves.)"
        if other_exc:
            acc.notes.append(f"For {work.replace('_', ' ')} the California figures are computed as for an alteration. "
                             "CBC 11B-202.4 has other exceptions besides Exception 8 that were not in the verified "
                             "rulebook; ask plan check whether one covers this work.")
        if ca_status == "capped_20_percent":
            all_comply = full is not None and full <= cap + EPS
            if juris == "los_angeles" and not all_comply:
                forms.append(acc.req("LADBS Summary of Accessibility Upgrades", (
                    f"Not every path-of-travel element will comply, so file the {P(LA_FORM, 'form')} showing what will "
                    f"be upgraded and its cost, plus a {P(LA_FORM, 'also_required')}."), LA_FORM))
            elif juris == "san_diego" and not all_comply:
                forms.append(acc.req("City of San Diego Form DS-2123", (
                    f"Request Exception 8 on {P(SD_FORM, 'form')}: list each element "
                    f"({_join(P(SD_FORM, 'elements_listed'))}), whether it complies, its cost and what you will spend, "
                    f"with all improvements to this area and the same path of travel in the preceding {ca_years} "
                    f"years; signed and certified by the {_join(P(SD_FORM, 'signers'), 'or')}."), SD_FORM))
    ca = CaliforniaPathOfTravel(status=ca_status, adjusted_construction_cost_usd=adj, valuation_threshold_usd=thr,
                                threshold_year=app.year, cap_fraction=cap_frac, twenty_percent_of_adjusted_cost_usd=cap,
                                max_required_spend_usd=ca_max, required_spend_usd=ca_required,
                                hardship_min_spend_usd=hardship_min, other_exceptions_unchecked=other_exc,
                                explanation=ca_expl, forms=forms)
    if thr is not None and ca_status != "not_triggered":
        stale = [p for p in (P(THRESHOLD, "prior_values_seen_usd") or [])
                 if not p.get("year") and str(app.year) in str(p.get("source", "")) and p.get("value") != thr]
        acc.notes.append(f"Valuation threshold {_usd_s(thr)} for {app.year} (DSA figure as printed by LADBS and San "
                         f"Diego). Confirm the figure the building department applies on your submittal date"
                         + (f": one LADBS sheet still printed {_usd_s(stale[0]['value'])}." if stale else "."))

    # ---------------- federal (28 CFR 36.403 / 35.151(b)(4))
    law = "ADA Title II (28 CFR 35.151(b)(4))" if t2 else "ADA Title III (28 CFR 36.403)"
    fed_frac = float(P(T2_20 if t2 else F_20, "disproportionate_above_fraction"))
    standards = _standards(acc, t2, app)
    fed_status, fed_limit, fed_required, fed_base = "not_evaluated", None, None, None
    if fac == "publicly_funded_multifamily":
        acc.use(S_RES_EXC)
        fed_expl = ("Residential dwelling units are exempt from the 2010 ADA Standards primary-function path-of-travel "
                    "rule (202.4). Federal rules for common areas or federally funded housing (Section 504) are not in "
                    "the verified rulebook: ask your architect.")
    elif work == "addition":
        fed_expl = ("The federal additions rule (2010 ADA Standards 202.2) is not in the verified rulebook, so the "
                    "federal limit was not computed for an addition: ask your architect.")
    elif work == "parking_resurfacing_restriping":
        fed_expl = ("Whether federal rules treat resurfacing or restriping as an alteration is not in the verified "
                    "rulebook; restripe to current accessible parking rules.")
    elif work == "barrier_removal":
        acc.use(F_BARRIER_NO_POT)
        fed_status = "not_triggered"
        fed_expl = ("Work done solely to remove barriers (28 CFR 36.304) does not trigger the federal path-of-travel "
                    "rule; each altered element must still meet the alterations standards.")
    elif work == "program_access":
        acc.use(T2_PROGRAM)
        fed_status = "not_triggered"
        fed_expl = ("Alterations made solely for program accessibility (28 CFR 35.150) do not trigger the Title II "
                    "path-of-travel rule.")
    elif work == "minor_work_only" and not t2:
        acc.use(F_MINOR)
        fed_status = "not_triggered"
        fed_expl = (f"Alterations to {_join(P(F_MINOR, 'non_triggering_work'))} are not treated as affecting a "
                    "primary function area, so no federal path-of-travel duty.")
    elif work == "maintenance_only" and not t2:
        acc.use(F_WHAT)
        fed_status = "not_triggered"
        fed_expl = (f"{_cap(_join(P(F_WHAT, 'not_alterations_unless_usability_affected')))} are not "
                    "alterations unless they affect the usability of the building. If yours does, re-run as an "
                    "alteration.")
    elif work in ("minor_work_only", "maintenance_only"):
        fed_expl = "The Title II rule for this kind of work is not in the verified rulebook."
    elif not affects:
        acc.use(T2_20 if t2 else F_POT)
        if not t2:
            acc.use(F_PRIMARY)
        fed_status = "not_triggered"
        fed_expl = ("The work does not affect an area containing a primary function, so no federal path-of-travel "
                    "duty. " + ("" if t2 else f"Not primary function areas: {_join(P(F_PRIMARY, 'not_primary'))}."))
    else:
        fed_status = "capped_20_percent"
        acc.use(*((T2_20, T2_SERIES, F_PRIORITY) if t2 else (F_POT, F_20, F_SERIES, F_PRIORITY, F_COSTS)))
        fed_base = base_override if base_override is not None else _money(c - x)
        fed_limit = _money(fed_frac * (fed_base + b_fed))
        fed_required = None if full is None else _money(min(full, fed_limit))
        acc.math.append(f"Federal limit = {_pct(fed_frac)} x (primary function alteration {_usd_s(fed_base)} + prior "
                        f"{_usd_s(b_fed)}) = {_usd_s(fed_limit)}")
        fed_expl = (f"Path-of-travel work costing more than {_pct(fed_frac)} of the primary-function alteration "
                    f"cost is disproportionate; up to {_usd_s(fed_limit)} must be spent (there is no dollar threshold "
                    "federally).")
        if base_override is None:
            acc.notes.append("The federal base assumes all of the construction cost (less path-of-travel work) "
                             "alters primary function areas. If part of it is in non-primary areas (corridors, "
                             "restrooms, storage, mechanical rooms), give primary_function_alteration_cost_usd.")
        if t2:
            acc.notes.append("Title II: restrooms count as a primary function area only if "
                             f"{P(T2_20, 'restrooms_primary_function_only_if')}.")
    if built_1991 and fed_status == "capped_20_percent":
        sh = T2_SAFE if t2 else F_SAFE
        acc.use(sh)
        acc.notes.append(("Path-of-travel elements already built or altered to the "
                          + (_join(P(T2_SAFE, "safe_harbor_standards"), "or") + f" before {P(T2_SAFE, 'built_before')}"
                             if t2 else P(F_SAFE, "safe_harbor_standard"))
                          + " need not be retrofitted to the 2010 Standards' incremental changes just because this "
                            "primary function area is altered (federal safe harbor). CBC 11B still applies "
                            "separately and may require the upgrade."))
    if tenant_only and not t2:
        acc.use(F_TENANT)
        acc.notes.append("Tenant work only in tenant-occupied areas does not put a federal path-of-travel duty on the "
                         "landlord for areas the landlord controls, unless those areas are otherwise being altered "
                         "(28 CFR 36.403(d)). California plan check still applies CBC 11B-202.4 to the tenant's "
                         "permit.")
    fed = FederalPathOfTravel(law=law, status=fed_status, primary_function_alteration_cost_usd=fed_base,
                              counted_prior_cost_usd=b_fed if fed_status == "capped_20_percent" else 0,
                              disproportionality_fraction=fed_frac if fed_status == "capped_20_percent" else None,
                              disproportionality_limit_usd=fed_limit, required_spend_usd=fed_required,
                              standards=standards, explanation=fed_expl)

    # ---------------- combined plan
    full_required = ca_status == "full_compliance"
    caps = [v for v in ((cap if ca_status == "capped_20_percent" else None), fed_limit) if v is not None]
    budget = None if full_required or not caps else max(caps)
    if ca_status == "threshold_unknown_for_year":
        acc.notes.append(f"The budget and plan use only the federal limit until the {app.year} valuation threshold is "
                         f"known; California may require more (full compliance if the adjusted cost is above it).")
    if full_required:
        combined = full
    elif budget is not None:
        combined = None if full is None else _money(min(full, budget))
    else:
        combined = None
    use_ca_order = ca_status != "not_triggered"
    if use_ca_order:
        acc.use(PRIORITY)
        labels = list(P(PRIORITY, "priority_order"))
    elif fed_status == "capped_20_percent":
        labels = list(P(F_PRIORITY, "priority_order"))
    else:
        labels = []
    plan = _plan(labels, costs, budget, full_required) if labels else []
    if budget is not None and len(caps) == 2 and abs(caps[0] - caps[1]) > 0.005:
        acc.math.append(f"Budget = larger of California {_usd_s(caps[0])} and federal {_usd_s(caps[1])} = "
                        f"{_usd_s(budget)} (both rules apply to a California project)")
    pct = met = None
    if spend is not None and adj > 0:
        pct = round(spend / adj * 100, 2)
        acc.math.append(f"Compliance % = A {_usd_s(spend)} / adjusted cost {_usd_s(adj)} x 100 = {_n(pct)}%")
        target = combined if combined is not None else budget
        if target is not None:
            met = spend >= target - 0.005
            acc.math.append(f"Planned {_usd_s(spend)} {'>=' if met else '<'} required {_usd_s(target)}")
    acc.use(PLAN_CHECK)
    acc.notes.append("Plan check reviews Title 24 (CBC) only; it is not delegated authority to review the ADA, so "
                     "meeting the federal limit is the owner's and designer's job even after final inspection.")

    # ---------------- summary and next step
    ca_short = {"capped_20_percent": f"Exception 8 applies, spend up to {_usd_s(cap)} ({_pct(cap_frac)} of "
                                     f"{_usd_s(adj)})",
                "full_compliance": f"above the {app.year} threshold, full path-of-travel compliance",
                "threshold_unknown_for_year": f"{app.year} valuation threshold not on file",
                "not_triggered": "no path-of-travel upgrade triggered"}[ca_status]
    fed_short = {"capped_20_percent": f"up to {_usd_s(fed_limit)}" if fed_limit is not None else "",
                 "not_triggered": "not triggered", "not_evaluated": "not evaluated"}[fed_status]
    if other_exc and ca_status != "not_triggered":
        ca_short += " if plan check treats this as an alteration (another CBC 11B-202.4 exception may apply)"
    summary = f"California: {ca_short}. Federal {law.split(' (')[0]}: {fed_short}."
    if combined is not None:
        summary += f" Plan at least {_usd_s(combined)} of path-of-travel work."
    if ca_status == "capped_20_percent":
        if juris == "los_angeles":
            how = "File LADBS's Summary of Accessibility Upgrades form with your unreasonable-hardship request."
        elif juris == "san_diego":
            how = "Fill in City of San Diego Form DS-2123, signed by the owner, architect or design professional."
        else:
            how = f"Ask plan check for its Exception 8 ({_pct(cap_frac)} disproportionate cost) form."
        if full is not None and full <= cap + EPS:
            how = "All path-of-travel elements fit inside the cap, so show every one as compliant on the plans."
        nxt = (f"{how} Put path-of-travel work worth {_usd_s(combined if combined is not None else budget)} on the "
               "plans in the priority order (entrance first, then the route to the altered area, then restrooms; "
               f"see spending_plan) and list every permit on this area and the same path of travel in the last "
               f"{ca_years} years.")
    elif ca_status == "full_compliance":
        nxt = (f"Show every path-of-travel element ({_join(elements)}) as fully compliant on the plans. If that cost "
               "is an unreasonable hardship, ask the building department about a hardship finding before you submit.")
    elif ca_status == "threshold_unknown_for_year":
        nxt = (f"Get the {app.year} valuation threshold from your building department (DSA sets it "
               f"{P(ANNUAL, 'reset_date')}) and re-run before you price the path-of-travel work.")
    else:
        nxt = ("Restripe to current accessible parking rules: run accessible_parking_requirements for the space "
               "counts, stall sizes, signs and markings.")
    res = PathOfTravelResult(permit_application_date=app.isoformat(), work_type=work, facility=fac,
                             jurisdiction=juris_label, path_of_travel_elements=elements, prior_alterations=prior_lines,
                             california=ca, federal=fed, full_path_of_travel_cost_usd=full, combined_budget_usd=budget,
                             combined_required_spend_usd=combined, priority_order=labels, spending_plan=plan,
                             compliance_percent=pct, planned_spend_meets_requirement=met, summary=summary,
                             next_step=nxt)
    return acc.finish(res)


# ================================================================ accessible parking (11B-208, 11B-502, 11A 1109A)
ParkingUse = Literal["general", "hospital_outpatient", "rehab_outpatient_pt", "covered_multifamily"]
_PARKING_USE = (
    ("covered_multifamily", ("covered multifamily", "multifamily", "multi family", "apartment", "apartments",
                             "condo", "condos", "condominium", "residential", "11a")),
    ("rehab_outpatient_pt", ("rehab", "rehabilitation", "physical therapy", "outpatient physical therapy", "pt")),
    ("hospital_outpatient", ("hospital", "hospital outpatient", "outpatient")),
    ("general", ("general", "public accommodation", "commercial", "retail", "office", "restaurant", "public building",
                 "government", "11b", "store", "shopping center", "warehouse")),
)
_KINDS = (  # CBC 1109A facility types, in the rule's words (index into ada-cbc-11a-1109a-parking-percentages)
    (1, ("private garage", "private garages", "tuck under", "tuck under garage")),
    (0, ("garage", "garages", "parking garage", "parking structure", "structure", "podium", "underground")),
    (2, ("carport", "carports")),
    (3, ("lot", "surface", "surface lot", "open", "uncovered", "off street", "off street parking", "spaces")),
)


def _parking_use(value) -> str:
    w = _words(value or "general")
    if w.replace(" ", "_") in {k for k, _ in _PARKING_USE}:
        return w.replace(" ", "_")
    for key, phrases in _PARKING_USE:
        if any(_has(w, p) for p in phrases):
            return key
    raise ValueError("use must be general (stores, offices, restaurants, public buildings), hospital_outpatient "
                     "(patient and visitor parking for hospital outpatient facilities), rehab_outpatient_pt "
                     "(rehabilitation or outpatient physical therapy) or covered_multifamily (CBC 11A); "
                     f"got {value!r}")


def _kind(value) -> str:
    types = P(A_PK, "facility_types")
    w = _words(value or "off street parking")
    for i, label in enumerate(types):
        if w == _words(label):
            return label
    for idx, phrases in _KINDS:
        if any(_has(w, p) for p in phrases):
            return types[idx]
    raise ValueError(f"kind must be one of {types} (garage, private garage, carport or lot); got {value!r}")


class ParkingFacilityIn(BaseModel):
    name: Optional[str] = Field(None, description="Lot or garage name, e.g. 'North lot'")
    total_spaces: Union[int, str] = Field(description="All parking spaces in this lot or garage")
    ev_charging_spaces: Union[int, str] = Field(0, description=(
        "EV charging spaces included in total_spaces (CBC 11B: they are not parking spaces)"))
    kind: Optional[str] = Field(None, description="Multifamily only: garage, private garage, carport or lot")
    assigned: Optional[bool] = Field(None, description=(
        "Multifamily only: spaces assigned to residents (true) or unassigned/visitor (false)"))
    use: Optional[str] = Field(None, description=(
        "Override for this facility: general, hospital_outpatient or rehab_outpatient_pt"))


class ParkingFacilityOut(BaseModel):
    name: str
    total_spaces: int
    ev_charging_spaces: int = 0
    counted_spaces: int
    use: str
    required_accessible: int
    van_accessible: Optional[int] = Field(None, description="CBC 11B: per facility. CBC 11A: see the totals")
    van_accessible_literal_minimum: Optional[int] = Field(None, description=(
        "CBC 11A only: 'one in every eight' read without rounding up (the rule does not say how to count a "
        "partial group of eight)"))
    math: str


class ParkingResult(_Base):
    code: Literal["CBC 11B / 2010 ADA Standards", "CBC 11A (covered multifamily)"]
    use: str
    work_type: Literal["new_or_altered", "resurfacing_restriping"]
    facilities: list[ParkingFacilityOut] = Field(default_factory=list)
    total_required_accessible: int
    total_van_accessible: int
    total_van_accessible_literal_minimum: Optional[int] = None
    covered_dwelling_units: Optional[int] = None
    dimensions: list[Requirement] = Field(default_factory=list)
    signs_and_markings: list[Requirement] = Field(default_factory=list)


def _facilities(facilities, total_spaces) -> list[dict]:
    if facilities in (None, [], "") and total_spaces in (None, ""):
        raise ValueError("give total_spaces for one lot, or facilities (one entry per lot or garage)")
    if facilities not in (None, [], "") and total_spaces not in (None, ""):
        raise ValueError("give total_spaces (one lot) or facilities (several), not both")
    if facilities in (None, [], ""):
        facilities = [{"total_spaces": total_spaces}]
    if isinstance(facilities, (int, float, str, dict, BaseModel)) and not isinstance(facilities, bool):
        facilities = [facilities]
    out = []
    for i, f in enumerate(facilities):
        if isinstance(f, BaseModel):
            f = f.model_dump(exclude_none=True)
        if isinstance(f, (int, float, str)) and not isinstance(f, bool):
            f = {"total_spaces": f}
        if not isinstance(f, dict):
            raise ValueError(f"facilities[{i}] must be a number of spaces or an object with total_spaces, got {f!r}")
        total = _count(f"facilities[{i}].total_spaces", f.get("total_spaces"))
        if total is None:
            raise ValueError(f"facilities[{i}].total_spaces is required")
        ev = _count(f"facilities[{i}].ev_charging_spaces", f.get("ev_charging_spaces")) or 0
        if ev > total:
            raise ValueError(f"facilities[{i}]: ev_charging_spaces ({ev}) cannot exceed total_spaces ({total})")
        out.append({"name": str(f.get("name") or f"Facility {i + 1}").strip(), "total": total, "ev": ev,
                    "kind": f.get("kind"), "assigned": _bool(f"facilities[{i}].assigned", f.get("assigned")),
                    "use": f.get("use")})
    return out


def _table_208(n: int) -> tuple[int, str]:
    """2010 ADA Standards Table 208.2 -> (required accessible spaces, math)."""
    for r in P(S_PK_TABLE, "table"):
        lo, hi = r["from"], r["to"]
        if n < lo or (hi is not None and n > hi):
            continue
        rng = f"{lo:,}-{hi:,}" if hi is not None else f"{lo:,}+"
        if isinstance(r.get("required"), (int, float)):
            return int(r["required"]), f"{n:,} spaces is in the {rng} row -> {int(r['required'])}"
        if "required_percent_of_total" in r:
            pct = r["required_percent_of_total"]
            req = _ceil_frac(Fraction(str(pct)) / 100, n)
            return req, (f"{n:,} spaces is in the {rng} row -> {_n(pct)}% x {n:,} = {_n(pct * n / 100)}, rounded up "
                         f"{req}")
        m = re.fullmatch(r"(\d+) plus (\d+) for each (\d+).*over (\d+)", str(r.get("required")))
        if not m:
            raise RuntimeError(f"cannot read Table 208.2 row {r!r}")
        base, per, each, over = (int(g) for g in m.groups())
        extra = math.ceil((n - over) / each) * per
        return base + extra, (f"{n:,} spaces is in the {rng} row -> {base} + {per} per {each} (or fraction) over "
                              f"{over:,}: {base} + ceil({n - over:,}/{each}) = {base + extra}")
    raise RuntimeError(f"no Table 208.2 row for {n} spaces")


def accessible_parking_requirements(facilities=None, total_spaces=None, use: str = "general",
                                    covered_dwelling_units=None, work_type: str = "new_or_altered") -> ParkingResult:
    acc = _Acc()
    puse = _parking_use(use)
    wt = _words(work_type or "new or altered")
    if any(_has(wt, p) for p in ("resurfacing", "resurface", "restriping", "restripe", "re stripe", "re striping",
                                 "slurry seal")):
        wtype = "resurfacing_restriping"
    elif wt in ("new or altered", "new", "new construction", "alteration", "altered", "addition", "remodel"):
        wtype = "new_or_altered"
    else:
        raise ValueError(f"work_type must be new_or_altered or resurfacing_restriping, got {work_type!r}")
    facs = _facilities(facilities, total_spaces)
    if wtype == "resurfacing_restriping":
        acc.use(RESTRIPE)
        acc.notes.append("Resurfacing or restriping alone does not trigger CBC 11B-202.4 path-of-travel upgrades, but "
                         "the restriped accessible spaces must meet the current counts, sizes and markings below.")
    if puse == "covered_multifamily":
        return _parking_11a(acc, facs, covered_dwelling_units, wtype)
    if covered_dwelling_units not in (None, ""):
        raise ValueError("covered_dwelling_units is for use covered_multifamily (CBC 11A) only")

    acc.use(PK_FACILITY, S_PK_TABLE, PK_VAN6, S_VAN)
    div, vmin = _divisor(P(PK_VAN6, "formula")), int(P(PK_VAN6, "min_van_spaces"))
    out = []
    for f in facs:
        fuse = _parking_use(f["use"]) if f["use"] else puse
        if fuse == "covered_multifamily":
            raise ValueError(f"{f['name']}: CBC 11A multifamily parking and CBC 11B parking are counted separately; "
                             "run use covered_multifamily on its own")
        n = f["total"] - f["ev"]
        if n == 0:
            req, m = 0, "no parking spaces counted"
        elif fuse == "general":
            req, m = _table_208(n)
        else:
            key = ("hospital_outpatient_fraction" if fuse == "hospital_outpatient"
                   else "rehab_and_outpatient_pt_fraction")
            frac = float(P(PK_FACILITY, key))
            req = _ceil_frac(frac, n)
            m = (f"{_pct(frac)} x {n:,} = {_n(frac * n)}, rounded up = {req} (rounding not stated; up is the safe "
                 "reading)")
        van = 0 if req == 0 else max(vmin, math.ceil(req / div))
        if req:
            m += f"; van-accessible = max({vmin}, ceil({req}/{div})) = {van}"
        if f["ev"]:
            m = f"{f['total']:,} spaces - {f['ev']:,} EV charging spaces = {n:,} counted; " + m
        out.append(ParkingFacilityOut(name=f["name"], total_spaces=f["total"], ev_charging_spaces=f["ev"],
                                      counted_spaces=n, use=fuse, required_accessible=req, van_accessible=van, math=m))
        acc.math.append(f"{f['name']}: {m}")
    if len(out) > 1:
        acc.notes.append("Each lot or garage is counted separately; the total is not based on all the site's "
                         "spaces together (CBC 11B-208, 2010 ADA Standards 208.2).")
    if any(f.ev_charging_spaces for f in out):
        acc.notes.append("EV charging spaces are not parking spaces for this count (CBC 11B-228.3, as cited by LADBS).")
    if any(f.use != "general" for f in out):
        acc.notes.append("The hospital and rehab percentages apply to patient and visitor spaces; count staff parking "
                         "as a separate facility with use 'general'.")
    acc.notes.append("CBC Table 11B-208.2 itself was not in the verified rulebook, so the general count uses the "
                     "federal 2010 ADA Standards Table 208.2 as the floor; confirm with plan check that the CBC table "
                     "does not require more.")
    # dimensions and markings (CBC 11B-502, with the federal minimums for comparison)
    sv = lambda k: P(PK_STALL, k)                                            # noqa: E731
    fed = lambda k: P(S_PK_DIM, k)                                           # noqa: E731
    dims = [
        acc.req("Car space", (f"{_n(sv('car_width_min_ft'))} ft wide x {_n(sv('stall_length_min_ft'))} ft long "
                              f"minimum, with a {_n(sv('access_aisle_min_ft'))} ft access aisle (two spaces may share "
                              f"one aisle). Federal minimum is {_n(fed('car_width_min_in'))} in wide with a "
                              f"{_n(fed('access_aisle_min_in'))} in aisle: build to the larger CBC size."),
                PK_STALL, S_PK_DIM),
        acc.req("Van space", (
            f"{_n(sv('van_width_min_ft'))} ft wide with a {_n(sv('van_aisle_min_ft_with_12ft_stall'))} ft aisle, or "
            f"{_n(sv('van_width_alt_min_ft'))} ft wide with an {_n(sv('van_aisle_min_ft_with_9ft_stall'))} ft aisle; "
            f"{_n(sv('stall_length_min_ft'))} ft long. LADBS also states van spaces are served by a "
            f"{_n(P(PK_VAN6, 'van_aisle_width_min_in'))} in aisle on the side {P(PK_VAN6, 'aisle_side')}: the "
            f"{_n(sv('van_width_alt_min_ft'))} ft + {_n(sv('van_aisle_min_ft_with_9ft_stall'))} ft "
            f"({_n(sv('van_aisle_min_ft_with_9ft_stall') * 12)} in) layout meets both statements."), PK_STALL, PK_VAN6),
        acc.req("Vertical clearance", (
            f"{P(PK_VERT, 'vertical_clearance_text')} ({_n(P(PK_VERT, 'vertical_clearance_min_in'))} in) minimum at "
            f"accessible spaces and along at least one vehicle route to them from the site entrances and exits."),
            PK_VERT),
    ]
    signs = [
        acc.req("Accessible space sign", (
            f"International Symbol of Accessibility sign, reflectorized, at least "
            f"{_n(P(PK_SIGN, 'reflectorized_min_area_sq_in'))} sq in, bottom at least "
            f"{_n(P(PK_SIGN, 'sign_bottom_height_min_in'))} in above grade; van spaces add '{P(PK_SIGN, 'van_text')}'; "
            f"added text or a sign below the symbol reads '{P(PK_SIGN, 'required_text')}'."), PK_SIGN),
        acc.req("Tow-away sign", (
            f"At least {_n(P(PK_TOW, 'sign_min_width_in'))} x {_n(P(PK_TOW, 'sign_min_height_in'))} in with "
            f"{_n(P(PK_TOW, 'letter_height_min_in'))} in letters, at {P(PK_TOW, 'location')}; fill in "
            f"{_join(P(PK_TOW, 'fill_in'))} as a permanent part of the sign."), PK_TOW),
        acc.req("Access aisle marking", (
            f"{_cap(P(PK_AISLE_MARK, 'border_color'))} painted border; hatching at most "
            f"{_n(P(PK_AISLE_MARK, 'hatch_spacing_max_in'))} in on center in a {P(PK_AISLE_MARK, 'hatch_color')} "
            f"color; '{P(PK_AISLE_MARK, 'text')}' in {P(PK_AISLE_MARK, 'text_color')} letters at least "
            f"{_n(P(PK_AISLE_MARK, 'letter_height_min_in'))} in high, visible from the drive aisle."), PK_AISLE_MARK),
        acc.req("Pavement symbol", (
            f"International Symbol of Accessibility painted in each accessible space, at least "
            f"{_n(P(PK_SURFACE, 'symbol_min_in')[0])} x {_n(P(PK_SURFACE, 'symbol_min_in')[1])} in, its centerline "
            f"within {_n(P(PK_SURFACE, 'centerline_offset_max_in'))} in of the space centerline "
            f"({_join(P(PK_SURFACE, 'schemes'), 'or')})."), PK_SURFACE),
    ]
    total_req, total_van = sum(f.required_accessible for f in out), sum(f.van_accessible for f in out)
    summary = (f"{total_req} accessible space{'s' if total_req != 1 else ''}, {total_van} of them van-accessible, "
               f"across {len(out)} facilit{'ies' if len(out) != 1 else 'y'}.")
    nxt = ("Lay out " + "; ".join(f"{f.name}: {f.required_accessible} accessible ({f.van_accessible} van)" for f in out
                                  if f.required_accessible)
           + f". Put them on the shortest accessible route to the entrance and dimension stalls, aisles, signs and "
             f"the {P(PK_SIGN, 'required_text')} text on the site plan.") if total_req else (
        "No accessible spaces are required because no parking spaces were counted.")
    res = ParkingResult(code="CBC 11B / 2010 ADA Standards", use=puse, work_type=wtype, facilities=out,
                        total_required_accessible=total_req, total_van_accessible=total_van, dimensions=dims,
                        signs_and_markings=signs, summary=summary, next_step=nxt)
    return acc.finish(res)


def _parking_11a(acc: _Acc, facs: list[dict], covered_dwelling_units, wtype: str) -> ParkingResult:
    units = _count("covered_dwelling_units", covered_dwelling_units)
    if not units:
        raise ValueError("covered_dwelling_units (the number of covered multifamily dwelling units) is required for "
                         "use covered_multifamily and must be at least 1")
    acc.use(A_PK, A_VAN8, A_PK_DIM)
    unit_frac, a_frac = float(P(A_PK, "per_covered_units_fraction")), float(P(A_PK, "assigned_spaces_fraction"))
    u_frac, per_type = float(P(A_PK, "unassigned_spaces_fraction")), int(P(A_PK, "min_per_parking_facility_type"))
    by_type: dict[str, dict] = {}
    unknown_assignment = False
    for f in facs:
        if f["use"] and _parking_use(f["use"]) != "covered_multifamily":
            raise ValueError(f"{f['name']}: CBC 11A multifamily parking and CBC 11B parking are counted separately")
        kind = _kind(f["kind"])
        t = by_type.setdefault(kind, {"assigned": 0, "unassigned": 0, "ev": 0, "names": []})
        if f["assigned"] is None:
            unknown_assignment = True
        t["assigned" if f["assigned"] else "unassigned"] += f["total"]
        t["ev"] += f["ev"]
        t["names"].append(f["name"])
    test_units = _ceil_frac(unit_frac, units)
    test_a = max(test_units, per_type * len(by_type))
    acc.math.append(f"Rate on units: {_pct(unit_frac)} x {units:,} covered units = {_n(unit_frac * units)}, rounded up "
                    f"{test_units}; at least {per_type} per facility type x {len(by_type)} type(s) -> {test_a}")
    out, test_b = [], 0
    for kind, t in by_type.items():
        ra = _ceil_frac(a_frac, t["assigned"]) if t["assigned"] else 0
        ru = _ceil_frac(u_frac, t["unassigned"]) if t["unassigned"] else 0
        req = max(per_type, ra + ru)
        parts = []
        if t["assigned"]:
            parts.append(f"assigned {_pct(a_frac)} x {t['assigned']:,} = {_n(a_frac * t['assigned'])} -> {ra}")
        if t["unassigned"]:
            parts.append(f"unassigned {_pct(u_frac)} x {t['unassigned']:,} = {_n(u_frac * t['unassigned'])} -> {ru}")
        m = f"{kind}: " + "; ".join(parts) + f"; at least {per_type} for this facility type -> {req}"
        acc.math.append(m)
        test_b += req
        total = t["assigned"] + t["unassigned"]
        out.append(ParkingFacilityOut(name=f"{kind} ({', '.join(t['names'])})", total_spaces=total,
                                      ev_charging_spaces=t["ev"], counted_spaces=total, use="covered_multifamily",
                                      required_accessible=req, math=m))
    total_req = max(test_a, test_b)
    acc.math.append(f"Smallest total that passes every test = max({test_a}, {test_b}) = {total_req}")
    div = _divisor(P(A_VAN8, "formula_conservative"))
    vmin = int(P(A_VAN8, "min_van_spaces"))
    van_lit, van_cons = max(vmin, total_req // div), max(vmin, math.ceil(total_req / div))
    acc.math.append(f"Van-accessible: one in every {div}, not less than {vmin}: {total_req}/{div} = "
                    f"{_n(total_req / div)} -> literal minimum {van_lit}, rounded up {van_cons}")
    if van_lit != van_cons:
        acc.notes.append(f"CBC 1109A.8.6 says 'one in every {div} accessible spaces, but not less than {vmin}' and "
                         f"does not say how to count a partial group of {div}. Plan {van_cons} van spaces (rounded up) "
                         f"unless plan check accepts {van_lit}.")
    if unknown_assignment:
        acc.notes.append(f"Some facilities did not say whether spaces are assigned; they were counted as unassigned "
                         f"({_pct(u_frac)}, the higher rate). Set assigned=true for resident-assigned spaces.")
    if any(t["ev"] for t in by_type.values()):
        acc.notes.append("EV charging spaces were left in the 11A count: the verified rulebook states the EV exclusion "
                         "only for CBC 11B. Ask plan check.")
    acc.notes.append(f"Where parking is assigned, provide {P(A_PK, 'assigned_on_request')} (CBC 1109A, as cited "
                     "by LADBS).")
    d = lambda k: P(A_PK_DIM, k)                                             # noqa: E731
    opts = " or ".join(f"{_n(o['space_ft'])} ft space + {_n(o['aisle_ft'])} ft aisle" for o in P(A_VAN8, "options"))
    dims = [
        acc.req("Single accessible space", (
            f"{_n(d('single_space_total_width_min_ft'))} ft wide minimum ({_n(d('parking_area_width_ft'))} ft space + "
            f"{_n(d('aisle_width_ft'))} ft loading aisle), {_n(d('length_min_ft'))} ft long; slopes at most "
            f"{_n(d('surface_slope_max_pct'))}% in any direction."), A_PK_DIM),
        acc.req("Van-accessible space", (
            f"{_n(P(A_VAN8, 'total_width_min_ft'))} ft wide total ({opts}), aisle on the "
            f"{P(A_VAN8, 'aisle_side')}, {_n(P(A_VAN8, 'length_min_ft'))} ft long."), A_VAN8),
        acc.req("Vertical clearance", (
            f"{_n(d('vertical_clearance_min_in'))} in ({_ftin(d('vertical_clearance_min_in'))}) minimum in garages to "
            "the accessible spaces."), A_PK_DIM),
    ]
    signs = [acc.req("Access aisle marking", f"{_cap(d('aisle_marking'))}.", A_PK_DIM)]
    summary = (f"{total_req} accessible resident/visitor space{'s' if total_req != 1 else ''} for {units:,} covered "
               f"units, {van_cons} van-accessible.")
    nxt = (f"Show {total_req} accessible spaces ({van_cons} van) on the site plan, at least {per_type} in each type of "
           f"parking facility, on an accessible route to the dwelling entrances they serve.")
    res = ParkingResult(code="CBC 11A (covered multifamily)", use="covered_multifamily", work_type=wtype,
                        facilities=out, total_required_accessible=total_req, total_van_accessible=van_cons,
                        total_van_accessible_literal_minimum=van_lit, covered_dwelling_units=units, dimensions=dims,
                        signs_and_markings=signs, summary=summary, next_step=nxt)
    return acc.finish(res)


# ================================================================ field check (CBC 11B dimensions)
class Check(BaseModel):
    item: str
    measured: str
    required: str
    result: Literal["pass", "fail", "needs_info"]
    fix: Optional[str] = None
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


class FieldCheckResult(_Base):
    checks: list[Check] = Field(default_factory=list)
    passed: int = 0
    failed: int = 0
    needs_info: int = 0


def _check(acc: _Acc, item: str, measured: str, required: str, result: str, fix: Optional[str], *ids: str) -> Check:
    acc.use(*ids)
    ids = list(dict.fromkeys(ids))
    return Check(item=item, measured=measured, required=required, result=result,
                 fix=None if result == "pass" else fix, rule_ids=ids, citation=_cit(ids),
                 verified=all(core.rule(i).verified for i in ids))


def _in(v: float) -> str:
    return f"{_n(v)} in" if v < 12 or v != int(v) or int(v) % 12 else f"{_n(v)} in ({int(v) // 12} ft)"


def accessibility_field_check(door_clear_width=None, door_opening_depth=None, level_change_height=None,
                              level_change_beveled=None, ramp_running_slope=None, ramp_rise=None, ramp_run=None,
                              ramp_cross_slope=None, ramp_clear_width=None, curb_ramp_running_slope=None,
                              curb_ramp_flare_slope=None, walkway_clear_width=None, walkway_running_slope=None,
                              walkway_cross_slope=None, counter_height=None, counter_length=None,
                              checkout_counter=False, turning_space_diameter=None, parking_stall_width=None,
                              parking_aisle_width=None, parking_stall_length=None, van_space=False,
                              parking_vertical_clearance=None, parking_sign_bottom_height=None,
                              existing_building=False) -> FieldCheckResult:
    acc = _Acc()
    checks: list[Check] = []
    # ---- door (11B-404.2.3)
    dw, dd = _length("door_clear_width", door_clear_width), _length("door_opening_depth", door_opening_depth)
    if dd is not None and dw is None:
        raise ValueError("door_opening_depth needs door_clear_width")
    if dw is not None:
        base, deep, deep_w = (P(DOOR, "clear_width_min_in"), P(DOOR, "deep_opening_threshold_in"),
                              P(DOOR, "deep_opening_clear_width_min_in"))
        need = deep_w if dd is not None and dd > deep + EPS else base
        req = (f"{_n(need)} in clear minimum" + (f" (opening over {_n(deep)} in deep)" if need == deep_w else "")
               + (f"; {_n(deep_w)} in if the opening is more than {_n(deep)} in deep" if dd is None else ""))
        ok = dw >= need - EPS
        checks.append(_check(acc, "Door clear width", _in(dw), req, "pass" if ok else "fail",
                             f"Widen the opening to {_n(need)} in clear (offset hinges or a wider door).", DOOR))
    # ---- change in level (11B-303)
    lc = _length("level_change_height", level_change_height)
    bev = _bool("level_change_beveled", level_change_beveled)
    if lc is not None:
        vmax, (blo, bhi) = P(LEVEL, "vertical_max_in"), P(LEVEL, "beveled_range_in")
        ramp_over, bslope = P(LEVEL, "over_in_requires_ramp"), P(LEVEL, "bevel_max_slope")
        if lc <= vmax + EPS:
            result, req, fix = "pass", f"up to {_n(vmax)} in may be vertical", None
        elif lc <= bhi + EPS:
            req = f"{_n(blo)}-{_n(bhi)} in must be beveled no steeper than {bslope}"
            result = {True: "pass", False: "fail", None: "needs_info"}[bev]
            fix = f"Bevel the edge at {bslope} or flatter (grind or add a beveled threshold)."
        else:
            req = f"over {_n(ramp_over)} in must be ramped ({_join(P(LEVEL, 'ramp_sections'), 'or')})"
            result, fix = "fail", (f"Replace the step with a ramp or curb ramp meeting "
                                   f"{_join(P(LEVEL, 'ramp_sections'), 'or')} (max {P(RAMP, 'running_slope_max')}).")
        ids = (LEVEL,) if lc <= bhi + EPS else (LEVEL, RAMP)
        checks.append(_check(acc, "Change in level", _in(lc) + ("" if bev is None else
                                                                (", beveled" if bev else ", not beveled")),
                             req, result, fix, *ids))
    # ---- ramp (11B-405)
    rise, run = _length("ramp_rise", ramp_rise), _length("ramp_run", ramp_run)
    rs = _slope("ramp_running_slope", ramp_running_slope)
    if run is not None and rise is None:
        raise ValueError("ramp_run needs ramp_rise (to compute the slope)")
    if rise is not None and run is not None:
        if run <= 0:
            raise ValueError("ramp_run must be more than 0")
        if rs is not None and abs(rs - rise / run) > 0.0005:
            raise ValueError(f"ramp_running_slope {_slope_s(rs)} does not match ramp_rise/ramp_run = "
                             f"{_slope_s(rise / run)}; give one or the other")
        rs = rise / run
        acc.math.append(f"Ramp slope = rise {_n(rise)} in / run {_n(run)} in = {_slope_s(rs)}")
    if rs is not None:
        mx = _ratio(P(RAMP, "running_slope_max"))
        ok = rs <= mx + EPS
        fix = (f"Lengthen the run to at least {_n(rise / mx)} in ({_n(rise / mx / 12)} ft) for this {_n(rise)} in "
               "rise, or lower the rise." if rise is not None
               else f"Regrade to {P(RAMP, 'running_slope_max')} or flatter.")
        ids = [RAMP]
        if not ok and _bool("existing_building", existing_building):
            rows = [r for r in P(S_STEEP, "table") if rs <= _ratio(r["slope_not_steeper_than"]) + EPS
                    and (rise is None or rise <= r["max_rise_in"] + EPS)]
            if rows:
                r = min(rows, key=lambda r: r["max_rise_in"])
                ids.append(S_STEEP)
                fix += (f" Federally (2010 Standards 405.2), an existing site or building with no room for a "
                        f"{P(RAMP, 'running_slope_max')} ramp may use up to {r['slope_not_steeper_than']} for a rise "
                        f"of {_n(r['max_rise_in'])} in or less"
                        + ("" if rise is not None else " (give ramp_rise to confirm)")
                        + "; whether CBC 11B allows the same was not verified, so ask plan check first.")
        checks.append(_check(acc, "Ramp running slope", _slope_s(rs),
                             f"{P(RAMP, 'running_slope_max')} ({_n(P(RAMP, 'running_slope_max_pct'))}%) maximum",
                             "pass" if ok else "fail", fix, *ids))
    if rise is not None:
        rmax = P(RAMP, "rise_per_run_max_in")
        checks.append(_check(acc, "Ramp rise per run", _in(rise), f"{_n(rmax)} in maximum per run",
                             "pass" if rise <= rmax + EPS else "fail",
                             f"Break the ramp into runs of {_n(rmax)} in rise or less with a landing at the "
                             f"{P(RAMP, 'landings')}.", RAMP))
    cs = _slope("ramp_cross_slope", ramp_cross_slope)
    if cs is not None:
        mx = _ratio(P(RAMP, "cross_slope_max"))
        checks.append(_check(acc, "Ramp cross slope", _slope_s(cs),
                             f"{P(RAMP, 'cross_slope_max')} ({_n(P(RAMP, 'cross_slope_max_pct'))}%) maximum",
                             "pass" if cs <= mx + EPS else "fail", "Regrade the ramp surface side to side.", RAMP))
    rw = _length("ramp_clear_width", ramp_clear_width)
    if rw is not None:
        mn = P(RAMP, "clear_width_min_in")
        checks.append(_check(acc, "Ramp clear width", _in(rw), f"{_n(mn)} in minimum", "pass" if rw >= mn - EPS
                             else "fail", f"Widen the ramp run to {_n(mn)} in clear.", RAMP))
    # ---- curb ramp (11B-406)
    crs, cfl = _slope("curb_ramp_running_slope", curb_ramp_running_slope), _slope("curb_ramp_flare_slope",
                                                                                   curb_ramp_flare_slope)
    if crs is not None:
        mx = _ratio(P(CURB, "running_slope_max"))
        checks.append(_check(acc, "Curb ramp running slope", _slope_s(crs), f"{P(CURB, 'running_slope_max')} maximum",
                             "pass" if crs <= mx + EPS else "fail",
                             f"Rebuild the curb ramp at {P(CURB, 'running_slope_max')} or flatter, with a "
                             f"{_n(P(CURB, 'bottom_turning_space_in')[0])} x "
                             f"{_n(P(CURB, 'bottom_turning_space_in')[1])} in turning space at the bottom sloped "
                             f"{P(CURB, 'turning_space_slope_max')} max.", CURB))
    if cfl is not None:
        mx = _ratio(P(CURB, "flare_slope_max"))
        checks.append(_check(acc, "Curb ramp flare slope", _slope_s(cfl), f"{P(CURB, 'flare_slope_max')} maximum",
                             "pass" if cfl <= mx + EPS else "fail",
                             f"Rebuild the flares at {P(CURB, 'flare_slope_max')} or flatter.", CURB))
    # ---- site walkway (11B-403)
    ww = _length("walkway_clear_width", walkway_clear_width)
    wrs = _slope("walkway_running_slope", walkway_running_slope)
    wcs = _slope("walkway_cross_slope", walkway_cross_slope)
    if ww is not None:
        mn = P(WALK, "walkway_width_min_in")
        checks.append(_check(acc, "Site walkway width", _in(ww), f"{_n(mn)} in minimum", "pass" if ww >= mn - EPS
                             else "fail", f"Widen the walk to {_n(mn)} in.", WALK))
    if wrs is not None:
        mx = P(WALK, "running_slope_max_pct") / 100
        ok = wrs <= mx + EPS
        checks.append(_check(acc, "Site walkway running slope", _slope_s(wrs), f"{_n(mx * 100)}% maximum",
                             "pass" if ok else "fail",
                             f"Regrade to {_n(mx * 100)}% or less, or build that run as a ramp to CBC 11B-405 "
                             f"({P(RAMP, 'running_slope_max')} max, {_n(P(RAMP, 'clear_width_min_in'))} in wide, "
                             f"{_n(P(RAMP, 'rise_per_run_max_in'))} in max rise per run).",
                             *((WALK,) if ok else (WALK, RAMP))))
    if wcs is not None:
        mx = P(WALK, "cross_slope_max_pct") / 100
        checks.append(_check(acc, "Site walkway cross slope", _slope_s(wcs), f"{_n(mx * 100)}% maximum",
                             "pass" if wcs <= mx + EPS else "fail", f"Regrade the walk to {_n(mx * 100)}% cross "
                             "slope or less.", WALK))
    # ---- sales / service counter (11B-904.4)
    ch, cl = _length("counter_height", counter_height), _length("counter_length", counter_length)
    checkout = bool(_bool("checkout_counter", checkout_counter))
    if ch is not None:
        mx = P(COUNTER, "checkout_counter_height_max_in" if checkout else "accessible_height_max_in")
        checks.append(_check(acc, "Checkout counter height" if checkout else "Sales/service counter height", _in(ch),
                             f"{_n(mx)} in maximum above the floor", "pass" if ch <= mx + EPS else "fail",
                             f"Lower a section to {_n(mx)} in or less.", COUNTER))
    if cl is not None:
        mn = P(COUNTER, "accessible_length_min_in")
        checks.append(_check(acc, "Accessible counter length", _in(cl), f"{_n(mn)} in minimum", "pass"
                             if cl >= mn - EPS else "fail", f"Make the lowered section {_n(mn)} in long, with clear "
                             "floor space for a parallel approach alongside it.", COUNTER))
    # ---- turning space (11B-304)
    ts = _length("turning_space_diameter", turning_space_diameter)
    if ts is not None:
        mn = P(TURN, "circle_diameter_in")
        checks.append(_check(acc, "Turning space", f"{_n(ts)} in circle", (
            f"{_n(mn)} in diameter circle, or a T-shape in a {_n(P(TURN, 't_shape_square_in'))} in square with "
            f"{_n(P(TURN, 't_arm_and_base_width_min_in'))} in arms and base"),
            "pass" if ts >= mn - EPS else "fail",
            f"Clear a {_n(mn)} in circle or lay out the T-shaped space.", TURN))
    # ---- parking stall (11B-502)
    pw = _length("parking_stall_width", parking_stall_width, "ft")
    pa = _length("parking_aisle_width", parking_aisle_width, "ft")
    pl = _length("parking_stall_length", parking_stall_length, "ft")
    van = bool(_bool("van_space", van_space))
    ft = lambda k: P(PK_STALL, k) * 12                                       # noqa: E731
    if pw is not None:
        if van:
            ok12 = pw >= ft("van_width_min_ft") - EPS and (pa is None or pa >= ft("van_aisle_min_ft_with_12ft_stall")
                                                           - EPS)
            ok9 = pw >= ft("van_width_alt_min_ft") - EPS and pa is not None and pa >= ft(
                "van_aisle_min_ft_with_9ft_stall") - EPS
            need = (f"{_n(P(PK_STALL, 'van_width_min_ft'))} ft with a "
                    f"{_n(P(PK_STALL, 'van_aisle_min_ft_with_12ft_stall'))} ft aisle, or "
                    f"{_n(P(PK_STALL, 'van_width_alt_min_ft'))} ft with an "
                    f"{_n(P(PK_STALL, 'van_aisle_min_ft_with_9ft_stall'))} ft aisle")
            if ok9 or (ok12 and pa is not None):
                result = "pass"
            elif pa is None and pw >= ft("van_width_alt_min_ft") - EPS:
                result = "needs_info"                       # wide enough for one of the two layouts; aisle unknown
            else:
                result = "fail"
            measured = f"{_n(pw / 12)} ft space" + (f", {_n(pa / 12)} ft aisle" if pa is not None else "")
            checks.append(_check(acc, "Van space width", measured, need, result,
                                 f"Re-stripe to {need}." if result == "fail" else "Measure the access aisle too.",
                                 PK_STALL))
        else:
            mn = ft("car_width_min_ft")
            checks.append(_check(acc, "Car space width", f"{_n(pw / 12)} ft", f"{_n(mn / 12)} ft minimum",
                                 "pass" if pw >= mn - EPS else "fail", f"Re-stripe to {_n(mn / 12)} ft wide.",
                                 PK_STALL))
    if pa is not None and not (van and pw is not None):
        mn = ft("access_aisle_min_ft")
        checks.append(_check(acc, "Access aisle width", f"{_n(pa / 12)} ft", f"{_n(mn / 12)} ft minimum",
                             "pass" if pa >= mn - EPS else "fail", f"Re-stripe the aisle to {_n(mn / 12)} ft.",
                             PK_STALL))
    if pl is not None:
        mn = ft("stall_length_min_ft")
        checks.append(_check(acc, "Accessible space length", f"{_n(pl / 12)} ft", f"{_n(mn / 12)} ft minimum",
                             "pass" if pl >= mn - EPS else "fail", f"Lengthen the space to {_n(mn / 12)} ft.",
                             PK_STALL))
    pv = _length("parking_vertical_clearance", parking_vertical_clearance)
    if pv is not None:
        mn = P(PK_VERT, "vertical_clearance_min_in")
        checks.append(_check(acc, "Parking vertical clearance", _in(pv),
                             f"{P(PK_VERT, 'vertical_clearance_text')} ({_n(mn)} in) minimum",
                             "pass" if pv >= mn - EPS else "fail",
                             f"Raise the lowest obstruction (pipes, sprinklers, signs, beams) to {_n(mn)} in at the "
                             "accessible spaces and along one vehicle route to them.", PK_VERT))
    sb = _length("parking_sign_bottom_height", parking_sign_bottom_height)
    if sb is not None:
        mn = P(PK_SIGN, "sign_bottom_height_min_in")
        checks.append(_check(acc, "Parking sign height", f"{_n(sb)} in to the bottom of the sign",
                             f"{_n(mn)} in minimum to the bottom", "pass" if sb >= mn - EPS else "fail",
                             f"Remount the sign with its bottom at least {_n(mn)} in above the ground.", PK_SIGN))
    if not checks:
        raise ValueError("give at least one measurement, e.g. door_clear_width=31.5 or ramp_rise=24 with ramp_run=264")
    passed = sum(c.result == "pass" for c in checks)
    failed = sum(c.result == "fail" for c in checks)
    info = sum(c.result == "needs_info" for c in checks)
    summary = f"{passed} of {len(checks)} measured items meet CBC 11B" + (f"; {failed} fail" if failed else "") + (
        f"; {info} need more information" if info else "") + "."
    fails = [c for c in checks if c.result == "fail"]
    if fails:
        nxt = f"Fix before the inspection: {fails[0].item}: {fails[0].fix}" + (
            f" (and {len(fails) - 1} more below)" if len(fails) > 1 else "")
    elif info:
        nxt = next(c.fix for c in checks if c.result == "needs_info")
    else:
        nxt = "All measured items meet the CBC 11B values; record the readings with the inspection paperwork."
    acc.notes.append(f"Values are the CBC 11B figures as restated by LADBS for the CBC effective "
                     f"{core.rule(DOOR).effective}; the CBC text itself was not read. Measure to finished surfaces.")
    res = FieldCheckResult(checks=checks, passed=passed, failed=failed, needs_info=info, summary=summary,
                           next_step=nxt)
    return acc.finish(res)


# ================================================================ which rules apply
ProjectType = Literal["new_construction", "alteration", "addition", "barrier_removal", "maintenance_only"]
BuildingUse = Literal["public_accommodation", "commercial_facility", "public_building", "multifamily",
                      "public_housing", "mixed_use"]
_PROJECT = (  # a phrase starting with "=" must be the whole answer ("new"), not part of it ("new restrooms")
    ("new_construction", ("new construction", "new building", "=new", "ground up", "ground up construction")),
    ("addition", ("addition", "additions", "add on", "expansion")),
    ("alteration", ("alteration", "alterations", "remodel", "renovation", "tenant improvement", "ti", "t i",
                    "rehabilitation", "structural repair", "reconstruction")),
    ("barrier_removal", ("barrier removal", "barrier", "readily achievable")),
    ("maintenance_only", ("maintenance", "reroof", "reroofing", "painting", "repair")),
)
_USE = (
    ("mixed_use", ("mixed use", "mixed")),
    ("public_housing", ("public housing",)),
    ("multifamily", ("multifamily", "multi family", "apartment", "apartments", "condo", "condos", "condominium",
                     "condominiums", "residential")),
    ("public_building", ("public building", "government", "city hall", "public agency", "fire station",
                         "county building", "state building")),
    ("public_accommodation", ("public accommodation", "retail", "store", "shop", "restaurant", "bank", "hotel",
                              "medical office", "clinic", "gym")),
    ("commercial_facility", ("commercial facility", "commercial", "office", "warehouse", "industrial", "factory")),
)
_SPECIAL = (
    ("shopping_center", ("shopping center", "shopping mall", "mall", "strip mall", "shopping")),
    ("health_care_office", ("health care", "healthcare", "medical", "doctor", "dental", "dentist", "clinic",
                            "health care provider")),
    ("transit_station", ("transit", "bus station", "train station", "depot", "rail station", "station")),
    ("airport_terminal", ("airport", "airport terminal", "air terminal")),
)


def _pick(value, table, field: str, default: Optional[str] = None) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            return None
        value = default
    w = _words(value)
    keys = [k for k, _ in table]
    if w.replace(" ", "_") in keys:
        return w.replace(" ", "_")
    for key, phrases in table:                              # whole-answer matches first
        if any(w == p.lstrip("=") for p in phrases):
            return key
    for key, phrases in table:
        if any(not p.startswith("=") and _has(w, p) for p in phrases):
            return key
    raise ValueError(f"{field} must be one of {', '.join(keys)}; got {value!r}")


class Regime(BaseModel):
    code: str
    applies: Optional[bool] = Field(description="None = depends on a fact or rule not available; see why")
    why: str
    requirements: list[Requirement] = Field(default_factory=list)


class TriggersResult(_Base):
    project_type: str
    building_use: str
    dwelling_units: int = 0
    regimes: list[Regime] = Field(default_factory=list)
    fha_covered_units: Optional[int] = None
    cbc_11a_accessible_multistory_units: Optional[int] = None
    public_housing_mobility_units: Optional[int] = None
    public_housing_communication_units: Optional[int] = None
    federal_elevator_exempt: Optional[bool] = Field(None, description=(
        "ADA Title III: True = no elevator required (28 CFR 36.404 / 36.401(d)); None = not evaluated"))
    accessible_public_entrances_min: Optional[int] = None
    general: list[Requirement] = Field(default_factory=list)
    penalties: list[Requirement] = Field(default_factory=list)
    other_tools: list[str] = Field(default_factory=list)


def accessibility_triggers(project_type: str, building_use: str, dwelling_units=0, has_elevator=None,
                           ground_floor_units=None, multistory_units=None, publicly_funded=False, public_entity=False,
                           stories=None, sq_ft_per_story=None, special_facility=None,
                           sales_or_rental_establishments=None, public_entrances=None, first_occupied_date=None,
                           as_of=None) -> TriggersResult:
    acc = _Acc()
    ptype = _pick(project_type, _PROJECT, "project_type")
    use = _pick(building_use, _USE, "building_use")
    today = _date(as_of, "as_of", default=logic.today_ca())
    units = _count("dwelling_units", dwelling_units) or 0
    ground = _count("ground_floor_units", ground_floor_units)
    multi = _count("multistory_units", multistory_units)
    elevator = _bool("has_elevator", has_elevator)
    funded = bool(_bool("publicly_funded", publicly_funded))
    gov = bool(_bool("public_entity", public_entity))
    nstories = _count("stories", stories)
    sqft = _num("sq_ft_per_story", sq_ft_per_story)
    special = _pick(special_facility, _SPECIAL, "special_facility")
    shops = _count("sales_or_rental_establishments", sales_or_rental_establishments)
    entrances = _count("public_entrances", public_entrances)
    first_occ = None if first_occupied_date in (None, "") else _date(first_occupied_date, "first_occupied_date")
    residential = use in ("multifamily", "public_housing", "mixed_use")
    if residential and units < 1:
        raise ValueError(f"dwelling_units must be at least 1 for building_use {use}")
    if not residential and units:
        raise ValueError(f"dwelling_units is for multifamily, public_housing or mixed_use buildings, not {use}")
    for name, v in (("ground_floor_units", ground), ("multistory_units", multi)):
        if v is not None and v > units:
            raise ValueError(f"{name} ({v}) cannot exceed dwelling_units ({units})")
    if nstories == 0:
        raise ValueError("stories must be at least 1")
    nonres = use in ("public_accommodation", "commercial_facility", "public_building", "mixed_use")
    title_ii = gov or use in ("public_building", "public_housing")
    title_iii = use in ("public_accommodation", "commercial_facility", "mixed_use") and not gov
    altering = ptype in ("alteration", "addition", "barrier_removal")
    regimes: list[Regime] = []
    tools: list[str] = []

    # ---------------- CBC 11B
    reqs: list[Requirement] = []
    if use == "public_housing" or (residential and use != "mixed_use" and funded):
        acc.use(PUB_MF)
        if use == "public_housing":
            acc.use(PH_DEF)
        if use == "public_housing":
            why11b = f"LADBS (CBC 1101A.1): public housing is under {P(PUB_MF, 'public_housing')}."
        else:
            why11b = (f"LADBS (CBC 1101A.1): new publicly funded multifamily is under "
                      f"{P(PUB_MF, 'new_publicly_funded_multifamily')}; existing publicly funded multifamily under "
                      f"{P(PUB_MF, 'existing_publicly_funded_multifamily')}.")
        applies11b = True
    elif nonres:
        why11b = ("CBC Chapter 11B covers public accommodations, commercial buildings and public buildings: new "
                  "construction, additions, alterations and structural repairs.")
        applies11b = True
    else:
        why11b = ("Privately funded multifamily housing is under CBC Chapter 11A; 11B covers public accommodations, "
                  "commercial and public buildings, public housing and publicly funded multifamily.")
        applies11b = False
    if applies11b:
        if ptype == "new_construction" and use != "public_housing":
            for rid in (DOOR, RAMP, WALK, LEVEL, PK_FACILITY):
                reqs.append(acc.rule(rid))
            tools += ["accessibility_field_check", "accessible_parking_requirements"]
        if altering:
            reqs.append(acc.rule(ALTERED))
            reqs.append(acc.rule(POT_REQ, extra=("Path-of-travel elements: "
                                                 f"{_join(P(POT_ELEMENTS, 'path_of_travel_elements'))}.")))
            acc.use(POT_ELEMENTS)
            tools.append("path_of_travel_obligation")
        if ptype == "maintenance_only":
            acc.notes.append("No verified CBC rule covers maintenance-only work; ask plan check whether the work is an "
                             "alteration (CBC 11B-202.3 covers renovation, structural repair and alteration).")
    ph_mob = ph_com = None
    if use == "public_housing" and ptype == "new_construction":
        acc.use(PH_UNITS)
        mf, cf, mn = (float(P(PH_UNITS, "mobility_unit_fraction")), float(P(PH_UNITS, "communication_unit_fraction")),
                      int(P(PH_UNITS, "min_units_each")))
        ph_mob, ph_com = max(mn, _ceil_frac(mf, units)), max(mn, _ceil_frac(cf, units))
        acc.math.append(f"Mobility units = max({mn}, {_pct(mf)} x {units} = {_n(mf * units)} rounded up) = {ph_mob}")
        acc.math.append(f"Communication units = max({mn}, {_pct(cf)} x {units} = {_n(cf * units)} rounded up) = "
                        f"{ph_com}")
        adapt = int(P(PH_UNITS, "adaptable_units_threshold_units"))
        reqs.append(acc.req("Public housing units (CBC 11B-233)", (
            f"At least {ph_mob} mobility unit(s) ({_pct(mf)}, at least {mn}) on an accessible route and {ph_com} "
            f"communication-feature unit(s) ({_pct(cf)}, at least {mn})"
            + (f"; with {units} units (>= {adapt}) adaptable units are also required." if units >= adapt else ".")),
            PH_UNITS))
    regimes.append(Regime(code="CBC Chapter 11B", applies=applies11b, why=why11b, requirements=reqs))

    # ---------------- CBC 11A
    if residential and use != "public_housing":
        reqs = []
        acc.use(A_APPLY)
        mult11 = None
        if ptype == "new_construction":
            why = ("CBC Chapter 11A applies to all newly constructed covered multifamily dwellings and their new "
                   "common-use areas. The unit count in the CBC definition of 'covered multifamily dwellings' was not "
                   "in the verified rulebook, so confirm coverage with the building department"
                   + (" (LADBS lists new publicly funded multifamily under 11B)." if funded else "."))
            for rid in (A_DOORS, A_SITE, A_PK, A_TEMP):
                reqs.append(acc.rule(rid))
            if elevator is True:
                reqs.append(acc.rule(A_MULTI_ELEV))
            elif elevator is False:
                reqs.append(acc.rule(A_GROUND))
                reqs.append(acc.rule(A_IMPRACT))
                if multi:
                    frac, mn = float(P(A_MULTI10, "fraction")), int(P(A_MULTI10, "min_units"))
                    mult11 = max(mn, _ceil_frac(frac, multi))
                    acc.math.append(f"CBC 1102A.3.1: max({mn}, {_pct(frac)} x {multi} multistory units = "
                                    f"{_n(frac * multi)} rounded up) = {mult11}")
                    reqs.append(acc.req("Multistory units, no elevator (CBC 1102A.3.1)", (
                        f"At least {mult11} of the {multi} multistory units need an accessible primary entry, an "
                        f"accessible powder room or bathroom and rooms on an accessible route on the entry level "
                        f"({_pct(frac)}, rounded up, at least {mn}; carriage units excepted)."), A_MULTI10))
            tools.append("accessible_parking_requirements")
        else:
            why = ("For existing buildings CBC 11A reaches additions that alone meet the covered-multifamily "
                   "definition, new common-use areas, and buildings gutted to the shell (treated as new); keep "
                   "existing access features as built.")
            reqs.append(acc.rule(A_APPLY))
            reqs.append(acc.rule(A_MAINTAIN))
            reqs.append(acc.rule(A_TEMP))
        regimes.append(Regime(code="CBC Chapter 11A", applies=None, why=why, requirements=reqs))
    else:
        mult11 = None

    # ---------------- Fair Housing Act
    fha_units = None
    if residential:
        acc.use(FHA_COVERED, FHA_DATE)
        min_units = int(P(FHA_COVERED, "min_units_per_building"))
        cutoff = _date(P(FHA_DATE, "first_occupancy_after"), "rule date")
        reqs = []
        if units < min_units:
            applies, why = False, (f"{units} dwelling unit(s): the FHA design rules cover buildings of {min_units} or "
                                   "more units (count each building separately).")
        else:
            if ptype == "new_construction":
                applies, when = True, f"designed and built for first occupancy after {cutoff}"
            elif first_occ is not None:
                applies = first_occ > cutoff
                when = (f"first occupied {first_occ}, after {cutoff}: it had to be built to these rules; keep them"
                        if applies else f"first occupied {first_occ}, on or before {cutoff}: the design rules do not "
                                        "apply")
            else:
                applies, when = None, (f"applies if the building was first occupied after {cutoff} (and its last "
                                       f"permit was issued after "
                                       f"{P(FHA_DATE, 'exempt_if_last_permit_or_renewal_issued_on_or_before')}); give "
                                       "first_occupied_date")
            if elevator is True:
                fha_units = units
                scope = f"elevator building: all {units} units are covered"
            elif elevator is False:
                fha_units = ground
                scope = (f"no elevator: the {ground} ground-floor unit(s) are covered" if ground is not None else
                         "no elevator: the ground-floor units are covered (give ground_floor_units)")
                acc.use(FHA_FIRST_DEF)
            else:
                scope = "give has_elevator: with an elevator every unit is covered, without one the ground-floor units"
            why = f"{units} units ({scope}); {when}."
            if applies is not False:
                for rid in (FHA_ENTRANCE, FHA_COMMON, FHA_UNIT, FHA_SAFE, FHA_LOCAL):
                    reqs.append(acc.rule(rid))
        regimes.append(Regime(code="Fair Housing Act (24 CFR 100.205)", applies=applies, why=why, requirements=reqs))

    # ---------------- ADA Title III
    elev_exempt = None
    if title_iii:
        reqs = []
        if ptype == "new_construction":
            acc.use(F_NEW, F_2010_DATE)
            reqs.append(acc.rule(F_NEW))
            reqs.append(acc.rule(F_IMPRACT))
            why = (f"New public accommodation or commercial facility first occupied after "
                   f"{P(F_NEW, 'first_occupancy_after')}: designed and built readily accessible under the "
                   f"{_standards(acc, False, today).split(' (')[0]}.")
        elif altering:
            reqs.append(acc.rule(F_ALTER))
            reqs.append(acc.rule(F_POT))
            reqs.append(acc.rule(S_NO_REDUCE))
            reqs.append(acc.rule(F_MAX_FEAS))
            why = ("Alterations to a public accommodation or commercial facility must make the altered portions "
                   "accessible.")
            if ptype == "barrier_removal":
                if use == "commercial_facility":
                    why = ("The readily achievable barrier-removal duty (28 CFR 36.304) is for public accommodations; "
                           "a commercial facility not open to the public does not have it. Any alteration made must "
                           "still comply.")
                else:
                    reqs.append(acc.rule(F_BARRIER))
                    reqs.append(acc.rule(F_BARRIER_NO_POT))
                    why = "Existing public accommodation: remove barriers where readily achievable."
            acc.use(F_2010_DATE)
            if "path_of_travel_obligation" not in tools:
                tools.append("path_of_travel_obligation")
        else:
            reqs.append(acc.rule(F_WHAT))
            why = "Maintenance-type work is not an alteration unless it affects usability."
            if use != "commercial_facility":
                reqs.append(acc.rule(F_BARRIER))
        if nstories is not None and sqft is not None and ptype != "maintenance_only":
            acc.use(F_ELEV)
            smin, amin = int(P(F_ELEV, "stories_less_than")), float(P(F_ELEV, "sq_ft_per_story_less_than"))
            mall = special == "shopping_center" or (shops is not None
                                                    and shops >= int(P(F_ELEV, "shopping_center_min_establishments")))
            excluded = mall or special in ("health_care_office", "transit_station", "airport_terminal")
            small = nstories < smin or sqft < amin
            elev_exempt = small and not excluded
            acc.math.append(f"Elevator exemption: fewer than {smin} stories? {nstories} -> "
                            f"{'yes' if nstories < smin else 'no'}; under {_n(amin)} sq ft per story? {_n(sqft)} -> "
                            f"{'yes' if sqft < amin else 'no'}; "
                            + ("exemption not available for this facility type" if excluded and small else
                               "exempt" if small else "not exempt"))
            reqs.append(acc.req("Elevator (28 CFR 36.404 / 36.401(d))", (
                "No elevator is required under the federal exemption." if elev_exempt else
                ("An elevator is required federally: the small-building exemption is not available for "
                 f"{_join(P(F_ELEV, 'exemption_not_available_for'), 'or')}." if excluded and small else
                 f"The federal small-building exemption does not apply ({smin}+ stories and {_n(amin)}+ sq ft per "
                 "story); plan an elevator for multi-story access."))
                + " CBC 11B-206.2.3 has its own exceptions (not in the verified rulebook): confirm with plan check.",
                F_ELEV))
        acc.use(F_PENALTY)
        regimes.append(Regime(code="ADA Title III (28 CFR part 36)", applies=True, why=why, requirements=reqs))
    if sqft is not None and sqft > P(VERT200, "floor_area_trigger_sq_ft") and ptype == "new_construction" and nonres:
        regimes[0].requirements.append(acc.req("Vertical access within 200 ft (CBC 11B-206.2.3.2)", (
            f"If elevators are required and any floor exceeds {_n(P(VERT200, 'floor_area_trigger_sq_ft'))} sq ft, "
            f"provide a ramp, elevator or lift within {_n(P(VERT200, 'max_travel_distance_ft'))} ft of travel of each "
            "stair and escalator."), VERT200))

    # ---------------- ADA Title II
    if title_ii:
        acc.use(T2_2010_DATE)
        reqs = []
        if altering:
            reqs.append(acc.rule(T2_20))
            reqs.append(acc.rule(T2_PROGRAM))
            if "path_of_travel_obligation" not in tools:
                tools.append("path_of_travel_obligation")
        why = (f"State or local government facility: {_standards(acc, True, today)}."
               + (" Public housing is housing built or altered by, for, or on behalf of a public entity "
                  f"({P(PH_DEF, 'federal_link')})." if use == "public_housing" else ""))
        regimes.append(Regime(code="ADA Title II (28 CFR 35.151)", applies=True, why=why, requirements=reqs))

    # ---------------- general items, penalties
    general = [acc.rule(PLAN_CHECK, "Plan check does not review the ADA or Fair Housing Act")]
    if ptype in ("new_construction", "alteration", "addition") and (title_ii or title_iii):
        general.append(acc.rule(S_SITES, "Your jobsite trailers and crew toilets are exempt"))
    entr_min = None
    if entrances and (title_ii or title_iii) and ptype == "new_construction":
        frac = float(P(S_ENTRANCES, "public_entrances_accessible_min_fraction"))
        entr_min = _ceil_frac(frac, entrances)
        acc.math.append(f"Public entrances: {_pct(frac)} x {entrances} = {_n(frac * entrances)}, rounded up "
                        f"{entr_min}")
        general.append(acc.req("Public entrances (2010 ADA Standards 206.4.1)", (
            f"At least {entr_min} of {entrances} public entrances accessible ({_pct(frac)}, rounded up), plus the "
            "specific entrances 206.4.2-206.4.9 require. Check CBC 11B for California's entrance rules."),
            S_ENTRANCES))
    pens = []
    if title_iii:
        pens.append(acc.req("ADA Title III civil penalty (DOJ lawsuit)", (
            f"Up to {_usd_s(P(F_PENALTY, 'first_violation_max_usd'))} for a first violation and "
            f"{_usd_s(P(F_PENALTY, 'subsequent_violation_max_usd'))} for a later one (assessed after "
            f"{P(F_PENALTY, 'applies_to_penalties_assessed_after')})."), F_PENALTY))
    if any(r.code.startswith("Fair Housing") and r.applies is not False for r in regimes):
        acc.use(FHA_HUD, FHA_DOJ)
        k1, k2 = "max_usd_one_prior_within_5_years", "max_usd_two_or_more_priors_within_7_years"
        pens.append(acc.req("Fair Housing Act, HUD administrative case", (
            f"Per discriminatory housing practice up to {_usd_s(P(FHA_HUD, 'max_usd_no_prior'))} (no prior), "
            f"{_usd_s(P(FHA_HUD, k1))} (one prior in {_years_in(k1)} years) or "
            f"{_usd_s(P(FHA_HUD, k2))} (two or more in {_years_in(k2)} years), from "
            f"{P(FHA_HUD, 'effective_per_ecfr')}."), FHA_HUD))
        pens.append(acc.req("Fair Housing Act, DOJ civil action", (
            f"Up to {_usd_s(P(FHA_DOJ, 'first_violation_max_usd'))} first and "
            f"{_usd_s(P(FHA_DOJ, 'subsequent_violation_max_usd'))} subsequent violation (assessed after "
            f"{P(FHA_DOJ, 'applies_to_penalties_assessed_after')})."), FHA_DOJ))
    if funded:
        acc.notes.append("Federal funding can bring Section 504 / UFAS requirements; those are not in the verified "
                         "rulebook and are not covered here.")
    if residential and use != "public_housing":
        tools.append("casp_lease_disclosure")
    elif nonres:
        tools.append("casp_lease_disclosure")
    applying = [r.code for r in regimes if r.applies]
    maybe = [r.code for r in regimes if r.applies is None]
    summary = ("Applies: " + (_join(applying) if applying else "none of the listed codes")
               + (f". Check: {_join(maybe)}" if maybe else "") + ".")
    counts = []
    if fha_units is not None:
        counts.append(f"{fha_units} FHA-covered unit(s)")
    if mult11 is not None:
        counts.append(f"{mult11} CBC 11A accessible multistory unit(s)")
    if ph_mob is not None:
        counts.append(f"{ph_mob} mobility and {ph_com} communication unit(s)")
    if counts:
        summary += " " + "; ".join(counts) + "."
    first_tool = next((t for t in ("path_of_travel_obligation", "accessible_parking_requirements",
                                   "accessibility_field_check") if t in tools), None)
    nxt = ("Have the architect show compliance with " + (_join(applying + maybe) or "the applicable codes")
           + " on the permit set (plan check will not check the federal rules)"
           + (f"; then run {first_tool}." if first_tool else "."))
    res = TriggersResult(project_type=ptype, building_use=use, dwelling_units=units, regimes=regimes,
                         fha_covered_units=fha_units, cbc_11a_accessible_multistory_units=mult11,
                         public_housing_mobility_units=ph_mob, public_housing_communication_units=ph_com,
                         federal_elevator_exempt=elev_exempt, general=general, penalties=pens,
                         accessible_public_entrances_min=entr_min, other_tools=list(dict.fromkeys(tools)),
                         summary=summary, next_step=nxt)
    return acc.finish(res)


# ================================================================ CASp lease statements and license fee
class Clause(BaseModel):
    clause: str
    text: str
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False
    caution: Optional[str] = None


class SanDiegoLicenseExample(BaseModel):
    business_tax_usd: float
    business_tax_applies_to: str
    state_disability_access_fee_usd: float
    total_before_wage_fee_usd: float
    minimum_wage_enforcement_fee_per_employee_usd: list[float]
    minimum_wage_fee_from: str
    employees: Optional[int] = None
    estimated_total_usd: Optional[list[float]] = Field(None, description="[low, high] using the two City figures")
    note: str


class LeaseResult(_Base):
    role: Literal["landlord", "tenant"]
    property_type: Literal["commercial", "residential"]
    premises_inspected_by_casp: Optional[bool] = None
    lease_clauses: list[Clause] = Field(default_factory=list)
    rights_and_duties: list[Requirement] = Field(default_factory=list)
    business_license_fee_usd: Optional[float] = None
    business_license: list[Requirement] = Field(default_factory=list)
    san_diego_example: Optional[SanDiegoLicenseExample] = None


def _clause(acc: _Acc, name: str, text: str, ids, caution: Optional[str] = None) -> Clause:
    acc.use(*ids)
    ids = list(dict.fromkeys(ids))
    return Clause(clause=name, text=text, rule_ids=ids, citation=_cit(ids),
                  verified=all(core.rule(i).verified for i in ids), caution=caution)


def _role(value) -> str:
    w = _words(value or "landlord")
    if w in ("landlord", "lessor", "owner", "property owner", "building owner", "commercial property owner"):
        return "landlord"
    if w in ("tenant", "lessee", "renter", "occupant", "business tenant"):
        return "tenant"
    raise ValueError(f"role must be landlord (lessor, owner) or tenant (lessee, renter); got {value!r}")


def casp_lease_disclosure(role: str = "landlord", premises_inspected_by_casp=None, lessor_name: Optional[str] = None,
                          property_type: str = "commercial", business_license_city: Optional[str] = None,
                          employees=None, tenant_improvements_planned: bool = False) -> LeaseResult:
    acc = _Acc()
    who = _role(role)
    pt = _words(property_type or "commercial")
    if pt in ("commercial", "retail", "office", "industrial", "shop", "yard", "warehouse", "commercial lease"):
        ptype = "commercial"
    elif pt in ("residential", "apartment", "home", "house", "dwelling", "condo", "residential lease"):
        ptype = "residential"
    else:
        raise ValueError(f"property_type must be commercial or residential; got {property_type!r}")
    inspected = _bool("premises_inspected_by_casp", premises_inspected_by_casp)
    staff = _count("employees", employees)
    ti = bool(_bool("tenant_improvements_planned", tenant_improvements_planned))
    lessor = (lessor_name or "Landlord").strip() or "Landlord"
    clauses: list[Clause] = []
    rights: list[Requirement] = []
    if ptype == "commercial":
        quote = core.rule(LEASE_STATUS).quote or ""
        m = re.match(r"(In accordance with California Civil Code section 1938, )CITY( hereby states that the Premises )"
                     r"has not been( inspected by a Certified Access Specialist \(CASp\)\.)", quote)
        if not m:
            raise RuntimeError("the Civil Code 1938 status quote in the rulebook changed; update casp_lease_disclosure")

        def status_text(done: bool) -> str:
            return f"{m.group(1)}{lessor}{m.group(2)}{'has been' if done else 'has not been'}{m.group(3)}"
        if inspected is None:
            clauses.append(_clause(acc, "CASp inspection status (Civil Code 1938)", (
                f"Use ONE of: (a) \"{status_text(False)}\" or (b) \"{status_text(True)}\""), [LEASE_STATUS],
                "Find out whether the premises were ever CASp-inspected before signing; the lease must say which."))
        else:
            clauses.append(_clause(acc, "CASp inspection status (Civil Code 1938)", status_text(inspected),
                                   [LEASE_STATUS]))
        conflict = P(LEASE_STMT, "wording_conflict")
        clauses.append(_clause(acc, "CASp statement (Civil Code 1938(e))",
                               P(LEASE_STMT, "statement_text_as_cited_to_1938e"), [LEASE_STMT], (
            f"Two City of San Diego documents print this statement with different wording ({conflict}); the statute "
            "itself was not read. Have your attorney check the exact statutory text before using it in a lease form.")))
        if inspected:
            acc.notes.append("Civil Code 1938 adds duties when a CASp report exists (copies to the tenant, timing, "
                             "rescission rights, repair responsibility); those terms were not in the verified "
                             "rulebook. Have your attorney add them.")
        rights.append(acc.req("Tenant may get a CASp inspection", (
            "The landlord may not prohibit the tenant from obtaining a CASp inspection for its occupancy if the "
            f"tenant asks. The parties agree on the {_join(P(LEASE_STMT, 'parties_must_agree_on'))}."), LEASE_STMT))
        rights.append(acc.rule(CASP, "What a CASp does"))
        if ti:
            acc.use(F_TENANT)
            rights.append(acc.req("Who pays for path-of-travel upgrades on tenant improvements", (
                "Federally, a tenant's alterations in areas only the tenant occupies do not put a path-of-travel duty "
                "on the landlord for areas it controls, unless those areas are otherwise altered (28 CFR 36.403(d)). "
                "California plan check still applies CBC 11B-202.4 to the TI permit, which often reaches the "
                "building entrance and restrooms: say in the lease who pays for those upgrades. Run "
                "path_of_travel_obligation for the amount."), F_TENANT))
    else:
        acc.use(FHA_MODS)
        may = P(FHA_MODS, "landlord_may_require")
        may_not = P(FHA_MODS, "landlord_may_not")
        rights.append(acc.req("Reasonable modifications (24 CFR 100.203)", (
            f"A landlord may not {may_not[0]} a tenant with a disability asks to make at the tenant's "
            f"expense ({P(FHA_MODS, 'paid_by')} pays). The landlord may require: {_join(may)}. The landlord may not "
            f"{may_not[1]}."), FHA_MODS))
        acc.notes.append("Civil Code 1938 CASp statements are for commercial leases; this residential lease needs "
                         "none.")
    # business license fee (SB 1186 / AB 1379 / AB 2164) and AB 3002 notice
    fee = float(P(FEE4, "fee_usd"))
    hist = P(FEE4, "history_usd")
    rev = ""
    for k, v in core.rule(FEE4).parameters.items():         # e.g. reversion_to_1_usd_on_2024_01_01: 'repealed ...'
        m = re.fullmatch(r"reversion_to_(\d+(?:_\d+)?)_usd_on_(\d{4})_(\d{2})_(\d{2})", k)
        if m:
            rev = (f"; the scheduled drop to {_usd_s(float(m.group(1).replace('_', '.')))} on "
                   f"{m.group(2)}-{m.group(3)}-{m.group(4)} was {v}")
    lic = [acc.req("State disability-access fee on business licenses", (
        f"{_usd_s(fee)} added to every {_join(P(FEE4, 'charged_on'), 'and every')} "
        f"(history: " + ", ".join(f"{_usd_s(v)} from {k}" for k, v in sorted(hist.items())) + rev + ")."), FEE4),
           acc.rule(AB3002)]
    sd = None
    juris, juris_label, county_note = _jurisdiction(business_license_city)
    if county_note:
        acc.notes.append(f"{juris_label}: the San Diego business tax example applies only to the City of San Diego.")
    if juris == "san_diego":
        ex = P(FEE4, "example_san_diego_mail_application_usd")
        mwe = ex["plus_per_employee_minimum_wage_enforcement_fee_usd"]
        rates = sorted(float(v) for k, v in mwe.items() if k.endswith("_page"))
        small = re.search(r"(\d+) employees or fewer", ex["business_tax_applies_to"])
        est = None
        if staff is not None and small and staff <= int(small.group(1)):
            est = [_money(ex["total"] + r * staff) for r in (rates[0], rates[-1])]
            acc.math.append(f"San Diego mail application: {_usd_s(ex['business_tax'])} business tax + "
                            f"{_usd_s(ex['sb_1186_fee'])} state fee = {_usd_s(ex['total'])}, + {staff} employees x "
                            f"{_usd_s(rates[0])} to {_usd_s(rates[-1])} wage-enforcement fee = {_usd_s(est[0])} to "
                            f"{_usd_s(est[1])}")
        elif staff is not None:
            acc.notes.append(f"With {staff} employees the San Diego business tax is not the "
                             f"{_usd_s(ex['business_tax'])} small-business rate ({ex['business_tax_applies_to']}); "
                             "confirm the amount with the City Treasurer.")
        sd = SanDiegoLicenseExample(business_tax_usd=ex["business_tax"],
                                    business_tax_applies_to=ex["business_tax_applies_to"],
                                    state_disability_access_fee_usd=ex["sb_1186_fee"],
                                    total_before_wage_fee_usd=ex["total"],
                                    minimum_wage_enforcement_fee_per_employee_usd=rates,
                                    minimum_wage_fee_from=mwe["from"],
                                    employees=staff, estimated_total_usd=est,
                                    note=f"{_cap(mwe['note'])}. Confirm with the City Treasurer.")
    if who == "landlord" and ptype == "commercial":
        summary = ("Put both Civil Code 1938 CASp statements in the lease: whether the premises were CASp-inspected, "
                   "and the standard CASp statement.")
        nxt = ("Check the property records for any CASp report, fill in clause 1 accordingly, paste clause 2, and have "
               "your attorney confirm the statutory wording before the lease is signed.")
    elif ptype == "commercial":
        summary = ("The lease must say whether the premises were CASp-inspected and include the standard CASp "
                   "statement; you may ask for a CASp inspection and the lease should say who pays for it and for "
                   "repairs.")
        nxt = ("Before signing, ask the landlord for the CASp status and any report, decide whether to order your own "
               "CASp inspection, and negotiate who pays for access repairs" + (" and path-of-travel upgrades on your "
                                                                               "TI" if ti else "") + ".")
    else:
        summary = "Residential rental: Fair Housing Act reasonable-modification rules apply; no CASp lease statements."
        nxt = ("Before modifying a rental for a tenant with a disability, get the landlord's written OK with a "
               "description of the work, pull any required permits, and agree on restoration at move-out if the "
               "landlord asks.")
    res = LeaseResult(role=who, property_type=ptype, premises_inspected_by_casp=inspected, lease_clauses=clauses,
                      rights_and_duties=rights, business_license_fee_usd=fee, business_license=lic,
                      san_diego_example=sd, summary=summary, next_step=nxt)
    return acc.finish(res)


# ================================================================ MCP tools
def _tool_docs() -> dict[str, str]:
    """Tool descriptions for AI agents, with every legal number read from the rulebook so they never go stale."""
    thr = _thresholds()
    year = max(thr)
    cap = float(P(CAP20, "cap_fraction_of_adjusted_construction_cost"))
    stall = lambda k: _n(P(PK_STALL, k))                                     # noqa: E731
    rows = P(S_PK_TABLE, "table")
    t208 = (f"{rows[0]['from']}-{rows[0]['to']} spaces: {rows[0]['required']} ... over {rows[-1]['from'] - 1:,}: "
            f"{rows[-1]['required']}")
    return {
        "triggers": (
            "Which accessibility rules apply to a California project: CBC Chapter 11B (public accommodations, "
            "commercial and public buildings, public housing, publicly funded multifamily), CBC Chapter 11A (covered "
            "multifamily), ADA Title III and Title II, the Fair Housing Act design rules (buildings of "
            f"{P(FHA_COVERED, 'min_units_per_building')}+ units first occupied after "
            f"{P(FHA_DATE, 'first_occupancy_after')}: all units with an elevator, ground-floor units without) and CBC "
            f"11B-233 public housing ({_pct(P(PH_UNITS, 'mobility_unit_fraction'))} mobility / "
            f"{_pct(P(PH_UNITS, 'communication_unit_fraction'))} communication units). Returns each regime with "
            "applies (true/false/null = confirm), why and key requirements, unit counts, the federal elevator "
            f"exemption (under {P(F_ELEV, 'stories_less_than')} stories or {_n(P(F_ELEV, 'sq_ft_per_story_less_than'))}"
            " sq ft per story, not for shopping centers, health care offices, transit or airports), the "
            f"{_pct(P(S_ENTRANCES, 'public_entrances_accessible_min_fraction'))} public-entrance count, penalties "
            f"(ADA Title III up to {_usd_s(P(F_PENALTY, 'first_violation_max_usd'))}), the tools to run next, math "
            "and citations. Accepts plain words ('TI', 'apartments', 'retail')."),
        "pot": (
            "California path-of-travel upgrade obligation for an alteration, addition or structural repair (CBC "
            "11B-202.4 Exception 8) plus the federal ADA 20% disproportionality rule (28 CFR 36.403 / 35.151). Give "
            "the construction cost and permit application date; add prior alterations from the last "
            f"{P(LOOKBACK, 'lookback_years')} years on the same path of travel, path-of-travel costs already in the "
            "price, estimated element costs and the city. Returns adjusted construction cost (this job - "
            "path-of-travel work + prior work), the test against the DSA valuation threshold "
            f"({_usd_s(thr[year])} for {year}; other years are reported as unknown, never guessed): at or below, "
            f"path-of-travel work need not exceed {_pct(cap)}; above, full compliance. Also the federal limit, the "
            "combined budget, a spending plan in the legal priority order (entrance, route, restroom, telephones, "
            "drinking fountains, other), the LADBS Summary of Accessibility Upgrades or San Diego DS-2123 step, the "
            "DS-2123 compliance %, math, next step and citations. hardship_requested adds the unverified hardship "
            "minimum (verified=false)."),
        "parking": (
            "Required accessible and van-accessible parking for each lot or garage (counted separately). CBC 11B / ADA "
            f"Table 208.2 ({t208}), hospital outpatient "
            f"{_pct(P(PK_FACILITY, 'hospital_outpatient_fraction'))} and rehab/PT "
            f"{_pct(P(PK_FACILITY, 'rehab_and_outpatient_pt_fraction'))}, van spaces 1 in "
            f"{_divisor(P(PK_VAN6, 'formula'))} (min {P(PK_VAN6, 'min_van_spaces')}); EV charging spaces excluded. "
            "use covered_multifamily applies "
            f"CBC 11A ({_pct(P(A_PK, 'per_covered_units_fraction'))} of covered units, "
            f"{_pct(P(A_PK, 'assigned_spaces_fraction'))} of assigned and {_pct(P(A_PK, 'unassigned_spaces_fraction'))}"
            f" of unassigned spaces, at least {P(A_PK, 'min_per_parking_facility_type')} per facility type, van 1 in "
            f"{_divisor(P(A_VAN8, 'formula_conservative'))}"
            f"). Also stall sizes (car {stall('car_width_min_ft')} ft, van {stall('van_width_min_ft')} ft + "
            f"{stall('van_aisle_min_ft_with_12ft_stall')} ft aisle or {stall('van_width_alt_min_ft')} + "
            f"{stall('van_aisle_min_ft_with_9ft_stall')} ft, {stall('stall_length_min_ft')} ft long), "
            f"{P(PK_VERT, 'vertical_clearance_text')} clearance, signs ('{P(PK_SIGN, 'required_text')}', tow-away) and "
            "pavement markings, with math and citations."),
        "field": (
            "Check field measurements against CBC 11B (LADBS 2026 restatement): door clear width "
            f"({_n(P(DOOR, 'clear_width_min_in'))} in, {_n(P(DOOR, 'deep_opening_clear_width_min_in'))} in if over "
            f"{_n(P(DOOR, 'deep_opening_threshold_in'))} in deep), changes in level ({_n(P(LEVEL, 'vertical_max_in'))} "
            f"in vertical, to {_n(P(LEVEL, 'over_in_requires_ramp'))} in beveled, more ramped), ramps "
            f"({P(RAMP, 'running_slope_max')}, cross {P(RAMP, 'cross_slope_max')}, {_n(P(RAMP, 'clear_width_min_in'))} "
            f"in wide, {_n(P(RAMP, 'rise_per_run_max_in'))} in rise per run), curb ramps, site walkways "
            f"({_n(P(WALK, 'walkway_width_min_in'))} in, {_n(P(WALK, 'running_slope_max_pct'))}% running, "
            f"{_n(P(WALK, 'cross_slope_max_pct'))}% cross), counters ({_n(P(COUNTER, 'accessible_height_max_in'))} in "
            f"high, {_n(P(COUNTER, 'accessible_length_min_in'))} in long), turning space, parking stalls, "
            "vertical clearance and parking sign height. Lengths accept 31.5, '31 1/2 in', '2 ft 8 in' or '9 ft' "
            "(bare numbers are inches, feet for parking stalls); slopes accept 8.33, '8.33%' or '1:12'. Returns "
            "pass/fail/needs_info per item with the fix, a next step and citations."),
        "lease": (
            "Commercial lease accessibility clauses for a landlord or tenant: the Civil Code 1938 statement of whether "
            "the premises were inspected by a Certified Access Specialist (CASp) and the standard 1938(e) CASp "
            "statement (text, with the wording caution), the tenant's right to a CASp inspection, what a CASp does, "
            "who pays for path-of-travel upgrades on tenant improvements, and the "
            f"{_usd_s(P(FEE4, 'fee_usd'))} state disability-access fee on every business license application or "
            "renewal (with the City of San Diego example). Residential rentals get the Fair Housing Act "
            "reasonable-modification rules instead."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    Num = Optional[Union[float, str]]
    years = P(LOOKBACK, "lookback_years")

    @mcp.tool(name="accessibility_triggers", annotations=ro, description=docs["triggers"])
    def _accessibility_triggers(
        project_type: Annotated[str, Field(min_length=1, description=(
            "new_construction, alteration (remodel, TI, renovation, structural repair), addition, barrier_removal or "
            "maintenance_only"))],
        building_use: Annotated[str, Field(min_length=1, description=(
            "public_accommodation (store, restaurant, bank, hotel, medical office), commercial_facility (office, "
            "warehouse, factory), public_building (government), multifamily (apartments, condos), public_housing or "
            "mixed_use (homes over retail)"))],
        dwelling_units: Annotated[Union[int, str], Field(description=(
            "Dwelling units in the building (count each building separately)"))] = 0,
        has_elevator: Annotated[Optional[bool], Field(description="Does the residential building have an elevator?")]
        = None,
        ground_floor_units: Annotated[Optional[Union[int, str]], Field(description=(
            "Units on a floor with a building entrance on an accessible route (no-elevator buildings)"))] = None,
        multistory_units: Annotated[Optional[Union[int, str]], Field(description=(
            f"Townhouse-style multistory units (CBC 11A {_pct(P(A_MULTI10, 'fraction'))} rule in no-elevator "
            "buildings)"))] = None,
        publicly_funded: Annotated[bool, Field(description="Multifamily built with public money")] = False,
        public_entity: Annotated[bool, Field(description=(
            "Built or altered by, for or on behalf of a state or local government (ADA Title II)"))] = False,
        stories: Annotated[Optional[Union[int, str]], Field(description="Number of stories")] = None,
        sq_ft_per_story: Annotated[Num, Field(description="Floor area per story, sq ft")] = None,
        special_facility: Annotated[Optional[str], Field(description=(
            "shopping_center, health_care_office, transit_station or airport_terminal (no federal elevator "
            "exemption)"))] = None,
        sales_or_rental_establishments: Annotated[Optional[Union[int, str]], Field(description=(
            f"Number of sales or rental establishments ({P(F_ELEV, 'shopping_center_min_establishments')}+ is a "
            "shopping center)"))] = None,
        public_entrances: Annotated[Optional[Union[int, str]], Field(description=(
            "Number of public entrances (new construction)"))] = None,
        first_occupied_date: Annotated[Optional[str], Field(description=(
            "Existing residential building: date first occupied (FHA 1991 cutoff)"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> TriggersResult:
        return accessibility_triggers(project_type, building_use, dwelling_units, has_elevator, ground_floor_units,
                                      multistory_units, publicly_funded, public_entity, stories, sq_ft_per_story,
                                      special_facility, sales_or_rental_establishments, public_entrances,
                                      first_occupied_date, as_of)

    @mcp.tool(name="path_of_travel_obligation", annotations=ro, description=docs["pot"])
    def _path_of_travel_obligation(
        construction_cost_usd: Annotated[Union[float, str], Field(description=(
            "Construction cost (permit valuation) of this alteration, e.g. 150000 or '$150,000'"))],
        permit_application_date: Annotated[Optional[str], Field(description=(
            "Date the permit application is submitted (picks the valuation threshold year); default today"))] = None,
        prior_alterations_cost_usd: Annotated[Num, Field(description=(
            f"Total cost of alterations in the preceding {years} years to this area or the same path of travel that "
            "did NOT provide an accessible path of travel"))] = None,
        prior_alterations: Annotated[Optional[list[PriorAlteration]], Field(description=(
            "Instead of the total: each earlier job [{date, cost_usd, provided_accessible_path_of_travel, "
            "same_path_of_travel}]; the tool applies the look-back window"))] = None,
        path_of_travel_costs_included_usd: Annotated[Num, Field(description=(
            "Path-of-travel upgrade costs already inside construction_cost_usd (they are excluded from the adjusted "
            "cost; access work in the altered area itself is not)"))] = None,
        full_path_of_travel_cost_usd: Annotated[Num, Field(description=(
            "Estimate to bring every path-of-travel element fully into compliance"))] = None,
        element_costs_usd: Annotated[Optional[dict[str, Union[float, str]]], Field(description=(
            "Estimates per element: entrance, route, restroom, telephones, drinking_fountains, other (parking, "
            "storage, alarms, signs); 0 = already complies"))] = None,
        planned_accessibility_spend_usd: Annotated[Num, Field(description=(
            "What you plan to spend on path-of-travel items (DS-2123 line A)"))] = None,
        work_type: Annotated[str, Field(description=(
            "alteration (remodel, TI), structural_repair, addition, barrier_removal, program_access (public entity), "
            "parking_resurfacing_restriping, minor_work_only (windows, hardware, controls, outlets, signage) or "
            "maintenance_only (reroofing, painting, asbestos removal)"))] = "alteration",
        facility: Annotated[str, Field(description=(
            "public_accommodation, commercial_facility, public_entity (government) or publicly_funded_multifamily"))]
        = "public_accommodation",
        affects_primary_function_area: Annotated[Optional[bool], Field(description=(
            "Does the work alter an area with a primary function (sales floor, offices, dining, work areas)? Not: "
            "restrooms, corridors, storage, mechanical rooms"))] = True,
        primary_function_alteration_cost_usd: Annotated[Num, Field(description=(
            "Federal base: cost of the alteration to the primary function area, if less than the construction "
            "cost"))] = None,
        jurisdiction: Annotated[Optional[str], Field(description=(
            "Building department: 'Los Angeles' / 'LADBS' / 'LA', 'San Diego' / 'SD', or any other city"))] = None,
        hardship_requested: Annotated[bool, Field(description=(
            "Above the threshold: show what an unreasonable-hardship finding involves (uses an unverified rule)"))]
        = False,
        tenant_only_alteration: Annotated[bool, Field(description=(
            "Tenant work only in areas the tenant alone occupies (28 CFR 36.403(d) landlord note)"))] = False,
        path_elements_built_to_1991_standards: Annotated[bool, Field(description=(
            "The path-of-travel elements were built or altered to the 1991 ADA Standards (federal safe harbor "
            "note)"))] = False,
    ) -> PathOfTravelResult:
        return path_of_travel_obligation(construction_cost_usd, permit_application_date, prior_alterations_cost_usd,
                                         prior_alterations, path_of_travel_costs_included_usd,
                                         full_path_of_travel_cost_usd, element_costs_usd,
                                         planned_accessibility_spend_usd, work_type, facility,
                                         affects_primary_function_area, primary_function_alteration_cost_usd,
                                         jurisdiction, hardship_requested, tenant_only_alteration,
                                         path_elements_built_to_1991_standards)

    @mcp.tool(name="accessible_parking_requirements", annotations=ro, description=docs["parking"])
    def _accessible_parking_requirements(
        total_spaces: Annotated[Optional[Union[int, str]], Field(description="Spaces in a single lot or garage")]
        = None,
        facilities: Annotated[Optional[list[ParkingFacilityIn]], Field(description=(
            "Several lots or garages: [{name, total_spaces, ev_charging_spaces, kind, assigned, use}]"))] = None,
        use: Annotated[str, Field(description=(
            "general (stores, offices, restaurants, public buildings), hospital_outpatient, rehab_outpatient_pt or "
            "covered_multifamily (CBC 11A)"))] = "general",
        covered_dwelling_units: Annotated[Optional[Union[int, str]], Field(description=(
            "covered_multifamily only: number of covered dwelling units"))] = None,
        work_type: Annotated[str, Field(description="new_or_altered or resurfacing_restriping")] = "new_or_altered",
    ) -> ParkingResult:
        return accessible_parking_requirements(facilities, total_spaces, use, covered_dwelling_units, work_type)

    L = Annotated[Num, Field(description="inches: 31.5, '31 1/2 in' or '2 ft 8 in'")]
    S = Annotated[Num, Field(description="percent (8.33 or '8.33%') or ratio ('1:12')")]
    LF = Annotated[Num, Field(description="feet: 9, '9 ft' or '108 in'")]

    @mcp.tool(name="accessibility_field_check", annotations=ro, description=docs["field"])
    def _accessibility_field_check(
        door_clear_width: L = None, door_opening_depth: L = None,
        level_change_height: L = None,
        level_change_beveled: Annotated[Optional[bool], Field(description="Is the level change beveled?")] = None,
        ramp_running_slope: S = None,
        ramp_rise: L = None,
        ramp_run: Annotated[Num, Field(description="Horizontal length of the ramp run, inches or '22 ft'")] = None,
        ramp_cross_slope: S = None, ramp_clear_width: L = None,
        curb_ramp_running_slope: S = None, curb_ramp_flare_slope: S = None,
        walkway_clear_width: L = None, walkway_running_slope: S = None, walkway_cross_slope: S = None,
        counter_height: L = None, counter_length: L = None,
        checkout_counter: Annotated[bool, Field(description="The counter is a check-out aisle counter")] = False,
        turning_space_diameter: L = None,
        parking_stall_width: LF = None, parking_aisle_width: LF = None, parking_stall_length: LF = None,
        van_space: Annotated[bool, Field(description="The stall is a van-accessible space")] = False,
        parking_vertical_clearance: L = None,
        parking_sign_bottom_height: L = None,
        existing_building: Annotated[bool, Field(description=(
            "Existing building or site (adds the federal steeper-ramp note when a ramp fails)"))] = False,
    ) -> FieldCheckResult:
        return accessibility_field_check(door_clear_width, door_opening_depth, level_change_height,
                                         level_change_beveled, ramp_running_slope, ramp_rise, ramp_run,
                                         ramp_cross_slope, ramp_clear_width, curb_ramp_running_slope,
                                         curb_ramp_flare_slope, walkway_clear_width, walkway_running_slope,
                                         walkway_cross_slope, counter_height, counter_length, checkout_counter,
                                         turning_space_diameter, parking_stall_width, parking_aisle_width,
                                         parking_stall_length, van_space, parking_vertical_clearance,
                                         parking_sign_bottom_height, existing_building)

    @mcp.tool(name="casp_lease_disclosure", annotations=ro, description=docs["lease"])
    def _casp_lease_disclosure(
        role: Annotated[str, Field(description="landlord (lessor, owner) or tenant (lessee)")] = "landlord",
        premises_inspected_by_casp: Annotated[Optional[bool], Field(description=(
            "Have the premises been inspected by a CASp? null = unknown"))] = None,
        lessor_name: Annotated[Optional[str], Field(description="Landlord's name for the clause text")] = None,
        property_type: Annotated[str, Field(description="commercial or residential")] = "commercial",
        business_license_city: Annotated[Optional[str], Field(description=(
            "City issuing your business license (San Diego adds a worked fee example)"))] = None,
        employees: Annotated[Optional[Union[int, str]], Field(description="Employees (San Diego fee example)")] = None,
        tenant_improvements_planned: Annotated[bool, Field(description=(
            "Tenant improvements are planned (adds who-pays-for-path-of-travel guidance)"))] = False,
    ) -> LeaseResult:
        return casp_lease_disclosure(role, premises_inspected_by_casp, lessor_name, property_type,
                                     business_license_city, employees, tenant_improvements_planned)
