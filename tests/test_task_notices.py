"""Idle logs do not wake models; fresh progress and completion still do."""

import pytest

from ddtui.state import AsyncTask, ToolContext
from ddtui.tools_tasks import collect_task_events, _task_registry_payload


class Process:
    pid = 12345
    return_code = None

    def poll(self):
        return self.return_code


@pytest.mark.parametrize("final_code", [0, 1])
def test_only_changed_output_notifies_and_completion_is_independent(tmp_path, monkeypatch, final_code):
    clock = [100.0]
    monkeypatch.setattr("ddtui.tools_tasks.time.monotonic", lambda: clock[0])
    path = tmp_path / "output.log"
    path.write_text("")
    task = AsyncTask(id="task-1", name="test", command="test", workdir=str(tmp_path),
                     output_path=path, status_path=tmp_path / "status.json", proc=Process(),
                     started_at=90.0, started_wall=1000.0, notice_time=10, next_notice_at=100.0)
    ctx = ToolContext(work_dir=str(tmp_path), tasks={task.id: task})
    assert collect_task_events(ctx) == []  # Even the first idle interval is quiet.
    path.write_text("progress 1")
    assert collect_task_events(ctx) == []  # Minimum interval still applies.
    clock[0] = 110.0
    events = collect_task_events(ctx)
    assert len(events) == 1 and "progress 1" in events[0]
    assert task.notice_count == 1
    fingerprint = _task_registry_payload(task)["last_notice_output"]
    assert fingerprint
    clock[0] = 120.0
    assert collect_task_events(ctx) == []
    assert task.notice_count == 1
    # Progress bars may overwrite the same number of bytes.
    path.write_text("progress 2")
    clock[0] = 130.0
    events = collect_task_events(ctx)
    assert len(events) == 1 and "progress 2" in events[0]
    assert task.last_notice_output != fingerprint
    # Completion is delivered immediately, even before the next notice interval.
    task.proc.return_code = final_code
    events = collect_task_events(ctx)
    assert len(events) == 1 and "[Async task complete]" in events[0]
    assert f"return_code: {final_code}" in events[0]
    assert collect_task_events(ctx) == []
