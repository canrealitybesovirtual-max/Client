"""Manual bench (spec 7.2): how long Today takes to show on a slow phone network, cold and with the service worker
warm. Not run in CI (network timings are noisy); run it after changing the shell and paste the numbers into the notes.

    python scripts/bench_ui_load.py [runs]

Starts this app with uvicorn on a free port (SQLite in a temporary directory, no CSLB data), signs up a company with
one project, then loads #/dashboard in Chromium with Chrome DevTools' network emulation at Lighthouse's "slow 4G"
mobile profile (150 ms round trip, 1.6 Mbit/s down, 750 kbit/s up) and reports the median of `runs` (default 3):
"content" = the task tiles are on screen, "ready" = the page is no longer busy. Needs Node with Playwright and its
Chromium (as the browser tests do)."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, runs] = [process.argv[2], Number(process.argv[3])];
  const browser = await chromium.launch();
  const setup = await browser.newContext();
  const sp = await setup.newPage();
  await sp.goto(base + '/#/login');
  await sp.evaluate(async () => {
    const csrf = () => decodeURIComponent((document.cookie.match(/tc_csrf=([^;]+)/) || [])[1] || '');
    const post = (u, b) => fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify(b) });
    await post('/api/auth/signup', { company_name: 'Bench Builders', email: 'bench@example.test', password: 'correct-horse-battery' });
    await post('/api/projects', { name: 'Elm St ADU', city: 'San Diego', county: 'San Diego', first_furnishing_date: '2026-11-03', last_furnishing_date: '2026-11-20' });
  });
  const state = await setup.storageState();
  await setup.close();
  const slow = { offline: false, latency: 150, downloadThroughput: 1.6 * 1024 * 1024 / 8, uploadThroughput: 750 * 1024 / 8 };
  const load = async (ctx, p) => {
    const cdp = await ctx.newCDPSession(p);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', slow);
    const t0 = Date.now();
    await p.goto(base + '/#/dashboard');
    await p.waitForSelector('a.task-tile', { timeout: 60000 });
    const content = Date.now() - t0;
    await p.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'), null, { timeout: 60000 });
    return { content, ready: Date.now() - t0 };
  };
  const cold = [], warm = [];
  for (let i = 0; i < runs; i++) {
    const ctx = await browser.newContext({ storageState: state, viewport: { width: 375, height: 812 } });
    const p = await ctx.newPage();
    cold.push(await load(ctx, p));
    await p.evaluate(async () => { await navigator.serviceWorker.ready; });
    await p.waitForTimeout(1500);
    const p2 = await ctx.newPage();
    warm.push(await load(ctx, p2));
    await ctx.close();
  }
  const med = (xs, k) => xs.map((x) => x[k]).sort((a, b) => a - b)[Math.floor(xs.length / 2)];
  process.stdout.write(JSON.stringify({ runs, cold: { content_ms: med(cold, 'content'), ready_ms: med(cold, 'ready') },
    warm: { content_ms: med(warm, 'content'), ready_ms: med(warm, 'ready') }, all: { cold, warm } }));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def main(runs: int = 3) -> None:
    node = shutil.which("node")
    if not node:
        sys.exit("node is not installed")
    env = dict(os.environ)
    npm = shutil.which("npm")
    if npm:
        root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
        env["NODE_PATH"] = os.pathsep.join(filter(None, [root, env.get("NODE_PATH")]))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tempfile.mkdtemp(prefix="tc-bench-")
    srv_env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "APP_URL", "RENDER_EXTERNAL_URL")}
    srv_env.update(DATA_DIR=data, LICENSE_DB="/nonexistent/cslb.sqlite", EVENTS_WORKER="0", MCP_REQUIRE_KEY="0")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=REPO, env=srv_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        from tests.support import server_up
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if server_up(base):
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(SCRIPT)
        r = subprocess.run([node, f.name, base, str(runs)], env=env, capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        if r.returncode:
            sys.exit(r.stderr[-2000:])
        out = json.loads(r.stdout)
        sizes = {n: os.path.getsize(os.path.join(REPO, "app", "static", n)) for n in ("app.js", "styles.css", "index.html")}
        print(json.dumps({"sizes_bytes": sizes, **out}, indent=1))
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
