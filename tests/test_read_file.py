"""read_file(s): numbering, pagination, partial failures, and output caps."""

import re

import pytest

from ddtui.config import READ_FILES_MAX_FILES, READ_FILES_MAX_TOTAL_CHARS
from ddtui.state import ToolContext
from ddtui.tools_files import tool_read_file, tool_read_files, tool_write_file


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(work_dir=str(tmp_path.resolve()))


def test_numbered_output_and_header(ctx, tmp_path):
    f = (tmp_path / "small.txt").resolve()
    f.write_text("alpha\nbeta\ngamma\n")
    out = tool_read_file(ctx, "small.txt")
    lines = out.splitlines()
    assert lines[0] == f"[{f}] lines 1-3 of 3"
    assert lines[1] == "     1\talpha"
    assert lines[3] == "     3\tgamma"


def test_pagination_keeps_absolute_line_numbers(ctx, tmp_path):
    (tmp_path / "small.txt").write_text("alpha\nbeta\ngamma\n")
    out = tool_read_file(ctx, "small.txt", offset=2, limit=1)
    lines = out.splitlines()
    assert "lines 2-2 of 3" in lines[0]
    assert lines[1] == "     2\tbeta"


def test_long_line_clipped(ctx, tmp_path):
    (tmp_path / "minified.js").write_text("x" * 50_000 + "\nshort\n")
    out = tool_read_file(ctx, "minified.js")
    first = out.splitlines()[1]
    assert len(first) < 3000
    assert "[line clipped; 50000 chars total]" in first
    assert "     2\tshort" in out


def test_total_cap_with_resume_hint(ctx, tmp_path):
    (tmp_path / "big.txt").write_text(
        "\n".join(f"line {i} " + "y" * 100 for i in range(1, 1001))
    )
    out = tool_read_file(ctx, "big.txt")
    assert len(out) < 70_000
    m = re.search(r"continue with offset=(\d+)", out)
    assert m, "resume hint missing"
    out2 = tool_read_file(ctx, "big.txt", offset=int(m.group(1)))
    assert out2.splitlines()[1].startswith(f"{int(m.group(1)):6d}\t")


def test_read_files_preserves_order_ranges_and_partial_errors(ctx, tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a1\na2\na3\n")
    b.write_text("b1\nb2\nb3\n")

    out = tool_read_files(ctx, [
        {"path": "a.txt", "limit": 2},
        {"path": "missing.txt"},
        {"path": "b.txt", "offset": 2, "limit": 1},
    ])

    assert out.startswith("[read_files] 2/3 files read successfully")
    a_header = f"[{a.resolve()}] lines 1-2 of 3"
    missing = f"Error: file not found: {(tmp_path / 'missing.txt').resolve()}"
    b_header = f"[{b.resolve()}] lines 2-2 of 3"
    assert out.index(a_header) < out.index(missing) < out.index(b_header)
    assert "     2\tb2" in out

    # Every successful item satisfies the same read-before-overwrite guard
    # as a standalone read_file call; a failed item does not create a receipt.
    assert str(a.resolve()) in ctx.read_files
    assert str(b.resolve()) in ctx.read_files
    assert str((tmp_path / "missing.txt").resolve()) not in ctx.read_files
    assert tool_write_file(ctx, "a.txt", "new a\n").startswith("Wrote")
    assert tool_write_file(ctx, "b.txt", "new b\n").startswith("Wrote")


def test_read_files_shares_one_batch_cap_and_keeps_resume_hints(ctx, tmp_path):
    paths = []
    for name in ("large-a.txt", "large-b.txt", "large-c.txt"):
        path = tmp_path / name
        path.write_text("\n".join(f"{name} line {i} " + "x" * 80 for i in range(800)))
        paths.append(path)

    out = tool_read_files(ctx, [{"path": path.name} for path in paths])

    assert len(out) <= READ_FILES_MAX_TOTAL_CHARS
    assert out.count("read_files batch cap") == len(paths)
    for path in paths:
        assert f"[{path.resolve()}]" in out
        assert f"read_file(path='{path.name}', offset=" in out
        assert str(path.resolve()) in ctx.read_files


def test_read_files_validates_batch_shape(ctx):
    assert "must be an array" in tool_read_files(ctx, "x")
    assert "at least one" in tool_read_files(ctx, [])
    too_many = [{"path": f"{i}.txt"} for i in range(READ_FILES_MAX_FILES + 1)]
    assert f"at most {READ_FILES_MAX_FILES}" in tool_read_files(ctx, too_many)
    all_bad = tool_read_files(ctx, [{}, "not-an-object"])
    assert all_bad.startswith("Error: read_files could not read any")
    assert "requires a non-empty path" in all_bad
    assert "must be an object" in all_bad
