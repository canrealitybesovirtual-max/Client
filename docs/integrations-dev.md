# Integrations: developer guide

This is the shared foundation every connector builds on. Each connector is **one file** in `app/connectors/`, so
several people can work in parallel without touching shared code. If you find yourself editing
`app/connectors/__init__.py`, `app/events.py`, `app/integrations_api.py`, `app/safe_http.py`, `app/crypto.py` or the
store, stop and raise it with the coordinator instead.

**Facts rule.** Every vendor URL, scope, header, field name, status code, rate limit and price in a connector comes
from the vendor's official documentation that you actually read. Put the page in `docs_url`. Anything you rely on
but could not confirm goes into the connector's `unconfirmed` tuple, which the settings page shows to the customer.
Never invent an endpoint.

**Secrets rule.** No real credential in code, tests, fixtures or logs. Operator app credentials come from env vars
(`required_env`), tenant credentials from `config_fields` with `secret=True` or from OAuth, and both are encrypted at
rest. Tests use made-up values and `httpx.MockTransport`.

---

## 1. Files

| File | What it is |
|---|---|
| `app/connectors/__init__.py` | `Connector` base class, `ConfigField`, `OAuth2Spec`, `TestResult`, errors, OAuth state/PKCE helpers, the auto-discovering registry |
| `app/connectors/_template.py` | Copy this. An API-key connector and an OAuth connector on placeholder `example.com` URLs. Not auto-loaded (leading `_`); the tests register it |
| `app/connectors/<provider>.py` | **Your connector** |
| `app/integrations_api.py` | `/api/integrations` routes (you don't edit this) |
| `app/events.py` | Event bus: `emit`, `subscribe`, the delivery worker |
| `app/webhooks.py` | Slack / Microsoft Teams / signed generic webhook formats and one delivery attempt |
| `app/safe_http.py` | `SafeHTTP`: the only way to make outbound HTTP calls (SSRF-guarded) |
| `app/crypto.py` | `SecretBox`: AES-256-GCM encryption of secrets at rest |
| `app/store.py` | Schema version 5 tables and their tenant-scoped methods (version 6 adds API key policy columns: docs/mcp-security.md) |
| `tests/support.py` | Test fixtures and helpers: both backends, `make_app`, `mock_http`, `json_response` |

---

## 2. Adding a connector

1. Copy `app/connectors/_template.py` to `app/connectors/<provider>.py`. Keep one class (or delete the one you
   don't need).
2. Fill in the class attributes:

```python
from . import ConfigField, Connector, NotConnected, OAuth2Spec, PermanentError, TestResult

class ProcoreConnector(Connector):
    provider = "procore"                 # ^[a-z][a-z0-9_]{1,39}$, unique, never changes (it's in URLs and the DB)
    display_name = "Procore"
    catalog_id = "..."                   # the id the coordinator assigned in the integration catalog
    description = "One or two plain sentences for the settings page."
    category = "project_management"      # project_management | accounting | payroll | hr | documents |
                                         # messaging | calendar | identity | other
    docs_url = "https://..."             # the official API docs you built this from (https)
    required_env = ("PROCORE_CLIENT_ID", "PROCORE_CLIENT_SECRET")   # operator-level app credentials only
    config_fields = (...)                # per-tenant settings, see 2.1
    oauth = OAuth2Spec(...)              # OAuth connectors only, see 2.2
    subscribes = ("sub_noncompliant",)   # events this connector delivers, see 4
    unconfirmed = ("Whether refresh tokens rotate (not stated on the OAuth page)",)
```

3. Implement:
   - `test(self) -> TestResult` (**required**): one cheap, read-only call with the stored credentials.
     Return `TestResult(ok, message, account=<company name or None>, details={...})`. Raise `NotConnected` if
     nothing is saved yet.
   - `handle_event(self, event, payload)` if `subscribes` is non-empty (see 4).
   - `on_connected(self, tokens) -> str | None` for OAuth connectors: return the connected account's label
     (e.g. from a "who am I" endpoint), shown in the UI.
   - Override `connect()`, `disconnect()` or `revoke()` only if the defaults don't fit (see 2.3).
4. Write `tests/test_<provider>.py` (see 6). Run `python -m pytest -q tests`.

Nothing else is needed. The registry imports every module in `app/connectors/` whose name doesn't start with `_`
and registers each `Connector` subclass defined in it. It validates the class (`validate_connector_class`): a
missing `test()`, an unknown event in `subscribes`, a non-https URL, OAuth env names missing from `required_env`, a
reserved or duplicate `provider` id all keep it from loading. A module that fails is logged and listed in
`connectors.import_errors` (and in `load_errors` on `GET /api/integrations` for owner/office); the other connectors
still load. `tests/test_integrations_api.py::test_discovery_loads_every_connector_module_cleanly` fails if any
module in the package has an error, so a broken connector can't be merged unnoticed.

Reserved provider ids: `webhooks events outbound providers oauth config test connect disconnect callback start
catalog`.

### 2.1 Per-tenant settings: `ConfigField`

```python
ConfigField(name, label, type="text", secret=False, required=False, help="", placeholder="",
            options=(), max_length=500, pattern=None, default=None)
```

- `type`: `text`, `textarea`, `url` (https only, SSRF-checked), `email`, `password` (must be `secret=True`),
  `number`, `boolean`, `select` (needs `options=(("value", "Label"), ...)`).
- `secret=True`: stored encrypted with the other secrets; never sent to the browser. The UI shows it as saved
  (`secrets_saved`); a blank value on save keeps the stored one.
- `pattern`: full-match regex for text-like fields.
- `name`: `^[a-z][a-z0-9_]{0,39}$`, not `oauth`.

The UI renders `config_schema` (a list of `ConfigField.to_schema()` dicts) generically; you write no UI code.
`Connector.clean_config(values, existing_config, existing_secrets)` validates a submission (unknown keys and bad
values raise `ConfigError` -> 422) and splits it into non-secret config and secret fields.

Inside the connector:

```python
self.config()            # dict of non-secret settings
self.secrets()           # dict of decrypted secrets: secret fields by name, OAuth tokens under "oauth"
self.save(status=..., config=..., secrets=..., external_account=..., last_error=...)   # any subset
self.status()            # {"status", "external_account", "last_error", "updated", "has_secret"}
self.env("NAME")         # an operator env var (None if unset); self.require_operator() raises NotConfigured
self.ctx.tenant_id, self.ctx.user_id, self.ctx.user_label, self.ctx.platform, self.ctx.store
```

Connection `status`: `disconnected`, `pending` (OAuth started), `connected`, `error`.

### 2.2 OAuth 2.0: `OAuth2Spec`

```python
OAuth2Spec(authorize_url, token_url, client_id_env, client_secret_env=None, scopes=(), scope_separator=" ",
           pkce=True, client_auth="basic", extra_authorize_params=(), revoke_url=None)
```

The base class implements the authorization-code grant (RFC 6749 section 4.1) with PKCE S256 (RFC 7636):

- `client_auth`: `"basic"` sends HTTP Basic with the form-urlencoded id and secret (RFC 6749 2.3.1; servers MUST
  support it), `"body"` sends `client_id`/`client_secret` form fields (some providers require this; check the
  docs), `"none"` is a public client (PKCE only).
- `pkce=False` if the provider's docs don't support PKCE.
- `scopes`: the least the connector needs, exactly as the provider's docs name them.
- `revoke_url`: an RFC 7009 revocation endpoint if the provider documents one; `disconnect()` calls it best-effort.
- `client_id_env` / `client_secret_env` must also be listed in `required_env`.

The flow, end to end (you write none of it):

1. `POST /api/integrations/<provider>/oauth/start` (owner/office, CSRF) creates a `state` signed with the app secret,
   bound to tenant + user + provider + redirect URI, valid 10 minutes, single use; the PKCE `code_verifier` travels
   inside it encrypted. Returns `{"authorize_url", "redirect_uri"}`; the connection becomes `pending`.
2. The browser signs in at the provider, which redirects to
   `GET /api/integrations/<provider>/oauth/callback?code=...&state=...`.
3. The callback checks the session cookie, the role, and the state (signature, expiry, same tenant and same user,
   not used before), exchanges the code at `token_url` with the verifier, stores the tokens encrypted, calls
   `on_connected()`, marks the connection `connected`, and redirects to
   `/#/settings?integration=<provider>&result=connected` (or `result=error`, with the message in the short-lived `tc_notice` cookie that only the server can set: text in the URL is never shown, so nobody can link to the page with their own words in its error banner).

**Operator setup** for an OAuth connector: register one OAuth app with the vendor with redirect URI
`<APP_URL>/api/integrations/<provider>/oauth/callback` (set `APP_URL`, or rely on Render's `RENDER_EXTERNAL_URL`, so
the URI is stable), then set the `required_env` variables on the server.

Calling the API: `self.access_token()` returns a valid token and refreshes it first when it expires within 60 s
(RFC 6749 section 6). Refreshes are serialized per tenant+provider in the process and compare-and-set in the
database, so with rotating refresh tokens two workers never both spend the same one. A refresh refused with
`invalid_grant` / `invalid_client` / `unauthorized_client` marks the connection `error` and raises `NotConnected`
("Reconnect"). `self.auth_headers()` is `{"authorization": "Bearer <token>"}`.

Token storage (in `secrets()["oauth"]`): `access_token`, `token_type`, `refresh_token` (kept if a refresh doesn't
return a new one), `scope`, `expires_at` (unix seconds, from `expires_in`).

### 2.3 Lifecycle defaults

| Method | Default |
|---|---|
| `connect()` | Settings-based connectors: `require_operator()`, `test()`, then status `connected` (with `account` as `external_account`) or `error` (with the message). OAuth connectors connect via the OAuth routes. |
| `disconnect()` | `revoke()` best-effort, then wipe the secrets and set `disconnected`; non-secret settings are kept. |
| `revoke()` | RFC 7009 POST to `oauth.revoke_url` if set; otherwise nothing. |
| `status()` | From the stored connection. |

### 2.4 Errors

Raise these (all in `app.connectors`); their messages are shown to the customer, so keep them plain and never
include a token, secret or response body.

| Exception | HTTP | Meaning |
|---|---|---|
| `ConnectorError` | 502 | Generic, user-facing failure |
| `NotConfigured` | 503 | The operator hasn't set `required_env` |
| `NotConnected` | 409 | Nothing usable saved for this tenant; reconnect |
| `ConfigError` | 422 | Bad settings (has `.field`) |
| `OAuthError` | 400 | An OAuth step failed (`.code` = RFC 6749 error code) |
| `PermanentError` | - | From `handle_event`: never retry this delivery |

---

## 3. Outbound HTTP: `SafeHTTP`

Never use `httpx`/`requests` directly. Use the platform client:

```python
with self.ctx.http() as http:                     # SafeHTTP; options can be overridden per call
    r = http.get(url, headers=..., params=...)
    r = http.post(url, json=... | data=... | content=..., headers=..., idempotent=None, retries=None,
                  timeout=None, max_bytes=None, follow_redirects=None)
```

It returns an `httpx.Response` with the body already read; HTTP error statuses are returned, not raised. It raises
`UnsafeURLError` (refused URL or address), `ResponseTooLarge`, or `UpstreamError` (connection failure, timeout, too
many redirects), all subclasses of `SafeHTTPError` with messages safe to show.

What it enforces: https only (http only for localhost with `allow_localhost=True`, tests only); no `user:pass@`;
ports 443 and 8443; the host must resolve only to public addresses (no private, loopback, link-local incl.
169.254.169.254, CGNAT, multicast, reserved, IPv6 ULA/link-local, or IPv6 forms embedding a private IPv4); the
check repeats at connect time on the address actually used (DNS rebinding); redirects followed by hand (max 3), each
target checked, `Authorization`/`Cookie` dropped across origins; env proxies ignored; 10 s read / 5 s connect /
30 s total per attempt; 5 MB response cap (decoded); 2 retries with exponential backoff and jitter on 429/500/502/
503/504, honoring `Retry-After` up to 30 s (a longer one is returned to you to reschedule). POST/PATCH are retried
only on 429 or when the connection never opened, unless you pass `idempotent=True`. Logs show method, host and
status only.

---

## 4. Events

### 4.1 Event names and payload

| Event | Meaning |
|---|---|
| `deadline_due_soon` | A tracked deadline (lien notice, COI or license expiry, toolbox talk, ...) is coming up or has passed |
| `license_problem_found` | A CSLB license check found a problem (status, classification, bond, workers' comp on file) |
| `sub_noncompliant` | A subcontractor failed a compliance check (license, insurance certificate, workers' comp, W-9, DIR) |
| `contract_generated` | A contract, waiver or notice was generated and saved as a document |
| `injury_report_due` | A workplace injury or illness needs a report by a deadline (the payload says which and when) |
| `payroll_issue` | A payroll or wage-rule check found an issue |

Nothing emits these yet: the modules that detect each condition call `emit` (coordinate with their owners). A new
name can be added with `events.register_event(name, description)`.

Payload (validated by `emit`; a violation is a `ValueError`, i.e. a bug in the emitter):

```python
{"title": "Sub's COI expires in 7 days",     # required, plain text, <= 200 chars
 "summary": "ABC Electric, policy ...",       # optional, <= 2000
 "severity": "warning",                       # info (default) | warning | critical
 "link": "/#/subs/12",                        # optional app path starting with "/"; made absolute with APP_URL
 "due_date": "2026-12-03",                    # optional YYYY-MM-DD
 "sub_id": 12, "project_id": 3, ...}          # anything else JSON; whole payload <= 32 KB
```

Don't put secrets or more personal data than the receiver needs into a payload: generic webhooks receive it as is.

### 4.2 Emitting

```python
platform = request.app.state.platform
platform.events.emit(tenant_id, "sub_noncompliant", payload, dedupe_key=f"sub:{sub_id}:wc")   # plain def / thread
await platform.events.aemit(tenant_id, "sub_noncompliant", payload)                           # async def handler
from app import events; events.emit(tenant_id, event, payload)                                # the running app's bus
```

`emit` resolves the subscribers, writes one `outbound_events` row per target in one transaction, wakes the worker and
returns the row ids (`[]` if nobody subscribes). It never waits for a delivery, and a database failure is logged and
swallowed (returns `[]`) so an alert can't break the request that raised it. It does run two or three quick queries,
so from `async def` code use `aemit` (the database must never be called on the event loop). With `dedupe_key`, each
target gets at most one delivery per key (enforced by a unique index); use it for conditions a scan finds again and
again. Rows are purged 30 days after they finish.

### 4.3 Subscribing

- **Connector**: list the events in `subscribes` and implement `handle_event(self, event, payload)`. It runs only for
  tenants whose connection is `connected`. Raise `PermanentError` when the delivery can never succeed; any other
  exception is retried. Use `self.access_token()` / `self.secrets()` as usual.
- **In-process function**:

```python
from app.events import subscribe

@subscribe(["deadline_due_soon", "injury_report_due"], name="calendar.push")   # or "*" for every event
def push(platform, tenant_id, event, payload): ...
```

- **Webhooks** (configured by the customer, see 5).

### 4.4 Delivery

A background thread (`platform.events`, started with the app; `EVENTS_WORKER=0` turns it off) claims due rows with row
locks (Postgres `FOR UPDATE SKIP LOCKED`; SQLite takes the write lock), so several instances never deliver one row
twice, and delivers up to 4 at a time. Outcomes:

| Status | When |
|---|---|
| `delivered` | 2xx / handler returned |
| `pending` + `next_attempt` | Retry: 5 s, 5 min, 30 min, 2 h, 5 h, 10 h, 14 h, 20 h, 24 h (the Standard Webhooks example schedule, +-10 % jitter), never sooner than `Retry-After` |
| `failed` | `PermanentError`, `NotConnected`, a Slack/Teams 4xx refusal, a redirect, a URL that now resolves to a private address, or 10 attempts used up |
| `skipped` | The webhook was deleted or turned off, the connector disconnected or uninstalled, the handler unregistered |

A worker that dies mid-delivery leaves the row `sending` with a 5-minute lease; it is retried when the lease ends.
`GET /api/integrations/outbound` is the delivery log.

---

## 5. Webhooks

Customers add them in Settings; three kinds:

- `slack`: a Slack incoming webhook (`https://hooks.slack.com/services/...`; other hosts are refused). Body
  `{"text": "*title*\nsummary\nDue: ...\n<link|Open in TradeComply>"}` in Slack mrkdwn with `& < >` escaped.
  Success is 200 `ok`; 4xx refusals are permanent; `no_service`, `no_active_hooks`, `channel_is_archived`,
  `team_disabled` and any 410 turn the webhook off. (docs.slack.dev, "Sending messages using incoming webhooks",
  "Formatting message text".)
- `teams`: a Microsoft Teams Workflows webhook ("When a Teams webhook request is received"). Body
  `{"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": null,
  "content": <Adaptive Card 1.2 with the title, summary and an Action.OpenUrl>}]}`.
  (learn.microsoft.com/en-us/connectors/teams.)
- `generic`: Standard Webhooks. Body `{"type": event, "timestamp": ISO 8601, "data": payload}`; headers
  `webhook-id` (same on every retry), `webhook-timestamp`, `webhook-signature: v1,<base64 HMAC-SHA256 of
  "{id}.{timestamp}.{body}">` with the `whsec_...` secret shown once at creation or rotation. Receivers verify with
  any Standard Webhooks library or `app.webhooks.verify(secret, headers, body)`. 410 turns the webhook off; other
  failures are retried; redirects are not followed.

Webhook URLs are credentials (Slack and Teams put the secret in the URL), so `webhooks.url` holds the URL
**encrypted**; responses show only `url_hint` (`https://host/…`).

---

## 6. Testing a connector

```python
import httpx, pytest
from fastapi.testclient import TestClient
from app import connectors as registry
from app.connectors.procore import ProcoreConnector
from tests.support import backend, json_response, make_app, mock_http, signup   # noqa: F401 (backend: autouse)

def vendor(request: httpx.Request) -> httpx.Response:
    # Answer exactly as the vendor's docs show: status, headers, JSON field names.
    if request.url.host == "api.vendor.example" and request.url.path == "/v1/me":   # use the real documented path
        assert request.headers["authorization"] == "Bearer test-token"
        return json_response(200, {"name": "Acme"})
    return json_response(404, {"error": "not found"})

def test_connect(tmp_path):
    app = make_app(tmp_path)                       # SQLite, then Postgres when TC_TEST_DATABASE_URL is set
    platform = app.state.platform
    platform.env = {"PROCORE_CLIENT_ID": "test-id", "PROCORE_CLIENT_SECRET": "test-secret"}   # fake operator env
    seen = mock_http(platform, vendor)             # every outbound call goes to vendor(); DNS is faked as public
    with TestClient(app) as c:
        signup(c)
        r = c.post("/api/integrations/procore/connect", json={"config": {...}})
        assert r.json()["status"] == "connected"
    assert seen[0].url.path == "/v1/me"
```

- A second person (another company, a field user, an anonymous caller): `with browser(c) as other:` (from
  `tests.support`), never a second `with TestClient(app)` on an app that is running. Each entered TestClient
  starts the app on its own event loop, and an MCP session opened through one client while another's loop owns
  /mcp can hang the test forever at exit, now and then; the app refuses the second start with a RuntimeError.
- `mock_http(platform, handler, hosts={"name": "10.0.0.1"})` lets you prove SSRF refusals too.
- `make_app` turns the event worker off; call `platform.events.drain()` to deliver queued events deterministically
  (`drain(now=datetime_in_the_future)` to run scheduled retries).
- To unit-test a connector without the API:
  `inst = ProcoreConnector(registry.ConnectorContext(platform, tenant_id))`.
- OAuth: see `tests/test_integrations_api.py` (`start_oauth`, `callback`, the `Vendor` class) for a full flow that
  checks the PKCE verifier against the challenge and the client authentication.
- Postgres locally: `TC_TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/tc_test python -m pytest -q tests`.

---

## 7. API routes (`/api/integrations`)

All need a session cookie (plus `X-CSRF-Token` on writes) or an API key. "staff" = owner or office.

| Method & path | Role | What |
|---|---|---|
| `GET /api/integrations` | any | `{"providers": [...], "events": [...], "load_errors"?}`; each provider: `provider, display_name, catalog_id, description, category, docs_url, auth ("oauth2"/"config"/"none"), operator_configured, missing_env (staff), config_schema, subscribes, unconfirmed, connection{status, external_account, last_error, updated}` |
| `GET /api/integrations/events` | any | The event catalog |
| `GET /api/integrations/catalog` | any | The product catalog (`tradecomply/data/integrations.json`, see 11) plus `tradecomply.mcp_url` (this server's `/mcp`) and `config_ready_ids`; cacheable, gzipped, cached by the service worker for offline use |
| `GET /api/integrations/catalog/mcp-config?ids=box,docusign` | any | Ready-to-paste AI-assistant settings (same as the `mcp_client_config` MCP tool) using this server's `/mcp` |
| `GET /api/integrations/{provider}` | staff | The provider view plus `config`, `secrets_saved` (names), `signed_in`, `secrets_readable` |
| `GET /api/integrations/{provider}/config` | staff | `{"config", "secrets_saved", "config_schema"}` |
| `PUT /api/integrations/{provider}/config` | staff | Body `{"config": {...}}`; validates, encrypts secret fields (blank keeps) |
| `POST /api/integrations/{provider}/connect` | staff, subscribed | Body `{"config"?}`; settings connectors: save + `connect()`; OAuth: returns `{"authorize_url"}` |
| `POST /api/integrations/{provider}/test` | staff, subscribed | `{"ok", "message", "account", "details", "connection"}`; flips connected/error |
| `POST /api/integrations/{provider}/disconnect` | staff | Revoke best-effort, wipe secrets |
| `POST /api/integrations/{provider}/oauth/start` | staff (a signed-in user, not an API key), subscribed | `{"authorize_url", "redirect_uri"}` |
| `GET /api/integrations/{provider}/oauth/callback` | the user who started | Redirects to `/#/settings?integration=..&result=connected|error` (an error's message in the `tc_notice` cookie) |
| `GET /api/integrations/webhooks` | staff | List (`url_hint`, never the URL) |
| `POST /api/integrations/webhooks` | staff, subscribed | `{"name", "url", "kind": slack|teams|generic, "events": [...|"*"], "enabled"}`; generic returns `signing_secret` once |
| `GET/PATCH/DELETE /api/integrations/webhooks/{id}` | staff | Read / change (name, url, kind, events, enabled) / delete |
| `POST /api/integrations/webhooks/{id}/test` | staff, subscribed | Sends a `test` message now: `{"ok", "status", "message", "disabled"}` |
| `POST /api/integrations/webhooks/{id}/rotate-secret` | staff | Generic only; new `signing_secret` once |
| `GET /api/integrations/outbound?status=&limit=&before_id=` | staff | Delivery log, newest first |

"subscribed" = the tenant's trial or subscription is active (otherwise 402). Test sends and connection tests are
limited to 30 per tenant per 10 minutes (429). Changes are written to the tenant's activity log
(`/api/audit`, tool `integrations.*`) with names and ids only.

---

## 8. Storage (schema version 5; version 6 adds the API key policy columns)

Both SQLite and PostgreSQL; on Postgres every new table has row level security on and no privileges for Supabase's
`anon`/`authenticated` roles, like migration 4. All methods filter by `tenant_id` except those marked OPERATOR ONLY.
Secret columns accept only `SecretBox` blobs (`v1.<kid>.<base64url>`); plaintext raises `ValueError`.

| Table | Columns |
|---|---|
| `integration_connections` | `id, tenant_id, provider, status, config_json, secret_blob, external_account, created, updated, last_error`, unique `(tenant_id, provider)` |
| `webhooks` | `id, tenant_id, name, url (encrypted), kind, events_json, signing_secret_blob, enabled, created, last_status, last_sent` |
| `outbound_events` | `id, tenant_id, event, payload_json, target, status, attempts, next_attempt, created`, plus `last_error` and `dedupe_key` (unique with tenant and target) |
| `mcp_audit_log` | `id, tenant_id (null = anonymous), api_key_id, tool, args_sha256, status (ok/error/denied/rate_limited), duration_ms, ip, created`, index `(tenant_id, created)` |
| `sso_configs` | `tenant_id (pk), issuer, client_id, secret_blob, allowed_domains_json, enforced, created` |
| `calendar_tokens` | `tenant_id, token_hash (pk, SHA-256 hex), created, revoked` |

Store methods (connectors normally use `self.save()` / `self.secrets()` instead):

```python
# connections
get_connection(tenant_id, provider, with_secret=False) -> dict | None   # has_secret; secret_blob only with_secret
list_connections(tenant_id) -> list[dict]
save_connection(tenant_id, provider, **fields) -> dict          # upsert; fields: status, config, secret_blob,
                                                                # external_account, last_error
update_connection(tenant_id, provider, *, expect_secret_blob=..., **fields) -> dict | None   # compare-and-set
delete_connection(tenant_id, provider) -> bool
connections_for_provider(provider, status="connected")          # OPERATOR ONLY (scheduled syncs)
# webhooks
create_webhook(tenant_id, *, name, url, kind, events, signing_secret_blob=None, enabled=True) -> dict
list_webhooks(tenant_id, enabled_only=False); get_webhook(tenant_id, id, with_secret=False)
update_webhook(tenant_id, id, **fields)  # name, url, kind, events, signing_secret_blob, enabled, last_status, last_sent
delete_webhook(tenant_id, id) -> bool; webhooks_for_event(tenant_id, event)
# outbound queue
enqueue_outbound(tenant_id, event, payload, targets, *, dedupe_key=None, delay_s=0) -> list[int]
claim_outbound(limit=20, lease_s=300, now=None)                 # OPERATOR ONLY (the worker)
finish_outbound(tenant_id, id, status, *, next_attempt=None, last_error=None) -> bool
get_outbound(tenant_id, id); list_outbound(tenant_id, status=None, limit=100, before_id=None)
purge_outbound(older_than) -> int                               # OPERATOR ONLY
# MCP audit log (written by the /mcp gate after each response: docs/mcp-security.md)
log_mcp_call(tenant_id, tool, status, *, api_key_id=None, args_sha256=None, duration_ms=None, ip=None) -> int
log_mcp_calls(entries, activity=()) -> int     # several rows + their activity-log rows, one transaction
list_mcp_audit(tenant_id, *, start=None, end=None, limit=100, cursor=None, api_key_id=None, status=None,
               tool=None) -> {"items", "next_cursor"}
mcp_audit_rows(tenant_id, *, start=None, end=None, api_key_id=None, status=None, tool=None, after_id=0,
               limit=500)                       # oldest first, for exports
trim_anonymous_mcp_audit(keep) -> int
# API key policy (schema 6: api_keys.scopes_json, rate_limit_per_min, expires_at; NULL = the default)
create_api_key(tenant_id, name, key_hash, prefix, created_by, *, scopes=None, rate_limit_per_min=None,
               expires_at=None); get_api_key(tenant_id, id); update_api_key(tenant_id, id, **fields)
admin_mcp_audit(*, start=None, end=None, tenant_id=None, anonymous=False, limit=100, cursor=None)   # OPERATOR ONLY
# SSO
get_sso_config(tenant_id, with_secret=False); delete_sso_config(tenant_id)
save_sso_config(tenant_id, *, issuer, client_id, secret_blob=<keep>, allowed_domains=(), enforced=False)
sso_configs_for_domain(domain)                                  # OPERATOR ONLY (login by email domain)
# calendar tokens (store only the SHA-256 hex of the token)
add_calendar_token(tenant_id, token_hash); list_calendar_tokens(tenant_id, include_revoked=False)
revoke_calendar_token(tenant_id, token_hash); revoke_calendar_tokens(tenant_id); calendar_token_tenant(token_hash)
# other
claim_nonce(nonce, expires) -> bool          # single-use tokens (OAuth state)
rotate_secret_blobs(transform) -> int        # OPERATOR ONLY (key rotation)
```

Date ranges are `[start, end)`; dates (`"2026-11-01"`) mean midnight UTC; timestamps are stored as UTC ISO text.

---

## 9. Encryption at rest

`platform.secret_box` (`app.crypto.SecretBox`): AES-256-GCM, 96-bit random nonce, blob
`v1.<key id>.<base64url(nonce || ciphertext || tag)>`, associated data `v1.<key id>.<context>`. Contexts bind a blob
to its owner: `connection_context(tenant_id, provider)`, `webhook_context(tenant_id)`, `sso_context(tenant_id)`.
A blob copied to another tenant's row doesn't decrypt.

Keys: `INTEGRATIONS_KEY` (base64 of 32 bytes; `render.yaml` has Render generate it), older keys in
`INTEGRATIONS_KEY_PREVIOUS` (decrypt only). Without `INTEGRATIONS_KEY` the key is HKDF-SHA256 of the app signing key
with a fixed info string, and that derived key stays accepted for decryption once `INTEGRATIONS_KEY` is set. A bad
key stops the app at startup. Rotate: new key in `INTEGRATIONS_KEY`, old in `INTEGRATIONS_KEY_PREVIOUS`, deploy, run
`python -m app.crypto rotate`, then drop the old key.

---

## 10. Checklist before you hand in a connector

- [ ] Every URL, scope, field and status code is from the vendor docs in `docs_url`; the rest is in `unconfirmed`.
- [ ] `required_env` holds only operator app credentials; tenant secrets are `secret=True` fields or OAuth tokens.
- [ ] All HTTP goes through `self.ctx.http()`; nothing logs a token, a secret field or a raw response body.
- [ ] `test()` is read-only and cheap; `handle_event` raises `PermanentError` for deliveries that can never succeed.
- [ ] Tests mock the vendor with its documented shapes, cover success, auth failure, a 429/5xx, and run on both
      backends; `python -m pytest -q tests` passes.
- [ ] No edits outside `app/connectors/<provider>.py` and `tests/test_<provider>.py`.

---

## 11. The integrations catalog and the Integrations page

`tradecomply/data/integrations.json` lists every product a contractor may use with TradeComply (36 entries, checked
2026-11-26) and how each fits: `integration` is `builtin` (TradeComply connects to it), `ai_assistant` (use the
product's MCP server next to TradeComply's in an AI client), `import_export` (files) or `reference`. Each entry has
`summary`, `tradecomply_role`, `uses` (each with the TradeComply `tools` it names), `mcp.servers` (`url` or
`url_template`, `confirmed`, `transport`, `auth`, `auth_needed`, `headers` with `${ENV}` references only, `command`
for local servers, `client_registration`, `default`), `gov` (US government-cloud variant: `supported` =
yes/partial/no/unconfirmed/not_applicable, with a note and link), `status`, `caveats`, `research_notes`, `sources`
and, for built-ins, `builtin` = `{panel, providers, what_it_does[, webhook_kind, accept]}`. Unconfirmed facts are
marked as such; self-reported vendor statistics are left out. `tests/test_integrations_guide.py` checks the shape,
that every named TradeComply tool exists, and that only documented endpoints count as `confirmed`.

**Your connector's card.** Set `catalog_id` to the entry's `id` (`procore`, `docusign`, `twilio`, `splunk`,
`oidc-sso`, `calendar-feed`, `microsoft-project` / `oracle-primavera`, ...). The page also matches a connector whose
`provider` is listed in the entry's `builtin.providers`; a connector matching neither appears under "More
connections on this server" (and `tests/test_integrations_catalog_api.py` names it). The `catalog` provider id is
reserved.

**What the page renders** (`#/integrations` in `app/static/app.js`; the OAuth callback's `#/settings?integration=..`
is forwarded there and its `message` shown as text): per card a panel by `builtin.panel`:

| `panel` | Renders |
|---|---|
| `connector` | The generic connector panel from `GET /api/integrations` (status, "not set up on this server" with `missing_env` for staff, `unconfirmed`, the `config_schema` form, Save / Sign in / Connect / Test / Disconnect) |
| `webhooks` | The webhook list and form for `builtin.webhook_kind` (slack, teams, generic) on `/api/integrations/webhooks` |
| `calendar_feed` | The private `.ics` link from `/api/calendar/feed` with copy and "Make a new link" |
| `schedule_import` | A file upload to the first `POST` path in `/api/openapi.json` whose path contains `schedule` and `import` or `upload` (multipart field `file`; a `{project_id}` path parameter or `project_id` query parameter gets a project picker). Until such a route exists the panel says schedule import isn't on this server yet |

A `calendar_feed` or `schedule_import` entry whose connector is also installed shows the connector panel below.

**MCP tools** (`tradecomply/integrations_guide.py`, `register(mcp)`; read-only): `integrations_for_task(task,
gov_cloud_required=False, limit=6)` and `mcp_client_config(ids, tradecomply_mcp_url=None)`. The second only adds
servers with a fixed https `url` that is `confirmed` and not a local `command`; the rest come back in `skipped`
with the reason (and the address template or install command). Without `tradecomply_mcp_url` it uses this server's
own address: the web app's (for `/api/tools/mcp_client_config`), else `APP_URL` or `RENDER_EXTERNAL_URL` + `/mcp`,
else the address the `/mcp` request came in on; over stdio with none of these the caller must pass it. The catalog
file stores only `tradecomply.mcp_path` (`/mcp`), never a server address.

## 12. Licensed professionals: handoffs and the finder

Some results need a person California law reserves the work for (attorney, engineer or architect stamp, QSD/QSP,
HERS rater, certified asbestos consultant, crane certifier and others). TradeComply says who, why (cited), when, and
links to the official public directory to find and check one. It never recommends, ranks or picks a person or firm,
never scrapes, and never works around a site's bot protection.

| File | What it is |
|---|---|
| `tradecomply/data/professionals.json` | Registry of 24 professional types: plain description, `when_required` / `not_required_when` / `optional_when` (each `confirmed` from an official page read on the check date, or `unconfirmed`), `find` (official directory links: `status` `reachable` or `blocked:<reason>`, `url_pattern_with_county` only where a filtered URL was tested), `verify_license_how`, `bring_or_ask`, `never_say`, the 58 counties and DIR's county codes |
| `tradecomply/data/handoffs.json` | Version 1, checked 2026-11-26. `handoffs`: 103 entries over 39 tools (`tool`, `test`, `professional_type_id`, `registry_type_id`, `requirement` = required / conditional / recommended / optional, `conditional_if`, `legal_basis`, `rule_ids`, plus plain `why`, `when_plain`, `legal_basis_plain`, `basis_complete`, `due_field`, `prefer_find`). `type_labels` (plain name per role), `role_find` (which link to show first for a role), `link_scopes` (pages that serve one city or district), `extra_types` (8 registry-shaped types professionals.json does not have yet, each fact from a page fetched 2026-11-26 and listed in `extra_types_sources_read`) |
| `tradecomply/professionals.py` | MCP tools and the logic below; `register(mcp)` (module `professionals` in `server.py` MODULES) |
| `tests/test_professionals.py` | Data honesty, the evaluator, counties, real scenarios, the API hook and routes, key scopes; both backends |

**MCP tools** (all read-only, closed world):

- `find_licensed_professional(county=None, professional_type=None, need=None, city=None, craft=None)`: a type id
  (handoff ids such as `professional_engineer_civil` are accepted) or a need in plain words (matched on the
  registry's `triggers`, names and descriptions; `other_possible_types` lists close runners-up). Returns
  `when_required` (each basis with `status`, `verified` and `rule_ids`), `directories` (`url`, `search_page_url`,
  `county_filtered`, `how_to_search`, `link_checked`, `status`, `only_for`, `applies_here`), `verify_license_how`,
  `bring_or_ask`, `do_not_say`, `neutral_note`. `county` is optional (links are statewide without it) but must be one
  of the 58 counties when given ("Fresno County" and "County of Fresno" are accepted; anything else is an error naming
  the closest counties). The only county-filtered link is DIR's public works apprenticeship search, and it needs a
  `craft` as well (DIR's results page redirects without one): `craft` takes a name or DIR code (`carpenter` -> 10).
- `who_must_sign(tool_name, result=None, arguments=None, county=None, city=None)`: the result as an object or JSON
  text, or the tool's arguments (the tool is then run in-process; only read-only tools, never itself). A result
  passed in may hold at most `MAX_RESULT_NODES` (25,000) values at any depth and 2,000,000 characters as text;
  the tests are walked on a worker thread, never on the server's event loop. Returns
  `handoffs` (below), `ran_tool`, `mapped_conditions` and `could_not_check` (entries whose fields the result lacks).
  When nothing fires, `note` says so and that this does not mean no licensed person is needed; relay it, never
  "no professional needed".
- `list_professional_types()`: every type with `also_called` ids, `directory_status` (`checked`, `unchecked` = the
  official pages could not be opened from the research network, `none` = no public directory exists) and its links,
  plus the counties.

**Handoff item** (the UI contract): `type_id`, `name`, `why`, `legal_basis`, `verified`, `find_url`,
`county_filtered` are always present; also `find_label`, `find_owner`, `find_link_checked`, `check_how`,
`requirement`, `requirement_label` ("Required by law", "Required by law if this applies", "Suggested, not required by
law", "Optional"; required and conditional add "(legal basis not yet verified)" when `verified` is false), `when`,
`due_date` (an ISO date from the result, e.g. the lien foreclosure date), `rule_ids`, `role_id` (unique in a list),
`handoff_id` and `also` (other reasons the same result needs the same person). One item per person, required first.
`type_id` is the finder's id (pass it to `/api/professionals/find?type=`); `find_url` is `null` when no directory
applies (a role you designate, a credential checked on the card, or a city page for a job elsewhere).

**`verified`** is true only when the entry cites rule ids, every one is `confirmed` or `corrected` in
`tradecomply/data/rulebook.json` (read live, so a rulebook rebuild changes it), and `basis_complete` is true (no part
of the legal basis is marked not yet confirmed). A suggested professional can be verified (its cited rule is
confirmed) while still not required by law: always show `requirement_label` with it.

**The web app hook** (`app/api.py` `attach_handoffs`): after a successful `POST /api/tools/{name}`, the response gets
`"handoffs": [...]` next to `structured` when any apply. Nothing else in the response changes (`result_sig` still
signs `structured` exactly, so saved documents keep their provenance check), a result that needs nobody gets no
field, and an exception is logged (`tradecomply.api`) with the field left out, never an error to the caller.
`GET /api/professionals/types` and `GET /api/professionals/find?type=|type_id=|need=&county=&city=&craft=` return
the same JSON as the tools (session or API key like other `/api` routes; 422 with the message for a bad county or
type; keys with `rest_api: false` get 403 as on every non-tool route).

**Conditions** (`test`) are Python-like expressions over `result` (the tool's structured output; dict keys read as
attributes or items) and `args` (the tool's inputs). `compile_test` allows only comparisons, `and`/`or`/`not`,
generator expressions, constants, field reads, `len`/`any`/`all` and the methods `startswith`, `endswith`, `lower`,
`upper`, `strip`, `get`, `json.dumps`; names other than `result`, `args`, `json` and loop variables, dunder
attributes, other calls and arithmetic are refused. A field the result lacks raises `MissingField`: the hook skips
that entry, `who_must_sign` lists it in `could_not_check`, and the tests treat it as a failure.

**`conditional_if`** (optional, `required` entries only) is a second test of the same kind. When it holds, or cannot
be read, the item is shown as `conditional`: use it where the tool itself can leave the deciding fact open (for
example `certifications_required` marks electrical, lead or RRP work `conditional` until the building and the
employer's classes are known, and `project_setup_checklist` lists the EPA RRP child-occupied item for any pre-1978
building). Never label a person "required by law" on facts the result does not show.

**Key scopes.** When `who_must_sign` runs a tool from its arguments, the caller's API key scope applies to that tool
too: on `/api/tools/who_must_sign` through `professionals.RUN_GUARD` (set by `call_tool`), on `/mcp` from the
principal the MCP gate puts in the request scope (`tc_principal`, checked with `app.mcp_governance.scope_allows`).
A key that may not run the tool gets an error telling it to pass the tool's result instead. Over stdio there is no
key and every read-only tool may run.

**Adding or changing a handoff.** Change the entry in `tradecomply/data/handoffs.json` (keep `version` and bump it
when a field's meaning changes), with a `test` that reads only fields the tool really returns, a `why` and
`when_plain` in plain English, a `legal_basis_plain` whenever `legal_basis` has notes meant for developers, and
`basis_complete: false` when any part of the basis is not in the rulebook as confirmed. Add a real input that makes
it fire to `CASES` in `tests/test_professionals.py`; `test_every_handoff_fires_on_a_real_tool_result` runs every
entry against real tool output. A new professional type goes in `professionals.json` (preferred) or
`extra_types`; every directory link must be an official page (`status` `reachable` only if it was opened, a county
pattern only if a filtered URL was tested), and a host that refuses automated access is recorded, not bypassed.
