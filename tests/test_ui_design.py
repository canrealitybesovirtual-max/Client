"""The web app's design system (app/static, spec 2): colour tokens with their contrast recomputed from styles.css for
light, dark and extra contrast; no colour outside the token blocks (except the signature canvas and print); no text
under 14 px (the phone tab labels are 13 px); every icon the script names has a symbol in the sprite; the Lucide
license ships; size budgets. The browser test (Node + Playwright + Chromium, skipped otherwise) checks every main page
at 320/375/1280 px in light and dark (the device's setting and the Settings > Display override): no sideways
scrolling, no text under 14 px, tap targets of 44 px on phones, reduced motion, extra contrast and forced colours.
Set TC_UI_SHOTS=<directory> to keep the screenshots (design-*.png)."""
import gzip
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


CSS, JS, INDEX = _read("styles.css"), _read("app.js"), _read("index.html")


def _block(start: str) -> str:
    i = CSS.index(start)
    return CSS[i:CSS.index("}", i)]


def _tokens(start: str) -> dict:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", _block(start)))


LIGHT = _tokens(":root {")
DARK = {**LIGHT, **_tokens(':root[data-theme="dark"] {')}


def _lum(h: str) -> float:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


SURFACES = ["surface", "bg", "surface-2", "surface-3", "accent-soft", "neutral-bg", "info-bg"]
# (foreground, background, minimum): spec 2.1. Text 7:1, muted text 5.8:1 everywhere, links and the info badge stay
# above AA (4.5:1) on tinted surfaces, lines and focus rings at least 3:1.
PAIRS = ([("ink", s, 7.0) for s in SURFACES] + [("ink-2", s, 7.0) for s in SURFACES] + [("muted", s, 5.8) for s in SURFACES]
         + [("link", "surface", 7.0), ("link", "accent-soft", 6.0), ("link", "info-bg", 6.0),
            ("on-primary", "primary", 7.0), ("on-primary", "primary-hover", 7.0), ("topbar-ink", "topbar", 7.0), ("brand-accent", "topbar", 4.5),
            ("red", "red-bg", 7.8), ("amber", "amber-bg", 7.8), ("green", "green-bg", 7.8), ("neutral", "neutral-bg", 7.0), ("info", "info-bg", 6.0),
            ("toast-ink", "toast-bg", 7.0), ("red-line", "surface", 3.0), ("amber-line", "surface", 3.0), ("green-line", "surface", 3.0),
            ("field-border", "surface", 4.0), ("field-border", "surface-2", 3.0), ("focus-outer", "focus-inner", 3.0),
            ("focus-outer", "surface", 3.0), ("focus-outer", "bg", 3.0), ("primary", "surface", 3.0)])


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_token_contrast(theme):
    t = LIGHT if theme == "light" else DARK
    low = [(fg, bg, round(ratio(t[fg], t[bg]), 2), need) for fg, bg, need in PAIRS if ratio(t[fg], t[bg]) < need]
    assert low == []


def test_dark_tokens_are_the_same_for_the_device_setting_and_the_override():
    media = CSS[CSS.index('@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {'):]
    media = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", media[:media.index("} }")]))
    override = dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", _block(':root[data-theme="dark"] {')))
    assert media == override and len(media) >= 35


def test_extra_contrast_turns_every_line_into_text_colour():
    for start in (':root[data-contrast="more"] {', "@media (prefers-contrast: more) { :root:root {"):
        block = _block(start)
        assert "--field-border: var(--ink-2)" in block and "--line: var(--ink-2)" in block and "--focus-ring: 5px" in block
    assert CSS.index(':root[data-contrast="more"]') > CSS.index('@media (prefers-color-scheme: dark)')    # wins in dark too
    for theme in (LIGHT, DARK):
        assert ratio(theme["ink-2"], theme["surface"]) >= 7 and ratio(theme["ink-2"], theme["surface-2"]) >= 7


def test_no_colour_outside_the_token_blocks_except_the_signature_and_print():
    rest = CSS
    for start in (":root {", ':root[data-theme="dark"] {'):
        rest = rest.replace(_block(start), "")
    media = rest.index('@media (prefers-color-scheme: dark)')
    rest = rest[:media] + rest[rest.index("} }", media) + 3:]
    rest = rest[:rest.index("@media print {")] + rest[rest.index(".print-only { display: none; }"):]
    rest = re.sub(r"/\*.*?\*/", "", rest, flags=re.S)
    sig = re.search(r"\.sig-pad \{[^}]*\}", rest).group(0)
    assert "#ffffff" in sig                                   # white in both themes, on purpose (commented)
    rest = rest.replace(sig, "")
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", rest) == []


def test_no_text_under_14px_except_the_phone_tab_labels():
    sizes = re.findall(r"([^{}]+)\{[^{}]*?font-size:\s*([^;}\s]+)", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S))
    for selector, size in sizes:
        selector = selector.strip()
        if size.startswith("var(--fs-") or size in ("inherit", "16px"):
            continue
        m = re.fullmatch(r"(\d*\.?\d+)rem", size)
        assert m, (selector, size)
        rem = float(m.group(1))
        if selector.endswith(".nav a"):
            assert rem in (0.8125, 1.0)                     # 13 px tabs on phones, 16 px in the sidebar
        else:
            assert rem >= 0.875, (selector, size)


def test_every_icon_the_script_names_has_a_symbol():
    symbols = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', INDEX))
    used = set(re.findall(r"icon\('([a-z0-9-]+)'", JS)) | set(re.findall(r"\bicon: '([a-z0-9-]+)'", JS))
    used |= set(re.findall(r'<use href="#i-([a-z0-9-]+)"', INDEX))
    for table in ("RAG_ICON", "BADGE_ICON", "GROUP_ICON"):
        body = re.search(table + r" = \{([^}]*)\}", JS).group(1)
        used |= set(re.findall(r":\s*'([a-z0-9-]+)'", body))
    assert used and used <= symbols, sorted(used - symbols)
    # the Lucide set (spec 2.11) plus the app's own logo; the old hand-drawn ids are gone
    assert len(symbols) == 69 and "logo" in symbols and {"plug", "find", "check", "cloud-off"} <= symbols
    assert not {"home", "tools", "field", "ask", "alert", "stop"} & symbols
    assert "lucide-static 1.71.0" in INDEX and "ICONS-LICENSE.txt" in INDEX


def test_theme_colour_follows_the_top_bar():
    assert '<meta name="theme-color" content="#0f3d6e" media="(prefers-color-scheme: light)">' in INDEX
    assert '<meta name="theme-color" content="#0b2440" media="(prefers-color-scheme: dark)">' in INDEX
    m = re.search(r"const THEME_TOPBAR = \{ light: '(#[0-9a-f]{6})', dark: '(#[0-9a-f]{6})' \}", JS)
    assert m and m.group(1) == LIGHT["topbar"] and m.group(2) == DARK["topbar"]


def test_display_keys_are_device_settings_written_only_when_changed():
    sec = JS[JS.index("// ======================================================================== display"):
             JS.index("// ======================================================================== combobox and chips")]
    assert "local.set('tc.display', v)" in sec and "local.del('tc.display')" in sec
    assert "local.set('tc.contrast', 'more')" in sec and "local.del('tc.contrast')" in sec
    assert "stored(" not in sec                              # not company data: never in tc.index, kept on sign-out
    assert "key === '/'" not in JS and "e.key === '/'" not in JS     # no single-character shortcuts (WCAG 2.1.4)


@pytest.fixture
def client(tmp_path):
    from app.config import Settings
    from app.main import create_app
    app = create_app(Settings.from_env(data_dir=str(tmp_path), database_url=None, events_background=False))
    with TestClient(app) as c:
        yield c
    app.state.platform.store.close()


def test_icon_license_ships_and_sizes_stay_in_budget(client):
    r = client.get("/static/ICONS-LICENSE.txt")
    assert r.status_code == 200 and "ISC License" in r.text and "Cole Bemis" in r.text and "i-plug = plug" in r.text
    for name, raw_kb, gz_kb in (("app.js", 390, 110), ("styles.css", 64, 15), ("index.html", 32, 9)):
        data = _read(name).encode()
        assert len(data) <= raw_kb * 1000, (name, len(data))
        assert len(gzip.compress(data, 6)) <= gz_kb * 1000, (name, len(gzip.compress(data, 6)))


# ---------------------------------------------------------------------------- browser test (Playwright)
SMOKE = r"""
const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = { errors: [], pages: {} };
  const watch = (p, tag) => { p.on('pageerror', (e) => out.errors.push(`${tag}: ${e}`)); p.on('dialog', (d) => { out.errors.push(`${tag}: native dialog`); d.dismiss(); }); };
  const call = (p, url, o = {}) => p.evaluate(async ([url, o]) => {
    const m = document.cookie.match(/tc_csrf=([^;]+)/);
    const r = await fetch(url, { method: o.method || 'GET', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' }, body: o.body ? JSON.stringify(o.body) : undefined });
    let data = null; try { data = await r.json(); } catch (e) { /* */ }
    return { status: r.status, data };
  }, [url, o]);
  const audit = (p) => p.evaluate(() => {
    const r = { overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth, small: [], targets: [] };
    for (const el of document.querySelectorAll('body *')) {
      if (!el.getClientRects().length || el.closest('.visually-hidden')) continue;
      if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
      const fs = parseFloat(getComputedStyle(el).fontSize);
      if (fs < (el.closest('#nav') ? 13 : 14) - 0.01) r.small.push(`${el.tagName} ${fs}px ${el.textContent.trim().slice(0, 30)}`);
    }
    if (innerWidth < 900) {
      for (const el of document.querySelectorAll('main button, main a.btn, main summary, main .chip, main a.list-row, main a.task-tile, main select, main input:not([type=checkbox]):not([type=radio]):not([type=file]), #nav a.in-bar, .topbar button:not([hidden]), .topbar a')) {
        if (!el.getClientRects().length) continue;
        const b = el.getBoundingClientRect();
        if (b.height < 43.5) r.targets.push(`${el.tagName}.${el.className} ${Math.round(b.height)} ${el.textContent.trim().slice(0, 30)}`);
      }
    }
    return r;
  });
  const owner = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
  const op = await owner.newPage();
  watch(op, 'setup');
  await op.goto(base + '/#/login');
  out.signup = (await call(op, '/api/auth/signup', { method: 'POST', body: { company_name: 'Design Builders', name: 'Dee Signer', email: 'design@example.test', password: 'correct-horse-battery', cslb_license: '123456' } })).status;
  const proj = (await call(op, '/api/projects', { method: 'POST', body: { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', first_furnishing_date: '2026-11-15', last_furnishing_date: '2026-11-25' } })).data;
  await call(op, '/api/subs', { method: 'POST', body: { name: 'Ace Roofing', license_number: '123456', scope: 'Roofing', project_id: proj.id } });
  const state = await owner.storageState();
  await owner.close();
  const PAGES = ['#/dashboard', '#/projects', `#/project/${proj.id}`, '#/subs', '#/tools', '#/tools/calculate_lien_deadline', '#/professionals', '#/documents', '#/field', '#/integrations', '#/settings', '#/more'];
  for (const [w, h] of [[1280, 800], [375, 812], [320, 640]]) {
    for (const scheme of ['light', 'dark']) {
      if (w === 320 && scheme === 'dark') continue;
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1, serviceWorkers: 'block', colorScheme: scheme, storageState: state });
      const p = await ctx.newPage();
      watch(p, `${w}-${scheme}`);
      for (const hash of PAGES) {
        await p.goto(base + '/' + hash);
        await p.waitForSelector('main h1');
        await p.waitForTimeout(hash === '#/integrations' ? 700 : 300);
        const r = await audit(p);
        r.bg = await p.evaluate(() => getComputedStyle(document.body).backgroundColor);
        out.pages[`${w}-${scheme}-${hash}`] = r;
        if (hash === '#/dashboard' || hash === '#/tools') await p.screenshot({ path: path.join(shots, `design-${hash.slice(2)}-${w}-${scheme}.png`) });
      }
      await ctx.close();
    }
  }
  // Settings > Display: the override, the theme colour, extra contrast; a reload keeps them; Match this device removes them
  const dctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block', colorScheme: 'light', storageState: state });
  const dp = await dctx.newPage();
  watch(dp, 'display');
  await dp.goto(base + '/#/settings?section=display');
  await dp.waitForSelector('#display-h');
  out.displayFocus = await dp.evaluate(() => document.activeElement && document.activeElement.id);
  await dp.click('#set-display button[role=radio][data-value=dark]');
  await dp.click('#set-display .switch');
  const look = () => dp.evaluate(() => ({ theme: document.documentElement.dataset.theme || null, contrast: document.documentElement.dataset.contrast || null,
    bg: getComputedStyle(document.body).backgroundColor, metas: [...document.querySelectorAll('meta[name=theme-color]')].map((m) => m.content),
    stored: Object.fromEntries(Object.entries(localStorage).filter(([k]) => k === 'tc.display' || k === 'tc.contrast')),
    checked: [...document.querySelectorAll('#set-display [role=radio]')].map((b) => b.getAttribute('aria-checked')),
    field: getComputedStyle(document.querySelector('main input[type=text], main input[type=password]')).borderTopColor,
    ink2: getComputedStyle(document.documentElement).getPropertyValue('--ink-2').trim() }));
  out.dark = await look();
  await dp.reload();
  await dp.waitForSelector('#display-h');
  out.darkAfterReload = await look();
  await dp.focus('#set-display button[role=radio][aria-checked=true]');
  await dp.keyboard.press('ArrowRight');                 // radio chips: arrows move and pick (wraps to "Match this device")
  out.afterArrow = await look();
  await dp.click('#set-display .switch');
  out.auto = await look();
  await dp.screenshot({ path: path.join(shots, 'design-settings-display.png') });
  // reduced motion: no transitions or animations
  await dp.emulateMedia({ reducedMotion: 'reduce' });
  out.motion = await dp.evaluate(() => [...document.querySelectorAll('.btn, .switch input, .chip, .sk-bar')].slice(0, 20).map((e) => parseFloat(getComputedStyle(e).transitionDuration) || 0));
  // forced colours: the focused button keeps a visible outline
  await dp.emulateMedia({ forcedColors: 'active', reducedMotion: 'no-preference' });
  await dp.focus('#set-display button[role=radio]');
  await dp.keyboard.press('Tab');
  await dp.keyboard.press('Shift+Tab');
  out.forced = await dp.evaluate(() => { const s = getComputedStyle(document.activeElement); return { style: s.outlineStyle, width: parseFloat(s.outlineWidth) }; });
  await dp.screenshot({ path: path.join(shots, 'design-forced-colors.png') });
  await dctx.close();
  process.stdout.write(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_design_in_a_real_browser(tmp_path, playwright_env, live_server):
    node, env = playwright_env
    shots = os.environ.get("TC_UI_SHOTS") or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    script = tmp_path / "design.js"
    script.write_text(SMOKE)
    r = subprocess.run([node, str(script), live_server, shots], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout)
    assert out["signup"] == 200 and out["errors"] == []
    bad = {k: v for k, v in out["pages"].items() if v["overflow"] or v["small"] or v["targets"]}
    assert bad == {}
    dark_bg = {v["bg"] for k, v in out["pages"].items() if "-dark-" in k}
    light_bg = {v["bg"] for k, v in out["pages"].items() if "-light-" in k}
    assert dark_bg == {"rgb(14, 20, 27)"} and light_bg == {"rgb(244, 246, 249)"}           # --bg, from the device setting
    assert out["displayFocus"] == "display-h"                                              # ?section= opens at its heading
    d = out["dark"]
    assert d["theme"] == "dark" and d["contrast"] == "more" and d["bg"] == "rgb(14, 20, 27)"
    assert d["metas"] == ["#0b2440", "#0b2440"] and d["stored"] == {"tc.display": "dark", "tc.contrast": "more"}
    assert d["checked"] == ["false", "false", "true"] and d["field"] == "rgb(194, 203, 213)" and d["ink2"] == "#c2cbd5"
    assert out["darkAfterReload"] == d
    a = out["afterArrow"]
    assert a["theme"] is None and a["checked"] == ["true", "false", "false"] and a["stored"] == {"tc.contrast": "more"}
    assert a["metas"] == ["#0f3d6e", "#0b2440"] and a["bg"] == "rgb(244, 246, 249)"
    assert out["auto"]["contrast"] is None and out["auto"]["stored"] == {}
    assert out["motion"] and max(out["motion"]) < 0.01
    assert out["forced"]["style"] == "solid" and out["forced"]["width"] >= 2
