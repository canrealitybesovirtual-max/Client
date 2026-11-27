"""Ask TradeComply, the in-app AI helper (app/assistant.py), against the fake Anthropic API (tests/fake_anthropic.py):
the real `anthropic` SDK over a mock transport, so the request on the wire, the streamed parsing and the typed errors
are the SDK's own. No network, no real key. Every test runs on SQLite and, with TC_TEST_DATABASE_URL, on PostgreSQL."""
import json
import logging
import threading
import time

import anthropic
import anyio
import httpx2
import pytest
from fastapi.testclient import TestClient

from app import assistant
from app.store import iso_in
from tests.fake_anthropic import (FAKE_KEY, FakeAnthropic, ask_tool, error_response, message, reply_text,
                                  search_then_tool, text, thinking, tool_use)
from tests.support import backend, browser, join, make_app, oauth_connect, signup  # noqa: F401  (backend: autouse)

LIEN = {"claimant_role": "subcontractor", "last_furnishing_date": "2026-11-01"}


@pytest.fixture
def fake():
    return FakeAnthropic()


@pytest.fixture
def app(tmp_path, fake):
    a = make_app(tmp_path, app_url="https://testserver")
    a.state.platform.env = {"ANTHROPIC_API_KEY": FAKE_KEY}
    a.state.platform.assistant_client = fake.client
    return a


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        signup(c)
        yield c


def new_chat(c) -> int:
    r = c.post("/api/assistant/conversations", json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ask(c, chat: int, text_: str = "When is my lien deadline?", **body):
    return c.post(f"/api/assistant/conversations/{chat}/messages", json={"text": text_, **body})


def rows(app, sql: str, params=()) -> list:
    with app.state.platform.store.connect() as con:
        return [tuple(r) for r in con.execute(sql, params).fetchall()]


def usage_rows(app) -> list:
    return rows(app, "select user_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens from "
                     "assistant_usage order by id")


# ---------------------------------------------------------------------------- availability
def test_not_set_up_hides_the_helper_and_tells_only_owners_the_setting(app, owner):
    app.state.platform.env = {}
    me = owner.get("/api/me").json()["assistant"]
    assert me == {"available": False, "configured": False, "enabled": True, "reason": "not_configured",
                  "env_var": "ANTHROPIC_API_KEY"}
    assert owner.post("/api/assistant/conversations", json={}).status_code == 403
    with browser(owner) as field:
        join(field, owner, "crew@acme.test", "field")
        st = field.get("/api/assistant/status").json()
        assert st["available"] is False and st["reason"] == "not_configured" and "env_var" not in st
    usage = owner.get("/api/assistant/usage").json()
    assert usage["configured"] is False and usage["env_var"] == "ANTHROPIC_API_KEY"
    assert FAKE_KEY not in json.dumps(usage) + json.dumps(me)


def test_status_when_set_up(owner):
    st = owner.get("/api/assistant/status").json()
    assert st["available"] is True and st["reason"] is None and "env_var" not in st
    assert st["limits"] == {"daily_messages": 30, "max_chars": 2000, "messages_left_today": 30}
    assert owner.get("/api/me").json()["assistant"]["available"] is True


# ---------------------------------------------------------------------------- the request on the wire
def test_request_follows_the_claude_api_docs(app, owner, fake):
    fake.add(reply_text("Hi."))
    chat = new_chat(owner)
    r = ask(owner, chat, "Hello", page="tools")
    assert r.status_code == 200, r.text
    body, headers = fake.bodies[0], fake.headers[0]
    assert headers["authorization"] == f"Bearer {FAKE_KEY}" and headers["anthropic-version"] == "2026-10-21"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01,thinking-binding-controls-2026-08-01"
    assert set(body) == {"model", "max_tokens", "system", "cache_control", "tools", "thinking", "output_config",
                         "fallbacks", "messages", "stream"}           # no tool_choice, temperature, top_p, top_k
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000 and body["stream"] is True
    assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["system"] == [{"type": "text", "text": assistant.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    # the messages end with the person's turn: today's date and the page name first, then what they typed
    assert body["messages"][-1]["role"] == "user"
    first, typed = body["messages"][-1]["content"]
    assert first["text"].startswith("[Today is ") and first["text"].endswith(" in California. Open page: Checks.]")
    assert typed == {"type": "text", "text": "Hello"}
    tools = body["tools"]
    assert tools[0] == {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
    client_tools = tools[1:]
    assert [t["name"] for t in client_tools] == sorted(t["name"] for t in client_tools) and len(client_tools) >= 70
    assert {t["name"] for t in client_tools if not t.get("defer_loading")} == assistant.ALWAYS_LOADED
    assert all(t["eager_input_streaming"] is True and "cache_control" not in t for t in client_tools)
    assert all(t["description"] and t["input_schema"]["type"] == "object" for t in client_tools)
    # only tools marked read-only (all of them, today)
    from app import mcp_governance as governance
    index = anyio.run(governance.tool_index, app.state.platform)
    assert all(index[t["name"]]["read_only"] for t in client_tools)


def test_system_and_tools_are_the_same_bytes_for_everyone(app, owner, fake):
    fake.add(reply_text("One."), reply_text("Two."), reply_text("Three."))
    chat = new_chat(owner)
    assert ask(owner, chat, "First", page="dashboard").status_code == 200
    assert ask(owner, chat, "Second", page="nowhere").status_code == 200
    with browser(owner) as other:
        signup(other, email="b@bravo.test", company="Bravo", license_number="654321")
        app.state.platform.store.update_user(other.get("/api/me").json()["tenant"]["id"],
                                             other.get("/api/me").json()["user"]["id"], role="field")
        assert ask(other, new_chat(other), "Third").status_code == 200
    dumps = {(json.dumps(b["system"], sort_keys=True), json.dumps(b["tools"], sort_keys=True)) for b in fake.bodies}
    assert len(dumps) == 1
    assert fake.bodies[1]["messages"][-1]["content"][0]["text"].endswith("Open page: none.]")   # unknown page


def test_checked_line_titles_come_from_the_catalog_but_the_tool_list_does_not(app):
    import asyncio

    from app import api, catalog
    specs = asyncio.run(api.tool_specs(app.state.platform))
    tools, titles = assistant.claude_tools(specs)
    assert titles["adu_rules"] == "ADU rules" and titles["select_lien_waiver"] == catalog.TOOLS["select_lien_waiver"][
        "title"]
    sent = json.dumps(tools)
    assert not [t for t in tools if "title" in t]                    # no tool sent to Anthropic carries a title
    assert "Choose a lien waiver" not in sent and "x-label" not in sent
    by_name = {t["name"]: t for t in tools[1:]}
    spec = next(s for s in specs if s["name"] == "adu_rules")
    assert by_name["adu_rules"]["input_schema"] == spec["input_schema"]


def test_client_is_built_from_the_operator_key_only(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "not-this-one")
    platform = app.state.platform
    platform.assistant_client = None
    c = assistant.client_for(platform, "sk-ant-operator-key")
    assert c.api_key == "sk-ant-operator-key" and str(c.base_url).rstrip("/") == "https://api.anthropic.com"
    assert c.max_retries == 2 and c.timeout == assistant.CLIENT_TIMEOUT
    assert assistant.client_for(platform, "sk-ant-operator-key") is c                  # made once per key
    assert assistant.client_for(platform, "sk-ant-new-key") is not c


def test_model_and_effort_come_from_the_environment(app, owner, fake):
    app.state.platform.env.update(ASSISTANT_MODEL="claude-sonnet-5-5", ASSISTANT_EFFORT="high")
    fake.add(reply_text("Hi."))
    assert ask(owner, new_chat(owner), "Hello").status_code == 200
    assert fake.bodies[0]["model"] == "claude-sonnet-5-5" and fake.bodies[0]["output_config"] == {"effort": "high"}
    app.state.platform.env.update(ASSISTANT_MODEL="gpt-4; drop", ASSISTANT_EFFORT="max")
    cfg = assistant.config(app.state.platform)
    assert (cfg.model, cfg.effort) == ("claude-opus-5-5", "medium")


# ---------------------------------------------------------------------------- the tool loop
def test_checks_run_as_the_person_through_the_tools_path(app, owner, fake):
    fake.add(search_then_tool("lien deadline", "calculate_lien_deadline", LIEN),
             reply_text("Record by **January 29, 2027**. [Find a professional](#/professionals)"))
    chat = new_chat(owner)
    r = ask(owner, chat)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["turn"]["checked"] == [{"name": "calculate_lien_deadline", "title": "Calculate lien deadline",
                                       "ok": True}]
    assert out["turn"]["outcome"] == "answered" and "January 29, 2027" in out["turn"]["reply"]
    assert out["title"] == "When is my lien deadline?" and out["messages_left_today"] == 29
    sent = fake.bodies[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    kinds = [b["type"] for b in sent[1]["content"]]
    assert kinds == ["thinking", "server_tool_use", "tool_search_tool_result", "tool_use"]
    (res,) = sent[2]["content"]                    # the server tool search id is never answered
    assert res["type"] == "tool_result" and res["tool_use_id"] == "toolu_01" and "is_error" not in res
    payload = json.loads(res["content"])
    assert payload["result"]["lien_recording_deadline"] == "2027-01-30" and "result_sig" not in res["content"]
    assert any(h["type_id"] == "attorney" for h in payload["handoffs"])     # licensed-professional handoffs
    audit = owner.get("/api/audit").json()
    assert [(a["tool"], a["user"], a["source"], a["ok"]) for a in audit] == \
        [("calculate_lien_deadline", "owner@acme.test", "assistant", True)]
    chat_out = owner.get(f"/api/assistant/conversations/{chat}").json()
    assert [t["prompt"] for t in chat_out["turns"]] == ["When is my lien deadline?"]
    assert "messages_json" not in json.dumps(chat_out) and "sig-abc123" not in json.dumps(chat_out)
    assert usage_rows(app) == [(owner.get("/api/me").json()["user"]["id"], 2400, 160, 0, 0)]     # both calls


def test_history_is_replayed_byte_for_byte(app, owner, fake):
    fake.add(ask_tool("calculate_lien_deadline", LIEN),
             message([thinking("sig-two"), text("Answer one.")]),
             reply_text("Answer two."))
    chat = new_chat(owner)
    assert ask(owner, chat, "First question").status_code == 200
    assert ask(owner, chat, "Second question").status_code == 200
    first_turn = fake.bodies[1]["messages"] + [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-two"}, {"type": "text", "text": "Answer one."}]}]
    replay = fake.bodies[2]["messages"]
    assert json.dumps(replay[:4], sort_keys=True) == json.dumps(first_turn, sort_keys=True)
    assert replay[4]["content"][1]["text"] == "Second question" and len(replay) == 5
    stored = json.loads(rows(app, "select messages_json from assistant_turns order by id")[0][0])
    assert json.dumps(stored, sort_keys=True) == json.dumps(first_turn, sort_keys=True)


def test_pause_turn_is_sent_back_unchanged(owner, fake):
    paused = message([thinking(), {"type": "server_tool_use", "id": "srvtoolu_01", "name": "tool_search_tool_bm25",
                                   "input": {"query": "heat illness"}}], stop_reason="pause_turn")
    fake.add(paused, reply_text("Shade at 80 F."))
    assert ask(owner, new_chat(owner), "Heat rules?").status_code == 200
    second = fake.bodies[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant"]        # no extra "continue" message
    assert second[1]["content"][1]["id"] == "srvtoolu_01"


def test_a_tool_not_marked_read_only_is_neither_offered_nor_run(app, owner, fake):
    platform = app.state.platform
    from app import api
    specs = anyio.run(api.tool_specs, platform)
    lien = "calculate_lien_deadline"
    changed = [dict(s, annotations={**s["annotations"], "readOnlyHint": False}) if s["name"] == lien else s
               for s in specs]
    platform.tool_cache = (time.monotonic() + 3600, changed)
    fake.add(ask_tool("calculate_lien_deadline", LIEN), reply_text("I can't run that one."))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 200 and r.json()["turn"]["checked"] == []
    assert "calculate_lien_deadline" not in {t.get("name") for t in fake.bodies[0]["tools"]}
    (res,) = fake.bodies[1]["messages"][2]["content"]
    assert res["is_error"] is True and res["content"] == "That check is not available here."
    assert owner.get("/api/audit").json() == []                                 # never ran
    assert assistant.claude_tools([{"name": "write_x", "title": "W", "description": "d",
                                    "input_schema": {"type": "object"}, "annotations": {}}]) == \
        ([assistant.SEARCH_TOOL], {})                                           # no annotation: fail closed


def test_tool_input_that_is_not_an_object_is_not_run(owner, fake):
    step = ask_tool("calculate_lien_deadline", {})
    step["_raw_inputs"] = {2: '"not an object"'}
    fake.add(step, reply_text("Sorry."))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 200
    (res,) = fake.bodies[1]["messages"][2]["content"]
    assert res["is_error"] is True and json.loads(res["content"]) == {"INVALID_JSON": '"not an object"'}
    assert r.json()["turn"]["checked"] == [{"name": "calculate_lien_deadline", "title": "Calculate lien deadline",
                                            "ok": False}]
    assert owner.get("/api/audit").json() == []


def test_bad_arguments_come_back_as_the_checks_own_plain_error(owner, fake):
    fake.add(ask_tool("calculate_lien_deadline", {"claimant_role": "subcontractor"}), reply_text("Need a date."))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 200
    (res,) = fake.bodies[1]["messages"][2]["content"]
    assert res["is_error"] is True and res["content"].startswith("Check these inputs.")
    assert [(a["source"], a["ok"]) for a in owner.get("/api/audit").json()] == [("assistant", False)]


def test_unreadable_tool_json_is_asked_again_at_most_twice(owner, fake):
    broken = ask_tool("calculate_lien_deadline", {})
    broken["_raw_inputs"] = {2: '{"claimant_role" "sub'}
    fake.add(dict(broken, _raw_inputs=dict(broken["_raw_inputs"])), reply_text("Fine now."))
    assert ask(owner, new_chat(owner)).status_code == 200
    assert len(fake.bodies) == 2 and fake.bodies[0] == fake.bodies[1]           # the same request again
    fake.add(*[dict(broken, _raw_inputs=dict(broken["_raw_inputs"])) for _ in range(3)])
    r = ask(owner, new_chat(owner))
    assert r.status_code == 502 and "couldn't be read" in r.json()["detail"]
    assert len(fake.bodies) == 5


def test_long_results_are_cut_with_a_note(owner, fake, monkeypatch):
    monkeypatch.setattr(assistant, "MAX_RESULT_CHARS", 300)
    fake.add(ask_tool("calculate_lien_deadline", LIEN), reply_text("Done."))
    assert ask(owner, new_chat(owner)).status_code == 200
    content = fake.bodies[1]["messages"][2]["content"][0]["content"]
    assert len(content) < 400 and content.endswith("[cut: the full result is on the Checks page, "
                                                   "#/tools/calculate_lien_deadline]")


# ---------------------------------------------------------------------------- who may use it
def test_people_only_never_api_keys_or_connected_apps(app, owner):
    key = owner.post("/api/keys", json={"name": "k"}).json()["key"]
    connected = oauth_connect(owner)["tokens"]["access_token"]
    with browser(owner) as anon:
        for token, code in ((key, 403), (connected, 401)):
            h = {"authorization": f"Bearer {token}"}
            assert anon.post("/api/assistant/conversations", json={}, headers=h).status_code == code
            assert anon.get("/api/assistant/conversations", headers=h).status_code == code
            assert anon.post("/api/assistant/conversations/1/messages", json={"text": "hi"},
                             headers=h).status_code == code
        assert anon.get("/api/assistant/status").status_code == 401
    csrf = owner.headers.pop("x-csrf-token")
    assert owner.post("/api/assistant/conversations", json={}).status_code == 403          # CSRF
    owner.headers["x-csrf-token"] = csrf


def test_chats_are_private_to_their_person(app, owner, fake):
    fake.add(reply_text("Mine."))
    chat = new_chat(owner)
    assert ask(owner, chat, "Owner's question").status_code == 200
    with browser(owner) as office, browser(owner) as stranger:
        join(office, owner, "office@acme.test", "office")
        signup(stranger, email="b@bravo.test", company="Bravo", license_number="654321")
        for c in (office, stranger):
            assert c.get(f"/api/assistant/conversations/{chat}").status_code == 404
            assert ask(c, chat).status_code == 404
            assert c.delete(f"/api/assistant/conversations/{chat}").status_code == 404
            assert c.post(f"/api/assistant/conversations/{chat}/stop").json() == {"stopping": False}
            assert c.get("/api/assistant/conversations").json() == {"conversations": []}
    assert len(fake.bodies) == 1
    mine = owner.get("/api/assistant/conversations").json()["conversations"]
    assert [(c["id"], c["title"], c["turns"]) for c in mine] == [(chat, "Owner's question", 1)]
    assert owner.delete(f"/api/assistant/conversations/{chat}").status_code == 204
    assert owner.get(f"/api/assistant/conversations/{chat}").status_code == 404


def test_any_role_can_ask_but_only_owners_see_usage_and_switch_it(app, owner, fake):
    fake.add(reply_text("For the field."))
    with browser(owner) as field, browser(owner) as office:
        join(field, owner, "crew@acme.test", "field")
        join(office, owner, "office@acme.test", "office")
        assert ask(field, new_chat(field), "Heat plan?").status_code == 200
        for c in (field, office):
            assert c.get("/api/assistant/usage").status_code == 403
            assert c.put("/api/assistant/settings", json={"enabled": False}).status_code == 403
        r = office.patch("/api/tenant", json={"settings": {"assistant_off": True}})   # reserved: ignored
        assert r.status_code == 200 and "assistant_off" not in r.json()["settings"]
        assert field.get("/api/assistant/status").json()["available"] is True
        usage = owner.get("/api/assistant/usage").json()
        assert usage["messages"] == 1 and usage["people"] == [{"email": "crew@acme.test", "messages": 1,
                                                                "counted_tokens": 1280}]
        assert usage["counted_tokens"] == 1280 and usage["budget_tokens"] == 5_000_000
        # the owner turns it off for the company
        st = owner.put("/api/assistant/settings", json={"enabled": False}).json()
        assert st["available"] is False and st["reason"] == "turned_off"
        assert field.get("/api/me").json()["assistant"]["reason"] == "turned_off"
        field_chat = field.get("/api/assistant/conversations").json()["conversations"][0]["id"]
        r = ask(field, field_chat)
        assert r.status_code == 403 and "turned off" in r.json()["detail"]
        assert field.post("/api/assistant/conversations", json={}).status_code == 403
        assert field.get(f"/api/assistant/conversations/{field_chat}").status_code == 200    # still readable
        assert owner.put("/api/assistant/settings", json={"enabled": True}).json()["available"] is True
        assert "assistant_off" not in owner.get("/api/me").json()["tenant"]["settings"]
        assert [a["tool"] for a in owner.get("/api/audit").json()][:2] == ["assistant.settings", "assistant.settings"]


def test_an_ended_subscription_is_a_402_like_every_check(app, owner, fake):
    chat = new_chat(owner)
    app.state.platform.store.update_tenant(owner.get("/api/me").json()["tenant"]["id"], status="canceled")
    assert ask(owner, chat).status_code == 402
    assert owner.get("/api/assistant/status").json()["reason"] == "no_subscription"
    assert fake.bodies == []


# ---------------------------------------------------------------------------- limits
def test_daily_message_limit(app, owner, fake):
    app.state.platform.env["ASSISTANT_DAILY_MESSAGES"] = "2"
    fake.add(reply_text("1"), reply_text("2"))
    chat = new_chat(owner)
    assert ask(owner, chat, "a").json()["messages_left_today"] == 1
    assert ask(owner, chat, "b").json()["messages_left_today"] == 0
    r = ask(owner, chat, "c")
    assert r.status_code == 429 and "daily limit" in r.json()["detail"] and len(fake.bodies) == 2
    owner.delete(f"/api/assistant/conversations/{chat}")                    # deleting chats doesn't reset it
    assert ask(owner, new_chat(owner), "d").status_code == 429


def test_monthly_company_budget_before_and_during_a_turn(app, owner, fake):
    env = app.state.platform.env
    env["ASSISTANT_MONTHLY_TOKENS"] = "1000"
    fake.add(reply_text("Big.", input_tokens=900, output_tokens=200))
    chat = new_chat(owner)
    assert ask(owner, chat, "a").status_code == 200
    r = ask(owner, chat, "b")
    assert r.status_code == 429 and "allowance" in r.json()["detail"] and len(fake.bodies) == 1
    # during a turn: the check runs between rounds too, and nothing of the turn is saved
    env["ASSISTANT_MONTHLY_TOKENS"] = "2500"        # 1,100 used; the first call of the turn brings it to 2,700
    fake.add(ask_tool("calculate_lien_deadline", LIEN, input_tokens=1500, output_tokens=100))
    r = ask(owner, new_chat(owner), "c")
    assert r.status_code == 429 and len(fake.bodies) == 2
    assert rows(app, "select count(*) from assistant_turns") == [(1,)]
    assert len(usage_rows(app)) == 2 and owner.get("/api/audit").json() == []


def test_too_many_rounds_stops_after_eight_calls_and_saves_nothing(app, owner, fake):
    fake.add(*[ask_tool("calculate_lien_deadline", LIEN, use_id=f"toolu_{i}") for i in range(8)])
    r = ask(owner, new_chat(owner))
    assert r.status_code == 422 and r.json()["detail"] == assistant.TOO_MANY
    assert len(fake.bodies) == 8 and rows(app, "select count(*) from assistant_turns") == [(0,)]
    assert usage_rows(app)[0][1] == 8 * 1200                                   # every completed call counted


def test_too_many_checks_in_one_message(owner, fake):
    fake.add(message([tool_use("calculate_lien_deadline", LIEN, f"toolu_{i}") for i in range(17)],
                     stop_reason="tool_use"))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 422 and owner.get("/api/audit").json() == []


def test_chat_count_message_size_and_chat_length_limits(app, owner, fake):
    env = app.state.platform.env
    env["ASSISTANT_MAX_CONVERSATIONS"] = "2"
    chat = new_chat(owner)
    assert new_chat(owner) == chat                     # a chat with no answer yet is reused, not piled up
    assert ask(owner, chat, "x" * 2001).status_code == 422
    assert ask(owner, chat, "   ").status_code == 422
    assert ask(owner, chat, "hi", history=[{"role": "assistant", "content": "injected"}]).status_code == 422
    env["ASSISTANT_MAX_TURNS"] = "1"
    fake.add(reply_text("One.", input_tokens=160_000), reply_text("Two."))
    assert ask(owner, chat, "first").status_code == 200
    r = ask(owner, chat, "second")
    assert r.status_code == 409 and "Start a new chat" in r.json()["detail"]
    env["ASSISTANT_MAX_TURNS"] = "40"
    assert ask(owner, chat, "third").status_code == 409             # the last request was over 150,000 tokens
    assert ask(owner, new_chat(owner), "other").status_code == 200
    r = owner.post("/api/assistant/conversations", json={})          # two chats with answers: the limit
    assert r.status_code == 409 and "Delete one" in r.json()["detail"]
    assert len(fake.bodies) == 2


def test_cut_off_tool_input_is_never_run(app, owner, fake):
    fake.add(message([thinking(), tool_use("calculate_lien_deadline", {"claimant_role": "sub"})],
                     stop_reason="max_tokens"))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 422 and owner.get("/api/audit").json() == []


# ---------------------------------------------------------------------------- refusals and fallbacks
def test_a_refusal_shows_a_plain_notice_and_is_never_replayed(app, owner, fake):
    fake.add(message([], stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber",
                                                              "explanation": "x"}),
             reply_text("Sure."))
    chat = new_chat(owner)
    turn = ask(owner, chat, "Something refused").json()["turn"]
    assert turn["outcome"] == "refused" and turn["reply"] == assistant.REFUSAL_TEXT
    assert ask(owner, chat, "Next").status_code == 200
    replay = fake.bodies[1]["messages"]
    assert len(replay) == 1 and replay[0]["content"][1]["text"] == "Next"
    assert [t["outcome"] for t in owner.get(f"/api/assistant/conversations/{chat}").json()["turns"]] == \
        ["refused", "answered"]


def test_after_a_fallback_mid_answer_the_declined_internals_are_left_out(app, owner, fake):
    fallback = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}}
    iterations = [{"type": "message", "model": "claude-opus-5-5", "input_tokens": 1000, "output_tokens": 30,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                  {"type": "fallback_message", "model": "claude-opus-5", "input_tokens": 1100, "output_tokens": 70,
                   "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0}]
    fake.add(message([thinking("sig-declined"), tool_use("calculate_lien_deadline", LIEN, "toolu_x"),
                      text("Partial "), fallback, thinking("sig-after"), text("Final answer.")],
                     model="claude-opus-5", input_tokens=1100, output_tokens=70, cache_read=50,
                     iterations=iterations),
             reply_text("Next."))
    chat = new_chat(owner)
    turn = ask(owner, chat).json()["turn"]
    assert turn["reply"] == "Partial Final answer." and turn["checked"] == []   # continued; the declined call never ran
    assert ask(owner, chat, "Next").status_code == 200
    stored = fake.bodies[1]["messages"][1]["content"]
    assert [b["type"] for b in stored] == ["text", "fallback", "thinking", "text"]
    assert stored[1] == fallback and stored[2]["signature"] == "sig-after"
    assert usage_rows(app)[0][1:] == (1000 + 1100, 30 + 70, 50, 0)          # both attempts, from usage.iterations
    assert owner.get("/api/audit").json() == []


def test_echo_rules_keep_paired_server_tool_blocks():
    blocks = [{"type": "server_tool_use", "id": "s1"}, {"type": "tool_search_tool_result", "tool_use_id": "s1"},
              {"type": "server_tool_use", "id": "s2"}, {"type": "redacted_thinking", "data": "x"},
              {"type": "text", "text": "a"}, {"type": "fallback"}, {"type": "tool_use", "id": "t"}]
    assert [b.get("id") or b.get("tool_use_id") or b["type"] for b in assistant.echoable(blocks)] == \
        ["s1", "s1", "text", "fallback", "t"]
    assert assistant.echoable(blocks[:2]) == blocks[:2]                    # no fallback: unchanged


# ---------------------------------------------------------------------------- errors from Anthropic
@pytest.mark.parametrize("step, status, words", [
    (error_response(429, "rate_limit_error", headers={"retry-after": "7"}), 503, "busy"),
    (error_response(529, "overloaded_error"), 503, "busy"),
    (error_response(500, "api_error"), 503, "busy"),
    (error_response(429, "rate_limit_error", error_code="enforced_spend_limit_reached"), 503, "spending limit"),
    (error_response(400, "invalid_request_error"), 502, "Start a new chat"),
    (error_response(404, "not_found_error"), 503, "not set up correctly"),
    (httpx2.ConnectError, 503, "Could not reach"),
    (httpx2.ReadTimeout, 504, "took too long"),
])
def test_anthropic_errors_become_plain_messages_and_nothing_is_saved(app, owner, fake, step, status, words):
    fake.add(step)
    r = ask(owner, new_chat(owner))
    assert r.status_code == status and words in r.json()["detail"], r.text
    assert rows(app, "select count(*) from assistant_turns") == [(0,)]
    assert [u[1:] for u in usage_rows(app)] == [(0, 0, 0, 0)]       # the message counts toward the daily limit
    if status == 503 and words == "busy" and "retry-after" in step.headers:
        assert r.headers["retry-after"] == "7"


def test_a_bad_key_is_logged_without_the_key(app, owner, fake, caplog):
    fake.add(error_response(401, "authentication_error", "invalid x-api-key"))
    with caplog.at_level(logging.DEBUG):
        r = ask(owner, new_chat(owner))
    assert r.status_code == 503 and "not set up correctly" in r.json()["detail"]
    assert "AuthenticationError" in caplog.text and "req_test_1" in caplog.text
    assert FAKE_KEY not in caplog.text and FAKE_KEY not in r.text


def test_usage_counts_completed_calls_when_a_later_call_fails(app, owner, fake):
    fake.add(ask_tool("calculate_lien_deadline", LIEN), error_response(529, "overloaded_error"))
    assert ask(owner, new_chat(owner)).status_code == 503
    assert usage_rows(app)[0][1:] == (1200, 80, 0, 0)
    assert rows(app, "select count(*) from assistant_turns") == [(0,)]


def test_the_whole_turn_has_a_deadline(app, owner, fake):
    app.state.platform.env["ASSISTANT_TURN_TIMEOUT_S"] = "1"

    async def slow(request):
        await anyio.sleep(5)
        return reply_text("late")
    fake.add(slow)
    started = time.monotonic()
    r = ask(owner, new_chat(owner))
    assert r.status_code == 504 and time.monotonic() - started < 4
    assert rows(app, "select count(*) from assistant_turns") == [(0,)]


def test_stop_ends_the_answer_and_one_message_at_a_time(app, owner, fake):
    arrived = threading.Event()

    async def held(request):
        arrived.set()
        await anyio.sleep(30)
        return reply_text("never")
    fake.add(reply_text("Earlier."), held)
    other = new_chat(owner)
    assert ask(owner, other, "earlier").status_code == 200
    chat = new_chat(owner)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", ask(owner, chat)))
    t.start()
    assert arrived.wait(10)
    busy = ask(owner, other, "meanwhile")
    assert busy.status_code == 409 and "Still answering" in busy.json()["detail"]
    assert owner.post(f"/api/assistant/conversations/{other}/stop").json() == {"stopping": False}
    assert owner.post(f"/api/assistant/conversations/{chat}/stop").json() == {"stopping": True}
    t.join(10)
    r = result["r"]
    assert r.status_code == 200 and r.json()["turn"] is None and r.json()["stopped"] is True
    assert rows(app, "select count(*) from assistant_turns") == [(1,)]           # only the earlier answer
    fake.add(reply_text("Back."))
    assert ask(owner, chat, "again").status_code == 200                         # not busy any more


# ---------------------------------------------------------------------------- data rights and retention
def test_export_has_the_text_not_the_raw_messages_and_deletion_removes_everything(app, owner, fake):
    fake.add(ask_tool("calculate_lien_deadline", LIEN), reply_text("Exported answer."))
    chat = new_chat(owner)
    assert ask(owner, chat, "Exported question").status_code == 200
    data = owner.get("/api/tenant/export").json()
    assert [(c["id"], c["title"]) for c in data["ask_tradecomply_chats"]] == [(chat, "Exported question")]
    (turn,) = data["ask_tradecomply_messages"]
    assert (turn["prompt"], turn["reply"], turn["outcome"]) == ("Exported question", "Exported answer.", "answered")
    assert turn["checked"] == [{"name": "calculate_lien_deadline", "ok": True, "title": "Calculate lien deadline"}]
    assert data["ask_tradecomply_usage"][0]["input_tokens"] == 2400
    text_ = json.dumps(data)
    assert "messages_json" not in turn and "sig-abc123" not in text_ and "toolu_01" not in text_
    assert "messages_json" in data["about"]["fields_left_out"]["ask_tradecomply_messages"]
    tid = owner.get("/api/me").json()["tenant"]["id"]
    removed = app.state.platform.store.delete_tenant(tid)
    assert removed["assistant_turns"] == 1 and removed["assistant_conversations"] == 1
    assert removed["assistant_usage"] == 1


def test_removing_a_person_deletes_their_chats_and_keeps_usage_unnamed(app, owner, fake):
    fake.add(reply_text("Crew answer."))
    with browser(owner) as field:
        join(field, owner, "crew@acme.test", "field")
        uid = field.get("/api/me").json()["user"]["id"]
        assert ask(field, new_chat(field), "Crew question").status_code == 200
    assert owner.delete(f"/api/users/{uid}").status_code in (200, 204)
    assert rows(app, "select count(*) from assistant_conversations") == [(0,)]
    assert rows(app, "select count(*) from assistant_turns") == [(0,)]
    assert rows(app, "select user_id from assistant_usage") == [(None,)]
    usage = owner.get("/api/assistant/usage").json()
    assert usage["messages"] == 1 and usage["people"] == [{"email": None, "messages": 1, "counted_tokens": 1280}]
    tid = owner.get("/api/me").json()["tenant"]["id"]      # an answer that finishes after the removal names no one
    app.state.platform.store.start_assistant_usage(tid, uid, "claude-opus-5-5")
    assert rows(app, "select user_id from assistant_usage order by id") == [(None,), (None,)]


def test_old_chats_are_purged(app, owner, fake):
    fake.add(reply_text("Old."))
    chat = new_chat(owner)
    assert ask(owner, chat, "Old question").status_code == 200
    keep = new_chat(owner)
    with app.state.platform.store.connect() as con:
        con.execute("update assistant_conversations set updated = ? where id = ?", (iso_in(-91 * 86400), chat))
        con.execute("update assistant_usage set created = ?", (iso_in(-401 * 86400),))
    app.state.platform.assistant_purged_at = float("-inf")
    assert [c["id"] for c in owner.get("/api/assistant/conversations").json()["conversations"]] == [keep]
    assert rows(app, "select count(*) from assistant_turns") == [(0,)] and usage_rows(app) == []


def test_sdk_error_classes_used_here_exist_and_nest_as_documented():
    assert issubclass(anthropic.APITimeoutError, anthropic.APIConnectionError)
    assert issubclass(anthropic.OverloadedError, anthropic.InternalServerError)
    assert issubclass(anthropic.EnforcedSpendLimitReachedError, anthropic.RateLimitError)
    assert issubclass(anthropic.ContractSpendLimitReachedError, anthropic.RateLimitError)
    assert isinstance(assistant.CLIENT_TIMEOUT, httpx2.Timeout)


# ---------------------------------------------------------------------------- review fixes (regressions)
def _held(arrived: threading.Event, release: threading.Event | None = None, reply: str = "late"):
    async def step(request):
        arrived.set()
        while not (release and release.is_set()):
            await anyio.sleep(0.02)
        return reply_text(reply)
    return step


def _sse_then(head_of: dict, exc: Exception | None = None, until: bytes = b"event: content_block_delta"):
    """An SSE body for `head_of` cut right before `until`: then `exc` is raised (a dropped connection), or, with no
    exc, the body just ends (no message_stop)."""
    from tests.fake_anthropic import sse
    full = sse(head_of, head_of.get("_raw_inputs"))
    head = full[: full.index(until)]
    if exc is None:
        return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_cut"},
                               content=head)

    class Broken(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield head
            raise exc
    return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_cut"},
                           stream=Broken())


def test_a_stopped_message_still_counts_toward_the_daily_limit(app, owner, fake):
    """Send, then Stop before Anthropic answers: that request reached Anthropic, so it uses a message up."""
    app.state.platform.env["ASSISTANT_DAILY_MESSAGES"] = "1"
    chat = new_chat(owner)
    arrived, out = threading.Event(), {}
    fake.add(_held(arrived))
    t = threading.Thread(target=lambda: out.setdefault("r", ask(owner, chat, "question")))
    t.start()
    assert arrived.wait(10)
    assert owner.post(f"/api/assistant/conversations/{chat}/stop").json() == {"stopping": True}
    t.join(10)
    assert out["r"].json() == {"turn": None, "stopped": True, "messages_left_today": 0}
    assert owner.get("/api/assistant/status").json()["limits"]["messages_left_today"] == 0
    r = ask(owner, chat, "again")
    assert r.status_code == 429 and len(fake.bodies) == 1 and len(usage_rows(app)) == 1


@pytest.mark.parametrize("exc, status", [(httpx2.ReadError("connection reset"), 503),
                                         (httpx2.RemoteProtocolError("incomplete chunked read"), 503),
                                         (httpx2.ReadTimeout("read timed out"), 504)])
def test_a_connection_lost_mid_answer_is_a_plain_message_and_its_input_counts(app, owner, fake, exc, status):
    fake.add(_sse_then(reply_text("A long answer that never finishes.", input_tokens=150_000), exc))
    with browser(owner, raise_server_exceptions=False) as c:
        c.cookies.update(owner.cookies)
        c.headers["x-csrf-token"] = owner.headers["x-csrf-token"]
        r = ask(c, new_chat(c))
    assert r.status_code == status and r.json()["detail"] in ("Could not reach the AI helper. Try again in a minute.",
                                                              "The AI helper took too long. Try again.")
    assert usage_rows(app)[0][1] == 150_000 and rows(app, "select count(*) from assistant_turns") == [(0,)]


def test_unreadable_tool_input_retries_are_counted(app, owner, fake):
    broken = ask_tool("calculate_lien_deadline", {})
    fake.add(*[dict(broken, _raw_inputs={2: '{"claimant_role" "sub'}) for _ in range(3)])
    r = ask(owner, new_chat(owner))
    assert r.status_code == 502 and len(fake.bodies) == 3
    assert usage_rows(app)[0][1] == 3 * 1200                    # message_start reported each call's input
    assert owner.get("/api/assistant/status").json()["limits"]["messages_left_today"] == 29


def test_a_stream_that_ends_before_message_stop_is_a_failed_call(app, owner, fake):
    """get_final_message() returns the partial snapshot then (stop_reason None): a tool input cut inside a field can
    still parse to a smaller valid object, so nothing runs; cut text is never saved as an answer."""
    step = ask_tool("calculate_lien_deadline", {})
    step["_raw_inputs"] = {2: '{"claimant_role": "subcontractor", "last_furnishing_date": "2026-11-01", '
                              '"notice_of_completion_date": "2026-11-0'}
    fake.add(_sse_then(step, until=b'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 2}'),
             _sse_then(reply_text("Record the lien by January 29, 2027."),
                       until=b'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 1}'))
    for _ in range(2):
        r = ask(owner, new_chat(owner))
        assert r.status_code == 502 and r.json()["detail"] == assistant.BROKEN_OFF
    assert owner.get("/api/audit").json() == [] and rows(app, "select count(*) from assistant_turns") == [(0,)]


def test_an_overloaded_error_inside_the_stream_is_busy(app, owner, fake):
    head = _sse_then(reply_text("x")).content
    err = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    fake.add(httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_mid"},
                             content=head + f"event: error\ndata: {json.dumps(err)}\n\n".encode()))
    r = ask(owner, new_chat(owner))
    assert r.status_code == 503 and r.json()["detail"] == assistant.BUSY


def test_an_empty_answer_is_shown_but_never_replayed(app, owner, fake):
    fake.add(ask_tool("calculate_lien_deadline", LIEN), message([], stop_reason="end_turn"), reply_text("Next."))
    chat = new_chat(owner)
    assert ask(owner, chat).json()["turn"]["reply"] == assistant.EMPTY_TEXT
    assert ask(owner, chat, "Try again").status_code == 200
    replay = fake.bodies[2]["messages"]
    assert len(replay) == 1 and all(m["content"] for m in replay)


def test_text_is_joined_across_a_fallback_but_not_across_checks():
    fb = {"type": "fallback"}
    assert assistant.reply_of([thinking(), text("by January 2"), fb, thinking(), text("9, 2027.")]) == \
        "by January 29, 2027."
    assert assistant.reply_of([text("One."), {"type": "server_tool_use"}, text("Two.")]) == "One.\n\nTwo."
    assert assistant.reply_of([thinking()]) == ""


def test_an_answer_finishing_after_its_person_was_removed_lands_nowhere(app, owner, fake):
    """SQLite gives a new person the removed one's id; chat ids are never reused, so A's late answer can't reach the
    new person's chat, and the usage row (written when A asked) was un-named when A was removed."""
    arrived, release, out = threading.Event(), threading.Event(), {}
    with browser(owner) as a, browser(owner) as c:
        join(a, owner, "a@acme.test", "field")
        a_id, a_chat = a.get("/api/me").json()["user"]["id"], new_chat(a)
        fake.add(_held(arrived, release, reply="Answer meant for A only."))
        t = threading.Thread(target=lambda: out.setdefault("r", ask(a, a_chat, "A's private question")))
        t.start()
        assert arrived.wait(10)
        assert owner.delete(f"/api/users/{a_id}").status_code == 200
        join(c, owner, "c@acme.test", "field")
        c_chat = new_chat(c)
        assert c_chat != a_chat
        release.set()
        t.join(15)
        assert out["r"].status_code in (401, 404)
        assert c.get(f"/api/assistant/conversations/{c_chat}").json()["turns"] == []
    assert rows(app, "select user_id from assistant_usage") == [(None,)]


def test_chats_past_retention_are_never_served_and_are_purged_without_helper_traffic(app, owner, fake):
    fake.add(reply_text("Old answer."))
    chat = new_chat(owner)
    assert ask(owner, chat, "Old question").status_code == 200
    with app.state.platform.store.connect() as con:
        con.execute("update assistant_conversations set updated = ?", (iso_in(-91 * 86400),))
    assert owner.get(f"/api/assistant/conversations/{chat}").status_code == 404     # before any purge ran
    assert ask(owner, chat, "more").status_code == 404
    app.state.platform.env = {}                                                     # the key removed
    app.state.platform.assistant_purged_at = float("-inf")
    assistant.retention(app.state.platform)                                         # the event worker, hourly
    assert rows(app, "select count(*) from assistant_turns") == [(0,)]


def test_people_asking_at_once_share_the_company_budget(app, owner, fake):
    """Usage is saved after every call and the company total read again before the next one, so concurrent turns
    overshoot by at most one call each (before: every turn ran to the end on the total read when it started)."""
    app.state.platform.env["ASSISTANT_MONTHLY_TOKENS"] = "3000"
    gate = threading.Barrier(2, timeout=10)

    async def first(request):
        await anyio.to_thread.run_sync(gate.wait)
        return ask_tool("calculate_lien_deadline", LIEN, input_tokens=1500, output_tokens=80)
    fake.add(first, first, reply_text("Done.", input_tokens=1500, output_tokens=80),
             reply_text("Done.", input_tokens=1500, output_tokens=80))
    with browser(owner) as office:
        join(office, owner, "office@acme.test", "office")
        chats, out = {"owner": new_chat(owner), "office": new_chat(office)}, {}
        threads = [threading.Thread(target=lambda who=who, c=c: out.setdefault(who, ask(c, chats[who])))
                   for who, c in (("owner", owner), ("office", office))]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
    used = sum(r[1] + r[2] for r in usage_rows(app))
    assert 429 in [r.status_code for r in out.values()] and used <= 3 * 1580, used


def test_unanswered_chats_never_block_a_new_chat(app, owner, fake):
    app.state.platform.env["ASSISTANT_MAX_CONVERSATIONS"] = "3"
    fake.add(reply_text("Subcontractors are on the Subs page."))
    assert ask(owner, new_chat(owner), "Where are subs?").status_code == 200
    for _ in range(3):                                   # first questions that got "busy"
        fake.add(error_response(529, "overloaded_error"))
        assert ask(owner, new_chat(owner), "Lien?").status_code == 503
    listed = owner.get("/api/assistant/conversations").json()["conversations"]
    assert [c["turns"] for c in listed] == [0, 1]
    assert owner.post("/api/assistant/conversations", json={}).status_code == 201


def test_what_a_private_chat_passes_to_a_check_is_not_copied_into_the_activity_log(app, owner, fake):
    """Final review data-private-chat-in-activity-log: the check's row in the company-wide activity log keeps the
    argument names and a fingerprint (as /mcp does), never the words from a chat only its person can read."""
    need = "attorney for Juan Perez who fell off our scaffold at 12 Oak St and wants to sue us"
    fake.add(search_then_tool("attorney", "find_licensed_professional", {"need": need}), reply_text("Here."))
    with browser(owner) as crew, browser(owner) as office:
        join(crew, owner, "crew@acme.test", "field")
        join(office, owner, "office@acme.test", "office")
        chat = new_chat(crew)
        assert ask(crew, chat, need).status_code == 200
        (row,) = [a for a in office.get("/api/audit").json() if a["tool"] == "find_licensed_professional"]
        from app import mcp_governance as governance
        assert row["source"] == "assistant" and row["args"] == governance.activity_args({"need": need})
        assert crew.delete(f"/api/assistant/conversations/{chat}").status_code == 204
    everything = json.dumps(owner.get("/api/audit").json()) + owner.get("/api/tenant/export").text
    assert "Juan Perez" not in everything and "scaffold" not in everything
