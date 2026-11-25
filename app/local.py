"""Offline / local mode: TradeComply for one company on one laptop, no internet needed.

    python -m app.local                 (or double-click TradeComply-local.command / .bat / tradecomply-local.sh)

- Data lives in ~/.tradecomply (or --data-dir / DATA_DIR): the app database, the signing key and CSLB license data.
- Binds 127.0.0.1 on a free port (or --port / PORT), so nothing is reachable from the network.
- First run: builds CSLB license data from the bundled snapshot in seed/, creates a local owner account, prints the
  login (also saved to LOCAL-LOGIN.txt in the data folder) and opens the browser already logged in.
- AI agents on the same computer: MCP at http://127.0.0.1:<port>/mcp, or stdio with `python server.py`.
- --update-licenses refreshes CSLB data from cslb.ca.gov in the background when the laptop is online.
"""
import argparse
import os
import secrets
import socket
import sys
import threading
import webbrowser

DEFAULT_DIR = "~/.tradecomply"
LOCAL_EMAIL = "owner@tradecomply.local"


def free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port on 127.0.0.1")


def prepare_env(data_dir: str) -> str:
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    os.environ["DATA_DIR"] = data_dir
    os.environ.setdefault("LICENSE_DB", os.path.join(data_dir, "cslb.sqlite"))
    os.environ["TC_LOCAL"] = "1"
    return data_dir


def ensure_license_data(quiet: bool = False) -> None:
    from tradecomply import cslb
    path = os.environ["LICENSE_DB"]
    if os.path.exists(path):
        return
    if not quiet:
        print("First run: loading the bundled CSLB license snapshot (about 10-30 seconds)...", flush=True)
    try:
        n = cslb.build_from_seed(path)
        if not quiet:
            print(f"  {n:,} California licenses loaded (snapshot in seed/).", flush=True)
    except Exception as e:      # the app still runs on demo data; results say so
        print(f"  Could not load the license snapshot ({e}). License checks will use demo data.", flush=True)


def ensure_owner(platform, company: str | None = None) -> tuple[dict, str | None]:
    """Create the local company and owner on first run. Returns (user, password if just created)."""
    from .auth import hash_password
    store = platform.store
    with store.connect() as con:
        row = con.execute("select * from users where role = 'owner' order by id limit 1").fetchone()
    if row:
        return dict(row), None
    password = secrets.token_urlsafe(12)
    tenant = store.create_tenant(company or "My Company", None, None, status="active", plan="local")
    user = store.create_user(tenant["id"], LOCAL_EMAIL, hash_password(password), "owner", name="Local owner")
    path = os.path.join(store.data_dir, "LOCAL-LOGIN.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"TradeComply local login\nEmail: {LOCAL_EMAIL}\nPassword: {password}\n"
                "Change the password in Settings after your first login, then delete this file.\n")
    return user, password


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.local", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "0") or 0), help="port on 127.0.0.1")
    ap.add_argument("--data-dir", default=os.getenv("DATA_DIR") or DEFAULT_DIR)
    ap.add_argument("--company", default=None, help="company name for the first run")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--update-licenses", action="store_true", help="refresh CSLB data in the background when online")
    args = ap.parse_args(argv)

    data_dir = prepare_env(args.data_dir)
    ensure_license_data()

    import uvicorn
    from app.main import app            # built from the environment set above (local mode, data dir)
    from tradecomply import cslb
    if args.update_licenses:
        cslb.start_auto_refresh(os.environ["LICENSE_DB"])

    platform = app.state.platform
    user, password = ensure_owner(platform, args.company)
    platform.local_user_id = user["id"]
    platform.local_login_token = secrets.token_urlsafe(24)
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    login_link = f"{url}/api/auth/local?token={platform.local_login_token}"

    print("\nTradeComply CA is running on this computer only.")
    print(f"  Web app:   {url}")
    print(f"  Data:      {data_dir}")
    if password:
        print(f"  Login:     {LOCAL_EMAIL} / {password}   (also saved in {os.path.join(data_dir, 'LOCAL-LOGIN.txt')})")
    else:
        print(f"  Login:     {user['email']} (password you set; first-run password is in LOCAL-LOGIN.txt if kept)")
    print(f"  One-time login link: {login_link}")
    print(f"  MCP for AI agents: {url}/mcp   (or stdio: {sys.executable} {os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'server.py')})")
    snap = cslb.meta().get("snapshot_date")
    print(f"  License data: {'CSLB snapshot ' + snap if snap else 'demo fixtures'}")
    print("Press Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [login_link]).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
