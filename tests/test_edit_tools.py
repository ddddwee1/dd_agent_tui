"""Edit tool set: apply_patch stub, replace_all, read-before-overwrite."""

import os

import pytest

from ddtui.state import ToolContext
from ddtui.tool_schemas import TOOLS
from ddtui.tools import TOOL_FUNCS, execute_tool
from ddtui.tools_files import (
    tool_apply_patch,
    tool_edit_file,
    tool_edit_lines,
    tool_multi_edit,
    tool_read_file,
    tool_write_file,
)


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(work_dir=str(tmp_path.resolve()))


# ── schema consistency ──

def test_apply_patch_gone_from_schema_but_dispatchable():
    names = [t["function"]["name"] for t in TOOLS]
    assert "apply_patch" not in names
    assert "apply_patch" in TOOL_FUNCS


def test_edit_file_has_replace_all_and_write_file_lost_force():
    ef = next(t for t in TOOLS if t["function"]["name"] == "edit_file")
    assert "replace_all" in ef["function"]["parameters"]["properties"]
    wf = next(t for t in TOOLS if t["function"]["name"] == "write_file")
    assert "force" not in wf["function"]["parameters"]["properties"]


def test_apply_patch_stub_redirects(ctx):
    r = tool_apply_patch(ctx, path="x.py", old_string="a", new_string="b")
    assert r.startswith("Error: apply_patch has been removed")
    r = execute_tool(ctx, "apply_patch", {"path": "x", "patch": "@@", "junk": 1})
    assert r.startswith("Error: apply_patch has been removed")


# ── write_file read-before-overwrite ──

def test_new_file_writes_directly(ctx):
    assert tool_write_file(ctx, "new.txt", "hello\n").startswith("Wrote")
    # own write refreshes the ledger → immediate overwrite is fine
    assert tool_write_file(ctx, "new.txt", "hello2\n").startswith("Wrote")


def test_unread_existing_file_refused(ctx, tmp_path):
    blind = tmp_path / "existing.txt"
    blind.write_text("precious\n")
    r = tool_write_file(ctx, "existing.txt", "clobber\n")
    assert "you have not read it" in r
    assert blind.read_text() == "precious\n"
    tool_read_file(ctx, "existing.txt")
    assert tool_write_file(ctx, "existing.txt", "ok\n").startswith("Wrote")


def test_external_change_detected(ctx, tmp_path):
    ext = tmp_path / "drift.txt"
    ext.write_text("v1\n")
    tool_read_file(ctx, "drift.txt")
    ext.write_text("v2-external\n")
    os.utime(ext, (ext.stat().st_atime, ext.stat().st_mtime + 5))
    r = tool_write_file(ctx, "drift.txt", "clobber\n")
    assert "changed on disk" in r
    assert ext.read_text() == "v2-external\n"
    tool_read_file(ctx, "drift.txt")
    assert tool_write_file(ctx, "drift.txt", "ok\n").startswith("Wrote")


def test_edits_refresh_ledger(ctx, tmp_path):
    f = tmp_path / "flow.txt"
    f.write_text("aaa\nbbb\n")
    tool_read_file(ctx, "flow.txt")
    tool_edit_file(ctx, "flow.txt", "aaa", "AAA")
    assert tool_write_file(ctx, "flow.txt", "rewritten\n").startswith("Wrote")

    f2 = tmp_path / "l2.txt"
    f2.write_text("1\n2\n3\n")
    tool_read_file(ctx, "l2.txt")
    tool_edit_lines(ctx, "l2.txt", "delete", 2, 2)
    assert tool_write_file(ctx, "l2.txt", "x\n").startswith("Wrote")

    f3 = tmp_path / "l3.txt"
    f3.write_text("aa bb\n")
    tool_read_file(ctx, "l3.txt")
    tool_multi_edit(ctx, "l3.txt", [{"old_string": "aa", "new_string": "cc"}])
    assert tool_write_file(ctx, "l3.txt", "y\n").startswith("Wrote")


# ── edit_file replace_all ──

def test_replace_all(ctx, tmp_path):
    m = tmp_path / "multi.txt"
    m.write_text("foo bar foo baz foo\n")
    r = tool_edit_file(ctx, "multi.txt", "foo", "qux", replace_all=True)
    assert m.read_text() == "qux bar qux baz qux\n"
    assert "replaced all 3 occurrence(s)" in r
    assert "@@" in r  # diff attached


def test_replace_all_occurrence_mutually_exclusive(ctx, tmp_path):
    m = tmp_path / "multi.txt"
    m.write_text("foo bar foo\n")
    r = tool_edit_file(ctx, "multi.txt", "foo", "qux", occurrence=1, replace_all=True)
    assert "mutually exclusive" in r
    assert m.read_text() == "foo bar foo\n"


def test_multimatch_without_flags_lists_context(ctx, tmp_path):
    m = tmp_path / "multi.txt"
    m.write_text("foo bar foo\n")
    r = tool_edit_file(ctx, "multi.txt", "foo", "qux")
    assert "matches 2 times" in r
    r = tool_edit_file(ctx, "multi.txt", "foo", "qux", occurrence=2)
    assert m.read_text() == "foo bar qux\n"
