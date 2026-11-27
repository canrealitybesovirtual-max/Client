"""Outgoing email: one send() for every message (team invites, password resets, email confirmations, notices).

Backends, picked from the operator's environment (platform.env) each time a message is sent:
  postmark  POSTMARK_SERVER_TOKEN set: Postmark's HTTPS API (port 443). Works on Render's free plan, which blocks
            outbound SMTP ports 25, 465 and 587 (port 25 on every plan). Link and open tracking are turned off on
            every message, so links are never rewritten through the provider.
  smtp      SMTP_HOST set: SMTP_SECURITY=starttls (default, port 587) or ssl (port 465). TLS is required and the
            server's certificate and host name are always checked (ssl.create_default_context); never plaintext.
  outbox    MAIL_BACKEND=outbox: kept in memory (platform.mailer.outbox) and, with MAIL_OUTBOX_FILE, appended to that
            file as JSON lines. Tests and local development only; refused when RENDER is set.
MAIL_FROM is required for all of them ("TradeComply <no-reply@your-company.example>"); MAIL_REPLY_TO is optional.
Not configured: send() returns False and the email features stay off (app/auth_links.py).

Messages are built with email.message.EmailMessage under the default policy, which refuses CR/LF in headers, and the
recipient goes through email.headerregistry.Address, so "a,b@x.example" can't become two recipients. send() blocks:
call it from a worker thread. It never raises and never logs a body, a link, a password or a key: failures log the
backend, the recipient's domain and a status or error code."""
import collections
import email.errors
import email.utils
import json
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage

import httpx

log = logging.getLogger("tradecomply.mail")

SMTP_TIMEOUT_S = 15
POSTMARK_URL = "https://api.postmarkapp.com/email"
STREAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
ENV_NAMES = {"common": ["MAIL_FROM", "MAIL_REPLY_TO"],
             "postmark": ["POSTMARK_SERVER_TOKEN", "POSTMARK_MESSAGE_STREAM"],
             "smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD"]}


@dataclass(frozen=True)
class MailConfig:
    backend: str | None                  # the backend to use; None = not configured
    kind: str | None                     # the backend the environment asks for (even if something is missing)
    sender: Address | None
    reply_to: Address | None
    missing: tuple[str, ...]             # names of environment variables to set or fix (never their values)
    smtp: tuple = field(default=(), repr=False)       # (host, port, security, username, password)
    postmark: tuple = field(default=(), repr=False)   # (server token, message stream)


def parse_address(value: str | None) -> Address | None:
    """One plain ASCII address ("Name <a@b.example>" or "a@b.example") as an Address, or None."""
    name, addr = email.utils.parseaddr(str(value or "").strip())
    if not addr or not addr.isascii():
        return None
    try:
        a = Address(display_name=" ".join(name.split())[:100], addr_spec=addr)
    except (ValueError, email.errors.HeaderParseError):
        return None
    return a if a.username and "." in a.domain else None


def recipient(to: str) -> Address:
    """The single recipient address, or ValueError: CR/LF, commas, angle brackets, a non-ASCII address or anything
    that isn't exactly one addr-spec is refused."""
    text = str(to or "")
    if not text.isascii() or len(text) > 254:
        raise ValueError("not a plain email address")
    try:
        a = Address(addr_spec=text)
    except (ValueError, email.errors.HeaderParseError):
        raise ValueError("not a plain email address") from None
    if a.addr_spec != text or not a.username or "." not in a.domain:
        raise ValueError("not a plain email address")
    return a


def valid_recipient(to: str) -> bool:
    try:
        recipient(to)
        return True
    except ValueError:
        return False


def compose(cfg: MailConfig, to: str, subject: str, text: str, html: str | None = None) -> tuple[EmailMessage,
                                                                                                    Address]:
    """The message and its one recipient. ValueError for a bad recipient or header."""
    rcpt = recipient(to)
    msg = EmailMessage()
    try:
        msg["From"] = cfg.sender
        msg["To"] = rcpt
        if cfg.reply_to:
            msg["Reply-To"] = cfg.reply_to
        msg["Subject"] = " ".join(str(subject).split())[:200]     # a company name can hold CR/LF: collapsed
        msg["Date"] = email.utils.formatdate(usegmt=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=cfg.sender.domain)
        msg["Auto-Submitted"] = "auto-generated"
    except email.errors.HeaderParseError:
        raise ValueError("bad header") from None
    msg.set_content(str(text))
    if html:
        msg.add_alternative(str(html), subtype="html")
    return msg, rcpt


class Mailer:
    def __init__(self, platform):
        self.platform = platform
        self.outbox: collections.deque = collections.deque(maxlen=200)
        self._warned = False

    # ------------------------------------------------------------------ configuration
    def config(self) -> MailConfig:
        env = self.platform.env
        get = lambda name: str(env.get(name) or "").strip()        # noqa: E731
        missing = []
        sender = parse_address(get("MAIL_FROM"))
        if sender is None:
            missing.append("MAIL_FROM")
        reply_to = None
        if get("MAIL_REPLY_TO"):
            reply_to = parse_address(get("MAIL_REPLY_TO"))
            if reply_to is None:
                missing.append("MAIL_REPLY_TO")
        smtp, postmark, kind = (), (), None
        if get("POSTMARK_SERVER_TOKEN"):
            kind = "postmark"
            stream = get("POSTMARK_MESSAGE_STREAM") or "outbound"
            if not STREAM_RE.match(stream):
                missing.append("POSTMARK_MESSAGE_STREAM")
            postmark = (get("POSTMARK_SERVER_TOKEN"), stream)
            if get("SMTP_HOST") and not self._warned:
                self._warned = True
                log.warning("mail: both POSTMARK_SERVER_TOKEN and SMTP_HOST are set; using Postmark")
        elif get("SMTP_HOST"):
            kind = "smtp"
            security = (get("SMTP_SECURITY") or "starttls").lower()
            if security not in ("starttls", "ssl"):
                missing.append("SMTP_SECURITY")
            port_text = get("SMTP_PORT")
            port = 465 if security == "ssl" else 587
            if port_text:
                port = int(port_text) if port_text.isdigit() and 1 <= int(port_text) <= 65535 else 0
                if not port:
                    missing.append("SMTP_PORT")
            user, password = get("SMTP_USERNAME"), str(env.get("SMTP_PASSWORD") or "")
            if bool(user) != bool(password):
                missing.append("SMTP_PASSWORD" if user else "SMTP_USERNAME")
            smtp = (get("SMTP_HOST"), port, security, user, password)
        elif get("MAIL_BACKEND").lower() == "outbox" and not get("RENDER"):
            kind = "outbox"
        else:
            missing.append("POSTMARK_SERVER_TOKEN or SMTP_HOST")
        return MailConfig(backend=None if missing else kind, kind=kind, sender=sender, reply_to=reply_to,
                          missing=tuple(missing), smtp=smtp, postmark=postmark)

    def configured(self) -> bool:
        return self.config().backend is not None

    def status(self) -> dict:
        """For Settings > Email: names only, never a value."""
        cfg = self.config()
        return {"configured": cfg.backend is not None, "backend": cfg.kind, "missing": list(cfg.missing),
                "env": ENV_NAMES}

    # ------------------------------------------------------------------ sending
    def send(self, to: str, subject: str, text: str, html: str | None = None) -> bool:
        """Send one message to one address. True if the backend accepted it. Blocking; never raises."""
        cfg = self.config()
        if cfg.backend is None:
            return False
        try:
            msg, rcpt = compose(cfg, to, subject, text, html)
        except ValueError:
            log.warning("mail: not sent: the recipient or a header was not valid")
            return False
        try:
            if cfg.backend == "smtp":
                return self._smtp(cfg, msg, rcpt)
            if cfg.backend == "postmark":
                return self._postmark(cfg, msg, rcpt, text, html)
            return self._outbox(msg, rcpt, text, html)
        except Exception as e:      # noqa: BLE001 - mail must never break the request that sent it
            log.warning("mail: %s send to a %s address failed (%s)", cfg.backend, rcpt.domain[:100], type(e).__name__)
            return False

    @staticmethod
    def _smtp(cfg: MailConfig, msg: EmailMessage, rcpt: Address) -> bool:
        host, port, security, user, password = cfg.smtp
        ctx = ssl.create_default_context()          # smtplib's own default checks no certificate at all
        server = None
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=ctx)
            else:
                server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
                server.ehlo()
                server.starttls(context=ctx)        # SMTPNotSupportedError without STARTTLS: never plaintext
                server.ehlo()
            if user:
                server.login(user, password)
            server.send_message(msg, to_addrs=[rcpt.addr_spec])
            return True
        except (smtplib.SMTPException, OSError) as e:
            # str(e) can quote the server's reply, which may include the user name: the class and code only
            log.warning("mail: smtp send to a %s address failed (%s %s)", rcpt.domain[:100], type(e).__name__,
                        getattr(e, "smtp_code", ""))
            return False
        finally:
            if server is not None:
                try:
                    server.quit()
                except (smtplib.SMTPException, OSError):
                    server.close()

    def _postmark(self, cfg: MailConfig, msg: EmailMessage, rcpt: Address, text: str, html: str | None) -> bool:
        from .safe_http import SafeHTTPError
        token, stream = cfg.postmark
        body = {"From": str(cfg.sender), "To": rcpt.addr_spec, "Subject": str(msg["Subject"]), "TextBody": str(text),
                "MessageStream": stream, "TrackOpens": False, "TrackLinks": "None",
                "Headers": [{"Name": "Auto-Submitted", "Value": "auto-generated"}]}
        if html:
            body["HtmlBody"] = str(html)
        if cfg.reply_to:
            body["ReplyTo"] = str(cfg.reply_to)
        try:
            # No idempotent=True: Postmark has no idempotency key, so a 5xx is not retried (it could send twice).
            # SafeHTTP retries a POST only on 429 or when the connection could not be opened.
            with self.platform.http() as http:
                r = http.post(POSTMARK_URL, headers={"Accept": "application/json",
                                                     "X-Postmark-Server-Token": token}, json=body)
        except (SafeHTTPError, httpx.HTTPError) as e:
            log.warning("mail: postmark send to a %s address failed (%s)", rcpt.domain[:100], type(e).__name__)
            return False
        try:
            data = r.json()
        except ValueError:
            data = None
        code = data.get("ErrorCode") if isinstance(data, dict) else None
        if r.status_code == 200 and code == 0:
            return True
        # Never the response's Message: for an inactive recipient it quotes the address.
        log.warning("mail: postmark send to a %s address failed (HTTP %s, ErrorCode %s)", rcpt.domain[:100],
                    r.status_code, code if isinstance(code, int) else "?")
        return False

    def _outbox(self, msg: EmailMessage, rcpt: Address, text: str, html: str | None) -> bool:
        item = {"to": rcpt.addr_spec, "subject": str(msg["Subject"]), "text": str(text), "html": html,
                "from": str(msg["From"]), "at": time.time()}
        self.outbox.append(item)
        path = str(self.platform.env.get("MAIL_OUTBOX_FILE") or "").strip()
        if path:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(item) + "\n")
        return True
