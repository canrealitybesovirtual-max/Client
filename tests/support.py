"""Test helpers for integrations (connector engineers: build your tests on these).

    from tests.support import backend, make_app, signup, mock_http, json_response   # `backend` is autouse

- backend: runs each test on SQLite, then on PostgreSQL in a fresh schema when TC_TEST_DATABASE_URL is set
  (skipped otherwise), exactly like tests/test_platform.py.
- make_app(tmp_path, **settings): an app on the current backend, with the event worker off (call
  app.state.platform.events.drain() to deliver) unless events_background=True is passed, and the search index not
  warmed at startup (app.search.build_now(platform) builds it; search_warm=True warms it like production).
- browser(c): another person's browser on the app client `c` is running (`with TestClient(app) as c`): its own
  cookies and headers, on c's event loop. Use it for every further client of that app, never a second
  `with TestClient(app)` (app/main.py refuses to start a running app again; see browser()).
- join(c, owner, email, role): the owner invites someone and client `c` joins through the invite link (signed in).
- mock_http(platform, handler, hosts=None): every outbound call of that app goes to handler(request) ->
  httpx.Response (an httpx.MockTransport); DNS answers come from `hosts` ({"name": "ip" or [ips]}, default: a public
  address for every name), so the SSRF checks still run. Returns the list of requests seen.
- Responses must match the vendor's documented shapes (status, headers, JSON fields): copy them from the docs.
"""
import json
import os
import secrets
import tempfile
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="tc-test-"))
os.environ.pop("DATABASE_URL", None)       # never a real database: Postgres only through TC_TEST_DATABASE_URL
PG_TEST_URL = (os.getenv("TC_TEST_DATABASE_URL") or "").strip() or None
PUBLIC_TEST_IP = "8.8.8.8"                 # any globally routable address; MockTransport never connects to it
PW = "correct-horse-battery"

_db = {"backend": "sqlite", "url": None, "apps": [], "stores": []}


def with_search_path(url: str, schema: str) -> str:
    return url + ("&" if "?" in url else "?") + "options=" + quote(f"-csearch_path={schema}", safe="")


@pytest.fixture(params=["sqlite", "postgres"], autouse=True)
def backend(request):
    schema = None
    if request.param == "postgres":
        if not PG_TEST_URL:
            pytest.skip("set TC_TEST_DATABASE_URL to run the PostgreSQL variant")
        import psycopg
        schema = "tc_i_" + secrets.token_hex(6)
        with psycopg.connect(PG_TEST_URL, autocommit=True) as con:
            con.execute(f'create schema "{schema}"')
        _db.update(backend="postgres", url=with_search_path(PG_TEST_URL, schema), apps=[], stores=[])
    else:
        _db.update(backend="sqlite", url=None, apps=[], stores=[])
    try:
        yield request.param
    finally:
        for a in _db["apps"]:
            a.state.platform.events.stop()
            a.state.platform.store.close()
        for st in _db["stores"]:
            st.close()
        _db.update(backend="sqlite", url=None, apps=[], stores=[])
        if schema:
            import psycopg
            with psycopg.connect(PG_TEST_URL, autocommit=True) as con:
                con.execute(f'drop schema "{schema}" cascade')


def make_app(tmp_path, **overrides):
    from app.config import Settings
    from app.main import create_app
    overrides.setdefault("database_url", _db["url"])
    overrides.setdefault("events_background", False)
    overrides.setdefault("search_warm", False)        # tests build the search index with app.search.build_now
    a = create_app(Settings.from_env(data_dir=str(tmp_path), **overrides))
    _db["apps"].append(a)
    return a


def store_for(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path), database_url=_db["url"])
    _db["stores"].append(st)
    return st


class _Browser(TestClient):
    __test__ = False

    def __init__(self, running: TestClient, **kwargs):
        kwargs.setdefault("base_url", str(running.base_url))
        super().__init__(running.app, **kwargs)
        self.running = running

    @property
    def portal(self):
        """The running client's portal (one thread, one event loop): every request is served there. Never None, so
        Starlette never starts a loop of its own for a request (TestClient._portal_factory)."""
        if self.running.portal is None:
            raise RuntimeError("browser(c) needs `c` running: use it inside `with TestClient(app) as c`")
        return self.running.portal

    def __enter__(self):
        assert self.portal is not None     # raises here, not on the first request, if `c` is not running
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def browser(c: TestClient, **kwargs) -> TestClient:
    """Another browser (its own cookie jar and headers) on the app client `c` is running, with c's base_url unless
    one is given. Use it as `with browser(owner) as other:` or plainly (`anon = browser(owner)`).

    Why not a second `with TestClient(app)`: each entered TestClient runs the app's lifespan on its own thread and
    event loop. FastMCP's lifespan points the app's /mcp route at the session manager it starts, so with two of them
    running, an MCP session opened through the other client runs on one loop inside a task group hosted on the
    other, and the inner client's exit then waits for a wakeup sent from the wrong thread: now and then it is lost
    and the test hangs forever. So app/main.py refuses the second entry, and extra browsers share c's loop instead.
    (A TestClient that is never entered serves each request on a new loop of its own, which is fine for the API but
    not for /mcp: send /mcp through `c` or a browser(c).)"""
    return _Browser(c, **kwargs)


def signup(c, email="owner@acme.test", company="Acme Builders", license_number="123456") -> dict:
    r = c.post("/api/auth/signup", json={"company_name": company, "email": email, "password": PW,
                                         "cslb_license": license_number})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
    return r.json()


def login(c, email: str, password: str = PW) -> None:
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")


def join(c, owner, email: str, role: str = "field", password: str = PW, name: str | None = None) -> dict:
    """The owner (client `owner`, signed in) invites `email`; client `c` accepts the invite link with `password`
    and is signed in as that person, with the CSRF header set. Returns the invite response."""
    r = owner.post("/api/users", json={"email": email, "role": role, **({"name": name} if name else {})})
    assert r.status_code == 200, r.text
    invite = r.json()
    token = invite["invite_path"].rsplit("/", 1)[1]
    r = c.post("/api/auth/invite/accept", json={"token": token, "password": password})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
    return invite


def redirect_query(r) -> dict:
    """The query of a redirect into the web app (#/login?..., #/settings?...) plus "message", the text the server
    left for that page in the tc_notice cookie (auth.set_notice: never in the URL, where anyone could write it)."""
    from urllib.parse import parse_qs, unquote
    loc = r.headers["location"]
    out = {k: v[0] for k, v in parse_qs(loc.split("?", 1)[1]).items()} if "?" in loc else {}
    assert "message" not in out, "a message in the URL could be anyone's words"
    notice = r.cookies.get("tc_notice")
    if notice:
        out["message"] = unquote(notice.strip('"'))
    return out


def fake_resolver(hosts: dict | None = None, default: str | None = PUBLIC_TEST_IP):
    """DNS for tests: hosts maps a name to an address or a list; other names get `default` (None = NXDOMAIN)."""
    def resolve(host: str, port: int) -> list[str]:
        value = (hosts or {}).get(host, default)
        if value is None:
            raise OSError("name does not resolve")
        return [value] if isinstance(value, str) else list(value)
    return resolve


def mock_http(platform, handler, hosts: dict | None = None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    platform.http_transport = httpx.MockTransport(wrapped)
    platform.http_resolver = fake_resolver(hosts)
    return seen


def json_response(status: int, body, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(),
                          headers={"content-type": "application/json", **(headers or {})})


CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    import base64
    import hashlib
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def oauth_connect(c, redirect_uri: str = CLAUDE_CALLBACK, client: dict | None = None, **authorize) -> dict:
    """The whole "Sign in with TradeComply" flow (app/oauth.py) for the person signed in on client `c`, the way
    Claude does it: register a public client, authorize, approve on the consent screen, exchange the code.
    Returns the client registration, the token response and the verifier."""
    from urllib.parse import parse_qs, urlsplit
    if client is None:
        r = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [redirect_uri],
                                            "token_endpoint_auth_method": "none"})
        assert r.status_code == 201, r.text
        client = r.json()
    verifier, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect_uri,
              "state": "s-" + secrets.token_hex(4), "code_challenge": challenge, "code_challenge_method": "S256",
              "scope": "tradecomply", **authorize}
    r = c.get("/oauth/authorize", params=params, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/#/connect?request="), r.text
    request_id = r.headers["location"].split("request=", 1)[1]
    r = c.post(f"/api/oauth/requests/{request_id}/approve")
    assert r.status_code == 200, r.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["redirect_to"]).query).items()}
    assert q["state"] == params["state"]
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"], "code_verifier": verifier,
                                     "client_id": client["client_id"], "redirect_uri": redirect_uri})
    assert r.status_code == 200, r.text
    return {"client": client, "tokens": r.json(), "verifier": verifier, "code": q["code"], "iss": q["iss"]}
