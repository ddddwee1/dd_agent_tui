"""read_file: cat -n line numbers, pagination, line/total clipping."""

import re

import pytest

from ddtui.state import ToolContext
from ddtui.tools_files import tool_read_file


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
