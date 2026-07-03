"""Explore-span mixin for the parent conversation.

All real logic lives in `explore_core`, parameterized over
(ExploreState, messages) so subagent sessions reuse it verbatim. This
mixin binds the core to the app's own state/history, keeps the legacy
`_active_explore` / `_explore_next_id` attribute API alive as
properties, and owns the one UI concern: mounting the summary block.
"""

from __future__ import annotations

from typing import Any

from . import explore_core
from .state import ExploreState
from .widgets import ExploreSummaryBlock


class AppExploreMixin:
    def _explore_state_obj(self) -> ExploreState:
        st = getattr(self, "_explore_state", None)
        if st is None:
            st = self._explore_state = ExploreState()
        return st

    # Legacy attribute API — every pre-existing call site (auto-compact
    # guard, /clear reset, autosave header, tests) reads or assigns
    # these directly.
    @property
    def _active_explore(self) -> dict | None:
        return self._explore_state_obj().active

    @_active_explore.setter
    def _active_explore(self, value: dict | None) -> None:
        self._explore_state_obj().active = value

    @property
    def _explore_next_id(self) -> int:
        return self._explore_state_obj().next_id

    @_explore_next_id.setter
    def _explore_next_id(self, value: int) -> None:
        self._explore_state_obj().next_id = value

    def _explore_payload(self) -> dict | None:
        return explore_core.explore_payload(self._explore_state_obj())

    def _restore_explore_payload(self, payload: Any) -> None:
        explore_core.restore_explore_payload(
            self._explore_state_obj(), payload, self.messages
        )

    def _reset_explore_state(self) -> None:
        state = self._explore_state_obj()
        state.active = None
        state.next_id = 1

    def _drop_explore_if_truncated(self) -> None:
        explore_core.drop_explore_if_truncated(
            self._explore_state_obj(), self.messages
        )

    async def _explore_start_tool(
        self, args: dict, tool_call_count: int
    ) -> str:
        return await explore_core.explore_start(
            self._explore_state_obj(), self.messages, args, tool_call_count
        )

    async def _explore_cancel_tool(
        self, args: dict, tool_call_count: int
    ) -> str:
        return await explore_core.explore_cancel(
            self._explore_state_obj(), args, tool_call_count
        )

    async def _explore_end_tool(
        self, args: dict, tool_call_count: int
    ) -> str:
        result, summary_message = await explore_core.explore_end(
            self._explore_state_obj(),
            self.messages,
            args,
            tool_call_count,
            provider=self.provider,
            model=self.model,
            effort=self.effort,
            session_id=self._session_id,
        )
        if summary_message is not None:
            await self._mount_widget(ExploreSummaryBlock(summary_message))
        return result
