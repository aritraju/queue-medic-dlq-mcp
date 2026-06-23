#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  seed_failure.sh — Trigger the full self-healing loop end-to-end
#
#  Prerequisites:
#    1. Docker Compose running:  docker compose -f docker/docker-compose.yml up -d
#    2. Server running:          uv run uvicorn src.main:app --reload
#    3. GEMINI_API_KEY in .env
# ─────────────────────────────────────────────────────────────────

BASE_URL="${BASE_URL:-http://localhost:8000}"

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Queue Medic — Self-Healing Demo"
echo "══════════════════════════════════════════════════════"
echo ""

# ── Step 1: Send a well-formed v1 payload (happy path) ───────────
echo "▶  Step 1: Sending a valid v1 payload (should insert directly)..."
curl -s -X POST "${BASE_URL}/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":   "demo-v1-001",
    "user_id":    101,
    "amount":     49.95,
    "timestamp":  "2024-06-15T10:00:00Z",
    "event_type": "purchase"
  }' | python3 -m json.tool
echo ""

sleep 1

# ── Step 2: Send a broken v2 payload (triggers DLQ + healing) ───
echo "▶  Step 2: Sending a BROKEN v2 payload (triggers DLQ + Gemini heal)..."
echo "   Watch the server terminal for the full repair loop output."
echo ""
curl -s -X POST "${BASE_URL}/ingest" \
  -H "Content-Type: application/json" \
  -d '{
    "event_id":   "demo-v2-001",
    "user_id":    "usr_202",
    "payload":    {"amount": 129.00, "currency": "USD"},
    "ts":         "2024-06-15T11:30:00Z",
    "event_type": "checkout"
  }' | python3 -m json.tool
echo ""

echo "   Waiting 15 seconds for the healing agent to complete..."
sleep 15

# ── Step 3: Confirm both events landed in DuckDB ─────────────────
echo "▶  Step 3: Checking events table — both records should appear..."
curl -s "${BASE_URL}/events?limit=5" | python3 -m json.tool
echo ""

# ── Step 4: Show the healing audit log ───────────────────────────
echo "▶  Step 4: Checking healing audit log..."
curl -s "${BASE_URL}/audit?limit=5" | python3 -m json.tool
echo ""

echo "══════════════════════════════════════════════════════"
echo "  Done! If demo-v2-001 appears in /events and /audit,"
echo "  the self-healing loop completed successfully."
echo "══════════════════════════════════════════════════════"
echo ""
