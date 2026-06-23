"""
Unit tests for RestrictedPython sandbox isolation.

These tests run entirely offline — no Gemini API key or external service needed.
"""
from src.sandbox import run_in_sandbox

# ── Shared fixture ────────────────────────────────────────────────────────────

V2_PAYLOAD = {
    "event_id": "evt-abc-123",
    "user_id": "usr_42",
    "payload": {"amount": 199.99, "currency": "USD"},
    "ts": "2024-06-15T14:00:00",
    "event_type": "checkout",
}

VALID_HEAL_CODE = """\
def heal_payload(raw_json):
    return {
        "event_id": raw_json["event_id"],
        "user_id": int(raw_json["user_id"].replace("usr_", "")),
        "amount": raw_json["payload"]["amount"],
        "timestamp": raw_json["ts"],
        "event_type": raw_json["event_type"],
    }
"""

# ── Happy path ────────────────────────────────────────────────────────────────

def test_valid_heal_function_succeeds():
    result = run_in_sandbox(VALID_HEAL_CODE, V2_PAYLOAD)
    assert result["success"] is True
    assert result["error"] is None
    assert result["result"]["user_id"] == 42
    assert result["result"]["amount"] == 199.99
    assert result["result"]["timestamp"] == "2024-06-15T14:00:00"
    assert result["result"]["event_id"] == "evt-abc-123"


def test_returns_all_required_fields():
    result = run_in_sandbox(VALID_HEAL_CODE, V2_PAYLOAD)
    assert result["success"] is True
    expected_keys = {"event_id", "user_id", "amount", "timestamp", "event_type"}
    assert expected_keys.issubset(result["result"].keys())


# ── Security: dangerous imports blocked ───────────────────────────────────────

def test_sandbox_blocks_os_import():
    code = """\
def heal_payload(raw_json):
    import os
    return {"leak": os.getcwd()}
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False


def test_sandbox_blocks_sys_import():
    code = """\
def heal_payload(raw_json):
    import sys
    sys.exit(1)
    return {}
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False


def test_sandbox_blocks_open():
    code = """\
def heal_payload(raw_json):
    open("/etc/passwd").read()
    return {}
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False


def test_sandbox_blocks_eval():
    code = """\
def heal_payload(raw_json):
    eval("__import__('os').system('echo pwned')")
    return {}
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False


# ── Contract validation ───────────────────────────────────────────────────────

def test_missing_heal_payload_function():
    code = "x = 1 + 1"
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False
    assert "heal_payload" in result["error"]


def test_returns_non_dict_is_rejected():
    code = """\
def heal_payload(raw_json):
    return "this is not a dict"
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False
    assert "dict" in result["error"]


def test_returns_list_is_rejected():
    code = """\
def heal_payload(raw_json):
    return [1, 2, 3]
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False


def test_syntax_error_is_caught():
    code = "def heal_payload(x: this is invalid syntax {"
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False
    assert "SyntaxError" in result["error"]


def test_runtime_key_error_is_caught():
    code = """\
def heal_payload(raw_json):
    return {"amount": raw_json["nonexistent_key"]}
"""
    result = run_in_sandbox(code, V2_PAYLOAD)
    assert result["success"] is False
    assert "RuntimeError" in result["error"]


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_sandbox_does_not_mutate_input():
    original = dict(V2_PAYLOAD)
    run_in_sandbox(VALID_HEAL_CODE, V2_PAYLOAD)
    assert V2_PAYLOAD == original
