# TradeComply CA

An MCP server that gives AI agents five California construction compliance tools.

| Tool | What it does |
|---|---|
| `verify_contractor_license` | CSLB license status, classifications, expiration, workers' comp, bond |
| `estimate_permit_fees` | Rough permit fee estimate by city (placeholder rates) |
| `lookup_safety_standard` | Cal/OSHA Title 8 rule for a hazard, federal reference, PPE |
| `calculate_lien_deadline` | Preliminary notice, lien recording and foreclosure deadlines, days left, risk |
| `check_subcontractor_compliance` | License + class + insurance + workers' comp + bond, risk rating |

**Not legal advice.** Every result has `verified: false` until the data in `tradecomply/rules.py` is checked.

## Run

```bash
pip install -r requirements.txt
python server.py                          # stdio (Claude Desktop / Claude Code)
MCP_TRANSPORT=http python server.py       # http://localhost:8000/mcp
uvicorn app.main:app --port 8000          # same, for hosts that run uvicorn (Render)
python -m pytest -q tests                 # 15 tests
```

Add to Claude Code: `claude mcp add tradecomply -- python /path/to/server.py`

## Before anyone relies on it (verification checklist)

Each item: check the source, update the value, set `verified: True` and `checked_on`.

1. **Lien law** (`CA_LIEN`): Civ. Code §8204, §8412, §8414, §8182, §8460.
2. **Cal/OSHA table** (`SAFETY_TABLE`): each Title 8 section, the 7.5 ft fall trigger, summaries.
3. **Penalties**: federal 29 CFR 1903.15(d) (January each year); add Cal/OSHA maximums.
4. **CSLB** (`CSLB`): bond amount (§7071.6), workers' comp rule (§7125).
5. **License data**: load CSLB's public data as a CSV (`LICENSE_CSV=...`, columns in `tradecomply/licenses.py`).
   Don't scrape the CheckLicense page.
6. **Permit fees**: replace placeholder schedules with real ones per city, priced by project type.

## Layout

- `server.py`: MCP tools (thin)
- `tradecomply/logic.py`: all rules, pure and tested
- `tradecomply/rules.py`: every regulatory number and source, in one file to review
- `tradecomply/licenses.py`: license data sources (demo fixtures, CSV)
