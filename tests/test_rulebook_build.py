"""scripts/build_rulebook.py: reproduces the hand-compiled rulebook from the 18 legacy packs, names legacy
duplicate ids exactly as before, refuses new duplicate ids, skips unreadable packs and writes atomically."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RULEBOOK = ROOT / "tradecomply" / "data" / "rulebook.json"

_spec = importlib.util.spec_from_file_location("build_rulebook", ROOT / "scripts" / "build_rulebook.py")
br = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("build_rulebook", br)
_spec.loader.exec_module(br)

# Exactly the packs the hand-compiled rulebook came from, in build order. Packs other agents add later are
# deliberately not part of this proof.
LEGACY = [
    "lawbook_p001-045", "lawbook_p046-095", "lawbook_p096-155", "lawbook_p156-215", "lawbook_p216-270",
    "lawbook_p271-325", "lawbook_p326-380", "lawbook_p381-430", "lawbook_p431-480", "lawbook_p481-540",
    "lawbook_p541-604", "web_business-tax-insurance", "web_calosha-programs", "web_labor-hr", "web_lien-payment",
    "web_permits-building", "web_project-management", "web_public-works",
]
LEGACY_FILES = [ROOT / "research" / f"{p}.verified.json" for p in LEGACY]


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _no_warnings(msg):
    raise AssertionError(f"unexpected warning: {msg}")


def _pack(path: Path, rules, **top) -> Path:
    path.write_text(json.dumps({"domain": "test", "rules": rules, "refuted": [], **top}), encoding="utf-8")
    return path


def _rule(rid, status=None, correction="", **kw):
    r = {"id": rid, "title": f"T {rid}", "plain_english": "p", "category": "deadline", "parameters": {"days": 1},
         "applies_to": "a", "citation": "c", "source_location": "s", "verbatim_quote": "q", "effective": "",
         "read_from_primary_text": True, "notes": "", **kw}
    if status:
        r["verification"] = {"status": status, "checked_against": "x", "correction": correction, "note": "n"}
    return r


@pytest.fixture(scope="module")
def legacy_rules():
    return br.build_rules(LEGACY_FILES, warn=_no_warnings)


def test_legacy_pack_list_matches_script():
    assert tuple(LEGACY) == br.LEGACY_PACKS
    assert all(p.is_file() for p in LEGACY_FILES)


def test_18_legacy_packs_reproduce_current_rules_byte_for_byte(legacy_rules):
    current = json.loads(RULEBOOK.read_bytes())
    # Later packs may add rules to the rulebook; the legacy packs' rules must still be exactly these, in order.
    legacy_current = [r for r in current["rules"] if r["pack"] in set(LEGACY)]
    assert len(legacy_rules) == len(legacy_current)
    assert _dumps(legacy_rules) == _dumps(legacy_current)
    if {r["pack"] for r in current["rules"]} <= set(LEGACY):  # rulebook built from the legacy packs only
        assert br.serialize(br.make_rulebook(legacy_rules, current["built_on"])) == RULEBOOK.read_bytes()


def test_rulebook_file_is_in_builder_format():
    raw = RULEBOOK.read_bytes()
    assert br.serialize(json.loads(raw)) == raw


def test_legacy_duplicate_ids_keep_their_pack_prefixed_names(legacy_rules):
    ids = [r["id"] for r in legacy_rules]
    assert len(ids) == len(set(ids))
    assert {i for i in ids if ":" in i} == br.LEGACY_PREFIXED_IDS
    by_id = {r["id"]: r for r in legacy_rules}
    # three packs share this id: the first keeps the bare id, the later two are prefixed with their pack
    assert by_id["asbestos-inspection-conflict-disclosure"]["pack"] not in {"web_calosha-programs",
                                                                            "web_permits-building"}
    assert by_id["web_permits-building:asbestos-inspection-conflict-disclosure"]["pack"] == "web_permits-building"


def test_status_correction_checked_on_derivation(legacy_rules):
    raw = {(p, r["id"]): r for p, f in zip(LEGACY, LEGACY_FILES) for r in json.loads(f.read_text())["rules"]}
    for r in legacy_rules:
        src = raw[(r["pack"], r["id"].split(":", 1)[-1])]
        v = src.get("verification") or {}
        assert r["status"] == v.get("status", "unchecked")
        assert r["correction"] == (v.get("correction") or None)
        assert r["checked_on"] == ("2026-11-25" if r["status"] in {"confirmed", "corrected"} else None)
        assert (r["source"], r["quote"], r["notes"]) == (src["source_location"], src["verbatim_quote"] or None,
                                                         src["notes"] or None)


def test_new_id_colliding_with_legacy_id_raises_naming_both_packs(tmp_path, legacy_rules):
    new = _pack(tmp_path / "osha2_new-pack.verified.json", [_rule("osha2-ok"), _rule("hic-down-payment-cap")])
    with pytest.raises(br.DuplicateRuleIdError) as e:
        br.build_rules(LEGACY_FILES + [new], warn=_no_warnings)
    msg = str(e.value)
    assert "hic-down-payment-cap" in msg and "lawbook_p001-045" in msg and "osha2_new-pack" in msg


def test_new_ids_colliding_with_each_other_raise_naming_both_packs(tmp_path):
    a = _pack(tmp_path / "hr_a.verified.json", [_rule("hr-same")])
    b = _pack(tmp_path / "hr_b.verified.json", [_rule("hr-same")])
    with pytest.raises(br.DuplicateRuleIdError) as e:
        br.build_rules([a, b], warn=_no_warnings)
    assert "'hr_a'" in str(e.value) and "'hr_b'" in str(e.value)
    assert br.main(["--packs", str(a), str(b), "--out", str(tmp_path / "rb.json")]) == 2
    assert not (tmp_path / "rb.json").exists()


def test_unique_new_pack_is_added_and_legacy_names_unchanged(tmp_path, legacy_rules):
    new = _pack(tmp_path / "osha2_extra.verified.json", [_rule("osha2-a", "confirmed")],
                verification_meta={"verified_on": "2026-12-01"})
    rules = br.build_rules(LEGACY_FILES + [new], warn=_no_warnings)
    assert _dumps(rules[:-1]) == _dumps(legacy_rules)
    assert rules[-1]["id"] == "osha2-a" and rules[-1]["checked_on"] == "2026-12-01"


def test_refuted_excluded_and_fields_mapped(tmp_path):
    p = _pack(tmp_path / "x_pack.verified.json", [
        _rule("x-unchecked"),
        _rule("x-confirmed", "confirmed"),
        _rule("x-corrected", "corrected", correction="fixed the amount"),
        _rule("x-unverifiable", "unverifiable", correction="leginfo blocked"),
        _rule("x-refuted", "refuted", correction="wrong"),
    ], refuted=[_rule("x-in-refuted-list")], verification_summary={"checked_on": "2026-11-30"})
    rules = {r["id"]: r for r in br.build_rules([p], warn=_no_warnings)}
    assert list(rules) == ["x-unchecked", "x-confirmed", "x-corrected", "x-unverifiable"]
    assert [(r["status"], r["correction"], r["checked_on"]) for r in rules.values()] == [
        ("unchecked", None, None), ("confirmed", None, "2026-11-30"),
        ("corrected", "fixed the amount", "2026-11-30"), ("unverifiable", "leginfo blocked", None)]
    r = rules["x-confirmed"]
    assert list(r) == ["id", "title", "plain_english", "category", "parameters", "applies_to", "citation", "source",
                       "quote", "effective", "notes", "status", "correction", "checked_on", "pack"]
    assert (r["source"], r["quote"], r["notes"], r["effective"], r["pack"]) == ("s", "q", None, "", "x_pack")


def test_default_globs_sorted_and_skips_unparseable_pack(tmp_path):
    _pack(tmp_path / "b.verified.json", [_rule("b-1")])
    _pack(tmp_path / "a.verified.json", [_rule("a-1")])
    (tmp_path / "c.verified.json").write_text('{"rules": [{"id": "c-1"', encoding="utf-8")  # half-written
    (tmp_path / "d.verified.json").write_text("[]", encoding="utf-8")                      # not a pack
    _pack(tmp_path / "e.json", [_rule("e-unverified-pack")])                               # not *.verified.json
    _pack(tmp_path / ".f.verified.json", [_rule("f-hidden-temp")])                          # writer's temp file
    paths = br.discover_packs(tmp_path)
    assert [p.name for p in paths] == ["a.verified.json", "b.verified.json", "c.verified.json", "d.verified.json"]
    warnings = []
    rules = br.build_rules(paths, warn=warnings.append)
    assert [r["id"] for r in rules] == ["a-1", "b-1"]
    assert len(warnings) == 2 and "c.verified.json" in warnings[0] and "d.verified.json" in warnings[1]


def test_write_is_atomic_noop_and_check_ignores_built_on(tmp_path):
    p = _pack(tmp_path / "a.verified.json", [_rule("a-1", "confirmed")], verification_meta={"verified_on": "2026-11-25"})
    out = tmp_path / "data" / "rulebook.json"
    written, book = br.build_and_write([p], out, built_on="2026-11-25", warn=_no_warnings)
    assert written and out.read_bytes() == br.serialize(book)
    assert os.listdir(out.parent) == ["rulebook.json"]  # no temp files left behind
    before = os.stat(out)
    written, _ = br.build_and_write([p], out, built_on="2027-01-01", warn=_no_warnings)
    assert not written and os.stat(out).st_mtime_ns == before.st_mtime_ns and os.stat(out).st_ino == before.st_ino

    assert br.main(["--check", "--packs", str(p), "--out", str(out)]) == 0
    stamped = json.loads(out.read_bytes())
    stamped["built_on"] = "1999-01-01"  # built_on alone never makes the rulebook stale
    out.write_bytes(br.serialize(stamped))
    assert br.main(["--check", "--packs", str(p), "--out", str(out)]) == 0
    stamped["rules"][0]["title"] = "hand edit"
    out.write_bytes(br.serialize(stamped))
    assert br.main(["--check", "--packs", str(p), "--out", str(out)]) == 1
    assert br.main(["--check", "--packs", str(p), "--out", str(tmp_path / "missing.json")]) == 1
