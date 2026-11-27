"""Governance for TradeComply's own MCP server at /mcp (the gate itself is auth.MCPGate). See docs/mcp-security.md.

What lives here:
- Access mode (resolve_mcp_access): whether /mcp needs an API key. On Render (Render sets RENDER=true on every
  service) a key is required unless MCP_PUBLIC_DEMO=1; MCP_REQUIRE_KEY=0 alone no longer opens it there.
- Origin check (origin_allowed): a request that carries an Origin header must come from this server's own origin,
  from MCP_ALLOWED_ORIGINS, or (local mode) from localhost. Requests without Origin (every non-browser MCP client)
  are not affected. OWASP MCP Security Cheat Sheet section 6: "Reject a present but invalid Origin with HTTP 403. Do
  not reject non-browser clients solely because they send no Origin header."
- Per-key tool scopes (normalize_scopes, scope_allows): an allow-list of tool names and/or categories (the tool's
  module, as /api/tools groups them), plus whether the key may also use the rest of the REST API. A key with no
  scopes (NULL) gets the default: every read-only tool (annotations.readOnlyHint true; today every tool), and the REST
  API as before. A category grants only the read-only tools in it; a tool that is not read-only must be named.
- The audit record of every MCP tool call (record): tenant, API key id or connected app (oauth_grant_id: an app
  that signed in with OAuth, app/oauth.py; it has the default scope), tool, SHA-256 of the arguments (never the
  arguments: payroll CSVs and employee names pass through them), status ok/error/denied/rate_limited, duration and
  client IP, into mcp_audit_log, plus the activity-log row the app already showed (now with argument NAMES and the
  same SHA-256 instead of argument values). Written after the last byte of the response has been sent, on a worker
  thread, in one transaction, so the database round trip adds nothing to the call's latency. A failed write is
  logged and never fails the call.
- Owner API (router): GET /api/mcp/audit (view, filters, paging), GET /api/mcp/audit/export (CSV/JSON download),
  GET /api/mcp/policy (access mode, limits and the tool categories a key scope can name).

The SHA-256 is computed over the arguments as canonical JSON: keys sorted, separators "," and ":", UTF-8, no ASCII
escaping (json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)). It lets a customer check
whether a given payload was the one sent; it is a fingerprint, not encryption: an argument with few possible values
(a 6-digit license number) can be guessed by hashing candidates.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import safe_json
from .auth import Principal, anyone, office_up, owner_only, platform_of, refuse_cross_site
from .store import MCP_AUDIT_STATUSES, now_iso, to_store_time

log = logging.getLogger("tradecomply.mcp_governance")

TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")
ANON_AUDIT_KEEP = 50_000          # newest anonymous rows kept in each log (they belong to no company)
ANON_TRIM_EVERY = 1000
MAX_PENDING_ROWS = 5000           # audit rows waiting for the database; beyond this new rows are dropped and counted
MAX_SCOPE_TOOLS = 500
MAX_EXPORT_ROWS = 100_000
EXPORT_PAGE = 1000
KEY_RATE_MAX = 600                # a key's own limit can be lower than the platform default, never higher
MAX_KEY_DAYS = 5 * 366
CATEGORY_LABELS = {               # display names for the modules /api/tools has no label for
    "hr_compliance": "HR compliance", "accessibility": "Accessibility", "federal_projects": "Federal projects",
    "trade_certifications": "Trade certifications", "jobsite_safety": "Jobsite safety",
    "local_rules": "City jobsite rules", "equipment_emissions": "Equipment emissions",
}


# ---------------------------------------------------------------------------- argument fingerprints
def canonical_args(args) -> str:
    return json.dumps(args if args is not None else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)


def args_sha256(args) -> str:
    # surrogatepass: arguments holding a lone surrogate ("\ud800", valid JSON) still get a row; every other value
    # hashes exactly as before
    return hashlib.sha256(canonical_args(args).encode("utf-8", "surrogatepass")).hexdigest()


def arg_fields(args) -> list[str]:
    """The top-level argument NAMES (schema field names, not values) for the activity log."""
    if not isinstance(args, dict):
        return []
    return sorted(str(k)[:80] for k in list(args)[:50])


def activity_args(args) -> dict:
    return {"fields": arg_fields(args), "args_sha256": args_sha256(args)}


# ---------------------------------------------------------------------------- access mode
def _tri(value) -> bool | None:
    v = (value or "").strip().lower()
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS:
        return False
    return None


@dataclass(frozen=True)
class MCPAccess:
    require_key: bool
    public_demo: bool
    reason: str
    warnings: tuple = ()


def resolve_mcp_access(env) -> MCPAccess:
    """MCP_REQUIRE_KEY=1 always requires a key. Otherwise MCP_PUBLIC_DEMO=1 opens /mcp to anonymous callers (audited
    and throttled). Otherwise, on Render (RENDER set), a key is required; elsewhere (a laptop, local mode, tests)
    MCP_REQUIRE_KEY decides and defaults to off."""
    require, demo = _tri(env.get("MCP_REQUIRE_KEY")), _tri(env.get("MCP_PUBLIC_DEMO")) is True
    on_render = bool((env.get("RENDER") or "").strip())
    if require is True:
        warnings = ("MCP_PUBLIC_DEMO=1 is ignored because MCP_REQUIRE_KEY=1",) if demo else ()
        return MCPAccess(True, False, "MCP_REQUIRE_KEY=1", warnings)
    if demo:
        return MCPAccess(False, True, "MCP_PUBLIC_DEMO=1 (anonymous public demo)")
    if on_render:
        warnings = ("MCP_REQUIRE_KEY=0 no longer opens /mcp on Render; set MCP_PUBLIC_DEMO=1 to run the anonymous "
                    "public demo",) if require is False else ()
        return MCPAccess(True, False, "running on Render without MCP_PUBLIC_DEMO=1", warnings)
    return MCPAccess(False, False, "not on Render and MCP_REQUIRE_KEY is not 1")


def parse_origins(value: str | None) -> tuple[str, ...]:
    out = []
    for part in (value or "").split(","):
        p = part.strip().rstrip("/").lower()
        if p == "*" or (p.startswith(("https://", "http://")) and "/" not in p.split("://", 1)[1]):
            out.append(p)
    return tuple(out)


def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


# Claude's own web origins. Claude calls /mcp from Anthropic's servers; if those calls carry an Origin header, it is
# one of these (which value, if any, is not confirmed: Claude's connector testing guide names strict Origin checks
# as a cause of failed connections). /mcp takes no cookies, so allowing them gives no page ambient credentials.
CLAUDE_ORIGINS = ("https://claude.ai", "https://claude.com")


def origin_allowed(platform, headers: dict, scope: dict) -> bool:
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True                  # non-browser clients send no Origin; never refused for that alone
    o = origin.rstrip("/").lower()
    s = platform.settings
    allowed = {*s.mcp_allowed_origins, *CLAUDE_ORIGINS}
    if "*" in allowed:
        return True
    own = _origin_of(s.app_url)
    host = headers.get("host")
    if host:
        proto = scope.get("scheme") or "http"
        fwd = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if fwd in ("http", "https") and not s.local_mode:
            proto = fwd
        allowed.add(f"{proto}://{host}".lower())
    if own:
        allowed.add(own)
    if o in allowed:
        return True
    if s.local_mode:
        parts = urlsplit(o)
        return parts.scheme == "http" and (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
    return False


# ---------------------------------------------------------------------------- tool index and scopes
async def tool_index(platform) -> dict[str, dict]:
    """{tool name: {"category", "category_label", "read_only", "title"}} from the MCP server (cached with
    /api/tools' 30-second tool list)."""
    from .api import tool_specs          # local import: api imports this module's router
    specs = await tool_specs(platform)
    cached = getattr(platform, "mcp_tool_index", None)
    if cached and cached[0] is specs:
        return cached[1]
    index = {s["name"]: {"category": s["module"],
                         "category_label": CATEGORY_LABELS.get(s["module"], s["module_label"]),
                         "read_only": (s.get("annotations") or {}).get("readOnlyHint") is True,
                         "title": s.get("title") or s["name"]} for s in specs}
    platform.mcp_tool_index = (specs, index)
    return index


def categories(index: dict) -> list[dict]:
    out: dict[str, dict] = {}
    for name, meta in index.items():
        c = out.setdefault(meta["category"], {"id": meta["category"], "label": meta["category_label"], "tools": []})
        c["tools"].append({"name": name, "title": meta["title"], "read_only": meta["read_only"]})
    return list(out.values())


def normalize_scopes(raw, index: dict) -> dict | None:
    """Validate a scope from the API. None or {} = the default (all read-only tools, REST API allowed). Otherwise a
    dict with any of: tools (names), categories (ids), rest_api (bool, default true). Omitting both tools and
    categories keeps the default tool set; giving either (even empty) makes the key an allow-list. ValueError with a
    readable message for unknown names."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("scopes must be an object with tools, categories and/or rest_api")
    bad = set(raw) - {"tools", "categories", "rest_api"}
    if bad:
        raise ValueError(f"Unknown scope field(s): {', '.join(sorted(map(str, bad)))}. Use tools, categories, "
                         "rest_api.")
    out: dict = {}
    cats = {m["category"] for m in index.values()}
    for field, known, what in (("tools", set(index), "tool"), ("categories", cats, "category")):
        if raw.get(field) is None:
            continue
        values = raw[field]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{field} must be a list of {what} names")
        if len(values) > MAX_SCOPE_TOOLS:
            raise ValueError(f"at most {MAX_SCOPE_TOOLS} {field}")
        unknown = sorted({v for v in values if v not in known})
        if unknown:
            raise ValueError(f"No {what} named {', '.join(repr(u[:80]) for u in unknown[:5])}"
                             f"{' (and more)' if len(unknown) > 5 else ''} on this server")
        out[field] = sorted(set(values))
    rest = raw.get("rest_api", True)
    if not isinstance(rest, bool):
        raise ValueError("rest_api must be true or false")
    if not rest:
        out["rest_api"] = False
    return out or None


def is_allow_list(scopes: dict | None) -> bool:
    return bool(scopes) and ("tools" in scopes or "categories" in scopes)


def scope_allows(scopes: dict | None, tool: str, index: dict) -> bool:
    meta = index.get(tool)
    if not is_allow_list(scopes):
        # Default: every read-only tool. A name the server doesn't have goes through so the server answers
        # "unknown tool" the usual way.
        return meta is None or meta["read_only"]
    if tool in scopes.get("tools", ()):
        return True
    return bool(meta and meta["read_only"] and meta["category"] in scopes.get("categories", ()))


def rest_api_allowed(scopes: dict | None) -> bool:
    return not scopes or scopes.get("rest_api", True) is not False


def describe_scopes(scopes: dict | None, index: dict) -> dict:
    allowed = sorted(n for n in index if scope_allows(scopes, n, index))
    if not is_allow_list(scopes):
        summary = "All read-only tools"
    else:
        parts = []
        if scopes.get("categories"):
            labels = sorted({m["category_label"] for m in index.values() if m["category"] in scopes["categories"]})
            parts.append(", ".join(labels))
        if scopes.get("tools"):
            parts.append(f"{len(scopes['tools'])} named tool{'' if len(scopes['tools']) == 1 else 's'}")
        summary = "; ".join(parts) or "No tools"
    return {"summary": summary, "allowed_tools": allowed, "tool_count": len(allowed),
            "rest_api": rest_api_allowed(scopes), "default": scopes is None}


# ---------------------------------------------------------------------------- tools/list filtering
def _filter_message(msg, allowed: set[str]) -> bool:
    if isinstance(msg, dict) and isinstance(msg.get("result"), dict) and isinstance(msg["result"].get("tools"),
                                                                                    list):
        tools = msg["result"]["tools"]
        keep = [t for t in tools if isinstance(t, dict) and t.get("name") in allowed]
        if len(keep) != len(tools):
            msg["result"]["tools"] = keep
            return True
    return False


def filter_tools_list(body: bytes, content_type: str, allowed: set[str]) -> bytes | None:
    """The tools/list response with only `allowed` tools, from a JSON or text/event-stream body; None if nothing
    changed or the body can't be read (the caller then sends it as it was: the call check is the enforcement)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "text/event-stream" not in content_type:
        try:
            msg = json.loads(text)
        except ValueError:
            return None
        changed = any([_filter_message(m, allowed) for m in (msg if isinstance(msg, list) else [msg])])
        return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode() if changed else None
    nl = "\r\n" if "\r\n" in text else "\n"
    events, changed = text.split(nl + nl), False
    for i, event in enumerate(events):
        lines = event.split(nl)
        data = [ln[5:].lstrip(" ") for ln in lines if ln.startswith("data:")]
        if not data:
            continue
        try:
            msg = json.loads("\n".join(data))
        except ValueError:
            continue
        if _filter_message(msg, allowed):
            changed = True
            others = [ln for ln in lines if not ln.startswith("data:")]
            events[i] = nl.join(others + ["data: " + json.dumps(msg, separators=(",", ":"), ensure_ascii=False)])
    return (nl + nl).join(events).encode() if changed else None


# ---------------------------------------------------------------------------- writing the audit rows
_pending_lock = threading.Lock()


def _write(platform, entries: list[dict], activity: list[dict]) -> None:
    platform.store.log_mcp_calls(entries, activity)
    anon = sum(1 for e in entries if e.get("tenant_id") is None)
    if anon:
        with _pending_lock:
            platform.anon_audit_writes += anon
            due = platform.anon_audit_writes >= ANON_TRIM_EVERY
            if due:
                platform.anon_audit_writes = 0
        if due:                       # keep the anonymous demo logs bounded
            platform.store.trim_anonymous_audit(ANON_AUDIT_KEEP)
            platform.store.trim_anonymous_mcp_audit(ANON_AUDIT_KEEP)


async def record(platform, entries: list[dict], activity: list[dict] = ()) -> None:
    """Write one request's audit rows on a worker thread (the gate calls this only after the response is sent).
    Never raises."""
    entries, activity = list(entries), list(activity)
    n = len(entries) + len(activity)
    if not n:
        return
    with _pending_lock:
        if platform.mcp_audit_pending + n > MAX_PENDING_ROWS:
            platform.mcp_audit_dropped += n
            dropped = platform.mcp_audit_dropped
        else:
            platform.mcp_audit_pending += n
            dropped = None
    if dropped is not None:
        log.error("MCP audit backlog full (%d rows waiting): %d rows dropped so far", MAX_PENDING_ROWS, dropped)
        return
    try:
        await anyio.to_thread.run_sync(_write, platform, entries, activity)
    except Exception:            # noqa: BLE001 - never fail or break an MCP call because of the audit log
        log.exception("MCP audit write failed")
    finally:
        with _pending_lock:
            platform.mcp_audit_pending -= n


def log_refused_origin(platform, origin: str | None) -> None:
    """A server log line for a refused Origin (at most 30 a minute per value), so an operator can see what a client
    sent and add it to MCP_ALLOWED_ORIGINS."""
    value = str(origin or "")[:200]
    if reject_logging_allowed(platform, f"origin|{value}"):
        log.warning("Refused an /mcp request with Origin %r (allow it with MCP_ALLOWED_ORIGINS)", value)


def reject_logging_allowed(platform, key: str) -> bool:
    """Denied and rate-limited calls are logged, but at most 30 rows a minute per key or IP, so a flood of refused
    calls can't flood the database."""
    lim = platform.mcp_reject_log_limiter
    if lim.retry_after(key):
        return False
    lim.hit(key)
    return True


# ---------------------------------------------------------------------------- owner API
router = APIRouter(prefix="/api/mcp", tags=["mcp governance"])
EXPORT_COLUMNS = ("id", "created", "tool", "status", "duration_ms", "api_key_id", "key_name", "key_prefix",
                  "oauth_grant_id", "app_name", "args_sha256", "ip")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value) -> str:
    """A CSV cell a spreadsheet won't run as a formula (OWASP CSV injection guidance: prefix with ')."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if isinstance(value, str) and text.startswith(_FORMULA_START) else text


def _bound(value: Optional[str], name: str) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    try:
        return to_store_time(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a date (YYYY-MM-DD) or an ISO timestamp")


def _filters(start, end, key_id, status, tool) -> dict:
    start_s, end_s = _bound(start, "start"), _bound(end, "end")
    if start_s and end_s and start_s >= end_s:
        raise HTTPException(422, "start must be before end")
    if status is not None and status not in MCP_AUDIT_STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(MCP_AUDIT_STATUSES)}")
    return {"start": start_s, "end": end_s, "api_key_id": key_id, "status": status, "tool": tool or None}


def _key_names(store, tenant_id: str) -> dict[int, dict]:
    return {k["id"]: {"name": k["name"], "prefix": k["prefix"], "revoked": k["revoked"]}
            for k in store.list_api_keys(tenant_id)}


def _view_row(row: dict, keys: dict, apps: dict) -> dict:
    k = keys.get(row.get("api_key_id")) or {}
    a = apps.get(row.get("oauth_grant_id")) or {}
    return {"id": row["id"], "created": row["created"], "tool": row["tool"], "status": row["status"],
            "duration_ms": row.get("duration_ms"), "api_key_id": row.get("api_key_id"), "key_name": k.get("name"),
            "key_prefix": k.get("prefix"), "oauth_grant_id": row.get("oauth_grant_id"),
            "app_name": f"{a['client_name']} ({a['user_email']})" if a else None,
            "args_sha256": row.get("args_sha256"), "ip": row.get("ip")}


@router.get("/audit")
def mcp_audit(request: Request, p: Principal = Depends(owner_only),
              start: Optional[str] = Query(None, max_length=40), end: Optional[str] = Query(None, max_length=40),
              key_id: Optional[int] = Query(None, ge=1, le=2 ** 63 - 1),
              status: Optional[str] = Query(None, max_length=20), tool: Optional[str] = Query(None, max_length=200),
              cursor: Optional[str] = Query(None, max_length=100), limit: int = Query(100, ge=1, le=500)):
    """This company's MCP tool calls, newest first (owner only). Filters: [start, end) in UTC, API key, status,
    tool. Arguments are never stored; each row has their SHA-256."""
    store = platform_of(request).store
    f = _filters(start, end, key_id, status, tool)
    try:
        # a cursor holds a timestamp with "+00:00"; an unencoded "+" in a query string arrives as a space
        page = store.list_mcp_audit(p.tenant_id, limit=limit, cursor=cursor.replace(" ", "+") if cursor else None,
                                    **f)
    except ValueError as e:
        raise HTTPException(422, str(e))
    keys, apps = _key_names(store, p.tenant_id), store.oauth_grant_names(p.tenant_id)
    return {"items": [_view_row(r, keys, apps) for r in page["items"]], "next_cursor": page["next_cursor"],
            "keys": [{"id": i, **k} for i, k in sorted(keys.items(), reverse=True)],
            "statuses": list(MCP_AUDIT_STATUSES), "stored": "tool, status, duration, API key or connected app, "
            "client IP and the SHA-256 of the arguments; never the arguments"}


@router.get("/audit/export")
def mcp_audit_export(request: Request, p: Principal = Depends(owner_only),
                     fmt: str = Query("csv", alias="format", pattern="^(csv|json)$"),
                     start: Optional[str] = Query(None, max_length=40),
                     end: Optional[str] = Query(None, max_length=40),
                     key_id: Optional[int] = Query(None, ge=1, le=2 ** 63 - 1),
                     status: Optional[str] = Query(None, max_length=20),
                     tool: Optional[str] = Query(None, max_length=200)):
    """Download this company's MCP call log (owner only), oldest first, with the same filters as the view, at most
    MAX_EXPORT_ROWS rows."""
    refuse_cross_site(request, "Download the MCP call log from Settings > AI assistant (MCP) in TradeComply, not from "
                               "a link on another site.")
    platform = platform_of(request)
    f = _filters(start, end, key_id, status, tool)
    lim = platform.mcp_export_limiter
    wait = lim.retry_after(p.tenant_id)
    if wait:
        raise HTTPException(429, "Too many downloads. Try again in a few minutes.", headers={"Retry-After": str(wait)})
    lim.hit(p.tenant_id)
    store, tid = platform.store, p.tenant_id
    store.audit(tid, p.label, "mcp_audit.export", {"format": fmt, **{k: v for k, v in f.items() if v is not None}},
                None, source="api")
    keys, apps = _key_names(store, tid), store.oauth_grant_names(tid)

    def rows():
        after, n = 0, 0
        while n < MAX_EXPORT_ROWS:
            page = store.mcp_audit_rows(tid, after_id=after, limit=min(EXPORT_PAGE, MAX_EXPORT_ROWS - n), **f)
            if not page:
                return
            for r in page:
                yield _view_row(r, keys, apps)
            n += len(page)
            after = page[-1]["id"]
            if len(page) < EXPORT_PAGE:
                return

    def csv_chunks():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(EXPORT_COLUMNS)
        for i, r in enumerate(rows(), 1):
            r = safe_json.clean(r)
            w.writerow([csv_cell(r.get(c)) for c in EXPORT_COLUMNS])
            if i % 500 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    def json_chunks():
        head = {"log": "mcp", "generated": now_iso(), **{k: v for k, v in f.items() if v is not None}}
        yield json.dumps(head)[:-1] + ',"rows":['
        n = 0
        for r in rows():
            yield ("," if n else "") + safe_json.dumps(r, separators=(",", ":"))
            n += 1
        yield f'],"row_count":{n},"truncated":{"true" if n >= MAX_EXPORT_ROWS else "false"}}}'

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(csv_chunks() if fmt == "csv" else json_chunks(),
                             media_type="text/csv; charset=utf-8" if fmt == "csv" else "application/json",
                             headers={"Content-Disposition": f'attachment; filename="tradecomply-mcp-calls-{stamp}'
                                                             f'.{fmt}"', "Cache-Control": "no-store"})


@router.get("/policy")
async def mcp_policy(request: Request, p: Principal = Depends(office_up)):
    """How /mcp is protected on this server, the limits, and the tool categories a key's scope can name."""
    platform = platform_of(request)
    s = platform.settings
    index = await tool_index(platform)
    return {"require_key": s.mcp_require_key, "public_demo": s.mcp_public_demo, "mode_reason": s.mcp_access_reason,
            "default_scope": "All read-only tools", "read_only_tools": sum(1 for m in index.values() if m["read_only"]),
            "tool_count": len(index), "key_rate_limit_per_min": platform.mcp_key_limiter.limit,
            "key_rate_limit_max": KEY_RATE_MAX, "tenant_rate_limit_per_min": platform.mcp_tenant_limiter.limit,
            "anonymous_rate_limit_per_min": platform.mcp_anon_limiter.limit, "max_key_days": MAX_KEY_DAYS,
            "categories": categories(index)}


TOOL_HASH_FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")


def tool_definition_sha256(tool: dict) -> str:
    """SHA-256 of one tool definition as tools/list sends it: the fields in TOOL_HASH_FIELDS that are present, as
    canonical JSON (sorted keys, "," and ":" separators, UTF-8). _meta is left out (FastMCP bookkeeping)."""
    return args_sha256({k: tool[k] for k in TOOL_HASH_FIELDS if tool.get(k) is not None})


@router.get("/tool-hashes")
async def tool_hashes(request: Request, p: Principal = Depends(anyone)):
    """A fingerprint of every tool definition this server publishes, so a client or gateway can pin the reviewed
    definitions and notice any change (OWASP MCP Security Cheat Sheet, section 2; MCP03 tool poisoning)."""
    from fastmcp import Client
    platform = platform_of(request)
    from .api import tool_specs
    specs = await tool_specs(platform)
    cached = getattr(platform, "mcp_tool_hashes", None)
    if not (cached and cached[0] is specs):
        async with Client(platform.mcp) as c:
            tools = await c.list_tools()
        wire = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
        hashes = {t["name"]: tool_definition_sha256(t) for t in wire}
        manifest = hashlib.sha256("\n".join(f"{n}:{hashes[n]}" for n in sorted(hashes)).encode()).hexdigest()
        cached = (specs, hashes, manifest)
        platform.mcp_tool_hashes = cached
    _, hashes, manifest = cached
    return {"algorithm": "sha256 over canonical JSON (sort_keys, separators ',' ':', UTF-8) of each tool's "
                         + ", ".join(TOOL_HASH_FIELDS) + " as tools/list returns them",
            "manifest_algorithm": "sha256 over the sorted lines '<name>:<tool sha256>' joined by newlines",
            "tool_count": len(hashes), "manifest_sha256": manifest, "tools": hashes}


# ---------------------------------------------------------------------------- key policy validation (api.py)
def validate_rate(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= KEY_RATE_MAX:
        raise ValueError(f"rate_limit_per_min must be a whole number from 1 to {KEY_RATE_MAX}")
    return value


def validate_expiry(expires_at=None, expires_in_days=None, now: datetime | None = None) -> str | None:
    now = now or datetime.now(timezone.utc)
    if expires_in_days is not None:
        if isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int) or \
                not 1 <= expires_in_days <= MAX_KEY_DAYS:
            raise ValueError(f"expires_in_days must be from 1 to {MAX_KEY_DAYS}")
        return to_store_time(now + timedelta(days=expires_in_days))
    if expires_at in (None, ""):
        return None
    try:
        when = to_store_time(expires_at)
    except ValueError:
        raise ValueError("expires_at must be a date (YYYY-MM-DD) or an ISO timestamp") from None
    if when <= to_store_time(now):
        raise ValueError("expires_at must be in the future")
    if when > to_store_time(now + timedelta(days=MAX_KEY_DAYS)):
        raise ValueError(f"expires_at can be at most {MAX_KEY_DAYS} days away")
    return when


def key_expired(key: dict, now: datetime | None = None) -> bool:
    exp = key.get("expires_at")
    return bool(exp) and exp <= to_store_time(now or datetime.now(timezone.utc))
