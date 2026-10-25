from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Iterable

from .actions import ToolSpec, strict_json_loads
from .content import build_content, file_block, image_block, text_of
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


SEARCH_TOOL_TYPES = {
    "web_search", "web_search_preview", "web_search_preview_2025_03_11",
    "web_search_20250305", "web_search_20260209",
}


def tool_items(raw_tools: Any) -> list[dict[str, Any]]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list) or any(not isinstance(item, dict) for item in raw_tools):
        raise ValueError("tools must be an array of objects")
    return raw_tools


def tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Tool name must be a non-empty string")
    return value


def _unique_tools(tools: Iterable[ToolSpec]) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    names: set[str] = set()
    for tool in tools:
        if tool.name in names:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        names.add(tool.name)
        result.append(tool)
    return result


def tool_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    if not isinstance(value, dict):
        raise ValueError("Tool parameters must be a JSON Schema object")
    return value


def tool_strict(value: Any) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError("Tool strict must be a boolean")
    return value


def custom_schema(format_: Any = None) -> dict[str, Any]:
    if format_ is not None and (not isinstance(format_, dict) or format_.get("type") != "text"):
        raise ValueError("Only custom tool format:text is supported; grammar is unsupported")
    return {
        "type": "object",
        "properties": {"input": {"type": "string"}},
        "required": ["input"],
        "additionalProperties": False,
    }


def history_call_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Historical tool call requires a non-empty call ID")
    return value


def normalize_history_call(call: Any, seen_ids: set[str]) -> dict[str, Any]:
    if not isinstance(call, dict):
        raise ValueError("Historical tool calls must be objects")
    call_id = history_call_id(call.get("id"))
    if call_id in seen_ids:
        raise ValueError(f"Duplicate historical tool call ID: {call_id}")
    kind = call.get("type")
    if kind not in ("function", "custom") or not isinstance(call.get(kind), dict):
        raise ValueError("Historical tool call must contain a function or custom object")
    tool = call[kind]
    name = tool_name(tool.get("name"))
    if kind == "custom":
        if not isinstance(tool.get("input"), str):
            raise ValueError("Historical custom tool input must be a string")
        arguments = json.dumps({"input": tool["input"]}, ensure_ascii=False, separators=(",", ":"))
        normalized = {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
    else:
        arguments = tool.get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("Historical function arguments must be a JSON string")
        try:
            parsed = strict_json_loads(arguments)
        except ValueError as exc:
            raise ValueError("Historical function arguments must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Historical function arguments must encode an object")
        normalized = deepcopy(call)
    seen_ids.add(call_id)
    return normalized


def normalize_history_calls(calls: Any, seen_ids: set[str]) -> tuple[dict[str, Any], ...]:
    if calls is None:
        return ()
    if not isinstance(calls, list):
        raise ValueError("Historical tool_calls must be an array")
    return tuple(normalize_history_call(call, seen_ids) for call in calls)


def chat_tools(raw_tools: Any) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    declared: set[str] = set()
    for item in tool_items(raw_tools):
        kind = item.get("type")
        if kind in SEARCH_TOOL_TYPES:
            name = tool_name(item.get("name", "web_search"))
        elif kind in ("function", "custom") and isinstance(item.get(kind), dict):
            tool = item[kind]
            name = tool_name(tool.get("name"))
            result.append(ToolSpec(
                name=name,
                description=str(tool.get("description") or ""),
                parameters=custom_schema(tool.get("format")) if kind == "custom" else tool_schema(tool.get("parameters")),
                kind=kind,
                strict=tool_strict(tool.get("strict")),
            ))
        else:
            raise ValueError(f"Unsupported Chat tool type: {kind!r}")
        if name in declared:
            raise ValueError(f"Duplicate tool name: {name}")
        declared.add(name)
    return _unique_tools(result)


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if isinstance(choice, str) and choice in {"auto", "required", "none"}:
        return choice
    if isinstance(choice, dict) and choice.get("type") in ("function", "custom"):
        tool = choice.get(choice["type"])
        if isinstance(tool, dict):
            return tool_name(tool.get("name"))
    raise ValueError("Unsupported Chat tool_choice")


def validate_choice(choice: str, tools: list[ToolSpec], body: dict[str, Any]) -> str:
    _unique_tools(tools)
    raw_choice = body.get("tool_choice")
    selected = isinstance(raw_choice, dict) and raw_choice.get("type") in {"tool", "function", "custom"}
    search_names = {
        tool_name(item.get("name", "web_search"))
        for item in tool_items(body.get("tools"))
        if item.get("type") in SEARCH_TOOL_TYPES
    }
    names = {tool.name for tool in tools}
    if selected or choice not in {"auto", "none", "required"}:
        if choice not in names | search_names:
            raise ValueError(f"Selected tool does not exist: {choice}")
        if choice in search_names:
            raise ValueError("Built-in search cannot satisfy a named tool_choice; use auto or none")
        selected_tool = next(tool for tool in tools if tool.name == choice)
        if selected and raw_choice["type"] in {"function", "custom"} and selected_tool.kind != raw_choice["type"]:
            raise ValueError("tool_choice type does not match selected tool")
    elif choice == "required" and not names:
        if not search_names:
            raise ValueError("tool_choice required needs at least one tool")
        raise ValueError("Built-in search cannot satisfy tool_choice required; use auto or none")
    return choice


def parallel_calls(body: dict[str, Any]) -> bool:
    value = body.get("parallel_tool_calls", True)
    if not isinstance(value, bool):
        raise ValueError("parallel_tool_calls must be a boolean")
    return value


def prepare_chat(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    choice = validate_choice(choice, tools, body)
    messages: list[TextMessage] = []
    seen_ids: set[str] = set()
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            raise ValueError("messages must contain objects")
        role = message.get("role", "user")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"Unsupported message role: {role!r}")
        calls = normalize_history_calls(message.get("tool_calls"), seen_ids)
        if calls and role != "assistant":
            raise ValueError("Only assistant messages may contain tool_calls")
        messages.append(TextMessage(
            role=role,
            content=chat_content(message.get("content")),
            tool_calls=calls,
            tool_call_id=history_call_id(message.get("tool_call_id")) if role == "tool" else message.get("tool_call_id"),
            reasoning_content=message.get("reasoning_content") or "",
            extra=deepcopy({
                key: value for key, value in message.items()
                if key not in {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"}
            }),
        ))

    sampling = {key: body[key] for key in ("temperature", "top_p", "stop") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(messages),
        tools=tuple(tools),
        tool_choice=choice,
        parallel_tool_calls=parallel_calls(body),
        max_tokens=int(body.get("max_completion_tokens") or body.get("max_tokens") or 8192),
        sampling=sampling,
        upstream_options=request_options(body, body.get("tools"), "chat"),
    )
