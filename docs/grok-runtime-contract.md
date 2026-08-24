# Grok Runtime Contract — Phase 3 Probe

**Probe date:** 2026-08-24 UTC  
**Repository:** `agent-executor-gateway`  
**Status:** **VERIFIED — authenticated headless execution contract confirmed**

## 1. Executable discovery

The direct filesystem probe found these installed launchers (some non-login shells may not include them in `PATH`):

```text
/home/codex/.grok/bin/grok -> ../downloads/grok-1.0.5-linux-x86_64
/home/codex/.local/bin/grok -> /home/codex/.grok/bin/grok
```

The resolved executable reports:

```text
grok 1.0.5 (5115b46bc9) [stable]
```

Phase 4 must resolve the binary through `GROK_BIN`, `PATH`, then the known fallbacks above. Do not assume `which grok` succeeds in every shell.

## 2. Confirmed CLI surface

`grok --help` confirms these headless controls. `--yolo` does **not** exist.

* single-turn prompt: `-p, --single <PROMPT>`
* working directory: `--cwd <CWD>`
* output formats: `plain`, `json`, `streaming-json`, `streaming-messages-json`
* session creation: `-s, --session-id <SESSION_ID>` (new UUID only; must not already exist)
* session continuation: `-r, --resume [<SESSION_ID_OR_TITLE>]`
* permissions: `--permission-mode <MODE>` with `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`
* model and reasoning: `-m, --model`, `--reasoning-effort` / `--effort`
* turn bound: `--max-turns <N>`

`grok agent --help` exposes `headless`, `stdio`, and `serve` transports. Phase 4 uses the verified top-level `-p` headless invocation, not `grok agent headless`.

## 3. Authentication

`grok models` reports:

```text
You are logged in with grok.com.
Default model: grok-4.6
Available models: grok-4.6, grok-4.5
```

`grok doctor` runs container terminal diagnostics. No credentials, tokens, or account data were copied into this repository or printed.

Parent Grok sessions export `GROK_SESSION_ID`, `GROK_AGENT`, and `GROK_WORKTREE`. Gateway turns must strip these so `--session-id` / `--resume` are not overridden by the parent conversation.

## 4. Chosen Phase 4 process command

Verified working new-session command:

```bash
grok \
  --session-id "$UUID" \
  --cwd "$CWD" \
  --output-format json \
  --permission-mode bypassPermissions \
  --effort low \
  --max-turns 8 \
  -p "$PROMPT"
```

Verified continuation command: replace `--session-id "$UUID"` with `--resume "$UUID"`.

## 5. Observed JSON schema

Pretty-printed JSON object on stdout, empty stderr, exit code 0. Observed keys:

```json
{
  "text": "Created `hello.txt` ...",
  "stopReason": "end_turn",
  "sessionId": "75cd5819-23f5-44a1-aa6f-e7711746e302",
  "requestId": "...",
  "thought": "...",
  "usage": {
    "input_tokens": 2767,
    "cache_read_input_tokens": 25600,
    "cache_creation_input_tokens": 0,
    "output_tokens": 140,
    "reasoning_tokens": 70,
    "total_tokens": 28507
  },
  "num_turns": 2,
  "total_cost_usd": 0.00325958,
  "total_cost_usd_ticks": 32595800,
  "modelUsage": {
    "grok-4.6-build": {
      "inputTokens": 2767,
      "outputTokens": 140,
      "cacheReadInputTokens": 25600,
      "cacheCreationInputTokens": 0,
      "modelCalls": 2,
      "costUSD": 0.00325958
    }
  }
}
```

Section 10 mapping:

| Gateway field | Grok source |
| --- | --- |
| `response` | `text` |
| `session_id` | `sessionId` |
| `usage.input_tokens` | `usage.input_tokens` |
| `usage.output_tokens` | `usage.output_tokens` |
| `usage.total_tokens` | `usage.total_tokens` |
| `usage.cost_usd` | `total_cost_usd` |
| `raw.parsed` | full JSON object, including `stopReason`, `thought`, `modelUsage`, cache tokens |

## 6. Runtime evidence

Disposable git repository under `/tmp/grok-phase3-probe-*`. Parent session env vars were unset.

| Capability | Status | Evidence |
| --- | --- | --- |
| Non-interactive coding | **Verified** | `-p` completed without TUI; exit 0 |
| JSON output schema | **Verified** | keys listed in section 5 |
| `cwd` enforcement | **Verified** | only `hello.txt` was added under the disposable repo; parent directory received only probe capture files |
| New session ID | **Verified** | `--session-id 75cd5819-23f5-44a1-aa6f-e7711746e302` was returned unchanged as `sessionId` |
| Resume session | **Verified** | `--resume` kept the same `sessionId`; `hello.txt` changed from `hello` to `hello world` |
| Permission behavior | **Verified** | `--permission-mode bypassPermissions` wrote files without a TUI prompt |
| Timeout behavior | **Verified** | 2.002s `communicate` deadline, `os.killpg(pgid, SIGKILL)`, leftover PIDs in the process group `[]`, `returncode -9` |

Full command/output record: [`docs/PHASE-4-SMOKE-TEST-LOG.md`](./PHASE-4-SMOKE-TEST-LOG.md).

## 7. Acceptance gate

Phase 3's runtime gate is satisfied. Phase 4 may use the observed `-p`, `--cwd`, `--output-format json`, `--session-id`, `--resume`, `--permission-mode bypassPermissions`, `--effort`, and `--max-turns` flags. Provider-specific fields stay in `raw`. The generic Gateway API still treats `session_id != null` as continuation.
