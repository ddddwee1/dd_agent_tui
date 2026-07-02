"""search_content / glob_files: ripgrep path, Python fallback, parity."""

import shutil

import pytest

import ddtui.tools_search as ts
from ddtui.state import ToolContext

HAS_RG = shutil.which("rg") is not None


@pytest.fixture
def tree(tmp_path):
    # rg only applies .gitignore inside a git repo — mirror the real
    # use case (project dirs are repos) with a bare .git marker.
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "top.py").write_text("needle at top\n")
    (tmp_path / "src" / "a.py").write_text("has needle here\nplain line\n")
    (tmp_path / "src" / "deep" / "b.py").write_text("NEEDLE upper\n")
    (tmp_path / "src" / "notes.txt").write_text("needle in txt\n")
    (tmp_path / "node_modules" / "junk.py").write_text("needle ignored\n")
    (tmp_path / ".gitignore").write_text("generated.py\n")
    (tmp_path / "generated.py").write_text("needle generated\n")
    return tmp_path


@pytest.fixture
def ctx(tree):
    return ToolContext(work_dir=str(tree.resolve()))


def _search(ctx, **kw):
    out = ts.tool_search_content(ctx, "needle", **kw)
    return out.splitlines() if out != "(no matches)" else []


def _common_search_assertions(lines):
    joined = "\n".join(lines)
    assert any(l.startswith("top.py:1:") for l in lines)          # root file
    assert "src/a.py:1: has needle here" in joined
    assert any("b.py:1:" in l for l in lines)                      # case-insensitive default
    assert "node_modules" not in joined                            # built-in ignores
    assert "needle in txt" in joined                               # no filter → all files


def test_python_fallback_search(ctx, monkeypatch):
    monkeypatch.setattr(ts, "_RG_PATH", None)
    lines = _search(ctx)
    _common_search_assertions(lines)
    # pure python does NOT read .gitignore
    assert any("generated.py" in l for l in lines)


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_search(ctx):
    lines = _search(ctx)
    _common_search_assertions(lines)
    # rg respects .gitignore — a strict improvement
    assert not any("generated.py" in l for l in lines)


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_and_python_agree_on_filter_and_case(ctx, monkeypatch):
    rg_lines = set(_search(ctx, file_filter="*.py", case_sensitive=True))
    monkeypatch.setattr(ts, "_RG_PATH", None)
    py_lines = set(_search(ctx, file_filter="*.py", case_sensitive=True))
    # identical except the gitignored file only python sees
    py_lines = {l for l in py_lines if "generated.py" not in l}
    assert rg_lines == py_lines
    assert not any("NEEDLE" in l for l in rg_lines)  # case-sensitive honored


def test_unsupported_regex_falls_back(ctx):
    # Rust regex rejects lookbehind (rg exit 2) → python path serves it.
    out = ts.tool_search_content(ctx, r"(?<=has )needle")
    assert "src/a.py:1:" in out
    assert "top.py" not in out  # lookbehind actually applied


def test_invalid_regex_reports(ctx):
    assert ts.tool_search_content(ctx, "[unclosed").startswith(
        "Error: invalid regex"
    )


def _glob(ctx, pattern):
    out = ts.tool_glob_files(ctx, pattern)
    if out.startswith("("):
        return []
    return out.splitlines()[1:]


def _common_glob_assertions(lines):
    assert "top.py" in lines                      # ** matches root level
    assert "src/deep/b.py" in lines
    assert not any(l.startswith("node_modules") for l in lines)
    assert "src/notes.txt" not in lines


def test_python_fallback_glob(ctx, monkeypatch):
    monkeypatch.setattr(ts, "_RG_PATH", None)
    lines = _glob(ctx, "**/*.py")
    _common_glob_assertions(lines)
    assert "generated.py" in lines                # no .gitignore awareness


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_glob(ctx):
    lines = _glob(ctx, "**/*.py")
    _common_glob_assertions(lines)
    assert "generated.py" not in lines            # .gitignore-aware


@pytest.mark.skipif(not HAS_RG, reason="ripgrep not installed")
def test_rg_glob_single_component_star(ctx):
    lines = _glob(ctx, "src/*.py")
    assert "src/a.py" in lines
    assert "src/deep/b.py" not in lines           # * must not cross /
