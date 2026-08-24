# Grok Runtime Contract — Phase 3 Probe

**Probe date:** 2026-08-24 UTC  
**Repository:** `agent-executor-gateway`  
**Status:** **PARTIAL — runtime executable confirmed; authenticated execution contract not yet verified**

## 1. Executable discovery

The current shell `PATH` does not include `/home/codex/.local/bin`, so `command -v grok` returns no result. A direct filesystem probe found the installed launcher:

```text
/home/codex/.local/bin/grok -> /home/codex/.grok/bin/grok
/home/codex/.grok/downloads/grok-1.0.5-linux-x86_64
```

The resolved executable reports:

```text
grok 1.0.5 (5115b46bc9) [stable]
```

This is an environment/path issue, not evidence that Grok is absent. Phase 4 should resolve the binary through `GROK_BIN`, `PATH`, or this existing fallback in deployment configuration rather than assuming `which grok` succeeds in every shell.

## 2. Confirmed CLI surface

`grok --help` confirms these top-level controls relevant to a future adapter:

* single-turn prompt: `-p, --single <PROMPT>`
* working directory: `--cwd <CWD>`
* output formats: `plain`, `json`, `streaming-json`, `streaming-messages-json`
* session creation: `-s, --session-id <SESSION_ID>` (new UUID only)
* session continuation: `-r, --resume [<SESSION_ID_OR_TITLE>]`
* permissions: `--permission-mode <MODE>` with `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`
* model and reasoning controls: `-m, --model`, `--reasoning-effort` / `--effort`
* turn bound: `--max-turns <N>`

`grok agent --help` exposes three transports:

* `grok agent headless` — headless WebSocket relay
* `grok agent stdio` — stdio transport
* `grok agent serve` — local WebSocket server (default bind `127.0.0.1:2419`)

The subcommand help confirms `grok agent headless --help`, `grok agent stdio --help`, and `grok agent serve --help` all exit successfully. The top-level invocation and transport split must be tested with an authenticated disposable session before choosing the Phase 4 process command.

## 3. Authentication state

`grok models` exits successfully but reports:

```text
You are not authenticated.
Default model: grok-4.6
Available models: grok-4.6, grok-4.5
```

`grok doctor` runs and reports container terminal diagnostics, but it does not establish an authenticated agent session. No credentials, tokens, or account data were copied into this repository or sent to any external service.

## 4. Required probes still pending

The following Phase 3 contract items remain **unverified** because authentication is not available in the current environment and no real coding task was authorized for this probe:

| Capability | Status | Required evidence |
| --- | --- | --- |
| Non-interactive coding | Pending auth | Disposable repository task exits without TUI |
| JSON output schema | Pending auth | Capture and classify `status`/`text`/`sessionId`/`stopReason`/`usage`/`modelUsage`/`num_turns` |
| `cwd` enforcement | Pending auth | File written only under a disposable cwd |
| New session ID | Pending auth | Stable UUID returned and normalized |
| Resume session | Pending auth | Second turn with `--resume` preserves context |
| Permission behavior | Pending auth | `plan`/`acceptEdits`/`bypassPermissions` behavior recorded |
| Timeout behavior | Pending auth | Deterministic short timeout and process-tree cleanup |

No `adapters/grok.py`, registry entry, or guessed JSON parser was added. Phase 3 is not complete and Phase 4 must not start until the table above has runtime evidence.

## 5. Next acceptance gate

After the Grok account is authenticated, run a disposable-repository matrix using the resolved launcher (or a configured `GROK_BIN`) and capture stdout, stderr, exit code, session identifier, and filesystem diff for each case. Only after those observations should the generic `ExecutorAdapter` implementation be written.
