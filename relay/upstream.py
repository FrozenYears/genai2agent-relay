from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlsplit

import requests

from .actions import strict_json_loads
from .config import RelayConfig
from .engine import RelayEvent, TextCompletionRequest, UpstreamReply


class UpstreamError(RuntimeError):
    pass


def api_base(base: str) -> str:
    base = base.rstrip("/")
    return base if urlsplit(base).path.rstrip("/") else base + "/v1"


class UpstreamClient:
    def __init__(self, config: RelayConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.config.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.config.upstream_api_key}"
        return headers

    @staticmethod
    def _payload(request: TextCompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **request.options, **request.sampling,
            "model": request.model, "messages": [message.as_dict() for message in request.messages],
            "stream": True, "stream_options": {"include_usage": True}, "max_tokens": request.max_tokens,
        }
        if request.tool_mode == "native":
            definitions = []
            for tool in request.tools:
                schema = tool.parameters if tool.kind == "function" else {
                    "type": "object", "properties": {"input": {"type": "string"}},
                    "required": ["input"], "additionalProperties": False,
                }
                function = {"name": tool.name, "description": tool.description, "parameters": schema}
                if tool.strict is not None:
                    function["strict"] = tool.strict
                definitions.append({"type": "function", "function": function})
            payload["tools"] = definitions
            payload["tool_choice"] = request.tool_choice if request.tool_choice in {"auto", "none", "required"} else {
                "type": "function", "function": {"name": request.tool_choice},
            }
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        return payload

    def stream(self, request: TextCompletionRequest) -> Iterator[RelayEvent]:
        response = None
        try:
            response = self.session.post(
                api_base(self.config.upstream_base_url) + "/chat/completions",
                headers=self._headers(), json=self._payload(request), stream=True,
                timeout=(self.config.upstream_connect_timeout, self.config.upstream_read_timeout),
            )
            if response.status_code != 200:
                raise UpstreamError(f"First-hop service returned HTTP {response.status_code}")
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            usage: dict[str, Any] = {}
            finish = None
            done = False
            payload_bytes = 0

            def consume(value: str) -> None:
                nonlocal payload_bytes
                payload_bytes += len(value.encode("utf-8"))
                if payload_bytes > self.config.max_action_bytes:
                    raise UpstreamError("First-hop payload exceeds MAX_ACTION_BYTES")

            for data in self._sse(response):
                if data is None:
                    yield RelayEvent("ping")
                    continue
                if data == "[DONE]":
                    done = True
                    break
                try:
                    chunk = strict_json_loads(data)
                    if not isinstance(chunk, dict):
                        raise ValueError("SSE data is not an object")
                    if chunk.get("error"):
                        raise UpstreamError("First-hop service returned a stream error")
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise ValueError("Expected one completion choice")
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("index", 0) != 0:
                        raise ValueError("Unexpected completion choice index")
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise ValueError("Invalid completion delta")
                    if finish is not None and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                        raise ValueError("Content arrived after the terminal finish reason")
                    for key, target, kind in (("content", content, "text"), ("reasoning_content", reasoning, "reasoning")):
                        value = delta.get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                raise ValueError("Completion content must be text")
                            if value:
                                consume(value)
                                target.append(value)
                                yield RelayEvent(kind, value)
                    fragments = delta.get("tool_calls") or []
                    if not isinstance(fragments, list):
                        raise ValueError("Invalid tool call deltas")
                    for fragment in fragments:
                        index = fragment.get("index")
                        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                            raise ValueError("Tool call delta must have a nonnegative index")
                        if index not in calls and not (
                            fragment.get("id") or any((fragment.get("function") or {}).get(key) for key in ("name", "arguments"))
                        ):
                            raise ValueError("A new tool call delta must contain payload")
                        call = calls.setdefault(index, {"type": "function", "function": {"name": "", "arguments": ""}})
                        if fragment.get("type", "function") != "function":
                            raise ValueError("Unsupported first-hop tool call type")
                        if "id" in fragment:
                            if not isinstance(fragment["id"], str) or not fragment["id"]:
                                raise ValueError("Invalid tool call ID")
                            if "id" in call and call["id"] != fragment["id"]:
                                raise ValueError("Tool call ID changed during streaming")
                            if "id" not in call:
                                consume(fragment["id"])
                            call["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        if not isinstance(function, dict):
                            raise ValueError("Invalid tool function delta")
                        for key in ("name", "arguments"):
                            if key in function:
                                if not isinstance(function[key], str):
                                    raise ValueError("Invalid function delta fragment")
                                consume(function[key])
                                call["function"][key] += function[key]
                    if choice.get("finish_reason") is not None:
                        if finish is not None:
                            raise ValueError("Duplicate terminal finish reason")
                        finish = choice["finish_reason"]
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise UpstreamError("First-hop service returned an invalid SSE completion: " + str(exc)) from exc
            if not done or finish is None:
                raise UpstreamError("First-hop stream ended before finish_reason and [DONE]")
            if calls and (finish != "tool_calls" or sorted(calls) != list(range(len(calls)))):
                raise UpstreamError("First-hop tool batch was incomplete or had an invalid finish reason")
            if finish == "tool_calls" and not calls:
                raise UpstreamError("First-hop declared tool calls without a batch")
            yield RelayEvent("reply", reply=UpstreamReply(
                "".join(content), "".join(reasoning), usage, finish,
                tuple(calls[index] for index in sorted(calls)),
            ))
        except requests.RequestException as exc:
            raise UpstreamError(f"First-hop connection failed: {type(exc).__name__}") from exc
        finally:
            if response is not None:
                response.close()

    def _sse(self, response: requests.Response) -> Iterator[str | None]:
        # 每个未闭合 SSE 事件单独限流；重复帧元数据不占用工具参数预算。
        event_bytes = 0
        pending = bytearray()
        data: list[str] = []
        # 不能等待凑满缓冲区，否则短正文和心跳会被 requests 延迟交付。
        for chunk in response.iter_content(chunk_size=1):
            if not chunk:
                continue
            for index, part in enumerate(chunk.split(b"\n")):
                if index:
                    event_bytes += 1
                    if event_bytes > self.config.max_action_bytes:
                        raise UpstreamError("First-hop SSE event exceeds MAX_ACTION_BYTES")
                    try:
                        line = pending.rstrip(b"\r").decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise UpstreamError("First-hop SSE is not UTF-8") from exc
                    pending.clear()
                    if not line:
                        event_bytes = 0
                        if data:
                            yield "\n".join(data)
                            data.clear()
                    elif line.startswith(":"):
                        yield None
                    elif line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
                event_bytes += len(part)
                if event_bytes > self.config.max_action_bytes:
                    raise UpstreamError("First-hop SSE event exceeds MAX_ACTION_BYTES")
                pending.extend(part)
        if pending or data:
            raise UpstreamError("First-hop SSE ended in an incomplete event")

    def models(self) -> dict[str, Any]:
        response = None
        try:
            response = self.session.get(
                api_base(self.config.upstream_base_url) + "/models", headers=self._headers(),
                timeout=(self.config.upstream_connect_timeout, self.config.upstream_read_timeout),
            )
            if response.status_code != 200:
                raise UpstreamError(f"First-hop service returned HTTP {response.status_code}")
            value = response.json()
            if not isinstance(value, dict):
                raise UpstreamError("First-hop model endpoint returned an invalid object")
            return value
        except requests.RequestException as exc:
            raise UpstreamError(f"Cannot reach first-hop service: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise UpstreamError("First-hop model endpoint returned invalid JSON") from exc
        finally:
            if response is not None:
                response.close()
