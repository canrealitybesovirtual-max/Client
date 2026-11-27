"""The Integrations page of the web app: served assets (nav entry, route, styles, offline caching of the catalog),
and a browser smoke test with Playwright when Node and its Chromium are installed (skipped otherwise): cards render
from the catalog, search and filters work, the Slack alert panel opens, the OAuth callback result is forwarded from
Settings and shown as text, no horizontal scrolling on a phone, and the page still opens offline."""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tradecomply import integrations_guide
from tests.support import server_up

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path):
    from app.config import Settings
    from app.main import create_app
    app = create_app(Settings.from_env(data_dir=str(tmp_path), database_url=None, events_background=False))
    with TestClient(app) as c:
        yield c
    app.state.platform.store.close()


def test_assets_wire_up_the_integrations_page(client):
    index = client.get("/").text
    assert 'href="#/integrations" data-route="integrations"' in index and 'id="i-plug"' in index
    assert '<span class="nav-long">Integrations</span>' in index
    js = client.get("/static/app.js").text
    assert "routes.integrations = async" in js and "/api/integrations/catalog" in js
    assert "location.replace(`#/integrations?${query.toString()}`)" in js        # OAuth callback lands on settings
    assert "integrations: 'Integrations'" in js
    css = client.get("/static/styles.css").text
    assert ".int-card" in css and ".chip[aria-pressed=true]" in css and "input[type=url]" in css
    sw = client.get("/sw.js").text
    # the integrations release bumped the shell cache to v3; later releases keep bumping it (never back below 3)
    version = int(sw.split("const SHELL_CACHE = 'tc-shell-v", 1)[1].split("'", 1)[0])
    assert "'/api/integrations/catalog'" in sw and version >= 3


def test_app_js_never_puts_html_from_the_catalog_into_the_page():
    with open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8") as f:
        js = f.read()
    section = js[js.index("// ======================================================================== integrations"):
                 js.index("// ======================================================================== settings")]
    assert "innerHTML" not in section and "html:" not in section and "insertAdjacentHTML" not in section
    assert ".replaceChildren(" not in section.replace("el.replaceChildren(); return append", "")


# ---------------------------------------------------------------------------- browser smoke (Playwright)
SMOKE = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, expected, builtinCount, shot] = process.argv.slice(2);
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const out = {};
  await page.goto(base + '/#/login');
  out.signup = await page.evaluate(async () => (await fetch('/api/auth/signup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ company_name: 'Smoke Builders', email: 'smoke@example.test', password: 'correct-horse-battery', cslb_license: '123456' }) })).status);
  await page.goto(base + '/#/integrations');
  await page.waitForSelector('.int-card');
  out.cards = await page.$$eval('.int-card', (els) => els.length);
  out.count = await page.textContent('#int-count');
  out.current = await page.$eval('#nav a[aria-current="page"]', (a) => a.dataset.route);
  out.title = await page.title();
  await page.fill('#int-q', 'docusign');
  await page.waitForTimeout(400);
  out.search = await page.$$eval('.int-card:not([hidden]) h3', (els) => els.map((e) => e.textContent));
  out.searchHash = await page.evaluate(() => location.hash);
  await page.fill('#int-q', '');
  await page.click('button.chip[data-type="builtin"]');
  await page.waitForTimeout(300);
  out.builtinShown = await page.$$eval('.int-card:not([hidden])', (els) => els.length);
  out.pressed = await page.getAttribute('button.chip[data-type="builtin"]', 'aria-pressed');
  await page.click('button.chip[data-type="builtin"]');
  await page.click('#int-slack details.int-connect > summary');
  await page.waitForSelector('#int-slack form input[type=url]');
  out.slackKinds = await page.$$eval('#int-slack .checks input', (els) => els.length);
  await page.click('#int-assistant input[value="box"]');
  await page.click('#int-assistant button[type=submit]');
  await page.waitForSelector('#int-assistant pre.code');
  out.config = JSON.parse(await page.textContent('#int-assistant pre.code'));
  out.nullText = await page.evaluate(() => document.querySelector('main').innerText.split('\n').filter((l) => l.trim() === 'null').length);
  await page.screenshot({ path: shot });
  // the server leaves the message in the tc_notice cookie (auth.set_notice); text in the URL is never shown
  await page.evaluate(() => { document.cookie = 'tc_notice=' + encodeURIComponent('denied <img src=x onerror=alert(1)>') + '; path=/'; });
  await page.goto(base + '/#/settings?integration=slack&result=error&message=' + encodeURIComponent('Call billing at 1-800-555-0199'));
  await page.waitForSelector('.banner.error');
  out.oauthHash = await page.evaluate(() => location.hash);
  out.oauthText = await page.textContent('.banner.error');
  out.oauthImg = await page.$$eval('main img', (els) => els.length);
  // offline: the service worker serves the app shell, the account and the catalog from its cache
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  await page.goto(base + '/#/integrations');
  await page.reload();                         // a full load through the service worker fills its API cache
  await page.waitForSelector('.int-card');
  await page.waitForTimeout(500);
  await ctx.setOffline(true);
  await page.reload();
  await page.waitForSelector('.int-card', { timeout: 15000 });
  out.offlineCards = await page.$$eval('.int-card', (els) => els.length);
  out.offlineWarn = await page.$$eval('.banner.warn', (els) => els.map((e) => e.textContent).join(' '));
  await ctx.setOffline(false);
  const phone = await browser.newContext({ viewport: { width: 375, height: 812 }, deviceScaleFactor: 1, storageState: await ctx.storageState() });
  const mp = await phone.newPage();
  await mp.goto(base + '/#/integrations');
  await mp.waitForSelector('.int-card');
  out.phoneOverflow = await mp.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  out.phoneNav = await mp.$$eval('#nav a', (as) => as.filter((a) => a.scrollWidth > a.clientWidth + 1).map((a) => a.innerText));
  out.errors = errors;
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _playwright():
    node = shutil.which("node")
    if not node:
        return None
    env = dict(os.environ)
    npm = shutil.which("npm")
    if npm:
        try:
            root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            root = ""
        if root:
            env["NODE_PATH"] = os.pathsep.join(filter(None, [root, env.get("NODE_PATH")]))
    probe = subprocess.run([node, "-e", "const p = require('playwright'); "
                            "process.exit(require('fs').existsSync(p.chromium.executablePath()) ? 0 : 3)"],
                           env=env, capture_output=True, timeout=60)
    return (node, env) if probe.returncode == 0 else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def playwright_env():
    found = _playwright()
    if not found:
        pytest.skip("Node.js with Playwright and its Chromium is not installed")
    return found


@pytest.fixture
def live_server(tmp_path, playwright_env):
    """This app in a real uvicorn process (SQLite in tmp_path); only started when the browser test can run."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL")}
    env.update(DATA_DIR=str(tmp_path / "data"), LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0",
               MCP_REQUIRE_KEY="0")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port",
                             str(port)], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("server exited: " + proc.stderr.read().decode()[-2000:])
            try:
                if server_up(base):
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            pytest.fail("server did not start")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_integrations_page_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    base = live_server
    entries = integrations_guide.entries()
    builtin = sum(1 for e in entries if e["integration"] == "builtin")
    script = tmp_path / "smoke.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, str(len(entries)), str(builtin), str(tmp_path / "page.png")],
                       env=env, capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["signup"] == 200 and out["current"] == "integrations" and out["title"].startswith("Integrations")
    assert out["cards"] == len(entries) and out["count"] == f"{len(entries)} products shown of {len(entries)}."
    assert out["search"][0] == "Docusign (eSignature)" and len(out["search"]) <= 3     # Workato names it too
    assert out["searchHash"] == "#/integrations?q=docusign"
    assert out["builtinShown"] == builtin and out["pressed"] == "true"
    assert out["slackKinds"] == 7                       # "All alerts" plus the six events
    assert out["config"]["mcpServers"]["box"] == {"type": "http", "url": "https://mcp.box.com"}
    assert out["config"]["mcpServers"]["tradecomply"]["url"] == base + "/mcp"
    assert out["nullText"] == 0
    assert out["oauthHash"] == "#/integrations" and "denied <img src=x onerror=alert(1)>" in out["oauthText"]
    assert "1-800" not in out["oauthText"]
    assert out["oauthImg"] == 0
    assert out["offlineCards"] == len(entries) and "Connection status is unavailable" in out["offlineWarn"]
    assert out["phoneOverflow"] is False and out["phoneNav"] == []
    assert out["errors"] == []
    assert (tmp_path / "page.png").stat().st_size > 10000
