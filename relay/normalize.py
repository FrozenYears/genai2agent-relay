from __future__ import annotations

import json
from typing import Any, Iterable

from .actions import ToolSpec, encode_calls, encode_result
from .content import attachments_of, build_content, file_block, image_block, text_of
from .engine import RelayRequest, TextMessage
from .features import request_options


def chat_content(content: Any) -> str | list[dict[str, Any]]:
    """Preserve text, image and file parts of a Chat Completions message."""
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return text_of(content)
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            kind = part.get("type")
            if kind in {"text", "input_text", "output_text"}:
                texts.append(str(part.get("text") or ""))
            elif kind == "image_url":
                image = part.get("image_url")
                if isinstance(image, str):
                    image = {"url": image}
                if isinstance(image, dict):
                    block = image_block(image.get("url"), image.get("detail"))
                    if block:
                        attachments.append(block)
            elif kind == "file":
                block = file_block(part.get("file"))
                if block:
                    attachments.append(block)
    return build_content(texts, attachments)


def _unique_tools(tools: Iterable[ToolSpec]) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    names: set[str] = set()
    for tool in tools:
        if not tool.name or tool.name in names:
            continue
        names.add(tool.name)
        result.append(tool)
    return result


def chat_tools(raw_tools: Any) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    for item in raw_tools or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function" and isinstance(item.get("function"), dict):
            function = item["function"]
            result.append(ToolSpec(
                name=str(function.get("name") or ""),
                description=str(function.get("description") or ""),
                parameters=function.get("parameters") if isinstance(function.get("parameters"), dict) else {"type": "object"},
            ))
    return _unique_tools(result)


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return "auto"
    kind = choice.get("type")
    if kind in {"auto", "any", "required", "none"}:
        return "required" if kind == "any" else kind
    if kind in {"tool", "function", "custom"}:
        function = choice.get("function") if isinstance(choice.get("function"), dict) else choice
        return str(function.get("name") or "auto")
    return "auto"


def prepare_chat(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    system_parts: list[str] = []
    messages: list[TextMessage] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            system_parts.append(text_of(message.get("content")))
            continue
        if role == "tool":
            raw = chat_content(message.get("content"))
            attachments = attachments_of(raw)
            envelope = encode_result(str(message.get("tool_call_id") or "unknown"), text_of(raw))
            messages.append(TextMessage(role="user", content=build_content([envelope], attachments)))
            continue
        content = chat_content(message.get("content"))
        raw_calls = message.get("tool_calls") or []
        if role == "assistant" and raw_calls:
            calls = []
            for raw_call in raw_calls:
                function = raw_call.get("function") or {}
                arguments = function.get("arguments") or "{}"
                try:
                    parameters = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    parameters = {"raw": arguments}
                calls.append({"operation": function.get("name", ""), "parameters": parameters})
            content = build_content([text_of(content), encode_calls(calls)], attachments_of(content))
        messages.append(TextMessage(role="assistant" if role == "assistant" else "user", content=content))

    sampling = {key: body[key] for key in ("temperature", "top_p", "stop") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(messages),
        instructions="\n\n".join(system_parts),
        tools=tuple(tools),
        tool_choice=choice,
        max_tokens=int(body.get("max_tokens") or 8192),
        sampling=sampling,
        upstream_options=request_options(body, body.get("tools"), "chat"),
    )
