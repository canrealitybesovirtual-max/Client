"""Billing: Stripe Checkout (subscription), billing portal and webhook, over Stripe's REST API with httpx.

Enabled only when STRIPE_SECRET_KEY and STRIPE_PRICE_ID are set (the price is the $1,000/month Core plan created in
the Stripe dashboard). The webhook needs STRIPE_WEBHOOK_SECRET and verifies the Stripe-Signature header exactly as
Stripe documents it: `t=<unix time>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">`, 5-minute tolerance, constant-time
compare against every v1 entry.

Access rules (entitlement):
  active                      -> full access
  past_due                    -> access continues while Stripe retries the card; the app shows a banner
  trialing, trial not ended   -> full access
  trialing past trial_ends    -> 402 on tool runs, dashboard and calendar; the app shows "Subscribe"
  canceled                    -> same as an ended trial
Data the customer entered (projects, subs, documents) stays readable and exportable either way.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import Principal, get_principal, is_https, owner_only, platform_of

log = logging.getLogger("tradecomply.billing")
router = APIRouter(prefix="/api/billing", tags=["billing"])
TOLERANCE_S = 300
PRICE_USD_MONTH = 1000

NOT_CONFIGURED = ("Online billing is not set up on this server yet (STRIPE_SECRET_KEY and STRIPE_PRICE_ID are not "
                  "set). Contact TradeComply to subscribe.")


# ---------------------------------------------------------------------------- entitlement
def entitlement(tenant: dict | None, now: datetime | None = None) -> dict:
    if not tenant:
        return {"ok": False, "status": "unknown", "reason": "no_account", "message": "Account not found"}
    now = now or datetime.now(timezone.utc)
    status = tenant.get("status")
    if tenant.get("plan") == "local":
        return {"ok": True, "status": status, "reason": "local", "message": "Local install"}
    if status == "active":
        return {"ok": True, "status": status, "reason": "active", "message": "Subscription active"}
    if status == "past_due":
        return {"ok": True, "status": status, "reason": "past_due", "warning": True,
                "message": "Your last payment failed. Update your card in Billing to avoid losing access."}
    if status == "trialing":
        try:
            ends = datetime.fromisoformat(tenant.get("trial_ends") or "")
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=timezone.utc)
        except ValueError:
            ends = now
        left = (ends - now).total_seconds()
        if left > 0:
            days = int(left // 86400) + (1 if left % 86400 else 0)
            return {"ok": True, "status": status, "reason": "trial", "trial_days_left": days,
                    "trial_ends": tenant.get("trial_ends"), "message": f"Free trial: {days} day{'' if days == 1 else 's'} left"}
        return {"ok": False, "status": status, "reason": "trial_expired", "trial_ends": tenant.get("trial_ends"),
                "message": "Your free trial has ended. Subscribe to keep running checks."}
    return {"ok": False, "status": status, "reason": "canceled",
            "message": "Your subscription is canceled. Subscribe to keep running checks."}


def require_entitled(platform, principal: Principal) -> dict:
    tenant = platform.store.get_tenant(principal.tenant_id)
    ent = entitlement(tenant)
    if not ent["ok"]:
        raise HTTPException(402, {"message": ent["message"], "reason": ent["reason"], "subscribe": True})
    return tenant


# ---------------------------------------------------------------------------- signature
class SignatureError(ValueError):
    pass


def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header (used by tests and local webhook replays)."""
    t = int(timestamp if timestamp is not None else time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def verify_signature(payload: bytes, header: str | None, secret: str, tolerance: int = TOLERANCE_S,
                     now: float | None = None) -> None:
    if not header:
        raise SignatureError("missing Stripe-Signature header")
    t, v1 = None, []
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            v1.append(v)
    if t is None or not t.isdigit() or not v1:
        raise SignatureError("malformed Stripe-Signature header")
    expected = hmac.new(secret.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in v1):
        raise SignatureError("signature does not match")
    if abs((now if now is not None else time.time()) - int(t)) > tolerance:
        raise SignatureError("timestamp outside the tolerance window")


# ---------------------------------------------------------------------------- events
STATUS_MAP = {"active": "active", "trialing": "active", "past_due": "past_due", "unpaid": "past_due",
              "canceled": "canceled", "incomplete_expired": "canceled", "paused": "past_due"}


def _invoice_subscription(obj: dict) -> str | None:
    sub = obj.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if not sub:      # newer API versions: invoice.parent.subscription_details.subscription
        sub = (((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
    return sub


def _event_created(event: dict) -> int:
    try:
        return int(event.get("created") or 0)
    except (TypeError, ValueError):
        return 0


def _set_status(store, tenant: dict, created: int, **fields) -> None:
    """Stripe doesn't deliver events in order (and retries old ones for days), so a status change is applied only if
    its event is not older than the last one applied to this tenant. Without this a late "subscription.updated:
    active" could undo a cancellation."""
    if "status" in fields:
        if created and created < int(tenant.get("stripe_event_created") or 0):
            fields.pop("status")
        elif created:
            fields["stripe_event_created"] = created
    fields = {k: v for k, v in fields.items() if v is not None and v != ""}
    if fields:
        store.update_tenant(tenant["id"], **fields)


def apply_event(store, event: dict) -> str | None:
    """Update the tenant a Stripe event refers to. Returns the tenant id changed, if any."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    meta_tid = (obj.get("metadata") or {}).get("tenant_id")
    created = _event_created(event)

    if etype == "checkout.session.completed":
        tid = obj.get("client_reference_id") or meta_tid
        tenant = store.get_tenant(tid) if tid else None
        if not tenant or obj.get("mode") not in (None, "subscription"):
            return None
        fields = {"stripe_customer_id": obj.get("customer"), "stripe_subscription_id": obj.get("subscription")}
        if obj.get("payment_status", "paid") in ("paid", "no_payment_required"):
            fields["status"] = "active"
        _set_status(store, tenant, created, **fields)
        return tenant["id"]

    if etype in ("customer.subscription.updated", "customer.subscription.deleted", "customer.subscription.created"):
        tenant = (store.get_tenant(meta_tid) if meta_tid else None) or store.tenant_by_stripe(
            customer_id=obj.get("customer"), subscription_id=obj.get("id"))
        if not tenant:
            return None
        status = "canceled" if etype.endswith("deleted") else STATUS_MAP.get(obj.get("status", ""))
        current = tenant.get("stripe_subscription_id")
        if current and obj.get("id") and obj.get("id") != current and status != "active":
            # An old subscription of the same customer ending (or failing) says nothing about the current one.
            return None
        _set_status(store, tenant, created, stripe_subscription_id=obj.get("id"),
                    stripe_customer_id=obj.get("customer"), **({"status": status} if status else {}))
        return tenant["id"]

    if etype in ("invoice.payment_failed", "invoice.paid", "invoice.payment_succeeded"):
        sub_id = _invoice_subscription(obj)
        tenant = store.tenant_by_stripe(customer_id=obj.get("customer"), subscription_id=sub_id)
        if not tenant:
            return None
        current = tenant.get("stripe_subscription_id")
        if current and sub_id and sub_id != current:
            return None
        if etype == "invoice.payment_failed":
            if tenant["status"] in ("active", "past_due"):
                _set_status(store, tenant, created, status="past_due")
        elif tenant["status"] == "past_due":
            _set_status(store, tenant, created, status="active")
        return tenant["id"]
    return None


# ---------------------------------------------------------------------------- Stripe REST
async def _stripe(platform, path: str, form: dict) -> dict:
    s = platform.settings
    try:
        async with httpx.AsyncClient(base_url=s.stripe_api_base, auth=(s.stripe_secret_key, ""), timeout=20) as c:
            r = await c.post(path, data=form)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach Stripe ({type(e).__name__}). Try again.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        log.warning("Stripe %s returned %s", path, r.status_code)
        raise HTTPException(502, f"Stripe error: {msg or r.status_code}")
    return r.json()


def base_url(request: Request) -> str:
    s = platform_of(request).settings
    if s.app_url:
        return s.app_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    return f"{'https' if is_https(request) else 'http'}://{host}"


@router.get("/status")
async def status(request: Request, p: Principal = Depends(get_principal)):
    platform = platform_of(request)
    tenant = platform.store.get_tenant(p.tenant_id)
    return {"configured": platform.settings.stripe_enabled, "status": tenant["status"], "plan": tenant["plan"],
            "price_usd_month": PRICE_USD_MONTH, "has_customer": bool(tenant.get("stripe_customer_id")),
            "entitlement": entitlement(tenant)}


@router.post("/checkout")
async def checkout(request: Request, p: Principal = Depends(owner_only)):
    platform = platform_of(request)
    s = platform.settings
    if not s.stripe_enabled:
        raise HTTPException(503, NOT_CONFIGURED)
    tenant = platform.store.get_tenant(p.tenant_id)
    base = base_url(request)
    form = {
        "mode": "subscription",
        "line_items[0][price]": s.stripe_price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": tenant["id"],
        "success_url": f"{base}/#/settings?billing=success",
        "cancel_url": f"{base}/#/settings?billing=canceled",
        "metadata[tenant_id]": tenant["id"],
        "subscription_data[metadata][tenant_id]": tenant["id"],
        "allow_promotion_codes": "true",
    }
    if tenant.get("stripe_customer_id"):
        form["customer"] = tenant["stripe_customer_id"]
    elif p.email:
        form["customer_email"] = p.email
    session = await _stripe(platform, "/v1/checkout/sessions", form)
    return {"url": session.get("url"), "id": session.get("id")}


@router.post("/portal")
async def portal(request: Request, p: Principal = Depends(owner_only)):
    platform = platform_of(request)
    if not platform.settings.stripe_enabled:
        raise HTTPException(503, NOT_CONFIGURED)
    tenant = platform.store.get_tenant(p.tenant_id)
    if not tenant.get("stripe_customer_id"):
        raise HTTPException(409, "No billing account yet. Subscribe first.")
    session = await _stripe(platform, "/v1/billing_portal/sessions",
                            {"customer": tenant["stripe_customer_id"], "return_url": f"{base_url(request)}/#/settings"})
    return {"url": session.get("url")}


@router.post("/webhook")
async def webhook(request: Request):
    platform = platform_of(request)
    secret = platform.settings.stripe_webhook_secret
    if not secret:
        raise HTTPException(503, "Stripe webhook is not configured (STRIPE_WEBHOOK_SECRET is not set).")
    payload = await request.body()
    try:
        verify_signature(payload, request.headers.get("stripe-signature"), secret)
    except SignatureError as e:
        raise HTTPException(400, f"Invalid Stripe signature: {e}")
    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    event_id = str(event.get("id") or "")
    if event_id and await anyio.to_thread.run_sync(platform.store.stripe_event_seen, event_id, event.get("type")):
        return {"received": True, "duplicate": True}
    try:
        tenant_id = await anyio.to_thread.run_sync(apply_event, platform.store, event)
    except Exception:
        log.exception("Stripe event %s failed", event.get("type"))
        if event_id:
            platform.store.forget_stripe_event(event_id)       # let Stripe's retry process it again
        raise HTTPException(500, "Event processing failed")
    return {"received": True, "tenant_updated": bool(tenant_id)}
