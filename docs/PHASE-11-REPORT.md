# Phase 11 Report — Retire Old Bridge

**Agent Executor Gateway (`agent-executor-gateway`)**  
**Completed:** August 24, 2026

## 1. Outcome

Phase 11 is complete. `agent-executor-gateway` is the sole production executor entry on `127.0.0.1:8765`; the superseded `antigravity-rest-bridge` deployment is stopped, its startup hooks are absent, its README points users to this repository, and its GitHub repository is archived/read-only.

No old source tree, Git history, production backup, token, or migration state was deleted. The local old repository and private migration backups are retained only for emergency recovery and historical reference, not dual maintenance.

## 2. Retirement evidence

| Requirement | Evidence |
| --- | --- |
| New Gateway is the production entry | `127.0.0.1:8765` health returned `200`; PID `21746` ran `/workspace/agent-executor-gateway/acp_server.py` throughout the observation window. |
| Persistent supervision | Watchdog PID `22869` had `PPID=1`, no TTY, and continued five-second health probes. |
| Global client handoff | `/usr/local/bin/acp-cli` resolved to `/workspace/agent-executor-gateway/acp-cli`. |
| Old deployment stopped | No active `/workspace/scripts/acp_server.py`, legacy watchdog, or legacy startup hook remained. Candidate port `8766` was closed. |
| Old repository deprecated | Commit `6fe8703` added English and Chinese superseded/archive notices pointing to the new Gateway. |
| Old repository archived | GitHub reported `hikki-fan/antigravity-rest-bridge` with `isArchived: true`. |
| No dual maintenance | New production work is committed only to `agent-executor-gateway`; the archived old repository is retained as a historical reference. |

## 3. Independent runtime validation

Codex independently performed and inspected these checks after production cutover:

* Full deterministic suite: `python3 -m unittest discover -s . -q` — **278 tests passed, 3 opt-in tests skipped**.
* AGY production API: new invocation and same-session continuation both returned HTTP `200`; the only generated file contained exactly `alpha beta`.
* Grok production API: disposable create/resume completed successfully; a separate real-repository read-only task returned HTTP `200` with the expected version and Phase 10 status.
* Bounded Grok timeout: one intentionally broad read-only audit exceeded its `240s` request budget and returned HTTP `504`; the whole Grok process group was absent afterward and Gateway health remained continuously `200`.
* Legacy compatibility and executor discovery endpoints returned HTTP `200` from the new process.
* Both Git worktrees were clean before retirement documentation changes; the old repository was clean again after its single deprecation commit.

The project release badge is `2.5.0`, while the legacy `/health` and `/acp/v1/status` contract intentionally continues to report `2.4.0`. That value is covered by compatibility tests and was not changed during retirement.

AGY also produced a partial independent audit in the preserved conversation `3a16c7d7-f80b-4abf-8607-ae3ffb133627`, but its final status was `partial_success` because the upstream quota was reached and it misstated focused test counts. Its report was therefore treated as untrusted supporting context, not completion evidence.

## 4. Observation scope and retained recovery assets

The same Gateway PID remained healthy from the cutover at `2026-08-24T05:51:43Z` through Phase 11 execution, while serving AGY and Grok work, a complete local regression suite, compatibility probes, and continuous watchdog checks. No fatal/traceback pattern was found in the production log.

The following recovery assets remain intentionally available:

* `/home/codex/.agent-executor-gateway/production/migration_state.json`
* `/home/codex/.agent-executor-gateway/production/previous_acp_cli_target`
* `/home/codex/.agent-executor-gateway/production/startup-backups/20260824T054450474862Z`
* Local `/workspace/antigravity-rest-bridge` Git history

GitHub archive status is reversible by a repository administrator. Restoring the old production route still requires the migration tool's explicit two-factor rollback authorization and strict process/port checks.

## 5. Final Definition of Done

Goal Prompt items 1–27 are satisfied. Items 22, 24, 25, 26, and 27 are evidenced by the production handoff, stopped legacy deployment, archived old repository, single-maintenance policy, and new global entry. Item 23 is bounded to the recorded observation window and exercised workloads above; future operational monitoring remains normal maintenance rather than a second deployment path.
