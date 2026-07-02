"""File enumeration, regex search, and glob tool implementations."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .state import ToolContext
from .tool_utils import _is_ignored_path, _matches_glob, _prune_ignored_dirs, _safe_path


def tool_list_files(
    ctx: ToolContext,
    path: str = ".",
    depth: int = 2,
    file_filter: str | None = None,
) -> str:
    """List files and directories recursively, with depth limit and glob filter."""
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"
    if not p.is_dir():
        # Single file – just show it
        return str(p)

    entries: list[str] = []
    depth_truncated = False
    for root, dirs, files in os.walk(p):
        rel = os.path.relpath(root, p)
        if rel == ".":
            current_depth = 0
        else:
            current_depth = rel.count(os.sep) + 1

        if current_depth > depth:
            if dirs or files:
                depth_truncated = True
            dirs[:] = []  # prune deeper dirs
            continue

        _prune_ignored_dirs(root, dirs)

        for d in sorted(dirs):
            entry = os.path.join(rel, d) + "/"
            entries.append(entry if rel != "." else f"{d}/")
        for f in sorted(files):
            fpath = Path(root, f)
            if _is_ignored_path(fpath):
                continue
            entry = os.path.join(rel, f) if rel != "." else f
            if file_filter and not _matches_glob(entry, file_filter):
                continue
            entries.append(entry)

    if not entries:
        return f"{p} (empty or no files matching filter)"
    out = "\n".join(entries)
    if depth_truncated:
        out += (
            f"\n…[depth limit {depth} reached; deeper entries hidden — "
            f"increase depth or list a subdirectory to see more]"
        )
    return out


def tool_search_content(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    file_filter: str = "*",
    case_sensitive: bool = False,
) -> str:
    """Grep-like regex search over files."""
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    MAX_MATCHES = 500
    matches: list[str] = []

    if p.is_file():
        files_to_search = [p]
    else:
        files_to_search = []
        for root, dirs, files in os.walk(p):
            _prune_ignored_dirs(root, dirs)
            for f in files:
                fpath = Path(root, f)
                if _is_ignored_path(fpath):
                    continue
                rel = fpath.relative_to(p).as_posix()
                if _matches_glob(rel, file_filter):
                    files_to_search.append(fpath)
            if len(files_to_search) > 2000:
                break  # don't scan massive trees

    for fpath in files_to_search:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        rel = os.path.relpath(str(fpath), str(p))
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(matches) >= MAX_MATCHES:
                            matches.append(f"…[truncated at {MAX_MATCHES} matches]")
                            return "\n".join(matches)
        except Exception:
            continue

    return "\n".join(matches) if matches else "(no matches)"


def tool_glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    """Recursive glob: returns file paths matching *pattern* under *path*.

    Common VCS/cache/build artifacts are skipped by default so patterns
    like `**/*.py` stay source-focused instead of returning `.git`,
    `__pycache__`, or egg-info contents. Sorted by mtime descending so
    recently-touched files come first.
    """
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"
    if not p.is_dir():
        return f"Error: not a directory: {p}"

    MAX_RESULTS = 1000
    matches: list[Path] = []
    try:
        for root, dirs, files in os.walk(p):
            _prune_ignored_dirs(root, dirs)
            for f in files:
                match = Path(root, f)
                if _is_ignored_path(match):
                    continue
                rel = match.relative_to(p).as_posix()
                if not _matches_glob(rel, pattern):
                    continue
                matches.append(match)
                # Hard cap on scan to avoid pathological patterns blowing memory.
                if len(matches) >= MAX_RESULTS * 5:
                    break
            if len(matches) >= MAX_RESULTS * 5:
                break
    except Exception as e:
        return f"Error: glob failed: {e}"

    if not matches:
        return f"(no files matching {pattern!r} under {p})"

    try:
        matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    except Exception:
        pass

    truncated = len(matches) > MAX_RESULTS
    shown = matches[:MAX_RESULTS]
    lines: list[str] = []
    for m in shown:
        try:
            lines.append(str(m.relative_to(p)))
        except ValueError:
            lines.append(str(m))

    head = f"{len(matches)}{' (truncated)' if truncated else ''} match(es) under {p}:"
    out = head + "\n" + "\n".join(lines)
    if truncated:
        out += f"\n…[truncated at {MAX_RESULTS} files]"
    return out
