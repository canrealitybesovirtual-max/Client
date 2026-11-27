"""tradecomply/search.py: tokenizer, vocabulary, BM25F index, typo and synonym handling, data files (spec 5.2, 7.3)."""
import asyncio
import json
import os
import re
import time

import pytest

from tradecomply import core
from tradecomply import search as E


@pytest.fixture(scope="module")
def specs():
    from fastmcp import Client

    from app.api import _attr
    from server import mcp

    async def go():
        async with Client(mcp) as c:
            tools = await c.list_tools()
        return [{"name": t.name, "title": t.title or t.name, "description": t.description or "",
                 "input_schema": _attr(t, "input_schema", "inputSchema")} for t in tools]
    return asyncio.run(go())


def all_docs(specs):
    from app import catalog
    from tradecomply import licensing, professionals
    with open(os.path.join(E.DATA, "integrations.json"), encoding="utf-8") as f:
        integrations = json.load(f)
    return (E.page_docs(catalog.PAGES) + E.tool_docs(specs, catalog.TOOLS) + E.rule_docs(core.all_rules())
            + E.professional_docs(professionals.registry(), professionals.handoff_data())
            + E.class_docs(licensing.class_table(), lambda rid: core.rule(rid).citation_text,
                           E.cslb_d_subcategories()["items"])
            + E.integration_docs(integrations))


@pytest.fixture(scope="module")
def ix(specs):
    t0 = time.perf_counter()
    index = E.build(all_docs(specs))
    index.build_seconds = time.perf_counter() - t0
    return index


def top(ix, q, t=None, n=5, typing=False):
    return [d.id for _, d, _ in ix.search(q, types={t} if t else None, limit=n, typing=typing)]


# ---------------------------------------------------------------------------- tokenizer
@pytest.mark.parametrize("text,want", [
    ("B&P 7159.5", ["bp", "7159.5"]), ("Bus. & Prof. Code 7031", ["bp", "7031"]),
    ("C-39", ["c39"]), ("c39", ["c39"]), ("C 39", ["c39"]), ("C-039", ["c39"]), ("B-2", ["b2"]),
    ("re-roof", ["reroof"]), ("$1,000", ["1000"]), ("W-9", ["w9"]), ("8 CCR 1541.1", ["8ccr", "1541.1"]),
    ("Cal/OSHA", ["calosha", "osha"]), ("1,500 sq ft", ["1500", "sqft"]), ("workers' comp", ["workerscomp"]),
    ("José Peña", ["jose", "pena"]),
])
def test_tokenizer_joins(text, want):
    assert E.tokens(text) == want


def test_a_and_b_join_only_with_a_hyphen():
    assert E.tokens("a 5 ft trench") == ["5", "ft", "trench"]
    assert E.tokens("B 2") == ["b", "2"]     # never joined without the hyphen


def test_stems_keep_acronyms_and_numbers():
    for w in ("hers", "casp", "noc", "swppp", "iipp", "rrp", "adu", "cobra"):
        assert E.stem(w) == w
    assert E.stem("roofing") == "roof" and E.stem("digging") == "dig" and E.stem("permits") == "permit"
    assert E.stem("7159.5") == "7159.5"


def test_fold_and_word_prefix_matching():
    assert E.match_rank("José Peña", "jose pena") >= 1
    assert E.match_rank("José Peña", "PEÑA") >= 1
    assert E.match_rank("Peña Concrete", "pe") == 2
    assert E.match_rank("Ace Roofing", "ace roofing") == 3
    assert E.match_rank("Ace Roofing", "roof") == 1
    assert E.match_rank("Ace Roofing", "oof") == 0


# ---------------------------------------------------------------------------- data files
def test_vocab_anchors_and_tools_exist(specs):
    names = {s["name"] for s in specs}
    ids = {r.id for r in core.all_rules()}
    v = E.vocab()
    assert len(v["groups"]) == 76               # 73 from the prototype + setup checklist, toolbox talk, integrations
    for g in v["groups"]:
        assert set(g["anchors"]) <= ids, g["concept"]
        assert set(g["tools"]) <= names, g["concept"]
        assert g["forms"] and all(isinstance(f, str) or set(f) == {"form", "weight"} for f in g["forms"])
        if g.get("page"):
            from app import catalog
            assert g["page"] in catalog.PAGE_BY_ID


def test_no_expansion_is_a_bare_number():
    for g in E.vocab()["groups"]:
        for e in g["expand"]:
            assert not re.fullmatch(r"[\d.\s]+", e), (g["concept"], e)
    for phrase, exps in E.synonyms().items():
        for e, _w in exps:
            assert not re.fullmatch(r"[\d.\s]+", e), (phrase, e)


def test_numbers_never_trigger_a_group():
    assert E.groups_in("10/20/2026") == []
    assert E.groups_in("we have 20 days and 1000 dollars and 5 ft") == []
    assert {g["concept"] for g in E.groups_in("when is the prelim due")} == {"preliminary notice (20-day)"}


def test_generic_forms_weigh_less():
    syn = E.synonyms()
    assert {w for _, w in syn["comp"]} == {0.3}
    assert {w for _, w in syn["workers comp"]} == {0.6}


def test_cslb_d_subcategories_file():
    d = E.cslb_d_subcategories()
    assert d["source"].startswith("https://www.cslb.ca.gov/") and d["fetched"] == "2026-11-27"
    assert len(d["items"]) == 64 and "D-24" not in {i["page_code"] for i in d["items"]}
    for i in d["items"]:
        assert re.fullmatch(r"D-\d{1,2}", i["page_code"]) and re.fullmatch(r"D-\d\d", i["code"])
        assert int(i["code"][2:]) == int(i["page_code"][2:])
    still = [i["page_code"] for i in d["items"] if E.d_still_issued(i)]
    assert len(still) == 28 and still[0] == "D-3" and still[-1] == "D-65"


# ---------------------------------------------------------------------------- index
def test_build_time_and_size(ix):
    assert ix.build_seconds < 5
    assert len(ix.docs) >= 3200
    kinds = {d.type for d in ix.docs}
    assert kinds == {"rule", "tool", "page", "professional", "class", "integration"}


def test_contractor_words_rank_the_right_things_first(ix):
    assert top(ix, "re-roof", "tool", 3)[0] in {"permit_required", "classification_for_scope",
                                                "energy_compliance_triggers"}
    assert "C-39" in top(ix, "re-roof", "class", 2)
    assert top(ix, "trench", "tool", 2)[0] in {"check_cal_osha_permits", "lookup_safety_standard"}
    assert "permit-trench-excavation-5ft" in top(ix, "trench", "rule", 3)
    assert top(ix, "lead paint", "tool", 1) == ["certifications_required"]
    assert "rrp-applicability-pre-1978" in top(ix, "lead paint", "rule", 3)
    assert top(ix, "porta potty", "tool", 1) == ["generate_safety_programs"]
    assert top(ix, "porta potty", "rule", 1) == ["toilets-1-per-20"]
    assert top(ix, "granny flat", n=1) == ["adu_rules"]
    assert all(d.startswith(("adu-", "jadu-")) for d in top(ix, "granny flat", "rule", 3))


@pytest.mark.parametrize("typo,fix", [("exacavation", "excavation"), ("prelimnary", "preliminary"),
                                      ("asbestoss", "asbesto")])
def test_typos_correct(ix, typo, fix):
    res, ex = ix.search_info(typo, limit=5)
    assert ex.fixes == {typo: fix}
    assert res and fix[:6] in json.dumps([d.id for _, d, _ in res])
    assert ix.surface(fix) in ("excavation", "preliminary", "asbestos")


def test_phrase_expansions_need_every_word(ix):
    ex = ix.expand("prelim")
    assert ("preliminari", "notic") not in [p[0] for p in ex.phrases]
    assert ("preliminary", "notic") in [p[0] for p in ex.phrases]
    acc, _ = ix.score(E.Expanded([], [p for p in ex.phrases if p[0] == ("preliminary", "notic")], 1, [], {}))
    for i in acc:
        words = set(E.tokens(" ".join(ix.docs[i].fields.values())))
        assert {"preliminary", "notic"} <= words


def test_work_caps(ix):
    q = " ".join(["trench", "permit", "roof", "lien", "notice", "asbestos", "heat", "shade", "water", "crane",
                  "scaffold", "ladder", "silica"])
    assert len(E.tokens(q)) == 13
    assert ix.expand(q).concepts == 12
    many_typos = "exacavationn prelimnaryy asbestosss scaffoldd laddderr"
    assert len(ix.expand(many_typos).fixes) <= 3


def test_professional_documents_never_hold_exception_text(ix):
    from tradecomply import professionals
    for t in professionals.registry()["types"]:
        doc = ix.docs[ix.by_id[("professional", t["id"])]]
        body = " ".join(doc.fields.values())
        for k in ("not_required_when", "optional_when"):
            for w in t.get(k, []):
                assert w["situation"] not in body


def test_deterministic(ix, specs):
    again = E.build(all_docs(specs))
    for q in ("prel", "trench permit", "C10", "granny flat", "stop notice"):
        assert [d.id for _, d, _ in ix.search(q, typing=True)] == [d.id for _, d, _ in again.search(q, typing=True)]
