"""Write-confirmation policy + the ToolConfirmScreen modal."""

import asyncio

from textual.app import App

from ddtui.app_confirm import (
    AppConfirmMixin,
    WRITE_CONFIRM_TOOLS,
    _summarize_write_args,
    always_allow,
)
from ddtui.widgets import ToolConfirmScreen


class FakeApp(AppConfirmMixin):
    """Stand-in for AgentApp: records pushed screens, answers scripted."""

    def __init__(self, answers):
        self._confirm_always = set()
        self._confirm_lock = asyncio.Lock()
        self._answers = list(answers)
        self.pushed = []

    def push_screen(self, screen, on_close):
        self.pushed.append(screen._tool_name)
        asyncio.get_running_loop().call_soon(on_close, self._answers.pop(0))


def test_gated_tool_set():
    assert WRITE_CONFIRM_TOOLS == frozenset(
        {"write_file", "edit_file", "edit_lines", "multi_edit"}
    )


def test_non_write_tools_bypass():
    async def run():
        app = FakeApp(answers=[])
        assert await app._confirm_tool_call("read_file", {}) is True
        assert await app._confirm_tool_call("bash", {"command": "ls"}) is True
        assert await app._confirm_tool_call("apply_patch", {"path": "x"}) is True
        assert app.pushed == []
    asyncio.run(run())


def test_allow_deny_always():
    async def run():
        app = FakeApp(answers=["allow", "deny"])
        assert await app._confirm_tool_call("edit_file", {"path": "a"}) is True
        assert await app._confirm_tool_call("edit_file", {"path": "a"}) is False
        assert app.pushed == ["edit_file", "edit_file"]

        app = FakeApp(answers=["always"])
        assert await app._confirm_tool_call("write_file", {"path": "b", "content": "x"}) is True
        assert await app._confirm_tool_call("write_file", {"path": "c", "content": "y"}) is True
        assert app.pushed == ["write_file"]  # whitelisted after first
        app._answers = ["deny"]
        assert await app._confirm_tool_call("multi_edit", {"path": "d", "edits": []}) is False
    asyncio.run(run())


def test_concurrent_confirms_serialize_with_recheck():
    async def run():
        app = FakeApp(answers=["always"])
        r1, r2 = await asyncio.gather(
            app._confirm_tool_call("edit_lines", {"path": "e", "mode": "delete", "start_line": 1, "end_line": 2}),
            app._confirm_tool_call("edit_lines", {"path": "f", "mode": "insert", "start_line": 3}),
        )
        assert (r1, r2) == (True, True)
        assert app.pushed == ["edit_lines"]  # lock re-check → single modal
    asyncio.run(run())


def test_always_allow_helper():
    assert asyncio.run(always_allow("write_file", {})) is True


def test_summaries():
    s = _summarize_write_args("write_file", {"path": "x.py", "content": "a\n" * 20})
    assert "x.py" in s and "20 行" in s
    s = _summarize_write_args("edit_file", {"path": "y.py", "old_string": "OLD" * 200, "new_string": "n"})
    assert "…(+" in s
    s = _summarize_write_args("edit_file", {"path": "y", "old_string": "a", "new_string": "b", "replace_all": True})
    assert "全部出现" in s
    s = _summarize_write_args("multi_edit", {"path": "z.py", "edits": [{}, {}]})
    assert "2 个替换操作" in s


def test_modal_keys_and_buttons():
    class Host(App):
        def __init__(self):
            super().__init__()
            self.results = []

        def show(self):
            self.push_screen(
                ToolConfirmScreen("edit_file", "path: a.py"), self.results.append
            )

    async def drive(key=None, button=None):
        app = Host()
        async with app.run_test() as pilot:
            app.show()
            await pilot.pause()
            if key:
                await pilot.press(key)
            else:
                await pilot.click(button)
            await pilot.pause()
        return app.results

    for key, want in [("y", "allow"), ("a", "always"), ("n", "deny"), ("escape", "deny")]:
        assert asyncio.run(drive(key=key)) == [want]
    for btn, want in [("#btn-allow", "allow"), ("#btn-always", "always"), ("#btn-deny", "deny")]:
        assert asyncio.run(drive(button=btn)) == [want]
