# ddtui-mcp：让其他 LLM 接管 ddtui 会话

`ddtui-mcp` 是一个 MCP（Model Context Protocol）stdio server，让 Claude Code、Codex CLI、Claude Desktop 或任何支持 MCP 的 agent 观察并控制活跃的 ddtui 会话。它不直接运行新 agent、不绕过 ddtui 的会话状态机——所有写入都走和远控网页相同的 `submit` / `steer` / `interrupt` 路径。

```
┌────────────┐  ws /ws/session   ┌─────────┐  ws /ws/control   ┌───────────┐  stdio   ┌──────────────┐
│ ddtui(TUI) │ ────────────────▶ │  relay  │ ◀──────────────── │ ddtui-mcp │ ◀──────▶ │ MCP 客户端    │
│  /remote on│                   │         │                   │  (bridge) │          │ (其他 LLM)   │
└────────────┘                   └─────────┘                   └───────────┘          └──────────────┘
```

三个进程角色：**ddtui** 主动外连 relay；**relay**（`ddtui-remote-server`）是中转站，可以跑在 VPS 上也可以跑在本机；**ddtui-mcp** 连 relay 的 control socket，把会话操作暴露成 MCP tools。

## 前置条件

```bash
pip install -e '.[remote]'     # websockets 依赖（relay 与 bridge 共用）
```

三方共用同一个 token（一行文本文件，默认 `~/.ddtui/remote_token`）：

```bash
mkdir -p ~/.ddtui
python -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.ddtui/remote_token
chmod 600 ~/.ddtui/remote_token
```

## 快速开始（纯本机）

```bash
# 1. 起 relay（本机回环即可）
ddtui-remote-server --host 127.0.0.1 --port 10000 --token-file ~/.ddtui/remote_token

# 2. ddtui 连上 relay（config.toml 里写 DDTUI_REMOTE_URL 也可以）
DDTUI_REMOTE_URL='ws://127.0.0.1:10000/ws/session' ddtui
#    然后在 TUI 里执行： /remote on

# 3. 起 MCP bridge（stdio，由 MCP 客户端拉起，见下节；手动验证可直接跑）
ddtui-mcp --relay ws://127.0.0.1:10000/ws/control --token-file ~/.ddtui/remote_token
```

VPS 场景只是把 `127.0.0.1` 换成 relay 所在主机；如果已经设置了 `DDTUI_REMOTE_URL`，或 `~/vps_addr.txt` 能推导出 relay 地址，`--relay` 可以省略（bridge 会把 session URL 自动改写成 control 路径）。

## 客户端注册

MCP 客户端负责拉起 `ddtui-mcp` 进程并通过 stdio 通信。

**Claude Code：**

```bash
claude mcp add ddtui -- ddtui-mcp --token-file ~/.ddtui/remote_token
```

**Codex CLI**（`~/.codex/config.toml`）：

```toml
[mcp_servers.ddtui]
command = "ddtui-mcp"
args = ["--token-file", "/Users/you/.ddtui/remote_token"]
```

**通用 JSON 配置**（Claude Desktop 等）：

```json
{
  "mcpServers": {
    "ddtui": {
      "command": "ddtui-mcp",
      "args": ["--token-file", "/Users/you/.ddtui/remote_token"]
    }
  }
}
```

## 工具参考

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `ddtui_list_sessions` | 列出 relay 上在线的 session | — |
| `ddtui_result` | 最轻量的"做完没有 + 结论"轮询 | `session_id`、`max_chars`（默认 4000） |
| `ddtui_observe` | 状态 + transcript tail + relay 事件 | `session_id`、`detail`、`max_messages`、`max_chars`、`max_events`、`after_seq` |
| `ddtui_submit` | 发送下一轮输入（忙时进 ddtui 排队） | `session_id`、`text` |
| `ddtui_steer` | 忙碌中的实时插话 | `session_id`、`text` |
| `ddtui_interrupt` | 请求中断当前回合 | `session_id` |
| `ddtui_terminal_read` | 只读 persistent terminal 输出 | `session_id`、`terminal_id`、`offset`、`max_chars` |

### observe 的三档 detail

控制者通常只需要"忙不忙、进展到哪、结论是什么"，不需要 ddtui 的完整工作记忆——一次 `read_file` 的原始返回就可能有 64K 字符。所以 observe 默认做上下文整形：

- **`status`**：不返回 transcript。顶层 `last_assistant` 字段（所有档位都有）已含最后一条 assistant 消息的截断版。
- **`chat`（默认）**：对话主线。user/assistant 的 content 按 `max_chars`（默认 800）截断；思考过程（`reasoning_content`）丢弃；工具往返折叠成一行占位——`[tool read_file → 64,000 chars]`、`[calls: bash, edit_file]`。默认取最后 `max_messages=20` 条。
- **`full`**：原文透传（仍受 `max_messages` 限制）。原始工具输出和完整参数都在里面，只在点名 debug 时用。

ddtui 侧的 snapshot 本身有两层前置削减：system 消息不外发、最多保留最后 200 条（`DDTUI_REMOTE_SNAPSHOT_MAX_MESSAGES`）。

### 推荐控制循环

```
ddtui_list_sessions
  → ddtui_submit(session_id, "任务描述")
  → ddtui_result 轮询（busy=false 且 last_assistant 更新 ⇒ 本轮完成）
  → 需要过程细节时 ddtui_observe(detail="chat")
  → 怀疑具体某步出错时 ddtui_observe(detail="full", max_messages=5)
  → 跑偏时 ddtui_steer（忙碌中）或 ddtui_interrupt
```

## 安全模型

- **认证**：单一共享 token，relay 对 session 和 control 两侧同源校验。token 文件建议 `chmod 600`。
- **写路径收敛**：外部模型能做的写操作只有 submit/steer/interrupt——和人类在远控网页上能做的完全一致，经过 ddtui 的排队/插话状态机，不存在直达工具层的通道。
- **`terminal_send` 不开放**：它等价于让外部模型往你的交互 shell 里打字。等 capability token 和审计日志落地后再考虑。
- **哲学边界**：ddtui 是 trusted local environment 工具，relay + token 是访问边界而非沙箱；不要把 relay 暴露在公网却使用弱 token。

## 配置项

`ddtui-mcp` 是独立进程，**只读环境变量和 CLI 参数**（不读 `~/.ddtui/config.toml`）：

| CLI | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `--relay` | `DDTUI_MCP_RELAY_URL` | 从 `DDTUI_REMOTE_URL` / `~/vps_addr.txt` 推导 | relay control WebSocket URL |
| `--token` | — | 空 | 直接传 token（优先于文件） |
| `--token-file` | `DDTUI_REMOTE_TOKEN_FILE` | `~/.ddtui/remote_token` | token 文件路径 |
| `--client-id` | `DDTUI_MCP_CLIENT_ID` | `mcp-<随机>` | 写进 relay command 信封的来源标识 |
| `--timeout` | `DDTUI_MCP_TIMEOUT` | `10` | relay 命令超时（秒） |

## 故障排查

- **`ddtui_list_sessions` 为空**：ddtui 没连上 relay——TUI 里 `/remote on` 后看状态栏；确认 `DDTUI_REMOTE_URL` 指向 `/ws/session` 路径。
- **bridge 报认证错误**：三方 token 不一致；确认 relay、ddtui、bridge 读的是同一个文件。
- **observe 拿到旧数据**：`refresh=true`（默认）会先向会话要一次新 snapshot；session 掉线时只剩缓存，看返回里的 `last_error`。
- **命令超时**：relay 在但 ddtui 会话没响应（比如正在 confirm 弹窗等待本地用户）；写操作都需要本地会话活着。
