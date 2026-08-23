# Phase 1 Live Runtime Smoke Test Log
**Agent Executor Gateway (`agent-executor-gateway`)**

---

## 1. Execution Overview & Isolation

* **Test Date**: 2026-08-24
* **Isolated test endpoint**: `127.0.0.1:18766`
* **Invocation Method**: `acp-cli` configured with `ACP_SERVER_URL=http://127.0.0.1:18766` and an isolated temporary `ACP_TOKEN_FILE`.
* **Token Isolation**: Both the test server and `acp-cli` were explicitly configured to the temporary token path; no test command referenced the production token path or value.
* **Production Daemon Isolation**: The test server ran independently on port `18766` and did not replace, signal, or restart the active production daemon on `127.0.0.1:8765`.
* **Subprocess Execution**: Executed against live Google Antigravity CLI runtime via `AntigravityAdapter`.

---

## 2. Health & Status Probe

* **Endpoint**: `http://127.0.0.1:18766/health`
* **Selected verified response fields**:
```json
{
  "status": "online",
  "service": "Antigravity REST Bridge Server",
  "version": "2.4.0",
  "auth_type": "Strict Bearer Token",
  "mode": "explicit_conversation_cli",
  "language_server": {
    "status": "disabled",
    "address": null,
    "mode": "explicit_conversation_cli"
  },
  "limits": {
    "max_payload_bytes": 2097152,
    "subprocess_timeout_sec": 300,
    "auth_grace_sec": 30,
    "total_process_timeout_sec": 330,
    "max_worker_threads": 1,
    "max_http_connections": 50,
    "max_post_connections": 45,
    "reserved_health_slots": 5,
    "socket_timeout_sec": 10.0,
    "admission_control": "HTTP 429 Bounded Semaphore (1)"
  }
}
```

---

## 3. Turn 1 — New Conversation Initiation

* **Prompt**: `Phase 1 refactored gateway smoke test. Do not use tools, modify files, or reveal environment data. Reply with exactly PHASE1_ADAPTER_NEW_OK and nothing else.`
* **Selected verified response fields**:
```json
{
  "status": "success",
  "action": "new-conversation",
  "conversation_id": "472569ac-0cb3-4160-9b6f-2d0e460b48c7",
  "mode": "explicit_conversation_cli",
  "parsed": {
    "conversation_id": "472569ac-0cb3-4160-9b6f-2d0e460b48c7",
    "status": "SUCCESS",
    "response": "PHASE1_ADAPTER_NEW_OK",
    "num_turns": 1
  }
}
```

---

## 4. Turn 2 — Conversation Continuation

* **Target Conversation**: `472569ac-0cb3-4160-9b6f-2d0e460b48c7`
* **Prompt**: `Phase 1 continuation smoke test. Do not use tools or modify files. Reply with exactly PHASE1_ADAPTER_CONTINUE_OK and nothing else.`
* **Selected verified response fields**:
```json
{
  "status": "success",
  "action": "invoke",
  "conversation_id": "472569ac-0cb3-4160-9b6f-2d0e460b48c7",
  "mode": "explicit_conversation_cli",
  "parsed": {
    "conversation_id": "472569ac-0cb3-4160-9b6f-2d0e460b48c7",
    "status": "SUCCESS",
    "response": "PHASE1_ADAPTER_CONTINUE_OK",
    "num_turns": 2
  }
}
```

---

## 5. Server Teardown & Process Cleanup

* **Teardown Signal**: `SIGINT` sent to test server process.
* **Server Output**:
```text
[*] Received signal 2, triggering async server shutdown...
[*] Exited serve_forever(). Cancelling unstarted futures & shutting down ThreadPool...
[*] Graceful shutdown completed cleanly.
```
* **Port Verification**: Port `18766` closed and freed.
* **Process Cleanup**: Verified zero surviving orphaned background processes.
* **Storage Cleanup**: Temporary token directory removed cleanly.

---

## 6. Codex Independent Verification Evidence

Codex independent verification after correction round 1:
* **Command A** (`test_acp_bridge.py`): 18 tests in 1.461s **PASS**
* **Command B** (`tests.test_legacy_compatibility`): 38 tests in 2.099s **PASS**
* **Command C** (`tests.test_core tests.test_agy_adapter`): 39 tests in 1.780s **PASS**
* **Command D** (`unittest discover -s . -v`): 95 tests in 5.299s **PASS**
* **Command E** (`compileall`): Exited with code `0`
* **Command F** (`git diff --check`): Exited with code `0`
* **Command G** (`rg -n -i grok ...`): `0` matches found
* **Core Provider Scan**: `0` provider identifiers or terms found in `core/`
* **Old Repository**: `/workspace/antigravity-rest-bridge` remains clean and unmodified
