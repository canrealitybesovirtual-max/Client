"""ASGI entry point for hosts that start the server with uvicorn (e.g. `uvicorn app.main:app`).
Serves the MCP endpoint at /mcp and a health check at /health."""
import logging
import os

from server import mcp
from tradecomply import cslb

logging.basicConfig(level=logging.INFO)
if os.getenv("CSLB_AUTO_DOWNLOAD") == "1":
    cslb.start_auto_refresh()   # builds real license data on boot (~3 min), refreshes daily

app = mcp.http_app()
