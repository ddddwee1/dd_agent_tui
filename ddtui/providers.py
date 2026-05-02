"""LLM provider adapters.

The app keeps one chat-completions-shaped message history. Providers
translate that history to the wire format they need and stream back a
small common event shape for the UI/tool loop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from .codex_auth import CodexAuth
from .config import (
    CODEX_BASE_URL,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    DEEPSEEK_API_KEY_PATH,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_REASONING_EFFORT,
)


@dataclass
class CompletionTokenDetails:
    reasoning_tokens: int = 0


@dataclass
class ProviderUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    completion_tokens_details: CompletionTokenDetails = field(
        default_factory=CompletionTokenDetails
    )


@dataclass
class ToolCallDelta:
    index: int
    id: str | None = None
    type: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass
class LLMStreamEvent:
    content: str = ""
    reasoning: str = ""
    tool_call: ToolCallDelta | None = None
    usage: Any | None = None


class ProviderConfigError(RuntimeError):
    """Raised when a provider is selected but its local credentials or
    configuration are missing/invalid.

    This intentionally inherits from RuntimeError (not SystemExit) so
    the TUI can surface the problem inline — for example when `/provider
    deepseek` is used without a DeepSeek API key — instead of the whole
    process exiting from inside provider construction.
    """


class LLMProvider:
    key: str
    label: str
    default_model: str
    default_effort: str

    async def complete_text(
        self, messages: list[dict], model: str, effort: str
    ) -> str:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
    ) -> AsyncIterator[LLMStreamEvent]:
        raise NotImplementedError
        yield LLMStreamEvent()


class DeepSeekProvider(LLMProvider):
    key = "deepseek"
    label = "DeepSeek"
    default_model = DEEPSEEK_MODEL
    default_effort = DEEPSEEK_REASONING_EFFORT

    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            api_key=load_deepseek_api_key(),
            base_url=DEEPSEEK_BASE_URL,
        )

    async def complete_text(
        self, messages: list[dict], model: str, effort: str
    ) -> str:
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            stream=False,
        )
        return (response.choices[0].message.content or "").strip()

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
    ) -> AsyncIterator[LLMStreamEvent]:
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
            reasoning_effort=effort,
            extra_body={"thinking": {"type": "enabled"}},
        )
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                yield LLMStreamEvent(usage=chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield LLMStreamEvent(reasoning=reasoning)
            content = getattr(delta, "content", None)
            if content:
                yield LLMStreamEvent(content=content)
            for tool_call in getattr(delta, "tool_calls", None) or []:
                function = getattr(tool_call, "function", None)
                yield LLMStreamEvent(
                    tool_call=ToolCallDelta(
                        index=tool_call.index
                        if tool_call.index is not None
                        else 0,
                        id=tool_call.id,
                        type=tool_call.type,
                        name=getattr(function, "name", None)
                        if function
                        else None,
                        arguments=getattr(function, "arguments", None)
                        if function
                        else None,
                    )
                )


class CodexResponsesProvider(LLMProvider):
    key = "codex"
    label = "Codex OAuth"
    default_model = CODEX_MODEL
    default_effort = CODEX_REASONING_EFFORT

    def __init__(self) -> None:
        self.auth = CodexAuth()
        self.base_url = CODEX_BASE_URL

    async def complete_text(
        self, messages: list[dict], model: str, effort: str
    ) -> str:
        parts: list[str] = []
        async for event in self.stream(messages, [], model, effort):
            if event.content:
                parts.append(event.content)
        return "".join(parts).strip()

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
    ) -> AsyncIterator[LLMStreamEvent]:
        payload = self._build_payload(messages, tools, model, effort)
        async for event in self._stream_payload(payload, allow_refresh=True):
            yield event

    def _build_payload(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
    ) -> dict[str, Any]:
        instructions, input_items = self._messages_to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions or "You are a helpful assistant.",
            "input": input_items,
            "stream": True,
            "store": False,
        }
        response_tools = self._tools_to_responses(tools)
        if response_tools:
            payload["tools"] = response_tools
            payload["tool_choice"] = "auto"
        if effort:
            payload["reasoning"] = {
                "effort": self._normalize_reasoning_effort(effort)
            }
        return payload

    async def _stream_payload(
        self, payload: dict[str, Any], allow_refresh: bool
    ) -> AsyncIterator[LLMStreamEvent]:
        headers = {
            **await self.auth.headers(),
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(60.0, read=None)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code == 401 and allow_refresh:
                    await response.aclose()
                    await self.auth.refresh()
                    async for event in self._stream_payload(
                        payload, allow_refresh=False
                    ):
                        yield event
                    return
                if response.status_code >= 400:
                    body = await response.aread()
                    raise RuntimeError(
                        "Codex Responses request failed: "
                        f"{response.status_code} "
                        f"{body.decode('utf-8', 'replace')[:1000]}"
                    )
                async for event in self._events_from_sse(response):
                    yield event

    async def _events_from_sse(
        self, response: httpx.Response
    ) -> AsyncIterator[LLMStreamEvent]:
        data_lines: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        yielded_calls: set[str] = set()

        async for raw_line in response.aiter_lines():
            line = raw_line.strip()
            if not line:
                if data_lines:
                    event = self._load_sse_data(data_lines)
                    data_lines = []
                    if event is not None:
                        async for item in self._event_to_stream_events(
                            event, calls, yielded_calls
                        ):
                            yield item
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line.startswith("{"):
                event = self._load_json_line(line)
                if event is not None:
                    async for item in self._event_to_stream_events(
                        event, calls, yielded_calls
                    ):
                        yield item

        if data_lines:
            event = self._load_sse_data(data_lines)
            if event is not None:
                async for item in self._event_to_stream_events(
                    event, calls, yielded_calls
                ):
                    yield item

    async def _event_to_stream_events(
        self,
        event: dict[str, Any],
        calls: dict[str, dict[str, Any]],
        yielded_calls: set[str],
    ) -> AsyncIterator[LLMStreamEvent]:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield LLMStreamEvent(content=delta)
            return
        if event_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                yield LLMStreamEvent(reasoning=delta)
            return
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                item_id = item.get("id") or item.get("call_id")
                if item_id:
                    calls[str(item_id)] = {
                        "index": event.get("output_index") or 0,
                        "id": item.get("call_id") or item_id,
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    }
            return
        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            if item_id and item_id in calls:
                calls[item_id]["arguments"] += event.get("delta") or ""
            return
        if event_type == "response.function_call_arguments.done":
            item_id = event.get("item_id")
            if item_id and item_id in calls:
                calls[item_id]["arguments"] = event.get("arguments") or ""
            return
        if event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                item_id = str(item.get("id") or item.get("call_id") or "")
                if item_id and item_id in yielded_calls:
                    return
                if item_id:
                    yielded_calls.add(item_id)
                call_id = item.get("call_id") or item_id
                yield LLMStreamEvent(
                    tool_call=ToolCallDelta(
                        index=event.get("output_index") or 0,
                        id=str(call_id),
                        type="function",
                        name=item.get("name") or "",
                        arguments=item.get("arguments") or "",
                    )
                )
            return
        if event_type == "response.completed":
            usage = (event.get("response") or {}).get("usage") or {}
            yield LLMStreamEvent(usage=self._usage_from_response(usage))

    def _messages_to_responses_input(
        self, messages: list[dict]
    ) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content") or ""
            if role == "system":
                if content:
                    instructions.append(str(content))
                continue
            if role == "user":
                input_items.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": str(content)}
                        ],
                    }
                )
                continue
            if role == "assistant":
                if content:
                    input_items.append(
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": str(content),
                                }
                            ],
                        }
                    )
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    call_id = tool_call.get("id") or f"call_{len(input_items)}"
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": function.get("name") or "",
                            "arguments": function.get("arguments") or "{}",
                        }
                    )
                continue
            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id") or "",
                        "output": str(content),
                    }
                )
        return "\n\n".join(instructions), input_items

    def _tools_to_responses(self, tools: list[dict]) -> list[dict[str, Any]]:
        response_tools: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function = tool.get("function") or {}
            response_tools.append(
                {
                    "type": "function",
                    "name": function.get("name") or "",
                    "description": function.get("description") or "",
                    "parameters": function.get("parameters") or {
                        "type": "object",
                        "properties": {},
                    },
                    "strict": False,
                }
            )
        return response_tools

    def _usage_from_response(self, usage: dict[str, Any]) -> ProviderUsage:
        details = usage.get("output_tokens_details") or {}
        return ProviderUsage(
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            completion_tokens_details=CompletionTokenDetails(
                reasoning_tokens=int(details.get("reasoning_tokens") or 0)
            ),
        )

    def _load_sse_data(self, data_lines: list[str]) -> dict[str, Any] | None:
        return self._load_json_line("\n".join(data_lines))

    def _load_json_line(self, line: str) -> dict[str, Any] | None:
        if line == "[DONE]":
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _normalize_reasoning_effort(self, effort: str) -> str:
        normalized = effort.strip().lower()
        if normalized == "max":
            return "xhigh"
        if normalized == "minimal":
            return "low"
        return normalized


def load_deepseek_api_key() -> str:
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        key = DEEPSEEK_API_KEY_PATH.read_text().strip()
    except Exception as exc:
        raise ProviderConfigError(
            "No DEEPSEEK_API_KEY env var, and cannot read API key from "
            f"{DEEPSEEK_API_KEY_PATH}: {exc}"
        ) from exc
    if not key:
        raise ProviderConfigError(
            f"API key file is empty: {DEEPSEEK_API_KEY_PATH}"
        )
    return key


def provider_names() -> tuple[str, ...]:
    return ("deepseek", "codex")


def build_provider(name: str) -> LLMProvider:
    normalized = name.strip().lower()
    if normalized == "deepseek":
        return DeepSeekProvider()
    if normalized == "codex":
        return CodexResponsesProvider()
    raise ProviderConfigError(
        f"Unknown provider {name!r}. Available: {', '.join(provider_names())}"
    )
