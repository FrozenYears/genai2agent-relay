from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .actions import (
    ActionDelimiterError,
    ActionTransportError,
    ActionValidationError,
    DecodedAction,
    ToolSpec,
    decode_action,
    render_action_prompt,
)
from .content import append_text, prepend_text


@dataclass(frozen=True)
class TextMessage:
    """One chat message; content is text or a list of text/attachment blocks."""

    role: str
    content: str | list[dict[str, Any]]


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


@dataclass(frozen=True)
class UpstreamReply:
    content: str
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None


@dataclass(frozen=True)
class RelayResult:
    action: DecodedAction
    upstream: UpstreamReply


@dataclass(frozen=True)
class TextCompletionRequest:
    model: str
    messages: tuple[TextMessage, ...]
    max_tokens: int = 8192
    sampling: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


class TextCompletionBackend(Protocol):
    def complete(self, request: TextCompletionRequest) -> UpstreamReply:
        """Return one complete, ordinary-text assistant response."""


class TextActionRelay:
    """Protocol-neutral action transport over a plain-text completion backend."""

    def __init__(self, backend: TextCompletionBackend, retries: int, max_action_bytes: int) -> None:
        self.backend = backend
        self.retries = retries
        self.max_action_bytes = max_action_bytes

    def run(self, request: RelayRequest) -> RelayResult:
        messages = self._prepare_messages(request)
        last_error: ActionTransportError | None = None
        for attempt in range(self.retries + 1):
            attempt_request = TextCompletionRequest(
                model=request.model,
                messages=tuple(messages),
                max_tokens=request.max_tokens,
                sampling=request.sampling,
                options=request.upstream_options,
            )
            reply = self.backend.complete(attempt_request)
            # 截断不属于序列化错误；不以相同预算盲目重试，也不交付可能未完成的操作。
            if reply.finish_reason == "length":
                raise ActionTransportError(
                    "First-hop output was truncated (finish_reason=length); "
                    "no actions were delivered. Review max_tokens (includes reasoning)."
                )
            try:
                if not reply.content.strip():
                    raise ActionTransportError("First-hop model returned no answer or action")
                decoded = decode_action(reply.content, list(request.tools), self.max_action_bytes)
                self._validate_choice(decoded, request.tool_choice)
                return RelayResult(action=decoded, upstream=reply)
            except ActionTransportError as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                if not reply.content.strip():
                    # 不把思考转为正文，也不向历史追加空的 assistant 消息。
                    messages.append(TextMessage(
                        role="user",
                        content=(
                            "上一次没有返回正文或实际工具调用，未执行任何操作。请继续。"
                            "如果需要操作，请输出完整合法的 @@ACTION@@ JSON 封套，"
                            "并以 @@END_ACTION@@ 结束；不要只输出思考或行动计划。"
                        ),
                    ))
                    continue
                diagnostic = ""
                if isinstance(exc.__cause__, json.JSONDecodeError):
                    error = exc.__cause__
                    diagnostic = (
                        f" The envelope JSON failed to parse: {error.msg} "
                        f"(line {error.lineno}, column {error.colno} within the JSON body). "
                        "Check the JSON structure at that position and resend the complete envelope, "
                        "not a patch or simulated execution result."
                    )
                elif isinstance(exc, ActionDelimiterError):
                    diagnostic = (
                        " The action opening marker is malformed or missing; it must be exactly @@ACTION@@. "
                        "Please continue and resend the complete envelope ending with @@END_ACTION@@. "
                        "Correctly escape quotes, backslashes and newlines inside JSON strings."
                    )
                elif isinstance(exc, ActionValidationError):
                    diagnostic = " " + exc.feedback
                    if exc.tool is not None:
                        diagnostic += "\n" + render_action_prompt([exc.tool], request.tool_choice)
                messages.extend((
                    TextMessage(role="assistant", content=reply.content),
                    TextMessage(
                        role="user",
                        content=(
                            "The previous serialization was invalid and nothing was executed. "
                            "Retry the response. If an operation is needed, use one final closed "
                            "@@ACTION@@ JSON envelope with an allowed operation and schema-valid parameters."
                            + diagnostic
                        ),
                    ),
                ))
        raise ActionTransportError(str(last_error or "Action transport failed"))

    @staticmethod
    def _prepare_messages(request: RelayRequest) -> list[TextMessage]:
        messages = list(request.messages)
        instructions = request.instructions.strip()

        if instructions:
            context = "Operating instructions:\n" + instructions
            first_user = next((index for index, message in enumerate(messages) if message.role == "user"), None)
            if first_user is None:
                messages.insert(0, TextMessage(role="user", content=context))
            else:
                original = messages[first_user]
                messages[first_user] = TextMessage(
                    role="user",
                    content=prepend_text(original.content, context + "\n\nUser message:"),
                )

        if request.tools and request.tool_choice != "none":
            reminder = render_action_prompt(list(request.tools), request.tool_choice)
            last_user = next(
                (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
                None,
            )
            if last_user is None:
                messages.append(TextMessage(role="user", content=reminder))
            else:
                original = messages[last_user]
                messages[last_user] = TextMessage(
                    role="user",
                    content=append_text(original.content, reminder),
                )
        return messages

    @staticmethod
    def _validate_choice(decoded: DecodedAction, choice: str) -> None:
        if choice == "required" and not decoded.calls:
            raise ActionTransportError("A required operation was not produced")
        if choice not in {"auto", "required", "none"}:
            if not decoded.calls or any(call.name != choice for call in decoded.calls):
                raise ActionTransportError(f"The required operation {choice!r} was not produced exclusively")
        if choice == "none" and decoded.calls:
            raise ActionTransportError("Operations are disabled for this request")
