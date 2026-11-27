"""TEST ONLY: the TradeComply app with Ask TradeComply answered by the fake Anthropic API (tests/fake_anthropic.py),
for the browser tests in tests/test_ui_assistant.py, which run it in a uvicorn process:

    python -m uvicorn tests.assistant_ui_app:app

The answers are scripted from the question's words: "lien" runs the real calculate_lien_deadline check through a
tool search, "script" answers with HTML that must show as text, "slow" waits 30 s (for the Stop button), anything
else gets a short answer with a link to a page of the app. TC_TEST_ASSISTANT=off leaves the key unset (the helper is
"not set up"). No network, no real key.
"""
import os

import anyio

from app.main import create_app
from tests.fake_anthropic import FAKE_KEY, FakeAnthropic, reply_text, search_then_tool

LIEN_ANSWER = ("Record your mechanics lien by **January 29, 2027**, the business day before the deadline the check "
               "found (January 30, 2027 is a Saturday).\n\n- Source: Cal. Civ. Code 8414, from *Calculate lien "
               "deadline*. The rule is not yet verified against the official source.\n- If you record it, the lawsuit "
               "to foreclose must be filed within 90 days. Talk to an attorney: [Find a professional](#/professionals)."
               "\n\nThis is compliance information, not legal advice.")
HTML_ANSWER = 'Here it is: <script>window.__pwned = 1</script><img src=x onerror="window.__pwned = 1"> **done**'
PLAIN_ANSWER = ("Your own license status is on the [Dashboard](#/dashboard). To check a subcontractor, open "
                "[Subcontractors](#/subs) or run the CSLB license lookup on the Tools page.")


def _texts(msg: dict) -> list[str]:
    content = msg.get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if b.get("type") == "text"]


def respond(body: dict) -> dict:
    messages = body["messages"]
    question = next(" ".join(_texts(m)) for m in reversed(messages) if m["role"] == "user" and _texts(m)).lower()
    last = messages[-1]
    answered_tool = last["role"] == "user" and any(b.get("type") == "tool_result" for b in last["content"])
    if "lien" in question:
        if answered_tool:
            return reply_text(LIEN_ANSWER, input_tokens=4200, output_tokens=260, cache_read=3000)
        return search_then_tool("mechanics lien deadline", "calculate_lien_deadline",
                                {"claimant_role": "subcontractor", "last_furnishing_date": "2026-11-01"},
                                input_tokens=900, output_tokens=120, cache_write=3000)
    if "script" in question:
        return reply_text(HTML_ANSWER)
    if "slow" in question:
        async def held(request):
            await anyio.sleep(30)
            return reply_text("Late answer.")
        return held
    return reply_text(PLAIN_ANSWER, input_tokens=800, output_tokens=90, cache_read=3000)


app = create_app()
_platform = app.state.platform
_platform.env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
if os.environ.get("TC_TEST_ASSISTANT") != "off":
    _platform.env["ANTHROPIC_API_KEY"] = FAKE_KEY
    fake = FakeAnthropic(respond)
    _platform.assistant_client = fake.client
