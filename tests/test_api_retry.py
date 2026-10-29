"""Model request recovery, retry limits, permanent errors and cancellation."""

import asyncio
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError
import pytest

import ddtui.api_retry as retry


@pytest.mark.parametrize("status, expected", [
    (400, False), (401, False), (403, False), (404, False), (408, True),
    (409, True), (429, True), (500, True), (503, True), (599, True),
])
@pytest.mark.parametrize("source", ["sdk", "httpx", "codex"])
def test_retryable_http_statuses(status, expected, source):
    response = httpx.Response(status, request=httpx.Request("POST", "https://model.test"))
    if source == "sdk":
        error = APIStatusError("failed", response=response, body=None)
    elif source == "httpx":
        error = httpx.HTTPStatusError("failed", request=response.request, response=response)
    else:
        error = retry.ProviderHTTPError(status, "failed")
    assert retry.is_retryable(error) is expected


@pytest.mark.parametrize("error", [
    ConnectionError("lost"), TimeoutError("stalled"), httpx.ConnectError("offline"),
    httpx.ReadTimeout("stalled"), httpx.RemoteProtocolError("peer disconnected"),
    APIConnectionError(request=httpx.Request("POST", "https://model.test")),
    retry.IncompleteStreamError("missing completion marker"),
])
def test_transient_failure_reconnects(error, instant_api_retries):
    async def run():
        calls = 0
        notices = []

        async def request():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            return "recovered"

        async def on_retry(notice):
            notices.append(notice)

        assert await retry.retry_request(request, on_retry=on_retry) == "recovered"
        assert calls == 2 and instant_api_retries == [1]
        assert len(notices) == 1 and notices[0].error is error
        assert notices[0].attempt == 1 and notices[0].max_retries == 3
        assert "自动重连（1/3）" in notices[0].message

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("bad response"), ValueError("bad input")])
def test_permanent_failure_is_not_retried(error, instant_api_retries):
    async def request():
        raise error

    with pytest.raises(type(error)) as caught:
        asyncio.run(retry.retry_request(request))
    assert caught.value is error
    assert instant_api_retries == []


@pytest.mark.parametrize("max_retries", [0, 3, 8])
def test_retry_exhaustion_and_capped_backoff(monkeypatch, instant_api_retries, max_retries):
    monkeypatch.setattr(retry, "API_MAX_RETRIES", max_retries)
    calls = 0
    error = httpx.ReadError("lost")

    async def request():
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(httpx.ReadError) as caught:
        asyncio.run(retry.retry_request(request))
    assert caught.value is error
    assert calls == max_retries + 1
    assert instant_api_retries == [1, 2, 4, 8, 16, 30, 30, 30][:max_retries]


def test_cancel_during_backoff_stops_reconnecting(monkeypatch):
    async def run():
        sleeping = asyncio.Event()
        calls = 0

        async def request():
            nonlocal calls
            calls += 1
            raise ConnectionError("offline")

        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(retry, "asyncio", SimpleNamespace(sleep=sleep))
        task = asyncio.create_task(retry.retry_request(request))
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1

    asyncio.run(run())
