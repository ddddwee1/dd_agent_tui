"""Markdown section navigation and persistent route receipts."""

import json

from ddtui.state import ToolContext
from ddtui.tools import PARALLEL_SAFE_TOOLS
from ddtui.tools_docs import (
    tool_doc_route_status,
    tool_follow_doc_link,
    tool_read_doc,
)


def _fixture(tmp_path):
    (tmp_path / "leaf.md").write_text(
        "# Leaf\n\n"
        "## Merge Gate\n\n"
        "Use the matrix.\n\n"
        "### Details\n\n"
        "Details stay in this section.\n\n"
        "## Other\n\n"
        "Not selected.\n"
    )
    (tmp_path / "router.md").write_text(
        "# Router\n\n"
        "## Build\n\n"
        "Use the build route.\n\n"
        "## Optimize\n\n"
        "Read the [merge gate](leaf.md#merge-gate).\n"
        "Broken [owner](missing.md#owner).\n"
        "External [site](https://example.com/doc).\n"
    )
    return ToolContext(work_dir=str(tmp_path))


def test_outline_uses_real_source_lines_and_does_not_auto_follow(tmp_path):
    ctx = _fixture(tmp_path)
    out = tool_read_doc(ctx, "router.md")
    assert "Document receipt doc-1" in out
    assert "     1\t# Router" in out
    assert "     7\t## Optimize" in out
    assert "links_in_selection: 3" in out
    assert ctx.doc_receipts["doc-1"]["links"][0]["followed_receipt_id"] is None
    assert str((tmp_path / "router.md").resolve()) in ctx.read_files
    json.dumps(ctx.doc_receipts)  # history payload must stay serializable


def test_section_follow_records_exact_link_edge(tmp_path):
    ctx = _fixture(tmp_path)
    section = tool_read_doc(ctx, "router.md", heading="Optimize")
    assert "selection: Optimize" in section
    assert "     7\t## Optimize" in section
    assert "## Build" not in section

    followed = tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    assert "Document receipt doc-2" in followed
    assert "selection: Merge Gate" in followed
    assert "Use the matrix." in followed
    assert "Not selected." not in followed
    assert "followed_from: doc-1/link-1" in followed
    assert ctx.doc_receipts["doc-1"]["links"][0]["followed_receipt_id"] == "doc-2"
    duplicate = tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    assert "already covered by doc-2" in duplicate
    assert "Use the matrix." not in duplicate

    status = tool_doc_route_status(ctx)
    assert "links 1/3 covered" in status
    assert "missing-file" in status


def test_broken_external_and_ambiguous_routes_fail_cleanly(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "router.md", heading="Optimize")
    assert "linked file does not exist" in tool_follow_doc_link(
        ctx, "doc-1", link_id="link-2"
    )
    assert "external links are not read" in tool_follow_doc_link(
        ctx, "doc-1", link_id="link-3"
    )
    assert "was not found" in tool_read_doc(ctx, "router.md", heading="Nope")


def test_doc_navigation_is_intentionally_serial():
    assert "read_doc" not in PARALLEL_SAFE_TOOLS
    assert "follow_doc_link" not in PARALLEL_SAFE_TOOLS
    assert "doc_route_status" not in PARALLEL_SAFE_TOOLS


def test_doc_output_caps_long_lines_and_reports_link_truncation(tmp_path):
    links = " ".join(f"[L{i}](leaf.md#merge-gate)" for i in range(45))
    (tmp_path / "leaf.md").write_text("# Leaf\n## Merge Gate\nok\n")
    (tmp_path / "many.md").write_text(
        "# Many\n\n## Section\n" + "x" * 5000 + "\n" + links + "\n"
    )
    ctx = ToolContext(work_dir=str(tmp_path))
    out = tool_read_doc(ctx, "many.md", heading="Section")
    assert "[line clipped; 5000 chars total]" in out
    assert "links_in_selection: 40" in out
    assert "links_truncated: true" in out
