"""Check results that answer first (spec 6.6), in a real browser: the headline built only from the check's own keys
(the lien check's next deadline, a license with where its data comes from, a sub's risk, a permit determination, "No
Cal/OSHA permit found for what you entered", a contract's problems, the summary of the hours, smoke and safety
checks), the next step, Save / Print / More actions, the details (empty values left out, "Expired 1,046 days ago",
not a negative count), the incident report link under the injury check, and no developer command anywhere. Runs
when Node with Playwright and its Chromium is installed (skipped otherwise; the test server has no CSLB data, so the
license checks answer from the demo records). TC_UI_SHOTS=<directory> keeps the screenshots (ux-tool-*-result-*.png)."""
import json
import os
import re
import subprocess

from tests.test_integrations_ui import live_server, playwright_env  # noqa: F401  (fixtures)

SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], results: {} };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const box = (p, name) => p.locator(`.field[data-field=${name}] input, .field[data-field=${name}] select, .field[data-field=${name}] textarea`).first();
  for (const [vw, tag] of [[1280, 'd'], [375, 'm']]) {
    const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: vw, height: tag === 'd' ? 900 : 812 }, serviceWorkers: 'block' });
    const p = await ctx.newPage();
    p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`));
    p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); });
    await p.goto(base + '/#/login');
    await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: `Result Builders ${tag}`, email: `results-${tag}@example.test`, password: 'correct-horse-battery' } });
    const run = async (name, fill = {}, shot = null) => {
      const u = `${base}/#/tools/${name}`;
      if (p.url() === u) await p.reload(); else await p.goto(u);
      await p.waitForSelector('main form.tool-form');
      await p.waitForTimeout(400);
      await p.$$eval('main form details', (ds) => ds.forEach((d) => { d.open = true; }));
      for (const [k, v] of Object.entries(fill)) {
        while (!(await box(p, k).isVisible())) await p.click('.action-bar button:has-text("Continue")');
        const el = box(p, k);
        if ((await el.evaluate((e) => e.tagName)) === 'SELECT') await el.selectOption(v); else await el.fill(v);
        await p.keyboard.press('Escape');
      }
      while (await p.isVisible('.action-bar button:has-text("Continue")')) await p.click('.action-bar button:has-text("Continue")');
      await p.click('main form button[type=submit]');
      await p.waitForSelector('.result-headline');
      await p.waitForTimeout(200);
      const r = await p.evaluate(() => {
        const card = document.querySelector('.result');
        const det = card.querySelector('details.result-details');
        return { headline: card.querySelector('.result-headline').textContent, next: (card.querySelector('.next-step') || {}).textContent || null,
          focused: document.activeElement.classList.contains('result-headline'), detailsOpen: det ? det.open : null,
          empties: det ? [...det.querySelectorAll(':scope > div > dl.kv > dd')].filter((d) => d.textContent.trim() === '—').length : 0,
          text: card.innerText + (det ? det.textContent : ''), order: [...card.children].map((c) => c.className || c.tagName),
          incident: (card.querySelector('a[href="#/field?kind=incident"]') || {}).textContent || null,
          more: !!card.querySelector('button[aria-haspopup=menu]') };
      });
      if (shot) await p.screenshot({ path: path.join(shots, `ux-tool-${shot}-result-${tag}.png`), fullPage: true });
      out.results[`${name}${Object.keys(fill).length ? ':' + Object.values(fill)[0] : ''}:${tag}`] = r;
    };
    await run('calculate_lien_deadline', { claimant_role: 'subcontractor', first_furnishing_date: '2026-11-03', last_furnishing_date: '2026-11-20' }, 'lien');
    await run('verify_contractor_license', { license_number: '123456' }, 'verify');
    await run('verify_contractor_license', { license_number: '999999' });
    await run('verify_contractor_license', { license_number: '24680' });
    await run('check_subcontractor_compliance', { license_number: '999999', required_classification: 'B' });
    await run('permit_required', { work_description: '10x12 storage shed', structure_type: 'detached_accessory_structure' });
    await run('check_cal_osha_permits', {});
    await run('validate_home_improvement_contract', { contract_price_usd: '20000' });
    await run('construction_hours', { city: 'San Diego', when: '2026-11-28T06:30' });
    await run('lookup_safety_standard', { hazard: 'trench' });
    await run('injury_reporting_deadlines', { incident_datetime: '2026-11-20T14:30' });
    if (tag === 'd') {
      await p.focus('.result button[aria-haspopup=menu]');
      await p.keyboard.press('Enter');
      out.menu = await p.evaluate(() => ({ items: [...document.querySelectorAll('.result [role=menu] [role=menuitem]')].map((i) => i.textContent), focus: document.activeElement.getAttribute('role') }));
      await p.keyboard.press('Escape');
      out.menuClosed = await p.evaluate(() => ({ hidden: document.querySelector('.result [role=menu]').hidden, focus: document.activeElement.getAttribute('aria-haspopup') }));
    }
    await ctx.close();
  }
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_results_answer_first(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "results.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=500)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == []
    for tag in ("d", "m"):
        res = {k.rsplit(":", 1)[0]: v for k, v in out["results"].items() if k.endswith(":" + tag)}
        lien = res["calculate_lien_deadline:subcontractor"]
        assert re.fullmatch(r"Next: lien recording — [A-Z][a-z]{2} \d{1,2}, \d{4} \((in \d+ days|tomorrow|today)\)", lien["headline"]), lien["headline"]
        assert lien["next"].startswith("Next step: 90 days after last furnishing") and lien["focused"]
        assert lien["detailsOpen"] is (tag == "d") and lien["empties"] == 0 and lien["more"]
        # the answer comes before the details
        assert lien["order"].index("card-head") < lien["order"].index("next-step") < [i for i, c in enumerate(lien["order"]) if "result-details" in c][0]
        ok = res["verify_contractor_license:123456"]
        assert ok["headline"] == "Demo data, not a live CSLB record — Example Plumbing Inc.: Active · expires Jun 30, 2027"
        assert not (ok["next"] or "").startswith("Next step: Demo data")                # said once, in the headline
        lapsed = res["verify_contractor_license:999999"]
        assert lapsed["headline"].startswith("Demo data, not a live CSLB record — Lapsed Builders LLC: Expired · expired Jan 15, 2024")
        assert re.search(r"Expired [\d,]+ days ago", lapsed["text"]) and "-1," not in lapsed["text"]
        missing = res["verify_contractor_license:24680"]["headline"]
        assert missing.startswith("No record found.")                                # the check's own first note, word for word
        assert res["check_subcontractor_compliance:999999"]["headline"] == "Do not use"          # RISK_TEXT[overall_risk]
        assert res["permit_required:10x12 storage shed"]["headline"] in ("Need more info", "Not required", "Required")
        assert res["check_cal_osha_permits"]["headline"] == "No Cal/OSHA permit found for what you entered"
        hic = res["validate_home_improvement_contract:20000"]["headline"]
        assert hic == "No problems found in what was checked" or re.fullmatch(r"\d+ problems?, \d+ warnings?", hic)
        assert res["construction_hours:San Diego"]["headline"].startswith("San Diego, construction, Saturday 2026-11-28 at 6:30 a.m.")
        assert res["lookup_safety_standard:trench"]["headline"] == "Excavation hazards and cave-in protection."
        assert res["injury_reporting_deadlines:2026-11-20T14:30"]["incident"].strip() == "Write the incident report"
        for v in res.values():
            assert "python -m" not in v["text"] and "Copy as JSON" not in v["text"]
    assert out["menu"] == {"items": ["Copy the data (for developers)"], "focus": "menuitem"}
    assert out["menuClosed"] == {"hidden": True, "focus": "menu"}
