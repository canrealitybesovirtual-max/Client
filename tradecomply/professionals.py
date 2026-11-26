"""Licensed professionals: who a result needs, why the law needs them, when, and where to find and check one.

  find_licensed_professional  a professional type (or a need in plain words) + the job's county -> what the person
                              does, when the law requires one (cited, with its verification status), the official
                              public directories (county-filtered only where the official site takes it in the URL),
                              how to check the license, and what to bring or ask
  who_must_sign               a tool's result (or its inputs) -> the licensed or certified people that result needs,
                              each with the reason, the legal basis and a link to find one
  list_professional_types     every professional type, its directories, and the 58 county names

Data:
  tradecomply/data/professionals.json  the registry of professional types and their official directories (each
                                       link marked reachable or blocked as of its check date)
  tradecomply/data/handoffs.json       the handoff map: for each tool, which result triggers which professional
                                       (a test expression over the result), the legal basis and rule ids; plus plain
                                       wording and the types the registry does not have yet (extra_types)

TradeComply never recommends, ranks or picks a person or firm, and never scrapes or works around a site's bot
protection: it links to the official search page and lets the user search. `verified` is True only when every
rule a statement relies on is confirmed (or corrected) in tradecomply/data/rulebook.json and no part of the legal
basis is marked unconfirmed.

The web app (app/api.py) attaches `handoffs` (handoffs_for) to /api/tools/{name} results and serves
/api/professionals/*; see docs/integrations-dev.md section 12.
"""
import ast
import difflib
import json
import logging
import operator
import os
import re
from contextvars import ContextVar
from functools import lru_cache
from typing import Annotated, Any, Callable, Literal, Optional, Union
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from .core import Result, all_rules

logger = logging.getLogger("tradecomply.professionals")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "professionals.json")
HANDOFFS_PATH = os.path.join(DATA_DIR, "handoffs.json")
VERIFIED_STATUS = {"confirmed", "corrected"}
REQUIREMENT_ORDER = ("required", "conditional", "recommended", "optional")
NEUTRAL_NOTE = ("TradeComply does not recommend, rank or choose any provider. These links go to official public "
                "search pages: pick a provider yourself and check their license before you hire.")
# Shown when who_must_sign finds nobody: an empty list is not a finding that no licensed person is needed.
NEMPTY = ("That does not mean no licensed or certified person is needed: the law may still require one for facts "
          "this result does not cover. ")
MAX_RESULT_JSON = 2_000_000
# A result passed in by the caller is walked by every handoff test of its tool: cap its size (values in it, at any
# depth). Real results have a few thousand; a 500-row bulk license check about 13,000.
MAX_RESULT_NODES = 25_000
MAX_NEED = 500
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
CITY_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,79}$")

# Set by the web app around an in-process tool run (app/api.py): tool name -> may this caller run it? Used when
# who_must_sign is given a tool's inputs instead of its result, so an API key limited to some tools can't run
# others through it.
RUN_GUARD: ContextVar[Optional[Callable[[str], bool]]] = ContextVar("tradecomply_professionals_run_guard",
                                                                    default=None)


# ---------------------------------------------------------------------------- data
@lru_cache(maxsize=1)
def registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def handoff_data() -> dict:
    with open(HANDOFFS_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def types() -> dict[str, dict]:
    """Every professional type by id: the registry's, then handoffs.json extra_types the registry doesn't have."""
    out = {t["id"]: t for t in registry()["types"]}
    for t in handoff_data().get("extra_types", []):
        out.setdefault(t["id"], t)
    return out


@lru_cache(maxsize=1)
def aliases() -> dict[str, str]:
    """Handoff professional type ids (e.g. professional_engineer_civil) -> the type that holds their directories
    (e.g. professional_engineer). Every type id maps to itself."""
    known = types()
    out = {tid: tid for tid in known}
    for e in handoffs():
        role = e["professional_type_id"]
        target = e.get("registry_type_id") if e.get("registry_type_id") in known else role
        if target in known:
            out.setdefault(role, target)
    return out


def handoffs() -> list[dict]:
    return handoff_data()["handoffs"]


@lru_cache(maxsize=1)
def _rule_status() -> dict[str, str]:
    return {r.id: r.status for r in all_rules()}


def rule_verified(rule_id: str) -> bool:
    return _rule_status().get(rule_id) in VERIFIED_STATUS


def counties() -> list[str]:
    return list(registry()["counties"])


def checked_on() -> str:
    return registry().get("checked") or handoff_data().get("checked") or ""


def type_label(role_id: str) -> str | None:
    return handoff_data().get("type_labels", {}).get(role_id)


# ---------------------------------------------------------------------------- input checks
def normalize_county(value: str) -> str:
    """One of California's 58 county names, spelled as the registry has it. 'fresno county', 'County of Fresno'
    and extra spaces are accepted; anything else is a ValueError naming the closest counties."""
    raw = value if isinstance(value, str) else ""
    s = re.sub(r"\s+", " ", raw).strip()[:80]
    s = re.sub(r"(?i)^county of\s+", "", s)
    s = re.sub(r"(?i)\s+county$", "", s)
    names = counties()
    found = {c.lower(): c for c in names}.get(s.lower())
    if found:
        return found
    if not s:
        raise ValueError("county is required: the California county where the job is, e.g. 'Los Angeles' or "
                         "'San Diego'")
    close = difflib.get_close_matches(s.title(), names, n=3, cutoff=0.6)
    shown = re.sub(r"[^\w .'\-]", "", raw)[:60]
    hint = (f" Did you mean {', '.join(close)}?" if close else
            " Use the county the job is in, e.g. 'Los Angeles' or 'San Diego' (put a city in `city`).")
    raise ValueError(f"'{shown}' is not one of California's 58 counties.{hint}")


def _clean_city(city: str | None) -> str | None:
    c = re.sub(r"\s+", " ", city or "").strip()
    if not c:
        return None
    if not CITY_RE.match(c):
        raise ValueError("city must be a city name (letters, spaces, periods, apostrophes and hyphens, up to 80 "
                         "characters), e.g. 'Fresno'")
    return c


def _soft_county(value) -> str | None:
    try:
        return normalize_county(value) if isinstance(value, str) and value.strip() else None
    except ValueError:
        return None


def _soft_city(value) -> str | None:
    try:
        return _clean_city(value) if isinstance(value, str) else None
    except ValueError:
        return None


def resolve_type(type_id: str) -> str:
    """A type id or a handoff alias -> the registry type id; ValueError with the closest ids otherwise."""
    key = re.sub(r"[\s\-]+", "_", (type_id or "").strip().lower())[:80]
    found = aliases().get(key)
    if found:
        return found
    close = difflib.get_close_matches(key, list(aliases()), n=3, cutoff=0.55)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    raise ValueError(f"Unknown professional_type {key!r}.{hint} Call list_professional_types for the ids, or "
                     "describe the need in `need` (e.g. 'stamped structural plans').")


# ---------------------------------------------------------------------------- free-text need -> type
_STOP = set("a an and are as at be by for from has have i if in into is it its me my need needs of on or our so "
            "that the their them this to up us we who will with you your someone somebody person people help get "
            "find hire licensed certified california ca".split())


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1]


def _stem(w: str) -> str:
    return w[:6] if len(w) > 6 else w


@lru_cache(maxsize=1)
def _need_index() -> dict[str, list[tuple[str, float]]]:
    idx: dict[str, list[tuple[str, float]]] = {}
    labels = handoff_data().get("type_labels", {})
    for role, target in aliases().items():
        if role != target and role in labels:
            idx.setdefault(target, []).append((labels[role], 2.0))
    for tid, t in types().items():
        idx.setdefault(tid, [])
        idx[tid] += [(t["name"], 2.0), (tid.replace("_", " "), 2.0), (t.get("plain_description", ""), 0.4)]
    return idx


def match_need(need: str) -> list[tuple[float, str]]:
    """Score every type against a plain-words need: whole trigger phrases count most, then shared words in the
    name, aliases and description. Best first; only types that share something with the need."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", (need or "").lower()) + " "
    want = {_stem(w) for w in _words(need)}
    scored = []
    for tid, t in types().items():
        s = 0.0
        for trig in t.get("triggers", []):
            phrase = " " + re.sub(r"[^a-z0-9]+", " ", trig.lower()).strip() + " "
            if phrase.strip() and phrase in text:
                s += 3.0 + len(phrase.split())
        for chunk, weight in _need_index()[tid]:
            s += weight * len(want & {_stem(w) for w in _words(chunk)})
        if s > 0:
            scored.append((round(s, 2), tid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored


# ---------------------------------------------------------------------------- directory links
def _filled_pattern(t: dict, f: dict, county: str | None, craft: str | None) -> str | None:
    """The link's county URL pattern filled in, or None when it has none, the page wasn't reachable when checked,
    or a placeholder can't be filled (e.g. the DIR apprenticeship search needs a craft as well as a county)."""
    pattern = f.get("url_pattern_with_county")
    if not pattern or f.get("status") != "reachable" or not county:
        return None
    params = f.get("url_pattern_params") or {}
    values = {}
    for name in re.findall(r"{(\w+)}", pattern):
        if name == "county":
            tables = registry().get("county_codes", {})
            hint = params.get("county", "")
            table = next((v for k, v in tables.items() if k in hint), None)
            if table is None and len(tables) == 1:
                table = next(iter(tables.values()))
            code = (table or {}).get(county)
            if not code:
                return None
            values[name] = quote_plus(code)
        elif name == "craft":
            code = craft_code(craft, t.get("das_craft_codes") or {})
            if not code:
                return None
            values[name] = quote_plus(code)
        else:
            return None
    url = pattern.format(**values)
    # the filled link must stay on the official search page's own site
    return url if official_url(url) and _host(url) == _host(f.get("url", "")) else None


OFFICIAL_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#][^\s\"'<>\\`]*)?$")


def official_url(url) -> bool:
    """Only plain https addresses become links (never javascript:, data:, http: or one with spaces or quotes)."""
    return isinstance(url, str) and bool(OFFICIAL_URL_RE.match(url))


def _host(url: str) -> str:
    return re.sub(r"^https://([^/?#:]+).*$", r"\1", url or "", flags=re.S).lower()


def craft_code(craft: str | None, codes: dict[str, str]) -> str | None:
    """A craft name or code ('carpenter', 'Electrical & Electronic', '22') -> the DIR craft code, when exactly one
    craft matches best."""
    c = (craft or "").strip().lower()
    if not c or not codes:
        return None
    if c in {v.lower() for v in codes.values()}:
        return next(v for v in codes.values() if v.lower() == c)
    for name, code in codes.items():
        if name.lower() == c:
            return code
    words = [w for w in _words(c) if len(w) >= 4]
    hits: dict[int, set] = {}
    for name, code in codes.items():           # longest shared word start wins: carpenter -> Carpentry, not Carpet
        n = max((len(os.path.commonprefix([w, x])) for w in words for x in _words(name)), default=0)
        if n >= 4:
            hits.setdefault(n, set()).add(code)
    if not hits:
        return None
    best = hits[max(hits)]
    return next(iter(best)) if len(best) == 1 else None


def _how_to_search(f: dict, filtered: bool, county: str | None, city: str | None) -> str:
    by = [s.lower() for s in f.get("search_by") or []]
    checked = f.get("status") == "reachable"
    if filtered:
        craft = " and the craft you gave" if "{craft}" in (f.get("url_pattern_with_county") or "") else ""
        return f"Opens the official list already filtered to {county} County{craft}."
    parts = []
    if not checked:
        parts.append("TradeComply could not open this page when it was last checked, so it may have moved.")
    if not by:
        return " ".join(parts + (["Information page; it has no search."] if checked else []))
    hint = []
    if county and any("county" in s for s in by):
        hint.append(f"choose {county} County" + (" and your craft" if any("craft" in s for s in by) else ""))
    if city and any("city" in s for s in by):
        hint.append(f"type {city} in the city field")
    if county and not hint and any(s.startswith("region") for s in by):
        hint.append(f"find the region that includes {county} County")
    if hint:
        parts.append(("On the page, " if checked else "It may let you ") + " and ".join(hint) + ".")
    else:
        parts.append(("Search by " if checked else "It may let you search by ") + ", ".join(f["search_by"]) + ".")
    return " ".join(parts)


class Directory(BaseModel):
    label: str = Field(description="Link text naming the official page")
    official_owner: str = Field(description="The agency or body that runs the page")
    url: str = Field(description="Where to send the user: the county-filtered list when county_filtered, else the "
                                 "official search page")
    search_page_url: str = Field(description="The official search page without filters")
    county_filtered: bool
    how_to_search: str
    search_by: list[str] = Field(default_factory=list)
    link_checked: bool = Field(description="True when the page was opened and showed this search on last_checked; "
                                           "False when it could not be opened from TradeComply's research network "
                                           "(the address may have moved)")
    status: str = Field(description="The registry's status: 'reachable', or 'blocked:<reason>' when the page could "
                                    "not be opened")
    last_checked: Optional[str] = None
    only_for: Optional[str] = Field(None, description="Set when the page serves one city or district only, e.g. "
                                                      "'City of Los Angeles jobs only'")
    applies_here: bool = Field(True, description="False when only_for names a different county or city than the "
                                                 "job's")
    note: str = ""


def _scope(url: str) -> dict:
    return handoff_data().get("link_scopes", {}).get(url) or {}


def _applies(scope: dict, county: str | None, city: str | None) -> bool:
    if scope.get("county") and county and scope["county"] != county:
        return False
    if scope.get("city") and city and scope["city"].lower() != city.lower():
        return False
    return True


def _directory(t: dict, f: dict, county: str | None, city: str | None, craft: str | None) -> Directory:
    filled = _filled_pattern(t, f, county, craft)
    scope = _scope(f["url"])
    applies = _applies(scope, county, city)
    how = _how_to_search(f, bool(filled), county, city)
    if scope.get("label"):
        how = f"{scope['label']}. {how}".strip() if applies else f"{scope['label']}: not for this job."
    return Directory(label=f["label"], official_owner=f.get("official_owner") or "", url=filled or f["url"],
                     search_page_url=f["url"], county_filtered=bool(filled), how_to_search=how,
                     search_by=f.get("search_by") or [], link_checked=f.get("status") == "reachable",
                     status=f.get("status") or "unknown", last_checked=f.get("fetched"),
                     only_for=scope.get("label"), applies_here=applies,
                     note=f.get("note") or "")


def directories(type_id: str, county: str | None = None, city: str | None = None,
                craft: str | None = None) -> list[Directory]:
    """The type's official directories in the registry's order, those that serve this job's place first."""
    t = types()[type_id]
    finds = [f for f in t.get("find", []) if official_url(f.get("url"))]
    if len(finds) < len(t.get("find", [])):
        logger.error("professional type %s: a find link is not a plain https address and was left out", type_id)
    dirs = [_directory(t, f, county, city, craft) for f in finds]
    return [d for d in dirs if d.applies_here] + [d for d in dirs if not d.applies_here]


def primary_directory(type_id: str, county: str | None = None, city: str | None = None,
                      craft: str | None = None, prefer: str | None = None) -> Directory | None:
    """The one link to show beside a need: a county-filtered list if the official site supports one, else the
    preferred page for this role (handoffs.json role_find / prefer_find), else the type's first page. Pages for
    another city or district are never chosen. None when no public directory exists (a role you designate, or a
    credential checked on the card)."""
    dirs = [d for d in directories(type_id, county, city, craft) if d.applies_here]
    for d in dirs:
        if d.county_filtered:
            return d
    for d in dirs:
        if prefer and d.search_page_url == prefer:
            return d
    return dirs[0] if dirs else None


def directory_status(type_id: str) -> str:
    finds = types()[type_id].get("find", [])
    if not finds:
        return "none"
    return "checked" if any(f.get("status") == "reachable" for f in finds) else "unchecked"


# ---------------------------------------------------------------------------- find_licensed_professional
class LegalBasis(BaseModel):
    situation: str
    legal_basis: str
    status: Literal["confirmed", "unconfirmed"] = Field(description="confirmed: read on an official source on the "
                                                                    "check date; unconfirmed: not read, say so")
    source_url: Optional[str] = None
    rule_ids: list[str] = Field(default_factory=list)
    verified: bool = Field(description="confirmed, and every rule id is confirmed or corrected in the rulebook")
    note: str = ""


class OtherType(BaseModel):
    type_id: str
    name: str


class FindProfessionalResult(Result):
    type_id: str
    name: str
    plain_description: str
    matched_by: str = Field(description="How the type was chosen: the type id given, or the words of `need`")
    other_possible_types: list[OtherType] = Field(default_factory=list)
    county: Optional[str] = Field(None, description="The job's county, when given (links are statewide otherwise)")
    city: Optional[str] = None
    craft: Optional[str] = None
    when_required: list[LegalBasis] = Field(description="Situations where the law requires this professional")
    not_required_when: list[LegalBasis] = Field(default_factory=list)
    optional_when: list[LegalBasis] = Field(default_factory=list, description="Suggested, not required by law")
    directories: list[Directory]
    directory_status: Literal["checked", "unchecked", "none"] = Field(
        description="checked: at least one official page was opened on its check date; unchecked: official pages "
                    "could not be opened from TradeComply's research network; none: no public directory exists")
    verify_license_how: str
    bring_or_ask: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    do_not_say: list[str] = Field(default_factory=list, description="For AI assistants: claims never to make")
    neutral_note: str = NEUTRAL_NOTE
    checked_on: str


def _basis(b: dict) -> LegalBasis:
    rids = [r for r in (b.get("rule_ids") or []) if isinstance(r, str)]
    status = "confirmed" if b.get("status") == "confirmed" else "unconfirmed"
    return LegalBasis(situation=b["situation"], legal_basis=b["legal_basis"], status=status,
                      source_url=b.get("source_url"), rule_ids=rids, note=b.get("note") or "",
                      verified=status == "confirmed" and all(rule_verified(r) for r in rids))


def find_licensed_professional(professional_type: str | None = None, county: str | None = None,
                               city: str | None = None, need: str | None = None,
                               craft: str | None = None) -> FindProfessionalResult:
    """county is optional (links are then statewide) but must be one of the 58 counties when given."""
    county_name = normalize_county(county) if isinstance(county, str) and county.strip() else None
    city_name = _clean_city(city)
    craft = (craft or "").strip()[:80] or None
    others: list[OtherType] = []
    if professional_type and professional_type.strip():
        tid = resolve_type(professional_type)
        how = f"professional_type {professional_type.strip()[:80]!r}"
    else:
        text = (need or "").strip()
        if not text:
            raise ValueError("Give professional_type (an id from list_professional_types) or need (what you need in "
                             "plain words, e.g. 'stamped plans for a retaining wall' or 'a lawyer to foreclose a "
                             "lien').")
        if len(text) > MAX_NEED:
            raise ValueError(f"need is too long (max {MAX_NEED} characters)")
        scored = match_need(text)
        if not scored:
            names = "; ".join(f"{t['name']} ({tid})" for tid, t in types().items())
            raise ValueError(f"No professional type matches {text[:80]!r}. Types: {names}.")
        tid = scored[0][1]
        top = scored[0][0]
        others = [OtherType(type_id=o, name=types()[o]["name"]) for s, o in scored[1:4] if s >= top * 0.7]
        how = f"need {text[:80]!r}"
    t = types()[tid]
    req = [_basis(b) for b in t.get("when_required", [])]
    out = FindProfessionalResult(
        type_id=tid, name=t["name"], plain_description=t.get("plain_description", ""), matched_by=how,
        other_possible_types=others, county=county_name, city=city_name, craft=craft, when_required=req,
        not_required_when=[_basis(b) for b in t.get("not_required_when", [])],
        optional_when=[_basis(b) for b in t.get("optional_when", [])],
        directories=directories(tid, county_name, city_name, craft), directory_status=directory_status(tid),
        verify_license_how=t.get("verify_license_how", ""), bring_or_ask=list(t.get("bring_or_ask", [])),
        notes=list(t.get("notes", [])), do_not_say=list(t.get("never_say", [])), checked_on=checked_on())
    known = _rule_status()
    out.cite(*[r for b in req for r in b.rule_ids if r in known])
    out.verified = bool(req) and all(b.verified for b in req) and out.verified
    return out


# ---------------------------------------------------------------------------- list_professional_types
class DirectoryLink(BaseModel):
    label: str
    url: str
    official_owner: str
    link_checked: bool
    county_filter_supported: bool


class ProfessionalType(BaseModel):
    type_id: str
    name: str
    plain_description: str
    also_called: list[str] = Field(default_factory=list, description="Other ids (from tool results) for this type")
    legally_required_situations: int
    directory_status: Literal["checked", "unchecked", "none"]
    directories: list[DirectoryLink]


class ProfessionalTypesResult(BaseModel):
    types: list[ProfessionalType]
    counties: list[str]
    checked_on: str
    note: str = NEUTRAL_NOTE


def list_professional_types() -> ProfessionalTypesResult:
    alias_of: dict[str, list[str]] = {}
    for role, target in aliases().items():
        if role != target:
            alias_of.setdefault(target, []).append(role)
    rows = []
    for tid, t in types().items():
        rows.append(ProfessionalType(
            type_id=tid, name=t["name"], plain_description=t.get("plain_description", ""),
            also_called=sorted(alias_of.get(tid, [])), legally_required_situations=len(t.get("when_required", [])),
            directory_status=directory_status(tid),
            directories=[DirectoryLink(label=f["label"], url=f["url"], official_owner=f.get("official_owner") or "",
                                       link_checked=f.get("status") == "reachable",
                                       county_filter_supported=bool(f.get("url_pattern_with_county"))
                                       and f.get("status") == "reachable") for f in t.get("find", [])
                         if official_url(f.get("url"))]))
    return ProfessionalTypesResult(types=rows, counties=counties(), checked_on=checked_on())


# ---------------------------------------------------------------------------- handoff tests (restricted evaluator)
class MissingField(Exception):
    """A handoff test read a field the result doesn't have."""


class _View:
    """A dict seen through a handoff test: keys read as attributes or items; a missing key is MissingField."""
    __slots__ = ("_d",)

    def __init__(self, d: dict):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, key):
        try:
            return _wrap(self._d[key])
        except (KeyError, TypeError):
            raise MissingField(key) from None

    def get(self, key, default=None):
        return _wrap(self._d.get(key, default))

    def __contains__(self, key):
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def __bool__(self):
        return bool(self._d)

    def __eq__(self, other):
        return self._d == (other._d if isinstance(other, _View) else other)

    __hash__ = None


def _wrap(v):
    if isinstance(v, dict):
        return _View(v)
    if isinstance(v, list):
        return [_wrap(x) for x in v]
    return v


def _unwrap(v):
    if isinstance(v, _View):
        return {k: _unwrap(x) for k, x in v._d.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


class _Json:
    @staticmethod
    def dumps(v, **_):
        return json.dumps(_unwrap(v), default=str)


_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare, ast.Eq, ast.NotEq,
          ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Call, ast.Attribute,
          ast.Subscript, ast.Name, ast.Load, ast.Store, ast.Constant, ast.Tuple, ast.List, ast.GeneratorExp,
          ast.comprehension, ast.IfExp)
_FUNCS = {"len": len, "any": any, "all": all}
_STR_METHODS = {"startswith", "endswith", "lower", "upper", "strip"}
_METHODS = _STR_METHODS | {"get", "dumps"}
_GLOBALS = {"result", "args", "json"} | set(_FUNCS)
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
            ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
            ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not}


def compile_test(expr: str) -> ast.Expression:
    """Parse a handoff test and check it uses only comparisons, boolean logic, generator expressions, field reads,
    len/any/all and a few string methods: no imports, no dunders, no other calls, no arithmetic. The tree is then
    walked by _run; nothing is ever compiled or evaluated as Python code."""
    tree = ast.parse(expr, mode="eval")
    bound = {n.id for c in ast.walk(tree) if isinstance(c, ast.comprehension) for n in ast.walk(c.target)
             if isinstance(n, ast.Name)}
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            raise ValueError(f"handoff test uses {type(node).__name__}: {expr}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"handoff test reads a private attribute: {expr}")
        if isinstance(node, ast.Name) and node.id not in _GLOBALS | bound:
            raise ValueError(f"handoff test uses an unknown name {node.id!r}: {expr}")
        if isinstance(node, ast.comprehension) and (node.is_async or not all(
                isinstance(n, (ast.Name, ast.Tuple, ast.Store)) for n in ast.walk(node.target))):
            raise ValueError(f"handoff test has a loop it can't run: {expr}")
        if isinstance(node, ast.Call):
            f = node.func
            ok = (isinstance(f, ast.Name) and f.id in _FUNCS) or (isinstance(f, ast.Attribute) and f.attr in _METHODS)
            if not ok or node.keywords:
                raise ValueError(f"handoff test makes a call that is not allowed: {expr}")
    return tree


def _bind(target, value, env: dict) -> dict:
    env = dict(env)
    if isinstance(target, ast.Name):
        env[target.id] = value
    else:                                       # a tuple target: for k, v in ...
        values = list(value)
        if len(values) != len(target.elts):
            raise ValueError("cannot unpack")
        for t, v in zip(target.elts, values):
            env = _bind(t, v, env)
    return env


def _loop(gens: list, i: int, elt, env: dict):
    g = gens[i]
    for value in _run(g.iter, env):
        inner = _bind(g.target, value, env)
        if all(_run(cond, inner) for cond in g.ifs):
            if i + 1 == len(gens):
                yield _run(elt, inner)
            else:
                yield from _loop(gens, i + 1, elt, inner)


def _run(node, env: dict):
    """Evaluate a checked handoff test tree. Only the node types compile_test allows reach here."""
    t = type(node)
    if t is ast.Expression:
        return _run(node.body, env)
    if t is ast.Constant:
        return node.value
    if t is ast.Name:
        return env[node.id]
    if t is ast.Tuple:
        return tuple(_run(e, env) for e in node.elts)
    if t is ast.List:
        return [_run(e, env) for e in node.elts]
    if t is ast.BoolOp:
        value = None
        for part in node.values:
            value = _run(part, env)
            if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                return value
        return value
    if t is ast.UnaryOp:                        # only `not`
        return not _run(node.operand, env)
    if t is ast.Compare:
        left = _run(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _run(comp, env)
            if not _COMPARE[type(op)](left, right):
                return False
            left = right
        return True
    if t is ast.IfExp:
        return _run(node.body, env) if _run(node.test, env) else _run(node.orelse, env)
    if t is ast.Subscript:
        return _run(node.value, env)[_run(node.slice, env)]
    if t is ast.Attribute:                      # field read on the result or the arguments only
        obj = _run(node.value, env)
        if not isinstance(obj, _View):
            raise MissingField(node.attr)
        return obj[node.attr]
    if t is ast.GeneratorExp:
        return _loop(node.generators, 0, node.elt, env)
    if t is ast.Call:
        args = [_run(a, env) for a in node.args]
        f = node.func
        if isinstance(f, ast.Name):
            return _FUNCS[f.id](*args)
        obj = _run(f.value, env)
        if f.attr in _STR_METHODS and isinstance(obj, str):
            return getattr(obj, f.attr)(*args)
        if f.attr == "get" and isinstance(obj, _View):
            return obj.get(*args)
        if f.attr == "dumps" and obj is _Json:
            return _Json.dumps(*args)
        raise MissingField(f.attr)
    raise ValueError(f"handoff test node {t.__name__} is not allowed")


@lru_cache(maxsize=1)
def _compiled() -> dict[str, ast.Expression]:
    out = {}
    for e in handoffs():
        try:
            out[e["id"]] = compile_test(e["test"])
        except (SyntaxError, ValueError) as ex:
            logger.error("handoff %s disabled: %s", e["id"], ex)
    return out


@lru_cache(maxsize=1)
def _compiled_conditional_if() -> dict[str, ast.Expression]:
    out = {}
    for e in handoffs():
        if e.get("conditional_if"):
            try:
                out[e["id"]] = compile_test(e["conditional_if"])
            except (SyntaxError, ValueError) as ex:
                logger.error("handoff %s conditional_if disabled: %s", e["id"], ex)
    return out


def evaluate(entry: dict, result: dict, args: dict | None = None) -> bool:
    """True if the entry's test holds for this result. MissingField when the result lacks a field the test reads;
    other exceptions mean the result has an unexpected shape."""
    tree = _compiled().get(entry["id"])
    if tree is None:
        raise ValueError(f"handoff {entry['id']} has no valid test")
    env = {"json": _Json, "result": _wrap(result), "args": _wrap(args or {}), **_FUNCS}
    return bool(_run(tree, env))


def effective_requirement(entry: dict, result: dict, args: dict | None = None) -> str:
    """The entry's requirement for this result. A 'required' entry with a `conditional_if` test is shown as
    'conditional' when that test holds (the tool itself left a deciding fact open, e.g. its own status for the item
    is 'conditional'), or when the test can't be read: never say "required by law" when the result doesn't show it."""
    req = entry["requirement"]
    if req != "required" or not entry.get("conditional_if"):
        return req
    tree = _compiled_conditional_if().get(entry["id"])
    if tree is None:
        return "conditional"
    env = {"json": _Json, "result": _wrap(result), "args": _wrap(args or {}), **_FUNCS}
    try:
        return "conditional" if _run(tree, env) else "required"
    except Exception:                               # noqa: BLE001 - an unreadable shape is not proof of "required"
        return "conditional"


# ---------------------------------------------------------------------------- who_must_sign
class HandoffReason(BaseModel):
    handoff_id: str
    requirement: Literal["required", "conditional", "recommended", "optional"]
    requirement_label: str
    why: str
    legal_basis: str
    verified: bool
    when: str
    due_date: Optional[str] = None
    rule_ids: list[str] = Field(default_factory=list)


class HandoffItem(BaseModel):
    """One licensed or certified person a result needs. The UI contract: type_id, name, why, legal_basis,
    verified, find_url and county_filtered are always present."""
    type_id: str = Field(description="Professional type for find_licensed_professional / /api/professionals/find")
    name: str = Field(description="Who, in plain words")
    why: str = Field(description="One plain sentence: why this result needs them")
    legal_basis: str = Field(description="The law behind it; parts not yet confirmed say so")
    verified: bool = Field(description="Every rule cited is confirmed in the rulebook and no part of the basis is "
                                       "unconfirmed")
    find_url: Optional[str] = Field(description="Official search page (None when no public directory exists)")
    county_filtered: bool = Field(description="find_url is already filtered to the job's county")
    find_label: Optional[str] = Field(None, description="Link text naming the official page")
    find_owner: Optional[str] = Field(None, description="The agency or body that runs the page")
    find_link_checked: bool = Field(False, description="The page was opened on its check date; False: it could not "
                                                       "be opened from TradeComply's research network")
    check_how: str = Field("", description="How to check this person's license or credential before hiring")
    requirement: Literal["required", "conditional", "recommended", "optional"]
    requirement_label: str = Field(description="Plain label, e.g. 'Required by law' or 'Suggested, not required "
                                               "by law'")
    when: str
    due_date: Optional[str] = Field(None, description="ISO date from the result, when the tool computed one")
    rule_ids: list[str] = Field(default_factory=list)
    role_id: str = Field(description="The specific role (e.g. asbestos_abatement_contractor); unique in a list")
    handoff_id: str
    also: list[HandoffReason] = Field(default_factory=list, description="Other reasons the same result needs this "
                                                                        "person")


class WhoMustSignResult(Result):
    tool: str
    handoffs: list[HandoffItem]
    ran_tool: bool = Field(description="True when the tool was run here from its inputs")
    mapped_conditions: int = Field(description="How many handoff conditions exist for this tool")
    could_not_check: list[str] = Field(default_factory=list, description="Handoff ids whose condition could not be "
                                                                         "read from this result (missing fields)")
    county: Optional[str] = None
    note: str = NEUTRAL_NOTE
    checked_on: str


def entry_verified(entry: dict) -> bool:
    rids = list(entry.get("rule_ids") or [])
    return bool(rids) and entry.get("basis_complete", True) and all(rule_verified(r) for r in rids)


def requirement_label(requirement: str, verified: bool) -> str:
    base = {"required": "Required by law", "conditional": "Required by law if this applies",
            "recommended": "Suggested, not required by law", "optional": "Optional"}.get(requirement, "Suggested")
    if requirement in ("required", "conditional") and not verified:
        base += " (legal basis not yet verified)"
    return base


def _due_date(entry: dict, result: dict) -> str | None:
    path = entry.get("due_field")
    cur: Any = result
    for part in (path or "").split(".") if path else []:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if path and isinstance(cur, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cur) else None


def _craft_from(args: dict) -> str | None:
    if isinstance(args.get("craft"), str):
        return args["craft"]
    by = args.get("journeyman_hours_by_craft")
    if isinstance(by, dict) and len(by) == 1:
        k = next(iter(by))
        return k if isinstance(k, str) else None
    return None


def _unwrap_result(result) -> dict:
    if isinstance(result, dict) and set(result) == {"result"} and isinstance(result["result"], dict):
        return result["result"]
    if isinstance(result, dict):
        return result
    return {"result": result}


def evaluate_tool(tool: str, result: dict, args: dict | None = None,
                  county: str | None = None, city: str | None = None) -> tuple[list[HandoffItem], int, list[str]]:
    """(items, number of conditions for the tool, ids that could not be checked). One item per role, strongest
    requirement first; a role triggered for several reasons lists the others under `also`."""
    args = args if isinstance(args, dict) else {}
    result = _unwrap_result(result)
    entries = [e for e in handoffs() if e["tool"] == tool]
    county = county or _soft_county(args.get("county"))
    city = city or _soft_city(args.get("city"))
    craft = _craft_from(args)
    fired, skipped = [], []
    for e in entries:
        try:
            if evaluate(e, result, args):
                req = effective_requirement(e, result, args)
                fired.append(e if req == e["requirement"] else {**e, "requirement": req})
        except MissingField:
            skipped.append(e["id"])
        except Exception as ex:                     # an unexpected shape: never fail the caller over it
            logger.debug("handoff %s not evaluated: %s", e["id"], ex)
            skipped.append(e["id"])
    groups: dict[str, list[dict]] = {}
    for e in fired:
        groups.setdefault(e["professional_type_id"], []).append(e)
    items = []
    for role, es in groups.items():
        es = sorted(es, key=lambda e: REQUIREMENT_ORDER.index(e["requirement"]))
        tid = aliases().get(role)
        if tid is None:
            logger.warning("handoff role %s has no professional type", role)
            continue
        prefer = next((e["prefer_find"] for e in es if e.get("prefer_find")), None) or \
            handoff_data().get("role_find", {}).get(role)
        link = primary_directory(tid, county, city, craft, prefer)

        def reason(e):
            v = entry_verified(e)
            return dict(handoff_id=e["id"], requirement=e["requirement"],
                        requirement_label=requirement_label(e["requirement"], v), why=e["why"],
                        legal_basis=e.get("legal_basis_plain") or e["legal_basis"], verified=v,
                        when=e.get("when_plain") or e["when"], due_date=_due_date(e, result),
                        rule_ids=list(e.get("rule_ids") or []))
        first = reason(es[0])
        items.append(HandoffItem(
            type_id=tid, name=type_label(role) or types()[tid]["name"], role_id=role,
            find_url=link.url if link else None, county_filtered=bool(link and link.county_filtered),
            find_label=link.label if link else None, find_owner=link.official_owner if link else None,
            find_link_checked=bool(link and link.link_checked),
            check_how=types()[tid].get("verify_license_how", ""), also=[HandoffReason(**reason(e)) for e in es[1:]],
            **first))
    order = {e["professional_type_id"]: i for i, e in reversed(list(enumerate(fired)))}
    items.sort(key=lambda h: (REQUIREMENT_ORDER.index(h.requirement), order[h.role_id]))
    return items, len(entries), skipped


def handoffs_for(tool: str, result, args: dict | None = None) -> list[dict]:
    """The web app's hook: the `handoffs` list for a /api/tools/{name} result ([] when none apply)."""
    if not isinstance(result, (dict, list)) or tool not in mapped_tools():
        return []
    items, _, _ = evaluate_tool(tool, result, args)
    return [h.model_dump() for h in items]


@lru_cache(maxsize=1)
def mapped_tools() -> frozenset:
    return frozenset(e["tool"] for e in handoffs())


def _too_big(value, limit: int = MAX_RESULT_NODES) -> bool:
    stack, n = [value], 0
    while stack:
        v = stack.pop()
        n += 1
        if n > limit:
            return True
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def _parse_result(result, *, trusted: bool = False) -> dict:
    """The caller's result as a dict. `trusted`: this server's own tool just made it (no size cap)."""
    if isinstance(result, str):
        if len(result) > MAX_RESULT_JSON:
            raise ValueError(f"result is too long (max {MAX_RESULT_JSON} characters of JSON)")
        try:
            result = json.loads(result)
        except (ValueError, RecursionError):
            raise ValueError("result must be the tool's JSON output (an object), or that JSON as text") from None
    if not isinstance(result, (dict, list)):
        raise ValueError("result must be the tool's JSON output (an object)")
    if not trusted and _too_big(result):
        raise ValueError(f"result is too large (more than {MAX_RESULT_NODES:,} values). Pass the tool's arguments "
                         "instead, so the tool is run here.")
    return _unwrap_result(result)


def who_must_sign(tool_name: str, result=None, arguments: dict | None = None, county: str | None = None,
                  city: str | None = None, ran_tool: bool = False) -> WhoMustSignResult:
    """Pure version (no tool run): evaluate a result you already have."""
    tool = (tool_name or "").strip()
    if not TOOL_NAME_RE.match(tool):
        raise ValueError("tool_name must be a TradeComply tool name, e.g. 'calculate_lien_deadline'")
    if result is None:
        raise ValueError("Pass the tool's result, or its arguments so the tool can be run")
    data = _parse_result(result, trusted=ran_tool)
    county_name = normalize_county(county) if county and county.strip() else None
    items, mapped, skipped = evaluate_tool(tool, data, arguments, county_name, _clean_city(city))
    args = arguments if isinstance(arguments, dict) else {}
    if not mapped:
        note = "No licensed-professional handoffs are mapped for this tool. " + NEMPTY + NEUTRAL_NOTE
    elif not items:
        note = ("None of the conditions TradeComply checks for this tool applied to this result"
                + (", and some could not be checked" if skipped else "") + ". " + NEMPTY + NEUTRAL_NOTE)
    else:
        note = NEUTRAL_NOTE
    out = WhoMustSignResult(tool=tool, handoffs=items, ran_tool=ran_tool, mapped_conditions=mapped,
                            could_not_check=skipped, county=county_name or _soft_county(args.get("county")),
                            note=note, checked_on=handoff_data().get("checked", ""))
    known = _rule_status()
    rids = [r for h in items for x in [h, *h.also] for r in x.rule_ids if r in known]
    out.cite(*dict.fromkeys(rids))
    out.verified = bool(items) and all(h.verified and all(a.verified for a in h.also) for h in items) and out.verified
    return out


# ---------------------------------------------------------------------------- running a tool from its inputs
def _caller_may_run(mcp_tool_name: str, py_module: str | None, read_only: bool) -> bool:
    """May the caller of who_must_sign run this tool itself? Over the web app the API sets RUN_GUARD. Over /mcp,
    an API key's tool scope is read from the request the MCP gate (app/auth.py) annotated; stdio and in-process
    callers without either already run every tool."""
    guard = RUN_GUARD.get()
    if guard is not None:
        return bool(guard(mcp_tool_name))
    try:
        from fastmcp.server.dependencies import get_http_request
        request = get_http_request()
    except Exception:                       # stdio, or no HTTP request behind this call
        return True
    principal = (request.scope.get("state") or {}).get("tc_principal")
    scopes = getattr(principal, "scopes", None)
    if not scopes or not ("tools" in scopes or "categories" in scopes):
        return read_only                    # the default key scope: every read-only tool
    try:                                    # the platform is running (it set tc_principal): use its own rules
        from app.api import module_for
        from app.mcp_governance import scope_allows
    except Exception:
        return mcp_tool_name in scopes.get("tools", ())
    index = {mcp_tool_name: {"category": module_for(mcp_tool_name, py_module), "read_only": read_only}}
    return scope_allows(scopes, mcp_tool_name, index)


async def _run_tool(mcp, tool: str, arguments: dict) -> dict:
    if tool == "who_must_sign":
        raise ValueError("who_must_sign can't run itself")
    try:
        found = await mcp.get_tool(tool)
    except Exception:
        found = None
    if found is None:
        raise ValueError(f"No tool named {tool!r} on this server")
    ann = getattr(found, "annotations", None)
    hints = ann.model_dump(by_alias=True, exclude_none=True) if hasattr(ann, "model_dump") else {}
    read_only = hints.get("readOnlyHint") is True
    fn = getattr(found, "fn", None)
    if not read_only:
        raise ValueError(f"{tool} is not a read-only tool; pass its result instead of its arguments")
    if not _caller_may_run(tool, getattr(fn, "__module__", None), read_only):
        raise ValueError(f"This API key is not allowed to use the tool {tool!r}. Pass that tool's result instead, "
                         "or ask an owner or office user to add the tool to the key in Settings > AI assistant (MCP)"
                         " > API keys.")
    try:
        res = await mcp.call_tool(tool, arguments)
    except Exception as ex:
        raise ValueError(f"Could not run {tool} with these arguments: {str(ex)[:600]}") from None
    data = getattr(res, "structured_content", None)
    if data is None:
        text = "\n".join(getattr(b, "text", "") for b in (getattr(res, "content", None) or []))
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError(f"{tool} did not return a JSON result") from None
    return _unwrap_result(data)


# ---------------------------------------------------------------------------- MCP registration
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    type_ids = ", ".join(types())

    @mcp.tool(name="find_licensed_professional", annotations=ro)
    def find_licensed_professional_tool(
        county: Annotated[Optional[str], Field(description="California county where the job is, e.g. 'Fresno' or "
                                                           "'San Diego' (one of the 58 counties); without it the links "
                                                           "are statewide")] = None,
        professional_type: Annotated[Optional[str], Field(description=f"Type id, e.g. 'attorney', 'architect', "
                                                                      f"'qsd_qsp'. Ids: {type_ids}")] = None,
        need: Annotated[Optional[str], Field(description="Or what you need in plain words, e.g. 'stamped plans for "
                                                         "a retaining wall', 'a lawyer to foreclose a lien', "
                                                         "'someone to write our SWPPP'")] = None,
        city: Annotated[Optional[str], Field(description="City of the job, used where the official search can "
                                                         "filter by city")] = None,
        craft: Annotated[Optional[str], Field(description="Apprenticeship only: the craft, e.g. 'carpenter' or "
                                                          "'electrical', so the DIR list can be filtered to the "
                                                          "county and craft")] = None,
    ) -> FindProfessionalResult:
        """Find and check a licensed or certified professional near a California job: what they do, when the law
        requires one (each legal basis cited, with confirmed/unconfirmed status), the official public directories
        to search (filtered to the county only where the official site supports it; otherwise the search page and
        what to type), how to check the license before hiring, and what to bring or ask.

        Never recommend, rank or name a specific person or firm: relay the official links and the neutral note.
        Say when a link could not be checked (link_checked=false) or a legal basis is unconfirmed."""
        return find_licensed_professional(professional_type, county, city, need, craft)

    @mcp.tool(name="who_must_sign", annotations=ro)
    async def who_must_sign_tool(
        tool_name: Annotated[str, Field(description="The TradeComply tool whose result to check, e.g. "
                                                    "'calculate_lien_deadline', 'calgreen_and_stormwater', "
                                                    "'project_setup_checklist'")],
        result: Annotated[Optional[Union[dict, str]], Field(description="That tool's result (the JSON object it "
                                                                         "returned), or the JSON as text")] = None,
        arguments: Annotated[Optional[dict], Field(description="Or that tool's inputs: the tool is run here with "
                                                               "them (read-only tools only)")] = None,
        county: Annotated[Optional[str], Field(description="County of the job, to filter directory links where "
                                                           "the official site supports it")] = None,
        city: Annotated[Optional[str], Field(description="City of the job")] = None,
    ) -> WhoMustSignResult:
        """List the licensed or certified people a TradeComply result needs (attorney, engineer, architect,
        QSD/QSP, HERS rater, certified asbestos consultant, crane certifier and others): for each, why (one plain
        sentence), the legal basis and whether it is verified, whether the law requires them or only suggests
        them, when, and a link to the official directory to find one.

        Pass the tool's result, or its arguments to run it here. Show the user every item with its requirement
        label; never say "required by law" for a suggested one, never recommend a specific person or firm, and never
        tell the user an empty list means no licensed person is needed (relay the note instead)."""
        ran = False
        if result is None:
            if arguments is None:
                raise ValueError("Pass the tool's result, or its arguments so the tool can be run")
            name = (tool_name or "").strip()
            if not TOOL_NAME_RE.match(name):
                raise ValueError("tool_name must be a TradeComply tool name, e.g. 'calculate_lien_deadline'")
            if name in mapped_tools():
                result, ran = await _run_tool(mcp, name, arguments), True
            else:
                result = {}
        # Parsing and walking a large result is CPU work: on a worker thread, so other requests keep being served.
        import anyio        # local import: a dependency of the MCP server, not of the rules package
        return await anyio.to_thread.run_sync(
            lambda: who_must_sign(tool_name, result, arguments, county, city, ran_tool=ran))

    @mcp.tool(name="list_professional_types", annotations=ro)
    def list_professional_types_tool() -> ProfessionalTypesResult:
        """Every kind of licensed or certified professional TradeComply can point to (id, name, plain description,
        other ids tool results use for it, how many situations legally require one), the official directories
        for each (whether the link was checked and whether it can be filtered by county), and the 58 county names
        find_licensed_professional accepts."""
        return list_professional_types()
