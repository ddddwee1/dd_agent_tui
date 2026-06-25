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
- bash 后台任务面板：长任务可以 `bash_start` 后用 `bash_check` / `bash_wait` / `bash_kill` 管理。
- 托管异步任务：`task_start` 会写临时输出文件，并在任务完成后通知 agent。
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
- `/list-history`：列出已保存对话。
- `/provider [deepseek|codex]`：查看 / 切换 provider。
- `/model [<id>]`：查看 / 切换模型。
- `/effort [<level>]`：查看 / 切换 reasoning effort。
- `/rewind`：退回上一条用户消息，并把文本回填到输入框。
- `/rethink`：删除最近一轮助手回复，让模型重新思考。
- `/help`：显示内置帮助。
- `/exit` / `/quit`：退出。

## 工具能力

模型可以调用这些工具：

- shell：`bash`、`bash_start`、`bash_check`、`bash_wait`、`bash_kill`、`bash_list`
- 交互式 Terminal：`terminal_start`、`terminal_send`、`terminal_read`、`terminal_interrupt`、`terminal_close`、`terminal_list`
- 托管异步任务：`task_start`、`task_check`、`task_read`、`task_wait`、`task_kill`、`task_list`
- 会话 checkpoint：`checkpoint_tool`、`checkpoint_get`、`checkpoint_clear`
- 项目笔记：`project_note_add`、`project_note_search`、`project_note_list`、`project_note_read`、`project_note_update`、`project_note_delete`
- 文件：`read_file`、`write_file`、`apply_patch`、`edit_file`、`edit_lines`、`multi_edit`
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

`bash_start` 是原始后台 shell，适合简单后台进程；如果是测试、构建、下载、训练、profile 等完成状态很重要的长任务，优先用 `task_start`。

`task_start` 会返回 `task_id`、`output_path` 和 `status_path`。agent 可以用 `task_check` 看 tail、用 `task_read` 按 offset 增量读输出，但它们只用于一次性检查当前输出是否会改变下一步行动。默认 `notify_on_complete=true`，任务完成后会向 agent 发送异步完成通知。严格规则：agent 可以在等待期间继续做别的事；做完其他事后不要调用 `task_wait`，也不要用反复 `task_check` / `task_read` 轮询来替代等待，而是记录 checkpoint（如有帮助）并暂停当前回复，等待通知自动唤醒。`task_wait` 正常用于 `notify_on_complete=false` 的任务；对 `notify_on_complete=true` 只应在用户明确要求阻塞，或任务明显快结束且只做一次短暂有界等待时使用。

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

- `spawn_agent` 创建会话并立即返回 `session_id`，后台继续跑。
- `chat_agent` 给已有会话发送下一轮消息，也立即返回。
- `await_agent(session_id, timeout?)` 获取结果；超时不会取消子 agent。
- `end_agent(session_id)` 释放会话并清理后台 bash 任务。

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
- `apply_patch`
- `edit_file`
- `edit_lines`
- `multi_edit`

拒绝后，模型会收到“工具调用被用户策略/确认弹窗拦截”的结果。

### 路径 sandbox

默认情况下：

- 写文件/编辑文件可以作用在任意路径（包括项目外）。
- `bash` / `bash_start` / `task_start` 的 workdir 也必须在项目目录内。

如果你想恢复旧行为，只允许项目目录内写入/编辑：

```bash
export DDTUI_ALLOW_UNSANDBOXED_WRITES=0
```

如果你明确想关闭 shell workdir 的限制：

```bash
export DDTUI_ALLOW_UNSANDBOXED_BASH=1
```

### bash 危险命令拦截

`bash`、`bash_start` 和 `task_start` 会拒绝一些明显危险模式，例如：

- `sudo`
- `curl`
- `wget`
- `chmod 777`
- `mkfs`
- `dd if=`
- `rm -rf /`

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

## 开发结构

主要模块：

- `ddtui/app.py`：Textual app shell、布局和入口。
- `ddtui/app_*.py`：输入、历史、主 agent loop、subagent、UI 生命周期、确认弹窗等 mixin/helper。
- `ddtui/tools.py`：工具 facade 和 dispatch。
- `ddtui/tool_schemas.py`：发给模型看的 JSON schemas。
- `ddtui/tools_*.py`：按领域拆分的工具实现。
- `ddtui/providers.py`：DeepSeek / Codex provider adapter。
- `ddtui/widgets.py`：Textual widgets。
- `ddtui/state.py`：运行时状态、token counter、后台 bash job 状态。
- `ddtui/config.py`：默认值、路径、限制和 system prompt。

基础校验：

```bash
python -m compileall ddtui agent.py
```

## 许可证

MIT License，见 [LICENSE](LICENSE)。

## 当前状态

还很早期，但已经能作为一个中文优先、可微操的终端 agent 使用。项目已迁移到 `pyproject.toml`，后续可能会继续补测试和整理发布流程。
