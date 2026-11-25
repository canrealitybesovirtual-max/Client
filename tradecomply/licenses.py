"""License record sources. CSLB offers no official lookup API, so the server reads from a source you control:
  - built-in fixtures (demo, offline)
  - a CSV file built from CSLB's free Public Data Portal (license master + workers' comp lists),
    https://www.cslb.ca.gov/onlineservices/dataportal/  (set LICENSE_CSV=/path/file.csv)
Never scrape the CheckLicense web page: its terms and CAPTCHAs make that fragile and possibly disallowed."""
import csv
import os
from functools import lru_cache

FIXTURES: dict[str, dict] = {
    "123456": {
        "license_number": "123456", "business_name": "Example Plumbing Inc.", "status": "active",
        "classifications": ["C-36 Plumbing"], "expiration_date": "2027-06-30",
        "workers_comp": "on_file", "contractor_bond_on_file": True, "disciplinary_actions": [],
    },
    "999999": {
        "license_number": "999999", "business_name": "Lapsed Builders LLC", "status": "expired",
        "classifications": ["B General Building"], "expiration_date": "2024-01-15",
        "workers_comp": "none", "contractor_bond_on_file": False,
        "disciplinary_actions": ["2023 citation: contracting outside classification"],
    },
    "777000": {
        "license_number": "777000", "business_name": "Solo Roofing", "status": "active",
        "classifications": ["C-39 Roofing"], "expiration_date": "2027-03-31",
        "workers_comp": "exempt", "contractor_bond_on_file": True, "disciplinary_actions": [],
    },
    "555000": {
        "license_number": "555000", "business_name": "Solo Electric", "status": "active",
        "classifications": ["C-10 Electrical"], "expiration_date": "2026-12-31",
        "workers_comp": "exempt", "contractor_bond_on_file": True, "disciplinary_actions": [],
    },
}

STATUSES = {"active", "expired", "suspended", "revoked", "inactive", "unknown"}
WC = {"on_file", "exempt", "none", "unknown"}


def _norm(n: str) -> str:
    return "".join(ch for ch in (n or "") if ch.isdigit())


@lru_cache(maxsize=1)
def _csv_records(path: str) -> dict[str, dict]:
    """Columns: license_number,business_name,status,classifications (';'-separated),expiration_date,
    workers_comp (on_file|exempt|none),contractor_bond_on_file (true|false),disciplinary_actions (';'-separated)."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            num = _norm(row.get("license_number", ""))
            if not num:
                continue
            status = (row.get("status") or "unknown").strip().lower()
            wc = (row.get("workers_comp") or "unknown").strip().lower()
            out[num] = {
                "license_number": num,
                "business_name": (row.get("business_name") or "").strip() or "unknown",
                "status": status if status in STATUSES else "unknown",
                "classifications": [c.strip() for c in (row.get("classifications") or "").split(";") if c.strip()],
                "expiration_date": (row.get("expiration_date") or "").strip() or None,
                "workers_comp": wc if wc in WC else "unknown",
                "contractor_bond_on_file": (row.get("contractor_bond_on_file") or "").strip().lower() == "true",
                "disciplinary_actions": [d.strip() for d in (row.get("disciplinary_actions") or "").split(";") if d.strip()],
            }
    return out


def find(license_number: str) -> tuple[dict | None, str]:
    """Returns (record, source_name). Order: CSLB SQLite (real data), custom CSV, demo fixtures."""
    from . import cslb
    num = _norm(license_number)
    rec = cslb.lookup(num)
    if rec:
        return rec, f"CSLB master file (as of {rec.get('cslb_last_update')})"
    if cslb.meta():
        # Real data is loaded but this number isn't in it: don't fall back to demo records.
        return None, "CSLB master file" if cslb.is_complete() else "CSLB master file (partial copy)"
    path = os.getenv("LICENSE_CSV")
    if path and os.path.exists(path):
        rec = _csv_records(path).get(num)
        if rec:
            return rec, f"csv:{os.path.basename(path)}"
    rec = FIXTURES.get(num)
    return (dict(rec), "demo fixtures") if rec else (None, "none")
