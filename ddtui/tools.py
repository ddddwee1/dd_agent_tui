"""Tool facade: schemas plus dispatch table.

The large tool implementation set is split by domain:
- `tool_schemas` keeps the JSON schemas sent to the LLM.
- `tools_bash`, `tools_files`, `tools_search`, `tools_web`, and
  `tools_todo` contain the concrete Python implementations.

`execute_tool` remains the single public dispatch entry point used by
`AgentApp` and subagents.
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
from .tools_files import (
    tool_edit_file,
    tool_edit_lines,
    tool_multi_edit,
    tool_read_file,
    tool_write_file,
)
from .tools_search import tool_glob_files, tool_list_files, tool_search_content
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
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "edit_lines": tool_edit_lines,
    "multi_edit": tool_multi_edit,
    "list_files": tool_list_files,
    "glob_files": tool_glob_files,
    "search_content": tool_search_content,
    "web_fetch": tool_web_fetch,
    "web_search": tool_web_search,
    "todo_tool": tool_todo_tool,
}


def execute_tool(ctx: ToolContext, name: str, args: dict) -> str:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(ctx, **args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Tool {name} crashed: {e}"
