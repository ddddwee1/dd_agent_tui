"""Shared helpers for tool implementations."""

from __future__ import annotations

import difflib
import fnmatch
import re
from pathlib import Path

from .config import DANGEROUS_SHELL_PATTERNS


_BASH_HARD_CHAR_CAP = 100_000


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and parents) and restrict it to the owner (0o700).

    Used for the runtime log dir that holds task/terminal command output,
    which can be sensitive and must not be world-readable the way /tmp is.
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _safe_path(work_dir: str, raw: str) -> Path:
    """Resolve a user-supplied path relative to *work_dir*.

    Returns an absolute Path. Does NOT enforce a sandbox by itself —
    callers that execute or mutate should use _sandbox_error().
    """
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return Path(work_dir, p).resolve()


def _sandbox_error(work_dir: str, p: Path, *, kind: str) -> str | None:
    """Return an error if *p* is outside work_dir for dangerous actions."""
    root = Path(work_dir).resolve()
    try:
        p.resolve().relative_to(root)
    except ValueError:
        return (
            f"Error: refusing to {kind} outside the project work_dir. "
            f"Path: {p.resolve()} · work_dir: {root}. "
            f"Set DDTUI_ALLOW_UNSANDBOXED_WRITES=1 for write/edit tools "
            f"or DDTUI_ALLOW_UNSANDBOXED_BASH=1 for bash workdirs to opt out."
        )
    return None


def _check_dangerous(command: str) -> str | None:
    """Return an error string if *command* matches any dangerous pattern, else None."""
    for pat in DANGEROUS_SHELL_PATTERNS:
        if re.search(pat, command):
            return f"Blocked: command matches dangerous pattern /{pat}/"
    return None


def _truncate_output(text: str, max_chars: int | None) -> str:
    """Truncate *text* to *max_chars* if set, respecting the hard cap."""
    if max_chars is None:
        return text
    try:
        cap = max(1, min(_BASH_HARD_CHAR_CAP, int(max_chars)))
    except (TypeError, ValueError):
        return text
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…[+{len(text) - cap} chars]"


def _unified_diff(before: str, after: str, p: Path, context: int = 2) -> str:
    """Render a unified-diff between two file contents. Used in edit_* tool
    results so both the model and the UI can see exactly what changed."""
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            lineterm="",
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            n=context,
        )
    )
    return "\n".join(diff_lines) if diff_lines else "(no changes)"


DEFAULT_IGNORED_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "ENV",
    "build",
    "dist",
    ".eggs",
    "node_modules",
})


DEFAULT_IGNORED_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.egg-info",
    ".DS_Store",
    "Thumbs.db",
)


def _is_ignored_path(path: Path) -> bool:
    """Return True for common VCS/cache/build artifacts.

    The tools default to a compact, source-focused view so the model
    doesn't waste context on `.git`, bytecode caches, egg metadata, or
    build outputs. This is intentionally a small built-in ignore list;
    it is not a security boundary.
    """
    for part in path.parts:
        if part in DEFAULT_IGNORED_DIRS:
            return True
        if part.endswith(".egg-info"):
            return True
    name = path.name
    return any(fnmatch.fnmatch(name, pat) for pat in DEFAULT_IGNORED_FILE_PATTERNS)


def _prune_ignored_dirs(root: str, dirs: list[str]) -> None:
    root_path = Path(root)
    dirs[:] = [d for d in dirs if not _is_ignored_path(root_path / d)]


def _matches_glob(path: str, pattern: str) -> bool:
    """Match a POSIX-style relative path against a user glob pattern."""
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
