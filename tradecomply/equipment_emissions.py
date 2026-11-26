"""California air rules for construction equipment, trucks and jobsite dust (CARB and the air districts).

  offroad_fleet_obligations         fleet horsepower + owner type (+ the machines) -> CARB off-road fleet size
                                    (13 CCR 2449), what you may add now, Tier phase-out date for each machine, DOORS
                                    reporting and the ROAR, EIN labels, idling, R99/R100 renewable diesel, records and
                                    the next step
  offroad_contracting_requirements  your role on a project (prime contractor, public works awarding body or sub) ->
                                    Certificates of Reported Compliance before contracting and every March 1-June 1,
                                    the CARB jobsite sign (projects of 8+ days), the 5-business-day report, record
                                    keeping and the haul-truck hiring checks (Truck and Bus, Clean Truck Check)
  truck_compliance                  one heavy truck (GVWR, fuel, engine model year, registration) -> Truck and Bus
                                    (2010 engine), the low-use and NOx-exempt-area options, the DMV registration check,
                                    Clean Truck Check tests per year and next deadlines, idling and Advanced Clean
                                    Fleets status
  dust_control_requirements         air district (or county) + disturbed acres + cubic yards a day -> South Coast
                                    Rules 403/403.1, San Joaquin Valley Rules 8011/8021/8041/3135, Bay Area Rule 6-6 and
                                    the CEQA dust measures: plans and notices with dates, track-out controls,
                                    large-operation duties, fees and records
  portable_engine_requirements      one portable engine (bhp, tier, months at one location) -> Portable Engine ATCM
                                    and PERP: portable or not, registration eligibility, fees, renewal dates, low-use
                                    reporting and the 2,500 bhp project notice

Every legal number, threshold and date comes from core.rule(id).param(key) at call time, and every rule a result
relies on is cited through core.Result.cite(), so `verified` is True only when all of them were verified. The pack is
research/web_air-equipment.verified.json (rule ids "air-...").

Not legal values (so not in the rulebook): the district, county, owner, role, fuel and engine-tier spellings below,
which only map a user's words onto the rules, and the unit conversion 1 acre = 43,560 sq ft. County names are checked
against the county lists of rule usa-regional-centers-by-county, used for spelling only and not cited. The counties
each air district covers are the scopes the dust rules state themselves (their applies_to text, or for the San Joaquin
Valley the counties parameter); a test checks every name below against that text.

Not computed, because the rulebook does not have them: the off-road fleet average / BACT turnover targets (13 CCR
2449.1(a)-(b)), Health and Safety Code penalty amounts, the current Clean Truck Check fee, the public agency and utility
fleet rule (13 CCR 2022-2022.1) that replaces Truck and Bus for municipal vehicles meeting it, and the dust rules of air
districts other than South Coast, San Joaquin Valley and the Bay Area.

Readings of the rule text a reviewer should know (adversarial review 2026-11-25): the jobsite sign day count takes the
first operating day as day 1; business days skip weekends but not holidays (the date can only be early); on the
optional 500 hp schedule an on-road engine counts as Tier 2 or better when newer than the on-road model year that
2449.1(c) pairs with Tier 1; Clean Truck Check quarterly testing is shown from CARB's fact-sheet year, with a caveat
from the regulation's own three-year mark; the SJV 48-hour notice and the rural 50-ft track-out test use the site's
area (site_acres) when given, else the disturbed area with a note.
"""
import calendar
import math
import re
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
# CARB In-Use Off-Road Diesel-Fueled Fleets (13 CCR 2449-2449.2, 2022 amendments)
ORD_APPLIES, ORD_SIZE, ORD_HP_EXCL, ORD_COMMON, ORD_RENTAL_FLEET, ORD_CAPTIVE = (
    "air-ord-applicability-25hp", "air-ord-fleet-size-classes", "air-ord-total-hp-exclusions",
    "air-ord-common-ownership", "air-ord-rental-lease-fleet", "air-ord-captive-attainment-counties")
ORD_IDLE, ORD_QUEUE, ORD_IDLE_POLICY, ORD_RENTAL_IDLE = (
    "air-ord-idling-5-minutes", "air-ord-queuing-definition", "air-ord-written-idling-policy",
    "air-ord-rental-idling-clause")
ORD_GROW, ORD_NEW_FLEET = "air-ord-changing-fleet-size", "air-ord-new-fleet-report-30-days"
ORD_ADD_T0, ORD_ADD_T3, ORD_ADD_T4I, ORD_ADD_ONROAD = (
    "air-ord-add-tier0-ban", "air-ord-add-tier3-ban-2024", "air-ord-add-tier4i-ban", "air-ord-add-onroad-2006-ban")
ORD_PH = {"large": "air-ord-tier-phaseout-large", "medium": "air-ord-tier-phaseout-medium",
          "small": "air-ord-tier-phaseout-small"}
ORD_PH_LOW_USE, ORD_PH_500, ORD_OPT_500 = ("air-ord-tier-phaseout-low-use-2036", "air-ord-tier2-phaseout-500hp-2036",
                                           "air-ord-optional-schedule-500hp")
ORD_SPECIAL, ORD_ZEV_DELAY = "air-ord-special-provision-designation", "air-ord-zev-delay-tier-phaseout"
ORD_ANNUAL, ORD_ROAR, ORD_RD_AFF = ("air-ord-annual-reporting-march-1", "air-ord-roar-affirmation",
                                    "air-ord-rd-affirmation")
ORD_SOLD, ORD_SALE_DISC = "air-ord-sold-vehicle-report-30-days", "air-ord-sales-disclosure"
ORD_EIN_APPLY, ORD_EIN_LABEL, ORD_EIN_SPEC, ORD_EMIS_LABEL = (
    "air-ord-ein-apply-30-days", "air-ord-ein-label-30-days", "air-ord-ein-label-spec",
    "air-ord-emission-label-replacement")
ORD_RECORDS, ORD_RETAIN = "air-ord-records-5-business-days", "air-ord-record-retention-2037"
ORD_R99, ORD_RD_EXEMPT, ORD_RD_COLD, ORD_RD_UNAVAIL, ORD_RD_RECORDS, ORD_RENTAL_RD = (
    "air-ord-renewable-diesel-r99", "air-ord-rd-exemptions", "air-ord-rd-cold-weather-reports",
    "air-ord-rd-unavailable-documentation", "air-ord-rd-purchase-records-3-years",
    "air-ord-rental-rd-contract-clause")
ORD_LOW_USE, ORD_LOW_USE_METER = "air-ord-permanent-low-use", "air-ord-low-use-hour-meter-reporting"
ORD_CRC_BEFORE, ORD_CRC_RETAIN, ORD_CRC_EMERGENCY, ORD_CRC_ISSUE = (
    "air-ord-crc-before-contract", "air-ord-crc-retain-3-years", "air-ord-crc-emergency-exemption",
    "air-ord-crc-issuance")
ORD_PRIME_ANNUAL, ORD_PRIME_ONLY, ORD_PRIME_REPORT, ORD_PRIME_DISCLOSE, ORD_PRIME_SIGN = (
    "air-ord-prime-annual-crc-march-june", "air-ord-prime-only-certified-fleets",
    "air-ord-prime-report-noncompliance-5-days", "air-ord-prime-disclose-responsible-parties",
    "air-ord-prime-jobsite-sign")
ORD_PRIME_DEF, ORD_AWARD_DEF = "air-ord-prime-contractor-definition", "air-ord-public-works-awarding-body"
ORD_ENFORCE, ORD_ZETA, ORD_PENALTIES = ("air-ord-enforcement-status-2025", "air-ord-zeta-alternate-pathway",
                                        "air-ord-penalties")
# Truck and Bus (13 CCR 2025)
TB_APPLIES, TB_2010, TB_HIRE, TB_LEASE, TB_SALE = (
    "air-tb-applicability-14000", "air-tb-2010-engine-required", "air-tb-hired-vehicle-verification",
    "air-tb-lease-clause", "air-tb-sales-disclosure")
TB_LOW_USE, TB_NOX, TB_RECORDS, TB_PENALTIES, TB_DMV = (
    "air-tb-low-use-1000-miles", "air-tb-nox-exempt-areas", "air-tb-records-5-business-days", "air-tb-penalties",
    "air-tb-dmv-registration-block")
# Clean Truck Check / HD I/M (13 CCR 2195-2199)
CTC_APPLIES, CTC_CONDITIONS, CTC_FREQ, CTC_START, CTC_ALIGN = (
    "air-ctc-applicability", "air-ctc-compliance-conditions", "air-ctc-testing-frequency",
    "air-ctc-periodic-testing-start", "air-ctc-deadline-alignment")
CTC_WINDOW, CTC_FEE, CTC_DMV, CTC_TRANSFER, CTC_HIRE = (
    "air-ctc-90-day-test-window", "air-ctc-compliance-fee", "air-ctc-dmv-registration", "air-ctc-transfer-of-ownership",
    "air-ctc-freight-contractor-hiring")
CTC_RECORDS, CTC_PASS, CTC_TAMPER, CTC_PENALTIES = ("air-ctc-records-5-years", "air-ctc-five-day-pass",
                                                    "air-ctc-no-tampered-emissions", "air-ctc-penalties")
ACF = "air-acf-government-fleets-only-2026"
# commercial vehicle idling (13 CCR 2485)
IDLE_TRUCK, IDLE_PTO, IDLE_RESTRICTED, IDLE_FINE, IDLE_CARRIER = (
    "air-truck-idling-5-minutes", "air-truck-idling-pto-exception", "air-truck-idling-restricted-area",
    "air-truck-idling-fine-1000-day", "air-truck-idling-carrier-info")
# PERP (13 CCR 2450 et seq.) and the Portable Engine ATCM (17 CCR 93116 et seq.)
PERP_VOLUNTARY, PERP_PORTABLE, PERP_TERM, PERP_FEES, PERP_PROJECT = (
    "air-perp-voluntary-district-permits", "air-perp-portable-12-months", "air-perp-registration-3-years",
    "air-perp-fees", "air-perp-large-project-notice")
PERP_ELIGIBLE, PERP_ATCM, PERP_FLEET, PERP_LOW_USE, PERP_METER = (
    "air-perp-engine-eligibility", "air-perp-portable-atcm-50bhp", "air-perp-fleet-size-750",
    "air-perp-low-use-200-hours", "air-perp-hour-meter-renewal")
# South Coast AQMD Rules 403 and 403.1
SC_VISIBLE, SC_BACM, SC_PM10, SC_TRACKOUT, SC_EXIT, SC_LARGE = (
    "air-scaqmd403-no-visible-dust-offsite", "air-scaqmd403-bacm-table1", "air-scaqmd403-pm10-50ug",
    "air-scaqmd403-trackout-25ft", "air-scaqmd403-exit-controls-5ac-100cy", "air-scaqmd403-large-operation")
SC_403N, SC_LARGE_RECORDS, SC_SIGNAGE, SC_SUPERVISOR, SC_LARGE_ENDS, SC_RENEW = (
    "air-scaqmd403-form-403n-7-days", "air-scaqmd403-large-op-daily-records", "air-scaqmd403-large-op-signage",
    "air-scaqmd403-dust-control-supervisor", "air-scaqmd403-large-op-ends-30-days",
    "air-scaqmd403-403n-annual-renewal")
SC_WIND, SC_TABLE2, SC_GRADING, SC_STOCKPILE, SC_CONTRACT_END, CV_PLAN = (
    "air-scaqmd403-high-wind-25mph", "air-scaqmd403-table2-large-op-measures", "air-scaqmd403-completed-grading-5-days",
    "air-scaqmd403-stockpiles-haul-freeboard", "air-scaqmd403-contractor-end-of-contract",
    "air-scaqmd4031-coachella-dust-plan")
# San Joaquin Valley APCD Regulation VIII and Rule 3135
SJV_DCP, SJV_REVIEW, SJV_48H, SJV_ONSITE, SJV_VDE, SJV_INACTIVE = (
    "air-sjv8021-dust-control-plan-thresholds", "air-sjv8021-plan-review-30-days",
    "air-sjv8021-small-site-48-hour-notice", "air-sjv8021-plan-onsite-training", "air-sjv8021-vde-20-percent",
    "air-sjv8021-inactive-half-acre-7-days")
SJV_SPEED, SJV_WIND, SJV_DEMO, SJV_EXEMPT, SJV_FEE = (
    "air-sjv8021-unpaved-15mph-signs", "air-sjv8021-wind-cease-work", "air-sjv8021-demolition-water",
    "air-sjv8021-exemptions", "air-sjv3135-dust-plan-fee-2026")
SJV_EOD, SJV_50FT, SJV_DEVICE, SJV_SPECS, SJV_RECORDS, SJV_COUNTIES = (
    "air-sjv8041-trackout-end-of-day", "air-sjv8041-trackout-50ft-immediate", "air-sjv8041-trackout-device-triggers",
    "air-sjv8041-trackout-device-specs", "air-sjv8011-records-1-year", "air-sjv-district-counties")
# Bay Area AQMD Rule 6-6 and 2022 CEQA Guidelines
BA_BASIC, BA_ENHANCED, BA_LARGE, BA_TRACKOUT, BA_OPACITY, BA_MONITOR = (
    "air-baaqmd-ceqa-basic-dust-bmps", "air-baaqmd-ceqa-enhanced-dust-bmps", "air-baaqmd66-large-site-1-acre",
    "air-baaqmd66-trackout-25ft-4-hours", "air-baaqmd66-cleanup-opacity", "air-baaqmd66-monitoring-records")

RULE_IDS = (
    ORD_APPLIES, ORD_SIZE, ORD_HP_EXCL, ORD_COMMON, ORD_RENTAL_FLEET, ORD_CAPTIVE, ORD_IDLE, ORD_QUEUE,
    ORD_IDLE_POLICY, ORD_RENTAL_IDLE, ORD_GROW, ORD_NEW_FLEET, ORD_ADD_T0, ORD_ADD_T3, ORD_ADD_T4I, ORD_ADD_ONROAD,
    *ORD_PH.values(), ORD_PH_LOW_USE, ORD_PH_500, ORD_OPT_500, ORD_SPECIAL, ORD_ZEV_DELAY, ORD_ANNUAL, ORD_ROAR,
    ORD_RD_AFF, ORD_SOLD, ORD_SALE_DISC, ORD_EIN_APPLY, ORD_EIN_LABEL, ORD_EIN_SPEC, ORD_EMIS_LABEL, ORD_RECORDS,
    ORD_RETAIN, ORD_R99, ORD_RD_EXEMPT, ORD_RD_COLD, ORD_RD_UNAVAIL, ORD_RD_RECORDS, ORD_RENTAL_RD, ORD_LOW_USE,
    ORD_LOW_USE_METER, ORD_CRC_BEFORE, ORD_CRC_RETAIN, ORD_CRC_EMERGENCY, ORD_CRC_ISSUE, ORD_PRIME_ANNUAL,
    ORD_PRIME_ONLY, ORD_PRIME_REPORT, ORD_PRIME_DISCLOSE, ORD_PRIME_SIGN, ORD_PRIME_DEF, ORD_AWARD_DEF, ORD_ENFORCE,
    ORD_ZETA, ORD_PENALTIES,
    TB_APPLIES, TB_2010, TB_HIRE, TB_LEASE, TB_SALE, TB_LOW_USE, TB_NOX, TB_RECORDS, TB_PENALTIES, TB_DMV,
    CTC_APPLIES, CTC_CONDITIONS, CTC_FREQ, CTC_START, CTC_ALIGN, CTC_WINDOW, CTC_FEE, CTC_DMV, CTC_TRANSFER, CTC_HIRE,
    CTC_RECORDS, CTC_PASS, CTC_TAMPER, CTC_PENALTIES, ACF, IDLE_TRUCK, IDLE_PTO, IDLE_RESTRICTED, IDLE_FINE,
    IDLE_CARRIER,
    PERP_VOLUNTARY, PERP_PORTABLE, PERP_TERM, PERP_FEES, PERP_PROJECT, PERP_ELIGIBLE, PERP_ATCM, PERP_FLEET,
    PERP_LOW_USE, PERP_METER,
    SC_VISIBLE, SC_BACM, SC_PM10, SC_TRACKOUT, SC_EXIT, SC_LARGE, SC_403N, SC_LARGE_RECORDS, SC_SIGNAGE,
    SC_SUPERVISOR, SC_LARGE_ENDS, SC_RENEW, SC_WIND, SC_TABLE2, SC_GRADING, SC_STOCKPILE, SC_CONTRACT_END, CV_PLAN,
    SJV_DCP, SJV_REVIEW, SJV_48H, SJV_ONSITE, SJV_VDE, SJV_INACTIVE, SJV_SPEED, SJV_WIND, SJV_DEMO, SJV_EXEMPT,
    SJV_FEE, SJV_EOD, SJV_50FT, SJV_DEVICE, SJV_SPECS, SJV_RECORDS, SJV_COUNTIES,
    BA_BASIC, BA_ENHANCED, BA_LARGE, BA_TRACKOUT, BA_OPACITY, BA_MONITOR,
)

_COUNTY_SPELLING_RULE = "usa-regional-centers-by-county"      # spelling only, never cited
SQFT_PER_ACRE = 43560                                          # unit conversion, not a legal value
# Input sanity limits, not legal values: a number above these is a typo, not a real fleet, machine, truck or site.
MAX_FLEET_HP, MAX_MACHINE_HP, MAX_GVWR_LBS, MAX_BHP = 10_000_000, 20_000, 1_000_000, 20_000
MAX_ACRES, MAX_CY_PER_DAY, MAX_SQFT, MAX_MILES, MAX_FEET = 1_000_000, 10_000_000, 100_000_000, 1_000_000, 30_000
HOURS_PER_LEAP_YEAR = 8784                                     # 366 x 24: the most hours a year can have
MAX_REGISTRATION_AHEAD_DAYS = 400       # a current California registration card expires within about a year


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _n(x: float) -> str:
    """Number for display: thousands separators, at most 2 decimals, no trailing zeros."""
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _join(items, last: str = "and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def _words(text) -> str:
    """Lowercase, punctuation to spaces, single spaces: 'Tier-4 (Final)' -> 'tier 4 final'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+]+", " ", str(text or "").lower())).strip()


_UNIT_TAIL = re.compile(r"^[a-z/%'. ]*$")


def _num(name: str, value, lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    """A number, or a string like '33,000 lb', '158 hp', '2.5 ac' (the unit words are ignored); None stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.match(r"^\s*(-?\d*\.?\d+)\s*(.*?)\s*$", str(value).replace(",", ""))
        if not m or not _UNIT_TAIL.match(m.group(2).lower()):
            raise ValueError(f"{name} must be a number, got {value!r}")
        v = float(m.group(1))
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    def bound(x: float) -> str:                       # 1900 stays 1900 (a year); 10000000 becomes 10,000,000
        return _n(x) if abs(x) >= 10000 else f"{x:g}"
    if lo is not None and v < lo:
        raise ValueError(f"{name} cannot be below {bound(lo)}, got {value!r}")
    if hi is not None and v > hi:
        raise ValueError(f"{name} cannot be above {bound(hi)}, got {value!r}")
    return v


def _whole(name: str, value, lo: float = 0, hi: Optional[float] = None) -> Optional[int]:
    v = _num(name, value, lo, hi)
    if v is None:
        return None
    if v != int(v):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return int(v)


def _year(name: str, value) -> Optional[int]:
    return _whole(name, value, 1900, 2100)


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


_US_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$")


def _date(value, field: str) -> Optional[date]:
    """ISO date (2026-11-25, or the date part of 2026-11-25T08:00) or the US form 11/25/2026 (month first)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    us = _US_DATE.match(str(value))
    try:
        if us:
            return date(int(us[3]), int(us[1]), int(us[2]))
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2026-11-25 (or 11/25/2026), got {value!r}") from None


def _as_of(value) -> date:
    return _date(value, "as_of") or logic.today_ca()


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _pdate(value) -> date:
    """The first ISO date in a rulebook value: '2024-01-01' or 'earlier of: ..., or 2037-01-01'."""
    m = _ISO.search(str(value))
    if not m:
        raise KeyError(f"no ISO date in rulebook value {value!r}")
    return date(int(m[1]), int(m[2]), int(m[3]))


def _pmd(value) -> tuple[int, int]:
    """(month, day) from a rulebook value that starts 'MM-DD': '03-01', '04-30 after each season used'."""
    m = re.match(r"^\s*(\d{2})-(\d{2})\b", str(value))
    if not m:
        raise KeyError(f"no MM-DD date in rulebook value {value!r}")
    return int(m[1]), int(m[2])


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def _month_day_text(value) -> tuple[int, int]:
    """(month, day) from rulebook text like 'January 31 of the compliance year'."""
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", str(value).lower())
    if not m:
        raise KeyError(f"no month and day in rulebook value {value!r}")
    return _MONTHS[m[1]], int(m[2])


def _days_in_text(value) -> int:
    m = re.search(r"within (\d+) days", str(value))
    if not m:
        raise KeyError(f"no 'within N days' in rulebook value {value!r}")
    return int(m[1])


def _add_months(d: date, n: int) -> date:
    """Same day n months away; the last day of the month when that day does not exist (Aug 31 - 6 months = Feb 28)."""
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _add_years(d: date, n: int) -> date:
    return _add_months(d, 12 * n)


def _next_md(md: tuple[int, int], today: date) -> date:
    d = date(today.year, *md)
    return d if d >= today else date(today.year + 1, *md)


def _business_days_after(d: date, n: int) -> date:
    """n weekdays after d. Holidays are NOT skipped, so the date is the earliest the deadline can be."""
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _given(x: Optional[float], unit: str) -> str:
    return "not given" if x is None else f"{_n(x)} {unit}"


def _names(value, field: str) -> Optional[list[str]]:
    """A list of names: ['Kern', 'Tulare'], or one string 'Kern, Tulare' / 'Kern and Tulare'; None or empty -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        return [c.strip() for c in re.split(r"[;,/]| and ", value) if c.strip()] or None
    if isinstance(value, (list, tuple)) and all(isinstance(c, str) for c in value):
        return [c for c in value if c.strip()] or None
    raise ValueError(f"{field} must be a list of county names (or one comma-separated string), got {value!r}")


def _cit(ids) -> str:
    return "; ".join(dict.fromkeys(core.rule(i).citation_text or i for i in ids))


class Requirement(BaseModel):
    item: str
    detail: str
    due: Optional[str] = Field(None, description="ISO date this must be done by, when one can be computed")
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


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

    def req(self, item: str, detail: str, *ids: str, due: Optional[date] = None) -> Requirement:
        self.use(*ids)
        ids = list(dict.fromkeys(ids))
        detail = detail[:1].upper() + detail[1:]
        r = Requirement(item=item, detail=detail, due=_iso(due), rule_ids=ids, citation=_cit(ids),
                        verified=bool(ids) and all(core.rule(i).verified for i in ids))
        self.reqs.append(r)
        return r

    def rule(self, rid: str, item: Optional[str] = None, extra: str = "", due: Optional[date] = None) -> Requirement:
        """A requirement worded exactly as the rulebook's plain-English text for that rule."""
        r = core.rule(rid)
        return self.req(item or r.title or rid, (r.plain_english or "") + (f" {extra}" if extra else ""), rid,
                        due=due)

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
                             + " Confirm with the official text before relying on it.")
        res.math = self.math + [m for m in res.math if m not in self.math]
        return res


class _Base(core.Result):
    summary: str = ""
    next_step: str = ""
    math: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)


# ---------------------------------------------------------------- counties (spelling only, not law)
_COUNTY_ABBREV = {"la": "Los Angeles", "oc": "Orange", "sd": "San Diego", "sf": "San Francisco",
                  "slo": "San Luis Obispo"}
_COUNTY_AMBIGUOUS = {"sb": "San Bernardino or Santa Barbara", "sc": "Santa Clara or Santa Cruz",
                     "sj": "San Joaquin County or the City of San Jose", "sm": "San Mateo, or a city"}


def _ca_counties() -> list[str]:
    names = set(P(ORD_CAPTIVE, "counties")) | set(P(SJV_COUNTIES, "counties"))
    try:
        sp = core.rule(_COUNTY_SPELLING_RULE)
        names |= {c for k in ("usa_south_digalert_counties", "usa_north_counties") for c in sp.param(k, [])}
    except KeyError:
        pass
    for full, partial in _SCOPE_COUNTIES.values():
        names |= set(full) | set(partial)
    return sorted(names)


def _county(value, field: str = "county") -> tuple[str, Optional[str]]:
    """'Kern', 'County of Kern', 'kern co.', 'Kern County, CA' -> ('Kern', None); 'City of San Diego' or 'SD' ->
    ('San Diego', note). A city whose name is not a county raises a ValueError asking for the county."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} is required, e.g. 'Fresno' or 'Orange County'")
    t = re.sub(r"\s+", " ", raw.lower().replace(".", "").replace("_", " ").replace(",", " ")).strip()
    t = re.sub(r"\s+(ca|calif|california|usa)$", "", t).strip()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"^(city and )?county of\s+", "", t)
    city = re.match(r"^(?:city|town) of\s+(.+)$", t)
    if city:
        t = city.group(1)
    t = re.sub(r"\s+(county|co|cnty|cty)$", "", t).strip()
    by_lower = {c.lower(): c for c in _ca_counties()}
    key = t.replace(" ", "")
    name = by_lower.get(t) or _COUNTY_ABBREV.get(key)
    if not name and key in _COUNTY_AMBIGUOUS:
        raise ValueError(f"{raw!r} is ambiguous ({_COUNTY_AMBIGUOUS[key]}); spell out the county.")
    if not name:
        close = get_close_matches(t, list(by_lower), n=3, cutoff=0.75)
        hint = (f" Did you mean {_join([by_lower[c] for c in close], 'or')}?" if close else
                " If it is a city, give the county the jobsite is in (or the air district, e.g. 'South Coast').")
        raise ValueError(f"{raw!r} is not a California county or air district name.{hint}")
    note = f"{raw!r} was read as {name} County." if (city or name.lower() != t) else None
    return name, note


# ================================================================ off-road fleets (13 CCR 2449)
OwnerType = Literal["business", "non_profit", "local_municipality", "low_population_county_municipality",
                    "non_profit_training_center", "state_agency", "federal_agency"]
FleetSize = Literal["small", "medium", "large"]
_OWNER_ALIASES = {
    "business": {"business", "company", "private", "private company", "private business", "contractor",
                 "corporation", "corp", "inc", "llc", "lp", "partnership", "sole proprietor", "for profit"},
    "non_profit": {"non profit", "nonprofit", "non profit organization", "nonprofit organization", "501c3",
                   "501 c 3", "charity"},
    "local_municipality": {"local municipality", "municipality", "city", "county", "local government",
                           "local agency", "public agency", "special district", "water district", "school district",
                           "municipal", "city or county"},
    "low_population_county_municipality": {"low population county municipality", "low population county",
                                           "local municipality in a low population county",
                                           "municipality in a low population county",
                                           "low population county fleet"},
    "non_profit_training_center": {"non profit training center", "nonprofit training center", "training center"},
    "state_agency": {"state", "state agency", "state of california", "california state agency", "caltrans",
                     "state government"},
    "federal_agency": {"federal", "federal agency", "united states", "us government", "federal government",
                       "us agency", "u s government"},
}
# Owner type -> the wording of its entry in the always_small / always_large lists of 13 CCR 2449(c)(28).
_OWNER_RULE_TEXT = {"state_agency": "State of California agencies", "federal_agency": "United States agencies",
                    "non_profit_training_center": "non-profit training center",
                    "low_population_county_municipality": "local municipality fleet in a low-population county"}
Designation = Literal["none", "permanent_low_use", "job_corps", "snow_removal", "emergency"]
# Designation -> its entry in the total-max-hp exclusions of 2449(c)(64) (Job Corps vehicles are not excluded).
_DESIGNATION_EXCLUSION = {"permanent_low_use": "permanent low-use vehicles",
                          "snow_removal": "dedicated snow-removal vehicles",
                          "emergency": "vehicles used solely for emergency operations"}
_TIERS = ("Tier 0", "Tier 1", "Tier 2", "Tier 3", "Tier 4 interim", "Tier 4 final")


def _owner(value) -> str:
    w = _words(value or "business")
    if w.replace(" ", "_") in _OWNER_ALIASES:
        return w.replace(" ", "_")
    for key, names in _OWNER_ALIASES.items():
        if w in names:
            return key
    raise ValueError("fleet_owner_type must be business, non_profit, local_municipality, "
                     "low_population_county_municipality, non_profit_training_center, state_agency or federal_agency; "
                     f"got {value!r}")


def _designation(value) -> str:
    w = _words(value or "none")
    if w in ("none", "no", "n a", "na", "normal", "regular"):
        return "none"
    if "low use" in w or w in ("plu", "lowuse", "low"):
        return "permanent_low_use"
    if "job corp" in w:
        return "job_corps"
    if "snow" in w:
        return "snow_removal"
    if "emergenc" in w:
        return "emergency"
    raise ValueError("designation must be none, permanent_low_use, job_corps, snow_removal or emergency; "
                     f"got {value!r}")


def _engine(value) -> tuple[str, Optional[int]]:
    """'Tier 2', 'T4i', 'tier 4 final', 'on-road 2005', 'zero-emission' -> ('Tier 2', None) / ('on-road', 2005) /
    ('zero-emission', None)."""
    w = _words(value)
    if not w:
        raise ValueError("engine_tier is required, e.g. 'Tier 3', 'Tier 4 final', 'on-road 2006' or 'zero-emission'")
    if re.search(r"\b(zero emission|zev|electric|battery|ev|fuel cell|hydrogen)\b", w):
        return "zero-emission", None
    if re.search(r"\b(on road|onroad|truck engine)\b", w):
        y = re.search(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", w)          # 'on-road 2005' or 'on-road MY2005'
        if not y:
            raise ValueError(f"an on-road engine needs its model year, e.g. 'on-road 2005'; got {value!r}")
        return "on-road", int(y.group(1))
    s = re.sub(r"[^a-z0-9]", "", w).replace("tier", "t")
    s = {"uncontrolled": "t0", "0": "t0", "1": "t1", "2": "t2", "3": "t3"}.get(s, s)
    if s in ("t4", "4"):
        raise ValueError("'Tier 4' is ambiguous: say Tier 4 interim (T4i) or Tier 4 final (T4f); the engine's "
                         "emission control label shows which")
    table = {"t0": "Tier 0", "t1": "Tier 1", "t2": "Tier 2", "t3": "Tier 3"}
    for k in ("t4i", "t4interim", "interimt4", "4i", "t4int"):
        table[k] = "Tier 4 interim"
    for k in ("t4f", "t4final", "finalt4", "4f", "t4fin"):
        table[k] = "Tier 4 final"
    if s in table:
        return table[s], None
    raise ValueError("engine_tier must be Tier 0, Tier 1, Tier 2, Tier 3, Tier 4 interim (T4i), Tier 4 final (T4f), "
                     f"'on-road <model year>' or zero-emission; got {value!r}")


class OffRoadVehicle(BaseModel):
    name: str = Field("", description="Your name for the machine, e.g. 'CAT 320 #14'")
    hp: float = Field(ge=0, le=MAX_MACHINE_HP, allow_inf_nan=False, description="Maximum engine horsepower (max hp)")
    engine_tier: str = Field(description=(
        "Tier 0, Tier 1, Tier 2, Tier 3, Tier 4 interim (T4i), Tier 4 final (T4f), 'on-road 2005' for an on-road "
        "engine with its model year, or zero-emission"))
    designation: str = Field("none", description=(
        "CARB designation: none, permanent_low_use, job_corps, snow_removal or emergency"))
    annual_hours: Optional[float] = Field(None, ge=0, le=HOURS_PER_LEAP_YEAR, allow_inf_nan=False, description=(
        "Engine hours in a calendar year (for the permanent low-use test)"))


class VehiclePlan(BaseModel):
    name: str
    hp: float
    engine: str = Field(description="'Tier 2', 'on-road MY2005' or 'zero-emission'")
    designation: str
    subject: bool = Field(description="True when 13 CCR 2449 covers this machine (diesel, at or above its hp threshold)")
    counted_in_fleet_hp: bool
    status: Literal["not_subject", "no_phase_out_date", "phase_out_scheduled", "may_not_operate", "special_provision"]
    may_not_operate_from: Optional[str] = Field(None, description="First day it may not be operated in California")
    designate_by: Optional[str] = Field(None, description=(
        "Last day to designate it as a special-provision vehicle (e.g. permanent low-use) to avoid that date"))
    zev_delay_to: Optional[str] = Field(None, description=(
        "The later phase-out date if a zero-emission machine added under 13 CCR 2449.1(d) is used to delay it"))
    detail: str = ""
    rule_ids: list[str] = Field(default_factory=list)


class PhaseOutStep(BaseModel):
    date: str
    tier: str
    onroad_model_year_max: int
    in_effect: bool


class OptionalScheduleCheck(BaseModel):
    counted_hp: float
    tier2_or_better_hp: float
    percent_tier2_or_better: float
    required_percent: Optional[float] = None
    required_since: Optional[str] = None
    meets_current_requirement: Optional[bool] = None
    next_date: Optional[str] = None
    next_required_percent: Optional[float] = None


class OffRoadFleetResult(_Base):
    subject: bool = Field(description=(
        "True when the fleet has at least one off-road diesel vehicle at or above the 13 CCR 2449(b) hp threshold"))
    total_fleet_hp: float
    hp_basis: str
    fleet_owner_type: str
    captive_attainment_area: Optional[bool] = None
    fleet_size: Optional[FleetSize] = None
    optional_500hp_schedule: bool = False
    may_add_now: Optional[str] = Field(None, description="Lowest engine tier you may add to the fleet today")
    may_add_onroad_engine_model_year_min: Optional[int] = None
    adding_changes: list[str] = Field(default_factory=list)
    phase_out_schedule: list[PhaseOutStep] = Field(default_factory=list)
    vehicles: list[VehiclePlan] = Field(default_factory=list)
    optional_schedule_check: Optional[OptionalScheduleCheck] = None
    next_annual_report_due: Optional[str] = None
    report_vehicles_as_of: Optional[str] = None
    renewable_diesel_required: Optional[bool] = None


def _captive(counties: list[str], acc: _Acc) -> tuple[Optional[bool], list[str]]:
    """All operating counties in the 2449(c)(6) list -> True. Sonoma counts only for its North Coast Air Basin part,
    which the user must say ('northern Sonoma', 'Sonoma (North Coast)'). This is the county test only: the caller
    applies the rule that fleets of the United States or the State of California are never captive."""
    acc.use(ORD_CAPTIVE)
    listed = set(P(ORD_CAPTIVE, "counties"))
    partial = str(P(ORD_CAPTIVE, "partial_county"))
    partial_name = partial.split(" (")[0]
    outside, unsure = [], []
    for raw in counties:
        w = _words(raw)
        if partial_name.lower() in w and re.search(r"\b(north|northern|north coast)\b", w):
            continue
        name, _ = _county(raw, "operating_counties")
        if name == partial_name:
            unsure.append(name)
        elif name not in listed:
            outside.append(name)
    if outside:
        return False, outside
    if unsure:
        acc.notes.append(f"Only part of {partial_name} County counts ({partial}). Say 'northern {partial_name}' if "
                         "every machine works only in that part.")
        return None, unsure
    return True, []


def _fleet_size(hp: float, owner: str, captive: Optional[bool], acc: _Acc) -> tuple[str, str]:
    acc.use(ORD_SIZE)
    small_max, large_over = P(ORD_SIZE, "small_max_hp"), P(ORD_SIZE, "large_min_hp_exclusive")
    always_small, always_large = P(ORD_SIZE, "always_small"), P(ORD_SIZE, "always_large")
    if _OWNER_RULE_TEXT.get(owner) in always_large:
        return "large", f"{_OWNER_RULE_TEXT[owner]} are always a large fleet, whatever their horsepower"
    if captive and "captive attainment area fleet" in always_small:
        return "small", "a captive attainment area fleet is a small fleet whatever its horsepower"
    if _OWNER_RULE_TEXT.get(owner) in always_small:
        return "small", f"a {_OWNER_RULE_TEXT[owner]} is a small fleet whatever its horsepower"
    if hp > large_over:
        return "large", f"{_n(hp)} hp > {_n(large_over)} hp -> large fleet"
    if hp <= small_max:
        return "small", f"{_n(hp)} hp <= {_n(small_max)} hp -> small fleet"
    # 2449(c)(28)(B): a medium fleet is one that is neither small (<= small_max) nor large (> large_over)
    return "medium", f"{_n(small_max)} hp < {_n(hp)} hp <= {_n(large_over)} hp -> medium fleet"


def _schedule(size: str, optional_500: bool, acc: _Acc) -> list[dict]:
    rid = ORD_PH[size]
    acc.use(rid)
    if P(rid, "fleet_size") != size:
        raise KeyError(f"rule {rid} is not the {size} fleet schedule")
    steps = [dict(s) for s in P(rid, "phaseout")]
    if optional_500:
        acc.use(ORD_PH_500)
        for s in steps:
            if s["tier"] == P(ORD_PH_500, "tier"):
                s["date"] = P(ORD_PH_500, "date")
    return sorted(steps, key=lambda s: s["date"])


def _stop(kind: str, my: Optional[int], steps: list[dict]) -> Optional[dict]:
    """The first schedule step that bans this engine: same tier, or an on-road engine of that model year or older."""
    for s in steps:
        if kind == s["tier"] or (kind == "on-road" and my is not None and my <= s["onroad_model_year_max"]):
            return s
    return None


def _vehicle_plan(v: OffRoadVehicle, steps: list[dict], as_of: date, acc: _Acc) -> VehiclePlan:
    kind, my = _engine(v.engine_tier)
    des = _designation(v.designation)
    label = f"on-road MY{my}" if kind == "on-road" else kind
    name = v.name.strip() or f"{_n(v.hp)} hp {label}"
    min_hp = P(ORD_APPLIES, "min_engine_max_hp")
    ids = [ORD_APPLIES]
    if kind == "zero-emission":
        return VehiclePlan(name=name, hp=v.hp, engine=label, designation=des, subject=False, counted_in_fleet_hp=False,
                           status="not_subject", rule_ids=ids, detail=(
                               "Zero-emission: no diesel engine, so 13 CCR 2449 does not cover it and its hp is not "
                               "in the fleet total. Each one added on or after "
                               f"{P(ORD_ZEV_DELAY, 'zev_added_on_or_after')} can delay one Tier 1 or Tier 2 "
                               f"phase-out by {P(ORD_ZEV_DELAY, 'delay_years')} years."))
    if v.hp < min_hp:
        return VehiclePlan(name=name, hp=v.hp, engine=label, designation=des, subject=False, counted_in_fleet_hp=False,
                           status="not_subject", rule_ids=ids,
                           detail=f"{_n(v.hp)} hp < {_n(min_hp)} hp: not covered by the off-road fleet rule.")
    excl = P(ORD_HP_EXCL, "excluded_from_total_hp")
    counted = _DESIGNATION_EXCLUSION.get(des) not in excl
    ids.append(ORD_HP_EXCL)
    plan = VehiclePlan(name=name, hp=v.hp, engine=label, designation=des, subject=True, counted_in_fleet_hp=counted,
                       status="no_phase_out_date", rule_ids=ids)
    step = _stop(kind, my, steps)
    if des in ("permanent_low_use", "job_corps"):
        lu = core.rule(ORD_PH_LOW_USE)
        plan.status = "special_provision"
        ids += [ORD_SPECIAL, ORD_PH_LOW_USE]
        detail = (f"Designated {des.replace('_', ' ')}: the regular phase-out dates do not apply if it was designated "
                  "by December 31 of the year before its phase-out date")
        if step:
            plan.designate_by = date(_pdate(step["date"]).year - 1, 12, 31).isoformat()
            detail += f" (for this machine: by {plan.designate_by}, or it may not operate from {step['date']})"
        detail += "."
        if kind == lu.param("tier") or (kind == "on-road" and my <= lu.param("onroad_model_year_max")):
            stop = _pdate(lu.param("date"))
            plan.may_not_operate_from = stop.isoformat()
            detail += f" Even so, a {kind if kind != 'on-road' else label} engine may not operate from {stop}."
            if stop <= as_of:
                plan.status = "may_not_operate"
        if des == "permanent_low_use":
            ids.append(ORD_LOW_USE)
            limit = P(ORD_LOW_USE, "max_hours_per_calendar_year_exclusive")
            avg = P(ORD_LOW_USE, "three_year_average_max_hours")
            detail += f" It must run less than {_n(limit)} hours each calendar year"
            if v.annual_hours is not None and v.annual_hours >= limit:
                detail += f": {_n(v.annual_hours)} hours is NOT under {_n(limit)}, so it does not qualify that way"
            detail += (f" (or, since {P(ORD_LOW_USE, 'three_year_option_from')}, average {_n(avg)} hours or fewer a year "
                       "over three consecutive years if it runs only in California under the same owner, with both "
                       "hour meter readings reported).")
        plan.detail = detail
        acc.use(*ids)
        return plan
    if des in ("snow_removal", "emergency"):
        plan.status = "special_provision"
        ids.append(ORD_SPECIAL)
        plan.detail = (f"Designated {des.replace('_', ' ')}: it is treated under its 13 CCR 2449(e) special provision, "
                       "not the regular phase-out dates, if designated by December 31 of the year before its "
                       "phase-out date.")
        if step:
            plan.designate_by = date(_pdate(step["date"]).year - 1, 12, 31).isoformat()
            plan.detail += f" For this machine: by {plan.designate_by}."
        acc.use(*ids)
        return plan
    if not step:
        plan.detail = ("No Tier phase-out date for this engine under 13 CCR 2449.1(c). It still counts toward the "
                       "fleet average / BACT requirements, which this tool does not compute.")
        acc.use(*ids)
        return plan
    stop = _pdate(step["date"])
    plan.may_not_operate_from = stop.isoformat()
    plan.designate_by = date(stop.year - 1, 12, 31).isoformat()
    ids.append(ORD_SPECIAL)
    if step["tier"] in P(ORD_ZEV_DELAY, "eligible_tiers"):
        plan.zev_delay_to = _add_years(stop, P(ORD_ZEV_DELAY, "delay_years")).isoformat()
        ids.append(ORD_ZEV_DELAY)
    what = label if kind == "on-road" else kind
    if stop <= as_of:
        plan.status = "may_not_operate"
        plan.detail = (f"{what}: may not be operated in California since {stop} ({step['tier']} phase-out for this "
                       "fleet size). Retire, sell, repower or replace it.")
    else:
        plan.status = "phase_out_scheduled"
        plan.detail = (f"{what}: may not be operated in California from {stop} ({(stop - as_of).days} days). To keep "
                       f"it longer, designate it permanent low-use by {plan.designate_by}")
        if plan.zev_delay_to:
            plan.detail += (f", or add a zero-emission machine to delay it to {plan.zev_delay_to} (only if it was "
                            f"reported to CARB before {P(ORD_ZEV_DELAY, 'delayed_vehicle_reported_before')})")
        plan.detail += "."
        limit = P(ORD_LOW_USE, "max_hours_per_calendar_year_exclusive")
        if v.annual_hours is not None and v.annual_hours < limit:
            ids.append(ORD_LOW_USE)
            plan.detail += (f" At {_n(v.annual_hours)} hours a year (< {_n(limit)}) it could qualify as permanent "
                            "low-use.")
    acc.use(*ids)
    return plan


def _optional_check(vehicles: list[VehiclePlan], as_of: date, acc: _Acc) -> Optional[OptionalScheduleCheck]:
    """13 CCR 2449(e)(16) Table 1: share of hp with a Tier 2 or higher engine. 'All vehicles in the fleet must be
    included', so every subject machine counts (designated low-use ones too). An on-road engine has no off-road tier;
    it is counted at the tier 2449.1(c) pairs it with: a model year newer than the Tier 1 step's on-road model year
    (MY2000+) is Tier 2 or better. Zero-emission machines are not subject to 2449, so they are not in the fleet."""
    acc.use(ORD_OPT_500, ORD_PH["small"])
    better = set(_TIERS[_TIERS.index("Tier 2"):])
    t1_onroad_max = next(s["onroad_model_year_max"] for s in P(ORD_PH["small"], "phaseout") if s["tier"] == "Tier 1")
    rows = [v for v in vehicles if v.subject]
    total = sum(v.hp for v in rows)
    if not total:
        return None

    def tier2_plus(v: VehiclePlan) -> bool:
        if v.engine.startswith("on-road MY"):
            return int(v.engine[len("on-road MY"):]) > t1_onroad_max
        return v.engine in better

    good = sum(v.hp for v in rows if tier2_plus(v))
    pct = good / total * 100
    table = sorted((_pdate(k), float(p)) for k, p in P(ORD_OPT_500, "min_percent_hp_tier2_or_higher").items())
    past = [(d, p) for d, p in table if d <= as_of]
    nxt = next(((d, p) for d, p in table if d > as_of), None)
    chk = OptionalScheduleCheck(counted_hp=total, tier2_or_better_hp=good, percent_tier2_or_better=round(pct, 1))
    if any(v.engine.startswith("on-road MY") for v in rows):
        acc.notes.append(f"Optional schedule: on-road engines were counted as Tier 2 or better when newer than model "
                         f"year {t1_onroad_max} (the Tier 1 / on-road pairing of 13 CCR 2449.1(c)). That pairing is "
                         "this tool's reading; CARB's DOORS calculation governs.")
    msg = f"Optional schedule: {_n(good)} hp Tier 2 or better / {_n(total)} hp = {pct:.1f}%"
    if past:
        d, p = past[-1]
        chk.required_percent, chk.required_since = p, d.isoformat()
        chk.meets_current_requirement = pct + 1e-9 >= p
        need = p / 100 * total
        msg += (f" vs {_n(p)}% required since {d} ({_n(p)}% x {_n(total)} hp = {_n(need)} hp) -> "
                + ("meets it" if chk.meets_current_requirement else f"short {_n(need - good)} hp"))
    if nxt:
        chk.next_date, chk.next_required_percent = nxt[0].isoformat(), nxt[1]
        msg += f"; {_n(nxt[1])}% from {nxt[0]}"
    acc.math.append(msg)
    return chk


def offroad_fleet_obligations(total_fleet_hp=None, fleet_owner_type="business", vehicles=None, operating_counties=None,
                              uses_optional_500hp_schedule=None, rents_out_equipment=False, new_fleet=False,
                              vehicle_added_on=None, as_of=None) -> OffRoadFleetResult:
    """CARB In-Use Off-Road Diesel-Fueled Fleets (13 CCR 2449-2449.2) for one fleet."""
    today = _as_of(as_of)
    owner = _owner(fleet_owner_type)
    hp_given = _num("total_fleet_hp", total_fleet_hp, 0, MAX_FLEET_HP)
    rents_out = bool(_bool("rents_out_equipment", rents_out_equipment))
    is_new = bool(_bool("new_fleet", new_fleet))
    added_on = _date(vehicle_added_on, "vehicle_added_on")
    opt_wanted = _bool("uses_optional_500hp_schedule", uses_optional_500hp_schedule)
    operating_counties = _names(operating_counties, "operating_counties")
    if isinstance(vehicles, (dict, OffRoadVehicle)):
        vehicles = [vehicles]                                     # one machine given on its own
    if vehicles is not None and not isinstance(vehicles, (list, tuple)):
        raise ValueError(f"vehicles must be a list of machines like [{{'hp': 158, 'engine_tier': 'Tier 2'}}], "
                         f"got {vehicles!r}")
    machines = [v if isinstance(v, OffRoadVehicle) else OffRoadVehicle.model_validate(v) for v in (vehicles or [])]
    if hp_given is None and not machines:
        raise ValueError("give total_fleet_hp (the total max hp of every off-road diesel vehicle under common "
                         "ownership or control) or the list of vehicles")
    acc = _Acc()
    acc.use(ORD_APPLIES, ORD_HP_EXCL, ORD_COMMON)
    min_hp = P(ORD_APPLIES, "min_engine_max_hp")

    # total max hp: the sum over subject engines, leaving out the 2449(c)(64) exclusions
    excl = P(ORD_HP_EXCL, "excluded_from_total_hp")

    def subject(v: OffRoadVehicle) -> bool:
        return _engine(v.engine_tier)[0] != "zero-emission" and v.hp >= min_hp

    counted = [v for v in machines if subject(v) and _DESIGNATION_EXCLUSION.get(_designation(v.designation))
               not in excl]
    counted_from_list = sum(v.hp for v in counted)
    if hp_given is not None:
        hp, basis = hp_given, "total_fleet_hp you gave"
        if machines and abs(counted_from_list - hp_given) > 1e-9:
            acc.notes.append(f"The machines listed add up to {_n(counted_from_list)} counted hp; the fleet size uses "
                             f"the {_n(hp_given)} hp total you gave (every company under common ownership or "
                             "control counts).")
    else:
        hp, basis = counted_from_list, (f"sum of the listed machines ({_n(min_hp)} hp+, diesel, not low-use, "
                                        "snow-removal or emergency-only)")
        acc.math.append(("Fleet hp = " + " + ".join(_n(v.hp) for v in counted) + f" = {_n(hp)} hp") if counted
                        else "Fleet hp = 0 (no counted machines)")
    has_subject = hp > 0 or any(subject(v) for v in machines)
    res = OffRoadFleetResult(subject=has_subject, total_fleet_hp=hp, hp_basis=basis, fleet_owner_type=owner)
    if not has_subject:
        res.summary = (f"No off-road diesel vehicle of {_n(min_hp)} hp or more: CARB's off-road fleet rule does not "
                       "apply to this fleet.")
        res.next_step = ("Nothing to file for the off-road rule. If you are the prime contractor on a job where other "
                         "companies' equipment runs, use offroad_contracting_requirements.")
        return acc.finish(res)

    captive, outside = (None, [])
    in_listed_counties = False          # every operating county is a 2449(c)(6) county (R99 vehicle exemption)
    if operating_counties:
        captive, outside = _captive(list(operating_counties), acc)
        in_listed_counties = captive is True
        if captive is False:
            acc.math.append(f"Captive attainment area: no ({_join(outside)} not in the 2449(c)(6) list)")
        elif captive and owner in ("state_agency", "federal_agency"):
            # 2449(c)(6): fleets of the United States or the State of California are large fleets and never captive,
            # even when every vehicle stays in the listed counties.
            captive = False
            acc.math.append(f"Captive attainment area: no ({_OWNER_RULE_TEXT[owner]} are never captive attainment "
                            "area fleets under 13 CCR 2449(c)(6), even inside the listed counties)")
        elif captive:
            acc.math.append("Captive attainment area: yes (every operating county is in the 2449(c)(6) list)")
        res.captive_attainment_area = captive
    size, why = _fleet_size(hp, owner, captive, acc)
    res.fleet_size = size
    acc.math.append(f"Fleet size: {why}")
    opt_max = P(ORD_OPT_500, "max_fleet_hp")
    if opt_wanted and (size != "small" or hp > opt_max):
        raise ValueError(f"the optional schedule is only for fleets of {_n(opt_max)} hp or less; this fleet is "
                         f"{_n(hp)} hp ({size})")
    opt = bool(opt_wanted) and hp <= opt_max
    res.optional_500hp_schedule = opt

    # adding vehicles (2449(d)(6))
    acc.use(ORD_ADD_T0, ORD_ADD_T3, ORD_ADD_T4I, ORD_ADD_ONROAD)
    t0 = _pdate(P(ORD_ADD_T0, "us_epa_authorization_date"))
    t3 = _pdate(P(ORD_ADD_T3, "effective"))
    key = "optional_500hp_or_less" if opt else size
    t4i = _pdate(P(ORD_ADD_T4I, "effective_by_fleet_size")[key])
    onroad = _pdate(P(ORD_ADD_ONROAD, "effective_by_fleet_size")[key])
    t4f_min, t4i_min = P(ORD_ADD_T4I, "minimum_tier_allowed_after_ban"), P(ORD_ADD_T3, "minimum_tier_allowed")
    if today >= t4i:
        res.may_add_now = f"{t4f_min} or higher"
    elif today >= t3 and size in P(ORD_ADD_T3, "fleet_sizes"):
        res.may_add_now = f"{t4i_min} or higher"
        res.adding_changes.append(f"From {t4i}: only {t4f_min} or higher may be added (13 CCR 2449(d)(6)"
                                  f"{'(F)' if opt else '(E)'}).")
    else:
        res.adding_changes.append(f"From {t3}: no Tier 3 additions; from {t4i}: only {t4f_min} or higher.")
    if today >= onroad:
        res.may_add_onroad_engine_model_year_min = P(ORD_ADD_ONROAD, "minimum_model_year_allowed")
    else:
        res.adding_changes.append(f"From {onroad}: an on-road-engine vehicle you add must have a model year "
                                  f"{P(ORD_ADD_ONROAD, 'minimum_model_year_allowed')} or newer engine.")
    if res.may_add_now:
        txt = f"Add only vehicles with {res.may_add_now} engines (zero-emission machines are not limited by these bans)"
        if res.may_add_onroad_engine_model_year_min:
            txt += (f"; on-road-engine vehicles must have a model year {res.may_add_onroad_engine_model_year_min} or "
                    "newer engine")
        txt += (f". Tier 0 additions are banned for every fleet ({'since' if today >= t0 else 'from'} {t0}, U.S. EPA "
                "authorization); Tier 3 since "
                f"{t3}. 'Adding' means buying, leasing for a year or more, or bringing a vehicle into California.")
        if res.adding_changes:
            txt += " " + " ".join(res.adding_changes)
        acc.req("What you may add to the fleet", txt, ORD_ADD_T0, ORD_ADD_T3, ORD_ADD_T4I, ORD_ADD_ONROAD)

    # Tier phase-out (2449.1(c))
    steps = _schedule(size, opt, acc)
    res.phase_out_schedule = [PhaseOutStep(date=s["date"], tier=s["tier"],
                                           onroad_model_year_max=s["onroad_model_year_max"],
                                           in_effect=_pdate(s["date"]) <= today) for s in steps]
    sched = "; ".join(f"{s['tier']} (or on-road MY{s['onroad_model_year_max']} and older) "
                      f"{'banned since' if _pdate(s['date']) <= today else 'banned from'} {s['date']}" for s in steps)
    acc.req(f"Tier phase-out for a {size} fleet" + (f" on the optional {_n(opt_max)} hp schedule" if opt else ""),
            f"{sched}. Retire, sell, repower, replace or designate permanent low-use by December 31 of the year before "
            "each date. Not computed here: the fleet average / BACT requirements of 13 CCR 2449.1(a)-(b).",
            ORD_PH[size], ORD_SPECIAL, *([ORD_PH_500] if opt else []))
    acc.notes.append("The fleet average / BACT turnover requirements (13 CCR 2449.1(a)-(b)) are not in the rulebook "
                     "and are not computed; check them in DOORS or with CARB.")
    if machines:
        res.vehicles = [_vehicle_plan(v, steps, today, acc) for v in machines]
        for v in res.vehicles:
            acc.use(*v.rule_ids)
        if opt:
            res.optional_schedule_check = _optional_check(res.vehicles, today, acc)
    if hp <= opt_max and size == "small" and not opt:
        table = P(ORD_OPT_500, "min_percent_hp_tier2_or_higher")
        acc.req(f"Optional schedule for fleets of {_n(opt_max)} hp or less",
                f"At {_n(hp)} hp (<= {_n(opt_max)} hp) you may instead follow 13 CCR 2449(e)(16) Table 1: "
                + ", ".join(f"{_n(p)}% of hp Tier 2 or better by {d}" for d, p in sorted(table.items()))
                + f", with Tier 2 phased out from {P(ORD_PH_500, 'date')} instead of the small fleet date. Every "
                  "vehicle counts. Pass uses_optional_500hp_schedule=true to check it.", ORD_OPT_500, ORD_PH_500)

    # growth (2449(d)(3))
    acc.use(ORD_GROW)
    if size == "small" and not (captive or owner in _OWNER_RULE_TEXT):
        room = P(ORD_SIZE, "small_max_hp") - hp
        acc.math.append(f"Room before medium: {_n(P(ORD_SIZE, 'small_max_hp'))} - {_n(hp)} = {_n(room)} hp")
        acc.req("If the fleet grows", f"Adding more than {_n(room)} hp makes this a medium fleet; it then has to meet "
                f"the medium or large requirements on the compliance date {P(ORD_GROW, 'grow_small_to_medium_or_large_years')} "
                "years after the year it grew.", ORD_GROW, ORD_SIZE)
    elif size == "medium" and owner not in _OWNER_RULE_TEXT:
        room = P(ORD_SIZE, "large_min_hp_exclusive") - hp
        acc.math.append(f"Room before large: {_n(P(ORD_SIZE, 'large_min_hp_exclusive'))} - {_n(hp)} = {_n(room)} hp")
        acc.req("If the fleet grows", f"Going over {_n(P(ORD_SIZE, 'large_min_hp_exclusive'))} hp (more than "
                f"{_n(room)} hp added) makes this a large fleet, with {P(ORD_GROW, 'grow_medium_to_large_years')} "
                "years to meet the large fleet requirements.", ORD_GROW, ORD_SIZE)

    # idling (2449(d)(2))
    mins = P(ORD_IDLE, "max_consecutive_idle_minutes")
    acc.req("Idling limit", f"No machine may idle more than {mins} consecutive minutes. Exceptions: "
            + _join(P(ORD_IDLE, "exceptions")) + ". Waiting in line before the workday or site opens is not "
            "'queuing', so the limit applies.", ORD_IDLE, ORD_QUEUE)
    if size in P(ORD_IDLE_POLICY, "applies_to_fleet_sizes"):
        acc.req("Written idling policy", f"As a {size} fleet you need a written idling policy, available to every "
                f"operator, stating idling is limited to {mins} consecutive minutes or less (required since "
                f"{P(ORD_IDLE_POLICY, 'required_since')}).", ORD_IDLE_POLICY)

    # reporting (2449(g))
    acc.use(ORD_ANNUAL, ORD_ROAR, ORD_RD_AFF)
    due = _next_md(_pmd(P(ORD_ANNUAL, "annual_due")), today)
    first, last = P(ORD_ANNUAL, "reporting_years")[size]
    snap_md = _pmd(P(ORD_ANNUAL, "snapshot_date"))
    if due.year <= last:
        snap = date(due.year - 1, *snap_md)
        res.next_annual_report_due, res.report_vehicles_as_of = due.isoformat(), snap.isoformat()
        acc.req("Annual DOORS report", f"Update every vehicle in CARB's DOORS by {due}, as it was on {snap} ({size} "
                f"fleets report every year {first}-{last}).", ORD_ANNUAL, due=due)
        roar = ("Sign the Responsible Official Affirmation of Reporting (ROAR) with the report: a responsible official, "
                "or a designated official with a signed designation letter the first time, affirms the report is "
                "accurate and the fleet complies")
        if captive:
            roar += "; as a captive attainment area fleet, also certify no vehicle operated outside the listed counties"
        acc.req("ROAR affirmation", roar + ".", ORD_ROAR, due=due)
        acc.req("Renewable diesel affirmation", f"Also by {due}: an affirmation signed by the responsible or "
                "designated official that the fleet met the R99/R100 renewable diesel requirement.", ORD_RD_AFF,
                due=due)
    else:
        acc.notes.append(f"Annual fleet reporting for {size} fleets runs {first}-{last}"
                         + ("; permanent low-use vehicles keep being reported every March 1 for as long as the fleet "
                            "owns or operates them." if P(ORD_ANNUAL, "low_use_vehicles_report_while_owned") else "."))
    if is_new:
        new_due = (added_on + timedelta(days=P(ORD_NEW_FLEET, "report_within_days"))) if added_on else None
        acc.req("Report the new fleet", f"Report every vehicle in DOORS within {P(ORD_NEW_FLEET, 'report_within_days')}"
                " days of buying it or bringing it into California. These apply immediately: "
                + _join(P(ORD_NEW_FLEET, "immediately_applicable")) + ".", ORD_NEW_FLEET, due=new_due)
        if new_due:
            acc.math.append(f"New fleet report: {added_on} + {P(ORD_NEW_FLEET, 'report_within_days')} days = {new_due}")
    ein_days = P(ORD_EIN_APPLY, "apply_within_days")
    ein_due = added_on + timedelta(days=ein_days) if added_on else None
    if ein_due:
        acc.math.append(f"EIN application: {added_on} + {ein_days} days = {ein_due}")
    acc.req("EIN for each added machine", f"Apply to CARB for an Equipment Identification Number (or report the "
            f"purchase if it already has one) within {ein_days} days of buying it or bringing it into California.",
            ORD_EIN_APPLY, due=ein_due)
    spec = P(ORD_EIN_SPEC, "background")
    color = spec["captive_attainment_area_fleet"] if captive else spec["default"]
    acc.req("EIN label", f"Within {P(ORD_EIN_LABEL, 'affix_within_days')} days of receiving the EIN, paint or affix it "
            f"{P(ORD_EIN_SPEC, 'text_color')} on {color} on both sides about {P(ORD_EIN_SPEC, 'height_above_ground_ft')}"
            f" ft above the ground, characters at least {_n(P(ORD_EIN_SPEC, 'min_char_height_in'))} in tall and "
            f"{_n(P(ORD_EIN_SPEC, 'min_char_width_in'))} in wide. CARB does not supply labels.",
            ORD_EIN_LABEL, ORD_EIN_SPEC)
    acc.rule(ORD_EMIS_LABEL, "Unreadable engine emission label")
    lows = [v for v in res.vehicles if v.designation == "permanent_low_use"]
    if lows:
        acc.req("Low-use hour meter readings", f"For {_join([v.name for v in lows])}: report the hour meter reading "
                f"taken within {P(ORD_LOW_USE_METER, 'reading_window_days')} days of December 31 (January 1 the first "
                "year) with the annual report, and keep a date-stamped photo of the meter.", ORD_LOW_USE_METER,
                ORD_LOW_USE, ORD_ANNUAL, due=due)

    # renewable diesel (2449.1(f))
    acc.use(ORD_R99, ORD_RD_EXEMPT)
    fuel = _join(P(ORD_R99, "required_fuel"), "or")
    exempt_fleets = P(ORD_RD_EXEMPT, "exempt_fleets")
    clean_my = int(re.search(r"MY(\d{4})", next(e for e in exempt_fleets if "Tier 4 final" in e)).group(1))
    subject_rows = [v for v in res.vehicles if v.subject]
    all_clean = bool(subject_rows) and all(v.engine == "Tier 4 final" or (v.engine.startswith("on-road MY") and
                                                                          int(v.engine[10:]) >= clean_my)
                                           for v in subject_rows)
    listed = list(P(ORD_CAPTIVE, "counties")) + [str(P(ORD_CAPTIVE, "partial_county"))]
    if captive:
        res.renewable_diesel_required = False
        acc.req("Renewable diesel", f"Exempt from the {fuel} requirement as a captive attainment area fleet.",
                ORD_RD_EXEMPT, ORD_CAPTIVE)
    elif in_listed_counties:
        # 2449.1(f)(2)(A): any vehicle is exempt while it operates in a 2449(c)(6) county, even in a fleet that is not
        # captive (e.g. a State of California or federal fleet).
        res.renewable_diesel_required = False
        acc.req("Renewable diesel", f"Not required while the machines operate in the 2449(c)(6) counties (every county "
                f"you gave is one), even though the fleet is not a captive attainment area fleet. Once a machine works "
                f"in any other county it must use {fuel} there.", ORD_RD_EXEMPT, ORD_CAPTIVE, ORD_R99)
    elif all_clean and hp_given is None:
        res.renewable_diesel_required = False
        acc.req("Renewable diesel", f"Exempt from the {fuel} requirement: every listed machine is Tier 4 final or a "
                f"MY{clean_my}+ on-road engine.", ORD_RD_EXEMPT)
    else:
        res.renewable_diesel_required = True
        months = [calendar.month_abbr[m] for m in P(ORD_RD_EXEMPT, "cold_season_months")]
        wm, wd = _pmd(P(ORD_RD_EXEMPT, "winter_fuel_procure_from"))
        acc.req("Renewable diesel (R99/R100)", f"Since {P(ORD_R99, 'from')}, buy {fuel} renewable diesel and use it in "
                "every covered machine in California, rented ones included. Exemptions: captive attainment area "
                "fleets, and any machine while it operates in one of the 2449(c)(6) counties ("
                + _join(listed) + f"); fleets made up only of Tier 4 final, MY{clean_my}+ on-road or zero-emission "
                "machines; places where "
                f"the January 10th-percentile low is below {P(ORD_RD_EXEMPT, 'cold_temp_threshold_f')} F (only "
                f"{_join(months)}; winter fuel may be bought from {calendar.month_name[wm]} {wd}); and days below "
                f"{P(ORD_RD_EXEMPT, 'cold_temp_threshold_f')} F, or forecast within "
                f"{P(ORD_RD_EXEMPT, 'forecast_lead_days_max')} days.", ORD_R99, ORD_RD_EXEMPT)
        rm, rd = _pmd(P(ORD_RD_COLD, "seasonal_exemption_report_due"))
        acc.req("Cold-weather exemption reports", f"If you use the seasonal cold exemption, report location, January "
                f"low and fuel volumes by {calendar.month_name[rm]} {rd}; for a cold snap, within "
                f"{P(ORD_RD_COLD, 'event_exemption_report_within_days')} days after it ends.", ORD_RD_COLD)
        acc.req("If R99/R100 is not available", "Other diesel is allowed only with records: your normal fueling "
                f"method, your attempts to get R99/R100 ({P(ORD_RD_UNAVAIL, 'retry_frequency')}), and proof it was not "
                "available. Preferring a supplier or brand does not count.", ORD_RD_UNAVAIL)
    acc.req("Fuel records", f"Keep fuel receipts, purchase orders or contracts showing the date and volume of renewable "
            f"diesel bought for {P(ORD_RD_RECORDS, 'retention_calendar_years')} calendar years.", ORD_RD_RECORDS)

    # rentals, records, selling
    acc.req("Rented machines", f"Machines rented or leased for less than {P(ORD_RENTAL_FLEET, 'short_term_rental_less_than_years')}"
            " year stay in the rental company's fleet (a written lease of a year or more can move them into yours). "
            "The renter is responsible for idling and must use renewable diesel in them.", ORD_RENTAL_FLEET,
            ORD_RENTAL_IDLE, ORD_R99)
    if rents_out:
        acc.req("Your rental contracts", "Each rental agreement must say the renter is responsible for the idling "
                "limit and must comply with the renewable diesel requirement; give CARB the renter's name and contact "
                f"information within {P(ORD_RENTAL_RD, 'disclose_renter_within_days')} days of a request.",
                ORD_RENTAL_IDLE, ORD_RENTAL_RD)
    retain = _pdate(P(ORD_RETAIN, "retain_until"))
    acc.req("Records", f"Keep copies of everything reported plus the required records at a location in California, "
            f"give them to CARB within {P(ORD_RECORDS, 'produce_within_business_days')} business days of a request, "
            f"and keep them while you have the fleet or until {retain}, whichever is earlier.", ORD_RECORDS,
            ORD_RETAIN)
    acc.req("When you sell a machine", f"Tell CARB within {P(ORD_SOLD, 'notify_within_days')} days of the sale and put "
            f"this sentence on the bill of sale (keep proof {P(ORD_SALE_DISC, 'record_retention_years')} years): \""
            f"{P(ORD_SALE_DISC, 'text')}\"", ORD_SOLD, ORD_SALE_DISC)
    acc.req("Certificate of Reported Compliance", "After your report and ROAR show compliance, download the "
            "Certificate from your DOORS fleet profile and send it to every prime contractor and public agency you "
            f"work for: they may not contract with you without it. Public lookup: {P(ORD_CRC_ISSUE, 'public_lookup')}",
            ORD_CRC_ISSUE, ORD_CRC_BEFORE)
    zeta = P(ORD_ZETA, "min_zev_share_of_total_hp")
    acc.notes.append("Alternative: a Zero-Emission Technology Application (ZETA) replaces the phase-out and fleet "
                     "average rules if you commit to " + _join(f"{_n(f * 100)}% of total hp zero-emission by {d}"
                                                               for d, f in sorted(zeta.items())) + ".")
    acc.use(ORD_ZETA)
    enf = core.rule(ORD_ENFORCE)
    acc.use(ORD_ENFORCE, ORD_PENALTIES)
    acc.notes.append(f"Enforcement: EPA authorized the 2022 amendments on {enf.param('epa_authorization_date')}; CARB "
                     f"enforced the adding-vehicle limits from {enf.param('adding_vehicle_enforcement_from')} and said "
                     "it would emphasize education for the phase-out, low-use and contracting rules until at least "
                     f"{enf.param('education_discretion_until_at_least')}. Plan on full enforcement now.")
    acc.notes.append("Penalties: civil or criminal under "
                     + ", ".join(P(ORD_PENALTIES, "statutes")) + " (dollar amounts not in the rulebook).")

    banned = [v for v in res.vehicles if v.status == "may_not_operate"]
    soon = sorted((v for v in res.vehicles if v.status == "phase_out_scheduled"), key=lambda v: v.may_not_operate_from)
    res.summary = (f"{size.capitalize()} fleet ({why})" + (f", on the optional {_n(opt_max)} hp schedule" if opt else "")
                   + f". May add: {res.may_add_now or 'see adding_changes'}. "
                   + (f"{len(banned)} machine(s) may not operate now. " if banned else "")
                   + (f"Next phase-out: {soon[0].name} on {soon[0].may_not_operate_from}. " if soon else "")
                   + (f"Annual DOORS report due {res.next_annual_report_due}." if res.next_annual_report_due else ""))
    chk = res.optional_schedule_check
    if banned:
        res.next_step = (f"Stop operating {_join(v.name for v in banned)} in California now (phase-out date passed); "
                         "retire, sell, repower or replace, and update DOORS.")
    elif chk and chk.meets_current_requirement is False:
        short = chk.required_percent / 100 * chk.counted_hp - chk.tier2_or_better_hp
        res.next_step = (f"Optional schedule not met: {chk.percent_tier2_or_better}% of hp is Tier 2 or better, "
                         f"{_n(chk.required_percent)}% has been required since {chk.required_since}. Replace or "
                         f"repower at least {_n(short)} hp of older engines with Tier 2 or better, or switch to the "
                         "small fleet requirements.")
    elif soon:
        s = soon[0]
        res.next_step = (f"Plan {s.name}: retire, replace, repower or designate it permanent low-use by "
                         f"{s.designate_by} (it may not operate from {s.may_not_operate_from}). Then file the annual "
                         f"DOORS report and ROAR by {res.next_annual_report_due}.")
    elif res.next_annual_report_due:
        res.next_step = (f"File the annual DOORS report, ROAR and renewable diesel affirmation by "
                         f"{res.next_annual_report_due} (vehicles as of {res.report_vehicles_as_of}), then send your "
                         "new Certificate of Reported Compliance to your prime contractors.")
    else:
        res.next_step = "Keep the fleet records and follow the idling and renewable diesel rules."
    return acc.finish(res)


# ================================================================ contracting (13 CCR 2449(i)-(j)) and hired trucks
ContractRole = Literal["prime_contractor", "public_works_awarding_body", "subcontractor", "private_owner"]
_ROLE_ALIASES = {
    "prime_contractor": {"prime", "prime contractor", "gc", "general contractor", "general", "prime gc",
                         "design builder", "design build", "cmar", "cm at risk"},
    "public_works_awarding_body": {"awarding body", "public works awarding body", "public agency", "agency",
                                   "awarding authority", "public owner", "city", "county", "school district",
                                   "water district", "state agency", "school board"},
    "subcontractor": {"sub", "subcontractor", "sub contractor", "trade contractor", "specialty contractor",
                      "equipment fleet", "fleet", "lower tier sub"},
    "private_owner": {"owner", "private owner", "project owner", "developer", "property owner", "owner builder",
                      "landowner", "private developer", "homeowner"},
}


def _role(value) -> str:
    w = _words(value or "prime_contractor")
    if w.replace(" ", "_") in _ROLE_ALIASES:
        return w.replace(" ", "_")
    for key, names in _ROLE_ALIASES.items():
        if w in names:
            return key
    raise ValueError("role must be prime_contractor (you hold the contract with the owner or awarding authority), "
                     "public_works_awarding_body, subcontractor or private_owner (a private project owner or "
                     f"developer); got {value!r}")


class ContractingResult(_Base):
    role: ContractRole
    duties_apply: bool = Field(description="True when the 2449(i)-(j) contracting duties apply to you on this project")
    certificates_needed_before_contract: bool = False
    certificates_due: Optional[str] = None
    annual_certificate_window_start: Optional[str] = None
    annual_certificate_window_end: Optional[str] = None
    sign_required: Optional[bool] = None
    sign_post_by: Optional[str] = None
    noncompliance_report_due: Optional[str] = None
    keep_certificates_until: Optional[str] = None


def _truck_hiring(acc: _Acc) -> None:
    acc.req("Hired diesel trucks: Truck and Bus", "Check that every truck over "
            f"{_n(P(TB_APPLIES, 'min_gvwr_lbs_exclusive'))} lb GVWR you hire or dispatch complies: keep "
            f"a {P(TB_HIRE, 'proof')}, and keep {P(TB_HIRE, 'records')}. Give records to CARB within "
            f"{P(TB_RECORDS, 'produce_within_business_days')} business days of a request.", TB_HIRE, TB_RECORDS,
            TB_APPLIES)
    acc.req("Hired heavy trucks: Clean Truck Check", "Contract only with compliant trucks or fleets and keep, for each "
            f"truck, {_join(P(CTC_HIRE, 'proof_options'), 'or')} (dated within "
            f"{P(CTC_HIRE, 'affirmation_max_age_months')} months). Keep the records {P(CTC_HIRE, 'retention_years')} "
            f"years and produce them within {P(CTC_HIRE, 'produce_within_hours')} hours.", CTC_HIRE, CTC_RECORDS,
            CTC_APPLIES)


def offroad_contracting_requirements(role="prime_contractor", offroad_vehicles_on_project=True,
                                     homeowner_personal_residence=False, emergency_operations=False,
                                     first_vehicle_operates_on=None, expected_operating_days=None, contract_date=None,
                                     project_completion_date=None, noncompliance_discovered_on=None,
                                     hires_haul_trucks=False, as_of=None) -> ContractingResult:
    """Prime contractor and public works awarding body duties under 13 CCR 2449(i)-(j), plus hired-truck checks."""
    today = _as_of(as_of)
    who = _role(role)
    on_project = _bool("offroad_vehicles_on_project", offroad_vehicles_on_project)
    homeowner = bool(_bool("homeowner_personal_residence", homeowner_personal_residence))
    emergency = bool(_bool("emergency_operations", emergency_operations))
    trucks = bool(_bool("hires_haul_trucks", hires_haul_trucks))
    start = _date(first_vehicle_operates_on, "first_vehicle_operates_on")
    contract = _date(contract_date, "contract_date")
    done = _date(project_completion_date, "project_completion_date")
    found = _date(noncompliance_discovered_on, "noncompliance_discovered_on")
    days = _whole("expected_operating_days", expected_operating_days, 0)
    if start and done and done < start:
        raise ValueError(f"project_completion_date ({done}) is before first_vehicle_operates_on ({start})")
    acc = _Acc()
    acc.use(ORD_PRIME_DEF, ORD_AWARD_DEF)
    res = ContractingResult(role=who, duties_apply=False)
    from_ = _pdate(P(ORD_CRC_BEFORE, "from"))

    if who == "private_owner":
        acc.use(ORD_PRIME_DEF, ORD_AWARD_DEF)
        acc.notes.append("A private project owner or developer is neither a prime contractor (the entity holding the "
                         "contract with the owner) nor a public works awarding body (a public agency), so 13 CCR "
                         "2449(i)-(j) puts no certificate, sign or reporting duty on you. Your prime contractor carries "
                         "them; a fleet you hire directly is itself the prime contractor for that work.")
        if trucks:
            _truck_hiring(acc)
        res.summary = "Private owner or developer: no 13 CCR 2449(i)-(j) contracting duties; your prime contractor has them."
        res.next_step = ("Put the prime contractor's 2449(i)-(j) duties (certificates, jobsite sign, reporting) in its "
                         "contract, and ask for its own Certificate of Reported Compliance if it brings equipment.")
        return acc.finish(res)

    if who == "subcontractor":
        acc.req("Your Certificate of Reported Compliance", "Before a prime contractor or public agency signs or renews "
                "a contract with you, it must have your fleet's valid Certificate (and those of your listed "
                "subcontractors). Download it from your DOORS profile and send it with your bid; renew it every year "
                "with your March 1 report.", ORD_CRC_BEFORE, ORD_CRC_ISSUE, ORD_PRIME_ANNUAL)
        acc.notes.append("A subcontractor is anyone whose contract is with the prime or another sub, not with the "
                         "owner. The prime-contractor duties (jobsite sign, collecting certificates, reporting) are the "
                         "prime's. Your own fleet duties: use offroad_fleet_obligations.")
        if trucks:
            _truck_hiring(acc)
        res.summary = "Subcontractor: no prime-contractor duties, but no certificate, no contract."
        res.next_step = ("Download your current Certificate of Reported Compliance from DOORS and send it to the prime "
                         "before the contract is signed.")
        acc.use(ORD_ENFORCE)
        return acc.finish(res)

    if on_project is False:
        res.summary = (f"No off-road diesel vehicles of {_n(P(ORD_APPLIES, 'min_engine_max_hp'))} hp or more on the "
                       "project: the 13 CCR 2449(i)-(j) "
                       "contracting duties do not apply.")
        res.next_step = "Nothing to collect for the off-road rule. Re-check if equipment is added to the scope."
        acc.use(ORD_APPLIES)
        if trucks:
            _truck_hiring(acc)
            res.next_step += " Hired haul trucks still need the Truck and Bus and Clean Truck Check checks below."
        return acc.finish(res)

    if who == "prime_contractor" and homeowner:
        acc.req("Homeowner exclusion", core.rule(ORD_PRIME_DEF).plain_english or "", ORD_PRIME_DEF)
        res.summary = ("Contract with a homeowner for their own home (not used for business): you are not a 'prime "
                       "contractor' under 13 CCR 2449, so the certificate, sign and reporting duties do not apply.")
        res.next_step = ("No certificates or CARB sign needed for this job. Your own equipment still follows the fleet "
                         "rules (offroad_fleet_obligations).")
        if trucks:
            _truck_hiring(acc)
        return acc.finish(res)

    res.duties_apply = True
    acc.use(ORD_ENFORCE, ORD_PENALTIES)
    if emergency:
        recs = P(ORD_CRC_EMERGENCY, "records")
        acc.req("Emergency project records", "Emergency operations are exempt from collecting and keeping "
                f"certificates, but keep records of {_join(recs)}. Routine maintenance or construction is not an "
                "emergency.", ORD_CRC_EMERGENCY)
    else:
        res.certificates_needed_before_contract = True
        res.certificates_due = _iso(contract)
        detail = ("Get a copy of the valid Certificate of Reported Compliance for each fleet you hire and for its "
                  f"listed subcontractors before you sign or renew the contract (required since {from_}). No valid "
                  f"certificate, no contract. Check it at {P(ORD_CRC_ISSUE, 'public_lookup')}.")
        if contract and contract < today:
            detail += f" The contract date you gave ({contract}) has passed: the certificate had to be in hand by then."
        acc.req("Certificates before contracting", detail, ORD_CRC_BEFORE, ORD_CRC_ISSUE, due=contract)
        yrs = P(ORD_CRC_RETAIN, "retain_years_after_project_completion")
        keep = _add_years(done, yrs) if done else None
        res.keep_certificates_until = _iso(keep)
        if keep:
            acc.math.append(f"Keep certificates: {done} + {yrs} years = {keep}")
        acc.req("Keep the certificates", f"Keep the certificates for {yrs} years after the project is completed and "
                f"give them to CARB within {P(ORD_CRC_RETAIN, 'produce_within_business_days')} business days of a "
                "request.", ORD_CRC_RETAIN, due=keep)

    if who == "prime_contractor":
        sm, sd = _pmd(P(ORD_PRIME_ANNUAL, "window_start"))
        em, ed = _pmd(P(ORD_PRIME_ANNUAL, "window_end"))
        end = date(today.year, em, ed)
        if today > end:
            end = date(today.year + 1, em, ed)
        begin = date(end.year, sm, sd)
        res.annual_certificate_window_start, res.annual_certificate_window_end = begin.isoformat(), end.isoformat()
        if not emergency:
            acc.req("Collect new certificates every year", f"Between {begin} and {end}, collect the new "
                    "certificates for the current year from every fleet with an ongoing contract with you on "
                    f"{begin}. Do not write contracts to dodge this.", ORD_PRIME_ANNUAL, due=end)
        acc.req("Only certified fleets on your sites", "Allow only fleets with a valid Certificate of Reported "
                "Compliance on your job sites.", ORD_PRIME_ONLY)
        n = P(ORD_PRIME_REPORT, "report_within_business_days")
        report_due = _business_days_after(found, n) if found else None
        res.noncompliance_report_due = _iso(report_due)
        if report_due:
            acc.math.append(f"Report due: {found} + {n} business days (Mon-Fri; holidays not skipped, so this is the "
                            f"earliest it can fall) = {report_due}")
        url, email = P(ORD_PRIME_REPORT, "report_to")
        alt = core.rule(ORD_ENFORCE).param("report_email_in_notice", None)
        acc.req("Report uncertified fleets and noncompliant machines", f"Within {n} business days of finding a fleet "
                f"without a valid certificate or a noncompliant machine on your site, report it on CARB's web form "
                f"{url} (or email {email}"
                + (f"; CARB's enforcement notice ({core.rule(ORD_ENFORCE).citation_text}) gives {alt}, so the web "
                   "form is the safer route" if alt and alt != email else "")
                + ") with: " + "; ".join(P(ORD_PRIME_REPORT, "required_fields")) + ". Keep a copy of what you sent.",
                ORD_PRIME_REPORT, ORD_ENFORCE, due=report_due)
        acc.req("Tell CARB who runs each machine", "When CARB asks, immediately give the name and contact information "
                "of the responsible party for every off-road diesel machine on the site.", ORD_PRIME_DISCLOSE)
        trig, exempt_max = P(ORD_PRIME_SIGN, "trigger_min_calendar_days"), P(ORD_PRIME_SIGN, "exempt_max_calendar_days")
        if days is not None:
            res.sign_required = days >= trig
        if res.sign_required is not False:
            post_by = start + timedelta(days=P(ORD_PRIME_SIGN, "post_by_calendar_day") - 1) if start else None
            res.sign_post_by = _iso(post_by)
            if post_by:
                acc.math.append(f"Sign: day 1 = {start} (first machine operates); calendar day "
                                f"{P(ORD_PRIME_SIGN, 'post_by_calendar_day')} = {post_by}")
            cond = "" if res.sign_required else f"If machines will operate {trig} calendar days or more: "
            loc = P(ORD_PRIME_SIGN, "location")
            acc.req("CARB off-road jobsite sign", f"{cond}post CARB's sign by calendar day "
                    f"{P(ORD_PRIME_SIGN, 'post_by_calendar_day')} counting the day the first machine operates as day "
                    "1 (the earlier reading of 2449(j)(5)): lettering larger "
                    f"than {P(ORD_PRIME_SIGN, 'min_font_pt_exclusive')}-point, posted {loc[0]} or in an {loc[1]}, "
                    f"{loc[2]}, with the sections: "
                    + "; ".join(P(ORD_PRIME_SIGN, "sections")) + ". The exact wording is set by 13 CCR 2449(j)(5).",
                    ORD_PRIME_SIGN, due=post_by)
        else:
            acc.req("CARB off-road jobsite sign", f"Not required: machines operate {days} calendar days, and projects of "
                    f"{exempt_max} calendar days or less are exempt.", ORD_PRIME_SIGN)
    else:
        acc.notes.append("As a public works awarding body you collect and keep certificates (13 CCR 2449(i)); the "
                         "annual collection, jobsite sign and reporting duties of 2449(j) are the prime contractor's.")
    if trucks:
        _truck_hiring(acc)
    enf = core.rule(ORD_ENFORCE)
    acc.notes.append(f"CARB said it would emphasize education for the contracting and prime contracting rules until at "
                     f"least {enf.param('education_discretion_until_at_least')}; plan on full enforcement now. "
                     "Penalties: Health and Safety Code (amounts not in the rulebook).")
    who_txt = "Prime contractor" if who == "prime_contractor" else "Public works awarding body"
    res.summary = (f"{who_txt}: " + ("emergency project, keep emergency records instead of certificates. " if emergency
                                     else "valid Certificates of Reported Compliance before every new or renewed "
                                          "contract with a fleet. ")
                   + (f"Jobsite sign by {res.sign_post_by}. " if res.sign_post_by else "")
                   + (f"Report due {res.noncompliance_report_due}. " if res.noncompliance_report_due else ""))
    if res.noncompliance_report_due:
        res.next_step = (f"Report the fleet or machine to CARB by {res.noncompliance_report_due} on the web form "
                         f"{P(ORD_PRIME_REPORT, 'report_to')[0]}, and keep a copy.")
    elif emergency:
        res.next_step = "Start the emergency file now: what happened, where, the dates, and each fleet's attestation."
    elif res.sign_post_by and _pdate(res.sign_post_by) >= today:
        res.next_step = (f"Collect every equipment fleet's certificate before signing its contract, and post CARB's "
                         f"sign by {res.sign_post_by}.")
    else:
        res.next_step = ("Collect every equipment fleet's certificate (and its listed subs') before signing; check each "
                         f"one at {P(ORD_CRC_ISSUE, 'public_lookup')}.")
    return acc.finish(res)


# ================================================================ heavy trucks
Fuel = Literal["diesel", "alternative_diesel", "dual_fuel", "natural_gas", "propane", "gasoline", "zero_emission"]
_FUEL_ALIASES = {
    "diesel": {"diesel", "d", "diesel fuel", "ulsd", "2 diesel", "no 2 diesel"},
    "alternative_diesel": {"alternative diesel", "renewable diesel", "rd", "r99", "r100", "biodiesel", "b20", "b5",
                           "hvo"},
    "dual_fuel": {"dual fuel", "dualfuel"},
    "natural_gas": {"natural gas", "cng", "lng", "rng", "gas natural"},
    "propane": {"propane", "lpg"},
    "gasoline": {"gasoline", "gas", "petrol", "unleaded"},
    "zero_emission": {"zero emission", "zev", "electric", "ev", "battery electric", "bev", "fuel cell", "hydrogen"},
}
# Fuel -> its name in the Truck and Bus fuels list (13 CCR 2025(b)).
_TB_FUEL = {"diesel": "diesel", "dual_fuel": "dual-fuel", "alternative_diesel": "alternative diesel"}
_DIESEL_FUELED = {"diesel", "alternative_diesel", "dual_fuel"}
VehicleUse = Literal["truck", "pickup", "motor_home", "agricultural", "emergency", "military_tactical", "snow_removal",
                     "two_engine_crane"]
# Vehicle use -> the start of its entry in the Clean Truck Check (2195(b)) and Truck and Bus (2025(c)) exclusion lists.
_USE_EXCLUSION = {"emergency": "authorized emergency vehicles", "military_tactical": "military tactical vehicles"}
_TB_USE_EXCLUSION = {"emergency": "authorized emergency vehicles", "military_tactical": "military tactical vehicles",
                     "snow_removal": "dedicated snow-removal vehicles",
                     "motor_home": "motor homes for non-commercial private use",
                     "two_engine_crane": "two-engine on-road vehicles subject to 13 CCR 2449"}
TruckOwner = Literal["private", "state_or_local_government", "federal"]


def _fuel(value) -> str:
    w = _words(value or "diesel")
    if w.replace(" ", "_") in _FUEL_ALIASES:
        return w.replace(" ", "_")
    for key, names in _FUEL_ALIASES.items():
        if w in names:
            return key
    raise ValueError(f"fuel must be one of {', '.join(_FUEL_ALIASES)}; got {value!r}")


def _vehicle_use(value) -> str:
    w = _words(value or "truck")
    if w in ("truck", "bus", "dump truck", "water truck", "mixer", "concrete mixer", "boom truck", "tractor",
             "haul truck", "work truck", "service truck", "flatbed", "semi", "tractor trailer", "transfer truck",
             "super 10", "lowboy", "vacuum truck", "concrete pump truck", "pump truck", ""):
        return "truck"
    if "pickup" in w or "pick up" in w:
        return "pickup"
    if "motor home" in w or "motorhome" in w or w == "rv":
        return "motor_home"
    if "snow" in w:
        return "snow_removal"
    if w in ("two engine crane", "2 engine crane", "two engine water well drilling rig", "two engine drill rig",
             "workover rig", "two engine vehicle", "two engine crane or rig"):
        return "two_engine_crane"
    if w in ("crane", "mobile crane", "truck crane", "drill rig", "water well drilling rig"):
        raise ValueError(f"{value!r}: say two_engine_crane if it has a separate auxiliary engine of 50 hp or more for "
                         "the crane or drill (13 CCR 2449 covers it instead of Truck and Bus), or truck if one engine "
                         "drives both the truck and the equipment")
    if "agricultur" in w or w in ("ag", "farm"):
        return "agricultural"
    if "emergency" in w or w in ("fire truck", "ambulance"):
        return "emergency"
    if "military" in w:
        return "military_tactical"
    raise ValueError("vehicle_use must be truck, pickup, motor_home, agricultural, emergency, military_tactical, "
                     f"snow_removal (dedicated snow-removal vehicle) or two_engine_crane (two-engine crane, drill rig or "
                     f"workover rig); got {value!r}")


def _truck_owner(value) -> str:
    w = _words(value or "private")
    if w in ("private", "business", "company", "contractor", "private fleet", "llc", "inc"):
        return "private"
    if w in ("state or local government", "state", "local government", "city", "county", "public agency",
             "state agency", "special district", "school district", "government"):
        return "state_or_local_government"
    if w in ("federal", "federal agency", "us government", "federal government"):
        return "federal"
    raise ValueError(f"owner_type must be private, state_or_local_government or federal; got {value!r}")


def _my_range(spec) -> tuple[Optional[int], Optional[int]]:
    """'1993 & older' / '2004 and older' -> (None, 1993); '2005-2007' -> (2005, 2007); '1996' -> (1996, 1996);
    '2007 or newer' -> (2007, None); 'All engines' -> (None, None)."""
    s = str(spec).lower()
    if "all" in s:
        return None, None
    yrs = [int(y) for y in re.findall(r"(?:19|20)\d\d", s)]
    if "older" in s:
        return None, yrs[0]
    if "newer" in s:
        return yrs[0], None
    if len(yrs) == 2:
        return yrs[0], yrs[1]
    return yrs[0], yrs[0]


def _in_range(my: int, rng) -> bool:
    lo, hi = rng
    return (lo is None or my >= lo) and (hi is None or my <= hi)


class CleanTruckDeadline(BaseModel):
    deadline: str
    earliest_test: str = Field(description="A passing test from this date (start of the early-test window) covers through the next "
                                           "deadline")


class TruckResult(_Base):
    gvwr_lbs: float
    fuel: Fuel
    truck_and_bus_applies: bool
    truck_and_bus_status: Literal["not_subject", "compliant_2010_engine", "compliant_low_use",
                                  "compliant_nox_exempt_area", "noncompliant", "unknown_engine_year"]
    original_replacement_deadline: Optional[str] = Field(None, description=(
        "When the phase-in table first required the newer engine for this engine year and weight class"))
    clean_truck_check_applies: bool
    tests_per_year: Optional[int] = None
    next_deadlines: list[CleanTruckDeadline] = Field(default_factory=list)
    dmv_compliance_window_start: Optional[str] = None
    idling_limit_applies: bool
    advanced_clean_fleets_applies: bool


def _ctc_deadlines(anchor_month: int, day: int, months_step: int, today: date, count: int = 2) -> list[date]:
    """Compliance deadlines (2195.1): on the compliance year's expiration day in every `months_step`-th month counted
    from the month the compliance year ends; the last day of the month when that day does not exist. Only the month
    and day matter, so the year of the date given is irrelevant. day=31 stands for 'the last day of the month'."""
    months = {(anchor_month - 1 + k * months_step) % 12 + 1 for k in range(12)}
    out = sorted(date(y, m, min(day, calendar.monthrange(y, m)[1]))
                 for y in range(today.year, today.year + 3) for m in months)
    return [d for d in out if d >= today][:count]


def _vin_digit(value) -> Optional[str]:
    """The last number of a VIN (2195.1 'Compliance year' (1)); a single digit is taken as that number."""
    if value is None or not str(value).strip():
        return None
    digits = re.findall(r"\d", str(value))
    if not digits:
        raise ValueError(f"vin must be the VIN (or just its last digit), got {value!r}")
    return digits[-1]


def truck_compliance(gvwr_lbs, engine_model_year=None, fuel="diesel", ca_registered=True, registration_expires=None,
                     annual_ca_miles=None, powers_stationary_equipment=False, annual_pto_hours=None,
                     operating_counties=None, has_pm_filter=None, obd_equipped=None, vehicle_use="truck",
                     owner_type="private", vehicle_model_year=None, acquired_on=None, new_vehicle=False,
                     leased=False, selling=False, vin=None, as_of=None) -> TruckResult:
    """Truck and Bus, Clean Truck Check, idling and Advanced Clean Fleets for one on-road vehicle."""
    today = _as_of(as_of)
    gvwr = _num("gvwr_lbs", gvwr_lbs, 0, MAX_GVWR_LBS)
    if gvwr is None:
        raise ValueError("gvwr_lbs is required (the manufacturer's gross vehicle weight rating on the door sticker)")
    if gvwr == 0:
        raise ValueError("gvwr_lbs must be more than 0")
    emy = _year("engine_model_year", engine_model_year)
    vmy = _year("vehicle_model_year", vehicle_model_year)
    f = _fuel(fuel)
    use = _vehicle_use(vehicle_use)
    owner = _truck_owner(owner_type)
    ca_reg = _bool("ca_registered", ca_registered)
    ca_reg = True if ca_reg is None else ca_reg
    expires = _date(registration_expires, "registration_expires")
    if expires and expires > today + timedelta(days=MAX_REGISTRATION_AHEAD_DAYS):
        raise ValueError(f"registration_expires {expires} is more than a year away; give the expiration date on the "
                         "truck's current DMV registration card")
    miles = _num("annual_ca_miles", annual_ca_miles, 0, MAX_MILES)
    stationary = bool(_bool("powers_stationary_equipment", powers_stationary_equipment))
    pto = _num("annual_pto_hours", annual_pto_hours, 0, HOURS_PER_LEAP_YEAR)
    vin_digit = _vin_digit(vin)
    pm = _bool("has_pm_filter", has_pm_filter)
    obd = _bool("obd_equipped", obd_equipped)
    bought = _date(acquired_on, "acquired_on")
    is_new = bool(_bool("new_vehicle", new_vehicle))
    operating_counties = _names(operating_counties, "operating_counties")
    acc = _Acc()

    # applicability
    tb_min = P(TB_APPLIES, "min_gvwr_lbs_exclusive")
    tb_excl_entry = next((e for e in P(TB_APPLIES, "excluded") if use in _TB_USE_EXCLUSION
                          and e.startswith(_TB_USE_EXCLUSION[use])), None)
    tb_excluded = tb_excl_entry is not None
    tb = f in _TB_FUEL and _TB_FUEL[f] in P(TB_APPLIES, "fuels") and gvwr > tb_min and not tb_excluded
    ctc_min = P(CTC_APPLIES, "min_gvwr_lbs_exclusive")
    ctc_excluded = (_USE_EXCLUSION.get(use) in P(CTC_APPLIES, "excluded")
                    or (f == "zero_emission" and "zero-emission heavy-duty vehicles" in P(CTC_APPLIES, "excluded")))
    ctc = f != "gasoline" and gvwr > ctc_min and not ctc_excluded
    idle_min = P(IDLE_TRUCK, "min_gvwr_lbs_exclusive")
    # 2485(h)(6): a pickup truck (Vehicle Code 471) is not a 'commercial motor vehicle', so the limit does not reach it
    idle_pickup_out = use == "pickup" and any("pickup" in e for e in P(IDLE_TRUCK, "not_commercial_motor_vehicles"))
    idle = f in _DIESEL_FUELED and gvwr > idle_min and not idle_pickup_out
    acf_min = P(ACF, "min_gvwr_lbs_exclusive")
    acf = owner == "state_or_local_government" and gvwr > acf_min
    acc.use(TB_APPLIES, CTC_APPLIES, IDLE_TRUCK, ACF)
    acc.math.append(f"GVWR {_n(gvwr)} lb vs Truck and Bus / Clean Truck Check > {_n(tb_min)} lb, idling > "
                    f"{_n(idle_min)} lb, Advanced Clean Fleets > {_n(acf_min)} lb")
    res = TruckResult(gvwr_lbs=gvwr, fuel=f, truck_and_bus_applies=tb, truck_and_bus_status="not_subject",
                      clean_truck_check_applies=ctc, idling_limit_applies=idle, advanced_clean_fleets_applies=acf)

    # Truck and Bus (13 CCR 2025)
    if tb:
        acc.use(TB_2010, TB_LOW_USE, TB_NOX, TB_DMV, TB_RECORDS, TB_PENALTIES)
        need = P(TB_2010, "min_engine_model_year")
        eff = _pdate(P(TB_2010, "effective"))
        if today < eff:
            acc.notes.append(f"Before {eff} the Truck and Bus phase-in tables applied; this result applies the rule in "
                             f"force from {eff} (every engine {need} or newer).")
        light_key, heavy_key = "schedule_lighter_26000_or_less", "schedule_heavier_over_26000_2010_engine_by"
        split = int(re.search(r"(\d{5})", light_key).group(1))      # the weight boundary named in the table's key
        heavy = gvwr > split
        if emy is None:
            res.truck_and_bus_status = "unknown_engine_year"
            acc.req("Truck and Bus: engine model year needed", f"Since {eff}, diesel trucks over {_n(tb_min)} lb GVWR "
                    f"need a {need} or newer engine (low-use and NOx-exempt-area trucks excepted). Give "
                    "engine_model_year (on the engine's emission label) to check this truck.", TB_2010)
        elif emy >= need:
            res.truck_and_bus_status = "compliant_2010_engine"
            acc.req("Truck and Bus", f"Engine MY{emy} >= {need}: meets the Truck and Bus engine requirement.", TB_2010)
        else:
            if heavy:
                for spec, by in P(TB_2010, heavy_key).items():
                    if _in_range(emy, _my_range(spec)):
                        res.original_replacement_deadline = _pdate(by).isoformat()
            else:
                for by, spec in sorted(P(TB_2010, light_key).items()):
                    if _in_range(emy, _my_range(spec)) and not res.original_replacement_deadline:
                        res.original_replacement_deadline = _pdate(by).isoformat()
            low_mi = P(TB_LOW_USE, "max_ca_miles_per_year_exclusive")
            low_hr = P(TB_LOW_USE, "max_engine_or_pto_hours_exclusive")
            low_ok = miles is not None and miles < low_mi and (not stationary or (pto is not None and pto < low_hr))
            nox_counties = set(P(TB_NOX, "counties"))
            nox_only = None
            if operating_counties:
                names = []
                for c in operating_counties:
                    w = _words(c)
                    names.append("Northern Sonoma" if "sonoma" in w and "north" in w else _county(c, "operating_counties")[0])
                nox_only = all(n in nox_counties for n in names)
            nox_ok = bool(nox_only) and pm is True
            if miles is not None:
                acc.math.append(f"Low-use: {_n(miles)} California miles a year vs < {_n(low_mi)}"
                                + (f"; PTO {_n(pto)} h vs < {_n(low_hr)} h" if stationary and pto is not None else "")
                                + f" -> {'qualifies' if low_ok else 'does not qualify'}")
            report_md = _month_day_text(P(TB_LOW_USE, "report_by"))
            report_days = _days_in_text(P(TB_LOW_USE, "report_by"))
            if low_ok:
                res.truck_and_bus_status = "compliant_low_use"
                due = _next_md(report_md, today)
                acc.req("Truck and Bus low-use exemption", f"Engine MY{emy} is older than {need}, but at under "
                        f"{_n(low_mi)} California miles a year it may keep running if reported in "
                        f"{P(TB_LOW_USE, 'system')} with odometer (and hour meter) readings by "
                        f"{calendar.month_name[report_md[0]]} {report_md[1]} each year, or within {report_days} days "
                        "of purchase.", TB_LOW_USE, TB_2010, due=due)
            elif nox_ok:
                res.truck_and_bus_status = "compliant_nox_exempt_area"
                lw, lh = P(TB_NOX, "label_background_in")
                acc.req("Truck and Bus NOx exempt area", f"Engine MY{emy} with a PM filter, operating only in the NOx "
                        f"exempt counties: report it each January (or within {report_days} days of purchase) and mark "
                        f"'{P(TB_NOX, 'label_text')}' on both doors in {P(TB_NOX, 'label_letter_height_in')}-inch "
                        f"letters, {P(TB_NOX, 'label_color')} {lw} x {lh} inch background (unless GPS-tracked). It may "
                        f"leave those counties only for {P(TB_NOX, 'may_leave_area_only_for')}: a job outside them "
                        "makes it noncompliant.", TB_NOX, TB_2010)
            else:
                res.truck_and_bus_status = "noncompliant"
                why = f"engine MY{emy} < {need}"
                if res.original_replacement_deadline:
                    why += f"; its phase-in deadline was {res.original_replacement_deadline}"
                acc.req("Truck and Bus: NOT compliant", f"Since {eff} diesel trucks over {_n(tb_min)} lb GVWR need a "
                        f"{need} or newer engine ({why}). To keep running this engine as it is, only two exemptions "
                        f"remain: low-use (under {_n(low_mi)} "
                        f"California miles a year; under {_n(low_hr)} engine/PTO hours if it powers stationary "
                        "equipment such as a drill rig or concrete pump) or the NOx exempt area (PM filter, only "
                        f"{_join(sorted(nox_counties))}). Otherwise bring the engine to the 2010 standard: "
                        + _join(P(TB_2010, "compliance_paths"), "or") + ".", TB_2010, TB_LOW_USE, TB_NOX)
        sched = P(TB_DMV, ("schedule_heavier_over_26000_by_vehicle_model_year" if heavy else
                           "schedule_lighter_14001_26000_by_vehicle_model_year"))
        my_for_dmv = vmy if vmy is not None else emy
        dmv_from = None
        if my_for_dmv is not None:
            for spec, d in sched.items():
                if _in_range(my_for_dmv, _my_range(spec)):
                    dmv_from = d
        dmv = (f"Since {P(TB_DMV, 'dmv_verifies_compliance_from')} the DMV checks Truck and Bus compliance before it "
               "registers a diesel truck over the limit"
               + (f"; for model year {my_for_dmv} {'(vehicle)' if vmy else '(engine year used; give vehicle_model_year)'}"
                  f" since {dmv_from}" if dmv_from else "") + ". CARB can also put a registration hold on a "
               "noncompliant truck.")
        acc.req("DMV registration check", dmv, TB_DMV)
        acc.notes.append(f"The statute behind the DMV check ({P(TB_DMV, 'statute_unverified')}) was not read; the "
                         "check itself is from CARB's official FAQ.")
        if _bool("leased", leased):
            acc.req("Truck lease clause", f"A lease effective {P(TB_LEASE, 'applies_to_leases_effective_on_or_after')} "
                    "or later must say whether the truck is excluded from the lessor's fleet during the lease; "
                    f"otherwise compliance is the {P(TB_LEASE, 'default_responsibility')}'s.", TB_LEASE)
        if _bool("selling", selling):
            acc.req("Sale disclosure", f"Put CARB's disclosure on the {_join(P(TB_SALE, 'where'), 'or')}: \""
                    f"{P(TB_SALE, 'text')}\"", TB_SALE)
        acc.notes.append(f"Truck and Bus records go to CARB within {P(TB_RECORDS, 'produce_within_business_days')} "
                         "business days of a request. Penalties: civil or criminal under "
                         + ", ".join(P(TB_PENALTIES, "statutes")) + " (amounts not in the rulebook).")
    elif f in _TB_FUEL and gvwr <= tb_min:
        acc.notes.append(f"Truck and Bus does not apply: {_n(gvwr)} lb is not over {_n(tb_min)} lb GVWR.")
    elif tb_excluded and f in _TB_FUEL:
        msg = f"Truck and Bus does not apply: 13 CCR 2025(c) excludes {tb_excl_entry}."
        if use == "motor_home":
            msg += (" That exclusion is only for non-commercial private use: a motor home used for the business (a "
                    "jobsite office, crew quarters) is covered, so run it as vehicle_use='truck'.")
        elif use == "two_engine_crane":
            acc.use(ORD_APPLIES)
            msg += " Both engines fall under CARB's off-road fleet rule instead: use offroad_fleet_obligations."
        acc.notes.append(msg)
    muni = next((e for e in P(TB_APPLIES, "excluded") if e.startswith("vehicles owned or operated by a municipality")),
                None)
    if tb and owner == "state_or_local_government" and muni:
        acc.notes.append(f"Government trucks: 13 CCR 2025(c) also excludes {muni} (the public agency and utility fleet "
                         "rule). That rule is not in the rulebook; if your agency complies under it, the Truck and Bus "
                         "results here do not apply.")

    # Clean Truck Check (13 CCR 2195-2199)
    if ctc:
        acc.use(CTC_CONDITIONS, CTC_FREQ, CTC_START, CTC_FEE, CTC_TAMPER, CTC_RECORDS, CTC_PENALTIES)
        start = _pdate(P(CTC_START, "periodic_testing_effective"))
        q_year = P(CTC_FREQ, "quarterly_start_year_per_carb_fact_sheet")
        reg_q = _add_years(start, P(CTC_FREQ, "initial_period_years"))
        if obd is None and emy is not None:
            # 2195.1 'OBD-equipped vehicle': diesel engines MY2013+, alternative fuel engines MY2018+
            yrs = P(CTC_FREQ, "obd_equipped_from_model_year")
            kind = "diesel" if f in _DIESEL_FUELED else "alternative_fuel"
            obd = emy >= yrs[kind]
            acc.math.append(f"OBD-equipped: engine MY{emy} {'>=' if obd else '<'} MY{yrs[kind]} for "
                            f"{kind.replace('_', ' ')} engines (13 CCR 2195.1) -> {'yes' if obd else 'no'} "
                            "(pass obd_equipped to override)")
        if use in ("motor_home", "agricultural"):
            tests, step = P(CTC_FREQ, "tests_per_year_motorhome_agricultural"), 12
        elif obd and today.year >= q_year:
            tests, step = P(CTC_FREQ, "tests_per_year_obd_after_initial_period"), 3
        else:
            tests, step = P(CTC_FREQ, "tests_per_year_initial"), 6
        res.tests_per_year = tests if today >= start else None
        acc.math.append(f"Quarterly OBD testing: regulation = {start} + {P(CTC_FREQ, 'initial_period_years')} years = "
                        f"{reg_q}; CARB fact sheet says {q_year}")
        freq = (f"{tests} passing test(s) per compliance year" if today >= start
                else f"periodic testing starts {start}")
        if use not in ("motor_home", "agricultural") and step == 6:
            freq += (f"; from {q_year} (per CARB's fact sheet; the regulation says {P(CTC_FREQ, 'initial_period_years')}"
                     f" years after {start}) OBD-equipped trucks test "
                     f"{P(CTC_FREQ, 'tests_per_year_obd_after_initial_period')} times a year")
            if obd is None:
                freq += " (give obd_equipped or engine_model_year to know which applies)"
            elif obd is False:
                freq += ", but this truck is not OBD-equipped, so it stays at " \
                        f"{P(CTC_FREQ, 'tests_per_year_initial')}"
        acc.req("Clean Truck Check testing", freq + ". To be compliant (CARB confirms within "
                f"{P(CTC_CONDITIONS, 'deemed_compliant_within_hours')} hours): "
                + _join(P(CTC_CONDITIONS, "conditions")) + ".", CTC_FREQ, CTC_START, CTC_CONDITIONS)
        window = P(CTC_WINDOW, "early_test_window_days")
        dls, basis = [], None
        if today >= start and ca_reg and expires:
            dls = _ctc_deadlines(expires.month, expires.day, step, today)
            basis = f"the registration expiration day ({expires.strftime('%m-%d')})"
            if expires < today:
                acc.notes.append(f"registration_expires {expires} has already passed: renew the registration (the DMV "
                                 "needs Clean Truck Check compliance first). The dates below use the same month and "
                                 "day.")
        elif today >= start and vin_digit is not None and (not ca_reg or not expires):
            table = P(CTC_ALIGN, "non_ca_compliance_year_end_month_by_vin_last_digit")
            m = int(table[vin_digit])
            dls = _ctc_deadlines(m, 31, step, today)
            basis = (f"the last day of {calendar.month_name[m]} (VIN ends in {vin_digit}; the compliance year "
                     f"ends on the {P(CTC_ALIGN, 'non_ca_compliance_year_ends')})")
            if ca_reg:
                acc.notes.append("Deadlines were set from the VIN because no registration_expires was given. The VIN "
                                 f"schedule is for vehicles registered outside California and "
                                 f"{P(CTC_ALIGN, 'vin_basis_also_for')} (such as exempt-plate government trucks); a "
                                 "truck with a normal California registration uses its registration expiration date "
                                 "instead.")
        if dls:
            acc.use(CTC_ALIGN, CTC_WINDOW)
            res.next_deadlines = [CleanTruckDeadline(deadline=d.isoformat(),
                                                     earliest_test=(d - timedelta(days=window)).isoformat())
                                  for d in dls]
            acc.math.append(f"Deadlines fall on {basis} every {step} months -> next: "
                            + ", ".join(d.isoformat() for d in dls) + f"; test window opens {window} days before each")
            acc.req("Next Clean Truck Check deadlines", " ".join(
                f"Deadline {d}: a passing test from {d - timedelta(days=window)} counts through the following "
                "deadline (testing earlier only covers this one)." for d in dls), CTC_ALIGN, CTC_WINDOW, due=dls[0])
            if obd and step == 6 and any(d >= reg_q for d in dls):
                acc.notes.append(f"From {reg_q} ({P(CTC_FREQ, 'initial_period_years')} years after periodic testing "
                                 "began) the regulation moves "
                                 f"OBD-equipped trucks to quarterly deadlines; CARB's fact sheet says {q_year}. Check "
                                 "the deadlines CARB shows in your CTC-VIS account.")
        if today >= start and ca_reg and expires:
            acc.use(CTC_DMV)
            reg = expires if expires >= today else next(
                d for d in (date(y, expires.month, min(expires.day, calendar.monthrange(y, expires.month)[1]))
                            for y in range(today.year, today.year + 2)) if d >= today)
            dmv_days = P(CTC_DMV, "compliance_window_before_registration_days")
            res.dmv_compliance_window_start = (reg - timedelta(days=dmv_days)).isoformat()
            acc.req("Registration renewal", f"For the DMV to renew on {reg}, every compliance condition must be met "
                    f"within the {dmv_days} days before it (from {res.dmv_compliance_window_start}).", CTC_DMV,
                    due=reg)
        elif ca_reg and today >= start and not dls:
            acc.use(CTC_ALIGN)
            acc.notes.append("Give registration_expires (the DMV registration expiration date) to compute the Clean "
                             "Truck Check deadlines: they fall on that day in months "
                             + _join(str(m) for m in P(CTC_ALIGN, "semiannual_deadline_months"))
                             + " of each compliance year (" + _join(str(m) for m in P(CTC_ALIGN,
                                                                                          "quarterly_deadline_months"))
                             + " when quarterly).")
        if not ca_reg:
            acc.use(CTC_ALIGN, CTC_PASS)
            if not dls:
                acc.notes.append("Not registered in California: the deadlines are "
                                 f"{P(CTC_ALIGN, 'non_ca_registered')}. Give vin (or just its last digit) to compute "
                                 "them.")
            acc.req("Five-Day Pass", f"To bring a noncompliant truck into California for a short job, apply for a "
                    f"Five-Day Pass at least {P(CTC_PASS, 'apply_business_days_before')} business days before entry: "
                    f"up to {P(CTC_PASS, 'max_consecutive_days')} consecutive days, once per calendar year per "
                    "vehicle; keep it in the truck.", CTC_PASS)
        base = P(CTC_FEE, "base_fee_usd")
        acc.req("Clean Truck Check fee", f"Pay the annual compliance fee for each vehicle before its first compliance "
                f"of the compliance year: {_usd(base)} in the regulation, adjusted every year by the California CPI "
                f"(CARB publishes the new amount by July 1). The current amount is not in the rulebook; check CARB's "
                "Clean Truck Check site.", CTC_FEE)
        if bought:
            acc.use(CTC_TRANSFER)
            if is_new:
                d = bought + timedelta(days=P(CTC_TRANSFER, "new_vehicle_comply_within_days"))
                acc.req("New truck", f"Report it and pay within {P(CTC_TRANSFER, 'new_vehicle_comply_within_days')} days "
                        f"of purchase (by {d}); no test is needed.", CTC_TRANSFER, due=d)
            else:
                lb = P(CTC_TRANSFER, "used_vehicle_lookback_days")
                acc.req("Used truck purchase", f"At the transfer on {bought}, compliance must have been shown within "
                        f"the {lb} days before it (since {bought - timedelta(days=lb)}); ask the seller for the "
                        "compliance record.", CTC_TRANSFER, due=bought)
        acc.req("No tampering", "Do not operate it with " + _join(P(CTC_TAMPER, "prohibited"), "or") + ".", CTC_TAMPER)
        acc.notes.append(f"Clean Truck Check records: keep {P(CTC_RECORDS, 'retention_years_min')} years and produce "
                         f"within {P(CTC_RECORDS, 'produce_within_hours')} hours. Penalties: "
                         f"{P(CTC_PENALTIES, 'statutes')}; another party's fault does not reduce them.")
    elif f == "gasoline" and gvwr > ctc_min:
        acc.notes.append("Clean Truck Check covers non-gasoline heavy-duty vehicles; this gasoline truck is not in it.")

    # idling (13 CCR 2485)
    if idle:
        acc.use(IDLE_PTO, IDLE_RESTRICTED, IDLE_FINE, IDLE_CARRIER)
        acc.req("Idling", f"No idling over {P(IDLE_TRUCK, 'max_consecutive_idle_minutes')} consecutive minutes "
                "anywhere; a diesel auxiliary power unit may not run over "
                f"{P(IDLE_TRUCK, 'aps_limit_minutes_near_restricted_area')} minutes within "
                f"{P(IDLE_TRUCK, 'restricted_area_distance_ft')} ft of homes, schools, hotels, hospitals, senior or child "
                "care. Exceptions include power take-off work (" + _join(P(IDLE_PTO, "pto_work_exempt")) + "), and "
                f"queuing more than {P(IDLE_PTO, 'queuing_exempt_if_beyond_ft')} ft from those places. CARB says the "
                f"owner and the driver can each be fined up to {_usd(P(IDLE_FINE, 'max_fine_usd_per_day'))} a day "
                "(the regulation itself applies the Health and Safety Code penalties).", IDLE_TRUCK, IDLE_PTO,
                IDLE_RESTRICTED, IDLE_FINE)
        acc.req("Driver paperwork", "If you dispatch it, give the driver your " + _join(P(IDLE_CARRIER,
                                                                                         "carrier_must_provide"))
                + "; the driver shows them with the license and registration on request.", IDLE_CARRIER)

    if idle_pickup_out and f in _DIESEL_FUELED and gvwr > idle_min:
        acc.notes.append("The 5-minute truck idling rule (13 CCR 2485) does not apply: a pickup truck as defined in "
                         "Vehicle Code 471 is not a 'commercial motor vehicle' under 2485(h)(6). Local idling rules "
                         "may still apply.")

    # Advanced Clean Fleets
    if acf:
        acc.req("Advanced Clean Fleets", f"As a state or local government fleet, this vehicle (over {_n(acf_min)} lb) "
                f"is covered by Advanced Clean Fleets ({_join(P(ACF, 'sections'))}, effective {P(ACF, 'effective')}). "
                "Its zero-emission purchase schedule is not in the rulebook.", ACF)
    elif owner == "private":
        acc.notes.append(f"Advanced Clean Fleets (as amended, effective {P(ACF, 'effective')}) covers only state and "
                         "local government fleets; its text has no private-fleet requirements.")
    else:
        acc.notes.append("Advanced Clean Fleets does not apply to federal fleets.")
    acc.notes.append("If you hire other companies' trucks, use offroad_contracting_requirements(hires_haul_trucks=true).")

    tb_txt = {"not_subject": "Truck and Bus does not apply", "compliant_2010_engine":
              f"Truck and Bus OK ({P(TB_2010, 'min_engine_model_year')}+ engine)",
              "compliant_low_use": "Truck and Bus OK only as low-use", "compliant_nox_exempt_area":
              "Truck and Bus OK only in the NOx exempt area", "noncompliant": "Truck and Bus NOT compliant",
              "unknown_engine_year": "Truck and Bus: engine model year needed"}[res.truck_and_bus_status]
    res.summary = (f"{_n(gvwr)} lb {f.replace('_', ' ')}: {tb_txt}. "
                   + (f"Clean Truck Check: {res.tests_per_year} test(s) a year"
                      + (f", next deadline {res.next_deadlines[0].deadline}" if res.next_deadlines else "") + ". "
                      if ctc and res.tests_per_year else "Clean Truck Check applies. " if ctc else "")
                   + (f"{P(IDLE_TRUCK, 'max_consecutive_idle_minutes')}-minute idling limit." if idle else ""))
    if res.truck_and_bus_status == "noncompliant":
        res.next_step = ("Take it off California roads or bring it into compliance: replace or repower with a "
                         f"{P(TB_2010, 'min_engine_model_year')}+ engine, retrofit a VDECS verified to 2010 model year "
                         f"emissions, or qualify it as low-use and report it in {P(TB_LOW_USE, 'system')}. The DMV "
                         "will not register it as is.")
    elif res.truck_and_bus_status == "unknown_engine_year":
        res.next_step = "Read the engine model year off the emission control label and run this again."
    elif res.next_deadlines:
        d = res.next_deadlines[0]
        res.next_step = (f"Book the Clean Truck Check test between {d.earliest_test} and {d.deadline}, and make sure the "
                         "annual fee is paid and the vehicle is reported.")
    elif ctc and not ca_reg:
        res.next_step = ("Give the VIN (its last digit sets the Clean Truck Check deadlines of a truck registered outside "
                         "California) and schedule the tests around them.")
    elif ctc:
        res.next_step = "Find the DMV registration expiration date and schedule the Clean Truck Check tests around it."
    else:
        res.next_step = "No CARB heavy-duty truck program applies; follow the idling limit if it is diesel." \
            if not idle else f"Follow the {P(IDLE_TRUCK, 'max_consecutive_idle_minutes')}-minute idling limit."
    return acc.finish(res)


# ================================================================ jobsite dust (air districts)
SCAQMD, SJV, BAAQMD = "south_coast", "san_joaquin_valley", "bay_area"
District = Literal["south_coast", "san_joaquin_valley", "bay_area"]
_DISTRICT_NAMES = {SCAQMD: "South Coast AQMD", SJV: "San Joaquin Valley APCD", BAAQMD: "Bay Area AQMD"}
_DISTRICT_ALIASES = {
    SCAQMD: {"south coast", "south coast aqmd", "scaqmd", "sc aqmd", "aqmd", "south coast air quality management "
             "district", "south coast air district", "southcoast"},
    SJV: {"san joaquin valley", "sjv", "sjvapcd", "sjv apcd", "valley air", "valley air district",
          "san joaquin valley apcd", "san joaquin valley air pollution control district", "san joaquin valley air "
          "district", "sjvaqmd"},
    BAAQMD: {"bay area", "baaqmd", "bay area aqmd", "bay area air quality management district", "bay area air "
             "district", "sf bay area", "san francisco bay area", "bay area air district"},
}
_DISTRICT_TOKENS = {SCAQMD: ("scaqmd", "south coast"), SJV: ("sjvapcd", "san joaquin valley"),
                    BAAQMD: ("baaqmd", "bay area")}
_DISTRICT_SUFFIX = re.compile(r"\b(air pollution control district|air quality management district|air resources "
                              r"district|air district|apcd|aqmd)\b")
_COACHELLA = ("coachella",)
# Counties each district covers, as stated in its rules' applies_to text (whole counties, and partial counties with
# the part named in that text). San Joaquin Valley counties are read from rule air-sjv-district-counties instead.
_SCOPE_COUNTIES = {
    SCAQMD: (("Orange",), {"Los Angeles": "the non-desert part", "Riverside": "the non-desert part",
                           "San Bernardino": "the non-desert part"}),
    BAAQMD: (("Alameda", "Contra Costa", "Marin", "Napa", "San Francisco", "San Mateo", "Santa Clara"),
             {"Sonoma": "southern Sonoma", "Solano": "southwestern Solano"}),
}
_SCOPE_RULE = {SCAQMD: SC_VISIBLE, BAAQMD: BA_LARGE, SJV: SJV_COUNTIES}
ProjectType = Literal["residential", "non_residential"]


def _sjv_counties() -> tuple[list[str], str]:
    partial = str(P(SJV_COUNTIES, "partial"))
    return list(P(SJV_COUNTIES, "counties")), partial.split(" (")[0]


def _district(value, coachella: Optional[bool]) -> dict:
    """-> {key, name, basis, county, part, coachella, note}. key None = a California county whose district has no
    dust rules in the rulebook."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("air_district is required: 'South Coast', 'San Joaquin Valley', 'Bay Area', 'Coachella "
                         "Valley' or the county of the jobsite")
    w = _words(raw)
    out = {"key": None, "name": None, "basis": "district", "county": None, "part": None, "coachella": bool(coachella),
           "note": None}
    if any(t in w for t in _COACHELLA):
        out.update(key=SCAQMD, coachella=True)
    else:
        hits = [k for k, names in _DISTRICT_ALIASES.items() if w in names]
        hits = hits or [k for k, toks in _DISTRICT_TOKENS.items() if any(t in w for t in toks)]
        if len(set(hits)) > 1:
            raise ValueError(f"{raw!r} names more than one air district; give one")
        if hits:
            out["key"] = hits[0]
    if out["key"] is None:
        bare = re.sub(r"\s+", " ", _DISTRICT_SUFFIX.sub(" ", w)).strip()      # 'San Diego APCD' -> 'san diego'
        county, note = _county(raw if bare == w else bare, "air_district")
        if bare != w and note is None:
            note = f"{raw!r} was read as the air district of {county} County."
        out.update(county=county, note=note, basis="county")
        sjv_full, sjv_part = _sjv_counties()
        if county in sjv_full:
            out["key"] = SJV
        elif county == sjv_part:
            part = re.search(r"\((.+)\)", str(P(SJV_COUNTIES, "partial")))
            out.update(key=SJV, part=f"the {part.group(1)}" if part else "part", basis="county_partial")
        for key, (full, partial) in _SCOPE_COUNTIES.items():
            if county in full:
                out["key"] = key
            elif county in partial:
                out.update(key=key, part=partial[county], basis="county_partial")
    if out["key"]:
        out["name"] = _DISTRICT_NAMES[out["key"]]
    if out["coachella"] and out["key"] != SCAQMD:
        raise ValueError("coachella_valley applies only to the South Coast AQMD (Riverside County's Coachella Valley)")
    return out


def _acres(name: str, value) -> Optional[float]:
    """Acres from a number or text: 2.5, '2.5 acres', '2.5 ac', '8,000 sq ft', '8000 sf'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        s = value.lower().replace(",", "").strip()
        m = re.match(r"^(-?\d*\.?\d+)\s*(sq\.?\s*ft|sqft|sf|square\s*feet|ft2|ft\^2)$", s)
        if m:
            v = float(m.group(1))
            if v < 0:
                raise ValueError(f"{name} cannot be below 0, got {value!r}")
            if v > MAX_SQFT:
                raise ValueError(f"{name} cannot be above {MAX_SQFT:,} sq ft, got {value!r}")
            return v / SQFT_PER_ACRE
    return _num(name, value, 0, MAX_ACRES)


def _project_type(value) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    w = _words(value)
    if w in ("residential", "res", "housing", "homes", "single family", "multifamily", "multi family", "apartments",
             "subdivision", "residential development"):
        return "residential"
    if w in ("non residential", "nonresidential", "commercial", "industrial", "institutional", "public", "retail",
             "office", "warehouse", "school", "road", "public works", "non res", "infrastructure"):
        return "non_residential"
    raise ValueError(f"project_type must be residential or non_residential (commercial, industrial, institutional, "
                     f"public); got {value!r}")


class DustResult(_Base):
    air_district: Optional[District] = None
    district_name: Optional[str] = None
    district_basis: str = ""
    county: Optional[str] = None
    covered: bool = Field(description="True when the rulebook has this district's construction dust rules")
    coachella_valley: bool = False
    disturbed_acres: Optional[float] = None
    plan_required: Optional[bool] = Field(None, description=(
        "Dust control plan before earthmoving (SJV Rule 8021 Dust Control Plan, Coachella Valley Rule 403.1 plan)"))
    plan_submit_by: Optional[str] = None
    large_operation: Optional[bool] = Field(None, description="South Coast Rule 403 'large operation'")
    trackout_control_required: Optional[bool] = None
    fees_usd: Optional[float] = None


def dust_control_requirements(air_district, disturbed_acres=None, daily_import_export_cy=None,
                              daily_earthmoving_cy=None, earthmoving_days_at_that_volume=None, project_type=None,
                              earthmoving_start=None, coachella_valley=None, vehicle_trips_per_day=None,
                              three_axle_trips_per_day=None, urban_area=None, demolition=False,
                              near_sensitive_receptors=False, wind_gust_mph=None, average_wind_mph=None,
                              single_family_addition=False, existing_building_sqft=None, added_building_sqft=None,
                              site_acres=None, paved_interior_roads=None, site_elevation_ft=None,
                              as_of=None) -> DustResult:
    """Construction dust rules for one site: South Coast AQMD, San Joaquin Valley APCD or Bay Area AQMD."""
    today = _as_of(as_of)
    acres = _acres("disturbed_acres", disturbed_acres)
    site = _acres("site_acres", site_acres)
    if acres is not None and site is not None and site + 1e-9 < acres:
        raise ValueError(f"site_acres ({_n(site)}) cannot be smaller than disturbed_acres ({_n(acres)}): the disturbed "
                         "area is part of the site")
    paved_roads = _bool("paved_interior_roads", paved_interior_roads)
    elev = _num("site_elevation_ft", site_elevation_ft, -300, MAX_FEET)
    imp = _num("daily_import_export_cy", daily_import_export_cy, 0, MAX_CY_PER_DAY)
    moved = _num("daily_earthmoving_cy", daily_earthmoving_cy, 0, MAX_CY_PER_DAY)
    days = _whole("earthmoving_days_at_that_volume", earthmoving_days_at_that_volume, 0, 366)
    ptype = _project_type(project_type)
    start = _date(earthmoving_start, "earthmoving_start")
    trips = _whole("vehicle_trips_per_day", vehicle_trips_per_day, 0)
    axle3 = _whole("three_axle_trips_per_day", three_axle_trips_per_day, 0)
    urban = _bool("urban_area", urban_area)
    demo = bool(_bool("demolition", demolition))
    sensitive = bool(_bool("near_sensitive_receptors", near_sensitive_receptors))
    gust = _num("wind_gust_mph", wind_gust_mph, 0, 200)
    avg_wind = _num("average_wind_mph", average_wind_mph, 0, 200)
    sfa = bool(_bool("single_family_addition", single_family_addition))
    existing = _num("existing_building_sqft", existing_building_sqft, 0, MAX_SQFT)
    added = _num("added_building_sqft", added_building_sqft, 0, MAX_SQFT)
    if (existing is None) != (added is None):
        raise ValueError("give both existing_building_sqft and added_building_sqft (or neither)")
    d = _district(air_district, _bool("coachella_valley", coachella_valley))
    acc = _Acc()
    res = DustResult(air_district=d["key"], district_name=d["name"], district_basis=d["basis"], county=d["county"],
                     covered=d["key"] is not None, coachella_valley=d["coachella"],
                     disturbed_acres=None if acres is None else round(acres, 4))
    if d["note"]:
        acc.notes.append(d["note"])
    if d["key"] is None:
        res.summary = (f"{d['county']} County: the rulebook has verified construction dust rules only for "
                       f"{_join(_DISTRICT_NAMES.values())}. {d['county']} County's air district is not one of them.")
        res.next_step = (f"Get the construction dust (fugitive dust) rules from the air district for {d['county']} "
                         "County. CARB's equipment rules still apply statewide (offroad_fleet_obligations, "
                         "truck_compliance).")
        return acc.finish(res)
    if d["part"]:
        acc.use(_SCOPE_RULE[d["key"]])
        if d["key"] == SCAQMD and d["county"] == "Riverside":
            # Rule 403's stated scope: the non-desert part of Riverside County, plus the Coachella Valley (Rule 403.1)
            acc.use(CV_PLAN)
            if d["coachella"]:
                acc.notes.append("Riverside County, Coachella Valley: inside the South Coast AQMD (Rule 403.1 "
                                 "applies with Rule 403). Other desert parts of the county outside the Coachella "
                                 "Valley are not; confirm on the district's map.")
            else:
                acc.notes.append(f"Only {d['part']} of Riverside County and its Coachella Valley are in the "
                                 f"{d['name']}. These requirements assume the non-desert part; if the site is in the "
                                 "Coachella Valley, Rule 403.1 also applies: pass coachella_valley=true.")
        else:
            acc.notes.append(f"Only {d['part']} of {d['county']} County is in the {d['name']} (per the rule's stated "
                             "scope). These requirements assume the site is in that part; confirm on the district's "
                             "map.")
    volume = max(v for v in (moved, imp) if v is not None) if (moved is not None or imp is not None) else None
    if moved is not None and imp is not None and imp > moved:
        acc.math.append(f"Earth-moving volume taken as the larger of moved ({_n(moved)}) and imported/exported "
                        f"({_n(imp)}) = {_n(volume)} cy/day")

    if d["key"] == SCAQMD:
        _south_coast(acc, res, acres, imp, volume, days, start, gust, d["coachella"], today)
    elif d["key"] == SJV:
        _san_joaquin(acc, res, acres, volume, days, ptype, start, trips, axle3, urban, demo, sfa, existing, added,
                     today, site, paved_roads, elev)
    else:
        _bay_area(acc, res, acres, sensitive, avg_wind)
    return acc.finish(res)


def _south_coast(acc, res, acres, imp, volume, days, start, gust, coachella, today):
    acc.req("No visible dust leaving the site", f"Dust may not stay visible in the air past the property line, or "
            f"exceed {P(SC_VISIBLE, 'max_opacity_percent_vehicle_movement')}% opacity from vehicle movement.",
            SC_VISIBLE)
    acc.req("Rule 403 Table 1 best available control measures", "Use the Table 1 measure for each dust source on the "
            "site: " + _join(P(SC_BACM, "source_categories")) + ".", SC_BACM)
    acc.req("PM10 limit", f"If sampled, downwind PM10 may not exceed upwind by more than "
            f"{P(SC_PM10, 'max_pm10_upwind_downwind_difference_ug_m3')} ug/m3.", SC_PM10)
    acc.req("Track-out", f"Track-out may not reach {P(SC_TRACKOUT, 'max_trackout_cumulative_ft_exclusive')} ft "
            f"(cumulative) from the exit; remove all of it at the {P(SC_TRACKOUT, 'cleanup')}.", SC_TRACKOUT)
    acc.rule(SC_STOCKPILE, "Stockpiles and haul trucks")
    acc.rule(SC_CONTRACT_END, "When your Rule 403 duty ends")
    ex_ac, ex_cy = P(SC_EXIT, "trigger_disturbed_acres_min"), P(SC_EXIT, "trigger_daily_import_export_cy_min")
    if acres is not None or imp is not None:
        need = (acres is not None and acres >= ex_ac) or (imp is not None and imp >= ex_cy)
        res.trackout_control_required = need
        acc.math.append(f"Exit controls: {_given(acres, 'ac')} vs >= {_n(ex_ac)} ac; {_given(imp, 'cy/day')} "
                        f"imported/exported vs >= {_n(ex_cy)} cy -> {'required' if need else 'not required'}")
    if res.trackout_control_required is not False:
        o = P(SC_EXIT, "options")
        g, pv, ws = o["gravel_pad"], o["paving"], o["wheel_shaker"]
        cond = "" if res.trackout_control_required else (f"If {_n(ex_ac)}+ acres are disturbed or {_n(ex_cy)}+ cy a "
                                                         "day are imported or exported: ")
        acc.req("Track-out control at every exit", cond + "at each exit to a paved public road use at least one of: a "
                f"washed gravel pad ({_n(g['min_rock_size_in'])}-inch rock, {_n(g['min_depth_in'])} in deep, "
                f"{_n(g['min_width_ft'])} ft wide x {_n(g['min_length_ft'])} ft long), paving "
                f"{_n(pv['min_length_ft'])} ft long x {_n(pv['min_width_ft'])} ft wide, a wheel shaker "
                f"{_n(ws['min_length_ft'])} x {_n(ws['min_width_ft'])} ft, a wheel washer, or an approved equivalent.",
                SC_EXIT)
    big_ac, big_cy, big_days = (P(SC_LARGE, "disturbed_acres_min"), P(SC_LARGE, "daily_earthmoving_cy_min"),
                                P(SC_LARGE, "days_in_365_min"))
    by_area = acres is not None and acres >= big_ac
    by_vol = volume is not None and volume >= big_cy
    if by_area or (by_vol and days is not None and days >= big_days):
        res.large_operation = True
    elif by_vol and days is None:
        res.large_operation = None
    elif acres is not None or volume is not None:
        res.large_operation = False
        if volume is None:
            acc.notes.append("Large-operation status was checked on area only; give daily_earthmoving_cy to check the "
                             "daily volume test too.")
    acc.use(SC_LARGE)
    acc.math.append(f"Large operation: {_given(acres, 'ac')} vs >= {_n(big_ac)} ac; {_given(volume, 'cy/day')} vs >= "
                    f"{_n(big_cy)} cy ({_n(P(SC_LARGE, 'daily_earthmoving_m3_min'))} m3) on {big_days}+ days in 365"
                    + (f" (you gave {days} days)" if days is not None else "")
                    + " -> " + {True: "yes", False: "no", None: f"yes if that volume happens on {big_days} or more days"
                                if by_vol else "unknown"}[res.large_operation])
    if res.large_operation is not False and (acres is not None or volume is not None):
        note = ("Large-operation duties (Rule 403(e)) do not apply to "
                + _join(P(SC_LARGE, "subdivision_e_not_applicable_to"), "or") + " (Rule 403(g)(7)).")
        if P(SC_LARGE, "e1_A_to_C_not_required_with_approved_aqmd_dust_plan"):
            note += (" With an AQMD-approved fugitive dust control plan and no change to its sources and controls, "
                     "Form 403 N, the site description and the daily records of (e)(1)(A)-(C) are not required (Rule "
                     "403(g)(8)); the signs, supervisor and Table 2 measures still are.")
        acc.notes.append(note)
        cond = "" if res.large_operation else f"If {_n(big_cy)}+ cy/day happens on {big_days}+ days: "
        acc.req("Large Operation Notification (Form 403 N)", cond + f"file Form {P(SC_403N, 'form')} within "
                f"{P(SC_403N, 'file_within_days')} days of qualifying, with the responsible people's names, addresses "
                f"and phones, a description and a site map. It is valid {P(SC_RENEW, 'valid_years')} year: resubmit at "
                f"least {P(SC_RENEW, 'resubmit_days_before_expiration_min')} days before it expires "
                f"(Form {P(SC_RENEW, 'no_change_form')} if nothing changed), and tell SCAQMD within "
                f"{P(SC_LARGE_ENDS, 'notify_within_days')} days after the site stops qualifying.", SC_403N, SC_RENEW,
                SC_LARGE_ENDS)
        acc.req("Large operation: daily records", f"Record the dust controls used every day and keep the records "
                f"{P(SC_LARGE_RECORDS, 'retention_years_min')} years.", SC_LARGE_RECORDS)
        acc.req("Large operation: project signs", "Put up project contact signs meeting the Rule 403 Implementation "
                "Handbook before any earthmoving.", SC_SIGNAGE, due=start)
        acc.req("Large operation: dust control supervisor", "Name a dust control supervisor hired by the owner or "
                f"developer, on site or there within {P(SC_SUPERVISOR, 'response_time_minutes_max')} minutes, with the "
                "SCAQMD Fugitive Dust Control Class certificate.", SC_SUPERVISOR)
        acc.req("Large operation: Table 2 measures", f"Keep earth-moving soil at {P(SC_TABLE2, 'min_soil_moisture_percent')}"
                f"% moisture or more (tested by {P(SC_TABLE2, 'moisture_test')}); water unpaved roads at least every "
                f"{P(SC_TABLE2, 'unpaved_road_watering_interval_hours')} hours "
                f"({P(SC_TABLE2, 'unpaved_road_waterings_per_8h_day')} times per 8-hour day), or once a day with a "
                f"{P(SC_TABLE2, 'alt_once_daily_speed_limit_mph')} mph limit, or use a chemical stabilizer. Stabilize "
                f"finished grading within {P(SC_GRADING, 'chemical_stabilizer_within_working_days')} working days (or "
                f"start vegetative cover within {P(SC_GRADING, 'alt_vegetative_cover_within_days')} days).", SC_TABLE2,
                SC_GRADING)
    w_lim = P(SC_WIND, "wind_gust_mph_exclusive")
    if gust is not None and gust > w_lim:
        t3 = P(SC_WIND, "table3_examples")
        acc.req("High wind now: Table 3 contingency measures", f"Gusts of {_n(gust)} mph are over {w_lim} mph: carry out "
                "the Table 3 contingency measures for each dust source and keep daily records. Earth-moving: "
                + _join(t3["earth_moving"], "or") + f"; disturbed areas: water {t3['disturbed_areas_watering_per_day']} "
                f"times a day ({t3['disturbed_areas_watering_per_day_if_wind_dust']} if there is wind-driven dust); "
                "unpaved roads: " + _join(t3["unpaved_roads"], "or") + ".", SC_WIND)
    else:
        acc.req("High winds", f"When gusts exceed {w_lim} mph, the visible-dust and PM10 limits are excused only if the "
                "Table 3 contingency measures are used and daily records kept.", SC_WIND)
    if coachella:
        cv = core.rule(CV_PLAN)
        lim_sf = cv.param("disturbed_sqft_exclusive")
        sqft = round(acres * SQFT_PER_ACRE, 6) if acres is not None else None     # '5,000 sq ft' stays 5,000
        acc.notes.append("Coachella Valley: Rule 403.1 is supplemental to Rule 403 (Rule 403.1(b)), so both apply.")
        if sqft is not None:
            res.plan_required = sqft > lim_sf
            ac4 = f"{acres:,.4f}".rstrip("0").rstrip(".")
            acc.math.append(f"Coachella Valley plan: {ac4} ac x {SQFT_PER_ACRE:,} = {_n(sqft)} sq ft vs > "
                            f"{_n(lim_sf)} sq ft -> {'required' if res.plan_required else 'not required'}")
        if res.plan_required is not False:
            review = cv.param("district_review_days")
            by = start - timedelta(days=review) if start else None
            res.plan_submit_by = _iso(by)
            if by:
                acc.math.append(f"Plan submit by: {start} - {review} days (district review) = {by}")
            sup = cv.param("supervisor_required_acres_min")
            acc.req("Coachella Valley fugitive dust control plan", (
                "" if res.plan_required else f"If more than {_n(lim_sf)} sq ft is disturbed: ")
                + "no earthmoving until SCAQMD approves the plan (it acts within "
                f"{review} days, so submit by {by or f'{review} days before earthmoving'}). Valid "
                f"{cv.param('plan_valid_years')} year; resubmit at least {cv.param('resubmit_days_before_expiration_min')}"
                f" days before it expires. Keep it on site, post contact signs"
                + (f", and name a dust control supervisor ({_n(sup)}+ acres) who has completed the "
                   f"{cv.param('supervisor_class')}" if acres is not None and acres >= sup
                   else f" (at {_n(sup)}+ acres, also name a dust control supervisor who has completed the "
                        f"{cv.param('supervisor_class')})")
                + f". Fees: {cv.param('fees')}. Exception "
                "(Rule 403.1(i)(4)): the SCAQMD plan is not required for " + cv.param("plan_to_city_or_county_instead")
                + "; the plan then goes to that city or county, so ask its grading office first.", CV_PLAN, due=by)
        acc.req("Coachella Valley daily records", "Write down every day the dust control actions taken under Rule 403.1 "
                f"and keep the records at least {cv.param('daily_records_retention_years')} years.", CV_PLAN)
    if res.plan_required:
        res.next_step = ("Ask the city or county whether it has a District-approved dust control ordinance (then the "
                         "plan goes to it); otherwise submit the Coachella Valley dust control plan to SCAQMD by "
                         f"{res.plan_submit_by or str(core.rule(CV_PLAN).param('district_review_days')) + ' days before earthmoving'}"
                         ". No earthmoving until it is approved.")
    elif res.large_operation:
        res.next_step = (f"File Form {P(SC_403N, 'form')} within {P(SC_403N, 'file_within_days')} days of qualifying "
                         "as a large operation, name the certified dust control supervisor and put up the project "
                         "signs before earthmoving.")
    elif res.trackout_control_required:
        res.next_step = "Install a track-out control (gravel pad, paving, wheel shaker or washer) at every exit before hauling."
    else:
        res.next_step = "Brief the crew on Rule 403 Table 1 measures for each activity and clean track-out every day."
    big = {True: "yes", False: "no", None: "possible (depends on how many days)" if by_vol else
           "unknown (give disturbed_acres and cubic yards)"}[res.large_operation]
    res.summary = (f"{res.district_name}{' (Coachella Valley: Rules 403 and 403.1)' if coachella else ' Rule 403'}: "
                   + f"large operation {big}; "
                   + f"exit track-out control {({True: 'required', False: 'not required', None: 'depends on size'})[res.trackout_control_required]}"
                   + (f"; dust control plan {'required' if res.plan_required else 'not required'}" if coachella and
                      res.plan_required is not None else "") + ".")


def _san_joaquin(acc, res, acres, volume, days, ptype, start, trips, axle3, urban, demo, sfa, existing, added, today,
                 site=None, paved_roads=None, elev=None):
    acc.use(SJV_COUNTIES)
    high = P(SJV_EXEMPT, "regulation_viii_exempt_elevation_ft_min")
    if elev is not None and elev >= high:
        # Rule 8011 §4.3 (incorporated by Rule 8021 §4.0): Regulation VIII does not apply at 3,000 ft or higher
        res.plan_required, res.trackout_control_required = False, False
        acc.math.append(f"Elevation {_n(elev)} ft vs >= {_n(high)} ft -> Regulation VIII exempt")
        acc.req("Regulation VIII does not apply", f"Activities at {_n(high)} ft or more above sea level are exempt from "
                "the District's fugitive dust rules (Rule 8011 §4.3): no Dust Control Plan, notice or track-out "
                "device under Rules 8021 and 8041. Check your permit or CEQA conditions for dust measures.", SJV_EXEMPT)
        res.summary = (f"{res.district_name}: site at {_n(elev)} ft is exempt from Regulation VIII (dust rules apply "
                       f"below {_n(high)} ft).")
        res.next_step = "No District dust plan or notice; follow any dust conditions in your permits."
        return
    exempt_8021 = False
    if sfa and P(SJV_EXEMPT, "single_family_additions_exempt"):
        exempt_8021 = True
        acc.math.append("Rule 8021 exemption: addition to an existing single-family home")
    elif existing is not None:
        cap = min(existing * P(SJV_EXEMPT, "addition_max_percent") / 100, P(SJV_EXEMPT, "addition_max_sqft"))
        exempt_8021 = added <= cap
        acc.math.append(f"Rule 8021 exemption: added {_n(added)} sq ft vs the lesser of "
                        f"{_n(P(SJV_EXEMPT, 'addition_max_percent'))}% x {_n(existing)} = "
                        f"{_n(existing * P(SJV_EXEMPT, 'addition_max_percent') / 100)} and "
                        f"{_n(P(SJV_EXEMPT, 'addition_max_sqft'))} sq ft = {_n(cap)} -> "
                        f"{'exempt' if exempt_8021 else 'not exempt'}")
    if exempt_8021:
        res.plan_required = False
        acc.req("Rule 8021 does not apply", "This remodel or addition is exempt from Rule 8021 (parking lots and other "
                "ancillary construction are not). Track-out must still be cleaned up under Rule 8041.", SJV_EXEMPT)
    else:
        acc.req("Visible dust limit", f"Use enough controls (pre-water, phase work, water or stabilizers, wind barriers, "
                f"treat haul roads) to keep visible dust at or below {P(SJV_VDE, 'max_visible_dust_opacity_percent')}% "
                "opacity.", SJV_VDE)
        res_ac, non_ac = P(SJV_DCP, "residential_disturbed_acres_min"), P(SJV_DCP, "nonresidential_disturbed_acres_min")
        cy_lim, cy_days = P(SJV_DCP, "bulk_cy_per_day_exclusive"), P(SJV_DCP, "bulk_days_min")
        thr = res_ac if ptype == "residential" else non_ac
        if ptype is None:
            acc.notes.append(f"project_type not given: the stricter non-residential threshold ({_n(non_ac)} acres) is "
                             f"used; residential sites need a plan at {_n(res_ac)} acres.")
        by_area = acres is not None and acres >= thr
        by_vol = volume is not None and volume > cy_lim
        if by_area or (by_vol and days is not None and days >= cy_days):
            res.plan_required = True
        elif by_vol and days is None:
            res.plan_required = None
        elif acres is not None or volume is not None:
            res.plan_required = False
            if volume is None:
                acc.notes.append("Dust Control Plan need was checked on area only; give daily_earthmoving_cy to check "
                                 "the bulk material test too.")
        acc.use(SJV_DCP)
        acc.math.append(f"Dust Control Plan: {_given(acres, 'ac')} vs >= {_n(thr)} ac "
                        f"({(ptype or 'non_residential').replace('_', '-')}); {_given(volume, 'cy/day')} vs > {_n(cy_lim)} cy "
                        f"on {cy_days}+ days -> " + {True: "required", False: "not required",
                                                     None: f"required if that volume happens on {cy_days} or more days"
                                                     if by_vol else "unknown"}[res.plan_required])
        if res.plan_required is not False and (acres is not None or volume is not None):
            review = P(SJV_REVIEW, "recommended_submit_days_before_start")
            notice = P(SJV_DCP, "notification_days_before_earthmoving")
            by = start - timedelta(days=review) if start else None
            res.plan_submit_by = _iso(by)
            fee = P(SJV_FEE, "filing_fee_usd")
            fee_from = _pdate(P(SJV_FEE, "effective"))
            res.fees_usd = float(fee) if today >= fee_from else None
            if by:
                acc.math.append(f"Plan: submit by {start} - {review} days = {by}; written notice between "
                                f"{start - timedelta(days=notice)} and {start} (within {notice} days before)")
            cond = "" if res.plan_required else f"If more than {_n(cy_lim)} cy/day is moved on {cy_days}+ days: "
            acc.req("Dust Control Plan (Rule 8021)", cond + "submit a Dust Control Plan and do not start construction "
                    f"until the District approves it. The District acts within {P(SJV_REVIEW, 'district_review_days')} "
                    f"days (a plan without comments after {P(SJV_REVIEW, 'auto_approved_after_days_without_comment')} "
                    f"days is approved), so submit at least {review} days ahead"
                    + (f", by {by}" if by else "") + f". Then give written notice within {notice} days before "
                    "earthmoving starts.", SJV_DCP, SJV_REVIEW, due=by)
            acc.req("Dust Control Plan fee (Rule 3135)", f"{_usd(fee)} nonrefundable when you file (effective "
                    f"{fee_from}); {_usd(P(SJV_FEE, 'modification_fee_usd'))} to modify an approved plan; late charges "
                    f"after {P(SJV_FEE, 'late_fee_after_days')} days."
                    + ("" if today >= fee_from else " The fee before that date is not in the rulebook."), SJV_FEE)
            acc.req("Plan on site and training", "Keep the approved plan at the site until all dust-generating work "
                    f"ends; at least {P(SJV_ONSITE, 'training_required_persons_min')} key person (or the plan's "
                    "preparer) must complete the District's Dust Control Training Class.", SJV_ONSITE)
            if start:
                acc.req("Written notice before earthmoving", f"Send it between {start - timedelta(days=notice)} and "
                        f"{start}.", SJV_DCP, due=start)
        # Rule 8021 §6.4 keys the 48-hour notice to the construction site's area, not only the disturbed area
        site_area = site if site is not None else acres
        if res.plan_required is not True and site_area is not None:
            lo, hi = P(SJV_48H, "residential_acres_range" if ptype == "residential" else "nonresidential_acres_range")
            h = P(SJV_48H, "notice_hours_before_earthmoving")
            basis = "site" if site is not None else "disturbed (give site_acres: the rule uses the site's area)"
            if lo <= site_area < hi:
                by = start - timedelta(days=math.ceil(h / 24)) if start else None
                acc.math.append(f"48-hour notice: {_n(lo)} <= {_n(site_area)} ac {basis} < {_n(hi)} ac -> required")
                acc.req("48-hour notice to the District", f"Written notice at least {h} hours before earthmoving starts"
                        + (f" (by the start time on {by}; e.g. a 7:00 a.m. start on {start} means 7:00 a.m. on {by})"
                           if by else "") + ".", SJV_48H, due=by)
            elif site is None and site_area < lo:
                acc.use(SJV_48H)
                acc.notes.append(f"48-hour notice: required if the construction site itself is {_n(lo)} to under "
                                 f"{_n(hi)} acres ({(ptype or 'non_residential').replace('_', '-')}), even when less "
                                 "is disturbed (Rule 8021 §6.4); give site_acres to check.")
        acc.req("Inactive areas", f"Stabilize any disturbed area of {_n(P(SJV_INACTIVE, 'min_area_acres'))} acre or more "
                f"left unused {P(SJV_INACTIVE, 'idle_days_min')} days or more.", SJV_INACTIVE)
        acc.req("Unpaved haul roads", f"{P(SJV_SPEED, 'max_speed_mph')} mph on uncontrolled unpaved access and haul "
                f"roads, with speed signs at each entrance and at least every {P(SJV_SPEED, 'sign_spacing_ft_max')} ft, "
                "readable both ways.", SJV_SPEED)
        acc.req("Stop when dust is visible", f"Stop outdoor soil-disturbing work whenever visible dust exceeds "
                f"{P(SJV_WIND, 'cease_when_opacity_percent_exceeds')}% opacity; keep water trucks running unless "
                "unsafe.", SJV_WIND)
        if demo:
            acc.req("Demolition", f"Apply water within {P(SJV_DEMO, 'water_within_hours')} hour of demolition to "
                    f"unpaved surfaces within {P(SJV_DEMO, 'radius_ft')} ft of the structure.", SJV_DEMO)
    # Rule 8041 track-out
    acc.req("Track-out cleanup (Rule 8041)", "Remove all visible track-out from paved public roads at the end of every "
            "workday. Not allowed: " + _join(P(SJV_EOD, "prohibited_methods")) + ". Allowed: "
            + _join(P(SJV_EOD, "allowed_methods"), "or") + ".", SJV_EOD)
    ft, rural_ac = P(SJV_50FT, "immediate_cleanup_ft"), P(SJV_50FT, "rural_project_min_acres")
    project_ac = site if site is not None else acres          # Rule 8041 §5.5: 'construction projects 10 acres ... in size'
    if urban or (urban is False and project_ac is not None and project_ac >= rural_ac):
        acc.req(f"Track-out reaching {ft} ft", f"Prevent track-out, or remove it immediately once it extends {ft} ft or "
                "more from the exit.", SJV_50FT)
    elif urban is None:
        acc.req(f"Track-out reaching {ft} ft", f"In a city (or an unincorporated area surrounded by one), or on a rural "
                f"project of {_n(rural_ac)}+ acres: remove track-out immediately once it extends {ft} ft.", SJV_50FT)
    dev_trips, dev_axle = P(SJV_DEVICE, "vehicle_trips_per_day_min"), P(SJV_DEVICE, "three_axle_trips_per_day_min")
    paved_trigger = bool(paved_roads) and P(SJV_DEVICE, "paved_interior_road_sites")
    need_dev = bool(res.plan_required) or (trips is not None and trips >= dev_trips) or \
        (axle3 is not None and axle3 >= dev_axle) or paved_trigger
    res.trackout_control_required = need_dev if (trips is not None or axle3 is not None or res.plan_required
                                                 or paved_roads is not None) else None
    acc.use(SJV_DEVICE)
    if trips is not None or axle3 is not None or paved_roads is not None:
        acc.math.append(f"Track-out device: {_given(trips, 'trips/day')} vs >= {dev_trips}; {_given(axle3, '3+-axle trips/day')} vs "
                        f">= {dev_axle}; Dust Control Plan site: {'yes' if res.plan_required else 'no'}; paved interior "
                        f"roads: {({True: 'yes', False: 'no', None: 'not given'})[paved_roads]} -> "
                        f"{'required' if need_dev else 'not required'}")
    if need_dev or res.trackout_control_required is None:
        s = core.rule(SJV_SPECS).parameters
        cond = "" if need_dev else (f"If the site has {dev_trips}+ vehicle trips a day, {dev_axle}+ trips by 3+-axle "
                                    "vehicles, paved interior roads, or a Dust Control Plan: ")
        acc.req("Track-out device at every exit", cond + f"install one at each exit to a paved public road: a grizzly "
                f"at least {s['grizzly_min_length_ft']} ft long, a gravel pad of {s['gravel_min_size_in']}-inch+ rock "
                f"{s['gravel_min_depth_in']} in deep and {s['gravel_min_length_ft']} ft long, or {s['paving_min_length_ft']}"
                " ft of paving, each the full exit width. If track-out still happens, remove it within "
                f"{P(SJV_DEVICE, 'residual_trackout_removal_minutes')} minutes.", SJV_DEVICE, SJV_SPECS)
    acc.req("Daily dust records (Rule 8011)", "For each day a control is used, record the measure, where and how much "
            "area, and any suppressant's date, amount and frequency (keep the product sheet). Keep them "
            f"{P(SJV_RECORDS, 'retention_years_after_completion')} year after the project ends.", SJV_RECORDS)
    lines = P(SJV_COUNTIES, "complaint_lines")
    acc.notes.append("District complaint lines: " + ", ".join(f"{k} {v}" for k, v in lines.items()) + ".")
    if res.plan_required and res.plan_submit_by:
        res.next_step = (f"Submit the Dust Control Plan with the {_usd(P(SJV_FEE, 'filing_fee_usd'))} fee by "
                         f"{res.plan_submit_by}, then send the written notice within "
                         f"{P(SJV_DCP, 'notification_days_before_earthmoving')} days before earthmoving.")
    elif res.plan_required:
        res.next_step = (f"Submit the Dust Control Plan at least {P(SJV_REVIEW, 'recommended_submit_days_before_start')}"
                         " days before construction; give earthmoving_start to get the dates.")
    elif exempt_8021:
        res.next_step = "No Rule 8021 plan or notice; clean all track-out at the end of every workday (Rule 8041)."
    else:
        res.next_step = ("Line up water trucks and track-out cleanup, keep the daily dust log, and send any required "
                         "notice before earthmoving.")
    res.summary = (f"{res.district_name}: Dust Control Plan "
                   + ("not needed (Rule 8021 exempt)" if exempt_8021 else
                      {True: "required", False: "not required", None: "depends on size and volume"}[res.plan_required])
                   + (f" (fee {_usd(res.fees_usd)})" if res.fees_usd else "")
                   + f"; track-out device {({True: 'required', False: 'not required', None: 'depends on traffic'})[res.trackout_control_required]}.")


def _bay_area(acc, res, acres, sensitive, avg_wind):
    lim = P(BA_LARGE, "site_area_acres_exclusive")
    large = acres is not None and acres > lim
    acc.use(BA_LARGE)
    if acres is not None:
        acc.math.append(f"Rule 6-6: {_n(acres)} ac vs > {_n(lim)} ac -> {'applies' if large else 'does not apply'} "
                        "(the area counts construction, bulk material handling and disturbed surfaces)")
    res.trackout_control_required = large if acres is not None else None
    if large or acres is None:
        cond = "" if large else f"If the site is over {_n(lim)} acre: "
        acc.req("Rule 6-6 track-out", cond + f"track-out over {P(BA_TRACKOUT, 'trackout_trigger_cumulative_linear_ft_exclusive')}"
                f" cumulative linear ft that raises dust must be cleaned within {P(BA_TRACKOUT, 'cleanup_within_hours')} "
                f"hours of finding it; no more than {P(BA_TRACKOUT, 'max_end_of_workday_quarts')} quart may remain at the"
                f" end of the workday. Cleanup may not raise dust over {P(BA_OPACITY, 'max_opacity_percent')}% opacity "
                f"for more than {P(BA_OPACITY, 'max_minutes_per_60')} minutes an hour.", BA_TRACKOUT, BA_OPACITY)
        acc.req("Rule 6-6 monitoring and records", f"Check each active exit at least "
                f"{P(BA_MONITOR, 'inspections_per_workday_min')} times every workday at peak exit times, log the checks "
                f"and every cleanup, and keep the log {P(BA_MONITOR, 'retention_years')} years.", BA_MONITOR)
    b = core.rule(BA_BASIC).parameters
    w_lim = b["suspend_work_avg_wind_mph_exclusive"]
    acc.req("CEQA basic dust measures (usually project conditions)", f"Water exposed surfaces "
            f"{b['water_exposed_surfaces_per_day']} times a day; cover haul trucks; remove track-out at least daily with "
            f"a {b['trackout_method']} (no dry power sweeping); {b['unpaved_speed_limit_mph']} mph on unpaved roads; "
            f"suspend excavation, grading and demolition when average wind exceeds {w_lim} mph; wash vehicles before "
            f"they leave; treat unpaved access roads {b['access_road_treatment_trigger_ft']}+ ft from pavement with "
            f"{b['access_road_layer_in'][0]}-{b['access_road_layer_in'][1]} in of wood chips, mulch or gravel; post a "
            f"sign with {_join(b['sign_must_show'])}, and respond to complaints within {b['complaint_response_hours']} "
            "hours. These come from the 2022 CEQA Guidelines (nonbinding) and apply when the lead agency imposes them; "
            "check your permit conditions.", BA_BASIC)
    if avg_wind is not None and avg_wind > w_lim:
        acc.req("Wind now over the limit", f"Average wind {_n(avg_wind)} mph > {w_lim} mph: suspend excavation, grading "
                "and demolition if the CEQA basic measures are conditions of your project.", BA_BASIC)
    if sensitive:
        e = core.rule(BA_ENHANCED).parameters
        acc.req("CEQA enhanced measures near sensitive uses", f"Limit simultaneous ground disturbance; wind breaks of at "
                f"most {e['wind_break_max_porosity_percent']}% porosity; early ground cover; sandbags on slopes over "
                f"{e['erosion_control_slope_percent_exclusive']}%; minimal stockpiles; hydroseed or stabilize areas "
                f"inactive {e['inactive_area_days_min']}+ calendar days.", BA_ENHANCED)
    res.summary = (f"{res.district_name}: Rule 6-6 "
                   + ({True: "applies", False: "does not apply"}[large] if acres is not None else f"applies over {_n(lim)} acre")
                   + "; the CEQA basic dust measures apply when your project's conditions include them.")
    res.next_step = (f"Set up exit checks {P(BA_MONITOR, 'inspections_per_workday_min')} times a workday and a "
                     f"track-out log (kept {P(BA_MONITOR, 'retention_years')} years), and post the dust complaint sign "
                     "before work starts." if large else
                     "Check your permit / CEQA conditions for the basic dust measures and post the dust complaint sign.")


# ================================================================ portable engines (PERP and Portable Engine ATCM)
class PortableEngineResult(_Base):
    engine_bhp: float
    subject_to_atcm: bool
    portable: Optional[bool] = None
    perp_eligible_new_registration: Optional[bool] = None
    atcm_fleet_size: Optional[Literal["small", "large"]] = None
    low_use: Optional[bool] = None
    fees_usd: Optional[float] = None
    renewal_return_by: Optional[str] = None
    project_notice_required: Optional[bool] = None
    project_notice_due: Optional[str] = None


def _perp_tier(value) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    s = re.sub(r"[^a-z0-9]", "", str(value).lower()).replace("tier", "t")
    flex = "flex" in s
    s = s.replace("flex", "")
    base = {"t4f": "Tier 4 Final", "t4final": "Tier 4 Final", "t4i": "Tier 4 Interim", "t4interim": "Tier 4 Interim",
            "t3": "Tier 3", "t2": "Tier 2", "t1": "Tier 1", "t0": "Tier 0"}.get(s)
    if s == "t4":
        raise ValueError("'Tier 4' is ambiguous: say Tier 4 Final or Tier 4 Interim (and 'flex' if certified flex)")
    if not base:
        raise ValueError(f"engine_tier must be like 'Tier 4 Final', 'Tier 4 Interim flex', 'Tier 3 flex' or "
                         f"'Tier 2 flex'; got {value!r}")
    return base + (" flex" if flex else "")


# special_category -> the start of its entry in the Portable Engine ATCM not-subject list (PERP FAQ Q14)
_PERP_SPECIAL = {"agricultural_source": "engines owned by an agricultural source",
                 "emergency_only": "engines used exclusively to alleviate",
                 "hazardous_location_tier3": "engines approved to operate in hazardous locations",
                 "military_tactical": "military-owned engines used in tactical support equipment",
                 "harbor_craft": "engines used exclusively on harbor craft"}


def _perp_special(value) -> Optional[str]:
    w = _words(value or "")
    if not w or w in ("none", "no", "n a", "na"):
        return None
    key = w.replace(" ", "_")
    aliases = {"agricultural": "agricultural_source", "ag": "agricultural_source", "farm": "agricultural_source",
               "emergency only": "emergency_only", "hazloc": "hazardous_location_tier3",
               "hazardous location": "hazardous_location_tier3", "military": "military_tactical",
               "harbor craft": "harbor_craft", "marine": "harbor_craft"}
    if w in ("emergency", "emergency use", "emergency use engine"):
        raise ValueError("'emergency' is ambiguous: an emergency-use engine (run only for maintenance, testing and "
                         "emergencies) is still under the Portable Engine ATCM; only an engine used exclusively to "
                         "alleviate a threat to public health and safety during an emergency event is outside it "
                         "(special_category='emergency_only')")
    key = key if key in _PERP_SPECIAL else aliases.get(w)
    if not key:
        raise ValueError("special_category must be none, " + ", ".join(_PERP_SPECIAL) + f"; got {value!r}")
    return key


def portable_engine_requirements(engine_bhp, engine_tier=None, fuel="diesel", propels_vehicle=False,
                                 auxiliary_engine_on_two_engine_vehicle=False, months_at_location=None,
                                 on_foundation=False, perp_registered=None, registration_expires=None,
                                 fleet_total_bhp=None, annual_hours=None, project_registered_bhp_on_site=None,
                                 extreme_ozone_district=None, project_start=None, engines_to_register=0,
                                 equipment_units_to_register=0, renewal=False, years_at_same_location=None,
                                 months_operated_per_year=None, special_category=None,
                                 as_of=None) -> PortableEngineResult:
    """Portable Engine ATCM and PERP for one portable engine (generator, compressor, pump, light tower...)."""
    today = _as_of(as_of)
    bhp = _num("engine_bhp", engine_bhp, 0, MAX_BHP)
    if bhp is None:
        raise ValueError("engine_bhp is required (the engine's rated brake horsepower)")
    tier = _perp_tier(engine_tier)
    f = _fuel(fuel)
    propels = bool(_bool("propels_vehicle", propels_vehicle))
    aux = bool(_bool("auxiliary_engine_on_two_engine_vehicle", auxiliary_engine_on_two_engine_vehicle))
    months = _num("months_at_location", months_at_location, 0, 1200)
    years_here = _num("years_at_same_location", years_at_same_location, 0, 100)
    months_run = _num("months_operated_per_year", months_operated_per_year, 0, 12)
    special = _perp_special(special_category)
    foundation = bool(_bool("on_foundation", on_foundation))
    registered = _bool("perp_registered", perp_registered)
    expires = _date(registration_expires, "registration_expires")
    fleet = _num("fleet_total_bhp", fleet_total_bhp, 0, MAX_FLEET_HP)
    hours = _num("annual_hours", annual_hours, 0, 8784)
    proj = _num("project_registered_bhp_on_site", project_registered_bhp_on_site, 0, MAX_FLEET_HP)
    extreme = _bool("extreme_ozone_district", extreme_ozone_district)
    pstart = _date(project_start, "project_start")
    n_eng = _whole("engines_to_register", engines_to_register, 0, 10_000) or 0
    n_units = _whole("equipment_units_to_register", equipment_units_to_register, 0, 10_000) or 0
    is_renewal = bool(_bool("renewal", renewal))
    acc = _Acc()
    acc.use(PERP_ATCM)
    min_bhp = P(PERP_ATCM, "min_bhp")
    diesel = f in _DIESEL_FUELED
    special_entry = (next(e for e in P(PERP_ATCM, "not_subject") if e.startswith(_PERP_SPECIAL[special]))
                     if special else None)
    subject = diesel and bhp >= min_bhp and not propels and not aux and not special_entry
    res = PortableEngineResult(engine_bhp=bhp, subject_to_atcm=subject)
    acc.math.append(f"Portable Engine ATCM: {'diesel' if diesel else f}, {_n(bhp)} bhp vs >= {_n(min_bhp)} bhp"
                    + ("; propels a vehicle" if propels else "") + ("; two-engine vehicle auxiliary" if aux else "")
                    + (f"; {special_entry}" if special_entry else "")
                    + f" -> {'subject' if subject else 'not subject'}")
    if propels or aux:
        acc.use(ORD_APPLIES)
        acc.notes.append("Engines that propel a vehicle, and auxiliary engines of two-engine vehicles such as cranes "
                         f"(when {P(ORD_APPLIES, 'two_engine_auxiliary_min_bhp')} bhp or more), fall under CARB's "
                         "off-road fleet rule instead: use offroad_fleet_obligations.")
    acc.req("PERP is voluntary; district permits are not", f"PERP registration lets portable equipment run "
            f"statewide without a permit from each of the {P(PERP_VOLUNTARY, 'air_districts')} air districts. If a "
            "district requires a permit, get it even if the unit is in PERP.", PERP_VOLUNTARY)
    lim = P(PERP_PORTABLE, "max_consecutive_months_at_location")
    seas = P(PERP_PORTABLE, "seasonal_source_not_portable")
    seasonal = (years_here is not None and months_run is not None and years_here >= seas["min_years_at_location"]
                and months_run >= seas["min_months_operating_per_year"])
    if months is not None or foundation or seasonal:
        res.portable = not foundation and not seasonal and (months is None or months <= lim)
        acc.math.append(f"Portable: {_given(months, 'consecutive months')} at one location vs <= {_n(lim)}"
                        + ("; on a foundation" if foundation else "")
                        + (f"; seasonal source ({_n(years_here)} years at the location, {_n(months_run)} months a year "
                           f"vs >= {seas['min_years_at_location']} years and >= "
                           f"{seas['min_months_operating_per_year']} months)" if seasonal else "")
                        + f" -> {'yes' if res.portable else 'no'}")
    acc.req("Stays portable", f"It must be movable, not on a foundation, and not stay at one location more than {lim} "
            "consecutive months (the clock starts when it arrives; moving it away and back does not reset it). It is "
            f"also not portable as a seasonal source: at one location {seas['min_years_at_location']}+ years and run "
            f"{seas['min_months_operating_per_year']}+ months each year.", PERP_PORTABLE)
    if not subject:
        res.summary = ("Not subject to the Portable Engine ATCM"
                       + (f" ({_n(bhp)} bhp < {_n(min_bhp)} bhp)" if diesel and bhp < min_bhp else
                          " (not a diesel engine)" if not diesel else f" ({special_entry})" if special_entry else
                          " (it propels a vehicle or is a two-engine vehicle's auxiliary engine)")
                       + ". A district permit may still be required.")
        res.next_step = "Ask the air district where it will run whether it needs a permit."
        return acc.finish(res)
    if res.portable is False:
        why = ("on a foundation" if foundation else "a seasonal source" if seasonal
               else f"more than {lim} months at one location")
        res.summary = f"Not portable ({why}): PERP does not cover it; it needs the district's stationary-source permit."
        res.next_step = (f"Apply for a permit from the air district where it sits"
                         + (" (a seasonal source is a stationary source)." if seasonal and not foundation else
                            "." if foundation else f", or move it before {lim} months run out."))
        return acc.finish(res)
    # eligibility for a new registration
    if tier:
        acc.use(PERP_ELIGIBLE)
        bands = {}
        for k, v in core.rule(PERP_ELIGIBLE).parameters.items():
            nums = [int(x) for x in re.findall(r"\d+", k)]
            if k.startswith("bhp_") and nums:
                bands[k] = (nums, v)
        ok = None
        for k, (nums, allowed) in bands.items():
            in_band = (len(nums) == 2 and nums[0] <= bhp <= nums[1]) or ("over" in k and bhp > nums[0])
            if in_band:
                ok = tier.lower() in [a.lower() for a in allowed]
                acc.math.append(f"PERP eligibility: {_n(bhp)} bhp is in '{k}'; allowed {_join(allowed, 'or')}; "
                                f"{tier} -> {'eligible' if ok else 'not eligible'}")
                acc.req("PERP registration eligibility", f"New registrations at this size need {_join(allowed, 'or')}"
                        f" engines: {tier} is {'eligible' if ok else 'NOT eligible'}. Tier 3 hazardous-location engines "
                        f"stay eligible until {P(PERP_ELIGIBLE, 'hazloc_tier3_eligible_until')}.", PERP_ELIGIBLE)
        res.perp_eligible_new_registration = ok
    # fleet size
    acc.use(PERP_FLEET)
    if fleet is not None:
        small = P(PERP_FLEET, "small_max_bhp")
        res.atcm_fleet_size = "small" if fleet <= small else "large"
        acc.math.append(f"ATCM fleet: {_n(fleet)} bhp vs <= {_n(small)} bhp -> {res.atcm_fleet_size}")
        acc.req("Portable engine fleet", f"{res.atcm_fleet_size.capitalize()} fleet: "
                + ("follow the tier phase-out schedule (each engine's date is on its registration certificate)."
                   if res.atcm_fleet_size == "small" else "follow the tier phase-out schedule, or the fleet average "
                   f"option only if you opted into it by {P(PERP_FLEET, 'fleet_average_opt_in_deadline')}.")
                + f" Every company registering after {P(PERP_FLEET, 'new_registrants_after')} is a small fleet.",
                PERP_FLEET)
    # low use
    acc.use(PERP_LOW_USE)
    lu = P(PERP_LOW_USE, "max_hours_per_calendar_year")
    if hours is not None:
        res.low_use = hours <= lu
        acc.math.append(f"Low-use: {_n(hours)} h/yr vs <= {_n(lu)} h -> {'yes' if res.low_use else 'no'}")
    if res.low_use is not False:
        due = _next_md(_pmd(P(PERP_LOW_USE, "annual_report_due")), today)
        acc.req("Low-use engine", ("" if res.low_use else f"If it runs {lu} hours a year or less: ")
                + f"no more than {lu} hours per calendar year (hours outside California and in emergencies do not "
                f"count), a non-resettable hour meter, and the annual low-use report by {due} for last year.",
                PERP_LOW_USE, due=due if res.low_use else None)
    # renewal
    acc.use(PERP_TERM, PERP_METER)
    if expires:
        back = P(PERP_TERM, "return_renewal_at_least_months_before")
        ret = _add_months(expires, -back)
        inv = expires - timedelta(days=P(PERP_TERM, "renewal_invoice_sent_days_before"))
        meter = _add_months(expires, -P(PERP_METER, "reading_within_months_of_expiration"))
        res.renewal_return_by = ret.isoformat()
        acc.math.append(f"Renewal: invoice about {expires} - {P(PERP_TERM, 'renewal_invoice_sent_days_before')} days = "
                        f"{inv}; return by {expires} - {back} months = {ret}; hour meter reading on or after {meter}")
        acc.req("PERP renewal", f"Registration expires {expires} (terms are usually {P(PERP_TERM, 'term_years')} years). "
                f"CARB emails the invoice about {inv}; email it back to {P(PERP_TERM, 'renewal_email')} by {ret} to "
                f"avoid the late fee, with an hour meter reading taken on or after {meter} (within "
                f"{P(PERP_METER, 'reading_within_months_of_expiration')} months of expiration).", PERP_TERM, PERP_METER,
                due=ret)
    elif registered:
        acc.req("PERP renewal", f"Registrations usually run {P(PERP_TERM, 'term_years')} years. Return the renewal "
                f"invoice at least {P(PERP_TERM, 'return_renewal_at_least_months_before')} months before expiration "
                f"to {P(PERP_TERM, 'renewal_email')}, with an hour meter reading taken within "
                f"{P(PERP_METER, 'reading_within_months_of_expiration')} months of expiration. Give "
                "registration_expires for the dates.", PERP_TERM, PERP_METER)
    # fees
    if n_eng or n_units:
        acc.use(PERP_FEES)
        fe = core.rule(PERP_FEES).parameters
        e_fee = fe["renewal_engine_or_pile_driver_usd" if is_renewal else "initial_engine_or_pile_driver_usd"]
        u_fee = fe["renewal_equipment_unit_usd" if is_renewal else "initial_equipment_unit_usd"]
        res.fees_usd = float(n_eng * e_fee + n_units * u_fee)
        kind = "renewal" if is_renewal else "initial"
        acc.math.append(f"PERP {kind} fees: {n_eng} engine(s) x {_usd(e_fee)} + {n_units} equipment unit(s) x "
                        f"{_usd(u_fee)} = {_usd(res.fees_usd)}")
        acc.req("PERP fees", f"{kind.capitalize()} registration: {_usd(e_fee)} per engine or pile driver, "
                f"{_usd(u_fee)} per equipment unit; {_usd(res.fees_usd)} for this request. Not refundable"
                + ("; a late renewal adds a penalty fee." if is_renewal else "."), PERP_FEES)
    # large project notice
    if proj is not None:
        acc.use(PERP_PROJECT)
        pl = P(PERP_PROJECT, "bhp_threshold_exclusive")
        over = proj > pl
        if over and extreme is not False:
            res.project_notice_required = True if extreme else None
            d14 = P(PERP_PROJECT, "notice_days_before_startup")
            due = pstart - timedelta(days=d14) if pstart else None
            res.project_notice_due = _iso(due)
            acc.math.append(f"Project notice: {_n(proj)} registered bhp on site vs > {_n(pl)} bhp -> over"
                            + (f"; due {pstart} - {d14} days = {due}" if due else ""))
            acc.req("Large project notice to the district", ("" if extreme else "If the district is designated extreme "
                    "non-attainment for ozone (the rulebook does not list them; ask the district): ")
                    + f"notify the district in writing at least {d14} days before startup"
                    + (f" (by {due})" if due else "")
                    + f", or within {P(PERP_PROJECT, 'notice_hours_after_exceedance')} hours if the site goes over "
                    f"{_n(pl)} bhp after startup. Exempt: {P(PERP_PROJECT, 'exempt')}.", PERP_PROJECT, due=due)
        else:
            res.project_notice_required = False
            acc.math.append(f"Project notice: {_n(proj)} registered bhp on site vs > {_n(pl)} bhp -> "
                            + ("not over" if not over else "district not extreme ozone non-attainment"))
    res.summary = (f"{_n(bhp)} bhp diesel portable engine: subject to the Portable Engine ATCM"
                   + (f"; PERP new registration {'eligible' if res.perp_eligible_new_registration else 'NOT eligible'}"
                      if res.perp_eligible_new_registration is not None else "")
                   + (f"; renewal back by {res.renewal_return_by}" if res.renewal_return_by else "")
                   + (f"; district notice by {res.project_notice_due}" if res.project_notice_due else "") + ".")
    if res.project_notice_required and res.project_notice_due:
        res.next_step = f"Send the district the written large-project notice by {res.project_notice_due}."
    elif res.renewal_return_by and _pdate(res.renewal_return_by) >= today:
        res.next_step = (f"Take an hour meter reading and email the renewal invoice to {P(PERP_TERM, 'renewal_email')} "
                         f"by {res.renewal_return_by}.")
    elif res.perp_eligible_new_registration is False:
        res.next_step = ("This engine cannot get a new PERP registration; get a district permit or use an eligible "
                         "(e.g. Tier 4 Final) engine.")
    elif registered is False:
        res.next_step = ("Register it in PERP (or get each district's permit) before it runs; check the district's "
                         "permit rules either way.")
    else:
        res.next_step = "Keep the registration, hour meter and any district permit current, and log hours each year."
    return acc.finish(res)


# ================================================================ MCP
def _tool_docs() -> dict:
    s = core.rule(ORD_SIZE).parameters
    return {
        "fleet": (
            "CARB In-Use Off-Road Diesel-Fueled Fleets regulation (13 CCR 2449, 2022 amendments) for an equipment "
            f"fleet: excavators, loaders, dozers, backhoes, cranes, forklifts and other self-propelled off-road diesel "
            f"machines of {P(ORD_APPLIES, 'min_engine_max_hp')} hp or more. Give the total max hp under common "
            "ownership or control (or the machines), the owner type and optionally the counties you work in. Returns "
            f"the fleet size (small <= {s['small_max_hp']:,} hp, medium {s['medium_min_hp']:,}-{s['medium_max_hp']:,}, "
            f"large > {s['large_min_hp_exclusive']:,}), the lowest engine tier you may add today, the Tier phase-out "
            "dates and each machine's last day, the designation deadline, DOORS reporting (due "
            f"{P(ORD_ANNUAL, 'annual_due')}) and ROAR, EIN labels, the "
            f"{P(ORD_IDLE, 'max_consecutive_idle_minutes')}-minute idling limit, R99/R100 renewable diesel, records, "
            "math, a next step and citations. Fleet average / BACT targets are not computed."),
        "contracting": (
            "Off-road diesel equipment duties for prime contractors and public works awarding bodies (13 CCR "
            "2449(i)-(j)): get each fleet's (and its listed subs') Certificate of Reported Compliance before signing or "
            f"renewing, collect new ones every {P(ORD_PRIME_ANNUAL, 'window_start')} to "
            f"{P(ORD_PRIME_ANNUAL, 'window_end')}, allow only certified fleets on site, report problems within "
            f"{P(ORD_PRIME_REPORT, 'report_within_business_days')} business days, post CARB's sign by calendar day "
            f"{P(ORD_PRIME_SIGN, 'post_by_calendar_day')} on projects of {P(ORD_PRIME_SIGN, 'trigger_min_calendar_days')}"
            f"+ days, keep certificates {P(ORD_CRC_RETAIN, 'retain_years_after_project_completion')} years. Handles "
            "the homeowner and emergency exceptions, gives dates from the project dates you pass, and (hires_haul_trucks)"
            " the Truck and Bus and Clean Truck Check checks for hired trucks."),
        "truck": (
            "One heavy on-road truck: CARB Truck and Bus (diesel over "
            f"{P(TB_APPLIES, 'min_gvwr_lbs_exclusive'):,} lb GVWR needs a {P(TB_2010, 'min_engine_model_year')}+ "
            f"engine; low-use under {P(TB_LOW_USE, 'max_ca_miles_per_year_exclusive'):,} CA miles a year; NOx exempt "
            "area), the DMV registration check, Clean Truck Check (tests per year, next deadlines from the DMV "
            f"registration expiration date, the {P(CTC_WINDOW, 'early_test_window_days')}-day test window, the fee), the "
            f"{P(IDLE_TRUCK, 'max_consecutive_idle_minutes')}-minute idling limit for diesel over "
            f"{P(IDLE_TRUCK, 'min_gvwr_lbs_exclusive'):,} lb, and Advanced Clean Fleets status (government fleets "
            "only). Also handles pickups, dedicated snow-removal vehicles, two-engine cranes and motor homes, and a VIN "
            "for trucks registered outside California. Accepts '33,000 lb', 'R99', 'CNG'."),
        "dust": (
            "Construction dust rules for one site. air_district: 'South Coast' / 'SCAQMD' / 'AQMD', 'Coachella "
            "Valley', 'San Joaquin Valley' / 'Valley Air', 'Bay Area' / 'BAAQMD', or the county ('Fresno', 'County of "
            "Orange', 'LA', 'City of San Diego'). Covers South Coast Rules 403/403.1 (track-out controls at "
            f"{P(SC_EXIT, 'trigger_disturbed_acres_min')}+ acres or {P(SC_EXIT, 'trigger_daily_import_export_cy_min')}+"
            f" cy/day, large operations at {P(SC_LARGE, 'disturbed_acres_min')}+ acres or "
            f"{P(SC_LARGE, 'daily_earthmoving_cy_min'):,} cy/day, Form 403 N, Coachella plans), San Joaquin Valley "
            f"Rules 8021/8041/8011/3135 (Dust Control Plan at {P(SJV_DCP, 'nonresidential_disturbed_acres_min')} acres "
            f"non-residential / {P(SJV_DCP, 'residential_disturbed_acres_min')} residential, "
            f"{_usd(P(SJV_FEE, 'filing_fee_usd'))} fee, 48-hour notices, track-out devices) and Bay Area Rule 6-6 "
            f"(sites over {P(BA_LARGE, 'site_area_acres_exclusive')} acre) with the CEQA dust measures. Other "
            "districts: says so. Give site_acres (the whole site) as well as disturbed_acres in the San Joaquin Valley: "
            "its 48-hour notice uses the site's area. Returns dated plans and notices, math, a next step and "
            "citations."),
        "perp": (
            "One portable engine (generator, compressor, pump, light tower, welder, crusher): the Portable Engine ATCM "
            f"({P(PERP_ATCM, 'min_bhp')}+ bhp diesel), whether it is still portable "
            f"(<= {P(PERP_PORTABLE, 'max_consecutive_months_at_location')} months at a location), PERP registration "
            f"eligibility by tier, fees ({_usd(P(PERP_FEES, 'initial_engine_or_pile_driver_usd'))} per engine new, "
            f"{_usd(P(PERP_FEES, 'renewal_engine_or_pile_driver_usd'))} renewal), renewal dates, low-use "
            f"(<= {P(PERP_LOW_USE, 'max_hours_per_calendar_year')} h/yr) reporting and the "
            f"{P(PERP_PROJECT, 'bhp_threshold_exclusive'):,} bhp project notice."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    nn = {"ge": 0, "allow_inf_nan": False}
    num = Optional[Union[float, str]]
    iso = Optional[str]

    @mcp.tool(name="offroad_fleet_obligations", annotations=ro, description=docs["fleet"])
    def _offroad_fleet_obligations(
        total_fleet_hp: Annotated[num, Field(description=(
            f"Total max hp of every off-road diesel vehicle ({_n(P(ORD_APPLIES, 'min_engine_max_hp'))} hp+) under "
            "common ownership or control, leaving out "
            "permanent low-use, snow-removal and emergency-only vehicles, e.g. 3200 or '3,200 hp'"))] = None,
        fleet_owner_type: Annotated[str, Field(description=(
            "business (default), non_profit, local_municipality, low_population_county_municipality, "
            "non_profit_training_center, state_agency or federal_agency"))] = "business",
        vehicles: Annotated[Optional[list[OffRoadVehicle]], Field(description=(
            "The machines, e.g. [{'name': 'CAT 320 #14', 'hp': 158, 'engine_tier': 'Tier 2'}, {'hp': 60, "
            "'engine_tier': 'T4f'}]"))] = None,
        operating_counties: Annotated[Optional[Union[list[str], str]], Field(description=(
            "Every county the fleet works in (for the captive attainment area test), e.g. ['Humboldt', 'Lake'] or "
            "'Humboldt, Lake'"))] = None,
        uses_optional_500hp_schedule: Annotated[Optional[bool], Field(description=(
            f"Fleets of {_n(P(ORD_OPT_500, 'max_fleet_hp'))} hp or less: you elected the 13 CCR 2449(e)(16) optional "
            "schedule"))] = None,
        rents_out_equipment: Annotated[bool, Field(description="You rent machines out to others")] = False,
        new_fleet: Annotated[bool, Field(description="First off-road diesel machines bought or brought into CA")]
        = False,
        vehicle_added_on: Annotated[iso, Field(description=(
            "ISO date a machine was bought or brought into California (EIN and new-fleet report dates)"))] = None,
        as_of: Annotated[iso, Field(description="ISO date; default today in California")] = None,
    ) -> OffRoadFleetResult:
        return offroad_fleet_obligations(total_fleet_hp, fleet_owner_type, vehicles, operating_counties,
                                         uses_optional_500hp_schedule, rents_out_equipment, new_fleet,
                                         vehicle_added_on, as_of)

    @mcp.tool(name="offroad_contracting_requirements", annotations=ro, description=docs["contracting"])
    def _offroad_contracting_requirements(
        role: Annotated[str, Field(description=(
            "prime_contractor (you hold the contract with the owner or awarding authority; 'GC'), "
            "public_works_awarding_body ('city', 'school district'), subcontractor ('sub') or private_owner "
            "('owner', 'developer')"))] = "prime_contractor",
        offroad_vehicles_on_project: Annotated[Optional[bool], Field(description=(
            f"Will off-road diesel vehicles of {_n(P(ORD_APPLIES, 'min_engine_max_hp'))} hp+ operate on the "
            "project?"))] = True,
        homeowner_personal_residence: Annotated[bool, Field(description=(
            "Your contract is with a homeowner for their own home (not used for business)"))] = False,
        emergency_operations: Annotated[bool, Field(description=(
            "The project is emergency operations (not routine maintenance or construction)"))] = False,
        first_vehicle_operates_on: Annotated[iso, Field(description="ISO date the first machine operates")] = None,
        expected_operating_days: Annotated[Optional[int], Field(ge=0, le=36600, description=(
            "Calendar days from the first day a machine operates on the project to the last, weekends included"))]
        = None,
        contract_date: Annotated[iso, Field(description="ISO date you will sign or renew a contract with a fleet")]
        = None,
        project_completion_date: Annotated[iso, Field(description="ISO date the project is completed")] = None,
        noncompliance_discovered_on: Annotated[iso, Field(description=(
            "ISO date you found a fleet without a valid certificate or a noncompliant machine"))] = None,
        hires_haul_trucks: Annotated[bool, Field(description=(
            "You hire or dispatch other companies' heavy trucks (dump, haul, water trucks)"))] = False,
        as_of: Annotated[iso, Field(description="ISO date; default today in California")] = None,
    ) -> ContractingResult:
        return offroad_contracting_requirements(role, offroad_vehicles_on_project, homeowner_personal_residence,
                                                emergency_operations, first_vehicle_operates_on,
                                                expected_operating_days, contract_date, project_completion_date,
                                                noncompliance_discovered_on, hires_haul_trucks, as_of)

    @mcp.tool(name="truck_compliance", annotations=ro, description=docs["truck"])
    def _truck_compliance(
        gvwr_lbs: Annotated[Union[float, str], Field(description=(
            "Manufacturer's gross vehicle weight rating, e.g. 33000 or '33,000 lb'"))],
        engine_model_year: Annotated[Optional[int], Field(ge=1900, le=2100, description=(
            "Engine model year from the emission control label"))] = None,
        fuel: Annotated[str, Field(description=(
            "diesel (default), renewable diesel / R99 / biodiesel, dual_fuel, natural_gas (CNG/LNG), propane, "
            "gasoline or zero_emission"))] = "diesel",
        ca_registered: Annotated[Optional[bool], Field(description="Registered with the California DMV")] = True,
        registration_expires: Annotated[iso, Field(description="ISO DMV registration expiration date")] = None,
        annual_ca_miles: Annotated[num, Field(description="Miles driven in California per year")] = None,
        powers_stationary_equipment: Annotated[bool, Field(description=(
            "The engine or PTO powers equipment used only while parked (drill rig, concrete pump)"))] = False,
        annual_pto_hours: Annotated[num, Field(description="Engine/PTO hours a year")] = None,
        operating_counties: Annotated[Optional[Union[list[str], str]], Field(description=(
            "Every county the truck operates in (NOx exempt area test)"))] = None,
        has_pm_filter: Annotated[Optional[bool], Field(description="Has a diesel particulate filter")] = None,
        obd_equipped: Annotated[Optional[bool], Field(description="Has on-board diagnostics (OBD)")] = None,
        vehicle_use: Annotated[str, Field(description=(
            "truck (default), pickup, motor_home, agricultural, emergency, military_tactical, snow_removal (dedicated "
            "snow-removal vehicle) or two_engine_crane (two-engine crane, drill rig or workover rig)"))] = "truck",
        owner_type: Annotated[str, Field(description="private (default), state_or_local_government or federal")]
        = "private",
        vehicle_model_year: Annotated[Optional[int], Field(ge=1900, le=2100, description=(
            "Vehicle model year (the DMV check schedule uses it)"))] = None,
        acquired_on: Annotated[iso, Field(description="ISO date you bought it")] = None,
        new_vehicle: Annotated[bool, Field(description="Bought new (first retail sale)")] = False,
        leased: Annotated[bool, Field(description="The truck is leased")] = False,
        selling: Annotated[bool, Field(description="You are selling it")] = False,
        vin: Annotated[Optional[str], Field(description=(
            "VIN, or just its last digit: sets the Clean Truck Check deadlines of a truck registered outside "
            "California or exempt from DMV registration"))] = None,
        as_of: Annotated[iso, Field(description="ISO date; default today in California")] = None,
    ) -> TruckResult:
        return truck_compliance(gvwr_lbs, engine_model_year, fuel, ca_registered, registration_expires,
                                annual_ca_miles, powers_stationary_equipment, annual_pto_hours, operating_counties,
                                has_pm_filter, obd_equipped, vehicle_use, owner_type, vehicle_model_year, acquired_on,
                                new_vehicle, leased, selling, vin, as_of)

    @mcp.tool(name="dust_control_requirements", annotations=ro, description=docs["dust"])
    def _dust_control_requirements(
        air_district: Annotated[str, Field(min_length=1, description=(
            "Air district or the jobsite county: 'South Coast', 'Coachella Valley', 'San Joaquin Valley', 'Bay Area', "
            "'Fresno', 'County of Orange', 'LA'"))],
        disturbed_acres: Annotated[num, Field(description=(
            "Disturbed surface area: acres (2.5) or text ('2.5 acres', '8,000 sq ft')"))] = None,
        daily_import_export_cy: Annotated[num, Field(description=(
            "Cubic yards a day hauled onto or off the site, e.g. 120 or '120 cy'"))] = None,
        daily_earthmoving_cy: Annotated[num, Field(description=(
            "Peak cubic yards a day moved, deposited or relocated on site"))] = None,
        earthmoving_days_at_that_volume: Annotated[Optional[int], Field(ge=0, le=366, description=(
            "How many days (in a year) the site will move that peak volume"))] = None,
        project_type: Annotated[Optional[str], Field(description=(
            "residential or non_residential (commercial, industrial, institutional, public)"))] = None,
        earthmoving_start: Annotated[iso, Field(description="ISO date earthmoving starts")] = None,
        coachella_valley: Annotated[Optional[bool], Field(description=(
            "South Coast site in the Coachella Valley (adds Rule 403.1)"))] = None,
        vehicle_trips_per_day: Annotated[Optional[int], Field(ge=0, description="Vehicle trips a day")] = None,
        three_axle_trips_per_day: Annotated[Optional[int], Field(ge=0, description=(
            "Trips a day by vehicles with 3 or more axles"))] = None,
        urban_area: Annotated[Optional[bool], Field(description=(
            "San Joaquin Valley: inside a city (or an unincorporated area surrounded by one)"))] = None,
        demolition: Annotated[bool, Field(description="Building demolition is part of the work")] = False,
        near_sensitive_receptors: Annotated[bool, Field(description=(
            "Near homes, schools or other sensitive uses (Bay Area enhanced measures)"))] = False,
        wind_gust_mph: Annotated[Optional[float], Field(ge=0, le=200, description="Current wind gusts, mph")] = None,
        average_wind_mph: Annotated[Optional[float], Field(ge=0, le=200, description="Current average wind, mph")]
        = None,
        single_family_addition: Annotated[bool, Field(description="Addition to an existing single-family home")]
        = False,
        existing_building_sqft: Annotated[Optional[float], Field(**nn, description=(
            "Existing building area, sq ft (remodel/addition exemption)"))] = None,
        added_building_sqft: Annotated[Optional[float], Field(**nn, description="Area being added, sq ft")] = None,
        site_acres: Annotated[num, Field(description=(
            "Total construction site area (San Joaquin Valley 48-hour notice and rural track-out tests use it)"))]
        = None,
        paved_interior_roads: Annotated[Optional[bool], Field(description=(
            "San Joaquin Valley: the site has paved interior roads (Rule 8041 track-out device)"))] = None,
        site_elevation_ft: Annotated[num, Field(description=(
            "Site elevation in feet (San Joaquin Valley dust rules do not apply at 3,000 ft or higher)"))] = None,
        as_of: Annotated[iso, Field(description="ISO date; default today in California")] = None,
    ) -> DustResult:
        return dust_control_requirements(air_district, disturbed_acres, daily_import_export_cy, daily_earthmoving_cy,
                                         earthmoving_days_at_that_volume, project_type, earthmoving_start,
                                         coachella_valley, vehicle_trips_per_day, three_axle_trips_per_day, urban_area,
                                         demolition, near_sensitive_receptors, wind_gust_mph, average_wind_mph,
                                         single_family_addition, existing_building_sqft, added_building_sqft,
                                         site_acres, paved_interior_roads, site_elevation_ft, as_of)

    @mcp.tool(name="portable_engine_requirements", annotations=ro, description=docs["perp"])
    def _portable_engine_requirements(
        engine_bhp: Annotated[Union[float, str], Field(description="Engine rated brake horsepower, e.g. 300")],
        engine_tier: Annotated[Optional[str], Field(description=(
            "e.g. 'Tier 4 Final', 'Tier 4 Interim flex', 'Tier 3 flex', 'Tier 2 flex', 'Tier 3'"))] = None,
        fuel: Annotated[str, Field(description="diesel (default), gasoline, natural_gas, propane...")] = "diesel",
        propels_vehicle: Annotated[bool, Field(description="The engine moves the machine")] = False,
        auxiliary_engine_on_two_engine_vehicle: Annotated[bool, Field(description=(
            "Auxiliary engine of a two-engine vehicle such as a crane"))] = False,
        months_at_location: Annotated[Optional[float], Field(**nn, description=(
            "Consecutive months it will stay at one location"))] = None,
        on_foundation: Annotated[bool, Field(description="Attached to a foundation")] = False,
        perp_registered: Annotated[Optional[bool], Field(description="Registered in PERP now")] = None,
        registration_expires: Annotated[iso, Field(description="ISO PERP registration expiration date")] = None,
        fleet_total_bhp: Annotated[Optional[float], Field(**nn, description=(
            "Total bhp of all your portable diesel engines"))] = None,
        annual_hours: Annotated[Optional[float], Field(ge=0, le=8784, description="Hours a calendar year")] = None,
        project_registered_bhp_on_site: Annotated[Optional[float], Field(**nn, description=(
            "Total bhp of PERP-registered engines on the project site at the same time"))] = None,
        extreme_ozone_district: Annotated[Optional[bool], Field(description=(
            "The district is designated extreme non-attainment for ozone"))] = None,
        project_start: Annotated[iso, Field(description="ISO project startup date")] = None,
        engines_to_register: Annotated[int, Field(ge=0, description="Engines or pile drivers to register")] = 0,
        equipment_units_to_register: Annotated[int, Field(ge=0, description="Equipment units to register")] = 0,
        renewal: Annotated[bool, Field(description="The registration is a renewal")] = False,
        years_at_same_location: Annotated[Optional[float], Field(ge=0, le=100, description=(
            "Years it has come back to the same location (seasonal-source test)"))] = None,
        months_operated_per_year: Annotated[Optional[float], Field(ge=0, le=12, description=(
            "Months a year it runs at that location (seasonal-source test)"))] = None,
        special_category: Annotated[Optional[str], Field(description=(
            "Engines outside the Portable Engine ATCM: agricultural_source, emergency_only (used only to relieve a "
            "threat to public health and safety in an emergency), hazardous_location_tier3, military_tactical or "
            "harbor_craft"))] = None,
        as_of: Annotated[iso, Field(description="ISO date; default today in California")] = None,
    ) -> PortableEngineResult:
        return portable_engine_requirements(engine_bhp, engine_tier, fuel, propels_vehicle,
                                            auxiliary_engine_on_two_engine_vehicle, months_at_location, on_foundation,
                                            perp_registered, registration_expires, fleet_total_bhp, annual_hours,
                                            project_registered_bhp_on_site, extreme_ozone_district, project_start,
                                            engines_to_register, equipment_units_to_register, renewal,
                                            years_at_same_location, months_operated_per_year, special_category, as_of)
