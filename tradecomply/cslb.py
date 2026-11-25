"""Real California license data from CSLB's free Public Data Portal (Master License file, updated daily).

  python -m tradecomply.cslb --download            # fetch the official CSV and build data/cslb.sqlite
  python -m tradecomply.cslb --file Master.csv     # build from a CSV you downloaded yourself

The master file covers licenses that are current or expired-but-renewable (cancelled/revoked ones are omitted).
It already includes workers' comp and bond data. Source page: https://www.cslb.ca.gov/onlineservices/dataportal/
"""
import argparse
import csv
import http.client
import io
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import urllib.request
from datetime import date, datetime

log = logging.getLogger("cslb")
DOWNLOAD_URL = "https://www.cslb.ca.gov/OnlineServices/DataPortal/DownLoadFile.ashx?fName=MasterLicenseData&type=C"
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cslb.sqlite")


def db_path(p: str | None = None) -> str:
    return p or os.getenv("LICENSE_DB") or _DEFAULT_DB
WC_ON_FILE = {"Workers' Compensation Insurance", "Leasing Firm, Temp Agency, etc", "Self-Insured"}


def _d(s: str) -> date | None:
    try:
        return datetime.strptime((s or "").strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def norm_class(code: str) -> str:
    """'C-10', 'C10 Electrical', 'c10' -> 'C10'."""
    return (code or "").strip().split(" ")[0].replace("-", "").upper()


def to_record(row: dict, today: date | None = None) -> dict | None:
    """One CSLB master-file row -> the server's license record."""
    num = "".join(ch for ch in row.get("LicenseNo", "") if ch.isdigit())
    if not num:
        return None
    today = today or date.today()
    primary = (row.get("PrimaryStatus") or "").strip()
    secondary = [s.strip() for s in (row.get("SecondaryStatus") or "").split("|") if s.strip()]
    exp = _d(row.get("ExpirationDate"))
    inact, react = _d(row.get("InactivationDate")), _d(row.get("ReactivationDate"))
    if "susp" in primary.lower():
        status = "suspended"
    elif inact and (not react or react < inact):
        status = "inactive"
    elif exp and exp < today:
        status = "expired"
    elif primary == "CLEAR":
        status = "active"
    else:
        status = "unknown"

    wc_type = (row.get("WorkersCompCoverageType") or "").strip()
    wc_exp = _d(row.get("WCExpirationDate"))
    if wc_type in WC_ON_FILE:
        wc = "on_file" if not wc_exp or wc_exp >= today else "none"
    elif wc_type == "Exempt":
        wc = "exempt"
    elif wc_type.startswith("License does not have current"):
        wc = "none"
    else:
        wc = "unknown"

    amount = (row.get("CBAmount") or "").strip()
    bond = amount.isdigit() and int(amount) > 0 and not (row.get("CBCancellationDate") or "").strip()
    flags = [f"CSLB status: {primary}"] if status == "suspended" else []
    flags += [f"Secondary status: {s}" for s in secondary]
    if (row.get("PendingSuspension") or "").strip():
        flags.append(f"Pending suspension on {row['PendingSuspension'].strip()}")
    if (row.get("PendingClassRemoval") or "").strip():
        flags.append(f"Classification removal pending ({row['PendingClassRemoval'].strip()})")
    return {
        "license_number": num,
        "business_name": (row.get("BusinessName") or "").strip() or "unknown",
        "status": status,
        "classifications": [c.strip() for c in (row.get("Classifications(s)") or "").split("|") if c.strip()],
        "expiration_date": exp.isoformat() if exp else None,
        "workers_comp": wc,
        "workers_comp_detail": wc_type or None,
        "contractor_bond_on_file": bool(bond),
        "bond_amount_usd": float(amount) if amount.isdigit() else None,
        "disciplinary_actions": flags,
        "city": (row.get("City") or "").strip() or None,
        "business_type": (row.get("BusinessType") or "").strip() or None,
        "cslb_last_update": (_d(row.get("LastUpdate")) or today).isoformat(),
    }


def build(csv_text_stream, db: str | None = None, truncated: bool = False, snapshot_date: str | None = None,
          only_if_more: bool = False) -> int:
    """Stream rows into a new SQLite file, then swap it in atomically. Skips a truncated last line."""
    db_path_ = db_path(db)
    os.makedirs(os.path.dirname(db_path_) or ".", exist_ok=True)
    tmp = db_path_ + ".building"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("create table licenses (license_number text primary key, rec text not null)")
    con.execute("create table meta (k text primary key, v text)")
    n, today = 0, date.today()
    batch = []
    for row in csv.DictReader(csv_text_stream):
        if None in row or any(v is None for v in row.values()):     # truncated or malformed line
            truncated = True
            continue
        rec = to_record(row, today)
        if rec:
            batch.append((rec["license_number"], json.dumps(rec, separators=(",", ":"))))
            n += 1
        if len(batch) >= 5000:
            con.executemany("insert or replace into licenses values (?,?)", batch)
            batch.clear()
    con.executemany("insert or replace into licenses values (?,?)", batch)
    con.execute("insert into meta values ('built_at', ?)", (datetime.now().isoformat(timespec="seconds"),))
    con.execute("insert into meta values ('rows', ?)", (str(n),))
    # CSLB's server ends the master-file download after ~150 s, so the file is often cut short. Record it, so a
    # missing license is reported as "not in our copy" instead of "not licensed".
    con.execute("insert into meta values ('complete', ?)", ("0" if truncated else "1",))
    con.execute("insert into meta values ('snapshot_date', ?)", (snapshot_date or date.today().isoformat(),))
    con.commit()
    con.close()
    have = int(meta(db).get("rows", 0))
    if n < 1000 or (only_if_more and n <= have and truncated):
        os.remove(tmp)
        raise RuntimeError(f"Only {n} licenses parsed (have {have}); keeping the existing database")
    os.replace(tmp, db_path_)
    return n


def download(db: str | None = None) -> int:
    req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "TradeComply-CA/1.0 (license data import)"})
    truncated = False
    with tempfile.TemporaryFile() as f:
        with urllib.request.urlopen(req, timeout=900) as r:
            try:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            except (http.client.IncompleteRead, ValueError, OSError) as e:
                truncated = True     # server closed the stream early; keep what arrived
                log.warning("CSLB download cut short after %s bytes: %s", f.tell(), type(e).__name__)
        f.seek(0)
        return build(io.TextIOWrapper(f, encoding="latin-1", newline=""), db, truncated=truncated, only_if_more=True)


SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed")


def build_from_seed(db: str | None = None) -> int:
    """Bundled snapshot (seed/cslb_master_YYYY-MM-DD.csv.gz) so a fresh host has data before any download works."""
    import glob
    import gzip
    files = sorted(glob.glob(os.path.join(SEED_DIR, "cslb_master_*.csv.gz")))
    if not files:
        return 0
    snap = os.path.basename(files[-1])[len("cslb_master_"):-len(".csv.gz")]
    with gzip.open(files[-1], "rt", encoding="utf-8", newline="") as f:
        return build(f, db, truncated=True, snapshot_date=snap)


def is_complete(db: str | None = None) -> bool:
    return meta(db).get("complete") == "1"


def lookup(license_number: str, db: str | None = None) -> dict | None:
    path = db_path(db)
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("select rec from licenses where license_number = ?", (license_number,)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        con.close()


def meta(db: str | None = None) -> dict:
    path = db_path(db)
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(con.execute("select k, v from meta").fetchall())
    finally:
        con.close()


def start_auto_refresh(db: str | None = None, max_age_hours: float = 20):
    """Background thread for hosted servers: build the database on boot if missing/stale, then refresh daily."""
    path = db_path(db)

    def loop():
        if not os.path.exists(path):
            try:
                log.info("CSLB seed snapshot loaded: %s licenses", build_from_seed(path))
            except Exception:
                log.exception("CSLB seed load failed")
        while True:
            try:
                age = time.time() - os.path.getmtime(path) if os.path.exists(path) else None
                if age is None or age > max_age_hours * 3600 or not is_complete(path):
                    log.info("downloading CSLB master file")
                    log.info("CSLB import done: %s licenses", download(path))
            except Exception:
                log.exception("CSLB refresh failed; keeping the existing data")
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True, name="cslb-refresh").start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--download", action="store_true")
    g.add_argument("--file")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    n = download(a.db) if a.download else build(open(a.file, encoding="latin-1", newline=""), a.db)
    print(f"{n} licenses -> {db_path(a.db)}")
