# TradeComply MCP server: security and governance

This page covers TradeComply's own MCP server (`/mcp` on the hosted app, `server.py` over stdio). It describes the
controls in the code, says how they hold up against the OWASP MCP Top 10 and the OWASP MCP Security Cheat Sheet, and
gives advice for customers who put TradeComply behind an MCP gateway (Workato, Arcade, MintMCP, Barndoor).

Code: `app/auth.py` (`MCPGate`, the ASGI gate in front of FastMCP), `app/mcp_governance.py` (access mode, Origin
check, key scopes, the call log, `/api/mcp/*`), `app/api.py` (`/api/keys`), `app/store.py` (schema 6: `api_keys`
policy columns, `mcp_audit_log`). Tests: `tests/test_mcp_governance.py`.

## Sources read for this page

| Source | What was read | How |
|---|---|---|
| OWASP MCP Top 10, version label "v0.1" (2025 list) | `tab_top10.md` and the ten `2025/MCP*.md` pages of github.com/OWASP/www-project-mcp-top-10, commit `165fe0f` (2026-07-29) | git clone of the public repo. owasp.org itself could not be reached from this build environment |
| OWASP MCP Security Cheat Sheet | `cheatsheets/MCP_Security_Cheat_Sheet.md` of github.com/OWASP/CheatSheetSeries, commit `05861f2` (2026-11-25) | git clone; also Context7's index of the same file |
| Render default environment variables | render.com/docs/environment-variables: "`RENDER`: This value is always `true`." | Context7's index of Render's docs |
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
| `ip` | the client address (first `X-Forwarded-For` entry behind Render's proxy; behind a gateway this is the gateway) |
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
  get 1,200 a minute.
- **Expiry.** Optional (`expires_in_days` or `expires_at`, at most 5 years). An expired key gets 401 everywhere.

The gate enforces the scope on every `tools/call` before the tool runs: a call outside it gets a JSON-RPC error
(`code -32003`, `data: {"tool": ..., "reason": "not_in_key_scope"}`) and a `denied` row. `tools/list` shows a
scoped key only the tools it may call (a convenience for the assistant; the call check is the enforcement).
`POST /api/tools/{name}` applies the same scope (403).

API keys can't create, change or revoke keys, so a narrow key can't mint itself a wider one. Creating, changing and
revoking keys is recorded in the activity log (`api_key.create`, `api_key.update` with the scope before and after,
`api_key.revoke`).

### Other checks in the gate

- **Origin.** A request with an `Origin` header from another site gets 403. Allowed: this server's own origin
  (`APP_URL` or the request's host), `MCP_ALLOWED_ORIGINS`, and `http://localhost`/`127.0.0.1` in local mode.
  Requests without `Origin`, which is every non-browser MCP client and every gateway, are not affected.
- **Bodies the gate can't read fail closed.** A POST body that is not valid JSON (or nests too deeply) gets a
  JSON-RPC parse error (400) and is never passed on, so a call can't slip past the scope check because two JSON
  parsers disagree.
- **Size caps.** 4 MB request body, at most 20 tool calls per request.
- **Tool definition hashes.** `GET /api/mcp/tool-hashes` returns the SHA-256 of every tool definition as `tools/list`
  sends it (name, title, description, inputSchema, outputSchema, annotations; canonical JSON) and a manifest hash,
  so a reviewer or gateway can pin the reviewed definitions and notice any change.

## Self-assessment against the OWASP MCP Top 10

Status: **Met** (done in code or configuration), **Partial** (done in part, or only if the customer configures it),
**Not met** (with the plan). Items that are about MCP clients or a customer's own estate are marked as such.

### MCP01: Token Mismanagement and Secret Exposure

| Check (from OWASP MCP01) | Status | What TradeComply does / plan |
|---|---|---|
| No hard-coded tokens or keys in configuration | Met | `SECRET_KEY` and `INTEGRATIONS_KEY` come from the environment (render.yaml generates them); integration secrets are AES-256-GCM encrypted per company (`app/crypto.py`); tests use fake values |
| Logs and telemetry don't record full prompts, arguments or secrets | Met | MCP arguments are stored only as SHA-256; tokens, keys and OAuth `code`/`state` are redacted from the access log. Web-app tool runs (`/api/tools`) still keep summarized arguments (strings up to 300 characters); plan: the same fingerprint there |
| API keys are not stored in a readable form | Met | Shown once, stored as SHA-256 only |
| Short-lived, scoped tokens; renewal per session | Partial | Keys are long-lived bearer tokens. They now have a tool scope and an optional expiry; nothing forces renewal. Plan: MCP authorization (OAuth 2.1) with short-lived access tokens |
| Tokens bound to the agent or session | Not met | A key works from anywhere until revoked or expired. Plan: optional IP allow-list per key; OAuth with audience-bound tokens |
| Revoke and rotate on suspected exposure | Partial | Revocation is immediate (one click, or automatic when the key's creator is removed). Rotation is manual: create a new key, update the assistant, revoke the old one |
| No secrets in model context | Met | The tools are read-only compliance logic; none returns a credential |

### MCP02: Privilege Escalation via Scope Creep

| Check (from OWASP MCP02) | Status | What TradeComply does / plan |
|---|---|---|
| Least privilege per agent, fine-grained scopes | Met | Per-key allow-list of tools or categories; tool-calls-only keys (`rest_api: false`); default limited to read-only tools |
| Runtime enforcement of disallowed tool calls | Met | The gate checks every `tools/call`; `/api/tools/{name}` checks the same scope |
| No path for an agent to widen its own access | Met | API keys can't create, change or revoke keys |
| Every permission change tracked | Met | `api_key.create/update/revoke` rows in the activity log, with the scope before and after |
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
| Short-lived OAuth 2.1 tokens, audience validation (MCP authorization spec) | Not met | `/mcp` accepts static API keys only. Plan: the MCP authorization profile (protected resource metadata, audience-bound tokens) |
| Mutual TLS | Not met | TLS ends at Render's proxy; no client certificates. Not planned for the hosted service; a gateway can add it |
| Every authorization decision logged | Partial | Denied and rate-limited calls are logged (throttled); requests with an invalid or expired key are refused without a row, because they belong to no company. Plan: count them per IP in the application log |
| Integration with the company's identity provider | Partial | People can sign in to the web app with OIDC single sign-on (`app/sso.py`); API keys are not tied to IdP identities |
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
| 6. Authentication, transport, rate limits, Origin | Partial | Keys, TLS via Render, per-key and per-company limits, Origin check. No OAuth 2.1 profile |
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
| Workato Enterprise MCP | Genies can use external custom MCP servers with header or query-parameter token auth (one shared auth context) or OAuth2; requests to external servers are proxied through Workato (docs.workato.com/en/mcp/genies-as-mcp-clients.html) | Exact field names in the current Genie UI | Add `https://<host>/mcp` as a custom MCP server with the **header** token option, `Authorization: Bearer <key>`. TradeComply has no OAuth for `/mcp`. In token mode every Genie user shares that one key, so TradeComply's log shows the key, not the person; Workato's own logs identify users. Recipes that call `/api/tools/{name}` or `/api/subs/import` with Workato's HTTP connector should use a separate key (REST access on only if they import), stored in a Workato connection, never in recipe text |
| Arcade MCP Gateways | A gateway has one URL, `https://api.arcade.dev/mcp/<gateway-slug>`, over Streamable HTTP; remote servers can be added to a project ("add remote servers" guide); Arcade recommends fewer than 80 tools per gateway; Arcade warns its own API key is not limited to one gateway | Whether Arcade can send a static `Authorization: Bearer <TradeComply key>` to a remote server, or needs OAuth | Test the remote-server flow with a scoped key first. TradeComply's tools (68 when this was written) fit under 80; a scoped key also shrinks what the gateway lists. Use separate TradeComply keys for separate gateways (for example estimating and payroll) |
| MintMCP (renamed Mintive, Oct 2026) | Console flow: Add Connector, remote server, server URL, auth mode (a bearer token if the server needs one), Connect, then add to a Virtual MCP; MintMCP brokers per-user credentials to remote connectors (docs.mintmcp.com / mintmcp.com/docs) | Per-user brokering of a static bearer token in practice; the URL format of a Virtual MCP | Use bearer-token mode. If per-user brokering works, give each member their own TradeComply key so TradeComply's log names each one; otherwise one scoped key per Virtual MCP. Virtual MCP bundles and TradeComply key scopes are two layers: keep both narrow |
| Barndoor AI | Barndoor markets a governed endpoint with per-tool-call policies and its own per-agent credentials (vendor marketing, self-reported) | Whether custom remote MCP servers such as TradeComply's can be added at all; the endpoint format; any docs | No public documentation was found. Don't plan on Barndoor for TradeComply until Barndoor confirms custom remote servers; then apply the general rules above |

Also listed in the research and not a gateway: Bulwark Agent Security is a small beta Python library (in-process
guardrails), not a network proxy, and is not recommended for production.

## Open items

In order of value: OAuth 2.1 for `/mcp` (short-lived, audience-bound tokens); pinned, hashed dependencies with an
SBOM; tamper-evident log rows (hash chain) and anomaly alerts; session-to-key binding; a company-wide key lifetime;
labelling echoed free text in tool output; the same argument fingerprints for web-app tool runs.
