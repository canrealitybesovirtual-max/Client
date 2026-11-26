# TradeComply CA

An MCP server that gives AI agents five California construction compliance tools.

| Tool | What it does |
|---|---|
| `verify_contractor_license` | CSLB license status, classifications, expiration, workers' comp, bond |
| `estimate_permit_fees` | San Diego building permit fees from official IB-501 tables + state fees; other cities get the official calculator link |
| `lookup_safety_standard` | Cal/OSHA Title 8 rule for a hazard, federal reference, PPE |
| `calculate_lien_deadline` | Preliminary notice, lien recording and foreclosure deadlines, days left, risk |
| `check_subcontractor_compliance` | License + class + insurance + workers' comp + bond, risk rating |

**Not legal advice.** Every result has `verified: false` until the data in `tradecomply/rules.py` is checked.

## Run

```bash
pip install -r requirements.txt
python server.py                          # stdio (Claude Desktop / Claude Code)
MCP_TRANSPORT=http python server.py       # http://localhost:8000/mcp
uvicorn app.main:app --port 8000          # web app at /, API at /api, MCP at /mcp (what Render runs)
python -m app.local                       # offline mode for one company on this computer
python -m pytest -q tests
```

Add to Claude Code: `claude mcp add tradecomply -- python /path/to/server.py`

## Platform: one rules engine, three front doors

The same tools (whatever is registered on `server.mcp`; the app discovers them at runtime) are served three ways:

| | Hosted (Render) | Local / offline (one laptop) | Claude Desktop / Code over stdio |
|---|---|---|---|
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | `python -m app.local`, or double-click `TradeComply-local.command` (macOS), `TradeComply-local.bat` (Windows), `tradecomply-local.sh` (Linux) | `python server.py` |
| Who | Many companies (tenants), web app + `/mcp` | One company, `127.0.0.1` only | One person's AI assistant |
| Data | `$DATA_DIR/tradecomply.sqlite` (put `DATA_DIR` on a persistent disk) | `~/.tradecomply` (app DB, signing key, CSLB snapshot) | none (tools only) |
| Accounts | Sign up, 14-day trial, $1,000/month via Stripe | Owner account created on first run; login printed and saved to `~/.tradecomply/LOCAL-LOGIN.txt`; browser opens already logged in | none |
| MCP | `https://<host>/mcp`, API key required on Render unless `MCP_PUBLIC_DEMO=1` ([docs/mcp-security.md](docs/mcp-security.md)) | `http://127.0.0.1:<port>/mcp` | stdio |
| License data | `CSLB_AUTO_DOWNLOAD=1`: bundled snapshot on boot, refreshed daily | bundled snapshot (`seed/`) loaded on first run; `--update-licenses` refreshes when online | `data/cslb.sqlite` if present, else demo fixtures |

The launchers create `.venv`, `pip install -r requirements.txt` (internet needed once) and run `python -m app.local`.
Local mode options: `--port`, `--data-dir`, `--company "Name"`, `--no-browser`, `--update-licenses`.

### Web app (`/`)

Vanilla JS single-page app in `app/static` (no build step, no CDN). Mobile-first, keyboard accessible, installable
(PWA). Screens: sign up / log in, 5-step onboarding (license health card, sub list upload with risk report, first
project with lien calendar, Cal/OSHA questionnaire, connect an AI agent), Dashboard (red/amber/green tiles, deadlines,
sub table, calendar download), Projects, Subcontractors (CSV import), Tools (forms generated from each tool's JSON
schema; results show "Verified against official source on <date>" or "Not yet verified", citations, disclaimer,
Save as document, Print / PDF), Documents, Field (toolbox talks, inspections, incident reports with on-screen
signatures; saved to IndexedDB offline and synced via `/api/field_logs/sync`), Settings (company, password, team
invites, API keys with per-key tool scope, rate limit and expiry, MCP config snippets, calendar feed, activity log,
the owner's MCP call log with CSV/JSON download, billing). The service worker caches the app
shell plus `/api/tools`, `/api/me`, `/api/config` and `/api/projects` (network first) so the Field screen opens offline.

Tool descriptions and parameter docs are written for AI agents as well as people, so the web app rewrites them for
the screen: the first sentence of a description shows (the rest under "What this check covers"), hints use the form's
labels instead of `snake_case` names, JSON shapes and "ISO date" are dropped, "as of" dates fold away, clock times get
a time picker, and results hide rule ids (they're listed under Sources), format money, dates and enum values, and fold
long item-by-item detail. Saved documents get the same treatment (`result_to_markdown`). A tool whose inputs match a
project's details (name, city, county, address, owner, contract price, furnishing/completion dates, claimant role) gets
a "Fill in from a project" picker, and the saved result is linked to that project.

Roles: **owner** (everything, incl. billing, team and keys), **office** (projects, subs, documents, tools, keys),
**field** (read projects/subs/documents, run tools, create field logs). API keys act with the office role, limited to
their tool scope (default: every read-only tool); a key with `rest_api: false` can only list and run tools. Keys
can't create, change or revoke keys.

### API (`/api/*`, reference at `/api/docs`, OpenAPI JSON at `/api/openapi.json`)

| Area | Endpoints |
|---|---|
| Auth | `POST /api/auth/signup` `login` `logout` `password`; `GET /api/auth/local?token=` (local mode one-time link) |
| Account | `GET /api/me`, `GET /api/config`, `PATCH /api/tenant`, `GET/POST /api/users`, `PATCH/DELETE /api/users/{id}`, `GET/POST /api/keys`, `PATCH /api/keys/{id}` (name, tool scope, rate limit, expiry), `DELETE /api/keys/{id}` (revoke), `GET /api/audit`, `GET /api/audit/{id}` |
| MCP governance | `GET /api/mcp/audit` (owner: MCP call log, filters, paging), `GET /api/mcp/audit/export?format=csv\|json` (owner), `GET /api/mcp/policy` (access mode, limits, tool categories), `GET /api/mcp/tool-hashes` (SHA-256 of every tool definition, for pinning). See [docs/mcp-security.md](docs/mcp-security.md) |
| Tools | `GET /api/tools` (name, description, input schema, module), `POST /api/tools/{name}` (JSON body = arguments; runs in-process through `fastmcp.Client(server.mcp)`, writes an audit row) |
| Records | `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}`, `GET /api/projects/{id}/deadlines`; `GET/POST /api/subs`, `GET/PATCH/DELETE /api/subs/{id}`, `GET /api/subs/{id}/check`, `POST /api/subs/import` (CSV: multipart `file`, `text/csv` body or JSON `{"csv": ...}`); `GET/POST /api/documents`, `POST /api/documents/from_result`, `GET/PATCH/DELETE /api/documents/{id}`; `GET/POST /api/field_logs`, `GET/DELETE /api/field_logs/{id}`, `POST /api/field_logs/sync` (idempotent by `client_uuid`) |
| Compliance view | `GET /api/dashboard`, `GET /api/license/{number}`, `GET /api/calendar.ics` (cookie, API key, or the private feed link from `GET /api/calendar/feed`; rotate with `POST /api/calendar/feed/rotate`) |
| Billing | `GET /api/billing/status`, `POST /api/billing/checkout`, `POST /api/billing/portal`, `POST /api/billing/webhook` |

Use an API key from a script: `curl -H "Authorization: Bearer tck_..." https://<host>/api/dashboard`.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | unset (SQLite) | `postgres://` or `postgresql://` URL: keep customer data in PostgreSQL instead of SQLite. See [Durable storage (Postgres)](#durable-storage-postgres) |
| `DATA_DIR` | `./data` | SQLite app database (when `DATABASE_URL` is unset) and generated signing key. When set (and `LICENSE_DB` isn't), CSLB data goes here too |
| `SECRET_KEY` | generated: in the database with Postgres, else in `DATA_DIR/secret_key` | Signs session cookies, CSRF tokens, calendar links and tool results. At least 32 characters (shorter values are refused at startup). Set it on Render (render.yaml generates one), use Postgres, or keep `DATA_DIR` on a persistent disk so sessions survive deploys |
| `MCP_REQUIRE_KEY` | off, but on Render a key is required anyway | `1`: `/mcp` always needs `Authorization: Bearer <API key>` of an active or trialing account. On Render (`RENDER` is set) `/mcp` needs a key unless `MCP_PUBLIC_DEMO=1`; `MCP_REQUIRE_KEY=0` alone no longer opens it there (a startup warning says so). Elsewhere (a laptop, local mode) off means anonymous calls are allowed |
| `MCP_PUBLIC_DEMO` | off | `1`: run `/mcp` as an anonymous public demo even on Render: every tool free to anyone, audited and throttled (60 tool calls/min per IP, 300/min in total). Ignored when `MCP_REQUIRE_KEY=1`. **Changing to the new default (no `MCP_PUBLIC_DEMO`) changes the live behavior of a Render deploy: anonymous `/mcp` callers get 401** |
| `MCP_ALLOWED_ORIGINS` | unset | Comma-separated browser origins (`https://app.example.com`) allowed to call `/mcp` besides this server's own; a request with any other `Origin` header gets 403. Requests without `Origin` (every non-browser MCP client) are never refused for that. `*` turns the check off |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` | unset | Enable checkout and the billing portal. The price is the $1,000/month recurring price from the Stripe dashboard |
| `STRIPE_WEBHOOK_SECRET` | unset | Verifies `/api/billing/webhook` (send `checkout.session.completed`, `customer.subscription.updated`/`deleted`, `invoice.payment_failed`, `invoice.paid`) |
| `APP_URL` | `RENDER_EXTERNAL_URL` or request host | Public base URL for Stripe return links, the MCP URL and calendar links, OAuth redirect URIs (`<APP_URL>/api/integrations/<provider>/oauth/callback`), links in Slack/Teams alerts, and the TradeComply address the `mcp_client_config` tool puts in generated assistant configs (over stdio with no `APP_URL` the caller must pass `tradecomply_mcp_url`). Set it before registering OAuth/SSO redirect URIs or handing out MCP and calendar links |
| `SUPPORT_EMAIL`, `SUPPORT_URL` | unset | The operator's support contact, shown to customers where they need help from whoever runs the server (billing not set up, an integration not turned on) and returned as `support_contact` by `GET /api/config`. Unset: the app says "ask whoever runs this TradeComply server". Use a business address; malformed values are ignored with a warning |
| `INTEGRATIONS_KEY` | derived from `SECRET_KEY` (HKDF) | Base64 of 32 random bytes: the AES-256-GCM key for integration secrets at rest (OAuth tokens, API keys, webhook URLs). render.yaml generates one. Older keys go in `INTEGRATIONS_KEY_PREVIOUS` (comma-separated) during a rotation; then run `python -m app.crypto rotate`. See [docs/integrations-dev.md](docs/integrations-dev.md) |
| `EVENTS_WORKER` | on | `0` turns off the background thread that delivers integration events (webhooks, connectors) |
| Connector app credentials (e.g. `PROCORE_CLIENT_ID`) | unset | Each connector lists its own `required_env`; it shows as "not set up on this server" until they are set |
| `COOKIE_SECURE` | auto | `1` forces the Secure cookie flag (it is set automatically when the request is https / `X-Forwarded-Proto: https`) |
| `CSLB_AUTO_DOWNLOAD` | off | `1`: build CSLB data from the bundled snapshot on boot and refresh it daily |
| `LICENSE_DB` | `data/cslb.sqlite` | CSLB SQLite path |
| `DATABASE_CONNECT_TIMEOUT`, `DATABASE_CONNECT_ATTEMPTS`, `DATABASE_POOL_MAX`, `DATABASE_POOL_IDLE` | 5 s, 3, 10, 4 | Postgres only: connect timeout per attempt, startup attempts, connections in use per instance, idle connections kept |

No operator or customer data is built in: every company signs up and enters its own company, license, contacts and
credentials, and everything about the operator (public address, support contact, Stripe, Twilio and other connector
app keys) comes from the environment variables above. The repository holds no server address (the integrations
catalog stores only the `/mcp` path; each install points its users at itself), no contact details and no demo phone
numbers other than 555-01xx. `tests/test_no_operator_data.py` checks this; to also scan every tracked file for your
own identifiers without writing them into the repo, put them one per line in a file outside the checkout and run
`TC_FORBIDDEN_STRINGS_FILE=/path/to/that/file python -m pytest -q tests/test_no_operator_data.py`.

Billing states: `trialing` (14 days) and `active` have full access; `past_due` keeps access with a banner while
Stripe retries the card; an ended trial or `canceled` gets HTTP 402 on tool runs, the dashboard and the calendar
(data stays readable). Without Stripe settings the billing endpoints return a 503 saying online billing is not set
up (the missing setting names go to the server log, not to customers).

### Durable storage (Postgres)

Without `DATABASE_URL`, accounts, projects, subs, documents, field logs and the audit log are in SQLite at
`DATA_DIR/tradecomply.sqlite`. On a Render instance with no persistent disk (the free plan) that file is lost on
every redeploy and spin-down: `/health` then reports `"storage": "sqlite", "storage_persistent": false` and the log
warns at startup. Set `DATABASE_URL` (in render.yaml it is declared with `sync: false`, so you paste it in the
dashboard) and the data lives in PostgreSQL; `/health` reports `"storage": "postgres", "storage_persistent": true`.

- **Supabase.** In the project dashboard click **Connect** and copy the **Session pooler** string
  (`postgres://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres`), which Supabase
  recommends for persistent backends on IPv4-only networks, or the **Transaction pooler** string (same host, port
  `6543`). Both pooler endpoints are IPv4 on every Supabase plan; the direct `db.<project-ref>.supabase.co`
  endpoint is IPv6 unless the project has the IPv4 add-on, so from Render use a pooler (IPv4). The app works in either
  pooler mode: prepared statements are off and nothing is kept in the session between transactions. Put in the real
  password, percent-encoding any `@ : / % # ?` in it (`@` becomes `%40`). Pick the Supabase region closest to the
  Render region; every query is a network round trip.
- **Supabase Data API.** Supabase serves the `public` schema (where these tables are created) over its REST and
  GraphQL Data API, and a table there without row level security can be read, changed and deleted by anyone with
  the project URL (Supabase docs: "Row Level Security" guide and Database Advisor lint
  `0013_rls_disabled_in_public`). These tables hold password and API-key hashes, every company's data and, without
  `SECRET_KEY`, the session signing key. So the schema migration turns on row level security for every table with
  no policies (the app connects as the tables' owner, which RLS doesn't restrict) and revokes every privilege of
  the `anon` and `authenticated` roles on them where those roles exist. If you don't use the Data API at all, the
  same lint page suggests also removing `public` from **Exposed schemas** in the project's API settings. Check the
  project's Security Advisor after the first deploy.
- **Render Postgres.** Create a database in the same region as the web service and paste its `postgresql://`
  connection URL from the database's page in the dashboard. Unverified: whether Render's internal URL accepts TLS
  (render.com's docs could not be read when this was written). If startup fails with a TLS error, see **TLS** below.
- **TLS.** Connections use `sslmode=require` unless the URL (or `PGSSLMODE`) sets `sslmode`, e.g.
  `?sslmode=verify-full` to also check the certificate, or `?sslmode=disable` for a local test server without TLS.

What happens at startup: the schema is created or migrated in one transaction under a Postgres advisory lock
(table `schema_version`; two instances starting together migrate once). If `DATABASE_URL` is set but the database
can't be reached (3 attempts, 5-second connect timeout), the server logs a CRITICAL line naming
`user@host:port/db` (never the password) and exits, so the deploy fails; it never falls back to a throwaway SQLite
file. A connection the pooler or server dropped while idle is replaced on the next query; if the database stops
answering later, `/health` returns 503 with `"ok": false` (no details) and the log names the storage. If
`SECRET_KEY` is unset, a key is generated once and stored in the table `app_secrets`, so logins survive restarts
and every instance shares it; anyone who can read that table can sign sessions, so set `SECRET_KEY` yourself if others can read the
database. Local mode (`python -m app.local`) always uses SQLite and ignores `DATABASE_URL`. Existing SQLite data is
not copied to Postgres automatically.

Tests: `python -m pytest -q tests` runs everything on SQLite. With
`TC_TEST_DATABASE_URL=postgresql://user:pw@host:5432/dbname` (a database the tests may write to; each test gets its
own schema, dropped afterwards) `tests/test_platform.py` also runs on PostgreSQL and `tests/test_store_pg.py` tests
the Postgres plumbing (pooler settings, advisory-lock migrations, reconnects, the shared key, row level security).
To also check the Supabase role revokes, create the roles on the test server first (`CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN; GRANT anon, authenticated TO <test user>;`).

How both backends are kept the same: every query on a company's data filters by `tenant_id`; sign-up creates the company and its owner
in one transaction (a double-submitted form gets a 409, never an ownerless company); the email is unique ignoring
case; a NUL character in text, which Postgres can't store, is saved as U+FFFD instead of failing; ties in sorted lists
are ordered by id; settings, calendar-link rotation and project edits are read-modify-writes under a row lock; a
Stripe status is applied only if its event is not older than the last one applied, checked inside the UPDATE. On
Postgres a statement error caught inside a transaction is reported (`TransactionAborted`) instead of the whole
transaction vanishing at commit. Database calls never run on the event loop.

### Security notes

scrypt password hashes; HMAC-signed HttpOnly, SameSite=Lax session cookies with a 14-day expiry, bound to the exact
user row (a removed user's cookie can't log in as a later user who gets the same id) and revoked server-side on
logout (a password change ends every other session); double-submit CSRF token on cookie-authenticated writes; API
keys shown once and stored as SHA-256, revoked automatically when their creator is removed or moved to the field
role; rate limits on login (5 failures per IP+email, 20 per email in 15 minutes; lockouts survive key flooding),
password changes, sign-ups and `/mcp` tool calls; every query is scoped by tenant; Stripe signatures checked with a
5-minute tolerance and constant-time compare, events de-duplicated and applied in order (a late, older event can't
undo a cancellation; an old subscription ending can't cancel the current one); request bodies capped at 8 MB by
bytes actually received (chunked uploads included); CSP (no inline script, no CDN), HSTS over https and other
security headers; gzip only on public, secret-free responses (app shell, static files, the tool list), never on
account data or keys; the markdown renderer never emits markup from input; "Verified" on a saved document only comes
from a tool result signed by this server, and editing the text drops it; dates outside 1900-2200 are rejected;
calendar and login tokens are redacted from the access log; local mode rejects any Host header other than
`127.0.0.1`/`localhost` (DNS rebinding). `/mcp` tool calls are logged in `mcp_audit_log` with the SHA-256 of the
arguments, never the arguments, and their activity-log rows keep only the argument names and that hash; rows are
written after the response has been sent. Tool runs from the web app (`/api/tools/{name}`) keep arguments in
summarized form (long strings such as a payroll CSV are stored as length + hash). Anonymous rows are capped at the
newest 50,000 in each log. MCP governance (scopes, limits, Origin check, OWASP MCP Top 10 self-assessment):
[docs/mcp-security.md](docs/mcp-security.md).

### Known limits

- Rate limits and the tool-list cache are in memory: run one instance. Customer data can be in Postgres
  (`DATABASE_URL`), but scaling out also needs shared rate limits (e.g. Redis). Database calls run on worker
  threads (up to `DATABASE_POOL_MAX` connections at once), but each is still a network round trip, so keep Postgres
  in or near the service's region.
- Per-IP limits use the first `X-Forwarded-For` entry, which a client can spoof; per-email and global limits back
  them up. Sessions are stateless signed cookies (revoked on logout); API keys expire only when given an expiry date.
- Team invites set "must change password", but the server doesn't force the change before other use.
- No email: team invites show a temporary password to share; there is no password-reset email yet (an owner can
  remove and re-invite a user).
- Toolbox-talk cadence counts Monday-Friday and ignores holidays. Lien items for public projects are not computed
  (stop payment notices and bond claims have their own rules).
- The service worker only caches what the user has opened; the Field screen needs one online visit per device.
- Signatures are on-screen drawings stored as PNG, not certified e-signatures.

## Before anyone relies on it (verification checklist)

Each item: check the source, update the value, set `verified: True` and `checked_on`.

1. **Lien law** (`CA_LIEN`): Civ. Code §8204, §8412, §8414, §8182, §8460.
2. **Cal/OSHA table** (`SAFETY_TABLE`): each Title 8 section, the 7.5 ft fall trigger, summaries.
3. **Penalties**: federal 29 CFR 1903.15(d) (January each year); add Cal/OSHA maximums.
4. **CSLB** (`CSLB`): bond amount (§7071.6), workers' comp rule (§7125).
5. **License data**: real CSLB data is built from the official Master License file (free, updated daily):
   `python -m tradecomply.cslb --download` (or set `CSLB_AUTO_DOWNLOAD=1` on the host to build on boot and refresh daily).
   Don't scrape the CheckLicense page.
6. **Permit fees**: San Diego IB-501 is loaded and verified (re-check when the city updates it; it changes several
   times a year). Los Angeles: LADBS publishes fees through its online calculator and LAMC §91.107, not a table;
   add it once those tables are transcribed. Other cities: add a schedule to `rules.py`.

## Layout

- `server.py`: MCP tools (thin)
- `tradecomply/logic.py`: all rules, pure and tested
- `tradecomply/rules.py`: every regulatory number and source, in one file to review
- `tradecomply/licenses.py`: license data sources (demo fixtures, CSV)
- `app/main.py`: the one ASGI app (FastAPI `/api`, static web app, `/mcp` mounted from `server.mcp`)
- `app/store.py` (SQLite, tenant-scoped), `app/auth.py` (accounts, sessions, CSRF, API keys, `/mcp` gate and audit),
  `app/billing.py` (Stripe), `app/api.py` (JSON API), `app/dashboard.py` (RAG dashboard, lien calendar, iCalendar),
  `app/local.py` (offline mode), `app/static/` (web app, service worker, manifest)
- `app/mcp_governance.py` (MCP access mode, Origin check, per-key tool scopes, the MCP call log, `/api/mcp/*`):
  see [docs/mcp-security.md](docs/mcp-security.md)
- `app/integrations_api.py` (`/api/integrations`), `app/connectors/` (one file per connector, auto-discovered),
  `app/events.py` (event bus and delivery worker), `app/webhooks.py` (Slack, Teams, signed generic webhooks),
  `app/safe_http.py` (SSRF-guarded outbound HTTP), `app/crypto.py` (secrets at rest): see
  [docs/integrations-dev.md](docs/integrations-dev.md)
- `tradecomply/data/integrations.json` (the integrations catalog: what each product is, how it works with TradeComply,
  MCP endpoints, government-cloud notes, sources), `tradecomply/integrations_guide.py` (MCP tools
  `integrations_for_task` and `mcp_client_config`), served at `/api/integrations/catalog` and shown on the web app's
  Integrations page
- `tests/test_platform.py`: accounts, CSRF, tenant isolation, API keys, `/mcp`, Stripe, 402, tools, dashboard, sync, CSV
