"""Route and experiment state survives save/resume-style serialization."""

import json

from ddtui.app import AgentApp
from ddtui.history_store import HistoryStore
from ddtui.state import ToolContext
from ddtui.optimization import budget_status, tool_experiment_plan


def _app(tmp_path):
    app = object.__new__(AgentApp)
    app.ctx = ToolContext(work_dir=str(tmp_path))
    app._session_id = "session-test"
    app.model = "model-test"
    app._history = HistoryStore()
    return app


def test_history_payload_roundtrips_evidence_state(tmp_path):
    app = _app(tmp_path)
    app.ctx.doc_receipts = {"doc-3": {"id": "doc-3", "links": []}}
    app.ctx.doc_receipt_next_id = 4
    app.ctx.experiments = {"exp-2": {"id": "exp-2", "records": []}}
    app.ctx.experiment_next_id = 3

    payload = json.loads(json.dumps(app._history_payload()))
    restored = _app(tmp_path)
    restored._restore_evidence_state(payload)
    assert restored.ctx.doc_receipts == app.ctx.doc_receipts
    assert restored.ctx.doc_receipt_next_id == 4
    assert restored.ctx.experiments == app.ctx.experiments
    assert restored.ctx.experiment_next_id == 3


def test_restore_counters_cannot_collide_with_existing_ids(tmp_path):
    app = _app(tmp_path)
    app._restore_evidence_state(
        {
            "doc_receipts": {"doc-8": {"id": "doc-8"}},
            "doc_receipt_next_id": "bad",
            "experiments": {"exp-5": {"id": "exp-5"}},
            "experiment_next_id": 1,
        }
    )
    assert app.ctx.doc_receipt_next_id == 9
    assert app.ctx.experiment_next_id == 6


def test_budget_and_source_identity_survive_resume(tmp_path):
    app = _app(tmp_path)
    (tmp_path / 'kernel.py').write_text('def kernel(x): return x\n')
    tool_experiment_plan(app.ctx, 'kernel.py')
    app.ctx.optimization_plan['device_requests'] = 7
    payload = json.loads(json.dumps(app._history_payload()))
    restored = _app(tmp_path)
    restored._restore_evidence_state(payload)
    assert restored.ctx.optimization_deadline is None
    assert budget_status(restored.ctx)['phase'] == 'correction'
    assert budget_status(restored.ctx)['device_requests_remaining'] == 3
    assert restored.ctx.optimization_plan['baseline'] == app.ctx.optimization_plan['baseline']
