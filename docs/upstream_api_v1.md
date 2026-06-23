# Upstream Event API — Contract v1.0

**Version:** 1.0.0  
**Status:** Deprecated (superseded by v2.0 — see `upstream_api_v2.md`)  
**Content-Type:** `application/json`

---

## Overview

The Upstream Event Service emits purchase and engagement events to the `/ingest`
endpoint. All events share a flat JSON structure.

---

## Event Payload Schema

| Field        | Type      | Required | Description                                 |
|--------------|-----------|----------|---------------------------------------------|
| `event_id`   | `string`  | Yes      | UUID v4 uniquely identifying this event     |
| `user_id`    | `integer` | Yes      | Numeric internal user identifier            |
| `amount`     | `float`   | Yes      | Transaction value in USD (top-level field)  |
| `timestamp`  | `string`  | Yes      | ISO-8601 UTC datetime of the event          |
| `event_type` | `string`  | Yes      | One of: `purchase`, `checkout`, `view`      |

---

## Example Payload

```json
{
  "event_id":   "3f7b9c2a-1234-4abc-8def-000000000001",
  "user_id":    42,
  "amount":     199.99,
  "timestamp":  "2024-06-15T14:00:00Z",
  "event_type": "purchase"
}
```

---

## DuckDB Target Schema (events table)

The ingestion pipeline writes directly to the following DuckDB schema.
The column names and types here are the authoritative insert contract.

```sql
CREATE TABLE events (
    event_id    VARCHAR   PRIMARY KEY,
    user_id     INTEGER   NOT NULL,
    amount      DOUBLE    NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    event_type  VARCHAR   NOT NULL,
    ingested_at TIMESTAMP DEFAULT current_timestamp
);
```

---

## Changelog

- **1.0.0** — Initial release. Flat structure, integer user IDs, top-level amount.
