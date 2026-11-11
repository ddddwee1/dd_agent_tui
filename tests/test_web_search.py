"""DeepSeek Search request contract, credentials and failure handling."""

import gzip
import io
import json
from urllib.error import HTTPError, URLError

import pytest

import ddtui.providers as providers
import ddtui.tools_web as web
from ddtui.state import ToolContext
from ddtui.tools import execute_tool


class Response:
    def __init__(self, data, *, compressed=False):
        self.body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = {}
        if compressed:
            self.body = gzip.compress(self.body)
            self.headers["Content-Encoding"] = "gzip"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body


@pytest.fixture
def search(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setattr(providers, "DEEPSEEK_API_KEY_PATH", tmp_path / "api-key")
    monkeypatch.setattr(web, "WEB_SEARCH_ENDPOINT", "https://api.deepseek.test/v1/tools/search")
    calls = []

    def respond(data, **kwargs):
        def open_url(request, *, timeout):
            calls.append((request, timeout))
            if isinstance(data, Exception):
                raise data
            return Response(data, **kwargs)

        monkeypatch.setattr(web, "urlopen", open_url)

    respond({"results": []})
    return ToolContext(work_dir=str(tmp_path)), calls, respond


def test_search_posts_unicode_query_and_formats_ranked_results(search):
    ctx, calls, respond = search
    query = '深度求索 "搜索" site:api-docs.deepseek.com'
    summary = "详细摘要" * 120
    respond({"results": [
        {"title": "Search &amp; Fetch", "url": "https://example.test/docs",
         "snippet": "short snippet", "summary": f"<strong>{summary}</strong> &amp; API",
         "date": "2026-11-01T00:00:00Z"},
        {"title": "Other page", "url": "https://example.test/other", "summary": "Other summary"},
    ]})

    result = execute_tool(ctx, "web_search", {"query": query})

    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://api.deepseek.test/v1/tools/search"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-deepseek-key"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"query": query, "count": 5, "need_content": False}
    assert timeout == 60
    assert "2 result(s)" in result
    assert "[1] Search & Fetch" in result and "[2] Other page" in result
    assert "https://example.test/docs" in result
    assert summary + " & API" in result and "short snippet" not in result
    assert "Published: 2026-11-01T00:00:00Z" in result
    assert "<strong>" not in result


def test_gzip_response_uses_snippet_when_summary_is_empty(search):
    ctx, _, respond = search
    respond({"results": [{"title": "Example", "url": "https://example.test",
                          "summary": "", "snippet": "Fallback snippet", "date": None}]},
            compressed=True)
    result = web.tool_web_search(ctx, "example")
    assert "Fallback snippet" in result
    assert "Published:" not in result


def test_empty_search_is_not_an_error(search):
    ctx, _, _ = search
    assert web.tool_web_search(ctx, "example") == "(no web results for 'example')"


@pytest.mark.parametrize("count, expected", [(1, 1), (100, 50), (0, 1), ("3", 3)])
def test_result_count_stays_within_api_limits(search, count, expected):
    ctx, calls, _ = search
    web.tool_web_search(ctx, "example", count=count)
    assert json.loads(calls[0][0].data)["count"] == expected


@pytest.mark.parametrize("query", ["", " \n ", None, "查" * 129])
def test_invalid_queries_do_not_send_requests(search, query):
    ctx, calls, _ = search
    assert web.tool_web_search(ctx, query).startswith("Error:")
    assert not calls


def test_query_limit_counts_unicode_characters(search):
    ctx, calls, _ = search
    query = "查" * 128
    web.tool_web_search(ctx, query)
    assert json.loads(calls[0][0].data)["query"] == query


def test_invalid_count_does_not_send_request(search):
    ctx, calls, _ = search
    assert "count must be an integer" in web.tool_web_search(ctx, "example", count="many")
    assert not calls


@pytest.mark.parametrize("env_key, expected", [(" env-key ", "env-key"), (None, "file-key")])
def test_search_reuses_provider_credential_precedence(search, monkeypatch, env_key, expected):
    ctx, calls, _ = search
    providers.DEEPSEEK_API_KEY_PATH.write_text("file-key\n")
    if env_key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY")
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", env_key)
    web.tool_web_search(ctx, "example")
    assert calls[0][0].get_header("Authorization") == f"Bearer {expected}"


@pytest.mark.parametrize("empty_file", [False, True])
def test_missing_credentials_are_actionable_and_can_be_fixed(search, monkeypatch, empty_file):
    ctx, calls, _ = search
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    if empty_file:
        providers.DEEPSEEK_API_KEY_PATH.write_text(" \n")
    result = web.tool_web_search(ctx, "example")
    assert result.startswith("Error: DeepSeek Search API key unavailable:")
    assert "DEEPSEEK_API_KEY_FILE" in result
    assert not calls
    providers.DEEPSEEK_API_KEY_PATH.write_text("new-key")
    assert "no web results" in web.tool_web_search(ctx, "example")
    assert calls[0][0].get_header("Authorization") == "Bearer new-key"


@pytest.mark.parametrize("status, message", [
    (401, "Invalid API key"), (402, "Insufficient balance"),
    (429, "Rate limit exceeded"), (503, "Service unavailable"),
])
def test_http_errors_preserve_status_and_api_message(search, status, message):
    ctx, _, respond = search
    body = io.BytesIO(json.dumps({"error": {"message": message}}).encode())
    respond(HTTPError(web.WEB_SEARCH_ENDPOINT, status, "API error", {}, body))
    result = web.tool_web_search(ctx, "example")
    assert f"HTTP error {status}" in result
    assert "DeepSeek Search" in result and message in result


@pytest.mark.parametrize("error, expected", [
    (URLError("connection refused"), "connection refused"),
    (TimeoutError(), "DeepSeek Search timed out"),
])
def test_network_failures_return_text(search, error, expected):
    ctx, _, respond = search
    respond(error)
    assert expected in web.tool_web_search(ctx, "example")


@pytest.mark.parametrize("data", [b"<html>unavailable</html>", [], {}, {"results": "bad"}, {"results": [None]}])
def test_malformed_api_responses_are_errors(search, data):
    ctx, _, respond = search
    respond(data)
    assert web.tool_web_search(ctx, "example").startswith("Error: DeepSeek Search returned")
