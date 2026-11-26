"""Test helpers for integrations (connector engineers: build your tests on these).

    from tests.support import backend, make_app, signup, mock_http, json_response   # `backend` is autouse

- backend: runs each test on SQLite, then on PostgreSQL in a fresh schema when TC_TEST_DATABASE_URL is set
  (skipped otherwise), exactly like tests/test_platform.py.
- make_app(tmp_path, **settings): an app on the current backend, with the event worker off (call
  app.state.platform.events.drain() to deliver) unless events_background=True is passed.
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
    a = create_app(Settings.from_env(data_dir=str(tmp_path), **overrides))
    _db["apps"].append(a)
    return a


def store_for(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path), database_url=_db["url"])
    _db["stores"].append(st)
    return st


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
