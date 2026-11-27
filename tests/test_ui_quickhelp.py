"""The Help panel in a real browser (spec 5.7, 5.8): one Help button for everyone signed in, a panel that always opens
on Quick help, the Ask TradeComply tab only where Claude is available (and no /api/assistant request where it isn't),
one box whose Send button names where the words go, Claude's note right above it on the Claude tab, Quick help waiting
while a Claude answer runs, answer cards (rule text with its correction, citation and status; requirement cards;
actions; follow-ups; a status line that says only the lead and the counts), a check opened with what Quick help
understood filled in and nothing run, a prefill that never reaches another check or a later visit, route answers
without rule cards, the hand-off to Claude that sends nothing, the human route after three misses, offline, the
glossary "(?)" buttons named by their term only, and nothing of one company's Quick help left when another signs in in
the same tab. Runs against tests/assistant_ui_app.py (fake Anthropic API; one server with Claude, one without) at 1280
and 375 px when Node with Playwright and its Chromium is installed (skipped otherwise). TC_UI_SHOTS=<directory> keeps
the screenshots (ux-help-*.png)."""
import json
import os
import re
import subprocess

from tests.test_integrations_ui import REPO, playwright_env  # noqa: F401  (fixture)
from tests.test_ui_assistant import servers  # noqa: F401  (fixture: fake Claude on, no key)

JS = open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8").read()
SECTION = JS[JS.index("// ======================================================================== Quick help (built in)"):
             JS.index("// ======================================================================== more page")]


def test_quick_help_builds_text_only_and_sends_nothing_in_an_address():
    for banned in ("html:", "innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "alert(", "confirm(", "prompt("):
        assert banned not in SECTION, banned
    assert "api('/api/help', { method: 'POST'" in SECTION and "'/api/help?" not in JS
    # the fixed lines the panel itself writes (spec 5.9)
    for line in ("Quick help is built into TradeComply. It matches your words to TradeComply\\'s checks, rules and pages. It is "
                 "not an AI model, and what you type is not sent outside TradeComply. It can miss what you mean, and it never "
                 "replaces a licensed professional.",
                 "Quick help doesn\\'t save your questions.",
                 "Quick help needs a connection. Search still finds checks, pages and projects saved on this phone.",
                 "That\\'s a lot of questions in a minute. Wait a moment and try again.",
                 "Wait for Claude\\'s answer, or press Stop on the Ask TradeComply tab.",
                 "What changed when TradeComply checked the official text: "):
        assert line in JS, line
    # nothing remembers the Claude tab; every open starts on Quick help
    assert "tc.helpTab" not in JS and "showTab('qh');                     // every open starts on Quick help" in JS
    assert "state.prefill = { tool: b.tool, args: b.args || {}, from: 'quickhelp', projectId: b.project_id || null };" in SECTION


SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, offBase, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], sizes: {} };
  const watch = (p, tag, urls) => {
    p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`));
    p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog ${d.message()}`); d.dismiss(); });
    p.on('request', (r) => urls.push([r.method(), r.url()]));
  };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  async function signUp(p, url, company, email) {
    await p.goto(url + '/#/login');
    await p.waitForSelector('main form input[name=email]');
    const s = (await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: company, email, password: 'correct-horse-battery' } })).status;
    await p.goto(url + '/#/dashboard');
    await p.waitForSelector('.tiles');
    return s;
  }
  const shot = (p, name, tag) => p.screenshot({ path: path.join(shots, `ux-${name}-${tag === 'desk' ? 'd' : 'm'}.png`) });
  const turns = (p) => p.$$eval('#ask-panel .qh-turn', (e) => e.length);
  async function ask(p, q, tag) {
    const n = await turns(p);
    await p.fill('#ask-panel textarea', q);
    if (tag === 'desk') await p.press('#ask-panel textarea', 'Enter'); else await p.click('#ask-panel button[type=submit]');
    await p.waitForFunction((n) => document.querySelectorAll('#ask-panel .qh-turn').length === n + 1
      && !!document.querySelector('#ask-panel .qh-turn:last-child .qh-a, #ask-panel .qh-turn:last-child .ask-error')
      && !document.querySelector('#ask-panel .qh-log[aria-busy]'), n);
    await p.waitForTimeout(100);
    return p.$eval('#ask-panel .qh-turn:last-child', (t) => ({
      q: t.querySelector('.qh-q').textContent,
      lead: (t.querySelector('.qh-lead') || {}).textContent || '',
      lines: [...t.querySelectorAll('.qh-line')].map((x) => x.textContent),
      understood: [...t.querySelectorAll('.qh-chip')].map((x) => x.textContent),
      rules: [...t.querySelectorAll('.qh-card')].filter((c) => c.querySelector('a[href^="#/rule/"]')).map((c) => ({ title: c.querySelector('h3').textContent,
        meta: c.querySelector('.qh-meta').textContent, badge: (c.querySelector('.badge') || {}).textContent || '',
        correction: (c.querySelector('.correction') || {}).textContent || '', read: c.querySelector('a[href^="#/rule/"]').getAttribute('href') })),
      reqs: [...t.querySelectorAll('.qh-card')].filter((c) => !c.querySelector('a[href^="#/rule/"]')).map((c) => ({ name: c.querySelector('h3').textContent, meta: c.querySelector('.qh-meta').textContent })),
      actions: [...t.querySelectorAll('.qh-act > button')].map((b) => ({ label: b.textContent, primary: !b.classList.contains('secondary') })),
      hints: [...t.querySelectorAll('.qh-act > p')].map((x) => x.textContent),
      followups: [...t.querySelectorAll('.qh-a > .qh-chips .chip')].map((x) => x.textContent),
      human: (t.querySelector('.qh-human') || {}).textContent || null,
      disclaimer: (t.querySelector('.qh-disc') || {}).textContent || '',
      say: document.querySelector('#qh-pane [role=status]').textContent }));
  }
  const tags = (p) => p.$$eval('main .field-tag:not([hidden])', (els) => els.map((e) => e.textContent.trim()));
  const toolPosts = (urls) => urls.filter(([m, u]) => m === 'POST' && /\/api\/tools\/[a-z_]+$/.test(u)).length;

  for (const [tag, viewport] of [['desk', { width: 1280, height: 800 }], ['phone', { width: 375, height: 812 }]]) {
    const o = out.sizes[tag] = {};
    const urls = [];
    const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport, serviceWorkers: 'block' });
    // before the app's own listener: when asked, the address moves on to another check before the app reads it
    await ctx.addInitScript(() => window.addEventListener('hashchange', () => {
      if (window.__swap && location.hash === window.__swap[0]) history.replaceState(history.state, '', window.__swap[1]);
    }));
    const p = await ctx.newPage();
    watch(p, tag, urls);
    o.signup = await signUp(p, base, `Sierra Build Co ${tag}`, `qh-${tag}@example.test`);

    // ---- the Help button, and the panel opening on Quick help
    o.button = { text: (await p.textContent('#ask-open')).trim(), hidden: await p.$eval('#ask-open', (b) => b.hidden) };
    await p.click('#ask-open');
    await p.waitForSelector('#ask-panel:not([hidden]) .qh-starters .chip');
    const panel = () => p.evaluate(() => {
      const send = document.querySelector('#ask-panel button[type=submit]');
      const above = send.closest('form').previousElementSibling;
      return { qh: document.getElementById('ask-tab-qh').getAttribute('aria-selected'), claude: document.getElementById('ask-tab-claude').getAttribute('aria-selected'),
        send: send.textContent, disabled: send.disabled, above: above.className, aboveText: above.textContent, focus: document.activeElement.getAttribute('aria-label'),
        title: document.getElementById('ask-title').textContent, reset: document.querySelector('.ask-head button').textContent };
    });
    o.opened = await panel();
    o.starters = await p.$$eval('#ask-panel .qh-starters .chip', (c) => c.map((x) => x.textContent));
    o.can = await p.$eval('#ask-panel .qh-can', (d) => d.open);
    await shot(p, 'help-quick-empty', tag);

    // ---- five sample questions
    o.lien = await ask(p, 'When is my prelim due if I first delivered lumber on Nov 3?', tag);
    o.canAfter = await p.$eval('#ask-panel .qh-can', (d) => d.open);
    await shot(p, 'help-quick-lien', tag);
    o.down = await ask(p, 'How much can I take as a down payment?', tag);
    o.engineer = await ask(p, 'do I need an engineer to design sloping for an excavation deeper than 20 feet', tag);
    o.hours = await ask(p, 'Can we start at 6:30 am Saturday in San Diego?', tag);
    o.shed = await ask(p, 'Do I need a permit for a 10x12 shed?', tag);
    // ---- route answers: no rule cards
    o.guard = await ask(p, 'can we skip respirators when the AQI is 160', tag);
    await shot(p, 'help-quick-guard', tag);
    o.sue = await ask(p, 'Should I sue the owner for not paying me?', tag);

    // ---- the hand-off to Claude: the Claude tab with the words typed in, nothing sent
    const sends = () => urls.filter(([m, u]) => m === 'POST' && u.includes('/api/assistant/')).length;
    const before = sends();
    await p.click('#ask-panel .qh-turn:last-child .qh-act > button:has-text("Ask TradeComply")');
    o.handoff = { ...(await panel()), text: await p.inputValue('#ask-panel textarea'), sent: sends() - before };
    await shot(p, 'help-claude-tab', tag);

    // ---- a slow Claude answer: Quick help waits and says why; Stop still stops it
    await p.fill('#ask-panel textarea', 'A slow question');
    await p.click('#ask-panel button[type=submit]');
    await p.waitForFunction(() => document.querySelector('#ask-panel button[type=submit]').textContent === 'Stop');
    await p.click('#ask-tab-qh');
    o.waiting = { ...(await panel()), wait: await p.$eval('#qh-wait', (w) => (w.hidden ? null : w.textContent)) };
    await p.click('#ask-tab-claude');
    o.stopShown = (await panel()).send;
    await p.click('#ask-panel button[type=submit]');
    await p.waitForFunction(() => document.querySelector('#ask-panel .ask-status').textContent.startsWith('Stopped'));
    o.stopped = { status: await p.textContent('#ask-panel .ask-status'), qhTurnsInClaude: await p.$$eval('#claude-pane .qh-turn', (e) => e.length),
      claudeTurnsInQh: await p.$$eval('#qh-pane .ask-turn', (e) => e.length) };

    // ---- closed and opened again: Quick help
    await p.click('#ask-panel .ask-head button:has-text("Close")');
    await p.click('#ask-open');
    await p.waitForSelector('#ask-panel:not([hidden])');
    o.reopened = await panel();

    // ---- three misses in a row: the human route
    await p.click('#ask-panel .ask-head button:has-text("New question")');
    o.afterNew = await turns(p);
    o.miss1 = await ask(p, 'zxqv flurb glorp', tag);
    o.miss2 = await ask(p, 'plorf zing wubbo', tag);
    o.miss3 = await ask(p, 'qqqx wwwx eeex rrrx', tag);
    await shot(p, 'help-quick-notfound-3', tag);

    // ---- open_tool: the check opens with what was understood filled in; nothing runs
    await p.click('#ask-panel .ask-head button:has-text("New question")');
    await ask(p, 'When is my prelim due if I first delivered lumber on Nov 3?', tag);
    const posts = toolPosts(urls);
    await p.click('#ask-panel .qh-turn:last-child .qh-act > button:not(.secondary)');
    await p.waitForFunction(() => location.hash === '#/tools/calculate_lien_deadline' && document.querySelectorAll('main .field-tag:not([hidden])').length > 0);
    await p.waitForTimeout(400);
    o.prefilled = { tags: await tags(p), line: await p.$eval('main .fill-line:not([hidden])', (e) => e.innerText), panel: await p.$eval('#ask-panel', (e) => !e.hidden),
      role: await p.inputValue('main .field[data-field=claimant_role] select'), first: await p.inputValue('main .field[data-field=first_furnishing_date] input'), ran: toolPosts(urls) - posts };
    await shot(p, 'tool-lien-from-quick-help', tag);
    // ... once: another check, then this one again, show nothing from Quick help
    await p.evaluate(() => { location.hash = '#/tools/stop_payment_notice_deadline'; });
    await p.waitForSelector('main form.tool-form');
    await p.waitForTimeout(300);
    o.otherCheck = await tags(p);
    await p.evaluate(() => { location.hash = '#/tools/calculate_lien_deadline'; });
    await p.waitForFunction(() => !!document.querySelector('main .field[data-field=claimant_role] select'));
    await p.waitForTimeout(300);
    o.lienAgain = await tags(p);
    // ... and a prefill for one check never fills another: the address changes to another check before it opens
    await p.evaluate(() => { location.hash = '#/dashboard'; });       // a different page first, so the action changes the address
    await p.waitForSelector('.tiles');
    if (await p.$eval('#ask-panel', (e) => e.hidden)) await p.click('#ask-open');
    await ask(p, 'When is my prelim due if I first delivered lumber on Nov 3?', tag);
    await p.evaluate(() => { window.__swap = ['#/tools/calculate_lien_deadline', '#/tools/stop_payment_notice_deadline']; });
    await p.click('#ask-panel .qh-turn:last-child .qh-act > button:not(.secondary)');
    await p.waitForFunction(() => location.hash === '#/tools/stop_payment_notice_deadline' && !!document.querySelector('main form.tool-form'));
    await p.waitForTimeout(400);
    o.swapped = await tags(p);
    await p.evaluate(() => { window.__swap = null; location.hash = '#/tools/calculate_lien_deadline'; });
    await p.waitForFunction(() => !!document.querySelector('main .field[data-field=claimant_role] select'));
    await p.waitForTimeout(400);
    o.swappedThenLien = await tags(p);

    // ---- glossary "(?)": named by the term only; the popover shows the rule
    const labels = await p.$$eval('main .tool-form label, main .tool-form legend', (ls) => ls.map((l) => l.textContent.replace(/\*|\(optional\)/g, '').trim()).filter(Boolean));
    o.gloss = await p.$$eval('main .gloss-btn', (bs) => bs.map((b) => b.getAttribute('aria-label')));
    o.glossHasLabel = o.gloss.some((g) => labels.some((l) => g.includes(l)));
    await p.click('main .gloss-btn');
    await p.waitForSelector('.gloss-pop a[href^="#/rule/"]');
    o.pop = await p.evaluate(() => ({ role: document.querySelector('.gloss-pop').getAttribute('role'), focus: document.activeElement.classList.contains('gloss-pop'),
      text: document.querySelector('.gloss-pop').innerText }));
    await p.keyboard.press('Escape');
    o.popClosed = await p.evaluate(() => ({ gone: !document.querySelector('.gloss-pop'), focus: (document.activeElement.getAttribute('aria-label') || '') }));

    // ---- offline: the pane says so, Send waits
    if (await p.$eval('#ask-panel', (e) => e.hidden)) await p.click('#ask-open');
    await ctx.setOffline(true);
    await p.waitForTimeout(300);
    o.offline = await p.evaluate(() => ({ text: document.querySelector('.qh-offline').hidden ? null : document.getElementById('qh-offline').textContent,
      disabled: document.querySelector('#ask-panel button[type=submit]').disabled, why: document.querySelector('#ask-panel button[type=submit]').getAttribute('aria-describedby') }));
    await shot(p, 'help-quick-offline', tag);
    await ctx.setOffline(false);
    await p.waitForTimeout(200);
    o.backOnline = await p.$eval('#ask-panel button[type=submit]', (b) => b.disabled);

    // ---- another person signs in in this tab (an invite link): none of this person's Quick help stays
    if (tag === 'desk') {
      const bc = await browser.newContext({ deviceScaleFactor: 1, viewport, serviceWorkers: 'block' });
      const bp = await bc.newPage();
      await bp.goto(base + '/#/login');
      await call(bp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Bay Builders', email: 'bay@example.test', password: 'correct-horse-battery' } });
      const inv = (await call(bp, '/api/users', { method: 'POST', body: { email: 'bay-office@example.test', role: 'office' } })).data;
      await bc.close();
      o.turnsBefore = await turns(p);
      await p.goto(base + '/#/invite/' + inv.invite_path.split('/').pop());
      await p.waitForSelector('input[name=pw]');
      await p.fill('input[name=pw]', 'office-horse-battery');
      await p.fill('input[name=pw_again]', 'office-horse-battery');
      await p.click('main button[type=submit]');
      await p.waitForSelector('main h1:has-text("Welcome")');
      await p.click('#ask-open');
      await p.waitForSelector('#ask-panel:not([hidden])');
      o.turnsAfter = await turns(p);
      o.companyB = await p.textContent('#company');
    }
    await ctx.close();

    // ---- a server without Claude: Help, Quick help only, no /api/assistant request
    const offUrls = [];
    const oc = await browser.newContext({ deviceScaleFactor: 1, viewport, serviceWorkers: 'block' });
    const q = await oc.newPage();
    watch(q, `${tag}-off`, offUrls);
    o.offSignup = await signUp(q, offBase, `No Claude ${tag}`, `qh-off-${tag}@example.test`);
    o.offButton = await q.$eval('#ask-open', (b) => b.hidden);
    await q.click('#ask-open');
    await q.waitForSelector('#ask-panel:not([hidden]) .qh-starters .chip');
    o.off = { tablists: await q.$$eval('#ask-panel [role=tablist]', (e) => e.length), send: await q.textContent('#ask-panel button[type=submit]') };
    o.offSue = await ask(q, 'Should I sue the owner for not paying me?', tag);
    await shot(q, 'help-off-server', tag);
    o.offAssistant = offUrls.filter(([, u]) => u.includes('/api/assistant/')).length;
    o.helpUrls = [...urls, ...offUrls].filter(([, u]) => u.includes('/api/help')).map(([m, u]) => [m, new URL(u).search]);
    await oc.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_help_panel_and_quick_help_in_a_real_browser(tmp_path, playwright_env, servers):  # noqa: F811
    node, env = playwright_env
    base, off_base = servers
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "quickhelp.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), base, off_base, shots], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    note_claude = "Answers come from Claude by Anthropic and can be wrong. What you type and tool results are sent to Anthropic."
    disclaimer = "Compliance information, not legal advice."
    for tag, o in out["sizes"].items():
        assert o["signup"] == 200 and o["offSignup"] == 200, tag
        assert o["button"] == {"text": "Help", "hidden": False} and o["offButton"] is False
        # opens on Quick help; the box says where the words go
        op = o["opened"]
        assert (op["qh"], op["claude"], op["send"], op["title"], op["reset"]) == ("true", "false", "Ask Quick help", "Help", "New question")
        assert op["above"] == "qh-storage" and op["aboveText"] == "Quick help doesn't save your questions." and op["focus"] == "Your question"
        assert o["starters"] == ["When is my prelim due?", "Can we start at 6:30 am Saturday in San Diego?", "Do I need a permit for a 10x12 shed?"]
        assert o["can"] is True and o["canAfter"] is False                 # "What Quick help can do" folds after an answer
        # the lien question: rule cards with citation and status, the check to open, follow-ups; the status line
        lien = o["lien"]
        assert lien["lead"] == "This check works out your answer. These rules are about your question."
        assert "Your role on this job: Material supplier" in lien["understood"] and "First furnishing date: Nov 3, 2026" in lien["understood"]
        assert any(x.startswith("I read “Nov 3” as Nov 3, 2026.") for x in lien["lines"])
        assert lien["rules"] and all(r["badge"] in ("Not yet verified",) or r["badge"].startswith("Verified") for r in lien["rules"])
        assert lien["rules"][0]["title"] == "Preliminary notice due within 20 days of first furnishing"
        assert "Civil Code 8200(a)" in lien["rules"][0]["meta"] and lien["rules"][0]["read"] == "#/rule/prelim-notice-20-day-deadline"
        assert lien["actions"][0] == {"label": "Open Calculate lien deadline", "primary": True}
        assert lien["hints"][0] == "Opens the check with these filled in. Nothing runs until you press Run check."
        assert "Still needed in the check: Last furnishing date." in lien["hints"] and lien["followups"]
        assert lien["say"].startswith(lien["lead"] + " ") and re.fullmatch(r"\d+ \w+(, \d+ \w+)*\.", lien["say"][len(lien["lead"]) + 1:])
        assert lien["disclaimer"].startswith(disclaimer)
        # a corrected rule: the correction right under the plain English
        dp = next(r for r in o["down"]["rules"] if r["read"] == "#/rule/hic-down-payment-cap")
        assert dp["correction"].startswith("What changed when TradeComply checked the official text: Cap itself confirmed")
        assert dp["badge"].startswith("Verified")
        # a professional the law requires here: the requirement card with its legal basis, and the state's lookups
        eng = o["engineer"]
        assert eng["reqs"] and "8 CCR 1541.1" in eng["reqs"][0]["meta"] and "Confirmed" in eng["reqs"][0]["meta"]
        assert {"label": "Find a professional engineer (official lookups)", "primary": False} in eng["actions"]
        assert o["hours"]["actions"][0]["label"] == "Open Construction hours"
        assert o["shed"]["actions"][0]["label"] == "Open Is a permit needed?"
        # route answers: the note, the next step, no rule cards
        g = o["guard"]
        assert g["lines"][0].startswith("TradeComply can't help skip or work around a requirement.") and not g["rules"] and not g["reqs"]
        assert not any(a["label"].startswith("Open ") for a in g["actions"])
        s = o["sue"]
        assert s["lines"][0] == "Whether to sue, and how, is legal advice. An attorney decides that with you." and not s["rules"]
        assert [a["label"] for a in s["actions"]] == ["Find an attorney (official lookups)", "Ask TradeComply (Claude)"]
        # the hand-off: the Claude tab, the words typed in, nothing sent; Claude's note right above the box
        h = o["handoff"]
        assert (h["qh"], h["claude"], h["send"], h["reset"]) == ("false", "true", "Send to Claude", "New chat")
        assert h["text"] == "Should I sue the owner for not paying me?" and h["sent"] == 0
        assert h["above"] == "ask-note" and h["aboveText"] == note_claude
        # while Claude answers, Quick help waits and says why; Stop stays with the Claude tab and still stops it
        w = o["waiting"]
        assert w["qh"] == "true" and w["disabled"] is True and w["send"] == "Ask Quick help"
        assert w["wait"] == "Wait for Claude's answer, or press Stop on the Ask TradeComply tab."
        assert o["stopShown"] == "Stop" and o["stopped"]["status"].startswith("Stopped")
        assert o["stopped"]["qhTurnsInClaude"] == 0 and o["stopped"]["claudeTurnsInQh"] == 0
        assert o["reopened"]["qh"] == "true" and o["reopened"]["send"] == "Ask Quick help"    # always opens on Quick help
        # three misses in a row: the human route (an owner: whoever runs this server)
        assert o["afterNew"] == 0
        assert o["miss1"]["human"] is None and o["miss2"]["human"] is None
        assert o["miss3"]["human"] == "For help with TradeComply, ask whoever runs this TradeComply server."
        assert o["miss1"]["lead"] == "I couldn't find a check or rule for that in TradeComply."
        assert {"label": "Find a professional", "primary": False} in o["miss1"]["actions"]
        # the check opened with what Quick help understood, tagged, with what is still needed; nothing ran
        pf = o["prefilled"]
        assert pf["tags"].count("From Quick help") == 2 and pf["role"] == "supplier" and pf["first"] == "2026-11-03" and pf["ran"] == 0
        assert "Filled in from Quick help: check these before you run it." in pf["line"] and "Still needed: Last furnishing date." in pf["line"]
        assert pf["panel"] is (tag == "desk")                                # phones show the check
        assert "From Quick help" not in o["otherCheck"] and "From Quick help" not in o["lienAgain"]
        assert "From Quick help" not in o["swapped"] and "From Quick help" not in o["swappedThenLien"]
        # glossary buttons: the term only
        assert o["gloss"] and all(re.fullmatch(r"What does [^?]+ mean\?", x) for x in o["gloss"]) and o["glossHasLabel"] is False
        assert o["pop"]["role"] == "dialog" and o["pop"]["focus"] is True and "Read the rule" in o["pop"]["text"]
        assert o["popClosed"]["gone"] and o["popClosed"]["focus"].startswith("What does ")
        # offline
        assert o["offline"] == {"text": "Quick help needs a connection. Search still finds checks, pages and projects saved on this phone.",
                                "disabled": True, "why": "qh-offline"}
        assert o["backOnline"] is False
        # no Claude on this server: no tabs, no Claude requests, no hand-off
        assert o["off"] == {"tablists": 0, "send": "Ask Quick help"} and o["offAssistant"] == 0
        assert "Ask TradeComply (Claude)" not in [a["label"] for a in o["offSue"]["actions"]]
        assert all(m == "POST" and qs == "" for m, qs in o["helpUrls"])     # questions only in POST bodies
        if tag == "desk":
            assert o["turnsBefore"] > 0 and o["turnsAfter"] == 0 and o["companyB"] == "Bay Builders"
