# TradeComply MCP server: security and governance

This page covers TradeComply's own MCP server (`/mcp` on the hosted app, `server.py` over stdio). It describes the
controls in the code, says how they hold up against the OWASP MCP Top 10 and the OWASP MCP Security Cheat Sheet, and
gives advice for customers who put TradeComply behind an MCP gateway (Workato, Arcade, MintMCP, Barndoor).

Code: `app/auth.py` (`MCPGate`, the ASGI gate in front of FastMCP), `app/mcp_governance.py` (access mode, Origin
check, key scopes, the call log, `/api/mcp/*`), `app/api.py` (`/api/keys`), `app/oauth.py` ("Sign in with
TradeComply": the OAuth 2.1 authorization server for MCP clients such as Claude), `app/store.py` (schema 6: `api_keys`
policy columns, `mcp_audit_log`; schema 7: the OAuth tables). Tests: `tests/test_mcp_governance.py`,
`tests/test_oauth.py`.

## Sources read for this page

| Source | What was read | How |
|---|---|---|
| OWASP MCP Top 10, version label "v0.1" (2025 list) | `tab_top10.md` and the ten `2025/MCP*.md` pages of github.com/OWASP/www-project-mcp-top-10, commit `165fe0f` (2026-07-29) | git clone of the public repo. owasp.org itself could not be reached from this build environment |
| OWASP MCP Security Cheat Sheet | `cheatsheets/MCP_Security_Cheat_Sheet.md` of github.com/OWASP/CheatSheetSeries, commit `05861f2` (2026-11-25) | git clone; also Context7's index of the same file |
| Render default environment variables | render.com/docs/environment-variables: "`RENDER`: This value is always `true`." | Context7's index of Render's docs |
| MCP authorization | The MCP specification's authorization pages for 2025-06-18, 2025-11-25 and 2026-07-28 (and the 2025-11-25 transports page), OAuth 2.1 (draft-ietf-oauth-v2-1, the working group's editor copy), the Client ID Metadata Document draft, Claude's connector docs (claude.com/docs/connectors/building: authentication, lazy authentication, troubleshooting, testing; "add an unlisted connector"; support article 11175166), Anthropic's published IP ranges, and the `mcp` 2.3.0 Python SDK's OAuth client and reference server | Read 2026-11-26. modelcontextprotocol.io, rfc-editor.org and datatracker.ietf.org could not be reached: the spec pages were read from the specification's own source files, and the RFCs themselves (6749, 6750, 7009, 7591, 7636, 8252, 8414, 8707, 9207, 9728) were not read, so rules taken only from them are marked unconfirmed below |
| Gateway facts | TradeComply's research entries for Workato, Arcade, MintMCP and Barndoor | Search extracts of the vendors' official pages; the vendor sites could not be fetched directly. Every gateway statement below is marked with what is confirmed and what is not |

One naming difference in the OWASP source: the Top 10 list calls MCP06 "Prompt Injection via Contextual Payloads",
and the MCP06 page itself is titled "Intent Flow Subversion". Both are covered under MCP06 below.

## What changed, and the live behavior change

**On Render, `/mcp` now needs an API key unless `MCP_PUBLIC_DEMO=1` is set.** This changes the live behavior of the
hosted server at the next deploy: anyone calling `/mcp` without a key (the anonymous public demo) gets HTTP 401.
`MCP_REQUIRE_KEY=0`, which kept the demo open before, no longer opens it on Render; the server logs a warning at
startup if it is set. API keys that already exist keep working, with the default scope (every read-only tool).

| Setting | Result |
|---|---|
| `MCP_REQUIRE_KEY=1` | Key always required (wins over `MCP_PUBLIC_DEMO`) |
| `MCP_PUBLIC_DEMO=1` | Anonymous public demo, anywhere (audited, 60 tool calls/min per IP, 300/min in total) |
| On Render (`RENDER` set), neither of the above | Key required |
| Not on Render (a laptop, `python -m app.local`, tests), neither of the above | Anonymous calls allowed, as before |

To keep the anonymous demo on Render, set `MCP_PUBLIC_DEMO=1` in the service's environment (render.yaml now sets it
to `0`). `GET /api/config` reports `mcp_require_key` and `mcp_public_demo`; the startup log line says which rule
decided.

The 401 answer is a JSON-RPC error that tells the caller how to get a key:

```json
{"jsonrpc": "2.0", "id": null, "error": {"code": -32001,
  "message": "An API key is required for this MCP server. An owner or office user creates one in TradeComply under Settings > AI assistant (MCP) > Create API key, and the AI assistant sends it as 'Authorization: Bearer <key>'.",
  "data": {"get_a_key": "https://<APP_URL>/#/settings", "header": "Authorization: Bearer <API key>",
           "where": "TradeComply > Settings > AI assistant (MCP) > Create API key (owner or office user)"}}}
```

It comes with `WWW-Authenticate: Bearer realm="tradecomply"`. Check a deploy with:

```
curl -i -X POST https://<host>/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
```

## The controls

### The MCP call log

Every `tools/call` that reaches the gate gets one row in `mcp_audit_log`:

| Field | Value |
|---|---|
| `tenant_id` | the company of the API key (NULL for an anonymous demo call) |
| `api_key_id` | the key used |
| `tool` | the tool name as sent |
| `args_sha256` | SHA-256 of the arguments as canonical JSON: `json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, UTF-8 |
| `status` | `ok`, `error` (the tool or server returned an error), `denied` (outside the key's scope, not run), `rate_limited` (not run) |
| `duration_ms` | from the gate passing the request on to the last byte of the answer (NULL for denied and rate-limited calls) |
| `ip` | the client address: the `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` points at (on Render `2`, the address Cloudflare saw, which the caller can't set); with `0` the first entry, which the caller chooses; behind a gateway this is the gateway |
| `created` | when the row was written (at most moments after the call) |

The arguments themselves are never stored: assistants pass payroll CSVs, employee names and wages through them. The
hash lets a customer check whether a given payload was sent. It is a fingerprint, not encryption: an argument with
few possible values, such as a 6-digit license number, can be found by hashing candidates.

The activity log (`/api/audit`, Settings > Activity log) keeps its row for each call that ran, now with the argument
names and the same hash instead of the values: `{"fields": ["license_number"], "args_sha256": "..."}`.

The rows are written after the last byte of the response has been sent, on a worker thread, in one transaction, so
the database round trip adds nothing to a call's latency (`test_audit_rows_are_written_after_the_response_is_sent`).
A failed write is logged and never fails the call. At most 5,000 rows wait for the database at once; beyond that new
rows are dropped and the count is logged at ERROR level. Denied and rate-limited calls are logged at most 30 rows a
minute per key or IP, so a flood of refused calls can't flood the database. Anonymous rows are capped at the newest
50,000.

Owners see the log in Settings > AI assistant call log (MCP) (filters: key, result, dates; "Show older" pages back)
and download it there as CSV or JSON. API:

- `GET /api/mcp/audit?start=&end=&key_id=&status=&tool=&cursor=&limit=` (owner): newest first, `[start, end)` in UTC,
  `next_cursor` for the next page. Each row carries the key's name and prefix.
- `GET /api/mcp/audit/export?format=csv|json&...` (owner): the same filters, oldest first, at most 100,000 rows, 20
  downloads per 10 minutes per company. CSV cells that start with `= + - @`, tab or CR get a leading `'` (OWASP CSV
  injection guidance). Each download is itself recorded in the activity log (`mcp_audit.export`).

### Per-key tool scopes, rate limits and expiry

Each API key has a policy (`PATCH /api/keys/{id}`, or Settings > API keys > Edit access):

- **Tool scope.** Default (NULL): every read-only tool, meaning every tool whose `annotations.readOnlyHint` is true;
  today that is every tool (68 when this was written). Or an allow-list of tool names and/or categories (the tool's module, as `/api/tools` and
  `GET /api/mcp/policy` group them). A category grants only the read-only tools in it; a tool that changes data, if
  one is ever added, has to be named. Unknown names are refused with 422.
- **REST API access** (`rest_api`, default true). A key with `rest_api: false` can list and run tools
  (`GET /api/tools`, `POST /api/tools/{name}`, `GET /api/mcp/tool-hashes`, `/mcp`) and nothing else: no projects,
  subs, documents or imports.
- **Rate limit.** Up to 600 tool calls a minute per key (the default), lower if set. All of a company's keys together
  get 1,200 a minute. The same counters cover `/mcp` and `POST /api/tools/{name}` (a 429 with Retry-After on either),
  so a key can't get round its limit through the REST route.
- **Expiry.** Optional (`expires_in_days` or `expires_at`, at most 5 years). An expired key gets 401 everywhere.

The gate enforces the scope on every `tools/call` before the tool runs: a call outside it gets a JSON-RPC error
(`code -32003`, `data: {"tool": ..., "reason": "not_in_key_scope"}`) and a `denied` row. `tools/list` shows a
scoped key only the tools it may call (a convenience for the assistant; the call check is the enforcement).
`POST /api/tools/{name}` applies the same scope (403).

API keys can't create, change or revoke keys, so a narrow key can't mint itself a wider one. Creating, changing and
revoking keys is recorded in the activity log (`api_key.create`, `api_key.update` with the scope before and after,
`api_key.revoke`).

### Other checks in the gate

- **Origin.** A request with an `Origin` header from another site gets 403, and a server log line names the refused
  value (at most 30 a minute per value). Allowed: this server's own origin (`APP_URL` or the request's host),
  Claude's (`https://claude.ai`, `https://claude.com`: Claude's connector testing guide names strict Origin checks
  as a cause of failed connections; which value, if any, its server-side calls send is not confirmed),
  `MCP_ALLOWED_ORIGINS`, and `http://localhost`/`127.0.0.1` in local mode. `/mcp` takes no cookies, so an allowed
  origin gives a web page no ambient credentials. Requests without `Origin`, which is every non-browser MCP client
  and every gateway, are not affected.
- **Bodies the gate can't read fail closed.** A POST body that is not valid JSON (or nests too deeply) gets a
  JSON-RPC parse error (400) and is never passed on, so a call can't slip past the scope check because two JSON
  parsers disagree.
- **Size caps.** 4 MB request body, at most 20 tool calls per request.
- **Tool definition hashes.** `GET /api/mcp/tool-hashes` returns the SHA-256 of every tool definition as `tools/list`
  sends it (name, title, description, inputSchema, outputSchema, annotations; canonical JSON) and a manifest hash,
  so a reviewer or gateway can pin the reviewed definitions and notice any change.

### Sign in with TradeComply (OAuth 2.1 for MCP clients, Claude custom connectors)

`app/oauth.py` is a small OAuth 2.1 authorization server that follows the MCP authorization spec, so a customer can
add TradeComply to Claude (web, Desktop, mobile) by pasting `<APP_URL>/mcp`. API keys keep working unchanged.

It needs a stable public https address: `APP_URL`, or Render's `RENDER_EXTERNAL_URL`. Without one (or with an
`http://` one on a public host) no OAuth is offered, as for SSO: the discovery documents answer 404, `/oauth/*`
503 with "set APP_URL", and the `/mcp` 401 challenge names no `resource_metadata`. MCP requires every authorization
server endpoint to be https, and an issuer taken from a public request's `Host` or `X-Forwarded-Host` would be
whatever the caller says. Only a loopback host (`http://127.0.0.1`, a developer's machine) works without it.

How a connection is made (what Claude and the `mcp` SDK do, confirmed in their docs and code):
1. `POST /mcp` without a token gets 401 with `WWW-Authenticate: Bearer realm="tradecomply",
   resource_metadata="<base>/.well-known/oauth-protected-resource/mcp", scope="tradecomply"` (an invalid or expired
   token adds `error="invalid_token"`, so the client refreshes). Claude starts the sign-in only on a 401: with the
   key optional (`MCP_PUBLIC_DEMO=1`, or off Render) anonymous calls succeed and Claude never asks.
2. Protected resource metadata (RFC 9728) at `/.well-known/oauth-protected-resource/mcp` and at the root variant:
   `resource` `<base>/mcp`, `authorization_servers` `[<base>]`, `scopes_supported` `["tradecomply"]`.
3. Authorization server metadata (RFC 8414) at `/.well-known/oauth-authorization-server`: issuer `<base>`, S256
   only, `authorization_response_iss_parameter_supported: true` (`iss` is on every redirect, errors and denials
   included). No `openid-configuration` (clients need one of the two and try this one first) and no
   `client_id_metadata_document_supported` (Claude would then switch to Client ID Metadata Documents, which this
   server doesn't fetch yet).
4. Dynamic client registration (RFC 7591, `POST /oauth/register`): Claude registers itself as a public client on
   every new connection. Redirect URIs must be `https://` or `http://` on 127.0.0.1, [::1] or localhost, without a
   fragment, plain ASCII with a plain DNS name or IP address as the host (the consent card shows the host as the
   thing an app can't fake; `https://clаude.ai` with a Cyrillic а would read as claude.ai while the browser goes to
   `xn--clude-5ve.ai`, so an internationalized domain registers in its `xn--` form and is shown that way). At most 5
   redirect URIs of at most 512 characters. An omitted `token_endpoint_auth_method` means `client_secret_basic` (the
   RFC default): a secret is returned once and stored as a SHA-256. A registration that never gets a grant is
   deleted an hour after it was made once no sign-in request of it is pending, and after a day in any case.
5. `GET /oauth/authorize`: an unknown client or a redirect URI it didn't register gets an error page, never a
   redirect. Exact string match, except loopback URIs, which match on any port (OAuth 2.1 section 8.4.2: Claude Code
   and the SDK listen on a port chosen at sign-in). PKCE S256 is required (no `plain`), repeated parameters are
   refused, `resource` (RFC 8707) is compared in canonical form (lowercase scheme and host, no default port, no
   trailing slash) with `<base>/mcp`, unknown scopes get `invalid_scope` (`offline_access` is accepted and ignored:
   refresh tokens are always issued). These errors are shown on a page with a "Back to <app>" link carrying
   `error`, `state` and `iss`, not redirected automatically: anyone can register a redirect address, and an
   automatic redirect would make TradeComply an open redirector (MCP security considerations; OAuth 2.1 section
   7.12.2). `state` may be at most 1,024 characters. The pending request is kept for 10 minutes (only the newest 10
   per app: the endpoint needs no sign-in, so one caller can't fill the table), it is tied to this browser by an
   HttpOnly, SameSite=Lax cookie scoped to `/api/oauth/requests/` whose name and value are a MAC of the request id,
   and the browser goes to the web app, `/#/connect?request=<id>`. The id is in the URL fragment; the web app's
   calls carry it in their path, which the access log redacts (`/api/oauth/requests/[redacted]`).
6. The consent card (signed-in person only, in the browser that started the sign-in: anywhere else the request's
   API answers 403 "started in another browser", so an id someone was sent or found can't connect the waiting app
   to their company; an API key can't approve) shows the address the app returns to next to the name it
   registered, because anyone can register an app called "Claude", and warns when it only returns to this computer.
   Approve binds the request to the person's company, account and session counter (read again, and held, in the
   transaction that writes the grant, so a person removed at that moment gets none) and answers
   `{redirect_to}` (`redirect_uri?code&state&iss`); the page navigates there itself, because the CSP's
   `form-action 'self'` would block a form post that redirects to claude.ai.
7. `POST /oauth/token` (form-encoded, `Cache-Control: no-store`): the code is single use and lives 60 seconds; PKCE
   is checked; `redirect_uri` only when sent (OAuth 2.1 removed it); `resource` when sent. A code presented again
   with otherwise valid parameters revokes the connection (OAuth 2.1 section 4.1.3); a replay with a wrong verifier
   or client changes nothing, so it can't be used to cut off the real client. Access tokens (`tco_...`) last 1 hour;
   refresh tokens (`tcr_...`) rotate on every use and the connection lasts 30 days from its last refresh. An old
   refresh token presented again revokes the whole connection (reuse detection), except within 5 seconds of its
   use: a duplicate sent at the same moment (a client refreshing from two places at once) gets `invalid_grant`
   without cutting off the connection it just renewed. A longer window would let a thief who rotated a stolen
   token first keep the connection when the real app then sends it. (A retry after a lost answer can't be saved
   either way: the app never got the new token.) Expired, revoked or reused tokens get `invalid_grant`; a request
   without `grant_type` gets `invalid_request`. A public client may leave `client_id` out of a refresh (OAuth 2.1
   section 4.3.1: the refresh token names its client); a confidential one always authenticates.
8. `POST /oauth/revoke` (RFC 7009): a refresh token ends its connection, an access token only itself; 200 for
   unknown tokens. A public client may leave `client_id` out here too.

On `/mcp`, an access token acts as the person who approved it: the gate reads their company, role and email from the
users row on every request (a removed person, a password change, or SSO enforcement that signs non-owners out ends
the connection at once), checks expiry, revocation and the audience (`<base>/mcp`: a token issued under another
`APP_URL` is refused), applies the subscription check (402), the per-connection rate limit (`oauth|<id>`, 600 a
minute) plus the company limit, and the default tool scope (every read-only tool; a tool that changes data needs an
API key that names it). Each call's `mcp_audit_log` row carries `oauth_grant_id`; the MCP log view and download show
the app's name and who approved it. Access tokens are never accepted on `/api` (their audience is `/mcp`; MCP spec:
servers MUST only accept tokens issued for them).

Rate limits: Claude makes registration, token and refresh calls from Anthropic's servers (a few addresses shared by
every customer, 160.79.104.0/21), so a tight per-IP limit would throttle every customer at once. Registration:
600 an hour per IP and 2,000 an hour in total; authorize, token and revoke: 1,200 a minute per IP; tokens issued:
60 a minute per app. The per-IP key is the whole `X-Forwarded-For` chain (or, with `TRUSTED_PROXY_HOPS`, the
trusted hop): a caller chooses the first entries, not the ones the proxies append, so it can't fill the bucket of
another address such as Claude's. The per-app limit counts only tokens actually issued (a client_id is no secret,
so failed attempts naming it count against the sender's address instead). Discovery is never limited (the SDK gives
up on a 429 there). On Render `TRUSTED_PROXY_HOPS` is `2` (render.yaml; README has the measurement). Known limit
without it: a caller can still start a fresh per-IP bucket by changing its own part of the chain (and on Render the
chain's last entry changes with every request anyway), so one caller can use up the 2,000-an-hour registration cap
and block new connections until it resets; what that costs in storage is bounded (5 short redirect URIs per registration,
deleted after an hour or a day).

Storage (schema 7): `oauth_clients` and `oauth_requests` are shared tables (no company yet); `oauth_grants` (one per
approval: who, which app, where it returns, when, last used, revoked) and `oauth_tokens` (the one-time code, access
and refresh tokens, by SHA-256 only) belong to the company: exported without hashes, deleted with it, row level
security and no Data API privileges on Postgres like every other table.

People manage connections in Settings > Connected apps (`GET /api/oauth/connections`, `DELETE
/api/oauth/connections/{id}`): their own, or (owners) everyone's in the company. Connecting, denying and
disconnecting are written to the activity log (`oauth.connect`, `oauth.deny`, `oauth.disconnect`), and so is every
connection that ends any other way (`oauth.revoked` by "TradeComply (automatic)", in the same transaction, with the
reason: `refresh_token_reused` or `code_reused`, which mean a token was copied and also get a server log warning,
`revoked_by_app`, `password_changed`, `person_removed`, `sso_required`).

Not done, and why: Client ID Metadata Documents (the spec's SHOULD in 2025-11-25; dynamic registration is deprecated
in 2026-07-28 but kept for at least 12 months, and it is what Claude uses when CIMD isn't advertised); refresh tokens
bound to the client by DPoP; a per-person "consent once" memory (every new connection asks again). Unconfirmed:
whether Claude's server-side calls send an `Origin` header (claude.ai's and claude.com's are allowed; another one is
named in the server log: add it to `MCP_ALLOWED_ORIGINS`), whether Claude's callback ties `state` to the browser session (the
browser binding here stops a shared consent link, not an authorize URL an attacker sends someone), and
the `https://claude.com/api/mcp/auth_callback` callback some pages mention (with dynamic registration nothing is
hard-coded: the server matches whatever Claude registers).

## Self-assessment against the OWASP MCP Top 10

Status: **Met** (done in code or configuration), **Partial** (done in part, or only if the customer configures it),
**Not met** (with the plan). Items that are about MCP clients or a customer's own estate are marked as such.

### MCP01: Token Mismanagement and Secret Exposure

| Check (from OWASP MCP01) | Status | What TradeComply does / plan |
|---|---|---|
| No hard-coded tokens or keys in configuration | Met | `SECRET_KEY` and `INTEGRATIONS_KEY` come from the environment (render.yaml generates them); integration secrets are AES-256-GCM encrypted per company (`app/crypto.py`); tests use fake values |
| Logs and telemetry don't record full prompts, arguments or secrets | Met | MCP arguments are stored only as SHA-256; tokens, keys and OAuth `code`/`state` are redacted from the access log. Ask TradeComply's checks keep the same fingerprint. Web-app tool runs (`/api/tools`) still keep summarized arguments (strings up to 300 characters); plan: the same fingerprint there |
| API keys are not stored in a readable form | Met | Shown once, stored as SHA-256 only |
| Short-lived, scoped tokens; renewal per session | Partial | Connected apps (OAuth, "Sign in with TradeComply") get 1-hour access tokens and rotating refresh tokens. API keys are long-lived bearer tokens with a tool scope and an optional expiry; nothing forces their renewal |
| Tokens bound to the agent or session | Partial | OAuth tokens are bound to one app registration, one person and this server's `/mcp` (audience). An API key works from anywhere until revoked or expired. Plan: optional IP allow-list per key |
| Revoke and rotate on suspected exposure | Partial | Revocation is immediate (one click, or automatic when the key's creator is removed). Rotation is manual: create a new key, update the assistant, revoke the old one |
| No secrets in model context | Met | The tools are read-only compliance logic; none returns a credential |

### MCP02: Privilege Escalation via Scope Creep

| Check (from OWASP MCP02) | Status | What TradeComply does / plan |
|---|---|---|
| Least privilege per agent, fine-grained scopes | Met | Per-key allow-list of tools or categories; tool-calls-only keys (`rest_api: false`); default limited to read-only tools |
| Runtime enforcement of disallowed tool calls | Met | The gate checks every `tools/call`; `/api/tools/{name}` checks the same scope |
| No path for an agent to widen its own access | Met | API keys can't create, change or revoke keys |
| Every permission change tracked | Met | `api_key.create/update/revoke` rows in the activity log, with the scope before and after; `user.role_change` (from, to), `user.remove`, `user.invite_released`, `auth.password_changed` and `api_key.revoke` (a person's keys ended by a password change or reset); company edits (`tenant.update`, field names), `calendar.feed_rotate`, and every deleted project, sub, document and field log (`*.delete`, written in the same transaction) |
| Expiry-based access | Partial | Optional expiry per key; no company-wide maximum lifetime yet. Plan: an owner setting "keys expire after N days" |
| Per-agent identity, no shared accounts | Partial | One key per assistant or gateway is the advice, and the log attributes every call to a key; nothing stops a company sharing one key. A gateway in shared-token mode is one identity for all its users (see the gateway section) |
| Automated entitlement reviews and drift alerts | Not met | Plan: a monthly owner summary of keys (scope, last used, unused for 90 days) through the event bus |
| Separation of duties for granting permissions | Partial | Owners and office users can both manage keys; field users can't |

### MCP03: Tool Poisoning

| Check (from OWASP MCP03) | Status | What TradeComply does / plan |
|---|---|---|
| Schemas and descriptions are not fetched dynamically from remote locations | Met | Every tool is Python code in this repository (`server.py`, `tradecomply/`), registered at import; nothing is registered at run time |
| Static indicators in names, descriptions and parameter text (model-directed imperatives, sensitive paths, zero-width or bidi characters, comment-smuggled text) | Met | `test_tool_definitions_show_none_of_owasps_poisoning_indicators` scans every tool in CI |
| Content-addressable identifiers so clients can pin definitions | Partial | `GET /api/mcp/tool-hashes` publishes a SHA-256 per tool and a manifest hash; verifying them is up to the client or gateway |
| Signed schemas (JWS/COSE) | Not met | No signatures on tool definitions. Plan: sign the tool-hash manifest with a published key |
| Reviewed, approved schema changes | Partial | Changes go through the repository and its tests; signed commits and required reviews are not enforced in this repo |
| Accurate annotations | Met | All tools are `readOnlyHint: true`, `openWorldHint: false`, and none writes data. Annotations are hints to clients, not enforcement |

### MCP04: Software Supply Chain Attacks and Dependency Tampering

| Check (from OWASP MCP04) | Status | What TradeComply does / plan |
|---|---|---|
| No connectors or plugins installed at run time from the internet | Met | Connectors are modules in `app/connectors/`; no code is downloaded at run time (the only data download is the CSLB license file, from a fixed URL) |
| Version pinning, no floating references | Not met | `requirements.txt` uses minimum versions (`fastmcp>=2.12`, ...), resolved fresh on every Render build. Plan: a lock file with hashes (`pip-compile --generate-hashes`) |
| Dependency integrity checks (hashes, signatures) | Not met | Plan: `pip install --require-hashes` from the lock file |
| SBOM and dependency scanning | Not met | Plan: CycloneDX SBOM per release and `pip-audit` in CI |
| Sandbox third-party components | Partial | The service runs in a Render container; the app has no plugin sandbox of its own |

### MCP05: Command Injection and Execution

| Check (from OWASP MCP05) | Status | What TradeComply does / plan |
|---|---|---|
| No shell, `eval`/`exec` or string-built commands in tools | Met | `test_tool_code_runs_no_shell_or_dynamic_code` checks `server.py` and `tradecomply/` |
| Parameters validated before use | Met | Every tool has a JSON Schema with types, enums and ranges; `additionalProperties: false` on every tool (a test checks it), and unknown arguments are refused |
| Parameterized database queries | Met | Every query uses placeholders; table and column names come from fixed lists |
| No fetching of URLs chosen by the model (SSRF) | Met | No tool fetches a caller-supplied URL. Connector traffic goes through `app/safe_http.py`, which refuses private, loopback and metadata addresses |
| Human approval for destructive operations | Met | There are none: every tool is read-only |

### MCP06: Prompt Injection via Contextual Payloads ("Intent Flow Subversion")

| Check (from OWASP MCP06) | Status | What TradeComply does / plan |
|---|---|---|
| A poisoned result can't trigger writes through this server | Met | All tools are read-only, and a key's scope limits what an assistant can call even if its plan is subverted |
| Tool output that echoes other people's text is marked as untrusted | Not met | Some results repeat text that came from customers or subcontractors (sub names, project names, CSV notes). They are returned as data fields, not instructions, but not labelled as untrusted. Plan: label echoed free-text fields in the output schema (a tool change, so it needs its own review) |
| Intent checks, guardrail models, human approval of plans | Client side | These sit in the AI client or gateway, not in an MCP server |

### MCP07: Insufficient Authentication and Authorization

| Check (from OWASP MCP07) | Status | What TradeComply does / plan |
|---|---|---|
| No anonymous access in production | Met | On Render `/mcp` needs a key unless `MCP_PUBLIC_DEMO=1` is set on purpose |
| Every request validated on the server, not per session | Met | The key is looked up on every request; the MCP session id is never used for authentication |
| Fine-grained authorization, deny by default | Met | Per-key scopes; tools that change data are never in the default scope |
| Subscription and tenant checked per request | Met | A key of a company whose trial or subscription ended gets 402 |
| Expiration, rotation and revocation | Partial | Revocation and optional expiry; no forced rotation |
| Short-lived OAuth 2.1 tokens, audience validation (MCP authorization spec) | Met | "Sign in with TradeComply" (`app/oauth.py`): protected resource metadata, PKCE, 1-hour audience-bound access tokens, rotating refresh tokens. API keys remain for clients without OAuth |
| Mutual TLS | Not met | TLS ends at Render's proxy; no client certificates. Not planned for the hosted service; a gateway can add it |
| Every authorization decision logged | Partial | Denied and rate-limited calls are logged (throttled); requests with an invalid or expired key are refused without a row, because they belong to no company. Plan: count them per IP in the application log |
| Integration with the company's identity provider | Partial | People can sign in to the web app with OIDC single sign-on (`app/sso.py`), and a connected app acts as the person who approved it after signing in that way; API keys are not tied to IdP identities |
| Origin and Host validation | Partial | Origin is checked (cheat sheet section 6). Host is checked in local mode; the hosted service relies on Render's routing |

### MCP08: Lack of Audit and Telemetry

| Check (from OWASP MCP08) | Status | What TradeComply does / plan |
|---|---|---|
| Structured log of every tool invocation with identity and timestamp | Met | `mcp_audit_log` and the activity log, both per company |
| Parameters used | Partial | Deliberate: only the SHA-256 of the arguments is kept, following MCP08's own "PII-safe logging: tokenize or mask" and the cheat sheet's "Redact secrets and PII from logs". The cheat sheet's "Log all MCP tool invocations with full parameters" is not followed, because the arguments carry employee data |
| Session id and schema version on each row | Not met | Plan: add a hash of the MCP session id and the tool-hash manifest to each row |
| Tamper-evident, append-only storage | Not met | Rows live in the application database and the operator can change them. Plan: a hash chain over rows; meanwhile customers keep an independent copy in their SIEM |
| Forwarding to a SIEM | Partial | The Splunk HTTP Event Collector export (`app/connectors/splunk.py`) sends both logs when a company connects it; owners can also download them |
| Alerts on anomalies | Not met | Plan: events for spikes in denied calls and for a key calling a tool for the first time, through the event bus (Slack, Teams, webhooks) |
| Access to logs restricted | Met | Owners only for the MCP log and its download; tenant-scoped queries |
| Retention policy | Partial | Company rows are kept until deleted with the company; anonymous rows are capped at the newest 50,000. Plan: a retention setting per company |
| Logging never slows or breaks the service | Met | Written after the response; failures are logged and never fail the call; the backlog is bounded and dropped rows are counted |

### MCP09: Shadow MCP Servers

| Check (from OWASP MCP09) | Status | What TradeComply does / plan |
|---|---|---|
| Secure-by-default configuration | Met | Key required on Render, rate limits, audit log and Origin check on by default |
| Local servers not exposed on the network | Met | `python -m app.local` binds `127.0.0.1` and refuses other Host headers; `python server.py` uses stdio |
| A customer can inventory every TradeComply endpoint | Customer side | Register the hosted `/mcp` and any local install with your security team (the endpoints are listed in README.md) |

### MCP10: Context Injection and Over-Sharing

| Check (from OWASP MCP10) | Status | What TradeComply does / plan |
|---|---|---|
| No shared or persistent context between users or tenants | Met | The tools keep no memory between calls and read no company data: they compute from their arguments, the rulebook and public CSLB data |
| Tenant isolation of stored data | Met | Every query a company can reach filters by `tenant_id` (the few operator-only store methods are never on a tenant route); the MCP log view is scoped to the caller's company |
| MCP sessions bound to the authenticated caller | Partial | FastMCP issues random session ids and the gate checks the key on every request, but a session id is not tied to one key. Low risk because sessions hold no company data. Plan: bind the session id to the key id in the gate |
| Over-sharing through tool lists | Met | A scoped key's `tools/list` shows only its tools |

## The OWASP MCP Security Cheat Sheet, server items

| Section | Status | Notes |
|---|---|---|
| 1. Least privilege; scoped, per-server credentials | Met | Per-key scopes; advice below: a separate key per assistant or gateway |
| 2. Tool description and schema integrity; strict JSON Schema | Partial | Strict schemas (`additionalProperties: false`) and published definition hashes; no signatures |
| 3. Sandbox and isolate | Partial | Hosted in a Render container; local installs use stdio or `127.0.0.1` |
| 4. Human approval for sensitive actions | Met | No tool changes data or spends money |
| 5. Input and output validation, SSRF | Met | Schema validation; no caller-chosen URLs; SafeHTTP for connectors |
| 6. Authentication, transport, rate limits, Origin | Met | OAuth 2.1 (MCP authorization) or keys, TLS via Render, per-key, per-connection and per-company limits, Origin check |
| 7. Optional message-level integrity | Not met | Optional in the cheat sheet; not implemented |
| 10. Monitoring, logging and auditing; SIEM; redact PII | Partial | Every invocation logged with a hash instead of parameters; Splunk export; no anomaly alerts yet |
| 12. Treat tool responses as untrusted | Client side | See MCP06: echoed free text is not labelled yet |
| 8, 9, 11 (multi-server isolation, supply chain for installers, consent dialogs) | Client side | For the AI client or gateway; see MCP04 for TradeComply's own dependencies |

## Putting TradeComply behind an MCP gateway

What holds for any gateway:

1. **One TradeComply key per gateway use, named after it** ("Workato Genie", "Arcade estimating"). Give it only the
   tools that use needs (Edit access), a rate limit and an expiry. TradeComply enforces this on its side, so a
   gateway policy mistake can't widen it.
2. **Tool-calls-only keys** (`rest_api` off) for anything that only runs checks. Turn REST access on only for a key
   that must import subs or read projects.
3. **The header, never the URL.** TradeComply reads keys only from `Authorization: Bearer <key>`; a key in a query
   string would end up in proxy logs and isn't accepted.
4. **Who did it.** TradeComply's log shows the key, the tool, the time and the gateway's address (calls arrive from
   the gateway, so the `ip` column is the gateway's egress, not the person). Match rows against the gateway's own
   log by time and tool; if the gateway logs the arguments, the SHA-256 of their canonical JSON matches
   `args_sha256`.
5. **Pin what you reviewed.** Note the `manifest_sha256` from `GET /api/mcp/tool-hashes` when you approve TradeComply,
   and review again when it changes.
6. **Limits.** All traffic through one shared key counts against that key's limit (at most 600 calls a minute) and
   the company's 1,200 a minute.
7. Gateways call TradeComply server to server and send no `Origin` header, so the Origin check doesn't affect them.

Per gateway, with what is confirmed and what is not:

| Gateway | Confirmed in research (search extracts of the vendor's docs) | Not confirmed: test before relying on it | How to set it up with TradeComply |
|---|---|---|---|
| Workato Enterprise MCP | Genies can use external custom MCP servers with header or query-parameter token auth (one shared auth context) or OAuth2; requests to external servers are proxied through Workato (docs.workato.com/en/mcp/genies-as-mcp-clients.html) | Exact field names in the current Genie UI | Add `https://<host>/mcp` as a custom MCP server with the **header** token option, `Authorization: Bearer <key>` (or the OAuth2 option: TradeComply's `/mcp` supports MCP authorization; not tested with Workato). In token mode every Genie user shares that one key, so TradeComply's log shows the key, not the person; Workato's own logs identify users. Recipes that call `/api/tools/{name}` or `/api/subs/import` with Workato's HTTP connector should use a separate key (REST access on only if they import), stored in a Workato connection, never in recipe text |
| Arcade MCP Gateways | A gateway has one URL, `https://api.arcade.dev/mcp/<gateway-slug>`, over Streamable HTTP; remote servers can be added to a project ("add remote servers" guide); Arcade recommends fewer than 80 tools per gateway; Arcade warns its own API key is not limited to one gateway | Whether Arcade can send a static `Authorization: Bearer <TradeComply key>` to a remote server, or needs OAuth | Test the remote-server flow with a scoped key first. TradeComply's tools (68 when this was written) fit under 80; a scoped key also shrinks what the gateway lists. Use separate TradeComply keys for separate gateways (for example estimating and payroll) |
| MintMCP (renamed Mintive, Oct 2026) | Console flow: Add Connector, remote server, server URL, auth mode (a bearer token if the server needs one), Connect, then add to a Virtual MCP; MintMCP brokers per-user credentials to remote connectors (docs.mintmcp.com / mintmcp.com/docs) | Per-user brokering of a static bearer token in practice; the URL format of a Virtual MCP | Use bearer-token mode. If per-user brokering works, give each member their own TradeComply key so TradeComply's log names each one; otherwise one scoped key per Virtual MCP. Virtual MCP bundles and TradeComply key scopes are two layers: keep both narrow |
| Barndoor AI | Barndoor markets a governed endpoint with per-tool-call policies and its own per-agent credentials (vendor marketing, self-reported) | Whether custom remote MCP servers such as TradeComply's can be added at all; the endpoint format; any docs | No public documentation was found. Don't plan on Barndoor for TradeComply until Barndoor confirms custom remote servers; then apply the general rules above |

Also listed in the research and not a gateway: Bulwark Agent Security is a small beta Python library (in-process
guardrails), not a network proxy, and is not recommended for production.

## Open items

In order of value: Client ID Metadata Documents for "Sign in with TradeComply" (fetched through `app/safe_http.py`);
pinned, hashed dependencies with an
SBOM; tamper-evident log rows (hash chain) and anomaly alerts; session-to-key binding; a company-wide key lifetime;
labelling echoed free text in tool output; the same argument fingerprints for web-app tool runs.
