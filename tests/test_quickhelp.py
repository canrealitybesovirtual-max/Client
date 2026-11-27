"""Quick help, POST /api/help (spec 5.4-5.12, 7.3): fixtures, honesty rules, entitlement, roles, privacy, limits."""
import json
import logging
import os
import random
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import assistant, catalog, quickhelp, search
from app.auth import RateLimiter
from tests.support import backend, browser, join, make_app, oauth_connect, signup  # noqa: F401
from tradecomply import core, professionals

SITE = "https://tradecomply.example"
FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(quickhelp, "today", lambda: date(2026, 11, 27))


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=SITE)
    a.state.platform.env = {"ANTHROPIC_API_KEY": "sk-ant-test-not-real"}       # Ask TradeComply is set up
    a.state.platform.help_user_limiter = RateLimiter(limit=100_000, window_s=60)
    search.build_now(a.state.platform)
    return a


@pytest.fixture
def owner(app):
    with TestClient(app, base_url=SITE) as c:
        c.tenant_id = signup(c, company="Sierra Build Co", license_number="1012345")["tenant_id"]
        c.elm = c.post("/api/projects", json={"name": "Elm St ADU", "city": "Oakland", "county": "Alameda",
                                              "first_furnishing_date": "2026-10-01",
                                              "last_furnishing_date": "2026-11-10"}).json()["id"]
        c.post("/api/projects", json={"name": "Kern Solar Yard", "city": "Bakersfield", "county": "Kern"})
        c.ace = c.post("/api/subs", json={"name": "Ace Roofing", "license_number": "1023456"}).json()["id"]
        yield c


def ask(c, question, **kw):
    r = c.post("/api/help", json={"question": question, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def problems(ans, exp):
    out = []
    acts = [b for b in ans["blocks"] if b["type"] == "action"]
    if exp.get("intent") and ans["intent"] != exp["intent"]:
        out.append(f"intent {ans['intent']} != {exp['intent']}")
    if exp.get("tool"):
        tb = next((b for b in acts if b["kind"] == "open_tool" and b["tool"] == exp["tool"]), None)
        if not tb:
            out.append(f"no open_tool {exp['tool']}")
        else:
            for k, v in (exp.get("args") or {}).items():
                if tb["args"].get(k) != v:
                    out.append(f"arg {k}={tb['args'].get(k)!r}, want {v!r}")
    cited = [b["rule_id"] for b in ans["blocks"] if b["type"] == "rule"]
    if exp.get("any_rules") and not set(exp["any_rules"]) & set(cited):
        out.append(f"rules {cited} miss {exp['any_rules']}")
    if exp.get("find") and not any(b["kind"] == "find_professional" and b["type_id"] == exp["find"] for b in acts):
        out.append(f"no find_professional {exp['find']}")
    if exp.get("go") and not any(b["kind"] == "go" and b["href"] == exp["go"] for b in acts):
        out.append(f"no go {exp['go']}")
    if exp.get("go_prefix") and not any(b["kind"] == "go" and b["href"].startswith(exp["go_prefix"]) for b in acts):
        out.append(f"no go {exp['go_prefix']}")
    if "route" in exp and ans["route"] != exp["route"]:
        out.append(f"route {ans['route']} != {exp['route']}")
    if exp.get("guard") and not any(b.get("code") == "GUARD" for b in ans["blocks"]):
        out.append("no GUARD note")
    return out


def run_set(c, items):
    bad = []
    for it in items:
        p = problems(ask(c, it["q"]), it["expect"])
        if p:
            bad.append((it["q"], p))
    return bad


# ---------------------------------------------------------------------------- fixtures
def test_the_30_and_the_10(owner):
    d = load("quickhelp_questions.json")
    assert len(d["answer"]) == 30 and len(d["handoff"]) == 10
    assert run_set(owner, d["answer"] + d["handoff"]) == []


def test_the_20_paraphrases(owner):
    assert run_set(owner, load("quickhelp_holdout.json")["questions"]) == []


def test_the_20_fresh_questions(owner):
    """Written before Quick help was ported or tuned; first-run score in docs/quick-help.md. Now a regression set."""
    items = load("quickhelp_fresh.json")["questions"]
    assert len(items) == 20
    assert run_set(owner, items) == []


def test_every_starter_and_follow_up_answers(owner):
    qs = {q for v in catalog.PAGE_STARTERS.values() for q in v} | {q for v in catalog.GROUP_STARTERS.values()
                                                                    for q in v}
    qs |= {q for v in catalog.FOLLOWUPS.values() for q in v}
    assert len(qs) > 80
    for q in sorted(qs):
        a = ask(owner, q)
        useful = [b for b in a["blocks"] if b["type"] in ("rule", "requirement") or (
            b["type"] == "action" and b["kind"] in ("open_tool", "go", "find_professional"))]
        assert a["intent"] not in ("not_found", "choices") and useful, q
    for intent in quickhelp.INTENT_TOOL:
        assert 2 <= len(catalog.FOLLOWUPS.get(intent, [])) <= 3, intent


# ---------------------------------------------------------------------------- professionals (spec 5.4 step 7)
@pytest.mark.parametrize("q,type_id", [("do I need a notary to record my lien", "notary_public"),
                                       ("do I need an architect for an interior remodel of a storefront", "architect"),
                                       ("do I need an engineer for a 3 ft retaining wall", "professional_engineer")])
def test_no_requirement_when_an_exception_fits_better(owner, q, type_id):
    a = ask(owner, q)
    assert a["intent"] == "find_pro" and not [b for b in a["blocks"] if b["type"] == "requirement"]
    a_name, names, licensed = quickhelp._pro_name(type_id)
    want = quickhelp.LEADS["LEAD_PRO_FIND" if licensed else "LEAD_PRO_FIND_OTHER"].format(a_name=a_name, names=names)
    assert a["lead"] == want
    assert [b for b in a["blocks"] if b.get("kind") == "find_professional"][0]["type_id"] == type_id


def test_requirement_when_the_law_requires_one(owner):
    a = ask(owner, "do I need an engineer to design sloping for an excavation deeper than 20 feet")
    req = [b for b in a["blocks"] if b["type"] == "requirement"]
    pe = professionals.types()["professional_engineer"]
    assert len(req) == 1 and req[0]["situation"] == pe["when_required"][0]["situation"]
    assert req[0]["status"] == "confirmed" and req[0]["verified"] is True
    assert req[0]["legal_basis"] == pe["when_required"][0]["legal_basis"]
    assert a["lead"].startswith("Here is when the law requires a professional engineer")


def test_an_unconfirmed_requirement_says_so(owner):
    a = ask(owner, "do I need an engineer or architect for the statement of special inspections before the "
                   "building permit is issued")
    req = [b for b in a["blocks"] if b["type"] == "requirement"]
    assert req and req[0]["status"] == "unconfirmed" and req[0]["verified"] is False
    assert "UNCONFIRMED_SOME" in [b.get("code") for b in a["blocks"]]


# ---------------------------------------------------------------------------- skip / work-around and routes
SKIP = ["can we skip respirators when the AQI is 160", "how do I skip the engineer stamp for a retaining wall",
        "skip the prelim", "can I avoid getting a permit for a deck", "how do we get around the trench permit",
        "is there a way around certified payroll", "can we work without a permit this weekend",
        "do we really have to wear respirators", "get out of fall protection on a one-story roof",
        "how can I not pay prevailing wage on a city job"]
PLAIN = ["Do I need a permit for a 10x12 shed?", "Do I have to send a prelim?", "When do I need a trench permit?"]
ROUTE_FORBIDDEN = {"rule", "requirement", "maybe_professional"}


def check_route(a):
    assert a["route"] in ("claude", "professional", "search") and a["intent"].startswith("route:")
    assert not [b for b in a["blocks"] if b["type"] in ROUTE_FORBIDDEN or b.get("kind") == "open_tool"]
    assert a["more_rules"] == [] and a["understood"] == [] and a["followups"] == []


def test_skip_questions_route_to_the_guard(owner):
    for q in SKIP:
        a = ask(owner, q)
        assert a["intent"] == "route:GUARD", q
        check_route(a)
    eng = ask(owner, SKIP[1])
    assert [b["type_id"] for b in eng["blocks"] if b.get("kind") == "find_professional"] == ["professional_engineer"]
    assert eng["route"] == "professional"
    for q in PLAIN:
        assert not ask(owner, q)["intent"].startswith("route:"), q


def test_every_route_answer_carries_no_rules(owner):
    for it in load("quickhelp_questions.json")["handoff"]:
        check_route(ask(owner, it["q"]))


# ---------------------------------------------------------------------------- rule blocks
def all_answers(c):
    d = load("quickhelp_questions.json")
    qs = [i["q"] for i in d["answer"] + d["handoff"] + load("quickhelp_holdout.json")["questions"]
          + load("quickhelp_fresh.json")["questions"]]
    return [(q, ask(c, q)) for q in qs]


def test_rule_blocks_are_verbatim(owner):
    with open(core.RULEBOOK_PATH, encoding="utf-8") as f:
        book = {r["id"]: r for r in json.load(f)["rules"]}
    seen = 0
    for q, a in all_answers(owner):
        rules = [b for b in a["blocks"] if b["type"] == "rule"]
        assert len(rules) <= 2 and len(a["more_rules"]) <= 5
        for b in rules:
            r = book[b["rule_id"]]
            seen += 1
            assert b["title"] == r["title"] and b["text"] == r["plain_english"] and b["citation"] == r["citation"]
            assert b["quote"] == (r.get("quote") or None) and b["applies_to"] == (r.get("applies_to") or None)
            assert b["verified"] == (r["status"] in ("confirmed", "corrected")) and b["status"] == r["status"]
            assert b["href"] == f"#/rule/{b['rule_id']}"
            assert b["source"] is None or b["source"].startswith("https://")
            if r["status"] == "corrected":
                assert b["correction"] and b["correction"] == r["correction"]
            else:
                assert b["correction"] is None
        if [b for b in rules if not b["verified"]]:
            n = sum(1 for b in rules if not b["verified"])
            assert quickhelp.NOTES["UNVERIFIED_SOME"].format(n=n) in [b.get("text") for b in a["blocks"]]
    assert seen > 50


def test_anchor_rules_come_first(owner):
    a = ask(owner, "When is my prelim due?")
    first = [b for b in a["blocks"] if b["type"] == "rule"][0]
    assert first["rule_id"] == "prelim-notice-20-day-deadline" and first["verified"] is False   # on point, unverified
    a = ask(owner, "Does an AC changeout need a HERS test?")
    assert [b for b in a["blocks"] if b["type"] == "rule"][0]["rule_id"] == "energy-hvac-changeout-hers"


def test_open_tool_args_match_the_schema(owner, app):
    import asyncio

    from app import api
    specs = {s["name"]: s for s in asyncio.run(api.tool_specs(app.state.platform))}
    for q, a in all_answers(owner):
        for b in a["blocks"]:
            if b.get("kind") != "open_tool":
                continue
            props = specs[b["tool"]]["input_schema"]["properties"]
            assert set(b["args"]) <= set(props), q
            assert b["filled"] == list(b["args"]) and b["label"] == f"Open {b['title']}"
            assert b["hint"] == quickhelp.NOTES["OPEN_TOOL_HINT"].format(button=quickhelp._final_button(b["tool"]))
            for k, v in b["args"].items():
                enum = quickhelp._enum(props[k])
                assert enum is None or v in enum, (q, k, v)
            required = specs[b["tool"]]["input_schema"].get("required", [])
            assert b["missing"] == [r for r in required if r not in b["args"]]


# ---------------------------------------------------------------------------- fixed copy and forbidden phrases
BANNED = ["guarantee", "replace your attorney", "replaces your attorney", "we recommend", "top-rated",
          "fully compliant",
          "best lawyer", "best engineer", "you don't need", "you do not need a", "no need for a", "you can skip",
          "skip the", "not required to hire", "as your attorney", "i recommend", "my advice"]


def own_strings(a, question):
    """Quick help's own words in an answer (the person's own words echoed in READ_DATE and choices cut out)."""
    out = [a["lead"]]
    for b in a["blocks"]:
        if b["type"] == "note":
            text = b["text"]
            if b["code"] == "READ_DATE":
                text = re.sub(r"“.*?”", "", text)
            out.append(text)
        if b["type"] == "action":
            out += [b.get("label", ""), b.get("hint", "")]
        if b["type"] == "requirement":
            out += [b["name"], b["situation"], b.get("legal_basis") or ""]
        if b["type"] == "maybe_professional":
            out += [b["name"], b["note"]]
    out += [str(u["display"]) for u in a["understood"]]
    out += [c["label"] for c in a["choices"]] + [f["label"] for f in a["followups"]]
    return [s.replace(question, "") for s in out]


def template_rx(text):
    return re.compile("^" + re.sub(r"\\\{\w+\\\}", ".+", re.escape(text)) + "$", re.S)


FIXED = [template_rx(t) for t in list(quickhelp.NOTES.values()) + list(quickhelp.LEADS.values())]


def fuzzed(n=200):
    rng = random.Random(7)
    d = load("quickhelp_questions.json")
    words = " ".join(i["q"] for i in d["answer"] + d["handoff"]).split()
    extra = ["skip", "permit", "lien", "AQI 300", "no", "engineer", "sue", "$5,000", "Nov 31", "13/45/2026", "C-99",
             "D-3", "you", "can", "we", "not", "really", "without", "a", "license", "José", "™", "'", "\"", "?"]
    return [" ".join(rng.choice(words + extra) for _ in range(rng.randint(1, 18))) for _ in range(n)]


def test_fixed_copy_and_no_forbidden_phrase(owner):
    never = [n.lower() for t in professionals.registry()["types"] for n in t.get("never_say", [])]
    answers = all_answers(owner) + [(q, ask(owner, q)) for q in fuzzed()]
    for q, a in answers:
        assert a["disclaimer"] == core.DISCLAIMER and a["rules_built_on"] == "2026-11-26"
        assert a["lead"] == "" or any(rx.match(a["lead"]) for rx in FIXED), a["lead"]
        for b in a["blocks"]:
            if b["type"] == "note":
                assert any(rx.match(b["text"]) for rx in FIXED), b
                assert b["code"] in quickhelp.NOTES
        for s in own_strings(a, q):
            low = s.lower()
            for bad in BANNED + never:
                assert bad not in low, (q, bad, s)


def test_not_found_offers_the_nearest_checks(owner):
    a = ask(owner, "zebra crossing stripes purple elephants")
    assert a["intent"] == "not_found" and a["choices"] == [] and a["confidence"] == "low"
    tools = [b for b in a["blocks"] if b.get("kind") == "open_tool"]
    assert len(tools) <= 3 and all(b["args"] == {} for b in tools)
    assert [b["code"] for b in a["blocks"] if b["type"] == "note"][:2] == ["NOT_FOUND", "NOT_FOUND_TRY"]


def test_company_names_fill_the_check(owner):
    a = ask(owner, "Is Ace Roofing's license still active?")
    tool = next(b for b in a["blocks"] if b.get("kind") == "open_tool")
    assert tool["args"] == {"license_number": "1023456"}
    assert f"#/subs?sub={owner.ace}" in [b.get("href") for b in a["blocks"]]
    a = ask(owner, "When do I have to record my lien on the Elm St ADU?")
    tool = next(b for b in a["blocks"] if b.get("kind") == "open_tool")
    assert tool["project_id"] == owner.elm and tool["href"] == f"#/tools/calculate_lien_deadline?project={owner.elm}"
    assert tool["args"]["last_furnishing_date"] == "2026-11-10"
    assert {"label": "Job", "value": str(owner.elm), "display": "Elm St ADU"} in a["understood"]
    by_id = ask(owner, "When do I have to record my lien?", project_id=owner.elm)
    assert next(b for b in by_id["blocks"] if b.get("kind") == "open_tool")["project_id"] == owner.elm
    with browser(owner) as other:
        signup(other, email="o@other.test", company="Other Co")
        theirs = other.post("/api/help", json={"question": "When do I have to record my lien?",
                                               "project_id": owner.elm})
        assert theirs.status_code == 200
        assert next(b for b in theirs.json()["blocks"] if b.get("kind") == "open_tool")["project_id"] is None
        assert "Elm St ADU" not in theirs.text and "Ace Roofing" not in other.post(
            "/api/help", json={"question": "Is Ace Roofing's license still active?"}).text


# ---------------------------------------------------------------------------- Claude, entitlement, roles
def test_claude_available_follows_assistant_status(owner, app):
    platform = app.state.platform
    q = "Should I sue the homeowner for the $18,000 they owe me?"
    cases = []
    a = ask(owner, q)
    cases.append(a)
    assert a["claude"]["available"] is True and [b for b in a["blocks"] if b.get("kind") == "ask_claude"]
    assert a["route"] == "claude"
    platform.env = {}                                                       # not_configured
    a = ask(owner, q)
    assert a["claude"]["available"] is False and not [b for b in a["blocks"] if b.get("kind") == "ask_claude"]
    assert a["route"] == "professional"
    assert ask(owner, "What does Nevada require for a contractor license?")["blocks"][-1]["code"] == "CLAUDE_OFF"
    platform.env = {"ANTHROPIC_API_KEY": "sk-ant-test-not-real"}
    platform.store.modify_tenant_settings(owner.tenant_id, lambda s: {**s, "assistant_off": True})   # turned_off
    assert ask(owner, q)["claude"]["available"] is False
    platform.store.modify_tenant_settings(owner.tenant_id, lambda s: {**s, "assistant_off": False})
    platform.store.update_tenant(owner.tenant_id, status="canceled")                 # no_subscription
    tenant = platform.store.get_tenant(owner.tenant_id)
    a = ask(owner, q)
    assert a["claude"]["available"] is False
    from app.auth import Principal
    for reason_env, expected in (({}, "not_configured"),):
        platform.env = reason_env
        st = assistant.status(platform, Principal(owner.tenant_id, "owner", "cookie", 1), tenant)
        assert st["reason"] == expected and ask(owner, q)["claude"]["available"] is st["available"]


def test_not_entitled_keeps_actions_and_drops_rule_text(owner, app):
    app.state.platform.store.update_tenant(owner.tenant_id, status="canceled")
    for q in ("When is my prelim due if I first delivered lumber on Nov 3?",
              "do I need an engineer to design sloping for an excavation deeper than 20 feet"):
        a = ask(owner, q)
        assert a["entitled"] is False and a["more_rules"] == []
        assert not [b for b in a["blocks"] if b["type"] in ("rule", "requirement")]
        assert "SUBSCRIBE" in [b.get("code") for b in a["blocks"]]
        assert [b for b in a["blocks"] if b["type"] == "action"]
        assert "rules" not in a["lead"] and "Here is when the law requires" not in a["lead"]


def test_go_actions_follow_the_role(owner):
    with browser(owner) as field:
        join(field, owner, "crew@acme.test", role="field")
        a = ask(field, "where is billing")
        assert [b["code"] for b in a["blocks"] if b["type"] == "note"] == ["ROLE_OWNER"]
        assert not [b for b in a["blocks"] if b["type"] == "action"]
        a = ask(field, "how do I add a project")
        assert [b["code"] for b in a["blocks"] if b["type"] == "note"] == ["ROLE_OFFICE"]
        with browser(owner) as office:
            join(office, owner, "office@acme.test", role="office")
            assert [b["code"] for b in ask(office, "where is billing")["blocks"]] == ["ROLE_OWNER"]
            assert ask(office, "how do I add a project")["blocks"][0]["href"] == "#/project/new"
    assert ask(owner, "where is billing")["blocks"][0]["href"] == "#/settings?section=billing"


# ---------------------------------------------------------------------------- privacy, limits, inputs
def test_nothing_typed_reaches_the_log(owner, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    secret = "Elm St ADU license 1023456 Zyxwvut"
    ask(owner, f"When do I have to record my lien on the {secret}?")

    def boom(*a, **k):
        raise ValueError("parsing failed")
    monkeypatch.setattr(quickhelp, "slots", boom)
    a = ask(owner, f"prelim for the {secret}")
    assert a["intent"] == "not_found"
    text = " ".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
    for part in ("Zyxwvut", "1023456", "Elm St", "record my lien", "prelim for"):
        assert part not in text
    assert "quickhelp failed intent=- exc=ValueError" in text
    assert re.search(r"intent=\S+ route=\S+ blocks=\d+ ms=", text)


def test_inputs_and_limits(owner, app):
    assert owner.post("/api/help", json={"question": ""}).status_code == 422
    assert owner.post("/api/help", json={"question": "   "}).status_code == 422
    assert owner.post("/api/help", json={"question": "x" * 2001}).status_code == 422
    assert owner.post("/api/help", json={"question": "x", "secret": 1}).status_code == 422
    assert owner.post("/api/help", json={"question": "x", "page": "nowhere"}).status_code == 422
    assert owner.post("/api/help", json={"question": "When is my prelim due?", "page": "rule"}).status_code == 200
    long = "When is my prelim due? " + "and more words " * 60
    a = ask(owner, long[:2000])
    assert a["blocks"][-1]["code"] == "TOO_LONG" and a["intent"] == "lien"
    a = ask(owner, "When is my prelim due?", tool="calculate_lien_deadline")
    assert a["intent"] == "lien"
    assert ask(owner, "When is my prelim due?", tool="no_such_tool")["intent"] == "lien"
    assert re.fullmatch(r"qh_[0-9a-f]{8}", a["answer_id"])
    app.state.platform.help_user_limiter = RateLimiter(limit=60, window_s=60)
    for _ in range(60):
        assert owner.post("/api/help", json={"question": "hi"}).status_code == 200
    r = owner.post("/api/help", json={"question": "hi"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


def test_answers_are_fast_enough(owner):
    """CI ceiling only (scripts/bench_search.py has the real numbers)."""
    import time
    lat = []
    for it in load("quickhelp_questions.json")["answer"]:
        t = time.perf_counter()
        ask(owner, it["q"])
        lat.append(time.perf_counter() - t)
    lat.sort()
    assert lat[int(len(lat) * 0.95)] < 0.25


def test_people_only(owner):
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    tokens = oauth_connect(owner)["tokens"]
    with browser(owner) as bot:
        r = bot.post("/api/help", json={"question": "prelim"}, headers={"authorization": f"Bearer {key}"})
        assert r.status_code == 403 and r.json()["detail"] == "Quick help and search are for people signed in to " \
                                                              "TradeComply."
        r = bot.post("/api/help", json={"question": "prelim"},
                     headers={"authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 401
    r = owner.post("/api/help", json={"question": "When is my prelim due?"})
    assert r.headers["cache-control"] == "no-store" and "content-encoding" not in r.headers
