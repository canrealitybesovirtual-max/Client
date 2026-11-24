"""ASGI entry point for hosts that start the server with uvicorn (e.g. `uvicorn app.main:app`).
Serves the MCP endpoint at /mcp and a health check at /health."""
from server import mcp

app = mcp.http_app()
