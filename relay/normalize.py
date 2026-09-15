from __future__ import annotations

import json
from typing import Any, Iterable

from .actions import ToolSpec, encode_calls, encode_result
from .engine import RelayRequest, TextMessage


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := flatten_text(item)))
    if isinstance(value, dict):
        for key in ("text", "input_text", "output_text"):
            if isinstance(value.get(key), str):
                return value[key]
        if "content" in value:
            return flatten_text(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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
    messages: list[dict[str, str]] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        if role in {"system", "developer"}:
            system_parts.append(flatten_text(message.get("content")))
            continue
        if role == "tool":
            messages.append({"role": "user", "content": encode_result(
                str(message.get("tool_call_id") or "unknown"),
                flatten_text(message.get("content")),
            )})
            continue
        content = flatten_text(message.get("content"))
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
            content = (content + "\n" if content else "") + encode_calls(calls)
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    sampling = {key: body[key] for key in ("temperature", "top_p", "stop") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(TextMessage(**message) for message in messages),
        instructions="\n\n".join(system_parts),
        tools=tuple(tools),
        tool_choice=choice,
        max_tokens=int(body.get("max_tokens") or 8192),
        sampling=sampling,
    )
