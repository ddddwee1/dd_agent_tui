"""Markdown section navigation and persistent route receipts."""

import json

from ddtui.state import ToolContext
from ddtui.tools import PARALLEL_SAFE_TOOLS
from ddtui.tools_docs import (
    restore_document_sections,
    sync_document_visibility,
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


def test_archived_receipt_allows_follow_to_return_body_again(tmp_path):
    ctx = _fixture(tmp_path)
    parent = tool_read_doc(ctx, "router.md", heading="Optimize")
    tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    sync_document_visibility(ctx, [{"role": "tool", "content": parent}])
    assert "context=archived" in tool_doc_route_status(ctx, "doc-2")
    recovered = tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    assert "Use the matrix." in recovered
    assert "Document receipt doc-3" in recovered


def test_changed_document_is_reread_even_with_visible_receipt(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "router.md", heading="Optimize")
    tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    path = tmp_path / "leaf.md"
    path.write_text(path.read_text().replace("Use the matrix.", "Use the current contract."))
    recovered = tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    assert "Use the current contract." in recovered


def test_recovery_checks_version_budget_outline_and_visibility(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "router.md")
    body = tool_read_doc(ctx, "leaf.md", heading="Merge Gate")
    documents, updates, deferred = restore_document_sections(ctx, [])
    assert len(documents) == 1 and "Use the matrix." in documents[0]["content"]
    assert updates["doc-1"]["context_status"] == "archived"
    assert not deferred
    assert not restore_document_sections(ctx, [{"role": "tool", "content": body}])[0]
    limited, _, deferred = restore_document_sections(ctx, [], max_chars=10)
    assert not limited and deferred[0]["reason"] == "character_budget"
    (tmp_path / "leaf.md").write_text("# Changed\n")
    changed, _, deferred = restore_document_sections(ctx, [])
    assert not changed and "document changed" in deferred[0]["reason"]


def test_legacy_receipt_does_not_suppress_reread_or_claim_version(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "router.md", heading="Optimize")
    tool_follow_doc_link(ctx, "doc-1", link_id="link-1")
    del ctx.doc_receipts["doc-2"]["source_sha256"]
    documents, _, deferred = restore_document_sections(ctx, [])
    assert all(m["document_receipt_id"] != "doc-2" for m in documents)
    assert any("version unknown" in item["reason"] for item in deferred)
    assert "Use the matrix." in tool_follow_doc_link(ctx, "doc-1", link_id="link-1")


def test_checkpoint_refs_take_priority_and_use_latest_selection(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "leaf.md", heading="Merge Gate")
    tool_read_doc(ctx, "leaf.md", heading="Merge Gate")
    for i in range(8):
        (tmp_path / f"recent-{i}.md").write_text(f"# Recent {i}\n\n## Detail\nFact {i}.\n")
        tool_read_doc(ctx, f"recent-{i}.md", heading="Detail")
    ctx.checkpoint = {"active_refs": ["doc-1（current contract）: required for the next step", "task-unknown"]}
    documents, _, deferred = restore_document_sections(ctx, [])
    ids = [m["document_receipt_id"] for m in documents]
    assert len(ids) == 6 and "doc-2" in ids and "doc-1" not in ids
    assert any(d["reason"] == "section_budget" for d in deferred)


def test_similar_reference_names_do_not_pin_a_document(tmp_path):
    ctx = _fixture(tmp_path)
    tool_read_doc(ctx, "leaf.md", heading="Merge Gate")
    for i in range(6):
        (tmp_path / f"recent-{i}.md").write_text(f"# Recent {i}\n\n## Detail\nFact {i}.\n")
        tool_read_doc(ctx, f"recent-{i}.md", heading="Detail")
    ctx.checkpoint = {"active_refs": ["doc-1-other", "doc-1suffix"]}
    documents, _, _ = restore_document_sections(ctx, [])
    assert all(m["document_receipt_id"] != "doc-1" for m in documents)
