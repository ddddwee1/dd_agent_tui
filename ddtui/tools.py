"""Tool facade: schemas plus dispatch table.

The large tool implementation set is split by domain:
- `tool_schemas` keeps the JSON schemas sent to the LLM.
- `tools_bash`, `tools_files`, `tools_search`, `tools_web`, and
  `tools_checkpoint`, `tools_notes`, `tools_tasks`, `tools_terminal`,
  `tools_todo` contain the concrete Python implementations.

`execute_tool` is the dispatch entry point for the stateless tool set
(everything in `TOOL_FUNCS`). A handful of meta-tools that need the live
LLM client / app state — the subagent tools, exploration-span tools, and
`compact_self` (see `APP_DISPATCHED_TOOLS`) — are handled by `AgentApp`
before dispatch and are intentionally absent from `TOOL_FUNCS`.
"""

from __future__ import annotations

from .state import ToolContext
from .tool_schemas import TOOLS
from .tools_bash import (
    tool_bash,
    tool_bash_check,
    tool_bash_kill,
    tool_bash_list,
    tool_bash_start,
    tool_bash_wait,
)
from .tools_checkpoint import (
    tool_checkpoint_clear,
    tool_checkpoint_get,
    tool_checkpoint_tool,
)
from .tools_files import (
    tool_apply_patch,
    tool_edit_file,
    tool_edit_lines,
    tool_multi_edit,
    tool_read_file,
    tool_write_file,
)
from .tools_notes import (
    tool_project_note_add,
    tool_project_note_delete,
    tool_project_note_list,
    tool_project_note_read,
    tool_project_note_search,
    tool_project_note_update,
)
from .tools_search import tool_glob_files, tool_list_files, tool_search_content
from .tools_tasks import (
    tool_task_check,
    tool_task_kill,
    tool_task_list,
    tool_task_read,
    tool_task_start,
    tool_task_wait,
)
from .tools_terminal import (
    tool_terminal_close,
    tool_terminal_interrupt,
    tool_terminal_list,
    tool_terminal_read,
    tool_terminal_send,
    tool_terminal_start,
)
from .tools_todo import tool_todo_tool
from .tools_web import tool_web_fetch, tool_web_search

# Re-exported for callers that import TOOLS from ddtui.tools.
__all__ = ["TOOLS", "TOOL_FUNCS", "execute_tool"]


TOOL_FUNCS = {
    "bash": tool_bash,
    "bash_start": tool_bash_start,
    "bash_check": tool_bash_check,
    "bash_wait": tool_bash_wait,
    "bash_kill": tool_bash_kill,
    "bash_list": tool_bash_list,
    "checkpoint_tool": tool_checkpoint_tool,
    "checkpoint_get": tool_checkpoint_get,
    "checkpoint_clear": tool_checkpoint_clear,
    "task_start": tool_task_start,
    "task_check": tool_task_check,
    "task_read": tool_task_read,
    "task_wait": tool_task_wait,
    "task_kill": tool_task_kill,
    "task_list": tool_task_list,
    "terminal_start": tool_terminal_start,
    "terminal_send": tool_terminal_send,
    "terminal_read": tool_terminal_read,
    "terminal_interrupt": tool_terminal_interrupt,
    "terminal_close": tool_terminal_close,
    "terminal_list": tool_terminal_list,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "apply_patch": tool_apply_patch,
    "edit_file": tool_edit_file,
    "edit_lines": tool_edit_lines,
    "multi_edit": tool_multi_edit,
    "project_note_add": tool_project_note_add,
    "project_note_search": tool_project_note_search,
    "project_note_list": tool_project_note_list,
    "project_note_read": tool_project_note_read,
    "project_note_update": tool_project_note_update,
    "project_note_delete": tool_project_note_delete,
    "list_files": tool_list_files,
    "glob_files": tool_glob_files,
    "search_content": tool_search_content,
    "web_fetch": tool_web_fetch,
    "web_search": tool_web_search,
    "todo_tool": tool_todo_tool,
}


# Parameter aliases so the LLM can use common synonyms without
# crashing the call.  Added because models sometimes confuse
# `path` / `file_path` / `directory` / `dir_path` etc.
_PARAM_ALIASES: dict[str, str] = {
    "file_path": "path",
    "dir_path": "path",
    "directory": "path",
    "file": "path",
    "folder": "path",
}

# Only tools that actually take a `path` parameter should have the path
# aliases applied. Applying them globally used to rewrite e.g. bash's
# `directory` into `path`, turning a recoverable typo into a hard
# TypeError. Derive the set from the schemas so it stays in sync.
_PATH_PARAM_TOOLS: frozenset[str] = frozenset(
    t["function"]["name"]
    for t in TOOLS
    if "path" in t.get("function", {}).get("parameters", {}).get("properties", {})
)


def _normalize_args(name: str, args: dict) -> dict:
    """Canonicalise common parameter-name variations in-place.

    Path aliases are only applied to tools whose schema declares a
    `path` parameter, so synonyms passed to unrelated tools are left
    untouched rather than being rewritten into an argument that tool
    does not accept.

    - If the canonical key is missing but an alias is present, the alias
      value is moved to the canonical key.
    - If both the canonical key and an alias are present, the alias is
      simply dropped (canonical wins).
    """
    if name not in _PATH_PARAM_TOOLS:
        return args
    for alias, canonical in _PARAM_ALIASES.items():
        if alias in args:
            if canonical not in args:
                args[canonical] = args.pop(alias)
            else:
                args.pop(alias)  # canonical already set; drop the alias
    return args


# Tools that are NOT dispatched here: they need the live LLM client and
# AgentApp state, so AgentApp intercepts them before execute_tool is
# reached (see app_agent_loop). Listed so a stray call that does reach
# execute_tool gets a precise message instead of a generic "Unknown tool".
APP_DISPATCHED_TOOLS: frozenset[str] = frozenset({
    "spawn_agent",
    "chat_agent",
    "await_agent",
    "end_agent",
    "compact_self",
    "explore_start",
    "explore_end",
    "explore_cancel",
})


def execute_tool(ctx: ToolContext, name: str, args: dict) -> str:
    """Dispatch a tool call to its implementation in TOOL_FUNCS.

    This is the dispatch entry point for the stateless tool set. The
    subagent meta-tools, exploration-span tools, and compact_self
    (see APP_DISPATCHED_TOOLS) need the live LLM client / app state and
    are handled by AgentApp before they reach here, so they are not in
    TOOL_FUNCS by design.
    """
    fn = TOOL_FUNCS.get(name)
    if not fn:
        if name in APP_DISPATCHED_TOOLS:
            return (
                f"Error: {name} is handled by the app layer, not execute_tool. "
                "This indicates a dispatch bug — it should have been "
                "intercepted before reaching generic tool dispatch."
            )
        return f"Unknown tool: {name}"
    try:
        return fn(ctx, **_normalize_args(name, args))
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Tool {name} crashed: {e}"
