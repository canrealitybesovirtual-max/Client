"""tradecomply/data/professionals.json: the registry of licensed or certified people a result may need, and the
official public directories for finding and checking one. These checks keep it honest: only official links, no
ranking or recommending, confirmed legal bases only from an official source that was read, and a county URL pattern
only where one was tested."""
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = json.load(open(os.path.join(REPO, "tradecomply", "data", "professionals.json"), encoding="utf-8"))
RULES = {r["id"] for r in json.load(open(os.path.join(REPO, "tradecomply", "data", "rulebook.json")))["rules"]}
TYPES = DATA["types"]
BASIS_KEYS = ("when_required", "not_required_when", "optional_when")
# Hosts whose pages were actually read for a "confirmed" legal basis.
READ_HOSTS = ("www.cslb.ca.gov", "www.dir.ca.gov", "www.ecfr.gov", "dbs.lacity.gov", "www.sandiego.gov")
# Directory, review and lead-generation sites: never a "find" link.
AGGREGATORS = re.compile(r"yelp|angi|thumbtack|homeadvisor|houzz|avvo|justia|findlaw|martindale|superlawyers|"
                         r"lawyers\.com|bbb\.org|buildzoom|porch|google\.com/maps", re.I)


def _bases():
    for t in TYPES:
        for key in BASIS_KEYS:
            for b in t.get(key, []):
                yield t["id"], key, b


def test_every_type_has_the_fields_the_finder_shows():
    ids = [t["id"] for t in TYPES]
    assert len(ids) == len(set(ids))
    for t in TYPES:
        for field in ("id", "name", "plain_description", "find", "verify_license_how", "bring_or_ask", "never_say"):
            assert field in t, (t["id"], field)
        assert t["plain_description"].strip() and t["verify_license_how"].strip(), t["id"]


def test_find_links_are_official_https_and_honestly_labelled():
    for t in TYPES:
        for f in t["find"]:
            where = (t["id"], f["label"])
            assert f["url"].startswith("https://") and not AGGREGATORS.search(f["url"]), where
            assert f["official_owner"].strip(), where
            assert f["status"] == "reachable" or f["status"].startswith("blocked:"), where
            if f["status"] != "reachable":
                # Never present an unopened page's search fields or URL pattern as confirmed.
                assert f["search_by_confirmed"] is False, where
                assert f["url_pattern_with_county"] is None, where
                assert f.get("url_source"), where


def test_county_url_pattern_only_where_tested_and_codes_cover_all_58_counties():
    patterns = [(t["id"], f) for t in TYPES for f in t["find"] if f["url_pattern_with_county"]]
    assert patterns, "the DAS public works search is the one tested county pattern"
    for tid, f in patterns:
        assert f["status"] == "reachable", tid
        for name in re.findall(r"{(\w+)}", f["url_pattern_with_county"]):
            assert name in f["url_pattern_params"], (tid, name)
    assert len(DATA["counties"]) == 58 == len(set(DATA["counties"]))
    for codes in DATA["county_codes"].values():
        assert set(codes) == set(DATA["counties"])


def test_legal_bases_are_confirmed_only_from_a_source_that_was_read():
    for tid, key, b in _bases():
        assert b["status"] in ("confirmed", "unconfirmed"), (tid, key, b["situation"][:40])
        if b["status"] == "confirmed":
            assert b["source_url"] and b["source_url"].split("/")[2] in READ_HOSTS, (tid, b["source_url"])
        for rid in b.get("rule_ids") or []:
            assert rid in RULES, (tid, rid)


def test_no_recommending_ranking_or_contact_details():
    text = json.dumps({t["id"]: {k: v for k, v in t.items() if k != "never_say"} for t in TYPES})
    assert not re.search(r"\b(we recommend|recommended (firm|provider|lawyer|contractor)|top-rated|best (lawyer|"
                         r"engineer|firm|contractor))\b", text, re.I)
    raw = json.dumps(DATA)
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", raw)
    assert not re.search(r"\(?\b[2-9]\d{2}\)?[ .-]\d{3}[ .-]\d{4}\b", raw)
    for t in TYPES:
        assert any("recommend" in s.lower() for s in t["never_say"]), t["id"]


def test_professionals_the_law_does_not_require_are_not_listed_as_required():
    casp = next(t for t in TYPES if t["id"] == "casp")
    assert casp["when_required"] == [] and casp["optional_when"]
