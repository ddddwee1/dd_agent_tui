"""Synchronous bash tool implementation.

Background/async command execution lives in the task_* tools
(`tools_tasks.py`); this module only provides the foreground one-shot
`bash`. Use `task_start` for anything that should run in the background
or whose completion matters.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_BASH,
    BASH_OUTPUT_MAX_CHARS,
    BASH_TIMEOUT,
)
from .state import ToolContext
from .tool_utils import (
    _BASH_HARD_CHAR_CAP,
    _check_dangerous,
    _safe_path,
    _sandbox_error,
    _truncate_output,
)


def tool_bash(
    ctx: ToolContext,
    command: str,
    workdir: str | None = None,
    max_output_chars: int | None = None,
) -> str:
    """Run a shell command with safety checks, timeout, and output truncation."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"

    cap = BASH_OUTPUT_MAX_CHARS
    if max_output_chars is not None:
        try:
            cap = max(1, min(_BASH_HARD_CHAR_CAP, int(max_output_chars)))
        except (TypeError, ValueError):
            return f"Error: max_output_chars must be an integer, got {max_output_chars!r}"

    cwd_path = _safe_path(ctx.work_dir, workdir) if workdir else Path(ctx.work_dir).resolve()
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="run bash in a workdir")
        if err:
            return err
    cwd = str(cwd_path)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {BASH_TIMEOUT}s"

    parts = []
    if result.stdout:
        parts.append(f"STDOUT:\n{_truncate_output(result.stdout, cap)}")
    if result.stderr:
        parts.append(f"STDERR:\n{_truncate_output(result.stderr, cap)}")
    parts.append(f"Exit code: {result.returncode}")
    return "\n".join(parts)
