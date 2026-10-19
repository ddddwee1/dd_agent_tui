"""Runtime feedback, reserved execution slots and effective-source regressions."""

import asyncio
import json
from types import SimpleNamespace

import ddtui.optimization as optimization
from ddtui.engine import TurnEngine
from ddtui.optimization import budget_status, completion_feedback, completion_state, tool_experiment_plan
from ddtui.providers import LLMStreamEvent
from ddtui.source_identity import change_kind, refresh, source_snapshot
from ddtui.state import ToolContext
from ddtui.tools_experiments import tool_experiment_record, tool_experiment_start
from ddtui.tools_tasks import tool_task_start
from ddtui.context_compaction import is_user_instruction
from ddtui.runtime_messages import EXPERIMENT_RUNTIME_KIND, guard_assistant_runtime_claims


def setup_plan(tmp_path, **kwargs):
    entry = tmp_path / 'kernel.py'
    entry.write_text('BLOCK = 64\ndef kernel(x):\n    return x + BLOCK\n')
    ctx = ToolContext(work_dir=str(tmp_path), session_id='budget-test')
    result = tool_experiment_plan(ctx, 'kernel.py', **kwargs)
    assert not result.startswith('Error:')
    return ctx, entry


def test_backup_comment_and_rollback_do_not_count_as_progress(tmp_path):
    ctx, entry = setup_plan(tmp_path, required_change='implementation')
    baseline = entry.read_text()
    (tmp_path / 'kernel.experiment.py').write_text('def faster(): return 2\n')
    entry.write_text(baseline + '# explanatory comment\n')
    assert completion_state(ctx)['change_kind'] == 'unchanged'
    entry.write_text(baseline.replace('64', '128'))
    assert completion_state(ctx)['change_kind'] == 'parameters'
    assert any('literals' in issue for issue in completion_state(ctx)['issues'])
    entry.write_text(baseline.replace('return x + BLOCK', 'return sum(x) + BLOCK'))
    assert completion_state(ctx)['change_kind'] == 'implementation_candidate'
    entry.write_text(baseline)
    assert completion_state(ctx)['change_kind'] == 'unchanged'


def test_local_dependency_drift_invalidates_task_binding(tmp_path):
    helper = tmp_path / 'helper.py'
    helper.write_text('value = 1\n')
    entry = tmp_path / 'kernel.py'
    entry.write_text('from helper import value\n')
    ctx = ToolContext(work_dir=str(tmp_path))
    tool_experiment_start(ctx, 'kernel.py', 'test helper')
    helper.write_text('value = 2\n')
    result = tool_task_start(ctx, 'echo should-not-run', artifacts=['kernel.py'], experiment_id='exp-1')
    assert 'dependencies changed' in result
    assert not ctx.tasks
    record = tool_experiment_record(ctx, 'exp-1', 'correctness', 'pass', command='manual', decision='keep')
    assert 'valid: false' in record
    assert ctx.experiments['exp-1']['status'] != 'kept'


def test_relative_imports_and_package_initializers_are_bound(tmp_path):
    package = tmp_path / 'pkg'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'helper.py').write_text('value = 1\n')
    entry = package / 'kernel.py'
    entry.write_text('from .helper import value\n')
    snapshot = source_snapshot(entry, tmp_path)
    assert len(snapshot['files']) == 3
    (package / '__init__.py').write_text('flag = True\n')
    assert change_kind(snapshot, refresh(snapshot)) != 'unchanged'


def test_explicit_dependency_updates_preserve_budget_and_bind_tasks(tmp_path):
    ctx, entry = setup_plan(tmp_path)
    dependency = tmp_path / 'lookup.dat'
    dependency.write_text('1 2 3')
    entry.write_text('def kernel(x): return sum(x)\n')
    tool_experiment_start(ctx, 'kernel.py', 'uses lookup', dependencies=['lookup.dat'])
    assert str(dependency) in ctx.experiments['exp-1']['source_snapshot']['files']
    assert ctx.optimization_plan['device_requests'] == 0
    dependency.write_text('1 2 4')
    assert 'dependencies changed' in tool_task_start(ctx, 'true', artifacts=['kernel.py'], experiment_id='exp-1')
    assert budget_status(ctx)['device_requests_remaining'] == 10


def test_device_reserve_is_enforced_at_actual_task_start(tmp_path):
    ctx, entry = setup_plan(tmp_path, max_device_requests=4, reserve_device_requests=2)
    entry.write_text('def kernel(x): return sum(x)\n')
    tool_experiment_start(ctx, 'kernel.py', 'screen')
    for _ in range(2):
        result = tool_task_start(ctx, 'exit 1', artifacts=['kernel.py'], experiment_id='exp-1', notice_time=0)
        assert 'Started managed task' in result
        list(ctx.tasks.values())[-1].proc.wait(timeout=10)
    assert budget_status(ctx)['device_requests_remaining'] == 2
    result = tool_task_start(ctx, 'echo blocked', artifacts=['kernel.py'], experiment_id='exp-1')
    assert 'reserve is protected' in result
    assert len(ctx.tasks) == 2
    assert 'requires a fix' in tool_task_start(ctx, 'true', artifacts=['kernel.py'], experiment_id='exp-1', budget_phase='fix')
    entry.write_text('def kernel(x): return sum(x) + 1\n')
    tool_experiment_start(ctx, 'kernel.py', 'repair', attempt_type='fix', parent_id='exp-1')
    result = tool_task_start(ctx, 'true', artifacts=['kernel.py'], experiment_id='exp-2', budget_phase='fix', notice_time=0)
    assert 'Started managed task' in result
    ctx.tasks['task-3'].proc.wait(timeout=10)
    tool_experiment_record(ctx, 'exp-2', 'correctness', 'pass', task_id='task-3')
    result = tool_task_start(ctx, 'true', artifacts=['kernel.py'], experiment_id='exp-2', budget_phase='confirm', notice_time=0)
    assert 'Started managed task' in result
    ctx.tasks['task-4'].proc.wait(timeout=10)
    assert budget_status(ctx)['device_requests_remaining'] == 0
    assert 'exhausted' in tool_task_start(ctx, 'true', artifacts=['kernel.py'], experiment_id='exp-2', budget_phase='confirm')


def test_live_clock_and_resume_do_not_reset_budget(tmp_path, monkeypatch):
    clock = SimpleNamespace(mono=100.0, wall=1000.0)
    monkeypatch.setattr(optimization, 'time', SimpleNamespace(monotonic=lambda: clock.mono, time=lambda: clock.wall))
    ctx, _ = setup_plan(tmp_path)
    clock.mono += 245
    clock.wall += 245
    assert budget_status(ctx)['remaining_seconds'] == 955
    assert 'already active' in tool_experiment_plan(ctx, 'kernel.py')
    restored = ToolContext(work_dir=str(tmp_path))
    restored.optimization_plan = json.loads(json.dumps(ctx.optimization_plan))
    clock.wall += 55
    assert budget_status(restored)['remaining_seconds'] == 900


def test_new_plan_does_not_inherit_old_candidate_counts(tmp_path):
    ctx, entry = setup_plan(tmp_path)
    tool_experiment_start(ctx, 'kernel.py', 'old attempt', max_candidates=1)
    ctx.optimization_plan['status'] = 'incomplete'
    tool_experiment_plan(ctx, 'kernel.py')
    entry.write_text('def kernel(x): return sum(x)\n')
    out = tool_experiment_start(ctx, 'kernel.py', 'new attempt', max_candidates=3)
    assert 'candidate_budget: 1/3' in out
    assert not ctx.experiments['exp-2']['budget_warnings']


def test_completion_waits_only_for_tasks_in_the_current_plan(tmp_path, monkeypatch):
    ctx, _ = setup_plan(tmp_path)
    monkeypatch.setattr(optimization, 'async_task_status', lambda task: ('running', None, 1))
    ctx.tasks['old'] = SimpleNamespace(experiment_id='exp-old')
    ctx.experiments['exp-old'] = {'plan_start_number': 0}
    assert completion_feedback(ctx) is not None
    tool_experiment_start(ctx, 'kernel.py', 'current attempt')
    ctx.tasks['current'] = SimpleNamespace(experiment_id='exp-1')
    assert completion_feedback(ctx) is None
    assert ctx.optimization_plan['completion_corrections'] == 1


def test_engine_injects_budget_and_rejects_premature_finish(tmp_path):
    ctx, _ = setup_plan(tmp_path)

    class Provider:
        calls = 0

        async def stream(self, messages, tools, model, effort):
            self.calls += 1
            assert messages[-1]['ddtui_kind'] == 'experiment_runtime'
            assert 'remaining_seconds' in messages[-1]['content']
            assert '"source_change": "unchanged"' in messages[-1]['content']
            yield LLMStreamEvent(content='Time budget exhausted; done.')

    provider = Provider()
    messages = [{'role': 'user', 'content': 'Optimize the kernel.'}]
    engine = TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[], model='unchanged', effort='max')
    asyncio.run(engine.run_turn())
    assert provider.calls == 3
    assert ctx.optimization_plan['status'] == 'incomplete'
    assert ctx.optimization_plan['completion_corrections'] == 2
    checks = [m for m in messages if m.get('ddtui_kind') == 'experiment_runtime' and 'completion check' in m['content']]
    assert len(checks) == 2
    assert 'equals the baseline' in checks[0]['content']


def test_runtime_budget_is_not_a_new_user_goal_or_assistant_authority():
    event = {'role': 'user', 'ddtui_kind': EXPERIMENT_RUNTIME_KIND, 'content': '[Experiment runtime]\nremaining=900'}
    assert not is_user_instruction(event)
    assert is_user_instruction({'role': 'user', 'content': event['content']})
    guarded, changed = guard_assistant_runtime_claims('[Experiment runtime]\nremaining=0')
    assert changed and 'unverified' in guarded
