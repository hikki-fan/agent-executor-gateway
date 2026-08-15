# Antigravity REST Bridge

[English](./README.md) | **中文文档**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.4.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Production--Ready-brightgreen.svg)]()

工业级、高可靠且安全的 **REST API & IPC 控制桥接服务**，专为 **Google Antigravity (`agy`) / OpenAI Codex** 显式 1:1 会话隔离与进程间通信而设计。

---

## 🌟 核心特性

- 🎯 **显式 1:1 会话映射与无状态网关**：完全取消全局 `agy -c` 最近会话抢占机制。每个 Codex 会话显式对应独立的 Antigravity `conversation_id`，天然多会话隔离。
- 🔒 **单会话并发互斥锁（Per-Conversation Lock）**：针对同一 `conversation_id` 的并发请求自动返回 `HTTP 409 Conflict` 保护，防止多轮状态机和 Transcript 污染。
- 🛑 **全局有界准入控制（HTTP 429）**：通过可配置的 `AGY_MAX_CONCURRENCY`（默认 `1`，保护代理链路）限制全局执行并发，超额立即返回 `HTTP 429 Too Many Requests`。
- ⏱️ **环境可配超时预算**：`ACP_AGENT_TIMEOUT_SEC`（默认 `300秒`）作为任务基础预算，另将 `ACP_AUTH_GRACE_SEC`（默认 `30秒`）加入进程期限以覆盖自动登录和前置初始化；默认进程总期限为 `330秒`，客户端超时为 `360秒`，超时仍会清理整个进程组。
- 🔁 **前置异常智能重试**：仅对任务未启动阶段的 transient 错误（如 `EOF`、网络重置等 0-turn 字典错误）进行最多 3 次快速重试；一旦任务开始执行（已消耗 Token / 已输出响应 / 已分配 `conversation_id`），绝不重试并原样返回供客户端决策。
- 🟡 **部分成功保留**：若 agy 已返回非空回复却把终态标为 `ERROR`，Bridge 返回 HTTP 200 和 `status: partial_success`，同时保留回复、原始错误及 CLI 退出码；没有回复的真实失败仍返回 HTTP 500。
- 🔐 **严格 Bearer Token 身份鉴权**：所有 POST API 操作均需通过 `Authorization: Bearer <TOKEN>` 认证（密钥文件采用 `0600` 属主独占权限）。
- ⚡ **专属配额 /health 探针**：为 `/health` 和状态心跳留有 5 个独立连接配额（响应延迟低至 0.001 秒），与重型 Agent 执行队列彻底解耦，绝不因业务繁忙被误判卡死。
- 🛡️ **慢连接与 Slowloris 防护**：配置了 Socket 单次 I/O 超时（10s）、请求体大小限制（2MB）以及 HTTP 总连接数上限（50 个 Socket）。

---

## 🏗️ 架构概览

```
[ Codex Session A ]        [ Codex Session B ]        [ 其他客户端 ]
         │                          │                       │
         ▼                          ▼                       ▼
  (conversation_id: A)      (conversation_id: B)     (无 conversation_id: 新建)
         │                          │                       │
         └──────────────────────────┼───────────────────────┘
                                    │
                                    ▼ (严格 Bearer Token 鉴权)
                    ┌─────────────────────────────────┐
                    │  Antigravity REST Bridge Server │  ── (监听端口 127.0.0.1:8765)
                    └─────────────────────────────────┘
                           │                   │
                    (快速 GET /health)    (重型 POST /invoke & /send-message)
                           │                   │
                           ▼                   ▼
                  ┌──────────────────┐ ┌──────────────────────────────┐
                  │  专属快速响应线程  │ │ 有界 Agent 任务池 (上限 N)    │
                  └──────────────────┘ │ 单会话互斥锁 (HTTP 409 保护)  │
                                       └──────────────────────────────┘
                                                       │
                                                       ▼
                                        ┌─────────────────────────────┐
                                        │ `agy --conversation <id>`   │
                                        │ 独立进程组 (无全局 agy -c 抢占)│
                                        └─────────────────────────────┘
                                                       ▲
                                                       │
                                   [ Watchdog 守护监视器 (PPID=1, TTY=?) ]
                                     (单例排他文件锁 & 5s 周期 /health 探测)
```

---

## 💡 会话生命周期与客户端职责规范

1. **客户端持有 `conversation_id`**：Bridge 采用无状态网关设计。首轮交互调用 `POST /acp/v1/invoke`（不带 `conversation_id`），Bridge 在成功响应中返回生成的 `conversation_id`。Codex 客户端必须在会话状态中持久记录该 ID。
2. **显式续接**：后续所有交互（`/invoke` 或 `/send-message`）必须显式传入 `conversation_id`。
3. **跨重启自愈**：Antigravity 会将对话历史以 `conversation_id` 为键持久化在本地磁盘。即使 Bridge 服务或容器重启，Codex 客户端只要携带原 `conversation_id` 即可无缝续接历史上下文。
4. **禁止同会话并发**：同一 Codex 会话严禁并发发送多个 Turn。若在上一轮生成结束前再次提交，Bridge 将立即返回 `HTTP 409 Conflict`。

---

## 🚀 快速开始与一键安装

### 一键脚本部署
```bash
./install.sh
```

### 命令行工具使用 (`acp-cli`)
```bash
# 查看桥接服务运行状态
acp-cli status

# 发起新 Agent 任务 (返回分配的 conversation_id)
acp-cli invoke "请协助审查 /workspace/processor.py 中的代码逻辑"

# 显式续接已有会话 (推荐使用无歧义 --conversation 选项，同时兼容位置参数)
acp-cli invoke --conversation <conversation_id> "继续修改测试用例"
acp-cli invoke <conversation_id> "继续修改测试用例"
acp-cli send <conversation_id> "确认，请继续执行"
```

---

## 📡 REST API 接口文档

### 1. 健康检查与状态探针 (`GET /health` 或 `GET /acp/v1/status`)
无需鉴权，专属快速响应线程秒级返回。
```bash
curl -s http://127.0.0.1:8765/health
```

### 2. 触发 / 继续 Agent 任务 (`POST /acp/v1/invoke`)
需要 Bearer Token 鉴权。

- **首次发起（新建会话）**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "执行代码重构"}'
```
响应示例：
```json
{
  "status": "success",
  "action": "new-conversation",
  "conversation_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b",
  "mode": "explicit_conversation_cli",
  "output": "..."
}
```

如果 agy 已生成可用回复，但 print mode 在结束阶段报告错误，Bridge 会同时保留两种事实：
```json
{
  "status": "partial_success",
  "conversation_id": "f4a0fc45-3d6c-9528-cdde2afcfa35",
  "warning": "agy reported ERROR after producing a non-empty response; review the response before relying on it",
  "upstream_status": "ERROR",
  "upstream_error": "Agent execution terminated due to error.",
  "cli_exit_code": 1,
  "parsed": {"response": "..."}
}
```

- **后续交互（继续已有会话）**：
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b", "prompt": "继续修改测试用例"}'
```

### 3. 推送对话消息 (`POST /acp/v1/send-message`)
需要 Bearer Token 鉴权。
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/send-message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recipient_id": "f4a0fc45-3d6c-462a-ab20-038a5fd8a04b", "content": "确认，继续执行下一阶段"}'
```

---

## 🛡️ 安全与限额参数规范

| 指标 / 防线 | 配置值 / 策略 | 详细说明 |
| :--- | :--- | :--- |
| **会话隔离模型** | 显式 `conversation_id` | 客户端持有 ID；完全取消全局 `agy -c` 抢占 |
| **单会话并发保护** | `HTTP 409 Conflict` | 针对同一会话并发提交时实施锁保护，防止状态破坏 |
| **全局并发任务上限** | `AGY_MAX_CONCURRENCY` (默认 `1`) | 采用非阻塞信号量门禁，超额返回 `HTTP 429` |
| **任务执行预算** | `ACP_AGENT_TIMEOUT_SEC` (默认 `300秒`) | 任务基础预算；组合期限到期后通过 `os.killpg(pgid, SIGKILL)` 清理进程组 |
| **自动登录宽限** | `ACP_AUTH_GRACE_SEC` (默认 `30秒`) | 为静默登录与前置初始化额外加入一次进程期限 |
| **客户端超时限制** | `ACP_CLIENT_TIMEOUT_SEC` (默认 `360秒`) | 默认等于任务预算 + 登录宽限 + 30 秒传输余量 |
| **最大总 HTTP 连接数** | `50` | `45 POST` + `5 Reserved /health` 配额隔离 |
| **Socket 空闲超时** | `10.0 秒` | 超过 10 秒无读写自动断开 Socket 链接 |
| **最大请求体限制** | `2 MB` | 超过 2MB 直接拦截并返回 `HTTP 413` |

---

## 📄 开源协议

MIT License © 2026 hikki-fan
