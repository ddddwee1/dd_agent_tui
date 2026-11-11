# Git 协作插件

该插件将 ddtui 的 Git 交接服务接到 Codex，使用 skill 和 stdio MCP 工具；CLI、TUI 和插件共用 `DDTUI_COLLAB_HOME` 中的数据库、任务身份及投递状态。跨机通信只经过用户指定的干净 Git 交接仓。

## 安装

在 ddtui 仓库根目录使用 Python 3.11+ 安装共享核心，再注册本地 marketplace：

```bash
python -m pip install -e .
command -v ddtui-collab-mcp
codex plugin marketplace add /absolute/path/to/dd_agent_tui
codex plugin add git-collab@ddtui-local --json
```

`ddtui-collab-mcp` 必须在 Codex 本地执行环境的 PATH 中；console entry 使用安装该包的解释器，不依赖系统 `python3` 的版本。自动续跑另需 `codex` 可执行文件及本机已保存的登录状态，不将凭据复制到插件或交接仓。当前控制路径已用 Codex CLI 0.161.0 验证。

插件同时提供根目录的 Agent Plugins manifest 和 `.codex-plugin/` 兼容 manifest。它要求本地执行能力，不依赖 hooks，也不因 MCP 断开而停止共享服务。安装完成后在新的 Codex 聊天中使用；桌面客户端的缓存与插件加载时机由客户端控制。

## 使用

向 Codex 明确指定交接仓、任务的源码 commit、允许路径、worker 与验证要求。插件默认使用 `coordinator` 绑定，先调用 `collab_bind`，再显式开启 `collab_listening`。`collab_publish_task` 发布不可变修订；`collab_list_results` 返回最近最多 128 个结果，`collab_get_result` 读取指定事件，`collab_ack_result` 确认已审查的结果。

用户授权自动推进后，按任务调用 `collab_codex_enable` 并设置有限的 timeout 和 max_turns。每个任务使用独立 Codex CLI 会话，后续结果恢复该会话；它审查结果并生成固定范围内的下一步任务，不接管任意已有桌面聊天。`collab_codex_pause` 与 `collab_codex_resume` 保留任务上下文和剩余预算；`collab_codex_status` 显示执行与待核对状态。

源码候选、任务/修订和接收确认由共享核心核对。结果属于外部证据，`completed` 或 `processed` 均不代表 kernel 官方验收。硬件、环境、checker、精度阈值、实验预算和上传授权沿用原任务契约。远端 ddtui 会话继续通过 `/collab task resume <task-id>` 领取、`pause` 释放，各自使用独立目录并遵守项目实际设备锁。

收到 `needs_attention` 时检查协作 home 下私有输出和在途进程。确认先前执行停止并核对其输出后，才使用 `collab_codex_retry` 的 `previous_stopped=true`；也可人工审查并确认结果。不因网络重试或会话退出重复实验。完整命令、结果格式和运行边界见[仓库 README](../../README.md#可选-git-任务交接)。

## 接口与验证

MCP server 暴露 13 个工具：绑定、监听、状态、任务列表、任务发布、结果列表、结果读取、结果确认，以及 Codex enable/status/pause/resume/retry。它通过本地 socket 调用 `ddtui.collab`，不创建第二个 Git polling loop。关闭 stdio 连接只关闭当前 MCP 客户端。

软件测试使用本地临时 Git 仓库、真实独立 daemon/MCP 进程与可控 Codex 子进程，覆盖任务发布、结果接收、自动续跑、回执确认、暂停和失败隔离；真实 Codex CLI 的两轮会话恢复也已验证。GitCode 网络权限、目标远端模型与 NPU 环境需要部署时单独验证。
