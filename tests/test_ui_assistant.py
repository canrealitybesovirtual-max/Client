"""The web app's side of Ask TradeComply (app/assistant.py): the Help button, the panel's Ask TradeComply tab, answers
rendered safely, the "Checked:" line, Stop, the daily limit, and the owner's card in Settings (set up and not set up).
The Help panel always opens on Quick help (tests/test_ui_quickhelp.py); these steps click the Ask TradeComply tab first.

Static checks run everywhere. The browser test runs when Node with Playwright and its Chromium is installed (skipped
otherwise) against two real servers started from tests/assistant_ui_app.py: one whose helper is answered by the fake
Anthropic API (tests/fake_anthropic.py; no network, no real key) and one with no key. Every screen is checked at 1280
and 375 px wide: no sideways scrolling, no page errors, no pop-up dialogs. Set TC_UI_SHOTS=<directory> to keep the
screenshots (ai-*.png)."""
import json
import os
import re
import subprocess
import sys
import time

import httpx
import pytest

from tests.test_integrations_ui import REPO, _free_port, playwright_env  # noqa: F401  (fixture)
from tests.support import server_up

STATIC = os.path.join(REPO, "app", "static")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


JS = _read("app.js")
PANEL = JS[JS.index("// ======================================================================== Ask TradeComply"):
           JS.index("// ======================================================================== boot")]
CARD = JS[JS.index("// Settings > Ask TradeComply (owners)"):JS.index("routes.settings = async")]
NOTE = "Answers come from Claude by Anthropic and can be wrong. What you type and tool results are sent to Anthropic."


# ---------------------------------------------------------------------------- static checks
def test_panel_sets_text_only_and_never_pops_up():
    for code in (PANEL, CARD):
        assert not re.search(r"innerHTML|insertAdjacentHTML|outerHTML|document\.write", code)
        assert not re.search(r"\balert\(|\bconfirm\(|\bprompt\(|openDialog|showModal|setInterval|setTimeout|"
                             r"location\.assign", code)
    assert PANEL.count("html:") == 1 and "html: md(t.reply)" in PANEL     # answers only through the escaping md()
    assert JS.count("location.assign(") == 2                    # unchanged: only the OAuth and SSO hand-offs
    assert NOTE in PANEL and "'aria-live': 'polite'" in PANEL and "role: 'log'" in PANEL
    assert "body: { text, page }" in PANEL                      # the page name only, never page data
    # someone else signed in on this device (an invite opened in the same tab): the last person's chats go
    assert "if (who !== ask.who) {" in PANEL and "ask.who = who; ask.conv = null; ask.chats = null;" in PANEL


def test_md_links_to_app_pages_only_in_a_strict_form():
    assert r"(#\/[a-z-]+(?:\/[A-Za-z0-9_-]+)?)" in JS
    assert "keep(`<a href=\"${href}\">${text}</a>`)" in JS      # same tab, no target: an app page
    assert "mailto:)[^\\s()\\u0000]+)\\)/g, (_, text, href) => link(href, text)" in JS     # external links unchanged


def test_ask_button_and_panel_start_hidden_and_sw_never_stores_answers():
    index, sw = _read("index.html"), _read("sw.js")
    assert re.search(r'<button id="ask-open" class="ask-btn" type="button" aria-controls="ask-panel" '
                     r'aria-expanded="false" hidden>', index)
    assert '<aside id="ask-panel" class="ask-panel" aria-labelledby="ask-title" hidden></aside>' in index   # role: askModal
    version = int(re.search(r"const SHELL_CACHE = 'tc-shell-v(\d+)'", sw).group(1))
    assert version >= 7 and f"/static/v{version}/app.js" in index and f"'/static/v{version}/app.js'" in sw
    skip = sw.index("if (url.pathname.startsWith('/api/assistant/')) return;")
    assert skip < sw.index("event.respondWith(networkFirstApi(request))")
    assert "/api/assistant" not in sw[sw.index("const API_CACHEABLE"):sw.index("self.addEventListener('install'")]


# ---------------------------------------------------------------------------- browser test (Playwright)
def _serve(tmp_path, name: str, **extra):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL",
                                                            "ANTHROPIC_API_KEY")
           and not k.startswith("ASSISTANT_")}
    env.update(DATA_DIR=str(tmp_path / name), LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0",
               MCP_REQUIRE_KEY="0", **extra)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.assistant_ui_app:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("server exited: " + proc.stderr.read().decode()[-2000:])
        try:
            if server_up(base):
                return proc, base
        except httpx.HTTPError:
            time.sleep(0.3)
    proc.kill()
    pytest.fail("server did not start")


@pytest.fixture
def servers(tmp_path, playwright_env):
    on = _serve(tmp_path, "on", ASSISTANT_DAILY_MESSAGES="4")
    off = _serve(tmp_path, "off", TC_TEST_ASSISTANT="off")
    try:
        yield on[1], off[1]
    finally:
        for proc, _ in (on, off):
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, offBase, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], overflow: {}, sizes: {} };
  const opts = { deviceScaleFactor: 1, serviceWorkers: 'block' };
  const watch = (p, tag) => {
    p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`));
    p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog ${d.message()}`); d.dismiss(); });
  };
  const text = (p, sel) => p.$eval(sel, (el) => el.innerText);
  const bars = (p, on) => p.evaluate((on) => document.querySelectorAll('.topbar, .nav').forEach((e) => { e.style.position = on ? '' : 'static'; }), on);
  async function signUp(p, url, company, email) {
    await p.goto(url + '/#/login');
    await p.waitForSelector('main form input[name=email]');
    const status = await p.evaluate(async ([company, email]) => (await fetch('/api/auth/signup', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ company_name: company, email, password: 'correct-horse-battery' }) })).status, [company, email]);
    await p.goto(url + '/#/dashboard');
    await p.waitForSelector('.tiles');
    return status;
  }
  const answered = (p, n) => p.waitForFunction((n) => document.querySelectorAll('#ask-panel .ask-turn').length === n && !document.querySelector('#ask-panel .ask-status').textContent.startsWith('Working'), n);

  for (const [tag, viewport] of [['desk', { width: 1280, height: 900 }], ['phone', { width: 375, height: 812 }]]) {
    const o = out.sizes[tag] = {};
    const shot = async (p, name, sel) => {
      const file = path.join(shots, `ai-${name}-${tag}.png`);
      if (!sel) return p.screenshot({ path: file });
      await bars(p, false);
      await p.locator(sel).screenshot({ path: file });
      await bars(p, true);
    };
    const check = async (p, name, sel) => { out.overflow[`${name}-${tag}`] = await p.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth); await shot(p, name, sel); };
    const ctx = await browser.newContext({ ...opts, viewport });
    const p = await ctx.newPage();
    watch(p, tag);
    o.signup = await signUp(p, base, `Ask Builders ${tag}`, `owner-${tag}@example.test`);

    // ---- closed: a small button in the top bar, reachable from the keyboard; it opens on Quick help
    await p.waitForSelector('#ask-open:not([hidden])');
    o.button = { text: await text(p, '#ask-open'), expanded: await p.getAttribute('#ask-open', 'aria-expanded'), inTopbar: await p.$eval('#ask-open', (b) => !!b.closest('.topbar')) };
    o.panelHiddenAtStart = await p.$eval('#ask-panel', (e) => e.hidden);
    o.tapTarget = await p.evaluate(() => {          // 44 px tall to a finger, though drawn 32 px in the top bar
      const b = document.getElementById('ask-open').getBoundingClientRect(), x = b.left + b.width / 2;
      return [b.top - 5, b.bottom + 5].every((y) => { const e = document.elementFromPoint(x, y); return !!(e && e.closest('#ask-open')); });
    });
    await check(p, 'closed');
    await p.focus('#ask-open');
    await p.keyboard.press('Enter');
    await p.waitForSelector('#ask-panel:not([hidden]) textarea');
    o.focusOnOpen = await p.evaluate(() => document.activeElement.getAttribute('aria-label'));
    o.expandedOpen = await p.getAttribute('#ask-open', 'aria-expanded');
    o.openTab = await p.getAttribute('#ask-tab-qh', 'aria-selected');
    await p.click('#ask-tab-claude');              // the Claude tab: its note right above the box, Send names Claude
    o.note = await text(p, '#ask-panel .ask-note');
    o.noteAboveBox = await p.$eval('#ask-panel .ask-note', (n) => n.nextElementSibling && n.nextElementSibling.matches('form.ask-form'));
    o.sendLabel = await text(p, '#ask-panel button[type=submit]');
    o.box = await p.$eval('#ask-panel', (e) => { const r = e.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; });
    o.role = await p.$eval('#ask-panel', (e) => [e.getAttribute('role'), e.getAttribute('aria-modal'), document.querySelector('.app').inert]);
    o.pageBeside = await p.evaluate(() => document.querySelector('main').getBoundingClientRect().right <= document.getElementById('ask-panel').getBoundingClientRect().left + 1);
    await check(p, 'open');
    await p.keyboard.press('Escape');
    await p.waitForSelector('#ask-panel', { state: 'hidden' });
    o.focusAfterEsc = await p.evaluate(() => document.activeElement.id);
    o.inertAfterClose = await p.$eval('.app', (e) => e.inert);
    await p.click('#ask-open');
    if (tag === 'desk') {                            // Esc also works after clicking the page beside the sheet
      await p.click('main h1');
      await p.keyboard.press('Escape');
    } else {                                         // the phone's Back button closes the full-screen panel
      await p.goBack();
    }
    await p.waitForSelector('#ask-panel', { state: 'hidden' });
    o.closedBy = { hash: await p.evaluate(() => location.hash) };

    // ---- a question that runs a check: the "Checked:" line, and a link to an app page
    await p.click('#ask-open');
    await p.click('#ask-tab-claude');
    await p.fill('#ask-panel textarea', 'When is my lien deadline? I last worked on Nov 1.');
    if (tag === 'desk') await p.press('#ask-panel textarea', 'Enter'); else await p.click('#ask-panel button[type=submit]');
    await answered(p, 1);
    o.checked = await text(p, '#ask-panel .ask-checked');
    o.answer = await text(p, '#ask-panel .ask-a');
    o.appLink = await p.$eval('#ask-panel .ask-a a[href="#/professionals"]', (a) => ({ target: a.getAttribute('target'), text: a.textContent }));
    o.boxEmptied = await p.$eval('#ask-panel textarea', (t) => t.value);
    await check(p, 'tool');

    // ---- an answer with HTML in it shows as text; the first turn stays as it was (the live log only grows)
    await p.$eval('#ask-panel .ask-turn', (e) => { e.dataset.seen = '1'; });
    await p.fill('#ask-panel textarea', 'Show me a script');
    await p.click('#ask-panel button[type=submit]');
    await answered(p, 2);
    o.firstTurnKept = await p.$eval('#ask-panel .ask-turn', (e) => e.dataset.seen === '1');
    o.html = { pwned: await p.evaluate(() => window.__pwned === 1), tags: await p.$$eval('#ask-panel .ask-a script, #ask-panel .ask-a img', (els) => els.length),
               shown: await p.$$eval('#ask-panel .ask-a', (els) => els[els.length - 1].innerText) };

    // ---- Stop: the button turns into Stop while it works; stopping keeps the typed text and saves nothing
    await p.fill('#ask-panel textarea', 'A slow question');
    await p.click('#ask-panel button[type=submit]');
    await p.waitForFunction(() => document.querySelector('#ask-panel button[type=submit]').textContent === 'Stop');
    o.working = await text(p, '#ask-panel .ask-status');
    await check(p, 'working');
    await p.click('#ask-panel button[type=submit]');
    await p.waitForFunction(() => document.querySelector('#ask-panel .ask-status').textContent.startsWith('Stopped'));
    o.stopped = { status: await text(p, '#ask-panel .ask-status'), typed: await p.$eval('#ask-panel textarea', (t) => t.value),
                  turns: await p.$$eval('#ask-panel .ask-turn', (els) => els.length), button: await text(p, '#ask-panel button[type=submit]') };

    // ---- the daily limit (4 here; the stopped question counts): this answer is the last; the next is refused
    await p.waitForTimeout(300);                       // the stopped turn has ended on the server
    await p.fill('#ask-panel textarea', 'Where do I check my license?');
    await p.click('#ask-panel button[type=submit]');
    await answered(p, 3);
    o.lastNote = await text(p, '#ask-panel .ask-status');
    await p.fill('#ask-panel textarea', 'One more question');
    await p.click('#ask-panel button[type=submit]');
    await p.waitForSelector('#ask-panel .ask-error[role=alert]');
    o.limit = { error: await text(p, '#ask-panel .ask-error'), typed: await p.$eval('#ask-panel textarea', (t) => t.value) };
    await check(p, 'limit');

    // ---- earlier chats, and New chat
    await p.click('#ask-panel .ask-head button:has-text("New chat")');
    o.newChat = await text(p, '#ask-panel .ask-log');
    o.chats = await text(p, '#ask-panel .ask-chats summary');
    await p.evaluate(() => { document.querySelector('#ask-panel .ask-chats').open = true; });   // keyboard: Delete, Keep
    await p.focus('#ask-panel .ask-chat-list button[aria-label^="Delete chat"]');
    await p.keyboard.press('Enter');
    o.focusOnDelete = await p.evaluate(() => document.activeElement.textContent);
    await p.keyboard.press('Enter');
    o.focusOnKeep = await p.evaluate(() => (document.activeElement.getAttribute('aria-label') || '').startsWith('Delete chat'));
    await p.evaluate(() => { document.querySelector('#ask-panel .ask-chats').open = false; });

    // ---- following an app link from an answer
    await p.click('#ask-panel .ask-chats summary');
    await p.click('#ask-panel .ask-chat-list .ask-chat-title');
    await p.waitForSelector('#ask-panel .ask-a a[href="#/professionals"]');
    await p.click('#ask-panel .ask-a a[href="#/professionals"]');
    await p.waitForFunction(() => location.hash === '#/professionals');
    o.afterLink = { hash: await p.evaluate(() => location.hash), panelOpen: await p.$eval('#ask-panel', (e) => !e.hidden) };
    if (o.afterLink.panelOpen) await p.click('#ask-panel .ask-head button:has-text("Close")');

    // ---- the owner's card in Settings: on, this month's use; switching it off hides the Claude tab, not Help
    await p.goto(base + '/#/settings');
    await p.waitForSelector('#ask-settings input[role=switch]');
    await p.waitForSelector('#ask-settings table');
    o.card = await text(p, '#ask-settings');
    await check(p, 'settings', '#ask-settings');
    await p.click('#ask-settings label.switch');
    await p.waitForSelector('#ask-settings .banner');
    o.offNote = await text(p, '#ask-settings .banner');
    o.switchedOff = await p.evaluate(() => ({ help: !document.getElementById('ask-open').hidden, claudeTab: !!document.querySelector('#ask-tab-claude') && document.querySelector('#ask-tab-claude').isConnected }));
    await p.click('#ask-settings label.switch');
    await p.waitForFunction(() => !!document.querySelector('#ask-panel [role=tablist] #ask-tab-claude'));
    o.switchedOn = await p.evaluate(() => !document.getElementById('ask-open').hidden);
    await ctx.close();

    // ---- a server where the helper is not set up: Help opens on Quick help, with no tabs and no Claude requests;
    // the owner is told which setting turns Claude on
    const offCtx = await browser.newContext({ ...opts, viewport });
    const q = await offCtx.newPage();
    watch(q, `${tag}-off`);
    const assistantCalls = [];
    q.on('request', (r) => { if (r.url().includes('/api/assistant/')) assistantCalls.push(r.url()); });
    o.offSignup = await signUp(q, offBase, `No Helper ${tag}`, `owner-off-${tag}@example.test`);
    o.offButtonHidden = await q.$eval('#ask-open', (b) => b.hidden);
    await q.click('#ask-open');
    await q.waitForSelector('#ask-panel:not([hidden]) textarea');
    o.offPanel = { tablists: await q.$$eval('#ask-panel [role=tablist]', (e) => e.length), quick: await q.$eval('#qh-pane', (e) => !e.hidden),
                   send: await text(q, '#ask-panel button[type=submit]'), title: await text(q, '#ask-title') };
    await q.click('#ask-panel .ask-head button:has-text("Close")');
    await q.goto(offBase + '/#/settings');
    await q.waitForSelector('#ask-settings');
    o.notSetUp = await text(q, '#ask-settings');
    await check(q, 'not-set-up', '#ask-settings');
    o.offAssistantCalls = assistantCalls.length;
    await offCtx.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_ask_tradecomply_in_a_real_browser(tmp_path, playwright_env, servers):
    node, env = playwright_env
    base, off_base = servers
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "ask.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, off_base, shots], env=env, capture_output=True, text=True,
                       timeout=400)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    assert not any(out["overflow"].values()), out["overflow"]
    for tag, o in out["sizes"].items():
        assert o["signup"] == 200 and o["offSignup"] == 200, tag
        assert o["button"] == {"text": "Help", "expanded": "false", "inTopbar": True} and o["panelHiddenAtStart"]
        assert o["openTab"] == "true" and o["noteAboveBox"] is True and o["sendLabel"] == "Send to Claude"
        assert o["focusOnOpen"] == "Your question" and o["expandedOpen"] == "true" and o["focusAfterEsc"] == "ask-open"
        assert o["tapTarget"] is True and o["inertAfterClose"] is False and o["closedBy"] == {"hash": "#/dashboard"}
        assert o["firstTurnKept"] is True and o["focusOnDelete"] == "Keep" and o["focusOnKeep"] is True
        assert o["note"] == NOTE
        if tag == "phone":
            assert o["box"] == {"x": 0, "y": 0, "w": 375, "h": 812}          # full screen
            assert o["role"] == ["dialog", "true", True]                     # a modal: the page behind is inert
        else:
            assert o["box"]["w"] == 420 and o["box"]["x"] + 420 == 1280 and o["box"]["y"] >= 50     # beside the page
            assert o["role"] == ["complementary", None, False] and o["pageBeside"] is True   # the page narrows
        assert o["checked"] == "Checked: Calculate lien deadline"
        assert "January 29, 2027" in o["answer"] and "not legal advice" in o["answer"]
        assert o["appLink"] == {"target": None, "text": "Find a professional"} and o["boxEmptied"] == ""
        assert o["html"]["pwned"] is False and o["html"]["tags"] == 0
        assert "<script>window.__pwned = 1</script>" in o["html"]["shown"] and "<img src=x" in o["html"]["shown"]
        assert o["working"] == "Working on it…"
        assert o["stopped"] == {"status": "Stopped. Nothing was saved.", "typed": "A slow question", "turns": 2,
                                "button": "Send to Claude"}
        assert o["lastNote"] == "That was your last question for today."
        assert "daily limit" in o["limit"]["error"] and o["limit"]["typed"] == "One more question"
        assert "Ask about a California rule" in o["newChat"] and o["chats"] == "Earlier chats (1)"
        assert o["afterLink"]["hash"] == "#/professionals"
        assert o["afterLink"]["panelOpen"] is (tag == "desk")                # phones show the page
        assert "Let my team use Ask TradeComply" in o["card"] and "This month: 4 questions" in o["card"]   # Stop counts
        assert f"owner-{tag}@example.test" in o["card"] and "Ask TradeComply is off" in o["offNote"]
        assert "Quick help is always on." in o["card"]
        assert o["switchedOff"] == {"help": True, "claudeTab": False} and o["switchedOn"] is True
        assert o["offButtonHidden"] is False                                 # Help is for everyone signed in
        assert o["offPanel"] == {"tablists": 0, "quick": True, "send": "Ask Quick help", "title": "Help"}
        assert o["offAssistantCalls"] == 0                                   # no Claude: /api/assistant is never asked
        assert "Not set up" in o["notSetUp"] and "ANTHROPIC_API_KEY" in o["notSetUp"]
