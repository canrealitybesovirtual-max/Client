"""Measure the search index, field suggestions and Quick help (manual benchmark; docs/quick-help.md has the numbers).

    python scripts/bench_search.py            # about a minute; prints JSON

Prints: index build time and memory (tracemalloc, the index alone), then p50/p95/p99/max in milliseconds for
  palette       every keystroke of the 60 fixture questions through the index (typing mode), as /api/search does
  suggest       static lists filtered for 10 fields (what the browser does from the vocab) and the server's
                search-backed sources, and the company sources over a big company (500 projects, 3,000 subs,
                5,000 documents, 2,000 field logs) in a scratch SQLite database
  help          the whole Quick help answer for the 60 fixture questions, 5 rounds
  http          the same through the app in-process (TestClient): POST /api/search, /api/suggest, /api/help
Numbers depend on the machine; CI only asserts generous ceilings (tests/test_search_api.py, test_quickhelp.py).
"""
import asyncio
import gc
import json
import os
import random
import statistics
import sys
import tempfile
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("LICENSE_DB", "/nonexistent/cslb.sqlite")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


def stats(xs):
    return {"n": len(xs), "p50_ms": round(pct(xs, .5), 2), "p95_ms": round(pct(xs, .95), 2),
            "p99_ms": round(pct(xs, .99), 2), "max_ms": round(max(xs), 2), "mean_ms": round(statistics.mean(xs), 2)}


def timed(fn, *a, **k):
    t = time.perf_counter()
    fn(*a, **k)
    return (time.perf_counter() - t) * 1000


def questions():
    fx = os.path.join(ROOT, "tests", "fixtures")
    out = []
    with open(os.path.join(fx, "quickhelp_questions.json"), encoding="utf-8") as f:
        d = json.load(f)
        out += [i["q"] for i in d["answer"] + d["handoff"]]
    with open(os.path.join(fx, "quickhelp_holdout.json"), encoding="utf-8") as f:
        out += [i["q"] for i in json.load(f)["questions"]]
    with open(os.path.join(fx, "quickhelp_fresh.json"), encoding="utf-8") as f:
        out += [i["q"] for i in json.load(f)["questions"]]
    return out


def main():
    from datetime import date

    from fastapi.testclient import TestClient

    from app import quickhelp, search
    from app.auth import RateLimiter
    from app.config import Settings
    from app.main import create_app
    from tradecomply import search as engine

    out = {}
    tmp = tempfile.mkdtemp(prefix="tc-bench-")
    app = create_app(Settings.from_env(data_dir=tmp, database_url=None, events_background=False, search_warm=False))
    platform = app.state.platform
    specs, modules = asyncio.run(search._collect(platform))
    docs = search.build_docs(specs)
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    ix = engine.build(docs)
    build_ms = (time.perf_counter() - t0) * 1000
    current, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del ix
    gc.collect()
    t0 = time.perf_counter()
    built = search.build_now(platform)
    full_ms = (time.perf_counter() - t0) * 1000
    out["build"] = {"index_build_ms": round(build_ms), "build_now_ms_incl_docs_and_related": round(full_ms),
                    "docs": len(built.index.docs), "terms": len(built.index.vocab),
                    "postings": sum(len(p) for p in built.index.post.values()),
                    "typo_keys": len(built.index.del_index), "synonym_phrases": len(built.index.syn),
                    "index_traced_mb": round(current / 1e6, 1), "rules_with_related_checks": len(built.related)}

    qs = questions()
    lat = [timed(built.index.search, q[:i], limit=120, typing=True) for q in qs for i in range(2, len(q) + 1)]
    out["palette_keystroke"] = stats(lat)

    vocab = search.build_vocab(specs)
    fields = [("valley_fever_requirements.county", "kern"), ("construction_hours.city", "san diego"),
              ("check_subcontractor_compliance.required_classification", "roofing"),
              ("company_license_obligations.classifications", "c-10"), ("cobra_deadlines.qualifying_event", "laid off"),
              ("overhead_line_clearance.equipment", "boom truck"), ("apprentice_hours_required.craft", "electrical"),
              ("leave_request_check.leave_type", "jury"), ("truck_compliance.fuel", "renewable"),
              ("jobsite_hazard_checklist.tasks", "scissor lift")]
    fmap = search.field_map(specs, vocab.sources)
    lat = []
    for key, full in fields:
        items = vocab.sources[fmap[key]["source"]]["items"]
        lat += [timed(engine.match_options, items, full[:i], 8) for i in range(1, len(full) + 1)]
    out["suggest_static_keystroke"] = stats(lat)
    lat = []
    for source, full in (("scope_phrase", "water heater"), ("need", "stamped plans"), ("rule", "prelim notice")):
        lat += [timed(search.remote_items, source, full[:i], 9, True) for i in range(2, len(full) + 1)]
    out["suggest_search_backed_keystroke"] = stats(lat)

    # a big company in the scratch database
    rng = random.Random(7)
    words = "Elm Oak Pine Main Harbor Mission Sierra Valley Ridge Canyon Coast Bay Mesa Vista Grove Park".split()
    kinds = "ADU Remodel Reroof Warehouse TI School Clinic Retail Duplex Garage".split()
    trades = "Roofing Electric Plumbing Concrete Drywall Framing Paving Glass HVAC Solar Tile Paint".split()
    store = platform.store
    tenant = store.create_tenant("Bench Builders", "1012345", None)["id"]
    with store.connect() as con:
        for n in range(500):
            con.execute("insert into projects (tenant_id, name, city, county, created) values (?,?,?,?,?)",
                        (tenant, f"{rng.randint(100, 9999)} {rng.choice(words)} St {rng.choice(kinds)}",
                         rng.choice(["Oakland", "Fresno", "San Diego", "Irvine"]), "Alameda", "2026-01-01"))
        for n in range(3000):
            con.execute("insert into subs (tenant_id, name, license_number, created) values (?,?,?,?)",
                        (tenant, f"{rng.choice(words)} {rng.choice(trades)} Inc", str(rng.randint(100000, 1199999)),
                         "2026-01-01"))
        for n in range(5000):
            con.execute("insert into documents (tenant_id, title, created) values (?,?,?)",
                        (tenant, f"Check result {rng.choice(kinds)} {n}", "2026-01-01"))
        for n in range(2000):
            names = json.dumps([{"name": f"{rng.choice(words)} {rng.choice(trades)}"} for _ in range(6)])
            con.execute("insert into field_logs (tenant_id, kind, date, topic, attendees, created) "
                        "values (?,?,?,?,?,?)",
                        (tenant, rng.choice(["toolbox_talk", "inspection"]), "2026-11-01",
                         f"{rng.choice(trades)} safety", names, "2026-01-01"))
    from app.auth import Principal
    p = Principal(tenant_id=tenant, role="owner", via="cookie", user_id=1)
    lat = []
    for source, full in (("project", "oak st"), ("sub", "harbor roofing"), ("sub_license", "12345"),
                         ("crew_name", "mesa tile"), ("document", "check result school"), ("project_city", "fre")):
        lat += [timed(search.company_items, platform, p, source, full[:i]) for i in range(1, len(full) + 1)]
    out["suggest_company_keystroke_big_company"] = stats(lat)

    company, _ = quickhelp.company_context(platform, p, None)
    ctx = quickhelp.Ctx(today=date(2026, 11, 27), role="owner", page=None, tool=None, project=None,
                        claude_available=True, entitled=True, built=built,
                        specs={s["name"]: s for s in specs}, company=company)
    lat = [timed(quickhelp.safe_answer, q, ctx) for _ in range(5) for q in qs]
    out["help_answer"] = stats(lat)

    platform.help_user_limiter = RateLimiter(limit=10 ** 6, window_s=60)
    platform.search_user_limiter = RateLimiter(limit=10 ** 6, window_s=60)
    platform.search_tenant_limiter = RateLimiter(limit=10 ** 6, window_s=60)
    platform.search_global_limiter = RateLimiter(limit=10 ** 6, window_s=60)
    with TestClient(app) as c:
        r = c.post("/api/auth/signup", json={"company_name": "Http Co", "email": "bench@bench.test",
                                             "password": "correct-horse-battery", "cslb_license": "1000001"})
        assert r.status_code == 200, r.text
        c.headers["x-csrf-token"] = c.cookies.get("tc_csrf")
        for name in ("search", "suggest", "help"):
            lat = []
            for q in qs[:30]:
                if name == "search":
                    body, path = {"q": q[:60]}, "/api/search"
                elif name == "suggest":
                    body, path = {"source": "project", "q": q[:10]}, "/api/suggest"
                else:
                    body, path = {"question": q}, "/api/help"
                t = time.perf_counter()
                assert c.post(path, json=body).status_code == 200
                lat.append((time.perf_counter() - t) * 1000)
            out[f"http_{name}"] = stats(lat)
        vocab_ms = timed(c.get, "/api/suggest/vocab")
        r = c.get("/api/suggest/vocab", headers={"accept-encoding": "identity"})
        import gzip
        out["vocab"] = {"first_request_ms": round(vocab_ms, 1), "bytes": len(r.content),
                        "gzip_bytes": len(gzip.compress(r.content, 6))}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
