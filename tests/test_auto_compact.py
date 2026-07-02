"""Auto-compact: threshold gate, guards, history swap, failure safety."""

import asyncio

import pytest

import ddtui.app_history as ah
from ddtui.app_history import AppHistoryMixin
from ddtui.config import COMPACT_KEEP_RECENT_TURNS
from ddtui.state import TokenCounter


@pytest.fixture(autouse=True)
def _pin_threshold(monkeypatch):
    # Scenarios below assert against a fixed 75% threshold so changing
    # the shipped default doesn't silently change what they test.
    monkeypatch.setattr(ah, "AUTO_COMPACT_THRESHOLD", 0.75)


def _messages(n_user=5):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_user):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    return msgs


class FakeApp(AppHistoryMixin):
    def __init__(self, *, limit=100_000, last_prompt=0, n_user=5):
        self.counter = TokenCounter()
        self.counter.last_prompt = last_prompt
        self.messages = _messages(n_user)
        self._active_explore = None
        self._limit = limit
        self.mounted = []
        self.compact_calls = 0
        self.autosaved = 0

    def _context_limit(self):
        return self._limit

    async def _mount_widget(self, w):
        # Static stores its renderable in a name-mangled private slot;
        # good enough for asserting notice text without an app context.
        self.mounted.append(str(getattr(w, "_Static__content", w)))

    async def _autosave_conversation(self):
        self.autosaved += 1

    async def _compact_messages(self, messages):
        self.compact_calls += 1
        kept = [m for m in messages if m["role"] == "system"]
        kept.append({"role": "system", "content": "# 历史摘要\n..."})
        kept.extend(messages[-2:])
        return kept, {"before_n": len(messages), "after_n": len(kept), "saved": 999}


def run(app):
    asyncio.run(app._auto_compact_if_needed())
    return app


def test_triggers_above_threshold(monkeypatch):
    app = FakeApp(limit=100_000, last_prompt=80_000)  # 80% > 75%
    run(app)
    assert app.compact_calls == 1
    assert app.autosaved == 1
    assert any("# 历史摘要" in (m.get("content") or "") for m in app.messages)
    assert any("自动压缩" in s for s in app.mounted)


def test_below_threshold_no_op():
    app = FakeApp(limit=100_000, last_prompt=50_000)  # 50% < 75%
    run(app)
    assert app.compact_calls == 0 and app.mounted == []


def test_disabled_by_zero_threshold(monkeypatch):
    monkeypatch.setattr(ah, "AUTO_COMPACT_THRESHOLD", 0.0)
    app = FakeApp(limit=100_000, last_prompt=99_000)
    run(app)
    assert app.compact_calls == 0


def test_no_limit_or_no_usage_no_op():
    app = FakeApp(limit=None, last_prompt=80_000)
    run(app)
    assert app.compact_calls == 0
    app = FakeApp(limit=100_000, last_prompt=0)
    run(app)
    assert app.compact_calls == 0


def test_open_explore_blocks():
    app = FakeApp(limit=100_000, last_prompt=90_000)
    app._active_explore = {"id": "exp-1"}
    run(app)
    assert app.compact_calls == 0


def test_short_history_blocks():
    app = FakeApp(limit=100_000, last_prompt=90_000,
                  n_user=COMPACT_KEEP_RECENT_TURNS)
    run(app)
    assert app.compact_calls == 0


def test_failure_leaves_history_untouched():
    app = FakeApp(limit=100_000, last_prompt=90_000)
    before = list(app.messages)

    async def boom(messages):
        raise RuntimeError("summarizer down")

    app._compact_messages = boom
    run(app)
    assert app.messages == before
    assert any("自动压缩失败" in s for s in app.mounted)
    assert app.autosaved == 0
