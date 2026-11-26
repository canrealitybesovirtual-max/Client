"""Schema version 5 store methods: integration connections, webhooks, the outbound event queue, the MCP audit log,
SSO settings and calendar tokens. Every test runs on SQLite and, with TC_TEST_DATABASE_URL, on PostgreSQL."""
import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.crypto import SecretBox, connection_context, rotate_stored_secrets, sso_context, webhook_context
from app.store import APP_TABLES, SCHEMA_VERSION, IntegrityError
from tests.support import backend, store_for  # noqa: F401 - backend is an autouse fixture

BOX = SecretBox([os.urandom(32)])


@pytest.fixture
def st(tmp_path):
    return store_for(tmp_path)


@pytest.fixture
def two(st):
    return st, st.create_tenant("A", None, None)["id"], st.create_tenant("B", None, None)["id"]


def blob(text="secret", ctx="c"):
    return BOX.encrypt(text, ctx)


def test_schema_version_and_tables(st):
    assert SCHEMA_VERSION >= 5 and st.schema_version() == SCHEMA_VERSION
    with st.connect() as con:
        for table in ("integration_connections", "webhooks", "outbound_events", "mcp_audit_log", "sso_configs",
                      "calendar_tokens"):
            assert table in APP_TABLES
            assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_upgrade_from_version_4_keeps_data(tmp_path, backend):
    """An existing database at version 4 gets the new tables and keeps everything it had."""
    s = store_for(tmp_path)
    tid = s.create_tenant("Old Co", None, None)["id"]
    with s.connect() as con:
        for table in ("integration_connections", "webhooks", "outbound_events", "mcp_audit_log", "sso_configs",
                      "calendar_tokens"):
            con.execute(f"drop table {table}")
        if s.backend == "sqlite":
            con.execute("pragma user_version = 4")
        else:
            con.execute("update schema_version set version = 4")
    s.migrate()
    assert s.schema_version() == SCHEMA_VERSION and s.get_tenant(tid)["company_name"] == "Old Co"
    assert s.save_connection(tid, "procore", status="pending")["status"] == "pending"


# ---------------------------------------------------------------------------- connections
def test_connection_upsert_and_tenant_isolation(two):
    st, a, b = two
    c = st.save_connection(a, "procore", status="pending", config={"company_id": 5}, secret_blob=blob())
    assert c["status"] == "pending" and c["config"] == {"company_id": 5} and c["has_secret"]
    assert "secret_blob" not in c and st.get_connection(a, "procore", with_secret=True)["secret_blob"]
    again = st.save_connection(a, "procore", status="connected", external_account="Acme")
    assert again["id"] == c["id"] and again["config"] == {"company_id": 5} and again["has_secret"]   # kept
    assert st.get_connection(b, "procore") is None and st.list_connections(b) == []
    assert st.update_connection(b, "procore", status="error") is None
    assert st.delete_connection(b, "procore") is False
    assert [x["provider"] for x in st.list_connections(a)] == ["procore"]
    st.save_connection(b, "procore", status="connected")
    assert {x["tenant_id"] for x in st.connections_for_provider("procore")} == {a, b}
    assert st.delete_connection(a, "procore") and st.get_connection(a, "procore") is None
    assert st.get_connection(b, "procore")["status"] == "connected"


def test_connection_compare_and_set(two):
    st, a, _ = two
    first = blob("v1")
    st.save_connection(a, "quickbooks", secret_blob=first, status="connected")
    second = blob("v2")
    assert st.update_connection(a, "quickbooks", expect_secret_blob=first, secret_blob=second)
    assert st.update_connection(a, "quickbooks", expect_secret_blob=first, secret_blob=blob("v3")) is None
    assert st.get_connection(a, "quickbooks", with_secret=True)["secret_blob"] == second
    st.save_connection(a, "gusto")
    assert st.update_connection(a, "gusto", expect_secret_blob=None, secret_blob=first)["has_secret"]


@pytest.mark.parametrize("fields,match", [
    ({"secret_blob": "plaintext-api-key"}, "encrypted blob"),
    ({"status": "on"}, "status"),
    ({"config": ["not", "a", "dict"]}, "config"),
    ({"config": {"x": "y" * 30000}}, "too large"),
    ({"created": "2020-01-01"}, "unknown"),
    ({"token": "x"}, "unknown"),
])
def test_connection_fields_are_whitelisted_and_validated(two, fields, match):
    st, a, _ = two
    with pytest.raises(ValueError, match=match):
        st.save_connection(a, "procore", **fields)


@pytest.mark.parametrize("provider", ["", "Procore", "pro core", "1abc", "x" * 41, "a;drop table"])
def test_bad_provider_ids_refused(two, provider):
    st, a, _ = two
    with pytest.raises(ValueError):
        st.save_connection(a, provider)


def test_connection_needs_a_real_tenant(st):
    with pytest.raises(IntegrityError):
        st.save_connection("t_missing", "procore")


# ---------------------------------------------------------------------------- webhooks
def test_webhook_crud_isolation_and_event_matching(two):
    st, a, b = two
    w = st.create_webhook(a, name=" Ops channel ", url=blob("https://hooks.slack.com/services/x"), kind="slack",
                          events=["sub_noncompliant", "deadline_due_soon", "sub_noncompliant"])
    assert w["name"] == "Ops channel" and w["events"] == ["deadline_due_soon", "sub_noncompliant"]
    assert w["enabled"] is True and w["has_signing_secret"] is False and "signing_secret_blob" not in w
    g = st.create_webhook(a, name="All", url=blob(), kind="generic", events=["*"], signing_secret_blob=blob("whsec"))
    assert st.get_webhook(a, g["id"], with_secret=True)["signing_secret_blob"]
    assert [x["id"] for x in st.webhooks_for_event(a, "sub_noncompliant")] == [w["id"], g["id"]]
    assert [x["id"] for x in st.webhooks_for_event(a, "payroll_issue")] == [g["id"]]
    assert st.get_webhook(b, w["id"]) is None and st.update_webhook(b, w["id"], enabled=False) is None
    assert st.delete_webhook(b, w["id"]) is False and st.webhooks_for_event(b, "sub_noncompliant") == []
    upd = st.update_webhook(a, w["id"], enabled=False, last_status="200 delivered", last_sent="2026-11-26")
    assert upd["enabled"] is False and upd["last_sent"] == "2026-11-26T00:00:00+00:00"
    assert [x["id"] for x in st.webhooks_for_event(a, "sub_noncompliant")] == [g["id"]]
    assert st.list_webhooks(a, enabled_only=True) == [st.get_webhook(a, g["id"])]
    assert st.delete_webhook(a, w["id"]) and st.get_webhook(a, w["id"]) is None
    assert st.get_webhook(a, 2 ** 70) is None


@pytest.mark.parametrize("fields,match", [
    ({"url": "https://hooks.slack.com/services/T/B/X"}, "encrypted"),        # the URL is a credential
    ({"kind": "discord"}, "kind"),
    ({"name": ""}, "name"),
    ({"events": "sub_noncompliant"}, "events"),
    ({"signing_secret_blob": "whsec_plain"}, "encrypted"),
])
def test_webhook_fields_validated(two, fields, match):
    st, a, _ = two
    args = {"name": "x", "url": blob(), "kind": "generic", "events": ["*"], **fields}
    with pytest.raises(ValueError, match=match):
        st.create_webhook(a, **args)


# ---------------------------------------------------------------------------- outbound queue
def test_outbound_enqueue_claim_finish_and_isolation(two):
    st, a, b = two
    ids = st.enqueue_outbound(a, "sub_noncompliant", {"title": "x"}, ["webhook:1", "connector:procore",
                                                                      "webhook:1"])
    assert len(ids) == 2                                     # duplicate targets collapse
    rows = st.claim_outbound(limit=10)
    assert {r["id"] for r in rows} == set(ids) and all(r["status"] == "sending" and r["attempts"] == 1 for r in rows)
    assert rows[0]["payload"] == {"title": "x"} and st.claim_outbound() == []      # leased, not handed out twice
    assert st.finish_outbound(b, ids[0], "delivered") is False                   # another tenant's row
    assert st.finish_outbound(a, ids[0], "delivered")
    assert st.finish_outbound(a, ids[1], "pending", next_attempt=datetime.now(timezone.utc) + timedelta(hours=1),
                              last_error="503")
    assert st.claim_outbound() == []
    later = st.claim_outbound(now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert [r["id"] for r in later] == [ids[1]] and later[0]["attempts"] == 2 and later[0]["last_error"] == "503"
    assert [r["status"] for r in st.list_outbound(a)] == ["sending", "delivered"]
    assert st.list_outbound(b) == [] and st.get_outbound(b, ids[0]) is None
    assert [r["id"] for r in st.list_outbound(a, status="delivered")] == [ids[0]]
    assert [r["id"] for r in st.list_outbound(a, before_id=ids[1])] == [ids[0]]
    with pytest.raises(ValueError):
        st.finish_outbound(a, ids[0], "pending")             # a retry needs next_attempt
    with pytest.raises(ValueError):
        st.finish_outbound(a, ids[0], "sending")


def test_outbound_lease_expiry_hands_the_row_out_again(two):
    st, a, _ = two
    st.enqueue_outbound(a, "payroll_issue", {"title": "x"}, ["handler:h"])
    assert len(st.claim_outbound(lease_s=60)) == 1
    assert st.claim_outbound() == []
    again = st.claim_outbound(now=datetime.now(timezone.utc) + timedelta(seconds=120))   # the worker died
    assert len(again) == 1 and again[0]["attempts"] == 2


def test_outbound_dedupe_key(two):
    st, a, b = two
    assert len(st.enqueue_outbound(a, "deadline_due_soon", {"title": "x"}, ["webhook:1", "webhook:2"],
                                   dedupe_key="coi:7:2026-12-01")) == 2
    assert st.enqueue_outbound(a, "deadline_due_soon", {"title": "x"}, ["webhook:1"],
                               dedupe_key="coi:7:2026-12-01") == []
    assert len(st.enqueue_outbound(a, "deadline_due_soon", {"title": "x"}, ["webhook:3"],
                                   dedupe_key="coi:7:2026-12-01")) == 1           # a new target still gets it
    assert len(st.enqueue_outbound(b, "deadline_due_soon", {"title": "x"}, ["webhook:1"],
                                   dedupe_key="coi:7:2026-12-01")) == 1           # keys are per tenant
    assert len(st.enqueue_outbound(a, "deadline_due_soon", {"title": "x"}, ["webhook:1"])) == 1   # no key: always


@pytest.mark.parametrize("args,match", [
    (("", {"title": "x"}, ["webhook:1"]), "event"),
    (("e", ["not a dict"], ["webhook:1"]), "payload"),
    (("e", {"x": "y" * 70000}, ["webhook:1"]), "too large"),
    (("e", {}, ["email:a@b"]), "target"),
    (("e", {}, ["webhook:1; drop"]), "target"),
])
def test_outbound_validation(two, args, match):
    st, a, _ = two
    with pytest.raises(ValueError, match=match):
        st.enqueue_outbound(a, *args)


def test_concurrent_claims_never_share_a_row(two):
    st, a, _ = two
    for i in range(30):
        st.enqueue_outbound(a, "payroll_issue", {"title": f"{i}"}, [f"handler:h{i}"])
    got, errors, gate = [], [], threading.Barrier(5)

    def worker():
        try:
            gate.wait()
            for _ in range(10):
                got.extend(r["id"] for r in st.claim_outbound(limit=4))
        except Exception as e:      # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(got) == 30 and len(set(got)) == 30


def test_purge_removes_only_old_finished_rows(two):
    st, a, _ = two
    ids = st.enqueue_outbound(a, "payroll_issue", {"title": "x"}, ["handler:a", "handler:b", "handler:c"])
    st.claim_outbound()
    st.finish_outbound(a, ids[0], "delivered")
    st.finish_outbound(a, ids[1], "failed", last_error="x")
    assert st.purge_outbound(datetime.now(timezone.utc) - timedelta(days=1)) == 0
    assert st.purge_outbound(datetime.now(timezone.utc) + timedelta(days=1)) == 2
    assert [r["id"] for r in st.list_outbound(a)] == [ids[2]]       # still sending: kept


# ---------------------------------------------------------------------------- MCP audit log
def test_mcp_audit_pages_by_date_range_and_tenant(two):
    st, a, b = two
    with st.connect() as con:     # 25 calls an hour apart, plus calls of B and anonymous ones
        base = datetime(2026, 11, 1, tzinfo=timezone.utc)
        for i in range(25):
            con.execute("insert into mcp_audit_log (tenant_id, api_key_id, tool, args_sha256, status, duration_ms, "
                        "ip, created) values (?,?,?,?,?,?,?,?)",
                        (a, 7, f"tool{i}", "a" * 64, "ok", i, "203.0.113.9",
                         (base + timedelta(hours=i)).isoformat()))
    st.log_mcp_call(b, "b_tool", "error", api_key_id=9, args_sha256=hashlib.sha256(b"{}").hexdigest(),
                    duration_ms=12, ip="198.51.100.1")
    st.log_mcp_call(None, "anon_tool", "rate_limited")
    seen, cursor = [], None
    while True:
        page = st.list_mcp_audit(a, start="2026-11-01T02:00:00Z", end="2026-11-01T20:00:00+00:00", limit=5,
                                 cursor=cursor)
        seen += [r["tool"] for r in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"tool{i}" for i in range(19, 1, -1)]                 # [02:00, 20:00), newest first, no gaps
    assert st.list_mcp_audit(b)["items"][0]["tool"] == "b_tool" and len(st.list_mcp_audit(b)["items"]) == 1
    everything = st.admin_mcp_audit(limit=1000)["items"]
    assert len(everything) == 27
    assert [r["tool"] for r in st.admin_mcp_audit(anonymous=True)["items"]] == ["anon_tool"]
    assert len(st.admin_mcp_audit(tenant_id=a, start="2026-11-01", end="2026-11-02", limit=1000)["items"]) == 24
    with pytest.raises(ValueError):
        st.list_mcp_audit(a, cursor="garbage")
    with pytest.raises(ValueError):
        st.list_mcp_audit(a, start="last tuesday")
    with pytest.raises(ValueError):
        st.list_mcp_audit("", limit=5)


@pytest.mark.parametrize("kwargs", [{"status": "fine"}, {"status": "ok", "args_sha256": "nothex"}])
def test_mcp_audit_validation(two, kwargs):
    st, a, _ = two
    with pytest.raises(ValueError):
        st.log_mcp_call(a, "tool", **kwargs)


# ---------------------------------------------------------------------------- SSO
def test_sso_config_upsert_keep_secret_and_domain_lookup(two):
    st, a, b = two
    s = st.save_sso_config(a, issuer="https://login.example.com/", client_id="cid", secret_blob=blob(),
                           allowed_domains=["Acme.test", "acme.test", "sub.acme.test"], enforced=True)
    assert s["allowed_domains"] == ["acme.test", "sub.acme.test"] and s["enforced"] and s["has_secret"]
    assert "secret_blob" not in s
    s2 = st.save_sso_config(a, issuer="https://login.example.com/", client_id="cid2", allowed_domains=["acme.test"])
    assert s2["client_id"] == "cid2" and s2["has_secret"] and s2["created"] == s["created"]   # secret kept
    assert st.get_sso_config(b) is None and st.delete_sso_config(b) is False
    st.save_sso_config(b, issuer="https://idp.example.org", client_id="x", allowed_domains=["other.test"])
    assert [c["tenant_id"] for c in st.sso_configs_for_domain("ACME.test")] == [a]
    assert st.sso_configs_for_domain("cme.test") == [] and st.sso_configs_for_domain("%") == []
    assert st.save_sso_config(a, issuer="i", client_id="c", secret_blob=None)["has_secret"] is False
    assert st.delete_sso_config(a) and st.get_sso_config(a) is None
    with pytest.raises(ValueError):
        st.save_sso_config(a, issuer="i", client_id="c", allowed_domains=["not a domain"])
    with pytest.raises(ValueError):
        st.save_sso_config(a, issuer="i", client_id="c", secret_blob="plain-secret")


# ---------------------------------------------------------------------------- calendar tokens
def test_calendar_tokens(two):
    st, a, b = two
    h1, h2 = hashlib.sha256(b"one").hexdigest(), hashlib.sha256(b"two").hexdigest()
    assert st.add_calendar_token(a, h1)["revoked"] is False
    st.add_calendar_token(a, h2)
    assert st.calendar_token_tenant(h1) == a and st.calendar_token_tenant("f" * 64) is None
    assert st.revoke_calendar_token(b, h1) is False and st.calendar_token_tenant(h1) == a
    assert st.revoke_calendar_token(a, h1) and st.calendar_token_tenant(h1) is None
    assert [t["token_hash"] for t in st.list_calendar_tokens(a)] == [h2]
    assert len(st.list_calendar_tokens(a, include_revoked=True)) == 2 and st.list_calendar_tokens(b) == []
    assert st.revoke_calendar_tokens(a) == 1 and st.calendar_token_tenant(h2) is None
    with pytest.raises(ValueError):
        st.add_calendar_token(a, "plain-token")
    with pytest.raises(IntegrityError):
        st.add_calendar_token(b, h1)                       # a hash belongs to one tenant


def test_claim_nonce_is_single_use(st):
    import time
    assert st.claim_nonce("oauth:abc", int(time.time()) + 60) is True
    assert st.claim_nonce("oauth:abc", int(time.time()) + 60) is False
    assert st.claim_nonce("oauth:def", int(time.time()) + 60) is True


# ---------------------------------------------------------------------------- key rotation
def test_rotate_stored_secrets_moves_every_blob_to_the_current_key(two):
    st, a, b = two
    old, new = os.urandom(32), os.urandom(32)
    old_box, both = SecretBox([old]), SecretBox([new, old])
    st.save_connection(a, "procore", secret_blob=old_box.encrypt_json({"t": 1}, connection_context(a, "procore")))
    w = st.create_webhook(b, name="w", url=old_box.encrypt("https://x.example.com/h", webhook_context(b)),
                          kind="generic", events=["*"], signing_secret_blob=old_box.encrypt("whsec_x",
                                                                                            webhook_context(b)))
    st.save_sso_config(a, issuer="i", client_id="c", secret_blob=old_box.encrypt("s", sso_context(a)))
    st.save_connection(b, "gusto", secret_blob=SecretBox([os.urandom(32)]).encrypt("lost", "x"))   # unknown key
    out = rotate_stored_secrets(st, both)
    assert out["rotated"] == 4 and out["unreadable"] == 1
    assert SecretBox([new]).decrypt_json(st.get_connection(a, "procore", with_secret=True)["secret_blob"],
                                         connection_context(a, "procore")) == {"t": 1}
    hook = st.get_webhook(b, w["id"], with_secret=True)
    assert SecretBox([new]).decrypt_text(hook["url"], webhook_context(b)) == "https://x.example.com/h"
    assert SecretBox([new]).decrypt_text(hook["signing_secret_blob"], webhook_context(b)) == "whsec_x"
    assert rotate_stored_secrets(st, both)["rotated"] == 0
