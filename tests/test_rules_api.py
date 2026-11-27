"""GET /api/rules/{rule_id} (spec 8.2.6): the rule page and the glossary popover."""
import pytest
from fastapi.testclient import TestClient

from app import search
from tests.support import backend, browser, join, make_app, oauth_connect, signup  # noqa: F401
from tradecomply import core

SITE = "https://tradecomply.example"
FIELDS = {"id", "title", "plain_english", "category", "citation", "source", "quote", "status", "verified",
          "checked_on", "effective", "correction", "applies_to", "related_tools", "disclaimer"}


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path, app_url=SITE)


@pytest.fixture
def owner(app):
    with TestClient(app, base_url=SITE) as c:
        c.tenant_id = signup(c)["tenant_id"]
        yield c


def test_rule_fields_verbatim(owner):
    for rid in ("prelim-notice-20-day-deadline", "hic-down-payment-cap", "permit-trench-excavation-5ft"):
        r = owner.get(f"/api/rules/{rid}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert set(body) == FIELDS
        rule = core.rule(rid)
        assert body["plain_english"] == rule.plain_english and body["quote"] == rule.quote
        assert body["citation"] == rule.citation_text and body["applies_to"] == rule.applies_to
        assert body["correction"] == rule.correction and body["verified"] == rule.verified
        assert body["disclaimer"] == core.DISCLAIMER
        assert "parameters" not in body and "notes" not in body and "pack" not in body
    corrected = owner.get("/api/rules/hic-down-payment-cap").json()
    assert corrected["status"] == "corrected" and corrected["correction"]


def test_related_tools_before_and_after_the_index(owner, app):
    search.reset_for_tests()
    try:
        early = owner.get("/api/rules/adu-detached-max-1200-attached-50pct").json()["related_tools"]
        assert early == [{"name": "adu_rules", "title": "ADU rules"}]       # computed without waiting for the index
        search.build_now(app.state.platform)
        assert owner.get("/api/rules/adu-detached-max-1200-attached-50pct").json()["related_tools"] == early
        assert owner.get("/api/rules/lien-deadline-subcontractor-supplier").json()["related_tools"] == []
    finally:
        search.reset_for_tests()


def test_unknown_not_entitled_and_who(owner, app):
    assert owner.get("/api/rules/no-such-rule").status_code == 404
    assert owner.get("/api/rules/" + "x" * 300).status_code == 404
    with browser(owner) as field:
        join(field, owner, "crew@acme.test", role="field")
        assert field.get("/api/rules/prelim-notice-20-day-deadline").status_code == 200
    default = owner.post("/api/keys", json={"name": "agent"}).json()["key"]
    tools_only = owner.post("/api/keys", json={"name": "bot", "scopes": {"tools": ["get_california_rule"],
                                                                         "rest_api": False}}).json()["key"]
    tokens = oauth_connect(owner)["tokens"]
    with browser(owner) as bot:
        for key in (default, tools_only):
            r = bot.get("/api/rules/prelim-notice-20-day-deadline", headers={"authorization": f"Bearer {key}"})
            assert r.status_code == 403
        r = bot.get("/api/rules/prelim-notice-20-day-deadline",
                    headers={"authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 401
        assert bot.get("/api/rules/prelim-notice-20-day-deadline").status_code == 401
    app.state.platform.store.update_tenant(owner.tenant_id, status="canceled")
    r = owner.get("/api/rules/prelim-notice-20-day-deadline")
    assert r.status_code == 402 and r.json()["detail"]["subscribe"] is True
    assert not [a for a in owner.get("/api/audit").json() if "rule" in a["tool"]]   # reading rules writes no row
