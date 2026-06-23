"""
HealingAgent: ReAct-style orchestration for text-in/text-out Gemini models.

Works with any Gemini model, including text-only ones (gemini-3.1-flash-lite, etc.)
that do not support native function calling.  The agent describes its tools in the
system prompt and outputs structured ACTION/ARGS markers which we parse and execute
via the MCP server subprocess.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai import types

from config.settings import settings

if TYPE_CHECKING:
    from src.target_db import DatabaseManager

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent

# ── System prompt ─────────────────────────────────────────────────────────────
# Written for text-out models: tools are described in plain text, not via the
# Gemini function-calling API.  The model outputs ACTION/ARGS markers that we
# parse and execute ourselves.

_SYSTEM_PROMPT = """You are a data pipeline repair engineer.
A JSON message failed schema validation and landed in the Dead Letter Queue.
Your task: write a Python function heal_payload(raw_json: dict) -> dict that
transforms the broken payload into a record matching the DuckDB events table.

════════════════════════════════════════════
AVAILABLE TOOLS
════════════════════════════════════════════

To call any tool output EXACTLY two lines (nothing else):
ACTION: <tool_name>
ARGS: {"arg": "value"}

For execute_heal_in_sandbox use this special block instead of ARGS:
ACTION: execute_heal_in_sandbox
PAYLOAD_JSON: <the failed payload as a single compact JSON line>
```python
def heal_payload(raw_json):
    return {
        "event_id":   ...,
        "user_id":    int(...),
        "amount":     float(...),
        "timestamp":  ...,
        "event_type": ...,
    }
```

Tool list:
• read_failed_message(message_id)         → returns the raw failed payload JSON
• inspect_table_schema(table_name)        → returns DuckDB column names + types
• read_api_docs(version)                  → returns upstream API changelog markdown
• execute_heal_in_sandbox(code, payload_json) → runs heal_payload safely, returns {"success": bool, "result": ..., "error": ...}

════════════════════════════════════════════
REQUIRED WORKFLOW
════════════════════════════════════════════
Step 1 — Read the failed message.
Step 2 — Inspect the events table schema.
Step 3 — Read API docs with version="v2" to understand what changed.
Step 4 — Write heal_payload and test it with execute_heal_in_sandbox.
Step 5 — If success=false, fix and retry.
Step 6 — Once success=true, output ONLY:

FINAL:
```python
def heal_payload(raw_json):
    return {...}
```

TARGET SCHEMA (events table):
  event_id   VARCHAR   (string)
  user_id    INTEGER   (must cast to int)
  amount     DOUBLE    (must cast to float)
  timestamp  TIMESTAMP (ISO-8601 string)
  event_type VARCHAR   (string)
"""


# ── ReAct response parser ─────────────────────────────────────────────────────

def _parse_action(text: str) -> tuple[str | None, dict[str, Any]]:
    """
    Extract (tool_name, tool_args) from a ReAct-formatted model response.

    Handles two forms:
    1. Standard:  ACTION: tool_name\\nARGS: {...}
    2. Sandbox:   ACTION: execute_heal_in_sandbox\\nPAYLOAD_JSON: ...\\n```python\\n...```
    """
    action_match = re.search(r"ACTION:\s*(\w+)", text)
    if not action_match:
        return None, {}

    tool_name = action_match.group(1).strip()

    if tool_name == "execute_heal_in_sandbox":
        payload_match = re.search(r"PAYLOAD_JSON:\s*(.+?)(?:\n|$)", text)
        code_match = re.search(r"```python\n(def heal_payload.*?)```", text, re.DOTALL)
        return tool_name, {
            "code": code_match.group(1).strip() if code_match else "",
            "payload_json": payload_match.group(1).strip() if payload_match else "{}",
        }

    # Standard ARGS JSON — allow multi-line values
    args_match = re.search(r"ARGS:\s*(\{.*?\})\s*(?:\n|$)", text, re.DOTALL)
    if args_match:
        try:
            return tool_name, json.loads(args_match.group(1))
        except json.JSONDecodeError:
            pass

    return tool_name, {}


def _extract_final_code(text: str) -> str | None:
    """Extract code from a FINAL: block or any heal_payload code block."""
    # Explicit FINAL: marker (preferred)
    final_match = re.search(r"FINAL:\s*```python\n(.*?)```", text, re.DOTALL)
    if final_match:
        return final_match.group(1).strip()

    # Fallback: any ```python block that defines heal_payload
    code_match = re.search(r"```python\n(def heal_payload.*?)```", text, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()

    return None


# ── HealingAgent ──────────────────────────────────────────────────────────────

class HealingAgent:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db = db_manager
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def heal(self, message_id: str, raw_payload: dict[str, Any]) -> bool:
        """
        Run the full ReAct repair loop for a failed DLQ message.

        Returns True if the message was successfully healed and inserted into
        DuckDB; False otherwise.
        """
        if not settings.gemini_api_key:
            logger.error("GEMINI_API_KEY is not configured — cannot run healing agent.")
            return False

        from fastmcp import Client

        server_script = str(_PROJECT_ROOT / "src" / "mcp_server.py")
        logger.info("Spawning MCP server subprocess: %s", server_script)

        async with Client(server_script) as mcp_client:
            generate_config = types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.1,
            )

            # Conversation history as alternating user/model text turns
            contents: list[types.Content] = [
                types.Content(
                    role="user",
                    parts=[types.Part(text=f"Repair failed message with id: {message_id}")],
                )
            ]

            final_code: str | None = None
            max_rounds = 14

            for round_num in range(max_rounds):
                logger.info("Agent round %d/%d", round_num + 1, max_rounds)

                response = await self._client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=contents,
                    config=generate_config,
                )

                response_text = (response.text or "").strip()
                logger.debug("Model output: %s", response_text[:400])

                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part(text=response_text)],
                    )
                )

                # ── 1. Check for FINAL answer ─────────────────────────────
                final_code = _extract_final_code(response_text)
                if final_code:
                    logger.info("Agent produced final code block (%d chars).", len(final_code))
                    break

                # ── 2. Parse and execute tool call ────────────────────────
                tool_name, tool_args = _parse_action(response_text)

                if not tool_name:
                    logger.warning(
                        "No ACTION found in round %d — response: %s",
                        round_num + 1, response_text[:200],
                    )
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=(
                                "I did not see a valid ACTION in your response. "
                                "Please output an ACTION line followed by ARGS, "
                                "or output FINAL: with your code block."
                            ))],
                        )
                    )
                    continue

                logger.info("  → tool: %s | args: %s", tool_name, list(tool_args.keys()))

                try:
                    raw_result = await mcp_client.call_tool(tool_name, tool_args)
                    result_text = raw_result.content[0].text if raw_result.content else "{}"
                except Exception as exc:
                    result_text = json.dumps({"error": str(exc)})
                    logger.warning("  ✗ Tool %s failed: %s", tool_name, exc)

                logger.debug("  ← result: %s", result_text[:300])

                # ── 3. Short-circuit on sandbox success ───────────────────
                if tool_name == "execute_heal_in_sandbox":
                    try:
                        outcome = json.loads(result_text)
                        if outcome.get("success"):
                            captured = tool_args.get("code", "").strip()
                            if captured:
                                final_code = captured
                                logger.info(
                                    "Sandbox validated — code captured from args (%d chars).",
                                    len(final_code),
                                )
                                break
                    except json.JSONDecodeError:
                        pass

                # Feed result back as a user turn
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"TOOL_RESULT:\n{result_text}")],
                    )
                )

            # ── End of loop ───────────────────────────────────────────────
            if final_code is None:
                logger.error("Healing agent did not converge after %d rounds.", max_rounds)
                return False

            return await self._apply_patch(
                message_id=message_id,
                raw_payload=raw_payload,
                code=final_code,
            )

    async def _apply_patch(
        self,
        message_id: str,
        raw_payload: dict[str, Any],
        code: str,
    ) -> bool:
        from src.sandbox import run_in_sandbox

        outcome = run_in_sandbox(code, raw_payload)
        if not outcome["success"]:
            logger.error("Final sandbox validation failed: %s", outcome["error"])
            self._db.write_audit(
                failed_message_id=message_id,
                event_id=str(raw_payload.get("event_id", "unknown")),
                original=raw_payload,
                healed={},
                code=code,
                status="sandbox_failed",
            )
            return False

        healed = outcome["result"]
        event_id = str(healed.get("event_id", "unknown"))

        try:
            self._db.insert_event(healed)
        except Exception as exc:
            logger.error("Failed to insert healed payload into DuckDB: %s", exc)
            self._db.write_audit(
                failed_message_id=message_id,
                event_id=event_id,
                original=raw_payload,
                healed=healed,
                code=code,
                status="insert_failed",
            )
            return False

        self._db.write_audit(
            failed_message_id=message_id,
            event_id=event_id,
            original=raw_payload,
            healed=healed,
            code=code,
            status="healed",
        )
        self._db.mark_failed_message_healed(message_id)
        logger.info("✓ event_id=%s healed and committed to DuckDB.", event_id)
        return True
