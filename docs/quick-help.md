# Quick help and search

Quick help is the helper built into TradeComply. It reads a question in the person's own words and answers with
TradeComply's own material: rulebook text, the professionals registry, fixed sentences and buttons that open a check
or a page. It is not an AI model, it makes no outside call, and it stores nothing. Search (the palette and the
type-ahead in forms) runs on the same index.

Code: `tradecomply/search.py` (the engine: tokenizer, vocabulary, BM25F index, document builders, field matching),
`app/search.py` (the index lifecycle, the field-to-source map, company lookups, `POST /api/search`,
`GET /api/suggest/vocab`, `POST /api/suggest`, `GET /api/rules/{id}`), `app/quickhelp.py` (the pipeline, the fixed
copy, `POST /api/help`), `app/catalog.py` (check titles, groups, starters, follow-ups, pages and Settings sections
with their roles), `app/store.py` (`Store.candidates`). Data: `tradecomply/data/vocab.json`, `glossary.json`,
`cslb_d_subcategories.json`. Tests: `tests/test_search_engine.py`, `test_search_index.py`, `test_search_api.py`,
`test_suggest_api.py`, `test_rules_api.py`, `test_quickhelp.py`, `test_catalog.py` (SQLite, and PostgreSQL when
`TC_TEST_DATABASE_URL` is set). Benchmark: `python scripts/bench_search.py`.

## What it is, and what it is not

Quick help can:
- find the right check and open it with the details it understood filled in (the job's dates, an amount, a county,
  the person's role); the person checks them and presses Run;
- show the California rule behind a deadline, verbatim, with its citation, official source, verification status and,
  for a rule the verifier corrected, the correction text directly under the plain English;
- say which kind of licensed professional a task may need, and link to the official state lookups;
- take the person to the right page.

Quick help can't, and never says it can:
- give legal advice, or say a licensed professional isn't needed (skip and work-around questions get the `GUARD`
  answer and no rule text at all);
- run a check (the person presses Run), compute a deadline (the check does), sign, stamp, file or send anything;
- answer from outside the California rulebook. Questions it must not answer itself (suing, drafting a letter,
  employee or contractor, firing, fault and enforceability, another state, tax, reading the person's own documents,
  bids) get a fixed note, the professional to find where one applies, and the hand-off to Ask TradeComply (Claude)
  when the server has it set up, which the person sends themselves.

A company whose subscription has ended still gets pages and checks from Quick help and search, but no rule text: no
rule or requirement blocks, no `rule` search group, `[]` from the `rule` suggestion source, and 402 from
`GET /api/rules/{id}` (the checks themselves return 402 as before).

## The pipeline (`app/quickhelp.py answer`)

1. **Clean**: spaces collapsed, the first 500 characters read (a longer question gets the `TOO_LONG` note).
2. **Route check**, before anything else. `GUARD` (skip, avoid, get around, work around, "do we really have to",
   "is there a way around", "how can I not", "<work> without a <permit, license, engineer, stamp, inspection,
   respirator, prelim, bond, insurance, workers' comp, fall protection, shoring, certified payroll, prevailing
   wage, CASp, HERS, SWPPP...>") comes first, so "can we skip respirators when the AQI is 160" is refused rather
   than answered as a smoke question. Plain requirement questions ("Do I have to send a prelim?") have no avoidance
   word and go on to the checks. Then the other routes (sue, draft, fire, employee or contractor, facts and fault,
   another state, tax, the person's own documents, judgment calls). A route answer carries only its note, one find
   action when a professional applies (an attorney, a CPA, or the professional a GUARD question names), and the Claude
   action or the `CLAUDE_OFF` note: never a rule, requirement, may-need-a-professional or open-check block.
3. **Navigation**, only when the question has an action or place word ("how do I", "where is", add, import, upload,
   invite, change, connect, save, print, set up, open, cancel...). Each page is checked against the person's role:
   a field user asking "where is billing" gets `ROLE_OWNER` and no link; "how do I add a project" gets `ROLE_OFFICE`.
   "Connect Procore" opens Integrations filtered to that product.
4. **Slots**: license numbers next to "license", "lic", "CSLB" or "#"; money ($45,000, 2M, 12.5k), percent, feet,
   square feet, acres, AQI, clock time, the next weekday, dates (ISO, 10/15/2026, "Nov 3" read as the last Nov 3
   unless the sentence is about the future, shown in a `READ_DATE` note), today, yesterday, tomorrow, the 58 counties,
   the 8 cities with local rules, role words ("I'm a sub", "delivered lumber"), 65 or older, license classes, a year,
   and the company's own job and sub names (read for this company only). A named job, or `project_id` in the
   request, fills the check's job fields (dates, city, county, contract price) where the question didn't.
5. **Intents**: 35 patterns, each tied to one check (lien, prelim, cancel, down payment, retention, prompt pay,
   Cal/OSHA permits, final pay, injury reporting, smoke, valley fever, heat, toilets, toolbox talks, construction
   hours, minimum wage, sick leave, overtime, public works, apprentices, ADUs, Title 24, stormwater, lead, asbestos,
   building permits, safety standards, unlicensed work, license lookup, workers' comp, license classes, stop payment
   notices, project setup, off-road diesel, trade certifications; plus finding a professional). Score: a pattern hit
   1.0 (a named professional 2.0; a death or hospital stay adds 1.0 to injury reporting), plus 0.4 for each matched
   vocabulary group of the intent's check (at most 0.8), plus 0.6 when `trade_certifications.classify_task()` sees
   paint disturbance on a pre-1978 building, plus 0.3 when the question is asked from that check. Groups only add to
   a pattern or classifier hit; a question with neither goes to step 6. The company's own job and sub names are left
   out first ("Elm St ADU" is not an ADU question). When the runner-up has a different check and scores at least 85%
   of the best, the answer is "Do you mean...?" with up to three choices.
6. **Fallbacks**: the best check from the index whose words cover at least half the question; else rules that do
   (and score at least 45% of the best); else "not found" with the three nearest checks.
7. **Professionals**: for the kind of professional asked about, the question's own words (no synonyms) are compared
   with each `when_required` situation and each `not_required_when` / `optional_when` situation in
   `professionals.json`. A requirement block appears only when the best `when_required` situation shares at least half
   of the question's words and more than every exception does. Otherwise the answer is the find action alone. The
   exception texts never appear in any answer, search result or index document.
8. **Rule order**: the intent's own rules, then its vocabulary groups' anchor rules (those anchored by several matched
   groups first; within each, the ones whose title and plain English share more of the question's words first), then
   the other index hits by score. One rule per citation. At most two rule blocks; up to five more under "More rules".
   Verification status never moves a rule up: every block carries its own badge.
9. **Entitlement**, then the counts notes (`UNVERIFIED_SOME`, `UNCONFIRMED_SOME`) and the envelope. Arguments for a
   check are validated against its schema (field exists, enum value, finite number, ISO date) before they are sent.

Everything runs inside one `try`: an exception is logged as `quickhelp failed intent=<name> exc=<type>` (never the
message, which could hold typed words) and the person gets the not-found answer.

## Data sources

| Source | What it gives | Read on |
|---|---|---|
| `tradecomply/data/rulebook.json` | 3,056 rules; status confirmed / corrected (verified) / unchecked / unverifiable; the verifier's correction | built 2026-11-26 |
| `tradecomply/data/professionals.json` | 24 kinds of professional, triggers, when the law requires one (with legal basis, source, status), exceptions, official lookups, `never_say`, the 58 counties (DIR public works apprenticeship search, https://www.dir.ca.gov/databases/das/pwaddrstart.asp) | counties 2026-11-26 |
| `tradecomply/data/handoffs.json` | which checks' results name which professional (Quick help only says "the check tells you") | |
| `tradecomply/data/cslb_d_subcategories.json` | CSLB's 64 C-61 "D" subcategories (D-24 does not exist), from https://www.cslb.ca.gov/About_Us/Library/Licensing_Classifications/C-61_Limited_Specialty/Default.aspx; 28 still issued | fetched 2026-11-27 |
| `licensing.class_table()` | A, B, B-2 and the 43 C classes (16 CCR 832) with 438 trade keywords and each class's scope rule | from the rulebook |
| `local_rules` | the 8 cities with local rules; San Diego and Los Angeles verified | from the rulebook |
| `tradecomply/data/vocab.json` | 76 concept groups: what contractors type and the rulebook's words for it, the checks and anchor rules | 2026-11-27 |
| `tradecomply/data/glossary.json` | 31 terms with the rule that explains each (no rule text) | 2026-11-27 |
| `tradecomply/data/integrations.json` | 36 products for "connect ..." and search | |

No statewide city list (Department of Finance E-1), NAICS list or workers' comp class list is used: `dof.ca.gov`
and `www.census.gov` were blocked from the build session, and nothing is written from memory. City fields suggest
the company's own project cities and the 8 covered cities.

## The fixed copy

Every sentence Quick help writes is in `app/quickhelp.py` `NOTES` and `LEADS` (a test checks every note and lead in
every answer against them; templates fill in only a date, a field label, a check title, a button name, a count, a
professional's name or the person's own quoted words). The lines that keep it honest:

- `GUARD`: "TradeComply can't help skip or work around a requirement. A licensed professional can tell you what your
  job needs."
- `MAYBE_PRO` (before a check runs): "Run the check: its result says whether the law requires this person here."
- `UNVERIFIED_SOME`: "TradeComply hasn't checked {n} of these rules against the official source yet. Read the source
  before you rely on it."
- `CLAUDE_CAN`: "Ask TradeComply (Claude) can talk it through. It sends your question to Anthropic and counts toward
  today's questions."
- Every answer carries `core.DISCLAIMER` verbatim.

Two leads were added to the spec's table, because the spec's `LEAD_PRO` / `LEAD_PRO_FIND` ("where the state lists
them", "The state lists licensed {name}s here") would be false for a notary (commissioned), a HERS rater
(certified by providers), a competent person (chosen by the employer), the building department and others:
`LEAD_PRO_OTHER` "Here is when the law requires {a_name}, and how to find one..." and `LEAD_PRO_FIND_OTHER` "The Find
a professional page shows how to find and check {a_name}...". `PRO_NOUNS` in `app/quickhelp.py` says which kinds the
state lists as licensed (attorney, engineer, surveyor, geologist, architect, landscape architect, contractor,
insurance agent, CPA, crane certifier) and carries the article ("an attorney").

A forbidden-phrase test runs over Quick help's own strings in every fixture answer and 200 fuzzed questions: the 84
`never_say` phrases, the app's banned words ("guarantee", "we recommend", "top-rated", "fully compliant"...) and "you
don't need", "you can skip", "skip the", "not required to hire", "as your attorney", "I recommend", "my advice".
Rule text is checked by byte-equality with `rulebook.json` instead: the law itself sometimes says "guarantee".

## Limits, privacy and what is stored

| Topic | Rule |
|---|---|
| Who | The session cookie only, any role, on every route (`auth.people_only`). API keys get 403 ("Quick help and search are for people signed in to TradeComply."); OAuth tokens get 401 from `get_principal`. Keys read rules through `search_california_rules` / `get_california_rule`, which are scoped, rate-limited and logged |
| Typed text | Never in a URL: search, suggest and help are POST with a JSON body (a GET gets 405). Only `/api/suggest/vocab`, which holds nothing typed, is a GET |
| Rate limits | Quick help 60 questions a minute per person; search and suggestions together 900 a minute per person, 1,800 per company, 12,000 for the server; 429 with `Retry-After`. The vocabulary is not limited (a 304 is cheap) |
| Work caps | 12 words of a query, typo fixes for 3, completion of the last word only; every pattern finishes in under 50 ms on 500 adversarial characters (test) |
| Stored | Nothing: no table, no activity-log row, no file. `Cache-Control: no-store` on every answer; the vocabulary is `private, no-cache` with an ETag |
| Logs | `tradecomply.quickhelp` at DEBUG: `intent=... route=... blocks=... ms=...` only. A test sends a job name and a license number, forces an error inside slot parsing, and finds neither in any log record |
| Company data | Read per request with the tenant from the session (`Store.candidates`: projects' name, city, county, address, owner, lender; subs' name, license number, scope; documents' titles; field logs' kind, topic, attendees; team names). Never cached, never in the index, never contact details or signatures. Team names only for owners and office (the team list is office-only); field logs are read with `kind in ('toolbox_talk', 'inspection')` in the SQL, so incident first reports (injured workers, witnesses) are never a source of names or topics. Matching is done in Python with case and accent folding, the same on SQLite and PostgreSQL |
| Compression | Only the vocabulary is gzipped (the same for everyone). Search, suggestions and answers carry company data and are never compressed |
| Claude | Only the text the person typed goes to Claude, and only when they press Send on the Ask TradeComply tab. Quick help's slots and company matches are never sent |

## The index

One in-memory BM25F index per process over 3,300 documents (3,056 rules, 74 checks, 74 CSLB classes: A, B, B-2, 43
C and the 28 D subcategories still issued, 36 integrations, 32 kinds of professional, 28 pages and Settings
sections). Built from data that is the same for every company, on a daemon thread started from the app's lifespan
(`SEARCH_WARM`, on by default; `tests/support.py make_app` turns it off and tests call `search.build_now`). A second
app in the same process with the same rulebook and tool list reuses it. A failed build is logged once and search
stays in fallback mode (rules from `core.search`, checks and pages by word match); there is no retry loop, no pickle
and no `gc.freeze()`. `/health` reports `"search": "off" | "building" | "ready" | "failed"`.

Each rule also gets its related checks: the checks whose handler module names the rule id literally, narrowed to the
checks of the vocabulary groups that anchor the rule. The five checks registered in `server.py` (license lookup,
permit fees, safety standard, lien deadline, sub compliance) call `tradecomply/logic.py`, which names no rulebook id,
so no rule lists them; the preliminary-notice rule lists only the project setup checklist.

## Measured performance

`python scripts/bench_search.py` on the build sandbox (4 vCPUs, Python 3.13, nothing else running), 2026-11-27:

| What | p50 | p95 | p99 | max | Budget (spec 7.2) |
|---|---|---|---|---|---|
| Index build (docs, index, related checks) | 1.06 s | | | | 2.0 s, in the background |
| Index memory (tracemalloc, the index alone) | 24.4 MB | | | | 40 MB |
| Palette search, every keystroke of the 80 fixture questions (4,563 queries) | 1.9 ms | 6.2 ms | 8.4 ms | 255 ms (one GC pause) | p95 15 ms |
| Static suggestions, 10 fields, every keystroke (what the browser filters) | 0.07 ms | 0.44 ms | 0.58 ms | 0.58 ms | p95 5 ms |
| Search-backed suggestions (scope phrases, needs, rules) | 0.18 ms | 5.9 ms | 8.8 ms | 8.8 ms | |
| Company suggestions, a big company (500 projects, 3,000 subs, 5,000 documents, 2,000 logs) | 6.8 ms | 13.6 ms | 17.6 ms | 17.6 ms | p95 10 ms (over for this size) |
| Quick help answer, 80 questions x 5, against the big company | 5.5 ms | 12.0 ms | 16.1 ms | 277 ms | p95 50 ms |
| HTTP in-process: POST /api/search / /api/suggest / /api/help | 12.5 / 4.7 / 14.2 ms | 21.8 / 6.8 / 22.5 ms | | | |
| `GET /api/suggest/vocab` | 126 KB raw, 26 KB gzipped | | | | 160 KB / 30 KB |

Index: 8,927 terms, 151,277 postings, 36,824 typo keys, 544 synonym phrases; 1,542 rules have related checks. CI
asserts only generous ceilings (search p95 under 100 ms, Quick help p95 under 250 ms). Company suggestions read up to
2,000 newest rows per table and match them in Python; a company with thousands of subs is slightly over the 10 ms
budget per keystroke, which the browser's 150 ms debounce absorbs.

## Accuracy

| Set | First run | Now |
|---|---|---|
| The 30 + 10 questions the prototype was built against (`tests/fixtures/quickhelp_questions.json`) | 40/40 (prototype) | 40/40 |
| 20 paraphrases written after the prototype was tuned (`quickhelp_holdout.json`) | prototype 15/20; this port before its own fixes 19/20 (18/20 counting the intent label) | 20/20 |
| 20 fresh questions written for this build before porting or tuning (`quickhelp_fresh.json`) | the unmodified prototype 17/20 (16/20 with the intent label); this port, untuned, 19/20 | 20/20 |
| Starters and follow-ups (100 questions) | | every one answered |

Pass = the expected check (or page, professional or route) is offered and, when the fixture names rules, at least one
of them is cited. The fresh set's one first-run miss ("a worker collapsed from heat stroke and died on site, how fast
do I have to report it" went to heat rules) was fixed by a generic rule (a death or hospital stay outranks the
subject), not by matching that sentence. All 60 fixtures are now regression tests, so they no longer measure how well
Quick help generalizes: real questions (with the owner's permission, anonymised) are the next honest measure.

## The unverified rules Quick help cites most

Over the 80 fixture questions and the 100 starters and follow-ups, Quick help shows 98 distinct rules; 32 are not
verified. The ones it shows most, where verification time pays off first (leginfo.legislature.ca.gov blocks automated
reading; a person can check these in a browser):

| Rule | Status | Citation | Shown in |
|---|---|---|---|
| `permit-trench-excavation-5ft` | unchecked | 8 CCR 341(d)(5)(A); Labor Code 6500 | 6 answers |
| `dir-registration-required-to-bid-or-work` | unchecked | Labor Code 1771.1(a), 1725.5 | 6 |
| `permit-scaffold-falsework-36ft` | unchecked | 8 CCR 341(d)(5)(B) | 5 |
| `lien-foreclosure-90-days-after-recording` | unverifiable | none cited (CSLB lien guide) | 5 |
| `workers-comp-required-classifications` | unchecked | B&P 7125 | 5 |
| `prelim-notice-20-day-deadline` | unverifiable | Civil Code 8200(a), 8204(a), 8206 | 4 |
| `energy-hvac-changeout-hers` | unchecked | Title 24 Part 6 150.2(b)1 | 4 |
| `lien-90-days-no-noc-or-cessation` | unverifiable | none cited (CSLB lien guide) | 4 |
| `prelim-notice-who-is-exempt` | unverifiable | Civil Code 8200(b)-(c) | 3 |
| `general-building-two-trade-rule` | unchecked | B&P 7057(b) | 3 |
| `hic-no-payment-ahead-of-work` | unchecked | B&P 7159 | 3 |
| `osha2-valley-fever-annual-training-may-1` | unverifiable | Labor Code 6709 | 3 |
| `energy-cf3r-hers-verification` | unchecked | Title 24 Part 6 10-103(a)(5) | 3 |
| `cgp-swppp-by-qsd` | unchecked | CGP Order WQ 2022-0057-DWQ, Attachment D.2 | 3 |
| `stop-notice-required-contents` | unchecked | none cited (CSLB lien glossary) | 3 |
| `bonded-stop-notice-125-percent` | unverifiable | none cited (CSLB lien guide) | 3 |

Every one of these shows "Not yet verified" in the answer and adds the `UNVERIFIED_SOME` note until it is checked.

## Adding an intent or a vocabulary group safely

- **A vocabulary group** (`tradecomply/data/vocab.json`): `forms` are what people type (a generic word as
  `{"form": "...", "weight": 0.3}`), `expand` the rulebook's own words for it (an expansion of several words matches
  only rules that hold all of them; never a bare number), `tools` the checks it belongs to, `anchors` the rules it
  should surface first. `tests/test_search_engine.py` checks that every anchor and check exists and that numbers never
  trigger a group. A synonym is spelling, not law, but a wrong one sends people to the wrong rule: have someone read
  the group, and run the fixtures.
- **An intent** (`app/quickhelp.py INTENTS`): one pattern, one check. Add its search words to `RULE_QUERY`, its
  argument building to `intent_args` (every value is validated against the check's schema anyway), two follow-up
  questions to `app/catalog.py FOLLOWUPS` (a test asks each and requires an answer), and at least one fixture. If it
  competes with an existing intent, prefer an `EXCLUDE` pattern on the broader one over a bigger score.
- Never add a sentence of Quick help's own outside `NOTES` and `LEADS`, never quote `not_required_when` or
  `optional_when`, and run `python -m pytest -q tests/test_quickhelp.py tests/test_search_engine.py` (the forbidden
  phrase, verbatim-rule and route tests catch the common mistakes).
