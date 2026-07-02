"""Tool facade: the single tool registry plus dispatch.

The large tool implementation set is split by domain:
- `tool_schemas` keeps the JSON schemas sent to the LLM.
- `tools_bash`, `tools_files`, `tools_search`, `tools_web`, and
  `tools_checkpoint`, `tools_notes`, `tools_tasks`, `tools_terminal`,
  `tools_todo` contain the concrete Python implementations.

TOOL_REGISTRY is the single source of truth for per-tool runtime
metadata. Everything that used to be a hand-maintained parallel list —
which tools the app layer intercepts, which need a write confirmation,
which results carry a peelable diff, which are hidden from subagents,
which may run concurrently — is DERIVED from it. Add a tool once, tag
it once.

`execute_tool` is the dispatch entry point for the stateless tool set
(every spec with a `func`). Specs with `func=None` are app-level meta
tools that need the live LLM client / app state — AgentApp handles them
before generic dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .state import ToolContext
from .tool_schemas import TOOLS
from .tools_bash import tool_bash
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

__all__ = [
    "TOOLS",
    "TOOL_REGISTRY",
    "TOOL_FUNCS",
    "APP_DISPATCHED_TOOLS",
    "CONFIRM_TOOLS",
    "DIFF_STRIP_TOOLS",
    "PARALLEL_SAFE_TOOLS",
    "SUBAGENT_BLOCKED_TOOLS",
    "PARENT_TOOL_SCHEMAS",
    "SUBAGENT_TOOL_SCHEMAS",
    "execute_tool",
]


@dataclass(frozen=True)
class ToolSpec:
    """Runtime metadata for one tool — the registry entry.

    func=None marks an app-level meta tool (needs the live LLM client /
    AgentApp state); it is intercepted by the host before generic
    dispatch. `parallel` means one call is read-only / side-effect-free,
    so a run of adjacent parallel-safe calls in one tool batch may
    execute concurrently. `parent` / `subagent` control which agent
    sees the schema.
    """

    name: str
    func: Callable[..., str] | None = None
    confirm: bool = False      # gate behind the tool_confirm hook
    strip_diff: bool = False   # result body carries a peelable unified diff
    parallel: bool = False     # safe to run concurrently with neighbors
    parent: bool = True        # advertised to the parent agent
    subagent: bool = True      # advertised to subagent sessions


def _specs() -> list[ToolSpec]:
    S = ToolSpec
    return [
        # shell / tasks / terminals
        S("bash", tool_bash),
        S("task_start", tool_task_start),
        S("task_check", tool_task_check, parallel=True),
        S("task_read", tool_task_read, parallel=True),
        S("task_wait", tool_task_wait),
        S("task_kill", tool_task_kill),
        S("task_list", tool_task_list, parallel=True),
        S("terminal_start", tool_terminal_start),
        S("terminal_send", tool_terminal_send),
        S("terminal_read", tool_terminal_read),
        S("terminal_interrupt", tool_terminal_interrupt),
        S("terminal_close", tool_terminal_close),
        S("terminal_list", tool_terminal_list, parallel=True),
        # files
        S("read_file", tool_read_file, parallel=True),
        S("write_file", tool_write_file, confirm=True),
        S("edit_file", tool_edit_file, confirm=True, strip_diff=True),
        S("edit_lines", tool_edit_lines, confirm=True, strip_diff=True),
        S("multi_edit", tool_multi_edit, confirm=True, strip_diff=True),
        # apply_patch is a removed-tool redirect stub: dispatchable (so
        # models with the prior get a pointer, not "Unknown tool") but
        # absent from the schema list, and never touches disk.
        S("apply_patch", tool_apply_patch, parallel=True),
        # search
        S("list_files", tool_list_files, parallel=True),
        S("glob_files", tool_glob_files, parallel=True),
        S("search_content", tool_search_content, parallel=True),
        # web
        S("web_fetch", tool_web_fetch, parallel=True),
        S("web_search", tool_web_search, parallel=True),
        # progress / memory
        S("todo_tool", tool_todo_tool),
        S("checkpoint_tool", tool_checkpoint_tool, subagent=False),
        S("checkpoint_get", tool_checkpoint_get, parallel=True, subagent=False),
        S("checkpoint_clear", tool_checkpoint_clear, subagent=False),
        S("project_note_add", tool_project_note_add),
        S("project_note_search", tool_project_note_search, parallel=True),
        S("project_note_list", tool_project_note_list, parallel=True),
        S("project_note_read", tool_project_note_read, parallel=True),
        S("project_note_update", tool_project_note_update),
        S("project_note_delete", tool_project_note_delete),
        # app-level meta tools (func=None): need the live LLM client /
        # app state, dispatched by the host loop before execute_tool.
        S("compact_self", None, parent=False),           # subagent-only
        S("explore_start", None, subagent=False),
        S("explore_end", None, subagent=False),
        S("explore_cancel", None, subagent=False),
        S("spawn_agent", None, subagent=False),
        S("chat_agent", None, subagent=False),
        S("await_agent", None, subagent=False),
        S("end_agent", None, subagent=False),
    ]


TOOL_REGISTRY: dict[str, ToolSpec] = {s.name: s for s in _specs()}

# ── derived views (the old hand-maintained lists) ──

TOOL_FUNCS: dict[str, Callable[..., str]] = {
    s.name: s.func for s in TOOL_REGISTRY.values() if s.func is not None
}
APP_DISPATCHED_TOOLS: frozenset[str] = frozenset(
    s.name for s in TOOL_REGISTRY.values() if s.func is None
)
CONFIRM_TOOLS: frozenset[str] = frozenset(
    s.name for s in TOOL_REGISTRY.values() if s.confirm
)
DIFF_STRIP_TOOLS: frozenset[str] = frozenset(
    s.name for s in TOOL_REGISTRY.values() if s.strip_diff
)
PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset(
    s.name for s in TOOL_REGISTRY.values() if s.parallel
)
SUBAGENT_BLOCKED_TOOLS: frozenset[str] = frozenset(
    s.name for s in TOOL_REGISTRY.values() if not s.subagent
)

PARENT_TOOL_SCHEMAS: list[dict] = [
    t for t in TOOLS if TOOL_REGISTRY[t["function"]["name"]].parent
]
SUBAGENT_TOOL_SCHEMAS: list[dict] = [
    t for t in TOOLS if TOOL_REGISTRY[t["function"]["name"]].subagent
]

# Drift guard: every advertised schema must have a registry entry (the
# reverse is allowed — apply_patch is dispatchable but unadvertised).
_schema_names = {t["function"]["name"] for t in TOOLS}
_missing = _schema_names - set(TOOL_REGISTRY)
if _missing:
    raise RuntimeError(
        f"tool_schemas.TOOLS entries missing from TOOL_REGISTRY: {_missing}"
    )


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


def execute_tool(ctx: ToolContext, name: str, args: dict) -> str:
    """Dispatch a tool call to its implementation in TOOL_FUNCS.

    This is the dispatch entry point for the stateless tool set. App-
    level meta tools (APP_DISPATCHED_TOOLS) need the live LLM client /
    app state and are handled by the host before they reach here.
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
