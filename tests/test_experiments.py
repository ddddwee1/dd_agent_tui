"""Artifact-bound experiment ledger and task evidence."""

import hashlib

from ddtui.state import ToolContext
from ddtui.tools_experiments import (
    tool_experiment_record,
    tool_experiment_start,
    tool_experiment_status,
)
from ddtui.tools_tasks import (
    collect_task_events,
    recover_tasks_for_session,
    tool_task_start,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_experiment_and_task_bind_exact_artifact_sha(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("answer = 1\n")
    ctx = ToolContext(work_dir=str(tmp_path), session_id="test-exp-bind")

    started = tool_experiment_start(
        ctx, "candidate.py", "candidate should pass", boundary="simulator"
    )
    expected = _sha(candidate.read_bytes())
    assert "Experiment exp-1 started" in started
    assert f"artifact_sha256: {expected}" in started

    task_result = tool_task_start(
        ctx,
        "echo correctness-ok",
        name="correctness",
        notice_time=0,
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    assert f"artifact_sha256_at_start: {expected}" in task_result
    task = ctx.tasks["task-1"]
    task.proc.wait(timeout=10)
    events = collect_task_events(ctx)
    assert len(events) == 1
    assert "status: success" in events[0]
    assert f"artifact_sha256_at_start: {expected}" in events[0]
    assert "experiment_id: exp-1" in events[0]

    correctness = tool_experiment_record(
        ctx,
        "exp-1",
        "correctness",
        "pass",
        task_id="task-1",
        evidence="public suite passed",
    )
    assert "valid: true" in correctness
    perf_start = tool_task_start(
        ctx,
        "echo performance-ok",
        name="performance",
        notice_time=0,
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    assert "Started managed task task-2" in perf_start
    ctx.tasks["task-2"].proc.wait(timeout=10)
    collect_task_events(ctx)
    performance = tool_experiment_record(
        ctx,
        "exp-1",
        "performance",
        "pass",
        task_id="task-2",
        metric_name="latency",
        metric_value="13.2 us",
        decision="keep",
    )
    assert "valid: true" in performance
    status = tool_experiment_status(ctx, "exp-1")
    assert "[kept]" in status
    assert "correctness=True" in status
    assert "performance=True" in status

    heldout = tool_task_start(
        ctx,
        "echo heldout-mismatch; exit 1",
        name="heldout",
        notice_time=0,
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    assert "Started managed task task-3" in heldout
    ctx.tasks["task-3"].proc.wait(timeout=10)
    collect_task_events(ctx)
    later_failure = tool_experiment_record(
        ctx,
        "exp-1",
        "correctness",
        "fail",
        task_id="task-3",
        evidence="held-out mismatch",
    )
    assert "valid: true" in later_failure
    status = tool_experiment_status(ctx, "exp-1")
    assert "[invalidated]" in status
    assert "correctness=False" in status
    assert "performance=False" in status
    assert ctx.experiments["exp-1"]["records"][1]["valid"] is False
    assert ctx.experiments["exp-1"]["records"][2]["task_output_path"]


def test_performance_before_correctness_is_retained_but_invalid(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("answer = 1\n")
    ctx = ToolContext(work_dir=str(tmp_path))
    tool_experiment_start(ctx, "candidate.py", "try it")
    out = tool_experiment_record(
        ctx,
        "exp-1",
        "performance",
        "pass",
        command="benchmark",
        metric_name="latency",
        metric_value="9 us",
    )
    assert "valid: false" in out
    assert "before correctness passes" in out
    assert "requires a completed artifact-bound task_id" in out
    assert len(ctx.experiments["exp-1"]["records"]) == 1


def test_artifact_drift_requires_new_experiment(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("answer = 1\n")
    ctx = ToolContext(work_dir=str(tmp_path))
    tool_experiment_start(ctx, "candidate.py", "first bytes")
    candidate.write_text("answer = 2\n")

    task = tool_task_start(
        ctx,
        "echo stale",
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    assert "does not match the experiment" in task
    assert not ctx.tasks
    record = tool_experiment_record(
        ctx, "exp-1", "correctness", "pass", command="manual-check"
    )
    assert "valid: false" in record
    assert "artifact bytes changed" in record


def test_nonzero_validation_task_is_valid_fail_evidence(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("answer = 1\n")
    ctx = ToolContext(work_dir=str(tmp_path), session_id="test-exp-fail")
    tool_experiment_start(ctx, "candidate.py", "expected to expose a bug")
    tool_task_start(
        ctx,
        "echo mismatch; exit 1",
        notice_time=0,
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    ctx.tasks["task-1"].proc.wait(timeout=10)
    collect_task_events(ctx)
    recorded = tool_experiment_record(
        ctx,
        "exp-1",
        "correctness",
        "fail",
        task_id="task-1",
        evidence="mismatch",
    )
    assert "valid: true" in recorded


def test_candidate_and_same_boundary_fix_budgets_warn(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("x = 1\n")
    ctx = ToolContext(work_dir=str(tmp_path))
    for index in range(4):
        candidate.write_text(f"x = {index}\n")
        out = tool_experiment_start(
            ctx,
            "candidate.py",
            f"candidate {index}",
            max_candidates=3 if index == 0 else 99,
            max_same_boundary_fixes=1,
        )
    assert "candidate budget exceeded (4/3)" in out

    candidate.write_text("x = 10\n")
    tool_experiment_start(
        ctx,
        "candidate.py",
        "fix one",
        attempt_type="fix",
        parent_id="exp-1",
        boundary="sim",
        max_same_boundary_fixes=1,
    )
    candidate.write_text("x = 11\n")
    fix_two = tool_experiment_start(
        ctx,
        "candidate.py",
        "fix two",
        attempt_type="fix",
        parent_id="exp-1",
        boundary="sim",
        max_same_boundary_fixes=1,
    )
    assert "same-boundary fix budget exceeded (2/1)" in fix_two


def test_task_recovery_preserves_artifact_binding(tmp_path):
    candidate = tmp_path / "candidate.py"
    candidate.write_text("answer = 1\n")
    session_id = "test-exp-recover"
    ctx = ToolContext(work_dir=str(tmp_path), session_id=session_id)
    tool_experiment_start(ctx, "candidate.py", "recoverable evidence")
    tool_task_start(
        ctx,
        "echo done",
        notice_time=0,
        artifacts=["candidate.py"],
        experiment_id="exp-1",
    )
    ctx.tasks["task-1"].proc.wait(timeout=10)
    collect_task_events(ctx)

    recovered_ctx = ToolContext(work_dir=str(tmp_path), session_id=session_id)
    recovered = recover_tasks_for_session(recovered_ctx, session_id)
    assert len(recovered) == 1
    assert recovered[0].experiment_id == "exp-1"
    assert recovered[0].artifact_hashes == ctx.tasks["task-1"].artifact_hashes
