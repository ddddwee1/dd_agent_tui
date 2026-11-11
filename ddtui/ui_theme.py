"""Shared graphite surfaces and semantic colors for the terminal workbench."""

from dataclasses import dataclass

from textual.theme import Theme


@dataclass(frozen=True)
class Palette:
    background: str = "#22272e"
    surface: str = "#282e36"
    panel: str = "#2d343d"
    text: str = "#c4cbd4"
    muted: str = "#919ca9"
    thinking_summary: str = "#b09ac7"
    border: str = "#3a434e"
    accent: str = "#8eafb9"
    success: str = "#94ad9c"
    warning: str = "#baa786"
    error: str = "#c39094"
    added: str = "#2b3b33"
    removed: str = "#3d3138"


UI = Palette()

WORKBENCH_THEME = Theme(
    name="ddtui",
    primary=UI.accent,
    secondary=UI.accent,
    accent=UI.accent,
    foreground=UI.text,
    background=UI.background,
    surface=UI.surface,
    panel=UI.panel,
    success=UI.success,
    warning=UI.warning,
    error=UI.error,
    dark=True,
    variables={
        "text-muted": UI.muted,
        "border": UI.accent,
        "border-blurred": UI.border,
        "block-cursor-text-style": "none",
        "block-cursor-foreground": UI.text,
        "block-cursor-background": UI.panel,
        "block-cursor-blurred-foreground": UI.muted,
        "block-cursor-blurred-background": UI.background,
        "input-selection-background": f"{UI.accent} 25%",
        "input-cursor-background": UI.accent,
        "input-cursor-foreground": UI.background,
    },
)
