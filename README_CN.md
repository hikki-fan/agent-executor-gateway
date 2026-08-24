# Agent Executor Gateway

[English](./README.md) | **中文文档**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.5.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-%E7%94%9F%E4%BA%A7%20Gateway-brightgreen.svg)]()

高可靠、Executor 中立的 **Agent Executor Gateway**，为 AI 编码 Agent 提供统一 REST API 编排、会话状态隔离与进程生命周期管控。

> [!NOTE]
> **生产迁移已完成**：`agent-executor-gateway` 已成为 `:8765` 上唯一的生产 Executor 入口，由 `scripts/gateway_watchdog.sh` 常驻监督，并通过统一 API 提供 AGY 与 Grok。旧 `antigravity-rest-bridge` 部署已停止，其 GitHub 仓库已于 2026 年 8 月 24 日归档。私有迁移备份继续用于紧急恢复，但所有新功能与缺陷修复只在本仓库维护。详见 [Phase 11 报告](./docs/PHASE-11-REPORT.md)。

可选启动交接工具 (`scripts/install_startup_handoff.py`) 默认只读；只有同时提供 `--apply`、`--confirm-startup-handoff` 和 `CONFIRM_STARTUP_HANDOFF=1` 才会执行，并在原子更新入口/profile 前创建私有备份。

---

## 🌟 核心特性

- 🚀 **双端口共存迁移候选 (Phase 9)**：候选管理脚本 (`scripts/migration_candidate.sh`) 在 `8766` 上独立管理 PID、日志与 `0600` Token，已用于完成生产切换前验证，并可在回滚窗口内继续做隔离回归。
- 🌿 **独立 Git Worktree 隔离管控 (Phase 8)**：Executor 中立的 Worktree 管理模块 (`orchestration/worktree.py`)，支持安全根目录限制 (`<repo_parent>/.agent-worktrees/`)、规范化分支命名 (`agent/<sanitized-task-id>-<executor>`)、严格路径逃逸防护以及基于 `git worktree remove` 和 prune 的安全清理。
- 🌳 **Task DAG 与有界并行分发 (Phase 8)**：依赖感知的 DAG 调度引擎 (`orchestration/dag.py`)，支持 `depends_on` 校验、环路检测、`READY` / `BLOCKED` 状态自动判定，以及独立任务跨 Worktree 的有界并发分发（例如 AGY Task-A 与 Grok Task-B 同时在两个独立 worktree 执行），严禁未经评审的自动合并。
- 🧭 **基于规则的任务路由 (Phase 7)**：实现 Section 22 确定性路由逻辑 (`orchestration/router.py`)：
  - `S`（低/高风险）与 `M`（功能/缺陷修复/重构）自动路由至 `agy`
  - `M`（排错/深入调查）自动路由至 `grok`
  - `L` / `XL` 阻断全自动执行，强制返回需人工/Codex 拆分或覆盖的决策
  - 显式 Codex/执行器覆盖具有最高优先级，并严格校验目标合法性。
- 🔄 **多执行器升级与状态机 (Phase 7)**：实现有界轮次状态机 (`orchestration/escalation.py`)，支持同执行器自我修复（默认 2 次尝试）、执行器升级切换（如 `agy -> grok`，默认 1 次切换），并在切换上限耗尽后触发 `REPLAN_REQUIRED`，彻底杜绝死循环。
- 📦 **结构化脱敏上下文交接 (Phase 7)**：实现 Section 27 任务交接上下文，传递原始目标、验收标准、基线 Commit、当前 Git Diff、变更文件、验证命令、失败输出及历史轮次记录，全流程自动脱敏 Bearer Token 及敏感凭据 (`[REDACTED]`)。
- 🛠️ **`agentctl` 统一控制工具 (Phase 6, 7, 8)**：支持 `agentctl worktree create/list/cleanup`、`agentctl task validate/verify/route/plan/ready/graph`、`agentctl executors`、`agentctl health` 及 `agentctl invoke`。
- 📋 **统一任务模型与校验 (Phase 6 & 8)**：实现 Executor 中立的 Task JSON 规范 (`orchestration/task.py`)，校验目标、分类、执行策略、变更范围、验收标准、验证命令及 `depends_on` 依赖。
- 🧪 **安全机器验证流水线 (Phase 6)**：安全命令执行器 (`orchestration/verifier.py`)，采用 `shell=False` 解析、`cwd` 严格隔离、进程组超时强杀 (`os.killpg`)、敏感凭据脱敏以及精简尾部日志提取。
- 🛡️ **严格变更范围管控 (Phase 6)**：基于 Git 状态与 Diff 的边界检查 (`orchestration/scope.py`)，拦截任何超出 `allowed_paths` 或落入 `forbidden_paths` 的已提交、已暂存、未暂存及未跟踪文件。
- 📊 **标准完成报告与指标 (Phase 6)**：生成 Section 30 JSON Completion Report 并向 `.agent/metrics.jsonl` 追加结构化执行指标。
- 🌐 **统一 Generic Executor API (Phase 2 & 4)**：提供标准化的执行器发现 (`GET /v1/executors`)、健康检查 (`GET /v1/executors/{executor}/health`) 与统一调用 (`POST /v1/executors/{executor}/invoke`)。
- 🤖 **多执行器后端支持**：同时支持 Google Antigravity (`agy`) 与 Grok Build (`grok`) 无头 CLI 运行环境。
- 📊 **Section 10 统一结果契约**：所有执行器统一返回 `ExecutorResult` 结构 (`status`, `executor`, `session_id`, `response`, `exit_code`, `timing`, `usage`, `warnings`, `error`, `raw`)。
- 🎯 **显式 1:1 会话隔离与无状态网关**：客户端持有 `session_id`（或旧版 `conversation_id`），完全杜绝全局 `agy -c` 抢占。
- 🔒 **单会话并发互斥锁（Per-Session Lock）**：针对同一 `(executor, session_id)` 的并发请求自动返回 `HTTP 409 Conflict`，且跨 Generic 与 Legacy 接口统一生效。
- 🛑 **统一有界准入控制（HTTP 429）**：全局 `GATEWAY_MAX_CONCURRENCY` 与独立的 `AGY_MAX_CONCURRENCY` / `GROK_MAX_CONCURRENCY` 信号量，超额立即返回 `HTTP 429`。
- ⏱️ **灵活超时预算管理**：支持请求级 `timeout_sec` 超时覆盖，同时控制执行预算与外层等待窗口（+5s 传输余量），超时通过 `os.killpg(pgid, SIGKILL)` 彻底清理进程组。
- 🔁 **前置异常智能重试**：新会话在 0-turn 启动阶段遇到 transient 错误（EOF/网络重置）自动重试最多 3 次，运行中错误如实保留供客户端决策。
- 🟡 **部分成功保留**：执行器已生成可用回复但退出状态为非零时返回 HTTP 200 `partial_success`，保留回复及诊断告警。
- 🔐 **严格 Bearer Token 身份鉴权**：所有 POST API 操作均需通过 `Authorization: Bearer <TOKEN>` 认证（`0600` 属主独占权限）。
- ⚡ **通用探针连接保障**：独立 45-POST 上限为探针和其他请求保留 5 个通用 HTTP 连接槽；这 5 个槽并非 `/health` 专属。
- 🛡️ **慢连接与 Slowloris 防护**：配置单次 I/O 超时（10s）、请求体大小限制（2MB）以及 HTTP 总连接数上限（50）。
- 🔄 **完整 Legacy ACP 兼容**：保留所有 `/acp/v1/*` 接口，无缝兼容现有 Codex 工作流。

---

## 🏗️ 架构概览

```
[ Codex / 客户端 ]
        │
        ├─── (Generic API: /v1/executors/*) ───────┐
        │                                          │
        └─── (Legacy API: /acp/v1/*, /health) ─────┤
                                                   ▼
                                   ┌────────────────────────────────┐
                                   │     acp_server.py (HTTP)       │
                                   │  - Strict Bearer 鉴权          │
                                   │  - 准入控制信号量 (429)        │
                                   │  - 会话锁管理器 (409)          │
                                   │  - ExecutorRegistry            │
                                   └───────────────┬────────────────┘
                                                   │
                                                   ▼
                                   ┌────────────────────────────────┐
                                   │      ExecutorAdapter (ABC)     │
                                   └───────┬────────────────┬───────┘
                                           │                │
                                           ▼                ▼
                    ┌────────────────────────────┐    ┌───────────────────────────┐
                    │  AntigravityAdapter (agy)  │    │     GrokAdapter (grok)    │
                    │  - 1:1 session_id 映射     │    │  - 无头 CLI (-p)          │
                    │  - 参数在 -p 前排序        │    │  - --session-id / --resume│
                    │  - 启动阶段智能重试        │    │  - JSON 解析与 usage 归一 │
                    │  - 统一 ExecutorResult     │    │  - 统一 ExecutorResult    │
                    └─────────────┬──────────────┘    └─────────────┬─────────────┘
                                  │                                 │
                                  └────────────────┬────────────────┘
                                                   ▼
                                   ┌────────────────────────────────┐
                                   │      core/process.py           │
                                   │  - 独立进程组 setsid           │
                                   │  - os.killpg 进程树清理        │
                                   └────────────────────────────────┘
```

---

## 📡 REST API 接口文档

### 1. Generic Executor API (Phase 2 & 4)

#### 执行器发现探针 (`GET /v1/executors`)
无需鉴权。
```bash
curl -s http://127.0.0.1:8765/v1/executors
```
响应示例：
```json
{
  "executors": [
    {
      "name": "agy",
      "available": true,
      "supports_session": true
    },
    {
      "name": "grok",
      "available": true,
      "supports_session": true
    }
  ]
}
```

#### 执行器健康检查 (`GET /v1/executors/{executor}/health`)
无需鉴权。支持 `agy` 与 `grok`。
```bash
curl -s http://127.0.0.1:8765/v1/executors/agy/health
curl -s http://127.0.0.1:8765/v1/executors/grok/health
```

#### 统一任务调用 (`POST /v1/executors/{executor}/invoke`)
需要 Bearer Token 鉴权。支持执行器：`agy`、`grok`。

- **启动 AGY 新建任务 / 会话**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/agy/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "实现任务取消功能",
    "cwd": "/workspace/project",
    "session_id": null,
    "model": "flash",
    "effort": "medium",
    "timeout_sec": 600
  }'
```

- **启动 Grok 新建任务 / 会话**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/grok/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "实现数据处理流程",
    "cwd": "/workspace/project",
    "session_id": null,
    "model": "grok-4.6",
    "effort": "high",
    "timeout_sec": 900
  }'
```

- **续接已有 Grok 会话**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/grok/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "为该流程添加单元测试",
    "cwd": "/workspace/project",
    "session_id": "01a0315c-0241-74b0-beb0-ff058535d5d6"
  }'
```

#### 统一 ExecutorResult 响应规范
所有 Generic 调用均返回 Section 10 标准响应格式：
```json
{
  "status": "success",
  "executor": "grok",
  "session_id": "01a0315c-0241-74b0-beb0-ff058535d5d6",
  "response": "已为数据处理流程添加完备单元测试。",
  "exit_code": 0,
  "timing": {
    "duration_ms": 4520
  },
  "usage": {
    "input_tokens": 11144,
    "output_tokens": 120,
    "total_tokens": 14136,
    "cost_usd": 0.00408816
  },
  "warnings": [],
  "error": null,
  "raw": {
    "parsed": {"text": "...", "stopReason": "end_turn"},
    "stdout": "...",
    "stderr": ""
  }
}
```

---

### 2. Legacy ACP 兼容接口

| 路径 | 方法 | 鉴权 | 描述 |
| :--- | :--- | :--- | :--- |
| `/health` | GET | 无 | 旧版健康探针及系统限制参数 |
| `/acp/v1/status` | GET | 无 | `/health` 别名 |
| `/acp/v1/invoke` | POST | Bearer | 发起或继续旧版会话任务 |
| `/acp/v1/new-conversation`| POST | Bearer | 显式新建旧版会话任务 |
| `/acp/v1/send-message` | POST | Bearer | 携带 `recipient_id` 发送消息 |
| `/acp/v1/metadata` | POST | Bearer | 返回 HTTP 501 Not Implemented |

---

## 🛠️ `agentctl` 命令行工具 (Phase 6 & 7)

仓库提供 `agentctl` 实用工具用于任务校验、流水线验证、路由判定、执行规划以及执行器状态检查：

### 1. 任务模型静态校验
无需执行任何命令，直接校验 Task JSON 是否合规：
```bash
./agentctl task validate .agent/tasks/TASK-001.json
```

### 2. 基于规则的任务路由 (Phase 7)
根据任务复杂度、类型与风险评估执行器路由：
```bash
./agentctl task route .agent/tasks/TASK-001.json
# 使用显式 Codex 覆盖：
./agentctl task route .agent/tasks/TASK-001.json --override grok
```

### 3. 执行与升级策略规划 (Phase 7)
展示完整的路由决策、备用执行器、尝试与切换预算及验收验证命令：
```bash
./agentctl task plan .agent/tasks/TASK-001.json
```

### 4. 任务机器验证流水线 (Phase 6)
运行机器验证命令并执行 Git 范围边界检查：
```bash
./agentctl task verify .agent/tasks/TASK-001.json --json
```

### 5. 执行器与健康检查
```bash
./agentctl executors
./agentctl health
```

---

## 🛡️ 安全与限额参数规范

| 指标 / 防线 | 配置值 / 策略 | 详细说明 |
| :--- | :--- | :--- |
| **会话隔离模型** | 显式 `session_id` / `conversation_id` | 客户端持有 ID；完全取消全局 `agy -c` 抢占 |
| **会话并发互斥** | `HTTP 409 Conflict` | 跨 Generic 与 Legacy 统一互斥保护 |
| **Gateway 全局任务上限** | `GATEWAY_MAX_CONCURRENCY`（默认 `2`） | 覆盖所有执行器的全局有界信号量，超额返回 `HTTP 429` |
| **执行器任务上限** | `AGY_MAX_CONCURRENCY=1`、`GROK_MAX_CONCURRENCY=1` | 各执行器独立有界信号量，超额返回 `HTTP 429` |
| **范围越界管控** | Git 状态/Diff 对比 allowed/forbidden globs | 发生任何越界返回 `scope_violation` 失败 |
| **安全机器验证** | `shell=False` 在仓库 `cwd` 中隔离执行 | 进程组 SIGKILL 强杀、敏感凭据脱敏与精简尾部日志 |
| **任务执行预算** | 请求 `timeout_sec` 或执行器默认值 | 组合期限到期后通过 `os.killpg(pgid, SIGKILL)` 清理进程组 |
| **传输等待余量** | `+5.0 秒` | 外层 Future 等待窗口在任务超时上额外增加 5 秒 |
| **最大连接数** | `50` | `45 POST` + `5 通用` 配额；最后 5 个槽并非 `/health` 专属 |
| **Socket 超时** | `10.0 秒` | 超过 10 秒无读写自动断开 Socket 链接 |
| **请求体限制** | `2 MB` | 超过 2MB 直接拦截并返回 `HTTP 413` |

---

## 📄 开源协议

MIT License © 2026 hikki-fan
