"""Retry transient model failures at a single request boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx
from openai import APIConnectionError, APIStatusError

from .config import API_MAX_RETRIES


class TransientStreamError(RuntimeError):
    """The provider reports a temporary stream failure."""


class IncompleteStreamError(TransientStreamError):
    """The model connection ended before its completion marker arrived."""


class ProviderHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIStatusError, httpx.HTTPStatusError, ProviderHTTPError)):
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
        return status in (408, 409, 429) or 500 <= status < 600
    return isinstance(exc, (
        APIConnectionError, httpx.TimeoutException, httpx.NetworkError,
        httpx.RemoteProtocolError, ConnectionError, TimeoutError,
        TransientStreamError,
    ))


@dataclass(frozen=True)
class RequestRetry:
    attempt: int
    max_retries: int
    delay: float
    error: Exception

    @property
    def message(self) -> str:
        detail = f"{type(self.error).__name__}: {self.error}"[:300]
        return (
            f"模型请求暂时失败，{self.delay:g} 秒后自动重连"
            f"（{self.attempt}/{self.max_retries}）：{detail}"
        )


_T = TypeVar("_T")


async def retry_request(
    request: Callable[[], Awaitable[_T]],
    *,
    on_retry: Callable[[RequestRetry], Awaitable[None]] | None = None,
) -> _T:
    """Reissue one model request; cancellation and permanent errors propagate."""
    attempt = 0
    while True:
        try:
            return await request()
        except Exception as exc:
            if attempt >= API_MAX_RETRIES or not is_retryable(exc):
                raise
            delay = min(30.0, 2.0 ** min(attempt, 5))
            attempt += 1
            if on_retry is not None:
                await on_retry(RequestRetry(attempt, API_MAX_RETRIES, delay, exc))
            await asyncio.sleep(delay)
