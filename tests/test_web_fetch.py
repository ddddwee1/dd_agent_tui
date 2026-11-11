"""DeepSeek Fetch requests, Markdown preservation and response limits."""

import gzip
import io
import json
from urllib.error import HTTPError

import pytest

import ddtui.providers as providers
import ddtui.tools_web as web
from ddtui.state import ToolContext
from ddtui.tools import execute_tool


@pytest.fixture
def fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(providers, "DEEPSEEK_API_KEY_PATH", tmp_path / "api-key")
    monkeypatch.setattr(web, "WEB_FETCH_ENDPOINT", "https://api.deepseek.test/v1/tools/fetch")
    calls = []

    def respond(data, *, compressed=False):
        def open_url(request, *, timeout):
            calls.append((request, timeout))
            if isinstance(data, Exception):
                raise data
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            response = io.BytesIO(gzip.compress(raw) if compressed else raw)
            response.headers = {"Content-Encoding": "gzip"} if compressed else {}
            return response

        monkeypatch.setattr(web, "urlopen", open_url)

    respond({"title": "Example", "content": "# Example\n\nPage content."})
    return ToolContext(work_dir=str(tmp_path)), calls, respond


def test_fetch_posts_url_and_preserves_markdown(fetch):
    ctx, calls, respond = fetch
    url = "https://example.test/文档?lang=zh&section=api"
    content = '# 文档\n\n[链接](https://example.test)\n\n```html\n<b>代码 &amp; 原文</b>\n```\n'
    respond({"title": "文档 & API", "content": content})

    result = execute_tool(ctx, "web_fetch", {"url": url})

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://api.deepseek.test/v1/tools/fetch"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-deepseek-key"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"url": url}
    assert timeout == 60
    assert url in result and "Title: 文档 & API" in result
    assert result.endswith(content)


def test_gzip_page_without_title_is_supported(fetch):
    ctx, _, respond = fetch
    respond({"title": "", "content": "# 中文\n\n完整正文。"}, compressed=True)
    result = web.tool_web_fetch(ctx, "https://example.test")
    assert result.endswith("# 中文\n\n完整正文。")
    assert "Title:" not in result


@pytest.mark.parametrize("url", [
    None, "", " \n ", "file:///etc/hosts", "https:///path",
    "https://[invalid", "https://example.test:invalid", "https://example.test:99999",
    "https://user:password@example.test", "https://@example.test",
])
def test_invalid_urls_are_rejected_before_request(fetch, url):
    ctx, calls, _ = fetch
    result = web.tool_web_fetch(ctx, url)
    assert result.startswith("Error:")
    assert "user:password" not in result
    assert not calls


def test_url_length_limit_is_checked_locally(fetch):
    ctx, calls, _ = fetch
    prefix = "https://example.test/"
    url = prefix + "查" * (8192 - len(prefix))
    assert not web.tool_web_fetch(ctx, url).startswith("Error:")
    assert json.loads(calls[0][0].data)["url"] == url
    assert "at most 8192" in web.tool_web_fetch(ctx, url + "查")
    assert len(calls) == 1


def test_fetch_reports_missing_deepseek_credentials(fetch, monkeypatch):
    ctx, calls, _ = fetch
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    result = web.tool_web_fetch(ctx, "https://example.test")
    assert result.startswith("Error: DeepSeek Fetch API key unavailable:")
    assert "DEEPSEEK_API_KEY_FILE" in result
    assert not calls


def test_max_chars_alias_limits_preview(fetch):
    ctx, _, respond = fetch
    respond({"title": "Example", "content": "中文正文\n" * 1000})
    result = web.tool_web_fetch(ctx, "https://example.test", max_chars=700)
    assert len(result) <= 700
    assert "history_read(" in result


def test_invalid_preview_limit_does_not_send_request(fetch):
    ctx, calls, _ = fetch
    result = web.tool_web_fetch(ctx, "https://example.test", max_output_chars="invalid")
    assert "max_output_chars must be an integer" in result
    assert not calls


@pytest.mark.parametrize("status, message", [
    (403, "URL rejected by security policy"),
    (404, "No usable page content"),
    (429, "Rate limit exceeded"),
])
def test_fetch_surfaces_api_errors(fetch, status, message):
    ctx, _, respond = fetch
    body = io.BytesIO(json.dumps({"error": {"message": message}}).encode())
    respond(HTTPError(web.WEB_FETCH_ENDPOINT, status, "API error", {}, body))
    result = web.tool_web_fetch(ctx, "https://example.test")
    assert f"HTTP error {status}" in result
    assert "DeepSeek Fetch" in result and message in result


@pytest.mark.parametrize("data", [
    b"<html>unavailable</html>", [], {}, {"title": "Example"},
    {"title": None, "content": "text"}, {"title": "Example", "content": None},
    {"title": "Example", "content": ""}, {"title": "Example", "content": " \n "},
])
def test_malformed_or_empty_page_is_an_error(fetch, data):
    ctx, _, respond = fetch
    respond(data)
    assert web.tool_web_fetch(ctx, "https://example.test").startswith("Error: DeepSeek Fetch returned")


@pytest.mark.parametrize("compressed", [False, True])
def test_response_size_cap_applies_before_and_after_gzip(fetch, monkeypatch, compressed):
    ctx, _, respond = fetch
    monkeypatch.setattr(web, "WEB_FETCH_MAX_BYTES", 500)
    respond({"title": "Example", "content": "x" * 1000}, compressed=compressed)
    result = web.tool_web_fetch(ctx, "https://example.test")
    assert result == "Error: DeepSeek Fetch response exceeded 500 bytes."
