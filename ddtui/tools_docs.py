"""Section-aware Markdown navigation with compact route receipts.

``read_file`` remains the raw escape hatch. These tools add only structure
already present in Markdown: headings, explicit links, resolved targets, and
which link was followed. They do not perform semantic retrieval or choose a
technical route for the model.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .state import ToolContext
from .tool_utils import _safe_path
from .tools_files import _note_file_known


DOC_SECTION_MAX_CHARS = 32_000
DOC_MAX_LINE_CHARS = 2_000
DOC_MAX_LINKS = 40
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_INLINE_LINK_RE = re.compile(
    r"(?<!!)\[([^\]]+)\]\(\s*<?([^\s)>]+)>?(?:\s+[^)]*)?\)"
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_heading(text: str) -> str:
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _slug(text: str) -> str:
    """Return a GitHub-style-enough heading slug for local doc links."""
    text = unicodedata.normalize("NFKC", _clean_heading(text)).casefold()
    text = re.sub(r"[^\w\-\s]", "", text, flags=re.UNICODE)
    return re.sub(r"[\s\-]+", "-", text).strip("-")


def _headings(lines: list[str]) -> list[dict]:
    seen: dict[str, int] = {}
    out: list[dict] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        text = _clean_heading(match.group(2))
        base = _slug(text)
        duplicate = seen.get(base, 0)
        seen[base] = duplicate + 1
        anchor = base if duplicate == 0 else f"{base}-{duplicate}"
        out.append(
            {
                "line": index + 1,
                "index": index,
                "level": len(match.group(1)),
                "text": text,
                "anchor": anchor,
            }
        )
    return out


def _select_heading(
    headings: list[dict], heading: str | None, anchor: str | None
) -> tuple[dict | None, str | None]:
    if anchor:
        wanted = unquote(anchor).lstrip("#").casefold()
        matches = [
            item for item in headings if item["anchor"].casefold() == wanted
        ]
        if not matches:
            return None, f"Error: Markdown anchor #{wanted} was not found."
        return matches[0], None
    if not heading:
        return None, None
    wanted = _clean_heading(heading).casefold()
    exact = [item for item in headings if item["text"].casefold() == wanted]
    if len(exact) == 1:
        return exact[0], None
    partial = [item for item in headings if wanted in item["text"].casefold()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        choices = ", ".join(
            f"{item['text']} (#{item['anchor']})" for item in partial[:8]
        )
        return None, f"Error: heading is ambiguous; matches: {choices}"
    return None, f"Error: Markdown heading {heading!r} was not found."


def _section_bounds(
    lines: list[str], headings: list[dict], selected: dict
) -> tuple[int, int]:
    start = selected["index"]
    end = len(lines)
    for item in headings:
        if item["index"] > start and item["level"] <= selected["level"]:
            end = item["index"]
            break
    return start, end


def _resolve_link(source: Path, label: str, target: str, link_id: str) -> dict:
    parsed = urlsplit(target)
    external = bool(parsed.scheme or parsed.netloc)
    fragment = unquote(parsed.fragment or "")
    resolved_path: Path | None = None
    file_exists: bool | None = None
    anchor_exists: bool | None = None
    if not external:
        raw_path = unquote(parsed.path)
        resolved_path = (
            source if not raw_path else (source.parent / raw_path).resolve()
        )
        file_exists = resolved_path.is_file()
        if file_exists and fragment:
            try:
                target_lines = resolved_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                anchors = {
                    item["anchor"].casefold() for item in _headings(target_lines)
                }
                anchor_exists = fragment.casefold() in anchors
            except (OSError, UnicodeDecodeError):
                anchor_exists = False
    return {
        "id": link_id,
        "label": _clean_heading(label)[:200],
        "target": target[:1000],
        "external": external,
        "resolved_path": str(resolved_path) if resolved_path is not None else None,
        "fragment": fragment or None,
        "file_exists": file_exists,
        "anchor_exists": anchor_exists,
        "followed_receipt_id": None,
        "covered_receipt_id": None,
    }


def _extract_links(source: Path, lines: list[str]) -> tuple[list[dict], bool]:
    links: list[dict] = []
    for line in lines:
        for match in _INLINE_LINK_RE.finditer(line):
            if len(links) >= DOC_MAX_LINKS:
                return links, True
            links.append(
                _resolve_link(
                    source,
                    match.group(1),
                    match.group(2),
                    f"link-{len(links) + 1}",
                )
            )
    return links, False


def _numbered(
    line_pairs: list[tuple[int, str]], max_chars: int
) -> tuple[str, bool, int]:
    parts: list[str] = []
    used = 0
    complete = True
    shown_through = 0
    for line_number, line in line_pairs:
        if len(line) > DOC_MAX_LINE_CHARS:
            line = (
                line[:DOC_MAX_LINE_CHARS]
                + f" …[line clipped; {len(line)} chars total]"
            )
        rendered = f"{line_number:6d}\t{line}"
        if parts and used + len(rendered) + 1 > max_chars:
            complete = False
            break
        parts.append(rendered)
        used += len(rendered) + 1
        shown_through = line_number
    return "\n".join(parts), complete, shown_through


def _format_link(link: dict) -> str:
    if link["external"]:
        state = "external"
    elif not link["file_exists"]:
        state = "missing-file"
    elif link["fragment"] and not link["anchor_exists"]:
        state = "missing-anchor"
    else:
        state = "resolved"
    followed = (
        f" -> {link['followed_receipt_id']}"
        if link["followed_receipt_id"]
        else (
            f" -> covered by {link['covered_receipt_id']}"
            if link.get("covered_receipt_id")
            else ""
        )
    )
    return f"- {link['id']} [{state}]{followed}: {link['label']} -> {link['target']}"


def _render_receipt(receipt: dict, body: str) -> str:
    lines = [
        f"Document receipt {receipt['id']}",
        f"path: {receipt['path']}",
        f"selection: {receipt['heading'] or 'outline'}",
        f"selection_lines: {receipt['start_line']}-{receipt['end_line']}",
        f"shown_through_line: {receipt['shown_through_line']}",
        f"content_complete: {str(receipt['content_complete']).lower()}",
        f"links_in_selection: {len(receipt['links'])}",
        f"links_truncated: {str(receipt['links_truncated']).lower()}",
    ]
    if receipt.get("parent_receipt_id"):
        lines.append(
            f"followed_from: {receipt['parent_receipt_id']}/{receipt['parent_link_id']}"
        )
    lines.extend(_format_link(link) for link in receipt["links"])
    lines.append("--- content ---")
    lines.append(body or "(no headings found)")
    if not receipt["content_complete"]:
        lines.append(
            f"…[section capped at {DOC_SECTION_MAX_CHARS} chars; use read_file "
            f"with offset={receipt['shown_through_line'] + 1} for raw continuation]"
        )
    return "\n".join(lines)


def _create_receipt(
    ctx: ToolContext,
    path: str,
    *,
    heading: str | None = None,
    anchor: str | None = None,
    parent_receipt_id: str | None = None,
    parent_link_id: str | None = None,
) -> str:
    p = _safe_path(ctx.work_dir, path)
    if p.suffix.lower() not in {".md", ".markdown", ".mdown"}:
        return f"Error: read_doc only accepts Markdown files: {p}"
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text"
    except Exception as exc:
        return f"Error reading {p}: {exc}"

    all_lines = text.splitlines()
    headings = _headings(all_lines)
    selected, error = _select_heading(headings, heading, anchor)
    if error:
        choices = "\n".join(
            f"- {item['text']} (#{item['anchor']})" for item in headings[:30]
        )
        return error + (f"\nAvailable headings:\n{choices}" if choices else "")

    if selected is None:
        line_pairs = [
            (
                item["line"],
                f"{'#' * item['level']} {item['text']}  <!-- #{item['anchor']} -->",
            )
            for item in headings
        ]
        start_line = 1
        end_line = len(all_lines)
        selection_heading = None
        selection_anchor = None
        link_source_lines = all_lines
    else:
        start, end = _section_bounds(all_lines, headings, selected)
        line_pairs = [
            (index + 1, all_lines[index]) for index in range(start, end)
        ]
        start_line = start + 1
        end_line = end
        selection_heading = selected["text"]
        selection_anchor = selected["anchor"]
        link_source_lines = all_lines[start:end]

    body, complete, shown_through = _numbered(line_pairs, DOC_SECTION_MAX_CHARS)
    receipt_id = ctx.alloc_doc_receipt_id()
    links, links_truncated = _extract_links(p, link_source_lines)
    receipt = {
        "id": receipt_id,
        "path": str(p),
        "heading": selection_heading,
        "anchor": selection_anchor,
        "start_line": start_line,
        "end_line": end_line,
        "shown_through_line": shown_through,
        "content_complete": complete,
        "links": links,
        "links_truncated": links_truncated,
        "parent_receipt_id": parent_receipt_id,
        "parent_link_id": parent_link_id,
        "created_at": _now(),
    }
    ctx.doc_receipts[receipt_id] = receipt
    for prior in ctx.doc_receipts.values():
        for link in prior["links"]:
            if (
                link["resolved_path"] == str(p)
                and (link["fragment"] or None) == (selection_anchor or None)
            ):
                link["covered_receipt_id"] = receipt_id
    _note_file_known(ctx, p)
    return _render_receipt(receipt, body)


def tool_read_doc(
    ctx: ToolContext,
    path: str,
    heading: str | None = None,
    anchor: str | None = None,
) -> str:
    """Read one Markdown section, or an outline when no section is given."""
    if heading and anchor:
        return "Error: pass heading or anchor, not both."
    return _create_receipt(ctx, path, heading=heading, anchor=anchor)


def tool_follow_doc_link(
    ctx: ToolContext,
    receipt_id: str,
    link_id: str | None = None,
    label: str | None = None,
) -> str:
    """Resolve and read one explicit link from a prior document receipt."""
    receipt = ctx.doc_receipts.get(receipt_id)
    if receipt is None:
        return f"Error: unknown receipt_id {receipt_id!r}. Use doc_route_status."
    if not link_id and not label:
        return "Error: pass link_id or label."
    wanted_label = _clean_heading(label or "").casefold()
    matches = [
        link
        for link in receipt["links"]
        if (link_id and link["id"] == link_id)
        or (wanted_label and wanted_label in link["label"].casefold())
    ]
    if len(matches) != 1:
        available = "\n".join(_format_link(link) for link in receipt["links"])
        if not matches:
            return f"Error: link was not found in {receipt_id}.\n{available}"
        return f"Error: link label is ambiguous in {receipt_id}.\n{available}"
    link = matches[0]
    existing = link["followed_receipt_id"] or link.get("covered_receipt_id")
    if existing and existing in ctx.doc_receipts:
        link["followed_receipt_id"] = existing
        return (
            f"Link already covered by {existing}; no duplicate content returned.\n"
            f"{_format_link(link)}\nUse doc_route_status(receipt_id={existing!r})."
        )
    if link["external"]:
        return "Error: external links are not read by follow_doc_link; use web_fetch."
    if not link["file_exists"]:
        return f"Error: linked file does not exist: {link['resolved_path']}"
    if link["fragment"] and not link["anchor_exists"]:
        return (
            f"Error: linked anchor #{link['fragment']} does not exist in "
            f"{link['resolved_path']}"
        )
    result = _create_receipt(
        ctx,
        link["resolved_path"],
        anchor=link["fragment"],
        parent_receipt_id=receipt_id,
        parent_link_id=link["id"],
    )
    if result.startswith("Error:"):
        return result
    child_id = f"doc-{ctx.doc_receipt_next_id - 1}"
    link["followed_receipt_id"] = child_id
    return result


def tool_doc_route_status(ctx: ToolContext, receipt_id: str | None = None) -> str:
    """Show compact, persistent document-navigation receipts."""
    if receipt_id:
        receipts = [ctx.doc_receipts.get(receipt_id)]
        if receipts[0] is None:
            return f"Error: unknown receipt_id {receipt_id!r}."
    else:
        receipts = list(ctx.doc_receipts.values())
    if not receipts:
        return "No document receipts. Use read_doc on a Markdown route entry."
    lines = [f"Document route status: {len(receipts)} receipt(s)"]
    for receipt in receipts:
        covered = sum(
            bool(link["followed_receipt_id"] or link.get("covered_receipt_id"))
            for link in receipt["links"]
        )
        unresolved = sum(
            not link["external"]
            and (not link["file_exists"] or link["anchor_exists"] is False)
            for link in receipt["links"]
        )
        lines.append(
            f"- {receipt['id']}: {receipt['path']} "
            f"#{receipt['anchor'] or 'outline'} lines "
            f"{receipt['start_line']}-{receipt['end_line']}; "
            f"links {covered}/{len(receipt['links'])} covered, "
            f"{unresolved} unresolved, "
            f"truncated={receipt.get('links_truncated', False)}"
        )
        for link in receipt["links"]:
            lines.append(f"  {_format_link(link)}")
    return "\n".join(lines)
