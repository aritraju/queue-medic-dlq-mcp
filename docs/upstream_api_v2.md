# Upstream Event API — Contract v2.0

**Version:** 2.0.0  
**Status:** Active (replaces v1.0 — breaking changes introduced)  
**Content-Type:** `application/json`

---

## Overview

Version 2.0 introduces three breaking structural changes to the event payload.
The DuckDB target schema has **not** been updated — this is the root cause of
the schema drift that the healing agent must resolve.

---

## Breaking Changes from v1.0

### 1. `user_id` type changed: `integer` → `string` (prefixed)

User IDs are now string-typed with a `"usr_"` prefix for cross-system traceability.

| v1.0                | v2.0                 |
|---------------------|----------------------|
| `"user_id": 42`     | `"user_id": "usr_42"` |

**Fix required:** strip the `"usr_"` prefix and cast to `int`.

---

### 2. `amount` moved: top-level → nested inside `payload` object

Transaction amounts are now nested inside a `payload` sub-object alongside
a new `currency` field.

| v1.0              | v2.0                                              |
|-------------------|---------------------------------------------------|
| `"amount": 199.99`| `"payload": {"amount": 199.99, "currency": "USD"}`|

**Fix required:** extract `payload.amount` into the top-level `amount` field.
The `currency` field has no target column and should be discarded.

---

### 3. `timestamp` field renamed to `ts`

The datetime field was shortened for bandwidth optimisation.

| v1.0                              | v2.0                        |
|-----------------------------------|-----------------------------|
| `"timestamp": "2024-06-15T14:00:00Z"` | `"ts": "2024-06-15T14:00:00Z"` |

**Fix required:** map `ts` → `timestamp`.

---

## Full v2.0 Example Payload

```json
{
  "event_id":   "3f7b9c2a-1234-4abc-8def-000000000002",
  "user_id":    "usr_42",
  "payload": {
    "amount":   199.99,
    "currency": "USD"
  },
  "ts":         "2024-06-15T14:00:00Z",
  "event_type": "purchase"
}
```

---

## Required Transformation (pseudo-code)

```python
def heal_payload(raw_json):
    return {
        "event_id":   raw_json["event_id"],
        "user_id":    int(raw_json["user_id"].replace("usr_", "")),
        "amount":     raw_json["payload"]["amount"],
        "timestamp":  raw_json["ts"],
        "event_type": raw_json["event_type"],
    }
```

---

## Changelog

- **2.0.0** — Breaking: user_id prefixed, amount nested in payload, timestamp → ts.
- **1.0.0** — See `upstream_api_v1.md`.
