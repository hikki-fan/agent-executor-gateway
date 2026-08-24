# Phase 2 Isolated HTTP Smoke Test

Date: 2026-08-24 (UTC)

The candidate server was started from this repository on loopback port `28991` with `AGY_MAX_CONCURRENCY=1`, a temporary token file, and no changes to the production service on port `8765`.

| Check | Result |
| --- | --- |
| `GET /health` | HTTP 200; `status=online`; `service=Antigravity REST Bridge Server`; limits reported expected values |
| `GET /v1/executors` | HTTP 200; exactly one registered executor: `agy` |
| Unauthenticated `POST /v1/executors/agy/invoke` | HTTP 401; strict Bearer authentication required |
| Authenticated `timeout_sec=0` request | HTTP 400; positive-integer validation enforced |
| SIGINT shutdown | Clean `serve_forever()` exit and thread-pool shutdown messages |

No real Agent task was submitted during this smoke test; execution behavior is covered by the deterministic mock-based API suite. The temporary server and token were removed after the check.
