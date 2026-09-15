from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .actions import (
    ActionTransportError,
    DecodedAction,
    ToolSpec,
    decode_action,
    render_action_prompt,
    render_action_reminder,
)


@dataclass(frozen=True)
class TextMessage:
    role: str
    content: str


@dataclass(frozen=True)
class RelayRequest:
    model: str
    messages: tuple[TextMessage, ...]
    instructions: str = ""
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: str = "auto"
    max_tokens: int = 8192
    sampling: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamReply:
    content: str
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


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
            )
            reply = self.backend.complete(attempt_request)
            try:
                if not reply.content.strip():
                    raise ActionTransportError("First-hop model returned no visible text")
                decoded = decode_action(reply.content, list(request.tools), self.max_action_bytes)
                self._validate_choice(decoded, request.tool_choice)
                return RelayResult(action=decoded, upstream=reply)
            except ActionTransportError as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
                messages.extend((
                    TextMessage(role="assistant", content=reply.content),
                    TextMessage(
                        role="user",
                        content=(
                            "The previous serialization was invalid and nothing was executed. "
                            "Retry the response. If an operation is needed, use one final closed "
                            "@@ACTION@@ JSON envelope with an allowed operation and schema-valid parameters."
                        ),
                    ),
                ))
        raise ActionTransportError(str(last_error or "Action transport failed"))

    @staticmethod
    def _prepare_messages(request: RelayRequest) -> list[TextMessage]:
        messages = list(request.messages)
        instructions = request.instructions.strip()
        if request.tools and request.tool_choice != "none":
            prompt = render_action_prompt(list(request.tools), request.tool_choice)
            instructions = f"{instructions}\n\n{prompt}" if instructions else prompt

        if instructions:
            context = "Operating instructions:\n" + instructions
            first_user = next((index for index, message in enumerate(messages) if message.role == "user"), None)
            if first_user is None:
                messages.insert(0, TextMessage(role="user", content=context))
            else:
                original = messages[first_user]
                messages[first_user] = TextMessage(
                    role="user",
                    content=context + "\n\nUser message:\n" + original.content,
                )

        if request.tools and request.tool_choice != "none":
            reminder = render_action_reminder()
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
                    content=original.content + "\n\n" + reminder,
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
