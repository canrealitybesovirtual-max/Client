# One-time links by email

Invites, password resets and email confirmation for TradeComply CA. Code: `app/mailer.py` (sending),
`app/auth_links.py` (links and routes), `app/store.py` (schema 8: `auth_links`, `users.email_confirmed_at`;
`create_auth_links`, `auth_link`, `use_auth_link`), the owner routes in `app/api.py`, the SPA routes `#/forgot`,
`#/reset/<token>`, `#/invite/<token>`, `#/confirm-email/<token>` in `app/static/app.js`. Tests:
`tests/test_email_links.py` (SQLite and PostgreSQL) and `tests/test_ui_email_links.py` (Playwright, 1280 and 375 px).
Setup for operators is in the README ("Email: invites, password resets and email confirmation").

## Decisions, and where the standards changed the brief

| Topic | Decision | Why |
|---|---|---|
| Email provider | Postmark's HTTPS API, or SMTP with required TLS | Render's free web services can't send on ports 25, 465 or 587 (port 25 is blocked on every plan), so a free-plan deploy needs HTTPS. Postmark lets every message turn link and open tracking off (`TrackLinks: "None"`, `TrackOpens: false`); tracking rewrites links through the provider, which exposes tokens and can use up single-use links. Resend was considered: its tracking is a per-domain dashboard setting, so it was not built |
| Token in the link | In the URL fragment, and used only by a POST after the person acts | Browsers never send the fragment to a server or in Referer, so tokens stay out of access logs. Mail scanners do open links (and link rewriters copy the whole URL), so the fragment alone isn't enough: opening a link changes nothing |
| Reset link lifetime | 30 minutes by email; 24 hours when an owner makes one to hand over | NIST caps emailed recovery codes at 24 hours; the person asking is waiting for the email |
| Email confirmation lifetime | 24 hours (the brief said 2-7 days) | NIST SP 800-63B-4 4.2.1.2: an address confirmation code has "the same characteristics as a recovery code", valid at most 24 hours by email |
| Invite lifetime | 7 days | Enrollment, not recovery; the owner chose the address. Joining through the emailed link confirms the address |
| Who may confirm an address | Only the signed-in person it was sent for | NIST 4.2.1.2: "A recovery address SHALL be established only after the subscriber provides the correct confirmation code". Otherwise a stranger at a mistyped sign-up address could confirm it, then reset the password and take the account |
| Forgot password | Only to confirmed addresses; the reply never differs | OWASP Forgot Password and Authentication cheat sheets. Existing people are never locked out: they keep their password, an owner can hand them a reset link, and a dismissible banner lets them confirm |
| Sign-in after reset | No: log in with the new password | OWASP Forgot Password: "Don't automatically log the user in". Joining by invite does sign in, like sign-up |
| Sign-in links by email ("magic link") | Not built | NIST SP 800-63B-4 3.1.3.1: "Email SHALL NOT be used for out-of-band authentication". Resets and confirmations are explicitly allowed. `'signin'` is reserved in the table's CHECK constraint |
| SSO-required companies | No password links except for owners; Forgot password emails a pointer to SSO | The same break-glass rule as login. Saying "this company uses SSO" in the HTTP reply would show the address has an account |
| Changing email | Current password, a confirmation link to the new address, a notice to the old one at request time | OWASP Authentication cheat sheet. Its stricter "confirm both addresses" was not followed (someone who lost the old mailbox could never change it). The notice goes out first, so the old owner can reset the password, which ends the pending link |
| Password length | 10 characters, as elsewhere in the app | OWASP: consistent with the rest of the app. NIST SP 800-63B-4 3.1.1.2 asks 15 for a password used alone: a product decision, one constant (`MIN_PASSWORD`) |

## How a link works

- Token: `tcl_` + `secrets.token_urlsafe(32)` (256 bits). The database keeps only its SHA-256 (`auth_links.token_hash`).
- Each link row records the person's email and session counter when it was made. Looking a link up joins the person
  on both, so a password change, a reset, an email change, SSO becoming required or a removed person (SQLite reuses
  ids) ends every open link without touching the rows.
- A link an owner made (an invite, or a reset link to hand over) works only while its maker is still an owner of the
  company: removing or demoting them deletes those links in the same transaction, and the lookup refuses any left
  behind. Otherwise an owner about to be removed could keep a member's reset link and take that account later.
- `use_auth_link` runs in one locked transaction (SQLite `BEGIN IMMEDIATE`; PostgreSQL locks the person, then
  re-reads the link `FOR UPDATE`, the same order link creation and person removal use, so two of one person's links
  used at the same moment can't deadlock): the second of two simultaneous uses waits, then finds the link gone. The new password is hashed before the lock (scrypt
  is slow) but after a lookup (an unknown token costs no hashing).
- Reset: new password, session counter + 1, every link of that person deleted, connected apps ended
  (`oauth.revoked` rows, reason `password_reset`), activity row `auth.password_reset`, the login lockout for that
  email cleared, and a "password was changed" notice to a confirmed address.
- Expired rows are deleted whenever a new link is made. Up to three open reset links per person, one invite (an
  emailed and a copied link) and two confirmations for the same address (sending a new one leaves the one sent a
  moment ago, at sign-up say, working).
- Forgot password: the per-IP limit (10 per 15 minutes) answers 429. The per-address limit (3 emails per 15
  minutes) and the server-wide cap (300 emails an hour) answer exactly like any other request and send nothing; the
  cap counts only emails that really go out, so requests for made-up addresses can't use it up and lock everyone out.
  Every lookup and the email happen in a background task after the response, so the reply's status, body, headers
  and timing don't depend on the address.
- Link checks and uses: 60 failed ones per IP per 10 minutes (a good link never counts, so a valid link from a busy
  shared network still works). Invite emails: 50 per company per hour (over it, the owner still gets the link to
  copy). Confirmation emails and email changes: 5 per person per hour.
- The per-IP limits here key on the whole `X-Forwarded-For` chain unless `TRUSTED_PROXY_HOPS` is set (`ip_key`, as
  OAuth does): a caller writes the first entries, not the ones Render's proxy appends, so a forged header never lands
  in another network's bucket. Set `TRUSTED_PROXY_HOPS` on Render once the header is checked, so a caller can't start
  a fresh bucket with each forged entry either.
- Joining with a copied invite link (texted by the owner) doesn't confirm the address, so a confirmation email is
  sent then, as at sign-up; joining with the emailed link confirms it.
- Unauthenticated link routes take JSON bodies only (FastAPI refuses a form or `text/plain` body with 422), so no
  other site can post them; signed-in routes need the CSRF header like every other write.
- Data rights: the company export has a `one_time_links` section without hashes or session counters; deleting a
  person or the company deletes their links.

## Messages

Plain text, short. Subjects and names have whitespace collapsed (a company name can't add a header) and names are
cut to 80 characters. Anyone can start a trial, so a company name or person's name is never put in a subject, is
labeled as typed by the company ("Company name, as its owner entered it: ..."), and loses control and invisible
characters (bidi overrides could reorder what is shown) and anything that looks like a web or email address
("[link removed]"): an email from TradeComply's address must not carry a link TradeComply didn't make. `email.message.EmailMessage` with the default policy refuses CR/LF in headers; the recipient
goes through `email.headerregistry.Address`, so `"a,b@evil.example"` is refused instead of becoming two recipients,
and SMTP gets `to_addrs=[that one address]`. Python's `smtplib` checks no certificate unless given a context, so
both STARTTLS and SMTPS get `ssl.create_default_context()`. `HeaderParseError` is not a `ValueError`; both are
caught. Logs name the backend, the recipient's domain and a status or error code, never the address, body, link or
key (Postmark's error `Message` can quote the address, so it isn't logged). A Postmark 5xx is not retried: there is
no idempotency key, so a retry could send twice.

## Sources

Read 2026-11-26. "Index" means the official page was read through the Context7 documentation index because the
site itself could not be reached from the build environment; check those on the live page before launch.

| Source | What it settled | How it was read |
|---|---|---|
| OWASP Forgot Password Cheat Sheet | Same answer for every address, per-account limits, hashed single-use tokens, no Host header in URLs, type the password twice, notify, don't sign in, end sessions and links | Source repository (github.com/OWASP/CheatSheetSeries) |
| OWASP Authentication Cheat Sheet | Generic recovery message, re-authenticate before an email change, the two email-change processes | Source repository |
| NIST SP 800-63B-4, sections 3.1.1.2, 3.1.3.1, 4.2.1.2, 4.6 | No email out-of-band authentication; recovery and confirmation codes at most 24 h by email; notify on recovery; 15-character passwords | Source repository (github.com/usnistgov/800-63-4) |
| Python 3.13 smtplib, email.message, email.policy, email.headerregistry, ssl | Default context checks nothing; header and recipient parsing | CPython source repository docs, plus checks on Python 3.13 |
| W3C Referrer Policy, WHATWG Fetch | The fragment is never sent | Source repositories |
| Render, free plan (`/docs/free`) | Free web services can't send on 25, 465, 587; 25 blocked on every plan | Index. When the block started: unconfirmed |
| Postmark Email API and API overview | Endpoint, headers, body fields, tracking switches, response and error codes | Index. Free-tier volume and the "sender signature not found" code: unconfirmed |
| Resend send-email API and errors | Request shape, tracking only per domain | Index (not built) |
| Supabase auth email templates | Mail scanners such as Microsoft Defender Safe Links open links and use them up | Index. Whether Safe Links runs JavaScript or copies the fragment: unconfirmed (Microsoft's page could not be reached) |
