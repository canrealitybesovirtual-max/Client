"""TradeComply is a product for other companies: nothing about whoever runs a server (their name, email, phone,
accounts, hosting address) may be built into the code, the UI, the catalog or the docs. Every customer signs up and
enters its own company, license, contacts and credentials; the operator's settings come from environment variables.

test_tracked_files_hold_none_of_your_identifiers scans every tracked file for identifiers you list in a file
OUTSIDE the repository (so they are never written into it), one per line, '#' for comments:

    TC_FORBIDDEN_STRINGS_FILE=/path/outside/the/repo/identifiers.txt python -m pytest -q tests/test_no_operator_data.py

Use specific values (email addresses, phone numbers, account ids, host names, usernames); entries shorter than 6
characters are ignored because they also occur in public data. Failures name the file and the entry's line number in
your list, never the value. Without the variable that test is skipped; the other tests always run.
"""
import os
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.support import make_app, signup

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_LEN = 6

# Files a customer or operator sees: code, web app, catalog, deploy config, docs, launchers.
PRODUCT = re.compile(r"^(app/.*\.(py|js|html|css|webmanifest|svg)|server\.py|render\.yaml|requirements\.txt|README\.md|"
                     r"docs/.*\.md|scripts/.*\.py|tradecomply/integrations_guide\.py|tradecomply/data/integrations\.json|"
                     r"TradeComply-local\.(bat|command)|tradecomply-local\.sh)$")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
RESERVED_DOMAIN = re.compile(r"(^|\.)(example\.(com|org|net)|example|test|invalid|local|localhost)$", re.I)
PUBLIC_AGENCY = re.compile(r"(^|\.)(gov|mil)$", re.I)
PHONES = [re.compile(x) for x in (                       # (area, exchange, line) in every common US spelling
    r"(?<![\w+])\+1[ .-]?\(?([2-9]\d{2})\)?[ .-]?(\d{3})[ .-]?(\d{4})(?!\d)",       # +14155550100, +1 415-555-0100
    r"(?<![\w.\-])\(([2-9]\d{2})\) ?(\d{3})[-.](\d{4})(?![\w.\-])",                # (415) 555-0100
    r"(?<![\w.\-])([2-9]\d{2})-(\d{3})-(\d{4})(?![\w.\-])",                         # 415-555-0100
    r"(?<![\w.\-])([2-9]\d{2})\.(\d{3})\.(\d{4})(?![\w.\-])",                       # 415.555.0100
)]
HOSTED_ADDRESS = re.compile(r"[A-Za-z0-9-]+\.onrender\.com", re.I)


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True, timeout=60).stdout
        return [p for p in out.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        found = []
        for dp, dns, fns in os.walk(REPO):
            dns[:] = [d for d in dns if d not in (".git", "__pycache__", ".venv", ".pytest_cache", "data")]
            found += [os.path.relpath(os.path.join(dp, f), REPO).replace(os.sep, "/") for f in fns]
        return found


def read(path: str) -> bytes:
    try:
        with open(os.path.join(REPO, path), "rb") as f:
            return f.read()
    except OSError:                      # deleted in the working tree but still tracked
        return b""


def product_texts() -> dict[str, str]:
    return {p: read(p).decode("utf-8", "replace") for p in tracked_files() if PRODUCT.match(p)}


def _variants(entry: str) -> set[bytes]:
    out = {entry.lower().encode()}
    digits = re.sub(r"\D", "", entry)
    if len(digits) >= 10 and re.fullmatch(r"[\d\s()+.\-]+", entry):     # a phone number: every common spelling
        d = digits[-10:]
        out |= {x.encode() for x in (d, "1" + d, f"{d[:3]}-{d[3:6]}-{d[6:]}", f"({d[:3]}) {d[3:6]}-{d[6:]}",
                                      f"{d[:3]}.{d[3:6]}.{d[6:]}", f"{d[:3]} {d[3:6]} {d[6:]}")}
    return out


def test_tracked_files_hold_none_of_your_identifiers():
    path = (os.getenv("TC_FORBIDDEN_STRINGS_FILE") or "").strip()
    if not path:
        pytest.skip("set TC_FORBIDDEN_STRINGS_FILE to a file outside the repo listing your own identifiers")
    with open(path, encoding="utf-8") as f:
        entries = [(n, line.strip()) for n, line in enumerate(f, 1)
                   if line.strip() and not line.lstrip().startswith("#") and len(line.strip()) >= MIN_LEN]
    assert entries, "the identifiers file lists nothing usable (entries need at least 6 characters)"
    assert not os.path.abspath(path).startswith(REPO + os.sep), "keep the identifiers file outside the repository"
    needles = [(n, v) for n, entry in entries for v in _variants(entry)]
    hits = set()
    for p in tracked_files():
        data = read(p).lower()
        hits |= {f"{p} (entry on line {n})" for n, v in needles if v in data}
    assert not hits, "your identifiers are in tracked files: " + "; ".join(sorted(hits))


def test_no_hosting_address_is_built_in():
    """The product never names a deployment: links come from APP_URL / RENDER_EXTERNAL_URL or the request."""
    hits = [p for p in tracked_files() if HOSTED_ADDRESS.search(read(p).decode("utf-8", "replace"))]
    assert not hits, f"a *.onrender.com host is hard-coded in {hits}"
    import json
    with open(os.path.join(REPO, "tradecomply", "data", "integrations.json"), encoding="utf-8") as f:
        cat = json.load(f)
    assert not any("://" in str(v) for v in cat["tradecomply"].values()), "the catalog must not hold a server URL"


def test_ui_config_and_docs_hold_no_contact_details():
    """Emails: only reserved example domains (and public agencies cited as rule sources). Phone numbers: only the
    fictional 555-0100..0199 range. No built-in 'Contact TradeComply': the operator's contact is SUPPORT_EMAIL /
    SUPPORT_URL."""
    texts = product_texts()
    assert len(texts) > 30 and "app/static/app.js" in texts and "render.yaml" in texts
    bad = []
    for p, text in texts.items():
        for m in EMAIL.finditer(text):
            if not (RESERVED_DOMAIN.search(m.group(1)) or PUBLIC_AGENCY.search(m.group(1))):
                bad.append(f"{p}: email on {m.group(1)}")
        for m in (m for rx in PHONES for m in rx.finditer(text)):
            if not (m.group(2) == "555" and m.group(3).startswith("01")):
                bad.append(f"{p}: phone number ending {m.group(3)[-2:]}")
        if re.search(r"contact tradecomply", text, re.I):
            bad.append(f"{p}: 'Contact TradeComply' (use the operator's SUPPORT_EMAIL / SUPPORT_URL)")
    assert not bad, bad
    js = texts["app/static/app.js"]
    assert not re.search(r"mailto:[A-Za-z0-9]", js), "the web app must not hard-code a mailto address"


def test_operator_settings_are_declared_without_values():
    with open(os.path.join(REPO, "render.yaml"), encoding="utf-8") as f:
        text = f.read()
    for key in ("APP_URL", "SUPPORT_EMAIL", "SUPPORT_URL", "DATABASE_URL", "STRIPE_SECRET_KEY"):
        m = re.search(rf"- key: {key}\n(\s+)(\w+): ?(.*)", text)
        assert m and m.group(2) == "sync" and m.group(3).split("#")[0].strip() == "false", key
    with open(os.path.join(REPO, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    assert "`SUPPORT_EMAIL`, `SUPPORT_URL`" in readme and "`APP_URL`" in readme


def test_support_contact_comes_only_from_the_environment(tmp_path, monkeypatch):
    for name in ("SUPPORT_EMAIL", "SUPPORT_URL", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env().support_contact is None
    with TestClient(make_app(tmp_path / "a")) as c:
        assert c.get("/api/config").json()["support_contact"] is None
        signup(c)
        r = c.post("/api/billing/checkout")
        assert r.status_code == 503 and r.json()["detail"] == "Online billing is not set up on this server yet."
    monkeypatch.setenv("SUPPORT_EMAIL", "help@tc-operator.example.test")
    monkeypatch.setenv("SUPPORT_URL", "https://tc-operator.example.test/support")
    with TestClient(make_app(tmp_path / "b")) as c:
        assert c.get("/api/config").json()["support_contact"] == {
            "email": "help@tc-operator.example.test", "url": "https://tc-operator.example.test/support"}
    monkeypatch.setenv("SUPPORT_EMAIL", "not an email")
    monkeypatch.setenv("SUPPORT_URL", "javascript:alert(1)")
    assert Settings.from_env().support_contact is None                  # malformed values are never shown


def test_mcp_client_config_points_at_the_server_it_runs_on(tmp_path, monkeypatch):
    """No APP_URL: the web app's tool run and an MCP-over-HTTP call both get this server's own address."""
    for name in ("APP_URL", "RENDER_EXTERNAL_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(make_app(tmp_path)) as c:
        signup(c)
        r = c.post("/api/tools/mcp_client_config", json={"ids": ["box"]})
        assert r.status_code == 200, r.text
        assert r.json()["structured"]["claude_code_mcp_json"]["mcpServers"]["tradecomply"]["url"] == \
            "http://testserver/mcp"
        h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/mcp", json=init, headers=h)
        assert r.status_code == 200, r.text
        sid = r.headers.get("mcp-session-id")
        if sid:
            h = {**h, "mcp-session-id": sid, "mcp-protocol-version": "2025-06-18"}
        c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
        r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "mcp_client_config", "arguments": {"ids": ["box"]}}})
        assert r.status_code == 200, r.text
        assert '"url":"http://testserver/mcp"' in r.text.replace(" ", "").replace('\\"', '"'), r.text[:500]
