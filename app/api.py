"""JSON API for the web app and for API-key clients: tools, projects, subs, documents, field logs, dashboard,
calendar, users, API keys and the audit log. Every handler scopes data to the caller's tenant.

Database calls never run on the event loop: on PostgreSQL each one is a network round trip (and can wait for a free
pooled connection), and one slow query there would stall every request, /health and /mcp included. Handlers that
only talk to the store are plain `def` (FastAPI runs them in its thread pool); `async def` handlers send their store
calls to a worker thread with anyio.to_thread."""
import csv
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastmcp import Client
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from tradecomply import integrations_guide, logic, professionals

from . import auth_links, dashboard
from . import mcp_governance as governance
from .auth import (EMAIL_RE, Principal, anyone, get_principal, hash_password, license_digits, new_api_key, office_up,
                   owner_only, platform_of)
from .billing import PRICE_USD_MONTH, base_url, entitlement, require_entitled
from .mailer import valid_recipient
from .store import FIELD_LOG_KINDS, IntegrityError

logger = logging.getLogger("tradecomply.api")
router = APIRouter(prefix="/api", tags=["api"])
TOOL_CACHE_S = 30


# ---------------------------------------------------------------------------- input models
def _iso_date(v):
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    if not s:
        return None
    parsed = parse_date(s)
    if not parsed:
        raise ValueError(f"use a date like 2026-03-15 (years {MIN_YEAR}-{MAX_YEAR})")
    return parsed


def _blank_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


IsoDate = Annotated[Optional[str], BeforeValidator(_iso_date)]
def Str(max_length: int):
    """Optional string, blank -> None, length-limited (the limit applies to the string, never to None)."""
    return Annotated[Optional[Annotated[str, Field(max_length=max_length)]], BeforeValidator(_blank_none)]


Text = Str(2000)
LongText = Str(500_000)


class ProjectIn(BaseModel):
    name: Str(200) = None
    address: Text = None
    city: Text = None
    county: Text = None
    owner_name: Text = None
    lender_name: Text = None
    public_or_private: Optional[Literal["public", "private"]] = None
    contract_price: Optional[float] = Field(None, ge=0)
    first_furnishing_date: IsoDate = None
    last_furnishing_date: IsoDate = None
    completion_date: IsoDate = None
    noc_date: IsoDate = None
    start_date: IsoDate = None
    status: Optional[Literal["planned", "active", "completed", "closed"]] = None
    claimant_role: Optional[Literal[dashboard.CLAIMANT_ROLES]] = None   # stored in extra
    extra: Optional[dict] = None


class SubIn(BaseModel):
    name: Str(200) = None
    license_number: Annotated[Optional[str], BeforeValidator(lambda v: license_digits(str(v)) if v else None)] = None
    scope: Text = None
    required_class: Str(20) = None
    coi_expiry: IsoDate = None
    wc_required: Optional[bool] = None
    w9_on_file: Optional[bool] = None
    dir_number: Str(40) = None
    email: Str(254) = None
    phone: Str(40) = None
    project_id: Optional[int] = None


class DocumentIn(BaseModel):
    """What a person can write. `verified` and `source_tool` are set only by the server (from a signed tool
    result), so a typed-up document can't wear a "Verified against official source" badge."""
    title: Str(300) = None
    kind: Str(60) = None
    body_markdown: LongText = None
    project_id: Optional[int] = None


class FromResultIn(BaseModel):
    tool: str = Field(max_length=200)
    result: dict
    result_sig: Optional[str] = Field(None, max_length=100)    # from POST /api/tools/{name}
    title: Str(300) = None
    project_id: Optional[int] = None
    field: Optional[str] = Field(None, max_length=200)


class Attendee(BaseModel):
    name: str = Field(max_length=200)
    signature: Optional[str] = Field(None, max_length=400_000)     # PNG data URL from the signature pad
    signed_at: Optional[str] = Field(None, max_length=40)
    acknowledged: Optional[bool] = None


class FieldLogIn(BaseModel):
    client_uuid: Optional[str] = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    project_id: Optional[int] = None
    kind: Literal["toolbox_talk", "inspection", "incident"]
    date: IsoDate
    topic: Text = None
    attendees: list[Attendee] = Field(default_factory=list, max_length=200)
    notes: LongText = None


class SyncIn(BaseModel):
    logs: list[dict] = Field(max_length=500)


RESERVED_SETTINGS = {"ics_gen", "assistant_off"}     # server-owned or owner-only keys inside tenant settings


class TenantIn(BaseModel):
    company_name: Str(200) = None
    cslb_license: Str(20) = None
    settings: Optional[dict] = None


class UserIn(BaseModel):
    email: str = Field(max_length=254)
    role: Literal["owner", "office", "field"] = "field"
    name: Optional[str] = Field(None, max_length=200)


class UserPatch(BaseModel):
    role: Optional[Literal["owner", "office", "field"]] = None
    name: Optional[str] = Field(None, max_length=200)


class ScopesIn(BaseModel):
    """Which tools an API key may call (docs/mcp-security.md). Leave tools and categories out for the default (every
    read-only tool); give either one, even empty, for an allow-list."""
    model_config = ConfigDict(extra="forbid")
    tools: Optional[list[Annotated[str, Field(max_length=100)]]] = Field(None, max_length=500)
    categories: Optional[list[Annotated[str, Field(max_length=100)]]] = Field(None, max_length=100)
    rest_api: bool = True


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: Optional[ScopesIn] = None
    rate_limit_per_min: Optional[int] = None
    expires_in_days: Optional[int] = None
    expires_at: Optional[str] = Field(None, max_length=40)


class KeyPatch(BaseModel):
    """Fields left out stay as they are; null resets scopes and rate_limit_per_min to the default and removes the
    expiry."""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    scopes: Optional[ScopesIn] = None
    rate_limit_per_min: Optional[int] = None
    expires_in_days: Optional[int] = None
    expires_at: Optional[str] = Field(None, max_length=40)


# ---------------------------------------------------------------------------- helpers
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y")


MIN_YEAR, MAX_YEAR = 1900, 2200     # anything outside is a typo, and year 9999 overflows date arithmetic


def parse_date(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    parsed = None
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(s[:20], fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = date.fromisoformat(s[:10])
        except ValueError:
            return None
    if not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return None
    return parsed.isoformat()


def _p(request: Request):
    return platform_of(request)


def _store(request: Request):
    return platform_of(request).store


def _check_project(request: Request, tenant_id: str, project_id: int | None):
    if project_id is not None and not _store(request).get("projects", tenant_id, project_id):
        raise HTTPException(422, "project_id does not match one of your projects")


def _not_found(what: str):
    raise HTTPException(404, f"{what} not found")


def _tenant_public(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "company_name", "cslb_license", "plan", "status", "trial_ends", "created",
                                  "settings")} | {"has_billing_account": bool(t.get("stripe_customer_id"))}


def _mcp_url(request: Request) -> str:
    return base_url(request) + "/mcp"


# ---------------------------------------------------------------------------- me / config / tenant / users / keys
@router.get("/config")
async def config(request: Request):
    s = _p(request).settings
    return {"product": "TradeComply CA", "mcp_url": _mcp_url(request), "mcp_require_key": s.mcp_require_key,
            "mcp_public_demo": s.mcp_public_demo,
            "stripe_enabled": s.stripe_enabled, "local_mode": s.local_mode, "price_usd_month": PRICE_USD_MONTH,
            "trial_days": s.trial_days, "claimant_roles": dashboard.ROLE_LABELS,
            "support_contact": s.support_contact,       # SUPPORT_EMAIL / SUPPORT_URL of whoever runs this server
            "email_links": auth_links.enabled(_p(request))}     # invites, resets and confirmations by email


@router.get("/me")
def me(request: Request, p: Principal = Depends(anyone)):
    from . import assistant         # imports this module (execute_tool)
    store = _store(request)
    tenant = store.get_tenant(p.tenant_id)
    user = store.user_by_id(p.user_id) if p.user_id else None
    return {"user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"],
                     "must_change_password": bool(user["must_change_password"]),
                     "email_confirmed": bool(user["email_confirmed_at"]),
                     "confirm_link_sent": not user["email_confirmed_at"] and store.confirm_link_sent(p.tenant_id,
                                                                                                     user["id"])}
            if user else
            {"id": None, "email": None, "name": p.label, "role": p.role, "must_change_password": False,
             "email_confirmed": False, "confirm_link_sent": False},
            "via": p.via, "tenant": _tenant_public(tenant), "entitlement": entitlement(tenant),
            "mcp_url": _mcp_url(request), "assistant": assistant.status(_p(request), p, tenant)}


@router.patch("/tenant")
def update_tenant(body: TenantIn, request: Request, p: Principal = Depends(office_up)):
    store = _store(request)
    fields = {}
    if body.company_name is not None:
        fields["company_name"] = body.company_name
    if body.cslb_license is not None:
        fields["cslb_license"] = license_digits(body.cslb_license)
    if body.settings is not None:
        mine = {str(k)[:100]: v for k, v in body.settings.items() if k not in RESERVED_SETTINGS}

        def merge(current: dict) -> dict:       # under a row lock: a concurrent save or feed rotation isn't lost
            merged = {**current, **mine}
            if len(json.dumps(merged)) > 20000:
                raise HTTPException(413, "settings too large")
            return merged
        store.modify_tenant_settings(p.tenant_id, merge)
    return _tenant_public(store.update_tenant(p.tenant_id, **fields))


@router.get("/users")
def list_users(request: Request, p: Principal = Depends(office_up)):
    return _store(request).list_users(p.tenant_id)


@router.post("/users")
async def invite_user(body: UserIn, request: Request, p: Principal = Depends(owner_only)):
    """Add someone to the company: they get an invite link to set their own password (app/auth_links.py), emailed
    when sending is set up and always shown to the owner to copy. Nobody, the owner included, ever knows it."""
    import secrets
    store = _store(request)
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email) or not valid_recipient(email):
        raise HTTPException(422, "Enter a valid email address")
    if await anyio.to_thread.run_sync(store.user_by_email, email):
        raise HTTPException(409, "That email already has an account")
    # a real hash of a secret nobody knows: a pending invitee's login takes the same time as anyone's, and fails
    pw_hash = await anyio.to_thread.run_sync(hash_password, secrets.token_urlsafe(32))
    name = " ".join((body.name or "").split()) or None
    try:
        u = await anyio.to_thread.run_sync(lambda: store.create_user(p.tenant_id, email, pw_hash, body.role,
                                                                     name=name))
    except IntegrityError:      # created a moment ago, after the check above (unique email index)
        raise HTTPException(409, "That email already has an account")
    return await auth_links.invite(request, p, u, "user.invite")


async def _member(request: Request, p: Principal, user_id: int) -> dict:
    u = await anyio.to_thread.run_sync(_store(request).user_by_id, user_id)
    if not u or u["tenant_id"] != p.tenant_id:
        _not_found("User")
    return u


@router.post("/users/{user_id}/invite")
async def resend_invite(user_id: int, request: Request, p: Principal = Depends(owner_only)):
    """A new invite link for someone who hasn't joined yet (the old links stop working). Also for people added
    before invite links, who got a temporary password: it stops working too."""
    import secrets
    u = await _member(request, p, user_id)
    if u["last_login"]:
        raise HTTPException(409, "They already joined. Make a reset link instead.")
    if u["must_change_password"]:
        pw_hash = await anyio.to_thread.run_sync(hash_password, secrets.token_urlsafe(32))
        u = await anyio.to_thread.run_sync(lambda: _store(request).update_user(
            p.tenant_id, user_id, pw_hash=pw_hash, must_change_password=0))
    return await auth_links.invite(request, p, u, "user.invite_resent")


@router.post("/users/{user_id}/reset-link")
async def make_reset_link(user_id: int, request: Request, p: Principal = Depends(owner_only)):
    """A one-time password reset link for a team member, for the owner to send them (text or in person). Nothing
    changes on their account until they use it."""
    if user_id == p.user_id:
        raise HTTPException(422, "Change your own password in Your account.")
    u = await _member(request, p, user_id)
    if not u["last_login"]:
        raise HTTPException(409, "They haven't joined yet. Send a new invite link instead.")
    return await auth_links.owner_reset_link(request, p, u)


@router.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, request: Request, p: Principal = Depends(owner_only)):
    store = _store(request)
    if user_id == p.user_id and body.role and body.role != "owner":
        raise HTTPException(422, "You can't remove your own owner role")
    u = store.update_user(p.tenant_id, user_id, **body.model_dump(exclude_none=True))
    if not u:
        _not_found("User")
    if u["role"] == "field":           # keys act with the office role; a field user can't keep one
        store.revoke_user_api_keys(p.tenant_id, user_id)
    return {k: u[k] for k in ("id", "email", "name", "role")}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, p: Principal = Depends(owner_only)):
    if user_id == p.user_id:
        raise HTTPException(422, "You can't delete your own account here")
    store = _store(request)
    if not store.delete_user(p.tenant_id, user_id):
        _not_found("User")
    store.revoke_user_api_keys(p.tenant_id, user_id)      # their agents lose access with them
    return {"ok": True}


def _person_only(p: Principal, what: str = "manage API keys") -> None:
    """An API key can't create, widen or revoke keys: otherwise a narrowly scoped key could mint itself a broad
    one (OWASP MCP02, privilege escalation via scope creep)."""
    if p.via != "cookie":
        raise HTTPException(403, f"Sign in to TradeComply to {what}; an API key can't do this.")


def _key_policy(body, index: dict, patch: bool) -> dict:
    """Validated policy fields from a KeyIn/KeyPatch body (only those the client sent, for a patch)."""
    sent = body.model_fields_set
    out = {}
    try:
        if "scopes" in sent or not patch:
            out["scopes"] = governance.normalize_scopes(body.scopes.model_dump() if body.scopes else None, index)
        if "rate_limit_per_min" in sent or not patch:
            out["rate_limit_per_min"] = governance.validate_rate(body.rate_limit_per_min)
        if "expires_in_days" in sent and body.expires_in_days is not None:
            out["expires_at"] = governance.validate_expiry(expires_in_days=body.expires_in_days)
        elif "expires_at" in sent or not patch:
            out["expires_at"] = governance.validate_expiry(expires_at=body.expires_at)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return out


def _key_out(k: dict, index: dict) -> dict:
    return {**k, "scope": governance.describe_scopes(k.get("scopes"), index),
            "expired": governance.key_expired(k)}


@router.get("/keys")
async def list_keys(request: Request, p: Principal = Depends(office_up)):
    platform = _p(request)
    index = await governance.tool_index(platform)
    keys = await anyio.to_thread.run_sync(platform.store.list_api_keys, p.tenant_id)
    return [_key_out(k, index) for k in keys]


@router.post("/keys")
async def create_key(body: KeyIn, request: Request, p: Principal = Depends(office_up)):
    _person_only(p)
    platform = _p(request)
    index = await governance.tool_index(platform)
    policy = _key_policy(body, index, patch=False)
    plain, prefix, key_hash = new_api_key()

    def save():
        k = platform.store.create_api_key(p.tenant_id, body.name.strip(), key_hash, prefix, p.user_id, **policy)
        platform.store.audit(p.tenant_id, p.label, "api_key.create", {"key_id": k["id"], "name": k["name"],
                             "default_scope": policy["scopes"] is None,
                             "rate_limit_per_min": policy["rate_limit_per_min"],
                             "expires_at": policy["expires_at"]}, None, source="api")
        return k
    k = await anyio.to_thread.run_sync(save)
    return {**_key_out(k, index), "key": plain, "message": "Copy this key now. It won't be shown again."}


@router.patch("/keys/{key_id}")
async def update_key(key_id: int, body: KeyPatch, request: Request, p: Principal = Depends(office_up)):
    """Change an active key's name, tool scope, rate limit or expiry. Every change is in the activity log."""
    _person_only(p)
    platform = _p(request)
    index = await governance.tool_index(platform)
    fields = _key_policy(body, index, patch=True)
    if "name" in body.model_fields_set and body.name is not None:
        fields["name"] = body.name.strip()

    def save():
        before = platform.store.get_api_key(p.tenant_id, key_id)
        if not before or before["revoked"]:
            return None
        k = platform.store.update_api_key(p.tenant_id, key_id, **fields)
        if k:
            platform.store.audit(p.tenant_id, p.label, "api_key.update", {
                "key_id": key_id, "fields": sorted(fields),
                "scope_before": governance.describe_scopes(before.get("scopes"), index)["summary"],
                "scope_after": governance.describe_scopes(k.get("scopes"), index)["summary"]}, None, source="api")
        return k
    k = await anyio.to_thread.run_sync(save)
    if not k:
        _not_found("Active API key")
    return _key_out(k, index)


@router.delete("/keys/{key_id}")
def revoke_key(key_id: int, request: Request, p: Principal = Depends(office_up)):
    _person_only(p, "revoke API keys")
    store = _store(request)
    if not store.revoke_api_key(p.tenant_id, key_id):
        _not_found("Active API key")
    store.audit(p.tenant_id, p.label, "api_key.revoke", {"key_id": key_id}, None, source="api")
    return {"ok": True}


@router.get("/audit")
def audit(request: Request, p: Principal = Depends(office_up), limit: int = Query(200, ge=1, le=1000)):
    return _store(request).list_audit(p.tenant_id, limit)


@router.get("/audit/{audit_id}")
def audit_row(audit_id: int, request: Request, p: Principal = Depends(office_up)):
    row = _store(request).get_audit(p.tenant_id, audit_id)
    if not row:
        _not_found("Audit entry")
    return row


# ---------------------------------------------------------------------------- tools (discovered from the MCP server)
MODULE_LABELS = {
    "licensing": "Licenses & subcontractors", "payments": "Liens & getting paid", "contracts": "Contracts & waivers",
    "safety_programs": "Safety (Cal/OSHA)", "payroll": "Payroll & labor", "public_works": "Public works",
    "building": "Permits & building", "business_ops": "Business, tax & HR", "project_setup": "Project setup",
    "rulebook_tools": "Rulebook search", "other": "Other tools",
}
MODULE_ORDER = list(MODULE_LABELS)
KEYWORDS = [   # used only when a tool's source module doesn't say where it belongs
    ("public_works", r"prevailing|public_works|ecpr|dir_|apprentic|certified_payroll"),
    ("payroll", r"payroll|wage|overtime|meal|rest_break|timesheet|paycheck|final_pay"),
    ("payments", r"lien|prelim|retention|prompt_pay|stop_payment|payment"),
    ("licensing", r"license|cslb|subcontractor|qualifier|bond"),
    ("contracts", r"(^|_)contracts?($|_)|waiver|change_order|home_improvement"),
    ("safety_programs", r"safety|osha|iipp|heat|toolbox|injur|incident|hazard"),
    ("building", r"permit|title24|title_24|hers|building|inspection|code"),
    ("project_setup", r"project|onboard|setup"),
    ("rulebook_tools", r"rule|search|citation|lookup"),
    ("business_ops", r"tax|insurance|employee|business|hr_|notice"),
]


def module_for(name: str, py_module: str | None) -> str:
    if py_module and py_module.startswith("tradecomply."):
        mod = py_module.split(".")[1]
        if mod not in ("logic", "core", "rules", "licenses", "cslb"):
            return mod
    for mod, pattern in KEYWORDS:
        if re.search(pattern, name):
            return mod
    return "other"


WORDS = {"cslb": "CSLB", "osha": "OSHA", "cal": "Cal", "coi": "COI", "noc": "NOC", "dir": "DIR", "iipp": "IIPP",
         "hers": "HERS", "ppe": "PPE", "usd": "($)", "id": "ID", "w9": "W-9", "ecpr": "eCPR", "url": "link",
         "ft": "(ft)", "pct": "(%)", "naics": "NAICS", "llc": "LLC", "wc": "workers' comp"}


def humanize(name: str) -> str:
    words = [WORDS.get(w.lower(), w.lower()) for w in re.split(r"[_\s]+", name or "") if w]
    text = " ".join(words).replace("Cal OSHA", "Cal/OSHA")
    text = re.sub(r"^lookup\b", "look up", text)
    return text[:1].upper() + text[1:]


def _attr(obj, *names):
    """MCP SDK v2 renamed inputSchema -> input_schema; read whichever this version has."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


async def tool_specs(platform, refresh: bool = False) -> list[dict]:
    cached = platform.tool_cache
    if cached and not refresh and time.monotonic() - cached[0] < TOOL_CACHE_S:
        return cached[1]
    async with Client(platform.mcp) as c:
        tools = await c.list_tools()
    specs = []
    for t in tools:
        py_module = None
        try:
            server_tool = await platform.mcp.get_tool(t.name)
            fn = getattr(server_tool, "fn", None)
            py_module = getattr(fn, "__module__", None)
        except Exception:
            pass
        mod = module_for(t.name, py_module)
        ann = t.annotations.model_dump(by_alias=True, exclude_none=True) if getattr(t, "annotations", None) else {}
        title = getattr(t, "title", None) or ann.get("title")
        if not title or title.lower() == t.name.replace("_", " ").lower():
            title = humanize(t.name)
        specs.append({"name": t.name, "title": title,
                      "description": t.description or "",
                      "input_schema": _attr(t, "input_schema", "inputSchema") or {"type": "object", "properties": {}},
                      "output_schema": _attr(t, "output_schema", "outputSchema"), "annotations": ann,
                      "module": mod, "module_label": MODULE_LABELS.get(mod, humanize(mod))})
    specs.sort(key=lambda s: (MODULE_ORDER.index(s["module"]) if s["module"] in MODULE_ORDER else 99, s["name"]))
    platform.tool_cache = (time.monotonic(), specs)
    return specs


@router.get("/tools")
async def list_tools(request: Request, p: Principal = Depends(anyone)):
    specs = await tool_specs(_p(request))
    if p.via == "key":                                     # a key sees only the tools it may call
        index = await governance.tool_index(_p(request))
        specs = [s for s in specs if governance.scope_allows(p.scopes, s["name"], index)]
    modules: dict[str, dict] = {}
    for s in specs:
        modules.setdefault(s["module"], {"id": s["module"], "label": s["module_label"], "tools": []})["tools"].append(s)
    return {"count": len(specs), "modules": list(modules.values()), "tools": specs}


def _summarize(args):
    from .auth import _summarize_args
    return _summarize_args(args)


def _canon(value):
    """JSON as both Python and the browser will re-serialize it: whole floats become ints (JS has one number
    type), keys sorted."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return int(value)
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def sign_result(platform, tool: str, result) -> str:
    digest = hashlib.sha256(json.dumps(_canon(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                       default=str).encode()).hexdigest()
    return platform.signer.mac("result", f"{tool}:{digest}")[:48]


def result_signed(platform, tool: str, result: dict, sig: str | None) -> bool:
    """True if `result` is (exactly) what this server's tool returned. The web app unwraps {"result": x} results,
    so both shapes are accepted."""
    if not sig:
        return False
    return any(hmac.compare_digest(sign_result(platform, tool, r), sig) for r in (result, {"result": result}))


async def run_tool(platform, name: str, args: dict) -> dict:
    async with Client(platform.mcp) as c:
        r = await c.call_tool(name, args, raise_on_error=False)
    text = "\n".join(getattr(b, "text", "") for b in (r.content or []) if getattr(b, "text", None))
    structured = r.structured_content
    if structured is None and text and not r.is_error:
        try:
            structured = json.loads(text)
        except ValueError:
            structured = None
    if isinstance(structured, dict) and set(structured) == {"result"} and not isinstance(structured["result"], dict):
        structured = {"result": structured["result"]}
    out = {"tool": name, "is_error": bool(r.is_error), "structured": structured, "text": text}
    if isinstance(structured, dict) and not r.is_error:
        out["result_sig"] = sign_result(platform, name, structured)
    return out


@router.post("/tools/{name}")
async def call_tool(name: str, request: Request, p: Principal = Depends(anyone)):
    await authorize_tool(request, p, name)
    try:
        args = await request.json() if (await request.body()) else {}
    except ValueError:
        raise HTTPException(422, "Body must be a JSON object of tool arguments")
    if isinstance(args, dict) and isinstance(args.get("arguments"), dict) and len(args) == 1:
        args = args["arguments"]
    return await run_authorized_tool(request, p, name, args, source="api")


async def execute_tool(request: Request, p: Principal, name: str, args, source: str) -> dict:
    """Everything POST /api/tools/{name} does for one call, for callers inside the app (Ask TradeComply,
    app/assistant.py): the same checks, guards, activity-log row, errors and handoffs. Raises HTTPException as the
    route does."""
    await authorize_tool(request, p, name)
    return await run_authorized_tool(request, p, name, args, source=source)


async def authorize_tool(request: Request, p: Principal, name: str) -> None:
    """May this caller run this tool now: subscription (402), the tool exists (404), an API key's rate limits (429)
    and tool scope (403)."""
    platform = _p(request)
    await anyio.to_thread.run_sync(require_entitled, platform, p)
    names = {s["name"] for s in await tool_specs(platform)}
    if name not in names:
        names = {s["name"] for s in await tool_specs(platform, refresh=True)}
        if name not in names:
            raise HTTPException(404, f"No tool named {name!r} on this server")
    if p.via == "key":
        # The key's own rate limit and the company's limit cover tool calls here as on /mcp (the same counters),
        # so a key limited to N calls a minute can't run unlimited calls through this route instead.
        limits = [(platform.mcp_key_limiter, f"key|{p.key_id}", p.rate_limit),
                  (platform.mcp_tenant_limiter, f"tenant|{p.tenant_id}", None)]
        wait = max(lim.retry_after(k, cap) for lim, k, cap in limits)
        if wait:
            raise HTTPException(429, f"Too many tool calls for this API key. Retry in {wait} s.",
                                headers={"Retry-After": str(wait)})
        for lim, k, _cap in limits:
            lim.hit(k)
    if p.via == "key" and not governance.scope_allows(p.scopes, name, await governance.tool_index(platform)):
        raise HTTPException(403, f"This API key is not allowed to use the tool {name!r}. An owner or office user "
                                 "can change the key's tools in Settings > AI assistant (MCP) > API keys.")


async def run_authorized_tool(request: Request, p: Principal, name: str, args, source: str) -> dict:
    """Run a tool authorize_tool allowed, with the caller's guards; write the activity-log row (`source`: "api",
    "assistant"); a tool error is a 422 with a plain message; handoffs are added to a result."""
    platform = _p(request)
    if not isinstance(args, dict):
        raise HTTPException(422, "Body must be a JSON object of tool arguments")
    # mcp_client_config without a URL points at the server the user is signed in to, never a built-in address
    url_token = integrations_guide.SERVER_MCP_URL.set(_mcp_url(request))
    # who_must_sign given another tool's inputs runs that tool: the caller's key scope applies to it too
    guard_token = professionals.RUN_GUARD.set(await _run_guard(platform, p))
    try:
        out = await run_tool(platform, name, args)
    finally:
        professionals.RUN_GUARD.reset(guard_token)
        integrations_guide.SERVER_MCP_URL.reset(url_token)
    sc = out["structured"] if isinstance(out["structured"], dict) else {}
    verified = sc.get("verified") if isinstance(sc.get("verified"), bool) else None
    await anyio.to_thread.run_sync(lambda: platform.store.audit(p.tenant_id, p.label, name, _summarize(args), verified,
                                                                ok=not out["is_error"], source=source))
    if out["is_error"]:
        raise HTTPException(422, friendly_tool_error(out["text"]))
    attach_handoffs(out, name, args)
    return out


async def _run_guard(platform, p: Principal):
    """Which tools this caller may run through who_must_sign: an API key's own scope; people signed in to the web
    app may run every tool (professionals.py still runs only read-only ones)."""
    if p.via != "key":
        return lambda tool: True
    index = await governance.tool_index(platform)
    return lambda tool: governance.scope_allows(p.scopes, tool, index)


def attach_handoffs(out: dict, name: str, args: dict) -> None:
    """Add "handoffs" (the licensed or certified people this result needs, tradecomply/professionals.py) next to
    the result when any apply. Additive only: no existing field changes (result_sig still covers `structured`
    exactly), and a failure here is logged and the field left out, never the caller's error."""
    try:
        items = professionals.handoffs_for(name, out.get("structured"), args)
    except Exception:
        logger.exception("handoffs for %s could not be computed; result sent without them", name)
        return
    if items:
        out["handoffs"] = items


# ---------------------------------------------------------------------------- licensed professionals (finder)
@router.get("/professionals/types")
def professional_types(request: Request, p: Principal = Depends(anyone)):
    """Every kind of licensed or certified professional and its official directories, plus the 58 county names:
    the same JSON as the list_professional_types tool. The same for every user, so it may be cached."""
    return JSONResponse(professionals.list_professional_types().model_dump(),
                        headers={"Cache-Control": "private, max-age=300"})


@router.get("/professionals/find")
def find_professional(request: Request, p: Principal = Depends(anyone),
                      type: Optional[str] = Query(None, max_length=80, description="Professional type id"),
                      type_id: Optional[str] = Query(None, max_length=80, description="Same as type"),
                      need: Optional[str] = Query(None, max_length=500, description="Or the need in plain words"),
                      county: Optional[str] = Query(None, max_length=80),
                      city: Optional[str] = Query(None, max_length=80),
                      craft: Optional[str] = Query(None, max_length=80)):
    """Who the professional is, when the law requires one, the official directories (county-filtered where the
    official site supports it), how to check the license and what to bring: the same JSON as the
    find_licensed_professional tool. TradeComply links to official searches only and recommends no one."""
    try:
        found = professionals.find_licensed_professional(type or type_id, county, city, need, craft)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return found.model_dump()


def friendly_tool_error(text: str) -> str:
    """FastMCP reports a tool's exception as "Error calling tool 'x': ..." and bad arguments as a raw pydantic
    dump. Say the same thing plainly: field name in words, the problem, no error codes or doc links."""
    t = re.sub(r"^Error (?:calling|executing) tool '?[\w.-]+'?:\s*", "", (text or "").strip())
    m = re.match(r"^\d+ validation errors? for [^\n]*\n(.*)", t, re.S)
    if m:
        msgs, field = [], ""
        for line in m.group(1).splitlines():
            if not line.strip() or line.strip().startswith("For further information"):
                continue
            if not line.startswith(" "):
                field = line.strip()
                continue
            msg = re.sub(r"\s*\[type=.*$", "", line.strip())
            msg = {"Missing required argument": "required", "Field required": "required",
                   "Unexpected keyword argument": "not a field of this check"}.get(msg, msg)
            msg = msg.replace("Input should be", "must be").replace("Value error, ", "")
            parts = field.split(".")
            where = humanize(parts[0]) + "".join(f", row {int(x) + 1}" if x.isdigit() else f", {humanize(x).lower()}"
                                                 for x in parts[1:])
            msgs.append(f"{where}: {msg}")
        t = "Check these inputs. " + "; ".join(msgs) + "." if msgs else t
    return t[:800] or "The check could not run with these inputs."


# ---------------------------------------------------------------------------- license lookup (onboarding step 1)
@router.get("/license/{number}")
async def license_health(number: str, request: Request, p: Principal = Depends(anyone)):
    await anyio.to_thread.run_sync(require_entitled, _p(request), p)
    digits = license_digits(number)
    if not digits:
        raise HTTPException(422, "Enter a CSLB license number (digits)")
    return await anyio.to_thread.run_sync(dashboard.license_card, digits)


# ---------------------------------------------------------------------------- projects
def _project_payload(body: ProjectIn, existing: dict | None = None) -> dict:
    data = body.model_dump(exclude_unset=True)
    role = data.pop("claimant_role", None)
    extra = dict((existing or {}).get("extra") or {})
    if data.get("extra"):
        extra.update(data["extra"])
    if role:
        extra["claimant_role"] = role
    data["extra"] = extra
    if len(json.dumps(extra)) > 50000:
        raise HTTPException(413, "extra is too large")
    return data


@router.get("/projects")
def list_projects(request: Request, p: Principal = Depends(anyone)):
    return _store(request).list("projects", p.tenant_id)


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, request: Request, p: Principal = Depends(office_up)):
    if not body.name:
        raise HTTPException(422, "Project name is required")
    return _store(request).insert("projects", p.tenant_id, _project_payload(body))


@router.get("/projects/{project_id}")
def get_project(project_id: int, request: Request, p: Principal = Depends(anyone)):
    row = _store(request).get("projects", p.tenant_id, project_id)
    if not row:
        _not_found("Project")
    return row


@router.patch("/projects/{project_id}")
def patch_project(project_id: int, body: ProjectIn, request: Request, p: Principal = Depends(office_up)):
    def change(row: dict) -> dict:       # `extra` is merged under a row lock, so two edits at once both land
        data = _project_payload(body, row)
        if "name" in data and not data["name"]:
            raise HTTPException(422, "Project name can't be empty")
        return data
    row = _store(request).modify("projects", p.tenant_id, project_id, change)
    if not row:
        _not_found("Project")
    return row


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, request: Request, p: Principal = Depends(office_up)):
    if not _store(request).delete("projects", p.tenant_id, project_id):
        _not_found("Project")
    return {"ok": True}


@router.get("/projects/{project_id}/deadlines")
async def project_deadlines(project_id: int, request: Request, p: Principal = Depends(anyone)):
    platform = _p(request)
    await anyio.to_thread.run_sync(require_entitled, platform, p)
    row = await anyio.to_thread.run_sync(platform.store.get, "projects", p.tenant_id, project_id)
    if not row:
        _not_found("Project")
    today = logic.today_ca()
    items, notes = dashboard.project_deadlines(row, today)
    last = (await anyio.to_thread.run_sync(platform.store.last_field_log_dates, p.tenant_id,
                                           "toolbox_talk")).get(project_id)
    tb = dashboard.toolbox_item(row, last, today)
    if tb:
        items.append(tb)
    items.sort(key=lambda i: i["date"])
    return {"project": row, "items": items, "notes": notes}


# ---------------------------------------------------------------------------- subs
@router.get("/subs")
async def list_subs(request: Request, p: Principal = Depends(anyone), check: bool = False,
                    project_id: Optional[int] = None):
    platform = _p(request)
    rows = await anyio.to_thread.run_sync(lambda: platform.store.list(
        "subs", p.tenant_id, where={"project_id": project_id} if project_id else None, order="name asc"))
    if check:
        await anyio.to_thread.run_sync(require_entitled, platform, p)
        checks = await anyio.to_thread.run_sync(lambda: [dashboard.sub_check(s) for s in rows])
        for s, c in zip(rows, checks):
            s["check"] = c
    return rows


@router.post("/subs", status_code=201)
def create_sub(body: SubIn, request: Request, p: Principal = Depends(office_up)):
    if not body.name:
        raise HTTPException(422, "Subcontractor name is required")
    _check_project(request, p.tenant_id, body.project_id)
    return _store(request).insert("subs", p.tenant_id, body.model_dump(exclude_unset=True))


@router.get("/subs/{sub_id}")
def get_sub(sub_id: int, request: Request, p: Principal = Depends(anyone)):
    row = _store(request).get("subs", p.tenant_id, sub_id)
    if not row:
        _not_found("Subcontractor")
    return row


@router.get("/subs/{sub_id}/check")
async def check_sub(sub_id: int, request: Request, p: Principal = Depends(anyone)):
    platform = _p(request)
    await anyio.to_thread.run_sync(require_entitled, platform, p)
    row = await anyio.to_thread.run_sync(platform.store.get, "subs", p.tenant_id, sub_id)
    if not row:
        _not_found("Subcontractor")
    return await anyio.to_thread.run_sync(dashboard.sub_check, row)


@router.patch("/subs/{sub_id}")
def patch_sub(sub_id: int, body: SubIn, request: Request, p: Principal = Depends(office_up)):
    store = _store(request)
    if not store.get("subs", p.tenant_id, sub_id):
        _not_found("Subcontractor")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and not data["name"]:
        raise HTTPException(422, "Name can't be empty")
    _check_project(request, p.tenant_id, data.get("project_id"))
    return store.update("subs", p.tenant_id, sub_id, data)


@router.delete("/subs/{sub_id}")
def delete_sub(sub_id: int, request: Request, p: Principal = Depends(office_up)):
    if not _store(request).delete("subs", p.tenant_id, sub_id):
        _not_found("Subcontractor")
    return {"ok": True}


HEADER_ALIASES = {
    "name": {"name", "company", "company name", "business name", "subcontractor", "sub", "sub name", "vendor",
             "vendor name", "subcontractor name", "contractor"},
    "license_number": {"license number", "license", "license #", "license no", "lic", "lic #", "lic no",
                       "cslb", "cslb license", "cslb #", "cslb number", "cslb license number", "license num"},
    "scope": {"scope", "trade", "scope of work", "work", "description"},
    "required_class": {"required class", "class", "classification", "required classification", "license class"},
    "coi_expiry": {"coi expiry", "coi expiration", "coi exp", "coi", "insurance expiry", "insurance expiration",
                   "gl expiry", "gl expiration", "liability insurance expiry", "coi expiration date",
                   "insurance exp", "coi expires"},
    "wc_required": {"wc required", "workers comp required", "employees on site", "has employees", "wc",
                    "workers comp"},
    "w9_on_file": {"w9 on file", "w9", "w 9", "w-9", "w-9 on file"},
    "dir_number": {"dir number", "dir", "dir #", "dir registration", "dir registration number", "pwcr"},
    "email": {"email", "e-mail", "email address", "contact email"},
    "phone": {"phone", "phone number", "telephone", "cell"},
}
_ALIAS = {alias: field for field, names in HEADER_ALIASES.items() for alias in names}
TRUE = {"y", "yes", "true", "1", "x", "on file", "on_file", "required"}
FALSE = {"n", "no", "false", "0", "none", "not required"}
MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 3 * 1024 * 1024
csv.field_size_limit(64 * 1024)        # one cell; a bigger one is an error (422), not a 500


def _int_or_422(value, name: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(422, f"{name} must be a number")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(422, f"{name} must be a number")


def _norm_header(h: str) -> str:
    h = (h or "").replace("\ufeff", "").strip().lower().replace("_", " ").replace("'", "").replace(".", "")
    return re.sub(r"\s+", " ", h)


def decode_csv(raw: bytes) -> str:
    raw = raw.replace(b"\x00", b"")            # NUL bytes (from a mis-saved file) would end up in names
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_sub_csv(text: str) -> tuple[list[dict], list[dict]]:
    """Returns (rows, skipped). Tolerates a BOM, blank lines, ; or tab delimiters and common header names."""
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip().strip(",;\t")]
    if not lines:
        return [], [{"line": 0, "reason": "The file is empty"}]
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    reader = csv.reader(lines, delimiter=delim)
    header = [_norm_header(h) for h in next(reader)]
    fields = [_ALIAS.get(h) for h in header]
    if "name" not in fields and "license_number" not in fields:
        return [], [{"line": 1, "reason": "No 'name' or 'license_number' column found. Expected columns: name, "
                                          "license_number, scope, required_class, coi_expiry, wc_required, "
                                          "w9_on_file, dir_number"}]
    rows, skipped = [], []
    for i, values in enumerate(reader, start=2):
        if len(rows) >= MAX_IMPORT_ROWS:
            skipped.append({"line": i, "reason": f"Only the first {MAX_IMPORT_ROWS} rows are imported"})
            break
        rec: dict[str, Any] = {}
        for f, v in zip(fields, values):
            if f and v is not None and v.strip():
                rec[f] = v.strip()
        if not rec:
            continue
        out: dict[str, Any] = {}
        if rec.get("name"):
            out["name"] = rec["name"][:200]
        if rec.get("license_number"):
            out["license_number"] = license_digits(rec["license_number"])
            if not out["license_number"]:
                skipped.append({"line": i, "reason": f"License number {rec['license_number']!r} has no digits"})
                continue
        for f in ("scope", "required_class", "dir_number", "email", "phone"):
            if rec.get(f):
                out[f] = rec[f][:500]
        if rec.get("coi_expiry"):
            d = parse_date(rec["coi_expiry"])
            if not d:
                skipped.append({"line": i, "reason": f"COI expiry {rec['coi_expiry']!r} is not a date"})
                continue
            out["coi_expiry"] = d
        for f in ("wc_required", "w9_on_file"):
            if rec.get(f):
                v = rec[f].strip().lower()
                if v in TRUE:
                    out[f] = True
                elif v in FALSE:
                    out[f] = False
        if not out.get("name") and not out.get("license_number"):
            skipped.append({"line": i, "reason": "Row has no name or license number"})
            continue
        out["_line"] = i
        rows.append(out)
    return rows, skipped


@router.post("/subs/import")
async def import_subs(request: Request, p: Principal = Depends(office_up), project_id: Optional[int] = None):
    """Upload a CSV (multipart field 'file', a text/csv body, or JSON {"csv": "..."}). Re-importing the same license
    number updates that sub instead of adding a duplicate. Returns the risk report for the imported subs."""
    platform = _p(request)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(422, "Attach the CSV as the 'file' field")
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if form.get("project_id"):
            project_id = _int_or_422(form.get("project_id"), "project_id")
    elif "application/json" in ctype:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(422, "Body must be JSON like {\"csv\": \"name,license_number\\n...\"}")
        if not isinstance(body, dict):
            raise HTTPException(422, "Body must be a JSON object like {\"csv\": \"...\"}")
        raw = str(body.get("csv") or "").encode()
        if body.get("project_id") is not None:
            project_id = _int_or_422(body.get("project_id"), "project_id")
    else:
        raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "CSV is larger than 3 MB")
    await anyio.to_thread.run_sync(_check_project, request, p.tenant_id, project_id)
    try:
        rows, skipped = parse_sub_csv(decode_csv(raw))
    except csv.Error as e:
        raise HTTPException(422, f"Could not read this CSV ({str(e)[:120]}). Save it from Excel as 'CSV UTF-8' "
                                 "and try again.")
    store = platform.store

    def save():
        created, updated, saved = 0, 0, []
        for r in rows:
            line = r.pop("_line")
            if project_id:
                r["project_id"] = project_id
            if not r.get("name"):
                lic = logic.verify_license(r["license_number"])
                r["name"] = lic.business_name if lic.found else f"License {r['license_number']}"
            existing = store.sub_by_license(p.tenant_id, r["license_number"]) if r.get("license_number") else None
            if existing:
                saved.append(store.update("subs", p.tenant_id, existing["id"], r))
                updated += 1
            else:
                saved.append(store.insert("subs", p.tenant_id, r))
                created += 1
            saved[-1]["_line"] = line
        return created, updated, saved

    created, updated, saved = await anyio.to_thread.run_sync(save)
    report, counts = [], {"red": 0, "amber": 0, "green": 0}
    ent = entitlement(await anyio.to_thread.run_sync(store.get_tenant, p.tenant_id))
    if ent["ok"]:
        report = await anyio.to_thread.run_sync(lambda: [dashboard.sub_check(s) for s in saved])
        for r in report:
            counts[r["rag"]] += 1
    return {"created": created, "updated": updated, "skipped": skipped, "report": report, "counts": counts,
            "report_available": ent["ok"], "message": None if ent["ok"] else ent["message"]}


# ---------------------------------------------------------------------------- documents
MARKDOWN_KEYS = re.compile(r"markdown|(^|_)md$|^document$|^body$|_document$|^draft$|_text$|^letter$|^notice$")


def markdown_fields(result: dict, prefix: str = "") -> list[tuple[str, str]]:
    found = []
    for k, v in (result or {}).items():
        if isinstance(v, str) and (MARKDOWN_KEYS.search(k) and ("\n" in v or v.startswith("#"))):
            found.append((prefix + k, v))
        elif isinstance(v, dict):
            found.extend(markdown_fields(v, prefix + k + "."))
        elif isinstance(v, list):          # e.g. programs[].document_outline_md -> "programs.0.document_outline_md"
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    found.extend(markdown_fields(item, f"{prefix}{k}.{i}."))
    return found


# A saved result is a record people print, file and hand to others, so it reads like a report: Yes/No instead of
# True/False, $ amounts, dates as "Nov 25, 2026", enum values in words, and no machine ids (rule ids are listed as
# Sources at the end). Mirrors how the web app shows a result.
_HIDE_KEYS = re.compile(r"^(rule_ids?|citation_ids|unverified_rule_ids|internal_\w+)$")
_NOT_MONEY = re.compile(r"(hours|days|count|pct|percent|periods?|weeks|months|years|(^|_)ft$|stories|employees|workers|ratio|"
                        r"multiplier|number|kwh|sq_ft|square|rate_per|(^|_)rate$|score|(^|_)mod$|minutes|age)")
_MONEY = re.compile(r"(usd|amount|price|fee|cost|wage|penalt|premium|total|owed|shortfall|gross|bond|payment|deposit|"
                    r"retention|subsidy|valuation)")
_MONEY_PARENT = re.compile(r"(_usd$|fees|line_items|costs|penalt|amounts|wages|premiums|exposure)")
_RULE_IDS = re.compile(r"\s*\[[a-z0-9]+(?:[-.][a-z0-9]+){2,}(?:,\s*[a-z0-9]+(?:[-.][a-z0-9]+)+)*\]")
_RISK_WORDS = {"low": "Low risk", "medium": "Medium risk", "high": "High risk", "do_not_use": "Do not use"}


def _is_money(key: str, ctx: bool = False) -> bool:
    return bool(re.search(r"(^|_)usd($|_)", key)) or ((bool(_MONEY.search(key)) or ctx) and not _NOT_MONEY.search(key))


def _md_value(key: str, v, ctx: bool = False) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        if _is_money(key, ctx):
            return f"${v:,.0f}" if float(v).is_integer() else f"${v:,.2f}"
        return f"{v:,}" if isinstance(v, int) else f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, str):
        if re.fullmatch(r"https?://\S+", v):
            host = re.sub(r"^www\.", "", v.split("/")[2])
            return f"[{host}]({v})"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            try:
                d = date.fromisoformat(v)
                return f"{d:%b} {d.day}, {d.year}"
            except ValueError:
                return v
        if re.fullmatch(r"[a-z]+(_[a-z0-9]+)*", v) and len(v) <= 40:
            return _RISK_WORDS.get(v) or humanize(v)
        snap = re.fullmatch(r"CSLB master file \(snapshot (\d{4}-\d{2}-\d{2})\)", v)
        if snap:
            return f"CSLB public license data as of {_md_value('', snap.group(1))}"
        return _RULE_IDS.sub("", v)
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return ", ".join(_md_value(key, x, ctx) for x in v)
        return f"{len(v)} item{'' if len(v) == 1 else 's'}"
    if isinstance(v, dict):
        return "; ".join(f"{humanize(k)}: {_md_value(k, x, ctx)}" for k, x in v.items() if not _hidden(k, x))
    return str(v)


def _md_cell(v, key: str = "", ctx: bool = False) -> str:
    if v is None or v == "" or v == []:
        return "—"
    return _md_value(key, v, ctx).replace("|", "\\|").replace("\n", " ")


def _hidden(key: str, val, depth: int = 1) -> bool:
    return (bool(_HIDE_KEYS.match(key)) or (key == "unverified" and isinstance(val, list)) or val is None or val == "" or val == [] or val == {}
            or (depth > 0 and key in ("verified", "citations", "disclaimer"))
            or (key == "key" and isinstance(val, str) and bool(re.fullmatch(r"[a-z0-9_-]+", val))))


def _simple_first(d: dict) -> list:
    """Plain values first, then lists and sections, so a top-level value never reads as part of the section above it."""
    return sorted(d.items(), key=lambda kv: isinstance(kv[1], (dict, list)) and not (
        isinstance(kv[1], list) and len(kv[1]) <= 6 and all(not isinstance(x, (dict, list)) and len(str(x)) <= 12
                                                            for x in kv[1])))


def _label(key: str, val, ctx: bool) -> str:
    label = humanize(key)
    return label[:-4] if label.endswith(" ($)") and isinstance(val, (int, float)) and _is_money(key, ctx) else label


def result_to_markdown(tool: str, result: dict, title: str | None = None) -> str:
    out = [f"# {title or humanize(tool)}", ""]
    if result.get("verified") is True:
        dates = sorted({c.get("checked_on") for c in result.get("citations") or [] if isinstance(c, dict)
                        and c.get("checked_on")})
        out.append(f"**Verified against official source{(' on ' + _md_value('', dates[-1])) if dates else ''}.**")
    else:
        out.append("**Not yet verified against the official source.**")
    out.append("")
    if result.get("overall_risk"):
        out.append(f"- **Risk:** {_md_value('overall_risk', result['overall_risk'])}")
    if result.get("recommended_action"):
        out.append(f"- **What to do:** {_md_value('recommended_action', result['recommended_action'])}")
    skip = {"verified", "citations", "disclaimer", "overall_risk", "recommended_action"}

    def render(key, val, level, ctx=False):
        label = humanize(key)
        ctx = ctx or bool(_MONEY_PARENT.search(key))
        if isinstance(val, dict):
            out.extend(["", f"{'#' * min(6, level)} {label[:-4] if ctx and label.endswith(' ($)') else label}", ""])
            for k2, v2 in _simple_first(val):
                if not _hidden(k2, v2):
                    render(k2, v2, level + 1, ctx)
            out.append("")
        elif isinstance(val, list) and len(val) <= 6 and all(not isinstance(x, (dict, list)) and len(str(x)) <= 12
                                                             for x in val):
            out.append(f"- **{label}:** {_md_cell(val, key, ctx)}")          # e.g. license classes: one line
        elif isinstance(val, list):
            out.extend(["", f"{'#' * min(6, level)} {label[:-4] if label.endswith(' ($)') else label}", ""])
            if all(isinstance(x, dict) for x in val):
                cols = [c for c in dict.fromkeys(k for x in val for k in x)
                        if not _HIDE_KEYS.match(c) and c not in ("citations", "disclaimer", "unverified")
                        and any(x.get(c) not in (None, "", [], {}) for x in val)][:8]
                out.append("| " + " | ".join(_label(c, 0, ctx) if _is_money(c, ctx) else humanize(c) for c in cols) + " |")
                out.append("|" + "---|" * len(cols))
                for x in val:
                    out.append("| " + " | ".join(_md_cell(x.get(c), c, ctx) for c in cols) + " |")
            else:
                out.extend(f"- {_md_cell(x, key, ctx)}" for x in val)
            out.append("")
        elif isinstance(val, str) and "\n" in val:
            out.extend(["", f"{'#' * min(6, level)} {label}", "", val, ""])
        else:
            out.append(f"- **{_label(key, val, ctx)}:** {_md_cell(val, key, ctx)}")
    for k, v in _simple_first(result):
        if k not in skip and not _hidden(k, v, depth=0):
            render(k, v, 2)
    out = [line for i, line in enumerate(out) if line or (i and out[i - 1])]      # no double blank lines
    keys = set()

    def collect(o, depth=0):
        if depth > 4:
            return
        if isinstance(o, dict):
            keys.update(k for k in o if "_" in k)
            for v in o.values():
                collect(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:5]:
                collect(v, depth + 1)
    collect(result)
    # notes refer to fields by their names ("shown in data_source"): use the labels the reader sees
    out = [re.sub(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", lambda m: f"“{humanize(m.group(1))}”" if m.group(1) in keys
                  else m.group(0), line) if "](http" not in line else line for line in out]
    cites = result.get("citations") or []
    if cites:
        out += ["", "## Sources", ""]
        for c in cites:
            if isinstance(c, dict):
                line = c.get("citation") or c.get("rule_id") or ""
                if c.get("source"):
                    line += f" ({c['source']})"
                if c.get("checked_on"):
                    line += f", checked {_md_value('', c['checked_on'])}"
                if c.get("status") and c["status"] not in ("confirmed", "corrected"):
                    line += " (not yet verified)"
                out.append(f"- {line}")
            else:
                out.append(f"- {c}")
    if result.get("disclaimer"):
        out += ["", "---", "", f"_{result['disclaimer']}_"]
    return "\n".join(out).strip() + "\n"


@router.get("/documents")
def list_documents(request: Request, p: Principal = Depends(anyone), project_id: Optional[int] = None):
    rows = _store(request).list("documents", p.tenant_id, where={"project_id": project_id} if project_id else None)
    for r in rows:
        r["excerpt"] = (r.pop("body_markdown") or "")[:240]
    return rows


@router.post("/documents", status_code=201)
def create_document(body: DocumentIn, request: Request, p: Principal = Depends(office_up)):
    if not body.title:
        raise HTTPException(422, "Title is required")
    _check_project(request, p.tenant_id, body.project_id)
    data = body.model_dump(exclude_unset=True)
    data.setdefault("kind", "note")
    data["created_by"] = p.label
    return _store(request).insert("documents", p.tenant_id, data)


@router.post("/documents/from_result", status_code=201)
def document_from_result(body: FromResultIn, request: Request, p: Principal = Depends(office_up)):
    """Save a tool result as a document: uses the result's markdown field(s) when it has them, otherwise a
    readable rendering of the whole result, with sources and the disclaimer."""
    _check_project(request, p.tenant_id, body.project_id)
    if len(json.dumps(body.result, default=str)) > 2_000_000:
        raise HTTPException(413, "Result too large to save")
    genuine = result_signed(_p(request), body.tool, body.result, body.result_sig)
    if not genuine:     # not provably this server's output: keep the content, drop the claims
        body.result = {k: v for k, v in body.result.items() if k != "verified"}
    fields = markdown_fields(body.result)
    if body.field:
        fields = [f for f in fields if f[0] == body.field] or fields
    if fields:
        md = "\n\n---\n\n".join(v.strip() for _, v in fields) + "\n"
        if body.result.get("disclaimer") and body.result["disclaimer"] not in md:
            md += f"\n\n---\n\n_{body.result['disclaimer']}_\n"
    else:
        md = result_to_markdown(body.tool, body.result, body.title)
    verified = body.result.get("verified") if genuine and isinstance(body.result.get("verified"), bool) else None
    return _store(request).insert("documents", p.tenant_id, {
        "title": body.title or humanize(body.tool), "kind": "tool_result" if genuine else "note", "body_markdown": md,
        "project_id": body.project_id, "source_tool": body.tool if genuine else None, "verified": verified,
        "created_by": p.label})


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, request: Request, p: Principal = Depends(anyone)):
    row = _store(request).get("documents", p.tenant_id, doc_id)
    if not row:
        _not_found("Document")
    return row


@router.patch("/documents/{doc_id}")
def patch_document(doc_id: int, body: DocumentIn, request: Request, p: Principal = Depends(office_up)):
    store = _store(request)
    row = store.get("documents", p.tenant_id, doc_id)
    if not row:
        _not_found("Document")
    data = body.model_dump(exclude_unset=True)
    _check_project(request, p.tenant_id, data.get("project_id"))
    if "body_markdown" in data and data["body_markdown"] != row["body_markdown"]:
        data["verified"] = None        # edited text is no longer the verified tool output
    return store.update("documents", p.tenant_id, doc_id, data)


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, request: Request, p: Principal = Depends(office_up)):
    if not _store(request).delete("documents", p.tenant_id, doc_id):
        _not_found("Document")
    return {"ok": True}


# ---------------------------------------------------------------------------- field logs
def _field_row(log: FieldLogIn, p: Principal) -> dict:
    attendees = [{"name": a.name, "signed": bool(a.signature) or bool(a.acknowledged), "signed_at": a.signed_at}
                 for a in log.attendees]
    signatures = [{"name": a.name, "image": a.signature, "signed_at": a.signed_at}
                  for a in log.attendees if a.signature]
    for s in signatures:
        if not s["image"].startswith("data:image/png;base64,"):
            raise ValueError("signatures must be PNG data URLs")
    if not log.date:
        raise ValueError("date is required")
    return {"client_uuid": log.client_uuid, "project_id": log.project_id, "kind": log.kind, "date": log.date,
            "topic": log.topic, "attendees": attendees, "signatures": signatures, "notes": log.notes,
            "created_by": p.label}


@router.get("/field_logs")
def list_field_logs(request: Request, p: Principal = Depends(anyone), project_id: Optional[int] = None,
                    kind: Optional[str] = None, include_signatures: bool = False):
    where = {}
    if project_id:
        where["project_id"] = project_id
    if kind:
        if kind not in FIELD_LOG_KINDS:
            raise HTTPException(422, "unknown kind")
        where["kind"] = kind
    rows = _store(request).list("field_logs", p.tenant_id, where=where, order="date desc", limit=500)
    if not include_signatures:
        for r in rows:
            r["signature_count"] = len(r.pop("signatures") or [])
    return rows


@router.post("/field_logs", status_code=201)
def create_field_log(body: FieldLogIn, request: Request, p: Principal = Depends(anyone)):
    _check_project(request, p.tenant_id, body.project_id)
    try:
        row, created = _store(request).insert_field_log_once(p.tenant_id, _field_row(body, p))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return row | {"duplicate": not created}


@router.get("/field_logs/{log_id}")
def get_field_log(log_id: int, request: Request, p: Principal = Depends(anyone)):
    row = _store(request).get("field_logs", p.tenant_id, log_id)
    if not row:
        _not_found("Field log")
    return row


@router.delete("/field_logs/{log_id}")
def delete_field_log(log_id: int, request: Request, p: Principal = Depends(office_up)):
    if not _store(request).delete("field_logs", p.tenant_id, log_id):
        _not_found("Field log")
    return {"ok": True}


@router.post("/field_logs/sync")
def sync_field_logs(body: SyncIn, request: Request, p: Principal = Depends(anyone)):
    """Offline queue upload. Each log carries a client_uuid; sending the same log twice never duplicates it."""
    store = _store(request)
    results = []
    for raw in body.logs:
        uuid = raw.get("client_uuid") if isinstance(raw, dict) else None
        try:
            log = FieldLogIn.model_validate(raw)
            if not log.client_uuid:
                raise ValueError("client_uuid is required for sync")
            if log.project_id is not None and not store.get("projects", p.tenant_id, log.project_id):
                raise ValueError("project not found")
            row, created = store.insert_field_log_once(p.tenant_id, _field_row(log, p))
            results.append({"client_uuid": log.client_uuid, "status": "created" if created else "duplicate",
                            "id": row["id"]})
        except (ValidationError, ValueError) as e:   # reported per log; the rest of the batch still syncs
            msg = e.errors()[0].get("msg") if isinstance(e, ValidationError) else str(e)
            results.append({"client_uuid": uuid, "status": "error", "error": str(msg)[:300]})
        except Exception:
            logger.exception("field log sync failed")
            results.append({"client_uuid": uuid, "status": "error",
                            "error": "Could not save this log right now. It stays on this device; sync again later."})
    return {"results": results, "created": sum(r["status"] == "created" for r in results),
            "duplicates": sum(r["status"] == "duplicate" for r in results),
            "errors": sum(r["status"] == "error" for r in results)}


# ---------------------------------------------------------------------------- dashboard + calendar
@router.get("/dashboard")
async def get_dashboard(request: Request, p: Principal = Depends(anyone)):
    platform = _p(request)
    tenant = await anyio.to_thread.run_sync(require_entitled, platform, p)
    data = await anyio.to_thread.run_sync(dashboard.build, platform.store, tenant)
    data["entitlement"] = entitlement(tenant)
    return data


def _ics_gen(tenant: dict) -> int:
    try:
        return int((tenant.get("settings") or {}).get("ics_gen", 0))
    except (TypeError, ValueError):
        return 0


def feed_token(platform, tenant: dict) -> str:
    return platform.signer.mac("ics", f"{tenant['id']}:{_ics_gen(tenant)}")[:40]


@router.get("/calendar/feed")
def calendar_feed(request: Request, p: Principal = Depends(office_up)):
    platform = _p(request)
    tenant = platform.store.get_tenant(p.tenant_id)
    return {"url": f"{base_url(request)}/api/calendar.ics?tenant={tenant['id']}&token={feed_token(platform, tenant)}"}


@router.post("/calendar/feed/rotate")
def rotate_feed(request: Request, p: Principal = Depends(office_up)):
    platform = _p(request)
    tenant = platform.store.modify_tenant_settings(     # locked: a settings save at the same moment can't undo it
        p.tenant_id, lambda current: {**current, "ics_gen": _ics_gen({"settings": current}) + 1})
    return {"url": f"{base_url(request)}/api/calendar.ics?tenant={tenant['id']}&token={feed_token(platform, tenant)}"}


@router.get("/calendar.ics")
async def calendar_ics(request: Request, tenant: Optional[str] = None, token: Optional[str] = None):
    """iCalendar feed of the tenant's deadlines. Works with the session cookie, an API key, or the private feed
    link from Settings (calendar apps can't send cookies)."""
    platform = _p(request)
    if tenant and token:
        t = await anyio.to_thread.run_sync(platform.store.get_tenant, tenant)
        if not t or not hmac.compare_digest(feed_token(platform, t), token):
            raise HTTPException(404, "Calendar not found")
        tid = t["id"]
    else:
        p = await get_principal(request)
        tid = p.tenant_id
    t = await anyio.to_thread.run_sync(platform.store.get_tenant, tid)
    if not entitlement(t)["ok"]:
        raise HTTPException(402, "Subscription required")
    data = await anyio.to_thread.run_sync(lambda: dashboard.build(platform.store, t, include_past=True))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = dashboard.to_ics(data["items"], t["company_name"], stamp)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="tradecomply.ics"'})
