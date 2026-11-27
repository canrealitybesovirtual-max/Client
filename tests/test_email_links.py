"""One-time links by email (app/auth_links.py, app/mailer.py, store migration 8): forgot password, reset, team
invites (emailed and copied), owner-made reset links and email confirmation, on SQLite and PostgreSQL.

No email leaves the test: the outbox backend keeps messages in memory, the SMTP backend talks to a fake smtplib and
the Postmark backend to an httpx.MockTransport. Every response shape below is the vendor's documented one."""
import hashlib
import json
import logging
import re
import smtplib
import ssl
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import auth_links, mailer
from app.mailer import MailConfig, compose, parse_address
from tests.support import (PW, backend, join, json_response, login, make_app, mock_http, oauth_connect,  # noqa: F401
                           signup)

APP_URL = "https://testserver"                 # https APP_URL; the TestClient's own host (connected apps need it)
MAIL = {"MAIL_BACKEND": "outbox", "MAIL_FROM": "TradeComply <no-reply@example.com>"}
TOKEN = re.compile(r"#/(reset|invite|confirm-email)/(tcl_[A-Za-z0-9_-]{43})")
NEW_PW = "a-new-password-1"


# ---------------------------------------------------------------------------- fixtures and helpers
@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=APP_URL)
    a.state.platform.env = dict(MAIL)
    return a


@pytest.fixture
def platform(app):
    return app.state.platform


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def mails(platform, to=None) -> list[dict]:
    return [m for m in platform.mailer.outbox if to is None or m["to"] == to]


def link_in(message: dict, kind: str) -> str:
    found = [t for k, t in TOKEN.findall(message["text"]) if k == kind]
    assert len(found) == 1, message["text"]
    return found[0]


def last_link(platform, to: str, kind: str) -> str:
    return link_in(mails(platform, to)[-1], kind)


def confirm(c, platform, email: str) -> None:
    """Confirm `email` with the newest confirmation link sent to it, signed in on client `c`."""
    r = c.post("/api/auth/email/confirm", json={"token": last_link(platform, email, "confirm-email")})
    assert r.status_code == 200, r.text


def forgot(c, email: str):
    return c.post("/api/auth/forgot", json={"email": email})


def reset(c, token: str, password: str = NEW_PW):
    return c.post("/api/auth/reset", json={"token": token, "new_password": password})


def activity(platform, tid: str, tool: str) -> list[dict]:
    return [r for r in platform.store.list_audit(tid, limit=500) if r["tool"] == tool]


def require_sso(platform, tid: str) -> None:
    platform.store.save_sso_config(tid, issuer="https://login.acme.example", client_id="tc", enforced=True,
                                   allowed_domains=["acme.test"])


def links_of(platform, uid: int) -> list[dict]:
    with platform.store.connect() as con:
        return [dict(r) for r in con.execute("select * from auth_links where user_id = ?", (uid,)).fetchall()]


# ---------------------------------------------------------------------------- the mail module
CFG = MailConfig(backend="outbox", kind="outbox", sender=parse_address("TradeComply <no-reply@example.com>"),
                 reply_to=parse_address("help@example.com"), missing=())


@pytest.mark.parametrize("to", ["a,b@evil.example", "x@y.example\r\nBcc: q@evil.example", "<a@b.example>",
                                "jöe@x.example", "a@b", "two words@x.example", "a@b.example\n", "", "x" * 250 + "@a.co"])
def test_compose_refuses_anything_but_one_plain_address(to, backend):
    with pytest.raises(ValueError):
        compose(CFG, to, "Hi", "Body")
    assert not mailer.valid_recipient(to)


def test_compose_builds_one_safe_message(backend):
    msg, rcpt = compose(CFG, "pat@acme.test", "You're invited to Evil\r\nBcc: x@evil.example Co", "Hello\nthere")
    assert rcpt.addr_spec == "pat@acme.test" and msg.get_all("To") == ["pat@acme.test"]
    assert msg.get_all("Subject") == ["You're invited to Evil Bcc: x@evil.example Co"]     # one line, one header
    assert msg.get_all("Bcc") is None and msg["From"] == "TradeComply <no-reply@example.com>"
    assert msg["Reply-To"] == "help@example.com" and msg["Date"] and msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"].endswith("@example.com>")
    assert "\r\nBcc:" not in msg.as_string() and msg.get_content().strip() == "Hello\nthere"


def test_mail_settings_and_status_name_variables_never_values(platform, backend):
    m = platform.mailer
    platform.env = {}
    st = m.status()
    assert st == {"configured": False, "backend": None, "missing": ["MAIL_FROM", "POSTMARK_SERVER_TOKEN or SMTP_HOST"],
                  "env": mailer.ENV_NAMES}
    secret_env = {"MAIL_FROM": "no-reply@example.com", "SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "u-name",
                  "SMTP_PASSWORD": "s3cret-smtp-pass", "SMTP_SECURITY": "none"}
    platform.env = secret_env
    st = m.status()
    assert st["configured"] is False and st["backend"] == "smtp" and st["missing"] == ["SMTP_SECURITY"]   # never plaintext
    assert "s3cret-smtp-pass" not in repr(st) and "u-name" not in repr(st) and "s3cret" not in repr(m.config())
    platform.env = {**secret_env, "SMTP_SECURITY": "ssl", "SMTP_PORT": "99999", "SMTP_PASSWORD": ""}
    assert m.status()["missing"] == ["SMTP_PORT", "SMTP_PASSWORD"]
    platform.env = {"MAIL_FROM": "not an address", "POSTMARK_SERVER_TOKEN": "pm-token"}
    assert m.status()["missing"] == ["MAIL_FROM"]
    platform.env = {**MAIL, "RENDER": "true"}                       # the outbox is never used on Render
    assert m.configured() is False
    platform.env = {"MAIL_FROM": "a@example.com", "POSTMARK_SERVER_TOKEN": "t", "SMTP_HOST": "smtp.example.com"}
    assert m.config().backend == "postmark"


class FakeSMTP:
    instances: list = []
    starttls_ok = True

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context, self.calls = host, port, timeout, context, []
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        if not FakeSMTP.starttls_ok:
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        self.tls_context = context
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg, to_addrs=None):
        self.sent = (msg, to_addrs)

    def quit(self):
        self.calls.append("quit")

    def close(self):
        pass


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.starttls_ok = [], True
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_smtp_requires_verified_tls_and_sends_to_one_address(platform, fake_smtp, caplog, backend):
    platform.env = {"MAIL_FROM": "TradeComply <no-reply@example.com>", "SMTP_HOST": "smtp.example.com",
                    "SMTP_USERNAME": "smtp-user-x", "SMTP_PASSWORD": "smtp-pass-123"}
    assert platform.mailer.send("pat@acme.test", "Subject", "secret body text") is True
    s = fake_smtp.instances[-1]
    assert (s.host, s.port, s.timeout) == ("smtp.example.com", 587, mailer.SMTP_TIMEOUT_S)
    assert s.calls[:3] == ["ehlo", "starttls", "ehlo"] and ("login", "smtp-user-x") in s.calls
    assert s.calls[-1] == "quit"
    ctx = s.tls_context
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert s.sent[1] == ["pat@acme.test"]
    platform.env = {**platform.env, "SMTP_SECURITY": "ssl"}
    assert platform.mailer.send("pat@acme.test", "Subject", "body") is True
    s = fake_smtp.instances[-1]
    assert s.port == 465 and s.context.verify_mode == ssl.CERT_REQUIRED and "starttls" not in s.calls
    # a server without STARTTLS: nothing is sent in the clear
    platform.env = {**platform.env, "SMTP_SECURITY": "starttls"}
    fake_smtp.starttls_ok = False
    with caplog.at_level(logging.INFO):
        assert platform.mailer.send("pat@acme.test", "Subject", "secret body text") is False
    assert not hasattr(fake_smtp.instances[-1], "sent")
    assert "SMTPNotSupportedError" in caplog.text
    for leak in ("smtp-pass-123", "secret body text", "pat@acme.test", "smtp-user-x"):
        assert leak not in caplog.text


def test_postmark_api_call_and_failures_without_leaks(platform, caplog, backend):
    platform.env = {"MAIL_FROM": "TradeComply <no-reply@example.com>", "POSTMARK_SERVER_TOKEN": "pm-server-token",
                    "MAIL_REPLY_TO": "help@example.com"}
    replies = [json_response(200, {"To": "pat@acme.test", "SubmittedAt": "2026-11-26T12:00:00Z",
                                   "MessageID": "b7bc2f4a-e38e-4336-af7d-e6c392c2f817", "ErrorCode": 0,
                                   "Message": "OK"})]
    seen = mock_http(platform, lambda req: replies[0])
    assert platform.mailer.send("pat@acme.test", "Reset", "https://testserver/#/reset/tcl_x") is True
    req = seen[-1]
    assert str(req.url) == "https://api.postmarkapp.com/email" and req.method == "POST"
    assert req.headers["x-postmark-server-token"] == "pm-server-token" and req.headers["accept"] == "application/json"
    body = json.loads(req.content)
    assert body["To"] == "pat@acme.test" and body["From"] == "TradeComply <no-reply@example.com>"
    assert body["TrackLinks"] == "None" and body["TrackOpens"] is False and body["MessageStream"] == "outbound"
    assert body["ReplyTo"] == "help@example.com" and body["Subject"] == "Reset" and "HtmlBody" not in body
    failures = [json_response(422, {"ErrorCode": 406, "Message": "You tried to send to recipient(s) that have been "
                                                                 "marked as inactive: pat@acme.test"}),
                json_response(401, {"ErrorCode": 10, "Message": "Request does not contain a valid Server token."}),
                json_response(500, {"ErrorCode": 101, "Message": "Internal error"}),
                json_response(200, {"ErrorCode": 300, "Message": "Invalid email request"})]
    for reply in failures:
        replies[0] = reply
        n = len(seen)
        with caplog.at_level(logging.INFO):
            assert platform.mailer.send("pat@acme.test", "Reset", "https://testserver/#/reset/tcl_x") is False
        assert len(seen) == n + 1                     # never retried: Postmark has no idempotency key
    for leak in ("pat@acme.test", "pm-server-token", "#/reset/", "inactive"):
        assert leak not in caplog.text
    assert "ErrorCode 406" in caplog.text and "HTTP 500" in caplog.text


# ---------------------------------------------------------------------------- not configured / no APP_URL
def test_without_email_owners_still_get_copy_links_and_nothing_is_sent(app, platform, owner, backend):
    platform.env = {}
    platform.mailer.outbox.clear()
    assert owner.get("/api/config").json()["email_links"] is False
    r = forgot(TestClient(app), "owner@acme.test")
    assert r.status_code == 200 and r.json()["emailed"] is False and "owner" in r.json()["message"]
    inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()
    assert inv["email_sent"] is False and "Email isn't set up" in inv["message"]
    assert inv["invite_path"].startswith("/#/invite/tcl_") and inv["invite_url"].startswith(APP_URL + "/#/invite/")
    with TestClient(app) as crew:
        r = crew.post("/api/auth/invite/accept", json={"token": inv["invite_path"].rsplit("/", 1)[1],
                                                       "password": PW, "name": "Crew Lead"})
        assert r.status_code == 200 and crew.get("/api/me").json()["user"]["name"] == "Crew Lead"
    st = owner.get("/api/auth/email-status").json()
    assert st["enabled"] is False and "MAIL_FROM" in st["missing"] and st["env"]["smtp"][0] == "SMTP_HOST"
    assert owner.post("/api/auth/email/resend").status_code == 503
    assert owner.post("/api/auth/email-test").status_code == 503
    assert list(platform.mailer.outbox) == []


@pytest.mark.parametrize("url", [None, "http://tc.example.test"])
def test_without_an_https_app_url_no_link_is_emailed(tmp_path, url, backend):
    app = make_app(tmp_path, app_url=url)
    platform = app.state.platform
    platform.env = dict(MAIL)
    with TestClient(app) as o:
        signup(o)
        assert o.get("/api/config").json()["email_links"] is False
        inv = o.post("/api/users", json={"email": "crew@acme.test", "role": "field"},
                     headers={"host": "evil.example", "x-forwarded-host": "evil.example"}).json()
        assert inv["invite_url"] is None and inv["invite_path"].startswith("/#/invite/") and not inv["email_sent"]
        assert forgot(o, "owner@acme.test").json()["emailed"] is False
        assert o.get("/api/auth/email-status").json()["missing"] == ["APP_URL"]
    assert list(platform.mailer.outbox) == []


def test_links_use_app_url_never_the_request_host(app, platform, owner, backend):
    owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"},
               headers={"host": "evil.example", "x-forwarded-host": "evil.example", "x-forwarded-proto": "http"})
    forgot(TestClient(app, base_url="http://evil.example"), "owner@acme.test")
    assert mails(platform)
    for m in mails(platform):
        assert "evil.example" not in m["text"]
        assert all(u.startswith(APP_URL + "/#/") for u in re.findall(r"https?://\S+", m["text"]))


# ---------------------------------------------------------------------------- forgot password
def test_forgot_answers_the_same_for_every_address(app, platform, owner, backend):
    confirm(owner, platform, "owner@acme.test")
    join(TestClient(app), owner, "unconfirmed@acme.test", "office")     # joined by a copied link: not confirmed
    other = TestClient(app)
    tid2 = signup(other, email="pat@bravo.test", company="Bravo Co")["tenant_id"]
    # sso@bravo.test joins through the emailed invite (so the address is confirmed), then SSO becomes required
    inv = other.post("/api/users", json={"email": "sso@bravo.test", "role": "field"}).json()
    with TestClient(app) as s:
        assert s.post("/api/auth/invite/accept", json={"token": last_link(platform, "sso@bravo.test", "invite"),
                                                       "password": PW}).status_code == 200
    assert inv["email_sent"] is True
    require_sso(platform, tid2)
    platform.mailer.outbox.clear()
    anon = TestClient(app)
    answers = []
    for email in ("owner@acme.test", "nobody@acme.test", "unconfirmed@acme.test", "sso@bravo.test", "BAD"):
        r = forgot(anon, email)
        answers.append((r.status_code, r.content, sorted((k, v) for k, v in r.headers.items())))
    assert len(set(map(repr, answers))) == 1 and answers[0][0] == 200
    assert [m["to"] for m in mails(platform)] == ["owner@acme.test", "sso@bravo.test"]
    link_in(mails(platform, "owner@acme.test")[0], "reset")
    pointer = mails(platform, "sso@bravo.test")[0]
    assert "#/reset/" not in pointer["text"] and "single sign-on" in pointer["text"] and "/#/login" in pointer["text"]


def test_forgot_limits_per_address_and_per_network(app, platform, owner, backend):
    confirm(owner, platform, "owner@acme.test")
    platform.mailer.outbox.clear()
    c = TestClient(app)
    first = forgot(c, "owner@acme.test")
    for _ in range(3):
        r = forgot(c, "owner@acme.test")
        assert (r.status_code, r.content) == (first.status_code, first.content)     # the 4th: the same answer
    assert len(mails(platform, "owner@acme.test")) == 3                              # ... and no email
    for k in range(6):
        assert forgot(c, f"x{k}@acme.test").status_code == 200
    r = forgot(c, "owner@acme.test")                                                  # the 11th from this network
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert forgot(TestClient(app), "o@x.example", ).status_code == 429               # same network (TestClient)


# ---------------------------------------------------------------------------- reset
def test_reset_link_flow(app, platform, owner, backend, monkeypatch):
    tid = owner.tenant_id
    confirm(owner, platform, "owner@acme.test")
    other_device = TestClient(app)
    login(other_device, "owner@acme.test")
    grant = oauth_connect(owner)
    inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()   # unrelated person
    for _ in range(5):                                       # a login lockout for this email
        TestClient(app).post("/api/auth/login", json={"email": "owner@acme.test", "password": "wrong-password-1"})
    assert TestClient(app).post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}).status_code == 429
    platform.mailer.outbox.clear()
    forgot(TestClient(app), "owner@acme.test")
    token = last_link(platform, "owner@acme.test", "reset")
    owner_id = owner.get("/api/me").json()["user"]["id"]
    older = auth_links.issue(platform, tid, owner_id, "invite", ttl_s=3600, copy=True)        # another open link
    anon = TestClient(app)
    for _ in range(3):                                       # opening it uses nothing up
        r = anon.post("/api/auth/link", json={"token": token, "purpose": "reset"})
        assert r.status_code == 200 and r.json()["email"] == "owner@acme.test" and r.json()["sso"] is False
    assert anon.post("/api/auth/link", json={"token": token, "purpose": "invite"}).status_code == 404
    assert reset(anon, token, "short").status_code == 422
    r = reset(anon, token)
    assert r.status_code == 200 and r.json() == {"ok": True, "email": "owner@acme.test", "api_keys_revoked": 0}
    assert "tc_session" not in r.headers.get("set-cookie", "")                       # nobody is signed in by it
    assert reset(anon, token).status_code == 404 and reset(anon, token).json()["detail"] == auth_links.GONE
    assert owner.get("/api/me").status_code == 401 and other_device.get("/api/me").status_code == 401
    assert anon.post("/api/auth/link", json={"token": older["copy_path"].rsplit("/", 1)[1],
                                             "purpose": "invite"}).status_code == 404
    login(TestClient(app), "owner@acme.test", NEW_PW)                                # the lockout was cleared
    assert TestClient(app).post("/api/auth/login", json={"email": "owner@acme.test", "password": PW}).status_code == 401
    rows = activity(platform, tid, "auth.password_reset")
    assert rows and rows[0]["args"] == {"user_id": owner_id, "via": "email"} and "tcl_" not in str(rows)
    revoked = activity(platform, tid, "oauth.revoked")
    assert revoked and revoked[0]["args"]["reason"] == "password_reset"
    assert grant["tokens"]["access_token"] and not platform.store.list_oauth_grants(tid)
    notice = mails(platform, "owner@acme.test")[-1]
    assert notice["subject"] == "Your TradeComply password was changed" and "/#/forgot" in notice["text"]
    assert links_of(platform, inv["id"])                                             # other people's links stay
    # expiry: 30 minutes
    forgot(TestClient(app), "owner@acme.test")
    token = last_link(platform, "owner@acme.test", "reset")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 29 * 60)
    assert anon.post("/api/auth/link", json={"token": token, "purpose": "reset"}).status_code == 200
    monkeypatch.setattr(time, "time", lambda: real() + 31 * 60)
    assert reset(anon, token).status_code == 404


def test_a_password_change_ends_outstanding_links(app, platform, owner, backend):
    confirm(owner, platform, "owner@acme.test")
    forgot(TestClient(app), "owner@acme.test")
    token = last_link(platform, "owner@acme.test", "reset")
    assert owner.post("/api/auth/password", json={"current_password": PW, "new_password": NEW_PW}).status_code == 200
    assert mails(platform, "owner@acme.test")[-1]["subject"] == "Your TradeComply password was changed"
    assert reset(TestClient(app), token).status_code == 404


def test_the_same_link_used_twice_at_once_works_once(app, platform, owner, backend):
    confirm(owner, platform, "owner@acme.test")
    forgot(TestClient(app), "owner@acme.test")
    token = last_link(platform, "owner@acme.test", "reset")
    barrier, results = threading.Barrier(2), {}

    def go(pw):
        c = TestClient(app)
        barrier.wait()
        results[pw] = reset(c, token, pw).status_code
    threads = [threading.Thread(target=go, args=(pw,)) for pw in ("first-password-1", "second-password-2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(results.values()) == [200, 404], results
    winner = next(pw for pw, code in results.items() if code == 200)
    for pw in results:
        r = TestClient(app).post("/api/auth/login", json={"email": "owner@acme.test", "password": pw})
        assert r.status_code == (200 if pw == winner else 401)


# ---------------------------------------------------------------------------- invites
def test_invites_emailed_and_copied(app, platform, owner, backend):
    tid = owner.tenant_id
    inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field", "name": "Ana"}).json()
    assert inv["email_sent"] is True and "temporary_password" not in inv
    mail = mails(platform, "crew@acme.test")[-1]
    assert mail["subject"] == "You're invited to join your team on TradeComply" and "7 days" in mail["text"]
    assert "Company name, as its owner entered it: Acme Builders\n" in mail["text"]
    emailed, copied = link_in(mail, "invite"), inv["invite_path"].rsplit("/", 1)[1]
    assert emailed != copied and inv["invite_url"] == APP_URL + inv["invite_path"]
    assert activity(platform, tid, "user.invite")[0]["args"] == {"user_id": inv["id"], "role": "field",
                                                                 "emailed": True}
    users = {u["email"]: u for u in owner.get("/api/users").json()}
    assert users["crew@acme.test"]["last_login"] is None                        # pending
    r = TestClient(app).post("/api/auth/login", json={"email": "crew@acme.test", "password": PW})
    assert r.status_code == 401 and r.json()["detail"] == "Email or password is incorrect"
    anon = TestClient(app)
    peek = anon.post("/api/auth/link", json={"token": copied, "purpose": "invite"}).json()
    assert peek["email"] == "crew@acme.test" and peek["company_name"] == "Acme Builders" and peek["name"] == "Ana"
    with TestClient(app) as crew:
        assert crew.post("/api/auth/invite/accept", json={"token": emailed, "password": "short"}).status_code == 422
        r = crew.post("/api/auth/invite/accept", json={"token": emailed, "password": PW})
        assert r.status_code == 200 and "tc_session" in r.headers.get("set-cookie", "")
        me = crew.get("/api/me").json()["user"]
        assert me["email_confirmed"] is True and me["must_change_password"] is False and me["name"] == "Ana"
    assert anon.post("/api/auth/invite/accept", json={"token": copied, "password": PW}).status_code == 404
    assert activity(platform, tid, "user.joined")[0]["args"]["via"] == "email"
    assert owner.post(f"/api/users/{inv['id']}/invite").status_code == 409           # already joined
    # a copied link joins too, but doesn't confirm the address
    inv2 = owner.post("/api/users", json={"email": "office@acme.test", "role": "office"}).json()
    with TestClient(app) as office:
        assert office.post("/api/auth/invite/accept", json={"token": inv2["invite_path"].rsplit("/", 1)[1],
                                                            "password": PW}).status_code == 200
        office.headers["x-csrf-token"] = office.cookies.get("tc_csrf")
        assert office.get("/api/me").json()["user"]["email_confirmed"] is False
        for path in ("/api/users", f"/api/users/{inv['id']}/invite", f"/api/users/{inv['id']}/reset-link"):
            assert office.post(path, json={"email": "x@acme.test"}).status_code == 403, path
        assert office.get("/api/auth/email-status").status_code == 403


def test_resend_and_cancel_invites(app, platform, owner, backend):
    inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()
    first = [inv["invite_path"].rsplit("/", 1)[1], last_link(platform, "crew@acme.test", "invite")]
    again = owner.post(f"/api/users/{inv['id']}/invite").json()
    assert again["email_sent"] is True and again["invite_path"] != inv["invite_path"]
    anon = TestClient(app)
    for token in first:
        assert anon.post("/api/auth/link", json={"token": token, "purpose": "invite"}).status_code == 404
    assert len(links_of(platform, inv["id"])) == 2
    other = TestClient(app)
    signup(other, email="b@bravo.test", company="Bravo Co")
    assert other.post(f"/api/users/{again['id']}/invite").status_code == 404        # another company's person
    assert other.post(f"/api/users/{again['id']}/reset-link").status_code == 404
    assert owner.delete(f"/api/users/{inv['id']}").status_code == 200                # cancel
    assert anon.post("/api/auth/link", json={"token": again["invite_path"].rsplit("/", 1)[1],
                                             "purpose": "invite"}).status_code == 404
    assert links_of(platform, inv["id"]) == []
    assert owner.post("/api/users/999999/invite").status_code == 404
    assert owner.post("/api/users", json={"email": "a,b@evil.example"}).status_code == 422
    assert owner.post("/api/users", json={"email": "owner@acme.test"}).status_code == 409


def test_invite_emails_are_capped_per_company(app, platform, owner, backend):
    for k in range(platform.mail_tenant_limiter.limit):
        assert owner.post("/api/users", json={"email": f"p{k}@acme.test"}).json()["email_sent"] is True
    r = owner.post("/api/users", json={"email": "one-more@acme.test"}).json()
    assert r["email_sent"] is False and r["invite_path"] and "this hour" in r["message"]
    assert not mails(platform, "one-more@acme.test")


# ---------------------------------------------------------------------------- owner-made reset links
def test_owner_reset_link(app, platform, owner, backend):
    tid = owner.tenant_id
    crew = TestClient(app)
    inv = join(crew, owner, "crew@acme.test", "field")
    pending = owner.post("/api/users", json={"email": "new@acme.test"}).json()
    me = owner.get("/api/me").json()["user"]["id"]
    assert owner.post(f"/api/users/{me}/reset-link").status_code == 422
    assert owner.post(f"/api/users/{pending['id']}/reset-link").status_code == 409
    platform.mailer.outbox.clear()
    r = owner.post(f"/api/users/{inv['id']}/reset-link").json()
    assert r["email"] == "crew@acme.test" and r["reset_url"] == APP_URL + r["reset_path"]
    from datetime import datetime, timezone
    left = datetime.fromisoformat(r["expires_at"]) - datetime.now(timezone.utc)
    assert 23.9 * 3600 < left.total_seconds() <= 24 * 3600
    assert mails(platform) == []                                                     # shown, not emailed
    assert crew.get("/api/me").status_code == 200                                    # nothing changed yet
    token = r["reset_path"].rsplit("/", 1)[1]
    assert reset(TestClient(app), token).status_code == 200
    assert reset(TestClient(app), token).status_code == 404
    assert crew.get("/api/me").status_code == 401
    login(TestClient(app), "crew@acme.test", NEW_PW)
    assert activity(platform, tid, "user.reset_link") and \
        activity(platform, tid, "auth.password_reset")[0]["args"]["via"] == "owner_link"
    # another owner can get one too
    second = TestClient(app)
    join(second, owner, "owner2@acme.test", "owner")
    assert owner.post(f"/api/users/{second.get('/api/me').json()['user']['id']}/reset-link").status_code == 200


# ---------------------------------------------------------------------------- SSO-required companies
def test_sso_required_companies_get_no_password_links_except_owners(app, platform, owner, backend):
    tid = owner.tenant_id
    crew = TestClient(app)
    inv = join(crew, owner, "crew@acme.test", "field")
    forgot(TestClient(app), "owner@acme.test")                     # unconfirmed owner: no email at all
    confirm(owner, platform, "owner@acme.test")
    reset_link = owner.post(f"/api/users/{inv['id']}/reset-link").json()["reset_path"].rsplit("/", 1)[1]
    pending = owner.post("/api/users", json={"email": "new@acme.test"}).json()
    require_sso(platform, tid)
    anon = TestClient(app)
    r = reset(anon, reset_link)
    assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
    assert anon.post("/api/auth/link", json={"token": reset_link, "purpose": "reset"}).json()["sso"] is True
    r = anon.post("/api/auth/invite/accept", json={"token": pending["invite_path"].rsplit("/", 1)[1], "password": PW})
    assert r.status_code == 403
    assert owner.post(f"/api/users/{inv['id']}/reset-link").status_code == 403
    sso_inv = owner.post("/api/users", json={"email": "sso-new@acme.test"}).json()
    assert sso_inv["sso"] is True and sso_inv["invite_path"] is None
    assert "#/invite/" not in mails(platform, "sso-new@acme.test")[-1]["text"]
    assert owner.post("/api/auth/email/change", json={"current_password": PW,
                                                      "new_email": "x@acme.test"}).status_code == 200   # owner
    # owners keep their break-glass password, so their reset still works
    platform.mailer.outbox.clear()
    assert forgot(anon, "owner@acme.test").status_code == 200
    assert reset(anon, last_link(platform, "owner@acme.test", "reset")).status_code == 200


# ---------------------------------------------------------------------------- email confirmation and change
def test_email_confirmation(app, platform, owner, backend, monkeypatch):
    mail = mails(platform, "owner@acme.test")[-1]
    assert mail["subject"] == "Confirm your email for TradeComply" and "24 hours" in mail["text"]
    token = link_in(mail, "confirm-email")
    assert owner.get("/api/me").json()["user"]["email_confirmed"] is False
    assert TestClient(app).post("/api/auth/email/confirm", json={"token": token}).status_code == 401
    stranger = TestClient(app)
    signup(stranger, email="s@bravo.test", company="Bravo Co")
    r = stranger.post("/api/auth/email/confirm", json={"token": token})
    assert r.status_code == 404 and r.json()["detail"] == auth_links.CONFIRM_GONE
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 25 * 3600)
    assert TestClient(app).post("/api/auth/link", json={"token": token, "purpose": "confirm"}).status_code == 404
    monkeypatch.setattr(time, "time", real)
    no_csrf = TestClient(app, cookies=owner.cookies)
    assert no_csrf.post("/api/auth/email/confirm", json={"token": token}).status_code == 403
    r = owner.post("/api/auth/email/confirm", json={"token": token})
    assert r.status_code == 200 and r.json() == {"ok": True, "email": "owner@acme.test", "changed": False}
    assert owner.get("/api/me").json()["user"]["email_confirmed"] is True
    assert owner.post("/api/auth/email/confirm", json={"token": token}).status_code == 404
    assert owner.post("/api/auth/email/resend").json() == {"ok": True, "sent": False, "confirmed": True}
    assert activity(platform, owner.tenant_id, "auth.email_confirmed")


def test_resend_confirmation_is_limited(app, platform, owner, backend):
    for _ in range(platform.mail_user_limiter.limit):
        assert owner.post("/api/auth/email/resend").json()["sent"] is True
    assert owner.post("/api/auth/email/resend").status_code == 429
    tokens = [t for m in mails(platform, "owner@acme.test") for k, t in TOKEN.findall(m["text"])]
    anon = TestClient(app)
    alive = [t for t in tokens if anon.post("/api/auth/link", json={"token": t, "purpose": "confirm"}).status_code == 200]
    assert alive == tokens[-2:]            # each new link replaces all but the one before it (sent a moment ago)


def test_changing_email(app, platform, owner, backend):
    tid = owner.tenant_id
    confirm(owner, platform, "owner@acme.test")
    laptop = TestClient(app)
    login(laptop, "owner@acme.test")
    taken = TestClient(app)
    join(taken, owner, "taken@acme.test", "office")
    change = lambda pw, new: owner.post("/api/auth/email/change", json={"current_password": pw, "new_email": new})  # noqa: E731
    assert change("wrong-password", "new@acme.test").status_code == 403
    assert change(PW, "taken@acme.test").status_code == 409
    assert change(PW, "owner@acme.test").status_code == 422
    assert change(PW, "a,b@evil.example").status_code == 422
    platform.mailer.outbox.clear()
    r = change(PW, "New@Acme.test")
    assert r.status_code == 200 and r.json() == {"ok": True, "sent": True, "sent_to": "new@acme.test"}
    notice = mails(platform, "owner@acme.test")[-1]
    assert notice["subject"] == "Your TradeComply sign-in email is changing" and "new@acme.test" in notice["text"]
    assert "#/" in notice["text"] and not TOKEN.search(notice["text"])          # no link that does anything
    assert owner.get("/api/me").json()["user"]["email"] == "owner@acme.test"     # not yet
    token = last_link(platform, "new@acme.test", "confirm-email")
    r = laptop.post("/api/auth/email/confirm", json={"token": token})                # any session of the person
    assert r.status_code == 200 and r.json() == {"ok": True, "email": "new@acme.test", "changed": True}
    laptop.headers["x-csrf-token"] = laptop.cookies.get("tc_csrf")
    me = laptop.get("/api/me")
    assert me.status_code == 200 and me.json()["user"]["email"] == "new@acme.test"
    assert me.json()["user"]["email_confirmed"] is True
    assert owner.get("/api/me").status_code == 401                               # other devices are signed out
    assert activity(platform, tid, "auth.email_changed")[0]["args"]["to"] == "new@acme.test"
    platform.mailer.outbox.clear()
    forgot(TestClient(app), "owner@acme.test")
    forgot(TestClient(app), "new@acme.test")
    assert [m["to"] for m in mails(platform)] == ["new@acme.test"]
    login(TestClient(app), "new@acme.test")


def test_an_email_change_to_an_address_taken_meanwhile_fails_cleanly(app, platform, owner, backend):
    confirm(owner, platform, "owner@acme.test")
    assert owner.post("/api/auth/email/change", json={"current_password": PW,
                                                      "new_email": "late@acme.test"}).status_code == 200
    token = last_link(platform, "late@acme.test", "confirm-email")
    join(TestClient(app), owner, "late@acme.test", "field")
    r = owner.post("/api/auth/email/confirm", json={"token": token})
    assert r.status_code == 409 and owner.get("/api/me").json()["user"]["email"] == "owner@acme.test"


def test_wrong_passwords_on_email_change_are_counted(app, platform, owner, backend):
    for _ in range(platform.login_limiter.limit):
        owner.post("/api/auth/email/change", json={"current_password": "nope-nope-nope", "new_email": "n@acme.test"})
    r = owner.post("/api/auth/email/change", json={"current_password": PW, "new_email": "n@acme.test"})
    assert r.status_code == 429


# ---------------------------------------------------------------------------- data rights, CSRF, odds and ends
def test_export_and_delete_cover_one_time_links(app, platform, owner, backend):
    inv = owner.post("/api/users", json={"email": "crew@acme.test"}).json()
    data = owner.get("/api/tenant/export").json()
    rows = data["one_time_links"]
    owner_id = owner.get("/api/me").json()["user"]["id"]
    assert {(r["purpose"], r["user_id"], r["sent_to"]) for r in rows} == {
        ("confirm", owner_id, "owner@acme.test"), ("invite", inv["id"], "crew@acme.test"), ("invite", inv["id"], None)}
    assert all("token_hash" not in r and "session_gen" not in r for r in rows) and "tcl_" not in str(data)
    with platform.store.connect() as con:
        hashes = [r[0] for r in con.execute("select token_hash from auth_links").fetchall()]
    assert len(hashes) == 3 and not any(x in str(data) for x in hashes)
    assert {"email_confirmed_at"} <= set(data["team_members"][0])
    assert data["about"]["left_out"] and "one-time link" in data["about"]["left_out"]
    r = owner.request("DELETE", "/api/tenant", json={"confirm_company_name": "Acme Builders", "password": PW})
    assert r.status_code == 200, r.text
    with platform.store.connect() as con:
        assert con.execute("select count(*) from auth_links").fetchone()[0] == 0


def test_link_routes_take_json_only_and_session_routes_need_csrf(app, owner, backend):
    anon = TestClient(app)
    for path in ("/api/auth/forgot", "/api/auth/link", "/api/auth/reset", "/api/auth/invite/accept"):
        r = anon.post(path, content=b"email=owner@acme.test", headers={"content-type": "text/plain"})
        assert r.status_code == 422, path
    no_csrf = TestClient(app, cookies=owner.cookies)
    for path in ("/api/auth/email/confirm", "/api/auth/email/resend", "/api/auth/email/change",
                 "/api/auth/email-test", "/api/users/1/invite", "/api/users/1/reset-link"):
        assert no_csrf.post(path, json={}).status_code == 403, path
    key = owner.post("/api/keys", json={"name": "bot"}).json()["key"]
    bot = TestClient(app, headers={"authorization": f"Bearer {key}"})
    assert bot.post("/api/auth/email/resend").status_code == 403


def test_bad_tokens_and_link_check_limits(app, platform, owner, backend):
    anon = TestClient(app)
    for token in ("", "tcl_short", "x" * 47, "tcl_" + "A" * 43, "../../etc", "tcl_" + "A" * 42 + "!"):
        r = anon.post("/api/auth/link", json={"token": token, "purpose": "reset"})
        assert r.status_code == 404 and r.json()["detail"] == auth_links.GONE, token
    used = 6
    for _ in range(platform.link_limiter.limit - used):
        anon.post("/api/auth/reset", json={"token": "tcl_" + "B" * 43, "new_password": NEW_PW})
    r = anon.post("/api/auth/link", json={"token": "tcl_" + "C" * 43, "purpose": "invite"})
    assert r.status_code == 429 and r.headers["retry-after"]


def test_no_email_sign_in_links(app, backend):
    """Email is never used to sign in (NIST SP 800-63B-4 3.1.3.1): there is no such route."""
    for path in ("/api/auth/signin-link", "/api/auth/magic-link", "/api/auth/signin-link/use"):
        assert TestClient(app).post(path, json={"email": "owner@acme.test"}).status_code in (404, 405)


def test_tokens_are_stored_only_as_hashes(app, platform, owner, backend):
    inv = owner.post("/api/users", json={"email": "crew@acme.test"}).json()
    plain = [inv["invite_path"].rsplit("/", 1)[1], last_link(platform, "crew@acme.test", "invite")]
    with platform.store.connect() as con:
        stored = [dict(r) for r in con.execute("select * from auth_links where user_id = ?",
                                               (inv["id"],)).fetchall()]
    assert len(stored) == 2 and not any(p in repr(stored) for p in plain)
    assert {r["token_hash"] for r in stored} == {hashlib.sha256(p.encode()).hexdigest() for p in plain}
    assert {r["sent_to"] for r in stored} == {None, "crew@acme.test"}


# ---------------------------------------------------------------------------- review fixes (regressions)
def test_links_an_owner_made_end_when_that_owner_is_removed(app, platform, owner, backend):
    """A removed owner's reset and invite links must not outlive them: they could set a member's password later."""
    crew = TestClient(app)
    crew_inv = join(crew, owner, "crew@acme.test", "field")
    rogue = TestClient(app)
    rogue_inv = join(rogue, owner, "rogue@acme.test", "owner")
    token = rogue.post(f"/api/users/{crew_inv['id']}/reset-link").json()["reset_path"].rsplit("/", 1)[1]
    invite = rogue.post("/api/users", json={"email": "later@acme.test", "role": "owner"}).json()
    invite_token = invite["invite_path"].rsplit("/", 1)[1]
    mine = owner.post(f"/api/users/{crew_inv['id']}/reset-link").json()["reset_path"].rsplit("/", 1)[1]
    assert owner.delete(f"/api/users/{rogue_inv['id']}").status_code == 200
    assert reset(TestClient(app), token, "rogue-chosen-pw-1").status_code == 404
    assert TestClient(app).post("/api/auth/invite/accept", json={"token": invite_token, "password": PW}) \
        .status_code == 404
    assert TestClient(app).post("/api/auth/login", json={"email": "crew@acme.test",
                                                         "password": "rogue-chosen-pw-1"}).status_code == 401
    with platform.store.connect() as con:
        assert con.execute("select count(*) from auth_links where created_by = ?",
                           (rogue_inv["id"],)).fetchone()[0] == 0
    assert reset(TestClient(app), mine).status_code == 200                # the remaining owner's link still works


def test_links_an_owner_made_end_when_that_owner_is_demoted(app, platform, owner, backend):
    rogue = TestClient(app)
    rogue_inv = join(rogue, owner, "rogue@acme.test", "owner")
    inv = rogue.post("/api/users", json={"email": "backdoor@acme.test", "role": "owner"}).json()
    token = inv["invite_path"].rsplit("/", 1)[1]
    assert owner.patch(f"/api/users/{rogue_inv['id']}", json={"role": "office"}).status_code == 200
    r = TestClient(app).post("/api/auth/invite/accept", json={"token": token, "password": PW})
    assert r.status_code == 404 and r.json()["detail"] == auth_links.GONE
    # a link left behind (made at the moment of the demotion) is refused at use time too
    other = owner.post("/api/users", json={"email": "other@acme.test", "role": "field"}).json()
    plain, h = auth_links.new_token()
    platform.store.create_auth_links(owner.tenant_id, other["id"], "invite", [(h, None)], 3600,
                                     created_by=rogue_inv["id"])
    assert TestClient(app).post("/api/auth/link", json={"token": plain, "purpose": "invite"}).status_code == 404


def test_made_up_addresses_cant_use_up_password_reset_for_everyone(app, platform, owner, backend):
    """The server-wide cap counts only emails that really go out; spoofed X-Forwarded-For entries can't help."""
    confirm(owner, platform, "owner@acme.test")
    attacker = TestClient(app)
    for k in range(platform.forgot_global_limiter.limit + 5):
        r = attacker.post("/api/auth/forgot", json={"email": f"nobody{k}@nowhere.example"},
                          headers={"x-forwarded-for": f"203.0.113.{k % 250}, 198.51.{k // 250}.1"})
        assert r.status_code == 200, (k, r.status_code)
    before = len(mails(platform, "owner@acme.test"))
    r = TestClient(app).post("/api/auth/forgot", json={"email": "owner@acme.test"},
                             headers={"x-forwarded-for": "192.0.2.77"})
    assert r.status_code == 200 and r.json()["message"] == auth_links.forgot_sent(platform)
    assert len(mails(platform, "owner@acme.test")) == before + 1
    # over the cap of real emails: the same reply, and nothing sent
    for _ in range(platform.forgot_global_limiter.limit):
        platform.forgot_global_limiter.hit("all")
    platform.forgot_email_limiter.reset("owner@acme.test")
    r = TestClient(app).post("/api/auth/forgot", json={"email": "owner@acme.test"},
                             headers={"x-forwarded-for": "192.0.2.78"})
    assert r.status_code == 200 and r.json()["message"] == auth_links.forgot_sent(platform)
    assert len(mails(platform, "owner@acme.test")) == before + 1


def test_a_forged_forwarded_for_cant_block_another_network_from_its_links(app, platform, owner, backend):
    inv = owner.post("/api/users", json={"email": "crew@acme.test", "role": "field"}).json()
    token = inv["invite_path"].rsplit("/", 1)[1]
    office = "192.0.2.10"
    attacker = TestClient(app)
    for _ in range(platform.link_limiter.limit + 1):
        attacker.post("/api/auth/link", json={"token": "tcl_" + "Z" * 43, "purpose": "invite"},
                      headers={"x-forwarded-for": f"{office}, 203.0.113.9"})
    r = attacker.post("/api/auth/link", json={"token": "tcl_" + "Z" * 43, "purpose": "invite"},
                      headers={"x-forwarded-for": f"{office}, 203.0.113.9"})
    assert r.status_code == 429                                           # the attacker's own bucket is full
    for _ in range(platform.forgot_ip_limiter.limit):
        attacker.post("/api/auth/forgot", json={"email": "x@nowhere.example"},
                      headers={"x-forwarded-for": f"{office}, 203.0.113.9"})
    crew = TestClient(app, headers={"x-forwarded-for": office})
    assert crew.post("/api/auth/forgot", json={"email": "crew@acme.test"}).status_code == 200
    for _ in range(platform.link_limiter.limit + 3):                      # good links never use the bucket up
        assert crew.post("/api/auth/link", json={"token": token, "purpose": "invite"}).status_code == 200
    assert crew.post("/api/auth/invite/accept", json={"token": token, "password": PW}).status_code == 200


def test_invite_email_never_carries_links_or_hidden_characters_the_company_typed(app, platform, backend):
    with TestClient(app) as c:
        r = c.post("/api/auth/signup", json={"company_name": "IRS Notice: pay now at http://evil.example/pay ‮",
                                             "email": "a@evil.example", "password": PW, "cslb_license": "123456",
                                             "name": "Call www.evil.example or help@evil.example"})
        assert r.status_code == 200
        c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
        assert c.post("/api/users", json={"email": "victim@corp.example", "role": "field"}).status_code == 200
    m = mails(platform, "victim@corp.example")[-1]
    assert m["subject"] == "You're invited to join your team on TradeComply"
    assert "evil.example" not in m["subject"] + m["text"] and "‮" not in m["text"]
    assert "Company name, as its owner entered it: IRS Notice: pay now at [link removed]\n" in m["text"]
    assert "Added by: Call [link removed] or [link removed]\n" in m["text"]
    assert auth_links._clean("Smith & Sons Co. J.R. Builders, Inc.") == "Smith & Sons Co. J.R. Builders, Inc."
    assert auth_links._clean("pay at evil.\u0440\u0444") == "pay at [link removed]"           # a domain in any script


def test_two_links_of_one_person_used_at_the_same_moment_never_fail_with_500(app, platform, owner, backend):
    """Postgres locked (link, person) together, so the invite email and the owner's copy used at once deadlocked."""
    for k in range(6):
        inv = owner.post("/api/users", json={"email": f"crew{k}@acme.test", "role": "field"}).json()
        tokens = (last_link(platform, f"crew{k}@acme.test", "invite"), inv["invite_path"].rsplit("/", 1)[1])
        barrier, out = threading.Barrier(2), []

        def go(tok, pw):
            c = TestClient(app, raise_server_exceptions=False)
            barrier.wait()
            out.append(c.post("/api/auth/invite/accept", json={"token": tok, "password": pw}).status_code)
        threads = [threading.Thread(target=go, args=(tok, PW + str(i))) for i, tok in enumerate(tokens)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert sorted(out) == [200, 404], out


def test_joining_with_a_copied_invite_link_emails_a_confirmation(app, platform, owner, backend):
    crew = TestClient(app)
    join(crew, owner, "crew@acme.test", "field")                          # the owner's copied link
    assert crew.get("/api/me").json()["user"]["email_confirmed"] is False
    confirm(crew, platform, "crew@acme.test")
    assert crew.get("/api/me").json()["user"]["email_confirmed"] is True
    assert forgot(TestClient(app), "crew@acme.test").status_code == 200
    assert link_in(mails(platform, "crew@acme.test")[-1], "reset")         # so Forgot password works for them
    other = TestClient(app)                                               # by the emailed link: no second email
    owner.post("/api/users", json={"email": "mail@acme.test", "role": "field"})
    r = other.post("/api/auth/invite/accept", json={"token": last_link(platform, "mail@acme.test", "invite"),
                                                    "password": PW})
    assert r.status_code == 200 and [m["subject"] for m in mails(platform, "mail@acme.test")] == [
        "You're invited to join your team on TradeComply"]


def test_sending_a_new_confirmation_keeps_the_sign_up_link_working(app, platform, owner, backend):
    first = last_link(platform, "owner@acme.test", "confirm-email")       # emailed at sign-up
    assert owner.post("/api/auth/email/resend").json()["sent"] is True    # the banner's "Send link"
    r = owner.post("/api/auth/email/confirm", json={"token": first})
    assert r.status_code == 200, r.text


def test_the_founding_owner_has_joined(app, platform, owner, backend):
    """Sign-up signs the founder in, so they are never listed as a pending invite (Team: 'Invited')."""
    with TestClient(app) as co:
        join(co, owner, "co@acme.test", "owner")
        rows = {u["email"]: u for u in co.get("/api/users").json()}
        assert rows["owner@acme.test"]["last_login"]
        assert co.post(f"/api/users/{rows['owner@acme.test']['id']}/reset-link").status_code == 200
        assert co.post(f"/api/users/{rows['owner@acme.test']['id']}/invite").status_code == 409


def test_migration_8_marks_people_from_before_invite_links_as_joined(tmp_path, backend):
    from tests.support import store_for
    st = store_for(tmp_path)
    tenant = st.create_tenant("Old Co", None, None)
    founder = st.create_user(tenant["id"], "founder@old.test", "x", "owner")                 # signed up, no login
    legacy = st.create_user(tenant["id"], "temp@old.test", "x", "field", must_change_password=True)
    with st.connect() as con:
        con.execute("update schema_version set version = 7" if st.backend == "postgres" else "pragma user_version = 7")
    st.close()
    st = store_for(tmp_path)
    rows = {u["email"]: u for u in st.list_users(tenant["id"])}
    assert rows["founder@old.test"]["last_login"] == founder["created"]
    assert rows["temp@old.test"]["last_login"] is None and legacy["last_login"] is None    # still pending
