"""Guidance in the web app (app/static): the "needs a licensed person" line under tool results, the Find a
professional page, the dashboard's Getting started checklist, the few inline field hints, and the owner's Account and
data section in Settings (download all company data, delete the company account).

Static checks run everywhere. The browser test runs when Node with Playwright and its Chromium is installed (skipped
otherwise). It drives a real server: real tool results with the handoffs the API attaches and the real finder
endpoints. Playwright route mocks add what a fresh server can't show: hostile or odd handoffs, the finder's empty, error,
offline and not-installed states, and the export and delete endpoints (answered 404 first, as before they exist). Set
TC_UI_SHOTS=<directory> to keep the screenshots (guide-*.png, 1280 and 375 px wide, deviceScaleFactor 1)."""
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


def _section(js: str, start: str, end: str) -> str:
    return js[js.index(start):js.index(end, js.index(start))]


JS = _read("app.js")
PROS = _section(JS, "// ======================================================================== licensed professionals",
                "// ======================================================================== documents")
DASH = _section(JS, "// ======================================================================== dashboard",
                "// ======================================================================== projects")
SETTINGS_DATA = _section(JS, "async function downloadCompanyData", "routes.settings = async")


@pytest.fixture
def client(tmp_path):
    from app.config import Settings
    from app.main import create_app
    app = create_app(Settings.from_env(data_dir=str(tmp_path), database_url=None, events_background=False))
    with TestClient(app) as c:
        yield c
    app.state.platform.store.close()


# ---------------------------------------------------------------------------- static checks
def test_served_assets_wire_up_the_guidance(client):
    index = client.get("/").text
    assert 'href="#/professionals" data-route="professionals" class="nav-wide"' in index and 'id="i-find"' in index
    js = client.get("/static/app.js").text
    assert "routes.professionals = async" in js and "professionals: 'Find a professional'" in js
    assert "/api/professionals/types" in js and "/api/professionals/find?" in js
    assert "handoffsLine(" in js and "gettingStartedCard(" in js and "companyDataSection()" in js
    css = client.get("/static/styles.css").text
    assert ".handoffs > summary" in css and ".gs-steps" in css and ".danger-zone" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert re.search(r"@media \(max-width: 899px\) \{ \.nav \.nav-wide \{ display: none; \} \}", css)
    sw = client.get("/sw.js").text
    version = int(re.search(r"const SHELL_CACHE = 'tc-shell-v(\d+)'", sw).group(1))
    assert version >= 4                                              # bumped for this release
    assert "'/api/professionals/types'" in sw                        # the finder's form opens offline
    assert "url.pathname === '/api/tenant/export'" in sw             # a download: never a cached copy


def test_professional_links_are_official_only_and_never_injected_as_html():
    assert "innerHTML" not in PROS and "html:" not in PROS and "insertAdjacentHTML" not in PROS
    # links open the official site in a new tab without giving it this page
    assert PROS.count("target: '_blank', rel: 'noopener noreferrer'") == 1 and "officialLink(" in PROS
    # only plain https addresses become links (a javascript:, data: or http: find_url is no link at all)
    assert r"""/^https:\/\/[a-z0-9.-]+(:\d{1,5})?([\/?#][^\s"'<>\\`]*)?$/i""" in PROS
    assert "TradeComply links to official state lookups and doesn\\'t recommend specific people." in PROS


def test_only_plain_https_addresses_become_links():
    """linkUrl() from app.js, run in Node: what the handoff line and the finder accept as a link."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    line = next(x for x in PROS.splitlines() if x.strip().startswith("const linkUrl ="))
    cases = {"https://www.cab.ca.gov/cons/archs/lic_search.shtml": True,
             "https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=SAN+DIEGOG&varType=22": True,
             " https://search.dca.ca.gov/ ": True,
             "http://www.cab.ca.gov/": False, "javascript:alert(1)": False, "JavaScript:alert(1)//https://x.gov": False,
             "data:text/html,hi": False, "//evil.example/x": False, "https://a b.gov/": False,
             'https://x.gov/"onmouseover="alert(1)': False, "https://x.gov/<script>": False,
             "https://x.gov\\@evil.example/": False, "https:javascript:alert(1)": False}
    script = line + "\nconst cases = " + json.dumps(list(cases)) + ";\nconsole.log(JSON.stringify(cases.map((u) => !!linkUrl(u))));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert dict(zip(cases, json.loads(r.stdout))) == cases


def test_guidance_never_pops_up_nags_or_overclaims():
    new_code = PROS + DASH + SETTINGS_DATA
    assert not re.search(r"openDialog|showModal|\balert\(|\bconfirm\(|\bprompt\(|setInterval", new_code)
    # the handoff line and the account deletion are collapsed until opened
    assert "h('details', { class: 'handoffs' }" in PROS and "h('details', { class: 'danger-zone' }" in SETTINGS_DATA
    lowered = JS.lower()
    for phrase in ("guarantee", "replaces your attorney", "replace your attorney", "we recommend", "top-rated",
                   "fully compliant", "best lawyer", "best engineer"):
        assert phrase not in lowered, phrase


def test_browser_storage_is_only_touched_inside_try_catch():
    assert JS.count("localStorage") == 3 and "sessionStorage" not in JS
    helper = JS[JS.index("const local = {"):JS.index("};", JS.index("const local = {"))]
    assert helper.count("try {") == 3 and helper.count("localStorage") == 3


def test_the_old_setup_banner_is_folded_into_the_checklist():
    assert "Finish setup to see your full picture" not in JS
    assert "local.set(gsKey(), 'done')" in DASH and "local.set(gsKey(), 'hidden')" in DASH
    assert "!can.write()" in DASH                                   # field staff never see it


def test_new_field_hints_are_few_and_cite_their_source():
    form = _section(JS, "function projectForm(", "routes.projects = async")
    assert "leave out finance charges (B&P 7159.5)" in form
    assert "2026_CSLB_Law_Book.pdf, read 2026-11-26" in form       # where the hint's legal fact was read
    assert "differ from the project\\'s completion date" in form
    assert form.count("hint:") <= 9                                 # 6 before this release, 2 added


# ---------------------------------------------------------------------------- browser test (Playwright)
SMOKE = r"""
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [] };
  const shot = (p, name, opts = {}) => p.screenshot({ path: path.join(shots, `guide-${name}.png`), ...opts });
  const overflow = (p) => p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  // Route mocks need the service worker out of the way (Playwright can't see requests it makes).
  const opts = { deviceScaleFactor: 1, serviceWorkers: 'block', acceptDownloads: true };
  const desk = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const page = await desk.newPage();
  page.on('pageerror', (e) => out.errors.push(String(e)));
  page.on('dialog', (d) => { out.errors.push('native dialog: ' + d.message()); d.dismiss(); });
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
      body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* not JSON */ }
    return { status: r.status, data };
  }, [url, o]);

  // ---- a new company (license given at sign-up), one project in San Diego County
  await page.goto(base + '/#/login');
  out.signup = (await call(page, '/api/auth/signup', { method: 'POST', body: { company_name: 'Guide Builders', email: 'owner@example.test', password: 'correct-horse-battery', cslb_license: '123456' } })).status;
  await page.goto(base + '/#/dashboard');
  await page.waitForSelector('.tiles');
  out.gs1 = await page.$$eval('.gs-steps a', (as) => as.map((a) => a.textContent));
  out.gsCount1 = await page.textContent('.gs-title .small');
  out.gsFirst = await page.evaluate(() => { const m = document.querySelector('main > div'); return [...m.children].findIndex((c) => c.classList.contains('gs')); });
  out.banners = await page.$$eval('main .banner', (b) => b.map((x) => x.textContent));
  await shot(page, 'dashboard');
  const proj = await call(page, '/api/projects', { method: 'POST', body: { name: 'Harbor Clinic', city: 'San Diego', county: 'San Diego County', claimant_role: 'direct_contractor',
    first_furnishing_date: '2026-10-01', last_furnishing_date: '2026-11-10' } });
  out.project = proj.status;

  // ---- the Tools page on a wide screen: one way to the finder (the sidebar), not three
  await page.goto(base + '/#/tools');
  await page.waitForSelector('.tool-list');
  out.deskToolsFinderLinks = await page.$$eval('a[href="#/professionals"]', (as) => as.filter((a) => a.offsetParent !== null).map((a) => a.closest('nav') ? 'nav' : 'main'));

  // ---- a real tool result: the handoffs the API attaches are drawn as one collapsed line under the result
  await page.goto(base + '/#/tools/calculate_lien_deadline');
  await page.waitForSelector('select[name=fill_project]');
  await page.selectOption('select[name=fill_project]', String(proj.data.id));
  const [realResp] = await Promise.all([page.waitForResponse((r) => r.url().endsWith('/api/tools/calculate_lien_deadline')), page.click('form button[type=submit]')]);
  out.realHandoffs = (await realResp.json()).handoffs || [];
  await page.waitForSelector('.result');
  out.real = await page.evaluate(() => {
    const d = document.querySelector('.result details.handoffs');
    if (!d) return null;
    const kv = document.querySelector('.result dl.kv');
    return { open: d.open, summary: d.querySelector('summary').innerText.replace(/\s+/g, ' ').trim(),
      beforeBody: !!kv && !!(d.compareDocumentPosition(kv) & Node.DOCUMENT_POSITION_FOLLOWING),
      items: [...d.querySelectorAll('.ho-item')].map((x) => ({ name: x.querySelector('.ho-name strong').textContent, unverified: !!x.querySelector('.ho-name .pill.amber'),
        link: (x.querySelector('a.ho-find') || {}).href || null, rel: (x.querySelector('a.ho-find') || {}).rel || null, target: (x.querySelector('a.ho-find') || {}).target || null,
        text: x.textContent })),
      rawShown: document.querySelector('.result').innerText.includes('Type id') || document.querySelector('.result').innerText.includes('Handoff id'),
      chevron: getComputedStyle(d.querySelector('summary'), '::after').content };
  });
  out.realSummaryName = await page.locator('details.handoffs > summary').ariaSnapshot();
  await page.focus('details.handoffs > summary');
  await page.keyboard.press('Enter');
  out.realOpened = await page.$eval('details.handoffs', (d) => d.open);
  out.dialogs = await page.$$eval('dialog', (d) => d.length);
  await page.$eval('.result', (el) => el.scrollIntoView());
  await shot(page, 'result');
  out.printOpen = await page.evaluate(() => { const d = document.querySelector('details.handoffs'); d.open = false; window.dispatchEvent(new Event('beforeprint'));
    const o = d.open; window.dispatchEvent(new Event('afterprint')); return [o, d.open]; });

  // ---- the real finder, from the result's "How to find and check one" (type and project carried over)
  await page.click('details.handoffs > summary');
  const [realFind] = await Promise.all([page.waitForResponse((r) => r.url().includes('/api/professionals/find')),
    page.click('details.handoffs .ho-item:first-child a[href^="#/professionals"]')]);
  out.realFindQuery = new URL(realFind.url()).search;
  const realFound = await realFind.json();
  await page.waitForSelector('.pf-entry');
  out.realFinder = await page.evaluate(() => ({ type: document.querySelector('#pf-type').value, county: document.querySelector('#pf-county').value,
    project: (document.querySelector('#pf-project') || {}).value, countyOptions: document.querySelectorAll('#pf-county option').length,
    nav: (document.querySelector('#nav a[aria-current="page"]') || {}).dataset.route, title: document.title, heading: document.querySelector('.pf-entry h2').textContent,
    links: [...document.querySelectorAll('.pf-entry a[target=_blank]')].map((a) => ({ href: a.getAttribute('href'), rel: a.rel })),
    status: document.querySelector('p[role=status]').textContent, text: document.querySelector('main').innerText,
    formNote: !document.querySelector('.pf-note').hidden,
    details: [...document.querySelectorAll('.pf-entry details')].map((d) => d.open) }));
  out.realFoundDirs = (realFound.directories || []).map((x) => x.url);
  out.realNeutral = realFound.neutral_note;
  await page.goto(base + '/#/professionals?type=apprenticeship_committee&county=San%20Diego');
  await page.waitForSelector('.pf-entry');
  out.dasHints = await page.$$eval('.pf-link .hint', (els) => els.map((x) => x.textContent));
  out.dasLinks = await page.$$eval('.pf-entry a.ho-find', (as) => as.map((a) => a.getAttribute('href')));
  await shot(page, 'finder', { fullPage: true });

  // ---- hostile or odd handoffs (mocked): text stays text, only http(s) links, unverified marked
  const HANDOFFS = [
    { type_id: 'attorney', name: 'Attorney (lawyer)', why: 'Test reason one.', legal_basis: 'Test basis one', verified: true,
      find_url: 'https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch', county_filtered: false },
    { type_id: 'notary_public', name: 'Notary public <img src=x onerror=alert(1)>', why: 'Test reason two.', legal_basis: 'Test basis two', verified: false,
      find_url: 'javascript:alert(1)', county_filtered: false },
    { type_id: 'apprenticeship_committee', name: 'Apprenticeship committee', why: 'Test reason three.', legal_basis: 'Test basis three', verified: false,
      find_url: 'https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=SAN+DIEGOG&varType=22', county_filtered: true,
      requirement: 'conditional', requirement_label: 'Required by law if this applies (legal basis not yet verified)', when: 'Test timing.', due_date: '2027-05-09',
      find_owner: 'Test agency', find_link_checked: false, check_how: 'Test: how to check.', also: [{ why: 'Test other reason.', legal_basis: 'Test basis four', verified: false }] }];
  await page.route('**/api/tools/calculate_lien_deadline', async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    json.handoffs = HANDOFFS;
    await route.fulfill({ response: resp, json });
  });
  await page.goto(base + '/#/tools/calculate_lien_deadline');
  await page.waitForSelector('select[name=fill_project]');
  await page.selectOption('select[name=fill_project]', String(proj.data.id));
  await page.click('form button[type=submit]');
  await page.waitForSelector('.result details.handoffs');
  out.ho = await page.$eval('details.handoffs', (d) => ({ open: d.open, summary: d.querySelector('summary').innerText.replace(/\s+/g, ' ').trim() }));
  await page.click('details.handoffs > summary');
  out.hoRowsClosed = await page.$$eval('details.handoffs details.ho-more', (ds) => ds.map((d) => d.open));
  for (let i = 1; i <= 3; i++) await page.click(`details.handoffs .ho-item:nth-child(${i}) details.ho-more > summary`);
  out.hoLinks = await page.$$eval('details.handoffs a[target=_blank]', (as) => as.map((a) => ({ href: a.href, rel: a.rel, text: a.innerText.replace(/\s+/g, ' ').trim() })));
  out.hoItems = await page.$$eval('details.handoffs .ho-item', (li) => li.map((x) => ({ name: x.querySelector('.ho-name strong').textContent,
    unverified: !!x.querySelector('.ho-name .pill.amber'), pills: [...x.querySelectorAll('.ho-name .pill')].map((p) => p.firstChild.textContent),
    hint: (x.querySelector('.hint') || {}).textContent || '', text: x.innerText,
    finder: x.querySelector('a[href^="#/professionals"]') ? x.querySelector('a[href^="#/professionals"]').getAttribute('href') : null })));
  out.hoNote = await page.textContent('details.handoffs .ho-note');
  out.injected = await page.$$eval('main img, a[href^="javascript:"]', (els) => els.length);
  await page.$eval('details.handoffs', (el) => el.scrollIntoView());
  await shot(page, 'result-handoffs');

  // ---- finder edge cases (mocked): another response shape, empty, error, offline, not on this server
  const TYPES = { types: [{ type_id: 'attorney', name: 'Attorney (lawyer)', plain_description: 'Test attorney description. Second sentence.' },
    { type_id: 'apprenticeship_committee', name: 'Apprenticeship committee', plain_description: 'Test committee description.' },
    { type_id: 'notary_public', name: 'Notary public', plain_description: 'Test notary description.' }] };
  let typesMode = 'ok';
  let findMode = 'ok';
  out.findQueries = [];
  await page.route('**/api/professionals/types*', (route) => typesMode === 'ok' ? route.fulfill({ json: TYPES }) : route.fulfill({ status: 404, json: { detail: 'Not Found' } }));
  await page.route('**/api/professionals/find*', (route) => {
    out.findQueries.push(new URL(route.request().url()).search);
    if (findMode === 'error') return route.fulfill({ status: 500, json: { detail: 'The lookup failed (test).' } });
    if (findMode === 'empty') return route.fulfill({ json: { results: [] } });
    return route.fulfill({ json: { results: [{ type_id: 'apprenticeship_committee', name: 'Apprenticeship committee', plain_description: 'Test committee description.',
      find: [{ label: 'DIR public works apprenticeship search', url: 'https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=SAN+DIEGOG&varType=22',
        official_owner: 'California Department of Industrial Relations', county_filtered: true, status: 'reachable' },
      { label: 'DIR apprenticeship program search', url: 'https://www.dir.ca.gov/databases/das/aigstart.asp', official_owner: 'California Department of Industrial Relations',
        county_filtered: false, status: 'blocked:test' }, { label: 'Not a link', url: 'data:text/html,hi' }],
      verify_license_how: 'Test: how to check one.', bring_or_ask: ['Bring: a test item.'],
      when_required: [{ situation: 'Test situation.', legal_basis: 'Test basis', status: 'unconfirmed' }] }] } });
  });
  await page.click('details.handoffs .ho-item:nth-child(3) a[href^="#/professionals"]');
  await page.waitForSelector('.pf-entry');
  out.finder = await page.evaluate(() => ({
    type: document.querySelector('#pf-type').value, county: document.querySelector('#pf-county').value,
    project: (document.querySelector('#pf-project') || {}).value, countyOptions: document.querySelectorAll('#pf-county option').length,
    typeHint: document.querySelector('#pf-type-h').textContent,
    links: [...document.querySelectorAll('.pf-entry a[target=_blank]')].map((a) => ({ href: a.getAttribute('href'), rel: a.rel })),
    hints: [...document.querySelectorAll('.pf-link .hint')].map((x) => x.textContent), status: document.querySelector('p[role=status]').textContent,
    notes: [...document.querySelectorAll('main p')].filter((p) => p.offsetParent !== null && p.textContent.includes("doesn't recommend specific people")).length,
    details: [...document.querySelectorAll('.pf-entry details')].map((d) => d.open) }));
  findMode = 'empty';
  await page.click('form button[type=submit]');
  await page.waitForSelector('.pf-results .empty');
  out.finderEmpty = await page.textContent('.pf-results .empty');
  findMode = 'error';
  await page.click('form button[type=submit]');
  await page.waitForSelector('.pf-results .banner.error');
  out.finderError = await page.textContent('.pf-results .banner.error');
  out.finderRetry = await page.$$eval('.pf-results .banner.error button', (b) => b.map((x) => x.textContent));
  await page.unroute('**/api/professionals/find*');
  await desk.setOffline(true);
  await page.click('form button[type=submit]');
  await page.waitForSelector('.pf-results .banner.warn');
  out.finderOffline = await page.textContent('.pf-results .banner.warn');
  await desk.setOffline(false);
  await page.selectOption('#pf-type', '');
  await page.click('form button[type=submit]');
  out.finderNoType = await page.textContent('.pf-results .banner.error');
  typesMode = '404';
  await page.goto(base + '/#/dashboard');
  await page.goto(base + '/#/professionals');
  await page.waitForSelector('main .banner.info');
  out.finderMissing = await page.textContent('main .banner.info');
  await page.unroute('**/api/professionals/types*');
  await page.unroute('**/api/tools/calculate_lien_deadline');

  // ---- the checklist after a project and a check: done steps are gone; Hide puts it away for good
  await page.goto(base + '/#/dashboard');
  await page.waitForSelector('.gs-steps');
  out.gs2 = await page.$$eval('.gs-steps a', (as) => as.map((a) => a.textContent));
  out.gsCount2 = await page.textContent('.gs-title .small');
  const tenantPatch = (p) => p.waitForResponse((r) => r.url().endsWith('/api/tenant') && r.request().method() === 'PATCH');
  await Promise.all([tenantPatch(page), page.click('.gs button:has-text("Hide")')]);
  out.gsAfterHide = await page.$$eval('.gs', (g) => g.length);
  out.focusAfterHide = await page.evaluate(() => document.activeElement && document.activeElement.id);
  await page.reload();
  await page.waitForSelector('.tiles');
  out.gsAfterReload = await page.$$eval('.gs', (g) => g.length);
  out.gsStored = await page.evaluate(() => Object.entries(localStorage).filter(([k]) => k.startsWith('tc.gettingStarted.')).map(([, v]) => v));
  const meNow = (await call(page, '/api/me')).data;
  out.gsHiddenFor = (meNow.tenant.settings || {})[`hidden_tips_u${meNow.user.id}`];
  const otherCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const op = await otherCtx.newPage();
  op.on('pageerror', (e) => out.errors.push('other browser: ' + String(e)));
  await op.goto(base + '/#/login');
  await call(op, '/api/auth/login', { method: 'POST', body: { email: 'owner@example.test', password: 'correct-horse-battery' } });
  await op.goto(base + '/#/dashboard');
  await op.waitForSelector('.tiles');
  out.gsOtherBrowser = await op.$$eval('.gs', (g) => g.length);
  await otherCtx.close();
  // ... but only for the person who hid it: a new office user in the same company still gets the checklist
  const officeInvite = await call(page, '/api/users', { method: 'POST', body: { email: 'office@example.test', role: 'office' } });
  const officeCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const oc = await officeCtx.newPage();
  oc.on('pageerror', (e) => out.errors.push('office: ' + String(e)));
  await oc.goto(base + '/#/login');
  await call(oc, '/api/auth/login', { method: 'POST', body: { email: 'office@example.test', password: officeInvite.data.temporary_password } });
  await oc.goto(base + '/#/dashboard');
  await oc.waitForSelector('.tiles');
  out.gsOfficeUser = await oc.$$eval('.gs', (g) => g.length);
  await officeCtx.close();

  // ---- project form hints
  await page.goto(base + '/#/project/new');
  await page.waitForSelector('input[name=contract_price]');
  out.priceHint = await page.evaluate(() => document.getElementById(document.querySelector('input[name=contract_price]').getAttribute('aria-describedby')).textContent);
  out.lastHint = await page.evaluate(() => document.getElementById(document.querySelector('input[name=last_furnishing_date]').getAttribute('aria-describedby')).textContent);
  out.countyList = await page.$$eval('#ca-counties option', (o) => o.length);

  // ---- Settings: Account and data (owner only)
  let exportMode = '404';
  await page.route('**/api/tenant/export', (route) => exportMode === '404' ? route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    : route.fulfill({ status: 200, headers: { 'content-type': 'application/json', 'content-disposition': 'attachment; filename="guide builders export.json"' }, body: '{"tenant":{"company_name":"Guide Builders"}}' }));
  let deleteMode = '404';
  out.deleteCalls = [];
  await page.route('**/api/tenant', (route) => {
    const req = route.request();
    if (req.method() !== 'DELETE') return route.continue();
    out.deleteCalls.push({ body: JSON.parse(req.postData() || 'null'), csrf: !!req.headers()['x-csrf-token'] });
    if (deleteMode === '404') return route.fulfill({ status: 404, json: { detail: 'Not Found' } });
    if (deleteMode === 'wrong') return route.fulfill({ status: 401, json: { detail: 'That password is not right.' } });
    return route.fulfill({ status: 200, json: { ok: true } });
  });
  await page.goto(base + '/#/settings');
  await page.waitForSelector('#company-data');
  out.jump = await page.$$eval('nav.jump a', (as) => as.map((a) => a.textContent));
  out.dangerOpen = await page.$eval('#company-data details.danger-zone', (d) => d.open);
  out.dangerChevron = await page.$eval('#company-data details.danger-zone > summary', (s) => getComputedStyle(s, '::after').content);
  out.dataHeadings = await page.$$eval('#company-data h2, #company-data h3', (hs) => hs.map((x) => x.textContent));
  await page.click('#company-data button:has-text("Download all company data")');
  await page.waitForSelector('#company-data .banner.info');
  out.exportMissing = await page.textContent('#company-data .banner.info');
  exportMode = 'ok';
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#company-data button:has-text("Download all company data")')]);
  out.exportName = dl.suggestedFilename();
  out.exportBody = fs.readFileSync(await dl.path(), 'utf8');
  await page.click('#company-data details.danger-zone > summary');
  await page.fill('#del-name', 'Guide Builder');
  await page.fill('#del-pw', 'correct-horse-battery');
  await page.click('#company-data button:has-text("Delete company account permanently")');
  out.deleteMismatch = await page.textContent('#company-data [role=alert] .banner');
  out.deleteCallsAfterMismatch = out.deleteCalls.length;
  await page.fill('#del-name', 'Guide Builders');
  await page.click('#company-data button:has-text("Delete company account permanently")');
  await page.waitForSelector('#company-data [role=alert] .banner.info');
  out.deleteMissing = await page.textContent('#company-data [role=alert] .banner.info');
  deleteMode = 'wrong';
  await page.fill('#del-pw', 'not-my-password');
  await page.click('#company-data button:has-text("Delete company account permanently")');
  await page.waitForSelector('#company-data [role=alert] .banner.error');
  out.deleteWrong = await page.textContent('#company-data [role=alert] .banner.error');
  out.stillSignedIn = await page.evaluate(() => location.hash === '#/settings' && !document.querySelector('#nav').hidden);
  await page.$eval('#company-data', (el) => el.scrollIntoView());
  await shot(page, 'settings');
  deleteMode = 'ok';
  await page.fill('#del-pw', 'correct-horse-battery');
  await page.click('#company-data button:has-text("Delete company account permanently")');
  await page.waitForSelector('.auth-wrap .banner.info');
  out.deleted = await page.evaluate(() => ({ hash: location.hash, banner: document.querySelector('.auth-wrap .banner.info').textContent,
    nav: document.querySelector('#nav').hidden, company: document.querySelector('#company').textContent,
    stored: Object.keys(localStorage).filter((k) => k.startsWith('tc.')) }));
  out.lastDelete = out.deleteCalls[out.deleteCalls.length - 1];

  // ---- a field user: no checklist and no Account and data section
  await page.goto(base + '/#/dashboard');
  await page.waitForSelector('.tiles');
  const invite = await call(page, '/api/users', { method: 'POST', body: { email: 'field@example.test', role: 'field' } });
  const fieldCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const fp = await fieldCtx.newPage();
  fp.on('pageerror', (e) => out.errors.push('field: ' + String(e)));
  await fp.goto(base + '/#/login');
  out.fieldLogin = (await call(fp, '/api/auth/login', { method: 'POST', body: { email: 'field@example.test', password: invite.data.temporary_password } })).status;
  await fp.goto(base + '/#/dashboard');
  await fp.waitForSelector('.tiles');
  out.fieldGs = await fp.$$eval('.gs', (g) => g.length);
  await fp.goto(base + '/#/settings');
  await fp.waitForSelector('main h1');
  await fp.waitForTimeout(300);
  out.fieldData = await fp.$$eval('#company-data', (g) => g.length);
  await fieldCtx.close();

  // ---- a company that has done every step never sees the checklist, even after deleting its project
  const doneCtx = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  const dp = await doneCtx.newPage();
  dp.on('pageerror', (e) => out.errors.push('done: ' + String(e)));
  await dp.route('**/api/integrations/webhooks', (route) => route.request().method() === 'GET' ? route.fulfill({ json: [{ id: 1, kind: 'slack', enabled: true }] }) : route.continue());
  await dp.goto(base + '/#/login');
  await call(dp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Done Builders', email: 'done@example.test', password: 'correct-horse-battery', cslb_license: '123456' } });
  await call(dp, '/api/subs', { method: 'POST', body: { name: 'Test Sub Co' } });
  const dproj = await call(dp, '/api/projects', { method: 'POST', body: { name: 'Done Project', county: 'Fresno' } });
  out.doneTool = (await call(dp, '/api/tools/verify_contractor_license', { method: 'POST', body: { license_number: '123456' } })).status;
  const donePatch = tenantPatch(dp);
  await dp.goto(base + '/#/dashboard');
  await dp.waitForSelector('.tiles');
  await donePatch;
  out.doneGs = await dp.$$eval('.gs', (g) => g.length);
  out.doneStored = await dp.evaluate(() => Object.entries(localStorage).filter(([k]) => k.startsWith('tc.gettingStarted.')).map(([, v]) => v));
  out.doneCompany = ((await call(dp, '/api/me')).data.tenant.settings || {}).getting_started;
  await call(dp, `/api/projects/${dproj.data.id}`, { method: 'DELETE' });
  await dp.reload();
  await dp.waitForSelector('.tiles');
  out.doneGsAfterUndo = await dp.$$eval('.gs', (g) => g.length);
  await doneCtx.close();

  // ---- storage blocked (private window, strict settings): the page still works and Hide still hides
  const noStore = await browser.newContext({ ...opts, viewport: { width: 1280, height: 900 } });
  await noStore.addInitScript(() => { Object.defineProperty(window, 'localStorage', { configurable: true, get() { throw new DOMException('blocked', 'SecurityError'); } }); });
  const np = await noStore.newPage();
  np.on('pageerror', (e) => out.errors.push('nostore: ' + String(e)));
  await np.goto(base + '/#/login');
  await call(np, '/api/auth/signup', { method: 'POST', body: { company_name: 'Private Builders', email: 'private@example.test', password: 'correct-horse-battery' } });
  await np.goto(base + '/#/dashboard');
  await np.waitForSelector('.gs-steps');
  out.noStoreSteps = await np.$$eval('.gs-steps a', (as) => as.length);
  await np.click('.gs button:has-text("Hide")');
  await np.goto(base + '/#/projects');
  await np.waitForSelector('main h1');
  await np.goto(base + '/#/dashboard');
  await np.waitForSelector('.tiles');
  out.noStoreAfterHide = await np.$$eval('.gs', (g) => g.length);
  await noStore.close();

  // ---- phone width (375 px), reduced motion: no sideways scrolling, nothing in the bottom bar overflows
  const phone = await browser.newContext({ ...opts, viewport: { width: 375, height: 812 }, reducedMotion: 'reduce' });
  const mp = await phone.newPage();
  mp.on('pageerror', (e) => out.errors.push('phone: ' + String(e)));
  // the real API this time: real handoffs and the real finder
  await mp.goto(base + '/#/login');
  await call(mp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Phone Builders With A Long Company Name', email: 'phone@example.test', password: 'correct-horse-battery' } });
  const pproj = await call(mp, '/api/projects', { method: 'POST', body: { name: 'Phone Job', county: 'Los Angeles', claimant_role: 'subcontractor', last_furnishing_date: '2026-11-10', first_furnishing_date: '2026-10-20' } });
  await mp.goto(base + '/#/dashboard');
  await mp.waitForSelector('.gs-steps');
  out.phone = { dashboard: await overflow(mp) };
  out.phoneNav = await mp.$$eval('#nav a', (as) => as.filter((a) => a.offsetParent !== null && a.scrollWidth > a.clientWidth + 1).map((a) => a.innerText));
  out.phoneNavWide = await mp.$eval('#nav a[data-route=professionals]', (a) => getComputedStyle(a).display);
  out.phoneStepHeights = await mp.$$eval('.gs-steps a', (as) => as.map((a) => a.getBoundingClientRect().height));
  out.phoneStepColumns = await mp.$$eval('.gs-steps a', (as) => [...new Set(as.map((a) => Math.round(a.getBoundingClientRect().left)))].length);
  await shot(mp, 'dashboard-phone');
  await mp.goto(base + '/#/tools');
  await mp.waitForSelector('.tool-list');
  out.phoneToolsLink = await mp.$$eval('main .page-head a[href="#/professionals"]', (a) => a.map((x) => x.offsetParent !== null));
  await mp.goto(base + '/#/tools/calculate_lien_deadline');
  await mp.waitForSelector('select[name=fill_project]');
  await mp.selectOption('select[name=fill_project]', String(pproj.data.id));
  await mp.click('form button[type=submit]');
  await mp.waitForSelector('.result details.handoffs');
  out.phone.resultClosed = await overflow(mp);
  out.phoneMotion = await mp.$eval('details.handoffs > summary', (s) => getComputedStyle(s, '::after').transitionDuration);
  await mp.click('details.handoffs > summary');
  out.phone.resultOpen = await overflow(mp);
  out.phoneFindButtons = await mp.$$eval('details.handoffs .ho-find', (as) => as.map((a) => Math.round(a.getBoundingClientRect().height)));
  out.phoneFindDot = await mp.$$eval('details.handoffs .ho-find .ho-dot', (ds) => ds.map((d) => getComputedStyle(d).display));
  await mp.$eval('details.handoffs', (el) => el.scrollIntoView());
  await shot(mp, 'result-phone');
  const many = await call(mp, '/api/tools/project_setup_checklist', { method: 'POST', body: { project_name: 'Mission St', city: 'Oakland', county: 'Alameda',
    owner_type: 'public_agency', is_home_improvement: false, residential_units: 0, contract_price_usd: 4500000, start_date: '2027-01-11' } });
  out.manyHandoffs = many.data.handoffs || [];
  await mp.route('**/api/tools/calculate_lien_deadline', async (route) => { const resp = await route.fetch(); const json = await resp.json(); json.handoffs = out.manyHandoffs; await route.fulfill({ response: resp, json }); });
  await mp.goto(base + '/#/tools/calculate_lien_deadline');
  await mp.waitForSelector('select[name=fill_project]');
  await mp.selectOption('select[name=fill_project]', String(pproj.data.id));
  await mp.click('form button[type=submit]');
  await mp.waitForSelector('.result details.handoffs');
  out.many = await mp.$eval('details.handoffs', (d) => ({ summary: d.querySelector('summary').innerText.replace(/\s+/g, ' ').trim(),
    summaryHeight: Math.round(d.querySelector('summary').getBoundingClientRect().height) }));
  await mp.click('details.handoffs > summary');
  out.many.rows = await mp.$$eval('details.handoffs .ho-item', (li) => li.map((x) => ({ row: !!x.querySelector('details.ho-more'), open: !!x.querySelector('details.ho-more[open]'),
    name: x.querySelector('.ho-name strong').textContent })));
  out.many.openHeight = await mp.$eval('details.handoffs', (d) => Math.round(d.getBoundingClientRect().height));
  out.phone.manyOpen = await overflow(mp);
  await mp.$eval('details.handoffs', (el) => el.scrollIntoView());
  await shot(mp, 'result-many-phone');
  out.manyPrint = await mp.evaluate(() => { window.dispatchEvent(new Event('beforeprint')); const o = [...document.querySelectorAll('details.ho-more')].every((d) => d.open);
    window.dispatchEvent(new Event('afterprint')); return [o, [...document.querySelectorAll('details.ho-more')].some((d) => d.open)]; });
  await mp.unroute('**/api/tools/calculate_lien_deadline');
  await mp.goto(base + `/#/professionals?type=attorney&project=${pproj.data.id}`);
  await mp.waitForSelector('.pf-entry');
  out.phoneFinderCounty = await mp.inputValue('#pf-county');
  out.phone.finder = await overflow(mp);
  await shot(mp, 'finder-phone', { fullPage: true });
  await mp.goto(base + '/#/settings');
  await mp.waitForSelector('#company-data');
  await mp.click('#company-data details.danger-zone > summary');
  out.phone.settings = await overflow(mp);
  await mp.$eval('#company-data', (el) => el.scrollIntoView());
  await shot(mp, 'settings-phone');
  await phone.close();

  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


SOFT_REQUIREMENTS = {"conditional", "recommended", "optional"}


def expected_summary(handoffs: list) -> str:
    """The closed handoff line as app.js words it: "May need" only when every item is conditional or suggested, at
    most two short names (long bracketed explanations dropped), the rest counted."""
    names = list(dict.fromkeys(re.sub(r"\s*\([^()]*\s[^()]*\)\s*$", "", x["name"]) or x["name"] for x in handoffs))
    may = all(x.get("requirement") in SOFT_REQUIREMENTS for x in handoffs)
    lead = ("May need" if may else "Needs") + (" a licensed or qualified person:" if len(names) == 1 else " licensed or qualified people:")
    shown = " and ".join(names) if len(names) <= 2 else f"{', '.join(names[:2])} and {len(names) - 2} more"
    return f"{lead} {shown}"


def test_handoff_line_wording_never_softens_a_required_person():
    assert "const SOFT_REQ = new Set(['conditional', 'recommended', 'optional']);" in PROS
    assert "items.every((x) => SOFT_REQ.has(x.req))" in PROS          # one required (or unlabeled) item -> "Needs"
    assert expected_summary([{"name": "Notary public", "requirement": "required"}, {"name": "Attorney", "requirement": "conditional"}]) \
        == "Needs licensed or qualified people: Notary public and Attorney"
    assert expected_summary([{"name": "Attorney"}]) == "Needs a licensed or qualified person: Attorney"
    assert expected_summary([{"name": "Certified Access Specialist (CASp)", "requirement": "recommended"}]) \
        == "May need a licensed or qualified person: Certified Access Specialist (CASp)"


def test_guidance_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "guidance_smoke.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["signup"] == 200 and out["project"] == 201

    # Getting started: derived from what the company did, at the top, done steps gone, no old banner
    assert out["gs1"] == ["Add your subcontractors", "Add a project", "Run a check",
                          "Set up deadline reminders"]
    # (no license step: the license card right under it already asks for the number)
    assert out["gsCount1"] == "0 of 4 done" and out["gsFirst"] == 1 and out["banners"] == []
    assert out["gs2"] == ["Add your subcontractors", "Set up deadline reminders"]
    assert out["gsCount2"] == "2 of 4 done"
    assert out["gsAfterHide"] == 0 and out["focusAfterHide"] == "main"
    assert out["gsAfterReload"] == 0 and out["gsStored"] == ["hidden"]
    assert out["gsHiddenFor"] == ["getting_started"] and out["gsOtherBrowser"] == 0   # hidden for good, on every device
    assert out["gsOfficeUser"] == 1                                       # for that person only
    assert out["doneTool"] == 200 and out["doneGs"] == 0 and out["doneStored"] == ["done"] and out["doneGsAfterUndo"] == 0
    assert out["doneCompany"] == "done"                                    # finished: away for the whole company
    assert out["noStoreSteps"] == 4 and out["noStoreAfterHide"] == 0
    assert out["fieldLogin"] == 200 and out["fieldGs"] == 0 and out["fieldData"] == 0

    # a real result: the handoffs the API sent, as one collapsed line above the result's details
    real = out["realHandoffs"]
    assert real, "calculate_lien_deadline should need an attorney (lien foreclosure)"
    assert out["real"]["open"] is False and out["real"]["beforeBody"] is True and out["real"]["rawShown"] is False
    assert out["real"]["summary"] == expected_summary(real)
    assert out["real"]["chevron"] == '""' and "›" not in out["realSummaryName"]      # drawn, not read aloud
    assert [i["name"] for i in out["real"]["items"]] == [x["name"] for x in real]
    assert [i["unverified"] for i in out["real"]["items"]] == [not x["verified"] for x in real]
    for item, x in zip(out["real"]["items"], real):
        assert item["link"] == x["find_url"] and x["why"] in item["text"]
        if item["link"]:
            assert item["target"] == "_blank" and set(item["rel"].split()) >= {"noopener", "noreferrer"}
            assert "Find one near the job" in item["text"]
    assert out["realOpened"] is True and out["dialogs"] == 0 and out["printOpen"] == [True, False]

    # the real finder, opened from that result: type, project and its county carried over
    rf = out["realFinder"]
    assert rf["type"] == real[0]["type_id"] and rf["county"] == "San Diego" and rf["project"]
    assert "county=San+Diego" in out["realFindQuery"] and f"type_id={real[0]['type_id']}" in out["realFindQuery"]
    assert rf["countyOptions"] == 59 and rf["nav"] == "professionals" and rf["title"].startswith("Find a professional")
    assert [x["href"] for x in rf["links"]] == out["realFoundDirs"]
    assert all(x["href"].startswith("https://") and set(x["rel"].split()) >= {"noopener", "noreferrer"} for x in rf["links"])
    assert out["realNeutral"] in rf["text"] and rf["formNote"] is False      # the results' note replaces the form's
    assert rf["status"].startswith("Showing ") and rf["status"].endswith("near San Diego County.")
    assert not any(rf["details"])                                    # the longer parts stay folded
    assert any("San Diego County" in hint for hint in out["dasHints"])     # the API's own "how to search" sentence
    assert out["dasLinks"] and all(u.startswith("https://www.dir.ca.gov/") for u in out["dasLinks"])

    # hostile or odd handoffs: text stays text, only http(s) becomes a link, unverified marked, extras shown
    assert out["ho"]["open"] is False
    assert out["ho"]["summary"].startswith("Needs licensed or qualified people: Attorney (lawyer), Notary public <img")
    assert out["ho"]["summary"].endswith(" and 1 more")
    assert out["hoRowsClosed"] == [False, False, False]                # three or more: each person is its own row
    assert [link["href"] for link in out["hoLinks"]] == [
        "https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch",
        "https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=SAN+DIEGOG&varType=22"]
    assert all(set(link["rel"].split()) >= {"noopener", "noreferrer"} for link in out["hoLinks"])
    assert all(link["text"].startswith("Find one near the job") and "(official site, opens in a new tab)" in link["text"]
               for link in out["hoLinks"])
    assert [i["unverified"] for i in out["hoItems"]] == [False, True, True]
    assert out["hoItems"][1]["hint"] == ""                          # javascript: address: shown as no link at all
    assert "No official search page is linked for this one." in out["hoItems"][1]["text"]
    assert out["hoItems"][0]["hint"] == "Opens the statewide search; look for the job's area there."
    third = out["hoItems"][2]
    assert third["pills"] == ["Required by law if this applies", "Unverified"]
    assert third["hint"].startswith("Run by Test agency. Opens already filtered to the job's county.") and "may have changed" in third["hint"]
    assert "When: Test timing. Key date: May 9, 2027." in third["text"] and "Also: Test other reason." in third["text"]
    assert "Check them: Test: how to check." in third["text"]
    assert third["finder"].startswith("#/professionals?type=apprenticeship_committee&project=")
    assert out["hoNote"] == "TradeComply links to official state lookups and doesn't recommend specific people."
    assert out["injected"] == 0

    # finder edge cases
    f = out["finder"]
    assert f["type"] == "apprenticeship_committee" and f["county"] == "San Diego" and f["project"]
    assert f["countyOptions"] == 59 and f["typeHint"] == "Test committee description."
    assert [x["href"] for x in f["links"]] == ["https://www.dir.ca.gov/databases/das/results_pwadd.asp?varCounty=SAN+DIEGOG&varType=22",
                                                "https://www.dir.ca.gov/databases/das/aigstart.asp"]    # the data: link is dropped
    assert f["hints"][0] == "Opens already filtered to San Diego County."
    assert any("may have changed" in hint for hint in f["hints"])
    assert f["status"] == "Showing 2 official lookups for Apprenticeship committee near San Diego County."
    assert f["notes"] == 1 and f["details"] == [False, False]           # said once, under the results
    assert any("type_id=apprenticeship_committee" in q and "county=San+Diego" in q for q in out["findQueries"])
    assert "No official lookup is listed for apprenticeship committee yet" in out["finderEmpty"]
    assert "The lookup failed (test)." in out["finderError"] and out["finderRetry"] == ["Try again"]
    assert "You're offline" in out["finderOffline"]
    assert out["finderNoType"] == "Choose the kind of professional you need."
    assert "isn't turned on on this server yet" in out["finderMissing"] and "ask whoever runs this TradeComply server" in out["finderMissing"]

    # hints under the fields people get wrong
    assert out["priceHint"].startswith("Your whole price for this job") and "(B&P 7159.5)" in out["priceHint"]
    assert "completion date" in out["lastHint"] and out["countyList"] == 58

    # Account and data
    assert out["jump"][-1] == "Account and data" and out["dangerOpen"] is False and out["dangerChevron"] == '""'
    assert out["dataHeadings"] == ["Account and data"]                    # the button names the download; no echo heading
    assert "isn't available on this server yet" in out["exportMissing"]
    assert out["exportName"] == "guide_builders_export.json" and json.loads(out["exportBody"])["tenant"]["company_name"] == "Guide Builders"
    assert out["deleteMismatch"].startswith("Type the company name exactly") and out["deleteCallsAfterMismatch"] == 0
    assert "isn't available on this server yet" in out["deleteMissing"]
    assert out["deleteWrong"] == "That password is not right." and out["stillSignedIn"] is True
    assert out["lastDelete"] == {"body": {"confirm_company_name": "Guide Builders", "password": "correct-horse-battery"}, "csrf": True}
    d = out["deleted"]
    assert d["hash"] == "#/login?deleted=1" and "deleted" in d["banner"] and d["nav"] is True and d["company"] == ""
    assert d["stored"] == []

    # phone: no sideways scrolling anywhere, the bottom bar fits, tap targets are big enough, motion reduced
    assert out["phone"] == {"dashboard": False, "resultClosed": False, "resultOpen": False, "finder": False, "settings": False,
                            "manyOpen": False}
    assert out["phoneNav"] == [] and out["phoneNavWide"] == "none" and out["phoneToolsLink"] == [True]
    assert out["deskToolsFinderLinks"] == ["nav"]                        # wide screens: the sidebar only
    assert min(out["phoneStepHeights"]) >= 44 and min(out["phoneFindButtons"]) >= 44
    assert out["phoneStepColumns"] == 2 and out["phoneFindDot"] and set(out["phoneFindDot"]) == {"none"}
    # a result that needs many people: a short closed line, then one closed row per person
    many = out["manyHandoffs"]
    assert len(many) >= 3, "project_setup_checklist for a public mixed-use job should need several people"
    assert out["many"]["summary"] == expected_summary(many) and out["many"]["summary"].endswith(f" and {len(many) - 2} more")
    assert out["many"]["summary"].startswith("Needs ")                   # it includes people the law requires
    assert out["many"]["summaryHeight"] <= 100                          # at most three lines at 375 px
    assert [r["name"] for r in out["many"]["rows"]] == [x["name"] for x in many]
    assert all(r["row"] and not r["open"] for r in out["many"]["rows"])
    assert out["many"]["openHeight"] < 120 + 130 * len(many)              # was ~5,300 px with every detail shown
    assert out["manyPrint"] == [True, False]
    assert out["phoneFinderCounty"] == "Los Angeles"
    assert float(out["phoneMotion"].rstrip("s")) < 0.01
    assert out["errors"] == []
    for name in ("dashboard", "result", "result-handoffs", "finder", "settings", "dashboard-phone", "result-phone",
                 "result-many-phone", "finder-phone", "settings-phone"):
        assert os.path.getsize(os.path.join(shots, f"guide-{name}.png")) > 5000, name
