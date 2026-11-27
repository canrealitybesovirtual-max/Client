"""Search (the command palette, spec 4) in a real browser: Ctrl+K from any page and from inside a text box (Cmd+K
only on a Mac, where Ctrl+K in a text box is left alone), the top bar button by keyboard and by touch, Esc giving the
focus back, the <dialog> outside .app, grouped results with headings, the seven sample searches giving the same
first result as the API, Enter and Ctrl+Enter, "Search the rulebook" from Checks, offline results from this device,
the subscription row instead of rules, recents gone after Log out, and on a phone: Search taking the Help sheet's
place, and Search -> "Ask TradeComply" -> Back closing Help and staying on the page (one history entry). Runs
against a server whose Ask TradeComply is answered by the fake Anthropic API (tests/assistant_ui_app.py), when Node
with Playwright and its Chromium is installed (skipped otherwise). TC_UI_SHOTS=<directory> keeps the screenshots."""
import json
import os
import subprocess
import sys

import pytest

from tests.test_integrations_ui import playwright_env  # noqa: F401  (fixture)
from tests.test_ui_assistant import _serve

QUERIES = {"prelim": ("Checks", "Calculate lien deadline"), "stop notice": ("Checks", "Stop payment notice deadline"),
           "coi": ("Checks", "Check subcontractor compliance"), "can I work saturday": ("Checks", "Construction hours"),
           "C10": ("License classes", "C-10"), "porta potty": ("Checks", "Generate safety programs"), "granny flat": ("Checks", "ADU rules")}


@pytest.fixture
def server(tmp_path, playwright_env):
    proc, base = _serve(tmp_path, "pal", ASSISTANT_DAILY_MESSAGES="20")
    try:
        yield base, str(tmp_path / "pal")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
const { execFileSync } = require('child_process');
(async () => {
  const [base, shots, dataDir, queries] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], urls: [] };
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); });
    p.on('request', (r) => out.urls.push([r.method(), r.url()])); };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const open = (p) => p.evaluate(() => !!document.querySelector('#palette[open]'));
  const groups = (p) => p.$$eval('#pal-list > li > ul[role=group]', (gs) => gs.map((g) => ({ label: document.getElementById(g.getAttribute('aria-labelledby')).textContent,
    items: [...g.querySelectorAll('[role=option] .pal-title, .pal-note')].map((x) => x.textContent) })));
  const search = async (p, q) => { await p.fill('#pal-q', q); await p.waitForTimeout(900); return groups(p); };
  const page = async (p, hash, sel = 'main h1') => { await p.goto(base + '/' + hash); await p.waitForSelector(sel); await p.waitForTimeout(300); };

  const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const p = await ctx.newPage();
  watch(p, 'desk');
  await p.goto(base + '/#/login');
  out.signup = (await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: 'Palette Builders', email: 'pal@example.test', password: 'correct-horse-battery' } })).status;
  const elm = (await call(p, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego' } })).data;
  await page(p, `#/project/${elm.id}`);
  // Ctrl+K from a page; the dialog sits outside .app, right after the Help panel
  await p.keyboard.press('Control+k');
  await p.waitForSelector('#palette[open]');
  await p.waitForTimeout(500);
  out.where = await p.evaluate(() => { const d = document.getElementById('palette'); return { inApp: !!d.closest('.app'), after: d.previousElementSibling.id, modal: d.matches(':modal') }; });
  out.empty = await groups(p);
  await p.screenshot({ path: path.join(shots, 'ux-palette-empty-d.png') });
  out.headings = await p.$$eval('#pal-list ul[role=group]', (gs) => gs.every((g) => { const h = document.getElementById(g.getAttribute('aria-labelledby')); return h && h.getAttribute('role') === 'presentation'; }));
  out.queries = {};
  for (const [q, [label]] of Object.entries(JSON.parse(queries))) {
    const g = (await search(p, q)).find((x) => x.label === label);
    out.queries[q] = g ? g.items[0] : null;
    if (q === 'prelim') await p.screenshot({ path: path.join(shots, 'ux-palette-prel-d.png') });
  }
  // keys: Down moves, Enter opens, Esc closes and gives the focus back
  await search(p, 'granny flat');
  await p.keyboard.press('Enter');
  await p.waitForSelector('main h1:has-text("ADU")');
  out.enter = { hash: await p.evaluate(() => location.hash), open: await open(p) };
  await p.focus('#search-open');
  await p.keyboard.press('Enter');
  await p.waitForSelector('#palette[open]');
  await p.keyboard.press('Escape');
  out.esc = { open: await open(p), focus: await p.evaluate(() => document.activeElement.id), dialogs: await p.$$eval('dialog', (d) => d.length),
    kbd: await p.textContent('.top-kbd'), shortcut: await p.getAttribute('#search-open', 'aria-keyshortcuts') };
  // Ctrl+Enter: a new tab
  await p.keyboard.press('Control+k');
  await search(p, 'prelim');
  const [tab] = await Promise.all([ctx.waitForEvent('page'), p.keyboard.press('Control+Enter')]);
  await tab.waitForLoadState();
  out.newTab = tab.url().replace(base, '');
  await tab.close();
  await p.keyboard.press('Escape');
  // from inside a text box
  await page(p, '#/field', 'textarea[name=notes]');
  await p.fill('textarea[name=notes]', 'Talked about ladders');
  await p.focus('textarea[name=notes]');
  await p.keyboard.press('Control+k');
  await p.waitForSelector('#palette[open]');
  out.fromTextarea = await p.evaluate(() => document.querySelector('textarea[name=notes]').value);
  await p.keyboard.press('Escape');
  // "Search the rulebook" from Checks when no check matches
  await page(p, '#/tools', '#check-q');
  await p.fill('#check-q', 'zzqxv');
  await p.waitForSelector('button:has-text("Search the rulebook for")');
  await p.click('button:has-text("Search the rulebook for")');
  await p.waitForSelector('#palette[open]');
  await p.waitForTimeout(900);
  out.rulebook = { q: await p.inputValue('#pal-q'), scope: await p.textContent('.pal-scope') };
  await p.click('.pal-scope button');
  await p.waitForTimeout(900);
  out.rulebook.widened = await p.textContent('.pal-scope');
  await p.keyboard.press('Escape');
  // recents: gone after Log out
  await page(p, `#/project/${elm.id}`);
  await p.keyboard.press('Control+k');
  await p.waitForTimeout(600);
  out.recentBefore = (await groups(p)).find((g) => g.label === 'Continue');
  await p.keyboard.press('Escape');
  await p.click('#account-open');
  await p.click('#account-menu .menu-logout');
  await p.waitForSelector('input[name=email]');
  out.recentKeys = await p.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('tc.recent.')));
  await call(p, '/api/auth/login', { method: 'POST', body: { email: 'pal@example.test', password: 'correct-horse-battery' } });
  await page(p, '#/dashboard');
  await p.keyboard.press('Control+k');
  await p.waitForTimeout(600);
  out.recentAfter = (await groups(p)).find((g) => g.label === 'Continue') || null;
  await p.keyboard.press('Escape');
  // the company's subscription ended: one row instead of the rules
  execFileSync(process.env.TC_PYTHON || 'python3', ['-c', `import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); con.execute("update tenants set trial_ends='2020-01-01T00:00:00+00:00' where id=(select tenant_id from users where email=?)", (sys.argv[2],)); con.commit()`, path.join(dataDir, 'tradecomply.sqlite'), 'pal@example.test']);
  await page(p, '#/tools', '#check-q');
  await p.keyboard.press('Control+k');
  out.unpaid = (await search(p, 'prelim')).find((g) => g.label === 'Rules');
  await p.keyboard.press('Escape');
  await ctx.close();

  // ---- a Mac: only Cmd+K; Ctrl+K in a text box keeps its own meaning
  const mac = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  await mac.addInitScript(() => { Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' }); Object.defineProperty(navigator, 'userAgentData', { get: () => undefined }); });
  const mp = await mac.newPage();
  watch(mp, 'mac');
  await mp.goto(base + '/#/login');
  await call(mp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Mac Builders', email: 'mac@example.test', password: 'correct-horse-battery' } });
  await page(mp, '#/field', 'textarea[name=notes]');
  await mp.evaluate(() => { window.__prevented = null; document.addEventListener('keydown', (e) => { if (e.key === 'k') window.__prevented = e.defaultPrevented; }); });
  await mp.focus('input[name=topic]');
  await mp.keyboard.press('Control+k');
  out.macCtrl = { open: await open(mp), prevented: await mp.evaluate(() => window.__prevented) };
  await mp.keyboard.press('Meta+k');
  out.macCmd = { open: await open(mp), kbd: await mp.textContent('.top-kbd'), shortcut: await mp.getAttribute('#search-open', 'aria-keyshortcuts') };
  await mac.close();

  // ---- a phone: a tap opens it full screen; offline it searches what is kept here; Search and Help share one history entry
  const pc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });
  const ph = await pc.newPage();
  watch(ph, 'phone');
  await ph.goto(base + '/#/login');
  await call(ph, '/api/auth/signup', { method: 'POST', body: { company_name: 'Phone Search', email: 'phs@example.test', password: 'correct-horse-battery' } });
  await call(ph, '/api/projects', { method: 'POST', body: { name: 'Kern Solar Yard', city: 'Bakersfield', county: 'Kern' } });
  await page(ph, '#/projects');
  await ph.evaluate(async () => { await navigator.serviceWorker.ready; });
  await ph.reload();
  await ph.waitForSelector('main h1');
  await ph.waitForTimeout(800);
  await ph.tap('#search-open');
  await ph.waitForSelector('#palette[open]');
  out.phoneBox = await ph.$eval('#palette', (d) => { const r = d.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; });
  await ph.waitForTimeout(500);
  await ph.screenshot({ path: path.join(shots, 'ux-palette-empty-m.png') });
  await ph.keyboard.press('Escape');
  await pc.setOffline(true);
  await ph.tap('#search-open');
  out.offline = { lien: await search(ph, 'lien'), kern: await search(ph, 'kern'), status: '' };
  await ph.waitForTimeout(1500);
  out.offline.status = await ph.textContent('#palette [role=status]');
  await ph.screenshot({ path: path.join(shots, 'ux-palette-offline-m.png') });
  await ph.keyboard.press('Escape');
  await pc.setOffline(false);
  await page(ph, '#/projects');
  const start = await ph.evaluate(() => [location.hash, history.length]);
  await ph.tap('#ask-open');
  await ph.waitForSelector('#ask-panel:not([hidden])');
  await ph.keyboard.press('Control+k');
  await ph.waitForSelector('#palette[open]');
  out.swap = { askHidden: await ph.$eval('#ask-panel', (a) => a.hidden), inert: await ph.$eval('.app', (a) => a.inert) };
  await ph.goBack();
  await ph.waitForTimeout(400);
  out.swap.afterBack = { open: await open(ph), hash: await ph.evaluate(() => location.hash) };
  await ph.tap('#search-open');
  await search(ph, 'when is my lien due');
  const sends = () => out.urls.filter(([m, u]) => m === 'POST' && u.includes('/messages')).length;
  const before = sends();
  await ph.tap('#pal-list [role=option]:has-text("Ask TradeComply")');
  await ph.waitForSelector('#ask-panel:not([hidden])');
  out.toAsk = { text: await ph.inputValue('#ask-panel textarea'), sent: sends() - before, palette: await open(ph) };
  await ph.goBack();
  await ph.waitForTimeout(500);
  out.toAsk.afterBack = { ask: await ph.$eval('#ask-panel', (a) => a.hidden), hash: await ph.evaluate(() => location.hash), length: await ph.evaluate(() => history.length) };
  // Search -> "Ask Quick help: …": the question is asked at once on the Quick help tab; Back closes Help, same page
  await ph.tap('#search-open');
  await search(ph, 'when is my prelim due');
  await ph.tap('#pal-list [role=option]:has-text("Ask Quick help")');
  await ph.waitForSelector('#ask-panel:not([hidden]) .qh-turn .qh-a');
  out.toQuick = { tab: await ph.$$eval('#ask-tab-qh[aria-selected=true]', (t) => t.length), q: await ph.textContent('#ask-panel .qh-turn:last-child .qh-q'), palette: await open(ph) };
  await ph.goBack();
  await ph.waitForTimeout(500);
  out.toQuick.afterBack = { ask: await ph.$eval('#ask-panel', (a) => a.hidden), hash: await ph.evaluate(() => location.hash) };
  out.start = start;
  await pc.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_search_palette_in_a_real_browser(tmp_path, playwright_env, server):
    node, env = playwright_env
    base, data_dir = server
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "palette.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, shots, data_dir, json.dumps(QUERIES)], env={**env, "TC_PYTHON": sys.executable},
                       capture_output=True, text=True, timeout=500)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["signup"] == 200
    assert out["where"] == {"inApp": False, "after": "ask-panel", "modal": True}
    labels = [g["label"] for g in out["empty"]]
    assert labels[:2] == ["Continue", "For this page"] and "Common checks" in labels and out["headings"]
    this_page = next(g for g in out["empty"] if g["label"] == "For this page")["items"]
    assert this_page == ["Lien and prelim dates for this job", "Setup checklist for this job", "New field log for this job"]
    for q, (_, first) in QUERIES.items():
        assert out["queries"][q] and out["queries"][q].startswith(first), (q, out["queries"][q])
    assert out["enter"] == {"hash": "#/tools/adu_rules", "open": False}
    assert out["esc"] == {"open": False, "focus": "search-open", "dialogs": 0, "kbd": "Ctrl K", "shortcut": "Control+K"}
    assert out["newTab"] == "/#/tools/calculate_lien_deadline"
    assert out["fromTextarea"] == "Talked about ladders"
    assert out["rulebook"]["q"] == "zzqxv" and out["rulebook"]["scope"].startswith("Searching rules")
    assert out["rulebook"]["widened"].startswith("Searching everything")
    assert out["recentBefore"] and "Elm St ADU" in out["recentBefore"]["items"]
    assert out["recentKeys"] == [] and (out["recentAfter"] is None or "Elm St ADU" not in out["recentAfter"]["items"])
    assert out["unpaid"]["items"] == ["Rule text needs an active subscription."]
    assert out["macCtrl"] == {"open": False, "prevented": False}
    assert out["macCmd"] == {"open": True, "kbd": "⌘ K", "shortcut": "Meta+K"}
    assert out["phoneBox"] == [375, 812]
    off = out["offline"]
    assert any(g["label"] == "Checks" and g["items"] for g in off["lien"])
    assert next(g for g in off["kern"] if g["label"] == "Your projects")["items"] == ["Kern Solar Yard"]
    assert next(g for g in off["lien"] if g["label"] == "Rules")["items"] == ["Needs a connection"]
    assert off["status"].startswith("You're offline")
    assert out["swap"]["askHidden"] and out["swap"]["afterBack"] == {"open": False, "hash": "#/projects"}
    assert out["toAsk"]["text"] == "when is my lien due" and out["toAsk"]["sent"] == 0 and not out["toAsk"]["palette"]
    assert out["toAsk"]["afterBack"]["ask"] is True and out["toAsk"]["afterBack"]["hash"] == "#/projects"
    assert out["toQuick"]["tab"] == 1 and out["toQuick"]["q"] == "when is my prelim due" and not out["toQuick"]["palette"]
    assert out["toQuick"]["afterBack"] == {"ask": True, "hash": "#/projects"}
