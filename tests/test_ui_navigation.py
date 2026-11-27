"""Navigation in the web app (spec 1.2-1.7): the top bar, the grouped sidebar, the phone bottom bar (five items per
role, the last is More, the elements themselves moved so reading, Tab and visual order agree), the More page, the
new-name aliases, aria-current, the account menu (APG menu button), Settings ?section=, and the loading outline.
Runs when Node with Playwright and its Chromium is installed (skipped otherwise). TC_UI_SHOTS=<directory> keeps the
screenshots (nav-*.png)."""
import json
import os
import re
import subprocess

from tests.test_integrations_ui import live_server, playwright_env  # noqa: F401  (fixtures)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8") as f:
    JS = f.read()
with open(os.path.join(REPO, "app", "static", "index.html"), encoding="utf-8") as f:
    INDEX = f.read()


def test_top_bar_and_nav_markup():
    top = INDEX[INDEX.index('<header class="topbar">'):INDEX.index("</header>")]
    order = [m for m in re.findall(r'(class="brand"|id="net"|id="search-open"|id="ask-open"|id="account-open")', top)]
    assert order == ['class="brand"', 'id="net"', 'id="search-open"', 'id="ask-open"', 'id="account-open"']
    assert 'aria-label="TradeComply CA, Today"' in top and '<span class="brand-text">' in top
    assert '<span id="company" class="company"></span>' in top          # the company name lives in the account button
    nav = INDEX[INDEX.index('<nav id="nav"'):INDEX.index("</nav>")]
    routes = re.findall(r'data-route="([a-z]+)"', nav)
    assert routes == ["dashboard", "projects", "subs", "field", "tools", "professionals", "documents", "integrations", "settings", "more"]
    assert re.findall(r'<p class="nav-group" id="ng-[a-z]+">([^<]+)</p>', nav) == ["Work", "Checks", "Paperwork", "Set up"]
    assert nav.count('role="group"') == 4
    # the aliases swap the address before drawing and keep the hashchange guard in step (no bounce on Back)
    assert "history.replaceState(history.state, '', hash);" in JS and "routeHash = hash;" in JS
    assert "const UNDER_MORE = new Set(['documents', 'document', 'integrations', 'settings', 'professionals', 'rule', 'welcome']);" in JS


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
  const open = async (p, hash) => { await p.goto(base + '/' + hash); await p.waitForSelector('main h1'); await p.waitForTimeout(250); };
  const bar = (p) => p.$$eval('#nav a', (as) => as.filter((a) => a.getClientRects().length).map((a) => ({ route: a.dataset.route, text: a.innerText.trim(),
    left: Math.round(a.getBoundingClientRect().left), size: parseFloat(getComputedStyle(a).fontSize), clipped: a.scrollWidth > a.clientWidth + 1,
    current: a.getAttribute('aria-current') })));
  const topOrder = (p) => p.$$eval('.topbar > *', (els) => els.map((e) => e.id || e.className));

  // ---- an owner, and a field user in the same company
  const oc = await browser.newContext({ ...opts, viewport: { width: 375, height: 812 } });
  const op = await oc.newPage();
  watch(op, 'owner-phone');
  await op.goto(base + '/#/login');
  out.signup = (await call(op, '/api/auth/signup', { method: 'POST', body: { company_name: 'Nav Builders', name: 'Nora Owner', email: 'nav@example.test', password: 'correct-horse-battery' } })).status;
  const inv = (await call(op, '/api/users', { method: 'POST', body: { email: 'crew@example.test', name: 'Fran Field', role: 'field' } })).data;
  await open(op, '#/dashboard');
  out.ownerBar = await bar(op);
  out.top = {};
  for (const hash of ['#/dashboard', '#/tools', '#/settings', '#/more']) { await open(op, hash); out.top[hash] = await topOrder(op); }
  await open(op, '#/documents');
  out.docsCurrent = await bar(op);
  out.docsPageLinks = await op.$$eval('#nav a[aria-current=page]', (as) => as.map((a) => a.dataset.route));
  await open(op, '#/more');
  out.moreOwner = { rows: await op.$$eval('main .list-row .lr-title', (e) => e.map((x) => x.textContent)), groups: await op.$$eval('main .more-group h2', (e) => e.map((x) => x.textContent)),
    who: await op.textContent('main .signed-in'), logout: await op.$$eval('main button', (b) => b.map((x) => x.innerText.trim()).filter((t) => t === 'Log out').length),
    current: (await bar(op)).find((x) => x.route === 'more').current };
  await op.screenshot({ path: path.join(shots, 'nav-more-office-m.png') });
  await open(op, '#/dashboard');
  await op.screenshot({ path: path.join(shots, 'nav-bar-office-m.png') });
  const ownerState = await oc.storageState();
  await oc.close();

  const fc = await browser.newContext({ ...opts, viewport: { width: 375, height: 812 } });
  const fp = await fc.newPage();
  watch(fp, 'field-phone');
  await fp.goto(base + '/#/login');
  out.join = (await call(fp, '/api/auth/invite/accept', { method: 'POST', body: { token: inv.invite_path.split('/').pop(), password: 'field-horse-battery' } })).status;
  await open(fp, '#/dashboard');
  out.fieldBar = await bar(fp);
  await fp.screenshot({ path: path.join(shots, 'nav-bar-field-m.png') });
  await open(fp, '#/subs');
  out.fieldSubsMore = (await bar(fp)).find((x) => x.route === 'more').current;
  await open(fp, '#/more');
  out.moreField = { rows: await fp.$$eval('main .list-row .lr-title', (e) => e.map((x) => x.textContent)), who: await fp.textContent('main .signed-in') };
  await fp.screenshot({ path: path.join(shots, 'nav-more-field-m.png') });
  await fc.close();

  // ---- 320 px with "Offline" showing: the brand drops to its logo, the bar still fits
  const sc = await browser.newContext({ ...opts, viewport: { width: 320, height: 640 }, storageState: ownerState });
  const sp = await sc.newPage();
  watch(sp, '320');
  await open(sp, '#/dashboard');
  await sp.evaluate(() => { document.getElementById('net').hidden = false; });
  out.narrow = await sp.evaluate(() => ({ overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    brandText: document.querySelector('.brand-text').getBoundingClientRect().width, brandName: document.querySelector('.brand').getAttribute('aria-label'),
    right: Math.max(...[...document.querySelectorAll('.topbar > *')].filter((e) => e.getClientRects().length).map((e) => e.getBoundingClientRect().right)) }));
  await sp.screenshot({ path: path.join(shots, 'nav-topbar-320.png') });
  await sc.close();

  // ---- desktop: grouped sidebar, aliases, account menu, ?section=, the loading outline
  const dc = await browser.newContext({ ...opts, viewport: { width: 1280, height: 800 }, storageState: ownerState });
  const dp = await dc.newPage();
  watch(dp, 'desk');
  await open(dp, '#/dashboard');
  out.sidebar = { groups: await dp.$$eval('#nav .nav-group', (e) => e.filter((x) => x.getClientRects().length).map((x) => x.textContent)),
    links: (await bar(dp)).map((x) => [x.route, x.text]) };
  await dp.screenshot({ path: path.join(shots, 'nav-sidebar-d.png') });
  out.aliases = {};
  for (const [from, h1] of [['#/checks/calculate_lien_deadline', 'Calculate lien deadline'], ['#/today', 'Today'], ['#/home', 'Today'], ['#/apps', 'Integrations'],
    ['#/log', 'Field log'], ['#/field-log', 'Field log'], ['#/docs', 'Documents'], ['#/subcontractors', 'Subcontractors'], ['#/checks', 'Checks'], ['#/help', 'Today'], ['#/search', 'Today']]) {
    await dp.goto(base + '/' + from);
    await dp.waitForFunction((h) => document.querySelector('main h1') && document.querySelector('main h1').textContent === h, h1);
    out.aliases[from] = await dp.evaluate(() => location.hash);
  }
  await open(dp, '#/projects');
  await dp.goto(base + '/#/apps');
  await dp.waitForFunction(() => location.hash === '#/integrations' && document.querySelector('main h1').textContent === 'Integrations');
  await dp.goBack();
  await dp.waitForFunction(() => document.querySelector('main h1') && document.querySelector('main h1').textContent === 'Projects');
  out.back = await dp.evaluate(() => location.hash);
  // the account menu, by keyboard
  await dp.focus('#account-open');
  await dp.keyboard.press('Enter');
  const menu = () => dp.evaluate(() => ({ open: !document.getElementById('account-menu').hidden, expanded: document.getElementById('account-open').getAttribute('aria-expanded'),
    focus: document.activeElement.id || document.activeElement.textContent.trim() }));
  out.menu = [await menu()];
  await dp.keyboard.press('ArrowDown'); out.menu.push(await menu());
  await dp.keyboard.press('End'); out.menu.push(await menu());
  await dp.keyboard.press('ArrowDown'); out.menu.push(await menu());
  await dp.keyboard.press('Escape'); out.menu.push(await menu());
  await dp.keyboard.press('ArrowUp'); out.menu.push(await menu());
  await dp.keyboard.press('Tab'); out.menu.push(await menu());
  await dp.click('#account-open');
  await dp.click('#account-menu a:has-text("Display")');
  await dp.waitForSelector('#display-h');
  out.menuDisplay = await dp.evaluate(() => ({ hash: location.hash, focus: document.activeElement.id }));
  // Settings ?section=team: at the Team heading after the page's own scroll to the top
  await dp.goto(base + '/#/settings?section=team');
  await dp.waitForSelector('#team-h');
  await dp.waitForTimeout(300);
  out.section = await dp.evaluate(() => ({ focus: document.activeElement.id, top: Math.round(document.getElementById('team-h').getBoundingClientRect().top), y: Math.round(scrollY),
    acct: !!document.getElementById('acct-h'), jumpLast: [...document.querySelectorAll('nav.jump a')].pop().textContent }));
  // a slow page: nothing for 400 ms, then an outline and one line for screen readers; gone once the page is in
  await dp.route('**/api/projects', async (route) => { await new Promise((r) => setTimeout(r, 1500)); await route.continue(); });
  await dp.goto(base + '/#/projects');
  await dp.waitForTimeout(150);
  out.loadingEarly = await dp.evaluate(() => ({ skeleton: !!document.querySelector('main .skeleton'), busy: document.getElementById('main').getAttribute('aria-busy') }));
  await dp.waitForSelector('main .skeleton');
  out.loading = await dp.evaluate(() => ({ status: document.getElementById('route-status').textContent, busy: document.getElementById('main').getAttribute('aria-busy') }));
  await dp.screenshot({ path: path.join(shots, 'nav-skeleton-d.png') });
  await dp.waitForFunction(() => document.querySelector('main h1') && document.querySelector('main h1').textContent === 'Projects');
  out.loaded = await dp.evaluate(() => ({ skeleton: !!document.querySelector('main .skeleton'), status: document.getElementById('route-status').textContent, busy: document.getElementById('main').getAttribute('aria-busy') }));
  await dp.unroute('**/api/projects');
  await dc.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_navigation_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "nav.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["signup"] == 200 and out["join"] == 200

    # phone bars: five per role, the last is More; DOM order (= Tab order) is the visual order; labels fit
    for key, want in (("ownerBar", ["Today", "Projects", "Subs", "Checks", "More"]), ("fieldBar", ["Today", "Log", "Checks", "Projects", "More"])):
        b = out[key]
        assert [x["text"] for x in b] == want, key
        assert [x["left"] for x in b] == sorted(x["left"] for x in b), key
        assert all(x["size"] >= 13 and not x["clipped"] for x in b), key
        assert b[0]["current"] == "page"
    # a page that lives under More marks More "true" and only its own link "page"
    assert next(x for x in out["docsCurrent"] if x["route"] == "more")["current"] == "true"
    assert out["docsPageLinks"] == ["documents"]
    assert out["fieldSubsMore"] == "true"
    # the same top bar, in the same order, everywhere
    assert len({json.dumps(v) for v in out["top"].values()}) == 1
    assert out["top"]["#/dashboard"][:2] == ["brand", "net"]
    # More, per role, with whose account this is
    assert out["moreOwner"]["groups"] == ["Work", "Checks", "Paperwork", "Help", "Set up"]
    assert out["moreOwner"]["rows"] == ["Field log", "Find a professional", "Documents", "Guided setup", "Integrations", "Settings", "Display"]
    assert out["moreOwner"]["who"] == "Signed in as Nora Owner · Nav Builders" and out["moreOwner"]["logout"] == 1
    assert out["moreOwner"]["current"] == "page"
    assert out["moreField"]["rows"] == ["Subcontractors", "Find a professional", "Documents", "Integrations", "Settings", "Display"]
    assert out["moreField"]["who"] == "Signed in as Fran Field · Nav Builders"
    # 320 px with Offline showing: the logo alone, the link keeps its name, nothing sideways
    n = out["narrow"]
    assert not n["overflow"] and n["brandText"] <= 1 and n["brandName"] == "TradeComply CA, Today" and n["right"] <= 320
    # desktop sidebar
    assert out["sidebar"]["groups"] == ["Work", "Checks", "Paperwork", "Set up"]
    assert out["sidebar"]["links"] == [["dashboard", "Today"], ["projects", "Projects"], ["subs", "Subcontractors"], ["field", "Field log"], ["tools", "All checks"],
                                       ["professionals", "Find a professional"], ["documents", "Documents"], ["integrations", "Integrations"], ["settings", "Settings"]]
    # aliases land on the real routes; Back doesn't bounce to the alias
    assert out["aliases"] == {"#/checks/calculate_lien_deadline": "#/tools/calculate_lien_deadline", "#/today": "#/dashboard", "#/home": "#/dashboard",
                              "#/apps": "#/integrations", "#/log": "#/field", "#/field-log": "#/field", "#/docs": "#/documents",
                              "#/subcontractors": "#/subs", "#/checks": "#/tools", "#/help": "#/dashboard", "#/search": "#/dashboard"}
    assert out["back"] == "#/projects"
    # the account menu: Enter opens on the first item, arrows move, End, Esc returns, Up opens on the last, Tab closes
    m = out["menu"]
    assert m[0] == {"open": True, "expanded": "true", "focus": "Settings"}
    assert m[1]["focus"] == "Display" and m[2]["focus"] == "Log out" and m[3]["focus"] == "Settings"
    assert m[4] == {"open": False, "expanded": "false", "focus": "account-open"}
    assert m[5] == {"open": True, "expanded": "true", "focus": "Log out"}
    assert m[6]["open"] is False
    assert out["menuDisplay"] == {"hash": "#/settings?section=display", "focus": "display-h"}
    s = out["section"]
    assert s["focus"] == "team-h" and s["y"] > 0 and 0 <= s["top"] < 200 and s["acct"] and s["jumpLast"] == "Account and data"
    # loading: nothing in the first 400 ms (the old page stays), then the outline; gone with the page
    assert out["loadingEarly"] == {"skeleton": False, "busy": "true"}
    assert out["loading"] == {"status": "Loading Projects…", "busy": "true"}
    assert out["loaded"] == {"skeleton": False, "status": "", "busy": None}
