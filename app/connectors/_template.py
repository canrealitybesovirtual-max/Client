"""TEMPLATE: copy to app/connectors/<provider>.py and replace every placeholder. Not loaded by discovery (the name
starts with "_"); tests/test_integrations_api.py registers these classes to prove the template works end to end.

Everything below that names the vendor (URLs, scopes, headers, JSON fields) is a PLACEHOLDER on example.com, a
domain reserved for documentation (RFC 2606). A real connector takes each of these from the vendor's official API
docs (put the page you used in docs_url), and lists anything it could not confirm there in `unconfirmed`.
"""
from __future__ import annotations

from . import ConfigField, Connector, NotConnected, OAuth2Spec, PermanentError, TestResult


class ExampleApiKeyConnector(Connector):
    """A settings-based connector: the tenant pastes an API key (stored encrypted) and picks an account region."""
    provider = "example_apikey"
    display_name = "Example (API key)"
    catalog_id = "example-apikey"
    description = "Template for a connector that signs in with an API key the customer pastes in."
    category = "other"
    docs_url = "https://docs.example.com/api"                   # PLACEHOLDER: the vendor's API reference
    required_env = ()                                             # API-key connectors often need no operator app
    config_fields = (
        ConfigField("api_key", "API key", type="password", secret=True, required=True,
                    help="Create a key under Settings > API in your Example account."),
        ConfigField("region", "Region", type="select", options=(("us", "United States"), ("eu", "Europe")),
                    default="us"),
        ConfigField("company_id", "Company ID", pattern=r"[0-9]{1,12}", help="Shown on your Example account page."),
    )
    subscribes = ("sub_noncompliant",)
    unconfirmed = ("PLACEHOLDER: list every endpoint or field not confirmed in the vendor docs",)

    def _base(self) -> str:
        region = self.config().get("region") or "us"
        return {"us": "https://api.example.com", "eu": "https://eu.api.example.com"}[region]

    def test(self) -> TestResult:
        key = self.secrets().get("api_key")
        if not key:
            raise NotConnected("Save an API key first.")
        with self.ctx.http() as http:
            r = http.get(f"{self._base()}/v1/me", headers={"authorization": f"Bearer {key}"})   # PLACEHOLDER
        if r.status_code == 401:
            return TestResult(False, "Example rejected the API key. Check it and save it again.")
        if r.status_code != 200:
            return TestResult(False, f"Example answered HTTP {r.status_code}.")
        return TestResult(True, "Connected.", account=(r.json() or {}).get("company_name"))

    def handle_event(self, event: str, payload: dict) -> None:
        key = self.secrets().get("api_key")
        with self.ctx.http() as http:
            r = http.post(f"{self._base()}/v1/alerts", headers={"authorization": f"Bearer {key}"},   # PLACEHOLDER
                          json={"title": payload["title"], "body": payload.get("summary") or ""})
        if r.status_code in (400, 404, 422):
            raise PermanentError(f"Example refused the alert (HTTP {r.status_code})")
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}")            # anything else is retried with backoff


class ExampleOAuthConnector(Connector):
    """An OAuth 2.0 authorization-code connector with PKCE. The operator registers one OAuth app with the vendor,
    with redirect URI <APP_URL>/api/integrations/example_oauth/oauth/callback, and sets the two env vars."""
    provider = "example_oauth"
    display_name = "Example (OAuth)"
    catalog_id = "example-oauth"
    description = "Template for a connector the customer connects by signing in to the vendor."
    category = "project_management"
    docs_url = "https://docs.example.com/oauth"                 # PLACEHOLDER
    required_env = ("EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET")
    oauth = OAuth2Spec(
        authorize_url="https://login.example.com/oauth/authorize",    # PLACEHOLDER
        token_url="https://login.example.com/oauth/token",            # PLACEHOLDER
        client_id_env="EXAMPLE_CLIENT_ID", client_secret_env="EXAMPLE_CLIENT_SECRET",
        scopes=("read",),                                              # PLACEHOLDER: least privilege, from the docs
        pkce=True, client_auth="basic")
    unconfirmed = ("PLACEHOLDER: e.g. whether refresh tokens rotate on every refresh",)

    def on_connected(self, tokens: dict) -> str | None:
        with self.ctx.http() as http:
            r = http.get("https://api.example.com/v1/me", headers={"authorization": f"Bearer {tokens['access_token']}"})
        return (r.json() or {}).get("company_name") if r.status_code == 200 else None

    def test(self) -> TestResult:
        with self.ctx.http() as http:
            r = http.get("https://api.example.com/v1/me", headers=self.auth_headers())     # refreshes if needed
        if r.status_code == 401:
            return TestResult(False, "Example no longer accepts this connection. Connect again.")
        return TestResult(r.status_code == 200, "Connected." if r.status_code == 200 else f"HTTP {r.status_code}",
                          account=(r.json() or {}).get("company_name") if r.status_code == 200 else None)
