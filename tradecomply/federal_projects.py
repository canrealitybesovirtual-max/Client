"""Federal and federally funded construction in California, on top of the verified pack
research/web_federal-works.verified.json (rule ids "fed-...").

  federal_project_requirements   prime contract value + funding source (+ awarding agency, your role, dates) -> which
                                 federal rules apply and which do not at this value (the threshold math for each):
                                 Davis-Bacon, certified payroll, CWHSSA overtime, Miller Act or 2 CFR 200.326 bonds with
                                 dollar amounts, SAM / E-Verify, subcontracting plan and self-performance, Buy
                                 American / BABA / FHWA Buy America, DBE (with the 2025-2026 pause), prompt payment,
                                 required clauses, California dual coverage, dated key steps and one concrete next step
  davis_bacon_vs_ca_prevailing   the SAM.gov wage determination and the DIR determination for one classification
                                 (+ what you pay) -> the cash floor and total package to pay, overtime minimums and the
                                 shortfall
  federal_overtime_exposure      a timesheet -> CWHSSA overtime owed and liquidated damages per worker per day, plus the
                                 California Labor Code 1813 penalty on dual-coverage jobs
  buy_america_check              one product or material -> does it meet BABA (2 CFR 184), FAR Buy American (52.225-9)
                                 or FHWA Buy America (23 CFR 635.410), with the domestic-content math and the
                                 waiver / exception test

Every legal number, date and threshold is read from the rulebook with core.rule(id).param(key) at call time, and every
rule a result relies on is cited through core.Result.cite(), so `verified` is True only when all of them are verified.
Thresholds that the October 1, 2025 FAR inflation rule changed (CWHSSA clause, simplified acquisition threshold,
subcontracting plan, self-performance clause, anti-lobbying certification) are picked by the as_of date from the
current and prior values the rulebook carries. The one unverifiable pack rule (EO 14026 minimum wage status) is shown
only when include_unverified is true and then makes the result verified=False.

California rules from other packs used here: 8 CCR 16001(b) dual coverage (in this pack), the LC 1815 8/40 overtime
rule, the LC 1813 penalty, LC 1771.4 eCPR uploads and B&P 7108.5 prompt payment.

Not legal values (so not in the rulebook): the spellings that map a user's words onto a funding source, agency, work
type or product category, and the 14-day "due soon" display window.
"""
import csv
import io
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from . import core, logic

# ---------------------------------------------------------------- rule ids
# Davis-Bacon and Related Acts (29 CFR parts 1, 3 and 5; FAR subpart 22.4)
DBA, DBA_PAY, DBA_POST, DBA_CONFORM, DBA_OPLAW = (
    "fed-dba-threshold-2000", "fed-dba-pay-weekly-full-wd-rates", "fed-dba-wd-and-wh1321-posting",
    "fed-dba-conformance-30-days", "fed-dba-operation-of-law")
DBA_RECORDS, DBA_DOCS, DBA_CPR, DBA_WH347, DBA_SOC = (
    "fed-dba-payroll-records-3-years", "fed-dba-contract-documents-3-years", "fed-dba-certified-payroll-weekly",
    "fed-dba-certified-payroll-contents-wh347", "fed-dba-statement-of-compliance")
DBA_APPR, DBA_APPR_RATIO, DBA_FLOW, SF1413 = (
    "fed-dba-apprentice-registered-only", "fed-dba-apprentice-ratio-locality", "fed-dba-subcontract-flowdown",
    "fed-far-sf1413-14-days")
DBA_ELIG, DBA_DEBAR, DBA_MATERIAL, DBA_DEMO, DBA_WORK, DBA_FOREPERSON = (
    "fed-dba-certification-of-eligibility", "fed-dba-debarment-3-years", "fed-dba-material-supplier",
    "fed-dba-demolition-covered", "fed-dba-building-or-work-solar-ev-broadband",
    "fed-dba-laborer-mechanic-forepersons-20pct")
FRINGE, FRINGE_QTR, FRINGE_ANNUAL, FRINGE_NO_CREDIT, OT_BASE = (
    "fed-dba-fringe-cash-or-plan", "fed-dba-fringe-quarterly-contributions", "fed-dba-fringe-annualization",
    "fed-dba-no-credit-legally-required-benefits", "fed-dba-overtime-on-basic-rate")
WD_BID, WD_SAM, WD_TYPES = ("fed-wd-10-day-bid-rule-90-day-award", "fed-wd-published-on-sam-gov",
                            "fed-wd-construction-type-schedules")
COPE_7, COPE_2000 = "fed-copeland-submit-7-days", "fed-copeland-2000-exemption"
# Contract Work Hours and Safety Standards Act
CWH_OT, CWH_LD, CWH_FAR, CWH_FLOW, CWH_SAFETY = (
    "fed-cwhssa-overtime-over-40", "fed-cwhssa-liquidated-damages-33", "fed-cwhssa-far-clause-200k",
    "fed-cwhssa-subcontract-flowdown", "fed-cwhssa-safety-conditions")
# bonds (Miller Act, FAR part 28, 2 CFR 200.326)
MILLER, MILLER_ALT, BONDS_NTP, BID_GUAR = (
    "fed-miller-bonds-over-150k", "fed-miller-alternative-payment-protection", "fed-bonds-before-notice-to-proceed",
    "fed-bid-guarantee-20pct-max-3m")
BOND_WAIVER, BOND_COPY, SURETY, ASSIST_BOND = (
    "fed-payment-bond-waiver-void", "fed-payment-bond-copy-on-request", "fed-surety-treasury-570",
    "fed-assisted-bonding-over-sat")
# FAR thresholds, subcontracting, registration
FAR_THRESH, NOT_ADJUSTED, SUBK_PLAN, SELF_PERF = (
    "fed-far-thresholds-2025-mpt-sat", "fed-far-1109-statutory-thresholds-not-adjusted",
    "fed-subcontracting-plan-900k-2m", "fed-self-performance-clause-2m")
EV_PRIME, EV_ENROLL, EV_SUB = ("fed-everify-prime-threshold-150k", "fed-everify-enroll-30-days-verify",
                               "fed-everify-subcontract-3500")
SAM_REG, SAM_ANNUAL, UEI, SAM_EXCL = ("fed-sam-registration-at-offer-and-award", "fed-sam-maintain-annual-update",
                                      "fed-uei-subrecipients-not-contractors", "fed-sam-exclusions-check")
# payments
PP_SUBS, PP_INTEREST, PP_RETAINAGE, GOV_RET = (
    "fed-prompt-pay-subs-7-days", "fed-prompt-pay-interest-owed-to-government", "fed-prompt-pay-retainage-allowed",
    "fed-gov-retainage-10pct")
# domestic content
BABA, BABA_IRON, BABA_MFG, BABA_CM, BABA_EXCL, BABA_WAIVER, BABA_STAFFORD = (
    "fed-baba-applies-all-four-categories", "fed-baba-iron-steel-all-processes", "fed-baba-manufactured-products-55pct",
    "fed-baba-construction-materials-standards", "fed-baba-excluded-materials-classification",
    "fed-baba-waivers-25pct-comment-periods", "fed-baba-stafford-act-exemption")
DOM_PREF, FAR_BA, TELECOM = ("fed-domestic-preference-200-322", "fed-far-buy-american-construction-65pct",
                             "fed-telecom-889-grant-prohibition")
# Federal-aid highway (23 CFR 635, former 633)
FHWA_STEEL, FHWA_MFG, FHWA_ALT, FHWA_30, FHWA_SUBS, FHWA_1273 = (
    "fed-fhwa-buy-america-steel-minimal-use", "fed-fhwa-buy-america-manufactured-products",
    "fed-fhwa-buy-america-alternate-bids-25pct", "fed-fhwa-30pct-self-perform",
    "fed-fhwa-written-authorized-subcontracts", "fed-fhwa-23cfr633-removed-2026")
# USDOT DBE program (49 CFR part 26)
DBE_PAY, DBE_RET, DBE_ASSURE, DBE_GOALS, DBE_REEVAL = (
    "fed-dbe-prompt-pay-30-days", "fed-dbe-retainage-30-days", "fed-dbe-assurance-clause",
    "fed-dbe-no-contract-goals-until-reevaluation", "fed-dbe-reevaluation-deadlines")
DBE_GFE, DBE_TERM, DBE_COUNT, DBE_BIDDERS = (
    "fed-dbe-gfe-submission-5-days", "fed-dbe-termination-consent-5-day-notice",
    "fed-dbe-count-only-when-certified-and-paid", "fed-dbe-bidders-list-data")
# required clauses, executive orders
APPX_II, BYRD, EO14026, EO13706 = ("fed-appendix-ii-contract-thresholds", "fed-byrd-anti-lobbying-certification",
                                   "fed-eo14026-status-2026", "fed-eo13706-paid-sick-leave")
# California dual coverage (8 CCR 16001(b); DLSE Public Works Manual)
CA_HIGHER, CA_BASIC, CA_REPORT = ("fed-ca-dual-coverage-pay-higher", "fed-ca-dual-coverage-basic-rate",
                                  "fed-ca-dual-reporting-wh347-and-ecpr")
# California rules from other packs
ECPR, BP7108, PW_OT, PEN_1813 = ("ecpr-monthly-submission", "bp-7108-5-pay-subs-7-days",
                                 "pw-overtime-8-40-time-and-half", "penalty-1813-overtime")

RULE_IDS = (DBA, DBA_PAY, DBA_POST, DBA_CONFORM, DBA_OPLAW, DBA_RECORDS, DBA_DOCS, DBA_CPR, DBA_WH347, DBA_SOC,
            DBA_APPR, DBA_APPR_RATIO, DBA_FLOW, SF1413, DBA_ELIG, DBA_DEBAR, DBA_MATERIAL, DBA_DEMO, DBA_WORK,
            DBA_FOREPERSON, FRINGE, FRINGE_QTR, FRINGE_ANNUAL, FRINGE_NO_CREDIT, OT_BASE, WD_BID, WD_SAM, WD_TYPES,
            COPE_7, COPE_2000, CWH_OT, CWH_LD, CWH_FAR, CWH_FLOW, CWH_SAFETY, MILLER, MILLER_ALT, BONDS_NTP, BID_GUAR,
            BOND_WAIVER, BOND_COPY, SURETY, ASSIST_BOND, FAR_THRESH, NOT_ADJUSTED, SUBK_PLAN, SELF_PERF, EV_PRIME,
            EV_ENROLL, EV_SUB, SAM_REG, SAM_ANNUAL, UEI, SAM_EXCL, PP_SUBS, PP_INTEREST, PP_RETAINAGE, GOV_RET, BABA,
            BABA_IRON, BABA_MFG, BABA_CM, BABA_EXCL, BABA_WAIVER, BABA_STAFFORD, DOM_PREF, FAR_BA, TELECOM, FHWA_STEEL,
            FHWA_MFG, FHWA_ALT, FHWA_30, FHWA_SUBS, FHWA_1273, DBE_PAY, DBE_RET, DBE_ASSURE, DBE_GOALS, DBE_REEVAL,
            DBE_GFE, DBE_TERM, DBE_COUNT, DBE_BIDDERS, APPX_II, BYRD, EO14026, EO13706, CA_HIGHER, CA_BASIC, CA_REPORT,
            ECPR, BP7108, PW_OT, PEN_1813)

# Pack rules no tool computes with, and why. Every other "fed-" rule is in RULE_IDS.
NOT_USED = {
    "fed-dba-split-classification-pay": "payroll-recording rule (split rates need hours per classification); no "
                                        "number to compute",
    "fed-dba-interest-on-back-wages": "the IRS underpayment rate it uses is not in the rulebook, so interest is not "
                                      "computed",
    "fed-dba-withholding-cross-contract": "agency enforcement remedy, not a contractor step",
    "fed-dba-withheld-funds-dol-priority": "priority of withheld funds among creditors; enforcement detail",
    "fed-dba-false-certification-penalty": "criminal/civil exposure for false payrolls; no amount to compute",
    "fed-dba-records-access-and-interviews": "audit access rule; no threshold or deadline to compute",
    "fed-dba-agency-payment-certification": "agency-side payment condition",
    "fed-dba-apprentice-fringe-benefits": "apprentice fringe follows the registered program; needs program data",
    "fed-dba-anti-retaliation": "worker protection with no number to compute",
    "fed-dba-dispute-hearing-30-days": "applies only after DOL findings; not part of project setup",
    "fed-dba-underpayment-1000-enforcement-report": "agency reporting threshold, not a contractor duty",
    "fed-dba-site-of-work": "coverage definition that needs site facts the tools do not collect",
    "fed-dba-covered-transportation-truckers": "coverage definition that needs trucking facts the tools do not collect",
    "fed-dba-fringe-trustee-rules": "plan-design rule for fringe trusts",
    "fed-dba-unfunded-plan-approval": "plan-approval process with DOL",
    "fed-dba-apprenticeship-cost-credit": "fringe-credit detail for apprenticeship costs",
    "fed-dba-admin-costs-not-creditable": "fringe-credit detail for plan administration costs",
    "fed-wd-project-wd-180-days": "agency-side validity of project wage determinations",
    "fed-wd-updates-on-options-and-annual": "mid-contract modification rule; needs modification facts",
    "fed-wd-eci-updates-3-years": "how DOL updates non-union rates; not a contractor step",
    "fed-wd-prevailing-wage-30-percent-rule": "how DOL sets rates; not a contractor step",
    "fed-wd-reconsideration-30-days": "procedure for challenging a wage determination",
    "fed-dba-contractor-affirmative-duty": "general duty statement",
    "fed-copeland-anti-kickback": "prohibition with no number to compute",
    "fed-copeland-allowed-deductions": "list of permitted deductions; payroll detail",
    "fed-copeland-payment-methods": "payment-method rule; payroll detail",
    "fed-cwhssa-payroll-records-3-years": "same 3-year retention as the Davis-Bacon record rule the tools cite",
    "fed-cwhssa-withheld-funds-workers-first": "order of payment from withheld funds; enforcement detail",
    "fed-gov-payment-due-dates": "when the government pays the prime; not a contractor deadline",
    "fed-progress-payment-certification": "invoice certification wording; no number to compute",
    "fed-prompt-pay-withholding-notice": "content of a withholding notice; the requirements tool cites the interest "
                                         "rule that sets the deadline",
    "fed-fhwa-no-convict-labor-no-state-preference": "prohibitions with no number to compute",
    "fed-dbe-disadvantage-no-presumption": "DBE certification standard (for DBE firms, not bidders)",
    "fed-dbe-pnw-cap-2047000": "DBE certification eligibility (for DBE firms, not bidders)",
    "fed-dbe-size-cap-30-72m": "DBE certification eligibility (for DBE firms, not bidders)",
    "fed-dbe-substitution-gfe-7-days": "applies only after a DBE is dropped on a goal contract",
    "fed-dbe-counting-suppliers-100-60-40": "DBE counting is suspended since 2025-10-03 (49 CFR 26.55(i)); the "
                                            "requirements tool reports the suspension instead of computing credit",
    "fed-dbe-cuf-30pct-own-forces": "DBE counting is suspended since 2025-10-03 (49 CFR 26.55(i))",
    "fed-dbe-trucking-credit": "DBE counting is suspended since 2025-10-03 (49 CFR 26.55(i))",
    "fed-dbe-subcontract-copies": "provide-on-request rule with no deadline",
}

DUE_SOON_DAYS = 14          # display window for key dates, not a legal number

FundingSource = Literal["direct_federal", "federal_aid_highway", "federal_transit", "federal_airport",
                        "federal_grant", "none"]
FUNDING_LABELS = {
    "direct_federal": "Direct federal contract (a federal agency is the owner; FAR clauses)",
    "federal_aid_highway": "Federal-aid highway (FHWA money through Caltrans or a local agency)",
    "federal_transit": "Federal transit (FTA money through a California transit agency)",
    "federal_airport": "Federal airport grant (FAA money through a California airport sponsor)",
    "federal_grant": "Other federal grant or loan through a California state or local agency (HUD, EPA, FEMA, ...)",
    "none": "No federal money",
}
USDOT = {"federal_aid_highway", "federal_transit", "federal_airport"}
ASSISTED = USDOT | {"federal_grant"}


# ---------------------------------------------------------------- small helpers
def P(rule_id: str, key: str, default=KeyError):
    r = core.rule(rule_id)
    return r.param(key) if default is KeyError else r.param(key, default)


def _ok(ids) -> bool:
    return all(core.rule(r).verified for r in ids)


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _usd(x: float) -> str:
    """$2,000 / $1,234.56 (no '.00' on whole dollars)."""
    x = _money(x)
    return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"


def _n(x: float) -> str:
    return f"{round(x, 4):g}"


def _iso_param(rule_id: str, key: str) -> date:
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(P(rule_id, key)))
    if not m:
        raise KeyError(f"rule {rule_id} parameter {key!r} has no ISO date")
    return date.fromisoformat(m.group(0))


def _words(text) -> str:
    """Lowercase, accents and punctuation removed, single spaces: 'City of San José, CA' -> 'city of san jose ca'."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _has(text: str, *names: str) -> bool:
    return any(re.search(rf"(?:^| ){re.escape(n)}(?: |$)", text) for n in names)


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y")


def _date(value, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = re.sub(r"\s+", " ", str(value or "").strip())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"{field} must be a date like 2026-11-25 or 11/25/2026, got {value!r}")


def _opt_date(value, field: str) -> Optional[date]:
    return None if value is None or (isinstance(value, str) and not value.strip()) else _date(value, field)


def _today(as_of) -> date:
    return _opt_date(as_of, "as_of") or logic.today_ca()


def _clean_num(v):
    """'$480,000' -> '480000' so pydantic can parse it; other values pass through."""
    if isinstance(v, str):
        return re.sub(r"[\s$,]", "", v)
    return v


def _number(value, field: str, *, allow_none: bool = False, positive: bool = False) -> Optional[float]:
    """A finite number >= 0 (> 0 if positive). Accepts '$480,000'. None or '' -> None when allow_none."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, got {value!r}")
    try:
        x = float(_clean_num(value)) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number like 480000 or $480,000, got {value!r}") from None
    if not math.isfinite(x) or x < 0 or (positive and x == 0):
        raise ValueError(f"{field} must be a finite number {'above' if positive else 'of at least'} 0, got {value!r}")
    return x


def _on(today: date, rule_id: str, now_key: str, prior_key: str, eff_key: str) -> tuple[float, Optional[str]]:
    """A threshold the 2025-10-01 FAR inflation rule changed: (value in effect on `today`, history note or None)."""
    eff = _iso_param(rule_id, eff_key)
    now, prior = P(rule_id, now_key), P(rule_id, prior_key)
    if today >= eff:
        return now, f"{_usd(prior)} before {eff.isoformat()}"
    return prior, f"{_usd(now)} from {eff.isoformat()}"


# ---------------------------------------------------------------- user words -> categories (not law)
_FUNDING_ALIASES = {
    "direct_federal": ("direct federal", "direct", "federal contract", "federal agency", "far", "far contract",
                       "usace", "army corps", "army corps of engineers", "us army corps of engineers",
                       "corps of engineers", "gsa", "general services administration", "va", "veterans affairs",
                       "department of veterans affairs", "navy", "us navy", "navfac", "nps", "national park service",
                       "air force", "usaf", "us army", "army", "marine corps", "usmc", "dod",
                       "department of defense", "bureau of reclamation", "usbr", "forest service", "usfs",
                       "federal lands highway", "flh", "dhs", "cbp", "coast guard", "uscg"),
    "federal_aid_highway": ("fhwa", "federal aid", "federal aid highway", "federal highway",
                            "federal highway administration", "highway", "federal aid road", "caltrans federal aid",
                            "caltrans local assistance", "local assistance"),
    "federal_transit": ("fta", "federal transit", "federal transit administration", "transit", "fta grant"),
    "federal_airport": ("faa", "aip", "faa aip", "airport improvement program", "airport",
                        "federal aviation administration", "faa grant"),
    "federal_grant": ("grant", "federal grant", "federal assistance", "assistance", "federal loan", "hud", "cdbg",
                      "epa", "srf", "state revolving fund", "cwsrf", "dwsrf", "fema", "usda", "rural development",
                      "eda", "iija", "bil", "bipartisan infrastructure law", "federally assisted", "grant funded",
                      "federal grant funded"),
    "none": ("none", "no", "no federal", "no federal money", "no federal funds", "state", "state only", "local",
             "local only", "state and local", "private", "na", "n a"),
}
_AMBIGUOUS_FUNDING = ("federal", "federally funded", "federal funds", "federal money", "fed", "federal funding",
                      "federal project", "yes")
# Agencies that make a contract direct federal (matched as whole words, checked before California bodies).
_FEDERAL_AGENCY_WORDS = ("usace", "army corps", "corps of engineers", "gsa", "general services administration",
                         "veterans affairs", "va", "navy", "navfac", "national park service", "nps", "air force",
                         "usaf", "army", "marine corps", "usmc", "department of defense", "dod",
                         "bureau of reclamation", "usbr", "forest service", "usfs", "homeland security", "dhs",
                         "cbp", "coast guard", "uscg", "department of energy", "federal", "united states")
_CA_PUBLIC_WORDS = ("caltrans", "california", "state of ca", "city", "county", "town", "school", "schools",
                    "unified", "usd", "district", "college", "university", "uc", "csu", "cal state", "port", "metro",
                    "transit", "authority", "jpa", "water", "sanitation", "housing authority", "commission",
                    "sandag", "mts", "bart", "vta", "sfmta", "lawa", "lausd", "caltrain", "metrolink", "dgs", "dwr",
                    "octa", "nctd", "samtrans", "sfpuc", "ladwp", "airport")
# Bare California city names / abbreviations people type for "City of ...".
_CA_CITIES = {
    "sd": "San Diego", "san diego": "San Diego", "la": "Los Angeles", "los angeles": "Los Angeles",
    "sf": "San Francisco", "san francisco": "San Francisco", "sj": "San Jose", "san jose": "San Jose",
    "oakland": "Oakland", "sacramento": "Sacramento", "sac": "Sacramento", "sacto": "Sacramento",
    "fresno": "Fresno", "long beach": "Long Beach", "lb": "Long Beach", "bakersfield": "Bakersfield",
    "anaheim": "Anaheim", "santa ana": "Santa Ana", "riverside": "Riverside", "stockton": "Stockton",
    "irvine": "Irvine", "chula vista": "Chula Vista", "fremont": "Fremont", "san bernardino": "San Bernardino",
    "modesto": "Modesto", "oxnard": "Oxnard", "fontana": "Fontana", "huntington beach": "Huntington Beach",
    "glendale": "Glendale", "santa clarita": "Santa Clarita", "oceanside": "Oceanside", "escondido": "Escondido",
    "carlsbad": "Carlsbad", "el cajon": "El Cajon", "pasadena": "Pasadena", "torrance": "Torrance",
    "ontario": "Ontario", "elk grove": "Elk Grove", "corona": "Corona", "berkeley": "Berkeley",
    "santa rosa": "Santa Rosa", "vista": "Vista", "national city": "National City",
}
AgencyKind = Literal["federal_agency", "california_public_agency", "other", "not_given"]


def normalize_funding(value) -> str:
    """'FHWA', 'Caltrans federal-aid' -> federal_aid_highway; 'USACE' -> direct_federal; 'CDBG' -> federal_grant."""
    t = _words(value)
    allowed = ", ".join(FUNDING_LABELS)
    if not t:
        raise ValueError(f"funding_source is required: one of {allowed}")
    if t.replace(" ", "_") in FUNDING_LABELS:
        return t.replace(" ", "_")
    if t in _AMBIGUOUS_FUNDING:
        raise ValueError(f"funding_source {value!r} is ambiguous. Use direct_federal when a federal agency (USACE, "
                         "GSA, VA, Navy, ...) is your owner, or federal_aid_highway / federal_transit / "
                         "federal_airport / federal_grant when federal money is passed through a California agency.")
    for key, names in _FUNDING_ALIASES.items():
        if t in names:
            return key
    # longer phrases such as "FHWA via Caltrans", "USACE LA District", "HUD CDBG grant": first specific match wins
    for key, names in _FUNDING_ALIASES.items():
        if key != "none" and _has(t, *(n for n in names if len(n) >= 3 and n not in ("direct", "grant"))):
            return key
    if _has(t, "grant", "loan"):
        return "federal_grant"
    raise ValueError(f"Unknown funding_source {value!r}. Use one of: {allowed} (aliases such as FHWA, FTA, FAA, "
                     "USACE, GSA, VA, HUD, CDBG, EPA SRF and FEMA are understood).")


def classify_agency(value) -> tuple[AgencyKind, Optional[str]]:
    """-> (kind, display name). 'SD' / 'City of San Diego' -> California public agency 'City of San Diego';
    'US Army Corps of Engineers, LA District' -> federal agency."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    t = _words(raw)
    if not t:
        return "not_given", None
    t = re.sub(r"(?: (?:ca|calif|california))+$", "", t).strip() or t
    t = re.sub(r"^the ", "", t)
    if _has(t, *_FEDERAL_AGENCY_WORDS) or t.startswith("us "):
        return "federal_agency", raw
    bare = re.sub(r"^(?:city|town) of ", "", t)
    bare = re.sub(r" city$", "", bare) if bare not in ("national city",) else bare
    if bare in _CA_CITIES:
        return "california_public_agency", f"City of {_CA_CITIES[bare]}"
    if _has(t, *_CA_PUBLIC_WORDS):
        return "california_public_agency", raw
    return "other", raw


WorkType = Literal["construction", "alteration", "repair", "painting_decorating", "demolition", "installation",
                   "material_supply"]
_WORK_ALIASES = {
    "construction": ("construction", "new construction", "build", "building", "ground up", "civil", "paving",
                     "grading", "concrete", "sitework", "site work", "underground"),
    "alteration": ("alteration", "alterations", "remodel", "renovation", "rehab", "rehabilitation",
                   "tenant improvement", "ti", "modernization", "retrofit", "addition"),
    "repair": ("repair", "repairs", "reconstruction", "replacement"),
    "painting_decorating": ("painting", "decorating", "painting and decorating", "painting decorating"),
    "demolition": ("demolition", "demo", "removal", "demolition and removal"),
    "installation": ("installation", "install", "solar", "ev charger", "ev chargers", "broadband", "fiber"),
    "material_supply": ("material supply", "material supplier", "supply", "supplier", "materials", "material",
                        "delivery", "materials only", "supply only"),
}


def normalize_work_type(value) -> str:
    t = _words(value) or "construction"
    if t.replace(" ", "_") in _WORK_ALIASES:
        return t.replace(" ", "_")
    for key, names in _WORK_ALIASES.items():
        if t in names:
            return key
    raise ValueError(f"Unknown work_type {value!r}. Use one of: {', '.join(_WORK_ALIASES)}")


# ---------------------------------------------------------------- result models
Unit = Literal["usd", "percent", "days", "hours", "count", "usd_per_hour"]


class Line(BaseModel):
    item: str
    value: Optional[float] = None
    unit: Unit = "usd"
    math: str
    rule_ids: list[str]
    verified: bool = True


class Requirement(BaseModel):
    area: str = Field(description="Wages, Payroll, Overtime, California, Bonds, Registration, Hiring, "
                                  "Subcontracting, Domestic content, DBE, Payments, Contract clauses, Leave")
    item: str
    applies: Optional[bool] = Field(description="True / False at this contract value, or None when the facts given "
                                                "cannot settle it (plan as if it applies)")
    why: str = Field(description="The threshold math or the reason")
    what_to_do: str = ""
    due: Optional[str] = None
    rule_ids: list[str]
    verified: bool = True


class KeyDate(BaseModel):
    what: str
    due: str
    days_left: int
    kind: Literal["deadline", "milestone"] = Field("deadline", description=(
        "deadline = something you must do by this date; milestone = a date that changes which rules apply"))
    status: Literal["overdue", "due_soon", "upcoming", "passed"]
    rule_ids: list[str]
    verified: bool = True


class BondPlan(BaseModel):
    regime: Literal["miller_act", "payment_protection", "2_cfr_200_326", "none"]
    bid_guarantee_usd: Optional[float] = None
    performance_bond_usd: Optional[float] = None
    payment_bond_usd: Optional[float] = None
    payment_protection_usd: Optional[float] = None
    options: list[str] = Field(default_factory=list)
    deadline: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class FederalRequirementsResult(core.Result):
    as_of: str
    funding_source: FundingSource
    funding_label: str
    awarding_agency: Optional[str] = None
    awarding_agency_kind: AgencyKind
    your_role: Literal["prime", "subcontractor"]
    work_type: WorkType
    prime_contract_value_usd: float
    your_contract_value_usd: Optional[float] = None
    davis_bacon_applies: Optional[bool]
    california_prevailing_wage_also_applies: Optional[bool]
    summary: str
    next_step: str
    requirements: list[Requirement] = Field(default_factory=list)
    bonds: BondPlan
    key_dates: list[KeyDate] = Field(default_factory=list)
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class _Ctx:
    """Collects cited rules, requirements, math lines, key dates and notes for one call."""

    def __init__(self, today: date):
        self.today, self.cited, self.reqs, self.lines, self.dates, self.notes = today, set(), [], [], [], []

    def use(self, *ids: str) -> list[str]:
        for rid in ids:
            core.rule(rid)                      # unknown id -> KeyError at once
        self.cited.update(ids)
        return list(ids)

    def req(self, area, item, applies, why, what, ids, due: Optional[date] = None):
        self.reqs.append(Requirement(area=area, item=item, applies=applies, why=why, what_to_do=what,
                                     due=due.isoformat() if due else None, rule_ids=self.use(*ids),
                                     verified=_ok(ids)))

    def line(self, item, value, unit, math_, *ids):
        self.lines.append(Line(item=item, value=None if value is None else _money(value), unit=unit, math=math_,
                               rule_ids=self.use(*ids), verified=_ok(ids)))

    def date(self, what, due: date, *ids, kind="deadline"):
        days = (due - self.today).days
        status = ("overdue" if kind == "deadline" else "passed") if days < 0 else (
            "due_soon" if days <= DUE_SOON_DAYS else "upcoming")
        self.dates.append(KeyDate(what=what, due=due.isoformat(), days_left=days, kind=kind, status=status,
                                  rule_ids=self.use(*ids), verified=_ok(ids)))

    def finish(self, res):
        res.cite(*sorted(self.cited))
        if "unverified_rule_ids" in type(res).model_fields:
            res.unverified_rule_ids = sorted(r for r in self.cited if not core.rule(r).verified)
        return res


class _Job:
    """Normalized facts for one federal_project_requirements call."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------- federal_project_requirements sections
def _sec_davis_bacon(ctx: _Ctx, j: _Job) -> Optional[bool]:
    thr = P(DBA, "prime_contract_value_greater_than_usd")
    v = j.value
    sub_note = (" Your subcontract is covered at any amount because the prime contract is."
                if not j.is_prime and P(DBA, "subcontracts_any_value_covered") else "")
    ids = [DBA]
    if j.fund == "none":
        applies, why = False, ("No federal money: Davis-Bacon and its Related Acts do not apply. California "
                               "prevailing wage may (see public_works_readiness).")
    elif j.work == "material_supply":
        conds = "; ".join(P(DBA_MATERIAL, "conditions_all_required"))
        applies, why = False, (f"A material supplier is not a covered subcontractor when all of these hold: {conds}. "
                               f"It loses that status if it {P(DBA_MATERIAL, 'loses_status_if')}.")
        ids.append(DBA_MATERIAL)
    elif v <= thr:
        applies, why = False, (f"{_usd(v)} is not more than {_usd(thr)}: Davis-Bacon covers contracts over "
                               f"{_usd(thr)} (a statutory figure, never raised for inflation).")
        ids.append(NOT_ADJUSTED)
    elif j.fund == "federal_grant" and j.dbra is None:
        applies, why = None, (f"{_usd(v)} > {_usd(thr)}, but grant or loan money brings Davis-Bacon only when the "
                              "program's law is a Davis-Bacon Related Act. Check the grant agreement or bid documents "
                              "for a federal wage determination; plan as covered until the agency says otherwise.")
        if j.work == "demolition":
            why += f" Demolition is covered when: {'; '.join(P(DBA_DEMO, 'covered_when'))}."
            ids.append(DBA_DEMO)
    elif j.dbra is False:
        applies, why = False, ("You said the funding program is not a Davis-Bacon Related Act, so the federal wage "
                               "rules do not attach (confirm this in writing with the agency).")
    elif j.work == "demolition":
        when = "; ".join(P(DBA_DEMO, "covered_when"))
        applies, why = None, (f"{_usd(v)} > {_usd(thr)}. Demolition is covered when: {when}. Plan as covered if "
                              "anything will be built on the site.")
        ids.append(DBA_DEMO)
    else:
        src = ("a federal agency contract" if j.fund == "direct_federal" else
               "federal assistance under a Davis-Bacon Related Act" if j.fund == "federal_grant" else
               "federal-aid highway, transit or airport money (Davis-Bacon Related Acts)")
        applies, why = True, (f"{_usd(v)} > {_usd(thr)} for construction paid by {src}; the threshold is statutory "
                              f"and not inflation-adjusted.{sub_note}")
        ids.append(NOT_ADJUSTED)
        if j.work == "installation":
            why += (" Covered 'building or work' expressly includes "
                    f"{', '.join(P(DBA_WORK, 'expressly_listed_examples')[:4])}.")
            ids.append(DBA_WORK)
    ctx.req("Wages", "Davis-Bacon prevailing wages", applies, why,
            ("Pay every laborer and mechanic on the site at least the wage determination basic rate plus fringe for "
             f"each classification actually worked, at least once every {P(DBA_PAY, 'max_days_between_paydays')} "
             "days, with deductions only as 29 CFR part 3 allows. Working forepersons who spend more than "
             f"{_n(P(DBA_FOREPERSON, 'foreperson_covered_if_trade_time_pct_greater_than'))}% of the week on trade "
             "work are covered too.") if applies is not False else "",
            ids + ([DBA_PAY, DBA_FOREPERSON] if applies is not False else []))
    if applies is False:
        return applies

    ctx.req("Wages", "Wage determination and job-site posters", applies,
            "Every covered contract carries a federal wage determination.",
            ("Use the wage determination in the contract (general determinations are published on SAM.gov). It "
             "must be the schedule for the type of construction: building, residential (up to "
             f"{P(WD_TYPES, 'residential_max_stories')} stories), highway or heavy. A trade missing from it needs a "
             f"conformance request; DOL answers in {P(DBA_CONFORM, 'dol_response_days')} days and the rate applies "
             "back to the first day of that work. Post the wage determination and the Davis-Bacon poster WH-1321 "
             "where workers can see them, at all times."),
            [WD_SAM, WD_TYPES, DBA_CONFORM, DBA_POST])
    ctx.req("Wages", "Apprentices", applies, "Apprentice rates are an exception to the journeyworker rate.",
            ("Pay apprentice rates only to workers individually registered in a program registered with the DOL "
             "Office of Apprenticeship or a recognized State Apprenticeship Agency, within the apprentice ratio of "
             "the project's locality (or your registered program's ratio if none applies there); anyone else, or "
             "anyone over the ratio, gets the full rate for the work performed."), [DBA_APPR, DBA_APPR_RATIO])
    ctx.req("Payroll", "Weekly certified payroll and Statement of Compliance", applies,
            (f"Required for every week covered work is done; the Copeland Act weekly statement applies to contracts "
             f"over {_usd(P(COPE_2000, 'exempt_contract_value_usd_at_or_below'))}."),
            ("Each week, submit a certified payroll (Optional Form WH-347 or any format with the same data; show "
             "only an identifying number such as the last 4 digits of the SSN) signed with the Statement of "
             f"Compliance, within {P(COPE_7, 'days_after_regular_payment_date')} days after the regular payday, to "
             "the agency (on federally assisted jobs, to the city, county, Caltrans or other agency that holds the "
             "records). " + ("You must collect and submit every subcontractor's payrolls."
                             if j.is_prime else "Send yours through the prime, who is responsible for them.")),
            [DBA_CPR, DBA_WH347, DBA_SOC, COPE_7, COPE_2000])
    ctx.req("Payroll", "Keep payroll and contract records", applies, "Record-keeping rule of the Davis-Bacon clause.",
            (f"Keep payroll and basic records, and the contract, bids, proposals and change orders, for "
             f"{P(DBA_RECORDS, 'retention_years_after_prime_contract_completion')} years after the whole prime "
             "contract is finished."), [DBA_RECORDS, DBA_DOCS])
    ctx.req("Subcontracting", "Flow the labor clauses down; check debarment", applies,
            "The clauses bind every tier.",
            ("Put the Davis-Bacon contract clauses (29 CFR 5.5(a)(1)-(11)) and the wage determination in every "
             "subcontract at every tier" + ("; you are liable for lower-tier back wages and interest. " if j.is_prime
                                            else "; the prime contractor answers for lower-tier back wages. ") +
             "Check SAM.gov exclusions before subcontracting: you certify you are not debarred and may not use a "
             "debarred firm. Disregarding obligations to workers or subcontractors means a "
             f"{P(DBA_DEBAR, 'debarment_years')}-year debarment from federal and Davis-Bacon work."),
            [DBA_FLOW, DBA_ELIG, DBA_DEBAR])
    if j.bid_open:
        days = P(WD_BID, "days_before_bid_opening")
        cutoff = j.bid_open - timedelta(days=days)
        if ctx.today <= cutoff:
            ctx.date(f"Re-check the wage determination on SAM.gov: a revision issued by this date ({days} days before "
                     "bid opening) applies to your bid", cutoff, WD_BID)
        elif ctx.today <= j.bid_open:
            ctx.date(f"Re-check the wage determination on SAM.gov before bid opening: the {days}-day cutoff "
                     f"({cutoff}) has passed, but a later revision still applies unless the agency finds there is "
                     "not enough time to notify bidders", j.bid_open, WD_BID)
        window = P(WD_BID, "award_window_days_after_bid_opening")
        ctx.date(f"Award window: if the contract is not awarded by this date ({window} days after bid opening), "
                 "wage determination revisions issued before award apply", j.bid_open + timedelta(days=window), WD_BID,
                 kind="milestone")
    return applies


def _sec_overtime(ctx: _Ctx, j: _Job) -> None:
    v, mult, wk = j.value, P(CWH_OT, "overtime_multiplier"), P(CWH_OT, "weekly_hours_threshold")
    ld = P(CWH_LD, "liquidated_damages_usd_per_worker_per_day")
    if j.fund == "direct_federal":
        thr, hist = _on(ctx.today, CWH_FAR, "far_clause_excluded_at_or_below_usd", "prior_value_usd",
                        "change_effective")
        applies = v > thr
        why = (f"{_usd(v)} {'>' if applies else 'is at or below'} {_usd(thr)}: FAR 22.305 leaves the overtime clause "
               f"out of contracts at or below {_usd(thr)} ({hist}), and out of commercial products/services "
               "contracts and a few others.")
        ids = [CWH_FAR, CWH_OT, CWH_LD, CWH_FLOW, OT_BASE]
    else:
        thr = P(CWH_OT, "contract_value_greater_than_usd")
        applies = v > thr
        why = (f"{_usd(v)} {'>' if applies else 'is not over'} {_usd(thr)} (the DOL figure for federally assisted "
               "contracts).")
        ids = [CWH_OT, CWH_LD, CWH_FLOW, OT_BASE] + ([CWH_SAFETY] if applies else [])
    what = ""
    if applies:
        what = (f"Pay {_n(mult)}x the basic rate (never below the wage determination basic rate; fringes stay at "
                f"straight time) for every hour over {wk} in a workweek, for every laborer and mechanic including "
                f"guards and watchpersons. A violation costs the unpaid overtime plus {_usd(ld)} per worker per "
                "calendar day in liquidated damages. Flow the clause down to every subcontract. Use "
                "federal_overtime_exposure to check a timesheet.")
        if j.fund != "direct_federal":
            what += (" No laborer or mechanic may work in unsanitary, hazardous or dangerous conditions "
                     "(40 U.S.C. 3704).")
    ctx.req("Overtime", f"CWHSSA weekly overtime (over {wk} hours)", applies, why, what, ids if applies else ids[:1])


def _sec_california(ctx: _Ctx, j: _Job) -> Optional[bool]:
    who = j.agency or "a California public agency"
    if j.fund == "direct_federal":
        ctx.req("California", "California prevailing wage (dual coverage)", False,
                ("A federal agency's own contract is under federal control; California prevailing wage applies to "
                 "federally funded work only when a California awarding body controls or carries it out "
                 f"({P(CA_HIGHER, 'state_law_not_applicable_when')} is excluded)."), "", [CA_HIGHER])
        return False
    if j.kind == "california_public_agency" or (j.kind == "not_given" and j.fund in USDOT):
        assumed = j.kind == "not_given"
        why = (f"Federally assisted work controlled or carried out by {who}: the state rate applies where higher "
               "(8 CCR 16001(b)).")
        if assumed:
            why += (" Assumed because federal-aid highway, transit and airport money reaches contractors through "
                    "California agencies; give awarding_agency to confirm.")
        ctx.req("California", "California prevailing wage also applies (dual coverage)", True, why,
                ("For each classification and hour pay the higher of the two: at least the DIR Basic Hourly Rate in "
                 "cash (employer payments cannot reduce it), and a total package at least the higher of the "
                 f"federal and state totals. California overtime also applies: over {P(PW_OT, 'daily_hours_threshold')}"
                 f" hours a day or {P(PW_OT, 'weekly_hours_threshold')} a week at "
                 f"{_n(P(PW_OT, 'min_multiplier'))}x. Use davis_bacon_vs_ca_prevailing to compare the two "
                 "determinations and public_works_readiness for DIR registration, apprenticeship and the other "
                 "state duties."), [CA_HIGHER, CA_BASIC, PW_OT])
        ctx.req("Payroll", "Two payroll filings (federal and California)", True,
                "Dual-coverage jobs report to both systems; one filing does not satisfy the other.",
                (f"File the weekly federal certified payroll with the agency within "
                 f"{P(COPE_7, 'days_after_regular_payment_date')} days after each payday, AND upload California "
                 f"electronic certified payroll (eCPR) to DIR at least every {P(ECPR, 'max_interval_days')} days "
                 f"(weekly is best) and within {P(ECPR, 'final_submission_within_days_after_last_work')} days after "
                 "your last day of work. Keep one payroll that supports both."), [CA_REPORT, COPE_7, ECPR])
        return True
    if j.kind == "federal_agency":           # assisted funding with a federal awarding agency is rejected earlier
        return False
    ctx.req("California", "California prevailing wage (dual coverage)", None,
            ("Not settled: give awarding_agency. If a California public agency (city, county, district, Caltrans, "
             "school, transit or airport agency) controls or carries out the project, California prevailing wage "
             "applies too and the higher rate controls."
             if j.kind == "not_given" else
             f"{who} does not look like a California public agency. If one controls or carries out the project, "
             "California prevailing wage applies too and the higher rate controls."), "", [CA_HIGHER])
    return None


def _sec_bonds(ctx: _Ctx, j: _Job) -> BondPlan:
    v, bid = j.value, j.bid
    if j.fund == "direct_federal":
        mil, lo = P(MILLER, "contract_value_greater_than_usd"), P(MILLER_ALT, "contract_value_greater_than_usd")
        hi = P(MILLER_ALT, "contract_value_at_or_below_usd")
        if v > mil:
            perf = v * P(MILLER, "performance_bond_pct_of_original_price") / 100
            pay = v * P(MILLER, "payment_bond_pct_of_original_price") / 100
            pmin, cmax = P(BID_GUAR, "solicitation_percent_min"), P(BID_GUAR, "solicitation_dollar_cap_max_usd")
            pct = pmin if j.bg_pct is None else j.bg_pct
            cap = cmax if j.bg_cap is None else j.bg_cap
            guar = min(bid * pct / 100, cap)
            days = P(BONDS_NTP, "default_days_to_furnish_executed_bonds_after_receipt_of_forms")
            forms = P(MILLER, "forms")
            perf_pct = _n(P(MILLER, "performance_bond_pct_of_original_price"))
            pay_pct = _n(P(MILLER, "payment_bond_pct_of_original_price"))
            ctx.line("Performance bond", perf, "usd", f"{_usd(v)} x {perf_pct}% = {_usd(perf)}", MILLER)
            ctx.line("Payment bond", pay, "usd", f"{_usd(v)} x {pay_pct}% = {_usd(pay)}", MILLER)
            src = "the solicitation's" if (j.bg_pct is not None or j.bg_cap is not None) else "the usual"
            ctx.line("Bid guarantee", guar, "usd", f"min({_n(pct)}% x {_usd(bid)} bid, {_usd(cap)} cap) = {_usd(guar)} "
                     f"({src} figures)", BID_GUAR)
            if j.is_prime:
                ctx.req("Bonds", "Miller Act performance and payment bonds", True,
                        f"{_usd(v)} > {_usd(mil)} (statutory; not inflation-adjusted).",
                        (f"Furnish a performance bond ({forms['performance']}) of {_usd(perf)} and a payment bond "
                         f"({forms['payment']}) of {_usd(pay)} from a surety on Treasury Circular 570 (or other "
                         "approved security), plus the same percentage of any later price increase. Return executed "
                         f"bonds within {days} days after you receive the forms unless the solicitation sets another "
                         "period, and always before the notice to proceed or starting work."),
                        [MILLER, NOT_ADJUSTED, SURETY, BONDS_NTP])
                ctx.req("Bonds", "Bid guarantee", True, "Required with the bid when performance and payment bonds are.",
                        (f"Bid bond of {_n(pct)}% of the bid or {_usd(cap)}, whichever is less: {_usd(guar)}. The "
                         "solicitation's own figures control (the contracting officer sets at least "
                         f"{_n(pmin)}% and no more than {_usd(cmax)}). A missing or short bid guarantee normally gets "
                         "a sealed bid rejected."), [BID_GUAR])
                if pct < pmin or cap > cmax:
                    ctx.notes.append(f"The bid guarantee figures given ({_n(pct)}%, {_usd(cap)} cap) are outside the "
                                     f"FAR 28.101-2(b) range (at least {_n(pmin)}%, cap not over {_usd(cmax)}); check "
                                     "the solicitation.")
            else:
                ctx.req("Bonds", "Your Miller Act payment bond rights", True,
                        f"The prime contract ({_usd(v)}) is over {_usd(mil)}, so the prime must carry a payment bond.",
                        ("Ask the contracting officer for the surety's name and address, the bond amount and a copy of "
                         "the payment bond. A waiver of your payment bond rights is void unless it is in writing, "
                         "signed by you, and signed after you first furnished labor or material."),
                        [MILLER, BOND_COPY, BOND_WAIVER])
            return BondPlan(regime="miller_act", bid_guarantee_usd=_money(guar), performance_bond_usd=_money(perf),
                            payment_bond_usd=_money(pay),
                            deadline=(f"within {days} days after receipt of the bond forms, before the notice to "
                                      "proceed"),
                            notes=["Surety must be on Treasury Circular 570 (or approved individual surety/security)."])
        if v > lo:
            prot = v * P(MILLER_ALT, "amount_pct_of_contract_price") / 100
            opts = list(P(MILLER_ALT, "options"))
            ctx.line("Payment protection", prot, "usd",
                     f"{_usd(v)} x {_n(P(MILLER_ALT, 'amount_pct_of_contract_price'))}% = {_usd(prot)}", MILLER_ALT)
            if j.is_prime:
                ctx.req("Bonds", "Payment protection (no Miller Act bonds at this value)", True,
                        f"{_usd(lo)} < {_usd(v)} <= {_usd(hi)} (statutory; not inflation-adjusted).",
                        (f"Furnish the payment protection the contracting officer selects ({', '.join(opts)}) for "
                         f"{_usd(prot)}, lasting the {P(MILLER_ALT, 'coverage_period')}. A bid guarantee is needed "
                         "only if the solicitation requires performance or payment bonds."),
                        [MILLER_ALT, NOT_ADJUSTED, BID_GUAR])
            return BondPlan(regime="payment_protection", payment_protection_usd=_money(prot), options=opts)
        if j.is_prime:
            ctx.req("Bonds", "Miller Act bonds", False,
                    f"{_usd(v)} is not more than {_usd(lo)}: no Miller Act bond or payment protection is required.",
                    "", [MILLER_ALT])
        return BondPlan(regime="none")
    # federally assisted: 2 CFR 200.326 federal minimum
    far_sat, hist = _on(ctx.today, FAR_THRESH, "simplified_acquisition_threshold_usd",
                        "simplified_acquisition_threshold_prior_usd", "effective")
    if j.sat is not None and j.sat > far_sat:
        raise ValueError(f"recipient_sat_usd ({_usd(j.sat)}) cannot exceed the FAR simplified acquisition threshold "
                         f"({_usd(far_sat)} on {ctx.today}): 2 CFR 200.1 caps an agency's own threshold at the FAR "
                         "figure")
    sat = far_sat if j.sat is None else j.sat
    sat_src = (f"the agency's own threshold (at most the FAR {_usd(far_sat)})" if j.sat is not None else
               f"the FAR figure ({hist}); the agency may set a lower one")
    ids = [ASSIST_BOND, FAR_THRESH]
    if v > sat:
        bg = bid * P(ASSIST_BOND, "bid_guarantee_pct") / 100
        perf = v * P(ASSIST_BOND, "performance_bond_pct") / 100
        pay = v * P(ASSIST_BOND, "payment_bond_pct") / 100
        ctx.line("Bid guarantee (2 CFR 200.326)", bg, "usd",
                 f"{_n(P(ASSIST_BOND, 'bid_guarantee_pct'))}% x {_usd(bid)} bid = {_usd(bg)}", *ids)
        ctx.line("Performance bond (2 CFR 200.326)", perf, "usd",
                 f"{_usd(v)} x {_n(P(ASSIST_BOND, 'performance_bond_pct'))}% = {_usd(perf)}", *ids)
        ctx.line("Payment bond (2 CFR 200.326)", pay, "usd",
                 f"{_usd(v)} x {_n(P(ASSIST_BOND, 'payment_bond_pct'))}% = {_usd(pay)}", *ids)
        if j.is_prime:
            ctx.req("Bonds", "Federal minimum bonding for assisted construction", True,
                    f"{_usd(v)} > {_usd(sat)}, {sat_src}.",
                    ("Unless the federal agency or pass-through entity accepted the agency's own bonding policy: bid "
                     f"guarantee {_usd(bg)}, performance bond {_usd(perf)} and payment bond {_usd(pay)}. California "
                     "public works bonding law also applies; check the bid documents."), ids)
        return BondPlan(regime="2_cfr_200_326", bid_guarantee_usd=_money(bg), performance_bond_usd=_money(perf),
                        payment_bond_usd=_money(pay),
                        notes=["The agency may instead use its own bonding policy if the federal agency accepted it."])
    if j.is_prime:
        ctx.req("Bonds", "Federal minimum bonding for assisted construction", False,
                f"{_usd(v)} is not over {_usd(sat)}, {sat_src}.",
                "California public works bonding law may still require bonds; check the bid documents.", ids)
    return BondPlan(regime="none")


def _sec_registration(ctx: _Ctx, j: _Job) -> None:
    v = j.value
    if j.fund == "direct_federal":
        if j.is_prime:
            ctx.req("Registration", "SAM.gov registration (UEI)", True, "Every offeror on a FAR contract.",
                    ("Be actively registered in SAM.gov, with your Unique Entity Identifier on the bid, when you "
                     "submit the offer and at award; update it at least every "
                     f"{P(SAM_ANNUAL, 'update_interval_months')} months and keep it active through final payment. "
                     "Registration includes IRS validation, so start early."), [SAM_REG, SAM_ANNUAL])
        thr, days_min = P(EV_PRIME, "contract_value_greater_than_usd"), P(EV_PRIME, "min_period_of_performance_days")
        sub_thr = P(EV_SUB, "subcontract_value_greater_than_usd")
        enroll = P(EV_ENROLL, "enroll_days_after_award")
        hire_days = P(EV_ENROLL, "new_hire_verification_business_days_after_hire")
        if j.is_prime:
            if v <= thr:
                applies, why = False, f"{_usd(v)} is not over {_usd(thr)}."
            elif j.pop is not None and j.pop < days_min:
                applies, why = False, f"Performance period {j.pop} days is under {days_min} days."
            else:
                applies = True
                why = (f"{_usd(v)} > {_usd(thr)}" + (f" and {j.pop} days >= {days_min}" if j.pop is not None else
                                                     f"; assumes a performance period of {days_min}+ days") + ".")
            what = (f"Enroll in E-Verify as a federal contractor within {enroll} days after award (if not already), "
                    f"verify each new hire within {hire_days} business days and each employee assigned to the "
                    f"contract; put the clause in construction and service subcontracts over {_usd(sub_thr)}."
                    if applies else "")
            ctx.req("Hiring", "E-Verify (FAR 52.222-54)", applies, why, what,
                    [EV_PRIME, EV_ENROLL, EV_SUB] if applies else [EV_PRIME])
            if applies and j.award:
                ctx.date("Enroll in E-Verify as a federal contractor", j.award + timedelta(days=enroll), EV_ENROLL)
        else:
            mine = j.mine
            applies = None if mine is None else mine > sub_thr
            why = (f"Applies to your subcontract when it is over {_usd(sub_thr)} and the prime contract has FAR "
                   "52.222-54." if mine is None else
                   f"Your subcontract {_usd(mine)} {'>' if applies else 'is not over'} {_usd(sub_thr)} (if the prime "
                   "contract has FAR 52.222-54).")
            ctx.req("Hiring", "E-Verify flow-down", applies, why,
                    (f"Enroll within {enroll} days after your subcontract award and verify new hires within "
                     f"{hire_days} business days.") if applies is not False else "",
                    [EV_SUB, EV_ENROLL] if applies is not False else [EV_SUB])
        return
    # federally assisted
    ctx.req("Registration", "SAM.gov registration", False,
            ("Under 2 CFR part 25 only the grantee and its first-tier subrecipients need a UEI; contractors hired "
             "under the federal award do not need SAM registration (the agency may still ask)."), "", [UEI])
    thr = P(SAM_EXCL, "covered_contract_expected_value_at_or_above_usd")
    if j.fund == "federal_aid_highway":
        applies, why = True, ("Federal-aid highway: suspended, debarred or voluntarily excluded contractors may not "
                              "participate at all (no dollar threshold).")
    elif j.is_prime:
        applies = v >= thr
        why = f"Prime contract {_usd(v)} {'>=' if applies else '<'} {_usd(thr)} (covered transaction threshold)."
    else:
        mine = j.mine
        applies = None if mine is None or mine >= thr else False
        why = (f"A lower-tier subcontract is covered when the agency extends coverage and it is {_usd(thr)} or more"
               + (f"; yours is {_usd(mine)}." if mine is not None else "."))
    ctx.req("Registration", "Not excluded in SAM.gov", applies, why,
            ("Check SAM.gov Exclusions for yourself and each sub before award; an excluded firm cannot get the "
             "contract.") if applies is not False else "", [SAM_EXCL])


def _sec_subcontracting(ctx: _Ctx, j: _Job, dba: Optional[bool]) -> None:
    v = j.value
    if j.fund == "direct_federal" and j.is_prime:
        thr, hist = _on(ctx.today, SUBK_PLAN, "construction_threshold_usd", "prior_construction_threshold_usd",
                        "effective")
        if j.small:
            applies, why = False, "Small business concerns are exempt."
        else:
            applies = v > thr
            why = (f"Other-than-small prime (set is_small_business if you are small): {_usd(v)} "
                   f"{'>' if applies else 'is not over'} {_usd(thr)} for construction ({hist}).")
        ctx.req("Subcontracting", "Small business subcontracting plan", applies, why,
                ("Submit an acceptable subcontracting plan; without one you are ineligible for award."
                 if applies else ""), [SUBK_PLAN])
        thr, hist = _on(ctx.today, SELF_PERF, "contract_value_greater_than_usd", "prior_threshold_usd", "effective")
        pct = P(SELF_PERF, "typical_min_self_perform_pct")
        if not j.fixed_price:
            applies, why, what = False, "FAR 52.236-1 applies to fixed-price construction contracts.", ""
        else:
            applies = v > thr
            why = f"Fixed-price construction {_usd(v)} {'>' if applies else 'is not over'} {_usd(thr)} ({hist})."
            what = ""
            if applies:
                amt = v * pct / 100
                ctx.line(f"Self-performance at the ordinary {_n(pct)}% floor", amt, "usd",
                         f"{_usd(v)} x {_n(pct)}% = {_usd(amt)}", SELF_PERF)
                what = (f"Perform on site with your own organization at least the percentage the contract states "
                        f"(FAR 52.236-1), ordinarily not less than {_n(pct)}% ({_usd(amt)} here).")
        ctx.req("Subcontracting", "Self-performance clause (FAR 52.236-1)", applies, why, what, [SELF_PERF])
    if j.fund == "direct_federal" and j.is_prime and dba:
        days = P(SF1413, "days_after_prime_award")
        ctx.req("Subcontracting", "SF 1413 for each construction subcontract", True, "FAR 52.222-11(d).",
                (f"Within {days} days after award, give the contracting officer a completed SF 1413 signed by each "
                 f"construction subcontractor, and within {days} days of any later subcontract award."), [SF1413],
                due=j.award + timedelta(days=days) if j.award else None)
        if j.award:
            ctx.date("Deliver SF 1413 for each construction subcontract", j.award + timedelta(days=days), SF1413)
    if j.fund == "federal_aid_highway":
        if j.is_prime:
            pct = P(FHWA_30, "min_self_perform_pct")
            if j.design_build:
                ctx.req("Subcontracting", f"Perform {_n(pct)}% with your own forces", False,
                        "The 23 CFR 635.116 percentage does not apply to design-build contracts.", "", [FHWA_30])
            else:
                spec = j.specialty or 0.0
                if spec > v:
                    raise ValueError("specialty_items_usd cannot exceed prime_contract_value_usd")
                amt = (v - spec) * pct / 100
                ctx.line("Own-forces minimum (23 CFR 635.116)", amt, "usd",
                         f"{_n(pct)}% x ({_usd(v)} - {_usd(spec)} specialty items) = {_usd(amt)}", FHWA_30)
                ctx.req("Subcontracting", f"Perform {_n(pct)}% with your own forces", True,
                        "Federal-aid highway prime contract, not design-build.",
                        (f"Perform at least {_usd(amt)} of the work with your own organization ({_n(pct)}% of the "
                         "original contract price minus specialty items); materials you buy for your own work "
                         "count." + ("" if j.specialty is not None else
                                     " Subtract the specialty items the contract lists (give specialty_items_usd).")),
                        [FHWA_30])
        ctx.req("Subcontracting", "Written authorization before subcontracting", True,
                "Federal-aid highway rule.",
                ("Do not subcontract any work without the state DOT's (or agency's) written authorization, and put "
                 "every subcontract in writing with all pertinent provisions of the prime contract."), [FHWA_SUBS])


def _sec_domestic(ctx: _Ctx, j: _Job) -> None:
    v = j.value
    if j.fund == "direct_federal":
        year = ctx.today.year
        pct, band = _far_pct(year)
        fe = P(FAR_BA, "iron_steel_foreign_content_pct_less_than")
        ctx.req("Domestic content", "Buy American - construction materials (FAR 52.225-9)", True,
                "Direct federal construction contract.",
                (f"Use only domestic construction material: manufactured material needs U.S. components over "
                 f"{_n(pct)}% of component cost (items delivered {band}); iron or steel material needs foreign iron "
                 f"and steel under {_n(fe)}% of component cost. Ask before award for exceptions (domestic more than "
                 f"{_n(P(FAR_BA, 'unreasonable_cost_differential_pct'))}% costlier, or unavailable). Check items with "
                 "buy_america_check."), [FAR_BA])
        return
    if j.fund == "federal_aid_highway":
        pct, floor = P(FHWA_STEEL, "minimal_use_pct_of_contract"), P(FHWA_STEEL, "minimal_use_floor_usd")
        allow = max(v * pct / 100, floor)
        ctx.line("Foreign steel/iron minimal-use allowance", allow, "usd",
                 f"max({_n(pct)}% x {_usd(v)} = {_usd(v * pct / 100)}, {_usd(floor)}) = {_usd(allow)}", FHWA_STEEL)
        p1, p2 = _iso_param(FHWA_MFG, "phase_1_projects_obligated_on_or_after"), _iso_param(
            FHWA_MFG, "phase_2_projects_obligated_on_or_after")
        mpct = P(FHWA_MFG, "phase_2_domestic_component_cost_pct_greater_than")
        if j.obligation is None:
            mfg = (f"Manufactured products: made in the U.S. on projects obligated on or after {p1}; also over "
                   f"{_n(mpct)}% U.S. components on projects obligated on or after {p2}. Ask the agency for the "
                   "project's federal obligation date (give fhwa_obligation_date).")
        elif j.obligation >= p2:
            mfg = (f"Manufactured products (obligated {j.obligation}, on or after {p2}): made in the U.S. with over "
                   f"{_n(mpct)}% U.S. component cost.")
        elif j.obligation >= p1:
            mfg = f"Manufactured products (obligated {j.obligation}, on or after {p1}): manufactured in the U.S."
        else:
            mfg = (f"Manufactured products: the project was obligated {j.obligation}, before {p1}; the rulebook's "
                   "FHWA rule does not cover it, so follow the contract's Buy America provisions.")
        ctx.req("Domestic content", "FHWA Buy America (23 CFR 635.410)", True, "Federal-aid highway project.",
                ("Steel and iron permanently incorporated must be made in the U.S., coatings included; foreign "
                 f"steel/iron is allowed only up to {_usd(allow)} delivered cost on this contract (track it "
                 f"cumulatively). {mfg} Check items with buy_america_check."), [FHWA_STEEL, FHWA_MFG])
    else:
        if j.stafford:
            ctx.req("Domestic content", "Build America, Buy America (BABA)", False,
                    "Stafford Act disaster and emergency assistance is exempt.", "", [BABA_STAFFORD])
        else:
            infra = j.infra if j.infra is not None else (True if j.fund in USDOT else None)
            examples = ", ".join(P(BABA, "infrastructure_examples")[:6])
            why = ("Federally assisted infrastructure project." if infra else
                   "Not infrastructure (you said so)." if infra is False else
                   f"Applies if the project is infrastructure (for example {examples}); give infrastructure_project.")
            what = ""
            if infra is not False:
                what = (f"Every iron and steel product, manufactured product and construction material incorporated "
                        "into the project must be produced in the U.S.: iron/steel with all processes from melting "
                        "through coating in the U.S.; manufactured products made in the U.S. with over "
                        f"{_n(P(BABA_MFG, 'domestic_component_cost_pct_greater_than'))}% U.S. component cost; the "
                        "listed construction materials (lumber, glass, drywall, plastics, non-ferrous metals, ...) "
                        "with all their processes in the U.S. Cement and aggregates are excluded. Only the federal "
                        "agency can waive it, on the awarding agency's written request (over "
                        f"{_n(P(BABA_WAIVER, 'unreasonable_cost_project_increase_pct_greater_than'))}% project cost "
                        "increase, non-availability, or public interest). Check items with buy_america_check.")
                if j.fund in ("federal_transit", "federal_airport"):
                    what += (" FTA and FAA program-specific domestic content rules are not in the rulebook; follow the "
                             "contract's Buy America provisions as well.")
            ctx.req("Domestic content", "Build America, Buy America (BABA)", infra, why, what,
                    [BABA, BABA_IRON, BABA_MFG, BABA_EXCL, BABA_WAIVER] if infra is not False else [BABA])
    names = ", ".join(P(TELECOM, "prohibited_manufacturers"))
    ctx.req("Domestic content", "Domestic preference and banned telecom equipment", True,
            "Clauses required under every federal grant (2 CFR 200.322 and 200.216).",
            ("Flow the domestic-preference clause into every subcontract and purchase order, and do not furnish "
             f"covered telecom or video surveillance equipment from {names} (or systems that use it as a "
             "substantial or essential component); 2 CFR 200.216 says which uses are covered."), [DOM_PREF, TELECOM])


def _sec_dbe(ctx: _Ctx, j: _Job) -> None:
    pause = _iso_param(DBE_GOALS, "effective")
    deadline = _iso_param(DBE_REEVAL, "ucp_completion_deadline")
    ext = P(DBE_REEVAL, "ucp_extension_max_days")
    late = _iso_param(DBE_REEVAL, "nonresponsive_dbe_submission_deadline")
    notice = P(DBE_TERM, "dbe_response_days")
    if ctx.today >= pause:
        status = (f"the CUCP must finish by {deadline} (one extension of up to {ext} days is allowed)"
                  if ctx.today <= deadline else
                  f"its {deadline} deadline (plus any extension of up to {ext} days) has passed, so ask the agency "
                  "whether the CUCP has finished")
        ctx.req("DBE", "DBE contract goals and counting are paused", False,
                (f"Since {pause}, USDOT recipients may not set DBE contract goals, and DBE participation does not "
                 f"count, until the California UCP (CUCP) finishes re-evaluating every DBE; {status}."),
                ("Do not expect DBE goals or good-faith-effort packages on new bids until the agency announces "
                 "otherwise. On contracts already awarded with DBE commitments, keep them: replacing a listed DBE "
                 f"needs the agency's written consent and {notice} days' written notice to the DBE."),
                [DBE_GOALS, DBE_REEVAL, DBE_COUNT, DBE_TERM])
        if ctx.today <= deadline:
            ctx.date("CUCP DBE re-evaluation deadline (goals may return after it finishes)", deadline, DBE_REEVAL,
                     kind="milestone")
        if ctx.today <= late:
            ctx.date("Last day for a non-responding DBE to send its re-evaluation documents (else disqualified)",
                     late, DBE_REEVAL, kind="milestone")
    else:
        days = P(DBE_GFE, "days_after_bid_opening")
        ctx.req("DBE", "DBE contract goal commitments", None,
                "Applies when the contract has a DBE goal.",
                (f"Submit DBE commitments (names, work, dollars, written confirmations) or good faith efforts with "
                 f"the bid or within {days} days after bid opening, as the solicitation says. Replacing a listed DBE "
                 f"later needs the agency's written consent and {notice} days' notice to the DBE."),
                [DBE_GFE, DBE_TERM])
        if j.bid_open:
            ctx.date("Latest DBE commitment / good faith efforts submission (if the contract has a goal)",
                     j.bid_open + timedelta(days=days), DBE_GFE)
    ctx.req("DBE", "DBE assurance clause and bidders list", True, "Every USDOT-assisted contract and subcontract.",
            ("Include the DBE nondiscrimination assurance in every subcontract" +
             ("; with your bid, give the agency bidders-list data (name, address, DBE status, NAICS, age, gross "
              "receipts) for yourself and every sub that quoted." if j.is_prime else ".")),
            [DBE_ASSURE] + ([DBE_BIDDERS] if j.is_prime else []))


def _sec_payments(ctx: _Ctx, j: _Job) -> None:
    if j.fund == "direct_federal":
        ctx.req("Payments", f"Pay subcontractors within {P(PP_SUBS, 'days_after_receipt_of_payment')} days of being "
                "paid", True, "FAR 52.232-27 (all tiers).",
                (f"Pay each subcontractor for satisfactory work within {P(PP_SUBS, 'days_after_receipt_of_payment')} "
                 "days after the government pays you, or owe interest; flow the same term down. If you withhold, "
                 "notify the sub in writing (copy to the contracting officer) and owe the government interest on "
                 f"the withheld amount from day {P(PP_INTEREST, 'interest_starts_day_after_receipt')} after you were "
                 "paid."), [PP_SUBS, PP_INTEREST])
        ctx.req("Payments", "Retainage", True, "FAR 52.232-5 and 52.232-27.",
                (f"The government may retain up to {_n(P(GOV_RET, 'max_retainage_pct'))}% only when progress is "
                 "unsatisfactory. You may retain from subs only if the subcontract says so, and may not bill retained "
                 "amounts until the sub is entitled to them."), [GOV_RET, PP_RETAINAGE])
        return
    days = P(BP7108, "deadline_days")
    if j.fund in USDOT:
        ctx.req("Payments", f"Pay subcontractors within {days} days of being paid", True,
                "California B&P 7108.5 (stricter) and the federal DBE prompt-payment rule (all subs, all tiers).",
                (f"Pay each sub its share within {days} days after you receive each progress payment (B&P 7108.5, "
                 "unless otherwise agreed in writing); the federal outer limit is "
                 f"{P(DBE_PAY, 'days_after_receipt_of_payment')} days. Return a sub's retainage within "
                 f"{P(DBE_RET, 'retainage_return_days_after_sub_work_completed')} days after its work is "
                 "satisfactorily completed (or under the agency's chosen retainage method); California retention "
                 "rules also apply (see public_works_readiness)."), [BP7108, DBE_PAY, DBE_RET])
    else:
        ctx.req("Payments", f"Pay subcontractors within {days} days of being paid", True, "California B&P 7108.5.",
                (f"Pay each sub its share within {days} days after you receive each progress payment, unless "
                 "otherwise agreed in writing."), [BP7108])


def _sec_clauses(ctx: _Ctx, j: _Job, include_unverified: bool, dba: Optional[bool]) -> None:
    v = j.value
    if j.fund in ASSISTED:
        t = P(APPX_II, "clauses_by_threshold_usd")
        names = {"termination_for_cause_and_convenience_over": "termination for cause and convenience",
                 "breach_remedies_over": "remedies for breach",
                 "clean_air_and_clean_water_over": "Clean Air Act and Clean Water Act compliance",
                 "byrd_anti_lobbying_certification_over": "anti-lobbying certification (each tier to the tier above)"}
        has = [f"{names[k]} (over {_usd(t[k])})" for k in names if v > t[k]]
        not_yet = [f"{names[k]} (over {_usd(t[k])})" for k in names if v <= t[k]]
        ctx.req("Contract clauses", "Required federal grant clauses (2 CFR 200 Appendix II)", True,
                f"Prime contract {_usd(v)}.",
                ("Your contract must carry: " + ("; ".join(has) if has else "none of the dollar-threshold clauses")
                 + ". Not required at this value: " + ("; ".join(not_yet) if not_yet else "none") +
                 ". Equal opportunity, debarment, recovered materials, telecom and domestic-preference clauses apply "
                 "without a dollar threshold (with the limits in their own rules)."), [APPX_II, BYRD])
    elif j.fund == "direct_federal":
        thr, hist = _on(ctx.today, BYRD, "far_direct_federal_threshold_usd", "far_direct_federal_prior_threshold_usd",
                        "far_threshold_change_effective")
        applies = v > thr
        ctx.req("Contract clauses", "Anti-lobbying certification (FAR 52.203-11 / 52.203-12)", applies,
                f"{_usd(v)} {'>' if applies else 'is not over'} {_usd(thr)} ({hist}).",
                ("Certify that no federal appropriated funds paid for lobbying, disclose other lobbying, and get the "
                 "same certification from each sub tier.") if applies else "", [BYRD])
        if dba:
            acc = P(EO13706, "accrual_hours_per_hours_worked")
            cap = P(EO13706, "annual_accrual_cap_min_hours")
            ctx.req("Leave", "Federal paid sick leave (EO 13706, FAR 52.222-62)", True,
                    "Contracts with the Davis-Bacon clause (FAR 52.222-6) include it.",
                    (f"Each covered worker earns at least {acc['earned']} hour of paid sick leave per "
                     f"{acc['per_hours_worked']} hours worked on the contract (you may cap accrual at {cap} hours a "
                     f"year, or front-load {P(EO13706, 'frontload_option_hours')}); tell workers their balance in "
                     "writing each pay period (or each month, if shorter). California paid sick leave applies too; "
                     "leave required by law cannot be credited toward Davis-Bacon fringes."),
                    [EO13706, FRINGE_NO_CREDIT])
        if include_unverified:
            ctx.req("Wages", "Federal contractor minimum wage (EO 14026 / EO 13658)", None,
                    ("UNVERIFIED: reported revoked on "
                     f"{P(EO14026, 'revocation_date')} by {P(EO14026, 'eo_14026_revoked_by')} and not enforced by DOL, "
                     "but 29 CFR part 23 and FAR 52.222-55 were still printed in the CFR on "
                     f"{P(EO14026, 'cfr_29_part_23_still_codified_as_of')}. The primary text could not be read."),
                    ("No federal contractor minimum wage is computed. If your contract includes FAR 52.222-55, ask "
                     "the contracting officer in writing whether it is enforced; Davis-Bacon and California minimum "
                     "wages still apply."), [EO14026])
    if j.fund == "federal_aid_highway":
        removed = _iso_param(FHWA_1273, "subpart_a_removed_effective")
        if ctx.today >= removed:
            why = (f"23 CFR part 633 (the rule that required Form FHWA-1273 in every contract) was removed effective "
                   f"{removed}; the Davis-Bacon, CWHSSA and Copeland clauses still apply by operation of law.")
            what = ("If your contract still includes FHWA-1273, follow it and attach it to subcontracts as the "
                    "contract says.")
        else:
            why = f"Until {removed}, 23 CFR 633 subpart A required FHWA-1273 in each contract and subcontract."
            what = "Physically attach FHWA-1273 to every subcontract (incorporation by reference was not allowed)."
        ctx.req("Contract clauses", "Form FHWA-1273", None if ctx.today >= removed else True, why, what,
                [FHWA_1273, DBA_OPLAW])


def _far_pct(year: int) -> tuple[float, str]:
    """FAR 52.225-9 domestic component percentage for items delivered in `year` -> (percent, band text)."""
    bands = P(FAR_BA, "domestic_component_pct_greater_than_by_delivery_year")
    starts = []
    for key, pct in bands.items():
        m = re.fullmatch(r"(\d{4})(?:-(\d{4})|\+)", str(key).strip())
        if not m:
            raise KeyError(f"rule {FAR_BA}: cannot read delivery-year band {key!r}")
        lo, hi = int(m.group(1)), int(m.group(2)) if m.group(2) else 9999
        starts.append(lo)
        if lo <= year <= hi:
            return pct, (f"{lo}-{hi}" if m.group(2) else f"from {lo}")
    return P(FAR_BA, "items_delivered_before_2024_pct"), f"before {min(starts)}"


# ---------------------------------------------------------------- tool 1
def federal_project_requirements(prime_contract_value_usd, funding_source, awarding_agency=None, is_prime=True,
                                 your_contract_value_usd=None, work_type="construction", davis_bacon_related_act=None,
                                 is_small_business=False, fixed_price=True, design_build=False,
                                 period_of_performance_days=None, infrastructure_project=None,
                                 stafford_act_disaster_funds=False, recipient_sat_usd=None, bid_price_usd=None,
                                 bid_guarantee_pct=None, bid_guarantee_cap_usd=None, specialty_items_usd=None,
                                 bid_opening_date=None, award_date=None, fhwa_obligation_date=None, as_of=None,
                                 include_unverified=True) -> FederalRequirementsResult:
    """Which federal rules a California construction contract triggers, with the threshold math for each."""
    today = _today(as_of)
    value = _number(prime_contract_value_usd, "prime_contract_value_usd")
    mine = _number(your_contract_value_usd, "your_contract_value_usd", allow_none=True)
    if is_prime and mine is not None and abs(mine - value) > 0.005:
        raise ValueError("is_prime is true, so your contract IS the prime contract: leave your_contract_value_usd "
                         "empty, or set is_prime=false if you are a subcontractor")
    if mine is not None and mine > value + 0.005:
        raise ValueError(f"your_contract_value_usd ({_usd(mine)}) cannot exceed the prime contract "
                         f"({_usd(value)}); thresholds are measured on the prime contract")
    fund = normalize_funding(funding_source)
    kind, agency = classify_agency(awarding_agency)
    if fund == "direct_federal" and kind == "california_public_agency":
        raise ValueError(f"{agency} is a California public agency, so its contract is not a direct federal "
                         "contract. If federal money pays for it, use funding_source federal_aid_highway, "
                         "federal_transit, federal_airport or federal_grant.")
    if fund in ASSISTED and kind == "federal_agency":
        raise ValueError(f"{agency} is a federal agency: a contract it awards is a direct federal contract. Use "
                         "funding_source='direct_federal'.")
    work = normalize_work_type(work_type)
    pop = None
    if period_of_performance_days is not None:
        pop = _number(period_of_performance_days, "period_of_performance_days")
        if pop != int(pop):
            raise ValueError(f"period_of_performance_days must be a whole number, got {period_of_performance_days!r}")
        pop = int(pop)
    bg_pct = _number(bid_guarantee_pct, "bid_guarantee_pct", allow_none=True, positive=True)
    if bg_pct is not None and bg_pct > 100:
        raise ValueError(f"bid_guarantee_pct is a percent of the bid (e.g. "
                         f"{_n(P(BID_GUAR, 'solicitation_percent_min'))}), got {bid_guarantee_pct!r}")
    bid_open, award = _opt_date(bid_opening_date, "bid_opening_date"), _opt_date(award_date, "award_date")
    if bid_open and award and award < bid_open:
        raise ValueError(f"award_date ({award}) is before bid_opening_date ({bid_open}); check the dates")
    j = _Job(today=today, value=value, mine=mine, is_prime=bool(is_prime), fund=fund, kind=kind, agency=agency,
             work=work, dbra=davis_bacon_related_act, small=bool(is_small_business), fixed_price=bool(fixed_price),
             design_build=bool(design_build), pop=pop, infra=infrastructure_project,
             stafford=bool(stafford_act_disaster_funds),
             sat=_number(recipient_sat_usd, "recipient_sat_usd", allow_none=True),
             bid=_number(bid_price_usd, "bid_price_usd", allow_none=True, positive=True) or value,
             bg_pct=bg_pct, bg_cap=_number(bid_guarantee_cap_usd, "bid_guarantee_cap_usd", allow_none=True,
                                           positive=True),
             specialty=_number(specialty_items_usd, "specialty_items_usd", allow_none=True),
             bid_open=bid_open, award=award, obligation=_opt_date(fhwa_obligation_date, "fhwa_obligation_date"))
    ctx = _Ctx(today)
    role = "prime" if j.is_prime else "subcontractor"

    if fund == "none":
        dba = _sec_davis_bacon(ctx, j)
        res = FederalRequirementsResult(
            as_of=today.isoformat(), funding_source=fund, funding_label=FUNDING_LABELS[fund], awarding_agency=agency,
            awarding_agency_kind=kind, your_role=role, work_type=work, prime_contract_value_usd=_money(value),
            your_contract_value_usd=None if mine is None else _money(mine), davis_bacon_applies=False,
            california_prevailing_wage_also_applies=None,
            summary="No federal money: no federal labor standards, bonding or Buy America rules apply.",
            next_step=("Check California public works instead: run public_works_readiness with the awarding body and "
                       "contract amount."), requirements=ctx.reqs, bonds=BondPlan(regime="none"))
        return ctx.finish(res)

    dba = _sec_davis_bacon(ctx, j)
    if work != "material_supply":
        _sec_overtime(ctx, j)
    dual = _sec_california(ctx, j) if dba is not False else (False if fund == "direct_federal" else None)
    if dba is False and fund != "direct_federal":
        ctx.notes.append("Davis-Bacon does not attach, so California dual coverage was not assessed; California "
                         "prevailing wage may still apply under state law (see public_works_readiness).")
    bonds = _sec_bonds(ctx, j) if work != "material_supply" else BondPlan(regime="none")
    if not j.is_prime and bonds.regime != "none":
        bonds.notes.insert(0, "These are the prime contractor's bonds on the prime contract amount, not yours.")
    _sec_registration(ctx, j)
    if work != "material_supply":
        _sec_subcontracting(ctx, j, dba)
    _sec_domestic(ctx, j)
    if fund in USDOT:
        _sec_dbe(ctx, j)
    _sec_payments(ctx, j)
    _sec_clauses(ctx, j, include_unverified, dba)
    ctx.dates.sort(key=lambda d: d.due)

    applies = [r for r in ctx.reqs if r.applies]
    maybe = [r for r in ctx.reqs if r.applies is None]
    off = [r for r in ctx.reqs if r.applies is False]
    dba_txt = {True: "applies", False: "does not apply", None: "may apply"}[dba]
    ca_txt = {True: "California prevailing wage also applies (pay the higher rate)",
              False: "California prevailing wage does not apply", None: "California dual coverage is not settled"}[dual]
    summary = (f"{FUNDING_LABELS[fund]}, {_usd(value)} prime contract, you are the {role}. Davis-Bacon {dba_txt}; "
               f"{ca_txt}. {_count(len(applies), 'requirement applies', 'requirements apply')}, "
               f"{_count(len(maybe), 'needs', 'need')} a fact checked, "
               f"{_count(len(off), 'is', 'are')} not triggered at this value.")
    res = FederalRequirementsResult(
        as_of=today.isoformat(), funding_source=fund, funding_label=FUNDING_LABELS[fund], awarding_agency=agency,
        awarding_agency_kind=kind, your_role=role, work_type=work, prime_contract_value_usd=_money(value),
        your_contract_value_usd=None if mine is None else _money(mine), davis_bacon_applies=dba,
        california_prevailing_wage_also_applies=dual, summary=summary, next_step=_next_step(ctx, j, dba, dual, bonds),
        requirements=ctx.reqs, bonds=bonds, key_dates=ctx.dates, line_items=ctx.lines, notes=ctx.notes)
    if j.fund == "federal_grant" and kind == "not_given":
        res.notes.append("Give awarding_agency (e.g. 'City of San Diego', 'SD', 'Caltrans') to settle California "
                         "dual coverage.")
    return ctx.finish(res)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _bond_tail(j: _Job, b: BondPlan) -> str:
    if not j.is_prime:
        return ""
    if b.regime == "miller_act":
        return (f" Line up your surety now: performance and payment bonds of {_usd(b.performance_bond_usd)} each and "
                f"a {_usd(b.bid_guarantee_usd)} bid guarantee (unless the solicitation states other figures).")
    if b.regime == "payment_protection":
        return f" Arrange the {_usd(b.payment_protection_usd)} payment protection the contracting officer selects."
    if b.regime == "2_cfr_200_326":
        return (f" Line up your surety now: a {_usd(b.bid_guarantee_usd)} bid guarantee and performance and payment "
                f"bonds of {_usd(b.performance_bond_usd)} each (unless the agency's own bonding policy was accepted).")
    return ""


def _next_step(ctx: _Ctx, j: _Job, dba: Optional[bool], dual: Optional[bool], bonds: BondPlan) -> str:
    soon = [d for d in ctx.dates if d.kind == "deadline" and d.status in ("overdue", "due_soon")]
    first = ""
    if soon:
        d = soon[0]
        first = (f"OVERDUE since {d.due}: {d.what}. " if d.status == "overdue" else f"By {d.due}: {d.what}. ")
    if dba is None and j.fund == "federal_grant" and j.dbra is None:
        return first + ("Ask the awarding agency in writing whether the grant program is a Davis-Bacon Related Act "
                        "and get the federal wage determination from the bid documents; plan your bid with federal "
                        "rates until they answer.")
    if dba is None and j.work == "demolition":
        return first + ("Ask the agency whether anything will be built on the site after demolition; if so, price "
                        "the job at the federal wage determination rates and set up weekly certified payroll.")
    if dual:
        return first + ("Pull the federal wage determination (SAM.gov) and the DIR prevailing wage determination for "
                        "every classification on the job and compare them with davis_bacon_vs_ca_prevailing: pay at "
                        "least the DIR basic rate in cash and the higher total package, and set up both the weekly "
                        "federal certified payroll and DIR eCPR." + _bond_tail(j, bonds))
    if dba:
        return first + ("Get the wage determination named in the contract, price every classification at its basic "
                        "rate plus fringe, and set up weekly certified payroll (WH-347) due within "
                        f"{P(COPE_7, 'days_after_regular_payment_date')} days after each payday."
                        + _bond_tail(j, bonds))
    if j.work == "material_supply":
        return first + ("Keep your role to delivery only (no installation or other work on the site), or you become "
                        "a covered subcontractor; confirm the Buy America rules for what you supply with "
                        "buy_america_check.")
    return first + ("Davis-Bacon does not attach at this value; review the requirements that still apply below and "
                    "confirm them against the contract documents." + _bond_tail(j, bonds))


# ---------------------------------------------------------------- tool 2: Davis-Bacon vs California rates
Coverage = Literal["dual", "federal_only"]
_COVERAGE_ALIASES = {
    "dual": ("dual", "both", "dual coverage", "federal and state", "federal and california", "federal aid",
             "federally assisted", "caltrans", "city", "county", "state and federal", "davis bacon and state"),
    "federal_only": ("federal only", "federal", "davis bacon only", "davis bacon", "dba only", "direct federal",
                     "federal_only", "federal enclave", "federal contract"),
}


def normalize_coverage(value) -> str:
    t = _words(value) or "dual"
    if t.replace(" ", "_") in _COVERAGE_ALIASES:
        return t.replace(" ", "_")
    for key, names in _COVERAGE_ALIASES.items():
        if t in names:
            return key
    raise ValueError(f"Unknown coverage {value!r}. Use 'dual' (federal money on a job a California agency controls: "
                     "both laws) or 'federal_only' (a federal agency's own contract)")


class PayCheck(BaseModel):
    cash_rate_paid_usd: float
    fringe_paid_usd: float
    total_paid_usd: float
    cash_short_per_hour_usd: float = 0
    total_short_per_hour_usd: float = 0
    raise_cash_by_usd: float = Field(0, description="Smallest straight-time cash raise per hour that fixes both tests")
    straight_time_shortfall_usd: Optional[float] = None
    overtime_cash_rate_paid_usd: Optional[float] = None
    overtime_short_per_hour_usd: Optional[float] = None
    overtime_shortfall_usd: Optional[float] = None
    complies: bool


class WageComparisonResult(core.Result):
    coverage: Coverage
    classification: Optional[str] = None
    federal_basic_rate_usd: float
    federal_fringe_rate_usd: float
    federal_total_usd: float
    state_basic_rate_usd: Optional[float] = None
    state_employer_payments_usd: Optional[float] = None
    state_total_usd: Optional[float] = None
    required_cash_rate_min_usd: Optional[float] = Field(
        description="Straight-time cash floor per hour (the DIR basic rate on dual jobs; None when Davis-Bacon alone "
                    "applies, which sets no separate cash floor)")
    required_total_min_usd: float = Field(description="Cash + creditable fringe per straight-time hour")
    total_controlled_by: Literal["state", "federal", "equal"]
    overtime_cash_min_usd: float = Field(description="Cash per overtime hour (the overtime multiplier x the basic "
                                                     "rate that applies)")
    overtime_total_min_usd: float = Field(description=(
        "Cash + fringe per overtime hour under the federal rule (multiplier x basic + fringe at straight time), or "
        "the DIR "
        "overtime rate when given and higher"))
    overtime_rule: str
    pay_check: Optional[PayCheck] = None
    explanation: str
    next_step: str
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def davis_bacon_vs_ca_prevailing(federal_basic_rate_usd, federal_fringe_rate_usd, state_basic_rate_usd=None,
                                 state_employer_payments_usd=None, state_total_rate_usd=None, coverage="dual",
                                 classification=None, cash_rate_paid_usd=None, fringe_paid_usd=None,
                                 cash_in_lieu_of_fringe_usd=None, straight_time_hours=None, overtime_hours=None,
                                 overtime_cash_rate_paid_usd=None,
                                 state_overtime_rate_usd=None) -> WageComparisonResult:
    """Compare a federal wage determination with the DIR determination for one classification."""
    cov = normalize_coverage(coverage)
    fb = _number(federal_basic_rate_usd, "federal_basic_rate_usd", positive=True)
    ff = _number(federal_fringe_rate_usd, "federal_fringe_rate_usd")
    sb = _number(state_basic_rate_usd, "state_basic_rate_usd", allow_none=True, positive=True)
    sp = _number(state_employer_payments_usd, "state_employer_payments_usd", allow_none=True)
    st = _number(state_total_rate_usd, "state_total_rate_usd", allow_none=True, positive=True)
    s_ot = _number(state_overtime_rate_usd, "state_overtime_rate_usd", allow_none=True, positive=True)
    cash = _number(cash_rate_paid_usd, "cash_rate_paid_usd", allow_none=True)
    fringe = _number(fringe_paid_usd, "fringe_paid_usd", allow_none=True)
    in_lieu = _number(cash_in_lieu_of_fringe_usd, "cash_in_lieu_of_fringe_usd", allow_none=True)
    hrs = _number(straight_time_hours, "straight_time_hours", allow_none=True)
    ot_hrs = _number(overtime_hours, "overtime_hours", allow_none=True)
    ot_cash = _number(overtime_cash_rate_paid_usd, "overtime_cash_rate_paid_usd", allow_none=True)
    ctx, notes = _Ctx(logic.today_ca()), []
    if cov == "dual":
        if sb is None or (sp is None and st is None):
            raise ValueError("coverage 'dual' needs the DIR determination: state_basic_rate_usd and either "
                             "state_employer_payments_usd (the fringe columns added up) or state_total_rate_usd")
        if st is None:
            st = sb + sp
        elif sp is None:
            if st < sb - 0.005:
                raise ValueError(f"state_total_rate_usd ({_usd(st)}) is below state_basic_rate_usd ({_usd(sb)})")
            sp = st - sb
        elif abs(sb + sp - st) > 0.01:
            raise ValueError(f"state basic {_usd(sb)} + employer payments {_usd(sp)} = {_usd(sb + sp)}, not the "
                             f"state total {_usd(st)}; check the DIR determination")
    elif any(x is not None for x in (sb, sp, st, s_ot)):
        notes.append("coverage is federal_only, so the state rates given were ignored.")
        sb = sp = st = s_ot = None
    if cash is None and (fringe is not None or hrs is not None):
        raise ValueError("give cash_rate_paid_usd (the straight-time cash rate you pay) to check your pay")
    if in_lieu is not None and cash is None:
        raise ValueError("cash_in_lieu_of_fringe_usd is part of cash_rate_paid_usd; give cash_rate_paid_usd too")
    if cash is not None and in_lieu is not None and in_lieu > cash:
        raise ValueError("cash_in_lieu_of_fringe_usd cannot exceed cash_rate_paid_usd")

    fed_total = fb + ff
    ctx.line("Federal total package", fed_total, "usd_per_hour",
             f"{_usd(fb)} basic + {_usd(ff)} fringe = {_usd(fed_total)}", FRINGE, DBA_PAY)
    mult_fed, mult_ca = P(OT_BASE, "overtime_multiplier"), P(PW_OT, "min_multiplier")
    if cov == "dual":
        cash_floor = sb
        total_min = max(st, fed_total)
        by = "equal" if abs(st - fed_total) < 0.005 else ("state" if st > fed_total else "federal")
        ctx.line("California total package", st, "usd_per_hour",
                 f"{_usd(sb)} basic + {_usd(sp)} employer payments = {_usd(st)}", CA_BASIC)
        ctx.line("Straight-time cash floor", sb, "usd_per_hour",
                 f"DIR basic hourly rate {_usd(sb)} (employer payments cannot reduce it)", CA_BASIC)
        ctx.line("Straight-time total package required", total_min, "usd_per_hour",
                 f"max(state {_usd(st)}, federal {_usd(fed_total)}) = {_usd(total_min)}", CA_HIGHER, CA_BASIC)
    else:
        cash_floor, total_min, by = None, fed_total, "federal"
        ctx.use(FRINGE)
    # Overtime: federal on the higher of the actual basic cash rate and the WD basic rate (29 CFR 5.32); California
    # 1.5x the basic rate (LC 1815). On dual jobs the lawful basic cash rate is at least the DIR basic rate.
    actual_basic = None
    if cash is not None:
        if in_lieu is None:
            # Presume the cash above the lowest lawful basic rate, up to the fringe still owed, is paid in place of
            # fringe benefits (it is what makes the total package); the rest of the cash is the basic rate.
            floor = max(fb, sb) if cov == "dual" else fb
            owed = max(0.0, total_min - floor - (fringe or 0.0))
            in_lieu = max(0.0, min(cash - floor, owed))
            if in_lieu > 0.005:
                notes.append(f"Presumed {_usd(in_lieu)} of your {_usd(cash)} cash rate is paid in place of fringe "
                             "benefits, so it is left out of the overtime base (29 CFR 5.32); give "
                             "cash_in_lieu_of_fringe_usd to override.")
        actual_basic = cash - in_lieu
    base = max([fb] + ([actual_basic] if actual_basic is not None else []) + ([sb] if cov == "dual" else []))
    ot_cash_min = mult_fed * base
    if cov == "dual":
        ot_cash_min = max(ot_cash_min, mult_ca * sb)
    fed_ot_total = mult_fed * base + ff
    ot_total_min = max(fed_ot_total, s_ot) if s_ot is not None else fed_ot_total
    base_txt = (f"{_usd(base)} (the highest of WD basic {_usd(fb)}"
                + (f", your basic cash {_usd(actual_basic)}" if actual_basic is not None else "")
                + (f", DIR basic {_usd(sb)}" if cov == "dual" else "") + ")")
    ctx.line("Overtime cash per hour", ot_cash_min, "usd_per_hour",
             f"{_n(mult_fed)} x {base_txt} = {_usd(ot_cash_min)}", OT_BASE, *([PW_OT] if cov == "dual" else []))
    ctx.line("Overtime total per hour (federal)", fed_ot_total, "usd_per_hour",
             f"{_n(mult_fed)} x {_usd(base)} + {_usd(ff)} fringe at straight time = {_usd(fed_ot_total)}", OT_BASE)
    if s_ot is not None:
        ctx.line("Overtime total per hour required", ot_total_min, "usd_per_hour",
                 f"max(federal {_usd(fed_ot_total)}, DIR overtime rate {_usd(s_ot)}) = {_usd(ot_total_min)}",
                 OT_BASE, PW_OT, CA_HIGHER)
    if cov == "dual":
        ot_rule = (f"Overtime on this job: over {P(PW_OT, 'daily_hours_threshold')} hours a day or "
                   f"{P(PW_OT, 'weekly_hours_threshold')} a week (California, LC 1815) and over "
                   f"{P(CWH_OT, 'weekly_hours_threshold')} a week (federal CWHSSA on contracts over "
                   f"{_usd(P(CWH_OT, 'contract_value_greater_than_usd'))}), at least {_n(mult_fed)}x the basic rate. "
                   "If the DIR determination's overtime rate is higher, it controls.")
        ctx.use(PW_OT, CWH_OT)
    else:
        ot_rule = (f"Federal overtime (CWHSSA): over {P(CWH_OT, 'weekly_hours_threshold')} hours a week at "
                   f"{_n(mult_fed)}x the basic rate, on contracts over the CWHSSA threshold; fringes and cash paid in "
                   "place of fringes are left out of the overtime base, which is never below the WD basic rate.")
        ctx.use(CWH_OT)

    check = None
    if cash is not None:
        fr = fringe or 0.0
        paid = cash + fr
        cash_short = max(0.0, cash_floor - cash) if cash_floor is not None else 0.0
        total_short = max(0.0, total_min - paid)
        raise_by = max(cash_short, total_short)
        check = PayCheck(cash_rate_paid_usd=_money(cash), fringe_paid_usd=_money(fr), total_paid_usd=_money(paid),
                         cash_short_per_hour_usd=_money(cash_short), total_short_per_hour_usd=_money(total_short),
                         raise_cash_by_usd=_money(raise_by), complies=_money(raise_by) == 0)
        ctx.line("Your straight-time pay", paid, "usd_per_hour", f"{_usd(cash)} cash + {_usd(fr)} fringe = {_usd(paid)}"
                 + (f"; cash short {_usd(cash_short)}" if cash_short else "") +
                 (f"; total short {_usd(total_short)}" if total_short else "") +
                 (f" -> raise cash by {_usd(raise_by)}/h" if raise_by else " -> meets both tests"),
                 FRINGE, *([CA_BASIC] if cov == "dual" else []))
        if hrs is not None:
            check.straight_time_shortfall_usd = _money(raise_by * hrs)
            if raise_by:
                ctx.line("Straight-time back pay", raise_by * hrs, "usd",
                         f"{_usd(raise_by)} x {_n(hrs)} h = {_usd(raise_by * hrs)}", FRINGE)
        if ot_cash is not None:
            ot_short = max(0.0, ot_cash_min - ot_cash, ot_total_min - (ot_cash + fr))
            check.overtime_cash_rate_paid_usd = _money(ot_cash)
            check.overtime_short_per_hour_usd = _money(ot_short)
            if ot_hrs is not None:
                check.overtime_shortfall_usd = _money(ot_short * ot_hrs)
            check.complies = check.complies and _money(ot_short) == 0
            if cov == "dual" and s_ot is None:
                notes.append("Overtime pay was checked against the federal total and the California cash floor only; "
                             "give state_overtime_rate_usd (the DIR determination's overtime rate) to check the state "
                             "total too.")
            ctx.line("Your overtime pay", ot_cash + fr, "usd_per_hour",
                     f"{_usd(ot_cash)} cash + {_usd(fr)} fringe; short {_usd(ot_short)}/h"
                     + (f" x {_n(ot_hrs)} h = {_usd(ot_short * ot_hrs)}" if ot_hrs is not None and ot_short else ""),
                     OT_BASE, *([PW_OT] if cov == "dual" else []))
        if fr:
            ctx.use(FRINGE_ANNUAL, FRINGE_QTR, FRINGE_NO_CREDIT)
            notes.append("Fringe credit per hour = plan cost for the period / ALL hours worked (public and private) "
                         "in that period, paid at least quarterly; no credit for benefits the law already requires "
                         "(workers' comp, required sick leave).")
    elif ot_cash is not None:
        notes.append("overtime_cash_rate_paid_usd needs cash_rate_paid_usd too; overtime pay was not checked.")

    who = f" for {classification}" if classification else ""
    if cov == "dual":
        ot_txt = (f"at least {_usd(ot_cash_min)} cash and {_usd(ot_total_min)} total per overtime hour"
                  if s_ot is not None else
                  f"at least {_usd(ot_cash_min)} cash and, under the federal rule, {_usd(ot_total_min)} total per "
                  "overtime hour; the DIR determination's overtime rate controls if higher (give "
                  "state_overtime_rate_usd to apply it)")
        explanation = (f"Dual coverage{who}: California does not let fringe credits reduce the DIR basic rate, so the "
                       f"cash floor is {_usd(sb)} an hour; the total package must be the higher of the state "
                       f"{_usd(st)} and federal {_usd(fed_total)} totals, so {_usd(total_min)} ({by} controls"
                       f"{'' if by != 'equal' else ': both are the same'}). Overtime: {ot_txt}.")
        if fb > sb:
            explanation += (f" The federal basic rate ({_usd(fb)}) is above the DIR basic rate, so the federal "
                            "overtime base is the federal rate.")
    else:
        explanation = (f"Davis-Bacon only{who}: cash plus creditable fringe must total at least {_usd(fed_total)} an "
                       "hour, in any mix of cash and bona fide fringe payments (29 CFR 5.31(b)(3)); there is no "
                       f"separate cash floor for straight time. Overtime: {_n(mult_fed)}x {_usd(base)} + {_usd(ff)} "
                       f"fringe = {_usd(fed_ot_total)} per overtime hour.")
    ex_b, ex_f = P(FRINGE, "dol_example.basic_hourly_rate_usd"), P(FRINGE, "dol_example.fringe_rate_usd")
    ex_t = P(FRINGE, "dol_example.all_cash_total_usd")
    if abs(fb - ex_b) < 0.005 and abs(ff - ex_f) < 0.005 and abs(ex_b + ex_f - ex_t) >= 0.005:
        notes.append(f"These are the rates of the example in 29 CFR 5.31(b). The regulation states a "
                     f"{_usd(ex_t)} total for {_usd(ex_b)} + {_usd(ex_f)}, but they add up to {_usd(ex_b + ex_f)}; "
                     "this tool always adds the rates you give.")
    if check is None:
        step = (f"Set this classification's pay at no less than {_usd(cash_floor)} cash and {_usd(total_min)} "
                f"total per straight-time hour and {_usd(ot_cash_min)} cash / {_usd(ot_total_min)} total per "
                "overtime hour, and enter it the same way on the federal certified payroll and DIR eCPR."
                if cov == "dual" else
                f"Set this classification's pay at no less than {_usd(total_min)} per straight-time hour (cash plus "
                f"creditable fringe) and {_usd(fed_ot_total)} per overtime hour.")
    elif check.complies:
        step = "Your rates meet the test; keep the fringe credit annualized and documented in case of an audit."
    else:
        parts = []
        if check.raise_cash_by_usd:
            parts.append(f"raise the straight-time cash rate by {_usd(check.raise_cash_by_usd)} to "
                         f"{_usd(cash + check.raise_cash_by_usd)} an hour")
        if check.overtime_short_per_hour_usd:
            parts.append(f"pay {_usd(check.overtime_short_per_hour_usd)} more per overtime hour")
        owed = (check.straight_time_shortfall_usd or 0) + (check.overtime_shortfall_usd or 0)
        step = ("On the next payroll, " + " and ".join(parts) +
                (f", and pay {_usd(owed)} in back wages for the hours given" if owed else "") +
                "; then correct the certified payrolls already filed.")
    res = WageComparisonResult(
        coverage=cov, classification=classification, federal_basic_rate_usd=_money(fb),
        federal_fringe_rate_usd=_money(ff), federal_total_usd=_money(fed_total),
        state_basic_rate_usd=None if sb is None else _money(sb),
        state_employer_payments_usd=None if sp is None else _money(sp),
        state_total_usd=None if st is None else _money(st),
        required_cash_rate_min_usd=None if cash_floor is None else _money(cash_floor),
        required_total_min_usd=_money(total_min), total_controlled_by=by, overtime_cash_min_usd=_money(ot_cash_min),
        overtime_total_min_usd=_money(ot_total_min), overtime_rule=ot_rule, pay_check=check, explanation=explanation,
        next_step=step, line_items=ctx.lines, notes=notes)
    ctx.use(DBA_PAY, *([CA_HIGHER, CA_BASIC] if cov == "dual" else []))
    return ctx.finish(res)


# ---------------------------------------------------------------- tool 3: overtime exposure
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


_Date = date


class TimeEntry(BaseModel):
    worker: str = Field(min_length=1, description="Worker name or ID")
    date: _Date = Field(description="Work date (YYYY-MM-DD or MM/DD/YYYY)")
    hours: float = Field(ge=0, le=24, allow_inf_nan=False, description="Hours worked that day on the job")
    overtime_hours_paid: float = Field(0, ge=0, le=24, allow_inf_nan=False,
                                       description="Hours of that day's work paid at the overtime rate or more")

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, v):
        return _date(v, "date")

    @field_validator("worker", mode="before")
    @classmethod
    def _strip(cls, v):
        return str(v or "").strip()


class WorkerWeek(BaseModel):
    worker: str
    week_start: str
    hours: float
    federal_overtime_hours: float
    california_overtime_hours: Optional[float] = None
    overtime_hours_paid: float
    unpaid_overtime_hours: float
    liquidated_damages_days: list[str] = Field(default_factory=list)
    lc1813_days: list[str] = Field(default_factory=list)
    back_wages_usd: Optional[float] = None
    math: str


class OvertimeExposureResult(core.Result):
    as_of: str
    cwhssa_applies: Optional[bool]
    cwhssa_reason: str
    california_public_works: bool
    workweek_starts_on: str
    weeks: list[WorkerWeek] = Field(default_factory=list)
    unpaid_overtime_hours: float = 0
    back_wages_usd: Optional[float] = None
    liquidated_damages_per_worker_day_usd: float
    liquidated_damages_worker_days: int = 0
    liquidated_damages_usd: float = 0
    agency_may_waive: bool = False
    lc1813_per_worker_day_usd: Optional[float] = None
    lc1813_worker_days: int = 0
    lc1813_penalty_usd: float = 0
    total_exposure_usd: Optional[float] = None
    next_step: str
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _bool(s) -> bool:
    return _words(s) in ("1", "true", "yes", "y", "t")


def parse_timesheet(records) -> list[TimeEntry]:
    """A list of {worker, date, hours, overtime_hours_paid} or CSV text with those columns (aliases: name/employee,
    day/work_date, hrs/hours_worked, ot_paid/ot_hours_paid)."""
    if isinstance(records, str):
        text = records.strip()
        if not text:
            raise ValueError("timesheet is empty")
        sample = text.splitlines()[0]
        delim = "\t" if "\t" in sample else ";" if sample.count(";") > sample.count(",") else ","
        rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
        alias = {"worker": ("worker", "name", "employee", "employee_name", "worker_name"),
                 "date": ("date", "day", "work_date"),
                 "hours": ("hours", "hrs", "hours_worked", "total_hours"),
                 "overtime_hours_paid": ("overtime_hours_paid", "ot_paid", "ot_hours_paid", "overtime_paid",
                                         "ot_hours")}
        out = []
        for n, row in enumerate(rows, start=2):
            low = {_words(k).replace(" ", "_"): (v or "").strip() for k, v in row.items() if k}
            rec = {}
            for field, names in alias.items():
                hit = next((low[x] for x in names if x in low and low[x] != ""), None)
                if hit is not None:
                    rec[field] = hit
            try:
                out.append(TimeEntry.model_validate(rec))
            except Exception as e:
                raise ValueError(f"timesheet line {n}: {_why(e)}") from None
        records = out
    if not isinstance(records, list) or not records:
        raise ValueError("timesheet must be a non-empty list of {worker, date, hours, overtime_hours_paid} or CSV text")
    entries = []
    for i, r in enumerate(records, start=1):
        try:
            entries.append(r if isinstance(r, TimeEntry) else TimeEntry.model_validate(r))
        except Exception as e:
            raise ValueError(f"timesheet entry {i}: {_why(e)}") from None
    return entries


def _why(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            first = errs()[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            return f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
        except Exception:
            pass
    return str(e)


def _ld_rate(today: date, ctx: _Ctx) -> float:
    eff = _iso_param(CWH_LD, "effective")
    if today >= eff:
        return P(CWH_LD, "liquidated_damages_usd_per_worker_per_day")
    hist = {int(y): amt for y, amt in P(CWH_LD, "history_usd").items()}
    years = [y for y in hist if y < eff.year and y <= today.year]
    if not years:
        raise ValueError(f"as_of {today} is before the earliest CWHSSA liquidated damages amount in the rulebook "
                         f"({min(hist)})")
    y = max(years)
    ctx.notes.append(f"as_of is before {eff}: priced at the {y} amount ({_usd(hist[y])}); the exact change dates of "
                     "earlier amounts are not in the rulebook.")
    return hist[y]


def federal_overtime_exposure(timesheet, basic_rate_usd=None, prime_contract_value_usd=None, funding_source=None,
                              awarding_agency=None, california_public_works=None, straight_time_paid_for_all_hours=True,
                              workweek_starts_on="monday", as_of=None) -> OvertimeExposureResult:
    """CWHSSA overtime owed and liquidated damages from a timesheet, plus LC 1813 on California dual-coverage jobs."""
    today = _today(as_of)
    entries = parse_timesheet(timesheet)
    base = _number(basic_rate_usd, "basic_rate_usd", allow_none=True, positive=True)
    value = _number(prime_contract_value_usd, "prime_contract_value_usd", allow_none=True)
    fund = normalize_funding(funding_source) if funding_source not in (None, "") else None
    kind, agency = classify_agency(awarding_agency)
    wk = _words(workweek_starts_on) or "monday"
    wk = next((d for d in _WEEKDAYS if d.startswith(wk[:3])), None) if len(wk) >= 3 else None
    if wk is None:
        raise ValueError(f"workweek_starts_on must be a weekday name, got {workweek_starts_on!r}")
    start = _WEEKDAYS.index(wk)
    if california_public_works is None:
        ca = bool(kind == "california_public_agency" and fund not in ("direct_federal", "none"))
    else:
        ca = bool(california_public_works)
    if fund == "direct_federal" and california_public_works:
        raise ValueError("a direct federal contract is under federal control: California public works overtime "
                         "(LC 1813) does not apply; set california_public_works=false")
    ctx = _Ctx(today)

    # aggregate per worker per day
    days: dict[tuple[str, date], list[float]] = {}
    for e in entries:
        cur = days.setdefault((e.worker, e.date), [0.0, 0.0])
        cur[0] += e.hours
        cur[1] += e.overtime_hours_paid
    for (w, d), (h, paid) in days.items():
        if h > 24 + 1e-9:
            raise ValueError(f"{w} has {_n(h)} hours on {d}; a day has 24")
        if paid > h + 1e-9:
            raise ValueError(f"{w} on {d}: overtime_hours_paid ({_n(paid)}) is more than the hours worked ({_n(h)})")
    weeks: dict[tuple[str, date], list[tuple[date, float, float]]] = {}
    for (w, d), (h, paid) in sorted(days.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        ws = d - timedelta(days=(d.weekday() - start) % 7)
        weeks.setdefault((w, ws), []).append((d, h, paid))

    limit, mult = P(CWH_OT, "weekly_hours_threshold"), P(CWH_OT, "overtime_multiplier")
    daily, weekly = P(PW_OT, "daily_hours_threshold"), P(PW_OT, "weekly_hours_threshold")
    premium = (mult - 1) if straight_time_paid_for_all_hours else mult
    rows, ld_days, ca_days, unpaid_total, back_total = [], 0, 0, 0.0, 0.0
    for (w, ws), items in weeks.items():
        cum = cum_st = 0.0
        fed_ot = ca_ot = paid_week = ca_unpaid = 0.0
        per_day = []
        for d, h, paid in items:
            f_today = max(0.0, cum + h - limit) - max(0.0, cum - limit)
            cum += h
            d_ot = max(0.0, h - daily)
            st = h - d_ot
            w_today = max(0.0, cum_st + st - weekly) - max(0.0, cum_st - weekly)
            cum_st += st
            c_today = d_ot + w_today
            fed_ot += f_today
            ca_ot += c_today
            paid_week += paid
            ca_unpaid += max(0.0, c_today - paid)
            per_day.append((d, f_today, c_today, paid))
        fed_unpaid = max(0.0, fed_ot - paid_week)
        ld = [d.isoformat() for d, f, _, p in per_day if fed_unpaid > 1e-9 and f > p + 1e-9]
        lc = [d.isoformat() for d, _, c, p in per_day if ca and c > p + 1e-9]
        unpaid = max(fed_unpaid, ca_unpaid) if ca else fed_unpaid
        back = None if base is None else _money(unpaid * premium * base)
        ld_days += len(ld)
        ca_days += len(lc)
        unpaid_total += unpaid
        back_total += back or 0.0
        math_ = (f"{_n(cum)} h; federal OT = max(0, {_n(cum)} - {_n(limit)}) = {_n(fed_ot)} h"
                 + (f"; California OT (over {_n(daily)}/day + straight-time over {_n(weekly)}/week) = {_n(ca_ot)} h"
                    if ca else "") + f"; paid as OT {_n(paid_week)} h; unpaid {_n(unpaid)} h"
                 + (f" x {_n(premium)} x {_usd(base)} = {_usd(back)}" if back is not None and unpaid else ""))
        rows.append(WorkerWeek(worker=w, week_start=ws.isoformat(), hours=_money(cum),
                               federal_overtime_hours=_money(fed_ot),
                               california_overtime_hours=_money(ca_ot) if ca else None,
                               overtime_hours_paid=_money(paid_week), unpaid_overtime_hours=_money(unpaid),
                               liquidated_damages_days=ld, lc1813_days=lc, back_wages_usd=back, math=math_))

    # does CWHSSA apply to this contract?
    if value is None or fund is None:
        applies = None
        reason = ("Give prime_contract_value_usd and funding_source to confirm CWHSSA applies; computed as if it "
                  "does.")
        ctx.use(CWH_OT)
    elif fund == "none":
        applies, reason = False, "No federal money: CWHSSA does not apply."
        ctx.use(CWH_OT)
    elif fund == "direct_federal":
        thr, hist = _on(today, CWH_FAR, "far_clause_excluded_at_or_below_usd", "prior_value_usd", "change_effective")
        applies = value > thr
        reason = (f"Direct federal: {_usd(value)} {'>' if applies else 'is at or below'} {_usd(thr)} "
                  f"(FAR clause threshold; {hist}).")
        ctx.use(CWH_FAR, CWH_OT)
    else:
        thr = P(CWH_OT, "contract_value_greater_than_usd")
        applies = value > thr
        reason = f"Federally assisted: {_usd(value)} {'>' if applies else 'is not over'} {_usd(thr)}."
        ctx.use(CWH_OT)
    rate = _ld_rate(today, ctx)
    ld_amount = 0.0 if applies is False else ld_days * rate
    waive_max = P(CWH_LD, "agency_head_waiver_max_usd")
    ctx.use(CWH_LD, OT_BASE, DBA_FOREPERSON)
    if applies is not False:
        ctx.line("CWHSSA liquidated damages", ld_amount, "usd",
                 f"{ld_days} worker-days x {_usd(rate)} = {_usd(ld_amount)}", CWH_LD)
    fed_owes = applies is not False
    lc_rate = P(PEN_1813, "per_worker_per_day_usd") if ca else None
    lc_amount = ca_days * lc_rate if ca else 0.0
    if ca:
        ctx.line("California LC 1813 overtime penalty", lc_amount, "usd",
                 f"{ca_days} worker-days x {_usd(lc_rate)} = {_usd(lc_amount)}", PEN_1813, PW_OT)
        ctx.use(CA_HIGHER)
    owed_hours = sum(r.unpaid_overtime_hours for r in rows) if (fed_owes or ca) else 0.0
    back_wages = None if base is None else _money(back_total if (fed_owes or ca) else 0.0)
    if base is not None and owed_hours:
        ctx.line("Overtime back wages", back_wages, "usd",
                 f"{_n(owed_hours)} unpaid OT h x {_n(premium)} x {_usd(base)} = {_usd(back_wages)}"
                 + (" (straight time already paid; premium only)" if straight_time_paid_for_all_hours else
                    " (no pay at all for those hours)"), OT_BASE, *([PW_OT] if ca else []))
    total = None if base is None else _money(back_wages + ld_amount + lc_amount)
    notes = list(ctx.notes)
    notes.append("Hours already paid at time-and-a-half or more (for example California daily overtime) are counted "
                 "against the federal weekly overtime; a liquidated-damages day is a calendar day on which the worker "
                 f"worked hours beyond the {_n(limit)}th in the workweek that were not paid as overtime.")
    if base is None and owed_hours:
        notes.append("Give basic_rate_usd (the basic cash rate, at least the wage determination basic rate) to price "
                     "the back wages.")
    notes.append(f"Covered workers include working forepersons who spend more than "
                 f"{_n(P(DBA_FOREPERSON, 'foreperson_covered_if_trade_time_pct_greater_than'))}% of the week on trade "
                 "work. Unpaid wages also carry interest; the rate is not in the rulebook, so it is not computed.")
    may_waive = applies is not False and 0 < ld_amount <= waive_max
    if may_waive:
        notes.append(f"Liquidated damages of {_usd(waive_max)} or less may be waived or reduced by the agency head "
                     "if the violation was inadvertent; larger amounts need DOL concurrence.")
    if applies is False and ld_days:
        notes.append("CWHSSA does not apply to this contract, so no federal liquidated damages; pay any overtime "
                     "the FLSA, California law or the contract requires.")
    if not owed_hours:
        step = "No unpaid overtime found in this timesheet; keep the daily and weekly hours on the certified payroll."
    else:
        workers = sorted({r.worker for r in rows if r.unpaid_overtime_hours})
        money = f" ({_usd(back_wages)})" if back_wages is not None else ""
        step = (f"Pay the {_n(owed_hours)} unpaid overtime hours{money} to {len(workers)} worker"
                f"{'s' if len(workers) != 1 else ''} ({', '.join(workers[:5])}{'...' if len(workers) > 5 else ''}) on "
                "the next payroll and file corrected certified payrolls"
                + (" with the agency and DIR eCPR" if ca else "") +
                (f"; liquidated damages exposure {_usd(ld_amount)}" if ld_amount else "") +
                (f", LC 1813 penalties {_usd(lc_amount)}" if lc_amount else "") + ".")
    res = OvertimeExposureResult(
        as_of=today.isoformat(), cwhssa_applies=applies, cwhssa_reason=reason, california_public_works=ca,
        workweek_starts_on=wk, weeks=rows, unpaid_overtime_hours=_money(owed_hours), back_wages_usd=back_wages,
        liquidated_damages_per_worker_day_usd=rate, liquidated_damages_worker_days=0 if applies is False else ld_days,
        liquidated_damages_usd=_money(ld_amount), agency_may_waive=may_waive, lc1813_per_worker_day_usd=lc_rate,
        lc1813_worker_days=ca_days, lc1813_penalty_usd=_money(lc_amount), total_exposure_usd=total, next_step=step,
        line_items=ctx.lines, notes=notes)
    return ctx.finish(res)


# ---------------------------------------------------------------- tool 4: Buy America
Program = Literal["baba", "far_buy_american", "fhwa_buy_america"]
PROGRAM_LABELS = {"baba": "Build America, Buy America (2 CFR part 184; federally assisted infrastructure)",
                  "far_buy_american": "FAR Buy American - construction materials (52.225-9; direct federal)",
                  "fhwa_buy_america": "FHWA Buy America (23 CFR 635.410; federal-aid highway)"}
_PROGRAM_ALIASES = {
    "baba": ("baba", "build america buy america", "build america", "bil", "iija", "2 cfr 184", "grant",
             "federal grant", "federal_grant", "fta", "federal transit", "federal_transit", "faa", "aip",
             "federal_airport", "epa", "srf", "hud", "cdbg", "federally assisted"),
    "far_buy_american": ("far", "far buy american", "buy american", "buy american act", "52 225 9", "far 52 225 9",
                         "direct federal", "direct_federal", "usace", "army corps", "gsa", "va", "navy", "navfac"),
    "fhwa_buy_america": ("fhwa", "fhwa buy america", "federal aid highway", "federal_aid_highway", "federal aid",
                         "highway", "23 cfr 635", "23 cfr 635 410", "caltrans federal aid"),
}
ItemType = Literal["iron_steel", "manufactured_product", "construction_material", "excluded_material"]
_ITEM_ALIASES = {
    "iron_steel": ("iron_steel", "iron or steel", "iron and steel", "iron", "steel", "iron steel", "rebar",
                   "reinforcing steel", "structural steel", "steel pipe", "ductile iron pipe", "steel beams"),
    "manufactured_product": ("manufactured_product", "manufactured product", "manufactured", "product",
                             "equipment", "assembly"),
    "construction_material": ("construction_material", "construction material", "material"),
    "excluded_material": ("excluded_material", "excluded", "cement", "cementitious", "cementitious materials",
                          "aggregate", "aggregates", "stone", "sand", "gravel", "binder", "aggregate binder",
                          "additive", "additives"),
}
# Everyday names for the eight 2 CFR 184.3 construction materials (keys of the rule's materials_and_standards).
_MATERIAL_ALIASES = {
    "non-ferrous metals": ("non ferrous", "nonferrous", "non ferrous metal", "non ferrous metals", "aluminum",
                           "aluminium", "copper", "brass", "zinc"),
    "plastic and polymer-based products": ("plastic", "plastics", "polymer", "pvc", "hdpe", "cpvc", "abs",
                                           "plastic pipe", "pvc pipe", "hdpe pipe", "polymer based"),
    "glass": ("glass", "glazing", "window glass"),
    "fiber optic cable": ("fiber optic cable", "fiber optic", "fiber cable"),
    "optical fiber": ("optical fiber",),
    "lumber": ("lumber", "wood framing", "dimensional lumber"),
    "drywall": ("drywall", "gypsum board", "wallboard", "sheetrock", "gypsum wallboard"),
    "engineered wood": ("engineered wood",),
}


def normalize_program(value) -> str:
    t = _words(value)
    if not t:
        raise ValueError(f"program is required: one of {', '.join(PROGRAM_LABELS)}")
    if t.replace(" ", "_") in PROGRAM_LABELS:
        return t.replace(" ", "_")
    if t in ("buy america",):
        raise ValueError("'Buy America' is ambiguous: use fhwa_buy_america for federal-aid highway jobs, baba for "
                         "other federally assisted infrastructure, or far_buy_american for a federal agency's own "
                         "contract")
    for key, names in _PROGRAM_ALIASES.items():
        if t in names:
            return key
    raise ValueError(f"Unknown program {value!r}. Use one of: {', '.join(PROGRAM_LABELS)}")


def _classify_item(item_type, item_name) -> tuple[str, Optional[str]]:
    """-> (item type, BABA construction-material category or None)."""
    t = _words(item_type)
    material = None
    name = _words(item_name)
    for cat, names in _MATERIAL_ALIASES.items():
        if t in names or name in names or _words(cat) in (t, name):
            material = cat
    if t.replace(" ", "_") in _ITEM_ALIASES:
        return t.replace(" ", "_"), material
    for key, names in _ITEM_ALIASES.items():
        if t in names:
            return key, material
    if material:
        return "construction_material", material
    raise ValueError(f"Unknown item_type {item_type!r}. Use one of: {', '.join(_ITEM_ALIASES)} (or a construction "
                     f"material such as lumber, drywall, glass, PVC or aluminum)")


class Component(BaseModel):
    name: str = Field(min_length=1)
    cost_usd: Annotated[float, BeforeValidator(_clean_num)] = Field(ge=0, allow_inf_nan=False)
    domestic: bool = Field(description="Mined, produced or manufactured in the U.S.")
    iron_steel: bool = Field(False, description="Iron or steel component (mill products, castings, forgings)")


class DomesticContentResult(core.Result):
    program: Program
    program_label: str
    item: Optional[str] = None
    item_type: ItemType
    construction_material_category: Optional[str] = None
    applies: bool
    complies: Optional[bool] = Field(description="None when a fact the test needs was not given")
    test: str
    domestic_content_pct: Optional[float] = None
    required_pct_greater_than: Optional[float] = None
    foreign_iron_steel_pct: Optional[float] = None
    foreign_iron_steel_limit_pct_less_than: Optional[float] = None
    minimal_use_allowance_usd: Optional[float] = None
    minimal_use_remaining_usd: Optional[float] = None
    exception_possible: Optional[bool] = None
    exception_test: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    next_step: str
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def buy_america_check(program, item_type, item_name=None, components=None, domestic_component_cost_usd=None,
                      total_component_cost_usd=None, foreign_iron_steel_component_cost_usd=None,
                      manufactured_in_us=None, all_manufacturing_in_us=None, delivery_date=None, cots=False,
                      critical_item=False, total_contract_cost_usd=None, contract_foreign_iron_steel_cost_usd=None,
                      fhwa_obligation_date=None, cost_if_domestic_usd=None, cost_if_foreign_usd=None,
                      stafford_act_disaster_funds=False, as_of=None) -> DomesticContentResult:
    """Domestic-content test for one product or material under BABA, FAR Buy American or FHWA Buy America."""
    today = _today(as_of)
    prog = normalize_program(program)
    kind, material = _classify_item(item_type, item_name)
    comps = [c if isinstance(c, Component) else Component.model_validate(c) for c in (components or [])]
    dom = _number(domestic_component_cost_usd, "domestic_component_cost_usd", allow_none=True)
    tot = _number(total_component_cost_usd, "total_component_cost_usd", allow_none=True)
    fis = _number(foreign_iron_steel_component_cost_usd, "foreign_iron_steel_component_cost_usd", allow_none=True)
    if comps and (dom is not None or tot is not None or fis is not None):
        raise ValueError("give either components or the domestic/total/foreign iron-steel cost figures, not both")
    if comps:
        tot = sum(c.cost_usd for c in comps)
        dom = sum(c.cost_usd for c in comps if c.domestic)
        fis = sum(c.cost_usd for c in comps if c.iron_steel and not c.domestic)
        iron_share = (sum(c.cost_usd for c in comps if c.iron_steel) / tot * 100) if tot else None
    else:
        iron_share = None
    if tot is not None and tot == 0:
        raise ValueError("total component cost must be more than 0")
    if dom is not None and tot is not None and dom > tot + 0.005:
        raise ValueError(f"domestic component cost ({_usd(dom)}) cannot exceed the total ({_usd(tot)})")
    if fis is not None and tot is not None and fis > tot + 0.005:
        raise ValueError(f"foreign iron/steel cost ({_usd(fis)}) cannot exceed the total component cost ({_usd(tot)})")
    contract = _number(total_contract_cost_usd, "total_contract_cost_usd", allow_none=True, positive=True)
    foreign_steel = _number(contract_foreign_iron_steel_cost_usd, "contract_foreign_iron_steel_cost_usd",
                            allow_none=True)
    c_dom = _number(cost_if_domestic_usd, "cost_if_domestic_usd", allow_none=True, positive=True)
    c_for = _number(cost_if_foreign_usd, "cost_if_foreign_usd", allow_none=True, positive=True)
    if (c_dom is None) != (c_for is None):
        raise ValueError("give both cost_if_domestic_usd and cost_if_foreign_usd for the cost test")
    delivered = _opt_date(delivery_date, "delivery_date") or today
    obligation = _opt_date(fhwa_obligation_date, "fhwa_obligation_date")
    ctx = _Ctx(today)
    missing: list[str] = []
    applies, complies, test = True, None, ""
    dom_pct = None if dom is None or tot is None else dom / tot * 100
    out = dict(domestic_content_pct=None if dom_pct is None else round(dom_pct, 2))
    exception, exc_test = None, None

    def pct_line(threshold, *ids):
        if dom_pct is None:
            missing.append("components (or domestic_component_cost_usd and total_component_cost_usd)")
            return None
        ok = dom_pct > threshold
        ctx.line("Domestic component cost share", dom_pct, "percent",
                 f"{_usd(dom)} U.S. / {_usd(tot)} all components = {round(dom_pct, 2):g}% "
                 f"{'>' if ok else '<='} {_n(threshold)}%", *ids)
        return ok

    def need(flag, name):
        if flag is None:
            missing.append(name)
        return flag

    if prog == "baba":
        if stafford_act_disaster_funds:
            applies, complies, test = False, None, "Stafford Act disaster and emergency assistance is exempt from BABA."
            ctx.use(BABA_STAFFORD)
        elif kind == "excluded_material":
            applies, test = False, ("Section 70917(c) materials (" + ", ".join(
                P(BABA_EXCL, "excluded_section_70917c_materials")) + ") are not covered by themselves; classify each "
                "item once, as delivered to the site.")
            ctx.use(BABA_EXCL)
        else:
            iron_cut = P(BABA_IRON, "predominantly_iron_steel_cost_pct_greater_than")
            if kind == "manufactured_product" and iron_share is not None and iron_share > iron_cut:
                ctx.notes.append(f"Iron and steel are {round(iron_share, 2):g}% of component cost (over "
                                 f"{_n(iron_cut)}%), so BABA treats this item as an iron or steel product.")
                ctx.line("Iron/steel share of component cost", iron_share, "percent",
                         f"{round(iron_share, 2):g}% > {_n(iron_cut)}% -> iron or steel product", BABA_IRON)
                kind = "iron_steel"
            if kind == "iron_steel":
                test = "All manufacturing processes, from initial melting through coating, in the U.S."
                complies = need(all_manufacturing_in_us,
                                "all_manufacturing_in_us (melting through coating in the U.S.?)")
                ctx.use(BABA_IRON)
            elif kind == "manufactured_product":
                thr = P(BABA_MFG, "domestic_component_cost_pct_greater_than")
                test = f"Manufactured in the U.S. and U.S. components over {_n(thr)}% of all component cost."
                out["required_pct_greater_than"] = thr
                ok = pct_line(thr, BABA_MFG)
                made = need(manufactured_in_us, "manufactured_in_us")
                complies = False if (made is False or ok is False) else (None if made is None or ok is None else True)
                ctx.use(BABA_MFG)
            else:
                stds = P(BABA_CM, "materials_and_standards")
                if material is None:
                    missing.append(f"item_name: which construction material ({', '.join(stds)})")
                    test = ("All manufacturing processes for the material in the U.S. (the standard depends on the "
                            "material).")
                else:
                    test = f"{material.capitalize()}: {stds[material]}, all in the U.S."
                complies = need(all_manufacturing_in_us, "all_manufacturing_in_us")
                ctx.use(BABA_CM)
            ctx.use(BABA)
            if c_dom is not None:
                cut = P(BABA_WAIVER, "unreasonable_cost_project_increase_pct_greater_than")
                inc = (c_dom - c_for) / c_for * 100
                exception = inc > cut
                exc_test = (f"Overall project cost with U.S. items {_usd(c_dom)} vs {_usd(c_for)}: +{round(inc, 2):g}% "
                            f"{'>' if exception else '<='} {_n(cut)}% -> "
                            + ("an unreasonable-cost waiver can be requested (by the awarding agency, in writing; "
                               f"posted at least {P(BABA_WAIVER, 'public_comment_days_min')} days for comment)"
                               if exception else "no unreasonable-cost waiver"))
                ctx.line("Project cost increase from U.S. items", inc, "percent", exc_test, BABA_WAIVER)
    elif prog == "far_buy_american":
        ctx.use(FAR_BA)
        if kind == "excluded_material":
            applies, test = True, ("FAR 52.225-9 has no exclusion for cement or aggregates; the rulebook rule covers "
                                   "manufactured and iron/steel construction material only, so confirm unmanufactured "
                                   "materials with the contracting officer.")
        elif kind == "iron_steel":
            lim = P(FAR_BA, "iron_steel_foreign_content_pct_less_than")
            out["foreign_iron_steel_limit_pct_less_than"] = lim
            test = (f"Manufactured in the U.S. and foreign iron and steel under {_n(lim)}% of the cost of all "
                    "components" + (" (COTS: only the iron and steel content is tested, COTS fasteners excluded)"
                                    if cots else "") + ".")
            made = need(manufactured_in_us, "manufactured_in_us")
            if fis is None or tot is None:
                missing.append("components with iron_steel flags (or foreign_iron_steel_component_cost_usd and "
                               "total_component_cost_usd)")
                ok = None
            else:
                share = fis / tot * 100
                ok = share < lim
                out["foreign_iron_steel_pct"] = round(share, 2)
                ctx.line("Foreign iron/steel share", share, "percent",
                         f"{_usd(fis)} foreign iron/steel / {_usd(tot)} all components = {round(share, 2):g}% "
                         f"{'<' if ok else '>='} {_n(lim)}%", FAR_BA)
            complies = False if (made is False or ok is False) else (None if made is None or ok is None else True)
        else:
            pct, band = _far_pct(delivered.year)
            out["required_pct_greater_than"] = pct
            made = need(manufactured_in_us, "manufactured_in_us")
            if cots:
                test = ("COTS construction material: the domestic content test is waived; it must be manufactured "
                        "in the U.S.")
                complies = made
            else:
                test = (f"Manufactured in the U.S. and U.S. components over {_n(pct)}% of component cost (delivered "
                        f"{delivered.year}: band {band}).")
                ok = pct_line(pct, FAR_BA)
                complies = False if (made is False or ok is False) else (None if made is None or ok is None else True)
        if c_dom is not None:
            diff = P(FAR_BA, "unreasonable_cost_differential_pct")
            over = (c_dom - c_for) / c_for * 100
            if critical_item:
                exception = None
                exc_test = (f"Domestic costs {round(over, 2):g}% more. For a critical item the {_n(diff)}% "
                            "differential is increased by the FAR 25.105 preference factor, which is not in the "
                            "rulebook; ask the contracting officer.")
            else:
                exception = over > diff
                exc_test = (f"Domestic {_usd(c_dom)} vs foreign {_usd(c_for)}: +{round(over, 2):g}% "
                            f"{'>' if exception else '<='} {_n(diff)}% -> " +
                            ("the cost of domestic material is unreasonable: ask the contracting officer to allow the "
                             "foreign material (before award, or explain why you could not)" if exception else
                             "not unreasonable; use domestic material"))
            ctx.line("Domestic price premium", over, "percent", exc_test, FAR_BA)
    else:   # FHWA
        if kind == "excluded_material":
            applies, test = False, ("Section 70917(c) materials (cement, aggregates, binders) are excluded from the "
                                    "FHWA manufactured products rule.")
            ctx.use(FHWA_MFG)
        elif kind == "iron_steel":
            ctx.use(FHWA_STEEL)
            test = ("Steel and iron permanently incorporated: all manufacturing in the U.S., coatings included; "
                    "foreign steel/iron allowed only within the minimal-use allowance.")
            made = all_manufacturing_in_us
            if contract is not None:
                pct, floor = P(FHWA_STEEL, "minimal_use_pct_of_contract"), P(FHWA_STEEL, "minimal_use_floor_usd")
                allow = max(contract * pct / 100, floor)
                out["minimal_use_allowance_usd"] = _money(allow)
                ctx.line("Minimal-use allowance", allow, "usd",
                         f"max({_n(pct)}% x {_usd(contract)} = {_usd(contract * pct / 100)}, {_usd(floor)}) = "
                         f"{_usd(allow)}", FHWA_STEEL)
            if made:
                complies = True
            elif made is False or foreign_steel is not None:
                if contract is None or foreign_steel is None:
                    missing.append("total_contract_cost_usd and contract_foreign_iron_steel_cost_usd (cumulative "
                                   "foreign steel/iron on the contract, delivered cost, including this item)")
                else:
                    allow = out["minimal_use_allowance_usd"]
                    ok = foreign_steel <= allow
                    out["minimal_use_remaining_usd"] = _money(max(0.0, allow - foreign_steel))
                    ctx.line("Foreign steel/iron used on the contract", foreign_steel, "usd",
                             f"{_usd(foreign_steel)} {'<=' if ok else '>'} allowance {_usd(allow)}"
                             + (f"; {_usd(allow - foreign_steel)} left" if ok else
                                f"; {_usd(foreign_steel - allow)} over"), FHWA_STEEL)
                    complies = ok
            else:
                missing.append("all_manufacturing_in_us (melting through coating in the U.S.?)")
            if c_dom is not None:
                alt = P(FHWA_ALT, "award_to_foreign_if_lowest_domestic_exceeds_lowest_foreign_by_pct_more_than")
                over = (c_dom - c_for) / c_for * 100
                exception = over > alt
                exc_test = (f"Alternate bids: lowest domestic {_usd(c_dom)} vs lowest foreign {_usd(c_for)}: "
                            f"+{round(over, 2):g}% {'>' if exception else '<='} {_n(alt)}% -> the contract goes to the "
                            + ("foreign-steel bid" if exception else "domestic bid"))
                ctx.line("Alternate bid difference", over, "percent", exc_test, FHWA_ALT)
        elif kind == "manufactured_product":
            ctx.use(FHWA_MFG)
            p1 = _iso_param(FHWA_MFG, "phase_1_projects_obligated_on_or_after")
            p2 = _iso_param(FHWA_MFG, "phase_2_projects_obligated_on_or_after")
            thr = P(FHWA_MFG, "phase_2_domestic_component_cost_pct_greater_than")
            if obligation is None:
                missing.append("fhwa_obligation_date (the date FHWA funds were obligated for the project; ask the "
                               "agency)")
                test = (f"Obligated on or after {p1}: manufactured in the U.S.; on or after {p2}: also over "
                        f"{_n(thr)}% U.S. component cost.")
            elif obligation >= p2:
                test = (f"Obligated {obligation} (on or after {p2}): manufactured in the U.S. and over {_n(thr)}% "
                        "U.S. components.")
                out["required_pct_greater_than"] = thr
                ok = pct_line(thr, FHWA_MFG)
                made = need(manufactured_in_us, "manufactured_in_us")
                complies = False if (made is False or ok is False) else (None if made is None or ok is None else True)
            elif obligation >= p1:
                test = f"Obligated {obligation} (on or after {p1}): manufactured in the U.S."
                complies = need(manufactured_in_us, "manufactured_in_us")
            else:
                applies, test = False, (f"Obligated {obligation}, before {p1}: the rulebook's FHWA manufactured "
                                        "products rule does not cover it; follow the contract's Buy America terms.")
            if applies and any(c.iron_steel for c in comps):
                special = "; ".join(f"{k}: {v}" for k, v in P(FHWA_MFG, "special_items").items())
                ctx.notes.append(f"Iron/steel components: {special} (23 CFR 635.410(b)); their cost still counts in "
                                 "the component calculation.")
        else:
            applies, test = False, ("The rulebook's FHWA rules cover steel/iron and manufactured products only; check "
                                    "the contract for other construction materials.")
            ctx.use(FHWA_STEEL, FHWA_MFG)
    if cots and prog != "far_buy_american":
        ctx.notes.append("cots only matters for FAR 52.225-9; it was ignored.")
    if critical_item and prog != "far_buy_american":
        ctx.notes.append("critical_item only matters for FAR 52.225-9; it was ignored.")
    if not applies:
        complies = None
        step = "No domestic-content test applies to this item under this program; keep the classification on file."
    elif complies and out.get("minimal_use_remaining_usd") is not None and not all_manufacturing_in_us:
        step = (f"It fits the minimal-use allowance for foreign steel/iron: {_usd(foreign_steel)} used of "
                f"{_usd(out['minimal_use_allowance_usd'])}, {_usd(out['minimal_use_remaining_usd'])} left. Log every "
                "foreign steel/iron item against this running total, and keep the delivered-cost records.")
    elif complies:
        step = ("It meets the test. Keep the supplier's certification (and the component cost breakdown) in the job "
                "file.")
    elif complies is None and missing:
        step = "Get from the supplier or agency: " + "; ".join(dict.fromkeys(missing)) + "; then run this check again."
    elif complies is None:
        step = ("The rulebook cannot settle this item; confirm in writing with the contracting officer or awarding "
                "agency before you buy it.")
    elif prog == "fhwa_buy_america":
        step = ("It does not meet the test: source a compliant item (U.S. steel/iron with U.S. coating, or a "
                "compliant manufactured product), and do not order this one without the agency's written direction.")
    else:
        route = {"baba": "ask the awarding agency to request a waiver from the federal agency",
                 "far_buy_american": "ask the contracting officer for an exception"}[prog]
        step = (f"It does not meet the test. Source a compliant item, or {route}"
                + (" (the cost test above supports it)" if exception else "") + ".")
    res = DomesticContentResult(program=prog, program_label=PROGRAM_LABELS[prog], item=item_name, item_type=kind,
                                construction_material_category=material if kind == "construction_material" else None,
                                applies=applies, complies=complies, test=test, exception_possible=exception,
                                exception_test=exc_test, missing=list(dict.fromkeys(missing)), next_step=step,
                                line_items=ctx.lines, notes=ctx.notes, **out)
    return ctx.finish(res)


# ---------------------------------------------------------------- MCP tools
def _tool_docs() -> dict[str, str]:
    """Tool descriptions for AI agents, with every legal number read from the rulebook so they never go stale."""
    today = logic.today_ca()
    cwh_far, _ = _on(today, CWH_FAR, "far_clause_excluded_at_or_below_usd", "prior_value_usd", "change_effective")
    sat, _ = _on(today, FAR_THRESH, "simplified_acquisition_threshold_usd",
                 "simplified_acquisition_threshold_prior_usd", "effective")
    plan, _ = _on(today, SUBK_PLAN, "construction_threshold_usd", "prior_construction_threshold_usd", "effective")
    ld = P(CWH_LD, "liquidated_damages_usd_per_worker_per_day")
    pct, band = _far_pct(today.year)
    return {
        "requirements": (
            "Federal rules for a construction contract in California. Give the prime contract amount and the funding "
            "source (direct_federal = a federal agency's own contract such as USACE, GSA, VA or Navy; "
            "federal_aid_highway = FHWA via Caltrans or a local agency; federal_transit = FTA; federal_airport = FAA; "
            "federal_grant = other federal grant/loan such as HUD CDBG, EPA SRF or FEMA; none), plus the awarding "
            "agency ('City of San Diego', 'SD', 'Caltrans', 'USACE'), your role and dates for more. Returns each "
            "requirement with applies true/false/null and the threshold math: Davis-Bacon over "
            f"{_usd(P(DBA, 'prime_contract_value_greater_than_usd'))} (statutory), weekly certified payroll due "
            f"{P(COPE_7, 'days_after_regular_payment_date')} days after payday, CWHSSA overtime over "
            f"{_usd(P(CWH_OT, 'contract_value_greater_than_usd'))} assisted / over {_usd(cwh_far)} direct federal, "
            f"Miller Act bonds over {_usd(P(MILLER, 'contract_value_greater_than_usd'))} (payment protection over "
            f"{_usd(P(MILLER_ALT, 'contract_value_greater_than_usd'))}) with the bid guarantee "
            f"(min({_n(P(BID_GUAR, 'solicitation_percent_min'))}% of bid, "
            f"{_usd(P(BID_GUAR, 'solicitation_dollar_cap_max_usd'))})), 2 CFR 200.326 bonds over the "
            f"{_usd(sat)} simplified acquisition threshold, SAM.gov and exclusions, E-Verify over "
            f"{_usd(P(EV_PRIME, 'contract_value_greater_than_usd'))}, subcontracting plan over {_usd(plan)} "
            f"(other-than-small), self-performance, FHWA {_n(P(FHWA_30, 'min_self_perform_pct'))}% own forces, Buy "
            "American / BABA / FHWA Buy America, "
            "the USDOT DBE program (goals paused since "
            f"{P(DBE_GOALS, 'effective')} pending re-evaluation), prompt payment, required clauses, federal paid "
            "sick leave, and California dual coverage (pay the higher of the federal and DIR rates, two payroll "
            "filings). Also the bond amounts, dated key steps (wage determination 10-day rule, E-Verify enrollment, "
            f"SF 1413, DBE deadlines), line items, a summary and one concrete next step. Thresholds the "
            f"{P(FAR_THRESH, 'effective')} FAR inflation rule changed are picked by as_of. Set "
            "include_unverified=false to drop the unverified EO 14026 minimum-wage status item."),
        "wages": (
            "Compare a federal Davis-Bacon wage determination with the California DIR determination for ONE "
            "classification. coverage 'dual' (federal money on a job a California agency controls: 8 CCR 16001(b)) "
            "or 'federal_only' (a federal agency's own contract). Returns the straight-time cash floor (dual: the "
            "DIR basic rate, which fringe credits cannot reduce; federal only: none, any cash/fringe mix totaling "
            "the WD amount, e.g. DOL's "
            f"{_usd(P(FRINGE, 'dol_example.basic_hourly_rate_usd'))} + {_usd(P(FRINGE, 'dol_example.fringe_rate_usd'))}"
            "), the total package (the higher of the two totals), which law controls, overtime minimums "
            f"({_n(P(OT_BASE, 'overtime_multiplier'))}x the higher basic rate plus fringe at straight time; "
            f"California over {P(PW_OT, 'daily_hours_threshold')} h/day or {P(PW_OT, 'weekly_hours_threshold')} "
            "h/week), and, if you give what you pay, a pass/fail check with the per-hour raise and back pay. Errors "
            "when the DIR basic + employer payments do not add up to the total."),
        "overtime": (
            "Federal overtime exposure from a timesheet (list of {worker, date, hours, overtime_hours_paid} or CSV "
            "text). Computes per worker-week the CWHSSA overtime (over "
            f"{P(CWH_OT, 'weekly_hours_threshold')} h/week at {_n(P(CWH_OT, 'overtime_multiplier'))}x), unpaid hours, "
            f"back wages (premium on basic_rate_usd), liquidated damages ({_usd(ld)} per worker per calendar day "
            f"with unpaid overtime, the amount in effect since {P(CWH_LD, 'effective')}), whether CWHSSA applies to "
            "the contract "
            f"(over {_usd(P(CWH_OT, 'contract_value_greater_than_usd'))} assisted / over {_usd(cwh_far)} direct "
            "federal), and on California dual-coverage jobs the daily/weekly California overtime and the LC 1813 "
            f"penalty ({_usd(P(PEN_1813, 'per_worker_per_day_usd'))} per worker per day). Returns totals, line "
            "items, notes and a next step."),
        "buy_america": (
            "Domestic-content check for ONE product or material. program: baba (2 CFR 184, federally assisted "
            "infrastructure), far_buy_american (FAR 52.225-9, a federal agency's own contract) or fhwa_buy_america "
            "(23 CFR 635.410, federal-aid highway). item_type: iron_steel, manufactured_product, "
            "construction_material (lumber, drywall, glass, plastics/PVC, aluminum/copper, fiber optic, engineered "
            "wood) or excluded_material (cement, aggregates). Give the component costs (each domestic or not, iron/"
            "steel or not) or the domestic and total cost. Tests: BABA manufactured products over "
            f"{_n(P(BABA_MFG, 'domestic_component_cost_pct_greater_than'))}% U.S. components; FAR over {_n(pct)}% "
            f"(items delivered {band}) and foreign iron/steel under "
            f"{_n(P(FAR_BA, 'iron_steel_foreign_content_pct_less_than'))}%; FHWA steel all U.S.-made with a "
            f"minimal-use allowance of max({_n(P(FHWA_STEEL, 'minimal_use_pct_of_contract'))}% of contract, "
            f"{_usd(P(FHWA_STEEL, 'minimal_use_floor_usd'))}) and manufactured products by obligation date. With "
            "cost_if_domestic_usd and cost_if_foreign_usd it also runs the waiver/exception test (BABA "
            f"{_n(P(BABA_WAIVER, 'unreasonable_cost_project_increase_pct_greater_than'))}% project cost, FAR "
            f"{_n(P(FAR_BA, 'unreasonable_cost_differential_pct'))}% price, FHWA "
            f"{_n(P(FHWA_ALT, 'award_to_foreign_if_lowest_domestic_exceeds_lowest_foreign_by_pct_more_than'))}% "
            "alternate bid). Returns complies true/false/null with what is missing, the math and a next step."),
    }


Money = Annotated[float, BeforeValidator(_clean_num), Field(ge=0, allow_inf_nan=False)]
OptMoney = Optional[Annotated[float, BeforeValidator(_clean_num), Field(ge=0, allow_inf_nan=False)]]


def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    docs = _tool_docs()
    ev_days = P(EV_PRIME, "min_period_of_performance_days")
    own = _n(P(FHWA_30, "min_self_perform_pct"))
    bands = "/".join(f"{_n(v)}%" for v in P(FAR_BA, "domestic_component_pct_greater_than_by_delivery_year").values())
    premium = _n(P(CWH_OT, "overtime_multiplier") - 1)

    @mcp.tool(name="federal_project_requirements", annotations=ro, description=docs["requirements"])
    def _federal_project_requirements(
        prime_contract_value_usd: Annotated[Money, Field(description=(
            "Prime contract amount (owner to prime). Thresholds are measured on it even if you are a sub"))],
        funding_source: Annotated[str, Field(description=(
            "direct_federal, federal_aid_highway, federal_transit, federal_airport, federal_grant or none; aliases "
            "like 'FHWA', 'FTA', 'FAA AIP', 'USACE', 'GSA', 'VA', 'HUD CDBG', 'EPA SRF', 'FEMA' work"))],
        awarding_agency: Annotated[Optional[str], Field(description=(
            "Who awards the contract, e.g. 'City of San Diego', 'SD', 'Caltrans', 'LA Metro', 'USACE LA District'. "
            "Settles California dual coverage"))] = None,
        is_prime: Annotated[bool, Field(description="True if you hold the prime contract; false for a subcontractor")] = True,
        your_contract_value_usd: Annotated[OptMoney, Field(description=(
            "Subcontractors: your subcontract amount (E-Verify and SAM exclusion tests)"))] = None,
        work_type: Annotated[str, Field(description=(
            "construction, alteration, repair, painting_decorating, demolition, installation (solar, EV chargers, "
            "broadband) or material_supply (delivery only)"))] = "construction",
        davis_bacon_related_act: Annotated[Optional[bool], Field(description=(
            "federal_grant only: is the grant program a Davis-Bacon Related Act? Leave empty if unknown"))] = None,
        is_small_business: Annotated[bool, Field(description="Small business concern (subcontracting plan exemption)")] = False,
        fixed_price: Annotated[bool, Field(description="Fixed-price contract (FAR self-performance clause)")] = True,
        design_build: Annotated[bool, Field(description=(
            f"Design-build (FHWA {own}% own-forces rule does not apply)"))] = False,
        period_of_performance_days: Annotated[Optional[int], Field(ge=0, description=(
            f"Contract performance period in days (E-Verify needs {ev_days} or more)"))] = None,
        infrastructure_project: Annotated[Optional[bool], Field(description=(
            "federal_grant: is it an infrastructure project for BABA? (highway/transit/airport are)"))] = None,
        stafford_act_disaster_funds: Annotated[bool, Field(description=(
            "FEMA Stafford Act disaster/emergency money (BABA exemption)"))] = False,
        recipient_sat_usd: Annotated[OptMoney, Field(description=(
            "Assisted jobs: the agency's own simplified acquisition threshold if lower than the FAR figure"))] = None,
        bid_price_usd: Annotated[OptMoney, Field(description="Your bid, for the bid guarantee; defaults to the contract amount")] = None,
        bid_guarantee_pct: Annotated[Optional[float], Field(gt=0, le=100, description=(
            "Direct federal: bid guarantee percent stated in the solicitation"))] = None,
        bid_guarantee_cap_usd: Annotated[OptMoney, Field(description=(
            "Direct federal: bid guarantee dollar cap stated in the solicitation"))] = None,
        specialty_items_usd: Annotated[OptMoney, Field(description=(
            f"Federal-aid highway: specialty items the contract identifies (excluded from the {own}% own-forces "
            "base)"))] = None,
        bid_opening_date: Annotated[Optional[str], Field(description="ISO bid opening date (wage determination dates)")] = None,
        award_date: Annotated[Optional[str], Field(description="ISO award date (E-Verify, SF 1413 deadlines)")] = None,
        fhwa_obligation_date: Annotated[Optional[str], Field(description=(
            "Federal-aid highway: date FHWA funds were obligated (Buy America manufactured products phase)"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
        include_unverified: Annotated[bool, Field(description=(
            "Include items whose rule is unverifiable (EO 14026 status); they make verified=false"))] = True,
    ) -> FederalRequirementsResult:
        return federal_project_requirements(
            prime_contract_value_usd, funding_source, awarding_agency, is_prime, your_contract_value_usd, work_type,
            davis_bacon_related_act, is_small_business, fixed_price, design_build, period_of_performance_days,
            infrastructure_project, stafford_act_disaster_funds, recipient_sat_usd, bid_price_usd, bid_guarantee_pct,
            bid_guarantee_cap_usd, specialty_items_usd, bid_opening_date, award_date, fhwa_obligation_date, as_of,
            include_unverified)

    rate = {"gt": 0, "allow_inf_nan": False}

    @mcp.tool(name="davis_bacon_vs_ca_prevailing", annotations=ro, description=docs["wages"])
    def _davis_bacon_vs_ca_prevailing(
        federal_basic_rate_usd: Annotated[float, BeforeValidator(_clean_num), Field(**rate, description=(
            "Basic hourly rate on the federal wage determination (SAM.gov) for the classification"))],
        federal_fringe_rate_usd: Annotated[Money, Field(description="Fringe benefit rate on the federal wage determination")],
        state_basic_rate_usd: Annotated[OptMoney, Field(description="DIR determination Basic Hourly Rate (dual jobs)")] = None,
        state_employer_payments_usd: Annotated[OptMoney, Field(description=(
            "DIR determination employer payments added up (health and welfare, pension, vacation/holiday, training, "
            "other)"))] = None,
        state_total_rate_usd: Annotated[OptMoney, Field(description="DIR determination Total Hourly Rate")] = None,
        coverage: Annotated[str, Field(description="'dual' (federal money, California agency) or 'federal_only'")] = "dual",
        classification: Annotated[Optional[str], Field(description="Craft/classification, e.g. 'Laborer Group 1'")] = None,
        cash_rate_paid_usd: Annotated[OptMoney, Field(description="Straight-time cash rate you pay per hour")] = None,
        fringe_paid_usd: Annotated[OptMoney, Field(description=(
            "Creditable fringe you pay per hour (annualized plan contributions)"))] = None,
        cash_in_lieu_of_fringe_usd: Annotated[OptMoney, Field(description=(
            "Part of the cash rate paid in place of fringe benefits (excluded from the federal overtime base)"))] = None,
        straight_time_hours: Annotated[OptMoney, Field(description="Straight-time hours to price a shortfall")] = None,
        overtime_hours: Annotated[OptMoney, Field(description="Overtime hours to price a shortfall")] = None,
        overtime_cash_rate_paid_usd: Annotated[OptMoney, Field(description="Cash rate you pay per overtime hour")] = None,
        state_overtime_rate_usd: Annotated[OptMoney, Field(description=(
            "DIR determination overtime total hourly rate, if listed"))] = None,
    ) -> WageComparisonResult:
        return davis_bacon_vs_ca_prevailing(
            federal_basic_rate_usd, federal_fringe_rate_usd, state_basic_rate_usd, state_employer_payments_usd,
            state_total_rate_usd, coverage, classification, cash_rate_paid_usd, fringe_paid_usd,
            cash_in_lieu_of_fringe_usd, straight_time_hours, overtime_hours, overtime_cash_rate_paid_usd,
            state_overtime_rate_usd)

    @mcp.tool(name="federal_overtime_exposure", annotations=ro, description=docs["overtime"])
    def _federal_overtime_exposure(
        timesheet: Annotated[Union[list[TimeEntry], str], Field(description=(
            "Daily hours: [{worker, date, hours, overtime_hours_paid}] or CSV text with a header such as "
            "worker,date,hours,overtime_hours_paid"))],
        basic_rate_usd: Annotated[OptMoney, Field(description=(
            "Basic cash hourly rate (at least the wage determination basic rate) to price back wages"))] = None,
        prime_contract_value_usd: Annotated[OptMoney, Field(description="Prime contract amount (CWHSSA threshold)")] = None,
        funding_source: Annotated[Optional[str], Field(description=(
            "direct_federal, federal_aid_highway, federal_transit, federal_airport, federal_grant (aliases ok)"))] = None,
        awarding_agency: Annotated[Optional[str], Field(description=(
            "Awarding agency; a California agency on assisted work turns on the LC 1813 check"))] = None,
        california_public_works: Annotated[Optional[bool], Field(description=(
            "Force the California dual-coverage overtime check on or off"))] = None,
        straight_time_paid_for_all_hours: Annotated[bool, Field(description=(
            f"True if every hour was paid at least straight time (back wages = the {premium}x premium only)"))] = True,
        workweek_starts_on: Annotated[str, Field(description="First day of your fixed payroll workweek")] = "monday",
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> OvertimeExposureResult:
        return federal_overtime_exposure(timesheet, basic_rate_usd, prime_contract_value_usd, funding_source,
                                         awarding_agency, california_public_works, straight_time_paid_for_all_hours,
                                         workweek_starts_on, as_of)

    @mcp.tool(name="buy_america_check", annotations=ro, description=docs["buy_america"])
    def _buy_america_check(
        program: Annotated[str, Field(description=(
            "baba, far_buy_american or fhwa_buy_america (aliases: BABA, FAR, Buy American, FHWA, Caltrans federal-aid)"))],
        item_type: Annotated[str, Field(description=(
            "iron_steel, manufactured_product, construction_material or excluded_material (or a material name: "
            "lumber, drywall, glass, PVC, aluminum, cement, aggregate)"))],
        item_name: Annotated[Optional[str], Field(description="What it is, e.g. 'pump station control panel'")] = None,
        components: Annotated[Optional[list[Component]], Field(description=(
            "Component costs: [{name, cost_usd, domestic, iron_steel}]"))] = None,
        domestic_component_cost_usd: Annotated[OptMoney, Field(description="Cost of U.S. components (instead of components)")] = None,
        total_component_cost_usd: Annotated[OptMoney, Field(description="Cost of all components")] = None,
        foreign_iron_steel_component_cost_usd: Annotated[OptMoney, Field(description=(
            "FAR iron/steel items: cost of foreign iron and steel in the item"))] = None,
        manufactured_in_us: Annotated[Optional[bool], Field(description="Final manufacture in the U.S.?")] = None,
        all_manufacturing_in_us: Annotated[Optional[bool], Field(description=(
            "Iron/steel and construction materials: every manufacturing process in the U.S.?"))] = None,
        delivery_date: Annotated[Optional[str], Field(description=(
            f"FAR: ISO delivery date (sets the {bands} band)"))] = None,
        cots: Annotated[bool, Field(description="FAR: commercially available off-the-shelf item")] = False,
        critical_item: Annotated[bool, Field(description="FAR: critical item / contains critical components")] = False,
        total_contract_cost_usd: Annotated[OptMoney, Field(description="FHWA: total contract cost (minimal-use allowance)")] = None,
        contract_foreign_iron_steel_cost_usd: Annotated[OptMoney, Field(description=(
            "FHWA: cumulative delivered cost of foreign steel/iron used on the contract"))] = None,
        fhwa_obligation_date: Annotated[Optional[str], Field(description="FHWA: date federal funds were obligated")] = None,
        cost_if_domestic_usd: Annotated[OptMoney, Field(description=(
            "Waiver/exception test: BABA overall project cost with U.S. items; FAR domestic material price; FHWA "
            "lowest domestic-steel total bid"))] = None,
        cost_if_foreign_usd: Annotated[OptMoney, Field(description="The same figure with foreign items")] = None,
        stafford_act_disaster_funds: Annotated[bool, Field(description="BABA: FEMA Stafford Act money (exempt)")] = False,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> DomesticContentResult:
        return buy_america_check(program, item_type, item_name, components, domestic_component_cost_usd,
                                 total_component_cost_usd, foreign_iron_steel_component_cost_usd, manufactured_in_us,
                                 all_manufacturing_in_us, delivery_date, cots, critical_item, total_contract_cost_usd,
                                 contract_foreign_iron_steel_cost_usd, fhwa_obligation_date, cost_if_domestic_usd,
                                 cost_if_foreign_usd, stafford_act_disaster_funds, as_of)
