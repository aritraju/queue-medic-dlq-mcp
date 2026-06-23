"""
MCP Server exposing four tools to the HealingAgent.

Run standalone (stdio transport):
    python -m src.mcp_server

Design note: this process runs as a subprocess of the main FastAPI app which
holds a DuckDB write lock.  To avoid cross-process lock conflicts we read
failed payloads from JSON sidecar files (written by store_failed_message) and
return the events schema as a static definition rather than querying DuckDB.
"""
import json
import logging
import sys
from pathlib import Path

from fastmcp import FastMCP

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.settings import settings  # noqa: E402

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="queue-medic-tools",
    instructions=(
        "Tools for inspecting a failed DLQ payload, the DuckDB target schema, "
        "upstream API documentation, and executing sandbox code transformations. "
        "Use them in order: read payload → inspect schema → read docs → test code."
    ),
)

# ── Tool A: Retrieve the raw failed payload ───────────────────────────────────

@mcp.tool()
def read_failed_message(message_id: str) -> str:
    """
    Retrieve the raw JSON payload of a failed message from the DLQ store.

    Args:
        message_id: UUID assigned to the failed message when it entered the DLQ.

    Returns:
        JSON string of the original malformed payload, or an error object.
    """
    sidecar = (
        Path(_PROJECT_ROOT)
        / Path(settings.duckdb_path).parent
        / "failed_payloads"
        / f"{message_id}.json"
    )
    if not sidecar.exists():
        return json.dumps({"error": f"No failed message found with id={message_id}"})
    return sidecar.read_text(encoding="utf-8")


# ── Tool B: Inspect the DuckDB target schema ──────────────────────────────────

# Static schema avoids opening DuckDB (which the main process holds write-locked).
_EVENTS_SCHEMA = [
    {"name": "event_id",   "type": "VARCHAR",   "notnull": True,  "pk": True},
    {"name": "user_id",    "type": "INTEGER",   "notnull": True,  "pk": False},
    {"name": "amount",     "type": "DOUBLE",    "notnull": True,  "pk": False},
    {"name": "timestamp",  "type": "TIMESTAMP", "notnull": True,  "pk": False},
    {"name": "event_type", "type": "VARCHAR",   "notnull": True,  "pk": False},
]

@mcp.tool()
def inspect_table_schema(table_name: str = "events") -> str:
    """
    Return column definitions for a DuckDB table.

    Args:
        table_name: Target table (default: 'events').

    Returns:
        JSON array of column metadata: name, type, notnull, pk flag.
    """
    if table_name == "events":
        return json.dumps(_EVENTS_SCHEMA, indent=2)
    return json.dumps({"error": f"Unknown table: {table_name!r}"})


# ── Tool C: Read upstream API documentation ───────────────────────────────────

@mcp.tool()
def read_api_docs(version: str = "v2") -> str:
    """
    Read upstream API contract documentation to understand payload structure.

    Args:
        version: 'v1' for original schema, 'v2' for updated schema (the one causing drift).

    Returns:
        Markdown content of the requested API documentation file.
    """
    docs_path = _PROJECT_ROOT / settings.docs_dir / f"upstream_api_{version}.md"
    if not docs_path.exists():
        return f"ERROR: Documentation file not found: {docs_path}"
    return docs_path.read_text(encoding="utf-8")


# ── Tool D: Execute transformation code in a secure sandbox ───────────────────

@mcp.tool()
def execute_heal_in_sandbox(code: str, payload_json: str) -> str:
    """
    Execute a Python transformation function inside a RestrictedPython sandbox.

    The code must define ``heal_payload(raw_json: dict) -> dict``.
    Blocked: __import__, open, eval, exec, os, sys, and all filesystem access.

    Args:
        code: Python source defining heal_payload.
        payload_json: JSON string of the raw malformed payload to transform.

    Returns:
        JSON: {"success": bool, "result": dict | null, "error": str | null}
    """
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"success": False, "result": None, "error": f"Invalid JSON: {exc}"})

    from src.sandbox import run_in_sandbox

    outcome = run_in_sandbox(code, payload)
    return json.dumps(outcome)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run()
