"""Accessibility checks of spec 7.1 that no other UI test makes: input purpose tokens only on the person's own details
(1.3.5), no single-character shortcuts (2.1.4), focus never hidden under the top bar, the bottom bar or a form's
action bar while tabbing through Today and a check on a phone with its keyboard up (2.4.11; 375 x 420 px), and no
sideways scrolling with the WCAG text-spacing overrides applied (1.4.12) at 375 and 1280 px. The browser test runs
when Node with Playwright and its Chromium is installed (skipped otherwise)."""
import json
import os
import re
import subprocess

from tests.test_integrations_ui import REPO, live_server, playwright_env  # noqa: F401  (fixtures)

JS = open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8").read()


def test_input_purpose_only_on_the_persons_own_details_and_no_one_key_shortcuts():
    tokens = set(re.findall(r"autocomplete: '([a-z-]+)'", JS))
    # sign-up, account, invite, log in: the person's own email, name, company and passwords; everything else off
    assert tokens <= {"off", "email", "name", "organization", "username", "current-password", "new-password"}, tokens
    assert not re.search(r"""key\s*===?\s*['"][/?.]['"]""", JS)          # no "/" or "?" shortcut (WCAG 2.1.4)


SMOKE = r"""
const { chromium } = require('playwright');
(async () => {
  const [base] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], hidden: {}, spacing: {} };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const setup = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
  const sp = await setup.newPage();
  await sp.goto(base + '/#/login');
  out.signup = (await call(sp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Focus Builders', email: 'focus@example.test', password: 'correct-horse-battery' } })).status;
  await call(sp, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', first_furnishing_date: '2026-11-15', last_furnishing_date: '2026-11-25' } });
  const state = await setup.storageState();
  await setup.close();

  // ---- 2.4.11: a phone with its keyboard up (375 x 420): Tab through the page; nothing focused hides under a bar
  const ctx = await browser.newContext({ viewport: { width: 375, height: 420 }, deviceScaleFactor: 1, serviceWorkers: 'block', storageState: state });
  const p = await ctx.newPage();
  p.on('pageerror', (e) => out.errors.push(String(e)));
  for (const [hash, ready] of [['#/dashboard', 'a.task-tile'], ['#/tools/calculate_lien_deadline', 'main form button[type=submit]']]) {
    await p.goto(base + '/' + hash);
    await p.waitForSelector(ready);
    await p.waitForTimeout(400);
    await p.focus('main');
    const bad = [];
    for (let i = 0; i < 45; i++) {
      await p.keyboard.press('Tab');
      const r = await p.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body || el.closest('.topbar, #nav, .action-bar, #ask-panel')) return null;      // the bars themselves
        const b = el.getBoundingClientRect();
        if (!b.width && !b.height) return null;
        // what a sighted person sees at the middle of the focused element: it, or a bar drawn over it?
        const x = Math.min(innerWidth - 1, Math.max(0, b.left + b.width / 2)), y = Math.min(innerHeight - 1, Math.max(0, b.top + b.height / 2));
        const hit = document.elementFromPoint(x, y);
        const off = b.bottom <= 0 || b.top >= innerHeight;
        const covered = hit && !el.contains(hit) && !hit.contains(el) && hit.closest('.topbar, #nav, .action-bar');
        return off || covered ? `${el.tagName}.${el.className} "${(el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30)}" ${Math.round(b.top)}-${Math.round(b.bottom)}` : null;
      });
      if (r) bad.push(r);
    }
    out.hidden[hash] = bad;
  }
  await ctx.close();

  // ---- 1.4.12: the WCAG text-spacing overrides: nothing scrolls sideways
  const SPACING = '* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }';
  for (const [w, h] of [[375, 812], [1280, 800]]) {
    // bypassCSP: the overrides come in as a user style sheet does (the page's own CSP allows no inline style)
    const c = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, serviceWorkers: 'block', storageState: state, bypassCSP: true });
    const q = await c.newPage();
    q.on('pageerror', (e) => out.errors.push(String(e)));
    for (const hash of ['#/dashboard', '#/projects', '#/tools', '#/tools/calculate_lien_deadline', '#/subs', '#/settings', '#/more']) {
      await q.goto(base + '/' + hash);
      await q.waitForSelector('main h1');
      await q.addStyleTag({ content: SPACING });
      await q.waitForTimeout(300);
      out.spacing[`${w}${hash}`] = await q.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    }
    await q.click('#ask-open');
    await q.waitForSelector('#ask-panel:not([hidden]) .qh-note');        // (More has no starter questions)
    await q.addStyleTag({ content: SPACING });
    out.spacing[`${w}#help`] = await q.evaluate(() => { const a = document.getElementById('ask-panel'); return a.scrollWidth > a.clientWidth || document.documentElement.scrollWidth > document.documentElement.clientWidth; });
    await c.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_focus_stays_in_view_and_text_spacing_does_not_break_layout(tmp_path, playwright_env, live_server):  # noqa: F811
    node, env = playwright_env
    script = tmp_path / "a11y.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server], env=env, capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["signup"] == 200 and out["errors"] == []
    assert out["hidden"] == {"#/dashboard": [], "#/tools/calculate_lien_deadline": []}
    assert not any(out["spacing"].values()), out["spacing"]
