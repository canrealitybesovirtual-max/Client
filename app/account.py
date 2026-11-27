"""Account data rights: download all of a company's data, and close (delete) the company account.

  GET    /api/tenant/export   One JSON file with every record the company has on this server (see SECTIONS),
                              streamed. Owner only, signed in with a session (not an API key); at most
                              EXPORT_LIMIT downloads per EXPORT_WINDOW_S per company; each download is written to
                              the activity log first, so the file includes its own log entry. Refused when the
                              browser says another site started the request (Sec-Fetch-Site cross-site/same-site).
  DELETE /api/tenant          Close the company account. Owner only, session + CSRF (like every cookie-authenticated
                              write). Body {"confirm_company_name": ..., "password": ...}: the name must match and the
                              password is checked again (wrong passwords count toward the same lockout as a password
                              change, and each try is counted before it is checked, so tries sent all at once can't
                              get past it). A second deletion of the same company while one runs gets 409. Then, in
                              this order:
                                1. the card subscription is canceled at Stripe, if this server has STRIPE_SECRET_KEY
                                   and the company has a subscription (best effort: a Stripe outage never blocks the
                                   deletion; the result goes in the receipt and, when it failed, in the server log
                                   with the subscription id so the operator can cancel it by hand);
                                2. each connected integration's access is revoked at the provider where the connector
                                   supports it (Connector.revoke(), best effort, reported per integration);
                                3. every row of the company is deleted from every company table in one transaction
                                   (Store.delete_tenant: the activity log and the MCP call log too, with or without
                                   ON DELETE CASCADE), which also ends every session and API key of the company;
                                4. this session's cookies are cleared.
                              Returns a short receipt (what was removed, billing, integrations, what is kept elsewhere).
  python -m app.account delete --tenant <id> [--yes]     Operator CLI: the same deletion for a company (without --yes
                              it only prints what would be removed).
  python -m app.account export --tenant <id> [--out FILE]  Operator CLI: the same export file.

Why not API keys: keys act with the office role and their scope model (docs/mcp-security.md) limits which tools a
key may call. It has no notion of account-level actions, so "export everything" or "delete the company" can't be
granted to a key cleanly. Both routes need the owner's own session.

Nothing here logs a password, a cookie, a token or a company's records. Log lines name the company id only.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import connectors as registry
from .auth import SESSION_COOKIE, Principal, RateLimiter, clear_session, get_principal, platform_of, verify_password
from .connectors import Connector, ConnectorContext
from .store import TENANT_EXPORT_OMITTED, now_iso

log = logging.getLogger("tradecomply.account")
router = APIRouter(prefix="/api/tenant", tags=["account"])

FORMAT = "tradecomply.company-export"
FORMAT_VERSION = 1
EXPORT_LIMIT, EXPORT_WINDOW_S = 5, 10 * 60          # downloads per company per window
PAGE = 500
PAGE_BY_TABLE = {"field_logs": 20, "documents": 100}   # rows with signatures or long text: smaller pages in memory
REVOKE_BUDGET_S = 45                                    # stop calling providers after this; the rest are reported
STRIPE_TIMEOUT_S = 20
SUBSCRIPTION_ID_RE = re.compile(r"^[A-Za-z0-9_]{3,255}$")

# (key in the export file, table, what it holds). Every company table except the profile ("tenants" = "company").
SECTIONS = (
    ("team_members", "users", "People who can sign in: email, name, role, when they were added and last signed in. "
                              "No passwords."),
    ("api_keys", "api_keys", "API keys: name, first characters, tool scope, rate limit, expiry and whether revoked. "
                             "Not the keys themselves."),
    ("projects", "projects", "Projects: address, owner, lender, contract price, dates and status."),
    ("subcontractors", "subs", "Subcontractors: license, scope, insurance certificate expiry, workers' comp, W-9, "
                               "DIR number and contact details."),
    ("documents", "documents", "Saved documents and tool results."),
    ("field_logs", "field_logs", "Toolbox talks, inspections and incident reports, with attendees and their "
                                 "on-screen signatures (PNG images)."),
    ("activity_log", "audit", "Who ran which check or changed what, and when."),
    ("ai_assistant_calls", "mcp_audit_log", "Calls from AI assistants (MCP): tool, result, time taken, API key or "
                                            "connected app, and IP address. The arguments were never stored, only "
                                            "their SHA-256."),
    ("integrations", "integration_connections", "Connected products, their status and non-secret settings. Not the "
                                                "sign-ins or keys."),
    ("webhooks", "webhooks", "Webhooks (Slack, Teams, other): name, events and last result. Not the addresses or "
                             "signing secrets."),
    ("notifications", "outbound_events", "Alerts queued for or sent to webhooks and integrations, with their "
                                         "status."),
    ("single_sign_on", "sso_configs", "Single sign-on settings: issuer, client id and allowed email domains. Not the "
                                      "client secret."),
    ("calendar_links", "calendar_tokens", "When each private calendar link was made and whether it was turned off. "
                                          "Not the links."),
    ("connected_apps", "oauth_grants", "Apps someone connected with \"Sign in with TradeComply\" (for example "
                                       "Claude): the app, who approved it, where it returns to, when it was made and "
                                       "last used, and whether it was disconnected."),
    ("connected_app_tokens", "oauth_tokens", "When each sign-in token of a connected app was issued, used and when "
                                             "it expires. Not the tokens."),
    ("one_time_links", "auth_links", "Invite, password reset and email confirmation links that are still open: who "
                                     "each is for, where it was emailed, who made it and when it expires. Not the "
                                     "links."),
    ("ask_tradecomply_chats", "assistant_conversations", "Chats with Ask TradeComply, the in-app AI helper: whose chat "
                                                         "it is, its title, and when it was started and last used."),
    ("ask_tradecomply_messages", "assistant_turns", "Each question asked in those chats, the answer shown, which "
                                                    "checks it ran and when. Not the AI model's raw messages."),
    ("ask_tradecomply_usage", "assistant_usage", "AI helper usage per message: who asked, the model, and the input, "
                                                 "output and cache tokens counted."),
)
FRIENDLY = {table: key for key, table, _ in SECTIONS} | {"tenants": "company"}

ABOUT = {
    "what": "Every record your company has in TradeComply on this server, as of the time above.",
    "left_out": "Passwords, password and API key hashes, session counters and stored credentials (integration "
                "sign-ins and keys, webhook addresses and signing secrets, the SSO client secret, private calendar "
                "links, connected apps' token hashes, one-time link hashes). They only protect your account on this server and are of no "
                "use anywhere else.",
    "keep_it_safe": "This file can include names, emails, phone numbers, IP addresses and signatures of your staff, "
                    "subcontractors and customers. Store it somewhere safe and share it only with people who need it.",
    "format": "JSON. Dates and times are ISO 8601 in UTC. Each list is oldest first.",
}


class AccountDeletionFailed(RuntimeError):
    """The database deletion failed (nothing was deleted: it is one transaction). `billing` is what step 1 did."""

    def __init__(self, billing: dict):
        super().__init__("account deletion failed")
        self.billing = billing


# ---------------------------------------------------------------------------- helpers
_limiter_lock = threading.Lock()
_closing: set[str] = set()          # companies being deleted right now in this process (a double click, two tabs)
_closing_lock = threading.Lock()


def _begin_closing(tenant_id: str) -> bool:
    """False if this company's deletion is already running here: a second one would cancel the subscription and
    contact every integration again, then report "nothing to cancel" as a failure."""
    with _closing_lock:
        if tenant_id in _closing:
            return False
        _closing.add(tenant_id)
        return True


def _end_closing(tenant_id: str) -> None:
    with _closing_lock:
        _closing.discard(tenant_id)


def _limiter(platform, name: str, limit: int, window_s: int) -> RateLimiter:
    """A rate limiter kept on the platform (one per app), created on first use."""
    with _limiter_lock:
        lim = getattr(platform, name, None)
        if lim is None:
            lim = RateLimiter(limit=limit, window_s=window_s)
            setattr(platform, name, lim)
        return lim


async def owner_session(p: Principal = Depends(get_principal)) -> Principal:
    """The account owner, signed in with a session cookie. get_principal has already checked the CSRF token on
    writes."""
    if p.via != "cookie":
        raise HTTPException(403, "Sign in to TradeComply with your own account to do this. An API key can't.")
    if p.role != "owner":
        raise HTTPException(403, f"Only the account owner can do this. Your role is {p.role}.")
    return p


def _same_name(typed: str, actual: str) -> bool:
    """Exact match, ignoring spaces at the ends, repeated spaces and Unicode composition (é typed two ways)."""
    def norm(s: str) -> str:
        return unicodedata.normalize("NFC", " ".join(str(s or "").split()))
    return bool(norm(actual)) and norm(typed) == norm(actual)


def support_phrase(settings) -> str:
    """How to reach whoever runs this server (SUPPORT_EMAIL / SUPPORT_URL), as the start of a sentence."""
    c = settings.support_contact or {}
    if c.get("email"):
        return f"Contact support at {c['email']}"
    if c.get("url"):
        return f"Contact support at {c['url']}"
    return "Ask whoever runs this TradeComply server"


# ---------------------------------------------------------------------------- export
def export_chunks(store, tenant_id: str, company: dict, exported_by: str | None):
    """The export file as text chunks (a generator: StreamingResponse runs it on a worker thread, so the database
    reads never block the event loop). Pages through each table by id, so memory stays small."""
    def dump(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    left_out = {FRIENDLY[t]: {c: why for c, why in cols.items() if c != "tenant_id"}
                for t, cols in TENANT_EXPORT_OMITTED.items()}
    head = {"format": FORMAT, "format_version": FORMAT_VERSION, "generated": now_iso(), "company_id": tenant_id,
            "exported_by": exported_by,
            "about": {**ABOUT, "sections": {key: what for key, _, what in SECTIONS},
                      "fields_left_out": {k: v for k, v in left_out.items() if v}},
            "company": company}
    yield dump(head)[:-1]
    counts = {}
    for key, table, _ in SECTIONS:
        yield f',\n"{key}":['
        after, n, size = 0, 0, PAGE_BY_TABLE.get(table, PAGE)
        while True:
            rows = store.export_rows(table, tenant_id, after_id=after, limit=size)
            for r in rows:
                yield ("," if n else "") + "\n" + dump(r)
                n += 1
            if len(rows) < size or "id" not in (rows[-1] if rows else {}):
                break
            after = rows[-1]["id"]
        counts[key] = n
        yield "]"
    yield f',\n"row_counts":{dump(counts)},\n"complete":true}}\n'


@router.get("/export")
async def export_company_data(request: Request, p: Principal = Depends(owner_session)):
    """Download every record of this company as one JSON file (owner only, session only; rate limited; logged)."""
    # A GET carries the session cookie (SameSite=Lax) when another site navigates the browser here. The file never
    # reaches that site, but each visit would use up one of the owner's downloads, write an activity-log entry and
    # leave a copy of the company's records in the Downloads folder. The app fetches it from its own page.
    if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
        raise HTTPException(403, "Download your company data from Settings > Account and data in TradeComply.")
    platform = platform_of(request)
    store = platform.store
    lim = _limiter(platform, "account_export_limiter", EXPORT_LIMIT, EXPORT_WINDOW_S)
    wait = lim.retry_after(p.tenant_id)
    if wait:
        raise HTTPException(429, f"Too many downloads in a short time. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    lim.hit(p.tenant_id)

    def start():
        rows = store.export_rows("tenants", p.tenant_id)
        if rows:
            store.audit(p.tenant_id, p.label, "tenant.export", {"format": FORMAT, "version": FORMAT_VERSION}, None,
                        source="api")
        return rows
    rows = await anyio.to_thread.run_sync(start)
    if not rows:
        raise HTTPException(404, "Company not found")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(export_chunks(store, p.tenant_id, rows[0], p.label), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="tradecomply-company-data-{stamp}'
                                                             '.json"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- closing the account
async def cancel_billing_subscription(platform, tenant: dict) -> dict:
    """Step 1: cancel the company's Stripe subscription now (DELETE /v1/subscriptions/{id} on Stripe's REST API;
    unconfirmed against docs.stripe.com, which this module's author could not read). Never raises: the result is
    reported."""
    s = platform.settings
    tid, sub = tenant.get("id"), tenant.get("stripe_subscription_id")
    ask = support_phrase(s)
    if not sub:
        return {"result": "no_subscription", "message": "There was no card subscription to cancel."}
    if not s.stripe_secret_key:
        log.warning("company %s was deleted with Stripe subscription %s, but STRIPE_SECRET_KEY is not set on this "
                    "server: cancel the subscription in the Stripe dashboard", tid, sub)
        return {"result": "not_set_up", "message": "Online billing isn't set up on this server, so your card "
                f"subscription could not be canceled from here. {ask} so you aren't charged again."}
    reason = None
    if not SUBSCRIPTION_ID_RE.match(str(sub)):
        reason = "bad_subscription_id"
    else:
        try:
            async with httpx.AsyncClient(base_url=s.stripe_api_base, auth=(s.stripe_secret_key, ""),
                                         timeout=STRIPE_TIMEOUT_S) as c:
                r = await c.delete(f"/v1/subscriptions/{sub}")
        except Exception as e:      # noqa: BLE001 - httpx.HTTPError, or InvalidURL from a bad STRIPE_API_BASE
            reason = f"stripe_unreachable ({type(e).__name__})"
        else:
            if r.status_code < 400:
                return {"result": "canceled", "message": "Your card subscription was canceled at Stripe. Past "
                                                         "invoices and receipts stay in Stripe's records."}
            reason = f"stripe_http_{r.status_code}"
    if tenant.get("status") == "canceled":           # Stripe had already told this server it ended
        return {"result": "already_canceled", "message": "Your card subscription had already been canceled."}
    log.warning("company %s was deleted but its Stripe subscription %s was not canceled (%s): cancel it in the "
                "Stripe dashboard", tid, sub, reason)
    return {"result": "failed", "reason": reason.split(" ")[0],
            "message": f"Your card subscription could not be canceled automatically. {ask} and ask them to cancel "
                       "it, so you aren't charged again."}


def _revocable(cls) -> bool:
    return cls.revoke is not Connector.revoke or bool(cls.oauth and cls.oauth.revoke_url)


def revoke_integration_access(platform, tenant_id: str) -> list[dict]:
    """Step 2 (blocking: run it on a worker thread): ask each provider to cancel TradeComply's access, for every
    connection that holds credentials, where the connector supports it. Never raises; one result per integration."""
    out, started = [], time.monotonic()
    for conn in platform.store.list_connections(tenant_id):
        if not conn.get("has_secret"):
            continue
        provider = conn["provider"]
        cls = registry.get(provider)
        name = cls.display_name if cls else provider
        item = {"provider": provider, "name": name}
        remove = f"To be thorough, also remove TradeComply's access in your {name} account."
        if cls is None or not _revocable(cls):
            out.append({**item, "result": "not_supported",
                        "message": f"TradeComply deleted its copy of the {name} sign-in or key. {remove}"})
            continue
        if time.monotonic() - started > REVOKE_BUDGET_S:
            out.append({**item, "result": "skipped",
                        "message": f"There wasn't time to contact {name}. TradeComply deleted its copy of the "
                                   f"sign-in. {remove}"})
            continue
        try:
            cls(ConnectorContext(platform, tenant_id, None, "account deletion")).revoke()
        except Exception as e:       # noqa: BLE001 - best effort; the local credentials are deleted either way
            log.warning("company %s deletion: revoking %s access failed (%s)", tenant_id, provider, type(e).__name__)
            out.append({**item, "result": "failed",
                        "message": f"{name} could not be reached to cancel TradeComply's access. TradeComply "
                                   f"deleted its copy of the sign-in. {remove}"})
        else:
            out.append({**item, "result": "revoke_requested",
                        "message": f"TradeComply asked {name} to cancel its access and deleted its copy of the "
                                   "sign-in."})
    return out


async def close_account(platform, tenant: dict, *, actor: str, cancel_billing: bool = True,
                        revoke: bool = True) -> dict:
    """Steps 1-3 for one company (the route and the CLI). Raises AccountDeletionFailed if the database deletion
    fails (then nothing was deleted). Returns the receipt."""
    tid = tenant["id"]
    billing = (await cancel_billing_subscription(platform, tenant) if cancel_billing else
               {"result": "skipped", "message": "The card subscription was left as it is."})
    integrations = await anyio.to_thread.run_sync(revoke_integration_access, platform, tid) if revoke else []
    try:
        removed = await anyio.to_thread.run_sync(platform.store.delete_tenant, tid)
    except Exception as e:          # noqa: BLE001 - reported to the caller; one transaction, so nothing was deleted
        log.error("company %s deletion failed in the database (%s); nothing was deleted", tid, type(e).__name__)
        raise AccountDeletionFailed(billing) from e
    log.info("company %s deleted by %s: %d rows removed; billing %s; %d integration(s) revoked", tid, actor,
             sum(removed.values()), billing["result"],
             sum(1 for i in integrations if i["result"] == "revoke_requested"))
    kept = ["Copies outside TradeComply are not affected: files you downloaded or printed, calendars you "
            "subscribed to, and messages already sent to Slack, Teams or by text."]
    if removed.get("webhooks"):
        kept.append("Webhook addresses you made in Slack, Teams or another product still exist there until you "
                    "remove them; TradeComply deleted its copy.")
    if tenant.get("stripe_customer_id"):
        kept.append("Stripe keeps its own records of your past invoices and payments.")
    kept.append("Database backups and server logs kept by whoever runs this server are not changed by this. They "
                "expire on that server's own schedule.")
    return {"ok": True, "deleted": True, "company_id": tid, "company_name": tenant.get("company_name"),
            "deleted_at": now_iso(), "removed": {FRIENDLY[t]: n for t, n in removed.items() if n},
            "billing": billing, "integrations": integrations, "signed_out": True, "kept_elsewhere": kept,
            "support_contact": platform.settings.support_contact,
            "message": "Your company account and its records were deleted from TradeComply. Everyone on your team "
                       "is signed out and every API key, connected app and calendar link stopped working."}


class CloseAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_company_name: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=256)


@router.delete("")
async def close_company_account(body: CloseAccountIn, request: Request, p: Principal = Depends(owner_session)):
    """Permanently delete this company and everything in it (owner only; confirm the company name and password)."""
    platform = platform_of(request)
    store = platform.store
    key = f"pw|{p.user_id}"             # the same lockout as a password change: 5 wrong passwords per 15 minutes
    wait = platform.login_limiter.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    tenant, user = await anyio.to_thread.run_sync(lambda: (store.get_tenant(p.tenant_id), store.user_by_id(p.user_id)))
    if not tenant or not user or user["tenant_id"] != p.tenant_id:
        raise HTTPException(401, "Please log in")
    if not _same_name(body.confirm_company_name, tenant["company_name"]):
        raise HTTPException(422, "The company name doesn't match. Type it exactly as it is shown in Settings. "
                                 "Nothing was deleted.")
    # Counted before the (slow) check and taken back when it is right, so requests sent all at once can't each
    # get a guess in before the first wrong one is counted (RateLimiter.claim).
    wait = platform.login_limiter.claim(key)
    if wait:
        raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    if not await anyio.to_thread.run_sync(verify_password, body.password, user["pw_hash"]):
        raise HTTPException(403, "Your password is incorrect. Nothing was deleted.")
    platform.login_limiter.reset(key)
    if not _begin_closing(p.tenant_id):
        raise HTTPException(409, "This company account is already being deleted. Wait a minute, then reload the "
                                 "page.")
    try:
        receipt = await close_account(platform, tenant, actor=f"its owner (user {p.user_id})")
    except AccountDeletionFailed as e:
        extra = (" Your card subscription was already canceled at Stripe." if e.billing["result"] == "canceled"
                 else "")
        raise HTTPException(503, "Deleting the account failed, so nothing was deleted. Try again in a few minutes."
                                 + extra)
    finally:
        _end_closing(p.tenant_id)
    payload = platform.signer.unsign("session", request.cookies.get(SESSION_COOKIE))
    if payload and payload.get("n"):    # its user is gone, so the cookie is dead anyway; record the nonce as well
        await anyio.to_thread.run_sync(store.revoke_session, str(payload["n"]), int(payload.get("e", 0)))
    resp = JSONResponse(receipt)
    clear_session(resp)
    return resp


# ---------------------------------------------------------------------------- operator CLI
USAGE = """usage: python -m app.account delete --tenant <company id> [--yes] [--keep-billing] [--no-revoke]
       python -m app.account export --tenant <company id> [--out FILE]

delete   Permanently delete a company and all its records (the same as Settings > Account and data > Delete
         company account). Without --yes it only prints what would be removed. It also cancels the company's
         Stripe subscription (skip with --keep-billing) and asks connected integrations to cancel TradeComply's
         access (skip with --no-revoke).
export   Write the company's data export (the same file as Settings > Download all company data) to FILE, or to
         standard output.
Uses the same DATABASE_URL / DATA_DIR, SECRET_KEY and INTEGRATIONS_KEY as the server."""


def main(argv=None, platform=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m app.account", usage=USAGE, add_help=True)
    ap.add_argument("command", choices=("delete", "export"))
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--keep-billing", action="store_true")
    ap.add_argument("--no-revoke", action="store_true")
    ap.add_argument("--out")
    try:
        args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return int(e.code or 0)
    own = platform is None
    if own:
        from .config import Platform, Settings
        platform = Platform(Settings.from_env(), None)
    store = platform.store
    try:
        tid = args.tenant.strip()
        counts = store.tenant_row_counts(tid)
        if not any(counts.values()):
            print(f"No company with id {tid!r} on this server (storage: {store.describe()}).", file=sys.stderr)
            return 1
        tenant = store.get_tenant(tid) or {"id": tid}      # no company row: rows left by an interrupted deletion
        if args.command == "export":
            if not counts["tenants"]:
                print(f"No company with id {tid!r} (only leftover rows; run delete to remove them).", file=sys.stderr)
                return 1
            company = store.export_rows("tenants", tid)[0]
            store.audit(tid, "operator (CLI)", "tenant.export", {"format": FORMAT, "version": FORMAT_VERSION}, None,
                        source="cli")
            if args.out:
                import os
                fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for chunk in export_chunks(store, tid, company, "operator (CLI)"):
                        f.write(chunk)
                print(f"Wrote {args.out}. It holds the company's records: keep it safe.", file=sys.stderr)
            else:
                for chunk in export_chunks(store, tid, company, "operator (CLI)"):
                    sys.stdout.write(chunk)
            return 0
        plan = {"company_id": tid, "company_name": tenant.get("company_name"),
                "rows": {FRIENDLY[t]: n for t, n in counts.items() if n}}
        if not args.yes:
            print(json.dumps({"would_delete": plan}, indent=2))
            print("Nothing was deleted. Add --yes to delete this company permanently.", file=sys.stderr)
            return 2
        try:
            receipt = anyio.run(lambda: close_account(platform, tenant, actor="the operator (CLI)",
                                                      cancel_billing=not args.keep_billing,
                                                      revoke=not args.no_revoke))
        except AccountDeletionFailed as e:
            print(f"Deleting failed in the database; nothing was deleted (billing: {e.billing['result']}). See the "
                  "log above.", file=sys.stderr)
            return 1
        print(json.dumps(receipt, indent=2))
        return 0
    finally:
        if own:
            store.close()


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
