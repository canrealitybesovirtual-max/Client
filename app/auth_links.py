"""One-time links by email: password reset, team invites and email confirmation (docs/email-links.md).

- Tokens: "tcl_" + 256 random bits. Only the SHA-256 is stored (auth_links), each link works once, and it is bound to
  the person's email and session counter when it was made, so a password change, a reset or an email change ends
  every outstanding link (app/store.py use_auth_link).
- The token travels in the URL fragment (#/reset/<token>), which browsers never send to a server or in Referer.
  Opening a link changes nothing: it is used only by a POST after the person acts (types a new password, presses
  Confirm), so mail scanners that open links can't use them up.
- Emailed links are built from the configured https APP_URL only, never from the request's Host or
  X-Forwarded-Host. No https APP_URL or no mail backend (app/mailer.py): nothing is emailed. Owners still get a
  copyable invite or reset link to send themselves.
- Forgot password answers the same way, at the same speed, whether or not the address has an account: every lookup
  and the email happen after the response. Reset links are emailed only to confirmed addresses, and confirming an
  address needs that person's own session, so a stranger at a mistyped signup address can't take the account over.
- Companies that require SSO: no password links for anyone but owners (the same break-glass rule as login). The
  forgot-password email points them to SSO instead.
- Email is never used to sign in (no "magic link"): NIST SP 800-63B-4 section 3.1.3.1. Resets, invites and
  confirmations are allowed there.
"""
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import sso
from .auth import (EMAIL_RE, MIN_PASSWORD, Principal, client_ip, get_principal, hash_key, hash_password, ip_key,
                   owner_only, platform_of, set_session, verify_password)
from .mailer import valid_recipient
from .store import IntegrityError

log = logging.getLogger("tradecomply.links")

TOKEN_PREFIX = "tcl_"
TOKEN_RE = re.compile(r"^tcl_[A-Za-z0-9_-]{43}$")
RESET_TTL_S = 30 * 60                 # emailed reset link (the person is waiting for it)
OWNER_RESET_TTL_S = 24 * 3600         # a reset link an owner hands over (NIST: 24 h at most for email recovery)
INVITE_TTL_S = 7 * 86400
CONFIRM_TTL_S = 24 * 3600             # NIST 800-63B-4 4.2.1.2: a confirmation code is a recovery code: 24 h
PATHS = {"reset": "/#/reset/", "invite": "/#/invite/", "confirm": "/#/confirm-email/"}
GONE = "This link has expired or was already used. Ask for a new one."
CONFIRM_GONE = ("This link has expired, was already used or is for another TradeComply account. Sign in as the "
                "account it was sent for, or send a new link from Settings.")
FORGOT_SENT = ("If that email has a TradeComply account with a confirmed address, we sent a link to reset the "
               "password. It works once, for 30 minutes. No email? Check spam, or ask your company's owner for a "
               "reset link.")
FORGOT_OFF = ("Email isn't set up on this server, so we can't send reset links. Ask your company's owner to make "
              "you a reset link in Settings > Team.")
MAIL_OFF = "Email isn't set up on this server."


def owner_help(platform) -> str:
    """For the company's owner, who has no owner to ask: whoever runs this server (SUPPORT_EMAIL / SUPPORT_URL)."""
    sc = platform.settings.support_contact or {}
    where = sc.get("email") or sc.get("url")
    return (f"If you are the owner, contact support at {where}." if where else
            "If you are the owner, ask whoever runs this TradeComply server.")


def forgot_sent(platform) -> str:
    return f"{FORGOT_SENT} {owner_help(platform)}"


def forgot_off(platform) -> str:
    return f"{FORGOT_OFF} {owner_help(platform)}"

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------- helpers
def link_base(settings) -> str | None:
    """Where emailed links point: the configured APP_URL, only when it is https. Never the request's own address."""
    url = (settings.app_url or "").strip()
    try:
        p = urlsplit(url)
        ok = p.scheme == "https" and p.hostname and not (p.username or p.password or p.query or p.fragment)
    except ValueError:
        ok = False
    return url.rstrip("/") if ok else None


def enabled(platform) -> bool:
    """Can this server email links? A mail backend and an https APP_URL."""
    return link_base(platform.settings) is not None and platform.mailer.configured()


def new_token() -> tuple[str, str]:
    plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_key(plain)


def _token_hash(token: str) -> str | None:
    return hash_key(token) if isinstance(token, str) and TOKEN_RE.match(token) else None


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).replace(microsecond=0).isoformat()


def issue(platform, tenant_id: str, user_id: int, purpose: str, *, ttl_s: int, email_to: str | None = None,
          copy: bool = False, created_by: int | None = None, keep: int = 0) -> dict | None:
    """Make an emailed link (email_to, only when sending is set up) and/or a copy link for an owner (copy), stored
    together. Blocking (store). None if the person isn't in this company or there was nothing to make."""
    base = link_base(platform.settings)
    emailed = new_token() if email_to and enabled(platform) else None
    copied = new_token() if copy else None
    tokens = ([(emailed[1], email_to)] if emailed else []) + ([(copied[1], None)] if copied else [])
    if not tokens:
        return None
    expires = platform.store.create_auth_links(tenant_id, user_id, purpose, tokens, ttl_s, created_by=created_by,
                                               keep=keep)
    if expires is None:
        return None
    path = PATHS[purpose]
    return {"expires": expires, "expires_at": _iso(expires),
            "email_url": base + path + emailed[0] if emailed else None,
            "copy_path": path + copied[0] if copied else None,
            "copy_url": base + path + copied[0] if copied and base else None}


_LINKISH = re.compile(r"\S*(?:://|www\.|@)\S*|\S*[\w-]\.[^\W\d_]{2,}\b\S*", re.IGNORECASE)  # a domain: any script


def _clean(value, limit: int = 80) -> str:
    """A name or company name, which its owner typed, for an email we send from TradeComply's own address: one line,
    at most `limit` characters, no control or invisible characters (bidi overrides could reorder what is shown), and
    no web or email address (an invite must never carry a link we didn't make)."""
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = _LINKISH.sub("[link removed]", " ".join(text.split()))
    return text[:limit]


def _help_line(platform) -> str:
    sc = platform.settings.support_contact or {}
    where = sc.get("email") or sc.get("url")
    return (f"Questions? Contact {where}." if where else "Questions? Ask your company's owner.")


def _footer(platform) -> str:
    return f"\n\n{_help_line(platform)}\n\nTradeComply CA. You got this email because of a sign-in on your account."


def reset_email(platform, email: str, url: str) -> tuple[str, str]:
    return ("Reset your TradeComply password",
            f"Someone asked to reset the password for {email} on TradeComply.\n\n"
            f"Set a new password here. The link works once, for 30 minutes:\n{url}\n\n"
            "Didn't ask? Ignore this email. Your password stays the same." + _footer(platform))


def sso_email(platform, email: str, company: str, base: str) -> tuple[str, str]:
    return ("Signing in to TradeComply",
            f"Someone asked to reset the TradeComply password for {email}.\n\n"
            "Your company signs in with single sign-on (SSO), so there is no TradeComply password to reset. "
            f"Open {base}/#/login, enter your email and choose Sign in with SSO.\n\n"
            "Didn't ask? Ignore this email." + _footer(platform))


def invite_email(platform, inviter: str, company: str, url: str | None, base: str) -> tuple[str, str]:
    """The subject is fixed and the names the company typed are labeled as theirs: anyone can start a trial, so
    nothing they type may read as TradeComply's own words."""
    how = (f"Set your password and sign in. The link works once, for 7 days:\n{url}" if url else
           f"Your company signs in with single sign-on (SSO): open {base}/#/login, enter this email address and "
           "choose Sign in with SSO.")
    return ("You're invited to join your team on TradeComply",
            "You were added to a company on TradeComply, the California construction compliance app.\n\n"
            f"Company name, as its owner entered it: {_clean(company) or '(none)'}\n"
            f"Added by: {_clean(inviter) or '(no name)'}\n\n{how}\n\n"
            "Not expecting this or don't know them? Ignore this email. TradeComply never asks you to pay, call a "
            "number or sign in anywhere else from an invite." + _footer(platform))


def confirm_email(platform, address: str, url: str) -> tuple[str, str]:
    return ("Confirm your email for TradeComply",
            f"Confirm {address} as the sign-in email for your TradeComply account. Then you can reset your "
            f"password by email if you ever forget it.\n\nThe link works once, for 24 hours. Sign in first if "
            f"asked:\n{url}\n\nDidn't ask? Ignore this email." + _footer(platform))


def email_changing_email(platform, old: str, new: str, base: str) -> tuple[str, str]:
    return ("Your TradeComply sign-in email is changing",
            f"Someone signed in as {old} asked to change the TradeComply sign-in email to {new}. It changes once "
            f"the new address is confirmed.\n\nIf this wasn't you, reset your password at {base}/#/forgot right "
            "away (that also cancels the change) and tell your company's owner." + _footer(platform))


def password_changed_email(platform, email: str, base: str) -> tuple[str, str]:
    return ("Your TradeComply password was changed",
            f"The password for {email} on TradeComply was just changed. You were signed out on every other "
            "device, apps you connected were disconnected, and API keys you made were turned off.\n\nIf this "
            f"wasn't you, reset it at {base}/#/forgot and tell your company's owner." + _footer(platform))


def _send(platform, to: str, message: tuple[str, str]) -> bool:
    """Blocking. For BackgroundTasks (a sync function runs in the thread pool after the response)."""
    return platform.mailer.send(to, *message)


def notify_password_changed(platform, user: dict) -> None:
    """Background: tell a confirmed address that its password changed (NIST 800-63B-4 4.6)."""
    base = link_base(platform.settings)
    if base and user.get("email_confirmed_at"):
        _send(platform, user["email"], password_changed_email(platform, user["email"], base))


def send_confirmation(platform, user: dict, address: str | None = None) -> bool:
    """Blocking: email a confirmation link for `address` (default: the person's current email)."""
    address = (address or user["email"]).strip().lower()
    if not enabled(platform):
        return False
    # keep=1: the link sent a moment ago (at sign-up, say) keeps working when the person asks for another
    made = issue(platform, user["tenant_id"], user["id"], "confirm", ttl_s=CONFIRM_TTL_S, email_to=address, keep=1)
    return bool(made and made["email_url"]) and _send(platform, address,
                                                      confirm_email(platform, address, made["email_url"]))


def after_signup(platform, user: dict) -> None:
    """Background, after a new company's owner signs up (or someone joins with a copied invite link): the
    confirmation email, when sending is set up."""
    try:
        send_confirmation(platform, user)
    except Exception as e:      # noqa: BLE001 - background work: logged, never raised
        log.warning("signup confirmation email failed (%s)", type(e).__name__)


def _limit(limiter, key: str, message: str):
    wait = limiter.claim(key)
    if wait:
        raise HTTPException(429, f"{message} Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})


def _link_limit(request: Request) -> str:
    """Count a link check from this network (ip_key: a forged X-Forwarded-For never lands in someone else's bucket);
    the caller hands the attempt back with _link_ok when the link was good, so only failed checks add up and a
    valid link from a busy shared network still works. Returns the key."""
    key = ip_key(request)
    _limit(platform_of(request).link_limiter, key, "Too many link checks from this network.")
    return key


def _link_ok(request: Request, key: str) -> None:
    platform_of(request).link_limiter.release(key)


async def _peek(request: Request, token: str, purpose: str) -> tuple[str, dict]:
    key = _link_limit(request)
    h = _token_hash(token)
    link = await anyio.to_thread.run_sync(platform_of(request).store.auth_link, h, purpose) if h else None
    if not link:
        raise HTTPException(404, GONE)
    _link_ok(request, key)
    return h, link


def _sso_refusal(platform, user: dict) -> str | None:
    return sso.password_refused(platform, {"tenant_id": user["tenant_id"], "role": user["role"]})


# ---------------------------------------------------------------------------- owner: invites and reset links
async def invite(request: Request, p: Principal, user: dict, action: str) -> dict:
    """Make (or replace) a person's invite: a copy link for the owner always, and an email when sending is set up.
    In a company that requires SSO a non-owner gets no link: they sign in with SSO (the email says so)."""
    platform = platform_of(request)
    store = platform.store
    tenant = await anyio.to_thread.run_sync(store.get_tenant, p.tenant_id)
    owner = await anyio.to_thread.run_sync(store.user_by_id, p.user_id) if p.user_id else None
    inviter = (owner or {}).get("name") or p.email or "Your company's owner"
    sso_only = await anyio.to_thread.run_sync(_sso_refusal, platform, user) is not None
    can_email = enabled(platform)
    over = can_email and bool(platform.mail_tenant_limiter.claim(f"tenant|{p.tenant_id}"))
    made, sent = None, False
    if not sso_only:
        made = await anyio.to_thread.run_sync(lambda: issue(
            platform, p.tenant_id, user["id"], "invite", ttl_s=INVITE_TTL_S, copy=True, created_by=p.user_id,
            email_to=user["email"] if can_email and not over else None))
        if made is None:
            raise HTTPException(404, "User not found")
    if can_email and not over and (sso_only or made["email_url"]):
        msg = invite_email(platform, inviter, tenant["company_name"], made and made["email_url"],
                           link_base(platform.settings))
        sent = await anyio.to_thread.run_sync(_send, platform, user["email"], msg)
    if sso_only:
        message = ("Invite emailed. " if sent else "") + \
            "Your company signs in with SSO, so they sign in with their company account. No link needed."
    elif sent:
        message = "Invite emailed. You can also send them this link yourself."
    elif over:
        message = "Your company sent a lot of email this hour, so this one wasn't emailed. Send them this link yourself."
    elif can_email:
        message = "We couldn't email the invite. Send them this link yourself."
    else:
        message = "Email isn't set up on this server, so send them this link yourself (text or email)."
    await anyio.to_thread.run_sync(lambda: store.audit(p.tenant_id, p.label, action, {
        "user_id": user["id"], "role": user["role"], "emailed": sent}, None))
    return {"id": user["id"], "email": user["email"], "role": user["role"], "name": user.get("name"),
            "invite_path": made and made["copy_path"], "invite_url": made and made["copy_url"],
            "expires_at": made and made["expires_at"], "email_sent": sent, "sso": sso_only, "message": message}


async def owner_reset_link(request: Request, p: Principal, user: dict) -> dict:
    platform = platform_of(request)
    refusal = await anyio.to_thread.run_sync(_sso_refusal, platform, user)
    if refusal:
        raise HTTPException(403, "Your company requires single sign-on (SSO), so they sign in with their company "
                                 "account. No password reset is needed.")
    made = await anyio.to_thread.run_sync(lambda: issue(
        platform, p.tenant_id, user["id"], "reset", ttl_s=OWNER_RESET_TTL_S, copy=True, created_by=p.user_id,
        keep=2))
    if made is None:
        raise HTTPException(404, "User not found")
    await anyio.to_thread.run_sync(lambda: platform.store.audit(p.tenant_id, p.label, "user.reset_link",
                                                                {"user_id": user["id"]}, None))
    return {"email": user["email"], "reset_path": made["copy_path"], "reset_url": made["copy_url"],
            "expires_at": made["expires_at"]}


# ---------------------------------------------------------------------------- routes
class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


class PeekIn(BaseModel):
    token: str = Field(max_length=100)
    purpose: Literal["reset", "invite", "confirm"]


class ResetIn(BaseModel):
    token: str = Field(max_length=100)
    new_password: str = Field(min_length=MIN_PASSWORD, max_length=256)


class AcceptIn(BaseModel):
    token: str = Field(max_length=100)
    password: str = Field(min_length=MIN_PASSWORD, max_length=256)
    name: str | None = Field(None, max_length=200)


class ConfirmIn(BaseModel):
    token: str = Field(max_length=100)


class EmailChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_email: str = Field(max_length=254)


def _forgot_task(platform, email: str) -> None:
    """After the response: the account lookup and the email, so the reply is the same whatever the address. The
    server-wide cap counts only emails that really go out (to a confirmed account), so requests for made-up addresses
    can't use it up; over it, nothing is sent and the reply stays the same."""
    try:
        user = platform.store.user_by_email(email)
        if not user or not user.get("email_confirmed_at") or platform.forgot_global_limiter.claim("all"):
            return
        base = link_base(platform.settings)
        if sso.password_refused(platform, user):
            tenant = platform.store.get_tenant(user["tenant_id"]) or {}
            _send(platform, user["email"], sso_email(platform, user["email"], tenant.get("company_name"), base))
            return
        made = issue(platform, user["tenant_id"], user["id"], "reset", ttl_s=RESET_TTL_S, email_to=user["email"],
                     keep=2)
        if made and made["email_url"]:
            _send(platform, user["email"], reset_email(platform, user["email"], made["email_url"]))
    except Exception as e:      # noqa: BLE001 - background work: logged without details, never raised
        log.warning("forgot-password email failed (%s)", type(e).__name__)


@router.post("/forgot")
async def forgot(body: ForgotIn, request: Request, background: BackgroundTasks):
    """Email a reset link. The reply never says whether the address has an account (OWASP)."""
    platform = platform_of(request)
    _limit(platform.forgot_ip_limiter, ip_key(request), "Too many reset requests from this network.")
    if not enabled(platform):
        return {"ok": True, "emailed": False, "message": forgot_off(platform)}
    email = body.email.strip().lower()
    if EMAIL_RE.match(email) and not platform.forgot_email_limiter.claim(email):
        background.add_task(_forgot_task, platform, email)
    return {"ok": True, "emailed": True, "message": forgot_sent(platform)}


@router.post("/link")
async def peek_link(body: PeekIn, request: Request):
    """What a link is for, without using it: the page shows it before the person acts."""
    platform = platform_of(request)
    _, link = await _peek(request, body.token, body.purpose)
    sso_only = body.purpose != "confirm" and \
        await anyio.to_thread.run_sync(_sso_refusal, platform, link) is not None
    return {"purpose": body.purpose, "email": link["sent_to"] if body.purpose == "confirm" else link["email"],
            "company_name": link["company_name"], "expires_at": _iso(link["expires"]), "sso": sso_only,
            "name": link["name"] if body.purpose == "invite" else None}


@router.post("/reset")
async def reset_password(body: ResetIn, request: Request, background: BackgroundTasks):
    """Set a new password with a reset link. Every session of that person ends, their connected apps and other
    links too; nobody is signed in by this (OWASP: log in again with the new password)."""
    platform = platform_of(request)
    h, link = await _peek(request, body.token, "reset")      # an unknown link costs no password hashing
    refusal = await anyio.to_thread.run_sync(_sso_refusal, platform, link)
    if refusal:
        raise HTTPException(403, refusal)
    pw_hash = await anyio.to_thread.run_sync(hash_password, body.new_password)
    user = await anyio.to_thread.run_sync(lambda: platform.store.use_auth_link(h, "reset", pw_hash=pw_hash))
    if not user:
        raise HTTPException(404, GONE)
    platform.email_limiter.reset(f"email|{user['email']}")           # a reset works around a login lockout
    platform.login_limiter.reset(f"{client_ip(request)}|{user['email']}")
    background.add_task(notify_password_changed, platform, user)
    return {"ok": True, "email": user["email"], "api_keys_revoked": user["api_keys_revoked"]}


@router.post("/invite/accept")
async def accept_invite(body: AcceptIn, request: Request, background: BackgroundTasks):
    """Join a company with an invite link: set a password and sign in."""
    platform = platform_of(request)
    h, link = await _peek(request, body.token, "invite")
    refusal = await anyio.to_thread.run_sync(_sso_refusal, platform, link)
    if refusal:
        raise HTTPException(403, refusal)
    pw_hash = await anyio.to_thread.run_sync(hash_password, body.password)
    name = " ".join((body.name or "").split()) or None
    user = await anyio.to_thread.run_sync(lambda: platform.store.use_auth_link(h, "invite", pw_hash=pw_hash,
                                                                               name=name))
    if not user:
        raise HTTPException(404, GONE)
    if not user["email_confirmed_at"]:      # joined by a link the owner copied (texted): confirm, as sign-up does
        background.add_task(after_signup, platform, user)
    resp = JSONResponse({"ok": True, "email": user["email"]}, background=background)
    set_session(resp, request, user)
    return resp


def _person(p: Principal) -> None:
    if p.via != "cookie":
        raise HTTPException(403, "Sign in to TradeComply to do this; an API key can't.")


@router.post("/email/confirm")
async def confirm_address(body: ConfirmIn, request: Request, p: Principal = Depends(get_principal)):
    """Confirm an address with the link emailed to it, signed in as the person it was sent for."""
    _person(p)
    platform = platform_of(request)
    key = _link_limit(request)
    h = _token_hash(body.token)
    try:
        user = await anyio.to_thread.run_sync(lambda: platform.store.use_auth_link(
            h, "confirm", user_id=p.user_id)) if h else None
    except IntegrityError:
        raise HTTPException(409, "That email already has an account.")
    if not user:
        raise HTTPException(404, CONFIRM_GONE)
    _link_ok(request, key)
    resp = JSONResponse({"ok": True, "email": user["email"], "changed": bool(user["previous_email"])})
    if user["previous_email"]:
        set_session(resp, request, user)     # the email is part of the session binding: keep this device signed in
    return resp


@router.post("/email/resend")
async def resend_confirmation(request: Request, p: Principal = Depends(get_principal)):
    _person(p)
    platform = platform_of(request)
    if not enabled(platform):
        raise HTTPException(503, MAIL_OFF)
    user = await anyio.to_thread.run_sync(platform.store.user_by_id, p.user_id)
    if user["email_confirmed_at"]:
        return {"ok": True, "sent": False, "confirmed": True}
    _limit(platform.mail_user_limiter, f"user|{p.user_id}", "Too many emails sent to you.")
    sent = await anyio.to_thread.run_sync(send_confirmation, platform, user)
    return {"ok": True, "sent": sent, "confirmed": False}


@router.post("/email/change")
async def change_email(body: EmailChangeIn, request: Request, background: BackgroundTasks,
                       p: Principal = Depends(get_principal)):
    """Change your sign-in email: your current password, then a link to the new address (it changes when you
    confirm it there) and a notice to the old one."""
    _person(p)
    platform = platform_of(request)
    if not enabled(platform):
        raise HTTPException(503, MAIL_OFF + " Ask whoever runs it to set it up.")
    new = body.new_email.strip().lower()
    if not EMAIL_RE.match(new) or not valid_recipient(new):
        raise HTTPException(422, "Enter a valid email address")
    user = await anyio.to_thread.run_sync(platform.store.user_by_id, p.user_id)
    if new == user["email"]:
        raise HTTPException(422, "That is already your email.")
    if await anyio.to_thread.run_sync(_sso_refusal, platform, user):
        raise HTTPException(403, "Your company signs in with single sign-on (SSO), so your email comes from your "
                                 "company account.")
    key = f"pw|{p.user_id}"
    _limit(platform.login_limiter, key, "Too many wrong passwords.")
    if not await anyio.to_thread.run_sync(verify_password, body.current_password, user["pw_hash"]):
        raise HTTPException(403, "Current password is incorrect")
    platform.login_limiter.reset(key)
    if await anyio.to_thread.run_sync(platform.store.user_by_email, new):
        raise HTTPException(409, "That email already has an account.")
    _limit(platform.mail_user_limiter, f"user|{p.user_id}", "Too many emails sent to you.")
    sent = await anyio.to_thread.run_sync(send_confirmation, platform, user, new)
    if sent and user["email_confirmed_at"]:
        background.add_task(_send, platform, user["email"],
                            email_changing_email(platform, user["email"], new, link_base(platform.settings)))
    return {"ok": True, "sent": sent, "sent_to": new}


@router.get("/email-status")
def email_status(request: Request, p: Principal = Depends(owner_only)):
    """Settings > Email (owners): is sending set up, and which environment variables to set. Names, never values."""
    platform = platform_of(request)
    st = platform.mailer.status()
    links = link_base(platform.settings) is not None
    return {**st, "links": links, "enabled": st["configured"] and links,
            "missing": st["missing"] + ([] if links else ["APP_URL"])}


@router.post("/email-test")
async def email_test(request: Request, p: Principal = Depends(owner_only)):
    """Send a test email to the owner's own address."""
    _person(p)
    platform = platform_of(request)
    if not platform.mailer.configured():
        raise HTTPException(503, MAIL_OFF)
    _limit(platform.integration_test_limiter, f"mail|{p.tenant_id}", "Too many test emails.")
    sent = await anyio.to_thread.run_sync(_send, platform, p.email, (
        "TradeComply test email", "This is a test from TradeComply. Email is set up: invites, password resets and "
                                  "email confirmations will be sent from this address." + _footer(platform)))
    return {"sent": sent, "to": p.email}
