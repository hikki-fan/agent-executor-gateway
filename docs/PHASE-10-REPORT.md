# Phase 10 Report — Production Migration Tooling & Preflight Infrastructure

**Agent Executor Gateway (`agent-executor-gateway`)**

## 1. Current status

* **Phase:** Phase 10 production cutover complete; Phase 11 retirement subsequently completed.
* **Production:** The new `agent-executor-gateway` serves `127.0.0.1:8765` (PID `21746`) with the unified AGY/Grok API.
* **Candidate:** Port `8766` is stopped.
* **Legacy repository:** `/workspace/antigravity-rest-bridge` is clean; Phase 11 later added only the deprecation commit `6fe8703` before archiving it.
* **Watchdog:** [`scripts/gateway_watchdog.sh`](../scripts/gateway_watchdog.sh) is active as PID `22869` with `PPID=1` and no TTY.
* **Client entry:** `/usr/local/bin/acp-cli` resolves to `/workspace/agent-executor-gateway/acp-cli`; the previous target is recorded in the private migration runtime.
* **Cutover:** Completed after explicit operator authorization. The rollback state and old client target remain available as inactive emergency-recovery assets.
* **Phase 11:** Completed on August 24, 2026; the old deployment is stopped and its GitHub repository is archived. See [`PHASE-11-REPORT.md`](./PHASE-11-REPORT.md).

The migration tool defaults to a read-only preflight. The guarded startup handoff was installed with a private backup before cutover. Phase 10 did not edit or delete the legacy repository; the later Phase 11 deprecation commit is documented separately.

## 2. Deliverables

1. [`scripts/migrate_production.sh`](../scripts/migrate_production.sh)
   - Read-only default preflight.
   - Two-factor cutover and rollback confirmation gates.
   - Strict `/proc` executable and NUL-delimited `argv[1]` identity checks.
   - PGID-leader-only process-group signalling.
   - Unknown-listener detection before launching on the production port.
   - Secure, aligned token selection.
   - Graceful legacy shutdown budget derived from `300s + 30s + 5s = 335s` by default.
   - Read-only persistent-startup handoff gate.
   - Atomic state records, singleton `flock`, and emergency rollback.
2. [`scripts/gateway_watchdog.sh`](../scripts/gateway_watchdog.sh)
   - Persistent supervisor for the new gateway.
   - Singleton lock, detached child launch, secure runtime files, strict process identity, health probing, and no killing of unknown listeners.
   - Installed and active after the authorized cutover.
3. [`scripts/install_startup_handoff.py`](../scripts/install_startup_handoff.py)
   - Default read-only renderer for the container entrypoint and shell profile.
   - Applying requires `--apply`, `--confirm-startup-handoff`, and `CONFIRM_STARTUP_HANDOFF=1`.
   - Creates a private timestamped backup, atomically replaces both files, and restores an already-written file if the second replacement fails.
4. [`tests/test_phase10_migration.py`](../tests/test_phase10_migration.py) and [`tests/test_startup_handoff.py`](../tests/test_startup_handoff.py)
   - 38 deterministic migration/handoff tests, including token alignment, startup-hook rejection, graceful-budget validation, watchdog isolation guards, read-only preflight, exact process identity, isolated cutover/rollback, atomic startup rewrites, backups, symlink rejection (including source artifacts), confirmation gates, and transactional restore on write failure.

## 3. Safety gates

| Action | Required authorization | Default behavior |
| --- | --- | --- |
| `preflight` (or no command) | None | Read-only inspection; no signals, runtime directory creation, lock creation, or bytecode writes |
| `status` | None | Read-only health, process, token-strategy, and state display |
| `cutover` | `--confirm-cutover` and `CONFIRM_PRODUCTION_CUTOVER=1`; watchdog override flags if needed | Refuses by default |
| `rollback` | `--confirm-rollback` and `CONFIRM_PRODUCTION_ROLLBACK=1` | Refuses by default |

The cutover was executed only after both confirmations and a passing preflight. The persistent startup files now point to the new watchdog and contain no legacy `acp_watchdog.sh` or `ensure_acp_bridge.sh` hook.

## 4. Authentication continuity

When `ACP_TOKEN_FILE` is explicitly supplied, the tool rejects symlinks, creates a missing file with mode `0600`, and exports that path to the launched service.

When it is unset, the tool reuses `/home/codex/.codex/acp_token` only when it is a non-empty, regular, non-symlink file with exact mode `0600`. This preserves existing `/usr/local/bin/acp-cli` authentication. If that token is unavailable or insecure, the tool creates a new `0600` token under the migration runtime directory and reports that an explicit client handoff is required.

Preflight is read-only: it reports the effective token path without creating or chmod-ing anything.

## 5. Graceful shutdown and port safety

The live legacy bridge reports a `300s` subprocess budget and `30s` authentication grace. The migration default is therefore `335s` (`300 + 30 + 5`), with the minimum enforced unless an operator supplies a larger configured budget.

Cutover sends `SIGTERM` only after strict identity verification, polls every 0.5 seconds, and escalates only to a verified process. Before starting the new gateway it requires the production port to have no listening socket at all; a silent or unknown listener is a hard failure.

An active legacy watchdog is stopped only after the watchdog override gate passes, using the same safe signal path and an explicit exit wait. If it does not exit, cutover aborts before touching the legacy server.

## 6. Persistent startup handoff gate

The candidate handoff target is [`scripts/gateway_watchdog.sh`](../scripts/gateway_watchdog.sh). Preflight checks, without editing files:

1. The candidate watchdog is a regular executable and not a symlink.
2. `/usr/local/bin/start-codex-container` exists, is a regular non-group/other-writable file, contains the marker `AGENT_EXECUTOR_GATEWAY_STARTUP_HANDOFF`, and references the candidate watchdog.
   It must also restore the new repository's `acp-cli`; any legacy `/workspace/scripts/acp-cli` or `/workspace/antigravity-rest-bridge/acp-cli` restoration is rejected.
3. An existing shell profile (by default `/home/codex/.bashrc`) contains the same handoff and no legacy hook. A missing profile is treated as optional; an insecure or legacy profile fails closed.

The live preflight passed immediately before cutover. The guarded installer then updated the real entrypoint/profile after explicit authorization, created a private backup at `/home/codex/.agent-executor-gateway/production/startup-backups/20260824T054450474862Z`, and left both updated files non-group/other-writable. The installed hook resolves the global `acp-cli` to this repository and starts the new watchdog with the production port, token file, and runtime directory explicitly supplied.

## 7. Read-only live verification

The following checks were run after the hardening changes:

* `python3 -m unittest discover -s . -q`: **278 tests passed, 3 opt-in tests skipped**.
* `python3 -m unittest tests.test_phase10_migration -q`: **29 tests passed**.
* `python3 -m unittest tests.test_startup_handoff -q`: **9 tests passed**.
* `bash -n scripts/migrate_production.sh scripts/gateway_watchdog.sh`: passed.
* `git diff --check`: passed.
* Isolated candidate runtime on `8766`: health and executor discovery passed; one real AGY invoke returned `success` and created a file in a disposable `/tmp` workspace.
* Grok live disposable smoke (create/resume `hello.txt`): **passed** in 17.995s with `GROK_HOME=/home/codex/.grok`.
* Live preflight immediately before cutover: **passed**.
* Authorized production cutover: new Gateway PID `21746` serves `127.0.0.1:8765` and reports version `2.4.0`; the candidate port `8766` is closed.
* Global client: `/usr/local/bin/acp-cli` resolves to `/workspace/agent-executor-gateway/acp-cli`; `acp-cli status` returned `0` and the new health payload.
* Compatibility probes: `/v1/executors` and `/acp/v1/status` both returned `200` from the new process.
* Production Grok API smoke: create/resume through `/v1/executors/grok/invoke` returned `200` twice and produced the expected final `hello world` file in a disposable `/tmp` workspace.
* The first production AGY API smoke returned structured HTTP `500` with the upstream `Individual quota reached` error and created no file. After the quota became available, a new invocation and same-session continuation both returned HTTP `200`; Codex independently verified the sole output file contained exactly `alpha beta`.
* Post-cutover watchdog: PID `22869`, `PPID=1`, no TTY, singleton lock mode `0600`.
* The old bridge stopped listening; its former PID `119` is a zombie retained by the container's non-reaping PID 1. No claim is made that historical zombies were reaped.
* The legacy repository remained clean and untouched throughout Phase 10; the startup backup preserves the pre-cutover entrypoint/profile and previous client target. Phase 11 later added only the archived-repository migration notice.

## 8. Phase 10 exit state

1. Production traffic and the global client use the new Gateway on `:8765`.
2. AGY and Grok production regressions completed; timeout cleanup and uninterrupted health probing were also observed under a bounded Grok task timeout.
3. Phase 11 retirement was completed after authorization. Historical rollback files remain private and inactive; the old codebase is no longer a development target.
