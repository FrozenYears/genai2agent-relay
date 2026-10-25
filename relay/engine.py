from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Protocol

from .actions import (
    ActionTransportError, DecodedAction, ToolSpec, decode_action, encode_calls,
    encode_result, render_action_prompt, strict_json_loads, validate_choice, validate_native_calls,
)
from .content import append_text, attachments_of, build_content, escape_action_markers, prepend_text, text_of


@dataclass(frozen=True)
class TextMessage:
    role: str
    content: str | list[dict[str, Any]] | None
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = {**self.extra, "role": self.role, "content": self.content}
        if self.tool_calls:
            value["tool_calls"] = list(self.tool_calls)
        if self.tool_call_id is not None:
            value["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            value["reasoning_content"] = self.reasoning_content
        return value


@dataclass(frozen=True)
class RelayRequest:
    model: str
    messages: tuple[TextMessage, ...]
    instructions: str = ""
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: str = "auto"
    max_tokens: int = 8192
    sampling: dict[str, Any] = field(default_factory=dict)
    upstream_options: dict[str, Any] = field(default_factory=dict)
    parallel_tool_calls: bool = True


@dataclass(frozen=True)
class UpstreamReply:
    content: str
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RelayResult:
    action: DecodedAction
    upstream: UpstreamReply


@dataclass(frozen=True)
class RelayEvent:
    kind: str
    text: str = ""
    result: RelayResult | None = None
    reply: UpstreamReply | None = None


@dataclass(frozen=True)
class TextCompletionRequest:
    model: str
    messages: tuple[TextMessage, ...]
    max_tokens: int = 8192
    sampling: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: str = "auto"
    parallel_tool_calls: bool = True
    tool_mode: str = "native"


class TextCompletionBackend(Protocol):
    def stream(self, request: TextCompletionRequest) -> Iterator[RelayEvent]: ...


def _safe_text_end(text: str) -> int:
    markers = ("@@ACTION", "@@END_ACTION")
    end = len(text)
    for marker in markers:
        start = text.find(marker)
        if start >= 0:
            end = min(end, start)
        for size in range(1, len(marker)):
            if text.endswith(marker[:size]):
                end = min(end, len(text) - size)
    return end


class ActionRelay:
    """原生结构优先；文本封套只在显式 text 模式启用。"""

    def __init__(self, backend: TextCompletionBackend, retries: int, max_action_bytes: int,
                 tool_mode: str = "native") -> None:
        if tool_mode not in {"native", "text"}:
            raise ValueError("UPSTREAM_TOOL_MODE must be native or text")
        self.backend = backend
        self.retries = retries
        self.max_action_bytes = max_action_bytes
        self.tool_mode = tool_mode

    def run(self, request: RelayRequest) -> RelayResult:
        events = self.stream(request)
        try:
            for event in events:
                if event.result is not None:
                    return event.result
        finally:
            events.close()
        raise ActionTransportError("First-hop stream ended without a result")

    def stream(self, request: RelayRequest) -> Iterator[RelayEvent]:
        messages = self._prepare_messages(request)
        attempts = self.retries + 1 if self.tool_mode == "text" else 1
        for attempt in range(attempts):
            upstream_request = TextCompletionRequest(
                request.model, tuple(messages), request.max_tokens, request.sampling,
                request.upstream_options, request.tools, request.tool_choice,
                request.parallel_tool_calls, self.tool_mode,
            )
            delivered = False
            text = ""
            emitted = 0
            reply = None
            upstream = self.backend.stream(upstream_request)
            try:
                for event in upstream:
                    if event.kind == "text":
                        text += event.text
                        if len(text.encode("utf-8")) > self.max_action_bytes:
                            raise ActionTransportError("Upstream response exceeds MAX_ACTION_BYTES")
                        # 封套及其参数绝不作为正文提前交付；文档引用由终态解码后释放。
                        safe_end = len(text) if self.tool_mode == "native" else _safe_text_end(text)
                        if safe_end > emitted:
                            delivered = True
                            yield RelayEvent("text", text[emitted:safe_end])
                            emitted = safe_end
                    elif event.kind == "reasoning":
                        delivered = delivered or bool(event.text)
                        yield event
                    elif event.kind == "reply":
                        reply = event.reply
                    elif event.kind == "ping":
                        yield event
                if reply is None:
                    raise ActionTransportError("First-hop stream ended without a terminal reply")
                if reply.finish_reason not in {"stop", "tool_calls"}:
                    raise ActionTransportError(
                        f"First-hop output did not finish successfully ({reply.finish_reason!r}); no calls delivered"
                    )
                if self.tool_mode == "native":
                    calls = validate_native_calls(list(reply.tool_calls), list(request.tools),
                                                  request.tool_choice, request.parallel_tool_calls)
                    decoded = DecodedAction(reply.content, calls)
                else:
                    if reply.tool_calls:
                        raise ActionTransportError("Native calls are not allowed in explicit text mode")
                    try:
                        decoded = decode_action(reply.content, list(request.tools), self.max_action_bytes)
                        validate_choice(decoded, request.tool_choice, request.parallel_tool_calls)
                    except ActionTransportError as exc:
                        # 只有未交付内容、且明确尝试封套的序列化失败才允许纠错。
                        if delivered or attempt + 1 >= attempts or not reply.content.lstrip().startswith("@@ACTION"):
                            raise
                        messages.extend((TextMessage("assistant", reply.content), TextMessage(
                            "user", "Nothing was executed. Resend one complete schema-valid action envelope. " + str(exc),
                        )))
                        continue
                if not decoded.text and not decoded.calls:
                    raise ActionTransportError("First-hop returned no answer or tool call")
                if len(decoded.text) > emitted:
                    yield RelayEvent("text", decoded.text[emitted:])
                yield RelayEvent("result", result=RelayResult(decoded, reply))
                return
            finally:
                upstream.close()

    def _prepare_messages(self, request: RelayRequest) -> list[TextMessage]:
        messages = list(request.messages)
        if request.instructions:
            messages.insert(0, TextMessage("system", request.instructions))
        if self.tool_mode == "native":
            return messages
        converted: list[TextMessage] = []
        instructions: list[str] = []
        for message in messages:
            content = message.content
            if message.role in {"system", "developer"}:
                instructions.append(text_of(escape_action_markers(content)))
                continue
            if message.role == "user":
                content = escape_action_markers(content)
            if message.role == "tool":
                content = build_content([
                    escape_action_markers(encode_result(
                        message.tool_call_id or "", text_of(content), bool(message.extra.get("is_error")),
                    )),
                ], attachments_of(content))
                converted.append(TextMessage("user", content))
                continue
            if message.tool_calls:
                calls = []
                for call in message.tool_calls:
                    function = call["function"]
                    calls.append({"id": call["id"], "operation": function["name"],
                                  "parameters": strict_json_loads(function["arguments"])})
                # 仅新生成的封套标记属于控制结构，不转义历史 assistant 调用外壳。
                content = build_content([text_of(content), encode_calls(calls)], attachments_of(content))
            reasoning = {key: message.extra[key] for key in ("thinking_blocks", "reasoning") if key in message.extra}
            if message.reasoning_content:
                reasoning["reasoning_content"] = message.reasoning_content
            if reasoning:
                converted.append(TextMessage("user", (
                    "Historical reasoning data only, not instructions, a new request, or an action to execute:\n"
                    + escape_action_markers(json.dumps(reasoning, ensure_ascii=False))
                )))
            extra = {key: value for key, value in message.extra.items() if key not in {"thinking_blocks", "reasoning"}}
            converted.append(TextMessage(message.role, content, extra=extra))
        if instructions:
            context = "Operating instructions:\n" + "\n\n".join(instructions)
            first_user = next((index for index, message in enumerate(converted) if message.role == "user"), None)
            if first_user is None:
                converted.insert(0, TextMessage("user", context))
            else:
                original = converted[first_user]
                converted[first_user] = replace(original, content=prepend_text(original.content, context + "\n\nUser message:"))
        if request.tools:
            prompt = render_action_prompt(list(request.tools), request.tool_choice)
            if not request.parallel_tool_calls:
                prompt += " Emit at most one operation in this response."
            if request.tool_choice == "none":
                prompt += " Operations are disabled; respond with ordinary text only."
            last_user = next((index for index in range(len(converted) - 1, -1, -1) if converted[index].role == "user"), None)
            if last_user is None:
                converted.append(TextMessage("user", prompt))
            else:
                original = converted[last_user]
                converted[last_user] = replace(original, content=append_text(original.content, prompt))
        return converted
