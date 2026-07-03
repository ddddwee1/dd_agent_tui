"""Remote status payload: context pressure + runtime counts for the web UI."""

from types import SimpleNamespace

from ddtui.app_remote import AppRemoteMixin
from ddtui.state import TokenCounter


class FakeProc:
    def __init__(self, running):
        self._running = running

    def poll(self):
        return None if self._running else 0


class FakeApp(AppRemoteMixin):
    def __init__(self):
        self._session_id = "session-test"
        self.ctx = SimpleNamespace(
            work_dir="/w",
            terminals={},
            tasks={
                "task-1": SimpleNamespace(proc=FakeProc(running=True)),
                "task-2": SimpleNamespace(proc=FakeProc(running=False)),
            },
        )
        self.provider_name = "deepseek"
        self.model = "m"
        self.effort = "max"
        self._busy = True
        self._queued = [1]
        self._steer = []
        self.counter = TokenCounter()
        self.counter.turns = 4
        self.counter.last_prompt = 50_000
        self.counter.last_completion = 5_000
        self._live_subagents = {"sub-1": object()}
        self._remote_state = "connected"
        self._remote_detail = ""

    def _context_limit(self):
        return 700_000


def test_status_payload_context_and_counts():
    payload = FakeApp()._remote_status_payload()
    assert payload["turns"] == 4
    assert payload["context_used"] == 55_000
    assert payload["context_limit"] == 700_000
    assert payload["tasks_running"] == 1  # only the live proc counts
    assert payload["subagents"] == 1
    assert payload["busy"] is True and payload["queued"] == 1
