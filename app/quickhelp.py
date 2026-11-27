"""Quick help: the helper built into TradeComply (docs/quick-help.md). POST /api/help.

Deterministic, inside this server, no model and no outside call. It matches the question to a check, a rule, a page
or a kind of licensed professional with the construction vocabulary, fixed intent patterns and the search index
(app/search.py), pulls out the facts it can (dates, amounts, feet, AQI, city, county, role, the company's own job
and sub names), and answers only with:
  - rulebook text, verbatim, with citation, official source, verification status and the verifier's correction;
  - a professionals.json requirement with its legal basis, only when one of the type's when_required situations
    fits the question better than every listed exception;
  - fixed sentences (NOTES, LEADS below) and app actions: open a check with fields filled in (the person presses
    Run), go to a page, find a professional, or hand the question to Ask TradeComply (Claude) when it is available.
It never runs a check, never computes a deadline, never says a licensed professional can be skipped, and stores
nothing: no table, no activity row; the log has the intent and counts at DEBUG, never the question.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from tradecomply import core
from tradecomply import search as engine
from tradecomply.logic import CA_TZ

from . import catalog
from . import search as searchmod
from .auth import Principal, people_only, platform_of

log = logging.getLogger("tradecomply.quickhelp")
router = APIRouter(prefix="/api", tags=["quickhelp"])

MAX_READ = 500

# ---------------------------------------------------------------------------- fixed copy (spec 5.9)
LEADS = {
    "LEAD_TOOL": "This check works out your answer. These rules are about your question.",
    "LEAD_RULES": "These rules cover it. The check works out your dates and amounts.",
    "LEAD_RULES_ONLY": "These rules match your question.",
    "LEAD_NAV": "That's in TradeComply here:",
    "LEAD_PRO": "Here is when the law requires {a_name}, and where the state lists them. Whether your job needs one "
                "depends on its details.",
    "LEAD_PRO_FIND": "The state lists licensed {names} here. Whether your job needs one depends on its details.",
    # Kinds of professional the state does not list as licensed (a competent person, the building department, a
    # HERS rater...): the same lines without "licensed" / "the state lists".
    "LEAD_PRO_OTHER": "Here is when the law requires {a_name}, and how to find one. Whether your job needs one "
                      "depends on its details.",
    "LEAD_PRO_FIND_OTHER": "The Find a professional page shows how to find and check {a_name}. Whether your job "
                           "needs one depends on its details.",
    "LEAD_CHOICES": "Do you mean one of these?",
    "LEAD_ROUTE": "",
    "LEAD_NOT_FOUND": "I couldn't find a check or rule for that in TradeComply.",
}
NOTES = {
    "NOT_FOUND": "I couldn't find a check or rule for that in TradeComply.",
    "NOT_FOUND_TRY": "Try other words, or:",
    "GUARD": "TradeComply can't help skip or work around a requirement. A licensed professional can tell you what "
             "your job needs.",
    "ROUTE_SUE": "Whether to sue, and how, is legal advice. An attorney decides that with you.",
    "ROUTE_DRAFT": "Writing a letter or contract text is not something Quick help does.",
    "ROUTE_EMPLOYEE": "Whether someone is an employee or an independent contractor depends on the facts of the work. "
                      "An attorney can tell you.",
    "ROUTE_FIRE": "Firing or disciplining someone is an employment-law decision for you and an attorney.",
    "ROUTE_FACTS": "That needs someone to read your documents and the facts.",
    "ROUTE_STATE": "TradeComply's rulebook covers California only.",
    "ROUTE_TAX": "Tax questions are for a CPA.",
    "ROUTE_DOCS": "Quick help can't read your documents.",
    "ROUTE_JUDGMENT": "That's a judgment call, not a rule lookup.",
    "CLAUDE_CAN": "Ask TradeComply (Claude) can talk it through. It sends your question to Anthropic and counts "
                  "toward today's questions.",
    "CLAUDE_OFF": "Ask TradeComply (Claude) isn't available here.",
    "MAYBE_PRO": "Run the check: its result says whether the law requires this person here.",
    "OPEN_TOOL_HINT": "Opens the check with these filled in. Nothing runs until you press {button}.",
    "MISSING": "Still needed in the check: {labels}.",
    "READ_DATE": "I read “{words}” as {date}. Change it in the check if that's wrong.",
    "UNVERIFIED_SOME": "TradeComply hasn't checked {n} of these rules against the official source yet. Read the "
                       "source before you rely on it.",
    "UNCONFIRMED_SOME": "TradeComply hasn't confirmed {n} of these requirements against the official source yet. "
                        "Read the legal basis before you rely on it.",
    "INDEX_WARMING": "Quick help is starting up. Try again in a few seconds, or use Search.",
    "TOO_LONG": "Quick help read the first 500 characters.",
    "ROLE_OFFICE": "Your office does that in TradeComply.",
    "ROLE_OWNER": "Your account owner does that in TradeComply.",
    "SUBSCRIBE": "Rule text needs an active subscription.",
}
ASK_LABEL = "Ask TradeComply (Claude)"

# How each kind of professional is named in a sentence; True = the state lists them as licensed (LEAD_PRO_FIND).
PRO_NOUNS = {
    "attorney": ("an attorney", "attorneys", True),
    "notary_public": ("a notary public", "notaries public", False),
    "professional_engineer": ("a professional engineer", "professional engineers", True),
    "land_surveyor": ("a land surveyor", "land surveyors", True),
    "geologist": ("a geologist", "geologists", True),
    "architect": ("an architect", "architects", True),
    "landscape_architect": ("a landscape architect", "landscape architects", True),
    "qsd_qsp": ("a QSD or QSP", "QSDs and QSPs", False),
    "hers_rater": ("a HERS rater", "HERS raters", False),
    "dsa_project_inspector": ("a DSA-certified project inspector", "DSA-certified project inspectors", False),
    "asbestos_consultant": ("a certified asbestos consultant", "certified asbestos consultants", False),
    "lead_professional": ("a CDPH-certified lead professional", "CDPH-certified lead professionals", False),
    "licensed_contractor": ("a licensed contractor", "contractors", True),
    "cslb_qualifier": ("a CSLB qualifying individual", "CSLB qualifying individuals", False),
    "insurance_agent_broker": ("an insurance agent or broker", "insurance agents and brokers", True),
    "cpa": ("a CPA", "CPAs", True),
    "crane_operator": ("a certified crane operator", "certified crane operators", False),
    "crane_certifier": ("a crane certifier", "crane certifiers", True),
    "apprenticeship_committee": ("an apprenticeship committee", "apprenticeship committees", False),
    "certified_electrician": ("a DIR-certified electrician", "DIR-certified electricians", False),
    "building_department": ("the building department", "building departments", False),
    "special_inspector": ("a special inspector", "special inspectors", False),
    "casp": ("a Certified Access Specialist (CASp)", "Certified Access Specialists", False),
    "competent_person": ("a competent person", "competent persons", False),
}

ROLE_DISPLAY = {"supplier": "Material supplier", "subcontractor": "Subcontractor",
                "direct_contractor": "Direct contractor (prime)", "equipment_lessor": "Equipment lessor",
                "design_professional": "Design professional", "laborer": "Laborer"}

# ---------------------------------------------------------------------------- route check (spec 5.4 step 2)
_REQ = (r"(?:permits?|licen[cs]es?|engineers?|stamps?|inspections?|inspectors?|respirators?|masks?|prelims?|"
        r"notices?|bonds?|insurance|workers'? ?comp|fall protection|shoring|competent person|certified payroll|"
        r"prevailing wage|casp|hers|swppp)")
GUARD = re.compile(
    r"\bskip(?:s|ped|ping)?\b|\bavoid(?:ing)? (?:the|a|an|getting|paying|pulling|needing)\b|\bget(?:ting)? out of\b"
    r"|\bget(?:ting)? around\b|\bwork(?:ing)? around\b|\bworkaround\b|\bloophole"
    r"|\b(?:work|build|start|operate|dig|do the job)\w* without (?:a |an |the |getting |pulling )?" + _REQ +
    r"|\bdo (?:we|i) really (?:have|need) to\b|\bis there (?:a|any) way (?:around|to avoid|not to)\b"
    r"|\bhow (?:do|can) (?:i|we) (?:not|avoid|skip|get out of|get around)\b"
    r"|\bdon'?t want to (?:pull|get) (?:a |the )?permit")
ROUTES = [   # (code, pattern, professional to find)
    ("SUE", r"\b(?:should|can) (?:i|we) (?:sue|take .{1,40} to court)\b|\bsue\b|\blawsuit\b|"
            r"\bmy (?:case|claim) (?:worth|win)", "attorney"),
    ("DRAFT", r"\b(?:write|draft|prepare)\b (?:me |us )?(?:a |an |the )?(?:\w+ ){0,2}"
              r"(?:letter|demand|clause|response|reply|email|contract)\b", None),
    ("FIRE", r"\bcan (?:we|i) (?:fire|terminate|discipline|demote|suspend)\b|\bshould (?:we|i) fire\b", "attorney"),
    ("EMPLOYEE", r"employee or (?:an )?independent contractor|independent contractor or (?:an )?employee|"
                 r"really an? (?:independent contractor|employee)\b|misclassif", "attorney"),
    ("FACTS", r"\bis (?:this|it|he|she|my|our|the) (?:\w+ ){0,3}(?:legal|enforceable)\b|\bwhat are my rights\b|"
              r"\binterpret\b|\bwho is (?:at fault|liable|responsible for the damage)\b|\bliable\b|\bat fault\b",
     "attorney"),
    ("STATE", r"\b(?:nevada|arizona|oregon|texas|washington state|new york|florida|another state|out of state law|"
              r"other states?)\b", None),
    ("TAX", r"\b(?:tax return|deduct\w*|depreciat\w*|write ?off|s[- ]?corp|taxes on|income tax|sales tax)\b", "cpa"),
    ("DOCS", r"\b(?:compare|summari[sz]e|read|review|check) (?:my|this|the attached|these|our) (?:\w+ )?"
             r"(?:contract|document|subcontract|plans|agreement|lease|invoice)s?\b", None),
    ("JUDGMENT", r"\b(?:what would you do|which is better|negotiat\w*|how much should (?:i|we) (?:charge|bid)|"
                 r"should (?:i|we) (?:bid|take the job|hire)\b)", None),
]
ROUTES = [(code, re.compile(rx), pro) for code, rx, pro in ROUTES]
NAMED_PRO = [   # a kind of professional the question names, longest names first
    (re.compile(r"\blandscape architect"), "landscape_architect"),
    (re.compile(r"\basbestos consultant"), "asbestos_consultant"),
    (re.compile(r"\bspecial (?:or deputy )?inspector|\bdeputy inspector"), "special_inspector"),
    (re.compile(r"\bcrane certifier"), "crane_certifier"),
    (re.compile(r"\bcrane operator"), "crane_operator"),
    (re.compile(r"\bhers (?:rater|test|verification)"), "hers_rater"),
    (re.compile(r"\bcompetent person"), "competent_person"),
    (re.compile(r"\binspector of record|\bdsa inspector|\bproject inspector"), "dsa_project_inspector"),
    (re.compile(r"\bcertified electrician|\bdir.certified electrician|\belectrician certification"),
     "certified_electrician"),
    (re.compile(r"\bbuilding (?:department|official|inspector)"), "building_department"),
    (re.compile(r"\binsurance (?:agent|broker)|\bsurety"), "insurance_agent_broker"),
    (re.compile(r"\bnotary"), "notary_public"),
    (re.compile(r"\battorney|\blawyer"), "attorney"),
    (re.compile(r"\bengineer"), "professional_engineer"),
    (re.compile(r"\barchitect"), "architect"),
    (re.compile(r"\bsurveyor"), "land_surveyor"),
    (re.compile(r"\bgeologist"), "geologist"),
    (re.compile(r"\bcpa\b|\baccountant"), "cpa"),
    (re.compile(r"\bqsd\b|\bqsp\b"), "qsd_qsp"),
    (re.compile(r"\bcasp\b|\bcertified access specialist"), "casp"),
]
GUARD_PRO = [   # a professional the GUARD question names (for its one find action)
    (re.compile(r"\bengineer|\bpe stamp|\bstamped (?:plans|calcs|drawings)"), "professional_engineer"),
    (re.compile(r"\barchitect"), "architect"),
    (re.compile(r"\battorney|\blawyer"), "attorney"),
    (re.compile(r"\bsurveyor"), "land_surveyor"),
    (re.compile(r"\bcasp\b"), "casp"),
    (re.compile(r"\bhers\b"), "hers_rater"),
    (re.compile(r"\bqsd|\bqsp|\bswppp"), "qsd_qsp"),
    (re.compile(r"\bspecial inspect"), "special_inspector"),
    (re.compile(r"\basbestos consultant"), "asbestos_consultant"),
    (re.compile(r"\bcompetent person"), "competent_person"),
]

# ---------------------------------------------------------------------------- navigation (spec 5.4 step 3)
ACTION_WORDS = re.compile(r"\bhow (?:do|can|to) (?:i|we)?\b|\bhow to\b|\bwhere (?:is|are|do|can|does)\b|\badd\b|"
                          r"\bimport\b|\bupload\b|\binvite\b|\bchange\b|\bconnect\b|\bsave\b|\bprint\b|"
                          r"\bset ?up\b|\bopen\b|\bcancel\b|\bturn on\b|\bturn off\b|\bdownload\b|\bexport\b")
NAV = [   # (pattern, page id in catalog.PAGES); first match wins
    (r"\b(?:upload|import)\b.*\b(?:subs?|subcontractors?|sub list|list|csv|spreadsheet)\b|\bsub list\b",
     "subs_import"),
    (r"\badd\b.*\b(?:sub|subs|subcontractors?)\b", "subs_add"),
    (r"\b(?:add|new|create)\b.*\b(?:project|job)\b", "project_new"),
    (r"\b(?:invite|add)\b.*\b(?:users?|person|people|foreman|office|crew|team|someone|members?|employees?|"
     r"super(?:intendent)?)\b", "settings-team"),
    (r"\b(?:subscription|billing|invoices?|payment card|credit card|my plan)\b", "settings-billing"),
    (r"\bpassword\b", "settings-password"),
    (r"\b(?:dark mode|light mode|display|theme|contrast)\b", "settings-display"),
    (r"\b(?:api keys?|mcp|connect claude|ai assistant)\b", "settings-mcp"),
    (r"\b(?:export|download)\b.*\b(?:data|everything|records)\b|\bdelete (?:my |our |the )?(?:company|account)\b",
     "settings-data"),
    (r"\bactivity log\b|\baudit log\b", "settings-activity"),
    (r"\b(?:company name|license number on (?:my|our) account)\b", "settings-company"),
    (r"\b(?:calendars?|reminders?|google calendar|outlook|ical|ics)\b", "calendar"),
    (r"\b(?:text alerts?|sms|text messages?)\b", "integrations:Twilio"),
    (r"\b(?:toolbox|tailgate|sign.?in sheets?|daily logs?|field logs?|inspection logs?|incident (?:logs?|reports?))\b",
     "field"),
    (r"\b(?:save|print)\b.*\b(?:results?|documents?|pdf|reports?)\b|\bmy documents\b|\bsafety programs?\b|"
     r"\bsaved results?\b", "documents"),
    (r"\bguided setup\b|\bgetting started\b", "welcome"),
]
NAV = [(re.compile(rx), page) for rx, page in NAV]
NAV_EXTRA_PAGES = {   # nav targets that are not search pages of their own
    "subs_add": {"id": "subs_add", "title": "Add a subcontractor", "href": "#/subs", "roles": catalog.OFFICE_UP},
    "settings-password": {"id": "settings-password", "title": "Change password",
                          "href": "#/settings?section=account", "roles": catalog.ALL},
}

# ---------------------------------------------------------------------------- slots (spec 5.4 step 4)
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_FUTURE_WORDS = re.compile(r"\b(?:will|going to|next|upcoming|tomorrow|planning|plan to|scheduled|starting on|"
                           r"start on)\b")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def slots(q: str, today: date, counties: list[str], cities: list[dict], projects: list[dict],
          subs: list[dict]) -> dict:
    t = q.lower()
    out: dict = {}
    m = re.search(r"\b(?:lic(?:ense)?\.?\s*(?:#|no\.?|number)?\s*)?(\d{6,8})\b", t)
    if m and re.search(r"lic|cslb|#", t):
        out["license_number"] = m[1]
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|million)?\b|\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|grand|million)\b",
                  t)
    if m:
        v = _num(m[1] or m[3])
        mult = m[2] or m[4] or ""
        out["money_usd"] = v * (1000 if mult in ("k", "thousand", "grand") else 1_000_000
                                if mult in ("m", "million") else 1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", t)
    if m:
        out["percent"] = float(m[1])
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ft|feet|foot|')(?!\s*(?:sq|square))", t)
    if m:
        out["feet"] = float(m[1])
    m = re.search(r"(\d[\d,]*)\s*(?:sq\.?\s*ft|square\s*f(?:ee|oo)t|sf)\b", t)
    if m:
        out["sqft"] = _num(m[1])
    m = re.search(r"(\d+(?:\.\d+)?)\s*acres?\b", t)
    if m:
        out["acres"] = float(m[1])
    m = re.search(r"\baqi\D{0,12}(\d{2,3})|(\d{2,3})\s*aqi", t)
    if m:
        out["aqi"] = int(m[1] or m[2])
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", t)
    if m and 1 <= int(m[1]) <= 12:
        h = int(m[1]) % 12 + (12 if m[3].startswith("p") else 0)
        out["time"] = f"{h:02d}:{int(m[2] or 0):02d}"
    days = re.findall(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if days:
        wd = WEEKDAYS.index(days[0])
        out["date_weekday"] = (today + timedelta(days=(wd - today.weekday()) % 7)).isoformat()
    dates, read = [], []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            dates.append(date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", t):
        y = int(m[3]) + (2000 if int(m[3]) < 100 else 0)
        try:
            dates.append(date(y, int(m[1]), int(m[2])))
        except ValueError:
            pass
    future = bool(_FUTURE_WORDS.search(t))
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
                         r"(?:,?\s+(\d{4}))?\b", t):
        try:
            y = int(m[3]) if m[3] else today.year
            d = date(y, MONTHS[m[1]], int(m[2]))
        except ValueError:
            continue
        if not m[3]:
            if d > today and not future:
                d = date(y - 1, d.month, d.day)          # "Nov 3" asked later in November: this past Nov 3
            elif d < today and future:
                d = date(y + 1, d.month, d.day)
            read.append((q[m.start():m.end()], d))
        dates.append(d)
    if "yesterday" in t:
        dates.append(today - timedelta(days=1))
    if re.search(r"\btoday\b", t):
        dates.append(today)
    if re.search(r"\btomorrow\b", t):
        dates.append(today + timedelta(days=1))
    if dates:
        out["dates"] = [d.isoformat() for d in dates]
    if read:
        out["read_dates"] = read
    for c in counties:
        cl = c.lower()
        if re.search(rf"\b{re.escape(cl)}\s+county\b|\bcounty of {re.escape(cl)}\b", t) or (
                re.search(rf"\b{re.escape(cl)}\b", t) and cl not in _AMBIGUOUS_COUNTIES):
            out["county"] = c
            break
    for c in cities:
        if any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", t) for a in c["aliases"] if len(a) > 3) or any(
                re.search(rf"\b{re.escape(a.upper())}\b", q) for a in c["aliases"] if len(a) <= 3):
            out["city"] = c["name"]
            out["city_key"] = c["key"]
            break
    ft = engine.fold(q)
    for sb in subs:
        n = engine.fold(sb["name"])
        if len(n) >= 3 and n in ft and re.search(rf"(?<!\w){re.escape(n)}(?:'?s)?(?!\w)", ft):
            out["sub"] = sb
            if sb.get("license_number"):
                out.setdefault("license_number", re.sub(r"\D", "", sb["license_number"]))
            break
    for p in projects:
        n = engine.fold(p["name"])
        if len(n) >= 3 and n in ft and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", ft):
            out["project"] = p
            break
    for rx, r in ((r"\b(?:supplier|material (?:yard|supplier)|lumber yard|supply house)\b|\bdeliver(?:ed|ing|y)? "
                   r"(?:the |our |my )?(?:lumber|materials?|concrete|rebar|steel|supplies|drywall|pipe)", "supplier"),
                  (r"\b(?:equipment rental|rental company|rented (?:the )?equipment|equipment lessor)\b",
                   "equipment_lessor"),
                  (r"\b(?:sub|subcontractor|i'?m a sub|as a sub)\b", "subcontractor"),
                  (r"\b(?:gc|general contractor|prime|direct contractor|contract with the owner)\b",
                   "direct_contractor"),
                  (r"\b(?:laborer|worker on the job)\b", "laborer"),
                  (r"\b(?:architect|engineer|designer)\b", "design_professional")):
        if re.search(rx, t):
            out["role"] = r
            break
    if re.search(r"\b(?:6[5-9]|[7-9]\d)\s*(?:years?|yo|yr)|\b(?:is|are|age|aged)\s+(?:6[5-9]|[7-9]\d)\b|"
                 r"\b(?:6[5-9]|[7-9]\d)-year-old|\bsenior|\belderly|\bover 65|\b65 or older", t):
        out["senior"] = True
    cls = re.findall(r"\b([abcd])-?(\d{1,2})\b", t)
    if cls:
        out["classes"] = [f"{a.upper()}-{int(b)}" for a, b in cls if not (a in "ab" and int(b) > 2)]
    m = re.search(r"\b(1[89]\d\d|20[0-2]\d)s?\b", t)
    if m:
        out["year"] = int(m[1])
    return out


_AMBIGUOUS_COUNTIES = {"orange", "lake", "sierra", "nevada", "glenn", "inyo", "kings", "alpine", "del norte",
                       "plumas", "napa", "marin", "yuba", "sutter", "san diego", "los angeles", "san francisco",
                       "sacramento", "fresno"}       # cities, words or states as well: need "County" to count


# ---------------------------------------------------------------------------- intents (spec 5.4 step 5)
# (name, pattern, check) in priority order (an exact tie goes to the earlier one).
INTENTS = [
    ("find_pro", r"\b(?:find|need|hire|get|who can|where do i get|where can i find)\b.{0,60}\b(?:engineer|attorney|"
                 r"lawyer|notary|surveyor|architect|hers rater|qsd|qsp|casp|asbestos consultant|special inspector|"
                 r"crane certifier|geologist|cpa|accountant|landscape architect)", "find_licensed_professional"),
    ("license_lookup", r"\b\d{6,8}\b.*\b(?:lic|cslb|active|valid|check|good)|\b(?:check|look ?up|verify|is)\b.*"
                       r"\blicense\b.*\d{6,8}|license (?:is )?(?:still )?(?:active|valid|current|expired|suspended|"
                       r"good)|\bcheck (?:a |the |their |his |her )?(?:contractor'?s? )?license\b",
     "verify_contractor_license"),
    ("workers_comp", r"workers'? ?comp|\bwork comp\b|\bcomp (?:insurance|exemption|certificate)\b",
     "check_subcontractor_compliance"),
    ("classification", r"\bwhat (?:license|class|classification)|\bwhich (?:license|class)|\blicense (?:do|would|does)"
                       r" (?:i|we|they|an?|my) |\bneed (?:a|which) license\b|\bcan (?:a|my|our|the) (?:[abcd]-?\d{0,2}"
                       r"|electrician|plumber|roofer|gc|b)\b|\b[cbd]-?\d{1,2} (?:do|install|work)|classification "
                       r"for|\blicense for (?!#?\d)|\b(?:my|our|a) b (?:license|contractor)\b",
     "classification_for_scope"),
    ("stop_notice", r"stop (?:payment )?notice|\bspn\b", "stop_payment_notice_deadline"),
    ("lien", r"\blien\b|\bprelim|preliminary notice|20.?day notice", "calculate_lien_deadline"),
    ("cancel", r"\bcancel", "home_improvement_cancellation_deadline"),
    ("down_payment", r"down ?payment|\bdeposit\b|up ?front|before the work is done|payment ahead",
     "validate_home_improvement_contract"),
    ("retention", r"retention|retainage", "retention_cap_check"),
    ("prompt_pay", r"(?:hasn'?t|haven'?t|not been|didn'?t|never|won'?t) (?:been )?paid|\bpay(?:ing)? (?:my |the |our )?"
                   r"subs?\b|late pay|prompt pay|days to pay|have to pay me|still owes|owes (?:me|us)|"
                   r"(?:how fast|how long|how many days).{0,40}\bpay (?:me|us|my|the)\b",
     "prompt_payment_check"),
    ("osha_permit", r"(?:trench|excavat|ditch|hole|scaffold|falsework|demoli|tower crane).*(?:permit|osha|deep)|"
                    r"(?:permit|osha).*(?:trench|excavat|scaffold|falsework|demolition|tower crane)",
     "check_cal_osha_permits"),
    ("final_pay", r"final (?:pay)?check|last (?:pay)?check|final pay\b|\bfired?\b|\bquit\b|laid (?:a \w+ )?off|"
                  r"\blayoff\b|\blet (?:him|her|them|someone) go\b", "final_paycheck_deadline"),
    ("injury", r"injur|got hurt|accident|hospital|amputat|fatal|\bdied\b|\bdeath\b|\ber\b|emergency room|stitches|"
               r"admitted|kept overnight|ambulance|log 300|300 log|osha 300", "injury_reporting_deadlines"),
    ("smoke", r"\bsmoke|\baqi\b|air quality", "wildfire_smoke_requirements"),
    ("valley_fever", r"valley fever|cocci", "valley_fever_requirements"),
    ("heat", r"\bheat\b|\bshade\b|hot (?:day|weather|out)|\bdegrees\b|\bit'?s hot\b|drinking water",
     "generate_safety_programs"),
    ("toilets", r"toilet|porta|restroom|sanitation|hand ?wash|washing station", "generate_safety_programs"),
    ("toolbox", r"\btoolbox|\btailgate|safety meeting|written (?:injury|safety) (?:and illness )?(?:prevention )?"
                r"program|\biipp\b", "generate_safety_programs"),
    ("hours", r"(?:what time|how early|\bstart|\bwork\b|noise|pour|run\b|jackhammer).*(?:\bam\b|\bpm\b|a\.m|p\.m|"
              r"sunday|saturday|holiday|\bnight\b|early|\bhours\b|weekend|weekday)|construction hours|"
              r"noise (?:ordinance|rules)|work hours", "construction_hours"),
    ("min_wage", r"minimum wage|min wage", "jobsite_minimum_wage"),
    ("sick", r"sick (?:leave|days|time|pay)", "sick_leave_status"),
    ("overtime", r"overtime|meal (?:break|period)|rest break|lunch break|double time", "check_payroll"),
    ("public_works", r"prevailing wage|public works|dir registration|certified payroll|\becpr\b|pwc.?100",
     "public_works_readiness"),
    ("apprentice", r"apprentic|das ?14[02]", "apprentice_hours_required"),
    ("adu", r"\badu\b|granny|jadu|in-law unit|casita|accessory dwelling", "adu_rules"),
    ("energy", r"title ?24|\bhers\b|energy code|cf[123]r", "energy_compliance_triggers"),
    ("stormwater", r"swppp|stormwater|storm water|\bacres?\b|erosion control", "calgreen_and_stormwater"),
    ("lead", r"lead paint|lead-based|lead safe|\blead\b|\brrp\b|pre-?1978|before 1978", "certifications_required"),
    ("asbestos", r"asbestos|popcorn|\bacm\b", "check_cal_osha_permits"),
    ("building_permit", r"(?:need|require|pull|get)\b.*\bpermit|permit (?:for|required|needed)|without a permit|"
                        r"\bpermit to\b", "permit_required"),
    ("safety", r"fall protection|harness|tie.?off|guardrail|ladder|scaffold|silica|trench|excavat|shoring|"
               r"concrete cut|cut-off saw|grinding", "lookup_safety_standard"),
    ("unlicensed", r"handyman|unlicensed|no license|under \$?1,?000|without a license|contracting without",
     "unlicensed_contracting_exposure"),
    ("setup", r"before (?:we|i) (?:start|break ground)|break(?:ing)? ground|new job checklist|pre-?construction|"
              r"set ?up (?:a|the|this|my) (?:job|project)|\b811\b|dig ?alert|call before you dig|usa north",
     "project_setup_checklist"),
    ("offroad", r"\bcarb\b|off.?road|diesel fleet|\btier [0-4]\b|\bdoors (?:reporting|system)|renewable diesel",
     "offroad_fleet_obligations"),
    ("certs", r"\bcertif(?:ied|ication)\b.{0,40}\b(?:electrician|crane|blaster|hoist|welder)|"
              r"\b(?:electrician|crane operator)s? (?:need|needs|must be) (?:to be )?certif",
     "certifications_required"),
]
# Patterns that keep an intent out when another, more specific one is the question.
EXCLUDE = {
    "safety": re.compile(r"\bpermit|\breport|\binjur|\bhospital|\bcarb\b|off.?road|\bfleet\b"),
    "injury": re.compile(r"illness prevention|\biipp\b"),
    "public_works": re.compile(r"apprentic"),
    "lead": re.compile(r"\bcertif(?:ied|ication)\b.{0,40}\belectrician"),
}
INTENTS = [(name, re.compile(rx), tool) for name, rx, tool in INTENTS]
INTENT_TOOL = {name: tool for name, _rx, tool in INTENTS}
INTENT_ORDER = {name: i for i, (name, _rx, _t) in enumerate(INTENTS)}
TOOL_INTENT: dict[str, str] = {}
for _name, _rx, _tool in INTENTS:
    TOOL_INTENT.setdefault(_tool, _name)

# Search words that help each intent find its rules (added to the question for the rule search).
RULE_QUERY = {
    "license_lookup": "verify contractor license status", "workers_comp": "workers comp exemption no employees",
    "classification": "license classification scope", "stop_notice": "stop payment notice deadline",
    "lien": "lien preliminary notice deadline", "cancel": "home improvement right to cancel",
    "down_payment": "home improvement down payment", "retention": "retention cap",
    "prompt_pay": "prompt payment progress payment subcontractor", "osha_permit": "cal/osha permit",
    "final_pay": "final pay deadline", "injury": "serious injury report 8 hours", "smoke": "wildfire smoke aqi",
    "valley_fever": "valley fever", "heat": "heat illness shade water", "toilets": "toilets employees",
    "toolbox": "toolbox tailgate meetings", "hours": "construction hours noise", "min_wage": "minimum wage",
    "sick": "paid sick leave", "overtime": "overtime meal period rest period",
    "public_works": "public works prevailing wage dir registration", "apprentice": "apprentice ratio hours",
    "adu": "adu", "energy": "energy hers", "stormwater": "stormwater construction general permit 1 acre",
    "lead": "lead renovation rrp pre-1978", "asbestos": "asbestos registration 100 sqft",
    "building_permit": "building permit exempt", "safety": "safety standard",
    "unlicensed": "minor work exemption 1000 unlicensed", "setup": "project setup",
    "offroad": "off-road diesel fleet", "certs": "certification required",
}
PICK_HINT = re.compile(r"\s*\(([^()]{3,80})\)\s*$")       # "... (Calculate lien deadline)": a "Do you mean" pick


# ---------------------------------------------------------------------------- the answer
class Ctx:
    def __init__(self, *, today: date, role: str, page: str | None, tool: str | None, project: dict | None,
                 claude_available: bool, entitled: bool, built, specs: dict[str, dict], company: dict):
        self.today, self.role, self.page, self.tool, self.project = today, role, page, tool, project
        self.claude_available, self.entitled, self.built, self.specs = claude_available, entitled, built, specs
        self.company = company          # {"name", "license", "projects": [...], "subs": [...], "get_project": fn}

    @property
    def ix(self) -> engine.Index:
        return self.built.index


def fill(code: str, table: dict | None = None, **kw) -> str:
    return (table or NOTES)[code].format(**kw)


def note(code: str, **kw) -> dict:
    return {"type": "note", "code": code, "text": fill(code, **kw)}


def _pro_name(type_id: str) -> tuple[str, str, bool]:
    if type_id in PRO_NOUNS:
        return PRO_NOUNS[type_id]
    from tradecomply import professionals
    t = professionals.types().get(type_id) or next(
        (x for x in professionals.handoff_data().get("extra_types", []) if x["id"] == type_id), {})
    name = re.split(r"\s*\(", t.get("name") or type_id.replace("_", " "), maxsplit=1)[0]
    name = name[:1].lower() + name[1:] if not name[:2].isupper() else name
    return (("an " if name[:1].lower() in "aeiou" else "a ") + name, name + "s", False)


def _type_name(type_id: str) -> str:
    from tradecomply import professionals
    t = professionals.types().get(type_id)
    if t:
        return t["name"]
    for x in professionals.handoff_data().get("extra_types", []):
        if x["id"] == type_id:
            return x["name"]
    return professionals.type_label(type_id) or type_id.replace("_", " ")


def find_action(type_id: str, county: str | None = None) -> dict:
    from urllib.parse import quote
    href = f"#/professionals?type={type_id}" + (f"&county={quote(county)}" if county else "")
    out = {"type": "action", "kind": "find_professional", "type_id": type_id, "href": href,
           "label": f"Find {_pro_name(type_id)[0]} (official lookups)"}
    if county:
        out["county"] = county
    return out


def ask_action(q: str) -> dict:
    return {"type": "action", "kind": "ask_claude", "label": ASK_LABEL, "question": q}


def rule_block(r) -> dict:
    src = r.source if (r.source or "").startswith("https://") else None
    return {"type": "rule", "rule_id": r.id, "title": r.title, "text": r.plain_english,
            "correction": r.correction if r.status == "corrected" else None, "quote": r.quote or None,
            "applies_to": r.applies_to or None, "citation": r.citation_text, "source": src, "status": r.status,
            "verified": r.status in core.VERIFIED, "checked_on": r.checked_on, "href": f"#/rule/{r.id}"}


def _final_button(tool: str) -> str:
    lay = catalog.LAYOUTS.get(tool)
    return lay["final_button"] if lay else "Run check"


def _label(tool: str, field: str, spec: dict) -> str:
    from .api import humanize
    if field in catalog.LABELS:
        return catalog.LABELS[field]
    return humanize(field)


def _enum(prop: dict) -> list | None:
    if prop.get("enum"):
        return prop["enum"]
    for x in prop.get("anyOf", []):
        if x.get("enum"):
            return x["enum"]
        if x.get("type") == "array" and (x.get("items") or {}).get("enum"):
            return None
    return None


def _types(prop: dict) -> set:
    t = prop.get("type")
    out = set(t if isinstance(t, list) else [t] if t else [])
    for x in prop.get("anyOf", []):
        if x.get("type"):
            out.add(x["type"])
    return out


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?$")


def validate_args(spec: dict, args: dict) -> dict:
    """Keep only arguments the check's schema has, with values it accepts (enum, finite number, ISO date, list of
    strings); anything else is dropped and so reported as still needed."""
    import math
    props = (spec.get("input_schema") or {}).get("properties") or {}
    out = {}
    for k, v in args.items():
        if v in (None, "", [], {}) or k not in props:
            continue
        p, types = props[k], _types(props[k])
        enum = _enum(p)
        if enum is not None and v not in enum:
            continue
        if isinstance(v, bool):
            if "boolean" not in types:
                continue
        elif isinstance(v, (int, float)):
            if not math.isfinite(v) or not types & {"number", "integer"}:
                continue
            if "integer" in types and "number" not in types:
                if v != int(v):
                    continue
                v = int(v)
        elif isinstance(v, str):
            if "string" not in types:
                continue
            if re.search(r"(?:_date|_on|^when|_at|datetime)$", k) and not _ISO.match(v):
                continue
        elif isinstance(v, list):
            if "array" not in types or not all(isinstance(x, str) for x in v):
                continue
        else:
            continue
        out[k] = v
    return out


def _display(field: str, value, spec: dict) -> str:
    if field == "claimant_role" or field.endswith("_role"):
        return ROLE_DISPLAY.get(value, str(value).replace("_", " ").capitalize())
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)) and field.endswith("_usd"):
        return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        d = date.fromisoformat(value)
        return f"{d:%b} {d.day}, {d.year}"
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", value):
        d = datetime.fromisoformat(value)
        h = d.hour % 12 or 12
        return f"{d:%a} {d:%b} {d.day}, {d.year}, {h}:{d.minute:02d} {'am' if d.hour < 12 else 'pm'}"
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    if isinstance(value, str) and "_" in value and " " not in value:
        return value.replace("_", " ").capitalize()
    return str(value)


def open_tool(ctx: Ctx, tool: str, args: dict, project_id: int | None = None) -> dict:
    spec = ctx.specs.get(tool) or {"input_schema": {}}
    args = validate_args(spec, args)
    required = (spec.get("input_schema") or {}).get("required") or []
    title = catalog.title(tool, spec.get("title"))
    out = {"type": "action", "kind": "open_tool", "tool": tool, "title": title, "args": args,
           "filled": list(args), "missing": [r for r in required if r not in args], "project_id": project_id,
           "label": f"Open {title}", "hint": fill("OPEN_TOOL_HINT", button=_final_button(tool)),
           "href": f"#/tools/{tool}" + (f"?project={project_id}" if project_id else "")}
    return out


def go_action(ctx: Ctx, page_id: str, q: str | None = None) -> tuple[dict | None, dict | None]:
    """A go action to a page the person's role can open, or (None, the role note)."""
    from urllib.parse import quote
    page = catalog.PAGE_BY_ID.get(page_id) or NAV_EXTRA_PAGES.get(page_id)
    if page is None:
        return None, None
    if ctx.role not in page["roles"]:
        return None, note("ROLE_OWNER" if tuple(page["roles"]) == catalog.OWNER else "ROLE_OFFICE")
    href = page["href"] + (f"?q={quote(q)}" if q else "")
    return {"type": "action", "kind": "go", "href": href, "label": f"Open {page['title']}"
            if not q else f"Open Integrations: {q}"}, None


def _anchor_rules(ctx: Ctx, intent: str, tool: str, question: str, has_extra: bool = False) -> list[str]:
    groups = [g for g in engine.groups_in(question) if tool in g["tools"]]
    if not groups and has_extra:
        return []
    if not groups:     # the check's own groups, first anchor each
        own = [g for g in engine.vocab()["groups"] if g["tools"][:1] == [tool]] or \
              [g for g in engine.vocab()["groups"] if tool in g["tools"]]
        groups = [dict(g, anchors=g["anchors"][:1]) for g in own][:2]
    order = [g["anchors"][i] for i in range(3) for g in groups if len(g["anchors"]) > i]      # round-robin
    shared = {r: sum(r in g["anchors"] for g in groups) for r in order}
    return sorted(dict.fromkeys(order), key=lambda r: (-shared[r], order.index(r)))   # in several groups: first


CITY_PREFIX = {"san_diego": "local-sd-", "los_angeles": "local-la-", "san_francisco": "local-sf-",
               "san_jose": "local-sj-", "sacramento": "local-sac-", "oakland": "local-oak-", "long_beach": "local-lb-",
               "fresno": "local-fre-"}
_LOCAL = re.compile(r"^local-(?:sd|la|sf|sj|sac|oak|lb|fre)-")


def rules_for(ctx: Ctx, query: str, city_key: str | None = None, min_cov: float = 0.5, k: int = 12) -> list[str]:
    """Rules the index finds for the question, by score: coverage >= min_cov and >= 45% of the best. A named city
    keeps other cities' local rules out."""
    res = ctx.ix.search(query, types={"rule"}, limit=k * 3)
    if city_key:
        pre = CITY_PREFIX.get(city_key, "")
        res = [r for r in res if not _LOCAL.match(r[1].id.split(":")[-1]) or r[1].id.split(":")[-1].startswith(pre)]
    if not res:
        return []
    best = res[0][0]
    return [d.id for s, d, m in res if m["coverage"] >= min_cov and s >= 0.45 * best][:k]


def _closest_first(ctx: Ctx, anchors: list[str], q: str) -> list[str]:
    """The anchor rules, the ones whose own title and plain English share more of the question's words first (a
    stable sort: anchor order breaks ties)."""
    def cov(rid):
        try:
            r = core.rule(rid)
        except KeyError:
            return 0.0
        return literal_coverage(q, f"{r.title} {r.plain_english}")
    return sorted(dict.fromkeys(anchors), key=lambda r: -cov(r))


def literal_coverage(q: str, text: str) -> float:
    """The share of the question's own words (stemmed, no synonyms) that the text contains."""
    words = set(engine.tokens(q))
    return len(words & set(engine.tokens(text))) / len(words) if words else 0.0


def order_rules(anchors: list[str], found: list[str]) -> list[str]:
    """Anchor rules first (in anchor order), then the other hits by score; one rule per citation (the first)."""
    seen_ids, seen_cites, out = set(), set(), []
    for rid in anchors + found:
        if rid in seen_ids:
            continue
        try:
            r = core.rule(rid)
        except KeyError:
            continue
        seen_ids.add(rid)
        cite = (r.citation_text or rid).lower().strip()
        if cite in seen_cites:
            continue
        seen_cites.add(cite)
        out.append(rid)
    return out


def _strip_company(q: str, s: dict) -> str:
    out = q
    for ent in (s.get("sub"), s.get("project")):
        if ent:
            out = re.sub(re.escape(ent["name"]) + r"(?:'?s)?", " ", out, flags=re.I)
    return out


def _has(q: str, rx: str) -> bool:
    return re.search(rx, q, re.I) is not None


def intent_args(intent: str, q: str, s: dict, ctx: Ctx) -> tuple[dict, list[str]]:
    """(arguments for the intent's check, extra anchor rules) from the slots."""
    d = s.get("dates", [])
    a: dict = {}
    extra: list[str] = []
    if intent == "license_lookup":
        a = {"license_number": s.get("license_number")}
    elif intent == "classification":
        m = (re.search(r"\b(?:need|license|class(?:ification)?|allowed)\b.*?\b(?:to|for)\s+(?:a |an |the )?(.+?)"
                       r"[?.!]*$", q, re.I) or re.search(r"\b(?:for|to|do)\s+(?:a |an |the )?(.+?)[?.!]*$", q, re.I))
        a = {"scope_of_work": (m[1] if m else q)[:300]}
        if s.get("role") == "subcontractor":
            a["contract_role"] = "subcontract"
    elif intent == "lien":
        a = {"claimant_role": s.get("role")}
        prelim = _has(q, r"prelim|20.?day|preliminary")
        noc = _has(q, r"notice of completion|\bnoc\b|cessation")
        if prelim:
            if d:
                a["first_furnishing_date"] = d[0]
            extra = ["prelim-notice-20-day-deadline", "prelim-notice-who-is-exempt"]
        elif d:
            a["last_furnishing_date"] = d[0]
        if noc and len(d) >= 2:
            a["notice_of_completion_date"] = d[-1]
        if not prelim:
            role = s.get("role")
            no_noc = _has(q, r"\bno (?:notice of completion|noc)|without (?:a |an )?(?:notice of completion|noc)|"
                             r"never (?:\w+ ){0,3}(?:recorded|filed)|not recorded|wasn'?t recorded")
            extra = ((["lien-90-days-no-noc-or-cessation"] if no_noc else
                      ["prime-lien-60-days-after-noc-or-cessation"] if role == "direct_contractor" and noc else
                      ["sub-lien-30-days-after-noc-or-cessation"] if noc else
                      ["lien-90-days-no-noc-or-cessation"]) + ["lien-foreclosure-90-days-after-recording"])
    elif intent == "stop_notice":
        a = {"claimant_role": s.get("role"), "project_type": "public" if _has(q, r"\bpublic|\bcity\b|\bcounty\b|"
                                                                              r"\bschool|\bstate\b") else
             "private" if _has(q, r"\bprivate\b") else None}
    elif intent == "cancel":
        a = {"received_signed_copy_on": d[0] if d else None, "buyer_age_65_or_older": s.get("senior")}
        extra = ["three-day-right-to-cancel-notice"] + (["senior-five-day-right-to-cancel"] if s.get("senior")
                                                         else [])
    elif intent == "down_payment":
        a = {"contract_price_usd": s.get("money_usd")}
        extra = ["hic-down-payment-cap"]
    elif intent == "retention":
        a = {"retention_pct": s.get("percent"),
             "project_type": "public" if _has(q, r"\bpublic|\bcity\b|\bcounty\b|\bschool") else
             "private" if _has(q, r"\bprivate\b|\bhomeowner\b") else None}
    elif intent == "prompt_pay":
        payer = "direct_contractor" if _has(q, r"\bgc\b|general contractor|\bprime\b") else \
            "owner" if _has(q, r"\bowner\b") and not _has(q, r"\bowner pays\b|after the owner") else None
        a = {"payer_role": payer,
             "payee_role": "subcontractor" if s.get("role") in ("subcontractor", "direct_contractor") or
             (payer == "direct_contractor" and _has(q, r"\bme\b|\bus\b")) else None,
             "project_type": "public" if _has(q, r"\bpublic\b") else "private" if _has(q, r"\bprivate\b") else None}
        extra = ["bp-7108-5-pay-subs-7-days"]
    elif intent in ("osha_permit", "asbestos"):
        trench = _has(q, r"trench|excavat|ditch|hole")
        a = {"excavation_max_depth_ft": s.get("feet") if trench or _has(q, r"\bdeep\b") else None,
             "workers_enter_trench": True if trench and _has(q, r"\b(?:in|into|enter|down|inside)\b") else None,
             "asbestos_sqft": s.get("sqft") if _has(q, r"asbestos|popcorn|\bacm\b") else None,
             "scaffold_or_falsework_height_ft": s.get("feet") if _has(q, r"scaffold|falsework") else None,
             "demolition": True if _has(q, r"demoli") else None}
        if intent == "asbestos":
            extra = ["asbestos-dosh-registration-100sqft", "asbestos-100-sqft-c22-or-certification"]
    elif intent == "final_pay":
        sep = ("discharged" if _has(q, r"\bfired?\b|\bfiring\b|discharg|let (?:him|her|them|someone) go|terminat")
               else "laid_off" if _has(q, r"laid (?:\w+ )?off|layoff|end of (?:the )?(?:job|project)")
               else "quit_without_notice" if _has(q, r"\bquit") and _has(q, r"without (?:any )?notice|no notice")
               else "quit_with_72h_notice" if _has(q, r"\bquit") and _has(q, r"72|notice") else None)
        a = {"separation_type": sep, "separation_date": d[0] if d else (s.get("date_weekday") and
                                                                         _past_weekday(ctx.today, q))}
    elif intent == "injury":
        a = {"hospitalized": True if _has(q, r"hospital|admitted|kept overnight") else None,
             "fatality": True if _has(q, r"\bdied\b|fatal|death|\bkilled\b") else None}
        extra = ["serious-injury-report-8-hours"]
    elif intent == "smoke":
        a = {"aqi_pm25": s.get("aqi")}
        extra = ["osha2-smoke-scope-aqi-151"]
    elif intent == "valley_fever":
        a = {"county": s.get("county"),
             "soil_disturbing_work": True if _has(q, r"\bdig|grad(?:e|ing)|trench|dirt|soil|earth ?mov") else None}
    elif intent == "heat":
        a = {"outdoor_work": True, "company_name": ctx.company.get("name")}
        extra = ["heat-shade-above-80f", "heat-high-heat-procedures-95f", "heat-water-one-quart-per-hour"]
    elif intent == "toilets":
        a = {"company_name": ctx.company.get("name")}
        extra = ["toilets-1-per-20"]
    elif intent == "toolbox":
        a = {"company_name": ctx.company.get("name")}
        extra = ["toolbox-tailgate-meetings-10-working-days", "iipp-written-program-required"]
    elif intent == "hours":
        when = s.get("date_weekday") or (d[0] if d else None)
        if when and s.get("time"):
            when = f"{when}T{s['time']}"
        a = {"city": s.get("city"), "when": when}
    elif intent == "min_wage":
        a = {"city": s.get("city")}
    elif intent == "sick":
        extra = ["paid-sick-leave-40-hours-5-days"]
    elif intent == "overtime":
        extra = ["daily-weekly-overtime-1-5x", "first-meal-period-5-hours"]
    elif intent == "public_works":
        body = ("school_district" if _has(q, r"\bschool") else "city" if _has(q, r"\bcity\b|city-") else
                "county" if _has(q, r"\bcounty\b") else "state_agency" if _has(q, r"\bstate agency|caltrans") else None)
        a = {"project_value_usd": s.get("money_usd"), "awarding_body_type": body}
    elif intent == "apprentice":
        a = {"prime_contract_usd": s.get("money_usd")}
        extra = ["apprentice-ratio-1-to-5", "apprenticeship-threshold-30000"]
    elif intent == "adu":
        a = {"sq_ft": s.get("sqft"),
             "adu_type": "detached" if _has(q, r"\bdetached") else "attached" if _has(q, r"\battached") else
             "jadu" if _has(q, r"\bjadu|junior") else "conversion" if _has(q, r"convert|conversion|garage") else None}
    elif intent == "energy":
        a = {"scope": "hvac_changeout" if _has(q, r"hvac|furnace|\bac\b|a/c|heat pump|changeout|change-out|"
                                                 r"air condition") else
             "reroof" if _has(q, r"roof") else "water_heater" if _has(q, r"water heater") else
             "window_replacement" if _has(q, r"window") else None}
    elif intent == "stormwater":
        a = {"disturbed_acres": s.get("acres")}
    elif intent == "lead":
        pre = _has(q, r"pre-?1978|before 1978") or (s.get("year") is not None and s["year"] < 1978)
        a = {"tasks": [q[:200]], "built_before_1978": True if pre else None,
             "year_built": s.get("year") if s.get("year") and s["year"] < 2030 else None}
        extra = ["rrp-applicability-pre-1978", "rrp-minor-repair-6-20-sqft"]
    elif intent == "building_permit":
        st = next((v for rx, v in STRUCTURES if _has(q, rx)), None)
        a = {"work_description": q[:300], "structure_type": st}
        extra = ["permit-required-exemptions-thresholds"]
    elif intent == "safety":
        hz = next((h for h, w in (("fall protection", r"fall|harness|tie.?off|guardrail|edge"), ("ladder", r"ladder"),
                                  ("scaffold", r"scaffold"), ("silica", r"silica|concrete cut|grind|cut-off saw"),
                                  ("excavation", r"trench|excavat|shoring")) if _has(q, w)), None)
        a = {"hazard": hz}
    elif intent == "unlicensed":
        a = {"contract_price_usd": s.get("money_usd"), "scope": q[:200]}
        extra = ["minor-work-exemption-1000", "minor-work-no-splitting"]
    elif intent == "workers_comp":
        a = {"license_number": s.get("license_number"),
             "required_classification": (s.get("classes") or [None])[0] or ("C-39" if _has(q, r"roof") else None)}
        extra = ["workers-comp-exemption-until-2028", "workers-comp-required-classifications"]
    elif intent == "setup":
        a = {"company_name": ctx.company.get("name")}
    return a, extra


STRUCTURES = [(r"\bshed\b|accessory (?:building|structure)|playhouse", "detached_accessory_structure"),
              (r"\bfence", "fence"), (r"retaining wall", "retaining_wall"), (r"\bdeck\b", "deck"),
              (r"\bpool|\bspa\b|hot tub", "pool_or_spa"), (r"re-?roof|new roof|roof replacement", "reroof"),
              (r"water heater", "water_heater"), (r"hvac|furnace|\bac\b|a/c|heat pump", "hvac_changeout"),
              (r"solar|\bpv\b", "solar_pv"), (r"demoli|tear down", "demolition"), (r"\baddition\b", "addition"),
              (r"window", "window_replacement"), (r"driveway|sidewalk|patio slab", "sidewalk_or_driveway"),
              (r"electrical|panel|outlet|wiring", "electrical"), (r"plumbing|repipe", "plumbing")]


def _past_weekday(today: date, q: str) -> str | None:
    m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", q.lower())
    if not m:
        return None
    back = (today.weekday() - WEEKDAYS.index(m[1])) % 7
    return (today - timedelta(days=back)).isoformat()


PROJECT_FILLS = {   # check field <- project column (only explicit matches: the named job or ?project=)
    "calculate_lien_deadline": {"first_furnishing_date": "first_furnishing_date",
                                "last_furnishing_date": "last_furnishing_date", "completion_date": "completion_date",
                                "notice_of_completion_date": "noc_date"},
    "stop_payment_notice_deadline": {"completion_date": "completion_date", "noc_recorded_date": "noc_date",
                                     "last_furnishing_date": "last_furnishing_date",
                                     "first_furnishing_date": "first_furnishing_date",
                                     "project_type": "public_or_private"},
    "prompt_payment_check": {"project_type": "public_or_private", "completion_date": "completion_date",
                             "noc_recorded_date": "noc_date"},
    "retention_cap_check": {"project_type": "public_or_private", "contract_price_usd": "contract_price"},
    "validate_home_improvement_contract": {"contract_price_usd": "contract_price", "start_date": "start_date",
                                           "completion_date": "completion_date"},
    "construction_hours": {"city": "city"}, "jobsite_minimum_wage": {"city": "city"},
    "valley_fever_requirements": {"county": "county"},
    "project_setup_checklist": {"project_name": "name", "city": "city", "county": "county",
                                "contract_price_usd": "contract_price", "start_date": "start_date"},
    "public_works_readiness": {"project_value_usd": "contract_price", "start_date": "start_date"},
    "unlicensed_contracting_exposure": {"contract_price_usd": "contract_price"},
}


def understood_from(tool: str, args: dict, spec: dict, s: dict, project: dict | None) -> list[dict]:
    out = []
    if project:
        out.append({"label": "Job", "value": str(project["id"]), "display": project["name"]})
    if s.get("sub"):
        out.append({"label": "Subcontractor", "value": str(s["sub"]["id"]), "display": s["sub"]["name"]})
    for k, v in args.items():
        if k in ("company_name", "work_description", "scope", "scope_of_work", "tasks"):
            continue
        out.append({"label": _label(tool, k, spec), "value": v, "display": _display(k, v, spec)})
    return out


def _maybe_pros(tool: str, q: str) -> list[dict]:
    """Kinds of professional the check's handoffs can name (data/handoffs.json) whose trigger words the question
    uses. Every handoff is a test on the check's result, so before a run Quick help only says the check will tell."""
    from tradecomply import professionals
    extra = {t["id"]: t for t in professionals.handoff_data().get("extra_types", [])}
    seen = []
    for h in professionals.handoffs():
        if h["tool"] != tool:
            continue
        tid = h.get("registry_type_id") or h["professional_type_id"]
        if tid in seen:
            continue
        t = professionals.types().get(tid) or extra.get(tid) or {}
        words = list(t.get("triggers", [])) + [re.split(r"\s*\(", t.get("name", ""), maxsplit=1)[0]]
        if any(w and re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", q.lower()) for w in words):
            seen.append(tid)
    return [{"type": "maybe_professional", "type_id": tid, "name": _type_name(tid), "note": NOTES["MAYBE_PRO"]}
            for tid in seen[:2]]


def answer_intent(intent: str, q: str, s: dict, ctx: Ctx, confidence: str) -> dict:
    tool = INTENT_TOOL.get(intent) or intent
    if intent == "find_pro":
        return answer_find_pro(q, s, ctx)
    spec = ctx.specs.get(tool) or {}
    args, extra = intent_args(intent, q, s, ctx)
    project = s.get("project") or ctx.project
    project_id = None
    if project:
        row = ctx.company["get_project"](project["id"])
        if row:
            project_id = row["id"]
            for field, col in PROJECT_FILLS.get(tool, {}).items():
                if args.get(field) in (None, "") and row.get(col) not in (None, ""):
                    args[field] = row[col]
            s = dict(s, project=row)
    blocks: list[dict] = []
    if intent == "license_lookup" and s.get("sub"):
        blocks.append({"type": "action", "kind": "go", "href": f"#/subs?sub={s['sub']['id']}",
                       "label": f"Open {s['sub']['name']} in Subcontractors"})
    ck = s.get("city_key") if intent in ("hours", "min_wage") else None
    anchors = extra + _anchor_rules(ctx, intent, tool, _strip_company(q, s), has_extra=bool(extra))
    if intent == "hours" and ck:
        from tradecomply import local_rules
        anchors = [local_rules._CITY_RULE[ck]] + [r for r in anchors if r.startswith(CITY_PREFIX[ck])]
    if intent == "classification":
        for _, d, _ in ctx.ix.search(args.get("scope_of_work") or q, types={"class"}, limit=3):
            anchors.append(d.meta["rule_id"])
    found = rules_for(ctx, f"{RULE_QUERY.get(intent, '')} {_strip_company(q, s)}", city_key=ck)
    plain = _strip_company(q, s)
    picked = _closest_first(ctx, extra, plain)            # the intent's own rules first, then its groups' anchors
    rids = order_rules(picked + _closest_first(ctx, [a for a in anchors if a not in picked], plain), found)
    act = open_tool(ctx, tool, args, project_id)
    if intent == "toolbox":
        go, role_note = go_action(ctx, "field")
        start = None
        if go:
            start = {"type": "action", "kind": "go", "href": "#/field?kind=toolbox_talk",
                     "label": "Start a toolbox talk"}
        blocks = ([start] if start else []) + blocks
        if role_note:
            blocks.append(role_note)
    out_blocks = [rule_block(core.rule(r)) for r in rids[:2]]
    more = [{"rule_id": r, "title": core.rule(r).title, "verified": core.rule(r).verified} for r in rids[2:7]]
    if intent != "toolbox":
        out_blocks.append(act)
    else:
        out_blocks += blocks
        blocks = []
        out_blocks.append(act)
    out_blocks += blocks
    out_blocks += _maybe_pros(tool, q)
    notes = []
    for words, d in s.get("read_dates", []):
        if d.isoformat() in json_values(act["args"]):
            notes.append(note("READ_DATE", words=words, date=f"{d:%b} {d.day}, {d.year}"))
    if act["missing"]:
        notes.append(note("MISSING", labels=", ".join(_label(tool, f, spec) for f in act["missing"])))
    lead = "LEAD_TOOL" if act["args"] else "LEAD_RULES"
    return {"intent": intent, "confidence": confidence, "route": None, "lead": LEADS[lead],
            "understood": understood_from(tool, act["args"], spec, s, s.get("project") if project_id else None),
            "choices": [], "blocks": out_blocks + notes, "more_rules": more,
            "followups": followups(intent, q)}


def json_values(args: dict) -> set:
    out = set()
    for v in args.values():
        out.add(v if not isinstance(v, str) else v[:10])
    return out


def answer_find_pro(q: str, s: dict, ctx: Ctx) -> dict:
    """Spec 5.4 step 7: a requirement block only when the best when_required situation fits the question (coverage
    >= 0.5) better than every not_required_when and optional_when situation of that type."""
    from tradecomply import professionals
    tid = next((t_id for rx, t_id in NAMED_PRO if rx.search(q.lower())), None)
    if tid is None:
        res = ctx.ix.search(q, types={"professional"}, limit=3)
        if not res:
            return not_found(q, ctx)
        tid = res[0][1].id
    t = professionals.types().get(tid) or {}
    def cov(w):      # the question's own words only: a synonym must not make a requirement fit
        return literal_coverage(q, w.get("situation") or "")
    best = max(t.get("when_required", []), key=cov, default=None)
    best_cov = cov(best) if best else 0.0
    exceptions = [cov(w) for k in ("not_required_when", "optional_when") for w in t.get(k, [])]
    blocks: list[dict] = []
    a_name, names, licensed = _pro_name(tid)
    if best and best_cov >= 0.5 and best_cov > max(exceptions, default=0.0):
        blocks.append({"type": "requirement", "type_id": tid, "name": t.get("name", tid),
                       "situation": best["situation"], "legal_basis": best.get("legal_basis"),
                       "source_url": best.get("source_url"), "status": best.get("status"),
                       "verified": best.get("status") == "confirmed"})
        lead = fill("LEAD_PRO" if licensed else "LEAD_PRO_OTHER", LEADS, a_name=a_name)
    else:
        lead = fill("LEAD_PRO_FIND" if licensed else "LEAD_PRO_FIND_OTHER", LEADS, a_name=a_name, names=names)
    blocks.append(find_action(tid, s.get("county")))
    understood = [{"label": "Kind of professional", "value": tid, "display": t.get("name", tid)}]
    if s.get("county"):
        understood.append({"label": "County", "value": s["county"], "display": f"{s['county']} County"})
    return {"intent": "find_pro", "confidence": "high" if best_cov >= 0.5 else "medium", "route": None,
            "lead": lead, "understood": understood, "choices": [], "blocks": blocks, "more_rules": [],
            "followups": followups("find_pro", q)}


def followups(intent: str, q: str) -> list[dict]:
    qn = engine.fold(q).strip(" ?")
    return [{"label": f, "question": f} for f in catalog.FOLLOWUPS.get(intent, [])
            if engine.fold(f).strip(" ?") != qn][:3]


def route_answer(code: str, q: str, ctx: Ctx, pro: str | None, guard: bool = False) -> dict:
    """Spec 5.9 "Route answers": the note, a find action when a professional applies, and the Claude action (or
    CLAUDE_OFF for the questions only Claude could take). Never a rule, requirement, maybe_professional or open_tool
    block, and no more_rules."""
    blocks = [note(code)]
    if pro:
        blocks.append(find_action(pro))
    if ctx.claude_available:
        blocks += [note("CLAUDE_CAN"), ask_action(q)]
    elif code in ("ROUTE_DRAFT", "ROUTE_STATE", "ROUTE_DOCS", "ROUTE_JUDGMENT"):
        blocks.append(note("CLAUDE_OFF"))
    if guard:
        route = "professional" if pro else ("claude" if ctx.claude_available else "search")
    else:
        route = "claude" if ctx.claude_available else ("professional" if pro else "search")
    return {"intent": f"route:{code}", "confidence": "high", "route": route, "lead": "", "understood": [],
            "choices": [], "blocks": blocks, "more_rules": [], "followups": []}


def nav_answer(page_id: str, q: str, ctx: Ctx, product: str | None = None) -> dict:
    act, role_note = go_action(ctx, page_id, product)
    blocks = [act] if act else [role_note]
    return {"intent": "nav", "confidence": "high", "route": None, "lead": LEADS["LEAD_NAV"] if act else "",
            "understood": [], "choices": [], "blocks": blocks, "more_rules": [], "followups": []}


def not_found(q: str, ctx: Ctx) -> dict:
    blocks = [note("NOT_FOUND"), note("NOT_FOUND_TRY")]
    if ctx.built:
        for _, d, _ in ctx.ix.search(q, types={"tool"}, limit=8):
            if d.meta.get("hidden"):
                continue
            blocks.append(open_tool(ctx, d.id, {}))
            if sum(b.get("kind") == "open_tool" for b in blocks) == 3:
                break
    if ctx.claude_available:
        blocks += [note("CLAUDE_CAN"), ask_action(q)]
    return {"intent": "not_found", "confidence": "low", "route": "claude" if ctx.claude_available else "search",
            "lead": LEADS["LEAD_NOT_FOUND"], "understood": [], "choices": [], "blocks": blocks, "more_rules": [],
            "followups": []}


def rules_only(q: str, rids: list[str], ctx: Ctx) -> dict:
    blocks = [rule_block(core.rule(r)) for r in rids[:2]]
    if ctx.claude_available:
        blocks += [note("CLAUDE_CAN"), ask_action(q)]
    return {"intent": "rules", "confidence": "low", "route": None, "lead": LEADS["LEAD_RULES_ONLY"],
            "understood": [], "choices": [], "blocks": blocks,
            "more_rules": [{"rule_id": r, "title": core.rule(r).title, "verified": core.rule(r).verified}
                           for r in rids[2:7]], "followups": []}


URGENT = re.compile(r"\bdied\b|\bdeath\b|\bfatal|\bkilled\b|hospitali[sz]|\badmitted\b|amputat")
MIN_INTENT = 0.6      # a pattern, the check's own task classifier, or two vocabulary groups; one group alone is not


def score_intents(q: str, t: str, ctx: Ctx) -> list[tuple[float, str]]:
    from tradecomply import trade_certifications
    groups = engine.groups_in(q)
    pick = PICK_HINT.search(q)
    picked = None
    if pick:
        want = engine.fold(pick.group(1))
        picked = next((n for n, c in catalog.TOOLS.items() if engine.fold(c["title"]) == want), None)
    scores = []
    lead_task = None
    for name, rx, tool in INTENTS:
        sc = 1.0 if rx.search(t) and not (name in EXCLUDE and EXCLUDE[name].search(t)) else 0.0
        if sc and name == "find_pro":
            sc = 2.0          # the person asks for the professional: that outranks the subject of the work
        if sc and name == "injury" and URGENT.search(t):
            sc += 1.0         # a death or hospital stay starts a reporting clock: that is the question
        bonus = min(0.8, 0.4 * sum(1 for g in groups if tool in g["tools"]))
        if name == "lead" and lead_task is None:
            year = re.search(r"\b(1[89]\d\d)s?\b", t)
            lead_task = bool(year and int(year[1]) < 1978 and
                             any(tr == "lead" for tr, _ in trade_certifications.classify_task(q)))
        if name == "lead" and lead_task:
            sc += 0.6
        if sc == 0:
            continue          # vocabulary groups only add to a pattern (or classifier) hit; else the index decides
        sc += bonus
        if ctx.tool and ctx.tool == tool:
            sc += 0.3
        if picked and picked == tool:
            sc += 2.0
        if sc >= MIN_INTENT:
            scores.append((sc, name))
    scores.sort(key=lambda x: (-x[0], INTENT_ORDER[x[1]]))
    return scores


def answer(question: str, ctx: Ctx) -> dict:
    """The pipeline (spec 5.4). Returns the answer without the envelope fields route() adds."""
    q = re.sub(r"\s+", " ", question).strip()[:MAX_READ]
    t = q.lower()
    # 2. route check: skip / work-around first, then the questions Quick help must not answer itself
    if GUARD.search(t):
        pro = next((tid for rx, tid in GUARD_PRO if rx.search(t)), None)
        return route_answer("GUARD", q, ctx, pro, guard=True)
    for code, rx, pro in ROUTES:
        if rx.search(t):
            return route_answer(f"ROUTE_{code}", q, ctx, pro)
    # 3. navigation, only with an action or place word
    if ACTION_WORDS.search(t):
        m = re.search(r"\bconnect\b\s+(?:to\s+)?(?:my\s+|our\s+)?([a-z0-9][\w .&-]{1,40}?)"
                      r"(?:\s+(?:to|with)\b|[?.!]|$)", t)
        if m:
            product = _product(m.group(1))
            if product:
                return nav_answer("integrations", q, ctx, product)
        for rx, page in NAV:
            if rx.search(t):
                if page.startswith("integrations:"):
                    return nav_answer("integrations", q, ctx, page.split(":", 1)[1])
                return nav_answer(page, q, ctx)
    if not ctx.built:
        return warming(q, ctx)
    s = slots(q, ctx.today, ctx.company["counties"], ctx.company["cities"], ctx.company["projects"],
              ctx.company["subs"])
    # 5. intents, on the question without the company's own job and sub names ("Elm St ADU" is not about ADUs)
    plain = _strip_company(q, s)
    scores = score_intents(plain, plain.lower(), ctx)
    if scores:
        best_score, best = scores[0]
        rival = next(((sc, n) for sc, n in scores[1:] if INTENT_TOOL[n] != INTENT_TOOL[best]), None)
        if rival and rival[0] >= 0.85 * best_score and not PICK_HINT.search(q):
            return choices_answer(q, [best, rival[1]] + [n for sc, n in scores[2:3]
                                                         if INTENT_TOOL[n] not in (INTENT_TOOL[best],
                                                                                   INTENT_TOOL[rival[1]])
                                                         and sc >= 0.85 * best_score], ctx)
        signals = best_score >= 1.4 or any(k in s for k in ("dates", "money_usd", "feet", "sqft", "aqi", "city",
                                                             "county", "role", "license_number", "acres"))
        return answer_intent(best, q, s, ctx, "high" if signals else "medium")
    # 6. fallbacks: the best check, then rules, then not found
    tools = [x for x in ctx.ix.search(q, types={"tool"}, limit=3) if not x[1].meta.get("hidden")]
    if tools and tools[0][2]["coverage"] >= 0.5:
        name = tools[0][1].id
        intent = TOOL_INTENT.get(name)
        if intent:
            return answer_intent(intent, q, {}, ctx, "medium")
        return tool_answer(name, q, ctx)
    rids = rules_for(ctx, q)
    if rids:
        return rules_only(q, order_rules([], rids), ctx)
    return not_found(q, ctx)


def tool_answer(name: str, q: str, ctx: Ctx) -> dict:
    """A check found by the index that has no intent of its own: its rules from the index and the open action."""
    rids = order_rules([], rules_for(ctx, q))
    act = open_tool(ctx, name, {})
    blocks = [rule_block(core.rule(r)) for r in rids[:2]] + [act] + _maybe_pros(name, q)
    return {"intent": "check", "confidence": "medium", "route": None, "lead": LEADS["LEAD_RULES"],
            "understood": [], "choices": [], "blocks": blocks,
            "more_rules": [{"rule_id": r, "title": core.rule(r).title, "verified": core.rule(r).verified}
                           for r in rids[2:7]], "followups": []}


def choices_answer(q: str, intents: list[str], ctx: Ctx) -> dict:
    ch = []
    for n in intents[:3]:
        title = catalog.title(INTENT_TOOL[n])
        ch.append({"label": title, "question": f"{q} ({title})"})
    return {"intent": "choices", "confidence": "medium", "route": None, "lead": LEADS["LEAD_CHOICES"],
            "understood": [], "choices": ch, "blocks": [], "more_rules": [], "followups": []}


def warming(q: str, ctx: Ctx) -> dict:
    return {"intent": "not_found", "confidence": "low", "route": "search", "lead": "", "understood": [],
            "choices": [], "blocks": [note("INDEX_WARMING")], "more_rules": [], "followups": []}


def _product(text: str) -> str | None:
    """A product the integrations catalog lists (by the vocabulary's integration names)."""
    g = next((g for g in engine.vocab()["groups"] if g.get("page") == "integrations"), None)
    if not g:
        return None
    words = engine.fold(text).strip()
    for f in g["forms"]:
        form = f["form"] if isinstance(f, dict) else f
        if re.search(rf"(?<!\w){re.escape(form)}(?!\w)", words):
            return form.title() if form.islower() and len(form) > 3 else form.upper() if len(form) <= 3 else form
    return None


def finish(out: dict, ctx: Ctx, too_long: bool) -> dict:
    """Entitlement trimming (spec 5.4 step 9), the counts notes and the envelope."""
    blocks = out["blocks"]
    if not ctx.entitled:
        blocks = [b for b in blocks if b["type"] not in ("rule", "requirement")]
        out["more_rules"] = []
        blocks.append(note("SUBSCRIBE"))
    unverified = sum(1 for b in blocks if b["type"] == "rule" and not b["verified"])
    if unverified:
        blocks.append(note("UNVERIFIED_SOME", n=unverified))
    unconfirmed = sum(1 for b in blocks if b["type"] == "requirement" and not b["verified"])
    if unconfirmed:
        blocks.append(note("UNCONFIRMED_SOME", n=unconfirmed))
    if too_long:
        blocks.append(note("TOO_LONG"))
    out["blocks"] = blocks
    return out


def safe_answer(question: str, ctx: Ctx) -> dict:
    """answer() inside one try: an exception is logged by type and intent only (never the message, which could hold
    typed words) and the person gets the not-found answer."""
    too_long = len(re.sub(r"\s+", " ", question).strip()) > MAX_READ
    try:
        out = answer(question, ctx)
    except Exception as e:     # noqa: BLE001
        log.error("quickhelp failed intent=%s exc=%s", "-", type(e).__name__)
        try:
            out = not_found(re.sub(r"\s+", " ", question).strip()[:MAX_READ], ctx)
        except Exception as e2:    # noqa: BLE001
            log.error("quickhelp failed intent=not_found exc=%s", type(e2).__name__)
            out = {"intent": "not_found", "confidence": "low", "route": "search", "lead": LEADS["LEAD_NOT_FOUND"],
                   "understood": [], "choices": [], "blocks": [note("NOT_FOUND")], "more_rules": [],
                   "followups": []}
    return finish(out, ctx, too_long)


# ---------------------------------------------------------------------------- route


def today() -> date:
    """Today in California (tests set a fixed day)."""
    return datetime.now(CA_TZ).date()


class HelpIn(searchmod.Strict):
    question: str = Field(min_length=1, max_length=2000)
    page: Optional[str] = Field(None, max_length=40)
    tool: Optional[str] = Field(None, max_length=80)
    project_id: Optional[int] = None


def company_context(platform, p: Principal, project_id: int | None) -> tuple[dict, dict | None]:
    from tradecomply import local_rules, professionals
    store, tid = platform.store, p.tenant_id
    tenant = store.get_tenant(tid) or {}
    projects = [{"id": r["id"], "name": r["name"], "city": r["city"], "county": r["county"]}
                for r in store.candidates(tid, "projects", ("id", "name", "city", "county"))
                if (r["name"] or "").strip()]
    subs = [{"id": r["id"], "name": r["name"], "license_number": r["license_number"]}
            for r in store.candidates(tid, "subs", ("id", "name", "license_number")) if (r["name"] or "").strip()]
    cities = [{"key": k, "name": local_rules._city_name(k), "aliases": sorted(local_rules._ALIASES.get(k, ()))}
              for k in local_rules._CITY_RULE]
    project = store.get("projects", tid, project_id) if project_id is not None else None
    company = {"name": tenant.get("company_name"), "license": tenant.get("cslb_license"), "projects": projects,
               "subs": subs, "counties": professionals.counties(), "cities": cities,
               "get_project": lambda pid: store.get("projects", tid, pid)}
    return company, project


@router.post("/help")
async def help_route(body: HelpIn, request: Request, p: Principal = Depends(people_only)):
    from . import api, assistant
    platform = platform_of(request)
    if body.page is not None and body.page not in assistant.PAGE_NAMES:
        raise HTTPException(422, "Unknown page")
    wait = platform.help_user_limiter.retry_after(f"u|{p.tenant_id}|{p.user_id}")
    if wait:
        raise HTTPException(429, "That's a lot of questions in a minute. Wait a moment and try again.",
                            headers={"Retry-After": str(wait)})
    platform.help_user_limiter.hit(f"u|{p.tenant_id}|{p.user_id}")
    t0 = time.perf_counter()
    tenant = await anyio.to_thread.run_sync(platform.store.get_tenant, p.tenant_id)
    ok = bool(searchmod.entitlement(tenant)["ok"])
    claude = bool(assistant.status(platform, p, tenant)["available"])
    built = await searchmod.ensure(platform)
    specs = {s["name"]: s for s in await api.tool_specs(platform)}
    tool = body.tool if body.tool in specs else None
    company, project = await anyio.to_thread.run_sync(company_context, platform, p, body.project_id)
    ctx = Ctx(today=today(), role=p.role, page=body.page, tool=tool, project=project,
              claude_available=claude, entitled=ok, built=built, specs=specs, company=company)
    out = await anyio.to_thread.run_sync(safe_answer, body.question, ctx)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    log.debug("intent=%s route=%s blocks=%d ms=%s", out["intent"], out["route"], len(out["blocks"]), ms)
    return {"answer_id": "qh_" + secrets.token_hex(4), "intent": out["intent"], "confidence": out["confidence"],
            "route": out["route"], "lead": out["lead"], "understood": out["understood"], "choices": out["choices"],
            "blocks": out["blocks"], "more_rules": out["more_rules"], "followups": out["followups"],
            "claude": {"available": claude}, "entitled": ok, "disclaimer": core.DISCLAIMER,
            "rules_built_on": searchmod.built_on(), "took_ms": ms}


router.add_api_route("/help", searchmod.post_only, methods=["GET", "HEAD"], include_in_schema=False)
