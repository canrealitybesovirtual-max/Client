"""PostgreSQL backend of app.store: SQL translation, pooler-safe connection settings, loud failure instead of a
silent SQLite fallback, migrations under an advisory lock, reconnects, thread safety and the shared signing key.

The first group needs no database. The rest run when TC_TEST_DATABASE_URL points at a PostgreSQL database the tests
may use (each test gets its own schema, dropped afterwards) and are skipped otherwise.
"""
import logging
import os
import secrets
import sqlite3
import threading
from urllib.parse import quote

import pytest

os.environ.pop("DATABASE_URL", None)       # never a real database: Postgres only through TC_TEST_DATABASE_URL

from app import local, store as store_mod  # noqa: E402
from app.store import (SCHEMA_VERSION, IntegrityError, Row, StorageUnavailable, Store,  # noqa: E402
                       describe_pg_url, pg_connect_kwargs, to_pg)

PG_TEST_URL = (os.getenv("TC_TEST_DATABASE_URL") or "").strip() or None
needs_pg = pytest.mark.skipif(not PG_TEST_URL, reason="set TC_TEST_DATABASE_URL to run the PostgreSQL tests")


# ---------------------------------------------------------------------------- no database needed
def test_placeholder_translation():
    assert to_pg("select * from t where a = ? and b = ?") == "select * from t where a = %s and b = %s"
    # quoted text keeps its question marks; every % is doubled because psycopg reads % even inside literals
    assert to_pg("select '?', \"q?\" from t where x like '%a' and y = ?") == \
        "select '?', \"q?\" from t where x like '%%a' and y = %s"
    assert to_pg("select 'it''s ?' where z = ?") == "select 'it''s ?' where z = %s"


def test_row_behaves_like_sqlite_row():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    want = con.execute("select 1 as a, 'x' as b").fetchone()
    got = Row(("a", "b"), {"a": 0, "b": 1}, (1, "x"))
    assert (got[0], got["b"], dict(got), tuple(got), got.keys(), len(got)) == \
        (want[0], want["b"], dict(want), tuple(want), want.keys(), len(want))


def test_connect_settings_for_poolers(monkeypatch):
    monkeypatch.delenv("PGSSLMODE", raising=False)
    monkeypatch.delenv("DATABASE_CONNECT_TIMEOUT", raising=False)
    kw = pg_connect_kwargs("postgresql://user.ref:pw@pooler.example.test:6543/postgres")
    assert kw["sslmode"] == "require"                    # TLS unless the URL says otherwise
    assert kw["prepare_threshold"] is None               # no prepared statements (transaction-mode poolers)
    assert kw["autocommit"] is False and 2 <= kw["connect_timeout"] <= 10
    assert "sslmode" not in pg_connect_kwargs("postgres://u:pw@h.example.test/db?sslmode=verify-full")
    assert "connect_timeout" not in pg_connect_kwargs("postgresql://u:pw@h.example.test/db?connect_timeout=3")
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    assert "sslmode" not in pg_connect_kwargs("postgresql://u:pw@h.example.test/db")


def test_logs_and_errors_never_show_the_password():
    d = describe_pg_url("postgresql://postgres.abcd:s3cret-pw@pooler.example.test:6543/postgres")
    assert d == "postgres.abcd@pooler.example.test:6543/postgres"


@pytest.mark.parametrize("url", ["postgresql://app:ab%zzs3cret@db.example.test/app",
                                 "postgresql://app:pa@s3cret@db.example.test/app"])
def test_malformed_url_error_does_not_quote_the_password(tmp_path, url):
    with pytest.raises(StorageUnavailable, match="percent-encoded") as e:
        Store(str(tmp_path), database_url=url, connect_attempts=1)
    assert "s3cret" not in str(e.value) and "s3cret" not in describe_pg_url(url)


def test_non_postgres_database_url_refused(tmp_path):
    with pytest.raises(StorageUnavailable, match="postgres://"):
        Store(str(tmp_path), database_url="mysql://u:pw@db.example.test/app")
    assert not (tmp_path / "tradecomply.sqlite").exists()


def test_unreachable_database_fails_loudly_without_sqlite_fallback(tmp_path, caplog):
    url = "postgresql://app:s3cret-pw@127.0.0.1:1/app?connect_timeout=2"     # nothing listens on port 1
    with caplog.at_level(logging.CRITICAL, logger="tradecomply.store"), pytest.raises(StorageUnavailable) as e:
        Store(str(tmp_path), database_url=url, connect_attempts=1)
    msg = str(e.value)
    assert "unreachable" in msg and "127.0.0.1:1" in msg and "s3cret-pw" not in msg
    assert any("unreachable" in r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL)
    assert not (tmp_path / "tradecomply.sqlite").exists()


def test_app_does_not_start_on_an_unreachable_database_url(tmp_path, monkeypatch):
    from app.config import Settings
    from app.main import create_app
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:pw@127.0.0.1:1/app?connect_timeout=2")
    monkeypatch.setenv("DATABASE_CONNECT_ATTEMPTS", "1")
    settings = Settings.from_env(data_dir=str(tmp_path))
    assert settings.database_url.startswith("postgresql://")
    with pytest.raises(StorageUnavailable):
        create_app(settings, mcp_server=object())
    assert not (tmp_path / "tradecomply.sqlite").exists()


def test_blank_database_url_means_sqlite(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "   ")
    assert store_mod.database_url_from_env() is None


def test_local_mode_ignores_database_url(tmp_path, monkeypatch, capsys):
    for k in ("DATA_DIR", "TC_LOCAL", "LICENSE_DB"):        # restored after the test
        monkeypatch.setenv(k, os.environ.get(k, ""))
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:pw@db.example.test/app")
    local.prepare_env(str(tmp_path))
    assert "DATABASE_URL" not in os.environ and "ignored in local mode" in capsys.readouterr().out


def test_sqlite_root_filesystem_is_not_persistent():
    assert store_mod.disk_is_persistent("/") is False


# ---------------------------------------------------------------------------- against PostgreSQL
def with_search_path(url: str, schema: str) -> str:
    return url + ("&" if "?" in url else "?") + "options=" + quote(f"-csearch_path={schema}", safe="")


@pytest.fixture
def pg():
    """(url, admin connection, open stores) for a fresh schema."""
    import psycopg
    schema = "tc_s_" + secrets.token_hex(6)
    admin = psycopg.connect(PG_TEST_URL, autocommit=True)
    admin.execute(f'create schema "{schema}"')
    opened: list[Store] = []
    try:
        yield with_search_path(PG_TEST_URL, schema), admin, opened
    finally:
        for s in opened:
            s.close()
        admin.execute(f'drop schema "{schema}" cascade')
        admin.close()


def open_store(pg, tmp_path, name="a") -> Store:
    s = Store(str(tmp_path / name), database_url=pg[0])
    pg[2].append(s)
    return s


@needs_pg
def test_migrations_are_versioned_and_idempotent(pg, tmp_path):
    s = open_store(pg, tmp_path)
    assert s.backend == "postgres" and s.schema_version() == SCHEMA_VERSION and s.persistent is True
    s.migrate()
    s.migrate()
    with s.connect() as con:
        assert con.execute("select count(*), max(version) from schema_version").fetchone()[:] == (1, SCHEMA_VERSION)
        tables = {r[0] for r in con.execute("select table_name from information_schema.tables "
                                            "where table_schema = current_schema()").fetchall()}
    assert {"tenants", "users", "api_keys", "projects", "subs", "documents", "field_logs", "audit", "stripe_events",
            "revoked_sessions", "app_secrets", "schema_version"} <= tables
    assert not os.path.exists(tmp_path / "a" / "tradecomply.sqlite")


@needs_pg
def test_instances_starting_together_migrate_once(pg, tmp_path, caplog):
    n = 6
    gate, errors, stores = threading.Barrier(n), [], []

    def start(i):
        try:
            gate.wait()
            stores.append(Store(str(tmp_path / f"i{i}"), database_url=pg[0]))
        except Exception as e:      # noqa: BLE001 - reported below
            errors.append(e)
    with caplog.at_level(logging.INFO, logger="tradecomply.store"):
        threads = [threading.Thread(target=start, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    pg[2].extend(stores)
    assert errors == [] and len(stores) == n
    assert sum("schema migrated" in r.getMessage() for r in caplog.records) == 1
    assert {s.schema_version() for s in stores} == {SCHEMA_VERSION}


@needs_pg
def test_data_survives_a_restart_on_another_instance(pg, tmp_path):
    a = open_store(pg, tmp_path, "a")
    t = a.create_tenant("Acme Builders", "123456", None)
    a.create_user(t["id"], "Owner@Acme.test", "hash", "owner")
    a.insert("projects", t["id"], {"name": "Main St", "contract_price": 1234567.89, "extra": {"k": [1, 2]}})
    a.close()
    b = open_store(pg, tmp_path, "b")         # a new instance with an empty disk
    assert b.get_tenant(t["id"])["company_name"] == "Acme Builders"
    assert b.user_by_email("OWNER@acme.TEST")["email"] == "owner@acme.test"
    p = b.list("projects", t["id"])[0]
    assert p["contract_price"] == 1234567.89 and p["extra"] == {"k": [1, 2]}
    assert b.list("projects", "t_other") == []


@needs_pg
def test_email_is_unique_regardless_of_case(pg, tmp_path):
    s = open_store(pg, tmp_path)
    t = s.create_tenant("A", None, None)
    s.create_user(t["id"], "crew@acme.test", "h", "field")
    with pytest.raises(IntegrityError) as e:
        with s.connect() as con:      # even a raw insert that skips the store's lowercasing
            con.execute("insert into users (tenant_id, email, pw_hash, role, created) values (?,?,?,?,?)",
                        (t["id"], "CREW@acme.test", "h", "field", "2026-11-25T00:00:00+00:00"))
    assert isinstance(e.value, sqlite3.IntegrityError)          # same class callers catch on SQLite
    assert len(s.list_users(t["id"])) == 1                      # the failed transaction left nothing behind
    assert s.count_tenants() == 1                               # and the pooled connection still works


@needs_pg
def test_reconnects_after_the_server_drops_a_pooled_connection(pg, tmp_path):
    s = open_store(pg, tmp_path)
    with s.connect() as con:
        pid = con.execute("select pg_backend_pid()").fetchone()[0]
    assert pg[1].execute("select pg_terminate_backend(%s)", (pid,)).fetchone()[0] is True
    assert s.count_tenants() == 0                               # the dead idle connection is replaced
    with s.connect() as con:
        assert con.execute("select pg_backend_pid()").fetchone()[0] != pid


@needs_pg
def test_no_prepared_statements_and_literal_percent(pg, tmp_path):
    s = open_store(pg, tmp_path)
    with s.connect() as con:
        for _ in range(12):            # psycopg would prepare a query after 5 runs by default
            con.execute("select * from tenants where id = ?", ("t_x",)).fetchall()
        assert con.execute("select count(*) from pg_prepared_statements").fetchone()[0] == 0
        assert con.execute("select ? || '%'", ("50",)).fetchone()[0] == "50%"


@needs_pg
def test_tls_is_used_when_the_url_does_not_say(pg, tmp_path):
    if "sslmode=" in PG_TEST_URL or os.getenv("PGSSLMODE"):
        pytest.skip("TC_TEST_DATABASE_URL sets its own sslmode")
    s = open_store(pg, tmp_path)
    with s.connect() as con:
        assert con.execute("select ssl from pg_stat_ssl where pid = pg_backend_pid()").fetchone()[0] is True


@needs_pg
def test_rollback_on_error(pg, tmp_path):
    s = open_store(pg, tmp_path)
    with pytest.raises(RuntimeError):
        with s.connect() as con:
            con.execute("insert into tenants (id, company_name, created) values (?,?,?)", ("t_1", "X", "now"))
            raise RuntimeError("boom")
    assert s.get_tenant("t_1") is None


@needs_pg
def test_many_threads_share_a_small_pool(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_MAX", "3")
    s = open_store(pg, tmp_path)
    tid = s.create_tenant("A", None, None)["id"]
    errors = []

    def work(i):
        try:
            for j in range(8):
                s.insert("field_logs", tid, {"kind": "inspection", "date": "2026-11-20", "client_uuid": f"{i}-{j}"})
                s.list("field_logs", tid, limit=5)
        except Exception as e:      # noqa: BLE001 - reported below
            errors.append(e)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = s.list("field_logs", tid)
    assert len(rows) == 96 and len({r["id"] for r in rows}) == 96
    assert pg[1].execute("select count(*) from pg_stat_activity where datname = current_database() "
                         "and pid <> pg_backend_pid() and usename = current_user").fetchone()[0] <= 3 + 1


@needs_pg
def test_shared_secret_is_generated_once_for_every_instance(pg, tmp_path):
    a, b = open_store(pg, tmp_path, "a"), open_store(pg, tmp_path, "b")
    key = a.shared_secret("secret_key")
    assert len(key) >= 32 and b.shared_secret("secret_key") == key and a.shared_secret("secret_key") == key
    with a.connect() as con:
        con.execute("update app_secrets set value = 'short' where name = 'secret_key'")
    fresh = b.shared_secret("secret_key")
    assert fresh != "short" and len(fresh) >= 32 and a.shared_secret("secret_key") == fresh


@needs_pg
def test_signing_key_from_database_unless_secret_key_set(pg, tmp_path, monkeypatch):
    from app.config import signing_key
    monkeypatch.delenv("SECRET_KEY", raising=False)
    s = open_store(pg, tmp_path)
    k = signing_key(s)
    assert k == signing_key(open_store(pg, tmp_path, "b")) and not os.path.exists(tmp_path / "a" / "secret_key")
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    assert signing_key(s) == b"k" * 40
    monkeypatch.setenv("SECRET_KEY", "short")
    with pytest.raises(RuntimeError):
        signing_key(s)
