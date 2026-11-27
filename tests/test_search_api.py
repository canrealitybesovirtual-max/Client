"""POST /api/search (spec 4.3, 8.2.4, 5.12) and the ReDoS bound on every pattern Quick help and search run."""
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import quickhelp, search
from tests.support import backend, browser, join, make_app, oauth_connect, signup  # noqa: F401
from tradecomply import search as engine

SITE = "https://tradecomply.example"


@pytest.fixture
def app(tmp_path):
    a = make_app(tmp_path, app_url=SITE)
    search.build_now(a.state.platform)
    return a


@pytest.fixture
def owner(app):
    with TestClient(app, base_url=SITE) as c:
        c.tenant_id = signup(c, company="Sierra Build Co")["tenant_id"]
        yield c


def find(c, q, **kw):
    r = c.post("/api/search", json={"q": q, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def group(body, t):
    return next((g["items"] for g in body["groups"] if g["type"] == t), [])


@pytest.mark.parametrize("q,t,first", [
    ("prelim", "tool", "calculate_lien_deadline"), ("stop notice", "tool", "stop_payment_notice_deadline"),
    ("coi", "tool", "check_subcontractor_compliance"), ("can I work saturday", "tool", "construction_hours"),
    ("C10", "class", "C-10"), ("porta potty", "tool", "generate_safety_programs"), ("granny flat", "tool", "adu_rules"),
    ("porta potty", "rule", "toilets-1-per-20"), ("trench permit", "rule", "permit-trench-excavation-5ft"),
])
def test_useful_first_results(owner, q, t, first):
    body = find(owner, q)
    assert body["ready"] is True and body["entitled"] is True
    assert group(body, t) and group(body, t)[0]["id"] == first, [i["id"] for i in group(body, t)]


def test_groups_order_quotas_and_shapes(owner):
    body = find(owner, "trench")
    types = [g["type"] for g in body["groups"]]
    assert types == [t for t in search.TYPES if t in types]
    for g in body["groups"]:
        assert g["label"] == search.GROUP_LABELS[g["type"]] and 0 < len(g["items"]) <= search.QUOTAS[g["type"]]
        for it in g["items"]:
            assert {"type", "id", "title", "detail", "href", "kind", "icon"} <= set(it) and it["href"].startswith("#/")
    rule = group(body, "rule")[0]
    assert rule["href"] == f"#/rule/{rule['id']}" and rule["kind"] in ("Rule · Verified", "Rule · Not yet verified")
    assert rule["verified"] == (rule["kind"] == "Rule · Verified") and rule["status"]
    tool = group(body, "tool")[0]
    assert tool["href"] == f"#/tools/{tool['id']}" and tool["kind"] == "Check" and tool["hidden"] is False
    assert body["nearest"] == [] and body["did_you_mean"] is None and body["took_ms"] >= 0


def test_class_results_link_to_their_scope_rule(owner):
    c10 = group(find(owner, "C10"), "class")[0]
    assert c10["href"] == "#/rule/class-c10-electrical-scope" and c10["detail"] == "CSLB class · 16 CCR 832.10"
    assert c10["kind"] == "License class"
    d = group(find(owner, "awnings"), "class")
    assert d and d[0]["id"] == "D-3" and d[0]["href"] == "#/rule/class-c61-limited-specialty-scope"


def test_types_limit_and_nearest(owner):
    body = find(owner, "prelim", types=["rule"])
    assert [g["type"] for g in body["groups"]] == ["rule"]
    near = find(owner, "zebra crossing paint stripes", types=["tool"])
    assert not group(near, "tool") and 0 < len(near["nearest"]) <= 3
    assert all(i["type"] == "tool" for i in near["nearest"])


def test_did_you_mean(owner):
    body = find(owner, "prelimnary")
    assert body["did_you_mean"] == {"q": "preliminary", "label": "preliminary"}
    assert group(body, "tool")[0]["id"] == "calculate_lien_deadline"


def test_company_groups_come_first_and_stay_in_the_company(owner):
    pid = owner.post("/api/projects", json={"name": "Elm St ADU", "city": "Oakland", "county": "Alameda"}).json()["id"]
    sid = owner.post("/api/subs", json={"name": "Elmhurst Electric", "license_number": "1055555"}).json()["id"]
    did = owner.post("/api/documents", json={"title": "Elm St toolbox talk", "body_markdown": "x"}).json()["id"]
    body = find(owner, "elm")
    assert [g["type"] for g in body["groups"]][:3] == ["project", "sub", "document"]
    assert group(body, "project")[0] == {"type": "project", "id": str(pid), "title": "Elm St ADU",
                                         "detail": "Oakland, Alameda County", "href": f"#/project/{pid}",
                                         "kind": "Project", "icon": "projects"}
    assert group(body, "sub")[0]["href"] == f"#/subs?sub={sid}"
    assert group(body, "document")[0]["href"] == f"#/document/{did}"
    assert group(find(owner, "1055555"), "sub")[0]["title"] == "Elmhurst Electric"
    with browser(owner) as other:
        signup(other, email="x@other.test", company="Other Co")
        raw = other.post("/api/search", json={"q": "elm"}).text
        for name in ("Elm St ADU", "Elmhurst Electric", "Elm St toolbox talk"):
            assert name not in raw


def test_go_to_rows_follow_the_role(owner):
    with browser(owner) as field:
        join(field, owner, "crew@acme.test", role="field")
        for q in ("billing", "team", "api key", "add a project"):
            pages = [i["id"] for i in group(find(field, q), "page")]
            assert not {"settings-billing", "settings-team", "settings-mcp", "project_new"} & set(pages), (q, pages)
        assert "settings-account" in [i["id"] for i in group(find(field, "password"), "page")]
    assert "settings-billing" in [i["id"] for i in group(find(owner, "billing"), "page")]


def test_no_rule_group_without_a_subscription(owner, app):
    app.state.platform.store.update_tenant(owner.tenant_id, status="canceled")
    body = find(owner, "prelim")
    assert body["entitled"] is False and "rule" not in [g["type"] for g in body["groups"]]
    assert group(body, "tool")                                     # checks and pages are still found


def test_people_only_no_gzip_and_bad_bodies(owner):
    key = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    tokens = oauth_connect(owner)["tokens"]
    with browser(owner) as bot:
        assert bot.post("/api/search", json={"q": "x"}, headers={"authorization": f"Bearer {key}"}).status_code == 403
        assert bot.post("/api/search", json={"q": "x"},
                        headers={"authorization": f"Bearer {tokens['access_token']}"}).status_code == 401
    r = owner.post("/api/search", json={"q": "trench"}, headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers           # company data: never compressed
    assert r.headers["cache-control"] == "no-store"
    for bad in ({"q": ""}, {"q": "   "}, {"q": "x" * 201}, {"q": "x", "types": ["secrets"]}, {"q": "x", "more": 1},
                {"q": "x", "context": {"page": "tools", "x": 1}}):
        assert owner.post("/api/search", json=bad).status_code == 422, bad


def test_search_is_fast_enough(owner):
    """CI ceiling only (the manual benchmark, scripts/bench_search.py, has the real numbers)."""
    lat = []
    for q in ("p", "pr", "pre", "prel", "preli", "prelim", "trench perm", "granny fl", "porta pot", "c-3", "set",
              "can i work sat", "stop not", "asbestos 150 sq", "valley fever ke", "lien deadline for a sub") * 2:
        t = time.perf_counter()
        assert owner.post("/api/search", json={"q": q}).status_code == 200
        lat.append(time.perf_counter() - t)
    lat.sort()
    assert lat[int(len(lat) * 0.95)] < 0.1


def test_work_caps():
    built = search.index()
    q = "trench permit roof lien notice asbestos heat shade water crane scaffold ladder silica"
    assert built.index.expand(q).concepts == 12


# ---------------------------------------------------------------------------- ReDoS
ADVERSARIAL = [
    "a" * 500, "1" * 500, " " * 499 + "x", "-" * 500, "/" * 500, "$" * 500, ("1," * 250), ("12:" * 166),
    ("lic " * 125), ("permit " * 71), ("without a " * 50), ("skip the " * 55), ("nov 3 " * 83), ("1/1/" * 125),
    ("c-" * 250), ("$1" * 250), ("aqi " * 125), ("(" * 250 + ")" * 250), ("work" + " " * 489 + "without"),
    ("connect " * 62), ("how do i " * 55), ("1.1." * 125), ("'" * 500), ("ft " * 166), ("d-0" * 166),
]


def all_patterns():
    pats = [quickhelp.GUARD, quickhelp.ACTION_WORDS, quickhelp.PICK_HINT, quickhelp.URGENT, quickhelp._FUTURE_WORDS]
    pats += [rx for _c, rx, _p in quickhelp.ROUTES] + [rx for rx, _p in quickhelp.NAV]
    pats += [rx for _n, rx, _t in quickhelp.INTENTS] + list(quickhelp.EXCLUDE.values())
    pats += [rx for rx, _t in quickhelp.NAMED_PRO + quickhelp.GUARD_PRO]
    pats += [rx for rx, _r, _n in engine._JOINS] + [engine._WORD]
    return pats


@pytest.mark.parametrize("text", ADVERSARIAL)
def test_redos(text):
    assert len(text) <= 500
    t = text.lower()
    for rx in all_patterns():
        t0 = time.perf_counter()
        rx.search(t)
        assert time.perf_counter() - t0 < 0.05, rx.pattern[:80]
    t0 = time.perf_counter()
    quickhelp.slots(text, __import__("datetime").date(2026, 11, 27), ["Kern"], [], [], [])
    for rx, st in quickhelp.STRUCTURES:
        re.search(rx, t)
    engine.tokens(text)
    assert time.perf_counter() - t0 < 0.05
