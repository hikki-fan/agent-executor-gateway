# Phase 4 Grok Adapter Smoke Test Log

Date: 2026-08-24 UTC

The smoke used `/home/codex/.local/bin/grok` version `1.0.5`, an authenticated writable temporary `GROK_HOME`, and disposable workspaces under `/tmp`. The project repository, old bridge repository, and production port `8765` were not touched.

## Runtime contract

An authenticated `-p ... --output-format json` probe exited `0` with empty stderr. Its single JSON object contained `text`, `stopReason`, `sessionId`, `requestId`, `usage`, `num_turns`, cost fields, and `modelUsage`.

## Adapter session and cwd smoke

`GrokAdapter.invoke(session_id=None)` created `hello.txt` in the disposable cwd with `hello` and returned `ExecutorResult.status=success` plus a normalized session ID. `GrokAdapter.resume(...)` passed `--resume` for the same ID and changed the file to `hello world`, again returning `success`. The workspace contained only `hello.txt` after both turns.

## Permission and timeout checks

* A plan-mode prompt did not create `plan-mode.txt`.
* `GrokAdapter.invoke(timeout_sec=5)` raised `subprocess.TimeoutExpired` for a long-running prompt. Process-group inspection found no remaining `grok` or child `sleep` process.

The live test is opt-in (`RUN_LIVE_GROK_SMOKE=1` plus an authenticated `GROK_HOME`) so ordinary unit-test discovery does not consume an account or require network access. Deterministic adapter and HTTP tests run without credentials.
