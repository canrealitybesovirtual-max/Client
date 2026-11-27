"""Type-ahead in a real browser (spec 3): the APG keyboard model (type, Down, Enter; Escape keeps the text; Tab
doesn't pick), aria-expanded / aria-activedescendant, one status line for the page, "Did you mean" for a mistyped
county, a class field that takes what is typed and suggests C-39 for "roofer", the company's own subs, a pointer
pick on a phone that keeps the focus, the list above a phone keyboard, a job fill that asks nothing and opens
nothing, saved suggestions offline, no typed words in any request address, and nothing of company A left after
company B signs in in the same tab. Runs at 1280 and 375 px when Node with Playwright and its Chromium is installed
(skipped otherwise). TC_UI_SHOTS=<directory> keeps the screenshots (ux-combobox-*.png)."""
import json
import os
import re
import subprocess

from tests.test_integrations_ui import REPO, live_server, playwright_env  # noqa: F401  (fixtures)

JS = open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8").read()


def test_typed_words_never_go_in_an_address():
    for route in ("/api/search?", "/api/suggest?", "/api/help?"):
        assert f"'{route}" not in JS and f"`{route}" not in JS
    assert "api('/api/suggest', { method: 'POST'" in JS and "api('/api/search', { method: 'POST'" in JS
    # suggestions come only while the field itself has the focus: a fill by the app asks nothing
    assert "if (document.activeElement !== inp) return;       // a fill by the app: no list, no request" in JS


SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], urls: [] };
  const watch = (p, tag) => {
    p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`));
    p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); });
    p.on('request', (r) => out.urls.push(r.url()));
  };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const field = (p, name) => p.locator(`.field[data-field=${name}] input[role=combobox]`);
  const state = (p) => p.evaluate(() => {
    const i = document.activeElement;
    const list = i && i.getAttribute('aria-controls') ? document.getElementById(i.getAttribute('aria-controls')) : null;
    const act = i && i.getAttribute('aria-activedescendant') ? document.getElementById(i.getAttribute('aria-activedescendant')) : null;
    return { value: i ? i.value : null, expanded: i ? i.getAttribute('aria-expanded') : null, open: !!list && !list.hidden,
      options: list ? [...list.querySelectorAll('[role=option]')].map((o) => o.textContent) : [], active: act ? act.textContent : null,
      activeShown: !!act && act.offsetParent !== null, status: (document.getElementById('cbx-status') || {}).textContent || '' };
  });
  const tool = async (p, name, q = '') => { const u = `${base}/#/tools/${name}${q}`; if (p.url() === u) await p.reload(); else await p.goto(u); await p.waitForSelector('main form.tool-form'); await p.waitForTimeout(400); };

  // ---- company A (desktop): a project, a sub
  const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const p = await ctx.newPage();
  watch(p, 'A');
  await p.goto(base + '/#/login');
  out.signup = (await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: 'Sierra Build Co', email: 'a@example.test', password: 'correct-horse-battery', cslb_license: '123456' } })).status;
  const elm = (await call(p, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', address: '1200 Elm St', owner_name: 'Dana Park', claimant_role: 'subcontractor',
    first_furnishing_date: '2026-11-03', last_furnishing_date: '2026-11-20', start_date: '2026-10-01', contract_price: 48000, public_or_private: 'private' } })).data;
  await call(p, '/api/subs', { method: 'POST', body: { name: 'Ace Roofing', license_number: '1023456', scope: 'Roofing' } });

  // keyboard: type, Down, Enter; Escape keeps the text; Tab doesn't pick
  await tool(p, 'valley_fever_requirements');
  const county = field(p, 'county');
  await county.click();
  await p.keyboard.type('san d');
  await p.waitForTimeout(200);
  out.typed = await state(p);
  await p.keyboard.press('ArrowDown');
  out.down = await state(p);
  await p.screenshot({ path: path.join(shots, 'ux-combobox-county-open-d.png') });
  await p.keyboard.press('Enter');
  out.picked = await state(p);
  await county.fill('');
  await p.keyboard.type('Sac');
  await p.waitForTimeout(100);
  await p.keyboard.press('Escape');
  out.escaped = await state(p);
  await county.fill('');
  await p.keyboard.type('Fres');
  await p.keyboard.press('ArrowDown');
  await p.keyboard.press('Tab');
  out.tabbed = await county.inputValue();
  await county.click();
  await p.waitForTimeout(1600);
  out.status = await state(p);
  // a mistyped county: "Did you mean" under the field, and the check doesn't take it
  await county.fill('San Deigo');
  await p.keyboard.press('Tab');
  await p.waitForTimeout(100);
  out.dym = await p.$eval('.cbx-dym', (e) => (e.hidden ? null : e.textContent));
  await p.click('main form button[type=submit]');
  out.strictError = await p.$eval('.error-text:not(:empty)', (e) => e.textContent).catch(() => null);
  await p.click('.cbx-dym button');
  out.dymFixed = await county.inputValue();

  // a class field the check accepts as typed (D-49), and C-39 for "roofer"
  await tool(p, 'check_subcontractor_compliance');
  const cls = field(p, 'required_classification');
  await cls.click();
  await p.keyboard.type('roofer');
  await p.waitForTimeout(150);
  out.roofer = (await state(p)).options.slice(0, 3);
  await p.screenshot({ path: path.join(shots, 'ux-combobox-class-roofer-d.png') });
  await cls.fill('D-49');
  await p.keyboard.press('Escape');
  const lic = field(p, 'license_number');
  await lic.click();
  await p.waitForTimeout(500);
  out.subLicense = (await state(p)).options;
  await p.screenshot({ path: path.join(shots, 'ux-combobox-sub-license-d.png') });
  await p.keyboard.type('1023');
  await p.waitForTimeout(500);
  await p.keyboard.press('ArrowDown');
  await p.keyboard.press('Enter');
  const [req] = await Promise.all([p.waitForRequest((r) => r.url().endsWith('/api/tools/check_subcontractor_compliance')), p.click('main form button[type=submit]')]);
  out.classSent = JSON.parse(req.postData());

  // a job fill of a long form: no suggestion request, no list open
  const before = out.urls.length;
  await tool(p, 'project_setup_checklist', `?project=${elm.id}`);
  await p.waitForTimeout(800);
  out.fill = { suggests: out.urls.slice(before).filter((u) => u.includes('/api/suggest')).length,
    open: await p.$$eval('[role=listbox]:not([hidden])', (l) => l.length), tagged: await p.$$eval('.field-tag:not([hidden])', (t) => t.length) };
  await p.goto(base + `/#/project/${elm.id}`);       // something to remember (Search's recents)
  await p.waitForSelector('main h1');

  // ---- phone: a pointer pick keeps the focus; the list fits above a keyboard (viewport 420 px high)
  const st = await ctx.storageState();
  const mc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 375, height: 812 }, serviceWorkers: 'block', hasTouch: true, isMobile: true, storageState: st });
  const mp = await mc.newPage();
  watch(mp, 'phone');
  await tool(mp, 'valley_fever_requirements');
  const mcounty = field(mp, 'county');
  await mcounty.tap();
  await mp.keyboard.type('Ker');
  await mp.waitForTimeout(150);
  await mp.screenshot({ path: path.join(shots, 'ux-combobox-county-open-m.png') });
  await mp.locator('[role=option]:has-text("Kern")').tap();
  out.tap = await state(mp);
  out.tapFocus = await mp.evaluate(() => document.activeElement.getAttribute('role'));
  await mp.setViewportSize({ width: 375, height: 420 });
  await mcounty.fill('');
  await mcounty.evaluate((e) => e.scrollIntoView({ block: 'start' }));
  await mp.keyboard.type('S');
  await mp.waitForTimeout(150);
  out.keyboardFit = await mp.evaluate(() => {
    const i = document.activeElement; const l = document.getElementById(i.getAttribute('aria-controls'));
    const r = l.getBoundingClientRect();
    return { bottom: Math.round(r.bottom), vh: window.visualViewport ? Math.round(window.visualViewport.height) : innerHeight, max: l.style.maxHeight };
  });
  await mc.close();

  // ---- offline (service worker on): the project form's suggestions come from the saved project list
  const oc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, storageState: st });
  const op = await oc.newPage();
  watch(op, 'offline');
  await op.goto(base + '/#/projects');
  await op.waitForSelector('main h1');
  await op.evaluate(async () => { await navigator.serviceWorker.ready; });
  await op.reload();
  await op.waitForSelector('main h1');
  await op.waitForTimeout(800);
  await op.goto(base + '/#/project/new');
  await op.waitForSelector('input[name=address]');
  await op.waitForTimeout(500);
  await oc.setOffline(true);
  await op.click('input[name=address]');
  await op.keyboard.type('12');
  await op.waitForTimeout(1900);
  out.offline = await state(op);
  await oc.setOffline(false);
  await oc.close();

  // ---- company B signs in in the same tab (an invite link): nothing of company A's stays
  const bc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const bp = await bc.newPage();
  await bp.goto(base + '/#/login');
  await call(bp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Bay Builders', email: 'b@example.test', password: 'correct-horse-battery' } });
  const inv = (await call(bp, '/api/users', { method: 'POST', body: { email: 'office@example.test', role: 'office' } })).data;
  await bc.close();
  await tool(p, 'verify_contractor_license');
  await field(p, 'license_number').click();
  await p.waitForTimeout(500);
  out.aBefore = (await state(p)).options;                 // loaded into memory for company A
  await p.goto(base + '/#/invite/' + inv.invite_path.split('/').pop());
  await p.waitForSelector('input[name=pw]');
  await p.fill('input[name=pw]', 'office-horse-battery');
  await p.fill('input[name=pw_again]', 'office-horse-battery');
  await p.click('main button[type=submit]');
  await p.waitForSelector('main h1:has-text("Welcome")');
  out.companyB = await p.textContent('#company');
  await tool(p, 'verify_contractor_license');
  await field(p, 'license_number').click();
  await p.waitForTimeout(600);
  out.bOptions = (await state(p)).options;
  await p.keyboard.press('Control+k');
  await p.waitForSelector('#palette[open]');
  await p.waitForTimeout(400);
  out.bContinue = await p.$$eval('#pal-list [role=option]', (o) => o.map((x) => x.textContent));
  out.bPrefill = await p.evaluate(() => document.querySelectorAll('.field-tag:not([hidden])').length);
  await ctx.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_combobox_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "combobox.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["signup"] == 200
    # type, Down, Enter
    assert out["typed"]["open"] and out["typed"]["expanded"] == "true" and out["typed"]["options"][0].startswith("San Diego")
    assert out["down"]["active"].startswith("San Diego") and out["down"]["activeShown"]
    assert out["picked"]["value"] == "San Diego" and not out["picked"]["open"] and out["picked"]["expanded"] == "false"
    assert out["escaped"]["value"] == "Sac" and not out["escaped"]["open"]          # Escape closes and keeps the text
    assert out["tabbed"] == "Fres"                                                  # Tab moves on without picking
    assert re.fullmatch(r"\d+ suggestions?|No match", out["status"]["status"])
    # a strict field: "Did you mean", the typed text stays, the check doesn't take it
    assert out["dym"] and "Did you mean" in out["dym"] and "San Diego" in out["dym"]
    assert out["strictError"] == "Pick one from the list" and out["dymFixed"] == "San Diego"
    # a class field that isn't strict: suggestions, and what was typed goes as typed
    assert out["roofer"] and out["roofer"][0].startswith("C-39 Roofing")
    assert out["classSent"]["required_classification"] == "D-49" and out["classSent"]["license_number"] == "1023456"
    assert any(o.startswith("1023456 · Ace Roofing") for o in out["subLicense"])
    # a job fill: no request for suggestions, nothing opens
    assert out["fill"]["suggests"] == 0 and out["fill"]["open"] == 0 and out["fill"]["tagged"] >= 5
    # phone: a tap picks and the box keeps the focus; the list ends above the keyboard
    assert out["tap"]["value"] == "Kern" and out["tapFocus"] == "combobox"
    assert out["keyboardFit"]["bottom"] <= out["keyboardFit"]["vh"]
    # offline: the saved project list still suggests the company's addresses
    assert out["offline"]["options"] and out["offline"]["options"][0].startswith("1200 Elm St")
    assert out["offline"]["status"] == "Showing saved suggestions, you're offline"
    # no typed words in any address
    for word in ("san d", "san%20d", "Deigo", "roofer", "1023", "Ker", "Fres"):
        assert not any(word.lower() in u.lower().split("#")[0] for u in out["urls"] if "/static/" not in u), word
    # company B in the same tab: none of company A's suggestions, searches or fills
    assert any("Ace Roofing" in o for o in out["aBefore"])
    assert out["companyB"] == "Bay Builders"
    assert not any("Ace Roofing" in o or "Elm St" in o for o in out["bOptions"] + out["bContinue"])
    assert out["bPrefill"] == 0
