"""File read/write/edit tool implementations."""

from __future__ import annotations

from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_WRITES,
    READ_FILE_MAX_LINE_CHARS,
    READ_FILE_MAX_LINES,
    READ_FILE_MAX_TOTAL_CHARS,
)
from .state import ToolContext
from .tool_utils import _safe_path, _sandbox_error, _unified_diff


def _note_file_known(ctx: ToolContext, p: Path) -> None:
    """Record that the model has current knowledge of *p*'s contents.

    Called after a successful read_file AND after every successful
    write/edit (the model knows what it just wrote), so the natural
    read → edit → write_file flow never trips the overwrite guard.
    """
    try:
        ctx.read_files[str(p)] = p.stat().st_mtime
    except OSError:
        pass


def _overwrite_error(ctx: ToolContext, p: Path) -> str | None:
    """Read-before-overwrite guard for whole-file writes.

    An existing file may only be overwritten if the model read (or
    wrote/edited) it this session and it hasn't changed on disk since.
    """
    if not p.exists():
        return None
    recorded = ctx.read_files.get(str(p))
    if recorded is None:
        return (
            f"Error: {p} already exists and you have not read it in this "
            "session. read_file it first so you know what you are "
            "replacing, or use edit_file/multi_edit for incremental "
            "changes."
        )
    try:
        current = p.stat().st_mtime
    except OSError:
        # stat raced with deletion — fall through; the write itself
        # will surface any real error.
        return None
    if current != recorded:
        return (
            f"Error: {p} changed on disk after you last read it. "
            "read_file it again before overwriting."
        )
    return None


def tool_read_file(
    ctx: ToolContext,
    path: str,
    offset: int = 1,
    limit: int = READ_FILE_MAX_LINES,
) -> str:
    """Read a file with optional pagination.

    Each output line is prefixed with its 1-indexed line number
    (``   N\\tcontent``, cat -n style) so the model can reference and
    edit by line without counting. The same base is used by edit_lines'
    start_line/end_line and by offset here.

    Two caps protect the context window: single lines are clipped at
    READ_FILE_MAX_LINE_CHARS (minified JS/JSON), and the whole result is
    capped at READ_FILE_MAX_TOTAL_CHARS with a resume hint.
    """
    p = _safe_path(ctx.work_dir, path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    lines = text.splitlines()
    total = len(lines)
    if offset < 1:
        offset = 1
    if offset > total:
        return f"File has {total} line(s); offset {offset} (1-indexed) is out of range."
    if limit <= 0:
        limit = READ_FILE_MAX_LINES

    start = offset - 1
    chunk = lines[start : start + limit]

    body_parts: list[str] = []
    body_chars = 0
    shown = 0
    budget_hit = False
    for i, line in enumerate(chunk):
        if len(line) > READ_FILE_MAX_LINE_CHARS:
            line = (
                line[:READ_FILE_MAX_LINE_CHARS]
                + f" …[line clipped; {len(line)} chars total]"
            )
        numbered = f"{offset + i:6d}\t{line}"
        if body_chars + len(numbered) > READ_FILE_MAX_TOTAL_CHARS and shown > 0:
            budget_hit = True
            break
        body_parts.append(numbered)
        body_chars += len(numbered) + 1  # +1 for the joining newline
        shown += 1

    end_line = offset + shown - 1
    header = f"[{p}] lines {offset}-{end_line} of {total}"
    out = header + "\n" + "\n".join(body_parts)
    if budget_hit:
        out += (
            f"\n…[output capped at {READ_FILE_MAX_TOTAL_CHARS} chars; "
            f"continue with offset={end_line + 1}]"
        )
    _note_file_known(ctx, p)
    return out


def tool_write_file(
    ctx: ToolContext, path: str, content: str, force: bool = True
) -> str:
    """Create a new file, or overwrite one the model has read.

    `force` is accepted for wire-compat with older conversations but has
    no effect — the read-before-overwrite guard is unconditional.
    """
    p = _safe_path(ctx.work_dir, path)
    if not ALLOW_UNSANDBOXED_WRITES:
        err = _sandbox_error(ctx.work_dir, p, kind="write file")
        if err:
            return err
    err = _overwrite_error(ctx, p)
    if err:
        return err
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _note_file_known(ctx, p)
        return f"Wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"Error writing {p}: {e}"


def tool_edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    occurrence: int | None = None,
    replace_all: bool = False,
) -> str:
    """Replace one occurrence of old_string with new_string, or every
    occurrence when *replace_all* is set.

    When neither *occurrence* nor *replace_all* is given and old_string
    matches more than once, return contextual information about every
    match so the caller can refine the request.
    """
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: old_string and new_string must be strings"
    if old_string == "":
        return "Error: old_string must be non-empty"
    if replace_all and occurrence is not None:
        return "Error: replace_all and occurrence are mutually exclusive — pick one"

    p = _safe_path(ctx.work_dir, path)
    if not ALLOW_UNSANDBOXED_WRITES:
        err = _sandbox_error(ctx.work_dir, p, kind="edit file")
        if err:
            return err
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    # Count and locate matches
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(old_string, start)
        if idx == -1:
            break
        positions.append(idx)
        # Non-overlapping, so the match count here agrees with
        # str.count()/str.replace() used elsewhere (multi_edit).
        start = idx + len(old_string)

    if not positions:
        return f"Error: old_string not found in {p}"

    if replace_all:
        new_text = text.replace(old_string, new_string)
        try:
            p.write_text(new_text)
        except Exception as e:
            return f"Error writing {p}: {e}"
        _note_file_known(ctx, p)
        diff = _unified_diff(text, new_text, p)
        return (
            f"Edited {p} (replaced all {len(positions)} occurrence(s))\n\n"
            f"{diff}"
        )

    if len(positions) > 1 and occurrence is None:
        # Build context for each match
        lines = text.splitlines()
        # Build a cheap char→line map
        char_to_line: list[int] = []
        for i, line in enumerate(lines):
            char_to_line.extend([i] * (len(line) + 1))  # +1 for newline
        ctx_parts = [
            f"old_string matches {len(positions)} times in {p}. "
            "Specify occurrence=N or add more surrounding context to make it unique.\n"
            "Matches:"
        ]
        for i, pos in enumerate(positions, 1):
            if pos < len(char_to_line):
                lineno = char_to_line[pos] + 1  # 1-indexed
            else:
                lineno = -1
            # Extract surrounding snippet (up to 3 lines around)
            before = text[max(0, pos - 80):pos]
            snippet = text[pos:pos + len(old_string)]
            after = text[pos + len(old_string):pos + len(old_string) + 80]
            ctx_parts.append(
                f"  [{i}] line ~{lineno}  …{before!r}>>>{snippet}<<<{after!r}…"
            )
        return "\n".join(ctx_parts)

    # Determine which occurrence to replace
    if occurrence is not None:
        if occurrence < 1 or occurrence > len(positions):
            return (
                f"Error: occurrence={occurrence} out of range; "
                f"there are {len(positions)} match(es) in {p}"
            )
        idx = positions[occurrence - 1]
    else:
        idx = positions[0]

    new_text = text[:idx] + new_string + text[idx + len(old_string):]
    try:
        p.write_text(new_text)
    except Exception as e:
        return f"Error writing {p}: {e}"
    _note_file_known(ctx, p)
    diff = _unified_diff(text, new_text, p)
    return (
        f"Edited {p} (replaced occurrence {occurrence or 1} of {len(positions)})\n\n"
        f"{diff}"
    )


def tool_apply_patch(ctx: ToolContext, **_kwargs) -> str:
    """Removed tool, kept as a redirect stub.

    apply_patch was a pure forwarding alias over edit_file/multi_edit,
    giving models three interchangeable entry points for the same exact
    replacement. It is gone from the advertised schema; this stub
    catches calls from models with an apply_patch prior (Codex) or from
    replayed old conversations and points them at the real tools.
    """
    return (
        "Error: apply_patch has been removed. Use edit_file for one exact "
        "replacement (replace_all=true replaces every occurrence), "
        "multi_edit for several replacements in one file, or write_file "
        "to create/overwrite a whole file."
    )


def tool_edit_lines(
    ctx: ToolContext,
    path: str,
    mode: str,
    start_line: int,
    end_line: int | None = None,
    content: str = "",
) -> str:
    """Edit a file by line numbers (1-indexed).

    mode == "replace": lines [start_line..end_line] inclusive → content
    mode == "insert":  insert content before start_line; end_line ignored
    mode == "delete":  remove lines [start_line..end_line]; content ignored
    """
    p = _safe_path(ctx.work_dir, path)
    if not ALLOW_UNSANDBOXED_WRITES:
        err = _sandbox_error(ctx.work_dir, p, kind="edit file")
        if err:
            return err
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    had_trailing_newline = text.endswith("\n")
    lines = text.splitlines()  # ["a", "b", "c"] — no line endings retained
    n = len(lines)

    def _split_content(c: str) -> list[str]:
        # Drop a single trailing newline so content="line\n" inserts ONE line,
        # not two (the second being empty).
        if c.endswith("\n"):
            c = c[:-1]
        return c.split("\n")

    if mode == "replace":
        if end_line is None:
            return "Error: replace mode requires end_line"
        if start_line < 1 or end_line < 1:
            return "Error: line numbers must be >= 1"
        if start_line > n or end_line > n:
            return f"Error: range [{start_line}..{end_line}] out of bounds; file has {n} line(s)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"
        new_block = _split_content(content)
        result_lines = lines[: start_line - 1] + new_block + lines[end_line:]
        action = f"replaced lines {start_line}-{end_line} with {len(new_block)} line(s)"
    elif mode == "insert":
        if start_line < 1:
            return "Error: line number must be >= 1"
        if start_line > n + 1:
            return (
                f"Error: start_line {start_line} out of bounds; "
                f"file has {n} line(s) (max insert position {n + 1})"
            )
        new_block = _split_content(content)
        result_lines = lines[: start_line - 1] + new_block + lines[start_line - 1 :]
        action = f"inserted {len(new_block)} line(s) before line {start_line}"
    elif mode == "delete":
        if end_line is None:
            return "Error: delete mode requires end_line"
        if start_line < 1 or end_line < 1:
            return "Error: line numbers must be >= 1"
        if start_line > n or end_line > n:
            return f"Error: range [{start_line}..{end_line}] out of bounds; file has {n} line(s)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"
        result_lines = lines[: start_line - 1] + lines[end_line:]
        action = f"deleted lines {start_line}-{end_line} ({end_line - start_line + 1} line(s))"
    else:
        return f"Error: unknown mode '{mode}'. Use 'replace', 'insert', or 'delete'."

    new_text = "\n".join(result_lines)
    if result_lines and had_trailing_newline:
        new_text += "\n"

    try:
        p.write_text(new_text)
    except Exception as e:
        return f"Error writing {p}: {e}"
    _note_file_known(ctx, p)
    diff = _unified_diff(text, new_text, p)
    return (
        f"Edited {p}: {action}; file now has {len(result_lines)} line(s)\n\n"
        f"{diff}"
    )


def _replace_nth(buffer: str, old: str, new: str, n: int) -> tuple[str, int]:
    """Replace the *n*-th (1-indexed) occurrence of *old* with *new* in
    *buffer*. Returns (new_buffer, total_match_count). Caller is
    responsible for validating n against the count."""
    positions: list[int] = []
    start = 0
    while True:
        idx = buffer.find(old, start)
        if idx == -1:
            break
        positions.append(idx)
        # Non-overlapping to match buffer.count(old), which callers use
        # to validate the occurrence index against.
        start = idx + len(old)
    if n < 1 or n > len(positions):
        return buffer, len(positions)
    idx = positions[n - 1]
    return buffer[:idx] + new + buffer[idx + len(old):], len(positions)


def tool_multi_edit(ctx: ToolContext, path: str, edits: list | None = None) -> str:
    """Apply a list of string replacements to one file atomically."""
    p = _safe_path(ctx.work_dir, path)
    if not ALLOW_UNSANDBOXED_WRITES:
        err = _sandbox_error(ctx.work_dir, p, kind="edit file")
        if err:
            return err
    try:
        before = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    if not isinstance(edits, list) or not edits:
        return "Error: 'edits' must be a non-empty array of edit operations"

    buffer = before
    summary: list[str] = []
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return f"Edit #{i}: must be a JSON object"
        old = edit.get("old_string")
        new = edit.get("new_string")
        occurrence = edit.get("occurrence")
        replace_all = bool(edit.get("replace_all", False))
        if old is None or new is None:
            return f"Edit #{i}: missing 'old_string' or 'new_string'"
        if not isinstance(old, str) or not isinstance(new, str):
            return f"Edit #{i}: 'old_string' and 'new_string' must be strings"
        if old == "":
            return f"Edit #{i}: old_string must be non-empty"
        if old == new:
            return f"Edit #{i}: old_string == new_string (no-op)"
        if replace_all and occurrence is not None:
            return (
                f"Edit #{i}: replace_all and occurrence are mutually "
                "exclusive — pick one"
            )

        count = buffer.count(old)
        if count == 0:
            return (
                f"Edit #{i}: old_string not found in current buffer. "
                "Note that earlier edits in this call may have already "
                "transformed the file; old_string must match the buffer "
                "AT THE TIME this edit runs."
            )

        if replace_all:
            buffer = buffer.replace(old, new)
            summary.append(f"  #{i}: replaced all {count} occurrence(s)")
        elif occurrence is not None:
            if not isinstance(occurrence, int):
                return f"Edit #{i}: 'occurrence' must be an integer"
            if occurrence < 1 or occurrence > count:
                return (
                    f"Edit #{i}: occurrence={occurrence} out of range "
                    f"(buffer has {count} match(es))"
                )
            buffer, _ = _replace_nth(buffer, old, new, occurrence)
            summary.append(
                f"  #{i}: replaced occurrence {occurrence}/{count}"
            )
        else:
            if count > 1:
                return (
                    f"Edit #{i}: old_string matches {count} times in the "
                    "current buffer. Set replace_all=true, set "
                    "occurrence=N (1-indexed), or expand old_string with "
                    "more surrounding context to make it unique."
                )
            buffer = buffer.replace(old, new, 1)
            summary.append(f"  #{i}: replaced 1 occurrence")

    if buffer == before:
        return "No changes — every edit was a no-op."

    try:
        p.write_text(buffer)
    except Exception as e:
        return f"Error writing {p}: {e}"
    _note_file_known(ctx, p)

    diff = _unified_diff(before, buffer, p)
    head = (
        f"multi_edit applied {len(edits)} edit(s) to {p}:\n"
        + "\n".join(summary)
    )
    return f"{head}\n\n{diff}"
