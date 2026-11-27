"""A fake Anthropic API for tests: the real `anthropic` SDK client (so request building, streaming, parsing and the
typed exceptions are the SDK's own) over an httpx2.MockTransport that answers each POST /v1/messages from a script.
No network and no real key: nothing ever reaches Anthropic.

    fake = FakeAnthropic([reply_text("Hi"), ...])          # or a function(body) -> message dict / Response / exception
    platform.env = {"ANTHROPIC_API_KEY": FAKE_KEY}
    platform.assistant_client = fake.client
    fake.bodies          # every request body, parsed JSON; fake.headers: their headers

A scripted step is a message dict (content blocks as the API returns them, stop_reason, usage), an httpx2.Response
(an error status with the API's error JSON), an exception class/instance (httpx2.ConnectError, httpx2.ReadTimeout), or
a coroutine function (awaited first: lets a test hold a request open). The SSE events follow the Messages streaming
format: message_start, content_block_start / *_delta / content_block_stop per block, message_delta, message_stop.
"""
from __future__ import annotations

import copy
import json

import anthropic
import httpx2

FAKE_KEY = "sk-ant-api03-test-fake-key-not-real-0000"


def usage(input_tokens=1200, output_tokens=80, cache_read=0, cache_write=0, iterations=None) -> dict:
    u = {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cache_read,
         "cache_creation_input_tokens": cache_write}
    if iterations is not None:
        u["iterations"] = iterations
    return u


def message(content: list[dict], stop_reason: str = "end_turn", stop_details=None, model="claude-opus-5-5",
            **usage_kw) -> dict:
    return {"content": content, "stop_reason": stop_reason, "stop_details": stop_details, "model": model,
            "usage": usage(**usage_kw)}


def thinking(signature: str = "sig-abc123") -> dict:
    return {"type": "thinking", "thinking": "", "signature": signature}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_use(name: str, inp, use_id: str = "toolu_01") -> dict:
    return {"type": "tool_use", "id": use_id, "name": name, "input": inp}


def reply_text(t: str, **kw) -> dict:
    return message([thinking(), text(t)], **kw)


def ask_tool(name: str, inp, use_id: str = "toolu_01", **kw) -> dict:
    return message([thinking(), text("Let me check."), tool_use(name, inp, use_id)], stop_reason="tool_use", **kw)


def search_then_tool(query: str, name: str, inp, use_id: str = "toolu_01", **kw) -> dict:
    """What a deferred tool looks like: a server-side tool search, its result, then the call."""
    return message([thinking(), {"type": "server_tool_use", "id": "srvtoolu_01", "name": "tool_search_tool_bm25",
                                 "input": {"query": query}},
                    {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_01",
                     "content": {"type": "tool_search_tool_search_result",
                                 "tool_references": [{"type": "tool_reference", "tool_name": name}]}},
                    tool_use(name, inp, use_id)], stop_reason="tool_use", **kw)


def error_response(status: int, err_type: str, msg: str = "error", headers: dict | None = None,
                   error_code: str | None = None) -> httpx2.Response:
    err = {"type": err_type, "message": msg}
    if error_code:
        err["details"] = {"error_code": error_code}
    return httpx2.Response(status, json={"type": "error", "error": err},
                           headers={"request-id": "req_test_1", **(headers or {})})


def sse(msg: dict, raw_inputs: dict | None = None) -> bytes:
    """The SSE stream for one message. raw_inputs maps a block index to the exact partial_json to send (broken JSON)."""
    events = []
    u = msg.get("usage") or usage()
    start_usage = {k: u[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    events.append({"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": msg.get("model", "claude-opus-5-5"),
        "content": [], "stop_reason": None, "stop_details": None, "stop_sequence": None,
        "usage": {**start_usage, "output_tokens": 1}}})
    for i, b in enumerate(msg["content"]):
        kind = b["type"]
        if kind == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            if b["text"]:
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "text_delta", "text": b["text"]}})
        elif kind == "thinking":
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            if b.get("thinking"):
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "signature_delta", "signature": b["signature"]}})
        elif kind in ("tool_use", "server_tool_use"):
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": kind, "id": b["id"], "name": b["name"], "input": {}}})
            partial = (raw_inputs or {}).get(i, json.dumps(b["input"]))
            events.append({"type": "content_block_delta", "index": i,
                           "delta": {"type": "input_json_delta", "partial_json": partial}})
        else:                                   # tool_search_tool_result, fallback, redacted_thinking: whole
            events.append({"type": "content_block_start", "index": i, "content_block": b})
        events.append({"type": "content_block_stop", "index": i})
    delta_usage = {"output_tokens": u["output_tokens"], **start_usage}
    if "iterations" in u:
        delta_usage["iterations"] = u["iterations"]
    events.append({"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"],
                                                      "stop_details": msg.get("stop_details"), "stop_sequence": None},
                   "usage": delta_usage})
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class FakeAnthropic:
    def __init__(self, script=None):
        self.script = list(script or []) if not callable(script) else script
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.client = anthropic.AsyncAnthropic(
            api_key=FAKE_KEY, base_url="https://api.anthropic.com", max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._handle)))

    def add(self, *steps) -> "FakeAnthropic":
        self.script.extend(steps)
        return self

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(copy.deepcopy(body))
        self.headers.append(dict(request.headers))
        step = self.script(body) if callable(self.script) else (self.script.pop(0) if self.script else None)
        if step is None:
            raise AssertionError("the fake Anthropic API got more requests than the test scripted")
        if callable(step) and not isinstance(step, type):
            step = await step(request)
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("scripted failure", request=request)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx2.Response):
            return step
        raw = step.pop("_raw_inputs", None) if isinstance(step, dict) else None
        return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_test_ok"},
                               content=sse(step, raw))
