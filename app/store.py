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
import re
import secrets
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

log = logging.getLogger("tradecomply.store")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERSION = 9

TENANT_STATUSES = ("trialing", "active", "past_due", "canceled")
ROLES = ("owner", "office", "field")
FIELD_LOG_KINDS = ("toolbox_talk", "inspection", "incident")
PROJECT_STATUSES = ("planned", "active", "completed", "closed")
# Integration foundation (schema version 5). See docs/integrations-dev.md.
CONNECTION_STATUSES = ("disconnected", "pending", "connected", "error")
WEBHOOK_KINDS = ("slack", "teams", "generic")
OUTBOUND_STATUSES = ("pending", "sending", "delivered", "failed", "skipped")
OUTBOUND_DONE = ("delivered", "failed", "skipped")
MCP_AUDIT_STATUSES = ("ok", "error", "denied", "rate_limited")
AUTH_LINK_PURPOSES = ("reset", "invite", "confirm", "signin")     # 'signin' is reserved (not offered: see README)
ASSISTANT_OUTCOMES = ("answered", "refused", "cut_off")              # Ask TradeComply turns (app/assistant.py)
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# Raised by both backends for unique / foreign-key / check violations. On SQLite it is sqlite3's own class; psycopg's
# IntegrityError is re-raised as PgIntegrityError, a subclass, so `except IntegrityError` works the same everywhere.
IntegrityError = sqlite3.IntegrityError


class PgIntegrityError(sqlite3.IntegrityError):
    """A PostgreSQL integrity violation (the original psycopg error is __cause__)."""


class TransactionAborted(RuntimeError):
    """PostgreSQL only: a statement failed inside a transaction whose block then ended normally (the error was
    caught inside it). Postgres had already aborted the transaction, so nothing in it was saved."""


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
    4: "",          # Postgres only (row level security); kept so both backends have the same version numbers
    # Integration foundation: per-tenant connector connections (secrets encrypted by app/crypto.py before they reach
    # the store), outbound webhooks, the outbound event queue, the MCP call log, SSO settings and calendar tokens.
    5: """
    create table if not exists integration_connections (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        provider text not null,
        status text not null default 'disconnected'
            check (status in ('disconnected','pending','connected','error')),
        config_json text not null default '{}',
        secret_blob text,
        external_account text,
        created text not null,
        updated text,
        last_error text,
        unique (tenant_id, provider)
    );

    create table if not exists webhooks (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text not null,
        url text not null,
        kind text not null default 'generic' check (kind in ('slack','teams','generic')),
        events_json text not null default '[]',
        signing_secret_blob text,
        enabled integer not null default 1,
        created text not null,
        last_status text,
        last_sent text
    );
    create index if not exists webhooks_tenant on webhooks(tenant_id);

    create table if not exists outbound_events (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        event text not null,
        payload_json text not null default '{}',
        target text not null,
        status text not null default 'pending'
            check (status in ('pending','sending','delivered','failed','skipped')),
        attempts integer not null default 0,
        next_attempt text,
        created text not null,
        last_error text,
        dedupe_key text
    );
    create index if not exists outbound_events_due on outbound_events(status, next_attempt);
    create index if not exists outbound_events_tenant on outbound_events(tenant_id, id);
    create unique index if not exists outbound_events_dedupe on outbound_events(tenant_id, target, dedupe_key);

    create table if not exists mcp_audit_log (
        id integer primary key,
        tenant_id text references tenants(id) on delete cascade,
        api_key_id integer,
        tool text not null,
        args_sha256 text,
        status text not null,
        duration_ms integer,
        ip text,
        created text not null
    );
    create index if not exists mcp_audit_log_tenant_created on mcp_audit_log(tenant_id, created);
    create index if not exists mcp_audit_log_created on mcp_audit_log(created);

    create table if not exists sso_configs (
        tenant_id text primary key references tenants(id) on delete cascade,
        issuer text not null,
        client_id text not null,
        secret_blob text,
        allowed_domains_json text not null default '[]',
        enforced integer not null default 0,
        created text not null
    );

    create table if not exists calendar_tokens (
        tenant_id text not null references tenants(id) on delete cascade,
        token_hash text primary key,
        created text not null,
        revoked integer not null default 0
    );
    create index if not exists calendar_tokens_tenant on calendar_tokens(tenant_id);
    """,
    # MCP governance (docs/mcp-security.md): per-key tool scopes, a per-key rate limit and an optional expiry date.
    # NULL keeps what every key had before: all read-only tools, the platform's default rate, no expiry.
    6: """
    alter table api_keys add column scopes_json text;
    alter table api_keys add column rate_limit_per_min integer;
    alter table api_keys add column expires_at text;
    """,
    # "Sign in with TradeComply" for MCP clients (app/oauth.py, OAuth 2.1 / MCP authorization): apps registered by
    # dynamic client registration and their pending sign-in requests (shared: no company yet), and per company the
    # grants a person approved and the tokens issued from them (only SHA-256 hashes; expiries are unix seconds).
    7: """
    create table if not exists oauth_clients (
        id text primary key,
        secret_hash text,
        name text not null,
        redirect_uris_json text not null,
        auth_method text not null check (auth_method in ('none','client_secret_post','client_secret_basic')),
        created text not null
    );
    create index if not exists oauth_clients_created on oauth_clients(created);

    create table if not exists oauth_requests (
        id text primary key,
        client_id text not null references oauth_clients(id) on delete cascade,
        redirect_uri text not null,
        state text,
        code_challenge text not null,
        scope text not null,
        resource text not null,
        expires integer not null,
        created text not null
    );
    create index if not exists oauth_requests_expires on oauth_requests(expires);
    create index if not exists oauth_requests_client on oauth_requests(client_id);

    create table if not exists oauth_grants (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id integer not null,
        user_email text not null,
        session_gen integer not null,
        client_id text not null references oauth_clients(id),
        client_name text not null,
        redirect_uri text not null,
        code_challenge text not null,
        scope text not null,
        resource text not null,
        created text not null,
        last_used text,
        expires integer not null,
        revoked integer not null default 0,
        revoked_at text
    );
    create index if not exists oauth_grants_tenant_user on oauth_grants(tenant_id, user_id);
    create index if not exists oauth_grants_client on oauth_grants(client_id);

    create table if not exists oauth_tokens (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        grant_id integer not null references oauth_grants(id) on delete cascade,
        kind text not null check (kind in ('code','access','refresh')),
        token_hash text not null unique,
        expires integer not null,
        used_at integer,
        created text not null
    );
    create index if not exists oauth_tokens_grant on oauth_tokens(grant_id);
    create index if not exists oauth_tokens_expires on oauth_tokens(expires);

    alter table mcp_audit_log add column oauth_grant_id integer;
    """,
    # One-time links sent by email or shown to an owner (app/auth_links.py): password reset, team invite and email
    # confirmation ('signin' is reserved: a CHECK constraint can't be changed later on SQLite without rebuilding the
    # table). Only the SHA-256 of each token is stored; expiries are unix seconds. A link is bound to the person's
    # email and session counter when it was made, so a password change, a reset, an email change, SSO becoming
    # required or a removed person (SQLite reuses ids) ends it.
    8: """
    create table if not exists auth_links (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id integer not null references users(id) on delete cascade,
        user_email text not null,
        purpose text not null check (purpose in ('reset','invite','confirm','signin')),
        token_hash text not null unique,
        sent_to text,
        session_gen integer not null,
        created_by integer,
        expires integer not null,
        created text not null
    );
    create index if not exists auth_links_user on auth_links(tenant_id, user_id, purpose);
    create index if not exists auth_links_expires on auth_links(expires);

    alter table users add column email_confirmed_at text;
    -- the Team page reads "never signed in" as "invited, has not joined". Before invite links everyone without a
    -- temporary password had signed in (a founder at sign-up, which set no last_login), so none of them is pending
    update users set last_login = created where last_login is null and must_change_password = 0;
    """,
    # Ask TradeComply, the in-app AI helper (app/assistant.py, docs/ai-helper.md). A chat belongs to one person; each
    # turn row holds the person's message and every API message it produced (messages_json), stored exactly as sent
    # and received and replayed unchanged on the next message (thinking blocks are bound to that prefix). prompt and
    # reply are the text shown in the panel and exported. Usage is one row per message, kept after a chat is deleted
    # (the daily and monthly limits count it); user_id is set to null when the person is removed.
    9: """
    -- autoincrement: a chat id is never given out twice (SQLite otherwise reuses the highest), so an answer still being
    -- written for a chat deleted meanwhile (its person removed, say) can never land in someone else's new chat
    create table if not exists assistant_conversations (
        id integer primary key autoincrement,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id integer not null,
        title text not null,
        created text not null,
        updated text not null
    );
    create index if not exists assistant_conversations_owner on assistant_conversations(tenant_id, user_id, updated);
    create index if not exists assistant_conversations_updated on assistant_conversations(updated);

    create table if not exists assistant_turns (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        conversation_id integer not null references assistant_conversations(id) on delete cascade,
        messages_json text not null,
        prompt text not null,
        reply text not null,
        checked_json text not null default '[]',
        outcome text not null check (outcome in ('answered','refused','cut_off')),
        context_tokens integer not null default 0,
        created text not null
    );
    create index if not exists assistant_turns_conversation on assistant_turns(conversation_id, id);

    create table if not exists assistant_usage (
        id integer primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id integer,
        model text not null,
        input_tokens integer not null default 0,
        output_tokens integer not null default 0,
        cache_read_tokens integer not null default 0,
        cache_write_tokens integer not null default 0,
        created text not null
    );
    create index if not exists assistant_usage_tenant on assistant_usage(tenant_id, created);
    create index if not exists assistant_usage_person on assistant_usage(tenant_id, user_id, created);
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
    # Supabase exposes the `public` schema through its Data API (REST and GraphQL), and in a default project the
    # `anon` and `authenticated` roles get privileges on new tables there; a table without row level security can be
    # read, changed and deleted by anyone with the project URL (Supabase docs: "Row Level Security", and Database
    # Advisor lint 0013_rls_disabled_in_public). These tables hold password hashes, API key hashes, every company's
    # data and (without SECRET_KEY) the session signing key. So: row level security on every table with no policy
    # (the app connects as the tables' owner, which RLS doesn't restrict unless forced), and no privileges at all
    # for anon/authenticated where those roles exist. On a plain PostgreSQL server this changes nothing visible.
    4: """
    do $rls$
    declare t text; r text; seq text;
    begin
      foreach t in array array['tenants','users','api_keys','projects','subs','documents','field_logs','audit',
                               'stripe_events','revoked_sessions','app_secrets','schema_version'] loop
        execute format('alter table %I enable row level security', t);
        foreach r in array array['anon','authenticated'] loop
          if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table %I from %I', t, r);
            if t in ('users','api_keys','projects','subs','documents','field_logs','audit') then   -- identity ids
              seq := pg_get_serial_sequence(quote_ident(t), 'id');
              execute format('revoke all on sequence %s from %I', seq, r);
            end if;
          end if;
        end loop;
      end loop;
    end
    $rls$;
    """,
    # Integration foundation (see the SQLite migration 5). Timestamps that are compared or sorted as text
    # (next_attempt, created) use collate "C" (byte order, like SQLite). Row level security and no Data API privileges
    # for the new tables, as migration 4 does for the others.
    5: """
    create table if not exists integration_connections (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        provider text not null,
        status text not null default 'disconnected'
            check (status in ('disconnected','pending','connected','error')),
        config_json text not null default '{}',
        secret_blob text,
        external_account text,
        created text not null,
        updated text,
        last_error text,
        unique (tenant_id, provider)
    );

    create table if not exists webhooks (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        name text collate "C" not null,
        url text not null,
        kind text not null default 'generic' check (kind in ('slack','teams','generic')),
        events_json text not null default '[]',
        signing_secret_blob text,
        enabled smallint not null default 1,
        created text not null,
        last_status text,
        last_sent text
    );
    create index if not exists webhooks_tenant on webhooks(tenant_id);

    create table if not exists outbound_events (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        event text not null,
        payload_json text not null default '{}',
        target text not null,
        status text not null default 'pending'
            check (status in ('pending','sending','delivered','failed','skipped')),
        attempts integer not null default 0,
        next_attempt text collate "C",
        created text collate "C" not null,
        last_error text,
        dedupe_key text
    );
    create index if not exists outbound_events_due on outbound_events(status, next_attempt);
    create index if not exists outbound_events_tenant on outbound_events(tenant_id, id);
    create unique index if not exists outbound_events_dedupe on outbound_events(tenant_id, target, dedupe_key);

    create table if not exists mcp_audit_log (
        id bigint generated by default as identity primary key,
        tenant_id text references tenants(id) on delete cascade,
        api_key_id bigint,
        tool text not null,
        args_sha256 text,
        status text not null,
        duration_ms integer,
        ip text,
        created text collate "C" not null
    );
    create index if not exists mcp_audit_log_tenant_created on mcp_audit_log(tenant_id, created);
    create index if not exists mcp_audit_log_created on mcp_audit_log(created);

    create table if not exists sso_configs (
        tenant_id text primary key references tenants(id) on delete cascade,
        issuer text not null,
        client_id text not null,
        secret_blob text,
        allowed_domains_json text not null default '[]',
        enforced smallint not null default 0,
        created text not null
    );

    create table if not exists calendar_tokens (
        tenant_id text not null references tenants(id) on delete cascade,
        token_hash text primary key,
        created text not null,
        revoked smallint not null default 0
    );
    create index if not exists calendar_tokens_tenant on calendar_tokens(tenant_id);

    do $rls5$
    declare t text; r text; seq text;
    begin
      foreach t in array array['integration_connections','webhooks','outbound_events','mcp_audit_log','sso_configs',
                               'calendar_tokens'] loop
        execute format('alter table %I enable row level security', t);
        foreach r in array array['anon','authenticated'] loop
          if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table %I from %I', t, r);
            if t in ('integration_connections','webhooks','outbound_events','mcp_audit_log') then   -- identity ids
              seq := pg_get_serial_sequence(quote_ident(t), 'id');
              execute format('revoke all on sequence %s from %I', seq, r);
            end if;
          end if;
        end loop;
      end loop;
    end
    $rls5$;
    """,
    # MCP governance (see the SQLite migration 6). New columns on api_keys, which already has row level security.
    6: """
    alter table api_keys add column if not exists scopes_json text;
    alter table api_keys add column if not exists rate_limit_per_min integer;
    alter table api_keys add column if not exists expires_at text collate "C";
    """,
    # OAuth for MCP clients (see the SQLite migration 7), with row level security and no Data API privileges.
    7: """
    create table if not exists oauth_clients (
        id text primary key,
        secret_hash text,
        name text not null,
        redirect_uris_json text not null,
        auth_method text not null check (auth_method in ('none','client_secret_post','client_secret_basic')),
        created text collate "C" not null
    );
    create index if not exists oauth_clients_created on oauth_clients(created);

    create table if not exists oauth_requests (
        id text primary key,
        client_id text not null references oauth_clients(id) on delete cascade,
        redirect_uri text not null,
        state text,
        code_challenge text not null,
        scope text not null,
        resource text not null,
        expires bigint not null,
        created text not null
    );
    create index if not exists oauth_requests_expires on oauth_requests(expires);
    create index if not exists oauth_requests_client on oauth_requests(client_id);

    create table if not exists oauth_grants (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id bigint not null,
        user_email text not null,
        session_gen integer not null,
        client_id text not null references oauth_clients(id),
        client_name text not null,
        redirect_uri text not null,
        code_challenge text not null,
        scope text not null,
        resource text not null,
        created text not null,
        last_used text collate "C",
        expires bigint not null,
        revoked smallint not null default 0,
        revoked_at text
    );
    create index if not exists oauth_grants_tenant_user on oauth_grants(tenant_id, user_id);
    create index if not exists oauth_grants_client on oauth_grants(client_id);

    create table if not exists oauth_tokens (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        grant_id bigint not null references oauth_grants(id) on delete cascade,
        kind text not null check (kind in ('code','access','refresh')),
        token_hash text not null unique,
        expires bigint not null,
        used_at bigint,
        created text not null
    );
    create index if not exists oauth_tokens_grant on oauth_tokens(grant_id);
    create index if not exists oauth_tokens_expires on oauth_tokens(expires);

    alter table mcp_audit_log add column if not exists oauth_grant_id bigint;

    do $rls7$
    declare t text; r text; seq text;
    begin
      foreach t in array array['oauth_clients','oauth_requests','oauth_grants','oauth_tokens'] loop
        execute format('alter table %I enable row level security', t);
        foreach r in array array['anon','authenticated'] loop
          if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table %I from %I', t, r);
            if t in ('oauth_grants','oauth_tokens') then   -- identity ids (the other two have text keys)
              seq := pg_get_serial_sequence(quote_ident(t), 'id');
              execute format('revoke all on sequence %s from %I', seq, r);
            end if;
          end if;
        end loop;
      end loop;
    end
    $rls7$;
    """,
    # One-time links (see the SQLite migration 8), with row level security and no Data API privileges.
    8: """
    create table if not exists auth_links (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id bigint not null references users(id) on delete cascade,
        user_email text not null,
        purpose text not null check (purpose in ('reset','invite','confirm','signin')),
        token_hash text not null unique,
        sent_to text,
        session_gen integer not null,
        created_by bigint,
        expires bigint not null,
        created text not null
    );
    create index if not exists auth_links_user on auth_links(tenant_id, user_id, purpose);
    create index if not exists auth_links_expires on auth_links(expires);

    alter table users add column if not exists email_confirmed_at text collate "C";
    update users set last_login = created where last_login is null and must_change_password = 0;   -- see SQLite 8

    do $rls8$
    declare r text; seq text;
    begin
      alter table auth_links enable row level security;
      foreach r in array array['anon','authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
          execute format('revoke all on table auth_links from %I', r);
          seq := pg_get_serial_sequence('auth_links', 'id');
          execute format('revoke all on sequence %s from %I', seq, r);
        end if;
      end loop;
    end
    $rls8$;
    """,
    # Ask TradeComply (see the SQLite migration 9), with row level security and no Data API privileges.
    9: """
    create table if not exists assistant_conversations (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id bigint not null,
        title text not null,
        created text collate "C" not null,
        updated text collate "C" not null
    );
    create index if not exists assistant_conversations_owner on assistant_conversations(tenant_id, user_id, updated);
    create index if not exists assistant_conversations_updated on assistant_conversations(updated);

    create table if not exists assistant_turns (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        conversation_id bigint not null references assistant_conversations(id) on delete cascade,
        messages_json text not null,
        prompt text not null,
        reply text not null,
        checked_json text not null default '[]',
        outcome text not null check (outcome in ('answered','refused','cut_off')),
        context_tokens integer not null default 0,
        created text collate "C" not null
    );
    create index if not exists assistant_turns_conversation on assistant_turns(conversation_id, id);

    create table if not exists assistant_usage (
        id bigint generated by default as identity primary key,
        tenant_id text not null references tenants(id) on delete cascade,
        user_id bigint,
        model text not null,
        input_tokens bigint not null default 0,
        output_tokens bigint not null default 0,
        cache_read_tokens bigint not null default 0,
        cache_write_tokens bigint not null default 0,
        created text collate "C" not null
    );
    create index if not exists assistant_usage_tenant on assistant_usage(tenant_id, created);
    create index if not exists assistant_usage_person on assistant_usage(tenant_id, user_id, created);

    do $rls9$
    declare t text; r text; seq text;
    begin
      foreach t in array array['assistant_conversations','assistant_turns','assistant_usage'] loop
        execute format('alter table %I enable row level security', t);
        foreach r in array array['anon','authenticated'] loop
          if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table %I from %I', t, r);
            seq := pg_get_serial_sequence(quote_ident(t), 'id');
            execute format('revoke all on sequence %s from %I', seq, r);
          end if;
        end loop;
      end loop;
    end
    $rls9$;
    """,
}
APP_TABLES = ("tenants", "users", "api_keys", "projects", "subs", "documents", "field_logs", "audit", "stripe_events",
              "revoked_sessions", "app_secrets", "schema_version", "integration_connections", "webhooks",
              "outbound_events", "mcp_audit_log", "sso_configs", "calendar_tokens", "oauth_clients", "oauth_requests",
              "oauth_grants", "oauth_tokens", "auth_links", "assistant_conversations", "assistant_turns",
              "assistant_usage")
assert sorted(MIGRATIONS) == sorted(PG_MIGRATIONS) and max(MIGRATIONS) == SCHEMA_VERSION

# Account data rights (app/account.py: GET /api/tenant/export, DELETE /api/tenant). Every table in APP_TABLES is
# either a company's own (deleted with it, child tables first) or shared by the whole server and holding nothing that
# names a company. A new table must be added to one of the two, or importing this module fails.
TENANT_DELETE_ORDER = ("outbound_events", "webhooks", "integration_connections", "mcp_audit_log", "calendar_tokens",
                       "oauth_tokens", "oauth_grants", "auth_links", "assistant_turns", "assistant_conversations",
                       "assistant_usage", "sso_configs", "field_logs", "documents", "subs", "projects", "api_keys",
                       "audit", "users")
SHARED_TABLES = {
    "stripe_events": "ids and types of Stripe events already processed (no company id)",
    "revoked_sessions": "random nonces of ended sessions and used one-time links, with an expiry (no company id)",
    "app_secrets": "the server's own generated keys",
    "schema_version": "the database schema version",
    "oauth_clients": "apps registered through OAuth dynamic client registration: name, redirect addresses and the "
                     "hash of a client secret (no company id)",
    "oauth_requests": "sign-in requests from apps waiting for someone to approve them, kept 10 minutes (no company "
                      "id)",
}
assert set(TENANT_DELETE_ORDER) | set(SHARED_TABLES) | {"tenants"} == set(APP_TABLES), "classify every new table"

# What the company export may read: an explicit column list per table, so a secret column added later is never
# exported by accident. tests/test_account.py checks that every column of every table is listed here or in
# TENANT_EXPORT_OMITTED (with the reason), on both backends.
TENANT_EXPORT: dict[str, tuple[str, ...]] = {
    "tenants": ("id", "company_name", "cslb_license", "plan", "status", "trial_ends", "stripe_customer_id",
                "stripe_subscription_id", "settings_json", "created"),
    "users": ("id", "email", "name", "role", "must_change_password", "created", "last_login",
              "email_confirmed_at"),
    "api_keys": ("id", "name", "prefix", "created_by", "created", "last_used", "revoked", "revoked_at", "scopes_json",
                 "rate_limit_per_min", "expires_at"),
    "projects": ("id", "name", "address", "city", "county", "owner_name", "lender_name", "public_or_private",
                 "contract_price", "first_furnishing_date", "last_furnishing_date", "completion_date", "noc_date",
                 "start_date", "status", "extra", "created", "updated"),
    "subs": ("id", "name", "license_number", "scope", "required_class", "coi_expiry", "wc_required", "w9_on_file",
             "dir_number", "email", "phone", "project_id", "created", "updated"),
    "documents": ("id", "project_id", "kind", "title", "body_markdown", "source_tool", "verified", "created",
                  "created_by"),
    "field_logs": ("id", "project_id", "kind", "date", "topic", "attendees", "signatures", "notes", "client_uuid",
                   "created_by", "created"),
    "audit": ("id", "user", "tool", "args_json", "verified", "ok", "source", "created"),
    "integration_connections": ("id", "provider", "status", "config_json", "external_account", "created", "updated",
                                "last_error"),
    "webhooks": ("id", "name", "kind", "events_json", "enabled", "created", "last_status", "last_sent"),
    "outbound_events": ("id", "event", "payload_json", "target", "status", "attempts", "next_attempt", "created",
                        "last_error", "dedupe_key"),
    "mcp_audit_log": ("id", "api_key_id", "oauth_grant_id", "tool", "args_sha256", "status", "duration_ms", "ip",
                      "created"),
    "sso_configs": ("issuer", "client_id", "allowed_domains_json", "enforced", "created"),
    "calendar_tokens": ("created", "revoked"),
    "oauth_grants": ("id", "user_id", "user_email", "client_id", "client_name", "redirect_uri", "scope", "resource",
                     "created", "last_used", "expires", "revoked", "revoked_at"),
    "oauth_tokens": ("id", "grant_id", "kind", "expires", "used_at", "created"),
    "auth_links": ("id", "user_id", "user_email", "purpose", "sent_to", "created_by", "expires", "created"),
    "assistant_conversations": ("id", "user_id", "title", "created", "updated"),
    "assistant_turns": ("id", "conversation_id", "prompt", "reply", "checked_json", "outcome", "context_tokens",
                        "created"),
    "assistant_usage": ("id", "user_id", "model", "input_tokens", "output_tokens", "cache_read_tokens",
                        "cache_write_tokens", "created"),
}
_SAME_FOR_EVERY_ROW = "the company id: the same on every row, given once at the top of the file"
TENANT_EXPORT_OMITTED: dict[str, dict[str, str]] = {
    "tenants": {"stripe_event_created": "internal ordering counter for Stripe events"},
    "users": {"tenant_id": _SAME_FOR_EVERY_ROW, "pw_hash": "password hash (a credential)",
              "session_gen": "session counter (signs everyone out on a password change)"},
    "api_keys": {"tenant_id": _SAME_FOR_EVERY_ROW, "key_hash": "hash of the API key (a credential)"},
    "integration_connections": {"tenant_id": _SAME_FOR_EVERY_ROW,
                                "secret_blob": "encrypted sign-in tokens and keys (credentials)"},
    "webhooks": {"tenant_id": _SAME_FOR_EVERY_ROW, "url": "encrypted address (Slack and Teams addresses are secrets)",
                 "signing_secret_blob": "encrypted signing secret (a credential)"},
    "sso_configs": {"tenant_id": _SAME_FOR_EVERY_ROW, "secret_blob": "encrypted SSO client secret (a credential)"},
    "calendar_tokens": {"tenant_id": _SAME_FOR_EVERY_ROW, "token_hash": "hash of the private calendar link (a "
                                                                        "credential)"},
    "oauth_grants": {"tenant_id": _SAME_FOR_EVERY_ROW,
                     "session_gen": "the person's session counter when they approved (a password change ends it)",
                     "code_challenge": "PKCE challenge of the one-time sign-in code (only used during sign-in)"},
    "oauth_tokens": {"tenant_id": _SAME_FOR_EVERY_ROW,
                     "token_hash": "hash of an access token, refresh token or one-time code (a credential)"},
    "auth_links": {"tenant_id": _SAME_FOR_EVERY_ROW,
                   "token_hash": "hash of a one-time invite, password reset or email confirmation link (a credential)",
                   "session_gen": "the person's session counter when the link was made (a password change ends it)"},
    "assistant_turns": {"tenant_id": _SAME_FOR_EVERY_ROW,
                        "messages_json": "the AI model's raw messages for the turn: thinking blocks are signed data no "
                                         "one can read, and the check inputs and results repeat the question and the "
                                         "answer (both are exported as prompt and reply)"},
    **{t: {"tenant_id": _SAME_FOR_EVERY_ROW} for t in ("projects", "subs", "documents", "field_logs", "audit",
                                                        "outbound_events", "mcp_audit_log", "assistant_conversations",
                                                        "assistant_usage")},
}
# Stored JSON text exported as JSON values (column -> key in the export), 0/1 columns exported as booleans, and
# "is there a secret" flags computed from omitted columns (flag -> column).
EXPORT_JSON = {"tenants": {"settings_json": "settings"}, "api_keys": {"scopes_json": "scopes"},
               "projects": {"extra": "extra"}, "field_logs": {"attendees": "attendees", "signatures": "signatures"},
               "audit": {"args_json": "args"}, "integration_connections": {"config_json": "config"},
               "webhooks": {"events_json": "events"}, "outbound_events": {"payload_json": "payload"},
               "sso_configs": {"allowed_domains_json": "allowed_domains"},
               "assistant_turns": {"checked_json": "checked"}}
EXPORT_BOOL = {"users": ("must_change_password",), "api_keys": ("revoked",), "subs": ("wc_required", "w9_on_file"),
               "documents": ("verified",), "audit": ("verified", "ok"), "webhooks": ("enabled",),
               "sso_configs": ("enforced",), "calendar_tokens": ("revoked",), "oauth_grants": ("revoked",)}
EXPORT_FLAGS = {"integration_connections": {"has_secret": "secret_blob"},
                "webhooks": {"has_signing_secret": "signing_secret_blob"}, "sso_configs": {"has_secret": "secret_blob"}}
EXPORT_PAGED = {t for t, cols in TENANT_EXPORT.items() if "id" in cols and t != "tenants"}   # paged by id
assert set(TENANT_EXPORT) == set(TENANT_DELETE_ORDER) | {"tenants"}

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
# Store.candidates: the only tables and columns type-ahead and search may read (no contact details, no signatures,
# no document bodies), and the only filters.
CANDIDATE_READS = {
    "projects": ("id", "name", "city", "county", "address", "owner_name", "lender_name"),
    "subs": ("id", "name", "license_number", "scope"),
    "documents": ("id", "title"),
    "field_logs": ("kind", "topic", "attendees"),
    "users": ("name",),
}
CANDIDATE_WHERE = {("field_logs", "toolbox_inspection"): "kind in ('toolbox_talk', 'inspection')"}
BOOL_COLUMNS = {"subs": {"wc_required", "w9_on_file"}, "documents": {"verified"}}
HAS_UPDATED = {"projects", "subs"}


# Allowed sort orders and the SQL for each. Equal dates or names are ordered by id, so a list comes back in the same
# order on both backends (without the tie-breaker Postgres and SQLite each return ties in their own order).
ORDER_SQL = {"id desc": "id desc", "id asc": "id asc", "date desc": "date desc, id desc",
             "date asc": "date asc, id asc", "name asc": "name asc, id asc"}
ORDERS = tuple(ORDER_SQL)

# Ids are 64-bit on both backends. A bigger number (a path like /api/projects/99999999999999999999) matches nothing;
# without this check SQLite raises OverflowError (a 500) while Postgres just finds no row.
MAX_ID = 2 ** 63 - 1

MIN_SECRET_LEN = 32

# Secrets reach the store only as blobs from app/crypto.py (SecretBox.encrypt): "v<version>.<key id>.<base64url>".
# The store refuses anything else in a secret column, so plaintext can't be saved there by mistake.
SECRET_BLOB_RE = re.compile(r"^v[0-9]{1,3}\.[0-9a-f]{8}\.[A-Za-z0-9_-]{20,}$")
SECRET_BLOB_COLUMNS = {"integration_connections": ("secret_blob",), "webhooks": ("url", "signing_secret_blob"),
                       "sso_configs": ("secret_blob",)}
MAX_CONFIG_JSON = 20_000
MAX_PAYLOAD_JSON = 64_000
TARGET_RE = re.compile(r"^(webhook|connector|handler):[A-Za-z0-9_.:-]{1,80}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ANY = object()          # "no expectation" default for compare-and-set arguments
_KEEP = object()         # "leave this column as it is" default


def _check_provider(provider: str) -> None:
    if not isinstance(provider, str) or not PROVIDER_RE.match(provider):
        raise ValueError(f"bad provider id {str(provider)[:50]!r}")


def _check_blob(value, column: str):
    if value is None:
        return None
    if not isinstance(value, str) or not SECRET_BLOB_RE.match(value) or len(value) > 200_000:
        raise ValueError(f"{column} must be an encrypted blob from app.crypto.SecretBox.encrypt, never plaintext")
    return value


def _dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _loads(text, default):
    try:
        out = json.loads(text) if text else default
    except ValueError:
        return default
    return out if isinstance(out, type(default)) else default


def _limit(limit, top: int = 1000) -> int:
    try:
        return max(1, min(int(limit), top))
    except (TypeError, ValueError):
        return 100
MIGRATION_LOCK_ID = 7_310_442_871_201      # pg_advisory_xact_lock key for schema migrations (any fixed bigint)


_ADD_COLUMN_RE = re.compile(r"alter table (\w+) add column (\w+)[^;]*;", re.I)


def _skip_existing_columns(con, script: str) -> str:
    """SQLite has no `add column if not exists`: drop ALTER TABLE ... ADD COLUMN statements (from the fixed migration
    text above, never user input) whose column is already there, so a migration can run again safely."""
    def keep(m):
        cols = {r[1] for r in con.execute(f"pragma table_info({m.group(1)})").fetchall()}
        return "" if m.group(2) in cols else m.group(0)
    return _ADD_COLUMN_RE.sub(keep, script)


def _check_table(table: str) -> None:
    """Table names are interpolated into SQL, so only the fixed tenant-owned tables are accepted."""
    if table not in COLUMNS:
        raise ValueError(f"bad table {table!r}")


def _id_ok(value) -> bool:
    """False for an int id no row can have (outside the signed 64-bit range). Other types are left to the query."""
    return not (isinstance(value, int) and not isinstance(value, bool) and not -MAX_ID - 1 <= value <= MAX_ID)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_in(seconds: float, now: datetime | None = None) -> str:
    """The store's timestamp format (UTC, whole seconds, "+00:00"), `seconds` from now (or from `now`)."""
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=seconds)).astimezone(timezone.utc).replace(microsecond=0).isoformat()


def to_store_time(value) -> str:
    """A datetime, a date ("2026-11-01" = midnight UTC) or an ISO timestamp, in the store's text format, so that
    comparing it with stored timestamps as text compares the times. ValueError for anything else."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if len(text) == 10:
            text += "T00:00:00+00:00"
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"not a date or ISO timestamp: {str(value)[:40]!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if not 1900 <= dt.year <= 2200:
        raise ValueError("timestamp out of range")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


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
    """Parameters as psycopg should send them so both backends store and match the same values:
    - booleans as 0/1 (the columns are smallint, as SQLite stores them); psycopg would send a Postgres boolean.
    - a NUL character in text becomes U+FFFD. Postgres text can't hold NUL (psycopg raises DataError), so without this
      a stray NUL in a note, a company name or a login email is a 500 on Postgres but saved or matched on SQLite. An
      offline field log with one would never sync. Replacing it (not dropping it) keeps lookups exact: a login with
      `a\\x00b@x` still doesn't match `ab@x`, so it can't be used to dodge the per-email rate limit."""
    out = []
    for p in params:
        if isinstance(p, bool):
            p = int(p)
        elif isinstance(p, str) and "\x00" in p:
            p = p.replace("\x00", "\ufffd")
        out.append(p)
    return out


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
        from psycopg.pq import TransactionStatus
        # A statement failed and the caller caught the error inside the `with` block. SQLite would go on and commit
        # the other statements; Postgres has already aborted the whole transaction, and psycopg's commit() would
        # then roll back without a word. Say so instead of losing the writes quietly.
        aborted = ok and not self._conn.closed and self._conn.info.transaction_status == TransactionStatus.INERROR
        broken = False
        try:
            if ok and not aborted:
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception:
            broken = True
            if ok:
                raise
        finally:
            self._pool.put(self._conn, self._born, discard=broken or self._conn.broken)
        if aborted:
            raise TransactionAborted("a statement in this transaction failed, so PostgreSQL rolled all of it back; "
                                     "nothing in it was saved")


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
    def _pick_pg_url(self) -> str:
        """DATABASE_URL may list several space-separated URLs (e.g. two pooler hosts); the first that connects and
        authenticates wins. Never falls back to SQLite: if none works, _open_pg raises StorageUnavailable as usual."""
        urls = self.database_url.split()
        if len(urls) == 1:
            return urls[0]
        import psycopg
        for u in urls:
            try:
                with psycopg.connect(u, **pg_connect_kwargs(u)) as probe:
                    probe.execute("select 1")
                log.info("DATABASE_URL: using %s", describe_pg_url(u))
                return u
            except Exception as e:  # noqa: BLE001 - try the next candidate; messages may quote secrets, so log the class only
                log.warning("DATABASE_URL candidate %s failed (%s)", describe_pg_url(u), e.__class__.__name__)
        return urls[-1]

    def _open_pg(self, connect_attempts: int | None) -> None:
        url = self.database_url = self._pick_pg_url()
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
                    con.executescript(_skip_existing_columns(con, MIGRATIONS[v]))
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
            if not _id_ok(v):
                return []
            sql += f" and {k} = ?"
            params.append(v)
        sql += f" order by {ORDER_SQL[order]} limit ?"
        params.append(int(limit))
        with self.connect() as con:
            return [self._row(table, r) for r in con.execute(sql, params).fetchall()]

    def candidates(self, tenant_id: str, table: str, columns, limit: int = 2000,
                   where: str | None = None) -> list[dict]:
        """The tenant's newest rows (id desc) with only the named columns, for type-ahead and search: matching,
        distinct values and case/accent folding happen in Python (app/search.py), the same on both backends.
        Table, columns and `where` come from the fixed allow-list CANDIDATE_READS, never from a request. Field logs
        are always read without incident reports (they name injured workers and witnesses) and never with their
        signatures column."""
        allowed = CANDIDATE_READS.get(table)
        cols = tuple(columns)
        if allowed is None or not cols or any(c not in allowed for c in cols):
            raise ValueError(f"bad candidate read {table!r} {cols!r}")
        sql = f"select {', '.join(cols)} from {table} where tenant_id = ?"
        if table == "field_logs":
            where = where or "toolbox_inspection"
        if where is not None:
            clause = CANDIDATE_WHERE.get((table, where))
            if clause is None:
                raise ValueError(f"bad candidate filter {where!r}")
            sql += f" and {clause}"
        sql += " order by id desc limit ?"
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql, (tenant_id, max(1, min(int(limit), 5000)))).fetchall()]

    def get(self, table: str, tenant_id: str, row_id) -> dict | None:
        _check_table(table)
        if not _id_ok(row_id):
            return None
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
        if not _id_ok(row_id):
            return None
        if table in HAS_UPDATED:
            values["updated"] = now_iso()
        if values:
            sets = ", ".join(f"{c} = ?" for c in values)
            with self.connect() as con:
                con.execute(f"update {table} set {sets} where tenant_id = ? and id = ?",
                            [*values.values(), tenant_id, row_id])
        return self.get(table, tenant_id, row_id)

    def _read_for_update(self, con, sql: str, params):
        """The first read of a read-modify-write transaction, with the row locked until the transaction ends, so
        two requests changing the same row one after the other each see the other's write (no lost update).
        Postgres: SELECT ... FOR UPDATE. SQLite: BEGIN IMMEDIATE takes the database write lock before reading."""
        if self.backend == "sqlite":
            con.execute("begin immediate")
            return con.execute(sql, params).fetchone()
        return con.execute(sql + " for update", params).fetchone()

    def modify(self, table: str, tenant_id: str, row_id, change) -> dict | None:
        """Read-modify-write of one tenant-owned row in one locked transaction: change(current row as a dict)
        returns the fields to write (it may raise, which rolls back). Returns the updated row, or None if there is
        no such row for this tenant. `change` must not use the store (it would need a second connection)."""
        _check_table(table)
        if not _id_ok(row_id):
            return None
        where = f"from {table} where tenant_id = ? and id = ?"
        with self.connect() as con:
            row = self._read_for_update(con, f"select * {where}", (tenant_id, row_id))
            if row is None:
                return None
            values = self._clean(table, change(self._row(table, row)) or {})
            if table in HAS_UPDATED:
                values["updated"] = now_iso()
            if values:
                con.execute(f"update {table} set {', '.join(f'{c} = ?' for c in values)} where tenant_id = ? "
                            "and id = ?", [*values.values(), tenant_id, row_id])
            return self._row(table, con.execute(f"select * {where}", (tenant_id, row_id)).fetchone())

    # what the activity-log row of a deleted record names (never its body, notes or signatures)
    DELETE_AUDIT = {"projects": ("project.delete", ("name", "status")), "subs": ("sub.delete", ("name", "license_number")),
                    "documents": ("document.delete", ("title", "kind")),
                    "field_logs": ("field_log.delete", ("kind", "date", "project_id"))}

    def delete(self, table: str, tenant_id: str, row_id, audit_by: str | None = None) -> bool:
        """Delete one row. With audit_by (the person's label), the activity-log row is written in the same
        transaction, so a record can't disappear without a trace."""
        _check_table(table)
        if not _id_ok(row_id):
            return False
        with self.connect() as con:
            row = con.execute(f"select * from {table} where tenant_id = ? and id = ?",
                              (tenant_id, row_id)).fetchone() if audit_by else None
            gone = con.execute(f"delete from {table} where tenant_id = ? and id = ?",
                               (tenant_id, row_id)).rowcount > 0
            if gone and audit_by and table in self.DELETE_AUDIT:
                action, cols = self.DELETE_AUDIT[table]
                row = dict(row) if row else {}
                self._audit_row(con, tenant_id, audit_by, action, {"id": row_id, **{c: row.get(c) for c in cols}},
                                None)
            return gone

    # ------------------------------------------------------------------ tenants
    def create_tenant(self, company_name: str, cslb_license: str | None, trial_ends: str | None,
                      status: str = "trialing", plan: str = "core") -> dict:
        tid = "t_" + secrets.token_hex(8)
        with self.connect() as con:
            con.execute("insert into tenants (id, company_name, cslb_license, plan, status, trial_ends, created) "
                        "values (?,?,?,?,?,?,?)", (tid, company_name, cslb_license, plan, status, trial_ends, now_iso()))
        return self.get_tenant(tid)

    def create_tenant_with_owner(self, company_name: str, cslb_license: str | None, trial_ends: str | None,
                                 email: str, pw_hash: str, name: str | None = None, status: str = "trialing",
                                 plan: str = "core") -> tuple[dict, dict]:
        """Sign-up: the company and its owner in one transaction. If the email is taken (two sign-ups racing past
        the app's "already exists" check, e.g. a double-submitted form), the unique email index raises
        IntegrityError and the tenant insert is rolled back with it, so no company is left without an owner."""
        tid = "t_" + secrets.token_hex(8)
        created = now_iso()
        with self.connect() as con:
            con.execute("insert into tenants (id, company_name, cslb_license, plan, status, trial_ends, created) "
                        "values (?,?,?,?,?,?,?)", (tid, company_name, cslb_license, plan, status, trial_ends, created))
            uid = self._insert_id(con, "insert into users (tenant_id, email, name, pw_hash, role, "
                                       "must_change_password, created, last_login) values (?,?,?,?,?,?,?,?)",
                                  (tid, email.strip().lower(), name, pw_hash, "owner", 0, created, created))
            tenant = self._row("tenants", con.execute("select * from tenants where id = ?", (tid,)).fetchone())
            user = dict(con.execute("select * from users where id = ?", (uid,)).fetchone())
        return tenant, user

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

    def modify_tenant_settings(self, tenant_id: str, change) -> dict | None:
        """Read-modify-write of a tenant's settings in one locked transaction: change(current settings) returns
        the new settings (it may raise, which rolls back). Without the lock, rotating the calendar link while
        someone saves a setting could write the old link generation back and revive a link meant to be dead."""
        with self.connect() as con:
            row = self._read_for_update(con, "select settings_json from tenants where id = ?", (tenant_id,))
            if row is None:
                return None
            try:
                current = json.loads(row[0] or "{}")
            except ValueError:
                current = {}
            new = change(current if isinstance(current, dict) else {})
            con.execute("update tenants set settings_json = ? where id = ?",
                        (json.dumps(new, separators=(",", ":")), tenant_id))
        return self.get_tenant(tenant_id)

    def update_tenant_from_event(self, tenant_id: str, created: int, status: str | None = None,
                                 **fields) -> bool:
        """Apply a Stripe event to a tenant. `fields` (customer and subscription ids) are always written; `status`
        only if the event is not older than the last one whose status was applied (tenants.stripe_event_created),
        and that check is part of the UPDATE itself. Reading the tenant first and deciding in Python would let two
        webhook deliveries processed at the same moment both pass the check, and the older one could land last
        (a late "active" undoing a cancellation). Returns whether the status was applied."""
        allowed = {"company_name", "cslb_license", "plan", "trial_ends", "stripe_customer_id", "stripe_subscription_id"}
        if set(fields) - allowed:
            raise ValueError(f"can't set {sorted(set(fields) - allowed)} from a Stripe event")
        values = dict(fields)
        if status is not None and status not in TENANT_STATUSES:
            raise ValueError(f"bad tenant status {status!r}")
        applied = False
        with self.connect() as con:
            if values:
                con.execute(f"update tenants set {', '.join(f'{k} = ?' for k in values)} where id = ?",
                            [*values.values(), tenant_id])
            if status is not None and created:
                applied = con.execute("update tenants set status = ?, stripe_event_created = ? "
                                      "where id = ? and stripe_event_created <= ?",
                                      (status, int(created), tenant_id, int(created))).rowcount > 0
            elif status is not None:           # an event without a timestamp: applied, and orders nothing
                applied = con.execute("update tenants set status = ? where id = ?", (status, tenant_id)).rowcount > 0
        return applied

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
        if not _id_ok(user_id):
            return None
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
            rows = con.execute("select id, email, name, role, created, last_login, must_change_password, "
                               "email_confirmed_at from users where tenant_id = ? order by id", (tenant_id,)).fetchall()
            return [dict(r) for r in rows]

    def update_user(self, tenant_id: str, user_id: int, **fields) -> dict | None:
        allowed = {"pw_hash", "role", "name", "session_gen", "must_change_password", "last_login"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if "role" in values and values["role"] not in ROLES:
            raise ValueError("role must be owner, office or field")
        if not _id_ok(user_id):
            return None
        if values:
            with self.connect() as con:
                con.execute(f"update users set {', '.join(f'{k} = ?' for k in values)} where tenant_id = ? and id = ?",
                            [*values.values(), tenant_id, user_id])
                if values.get("role", "owner") != "owner":      # no longer an owner: the links they made end too
                    con.execute("delete from auth_links where tenant_id = ? and created_by = ?", (tenant_id, user_id))
        with self.connect() as con:
            row = con.execute("select * from users where tenant_id = ? and id = ?", (tenant_id, user_id)).fetchone()
            return dict(row) if row else None

    def delete_user(self, tenant_id: str, user_id: int, audit_by: str | None = None) -> bool:
        """Removes the person and, in the same transaction, ends every app connection they approved: SQLite can
        give the next new user the same id, who must never inherit a live grant. The row goes first: on Postgres
        that waits for an approval holding it (approve_oauth_request), whose grant is then ended here too. With
        audit_by, the "user.remove" activity row is written in the same transaction."""
        if not _id_ok(user_id):
            return False
        with self.connect() as con:
            before = con.execute("select email, role, last_login from users where tenant_id = ? and id = ?",
                                 (tenant_id, user_id)).fetchone() if audit_by else None
            gone = self._delete_user_rows(con, tenant_id, user_id)
            if gone and audit_by and before:
                before = dict(before)
                self._audit_row(con, tenant_id, audit_by, "user.remove", {
                    "user_id": user_id, "email": before["email"], "role": before["role"],
                    "was_pending": not before["last_login"]}, None)
            return gone

    def release_pending_invitee(self, email: str, except_tenant: str | None, reason: str) -> dict | None:
        """An address held only by someone a company added who never joined (never signed in, address never
        confirmed, no temporary password) doesn't reserve it for good: a sign-up with that address, or another
        company's invite, removes that pending row (with a "user.invite_released" activity row in its company, and
        its invite links), and the caller then creates the account. Anyone else (joined, confirmed, or in
        `except_tenant`) keeps the address: returns None. In one locked transaction with the person's row, the lock
        use_auth_link takes, so an invite accepted at the same moment either wins (the row is no longer pending)
        or finds its link gone."""
        email = (email or "").strip().lower()
        with self.connect() as con:
            if self.backend == "sqlite":
                con.execute("begin immediate")
                row = con.execute(f"select * from users where {self._email_eq}", (email,)).fetchone()
            else:
                row = con.execute(f"select * from users where {self._email_eq} for update", (email,)).fetchone()
            if row is None:
                return None
            u = dict(row)
            if u["tenant_id"] == except_tenant or u["last_login"] or u["email_confirmed_at"] \
                    or u["must_change_password"]:
                return None
            self._delete_user_rows(con, u["tenant_id"], u["id"])
            self._audit_row(con, u["tenant_id"], self.OAUTH_ACTOR, "user.invite_released",
                            {"user_id": u["id"], "email": u["email"], "role": u["role"], "reason": reason}, None)
            return u

    def _delete_user_rows(self, con, tenant_id: str, user_id: int) -> bool:
        """delete_user's work, in the caller's transaction."""
        gone = con.execute("delete from users where tenant_id = ? and id = ?", (tenant_id, user_id)).rowcount > 0
        self._end_oauth_grants(con, "tenant_id = ? and user_id = ?", (tenant_id, user_id), "person_removed")
        # their links, and the invite and reset links they made for others as an owner
        con.execute("delete from auth_links where tenant_id = ? and (user_id = ? or created_by = ?)",
                    (tenant_id, user_id, user_id))
        # their Ask TradeComply chats go too (a new person given the same id must not see them); usage rows stay
        # for the company's monthly count, no longer naming anyone
        con.execute("delete from assistant_turns where tenant_id = ? and conversation_id in (select id from "
                    "assistant_conversations where tenant_id = ? and user_id = ?)", (tenant_id, tenant_id, user_id))
        con.execute("delete from assistant_conversations where tenant_id = ? and user_id = ?", (tenant_id, user_id))
        con.execute("update assistant_usage set user_id = null where tenant_id = ? and user_id = ?",
                    (tenant_id, user_id))
        return gone

    # ------------------------------------------------------------------ API keys
    # Governance fields (schema 6, docs/mcp-security.md). The store checks types and sizes only; which tool names
    # and categories a scope may name is decided by app/mcp_governance.py.
    KEY_POLICY_FIELDS = ("scopes", "rate_limit_per_min", "expires_at")
    MAX_KEY_RATE = 100_000
    LAST_USED_EVERY_S = 60          # last_used is a hint: rewritten at most once a minute, not on every call

    @staticmethod
    def _key_policy_values(fields: dict) -> dict:
        out = {}
        if "scopes" in fields:
            sc = fields["scopes"]
            if sc is not None and not isinstance(sc, dict):
                raise ValueError("scopes must be a dict or None")
            text = None if sc is None else _dumps(sc)
            if text is not None and len(text) > MAX_CONFIG_JSON:
                raise ValueError("scopes are too large")
            out["scopes_json"] = text
        if "rate_limit_per_min" in fields:
            v = fields["rate_limit_per_min"]
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= Store.MAX_KEY_RATE):
                raise ValueError(f"rate_limit_per_min must be a whole number from 1 to {Store.MAX_KEY_RATE}, or None")
            out["rate_limit_per_min"] = v
        if "expires_at" in fields:
            v = fields["expires_at"]
            out["expires_at"] = None if v in (None, "") else to_store_time(v)
        return out

    def create_api_key(self, tenant_id: str, name: str, key_hash: str, prefix: str, created_by: int | None, *,
                       scopes: dict | None = None, rate_limit_per_min: int | None = None,
                       expires_at=None) -> dict:
        pol = self._key_policy_values({"scopes": scopes, "rate_limit_per_min": rate_limit_per_min,
                                       "expires_at": expires_at})
        with self.connect() as con:
            kid = self._insert_id(con, "insert into api_keys (tenant_id, name, key_hash, prefix, created_by, "
                                       "created, scopes_json, rate_limit_per_min, expires_at) "
                                       "values (?,?,?,?,?,?,?,?,?)",
                                  (tenant_id, name, key_hash, prefix, created_by, now_iso(), pol["scopes_json"],
                                   pol["rate_limit_per_min"], pol["expires_at"]))
            return self._key(con.execute("select * from api_keys where id = ?", (kid,)).fetchone())

    @staticmethod
    def _key(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d.pop("key_hash", None)          # never leaves the store
        d["revoked"] = bool(d["revoked"])
        d["scopes"] = _loads(d.pop("scopes_json", None), {}) or None     # None = the default scope
        return d

    def list_api_keys(self, tenant_id: str) -> list[dict]:
        with self.connect() as con:
            return [self._key(r) for r in con.execute("select * from api_keys where tenant_id = ? order by id desc",
                                                      (tenant_id,)).fetchall()]

    def get_api_key(self, tenant_id: str, key_id) -> dict | None:
        if not _id_ok(key_id):
            return None
        with self.connect() as con:
            return self._key(con.execute("select * from api_keys where tenant_id = ? and id = ?",
                                         (tenant_id, key_id)).fetchone())

    def update_api_key(self, tenant_id: str, key_id, **fields) -> dict | None:
        """Change an active key's name or policy (scopes, rate_limit_per_min, expires_at; None = the default).
        Revoked keys can't be changed. Returns the key, or None if this tenant has no such active key."""
        bad = set(fields) - {"name", *self.KEY_POLICY_FIELDS}
        if bad:
            raise ValueError(f"unknown API key field(s) {sorted(bad)}")
        values = self._key_policy_values(fields)
        if "name" in fields:
            name = str(fields["name"] or "").strip()
            if not 1 <= len(name) <= 100:
                raise ValueError("name must be 1 to 100 characters")
            values["name"] = name
        if not _id_ok(key_id):
            return None
        with self.connect() as con:
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                if con.execute(f"update api_keys set {sets} where tenant_id = ? and id = ? and revoked = 0",
                               (*values.values(), tenant_id, key_id)).rowcount == 0:
                    return None
            row = con.execute("select * from api_keys where tenant_id = ? and id = ? and revoked = 0",
                              (tenant_id, key_id)).fetchone()
            return self._key(row)

    def revoke_api_key(self, tenant_id: str, key_id: int) -> bool:
        if not _id_ok(key_id):
            return False
        with self.connect() as con:
            return con.execute("update api_keys set revoked = 1, revoked_at = ? where tenant_id = ? and id = ? "
                               "and revoked = 0", (now_iso(), tenant_id, key_id)).rowcount > 0

    def revoke_user_api_keys(self, tenant_id: str, user_id: int, reason: str | None = None,
                             audit_by: str | None = None) -> int:
        """Keys act for the company, not the person; when someone leaves (or loses office access) their keys go.
        So do they when the person changes or resets their password (reason, with an activity row): a key made with a
        stolen session must not outlive the recovery."""
        if not _id_ok(user_id):
            return 0
        with self.connect() as con:
            return self._revoke_user_keys(con, tenant_id, user_id, reason, audit_by)

    def _revoke_user_keys(self, con, tenant_id: str, user_id: int, reason: str | None = None,
                          audit_by: str | None = None) -> int:
        n = con.execute("update api_keys set revoked = 1, revoked_at = ? where tenant_id = ? and created_by = ? "
                        "and revoked = 0", (now_iso(), tenant_id, user_id)).rowcount
        if n and reason:
            self._audit_row(con, tenant_id, audit_by or self.OAUTH_ACTOR, "api_key.revoke",
                            {"user_id": user_id, "keys": n, "reason": reason}, None)
        return n

    def api_key_by_hash(self, key_hash: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("select * from api_keys where key_hash = ?", (key_hash,)).fetchone()
            if row is None:
                return None
            d = self._key(row)
            now = now_iso()
            if not d["revoked"] and (not d.get("last_used") or d["last_used"] < iso_in(-self.LAST_USED_EVERY_S)):
                con.execute("update api_keys set last_used = ? where id = ?", (now, d["id"]))
                d["last_used"] = now
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

    def claim_nonce(self, nonce: str, expires: int) -> bool:
        """Single-use tokens (e.g. an OAuth `state`): True the first time `nonce` is presented, False every later time
        until `expires` (unix seconds) has passed. Kept in revoked_sessions; callers prefix the nonce with its use
        ("oauth:...") so it can never collide with a session nonce."""
        with self.connect() as con:
            con.execute("delete from revoked_sessions where expires < ?",
                        (int(datetime.now(timezone.utc).timestamp()),))
            return con.execute("insert into revoked_sessions (nonce, expires) values (?, ?) on conflict (nonce) "
                               "do nothing", (str(nonce)[:200], int(expires))).rowcount == 1

    def release_nonce(self, nonce: str) -> None:
        """Undo claim_nonce when the work it guarded failed, so the sender's retry of the same message is accepted.
        Only prefixed single-use nonces ("twilio:...") can be released, never a revoked session's nonce."""
        if ":" not in str(nonce):
            raise ValueError("only a prefixed single-use nonce can be released")
        with self.connect() as con:
            con.execute("delete from revoked_sessions where nonce = ?", (str(nonce)[:200],))

    # ------------------------------------------------------------------ one-time links (app/auth_links.py)
    # A link joins to its person only while that person's email and session counter are what they were when it was
    # made: a password change, a reset, an email change or SSO becoming required (each bumps or changes one) ends
    # every outstanding link, and a removed person's id reused by SQLite never matches. A link an owner made (an
    # invite or a reset link to hand over) works only while that person is still an owner of the company: removing
    # or demoting them deletes those links too (delete_user, update_user), and this join refuses any left behind.
    _AUTH_LINK_SQL = ("select l.id, l.tenant_id, l.user_id, l.purpose, l.sent_to, l.created_by, l.expires, u.email, "
                      "u.role, u.name, u.last_login, u.email_confirmed_at, t.company_name from auth_links l "
                      "join users u on u.id = l.user_id and u.tenant_id = l.tenant_id "
                      "and u.session_gen = l.session_gen and lower(u.email) = lower(l.user_email) "
                      "join tenants t on t.id = l.tenant_id "
                      "left join users c on c.id = l.created_by and c.tenant_id = l.tenant_id and c.role = 'owner' "
                      "where l.token_hash = ? and l.purpose = ? and l.expires > ? "
                      "and (l.created_by is null or c.id is not null)")

    def create_auth_links(self, tenant_id: str, user_id: int, purpose: str, tokens: list[tuple[str, str | None]],
                          ttl_s: int, *, created_by: int | None = None, keep: int = 0) -> int | None:
        """Store one-time links for a person of this company: `tokens` is a list of (SHA-256 of the token, the
        address it is emailed to or None for a link shown to an owner). In the same transaction: expired links
        (anyone's) are deleted, and of this person's earlier links for this purpose only the newest `keep` stay
        (for a confirmation, only ones sent to the same address: asking to confirm another address ends the rest).
        Returns the expiry (unix seconds), or None if the person isn't in this company."""
        if purpose not in AUTH_LINK_PURPOSES or not tokens:
            raise ValueError("bad one-time link")
        if not all(isinstance(h, str) and SHA256_RE.match(h) for h, _ in tokens):
            raise ValueError("store only the SHA-256 of a one-time link token")
        if not _id_ok(user_id):
            return None
        now = int(time.time())
        expires = now + int(ttl_s)
        with self.connect() as con:
            u = self._read_for_update(con, "select id, email, session_gen from users where tenant_id = ? and id = ?",
                                      (tenant_id, user_id))
            if u is None:
                return None
            con.execute("delete from auth_links where expires < ?", (now,))
            same = purpose == "confirm"
            con.execute("delete from auth_links where tenant_id = ? and user_id = ? and purpose = ? and id not in "
                        "(select id from auth_links where tenant_id = ? and user_id = ? and purpose = ? "
                        + ("and sent_to = ? " if same else "") + "order by id desc limit ?)",
                        (tenant_id, user_id, purpose, tenant_id, user_id, purpose,
                         *([(tokens[0][1] or "").strip().lower()] if same else []), max(0, int(keep))))
            for token_hash, sent_to in tokens:
                con.execute("insert into auth_links (tenant_id, user_id, user_email, purpose, token_hash, sent_to, "
                            "session_gen, created_by, expires, created) values (?,?,?,?,?,?,?,?,?,?)",
                            (tenant_id, user_id, u["email"], purpose, token_hash,
                             sent_to.strip().lower() if sent_to else None, u["session_gen"], created_by, expires,
                             now_iso()))
        return expires

    def auth_link(self, token_hash: str, purpose: str) -> dict | None:
        """A live link with its person and company, without using it up (opening a link changes nothing)."""
        if purpose not in AUTH_LINK_PURPOSES or not isinstance(token_hash, str) or not SHA256_RE.match(token_hash):
            return None
        with self.connect() as con:
            row = con.execute(self._AUTH_LINK_SQL, (token_hash, purpose, int(time.time()))).fetchone()
        return dict(row) if row else None

    def confirm_link_sent(self, tenant_id: str, user_id: int) -> bool:
        """Is a confirmation link for the person's current email still open (sent at sign-up, say)?"""
        with self.connect() as con:
            return con.execute("select 1 from auth_links l join users u on u.id = l.user_id and u.tenant_id = "
                               "l.tenant_id and u.session_gen = l.session_gen and l.sent_to = lower(u.email) "
                               "where l.tenant_id = ? and l.user_id = ? and l.purpose = 'confirm' and l.expires > ?",
                               (tenant_id, user_id, int(time.time()))).fetchone() is not None

    def use_auth_link(self, token_hash: str, purpose: str, *, user_id: int | None = None, pw_hash: str | None = None,
                      name: str | None = None) -> dict | None:
        """Use a link once, in one locked transaction (SQLite: the write lock; Postgres: FOR UPDATE on the person,
        then on the link), so two requests with the same link can't both succeed: the second waits, then finds it gone.
        `user_id`: only for that person (an email confirmation needs the person's own session). By purpose:
          reset    new password, session counter + 1 (every session ends), all their links deleted, connected apps
                   ended (oauth.revoked rows), the API keys they made revoked, activity row auth.password_reset
          invite   their password and name, session counter + 1, signed in now (last_login), the address confirmed
                   when the link came by email, all their links deleted, activity row user.joined
          confirm  the address the link was emailed to becomes confirmed; if it isn't their current email, it
                   becomes their email (IntegrityError if someone else has it), activity row auth.email_confirmed
                   or auth.email_changed
        Returns the person's updated row plus previous_email (an email change) and company_name, or None."""
        if purpose not in ("reset", "invite", "confirm") or not isinstance(token_hash, str) \
                or not SHA256_RE.match(token_hash):
            return None
        if purpose in ("reset", "invite") and not pw_hash:
            raise ValueError("a new password hash is required")
        params = (token_hash, purpose, int(time.time()))
        with self.connect() as con:
            if self.backend == "sqlite":
                con.execute("begin immediate")
                row = con.execute(self._AUTH_LINK_SQL, params).fetchone()
            else:
                # The person first, then the link: the order create_auth_links and delete_user lock in. Locking both
                # at once (FOR UPDATE OF l, u) let two of one person's links used at the same moment each hold a row
                # the other needed (a deadlock, a 500). The link is read again under the lock: the other request
                # may have used it, or ended it, meanwhile.
                row = con.execute(self._AUTH_LINK_SQL, params).fetchone()
                if row is not None:
                    con.execute("select id from users where tenant_id = ? and id = ? for update",
                                (row["tenant_id"], row["user_id"]))
                    row = con.execute(self._AUTH_LINK_SQL + " for update of l", params).fetchone()
            if row is None or (user_id is not None and row["user_id"] != user_id) \
                    or (purpose == "confirm" and not row["sent_to"]):
                return None
            link = dict(row)
            tid, uid, email, ts = link["tenant_id"], link["user_id"], link["email"], now_iso()
            emailed_here = (link["sent_to"] or "") == email.lower()
            who, previous, keys_revoked = (tid, uid), None, 0
            if purpose == "reset":
                con.execute("update users set pw_hash = ?, session_gen = session_gen + 1, must_change_password = 0, "
                            "email_confirmed_at = coalesce(email_confirmed_at, ?) where tenant_id = ? and id = ?",
                            (pw_hash, ts if emailed_here else None, *who))
                con.execute("delete from auth_links where tenant_id = ? and user_id = ?", who)
                self._end_oauth_grants(con, "tenant_id = ? and user_id = ?", who, "password_reset")
                self._audit_row(con, tid, email, "auth.password_reset",
                                {"user_id": uid, "via": "email" if link["sent_to"] else "owner_link"}, None)
                keys_revoked = self._revoke_user_keys(con, tid, uid, "password_reset", email)
            elif purpose == "invite":
                con.execute("update users set pw_hash = ?, name = coalesce(?, name), session_gen = session_gen + 1, "
                            "must_change_password = 0, last_login = ?, email_confirmed_at = "
                            "coalesce(email_confirmed_at, ?) where tenant_id = ? and id = ?",
                            (pw_hash, name, ts, ts if emailed_here else None, *who))
                con.execute("delete from auth_links where tenant_id = ? and user_id = ?", who)
                self._audit_row(con, tid, email, "user.joined",
                                {"user_id": uid, "via": "email" if emailed_here else "copied_link"}, None)
            else:
                target = link["sent_to"] or ""
                if emailed_here:
                    con.execute("update users set email_confirmed_at = ? where tenant_id = ? and id = ?", (ts, *who))
                    con.execute("delete from auth_links where tenant_id = ? and user_id = ? and purpose = 'confirm'",
                                who)
                    self._audit_row(con, tid, email, "auth.email_confirmed", {"user_id": uid}, None)
                else:
                    con.execute("update users set email = ?, email_confirmed_at = ? where tenant_id = ? and id = ?",
                                (target, ts, *who))
                    con.execute("delete from auth_links where tenant_id = ? and user_id = ?", who)
                    self._audit_row(con, tid, target, "auth.email_changed", {"user_id": uid, "from": email,
                                                                             "to": target}, None)
                    previous = email
            user = dict(con.execute("select * from users where tenant_id = ? and id = ?", who).fetchone())
        return {**user, "previous_email": previous, "company_name": link["company_name"],
                "api_keys_revoked": keys_revoked}

    # ------------------------------------------------------------------ audit
    def _tenant_alive(self, con, tenant_id: str | None) -> bool:
        """For a write to a table with no foreign key to tenants (the activity log): is the company still there?
        The company row is locked in a mode that waits for a deletion in progress (Postgres: FOR KEY SHARE, which
        conflicts only with delete_tenant's FOR UPDATE, like a foreign key check; SQLite: the write lock), so a
        request that was already running when its company was deleted can't leave a row behind afterwards. Must be
        the first statement of the transaction. None (an anonymous call) is always alive."""
        if tenant_id is None:
            return True
        if self.backend == "sqlite":
            con.execute("begin immediate")
            return con.execute("select 1 from tenants where id = ?", (tenant_id,)).fetchone() is not None
        return con.execute("select 1 from tenants where id = ? for key share", (tenant_id,)).fetchone() is not None

    def audit(self, tenant_id: str | None, user: str | None, tool: str, args: dict | None, verified: bool | None,
              ok: bool = True, source: str = "api") -> None:
        """One activity-log row. Nothing is written for a company that no longer exists (deleted while this
        request was running)."""
        with self.connect() as con:
            if self._tenant_alive(con, tenant_id):
                self._audit_row(con, tenant_id, user, tool, args, verified, ok, source)

    @staticmethod
    def _audit_row(con, tenant_id, user, tool, args, verified, ok=True, source="api") -> None:
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
        if not _id_ok(audit_id):
            return None
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

    # ================================================================== integration foundation (schema version 5)
    # Every method here that takes tenant_id filters by it, and writable columns come from fixed whitelists (unknown
    # fields raise ValueError, so a typo in a connector fails its tests instead of being dropped). Methods marked
    # OPERATOR ONLY work across tenants: they serve the event delivery worker, key rotation, login by email domain
    # and the platform operator. Never call them with a tenant's input as the tenant selector.

    # ------------------------------------------------------------------ integration_connections
    CONNECTION_FIELDS = ("status", "config", "secret_blob", "external_account", "last_error")

    @staticmethod
    def _connection_values(fields: dict) -> dict:
        bad = set(fields) - set(Store.CONNECTION_FIELDS)
        if bad:
            raise ValueError(f"unknown connection field(s) {sorted(bad)}; allowed: {Store.CONNECTION_FIELDS}")
        out = {}
        if "status" in fields:
            if fields["status"] not in CONNECTION_STATUSES:
                raise ValueError(f"status must be one of {CONNECTION_STATUSES}")
            out["status"] = fields["status"]
        if "config" in fields:
            cfg = fields["config"] if fields["config"] is not None else {}
            if not isinstance(cfg, dict):
                raise ValueError("config must be a dict of non-secret settings")
            text = _dumps(cfg)
            if len(text) > MAX_CONFIG_JSON:
                raise ValueError("config is too large")
            out["config_json"] = text
        if "secret_blob" in fields:
            out["secret_blob"] = _check_blob(fields["secret_blob"], "secret_blob")
        for k, n in (("external_account", 300), ("last_error", 1000)):
            if k in fields:
                out[k] = None if fields[k] is None else str(fields[k])[:n]
        return out

    @staticmethod
    def _connection(row, with_secret: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["config"] = _loads(d.pop("config_json", None), {})
        d["has_secret"] = bool(d.get("secret_blob"))
        if not with_secret:
            d.pop("secret_blob", None)
        return d

    def get_connection(self, tenant_id: str, provider: str, with_secret: bool = False) -> dict | None:
        """This tenant's connection to `provider`, or None. The encrypted secret_blob is included only with
        with_secret=True (has_secret says whether one is stored)."""
        with self.connect() as con:
            row = con.execute("select * from integration_connections where tenant_id = ? and provider = ?",
                              (tenant_id, provider)).fetchone()
        return self._connection(row, with_secret)

    def list_connections(self, tenant_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("select * from integration_connections where tenant_id = ? order by provider, id",
                               (tenant_id,)).fetchall()
        return [self._connection(r) for r in rows]

    def save_connection(self, tenant_id: str, provider: str, **fields) -> dict:
        """Insert or update (unique per tenant + provider) in one statement. `fields`: status, config (dict),
        secret_blob (encrypted), external_account, last_error. Columns not given keep their value (or default)."""
        _check_provider(provider)
        values = self._connection_values(fields)
        now = now_iso()
        cols = ["tenant_id", "provider", *values, "created", "updated"]
        sets = ", ".join([f"{c} = excluded.{c}" for c in values] + ["updated = excluded.updated"])
        with self.connect() as con:
            con.execute(f"insert into integration_connections ({', '.join(cols)}) values "
                        f"({', '.join('?' * len(cols))}) on conflict (tenant_id, provider) do update set {sets}",
                        [tenant_id, provider, *values.values(), now, now])
            row = con.execute("select * from integration_connections where tenant_id = ? and provider = ?",
                              (tenant_id, provider)).fetchone()
        return self._connection(row)

    def update_connection(self, tenant_id: str, provider: str, *, expect_secret_blob=_ANY, **fields) -> dict | None:
        """Update an existing connection. With expect_secret_blob, only if the stored blob is still exactly that
        value (compare-and-set, e.g. two workers refreshing one OAuth token: the loser gets None and re-reads).
        Returns the updated row, or None (no such connection, or the compare failed)."""
        values = self._connection_values(fields)
        values["updated"] = now_iso()
        sql = (f"update integration_connections set {', '.join(f'{c} = ?' for c in values)} "
               "where tenant_id = ? and provider = ?")
        params = [*values.values(), tenant_id, provider]
        if expect_secret_blob is not _ANY:
            if expect_secret_blob is None:
                sql += " and secret_blob is null"
            else:
                sql += " and secret_blob = ?"
                params.append(expect_secret_blob)
        with self.connect() as con:
            if con.execute(sql, params).rowcount == 0:
                return None
            row = con.execute("select * from integration_connections where tenant_id = ? and provider = ?",
                              (tenant_id, provider)).fetchone()
        return self._connection(row)

    def delete_connection(self, tenant_id: str, provider: str) -> bool:
        with self.connect() as con:
            return con.execute("delete from integration_connections where tenant_id = ? and provider = ?",
                               (tenant_id, provider)).rowcount > 0

    def connections_for_provider(self, provider: str, status: str = "connected") -> list[dict]:
        """OPERATOR ONLY: every tenant's connection to `provider` with this status (scheduled syncs). No secrets."""
        with self.connect() as con:
            rows = con.execute("select * from integration_connections where provider = ? and status = ? "
                               "order by tenant_id", (provider, status)).fetchall()
        return [self._connection(r) for r in rows]

    # ------------------------------------------------------------------ webhooks
    WEBHOOK_FIELDS = ("name", "url", "kind", "events", "signing_secret_blob", "enabled", "last_status", "last_sent")

    @staticmethod
    def _webhook_values(fields: dict) -> dict:
        bad = set(fields) - set(Store.WEBHOOK_FIELDS)
        if bad:
            raise ValueError(f"unknown webhook field(s) {sorted(bad)}; allowed: {Store.WEBHOOK_FIELDS}")
        out = {}
        if "name" in fields:
            name = str(fields["name"] or "").strip()
            if not 1 <= len(name) <= 100:
                raise ValueError("webhook name must be 1-100 characters")
            out["name"] = name
        if "url" in fields:      # the URL is a credential (Slack and Teams URLs carry their secret): encrypted
            if fields["url"] is None:
                raise ValueError("url is required")
            out["url"] = _check_blob(fields["url"], "url")
        if "kind" in fields:
            if fields["kind"] not in WEBHOOK_KINDS:
                raise ValueError(f"kind must be one of {WEBHOOK_KINDS}")
            out["kind"] = fields["kind"]
        if "events" in fields:
            events = fields["events"]
            if not isinstance(events, (list, tuple)) or len(events) > 50 or not all(
                    isinstance(e, str) and 1 <= len(e) <= 60 for e in events):
                raise ValueError("events must be a list of event names")
            out["events_json"] = _dumps(sorted(set(events)))
        if "signing_secret_blob" in fields:
            out["signing_secret_blob"] = _check_blob(fields["signing_secret_blob"], "signing_secret_blob")
        if "enabled" in fields:
            out["enabled"] = 1 if fields["enabled"] else 0
        if "last_status" in fields:
            out["last_status"] = None if fields["last_status"] is None else str(fields["last_status"])[:300]
        if "last_sent" in fields:
            out["last_sent"] = None if fields["last_sent"] is None else to_store_time(fields["last_sent"])
        return out

    @staticmethod
    def _webhook(row, with_secret: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["events"] = _loads(d.pop("events_json", None), [])
        d["enabled"] = bool(d["enabled"])
        d["has_signing_secret"] = bool(d.get("signing_secret_blob"))
        if not with_secret:
            d.pop("signing_secret_blob", None)
        return d

    def create_webhook(self, tenant_id: str, *, name: str, url: str, kind: str, events: list[str],
                       signing_secret_blob: str | None = None, enabled: bool = True) -> dict:
        """`url` and `signing_secret_blob` are SecretBox blobs (the store refuses plaintext)."""
        values = self._webhook_values({"name": name, "url": url, "kind": kind, "events": events,
                                       "signing_secret_blob": signing_secret_blob, "enabled": enabled})
        values["tenant_id"], values["created"] = tenant_id, now_iso()
        cols = list(values)
        with self.connect() as con:
            wid = self._insert_id(con, f"insert into webhooks ({', '.join(cols)}) "
                                       f"values ({', '.join('?' * len(cols))})", [values[c] for c in cols])
            row = con.execute("select * from webhooks where tenant_id = ? and id = ?", (tenant_id, wid)).fetchone()
        return self._webhook(row)

    def list_webhooks(self, tenant_id: str, enabled_only: bool = False) -> list[dict]:
        sql = "select * from webhooks where tenant_id = ?" + (" and enabled = 1" if enabled_only else "")
        with self.connect() as con:
            rows = con.execute(sql + " order by id", (tenant_id,)).fetchall()
        return [self._webhook(r) for r in rows]

    def get_webhook(self, tenant_id: str, webhook_id, with_secret: bool = False) -> dict | None:
        if not _id_ok(webhook_id):
            return None
        with self.connect() as con:
            row = con.execute("select * from webhooks where tenant_id = ? and id = ?",
                              (tenant_id, webhook_id)).fetchone()
        return self._webhook(row, with_secret)

    def update_webhook(self, tenant_id: str, webhook_id, **fields) -> dict | None:
        values = self._webhook_values(fields)
        if not _id_ok(webhook_id):
            return None
        if values:
            with self.connect() as con:
                con.execute(f"update webhooks set {', '.join(f'{c} = ?' for c in values)} "
                            "where tenant_id = ? and id = ?", [*values.values(), tenant_id, webhook_id])
        return self.get_webhook(tenant_id, webhook_id)

    def delete_webhook(self, tenant_id: str, webhook_id) -> bool:
        if not _id_ok(webhook_id):
            return False
        with self.connect() as con:
            return con.execute("delete from webhooks where tenant_id = ? and id = ?",
                               (tenant_id, webhook_id)).rowcount > 0

    def webhooks_for_event(self, tenant_id: str, event: str) -> list[dict]:
        """This tenant's enabled webhooks subscribed to `event` (or to every event, "*")."""
        return [w for w in self.list_webhooks(tenant_id, enabled_only=True)
                if event in w["events"] or "*" in w["events"]]

    # ------------------------------------------------------------------ outbound_events (the delivery queue)
    @staticmethod
    def _outbound(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["payload"] = _loads(d.pop("payload_json", None), {})
        return d

    def enqueue_outbound(self, tenant_id: str, event: str, payload: dict, targets, *, dedupe_key: str | None = None,
                         delay_s: float = 0) -> list[int]:
        """One pending row per target ("webhook:<id>", "connector:<provider>", "handler:<name>") in one transaction.
        With dedupe_key, a target that already has a row with that key for this tenant is skipped (the unique index
        decides, so two emitters racing can't both add it). Returns the new row ids."""
        if not isinstance(event, str) or not 1 <= len(event) <= 60:
            raise ValueError("bad event name")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        text = _dumps(payload)
        if len(text) > MAX_PAYLOAD_JSON:
            raise ValueError(f"payload is too large ({len(text)} bytes; max {MAX_PAYLOAD_JSON})")
        targets = list(dict.fromkeys(targets))
        for t in targets:
            if not isinstance(t, str) or not TARGET_RE.match(t):
                raise ValueError(f"bad target {str(t)[:90]!r}")
        if dedupe_key is not None:
            dedupe_key = str(dedupe_key)[:200]
        now, due = now_iso(), iso_in(delay_s)
        sql = ("insert into outbound_events (tenant_id, event, payload_json, target, status, attempts, next_attempt, "
               "created, dedupe_key) values (?,?,?,?,'pending',0,?,?,?) on conflict do nothing")
        ids = []
        with self.connect() as con:
            for t in targets:
                params = (tenant_id, event, text, t, due, now, dedupe_key)
                if self.backend == "postgres":
                    row = con.execute(sql + " returning id", params).fetchone()
                    if row:
                        ids.append(row[0])
                else:
                    cur = con.execute(sql, params)
                    if cur.rowcount:
                        ids.append(cur.lastrowid)
        return ids

    def claim_outbound(self, limit: int = 20, lease_s: float = 300, now: datetime | None = None) -> list[dict]:
        """OPERATOR ONLY (the delivery worker): lock up to `limit` due rows (pending and due, or `sending` whose lease
        ran out because a worker died), mark them `sending` with attempts + 1 and next_attempt = the lease end, and
        return them. Two workers (or two instances) never get the same row: Postgres skips rows another worker has
        locked; SQLite takes the write lock first."""
        at = to_store_time(now or datetime.now(timezone.utc))
        lease_until = iso_in(lease_s, now)
        sql = ("select * from outbound_events where status in ('pending', 'sending') and next_attempt <= ? "
               "order by next_attempt, id limit ?")
        with self.connect() as con:
            if self.backend == "sqlite":
                con.execute("begin immediate")
                rows = con.execute(sql, (at, _limit(limit, 500))).fetchall()
            else:
                rows = con.execute(sql + " for update skip locked", (at, _limit(limit, 500))).fetchall()
            out = []
            for r in rows:
                con.execute("update outbound_events set status = 'sending', attempts = attempts + 1, next_attempt = ? "
                            "where tenant_id = ? and id = ?", (lease_until, r["tenant_id"], r["id"]))
                d = self._outbound(r)
                d.update(status="sending", attempts=d["attempts"] + 1, next_attempt=lease_until)
                out.append(d)
        return out

    def finish_outbound(self, tenant_id: str, outbound_id, status: str, *, next_attempt=None,
                        last_error: str | None = None) -> bool:
        """Record a delivery result: delivered / failed / skipped, or pending again with next_attempt (a retry)."""
        if status not in ("pending", *OUTBOUND_DONE):
            raise ValueError(f"status must be pending or one of {OUTBOUND_DONE}")
        if status == "pending" and next_attempt is None:
            raise ValueError("a retry needs next_attempt")
        if not _id_ok(outbound_id):
            return False
        nxt = None if next_attempt is None else to_store_time(next_attempt)
        with self.connect() as con:
            return con.execute("update outbound_events set status = ?, next_attempt = ?, last_error = ? "
                               "where tenant_id = ? and id = ?",
                               (status, nxt, None if last_error is None else str(last_error)[:1000], tenant_id,
                                outbound_id)).rowcount > 0

    def get_outbound(self, tenant_id: str, outbound_id) -> dict | None:
        if not _id_ok(outbound_id):
            return None
        with self.connect() as con:
            return self._outbound(con.execute("select * from outbound_events where tenant_id = ? and id = ?",
                                              (tenant_id, outbound_id)).fetchone())

    def list_outbound(self, tenant_id: str, status: str | None = None, limit: int = 100,
                      before_id: int | None = None) -> list[dict]:
        """Newest first. Page with before_id = the last id of the previous page."""
        sql, params = "select * from outbound_events where tenant_id = ?", [tenant_id]
        if status is not None:
            if status not in OUTBOUND_STATUSES:
                raise ValueError(f"status must be one of {OUTBOUND_STATUSES}")
            sql += " and status = ?"
            params.append(status)
        if before_id is not None:
            if not _id_ok(before_id):
                return []
            sql += " and id < ?"
            params.append(int(before_id))
        params.append(_limit(limit))
        with self.connect() as con:
            return [self._outbound(r) for r in con.execute(sql + " order by id desc limit ?", params).fetchall()]

    def purge_outbound(self, older_than) -> int:
        """OPERATOR ONLY: delete finished rows (delivered, failed, skipped) created before `older_than`."""
        with self.connect() as con:
            return con.execute("delete from outbound_events where status in ('delivered', 'failed', 'skipped') "
                               "and created < ?", (to_store_time(older_than),)).rowcount

    # ------------------------------------------------------------------ mcp_audit_log
    _MCP_INSERT = ("insert into mcp_audit_log (tenant_id, api_key_id, oauth_grant_id, tool, args_sha256, status, "
                   "duration_ms, ip, created) values (?,?,?,?,?,?,?,?,?)")

    def log_mcp_call(self, tenant_id: str | None, tool: str, status: str, *, api_key_id: int | None = None,
                     oauth_grant_id: int | None = None, args_sha256: str | None = None, duration_ms: int | None = None,
                     ip: str | None = None) -> int:
        """One row per MCP tool call. Arguments are never stored, only their SHA-256 (hex). tenant_id None = an
        anonymous call; api_key_id or oauth_grant_id (an app connected with OAuth) says who called."""
        row = self._mcp_row({"tenant_id": tenant_id, "api_key_id": api_key_id, "oauth_grant_id": oauth_grant_id,
                             "tool": tool, "status": status, "args_sha256": args_sha256, "duration_ms": duration_ms,
                             "ip": ip})
        with self.connect() as con:
            return self._insert_id(con, self._MCP_INSERT, (*row, now_iso()))

    @staticmethod
    def _mcp_row(entry: dict) -> tuple:
        status = entry.get("status")
        if status not in MCP_AUDIT_STATUSES:
            raise ValueError(f"status must be one of {MCP_AUDIT_STATUSES}")
        sha = entry.get("args_sha256")
        if sha is not None and not SHA256_RE.match(str(sha)):
            raise ValueError("args_sha256 must be a lowercase hex SHA-256")
        dur = entry.get("duration_ms")
        dur = None if dur is None else max(0, min(int(dur), 2 ** 31 - 1))
        ip = entry.get("ip")
        return (entry.get("tenant_id"), entry.get("api_key_id"), entry.get("oauth_grant_id"),
                str(entry.get("tool") or "?")[:200], sha, status, dur, None if ip is None else str(ip)[:64])

    def log_mcp_calls(self, entries: list[dict], activity: list[dict] = ()) -> int:
        """Several MCP call rows (the fields of log_mcp_call, plus tenant_id) and their activity-log rows (the
        arguments of audit(): tenant_id, user, tool, args, verified, ok, source) in ONE transaction: the /mcp gate
        writes a request's rows with a single database round trip, after the response has been sent."""
        rows = [self._mcp_row(e) for e in entries]
        created = now_iso()
        tenants = list(dict.fromkeys([r[0] for r in rows] + [a.get("tenant_id") for a in activity]))
        with self.connect() as con:
            # One request is one company (or anonymous). A deleted company gets no rows: mcp_audit_log's foreign
            # key would refuse them anyway, the activity log has none (see _tenant_alive).
            if self.backend == "sqlite" and any(t is not None for t in tenants):
                con.execute("begin immediate")
            gone = {t for t in tenants if t is not None and con.execute(
                "select 1 from tenants where id = ?" + ("" if self.backend == "sqlite" else " for key share"),
                (t,)).fetchone() is None}
            rows = [r for r in rows if r[0] not in gone]
            activity = [a for a in activity if a.get("tenant_id") not in gone]
            for r in rows:
                con.execute(self._MCP_INSERT, (*r, created))
            for a in activity:
                v = a.get("verified")
                con.execute('insert into audit (tenant_id, "user", tool, args_json, verified, ok, source, created) '
                            "values (?,?,?,?,?,?,?,?)",
                            (a.get("tenant_id"), a.get("user"), str(a.get("tool") or "?")[:200],
                             json.dumps(a.get("args") or {}, separators=(",", ":"), default=str)[:20000],
                             None if v is None else int(bool(v)), int(bool(a.get("ok", True))),
                             a.get("source") or "mcp", created))
        return len(rows)

    def trim_anonymous_mcp_audit(self, keep: int) -> int:
        """Anonymous (public demo) MCP call rows belong to no company: keep only the newest `keep`."""
        with self.connect() as con:
            row = con.execute("select id from mcp_audit_log where tenant_id is null order by id desc limit 1 "
                              "offset ?", (int(keep),)).fetchone()
            if not row:
                return 0
            return con.execute("delete from mcp_audit_log where tenant_id is null and id <= ?", (row[0],)).rowcount

    def mcp_audit_rows(self, tenant_id: str, *, start=None, end=None, api_key_id=None, status=None, tool=None,
                       after_id: int = 0, limit: int = 500) -> list[dict]:
        """This tenant's MCP call rows, OLDEST first, with id > after_id (for exports that page forward), created in
        [start, end), optionally one API key, status or tool."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        where, params = ["tenant_id = ?", "id > ?"], [tenant_id, int(after_id)]
        for col, val in (("api_key_id", api_key_id), ("status", status), ("tool", tool)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if start is not None:
            where.append("created >= ?")
            params.append(to_store_time(start))
        if end is not None:
            where.append("created < ?")
            params.append(to_store_time(end))
        with self.connect() as con:
            return [dict(r) for r in con.execute(f"select * from mcp_audit_log where {' and '.join(where)} "
                                                 "order by id limit ?", (*params, _limit(limit, 5000))).fetchall()]

    def _audit_page(self, where: list[str], params: list, start, end, limit, cursor) -> dict:
        if start is not None:
            where.append("created >= ?")
            params.append(to_store_time(start))
        if end is not None:
            where.append("created < ?")
            params.append(to_store_time(end))
        if cursor:
            try:
                c_created, c_id = str(cursor).rsplit("|", 1)
                c_created, c_id = to_store_time(c_created), int(c_id)
            except ValueError:
                raise ValueError("bad cursor") from None
            where.append("(created < ? or (created = ? and id < ?))")
            params += [c_created, c_created, c_id]
        n = _limit(limit)
        sql = (f"select * from mcp_audit_log where {' and '.join(where) or '1 = 1'} "
               "order by created desc, id desc limit ?")
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, [*params, n + 1]).fetchall()]
        more = len(rows) > n
        rows = rows[:n]
        return {"items": rows, "next_cursor": f"{rows[-1]['created']}|{rows[-1]['id']}" if more else None}

    def list_mcp_audit(self, tenant_id: str, *, start=None, end=None, limit: int = 100,
                       cursor: str | None = None, api_key_id: int | None = None, status: str | None = None,
                       tool: str | None = None) -> dict:
        """This tenant's MCP calls, newest first, created in [start, end), optionally only one API key, status or
        tool. Returns {"items", "next_cursor"}; pass next_cursor back as `cursor` for the next page (None = no
        more)."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        where, params = ["tenant_id = ?"], [tenant_id]
        for col, val in (("api_key_id", api_key_id), ("status", status), ("tool", tool)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        return self._audit_page(where, params, start, end, limit, cursor)

    def admin_mcp_audit(self, *, start=None, end=None, tenant_id: str | None = None, anonymous: bool = False,
                        limit: int = 100, cursor: str | None = None) -> dict:
        """OPERATOR ONLY (platform admin, never a tenant route): MCP calls across every tenant, or one tenant, or only
        anonymous calls, created in [start, end), newest first, paged like list_mcp_audit."""
        where, params = [], []
        if anonymous:
            where.append("tenant_id is null")
        elif tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        return self._audit_page(where, params, start, end, limit, cursor)

    # ------------------------------------------------------------------ sso_configs
    @staticmethod
    def _sso(row, with_secret: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["allowed_domains"] = _loads(d.pop("allowed_domains_json", None), [])
        d["enforced"] = bool(d["enforced"])
        d["has_secret"] = bool(d.get("secret_blob"))
        if not with_secret:
            d.pop("secret_blob", None)
        return d

    def get_sso_config(self, tenant_id: str, with_secret: bool = False) -> dict | None:
        with self.connect() as con:
            return self._sso(con.execute("select * from sso_configs where tenant_id = ?", (tenant_id,)).fetchone(),
                             with_secret)

    def save_sso_config(self, tenant_id: str, *, issuer: str, client_id: str, secret_blob=_KEEP,
                        allowed_domains=(), enforced: bool = False) -> dict:
        """Insert or replace this tenant's SSO settings. secret_blob (encrypted) is left as it is unless given."""
        issuer, client_id = str(issuer or "").strip(), str(client_id or "").strip()
        if not issuer or len(issuer) > 500 or not client_id or len(client_id) > 300:
            raise ValueError("issuer (max 500) and client_id (max 300) are required")
        domains = sorted({str(d).strip().lower() for d in allowed_domains or ()})
        if len(domains) > 50 or not all(DOMAIN_RE.match(d) for d in domains):
            raise ValueError("allowed_domains must be up to 50 domain names like example.com")
        values = {"issuer": issuer, "client_id": client_id, "allowed_domains_json": _dumps(domains),
                  "enforced": 1 if enforced else 0}
        if secret_blob is not _KEEP:
            values["secret_blob"] = _check_blob(secret_blob, "secret_blob")
        cols = ["tenant_id", *values, "created"]
        sets = ", ".join(f"{c} = excluded.{c}" for c in values)
        with self.connect() as con:
            con.execute(f"insert into sso_configs ({', '.join(cols)}) values ({', '.join('?' * len(cols))}) "
                        f"on conflict (tenant_id) do update set {sets}", [tenant_id, *values.values(), now_iso()])
        return self.get_sso_config(tenant_id)

    def delete_sso_config(self, tenant_id: str) -> bool:
        with self.connect() as con:
            return con.execute("delete from sso_configs where tenant_id = ?", (tenant_id,)).rowcount > 0

    def sso_configs_for_domain(self, domain: str) -> list[dict]:
        """OPERATOR ONLY (the login page): SSO settings of every tenant that lists this email domain. No secrets."""
        domain = str(domain or "").strip().lower()
        if not DOMAIN_RE.match(domain):
            return []
        with self.connect() as con:      # a small table; matched in Python so both backends behave the same
            rows = con.execute("select * from sso_configs where allowed_domains_json like ?",
                               (f'%"{domain}"%',)).fetchall()
        return [s for s in (self._sso(r) for r in rows) if domain in s["allowed_domains"]]

    # ------------------------------------------------------------------ calendar_tokens
    @staticmethod
    def _cal(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["revoked"] = bool(d["revoked"])
        return d

    def add_calendar_token(self, tenant_id: str, token_hash: str) -> dict:
        """Store the SHA-256 (hex) of a calendar feed token; the token itself is shown once and never stored."""
        if not SHA256_RE.match(str(token_hash or "")):
            raise ValueError("token_hash must be a lowercase hex SHA-256")
        with self.connect() as con:
            con.execute("insert into calendar_tokens (tenant_id, token_hash, created, revoked) values (?,?,?,0)",
                        (tenant_id, token_hash, now_iso()))
            return self._cal(con.execute("select * from calendar_tokens where tenant_id = ? and token_hash = ?",
                                         (tenant_id, token_hash)).fetchone())

    def list_calendar_tokens(self, tenant_id: str, include_revoked: bool = False) -> list[dict]:
        sql = "select * from calendar_tokens where tenant_id = ?" + ("" if include_revoked else " and revoked = 0")
        with self.connect() as con:
            return [self._cal(r) for r in con.execute(sql + " order by created, token_hash", (tenant_id,)).fetchall()]

    def revoke_calendar_token(self, tenant_id: str, token_hash: str) -> bool:
        with self.connect() as con:
            return con.execute("update calendar_tokens set revoked = 1 where tenant_id = ? and token_hash = ? "
                               "and revoked = 0", (tenant_id, token_hash)).rowcount > 0

    def revoke_calendar_tokens(self, tenant_id: str) -> int:
        with self.connect() as con:
            return con.execute("update calendar_tokens set revoked = 1 where tenant_id = ? and revoked = 0",
                               (tenant_id,)).rowcount

    def calendar_token_tenant(self, token_hash: str) -> str | None:
        """The tenant a live (not revoked) calendar token belongs to, by the token's hash. The token is the
        credential, so this lookup is by hash alone, like api_key_by_hash."""
        if not SHA256_RE.match(str(token_hash or "")):
            return None
        with self.connect() as con:
            row = con.execute("select tenant_id from calendar_tokens where token_hash = ? and revoked = 0",
                              (token_hash,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ OAuth (app/oauth.py)
    # Apps that sign in with TradeComply (MCP clients such as Claude). Every token is random and stored only as its
    # SHA-256; expiries are unix seconds. A grant is one approval by one person; its one-time code, access tokens
    # and refresh tokens are rows of oauth_tokens. Ending a grant marks it revoked and deletes its tokens.
    OAUTH_CODE_KEEP_S = 3600           # a used one-time code is kept this long after it expires, to spot replays
    OAUTH_IDLE_CLIENT_S = 3600         # a registration with no grant and no pending request is removed after this
    OAUTH_UNUSED_CLIENT_S = 86400      # and one that never got a grant after this, pending requests or not
    OAUTH_ACTOR = "TradeComply (automatic)"     # who ended a connection in the activity log, when no person did

    def create_oauth_client(self, client_id: str, name: str, redirect_uris: list[str], auth_method: str,
                            secret_hash: str | None = None) -> dict:
        if auth_method not in ("none", "client_secret_post", "client_secret_basic"):
            raise ValueError("bad token_endpoint_auth_method")
        if (secret_hash is None) != (auth_method == "none") or (secret_hash and not SHA256_RE.match(secret_hash)):
            raise ValueError("a confidential client needs a secret hash; a public one has none")
        with self.connect() as con:
            con.execute("insert into oauth_clients (id, secret_hash, name, redirect_uris_json, auth_method, created) "
                        "values (?,?,?,?,?,?)", (client_id, secret_hash, name, _dumps(list(redirect_uris)),
                                                 auth_method, now_iso()))
        return self.get_oauth_client(client_id)

    def get_oauth_client(self, client_id: str) -> dict | None:
        """The registration, with secret_hash (the caller checks a presented secret; it never leaves the server)."""
        with self.connect() as con:
            row = con.execute("select * from oauth_clients where id = ?", (str(client_id)[:200],)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["redirect_uris"] = _loads(d.pop("redirect_uris_json"), [])
        return d

    def create_oauth_request(self, request_hash: str, client_id: str, *, redirect_uri: str, state: str | None,
                             code_challenge: str, scope: str, resource: str, ttl_s: int, keep: int) -> None:
        """A pending sign-in request. Only the newest `keep` of one app's pending requests stay (anyone can open
        /oauth/authorize, so without a cap one caller could fill the table)."""
        with self.connect() as con:
            con.execute("insert into oauth_requests (id, client_id, redirect_uri, state, code_challenge, scope, "
                        "resource, expires, created) values (?,?,?,?,?,?,?,?,?)",
                        (request_hash, client_id, redirect_uri, state, code_challenge, scope, resource,
                         int(time.time()) + int(ttl_s), now_iso()))
            con.execute("delete from oauth_requests where client_id = ? and id not in (select id from oauth_requests "
                        "where client_id = ? order by expires desc, id limit ?)", (client_id, client_id, int(keep)))

    def get_oauth_request(self, request_hash: str) -> dict | None:
        """A pending (unexpired) request with its app's name and auth method."""
        with self.connect() as con:
            row = con.execute("select r.*, c.name as client_name, c.redirect_uris_json from oauth_requests r "
                              "join oauth_clients c on c.id = r.client_id where r.id = ? and r.expires > ?",
                              (request_hash, int(time.time()))).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["redirect_uris"] = _loads(d.pop("redirect_uris_json"), [])
        return d

    def take_oauth_request(self, request_hash: str) -> dict | None:
        """Remove a pending request and return it (single use: a second caller gets None). For a denial."""
        with self.connect() as con:
            row = self._read_for_update(con, "select * from oauth_requests where id = ?", (request_hash,))
            if row is None:
                return None
            con.execute("delete from oauth_requests where id = ?", (request_hash,))
        return dict(row) if row["expires"] > int(time.time()) else None

    def approve_oauth_request(self, request_hash: str, user: dict, code_hash: str, code_ttl_s: int) -> dict | None:
        """In one transaction: take the pending request, create the grant for this person (their company, user id,
        email and session counter) and its one-time code. None if the request is gone or expired, or if `user`
        (read when the approval came in) is no longer that exact person: removed, or signed out everywhere by a
        password change, since. The person's row is read and held in this transaction, so a removal can't slip in
        between (SQLite would give the next invited person the same id)."""
        now = int(time.time())
        with self.connect() as con:
            row = self._read_for_update(con, "select r.*, c.name as client_name from oauth_requests r join "
                                             "oauth_clients c on c.id = r.client_id where r.id = ?", (request_hash,))
            if row is None:
                return None
            con.execute("delete from oauth_requests where id = ?", (request_hash,))
            req = dict(row)
            if req["expires"] <= now:
                return None
            held = con.execute("select id from users where id = ? and tenant_id = ? and email = ? and session_gen = ?"
                               + ("" if self.backend == "sqlite" else " for share"),
                               (user["id"], user["tenant_id"], user["email"], user["session_gen"])).fetchone()
            if held is None:
                return None
            gid = self._insert_id(con, "insert into oauth_grants (tenant_id, user_id, user_email, session_gen, "
                                       "client_id, client_name, redirect_uri, code_challenge, scope, resource, "
                                       "created, expires) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (user["tenant_id"], user["id"], user["email"], user["session_gen"],
                                   req["client_id"], req["client_name"], req["redirect_uri"], req["code_challenge"],
                                   req["scope"], req["resource"], now_iso(), now + int(code_ttl_s)))
            con.execute("insert into oauth_tokens (tenant_id, grant_id, kind, token_hash, expires, created) "
                        "values (?,?,'code',?,?,?)", (user["tenant_id"], gid, code_hash, now + int(code_ttl_s),
                                                      now_iso()))
        return {**req, "grant_id": gid}

    _OAUTH_TOKEN_SQL = ("select t.id as token_id, t.expires as token_expires, t.used_at, g.* from oauth_tokens t "
                        "join oauth_grants g on g.id = t.grant_id join users u on u.id = g.user_id "
                        "and u.tenant_id = g.tenant_id and u.session_gen = g.session_gen "
                        "where t.token_hash = ? and t.kind = ?")

    def _lock_oauth_token(self, con, token_hash: str, kind: str) -> dict | None:
        """First read of a redeem/rotate transaction: the token and its grant, locked (SQLite: the write lock)."""
        if self.backend == "sqlite":
            con.execute("begin immediate")
            row = con.execute(self._OAUTH_TOKEN_SQL, (token_hash, kind)).fetchone()
        else:
            row = con.execute(self._OAUTH_TOKEN_SQL + " for update of t, g", (token_hash, kind)).fetchone()
        return dict(row) if row else None

    def _end_oauth_grants(self, con, where: str, params, reason: str | None = None) -> int:
        """Revoke the grants matching `where` (fixed SQL from this class) and delete every token issued from them.
        With a reason, each connection ended gets an activity-log row (oauth.revoked) in the same transaction, so a
        connection never just vanishes from Settings."""
        ended = con.execute(f"select id, tenant_id, client_name from oauth_grants where {where} and revoked = 0",
                            params).fetchall() if reason else []
        con.execute(f"delete from oauth_tokens where grant_id in (select id from oauth_grants where {where})", params)
        n = con.execute(f"update oauth_grants set revoked = 1, revoked_at = ? where {where} and revoked = 0",
                        (now_iso(), *params)).rowcount
        for g in ended:
            self._audit_row(con, g["tenant_id"], self.OAUTH_ACTOR, "oauth.revoked",
                            {"connection_id": g["id"], "app": g["client_name"], "reason": reason}, None)
        return n

    def _issue_oauth_tokens(self, con, grant: dict, access_hash: str, refresh_hash: str, access_ttl_s: int,
                            refresh_ttl_s: int, now: int) -> None:
        for kind, token_hash, ttl in (("access", access_hash, access_ttl_s), ("refresh", refresh_hash, refresh_ttl_s)):
            con.execute("insert into oauth_tokens (tenant_id, grant_id, kind, token_hash, expires, created) "
                        "values (?,?,?,?,?,?)", (grant["tenant_id"], grant["id"], kind, token_hash, now + int(ttl),
                                                 now_iso()))
        con.execute("update oauth_grants set expires = ?, last_used = ? where id = ?",
                    (now + int(refresh_ttl_s), now_iso(), grant["id"]))

    def redeem_oauth_code(self, code_hash: str, check, access_hash: str, refresh_hash: str, access_ttl_s: int,
                          refresh_ttl_s: int) -> tuple[dict | None, str | None]:
        """Exchange a one-time code for an access and a refresh token, in one locked transaction. check(grant) is a
        pure function (client, PKCE verifier, redirect_uri, resource) returning an OAuth error code or None. A code
        presented a second time with otherwise valid parameters revokes the grant and everything issued from it
        (OAuth 2.1 section 4.1.3); a replay with wrong parameters changes nothing, so it can't be used to cut off
        the real client. Returns (grant, None), (None, error), or (grant, "invalid_grant") when the replay ended
        the grant."""
        now = int(time.time())
        with self.connect() as con:
            g = self._lock_oauth_token(con, code_hash, "code")
            if g is None:
                return None, "invalid_grant"
            error = check(g)
            if error:
                return None, error
            if g["used_at"] is not None:
                ended = self._end_oauth_grants(con, "id = ?", (g["id"],), "code_reused")
                return (g if ended else None), "invalid_grant"
            if g["revoked"] or g["token_expires"] <= now:
                return None, "invalid_grant"
            con.execute("update oauth_tokens set used_at = ? where id = ?", (now, g["token_id"]))
            self._issue_oauth_tokens(con, g, access_hash, refresh_hash, access_ttl_s, refresh_ttl_s, now)
        return g, None

    def rotate_oauth_refresh(self, refresh_hash: str, check, access_hash: str, new_refresh_hash: str,
                             access_ttl_s: int, refresh_ttl_s: int, grace_s: int) -> tuple[dict | None, str | None]:
        """Trade a refresh token for a new access token and a new refresh token (the old one is used up), and
        slide the grant's expiry. An old refresh token presented again revokes the whole grant (OAuth 2.1
        refresh token reuse detection), except within grace_s (a few seconds) of its use: a client that sent the
        same refresh twice at once gets invalid_grant without losing the connection it just renewed. (Longer would
        let a thief who rotated a stolen token first keep the connection when the real client then sends it.)
        Returns (grant, None), (None, error), or (grant, "invalid_grant") when the reuse ended the grant."""
        now = int(time.time())
        with self.connect() as con:
            g = self._lock_oauth_token(con, refresh_hash, "refresh")
            if g is None:
                return None, "invalid_grant"
            error = check(g)
            if error:
                return None, error
            if g["revoked"] or g["expires"] <= now or g["token_expires"] <= now:
                return None, "invalid_grant"
            if g["used_at"] is not None:
                if now - int(g["used_at"]) > grace_s and \
                        self._end_oauth_grants(con, "id = ?", (g["id"],), "refresh_token_reused"):
                    return g, "invalid_grant"
                return None, "invalid_grant"
            con.execute("update oauth_tokens set used_at = ? where id = ?", (now, g["token_id"]))
            self._issue_oauth_tokens(con, g, access_hash, new_refresh_hash, access_ttl_s, refresh_ttl_s, now)
        return g, None

    def oauth_access(self, token_hash: str) -> dict | None:
        """The live grant behind an access token (not expired, not revoked, its person still in the company with
        the same session counter), with that person's current email and role. Touches last_used at most once a
        minute."""
        if not SHA256_RE.match(str(token_hash or "")):
            return None
        now = int(time.time())
        with self.connect() as con:
            row = con.execute("select g.id, g.tenant_id, g.user_id, g.client_name, g.scope, g.resource, g.last_used, "
                              "u.email, u.role from oauth_tokens t join oauth_grants g on g.id = t.grant_id "
                              "join users u on u.id = g.user_id and u.tenant_id = g.tenant_id "
                              "and u.session_gen = g.session_gen where t.token_hash = ? and t.kind = 'access' "
                              "and t.expires > ? and g.revoked = 0 and g.expires > ?",
                              (token_hash, now, now)).fetchone()
            if row is None:
                return None
            d = dict(row)
            if not d["last_used"] or d["last_used"] < iso_in(-self.LAST_USED_EVERY_S):
                con.execute("update oauth_grants set last_used = ? where id = ?", (now_iso(), d["id"]))
        return d

    def revoke_oauth_token(self, token_hash: str, client_id: str) -> bool:
        """RFC 7009 for one client's own token: a refresh token ends its grant, an access token only itself."""
        with self.connect() as con:
            row = con.execute("select t.id, t.kind, t.grant_id from oauth_tokens t join oauth_grants g "
                              "on g.id = t.grant_id where t.token_hash = ? and g.client_id = ? "
                              "and t.kind in ('access', 'refresh')", (token_hash, client_id)).fetchone()
            if row is None:
                return False
            if row["kind"] == "refresh":
                self._end_oauth_grants(con, "id = ?", (row["grant_id"],), "revoked_by_app")
            else:
                con.execute("delete from oauth_tokens where id = ?", (row["id"],))
        return True

    def oauth_token_client(self, token_hash: str) -> str | None:
        """The client id an access or refresh token was issued to (a public client may leave its client_id out
        of a refresh or revocation request: the token says which client it is)."""
        if not SHA256_RE.match(str(token_hash or "")):
            return None
        with self.connect() as con:
            row = con.execute("select g.client_id from oauth_tokens t join oauth_grants g on g.id = t.grant_id "
                              "where t.token_hash = ? and t.kind in ('access', 'refresh')", (token_hash,)).fetchone()
        return row[0] if row else None

    def list_oauth_grants(self, tenant_id: str, user_id: int | None = None) -> list[dict]:
        """Live connections (not revoked, not expired), newest first; one person's, or the whole company's."""
        sql = ("select id, user_id, user_email, client_id, client_name, redirect_uri, scope, created, last_used "
               "from oauth_grants where tenant_id = ? and revoked = 0 and expires > ?")
        params: list = [tenant_id, int(time.time())]
        if user_id is not None:
            sql += " and user_id = ?"
            params.append(user_id)
        with self.connect() as con:
            return [dict(r) for r in con.execute(sql + " order by id desc limit 500", params).fetchall()]

    def oauth_grant_names(self, tenant_id: str) -> dict[int, dict]:
        """grant id -> app name and who approved it, revoked grants included (to label the MCP call log)."""
        with self.connect() as con:
            return {r["id"]: {"client_name": r["client_name"], "user_email": r["user_email"]}
                    for r in con.execute("select id, client_name, user_email from oauth_grants where tenant_id = ?",
                                         (tenant_id,)).fetchall()}

    def revoke_oauth_grant(self, tenant_id: str, grant_id, user_id: int | None = None) -> bool:
        """End one live connection of this company (and, given user_id, only if it is that person's)."""
        if not _id_ok(grant_id) or (user_id is not None and not _id_ok(user_id)):
            return False
        where, params = "tenant_id = ? and id = ? and expires > ?", [tenant_id, grant_id, int(time.time())]
        if user_id is not None:
            where += " and user_id = ?"
            params.append(user_id)
        with self.connect() as con:
            return self._end_oauth_grants(con, where, params) > 0

    def revoke_user_oauth_grants(self, tenant_id: str, user_id: int, reason: str) -> int:
        """A password change (or SSO becoming required) ends every app connection that person approved."""
        if not _id_ok(user_id):
            return 0
        with self.connect() as con:
            return self._end_oauth_grants(con, "tenant_id = ? and user_id = ?", (tenant_id, user_id), reason)

    def purge_oauth(self) -> None:
        """Delete expired sign-in requests and tokens, and app registrations that never got a grant: after an hour
        with no pending request (Claude registers anew for every connection, and signs in within minutes), and
        after a day in any case (pending requests are deleted with them)."""
        now = int(time.time())
        with self.connect() as con:
            con.execute("delete from oauth_requests where expires < ?", (now,))
            con.execute("delete from oauth_tokens where expires < ? and (kind <> 'code' or expires < ?)",
                        (now, now - self.OAUTH_CODE_KEEP_S))
        try:
            with self.connect() as con:
                con.execute("delete from oauth_clients where not exists (select 1 from oauth_grants g where "
                            "g.client_id = oauth_clients.id) and (created < ? or (created < ? and not exists "
                            "(select 1 from oauth_requests r where r.client_id = oauth_clients.id)))",
                            (iso_in(-self.OAUTH_UNUSED_CLIENT_S), iso_in(-self.OAUTH_IDLE_CLIENT_S)))
        except IntegrityError:          # a grant for one of them was created at this moment: next time
            pass

    # ------------------------------------------------------------------ Ask TradeComply (app/assistant.py)
    # Every chat query filters by company AND person: a chat is visible only to the person who started it.
    def create_assistant_conversation(self, tenant_id: str, user_id: int, max_count: int) -> dict | None:
        """A new, empty chat for this person; None when they already have max_count chats. A chat of theirs that has
        no answer yet (its first question was stopped, failed or never sent) is reused instead of making another:
        the app lists only chats with answers, so empty ones must not pile up against the limit unseen."""
        now = now_iso()
        with self.connect() as con:
            self._read_for_update(con, "select id from tenants where id = ?", (tenant_id,))
            empty = con.execute("select id, created from assistant_conversations c where tenant_id = ? and "
                                "user_id = ? and not exists (select 1 from assistant_turns t where "
                                "t.conversation_id = c.id) order by id desc limit 1", (tenant_id, user_id)).fetchone()
            if empty is not None:
                con.execute("update assistant_conversations set updated = ? where tenant_id = ? and id = ?",
                            (now, tenant_id, empty["id"]))
                return {"id": empty["id"], "title": "New chat", "created": empty["created"], "updated": now,
                        "turns": 0}
            have = con.execute("select count(*) from assistant_conversations where tenant_id = ? and user_id = ?",
                               (tenant_id, user_id)).fetchone()[0]
            if have >= max_count:
                return None
            new_id = self._insert_id(con, "insert into assistant_conversations (tenant_id, user_id, title, created, "
                                          "updated) values (?,?,?,?,?)", (tenant_id, user_id, "New chat", now, now))
        return {"id": new_id, "title": "New chat", "created": now, "updated": now, "turns": 0}

    def list_assistant_conversations(self, tenant_id: str, user_id: int, limit: int = 200,
                                     since: str | None = None) -> list[dict]:
        """The person's chats, newest first; `since`: only chats used since then (retention not yet purged)."""
        with self.connect() as con:
            rows = con.execute("select c.id, c.title, c.created, c.updated, (select count(*) from assistant_turns t "
                               "where t.conversation_id = c.id) as turns from assistant_conversations c "
                               "where c.tenant_id = ? and c.user_id = ? and c.updated >= ? "
                               "order by c.updated desc, c.id desc limit ?",
                               (tenant_id, user_id, since or "", _limit(limit))).fetchall()
        return [dict(r) for r in rows]

    def assistant_conversation(self, tenant_id: str, user_id: int, conversation_id,
                               since: str | None = None) -> dict | None:
        """The chat and its turns, oldest first (messages_json as stored text), if it is this person's (and, with
        `since`, used since then)."""
        if not _id_ok(conversation_id):
            return None
        with self.connect() as con:
            row = con.execute("select id, title, created, updated from assistant_conversations where tenant_id = ? "
                              "and user_id = ? and id = ? and updated >= ?",
                              (tenant_id, user_id, conversation_id, since or "")).fetchone()
            if row is None:
                return None
            turns = con.execute("select id, messages_json, prompt, reply, checked_json, outcome, context_tokens, "
                                "created from assistant_turns where tenant_id = ? and conversation_id = ? order by id",
                                (tenant_id, conversation_id)).fetchall()
        out = dict(row)
        out["turns"] = [{**dict(t), "checked": _loads(t["checked_json"], [])} for t in turns]
        return out

    def append_assistant_turn(self, tenant_id: str, user_id: int, conversation_id, *, messages: list, prompt: str,
                              reply: str, checked: list, outcome: str, context_tokens: int = 0,
                              title: str | None = None) -> dict | None:
        """Add one turn to the person's chat and touch the chat (its title too, while it is "New chat"), in one
        transaction. None if the chat is gone or is not theirs."""
        if outcome not in ASSISTANT_OUTCOMES:
            raise ValueError(f"bad outcome {outcome!r}")
        if not _id_ok(conversation_id):
            return None
        now = now_iso()
        with self.connect() as con:
            row = self._read_for_update(con, "select title from assistant_conversations where tenant_id = ? and "
                                             "user_id = ? and id = ?", (tenant_id, user_id, conversation_id))
            if row is None:
                return None
            turn = {"messages_json": json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
                    "prompt": prompt, "reply": reply, "checked_json": _dumps(checked), "outcome": outcome,
                    "context_tokens": int(context_tokens or 0), "created": now}
            turn_id = self._insert_id(con, "insert into assistant_turns (tenant_id, conversation_id, messages_json, "
                                           "prompt, reply, checked_json, outcome, context_tokens, created) values "
                                           "(?,?,?,?,?,?,?,?,?)", (tenant_id, conversation_id, *turn.values()))
            new_title = title if title and row["title"] == "New chat" else row["title"]
            con.execute("update assistant_conversations set updated = ?, title = ? where tenant_id = ? and id = ?",
                        (now, new_title, tenant_id, conversation_id))
        return {"id": turn_id, **turn, "checked": checked, "title": new_title}

    def delete_assistant_conversation(self, tenant_id: str, user_id: int, conversation_id) -> bool:
        if not _id_ok(conversation_id):
            return False
        with self.connect() as con:
            row = self._read_for_update(con, "select id from assistant_conversations where tenant_id = ? and "
                                             "user_id = ? and id = ?", (tenant_id, user_id, conversation_id))
            if row is None:
                return False
            con.execute("delete from assistant_turns where tenant_id = ? and conversation_id = ?",
                        (tenant_id, conversation_id))
            con.execute("delete from assistant_conversations where tenant_id = ? and id = ?",
                        (tenant_id, conversation_id))
            return True

    def start_assistant_usage(self, tenant_id: str, user_id: int, model: str) -> int | None:
        """The usage row of one message, written before its first call to Anthropic: the message counts toward the
        daily limit even if it is then stopped, times out or fails. Tokens are added as each call reports them
        (add_assistant_usage). None for a company deleted meanwhile."""
        with self.connect() as con:
            if not self._tenant_alive(con, tenant_id):
                return None
            return self._insert_id(con, "insert into assistant_usage (tenant_id, user_id, model, created) values "
                                        "(?, (select id from users where tenant_id = ? and id = ?), ?, ?)",
                                   (tenant_id, tenant_id, user_id, str(model)[:100], now_iso()))

    def add_assistant_usage(self, tenant_id: str, usage_id: int, *, input_tokens: int = 0, output_tokens: int = 0,
                            cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        with self.connect() as con:
            con.execute("update assistant_usage set input_tokens = input_tokens + ?, output_tokens = output_tokens + "
                        "?, cache_read_tokens = cache_read_tokens + ?, cache_write_tokens = cache_write_tokens + ? "
                        "where tenant_id = ? and id = ?",
                        (int(input_tokens), int(output_tokens), int(cache_read_tokens), int(cache_write_tokens),
                         tenant_id, usage_id))

    def assistant_usage_since(self, tenant_id: str, since: str, user_id: int | None = None) -> dict:
        """Sums since `since` (store time, UTC) for the company, or for one person in it: messages and tokens."""
        sql = ("select count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0), "
               "coalesce(sum(cache_read_tokens), 0), coalesce(sum(cache_write_tokens), 0) from assistant_usage "
               "where tenant_id = ? and created >= ?")
        params: list = [tenant_id, since]
        if user_id is not None:
            sql += " and user_id = ?"
            params.append(user_id)
        with self.connect() as con:
            row = con.execute(sql, params).fetchone()
        return dict(zip(("messages", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"),
                        (int(v) for v in row)))

    def assistant_usage_by_person(self, tenant_id: str, since: str) -> list[dict]:
        """Per person since `since`: user_id (None for people since removed), messages and token sums."""
        with self.connect() as con:
            rows = con.execute("select user_id, count(*) as messages, coalesce(sum(input_tokens), 0) as "
                               "input_tokens, coalesce(sum(output_tokens), 0) as output_tokens, "
                               "coalesce(sum(cache_read_tokens), 0) as cache_read_tokens, "
                               "coalesce(sum(cache_write_tokens), 0) as cache_write_tokens from assistant_usage "
                               "where tenant_id = ? and created >= ? group by user_id order by messages desc, user_id",
                               (tenant_id, since)).fetchall()
        return [{k: (v if k == "user_id" else int(v)) for k, v in dict(r).items()} for r in rows]    # PG sums: numeric

    def purge_assistant(self, chats_before: str, usage_before: str) -> int:
        """Retention: delete chats not used since `chats_before` (with their turns) and usage rows older than
        `usage_before`. Returns the number of chats deleted."""
        with self.connect() as con:
            con.execute("delete from assistant_turns where conversation_id in (select id from assistant_conversations "
                        "where updated < ?)", (chats_before,))
            gone = con.execute("delete from assistant_conversations where updated < ?", (chats_before,)).rowcount
            con.execute("delete from assistant_usage where created < ?", (usage_before,))
            return gone

    # ------------------------------------------------------------------ key rotation of stored secrets
    def rotate_secret_blobs(self, transform) -> int:
        """OPERATOR ONLY (key rotation, app/crypto.py): for every stored secret blob, transform(table, row, column,
        blob) returns a replacement blob or None (leave it). Each replacement is a compare-and-set, so a value
        changed meanwhile is left alone. Returns the number of blobs replaced."""
        keys = {"integration_connections": ("tenant_id", "id"), "webhooks": ("tenant_id", "id"),
                "sso_configs": ("tenant_id",)}
        changed = 0
        for table, columns in SECRET_BLOB_COLUMNS.items():
            with self.connect() as con:
                rows = [dict(r) for r in con.execute(f"select * from {table}").fetchall()]
            for row in rows:
                for col in columns:
                    old = row.get(col)
                    if not old:
                        continue
                    new = transform(table, row, col, old)
                    if not new or new == old:
                        continue
                    _check_blob(new, col)
                    where = " and ".join(f"{k} = ?" for k in keys[table])
                    with self.connect() as con:
                        changed += con.execute(f"update {table} set {col} = ? where {where} and {col} = ?",
                                               [new, *(row[k] for k in keys[table]), old]).rowcount
        return changed

    # ================================================================== account data rights (app/account.py)
    @staticmethod
    def _check_tenant_id(tenant_id) -> str:
        if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 100:
            raise ValueError("a company (tenant) id is required")
        return tenant_id

    @staticmethod
    def _export_row(table: str, row) -> dict:
        d = dict(row)
        for col, key in EXPORT_JSON.get(table, {}).items():
            if col in d:
                text = d.pop(col)
                try:
                    d[key] = json.loads(text) if isinstance(text, str) and text else None
                except ValueError:
                    d[key] = text           # not JSON after all: exported as the text it is
        for col in (*EXPORT_BOOL.get(table, ()), *EXPORT_FLAGS.get(table, {})):
            if d.get(col) is not None:
                d[col] = bool(d[col])
        return d

    def export_rows(self, table: str, tenant_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        """One page of a company's rows for its data export: only the columns in TENANT_EXPORT (never a password or
        key hash, a session counter or an encrypted secret), JSON text as JSON values and 0/1 flags as booleans.
        Tables with an id come oldest first with id > after_id; the others (one SSO row, a few calendar links)
        come whole. "tenants" is the company profile (one row)."""
        self._check_tenant_id(tenant_id)
        if table not in TENANT_EXPORT:
            raise ValueError(f"bad table {table!r}")
        cols = [f'"{c}"' for c in TENANT_EXPORT[table]]
        cols += [f'case when "{src}" is null then 0 else 1 end as "{flag}"'
                 for flag, src in EXPORT_FLAGS.get(table, {}).items()]
        sql = f"select {', '.join(cols)} from {table} where {'id' if table == 'tenants' else 'tenant_id'} = ?"
        params: list = [tenant_id]
        if table in EXPORT_PAGED:
            sql += " and id > ? order by id limit ?"
            params += [int(after_id), _limit(limit, 5000)]
        elif table == "calendar_tokens":
            sql += " order by created, token_hash limit 10000"
        with self.connect() as con:
            return [self._export_row(table, r) for r in con.execute(sql, params).fetchall()]

    def tenant_row_counts(self, tenant_id: str) -> dict[str, int]:
        """How many rows each company table holds for this company ("tenants": 1 while the company exists)."""
        self._check_tenant_id(tenant_id)
        with self.connect() as con:
            out = {t: con.execute(f"select count(*) from {t} where tenant_id = ?", (tenant_id,)).fetchone()[0]
                   for t in TENANT_DELETE_ORDER}
            out["tenants"] = con.execute("select count(*) from tenants where id = ?", (tenant_id,)).fetchone()[0]
        return out

    def delete_tenant(self, tenant_id: str) -> dict[str, int]:
        """Delete a company and every row that belongs to it, in ONE transaction, on both backends: each table in
        TENANT_DELETE_ORDER by tenant_id (child tables first, so nothing depends on ON DELETE CASCADE: the activity
        log has no foreign key at all), then the company row. The company row is locked first, so on PostgreSQL a
        write that references the company at the same moment waits and then fails its foreign key check instead of
        leaving a stray row. Rows left by an earlier, interrupted deletion (the company row already gone) are removed
        too. Returns the number of rows removed per table. Tables in SHARED_TABLES hold no company data."""
        self._check_tenant_id(tenant_id)
        with self.connect() as con:
            self._read_for_update(con, "select id from tenants where id = ?", (tenant_id,))
            removed = {t: con.execute(f"delete from {t} where tenant_id = ?", (tenant_id,)).rowcount
                       for t in TENANT_DELETE_ORDER}
            removed["tenants"] = con.execute("delete from tenants where id = ?", (tenant_id,)).rowcount
        return removed
