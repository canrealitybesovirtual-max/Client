"""The web app's side of one-time links by email (app/auth_links.py): Forgot password, the reset page, invite links
(the owner copies one in Settings > Team, the invitee joins), the owner's reset link, the email confirmation banner
and page (signed in and signed out), Settings > Your account and Settings > Email.

Static checks run everywhere. The browser test runs when Node with Playwright and its Chromium is installed (skipped
otherwise) against a real server with APP_URL=https://tc.example.test and the outbox mail backend writing to a file;
links read from that file are opened on the test server's own address. Every screen is checked at 1280 and 375 px
wide: no sideways scrolling, no page errors, no pop-up dialogs. Set TC_UI_SHOTS=<directory> to keep the screenshots
(links-*.png)."""
import json
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

from tests.test_integrations_ui import REPO, _free_port, playwright_env  # noqa: F401  (fixture)

STATIC = os.path.join(REPO, "app", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


JS = _read("app.js")
LINKS = JS[JS.index("// ======================================================================== one-time links"):
           JS.index("// ======================================================================== connect an app")]
SETTINGS_LINKS = JS[JS.index("// Settings > Team: what an invite"):JS.index("routes.settings = async")]


# ---------------------------------------------------------------------------- static checks
def test_link_screens_set_text_only_and_never_pop_up():
    for code in (LINKS, SETTINGS_LINKS):
        assert not re.search(r"innerHTML|insertAdjacentHTML|\bhtml:|outerHTML|document\.write", code)
        assert not re.search(r"\balert\(|\bconfirm\(|\bprompt\(|openDialog|showModal|setInterval|location\.assign",
                             code)
    assert JS.count("location.assign(") == 2                    # unchanged: only the OAuth and SSO hand-offs
    # Cancel invite deletes the person, so it asks first like Remove; someone who signed in is never "Invited"
    assert "if (!confirm(`Cancel the invite for ${u.email}? Their invite link stops working.`)) return;" in JS
    assert "u.last_login ? fmtDateTime(u.last_login) : u.id === me.user.id ? '—'" in JS
    # the token is read from the address and sent only in a POST body, never in a query string
    assert "/api/auth/link', { method: 'POST', body: { token, purpose } }" in LINKS
    assert not re.search(r"\?token=|token=\$\{", JS)
    assert "const PUBLIC = new Set(['login', 'signup', 'forgot']);" in JS and "const OPEN = new Set(['reset', 'invite']);" in JS
    assert "h('a', { href: '#/forgot' }, 'Forgot password?')" in JS


def test_service_worker_bumped_and_never_handles_link_calls():
    sw, index = _read("sw.js"), _read("index.html")
    version = int(re.search(r"const SHELL_CACHE = 'tc-shell-v(\d+)'", sw).group(1))
    assert version >= 6 and f"'/static/v{version}/app.js'" in sw                  # bumped for this release (or later)
    assert f'/static/v{version}/app.js' in index and f'/static/v{version}/styles.css' in index
    assert "if (request.method !== 'GET') return;" in sw                 # every link call is a POST


def test_the_confirm_banner_is_small_and_dismissible():
    banner = JS[JS.index("function emailConfirmBanner()"):JS.index("// ======================================================================== shared UI pieces")]
    assert "['dashboard', 'settings'].includes(route)" in banner and "local.set(`tc.confirmHidden.${u.id}`, '1')" in banner
    assert "state.config.email_links" in banner and "'Not now'" in banner


# ---------------------------------------------------------------------------- browser test (Playwright)
@pytest.fixture
def mail_server(tmp_path, playwright_env):
    """This app in a real uvicorn process with an https APP_URL and the outbox mail backend (a JSON-lines file)."""
    port = _free_port()
    outbox = tmp_path / "outbox.jsonl"
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL", "RENDER")
           and not k.startswith(("SMTP_", "POSTMARK_", "MAIL_"))}
    env.update(DATA_DIR=str(tmp_path / "data"), LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0",
               MCP_REQUIRE_KEY="0", APP_URL="https://tc.example.test", MAIL_BACKEND="outbox",
               MAIL_OUTBOX_FILE=str(outbox), MAIL_FROM="TradeComply <no-reply@example.com>")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                             str(port)], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("server exited: " + proc.stderr.read().decode()[-2000:])
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            pytest.fail("server did not start")
        yield base, str(outbox)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


SMOKE = r"""
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const [base, shots, outboxFile] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], overflow: {}, sizes: {} };
  const opts = { deviceScaleFactor: 1, serviceWorkers: 'block' };
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog ${d.message()}`); d.dismiss(); }); };
  const mails = () => (fs.existsSync(outboxFile) ? fs.readFileSync(outboxFile, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);
  const local = (u) => u.replace('https://tc.example.test', base);         // emailed links name APP_URL
  async function mailLink(to, kind, after = 0) {
    for (let k = 0; k < 50; k++) {                        // the email is sent just after the response
      const re = new RegExp(`https://tc\\.example\\.test/#/${kind}/tcl_[A-Za-z0-9_-]{43}`);
      const m = mails().filter((x) => x.to === to).slice(after).reverse().find((x) => re.test(x.text));
      const found = m && m.text.match(re);
      if (found) return local(found[0]);
      await new Promise((r) => setTimeout(r, 100));
    }
    throw new Error(`no ${kind} email for ${to}`);
  }
  const count = (to) => mails().filter((x) => x.to === to).length;
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
      body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* not JSON */ }
    return { status: r.status, data };
  }, [url, o]);
  const text = (p, sel = 'main') => p.$eval(sel, (el) => el.innerText);

  for (const [tag, viewport] of [['desk', { width: 1280, height: 900 }], ['phone', { width: 375, height: 812 }]]) {
    const o = out.sizes[tag] = {};
    const ownerEmail = `owner-${tag}@example.test`;
    const fieldEmail = `field-${tag}@example.test`;
    const ctx = (name) => browser.newContext({ ...opts, viewport }).then(async (c) => { const p = await c.newPage(); watch(p, `${tag}-${name}`); return [c, p]; });
    // a whole page, or for a long page (Settings) just the section in question
    const bars = (p, on) => p.evaluate((on) => document.querySelectorAll('.topbar, .nav').forEach((e) => { e.style.position = on ? '' : 'static'; }), on);
    const shot = async (p, name, sel) => {
      const file = path.join(shots, `links-${name}-${tag}.png`);
      if (!sel) return p.screenshot({ path: file, fullPage: true });
      await bars(p, false);                   // a section alone: the fixed bars would be drawn over it
      await p.locator(sel).screenshot({ path: file });
      await bars(p, true);
    };
    const check = async (p, name, sel) => { out.overflow[`${name}-${tag}`] = await p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth); await shot(p, name, sel); };
    const ACCOUNT = 'section[aria-labelledby=acct-h]';
    const TEAM = 'section.card:has(> h2:text-is("Team"))';

    // ---- the login page and its "Forgot password?" link; an owner signs up
    const [ownerCtx, op] = await ctx('owner');
    await op.goto(base + '/#/login');
    await op.waitForSelector('main form input[name=email]');
    o.forgotLink = await op.$eval('main .forgot-link a', (a) => a.getAttribute('href'));
    await check(op, 'login');
    o.signup = (await call(op, '/api/auth/signup', { method: 'POST', body: { company_name: `Links Builders ${tag}`, email: ownerEmail, password: 'correct-horse-battery' } })).status;

    // ---- the confirmation banner: small, only on the dashboard and Settings, "Not now" hides it for good
    await op.goto(base + '/#/dashboard');
    await op.waitForSelector('#banner .banner.info');
    o.banner = await text(op, '#banner');
    await check(op, 'confirm-banner');
    await op.click('#banner button:has-text("Not now")');
    await op.reload();
    await op.waitForSelector('.tiles');
    o.bannerAfter = await text(op, '#banner');

    // ---- the confirmation link opened signed out: the login shows in its place, then the Confirm button
    const confirmUrl = await mailLink(ownerEmail, 'confirm-email');
    const [cc, cp] = await ctx('confirm');
    await cp.goto(confirmUrl);
    await cp.waitForSelector('main form input[name=email]');
    o.confirmLogin = { hero: await text(cp, '.auth-hero'), hash: await cp.evaluate(() => location.hash.slice(0, 18)) };
    await check(cp, 'confirm-signed-out');
    await cp.fill('input[name=email]', ownerEmail);
    await cp.fill('input[name=password]', 'correct-horse-battery');
    await cp.click('main button[type=submit]');
    await cp.waitForSelector('main h1:has-text("Confirm your email")');
    o.confirmCard = await text(cp, 'main .card');
    await check(cp, 'confirm-email');
    await cp.click('main button:has-text("Confirm email")');
    await cp.waitForSelector('#acct-h');
    o.confirmed = { hash: await cp.evaluate(() => location.hash), pill: await cp.$eval('section[aria-labelledby=acct-h] .kv .pill', (e) => e.textContent) };
    await cc.close();

    // ---- Settings: Your account (change email) and Email (owners); also how Email looks with nothing set up
    await op.goto(base + '/#/settings');
    await op.reload();                                    // the email was confirmed in another browser
    await op.waitForSelector('#email-h');
    await op.waitForSelector('#email .pill');
    o.emailSection = await text(op, '#email');
    await op.click('section[aria-labelledby=acct-h] summary:has-text("Change email")');
    await check(op, 'settings-account', ACCOUNT);
    await shot(op, 'settings-email', '#email');
    await op.route('**/api/auth/email-status', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      configured: false, backend: null, missing: ['MAIL_FROM', 'POSTMARK_SERVER_TOKEN or SMTP_HOST', 'APP_URL'], links: false, enabled: false,
      env: { common: ['MAIL_FROM', 'MAIL_REPLY_TO'], postmark: ['POSTMARK_SERVER_TOKEN', 'POSTMARK_MESSAGE_STREAM'], smtp: ['SMTP_HOST', 'SMTP_PORT', 'SMTP_SECURITY', 'SMTP_USERNAME', 'SMTP_PASSWORD'] } }) }));
    await op.reload();
    await op.waitForSelector('#email .pill');
    o.emailOff = await text(op, '#email');
    await check(op, 'settings-email-off', '#email');
    await op.unroute('**/api/auth/email-status');

    // ---- Team: invite someone; the owner gets the link to copy (and it was emailed)
    await op.reload();
    await op.waitForSelector('#email .pill');
    await op.fill('form input[name=email][type=email]:not([autocomplete])', fieldEmail);
    await op.fill('form input[name=name][autocomplete=off]', 'Field Lead');
    await op.click('button:has-text("Invite")');
    await op.waitForSelector('.secret .copy-box pre');
    o.inviteBox = await text(op, '.secret');
    const inviteUrl = local(await op.$eval('.secret .copy-box pre', (e) => e.textContent));
    o.invitePill = await op.$eval('table.data', (t) => t.innerText.includes('Invited'));
    await check(op, 'team-invite', TEAM);
    o.inviteEmailed = count(fieldEmail);

    // ---- the invitee opens the copied link, types two different passwords, then joins and lands on Welcome
    const [ic, ip] = await ctx('invitee');
    await ip.goto(inviteUrl);
    await ip.waitForSelector('main h1:has-text("Join")');
    o.inviteCard = await text(ip, 'main .card');
    o.inviteName = await ip.$eval('input[name=name]', (e) => e.value);
    await check(ip, 'invite');
    await ip.fill('input[name=pw]', 'field-horse-battery');
    await ip.fill('input[name=pw_again]', 'field-horse-batteryX');
    await ip.click('main button[type=submit]');
    await ip.waitForSelector('main .banner.error');
    o.inviteMismatch = await text(ip, 'main .banner.error');
    await ip.fill('input[name=pw_again]', 'field-horse-battery');
    await ip.click('main button[type=submit]');
    await ip.waitForSelector('main h1:has-text("Welcome")');
    o.joined = { hash: await ip.evaluate(() => location.hash), company: await text(ip, '#company') };
    await check(ip, 'invite-joined');
    await ip.goto(base + '/#/settings');
    await ip.waitForSelector('#acct-h');
    o.fieldSeesEmail = await ip.$('#email') !== null;
    o.fieldSeesTeam = (await text(ip)).includes('Invite someone');
    await ic.close();
    // joined with the copied link: a confirmation email follows (the emailed invite would have confirmed it)
    await mailLink(fieldEmail, 'confirm-email');
    o.joinConfirmSent = true;
    // the emailed copy of the invite no longer works
    const emailedInvite = await mailLink(fieldEmail, 'invite');
    const [gc, gp] = await ctx('gone');
    await gp.goto(emailedInvite);
    await gp.waitForSelector('main h1:has-text("expired")');
    o.inviteGone = await text(gp, 'main .card');
    await check(gp, 'link-expired');
    await gc.close();

    // ---- the owner makes a reset link for the field user; it is opened on another phone
    await op.reload();                                    // the team list again: they joined
    await op.waitForSelector('button:has-text("Reset link")');
    await op.click('button:has-text("Reset link")');
    await op.waitForSelector('.secret .copy-box pre');
    o.resetBox = await text(op, '.secret');
    const resetUrl = local(await op.$eval('.secret .copy-box pre', (e) => e.textContent));
    await check(op, 'team-reset-link', TEAM);
    const [rc, rp] = await ctx('reset');
    await rp.goto(resetUrl);
    await rp.waitForSelector('main h1:has-text("Set a new password")');
    o.resetCard = await text(rp, 'main .card');
    o.resetFields = await rp.$$eval('main input[type=password]', (els) => els.length);
    await check(rp, 'reset');
    await rp.fill('input[name=pw]', 'short');
    await rp.fill('input[name=pw_again]', 'short');
    await rp.click('main button[type=submit]');
    await rp.waitForSelector('main .banner.error');
    o.resetShort = await text(rp, 'main .banner.error');
    await rp.fill('input[name=pw]', 'field-new-password-1');
    await rp.fill('input[name=pw_again]', 'field-new-password-1');
    await rp.click('main button[type=submit]');
    await rp.waitForSelector('main h1:has-text("Password changed")');
    o.resetDone = await text(rp, 'main .card');
    await check(rp, 'reset-done');
    await rp.click('main a:has-text("Log in")');
    await rp.waitForSelector('main form input[name=email]');
    o.afterReset = (await call(rp, '/api/auth/login', { method: 'POST', body: { email: fieldEmail, password: 'field-new-password-1' } })).status;
    await rc.close();

    // ---- Forgot password, signed out: the same answer for anyone, then the emailed link
    const [fc, fp] = await ctx('forgot');
    await fp.goto(base + '/#/forgot');
    await fp.waitForSelector('main form input[name=email]');
    await check(fp, 'forgot');
    const before = count(ownerEmail);
    await fp.fill('input[name=email]', ownerEmail);
    await fp.click('main button[type=submit]');
    await fp.waitForSelector('main .banner.info');
    o.forgotSent = await text(fp, 'main .card');
    await check(fp, 'forgot-sent');
    const forgotUrl = await mailLink(ownerEmail, 'reset', before);
    await fp.goto(forgotUrl);
    await fp.waitForSelector('main h1:has-text("Set a new password")');
    await fp.fill('input[name=pw]', 'owner-new-password-1');
    await fp.fill('input[name=pw_again]', 'owner-new-password-1');
    await fp.click('main button[type=submit]');
    await fp.waitForSelector('main h1:has-text("Password changed")');
    o.ownerSignedOut = (await call(op, '/api/me')).status;       // the owner's other session ended
    o.passwordNotice = mails().filter((m) => m.to === ownerEmail).pop().subject;
    await fc.close();
    await ownerCtx.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_email_link_screens_in_a_real_browser(tmp_path, playwright_env, mail_server):
    node, env = playwright_env
    base, outbox = mail_server
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "links.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, shots, outbox], env=env, capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    assert not any(out["overflow"].values()), out["overflow"]
    for tag, o in out["sizes"].items():
        assert o["forgotLink"] == "#/forgot" and o["signup"] == 200, tag
        assert "We emailed a link to confirm" in o["banner"] and "Send a new link" in o["banner"]   # sent at sign-up
        assert o["bannerAfter"] == ""
        assert "Log in to confirm your email" in o["confirmLogin"]["hero"] and o["confirmLogin"]["hash"] == "#/confirm-email/tc"
        assert f"owner-{tag}@example.test" in o["confirmCard"]
        assert o["confirmed"] == {"hash": "#/settings", "pill": "Confirmed"}
        assert "Set up" in o["emailSection"] and "test outbox" in o["emailSection"]
        assert "Not set up" in o["emailOff"] and "MAIL_FROM" in o["emailOff"] and "SMTP_PASSWORD" in o["emailOff"]
        assert "Render's free plan blocks email (SMTP) ports" in o["emailOff"]
        assert "Invite emailed." in o["inviteBox"] and "Works once" in o["inviteBox"] and o["invitePill"]
        assert o["inviteEmailed"] == 1
        assert f"Join Links Builders {tag} on TradeComply" in o["inviteCard"] and o["inviteName"] == "Field Lead"
        assert "don't match" in o["inviteMismatch"]
        assert o["joined"]["hash"] == "#/welcome" and o["joined"]["company"] == f"Links Builders {tag}"
        assert o["joinConfirmSent"] is True
        assert o["fieldSeesEmail"] is False and o["fieldSeesTeam"] is False
        assert "expired or was already used" in o["inviteGone"]
        assert f"Password reset link for field-{tag}@example.test" in o["resetBox"] and "Send it to them yourself" in o["resetBox"]
        assert f"field-{tag}@example.test" in o["resetCard"] and o["resetFields"] == 2
        assert "at least 10 characters" in o["resetShort"].lower()
        assert "Password changed" in o["resetDone"] and o["afterReset"] == 200
        assert "If that email has a TradeComply account" in o["forgotSent"]
        assert o["ownerSignedOut"] == 401 and o["passwordNotice"] == "Your TradeComply password was changed"
