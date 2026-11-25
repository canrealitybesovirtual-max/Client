"""Storage for the multi-tenant platform: SQLite by default, PostgreSQL when DATABASE_URL is set (no ORM).

- SQLite (stdlib sqlite3): one file at $DATA_DIR/tradecomply.sqlite. Local/offline mode and the tests use it.
- PostgreSQL (psycopg 3): selected by a postgres:// or postgresql:// DATABASE_URL, for a hosted server whose disk is
  not persistent (Render's free plan). Works through Supabase's Supavisor pooler in transaction mode: no prepared
  statements, nothing kept in the session between transactions, the migration lock is transaction-scoped.

Both backends run the same SQL (written with `?` placeholders, translated for psycopg), return the same Python values
(booleans are 0/1 integers, JSON and timestamps are text) and raise the same IntegrityError. Every tenant-owned row
carries tenant_id and every query on those tables filters by it: the generic helpers below take tenant_id as a
required argument and never build SQL from user input (table and column names come from fixed whitelists).
"""
from __future__ import annotations

import functools
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger("tradecomply.store")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERSION = 3

TENANT_STATUSES = ("trialing", "active", "past_due", "canceled")
ROLES = ("owner", "office", "field")
FIELD_LOG_KINDS = ("toolbox_talk", "inspection", "incident")
PROJECT_STATUSES = ("planned", "active", "completed", "closed")

# Raised by both backends for unique / foreign-key / check violations. On SQLite it is sqlite3's own class; psycopg's
# IntegrityError is re-raised as PgIntegrityError, a subclass, so `except IntegrityError` works the same everywhere.
IntegrityError = sqlite3.IntegrityError


class PgIntegrityError(sqlite3.IntegrityError):
    """A PostgreSQL integrity violation (the original psycopg error is __cause__)."""


class StorageUnavailable(RuntimeError):
    """DATABASE_URL is set but the database can't be used. The app refuses to start rather than fall back to SQLite."""


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
    3: """
    create table if not exists app_secrets (
        name text primary key,
        value text not null,
        created text not null
    );
    """,
}

# The same schema for PostgreSQL. Differences, all chosen so both backends return the same values:
# - ids: `bigint generated by default as identity` (SQLite: integer primary key). Postgres never reuses an id.
# - email: unique index on lower(email) (SQLite: unique collate nocase); lookups use lower(email) = lower(?).
# - booleans: smallint 0/1, as SQLite stores them (raw queries and `revoked = 0` filters work unchanged).
# - JSON (settings_json, extra, attendees, signatures, args_json) and timestamps stay ISO-8601 text: the store
#   serializes them itself, so values round-trip byte for byte on both backends.
# - contract_price: double precision (Postgres `real` is 4 bytes; SQLite REAL is 8).
# - columns that are sorted or compared as text (names, field log dates) use collate "C": byte order, like SQLite.
# - "user" is a reserved word in Postgres, so the audit column is quoted (SQLite accepts the same quoting).
PG_MIGRATIONS: dict[int, str] = {
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
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        email text not null,
        name text,
        pw_hash text not null,
        role text not null check (role in ('owner','office','field')),
        session_gen integer not null default 0,
        must_change_password smallint not null default 0,
        created text not null,
        last_login text
    );
    create unique index if not exists users_email_lower on users (lower(email));
    create index if not exists users_tenant on users(tenant_id);

    create table if not exists api_keys (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text not null,
        key_hash text not null unique,
        prefix text not null,
        created_by bigint,
        created text not null,
        last_used text,
        revoked smallint not null default 0,
        revoked_at text
    );
    create index if not exists api_keys_tenant on api_keys(tenant_id);

    create table if not exists projects (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text collate "C" not null,
        address text, city text, county text,
        owner_name text, lender_name text,
        public_or_private text not null default 'private' check (public_or_private in ('public','private')),
        contract_price double precision,
        first_furnishing_date text, last_furnishing_date text, completion_date text, noc_date text, start_date text,
        status text not null default 'active' check (status in ('planned','active','completed','closed')),
        extra text not null default '{}',
        created text not null, updated text
    );
    create index if not exists projects_tenant on projects(tenant_id);

    create table if not exists subs (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text collate "C" not null,
        license_number text, scope text, required_class text, coi_expiry text,
        wc_required smallint not null default 1,
        w9_on_file smallint not null default 0,
        dir_number text, email text, phone text,
        project_id bigint references projects(id) on delete set null,
        created text not null, updated text
    );
    create index if not exists subs_tenant on subs(tenant_id);

    create table if not exists documents (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        project_id bigint references projects(id) on delete set null,
        kind text not null default 'note',
        title text not null,
        body_markdown text not null default '',
        source_tool text,
        verified smallint,
        created text not null,
        created_by text
    );
    create index if not exists documents_tenant on documents(tenant_id);

    create table if not exists field_logs (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        project_id bigint references projects(id) on delete set null,
        kind text not null check (kind in ('toolbox_talk','inspection','incident')),
        date text collate "C" not null,
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
        id bigint generated by default as identity primary key,
        tenant_id text,
        "user" text,
        tool text not null,
        args_json text,
        verified smallint,
        ok smallint,
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
        expires bigint not null
    );
    alter table tenants add column if not exists stripe_event_created bigint not null default 0;
    """,
    3: """
    create table if not exists app_secrets (
        name text primary key,
        value text not null,
        created text not null
    );
    """,
}
assert sorted(MIGRATIONS) == sorted(PG_MIGRATIONS) and max(MIGRATIONS) == SCHEMA_VERSION

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

MIN_SECRET_LEN = 32
MIGRATION_LOCK_ID = 7_310_442_871_201      # pg_advisory_xact_lock key for schema migrations (any fixed bigint)


def _check_table(table: str) -> None:
    """Table names are interpolated into SQL, so only the fixed tenant-owned tables are accepted."""
    if table not in COLUMNS:
        raise ValueError(f"bad table {table!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> str:
    return os.path.abspath(os.path.expanduser(os.getenv("DATA_DIR") or os.path.join(REPO_ROOT, "data")))


def database_url_from_env() -> str | None:
    """DATABASE_URL, or None when it is unset or blank (blank = SQLite, e.g. an empty Render env var)."""
    return (os.getenv("DATABASE_URL") or "").strip() or None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------- persistence of the SQLite file
def _mount_of(path: str) -> str:
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def disk_is_persistent(path: str) -> bool:
    """True when `path` is on its own mounted disk (a Render persistent disk is mounted at its mountPath). The
    container's root filesystem and memory filesystems are thrown away on every deploy or restart."""
    mount = _mount_of(path)
    if mount == "/":
        return False
    fstype = None
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1].replace("\\040", " ") == mount:
                    fstype = parts[2]           # the last entry for a mount point is the one in effect
    except OSError:
        return True                             # can't tell; it is a separate mount, so assume a real disk
    return fstype not in {"tmpfs", "ramfs", "overlay", "devtmpfs", "squashfs"}


# ---------------------------------------------------------------------------- PostgreSQL plumbing
@functools.lru_cache(maxsize=1024)
def to_pg(sql: str) -> str:
    """Rewrite a qmark (`?`) query for psycopg: `?` -> `%s`, a literal `%` -> `%%`. Text inside quotes ('...' or
    "...") keeps its `?` (only its `%` is doubled). Used only when the query has parameters; psycopg runs a query
    without parameters as is."""
    out, quote = [], None
    for ch in sql:
        if ch == "%":
            out.append("%%")
        elif quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _pg_params(params) -> list:
    # Booleans are stored as 0/1 smallints (as SQLite does); psycopg would send a Python bool as a Postgres boolean.
    return [int(p) if isinstance(p, bool) else p for p in params]


class Row:
    """sqlite3.Row look-alike for PostgreSQL rows: row[0], row["col"], dict(row), tuple(row) and row.keys()."""
    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names: tuple, index: dict, values: tuple):
        self._names, self._index, self._values = names, index, values

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._names == other._names and self._values == other._values
        return NotImplemented

    def __hash__(self):
        return hash((self._names, self._values))

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


def _row_factory(cursor):
    names = tuple(c.name for c in cursor.description) if cursor.description else ()
    index: dict[str, int] = {}
    for i, n in enumerate(names):
        index.setdefault(n, i)

    def make(values):
        return Row(names, index, tuple(values))
    return make


def pg_connect_kwargs(url: str, connect_timeout: int | None = None) -> dict:
    """psycopg.connect() arguments for DATABASE_URL. sslmode=require unless the URL (or PGSSLMODE) says otherwise;
    prepared statements off (Supavisor/PgBouncer transaction mode can't use them); a short connect timeout."""
    from psycopg.conninfo import conninfo_to_dict
    params = conninfo_to_dict(url)
    kw = {"autocommit": False, "prepare_threshold": None, "row_factory": _row_factory}
    if "connect_timeout" not in params:
        kw["connect_timeout"] = connect_timeout or _env_int("DATABASE_CONNECT_TIMEOUT", 5)
    if "sslmode" not in params and not os.getenv("PGSSLMODE"):
        kw["sslmode"] = "require"
    return kw


def describe_pg_url(url: str) -> str:
    """user@host:port/dbname for logs and errors, never the password."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        p = conninfo_to_dict(url)
    except Exception:       # noqa: BLE001 - a malformed URL is reported by the caller
        return "(unparseable DATABASE_URL)"
    host = p.get("host") or "localhost"
    if "@" in host:
        return "(malformed DATABASE_URL)"
    if "port" in p:
        host = f"{host}:{p['port']}"
    return f"{p.get('user') or '?'}@{host}/{p.get('dbname') or '?'}"


class _PgPool:
    """A small thread-safe connection pool. At most `max_size` connections are checked out at once (callers wait up
    to `timeout` seconds); up to `max_idle` are kept for reuse, each for at most `idle_s` seconds idle and
    `lifetime_s` in total, so connections a pooler or the server has dropped are rarely handed out (and when one is,
    _PgConnection reconnects)."""

    def __init__(self, url: str, kwargs: dict, max_size: int, max_idle: int, timeout: float = 30.0,
                 idle_s: float = 120.0, lifetime_s: float = 1800.0):
        self.url, self.kwargs = url, kwargs
        self.max_idle, self.timeout, self.idle_s, self.lifetime_s = max_idle, timeout, idle_s, lifetime_s
        self._slots = threading.BoundedSemaphore(max_size)
        self._lock = threading.Lock()
        self._idle: list[tuple] = []                  # (conn, born, last_used)

    def new(self):
        import psycopg
        return psycopg.connect(self.url, **self.kwargs), time.monotonic()

    def get(self):
        if not self._slots.acquire(timeout=self.timeout):
            raise StorageUnavailable(f"no free database connection after {self.timeout:.0f}s")
        try:
            now = time.monotonic()
            while True:
                with self._lock:
                    item = self._idle.pop() if self._idle else None
                if item is None:
                    return self.new()
                conn, born, last = item
                if conn.closed or conn.broken or now - last > self.idle_s or now - born > self.lifetime_s:
                    self._close(conn)
                    continue
                return conn, born
        except BaseException:
            self._slots.release()
            raise

    def put(self, conn, born: float, discard: bool = False) -> None:
        try:
            from psycopg.pq import TransactionStatus
            if discard or conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
                self._close(conn)
                return
            with self._lock:
                if len(self._idle) < self.max_idle:
                    self._idle.append((conn, born, time.monotonic()))
                    return
            self._close(conn)
        finally:
            self._slots.release()

    @staticmethod
    def _close(conn) -> None:
        try:
            conn.close()
        except Exception:       # noqa: BLE001 - closing a dead connection
            pass

    def close(self) -> None:
        """Close the idle connections. The pool stays usable (new connections are opened on demand)."""
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _, _ in idle:
            self._close(conn)


class _PgConnection:
    """One transaction on a pooled psycopg connection, with the sqlite3 calls the store uses: execute() with `?`
    placeholders, executescript(). If the connection turns out to be dead on the first statement (the pooler or
    server dropped it while it sat idle), that statement is retried once on a fresh connection: nothing of this
    transaction had run, so the retry is safe. A failure later in a transaction is raised (it rolls back)."""

    def __init__(self, pool: _PgPool):
        self._pool = pool
        self._conn, self._born = pool.get()
        self._started = False

    def _run(self, sql: str, params):
        import psycopg
        try:
            if params:
                return self._conn.execute(to_pg(sql), _pg_params(params))
            return self._conn.execute(sql)
        except psycopg.IntegrityError as e:
            raise PgIntegrityError(str(e).strip()) from e

    def execute(self, sql: str, params=()):
        import psycopg
        if self._started:
            return self._run(sql, params)
        self._started = True
        try:
            return self._run(sql, params)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if not (self._conn.closed or self._conn.broken):
                raise
            log.warning("database connection was dropped; reconnecting")
            _PgPool._close(self._conn)
            self._conn, self._born = self._pool.new()
            return self._run(sql, params)

    def executescript(self, script: str) -> None:
        self.execute(script)

    # used by Store.connect()
    def _finish(self, ok: bool) -> None:
        broken = False
        try:
            if ok:
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception:
            broken = True
            if ok:
                raise
        finally:
            self._pool.put(self._conn, self._born, discard=broken or self._conn.broken)


# ---------------------------------------------------------------------------- the store
class Store:
    def __init__(self, data_dir: str | None = None, database_url: str | None = None,
                 connect_attempts: int | None = None):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir or default_data_dir()))
        os.makedirs(self.data_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._persistent: bool | None = None
        self.database_url = (database_url or "").strip() or None
        if self.database_url is None:
            self.backend = "sqlite"
            self.path = os.path.join(self.data_dir, "tradecomply.sqlite")
            self._email_eq = "email = ?"            # unique collate nocase; emails are stored lowercased
            self.migrate()
        else:
            self.backend = "postgres"
            self.path = None
            self._email_eq = "lower(email) = lower(?)"   # matches the unique index on lower(email)
            self._open_pg(connect_attempts)

    # ------------------------------------------------------------------ plumbing
    def _open_pg(self, connect_attempts: int | None) -> None:
        url = self.database_url
        where = describe_pg_url(url)
        self.location = f"postgres {where}"
        if not url.startswith(("postgres://", "postgresql://")):
            raise StorageUnavailable("DATABASE_URL must start with postgres:// or postgresql:// "
                                     f"(got {url.split(':', 1)[0]!r}). Unset it to use SQLite.")
        try:
            import psycopg
            import psycopg.conninfo
        except ImportError as e:
            raise StorageUnavailable("DATABASE_URL is set but psycopg is not installed "
                                     "(pip install -r requirements.txt)") from e
        bad_url = StorageUnavailable(
            "DATABASE_URL is not a valid PostgreSQL URL. Characters such as @ : / % # ? in the password must be "
            "percent-encoded (e.g. @ as %40), or reset the database password to letters and digits.")
        try:            # libpq's parse errors can quote the password, so they are not repeated here
            kwargs = pg_connect_kwargs(url)
        except psycopg.Error:
            raise bad_url from None
        if "@" in (psycopg.conninfo.conninfo_to_dict(url).get("host") or ""):
            raise bad_url                    # an unencoded @ in the password: the "host" holds part of it
        self._pool = _PgPool(url, kwargs, max_size=max(1, _env_int("DATABASE_POOL_MAX", 10)),
                             max_idle=max(0, _env_int("DATABASE_POOL_IDLE", 4)))
        weakref.finalize(self, self._pool.close)
        attempts = max(1, connect_attempts or _env_int("DATABASE_CONNECT_ATTEMPTS", 3))
        for attempt in range(1, attempts + 1):
            try:
                self.migrate()
                return
            except psycopg.OperationalError as e:
                reason = " ".join(str(e).split()) or e.__class__.__name__
                if attempt < attempts:
                    log.warning("PostgreSQL at %s not reachable (attempt %d of %d): %s", where, attempt, attempts,
                                reason)
                    time.sleep(min(2 ** attempt, 5))
                    continue
                msg = (f"DATABASE_URL is set but PostgreSQL at {where} is unreachable ({reason}). Not starting: "
                       "customer data must never go to a local SQLite file instead. Fix DATABASE_URL (or unset it "
                       "to run on SQLite deliberately).")
                log.critical(msg)
                raise StorageUnavailable(msg) from None
            except (psycopg.Error, PgIntegrityError) as e:
                msg = f"PostgreSQL at {where}: schema migration failed: {' '.join(str(e).split())}"
                log.critical(msg)
                raise StorageUnavailable(msg) from e

    @contextmanager
    def connect(self):
        """One transaction: commits when the block ends, rolls back if it raises. Yields an object with sqlite3's
        execute(sql, params) / executescript(); rows support row[0], row["col"] and dict(row) on both backends."""
        if self.backend == "postgres":
            con = _PgConnection(self._pool)
            try:
                yield con
            except BaseException:
                con._finish(ok=False)
                raise
            con._finish(ok=True)
            return
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

    def close(self) -> None:
        """Release pooled database connections (the store stays usable). No-op on SQLite."""
        if self.backend == "postgres":
            self._pool.close()

    def migrate(self):
        if self.backend == "postgres":
            return self._migrate_pg()
        with self._lock, self.connect() as con:
            con.execute("pragma journal_mode = wal")
            version = con.execute("pragma user_version").fetchone()[0]
            for v in sorted(MIGRATIONS):
                if v > version:
                    con.executescript(MIGRATIONS[v])
                    con.execute(f"pragma user_version = {int(v)}")

    def _migrate_pg(self):
        """All pending migrations in one transaction, under a transaction-scoped advisory lock: a second instance
        starting at the same moment waits, then sees the new version and does nothing. (A session-level
        pg_advisory_lock would not be safe through a transaction-mode pooler; DDL is transactional in Postgres, so
        a failed migration leaves nothing half-applied.)"""
        with self._lock, self.connect() as con:
            con.execute("select pg_advisory_xact_lock(?)", (MIGRATION_LOCK_ID,))
            con.execute("create table if not exists schema_version (id smallint primary key default 1 "
                        "check (id = 1), version integer not null, updated text not null)")
            row = con.execute("select version from schema_version where id = 1").fetchone()
            version = row[0] if row else 0
            if row is None:
                con.execute("insert into schema_version (id, version, updated) values (1, 0, ?)", (now_iso(),))
            applied = [v for v in sorted(PG_MIGRATIONS) if v > version]
            for v in applied:
                con.executescript(PG_MIGRATIONS[v])
            if applied:
                con.execute("update schema_version set version = ?, updated = ? where id = 1",
                            (applied[-1], now_iso()))
                log.info("PostgreSQL schema migrated from version %d to %d", version, applied[-1])

    def schema_version(self) -> int:
        with self.connect() as con:
            if self.backend == "postgres":
                row = con.execute("select version from schema_version where id = 1").fetchone()
                return row[0] if row else 0
            return con.execute("pragma user_version").fetchone()[0]

    @property
    def persistent(self) -> bool:
        """Will the data survive a redeploy? Postgres: yes. SQLite: yes, except on Render when DATA_DIR is not on a
        mounted persistent disk (the instance filesystem is replaced on every deploy and on free-plan spin-down)."""
        if self._persistent is None:
            self._persistent = (self.backend == "postgres" or not os.getenv("RENDER")
                                or disk_is_persistent(self.data_dir))
        return self._persistent

    def describe(self) -> str:
        return self.location if self.backend == "postgres" else f"sqlite {self.path}"

    def _insert_id(self, con, sql: str, params) -> int:
        """Run an INSERT into a table with an integer id and return the new id."""
        if self.backend == "postgres":
            return con.execute(sql + " returning id", params).fetchone()[0]
        return con.execute(sql, params).lastrowid

    @staticmethod
    def _row(table: str, row) -> dict | None:
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
            new_id = self._insert_id(con, f"insert into {table} ({', '.join(cols)}) "
                                          f"values ({', '.join('?' * len(cols))})", [values[c] for c in cols])
            row = con.execute(f"select * from {table} where tenant_id = ? and id = ?", (tenant_id, new_id)).fetchone()
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
            uid = self._insert_id(con, "insert into users (tenant_id, email, name, pw_hash, role, "
                                       "must_change_password, created) values (?,?,?,?,?,?,?)",
                                  (tenant_id, email.strip().lower(), name, pw_hash, role, int(must_change_password),
                                   now_iso()))
            return dict(con.execute("select * from users where id = ?", (uid,)).fetchone())

    def user_by_email(self, email: str) -> dict | None:
        with self.connect() as con:
            row = con.execute(f"select * from users where {self._email_eq}",
                              ((email or "").strip().lower(),)).fetchone()
            return dict(row) if row else None

    def user_by_id(self, user_id: int) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from users where id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def first_owner(self) -> dict | None:
        """The oldest owner account (local mode has exactly one company)."""
        with self.connect() as con:
            row = con.execute("select * from users where role = 'owner' order by id limit 1").fetchone()
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
            kid = self._insert_id(con, "insert into api_keys (tenant_id, name, key_hash, prefix, created_by, "
                                       "created) values (?,?,?,?,?,?)",
                                  (tenant_id, name, key_hash, prefix, created_by, now_iso()))
            return self._key(con.execute("select * from api_keys where id = ?", (kid,)).fetchone())

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
        sql = ("insert into revoked_sessions (nonce, expires) values (?, ?) on conflict (nonce) do nothing"
               if self.backend == "postgres" else
               "insert or ignore into revoked_sessions (nonce, expires) values (?, ?)")
        with self.connect() as con:
            con.execute(sql, (nonce, int(expires)))
            con.execute("delete from revoked_sessions where expires < ?", (int(datetime.now(timezone.utc).timestamp()),))

    def session_revoked(self, nonce: str) -> bool:
        with self.connect() as con:
            return con.execute("select 1 from revoked_sessions where nonce = ?", (nonce,)).fetchone() is not None

    # ------------------------------------------------------------------ audit
    def audit(self, tenant_id: str | None, user: str | None, tool: str, args: dict | None, verified: bool | None,
              ok: bool = True, source: str = "api") -> None:
        with self.connect() as con:
            con.execute('insert into audit (tenant_id, "user", tool, args_json, verified, ok, source, created) '
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
        except IntegrityError:
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
        sql = ("insert into stripe_events (id, type, created) values (?,?,?) on conflict (id) do nothing"
               if self.backend == "postgres" else
               "insert or ignore into stripe_events (id, type, created) values (?,?,?)")
        with self.connect() as con:
            return con.execute(sql, (event_id, event_type, now_iso())).rowcount == 0

    def forget_stripe_event(self, event_id: str) -> None:
        with self.connect() as con:
            con.execute("delete from stripe_events where id = ?", (event_id,))

    # ------------------------------------------------------------------ app-wide secrets (Postgres)
    def shared_secret(self, name: str, length: int = 48) -> str:
        """A random secret generated once and kept in the app_secrets table, the same for every instance that uses
        this database. Two instances starting together both get the first one written; a stored value shorter
        than MIN_SECRET_LEN characters is replaced."""
        fresh = secrets.token_urlsafe(length)
        sql = ("insert into app_secrets (name, value, created) values (?, ?, ?) on conflict (name) do update "
               "set value = excluded.value, created = excluded.created where length(app_secrets.value) < ?")
        with self.connect() as con:
            cur = con.execute(sql, (name, fresh, now_iso(), MIN_SECRET_LEN))
            value = con.execute("select value from app_secrets where name = ?", (name,)).fetchone()[0]
        if cur.rowcount and value == fresh:
            log.info("generated a new %s and stored it in the database (table app_secrets)", name)
        return value
