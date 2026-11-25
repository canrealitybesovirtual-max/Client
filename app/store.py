"""SQLite storage for the multi-tenant platform (stdlib sqlite3, no ORM).

One file at $DATA_DIR/tradecomply.sqlite. Every tenant-owned row carries tenant_id and every query on those tables
filters by it: the generic helpers below take tenant_id as a required argument and never build SQL from user input
(column names come from a fixed whitelist).
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERSION = 2

TENANT_STATUSES = ("trialing", "active", "past_due", "canceled")
ROLES = ("owner", "office", "field")
FIELD_LOG_KINDS = ("toolbox_talk", "inspection", "incident")
PROJECT_STATUSES = ("planned", "active", "completed", "closed")

MIGRATIONS: dict[int, str] = {
    1: """
    create table if not exists tenants (
        id text primary key,
        company_name text not null,
        cslb_license text,
        plan text not null default 'core',
        status text not null default 'trialing' check (status in ('trialing','active','past_due','canceled')),
        trial_ends text,
        stripe_customer_id text,
        stripe_subscription_id text,
        settings_json text not null default '{}',
        created text not null
    );
    create index if not exists tenants_stripe_customer on tenants(stripe_customer_id);
    create index if not exists tenants_stripe_sub on tenants(stripe_subscription_id);

    create table if not exists users (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        email text not null unique collate nocase,
        name text,
        pw_hash text not null,
        role text not null check (role in ('owner','office','field')),
        session_gen integer not null default 0,
        must_change_password integer not null default 0,
        created text not null,
        last_login text
    );
    create index if not exists users_tenant on users(tenant_id);

    create table if not exists api_keys (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text not null,
        key_hash text not null unique,
        prefix text not null,
        created_by integer,
        created text not null,
        last_used text,
        revoked integer not null default 0,
        revoked_at text
    );
    create index if not exists api_keys_tenant on api_keys(tenant_id);

    create table if not exists projects (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text not null,
        address text, city text, county text,
        owner_name text, lender_name text,
        public_or_private text not null default 'private' check (public_or_private in ('public','private')),
        contract_price real,
        first_furnishing_date text, last_furnishing_date text, completion_date text, noc_date text, start_date text,
        status text not null default 'active' check (status in ('planned','active','completed','closed')),
        extra text not null default '{}',
        created text not null, updated text
    );
    create index if not exists projects_tenant on projects(tenant_id);

    create table if not exists subs (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text not null,
        license_number text, scope text, required_class text, coi_expiry text,
        wc_required integer not null default 1,
        w9_on_file integer not null default 0,
        dir_number text, email text, phone text,
        project_id integer references projects(id) on delete set null,
        created text not null, updated text
    );
    create index if not exists subs_tenant on subs(tenant_id);

    create table if not exists documents (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        project_id integer references projects(id) on delete set null,
        kind text not null default 'note',
        title text not null,
        body_markdown text not null default '',
        source_tool text,
        verified integer,
        created text not null,
        created_by text
    );
    create index if not exists documents_tenant on documents(tenant_id);

    create table if not exists field_logs (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        project_id integer references projects(id) on delete set null,
        kind text not null check (kind in ('toolbox_talk','inspection','incident')),
        date text not null,
        topic text,
        attendees text not null default '[]',
        signatures text not null default '[]',
        notes text,
        client_uuid text,
        created_by text,
        created text not null,
        unique (tenant_id, client_uuid)
    );
    create index if not exists field_logs_tenant on field_logs(tenant_id, project_id, kind, date);

    create table if not exists audit (
        id integer primary key,
        tenant_id text,
        user text,
        tool text not null,
        args_json text,
        verified integer,
        ok integer,
        source text,
        created text not null
    );
    create index if not exists audit_tenant on audit(tenant_id, id);

    create table if not exists stripe_events (
        id text primary key,
        type text,
        created text not null
    );
    """,
    2: """
    create table if not exists revoked_sessions (
        nonce text primary key,
        expires integer not null
    );
    alter table tenants add column stripe_event_created integer not null default 0;
    """,
}

# Writable columns per tenant-owned table. Anything else in a payload is ignored.
COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("name", "address", "city", "county", "owner_name", "lender_name", "public_or_private",
                 "contract_price", "first_furnishing_date", "last_furnishing_date", "completion_date", "noc_date",
                 "start_date", "status", "extra"),
    "subs": ("name", "license_number", "scope", "required_class", "coi_expiry", "wc_required", "w9_on_file",
             "dir_number", "email", "phone", "project_id"),
    "documents": ("project_id", "kind", "title", "body_markdown", "source_tool", "verified", "created_by"),
    "field_logs": ("project_id", "kind", "date", "topic", "attendees", "signatures", "notes", "client_uuid",
                   "created_by"),
}
JSON_COLUMNS = {"projects": {"extra"}, "field_logs": {"attendees", "signatures"}}
BOOL_COLUMNS = {"subs": {"wc_required", "w9_on_file"}, "documents": {"verified"}}
HAS_UPDATED = {"projects", "subs"}


ORDERS = ("id desc", "id asc", "date desc", "date asc", "name asc")


def _check_table(table: str) -> None:
    """Table names are interpolated into SQL, so only the fixed tenant-owned tables are accepted."""
    if table not in COLUMNS:
        raise ValueError(f"bad table {table!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.getenv("DATA_DIR") or os.path.join(REPO_ROOT, "data")))


class Store:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir or default_data_dir()))
        os.makedirs(self.data_dir, exist_ok=True)
        self.path = os.path.join(self.data_dir, "tradecomply.sqlite")
        self._lock = threading.Lock()
        self.migrate()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("pragma foreign_keys = on")
        con.execute("pragma busy_timeout = 15000")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self):
        with self._lock, self.connect() as con:
            con.execute("pragma journal_mode = wal")
            version = con.execute("pragma user_version").fetchone()[0]
            for v in sorted(MIGRATIONS):
                if v > version:
                    con.executescript(MIGRATIONS[v])
                    con.execute(f"pragma user_version = {int(v)}")

    def schema_version(self) -> int:
        with self.connect() as con:
            return con.execute("pragma user_version").fetchone()[0]

    @staticmethod
    def _row(table: str, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for col in JSON_COLUMNS.get(table, ()):
            if col in d and isinstance(d[col], str):
                try:
                    d[col] = json.loads(d[col])
                except ValueError:
                    pass
        for col in BOOL_COLUMNS.get(table, ()):
            if col in d and d[col] is not None:
                d[col] = bool(d[col])
        if table == "tenants":
            try:
                d["settings"] = json.loads(d.pop("settings_json") or "{}")
            except ValueError:
                d["settings"] = {}
        return d

    @staticmethod
    def _clean(table: str, data: dict) -> dict:
        _check_table(table)
        out = {}
        for k, v in data.items():
            if k not in COLUMNS[table]:
                continue
            if k in JSON_COLUMNS.get(table, ()) and not isinstance(v, str):
                v = json.dumps(v if v is not None else ({} if k == "extra" else []), separators=(",", ":"))
            elif k in BOOL_COLUMNS.get(table, ()) and v is not None:
                v = 1 if v else 0
            out[k] = v
        return out

    # ------------------------------------------------------------------ generic tenant-scoped CRUD
    def list(self, table: str, tenant_id: str, where: dict | None = None, order: str = "id desc",
             limit: int = 1000) -> list[dict]:
        _check_table(table)
        if order not in ORDERS:
            raise ValueError(f"bad order {order!r}")
        sql, params = f"select * from {table} where tenant_id = ?", [tenant_id]
        for k, v in (where or {}).items():
            if k not in COLUMNS[table] and k != "id":
                raise ValueError(f"bad column {k!r}")
            sql += f" and {k} = ?"
            params.append(v)
        sql += f" order by {order} limit ?"
        params.append(int(limit))
        with self.connect() as con:
            return [self._row(table, r) for r in con.execute(sql, params).fetchall()]

    def get(self, table: str, tenant_id: str, row_id) -> dict | None:
        _check_table(table)
        with self.connect() as con:
            row = con.execute(f"select * from {table} where tenant_id = ? and id = ?", (tenant_id, row_id)).fetchone()
            return self._row(table, row)

    def insert(self, table: str, tenant_id: str, data: dict) -> dict:
        values = self._clean(table, data)
        values["tenant_id"] = tenant_id
        values["created"] = now_iso()
        cols = list(values)
        with self.connect() as con:
            cur = con.execute(f"insert into {table} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})",
                              [values[c] for c in cols])
            row = con.execute(f"select * from {table} where tenant_id = ? and id = ?",
                              (tenant_id, cur.lastrowid)).fetchone()
            return self._row(table, row)

    def update(self, table: str, tenant_id: str, row_id, data: dict) -> dict | None:
        values = self._clean(table, data)
        if table in HAS_UPDATED:
            values["updated"] = now_iso()
        if values:
            sets = ", ".join(f"{c} = ?" for c in values)
            with self.connect() as con:
                con.execute(f"update {table} set {sets} where tenant_id = ? and id = ?",
                            [*values.values(), tenant_id, row_id])
        return self.get(table, tenant_id, row_id)

    def delete(self, table: str, tenant_id: str, row_id) -> bool:
        _check_table(table)
        with self.connect() as con:
            return con.execute(f"delete from {table} where tenant_id = ? and id = ?",
                               (tenant_id, row_id)).rowcount > 0

    # ------------------------------------------------------------------ tenants
    def create_tenant(self, company_name: str, cslb_license: str | None, trial_ends: str | None,
                      status: str = "trialing", plan: str = "core") -> dict:
        tid = "t_" + secrets.token_hex(8)
        with self.connect() as con:
            con.execute("insert into tenants (id, company_name, cslb_license, plan, status, trial_ends, created) "
                        "values (?,?,?,?,?,?,?)", (tid, company_name, cslb_license, plan, status, trial_ends, now_iso()))
        return self.get_tenant(tid)

    def get_tenant(self, tenant_id: str) -> dict | None:
        with self.connect() as con:
            return self._row("tenants", con.execute("select * from tenants where id = ?", (tenant_id,)).fetchone())

    def tenant_by_stripe(self, customer_id: str | None = None, subscription_id: str | None = None) -> dict | None:
        with self.connect() as con:
            row = None
            if subscription_id:
                row = con.execute("select * from tenants where stripe_subscription_id = ?",
                                  (subscription_id,)).fetchone()
            if row is None and customer_id:
                row = con.execute("select * from tenants where stripe_customer_id = ?", (customer_id,)).fetchone()
            return self._row("tenants", row)

    def update_tenant(self, tenant_id: str, **fields) -> dict | None:
        allowed = {"company_name", "cslb_license", "plan", "status", "trial_ends", "stripe_customer_id",
                   "stripe_subscription_id", "stripe_event_created"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if "settings" in fields:
            values["settings_json"] = json.dumps(fields["settings"], separators=(",", ":"))
        if "status" in values and values["status"] not in TENANT_STATUSES:
            raise ValueError(f"bad tenant status {values['status']!r}")
        if values:
            with self.connect() as con:
                con.execute(f"update tenants set {', '.join(f'{k} = ?' for k in values)} where id = ?",
                            [*values.values(), tenant_id])
        return self.get_tenant(tenant_id)

    def count_tenants(self) -> int:
        with self.connect() as con:
            return con.execute("select count(*) from tenants").fetchone()[0]

    # ------------------------------------------------------------------ users
    def create_user(self, tenant_id: str, email: str, pw_hash: str, role: str, name: str | None = None,
                    must_change_password: bool = False) -> dict:
        if role not in ROLES:
            raise ValueError("role must be owner, office or field")
        with self.connect() as con:
            cur = con.execute("insert into users (tenant_id, email, name, pw_hash, role, must_change_password, created) "
                              "values (?,?,?,?,?,?,?)",
                              (tenant_id, email.strip().lower(), name, pw_hash, role, int(must_change_password),
                               now_iso()))
            return dict(con.execute("select * from users where id = ?", (cur.lastrowid,)).fetchone())

    def user_by_email(self, email: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from users where email = ?", ((email or "").strip().lower(),)).fetchone()
            return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from users where id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self, tenant_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("select id, email, name, role, created, last_login, must_change_password from users "
                               "where tenant_id = ? order by id", (tenant_id,)).fetchall()
            return [dict(r) for r in rows]

    def update_user(self, tenant_id: str, user_id: int, **fields) -> dict | None:
        allowed = {"pw_hash", "role", "name", "session_gen", "must_change_password", "last_login"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if "role" in values and values["role"] not in ROLES:
            raise ValueError("role must be owner, office or field")
        if values:
            with self.connect() as con:
                con.execute(f"update users set {', '.join(f'{k} = ?' for k in values)} where tenant_id = ? and id = ?",
                            [*values.values(), tenant_id, user_id])
        with self.connect() as con:
            row = con.execute("select * from users where tenant_id = ? and id = ?", (tenant_id, user_id)).fetchone()
            return dict(row) if row else None

    def delete_user(self, tenant_id: str, user_id: int) -> bool:
        with self.connect() as con:
            return con.execute("delete from users where tenant_id = ? and id = ?", (tenant_id, user_id)).rowcount > 0

    # ------------------------------------------------------------------ API keys
    def create_api_key(self, tenant_id: str, name: str, key_hash: str, prefix: str, created_by: int | None) -> dict:
        with self.connect() as con:
            cur = con.execute("insert into api_keys (tenant_id, name, key_hash, prefix, created_by, created) "
                              "values (?,?,?,?,?,?)", (tenant_id, name, key_hash, prefix, created_by, now_iso()))
            return self._key(con.execute("select * from api_keys where id = ?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _key(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d.pop("key_hash", None)          # never leaves the store
        d["revoked"] = bool(d["revoked"])
        return d

    def list_api_keys(self, tenant_id: str) -> list[dict]:
        with self.connect() as con:
            return [self._key(r) for r in con.execute("select * from api_keys where tenant_id = ? order by id desc",
                                                      (tenant_id,)).fetchall()]

    def revoke_api_key(self, tenant_id: str, key_id: int) -> bool:
        with self.connect() as con:
            return con.execute("update api_keys set revoked = 1, revoked_at = ? where tenant_id = ? and id = ? "
                               "and revoked = 0", (now_iso(), tenant_id, key_id)).rowcount > 0

    def revoke_user_api_keys(self, tenant_id: str, user_id: int) -> int:
        """Keys act for the company, not the person; when someone leaves (or loses office access) their keys go."""
        with self.connect() as con:
            return con.execute("update api_keys set revoked = 1, revoked_at = ? where tenant_id = ? and created_by = ? "
                               "and revoked = 0", (now_iso(), tenant_id, user_id)).rowcount

    def api_key_by_hash(self, key_hash: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from api_keys where key_hash = ?", (key_hash,)).fetchone()
            if row is None:
                return None
            d = self._key(row)
            if not d["revoked"]:
                con.execute("update api_keys set last_used = ? where id = ?", (now_iso(), d["id"]))
            return d

    # ------------------------------------------------------------------ sessions revoked by logout
    def revoke_session(self, nonce: str, expires: int) -> None:
        with self.connect() as con:
            con.execute("insert or ignore into revoked_sessions (nonce, expires) values (?, ?)", (nonce, int(expires)))
            con.execute("delete from revoked_sessions where expires < ?", (int(datetime.now(timezone.utc).timestamp()),))

    def session_revoked(self, nonce: str) -> bool:
        with self.connect() as con:
            return con.execute("select 1 from revoked_sessions where nonce = ?", (nonce,)).fetchone() is not None

    # ------------------------------------------------------------------ audit
    def audit(self, tenant_id: str | None, user: str | None, tool: str, args: dict | None, verified: bool | None,
              ok: bool = True, source: str = "api") -> None:
        with self.connect() as con:
            con.execute("insert into audit (tenant_id, user, tool, args_json, verified, ok, source, created) "
                        "values (?,?,?,?,?,?,?,?)",
                        (tenant_id, user, tool, json.dumps(args or {}, separators=(",", ":"), default=str)[:20000],
                         None if verified is None else int(bool(verified)), int(bool(ok)), source, now_iso()))

    def trim_anonymous_audit(self, keep: int) -> int:
        with self.connect() as con:
            row = con.execute("select id from audit where tenant_id is null order by id desc limit 1 offset ?",
                              (int(keep),)).fetchone()
            if not row:
                return 0
            return con.execute("delete from audit where tenant_id is null and id <= ?", (row[0],)).rowcount

    def list_audit(self, tenant_id: str, limit: int = 200) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("select * from audit where tenant_id = ? order by id desc limit ?",
                               (tenant_id, int(limit))).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["args"] = json.loads(d.pop("args_json") or "{}")
            except ValueError:
                d["args"] = {}
            d["verified"] = None if d["verified"] is None else bool(d["verified"])
            d["ok"] = bool(d["ok"])
            out.append(d)
        return out

    def get_audit(self, tenant_id: str, audit_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from audit where tenant_id = ? and id = ?", (tenant_id, audit_id)).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ field logs (idempotent offline sync)
    def insert_field_log_once(self, tenant_id: str, data: dict) -> tuple[dict, bool]:
        """Insert unless (tenant_id, client_uuid) already exists. Returns (row, created)."""
        uuid = data.get("client_uuid")
        if uuid:
            with self.connect() as con:
                row = con.execute("select * from field_logs where tenant_id = ? and client_uuid = ?",
                                  (tenant_id, uuid)).fetchone()
            if row:
                return self._row("field_logs", row), False
        try:
            return self.insert("field_logs", tenant_id, data), True
        except sqlite3.IntegrityError:
            with self.connect() as con:     # lost a race with a concurrent sync of the same log
                row = con.execute("select * from field_logs where tenant_id = ? and client_uuid = ?",
                                  (tenant_id, uuid)).fetchone()
            if row is None:
                raise
            return self._row("field_logs", row), False

    def last_field_log_dates(self, tenant_id: str, kind: str) -> dict[int, str]:
        with self.connect() as con:
            rows = con.execute("select project_id, max(date) from field_logs where tenant_id = ? and kind = ? "
                               "group by project_id", (tenant_id, kind)).fetchall()
            return {r[0]: r[1] for r in rows if r[0] is not None}

    # ------------------------------------------------------------------ subs
    def sub_by_license(self, tenant_id: str, license_number: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from subs where tenant_id = ? and license_number = ? order by id limit 1",
                              (tenant_id, license_number)).fetchone()
            return self._row("subs", row)

    # ------------------------------------------------------------------ stripe idempotency
    def stripe_event_seen(self, event_id: str, event_type: str) -> bool:
        """Records the event; returns True if it was already processed."""
        with self.connect() as con:
            try:
                con.execute("insert into stripe_events (id, type, created) values (?,?,?)",
                            (event_id, event_type, now_iso()))
                return False
            except sqlite3.IntegrityError:
                return True

    def forget_stripe_event(self, event_id: str) -> None:
        with self.connect() as con:
            con.execute("delete from stripe_events where id = ?", (event_id,))
