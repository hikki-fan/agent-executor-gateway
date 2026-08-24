# Agent Executor Gateway

[English](./README.md) | **中文文档**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.4.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Phase%202%20Candidate-yellow.svg)]()

高可靠、Executor 中立的 **Agent Executor Gateway**，为 AI 编码 Agent 提供统一 REST API 编排、会话状态隔离与进程生命周期管控。

> [!NOTE]
> **Phase 2 说明**：当前已引入 Generic Executor API 并注册 `agy` 执行器。旧版 ACP API 在已测试契约下保持向后兼容。后续阶段将逐步引入 Grok 及生产迁移。

---

## 🌟 核心特性

- 🌐 **统一 Generic Executor API (Phase 2)**：提供标准化的执行器发现 (`GET /v1/executors`)、健康检查 (`GET /v1/executors/{executor}/health`) 与统一调用 (`POST /v1/executors/{executor}/invoke`)。
- 📊 **Section 10 统一结果契约**：所有执行器统一返回 `ExecutorResult` 结构 (`status`, `executor`, `session_id`, `response`, `exit_code`, `timing`, `usage`, `warnings`, `error`, `raw`)。
- 🎯 **显式 1:1 会话隔离与无状态网关**：客户端持有 `session_id`（或旧版 `conversation_id`），完全杜绝全局 `agy -c` 抢占。
- 🔒 **单会话并发互斥锁（Per-Session Lock）**：针对同一 `(executor, session_id)` 的并发请求自动返回 `HTTP 409 Conflict`，且跨 Generic 与 Legacy 接口统一生效。
- 🛑 **全局有界准入控制（HTTP 429）**：共享执行器并发信号量，超额立即返回 `HTTP 429 Too Many Requests`。
- ⏱️ **灵活超时预算管理**：支持请求级 `timeout_sec` 超时覆盖，同时控制执行预算与外层等待窗口（+5s 传输余量），超时通过 `os.killpg(pgid, SIGKILL)` 彻底清理进程组。
- 🔁 **前置异常智能重试**：新会话在 0-turn 启动阶段遇到 transient 错误（EOF/网络重置）自动重试最多 3 次，运行中错误如实保留供客户端决策。
- 🟡 **部分成功保留**：执行器已生成可用回复但退出状态为 `ERROR` 时返回 HTTP 200 `partial_success`，保留回复及诊断告警。
- 🔐 **严格 Bearer Token 身份鉴权**：所有 POST API 操作均需通过 `Authorization: Bearer <TOKEN>` 认证（`0600` 属主独占权限）。
- ⚡ **通用探针容量**：独立的 45 个 POST 配额会为探针和其他请求留下 5 个通用 HTTP 连接槽；这 5 个槽并非 `/health` 专属。
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
                                   └───────────────┬────────────────┘
                                                   │
                                                   ▼
                                   ┌────────────────────────────────┐
                                   │     AntigravityAdapter (agy)   │
                                   │  - 1:1 session_id 映射         │
                                   │  - 命令参数在 -p 前排序        │
                                   │  - 启动阶段智能重试            │
                                   │  - 统一 ExecutorResult 格式化  │
                                   └───────────────┬────────────────┘
                                                   │
                                                   ▼
                                   ┌────────────────────────────────┐
                                   │      core/process.py           │
                                   │  - 独立进程组 setsid           │
                                   │  - os.killpg 进程树清理        │
                                   └────────────────────────────────┘
```

---

## 📡 REST API 接口文档

### 1. Generic Executor API (Phase 2)

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
    }
  ]
}
```

#### 执行器健康检查 (`GET /v1/executors/{executor}/health`)
无需鉴权。
```bash
curl -s http://127.0.0.1:8765/v1/executors/agy/health
```
响应示例：
```json
{
  "status": "online",
  "service": "Antigravity REST Bridge Server",
  "version": "2.4.0",
  "mode": "explicit_conversation_cli",
  "binary": "/home/codex/.local/bin/agy",
  "available": true
}
```

#### 统一任务调用 (`POST /v1/executors/{executor}/invoke`)
需要 Bearer Token 鉴权。

- **新建会话 / 初始任务**：
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

- **续接已有会话**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/v1/executors/agy/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "根据测试反馈继续修复",
    "cwd": "/workspace/project",
    "session_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b"
  }'
```

#### 统一 ExecutorResult 响应规范
所有 Generic 调用均返回 Section 10 标准响应格式：
```json
{
  "status": "success",
  "executor": "agy",
  "session_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b",
  "response": "代码重构已完成。",
  "exit_code": 0,
  "timing": {
    "duration_ms": 4520
  },
  "usage": {
    "input_tokens": 240,
    "output_tokens": 120,
    "total_tokens": 360,
    "cost_usd": null
  },
  "warnings": [],
  "error": null,
  "raw": {
    "parsed": {"status": "SUCCESS"},
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

## 🛡️ 安全与限额参数规范

| 指标 / 防线 | 配置值 / 策略 | 详细说明 |
| :--- | :--- | :--- |
| **会话隔离模型** | 显式 `session_id` / `conversation_id` | 客户端持有 ID；完全取消全局 `agy -c` 抢占 |
| **会话并发互斥** | `HTTP 409 Conflict` | 跨 Generic 与 Legacy 统一互斥保护 |
| **全局任务上限** | `AGY_MAX_CONCURRENCY` (默认 `1`) | 共享非阻塞信号量门禁，超额返回 `HTTP 429` |
| **任务执行预算** | 请求 `timeout_sec` 或 `ACP_AGENT_TIMEOUT_SEC` | 组合期限到期后通过 `os.killpg(pgid, SIGKILL)` 清理进程组 |
| **传输等待余量** | `+5.0 秒` | 外层 Future 等待窗口在任务超时上额外增加 5 秒 |
| **最大连接数** | `50` | `45 POST` 配额加 `5` 个通用 HTTP 连接槽，后 5 个并非 `/health` 专属 |
| **Socket 超时** | `10.0 秒` | 超过 10 秒无读写自动断开 Socket 链接 |
| **请求体限制** | `2 MB` | 超过 2MB 直接拦截并返回 `HTTP 413` |

---

## 📄 开源协议

MIT License © 2026 hikki-fan
