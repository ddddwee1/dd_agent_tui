我就是想有一个自己可以微操的，随时可以扩展的，说中文的deepseek。

随便做的一个，好玩。

# ddtui

一个基于 Textual 的终端 AI agent。目标是：中文优先、可微操、随时可扩展，并且能在 TUI 里直接看思考、工具调用、后台任务、TODO 和子 agent 状态。

> 目前还是自用/实验性质，接口和行为可能会继续变。

## 功能概览

- 中文优先的对话体验。
- 支持 DeepSeek thinking mode，以及 Codex OAuth provider。
- 流式展示思考过程和最终回答。
- 内置工具调用：bash、文件读写/编辑、搜索/glob、网页抓取、Brave Search、TODO、子 agent。
- 写文件/编辑文件前弹窗确认。
- 托管异步任务：后台/长任务用 `task_start`（写临时输出文件、任务完成后通知 agent），用 `task_check` / `task_read` / `task_wait` / `task_kill` / `task_list` 管理；侧栏有任务面板。
- 会话 checkpoint：记录当前目标、证据、决策、blocker、active refs 和下一步。
- 项目笔记：记录 repo-local runbook、坑点、验证过的命令和约定。
- 子 agent 异步并发：可 spawn 多个子会话，在侧边栏和 tab 中观察状态。
- 对话保存、加载、压缩、回退和重新思考。
- 自动读取项目根目录下的 `AGENTS.md` 作为项目级指令。

## 安装

要求 Python 3.10+。

开发模式安装：

```bash
git clone https://github.com/ddddwee1/dd_agent_tui.git
cd dd_agent_tui
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

也可以直接安装依赖后运行模块：

```bash
pip install -r requirements.txt
python -m ddtui
```

安装后会有命令：

```bash
ddtui
```

兼容入口：

```bash
python -m ddtui
python agent.py
```

## Provider 与配置

### DeepSeek（默认）

默认 provider 是 DeepSeek：

```bash
ddtui
```

API key 读取顺序：

1. `DEEPSEEK_API_KEY` 环境变量；
2. `DEEPSEEK_API_KEY_FILE` 指向的文件；
3. 默认文件 `~/deepseek_apikey.txt`。

常用环境变量：

```bash
export DEEPSEEK_API_KEY="sk-..."
export DEEPSEEK_MODEL="deepseek-v4-pro"
export DEEPSEEK_REASONING_EFFORT="max"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
```

### Codex OAuth

启动时选择 Codex OAuth：

```bash
DDTUI_PROVIDER=codex ddtui
```

Codex OAuth 会读取 `~/.codex/auth.json`，所以需要先运行过：

```bash
codex login
```

相关环境变量：

```bash
export CODEX_HOME="$HOME/.codex"
export CODEX_AUTH_FILE="$HOME/.codex/auth.json"
export CODEX_MODEL="gpt-5.5"
export CODEX_REASONING_EFFORT="xhigh"
```

### 运行中切换

在 TUI 内可以用：

```text
/provider
/provider deepseek
/provider codex
/model <model-id>
/effort <level>
```

切换 provider/model/effort 后，从下一轮请求开始生效。

## 基本使用

启动：

```bash
ddtui
```

进入 TUI 后直接输入问题，按 Enter 发送。

如果项目根目录存在 `AGENTS.md`，启动时会读取并加入 system message。可以把项目约定、编码规范、禁止修改的目录、测试命令等写进去。

## 快捷键

- `Enter`：发送当前输入。
- `Option+Enter`：插入换行。
- `Cmd+Enter`：实时插话 steer。
  - iTerm2 需要把 ⌘Return 配成 CSI u；macOS 自带 Terminal.app 不支持时，可以先 `Option+Enter` 换行再 Enter。
- `ESC × 2`：中断当前任务。
- `Ctrl+X`：取消排队消息 / steer。
- `Ctrl+T`：展开 / 折叠所有思考块。
- `Ctrl+L`：清空当前对话。
- `Ctrl+C`：退出。

## Slash 命令

- `/clear`：清空对话，保留 system prompt 和 `AGENTS.md`。
- `/compact`：压缩历史，保留最近两轮原文。
- `/save <name>`：保存到 `~/.ddtui/history/<name>.json`。
- `/load <name>`：读回保存的对话。
- `/resume [name]`：恢复保存的对话；不带名字时弹出选择窗口。
- `/remote [status|on [url]|off|snapshot]`：管理 VPS 远程控制连接。
- `/list-history`：列出已保存对话。
- `/provider [deepseek|codex]`：查看 / 切换 provider。
- `/model [<id>]`：查看 / 切换模型。
- `/effort [<level>]`：查看 / 切换 reasoning effort。
- `/rewind`：退回上一条用户消息，并把文本回填到输入框。
- `/rethink`：删除最近一轮助手回复，让模型重新思考。
- `/help`：显示内置帮助。
- `/exit` / `/quit`：退出。

## 远程控制

远控是三段式结构：本地活跃 ddtui session 主动连到 VPS relay，浏览器登录 relay 前端后订阅并控制这个 session。VPS 不运行 agent，也不直接读写你的本机文件；它只做 WebSocket 转发、在线 session 列表和短期事件缓存。

```text
Browser Web UI  <->  VPS relay  <->  local ddtui session
```

安装远控依赖：

```bash
pip install -e '.[remote]'
# 或
pip install -r requirements-remote.txt
```

生成/设置一个登录 token。本地 `/remote on` 如果找不到 token，会自动写入 `~/.ddtui/remote_token`；VPS relay 也需要同一个 token。

在 VPS 上启动 relay：

```bash
export DDTUI_REMOTE_TOKEN='replace-with-a-long-random-token'
ddtui-remote-server --host 0.0.0.0 --port 10000
```

VPS 端口建议使用 `10000~10010` 这一段；默认是 `10000`，如果这个端口被占用，可以在同一段里换一个端口，并让本地 `DDTUI_REMOTE_PORT` / `/remote on <url>` 保持一致。

### VPS 部署 / 更新流程

下面是一套直接可用的部署流程。假设 `~/vps_addr.txt` 里写着 SSH 目标，例如：

```text
root@<vps-host>
```

本地先准备登录 token；本地 ddtui 和 VPS relay 必须使用同一个 token：

```bash
mkdir -p ~/.ddtui
test -s ~/.ddtui/remote_token || python3 - <<'PY'
from pathlib import Path
import secrets
p = Path.home() / ".ddtui" / "remote_token"
p.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
p.chmod(0o600)
PY
```

把当前工作树和 token 同步到 VPS：

```bash
ssh -o StrictHostKeyChecking=accept-new "$(cat ~/vps_addr.txt)" \
  'mkdir -p /root/dd_agent_tui /root/.ddtui && chmod 700 /root/.ddtui'

rsync -az --delete \
  --exclude .git --exclude .venv --exclude '__pycache__' --exclude '*.pyc' \
  -e 'ssh -o StrictHostKeyChecking=accept-new' \
  ./ "$(cat ~/vps_addr.txt)":/root/dd_agent_tui/

scp -q ~/.ddtui/remote_token \
  "$(cat ~/vps_addr.txt)":/root/.ddtui/remote_token

ssh "$(cat ~/vps_addr.txt)" 'chmod 600 /root/.ddtui/remote_token'
```

首次部署时，在 VPS 上创建 venv 并安装远控依赖。Ubuntu 如果缺 `ensurepip`，先装对应的 `python3.12-venv`：

```bash
ssh "$(cat ~/vps_addr.txt)" '
  cd /root/dd_agent_tui &&
  (python3 -m venv .venv || (apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12-venv && python3 -m venv .venv)) &&
  . .venv/bin/activate &&
  python -m pip install -U pip &&
  python -m pip install -e ".[remote]"
'
```

用 tmux 常驻启动 relay：

```bash
ssh "$(cat ~/vps_addr.txt)" '
  if tmux has-session -t ddtui-remote 2>/dev/null; then
    tmux kill-session -t ddtui-remote
  fi
  tmux new-session -d -s ddtui-remote "
    cd /root/dd_agent_tui &&
    . .venv/bin/activate &&
    export DDTUI_REMOTE_TOKEN_FILE=/root/.ddtui/remote_token &&
    ddtui-remote-server --host 0.0.0.0 --port 10000
  "
  sleep 1
  tmux clear-history -t ddtui-remote || true
  ss -ltnp | grep ":10000\b" || true
'
```

检查服务：

```bash
curl -fsS http://<vps-host>:10000/healthz
ssh "$(cat ~/vps_addr.txt)" 'tmux ls; tmux capture-pane -pt ddtui-remote -S -40'
```

常用运维命令：

```bash
# 看 relay 日志
ssh "$(cat ~/vps_addr.txt)" 'tmux capture-pane -pt ddtui-remote -S -80'

# 进入 tmux
ssh "$(cat ~/vps_addr.txt)" 'tmux attach -t ddtui-remote'

# 停止 relay
ssh "$(cat ~/vps_addr.txt)" 'tmux kill-session -t ddtui-remote'
```

然后浏览器打开：

```text
http://<vps-host>:10000/
```

本地 session 连接 relay。可以显式传 URL：

```text
/remote on ws://<vps-host>:10000/ws/session
```

也可以让 ddtui 从 `~/vps_addr.txt` 推导默认地址；例如文件内容是 `root@<vps-host>` 时，默认会推导成 `ws://<vps-host>:10000/ws/session`：

```text
/remote on
```

常用环境变量：

```bash
export DDTUI_REMOTE_URL='ws://<vps-host>:10000/ws/session'
export DDTUI_REMOTE_TOKEN='replace-with-a-long-random-token'
export DDTUI_REMOTE_AUTO=1
export DDTUI_REMOTE_PORT=10000
export DDTUI_REMOTE_TLS=0
```

网页前端第一版支持：

- 登录 token。
- 查看在线 session 列表、cwd、model、busy/idle、queue/steer 数。
- 订阅 transcript/status/streaming delta/tool started/terminal tail。
- 发送普通输入、steer 实时插话、interrupt。
- 读取 terminal 输出。

断线行为：

- 本地 ddtui session 到 VPS relay 会自动重连，网络短断后通常不需要重新输入 `/remote on`。
- 浏览器网页到 VPS relay 也会自动重连；重连后会刷新在线 session 列表，并对当前 session 重新订阅和拉取 snapshot。
- 手动点击网页 `Logout`，或在本地执行 `/remote off`，会主动停止对应连接，需要之后手动重新登录或 `/remote on`。

远程 `terminal_send` 和 `terminal_interrupt` 默认关闭，因为它相当于远程向交互 shell 输入命令。确实需要时，在本地 ddtui 进程设置：

```bash
export DDTUI_REMOTE_ALLOW_TERMINAL_SEND=1
```

安全建议：relay 前面最好放 HTTPS/WSS（Caddy/Nginx 均可），token 使用长随机串；不需要远控时用 `/remote off` 断开。

## 给其他大模型接入

`ddtui-mcp` 是一个 MCP stdio bridge，让 Claude、Codex、其他支持 MCP 的 agent 或本地脚本通过现有 VPS relay 观察和控制活跃 ddtui session。它不直接运行新的 agent，也不绕过 ddtui 的会话状态机；所有写入都会走和远控网页一样的 `submit` / `steer` / `interrupt` 路径。

先安装远控依赖：

```bash
pip install -e '.[remote]'
```

然后启动 MCP server。`--relay` 指向 relay 的 control socket，token 和本地 `/remote on` 使用同一个：

```bash
ddtui-mcp \
  --relay ws://<vps-host>:10000/ws/control \
  --token-file ~/.ddtui/remote_token
```

如果已经设置了 `DDTUI_REMOTE_URL`，或者 `~/vps_addr.txt` 可以推导出 relay 地址，也可以省略 `--relay`：

```bash
ddtui-mcp --token-file ~/.ddtui/remote_token
```

暴露给外部模型的 MCP tools：

- `ddtui_list_sessions`：列出当前在线 session。
- `ddtui_observe`：读取 session 状态、transcript tail 和最近 relay 事件。
- `ddtui_submit`：发送普通下一轮输入；如果本地 session 正忙，会进入 ddtui 的 queued input。
- `ddtui_steer`：发送忙碌中的实时插话。
- `ddtui_interrupt`：请求中断当前回合。
- `ddtui_terminal_read`：只读 persistent terminal 输出。

MVP 默认不开放 `terminal_send`。它等价于让外部模型往交互 shell 里打字，后续会单独做 capability token 和审计日志后再考虑打开。

## 工具能力

模型可以调用这些工具：

- shell（同步一次性）：`bash`
- 交互式 Terminal：`terminal_start`、`terminal_send`、`terminal_read`、`terminal_interrupt`、`terminal_close`、`terminal_list`
- 托管异步任务：`task_start`、`task_check`、`task_read`、`task_wait`、`task_kill`、`task_list`
- 临时探索：`explore_start`、`explore_end`、`explore_cancel`
- 会话 checkpoint：`checkpoint_tool`、`checkpoint_get`、`checkpoint_clear`
- 项目笔记：`project_note_add`、`project_note_search`、`project_note_list`、`project_note_read`、`project_note_update`、`project_note_delete`
- 文件：`read_file`、`write_file`、`edit_file`、`edit_lines`、`multi_edit`
- 搜索：`list_files`、`glob_files`、`search_content`
- Web：`web_fetch`、`web_search`
- 任务进度：`todo_tool`
- 子 agent：`spawn_agent`、`chat_agent`、`await_agent`、`end_agent`

### 交互式 Terminal

`terminal_start` 会启动一个常驻 PTY，并在顶部 tab 里展示实时输出。适合需要保持上下文的交互会话，例如 `ssh`、远端 `tmux`、REPL、debugger 或交互安装器。连续远端操作时，agent 应优先开一个 terminal，然后用 `terminal_send` 往里面输入命令，而不是反复执行 `ssh host "cmd"`。

推荐远端长会话用 tmux，例如：

```bash
ssh -tt host 'tmux new -A -s ddtui-agent'
```

非交互的长测试、构建、下载、训练、profile 仍优先用 `task_start`，这样可以自动通知完成状态。

### 托管异步任务

命令执行只有两种形态：同步快命令用 `bash`（前台跑完直接返回，≤120s）；需要放后台或耗时较长（测试、构建、下载、训练、profile 等完成状态很重要的工作）一律用 `task_start`。是否收完成通知由 `notify_on_complete` 决定——`true`（默认）在结束时推送异步通知，`false` 则是纯后台、由你自己 `task_check` / `task_wait`。

`task_start` 会返回 `task_id`、`output_path` 和 `status_path`。任务与 terminal 的输出/状态文件写在 `~/.ddtui/runtime/logs/`（目录 `chmod 700`，不再放世界可读的 `/tmp`；可用 `DDTUI_LOG_DIR` 覆盖）。agent 可以用 `task_check` 看 tail、用 `task_read` 按 offset 增量读输出，但它们只用于一次性检查当前输出是否会改变下一步行动。默认 `notify_on_complete=true`，任务完成后会向 agent 发送异步完成通知。严格规则：agent 可以在等待期间继续做别的事；做完其他事后不要调用 `task_wait`，也不要用反复 `task_check` / `task_read` 轮询来替代等待，而是记录 checkpoint（如有帮助）并暂停当前回复，等待通知自动唤醒。`task_wait` 正常用于 `notify_on_complete=false` 的任务；对 `notify_on_complete=true` 只应在用户明确要求阻塞，或任务明显快结束且只做一次短暂有界等待时使用。

### 会话 checkpoint

`todo_tool` 管执行清单，`checkpoint_tool` 管当前工作状态，`checkpoint_clear` 收回当前 checkpoint，`project_note_*` 管长期项目知识。checkpoint 会替换上一份状态，不是追加日志。等待中的 checkpoint 如果只引用了已完成 task，task 完成通知也会自动收回它。

适合在长任务、并行 task/subagent、复杂 debug、多假设推理、等待异步通知、准备暂停当前回复、或 `/resume` 后状态不确定时使用。`checkpoint_get` 可以显式读取当前 checkpoint。checkpoint 不单独落文件，但会进入对话历史并跟随 autosave；恢复会话时会从最新一次 `checkpoint_tool` 重建侧边栏。

### 项目笔记

项目笔记默认保存在当前 repo 的：

```text
.ddtui/notes/notes.json
```

也可以用 `DDTUI_PROJECT_NOTES_DIR` 指到个人目录。笔记用于记录可复用的项目事实，例如测试命令、环境设置、远端连接、架构约定、坑点和用户确认过的决策。agent 不确定项目特定流程时会优先 `project_note_search`，但仍应根据 `source` / `confidence` 判断是否需要再验证。

为了控制体量，相关旧笔记应该用 `project_note_update` 合并更新，而不是反复新增近似重复 note。`project_note_delete` 是软删除，会把 note 标为 `archived=true`；普通 search/list 默认隐藏 archived note。不要保存 token、密码、API key、private key 或大段原始日志。

### 子 agent

子 agent 是异步的：

- `spawn_agent` 创建会话并立即返回 `session_id`，后台继续跑。可选 `model` / `effort` 参数按会话覆盖（仅限当前 provider 的模型），搜索/只读类任务可以指定更便宜的模型。
- `chat_agent` 给已有会话发送下一轮消息，也立即返回。
- `await_agent(session_id, timeout?)` 立刻获取结果；超时不会取消子 agent。
- 不 await 也可以：子 agent 完成后，未读结果会像任务完成通知一样**自动投递**给父 agent（`[Subagent result]` 消息）——父 agent 忙时在下一次模型调用边界注入，空闲时自动唤醒新回合。
- `end_agent(session_id)` 释放会话并清理后台 bash 任务。

子 agent 使用独立的精简 system prompt——它知道自己是子 agent、结果会截断回传。项目级 `AGENTS.md` 和环境信息仍会注入，与父 agent 遵循同样的项目约定。

子 agent 的工具面：有 bash、文件、搜索、web、todo、项目笔记、`compact_self`，**也有 `task_*` 和 `terminal_*`**——但任务完成通知只投递给父 agent，子 agent 的任务强制为纯后台模式（`notify_on_complete=false`），需要自己 `task_wait` / `task_check`；子 agent 的 terminal 没有顶部 tab 展示。没有 checkpoint（其展示/恢复/自动收回都只挂在父会话上）、没有 explore（其实现操作父对话历史）、不能嵌套子 agent。

限制：

- 同时最多 5 个 live subagent。
- 单轮最多 30 个 LLM/tool 循环。
- 返回给父对话的子 agent 结果会截断到 20,000 字符。
- 闲置 10 分钟自动回收。
- 子 agent 不能再嵌套调用子 agent。

### Web Search

`web_search` 使用 Brave Search API。默认从 `~/brave_apikey.txt` 读取 key，也可以设置：

```bash
export BRAVE_API_KEY_FILE="$HOME/brave_apikey.txt"
```

如果没有 key，工具会返回清晰错误，不会让 TUI 崩溃。

## 工具安全说明

这不是强安全沙箱，但默认做了一些防护，避免模型误操作。

### 写操作确认

以下工具执行前会弹窗确认：

- `write_file`
- `edit_file`
- `edit_lines`
- `multi_edit`

弹窗内可以选择：

- `Y` 允许本次调用；
- `A` 本会话内总是允许该工具（按工具名记忆，`/clear` 不重置，重启后失效）；
- `N` / `Esc` 拒绝。拒绝后，模型会收到“工具调用被用户策略/确认弹窗拦截”的结果。

父 agent 和子 agent 的写操作共用同一套确认；并发确认会排队逐个弹出，不会叠窗。注意：远程（`/remote`）会话触发的写操作也会等待**本地**弹窗确认。

另外，`write_file` 覆盖**已存在**文件前要求模型本会话内先 `read_file` 读过该文件（或刚编辑过它），且文件此后未被外部修改；否则会被拒绝并提示先读。新建文件不受影响。这可以防止模型在没看过旧内容的情况下整文件覆盖掉你的东西。

如果确认弹窗打扰到你（完全信任本机环境），可以关闭：

```bash
export DDTUI_CONFIRM_WRITES=0
```

### 路径 sandbox

默认情况下：

- 写文件/编辑文件可以作用在任意路径（包括项目外）。
- `bash` / `task_start` / `terminal_start` 的 workdir 也必须在项目目录内。

如果你想恢复旧行为，只允许项目目录内写入/编辑：

```bash
export DDTUI_ALLOW_UNSANDBOXED_WRITES=0
```

如果你明确想关闭 shell workdir 的限制：

```bash
export DDTUI_ALLOW_UNSANDBOXED_BASH=1
```

### bash 危险命令拦截

ddtui 假设运行在可信的本地环境，**不**试图用黑名单去防御恶意 agent（黑名单也做不到）。
`bash`、`task_start`、`terminal_start` 只拦截极少数灾难性、不可逆、几乎不会
是有意为之的命令，避免模型幻觉出的一条命令把机器抹掉或重新格式化。日常工具（curl、wget、
sudo 等）不再拦截。当前只拒绝：

- `rm -rf /`（整盘清空）
- `mkfs`（重新格式化文件系统）
- `dd ... of=/dev/*`（直接写裸设备）

### 默认忽略缓存/构建目录

文件枚举、glob、搜索默认跳过常见噪音目录/文件，例如：

- `.git`
- `__pycache__`
- `.pytest_cache`
- `.mypy_cache`
- `.ruff_cache`
- `.venv`、`venv`
- `node_modules`
- `build`、`dist`
- `*.egg-info`

## 对话历史

`/save <name>` 会保存到：

```text
~/.ddtui/history/<name>.json
```

`/load <name>` 可以恢复对话并重建聊天视图。保存文件只包含 message 历史和少量元信息，不包含原始 token usage；加载后 token 计数会重置。

新会话启动时会创建独立的自动保存文件；每轮结束、回到可输入状态时会自动写入当前对话。`/resume` 不带文件名会打开选择窗口，列表按文件最后编辑时间排序，并显示到年月日小时分钟。

断线恢复使用 `~/.ddtui/runtime/<session_id>/` 记录当前回合和托管异步任务。重启后仍默认进入新会话，但如果检测到未完成回合，会提示用 `/resume <name>` 接回。恢复时，断在模型流式输出阶段的回合会回滚到上一条稳定消息，并把用户输入放回输入框；断在工具执行阶段不会自动重跑工具，而是补一个未知结果占位，避免重复执行有副作用的操作。`task_start` 创建的托管任务由独立 runner 负责写最终状态，恢复会话后可以重新出现在侧边栏并补发完成通知。

`/clear` 会开始一个新的自动保存会话，不会把刚清空的内容写回旧会话文件。

`/compact` 会把较早历史压缩成一个摘要 system message，保留最近两轮原文，适合长对话里降低上下文压力。

上下文压力还有自动兜底：某个回合结束时，如果最近一次请求的 prompt tokens 超过了模型上下文窗口的 95%，会自动执行同样的压缩并在对话里提示。阈值用 `DDTUI_AUTO_COMPACT_THRESHOLD` 调整（0 到 0.95 之间的小数），设为 `0` 关闭。自动压缩只发生在回合完全结束、无排队消息的安静时刻；explore 进行中会跳过。压缩失败不影响对话，可稍后手动 `/compact`。

`explore_start` / `explore_end` 让 agent 把一段临时探索从主上下文里收束掉：适合单点功能探针、bug 定位、假设验证、代码考古、方案侦察、低密度资料搜索、环境检查、性能/数值实验、测试面发现、数据样本检查、日志聚类和风险预检。`explore_end` 会把 raw 过程归档到 `~/.ddtui/explorations/<session_id>/<explore_id>.json`，并在对话里留下一个探索摘要 system message；`explore_cancel` 则取消边界，不压缩历史。

## 开发结构

主要模块：

- `ddtui/engine.py`：UI 无关的 `TurnEngine`——stream → append → dispatch 的 LLM 工具循环，父 agent 和子 agent 共用；宿主通过 `TurnObserver` 回调订阅渲染。相邻的只读工具调用会并发执行。
- `ddtui/app.py`：Textual app shell、布局和入口。
- `ddtui/app_agent_loop.py`：父 agent 的 engine 宿主（Textual observer、meta 工具 handler、外层回合驱动）。
- `ddtui/app_*.py`：输入、历史、subagent、UI 生命周期、确认弹窗等 mixin/helper。
- `ddtui/tools.py`：统一工具注册表（`ToolSpec`）+ dispatch。每个工具在注册表声明一次元数据（是否需要写确认、结果是否剥离 diff、是否并行安全、父/子 agent 可见性），所有派生清单自动生成。
- `ddtui/tool_schemas.py`：发给模型看的 JSON schemas。
- `ddtui/tools_*.py`：按领域拆分的工具实现。
- `ddtui/providers.py`：DeepSeek / Codex provider adapter；`attach_reasoning` 抽象各家 thinking 的消息格式。
- `ddtui/widgets.py`：Textual widgets。
- `ddtui/state.py`：运行时状态、token counter、后台 bash job 状态。
- `ddtui/config.py`：默认值、路径、限制和 system prompt。

基础校验：

```bash
pip install -e '.[dev]'
python -m pytest tests/ -q     # 回归测试（glob 语义、编辑守卫、engine、端到端等）
python -m compileall ddtui agent.py
```

## 许可证

MIT License，见 [LICENSE](LICENSE)。

## 当前状态

还很早期，但已经能作为一个中文优先、可微操的终端 agent 使用。项目已迁移到 `pyproject.toml`，后续可能会继续补测试和整理发布流程。
