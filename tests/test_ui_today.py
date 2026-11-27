"""Today (spec 1.8) and the project hub (1.9) in a real browser: what needs you now and the one action each item
takes; the job picker that starts empty (the last job used is a chip, never picked by itself) and is the only thing
that adds ?project= to a tile; the city tile follows the cities TradeComply has rules for; the pinned status boxes,
deadline list and Getting started place; the field crew's version; Today offline (service worker on). Also the
v8-shaped /api/tools a browser can still hold in its cache right after this release (spec 1.10, "Old cached data").
Runs when Node with Playwright and its Chromium is installed (skipped otherwise). TC_UI_SHOTS=<directory> keeps the
screenshots (today-*.png)."""
import json
import os
import subprocess

from tests.test_integrations_ui import live_server, playwright_env  # noqa: F401  (fixtures)

SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [] };
  const opts = { deviceScaleFactor: 1, serviceWorkers: 'block' };
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); }); };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const iso = (days) => new Date(Date.now() + days * 864e5).toISOString().slice(0, 10);
  const today = async (p) => { await p.goto(base + '/#/dashboard'); await p.waitForSelector('main h1'); await p.waitForSelector('.task-tile'); await p.waitForTimeout(300); };
  const tiles = (p) => p.$$eval('a.task-tile', (as) => as.map((a) => ({ label: a.querySelector('.tt-label').textContent, hint: a.querySelector('.tt-hint').textContent, href: a.getAttribute('href') })));

  const ctx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const p = await ctx.newPage();
  watch(p, 'owner');
  await p.goto(base + '/#/login');
  out.signup = (await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: 'Today Builders', email: 'today@example.test', password: 'correct-horse-battery' } })).status;
  const elm = (await call(p, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', claimant_role: 'subcontractor',
    first_furnishing_date: iso(-12), last_furnishing_date: iso(-2) } })).data;
  const kern = (await call(p, '/api/projects', { method: 'POST', body: { name: 'Kern Solar Yard', city: 'Bakersfield', county: 'Kern', public_or_private: 'public' } })).data;
  const sub = (await call(p, '/api/subs', { method: 'POST', body: { name: 'Ace Roofing', coi_expiry: iso(9), project_id: elm.id } })).data;
  const me = (await call(p, '/api/me')).data;
  out.ids = { elm: elm.id, kern: kern.id, sub: sub.id };

  // ---- the last job used on this device is offered, never picked
  await p.evaluate(([k, v]) => localStorage.setItem(k, v), [`tc.lastJob.${me.tenant.id}.${me.user.id}`, String(elm.id)]);
  await today(p);
  out.pinned = await p.evaluate(() => {
    const m = document.querySelector('main > div');
    return { gsFirst: [...m.children].findIndex((c) => c.classList.contains('gs')), headFirst: m.children[0].classList.contains('page-head'),
      tilesInHealth: !!document.querySelector('.health .tiles .tile.amber'), dlInHealth: !!document.querySelector('.health section[aria-labelledby=dl-h]'),
      statusInTasks: document.querySelectorAll('.task-tiles .tile, .task-tiles.tiles').length, banners: document.querySelectorAll('main .banner').length,
      h1: document.querySelector('main h1').textContent, title: document.title };
  });
  out.needs = await p.$$eval('.needs-row', (rows) => rows.map((r) => ({ title: r.querySelector('h3').textContent, badge: r.querySelector('.badge').textContent,
    act: r.querySelector('.needs-act a') ? [r.querySelector('.needs-act a').textContent, r.querySelector('.needs-act a').getAttribute('href')] : null })));
  out.start = { job: await p.inputValue('select[name=today_job]'), chip: await p.$$eval('.job-pick .chip', (c) => c.map((x) => x.textContent)), tiles: await tiles(p) };
  await p.screenshot({ path: path.join(shots, 'today-owner-data.png'), fullPage: true });
  await p.click('.job-pick .chip');
  out.picked = { job: await p.inputValue('select[name=today_job]'), tiles: await tiles(p), chip: await p.$$eval('.job-pick .chip', (c) => c.length) };
  await p.selectOption('select[name=today_job]', String(kern.id));
  out.kern = { tiles: await tiles(p), note: await p.$$eval('.task-tiles .task-note', (n) => n.map((x) => x.textContent)) };
  // a tile with no job picked opens its check with nothing filled in
  await p.selectOption('select[name=today_job]', '');
  await Promise.all([p.waitForSelector('main form'), p.click('a.task-tile:has-text("Lien and prelim dates")')]);
  await p.waitForSelector('select[name=fill_project]');
  await p.waitForTimeout(300);
  out.noJob = { hash: await p.evaluate(() => location.hash), sel: await p.inputValue('select[name=fill_project]'),
    dates: await p.$$eval('main form input[type=date]', (i) => i.map((x) => x.value).filter(Boolean)) };
  // ... and with a job picked, from that job
  await today(p);
  await p.selectOption('select[name=today_job]', String(elm.id));
  await Promise.all([p.waitForSelector('main form'), p.click('a.task-tile:has-text("Lien and prelim dates")')]);
  await p.waitForFunction(() => document.querySelector('select[name=fill_project]') && document.querySelector('select[name=fill_project]').value !== '');
  out.withJob = { hash: await p.evaluate(() => location.hash), sel: await p.inputValue('select[name=fill_project]'),
    dates: await p.$$eval('main form input[type=date]', (i) => i.map((x) => x.value).filter(Boolean)) };
  // Needs you now -> Open sub: that sub's row, marked
  await today(p);
  await p.click('.needs-row a.btn:has-text("Open sub")');
  await p.waitForSelector('tr.row-highlight');
  out.subRow = await p.evaluate(() => ({ hash: location.hash, id: document.querySelector('tr.row-highlight').id }));
  // the project hub: its checks open with ?project=; city rows only for a covered city
  await p.goto(base + `/#/project/${elm.id}`);
  await p.waitForSelector('main h1');
  out.hubElm = await p.$$eval('section[aria-labelledby=pc-h] a.list-row', (as) => as.map((a) => [a.textContent.trim(), a.getAttribute('href')]));
  out.hubElmTop = await p.evaluate(() => ({ crumbs: [...document.querySelectorAll('.crumbs li')].map((l) => l.textContent), lc: !!document.getElementById('lc-h') }));
  await p.goto(base + `/#/project/${kern.id}`);
  await p.waitForSelector('main h1');
  out.hubKern = { rows: await p.$$eval('section[aria-labelledby=pc-h] a.list-row', (as) => as.map((a) => a.textContent.trim())),
    note: await p.textContent('section[aria-labelledby=pc-h]'), callout: await p.$$eval('.callout', (c) => c.map((x) => x.textContent)) };
  const ownerState = await ctx.storageState();
  const inv = (await call(p, '/api/users', { method: 'POST', body: { email: 'crew@example.test', role: 'field' } })).data;
  await ctx.close();

  // ---- a field user: six field tiles, the status boxes, no office buttons, no checklist
  const fc = await browser.newContext({ ...opts, viewport: { width: 375, height: 812 } });
  const fp = await fc.newPage();
  watch(fp, 'field');
  await fp.goto(base + '/#/login');
  out.join = (await call(fp, '/api/auth/invite/accept', { method: 'POST', body: { token: inv.invite_path.split('/').pop(), password: 'field-horse-battery' } })).status;
  await today(fp);
  out.field = { tiles: (await tiles(fp)).map((t) => t.label), statusTiles: await fp.$$eval('.tiles .tile', (t) => t.length), gs: await fp.$$eval('.gs', (g) => g.length),
    run: await fp.$$eval('main a[href="#/tools"].btn', (a) => a.length), purpose: await fp.textContent('main .purpose'),
    needs: await fp.$$eval('.needs-row h3', (h) => h.map((x) => x.textContent)) };
  await fp.screenshot({ path: path.join(shots, 'today-field.png'), fullPage: true });
  await fc.close();

  // ---- offline (service worker on): the header, the job picker from the saved project list and the tiles still work
  const oc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 375, height: 812 }, storageState: ownerState });
  const offp = await oc.newPage();
  watch(offp, 'offline');
  await offp.goto(base + '/#/dashboard');
  await offp.waitForSelector('.task-tile');
  await offp.evaluate(async () => { await navigator.serviceWorker.ready; });
  await offp.reload();
  await offp.waitForSelector('.task-tile');
  await offp.waitForTimeout(800);
  await oc.setOffline(true);
  await offp.reload();
  await offp.waitForSelector('main h1', { timeout: 15000 });
  await offp.waitForTimeout(800);
  out.offline = await offp.evaluate(() => ({ h1: document.querySelector('main h1').textContent, tiles: document.querySelectorAll('a.task-tile').length,
    jobs: [...document.querySelectorAll('select[name=today_job] option')].map((o) => o.textContent), text: document.querySelector('main').innerText,
    net: !document.getElementById('net').hidden }));
  await offp.screenshot({ path: path.join(shots, 'today-offline-m.png') });
  await oc.setOffline(false);
  await oc.close();

  // ---- a browser still holding the v8-shaped /api/tools (no groups, common, summary or layout) in its cache
  const vc = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 }, storageState: ownerState });
  const vp = await vc.newPage();
  watch(vp, 'v8');
  await vp.route('**/api/tools', async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    delete json.groups; delete json.common;
    for (const t of json.tools) { for (const k of ['summary', 'layout', 'group', 'group_label', 'hidden']) delete t[k]; }
    await route.fulfill({ response: resp, json });
  });
  await vp.goto(base + '/#/tools');
  await vp.waitForSelector('.tool-list details');
  out.v8 = { groups: await vp.$$eval('.tool-list details summary .tg-label', (s) => s.map((x) => x.textContent)), rows: await vp.$$eval('.tool-list a.list-row', (a) => a.length),
    common: await vp.$$eval('.common-checks', (c) => c.length) };
  await vp.goto(base + '/#/tools/calculate_lien_deadline');
  await vp.waitForSelector('main form button[type=submit]');
  out.v8tool = await vp.evaluate(() => ({ h1: document.querySelector('main h1').textContent, crumbs: [...document.querySelectorAll('.crumbs li')].map((l) => l.textContent) }));
  await today(vp);
  out.v8today = (await tiles(vp)).length;
  await vc.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""

OWNER_TILES = ["Lien and prelim dates", "Check a sub", "Verify a license", "Is a permit needed?", "Check a homeowner contract",
               "Can we work today?", "Start a toolbox talk", "Find a licensed professional"]
FIELD_TILES = ["Start a toolbox talk", "Daily inspection", "Someone got hurt", "Can we work today?", "Wildfire smoke", "Look up a safety rule"]


def test_today_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "today.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["signup"] == 200 and out["join"] == 200
    ids = out["ids"]
    pin = out["pinned"]
    assert pin == {"gsFirst": 1, "headFirst": True, "tilesInHealth": True, "dlInHealth": True, "statusInTasks": 0, "banners": 0,
                   "h1": "Today", "title": "Today · TradeComply CA"}
    # one action per item, each naming its own job or sub
    acts = {n["title"]: n["act"] for n in out["needs"]}
    assert acts["Serve 20-day preliminary notice"] == ["Open lien check", f"#/tools/calculate_lien_deadline?project={ids['elm']}"]
    assert acts["Sub insurance certificate (COI) expires"] == ["Open sub", f"#/subs?sub={ids['sub']}"]
    talks = [n["act"] for n in out["needs"] if n["act"] and n["act"][0] == "Start toolbox talk"]
    assert talks and all(a[1] in (f"#/field?project={ids['elm']}&kind=toolbox_talk", f"#/field?project={ids['kern']}&kind=toolbox_talk") for a in talks)
    assert len(out["needs"]) <= 5 and all(n["badge"] for n in out["needs"])
    # the picker starts empty though a job was used here before; the chip offers it; only a picked job adds ?project=
    st = out["start"]
    assert st["job"] == "" and st["chip"] == ["Last used: Elm St ADU · Use it"]
    assert [t["label"] for t in st["tiles"]] == OWNER_TILES and not any("project=" in t["href"] for t in st["tiles"])
    assert st["tiles"][5]["hint"] == "City construction hours (8 cities so far)"
    pk = out["picked"]
    assert pk["job"] == str(ids["elm"]) and pk["chip"] == 0
    hrefs = {t["label"]: t["href"] for t in pk["tiles"]}
    assert hrefs["Lien and prelim dates"] == f"#/tools/calculate_lien_deadline?project={ids['elm']}"
    assert hrefs["Start a toolbox talk"] == f"#/field?kind=toolbox_talk&project={ids['elm']}"
    assert hrefs["Check a sub"] == "#/tools/check_subcontractor_compliance" and hrefs["Verify a license"] == "#/tools/verify_contractor_license"
    assert pk["tiles"][5]["hint"] == "Construction hours in San Diego"
    # a job in a city TradeComply has no rules for: a plain line, no link
    assert out["kern"]["note"] == ["TradeComply has city rules for 8 cities; Bakersfield isn't one yet."]
    assert not any("construction_hours" in t["href"] for t in out["kern"]["tiles"])
    assert out["noJob"] == {"hash": "#/tools/calculate_lien_deadline", "sel": "", "dates": []}
    assert out["withJob"]["hash"] == f"#/tools/calculate_lien_deadline?project={ids['elm']}" and out["withJob"]["sel"] == str(ids["elm"])
    assert len(out["withJob"]["dates"]) >= 2
    assert out["subRow"] == {"hash": f"#/subs?sub={ids['sub']}", "id": f"sub-{ids['sub']}"}
    # the project hub
    hub = dict(out["hubElm"])
    assert hub["Lien and prelim dates"] == f"#/tools/calculate_lien_deadline?project={ids['elm']}"
    assert hub["Work hours in San Diego"] == f"#/tools/construction_hours?project={ids['elm']}"
    assert hub["Find a professional near this job"] == f"#/professionals?project={ids['elm']}"
    assert "Public works readiness" not in hub
    assert out["hubElmTop"] == {"crumbs": ["Projects", "Elm St ADU"], "lc": True}
    k = out["hubKern"]
    assert "Public works readiness" in k["rows"] and not any(x.startswith("Work hours") for x in k["rows"])
    assert "TradeComply has city rules for 8 cities; Bakersfield isn't one yet." in k["note"]
    assert k["callout"] and "first and last furnishing dates" in k["callout"][0]
    # field crews
    f = out["field"]
    assert f["tiles"] == FIELD_TILES and f["statusTiles"] == 3 and f["gs"] == 0 and f["run"] == 0
    assert f["purpose"] == "Your job today: safety talks, logs and quick checks."
    assert all("toolbox" in n.lower() for n in f["needs"])
    # offline
    o = out["offline"]
    assert o["h1"] == "Today" and o["tiles"] == 8 and o["jobs"][0] == "Pick a job" and sorted(o["jobs"][1:]) == ["Elm St ADU — San Diego", "Kern Solar Yard — Bakersfield"]
    assert "Deadlines need a connection. Your tiles and saved logs still work." in o["text"] and "Something went wrong" not in o["text"] and o["net"]
    # v8-shaped tool list: the old modules, titles and descriptions still draw
    assert len(out["v8"]["groups"]) >= 10 and out["v8"]["rows"] >= 70 and out["v8"]["common"] == 0
    assert out["v8tool"]["h1"] == "Calculate lien deadline" and out["v8tool"]["crumbs"][0] == "Checks"
    assert out["v8today"] == 8
