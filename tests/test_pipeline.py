"""
Pipeline integration tests using real DuckDB (in-memory) and mocked RabbitMQ messages.

No running RabbitMQ or Gemini API key required for these tests.
"""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

# Ensure settings can load without a real .env
os.environ.setdefault("GEMINI_API_KEY", "test-key-ci")

from src.rabbitmq_client import RabbitMQManager
from src.target_db import DatabaseManager

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(db_path=str(tmp_path / "test.duckdb"))
    manager.initialize()
    yield manager
    manager.close()


def _mock_message(payload: dict) -> AsyncMock:
    msg = AsyncMock()
    msg.body = json.dumps(payload).encode()
    return msg


# ── Schema validation (DuckDB layer) ─────────────────────────────────────────


def test_v1_payload_inserts_successfully(db):
    payload = {
        "event_id": "evt-v1-001",
        "user_id": 42,
        "amount": 99.99,
        "timestamp": "2024-06-15T10:30:00",
        "event_type": "purchase",
    }
    db.insert_event(payload)
    row = db.conn.execute(
        "SELECT event_id, user_id, amount FROM events WHERE event_id = 'evt-v1-001'"
    ).fetchone()
    assert row is not None
    assert row[1] == 42
    assert row[2] == pytest.approx(99.99)


def test_v2_payload_fails_on_direct_insert(db):
    v2_payload = {
        "event_id": "evt-v2-001",
        "user_id": "usr_42",                          # string — should fail int cast
        "payload": {"amount": 199.99, "currency": "USD"},
        "ts": "2024-06-15T10:30:00",
        "event_type": "checkout",
    }
    with pytest.raises(Exception):
        db.insert_event(v2_payload)


def test_missing_required_field_fails(db):
    with pytest.raises((KeyError, Exception)):
        db.insert_event({"event_id": "evt-incomplete"})


# ── RabbitMQ consumer logic (mocked messages) ─────────────────────────────────


@pytest.mark.asyncio
async def test_valid_payload_is_acked(db):
    payload = {
        "event_id": "evt-v1-002",
        "user_id": 7,
        "amount": 14.50,
        "timestamp": "2024-06-15T11:00:00",
        "event_type": "view",
    }
    mock_msg = _mock_message(payload)
    manager = RabbitMQManager(db_manager=db)

    await manager._handle_primary_message(mock_msg)

    mock_msg.ack.assert_called_once()
    mock_msg.nack.assert_not_called()
    row = db.conn.execute(
        "SELECT event_id FROM events WHERE event_id = 'evt-v1-002'"
    ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_v2_payload_is_nacked_and_not_inserted(db):
    payload = {
        "event_id": "evt-v2-002",
        "user_id": "usr_99",
        "payload": {"amount": 299.00, "currency": "EUR"},
        "ts": "2024-06-15T12:00:00",
        "event_type": "purchase",
    }
    mock_msg = _mock_message(payload)
    manager = RabbitMQManager(db_manager=db)

    # Patch out the healing agent so this test stays unit-level
    with patch("src.rabbitmq_client.RabbitMQManager._handle_dlq_message", new_callable=AsyncMock):
        await manager._handle_primary_message(mock_msg)

    mock_msg.nack.assert_called_once_with(requeue=False)
    mock_msg.ack.assert_not_called()
    row = db.conn.execute(
        "SELECT event_id FROM events WHERE event_id = 'evt-v2-002'"
    ).fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_malformed_json_does_not_crash_consumer(db):
    mock_msg = AsyncMock()
    mock_msg.body = b"{ this is not valid json }"
    manager = RabbitMQManager(db_manager=db)

    await manager._handle_primary_message(mock_msg)

    mock_msg.nack.assert_called_once_with(requeue=False)


# ── Failed message store ──────────────────────────────────────────────────────


def test_store_and_retrieve_failed_message(db):
    payload = {"event_id": "dlq-001", "user_id": "usr_1", "ts": "2024-01-01T00:00:00"}
    msg_id = db.store_failed_message(payload)

    retrieved = db.get_failed_message(msg_id)
    assert retrieved is not None
    assert retrieved["event_id"] == "dlq-001"


def test_get_nonexistent_failed_message_returns_none(db):
    assert db.get_failed_message("nonexistent-uuid") is None


def test_mark_failed_message_healed(db):
    payload = {"event_id": "dlq-002"}
    msg_id = db.store_failed_message(payload)
    db.mark_failed_message_healed(msg_id)

    row = db.conn.execute(
        "SELECT status FROM failed_messages WHERE id = ?", [msg_id]
    ).fetchone()
    assert row[0] == "healed"


# ── Audit log ─────────────────────────────────────────────────────────────────


def test_audit_entry_is_written(db):
    db.write_audit(
        failed_message_id="fm-001",
        event_id="evt-healed-001",
        original={"user_id": "usr_1"},
        healed={"user_id": 1},
        code="def heal_payload(x): return x",
        status="healed",
    )
    row = db.conn.execute(
        "SELECT event_id, status FROM healing_audit WHERE failed_message_id = 'fm-001'"
    ).fetchone()
    assert row is not None
    assert row[0] == "evt-healed-001"
    assert row[1] == "healed"


# ── Schema introspection ──────────────────────────────────────────────────────


def test_inspect_events_table_schema(db):
    schema = db.get_table_schema("events")
    col_names = [col["name"] for col in schema]
    assert "event_id" in col_names
    assert "user_id" in col_names
    assert "amount" in col_names
    assert "timestamp" in col_names
    assert "event_type" in col_names


def test_user_id_column_is_integer(db):
    schema = db.get_table_schema("events")
    user_id_col = next(c for c in schema if c["name"] == "user_id")
    assert user_id_col["type"] == "INTEGER"
