"""Runtime budgets and completion checks for an explicitly planned experiment."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .source_identity import change_kind, source_snapshot
from .state import async_task_status
from .tool_utils import _safe_path


def tool_experiment_plan(ctx, artifact_path, source_root=None, dependencies=None,
                         wall_seconds=1200, max_device_requests=10,
                         reserve_device_requests=3, reserve_seconds=300,
                         required_change='any'):
    """Freeze a baseline before editing; reserve execution slots for repair."""
    if ctx.optimization_plan and ctx.optimization_plan['status'] == 'active':
        return 'Error: an experiment plan is already active; use experiment_status. Budgets cannot be reset mid-plan.'
    if required_change not in ('any', 'implementation'):
        return 'Error: required_change must be any or implementation.'
    values = (wall_seconds, max_device_requests, reserve_device_requests, reserve_seconds)
    if any(type(v) is not int for v in values) or not (
        1 <= wall_seconds <= 86400 and 1 <= max_device_requests <= 1000
        and 0 <= reserve_device_requests < max_device_requests and 0 <= reserve_seconds < wall_seconds
    ):
        return 'Error: require positive bounded budgets and reserves smaller than their totals.'
    try:
        entry = _safe_path(ctx.work_dir, artifact_path)
        root = _safe_path(ctx.work_dir, source_root) if source_root else entry.parent
        deps = [_safe_path(ctx.work_dir, p) for p in (dependencies or [])]
        baseline = source_snapshot(entry, root, deps)
    except (OSError, ValueError, SyntaxError) as exc:
        return f'Error: cannot snapshot baseline: {exc}'
    ctx.optimization_plan = {
        'status': 'active', 'baseline': baseline, 'required_change': required_change,
        'wall_seconds': wall_seconds, 'deadline_wall': time.time() + wall_seconds,
        'max_device_requests': max_device_requests, 'reserve_device_requests': reserve_device_requests,
        'reserve_seconds': reserve_seconds, 'device_requests': 0, 'model_rounds': 0,
        'completion_corrections': 0, 'first_experiment_number': ctx.experiment_next_id,
    }
    ctx.optimization_deadline = time.monotonic() + wall_seconds
    return json.dumps({'baseline': baseline, 'budget': budget_status(ctx),
                       'execution': 'Use task_start with experiment_id. budget_phase=screen for exploration, fix for a child fix attempt, confirm for a correctness-passed candidate, local for CPU-only work. Native calls outside managed tasks are not counted.'}, ensure_ascii=False)


def budget_status(ctx):
    plan = ctx.optimization_plan
    if not plan:
        return None
    if ctx.optimization_deadline is None:
        ctx.optimization_deadline = time.monotonic() + max(0, plan['deadline_wall'] - time.time())
    remaining = max(0, ctx.optimization_deadline - time.monotonic())
    requests = max(0, plan['max_device_requests'] - plan['device_requests'])
    phase = 'exhausted' if remaining <= 0 or requests == 0 else (
        'correction' if remaining <= plan['reserve_seconds'] or requests <= plan['reserve_device_requests'] else 'search')
    return {'status': plan['status'], 'phase': phase, 'elapsed_seconds': round(plan['wall_seconds'] - remaining, 1),
            'remaining_seconds': round(remaining, 1), 'model_rounds': plan['model_rounds'],
            'device_requests_used': plan['device_requests'], 'device_requests_remaining': requests,
            'screen_requests_remaining': max(0, requests - plan['reserve_device_requests']) if phase == 'search' else 0,
            'reserved_device_requests': plan['reserve_device_requests']}


def active_plan(ctx):
    return ctx.optimization_plan and ctx.optimization_plan['status'] == 'active'


def admit_device_request(ctx, experiment_id, phase):
    """Check admission before process creation; charge only a launched task."""
    if phase not in ('screen', 'fix', 'confirm', 'local'):
        return 'Error: budget_phase must be screen/fix/confirm/local.'
    if not active_plan(ctx) or phase == 'local':
        return None
    entry = ctx.experiments.get(experiment_id)
    if not entry or not entry.get('source_snapshot') or entry.get('plan_start_number') != ctx.optimization_plan['first_experiment_number']:
        return 'Error: planned device execution requires a current experiment_start snapshot.'
    budget = budget_status(ctx)
    if budget['phase'] == 'exhausted':
        return 'Error: execution budget exhausted. ' + json.dumps(budget)
    if phase == 'screen' and budget['phase'] == 'correction':
        return 'Error: correction reserve is protected; repair or confirm the current candidate. ' + json.dumps(budget)
    if phase == 'fix' and entry.get('attempt_type') != 'fix':
        return 'Error: budget_phase=fix requires a fix experiment with parent_id.'
    if phase == 'confirm' and not correctness_passed(entry):
        return 'Error: confirmation reserve requires correctness pass evidence for this exact candidate.'
    return None


def correctness_passed(entry):
    for record in reversed(entry['records']):
        if record['kind'] == 'correctness' and record['valid']:
            return record['result'] == 'pass'
    return False


def completion_state(ctx):
    plan = ctx.optimization_plan
    try:
        base = plan['baseline']
        current = source_snapshot(Path(base['entry']), Path(base['root']),
                                  plan.get('current_dependencies', base['dependencies']))
    except (OSError, ValueError, SyntaxError) as exc:
        return {'issues': [f'Current source cannot be inspected: {exc}'], 'change_kind': 'unknown'}
    kind = change_kind(plan['baseline'], current)
    issues = []
    if kind == 'unchanged':
        issues.append('The executable source equals the baseline; backups, comments and rollback are not implementation progress.')
    elif plan['required_change'] == 'implementation' and kind == 'parameters':
        issues.append('Only literals changed; the requested implementation comparison remains open.')
    entries = [e for e in ctx.experiments.values() if e.get('source_snapshot', {}).get('files') == current['files']
               and int(e['id'].split('-')[-1]) >= plan['first_experiment_number']]
    if not any(correctness_passed(e) for e in entries):
        issues.append('No valid correctness pass is bound to the current executable source and dependencies.')
    if not any(correctness_passed(e) and any(r['kind'] == 'performance' and r['valid'] and r['result'] == 'pass' for r in e['records']) for e in entries):
        issues.append('No valid performance comparison is bound to the current executable source and dependencies.')
    return {'change_kind': kind, 'issues': issues, 'coverage': current['coverage'], 'unknown': current['unknown'],
            'interpretation': 'Implementation-candidate classification is not proof of reached structural work, numerical coverage or a speedup. Check the changed path and matched metrics.'}


def round_feedback(ctx):
    if not active_plan(ctx):
        return None
    state = completion_state(ctx)
    return '[Experiment runtime]\n' + json.dumps({'budget': budget_status(ctx),
        'source_change': state['change_kind'], 'pending_checks': state['issues'],
        'instruction': 'Use runtime values for stopping decisions. In correction phase repair or confirm existing work; do not consume reserved calls on new candidates. Choose the next action to close one pending check. When source evidence resolves the current question, apply the authorized narrow change or run its discriminating check before expanding unrelated documentation. The relevant skill owner supplies the mechanism, not a replacement planner.'}, ensure_ascii=False)


def completion_feedback(ctx):
    if not active_plan(ctx):
        return None
    plan = ctx.optimization_plan
    if any(t.experiment_id and ctx.experiments.get(t.experiment_id, {}).get('plan_start_number') == plan['first_experiment_number']
           and async_task_status(t)[0] == 'running' for t in ctx.tasks.values()):
        return None
    state = completion_state(ctx)
    plan['completion_state'] = state
    if not state['issues']:
        plan['status'] = 'complete'
        return None
    if budget_status(ctx)['phase'] == 'exhausted' or plan['completion_corrections'] >= 2:
        plan['status'] = 'incomplete'
        return None
    plan['completion_corrections'] += 1
    return '[Experiment completion check]\n' + json.dumps({'budget': budget_status(ctx), **state,
        'instruction': 'Resolve a feasible missing check or implementation comparison within the remaining budget. A failed candidate does not establish a hardware limit. If blocked, state the concrete evidence and incomplete scope.'}, ensure_ascii=False)
