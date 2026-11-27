"""Sign in with TradeComply (app/oauth.py): OAuth 2.1 for MCP clients such as Claude custom connectors. Discovery
(RFC 9728, RFC 8414), dynamic client registration (RFC 7591), the authorization endpoint (redirect URI rules, PKCE,
resource, scope), the consent API, the token endpoint (single-use codes, PKCE, rotation and reuse detection),
revocation (RFC 7009), the /mcp gate for OAuth access tokens (audience, live role, limits, audit rows, 402), connected
apps, tenant isolation, the store's cleanup, and an end-to-end run of the official `mcp` Python SDK's OAuth client.
Every test runs on SQLite and (with TC_TEST_DATABASE_URL) on PostgreSQL."""
import base64
import html
import re
import socket
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

import anyio
import pytest
from fastapi.testclient import TestClient

from app import mcp_governance as gov
from app import oauth
from app.auth import RateLimiter, hash_key
from tests.support import (CLAUDE_CALLBACK, PW, backend, join, make_app, oauth_connect, pkce,  # noqa: F401
                           signup)
from tests.test_mcp_governance import INIT, MCP_HEADERS, lic, mcp_rows, mcp_session, sse_json

BASE = "https://tradecomply.example"
LOOPBACK_CB = "http://127.0.0.1:33418/callback"


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path, mcp_require_key=True, app_url=BASE)


@pytest.fixture
def owner(app):
    with TestClient(app, base_url=BASE) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def back_link(r) -> str:
    """The "Back to <app>" link of an authorization error page (never an automatic redirect)."""
    assert r.status_code == 400 and "location" not in r.headers, (r.status_code, r.headers)
    return html.unescape(re.search(r'<a class="btn" href="([^"]+)">Back to ', r.text).group(1))


def register(c, **meta) -> dict:
    r = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK],
                                        "token_endpoint_auth_method": "none", **meta})
    assert r.status_code == 201, r.text
    return r.json()


def authorize(c, client: dict, challenge: str, redirect_uri: str | None = CLAUDE_CALLBACK, **extra):
    params = {"response_type": "code", "client_id": client["client_id"], "state": "st8",
              "code_challenge": challenge, "code_challenge_method": "S256", **extra}
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    return c.get("/oauth/authorize", params={k: v for k, v in params.items() if v is not None},
                 follow_redirects=False)


def request_id(r) -> str:
    assert r.status_code == 302 and r.headers["location"].startswith("/#/connect?request="), (r.status_code, r.text)
    return r.headers["location"].split("request=", 1)[1]


def approved_code(c, client: dict, redirect_uri: str = CLAUDE_CALLBACK, **extra) -> tuple[str, str]:
    """(code, verifier) for the person signed in on `c`."""
    verifier, challenge = pkce()
    rid = request_id(authorize(c, client, challenge, redirect_uri, **extra))
    r = c.post(f"/api/oauth/requests/{rid}/approve")
    assert r.status_code == 200, r.text
    return query(r.json()["redirect_to"])["code"], verifier


def token(c, **form):
    return c.post("/oauth/token", data=form)


def mcp(c, access: str):
    return mcp_session(c, {"authorization": f"Bearer {access}"})


def tools_call_status(c, access: str) -> int:
    r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {access}"})
    return r.status_code


# ---------------------------------------------------------------------------- discovery and the 401 challenge
def test_discovery_documents(owner):
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        r = owner.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert r.headers["cache-control"] == "no-store" and r.headers["access-control-allow-origin"] == "*"
        assert r.json() == {"resource": f"{BASE}/mcp", "authorization_servers": [BASE],
                            "bearer_methods_supported": ["header"], "scopes_supported": ["tradecomply"],
                            "resource_name": "TradeComply"}
    m = owner.get("/.well-known/oauth-authorization-server").json()
    assert m["issuer"] == BASE and m["authorization_endpoint"] == f"{BASE}/oauth/authorize"
    assert m["token_endpoint"] == f"{BASE}/oauth/token" and m["registration_endpoint"] == f"{BASE}/oauth/register"
    assert m["revocation_endpoint"] == f"{BASE}/oauth/revoke"
    assert m["code_challenge_methods_supported"] == ["S256"] and m["response_types_supported"] == ["code"]
    assert m["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert set(m["token_endpoint_auth_methods_supported"]) == {"none", "client_secret_post", "client_secret_basic"}
    assert m["authorization_response_iss_parameter_supported"] is True
    assert "client_id_metadata_document_supported" not in m          # Claude would switch to CIMD
    assert owner.get("/.well-known/openid-configuration").status_code == 404
    pre = owner.options("/oauth/token", headers={"origin": "https://inspector.example",
                                                 "access-control-request-method": "POST"})
    assert pre.status_code == 204 and "MCP-Protocol-Version" in pre.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in pre.headers


def test_mcp_401_names_the_resource_metadata(app):
    with TestClient(app, base_url=BASE) as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 401
        challenge = r.headers["www-authenticate"]
        assert challenge == ('Bearer realm="tradecomply", resource_metadata="https://tradecomply.example/'
                             '.well-known/oauth-protected-resource/mcp", scope="tradecomply"')
        assert r.json()["error"]["data"]["get_a_key"] == f"{BASE}/#/settings"       # the key help stays
        r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": "Bearer tco_not-a-token"})
        assert r.status_code == 401 and r.headers["www-authenticate"].endswith(', error="invalid_token"')
        assert "access token" in r.json()["error"]["message"]


def test_without_app_url_oauth_is_off_on_a_public_host_and_agrees_on_loopback(tmp_path, monkeypatch):
    """Like SSO (sso.missing_env): with no APP_URL the issuer would be whatever a caller's Host or X-Forwarded-Host
    says, and plain http on a public host (MCP: authorization server endpoints MUST be https). So no OAuth is
    offered there; on a loopback host (a developer's machine, local mode) the challenge, resource and issuer agree."""
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    app = make_app(tmp_path, mcp_require_key=True, app_url=None)
    hdr = {"x-forwarded-host": "attacker.example", "x-forwarded-proto": "https"}
    with TestClient(app, base_url="http://tradecomply.example.com") as c:
        for h in ({}, hdr):
            r = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, **h})
            assert r.status_code == 401 and r.headers["www-authenticate"] == 'Bearer realm="tradecomply"'
            assert "sign_in" not in r.json()["error"]["data"] and r.json()["error"]["data"]["get_a_key"]
            for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server"):
                assert c.get(path, headers=h).status_code == 404
            r = c.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}, headers=h)
            assert r.status_code == 503 and "APP_URL" in r.json()["error_description"]
            assert c.post("/oauth/token", data={"grant_type": "refresh_token"}, headers=h).status_code == 503
            assert c.get("/oauth/authorize", params={"client_id": "x"}, headers=h).status_code == 503
        bad = c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "host": 'evil"x, y=z'})
        assert bad.headers["www-authenticate"].count('"') == 2           # the host can't break out of the value
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        challenge = c.post("/mcp", json=INIT, headers=MCP_HEADERS).headers["www-authenticate"]
        prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
        asm = c.get("/.well-known/oauth-authorization-server").json()
        assert 'resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp"' in challenge
        assert prm["resource"] == "http://127.0.0.1:8000/mcp"
        assert prm["authorization_servers"] == [asm["issuer"]] == ["http://127.0.0.1:8000"]
    for url, offered in (("https://app.customer.example", True), ("http://app.customer.example", False),
                         ("http://localhost:8000", True)):
        app.state.platform.settings.app_url = url
        with TestClient(app, base_url="http://internal:10000") as c:
            r = c.get("/.well-known/oauth-authorization-server")
            assert (r.status_code == 200 and r.json()["issuer"] == url) if offered else r.status_code == 404, url


# ---------------------------------------------------------------------------- registration
def test_register_public_and_confidential_clients(owner, app):
    pub = register(owner, application_type="native", logo_uri="https://x.example/logo.png")   # unknown: ignored
    assert pub["client_id"].startswith("tcc_") and "client_secret" not in pub
    assert pub["token_endpoint_auth_method"] == "none" and pub["redirect_uris"] == [CLAUDE_CALLBACK]
    assert pub["grant_types"] == ["authorization_code", "refresh_token"] and pub["response_types"] == ["code"]
    r = owner.post("/oauth/register", json={"redirect_uris": [LOOPBACK_CB], "client_name": "  My \n  editor\x07 "})
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    conf = r.json()
    # no token_endpoint_auth_method: the RFC 7591 default, client_secret_basic, with a secret shown once
    assert conf["token_endpoint_auth_method"] == "client_secret_basic" and conf["client_secret"].startswith("tcs_")
    assert conf["client_secret_expires_at"] == 0 and conf["client_name"] == "My editor"
    stored = app.state.platform.store.get_oauth_client(conf["client_id"])
    assert stored["secret_hash"] == hash_key(conf["client_secret"])
    with app.state.platform.store.connect() as con:
        dump = repr([tuple(r) for r in con.execute("select * from oauth_clients").fetchall()])
    assert conf["client_secret"] not in dump
    unnamed = register(owner, client_name=None)
    assert unnamed["client_name"] == "claude.ai"                       # falls back to where it returns
    assert len(register(owner, client_name="x" * 500)["client_name"]) == oauth.MAX_NAME


@pytest.mark.parametrize("meta, error", [
    ({}, "invalid_redirect_uri"),
    ({"redirect_uris": []}, "invalid_redirect_uri"),
    ({"redirect_uris": "https://claude.ai/cb"}, "invalid_redirect_uri"),
    ({"redirect_uris": ["http://claude.ai/cb"]}, "invalid_redirect_uri"),            # http only on loopback
    ({"redirect_uris": ["https://claude.ai/cb#frag"]}, "invalid_redirect_uri"),
    ({"redirect_uris": ["https://user:pw@claude.ai/cb"]}, "invalid_redirect_uri"),
    ({"redirect_uris": ["javascript:alert(1)"]}, "invalid_redirect_uri"),
    ({"redirect_uris": ["com.example.app:/cb"]}, "invalid_redirect_uri"),           # MCP: https or localhost
    ({"redirect_uris": ["https://claude.ai/a b"]}, "invalid_redirect_uri"),
    ({"redirect_uris": ["https://x.example/%d" % i for i in range(11)]}, "invalid_redirect_uri"),
    ({"redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": "private_key_jwt"},
     "invalid_client_metadata"),
    ({"redirect_uris": [CLAUDE_CALLBACK], "grant_types": ["client_credentials"]}, "invalid_client_metadata"),
    ({"redirect_uris": [CLAUDE_CALLBACK], "response_types": ["token"]}, "invalid_client_metadata"),
])
def test_register_refuses_bad_metadata(owner, meta, error):
    r = owner.post("/oauth/register", json=meta)
    assert r.status_code == 400 and r.json()["error"] == error, r.text


def test_register_refuses_non_json_and_is_rate_limited_per_ip_and_in_total(owner, app):
    r = owner.post("/oauth/register", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"
    platform = app.state.platform
    platform.oauth_register_limiter = RateLimiter(limit=1, window_s=3600)
    assert owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}).status_code == 201
    r = owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert r.json()["error"] == "temporarily_unavailable"
    platform.oauth_register_limiter = RateLimiter(limit=100, window_s=3600)
    platform.oauth_register_global_limiter = RateLimiter(limit=1, window_s=3600)
    assert register(owner)
    assert owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]},
                      headers={"x-forwarded-for": "198.51.100.9"}).status_code == 429
    # discovery is never limited (a 429 there makes the MCP SDK give up)
    assert all(owner.get("/.well-known/oauth-protected-resource/mcp").status_code == 200 for _ in range(5))


# ---------------------------------------------------------------------------- the authorization endpoint
def test_bad_client_or_redirect_gets_an_error_page_never_a_redirect(owner):
    client = register(owner)
    _, challenge = pkce()
    for c_id, uri in (("tcc_unknown", CLAUDE_CALLBACK), (client["client_id"], "https://evil.example/cb"),
                      (client["client_id"], "https://claude.ai/api/mcp/auth_callback/x"),
                      (client["client_id"], CLAUDE_CALLBACK + "?x=1")):
        r = authorize(owner, {**client, "client_id": c_id}, challenge, uri)
        assert r.status_code == 400 and "location" not in r.headers
        assert r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "no-store"
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in \
            r.headers["content-security-policy"]
        assert "can't connect" in r.text and "evil.example" not in r.text
    r = owner.get("/oauth/authorize", params=[("client_id", client["client_id"]), ("client_id", "x"),
                                              ("redirect_uri", CLAUDE_CALLBACK)], follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


def test_redirect_uri_rules(owner):
    _, challenge = pkce()
    one = register(owner)
    assert request_id(authorize(owner, one, challenge, redirect_uri=None))        # one registered: may be omitted
    two = register(owner, redirect_uris=[CLAUDE_CALLBACK, "https://claude.ai/other"])
    assert authorize(owner, two, challenge, redirect_uri=None).status_code == 400  # two: must be sent
    loop = register(owner, redirect_uris=["http://127.0.0.1/callback", "http://localhost/callback",
                                          "http://[::1]:9/cb"])
    for uri in ("http://127.0.0.1:51234/callback", "http://localhost:8080/callback", "http://[::1]:7777/cb",
                "http://127.0.0.1/callback"):
        assert request_id(authorize(owner, loop, challenge, uri)), uri               # any port on loopback
    for uri in ("http://127.0.0.1:5/other", "http://localhost.evil.example:5/callback",
                "http://127.0.0.2:5/callback", "https://127.0.0.1:5/callback", "http://127.0.0.1:5/callback#x",
                "http://127.0.0.1:5/callback?a=1"):
        assert authorize(owner, loop, challenge, uri).status_code == 400, uri
    lh = register(owner, redirect_uris=["http://localhost/callback"])
    assert authorize(owner, lh, challenge, "http://127.0.0.1:5/callback").status_code == 400   # hosts don't mix


@pytest.mark.parametrize("change, error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"code_challenge": None}, "invalid_request"),
    ({"code_challenge_method": None}, "invalid_request"),                 # absent means plain: refused
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge": "short"}, "invalid_request"),
    ({"scope": "tradecomply admin"}, "invalid_scope"),
    ({"resource": "https://other.example/mcp"}, "invalid_target"),
    ({"resource": f"{BASE}/api"}, "invalid_target"),
    ({"state": "s" * (oauth.MAX_STATE + 1)}, "invalid_request"),
])
def test_authorize_errors_link_back_to_the_app_with_state_and_iss(owner, change, error):
    """Shown on a page with a link back (error, state, iss): anyone can register a redirect address, so an
    automatic redirect would make TradeComply an open redirector."""
    client = register(owner)
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CLAUDE_CALLBACK,
              "state": "st8", "code_challenge": challenge, "code_challenge_method": "S256", **change}
    r = owner.get("/oauth/authorize", params={k: v for k, v in params.items() if v is not None},
                  follow_redirects=False)
    link = back_link(r)
    assert link.startswith(CLAUDE_CALLBACK + "?") and "Back to claude.ai" in r.text
    q = query(link)
    assert q["error"] == error and q["iss"] == BASE and q.get("state") == params["state"]
    assert q["error_description"] in html.unescape(r.text)


def test_authorize_accepts_canonical_resource_and_ignores_offline_access(owner, app):
    client = register(owner)
    _, challenge = pkce()
    for res in ("HTTPS://TradeComply.Example/mcp/", "https://tradecomply.example:443/mcp", f"{BASE}/mcp"):
        assert request_id(authorize(owner, client, challenge, resource=res, scope="tradecomply offline_access"))
    r = owner.get("/oauth/authorize", params=[("client_id", client["client_id"]), ("redirect_uri", CLAUDE_CALLBACK),
                                              ("response_type", "code"), ("code_challenge", challenge),
                                              ("code_challenge_method", "S256"), ("scope", "a"), ("scope", "b")],
                  follow_redirects=False)
    assert query(back_link(r))["error"] == "invalid_request"                     # parameters must not repeat
    rid = request_id(authorize(owner, client, challenge))
    with app.state.platform.store.connect() as con:          # only the hash of the request id is stored
        ids = [r[0] for r in con.execute("select id from oauth_requests").fetchall()]
    assert rid not in ids and hash_key(rid) in ids
    assert owner.get(f"/api/oauth/requests/{rid}").json()["scopes"][0]["id"] == "tradecomply"


# ---------------------------------------------------------------------------- consent
def test_consent_shows_who_and_where_and_needs_a_signed_in_person(owner, app):
    client = register(owner, client_name="Claude")
    _, challenge = pkce()
    rid = request_id(authorize(owner, client, challenge))
    v = owner.get(f"/api/oauth/requests/{rid}")
    assert v.status_code == 200 and v.headers["cache-control"] == "no-store"
    assert {k: v.json()[k] for k in ("client_name", "redirect_host", "user_email", "company_name", "role",
                                     "local_only")} == {
        "client_name": "Claude", "redirect_host": "claude.ai", "user_email": "owner@acme.test",
        "company_name": "Acme Builders", "role": "owner", "local_only": False}
    assert 0 < v.json()["expires_in"] <= oauth.REQUEST_TTL_S
    local = register(owner, redirect_uris=[LOOPBACK_CB])
    lrid = request_id(authorize(owner, local, challenge, LOOPBACK_CB))
    assert owner.get(f"/api/oauth/requests/{lrid}").json()["local_only"] is True
    key = owner.post("/api/keys", json={"name": "k"}).json()["key"]
    with TestClient(app, base_url=BASE) as anon:
        assert anon.get(f"/api/oauth/requests/{rid}").status_code == 401
        assert anon.post(f"/api/oauth/requests/{rid}/approve").status_code == 401
        h = {"authorization": f"Bearer {key}"}
        assert anon.get(f"/api/oauth/requests/{rid}", headers=h).status_code == 403
        assert anon.post(f"/api/oauth/requests/{rid}/approve", headers=h).status_code == 403    # never a key
    with TestClient(app, base_url=BASE) as no_csrf:
        no_csrf.cookies.update(owner.cookies)
        assert no_csrf.post(f"/api/oauth/requests/{rid}/approve").status_code == 403           # CSRF
    assert owner.get("/api/oauth/requests/not-a-real-request-id-at-all").status_code == 404


def test_approve_is_single_use_and_deny_returns_access_denied(owner, app):
    client = register(owner)
    _, challenge = pkce()
    rid = request_id(authorize(owner, client, challenge))
    r = owner.post(f"/api/oauth/requests/{rid}/approve")
    q = query(r.json()["redirect_to"])
    assert r.json()["redirect_to"].startswith(CLAUDE_CALLBACK + "?code=tcg_")
    assert q["state"] == "st8" and q["iss"] == BASE
    assert owner.post(f"/api/oauth/requests/{rid}/approve").status_code == 404
    assert owner.post(f"/api/oauth/requests/{rid}/deny").status_code == 404
    rid = request_id(authorize(owner, client, challenge))
    r = owner.post(f"/api/oauth/requests/{rid}/deny")
    q = query(r.json()["redirect_to"])
    assert q["error"] == "access_denied" and q["state"] == "st8" and q["iss"] == BASE and "code" not in q
    assert owner.post(f"/api/oauth/requests/{rid}/approve").status_code == 404
    rid = request_id(authorize(owner, client, challenge))
    with app.state.platform.store.connect() as con:
        con.execute("update oauth_requests set expires = 1")
    assert owner.get(f"/api/oauth/requests/{rid}").status_code == 404
    assert owner.post(f"/api/oauth/requests/{rid}/approve").status_code == 404
    acts = [a["tool"] for a in owner.get("/api/audit").json()]
    assert acts.count("oauth.connect") == 1


# ---------------------------------------------------------------------------- the token endpoint
def test_full_flow_tokens_and_tool_call(owner, app):
    got = oauth_connect(owner)
    t = got["tokens"]
    assert got["iss"] == BASE and t["token_type"] == "Bearer" and t["expires_in"] == 3600
    assert t["access_token"].startswith("tco_") and t["refresh_token"].startswith("tcr_") and t["scope"] == "tradecomply"
    with TestClient(app, base_url=BASE) as bot:
        h = mcp(bot, t["access_token"])
        r = lic(bot, h)
        assert r.status_code == 200 and "result" in sse_json(r.text)
        listed = bot.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        assert len(sse_json(listed.text)["result"]["tools"]) > 10
        assert bot.get("/api/projects", headers={"authorization": f"Bearer {t['access_token']}"}).status_code == 401
    rows = mcp_rows(app, owner.tenant_id)
    assert rows[0]["oauth_grant_id"] and rows[0]["api_key_id"] is None and rows[0]["status"] == "ok"
    act = [a for a in owner.get("/api/audit").json() if a["source"] == "mcp"]
    assert act[0]["user"] == "owner@acme.test via Claude"
    log = owner.get("/api/mcp/audit").json()["items"][0]
    assert log["app_name"] == "Claude (owner@acme.test)" and log["oauth_grant_id"] == rows[0]["oauth_grant_id"]
    with app.state.platform.store.connect() as con:            # only hashes are stored
        dump = repr([tuple(r) for r in con.execute("select * from oauth_tokens").fetchall()])
    for plain in (t["access_token"], t["refresh_token"], got["code"]):
        assert plain not in dump and hash_key(plain) in dump


def test_wrong_verifier_or_client_changes_nothing_but_a_valid_replay_revokes(owner, app):
    client, other = register(owner), register(owner)
    code, verifier = approved_code(owner, client)
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CALLBACK}
    bad = [{**base, "client_id": client["client_id"], "code_verifier": pkce()[0]},           # wrong verifier
           {**base, "client_id": other["client_id"], "code_verifier": verifier},             # another app
           {**base, "client_id": client["client_id"], "code_verifier": verifier,
            "redirect_uri": "https://claude.ai/other"}]
    for form in bad:
        r = token(owner, **form)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant", form
    r = token(owner, **{**base, "client_id": client["client_id"], "code_verifier": verifier,
                        "resource": "https://other.example/mcp"})
    assert r.json()["error"] == "invalid_target"
    # the real client still gets its tokens (redirect_uri may be left out: OAuth 2.1)
    r = token(owner, grant_type="authorization_code", code=code, client_id=client["client_id"],
              code_verifier=verifier, resource=f"{BASE}/mcp")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    access = r.json()["access_token"]
    assert tools_call_status(owner, access) == 200
    # a wrong-parameter replay of the used code does not revoke ...
    assert token(owner, **bad[1]).json()["error"] == "invalid_grant" and tools_call_status(owner, access) == 200
    # ... a valid replay does: everything issued from the code stops working
    r = token(owner, **{**base, "client_id": client["client_id"], "code_verifier": verifier})
    assert r.json()["error"] == "invalid_grant"
    assert tools_call_status(owner, access) == 401
    assert owner.get("/api/oauth/connections").json() == []


def test_expired_code_and_malformed_requests(owner, app):
    client = register(owner)
    code, verifier = approved_code(owner, client)
    with app.state.platform.store.connect() as con:
        con.execute("update oauth_tokens set expires = 1 where kind = 'code'")
    r = token(owner, grant_type="authorization_code", code=code, client_id=client["client_id"], code_verifier=verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = owner.post("/oauth/token", json={"grant_type": "authorization_code", "code": code})
    assert r.json()["error"] == "invalid_request"                                       # form-encoded only
    r = owner.post("/oauth/token", content=f"grant_type=refresh_token&refresh_token=a&refresh_token=b&client_id="
                                           f"{client['client_id']}",
                   headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.json()["error"] == "invalid_request" and "refresh_token" in r.json()["error_description"]
    assert token(owner, grant_type="password", client_id=client["client_id"]).json()["error"] == \
        "unsupported_grant_type"
    assert token(owner, grant_type="authorization_code", code=code, client_id=client["client_id"],
                 code_verifier="short").json()["error"] == "invalid_request"
    r = token(owner, grant_type="authorization_code", code=code, code_verifier=verifier)          # no client_id
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert r.headers["access-control-allow-origin"] == "*"


def test_confidential_clients_authenticate_the_way_they_registered(owner):
    basic = owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}).json()
    post = owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK],
                                               "token_endpoint_auth_method": "client_secret_post"}).json()
    pub = register(owner)

    def b64(cid, secret):
        return "Basic " + base64.b64encode(f"{quote(cid, safe='')}:{quote(secret, safe='')}".encode()).decode()
    code, verifier = approved_code(owner, basic)
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}
    for headers, extra in (({"authorization": b64(basic["client_id"], "tcs_wrong")}, {}),
                           ({}, {"client_id": basic["client_id"], "client_secret": basic["client_secret"]}),
                           ({}, {"client_id": basic["client_id"]}),
                           ({"authorization": "Basic !!!"}, {}),
                           ({"authorization": "Bearer x"}, {})):
        r = owner.post("/oauth/token", data={**form, **extra}, headers=headers)
        assert r.status_code == 401 and r.json()["error"] == "invalid_client", (headers, extra)
    r = owner.post("/oauth/token", data={**form, "client_id": pub["client_id"]},
                   headers={"authorization": b64(basic["client_id"], basic["client_secret"])})
    assert r.json()["error"] == "invalid_request"                     # Basic and a different body client_id
    r = owner.post("/oauth/token", data={**form, "client_id": basic["client_id"]},        # the SDK sends both
                   headers={"authorization": b64(basic["client_id"], basic["client_secret"])})
    assert r.status_code == 200, r.text
    code, verifier = approved_code(owner, post)
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": post["client_id"]}
    assert token(owner, **form, client_secret="tcs_wrong").status_code == 401
    assert owner.post("/oauth/token", data=form, headers={"authorization": b64(post["client_id"],
                                                                                 post["client_secret"])}
                      ).status_code == 401                            # registered for _post, not Basic
    assert token(owner, **form, client_secret=post["client_secret"]).status_code == 200
    code, verifier = approved_code(owner, pub)
    r = token(owner, grant_type="authorization_code", code=code, code_verifier=verifier, client_id=pub["client_id"],
              client_secret="anything")
    assert r.status_code == 401                                       # a public client sends no secret


def test_refresh_rotates_and_reuse_revokes_after_the_grace_period(owner, app):
    got = oauth_connect(owner)
    cid, t1 = got["client"]["client_id"], got["tokens"]
    r = token(owner, grant_type="refresh_token", refresh_token=t1["refresh_token"], client_id=cid,
              resource=f"{BASE}/mcp", scope="tradecomply")
    assert r.status_code == 200, r.text
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["access_token"] != t1["access_token"]
    assert tools_call_status(owner, t2["access_token"]) == 200
    # the old refresh token again, right away (a duplicate sent at the same moment): refused, connection kept
    r = token(owner, grant_type="refresh_token", refresh_token=t1["refresh_token"], client_id=cid)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert tools_call_status(owner, t2["access_token"]) == 200
    # wider scope, wrong resource, another app: refused, nothing revoked
    assert token(owner, grant_type="refresh_token", refresh_token=t2["refresh_token"], client_id=cid,
                 scope="tradecomply admin").json()["error"] == "invalid_scope"
    assert token(owner, grant_type="refresh_token", refresh_token=t2["refresh_token"], client_id=cid,
                 resource="https://other.example/mcp").json()["error"] == "invalid_target"
    other = register(owner)
    assert token(owner, grant_type="refresh_token", refresh_token=t2["refresh_token"],
                 client_id=other["client_id"]).json()["error"] == "invalid_grant"
    assert token(owner, grant_type="refresh_token", refresh_token="tcr_unknown", client_id=cid).json()["error"] == \
        "invalid_grant"
    # after the grace period, a reused refresh token means it was copied: the whole connection ends
    with app.state.platform.store.connect() as con:
        con.execute("update oauth_tokens set used_at = used_at - ? where used_at is not null",
                    (oauth.REFRESH_GRACE_S + 5,))
    assert token(owner, grant_type="refresh_token", refresh_token=t1["refresh_token"], client_id=cid).json()[
        "error"] == "invalid_grant"
    assert tools_call_status(owner, t2["access_token"]) == 401
    assert token(owner, grant_type="refresh_token", refresh_token=t2["refresh_token"], client_id=cid).json()[
        "error"] == "invalid_grant"


def test_expired_access_and_refresh_tokens(owner, app):
    got = oauth_connect(owner)
    cid, t = got["client"]["client_id"], got["tokens"]
    store = app.state.platform.store
    with store.connect() as con:
        con.execute("update oauth_tokens set expires = 1 where kind = 'access'")
    assert tools_call_status(owner, t["access_token"]) == 401
    r = token(owner, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=cid)
    assert r.status_code == 200
    with store.connect() as con:
        con.execute("update oauth_grants set expires = 1")
    assert tools_call_status(owner, r.json()["access_token"]) == 401
    assert token(owner, grant_type="refresh_token", refresh_token=r.json()["refresh_token"], client_id=cid).json()[
        "error"] == "invalid_grant"
    store.purge_oauth()
    with store.connect() as con:
        assert con.execute("select count(*) from oauth_tokens where kind = 'access' and expires = 1"
                           ).fetchone()[0] == 0


def test_revocation(owner, app):
    got = oauth_connect(owner)
    cid, t = got["client"]["client_id"], got["tokens"]
    other = register(owner)
    assert owner.post("/oauth/revoke", data={"token": "tco_nothing", "client_id": cid}).status_code == 200
    r = owner.post("/oauth/revoke", data={"token": t["refresh_token"], "client_id": other["client_id"]})
    assert r.status_code == 200 and tools_call_status(owner, t["access_token"]) == 200     # not that app's token
    assert owner.post("/oauth/revoke", data={"token": t["access_token"], "client_id": cid}).status_code == 200
    assert tools_call_status(owner, t["access_token"]) == 401                              # just this token
    r = token(owner, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=cid)
    assert r.status_code == 200
    assert owner.post("/oauth/revoke", data={"token": r.json()["refresh_token"], "client_id": cid,
                                             "token_type_hint": "refresh_token"}).status_code == 200
    assert tools_call_status(owner, r.json()["access_token"]) == 401                      # the whole connection
    assert owner.get("/api/oauth/connections").json() == []
    assert owner.post("/oauth/revoke", data={"client_id": cid}).json()["error"] == "invalid_request"
    assert owner.post("/oauth/revoke", data={"token": "x"}).status_code == 401


def test_token_endpoint_rate_limits(owner, app):
    """The app's own limit counts only tokens it was given: someone who merely knows its client_id (it is in every
    authorize URL) can't use it up, not even a confidential app's without the secret."""
    platform = app.state.platform
    got = oauth_connect(owner)
    cid, refresh = got["client"]["client_id"], got["tokens"]["refresh_token"]
    conf = owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK],
                                               "token_endpoint_auth_method": "client_secret_post"}).json()
    platform.oauth_client_limiter = RateLimiter(limit=2, window_s=60)
    with TestClient(app, base_url=BASE) as attacker:
        for i in range(4):
            h = {"x-forwarded-for": f"192.0.2.{i}"}
            assert attacker.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "tcr_x",
                                                       "client_id": cid}, headers=h).status_code == 400
            assert attacker.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "tcr_x",
                                                       "client_id": conf["client_id"], "client_secret": "tcs_wrong"},
                                 headers=h).status_code == 401
    assert token(owner, grant_type="refresh_token", refresh_token="tcr_x", client_id=conf["client_id"],
                 client_secret=conf["client_secret"]).status_code == 400            # its own bucket is untouched
    for _ in range(2):
        r = token(owner, grant_type="refresh_token", refresh_token=refresh, client_id=cid)
        assert r.status_code == 200, r.text
        refresh = r.json()["refresh_token"]
    r = token(owner, grant_type="refresh_token", refresh_token=refresh, client_id=cid)
    assert r.status_code == 429 and r.headers["retry-after"] and r.headers["cache-control"] == "no-store"


def test_per_ip_limits_cant_be_aimed_at_another_address(owner, app):
    """Claude's calls for every customer come from a few Anthropic addresses. A caller who puts one of them first in
    X-Forwarded-For fills only its own bucket: the proxies append the caller's real address to the chain."""
    platform = app.state.platform
    got = oauth_connect(owner)
    platform.oauth_ip_limiter = RateLimiter(limit=1, window_s=60)
    platform.oauth_register_limiter = RateLimiter(limit=1, window_s=60)
    claude, forged = {"x-forwarded-for": "160.79.104.10"}, {"x-forwarded-for": "160.79.104.10, 203.0.113.9"}
    with TestClient(app, base_url=BASE) as attacker:
        for _ in range(2):
            attacker.post("/oauth/token", data={"grant_type": "x"}, headers=forged)
            attacker.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}, headers=forged)
        assert attacker.post("/oauth/token", data={"grant_type": "x"}, headers=forged).status_code == 429
    r = owner.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": got["client"]["client_id"],
                                         "refresh_token": got["tokens"]["refresh_token"]}, headers=claude)
    assert r.status_code == 200, r.text
    assert owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}, headers=claude).status_code == 201
    # With TRUSTED_PROXY_HOPS the trusted hop is the address: changing the part in front no longer helps either
    platform.settings.trusted_proxy_hops = 1
    platform.oauth_ip_limiter = RateLimiter(limit=1, window_s=60)
    assert owner.post("/oauth/token", data={"grant_type": "x"},
                      headers={"x-forwarded-for": "1.1.1.1, 203.0.113.9"}).status_code == 400
    assert owner.post("/oauth/token", data={"grant_type": "x"},
                      headers={"x-forwarded-for": "2.2.2.2, 203.0.113.9"}).status_code == 429


def test_client_ip_with_trusted_proxy_hops():
    from types import SimpleNamespace

    from app.auth import forwarded_ip
    from app.config import _proxy_hops
    s = SimpleNamespace(local_mode=False, trusted_proxy_hops=0)
    assert forwarded_ip(s, "6.6.6.6, 203.0.113.9", "10.0.0.1") == "6.6.6.6"        # default: unchanged
    s.trusted_proxy_hops = 1
    assert forwarded_ip(s, "6.6.6.6, 203.0.113.9", "10.0.0.1") == "203.0.113.9"
    s.trusted_proxy_hops = 2
    assert forwarded_ip(s, "6.6.6.6, 203.0.113.9, 198.51.100.4", "10.0.0.1") == "203.0.113.9"
    assert forwarded_ip(s, "203.0.113.9", "10.0.0.1") == "10.0.0.1"         # didn't pass through both proxies
    assert forwarded_ip(s, None, "10.0.0.1") == "10.0.0.1"
    assert [_proxy_hops(v) for v in (None, "", "1", "2", "-1", "x", "9")] == [0, 0, 1, 2, 0, 0, 0]


# ---------------------------------------------------------------------------- the /mcp gate
def test_access_token_follows_the_person(owner, app):
    store = app.state.platform.store
    with TestClient(app, base_url=BASE) as o:
        office = join(o, owner, "office@acme.test", "office")
        t = oauth_connect(o)["tokens"]
        assert tools_call_status(owner, t["access_token"]) == 200
        assert owner.patch(f"/api/users/{office['id']}", json={"role": "field"}).status_code == 200
        assert tools_call_status(owner, t["access_token"]) == 200                    # read live: still allowed
        # a password change disconnects every app of that person
        r = o.post("/api/auth/password", json={"current_password": PW, "new_password": PW + "-new"})
        assert r.status_code == 200
        assert tools_call_status(owner, t["access_token"]) == 401
        o.headers["x-csrf-token"] = o.cookies.get("tc_csrf")                       # the new session's token
        t = oauth_connect(o)["tokens"]
        assert tools_call_status(owner, t["access_token"]) == 200
    # removing the person ends it at once, and a new person who gets the same id (SQLite) inherits nothing
    assert owner.delete(f"/api/users/{office['id']}").status_code == 200
    assert tools_call_status(owner, t["access_token"]) == 401
    new = owner.post("/api/users", json={"email": "next@acme.test", "role": "office"}).json()
    assert tools_call_status(owner, t["access_token"]) == 401
    assert all(g["user_id"] != new["id"] for g in store.list_oauth_grants(owner.tenant_id))


def test_subscription_audience_and_limits(owner, app, monkeypatch):
    platform = app.state.platform
    t = oauth_connect(owner)["tokens"]["access_token"]
    with TestClient(app, base_url=BASE) as bot:
        h = mcp(bot, t)
        platform.mcp_key_limiter = RateLimiter(limit=2, window_s=60)
        for rid in (2, 3):
            assert lic(bot, h, rid).status_code == 200
        r = lic(bot, h, 4)
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        assert platform.mcp_key_limiter.retry_after("key|None") == 0       # its own bucket, not the keys' one
        statuses = [x["status"] for x in mcp_rows(app, owner.tenant_id)]
        assert statuses[0] == "rate_limited"
        platform.mcp_key_limiter = RateLimiter(limit=600, window_s=60)
        # a tool outside the default scope (one that is not read-only) is refused with a JSON-RPC error
        real = gov.tool_index

        async def fake(p):
            index = dict(await real(p))
            index["verify_contractor_license"] = {**index["verify_contractor_license"], "read_only": False}
            return index
        monkeypatch.setattr(gov, "tool_index", fake)
        r = lic(bot, h, 5)
        assert r.status_code == 200 and "read-only tools" in r.json()["error"]["message"]
        monkeypatch.setattr(gov, "tool_index", real)
        platform.store.update_tenant(owner.tenant_id, status="canceled")
        assert lic(bot, h, 6).status_code == 402
        platform.store.update_tenant(owner.tenant_id, status="trialing")
        # the token was issued for https://tradecomply.example/mcp; under another address it is refused
        platform.settings.app_url = "https://other.example"
        r = lic(bot, h, 7)
        assert r.status_code == 401 and 'resource_metadata="https://other.example/' in r.headers["www-authenticate"]
        platform.settings.app_url = BASE
        assert lic(bot, h, 8).status_code == 200


# ---------------------------------------------------------------------------- connected apps
def test_connections_are_per_person_owners_see_the_company_and_companies_are_isolated(owner, app):
    mine = oauth_connect(owner)
    with TestClient(app, base_url=BASE) as o, TestClient(app, base_url=BASE) as other:
        join(o, owner, "office@acme.test", "office")
        theirs = oauth_connect(o, redirect_uri=LOOPBACK_CB)
        signup(other, email="owner@bravo.test", company="Bravo Electric", license_number="654321")
        rows = owner.get("/api/oauth/connections").json()
        assert [(r["user_email"], r["mine"]) for r in rows] == [("office@acme.test", False),
                                                               ("owner@acme.test", True)]
        assert rows[0]["redirect_host"] == "127.0.0.1" and rows[0]["scopes"][0]["id"] == "tradecomply"
        assert {"id", "client_name", "created", "last_used"} <= set(rows[0])
        assert [r["user_email"] for r in o.get("/api/oauth/connections").json()] == ["office@acme.test"]
        assert other.get("/api/oauth/connections").json() == []
        mine_id = next(r["id"] for r in rows if r["mine"])
        theirs_id = rows[0]["id"]
        assert other.delete(f"/api/oauth/connections/{mine_id}").status_code == 404     # another company
        assert o.delete(f"/api/oauth/connections/{mine_id}").status_code == 404         # not theirs
        assert tools_call_status(owner, mine["tokens"]["access_token"]) == 200
        assert owner.delete(f"/api/oauth/connections/{theirs_id}").status_code == 200    # owners may
        assert tools_call_status(owner, theirs["tokens"]["access_token"]) == 401
        assert o.delete(f"/api/oauth/connections/{theirs_id}").status_code == 404
        key = owner.post("/api/keys", json={"name": "k"}).json()["key"]
        assert other.get("/api/oauth/connections", headers={"authorization": f"Bearer {key}"}).status_code == 403
        assert owner.delete(f"/api/oauth/connections/{mine_id}").status_code == 200
        assert tools_call_status(owner, mine["tokens"]["access_token"]) == 401
    assert [a["tool"] for a in owner.get("/api/audit").json()].count("oauth.disconnect") == 2


def test_store_cleanup_and_shared_tables_hold_no_company(owner, app):
    store = app.state.platform.store
    oauth_connect(owner)
    stale = register(owner)
    _, challenge = pkce()
    request_id(authorize(owner, stale, challenge))
    with store.connect() as con:
        con.execute("update oauth_clients set created = '2020-01-01T00:00:00+00:00'")
        con.execute("update oauth_requests set expires = 1")
        rows = [dict(r) for t in ("oauth_clients", "oauth_requests") for r in
                con.execute(f"select * from {t}").fetchall()]
    assert rows and owner.tenant_id not in repr(rows)
    store.purge_oauth()
    with store.connect() as con:
        assert con.execute("select count(*) from oauth_requests").fetchone()[0] == 0
        left = [r[0] for r in con.execute("select id from oauth_clients").fetchall()]
    assert stale["client_id"] not in left and len(left) == 1          # the one with a grant stays


# ---------------------------------------------------------------------------- hardening (security review findings)
def audit_rows(c, tool: str) -> list[dict]:
    return [a for a in c.get("/api/audit").json() if a["tool"] == tool]


def test_look_alike_unicode_redirect_hosts_are_refused(owner):
    """The consent card shows the host as the one thing an app can't fake. https://clаude.ai (Cyrillic а) would
    read as claude.ai while the browser goes to xn--clude-5ve.ai, a domain anyone can register."""
    for uri in ("https://clаude.ai/api/mcp/auth_callback", "https://аррӏе.com/cb",
                "https://cl%D0%B0ude.ai/cb", "https://claude.ai/cb/é", "https://[2001:db8::1]/cb"):
        r = owner.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [uri]})
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri", uri
    client = register(owner)
    _, challenge = pkce()
    assert authorize(owner, client, challenge, "https://clаude.ai/api/mcp/auth_callback").status_code == 400
    puny = register(owner, redirect_uris=["https://xn--clude-5ve.ai/api/mcp/auth_callback"])   # shown as it is
    rid = request_id(authorize(owner, puny, challenge, "https://xn--clude-5ve.ai/api/mcp/auth_callback"))
    assert owner.get(f"/api/oauth/requests/{rid}").json()["redirect_host"] == "xn--clude-5ve.ai"


def test_anonymous_authorize_and_register_calls_store_little(owner, app):
    """/oauth/authorize and /oauth/register need no sign-in, so what one call can store is small and bounded: the
    newest MAX_PENDING requests per app, short redirect URIs and state, few redirect URIs per app."""
    store = app.state.platform.store
    for meta in ({"redirect_uris": ["https://x.example/%d" % i for i in range(oauth.MAX_REDIRECT_URIS + 1)]},
                 {"redirect_uris": ["https://x.example/" + "a" * oauth.MAX_URI]}):
        assert owner.post("/oauth/register", json=meta).json()["error"] == "invalid_redirect_uri"
    client = register(owner)
    _, challenge = pkce()
    for _ in range(oauth.MAX_PENDING + 3):
        request_id(authorize(owner, client, challenge, state="s" * oauth.MAX_STATE))
    with store.connect() as con:
        assert con.execute("select count(*) from oauth_requests").fetchone()[0] == oauth.MAX_PENDING
        con.execute("update oauth_requests set expires = expires - 60")          # the older ones
    newest = request_id(authorize(owner, client, challenge))
    with store.connect() as con:
        assert con.execute("select count(*) from oauth_requests").fetchone()[0] == oauth.MAX_PENDING
    assert owner.get(f"/api/oauth/requests/{newest}").status_code == 200


def test_unused_registrations_go_after_an_hour_or_a_day(owner, app):
    store = app.state.platform.store
    granted = oauth_connect(owner)["client"]["client_id"]
    idle, waiting, old = register(owner), register(owner), register(owner)
    _, challenge = pkce()
    request_id(authorize(owner, waiting, challenge))
    request_id(authorize(owner, old, challenge))
    with store.connect() as con:
        con.execute("update oauth_clients set created = ?", ("2000-01-01T00:00:00+00:00",))     # all very old
        con.execute("update oauth_clients set created = ? where id in (?, ?)",
                    (iso_ago(2 * 3600), idle["client_id"], waiting["client_id"]))              # two hours old
    store.purge_oauth()
    with store.connect() as con:
        left = {r[0] for r in con.execute("select id from oauth_clients").fetchall()}
        pending = {r[0] for r in con.execute("select client_id from oauth_requests").fetchall()}
    # no grant, no pending request, over an hour: gone; a pending sign-in keeps it, but never past a day
    assert left == {granted, waiting["client_id"]} and pending == {waiting["client_id"]}


def iso_ago(seconds: int) -> str:
    from app.store import iso_in
    return iso_in(-seconds)


def test_refresh_reuse_after_a_few_seconds_ends_a_thiefs_connection_too(owner, app, caplog):
    """A thief who rotated a stolen refresh token first must not keep the connection when the real app then sends
    the same (now used) token: only a duplicate sent at the same moment is let off."""
    assert oauth.REFRESH_GRACE_S <= 10
    got = oauth_connect(owner)
    cid, r1 = got["client"]["client_id"], got["tokens"]["refresh_token"]
    with TestClient(app, base_url=BASE) as thief:
        stolen = token(thief, grant_type="refresh_token", refresh_token=r1, client_id=cid).json()
    with app.state.platform.store.connect() as con:
        con.execute("update oauth_tokens set used_at = used_at - ? where used_at is not null and kind = 'refresh'",
                    (oauth.REFRESH_GRACE_S + 1,))
    with caplog.at_level("WARNING", logger="tradecomply.oauth"):
        assert token(owner, grant_type="refresh_token", refresh_token=r1, client_id=cid).json()["error"] == \
            "invalid_grant"
    assert tools_call_status(owner, stolen["access_token"]) == 401
    assert token(owner, grant_type="refresh_token", refresh_token=stolen["refresh_token"],
                 client_id=cid).json()["error"] == "invalid_grant"
    assert "used refresh token was sent again" in caplog.text
    assert [a["args"]["reason"] for a in audit_rows(owner, "oauth.revoked")] == ["refresh_token_reused"]


def test_every_ended_connection_and_denial_is_in_the_activity_log(owner, app, caplog):
    def reasons():
        return sorted(a["args"]["reason"] for a in audit_rows(owner, "oauth.revoked"))
    # a valid code replay
    client = register(owner)
    code, verifier = approved_code(owner, client)
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
            "client_id": client["client_id"]}
    assert token(owner, **form).status_code == 200
    with caplog.at_level("WARNING", logger="tradecomply.oauth"):
        assert token(owner, **form).json()["error"] == "invalid_grant"
    assert "one-time code was used twice" in caplog.text and reasons() == ["code_reused"]
    # the app revokes its refresh token
    got = oauth_connect(owner)
    assert owner.post("/oauth/revoke", data={"token": got["tokens"]["refresh_token"],
                                             "client_id": got["client"]["client_id"]}).status_code == 200
    assert reasons() == ["code_reused", "revoked_by_app"]
    # a removed person; a password change
    with TestClient(app, base_url=BASE) as o:
        office = join(o, owner, "o@acme.test", "office")
        oauth_connect(o)
    assert owner.delete(f"/api/users/{office['id']}").status_code == 200
    oauth_connect(owner)
    assert owner.post("/api/auth/password", json={"current_password": PW, "new_password": PW + "-new"}
                      ).status_code == 200
    owner.headers["x-csrf-token"] = owner.cookies.get("tc_csrf")
    assert reasons() == ["code_reused", "password_changed", "person_removed", "revoked_by_app"]
    assert all(a["user"] == "TradeComply (automatic)" for a in audit_rows(owner, "oauth.revoked"))
    # a denial
    _, challenge = pkce()
    rid = request_id(authorize(owner, client, challenge))
    assert owner.post(f"/api/oauth/requests/{rid}/deny").status_code == 200
    assert [(a["args"]["app"], a["args"]["returns_to"]) for a in audit_rows(owner, "oauth.deny")] == \
        [("Claude", "claude.ai")]


def test_pending_request_ids_never_reach_the_access_log():
    import logging

    from app.main import RedactTokens
    rid = "Q" * 43
    for method, path in (("GET", f"/api/oauth/requests/{rid}"), ("POST", f"/api/oauth/requests/{rid}/approve"),
                         ("POST", f"/api/oauth/requests/{rid}/deny")):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                ("203.0.113.5:1", method, path, "1.1", 200), None)
        RedactTokens().filter(rec)
        assert rid not in rec.getMessage() and "/api/oauth/requests/[redacted]" in rec.getMessage()


def test_a_request_can_only_be_approved_in_the_browser_that_started_it(owner, app):
    """A pending request's id alone (a link someone was sent, a log) must not let another signed-in person approve
    it as themselves and connect the waiting app to their company."""
    with TestClient(app, base_url=BASE) as initiator, TestClient(app, base_url=BASE) as other:
        client = register(initiator)
        verifier, challenge = pkce()
        r = authorize(initiator, client, challenge)
        rid = request_id(r)
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("tc_oauth_") and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert "Path=/api/oauth/requests/" in cookie and "Max-Age=600" in cookie and "Secure" in cookie
        signup(other, email="boss@bravo.test", company="Bravo Electric", license_number="654321")
        for call in (lambda: other.get(f"/api/oauth/requests/{rid}"),
                     lambda: other.post(f"/api/oauth/requests/{rid}/approve"),
                     lambda: other.post(f"/api/oauth/requests/{rid}/deny"),
                     lambda: owner.post(f"/api/oauth/requests/{rid}/approve")):
            r = call()
            assert r.status_code == 403 and "started in another browser" in r.json()["detail"]
        # forging the cookie needs the server's key
        other.cookies.set(cookie.split("=", 1)[0], "0" * 32, path="/api/oauth/requests/")
        assert other.post(f"/api/oauth/requests/{rid}/approve").status_code == 403
        # the browser that started it signs in (or signs up) and approves
        signup(initiator, email="owner@charlie.test", company="Charlie Co", license_number="111111")
        r = initiator.post(f"/api/oauth/requests/{rid}/approve")
        assert r.status_code == 200
        t = token(initiator, grant_type="authorization_code", code=query(r.json()["redirect_to"])["code"],
                  code_verifier=verifier, client_id=client["client_id"])
        assert t.status_code == 200
        assert app.state.platform.store.oauth_access(hash_key(t.json()["access_token"]))["email"] == \
            "owner@charlie.test"


def test_removing_a_person_while_they_approve_leaves_no_grant(owner, app, backend):
    """The owner's Remove can land between the approval reading the person and writing the grant. The grant must
    not be written (on SQLite the next person invited would get the same id, and with it the connection)."""
    store = app.state.platform.store
    with TestClient(app, base_url=BASE) as o:
        office = join(o, owner, "leaver@acme.test", "office")
        client = register(o)
        verifier, challenge = pkce()
        rid = request_id(authorize(o, client, challenge))
        real = store.approve_oauth_request

        def removed_first(*a, **k):
            assert owner.delete(f"/api/users/{office['id']}").status_code == 200
            return real(*a, **k)
        store.approve_oauth_request = removed_first
        try:
            assert o.post(f"/api/oauth/requests/{rid}/approve").status_code == 404
        finally:
            store.approve_oauth_request = real
    with store.connect() as con:
        assert con.execute("select count(*) from oauth_grants where user_id = ?", (office["id"],)).fetchone()[0] == 0
    new = owner.post("/api/users", json={"email": "newhire@acme.test", "role": "owner"}).json()
    assert store.list_oauth_grants(owner.tenant_id, new["id"]) == []


def test_claudes_web_origins_are_allowed_on_mcp(owner, caplog):
    """If Claude's calls carry an Origin header it is claude.ai's or claude.com's (which one: unconfirmed). Other
    sites still get 403, and the refused value is logged so an operator can allow it."""
    a = oauth_connect(owner)["tokens"]["access_token"]
    h = {**MCP_HEADERS, "authorization": f"Bearer {a}"}
    for origin in ("https://claude.ai", "https://claude.com", "https://CLAUDE.ai/", BASE):
        assert owner.post("/mcp", json=INIT, headers={**h, "origin": origin}).status_code == 200, origin
    with caplog.at_level("WARNING", logger="tradecomply.mcp_governance"):
        for origin in ("https://evil.example", "https://claude.ai.evil.example", "http://claude.ai"):
            assert owner.post("/mcp", json=INIT, headers={**h, "origin": origin}).status_code == 403, origin
    assert "'https://evil.example'" in caplog.text and "MCP_ALLOWED_ORIGINS" in caplog.text


def test_public_clients_may_leave_client_id_out_when_refreshing_or_revoking(owner):
    """OAuth 2.1 section 4.3.1: the refresh token names its client. A confidential client still authenticates."""
    got = oauth_connect(owner)
    r = token(owner, grant_type="refresh_token", refresh_token=got["tokens"]["refresh_token"])
    assert r.status_code == 200, r.text
    assert owner.post("/oauth/revoke", data={"token": r.json()["refresh_token"]}).status_code == 200
    assert tools_call_status(owner, r.json()["access_token"]) == 401
    conf = owner.post("/oauth/register", json={"redirect_uris": [CLAUDE_CALLBACK]}).json()    # client_secret_basic
    code, verifier = approved_code(owner, conf)
    basic = base64.b64encode(f"{conf['client_id']}:{conf['client_secret']}".encode()).decode()
    t = owner.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
                   headers={"authorization": f"Basic {basic}"}).json()
    r = token(owner, grant_type="refresh_token", refresh_token=t["refresh_token"])
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    assert owner.post("/oauth/revoke", data={"token": t["refresh_token"]}).status_code == 401
    assert tools_call_status(owner, t["access_token"]) == 200


def test_a_missing_grant_type_is_invalid_request(owner):
    client = register(owner)
    assert token(owner, client_id=client["client_id"], code="x").json()["error"] == "invalid_request"
    assert token(owner, grant_type="", client_id=client["client_id"]).json()["error"] == "invalid_request"
    assert token(owner, grant_type="password", client_id=client["client_id"]).json()["error"] == \
        "unsupported_grant_type"


# ---------------------------------------------------------------------------- what customers are told
def test_mcp_client_config_explains_the_claude_connector():
    from tradecomply import integrations_guide as g
    r = g.mcp_client_config([], "https://tradecomply.example/mcp", trusted_url=True)
    cc = r.claude_connector
    assert cc["url"] == "https://tradecomply.example/mcp" and cc["works"] is True
    assert "Customize > Connectors" in cc["steps"][0] and "Add custom connector" in cc["steps"][0]
    assert "https://tradecomply.example/mcp" in cc["steps"][1] and "Approve" in cc["steps"][2]
    assert "Organization settings > Connectors" in cc["team_steps"][0] and "API key" not in " ".join(cc["steps"])
    assert r.claude_code_mcp_json["mcpServers"]["tradecomply"]["headers"]      # the key settings stay as they were
    local = g.mcp_client_config([], "http://127.0.0.1:8000/mcp", trusted_url=True).claude_connector
    assert local["works"] is False and "can't reach it" in local["notes"][-1]


# ---------------------------------------------------------------------------- the official MCP SDK as the client
@pytest.fixture
def served(tmp_path):
    """The app on a real port (in-process uvicorn): the SDK's streaming GET needs a real HTTP server."""
    uvicorn = pytest.importorskip("uvicorn")
    app = make_app(tmp_path, mcp_require_key=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield app, f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def test_the_mcp_python_sdk_connects_lists_tools_and_calls_one(served):
    """mcp.client.auth.OAuthClientProvider does the whole flow by itself: the 401, protected resource and server
    metadata discovery, dynamic registration (it omits token_endpoint_auth_method, so it gets a secret and uses
    HTTP Basic), PKCE, RFC 9207 iss check, the code exchange; then a normal MCP session."""
    httpx2 = pytest.importorskip("httpx2")
    pytest.importorskip("mcp.client.auth")
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
    app, base = served

    class Memory:
        tokens = info = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.info

        async def set_client_info(self, info):
            self.info = info

    out = {}

    async def run():
        async with httpx2.AsyncClient(base_url=base, timeout=30) as browser:     # the person's browser
            r = await browser.post("/api/auth/signup", json={"company_name": "Acme Builders", "email":
                                                             "owner@acme.test", "password": PW})
            assert r.status_code == 200, r.text
            csrf = browser.cookies.get("tc_csrf")
            landed = {}

            async def redirect_handler(url: str):
                r = await browser.get(url)                                 # /oauth/authorize -> the consent page
                rid = r.headers["location"].split("request=", 1)[1]
                r = await browser.post(f"/api/oauth/requests/{rid}/approve", headers={"x-csrf-token": csrf})
                assert r.status_code == 200, r.text
                landed.update(query(r.json()["redirect_to"]))

            async def callback_handler():
                return AuthorizationCodeResult(code=landed["code"], state=landed["state"], iss=landed["iss"])

            storage = Memory()
            provider = OAuthClientProvider(
                server_url=f"{base}/mcp", storage=storage, redirect_handler=redirect_handler,
                callback_handler=callback_handler,
                client_metadata=OAuthClientMetadata(client_name="MCP SDK test", redirect_uris=[LOOPBACK_CB]))
            async with httpx2.AsyncClient(auth=provider, timeout=30) as http:
                async with streamable_http_client(f"{base}/mcp", http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        out["tools"] = [t.name for t in (await session.list_tools()).tools]
                        result = await session.call_tool("verify_contractor_license", {"license_number": "123456"})
                        out["error"] = result.is_error
                        out["text"] = result.content[0].text
            out["client"] = storage.info
    anyio.run(run)
    assert "verify_contractor_license" in out["tools"] and out["error"] is False and "123456" in out["text"]
    assert out["client"].client_id.startswith("tcc_") and out["client"].token_endpoint_auth_method == \
        "client_secret_basic"
    store = app.state.platform.store
    tenant_id = store.user_by_email("owner@acme.test")["tenant_id"]
    rows = mcp_rows(app, tenant_id)
    assert [r["tool"] for r in rows] == ["verify_contractor_license"] and rows[0]["oauth_grant_id"]
    assert [g["client_name"] for g in store.list_oauth_grants(tenant_id)] == ["MCP SDK test"]
