我就是想有一个自己可以微操的，随时可以扩展的，说中文的deepseek。

随便做的一个，好玩。

## Provider

默认还是 DeepSeek：

```bash
ddtui
```

启动时也可以选择 Codex OAuth：

```bash
DDTUI_PROVIDER=codex ddtui
```

运行中用 `/provider` 查看当前模式，用 `/provider deepseek` 或
`/provider codex` 切换。Codex OAuth 会读取 `~/.codex/auth.json`，
所以需要先跑过 `codex login`。
