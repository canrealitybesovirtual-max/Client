"""Search, field suggestions and the rule page, for people signed in to the web app (docs/quick-help.md).

  POST /api/search          one box for checks, rules, pages, professionals, license classes, integrations and the
                            company's own projects, subs and documents (typed text in the body, never the URL)
  GET  /api/suggest/vocab   the static suggestion lists, the field-to-source map, glossary terms and starters: the
                            same for everyone, no company data and no rule text (ETag, Cache-Control: private,
                            no-cache)
  POST /api/suggest         one company or search-backed suggestion source while someone types
  GET  /api/rules/{id}      one rule for the rule page and the glossary popover (needs a subscription, like checks)

Who: the session cookie only (auth.people_only). API keys get 403, OAuth tokens 401; keys read rules through the
search_california_rules / get_california_rule tools, which are scoped, rate-limited and logged.

The index (tradecomply/search.py) holds only data that is the same for every company. It is built once per process,
on a daemon thread started from the app's lifespan (setting search_warm) or by the first request that needs it, and
shared by every app in the process. Company rows are read per request, scoped by the caller's tenant
(Store.candidates), and matched in Python. Nothing typed is stored or logged; errors are logged by type only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from tradecomply import core
from tradecomply import search as engine

from . import catalog
from .auth import Principal, people_only, platform_of
from .billing import entitlement, require_entitled

log = logging.getLogger("tradecomply.search")
router = APIRouter(prefix="/api", tags=["search"])


# ---------------------------------------------------------------------------- the index: one per process
@dataclass
class Built:
    key: str
    index: engine.Index
    related: dict[str, list[str]]       # rule id -> related check names (spec 5.3)
    build_ms: int


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.key: str | None = None
        self.built: Built | None = None
        self.status = "off"             # off | building | ready | failed
        self.thread: threading.Thread | None = None


STATE = _State()
_RELATED: dict[str, dict[str, list[str]]] = {}      # key -> rule id -> checks (also before the index is ready)


def rules_built_on() -> str:
    with open(core.RULEBOOK_PATH, encoding="utf-8") as f:
        head = f.read(400)
    m = re.search(r'"built_on"\s*:\s*"([^"]+)"', head)
    return m.group(1) if m else _built_on_slow()


def _built_on_slow() -> str:
    with open(core.RULEBOOK_PATH, encoding="utf-8") as f:
        return str(json.load(f).get("built_on") or "")


_BUILT_ON = None


def built_on() -> str:
    global _BUILT_ON
    if _BUILT_ON is None:
        _BUILT_ON = rules_built_on()
    return _BUILT_ON


def index_key(specs: list[dict]) -> str:
    names = ",".join(sorted(s["name"] for s in specs))
    return f"{built_on()}.{hashlib.sha256(names.encode()).hexdigest()[:12]}"


async def _collect(platform) -> tuple[list[dict], dict[str, str]]:
    """The tool specs (api.tool_specs, cached) and each tool's Python module (for related checks)."""
    from . import api
    specs = await api.tool_specs(platform)
    modules = {}
    for s in specs:
        try:
            tool = await platform.mcp.get_tool(s["name"])
            modules[s["name"]] = getattr(getattr(tool, "fn", None), "__module__", None) or ""
        except Exception:      # noqa: BLE001 - a tool without a readable handler just has no related rules
            modules[s["name"]] = ""
    return specs, modules


_ID_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:.\-]*")


def related_map(specs: list[dict], modules: dict[str, str]) -> dict[str, list[str]]:
    """Rule id -> the checks whose handler module names that id literally in its source, narrowed to the checks of
    the vocabulary groups that anchor the rule when any group does ([] when nothing is left). Hidden checks (for AI
    assistants) are never offered."""
    key = index_key(specs)
    if key in _RELATED:
        return _RELATED[key]
    ids = {r.id for r in core.all_rules()}
    by_module: dict[str, set[str]] = {}
    for mod in set(modules.values()):
        m = sys.modules.get(mod)
        path = getattr(m, "__file__", None)
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                by_module[mod] = set(_ID_TOKEN.findall(f.read())) & ids
        except OSError:
            continue
    order = {name: i for i, name in enumerate(n for g in catalog.GROUPS for n in g["tools"])}
    visible = [s["name"] for s in specs if not catalog.TOOLS.get(s["name"], {}).get("hidden")]
    anchored: dict[str, set[str]] = {}
    for g in engine.vocab()["groups"]:
        for rid in g["anchors"]:
            anchored.setdefault(rid, set()).update(g["tools"])
    out: dict[str, list[str]] = {}
    for name in visible:
        for rid in by_module.get(modules.get(name, ""), ()):
            out.setdefault(rid, []).append(name)
    for rid, tools in out.items():
        if rid in anchored:
            tools[:] = [t for t in tools if t in anchored[rid]]
        tools.sort(key=lambda t: (order.get(t, 999), t))
    out = {k: v for k, v in out.items() if v}
    _RELATED[key] = out
    return out


def build_docs(specs: list[dict]) -> list[engine.Doc]:
    from tradecomply import licensing, professionals

    def citation(rid):
        try:
            return core.rule(rid).citation_text
        except KeyError:
            return None
    with open(os.path.join(engine.DATA, "integrations.json"), encoding="utf-8") as f:
        integrations = json.load(f)
    return (engine.page_docs(catalog.PAGES)
            + engine.tool_docs(specs, catalog.TOOLS)
            + engine.rule_docs(core.all_rules())
            + engine.professional_docs(professionals.registry(), professionals.handoff_data())
            + engine.class_docs(licensing.class_table(), citation, engine.cslb_d_subcategories()["items"])
            + engine.integration_docs(integrations))


def _build(specs: list[dict], modules: dict[str, str]) -> Built:
    t0 = time.perf_counter()
    ix = engine.build(build_docs(specs))
    related = related_map(specs, modules)
    return Built(index_key(specs), ix, related, int((time.perf_counter() - t0) * 1000))


def _run_build(key: str, specs: list[dict], modules: dict[str, str]) -> None:
    try:
        built = _build(specs, modules)
    except Exception as e:     # noqa: BLE001 - logged once; search stays in fallback mode, no retry loop
        log.error("search index build failed (%s: %s)", type(e).__name__, str(e)[:200])
        with STATE.lock:
            if STATE.key == key:
                STATE.status = "failed"
        return
    with STATE.lock:
        if STATE.key == key:
            STATE.built, STATE.status = built, "ready"
    log.info("search index ready: %d documents in %d ms", len(built.index.docs), built.build_ms)


async def warm(platform) -> None:
    """Start building the index for this server's tools on a daemon thread, unless the same index is ready or being
    built (one per process; every app in the process shares it). Never raises."""
    try:
        specs, modules = await _collect(platform)
    except Exception as e:     # noqa: BLE001
        log.error("search index: could not read the tool list (%s)", type(e).__name__)
        return
    _start(specs, modules)


def _start(specs: list[dict], modules: dict[str, str]) -> None:
    key = index_key(specs)
    with STATE.lock:
        if STATE.key == key and STATE.status in ("building", "ready", "failed"):
            return
        STATE.key, STATE.status, STATE.built = key, "building", None
        STATE.thread = threading.Thread(target=_run_build, args=(key, specs, modules), daemon=True,
                                        name="search-index")
        STATE.thread.start()


def build_now(platform) -> Built:
    """Tests: build (or reuse) the index synchronously and return it."""
    specs, modules = asyncio.run(_collect(platform))
    key = index_key(specs)
    with STATE.lock:
        if STATE.key == key and STATE.status == "ready" and STATE.built:
            return STATE.built
        thread = STATE.thread if STATE.key == key and STATE.status == "building" else None
    if thread:
        thread.join()
        with STATE.lock:
            if STATE.built and STATE.built.key == key:
                return STATE.built
    built = _build(specs, modules)
    with STATE.lock:
        STATE.key, STATE.built, STATE.status = key, built, "ready"
    return built


def ready() -> bool:
    return STATE.status == "ready" and STATE.built is not None


def index() -> Built | None:
    return STATE.built if ready() else None


def status() -> str:
    return STATE.status


async def ensure(platform) -> Built | None:
    """The index if ready; otherwise start the build (when never started) and return None (callers use fallbacks)."""
    if ready():
        return STATE.built
    if STATE.status == "off":
        await warm(platform)
    return index()


def reset_for_tests() -> None:
    with STATE.lock:
        STATE.key, STATE.built, STATE.status, STATE.thread = None, None, "off", None


# ---------------------------------------------------------------------------- limits and errors
def _p(request: Request):
    return platform_of(request)


def search_limit(request: Request, p: Principal) -> None:
    """900 calls a minute per person, 1,800 per company, 12,000 for the server (search and suggest together)."""
    platform = _p(request)
    checks = [(platform.search_user_limiter, f"u|{p.tenant_id}|{p.user_id}"),
              (platform.search_tenant_limiter, f"t|{p.tenant_id}"),
              (platform.search_global_limiter, "all")]
    wait = max(lim.retry_after(k) for lim, k in checks)
    if wait:
        raise HTTPException(429, "Too many searches in a minute. Wait a moment and try again.",
                            headers={"Retry-After": str(wait)})
    for lim, k in checks:
        lim.hit(k)


def entitled(platform, p: Principal) -> tuple[bool, dict | None]:
    tenant = platform.store.get_tenant(p.tenant_id)
    return bool(entitlement(tenant)["ok"]), tenant


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------- company rows (per request)
def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _place(city, county) -> str:
    city, county = _clean(city), _clean(county)
    if city and county:
        return f"{city}, {county} County"
    return city or (f"{county} County" if county else "")


def _rank(items: list[dict], q: str, text=lambda it: it["value"]) -> list[dict]:
    """Exact match first, then any word-prefix match, then most recent (the input order)."""
    out = []
    for n, it in enumerate(items):
        r = max(engine.match_rank(t, q) for t in (text(it) if isinstance(text(it), (list, tuple)) else [text(it)]))
        if r:
            out.append((0 if r == 3 else 1, n, it))
    out.sort(key=lambda x: (x[0], x[1]))
    return [it for _, _, it in out]


def _distinct(values) -> list[str]:
    seen, out = set(), []
    for v in values:
        v = _clean(v)
        k = engine.fold(v)
        if v and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _attendee_names(rows: list[dict]) -> list[str]:
    names = []
    for r in rows:
        raw = r.get("attendees")
        try:
            people = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(people, list):
            continue
        for a in people:
            name = a.get("name") if isinstance(a, dict) else None
            if isinstance(name, str):
                names.append(name)
    return names


PROJECT_COLS = {"project_city": "city", "project_county": "county", "project_address": "address",
                "owner_name": "owner_name", "lender_name": "lender_name"}
COMPANY_SOURCES = ["project", "project_city", "project_county", "project_address", "owner_name", "lender_name",
                   "sub", "sub_license", "crew_name", "field_topic", "document"]
REMOTE_SOURCES = ["scope_phrase", "rule", "need"]
SOURCE_LABELS = {"project": "Your projects", "sub": "Your subcontractors", "document": "Your documents",
                 "team": "Your team", "logs": "Earlier logs"}


def company_items(platform, p: Principal, source: str, q: str, tool: str | None = None,
                  field: str | None = None) -> list[dict]:
    """Suggestions from the caller's own company rows, ranked for `q` (empty = the most recent)."""
    store, tid = platform.store, p.tenant_id
    if source == "project":
        rows = store.candidates(tid, "projects", ("id", "name", "city", "county"))
        items, seen = [], set()
        for r in rows:
            name = _clean(r["name"])
            if name and engine.fold(name) not in seen:
                seen.add(engine.fold(name))
                items.append({"value": name, "label": name, "detail": _place(r["city"], r["county"]),
                              "source_label": SOURCE_LABELS["project"]})
        return _rank(items, q)
    if source in PROJECT_COLS:
        col = PROJECT_COLS[source]
        rows = store.candidates(tid, "projects", (col,))
        return _rank([{"value": v, "label": v, "source_label": SOURCE_LABELS["project"]}
                      for v in _distinct(r[col] for r in rows)], q)
    if source == "sub":
        rows = store.candidates(tid, "subs", ("name", "license_number"))
        return _rank([{"value": v, "label": v, "source_label": SOURCE_LABELS["sub"]}
                      for v in _distinct(r["name"] for r in rows)], q)
    if source == "sub_license":
        rows = store.candidates(tid, "subs", ("name", "license_number", "scope"))
        items, seen = [], set()
        for r in rows:
            n = re.sub(r"\D", "", str(r["license_number"] or ""))
            if n and n not in seen:
                seen.add(n)
                item = {"value": n, "label": f"{n} · {_clean(r['name'])}", "source_label": SOURCE_LABELS["sub"],
                        "_names": [n, _clean(r["name"])]}
                if _clean(r["scope"]):
                    item["detail"] = _clean(r["scope"])[:80]
                items.append(item)
        tenant = store.get_tenant(tid) or {}
        own = re.sub(r"\D", "", str(tenant.get("cslb_license") or ""))
        if own and own not in seen:
            mine = {"value": own, "label": f"{own} · {_clean(tenant.get('company_name'))} (your license)",
                    "source_label": "Your company", "_names": [own, _clean(tenant.get("company_name"))]}
            if field == "contractor_license_number":
                items.insert(0, mine)
            else:
                items.append(mine)
        ranked = _rank(items, q, text=lambda it: it["_names"])
        for it in ranked:
            it.pop("_names", None)
        return ranked
    if source == "crew_name":
        items = []
        if p.role in ("owner", "office"):          # the team list is office_up, like GET /api/users
            items += [{"value": v, "label": v, "source_label": SOURCE_LABELS["team"]}
                      for v in _distinct(r["name"] for r in store.candidates(tid, "users", ("name",)))]
        have = {engine.fold(i["value"]) for i in items}
        logs = store.candidates(tid, "field_logs", ("kind", "topic", "attendees"), limit=500)
        items += [{"value": v, "label": v, "source_label": SOURCE_LABELS["logs"]}
                  for v in _distinct(_attendee_names(logs)) if engine.fold(v) not in have]
        return _rank(items, q)
    if source == "field_topic":
        logs = store.candidates(tid, "field_logs", ("kind", "topic", "attendees"), limit=500)
        return _rank([{"value": v, "label": v, "source_label": SOURCE_LABELS["logs"]}
                      for v in _distinct(r["topic"] for r in logs)], q)
    if source == "document":
        rows = store.candidates(tid, "documents", ("id", "title"))
        return _rank([{"value": v, "label": v, "source_label": SOURCE_LABELS["document"]}
                      for v in _distinct(r["title"] for r in rows)], q)
    raise KeyError(source)


def company_search(platform, p: Principal, q: str) -> dict[str, list[dict]]:
    """Projects, subs and documents of the caller's company whose name (or license number) matches `q`."""
    store, tid = platform.store, p.tenant_id
    out: dict[str, list[dict]] = {}
    rows = store.candidates(tid, "projects", ("id", "name", "city", "county"))
    out["project"] = [{"type": "project", "id": str(r["id"]), "title": _clean(r["name"]),
                       "detail": _place(r["city"], r["county"]), "href": f"#/project/{r['id']}", "kind": "Project",
                       "icon": "projects"}
                      for r in _rank(rows, q, text=lambda r: _clean(r["name"]))]
    rows = store.candidates(tid, "subs", ("id", "name", "license_number", "scope"))
    out["sub"] = [{"type": "sub", "id": str(r["id"]), "title": _clean(r["name"]),
                   "detail": " · ".join(x for x in (_clean(r["license_number"]), _clean(r["scope"])[:60]) if x),
                   "href": f"#/subs?sub={r['id']}", "kind": "Sub", "icon": "subs"}
                  for r in _rank(rows, q, text=lambda r: [_clean(r["name"]), _clean(r["license_number"])])]
    rows = store.candidates(tid, "documents", ("id", "title"))
    out["document"] = [{"type": "document", "id": str(r["id"]), "title": _clean(r["title"]), "detail": "Document",
                        "href": f"#/document/{r['id']}", "kind": "Document", "icon": "docs"}
                       for r in _rank(rows, q, text=lambda r: _clean(r["title"]))]
    return out


# ---------------------------------------------------------------------------- static sources (the vocab)
def _official_dir() -> dict:
    from tradecomply import professionals
    reg = professionals.registry()
    src = reg.get("counties_source") or {}
    return {"name": "DIR public works apprenticeship search", "url": src.get("url") if isinstance(src, dict)
            else "https://www.dir.ca.gov/databases/das/pwaddrstart.asp",
            "read_on": src.get("read_on") if isinstance(src, dict) else "2026-11-26"}


CSLB_OFFICIAL = {"name": "CSLB Licensing Classifications",
                 "url": "https://www.cslb.ca.gov/About_Us/Library/Licensing_Classifications/", "read_on": "2026-11-27"}


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _county_items() -> tuple[list[dict], dict]:
    from tradecomply import equipment_emissions as ee
    from tradecomply import professionals
    abbrev: dict[str, list[str]] = {}
    for k, v in ee._COUNTY_ABBREV.items():
        abbrev.setdefault(v, []).append(k.upper())
    items = [engine.option(c, c, aliases=[*abbrev.get(c, []), f"{c} County", f"County of {c}"])
             for c in professionals.counties()]
    ask = {k.upper(): [c.strip() for c in re.split(r"\s+or\s+", v)] for k, v in ee._COUNTY_AMBIGUOUS.items()}
    return items, ask


def _city_items() -> list[dict]:
    from tradecomply import local_rules as lr
    out = []
    for key in lr._CITY_RULE:
        name = lr._city_name(key)
        ver = key in lr.VERIFIED_CITIES
        aliases = sorted(a for a in lr._ALIASES.get(key, ()) if a != name.lower())
        out.append(engine.option(name, name, "City rules verified" if ver else "City rules not yet verified",
                                 aliases=[f"City of {name}"] + [a.upper() if len(a) <= 3 else a for a in aliases],
                                 verified=ver, rule_id=lr._CITY_RULE[key]))
    return out


def _trade_words() -> dict[str, list[str]]:
    """Class code -> what contractors call the trade, from the vocabulary groups that expand to that class
    ("roofer", "re-roof" and "shingles" for C-39)."""
    out: dict[str, list[str]] = {}
    for g in engine.vocab()["groups"]:
        for code in re.findall(r"\(([ABC](?:-\d+)?)\)", g["concept"]):       # "roofing (C-39)"
            out.setdefault(code, []).extend(f["form"] if isinstance(f, dict) else f for f in g["forms"])
    return out


def _class_items(held: bool) -> list[dict]:
    from tradecomply import licensing
    words = _trade_words()
    out = []
    for code, name, rid, cite in engine.BASE_CLASSES:
        out.append(engine.option(code, f"{code} {name}", cite,
                                 aliases=[code.replace("-", ""), name.lower(), *words.get(code, [])], rule_id=rid))
    for code, e in licensing.class_table().items():
        cite = core.rule(e.rule_id).citation_text or "16 CCR 832"
        num = code.split("-")[1]
        out.append(engine.option(code, f"{code} {e.name}", cite,
                                 aliases=[f"c{num}", f"C {num}", e.name.lower(), *e.keywords, *words.get(code, [])],
                                 rule_id=e.rule_id))
    for it in engine.cslb_d_subcategories()["items"]:
        still = engine.d_still_issued(it)
        if not still and not held:
            continue
        code = it["page_code"]
        num = code.split("-")[1]
        detail = ("C-61 Limited Specialty subcategory" if still
                  else "No longer issued · " + engine.d_note(it))
        aliases = list(dict.fromkeys([it["code"], f"d{int(num):02d}", f"d{int(num)}", f"C-61/{code}"]))
        out.append(engine.option(code, f"{code} {engine.d_name(it)}", detail, aliases=aliases))
    return out


def _alias_items(table: dict, labels: dict | None = None) -> list[dict]:
    out = []
    for key, names in table.items():
        names = sorted(names) if not isinstance(names, str) else [names]
        out.append(engine.option(key, (labels or {}).get(key) or key.replace("_", " ").capitalize(),
                                 aliases=[n for n in names if n != key]))
    return out


def _described_values(description: str) -> list[str]:
    """The values a description lists: "outdoors, enclosed_unfiltered (a building...) or enclosed_vehicle_filtered"
    -> outdoors, enclosed_unfiltered, enclosed_vehicle_filtered. Bracketed asides are dropped; a list item counts
    when it is one lower-case word, or a snake_case word anywhere."""
    text = description or ""
    while True:
        bare = re.sub(r"\([^()]*\)", " ", text)
        if bare == text:
            break
        text = bare
    found = []
    for piece in re.split(r",|;|\bor\b|\band\b", text):
        piece = piece.strip().rstrip(".")
        piece = piece.split(":")[-1].strip()
        if re.fullmatch(r"[a-z][a-z0-9_]{2,}", piece):
            found.append(piece)
    found += re.findall(r"\b[a-z]+(?:_[a-z0-9]+)+\b", description or "")
    return list(dict.fromkeys(found))


def static_sources(specs: list[dict]) -> dict[str, dict]:
    """Every static suggestion source: label, strict, items (value, label, aliases, detail...), official source."""
    from tradecomply import equipment_emissions as ee
    from tradecomply import federal_projects as fp
    from tradecomply import hr_compliance as hr
    from tradecomply import jobsite_safety as js
    from tradecomply import professionals, rulebook_tools
    from tradecomply import rules as safety_rules
    counties, ask = _county_items()
    cities = _city_items()
    s: dict[str, dict] = {}
    s["county"] = {"label": "California county", "strict": True, "official": _official_dir(), "items": counties,
                   "ask_which": ask}
    s["city_covered"] = {"label": "City TradeComply has local rules for", "strict": True, "items": cities}
    s["city_tenant"] = {"label": "City", "strict": False, "items": cities}
    s["class_needed"] = {"label": "CSLB license class", "strict": False, "official": CSLB_OFFICIAL,
                         "items": _class_items(held=False)}
    s["class_held"] = {"label": "CSLB license class you hold", "strict": False, "official": CSLB_OFFICIAL,
                       "items": _class_items(held=True)}
    appr = professionals.types().get("apprenticeship_committee", {})
    s["craft"] = {"label": "Craft (DIR DAS)", "strict": False,
                  "items": [engine.option(name, name, f"DAS craft code {code}")
                            for name, code in (appr.get("das_craft_codes") or {}).items()]}
    s["hazard"] = {"label": "Hazard", "strict": False,
                   "items": [engine.option(k, k.capitalize(), v.get("cal_osha"), aliases=list(v.get("aliases", [])))
                             for k, v in safety_rules.SAFETY_TABLE.items()]}
    s["equipment"] = {"label": "Equipment", "strict": True,
                      "items": [engine.option(e[0], e[1], aliases=list(e[2])) for e in js._EQUIPMENT]}
    tasks = []
    for g in js._GROUPS:
        words = [w.rstrip("*") for w in g["words"]]
        tasks.append(engine.option(words[0], words[0], g["label"], aliases=words[1:]))
    tasks += [engine.option(a, a, e[1]) for e in js._EQUIPMENT for a in e[2][:2]]
    s["task"] = {"label": "Task", "strict": False, "items": tasks}
    s["professional_type"] = {"label": "Kind of professional", "strict": True,
                              "items": [engine.option(t["id"], t["name"], aliases=list(t.get("triggers", [])))
                                        for t in professionals.registry()["types"]]}
    s["tool_name"] = {"label": "Check", "strict": False,
                      "items": [engine.option(sp["name"], catalog.title(sp["name"], sp.get("title")))
                                for sp in specs]}
    s["rule_category"] = {"label": "Rule category", "strict": True,
                          "items": [engine.option(c, c.replace("_", " ").capitalize())
                                    for c in rulebook_tools.categories()]}
    s["leave_type"] = {"label": "Kind of leave", "strict": True, "items": _alias_items(hr._LEAVE_ALIASES)}
    s["qualifying_event"] = {"label": "Qualifying event", "strict": True, "items": _alias_items(hr._COBRA_ALIASES)}
    s["funding_source"] = {"label": "Where the money comes from", "strict": True,
                           "items": _alias_items(fp._FUNDING_ALIASES)}
    s["work_type_federal"] = {"label": "Kind of work", "strict": True, "items": _alias_items(fp._WORK_ALIASES)}
    s["buy_america_program"] = {"label": "Program", "strict": True, "items": _alias_items(fp._PROGRAM_ALIASES)}
    items = _alias_items(fp._ITEM_ALIASES)
    for it in items:      # a material name ("lumber", "PVC") is a construction material to the tool
        if it["value"] == "construction_material":
            extra = [n for names in fp._MATERIAL_ALIASES.values() for n in names] + list(fp._MATERIAL_ALIASES)
            fresh = engine.option(it["value"], it["label"], aliases=[*it.get("aliases", []), *extra])
            it.clear()
            it.update(fresh)
    s["buy_america_item"] = {"label": "Item", "strict": True, "items": items}
    s["fuel"] = {"label": "Fuel", "strict": True, "items": _alias_items(ee._FUEL_ALIASES)}
    s["fleet_owner_type"] = {"label": "Fleet owner", "strict": True, "items": _alias_items(ee._OWNER_ALIASES)}
    s["offroad_role"] = {"label": "Your role", "strict": True, "items": _alias_items(ee._ROLE_ALIASES)}
    s["cs_role"] = {"label": "Your role", "strict": True, "items": _alias_items(js._CS_ROLES)}
    s["me_role"] = {"label": "Your role", "strict": True, "items": _alias_items(js._ME_ROLES)}
    districts = _alias_items(ee._DISTRICT_ALIASES, {k: k for k in ee._DISTRICT_ALIASES})
    s["air_district"] = {"label": "Air district or county", "strict": False,
                         "items": districts + [engine.option(c["value"], c["label"], "County") for c in counties]}
    # values written in a field's own description (suggestions only: the tool's error stays the authority)
    by_name = {sp["name"]: sp for sp in specs}
    for tool, field in DESCRIBED:
        prop = ((by_name.get(tool) or {}).get("input_schema") or {}).get("properties", {}).get(field) or {}
        vals = _described_values(prop.get("description") or "")
        if vals:
            s[f"values.{tool}.{field}"] = {"label": field.replace("_", " ").capitalize(), "strict": False,
                                           "items": [engine.option(v, v.replace("_", " ")) for v in vals]}
    return s


# The 15 pseudo-enum fields without an alias table (values read from the field's description).
DESCRIBED = [("wildfire_smoke_requirements", "work_setting"), ("accessibility_triggers", "project_type"),
             ("accessibility_triggers", "building_use"), ("accessibility_triggers", "special_facility"),
             ("path_of_travel_obligation", "work_type"), ("path_of_travel_obligation", "facility"),
             ("accessible_parking_requirements", "use"), ("accessible_parking_requirements", "work_type"),
             ("truck_compliance", "vehicle_use"), ("truck_compliance", "owner_type"),
             ("portable_engine_requirements", "special_category"), ("construction_hours", "work_type"),
             ("construction_debris_plan", "project_type"), ("certifications_required", "building_type"),
             ("check_crew_certifications", "building_type"), ("equipment_operating_permits", "building_type")]

# tool.field -> (source, strict for this field, multiple). Strict only where the tool raises on an off-list value
# (tests/test_suggest_api.py sends each strict field one off-list value through the MCP server).
FIELD_MAP: dict[str, tuple[str, bool, bool]] = {
    "valley_fever_requirements.county": ("county", True, False),
    "jobsite_hazard_checklist.county": ("county", True, False),
    "find_licensed_professional.county": ("county", True, False),
    "who_must_sign.county": ("county", True, False),
    "project_setup_checklist.county": ("county", False, False),          # the tool only uses it for wording
    "project_risk_score.county": ("county", False, False),
    "offroad_fleet_obligations.operating_counties": ("county", False, True),   # also takes air district names
    "truck_compliance.operating_counties": ("county", False, True),
    "construction_hours.city": ("city_covered", True, False),
    "local_jobsite_requirements.city": ("city_covered", True, False),
    "construction_debris_plan.city": ("city_covered", True, False),
    "estimate_permit_fees.jurisdiction": ("city_tenant", False, False),
    "compliance_calendar.cities_worked": ("city_tenant", False, True),
    "project_setup_checklist.city": ("city_tenant", False, False),
    "project_risk_score.city": ("city_tenant", False, False),
    "jobsite_minimum_wage.city": ("city_tenant", False, False),
    "path_of_travel_obligation.jurisdiction": ("city_tenant", False, False),
    "casp_lease_disclosure.business_license_city": ("city_tenant", False, False),
    "certifications_required.city": ("city_tenant", False, False),
    "equipment_operating_permits.city": ("city_tenant", False, False),
    "find_licensed_professional.city": ("city_tenant", False, False),
    "who_must_sign.city": ("city_tenant", False, False),
    "company_license_obligations.classifications": ("class_held", False, True),
    "validate_home_improvement_contract.license_classifications": ("class_held", False, True),
    "compliance_calendar.license_classifications": ("class_held", False, True),
    "local_jobsite_requirements.contractor_license_classes": ("class_held", False, True),
    "certifications_required.employer_license_classes": ("class_held", False, True),
    "check_crew_certifications.employer_license_classes": ("class_held", False, True),
    "check_subcontractor_compliance.required_classification": ("class_needed", False, False),
    "verify_contractor_license.license_number": ("sub_license", False, False),
    "check_subcontractor_compliance.license_number": ("sub_license", False, False),
    "project_setup_checklist.contractor_license_number": ("sub_license", False, False),
    "project_risk_score.contractor_license_number": ("sub_license", False, False),
    "project_setup_checklist.project_name": ("project", False, False),
    "project_risk_score.project_name": ("project", False, False),
    "classification_for_scope.scope_of_work": ("scope_phrase", False, False),
    "unlicensed_contracting_exposure.scope": ("scope_phrase", False, False),
    "check_change_order.scope_of_work": ("scope_phrase", False, False),
    "permit_required.work_description": ("scope_phrase", False, False),
    "integrations_for_task.task": ("scope_phrase", False, False),
    "jobsite_hazard_checklist.tasks": ("task", False, True),
    "certifications_required.tasks": ("task", False, True),
    "lookup_safety_standard.hazard": ("hazard", False, False),
    "overhead_line_clearance.equipment": ("equipment", True, False),
    "apprentice_hours_required.craft": ("craft", False, False),
    "find_licensed_professional.craft": ("craft", False, False),
    "public_works_readiness.journeyman_hours_by_craft": ("craft", False, True),
    "find_licensed_professional.professional_type": ("professional_type", True, False),
    "find_licensed_professional.need": ("need", False, False),
    "who_must_sign.tool_name": ("tool_name", False, False),         # the tool accepts any name (probe 2026-11-27)
    "search_california_rules.category": ("rule_category", True, False),
    "get_california_rule.rule_id": ("rule", False, False),
    "leave_request_check.leave_type": ("leave_type", True, False),
    "cobra_deadlines.qualifying_event": ("qualifying_event", True, False),
    "federal_project_requirements.funding_source": ("funding_source", True, False),
    "federal_overtime_exposure.funding_source": ("funding_source", False, False),
    "federal_project_requirements.work_type": ("work_type_federal", True, False),
    "buy_america_check.program": ("buy_america_program", True, False),
    "buy_america_check.item_type": ("buy_america_item", True, False),
    "truck_compliance.fuel": ("fuel", True, False),
    "portable_engine_requirements.fuel": ("fuel", True, False),
    "offroad_fleet_obligations.fleet_owner_type": ("fleet_owner_type", True, False),
    "offroad_contracting_requirements.role": ("offroad_role", True, False),
    "confined_space_classification.your_role": ("cs_role", True, False),
    "jobsite_hazard_checklist.your_role": ("me_role", True, False),
    "dust_control_requirements.air_district": ("air_district", False, False),
}
for _t, _f in DESCRIBED:
    FIELD_MAP[f"{_t}.{_f}"] = (f"values.{_t}.{_f}", False, False)

APP_FIELDS = {"project.city": {"source": "city_tenant", "strict": False},
              "project.county": {"source": "county", "strict": True},
              "project.address": {"source": "project_address"}, "project.owner_name": {"source": "owner_name"},
              "project.lender_name": {"source": "lender_name"}, "sub.name": {"source": "sub"},
              "sub.scope": {"source": "scope_phrase"},
              "sub.required_class": {"source": "class_needed", "strict": False},
              "field.topic": {"source": "field_topic"}, "field.attendee": {"source": "crew_name"},
              "finder.need": {"source": "need"}}


def field_map(specs: list[dict], sources: dict[str, dict]) -> dict[str, dict]:
    """tool.field -> {source, strict, multiple, chips} for fields that exist in this server's schemas."""
    props = {sp["name"]: set(((sp.get("input_schema") or {}).get("properties") or {})) for sp in specs}
    out = {}
    for key, (source, strict, multiple) in FIELD_MAP.items():
        tool, field = key.split(".", 1)
        if field not in props.get(tool, ()):
            continue
        if source.startswith("values.") and source not in sources:
            continue
        n = len(sources.get(source, {}).get("items", ())) if source in sources else None
        out[key] = {"source": source, "strict": strict, "multiple": multiple,
                    "chips": bool(strict and not multiple and n is not None and n <= 6)}
    return out


@dataclass
class Vocab:
    key: str
    body: bytes
    version: str
    sources: dict          # with _keys, for server-side matching


_VOCAB: dict[str, Vocab] = {}
_VOCAB_LOCK = threading.Lock()


def build_vocab(specs: list[dict]) -> Vocab:
    key = index_key(specs)
    with _VOCAB_LOCK:
        if key in _VOCAB:
            return _VOCAB[key]
    sources = static_sources(specs)
    public_sources = {}
    for sid, src in sources.items():
        public_sources[sid] = {k: v for k, v in src.items() if k != "items"}
        public_sources[sid]["items"] = [_public(it) for it in src["items"]]
    payload = {
        "rules_built_on": built_on(),
        "sources": public_sources,
        "fields": field_map(specs, sources),
        "app_fields": APP_FIELDS,
        "company_sources": COMPANY_SOURCES,
        "remote_sources": REMOTE_SOURCES,
        "tool_keys": {name: words for name, words in engine.tool_words().items()
                      if name in {s["name"] for s in specs}},
        "glossary": [{k: g[k] for k in ("term", "aliases", "expansion", "rule_id") if k in g}
                     for g in engine.glossary()],
        "page_starters": catalog.PAGE_STARTERS,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    version = f"{built_on()}.{hashlib.sha256(raw).hexdigest()[:12]}"
    body = json.dumps({"version": version, **payload}, ensure_ascii=False, separators=(",", ":")).encode()
    v = Vocab(key, body, version, sources)
    with _VOCAB_LOCK:
        _VOCAB.clear()
        _VOCAB[key] = v
    return v


def remote_items(source: str, q: str, limit: int, ok: bool) -> list[dict]:
    """Search-backed sources: scope phrases (CSLB class keywords), rules (index; [] when not entitled) and needs
    (professional triggers)."""
    if source == "scope_phrase":
        return [_public(o) for o in engine.match_options(_scope_options(), q, limit)]
    if source == "need":
        return [_public(o) for o in engine.match_options(_need_options(), q, limit)]
    if source == "rule":
        if not ok or len(q) < 2:
            return []
        built = index()
        if built:
            hits = built.index.search(q, types={"rule"}, limit=limit, typing=True)
            docs = [d for _, d, _ in hits]
            return [{"value": d.id, "label": d.title, "detail": d.meta.get("citation") or "",
                     "verified": d.meta["verified"], "source_label": "Rules"} for d in docs]
        return [{"value": r.id, "label": r.title or r.id, "detail": r.citation_text or "", "verified": r.verified,
                 "source_label": "Rules"} for r in core.search(q, limit)]
    raise KeyError(source)


_SCOPE: list | None = None
_NEED: list | None = None


def _scope_options() -> list[dict]:
    global _SCOPE
    if _SCOPE is None:
        from tradecomply import licensing
        seen, out = set(), []
        for code, e in licensing.class_table().items():
            for kw in e.keywords:
                if kw not in seen:
                    seen.add(kw)
                    out.append(engine.option(kw, kw, f"{code} {e.name}", source_label="Scope of work"))
        _SCOPE = out
    return _SCOPE


def _need_options() -> list[dict]:
    global _NEED
    if _NEED is None:
        from tradecomply import professionals
        out = []
        for t in professionals.registry()["types"] + professionals.handoff_data().get("extra_types", []):
            for trig in t.get("triggers", []):
                out.append(engine.option(trig, trig, t["name"], type_id=t["id"], source_label="Licensed professionals"))
        _NEED = out
    return _NEED


# ---------------------------------------------------------------------------- routes
class SuggestIn(Strict):
    source: str = Field(min_length=1, max_length=60)
    q: str = Field("", max_length=100)
    limit: int = Field(8, ge=1, le=20)
    tool: Optional[str] = Field(None, max_length=80)
    field: Optional[str] = Field(None, max_length=80)


@router.get("/suggest/vocab")
async def suggest_vocab(request: Request, p: Principal = Depends(people_only)):
    from . import api
    specs = await api.tool_specs(_p(request))
    v = await anyio.to_thread.run_sync(build_vocab, specs)
    etag = f'"{v.version}"'
    sent = [x.strip() for x in (request.headers.get("if-none-match") or "").split(",") if x.strip()]
    if any(x.removeprefix("W/") in (etag, "*") for x in sent):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    return Response(v.body, media_type="application/json",
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"})


@router.post("/suggest")
async def suggest(body: SuggestIn, request: Request, p: Principal = Depends(people_only)):
    if body.source not in COMPANY_SOURCES and body.source not in REMOTE_SOURCES:
        raise HTTPException(400, "Unknown suggestion source")
    search_limit(request, p)
    platform = _p(request)
    t0 = time.perf_counter()
    try:
        if body.source in COMPANY_SOURCES:
            items = await anyio.to_thread.run_sync(company_items, platform, p, body.source, body.q, body.tool,
                                                   body.field)
        else:
            ok = True
            if body.source == "rule":
                ok, _tenant = await anyio.to_thread.run_sync(entitled, platform, p)
                await ensure(platform)
            items = await anyio.to_thread.run_sync(remote_items, body.source, body.q, body.limit + 1, ok)
    except Exception as e:     # noqa: BLE001 - logged by type only (the query could hold anything typed)
        log.error("suggest failed source=%s exc=%s", body.source, type(e).__name__)
        items = []
    more = len(items) > body.limit
    return {"source": body.source, "q": body.q, "items": items[:body.limit], "more": more,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1)}


TYPES = ("project", "sub", "document", "page", "tool", "professional", "class", "rule", "integration")
QUOTAS = {"project": 3, "sub": 3, "document": 3, "page": 3, "tool": 5, "professional": 2, "class": 2, "rule": 5,
          "integration": 2}
GROUP_LABELS = {"project": "Your projects", "sub": "Your subs", "document": "Your documents", "page": "Go to",
                "tool": "Checks", "professional": "Licensed professionals", "class": "License classes",
                "rule": "Rules", "integration": "Integrations"}
PAGE_ICONS = {"dashboard": "today", "projects": "projects", "project_new": "projects", "subs": "subs",
              "subs_import": "subs", "tools": "checks", "documents": "docs", "field": "log",
              "professionals": "find", "integrations": "plug", "calendar": "plug", "welcome": "today",
              "more": "more"}
GROUP_ICON = {g["id"]: g["icon"] for g in catalog.GROUPS}
TOOL_THRESHOLD = 0.5


class SearchContext(Strict):
    page: Optional[str] = Field(None, max_length=40)
    tool: Optional[str] = Field(None, max_length=80)
    project_id: Optional[int] = None


class SearchIn(Strict):
    q: str = Field(min_length=1, max_length=200)
    types: Optional[list[Literal[TYPES]]] = Field(None, max_length=9)
    limit: int = Field(20, ge=1, le=50)
    context: Optional[SearchContext] = None


def _item(d: engine.Doc) -> dict:
    if d.type == "page":
        return {"type": "page", "id": d.id, "title": d.title, "detail": d.meta.get("detail") or "Page",
                "href": d.meta["href"], "kind": "Page", "icon": PAGE_ICONS.get(d.id, "settings")}
    if d.type == "tool":
        hidden = bool(d.meta.get("hidden"))
        return {"type": "tool", "id": d.id, "title": d.title, "detail": d.meta.get("summary") or "",
                "href": d.meta["href"], "kind": "For AI assistants" if hidden else "Check",
                "icon": GROUP_ICON.get(d.meta.get("group"), "checks"), "hidden": hidden}
    if d.type == "professional":
        return {"type": "professional", "id": d.id, "title": d.title, "detail": "Official state lookups",
                "href": d.meta["href"], "kind": "Professional", "icon": "find"}
    if d.type == "class":
        cite = d.meta.get("citation")
        return {"type": "class", "id": d.id, "title": d.title,
                "detail": f"CSLB class · {cite}" if cite else "CSLB class", "href": f"#/rule/{d.meta['rule_id']}",
                "kind": "License class", "icon": "g-license"}
    if d.type == "rule":
        v = bool(d.meta["verified"])
        return {"type": "rule", "id": d.id, "title": d.title, "detail": d.meta.get("citation") or "",
                "href": d.meta["href"], "kind": "Rule · Verified" if v else "Rule · Not yet verified", "icon": "rule",
                "status": d.meta["status"], "verified": v}
    return {"type": "integration", "id": d.id, "title": d.title, "detail": d.meta.get("category") or "Integration",
            "href": d.meta["href"], "kind": "Integration", "icon": "plug"}


def _fallback_search(q: str, role: str, want: set[str], ok: bool) -> dict[str, list[dict]]:
    """While the index builds: rules from core.search, checks and pages by word match."""
    out: dict[str, list[dict]] = {}
    if "page" in want:
        out["page"] = [_item(d) for d in engine.page_docs(catalog.pages_for(role))
                       if engine.match_rank(d.title, q) or engine.match_rank(d.fields.get("keys", ""), q)]
    if "tool" in want:
        docs = [engine.Doc("tool", n, c["title"], {}, meta={"href": f"#/tools/{n}", "summary": c["summary"],
                                                            "hidden": c["hidden"], "group": c["group"]})
                for n, c in catalog.TOOLS.items() if not c["hidden"]]
        out["tool"] = [_item(d) for d in docs if engine.match_rank(d.title, q) or engine.match_rank(
            d.meta["summary"], q)]
    if "rule" in want and ok:
        out["rule"] = [_item(engine.rule_docs([r])[0]) for r in core.search(q, 5)]
    return out


def run_search(platform, p: Principal, body: SearchIn, ok: bool) -> dict:
    t0 = time.perf_counter()
    q = body.q
    want = set(body.types or TYPES)
    groups: dict[str, list[dict]] = {}
    did_you_mean, nearest = None, []
    if want & {"project", "sub", "document"}:
        for t, items in company_search(platform, p, q).items():
            if t in want:
                groups[t] = items
    built = index()
    if built:
        index_types = {t for t in want if t not in ("project", "sub", "document")}
        if not ok:
            index_types.discard("rule")      # rule text follows the subscription (D12)
        res, ex = built.index.search_info(q, types=index_types, limit=120, typing=True,
                                          filt=lambda d: d.type != "page" or p.role in d.meta["roles"]) \
            if index_types else ([], None)
        tools = [(s, d, m) for s, d, m in res if d.type == "tool"]
        strong = [x for x in tools if x[2]["coverage"] >= TOOL_THRESHOLD]
        for t in ("page", "tool", "professional", "class", "rule", "integration"):
            if t not in index_types:
                continue
            docs = [(s, d, m) for s, d, m in res if d.type == t]
            if t == "tool":
                docs = strong
            groups[t] = [_item(d) for _, d, _ in docs]
        if "tool" in index_types and tools and not strong:
            nearest = [_item(d) for _, d, _ in tools[:3]]
        if ex and ex.fixes and res:
            word, fix = next(iter(ex.fixes.items()))
            label = built.index.surface(fix)
            fixed = re.sub(re.escape(word), label, engine.ascii_fold(q), count=1) if word in engine.ascii_fold(q) \
                else label
            did_you_mean = {"q": fixed, "label": label}
    else:
        groups.update(_fallback_search(q, p.role, want, ok))
    limit = body.limit
    out_groups = [{"type": t, "label": GROUP_LABELS[t], "items": groups[t][:min(QUOTAS[t], limit)]}
                  for t in TYPES if t in want and groups.get(t)]
    return {"q": q, "ready": built is not None, "entitled": ok, "did_you_mean": did_you_mean, "nearest": nearest,
            "took_ms": round((time.perf_counter() - t0) * 1000, 1), "groups": out_groups}


@router.post("/search")
async def search(body: SearchIn, request: Request, p: Principal = Depends(people_only)):
    search_limit(request, p)
    platform = _p(request)
    ok, _tenant = await anyio.to_thread.run_sync(entitled, platform, p)
    await ensure(platform)
    try:
        return await anyio.to_thread.run_sync(run_search, platform, p, body, ok)
    except Exception as e:     # noqa: BLE001 - logged by type only
        log.error("search failed exc=%s", type(e).__name__)
        return {"q": body.q, "ready": ready(), "entitled": ok, "did_you_mean": None, "nearest": [], "took_ms": 0,
                "groups": []}


def post_only():
    """405 for a GET on a POST-only route (the catch-all /mcp mount would otherwise answer 404): typed text never
    goes in a URL, where access logs, proxies and the browser history keep it."""
    raise HTTPException(405, "Send this as a POST with a JSON body.", headers={"Allow": "POST"})


router.add_api_route("/search", post_only, methods=["GET", "HEAD"], include_in_schema=False)
router.add_api_route("/suggest", post_only, methods=["GET", "HEAD"], include_in_schema=False)


RULE_FIELDS = ("id", "title", "plain_english", "category", "citation", "source", "quote", "status", "verified",
               "checked_on", "effective", "correction", "applies_to")


def rule_payload(rule_id: str, related: dict[str, list[str]]) -> dict:
    from tradecomply import rulebook_tools
    try:
        core.rule(rule_id)
    except KeyError:
        raise HTTPException(404, "No rule with that id") from None
    d = rulebook_tools.get_rule(rule_id).model_dump()
    out = {k: d.get(k) for k in RULE_FIELDS}
    out["related_tools"] = [{"name": n, "title": catalog.title(n)} for n in related.get(rule_id, [])]
    out["disclaimer"] = core.DISCLAIMER
    return out


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str, request: Request, p: Principal = Depends(people_only)):
    platform = _p(request)
    await anyio.to_thread.run_sync(require_entitled, platform, p)
    if len(rule_id) > 200:
        raise HTTPException(404, "No rule with that id")
    built = index()
    if built:
        related = built.related
    else:
        specs, modules = await _collect(platform)
        related = await anyio.to_thread.run_sync(related_map, specs, modules)
    return JSONResponse(rule_payload(rule_id, related))
