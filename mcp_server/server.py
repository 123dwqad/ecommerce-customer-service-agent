"""Authenticated commerce MCP boundary; actors are never supplied by the model."""
import os
import logging
from fastmcp import FastMCP
from app.commerce.config import Settings
from app.commerce.knowledge import Knowledge
from app.commerce.service import BusinessError, CommerceService
from app.commerce.store import Store
from app.commerce.tools import verify_request

settings = Settings()
store = Store(settings.database_url)
service = CommerceService(store, Knowledge(settings))
mcp = FastMCP("Commerce Tools MCP")

@mcp.tool
def commerce_dispatch(envelope: dict) -> dict:
    """Execute a schema-validated tool request signed by the authenticated API."""
    payload = verify_request(settings.signing_key, envelope)
    actor = store.actor_from_session(payload["session_hash"])
    logging.getLogger(__name__).info("request_id=%s tool=%s", payload.get("request_id"), payload["name"])
    try:
        return service.invoke(payload["name"], payload["arguments"], *actor)
    except BusinessError as exc:
        return {"error": exc.code, "message": str(exc)}

if __name__ == "__main__":
    if len(settings.signing_key) < 32:
        raise RuntimeError("Set COMMERCE_SIGNING_KEY to at least 32 random characters in API and MCP environments")
    store.initialize()
    mcp.run(transport="sse", host="0.0.0.0", port=int(os.getenv("COMMERCE_MCP_PORT", "8004")))
