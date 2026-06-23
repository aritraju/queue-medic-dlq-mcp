"""
FastAPI server — ingestion gateway, SSE log stream, and dashboard.
"""
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from src.log_stream import log_broadcaster
from src.rabbitmq_client import RabbitMQManager
from src.target_db import DatabaseManager

logger = logging.getLogger(__name__)

_db: DatabaseManager | None = None
_rmq: RabbitMQManager | None = None

_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _rmq

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Attach the SSE broadcaster to the running event loop
    log_broadcaster.attach_loop(asyncio.get_running_loop())
    logging.getLogger().addHandler(log_broadcaster)

    _db = DatabaseManager()
    _db.initialize()

    _rmq = RabbitMQManager(db_manager=_db)
    await _rmq.connect()
    await _rmq.setup_topology()

    consumer_task = asyncio.create_task(_rmq.start_consumers())

    logger.info("Queue Medic is fully operational.")
    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    await _rmq.close()
    _db.close()
    logging.getLogger().removeHandler(log_broadcaster)


app = FastAPI(
    title="Queue Medic — Self-Healing DLQ API",
    description=(
        "Accepts event payloads, validates them against DuckDB schema, "
        "and autonomously heals malformed messages via Gemini 2.0 Flash + MCP."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


# ── SSE log stream ────────────────────────────────────────────────────────────

@app.get("/stream/logs", include_in_schema=False)
async def stream_logs(request: Request) -> EventSourceResponse:
    q = log_broadcaster.subscribe()

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield {"event": "log", "data": json.dumps(entry)}
                except TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            log_broadcaster.unsubscribe(q)

    return EventSourceResponse(generator())


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.get("/api/stats", tags=["ops"])
async def get_stats() -> JSONResponse:
    """Aggregate counts for the dashboard stat cards."""
    if _db is None:
        return JSONResponse({"events": 0, "healed": 0, "pending": 0, "failures": 0})

    events = _db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    healed = _db.conn.execute(
        "SELECT COUNT(*) FROM healing_audit WHERE status = 'healed'"
    ).fetchone()[0]
    pending = _db.conn.execute(
        "SELECT COUNT(*) FROM failed_messages WHERE status = 'pending'"
    ).fetchone()[0]
    failures = _db.conn.execute(
        "SELECT COUNT(*) FROM healing_audit WHERE status != 'healed'"
    ).fetchone()[0]

    return JSONResponse({"events": events, "healed": healed, "pending": pending, "failures": failures})


# ── Core API ──────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "queue-medic-dlq-api"})


@app.post("/ingest", status_code=202, tags=["events"])
async def ingest_event(payload: dict) -> JSONResponse:
    """
    Accept an event payload and publish it to the primary RabbitMQ queue.

    - Schema-compliant (v1) payloads insert directly into DuckDB.
    - Malformed (v2) payloads are routed to the DLQ and healed automatically.
    """
    if _rmq is None:
        raise HTTPException(status_code=503, detail="Message broker unavailable.")

    if "event_id" not in payload:
        payload["event_id"] = str(uuid.uuid4())

    await _rmq.publish(payload)
    logger.info("Accepted event_id=%s", payload["event_id"])
    return JSONResponse({"status": "accepted", "event_id": payload["event_id"]})


@app.get("/events", tags=["ops"])
async def list_events(limit: int = 30) -> JSONResponse:
    """Return the most recently ingested events from DuckDB."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database unavailable.")
    rows = _db.conn.execute(
        "SELECT event_id, user_id, amount, timestamp, event_type, ingested_at "
        "FROM events ORDER BY ingested_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    events = [
        {
            "event_id": r[0], "user_id": r[1], "amount": r[2],
            "timestamp": str(r[3]), "event_type": r[4], "ingested_at": str(r[5]),
        }
        for r in rows
    ]
    return JSONResponse({"count": len(events), "events": events})


@app.get("/audit", tags=["ops"])
async def list_audit_log(limit: int = 20) -> JSONResponse:
    """Return the most recent healing audit entries."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Database unavailable.")
    rows = _db.conn.execute(
        "SELECT id, failed_message_id, event_id, status, healed_at "
        "FROM healing_audit ORDER BY healed_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    entries = [
        {
            "id": r[0], "failed_message_id": r[1], "event_id": r[2],
            "status": r[3], "healed_at": str(r[4]),
        }
        for r in rows
    ]
    return JSONResponse({"count": len(entries), "entries": entries})
