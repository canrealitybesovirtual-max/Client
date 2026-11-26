"""Docusign eSignature: per-tenant OAuth sign-in (Confidential Authorization Code Grant with PKCE), account and base
URI discovery through /oauth/userinfo, sending a TradeComply home improvement contract (built by
tradecomply.contracts.generate_home_improvement_contract) to the contractor and the buyer(s) for signature, and
tracking each envelope by polling (at most every 20 minutes) and, when the tenant adds a Connect HMAC key, through an
envelope-level Connect webhook whose messages are HMAC-verified. Envelope ids are stored on the TradeComply project.

Sources (Docusign's official developer documentation, read on 2026-11-26 through Context7's index of
developers.docusign.com, because developers.docusign.com itself is not reachable from the build environment; field
names were cross-checked against the models of Docusign's official Python SDK, docusign-esign 8.0.0 from PyPI, which
is generated from the eSignature REST API v2.1 specification):

- platform/auth/reference/obtain-consent: GET {auth}/oauth/auth with response_type=code, scope (space-separated;
  `signature` for the eSignature API), client_id (the integration key), state (recommended against CSRF),
  redirect_uri (must exactly match one registered for the integration key); prompt is optional.
- platform/auth/authcode: Confidential ACG "can optionally include a Proof Key for Code Exchange (PKCE) challenge";
  access tokens last 8 hours by default. platform/auth/public-authcode-get-token: code_challenge,
  code_challenge_method (S256 is the only supported value) and code_verifier in the token request.
- platform/auth/reference/scopes: `extended` "Used when requesting an access token using Authorization Code Grant
  ... a new refresh token is issued with a full lifetime (typically 30 days)".
- platform/auth/reference/obtain-access-token and platform/auth/confidential-authcode-get-token: POST
  {auth}/oauth/token, Content-Type application/x-www-form-urlencoded, `Authorization: Basic base64(integration key +
  ":" + secret key)`, grant_type=authorization_code and code; the code is "only viable for two minutes"; response
  access_token, token_type (Bearer), refresh_token, expires_in, scope. Hosts: account-d.docusign.com (developer/demo)
  and account.docusign.com (production).
- platform/auth/reference/user-info and platform/resource-limits: GET {auth}/oauth/userinfo with the bearer token;
  response sub, name, email, accounts[] {account_id, is_default, account_name, base_uri}; cache it and "don't call
  /oauth/userinfo more often than once per authentication per user ... do not call it when a refresh token is used";
  hourly limits per user (25,000) and integration key (50,000); over the limit: HTTP 429 with error
  principal_throttled.
- esign-rest-api/reference/envelopes/envelopes/create and how-to/request-signature-email-remote: POST
  {base_uri}/restapi/v2.1/accounts/{accountId}/envelopes with emailSubject, documents, recipients and status "sent";
  answers 201 {envelopeId, uri, statusDateTime, status}. Documents may be HTML (fileExtension "html", documentBase64);
  "If the document is not a PDF it is converted to a PDF" (SDK Document model). Signer: email (max 100 characters),
  name, recipientId, routingOrder, tabs {signHereTabs, dateSignedTabs, initialHereTabs}.
- esign-rest-api/esign101/concepts/tabs/auto-place and reference/envelopes/enveloperecipienttabs: anchor tabs
  (anchorString, anchorUnits pixels, anchorXOffset, anchorYOffset, anchorIgnoreIfNotPresent "false" = error when the
  string is missing); a tab is created at every occurrence; use unique strings, e.g. in white font so they are
  invisible; the same anchored tab can't be used for different recipients; Sign Here and Initial Here tabs are placed
  from the bottom-left of the anchor string.
- esign-rest-api/reference/envelopes/envelopes/get: GET .../envelopes/{envelopeId} (status, sentDateTime,
  deliveredDateTime, completedDateTime, declinedDateTime, voidedDateTime, statusChangedDateTime).
- esign-rest-api/esign101/rules-and-limits: status polling "limited to once every 15 minutes" per envelope; "design
  your app to poll at 20-minute intervals" (violations can fail Go-Live review); 3,000 calls per hour per account by
  default; headers X-RateLimit-Limit / -Remaining / -Reset (Unix epoch) and X-BurstLimit-Limit / -Remaining (30-second
  burst); 429 Too Many Requests. Error bodies: {"errorCode", "message"} (esign101/error-codes).
- platform/webhooks/connect/create-webhook-listener: envelope-level Connect (the envelope's eventNotification) "is
  available to all Docusign plans"; json-sim-event-model: JSON SIM messages carry `event` (envelope-sent,
  envelope-delivered, envelope-completed, envelope-declined, envelope-voided, ...) and `data` {accountId, userId,
  envelopeId, envelopeSummary?}. SDK EventNotification model: url, requireAcknowledgment, includeHMAC,
  loggingEnabled, events, eventData {version, format, includeData}.
- platform/webhooks/connect/hmac, setting-up-hmac, validate: Connect keys are added under Admin > Connect > Connect
  Keys (the full key is shown only once); each message carries X-DocuSign-Signature-1..N, one per key (up to one
  hundred); compute HMAC-SHA256 over the entire raw body, base64-encode it, and accept the message when at least one
  header matches; remove any `"` characters from the secret first.

Operator setup: create an integration key in Docusign Apps and Keys with a secret key and "Authorization Code
Grant", register the redirect URI <APP_URL>/api/integrations/docusign/oauth/callback (set APP_URL so it is stable),
then set DOCUSIGN_INTEGRATION_KEY, DOCUSIGN_SECRET and DOCUSIGN_ENV ("demo" for a developer account, "production" after
Docusign's Go-Live review promotes the key). Nothing works until all three are set; the settings page then shows
"not configured on this server" instead of failing.

What the document contains (California, B&P 7159 home improvement contract): the generator's full contract, with
the Three-Day (or Five-Day senior, or Seven-Day disaster) Right to Cancel notice next to the buyer's signature and the
two detachable Notice of Cancellation copies. TradeComply refuses to send when the generator reports a problem, when a
[[...]] placeholder is still unfilled, or when that cancellation notice and both Notice of Cancellation copies are not
in the document; service and repair forms and contracts marked "negotiated at the contractor's place of business"
(which leave the notice out) are not sent through Docusign. The contractor signs first (routing order 1), then the
buyer(s) (routing order 2), so the envelope completes when the last buyer signs and every signer is emailed the signed
copy then. The buyer's cancellation period is recomputed from that completion date; when it differs from the date of
transaction printed in the Notice of Cancellation, the envelope record says so and tells the office to give the buyer
a corrected notice.

HTTP API (`router`; app/main.py must include it before the catch-all MCP mount at "/", see tests):
POST /api/integrations/docusign/preview, POST /api/integrations/docusign/envelopes,
GET /api/integrations/docusign/envelopes/{envelope_id}, GET /api/integrations/docusign/projects/{project_id}/envelopes,
and the public Connect listener POST /api/integrations/docusign/connect-listener/{tenant_id}/{tag}. The generic
/api/integrations/docusign/... routes (config, test, oauth/start, oauth/callback, disconnect) work as for every
connector.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import math
import re
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from . import (ConfigError, ConfigField, Connector, ConnectorContext, ConnectorError, NotConfigured, NotConnected,
               OAuth2Spec, TestResult)
from ..auth import Principal, RateLimiter, office_up, platform_of
from ..billing import base_url, require_entitled
from ..safe_http import SafeHTTPError, UpstreamError, retry_after_seconds

log = logging.getLogger("tradecomply.connectors.docusign")

ENVIRONMENTS = {"demo": "https://account-d.docusign.com",           # obtain-access-token: developer environment
                "production": "https://account.docusign.com"}       # ... and production
SCOPES = ("signature", "extended")
API_PATH = "/restapi/v2.1/accounts/"
POLL_INTERVAL_S = 20 * 60          # rules-and-limits: "design your app to poll at 20-minute intervals"
BURST_PAUSE_S = 30                 # X-BurstLimit-* count a 30-second burst
FORCE_REFRESH = 10 ** 9            # access_token(leeway=FORCE_REFRESH) refreshes even an unexpired token (after a 401)
EMAIL_SUBJECT_MAX = 100            # envelopes/create: subject lines over 100 characters are cut
EMAIL_BLURB_MAX = 10_000           # EnvelopeDefinition.emailBlurb: "a maximum of 10000 characters"
SIGNER_EMAIL_MAX = 100             # Signer.email: "Maximum length: 100 characters"
NAME_MAX = 100                     # TradeComply's own cap for signer names
MAX_BUYERS = 10                    # TradeComply's own cap
MAX_ACCOUNTS = 50
MAX_ENVELOPES_PER_PROJECT = 20     # project.extra stays small (the projects API caps extra at 50,000 characters)
MAX_DOCUSIGN_EXTRA = 30_000
MAX_PROJECTS_SCANNED = 5000
MAX_CONNECT_BODY = 1_000_000
MAX_SIGNATURE_HEADERS = 100        # hmac: one header per key, "up to one hundred"
MAX_FILL_IN = 20_000
CONNECT_EVENTS = ("envelope-sent", "envelope-delivered", "envelope-completed", "envelope-declined", "envelope-voided")
EVENT_STATUS = {"envelope-sent": "sent", "envelope-delivered": "delivered", "envelope-completed": "completed",
                "envelope-declined": "declined", "envelope-voided": "voided"}
FINAL_STATUSES = ("completed", "declined", "voided")
STATUS_RANK = {"created": 0, "sent": 1, "delivered": 2, "signed": 3, "completed": 4, "declined": 4, "voided": 4}
HIC_KINDS = ("three_day", "five_day_senior", "seven_day_disaster")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
TENANT_RE = re.compile(r"^t_[0-9a-f]{16}$")
TAG_RE = re.compile(r"^[0-9a-f]{64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
STATUS_RE = re.compile(r"^[a-z]{1,20}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PLACEHOLDER_RE = re.compile(r"\[\[[^\]]+\]\]")
TOKEN_RE = re.compile(r"\x02([sdi])(\d{1,2})\x03")
CONNECT_PURPOSE = "docusign-connect"

# Indirections so tests can move the clock.
_now = time.time

_pause: dict[str, float] = {}        # account id -> unix time until which Docusign calls wait (rate limit headers)
_pause_lock = threading.Lock()


# ---------------------------------------------------------------------------- errors
class DocusignRateLimited(ConnectorError):
    """Docusign's API limit for the account is used up; nothing was sent."""
    status_code = 429

    def __init__(self, wait_s: float):
        self.retry_after = max(1, int(math.ceil(wait_s)))
        when = time.strftime("%H:%M UTC", time.gmtime(_now() + self.retry_after))
        super().__init__(f"Docusign's API limit for this account is used up until about {when}. Nothing was sent; "
                         "try again after that.")


class NotFound(ConnectorError):
    status_code = 404


class ContractNotReady(ConfigError):
    """The contract can't be sent yet: generator problems or unfilled placeholders (422, with the lists)."""

    def __init__(self, message: str, problems: list[str] | None = None, unfilled: list[str] | None = None):
        super().__init__(message)
        self.problems, self.unfilled = list(problems or []), list(unfilled or [])


# ---------------------------------------------------------------------------- small helpers
def environment_name(env) -> str | None:
    """'demo' or 'production' from DOCUSIGN_ENV; None when unset or anything else (the operator must choose)."""
    value = (env.get("DOCUSIGN_ENV") or "").strip().lower()
    return value if value in ENVIRONMENTS else None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")


def _text(value, limit: int) -> str | None:
    """One line of text from Docusign or a request: whitespace runs and control characters become one space."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = _SPACE.sub(" ", str(value)).strip()
    return text[:limit].strip() or None


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"[:120]


def clean_base_uri(value) -> str | None:
    """The base_uri of a userinfo account as https://host, or None. Only docusign.net / docusign.com hosts are
    accepted (Docusign's examples: na3.docusign.net, eu.docusign.net, demo.docusign.net): the bearer token is sent
    there, so a strange host is refused rather than trusted."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        u = urlsplit(value.strip())
        port = u.port
    except ValueError:
        return None
    host = (u.hostname or "").lower().rstrip(".")
    if u.scheme != "https" or u.username or u.password or port not in (None, 443) or u.query or u.fragment:
        return None
    if u.path not in ("", "/") or not re.fullmatch(r"[a-z0-9.-]{1,253}", host):
        return None
    if not (host.endswith(".docusign.net") or host.endswith(".docusign.com")):
        return None
    return f"https://{host}"


_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def parse_time(value) -> datetime | None:
    """A Docusign date-time ("2023-04-06T19:38:30.6530000Z": up to 7 fraction digits) as an aware UTC datetime.
    Times without a zone are read as UTC."""
    if not isinstance(value, str):
        return None
    m = _DT_RE.match(value.strip())
    if not m:
        return None
    day, clock, frac, zone = m.groups()
    text = f"{day}T{clock}" + (f".{(frac + '000000')[:6]}" if frac else "")
    if zone and zone != "Z":
        text += zone if ":" in zone else f"{zone[:3]}:{zone[3:]}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _iso(datetime.fromtimestamp(_now(), timezone.utc))


def _ca_date(dt: datetime) -> date:
    from tradecomply import logic
    return dt.astimezone(logic.CA_TZ).date()


def _today_ca() -> date:
    return _ca_date(datetime.fromtimestamp(_now(), timezone.utc))


def _check_pause(account_id: str) -> None:
    with _pause_lock:
        until = _pause.get(account_id, 0.0)
    if until > _now():
        raise DocusignRateLimited(until - _now())


def _set_pause(account_id: str, wait_s: float) -> None:
    with _pause_lock:
        _pause[account_id] = max(_pause.get(account_id, 0.0), _now() + max(1.0, wait_s))


def connect_tag(platform, tenant_id: str) -> str:
    """The second path segment of a tenant's Connect listener URL: HMAC of the tenant id under the app key, so only
    URLs TradeComply made are accepted (the message itself is then checked with the tenant's Docusign HMAC key)."""
    return platform.signer.mac(CONNECT_PURPOSE, tenant_id)


def verify_connect_hmac(key: str, body: bytes, headers) -> bool:
    """hmac/validate: HMAC-SHA256 of the entire raw body with the Connect key (quotes removed), base64; one matching
    X-DocuSign-Signature-N header is enough."""
    if not key or not isinstance(body, (bytes, bytearray)):
        return False
    want = base64.b64encode(hmac.new(key.replace('"', "").encode("utf-8"), bytes(body),
                                     hashlib.sha256).digest()).decode("ascii")
    ok = False
    for n in range(1, MAX_SIGNATURE_HEADERS + 1):
        sent = headers.get(f"x-docusign-signature-{n}")
        if sent is not None and hmac.compare_digest(sent.strip().encode("ascii", "replace"), want.encode("ascii")):
            ok = True
    return ok


# ---------------------------------------------------------------------------- the contract document
H1 = "font-size:16pt;font-weight:bold;margin:14pt 0 6pt 0"
H2 = "font-size:13pt;font-weight:bold;margin:12pt 0 4pt 0"
P = "font-size:12pt;margin:0 0 8pt 0"
CELL = "border:1px solid #000000;padding:3pt 5pt;font-size:12pt;text-align:left;vertical-align:top"
BODY = "font-family:Arial,Helvetica,sans-serif;font-size:12pt;line-height:1.35;color:#000000;background:#ffffff"
ANCHOR = "color:#ffffff"
_PRINT_RE = re.compile(r"^<!-- PRINT: [^\n]*-->$")
_SEP_RE = re.compile(r"^\|[\s|:-]+\|$")


def _inline(text: str, anchors: dict[str, str]) -> str:
    out = html.escape(text, quote=True)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)

    def anchor(m):
        return f'<span style="{ANCHOR}">{anchors[m.group(1) + m.group(2)]}</span>'
    return TOKEN_RE.sub(anchor, out)


def render_html(markdown: str, anchors: dict[str, str], title: str) -> str:
    """The generator's Markdown (headings, bold, lists, tables, "---" breaks) as a self-contained HTML document with
    inline styles. Every piece of text is HTML-escaped; anchor tokens become white anchor strings. Type sizes meet the
    largest statutory minimum the contract has: 12-pt text (B&P 7159 statements are at least 10 or 12 pt), 16-pt
    headings (the 16 CCR 872 form heading is at least 14 pt). The generator's PRINT comments are dropped. Non-ASCII
    characters are written as character references so the converter can't misread the encoding."""
    parts = []
    for block in re.split(r"\n{2,}", markdown.strip()):
        b = block.strip("\n")
        if not b.strip():
            continue
        if b.strip() == "---":
            parts.append('<div style="page-break-before:always"></div>')
            continue
        if _PRINT_RE.match(b.strip()):
            continue
        if b.startswith("# "):
            parts.append(f'<h1 style="{H1}">{_inline(b[2:], anchors)}</h1>')
            continue
        if b.startswith("## "):
            parts.append(f'<h2 style="{H2}">{_inline(b[3:], anchors)}</h2>')
            continue
        lines = b.split("\n")
        if all(ln.startswith("|") and ln.rstrip().endswith("|") for ln in lines) and len(lines) >= 2:
            rows = [ln for ln in lines if not _SEP_RE.match(ln.strip())]
            out = ['<table style="border-collapse:collapse;margin:0 0 8pt 0">']
            for i, ln in enumerate(rows):
                cells = [c.strip() for c in ln.strip()[1:-1].split("|")]
                tag = "th" if i == 0 else "td"
                out.append("<tr>" + "".join(f'<{tag} style="{CELL}">{_inline(c, anchors)}</{tag}>' for c in cells)
                           + "</tr>")
            parts.append("".join(out) + "</table>")
            continue
        if all(ln.startswith("- ") for ln in lines):
            parts.append(f'<ul style="{P}">' + "".join(f"<li>{_inline(ln[2:], anchors)}</li>" for ln in lines)
                         + "</ul>")
            continue
        parts.append(f'<p style="{P}">' + "<br>".join(_inline(ln, anchors) for ln in lines) + "</p>")
    doc = (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>'
           f'<body style="{BODY}">' + "\n".join(parts) + "</body></html>")
    return doc.encode("ascii", "xmlcharrefreplace").decode("ascii")


def _clean(text: str) -> str:
    return CONTROL_RE.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " "))


@dataclass
class Signer:
    role: str                  # "contractor" | "buyer"
    index: int                 # 0 = contractor, 1.. = buyers in contract order
    name: str
    email: str
    initials: bool = False


@dataclass
class PreparedContract:
    contract: object                       # tradecomply.contracts.GeneratedContract
    markdown: str                          # filled contract text (no anchors): saved as the TradeComply document
    html: str                              # the document sent to Docusign
    anchors: dict[str, str]                # "s1" -> "/tc....s1/"
    anchor_counts: dict[str, int]
    signers: list[Signer]
    signing_date: str
    title: str
    notes: list[str] = field(default_factory=list)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.html.encode("ascii")).hexdigest()


def _sign_line(name: str) -> str:
    return f"Buyer {name}: Signature ______________________  Date: ________"


def prepare_contract(contract: dict, buyer_emails: list, contractor_name, contractor_email,
                     fill_ins: dict | None = None, today: date | None = None) -> PreparedContract:
    """Generate the contract with tradecomply.contracts and turn it into the document Docusign gets. Pure (no I/O).
    Raises ConfigError (bad input) or ContractNotReady (problems / unfilled placeholders / missing notices)."""
    from tradecomply import contracts as C
    today = today or _today_ca()
    if not isinstance(contract, dict):
        raise ConfigError("contract must be the fields of generate_home_improvement_contract")
    data = dict(contract)
    if data.get("service_and_repair"):
        raise ConfigError("Service and repair forms aren't sent through Docusign. Send the home improvement contract "
                          "(service_and_repair false).", "service_and_repair")
    if data.get("negotiated_at_contractor_place_of_business"):
        raise ConfigError("Contracts sent through Docusign always include the right-to-cancel notice and the Notice "
                          "of Cancellation: set negotiated_at_contractor_place_of_business to false.",
                          "negotiated_at_contractor_place_of_business")
    data["service_and_repair"] = False
    data["negotiated_at_contractor_place_of_business"] = False
    signing = data.get("signing_date") or today.isoformat()
    try:
        signing_day = date.fromisoformat(str(signing))
    except ValueError:
        raise ConfigError("signing_date must be a date (YYYY-MM-DD)", "signing_date") from None
    if signing_day < today:
        raise ConfigError("signing_date (the date of the transaction printed in the Notice of Cancellation) can't be "
                          "before today when the contract is sent for signature now.", "signing_date")
    if signing_day > today + timedelta(days=30):
        raise ConfigError("signing_date is more than 30 days away; send the contract closer to the signing date.",
                          "signing_date")
    data["signing_date"] = signing_day.isoformat()
    try:
        fields = C.ContractFields.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(x) for x in err.get("loc", ())) or "contract"
        raise ConfigError(f"contract.{loc}: {err.get('msg', 'invalid')}"[:300], loc) from None

    names = [_text(n, NAME_MAX) or "" for n in fields.buyer_names]
    if len(names) > MAX_BUYERS:
        raise ConfigError(f"At most {MAX_BUYERS} buyers can sign one envelope", "buyer_names")
    if any(n != raw for n, raw in zip(names, fields.buyer_names)):
        raise ConfigError(f"Buyer names must be one line of at most {NAME_MAX} characters", "buyer_names")
    if len({n.casefold() for n in names}) != len(names):
        raise ConfigError("Each buyer needs a different name (each one gets their own signature lines)",
                          "buyer_names")
    if not isinstance(buyer_emails, list) or len(buyer_emails) != len(names):
        raise ConfigError("Give one email address for each buyer, in the same order as buyer_names", "buyer_emails")
    emails = []
    for e in buyer_emails:
        text = _text(e, SIGNER_EMAIL_MAX + 1) or ""
        if not EMAIL_RE.match(text) or len(text) > SIGNER_EMAIL_MAX:
            raise ConfigError("Each buyer email must be a valid address of at most 100 characters", "buyer_emails")
        emails.append(text)
    c_name = _text(contractor_name, NAME_MAX + 1) or ""
    c_email = _text(contractor_email, SIGNER_EMAIL_MAX + 1) or ""
    if not c_name or len(c_name) > NAME_MAX:
        raise ConfigError("Who signs for the contractor? Give contractor_signer_name (or save it in the Docusign "
                          "settings).", "contractor_signer_name")
    if not EMAIL_RE.match(c_email) or len(c_email) > SIGNER_EMAIL_MAX:
        raise ConfigError("Give a valid contractor_signer_email of at most 100 characters (or save it in the "
                          "Docusign settings).", "contractor_signer_email")
    if any(e.casefold() == c_email.casefold() for e in emails):
        raise ConfigError("A buyer's email can't be the contractor signer's email: each buyer signs from their own "
                          "inbox.", "buyer_emails")

    result = C.generate_home_improvement_contract(fields, as_of=today)
    cancel = result.cancellation
    if result.contract_type != "home_improvement" or not cancel.applies or cancel.kind not in HIC_KINDS:
        raise ContractNotReady("This contract doesn't include the right-to-cancel notice and the Notice of "
                               "Cancellation, so it can't be sent through Docusign.")
    if result.problems:
        raise ContractNotReady("Fix the contract's problems before sending it.", problems=result.problems)

    md = _clean(result.markdown)
    main, sep, rest = md.partition("\n\n---\n\n")
    if not sep:
        raise ContractNotReady("The contract has no attachments (the Notice of Cancellation copies are missing).")
    rest = sep + rest

    # -- signature, date and initial anchors, in the contract body only (never in the attachments: the Notice of
    #    Cancellation and the change order form are signed later, if ever)
    tok = lambda kind, idx: f"\x02{kind}{idx}\x03"      # noqa: E731
    for i, name in enumerate(names, 1):
        line = _sign_line(name)
        if main.count(line) < 2:          # the signature block and the right-to-cancel acknowledgment
            raise ContractNotReady("The contract's buyer signature lines weren't found where expected.")
        main = main.replace(line, f"Buyer {name}: Signature {tok('s', i)}______________________  "
                                  f"Date: {tok('d', i)}________")
    company = _clean(fields.contractor_name)
    c_line = f"{company}, by: ______________________  Date: ________"
    if main.count(c_line) < 1:
        raise ContractNotReady("The contract's contractor signature line wasn't found where expected.")
    main = main.replace(c_line, f"{company}, by: {tok('s', 0)}______________________  Date: {tok('d', 0)}________")
    first_page, n = re.subn(r"^\*\*Date buyer signed this contract:\*\* .*$",
                            f"**Date buyer signed this contract:** {tok('d', 1)}________________", main, count=1,
                            flags=re.M)
    if n != 1:
        raise ContractNotReady("The first-page 'Date buyer signed this contract' line wasn't found.")
    main = first_page
    initials = False
    arb = "Buyer's initials: ______    Contractor's initials: ______"
    if arb in main:
        initials = True
        main = main.replace(arb, "Buyer's initials: " + "  ".join(f"{names[i - 1]} {tok('i', i)}______"
                                                                   for i in range(1, len(names) + 1))
                            + f"    Contractor's initials: {tok('i', 0)}______")

    # -- the generator's [[...]] placeholders, filled with the office's text
    fill_ins = fill_ins or {}
    if not isinstance(fill_ins, dict):
        raise ConfigError("fill_ins must map each placeholder to its text", "fill_ins")
    known = set(result.placeholders)
    unknown = [k for k in fill_ins if k not in known]
    if unknown:
        raise ConfigError("fill_ins has text for a placeholder this contract doesn't have: "
                          f"{str(unknown[0])[:120]}", "fill_ins")
    for key, value in fill_ins.items():
        text = _clean(value if isinstance(value, str) else "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if not text or len(text) > MAX_FILL_IN or "[[" in text or "]]" in text:
            raise ConfigError(f"The text for {key[:80]} must be 1 to {MAX_FILL_IN} characters without [[ or ]]",
                              "fill_ins")
        main, rest = main.replace(key, text), rest.replace(key, text)
    unfilled = sorted(set(PLACEHOLDER_RE.findall(main + rest)))
    if unfilled:
        raise ContractNotReady("Fill in every [[...]] placeholder before sending (give the text in fill_ins).",
                               unfilled=unfilled)

    # -- the right-to-cancel notice and both Notice of Cancellation copies must be in what is sent
    notice = getattr(C, "_cancel_notice_text", None)
    lines = [x.strip() for x in (notice(cancel.kind) if notice else cancel.notice_title or "").split("\n")
             if x.strip()]
    plain_main = TOKEN_RE.sub("", main)
    title_at = plain_main.find(f"**{cancel.notice_title}**")
    ack_at = plain_main.find("Owner acknowledges receipt of this notice.", max(title_at, 0))
    if not lines or any(f"**{x}**" not in plain_main for x in lines) or title_at < 0 or ack_at < 0 \
            or main.find(tok("s", 1), ack_at) < 0:
        raise ContractNotReady(f"The {cancel.notice_title or 'right-to-cancel'} notice, signed by the buyer, isn't in "
                               "the contract, so it can't be sent.")
    if not all(f"# Attachment A-{i}: Notice of Cancellation" in rest for i in (1, 2)) or \
            rest.count("I hereby cancel this transaction.") < 2:
        raise ContractNotReady("The two Notice of Cancellation copies aren't attached, so the contract can't be sent.")

    nonce = _secrets.token_hex(4)
    anchors = {f"{k}{i}": f"/tc{nonce}{k}{i}/" for k in "sdi" for i in range(len(names) + 1)}
    title = "Home Improvement Contract"
    doc = render_html(main + rest, anchors, title)
    counts = {k: doc.count(a) for k, a in anchors.items()}
    expected = {k: (main.count(f"\x02{k[0]}{k[1:]}\x03")) for k in anchors}
    if counts != expected:            # a fill-in or contract field happened to contain an anchor string
        raise ContractNotReady("The document couldn't be prepared for signing; try again.")
    signers = [Signer("contractor", 0, c_name, c_email, initials)] + \
        [Signer("buyer", i, names[i - 1], emails[i - 1], initials) for i in range(1, len(names) + 1)]
    plain = TOKEN_RE.sub("", main + rest)
    notes = list(result.notes)
    if not contract.get("signing_date"):
        notes.append(f"signing_date was not given; the Notice of Cancellation uses today, {signing_day.isoformat()}.")
    return PreparedContract(contract=result, markdown=plain, html=doc, anchors=anchors, anchor_counts=counts,
                            signers=signers, signing_date=signing_day.isoformat(), title=title, notes=notes)


def _anchor_tab(anchor: str, label: str, y_offset: str = "0") -> dict:
    return {"anchorString": anchor, "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": y_offset,
            "anchorIgnoreIfNotPresent": "false", "tabLabel": label}


def envelope_definition(prep: PreparedContract, *, email_subject: str, email_blurb: str,
                        connect_url: str | None) -> dict:
    """envelopes/create body: one HTML document, the contractor (routing order 1) and the buyers (routing order 2)
    with signHere / dateSigned (and initialHere for the arbitration clause) anchor tabs, sent at once."""
    signers = []
    for s in prep.signers:
        label = "contractor" if s.role == "contractor" else f"buyer{s.index}"
        tabs = {"signHereTabs": [_anchor_tab(prep.anchors[f"s{s.index}"], f"{label}_signature")],
                "dateSignedTabs": [_anchor_tab(prep.anchors[f"d{s.index}"], f"{label}_date_signed")]}
        if s.initials and prep.anchor_counts.get(f"i{s.index}"):
            tabs["initialHereTabs"] = [_anchor_tab(prep.anchors[f"i{s.index}"], f"{label}_arbitration_initials")]
        signers.append({"email": s.email, "name": s.name, "recipientId": str(s.index + 1),
                        "routingOrder": "1" if s.role == "contractor" else "2", "tabs": tabs})
    body = {"emailSubject": email_subject[:EMAIL_SUBJECT_MAX], "emailBlurb": email_blurb[:EMAIL_BLURB_MAX],
            "documents": [{"documentBase64": base64.b64encode(prep.html.encode("ascii")).decode("ascii"),
                           "name": prep.title, "fileExtension": "html", "documentId": "1"}],
            "recipients": {"signers": signers}, "status": "sent"}
    if connect_url:
        body["eventNotification"] = {"url": connect_url, "requireAcknowledgment": "true", "loggingEnabled": "true",
                                     "includeHMAC": "true", "events": list(CONNECT_EVENTS),
                                     "eventData": {"version": "restv2.1", "format": "json"}}
    return body


# ---------------------------------------------------------------------------- envelope records (project.extra)
def _record_view(rec: dict, project_id: int | None = None) -> dict:
    out = {k: v for k, v in rec.items()}
    if project_id is not None:
        out["project_id"] = project_id
    if rec.get("status") not in FINAL_STATUSES and rec.get("checked_at"):
        last = parse_time(rec["checked_at"])
        if last:
            out["next_check_after"] = _iso(last + timedelta(seconds=POLL_INTERVAL_S))
    return out


def _merge_status(rec: dict, status: str | None, *, at: datetime | None, times: dict, source: str,
                  event: str | None = None) -> bool:
    """Apply a status (from a poll or a Connect message) to a record in place. A final status (completed, declined,
    voided) never goes back to an earlier one, so retried or out-of-order messages are harmless. Returns True when this
    change completed the envelope."""
    was_completed = rec.get("status") == "completed"
    stamp = _iso(at) if at else _now_iso()
    for key, value in times.items():
        dt = parse_time(value)
        if dt and not rec.get(key):
            rec[key] = _iso(dt)
    if event:
        rec["events"] = (rec.get("events") or [])[-9:] + [{"event": event, "at": stamp}]
    if status and STATUS_RE.match(status):
        current = rec.get("status") or "created"
        if current in FINAL_STATUSES and status != current:
            pass                                   # a final status never changes (late or retried messages)
        elif STATUS_RANK.get(status, 0) < STATUS_RANK.get(current, 0):
            pass                                   # never back to an earlier step (out-of-order messages)
        else:
            if status != current:
                rec["status"], rec["status_at"] = status, stamp
            rec["source"] = source
            if status == "completed" and not rec.get("completed_at"):
                rec["completed_at"] = stamp
        rec.pop("error", None)
    now_completed = rec.get("status") == "completed" and not was_completed
    if now_completed:
        _cancellation_after_completion(rec)
    return now_completed


def _cancellation_after_completion(rec: dict) -> None:
    """The buyer gets the signed copy when the envelope completes (the last buyer signs): count the cancellation
    period from that California date (tradecomply.contracts.cancellation_deadline) and compare it with the date of
    transaction printed in the Notice of Cancellation."""
    from tradecomply import contracts as C
    done = parse_time(rec.get("completed_at")) or datetime.fromtimestamp(_now(), timezone.utc)
    received = _ca_date(done)
    res = C.cancellation_deadline(received_signed_copy_on=received.isoformat(),
                                  buyer_age_65_or_older=bool(rec.get("buyer_age_65_or_older")),
                                  disaster_repair=bool(rec.get("disaster_repair")), as_of=_today_ca())
    p = res.cancellation
    rec["cancellation"] = {"kind": p.kind, "business_days": p.business_days,
                           "received_signed_copy_on": p.received_signed_copy_on,
                           "last_day_to_cancel": p.last_day_to_cancel,
                           "earliest_safe_start_date": p.earliest_safe_start_date, "verified": res.verified}
    match = received.isoformat() == rec.get("signing_date")
    rec["notice_dates_match"] = match
    if not match:
        rec["warning"] = (f"The buyer received the signed copy on {received.isoformat()}, but the Notice of "
                          f"Cancellation gives {rec.get('signing_date')} as the date of the transaction. The buyer may "
                          f"cancel until midnight of {p.last_day_to_cancel}. Give the buyer a corrected Notice of "
                          f"Cancellation with these dates and don't start work before {p.earliest_safe_start_date}.")


def _docusign_extra(project: dict) -> dict:
    extra = project.get("extra") if isinstance(project.get("extra"), dict) else {}
    ds = extra.get("docusign") if isinstance(extra.get("docusign"), dict) else {}
    envs = ds.get("envelopes") if isinstance(ds.get("envelopes"), list) else []
    return {**ds, "envelopes": [e for e in envs if isinstance(e, dict) and e.get("envelope_id")]}


# ---------------------------------------------------------------------------- the eSignature REST client
class DocusignAPI:
    """Calls to one account's eSignature REST API v2.1 for one tenant: bearer token (refreshed once after a 401),
    the rate-limit headers (a used-up hour or burst pauses every call for that account), plain error messages.
    Never logs a token or a body."""

    def __init__(self, connector: "DocusignConnector", http, account: dict):
        self.connector, self.http = connector, http
        self.account_id = account["account_id"]
        self.base = account["base_uri"] + API_PATH + quote(self.account_id, safe="")
        self.token: str | None = None

    def _note_limits(self, resp: httpx.Response) -> None:
        remaining, reset = _int(resp.headers.get("x-ratelimit-remaining")), _int(resp.headers.get("x-ratelimit-reset"))
        if remaining is not None and remaining <= 0 and reset:
            _set_pause(self.account_id, reset - _now())
        burst = _int(resp.headers.get("x-burstlimit-remaining"))
        if burst is not None and burst <= 0:
            _set_pause(self.account_id, BURST_PAUSE_S)

    def request(self, method: str, path: str, *, json_body: dict | None = None) -> httpx.Response:
        refreshed = False
        while True:
            _check_pause(self.account_id)
            if self.token is None:
                self.token = self.connector.access_token()
            headers = {"authorization": f"Bearer {self.token}", "accept": "application/json"}
            try:
                if method == "GET":
                    resp = self.http.get(self.base + path, headers=headers, retries=0, follow_redirects=False)
                else:
                    resp = self.http.post(self.base + path, json=json_body, headers=headers, retries=0,
                                          follow_redirects=False)
            except UpstreamError as e:
                raise ConnectorError(f"Could not reach Docusign: {e}") from None
            self._note_limits(resp)
            if resp.status_code == 401:
                if not refreshed:            # expired early or revoked: refresh once (401 = nothing was created)
                    refreshed = True
                    self.token = self.connector.access_token(leeway=FORCE_REFRESH)
                    continue
                self.connector.lost_access()
                raise NotConnected("Docusign no longer accepts this connection. Reconnect Docusign to continue.")
            if resp.status_code == 429:
                reset = _int(resp.headers.get("x-ratelimit-reset"))
                waits = [w for w in (retry_after_seconds(resp), (reset - _now()) if reset else None) if w]
                wait = max(waits) if waits else 60.0
                _set_pause(self.account_id, wait)
                log.warning("docusign: rate limited (429) for account %s, pausing %.0f s", self.account_id, wait)
                raise DocusignRateLimited(wait)
            return resp


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    code = body.get("errorCode") if isinstance(body, dict) else None
    return code if isinstance(code, str) and ERROR_CODE_RE.match(code) else None


def _fail_message(resp: httpx.Response, what: str) -> str:
    code = _error_code(resp)
    if resp.status_code == 403:
        return (f"Docusign refused {what} (HTTP 403{', ' + code if code else ''}). The Docusign user who connected "
                "needs permission to send envelopes in this account.")
    return f"Docusign refused {what} (HTTP {resp.status_code}{', ' + code if code else ''})."


# ---------------------------------------------------------------------------- the connector
class DocusignConnector(Connector):
    provider = "docusign"
    display_name = "Docusign"
    catalog_id = "docusign"
    description = ("Sign in with Docusign to send TradeComply home improvement contracts, with the right-to-cancel "
                   "notice and the Notice of Cancellation, to your company and the homeowner for e-signature, and see "
                   "on the project when they are signed.")
    category = "documents"
    docs_url = "https://developers.docusign.com/docs/esign-rest-api/"
    required_env = ("DOCUSIGN_INTEGRATION_KEY", "DOCUSIGN_SECRET", "DOCUSIGN_ENV")
    oauth = OAuth2Spec(
        authorize_url=ENVIRONMENTS["demo"] + "/oauth/auth", token_url=ENVIRONMENTS["demo"] + "/oauth/token",
        client_id_env="DOCUSIGN_INTEGRATION_KEY", client_secret_env="DOCUSIGN_SECRET", scopes=SCOPES, pkce=True,
        client_auth="basic", revoke_url=None)
    config_fields = (
        ConfigField("account_id", "Docusign account ID", pattern=r"[A-Za-z0-9-]{1,64}", max_length=64,
                    help="Leave blank to send from your default Docusign account. The connection test lists the "
                         "accounts your Docusign login can use."),
        ConfigField("contractor_signer_name", "Who signs for your company", max_length=NAME_MAX,
                    help="Name of the person who signs contracts for the contractor (can be changed per contract)."),
        ConfigField("contractor_signer_email", "Their email", type="email", max_length=SIGNER_EMAIL_MAX,
                    help="Docusign sends the contractor's copy to sign here first."),
        ConfigField("connect_hmac_key", "Connect HMAC key (optional)", type="password", secret=True, max_length=200,
                    help="In Docusign: Admin > Connect > Connect Keys > Add Secret Key, then paste it here (Docusign "
                         "shows it only once). With it, Docusign tells TradeComply as soon as a contract is signed; "
                         "without it, TradeComply checks an envelope's status when you open it, at most every 20 "
                         "minutes."),
    )
    INTERNAL_CONFIG = ("accounts", "environment", "docusign_user")
    unconfirmed = (
        "Docusign converts the HTML contract to PDF; whether its converter keeps TradeComply's type sizes (12-pt text, "
        "16-pt headings, which meet the statutory minimums) and the page breaks before the detachable Notice of "
        "Cancellation copies was not confirmed. Check the PDF of a demo envelope before going live.",
        "Anchor tab offsets (0, 0 pixels) and the white anchor text were not tried in a real envelope; Docusign says "
        "offsets take trial and error.",
        "The envelope-level JSON SIM settings (events plus eventData version restv2.1, format json) follow Docusign's "
        "Connect pages, whose envelope-level example was not shown in full.",
        "includeHMAC on an envelope-level webhook is assumed to sign with the account's Connect Keys; Docusign's HMAC "
        "setup steps describe account-level configurations. Whether every plan has the Connect Keys page was not "
        "confirmed.",
        "The HMAC key is used as the UTF-8 bytes of the key text, as in Docusign's validation samples (the sample code "
        "itself was not read).",
        "Connect messages are read for event, data.envelopeId, data.accountId (documented) and generatedDateTime "
        "(not seen in a sample message).",
        "No token revocation endpoint was found in Docusign's OAuth reference: Disconnect deletes the tokens in "
        "TradeComply only.",
        "The refresh request (grant_type=refresh_token, refresh_token in the form body, Basic client authentication) "
        "follows RFC 6749 section 6; Docusign's reference table lists refresh_token under headers.",
        "API base URIs are accepted only on docusign.net or docusign.com hosts (Docusign's examples: "
        "na3.docusign.net, eu.docusign.net, demo.docusign.net).",
        "Times without a time zone in Docusign responses are read as UTC (Docusign's examples end in Z).",
        "The buyer's cancellation period is counted from the envelope's completion, assuming Docusign emails every "
        "signer the completed copy then (the default; an account can change it).",
        "Docusign documents no idempotency key for creating envelopes: if the connection drops after Docusign accepted "
        "one, it can exist without TradeComply knowing its id (the id is logged when it is created).",
        "The connection test reads GET /restapi/v2.1/accounts/{accountId}; that any user of the account may read it "
        "was not confirmed.",
        "Whether a California home improvement contract signed through Docusign meets B&P 7159 and the Civil Code "
        "cancellation-notice delivery rules was not researched; ask your attorney before relying on it.",
    )

    def __init__(self, ctx):
        super().__init__(ctx)
        auth = ENVIRONMENTS[environment_name(ctx.env) or "demo"]
        self.oauth = replace(type(self).oauth, authorize_url=auth + "/oauth/auth", token_url=auth + "/oauth/token")

    # ------------------------------------------------------------------ operator configuration
    @classmethod
    def missing_env(cls, env=None) -> list[str]:
        import os
        env = os.environ if env is None else env
        missing = super().missing_env(env)
        if "DOCUSIGN_ENV" not in missing and environment_name(env) is None:
            missing.append("DOCUSIGN_ENV")
        return missing

    @classmethod
    def describe(cls, env=None) -> dict:
        import os
        env = os.environ if env is None else env
        return {**super().describe(env), "docusign_environment": environment_name(env)}

    def require_operator(self) -> None:
        if (self.ctx.env.get("DOCUSIGN_ENV") or "").strip() and environment_name(self.ctx.env) is None:
            raise NotConfigured("Docusign isn't set up on this server yet (DOCUSIGN_ENV must be 'demo' or "
                                "'production').")
        super().require_operator()

    def environment(self) -> str:
        return environment_name(self.ctx.env) or "demo"

    def auth_base(self) -> str:
        return ENVIRONMENTS[self.environment()]

    @classmethod
    def clean_config(cls, values, existing_config=None, existing_secrets=None):
        config, secret = super().clean_config(values, existing_config, existing_secrets)
        for key in cls.INTERNAL_CONFIG:
            if key in (existing_config or {}):
                config[key] = existing_config[key]
        return config, secret

    def _update_config(self, **changes) -> None:
        config = {**self.config(), **changes}
        self.save(config={k: v for k, v in config.items() if v is not None})

    def lost_access(self) -> None:
        self.save(status="error", last_error="Docusign no longer accepts this connection. Reconnect to continue.")

    # ------------------------------------------------------------------ OAuth specifics
    def _client_auth_headers(self, form: dict) -> dict:
        """obtain-access-token: 'your integration key and secret key, concatenated by a colon character, converted
        into Base64 and prefixed with the word Basic' (no form-encoding first)."""
        client_id, secret = self._oauth_client()
        raw = f"{client_id}:{secret}".encode("utf-8")
        return {"accept": "application/json", "authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None) -> dict:
        """The documented body: grant_type=authorization_code and code (+ the PKCE code_verifier)."""
        form = {"grant_type": "authorization_code", "code": code}
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._token_request(form)

    def on_connected(self, tokens: dict) -> str | None:
        """Right after sign-in (the one userinfo call per authentication): the accounts this login can use and
        their base URIs, stored in the settings, and the account's name as the label."""
        info = self._userinfo(tokens["access_token"])
        self._update_config(accounts=info["accounts"], environment=self.environment(), docusign_user=info["name"])
        account = self._pick(info["accounts"], self.config().get("account_id"), strict=False)
        if not account:
            return "No Docusign account found for this login"
        return account["account_name"] + (" (demo)" if self.environment() == "demo" else "")

    def _userinfo(self, token: str) -> dict:
        try:
            with self.ctx.http() as http:
                r = http.get(self.auth_base() + "/oauth/userinfo", retries=0, follow_redirects=False,
                             headers={"authorization": f"Bearer {token}", "accept": "application/json"})
        except UpstreamError as e:
            raise ConnectorError(f"Could not reach Docusign: {e}") from None
        if r.status_code == 429:
            raise DocusignRateLimited(retry_after_seconds(r) or 3600 - (_now() % 3600))
        if r.status_code == 401:
            raise NotConnected("Docusign no longer accepts this connection. Reconnect Docusign to continue.")
        if r.status_code != 200:
            raise ConnectorError(f"Docusign answered HTTP {r.status_code} when TradeComply asked which accounts "
                                 "you can use.")
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ConnectorError("Docusign sent your account list in a form TradeComply couldn't read.")
        accounts, skipped = [], 0
        for a in data.get("accounts") if isinstance(data.get("accounts"), list) else []:
            if not isinstance(a, dict):
                continue
            aid, base = str(a.get("account_id") or ""), clean_base_uri(a.get("base_uri"))
            if not ID_RE.match(aid) or not base:
                skipped += 1
                continue
            accounts.append({"account_id": aid, "account_name": _text(a.get("account_name"), 200) or aid,
                             "base_uri": base, "is_default": a.get("is_default") in (True, "true", "True")})
        if skipped:
            log.warning("docusign: %d account(s) from userinfo skipped (bad id or base_uri host)", skipped)
        return {"name": _text(data.get("name"), 200), "accounts": accounts[:MAX_ACCOUNTS]}

    @staticmethod
    def _pick(accounts: list, wanted: str | None, strict: bool = True) -> dict | None:
        if wanted:
            match = next((a for a in accounts if a["account_id"].lower() == str(wanted).lower()), None)
            if match or strict:
                if not match:
                    raise ConfigError("Your Docusign login can't use the account ID in the settings. Clear it to use "
                                      "your default account, or run the connection test to see your accounts.",
                                      "account_id")
                return match
        return next((a for a in accounts if a.get("is_default")), None) or (accounts[0] if accounts else None)

    def accounts(self) -> list[dict]:
        cfg = self.config()
        stored_env = cfg.get("environment")
        if stored_env and stored_env != self.environment():
            raise NotConnected(f"This Docusign connection was made in the {stored_env} environment and the server now "
                               f"uses {self.environment()}. Reconnect Docusign.")
        accounts = cfg.get("accounts") if isinstance(cfg.get("accounts"), list) else None
        if accounts is None:              # on_connected couldn't read them (e.g. throttled): once, now
            info = self._userinfo(self.access_token())
            self._update_config(accounts=info["accounts"], environment=self.environment(), docusign_user=info["name"])
            accounts = info["accounts"]
        return [a for a in accounts if isinstance(a, dict) and ID_RE.match(str(a.get("account_id", "")))
                and clean_base_uri(a.get("base_uri"))]

    def selected_account(self) -> dict:
        account = self._pick(self.accounts(), self.config().get("account_id"))
        if not account:
            raise ConnectorError("Your Docusign login has no account TradeComply can send from.")
        return account

    def _account_by_id(self, account_id: str) -> dict:
        match = next((a for a in self.accounts() if a["account_id"].lower() == str(account_id).lower()), None)
        if not match:
            raise ConnectorError("The Docusign account this envelope was sent from is no longer available to your "
                                 "Docusign login.")
        return match

    def test(self) -> TestResult:
        account = self.selected_account()
        details = {"environment": self.environment(), "account_id": account["account_id"],
                   "accounts": [{k: a[k] for k in ("account_id", "account_name", "is_default")}
                                for a in self.accounts()],
                   "connect_webhook": bool(self._hmac_key())}
        with self.ctx.http() as http:
            r = DocusignAPI(self, http, account).request("GET", "")
        if r.status_code != 200:
            return TestResult(False, _fail_message(r, "the account check"), details=details)
        try:
            data = r.json()
        except ValueError:
            data = {}
        name = _text((data or {}).get("accountName"), 200) or account["account_name"]
        return TestResult(True, f"Connected to {name} ({self.environment()}).", account=name, details=details)

    def _hmac_key(self) -> str | None:
        try:
            key = self.secrets().get("connect_hmac_key")
        except NotConnected:
            return None
        return key if isinstance(key, str) and key.strip() else None

    def connect_url(self, app_url: str | None) -> str | None:
        """The envelope-level webhook URL (https only; Connect needs a public https listener)."""
        if not app_url or not app_url.startswith("https://"):
            return None
        tid = self.ctx.tenant_id
        return f"{app_url.rstrip('/')}/api/integrations/docusign/connect-listener/{tid}/" \
               f"{connect_tag(self.ctx.platform, tid)}"

    # ------------------------------------------------------------------ envelopes
    def _ready(self) -> None:
        self.require_operator()
        conn = self.connection()
        if not conn or conn.get("status") not in ("connected", "error"):
            raise NotConnected("Connect Docusign first.")

    def _project(self, project_id) -> dict:
        project = self.ctx.store.get("projects", self.ctx.tenant_id, project_id)
        if not project:
            raise NotFound("Project not found")
        return project

    def find_envelope(self, envelope_id: str) -> tuple[int, dict] | None:
        """(project id, record) of this tenant's envelope, or None. Only this tenant's projects are searched."""
        if not isinstance(envelope_id, str) or not ID_RE.match(envelope_id):
            return None
        for p in self.ctx.store.list("projects", self.ctx.tenant_id, order="id desc", limit=MAX_PROJECTS_SCANNED):
            for rec in _docusign_extra(p)["envelopes"]:
                if str(rec.get("envelope_id")).lower() == envelope_id.lower():
                    return p["id"], rec
        return None

    def project_envelopes(self, project_id) -> list[dict]:
        project = self._project(project_id)
        return [_record_view(r, project["id"]) for r in reversed(_docusign_extra(project)["envelopes"])]

    def _update_record(self, project_id: int, envelope_id: str, change) -> dict | None:
        """Read-modify-write of one envelope record under the project's row lock. change(record) -> any."""
        holder: dict = {}

        def apply(row):
            extra = dict(row.get("extra") or {}) if isinstance(row.get("extra"), dict) else {}
            ds = _docusign_extra(row)
            for rec in ds["envelopes"]:
                if str(rec.get("envelope_id")).lower() == envelope_id.lower():
                    holder["result"] = change(rec)
                    holder["record"] = dict(rec)
                    break
            extra["docusign"] = ds
            return {"extra": extra}
        saved = self.ctx.store.modify("projects", self.ctx.tenant_id, project_id, apply)
        if saved is None or "record" not in holder:
            return None
        return holder

    def preview(self, *, contract: dict, buyer_emails: list, contractor_signer_name=None, contractor_signer_email=None,
                fill_ins: dict | None = None) -> dict:
        """What would be sent (no call to Docusign), or the problems / unfilled placeholders that block sending."""
        cfg = self.config()
        prep = prepare_contract(contract, buyer_emails, contractor_signer_name or cfg.get("contractor_signer_name"),
                                contractor_signer_email or cfg.get("contractor_signer_email"), fill_ins)
        c = prep.contract
        return {"ready": True, "html": prep.html, "document_sha256": prep.sha256, "contract_type": c.contract_type,
                "cancellation": c.cancellation.model_dump(), "notes": prep.notes,
                "signers": [{"role": s.role, "name": s.name, "email": _mask(s.email)} for s in prep.signers],
                "type_size_notes": [t.model_dump() for t in c.type_size_notes]}

    def send_contract(self, *, project_id, contract: dict, buyer_emails: list, contractor_signer_name=None,
                      contractor_signer_email=None, fill_ins: dict | None = None, email_subject: str | None = None,
                      email_message: str | None = None, app_url: str | None = None,
                      user_label: str | None = None) -> dict:
        """Generate, check and send the contract as one envelope, then save the contract as a TradeComply document and
        the envelope on the project. Everything that can fail locally is checked before Docusign is called."""
        self._ready()
        project = self._project(project_id)
        open_now = [r for r in _docusign_extra(project)["envelopes"] if r.get("status") not in FINAL_STATUSES]
        if len(open_now) >= MAX_ENVELOPES_PER_PROJECT:
            raise ConfigError(f"This project already has {MAX_ENVELOPES_PER_PROJECT} envelopes waiting for "
                              "signatures.")
        cfg = self.config()
        prep = prepare_contract(contract, buyer_emails, contractor_signer_name or cfg.get("contractor_signer_name"),
                                contractor_signer_email or cfg.get("contractor_signer_email"), fill_ins)
        c = prep.contract
        f_contractor = prep.signers[0]
        address = _text(contract.get("project_address"), 200) or "your project"
        subject = _text(email_subject, EMAIL_SUBJECT_MAX) or f"Home improvement contract: {address}"
        days = c.cancellation.business_days
        blurb = (_clean(email_message).strip() if isinstance(email_message, str) and email_message.strip() else
                 f"{_text(contract.get('contractor_name'), 200)} sent you a home improvement contract for {address}. "
                 f"It includes your right to cancel within {days} business days and two Notice of Cancellation "
                 "forms. Please read it before you sign.")
        account = self.selected_account()
        hook = self.connect_url(app_url) if self._hmac_key() else None
        body = envelope_definition(prep, email_subject=subject, email_blurb=blurb, connect_url=hook)
        with self.ctx.http() as http:
            resp = DocusignAPI(self, http, account).request("POST", "/envelopes", json_body=body)
        if resp.status_code not in (200, 201):
            raise ConnectorError(_fail_message(resp, "the envelope"))
        try:
            data = resp.json()
        except ValueError:
            data = {}
        envelope_id = str((data or {}).get("envelopeId") or "")
        if not ID_RE.match(envelope_id):
            raise ConnectorError("Docusign accepted the envelope but sent no envelope id TradeComply could read. Check "
                                 "your Docusign account before sending it again.")
        log.info("docusign: tenant %s created envelope %s (project %s)", self.ctx.tenant_id, envelope_id, project_id)
        status = (data or {}).get("status") if STATUS_RE.match(str((data or {}).get("status") or "")) else "sent"
        sent = parse_time((data or {}).get("statusDateTime")) or datetime.fromtimestamp(_now(), timezone.utc)
        doc = self.ctx.store.insert("documents", self.ctx.tenant_id, {
            "project_id": project["id"], "kind": "contract", "title": f"Home improvement contract, {address}"[:300],
            "body_markdown": prep.markdown, "source_tool": "generate_home_improvement_contract",
            "verified": bool(c.verified), "created_by": (user_label or "Docusign")[:200]})
        fields = c.cancellation
        record = {"envelope_id": envelope_id, "account_id": account["account_id"], "environment": self.environment(),
                  "status": status, "status_at": _iso(sent), "sent_at": _iso(sent), "source": "send",
                  "document_id": doc["id"], "document_sha256": prep.sha256, "signing_date": prep.signing_date,
                  "cancellation_kind": fields.kind, "business_days": fields.business_days,
                  "planned_last_day_to_cancel": fields.last_day_to_cancel,
                  "buyer_age_65_or_older": bool(contract.get("buyer_age_65_or_older")),
                  "disaster_repair": bool(contract.get("disaster_repair")),
                  "signers": [{"role": s.role, "name": s.name, "email": _mask(s.email)} for s in prep.signers],
                  "connect": bool(hook), "events": []}

        def add(row):
            """Append the record; to stay small, drop the oldest finished envelopes first (never an open one)."""
            extra = dict(row.get("extra") or {}) if isinstance(row.get("extra"), dict) else {}
            ds = _docusign_extra(row)
            envs = ds["envelopes"] + [record]
            while len(envs) > MAX_ENVELOPES_PER_PROJECT or len(json.dumps(envs)) > MAX_DOCUSIGN_EXTRA:
                old = next((i for i, r in enumerate(envs[:-1]) if r.get("status") in FINAL_STATUSES), None)
                if old is None:
                    break
                envs.pop(old)
            extra["docusign"] = {**ds, "envelopes": envs}
            return {"extra": extra}
        if self.ctx.store.modify("projects", self.ctx.tenant_id, project["id"], add) is None:
            log.warning("docusign: project %s was deleted before envelope %s could be recorded", project["id"],
                        envelope_id)
        self._emit("contract_generated", {
            "title": f"Contract sent for signature: {address}"[:200],
            "summary": f"Home improvement contract sent through Docusign to {f_contractor.name} and "
                       f"{len(prep.signers) - 1} buyer(s).", "severity": "info", "link": "/#/projects",
            "project_id": project["id"], "document_id": doc["id"], "envelope_id": envelope_id, "source": "docusign"},
            dedupe_key=f"docusign:{envelope_id}:sent")
        notes = list(prep.notes)
        if not hook:
            notes.append("No Connect webhook on this envelope (add a Connect HMAC key in the Docusign settings, and "
                         "the app must be on https): open the envelope to check its status.")
        return {"envelope": _record_view(record, project["id"]), "document_id": doc["id"],
                "project_id": project["id"], "cancellation": fields.model_dump(), "notes": notes}

    def envelope_status(self, envelope_id: str, *, refresh: bool = True) -> dict:
        """The envelope's record, refreshed from Docusign (GET envelope) when it isn't final and wasn't checked in
        the last 20 minutes. The check is claimed in the database first, so two requests never both poll."""
        loc = self.find_envelope(envelope_id)
        if not loc:
            raise NotFound("Envelope not found")
        project_id, rec = loc
        envelope_id = rec["envelope_id"]
        if not refresh or rec.get("status") in FINAL_STATUSES:
            return {**_record_view(rec, project_id), "checked": False}
        self._ready()
        now = _now()

        def claim(r):
            last = parse_time(r.get("checked_at"))
            if last and now - last.timestamp() < POLL_INTERVAL_S:
                return False
            r["checked_at"] = _iso(datetime.fromtimestamp(now, timezone.utc))
            return True
        claimed = self._update_record(project_id, envelope_id, claim)
        if not claimed or not claimed["result"]:
            current = claimed["record"] if claimed else rec
            return {**_record_view(current, project_id), "checked": False}
        account = self._account_by_id(rec["account_id"])
        with self.ctx.http() as http:
            resp = DocusignAPI(self, http, account).request("GET", f"/envelopes/{quote(envelope_id, safe='')}")
        if resp.status_code == 404:
            def missing(r):
                r["error"] = "Docusign can't find this envelope (deleted or in another account)."
            out = self._update_record(project_id, envelope_id, missing)
            return {**_record_view(out["record"] if out else rec, project_id), "checked": True}
        if resp.status_code != 200:
            raise ConnectorError(_fail_message(resp, "the envelope status request"))
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ConnectorError("Docusign sent the envelope status in a form TradeComply couldn't read.")
        status = str(data.get("status") or "").lower()
        times = {"sent_at": data.get("sentDateTime"), "delivered_at": data.get("deliveredDateTime"),
                 "completed_at": data.get("completedDateTime"), "declined_at": data.get("declinedDateTime"),
                 "voided_at": data.get("voidedDateTime")}
        at = parse_time(data.get("statusChangedDateTime"))
        out = self._update_record(project_id, envelope_id, lambda r: _merge_status(
            r, status, at=at, times=times, source="poll"))
        if out is None:
            raise NotFound("Envelope not found")
        if out["result"]:
            self._completed(project_id, out["record"])
        return {**_record_view(out["record"], project_id), "checked": True}

    def apply_connect_message(self, message: dict) -> str:
        """A verified Connect (JSON SIM) message: update this tenant's envelope. Returns what happened."""
        event = message.get("event") if isinstance(message.get("event"), str) else None
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        envelope_id = data.get("envelopeId") if isinstance(data.get("envelopeId"), str) else ""
        if not event or not ID_RE.match(envelope_id):
            return "ignored: not an envelope event"
        loc = self.find_envelope(envelope_id)
        if not loc:
            return "ignored: unknown envelope"
        project_id, rec = loc
        account_id = data.get("accountId")
        if isinstance(account_id, str) and account_id and account_id.lower() != str(rec.get("account_id")).lower():
            return "ignored: another account"
        summary = data.get("envelopeSummary") if isinstance(data.get("envelopeSummary"), dict) else {}
        times = {"sent_at": summary.get("sentDateTime"), "delivered_at": summary.get("deliveredDateTime"),
                 "completed_at": summary.get("completedDateTime"), "declined_at": summary.get("declinedDateTime"),
                 "voided_at": summary.get("voidedDateTime")}
        at = parse_time(message.get("generatedDateTime"))
        status = EVENT_STATUS.get(event)
        if status and not times.get(f"{status}_at") and at:
            times[f"{status}_at"] = _iso(at)
        out = self._update_record(project_id, rec["envelope_id"], lambda r: _merge_status(
            r, status, at=at, times={k: v for k, v in times.items() if v}, source="connect", event=event[:40]))
        if out is None:
            return "ignored: unknown envelope"
        if out["result"]:
            self._completed(project_id, out["record"])
        return "applied"

    def _completed(self, project_id: int, rec: dict) -> None:
        cancel = rec.get("cancellation") or {}
        if not cancel.get("last_day_to_cancel"):
            return
        match = rec.get("notice_dates_match", True)
        self._emit("deadline_due_soon", {
            "title": f"Contract signed: buyer may cancel until {cancel['last_day_to_cancel']}"[:200],
            "summary": (rec.get("warning") or f"Don't start work before {cancel['earliest_safe_start_date']}.")[:2000],
            "severity": "info" if match else "warning", "link": "/#/projects",
            "due_date": cancel["last_day_to_cancel"], "project_id": project_id, "envelope_id": rec["envelope_id"],
            "document_id": rec.get("document_id"), "source": "docusign"},
            dedupe_key=f"docusign:{rec['envelope_id']}:completed")

    def _emit(self, event: str, payload: dict, dedupe_key: str) -> None:
        try:
            self.ctx.platform.events.emit(self.ctx.tenant_id, event, payload, dedupe_key=dedupe_key)
        except ValueError:
            log.exception("docusign: bad %s payload", event)


def handle_connect(platform, tenant_id: str, tag: str, body: bytes, headers: dict) -> tuple[int, dict]:
    """The Connect listener's work (runs on a worker thread): check the URL tag, the tenant's HMAC key and the
    signature, then apply the message to that tenant's envelope. Answers 200 for every verified message (so Docusign
    doesn't retry one TradeComply chose to ignore), 401 for a bad or missing signature, 404 for an unknown URL."""
    if not TENANT_RE.match(tenant_id) or not TAG_RE.match(tag) or \
            not hmac.compare_digest(tag, connect_tag(platform, tenant_id)):
        return 404, {"detail": "Not found"}
    inst = DocusignConnector(ConnectorContext(platform, tenant_id))
    if not inst.connection():
        return 404, {"detail": "Not found"}
    key = inst._hmac_key()
    if not key or not verify_connect_hmac(key, body, headers):
        log.warning("docusign: Connect message for tenant %s refused (%s)", tenant_id,
                    "no HMAC key saved" if not key else "signature mismatch")
        return 401, {"detail": "Signature check failed"}
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 200, {"ok": True, "result": "ignored: not JSON"}
    if not isinstance(message, dict):
        return 200, {"ok": True, "result": "ignored: not an object"}
    result = inst.apply_connect_message(message)
    log.info("docusign: Connect %s for tenant %s: %s", str(message.get("event"))[:40], tenant_id, result)
    return 200, {"ok": True, "result": result}


# ---------------------------------------------------------------------------- HTTP routes
router = APIRouter(prefix="/api/integrations/docusign", tags=["integrations"])
_send_limiter = RateLimiter(limit=30, window_s=60 * 60)           # envelopes per tenant per hour
_connect_limiter = RateLimiter(limit=2000, window_s=60 * 60)      # Connect messages per tenant per hour


class ContractIn(BaseModel):
    contract: dict = Field(description="The fields of generate_home_improvement_contract (ContractFields)")
    buyer_emails: list[str] = Field(min_length=1, max_length=MAX_BUYERS)
    contractor_signer_name: Optional[str] = Field(None, max_length=200)
    contractor_signer_email: Optional[str] = Field(None, max_length=200)
    fill_ins: dict[str, str] = Field(default_factory=dict, max_length=50)


class SendIn(ContractIn):
    project_id: int = Field(gt=0, lt=2 ** 63)
    email_subject: Optional[str] = Field(None, max_length=200)
    email_message: Optional[str] = Field(None, max_length=EMAIL_BLURB_MAX)


def _instance(request: Request, p: Principal) -> DocusignConnector:
    return DocusignConnector(ConnectorContext(platform_of(request), p.tenant_id, p.user_id, p.label))


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, DocusignRateLimited):
        return HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after)})
    if isinstance(e, ContractNotReady):
        return HTTPException(422, {"message": str(e), "problems": e.problems, "unfilled_placeholders": e.unfilled})
    if isinstance(e, ConfigError):
        return HTTPException(422, {"message": str(e), "field": e.field})
    if isinstance(e, ConnectorError):
        return HTTPException(e.status_code, str(e))
    if isinstance(e, SafeHTTPError):
        return HTTPException(502, str(e))
    log.exception("docusign: unexpected failure")
    return HTTPException(500, "The Docusign integration failed unexpectedly")


def _audit(request: Request, p: Principal, action: str, args: dict, ok: bool = True) -> None:
    try:
        platform_of(request).store.audit(p.tenant_id, p.label, f"integrations.docusign_{action}", args, None, ok=ok,
                                         source="integrations")
    except Exception:      # noqa: BLE001 - the action already happened
        log.exception("docusign: audit write failed")


@router.post("/preview")
def preview(body: ContractIn, request: Request, p: Principal = Depends(office_up)):
    """The document that would be sent (HTML with invisible anchors), or 422 with the contract's problems and the
    placeholders still to fill in. No call to Docusign."""
    try:
        return _instance(request, p).preview(contract=body.contract, buyer_emails=body.buyer_emails,
                                             contractor_signer_name=body.contractor_signer_name,
                                             contractor_signer_email=body.contractor_signer_email,
                                             fill_ins=body.fill_ins)
    except Exception as e:      # noqa: BLE001 - mapped to a plain message
        raise _fail(e)


@router.post("/envelopes", status_code=201)
def send_envelope(body: SendIn, request: Request, p: Principal = Depends(office_up)):
    """Generate the home improvement contract, check it, and send it through Docusign to the contractor signer and
    the buyer(s). The envelope id is stored on the project and the contract is saved as a document."""
    platform = platform_of(request)
    require_entitled(platform, p)
    key = f"tenant|{p.tenant_id}"
    wait = _send_limiter.retry_after(key)
    if wait:
        raise HTTPException(429, f"Too many contracts sent in an hour. Try again in {max(1, wait // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})
    inst = _instance(request, p)
    try:
        inst.require_operator()
        _send_limiter.hit(key)
        result = inst.send_contract(project_id=body.project_id, contract=body.contract, buyer_emails=body.buyer_emails,
                                    contractor_signer_name=body.contractor_signer_name,
                                    contractor_signer_email=body.contractor_signer_email, fill_ins=body.fill_ins,
                                    email_subject=body.email_subject, email_message=body.email_message,
                                    app_url=platform.settings.app_url or base_url(request), user_label=p.label)
    except Exception as e:      # noqa: BLE001
        _audit(request, p, "send", {"project_id": body.project_id}, ok=False)
        raise _fail(e)
    _audit(request, p, "send", {"project_id": result["project_id"], "envelope_id": result["envelope"]["envelope_id"],
                                "document_id": result["document_id"]})
    return result


@router.get("/envelopes/{envelope_id}")
def get_envelope(envelope_id: str, request: Request, refresh: bool = Query(True), p: Principal = Depends(office_up)):
    """The envelope's status. Asks Docusign at most every 20 minutes per envelope (Docusign's polling rule) and
    never after it is completed, declined or voided; refresh=false never asks."""
    if not ID_RE.match(envelope_id):
        raise HTTPException(404, "Envelope not found")
    if refresh:
        require_entitled(platform_of(request), p)
    try:
        return _instance(request, p).envelope_status(envelope_id, refresh=refresh)
    except Exception as e:      # noqa: BLE001
        raise _fail(e)


@router.get("/projects/{project_id}/envelopes")
def list_project_envelopes(project_id: int, request: Request, p: Principal = Depends(office_up)):
    """The project's Docusign envelopes, newest first (no call to Docusign)."""
    try:
        return {"project_id": project_id, "envelopes": _instance(request, p).project_envelopes(project_id)}
    except Exception as e:      # noqa: BLE001
        raise _fail(e)


@router.post("/connect-listener/{tenant_id}/{tag}", include_in_schema=False)
async def connect_listener(tenant_id: str, tag: str, request: Request):
    """Docusign Connect posts envelope events here (the URL is put on each envelope TradeComply sends). No session:
    the URL tag and the HMAC signature authenticate the message."""
    platform = platform_of(request)
    if not TENANT_RE.match(tenant_id) or not TAG_RE.match(tag) or \
            not hmac.compare_digest(tag, connect_tag(platform, tenant_id)):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    key = f"tenant|{tenant_id}"           # counted only for URLs TradeComply made, so junk can't use up the limit
    wait = _connect_limiter.retry_after(key)
    if wait:
        return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": str(wait)})
    _connect_limiter.hit(key)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_CONNECT_BODY:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items() if k.lower().startswith("x-docusign-signature-")}
    status, out = await anyio.to_thread.run_sync(lambda: handle_connect(platform, tenant_id, tag, body, headers))
    return JSONResponse(out, status_code=status)
