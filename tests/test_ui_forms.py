"""Check forms in a real browser (spec 6.1-6.5): the "For job" select (outside the check's <form>, filled only by an
explicit choice, tags, the lasting "Filled from ... · Undo" line and the toast's Undo, never over a typed value), the
hand-made steps and sections (Continue checks one step, Review lists only what applies, a skipped field with a
default says what the check uses, Change goes back to the field, the named final button), "More details" on other
long forms, show-if (hidden fields are not sent), date and date-time boxes, one box for a number or words with a unit
(the typed text is sent as typed), Enter to the next field, and drafts: kept and offered back after a reload and
after the session ended, gone after Log out and never shown to another person, dropped when the check changed,
never for the injury check or an incident report, 10 a person and 200,000 characters on the device (oldest first),
and a full storage handled by dropping the oldest draft once. Runs when Node with Playwright and its Chromium is
installed (skipped otherwise). TC_UI_SHOTS=<directory> keeps the screenshots (ux-tool-*.png, ux-draft-*.png)."""
import json
import os
import subprocess

from tests.test_integrations_ui import REPO, live_server, playwright_env  # noqa: F401  (fixtures)

JS = open(os.path.join(REPO, "app", "static", "app.js"), encoding="utf-8").read()
LAYOUTS = {"validate_home_improvement_contract": "Check this contract", "generate_safety_programs": "Show my safety programs",
           "permit_required": "Check if a permit is needed", "project_setup_checklist": "Make my checklist",
           "project_risk_score": "Grade this project"}
DATES = {"check_change_order": ["buyer_signed_on", "contractor_signed_on", "changed_work_starts_on"],
         "home_improvement_cancellation_deadline": ["received_signed_copy_on"],
         "validate_home_improvement_contract": ["contract_signed_on", "buyer_received_signed_copy_on"],
         "check_crew_certifications": ["firm_rrp_certification_expires"], "compliance_calendar": ["started_paying_wages_on"],
         "portable_engine_requirements": ["registration_expires", "project_start"], "truck_compliance": ["registration_expires"]}
DATETIMES = {"injury_reporting_deadlines": ["incident_datetime", "employer_knew_at"], "construction_hours": ["when"],
             "check_cal_osha_permits": ["planned_start"]}


def test_drafts_leave_out_the_injury_check_and_secrets():
    assert "spec.name === 'injury_reporting_deadlines' ? null : draftUi(" in JS
    assert "dui = k === 'incident' ? null : draftUi(" in JS          # the field log: never an incident report
    assert "const NEVER_DRAFT = /password|api_?key|secret|token/;" in JS
    assert "const DRAFT = { days: 14, max: 20000, perUser: 10, device: 200000 };" in JS


SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, shots, dates, datetimes, layouts] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [] };
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); }); };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const tool = async (p, name, q = '') => { const u = `${base}/#/tools/${name}${q}`; if (p.url() === u) await p.reload(); else await p.goto(u); await p.waitForSelector('main form.tool-form'); await p.waitForTimeout(500); };
  const box = (p, name) => p.locator(`.field[data-field=${name}] input, .field[data-field=${name}] select, .field[data-field=${name}] textarea`).first();
  const sent = async (p, name, act) => { const [req] = await Promise.all([p.waitForRequest((r) => r.url().endsWith(`/api/tools/${name}`)), act()]); return JSON.parse(req.postData()); };
  const store = (p) => p.evaluate(() => { const o = {}; for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); o[k] = localStorage.getItem(k); } return o; });
  const draftKeys = async (p) => Object.keys(await store(p)).filter((k) => k.startsWith('tc.draft.'));

  const ctx = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
  const p = await ctx.newPage();
  watch(p, 'owner');
  await p.goto(base + '/#/login');
  out.signup = (await call(p, '/api/auth/signup', { method: 'POST', body: { company_name: 'Form Builders', email: 'forms@example.test', password: 'correct-horse-battery', cslb_license: '123456' } })).status;
  const me = (await call(p, '/api/me')).data;
  const elm = (await call(p, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', claimant_role: 'subcontractor',
    first_furnishing_date: '2026-11-03', last_furnishing_date: '2026-11-20', contract_price: 48000 } })).data;

  // ---- "For job": outside the form, starts on "Pick a job", fills only what is empty, Undo puts it back exactly
  await tool(p, 'calculate_lien_deadline');
  out.forJob = await p.evaluate(() => { const s = document.querySelector('select[name=fill_project]'); return { value: s.value, label: document.querySelector(`label[for="${s.id}"]`).textContent,
    inForm: !!s.closest('form'), first: s.options[0].textContent, tags: document.querySelectorAll('.field-tag:not([hidden])').length }; });
  await box(p, 'first_furnishing_date').fill('2026-10-15');          // typed by the person: never replaced
  await p.selectOption('select[name=fill_project]', String(elm.id));
  await p.waitForTimeout(200);
  const vals = (pg) => pg.evaluate(() => Object.fromEntries([...document.querySelectorAll('main form .field[data-field]')].map((f) => [f.dataset.field, (f.querySelector('input, select') || {}).value])));
  out.filled = { vals: await vals(p), tags: await p.$$eval('.field-tag:not([hidden])', (t) => t.map((x) => x.closest('.field').dataset.field)),
    line: await p.textContent('.fill-line'), toast: await p.textContent('#toast') };
  await p.screenshot({ path: path.join(shots, 'ux-tool-lien-prefilled-d.png'), fullPage: true });
  await p.click('.fill-line button:has-text("Undo")');
  out.undone = { vals: await vals(p), tags: await p.$$eval('.field-tag:not([hidden])', (t) => t.length), job: await p.inputValue('select[name=fill_project]') };
  await p.selectOption('select[name=fill_project]', String(elm.id));
  await p.click('#toast .toast-act');
  out.toastUndo = await vals(p);
  // Enter in a one-line box goes to the next field, and runs nothing
  let ran = 0; p.on('request', (r) => { if (r.url().includes('/api/tools/calculate_lien_deadline')) ran++; });
  await box(p, 'first_furnishing_date').focus();
  await p.keyboard.press('Enter');
  out.enter = { ran, field: await p.evaluate(() => (document.activeElement.closest('.field') || {}).dataset ? document.activeElement.closest('.field').dataset.field : null) };

  // ---- the hand-made steps: permit_required
  await tool(p, 'permit_required');
  out.steps = { title: await p.textContent('.step-title'), stepper: await p.$$eval('.stepper li', (l) => l.length) };
  await p.click('.action-bar button:has-text("Continue")');
  out.steps.blocked = { title: await p.textContent('.step-title'), summary: await p.$$eval('.banner.error li', (l) => l.map((x) => x.textContent)) };
  await box(p, 'work_description').fill('6 ft wood fence along the back lot line');
  await box(p, 'structure_type').selectOption('fence');
  await p.click('.action-bar button:has-text("Continue")');
  out.steps.two = { title: await p.textContent('.step-title'),
    shown: await p.$$eval('.form-step:not([hidden]) .field[data-field]', (f) => f.filter((x) => !x.hidden).map((x) => x.dataset.field)) };
  await p.screenshot({ path: path.join(shots, 'ux-tool-permit-step2-d.png'), fullPage: true });
  await box(p, 'fence_height_ft').fill('6');
  for (let i = 0; i < 4; i++) await p.click('.action-bar button:has-text("Continue")');
  out.review = { title: await p.textContent('.step-title'), rows: await p.$$eval('.rv-row', (r) => r.map((x) => [x.querySelector('dt').textContent, x.querySelector('dd span').textContent])),
    run: await p.textContent('main form button[type=submit]'), hint: await p.textContent('.run-hint') };
  await p.screenshot({ path: path.join(shots, 'ux-tool-permit-review-d.png'), fullPage: true });
  await p.click('.rv-row:has(dt:text-is("Fence height (ft)")) button');
  out.review.change = { title: await p.textContent('.step-title'), focus: await p.evaluate(() => document.activeElement.closest('.field').dataset.field) };
  for (let i = 0; i < 4; i++) await p.click('.action-bar button:has-text("Continue")');
  out.permitSent = await sent(p, 'permit_required', () => p.click('main form button[type=submit]'));

  // ---- every hand-made layout: its steps and final button; sections for the accessibility field check
  out.layouts = {};
  for (const [name] of Object.entries(JSON.parse(layouts))) {
    await tool(p, name);
    out.layouts[name] = { steps: await p.$$eval('.stepper li', (l) => l.length), button: (await p.textContent('main form button[type=submit]')).trim() };
  }
  await tool(p, 'accessibility_field_check');
  out.sections = await p.$$eval('details.form-sec', (d) => d.map((x) => [x.open, x.querySelector('.count').textContent]));
  await p.screenshot({ path: path.join(shots, 'ux-tool-access-sections-d.png'), fullPage: true });
  await p.$$eval('details', (ds) => ds.forEach((d) => { d.open = true; }));
  await box(p, 'door_clear_width').fill('813 mm');
  await box(p, 'parking_stall_width').fill('108 in');
  out.accessSent = await sent(p, 'accessibility_field_check', () => p.click('main form button[type=submit]'));
  // show-if: the service-and-repair questions appear only for a service and repair contract, and aren't sent otherwise
  await tool(p, 'validate_home_improvement_contract');
  out.hic = { step1: await p.textContent('.step-title'), hiddenBefore: await p.$eval('.field[data-field=service_charge_usd]', (f) => [f.hidden, f.inert]) };
  await p.click('.field[data-field=is_service_and_repair] label.switch');
  out.hic.shownAfter = await p.$eval('.field[data-field=service_charge_usd]', (f) => f.hidden);
  await p.screenshot({ path: path.join(shots, 'ux-tool-hic-step1-d.png'), fullPage: true });

  // ---- long forms without a layout: one closed "More details"
  await tool(p, 'adu_rules');
  out.more = await p.$$eval('details.more-details', (d) => d.map((x) => [x.open, x.querySelector('summary').textContent]));

  // ---- date and date-time boxes
  out.kinds = {};
  for (const [name, fields] of Object.entries({ ...JSON.parse(dates), ...JSON.parse(datetimes) })) {
    await tool(p, name);
    for (const f of fields) out.kinds[`${name}.${f}`] = await p.$eval(`.field[data-field=${f}] input`, (i) => i.type).catch(() => 'missing');
  }
  // ---- one box for a number or words with a unit: the typed text goes as typed, a plain number as a number
  await tool(p, 'overhead_line_clearance');
  await box(p, 'voltage_kv').fill('12,470 V');
  await box(p, 'equipment').fill('crane');
  out.voltage = await sent(p, 'overhead_line_clearance', () => p.click('main form button[type=submit]'));
  await tool(p, 'dust_control_requirements');
  await p.$$eval('details', (ds) => ds.forEach((d) => { d.open = true; }));
  await box(p, 'air_district').fill('South Coast');
  await box(p, 'disturbed_acres').fill('8,000 sq ft');
  out.acres = await sent(p, 'dust_control_requirements', () => p.click('main form button[type=submit]'));
  await box(p, 'disturbed_acres').fill('2.5');
  out.acresNum = await sent(p, 'dust_control_requirements', () => p.click('main form button[type=submit]'));
  await tool(p, 'path_of_travel_obligation');
  await box(p, 'construction_cost_usd').fill('$1,250');
  out.money = await sent(p, 'path_of_travel_obligation', () => p.click('main form button[type=submit]'));

  // ---- drafts
  const dkey = `tc.draft.${me.tenant.id}.${me.user.id}.tool:calculate_lien_deadline`;
  await tool(p, 'calculate_lien_deadline');
  if (await p.$('.draft-offer')) await p.click('.draft-offer button:has-text("Start over")');     // the one typed above
  await box(p, 'last_furnishing_date').fill('2026-11-21');
  await p.waitForTimeout(1300);
  let s = await store(p);
  out.draft = { kept: !!s[dkey], indexed: !!JSON.parse(s['tc.index'] || '{}')[dkey], line: (await p.textContent('.draft-line').catch(() => '')).trim() };
  await p.reload();
  await p.waitForSelector('.draft-offer');
  out.draft.offer = await p.textContent('.draft-offer');
  await p.screenshot({ path: path.join(shots, 'ux-draft-restored-d.png'), fullPage: true });
  await p.click('.draft-offer button:has-text("Continue")');
  out.draft.restored = await box(p, 'last_furnishing_date').inputValue();
  // the session ends (signed out elsewhere): after signing back in, the same form offers the draft
  await call(p, '/api/auth/logout', { method: 'POST' });
  await p.goto(base + '/#/projects');                      // the next request gets a 401: signed out, not logged out
  await p.waitForSelector('input[name=email]');
  out.draft.after401 = !!(await store(p))[dkey];
  await p.fill('input[name=email]', 'forms@example.test');
  await p.fill('input[name=password]', 'correct-horse-battery');
  await p.click('main form button[type=submit]');
  await p.waitForFunction(() => !location.hash.startsWith('#/login') && document.querySelector('main h1'));
  await tool(p, 'calculate_lien_deadline');
  await p.waitForSelector('.draft-offer', { timeout: 5000 }).catch(() => {});
  out.draft.offeredAgain = await p.$$eval('.draft-offer', (d) => d.length);
  // another version of the check: the draft is dropped
  await p.evaluate((k) => { const d = JSON.parse(localStorage.getItem(k)); d.form = 'older'; localStorage.setItem(k, JSON.stringify(d)); }, dkey);
  await tool(p, 'calculate_lien_deadline');
  out.draft.otherVersion = { offer: await p.$$eval('.draft-offer', (d) => d.length), kept: !!(await store(p))[dkey] };
  // never for the injury check, nor for an incident report
  await tool(p, 'injury_reporting_deadlines');
  await p.locator('main form input[type=datetime-local]').first().fill('2026-11-20T14:30');
  await p.waitForTimeout(1300);
  await p.goto(base + '/#/field?kind=incident');
  await p.waitForSelector('textarea[name=notes]');
  await p.fill('textarea[name=notes]', 'Fell from a ladder');
  await p.waitForTimeout(1300);
  out.draft.never = (await draftKeys(p)).filter((k) => /injury|field:incident/.test(k));
  await p.selectOption('select[name=kind]', 'toolbox_talk');
  await p.fill('input[name=topic]', 'Ladder safety');
  await p.waitForTimeout(1300);
  out.draft.toolbox = (await draftKeys(p)).filter((k) => k.endsWith('field:toolbox_talk')).length;
  // caps: 10 a person, 200,000 characters on the device, the oldest first
  await p.evaluate(([t, u]) => {
    const ix = JSON.parse(localStorage.getItem('tc.index') || '{}');
    const put = (k, who, saved, size) => { localStorage.setItem(k, JSON.stringify({ v: 1, saved, form: 1, values: { notes: 'x'.repeat(size) } })); ix[k] = who; };
    for (let i = 0; i < 11; i++) put(`tc.draft.${t}.${u}.doc:${100 + i}`, { t, u }, `2026-11-0${(i % 9) + 1}T0${i % 10}:00:00Z`, 100);
    for (let i = 0; i < 4; i++) put(`tc.draft.999.888.doc:${i}`, { t: 999, u: 888 }, `2026-10-0${i + 1}T00:00:00Z`, 60000);
    localStorage.setItem('tc.index', JSON.stringify(ix));
  }, [me.tenant.id, me.user.id]);
  await tool(p, 'calculate_lien_deadline');
  await box(p, 'last_furnishing_date').fill('2026-11-22');
  await p.waitForTimeout(1300);
  s = await store(p);
  const drafts = Object.entries(s).filter(([k]) => k.startsWith('tc.draft.'));
  out.caps = { mine: drafts.filter(([k]) => k.startsWith(`tc.draft.${me.tenant.id}.${me.user.id}.`)).length, total: drafts.reduce((n, [, v]) => n + v.length, 0),
    other: drafts.filter(([k]) => k.startsWith('tc.draft.999.')).map(([k]) => k).sort(), lien: !!s[dkey], oldestMine: !!s[`tc.draft.${me.tenant.id}.${me.user.id}.doc:100`] };
  // a full storage: the oldest draft goes and the save is tried once more; still full: say so once, stop
  await p.evaluate(() => { const orig = window.__set = Storage.prototype.setItem; let left = 1; Storage.prototype.setItem = function (k, v) { if (k.startsWith('tc.draft.') && left-- > 0) throw new DOMException('full', 'QuotaExceededError'); return orig.call(this, k, v); }; });
  const n0 = (await draftKeys(p)).length;
  await box(p, 'last_furnishing_date').fill('2026-11-23');
  await p.waitForTimeout(1300);
  out.full = { before: n0, after: (await draftKeys(p)).length, kept: JSON.parse((await store(p))[dkey] || '{}').values };
  await p.evaluate(() => { Storage.prototype.setItem = function (k, v) { if (k.startsWith('tc.draft.')) throw new DOMException('full', 'QuotaExceededError'); return window.__set.call(this, k, v); }; });
  await box(p, 'last_furnishing_date').fill('2026-11-24');
  await p.waitForTimeout(1300);
  out.full.message = await p.textContent('.draft-slot');
  await p.reload();
  await p.waitForSelector('main form');
  // explicit Log out: this person's drafts go; another person on this browser never sees them
  await tool(p, 'calculate_lien_deadline');
  if (await p.$('.draft-offer')) await p.click('.draft-offer button:has-text("Continue")');
  await box(p, 'last_furnishing_date').fill('2026-11-25');
  await p.waitForTimeout(1300);
  const inv = (await call(p, '/api/users', { method: 'POST', body: { email: 'office@example.test', role: 'office' } })).data;
  await p.click('#account-open');
  await p.click('#account-menu .menu-logout');
  await p.waitForSelector('input[name=email]');
  out.loggedOut = (await draftKeys(p)).filter((k) => k.startsWith(`tc.draft.${me.tenant.id}.${me.user.id}.`));
  // ... and one kept for this person isn't offered to another
  await p.evaluate(([k]) => { localStorage.setItem(k, JSON.stringify({ v: 1, saved: new Date().toISOString(), form: 'x', values: {} })); }, [dkey]);
  out.join = (await call(p, '/api/auth/invite/accept', { method: 'POST', body: { token: inv.invite_path.split('/').pop(), password: 'office-horse-battery' } })).status;
  await tool(p, 'calculate_lien_deadline');
  out.otherPerson = await p.$$eval('.draft-offer', (d) => d.length);
  await ctx.close();

  // ---- phone: the stepped form and its action bar; after Run the answers fold into one line
  const mc = await browser.newContext({ deviceScaleFactor: 1, viewport: { width: 375, height: 812 }, serviceWorkers: 'block' });
  const mp = await mc.newPage();
  watch(mp, 'phone');
  await mp.goto(base + '/#/login');
  await call(mp, '/api/auth/signup', { method: 'POST', body: { company_name: 'Phone Forms', email: 'pf@example.test', password: 'correct-horse-battery' } });
  await tool(mp, 'permit_required');
  await mp.screenshot({ path: path.join(shots, 'ux-tool-permit-step1-m.png'), fullPage: true });
  out.phoneBar = await mp.$eval('.action-bar', (b) => getComputedStyle(b).position);
  await tool(mp, 'construction_hours');
  await box(mp, 'city').fill('San Diego');
  await mp.click('.field[data-field=when] .vchips button:has-text("Now")');
  await mp.click('main form button[type=submit]');
  await mp.waitForSelector('.result-headline');
  await mp.waitForTimeout(300);
  out.phoneAfterRun = { form: await mp.$eval('main form.tool-form', (f) => [...f.querySelectorAll('.field[data-field]')].every((x) => x.offsetParent === null)), summary: await mp.textContent('.form-summary'),
    run: await mp.isVisible('main form button[type=submit]'),
    focus: await mp.evaluate(() => document.activeElement.className) };
  await mc.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_check_forms_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "forms.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots, json.dumps(DATES), json.dumps(DATETIMES), json.dumps(LAYOUTS)],
                       env=env, capture_output=True, text=True, timeout=500)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["errors"] == [] and out["signup"] == 200
    # "For job": outside the form, empty until a job is picked
    assert out["forJob"] == {"value": "", "label": "For job", "inForm": False, "first": "Pick a job", "tags": 0}
    f = out["filled"]
    assert f["vals"]["first_furnishing_date"] == "2026-10-15" and "first_furnishing_date" not in f["tags"]     # typed: kept
    assert f["vals"]["last_furnishing_date"] == "2026-11-20" and f["vals"]["claimant_role"] == "subcontractor"
    assert set(f["tags"]) == {"claimant_role", "last_furnishing_date"}
    assert f["line"].startswith("Filled 2 of 6 from Elm St ADU") and "Undo" in f["line"] and "Undo" in f["toast"]
    u = out["undone"]
    assert u["vals"]["last_furnishing_date"] == "" and u["vals"]["claimant_role"] == "" and u["vals"]["first_furnishing_date"] == "2026-10-15"
    assert u["tags"] == 0 and u["job"] == ""
    assert out["toastUndo"] == u["vals"]
    assert out["enter"] == {"ran": 0, "field": "completion_date"}
    # steps
    st = out["steps"]
    assert st["title"] == "Step 1 of 6 · The work" and st["stepper"] == 6
    assert st["blocked"]["title"] == st["title"] and any("Work description" in x for x in st["blocked"]["summary"])
    assert st["two"]["title"].startswith("Step 2 of 6") and set(st["two"]["shown"]) == {"fence_height_ft", "is_pool_barrier"}
    rv = out["review"]
    assert rv["title"] == "Step 6 of 6 · Review your answers" and rv["run"] == "Check if a permit is needed"
    assert rv["hint"] == "Runs the check. Nothing is filed or sent."
    rows = dict(rv["rows"])
    assert rows["Fence height (ft)"] == "6" and "Retaining wall height (ft)" not in rows and "Deck attached or serves exit door" not in rows
    assert rows["Stories"] == "1"
    assert any(v.startswith("Not provided — the check uses ") for v in rows.values())
    assert rv["change"] == {"title": "Step 2 of 6 · Details for this kind of work", "focus": "fence_height_ft"}
    sent = out["permitSent"]
    assert sent["structure_type"] == "fence" and sent["fence_height_ft"] == 6 and "retaining_wall_height_ft" not in sent and "deck_attached_or_serves_exit_door" not in sent
    for name, button in LAYOUTS.items():
        assert out["layouts"][name]["button"] == button and out["layouts"][name]["steps"] >= 3, name
    assert out["sections"][0][0] is True and all(not s[0] for s in out["sections"][1:]) and out["sections"][0][1].endswith("filled")
    assert out["accessSent"]["door_clear_width"] == "813 mm" and out["accessSent"]["parking_stall_width"] == "108 in"
    assert out["hic"]["hiddenBefore"] == [True, True] and out["hic"]["shownAfter"] is False
    assert len(out["more"]) == 1 and out["more"][0][0] is False and out["more"][0][1].startswith("More details (fill in what applies)")
    for name, fields in DATES.items():
        for fld in fields:
            assert out["kinds"][f"{name}.{fld}"] == "date", (name, fld)
    for name, fields in DATETIMES.items():
        for fld in fields:
            assert out["kinds"][f"{name}.{fld}"] == "datetime-local", (name, fld)
    assert out["voltage"]["voltage_kv"] == "12,470 V"
    assert out["acres"]["disturbed_acres"] == "8,000 sq ft" and out["acresNum"]["disturbed_acres"] == 2.5
    assert out["money"]["construction_cost_usd"] == 1250
    # drafts
    d = out["draft"]
    assert d["kept"] and d["indexed"] and d["line"].startswith("Draft kept on this device")
    assert d["offer"].startswith("You have a draft from") and d["restored"] == "2026-11-21"
    assert d["after401"] and d["offeredAgain"] == 1
    assert d["otherVersion"] == {"offer": 0, "kept": False}
    assert d["never"] == [] and d["toolbox"] == 1
    c = out["caps"]
    assert c["mine"] <= 10 and c["total"] <= 200000 and c["lien"] and not c["oldestMine"]
    assert c["other"] == ["tc.draft.999.888.doc:1", "tc.draft.999.888.doc:2", "tc.draft.999.888.doc:3"]   # the oldest went first
    assert out["full"]["after"] == out["full"]["before"] - 1 and out["full"]["kept"]["fields"]["last_furnishing_date"] == "2026-11-23"
    assert "Couldn't keep a draft on this device" in out["full"]["message"]
    assert out["loggedOut"] == [] and out["join"] == 200 and out["otherPerson"] == 0
    # phone
    assert out["phoneBar"] == "sticky"
    assert out["phoneAfterRun"]["form"] is True and "answers · Edit" in out["phoneAfterRun"]["summary"] and out["phoneAfterRun"]["run"]
    assert out["phoneAfterRun"]["focus"] == "result-headline"
