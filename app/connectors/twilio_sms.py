"""Text-message (SMS) alerts through the operator's Twilio account, to phone numbers each company adds with recorded
consent, with STOP/START replies honored from Twilio's signed inbound webhook.

Who holds what
- Operator (the people running this TradeComply server), env vars: TWILIO_ACCOUNT_SID; TWILIO_AUTH_TOKEN, or an API
  key TWILIO_API_KEY (SK...) + TWILIO_API_SECRET for the REST calls; TWILIO_MESSAGING_SERVICE_SID (MG..., preferred)
  or TWILIO_FROM (an E.164 Twilio number). Validating Twilio's inbound webhook needs TWILIO_AUTH_TOKEN even when an API
  key sends the texts (Twilio signs webhooks with the Auth Token); without it STOP replies are not recorded here
  (Twilio still blocks texts to that phone and the send fails with error 21610, which is recorded as an opt-out).
  Until the env vars are set the settings page shows "not set up on this server" and nothing is sent.
- A company (tenant): which alerts to text (lowest severity, optional event list, a daily cap per phone) and the
  phone numbers. Numbers, names and consent records are encrypted at rest in a separate store row
  (integration_connections provider "sms_recipients", which no connector or generic route touches) so a settings
  save or a disconnect can never lose an opt-out.

US A2P 10DLC: carriers block application-to-person texts to US phones from unregistered 10-digit numbers. Before
production texting the operator registers a Brand and Campaign in Twilio (Trust Hub) and puts the sender in a
Messaging Service linked to that campaign (toll-free numbers need toll-free verification instead). The connection
test reports the Messaging Service's `us_app_to_person_registered` flag; the settings page says the same.

Consent: an owner or office user who is signed in (not an API key) adds a number and confirms CONSENT_TEXT, that the
person agreed to receive alert texts at that number. The record keeps the time, the user's id and email, the exact
wording confirmed and a history of every opt-in and opt-out. A confirmation text (program name, "Msg frequency
varies", "Msg & data rates may apply", HELP and STOP) is sent right after, which is what CTIA guidance and A2P
campaign reviews look for; the exact campaign wording is the operator's to register. A number that replied STOP can't
be added back by staff: the person texts START (or YES) to opt back in. Texts staff trigger by hand (confirmations,
test texts) are limited per company and to MANUAL_TEXTS_PER_PHONE_DAY per phone per day, so removing and re-adding
a number can't flood a phone at the operator's expense.

Inbound replies (POST /api/integrations/twilio-sms/inbound, configured as the Messaging Service's or number's
"A message comes in" webhook): the X-Twilio-Signature is checked (HMAC-SHA1 with the Auth Token over the full URL
plus the POST parameters sorted by name, Base64), AccountSid must be this operator's, then STOP-type replies mark the
phone opted out and START/YES mark it opted back in. The signature has no timestamp, so each MessageSid is applied
once (remembered REPLY_DEDUPE_S): a captured request replayed later changes nothing. All companies share the operator's sender, and Twilio opts a
phone out of the whole sender (Messaging Service), so a reply applies to that phone in every company's list; nothing
about the other companies is revealed. The answer is an empty TwiML <Response/>, so Twilio's own opt-out replies are
the only ones sent.

Message length: Body may be up to 1,600 characters; more than 160 GSM-7 characters (70 UCS-2) is split into
segments, each billed. Alerts are composed to fit MAX_SEGMENTS segments (153 GSM-7 / 67 UCS-2 characters each once
concatenated, per 3GPP TS 23.038/23.040, not re-read this session), with typographic quotes and dashes replaced so
plain GSM-7 is used where possible. Every alert starts with "TradeComply (<company>):" and ends with "Reply STOP to
opt out."

Sources (read 2026-11-26; www.twilio.com is blocked from the build environment, so through Context7's index of
twilio.com/docs and Twilio's official OpenAPI spec, github.com/twilio/twilio-oai spec/json/twilio_api_v2010.json and
twilio_messaging_v1.json, read directly):
- CreateMessage (api_v2010): POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json,
  form-encoded, HTTP Basic (accountSid_authToken); To required; MessagingServiceSid ^MG[0-9a-fA-F]{32}$ ("when this
  parameter is provided and the from parameter is omitted, Twilio selects the optimal sender"); From; Body "can be up
  to 1,600 characters ... If the body contains more than 160 GSM-7 characters (or 70 UCS-2 characters), the message
  is segmented and charged accordingly"; answers 201 with the Message resource (sid ^(SM|MM)[0-9a-fA-F]{32}$, status
  queued/accepted/..., error_code, num_segments). Global-infrastructure docs: "-u $TWILIO_API_KEY:$TWILIO_API_SECRET".
  Key resource sid ^SK[0-9a-fA-F]{32}$, Account sid ^AC[0-9a-fA-F]{32}$.
- usage/twilios-response: errors are JSON {status, message, code, more_info}. iam/test-credentials: 21211 invalid
  number, 21612 can't route, 21408 no permission for the region, 21610 blocked (unsubscribed), 21614 not
  SMS-capable. api/errors: 21617 body over 1600 characters, 21635 landline.
- FetchService (messaging_v1): GET https://messaging.twilio.com/v1/Services/{Sid}; us_app_to_person_registered
  "Whether US A2P campaign is registered for this Service". ReadIncomingPhoneNumber: GET
  /2010-04-01/Accounts/{AccountSid}/IncomingPhoneNumbers.json?PhoneNumber=... (capabilities.sms).
- usage/security: the X-Twilio-Signature algorithm and its worked example (Auth Token 12345 ->
  L/OH5YylLD5NRKLltdqwSvS0BnU=, used in the tests); twilio-python RequestValidator checks the URL with and without
  the :443 port "since sig generation on back end is inconsistent". usage/api-security-reference-guide/webhook:
  reject any request with a mismatched or missing signature.
- messaging/twiml: inbound messages are form-encoded POSTs (From, To, Body, MessageSid, AccountSid,
  MessagingServiceSid, ...); "Opt out keywords are passed to your application ... All future messages to the user are
  not sent. 'Start' and 'Yes' keywords will also be passed to your application and to opt users back in"; opting out
  of a number in a Messaging Service opts out of the whole service; OptOutType (STOP, HELP, START) is sent only with
  Advanced Opt-Out enabled. Status callbacks may be answered with 204 or an empty <Response/>.
- messaging/compliance/a2p-10dlc and the free-trial guide: US 10DLC numbers must be registered before sending to US
  recipients; "Carriers block any A2P messaging traffic to US recipients using unregistered Twilio 10DLC numbers";
  registration needs a paid account.

HTTP API (`router`; app/main.py must include it before the catch-all MCP mount at "/"):
GET/POST /api/integrations/twilio_sms/recipients, DELETE /api/integrations/twilio_sms/recipients/{id},
POST /api/integrations/twilio_sms/recipients/{id}/test (owner/office), and the public, signature-checked
POST /api/integrations/twilio-sms/inbound (hyphenated: Twilio's StatusCallback rule says "underscores are not
allowed", so the Twilio-facing path has none).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import math
import os
import re
import secrets as _secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import ConfigError, ConfigField, Connector, ConnectorError, NotConfigured, TestResult
from .alerts import EVENTS_FIELD, clean_event_list, company_name, delivery_id, human_date, severity_field, wants
from ..auth import Principal, RateLimiter, office_up, platform_of
from ..billing import base_url, require_entitled
from ..crypto import DecryptError, connection_context
from ..events import EVENTS
from ..safe_http import SafeHTTPError
from ..store import now_iso

log = logging.getLogger("tradecomply.connectors.twilio_sms")

API_BASE = "https://api.twilio.com"
MESSAGING_BASE = "https://messaging.twilio.com"
DATA_PROVIDER = "sms_recipients"          # the encrypted phone book's store row (not a connector)
INBOUND_PATH = "/api/integrations/twilio-sms/inbound"
MAX_BODY = 1600                           # CreateMessage Body limit
MAX_SEGMENTS = 2
MAX_RECIPIENTS = 50
HISTORY_KEEP = 30
SENT_KEEP_DAYS = 5                        # longer than the event bus's ~3-day retry window
SID_RE = {"account": re.compile(r"^AC[0-9a-fA-F]{32}$"), "key": re.compile(r"^SK[0-9a-fA-F]{32}$"),
          "service": re.compile(r"^MG[0-9a-fA-F]{32}$")}
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
MESSAGE_SID_RE = re.compile(r"^(SM|MM)[0-9a-fA-F]{32}$")      # api_v2010 Message sid pattern
REPLY_DEDUPE_S = 3 * 366 * 86400          # how long an applied STOP/START MessageSid is remembered (replay guard)
MANUAL_TEXTS_PER_PHONE_DAY = 5            # confirmation + test texts to one phone in 24 hours, all companies together
NANP_RE = re.compile(r"^\+1[2-9][0-9]{2}[2-9][0-9]{6}$")
OPTED_OUT_CODE = 21610
RECIPIENT_ERRORS = {21211: "Twilio says this is not a valid phone number",
                    21408: "texting this country isn't enabled on the server's Twilio account",
                    21612: "Twilio can't send texts to this number",
                    21614: "this number can't receive text messages",
                    21635: "this number is a landline"}
# Keywords: STOP/START/YES are in Twilio's messaging/twiml page; the rest of the list follows Twilio's help article
# on opt-out keywords, which could not be read (see unconfirmed). Matching errs toward opting people out.
OPT_OUT_WORDS = frozenset({"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"})
OPT_IN_WORDS = frozenset({"START", "YES", "UNSTOP"})
HELP_WORDS = frozenset({"HELP", "INFO"})
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
A2P_NOTE = ("US carriers block texts from 10-digit numbers that are not registered for A2P 10DLC: before production "
            "texting, the TradeComply operator must register the sending number's Brand and Campaign in Twilio (or "
            "use a verified toll-free number). Until then texts may be rejected or never delivered.")
CONSENT_TEXT = ("I confirm this person agreed to receive TradeComply compliance alert texts for {company} at this "
                "number. Message frequency varies. Message and data rates may apply. They can reply STOP to opt out "
                "or HELP for help.")
CONFIRMATION_SMS = ("TradeComply ({company}): you're signed up for compliance alert texts. Msg frequency varies. "
                    "Msg & data rates may apply. Reply HELP for help, STOP to opt out.")

# GSM 03.38 default alphabet and its extension table (extension characters take two septets)
GSM_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM_EXTENDED = set("^{}\\[~]|€\f")
ASCII_FOR = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
             "…": "...", " ": " ", "•": "-", "·": "-", "→": "->"}


# ---------------------------------------------------------------------------- phone numbers
def normalize_phone(value) -> str | None:
    """+1XXXXXXXXXX for a US/Canada (NANP) number written any common way, else None. Only NANP numbers are accepted:
    the sender is a US number and other countries need extra Twilio permissions (error 21408)."""
    text = str(value or "").strip()
    if not text or len(text) > 40:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if text.startswith("+") and not text.startswith("+1"):
        return None
    if len(digits) == 10:
        digits = "1" + digits
    phone = "+" + digits
    return phone if NANP_RE.match(phone) else None


def mask_phone(phone: str) -> str:
    return f"•••-•••-{phone[-4:]}" if phone and len(phone) >= 4 else "•••"


# ---------------------------------------------------------------------------- message length
def plain(text: str) -> str:
    """Typographic punctuation to its ASCII form (keeps GSM-7, so a segment holds 160 characters, not 70)."""
    out = "".join(ASCII_FOR.get(ch, ch) for ch in str(text or ""))
    return re.sub(r"[\x00-\x08\x0b\x0e-\x1f\x7f]", "", out)


def encoding(text: str) -> str:
    return "GSM-7" if all(ch in GSM_BASIC or ch in GSM_EXTENDED for ch in text) else "UCS-2"


def segments(text: str) -> int:
    """How many SMS segments `text` takes (GSM-7: 160 / 153 per concatenated part; UCS-2: 70 / 67 UTF-16 units)."""
    if encoding(text) == "GSM-7":
        units, single, multi = sum(2 if ch in GSM_EXTENDED else 1 for ch in text), 160, 153
    else:
        units, single, multi = len(text.encode("utf-16-le")) // 2, 70, 67
    if units <= single:
        return 1
    return math.ceil(units / multi)


def _shorten(text: str, n: int) -> str:
    return text if len(text) <= n else (text[: max(0, n - 3)].rstrip() + "...")


def fit_sms(head: str, middle: str, tail: str, max_segments: int = MAX_SEGMENTS) -> str:
    """head + middle + tail cut (middle first, then head) so it fits max_segments and 1600 characters."""
    def ok(t: str) -> bool:
        return len(t) <= MAX_BODY and segments(t) <= max_segments

    full = head + middle + tail
    if ok(full):
        return full
    lo, hi = 0, len(middle)
    while lo < hi:                       # the longest prefix of `middle` that still fits
        mid = (lo + hi + 1) // 2
        if ok(head + _shorten(middle, mid) + tail):
            lo = mid
        else:
            hi = mid - 1
    if lo >= 12:
        return head + _shorten(middle, lo) + tail
    lo, hi = 0, len(head)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(_shorten(head, mid) + tail):
            lo = mid
        else:
            hi = mid - 1
    return _shorten(head, lo) + tail


def alert_text(event: str, payload: dict, company: str | None, app_url: str | None,
               max_segments: int = MAX_SEGMENTS) -> str:
    from .. import webhooks
    prefix = f"TradeComply ({plain(company)[:60]}): " if company else "TradeComply: "
    title = plain(payload.get("title") or event.replace("_", " "))
    due = human_date(payload.get("due_date"))
    head = prefix + title.rstrip(".") + "." + (f" Due {due}." if due and due not in title else "")
    link = webhooks.absolute_link(payload.get("link"), app_url)
    tail = (f" {link}" if link else "") + " Reply STOP to opt out."
    middle = (" " + plain(payload["summary"]).strip()) if payload.get("summary") else ""
    return fit_sms(head, middle, tail, max_segments)


# ---------------------------------------------------------------------------- the encrypted phone book
def _ctx(tenant_id: str) -> str:
    return connection_context(tenant_id, DATA_PROVIDER)


def load_book(platform, tenant_id: str) -> tuple[dict, str | None, bool]:
    """(book, stored blob, row exists). The book is {"recipients": [...]}."""
    conn = platform.store.get_connection(tenant_id, DATA_PROVIDER, with_secret=True)
    blob = (conn or {}).get("secret_blob")
    if not blob:
        return {"recipients": []}, blob, conn is not None
    try:
        book = platform.secret_box.decrypt_json(blob, _ctx(tenant_id))
    except DecryptError:
        raise ConnectorError("The saved text-alert phone numbers can't be read on this server (the encryption key "
                             "changed). Ask the operator to restore INTEGRATIONS_KEY.") from None
    if not isinstance(book, dict) or not isinstance(book.get("recipients"), list):
        book = {"recipients": []}
    return book, blob, True


def modify_book(platform, tenant_id: str, change):
    """Apply change(book) -> result under compare-and-set (an inbound STOP and a staff edit can't overwrite each
    other). Returns change's result."""
    store = platform.store
    for _ in range(10):
        book, blob, exists = load_book(platform, tenant_id)
        if not exists:
            store.save_connection(tenant_id, DATA_PROVIDER, status="disconnected")
        result = change(book)
        new_blob = platform.secret_box.encrypt_json(book, _ctx(tenant_id))
        if store.update_connection(tenant_id, DATA_PROVIDER, expect_secret_blob=blob, secret_blob=new_blob):
            return result
    raise ConnectorError("The phone list is busy. Try again.")


def _history(rec: dict, action: str, via: str, by: str | None = None) -> None:
    rec.setdefault("history", []).append({"at": now_iso(), "action": action, "via": via, **({"by": by} if by else {})})
    rec["history"] = rec["history"][-HISTORY_KEEP:]


def _find(book: dict, rid: str) -> dict | None:
    return next((r for r in book["recipients"] if r.get("id") == rid), None)


def recipient_view(rec: dict) -> dict:
    consent = rec.get("consent") or {}
    return {"id": rec.get("id"), "name": rec.get("name"), "phone": mask_phone(rec.get("phone", "")),
            "status": rec.get("status"), "consent_at": consent.get("at"), "consent_by": consent.get("by"),
            "opted_out_at": rec.get("opted_out_at"), "opted_out_via": rec.get("opted_out_via"),
            "last_sent_at": rec.get("last_sent_at"), "last_status": rec.get("last_status"),
            "last_error": rec.get("last_error")}


def _ca_day() -> str:
    from tradecomply import logic
    return logic.today_ca().isoformat()


# ---------------------------------------------------------------------------- Twilio webhook signature
def compute_signature(auth_token: str, url: str, params) -> str:
    """X-Twilio-Signature (usage/security): Base64(HMAC-SHA1(Auth Token, URL + each POST name + value, names sorted;
    repeated names' values sorted, as twilio-python's RequestValidator does))."""
    pairs = list(params.items()) if isinstance(params, dict) else list(params or [])
    by_name: dict[str, set] = {}
    for k, v in pairs:
        by_name.setdefault(k, set()).add(v)
    s = url + "".join(name + value for name in sorted(by_name) for value in sorted(by_name[name]))
    return base64.b64encode(hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()).decode()


def _port_variants(url: str) -> list[str]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        return [url, urlunsplit(parts._replace(netloc=host))]
    default = 443 if parts.scheme == "https" else 80
    return [url, urlunsplit(parts._replace(netloc=f"{host}:{default}"))]


def valid_signature(auth_token: str, urls, params, signature: str | None) -> bool:
    if not signature or not auth_token:
        return False
    return any(hmac.compare_digest(compute_signature(auth_token, variant, params), signature)
               for url in urls for variant in _port_variants(url))


def classify_reply(body: str | None, opt_out_type: str | None = None) -> str | None:
    """'stop', 'start', 'help' or None. Twilio's OptOutType wins when present (Advanced Opt-Out)."""
    kind = (opt_out_type or "").strip().upper()
    if kind in ("STOP", "START", "HELP"):
        return kind.lower()
    words = re.findall(r"[A-Za-z]+", str(body or "").upper())
    if not words:
        return None
    if len(words) == 1 and words[0] in OPT_IN_WORDS:
        return "start"
    if words[0] in OPT_OUT_WORDS or (len(words) <= 3 and OPT_OUT_WORDS.intersection(words)):
        return "stop"
    if len(words) == 1 and words[0] in HELP_WORDS:
        return "help"
    return None


def apply_reply(platform, phone: str, action: str) -> int:
    """OPERATOR ONLY: record a STOP or START reply from `phone` in every company's list. Returns how many records
    changed."""
    changed = 0
    for conn in platform.store.connections_for_provider(DATA_PROVIDER, status="disconnected"):
        tid = conn["tenant_id"]

        def change(book):
            n = 0
            for rec in book["recipients"]:
                if rec.get("phone") != phone:
                    continue
                if action == "stop" and rec.get("status") == "active":
                    rec.update(status="opted_out", opted_out_at=now_iso(), opted_out_via="reply")
                    _history(rec, "opt_out", "reply")
                    n += 1
                elif action == "start" and rec.get("status") == "opted_out":
                    rec.update(status="active", opted_out_at=None, opted_out_via=None, last_error=None)
                    _history(rec, "opt_in", "reply")
                    n += 1
            return n
        try:
            book, _, _ = load_book(platform, tid)
            if any(r.get("phone") == phone for r in book["recipients"]):
                changed += modify_book(platform, tid, change)
        except ConnectorError:
            log.warning("SMS reply could not be recorded for tenant %s (phone list unreadable)", tid)
    return changed


# ---------------------------------------------------------------------------- the connector
class TwilioSmsConnector(Connector):
    provider = "twilio_sms"
    display_name = "Text message alerts (Twilio)"
    catalog_id = "twilio"
    description = ("Texts TradeComply alerts to the phone numbers your company adds, each with the person's consent "
                   "recorded. Replies of STOP are honored right away. " + A2P_NOTE)
    category = "messaging"
    docs_url = "https://www.twilio.com/docs/messaging/api/message-resource"
    required_env = ("TWILIO_ACCOUNT_SID",)          # plus the either/or pairs checked in missing_env()
    config_fields = (
        severity_field("warning", "Each text costs money and interrupts someone in the field. " + A2P_NOTE),
        EVENTS_FIELD,
        ConfigField("max_per_day", "Most texts per phone per day", type="number", default=10,
                    help="1 to 50. Alerts over the limit are skipped for that phone until the next day."),
    )
    subscribes = tuple(EVENTS)
    unconfirmed = (
        "Opt-out keywords beyond STOP and opt-in keywords beyond START and YES (STOPALL, UNSUBSCRIBE, CANCEL, END, "
        "QUIT, REVOKE, OPTOUT, UNSTOP) come from Twilio's help article, which could not be read; TradeComply treats "
        "any short reply containing one of them as an opt-out.",
        "Delivery receipts (status callbacks) are not used: a text Twilio accepted can still fail later (for example "
        "error 30034 for an unregistered 10DLC sender) without this page knowing. Check the operator's Twilio "
        "Messaging logs.",
        "Whether Twilio answers HELP on its own for a number without Advanced Opt-Out was not confirmed; the operator "
        "should set the HELP reply in the Messaging Service's Advanced Opt-Out settings.",
        "The 153/67-character size of concatenated segments is from the GSM standards, not re-read on Twilio's "
        "pages.",
    )

    # ------------------------------------------------------------------ operator configuration
    @classmethod
    def missing_env(cls, env=None) -> list[str]:
        env = os.environ if env is None else env

        def val(name):
            return (env.get(name) or "").strip()
        missing = []
        if not SID_RE["account"].match(val("TWILIO_ACCOUNT_SID")):
            missing.append("TWILIO_ACCOUNT_SID")
        key_ok = SID_RE["key"].match(val("TWILIO_API_KEY")) and val("TWILIO_API_SECRET")
        if not (val("TWILIO_AUTH_TOKEN") or key_ok):
            missing.append("TWILIO_AUTH_TOKEN (or TWILIO_API_KEY and TWILIO_API_SECRET)")
        if not (SID_RE["service"].match(val("TWILIO_MESSAGING_SERVICE_SID")) or E164_RE.match(val("TWILIO_FROM"))):
            missing.append("TWILIO_MESSAGING_SERVICE_SID (or TWILIO_FROM)")
        return missing

    @classmethod
    def describe(cls, env=None) -> dict:
        env = os.environ if env is None else env
        return {**super().describe(env), "a2p_10dlc_note": A2P_NOTE, "inbound_webhook_path": INBOUND_PATH,
                "replies_recorded": bool((env.get("TWILIO_AUTH_TOKEN") or "").strip())}

    @classmethod
    def clean_config(cls, values, existing_config=None, existing_secrets=None):
        config, secret = super().clean_config(values, existing_config, existing_secrets)
        config["events"] = clean_event_list(config.get("events"))
        cap = config.get("max_per_day")
        if cap is not None and (not isinstance(cap, int) or not 1 <= cap <= 50):
            raise ConfigError("Most texts per phone per day: a whole number from 1 to 50", "max_per_day")
        return config, secret

    def _account(self) -> str:
        return self.env("TWILIO_ACCOUNT_SID")

    def _auth(self) -> tuple[str, str]:
        key, key_secret = self.env("TWILIO_API_KEY"), self.env("TWILIO_API_SECRET")
        if key and key_secret and SID_RE["key"].match(key):
            return key, key_secret
        return self._account(), self.env("TWILIO_AUTH_TOKEN") or ""

    def _sender(self) -> dict:
        service = self.env("TWILIO_MESSAGING_SERVICE_SID")
        if service and SID_RE["service"].match(service):
            return {"MessagingServiceSid": service}
        return {"From": self.env("TWILIO_FROM")}

    # ------------------------------------------------------------------ test
    def test(self) -> TestResult:
        self.require_operator()
        sender = self._sender()
        try:
            with self.ctx.http() as http:
                if "MessagingServiceSid" in sender:
                    r = http.get(f"{MESSAGING_BASE}/v1/Services/{sender['MessagingServiceSid']}", auth=self._auth())
                else:
                    r = http.get(f"{API_BASE}/2010-04-01/Accounts/{self._account()}/IncomingPhoneNumbers.json",
                                 params={"PhoneNumber": sender["From"]}, auth=self._auth())
        except SafeHTTPError as e:
            return TestResult(False, f"Could not reach Twilio: {e}")
        if r.status_code in (401, 403):
            return TestResult(False, "Twilio didn't accept this server's Twilio credentials. Tell the TradeComply "
                                     "operator (the SMS settings on the server need fixing).")
        if r.status_code == 404:
            return TestResult(False, "Twilio doesn't know the server's sending Messaging Service. Tell the "
                                     "TradeComply operator.")
        if r.status_code != 200:
            return TestResult(False, f"Twilio answered HTTP {r.status_code}. Try again later.")
        try:
            body = r.json()
        except ValueError:
            body = {}
        active = sum(1 for rec in load_book(self.ctx.platform, self.ctx.tenant_id)[0]["recipients"]
                     if rec.get("status") == "active")
        details = {"phones_opted_in": active}
        if "MessagingServiceSid" in sender:
            registered = body.get("us_app_to_person_registered") if isinstance(body, dict) else None
            details["a2p_10dlc_registered"] = registered
            a2p = ("The sender is registered for US A2P 10DLC." if registered else
                   "Warning: Twilio says the sender's Messaging Service has no registered US A2P 10DLC campaign, so "
                   "US carriers will block these texts. " + A2P_NOTE)
        else:
            numbers = body.get("incoming_phone_numbers") if isinstance(body, dict) else None
            if not numbers:
                return TestResult(False, "The server's sending number isn't in its Twilio account. Tell the "
                                         "TradeComply operator.")
            if not (numbers[0].get("capabilities") or {}).get("sms"):
                return TestResult(False, "The server's sending number can't send SMS. Tell the TradeComply operator.")
            a2p = "A2P 10DLC registration of a single number can't be checked from here. " + A2P_NOTE
        if not active:
            return TestResult(True, "Twilio is ready, but no phone numbers have opted in yet. " + a2p,
                              account="No phones opted in", details=details)
        return TestResult(True, f"Twilio is ready. {active} phone number(s) opted in. {a2p}",
                          account=f"{active} phone(s) opted in", details=details)

    # ------------------------------------------------------------------ sending
    def send_text(self, http, to: str, body: str) -> dict:
        """One CreateMessage call. Returns {"ok", "sid", "status", "code", "retry", "message"}; never raises for HTTP
        problems."""
        form = {"To": to, "Body": body[:MAX_BODY], **self._sender()}
        url = f"{API_BASE}/2010-04-01/Accounts/{self._account()}/Messages.json"
        try:
            r = http.post(url, data=form, auth=self._auth(), retries=0, follow_redirects=False, max_bytes=256 * 1024)
        except SafeHTTPError as e:
            return {"ok": False, "retry": True, "code": None, "message": f"Could not reach Twilio: {e}"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        if r.status_code in (200, 201) and data.get("sid"):
            return {"ok": True, "sid": str(data["sid"])[:40], "status": str(data.get("status") or "queued")[:20],
                    "segments": data.get("num_segments")}
        code = data.get("code") if isinstance(data.get("code"), int) else None
        if r.status_code in (401, 403):
            return {"ok": False, "retry": True, "code": code, "auth": True,
                    "message": "Twilio didn't accept this server's Twilio credentials"}
        if r.status_code == 429 or code == 20429 or r.status_code >= 500:
            return {"ok": False, "retry": True, "code": code, "message": f"Twilio is busy (HTTP {r.status_code})"}
        text = RECIPIENT_ERRORS.get(code) or ("the person replied STOP" if code == OPTED_OUT_CODE else
                                               f"Twilio refused the text (HTTP {r.status_code}"
                                               + (f", error {code})" if code else ")"))
        return {"ok": False, "retry": False, "code": code, "message": text}

    def _record(self, rid: str, outcome: dict, fingerprint: str | None) -> None:
        def change(book):
            rec = _find(book, rid)
            if not rec:
                return
            now = now_iso()
            if outcome.get("ok"):
                rec.update(last_sent_at=now, last_status=outcome.get("status"), last_error=None,
                           last_sid=outcome.get("sid"))
                day = _ca_day()
                rec["day_count"] = (rec.get("day_count", 0) if rec.get("day") == day else 0) + 1
                rec["day"] = day
                if fingerprint:
                    sent = rec.setdefault("sent", {})
                    sent[fingerprint] = now
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=SENT_KEEP_DAYS)).isoformat()
                    rec["sent"] = {k: v for k, v in sent.items() if v >= cutoff}
            elif outcome.get("code") == OPTED_OUT_CODE:
                rec.update(status="opted_out", opted_out_at=now, opted_out_via="twilio_21610",
                           last_error="Twilio says this person opted out (replied STOP). They can text START to opt "
                                      "back in.")
                _history(rec, "opt_out", "twilio_21610")
            else:
                rec["last_error"] = outcome.get("message")
        modify_book(self.ctx.platform, self.ctx.tenant_id, change)

    def handle_event(self, event: str, payload: dict) -> None:
        config = self.config()
        if not wants(config, event, payload):
            return
        self.require_operator()
        book, _, _ = load_book(self.ctx.platform, self.ctx.tenant_id)
        fingerprint = delivery_id(self.ctx.tenant_id, event, payload)
        cap = config.get("max_per_day") or 10
        day = _ca_day()
        todo = [r for r in book["recipients"] if r.get("status") == "active"
                and fingerprint not in (r.get("sent") or {})
                and not (r.get("day") == day and r.get("day_count", 0) >= cap)]
        if not todo:
            return
        platform = self.ctx.platform
        text = alert_text(event, payload, company_name(platform, self.ctx.tenant_id), platform.settings.app_url)
        retry_reason = None
        with self.ctx.http() as http:
            for rec in todo:
                outcome = self.send_text(http, rec["phone"], text)
                if outcome.get("retry"):
                    retry_reason = outcome["message"]
                    if outcome.get("auth"):
                        self.save(last_error="Twilio didn't accept this server's Twilio credentials. Tell the "
                                             "TradeComply operator.")
                    break                     # busy or misconfigured: the bus retries the rest later
                self._record(rec["id"], outcome, fingerprint)
        if retry_reason:
            raise ConnectorError(retry_reason)


# ---------------------------------------------------------------------------- HTTP API
router = APIRouter(tags=["integrations"])
STAFF_PREFIX = "/api/integrations/twilio_sms/recipients"


class RecipientIn(BaseModel):
    phone: str = Field(min_length=7, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    consent: bool = False
    send_confirmation: bool = True


def _audit(platform, p: Principal, action: str, args: dict, ok: bool = True) -> None:
    try:
        platform.store.audit(p.tenant_id, p.label, f"integrations.{action}", args, None, ok=ok,
                             source="integrations")
    except Exception:      # noqa: BLE001 - the change itself happened
        log.exception("audit write failed")


def _connector(platform, p: Principal) -> TwilioSmsConnector:
    from . import ConnectorContext
    return TwilioSmsConnector(ConnectorContext(platform, p.tenant_id, p.user_id, p.label))


_limiter_guard = threading.Lock()


def _phone_limiter(platform) -> RateLimiter:
    with _limiter_guard:
        lim = getattr(platform, "_sms_phone_limiter", None)
        if lim is None:
            lim = platform._sms_phone_limiter = RateLimiter(limit=MANUAL_TEXTS_PER_PHONE_DAY, window_s=86400)
        return lim


def _rate_limit(platform, p: Principal, phone: str) -> None:
    """Texts staff trigger by hand (the confirmation when a phone is added, test texts) cost the operator money and
    land on someone's phone: at most 30 per company per 10 minutes, and MANUAL_TEXTS_PER_PHONE_DAY per phone per day
    over all companies (adding, removing and re-adding a number can't be used to flood a phone)."""
    limiter, key = platform.integration_test_limiter, f"tenant|{p.tenant_id}"
    phones, pkey = _phone_limiter(platform), "phone|" + hashlib.sha256(phone.encode()).hexdigest()[:24]
    wait = limiter.retry_after(key)
    if wait:
        raise HTTPException(429, "Too many texts in a short time. Try again in a few minutes.",
                            headers={"Retry-After": str(wait)})
    wait = phones.retry_after(pkey)
    if wait:
        raise HTTPException(429, f"{mask_phone(phone)} has been sent {MANUAL_TEXTS_PER_PHONE_DAY} texts from "
                                 "TradeComply in the last 24 hours. Try again later.",
                            headers={"Retry-After": str(wait)})
    limiter.hit(key)
    phones.hit(pkey)


def _book_or_error(platform, tenant_id: str) -> dict:
    try:
        return load_book(platform, tenant_id)[0]
    except ConnectorError as e:
        raise HTTPException(409, str(e))


@router.get(STAFF_PREFIX)
def list_recipients(request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)
    book = _book_or_error(platform, p.tenant_id)
    company = company_name(platform, p.tenant_id) or "your company"
    shown = [recipient_view(r) for r in book["recipients"] if r.get("status") != "removed"]
    return {"recipients": shown, "max_recipients": MAX_RECIPIENTS, "consent_text": CONSENT_TEXT.format(company=company),
            "a2p_10dlc_note": A2P_NOTE, "operator_configured": TwilioSmsConnector.operator_configured(platform.env),
            "connection": _connector(platform, p).status()["status"],
            "inbound_webhook_url": f"{base_url(request)}{INBOUND_PATH}"}


@router.post(STAFF_PREFIX, status_code=201)
def add_recipient(body: RecipientIn, request: Request, p: Principal = Depends(office_up)):
    """Add a phone number with the signed-in user's confirmation that the person agreed to receive alert texts."""
    platform = platform_of(request)
    require_entitled(platform, p)
    if p.user_id is None:
        raise HTTPException(403, "Sign in to the TradeComply app to add a phone number: an API key can't confirm a "
                                 "person's consent.")
    if not body.consent:
        raise HTTPException(422, "Confirm that this person agreed to receive alert texts at this number.")
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "Enter a US or Canadian mobile number, e.g. (415) 555-0100.")
    name = re.sub(r"\s+", " ", body.name).strip()[:100]
    company = company_name(platform, p.tenant_id) or "your company"
    if body.send_confirmation and TwilioSmsConnector.operator_configured(platform.env):
        _rate_limit(platform, p, phone)         # before anything changes: a refused add leaves the list as it was
    consent = {"at": now_iso(), "user_id": p.user_id, "by": p.label, "method": "staff_confirmed",
               "text": CONSENT_TEXT.format(company=company)}

    def change(book):
        rec = next((r for r in book["recipients"] if r.get("phone") == phone), None)
        if rec and rec.get("status") == "active":
            raise HTTPException(409, f"{mask_phone(phone)} is already on the list.")
        if rec and rec.get("status") == "opted_out":
            raise HTTPException(409, "This person opted out by replying STOP. Only they can opt back in, by texting "
                                     "START to the number TradeComply's alerts come from.")
        if not rec and sum(1 for r in book["recipients"] if r.get("status") == "active") >= MAX_RECIPIENTS:
            raise HTTPException(409, f"At most {MAX_RECIPIENTS} phone numbers per company.")
        if not rec:
            rec = {"id": _secrets.token_hex(6), "phone": phone}
            book["recipients"].append(rec)
        rec.update(name=name, status="active", consent=consent, removed_at=None, removed_by=None, last_error=None)
        _history(rec, "opt_in", "staff_confirmed", p.label)
        return dict(rec)
    try:
        rec = modify_book(platform, p.tenant_id, change)
    except ConnectorError as e:
        raise HTTPException(409, str(e))
    _audit(platform, p, "sms_recipient_add", {"recipient_id": rec["id"], "phone_last4": phone[-4:]})
    out = {"recipient": recipient_view(rec), "confirmation": None}
    inst = _connector(platform, p)
    if body.send_confirmation:
        if not inst.operator_configured(platform.env):
            out["confirmation"] = {"sent": False, "message": "Text alerts aren't set up on this server yet, so no "
                                                             "confirmation text was sent."}
        else:
            text = fit_sms(plain(CONFIRMATION_SMS.format(company=company[:60])), "", "", 2)
            with inst.ctx.http() as http:
                outcome = inst.send_text(http, phone, text)
            if not outcome.get("retry"):
                inst._record(rec["id"], outcome, None)
            out["confirmation"] = {"sent": bool(outcome.get("ok")),
                                   "message": "Confirmation text sent." if outcome.get("ok") else outcome["message"]}
    return out


@router.delete(STAFF_PREFIX + "/{rid}")
def remove_recipient(rid: str, request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)

    def change(book):
        rec = _find(book, rid)
        if not rec or rec.get("status") == "removed":
            raise HTTPException(404, "Phone number not found")
        if rec.get("status") == "active":
            rec["status"] = "removed"
        rec.update(removed_at=now_iso(), removed_by=p.label)
        _history(rec, "removed", "staff", p.label)
        return rec.get("phone", "")[-4:]
    try:
        last4 = modify_book(platform, p.tenant_id, change)
    except ConnectorError as e:
        raise HTTPException(409, str(e))
    _audit(platform, p, "sms_recipient_remove", {"recipient_id": rid, "phone_last4": last4})
    return {"ok": True}


@router.post(STAFF_PREFIX + "/{rid}/test")
def test_recipient(rid: str, request: Request, p: Principal = Depends(office_up)):
    platform = platform_of(request)
    require_entitled(platform, p)
    inst = _connector(platform, p)
    try:
        inst.require_operator()
    except NotConfigured as e:
        raise HTTPException(503, str(e))
    rec = _find(_book_or_error(platform, p.tenant_id), rid)
    if not rec or rec.get("status") == "removed":
        raise HTTPException(404, "Phone number not found")
    if rec.get("status") != "active":
        raise HTTPException(409, "This person opted out; TradeComply can't text them.")
    _rate_limit(platform, p, rec["phone"])
    company = company_name(platform, p.tenant_id)
    text = alert_text("test", {"title": "Test text: alerts reach this phone"}, company, None)
    with inst.ctx.http() as http:
        outcome = inst.send_text(http, rec["phone"], text)
    if not outcome.get("retry"):
        inst._record(rid, outcome, None)
    _audit(platform, p, "sms_recipient_test", {"recipient_id": rid, "ok": bool(outcome.get("ok"))},
           ok=bool(outcome.get("ok")))
    return {"ok": bool(outcome.get("ok")), "message": "Test text sent." if outcome.get("ok") else outcome["message"]}


def _candidate_urls(request: Request) -> list[str]:
    path_q = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    platform = platform_of(request)
    bases = [b for b in (platform.settings.app_url, base_url(request)) if b]
    return list(dict.fromkeys(b.rstrip("/") + path_q for b in bases))


@router.post(INBOUND_PATH)
async def inbound(request: Request):
    """Twilio's "a message comes in" webhook. Signature-checked; records STOP/START; answers empty TwiML."""
    platform = platform_of(request)
    env = platform.env
    token, account = (env.get("TWILIO_AUTH_TOKEN") or "").strip(), (env.get("TWILIO_ACCOUNT_SID") or "").strip()
    if not token or not account:
        return Response("Text replies aren't set up on this server.", status_code=503, media_type="text/plain")
    if "application/x-www-form-urlencoded" not in (request.headers.get("content-type") or ""):
        return Response("Unsupported content type", status_code=415, media_type="text/plain")
    raw = await request.body()
    if len(raw) > 64 * 1024:
        return Response("Too large", status_code=413, media_type="text/plain")
    try:
        params = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return Response("Bad request", status_code=400, media_type="text/plain")
    if not valid_signature(token, _candidate_urls(request), params, request.headers.get("x-twilio-signature")):
        log.warning("twilio inbound: signature check failed")
        return Response("Forbidden", status_code=403, media_type="text/plain")
    fields = dict(params)
    if fields.get("AccountSid") != account:
        log.warning("twilio inbound: message for another Twilio account refused")
        return Response("Forbidden", status_code=403, media_type="text/plain")
    action = classify_reply(fields.get("Body"), fields.get("OptOutType"))
    phone = normalize_phone(fields.get("From"))
    if action in ("stop", "start") and phone:
        # The signature has no timestamp, so a captured request stays valid forever: each message (MessageSid) is
        # applied once. Otherwise replaying an old START would opt back in someone who later replied STOP.
        sid = fields.get("MessageSid") or ""
        if not MESSAGE_SID_RE.match(sid):
            log.warning("twilio inbound: reply without a valid MessageSid ignored")
            return Response(EMPTY_TWIML, media_type="text/xml")
        nonce = f"twilio:{sid}"
        store = platform.store
        if not await anyio.to_thread.run_sync(store.claim_nonce, nonce, int(time.time()) + REPLY_DEDUPE_S):
            log.info("twilio inbound: message already applied (a replay or a retry); ignored")
            return Response(EMPTY_TWIML, media_type="text/xml")
        try:
            changed = await anyio.to_thread.run_sync(apply_reply, platform, phone, action)
        except BaseException:
            await anyio.to_thread.run_sync(store.release_nonce, nonce)      # Twilio's retry must still count
            raise
        log.info("twilio inbound: %s reply recorded on %d record(s)", action, changed)
    return Response(EMPTY_TWIML, media_type="text/xml")
