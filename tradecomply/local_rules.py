"""City jobsite rules for California construction: work hours, street and right-of-way permits, dumpsters in the
street, haul routes, demolition notices and construction & demolition (C&D) debris.

  construction_hours           city + date/time (+ kind of work) -> allowed now? the day's windows, the next time
                               work is allowed, the permit route (San Diego Construction Noise Permit with dated
                               steps; Los Angeles citywide and hillside HCR hours) and the penalty
  local_jobsite_requirements   city + project facts -> the city permits, notices and dated deadlines for street /
                               right-of-way work, a dumpster or storage container in the street, night work, trenching,
                               hillside earthwork (Los Angeles haul routes) and demolition, with the fees
  construction_debris_plan     city + project type and size (+ tonnage) -> C&D deposit (San Diego, with the math),
                               diversion target and shortfall, refund outcome and deadline, hauler and facility rules,
                               landfill cost
  san_diego_right_of_way_fees  blocks, weeks, meters, dumpster or storage container (+ noise permit) -> the
                               IB 177 / IB 718 fee estimate line by line

Cities: San Diego and Los Angeles were checked against sandiego.gov and dbs.lacity.gov. San Francisco, San Jose,
Sacramento, Oakland, Long Beach and Fresno rest on search excerpts only (their official hosts could not be read), so
every answer that uses them has verified=False and says so.

Every legal number, time, date and threshold is read from the rulebook with core.rule(id).param(key) at call time,
and every rule a result relies on is cited through core.Result.cite(), so `verified` is True only when all of them
are verified. The pack is research/web_local-jobsite.verified.json (rule ids "local-..."), plus three statewide or
federal rules from other packs (CALGreen 65% diversion, the 811 two-working-day notice, the NESHAP 10-working-day
demolition notice).

Not legal values (so not in the rulebook): the city, work-type and project-type spellings below, which only map a
user's words onto the rules, and the calendar convention that a working or business day is Monday-Friday less the
City holidays the rulebook lists (San Diego does not define the terms; the result says so where it is used).
"""
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field

from . import core, logic

# ---------------------------------------------------------------- rule ids
# San Diego construction noise (SDMC 59.5.0404 and the DSD Construction Noise Permit)
SD_BAN, SD_WINDOWS, SD_75DB, SD_EMERG = (
    "local-sd-construction-noise-night-sunday-holiday-ban", "local-sd-noise-permit-required-windows",
    "local-sd-construction-noise-75db-daytime", "local-sd-construction-noise-emergency-48h")
SD_NP_COND, SD_HOLIDAYS, SD_NP_FEE, SD_NP_NOTICE = (
    "local-sd-noise-permit-conditions", "local-sd-city-holidays-2026", "local-sd-noise-permit-fee",
    "local-sd-noise-permit-neighbor-notice-72h-500ft")
SD_NP_DS3073, SD_NP_POST, SD_NP_TCP_FIRST, SD_NP_5WD = (
    "local-sd-noise-permit-notification-list-ds3073", "local-sd-noise-permit-site-posting",
    "local-sd-noise-permit-requires-traffic-control-permit-first", "local-sd-noise-permit-apply-5-working-days")
SD_NP_HOLIDAY, SD_NP_EXT, SD_NP_REVOKE, SD_PENALTY = (
    "local-sd-noise-permit-holiday-3-business-days", "local-sd-noise-permit-extension-48h",
    "local-sd-noise-permit-revocation", "local-sd-construction-noise-civil-penalty")
SD_DELIVERY, SD_REFUSE, SD_ZONE_TABLE = ("local-sd-deliveries-before-7am-penalized",
                                         "local-sd-refuse-vehicle-hours-residential",
                                         "local-sd-noise-one-hour-limits-table")
# San Diego Traffic Control Permit (IB 177, DS-269)
SD_TCP_REQ, SD_TCP_15BD, SD_TCP_5WD, SD_TCP_HOURS = (
    "local-sd-traffic-control-permit-required", "local-sd-tcp-processing-15-business-days",
    "local-sd-tcp-apply-5-working-days", "local-sd-tcp-standard-work-hours")
SD_TCP_NO_WKND, SD_TCP_FEES, SD_TCP_EXT, SD_TCP_NOTIFY = (
    "local-sd-tcp-no-weekend-holiday-work", "local-sd-tcp-fees", "local-sd-tcp-extension-5-working-days",
    "local-sd-tcp-agency-notifications-5-working-days")
SD_TCP_SIGNS, SD_TCP_NO_PERMIT, SD_TCP_MUTCD, SD_TCP_TRAINED = (
    "local-sd-tcp-no-parking-signs-72h", "local-sd-tcp-work-without-permit", "local-sd-tcp-plan-ca-mutcd-2026",
    "local-sd-tcp-trained-traffic-control-workers")
SD_TCP_INS, SD_TCP_PW, SD_TCP_TRENCH, SD_TCP_PED = (
    "local-sd-tcp-insurance-1m-additional-insured", "local-sd-tcp-prevailing-wage-statement",
    "local-sd-tcp-trench-plates-daily-cleanup", "local-sd-tcp-pedestrian-access-ada")
SD_TCP_LANES, SD_TCP_SIGNAL, SD_PRECON = ("local-sd-tcp-lane-widths", "local-sd-tcp-traffic-signal-work-5-days",
                                          "local-sd-row-preconstruction-meeting")
# San Diego dumpsters and storage containers in the street (IB 718)
SD_DUMP, SD_DUMP_CLEAR, SD_DUMP_3WD, SD_DUMP_SIGNS, SD_STORAGE_7D = (
    "local-sd-dumpster-in-row-permit-and-fees", "local-sd-dumpster-placement-clearances",
    "local-sd-dumpster-3-working-days-notice", "local-sd-dumpster-no-parking-signs-72h",
    "local-sd-storage-container-7-days")
# San Diego Right-of-Way permit, street preservation, monuments
SD_ROW_REQ, SD_ROW_TERM, SD_ROW_EXT60, SD_EMRA = (
    "local-sd-row-permit-required", "local-sd-row-permit-term-2-years-2-extensions",
    "local-sd-row-extension-window-60-days", "local-sd-emra-private-encroachment")
SD_MONUMENT, SD_MORATORIUM, SD_MORATORIUM_DEF, SD_OVERLAY, SD_RESTORE = (
    "local-sd-survey-monument-7-day-notice", "local-sd-street-moratorium-waiver",
    "local-sd-moratorium-street-definition", "local-sd-moratorium-overlay-180-working-days",
    "local-sd-trench-final-restoration-180-days")
# San Diego C&D debris and Miramar Landfill
SD_CD_APPLIES, SD_CD_DEPOSIT, SD_CD_EXEMPT, SD_CD_WMF = (
    "local-sd-cd-deposit-ordinance-applies", "local-sd-cd-deposit-amounts", "local-sd-cd-exemptions",
    "local-sd-cd-wmf-and-deposit-before-permit")
SD_CD_65, SD_CD_REFUND, SD_CD_PRORATE, SD_CD_FACILITIES = (
    "local-sd-cd-diversion-65-percent", "local-sd-cd-refund-180-days", "local-sd-cd-refund-prorated",
    "local-sd-cd-certified-facilities-for-refund")
SD_CD_HAULER, SD_FRANCHISE_BIZ = ("local-sd-cd-franchised-hauler-recommended-self-haul-allowed",
                                  "local-sd-franchise-hauler-business-service")
SD_MIRAMAR_COVER, SD_MIRAMAR_RATES, SD_MIRAMAR_FILL = (
    "local-sd-miramar-uncovered-load-fee", "local-sd-miramar-cd-disposal-rates", "local-sd-miramar-clean-fill-preapproval")
# Los Angeles (LADBS)
LA_HOURS, LA_HOLIDAY, LA_PENALTY, LA_HCR, LA_HCR_LIGHTS = (
    "local-la-construction-hours", "local-la-national-holiday-hours", "local-la-hours-violation-misdemeanor",
    "local-la-hcr-hillside-hours", "local-la-hcr-no-floodlights-after-6pm")
LA_HAUL_HEARING, LA_HAUL_8WK, LA_HAUL_FEE, LA_HAUL_CONTENTS = (
    "local-la-haul-route-hearing-1000cy-hillside", "local-la-haul-route-processing-8-weeks",
    "local-la-haul-route-filing-fee", "local-la-haul-route-application-contents")
LA_HAUL_OWNERS, LA_HAUL_CEQA, LA_HAUL_12_18, LA_HILLSIDE_NOTICE = (
    "local-la-haul-route-owner-list-6-months", "local-la-haul-route-ceqa-first", "local-la-haul-route-12-and-18-months",
    "local-la-hillside-1000cy-neighbor-notice-10-days")
LA_EO1, LA_HAUL_CONDITIONS, LA_BIRD = ("local-la-eo1-haul-route-ministerial", "local-la-approved-haul-route-conditions",
                                       "local-la-bird-streets-haul-requests-mondays")
LA_FLAGGERS, LA_MATERIALS, LA_SWEEP = ("local-la-flaggers-and-no-peak-hour-closures",
                                       "local-la-materials-in-row-boss-permit", "local-la-streets-swept-concrete-spills")
LA_CD_PROC, LA_CD_HAULER, LA_PERMIT_HAULER = ("local-la-cd-certified-processors-only",
                                              "local-la-cd-private-waste-hauler-permit",
                                              "local-la-building-permit-requires-listed-hauler")
LA_DEMO_45, LA_DEMO_PLACARD, LA_DEMO_LICENSE, LA_DEMO_INSP, LA_DEMO_SEWER = (
    "local-la-demolition-45-year-notice-30-days", "local-la-demolition-placard", "local-la-demolition-permit-c21-or-a",
    "local-la-demolition-inspection-24h", "local-la-demolition-sewer-cap-and-lateral-support")
LA_500FT_U, LA_AFTER_HOURS_U, LA_EQUIP_U, LA_BOE_U, LA_PODS_U = (
    "local-la-41-40-residential-500ft-unverified", "local-la-41-40-after-hours-permission-unverified",
    "local-la-112-05-equipment-75dba-unverified", "local-la-boe-a-b-permits-unverified",
    "local-la-pods-prohibited-public-property-unverified")
# statewide covered-load rules (Vehicle Code; statute text not read)
VC23115_U, VC23114_U = "local-ca-vc23115-refuse-loads-totally-covered", "local-ca-vc23114-covered-loads-unverified"
# other cities (all unverifiable)
SF_EQUIP_U, SF_NIGHT_U, SF_NIGHT_COND_U = ("local-sf-equipment-80dba-at-100ft-unverified",
                                           "local-sf-night-work-8pm-7am-permit-unverified",
                                           "local-sf-night-noise-permit-conditions-unverified")
SF_TRANSPORTER_U, SF_STREET_SPACE_U, SF_DEBRIS_BOX_U = ("local-sf-cd-transporter-permit-unverified",
                                                        "local-sf-street-space-permit-unverified",
                                                        "local-sf-debris-box-timing-unverified")
SJ_HOURS_U, SJ_STREET_U, SJ_CDDD_U, SJ_HAULERS_U, SJ_ENCROACH_U = (
    "local-sj-construction-hours-unverified", "local-sj-street-work-hours-unverified", "local-sj-cddd-deposit-unverified",
    "local-sj-authorized-cd-haulers-only-unverified", "local-sj-revocable-encroachment-permit-unverified")
SAC_HOURS_U, SAC_URGENT_U, SAC_CD_U, SAC_HAULER_U, SAC_FEE_U = (
    "local-sac-construction-noise-exemption-hours-unverified", "local-sac-after-hours-urgent-necessity-3-days-unverified",
    "local-sac-cd-65-percent-waste-log-unverified", "local-sac-mixed-cd-franchised-hauler-only-unverified",
    "local-sac-green-halo-admin-fee-unverified")
OAK_CODE_U, OAK_COA_U, OAK_CD_U, OAK_HAULER_U, OAK_WRRP_U = (
    "local-oak-construction-noise-code-windows-unverified", "local-oak-standard-coa-hours-unverified",
    "local-oak-cd-100-asphalt-concrete-65-rest-unverified", "local-oak-cd-nef-haulers-only-unverified",
    "local-oak-wrrp-required-unverified")
LB_HOURS_U, LB_AFTER_U, LB_CD_U, LB_DEPOSIT_U = ("local-lb-construction-hours-unverified",
                                                 "local-lb-after-hours-permit-unverified",
                                                 "local-lb-cd-65-percent-unverified", "local-lb-cd-deposit-unverified")
LB_HAULER_U, LB_DOCS_U, LB_BIN_U = ("local-lb-cd-permitted-hauler-or-self-haul-unverified",
                                    "local-lb-cd-documentation-90-days-unverified",
                                    "local-lb-bin-in-street-temporary-occupancy-permit-unverified")
FRE_HOURS_U, FRE_HAULERS_U, FRE_FEE_U = ("local-fre-construction-hours-unverified", "local-fre-nero-haulers-only-unverified",
                                         "local-fre-nero-franchise-fee-and-window-unverified")
# statewide / federal rules from other packs
CALGREEN, USA811, NESHAP = ("calgreen-construction-waste-65-percent", "excavation-usa-notice-2-working-days",
                            "neshap-notification-10-working-days")

# Pack rules no tool relies on, and why (kept here so the test can prove every other pack rule is used).
NOT_USED = {
    SD_ZONE_TABLE: "zone limits for non-construction noise; construction noise is governed by 59.5.0404",
    SD_FRANCHISE_BIZ: "ongoing trash service at a business (e.g. a contractor's yard), not a jobsite",
    LA_EQUIP_U: "unverified equipment dBA limit; no tool measures equipment noise",
    LA_BIRD: "one neighborhood's haul-request window (Bird Streets, CD-4)",
    FRE_FEE_U: "franchise fee owed by roll-off haulers, not contractors",
    VC23114_U: "general load-securement statute (unverified); VC 23115 is the debris-specific rule used",
}

SD, LA, SF, SJ, SAC, OAK, LB, FRE = ("san_diego", "los_angeles", "san_francisco", "san_jose", "sacramento", "oakland",
                                     "long_beach", "fresno")
VERIFIED_CITIES = (SD, LA)
_CITY_RULE = {SD: SD_BAN, LA: LA_HOURS, SF: SF_NIGHT_U, SJ: SJ_HOURS_U, SAC: SAC_HOURS_U, OAK: OAK_CODE_U,
              LB: LB_HOURS_U, FRE: FRE_HOURS_U}

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY = 24 * 60

Status = Literal["allowed", "needs_permit", "not_allowed", "restricted", "check_holiday"]
WorkType = Literal["construction", "interior", "delivery", "street_work", "bin_service"]


# ---------------------------------------------------------------- helpers
def P(rule_id: str, key: str):
    return core.rule(rule_id).param(key)


def _n(x: float) -> str:
    """Number for display: thousands separators, at most 2 decimals, no trailing zeros."""
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _money(x: float) -> float:
    """Round half up to cents (fees are never rounded down by float noise)."""
    return math.floor(x * 100 + 0.5 + 1e-9) / 100


def _join(items, last: str = "and") -> str:
    items = [str(i) for i in items]
    if len(items) <= 2:
        return f" {last} ".join(items)
    return ", ".join(items[:-1]) + f", {last} " + items[-1]


def _words(text) -> str:
    """Lowercase, accents and punctuation removed, single spaces: 'San José, CA' -> 'san jose ca'."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _m(hhmm: str) -> int:
    h, mm = str(hhmm).split(":")
    return int(h) * 60 + int(mm)


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clock(minutes: int) -> str:
    """07:00 -> '7 a.m.', 19:30 -> '7:30 p.m.', 0 -> 'midnight', 720 -> 'noon'."""
    minutes %= DAY
    if minutes == 0:
        return "midnight"
    if minutes == 720:
        return "noon"
    h, mm = divmod(minutes, 60)
    suffix = "a.m." if h < 12 else "p.m."
    h12 = h % 12 or 12
    return f"{h12}{':%02d' % mm if mm else ''} {suffix}"


def _num(name: str, value, lo: Optional[float] = None, hi: Optional[float] = None,
         integer: bool = False) -> Optional[float]:
    """A number, or a string like '2,500 cy', '3 blocks', '$450,000'; None or '' stays None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = re.match(r"^\s*\$?\s*(-?\d*\.?\d+)\s*([a-z .'/%]*)$", str(value).replace(",", "").lower())
        if not m:
            raise ValueError(f"{name} must be a number, got {value!r}")
        v = float(m.group(1))
    if not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    if lo is not None and v < lo:
        raise ValueError(f"{name} cannot be below {lo:g}, got {value!r}")
    if hi is not None and v > hi:
        raise ValueError(f"{name} cannot be above {hi:g}, got {value!r}")
    if integer and v != int(v):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return v


def _date(value, field: str) -> Optional[date]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be an ISO date like 2026-12-07, got {value!r}") from None


_DT = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T]+(\d{1,2})(?::(\d{2}))?(?::\d{2}(?:\.\d+)?)?\s*"
                 r"(a\.?m\.?|p\.?m\.?)?\s*(z|[+-]\d{2}:?\d{2})?)?$", re.I)


def _when(value, field: str = "when") -> tuple[date, Optional[int]]:
    """ISO date or date-time in California local time -> (date, minutes after midnight or None for a date only).
    '2026-11-27T06:30', '2026-11-27 6:30 pm', '2026-11-27T14:30Z' (converted to Pacific time), '2026-11-27'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        now = datetime.now(logic.CA_TZ)
        return now.date(), now.hour * 60 + now.minute
    if isinstance(value, datetime):
        dt = value.astimezone(logic.CA_TZ) if value.tzinfo else value
        return dt.date(), dt.hour * 60 + dt.minute
    if isinstance(value, date):
        return value, None
    s = str(value).strip()
    m = _DT.match(s)
    if not m:
        raise ValueError(f"{field} must be an ISO date or date-time in California time, like 2026-11-27T06:30, "
                         f"'2026-11-27 6:30 pm' or 2026-11-27; got {value!r}")
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        raise ValueError(f"{field} has an impossible date: {value!r}") from None
    if m.group(2) is None:
        return d, None
    h, mm, ap, tz = int(m.group(2)), int(m.group(3) or 0), (m.group(4) or "").lower(), m.group(5)
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f"{field}: an a.m./p.m. hour must be 1-12, got {value!r}")
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    if h > 23 or mm > 59:
        raise ValueError(f"{field} has an impossible time: {value!r}")
    if tz:
        off = "+00:00" if tz.lower() == "z" else (tz if ":" in tz else tz[:3] + ":" + tz[3:])
        dt = datetime.fromisoformat(f"{d.isoformat()}T{h:02d}:{mm:02d}{off}").astimezone(logic.CA_TZ)
        return dt.date(), dt.hour * 60 + dt.minute
    return d, h * 60 + mm


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


# ---------------------------------------------------------------- city names (spelling only, not law)
_ALIASES = {
    SD: {"san diego", "sd", "s d", "sandiego", "cosd", "city of sd", "sdca"},
    LA: {"los angeles", "la", "l a", "lacity", "city of la", "los angles", "losangeles", "ladbs"},
    SF: {"san francisco", "sf", "s f", "san fran", "sanfrancisco"},
    SJ: {"san jose", "sj", "s j", "sanjose"},
    SAC: {"sacramento", "sac", "sacto", "sac town", "sactown"},
    OAK: {"oakland"},
    LB: {"long beach", "lb", "longbeach"},
    FRE: {"fresno"},
}


def _city_name(key: str) -> str:
    return P(_CITY_RULE[key], "city")


def normalize_city(city) -> str:
    """'SD', 'City of San Diego', 'san diego, ca' -> 'san_diego'; 'L.A.' -> 'los_angeles'. Counties and cities
    without rules raise a ValueError that says what is covered."""
    raw = str(city or "").strip()
    t = _words(raw)
    if not t:
        raise ValueError("city is required, e.g. 'San Diego' or 'Los Angeles' (the city the jobsite is in)")
    for _ in range(2):
        t = re.sub(r"\s+(?:ca|calif|california|usa|us)$", "", t).strip()
    t = re.sub(r"^the\s+", "", t)
    county = bool(re.search(r"\bcounty\b|\bunincorporated\b", t))
    t = re.sub(r"^(?:city|town)\s+of\s+", "", t)
    t = re.sub(r"\s+city$", "", t) if t not in ("la city",) else "la"
    if county:
        core_name = re.sub(r"\b(?:county|of|unincorporated|area)\b", " ", t)
        core_name = re.sub(r"\s+", " ", core_name).strip()
        hit = next((k for k, names in _ALIASES.items() if core_name in names), None)
        where = f"{_city_name(hit)} County" if hit else raw
        raise ValueError(f"{raw!r} is a county. These are City rules and apply only inside city limits; "
                         f"unincorporated {where} follows county rules, which are not in the rulebook. Give the "
                         f"city the jobsite is in.")
    for key, names in _ALIASES.items():
        if t in names:
            return key
    covered = ", ".join(_city_name(k) for k in VERIFIED_CITIES)
    others = ", ".join(_city_name(k) for k in _ALIASES if k not in VERIFIED_CITIES)
    close = get_close_matches(t, [n for names in _ALIASES.values() for n in names if len(n) > 3], n=1, cutoff=0.8)
    hint = ""
    if close:
        hint = f" Did you mean {_city_name(next(k for k, v in _ALIASES.items() if close[0] in v))}?"
    raise ValueError(f"No city jobsite rules for {raw!r} in the rulebook.{hint} Covered: {covered} (verified) and "
                     f"{others} (unverified). For other cities, check the city's municipal code and building "
                     f"department.")


# ---------------------------------------------------------------- results
class Requirement(BaseModel):
    item: str
    detail: str
    due: Optional[str] = Field(None, description="ISO date to act by (on or before)")
    due_time: Optional[str] = Field(None, description="Clock time on the due date (HH:MM), when it matters")
    estimate: bool = Field(False, description="True when the date comes from a city processing-time estimate, not a "
                                              "legal minimum")
    rule_ids: list[str] = Field(default_factory=list)
    citation: str = ""
    verified: bool = False


class Deadline(BaseModel):
    what: str
    due: str
    due_time: Optional[str] = None
    days_left: int
    status: Literal["upcoming", "due_today", "passed"]
    estimate: bool = Field(False, description="True for a recommended date from a processing-time estimate")
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class FeeLine(BaseModel):
    item: str
    amount_usd: float
    math: str
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = False


def _cit(ids) -> str:
    return "; ".join(dict.fromkeys(core.rule(i).citation_text or i for i in ids))


def _verified(ids) -> bool:
    return bool(ids) and all(core.rule(i).verified for i in ids)


class _Acc:
    """Collects, for one call, the rules relied on, the requirements, fees, math and notes."""

    def __init__(self, today: date):
        self.today = today
        self.ids: list[str] = []
        self.reqs: list[Requirement] = []
        self.fees: list[FeeLine] = []
        self.math: list[str] = []
        self.notes: list[str] = []

    def use(self, *ids: str) -> list[str]:
        for i in ids:
            core.rule(i)                                    # unknown id -> KeyError, never a silent miss
            if i not in self.ids:
                self.ids.append(i)
        return list(ids)

    def req(self, item: str, detail: str, *ids: str, due: Optional[date] = None,
            due_time: Optional[int] = None, estimate: bool = False) -> Requirement:
        self.use(*ids)
        ids = list(dict.fromkeys(ids))
        r = Requirement(item=item, detail=detail, due=due.isoformat() if due else None,
                        due_time=_hm(due_time) if due is not None and due_time is not None else None,
                        estimate=estimate and due is not None, rule_ids=ids, citation=_cit(ids),
                        verified=_verified(ids))
        self.reqs.append(r)
        return r

    def fee(self, item: str, amount: float, math_text: str, *ids: str) -> FeeLine:
        self.use(*ids)
        f = FeeLine(item=item, amount_usd=_money(amount), math=math_text, rule_ids=list(ids), verified=_verified(ids))
        self.fees.append(f)
        return f

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def deadlines(self) -> list[Deadline]:
        out = []
        for r in self.reqs:
            if not r.due:
                continue
            d = date.fromisoformat(r.due)
            left = (d - self.today).days
            out.append(Deadline(what=r.item, due=r.due, due_time=r.due_time, days_left=left,
                                status="passed" if left < 0 else "due_today" if left == 0 else "upcoming",
                                estimate=r.estimate, rule_ids=r.rule_ids, verified=r.verified))
        return sorted(out, key=lambda x: (x.due, x.due_time or ""))

    def finish(self, res):
        fields = type(res).model_fields
        if "requirements" in fields:
            res.requirements = self.reqs
        if "deadlines" in fields:
            res.deadlines = self.deadlines()
        if "fees" in fields and not res.fees:
            res.fees = self.fees
        res.notes = self.notes + [n for n in res.notes if n not in self.notes]
        res.math = self.math + [m for m in res.math if m not in self.math]
        res.cite(*self.ids)
        res.unverified_rule_ids = [i for i in self.ids if not core.rule(i).verified]
        for i in res.unverified_rule_ids:
            r = core.rule(i)
            res.notes.append(f"NOT VERIFIED ({r.status}): {r.title} [{r.citation_text}]. "
                             + (r.notes or "The primary text could not be read.")
                             + " Confirm with the city before relying on it.")
        return res


class _Base(core.Result):
    city: str = ""
    city_rules_verified: bool = Field(False, description="True for San Diego and Los Angeles, whose official pages "
                                                         "were read; False for the cities resting on search excerpts")
    summary: str = ""
    next_step: str = ""
    math: list[str] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- calendar (San Diego City holidays)
def _sd_holidays() -> dict[date, str]:
    return {date.fromisoformat(h["date"]): h["name"] for h in P(SD_HOLIDAYS, "holidays")}


def _sd_list_years() -> set[int]:
    return {int(P(SD_HOLIDAYS, "year"))}


class _Cal:
    """Working/business days: Monday-Friday less the City holidays the rulebook lists. Records the years for which
    no list is published, so the result can say that a holiday there would move a date earlier."""

    def __init__(self, city: str):
        self.city = city
        self.hol = _sd_holidays() if city == SD else {}
        self.years = _sd_list_years() if city == SD else set()
        self.unknown_years: set[int] = set()
        self.skipped: list[date] = []

    def workday(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        if self.city == SD and d.year not in self.years:
            self.unknown_years.add(d.year)
        if d in self.hol:
            if d not in self.skipped:
                self.skipped.append(d)
            return False
        return True

    def before(self, d: date, n: int) -> date:
        """The day n working days before d (d itself not counted): submit on or before it."""
        cur, left = d, int(n)
        while left > 0:
            cur -= timedelta(days=1)
            if self.workday(cur):
                left -= 1
        return cur

    def explain(self) -> str:
        bits = ["working/business days counted Monday-Friday"]
        if self.city == SD:
            bits.append("skipping the City holidays on the City's published list (DSD does not define the term)")
            if self.skipped:
                bits.append("skipped: " + ", ".join(f"{d.isoformat()} {self.hol[d]}" for d in sorted(self.skipped)))
            if self.unknown_years:
                bits.append("no City holiday list is published for " + ", ".join(map(str, sorted(self.unknown_years)))
                            + ", so a holiday then would make the date earlier")
        return "; ".join(bits)


def _weekdays_before(d: date, n: int) -> date:
    cur, left = d, int(n)
    while left > 0:
        cur -= timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


def _before_hours(start: date, start_min: Optional[int], hours: int) -> tuple[date, Optional[int]]:
    """`hours` before the start: exact when the start time is known; otherwise the date hours // 24 days earlier
    (act by the same clock time as the start; the due_time is left open)."""
    if start_min is None:
        return start - timedelta(days=hours // 24), None
    dt = datetime(start.year, start.month, start.day) + timedelta(minutes=start_min) - timedelta(hours=hours)
    return dt.date(), dt.hour * 60 + dt.minute


# ================================================================ 1. construction hours
class Window(BaseModel):
    start: str = Field(description="HH:MM")
    end: str = Field(description="HH:MM; 24:00 is midnight at the end of the day")
    status: Status
    reason: str


class ConstructionHoursResult(_Base):
    date: str
    time: Optional[str] = Field(None, description="HH:MM checked (None when only a date was given)")
    day_of_week: str
    work_type: WorkType
    hcr_district: bool = False
    holiday: Optional[bool] = Field(None, description="True/False when known; None when the date's holiday status "
                                                      "could not be determined")
    holiday_name: Optional[str] = None
    holiday_basis: str = ""
    status: Status = Field(description=(
        "allowed: no permit needed for that time | needs_permit: only with a city permit or approval granted "
        "beforehand | not_allowed: prohibited | restricted: allowed only if quiet / within noise limits | "
        "check_holiday: the answer depends on whether the date is a holiday"))
    allowed: Optional[bool] = Field(None, description="True allowed, False needs a permit or not allowed, None for "
                                                      "restricted / check_holiday")
    allowed_windows: list[str] = Field(default_factory=list, description="Times that day needing no permit")
    day_schedule: list[Window] = Field(default_factory=list)
    next_allowed_start: Optional[str] = Field(None, description="Next date-time (ISO, Pacific) with status allowed")
    penalty: Optional[str] = None
    requirements: list[Requirement] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)


_Span = tuple  # (start_min, end_min, status, reason, rule_ids)


def _work_type(value) -> str:
    w = _words(value or "construction")
    if w in ("construction", "interior", "delivery", "street work", "bin service"):
        return w.replace(" ", "_")
    if re.search(r"\b(dumpster|roll ?off|bin|bins|debris box|refuse|trash|garbage|pickup|pick up|swap|haul away)\b", w):
        return "bin_service"
    if re.search(r"\b(deliver\w*|concrete truck|ready mix|material drop|drop off|supplier)\b", w):
        return "delivery"
    if re.search(r"\b(right of way|row|street|streets|sidewalk|lane|traffic control|alley|road|roadway|curb)\b", w):
        return "street_work"
    if re.search(r"\b(interior|inside|indoor|indoors)\b", w):
        return "interior"
    if re.search(r"\b(construct\w*|demo\w*|excavat\w*|grading|framing|build\w*|repair|alter\w*|noisy|work|"
                 r"jackhammer|saw\w*|pour\w*|roofing|concrete|pile|drill\w*)\b", w):
        return "construction"
    raise ValueError("work_type must be construction (default), interior, delivery, street_work (work in the public "
                     f"right-of-way) or bin_service (dumpster swap or refuse pickup); got {value!r}")


def _sd_holiday_status(d: date) -> tuple[Optional[bool], Optional[str]]:
    if d.year in _sd_list_years():
        name = _sd_holidays().get(d)
        return name is not None, name
    return None, None


def _sd_construction(d: date, hol: bool, hol_name: Optional[str]) -> list:
    win = P(SD_WINDOWS, "no_noise_permit_window")
    ids = [SD_BAN, SD_WINDOWS]
    day = WEEKDAYS[d.weekday()]
    if hol:
        return [(0, DAY, "needs_permit", f"{hol_name or 'City holiday'}: a Construction Noise Permit is needed for "
                 "construction all day on City holidays", ids + [SD_HOLIDAYS])]
    if day not in win["days"]:
        return [(0, DAY, "needs_permit", f"{day.title()}: a Construction Noise Permit is needed for construction all "
                 "day", ids)]
    s, e = _m(win["start"]), _m(win["end"])
    return [(0, s, "needs_permit", f"before {_clock(s)}: a Construction Noise Permit is needed", ids),
            (s, e, "allowed", f"{_clock(s)}-{_clock(e)} Monday-Saturday: no noise permit needed (the "
             f"{P(SD_75DB, 'limit_db')} dB {P(SD_75DB, 'period.hours')}-hour average limit at residential property "
             "lines still applies)", ids + [SD_75DB]),
            (e, DAY, "needs_permit", f"after {_clock(e)}: a Construction Noise Permit is needed", ids)]


def _sd_delivery(d, hol, hol_name):
    cut = _m(P(SD_DELIVERY, "noisy_arrival_or_staging_before"))
    out = []
    for s, e, st, why, ids in _sd_construction(d, hol, hol_name):
        if st == "allowed":
            out.append((s, e, "allowed", "inside the construction hours; no delivery-hour limit", ids))
        else:
            extra = (f" Noisy delivery trucks arriving or staging near homes before {_clock(cut)} are named by the City "
                     "as a penalty trigger.") if s < cut else ""
            out.append((s, e, "restricted", f"restricted construction-noise time ({why.split(':')[0]}): the project "
                        "answers for noise its contractors and suppliers make on and off the site; deliveries must "
                        f"not create excessive noise.{extra}", ids + [SD_DELIVERY, SD_PENALTY]))
    return out


def _sd_bin(d, residential: bool):
    r = P(SD_REFUSE, "refuse_vehicle_restricted")
    s, e = _m(r["end"]), _m(r["start"])                     # allowed 06:00-19:00
    if not residential:
        return [(0, DAY, "allowed", "SDMC 59.5.0406 limits refuse vehicles only in residential areas", [SD_REFUSE])]
    why = (f"refuse compacting, processing or collection vehicles may not operate {_clock(e)}-{_clock(s)} in a "
           "residential area without a permit from the Noise Abatement Administrator")
    return [(0, s, "needs_permit", why, [SD_REFUSE]),
            (s, e, "allowed", f"{_clock(s)}-{_clock(e)}: refuse vehicles may operate", [SD_REFUSE]),
            (e, DAY, "needs_permit", why, [SD_REFUSE])]


def _sd_street(d, hol, hol_name):
    day_w = P(SD_TCP_HOURS, "standard_day_work")
    night_w = P(SD_TCP_HOURS, "standard_night_work")
    ds, de = _m(day_w["start"]), _m(day_w["end"])
    ns, ne = _m(night_w["start"]), _m(night_w["end"])
    ids = [SD_TCP_HOURS, SD_TCP_NO_WKND, SD_TCP_REQ]
    noise = [SD_BAN, SD_WINDOWS]
    day = WEEKDAYS[d.weekday()]
    prev = WEEKDAYS[(d.weekday() - 1) % 7]
    off_days = P(SD_TCP_NO_WKND, "no_work_without_approval")          # saturday, sunday, holiday
    night_tail = (f"standard night work ({_clock(ns)}-{_clock(ne)}, nights starting "
                  f"{_join([n.title() for n in night_w['nights_starting']])}): allowed only if the TCP lists night "
                  "hours AND you hold a Construction Noise Permit")
    approval = "outside the standard Traffic Control hours: only if approved on the permit"
    out = []
    # early morning: the tail of the previous night's shift
    early = "needs_permit"
    early_why = (night_tail if prev in night_w["nights_starting"] and not hol else
                 approval + " (and a Construction Noise Permit before 7 a.m.)")
    out.append((0, ne, early, early_why, ids + noise))
    if hol or day in off_days:
        label = (hol_name or "City holiday") if hol else day.title()
        out.append((ne, ns, "needs_permit", f"{label}: no right-of-way work on Saturdays, Sundays or holidays unless "
                    "approved (Sundays and holidays also need a Construction Noise Permit)", ids + noise))
    else:
        out.append((ne, ds, "needs_permit", approval + " (a Construction Noise Permit too before 7 a.m.)", ids + noise))
        out.append((ds, de, "allowed", f"standard day work {_clock(ds)}-{_clock(de)} Monday-Friday (you still need "
                    "the Traffic Control Permit)", ids))
        out.append((de, ns, "needs_permit", approval + " (a Construction Noise Permit too after 7 p.m.)", ids + noise))
    if day in night_w["nights_starting"] and not hol:
        out.append((ns, DAY, "needs_permit", night_tail, ids + noise))
    else:
        out.append((ns, DAY, "needs_permit", approval + " (and a Construction Noise Permit)", ids + noise))
    return out


def _la_construction(d, hol, work, hcr):
    hrs = P(LA_HOURS, "permitted_hours")
    day = WEEKDAYS[d.weekday()]
    ids = [LA_HOURS]
    if day == "sunday":
        return [(0, DAY, "not_allowed", "Sunday: no construction or demolition permitted in Los Angeles", ids)]
    if hol:
        win, label, ids = P(LA_HOLIDAY, "national_holiday_hours"), "national holiday", ids + [LA_HOLIDAY]
    elif day == "saturday":
        win, label = hrs["saturday"], "Saturday"
    else:
        win, label = hrs["monday_friday"], "Monday-Friday"
    s, e = _m(win["start"]), _m(win["end"])
    if hcr:
        ids = ids + [LA_HCR]
        if day == "saturday":
            if work != "interior":
                return [(0, DAY, "not_allowed", "HCR hillside district on a Saturday: only interior work is allowed",
                         ids)]
            label = "Saturday (HCR: interior work only)"
        else:
            hw = P(LA_HCR, "weekday_hours")
            s, e = max(s, _m(hw["start"])), min(e, _m(hw["end"]))
            label += " in an HCR hillside district"
    return [(0, s, "not_allowed", f"before {_clock(s)} ({label})", ids),
            (s, e, "allowed", f"{_clock(s)}-{_clock(e)} {label}", ids),
            (e, DAY, "not_allowed", f"after {_clock(e)} ({label})", ids)]


def _sf(d):
    w = P(SF_NIGHT_U, "restricted_window")
    s, e = _m(w["end"]), _m(w["start"])
    lim = (f"louder than ambient + {P(SF_NIGHT_U, 'threshold_dba_over_ambient')} dBA at the nearest property line "
           "needs a special permit (Public Works for the right-of-way, Building Inspection for private property)")
    day_why = (f"daytime: powered equipment limited to {P(SF_EQUIP_U, 'max_dba')} dBA at "
               f"{P(SF_EQUIP_U, 'measured_at_ft')} ft")
    return [(0, s, "needs_permit", f"night ({_clock(e)}-{_clock(s)}): {lim}", [SF_NIGHT_U, SF_NIGHT_COND_U]),
            (s, e, "allowed", day_why, [SF_EQUIP_U]),
            (e, DAY, "needs_permit", f"night ({_clock(e)}-{_clock(s)}): {lim}", [SF_NIGHT_U, SF_NIGHT_COND_U])]


def _window_day(win: Optional[dict], ids, allowed_why: str, outside_status: str, outside_why: str):
    if not win:
        return [(0, DAY, outside_status, outside_why, ids)]
    s, e = _m(win["start"]), _m(win["end"])
    return [(0, s, outside_status, outside_why, ids), (s, e, "allowed", allowed_why.format(s=_clock(s), e=_clock(e)),
                                                       ids), (e, DAY, outside_status, outside_why, ids)]


def _other_city(city, d):
    day = WEEKDAYS[d.weekday()]
    wk = day in WEEKDAYS[:5]
    if city == SF:
        return _sf(d)
    if city == SJ:
        win = P(SJ_HOURS_U, "weekday_hours") if wk else None
        return _window_day(win, [SJ_HOURS_U], "{s}-{e} Monday-Friday", "not_allowed",
                           "outside 7 a.m.-7 p.m. Monday-Friday: not allowed unless your planning entitlement "
                           "allows it")
    if city == SAC:
        ex = P(SAC_HOURS_U, "exempt_hours")
        win = ex["sunday"] if day == "sunday" else ex["monday_saturday"]
        return _window_day(win, [SAC_HOURS_U, SAC_URGENT_U], "{s}-{e}: construction noise exempt (with working engine "
                           "silencers)", "restricted", "outside the exempt hours: the noise ordinance limits apply; "
                           "the building director may allow urgent-necessity work for up to "
                           f"{P(SAC_URGENT_U, 'max_days')} days")
    if city == OAK:
        coa = P(OAK_COA_U, "coa_hours")
        win = coa["monday_friday"] if wk else coa["saturday"] if day == "saturday" else None
        return _window_day(win, [OAK_COA_U, OAK_CODE_U], "{s}-{e}: Oakland's standard condition-of-approval hours",
                           "restricted", "outside Oakland's standard condition-of-approval hours: check your "
                           "project's conditions; the code's stricter night limits apply 7 p.m.-7 a.m. weekdays and "
                           "8 p.m.-9 a.m. weekends and federal holidays")
    if city == LB:
        ph = P(LB_HOURS_U, "permitted_hours")
        win = ph["monday_friday"] if wk else ph["saturday"] if day == "saturday" else None
        return _window_day(win, [LB_HOURS_U, LB_AFTER_U], "{s}-{e} (federal holidays count as weekdays)",
                           "needs_permit", "outside the permitted hours (and all day Sunday): only with an after-hours "
                           "permit from the Health Department Noise Control Officer for good cause")
    if city == FRE:
        win = P(FRE_HOURS_U, "exempt_hours")["monday_saturday"] if day != "sunday" else None
        return _window_day(win, [FRE_HOURS_U], "{s}-{e} Monday-Saturday: permitted construction exempt from the noise "
                           "rules", "restricted", "outside the exempt hours (and all day Sunday): the noise ordinance "
                           "limits apply")
    raise AssertionError(city)


def _spans(city, d, work, hcr, residential, hol, hol_name):
    if city == SD:
        if work == "delivery":
            return _sd_delivery(d, hol, hol_name)
        if work == "bin_service":
            return _sd_bin(d, residential)
        if work == "street_work":
            return _sd_street(d, hol, hol_name)
        return _sd_construction(d, hol, hol_name)
    if city == LA:
        spans = _la_construction(d, hol, work, hcr)
        if work == "delivery":
            spans = [(s, e, st, why + "; LAMC 41.40 reportedly covers job-site deliveries too", ids + [LA_500FT_U])
                     for s, e, st, why, ids in spans]
        if work == "street_work":
            spans = [(s, e, st, why + ("; no street closures in peak traffic hours, flaggers for delivery "
                                      "blockages" if st == "allowed" else ""), ids + [LA_FLAGGERS])
                     for s, e, st, why, ids in spans]
        return spans
    if city == SJ and work == "street_work":
        sw = P(SJ_STREET_U, "street_work_hours")
        win = sw["monday_friday"] if d.weekday() < 5 else sw["saturday_sunday"]
        return _window_day(win, [SJ_STREET_U], "{s}-{e}: San Jose street work hours", "not_allowed",
                           "outside San Jose's street work hours")
    return _other_city(city, d)


def _merge(a: list, b: list, label: str) -> list:
    """Combine the not-a-holiday schedule `a` and the holiday schedule `b`: same status stays, a difference becomes
    check_holiday."""
    cuts = sorted({0, DAY} | {x for s in a + b for x in (s[0], s[1])})
    out = []
    for lo, hi in zip(cuts, cuts[1:]):
        sa = next(s for s in a if s[0] <= lo < s[1])
        sb = next(s for s in b if s[0] <= lo < s[1])
        if sa[2] == sb[2]:
            seg = (lo, hi, sa[2], sa[3], list(dict.fromkeys(sa[4] + sb[4])))
        else:
            seg = (lo, hi, "check_holiday", f"{sa[2].replace('_', ' ')} if {label} is not a holiday ({sa[3]}); "
                   f"{sb[2].replace('_', ' ')} if it is ({sb[3]})", list(dict.fromkeys(sa[4] + sb[4])))
        if out and out[-1][2] == seg[2] and out[-1][3] == seg[3]:
            out[-1] = (out[-1][0], hi, seg[2], seg[3], list(dict.fromkeys(out[-1][4] + seg[4])))
        else:
            out.append(seg)
    return out


def _resolve_holiday(city, d, given: Optional[bool], acc: Optional[_Acc]) -> tuple[Optional[bool], Optional[str], str]:
    """(is_holiday, name, basis). San Diego uses the City's published list for the years it covers (the list wins
    over the caller); Los Angeles has no published list, so it is the caller's answer or unknown."""
    if city == SD:
        listed, name = _sd_holiday_status(d)
        if listed is not None:
            if given is not None and given != listed and acc:
                acc.note(f"You said {d.isoformat()} is {'not ' if not given else ''}a holiday, but the City's "
                         f"{d.year} holiday list {'has ' + name if listed else 'does not list it'}; the City list "
                         "is used.")
            if acc:
                acc.use(SD_HOLIDAYS)
            return listed, name, f"City of San Diego {d.year} holiday list"
        if given is not None:
            return given, None, "your answer (the City has not published a holiday list for this year)"
        return None, None, (f"unknown: the City has published holidays only for "
                            f"{', '.join(map(str, sorted(_sd_list_years())))}")
    if city == LA:
        if given is not None:
            return given, None, "your answer (LADBS does not list the national holidays)"
        return None, None, "unknown: LADBS gives national-holiday hours but does not list the holidays"
    return (given, None, "your answer") if given is not None else (None, None, "no holiday rule for this city")


def _day_spans(city, d, work, hcr, residential, given_holiday, acc=None):
    hol, name, basis = _resolve_holiday(city, d, given_holiday, acc)
    if city in (SD, LA) and hol is None and not (city == SD and work == "bin_service"):
        a = _spans(city, d, work, hcr, residential, False, None)
        b = _spans(city, d, work, hcr, residential, True, None)
        return _merge(a, b, d.isoformat()), hol, name, basis
    return _spans(city, d, work, hcr, residential, bool(hol), name), hol, name, basis


def _at(spans, t: int):
    return next(s for s in spans if s[0] <= t < s[1])


def _next_allowed(city, d, t, work, hcr, residential, today_spans) -> tuple[Optional[str], bool]:
    """First date-time at or after (d, t) with status allowed, within 14 days. Days with an unknown holiday status
    are assumed not to be holidays; the flag says whether that assumption was used."""
    assumed = False
    for s in today_spans:
        if s[2] == "allowed" and s[1] > t:
            return f"{d.isoformat()}T{_hm(max(s[0], t))}", False
    for k in range(1, 15):
        dd = d + timedelta(days=k)
        hol, _, _ = _resolve_holiday(city, dd, None, None)
        if hol is None and city in (SD, LA):
            assumed = True
        for s in _spans(city, dd, work, hcr, residential, bool(hol), None):
            if s[2] == "allowed":
                return f"{dd.isoformat()}T{_hm(s[0])}", assumed
    return None, assumed


def _sd_noise_permit_plan(acc: _Acc, cal: _Cal, start: date, start_min: Optional[int], row: bool,
                          today: date) -> None:
    """The dated steps to get and use a San Diego Construction Noise Permit for work starting on `start`."""
    fee = P(SD_NP_FEE, "fee_usd")
    acc.req("Construction Noise Permit (San Diego DSD)",
            f"Needed before construction 7 p.m.-7 a.m., on Sundays and on City holidays; it must be applied for and "
            f"granted before the work. Apply in Accela ({P(SD_WINDOWS, 'apply_via')}); fee {_usd(fee)} per "
            f"application ({P(SD_NP_FEE, 'per')}). Questions: {P(SD_WINDOWS, 'contact')}.",
            SD_WINDOWS, SD_BAN, SD_NP_FEE)
    dues = []
    if row:
        n = int(P(SD_NP_5WD, "min_working_days_before_work"))
        np_due = cal.before(start, n)
        dues.append(np_due)
        tcp_bd = int(P(SD_TCP_15BD, "typical_processing_business_days"))
        tcp_due = cal.before(np_due, tcp_bd)
        acc.req("Get the Traffic Control Permit approved first",
                f"The noise permit application needs an approved Traffic Control Permit with an approved traffic "
                f"control plan. TCPs take about {tcp_bd} business days (the City's estimate, not a legal deadline), "
                f"so submit the TCP by {tcp_due.isoformat()} ({tcp_bd} business days before the noise permit "
                "deadline).", SD_NP_TCP_FIRST, SD_TCP_15BD, due=tcp_due, estimate=True)
        acc.req("Submit the Construction Noise Permit application",
                f"In Accela, with the approved Traffic Control Permit, at least {n} working days before the work.",
                SD_NP_5WD, due=np_due)
        acc.math.append(f"Noise permit: {start.isoformat()} - {n} working days = {np_due.isoformat()}; TCP: "
                        f"{np_due.isoformat()} - {tcp_bd} business days = {tcp_due.isoformat()} ({cal.explain()})")
    else:
        acc.req("Apply early (work on private property)",
                "DSD publishes no fixed lead time for night or holiday work entirely on private property, but the "
                "permit must be granted before the work and before you hand out the 72-hour neighbor notice, so "
                "apply now. Some work entirely on private property needs no Traffic Control Permit first.",
                SD_BAN, SD_NP_TCP_FIRST)
    # A City holiday closure inside the application window (the 5 working days DSD sets for right-of-way noise
    # permits, before the work) has its own filing date: 3 business days before the closure.
    n5 = int(P(SD_NP_5WD, "min_working_days_before_work"))
    win_lo = cal.before(start, n5)
    hols = sorted(h for h in cal.hol if max(today, win_lo) <= h <= start)
    if hols:
        bd = int(P(SD_NP_HOLIDAY, "min_business_days_before_holiday_closure"))
        h = hols[0]
        h_due = cal.before(h, bd)
        if not dues or h_due < min(dues):
            dues.append(h_due)
            acc.req(f"File before the {cal.hol[h]} closure",
                    f"DSD asks for noise permit applications at least {bd} business days before a City holiday "
                    f"closure ({h.isoformat()} {cal.hol[h]}).", SD_NP_HOLIDAY, due=h_due)
            acc.math.append(f"Holiday closure {h.isoformat()} - {bd} business days = {h_due.isoformat()}")
    lead = int(P(SD_NP_NOTICE, "lead_time_hours"))
    radius = P(SD_NP_NOTICE, "radius_ft")
    n_date, n_min = _before_hours(start, start_min, lead)
    acc.req("Deliver the neighbor notice",
            f"After the permit is approved, deliver a notice at least {lead} hours before the work starts to all "
            f"{_join(P(SD_NP_NOTICE, 'recipients'), 'and')} within {_n(radius)} ft of the worksite's outer boundaries. "
            f"The City template ({P(SD_NP_NOTICE, 'notice_templates')[1]}) is guidance on what to include: "
            f"{_join(P(SD_NP_NOTICE, 'template_guidance_includes'))}.", SD_NP_NOTICE, due=n_date, due_time=n_min)
    acc.math.append(f"Neighbor notice: {start.isoformat()}" + (f" {_hm(start_min)}" if start_min is not None else "")
                    + f" - {lead} h = {n_date.isoformat()}"
                    + (f" {_hm(n_min)}" if n_min is not None else " (no start time given: deliver it by the same "
                                                                  "clock time the work will start)"))
    acc.req("Form DS-3073 and 500-ft radius map",
            f"Submit with the application: {P(SD_NP_DS3073, 'form')}, a signed list certifying you notified "
            f"{_join(P(SD_NP_DS3073, 'covers'))} within {_n(P(SD_NP_DS3073, 'radius_ft'))} ft, and a map of every "
            "property in that radius.", SD_NP_DS3073)
    acc.req("Post a Notice of Pending Construction", core.rule(SD_NP_POST).plain_english, SD_NP_POST)
    acc.req("Follow the permit conditions", core.rule(SD_NP_COND).plain_english + " "
            + core.rule(SD_NP_REVOKE).plain_english, SD_NP_COND, SD_NP_REVOKE)
    acc.req("Extend before it expires",
            f"Request an extension at least {P(SD_NP_EXT, 'min_hours_before_expiration')} hours before the permit "
            "expires; an expired permit cannot be extended (each extension is a new application and fee).",
            SD_NP_EXT, SD_NP_FEE)


def _street_needs_noise_permit(d: date, t: Optional[int], hol: Optional[bool], hol_name: Optional[str]) -> bool:
    spans = _sd_construction(d, bool(hol), hol_name)
    if t is None:
        return all(s[2] != "allowed" for s in spans)
    return _at(spans, t)[2] != "allowed"


def construction_hours(city, when=None, work_type="construction", hcr_district: bool = False,
                       residential_area: bool = True, is_holiday: Optional[bool] = None,
                       emergency_work: bool = False, as_of=None) -> ConstructionHoursResult:
    """Is this kind of work allowed in this city at this date and time, and if not, what does it take?"""
    key = normalize_city(city)
    d, t = _when(when)
    work = _work_type(work_type)
    hcr = bool(_bool("hcr_district", hcr_district))
    residential = _bool("residential_area", residential_area)
    residential = True if residential is None else residential
    given_hol = _bool("is_holiday", is_holiday)
    emergency = bool(_bool("emergency_work", emergency_work))
    today = _date(as_of, "as_of") or logic.today_ca()
    if hcr and key != LA:
        raise ValueError("hcr_district (Hillside Construction Regulation district) applies only to Los Angeles")
    if work == "bin_service" and key != SD:
        raise ValueError(f"bin_service hours are in the rulebook only for San Diego (SDMC 59.5.0406); for "
                         f"{_city_name(key)} use work_type 'construction' or ask the hauler and the city")
    if emergency and key != SD:
        raise ValueError("emergency_work applies to San Diego's SDMC 59.5.0404(c) notice only")
    acc = _Acc(today)
    spans, hol, hol_name, basis = _day_spans(key, d, work, hcr, residential, given_hol, acc)
    for s in spans:
        acc.use(*s[4])
    name = _city_name(key)
    day = WEEKDAYS[d.weekday()]
    res = ConstructionHoursResult(city=name, city_rules_verified=key in VERIFIED_CITIES, date=d.isoformat(),
                                  time=_hm(t) if t is not None else None, day_of_week=day, work_type=work,
                                  hcr_district=hcr, holiday=hol, holiday_name=hol_name, holiday_basis=basis,
                                  status="allowed")
    res.day_schedule = [Window(start=_hm(s), end=_hm(e) if e < DAY else "24:00", status=st, reason=why)
                        for s, e, st, why, _ in spans]
    res.allowed_windows = [f"{w.start}-{w.end}" for w in res.day_schedule if w.status == "allowed"]
    if t is not None:
        cur = _at(spans, t)
        res.status = cur[2]
        why = cur[3]
    else:
        st = [s[2] for s in spans]
        res.status = ("allowed" if "allowed" in st else "check_holiday" if "check_holiday" in st else
                      "restricted" if "restricted" in st else "needs_permit" if "needs_permit" in st else "not_allowed")
        why = "; ".join(dict.fromkeys(s[3] for s in spans if s[2] == res.status))
    res.allowed = True if res.status == "allowed" else False if res.status in ("needs_permit", "not_allowed") else None
    nxt, assumed = _next_allowed(key, d, t if t is not None else 0, work, hcr, residential, spans)
    res.next_allowed_start = nxt
    if assumed and nxt:
        acc.note(f"next_allowed_start assumes the days before it are not holidays ({basis.split(':')[-1].strip()}).")

    when_txt = f"{day.title()} {d.isoformat()}" + (f" at {_clock(t)}" if t is not None else "")

    def _span_txt(w):
        return f"{_clock(_m(w.start))}-{_clock(_m(w.end)) if w.end != '24:00' else 'midnight'}"
    windows_txt = ", ".join([_span_txt(w) for w in res.day_schedule if w.status == "allowed"]
                            + [_span_txt(w) + " if not a holiday" for w in res.day_schedule
                               if w.status == "check_holiday" and w.reason.startswith("allowed if")]) or "none"
    # holiday notes
    if hol and key == SD and hol_name:
        exc = P(SD_BAN, "holidays_excepted_from_ban")
        if any(_words(x).split()[0] in _words(hol_name) for x in exc) or "president" in _words(hol_name):
            acc.note(f"SDMC 59.5.0404(a) excepts {_join(exc)} from the holiday ban, but the City does not say whether "
                     f"its '{hol_name}' is that day, and DSD requires a noise permit all day on City holidays; it is "
                     f"treated as a holiday here. Confirm with {P(SD_WINDOWS, 'contact')}.")
    if key == SD and day == "saturday" and work in ("construction", "interior"):
        acc.note(f"Saturday: the code and the DSD page body allow 7 a.m.-7 p.m. without a noise permit, but the DSD "
                 f"page headline says permits are needed 'all day on Saturdays'. Confirm with "
                 f"{P(SD_WINDOWS, 'contact')} before scheduling Saturday daytime work without a permit.")
    if key == SD and work == "interior":
        acc.note("SDMC 59.5.0404(a) bans only noisy work in these windows, but DSD requires a Construction Noise "
                 "Permit for any construction in them, so interior work is treated the same.")
    if key == SD and work == "bin_service" and not P(SD_REFUSE, "roll_off_service_expressly_named"):
        acc.note("SDMC 59.5.0406 names 'refuse compacting, processing, or collection' vehicles; whether a roll-off "
                 "box swap counts is not stated. Schedule swaps inside the allowed hours to be safe.")
    if key == LA and hol is None:
        acc.note(f"LADBS limits national holidays to Saturday hours ({_clock(_m(P(LA_HOLIDAY, 'national_holiday_hours.start')))}"
                 f"-{_clock(_m(P(LA_HOLIDAY, 'national_holiday_hours.end')))}) but does not list them; pass "
                 "is_holiday=true or false for a firm answer.")
    if key == LA and hcr:
        acc.use(LA_HCR_LIGHTS)
        acc.note(core.rule(LA_HCR_LIGHTS).plain_english)
    if key == LB:
        acc.note("Long Beach treats a federal holiday as a weekday (reported, unverified); the Harbor District has "
                 "different rules.")
    if emergency:
        hrs = int(P(SD_EMERG, "notify_within_hours"))
        start_dt = datetime(d.year, d.month, d.day) + timedelta(minutes=t or 0)
        due = start_dt + timedelta(hours=hrs)
        acc.req("Notify the Noise Abatement Administrator (emergency work)",
                f"Emergency construction equipment is exempt from the {P(SD_75DB, 'limit_db')} dB daytime limit only "
                f"if DSD's Noise Abatement Administrator is notified within {hrs} hours after the work starts. On "
                "its words the exception does not waive the night, Sunday or holiday permit requirement.",
                SD_EMERG, due=due.date(), due_time=due.hour * 60 + due.minute)
        acc.math.append(f"Emergency notice: {start_dt.date().isoformat()} {_hm(t or 0)} + {hrs} h = "
                        f"{due.date().isoformat()} {_hm(due.hour * 60 + due.minute)}")

    # penalty and next step
    noise_window = key == SD and work in ("construction", "interior", "street_work") and (
        work != "street_work" or _street_needs_noise_permit(d, t, hol, hol_name))
    if res.status != "allowed":
        bits = []
        if key == SD and work == "street_work":
            acc.use(SD_TCP_NO_PERMIT)
            bits.append("street work outside what the Traffic Control Permit allows brings a stop-work order, a notice "
                        "of violation and Building and Land Use Enforcement fines (amounts not published in IB 177)")
        if key == SD and (noise_window or work == "delivery"):
            acc.use(SD_PENALTY)
            bits.append(f"{_usd(P(SD_PENALTY, 'civil_penalty_usd'))} civil penalty for excessive construction noise in "
                        f"restricted hours, up to {_usd(P(SD_PENALTY, 'max_per_incident_usd'))} per incident (usually "
                        "after a history of noise issues at the location; City policy flyer)")
        elif key == LA:
            acc.use(LA_PENALTY)
            bits.append(f"misdemeanor for the responsible party: fine up to {_usd(P(LA_PENALTY, 'fine_usd_max'))} "
                        f"and/or up to {P(LA_PENALTY, 'jail_months_max')} months in County Jail")
        res.penalty = ("; ".join(bits)[:1].upper() + "; ".join(bits)[1:]) if bits else None
    status_words = {"allowed": "ALLOWED", "needs_permit": "NEEDS A PERMIT", "not_allowed": "NOT ALLOWED",
                    "restricted": "RESTRICTED", "check_holiday": "DEPENDS ON WHETHER IT IS A HOLIDAY"}
    res.summary = (f"{name}, {work.replace('_', ' ')}, {when_txt}: {status_words[res.status]} - {why.rstrip('.')}. "
                   f"No-permit window(s) that day: {windows_txt}.")
    nxt_txt = f" (next no-permit time: {nxt.replace('T', ' ')})" if nxt and res.status != "allowed" else ""
    if res.status == "allowed":
        res.next_step = ("Go ahead inside the window shown"
                         + (f"; keep the average under {P(SD_75DB, 'limit_db')} dB at residential property lines"
                            if key == SD and work in ("construction", "interior") else "")
                         + ("; you still need the Traffic Control Permit for street work" if work == "street_work"
                            else "") + ".")
    elif res.status == "check_holiday":
        res.next_step = (f"Find out whether {d.isoformat()} is a "
                         f"{'City' if key == SD else 'national'} holiday, then call again with is_holiday=true or "
                         f"false{nxt_txt}.")
    elif key == SD and work == "street_work" and not noise_window:
        acc.use(SD_TCP_HOURS)
        res.next_step = ("Ask for these hours to be approved on your Traffic Control Permit (note them on the traffic "
                         f"control plan), or work in the standard window{nxt_txt}. No noise permit is needed at this "
                         "time of day.")
    elif key == SD and res.status == "needs_permit" and noise_window:
        cal = _Cal(SD)
        _sd_noise_permit_plan(acc, cal, d, t, work == "street_work", today)
        dls = acc.deadlines()
        late = [x for x in dls if x.status == "passed" and not x.estimate]
        nxt_dl = next((x for x in dls if x.status != "passed"), None)
        behind = [x for x in dls if x.status == "passed" and x.estimate]
        if behind and not late:
            res.next_step = (f"Move the work into the no-permit window{nxt_txt}, or get a Construction Noise Permit "
                             f"({_usd(P(SD_NP_FEE, 'fee_usd'))}). You are behind DSD's processing estimate "
                             f"('{behind[0].what}', {behind[0].due}), so file now"
                             + (f"; the next legal deadline is '{nxt_dl.what}' by {nxt_dl.due}" if nxt_dl else "")
                             + ".")
        elif late:
            res.next_step = (f"Move the work into the no-permit window{nxt_txt}. A noise permit for this date is "
                             f"already late: '{late[0].what}' was due {late[0].due}. Ask "
                             f"{P(SD_WINDOWS, 'contact')} whether it can still be processed, or pick a later date.")
        else:
            res.next_step = (f"Move the work into the no-permit window{nxt_txt}, or get a Construction Noise Permit "
                             f"({_usd(P(SD_NP_FEE, 'fee_usd'))})"
                             + (f": first step '{nxt_dl.what}' by {nxt_dl.due}" if nxt_dl else "") + ".")
        if cal.unknown_years or cal.skipped:
            acc.math.append(cal.explain())
    elif key == SD and work == "bin_service":
        res.next_step = ("Schedule the swap inside the allowed hours or get a permit from the Noise Abatement "
                         f"Administrator first{nxt_txt}.")
    elif key == SD and work == "delivery":
        res.next_step = (f"Have trucks arrive and stage quietly, away from homes, or schedule the delivery for "
                         f"{nxt.replace('T', ' ') if nxt else 'the allowed hours'}.")
    elif key == LA:
        res.next_step = (f"Move the work into the permitted hours{nxt_txt}. The rulebook has no verified after-hours "
                         "permission route for Los Angeles (LAMC 41.40 could not be read): ask LADBS before planning "
                         "work outside these hours.")
    else:
        res.next_step = (f"Move the work into the window shown{nxt_txt}, or get the permission described above. "
                         f"These {name} hours are UNVERIFIED (official text not read): confirm with the city first.")
    return acc.finish(res)


# ================================================================ 2. local jobsite requirements
class JobsiteRequirementsResult(_Base):
    start_date: Optional[str] = None
    triggers: list[str] = Field(default_factory=list, description="The facts that triggered requirements")
    requirements: list[Requirement] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)
    fees: list[FeeLine] = Field(default_factory=list)
    fees_total_usd: Optional[float] = None


def _bin_type(value) -> str:
    w = _words(value or "dumpster")
    if re.search(r"\b(storage|container|pod|pods|moving|portable storage|conex|shipping)\b", w):
        return "storage_container"
    if re.search(r"\b(dumpster|roll ?off|rolloff|bin|debris box|skip|roll off box)\b", w):
        return "dumpster"
    raise ValueError(f"bin_type must be dumpster (roll-off / debris box) or storage_container (moving pod); got "
                     f"{value!r}")


def _sd_tcp_fees(acc: _Acc, blocks: int, weeks: float, extension_requests: Optional[int] = None,
                 modifications: int = 0, new_sheets: int = 0) -> dict:
    f = core.rule(SD_TCP_FEES)
    init_w, ext_max = int(f.param("initial_period_weeks_max")), int(f.param("extension_weeks_max"))
    ext_weeks = max(0, math.ceil(round(weeks - init_w, 9)))
    min_req = math.ceil(ext_weeks / ext_max) if ext_weeks else 0
    req = min_req if extension_requests is None else int(extension_requests)
    if req < min_req:
        raise ValueError(f"{ext_weeks} extension week(s) need at least {min_req} extension request(s): IB 177 lets "
                         f"each extension be requested for up to {ext_max} weeks")
    if req > ext_weeks:
        raise ValueError(f"extension_requests ({req}) cannot exceed the {ext_weeks} extension week(s) needed")
    proc = f.param("processing_fee_usd_flat")
    initial = f.param("initial_fee_usd_per_block") * blocks
    ext = f.param("extension_fee_usd_per_block_per_week") * blocks * ext_weeks
    n_proc = 1 + req + modifications
    acc.fee("Traffic Control Initial", initial,
            f"{_usd(f.param('initial_fee_usd_per_block'))}/block x {blocks} block(s) (covers up to {init_w} weeks)",
            SD_TCP_FEES)
    if ext_weeks:
        acc.fee("Traffic Control Extension", ext,
                f"{_usd(f.param('extension_fee_usd_per_block_per_week'))}/block/week x {blocks} block(s) x "
                f"{ext_weeks} week(s) (ceil({_n(weeks)} - {init_w}))", SD_TCP_FEES)
    acc.fee("Traffic Control Processing Fee", proc * n_proc,
            f"{_usd(proc)} x {n_proc} request(s) (initial + {req} extension request(s) + {modifications} "
            f"modification(s); {f.param('processing_fee_charged_per')})", SD_TCP_FEES)
    if new_sheets:
        acc.fee("Modification new sheet fee", f.param("initial_fee_usd_per_block") * new_sheets,
                f"{_usd(f.param('initial_fee_usd_per_block'))} x {new_sheets} new sheet(s) "
                f"({f.param('modification_fee')})", SD_TCP_FEES)
    return {"extension_weeks": ext_weeks, "extension_requests": req, "min_requests": min_req}


def _sd_bin_fees(acc: _Acc, kind: str, weeks: Optional[float] = None, days: Optional[float] = None) -> None:
    proc = P(SD_DUMP, "processing_fee_usd_flat")
    if kind == "dumpster":
        fee = P(SD_DUMP, "dumpster_fee_usd")
        m = re.search(r"up to (\d+) weeks", P(SD_DUMP, "dumpster_fee_period"))
        per_w = int(m.group(1)) if m else None
        periods = max(1, math.ceil(round((weeks or 0) / per_w, 9))) if per_w and weeks else 1
        acc.fee("Dumpster fee", fee * periods,
                f"{_usd(fee)} x {periods} period(s) ({P(SD_DUMP, 'dumpster_fee_period')}"
                + (f"; ceil({_n(weeks)} weeks / {per_w})" if weeks else "") + ")", SD_DUMP)
        if periods > 1:
            acc.note("A dumpster kept past the first period is assumed to be billed again for each period; IB 718 "
                     "does not say how renewals are processed. Ask DSD.")
    else:
        fee = P(SD_DUMP, "storage_container_fee_usd_per_week")
        max_days = int(P(SD_STORAGE_7D, "storage_container_permit_max_days"))
        acc.fee("Temporary storage container fee", fee,
                f"{_usd(fee)} for up to {P(SD_DUMP, 'storage_container_max_weeks')} week ({max_days} days)", SD_DUMP,
                SD_STORAGE_7D)
        if days and days > max_days:
            acc.note(f"A storage container permit lasts up to {max_days} days; extensions carry additional fees that "
                     "IB 718 does not price, so only the first week is in the total.")
    acc.fee("Processing fee (dumpster / container permit)", proc, f"{_usd(proc)} flat per request", SD_DUMP)


def _la_haul_fee(cy: float) -> tuple[float, str]:
    f = core.rule(LA_HAUL_FEE)
    base, unit = f.param("base_fee_usd"), f.param("base_cubic_yards")
    add, pct, flat = (f.param("additional_fee_usd_per_1000_cy_or_portion"), f.param("surcharge_percent"),
                      f.param("surcharge_flat_usd"))
    steps = math.ceil(round(max(0.0, cy - unit) / unit, 9))
    sub = base + add * steps
    total = _money(sub * (1 + pct / 100) + flat)
    return total, (f"({_usd(base)} first {_n(unit)} cy + {_usd(add)} x {steps} added {_n(unit)}-cy step(s) "
                   f"[ceil(({_n(cy)} - {_n(unit)}) / {_n(unit)})] = {_usd(sub)}) x {1 + pct / 100:g} + "
                   f"{_usd(flat)} = {_usd(total)}")


def _sd_jobsite(acc, cal, f, start, triggers):
    """San Diego permits and notices for the facts in f."""
    acc.req("Work hours", core.rule(SD_WINDOWS).plain_english + " Use construction_hours for a specific day.",
            SD_WINDOWS, SD_BAN)
    if f["row"] or f["builds_row"]:
        triggers.append("work in the public right-of-way")
        n = int(P(SD_TCP_5WD, "min_working_days_before_start"))
        bd = int(P(SD_TCP_15BD, "typical_processing_business_days"))
        due5 = cal.before(start, n) if start else None
        due15 = cal.before(start, bd) if start else None
        acc.req("Traffic Control Permit (DSD)",
                f"Required for any work encroaching into streets, sidewalks, parking spaces, medians or alleys. File "
                f"{_join(P(SD_TCP_REQ, 'forms'))} in Accela ({P(SD_TCP_REQ, 'apply_via')}) at least {n} working days "
                "before work starts (the legal minimum).", SD_TCP_REQ, SD_TCP_5WD, due=due5)
        acc.req("Submit the TCP early (processing time)",
                f"DSD says processing takes about {bd} business days, so submit by "
                + (due15.isoformat() if due15 else f"{bd} business days ahead") + " to start on time.",
                SD_TCP_15BD, due=due15, estimate=True)
        if start:
            acc.math.append(f"TCP: {start.isoformat()} - {n} working days = {due5.isoformat()} (legal minimum); "
                            f"- {bd} business days = {due15.isoformat()} (processing estimate)")
        acc.req("Traffic control plan", core.rule(SD_TCP_MUTCD).plain_english, SD_TCP_MUTCD)
        acc.req("Trained traffic control crew", core.rule(SD_TCP_TRAINED).plain_english, SD_TCP_TRAINED)
        acc.req("Insurance and indemnity",
                f"Declare public liability insurance of at least {_usd(P(SD_TCP_INS, 'liability_insurance_usd_csl_min'))} "
                f"combined single limit for the permit term, naming the {P(SD_TCP_INS, 'additional_insured')} as "
                "additional insured, and agree to defend and indemnify the City.", SD_TCP_INS)
        acc.req("Prevailing wage statement on DS-269", core.rule(SD_TCP_PW).plain_english, SD_TCP_PW)
        acc.req("Standard work hours", core.rule(SD_TCP_HOURS).plain_english + " "
                + core.rule(SD_TCP_NO_WKND).plain_english, SD_TCP_HOURS, SD_TCP_NO_WKND)
        nn = int(P(SD_TCP_NOTIFY, "lead_time_working_days"))
        acc.req("Notify agencies", f"At least {nn} working days before work, when applicable: "
                f"{_join(P(SD_TCP_NOTIFY, 'notify'))}; tell owners and tenants before closing driveways.",
                SD_TCP_NOTIFY, due=cal.before(start, nn) if start else None)
        hrs = int(P(SD_TCP_SIGNS, "lead_time_hours"))
        sd_, st_ = _before_hours(start, f["start_min"], hrs) if start else (None, None)
        acc.req("Post Tow-Away/No Parking signs",
                f"At least {hrs} hours before work starts; cover or tag parking meters. Sample sign: "
                f"{P(SD_TCP_SIGNS, 'sample_sign')}.", SD_TCP_SIGNS, due=sd_, due_time=st_)
        acc.req("Pre-construction meeting", core.rule(SD_PRECON).plain_english, SD_PRECON)
        acc.req("Keep the street safe and clean", core.rule(SD_TCP_TRENCH).plain_english + " "
                + core.rule(SD_TCP_LANES).plain_english + " " + core.rule(SD_TCP_PED).plain_english,
                SD_TCP_TRENCH, SD_TCP_LANES, SD_TCP_PED)
        acc.req("No work without a valid permit", core.rule(SD_TCP_NO_PERMIT).plain_english, SD_TCP_NO_PERMIT)
        init_w = int(P(SD_TCP_EXT, "initial_validity_weeks_max"))
        if f["weeks"] and f["weeks"] > init_w and start:
            exp = start + timedelta(weeks=init_w)
            nx = int(P(SD_TCP_EXT, "extension_request_min_working_days_before_expiration"))
            due = cal.before(exp, nx)
            acc.req("Extend the Traffic Control Permit",
                    f"The TCP is valid for up to {init_w} weeks; your {_n(f['weeks'])} weeks need an extension filed "
                    f"at least {nx} working days before it expires (City Engineer approval; a new DS-269 after "
                    f"{P(SD_TCP_EXT, 'new_ds269_after_extensions')} extensions). Use the expiration printed on the "
                    "permit; this date assumes it expires 3 weeks after the start.", SD_TCP_EXT, due=due)
            acc.math.append(f"TCP extension: ({start.isoformat()} + {init_w} weeks = {exp.isoformat()}) - {nx} working "
                            f"days = {due.isoformat()}")
        else:
            acc.req("TCP term", core.rule(SD_TCP_EXT).plain_english, SD_TCP_EXT)
        if f["blocks"]:
            _sd_tcp_fees(acc, int(f["blocks"]), f["weeks"] or 1)
        if f["signals"]:
            triggers.append("work affecting traffic signals")
            ns = int(P(SD_TCP_SIGNAL, "min_working_days_notice"))
            acc.req("Traffic signals", f"Contact {P(SD_TCP_SIGNAL, 'contact')} at least {ns} working days ahead.",
                    SD_TCP_SIGNAL, due=cal.before(start, ns) if start else None)
    if f["builds_row"]:
        triggers.append("construction in the public right-of-way")
        acc.req("Public Right-of-Way Permit (DSD)",
                core.rule(SD_ROW_REQ).plain_english + " A separate Traffic Control Permit is still required.",
                SD_ROW_REQ)
        acc.req("ROW permit term and extensions",
                core.rule(SD_ROW_TERM).plain_english + " " + core.rule(SD_ROW_EXT60).plain_english,
                SD_ROW_TERM, SD_ROW_EXT60)
    if f["private_improvement"]:
        triggers.append("private improvement left in the right-of-way")
        acc.req("Encroachment Maintenance and Removal Agreement",
                f"{P(SD_EMRA, 'document')}: " + core.rule(SD_EMRA).plain_english, SD_EMRA)
    if f["trench"]:
        triggers.append("trenching in a street")
        mor = P(SD_MORATORIUM_DEF, "moratorium_years")
        acc.req("Moratorium street check",
                core.rule(SD_MORATORIUM).plain_english + f" Under the 2016 memo a moratorium street was resurfaced or "
                f"reconstructed in the last {mor['reconstructed_or_resurfaced']} years or slurry sealed in the last "
                f"{mor['slurry_sealed']}; the current code may differ, so check the street with the City before "
                "bidding.", SD_MORATORIUM, SD_MORATORIUM_DEF)
        acc.req("Street restoration", core.rule(SD_RESTORE).plain_english + " "
                + core.rule(SD_OVERLAY).plain_english, SD_RESTORE, SD_OVERLAY)
    if f["monument"]:
        triggers.append("survey monument disturbed")
        days = int(P(SD_MONUMENT, "lead_time_days"))
        acc.req("Survey monument notice", core.rule(SD_MONUMENT).plain_english, SD_MONUMENT,
                due=(start - timedelta(days=days)) if start else None)
    if f["bin"]:
        kind = f["bin"]
        triggers.append(f"{kind.replace('_', ' ')} in the street")
        n3 = int(P(SD_DUMP_3WD, "min_working_days_before_placement"))
        hrs = int(P(SD_DUMP_SIGNS, "lead_time_hours"))
        acc.req(f"Traffic Control Permit for the {kind.replace('_', ' ')}",
                f"Required when it encroaches into the street or sidewalk area; file "
                f"{_join(P(SD_DUMP, 'forms'))} with start and end dates at least {n3} working days before placement.",
                SD_DUMP, SD_DUMP_3WD, due=cal.before(start, n3) if start else None)
        sd_, st_ = _before_hours(start, f["start_min"], hrs) if start else (None, None)
        acc.req("No Parking signs for the bin", f"Post Tow-Away/No Parking signs {hrs} hours before placing it and "
                "deactivate parking meters where applicable.", SD_DUMP_SIGNS, due=sd_, due_time=st_)
        c = P(SD_DUMP_CLEAR, "min_clearance_ft")
        acc.req("Placement", f"Not in front of {_join(P(SD_DUMP_CLEAR, 'prohibited_curbs'), 'or')} curb; at least "
                f"{c['door_to_nearest_obstruction']} ft from the door to the nearest obstruction, "
                f"{c['door_to_alley_or_curb_return']} ft to an alley or curb return and {c['door_to_fire_hydrant']} ft "
                "to a fire hydrant.", SD_DUMP_CLEAR)
        if kind == "storage_container":
            acc.req("Container permit length", core.rule(SD_STORAGE_7D).plain_english, SD_STORAGE_7D)
        r = P(SD_REFUSE, "refuse_vehicle_restricted")
        acc.req("Bin swaps in residential areas",
                f"Refuse collection vehicles may not operate {_clock(_m(r['start']))}-{_clock(_m(r['end']))} in "
                "residential areas without a permit; the code does not say whether roll-off swaps count, so schedule "
                "them inside the allowed hours.", SD_REFUSE)
        _sd_bin_fees(acc, kind, weeks=f["weeks"], days=(f["weeks"] or 0) * 7)
    if f["night"]:
        triggers.append("night, Sunday or holiday work")
        if start:
            _sd_noise_permit_plan(acc, cal, start, f["start_min"], bool(f["row"] or f["builds_row"]), acc.today)
        else:
            acc.req("Construction Noise Permit (San Diego DSD)", core.rule(SD_WINDOWS).plain_english,
                    SD_WINDOWS, SD_BAN, SD_NP_FEE)
        acc.fee("Construction Noise Permit", P(SD_NP_FEE, "fee_usd"),
                f"{_usd(P(SD_NP_FEE, 'fee_usd'))} per application ({P(SD_NP_FEE, 'per')})", SD_NP_FEE)
    if f["earthwork"]:
        triggers.append(f"{_n(f['earthwork'])} cy of earthwork")
        acc.req("Export soil to Miramar Landfill", core.rule(SD_MIRAMAR_FILL).plain_english, SD_MIRAMAR_FILL)
    if f["demolition"]:
        triggers.append("demolition")
        acc.req("C&D debris deposit and diversion",
                core.rule(SD_CD_APPLIES).plain_english + " Use construction_debris_plan for the deposit amount.",
                SD_CD_APPLIES)
    if f["trench"]:
        _usa(acc, start)


def _usa(acc, start):
    n = int(P(USA811, "advance_notice_working_days_min"))
    due = _weekdays_before(start, n) if start else None
    acc.req("Call 811 (Underground Service Alert)", core.rule(USA811).plain_english, USA811, due=due)
    if start:
        acc.math.append(f"811: {start.isoformat()} - {n} working days (Monday-Friday) = {due.isoformat()}; a state "
                        "holiday in between makes it earlier")


def _neshap(acc, start):
    n = int(P(NESHAP, "lead_time_working_days"))
    due = _weekdays_before(start, n) if start else None
    acc.req("Asbestos/demolition notice to the air district", core.rule(NESHAP).plain_english, NESHAP, due=due)
    if start:
        acc.math.append(f"NESHAP: {start.isoformat()} - {n} working days ({P(NESHAP, 'working_day_definition')}) = "
                        f"{due.isoformat()}")


def _la_jobsite(acc, f, start, triggers):
    hrs = P(LA_HOURS, "permitted_hours")
    acc.req("Work hours",
            f"Monday-Friday {_clock(_m(hrs['monday_friday']['start']))}-{_clock(_m(hrs['monday_friday']['end']))}, "
            f"Saturday and national holidays {_clock(_m(hrs['saturday']['start']))}-{_clock(_m(hrs['saturday']['end']))}"
            ", no Sunday work." + (" HCR hillside district: weekdays "
                                   f"{_clock(_m(P(LA_HCR, 'weekday_hours.start')))}-"
                                   f"{_clock(_m(P(LA_HCR, 'weekday_hours.end')))}, Saturdays interior work only; no "
                                   "floodlights after 6 p.m." if f["hcr"] else ""),
            LA_HOURS, LA_HOLIDAY, *([LA_HCR, LA_HCR_LIGHTS] if f["hcr"] else []))
    acc.req("Keep streets and sidewalks clean", core.rule(LA_SWEEP).plain_english, LA_SWEEP)
    if f["row"] or f["builds_row"]:
        triggers.append("work in the public right-of-way")
        acc.req("Street closures and flaggers", core.rule(LA_FLAGGERS).plain_english
                + " 'Peak traffic hours' are not defined on the sheet; ask StreetsLA/LADOT.", LA_FLAGGERS)
    if f["builds_row"]:
        triggers.append("construction in the public right-of-way")
        acc.req("Bureau of Engineering permit (UNVERIFIED)", core.rule(LA_BOE_U).plain_english, LA_BOE_U)
    if f["bin"] or f["row"]:
        acc.req("Materials stored in the street", core.rule(LA_MATERIALS).plain_english, LA_MATERIALS)
    if f["bin"] == "storage_container":
        triggers.append("storage container in the street")
        acc.req("Storage pods on public property (UNVERIFIED)", core.rule(LA_PODS_U).plain_english, LA_PODS_U)
    elif f["bin"] == "dumpster":
        triggers.append("dumpster in the street")
        acc.note("The rulebook has no verified Los Angeles rule for a roll-off dumpster in the street (StreetsLA "
                 "permit pages could not be read). Ask StreetsLA (Bureau of Street Services) before placing one; the "
                 "hauler must hold a LASAN Private Waste Hauler Permit (see construction_debris_plan).")
    if f["night"]:
        triggers.append("night, Sunday or holiday work")
        acc.req("After-hours work (UNVERIFIED route)", "Construction outside the permitted hours is not allowed. "
                + core.rule(LA_AFTER_HOURS_U).plain_english, LA_HOURS, LA_AFTER_HOURS_U, LA_PENALTY)
    cy = f["earthwork"]
    trig = P(LA_HAUL_HEARING, "trigger_cubic_yards_over")
    if cy and f["hillside"] and cy > trig:
        triggers.append(f"{_n(cy)} cy import/export in a Hillside Grading Area")
        if f["eo1"]:
            acc.req("Haul route (EO 1 wildfire rebuild): ministerial",
                    core.rule(LA_EO1).plain_english + f" Eligibility: {P(LA_EO1, 'eligible_project')}.", LA_EO1)
        else:
            wk = int(P(LA_HAUL_8WK, "min_processing_weeks"))
            due = start - timedelta(weeks=wk) if start else None
            acc.req("Haul route approval (BBSC public hearing)",
                    core.rule(LA_HAUL_HEARING).plain_english + f" File with the Building and Safety Commission Office "
                    f"{P(LA_HAUL_HEARING, 'file_with').split('Office')[-1].strip()}; processing takes at least {wk} "
                    "weeks.", LA_HAUL_HEARING, LA_HAUL_8WK, due=due)
            if start:
                acc.math.append(f"Haul route: {start.isoformat()} - {wk} weeks = {due.isoformat()} (minimum processing; "
                                "the CEQA determination and hearing can take longer)")
            acc.req("CEQA first", core.rule(LA_HAUL_CEQA).plain_english, LA_HAUL_CEQA)
            acc.req("Application package", core.rule(LA_HAUL_CONTENTS).plain_english + " "
                    + core.rule(LA_HAUL_OWNERS).plain_english, LA_HAUL_CONTENTS, LA_HAUL_OWNERS)
            acc.req("After approval", core.rule(LA_HAUL_12_18).plain_english + " "
                    + core.rule(LA_HAUL_CONDITIONS).plain_english, LA_HAUL_12_18, LA_HAUL_CONDITIONS)
            fee, how = _la_haul_fee(cy)
            acc.fee("Haul route (import-export) filing fee", fee, how, LA_HAUL_FEE)
            acc.note("Haul route fee: the 22% surcharge is applied to the whole base fee (the plain reading; the 2018 "
                     "form does not spell it out) and no 2026 fee source was read. Confirm with the Commission Office.")
        n_days = int(P(LA_HILLSIDE_NOTICE, "mail_days_before_permit_issuance"))
        acc.req("Neighbor notices before the grading permit", core.rule(LA_HILLSIDE_NOTICE).plain_english
                + f" The permit cannot be issued sooner than {n_days} days after the mailing.", LA_HILLSIDE_NOTICE)
    elif cy and cy > trig:
        triggers.append(f"{_n(cy)} cy of earthwork (not in a Hillside Grading Area)")
        acc.note(f"The BBSC haul-route hearing applies to more than {_n(trig)} cy in a Hillside Grading Area; you said "
                 "the site is not in one. Check ZIMAS for the designation.")
    if f["demolition"]:
        triggers.append("demolition")
        age = f["age"]
        over = P(LA_DEMO_45, "building_age_years_over")
        if age is None or age > over:
            days = int(P(LA_DEMO_45, "min_days_before_permit_issuance"))
            acc.req(f"{over}-year demolition notice" + ("" if age is not None else f" (if the building is over {over} "
                                                        "years old)"),
                    core.rule(LA_DEMO_45).plain_english + f" Plan on the permit no sooner than {days} days after the "
                    "notice and posting.", LA_DEMO_45)
            pl = P(LA_DEMO_PLACARD, "placard_min_size_in")
            acc.req("Demolition placard", core.rule(LA_DEMO_PLACARD).plain_english
                    + f" ({pl[0]} x {pl[1]} in minimum).", LA_DEMO_PLACARD)
        lic = P(LA_DEMO_LICENSE, "eligible_licenses")
        have = [c.upper().replace(" ", "").replace("CLASS", "") for c in (f["licenses"] or [])]
        ok = any(c in lic for c in have) if have else None
        acc.req("Who may pull the demolition permit", core.rule(LA_DEMO_LICENSE).plain_english
                + ("" if ok is None else " Your classes " + _join(have) + (" qualify." if ok else
                   f" are not {_join(lic, 'or')}; only the owner or B-contractor exceptions could apply.")),
                LA_DEMO_LICENSE)
        acc.req("Sewer cap and lateral support", core.rule(LA_DEMO_SEWER).plain_english, LA_DEMO_SEWER)
        h = int(P(LA_DEMO_INSP, "inspection_request_min_hours_before_start"))
        sd_, st_ = _before_hours(start, f["start_min"], h) if start else (None, None)
        acc.req("Demolition inspection request", core.rule(LA_DEMO_INSP).plain_english, LA_DEMO_INSP,
                due=sd_, due_time=st_)
        _neshap(acc, start)
    if f["trench"]:
        triggers.append("trenching in a street")
        _usa(acc, start)


def _other_jobsite(acc, key, f, start, triggers):
    name = _city_name(key)
    hours_rule = {SF: [SF_NIGHT_U, SF_EQUIP_U], SJ: [SJ_HOURS_U], SAC: [SAC_HOURS_U], OAK: [OAK_COA_U, OAK_CODE_U],
                  LB: [LB_HOURS_U], FRE: [FRE_HOURS_U]}[key]
    acc.req("Work hours (UNVERIFIED)", " ".join(core.rule(i).plain_english for i in hours_rule), *hours_rule)
    if f["row"] or f["builds_row"]:
        triggers.append("work in the public right-of-way")
        if key == SF:
            acc.req("Street space permit (UNVERIFIED)", core.rule(SF_STREET_SPACE_U).plain_english, SF_STREET_SPACE_U)
        elif key == SJ:
            acc.req("Encroachment permit (UNVERIFIED)", core.rule(SJ_ENCROACH_U).plain_english + " "
                    + core.rule(SJ_STREET_U).plain_english, SJ_ENCROACH_U, SJ_STREET_U)
        else:
            acc.note(f"The rulebook has no {name} right-of-way permit rule; ask the city's public works department.")
    if f["bin"]:
        triggers.append(f"{f['bin'].replace('_', ' ')} in the street")
        if key == SF:
            hrs = P(SF_DEBRIS_BOX_U, "no_parking_signs_min_hours_ahead")
            bd = int(P(SF_DEBRIS_BOX_U, "apply_min_business_days_ahead"))
            acc.req("Debris box in the street (UNVERIFIED)", core.rule(SF_DEBRIS_BOX_U).plain_english + " "
                    + core.rule(SF_STREET_SPACE_U).plain_english, SF_DEBRIS_BOX_U, SF_STREET_SPACE_U,
                    due=_weekdays_before(start, bd) if start else None)
            if start:
                sd_, st_ = _before_hours(start, f["start_min"], int(hrs))
                acc.req("No Parking signs (UNVERIFIED)", f"Post at least {hrs} hours ahead.", SF_DEBRIS_BOX_U,
                        due=sd_, due_time=st_)
                acc.math.append(f"SF debris box: {start.isoformat()} - {bd} business days (Monday-Friday; city "
                                f"holidays not known) = {_weekdays_before(start, bd).isoformat()}")
            acc.req("C&D transporter permit (UNVERIFIED)", core.rule(SF_TRANSPORTER_U).plain_english, SF_TRANSPORTER_U)
        elif key == LB:
            bd = int(P(LB_BIN_U, "apply_min_business_days_before_delivery"))
            acc.req("Temporary Occupancy Permit for the bin (UNVERIFIED)", core.rule(LB_BIN_U).plain_english
                    + f" Apply at least {bd} business days before delivery; No Parking signs "
                    f"{P(LB_BIN_U, 'no_parking_signs_hours_before')} hours before.", LB_BIN_U,
                    due=_weekdays_before(start, bd) if start else None)
        else:
            hauler = {SJ: SJ_HAULERS_U, SAC: SAC_HAULER_U, OAK: OAK_HAULER_U, FRE: FRE_HAULERS_U}[key]
            acc.req("Use an authorized hauler (UNVERIFIED)", core.rule(hauler).plain_english, hauler)
            acc.note(f"The rulebook has no {name} rule for placing a bin in the street; ask the city.")
    if f["night"]:
        triggers.append("night, Sunday or holiday work")
        route = {SF: [SF_NIGHT_U, SF_NIGHT_COND_U], SJ: [SJ_HOURS_U], SAC: [SAC_URGENT_U], OAK: [OAK_CODE_U],
                 LB: [LB_AFTER_U], FRE: [FRE_HOURS_U]}[key]
        acc.req("Night, Sunday or holiday work (UNVERIFIED)", " ".join(core.rule(i).plain_english for i in route),
                *route)
    if f["demolition"]:
        triggers.append("demolition")
        _neshap(acc, start)
    if f["trench"]:
        triggers.append("trenching in a street")
        _usa(acc, start)
    if f["earthwork"] and key != LA:
        acc.note(f"The rulebook has no {name} haul-route or grading rule; ask the building department.")


def local_jobsite_requirements(city, start_date=None, work_in_public_right_of_way: bool = False,
                               builds_in_right_of_way: bool = False, uses_street_for_bin: bool = False,
                               bin_type: str = "dumpster", night_sunday_or_holiday_work: bool = False,
                               trenching_in_street: bool = False, affects_traffic_signals: bool = False,
                               disturbs_survey_monument: bool = False, private_improvement_stays_in_row: bool = False,
                               earthwork_cubic_yards=None, hillside_grading_area: bool = False,
                               eo1_wildfire_rebuild: bool = False, hcr_district: bool = False,
                               demolition: bool = False, building_age_years=None,
                               contractor_license_classes: Optional[list[str]] = None, blocks=None,
                               weeks_in_street=None, as_of=None) -> JobsiteRequirementsResult:
    """City permits, notices and dated deadlines for one jobsite."""
    key = normalize_city(city)
    has_start = not (start_date is None or (isinstance(start_date, str) and not start_date.strip()))
    start, start_min = _when(start_date, "start_date") if has_start else (None, None)
    today = _date(as_of, "as_of") or logic.today_ca()
    b = {k: bool(_bool(k, v)) for k, v in {
        "row": work_in_public_right_of_way, "builds_row": builds_in_right_of_way, "bin": uses_street_for_bin,
        "night": night_sunday_or_holiday_work, "trench": trenching_in_street, "signals": affects_traffic_signals,
        "monument": disturbs_survey_monument, "private_improvement": private_improvement_stays_in_row,
        "hillside": hillside_grading_area, "eo1": eo1_wildfire_rebuild, "hcr": hcr_district,
        "demolition": demolition}.items()}
    f = dict(b)
    f["start_min"] = start_min
    f["bin"] = _bin_type(bin_type) if b["bin"] else None
    f["earthwork"] = _num("earthwork_cubic_yards", earthwork_cubic_yards, lo=0)
    f["age"] = _num("building_age_years", building_age_years, lo=0, hi=1000)
    f["blocks"] = _num("blocks", blocks, lo=0, hi=500, integer=True)
    f["weeks"] = _num("weeks_in_street", weeks_in_street, lo=0, hi=520)
    if isinstance(contractor_license_classes, str):
        contractor_license_classes = [c for c in re.split(r"[,;/ ]+", contractor_license_classes) if c]
    f["licenses"] = contractor_license_classes
    if key != LA and (f["hillside"] or f["eo1"] or f["hcr"]):
        raise ValueError("hillside_grading_area, eo1_wildfire_rebuild and hcr_district are Los Angeles designations")
    if f["blocks"] and key != SD:
        raise ValueError("blocks (Traffic Control Permit fee) is priced only for San Diego")
    if f["blocks"] and not (f["row"] or f["builds_row"]):
        raise ValueError("blocks prices a Traffic Control Permit; set work_in_public_right_of_way=true")
    if (f["signals"] or f["private_improvement"]) and not (f["row"] or f["builds_row"]):
        raise ValueError("affects_traffic_signals and private_improvement_stays_in_row need "
                         "work_in_public_right_of_way or builds_in_right_of_way")
    acc = _Acc(today)
    cal = _Cal(key)
    triggers: list[str] = []
    if key == SD:
        _sd_jobsite(acc, cal, f, start, triggers)
    elif key == LA:
        _la_jobsite(acc, f, start, triggers)
    else:
        _other_jobsite(acc, key, f, start, triggers)
    if start and key == SD and (cal.skipped or cal.unknown_years):
        acc.math.append(cal.explain())
    name = _city_name(key)
    res = JobsiteRequirementsResult(city=name, city_rules_verified=key in VERIFIED_CITIES,
                                    start_date=start.isoformat() if start else None, triggers=triggers)
    acc.finish(res)
    if res.fees:
        res.fees_total_usd = _money(sum(x.amount_usd for x in res.fees))
        res.math.append("Fees: " + " + ".join(f"{x.item} {_usd(x.amount_usd)}" for x in res.fees)
                        + f" = {_usd(res.fees_total_usd)}")
    passed = [d for d in res.deadlines if d.status == "passed" and not d.estimate]
    behind = [d for d in res.deadlines if d.status == "passed" and d.estimate]
    upcoming = [d for d in res.deadlines if d.status != "passed"]
    res.summary = (f"{name}: {len(res.requirements)} requirement(s)"
                   + (f" for {_join(triggers)}" if triggers else " (no special triggers: hours and site upkeep)")
                   + (f"; {len(res.deadlines)} dated deadline(s), {len(passed)} already passed" if res.deadlines else "")
                   + (f"; fees about {_usd(res.fees_total_usd)}" if res.fees_total_usd is not None else "") + ".")
    if passed:
        res.next_step = (f"Already late: '{passed[0].what}' was due {passed[0].due}. Do it now and move the start "
                         "date if the city needs the full lead time.")
    elif behind:
        res.next_step = (f"Behind the city's processing estimate ('{behind[0].what}', {behind[0].due}): file now; "
                         + (f"the next legal deadline is '{upcoming[0].what}' by {upcoming[0].due}"
                            if upcoming else "expect the start date to slip") + ".")
    elif upcoming:
        res.next_step = f"Next: '{upcoming[0].what}' by {upcoming[0].due}" + (
            f" {upcoming[0].due_time}" if upcoming[0].due_time else "") + f" ({upcoming[0].days_left} day(s) left)."
    elif not start and res.requirements:
        res.next_step = "Give start_date to get the dates each step is due."
    else:
        res.next_step = "Follow the requirements listed."
    if key not in VERIFIED_CITIES:
        res.next_step += f" {name} rules are UNVERIFIED (official text not read): confirm with the city."
    return res


# ================================================================ 3. construction debris (C&D)
class DebrisPlanResult(_Base):
    project_type: str
    square_feet: Optional[float] = None
    deposit_required: Optional[bool] = Field(None, description="None when it cannot be determined from the inputs")
    deposit_usd: Optional[float] = None
    deposit_basis: str = ""
    exempt_reason: Optional[str] = None
    diversion_required_percent: Optional[float] = None
    diversion_achieved_percent: Optional[float] = None
    tons_to_divert_for_target: Optional[float] = None
    additional_tons_needed: Optional[float] = None
    refund_outlook: Literal["full", "pro_rated", "unknown", "not_applicable"] = "not_applicable"
    refund_deadline: Optional[str] = None
    landfill_cost_usd: Optional[float] = None
    city_fee_usd: Optional[float] = Field(None, description="Other city C&D fee or deposit computed (unverified "
                                                            "cities)")
    requirements: list[Requirement] = Field(default_factory=list)
    deadlines: list[Deadline] = Field(default_factory=list)


_PT = ("residential_new_construction", "nonresidential_new_construction", "residential_alteration",
       "nonresidential_alteration", "hotel_alteration", "demolition")
_IGNORE = {"replacement", "replace", "new", "install", "installation", "project", "projects", "work", "permit",
           "permits", "a", "the", "of", "and", "or", "for", "job", "re"}
_SYN = {"electrical": "electric", "roof": "roofing", "reroof": "roofing", "pools": "pool", "fences": "fence",
        "windows": "window", "doors": "door", "decks": "deck", "repipe": "pipe", "skylights": "skylight",
        "awnings": "awning", "carports": "carport", "stucco": "stucco", "solar": "solar"}


def _project_type(value) -> tuple[str, Optional[str]]:
    """-> (canonical deposit type, None) or ('exempt', the matching San Diego exemption)."""
    w = _words(value)
    if not w:
        raise ValueError("project_type is required, e.g. residential_new_construction, residential_alteration, "
                         "nonresidential_new_construction, nonresidential_alteration, demolition, or an exempt item "
                         "such as roofing or fence")
    if w.replace(" ", "_") in _PT:
        return w.replace(" ", "_"), None
    toks = set(w.split())
    demo = bool(toks & {"demolition", "demo", "teardown", "wrecking", "demolish"})
    hotel = bool(toks & {"hotel", "motel"})
    nonres = bool(toks & {"nonresidential", "commercial", "office", "retail", "industrial", "warehouse", "ti",
                          "restaurant", "nonres"}) or "non residential" in w or "tenant improvement" in w
    res = (bool(toks & {"residential", "home", "house", "sfr", "adu", "apartment", "apartments", "condo", "duplex",
                        "residence", "res", "multifamily"}) or "single family" in w) and not nonres
    new = bool(toks & {"new", "ground"}) or "new construction" in w
    alter = bool(toks & {"alteration", "alterations", "remodel", "renovation", "ti", "improvement", "alter"})
    if demo and not (new or alter):
        return "demolition", None
    if hotel and alter:
        return "hotel_alteration", None
    if (res or nonres) and (new or alter) and not (res and nonres):
        return ("residential_" if res else "nonresidential_") + ("new_construction" if new else "alteration"), None
    exempt = P(SD_CD_EXEMPT, "exempt")
    sig = {_SYN.get(t, t) for t in toks} - _IGNORE
    for item in exempt:
        itoks = {_SYN.get(t, t) for t in _words(item).split()}
        if sig and sig <= itoks:
            return "exempt", item
    raise ValueError(f"project_type {value!r} is not clear. Use one of {', '.join(_PT)}, or an exempt item from the "
                     f"San Diego list (e.g. {', '.join(exempt[:6])}). Additions are not classified by the City's "
                     "table: ask ESD whether yours is new construction or an alteration.")


def _sd_deposit(acc: _Acc, ptype: str, sqft: Optional[float]) -> tuple[Optional[bool], Optional[float], str,
                                                                        Optional[str]]:
    """(deposit_required, deposit_usd, basis, exempt_reason) for San Diego."""
    r = core.rule(SD_CD_DEPOSIT)
    types = r.param("deposit_types")
    acc.use(SD_CD_DEPOSIT)
    flat = types["residential_alterations_flat"]
    key = {"residential_new_construction": "residential_new_construction_nonresidential_alterations_demolition",
           "nonresidential_alteration": "residential_new_construction_nonresidential_alterations_demolition",
           "hotel_alteration": "residential_new_construction_nonresidential_alterations_demolition",
           "demolition": "residential_new_construction_nonresidential_alterations_demolition",
           "nonresidential_new_construction": "nonresidential_new_construction"}.get(ptype)
    if sqft is None:
        return None, None, "give square_feet to compute the deposit", None
    if ptype == "residential_alteration":
        if sqft < flat["min_sq_ft_subject"]:
            return False, None, (f"{_n(sqft)} sq ft is below the {_n(flat['min_sq_ft_subject'])} sq ft minimum in the "
                                 "deposit table"), "below the minimum square footage"
        if sqft <= flat["max_sq_ft_subject"]:
            acc.math.append(f"Residential alteration {_n(sqft)} sq ft is within {_n(flat['min_sq_ft_subject'])}-"
                            f"{_n(flat['max_sq_ft_subject'])} sq ft -> flat {_usd(flat['usd_flat'])}")
            return True, float(flat["usd_flat"]), "flat rate for residential alterations", None
        key = "residential_new_construction_nonresidential_alterations_demolition"
        acc.note(f"Residential alterations over {_n(flat['max_sq_ft_subject'])} sq ft are "
                 f"{r.param('residential_alterations_7000_sq_ft_plus_and_hotels')}.")
    t = types[key]
    if sqft < t["min_sq_ft_subject"]:
        return False, None, (f"{_n(sqft)} sq ft is below the {_n(t['min_sq_ft_subject'])} sq ft minimum in the "
                             "deposit table"), "below the minimum square footage"
    subject = min(sqft, t["max_sq_ft_subject"])
    dep = _money(t["usd_per_sq_ft"] * subject)
    acc.math.append(f"Deposit = {_usd(t['usd_per_sq_ft'])}/sq ft x min({_n(sqft)}, {_n(t['max_sq_ft_subject'])}) sq ft "
                    f"= {_usd(dep)} (published range {_usd(t['range_usd'][0])}-{_usd(t['range_usd'][1])})")
    if sqft > t["max_sq_ft_subject"] and r.param("formula_is_inferred", False):
        acc.note(f"The cap at {_n(t['max_sq_ft_subject'])} sq ft ('{r.param('max_sq_ft_column_label')}') is inferred "
                 "from the published deposit range, not stated; confirm the amount with ESD.")
    floor = P(SD_CD_EXEMPT, "min_deposit_usd")
    if dep < floor:
        acc.use(SD_CD_EXEMPT)
        return False, dep, f"a deposit under {_usd(floor)} is exempt", f"deposit below {_usd(floor)}"
    label = {"residential_new_construction_nonresidential_alterations_demolition":
             "residential new construction, non-residential alterations and demolition",
             "nonresidential_new_construction": "non-residential new construction"}.get(key, key.replace("_", " "))
    return True, dep, f"{_usd(t['usd_per_sq_ft'])} per sq ft ({label})", None


def construction_debris_plan(city, project_type, square_feet=None, total_debris_tons=None, diverted_tons=None,
                             final_inspection_date=None, permit_issued_date=None, hauling: str = "hauler",
                             debris_generated_in_city: bool = True, project_value_usd=None,
                             as_of=None) -> DebrisPlanResult:
    """C&D debris deposit, diversion target, refund and hauling rules for one project."""
    key = normalize_city(city)
    ptype, exempt_item = _project_type(project_type)
    sqft = _num("square_feet", square_feet, lo=0, hi=10_000_000)
    total = _num("total_debris_tons", total_debris_tons, lo=0)
    div = _num("diverted_tons", diverted_tons, lo=0)
    if div is not None and total is None:
        raise ValueError("diverted_tons needs total_debris_tons (all C&D debris generated, in tons)")
    if div is not None and total is not None and div > total + 1e-9:
        raise ValueError(f"diverted_tons ({_n(div)}) cannot exceed total_debris_tons ({_n(total)})")
    final = _date(final_inspection_date, "final_inspection_date")
    issued = _date(permit_issued_date, "permit_issued_date")
    value = _num("project_value_usd", project_value_usd, lo=0)
    h = _words(hauling or "hauler")
    self_haul = bool(re.search(r"\b(self|own|ourselves|myself|we haul|our truck)", h))
    in_city = _bool("debris_generated_in_city", debris_generated_in_city)
    in_city = True if in_city is None else in_city
    today = _date(as_of, "as_of") or logic.today_ca()
    acc = _Acc(today)
    name = _city_name(key)
    res = DebrisPlanResult(city=name, city_rules_verified=key in VERIFIED_CITIES,
                           project_type=ptype if not exempt_item else f"exempt: {exempt_item}", square_feet=sqft)
    new_constr = ptype in ("residential_new_construction", "nonresidential_new_construction")
    target = None
    if key == SD:
        acc.use(SD_CD_APPLIES)
        if exempt_item:
            acc.use(SD_CD_EXEMPT, CALGREEN)
            res.deposit_required, res.exempt_reason = False, exempt_item
            res.deposit_basis = f"exempt from the San Diego C&D deposit: {exempt_item}"
            acc.req("No C&D deposit", core.rule(SD_CD_EXEMPT).plain_english + " CALGreen's 65% diversion can still "
                    "apply to an exempt project if it is covered new construction or a covered addition/alteration.",
                    SD_CD_EXEMPT, CALGREEN)
        else:
            res.deposit_required, res.deposit_usd, res.deposit_basis, res.exempt_reason = _sd_deposit(acc, ptype, sqft)
            pct_new = P(SD_CD_65, "diversion_min_percent_by_weight")
            legacy_end = date.fromisoformat(P(SD_CD_65, "legacy_rate_permits_issued_through"))
            target = P(SD_CD_65, "legacy_rate_percent") if issued and issued <= legacy_end else pct_new
            if res.deposit_required is not False:
                acc.req("Waste Management Form and deposit before the permit",
                        core.rule(SD_CD_WMF).plain_english + f" Form: {P(SD_CD_WMF, 'form_url')}; tons conversion "
                        f"table: {P(SD_CD_WMF, 'conversion_table')}.", SD_CD_WMF)
                acc.req(f"Divert {_n(target)}% by weight", core.rule(SD_CD_65).plain_english, SD_CD_65)
                acc.req("Use certified facilities and keep receipts",
                        core.rule(SD_CD_FACILITIES).plain_english + f" Landfill disposal earns no diversion credit; "
                        f"every receipt must show the {_join(P(SD_CD_FACILITIES, 'receipt_must_show'))}. Directory: "
                        f"{P(SD_CD_FACILITIES, 'facility_directory_url')}.", SD_CD_FACILITIES)
                n = int(P(SD_CD_REFUND, "submit_within_days_after_final_inspection"))
                due = final + timedelta(days=n) if final else None
                acc.req("Request the refund", core.rule(SD_CD_REFUND).plain_english
                        + f" Mail to: {P(SD_CD_REFUND, 'mail_to')}.", SD_CD_REFUND, due=due)
                if final:
                    res.refund_deadline = due.isoformat()
                    acc.math.append(f"Refund request: {final.isoformat()} + {n} days = {due.isoformat()} (the City "
                                    "does not say calendar or business days; calendar days give the earlier date)")
                acc.req("Full or pro-rated refund", core.rule(SD_CD_PRORATE).plain_english, SD_CD_PRORATE)
            acc.req("Hauling", core.rule(SD_CD_HAULER).plain_english + f" Franchise hauler list (rev. "
                    f"{P(SD_CD_HAULER, 'hauler_list_revision')}): {P(SD_CD_HAULER, 'hauler_list_url')}. Whether a "
                    "non-franchised company may haul C&D for hire was not verified.", SD_CD_HAULER)
        acc.req("Cover every load", core.rule(SD_MIRAMAR_COVER).plain_english
                + f" ({_usd(P(SD_MIRAMAR_COVER, 'uncovered_load_fee_usd'))} per uncovered load at Miramar).",
                SD_MIRAMAR_COVER)
        if new_constr:
            acc.use(CALGREEN)
    elif key == LA:
        acc.req("City-certified C&D processors only", core.rule(LA_CD_PROC).plain_english, LA_CD_PROC)
        acc.req("Private Waste Hauler Permit", core.rule(LA_CD_HAULER).plain_english
                + f" Permitted hauler list: {P(LA_CD_HAULER, 'permitted_hauler_list_url')}.", LA_CD_HAULER)
        acc.req("Name the hauler on the building permit", core.rule(LA_PERMIT_HAULER).plain_english, LA_PERMIT_HAULER)
        res.deposit_basis = "the rulebook has no Los Angeles C&D deposit amount (LASAN pages could not be read)"
        if self_haul:
            acc.note("Hauling your own debris in Los Angeles still needs the LASAN Private Waste Hauler Permit.")
    else:
        _other_debris(acc, key, res, ptype, value)
        target = res.diversion_required_percent
    if new_constr or (key not in (SD,) and not exempt_item):
        pc = P(CALGREEN, "min_diversion_percent")
        acc.req(f"CALGreen {pc}% diversion (statewide)", core.rule(CALGREEN).plain_english, CALGREEN)
        target = target if target is not None else pc
        if ptype not in ("residential_new_construction", "nonresidential_new_construction"):
            acc.note("CALGreen 4.408.1/5.408.1 cover new construction and covered additions/alterations only; check "
                     "whether your alteration is covered.")
    res.diversion_required_percent = target
    # tonnage math
    if total is not None and target is not None:
        need = _money(total * target / 100)
        res.tons_to_divert_for_target = need
        acc.math.append(f"Target: {_n(total)} t x {_n(target)}% = {_n(need)} t diverted")
        if div is not None:
            pct = round(div / total * 100, 1) if total else 100.0
            res.diversion_achieved_percent = pct
            res.additional_tons_needed = _money(max(0.0, need - div))
            acc.math.append(f"Achieved: {_n(div)} / {_n(total)} t = {pct:g}%"
                            + (f"; short {_n(res.additional_tons_needed)} t" if res.additional_tons_needed else ""))
            if key == SD and res.deposit_required:
                res.refund_outlook = "full" if div >= need - 1e-9 else "pro_rated"
                if res.refund_outlook == "pro_rated" and not P(SD_CD_PRORATE, "proration_formula_published"):
                    acc.note("The City pro-rates the refund by the recycling rate achieved but does not publish the "
                             "formula, so no partial refund amount is computed.")
    if key == SD and res.deposit_required and res.refund_outlook == "not_applicable":
        res.refund_outlook = "unknown"
    if key == SD and total is not None:
        land = total - (div or 0) if div is not None else None
        if land:
            rate = P(SD_MIRAMAR_RATES, "cd_debris_usd_per_ton")["city_waste" if in_city else "non_city_waste"]
            res.landfill_cost_usd = _money(land * rate)
            acc.use(SD_MIRAMAR_RATES)
            acc.math.append(f"Miramar C&D disposal: {_n(land)} t x {_usd(rate)}/t "
                            f"({'City' if in_city else 'non-City'} waste, from "
                            f"{P(SD_MIRAMAR_RATES, 'effective')}) = {_usd(res.landfill_cost_usd)}; landfilled tons earn "
                            "no diversion credit")
    if self_haul:
        acc.req("Covered loads on the road (UNVERIFIED)", core.rule(VC23115_U).plain_english, VC23115_U)
    acc.finish(res)
    dep = (f"deposit {_usd(res.deposit_usd)}" if res.deposit_required and res.deposit_usd is not None else
           f"no deposit ({res.exempt_reason})" if res.deposit_required is False else
           f"deposit: {res.deposit_basis}" if res.deposit_basis else "")
    div_txt = ""
    if res.diversion_achieved_percent is not None:
        div_txt = (f"; diverted {res.diversion_achieved_percent:g}% vs {_n(target)}% required"
                   + (f" (short {_n(res.additional_tons_needed)} t)" if res.additional_tons_needed else " (met)"))
    elif target is not None:
        div_txt = f"; divert at least {_n(target)}%" + (
            f" ({_n(res.tons_to_divert_for_target)} t of {_n(total)} t)" if res.tons_to_divert_for_target else "")
    res.summary = f"{name}, {res.project_type}: {dep}{div_txt}" + (
        f"; refund {res.refund_outlook.replace('_', '-')}" if key == SD and res.deposit_required else "") + (
        f"; refund request due {res.refund_deadline}" if res.refund_deadline else "") + "."
    if key == SD and res.deposit_required:
        if res.refund_deadline:
            res.next_step = (f"Send WMF Part II, the WMF I copy and every receipt by {res.refund_deadline} "
                             f"({(date.fromisoformat(res.refund_deadline) - today).days} day(s) left)"
                             + (f"; divert {_n(res.additional_tons_needed)} more tons first if you can"
                                if res.additional_tons_needed else "") + ".")
        else:
            res.next_step = ("File Waste Management Form Part I with your debris estimate and pay the deposit at permit "
                             "issuance; send every load to a certified facility and keep receipts showing the project "
                             "address and permit number.")
    elif key == SD and res.deposit_required is None:
        res.next_step = "Give square_feet to compute the deposit you will pay at permit issuance."
    elif key == SD:
        res.next_step = "No San Diego deposit; still recycle and keep receipts if CALGreen applies."
    elif key == LA:
        res.next_step = ("Hire a hauler on LASAN's Permitted Waste Haulers list (or get your own Private Waste Hauler "
                         "Permit) and put its name and permit number on the building permit application.")
    else:
        res.next_step = f"{name} C&D rules are UNVERIFIED (official text not read): confirm the deposit, plan and hauler rules with the city."
    return res


def _other_debris(acc, key, res, ptype, value):
    name = _city_name(key)
    if key == SF:
        acc.req("C&D transporter permit (UNVERIFIED)", core.rule(SF_TRANSPORTER_U).plain_english, SF_TRANSPORTER_U)
    elif key == SJ:
        a = P(SJ_CDDD_U, "applies_to")
        acc.req("C&D Diversion Deposit (UNVERIFIED)", core.rule(SJ_CDDD_U).plain_english
                + f" Reported to apply to residential alterations valued at ${_n(a['residential_alteration_valuation_usd_min'])}+,"
                f" non-residential alterations at ${_n(a['nonresidential_alteration_valuation_usd_min'])}+ and all "
                "demolitions; the amount (by square footage) was not found.", SJ_CDDD_U)
        acc.req("Authorized haulers only (UNVERIFIED)", core.rule(SJ_HAULERS_U).plain_english, SJ_HAULERS_U)
        if value is not None and ptype in ("residential_alteration", "nonresidential_alteration", "demolition"):
            lim = (a["residential_alteration_valuation_usd_min"] if ptype == "residential_alteration" else
                   a["nonresidential_alteration_valuation_usd_min"] if ptype == "nonresidential_alteration" else 0)
            res.deposit_required = ptype == "demolition" or value >= lim
            acc.math.append(f"San Jose CDDD trigger: valuation {_usd(value)} vs {_usd(lim)} -> "
                            f"{'applies' if res.deposit_required else 'does not apply'} (unverified)")
        res.deposit_basis = "San Jose deposit amount not in the rulebook"
    elif key == SAC:
        pct = P(SAC_CD_U, "diversion_min_percent")
        res.diversion_required_percent = pct
        acc.req("65% diversion and waste log (UNVERIFIED)", core.rule(SAC_CD_U).plain_english, SAC_CD_U)
        acc.req("Franchised hauler or self-haul (UNVERIFIED)", core.rule(SAC_HAULER_U).plain_english, SAC_HAULER_U)
        acc.req("Green Halo plan and admin fee (UNVERIFIED)", core.rule(SAC_FEE_U).plain_english, SAC_FEE_U)
        if value is not None:
            pc, mn = P(SAC_FEE_U, "admin_fee_percent_of_valuation"), P(SAC_FEE_U, "admin_fee_min_usd")
            res.city_fee_usd = _money(max(mn, value * pc / 100))
            acc.math.append(f"Sacramento admin fee = max({_usd(mn)}, {pc:g}% x {_usd(value)}) = "
                            f"{_usd(res.city_fee_usd)} (unverified)")
    elif key == OAK:
        res.diversion_required_percent = P(OAK_CD_U, "other_materials_percent")
        acc.req("100% asphalt/concrete, 65% the rest (UNVERIFIED)", core.rule(OAK_CD_U).plain_english, OAK_CD_U)
        acc.req("Waste Reduction and Recycling Plan (UNVERIFIED)", core.rule(OAK_WRRP_U).plain_english, OAK_WRRP_U)
        acc.req("Non-Exclusive Franchise haulers (UNVERIFIED)", core.rule(OAK_HAULER_U).plain_english, OAK_HAULER_U)
    elif key == LB:
        res.diversion_required_percent = P(LB_CD_U, "diversion_min_percent")
        acc.req("65% diversion (UNVERIFIED)", core.rule(LB_CD_U).plain_english, LB_CD_U)
        acc.req("C&D deposit (UNVERIFIED)", core.rule(LB_DEPOSIT_U).plain_english, LB_DEPOSIT_U)
        acc.req("Permitted hauler or self-haul (UNVERIFIED)", core.rule(LB_HAULER_U).plain_english, LB_HAULER_U)
        acc.req("Refund documents (UNVERIFIED)", core.rule(LB_DOCS_U).plain_english, LB_DOCS_U)
        if value is not None:
            pc, cap, mn = (P(LB_DEPOSIT_U, "deposit_percent_of_project_value"), P(LB_DEPOSIT_U, "deposit_cap_usd"),
                           P(LB_DEPOSIT_U, "deposit_min_usd"))
            res.city_fee_usd = _money(max(mn, min(value * pc / 100, cap)))
            res.deposit_required, res.deposit_usd = True, res.city_fee_usd
            acc.math.append(f"Long Beach deposit = max({_usd(mn)}, min({pc:g}% x {_usd(value)}, {_usd(cap)})) = "
                            f"{_usd(res.city_fee_usd)} (unverified)")
        res.deposit_basis = "Long Beach: 3% of project value, capped, with a minimum (unverified)"
    elif key == FRE:
        acc.req("NERO franchise haulers only (UNVERIFIED)", core.rule(FRE_HAULERS_U).plain_english
                + f" Haulers listed Feb. 2026: {_join(P(FRE_HAULERS_U, 'approved_haulers_listed_feb_2026'))}.",
                FRE_HAULERS_U)
    if not res.deposit_basis:
        res.deposit_basis = f"no {name} C&D deposit in the rulebook"


# ================================================================ 4. San Diego right-of-way fees
class RowFeesResult(_Base):
    blocks: int = 0
    weeks: float = 0
    extension_weeks: int = 0
    extension_requests: int = 0
    new_ds269_required: bool = False
    fees: list[FeeLine] = Field(default_factory=list)
    total_usd: float = 0.0


def san_diego_right_of_way_fees(blocks=0, weeks=None, extension_requests=None, modifications=0,
                                modification_new_sheets=0, metered_spaces=0, meter_days=0, meter_posts_removed=0,
                                pay_stations_removed=0, dumpster_weeks=None, storage_container_days=None,
                                noise_permit_applications=0) -> RowFeesResult:
    """IB 177 / IB 718 fee estimate for San Diego street work and bins, line by line."""
    nb = int(_num("blocks", blocks, lo=0, hi=500, integer=True) or 0)
    wk = _num("weeks", weeks, lo=0, hi=520)
    er = _num("extension_requests", extension_requests, lo=0, hi=100, integer=True)
    mods = int(_num("modifications", modifications, lo=0, hi=100, integer=True) or 0)
    sheets = int(_num("modification_new_sheets", modification_new_sheets, lo=0, hi=100, integer=True) or 0)
    spaces = int(_num("metered_spaces", metered_spaces, lo=0, hi=1000, integer=True) or 0)
    mdays = int(_num("meter_days", meter_days, lo=0, hi=3650, integer=True) or 0)
    posts = int(_num("meter_posts_removed", meter_posts_removed, lo=0, hi=1000, integer=True) or 0)
    stations = int(_num("pay_stations_removed", pay_stations_removed, lo=0, hi=1000, integer=True) or 0)
    dweeks = _num("dumpster_weeks", dumpster_weeks, lo=0, hi=520)
    cdays = _num("storage_container_days", storage_container_days, lo=0, hi=3650)
    npa = int(_num("noise_permit_applications", noise_permit_applications, lo=0, hi=100, integer=True) or 0)
    if nb and not wk:
        raise ValueError("weeks is required with blocks: how many weeks the traffic control will be in place")
    if (sheets or mods or er) and not nb:
        raise ValueError("extension_requests, modifications and modification_new_sheets need blocks (a Traffic "
                         "Control Permit)")
    if (spaces and not mdays) or (mdays and not spaces):
        raise ValueError("metered_spaces and meter_days go together (spaces x days)")
    if not any([nb, spaces, posts, stations, dweeks, cdays, npa]):
        raise ValueError("Nothing to price: give blocks and weeks, metered spaces, a dumpster, a storage container or "
                         "noise permit applications")
    acc = _Acc(logic.today_ca())
    res = RowFeesResult(city=_city_name(SD), city_rules_verified=True, blocks=nb, weeks=wk or 0)
    if nb:
        info = _sd_tcp_fees(acc, nb, wk, None if er is None else int(er), mods, sheets)
        res.extension_weeks, res.extension_requests = info["extension_weeks"], info["extension_requests"]
        lim = int(P(SD_TCP_EXT, "new_ds269_after_extensions"))
        res.new_ds269_required = res.extension_requests > lim
        acc.use(SD_TCP_EXT)
        acc.math.append(f"Extension weeks = ceil({_n(wk)} - {P(SD_TCP_FEES, 'initial_period_weeks_max')}) = "
                        f"{res.extension_weeks}; each extension request covers up to "
                        f"{P(SD_TCP_FEES, 'extension_weeks_max')} weeks (IB 177), so at least {info['min_requests']} "
                        f"request(s); {res.extension_requests} used")
        if res.new_ds269_required:
            acc.note(f"After {lim} extensions the City requires a new, signed DS-269.")
    meters = P(SD_TCP_FEES, "parking_meters_usd")
    if spaces:
        acc.fee("Metered spaces out of use", meters["per_metered_space_per_day"] * spaces * mdays,
                f"{_usd(meters['per_metered_space_per_day'])} x {spaces} space(s) x {mdays} day(s)", SD_TCP_FEES)
        acc.note(f"IB 177 also lists 'Meter covered (per day) {_usd(meters['meter_covered_per_day'])}'; ask DSD "
                 "whether a covered meter is billed under one line or both. Only the per-space line is counted.")
    if posts:
        acc.fee("Meter head post removal", meters["single_or_dual_meter_head_post_removal"] * posts,
                f"{_usd(meters['single_or_dual_meter_head_post_removal'])} x {posts}", SD_TCP_FEES)
    if stations:
        acc.fee("Pay station removal", meters["pay_station_removal"] * stations,
                f"{_usd(meters['pay_station_removal'])} x {stations}", SD_TCP_FEES)
    if dweeks:
        _sd_bin_fees(acc, "dumpster", weeks=dweeks)
    if cdays:
        _sd_bin_fees(acc, "storage_container", days=cdays)
    if npa:
        fee = P(SD_NP_FEE, "fee_usd")
        acc.fee("Construction Noise Permit", fee * npa, f"{_usd(fee)} x {npa} application(s) ({P(SD_NP_FEE, 'per')})",
                SD_NP_FEE)
    acc.note(f"Inspection fees are not included: {P(SD_TCP_FEES, 'inspection_fees')}.")
    if nb:
        acc.use(SD_TCP_5WD, SD_TCP_15BD)
        res.next_step = ("File in Accela at least "
                         f"{P(SD_TCP_5WD, 'min_working_days_before_start')} working days before work (allow about "
                         f"{P(SD_TCP_15BD, 'typical_processing_business_days')} business days to process) and pay "
                         "when DSD invoices.")
    else:
        res.next_step = "Apply in Accela and pay when DSD invoices the permit."
    acc.finish(res)
    res.total_usd = _money(sum(x.amount_usd for x in res.fees))
    res.math.append(" + ".join(f"{x.item} {_usd(x.amount_usd)}" for x in res.fees) + f" = {_usd(res.total_usd)}")
    res.summary = f"San Diego right-of-way fees: {_usd(res.total_usd)} ({len(res.fees)} line(s))."
    return res


# ================================================================ MCP
RULE_IDS = tuple(dict.fromkeys(v for k, v in list(globals().items())
                               if k.isupper() and isinstance(v, str) and (v.startswith("local-") or
                                                                         v in (CALGREEN, USA811, NESHAP))
                               and v not in NOT_USED))


def _tool_docs() -> dict:
    win = P(SD_WINDOWS, "no_noise_permit_window")
    la = P(LA_HOURS, "permitted_hours")
    tcp = core.rule(SD_TCP_FEES)
    dep = P(SD_CD_DEPOSIT, "deposit_types")
    others = ", ".join(_city_name(k) for k in _ALIASES if k not in VERIFIED_CITIES)
    return {
        "hours": (
            "Is construction allowed at this date and time in this California city, and if not, what does it take? "
            f"San Diego: {_clock(_m(win['start']))}-{_clock(_m(win['end']))} Monday-Saturday without a Construction "
            f"Noise Permit (fee {_usd(P(SD_NP_FEE, 'fee_usd'))}); nights, Sundays and City holidays (the City's "
            f"{P(SD_HOLIDAYS, 'year')} list) need the permit, with the dated application and 72-hour/500-ft notice "
            f"steps. Los Angeles: Monday-Friday {_clock(_m(la['monday_friday']['start']))}-"
            f"{_clock(_m(la['monday_friday']['end']))}, Saturday and national holidays "
            f"{_clock(_m(la['saturday']['start']))}-{_clock(_m(la['saturday']['end']))}, no Sunday work; HCR hillside "
            f"districts stricter. Also {others} (UNVERIFIED). work_type: construction, interior, delivery, "
            "street_work (right-of-way / traffic control hours) or bin_service (San Diego refuse-vehicle hours). "
            "Returns the status at that time, the day's windows, the next no-permit time, the penalty, a next step "
            "and citations. Accepts 'SD', 'City of San Diego', 'L.A.', '2026-11-27 6:30 pm'."),
        "jobsite": (
            "City permits, notices and dated deadlines for one California jobsite from simple yes/no facts: work in "
            "the public right-of-way (San Diego Traffic Control Permit: file at least "
            f"{P(SD_TCP_5WD, 'min_working_days_before_start')} working days ahead, about "
            f"{P(SD_TCP_15BD, 'typical_processing_business_days')} business days to process; Right-of-Way Permit), a "
            "dumpster or storage container in the street, night/Sunday/holiday work (noise permit), trenching (811, "
            "moratorium streets), traffic signals, survey monuments, Los Angeles hillside import/export over "
            f"{_n(P(LA_HAUL_HEARING, 'trigger_cubic_yards_over'))} cy (BBSC haul route hearing, fee computed), and "
            f"demolition (Los Angeles {P(LA_DEMO_45, 'building_age_years_over')}-year notice, placard, C-21/A, NESHAP "
            "10 working days). Give start_date to get the due dates; blocks + weeks_in_street price the San Diego "
            "TCP. Returns requirements, sorted deadlines with days left, fees with the math, a next step and "
            "citations."),
        "debris": (
            "Construction & demolition (C&D) debris plan for a California project. San Diego: refundable deposit "
            f"({_usd(dep['residential_new_construction_nonresidential_alterations_demolition']['usd_per_sq_ft'])}/sq ft "
            f"residential new, non-residential alterations and demolition; "
            f"{_usd(dep['nonresidential_new_construction']['usd_per_sq_ft'])}/sq ft non-residential new; "
            f"{_usd(dep['residential_alterations_flat']['usd_flat'])} flat for residential alterations "
            f"{_n(dep['residential_alterations_flat']['min_sq_ft_subject'])}-"
            f"{_n(dep['residential_alterations_flat']['max_sq_ft_subject'])} sq ft; exemptions such as roofing, "
            f"fences and electrical-only), {_n(P(SD_CD_65, 'diversion_min_percent_by_weight'))}% diversion by weight, "
            f"refund request within {P(SD_CD_REFUND, 'submit_within_days_after_final_inspection')} days of final "
            "inspection, certified facilities, hauling and Miramar landfill cost. Los Angeles: certified processors, "
            f"LASAN hauler permit. {others}: UNVERIFIED rules (Long Beach deposit and Sacramento fee computed from "
            "project_value_usd). Give total_debris_tons and diverted_tons for the shortfall and refund outlook."),
        "fees": (
            "San Diego right-of-way fee estimate (IB 177 May 2026, IB 718 July 2026): Traffic Control Initial "
            f"{_usd(tcp.param('initial_fee_usd_per_block'))}/block (up to {tcp.param('initial_period_weeks_max')} "
            f"weeks), Extension {_usd(tcp.param('extension_fee_usd_per_block_per_week'))}/block/week (each request up "
            f"to {tcp.param('extension_weeks_max')} weeks), Processing {_usd(tcp.param('processing_fee_usd_flat'))} "
            "per request (initial, each extension, each modification), metered spaces "
            f"{_usd(tcp.param('parking_meters_usd.per_metered_space_per_day'))}/space/day, meter post and pay station "
            f"removal, dumpster {_usd(P(SD_DUMP, 'dumpster_fee_usd'))} per period, storage container "
            f"{_usd(P(SD_DUMP, 'storage_container_fee_usd_per_week'))}/week, and Construction Noise Permits "
            f"{_usd(P(SD_NP_FEE, 'fee_usd'))} each. Returns each line with its math and the total."),
    }


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    city_desc = ("City of the jobsite: San Diego or Los Angeles (verified); San Francisco, San Jose, Sacramento, "
                 "Oakland, Long Beach or Fresno (unverified). 'SD', 'City of San Diego', 'L.A.' work")
    date_desc = "ISO date; default today in California"

    @mcp.tool(name="construction_hours", annotations=ro, description=docs["hours"])
    def _construction_hours(
        city: Annotated[str, Field(min_length=1, description=city_desc)],
        when: Annotated[Optional[str], Field(description=(
            "California local date and time to check, e.g. '2026-11-27T06:30' or '2026-11-27 6:30 pm'; a date "
            "alone returns the whole day; default now"))] = None,
        work_type: Annotated[str, Field(description=(
            "construction (default; also demolition, excavation, grading), interior, delivery, street_work (work "
            "in the street, sidewalk or alley under traffic control) or bin_service (dumpster swap / refuse pickup, "
            "San Diego)"))] = "construction",
        hcr_district: Annotated[bool, Field(description=(
            "Los Angeles only: the site is in a Hillside Construction Regulation (HCR) district"))] = False,
        residential_area: Annotated[bool, Field(description=(
            "San Diego bin_service: the site is in a residential area"))] = True,
        is_holiday: Annotated[Optional[bool], Field(description=(
            "Is the date a holiday for this city (San Diego City holiday; Los Angeles national holiday)? Leave empty "
            "to use the city's published list (San Diego 2026); Los Angeles publishes none"))] = None,
        emergency_work: Annotated[bool, Field(description=(
            "San Diego: emergency work (adds the 48-hour notice for the daytime noise-limit exemption)"))] = False,
        as_of: Annotated[Optional[str], Field(description=date_desc + " (for days left on permit steps)")] = None,
    ) -> ConstructionHoursResult:
        return construction_hours(city, when, work_type, hcr_district, residential_area, is_holiday, emergency_work,
                                  as_of)

    nn = {"ge": 0, "allow_inf_nan": False}

    @mcp.tool(name="local_jobsite_requirements", annotations=ro, description=docs["jobsite"])
    def _local_jobsite_requirements(
        city: Annotated[str, Field(min_length=1, description=city_desc)],
        start_date: Annotated[Optional[str], Field(description="ISO date work (or bin placement) starts")] = None,
        work_in_public_right_of_way: Annotated[bool, Field(description=(
            "Any work or equipment in the street, sidewalk, parking lane, median or alley (lane closure, crane, "
            "concrete pump, sidewalk closure)"))] = False,
        builds_in_right_of_way: Annotated[bool, Field(description=(
            "Construction in the right-of-way itself: curb, gutter, sidewalk, driveway, utility or street work"))]
        = False,
        uses_street_for_bin: Annotated[bool, Field(description=(
            "A dumpster or storage container will sit in the street or sidewalk area"))] = False,
        bin_type: Annotated[str, Field(description="dumpster (roll-off / debris box) or storage_container (pod)")]
        = "dumpster",
        night_sunday_or_holiday_work: Annotated[bool, Field(description=(
            "Any work outside the city's daytime hours, on Sundays or on holidays"))] = False,
        trenching_in_street: Annotated[bool, Field(description="Trenching or excavation in a street")] = False,
        affects_traffic_signals: Annotated[bool, Field(description="Work affects traffic signals")] = False,
        disturbs_survey_monument: Annotated[bool, Field(description=(
            "A vertical control survey monument will be disturbed or destroyed (San Diego)"))] = False,
        private_improvement_stays_in_row: Annotated[bool, Field(description=(
            "A privately owned or maintained improvement will remain in the right-of-way (San Diego EMRA)"))] = False,
        earthwork_cubic_yards: Annotated[Optional[float], Field(**nn, description=(
            "Earth imported to or exported from the site, cubic yards"))] = None,
        hillside_grading_area: Annotated[bool, Field(description=(
            "Los Angeles: the site is in a designated Hillside Grading Area (ZIMAS)"))] = False,
        eo1_wildfire_rebuild: Annotated[bool, Field(description=(
            "Los Angeles: an Emergency Executive Order No. 1 wildfire-rebuild project"))] = False,
        hcr_district: Annotated[bool, Field(description=(
            "Los Angeles: the site is in a Hillside Construction Regulation (HCR) district"))] = False,
        demolition: Annotated[bool, Field(description="The job includes demolishing a building")] = False,
        building_age_years: Annotated[Optional[float], Field(**nn, description=(
            "Age of the building to be demolished, years (Los Angeles 45-year notice)"))] = None,
        contractor_license_classes: Annotated[Optional[list[str]], Field(description=(
            "Your CSLB classes, e.g. ['B', 'C-21'] (Los Angeles demolition permit eligibility)"))] = None,
        blocks: Annotated[Optional[int], Field(ge=0, le=500, description=(
            "San Diego: city blocks under traffic control (prices the TCP)"))] = None,
        weeks_in_street: Annotated[Optional[float], Field(**nn, description=(
            "Weeks the traffic control or bin stays in the street"))] = None,
        as_of: Annotated[Optional[str], Field(description=date_desc)] = None,
    ) -> JobsiteRequirementsResult:
        return local_jobsite_requirements(
            city, start_date, work_in_public_right_of_way, builds_in_right_of_way, uses_street_for_bin, bin_type,
            night_sunday_or_holiday_work, trenching_in_street, affects_traffic_signals, disturbs_survey_monument,
            private_improvement_stays_in_row, earthwork_cubic_yards, hillside_grading_area, eo1_wildfire_rebuild,
            hcr_district, demolition, building_age_years, contractor_license_classes, blocks, weeks_in_street, as_of)

    @mcp.tool(name="construction_debris_plan", annotations=ro, description=docs["debris"])
    def _construction_debris_plan(
        city: Annotated[str, Field(min_length=1, description=city_desc)],
        project_type: Annotated[str, Field(min_length=1, description=(
            "residential_new_construction, nonresidential_new_construction, residential_alteration, "
            "nonresidential_alteration, hotel_alteration, demolition, or a San Diego exempt item (roofing, fence, "
            "deck, window, electric-only ...); plain words like 'commercial TI' or 'new home' work"))],
        square_feet: Annotated[Optional[float], Field(**nn, description="Project area, sq ft (San Diego deposit)")]
        = None,
        total_debris_tons: Annotated[Optional[float], Field(**nn, description="All C&D debris generated, tons")]
        = None,
        diverted_tons: Annotated[Optional[float], Field(**nn, description=(
            "Tons recycled, reused or donated with receipts"))] = None,
        final_inspection_date: Annotated[Optional[str], Field(description="ISO date final inspection was passed")]
        = None,
        permit_issued_date: Annotated[Optional[str], Field(description="ISO date the permit was issued")] = None,
        hauling: Annotated[str, Field(description="'hauler' (default) or 'self-haul'")] = "hauler",
        debris_generated_in_city: Annotated[bool, Field(description=(
            "San Diego: debris generated inside the City (Miramar City-waste rate)"))] = True,
        project_value_usd: Annotated[Optional[float], Field(**nn, description=(
            "Project value/valuation, USD (Long Beach deposit, Sacramento fee, San Jose trigger)"))] = None,
        as_of: Annotated[Optional[str], Field(description=date_desc)] = None,
    ) -> DebrisPlanResult:
        return construction_debris_plan(city, project_type, square_feet, total_debris_tons, diverted_tons,
                                        final_inspection_date, permit_issued_date, hauling, debris_generated_in_city,
                                        project_value_usd, as_of)

    @mcp.tool(name="san_diego_right_of_way_fees", annotations=ro, description=docs["fees"])
    def _san_diego_right_of_way_fees(
        blocks: Annotated[int, Field(ge=0, le=500, description="City blocks under traffic control")] = 0,
        weeks: Annotated[Optional[float], Field(**nn, description="Weeks of traffic control in total")] = None,
        extension_requests: Annotated[Optional[int], Field(ge=0, le=100, description=(
            "Extension requests you will file (default: the fewest that cover the weeks)"))] = None,
        modifications: Annotated[int, Field(ge=0, le=100, description="Modifications to the approved TCP")] = 0,
        modification_new_sheets: Annotated[int, Field(ge=0, le=100, description=(
            "New plan sheets added by modifications"))] = 0,
        metered_spaces: Annotated[int, Field(ge=0, le=1000, description="Metered parking spaces taken out of use")]
        = 0,
        meter_days: Annotated[int, Field(ge=0, le=3650, description="Days those spaces are out of use")] = 0,
        meter_posts_removed: Annotated[int, Field(ge=0, le=1000, description="Meter head posts removed")] = 0,
        pay_stations_removed: Annotated[int, Field(ge=0, le=1000, description="Pay stations removed")] = 0,
        dumpster_weeks: Annotated[Optional[float], Field(**nn, description="Weeks a dumpster sits in the street")]
        = None,
        storage_container_days: Annotated[Optional[float], Field(**nn, description=(
            "Days a storage container (pod) sits in the street"))] = None,
        noise_permit_applications: Annotated[int, Field(ge=0, le=100, description=(
            "Construction Noise Permit applications (each extension is a new one)"))] = 0,
    ) -> RowFeesResult:
        return san_diego_right_of_way_fees(blocks, weeks, extension_requests, modifications, modification_new_sheets,
                                           metered_spaces, meter_days, meter_posts_removed, pay_stations_removed,
                                           dumpster_weeks, storage_container_days, noise_permit_applications)
