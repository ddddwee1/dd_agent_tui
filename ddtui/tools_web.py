"""DeepSeek Search and Fetch tool implementations."""

from __future__ import annotations

import gzip
import html
import io
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import (
    WEB_FETCH_ENDPOINT,
    WEB_FETCH_HARD_CHAR_CAP,
    WEB_FETCH_MAX_BYTES,
    WEB_FETCH_MAX_CHARS,
    WEB_FETCH_TIMEOUT,
    WEB_FETCH_URL_MAX_CHARS,
    WEB_SEARCH_DEFAULT_COUNT,
    WEB_SEARCH_ENDPOINT,
    WEB_SEARCH_MAX_COUNT,
    WEB_SEARCH_QUERY_MAX_CHARS,
    WEB_SEARCH_SNIPPET_MAX_CHARS,
    WEB_SEARCH_TIMEOUT,
)
from .providers import ProviderConfigError, load_deepseek_api_key
from .state import ToolContext
from .tool_output import limit_tool_output


def _deepseek_web_request(
    endpoint: str,
    payload: dict,
    *,
    operation: str,
    timeout: int,
    max_bytes: int | None = None,
) -> dict | str:
    """Post JSON to a DeepSeek web tool, returning an object or an error string."""
    label = f"DeepSeek {operation}"
    try:
        api_key = load_deepseek_api_key()
    except ProviderConfigError as e:
        return (
            f"Error: {label} API key unavailable: {e}. "
            "Set DEEPSEEK_API_KEY or DEEPSEEK_API_KEY_FILE "
            "(default: ~/deepseek_apikey.txt)."
        )

    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ddtui-agent/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower().strip()
            raw = resp.read(max_bytes + 1) if max_bytes is not None else resp.read()
    except HTTPError as e:
        # Include the API's message for authentication, balance and rate limits.
        try:
            body = e.read(400).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return f"HTTP error {e.code} {e.reason} from {label}: {body}"
    except URLError as e:
        return f"URL error from {label}: {e.reason}"
    except TimeoutError:
        return f"Error: {label} timed out after {timeout}s"
    except Exception as e:
        return f"Error calling {label}: {e}"

    if max_bytes is not None and len(raw) > max_bytes:
        return f"Error: {label} response exceeded {max_bytes} bytes."
    if encoding == "gzip":
        try:
            if max_bytes is None:
                raw = gzip.decompress(raw)
            else:
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as decoded:
                    raw = decoded.read(max_bytes + 1)
        except Exception as e:
            return f"Error: failed to decompress {label} gzip response: {e}"
    if max_bytes is not None and len(raw) > max_bytes:
        return f"Error: {label} response exceeded {max_bytes} bytes."

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return f"Error: {label} returned non-JSON: {e}"
    if not isinstance(data, dict):
        return f"Error: {label} returned non-object JSON."
    return data


def tool_web_fetch(
    ctx: ToolContext,
    url: str,
    max_output_chars: int | None = None,
    max_chars: int | None = None,
) -> str:
    """Fetch an http(s) page via DeepSeek and return its title and Markdown.

    The preview cap is `max_output_chars` (minimum 512), matching bash.
    Full fetched text is archived before applying this cap.
    `max_chars` is accepted as a backward-compatible alias.
    """
    # Canonical param is max_output_chars (consistent with bash/task_*);
    # max_chars kept as an alias so older callers keep working.
    cap_arg = max_output_chars if max_output_chars is not None else max_chars
    cap = WEB_FETCH_MAX_CHARS
    if cap_arg is not None:
        try:
            cap = max(512, min(WEB_FETCH_HARD_CHAR_CAP, int(cap_arg)))
        except (TypeError, ValueError):
            return f"Error: max_output_chars must be an integer, got {cap_arg!r}"

    if not isinstance(url, str) or not url.strip():
        return "Error: web_fetch requires a non-empty 'url'."
    if len(url) > WEB_FETCH_URL_MAX_CHARS:
        return f"Error: url must be at most {WEB_FETCH_URL_MAX_CHARS} Unicode characters."
    try:
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            return "Error: web_fetch URLs must not contain a username or password."
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "Error: web_fetch requires an http(s) URL with a host."
        parsed.port  # Validate the port when one is present.
    except ValueError:
        return "Error: web_fetch requires a valid http(s) URL."

    data = _deepseek_web_request(
        WEB_FETCH_ENDPOINT, {"url": url}, operation="Fetch",
        timeout=WEB_FETCH_TIMEOUT, max_bytes=WEB_FETCH_MAX_BYTES,
    )
    if isinstance(data, str):
        return data
    title, content = data.get("title"), data.get("content")
    if not isinstance(title, str) or not isinstance(content, str):
        return "Error: DeepSeek Fetch returned an invalid page response (expected title and content strings)."
    if not content.strip():
        return "Error: DeepSeek Fetch returned empty page content."

    header = f"web_fetch {url}"
    if title.strip():
        header += f"\nTitle: {title.strip()}"
    return limit_tool_output(ctx, f"{header}\n\n{content}", name="web_fetch",
                             arguments={"url": url}, max_chars=cap)


def tool_web_search(
    ctx: ToolContext, query: str, count: int | None = None
) -> str:
    """Search via DeepSeek and return ranked title, URL and summary blocks.

    Credentials follow the DeepSeek provider's environment/file precedence.
    API and configuration errors are returned as text for the model.
    """
    if not isinstance(query, str) or not query.strip():
        return "Error: web_search requires a non-empty 'query'."
    if len(query) > WEB_SEARCH_QUERY_MAX_CHARS:
        return (
            f"Error: query must be at most {WEB_SEARCH_QUERY_MAX_CHARS} "
            "Unicode characters. Shorten the search query."
        )

    n = WEB_SEARCH_DEFAULT_COUNT
    if count is not None:
        try:
            n = max(1, min(WEB_SEARCH_MAX_COUNT, int(count)))
        except (TypeError, ValueError):
            return f"Error: count must be an integer, got {count!r}"

    data = _deepseek_web_request(
        WEB_SEARCH_ENDPOINT, {"query": query, "count": n, "need_content": False},
        operation="Search", timeout=WEB_SEARCH_TIMEOUT,
    )
    if isinstance(data, str):
        return data

    results = data.get("results")
    if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
        return "Error: DeepSeek Search returned an invalid 'results' array."
    results = results[:n]
    if not results:
        return f"(no web results for {query!r})"

    lines: list[str] = [f"web_search {query!r} → {len(results)} result(s)"]
    for i, r in enumerate(results, 1):
        title = html.unescape(str(r.get("title") or "").strip()) or "(untitled)"
        link = str(r.get("url") or "").strip()
        snippet = str(r.get("summary") or r.get("snippet") or "").strip()
        # Normalize markup and entities in search-result summaries.
        snippet = html.unescape(re.sub(r"<[^>]+>", "", snippet))
        if len(snippet) > WEB_SEARCH_SNIPPET_MAX_CHARS:
            snippet = snippet[:WEB_SEARCH_SNIPPET_MAX_CHARS] + "…"
        block = [f"[{i}] {title}"]
        if link:
            block.append(f"    {link}")
        if r.get("date"):
            block.append(f"    Published: {r['date']}")
        if snippet:
            block.append(f"    {snippet}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)
