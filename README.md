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
signatures; saved to IndexedDB offline and synced via `/api/field_logs/sync`), Settings (company, password, email
confirmation and change, team invite links and owner-made password reset links, the owner's Email status, API keys
with per-key tool scope, rate limit and expiry, MCP config snippets, calendar feed, activity log, the owner's MCP call
log with CSV/JSON download, billing). Signed out: log in, sign up, Forgot password, and the pages one-time links open
(set a new password, join your team, confirm your email). An **Ask** button in the top bar opens Ask TradeComply, the
AI helper (see below), when the server has it set up. The service worker caches the app
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
| One-time links | `POST /api/auth/forgot` (`{email}`: always the same answer), `POST /api/auth/link` (`{token, purpose}`: what a link is for, without using it), `POST /api/auth/reset` (`{token, new_password}`), `POST /api/auth/invite/accept` (`{token, password, name?}`: joins and signs in), `POST /api/auth/email/confirm` (`{token}`, signed in as that person), `POST /api/auth/email/resend`, `POST /api/auth/email/change` (`{current_password, new_email}`), `GET /api/auth/email-status` and `POST /api/auth/email-test` (owner). See [Email: invites, password resets and email confirmation](#email-invites-password-resets-and-email-confirmation) |
| Account | `GET /api/me`, `GET /api/config`, `PATCH /api/tenant`, `GET/POST /api/users` (POST: an invite link), `POST /api/users/{id}/invite` (a new invite link), `POST /api/users/{id}/reset-link` (owner: a password reset link to hand over), `PATCH/DELETE /api/users/{id}` (DELETE also cancels an invite), `GET/POST /api/keys`, `PATCH /api/keys/{id}` (name, tool scope, rate limit, expiry), `DELETE /api/keys/{id}` (revoke), `GET /api/audit`, `GET /api/audit/{id}` |
| Account data | `GET /api/tenant/export` (owner, signed in: one JSON file with every record of the company, no credentials), `DELETE /api/tenant` (owner, signed in: close the account; body `confirm_company_name`, `password`). See [Company data export and account deletion](#company-data-export-and-account-deletion) |
| Connected apps (OAuth) | `GET /api/oauth/requests/{id}`, `POST /api/oauth/requests/{id}/approve`, `POST .../deny` (the consent screen, signed in), `GET /api/oauth/connections` (yours; owners see the company's), `DELETE /api/oauth/connections/{id}` (disconnect). Protocol endpoints outside `/api`: `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server`, `POST /oauth/register`, `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke`. See [Add TradeComply to Claude](#add-tradecomply-to-claude-custom-connector) |
| MCP governance | `GET /api/mcp/audit` (owner: MCP call log, filters, paging), `GET /api/mcp/audit/export?format=csv\|json` (owner), `GET /api/mcp/policy` (access mode, limits, tool categories), `GET /api/mcp/tool-hashes` (SHA-256 of every tool definition, for pinning). See [docs/mcp-security.md](docs/mcp-security.md) |
| Tools | `GET /api/tools` (name, description, input schema, module), `POST /api/tools/{name}` (JSON body = arguments; runs in-process through `fastmcp.Client(server.mcp)`, writes an audit row) |
| AI helper | `GET /api/assistant/status`, `GET/POST /api/assistant/conversations`, `GET/DELETE /api/assistant/conversations/{id}`, `POST /api/assistant/conversations/{id}/messages` (`{text, page?}`: the answer when done), `POST /api/assistant/conversations/{id}/stop`, `GET /api/assistant/usage` and `PUT /api/assistant/settings` (owner). Signed-in people only (not API keys). See [Ask TradeComply](#ask-tradecomply-the-ai-helper) |
| Records | `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/{id}`, `GET /api/projects/{id}/deadlines`; `GET/POST /api/subs`, `GET/PATCH/DELETE /api/subs/{id}`, `GET /api/subs/{id}/check`, `POST /api/subs/import` (CSV: multipart `file`, `text/csv` body or JSON `{"csv": ...}`); `GET/POST /api/documents`, `POST /api/documents/from_result`, `GET/PATCH/DELETE /api/documents/{id}`; `GET/POST /api/field_logs`, `GET/DELETE /api/field_logs/{id}`, `POST /api/field_logs/sync` (idempotent by `client_uuid`) |
| Compliance view | `GET /api/dashboard`, `GET /api/license/{number}`, `GET /api/calendar.ics` (cookie, API key, or the private feed link from `GET /api/calendar/feed`; rotate with `POST /api/calendar/feed/rotate`) |
| Billing | `GET /api/billing/status`, `POST /api/billing/checkout`, `POST /api/billing/portal`, `POST /api/billing/webhook` |

Use an API key from a script: `curl -H "Authorization: Bearer tck_..." https://<host>/api/dashboard`.

### Add TradeComply to Claude (custom connector)

Customers can add TradeComply to Claude (claude.ai, Claude Desktop and the Claude mobile apps) with just the address
`<APP_URL>/mcp`: no API key and no config file. Claude signs in with TradeComply's own OAuth 2.1 server
(`app/oauth.py`, following the MCP authorization spec), and the person approves the connection in TradeComply.

- Claude Free, Pro or Max: Customize > Connectors > + Add > Add custom connector, name it TradeComply, paste
  `https://tradecomply.example/mcp` (your `APP_URL` + `/mcp`), keep the detected settings (OAuth client: Register
  automatically) and choose Add, then Connect. Claude opens TradeComply: sign in (password or SSO) and choose Approve.
- Claude Team or Enterprise: an Owner adds it once under Organization settings > Connectors > Add > Custom > Web, then
  each person chooses Connect and approves it in TradeComply.
- Claude Code: `claude mcp add --transport http tradecomply https://tradecomply.example/mcp`, then `/mcp` to sign in.
- The `mcp_client_config` tool and Settings > AI assistant (MCP) give the same steps with this server's address.

The connection acts as the person who approved it: their company and their role, read from their account on every
call (removing the person or changing their password disconnects it at once), with the default tool scope (every
read-only tool), the same rate limits, the same subscription check (402) and the same MCP call log rows (marked with
the connection). Each person sees their connections under Settings > Connected apps and can disconnect them; owners
see and can disconnect everyone's. OAuth access tokens work on `/mcp` only, never on `/api`.

What it needs from the server:
- A stable public https address in `APP_URL` (or Render's `RENDER_EXTERNAL_URL`, always set on Render): it is the
  OAuth issuer and the resource Claude checks, and must be exactly the address customers paste. Without it (or with
  an `http://` one on a public host) the server offers no OAuth at all, like SSO: the discovery documents answer
  404, `/oauth/*` 503, and the `/mcp` 401 names no sign-in. Only a loopback address (`http://127.0.0.1`, a
  developer's machine) works without it. Claude calls `/mcp` and the OAuth endpoints from Anthropic's servers, so a
  laptop install can't be added as a connector.
- `/mcp` must answer 401 without a token, which is what starts the sign-in: that is the default on Render. With
  `MCP_PUBLIC_DEMO=1` (or off Render without `MCP_REQUIRE_KEY=1`) anonymous calls succeed and Claude connects without
  signing in, so no company data is reachable that way.
- `https://claude.ai` and `https://claude.com` are allowed as `Origin` on `/mcp` (whether Claude's server-side calls
  carry an Origin header at all was not confirmed when this was written). If a connector still fails with 403
  "Origin not allowed", the server log names the refused origin: add it to `MCP_ALLOWED_ORIGINS`.

Protocol details, limits and what was checked against which spec: [docs/mcp-security.md](docs/mcp-security.md)
("Sign in with TradeComply").

### Company data export and account deletion

Customers in California (CCPA/CPRA) and business buyers expect to be able to take their data with them and to close
their account. Settings > Account and data does both, for the account owner only (`app/account.py`).

**Download all company data** (`GET /api/tenant/export`): one JSON file (`tradecomply-company-data-<date>.json`,
streamed, nothing cached) with the company profile and settings, team members, API keys, projects, subcontractors,
documents, field logs with their signatures, the activity log, the AI assistant (MCP) call log, integrations,
webhooks, queued and sent notifications, single sign-on settings and calendar links. It leaves out what only
protects the account on this server: password and API key hashes, session counters, and every stored credential
(integration sign-ins and keys, webhook addresses and signing secrets, the SSO client secret, private calendar
links, the hashes of open invite, reset and confirmation links). The file lists what it left out and why. Which columns may be exported is a fixed list per table in
`app/store.py` (`TENANT_EXPORT`), so a secret column added later is never exported by accident;
`tests/test_account.py` fails until every new column is listed as exported or left out. Five downloads per company
per 10 minutes; each one is written to the activity log.

**Delete company account** (`DELETE /api/tenant`, body `{"confirm_company_name": "...", "password": "..."}`, with
the CSRF header like every signed-in change): the company name must match exactly and the owner's password is
checked again (wrong passwords share the password-change lockout: 5 per 15 minutes). Then:

1. The card subscription is canceled at Stripe right away when this server has `STRIPE_SECRET_KEY` and the company
   has a subscription (`DELETE /v1/subscriptions/<id>`). Best effort: if Stripe can't be reached or refuses, the
   deletion still happens, the receipt says so with the `SUPPORT_EMAIL` / `SUPPORT_URL` contact, and the server log
   names the subscription id so the operator can cancel it in the Stripe dashboard. Stripe keeps its own invoice and
   payment records; the Stripe customer is not deleted.
2. Each connected integration is asked to cancel TradeComply's access where its connector can do that
   (`Connector.revoke()`, e.g. an OAuth token revocation endpoint), best effort and reported one by one. For the
   others the receipt says to remove TradeComply's access in that product's own settings.
3. Every row of the company is deleted from every company table in one transaction, on SQLite and PostgreSQL alike
   (`Store.delete_tenant`): child tables first and by `tenant_id`, so the activity log and the MCP call log (which
   don't rely on `ON DELETE CASCADE`) go too. Every table in `APP_TABLES` is listed either as a company table or as a
   shared one that holds nothing naming a company (`SHARED_TABLES`: processed Stripe event ids, ended-session
   nonces, the server's own keys, the schema version); importing the store fails if a new table is in neither list.
4. Every session, API key and calendar link of the company stops working (their rows are gone), and this browser's
   cookies are cleared. The email addresses are free to sign up again.

The answer is a short receipt: rows removed per section, the billing result, each integration's result, and what
is kept elsewhere: files people downloaded or printed, subscribed calendars, messages already sent to Slack, Teams or
by text, and the database backups and server logs of whoever runs the server (they expire on that server's own
schedule). Before deleting, a company should download its data and decide with its own advisers whether any records
must be kept; this feature carries out that decision, it doesn't make it.

API keys can do neither: they act with the office role and their scope model limits which tools a key may call,
with no notion of account-level actions, so both routes need the owner's own sign-in.

Operators (for a request that arrives by email, or a company that can't sign in):

```bash
python -m app.account delete --tenant t_0123456789abcdef          # prints what would be removed; deletes nothing
python -m app.account delete --tenant t_0123456789abcdef --yes    # deletes (add --keep-billing or --no-revoke to skip steps 1 or 2)
python -m app.account export --tenant t_0123456789abcdef --out company.json   # the same file as the download (mode 0600)
```

They use the server's `DATABASE_URL` / `DATA_DIR`, `SECRET_KEY` and `INTEGRATIONS_KEY`. Running `delete` again on
a company that is already gone removes any row written while the first run was in progress.

Unconfirmed when this was written (2026-11-26): the Stripe cancel call above was not checked against Stripe's API
reference (docs.stripe.com could not be reached from the environment it was written in), and no California privacy
law text was read for this feature (leginfo.legislature.ca.gov answered with a bot challenge, HTTP 403, and was not
bypassed; oag.ca.gov and cppa.ca.gov could not be reached). Nothing here says what the CCPA/CPRA requires of the
operator or of a customer; ask an attorney.

### Email: invites, password resets and email confirmation

People join a company through an **invite link** and set their own password; nobody, the owner included, ever sees
it. When the server can send email, the link is emailed; the owner always gets a copy to send by text as well, so
invites work with no email set up at all (Settings > Team: Invite, New invite link, Cancel invite). For someone who
already joined, an owner can make a **password reset link** to hand over (Settings > Team > Reset link). With email
set up, people can also use **Forgot password?** on the login page, and confirm their own address (a small banner on
the dashboard, which "Not now" hides for good; or Settings > Your account, which also changes the sign-in email).

| Link | Lasts | Made by | Notes |
|---|---|---|---|
| Reset (emailed) | 30 minutes | Forgot password | Only to a **confirmed** address. Signs the person out everywhere, disconnects their connected apps, turns off the API keys they made, ends their other links, clears a login lockout, emails a "your password was changed" notice; it doesn't sign anyone in. A password change in Settings does the same (except the lockout), so a key someone made with a stolen session doesn't outlive the recovery |
| Reset (owner) | 24 hours | Settings > Team | Shown to the owner to send themselves; nothing on the account changes until it is used |
| Invite | 7 days | Settings > Team | Emailed (when set up) and shown to the owner. Using one ends the other. The emailed one also confirms the address |
| Confirm email | 24 hours | Sign-up, Settings | Must be opened while signed in as that person (the login shows in its place). A new address replaces the old one only when confirmed; the old address gets a notice first |

Each link works once, from one POST after the person acts (opening it changes nothing, so mail scanners that open
links can't use it up), and its token sits after the `#`, which never reaches a server log. A password change, a
reset, an email change or the person being removed ends every link they have, and removing or demoting an owner ends
the invite and reset links that owner made. Companies that require SSO get no
password links except for owners (the same break-glass rule as login); Forgot password emails them a pointer to SSO.
There is **no sign-in link by email** ("magic link"): NIST SP 800-63B-4 section 3.1.3.1 says email SHALL NOT be used
for out-of-band authentication. People added before invite links keep their temporary password and are still asked to
change it; an owner can send them an invite link instead, which turns the temporary password off.

**Setting up email** (whoever runs the server; Settings > Email shows owners whether it is set up and which variable
names are missing, never values):

- `MAIL_FROM`, for example `TradeComply <no-reply@your-company.example>`, and `APP_URL` with the server's **https**
  address (links are built only from it, never from the request's host; without it nothing is emailed).
- Then either **Postmark** (`POSTMARK_SERVER_TOKEN`; HTTPS, so it also works on Render's free plan, which blocks the
  SMTP ports 25, 465 and 587; Render blocks port 25 on every plan). `MAIL_FROM` must be a confirmed Sender Signature
  or domain in Postmark. TradeComply turns link and open tracking off on every message, so links are never rewritten
  through Postmark.
- Or **SMTP** (`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY=starttls` (port 587, the default) or `ssl` (465),
  `SMTP_USERNAME`, `SMTP_PASSWORD`). TLS is required and the certificate is checked; a server without STARTTLS gets
  nothing. render.yaml's `starter` plan can use SMTP; a free-plan Render service needs Postmark.
- Settings > Email > Send me a test email checks it.

Without email everything still works through copied links; "Forgot password?" then tells people to ask their
owner. Design notes and sources: [docs/email-links.md](docs/email-links.md).

### Ask TradeComply (the AI helper)

With `ANTHROPIC_API_KEY` set, an **Ask** button in the top bar opens a panel where anyone signed in asks questions in
plain words: beside the page on wide screens (the page narrows), full screen on phones (a modal dialog that the Back
button and Esc close). Claude (Anthropic's Messages API, `claude-opus-5-5` by default) answers and
runs TradeComply's own read-only checks through the same path as `POST /api/tools/{name}`, as that person: the same
subscription check, activity-log rows (Via "Ask TradeComply") and licensed-professional handoffs. Each answer shows
"Checked: ..." with the checks it ran. The panel says on every screen: "Answers come from Claude by Anthropic and can
be wrong. What you type and tool results are sent to Anthropic." It helps licensed people and never replaces them.

- Sent to Anthropic: the (non-secret) system prompt, the tool list, the chat so far with today's date and the open
  page's name, and each check's inputs and result. Checks read no company records.
- Stored: each person's chats (private to them; deleted after 90 days without use, with the person, or in the company
  deletion), and token usage per question. The company export has the questions, answers and usage, not the model's
  raw messages.
- Limits: 30 questions per person per day, 5M tokens per company per month, 50 chats per person, 40 questions per
  chat, 8 calls and 16 checks per question, 120 s per answer. Owners see usage and turn it off in Settings > Ask
  TradeComply.
- Refusal fallback is on (`fallbacks: "default"`); a refused question gets a plain notice. Never set
  `ANTHROPIC_LOG=debug` in production (the SDK would log what people type).

Data flow, every setting and the design decisions with their sources: [docs/ai-helper.md](docs/ai-helper.md).

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | unset (SQLite) | `postgres://` or `postgresql://` URL: keep customer data in PostgreSQL instead of SQLite. See [Durable storage (Postgres)](#durable-storage-postgres) |
| `DATA_DIR` | `./data` | SQLite app database (when `DATABASE_URL` is unset) and generated signing key. When set (and `LICENSE_DB` isn't), CSLB data goes here too |
| `SECRET_KEY` | generated: in the database with Postgres, else in `DATA_DIR/secret_key` | Signs session cookies, CSRF tokens, calendar links and tool results. At least 32 characters (shorter values are refused at startup). Set it on Render (render.yaml generates one), use Postgres, or keep `DATA_DIR` on a persistent disk so sessions survive deploys |
| `MCP_REQUIRE_KEY` | off, but on Render a key is required anyway | `1`: `/mcp` always needs `Authorization: Bearer <API key>` of an active or trialing account. On Render (`RENDER` is set) `/mcp` needs a key unless `MCP_PUBLIC_DEMO=1`; `MCP_REQUIRE_KEY=0` alone no longer opens it there (a startup warning says so). Elsewhere (a laptop, local mode) off means anonymous calls are allowed |
| `MCP_PUBLIC_DEMO` | off | `1`: run `/mcp` as an anonymous public demo even on Render: every tool free to anyone, audited and throttled (60 tool calls/min per IP, 300/min in total). Ignored when `MCP_REQUIRE_KEY=1`. **Changing to the new default (no `MCP_PUBLIC_DEMO`) changes the live behavior of a Render deploy: anonymous `/mcp` callers get 401** |
| `MCP_ALLOWED_ORIGINS` | unset | Comma-separated browser origins (`https://app.example.com`) allowed to call `/mcp` besides this server's own and Claude's (`https://claude.ai`, `https://claude.com`); a request with any other `Origin` header gets 403 (and a server log line naming it). Requests without `Origin` (every non-browser MCP client) are never refused for that. `*` turns the check off |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` | unset | Enable checkout and the billing portal. The price is the $1,000/month recurring price from the Stripe dashboard |
| `STRIPE_WEBHOOK_SECRET` | unset | Verifies `/api/billing/webhook` (send `checkout.session.completed`, `customer.subscription.updated`/`deleted`, `invoice.payment_failed`, `invoice.paid`) |
| `APP_URL` | `RENDER_EXTERNAL_URL` or request host | Public base URL for Stripe return links, the MCP URL and calendar links, the OAuth issuer and protected resource of "Sign in with TradeComply" (`<APP_URL>`, `<APP_URL>/mcp`: changing it disconnects every connected app; it must be https, and without it connected apps are off except on a loopback address), OAuth redirect URIs (`<APP_URL>/api/integrations/<provider>/oauth/callback`), links in Slack/Teams alerts, and the TradeComply address the `mcp_client_config` tool puts in generated assistant configs (over stdio with no `APP_URL` the caller must pass `tradecomply_mcp_url`). Set it before registering OAuth/SSO redirect URIs or handing out MCP and calendar links |
| `SUPPORT_EMAIL`, `SUPPORT_URL` | unset | The operator's support contact, shown to customers where they need help from whoever runs the server (billing not set up, an integration not turned on) and returned as `support_contact` by `GET /api/config`. Unset: the app says "ask whoever runs this TradeComply server". Use a business address; malformed values are ignored with a warning |
| `INTEGRATIONS_KEY` | derived from `SECRET_KEY` (HKDF) | Base64 of 32 random bytes: the AES-256-GCM key for integration secrets at rest (OAuth tokens, API keys, webhook URLs). render.yaml generates one. Older keys go in `INTEGRATIONS_KEY_PREVIOUS` (comma-separated) during a rotation; then run `python -m app.crypto rotate`. See [docs/integrations-dev.md](docs/integrations-dev.md) |
| `EVENTS_WORKER` | on | `0` turns off the background thread that delivers integration events (webhooks, connectors) |
| `MAIL_FROM` | unset (no email) | Sender of invites, reset links and confirmations, e.g. `TradeComply <no-reply@your-company.example>`. With a backend below and an https `APP_URL`, email links are on. See [Email](#email-invites-password-resets-and-email-confirmation) |
| `MAIL_REPLY_TO` | unset | Optional Reply-To address |
| `POSTMARK_SERVER_TOKEN`, `POSTMARK_MESSAGE_STREAM` | unset, `outbound` | Send through Postmark's HTTPS API (works on Render's free plan). Wins over SMTP when both are set |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD` | unset, 587 / 465, `starttls` | Send by SMTP with required TLS (`starttls` or `ssl`; anything else turns email off). Render's free plan blocks SMTP ports |
| `MAIL_BACKEND=outbox`, `MAIL_OUTBOX_FILE` | unset | Development and tests only (ignored on Render): keep messages in memory and, with `MAIL_OUTBOX_FILE`, append them to that file as JSON lines |
| `ANTHROPIC_API_KEY` | unset (no AI helper) | The operator's Anthropic API key for Ask TradeComply. Unset: the Ask button is hidden and owners see "AI helper is not set up". Never per company, never logged or sent to a browser |
| `ASSISTANT_MODEL`, `ASSISTANT_EFFORT` | `claude-opus-5-5`, `medium` | The model and effort (`low`, `medium`, `high`) for Ask TradeComply |
| `ASSISTANT_DAILY_MESSAGES`, `ASSISTANT_MONTHLY_TOKENS` | 30, 5000000 | Questions per person per California day; tokens per company per month (input + cache writes + output + cache reads / 10) |
| `ASSISTANT_MAX_CONVERSATIONS`, `ASSISTANT_MAX_TURNS`, `ASSISTANT_RETENTION_DAYS`, `ASSISTANT_TURN_TIMEOUT_S` | 50, 40, 90, 120 | Chats per person, questions per chat, days an unused chat is kept, seconds per answer. See [docs/ai-helper.md](docs/ai-helper.md) |
| Connector app credentials (e.g. `PROCORE_CLIENT_ID`) | unset | Each connector lists its own `required_env`; it shows as "not set up on this server" until they are set |
| `TRUSTED_PROXY_HOPS` | `0` (`2` in render.yaml) | How many proxies in front of the app each append the address they saw to `X-Forwarded-For`. `0`: per-IP limits use the first entry, which a client can set. `N`: the N-th entry from the right, which it can't. On Render use `2` (render.yaml sets it): measured on the live service on 2026-11-27, the app receives `<whatever the client sent>, <the client address Cloudflare saw>, <a Render proxy address that changes with every request>`. With `0` the per-IP limits keyed on the first entry could be dodged by sending a made-up header, and those keyed on the whole chain (forgot password, link checks, OAuth) never added up, because the last entry changes on every request. `1` would key every limit on Render's proxy addresses, shared by many customers. Behind another host, check first (log the header of a request you send with a made-up `X-Forwarded-For`): a wrong value makes many people share one address's limits. Re-check on Render after it changes its network |
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
keys shown once and stored as SHA-256, revoked automatically when their creator is removed, moved to the field
role, or changes or resets their password; rate limits on login (5 failures per IP+email, 20 per email in 15 minutes; lockouts survive key flooding),
password changes, sign-ups and `/mcp` tool calls; every query is scoped by tenant; Stripe signatures checked with a
5-minute tolerance and constant-time compare, events de-duplicated and applied in order (a late, older event can't
undo a cancellation; an old subscription ending can't cancel the current one); request bodies capped at 8 MB by
bytes actually received (chunked uploads included); a JSON body holding a lone UTF-16 surrogate (`"\ud800"`) or a
non-finite number (NaN, Infinity, 1e400) is refused with 422 before any route runs, because one such stored value made
every later page, download and export that included it fail, and anything stored before that is repaired on the way
out (`app/safe_json.py`); 422 replies say which field and why, never what was sent (a password, say); downloads
signed in with the session cookie (company data, activity log, MCP call log, calendar) are refused when the browser
says another site started them (Sec-Fetch-Site); sign-in errors reach the login and integrations pages in a short-lived
cookie only the server sets, never as text in the URL; CSP (no inline script, no CDN), HSTS over https and other
security headers; gzip only on public, secret-free responses (app shell, static files, the tool list), never on
account data or keys; the markdown renderer never emits markup from input; "Verified" on a saved document only comes
from a tool result signed by this server, and editing the text drops it; dates outside 1900-2200 are rejected;
calendar and login tokens are redacted from the access log; local mode rejects any Host header other than
`127.0.0.1`/`localhost` (DNS rebinding). `/mcp` tool calls are logged in `mcp_audit_log` with the SHA-256 of the
arguments, never the arguments, and their activity-log rows keep only the argument names and that hash; rows are
written after the response has been sent. Tool runs from the web app (`/api/tools/{name}`) keep arguments in
summarized form (long strings such as a payroll CSV are stored as length + hash); checks Ask TradeComply runs keep only
the argument names and their hash, like `/mcp`, so a private chat's words never reach the shared activity log. Anonymous rows are capped at the
newest 50,000 in each log. Connected apps (OAuth for Claude and other MCP clients): PKCE S256 required, single-use
60-second codes, access tokens for 1 hour, rotating refresh tokens with reuse detection, only SHA-256 hashes stored,
tokens bound to `<APP_URL>/mcp`, the consent screen shows the address the app returns to (plain-ASCII hosts only, so
a look-alike domain can't pass for another), and approving needs the person's own session (never an API key) in the
browser that started the sign-in. One-time links (invites, password resets, email confirmation): 256-bit tokens in
the URL fragment, stored as SHA-256, single use under a row lock, bound to the person's email and session counter,
used only by a POST after the person acts, built only from the https `APP_URL`; Forgot password answers the same way
for every address and does its lookup after the response; messages are built with Python's `EmailMessage` (no header
injection, one recipient), SMTP needs verified TLS, and no token, link, body or key is logged. Ask TradeComply: people
with a session only (API keys 403, OAuth tokens never on `/api`), chats readable only by their person, checks run as
that person through the same path as the Tools page and only read-only ones, answers rendered by the same escaping
markdown renderer, the Anthropic key passed explicitly and never logged, and `/api/assistant/*` never cached by the
service worker. MCP governance (scopes, limits, Origin check, OAuth, OWASP MCP Top 10
self-assessment): [docs/mcp-security.md](docs/mcp-security.md).

### Known limits

- Rate limits and the tool-list cache are in memory: run one instance. Customer data can be in Postgres
  (`DATABASE_URL`), but scaling out also needs shared rate limits (e.g. Redis). Database calls run on worker
  threads (up to `DATABASE_POOL_MAX` connections at once), but each is still a network round trip, so keep Postgres
  in or near the service's region.
- Per-IP limits use the address `TRUSTED_PROXY_HOPS` points at (on Render, `2`: the client address Cloudflare saw,
  which a client can't set); per-email and global limits back them up. Without it (`0`, the default off Render) they
  use the first `X-Forwarded-For` entry, which a client can spoof, and OAuth's per-IP limits and the email-link and
  forgot-password limits key on the whole chain: a caller can't aim them at someone else's address, such as Claude's
  or an office's, but it can bypass them, and then one caller can use up the global cap of 2,000 app registrations an
  hour, which blocks new connections until it resets. On Render that chain also ends in an address that changes with
  every request, so with `0` those limits never add up at all. Sessions are stateless signed cookies (revoked on logout); API keys expire only when given an expiry date.
- People added before invite links have "must change password", but the server doesn't force the change before
  other use (an owner can send them an invite link instead).
- Any company on a trial can make this server email an invite to any address, with a company name it chose. Invite
  emails are capped at 50 per company per hour, names are cut to 80 characters, bodies are plain text and links only
  ever point at `APP_URL`; watch the mail provider's bounce and complaint reports. A company can add at most 100
  people an hour (60 a day on the free trial).
- One email address is one account on the whole server. Someone a company added who never joined doesn't keep the
  address: a sign-up with it, or another company's invite, removes that pending person (their company sees
  `user.invite_released` in its activity log, and the invite link stops working). But an account someone signed up
  with, or joined with a copied invite link, keeps its address even if it was never confirmed, so a stranger can still
  hold an address that isn't theirs that way. Nothing on the server proves who owns a mailbox without email; the fix
  today is the operator removing that company (`python -m app.account delete --tenant <id>`) after checking.
- Sign-up says whether an address already has an account ("Log in instead"); every attempt counts toward the sign-up
  limits (10 an hour per address, 500 an hour in total), so asking costs as much as signing up. Forgot password never
  says.
- Passwords need 10 characters, as before. NIST SP 800-63B-4 asks 15 for a password used alone; raising it is one
  constant (`MIN_PASSWORD`) and the web app's hints.
- No sign-in link by email (see Email above). Without email set up, someone who forgot their password needs an
  owner's reset link, and a company's only owner needs whoever runs the server (the Forgot password page says so and
  shows `SUPPORT_EMAIL` / `SUPPORT_URL` when set: set one before launch).
- Ask TradeComply's "one answer at a time" and Stop are in memory too (one instance). Its answers come from an AI
  model and can be wrong; token budgets are estimates until real usage is seen (docs/ai-helper.md).
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
  `app/billing.py` (Stripe), `app/api.py` (JSON API), `app/account.py` (company data export, account deletion),
  `app/dashboard.py` (RAG dashboard, lien calendar, iCalendar), `app/local.py` (offline mode), `app/static/` (web
  app, service worker, manifest)
- `app/mcp_governance.py` (MCP access mode, Origin check, per-key tool scopes, the MCP call log, `/api/mcp/*`):
  see [docs/mcp-security.md](docs/mcp-security.md)
- `app/assistant.py` (Ask TradeComply, the in-app AI helper, `/api/assistant/*`): see [docs/ai-helper.md](docs/ai-helper.md);
  tests run against `tests/fake_anthropic.py` (the real SDK over a mock transport)
- `app/integrations_api.py` (`/api/integrations`), `app/connectors/` (one file per connector, auto-discovered),
  `app/events.py` (event bus and delivery worker), `app/webhooks.py` (Slack, Teams, signed generic webhooks),
  `app/safe_http.py` (SSRF-guarded outbound HTTP), `app/crypto.py` (secrets at rest): see
  [docs/integrations-dev.md](docs/integrations-dev.md)
- `tradecomply/data/integrations.json` (the integrations catalog: what each product is, how it works with TradeComply,
  MCP endpoints, government-cloud notes, sources), `tradecomply/integrations_guide.py` (MCP tools
  `integrations_for_task` and `mcp_client_config`), served at `/api/integrations/catalog` and shown on the web app's
  Integrations page
- `tests/test_platform.py`: accounts, CSRF, tenant isolation, API keys, `/mcp`, Stripe, 402, tools, dashboard, sync, CSV
