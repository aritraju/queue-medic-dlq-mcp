# Queue Medic — Self-Healing Dead Letter Queue

> **An autonomous data pipeline repair system that detects broken messages, reasons about schema drift using a Gemini AI agent, and heals them — without human intervention.**

[![CI](https://github.com/aritraju/queue-medic-dlq-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/aritraju/queue-medic-dlq-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-3.13-orange)
![DuckDB](https://img.shields.io/badge/DuckDB-1.1-yellow)
![Gemini](https://img.shields.io/badge/Gemini-3.1%20Flash%20Lite-purple)
![Tests](https://img.shields.io/badge/Tests-24%20passing-brightgreen)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

---

## What This Is

Queue Medic is a **production-grade backend system** that autonomously repairs broken event payloads using an AI agent. It demonstrates deep integration of modern backend engineering patterns — message brokers, AI agents, secure sandboxes, and real-time streaming — in a single deployable application.

**The core scenario:** An upstream API changed its payload schema (v2 breaking change). Messages start failing validation and flooding a Dead Letter Queue. Instead of waking up an engineer at 3 AM, Queue Medic's AI agent automatically:

1. Reads the failed payload
2. Inspects the target database schema  
3. Reads the API changelog to understand what changed
4. Writes a Python transformation function
5. Tests it safely in a sandboxed environment
6. Inserts the healed record into DuckDB — with a full audit trail

**Zero human intervention. Seconds to complete. Every healed message fully audited.**

---

## Live Demo Output

Sending a broken v2 payload:

```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":   "evt-demo-001",
    "user_id":    "usr_202",
    "payload":    {"amount": 129.00, "currency": "USD"},
    "ts":         "2024-06-15T11:30:00Z",
    "event_type": "checkout"
  }'
```

Server log (real output from a live run):

```
19:15:58 | WARNING  | src.rabbitmq_client  | ✗ Schema validation failed (int() argument must be a string... not 'dict') — routing to DLQ.
19:15:58 | WARNING  | src.rabbitmq_client  | ⚕  DLQ received event_id=evt-demo-001 — starting repair loop.
19:15:58 | INFO     | src.healing_agent    | Spawning MCP server subprocess: .../src/mcp_server.py
19:15:59 | INFO     | src.healing_agent    | Agent round 1/14
19:15:59 | INFO     | src.healing_agent    |   → tool: read_failed_message | args: ['message_id']
19:16:00 | INFO     | src.healing_agent    | Agent round 2/14
19:16:01 | INFO     | src.healing_agent    |   → tool: inspect_table_schema | args: ['table_name']
19:16:02 | INFO     | src.healing_agent    | Agent round 3/14
19:16:03 | INFO     | src.healing_agent    |   → tool: read_api_docs | args: ['version']
19:16:05 | INFO     | src.healing_agent    | Agent round 4/14
19:16:06 | INFO     | src.healing_agent    |   → tool: execute_heal_in_sandbox | args: ['code', 'payload_json']
19:16:07 | INFO     | src.healing_agent    | Sandbox validated — code captured from args (187 chars).
19:16:07 | INFO     | src.healing_agent    | ✓ event_id=evt-demo-001 healed and committed to DuckDB.
19:16:07 | INFO     | src.rabbitmq_client  | ✓ Healing complete for event_id=evt-demo-001 — DLQ message ack'd.
```

Result — healed record in DuckDB:

```json
{
  "event_id":   "evt-demo-001",
  "user_id":    202,
  "amount":     129.0,
  "timestamp":  "2024-06-15 11:30:00",
  "event_type": "checkout"
}
```

The agent correctly mapped `"usr_202"` → `202` (int), extracted `payload.amount` to top-level, and renamed `ts` → `timestamp` — all without any hardcoded rules.

---

## Architecture

```mermaid
flowchart TD
    A["POST /ingest\nFastAPI Gateway"] -->|publish| B["RabbitMQ\nevents_queue\n(AMQP)"]

    B --> C{"Primary Consumer\nSchema Validation"}

    C -->|"valid payload\nack ✓"| D[("DuckDB\nevents table")]

    C -->|"invalid payload\nnack — requeue=False"| E["RabbitMQ DLX\n(Dead Letter Exchange)"]

    E --> F["dead_letter_queue"]

    F --> G["DLQ Consumer\ndetects failure"]

    G --> H["HealingAgent\nGemini 3.1 Flash Lite\n(ReAct)"]

    H -->|"Tool A: read_failed_message"| I[("JSON sidecar\nfailed_payloads/")]
    H -->|"Tool B: inspect_table_schema"| D
    H -->|"Tool C: read_api_docs v2"| J["docs/\nupstream_api_v2.md"]
    H -->|"Tool D: execute_heal_in_sandbox"| K["RestrictedPython\nSandbox"]

    K -->|"success=True"| H

    H -->|"healed payload\ninsert"| D
    H -->|"ack ✓"| F
    H -->|"audit record"| L[("DuckDB\nhealing_audit")]

    style A fill:#4f8ef7,color:#fff
    style H fill:#8b5cf6,color:#fff
    style K fill:#f59e0b,color:#fff
    style D fill:#10b981,color:#fff
    style F fill:#ef4444,color:#fff
```

### Data Flow

| Stage | What Happens |
|---|---|
| **Ingest** | FastAPI accepts any JSON payload, assigns a UUID if missing, publishes to RabbitMQ |
| **Validate** | Primary consumer attempts a typed insert into DuckDB; invalid types trigger an immediate `nack(requeue=False)` |
| **DLX routing** | RabbitMQ's native Dead Letter Exchange routes the nacked message to `dead_letter_queue` — no custom routing code |
| **Store** | DLQ consumer writes the raw payload to `failed_messages` table + a JSON sidecar file |
| **Heal** | HealingAgent spawns an MCP subprocess, runs a ReAct loop with Gemini, validates code in sandbox |
| **Commit** | Healed record is inserted into `events`, an audit entry is written, the DLQ message is ack'd |

---

## Technology Stack

| Layer | Technology | Why |
|---|---|---|
| **API Gateway** | FastAPI 0.115 | Async-native, automatic OpenAPI docs, SSE streaming support |
| **Message Broker** | RabbitMQ 3.13 | Battle-tested DLX/DLQ routing via `x-dead-letter-exchange` |
| **Async AMQP** | `aio-pika` | Fully async — no event-loop blocking on AMQP I/O |
| **Target Store** | DuckDB 1.1 | Zero-config columnar warehouse; simulates BigQuery/Redshift locally |
| **AI Agent** | Gemini 3.1 Flash Lite | Free tier, fast reasoning, ReAct-style tool orchestration |
| **LLM SDK** | `google-genai` | Official SDK — no LangChain overhead |
| **Agent Protocol** | FastMCP 2.x | Stdio subprocess transport (same protocol Claude Desktop uses) |
| **Code Sandbox** | RestrictedPython | AST-level security; blocks `import`, `open`, `eval`, `exec` before `exec()` |
| **Config** | `pydantic-settings` | Type-safe `.env` loading, zero hardcoded credentials |
| **Package Manager** | `uv` | 10–100× faster than pip/Poetry; deterministic lockfile |
| **CI** | GitHub Actions | Lint + 24 tests on every push; no external services needed |

---

## Key Engineering Decisions

### 1. ReAct Prompting Instead of Native Function Calling

Gemini 3.1 Flash Lite is a text-in/text-out model with no native function-calling support. Rather than switching to a heavier model, the agent uses **ReAct** (Reason + Act) prompting: tools are described in the system prompt as plain text with `ACTION: <tool>` / `ARGS: {...}` output markers. The agent parses these with regex and dispatches to the MCP server. This means:

- Any LLM works — model upgrades don't require code changes
- Full observability: every tool call is visible in plaintext logs  
- Deterministic at temperature=0.1 — the transformation function is consistent

### 2. JSON Sidecar Files to Solve DuckDB Cross-Process Locking

DuckDB allows only **one writer process** at a time. The FastAPI app holds the write lock. The MCP server runs as a subprocess — if it opened a second DuckDB connection, it would deadlock.

The solution: `store_failed_message()` writes `data/failed_payloads/{id}.json` alongside the DuckDB insert. The MCP subprocess reads this file directly — no shared connection, no locks, no cross-process state.

```
FastAPI process  ──writes──▶  data/failed_payloads/abc.json
MCP subprocess   ──reads──▶   data/failed_payloads/abc.json   (no DuckDB needed)
```

### 3. RestrictedPython for LLM Code Execution Safety

LLM-generated code is never `exec()`'d directly. RestrictedPython **compiles** code through a security-auditing AST pass first. Blocked at the bytecode level:

- `__import__` (all imports)
- `open` (filesystem access)
- `eval`, `exec`, `compile`
- `globals`, `locals`, `__builtins__`

Only pure data-transformation builtins are whitelisted (`int`, `str`, `float`, `dict`, `list`, `sorted`, etc.). The test suite verifies `import os`, `open("/etc/passwd")`, and `eval()` all fail inside the sandbox.

### 4. Model Context Protocol (MCP) for Agent Tooling

The four agent tools (`read_failed_message`, `inspect_table_schema`, `read_api_docs`, `execute_heal_in_sandbox`) are exposed via **FastMCP** on stdio transport — the same protocol Claude Desktop uses. The healing agent is a standard MCP client (`fastmcp.Client`). Benefits:

- Tools are completely decoupled from the LLM provider
- The MCP server can be tested, replaced, or extended independently
- Any MCP-compatible client can drive the same tool set

### 5. SSE Log Streaming

A custom `LogBroadcaster` logging handler pushes every log record to an asyncio queue. The dashboard's `/stream/logs` endpoint reads from this queue via Server-Sent Events — real-time log tailing in the browser with zero polling overhead.

---

## Project Structure

```
queue-medic-dlq-mcp/
├── .github/
│   └── workflows/ci.yml          # GitHub Actions: ruff lint + 24 tests
├── config/
│   └── settings.py               # Pydantic Settings — typed .env loading
├── docs/
│   ├── upstream_api_v1.md        # Original flat schema (v1 contract)
│   └── upstream_api_v2.md        # Breaking changes: nested amount, prefixed user_id, ts rename
├── docker/
│   └── docker-compose.yml        # RabbitMQ 3.13 + management UI
├── scripts/
│   └── seed_failure.sh           # One-command end-to-end demo
├── src/
│   ├── main.py                   # FastAPI app: /ingest, /events, /audit, /api/stats, SSE
│   ├── rabbitmq_client.py        # AMQP publisher + primary consumer + DLQ consumer
│   ├── target_db.py              # DuckDB init, insert, failed message store, audit log
│   ├── mcp_server.py             # FastMCP server: 4 tools over stdio transport
│   ├── healing_agent.py          # Gemini ReAct loop + MCP client orchestration
│   ├── sandbox.py                # RestrictedPython execution environment
│   ├── log_stream.py             # SSE broadcaster (asyncio queue + logging handler)
│   └── dashboard.html            # Real-time monitoring dashboard (vanilla JS + SSE)
└── tests/
    ├── test_healing.py           # 12 sandbox tests: happy path + security isolation
    └── test_pipeline.py          # 12 pipeline tests: real DuckDB, mocked AMQP messages
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker Desktop

### 1. Clone and install

```bash
git clone https://github.com/aritraju/queue-medic-dlq-mcp.git
cd queue-medic-dlq-mcp
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your Gemini API key:

```env
GEMINI_API_KEY=your_key_here   # free key: https://aistudio.google.com/app/apikey
```

### 3. Start RabbitMQ

```bash
docker compose -f docker/docker-compose.yml up -d
# Management UI: http://localhost:15672  (guest / guest)
```

### 4. Run the tests

All 24 tests pass with no external services and no API key required:

```bash
uv run pytest tests/ -v
```

Expected output:

```
tests/test_healing.py::test_valid_heal_function_succeeds          PASSED
tests/test_healing.py::test_returns_all_required_fields           PASSED
tests/test_healing.py::test_sandbox_blocks_os_import              PASSED
tests/test_healing.py::test_sandbox_blocks_sys_import             PASSED
tests/test_healing.py::test_sandbox_blocks_open                   PASSED
tests/test_healing.py::test_sandbox_blocks_eval                   PASSED
tests/test_healing.py::test_missing_heal_payload_function         PASSED
tests/test_healing.py::test_returns_non_dict_is_rejected          PASSED
tests/test_healing.py::test_returns_list_is_rejected              PASSED
tests/test_healing.py::test_syntax_error_is_caught                PASSED
tests/test_healing.py::test_runtime_key_error_is_caught           PASSED
tests/test_healing.py::test_sandbox_does_not_mutate_input         PASSED
tests/test_pipeline.py::test_v1_payload_inserts_successfully      PASSED
tests/test_pipeline.py::test_v2_payload_fails_on_direct_insert    PASSED
...
24 passed in 1.23s
```

### 5. Start the API server

```bash
uv run uvicorn src.main:app --reload --port 8000
```

- **Dashboard:** [http://localhost:8000](http://localhost:8000) — real-time log streaming, live stats
- **API docs:** [http://localhost:8000/docs](http://localhost:8000/docs) — Swagger UI

---

## End-to-End Walkthrough

Run the full demo in one command (requires RabbitMQ + server running):

```bash
bash scripts/seed_failure.sh
```

Or step by step:

### Step 1 — Valid v1 payload (happy path)

```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":   "evt-v1-001",
    "user_id":    101,
    "amount":     49.95,
    "timestamp":  "2024-06-15T10:00:00Z",
    "event_type": "purchase"
  }'
# → {"status":"accepted","event_id":"evt-v1-001"}
```

Inserts directly into DuckDB within milliseconds:

```
10:00:01 | INFO | src.rabbitmq_client | ✓ Inserted event_id=evt-v1-001 into DuckDB.
```

### Step 2 — Broken v2 payload (triggers AI healing)

The upstream API changed: `user_id` is now a prefixed string, `amount` is nested inside `payload`, and `timestamp` was renamed to `ts`.

```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":   "evt-v2-001",
    "user_id":    "usr_202",
    "payload":    {"amount": 129.00, "currency": "USD"},
    "ts":         "2024-06-15T11:30:00Z",
    "event_type": "checkout"
  }'
# → {"status":"accepted","event_id":"evt-v2-001"}
```

### Step 3 — Verify both records in DuckDB

```bash
curl http://localhost:8000/events?limit=5
```

```json
{
  "count": 2,
  "events": [
    {
      "event_id":   "evt-v2-001",
      "user_id":    202,
      "amount":     129.0,
      "timestamp":  "2024-06-15 11:30:00",
      "event_type": "checkout",
      "ingested_at": "2024-06-15 10:00:13"
    },
    {
      "event_id":   "evt-v1-001",
      "user_id":    101,
      "amount":     49.95,
      "timestamp":  "2024-06-15 10:00:00",
      "event_type": "purchase",
      "ingested_at": "2024-06-15 10:00:01"
    }
  ]
}
```

### Step 4 — Inspect the healing audit log

```bash
curl http://localhost:8000/audit?limit=5
```

```json
{
  "count": 1,
  "entries": [
    {
      "id":                 "19a8d634-...",
      "failed_message_id":  "bbd3def7-...",
      "event_id":           "evt-v2-001",
      "status":             "healed",
      "healed_at":          "2024-06-15 10:00:13"
    }
  ]
}
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/ingest` | Accept an event payload, publish to RabbitMQ |
| `GET` | `/events` | List recently inserted events (`?limit=N`, default 30) |
| `GET` | `/audit` | List recent healing audit entries (`?limit=N`, default 20) |
| `GET` | `/api/stats` | Aggregate counts: total events, healed, pending, failures |
| `GET` | `/stream/logs` | SSE log stream (consumed by dashboard) |
| `GET` | `/health` | Service health check |
| `GET` | `/docs` | Auto-generated Swagger UI |

---

## The Schema Drift Problem (v1 → v2 Breaking Changes)

The built-in demo scenario simulates a real-world upstream API breaking change:

| Field | v1 (original) | v2 (breaking) | Fix |
|---|---|---|---|
| `user_id` | `42` (integer) | `"usr_42"` (prefixed string) | Strip `"usr_"`, cast to `int` |
| `amount` | `129.00` (top-level) | `{"payload": {"amount": 129.00}}` (nested) | Extract `payload.amount` |
| `timestamp` | `"2024-06-15T10:00:00Z"` | field renamed to `ts` | Map `ts` → `timestamp` |

The AI agent discovers all three drifts autonomously by reading the API docs — no hardcoded transformation rules anywhere in the codebase.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Google AI Studio API key — get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey) |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Gemini model identifier |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:5672/` | AMQP connection string |
| `PRIMARY_EXCHANGE` | `events_exchange` | Primary RabbitMQ exchange name |
| `DLX_EXCHANGE` | `dlx_exchange` | Dead-letter exchange name |
| `PRIMARY_QUEUE` | `events_queue` | Main processing queue |
| `DEAD_LETTER_QUEUE` | `dead_letter_queue` | DLQ name |
| `DUCKDB_PATH` | `data/events.duckdb` | DuckDB file path (relative to project root) |
| `DOCS_DIR` | `docs` | API documentation directory |

---

## Security Model

- **No LLM code runs without sandbox validation.** All AI-generated code passes through RestrictedPython's AST compilation before any `exec()` call.
- **Secrets in `.env` only.** `pydantic-settings` loads credentials from the environment; no values are hardcoded anywhere. `.env` is gitignored.
- **Sandbox escape attempts are blocked at the bytecode level** — not via runtime exception catching. `import os`, `open()`, `eval()`, and all `__dunder__` access are stripped before execution.
- **DLQ messages that fail healing are nacked without requeue** — preventing infinite poison-pill loops.

---

## Author

**Aritra Ghorai**
[![LinkedIn](https://img.shields.io/badge/LinkedIn-aritraghorai-blue?logo=linkedin)](https://www.linkedin.com/in/aritraghorai/)
[![GitHub](https://img.shields.io/badge/GitHub-aritraju-black?logo=github)](https://github.com/aritraju)

---

## License

MIT License

Copyright (c) 2026 Aritra Ghorai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
