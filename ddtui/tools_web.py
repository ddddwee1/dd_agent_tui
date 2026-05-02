"""HTTP fetch and Brave Search tool implementations."""

from __future__ import annotations

import gzip
import html
import json
import re
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import (
    WEB_FETCH_HARD_CHAR_CAP,
    WEB_FETCH_MAX_BYTES,
    WEB_FETCH_MAX_CHARS,
    WEB_FETCH_TIMEOUT,
    WEB_SEARCH_API_KEY_PATH,
    WEB_SEARCH_DEFAULT_COUNT,
    WEB_SEARCH_ENDPOINT,
    WEB_SEARCH_MAX_COUNT,
    WEB_SEARCH_SNIPPET_MAX_CHARS,
    WEB_SEARCH_TIMEOUT,
)
from .state import ToolContext


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML to readable plain text.

    Skips script/style/noscript/svg/head subtrees entirely. Inserts a newline
    around block-level tags so the resulting text retains paragraph structure.
    Whitespace is collapsed by the caller.
    """

    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head"})
    BLOCK_TAGS = frozenset({
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "nav", "aside",
        "blockquote", "pre", "table", "hr", "ul", "ol",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse intra-line runs of whitespace, drop trailing spaces, then
        # squeeze 3+ blank lines down to one.
        cleaned: list[str] = []
        blank_run = 0
        for line in raw.splitlines():
            line = re.sub(r"[ \t]+", " ", line).strip()
            if not line:
                blank_run += 1
                if blank_run <= 1:
                    cleaned.append("")
            else:
                blank_run = 0
                cleaned.append(line)
        return "\n".join(cleaned).strip()


_BINARY_CTYPE_PREFIXES = (
    "image/", "audio/", "video/", "font/",
    "application/pdf", "application/zip", "application/x-tar",
    "application/x-gzip", "application/x-bzip", "application/x-7z",
    "application/octet-stream",
)


def tool_web_fetch(
    ctx: ToolContext, url: str, max_chars: int | None = None
) -> str:
    """Fetch an http(s) URL and return its body as text.

    HTML is stripped to plain text; other text-like content types are
    returned verbatim. Binary types are refused. Size is capped both at
    the byte level (raw download) and the char level (returned string).
    """
    cap = WEB_FETCH_MAX_CHARS
    if max_chars is not None:
        try:
            cap = max(1, min(WEB_FETCH_HARD_CHAR_CAP, int(max_chars)))
        except (TypeError, ValueError):
            return f"Error: max_chars must be an integer, got {max_chars!r}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Error: only http(s) URLs are allowed (got: {url!r})"

    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ddtui-agent/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urlopen(req, timeout=WEB_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").strip()
            encoding = (resp.headers.get("Content-Encoding") or "").lower().strip()
            final_url = resp.geturl()
            status = resp.status
            # Read at most MAX_BYTES + 1 so we can detect truncation. Keep
            # the +1 separate from MAX_BYTES because some servers ignore
            # range hints; this just bounds memory.
            raw = resp.read(WEB_FETCH_MAX_BYTES + 1)
    except HTTPError as e:
        return f"HTTP error {e.code} {e.reason} for {url}"
    except URLError as e:
        return f"URL error: {e.reason} for {url}"
    except TimeoutError:
        return f"Error: request timed out after {WEB_FETCH_TIMEOUT}s"
    except Exception as e:
        return f"Error fetching {url}: {e}"

    truncated_bytes = len(raw) > WEB_FETCH_MAX_BYTES
    if truncated_bytes:
        raw = raw[:WEB_FETCH_MAX_BYTES]

    ctype_lc = ctype.lower()
    if any(ctype_lc.startswith(p) for p in _BINARY_CTYPE_PREFIXES):
        return (
            f"Refused: content-type {ctype!r} is binary; web_fetch returns "
            f"text only ({final_url})"
        )

    # Decode Content-Encoding (only matters when server actually compressed).
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                # Some servers send raw deflate (no zlib header).
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:
        return f"Error: failed to decompress {encoding!r} response: {e}"

    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    looks_html = (
        "html" in ctype_lc
        or body.lstrip()[:14].lower().startswith(("<!doctype html", "<html"))
    )
    if looks_html:
        parser = _HTMLTextExtractor()
        try:
            parser.feed(body)
            parser.close()
            text = parser.text() or body
        except Exception:
            text = body
    else:
        text = body

    notes: list[str] = []
    if len(text) > cap:
        text = text[:cap]
        notes.append(f"truncated at {cap} chars")
    if truncated_bytes:
        notes.append(f"raw response exceeded {WEB_FETCH_MAX_BYTES // 1000} KB")
    suffix = f"\n\n…[{'; '.join(notes)}]" if notes else ""

    header = f"GET {final_url} → {status} ({ctype or 'unknown content-type'})"
    return f"{header}\n\n{text}{suffix}"


_BRAVE_API_KEY: str | None = None


def _load_brave_api_key() -> str:
    """Read the Brave Search API key from disk on first call; cache.
    Returns empty string if the file is missing or empty so the caller
    can surface a friendly error."""
    global _BRAVE_API_KEY
    if _BRAVE_API_KEY is not None:
        return _BRAVE_API_KEY
    try:
        _BRAVE_API_KEY = Path(WEB_SEARCH_API_KEY_PATH).read_text().strip()
    except (OSError, FileNotFoundError):
        _BRAVE_API_KEY = ""
    return _BRAVE_API_KEY


def tool_web_search(
    ctx: ToolContext, query: str, count: int | None = None
) -> str:
    """Run a Brave Search and return a ranked list of (title, url,
    snippet) blocks. Errors are surfaced as plain text so the model can
    react (rate limit, bad key, no results)."""
    if not query or not query.strip():
        return "Error: web_search requires a non-empty 'query'."
    api_key = _load_brave_api_key()
    if not api_key:
        return (
            f"Error: Brave Search API key not found at "
            f"{WEB_SEARCH_API_KEY_PATH}. Put the key (one line) there, "
            f"or set $BRAVE_API_KEY_FILE to a different path."
        )

    n = WEB_SEARCH_DEFAULT_COUNT
    if count is not None:
        try:
            n = max(1, min(WEB_SEARCH_MAX_COUNT, int(count)))
        except (TypeError, ValueError):
            return f"Error: count must be an integer, got {count!r}"

    url = f"{WEB_SEARCH_ENDPOINT}?{urlencode({'q': query, 'count': n})}"
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
            "User-Agent": "ddtui-agent/1.0",
        },
    )
    try:
        with urlopen(req, timeout=WEB_SEARCH_TIMEOUT) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower().strip()
            raw = resp.read()
    except HTTPError as e:
        # Brave returns 401/422/429 with a JSON body — surface a slice
        # so the model knows whether it's a key, quota, or query issue.
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"HTTP error {e.code} {e.reason} from Brave Search: {body[:400]}"
    except URLError as e:
        return f"URL error: {e.reason}"
    except TimeoutError:
        return f"Error: Brave Search timed out after {WEB_SEARCH_TIMEOUT}s"
    except Exception as e:
        return f"Error calling Brave Search: {e}"

    if encoding == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception as e:
            return f"Error: failed to decompress gzip response: {e}"

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return f"Error: Brave Search returned non-JSON: {e}"

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"(no web results for {query!r})"

    lines: list[str] = [f"web_search {query!r} → {len(results)} result(s)"]
    for i, r in enumerate(results, 1):
        title = html.unescape((r.get("title") or "").strip()) or "(untitled)"
        link = (r.get("url") or "").strip()
        snippet = (r.get("description") or "").strip()
        # Brave wraps matched terms in <strong>…</strong>; strip HTML
        # then unescape entities so &quot; / &amp; come out as " / &.
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet))
        if len(snippet) > WEB_SEARCH_SNIPPET_MAX_CHARS:
            snippet = snippet[:WEB_SEARCH_SNIPPET_MAX_CHARS] + "…"
        block = [f"[{i}] {title}"]
        if link:
            block.append(f"    {link}")
        if snippet:
            block.append(f"    {snippet}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)
