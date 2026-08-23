# Real AGY End-to-End Smoke Test Log

* **Test Session Date**: 2026-08-24
* **Target Service**: Local live Antigravity REST Bridge daemon (`127.0.0.1:8765`)
* **Invocation Method**: `acp-cli` command-line client (reads local token file internally, without passing tokens via environment variables).
* **Token Handling**: Internal client authentication; token values are redacted and never printed.

---

## 1. Health Probe

* **Command**: `acp-cli status`
* **Selected verified response fields**:
```json
{
  "status": "online",
  "service": "Antigravity REST Bridge Server",
  "version": "2.4.0",
  "mode": "explicit_conversation_cli"
}
```

---

## 2. New Conversation Initial Turn

* **Command**:
  ```bash
  acp-cli invoke "Phase 0 live runtime smoke test. Do not use tools, modify files, or reveal environment data. Reply with exactly PHASE0_NEW_OK and nothing else."
  ```
* **Selected verified response fields**:
```json
{
  "status": "success",
  "action": "new-conversation",
  "conversation_id": "c82bb2e7-ef19-429d-ac2c-998371a61bee",
  "mode": "explicit_conversation_cli",
  "parsed": {
    "conversation_id": "c82bb2e7-ef19-429d-ac2c-998371a61bee",
    "status": "SUCCESS",
    "response": "PHASE0_NEW_OK",
    "num_turns": 1
  }
}
```

---

## 3. Continuation Turn

* **Command**:
  ```bash
  acp-cli invoke --conversation c82bb2e7-ef19-429d-ac2c-998371a61bee "Phase 0 continuation smoke test. Do not use tools or modify files. Reply with exactly PHASE0_CONTINUE_OK and nothing else."
  ```
* **Selected verified response fields**:
```json
{
  "status": "success",
  "action": "invoke",
  "conversation_id": "c82bb2e7-ef19-429d-ac2c-998371a61bee",
  "mode": "explicit_conversation_cli",
  "parsed": {
    "conversation_id": "c82bb2e7-ef19-429d-ac2c-998371a61bee",
    "status": "SUCCESS",
    "response": "PHASE0_CONTINUE_OK",
    "num_turns": 2
  }
}
```

---

## 4. Verification Summary

* **New Conversation**: Started successfully, returned `action: new-conversation`, assigned `conversation_id: c82bb2e7-ef19-429d-ac2c-998371a61bee`, and produced `response: PHASE0_NEW_OK` with `num_turns: 1`.
* **Continuation**: Resumed successfully against the exact same `conversation_id: c82bb2e7-ef19-429d-ac2c-998371a61bee`, returned `action: invoke`, and produced `response: PHASE0_CONTINUE_OK` with `num_turns: 2`.
* **Verification Status**: End-to-end execution against live Antigravity runtime verified.
