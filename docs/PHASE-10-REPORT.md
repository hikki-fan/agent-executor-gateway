# Phase 10 Report — Production Migration Tooling & Preflight Infrastructure

**Agent Executor Gateway (`agent-executor-gateway`)**

## 1. Current status

* **Phase:** Phase 10 candidate migration tooling and preflight.
* **Production:** **Not migrated.** The legacy bridge remains online on `127.0.0.1:8765` (PID `119`) and was not signalled or restarted.
* **Candidate:** Port `8766` is stopped.
* **Legacy repository:** `/workspace/antigravity-rest-bridge` is clean and untouched.
* **Cutover:** Blocked pending explicit operator authorization **and** a persistent startup handoff to the new gateway watchdog.
* **Phase 11:** Old-bridge retirement, container startup edits, and repository archiving are deferred and require separate authorization.

The migration tool defaults to a read-only preflight. It does not edit `/usr/local/bin/start-codex-container`, `/home/codex/.bashrc`, the legacy repository, or the production process.

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
   - Candidate persistent supervisor for the new gateway.
   - Singleton lock, detached child launch, secure runtime files, strict process identity, health probing, and no killing of unknown listeners.
   - It is **not installed or started** by this phase.
3. [`tests/test_phase10_migration.py`](../tests/test_phase10_migration.py)
   - 29 deterministic migration tests, including token alignment, startup-hook rejection, graceful-budget validation, watchdog isolation guards, read-only preflight, exact process identity, and isolated cutover/rollback.

## 3. Safety gates

| Action | Required authorization | Default behavior |
| --- | --- | --- |
| `preflight` (or no command) | None | Read-only inspection; no signals, runtime directory creation, lock creation, or bytecode writes |
| `status` | None | Read-only health, process, token-strategy, and state display |
| `cutover` | `--confirm-cutover` and `CONFIRM_PRODUCTION_CUTOVER=1`; watchdog override flags if needed | Refuses by default |
| `rollback` | `--confirm-rollback` and `CONFIRM_PRODUCTION_ROLLBACK=1` | Refuses by default |

Even with both cutover confirmations, preflight must pass. In particular, the persistent startup files must already point to the new watchdog and contain no legacy `acp_watchdog.sh` or `ensure_acp_bridge.sh` hook.

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
3. An existing shell profile (by default `/home/codex/.bashrc`) contains the same handoff and no legacy hook. A missing profile is treated as optional; an insecure or legacy profile fails closed.

The live preflight correctly reports that the current entrypoint still launches the legacy watchdog and that `/home/codex/.bashrc` is mode `0666`. No attempt was made to repair either file because doing so changes container startup behavior and requires explicit authorization.

## 7. Read-only live verification

The following checks were run after the hardening changes:

* `python3 -m unittest discover -s . -v`: **269 tests passed, 3 opt-in tests skipped**.
* `python3 -m unittest tests.test_phase10_migration -q`: **29 tests passed**.
* `bash -n scripts/migrate_production.sh scripts/gateway_watchdog.sh`: passed.
* `git diff --check`: passed.
* Isolated candidate runtime on `8766`: health and executor discovery passed; one real AGY invoke returned `success` and created a file in a disposable `/tmp` workspace.
* Grok live disposable smoke (create/resume `hello.txt`): **passed** in 17.995s with `GROK_HOME=/home/codex/.grok`.
* A repeat of the dedicated AGY cwd regression was blocked by the upstream response `Individual quota reached` (the Gateway correctly returned HTTP 500 with the structured error); this is an account-quota limitation, not a Gateway failure.
* Live preflight: returned `1` as intended because startup handoff is not installed.
* Live preflight created no migration runtime directory, changed no legacy file metadata, changed no legacy repository content, and left PID `119` and port `8765` healthy before and after the probe.
* The candidate was stopped after the smoke checks; port `8766` is closed.

## 8. Remaining authorized operations

1. Separately authorize and review the startup handoff installation (entrypoint/profile and any required ownership/mode changes).
2. Re-run live preflight until it passes, then separately authorize the two-factor Phase 10 cutover.
3. Observe the new gateway and run AGY/Grok regression and a real project task.
4. Only after a stable observation window, separately authorize Phase 11: stop the old bridge/container integration, add deprecation text, archive the old repository read-only, and remove dual maintenance.

Until those authorizations are given, the old production bridge remains the sole active service on port `8765`.
