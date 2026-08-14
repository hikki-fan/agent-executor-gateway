# Antigravity REST Bridge

[English](./README.md) | **中文文档**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/Version-2.1.0-blue.svg)]()
[![Status](https://img.shields.io/badge/Status-Production--Ready-brightgreen.svg)]()

工业级、高可靠且安全的 **REST API & IPC 控制桥接服务**，专为 **Google Antigravity (`agy`) / OpenAI Codex** Agent 进程间通信与自动化控制而设计。

---

## 🌟 核心特性

- 🔐 **严格 Bearer Token 身份鉴权**：所有 POST API 操作均需通过 `Authorization: Bearer <TOKEN>` 认证（密钥文件采用 `0600` 属主独占权限）。
- ⚡ **专属配额 /health 探针**：为 `/health` 和状态心跳留有 5 个独立连接配额（响应延迟低至 0.001 秒），与重型 Agent 执行队列彻底解耦，绝不因业务繁忙被误判卡死。
- 🛑 **有界准入控制（HTTP 429 反压）**：当并发 Agent 任务达到 10 个上限时，自动通过非阻塞信号量拒绝超额任务并返回 `HTTP 429 Too Many Requests`，拒绝无界无限排队。
- 🛡️ **慢连接与 Slowloris 防护**：配置了 Socket 单次 I/O 超时（10s）、请求体大小限制（2MB）以及 HTTP 总连接数上限（50 个 Socket）。
- 🧹 **进程组树完整清理**：通过独立的 Process Group 进程组隔离（`start_new_session=True` + `os.killpg`），确保 Agent 命令超时后 100% 干净杀死子进程及其所有脱离后代。
- 🔄 **消除死锁 SIGTERM 与自愈 Watchdog**：异步信号处理（避免 Python `BaseServer.shutdown()` 主线程死锁）+ `cancel_futures=True`（带有 65s 优雅退场缓冲区），结合脱离 TTY 终端的单例文件锁 Watchdog 持续监控自愈。

---

## 🏗️ 架构概览

```
[ 外部客户端 / Codex CLI / 第三方 Agent ]
                   │
                   ▼
      [ acp-cli / HTTP REST API ]
                   │
       (严格 Bearer Token 身份鉴权)
                   │
                   ▼
  ┌─────────────────────────────────┐
  │  Antigravity REST Bridge Server │  ── (监听端口 127.0.0.1:8765)
  └─────────────────────────────────┘
         │                   │
  (快速 GET /health)    (重型 Agent 任务 POST)
         │                   │
         ▼                   ▼
┌──────────────────┐ ┌──────────────────────────────┐
│  专属快速响应线程  │ │ 有界 Agent 任务线程池 (上限10) │
└──────────────────┘ └──────────────────────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │ `agy agentapi` 独立进程组   │
              └─────────────────────────────┘
                             ▲
                             │
         [ Watchdog 守护监视器 (PPID=1, TTY=?) ]
           (单例排他文件锁 & 5s 周期心跳)
```

---

## 🚀 快速开始与一键安装

### 一键脚本部署
```bash
./install.sh
```

### 命令行工具使用 (`acp-cli`)
```bash
# 查看桥接服务与 Watchdog 运行状态
acp-cli status

# 发起新 Agent 任务
acp-cli invoke "请协助审查 /workspace/processor.py 中的代码逻辑"

# 向指定对话发送交互消息
acp-cli send <conversation_id> "确认，请继续执行"
```

---

## 📡 REST API 接口文档

### 1. 健康检查与状态探针 (`GET /health` 或 `GET /acp/v1/status`)
无需鉴权，专属快速响应线程秒级返回。
```bash
curl -s http://127.0.0.1:8765/health
```

### 2. 触发 Agent 任务 (`POST /acp/v1/invoke`)
需要 Bearer Token 鉴权。
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/invoke \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "执行代码重构"}'
```

### 3. 推送对话消息 (`POST /acp/v1/send-message`)
需要 Bearer Token 鉴权。
```bash
TOKEN=$(cat ~/.codex/acp_token)
curl -s -X POST http://127.0.0.1:8765/acp/v1/send-message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"recipient_id": "YOUR_CONVERSATION_ID", "content": "继续执行下一阶段"}'
```

---

## 🛡️ 安全与限额参数规范

| 指标 / 防线 | 配置值 / 策略 | 详细说明 |
| :--- | :--- | :--- |
| **最大并发 Agent 任务** | `10` | 采用 `BoundedSemaphore(10)` 进行门禁拦截 |
| **任务超额策略** | `HTTP 429` | 超额非阻塞拒绝，立即返回 Too Many Requests |
| **最大总 HTTP 连接数** | `50` | `45 POST` + `5 Reserved /health` 配额隔离 |
| **Socket 空闲超时** | `10.0 秒` | 超过 10 秒无读写自动断开 Socket 链接 |
| **Agent 子进程超时** | `60.0 秒` | 通过 `os.killpg(pgid, SIGKILL)` 清理整个进程组 |
| **最大请求体限制** | `2 MB` | 超过 2MB 直接拦截并返回 `HTTP 413` |
| **SIGTERM 优雅等待窗口**| `65.0 秒` | 允许 60 秒存量运行任务优雅退场 |

---

## 📄 开源协议

MIT License © 2026 hikki-fan
