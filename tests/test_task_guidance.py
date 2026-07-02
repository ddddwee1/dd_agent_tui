"""Runtime guidance: escalating anti-polling warnings, slow-bash note."""

import time

import pytest

import ddtui.tools_bash as tools_bash_mod
from ddtui.state import ToolContext
from ddtui.tools_bash import tool_bash
from ddtui.tools_tasks import (
    tool_task_check,
    tool_task_kill,
    tool_task_read,
    tool_task_start,
)


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(work_dir=str(tmp_path))


def test_anti_polling_escalation(ctx):
    tool_task_start(ctx, "sleep 20", name="poll-test")
    tid = next(iter(ctx.tasks))
    try:
        time.sleep(0.3)
        r1 = tool_task_check(ctx, tid)
        assert "do not poll" in r1 and "polling loop" not in r1
        r2 = tool_task_check(ctx, tid)
        assert "checked this RUNNING task 2 times" in r2
        assert "NOT abandoning the task" in r2  # psychological assurance
        r3 = tool_task_read(ctx, tid, offset=0)
        assert "inspected this RUNNING task 3 times" in r3
    finally:
        tool_task_kill(ctx, tid, force=True)


def test_slow_bash_note(ctx, monkeypatch):
    r = tool_bash(ctx, "echo fast")
    assert "[note]" not in r
    monkeypatch.setattr(tools_bash_mod, "BASH_SLOW_COMMAND_SECONDS", 0.5)
    r = tool_bash(ctx, "sleep 0.6; echo slow-done")
    assert "slow-done" in r
    assert "[note]" in r and "task_start" in r
