"""Federal and federally funded construction in California, on top of the verified pack
research/web_federal-works.verified.json (rule ids "fed-...").

  federal_project_requirements   prime contract value + funding source (+ awarding agency, your role, dates) -> which
                                 federal rules apply and which do not at this value (the threshold math for each):
                                 Davis-Bacon, certified payroll, CWHSSA overtime, Miller Act or 2 CFR 200.326 bonds with
                                 dollar amounts, SAM / E-Verify, subcontracting plan and self-performance, Buy
                                 American / BABA / FHWA and FTA Buy America, DBE (with the 2025-2026 pause), prompt
                                 payment, required clauses, California dual coverage, dated key steps and one concrete
                                 next step
  davis_bacon_vs_ca_prevailing   the SAM.gov wage determination and the DIR determination for one classification
                                 (+ what you pay) -> the cash floor and total package to pay, overtime minimums and the
                                 shortfall
  federal_overtime_exposure      a timesheet -> CWHSSA overtime owed and liquidated damages per worker per day, plus the
                                 California Labor Code 1813 penalty on dual-coverage jobs
  buy_america_check              one product or material -> does it meet BABA (2 CFR 184), FAR Buy American (52.225-9),
                                 FHWA Buy America (23 CFR 635.410) or FTA Buy America (49 CFR 661.5), with the
                                 domestic-content math and the waiver / exception test (FAA airport terms are not in
                                 the rulebook, so FAA is refused rather than tested as BABA)
  cmmc_requirements              DoD or civilian contract + what information you will hold (CUI, FCI only, none) + award
                                 date + your subcontract tier (+ the solicitation's CMMC level, your SPRS score and
                                 current CMMC status) -> the CMMC phase on the award date, the level you must hold (32
                                 CFR 170.23 for subcontractors), the assessment, POA&M and affirmation rules, SPRS / CMMC
                                 UID steps, DFARS 252.204-7012/-7019/-7020 (NIST SP 800-171 Rev 2, 72-hour DIBNet
                                 reporting), FAR 52.204-21, Section 889, Kaspersky, TikTok and SAM.gov items, renewal
                                 dates and one next step (rules "cmmc-..." from research/web_cmmc-federal-cyber)

Every legal number, date and threshold is read from the rulebook with core.rule(id).param(key) at call time, and every
rule a result relies on is cited through core.Result.cite(), so `verified` is True only when all of them are verified.
Thresholds that the October 1, 2025 FAR inflation rule changed (CWHSSA clause, simplified acquisition threshold,
subcontracting plan, self-performance clause, anti-lobbying certification) are picked by the as_of date from the
current and prior values the rulebook carries. The one unverifiable pack rule (EO 14026 minimum wage status) is shown
only when include_unverified is true and then makes the result verified=False.

State agencies (Caltrans, DGS, DWR, ...) procure under their own rules (2 CFR 200.317), so the 2 CFR 200.326 bond
minimums are applied only to local agencies' contracts.

California rules from other packs used here: 8 CCR 16001(b) dual coverage (in this pack), the LC 1815 8/40 overtime
rule, the LC 1813 penalty, LC 1771.4 eCPR uploads, and B&P 7108.5 prompt payment (PCC 10262 instead when the prime
contract is a California state agency's, e.g. Caltrans).

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
FTA_BA = "fed-fta-buy-america-all-us-components"
# 2 CFR 200.317: a State agency procures under its own rules (no 2 CFR 200.326 bond minimums)
STATE_PROC = "fed-state-procurement-own-policies-200-317"
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
PCC_10262 = "state-contract-pay-subs-7-days"

RULE_IDS = (DBA, DBA_PAY, DBA_POST, DBA_CONFORM, DBA_OPLAW, DBA_RECORDS, DBA_DOCS, DBA_CPR, DBA_WH347, DBA_SOC,
            DBA_APPR, DBA_APPR_RATIO, DBA_FLOW, SF1413, DBA_ELIG, DBA_DEBAR, DBA_MATERIAL, DBA_DEMO, DBA_WORK,
            DBA_FOREPERSON, FRINGE, FRINGE_QTR, FRINGE_ANNUAL, FRINGE_NO_CREDIT, OT_BASE, WD_BID, WD_SAM, WD_TYPES,
            COPE_7, COPE_2000, CWH_OT, CWH_LD, CWH_FAR, CWH_FLOW, CWH_SAFETY, MILLER, MILLER_ALT, BONDS_NTP, BID_GUAR,
            BOND_WAIVER, BOND_COPY, SURETY, ASSIST_BOND, FAR_THRESH, NOT_ADJUSTED, SUBK_PLAN, SELF_PERF, EV_PRIME,
            EV_ENROLL, EV_SUB, SAM_REG, SAM_ANNUAL, UEI, SAM_EXCL, PP_SUBS, PP_INTEREST, PP_RETAINAGE, GOV_RET, BABA,
            BABA_IRON, BABA_MFG, BABA_CM, BABA_EXCL, BABA_WAIVER, BABA_STAFFORD, DOM_PREF, FAR_BA, TELECOM, FTA_BA,
            STATE_PROC, FHWA_STEEL,
            FHWA_MFG, FHWA_ALT, FHWA_30, FHWA_SUBS, FHWA_1273, DBE_PAY, DBE_RET, DBE_ASSURE, DBE_GOALS, DBE_REEVAL,
            DBE_GFE, DBE_TERM, DBE_COUNT, DBE_BIDDERS, APPX_II, BYRD, EO14026, EO13706, CA_HIGHER, CA_BASIC, CA_REPORT,
            ECPR, BP7108, PW_OT, PEN_1813, PCC_10262)

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
MAX_INPUT = 1e12            # sanity limit on any dollar, rate or hour input (a typo guard, not a legal number)

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


def _pct(part: float, whole: float) -> float:
    """part / whole as a percent, rounded to 9 places so binary float noise cannot flip a threshold test:
    5,500 / 10,000 is exactly 55%, not 55.00000000000001% (which would pass a "more than 55%" test)."""
    return round(part / whole * 100, 9)


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
    if x > MAX_INPUT:
        raise ValueError(f"{field} = {value!r} is not a realistic figure (over {MAX_INPUT:,.0f}); check the amount")
    return x


_TRUE, _FALSE = ("true", "t", "yes", "y", "1"), ("false", "f", "no", "n", "0")


def _flag(value, field: str, *, allow_none: bool = False) -> Optional[bool]:
    """True/False from a bool or the words a form sends ('false', 'No', '0'); None stays None when allowed.
    bool('false') is True in Python, so a string is never passed through bool()."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if allow_none:
            return None
        raise ValueError(f"{field} must be true or false")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    t = _words(value)
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ValueError(f"{field} must be true or false, got {value!r}")


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
# Bare "army" and "federal" are not on the list: 'Salvation Army' and 'Federal Express' are not federal agencies.
_FEDERAL_AGENCY_WORDS = ("usace", "army corps", "corps of engineers", "gsa", "general services administration",
                         "veterans affairs", "va", "navy", "navfac", "national park service", "nps", "air force",
                         "usaf", "us army", "u s army", "department of the army", "army reserve", "marine corps",
                         "usmc", "department of defense", "dod", "bureau of reclamation", "usbr", "forest service",
                         "usfs", "homeland security", "dhs", "cbp", "coast guard", "uscg", "department of energy",
                         "united states", "naval", "department of the interior", "bureau of land management", "blm",
                         "nasa", "federal government", "federal aviation administration",
                         "federal highway administration", "federal transit administration", "federal lands",
                         "federal emergency management agency", "federal bureau of prisons", "bureau of prisons",
                         "federal agency")
_STRONG_FEDERAL_WORDS = ("usace", "army corps", "corps of engineers", "gsa", "general services administration",
                         "navfac", "naval", "united states", "us army", "us navy", "u s army", "u s navy",
                         "federal government", "federal agency")
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
# California state agencies, whose contracts fall under the State Contract Act (PCC 10262 prompt payment). UC and
# CSU are left out: they contract under their own Public Contract Code parts.
_CA_STATE_AGENCY_WORDS = ("caltrans", "california department of transportation", "state of california", "state of ca",
                          "dgs", "department of general services", "dwr", "department of water resources",
                          "california department", "calvet", "cal fire")
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
    if classify_agency(value)[0] == "california_public_agency":
        raise ValueError(f"funding_source {value!r} names an agency, not where the money comes from. Put the agency "
                         "in awarding_agency and give the money: federal_aid_highway (FHWA), federal_transit (FTA), "
                         "federal_airport (FAA), federal_grant (HUD, EPA, FEMA, ...) or none (state or local money "
                         "only).")
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
    federal = _has(t, *_FEDERAL_AGENCY_WORDS) or t.startswith(("us ", "u s "))
    # 'California Department of Veterans Affairs' (CalVet) is a state agency even though 'veterans affairs' is a
    # federal name; only an unmistakably federal word ('United States', 'USACE', 'GSA', ...) beats 'California'.
    if federal and _has(t, "california", "calvet", "caltrans", "state of ca") and not (
            _has(t, *_STRONG_FEDERAL_WORDS) or t.startswith(("us ", "u s "))):
        federal = False
    if federal:
        return "federal_agency", raw
    bare = re.sub(r"^(?:city|town) of ", "", t)
    bare = re.sub(r" city$", "", bare) if bare not in ("national city",) else bare
    if bare in _CA_CITIES:
        return "california_public_agency", f"City of {_CA_CITIES[bare]}"
    if _has(t, *_CA_PUBLIC_WORDS):
        return "california_public_agency", raw
    return "other", raw


def _is_state_agency(name) -> bool:
    """'Caltrans District 11', 'State of California DGS' -> True; 'City of San Diego', 'UC San Diego' -> False."""
    t = _words(name)
    return bool(t) and classify_agency(name)[0] == "california_public_agency" and _has(t, *_CA_STATE_AGENCY_WORDS)


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
                                  "Subcontracting, Domestic content, DBE, Payments, Contract clauses, Leave; CMMC tool: "
                                  "CMMC, Safeguarding, Incident reporting, Subcontracts, Supply chain, SAM.gov")
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
    elif j.fund == "federal_grant" and j.dbra is False:
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
            ("Each week, submit a certified payroll (Optional Form WH-347 or any format with the same data; "
             f"identify workers by an {P(DBA_WH347, 'worker_identifier')}, never the full SSN or home address) "
             "signed with the Statement of "
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
        if applies and j.fund == "federal_grant" and j.dbra is not True and P(CWH_OT, "clause_inserted_in_addition_"
                                                                                     "to_dba_clauses"):
            # 29 CFR 5.5(b): the CWHSSA clauses go into assisted contracts "subject to the overtime provisions" of
            # CWHSSA, "in addition to" the Davis-Bacon clauses, so a grant's coverage follows its Davis-Bacon status.
            applies = None
            why += (" On grant money the CWHSSA clause goes in alongside the Davis-Bacon clauses (29 CFR 5.5(b)), so "
                    + ("confirm with the agency whether this contract carries it even though the program is not a "
                       "Davis-Bacon Related Act." if j.dbra is False else
                       "it follows the Davis-Bacon answer above; plan as covered until the agency says otherwise."))
        ids = [CWH_OT, CWH_LD, CWH_FLOW, OT_BASE] + ([CWH_SAFETY] if applies is not False else [])
    what = ""
    if applies is not False:
        what = (f"Pay {_n(mult)}x the basic rate (never below the wage determination basic rate; fringes stay at "
                f"straight time) for every hour over {wk} in a workweek, for every laborer and mechanic including "
                f"guards and watchpersons. A violation costs the unpaid overtime plus {_usd(ld)} per worker per "
                "calendar day in liquidated damages. Flow the clause down to every subcontract. Use "
                "federal_overtime_exposure to check a timesheet.")
        if j.fund != "direct_federal":
            what += (" No laborer or mechanic may work in unsanitary, hazardous or dangerous conditions "
                     f"({P(CWH_SAFETY, 'statute')}).")
    ctx.req("Overtime", f"CWHSSA weekly overtime (over {wk} hours)", applies, why, what,
            ids if applies is not False else ids[:1])


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
    # federally assisted: 2 CFR 200.326 federal minimum, over the simplified acquisition threshold, except on a
    # State agency's own contract: 2 CFR 200.317 has the State follow its own procurement policies.
    if _is_state_agency(j.agency) and P(STATE_PROC, "state_follows_own_procurement_policies"):
        if j.is_prime:
            ctx.req("Bonds", "Federal minimum bonding for assisted construction", False,
                    (f"{j.agency} is a California state agency: under 2 CFR 200.317 a State procures under its own "
                     f"policies, so the 2 CFR 200.326 federal minimum ({_n(P(ASSIST_BOND, 'bid_guarantee_pct'))}% "
                     f"bid guarantee, {_n(P(ASSIST_BOND, 'performance_bond_pct'))}% performance and "
                     f"{_n(P(ASSIST_BOND, 'payment_bond_pct'))}% payment bonds) does not govern this contract."),
                    "The bid documents and California state contract law set the bid security and bonds; follow "
                    "them.", [STATE_PROC, ASSIST_BOND])
        else:
            ctx.use(STATE_PROC)
        return BondPlan(regime="none", notes=["State agency contract: bonds follow the bid documents and California "
                                              "law, not 2 CFR 200.326 (2 CFR 200.317)."])
    sat, sat_src = j.sat_eff, j.sat_src
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
            if v <= thr or (j.pop is not None and j.pop < days_min):
                # the prime contract itself is outside FAR 22.1803, so there is no clause to flow down
                applies = False
                why = ("The prime contract does not carry FAR 52.222-54 ("
                       + (f"{_usd(v)} is not over {_usd(thr)}" if v <= thr else
                          f"performance period {j.pop} days is under {days_min} days")
                       + "), so there is nothing to flow down to you.")
            else:
                applies = None if mine is None else mine > sub_thr
                why = (f"The prime contract ({_usd(v)} > {_usd(thr)}) carries FAR 52.222-54; it reaches your "
                       f"subcontract when that is over {_usd(sub_thr)}." if mine is None else
                       f"Prime contract {_usd(v)} > {_usd(thr)}; your subcontract {_usd(mine)} "
                       f"{'>' if applies else 'is not over'} {_usd(sub_thr)}.")
            ctx.req("Hiring", "E-Verify flow-down", applies, why,
                    (f"Enroll within {enroll} days after your subcontract award and verify new hires within "
                     f"{hire_days} business days.") if applies is not False else "",
                    [EV_PRIME, EV_SUB, EV_ENROLL] if applies is not False else [EV_PRIME, EV_SUB])
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
            why = (f"Fixed-price construction {_usd(v)} {'>' if applies else 'is not over'} {_usd(thr)} ({hist}); "
                   f"the clause is not used on contracts under {P(SELF_PERF, 'excluded_programs')}.")
            what = ("Not required at this value, but FAR 36.501(b) lets the contracting officer include it anyway: "
                    "if the contract has FAR 52.236-1, perform at least the percentage it states with your own forces."
                    if not applies and P(SELF_PERF, "may_insert_at_or_below_threshold", False) else "")
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
        fe = P(FAR_BA, "iron_steel_foreign_content_pct_less_than")
        bands = "; ".join(f"over {_n(pct)}% for items delivered {_band_text(key)}"
                          for key, pct in P(FAR_BA, "domestic_component_pct_greater_than_by_delivery_year").items())
        ctx.req("Domestic content", "Buy American - construction materials (FAR 52.225-9)", True,
                "Direct federal construction contract.",
                (f"Use only domestic construction material: manufactured material needs U.S. components {bands} "
                 f"(by the year each item is delivered); iron or steel material needs foreign iron and steel under "
                 f"{_n(fe)}% of component cost. Ask before award for exceptions (domestic more than "
                 f"{_n(P(FAR_BA, 'unreasonable_cost_differential_pct'))}% costlier, or unavailable). If the contract "
                 "has the Trade Agreements version instead (FAR 52.225-11), designated-country construction "
                 "material is also allowed; check which clause it has. Check items with buy_america_check."),
                [FAR_BA])
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
    elif j.fund == "federal_transit":
        cost_pct = P(FTA_BA, "price_differential_cost_increase_pct_more_than")
        ctx.req("Domestic content", "FTA Buy America (49 CFR 661.5)", True, "FTA-assisted project.",
                ("Iron, steel and manufactured products used in the project must be produced in the U.S. Steel and "
                 f"iron construction materials: {P(FTA_BA, 'steel_iron_standard')}. A manufactured product: all "
                 "manufacturing in the U.S. AND all of its components of U.S. origin (a component counts if it is "
                 "manufactured in the U.S., wherever its subcomponents come from), which is stricter than the "
                 f"BABA {_n(P(BABA_MFG, 'domestic_component_cost_pct_greater_than'))}% test: a product that only "
                 f"meets BABA does not meet FTA. Rolling stock has its own rule ({P(FTA_BA, 'rolling_stock')}, not "
                 "in the rulebook). Only FTA can waive it (public interest, non-availability, or the domestic item raising "
                 f"the cost by more than {_n(cost_pct)}%). For construction materials (lumber, glass, drywall, "
                 "plastics, non-ferrous metals) follow the contract's Buy America provisions. Check items with "
                 "buy_america_check program fta_buy_america."), [FTA_BA, BABA_MFG])
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
            if j.fund == "federal_airport" and infra is not False:
                # 2 CFR 184.2(a) (rule notes): part 184 yields to an agency's pre-November 15, 2021 Buy America
                # preference that meets or exceeds BABA; FAA's own terms are not in the rulebook.
                infra = None
                why = ("FAA airport grant: FAA's own Buy American terms, which are not in the rulebook, may replace or "
                       "add to BABA (2 CFR 184.2(a) leaves an agency's pre-November 15, 2021 Buy America preference "
                       "in place of BABA where it meets or exceeds it). The BABA tests below are for reference; "
                       "the contract's Buy American provisions control.")
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
                if j.fund == "federal_airport":
                    what += (" Get FAA's Buy American requirements for this project from the airport sponsor before "
                             "you buy; a product that meets BABA may not meet them.")
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
    # B&P 7108.5 covers public works "except where Section 10262 of the Public Contract Code applies": a prime
    # contractor on a state agency's contract (Caltrans, DGS, DWR, ...) pays its subs under PCC 10262 instead.
    if j.is_prime and _is_state_agency(j.agency):
        days, state_id = P(PCC_10262, "deadline_days_after_progress_payment"), PCC_10262
        law, ca_terms = "California PCC 10262 (state agency contract)", (
            f"Pay each sub its share within {days} days after you receive each progress payment (PCC 10262, which "
            "displaces B&P 7108.5 on state agency contracts); an underpaid sub notifies the "
            + " and the ".join(P(PCC_10262, "sub_must_notify_of_underpayment")) + " in writing")
    else:
        days, state_id = P(BP7108, "deadline_days"), BP7108
        law, ca_terms = "California B&P 7108.5", (
            f"Pay each sub its share within {days} days after you receive each progress payment (B&P 7108.5, "
            "unless otherwise agreed in writing)")
    if j.fund in USDOT:
        ctx.req("Payments", f"Pay subcontractors within {days} days of being paid", True,
                f"{law} and the federal DBE prompt-payment rule (all subs, all tiers); the state deadline is the "
                "stricter one.",
                (f"{ca_terms}; the federal outer limit is "
                 f"{P(DBE_PAY, 'days_after_receipt_of_payment')} days. Return a sub's retainage within "
                 f"{P(DBE_RET, 'retainage_return_days_after_sub_work_completed')} days after its work is "
                 "satisfactorily completed (or under the agency's chosen retainage method); California retention "
                 "rules also apply (see public_works_readiness)."), [state_id, DBE_PAY, DBE_RET])
    else:
        ctx.req("Payments", f"Pay subcontractors within {days} days of being paid", True, f"{law}.",
                f"{ca_terms}.", [state_id])


def _sec_clauses(ctx: _Ctx, j: _Job, include_unverified: bool, dba: Optional[bool]) -> None:
    v = j.value
    if j.fund in ASSISTED:
        t = dict(P(APPX_II, "clauses_by_threshold_usd"))
        # Appendix II(A) defines its threshold as "the inflation adjusted amount determined by the [FAR] Councils",
        # i.e. the FAR SAT in effect on as_of; an agency's own lower SAT (which drives 2 CFR 200.326 bonding) does
        # not move it.
        far_sat, far_hist = _on(ctx.today, FAR_THRESH, "simplified_acquisition_threshold_usd",
                                "simplified_acquisition_threshold_prior_usd", "effective")
        t["breach_remedies_over"] = far_sat
        names = {"termination_for_cause_and_convenience_over": "termination for cause and convenience",
                 "breach_remedies_over": "remedies for breach",
                 "clean_air_and_clean_water_over": "Clean Air Act and Clean Water Act compliance",
                 "byrd_anti_lobbying_certification_over": "anti-lobbying certification (each tier to the tier above)"}
        has = [f"{names[k]} (over {_usd(t[k])})" for k in names if v > t[k]]
        not_yet = [f"{names[k]} (over {_usd(t[k])})" for k in names if v <= t[k]]
        why = (f"Prime contract {_usd(v)}; remedies for breach are measured against the FAR simplified acquisition "
               f"threshold ({far_hist}), which Appendix II(A) names, even if the agency uses a lower one for "
               "bonding.")
        lobby = t["byrd_anti_lobbying_certification_over"]
        if not j.is_prime:
            why += (f" The anti-lobbying certification goes tier by tier on each bid or award over {_usd(lobby)}: "
                    + ("give your subcontract amount to settle yours." if j.mine is None else
                       f"your {_usd(j.mine)} subcontract {'is over' if j.mine > lobby else 'is not over'} "
                       f"{_usd(lobby)}, so you {'must' if j.mine > lobby else 'need not'} certify to the prime."))
        ctx.req("Contract clauses", "Required federal grant clauses (2 CFR 200 Appendix II)", True, why,
                (("Your contract" if j.is_prime else "The prime contract") + " must carry: "
                 + ("; ".join(has) if has else "none of the dollar-threshold clauses")
                 + ". Not required at this value: " + ("; ".join(not_yet) if not_yet else "none") +
                 ". Equal opportunity, debarment, recovered materials, telecom and domestic-preference clauses apply "
                 "without a dollar threshold (with the limits in their own rules)."), [APPX_II, BYRD, FAR_THRESH])
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
                     "writing each pay period (or each month, if shorter). California paid sick leave is a separate "
                     "state law; check whether it also reaches this site. Leave that a law requires cannot be "
                     "credited toward Davis-Bacon fringes."),
                    [EO13706, FRINGE_NO_CREDIT])
        if include_unverified:
            ctx.req("Wages", "Federal contractor minimum wage (EO 14026 / EO 13658)", None,
                    ("UNVERIFIED: reported revoked on "
                     f"{P(EO14026, 'revocation_date')} by {P(EO14026, 'eo_14026_revoked_by')} and not enforced by DOL, "
                     "but 29 CFR part 23 and FAR 52.222-55 were still printed in the CFR on "
                     f"{P(EO14026, 'cfr_29_part_23_still_codified_as_of')}. The primary text could not be read."),
                    ("No federal contractor minimum wage is computed. If your contract includes FAR 52.222-55, ask "
                     "the contracting officer in writing whether it is enforced; the Davis-Bacon wage determination "
                     "rates still apply."), [EO14026])
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


def _assisted_sat(today: date, recipient_sat: Optional[float]) -> tuple[float, str]:
    """The simplified acquisition threshold an assisted contract is measured against (2 CFR 200.1): the agency's
    own figure if it set one, never above the FAR 2.101 figure in effect on `today` -> (threshold, where it came
    from)."""
    far_sat, hist = _on(today, FAR_THRESH, "simplified_acquisition_threshold_usd",
                        "simplified_acquisition_threshold_prior_usd", "effective")
    if recipient_sat is not None and recipient_sat > far_sat:
        raise ValueError(f"recipient_sat_usd ({_usd(recipient_sat)}) cannot exceed the FAR simplified acquisition "
                         f"threshold ({_usd(far_sat)} on {today}): 2 CFR 200.1 caps an agency's own threshold at the "
                         "FAR figure")
    if recipient_sat is not None:
        return recipient_sat, f"the agency's own simplified acquisition threshold (at most the FAR {_usd(far_sat)})"
    return far_sat, f"the FAR simplified acquisition threshold ({hist}); the agency may set a lower one"


def _band_text(key) -> str:
    """A delivery-year band key of the FAR 52.225-9 rule: '2024-2028' -> 'in 2024-2028', '2029+' -> 'from 2029'."""
    m = re.fullmatch(r"(\d{4})(?:-(\d{4})|\+)", str(key).strip())
    if not m:
        raise KeyError(f"rule {FAR_BA}: cannot read delivery-year band {key!r}")
    return f"in {m.group(1)}-{m.group(2)}" if m.group(2) else f"from {m.group(1)}"


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
    is_prime, include_unverified = _flag(is_prime, "is_prime"), _flag(include_unverified, "include_unverified")
    is_small_business, fixed_price = _flag(is_small_business, "is_small_business"), _flag(fixed_price, "fixed_price")
    design_build = _flag(design_build, "design_build")
    stafford_act_disaster_funds = _flag(stafford_act_disaster_funds, "stafford_act_disaster_funds")
    davis_bacon_related_act = _flag(davis_bacon_related_act, "davis_bacon_related_act", allow_none=True)
    infrastructure_project = _flag(infrastructure_project, "infrastructure_project", allow_none=True)
    value = _number(prime_contract_value_usd, "prime_contract_value_usd", positive=True)
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
    j.sat_eff, j.sat_src = _assisted_sat(today, j.sat) if fund in ASSISTED else (None, None)
    ctx = _Ctx(today)
    if j.sat is not None and fund not in ASSISTED:
        ctx.notes.append("recipient_sat_usd was ignored: it only applies to federally assisted contracts let by a "
                         "state or local agency.")
    elif j.sat is not None and _is_state_agency(agency):
        ctx.notes.append("recipient_sat_usd was ignored for bonding: a California state agency follows its own "
                         "procurement rules (2 CFR 200.317), and Appendix II(A) uses the FAR figure.")
    if j.stafford and fund in ("direct_federal", "federal_aid_highway", "federal_transit"):
        ctx.notes.append("stafford_act_disaster_funds was ignored: it is a BABA (2 CFR part 184) exemption, and this "
                         "funding source has its own domestic-content rule.")
        j.stafford = False
    if j.infra is not None and fund in ("direct_federal", "federal_aid_highway", "federal_transit"):
        ctx.notes.append("infrastructure_project was ignored: it only decides BABA on grant and airport money; this "
                         "funding source has its own domestic-content rule.")
    role = "prime" if j.is_prime else "subcontractor"
    if davis_bacon_related_act is not None and fund not in ("federal_grant", "none"):
        ctx.notes.append(
            "davis_bacon_related_act was ignored: it only settles coverage for federal_grant money. "
            + ("A federal agency's own construction contract is covered by the Davis-Bacon Act itself."
               if fund == "direct_federal" else
               "Federal-aid highway, transit and airport money is covered as Davis-Bacon Related Act money."))
        j.dbra = None

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
    """The CWHSSA liquidated damages amount in effect on `today` (29 CFR 5.8: it changes each January 15)."""
    eff = _iso_param(CWH_LD, "effective")
    if today >= eff:
        return P(CWH_LD, "liquidated_damages_usd_per_worker_per_day")
    hist = sorted((date.fromisoformat(k), amt) for k, amt in P(CWH_LD, "history_by_effective_date_usd").items())
    past = [(d, amt) for d, amt in hist if d <= today]
    if past:
        d, amt = past[-1]
        ctx.notes.append(f"as_of {today} is before {eff}: priced at the {_usd(amt)} amount in effect from {d}.")
        return amt
    raise ValueError(f"as_of {today} is before the earliest CWHSSA liquidated damages amount in the rulebook (in "
                     f"effect from {hist[0][0]})")


def federal_overtime_exposure(timesheet, basic_rate_usd=None, prime_contract_value_usd=None, funding_source=None,
                              awarding_agency=None, california_public_works=None, straight_time_paid_for_all_hours=True,
                              workweek_starts_on="monday", as_of=None) -> OvertimeExposureResult:
    """CWHSSA overtime owed and liquidated damages from a timesheet, plus LC 1813 on California dual-coverage jobs."""
    today = _today(as_of)
    california_public_works = _flag(california_public_works, "california_public_works", allow_none=True)
    straight_time_paid_for_all_hours = _flag(straight_time_paid_for_all_hours, "straight_time_paid_for_all_hours")
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
        if applies and fund == "federal_grant" and P(CWH_OT, "clause_inserted_in_addition_to_dba_clauses"):
            applies = None
            reason += (" On grant money the CWHSSA clause goes in alongside the Davis-Bacon clauses (29 CFR 5.5(b)); "
                       "computed as if it applies, so confirm the program is a Davis-Bacon Related Act.")
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
    notes.append(f"Hours already paid at {_n(mult)}x or more (for example California daily overtime) are counted "
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
Program = Literal["baba", "far_buy_american", "fhwa_buy_america", "fta_buy_america"]
PROGRAM_LABELS = {"baba": "Build America, Buy America (2 CFR part 184; federally assisted infrastructure)",
                  "far_buy_american": "FAR Buy American - construction materials (52.225-9; direct federal)",
                  "fhwa_buy_america": "FHWA Buy America (23 CFR 635.410; federal-aid highway)",
                  "fta_buy_america": "FTA Buy America (49 CFR 661.5; FTA-assisted transit)"}
_PROGRAM_ALIASES = {
    "baba": ("baba", "build america buy america", "build america", "bil", "iija", "2 cfr 184", "grant",
             "federal grant", "federal_grant", "epa", "srf", "hud", "cdbg", "federally assisted"),
    "fta_buy_america": ("fta", "fta buy america", "federal transit", "federal_transit", "transit", "49 cfr 661",
                        "49 cfr 661 5", "federal transit administration"),
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
    if t in ("faa", "aip", "faa aip", "federal airport", "airport", "airport improvement program",
             "federal aviation administration"):
        raise ValueError("FAA airport grant projects carry FAA's own Buy American terms, which are not in the "
                         "rulebook (2 CFR 184.2(a) leaves an agency's pre-November 15, 2021 Buy America preference in "
                         "place of BABA where it meets or exceeds it), so this tool cannot say whether an item "
                         "complies. Get the project's Buy American requirements from the airport sponsor; use "
                         "program='baba' only to see the BABA test.")
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
    cost_usd: Annotated[float, BeforeValidator(_clean_num)] = Field(ge=0, le=MAX_INPUT, allow_inf_nan=False)
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
    manufactured_in_us = _flag(manufactured_in_us, "manufactured_in_us", allow_none=True)
    all_manufacturing_in_us = _flag(all_manufacturing_in_us, "all_manufacturing_in_us", allow_none=True)
    cots, critical_item = _flag(cots, "cots"), _flag(critical_item, "critical_item")
    stafford_act_disaster_funds = _flag(stafford_act_disaster_funds, "stafford_act_disaster_funds")
    if components is not None and not isinstance(components, list):
        raise ValueError("components must be a list of {name, cost_usd, domestic, iron_steel}")
    comps = []
    for i, c in enumerate(components or [], start=1):
        try:
            comps.append(c if isinstance(c, Component) else Component.model_validate(c))
        except Exception as e:
            raise ValueError(f"component {i}: {_why(e)}") from None
    dom = _number(domestic_component_cost_usd, "domestic_component_cost_usd", allow_none=True)
    tot = _number(total_component_cost_usd, "total_component_cost_usd", allow_none=True)
    fis = _number(foreign_iron_steel_component_cost_usd, "foreign_iron_steel_component_cost_usd", allow_none=True)
    if comps and (dom is not None or tot is not None or fis is not None):
        raise ValueError("give either components or the domestic/total/foreign iron-steel cost figures, not both")
    if comps:
        tot = sum(c.cost_usd for c in comps)
        dom = sum(c.cost_usd for c in comps if c.domestic)
        fis = sum(c.cost_usd for c in comps if c.iron_steel and not c.domestic)
        iron_share = _pct(sum(c.cost_usd for c in comps if c.iron_steel), tot) if tot else None
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
    dom_pct = None if dom is None or tot is None else _pct(dom, tot)
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
                inc = _pct(c_dom - c_for, c_for)
                exception = inc > cut
                exc_test = (f"Overall project cost with U.S. items {_usd(c_dom)} vs {_usd(c_for)}: +{round(inc, 2):g}% "
                            f"{'>' if exception else '<='} {_n(cut)}% -> "
                            + ("an unreasonable-cost waiver can be requested (by the awarding agency, in writing; "
                               f"posted at least {P(BABA_WAIVER, 'public_comment_days_min')} days for comment)"
                               if exception else "no unreasonable-cost waiver"))
                ctx.line("Project cost increase from U.S. items", inc, "percent", exc_test, BABA_WAIVER)
    elif prog == "fta_buy_america":
        ctx.use(FTA_BA)
        if kind == "iron_steel":
            test = f"Steel and iron: {P(FTA_BA, 'steel_iron_standard')}."
            complies = need(all_manufacturing_in_us, "all_manufacturing_in_us (every steel/iron manufacturing "
                                                     "process in the U.S.?)")
            ctx.notes.append(f"FTA's steel and iron rule covers {P(FTA_BA, 'steel_iron_scope')}; steel or iron that "
                             "is a component of a manufactured product is tested with that product instead.")
        elif kind == "manufactured_product":
            req_pct = P(FTA_BA, "manufactured_product_us_origin_components_pct_required")
            test = ("Manufactured product: all manufacturing processes in the U.S. and all components of U.S. "
                    f"origin (a component is U.S. origin if {P(FTA_BA, 'component_us_origin_if')}).")
            made = need(manufactured_in_us, "manufactured_in_us (all manufacturing processes in the U.S.?)")
            if dom_pct is None:
                missing.append("components (or domestic_component_cost_usd and total_component_cost_usd)")
                ok = None
            else:
                ok = dom_pct >= req_pct
                foreign = tot - dom
                ctx.line("U.S.-origin component share", dom_pct, "percent",
                         f"{_usd(dom)} U.S. / {_usd(tot)} all components = {round(dom_pct, 2):g}% "
                         + ("-> every component is U.S. origin" if ok else
                            f"-> {_usd(foreign)} of components are not U.S. origin (FTA requires all)"), FTA_BA)
            complies = False if (made is False or ok is False) else (None if made is None or ok is None else True)
            baba_pct = P(BABA_MFG, "domestic_component_cost_pct_greater_than")
            if ok is False and dom_pct > baba_pct:
                ctx.notes.append(f"This item would pass the BABA {_n(baba_pct)}% test, but FTA requires every "
                                 "component to be of U.S. origin.")
                ctx.use(BABA_MFG)
        else:
            test = ("49 CFR 661.5 covers iron, steel and manufactured products; the rulebook does not say how FTA "
                    "treats this material, so follow the contract's Buy America provisions.")
        ctx.notes.append(f"Rolling stock (buses, railcars) has its own rule ({P(FTA_BA, 'rolling_stock')}), which is "
                         "not in the rulebook and is not tested here.")
        if c_dom is not None:
            mult = P(FTA_BA, "price_differential_waiver_if_lowest_foreign_bid_times")
            exception = c_for * mult < c_dom - 1e-9
            inc = _pct(c_dom - c_for, c_for)
            exc_test = (f"Lowest foreign bid {_usd(c_for)} x {_n(mult)} = {_usd(c_for * mult)} "
                        f"{'<' if exception else '>='} lowest domestic bid {_usd(c_dom)} (+{round(inc, 2):g}%) -> "
                        + ("FTA can grant a price-differential waiver (the agency asks FTA)" if exception else
                           "no price-differential waiver"))
            ctx.line("Price differential (FTA)", inc, "percent", exc_test, FTA_BA)
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
                share = _pct(fis, tot)
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
            if iron_share:
                ctx.notes.append(f"Iron and steel are {round(iron_share, 2):g}% of this item's component cost. FAR "
                                 "52.225-9 tests material that is wholly or predominantly iron or steel by its foreign "
                                 "iron and steel content instead; if this item is, run the check again with item_type "
                                 "iron_steel.")
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
            over = _pct(c_dom - c_for, c_for)
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
                over = _pct(c_dom - c_for, c_for)
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
                 "fta_buy_america": "ask the transit agency to request an FTA waiver",
                 "far_buy_american": "ask the contracting officer for an exception"}[prog]
        step = (f"It does not meet the test. Source a compliant item, or {route}"
                + (" (the cost test above supports it)" if exception else "") + ".")
    res = DomesticContentResult(program=prog, program_label=PROGRAM_LABELS[prog], item=item_name, item_type=kind,
                                construction_material_category=material if kind == "construction_material" else None,
                                applies=applies, complies=complies, test=test, exception_possible=exception,
                                exception_test=exc_test, missing=list(dict.fromkeys(missing)), next_step=step,
                                line_items=ctx.lines, notes=ctx.notes, **out)
    return ctx.finish(res)


# ---------------------------------------------------------------- tool 5: CMMC and federal cybersecurity
# Rules from research/web_cmmc-federal-cyber.verified.json ("cmmc-..."): 32 CFR part 170, DFARS 204.73 / 204.75,
# DFARS 252.204-7012/-7019/-7020/-7021/-7025, FAR 52.204-21/-23/-24/-25/-26/-27 and 52.204-7/-8.
CMMC_SCOPE, CMMC_PHASES, CMMC_PRESCRIBE = ("cmmc-program-scope-dod-fci-cui", "cmmc-implementation-phases",
                                           "cmmc-dfars-7021-prescription-2028")
CMMC_L1, CMMC_L2, CMMC_REV3, CMMC_L3 = ("cmmc-level1-15-far-requirements", "cmmc-level2-nist-800-171-r2-110",
                                        "cmmc-nist-800-171-rev3-not-cmmc-baseline", "cmmc-level3-24-nist-800-172")
CMMC_L1_SELF, CMMC_L2_SELF, CMMC_L2_C3PAO = ("cmmc-level1-annual-self-assessment", "cmmc-level2-self-assessment-3-years",
                                             "cmmc-level2-c3pao-certification-3-years")
CMMC_POAM, CMMC_SCORING, CMMC_AFFIRM, CMMC_SUBS = ("cmmc-poam-80pct-180-day-closeout", "cmmc-level2-scoring-5-3-1",
                                                   "cmmc-affirmation-annual-sprs", "cmmc-subcontractor-flowdown-levels")
CMMC_SCOPE_L1, CMMC_CLOUD = "cmmc-level1-scope-fci-assets", "cmmc-cloud-fedramp-moderate"
CMMC_7021, CMMC_CURRENT, CMMC_AWARD, CMMC_7025 = ("cmmc-dfars-7021-contractor-duties", "cmmc-current-status-definition",
                                                  "cmmc-award-eligibility-sprs", "cmmc-dfars-7025-notice-uids")
CMMC_STATUS_DATE = "cmmc-status-date-definition"
DFARS_7012, DFARS_INCIDENT, DFARS_MEDIA, DFARS_7012_FLOW, CDI_DEF = (
    "cmmc-dfars-7012-nist-800-171-safeguarding", "cmmc-dfars-7012-incident-72-hours", "cmmc-dfars-7012-media-90-days",
    "cmmc-dfars-7012-flowdown", "cmmc-cdi-definition")
DFARS_7019, DFARS_7020 = "cmmc-dfars-7019-assessment-3-years", "cmmc-dfars-7020-access-rebuttal-subs"
FCI_DEF, CUI_DEF, FAR_21, NON_DOD = ("cmmc-fci-definition", "cmmc-cui-definition",
                                     "cmmc-far-52-204-21-basic-safeguarding", "cmmc-non-dod-no-cmmc")
KASPERSKY, SEC889, SEC889_REPS, BYTEDANCE, SAM_REPS = (
    "cmmc-kaspersky-52-204-23", "cmmc-section-889-52-204-25", "cmmc-section-889-representations",
    "cmmc-bytedance-52-204-27", "cmmc-sam-annual-reps-12-months")

CMMC_RULE_IDS = (CMMC_SCOPE, CMMC_PHASES, CMMC_PRESCRIBE, CMMC_L1, CMMC_L2, CMMC_REV3, CMMC_L3, CMMC_L1_SELF,
                 CMMC_L2_SELF, CMMC_L2_C3PAO, CMMC_POAM, CMMC_SCORING, CMMC_AFFIRM, CMMC_SUBS, CMMC_SCOPE_L1,
                 CMMC_CLOUD, CMMC_7021, CMMC_CURRENT, CMMC_STATUS_DATE, CMMC_AWARD, CMMC_7025, DFARS_7012,
                 DFARS_INCIDENT, DFARS_MEDIA,
                 DFARS_7012_FLOW, CDI_DEF, DFARS_7019, DFARS_7020, FCI_DEF, CUI_DEF, FAR_21, NON_DOD, KASPERSKY,
                 SEC889, SEC889_REPS, BYTEDANCE, SAM_REPS)
RULE_IDS = RULE_IDS + CMMC_RULE_IDS
# Every "cmmc-" pack rule is in CMMC_RULE_IDS; none is unused.
CMMC_NOT_USED: dict[str, str] = {}

MAX_TIER = 20               # typo guard on subcontract_tier, not a legal number
SCORE_FLOOR = -1000         # typo guard on sprs_score (a Level 2 score can be negative), not a legal number

CmmcLevel = Literal["Level 1 (Self)", "Level 2 (Self)", "Level 2 (C3PAO)", "Level 3 (DIBCAC)"]
LEVELS: tuple[str, ...] = ("Level 1 (Self)", "Level 2 (Self)", "Level 2 (C3PAO)", "Level 3 (DIBCAC)")
# "or higher" (32 CFR 170.16(a), 170.17(a), 170.18(a): a higher status satisfies the lower ones; DFARS 204.7502(b))
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS, 1)}
Phase = Literal["before_phase_1", "phase_1", "phase_2", "phase_3", "phase_4"]
CmmcStatus = Literal["Final Level 1 (Self)", "Conditional Level 2 (Self)", "Final Level 2 (Self)",
                     "Conditional Level 2 (C3PAO)", "Final Level 2 (C3PAO)", "Conditional Level 3 (DIBCAC)",
                     "Final Level 3 (DIBCAC)"]


def normalize_cmmc_level(value, field: str = "solicitation_cmmc_level") -> Optional[str]:
    """'L2 C3PAO', 'level 2 (c3pao)', 'CMMC Level 1 (Self)' -> a LEVELS label; 'none' / 'no CMMC' -> 'none';
    None / '' -> None. A bare 'Level 2' is refused: Self and C3PAO are different requirements."""
    t = _words(value)
    if not t:
        return None
    if t in ("none", "no", "no cmmc", "not required", "no cmmc requirement", "n a", "na", "no clause"):
        return "none"
    m = re.search(r"(?:^| )(?:level|lvl|l)? ?([123])(?: |$)", t) or re.search(r"(?:^| )l([123])(?: |$)", t)
    level = m.group(1) if m else None
    c3pao, dibcac, self_ = _has(t, "c3pao", "third party", "certification"), _has(t, "dibcac", "dcma"), _has(t, "self")
    if level == "1" and not (c3pao or dibcac):
        return LEVELS[0]
    if level == "2" and self_ and not (c3pao or dibcac):
        return LEVELS[1]
    if level == "2" and c3pao and not (self_ or dibcac):
        return LEVELS[2]
    if level == "3" or (level is None and dibcac):
        return LEVELS[3]
    if level == "2":
        raise ValueError(f"{field} {value!r}: say 'Level 2 (Self)' or 'Level 2 (C3PAO)'; DFARS 252.204-7025(b)(1) "
                         "names one of them")
    raise ValueError(f"Unknown CMMC level {value!r} in {field}. Use one of: {', '.join(LEVELS)}, or 'none' when the "
                     "solicitation has no CMMC requirement")


def normalize_cmmc_status(value) -> Optional[str]:
    """'final L2 c3pao' -> 'Final Level 2 (C3PAO)'. The status names are DFARS 204.7501's seven statuses."""
    t = _words(value)
    if not t:
        return None
    kind = "Conditional" if _has(t, "conditional", "cond") else ("Final" if _has(t, "final") else None)
    level = normalize_cmmc_level(re.sub(r"(?:^| )(?:conditional|cond|final)(?= |$)", " ", t).strip() or t,
                                 "current_cmmc_status")
    if level in (None, "none"):
        raise ValueError(f"current_cmmc_status {value!r} is not a CMMC status; use e.g. 'Final Level 2 (C3PAO)'")
    if level == LEVELS[0]:
        if kind == "Conditional":
            raise ValueError("There is no Conditional Level 1: 32 CFR 170.21(a)(1) allows no POA&M at Level 1, so the "
                             "only Level 1 status is 'Final Level 1 (Self)'")
        kind = "Final"
    if kind is None:
        raise ValueError(f"current_cmmc_status {value!r}: say Final or Conditional (e.g. 'Conditional Level 2 (Self)')")
    return f"{kind} {level}"


def normalize_tier(value) -> int:
    """0 = prime; 1 = first-tier subcontractor; 'prime', 'sub', '2nd tier' are understood."""
    if isinstance(value, bool) or (isinstance(value, str) and re.search(r"-\s*\d", value)):
        raise ValueError(f"subcontract_tier must be a whole number of at least 0 (0 = prime), got {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value != int(value):
            raise ValueError(f"subcontract_tier must be a whole number (0 = prime), got {value!r}")
        tier = int(value)
    else:
        t = _words(value)
        words = {"prime": 0, "prime contractor": 0, "gc": 0, "general contractor": 0, "sub": 1, "subcontractor": 1,
                 "first tier": 1, "first tier sub": 1, "first tier subcontractor": 1, "second tier": 2,
                 "third tier": 3}
        m = re.fullmatch(r"(?:tier )?(\d+)(?:st|nd|rd|th)?(?: tier)?(?: sub(?:contractor)?)?", t)
        if t in words:
            tier = words[t]
        elif m:
            tier = int(m.group(1))
        else:
            raise ValueError(f"subcontract_tier must be 0 for the prime contractor or 1, 2, ... for a subcontractor "
                             f"tier, got {value!r}")
    if not 0 <= tier <= MAX_TIER:
        raise ValueError(f"subcontract_tier must be between 0 (prime) and {MAX_TIER}, got {value!r}")
    return tier


def _add_years(d: date, years: int) -> date:
    """d + whole years; Feb 29 -> Feb 28 (the earlier day, so a 'not older than' limit is never overstated)."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _cmmc_phase(on: date) -> tuple[str, Optional[date], Optional[date]]:
    """(phase, start of that phase, start of the next phase) for a contract awarded on `on` (32 CFR 170.3(e))."""
    starts = [_iso_param(CMMC_PHASES, f"phase_{i}_start") for i in (1, 2, 3, 4)]
    if on < starts[0]:
        return "before_phase_1", None, starts[0]
    for i in (3, 2, 1, 0):
        if on >= starts[i]:
            return f"phase_{i + 1}", starts[i], starts[i + 1] if i < 3 else None
    raise AssertionError("unreachable")


class CmmcRequirementsResult(core.Result):
    as_of: str
    dod_contract: bool
    your_role: Literal["prime", "subcontractor"]
    subcontract_tier: int
    information: Literal["cui", "fci_only", "none"]
    contract_award_date: str
    cmmc_phase: Phase = Field(description="Implementation phase on the award date (32 CFR 170.3(e))")
    cmmc_phase_started_on: Optional[str] = None
    next_phase_starts_on: Optional[str] = None
    cmmc_applies: Optional[bool] = Field(description=(
        "True / False, or None when only the solicitation can settle it (Phases 1-3: the program office decides "
        "whether a CMMC level goes in; plan as if it applies)"))
    required_cmmc_level: Optional[CmmcLevel] = Field(None, description=(
        "The level you must hold (or higher): the solicitation's level for a prime, the 32 CFR 170.23 level for a "
        "subcontractor, or the minimum for your information when the solicitation's level was not given"))
    required_level_is_minimum_only: bool = Field(False, description=(
        "True when the solicitation's level was not given, so required_cmmc_level is only the floor for your "
        "information"))
    plan_for_cmmc_level: Optional[CmmcLevel] = Field(None, description=(
        "What DoD said it intends for the award date's phase (32 CFR 170.3(e)) when the solicitation's level is not "
        "known; the solicitation controls"))
    assessment: Optional[str] = None
    nist_sp_800_171_revision_for_cmmc: Optional[str] = None
    sprs_score_outcome: Optional[Literal["final", "conditional_possible", "not_eligible"]] = None
    status_meets_required_level: Optional[bool] = None
    status_current_at_award: Optional[bool] = None
    summary: str
    next_step: str
    requirements: list[Requirement] = Field(default_factory=list)
    key_dates: list[KeyDate] = Field(default_factory=list)
    line_items: list[Line] = Field(default_factory=list)
    unverified_rule_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _min_conditional_score() -> int:
    """Lowest whole Level 2 score with score / max >= ratio (32 CFR 170.21(a)(2)(i)): 0.8 x 110 = 88."""
    return math.ceil(round(P(CMMC_POAM, "min_score_ratio") * P(CMMC_L2, "max_score"), 9))


def _assessment_text(level: str) -> str:
    yrs, l1 = P(CMMC_L2_SELF, "reassessment_interval_years"), P(CMMC_L1_SELF, "reassessment_interval_months")
    keep = P(CMMC_L1_SELF, "artifact_retention_years")
    if level == LEVELS[0]:
        return (f"Self-assessment of all {P(CMMC_L1, 'requirement_count')} FAR 52.204-21 requirements every {l1} "
                f"months, all MET (no POA&M), results and an affirmation in SPRS; keep evidence {keep} years")
    if level == LEVELS[1]:
        return (f"Self-assessment of all {P(CMMC_L2, 'requirement_count')} NIST SP 800-171 Rev 2 requirements every "
                f"{yrs} years, score and affirmation in SPRS, yearly affirmation; keep evidence {keep} years")
    if level == LEVELS[2]:
        return (f"Certification assessment by an authorized C3PAO every {P(CMMC_L2_C3PAO, 'reassessment_interval_years')}"
                f" years (results to eMASS and SPRS), affirmation at each assessment and yearly; keep hashed evidence "
                f"{P(CMMC_L2_C3PAO, 'artifact_retention_years')} years")
    return (f"DCMA DIBCAC assessment of {P(CMMC_L3, 'requirement_count')} NIST SP 800-172 requirements every "
            f"{P(CMMC_L3, 'reassessment_years')} years, on top of a Final Level 2 (C3PAO) renewed every "
            f"{P(CMMC_L3, 'level_2_c3pao_reassessment_years')} years; yearly affirmations of both")


def _level_rule(level: str) -> list[str]:
    return {LEVELS[0]: [CMMC_L1, CMMC_L1_SELF], LEVELS[1]: [CMMC_L2, CMMC_L2_SELF],
            LEVELS[2]: [CMMC_L2, CMMC_L2_C3PAO], LEVELS[3]: [CMMC_L3, CMMC_L2_C3PAO]}[level]


def _status_dates(ctx: _Ctx, status: str, status_date: date, last_aff: Optional[date], award: date) -> bool:
    """Adds the closeout, renewal and affirmation dates of a CMMC status you hold; True if it is still current on
    `award` (DFARS 204.7501 'Current') without a renewal. `status_date` is the CMMC Status Date (32 CFR 170.4): a
    Final that follows a Conditional keeps the Conditional's date, so its 3-year renewal counts from that date."""
    level = status.split(" ", 1)[1]
    cond = status.startswith("Conditional")
    rule = {LEVELS[0]: CMMC_L1_SELF, LEVELS[1]: CMMC_L2_SELF, LEVELS[2]: CMMC_L2_C3PAO, LEVELS[3]: CMMC_L3}[level]
    if cond:
        days = P(CMMC_CURRENT, "conditional_level_2_max_days" if level != LEVELS[3] else "conditional_level_3_max_days")
        expires = status_date + timedelta(days=days)
        ctx.date(f"POA&M closeout assessment posted, with a new affirmation, for your {status} ({days} days from the "
                 f"{status_date} CMMC Status Date) or the Conditional status expires", expires, CMMC_POAM,
                 CMMC_CURRENT, CMMC_AFFIRM)
        # 32 CFR 170.4: the Final that follows the closeout keeps this date; 170.16(a)(1), 170.17(a)(1) and
        # 170.18(a)(1) count the 3-year reassessment from the Conditional's CMMC Status Date.
        yrs = P(CMMC_STATUS_DATE, "reassessment_years")
        ctx.date(f"New {level} assessment posted within {yrs} years of the {status_date} CMMC Status Date (the Final "
                 "status that follows the closeout keeps that date)", _add_years(status_date, yrs), rule,
                 CMMC_STATUS_DATE)
    else:
        key = {LEVELS[0]: "final_level_1_max_years", LEVELS[1]: "final_level_2_max_years",
               LEVELS[2]: "final_level_2_max_years", LEVELS[3]: "final_level_3_max_years"}[level]
        yrs = P(CMMC_CURRENT, key)
        expires = _add_years(status_date, yrs)
        ctx.date(f"New {level} assessment posted before your {status} ({status_date} CMMC Status Date) is {yrs} year"
                 f"{'s' if yrs != 1 else ''} old", expires, rule, CMMC_CURRENT,
                 *([] if level == LEVELS[0] else [CMMC_STATUS_DATE]))
        if level != LEVELS[0]:
            ctx.notes.append(f"Renewal dates count from the CMMC Status Date in SPRS. If your {status} came from a "
                             "POA&M closeout, that date is the earlier Conditional status date, not the closeout date "
                             "(32 CFR 170.4: 'A new date is not set for a Final that follows a Conditional'); give "
                             "that date as cmmc_status_date.")
    aff_from = last_aff or status_date
    aff_due = _add_years(aff_from, P(CMMC_CURRENT, "affirmation_max_years"))
    ctx.date("Affirming Official's annual affirmation in SPRS (from the "
             + (f"last affirmation on {last_aff}" if last_aff else f"{status_date} CMMC Status Date; give "
                "last_affirmation_date if you affirmed since")
             + ("; once the POA&M is closed out, yearly affirmations follow the Final's CMMC Status Date, which stays "
                f"{status_date}" if cond else "") + ")", aff_due, CMMC_AFFIRM, CMMC_CURRENT,
             *([CMMC_STATUS_DATE] if cond else []))
    if level == LEVELS[3]:
        ctx.notes.append("A Level 3 (DIBCAC) status rests on a Final Level 2 (C3PAO) for the same scope: that Level 2 "
                         f"(C3PAO) must also be reassessed every {P(CMMC_L3, 'level_2_c3pao_reassessment_years')} "
                         "years and affirmed every year (32 CFR 170.18(a), 170.22(b)(4)); give that status in a "
                         "separate call to date it.")
    return award <= expires and award <= aff_due


def cmmc_requirements(handles_cui, handles_fci_only, dod_contract, contract_award_date=None, subcontract_tier=0,
                      solicitation_cmmc_level=None, sprs_score=None, current_cmmc_status=None, cmmc_status_date=None,
                      last_affirmation_date=None, as_of=None) -> CmmcRequirementsResult:
    """CMMC level, assessments, SPRS steps, incident reporting and federal cyber clauses for one contract."""
    today = _today(as_of)
    cui = _flag(handles_cui, "handles_cui")
    fci_only = _flag(handles_fci_only, "handles_fci_only")
    dod = _flag(dod_contract, "dod_contract")
    if cui and fci_only:
        raise ValueError("handles_fci_only means FCI and no CUI, but handles_cui is true. Set handles_fci_only=false "
                         "when you will handle CUI (Level 2 also covers the FCI requirements, 32 CFR 170.16(a)).")
    tier = normalize_tier(subcontract_tier)
    prime = tier == 0
    award = _opt_date(contract_award_date, "contract_award_date") or today
    sol = normalize_cmmc_level(solicitation_cmmc_level)
    if sol is not None and not dod:
        raise ValueError("A CMMC level is set only in DoD solicitations (DFARS 252.204-7025); dod_contract is false. "
                         "Leave solicitation_cmmc_level empty or set dod_contract=true.")
    status = normalize_cmmc_status(current_cmmc_status)
    status_date = _opt_date(cmmc_status_date, "cmmc_status_date")
    last_aff = _opt_date(last_affirmation_date, "last_affirmation_date")
    if status_date and status_date > today:
        raise ValueError(f"cmmc_status_date ({status_date}) is after as_of ({today}); give the date the status was "
                         "posted in SPRS or eMASS")
    if last_aff and status_date and last_aff < status_date:
        raise ValueError(f"last_affirmation_date ({last_aff}) is before cmmc_status_date ({status_date}); an "
                         "affirmation is due with each new status (32 CFR 170.22(a)(3))")
    if last_aff and last_aff > today:
        raise ValueError(f"last_affirmation_date ({last_aff}) is after as_of ({today})")
    score = None
    if sprs_score is not None and not (isinstance(sprs_score, str) and not sprs_score.strip()):
        if isinstance(sprs_score, bool):
            raise ValueError(f"sprs_score must be a whole number, got {sprs_score!r}")
        try:
            s = float(_clean_num(sprs_score)) if isinstance(sprs_score, str) else float(sprs_score)
        except (TypeError, ValueError):
            raise ValueError(f"sprs_score must be a whole number like 104, got {sprs_score!r}") from None
        top = P(CMMC_L2, "max_score")
        if not math.isfinite(s) or s != int(s) or s > top or s < SCORE_FLOOR:
            raise ValueError(f"sprs_score must be a whole number of at most {top} (it can be negative), got "
                             f"{sprs_score!r}")
        score = int(s)

    info = "cui" if cui else ("fci_only" if fci_only else "none")
    phase, started, nxt = _cmmc_phase(award)
    ctx = _Ctx(today)
    role = "prime" if prime else "subcontractor"
    phase4 = _iso_param(CMMC_PRESCRIBE, "from")

    # ------------------------------------------------ CMMC level
    required = plan = None
    minimum_only = False
    applies: Optional[bool]
    level_ids = [CMMC_SCOPE, CMMC_PHASES, CMMC_PRESCRIBE]
    if not dod:
        applies = False
        why = ("CMMC applies only to DoD contracts and subcontracts (32 CFR 170.3(a)(1)); this is not a DoD "
               "contract.")
        level_ids = [CMMC_SCOPE, NON_DOD]
    elif info == "none":
        applies = False
        why = ("CMMC applies to contractors and subcontractors that will process, store or transmit FCI or CUI on "
               "their own systems; you said you will handle neither. If you will hold any information provided by or "
               "generated for the government under the contract that is not meant for public release (other than "
               "simple transactional data such as payment information), that is FCI: set handles_fci_only=true.")
        level_ids += [FCI_DEF]
        if sol not in (None, "none"):
            ctx.notes.append(f"The solicitation names {sol}: the moment any of your systems will hold FCI or CUI for "
                             "this contract, you need that status for them (DFARS 252.204-7025(b)).")
    elif sol == "none":
        applies = False
        why = ("The solicitation (or prime contract) has no CMMC level, so no CMMC status is a condition of this "
               "award.")
        if award >= phase4:
            why += (f" From {phase4} DFARS 204.7504(a)(2) prescribes the clause whenever contractor systems will "
                    "handle FCI or CUI, unless DoD waived it (32 CFR 170.5(d)) or the buy is COTS-only: confirm with "
                    "the contracting officer.")
    else:
        floor = LEVELS[1] if cui else LEVELS[0]
        if prime:
            if sol:
                required = sol
                if cui and LEVEL_RANK[sol] < LEVEL_RANK[LEVELS[1]]:
                    ctx.notes.append("The solicitation names Level 1 (Self) but you will handle CUI: Level 1 covers only "
                                     "the FCI safeguards. Ask the contracting officer; DFARS 252.204-7012 and NIST SP "
                                     "800-171 apply to the CUI either way.")
            else:
                required, minimum_only = floor, True
        else:
            level_ids.append(CMMC_SUBS)
            if fci_only:
                required = LEVELS[0]
            elif sol and LEVEL_RANK[sol] >= LEVEL_RANK[LEVELS[2]]:
                required = LEVELS[2]
            else:
                required, minimum_only = LEVELS[1], sol is None
            if sol == LEVELS[0] and cui:
                ctx.notes.append("The prime contract names Level 1 (Self), but a subcontractor that handles CUI needs "
                                 "at least Level 2 (Self) (32 CFR 170.23(a)(2)); tell the prime.")
        if sol:
            applies = True
        elif phase == "phase_4":
            applies = True
        else:
            applies = None
        if not sol and phase != "before_phase_1" and cui:
            plan = LEVELS[1] if phase == "phase_1" else LEVELS[2]
        elif not sol and phase != "before_phase_1":
            plan = LEVELS[0]
        level_ids += _level_rule(required)
        whose = "the solicitation" if prime else "the prime contract"
        info_txt = "CUI" if cui else "FCI only"
        if sol and prime:
            why = f"{whose.capitalize()} requires {sol} (DFARS 252.204-7025(b)(1)); you must hold it or higher."
        elif not prime:
            why = (f"You are a tier-{tier} subcontractor handling {info_txt}; under 32 CFR 170.23 that needs at least "
                   f"{required}" + (f" because the prime contract requires {sol}" if sol and cui and
                                    LEVEL_RANK[sol] >= LEVEL_RANK[LEVELS[2]] else "") + ".")
        else:
            why = (f"You will handle {info_txt}; the CMMC level that protects it is {required} at least (Level 1 = the "
                   f"FAR 52.204-21 safeguards for FCI, Level 2 = NIST SP 800-171 Rev 2 for CUI). The solicitation's "
                   "DFARS 252.204-7025 notice names the level that binds you.")
        if applies is None:
            if phase == "before_phase_1":
                why += (f" The award date {award} is before Phase 1 ({_iso_param(CMMC_PHASES, 'phase_1_start')}), "
                        "when the current DFARS 252.204-7021 (NOV 2025) and the 252.204-7025 notice took effect: a "
                        "CMMC requirement applies only if your contract includes a CMMC clause. The version in use "
                        f"until {P(CMMC_7021, 'prior_version_in_effect_until')}, {P(CMMC_7021, 'prior_version')}, "
                        f"required {P(CMMC_7021, 'prior_version_requirement')}.")
                level_ids.append(CMMC_7021)
            else:
                why += (f" Until {_iso_param(CMMC_PRESCRIBE, 'until')} the clause goes in only when the program office "
                        f"sets a level (DFARS 204.7504(a)(1)); plan as if it applies.")
        elif phase == "phase_4" and not sol:
            why += (f" From {phase4} DFARS 204.7504(a)(2) puts the clause in whenever contractor systems will handle "
                    "FCI or CUI (unless waived or COTS-only).")
    item = f"CMMC status {required} (or higher)" if required else "CMMC status"
    if not required:
        what = ("No CMMC status is needed for this award; the "
                + ("FAR 52.204-21 safeguards and " if info != "none" else "") + "supply-chain clauses below still "
                "apply." if dod else "No CMMC. Follow the federal clauses below.")
    elif phase == "before_phase_1":
        what = (f"Check whether your {'contract' if prime else 'subcontract'} includes DFARS 252.204-7021 and which "
                "version; meet the CMMC level it names. CMMC statuses, SPRS affirmations and CMMC UIDs started with "
                f"Phase 1 on {_iso_param(CMMC_PHASES, 'phase_1_start')}.")
    elif prime:
        # DFARS 252.204-7025(b)(1)-(2) and 252.204-7021(d)(1)(i): the level is needed for every contractor system
        # that will process, store or transmit FCI *or* CUI, with a current affirmation, before award.
        what = (f"Before award, hold a current {required} status and a current affirmation in SPRS for every "
                "system that will process, store or transmit FCI or CUI for this contract, and list the CMMC UIDs in "
                "your proposal.")
    else:
        what = (f"Hold a current {required} status and an affirmation in SPRS before your subcontract is awarded; the "
                "prime must check it (DFARS 252.204-7021(f)(2)).")
    ctx.req("CMMC", item, applies, why, what, level_ids)

    if required and applies is not False:
        lvl = required
        ctx.req("CMMC", f"Assessment for {lvl}", applies, _assessment_text(lvl) + ".",
                ("Define the assessment scope first: every system that handles FCI (IoT, OT and other specialized "
                 "assets are outside the Level 1 scope)." if lvl == LEVELS[0] else
                 "Write the system security plan first: without an up-to-date SSP the assessment cannot be "
                 "completed (32 CFR 170.24(c)(2)(i)(B)(5))."),
                _level_rule(lvl) + ([CMMC_SCOPE_L1] if lvl == LEVELS[0] else [CMMC_SCORING]))
        ratio = P(CMMC_POAM, "min_score_ratio")
        if lvl == LEVELS[0]:
            ctx.req("CMMC", "No POA&M at Level 1", applies,
                    "Every Level 1 requirement must be MET; a plan of action and milestones is never allowed at Level "
                    "1 and award needs a Final Level 1 (Self) status (DFARS 204.7502(b)(2)).",
                    "Fix every gap before you post the self-assessment.", [CMMC_POAM, CMMC_L1_SELF, CMMC_AWARD])
        elif lvl == LEVELS[3]:
            top3 = P(CMMC_POAM, "level_3_max_score")
            mn3 = math.ceil(round(ratio * top3, 9))
            ctx.line("Lowest Level 3 score for a Conditional status", mn3, "count",
                     f"ceil({_n(ratio)} x {top3}) = {mn3} (score / {top3} >= {_n(ratio)}; 1 point per requirement)",
                     CMMC_POAM, CMMC_L3)
            ctx.req("CMMC", "POA&M rules for a Conditional Level 3", applies,
                    f"Level 3 starts only from a Final Level 2 (C3PAO) (all {P(CMMC_L2, 'max_score')} points). A "
                    f"Conditional Level 3 needs at least {mn3} of {top3} ({_n(ratio)} x {top3}), none of "
                    f"{len(P(CMMC_POAM, 'level_3_never_on_poam'))} listed requirements open, and a DIBCAC closeout "
                    f"within {P(CMMC_POAM, 'closeout_days')} days.",
                    "Never on a Level 3 POA&M: " + "; ".join(P(CMMC_POAM, "level_3_never_on_poam")) + ".",
                    [CMMC_POAM, CMMC_L3, CMMC_SCORING])
        else:
            mn = _min_conditional_score()
            ctx.line("Lowest Level 2 score for a Conditional status", mn, "count",
                     f"ceil({_n(ratio)} x {P(CMMC_L2, 'max_score')}) = {mn} (score / {P(CMMC_L2, 'max_score')} >= "
                     f"{_n(ratio)})", CMMC_POAM, CMMC_L2)
            ctx.req("CMMC", "POA&M rules for a Conditional status", applies,
                    f"A Conditional status needs a score of at least {mn} of {P(CMMC_L2, 'max_score')} "
                    f"({_n(ratio)} x {P(CMMC_L2, 'max_score')}), only {P(CMMC_POAM, 'level_2_poam_item_max_points')}-point "
                    f"items on the POA&M (SC.L2-3.13.11 non-FIPS encryption excepted), none of "
                    f"{len(P(CMMC_POAM, 'level_2_never_on_poam'))} listed requirements open, and a closeout assessment "
                    f"within {P(CMMC_POAM, 'closeout_days')} days or the status expires.",
                    "Never on a POA&M: " + "; ".join(P(CMMC_POAM, "level_2_never_on_poam")) + ".",
                    [CMMC_POAM, CMMC_SCORING])
        ctx.req("CMMC", "Annual affirmation by your Affirming Official", applies,
                f"A senior official affirms continuing compliance in SPRS at each assessment"
                + ("" if lvl == LEVELS[0] else ", after a POA&M closeout")       # 170.21(a)(1): no Level 1 POA&M
                + f" and every {P(CMMC_AFFIRM, 'affirmation_interval_months')} months"
                + (f", for the Final Level 2 (C3PAO) as well as the Level 3 status" if lvl == LEVELS[3] else "")
                + f"; an affirmation older than {P(CMMC_CURRENT, 'affirmation_max_years')} year makes the status not "
                "current.",
                "Name the Affirming Official now and calendar the yearly affirmation.", [CMMC_AFFIRM, CMMC_CURRENT])
        if prime and phase != "before_phase_1":          # DFARS 252.204-7025(d) applies from Phase 1
            ctx.req("CMMC", "CMMC UIDs in the proposal; SPRS check before award", applies,
                    f"The contracting officer checks SPRS and cannot award, exercise an option or extend without a "
                    f"current status at the required level for every CMMC UID (each UID is "
                    f"{P(CMMC_7025, 'cmmc_uid_characters')} characters, issued by SPRS).",
                    "Post the assessment in SPRS early enough to get the UIDs and put them in the proposal.",
                    [CMMC_AWARD, CMMC_7025, CMMC_CURRENT])
        ctx.req("CMMC", "DFARS 252.204-7021 duties during performance", applies,
                "Keep the status current for the whole contract, handle FCI/CUI only on systems that hold it, affirm "
                "yearly per CMMC UID" + (", report new or changed UIDs to the contracting officer" if prime else "")
                + " and close any POA&M to a Final status.",
                "Do not move contract FCI or CUI onto a system outside your assessed scope.", [CMMC_7021, CMMC_CURRENT])
        ctx.req("Subcontracts", "Flow CMMC down to your own subcontractors that will handle FCI or CUI", applies,
                f"32 CFR 170.23: FCI only = {P(CMMC_SUBS, 'fci_only')}; CUI = at least {P(CMMC_SUBS, 'cui_minimum')}, "
                f"or {P(CMMC_SUBS, 'cui_when_prime_requires_level_2_c3pao')} when the prime contract requires Level 2 "
                "(C3PAO) or Level 3. The clause flows to subcontracts that will involve FCI or CUI, COTS excluded.",
                "Before awarding each such subcontract, confirm its current status and affirmation (DFARS "
                "252.204-7021(f)(2)).", [CMMC_SUBS, CMMC_7021])
    if dod and info != "none" and phase != "phase_4":
        # 32 CFR 170.3(e)(1), (3), (4); DFARS 204.7503(c): option exercises can add a CMMC status later
        p1, p3 = _iso_param(CMMC_PHASES, "phase_1_start"), _iso_param(CMMC_PHASES, "phase_3_start")
        opt_why = ((f"Your award is before Phase 1 ({p1}): DoD may require Level 1 (Self) or Level 2 (Self) to "
                    "exercise an option on a contract awarded before then. " if phase == "before_phase_1" else
                    f"From Phase 3 ({p3}) DoD intends to require Level 2 (C3PAO) to exercise an option on applicable "
                    "contracts awarded after Phase 1 began. ")
                   + f"From Phase 4 ({phase4}) DoD will include CMMC requirements in option periods on contracts "
                   "awarded before Phase 4. The contracting officer checks SPRS before exercising an option or "
                   "extending the period of performance.")
        ctx.req("CMMC", "CMMC at option exercise", None, opt_why,
                "If the contract has option periods, ask " + ("the contracting officer" if prime else "your prime")
                + " which CMMC level each option will require and have that status current before the option is "
                "exercised.", [CMMC_PHASES, CMMC_AWARD])
    if required and applies is not False:
        if cui:
            ctx.req("CMMC", "Cloud services holding CUI: FedRAMP Moderate or equivalent", applies,
                    "A cloud service that processes, stores or transmits CUI must be FedRAMP Authorized at Moderate "
                    "(or higher) or meet the equivalent under DoD policy; your connecting systems stay in scope.",
                    "Get the provider's customer responsibility matrix into your SSP.", [CMMC_CLOUD])

    # ------------------------------------------------ safeguarding and incident reporting
    if info != "none":
        ctx.req("Safeguarding", "FAR 52.204-21 basic safeguarding (the 15 requirements)", True,
                f"Any federal contract where your systems may hold FCI carries FAR 52.204-21: "
                f"{P(FAR_21, 'requirement_count')} basic safeguards, flowed to subcontractors that may hold FCI"
                + (" (CUI you receive under the contract is also non-public contract information)" if cui else "")
                + ".", "Apply all 15 to every system that holds contract information.", [FAR_21, FCI_DEF, CMMC_L1])
    else:
        ctx.req("Safeguarding", "FAR 52.204-21 basic safeguarding (the 15 requirements)", False,
                "Only applies when your systems may hold FCI; you said they will not.",
                "Re-check if the agency sends you non-public drawings, schedules or other contract information.",
                [FAR_21, FCI_DEF])
    if dod and cui:
        rev = P(CMMC_L2, "standard")
        ctx.req("Safeguarding", "DFARS 252.204-7012: NIST SP 800-171 on systems holding covered defense information",
                True, f"DFARS 252.204-7012 is in all DoD contracts except COTS-only buys; CUI you receive or create "
                "in performing the contract meets its covered defense information definition, so NIST SP 800-171 "
                "applies to the systems holding it. CMMC Level 2 "
                f"uses {rev}; 32 CFR part 170 does not adopt Revision 3. For 7012 itself the version is the one 'in "
                "effect at the time the solicitation is issued or as authorized by the Contracting Officer'.",
                "Ask the contracting officer which NIST SP 800-171 revision applies to 7012 compliance and write the "
                "SSP to it.", [DFARS_7012, CDI_DEF, CUI_DEF, CMMC_REV3, CMMC_L2])
        if prime:
            ctx.req("Safeguarding", "NIST SP 800-171 DoD Assessment in SPRS (DFARS 252.204-7019)", True,
                    f"To be considered for award you need a NIST SP 800-171 DoD Assessment no more than "
                    f"{P(DFARS_7019, 'max_age_years')} years old (unless the solicitation sets less) with its summary "
                    f"score (out of {P(DFARS_7019, 'max_score')}) in SPRS.",
                    f"No score yet: do a Basic Assessment and email the summary to "
                    f"{P(DFARS_7019, 'basic_submission_email')}.", [DFARS_7019, DFARS_7020])
        else:
            ctx.req("Subcontracts", "Basic NIST SP 800-171 Assessment before your subcontract award (DFARS "
                    "252.204-7020)", True,
                    f"Your prime may not award a subcontract that requires NIST SP 800-171 unless you completed at "
                    f"least a Basic Assessment within the last {P(DFARS_7020, 'subcontract_assessment_max_age_years')} "
                    "years.",
                    f"Post a Basic Assessment (email {P(DFARS_7019, 'basic_submission_email')}) before the subcontract "
                    "is signed.", [DFARS_7020, DFARS_7019])
        hrs, keep = P(DFARS_INCIDENT, "report_within_hours"), P(DFARS_MEDIA, "preserve_days")
        ctx.req("Incident reporting", f"Report cyber incidents to DoD within {hrs} hours", True,
                f"A cyber incident affecting a system with covered defense information must be reported at "
                f"{P(DFARS_INCIDENT, 'report_to')} within {hrs} hours of discovery (a DoD-approved medium assurance "
                f"certificate is needed to file); preserve images and packet captures for {keep} days from the report "
                "and send isolated malware to DC3."
                + ("" if prime else " Give the DoD incident report number to the prime (or next higher tier)."),
                "Get the medium assurance certificate now and put the 72-hour clock in your incident response plan.",
                [DFARS_INCIDENT, DFARS_MEDIA] + ([] if prime else [DFARS_7012_FLOW]))
        ctx.req("Subcontracts", "Flow DFARS 252.204-7012 to subcontracts with covered defense information", True,
                "Put 252.204-7012 unaltered in subcontracts whose performance involves covered defense information "
                "or operationally critical support; subcontractors report variance requests and incident numbers up "
                "the chain.", "Check each subcontract package for CUI markings before you send it.", [DFARS_7012_FLOW])
    elif dod and fci_only:
        ctx.req("Safeguarding", "DFARS 252.204-7012: NIST SP 800-171", False,
                "The clause is in nearly every DoD contract, but its NIST SP 800-171 and 72-hour reporting duties "
                "attach to systems holding covered defense information; you said you will handle FCI only.",
                "If the government later sends CUI-marked documents, 7012 and NIST SP 800-171 apply to them.",
                [DFARS_7012, CDI_DEF])
    if not dod and cui:
        ctx.req("Safeguarding", "CUI safeguarding on a civilian contract", None,
                "No CMMC and no FAR-wide CUI clause (eCFR 48 CFR part 52, checked as of "
                f"{P(NON_DOD, 'checked_as_of')}); CUI rules come from the agency, and FAR 52.204-21 does not relieve "
                "you of them.", "Ask the contracting officer which CUI clauses and standard the contract uses.",
                [NON_DOD, CUI_DEF])

    # ------------------------------------------------ supply chain prohibitions (every federal contract)
    d889 = P(SEC889, "report_within_business_days")
    ctx.req("Supply chain", "Section 889: no Huawei/ZTE/Hytera/Hikvision/Dahua telecom or video surveillance", True,
            f"FAR 52.204-25 is in all solicitations and contracts. Report covered equipment found during performance "
            f"within {d889} business day" + ("" if prime else "; your flowed-down clause omits the company-wide 'use' "
                                              "ban of (b)(2), which binds the prime") + ".",
            "Check jobsite cameras, radios and network gear against the covered producers before you install or "
            "deliver them.", [SEC889])
    if prime:
        ctx.req("SAM.gov", "Section 889 representations (FAR 52.204-26 in SAM, 52.204-24 in the offer)", True,
                "52.204-26 is answered in your SAM.gov annual representations and applies to all solicitations; "
                "complete 52.204-24 only for an answer that was 'does'. A reasonable inquiry needs no audit.",
                "Review the SAM.gov exclusions list for covered telecom entities and confirm your answers.",
                [SEC889_REPS])
    ctx.req("Supply chain", "No Kaspersky products for the government or contract deliverables", True,
            f"FAR 52.204-23 is in all solicitations and contracts and flows to all subcontracts; report a covered "
            f"article provided to the government within {P(KASPERSKY, 'report_within_business_days')} business days.",
            "Remove Kaspersky software from machines used on the contract.", [KASPERSKY])
    ctx.req("Supply chain", "No TikTok on devices used for the contract", True,
            "FAR 52.204-27 bars TikTok (or any ByteDance successor app) on IT used or provided under the contract, "
            "including employees' own devices, unless the contracting officer gives written notice of an exception.",
            "Tell crews and office staff; check phones used for project email, photos and apps.", [BYTEDANCE])
    if prime:
        ctx.req("SAM.gov", "SAM.gov registration and annual representations", True,
                f"Be registered in SAM.gov when you bid and at award; by bidding you confirm the SAM.gov "
                f"representations were updated within the last {P(SAM_REPS, 'updated_within_months')} months and are "
                "current, accurate and complete.", "Update SAM.gov before the bid if anything changed.",
                [SAM_REPS, SAM_REG, SAM_ANNUAL])
    else:
        cage = bool(dod and required and applies is not False)
        ctx.req("SAM.gov", "SAM.gov registration and representations", False,
                "FAR 52.204-7 and 52.204-8 bind offerors on the prime contract, not subcontractors"
                + ("; your SPRS entries must still list the CAGE codes of the systems in scope (32 CFR "
                   "170.15(a)(1)(i)(D), 170.16(a)(1)(i)(D))." if cage else "."),
                "Ask your prime what it needs from you.", [SAM_REPS] + ([CMMC_L1_SELF, CMMC_L2_SELF] if cage else []))

    # ------------------------------------------------ key dates
    p_dates = [(f"Phase {i} of CMMC implementation begins", _iso_param(CMMC_PHASES, f"phase_{i}_start"))
               for i in (1, 2, 3, 4)]
    for what, when in p_dates:
        if when > today and dod:
            ctx.date(what, when, CMMC_PHASES, kind="milestone")
    if dod and applies is not False and required and phase == "before_phase_1":
        ctx.date("Award date: any CMMC level your contract named had to be met (before Phase 1)", award, CMMC_PHASES,
                 CMMC_7021, kind="deadline" if award >= today else "milestone")
    elif dod and applies is not False and required:
        ctx.date((f"Current {required} status and affirmation in SPRS, CMMC UIDs in the proposal" if prime else
                  f"Current {required} status and affirmation in SPRS before your subcontract award"),
                 award, CMMC_AWARD if prime else CMMC_7021, CMMC_CURRENT,
                 kind="deadline" if award >= today else "milestone")

    # ------------------------------------------------ SPRS score and a status you hold
    outcome = None
    if score is not None:
        if not cui:
            ctx.notes.append("sprs_score was ignored: Level 1 has no score (every requirement MET or NOT MET), and the "
                             "Level 2 score applies only to CUI.")
        else:
            mn, top = _min_conditional_score(), P(CMMC_L2, "max_score")
            outcome = "final" if score == top else ("conditional_possible" if score >= mn else "not_eligible")
            ctx.line("Your Level 2 score short of a Final status", top - score, "count",
                     f"{top} - {score} = {top - score} points open", CMMC_SCORING, CMMC_L2)
            ctx.notes.append({
                "final": f"A score of {top} means every requirement is MET: a Final Level 2 status.",
                "conditional_possible": (f"{score} >= {mn}: a Conditional status is possible only if every open item is a "
                                         "1-point item (or SC.L2-3.13.11 non-FIPS encryption) and none of the listed "
                                         f"requirements is open; close out within {P(CMMC_POAM, 'closeout_days')} "
                                         "days."),
                "not_eligible": (f"{score} < {mn}: no Level 2 status (Conditional needs score / {top} >= "
                                 f"{_n(P(CMMC_POAM, 'min_score_ratio'))}); fix items before the assessment."),
            }[outcome])
            if dod:
                # DFARS 252.204-7019(b) / 252.204-7020(a): the SPRS "summary level score" of a NIST SP 800-171 DoD
                # Assessment is a separate assessment under the DoD Assessment Methodology, not a CMMC status.
                ctx.use(CMMC_SCORING, DFARS_7019)
                ctx.notes.append("sprs_score was read as a CMMC Level 2 assessment score under 32 CFR 170.24. A NIST "
                                 "SP 800-171 DoD Assessment summary score posted in SPRS under DFARS 252.204-7019/-7020 "
                                 f"comes from the separate {P(DFARS_7019, 'scored_under').split(' (')[0]}; it does not "
                                 "give you a CMMC status, and this tool does not convert one score into the other.")
    meets = current = clash = None
    if status and not status_date:
        ctx.notes.append("current_cmmc_status was given without cmmc_status_date, so its renewal and affirmation dates "
                         "were not computed.")
    if status_date and not status:
        ctx.notes.append("cmmc_status_date was given without current_cmmc_status, so it was not used.")
    if status:
        level = status.split(" ", 1)[1]
        if required and dod:
            meets = LEVEL_RANK[level] >= LEVEL_RANK[required]
            if not meets:
                ctx.notes.append(f"Your {status} is below the required {required}; a higher status satisfies a "
                                 "lower requirement, never the reverse.")
        if status_date:
            current = _status_dates(ctx, status, status_date, last_aff, award)
            if not current:
                ctx.notes.append(f"Your {status} will not be current on the {award} award date unless renewed (age "
                                 "limit or yearly affirmation, DFARS 204.7501 'Current').")
        if score is not None and cui and level != LEVELS[0]:
            # A Final Level 2 needs every requirement MET (170.16(a)(1), 170.17(a)(1)) and Level 3 needs the maximum
            # Level 2 score (170.24(c)(3)); a Conditional Level 2 needs score / 110 >= 0.8 (170.21(a)(2)(i)).
            top, mn = P(CMMC_L2, "max_score"), _min_conditional_score()
            clash = None
            if status.startswith("Final") and score < top:
                clash = f"a {status} means every Level 2 requirement was MET ({top} of {top})"
            elif level == LEVELS[3] and score < top:
                clash = f"Level 3 rests on a Final Level 2 (C3PAO) with the maximum score of {top}"
            elif status.startswith("Conditional") and score < mn:
                clash = f"a {status} needs a Level 2 score of at least {mn}"
            if clash:
                current = None                  # the dates alone cannot show the status is still current
                ctx.use(CMMC_CURRENT, CMMC_POAM)
                ctx.notes.append(f"Your sprs_score {score} does not fit your status: {clash}. If the score is newer "
                                 "than the status, your compliance has changed since the CMMC Status Date, and a "
                                 f"status is current only with '{P(CMMC_CURRENT, 'condition')}' (DFARS 204.7501): "
                                 "fix the gaps and reassess before you rely on it.")
    ctx.dates.sort(key=lambda d: d.due)

    # ------------------------------------------------ summary and next step
    info_txt = {"cui": "CUI", "fci_only": "FCI only", "none": "no FCI or CUI"}[info]
    who = "prime contractor" if prime else f"tier-{tier} subcontractor"
    if not dod:
        head = f"Non-DoD federal contract, {who}, {info_txt}: no CMMC."
    elif applies is False:
        head = f"DoD contract, {who}, {info_txt}: no CMMC status needed for this award."
    else:
        lvl = required + (" at least" if minimum_only else "")
        head = (f"DoD contract awarded {award} ({phase.replace('_', ' ')}), {who}, {info_txt}: CMMC "
                f"{'applies' if applies else 'may apply'}, {lvl}"
                + (f"; plan for {plan}" if plan and plan != required else "") + ".")
    on = [r for r in ctx.reqs if r.applies]
    summary = head + f" {_count(len(on), 'requirement applies', 'requirements apply')}."
    soon = [d for d in ctx.dates if d.kind == "deadline" and d.status in ("overdue", "due_soon")]
    first = ""
    if soon:
        d = soon[0]
        first = f"OVERDUE since {d.due}: {d.what}. " if d.status == "overdue" else f"By {d.due}: {d.what}. "
    if not dod:
        step = ("Apply the 15 FAR 52.204-21 safeguards to every system holding contract information"
                + (" and ask the contracting officer which CUI clauses apply" if cui else "") + "; keep Kaspersky, "
                "TikTok and Section 889 equipment off the job." if info != "none" else
                "No CMMC or FAR 52.204-21 duties; keep Kaspersky, TikTok and Section 889 equipment off the job"
                + (" and keep SAM.gov current." if prime else "."))
    elif applies is False:
        step = ("Keep Kaspersky, TikTok and Section 889 equipment off the job" + (" and keep SAM.gov current" if prime
                                                                                 else "")
                + ("; apply the FAR 52.204-21 safeguards to systems holding contract information." if info != "none"
                   else "."))
    elif required == LEVELS[0]:
        step = (f"Self-assess the {P(CMMC_L1, 'requirement_count')} FAR 52.204-21 requirements on every system "
                f"holding FCI, post the result in SPRS and have your Affirming Official affirm before {award}.")
    else:
        lv = plan if (minimum_only and plan) else required
        n2 = P(CMMC_L2, "requirement_count")
        if minimum_only and phase == "before_phase_1":
            # DFARS 252.204-7025 and the NOV 2025 clause did not exist before Phase 1 (eCFR version dates)
            step = ("Check your contract for DFARS 252.204-7021 and the level it names. " if prime else
                    "Ask the prime whether its contract includes a CMMC clause and which level it names. ")
        elif minimum_only:
            step = ("Read the solicitation's DFARS 252.204-7025 paragraph (b)(1) for the level. " if prime else
                    "Ask the prime which CMMC level its contract requires. ")
        else:
            step = ""
        if lv == LEVELS[3]:
            # 170.18(a)(1): Final Level 2 (C3PAO) first; 170.24(c)(3): the maximum Level 2 score is required
            step += (f"Plan for {lv}: write the system security plan, reach a Final Level 2 (C3PAO) with an authorized "
                     f"C3PAO (book early) with all {n2} NIST SP 800-171 Rev 2 requirements MET, then the DCMA DIBCAC "
                     f"Level 3 assessment of the {P(CMMC_L3, 'requirement_count')} NIST SP 800-172 requirements")
        else:
            step += (f"Plan for {lv}: write the system security plan, assess all {n2} NIST SP 800-171 Rev 2 "
                     "requirements" + (" with an authorized C3PAO (book early)" if lv == LEVELS[2] else ""))
        step += (f", and keep a NIST SP 800-171 DoD Assessment no more than {P(DFARS_7019, 'max_age_years')} years old "
                 "in SPRS meanwhile.")
    if clash:
        step = f"Your sprs_score {score} conflicts with your {status}: resolve that first (see notes). " + step
    elif status and status_date and meets and required and applies is not False:
        if current is False:
            step = f"Your {status} will not be current on {award} unless renewed (see key_dates). " + step
        elif current and not minimum_only:
            step = (f"Your {status} meets the {required} requirement and is current on {award}: keep it current with "
                    "the renewal and affirmation dates in key_dates"
                    + (f", and keep a NIST SP 800-171 DoD Assessment no more than {P(DFARS_7019, 'max_age_years')} "
                       "years old in SPRS." if cui else "."))
    res = CmmcRequirementsResult(
        as_of=today.isoformat(), dod_contract=dod, your_role=role, subcontract_tier=tier, information=info,
        contract_award_date=award.isoformat(), cmmc_phase=phase,
        cmmc_phase_started_on=started.isoformat() if started else None,
        next_phase_starts_on=nxt.isoformat() if nxt else None, cmmc_applies=applies, required_cmmc_level=required,
        required_level_is_minimum_only=minimum_only, plan_for_cmmc_level=plan,
        assessment=_assessment_text(required) if required and applies is not False else None,
        nist_sp_800_171_revision_for_cmmc=("Revision 2 (32 CFR 170.2(a)(9)); Revision 3 is not adopted"
                                           if cui and dod else None),
        sprs_score_outcome=outcome, status_meets_required_level=meets, status_current_at_award=current,
        summary=summary, next_step=first + step, requirements=ctx.reqs, key_dates=ctx.dates, line_items=ctx.lines,
        notes=ctx.notes)
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
            "American / BABA / FHWA and FTA Buy America, "
            "the USDOT DBE program (goals paused since "
            f"{P(DBE_GOALS, 'effective')} pending re-evaluation), prompt payment, required clauses, federal paid "
            "sick leave, and California dual coverage (pay the higher of the federal and DIR rates, two payroll "
            "filings). Also the bond amounts, dated key steps (wage determination "
            f"{P(WD_BID, 'days_before_bid_opening')}-day rule, E-Verify enrollment, SF 1413, DBE deadlines), line "
            "items, a summary and one concrete next step. Thresholds the "
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
            "infrastructure), far_buy_american (FAR 52.225-9, a federal agency's own contract), fhwa_buy_america "
            "(23 CFR 635.410, federal-aid highway) or fta_buy_america (49 CFR 661.5, FTA transit: every component of "
            "a manufactured product of U.S. origin). FAA airport projects are refused: FAA's own terms are not in the "
            "rulebook. item_type: iron_steel, manufactured_product, "
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
            "alternate bid, FTA lowest foreign bid x "
            f"{_n(P(FTA_BA, 'price_differential_waiver_if_lowest_foreign_bid_times'))} vs lowest domestic). Returns complies true/false/null with what is missing, the math and a next step."),
        "cmmc": (
            "CMMC and federal cybersecurity rules for one contract you are bidding or performing. Give whether it is a "
            "DoD contract (USACE, NAVFAC, Air Force, Army, ...), whether your systems will hold CUI (incl. covered "
            "defense information) or FCI only, the award date and your subcontract tier (0 = prime). Optional: the "
            "CMMC level the solicitation or prime contract names (DFARS 252.204-7025), your Level 2 SPRS score, and a "
            "CMMC status you hold with its date. Returns the CMMC phase on the award date (Phase 1 from "
            f"{P(CMMC_PHASES, 'phase_1_start')}, Phase 2 from {P(CMMC_PHASES, 'phase_2_start')}, Phase 3 from "
            f"{P(CMMC_PHASES, 'phase_3_start')}, full implementation from {P(CMMC_PHASES, 'phase_4_start')}), "
            "cmmc_applies true/false/null, the level you must hold (subcontractors per 32 CFR 170.23: FCI only = "
            "Level 1 (Self), CUI = Level 2 (Self), or Level 2 (C3PAO) under a C3PAO or Level 3 prime), what to plan "
            f"for when the level is unknown, the assessment (Level 1: {P(CMMC_L1, 'requirement_count')} FAR "
            f"52.204-21 requirements yearly, no POA&M; Level 2: {P(CMMC_L2, 'requirement_count')} NIST SP 800-171 "
            f"Rev 2 requirements every {P(CMMC_L2_SELF, 'reassessment_interval_years')} years, Conditional status "
            f"from a score of {_min_conditional_score()} with a {P(CMMC_POAM, 'closeout_days')}-day POA&M closeout; "
            f"Level 3: {P(CMMC_L3, 'requirement_count')} NIST SP 800-172 requirements by DCMA DIBCAC), yearly "
            "affirmations and CMMC UIDs in SPRS, DFARS 252.204-7012 NIST SP 800-171 and "
            f"{P(DFARS_INCIDENT, 'report_within_hours')}-hour DIBNet incident reporting, the "
            f"{P(DFARS_7019, 'max_age_years')}-year NIST SP 800-171 DoD Assessment in SPRS, FAR 52.204-21, Section "
            "889, Kaspersky and TikTok bans, SAM.gov representations, renewal dates and a next step. Not legal "
            "advice; the solicitation's own CMMC level controls."),
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
            "baba, far_buy_american, fhwa_buy_america or fta_buy_america (aliases: BABA, FAR, Buy American, FHWA, "
            "Caltrans federal-aid, FTA)"))],
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
            "lowest domestic-steel total bid; FTA lowest bid offering the U.S. item"))] = None,
        cost_if_foreign_usd: Annotated[OptMoney, Field(description="The same figure with foreign items")] = None,
        stafford_act_disaster_funds: Annotated[bool, Field(description="BABA: FEMA Stafford Act money (exempt)")] = False,
        as_of: Annotated[Optional[str], Field(description="ISO date; default today in California")] = None,
    ) -> DomesticContentResult:
        return buy_america_check(program, item_type, item_name, components, domestic_component_cost_usd,
                                 total_component_cost_usd, foreign_iron_steel_component_cost_usd, manufactured_in_us,
                                 all_manufacturing_in_us, delivery_date, cots, critical_item, total_contract_cost_usd,
                                 contract_foreign_iron_steel_cost_usd, fhwa_obligation_date, cost_if_domestic_usd,
                                 cost_if_foreign_usd, stafford_act_disaster_funds, as_of)

    top = P(CMMC_L2, "max_score")

    @mcp.tool(name="cmmc_requirements", annotations=ro, description=docs["cmmc"])
    def _cmmc_requirements(
        handles_cui: Annotated[bool, Field(description=(
            "Will your systems process, store or transmit CUI for this contract (e.g. CUI-marked drawings or specs, "
            "controlled technical information)?"))],
        handles_fci_only: Annotated[bool, Field(description=(
            "FCI but no CUI: non-public information provided by or generated for the government under the contract "
            "(not public or simple payment information)"))],
        dod_contract: Annotated[bool, Field(description=(
            "Is the prime contract with the Department of Defense (USACE, NAVFAC, Army, Air Force, DLA, ...)?"))],
        contract_award_date: Annotated[Optional[str], Field(description=(
            "ISO award date of the contract you are signing (subcontractors: your subcontract); sets the CMMC "
            "phase. Default: today in California"))] = None,
        subcontract_tier: Annotated[Union[int, str], Field(description=(
            "0 = prime contractor, 1 = first-tier subcontractor, 2 = second tier, ..."))] = 0,
        solicitation_cmmc_level: Annotated[Optional[str], Field(description=(
            "CMMC level the solicitation or prime contract names (DFARS 252.204-7025(b)(1)): 'Level 1 (Self)', "
            "'Level 2 (Self)', 'Level 2 (C3PAO)', 'Level 3 (DIBCAC)', or 'none'. Leave empty if unknown"))] = None,
        sprs_score: Annotated[Optional[int], Field(le=top, description=(
            f"Your CMMC Level 2 assessment score under 32 CFR 170.24 (out of {top}; may be negative) to test Final / "
            "Conditional eligibility. A NIST SP 800-171 DoD Assessment score (DFARS 252.204-7019) is a different "
            "assessment"))] = None,
        current_cmmc_status: Annotated[Optional[str], Field(description=(
            "A CMMC status you hold, e.g. 'Final Level 1 (Self)', 'Conditional Level 2 (Self)', 'Final Level 2 "
            "(C3PAO)'"))] = None,
        cmmc_status_date: Annotated[Optional[str], Field(description=(
            "ISO CMMC Status Date shown in SPRS: the date the results were submitted to SPRS or eMASS. For a Final "
            "status reached by closing out a POA&M, this is the earlier Conditional status date (32 CFR 170.4: no "
            "new date is set), not the closeout date"))] = None,
        last_affirmation_date: Annotated[Optional[str], Field(description=(
            "ISO date of your Affirming Official's last affirmation in SPRS"))] = None,
        as_of: Annotated[Optional[str], Field(description="ISO date to evaluate as of; default today in California")] = None,
    ) -> CmmcRequirementsResult:
        return cmmc_requirements(handles_cui, handles_fci_only, dod_contract, contract_award_date, subcontract_tier,
                                 solicitation_cmmc_level, sprs_score, current_cmmc_status, cmmc_status_date,
                                 last_affirmation_date, as_of)
