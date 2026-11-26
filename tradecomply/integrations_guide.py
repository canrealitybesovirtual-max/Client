"""Integrations guide: which other products a California contractor can use with TradeComply, and how.

  integrations_for_task  plain-language task -> the catalog products that fit, what to use them for and with which
                         TradeComply tools, their MCP endpoints, government-cloud notes and caveats
  mcp_client_config      selected catalog ids -> ready-to-paste MCP client config (Claude Code .mcp.json, Claude
                         Desktop) joining TradeComply's MCP endpoint with those products' confirmed remote endpoints

Facts come from tradecomply/data/integrations.json: each entry was written from the vendor's official documentation
or code as read on the catalog's `checked_on` date, and anything that could not be confirmed is marked so. Both
tools only read that file; they never call a vendor or store anything.
"""
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from typing import Annotated, Optional

from pydantic import BaseModel, Field

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "integrations.json")
NOTE = ("Vendor facts were checked against official documentation on {date}; items marked unconfirmed were not. "
        "Vendor features, plans and availability change, so check the official link before you rely on one. "
        "TradeComply's own tools only read and check; they never change data in other systems.")
GOV_OK = ("yes", "partial", "not_applicable")
MAX_TASK = 2000
MAX_IDS = 25
KEY_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------- catalog access
@lru_cache(maxsize=1)
def load_catalog() -> dict:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def entries() -> list[dict]:
    return load_catalog()["entries"]


def _by_id() -> dict[str, dict]:
    return {e["id"]: e for e in entries()}


def get_entry(entry_id: str) -> dict:
    found = _by_id().get((entry_id or "").strip().lower())
    if found is None:
        raise KeyError(entry_id)
    return found


def _labels(kind: str) -> dict[str, str]:
    return {x["id"]: x["label"] for x in load_catalog()[kind]}


def usable_remote(server: dict) -> bool:
    """A server that can go straight into a client config: a fixed https URL read in the vendor's own docs or
    code, reached over HTTP (not a local stdio process)."""
    return bool(server.get("url") and server.get("confirmed") and not server.get("command")
                and server["url"].startswith("https://"))


def config_ready_ids() -> list[str]:
    """Entry ids that contribute at least one server to mcp_client_config by default."""
    return [e["id"] for e in entries() if e.get("recommended", True)
            and any(usable_remote(s) and s.get("default", True) for s in e["mcp"]["servers"])]


# ---------------------------------------------------------------------------- search
_STOP = set("a an and are as at be but by can do for from get has have how i if in into is it its me my need of on "
            "or our so than that the their them then there these this to up us use using want we what when where "
            "which who will with without would you your tradecomply help like make sure all any".split())


def _stem(w: str) -> str:
    """Tiny suffix stripper so 'certificates', 'certificate' and 'scheduled', 'schedule' meet."""
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("s") and len(w) > 3 and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ing") and len(w) >= 6:
        w = w[:-3]
    elif w.endswith("ed") and len(w) >= 5:
        w = w[:-2]
    if w.endswith("e") and len(w) >= 5:
        w = w[:-1]
    return w


def _tokens(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"[a-z0-9]+(?:_[a-z0-9]+)*", (text or "").lower()):
        word = m.group(0)
        if "_" in word:
            out.append(word)                    # a whole tool name, e.g. bulk_check_subcontractors
        out += [_stem(p) for p in word.split("_") if p not in _STOP and len(p) > 1]
    return out


def _entry_text(e: dict) -> list[tuple[str, float]]:
    cats, kinds = _labels("categories"), _labels("integration_types")
    return [(" ".join(e["keywords"]), 3.0), (e["name"], 3.0), (e["vendor"], 2.0),
            (cats.get(e["category"], ""), 1.0), (kinds.get(e["integration"], ""), 0.5), (e["summary"], 1.0),
            (e["tradecomply_role"], 1.0), (" ".join(u["text"] for u in e["uses"]), 0.4),
            (" ".join(e["tools"]), 0.4), ((e.get("builtin") or {}).get("what_it_does", ""), 1.0)]


@lru_cache(maxsize=1)
def _index():
    docs = {}
    for e in entries():
        tf = Counter()
        for text, weight in _entry_text(e):
            for t in _tokens(text):
                tf[t] += weight
        docs[e["id"]] = tf
    df = Counter(t for tf in docs.values() for t in tf)
    avg = sum(sum(tf.values()) for tf in docs.values()) / max(1, len(docs))
    return docs, df, avg


def _score(entry_id: str, terms: list[str]) -> float:
    docs, df, avg = _index()
    tf, n = docs[entry_id], len(docs)
    dl = sum(tf.values())
    s = 0.0
    for t in set(terms):
        if t in tf:
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.6 + 0.4 * dl / avg))
    return s


def _relevant_uses(e: dict, terms: list[str], limit: int = 3) -> list[dict]:
    wanted = set(terms)
    ranked = sorted(((len(wanted & set(_tokens(u["text"]))), -i, u) for i, u in enumerate(e["uses"])),
                    key=lambda x: (x[0], x[1]), reverse=True)
    picked = [u for hits, _, u in ranked if hits][:limit]
    return picked or e["uses"][:2]


# ---------------------------------------------------------------------------- result models
class UseCase(BaseModel):
    text: str
    tradecomply_tools: list[str] = Field(default_factory=list)


class ServerInfo(BaseModel):
    key: str
    label: str
    url: Optional[str] = None
    url_template: Optional[str] = None
    endpoint_confirmed: bool
    transport: str
    auth_needed: bool
    auth: str
    command: Optional[str] = None
    notes: str = ""


class GovInfo(BaseModel):
    supported: str = Field(description="yes | partial | no | unconfirmed | not_applicable")
    label: str
    name: Optional[str] = None
    url: Optional[str] = None
    note: str = ""


class IntegrationMatch(BaseModel):
    id: str
    name: str
    vendor: str
    category: str
    integration: str = Field(description="builtin | ai_assistant | import_export | reference")
    integration_label: str
    summary: str
    how_it_fits: str
    built_in_feature: Optional[str] = None
    uses: list[UseCase]
    tradecomply_tools: list[str]
    mcp_servers: list[ServerInfo]
    official_url: str
    vendor_status: str
    vendor_status_note: str
    gov_cloud: Optional[GovInfo] = None
    caveats: list[str]
    score: float


class Excluded(BaseModel):
    id: str
    name: str
    reason: str


class IntegrationsForTaskResult(BaseModel):
    task: str
    gov_cloud_required: bool
    matches: list[IntegrationMatch]
    excluded_for_gov_cloud: list[Excluded] = Field(default_factory=list)
    next_steps: list[str]
    checked_on: str
    note: str


class ConfiguredServer(BaseModel):
    name: str
    entry_id: str
    product: str
    url: str
    auth_needed: bool
    auth: str
    env: list[str] = Field(default_factory=list)
    client_registration: Optional[str] = None
    notes: str = ""


class SkippedSelection(BaseModel):
    id: str
    name: Optional[str] = None
    reason: str
    url_template: Optional[str] = None
    command: Optional[str] = None


class EnvVar(BaseModel):
    name: str
    used_for: str


class McpClientConfigResult(BaseModel):
    claude_code_mcp_json: dict = Field(description="Paste into .mcp.json (Claude Code). ${VAR} values are read "
                                                   "from your environment.")
    claude_code_mcp_json_text: str
    claude_code_commands: list[str]
    claude_desktop_config: dict = Field(description="TradeComply for claude_desktop_config.json (via mcp-remote)")
    claude_desktop_connectors: list[dict] = Field(description="Add each in Claude: Settings > Connectors > Add "
                                                              "custom connector")
    servers: list[ConfiguredServer]
    env_vars: list[EnvVar]
    skipped: list[SkippedSelection]
    also_available: list[str] = Field(default_factory=list)
    notes: list[str]
    checked_on: str


# ---------------------------------------------------------------------------- integrations_for_task
def _server_info(s: dict) -> ServerInfo:
    return ServerInfo(key=s["key"], label=s["label"], url=s.get("url"), url_template=s.get("url_template"),
                      endpoint_confirmed=bool(s.get("confirmed")), transport=s["transport"],
                      auth_needed=bool(s.get("auth_needed", True)), auth=s.get("auth", ""),
                      command=s.get("command"), notes=s.get("notes", ""))


def _gov_info(g: dict | None) -> GovInfo | None:
    if not g:
        return None
    return GovInfo(supported=g["supported"], label=g.get("label") or g["supported"], name=g.get("name"),
                   url=g.get("url"), note=g.get("note", ""))


def _match(e: dict, terms: list[str], score: float) -> IntegrationMatch:
    kinds = _labels("integration_types")
    uses = _relevant_uses(e, terms)
    tools = []
    for u in uses:
        tools += [t for t in u["tools"] if t not in tools]
    return IntegrationMatch(
        id=e["id"], name=e["name"], vendor=e["vendor"], category=_labels("categories").get(e["category"], e["category"]),
        integration=e["integration"], integration_label=kinds.get(e["integration"], e["integration"]),
        summary=e["summary"], how_it_fits=e["tradecomply_role"],
        built_in_feature=(e.get("builtin") or {}).get("what_it_does"),
        uses=[UseCase(text=u["text"], tradecomply_tools=u["tools"]) for u in uses],
        tradecomply_tools=tools or e["tools"][:8], mcp_servers=[_server_info(s) for s in e["mcp"]["servers"]],
        official_url=e["official_url"], vendor_status=e["status"]["label"], vendor_status_note=e["status"]["note"],
        gov_cloud=_gov_info(e.get("gov")), caveats=e["caveats"][:3], score=round(score, 3))


def integrations_for_task(task: str, gov_cloud_required: bool = False, limit: int = 6) -> IntegrationsForTaskResult:
    task = (task or "").strip()
    if not task:
        raise ValueError("task must not be empty: describe what you want to do, e.g. 'send lien waivers for "
                         "signature' or 'check the subs in our Procore directory'")
    if len(task) > MAX_TASK:
        raise ValueError(f"task is too long (max {MAX_TASK} characters)")
    limit = max(1, min(int(limit), 15))
    terms = _tokens(task)
    scored = sorted(((s, e) for e in entries() if e.get("recommended", True)
                     for s in [_score(e["id"], terms)] if s > 0), key=lambda x: (-x[0], x[1]["name"]))
    if scored:                                   # drop the long tail: keep what scores near the best match
        top = scored[0][0]
        scored = [(s, e) for s, e in scored if s >= top * 0.3]
    excluded = []
    if gov_cloud_required:
        kept = []
        for s, e in scored:
            g = e.get("gov")
            if g and g["supported"] in GOV_OK:
                kept.append((s, e))
            else:
                excluded.append(Excluded(id=e["id"], name=e["name"], reason=(
                    g["note"] if g and g.get("note") else "No US government cloud version was found in our research.")))
        scored = kept
    matches = [_match(e, terms, s) for s, e in scored[:limit]]
    steps = []
    if any(m.integration == "builtin" for m in matches):
        steps.append("Built-in connections: open the Integrations page in the TradeComply web app and use the "
                     "Connect panel on that product's card (owner or office role).")
    ready = [m.id for m in matches if m.id in config_ready_ids()]
    if ready:
        steps.append("To use them in your AI assistant next to TradeComply, call mcp_client_config with ids "
                     f"{ready} for ready-to-paste settings.")
    if any(m.integration == "import_export" for m in matches):
        steps.append("Import/export products: export a CSV or schedule file from the product and upload it in "
                     "TradeComply (for example Subs > Import for a subcontractor list).")
    if not matches:
        cats = ", ".join(c["label"] for c in load_catalog()["categories"])
        steps.append("No catalog product matched. Try naming the product or the job (e-signature, text alerts, "
                     f"schedule import, single sign-on, Procore, Slack). Categories: {cats}.")
    if gov_cloud_required:
        steps.append("This filter checks the other product. TradeComply's own hosted service makes no FedRAMP "
                     "claim; confirm with TradeComply before using it with controlled unclassified information "
                     "(CUI).")
    cat = load_catalog()
    return IntegrationsForTaskResult(task=task, gov_cloud_required=gov_cloud_required, matches=matches,
                                     excluded_for_gov_cloud=excluded[:15], next_steps=steps,
                                     checked_on=cat["checked_on"], note=NOTE.format(date=cat["checked_on"]))


# ---------------------------------------------------------------------------- mcp_client_config
def _check_tradecomply_url(url: str | None, trusted: bool = False) -> str:
    """trusted: the URL is this server's own address (the web API passes it), so plain http on a LAN host is
    allowed; anything a caller types must be https (or http://localhost)."""
    default = load_catalog()["tradecomply"]["mcp_url"]
    url = (url or "").strip() or default
    local = re.match(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/", url)
    plain = trusted and re.match(r"^http://[A-Za-z0-9.\-\[\]:]+/", url)
    if len(url) > 500 or any(c.isspace() for c in url) or not (url.startswith("https://") or local or plain):
        raise ValueError("tradecomply_mcp_url must be an https:// URL (or http://localhost for a local install), "
                         "e.g. " + default)
    return url


def _server_name(entry_id: str, key: str, taken: set) -> str:
    prefix = entry_id.split("-")[0]
    name = key if key.startswith(prefix) else f"{prefix}-{key}"
    name = KEY_RE.sub("-", name.lower()).strip("-")[:60] or "server"
    base, n = name, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def _closest(wanted: str) -> list[str]:
    ids = list(_by_id())
    w = set(_tokens(wanted.replace("-", " ")))
    ranked = sorted(ids, key=lambda i: -len(w & set(_tokens(i.replace("-", " ")))))
    return [i for i in ranked if w & set(_tokens(i.replace("-", " ")))][:3]


def _skip_reason(e: dict, s: dict | None) -> SkippedSelection:
    if not e.get("recommended", True):
        return SkippedSelection(id=e["id"], name=e["name"], reason="Not recommended: " +
                                (e.get("not_recommended_reason") or "see the catalog entry."))
    if s is None:
        how = {"builtin": "It's built into TradeComply: connect it on the Integrations page.",
               "import_export": "Use it by exporting and importing files.",
               "reference": "It's reference material, not something to connect."}.get(e["integration"], "")
        note = e["mcp"].get("note") or "No MCP server is documented."
        return SkippedSelection(id=e["id"], name=e["name"], reason=f"No confirmed remote MCP endpoint. {note} {how}".strip())
    if s.get("command"):
        return SkippedSelection(id=f"{e['id']}/{s['key']}", name=s["label"], command=s["command"],
                                reason=("Runs on your own computer (stdio), so it isn't a remote endpoint. Install it "
                                        "with the vendor's command and your own credentials. " + s.get("notes", "")).strip())
    if s.get("url_template") and not s.get("url"):
        return SkippedSelection(id=f"{e['id']}/{s['key']}", name=s["label"], url_template=s["url_template"],
                                reason=("The address is specific to your account: fill in your values, then add it "
                                        "as an http server. " + s.get("notes", "")).strip())
    if not s.get("confirmed"):
        return SkippedSelection(id=f"{e['id']}/{s['key']}", name=s["label"], url_template=s.get("url"),
                                reason=("Endpoint not confirmed in the vendor's own documentation. " +
                                        s.get("notes", "")).strip())
    return SkippedSelection(id=f"{e['id']}/{s['key']}", name=s["label"], reason="No usable endpoint.")


ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _env_list(headers: dict) -> list[str]:
    return sorted({m for v in headers.values() for m in ENV_REF.findall(v)})


def _shell_vars(value: str) -> str:
    """${NAME} (.mcp.json expansion) -> $NAME (expanded by the shell when the command runs)."""
    return ENV_REF.sub(lambda m: "$" + m.group(1), value)


def mcp_client_config(ids: list[str], tradecomply_mcp_url: str | None = None, *,
                      trusted_url: bool = False) -> McpClientConfigResult:
    if not isinstance(ids, (list, tuple)):
        raise ValueError("ids must be a list of catalog ids, e.g. ['box', 'docusign']")
    if len(ids) > MAX_IDS:
        raise ValueError(f"select at most {MAX_IDS} ids")
    tc_url = _check_tradecomply_url(tradecomply_mcp_url, trusted_url)
    by_id = _by_id()
    picks: list[tuple[dict, dict]] = []
    skipped: list[SkippedSelection] = []
    also: list[str] = []
    seen = set()
    for raw in ids:
        wanted = str(raw or "").strip().lower()
        if not wanted or wanted in seen:
            continue
        seen.add(wanted)
        eid, _, skey = wanted.partition("/")
        e = by_id.get(eid)
        if e is None:
            near = _closest(eid)
            skipped.append(SkippedSelection(id=wanted, reason="Unknown catalog id" +
                                            (f"; did you mean {', '.join(near)}?" if near else ".")))
            continue
        if not e.get("recommended", True):
            skipped.append(_skip_reason(e, None))
            continue
        servers = e["mcp"]["servers"]
        if skey:
            s = next((x for x in servers if x["key"] == skey), None)
            if s is None:
                skipped.append(SkippedSelection(id=wanted, name=e["name"], reason=(
                    "Unknown server for this product; choose one of: " +
                    ", ".join(f"{e['id']}/{x['key']}" for x in servers)) if servers else "This product has no MCP server."))
            elif usable_remote(s):
                picks.append((e, s))
            else:
                skipped.append(_skip_reason(e, s))
            continue
        chosen = [s for s in servers if usable_remote(s) and s.get("default", True)]
        picks += [(e, s) for s in chosen]
        if not chosen:
            first = next((s for s in servers if s.get("default", True)), servers[0] if servers else None)
            skipped.append(_skip_reason(e, first))
        also += [f"{e['id']}/{s['key']} ({s['label']})" for s in servers if usable_remote(s) and not s.get("default", True)]
    picks = list({(e["id"], s["key"]): (e, s) for e, s in picks}.values())

    tc_headers = {"Authorization": "Bearer ${TRADECOMPLY_API_KEY}"}
    mcp_servers = {"tradecomply": {"type": "http", "url": tc_url, "headers": tc_headers}}
    commands = [f'claude mcp add --transport http tradecomply {tc_url} --header "Authorization: Bearer '
                f'$TRADECOMPLY_API_KEY"']
    env = {"TRADECOMPLY_API_KEY": "TradeComply API key (Settings > AI assistant (MCP) in the TradeComply web app)"}
    configured, connectors, taken = [], [], {"tradecomply"}
    for e, s in picks:
        name = _server_name(e["id"], s["key"], taken)
        item = {"type": "http", "url": s["url"]}
        if s.get("headers"):
            item["headers"] = dict(s["headers"])
        mcp_servers[name] = item
        hdr = "".join(f' --header "{k}: {_shell_vars(v)}"' for k, v in (s.get("headers") or {}).items())
        commands.append(f"claude mcp add --transport http {name} {s['url']}{hdr}")
        names = _env_list(s.get("headers") or {})
        for n in names:
            env.setdefault(n, f"{e['name']}: {s['label']}")
        note = s.get("notes", "")
        if s.get("client_registration") == "admin_issued":
            note = (note + " No dynamic client registration: get an OAuth client ID and secret from your admin "
                    "(register the redirect URI http://localhost:8080/callback) and add the server with "
                    f"claude mcp add --transport http --client-id <CLIENT_ID> --client-secret --callback-port 8080 "
                    f"{name} {s['url']}").strip()
        configured.append(ConfiguredServer(name=name, entry_id=e["id"], product=e["name"], url=s["url"],
                                           auth_needed=bool(s.get("auth_needed", True)), auth=s.get("auth", ""),
                                           env=names, client_registration=s.get("client_registration"), notes=note))
        several = sum(1 for x, _ in picks if x["id"] == e["id"]) > 1
        connectors.append({"name": f"{e['name']}: {s['label']}" if several else e["name"], "url": s["url"],
                           "sign_in": s.get("auth", ""), "headers": sorted((s.get("headers") or {}).keys())})
    mcp_json = {"mcpServers": mcp_servers}
    desktop = {"mcpServers": {"tradecomply": {
        "command": "npx", "args": ["-y", "mcp-remote", tc_url, "--header", "Authorization:${TC_AUTH}"],
        "env": {"TC_AUTH": "Bearer <YOUR_TRADECOMPLY_API_KEY>"}}}}
    notes = [
        "Keep API keys in environment variables or your system's secret store, never in a file you share or commit.",
        "Servers with auth_needed ask you to sign in the first time; in Claude Code run /mcp to sign in or check status.",
        "TradeComply's MCP tools are read-only. Other products' servers may be able to change data in those products; "
        "start with read-only access where the vendor offers it.",
        "Claude Desktop: paste claude_desktop_config into claude_desktop_config.json (Settings > Developer > Edit "
        "config; needs Node.js) and add the other products as custom connectors (Settings > Connectors > Add custom "
        "connector).",
    ]
    if not picks:
        notes.insert(0, "Only TradeComply was added: none of the selected products has a confirmed remote MCP endpoint.")
    return McpClientConfigResult(
        claude_code_mcp_json=mcp_json, claude_code_mcp_json_text=json.dumps(mcp_json, indent=2),
        claude_code_commands=commands, claude_desktop_config=desktop, claude_desktop_connectors=connectors,
        servers=configured, env_vars=[EnvVar(name=k, used_for=v) for k, v in env.items()], skipped=skipped,
        also_available=also, notes=notes, checked_on=load_catalog()["checked_on"])


# ---------------------------------------------------------------------------- MCP registration
def register(mcp) -> None:
    ro = {"readOnlyHint": True, "openWorldHint": False}
    ready = ", ".join(config_ready_ids())

    @mcp.tool(name="integrations_for_task", annotations=ro)
    def integrations_for_task_tool(
        task: Annotated[str, Field(description="What the contractor wants to do, in plain words, e.g. 'send home "
                                               "improvement contracts for signature', 'text crews when smoke is "
                                               "bad', 'check the subs in our Procore directory', 'import our P6 "
                                               "schedule'")],
        gov_cloud_required: Annotated[bool, Field(description="Only products with a US government cloud version "
                                                              "(or no vendor cloud involved, like file imports)")] = False,
        limit: Annotated[int, Field(ge=1, le=15, description="Maximum products to return")] = 6,
    ) -> IntegrationsForTaskResult:
        """Find the products in TradeComply's integrations catalog that fit a task, and how to use each with
        TradeComply: built in (connect on TradeComply's Integrations page), use alongside TradeComply in your AI
        assistant (via the product's MCP server), import/export files, or reference only.

        Each match has what the product is, how it fits, example uses naming the TradeComply tools to call, the
        product's MCP servers (url, whether the endpoint was confirmed in the vendor's docs, and the sign-in it
        needs), vendor status, a government-cloud note and the main caveats. With gov_cloud_required, products
        without a US government version are listed in excluded_for_gov_cloud with the reason. Tell the user which
        facts are unconfirmed. Use mcp_client_config to get paste-ready AI-assistant settings."""
        return integrations_for_task(task, gov_cloud_required, limit)

    @mcp.tool(name="mcp_client_config", annotations=ro)
    def mcp_client_config_tool(
        ids: Annotated[list[str], Field(min_length=1, max_length=MAX_IDS,
                                        description="Catalog ids from integrations_for_task, e.g. ['box', "
                                                    "'docusign']; 'id/server' picks one server, e.g. "
                                                    "'salesforce/sobject-all'. Ids with a confirmed remote "
                                                    f"endpoint: {ready}")],
        tradecomply_mcp_url: Annotated[Optional[str], Field(description="TradeComply MCP URL to use (default: the "
                                                                        "hosted service's /mcp)")] = None,
    ) -> McpClientConfigResult:
        """Ready-to-paste MCP client settings that put TradeComply's MCP server and the selected products' remote
        MCP servers in one AI assistant.

        Returns claude_code_mcp_json (and its text) for a Claude Code .mcp.json, matching `claude mcp add`
        commands, a Claude Desktop config for TradeComply plus the custom connectors to add for the others, the
        environment variables to set (API keys stay in ${VAR} references, never in the file), and per server
        whether sign-in is needed. Only fixed endpoints confirmed in the vendor's own docs are added; products whose
        address is specific to the customer's account, that run locally, or that have no confirmed endpoint are in
        `skipped` with the reason (and the address template or install command when there is one)."""
        return mcp_client_config(ids, tradecomply_mcp_url)
