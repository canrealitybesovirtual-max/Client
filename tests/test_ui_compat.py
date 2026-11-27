"""A browser that still holds a v8-shaped /api/tools (spec 1.10 "Old cached data"): right after the v9 deploy an
offline browser can keep the old tool list (the service worker's API cache isn't versioned with the shell), with no
groups, common checks, summaries or layouts. The Checks index, Today's tiles, a long form (one page instead of
steps), the Search palette and the Help panel's starter questions must still work from `modules` and `title`, with
no page error. Runs at 1280 and 375 px when Node with Playwright and its Chromium is installed (skipped otherwise)."""
import json
import os
import subprocess

from tests.test_integrations_ui import live_server, playwright_env  # noqa: F401  (fixtures)

SMOKE = r"""
const { chromium } = require('playwright');
(async () => {
  const [base] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], sizes: {} };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    return r.status;
  }, [url, o]);
  for (const [tag, viewport] of [['desk', { width: 1280, height: 800 }], ['phone', { width: 375, height: 812 }]]) {
    const o = out.sizes[tag] = {};
    const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport, serviceWorkers: 'block' });
    const p = await ctx.newPage();
    p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`));
    p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); });
    await p.route('**/api/tools', async (route) => {          // the v8 shape: what an old cached copy holds
      const resp = await route.fetch();
      const json = await resp.json();
      delete json.groups; delete json.common;
      for (const t of json.tools) { for (const k of ['summary', 'layout', 'group', 'group_label', 'hidden']) delete t[k]; }
      await route.fulfill({ response: resp, json });
    });
    await p.goto(base + '/#/login');
    o.signup = await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: `Old Cache ${tag}`, email: `v8-${tag}@example.test`, password: 'correct-horse-battery' } });
    await p.goto(base + '/#/tools');
    await p.waitForSelector('.tool-list details');
    o.checks = { groups: await p.$$eval('.tool-list details', (d) => d.length), rows: await p.$$eval('.tool-list a.list-row', (a) => a.length) };
    // a check with a hand-made step layout in v9: one page here (no layout), Run at the end
    await p.goto(base + '/#/tools/permit_required');
    await p.waitForSelector('main form.tool-form');
    o.permit = { steppers: await p.$$eval('main .stepper', (s) => s.filter((x) => x.getClientRects().length).length),
      run: await p.$eval('main form.tool-form button[type=submit]', (b) => !b.hidden && b.textContent), fields: await p.$$eval('main form.tool-form .field', (f) => f.length) };
    // the Help panel on a check: the Checks page's starters (no groups to take the check's own from)
    await p.click('#ask-open');
    await p.waitForSelector('#ask-panel:not([hidden]) .qh-starters .chip');
    o.starters = await p.$$eval('#ask-panel .qh-starters .chip', (c) => c.map((x) => x.textContent));
    await p.click('#ask-panel .ask-head button:has-text("Close")');
    // Search: nothing typed (no common checks to list), then a word
    await p.click('#search-open');
    await p.waitForSelector('#palette[open]');
    await p.waitForTimeout(400);
    o.palEmpty = await p.$$eval('#pal-list .opt-group', (g) => g.map((x) => x.textContent));
    await p.fill('#pal-q', 'lien');
    await p.waitForFunction(() => [...document.querySelectorAll('#pal-list .opt-group')].some((g) => g.textContent === 'Checks'));
    o.palLien = await p.$$eval('#pal-list [role=option]', (x) => x.map((e) => e.textContent).slice(0, 3));
    await p.keyboard.press('Escape');
    await p.goto(base + '/#/dashboard');
    await p.waitForSelector('a.task-tile');
    o.tiles = await p.$$eval('a.task-tile', (a) => a.map((x) => x.getAttribute('href')));
    await ctx.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_a_v8_shaped_tool_list_still_works(tmp_path, playwright_env, live_server):  # noqa: F811
    node, env = playwright_env
    script = tmp_path / "compat.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    for tag, o in out["sizes"].items():
        assert o["signup"] == 200, tag
        assert o["checks"]["groups"] >= 10 and o["checks"]["rows"] >= 70
        assert o["permit"]["steppers"] == 0 and o["permit"]["run"] == "Run check" and o["permit"]["fields"] >= 5
        assert o["starters"] == ["Which check gives my lien deadline?", "Do I need a Cal/OSHA permit for a 6 ft trench?",
                                 "Can a handyman do a $900 job without a license?"]
        assert "Common checks" not in o["palEmpty"]
        assert o["palLien"] and any("lien" in x.lower() for x in o["palLien"])
        assert len(o["tiles"]) == 8 and "#/tools/calculate_lien_deadline" in o["tiles"]
