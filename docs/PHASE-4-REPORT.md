# Phase 4 Report — Grok Executor Adapter
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Executive Summary & Metadata

* **Repository Name**: `agent-executor-gateway`
* **Current Phase**: `Phase 4 — GrokAdapter`
* **Lifecycle Status**: `Phase 4 Candidate — Agy implementation reviewed and independently verified by Codex; acceptance commit pending at report authoring time`
* **Baseline Commit**: `58bed5fbfd3deaae8d63d5329eed5943e508291f` (`docs: record partial grok runtime probe`)
* **Upstream Reference**: `/workspace/antigravity-rest-bridge` (Unmodified, Clean, Zero changes)
* **Scope Discipline**: Phase 4 implements `adapters/grok.py`, registers `grok` in `executor_registry`, and exposes `GET /v1/executors/grok/health` plus `POST /v1/executors/grok/invoke`. Phase 5 multi-executor concurrency, Task DAGs, routing, and production cutover remain out of scope.

---

## 2. Implementation Summary

### 2.1 Runtime contract (Phase 3 gate)

Authenticated `grok` 1.0.5 was probed before adapter completion. Evidence is in [`docs/grok-runtime-contract.md`](./grok-runtime-contract.md) and [`docs/PHASE-4-SMOKE-TEST-LOG.md`](./PHASE-4-SMOKE-TEST-LOG.md).

Verified headless command (no `--yolo`; that flag does not exist):

```bash
grok \
  --session-id "$UUID" \          # new session only
  --resume "$UUID" \              # continuation only
  --cwd "$CWD" \
  --output-format json \
  --permission-mode bypassPermissions \
  --effort low \
  --max-turns N \
  -p "$PROMPT"
```

Observed JSON keys: `text`, `stopReason`, `sessionId`, `requestId`, `thought`, `usage`, `num_turns`, `total_cost_usd`, `modelUsage`.

### 2.2 Grok Adapter (`adapters/grok.py`)

* `resolve_grok_bin()`: `GROK_BIN` → `PATH` → known fallbacks.
* `GrokConfig`: `GROK_BIN`, `GROK_MODEL`, `GROK_EFFORT`, `GROK_AGENT_TIMEOUT_SEC` (default 900), `GROK_PERMISSION_MODE` (default `bypassPermissions`), `GROK_MAX_TURNS` (default 50).
* New session: generate UUID4 and pass `--session-id`. Continuation (`session_id != null`): pass `--resume`.
* Strip inherited `GROK_SESSION_ID`, `GROK_AGENT`, and `GROK_WORKTREE` so gateway turns do not attach to a parent Grok session.
* Parse `text` → `response`, `sessionId` → `session_id`, `usage.*` + `total_cost_usd` → Section 10 `usage`. Extra provider fields remain in `raw.parsed`.
* Status: `success` (exit 0 + JSON with text/`sessionId`), `partial_success` (non-zero exit + usable `text`), `error` otherwise.
* Process execution uses `core.process.run_process_group` (`stdin=DEVNULL`, `start_new_session=True`, `os.killpg` on timeout).

### 2.3 Server integration (`acp_server.py`)

* Registers `grok_adapter` beside `agy_adapter`.
* Generic routes already dispatch by executor name; Grok is therefore reachable at `/v1/executors/grok/health` and `/v1/executors/grok/invoke`.
* Default Future wait uses the invoked adapter timeout (`total_process_timeout` / `default_timeout_sec`) plus the +5s transport margin.
* Legacy `/acp/v1/*` remains mapped only to `AntigravityAdapter`.

---

## 3. Test Suite Coverage & Verification Results

| Suite | Tests | Status |
| :--- | :---: | :--- |
| `tests/test_grok_adapter.py` | 22 | **21 PASS, 1 skipped** (`RUN_LIVE_GROK_SMOKE` gated) |
| `tests/test_executor_api.py` | 27 | **PASS** (discovery now expects `agy` + `grok`) |
| `tests/test_legacy_compatibility.py` | 38 | **PASS** |
| `tests/test_agy_adapter.py` | 20 | **PASS** |
| `tests/test_core.py` | 19 | **PASS** (forbidden terms now include Grok identifiers) |
| `test_acp_bridge.py` | 18 | **PASS** |
| **Discovery total** | **144** | **OK (skipped=1)** |

Additional checks:

* `python3 -m compileall -q acp_server.py api core adapters tests test_acp_bridge.py` → exit 0
* `git diff --check` → exit 0
* `core/` AST neutrality: zero `grok` / `GROK_*` / Grok CLI flags
* `/workspace/antigravity-rest-bridge` remains unmodified

### Live CLI probe (not the gated unit test)

Disposable repo `/tmp/grok-phase3-probe-8cVTGj/repo`:

1. New session `--session-id 75cd5819-23f5-44a1-aa6f-e7711746e302` created `hello.txt` with `hello`.
2. `--resume` same UUID changed contents to `hello world`.
3. 2.002s timeout + `killpg` left zero PIDs in the process group (`returncode -9`).

### Isolated HTTP smoke

Loopback `:28992`, temporary token, production `:8765` untouched:

* `GET /health` → 200
* `GET /v1/executors` → `agy` + `grok`
* `GET /v1/executors/grok/health` → 200 `Grok Build Agent`
* Unauthenticated Grok invoke → 401
* `timeout_sec: 0` → 400

---

## 4. Scope Discipline — What Was Not Done

1. **No Phase 5 concurrency split**: Grok still shares the existing worker semaphore (`AGY_MAX_CONCURRENCY`). Per-executor `GROK_MAX_CONCURRENCY` and `GATEWAY_MAX_CONCURRENCY` are deferred.
2. **No Task DAG / router / worktree**.
3. **No production port cutover**: `:8765` was not restarted.
4. **Commit/push timing**: this report was authored before the Phase 4 acceptance commit; the commit and remote verification are recorded in Git history and the final handoff.
5. **Gated live adapter smoke**: `tests.test_grok_adapter.TestGrokLiveSmokeIntegration` is skipped unless `RUN_LIVE_GROK_SMOKE=1`. The authenticated CLI matrix above is the runtime evidence for hello.txt continuity.

---

## 5. Status & Handoff

Phase 4 candidate passed the independent review gates and is ready for the acceptance commit:

```text
feat: add grok executor adapter
```
