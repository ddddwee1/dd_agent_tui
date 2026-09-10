"""Bounded access to this conversation's archived source evidence."""

import json

from .history_archive import read_archive, search_archive
from .state import ToolContext


def tool_history_search(ctx: ToolContext, query: str, scope: str = "",
                        limit: int = 5, offset: int = 0) -> str:
    try:
        result = search_archive(ctx.session_id, query, scope=scope, limit=limit, offset=offset)
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, FileNotFoundError) as exc:
        return f"Error: {exc}"


def tool_history_read(ctx: ToolContext, ref: str, start: int = 0,
                      max_chars: int = 6000) -> str:
    try:
        return json.dumps(read_archive(ctx.session_id, ref, start=start, max_chars=max_chars),
                          ensure_ascii=False)
    except (ValueError, FileNotFoundError) as exc:
        return f"Error: {exc}"
