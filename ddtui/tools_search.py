"""File enumeration, regex search, and glob tool implementations.

search_content and glob_files prefer ripgrep when it is installed —
one to two orders of magnitude faster on big trees, .gitignore-aware,
and with glob semantics identical to ours. Every rg failure mode
(binary missing, unsupported regex syntax, timeout) falls back to the
pure-Python implementation, so behavior degrades gracefully rather
than erroring. Set DDTUI_NO_RIPGREP=1 to force the Python paths.

Behavior parity notes: the rg paths pass --hidden plus explicit
exclusions mirroring DEFAULT_IGNORED_DIRS / _FILE_PATTERNS, so the two
implementations see the same tree — except that rg additionally
respects .gitignore (a strict improvement for source-focused search).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .state import ToolContext
from .tool_utils import (
    DEFAULT_IGNORED_DIRS,
    DEFAULT_IGNORED_FILE_PATTERNS,
    _is_ignored_path,
    _matches_glob,
    _prune_ignored_dirs,
    _safe_path,
)

SEARCH_MAX_MATCHES = 500
SEARCH_MAX_FILES = 2000       # python-path candidate cap (rg has no cap)
GLOB_MAX_RESULTS = 1000
_RG_TIMEOUT = 15

_RG_PATH: str | None = shutil.which("rg")
if os.environ.get("DDTUI_NO_RIPGREP", "").strip().lower() in (
    "1", "true", "yes", "on",
):
    _RG_PATH = None


def _rg_ignore_globs() -> list[str]:
    """Exclusions mirroring the built-in ignore lists, so the rg paths
    and the Python paths see the same tree."""
    args: list[str] = []
    for d in sorted(DEFAULT_IGNORED_DIRS):
        args += ["-g", f"!{d}/"]
    for pat in DEFAULT_IGNORED_FILE_PATTERNS:
        args += ["-g", f"!{pat}"]
    args += ["-g", "!*.egg-info/"]
    return args


def _run_rg(args: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    """Run ripgrep; None means 'unusable, fall back to Python'.

    rg exit codes: 0 = matches, 1 = no matches, 2 = error (e.g. a
    Python-regex construct Rust's engine rejects, like lookbehind).
    """
    if _RG_PATH is None:
        return None
    try:
        r = subprocess.run(
            [_RG_PATH, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_RG_TIMEOUT,
        )
    except Exception:
        return None
    if r.returncode not in (0, 1):
        return None
    return r


def _strip_dot_prefix(rel: str) -> str:
    return rel[2:] if rel.startswith("./") else rel


# ───────── list_files ─────────

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


# ───────── search_content ─────────

def _search_rg(
    p: Path, pattern: str, file_filter: str, case_sensitive: bool
) -> list[str] | None:
    """ripgrep search path. Returns formatted match lines, or None to
    fall back (rg missing / regex unsupported / rg error).

    file_filter is applied in Python via _matches_glob, NOT passed as
    rg -g: command-line globs are rg's highest-precedence override
    layer, so a whitelist -g resurrects .gitignore'd files — exactly
    the wrong interaction. Post-filtering match lines is cheap and
    keeps one authoritative glob semantics (ours, tested).
    """
    args = [
        "--line-number", "--no-heading", "--color=never",
        "--max-columns", "4096", "--max-columns-preview",
        "--max-count", str(SEARCH_MAX_MATCHES),  # per-file blowup guard
        "--hidden",
        *_rg_ignore_globs(),
    ]
    if not case_sensitive:
        args.append("-i")
    args += ["-e", pattern, "--", "."]
    r = _run_rg(args, cwd=str(p))
    if r is None:
        return None
    want_filter = bool(file_filter) and file_filter != "*"
    matches: list[str] = []
    for ln in r.stdout.splitlines():
        parts = ln.split(":", 2)
        if len(parts) != 3:
            continue
        rel, lineno, content = parts
        rel = _strip_dot_prefix(rel)
        if want_filter and not _matches_glob(rel, file_filter):
            continue
        matches.append(f"{rel}:{lineno}: {content.rstrip()}")
        if len(matches) >= SEARCH_MAX_MATCHES:
            matches.append(f"…[truncated at {SEARCH_MAX_MATCHES} matches]")
            break
    return matches


def _search_python(
    p: Path, regex: re.Pattern, file_filter: str
) -> list[str]:
    """Pure-Python fallback: os.walk + line regex."""
    matches: list[str] = []
    files_to_search: list[Path] = []
    file_scan_truncated = False
    for root, dirs, files in os.walk(p):
        _prune_ignored_dirs(root, dirs)
        for f in files:
            fpath = Path(root, f)
            if _is_ignored_path(fpath):
                continue
            rel = fpath.relative_to(p).as_posix()
            if _matches_glob(rel, file_filter):
                files_to_search.append(fpath)
        if len(files_to_search) > SEARCH_MAX_FILES:
            # Don't scan massive trees — but say so instead of
            # silently reporting a partial result as complete.
            file_scan_truncated = True
            break

    for fpath in files_to_search:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        rel = os.path.relpath(str(fpath), str(p))
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(matches) >= SEARCH_MAX_MATCHES:
                            matches.append(
                                f"…[truncated at {SEARCH_MAX_MATCHES} matches]"
                            )
                            return matches
        except Exception:
            continue

    if file_scan_truncated:
        matches.append(
            f"…[only the first {SEARCH_MAX_FILES} candidate files were "
            "searched; results may be incomplete — narrow path or "
            "file_filter to cover the rest]"
        )
    return matches


def tool_search_content(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    file_filter: str = "*",
    case_sensitive: bool = False,
) -> str:
    """Grep-like regex search over files (ripgrep-accelerated)."""
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"

    # Validate against Python's re up front: it is the documented
    # pattern dialect, and the fallback path needs it anyway.
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    if p.is_file():
        matches: list[str] = []
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        matches.append(f"{p.name}:{lineno}: {line.rstrip()}")
                        if len(matches) >= SEARCH_MAX_MATCHES:
                            matches.append(
                                f"…[truncated at {SEARCH_MAX_MATCHES} matches]"
                            )
                            break
        except Exception as e:
            return f"Error reading {p}: {e}"
        return "\n".join(matches) if matches else "(no matches)"

    matches = _search_rg(p, pattern, file_filter, case_sensitive)
    if matches is None:
        matches = _search_python(p, regex, file_filter)
    return "\n".join(matches) if matches else "(no matches)"


# ───────── glob_files ─────────

def _glob_rg(p: Path, pattern: str) -> list[Path] | None:
    """ripgrep file listing. Returns absolute Paths, or None to fall back.

    The pattern is matched in Python (_matches_glob), not via rg -g —
    see _search_rg for why (-g overrides .gitignore)."""
    args = ["--files", "--hidden", *_rg_ignore_globs()]
    r = _run_rg(args, cwd=str(p))
    if r is None:
        return None
    out: list[Path] = []
    for ln in r.stdout.splitlines():
        if not ln:
            continue
        rel = _strip_dot_prefix(ln)
        if _matches_glob(rel, pattern):
            out.append(p / rel)
    return out


def _glob_python(p: Path, pattern: str) -> list[Path]:
    """Pure-Python fallback: os.walk + _matches_glob."""
    matches: list[Path] = []
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
            if len(matches) >= GLOB_MAX_RESULTS * 5:
                return matches
        if len(matches) >= GLOB_MAX_RESULTS * 5:
            break
    return matches


def tool_glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    """Recursive glob: returns file paths matching *pattern* under *path*.

    Common VCS/cache/build artifacts are skipped by default so patterns
    like `**/*.py` stay source-focused instead of returning `.git`,
    `__pycache__`, or egg-info contents. Sorted by mtime descending so
    recently-touched files come first. ripgrep-accelerated (and then
    .gitignore-aware) when available.
    """
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"
    if not p.is_dir():
        return f"Error: not a directory: {p}"

    try:
        matches = _glob_rg(p, pattern)
        if matches is None:
            matches = _glob_python(p, pattern)
    except Exception as e:
        return f"Error: glob failed: {e}"

    if not matches:
        return f"(no files matching {pattern!r} under {p})"

    try:
        matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    except Exception:
        pass

    truncated = len(matches) > GLOB_MAX_RESULTS
    shown = matches[:GLOB_MAX_RESULTS]
    lines: list[str] = []
    for m in shown:
        try:
            lines.append(str(m.relative_to(p)))
        except ValueError:
            lines.append(str(m))

    head = f"{len(matches)}{' (truncated)' if truncated else ''} match(es) under {p}:"
    out = head + "\n" + "\n".join(lines)
    if truncated:
        out += f"\n…[truncated at {GLOB_MAX_RESULTS} files]"
    return out
