"""Browser checks for the final pre-launch review's UI fixes (2026-11-27), against a real server with email off and
SUPPORT_EMAIL set, at 1280 and 375 px. Runs when Node with Playwright and its Chromium is installed (skipped
otherwise). Set TC_UI_SHOTS=<directory> to keep the screenshots (final-*.png).

- skip link: focuses the page and keeps the route and anything typed (it went to the Dashboard, or Login, at #main)
- Forgot password with email off names who an owner should contact
- the welcome stepper fits a 375 px phone; welcome step 5 points to where the API keys are
- a Dashboard box that counts the license card marks it instead of showing "Nothing in this group."
- the Field role says what Field users can see; a field user's empty pages don't point at buttons they don't have
- a session the server ends (the person removed) also clears the offline copy of the company's data
- the login page never shows words from the URL in its error banner
- an expired trial with no online billing says who to contact
"""
import json
import os
import subprocess
import sys

import pytest

from tests.test_integrations_ui import REPO, _free_port, playwright_env  # noqa: F401  (fixture)

SCRIPT = r"""
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const [base, OUT, dataDir] = process.argv.slice(2);
const prefix = 'final';
const PW = 'correct-horse-battery';
const phone = { viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 };
const desk = { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 };
const res = {};
const stamp = Date.now();
const shot = (p, name, o = {}) => p.screenshot({ path: path.join(OUT, `${prefix}-${name}.png`), ...o });
async function call(page, url, o = {}) {
  return page.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
}
async function signupVia(page, company, email, license) {
  await page.goto(base + '/#/login');
  await page.waitForSelector('main');
  const r = await call(page, '/api/auth/signup', { method: 'POST', body: { company_name: company, email, password: PW, cslb_license: license } });
  if (r.status !== 200) throw new Error('signup ' + r.status + JSON.stringify(r.data));
  await page.reload(); await page.waitForSelector('main');
  return r;
}
async function joinVia(ownerPage, page, email, role) {
  const inv = await call(ownerPage, '/api/users', { method: 'POST', body: { email, role } });
  await page.goto(base + '/#/login'); await page.waitForSelector('main');
  const token = inv.data.invite_path.split('/').pop();
  const r = await call(page, '/api/auth/invite/accept', { method: 'POST', body: { token, password: PW } });
  if (r.status !== 200) throw new Error('join ' + r.status);
  return inv.data;
}
async function section(name, fn) {
  try { await fn(); } catch (e) { res[name] = { error: String(e).slice(0, 400) }; }
}

(async () => {
  const browser = await chromium.launch();
  // ---- skip link (desktop, signed in, lien form half filled)
  await section('skip_signed_in', async () => {
    const c = await browser.newContext({ ...desk, serviceWorkers: 'block' }); const p = await c.newPage();
    await signupVia(p, 'Skip Co', `skip-${stamp}@example.com`, '123456');
    await p.goto(base + '/#/tools/calculate_lien_deadline');
    await p.waitForSelector('main form'); await p.waitForTimeout(500);
    await p.getByLabel('Last furnishing date').fill('2026-11-01');
    await p.focus('a.skip-link'); await p.keyboard.press('Enter'); await p.waitForTimeout(700);
    res.skip_signed_in = await p.evaluate(() => ({ hash: location.hash, h1: (document.querySelector('main h1') || {}).textContent,
      date: ([...document.querySelectorAll('main input[type=date]')].map((i) => i.value).filter(Boolean)[0]) || null, focused: document.activeElement && (document.activeElement.id || document.activeElement.tagName) }));
    await shot(p, 'skip-link-lien-form');
    await c.close();
  });
  // ---- skip link (signed out, sign-up form)
  await section('skip_signup', async () => {
    const c = await browser.newContext({ ...desk, serviceWorkers: 'block' }); const p = await c.newPage();
    await p.goto(base + '/#/signup'); await p.waitForSelector('input[name=company_name]');
    await p.fill('input[name=company_name]', 'Half Typed Builders');
    await p.focus('a.skip-link'); await p.keyboard.press('Enter'); await p.waitForTimeout(700);
    res.skip_signup = await p.evaluate(() => ({ hash: location.hash, h1: (document.querySelector('main h1') || {}).textContent,
      company: (document.querySelector('input[name=company_name]') || {}).value || null }));
    await shot(p, 'skip-link-signup');
    await c.close();
  });
  // ---- forgot password as an owner, email off (support contact set)
  await section('forgot', async () => {
    const c = await browser.newContext({ ...phone, serviceWorkers: 'block' }); const p = await c.newPage();
    await p.goto(base + '/#/forgot'); await p.waitForSelector('main .card');
    res.forgot = await p.$eval('main', (e) => e.innerText);
    await shot(p, 'forgot-email-off');
    await c.close();
  });
  // ---- new company, no license: tiles, welcome stepper, welcome step 5, invite role picker; then a field user
  await section('owner_pages', async () => {
    const c = await browser.newContext({ ...phone, serviceWorkers: 'block' }); const p = await c.newPage();
    await signupVia(p, 'Tiles Co', `tiles-${stamp}@example.com`, '');
    await p.goto(base + '/#/welcome'); await p.waitForSelector('.stepper'); await p.waitForTimeout(300);
    res.stepper = await p.$eval('.stepper', (e) => ({ client: e.clientWidth, scroll: e.scrollWidth, right: Math.round(e.getBoundingClientRect().right),
      last: Math.round(e.lastElementChild.getBoundingClientRect().right), labels: [...e.children].map((li) => li.innerText.replace(/\s+/g, ' ')) }));
    await shot(p, 'welcome-stepper-375');
    await p.goto(base + '/#/welcome?step=5'); await p.waitForTimeout(800);
    res.welcome5 = await p.$eval('main', (e) => (e.innerText.match(/[^\n]*(API keys|Edit access)[^\n]*/g) || []).slice(0, 4));
    await shot(p, 'welcome-step5', { fullPage: true });
    await p.goto(base + '/#/dashboard'); await p.waitForSelector('.tiles'); await p.waitForTimeout(400);
    await p.tap('.tile.amber'); await p.waitForTimeout(400);
    res.tiles = await p.evaluate(() => ({ tile: document.querySelector('.tile.amber').innerText.replace(/\s+/g, ' '),
      deadlines: document.querySelector('section[aria-labelledby=dl-h]').innerText }));
    await p.$eval('section[aria-labelledby=dl-h]', (e) => e.scrollIntoView());
    await shot(p, 'dashboard-amber-tile');
    await p.goto(base + '/#/settings'); await p.waitForSelector('select[name=role]');
    res.roles = await p.$$eval('select[name=role] option', (os) => os.map((o) => o.textContent));
    const form = await p.$('select[name=role]');
    await form.scrollIntoViewIfNeeded();
    await shot(p, 'invite-role-picker');
    // a field user on a phone, in a company with no data
    const fc = await browser.newContext({ ...phone, serviceWorkers: 'block' }); const fp = await fc.newPage();
    await joinVia(p, fp, `crew-${stamp}@example.com`, 'field');
    await fp.goto(base + '/#/subs'); await fp.waitForSelector('main h1'); await fp.waitForTimeout(500);
    res.field_subs = await fp.$eval('main', (e) => e.innerText);
    await shot(fp, 'field-subs-empty');
    await fp.goto(base + '/#/dashboard'); await fp.waitForSelector('.tiles'); await fp.waitForTimeout(500);
    res.field_dash = await fp.$eval('main', (e) => (e.innerText.match(/[^\n]*(license|Add projects|lien calendar|subs)[^\n]*/gi) || []).slice(0, 8));
    await shot(fp, 'field-dashboard', { fullPage: true });
    await fp.goto(base + '/#/projects'); await fp.waitForSelector('main h1'); await fp.waitForTimeout(400);
    res.field_projects = await fp.$eval('main', (e) => e.innerText);
    await fc.close(); await c.close();
  });
  // ---- service worker cache after the server ends the session (the person is removed)
  await section('sw_cache', async () => {
    const oc = await browser.newContext({ ...desk, serviceWorkers: 'block' }); const op = await oc.newPage();
    await signupVia(op, 'SW Co', `sw-${stamp}@example.com`, '123456');
    await call(op, '/api/projects', { method: 'POST', body: { name: 'Secret Tower Job', address: '1 Private Way' } });
    const mc = await browser.newContext({ ...desk }); const mp = await mc.newPage();
    const inv = await joinVia(op, mp, `office-${stamp}@example.com`, 'office');
    await mp.goto(base + '/#/projects'); await mp.waitForSelector('main h1');
    await mp.evaluate(async () => { await navigator.serviceWorker.ready; });
    await mp.reload(); await mp.waitForSelector('main h1'); await mp.waitForTimeout(1200);
    const before = await mp.evaluate(async () => { const c = await caches.open('tc-api-v1'); return (await c.keys()).map((r) => new URL(r.url).pathname); });
    await call(op, `/api/users/${inv.id}`, { method: 'DELETE' });
    await mp.goto(base + '/#/projects?again=1'); await mp.waitForTimeout(1500);
    const shown = await mp.$eval('main', (e) => e.innerText.slice(0, 200));
    const after = await mp.evaluate(async () => { const c = await caches.open('tc-api-v1'); return (await c.keys()).map((r) => new URL(r.url).pathname); });
    await mc.setOffline(true);
    await mp.reload().catch(() => {}); await mp.waitForTimeout(2000);
    const offline = await mp.$eval('body', (e) => e.innerText.slice(0, 400)).catch((e) => String(e));
    res.sw_cache = { cachedBefore: before, onlineAfterRemoval: shown, cachedAfter: after, offlineReload: offline };
    await shot(mp, 'sw-offline-after-removal');
    await mc.close(); await oc.close();
  });
  // ---- words from the URL on the real login page
  await section('login_spoof', async () => {
    const c = await browser.newContext({ ...desk, serviceWorkers: 'block' }); const p = await c.newPage();
    await p.goto(base + '/#/login?sso=error&message=' + encodeURIComponent('Your TradeComply subscription has lapsed and your account is locked. Call billing at 1-800-555-0199.'));
    await p.waitForSelector('main [role=alert]'); await p.waitForTimeout(300);
    res.login_spoof = await p.$eval('main [role=alert]', (e) => e.innerText);
    await shot(p, 'login-url-message');
    await c.close();
  });
  // ---- trial over, no online billing: what Subscribe says
  await section('expired_trial', async () => {
    const c = await browser.newContext({ ...desk, serviceWorkers: 'block' }); const p = await c.newPage();
    const email = `expired-${stamp}@example.com`;
    await signupVia(p, 'Expired Co', email, '123456');
    execFileSync(process.env.TC_PYTHON || 'python3', ['-c', `import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); con.execute("update tenants set trial_ends='2020-01-01T00:00:00+00:00' where id=(select tenant_id from users where email=?)", (sys.argv[2],)); con.commit()`, path.join(dataDir, 'tradecomply.sqlite'), email]);
    await p.goto(base + '/#/dashboard'); await p.reload(); await p.waitForSelector('main h1'); await p.waitForTimeout(500);
    const btn = await p.$('main button:has-text("Subscribe now")');
    if (btn) { await btn.click(); await p.waitForTimeout(800); }
    res.expired_trial = await p.evaluate(() => ({ card: document.querySelector('main').innerText, toast: [...document.querySelectorAll('.toast')].map((t) => t.innerText) }));
    await shot(p, 'expired-trial-subscribe');
    await c.close();
  });
  await browser.close();
  process.stdout.write(JSON.stringify(res));
})();

"""


@pytest.fixture
def server(tmp_path, playwright_env):
    import time

    import httpx
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL", "RENDER")
           and not k.startswith(("SMTP_", "POSTMARK_", "MAIL_", "STRIPE_"))}
    data = tmp_path / "data"
    env.update(DATA_DIR=str(data), LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0", MCP_REQUIRE_KEY="1",
               SUPPORT_EMAIL="support@example.com")
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
        yield base, str(data)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_final_review_ui_fixes_in_a_real_browser(tmp_path, playwright_env, server):
    node, env = playwright_env
    base, data = server
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "final.js"
    script.write_text(SCRIPT)
    r = subprocess.run([node, str(script), base, shots, data], env={**env, "TC_PYTHON": sys.executable},
                       capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert not [k for k, v in out.items() if isinstance(v, dict) and "error" in v], out
    assert out["skip_signed_in"] == {"hash": "#/tools/calculate_lien_deadline", "h1": "Calculate lien deadline",
                                     "date": "2026-11-01", "focused": "main"}
    assert out["skip_signup"] == {"hash": "#/signup", "h1": "Start your free trial", "company": "Half Typed Builders"}
    assert "If you are the owner, contact support: support@example.com." in out["forgot"]
    st = out["stepper"]
    assert st["scroll"] <= st["client"] and st["last"] <= st["right"], st
    assert any("in Settings > AI assistant (MCP)" in line for line in out["welcome5"])
    assert not any("API keys below" in line for line in out["welcome5"])
    assert "Your license is in it" in out["tiles"]["deadlines"]
    assert out["roles"][0].startswith("Field (field logs and checks; views projects, subs, docs)")
    assert "Import your list above" not in out["field_subs"] and "Your office adds subcontractors" in out["field_subs"]
    assert not any("Add your CSLB license number in Settings." in line for line in out["field_dash"])
    assert "Add one to build" not in out["field_projects"]
    sw = out["sw_cache"]
    assert "/api/projects" in sw["cachedBefore"] and sw["cachedAfter"] == []
    assert "Secret Tower Job" not in sw["offlineReload"] and "SW Co" not in sw["offlineReload"]
    assert "1-800" not in out["login_spoof"] and "Signing in with SSO didn't work" in out["login_spoof"]
    assert "To subscribe, contact support: support@example.com." in out["expired_trial"]["card"]
