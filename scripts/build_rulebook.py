#!/usr/bin/env python3
"""Compile tradecomply/data/rulebook.json from the verified research packs (research/*.verified.json).

    python scripts/build_rulebook.py            # rebuild; the file is rewritten only when its rules change
    python scripts/build_rulebook.py --check    # exit 1 if the committed rulebook differs from a fresh build
    python scripts/build_rulebook.py --packs research/a.verified.json ...   # explicit packs, in that order

How a pack rule becomes a rulebook rule. This reproduces, byte for byte, the hand-compiled rulebook of 2026-11-25
(2,094 rules from the 18 packs in LEGACY_PACKS); only "built_on" may differ.
  * Packs are read in sorted file-name order, rules in file order. Only a pack's "rules" list is read (its
    "refuted" list is not), and a rule whose verification.status is "refuted" is left out.
  * id, title, plain_english, category, parameters, applies_to, citation and effective are copied unchanged;
    source_location -> source and verbatim_quote -> quote. An empty quote or notes becomes null.
    Verifiers edit a rule in place, so a corrected rule already carries the corrected values.
  * status     = verification.status, or "unchecked" when the rule has no verification object (out of scope).
  * correction = verification.correction, or null when it is empty or missing (verification.note is not copied).
  * checked_on = for confirmed and corrected rules, the date the pack was verified (verification.checked_on or
                 verified_on on the rule if present, else the pack's verification_meta.verified_on or
                 verification_summary.checked_on); null for every other status.
  * pack       = the file name without ".verified.json".
  * ids: 50 legacy rules reused an id that an earlier pack already had and were named "pack:id"
    (LEGACY_PREFIXED_IDS). They keep exactly those names. Any other repeated id, in a new pack or a legacy one,
    is an error naming both packs: new packs must use unique id prefixes such as "osha2-" or "hr-".

Other agents may write packs, or run this script, at the same time: a pack that cannot be read or parsed is
skipped with a warning, builds are serialized with a lock, and the rulebook is written to a temporary file in the
same folder and moved into place with os.replace, so readers never see a partial file.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no build lock; the atomic replace still prevents torn files.
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
RESEARCH_DIR = ROOT / "research"
RULEBOOK_PATH = ROOT / "tradecomply" / "data" / "rulebook.json"
PACK_SUFFIX = ".verified.json"
BUILT_FROM = "research/*" + PACK_SUFFIX
VERIFIED = {"confirmed", "corrected"}
KNOWN_STATUSES = VERIFIED | {"unverifiable", "unchecked", "refuted"}

# The packs the hand-compiled rulebook was built from, in build order.
LEGACY_PACKS = (
    "lawbook_p001-045", "lawbook_p046-095", "lawbook_p096-155", "lawbook_p156-215", "lawbook_p216-270",
    "lawbook_p271-325", "lawbook_p326-380", "lawbook_p381-430", "lawbook_p431-480", "lawbook_p481-540",
    "lawbook_p541-604", "web_business-tax-insurance", "web_calosha-programs", "web_labor-hr", "web_lien-payment",
    "web_permits-building", "web_project-management", "web_public-works",
)

# Legacy rules whose id an earlier pack already used. Code and saved results may refer to these names, so they
# never change. Do not add to this list: give new rules unique ids instead.
LEGACY_PREFIXED_IDS = frozenset({
    "lawbook_p216-270:unlicensed-contracting-misdemeanor",
    "lawbook_p326-380:out-of-state-discipline-grounds",
    "lawbook_p381-430:home-improvement-contractor-definition",
    "lawbook_p381-430:home-improvement-definition",
    "lawbook_p381-430:his-registration-required",
    "lawbook_p381-430:his-registration-expiration",
    "lawbook_p381-430:hic-signed-copy-before-work",
    "lawbook_p541-604:citation-appeal-15-working-days",
    "web_business-tax-insurance:contractor-license-bond-25000",
    "web_business-tax-insurance:contractor-bond-beneficiaries",
    "web_business-tax-insurance:llc-liability-insurance-aggregate",
    "web_business-tax-insurance:disciplinary-bond-amount",
    "web_business-tax-insurance:wc-mandatory-classifications",
    "web_business-tax-insurance:report-convictions-90-days",
    "web_business-tax-insurance:fraudulent-license-number-use",
    "web_business-tax-insurance:minor-work-exemption-1000",
    "web_business-tax-insurance:unlicensed-citation-civil-penalty",
    "web_business-tax-insurance:city-business-license-cslb-statement",
    "web_business-tax-insurance:his-registration-required",
    "web_business-tax-insurance:his-registration-exemptions",
    "web_calosha-programs:asbestos-inspection-conflict-disclosure",
    "web_labor-hr:heat-cool-down-rest-5-minutes",
    "web_labor-hr:llc-worker-protection-bond-100000",
    "web_lien-payment:public-works-no-mechanics-lien",
    "web_lien-payment:hic-no-payment-ahead-of-work",
    "web_lien-payment:hic-mechanics-lien-warning",
    "web_permits-building:permit-applicant-license-statement",
    "web_permits-building:unpermitted-work-presumed-willful",
    "web_permits-building:asbestos-inspection-conflict-disclosure",
    "web_permits-building:asbestos-hiring-uncertified-penalty",
    "web_permits-building:usa-ticket-valid-28-days",
    "web_permits-building:usa-white-paint-delineation",
    "web_permits-building:usa-hand-tools-before-power-equipment",
    "web_permits-building:usa-damage-report-48-hours",
    "web_permits-building:usa-ticket-request-information",
    "web_permits-building:human-remains-stop-work-notify-coroner",
    "web_permits-building:well-completion-report-60-days",
    "web_project-management:hic-no-payment-ahead-of-work",
    "web_project-management:hic-downpayment-cap",
    "web_project-management:hic-salesperson-commission-pro-rata",
    "web_project-management:wc-lapse-automatic-suspension",
    "web_project-management:hic-approximate-completion-date",
    "web_project-management:records-retention-5-years",
    "web_project-management:structural-defect-definition",
    "web_project-management:roof-warranty-transferable",
    "web_project-management:hic-cgl-insurance-notice",
    "web_project-management:hic-workers-comp-notice",
    "web_project-management:llc-liability-insurance-minimum",
    "web_project-management:small-claims-limit-12500",
    "web_project-management:hic-signed-copy-before-work",
})

# Rulebook fields that must be a string or null for tradecomply.core.Rule to load them.
_TEXT_FIELDS = ("title", "plain_english", "category", "applies_to", "citation", "source", "quote", "effective",
                "notes", "status", "correction", "checked_on")


class RulebookError(Exception):
    """The rulebook cannot be built."""


class DuplicateRuleIdError(RulebookError, ValueError):
    """Two rules would get the same id."""


def _stderr(msg: str) -> None:
    print(f"build_rulebook: warning: {msg}", file=sys.stderr)


def pack_name(path) -> str:
    name = os.path.basename(os.fspath(path))
    return name[: -len(PACK_SUFFIX)] if name.endswith(PACK_SUFFIX) else os.path.splitext(name)[0]


def discover_packs(research_dir=RESEARCH_DIR) -> list[Path]:
    """research/*.verified.json sorted by file name (dot files, e.g. other writers' temp files, are not matched)."""
    found = glob.glob(os.path.join(glob.escape(os.fspath(research_dir)), "*" + PACK_SUFFIX))
    return sorted((Path(p) for p in found if os.path.isfile(p)), key=lambda p: p.name)


def _read_pack(path, warn) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:  # ValueError: JSONDecodeError and UnicodeDecodeError (e.g. half-written)
        warn(f"skipping {path}: cannot read or parse it ({e})")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        warn(f"skipping {path}: no top-level \"rules\" list")
        return None
    return data


def _pack_verified_on(data: dict) -> str | None:
    for key in ("verification_meta", "verification_summary"):
        meta = data.get(key)
        if isinstance(meta, dict):
            for date_key in ("verified_on", "checked_on"):
                if isinstance(meta.get(date_key), str) and meta[date_key]:
                    return meta[date_key]
    for date_key in ("verified_on", "checked_on"):
        if isinstance(data.get(date_key), str) and data[date_key]:
            return data[date_key]
    return None


def convert_rule(raw: dict, pack: str, pack_verified_on: str | None = None) -> dict:
    """One pack rule -> one rulebook rule (before duplicate-id naming). Key order is the rulebook's."""
    v = raw.get("verification")
    v = v if isinstance(v, dict) else {}
    status = v.get("status") or "unchecked"
    if isinstance(status, str):
        status = status.strip().lower()
    checked_on = None
    if status in VERIFIED:
        checked_on = v.get("checked_on") or v.get("verified_on") or pack_verified_on
    params = raw.get("parameters")
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "plain_english": raw.get("plain_english"),
        "category": raw.get("category"),
        "parameters": {} if params is None else params,
        "applies_to": raw.get("applies_to"),
        "citation": raw.get("citation"),
        "source": raw.get("source_location"),
        "quote": raw.get("verbatim_quote") or None,
        "effective": raw.get("effective"),
        "notes": raw.get("notes") or None,
        "status": status,
        "correction": v.get("correction") or None,
        "checked_on": checked_on,
        "pack": pack,
    }


def _problem(rule: dict) -> str | None:
    if not isinstance(rule["id"], str) or not rule["id"].strip():
        return "missing or non-string id"
    if not isinstance(rule["parameters"], dict):
        return "parameters is not a JSON object"
    for field in _TEXT_FIELDS:
        if rule[field] is not None and not isinstance(rule[field], str):
            return f"{field} is not a string"
    return None


def build_rules(pack_paths, warn=_stderr) -> list[dict]:
    """Rules from these pack files, in this order. Raises DuplicateRuleIdError on any non-legacy repeated id."""
    rules: list[dict] = []
    owner: dict[str, tuple[str, str]] = {}  # final id -> (pack, path) that holds it
    clashes: list[str] = []
    for path in pack_paths:
        data = _read_pack(path, warn)
        if data is None:
            continue
        pack, verified_on = pack_name(path), _pack_verified_on(data)
        undated = 0
        for i, raw in enumerate(data["rules"]):
            if not isinstance(raw, dict):
                warn(f"skipping rule #{i} in {path}: not a JSON object")
                continue
            if "verification" in raw and not isinstance(raw["verification"], dict):
                warn(f"rule {raw.get('id')!r} in {path}: verification is not an object; treated as unchecked")
            rule = convert_rule(raw, pack, verified_on)
            problem = _problem(rule)
            if problem:
                warn(f"skipping rule #{i} ({rule['id']!r}) in {path}: {problem}")
                continue
            if rule["status"] == "refuted":
                continue
            if rule["status"] not in KNOWN_STATUSES:
                warn(f"rule {rule['id']!r} in {path}: unknown verification status {rule['status']!r} "
                     f"(treated as unverified)")
            if rule["status"] in VERIFIED and not rule["checked_on"]:
                undated += 1
            if f"{pack}:{rule['id']}" in LEGACY_PREFIXED_IDS:
                rule["id"] = f"{pack}:{rule['id']}"
            rid = rule["id"]
            if rid in owner:
                first_pack, first_path = owner[rid]
                where = (f"twice in pack {pack!r} ({path})" if first_pack == pack else
                         f"in pack {first_pack!r} ({first_path}) and in pack {pack!r} ({path})")
                clashes.append(f"rule id {rid!r} is defined {where}")
            else:
                owner[rid] = (pack, os.fspath(path))
            rules.append(rule)
        if undated:
            warn(f"{path}: {undated} confirmed/corrected rule(s) have no verification date "
                 f"(verification_meta.verified_on); checked_on is null")
    if clashes:
        raise DuplicateRuleIdError(
            "duplicate rule ids:\n  " + "\n  ".join(clashes)
            + "\nEvery new rule id must be unique across research/*.verified.json; use a pack-specific prefix "
              "(e.g. \"osha2-\", \"hr-\").")
    return rules


def make_rulebook(rules: list[dict], built_on: str | None) -> dict:
    return {"built_from": BUILT_FROM, "built_on": built_on, "rules": rules}


def build_rulebook(pack_paths=None, built_on: str | None = None, warn=_stderr) -> dict:
    """The whole rulebook. pack_paths=None globs research/*.verified.json; built_on defaults to today."""
    paths = discover_packs() if pack_paths is None else list(pack_paths)
    return make_rulebook(build_rules(paths, warn), built_on or datetime.date.today().isoformat())


def serialize(book: dict) -> bytes:
    """The rulebook's on-disk format: compact, ASCII-escaped JSON with no trailing newline."""
    return json.dumps(book, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def write_atomic(path, data: bytes) -> None:
    """Write to a temp file in the same folder, fsync, then os.replace: readers see the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _build_lock(out_path):
    """Serialize concurrent builds of the same output, so a slower build cannot overwrite a newer one with rules
    read earlier. The lock file lives in the system temp folder, not the repository."""
    if fcntl is None:
        yield
        return
    key = hashlib.sha1(os.fspath(Path(out_path).resolve()).encode()).hexdigest()[:16]
    with open(os.path.join(tempfile.gettempdir(), f"tradecomply-build-rulebook-{key}.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_current(out_path) -> tuple[bytes | None, dict | None]:
    try:
        raw = Path(out_path).read_bytes()
    except OSError:
        return None, None
    try:
        book = json.loads(raw)
    except ValueError:
        return raw, None
    return raw, book if isinstance(book, dict) else None


def _diff_summary(old: dict | None, new: dict) -> str:
    if old is None or not isinstance(old.get("rules"), list):
        return "the current file is missing or is not a rulebook"
    old_rules = {r.get("id"): r for r in old["rules"] if isinstance(r, dict)}
    new_rules = {r["id"]: r for r in new["rules"]}
    added = [i for i in new_rules if i not in old_rules]
    removed = [i for i in old_rules if i not in new_rules]
    changed = [i for i in new_rules if i in old_rules and old_rules[i] != new_rules[i]]
    parts = [f"{len(added)} added", f"{len(removed)} removed", f"{len(changed)} changed"]
    if not (added or removed or changed):
        parts.append("rule order or file format differs" if old.get("rules") != new["rules"]
                     else "built_from or file format differs")
    sample = [f"+{i}" for i in added[:5]] + [f"-{i}" for i in removed[:5]] + [f"~{i}" for i in changed[:5]]
    return ", ".join(parts) + (f" (e.g. {' '.join(sample)})" if sample else "")


def check_rulebook(pack_paths=None, out_path=RULEBOOK_PATH, warn=_stderr) -> tuple[bool, str]:
    """(True, msg) when out_path equals a fresh build byte for byte, ignoring only built_on."""
    raw, current = _read_current(out_path)
    built_on = current.get("built_on") if current else None
    fresh = build_rulebook(pack_paths, built_on or "unknown", warn)
    if raw is not None and current is not None and serialize(fresh) == raw:
        return True, f"{out_path} is up to date ({len(fresh['rules'])} rules)"
    return False, f"{out_path} is stale: {_diff_summary(current, fresh)}. Run scripts/build_rulebook.py."


def build_and_write(pack_paths=None, out_path=RULEBOOK_PATH, built_on: str | None = None,
                    warn=_stderr) -> tuple[bool, dict]:
    """Build and write out_path atomically. Returns (written, rulebook). When the rules, order and format are
    unchanged the file is left alone (keeping its built_on), so a no-op rebuild leaves no diff."""
    with _build_lock(out_path):
        paths = discover_packs() if pack_paths is None else list(pack_paths)
        rules = build_rules(paths, warn)
        raw, current = _read_current(out_path)
        if raw is not None and current is not None and serialize(make_rulebook(rules, current.get("built_on"))) == raw:
            return False, make_rulebook(rules, current.get("built_on"))
        book = make_rulebook(rules, built_on or datetime.date.today().isoformat())
        write_atomic(out_path, serialize(book))
        return True, book


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compile tradecomply/data/rulebook.json from research/*.verified.json.")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the rulebook differs from a fresh build (built_on is ignored)")
    ap.add_argument("--packs", nargs="+", metavar="FILE",
                    help="build from these pack files, in this order, instead of research/*.verified.json")
    ap.add_argument("--out", default=os.fspath(RULEBOOK_PATH), help="rulebook path (default: %(default)s)")
    args = ap.parse_args(argv)
    packs = [Path(p) for p in args.packs] if args.packs else None
    try:
        if args.check:
            ok, msg = check_rulebook(packs, args.out)
            print(f"build_rulebook: {msg}", file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        written, book = build_and_write(packs, args.out)
    except RulebookError as e:
        print(f"build_rulebook: error: {e}", file=sys.stderr)
        return 2
    statuses = Counter(r["status"] for r in book["rules"])
    print(f"build_rulebook: {len(book['rules'])} rules from {len({r['pack'] for r in book['rules']})} packs "
          f"({', '.join(f'{k} {v}' for k, v in sorted(statuses.items()))}) -> {args.out} "
          f"[{'written' if written else 'unchanged'}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
