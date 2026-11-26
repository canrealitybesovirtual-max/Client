"""The web app's side of "Sign in with TradeComply" (app/oauth.py): the #/connect consent route, login-then-return
(password, SSO, a temporary password, another account), Settings > Connected apps with Disconnect, the "Add
TradeComply to Claude" steps on the Integrations page and in Settings > AI assistant, and the service worker never
handling /oauth, /.well-known or /api/oauth.

Static checks run everywhere. The browser test runs when Node with Playwright and its Chromium is installed (skipped
otherwise) and drives the real server, the way Claude does: register a client, open /oauth/authorize, log in, approve
on the consent card, land on the app's redirect_uri (https://claude.ai/... answered by a Playwright route, no network)
with code, state and iss, exchange the code at /oauth/token and use the token on /mcp; then the deny path, the
expired and already-used request, the temporary-password and SSO paths, and Disconnect, at 1280 and 375 px wide. Set
TC_UI_SHOTS=<directory> to keep the screenshots (oauth-*.png, deviceScaleFactor 1)."""
import json
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.test_integrations_ui import live_server, playwright_env  # noqa: F401  (fixtures)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "app", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


JS = _read("app.js")
CONNECT = JS[JS.index("// ======================================================================== connect an app"):
             JS.index("// ======================================================================== license + alerts")]


@pytest.fixture
def client(tmp_path):
    from app.config import Settings
    from app.main import create_app
    app = create_app(Settings.from_env(data_dir=str(tmp_path), database_url=None, events_background=False))
    with TestClient(app) as c:
        yield c
    app.state.platform.store.close()


# ---------------------------------------------------------------------------- static checks
def test_served_assets_wire_up_the_consent_and_connected_apps(client):
    js = client.get("/static/app.js").text
    assert "routes.connect = async" in js and "connect: 'Connect an app'" in js
    assert "/api/oauth/requests/${id}" in js and "/api/oauth/connections" in js and "/api/auth/sso/start" in js
    assert "sections.push(connectedAppsSection());\n    if (can.write()) {" in js        # every role, not only office
    assert "claudeCard(cat.tradecomply.mcp_url)" in js and "claudeConnector(url)" in js
    css = client.get("/static/styles.css").text
    assert ".connect-host" in css and ".connect-actions" in css and "ol.steps" in css
    sw = client.get("/sw.js").text
    assert int(re.search(r"const SHELL_CACHE = 'tc-shell-v(\d+)'", sw).group(1)) >= 5     # bumped for this release
    skip = next(line for line in sw.splitlines() if "'/oauth/'" in line)
    assert all(p in skip for p in ("'/oauth/'", "'/.well-known/'", "'/api/oauth/'")) and skip.strip().endswith(
        "return;   // sign-in: network only")
    assert sw.index("'/api/oauth/'") < sw.index("event.respondWith(networkFirstApi(request))")


def test_consent_code_sets_text_only_and_never_pops_up():
    assert not re.search(r"innerHTML|insertAdjacentHTML|\bhtml:|outerHTML|document\.write", CONNECT)
    assert not re.search(r"\balert\(|\bconfirm\(|\bprompt\(|openDialog|showModal|setInterval|sessionStorage", CONNECT)
    # the only navigation off the site is to the app's own redirect_uri, after Approve, and only to http(s)
    assert JS.count("location.assign(") == 2 and "if (to) location.assign(to);" in CONNECT
    assert "const to = webUrl(r.redirect_to);" in CONNECT and "if (!webUrl(r.authorize_url))" in JS
    # Deny never navigates: it offers a link back (OAuth 2.1 section 7.12.2)
    deny = CONNECT[CONNECT.index("} else {\n        show("):CONNECT.index("const other =")]
    assert "location" not in deny and "Back to ${req.redirect_host}" in CONNECT
    # the request id is kept only through the guarded `local` helper, validated, for 10 minutes
    assert "local.set(CONNECT_KEY" in CONNECT and "10 * 60 * 1000" in CONNECT and "REQUEST_RE.test(id" in CONNECT


def test_consent_leads_with_the_address_not_the_name():
    card = CONNECT[CONNECT.index("return show(h('div', { class: 'card connect' }"):CONNECT.index("function passwordFirst")]
    assert card.index("req.redirect_host") < card.index("req.client_name")
    assert "req.local_only ?" in card and "Approve only if you started connecting this app yourself" in card
    for phrase in ("guarantee", "recommended", "trusted", "verified app", "safe to approve"):
        assert phrase not in card.lower(), phrase


# ---------------------------------------------------------------------------- browser test (Playwright)
SMOKE = r"""
const { chromium } = require('playwright');
const crypto = require('crypto');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [] };
  const CB = 'https://claude.ai/api/mcp/auth_callback';
  const LOOP = 'http://127.0.0.1:9/callback';            // port 9 (discard): nothing listens; the route answers
  const opts = { deviceScaleFactor: 1, serviceWorkers: 'block' };
  const shot = (p, name, o = {}) => p.screenshot({ path: path.join(shots, `oauth-${name}.png`), ...o });
  const overflow = (p) => p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  const small = (p) => p.$$eval('main button, main a.btn, main summary', (els) => els.filter((e) => e.offsetParent !== null)
    .filter((e) => e.getBoundingClientRect().height < 44).map((e) => e.textContent.trim()));
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog ${d.message()}`); d.dismiss(); }); };
  const pkce = () => { const v = crypto.randomBytes(48).toString('base64url'); return [v, crypto.createHash('sha256').update(v).digest('base64url')]; };
  const form = async (p, body) => { const r = await fetch(base + p, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams(body) });
    return { status: r.status, data: await r.json().catch(() => null) }; };
  const register = async (name, uri) => (await (await fetch(base + '/oauth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_name: name, redirect_uris: [uri], token_endpoint_auth_method: 'none' }) })).json());
  const authorizeUrl = (client, challenge, state, uri = CB) => `${base}/oauth/authorize?` + new URLSearchParams({ response_type: 'code', client_id: client.client_id,
    redirect_uri: uri, state, code_challenge: challenge, code_challenge_method: 'S256', scope: 'tradecomply', resource: base + '/mcp' });
  const mcp = (token) => fetch(base + '/mcp', { method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'ui-test', version: '1' } } }) }).then((r) => r.status);
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
      body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* not JSON */ }
    return { status: r.status, data };
  }, [url, o]);
  const callback = (route) => route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>The app</title><p>Back in the app.</p>' });
  const consentState = (p) => p.evaluate(() => {
    const card = document.querySelector('main .card');
    return { title: document.title, hash: location.hash, h1: card.querySelector('h1').textContent, host: (card.querySelector('.connect-host') || {}).textContent,
      name: (card.querySelector('.connect-name') || {}).textContent, text: card.innerText, focus: document.activeElement && document.activeElement.tagName,
      warns: [...card.querySelectorAll('.banner.warn')].map((b) => b.textContent), buttons: [...card.querySelectorAll('button')].map((b) => b.textContent),
      bannerSlot: document.querySelector('#banner').innerText, nav: !document.querySelector('#nav').hidden };
  });

  // ---- an owner, an office user with a temporary password, and a Claude-like client
  const setupCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const sp = await setupCtx.newPage();
  watch(sp, 'setup');
  await sp.goto(base + '/#/login');
  out.signup = (await call(sp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Connect Builders Inc.', email: 'owner@example.test', password: 'correct-horse-battery' } })).status;
  const invite = await call(sp, '/api/users', { method: 'POST', body: { email: 'office@example.test', role: 'office' } });
  out.invite = invite.status;
  await setupCtx.close();
  const claude = await register('Claude', CB);

  // ---- desktop: signed out -> /oauth/authorize -> the login in place of the consent -> log in -> consent -> Approve
  const desk = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  await desk.route('https://claude.ai/**', callback);
  await desk.route('http://127.0.0.1:9/**', callback);
  const page = await desk.newPage();
  watch(page, 'desk');
  const [v1, c1] = pkce();
  await page.goto(authorizeUrl(claude, c1, 'state-desk'));
  await page.waitForSelector('main form input[name=email]');
  out.login = await page.evaluate(() => ({ hash: location.hash, lead: document.querySelector('.auth-hero p').textContent,
    headings: [...document.querySelectorAll('main h2')].map((x) => x.textContent), sso: [...document.querySelectorAll('main button')].map((b) => b.textContent) }));
  const requestId = new URLSearchParams(out.login.hash.split('?')[1]).get('request');
  await shot(page, 'login');
  await page.fill('input[name=email]', 'owner@example.test');
  await page.fill('input[name=password]', 'correct-horse-battery');
  await page.click('main button[type=submit]');
  await page.waitForSelector('.connect-host');
  out.consent = await consentState(page);
  out.consentAria = await page.locator('main .card').ariaSnapshot();
  out.deskSmall = await small(page);
  await shot(page, 'consent');
  await page.focus('main button:has-text("Approve")');                  // keyboard only
  await Promise.all([page.waitForURL(/^https:\/\/claude\.ai\//), page.keyboard.press('Enter')]);
  const back = new URL(page.url());
  out.approved = { at: back.origin + back.pathname, state: back.searchParams.get('state'), iss: back.searchParams.get('iss'), code: back.searchParams.get('code') || '' };
  const tok = await form('/oauth/token', { grant_type: 'authorization_code', code: out.approved.code, code_verifier: v1, client_id: claude.client_id,
    redirect_uri: CB, resource: base + '/mcp' });
  out.token = { status: tok.status, type: tok.data.token_type, access: String(tok.data.access_token).slice(0, 4), refresh: String(tok.data.refresh_token).slice(0, 4),
    expires_in: tok.data.expires_in, scope: tok.data.scope };
  out.mcpWithToken = await mcp(tok.data.access_token);
  out.mcpWithBadToken = await mcp('tco_not-a-real-token');

  // ---- the same request again: already used
  await page.goto(`${base}/#/connect?request=${requestId}`);
  await page.waitForSelector('main h1:has-text("expired")');
  out.used = await page.evaluate(() => ({ h1: document.querySelector('main h1').textContent, text: document.querySelector('main .card').innerText }));
  // ---- an unknown request and a malformed one
  await page.goto(`${base}/#/connect?request=${'A'.repeat(43)}`);
  await page.waitForSelector('main h1:has-text("expired")');
  out.unknown = await page.textContent('main .card p');
  await shot(page, 'expired');
  await page.goto(`${base}/#/connect?request=<b>x</b>`);
  await page.waitForSelector('main h1:has-text("expired")');
  out.malformed = await page.evaluate(() => ({ text: document.querySelector('main .card').innerText, b: document.querySelectorAll('main b').length }));
  // ---- a request started somewhere else (here: outside any browser) can't be approved in this one
  const [, c7] = pkce();
  const elsewhere = await fetch(authorizeUrl(claude, c7, 'state-elsewhere'), { redirect: 'manual' });
  const elsewhereId = new URLSearchParams((elsewhere.headers.get('location') || '').split('?')[1]).get('request');
  await page.goto(`${base}/#/connect?request=${elsewhereId}`);
  await page.waitForSelector('main h1:has-text("Start again")');
  out.elsewhere = await page.evaluate(() => ({ text: document.querySelector('main .card').innerText, buttons: document.querySelectorAll('main button').length }));

  // ---- deny, from an app on this computer (loopback redirect): a warning, then only a link back
  const local = await register('Local Tool <img src=x onerror=alert(1)>', LOOP);
  const [, c2] = pkce();
  await page.goto(authorizeUrl(local, c2, 'state-deny', LOOP));
  await page.waitForSelector('.connect-host');
  out.localConsent = await consentState(page);
  out.injected = await page.$$eval('main img', (els) => els.length);
  await page.click('main button:has-text("Deny")');
  await page.waitForSelector('main h1:has-text("Not connected")');
  out.denied = await page.evaluate(() => { const a = [...document.querySelectorAll('main a.btn')].find((x) => x.textContent.startsWith('Back to'));
    return { url: location.href, text: document.querySelector('main .card').innerText, link: a && a.href, linkText: a && a.textContent, focus: document.activeElement && document.activeElement.tagName }; });
  await shot(page, 'denied');

  // ---- Settings: Connected apps lists the connection; Disconnect ends the tokens at once
  await page.goto(base + '/#/settings');
  await page.waitForSelector('#connected-apps table');
  out.apps = await page.evaluate(() => ({ heads: [...document.querySelectorAll('#connected-apps th')].map((x) => x.textContent),
    rows: [...document.querySelectorAll('#connected-apps tbody tr')].map((r) => r.innerText), jump: [...document.querySelectorAll('nav.jump a')].map((a) => a.textContent) }));
  await page.$eval('#connected-apps', (el) => el.scrollIntoView());
  await shot(page, 'settings-apps');
  await Promise.all([page.waitForResponse((r) => /\/api\/oauth\/connections\/\d+$/.test(r.url()) && r.request().method() === 'DELETE'),
    page.click('#connected-apps button:has-text("Disconnect")')]);
  await page.waitForSelector('#connected-apps p:has-text("No apps are connected.")');
  out.afterDisconnect = { toast: await page.textContent('#toast'), focus: await page.evaluate(() => document.activeElement.id) };
  out.mcpAfterDisconnect = await mcp(tok.data.access_token);
  out.refreshAfterDisconnect = (await form('/oauth/token', { grant_type: 'refresh_token', refresh_token: tok.data.refresh_token, client_id: claude.client_id })).data.error;
  out.aiSection = await page.evaluate(() => { const d = document.querySelector('#claude-connector'); return d && { open: d.open, summary: d.querySelector('summary').textContent,
    url: d.querySelector('pre').textContent, steps: d.querySelectorAll('ol.steps li').length }; });

  // ---- Integrations: "Add TradeComply to Claude" with this server's own address
  await page.goto(base + '/#/integrations');
  await page.waitForSelector('#int-claude');
  out.intClaude = await page.evaluate(() => { const s = document.querySelector('#int-claude');
    return { before: !!(s.compareDocumentPosition(document.querySelector('.int-filters')) & Node.DOCUMENT_POSITION_FOLLOWING), url: s.querySelector('ol.steps pre').textContent,
      steps: [...s.querySelectorAll('ol.steps > li')].map((li) => li.innerText.split('\n')[0]), warns: [...s.querySelectorAll('.banner')].map((b) => b.textContent),
      more: [...s.querySelectorAll('details > summary')].map((x) => x.textContent) }; });
  await page.$eval('#int-claude', (el) => el.scrollIntoView());
  await shot(page, 'integrations-claude');
  await desk.close();

  // ---- phone: a temporary password first, then the consent, then Approve
  const phone = await browser.newContext({ ...opts, viewport: { width: 375, height: 812 } });
  await phone.route('https://claude.ai/**', callback);
  const mp = await phone.newPage();
  watch(mp, 'phone');
  const [v3, c3] = pkce();
  await mp.goto(authorizeUrl(claude, c3, 'state-phone'));
  await mp.waitForSelector('main form input[name=email]');
  out.phone = { login: await overflow(mp) };
  await shot(mp, 'login-phone');
  await mp.fill('input[name=email]', 'office@example.test');
  await mp.fill('input[name=password]', invite.data.temporary_password);
  await mp.click('main button[type=submit]');
  await mp.waitForSelector('main h1:has-text("Set your own password first")');
  out.pwFirst = await mp.evaluate(() => ({ hash: location.hash, banner: document.querySelector('#banner').innerText, text: document.querySelector('main .card').innerText }));
  out.phone.password = await overflow(mp);
  out.phoneSmallPw = await small(mp);
  await shot(mp, 'password-first-phone');
  await mp.fill('input[name=current_password]', invite.data.temporary_password);
  await mp.fill('input[name=new_password]', 'office-horse-battery');
  await mp.click('main button[type=submit]');
  await mp.waitForSelector('.connect-host');
  out.phoneConsent = await consentState(mp);
  out.phone.consent = await overflow(mp);
  out.phoneSmall = await small(mp);
  out.phoneButtons = await mp.$$eval('.connect-actions .btn', (bs) => bs.map((b) => { const r = b.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
  await shot(mp, 'consent-phone');
  await mp.$eval('.connect-actions', (el) => el.scrollIntoView({ block: 'center' }));
  await shot(mp, 'consent-phone-actions');
  await Promise.all([mp.waitForURL(/^https:\/\/claude\.ai\//), mp.click('main button:has-text("Approve")')]);
  const pb = new URL(mp.url());
  out.phoneToken = (await form('/oauth/token', { grant_type: 'authorization_code', code: pb.searchParams.get('code') || '', code_verifier: v3, client_id: claude.client_id })).status;
  await mp.goto(base + '/#/settings');
  await mp.waitForSelector('#connected-apps table');
  out.phone.settings = await overflow(mp);
  out.phoneApps = await mp.$$eval('#connected-apps tbody tr', (rs) => rs.map((r) => r.innerText));
  await mp.$eval('#connected-apps', (el) => el.scrollIntoView());
  await shot(mp, 'settings-apps-phone');
  await mp.goto(base + '/#/integrations');
  await mp.waitForSelector('#int-claude');
  out.phone.integrations = await overflow(mp);
  await mp.$eval('#int-claude', (el) => el.scrollIntoView());
  await shot(mp, 'integrations-claude-phone');
  await phone.close();

  // ---- SSO: "Sign in with SSO" leaves for the company's sign-in page and comes back on #/dashboard (as the real
  // callback does); the waiting request is picked up again. The identity provider is a Playwright route that signs
  // the browser in with the owner's password behind the scenes, then redirects like the SSO callback.
  const ssoCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  await ssoCtx.route('https://claude.ai/**', callback);
  out.ssoStart = [];
  await ssoCtx.route('**/api/auth/sso/start', (route) => { out.ssoStart.push(JSON.parse(route.request().postData() || 'null'));
    return route.fulfill({ json: { authorize_url: 'https://idp.example.test/authorize?client_id=tc' } }); });
  await ssoCtx.route('https://idp.example.test/**', async (route) => {
    const r = await ssoCtx.request.post(base + '/api/auth/login', { data: { email: 'owner@example.test', password: 'correct-horse-battery' } });
    out.ssoLogin = r.status();
    return route.fulfill({ status: 303, headers: { location: base + '/#/dashboard' } });
  });
  const sso = await ssoCtx.newPage();
  watch(sso, 'sso');
  const [, c4] = pkce();
  await sso.goto(authorizeUrl(claude, c4, 'state-sso'));
  await sso.waitForSelector('main form input[name=email]');
  await sso.click('main button:has-text("Sign in with SSO")');
  out.ssoEmpty = await sso.textContent('main [role=alert]');
  await sso.fill('input[name=email]', 'owner@example.test');
  await sso.click('main button:has-text("Sign in with SSO")');
  await sso.waitForSelector('.connect-host');
  out.sso = await consentState(sso);
  // a failed SSO sign-in comes back as #/login?sso=error&message=... (app/sso.py): shown as text
  await call(sso, '/api/auth/logout', { method: 'POST' });
  await sso.goto(base + '/#/login?sso=error&message=' + encodeURIComponent('Test failure <b>bold</b>'));
  await sso.reload();
  await sso.waitForSelector('main [role=alert] .banner.error');
  out.ssoError = await sso.evaluate(() => ({ text: document.querySelector('main [role=alert]').textContent, b: document.querySelectorAll('main b').length }));
  await ssoCtx.close();

  // ---- "Use another account" on the consent: signs out and shows the login for the same request
  const other = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const op = await other.newPage();
  watch(op, 'other');
  await op.goto(base + '/#/login');
  await call(op, '/api/auth/login', { method: 'POST', body: { email: 'office@example.test', password: 'office-horse-battery' } });
  const [, c5] = pkce();
  await op.goto(authorizeUrl(claude, c5, 'state-other'));
  await op.waitForSelector('.connect-host');
  out.otherEmail = await op.textContent('.connect-other');
  await op.click('main button:has-text("Use another account")');
  await op.waitForSelector('main form input[name=email]');
  out.otherAfter = await op.evaluate(() => ({ hash: location.hash, nav: document.querySelector('#nav').hidden, lead: document.querySelector('.auth-hero p').textContent }));
  // signed in elsewhere instead, then a new request: the one in the address wins over the remembered older one
  await call(op, '/api/auth/login', { method: 'POST', body: { email: 'office@example.test', password: 'office-horse-battery' } });
  const [, c6] = pkce();
  await op.goto(authorizeUrl(await register('Second App', CB), c6, 'state-newer'));
  await op.waitForSelector('.connect-host');
  await op.waitForTimeout(300);
  out.newer = await consentState(op);
  out.newerStored = await op.evaluate(() => localStorage.getItem('tc.connect'));
  await other.close();

  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_connect_flow_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    base = live_server
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "oauth.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, shots], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    assert out["signup"] == 200 and out["invite"] == 200

    # signed out: the login shows in place of the consent and keeps the request in the address
    assert out["login"]["hash"].startswith("#/connect?request=")
    assert out["login"]["lead"] == "Log in to finish connecting the app."
    assert "Using an AI assistant?" not in out["login"]["headings"]
    assert "Sign in with SSO" in out["login"]["sso"]

    # the consent: the address first, then the app's own name; who and which company; what it can do
    c = out["consent"]
    assert c["hash"].startswith("#/connect?request=") and c["title"] == "Connect an app · TradeComply CA" and c["nav"]
    assert c["h1"] == "Connect an app to TradeComply?" and c["host"] == "claude.ai" and c["name"] == "“Claude”"
    assert c["text"].index("claude.ai") < c["text"].index("“Claude”")
    assert "as owner@example.test at Connect Builders Inc." in c["text"] and "Inc.." not in c["text"]
    assert "Run TradeComply's compliance tools for your company" in c["text"] and "your role (Owner)" in c["text"]
    assert c["warns"] == [] and c["buttons"] == ["Approve", "Deny", "Use another account"]
    assert c["focus"] == "MAIN"
    assert '- button "Approve"' in out["consentAria"] and '- button "Deny"' in out["consentAria"]
    assert '- heading "Connect an app to TradeComply?" [level=1]' in out["consentAria"]
    assert out["deskSmall"] == []

    # approved: back at the app's redirect_uri with code, state and iss; the code buys working tokens
    a = out["approved"]
    assert a["at"] == "https://claude.ai/api/mcp/auth_callback" and a["state"] == "state-desk" and a["iss"] == base
    assert a["code"].startswith("tcg_")
    assert out["token"] == {"status": 200, "type": "Bearer", "access": "tco_", "refresh": "tcr_", "expires_in": 3600,
                            "scope": "tradecomply"}
    assert out["mcpWithToken"] == 200 and out["mcpWithBadToken"] == 401

    # used, unknown and malformed requests: one plain card, nothing injected
    assert out["used"]["h1"] == "This request has expired" and "already used" in out["used"]["text"]
    assert "Go back to the app and connect again." in out["unknown"]
    assert "This request has expired" in out["malformed"]["text"] and out["malformed"]["b"] == 0
    assert "started in another browser" in out["elsewhere"]["text"] and out["elsewhere"]["buttons"] == 0

    # an app on this computer: a warning; its name stays text; Deny offers a link back and goes nowhere by itself
    lc = out["localConsent"]
    assert lc["host"] == "127.0.0.1" and len(lc["warns"]) == 1 and "on this computer just now" in lc["warns"][0]
    assert lc["name"] == "“Local Tool <img src=x onerror=alert(1)>”" and out["injected"] == 0
    d = out["denied"]
    assert d["url"].startswith(base + "/#/connect?request=") and d["focus"] == "H1"
    assert "Nothing was shared with it." in d["text"] and d["linkText"] == "Back to 127.0.0.1"
    q = dict(p.split("=", 1) for p in d["link"].split("?", 1)[1].split("&"))
    assert d["link"].startswith("http://127.0.0.1:9/callback?") and q["error"] == "access_denied"
    assert q["state"] == "state-deny" and "iss" in q

    # Settings > Connected apps (owner: with the person); Disconnect ends the access and refresh tokens at once
    assert out["apps"]["heads"] == ["App", "Person", "Connected", "Last used", "Actions"]
    assert len(out["apps"]["rows"]) == 1 and "Claude" in out["apps"]["rows"][0] and "Returns to claude.ai" in out["apps"]["rows"][0]
    assert "owner@example.test (you)" in out["apps"]["rows"][0]
    assert out["apps"]["jump"][:2] == ["Company", "Your account"] and "Connected apps" in out["apps"]["jump"]
    assert "Disconnected “Claude”" in out["afterDisconnect"]["toast"] and out["afterDisconnect"]["focus"] == "apps-h"
    assert out["mcpAfterDisconnect"] == 401 and out["refreshAfterDisconnect"] == "invalid_grant"
    assert out["aiSection"] == {"open": True, "summary": "Claude on the web, desktop or phone: log in, no key",
                                "url": base + "/mcp", "steps": 3}

    # Integrations: the steps carry this server's own address; a local http:// address is flagged
    ic = out["intClaude"]
    assert ic["before"] and ic["url"] == base + "/mcp"
    assert ic["steps"][0] == "In Claude, open Customize → Connectors and choose + Add → Add custom connector."
    assert ic["steps"][2].startswith("Choose Connect.")
    assert any("Claude can't reach " + base + "/mcp" in w for w in ic["warns"])
    assert any("without signing in" in w for w in ic["warns"])               # this test server has MCP_REQUIRE_KEY=0
    assert ic["more"] == ["Claude Team or Enterprise", "Claude Code (terminal)"]

    # phone: a temporary password is replaced first (changing it later would disconnect the app), then the consent
    assert out["pwFirst"]["hash"].startswith("#/connect?request=")
    assert "temporary password" not in out["pwFirst"]["banner"]               # the page asks itself, no second nag
    assert "Changing your password disconnects the apps you connected" in out["pwFirst"]["text"]
    pc = out["phoneConsent"]
    assert pc["host"] == "claude.ai" and "as office@example.test at Connect Builders" in pc["text"] and pc["warns"] == []
    assert "your role (Office)" in pc["text"]
    assert out["phoneToken"] == 200
    assert out["phoneApps"] and "Claude" in out["phoneApps"][0]
    assert not any(out["phone"].values()), out["phone"]
    assert out["phoneSmall"] == [] and out["phoneSmallPw"] == []
    assert all(h >= 44 for _, h in out["phoneButtons"]) and all(w >= 120 for w, _ in out["phoneButtons"])

    # SSO: the email is needed first; after the identity provider's round trip the consent is back
    assert "Enter your work email" in out["ssoEmpty"]
    assert out["ssoStart"] == [{"email": "owner@example.test"}] and out["ssoLogin"] == 200
    assert out["sso"]["host"] == "claude.ai" and out["sso"]["hash"].startswith("#/connect?request=")
    assert "as owner@example.test" in out["sso"]["text"]
    assert out["ssoError"]["text"] == "Test failure <b>bold</b>" and out["ssoError"]["b"] == 0

    # someone else's session on this computer: "Use another account" signs out, same request
    assert out["otherEmail"].startswith("Not office@example.test?")
    assert out["otherAfter"]["hash"].startswith("#/connect?request=") and out["otherAfter"]["nav"]
    assert out["otherAfter"]["lead"] == "Log in to finish connecting the app."
    assert out["newer"]["name"] == "“Second App”" and out["newerStored"] is None


# ---------------------------------------------------------------------------- upgrading from the previous release
# The service worker of the release before "Sign in with TradeComply" (tc-shell-v4), as the browsers of people who
# used the web app have it installed. It answers /static/app.js from its cache (ignoring any ?query) and fills that
# cache from the browser's HTTP cache. So when such a person opens Claude's sign-in popup right after this release
# is deployed, that worker runs the first page: index.html must name this release's script by a path it never saw.
SW_V4 = r"""/* TradeComply service worker: the app shell works offline; API calls go to the network first and fall back to
   the last copy of the tool schemas, account, config, project list, integrations catalog and the list of kinds of
   licensed professionals, so the field screen, the Integrations page and the finder's form open without signal.
   v4: guidance release (professional finder, getting started, account data). */
const SHELL_CACHE = 'tc-shell-v4';
const API_CACHE = 'tc-api-v1';
const SHELL = ['/', '/static/app.js', '/static/styles.css', '/manifest.webmanifest', '/icon.svg'];
const API_CACHEABLE = ['/api/tools', '/api/me', '/api/config', '/api/projects', '/api/integrations/catalog', '/api/professionals/types'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE && k !== API_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'clear-api-cache') event.waitUntil(caches.delete(API_CACHE));
});

function offlineJson() {
  return new Response(JSON.stringify({ detail: 'You are offline. This will work again when you reconnect.', offline: true }),
    { status: 503, headers: { 'Content-Type': 'application/json' } });
}

async function networkFirstApi(request) {
  const url = new URL(request.url);
  const cacheable = API_CACHEABLE.includes(url.pathname);
  try {
    const res = await fetch(request);
    if (cacheable && res.ok) {
      const copy = res.clone();
      caches.open(API_CACHE).then((c) => c.put(request, copy));
    }
    return res;
  } catch (err) {
    if (cacheable) {
      const hit = await caches.match(request, { cacheName: API_CACHE });
      if (hit) return hit;
    }
    return offlineJson();
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(SHELL_CACHE);
  const hit = await cache.match(request, { ignoreSearch: true });
  const fresh = fetch(request).then((res) => {
    if (res.ok) cache.put(request, res.clone());
    return res;
  }).catch(() => null);
  return hit || (await fresh) || new Response('Offline', { status: 503 });
}

async function navigation(request) {
  try {
    const res = await fetch(request);
    // Only the real app shell may become the offline copy of '/': caching any page navigated to (a 404, /health,
    // an error) would replace the app with that page offline.
    const path = new URL(request.url).pathname;
    if (res.ok && (path === '/' || path === '/index.html') && (res.headers.get('content-type') || '').includes('text/html')) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put('/', res.clone());
    }
    return res;
  } catch (err) {
    return (await caches.match('/', { cacheName: SHELL_CACHE })) || new Response('Offline', { status: 503 });
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/mcp' || url.pathname.startsWith('/mcp/') || url.pathname === '/api/calendar.ics'
    || url.pathname === '/api/integrations/calendar_feed/calendar.ics' || url.pathname === '/api/tenant/export') return;      // downloads: never a cached or JSON copy
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirstApi(request));
  } else if (request.mode === 'navigate') {
    event.respondWith(navigation(request));
  } else if (SHELL.includes(url.pathname) || url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidate(request));
  }
});
"""
OLD_SHELL = {
    "/": ("text/html", b'<!doctype html><html><head><link rel="stylesheet" href="/static/styles.css">'
                       b'<script src="/static/app.js" defer></script></head><body><main id="main"></main></body></html>'),
    "/sw.js": ("text/javascript", SW_V4.encode()),
    "/static/app.js": ("text/javascript", b"navigator.serviceWorker.register('/sw.js');"
                                          b"document.getElementById('main').textContent = 'previous release';"),
    "/static/styles.css": ("text/css", b"main { color: black; }"),
    "/manifest.webmanifest": ("application/manifest+json", b"{}"),
    "/icon.svg": ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
}
UPGRADE = r"""
const { chromium } = require('playwright');
const crypto = require('crypto');
const [phase, base, profile] = process.argv.slice(2);
(async () => {
  const ctx = await chromium.launchPersistentContext(profile, { viewport: { width: 375, height: 812 }, deviceScaleFactor: 1 });
  const page = ctx.pages()[0] || await ctx.newPage();
  const out = {};
  if (phase === 'old') {
    await page.goto(base + '/');
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.reload();
    out.controlled = await page.evaluate(() => !!navigator.serviceWorker.controller);
    out.caches = await page.evaluate(async () => caches.keys());
    out.text = await page.textContent('main');
  } else {
    // signed in (as they were before the deploy) without opening a page: the popup is the first one after it
    out.signup = (await ctx.request.post(base + '/api/auth/signup', { data: { company_name: 'Upgrade Co', email: 'up@example.test', password: 'correct-horse-battery' } })).status();
    const client = await (await ctx.request.post(base + '/oauth/register', { data: { client_name: 'Claude', redirect_uris: ['https://claude.ai/api/mcp/auth_callback'], token_endpoint_auth_method: 'none' } })).json();
    const challenge = crypto.createHash('sha256').update(crypto.randomBytes(48).toString('base64url')).digest('base64url');
    await page.goto(`${base}/oauth/authorize?` + new URLSearchParams({ response_type: 'code', client_id: client.client_id,
      redirect_uri: 'https://claude.ai/api/mcp/auth_callback', state: 's', code_challenge: challenge, code_challenge_method: 'S256' }));
    await page.waitForSelector('main h1', { timeout: 15000 });
    await page.waitForTimeout(300);
    out.first = await page.evaluate(() => ({ h1: document.querySelector('main h1').textContent, host: (document.querySelector('.connect-host') || {}).textContent,
      weight: document.querySelector('.connect-host') && getComputedStyle(document.querySelector('.connect-host')).fontWeight }));
  }
  process.stdout.write(JSON.stringify(out));
  await ctx.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _serve_old_shell(port: int):
    """The previous release's shell on 127.0.0.1:port, its files served like StaticFiles did (Last-Modified, no
    Cache-Control: the browser treats them as fresh for a while)."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            ctype, body = OLD_SHELL.get(self.path.split("?")[0], ("text/plain", None))
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body or b"")))
            if self.path.startswith("/static/"):
                self.send_header("Last-Modified", "Mon, 01 Jan 2024 00:00:00 GMT")
            else:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_first_sign_in_after_the_deploy_shows_the_consent_even_with_the_old_service_worker(tmp_path, playwright_env):
    import sys
    import time

    import httpx

    from tests.test_integrations_ui import _free_port
    node, env = playwright_env
    port, profile, script = _free_port(), tmp_path / "profile", tmp_path / "upgrade.js"
    script.write_text(UPGRADE)
    base = f"http://127.0.0.1:{port}"
    old = _serve_old_shell(port)
    try:
        r = subprocess.run([node, str(script), "old", base, str(profile)], env=env, capture_output=True, text=True,
                           timeout=120)
    finally:
        old.shutdown()
        old.server_close()
    assert r.returncode == 0, r.stderr[-3000:]
    before = json.loads(r.stdout)
    assert before == {"controlled": True, "caches": ["tc-shell-v4"], "text": "previous release"}, before

    # this release, on the same address (the deploy)
    senv = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL")}
    senv.update(DATA_DIR=str(tmp_path / "data"), LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0",
                MCP_REQUIRE_KEY="1")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                             str(port)], cwd=REPO, env=senv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while True:
            assert proc.poll() is None and time.time() < deadline, "server did not start"
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        r = subprocess.run([node, str(script), "new", base, str(profile)], env=env, capture_output=True, text=True,
                           timeout=120)
    finally:
        proc.terminate()
        proc.wait(timeout=15)
    assert r.returncode == 0, r.stderr[-3000:]
    after = json.loads(r.stdout)
    assert after["signup"] == 200
    # this release's script and styles, not the cached previous ones (which would show the dashboard instead)
    assert after["first"] == {"h1": "Connect an app to TradeComply?", "host": "claude.ai", "weight": "700"}, after


def test_the_shell_names_release_paths_the_service_worker_caches(client):
    """index.html, sw.js's SHELL and the server agree on this release's asset paths, which are not the ones the
    previous worker cached; /static files are revalidated (no-cache) and the worker installs past the HTTP cache."""
    index = client.get("/").text
    assets = re.findall(r'(?:src|href)="(/static/[^"]+)"', index)
    sw = client.get("/sw.js").text
    version = re.search(r"const SHELL_CACHE = 'tc-shell-v(\d+)'", sw).group(1)
    assert sorted(assets) == [f"/static/v{version}/app.js", f"/static/v{version}/styles.css"]
    shell = re.search(r"const SHELL = \[([^\]]+)\]", sw).group(1)
    assert all(f"'{a}'" in shell for a in assets) and "'/static/app.js'" not in shell
    assert "new Request(u, { cache: 'reload' })" in sw
    for a in assets:
        r = client.get(a)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.content == \
            client.get(a.replace(f"/v{version}", "")).content
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get(f"/static/v{version}/docs.js").status_code == 404           # only the shell's two files
    assert client.get("/static/vx/app.js").status_code == 404
