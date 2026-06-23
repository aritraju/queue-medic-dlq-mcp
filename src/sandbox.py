"""
RestrictedPython sandbox for executing LLM-generated transformation functions.

Blocks: __import__, open, eval, exec, globals, locals, compile, and all
OS-level builtins. Allows only pure data-manipulation operations.
"""
import builtins
import logging
from typing import Any

from RestrictedPython import compile_restricted, safe_builtins

logger = logging.getLogger(__name__)

_ALLOWED_EXTRA: tuple[str, ...] = (
    "int", "str", "float", "bool", "list", "dict", "tuple",
    "set", "frozenset", "len", "range", "enumerate", "zip",
    "map", "filter", "sorted", "reversed", "sum", "min", "max",
    "abs", "round", "isinstance", "issubclass", "repr",
    "ValueError", "KeyError", "TypeError", "IndexError",
    "AttributeError", "StopIteration",
)


def run_in_sandbox(code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Compile and execute *code* inside a RestrictedPython environment.

    The code must define a callable ``heal_payload(raw_json: dict) -> dict``.

    Returns one of:
    - ``{"success": True,  "result": <dict>, "error": None}``
    - ``{"success": False, "result": None,   "error": <str>}``
    """
    try:
        byte_code = compile_restricted(code, filename="<heal_payload>", mode="exec")
    except SyntaxError as exc:
        return {"success": False, "result": None, "error": f"SyntaxError: {exc}"}

    safe = dict(safe_builtins)
    for name in _ALLOWED_EXTRA:
        fn = getattr(builtins, name, None)
        if fn is not None:
            safe[name] = fn

    restricted_globals: dict[str, Any] = {
        "__builtins__": safe,
        "_getattr_": getattr,
        "_getitem_": lambda obj, key: obj[key],
        "_getiter_": iter,
        "_write_": lambda x: x,
        "_inplacevar_": lambda op, x, y: op(x, y),
    }
    local_vars: dict[str, Any] = {"raw_json": payload}

    try:
        exec(byte_code, restricted_globals, local_vars)  # noqa: S102
    except Exception as exc:
        return {"success": False, "result": None, "error": f"ExecutionError: {exc}"}

    fn = local_vars.get("heal_payload")
    if not callable(fn):
        return {
            "success": False,
            "result": None,
            "error": "Code must define a callable named 'heal_payload'.",
        }

    try:
        result = fn(payload)
    except Exception as exc:
        return {"success": False, "result": None, "error": f"RuntimeError in heal_payload: {exc}"}

    if not isinstance(result, dict):
        return {
            "success": False,
            "result": None,
            "error": f"heal_payload must return dict, got {type(result).__name__}.",
        }

    logger.debug("Sandbox execution succeeded: %s", list(result.keys()))
    return {"success": True, "result": result, "error": None}
