from __future__ import annotations

import json
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .actions import ToolSpec
from .content import build_content, file_block, image_block, text_of
from .engine import RelayRequest, RelayResult, TextMessage
from .features import request_options
from .normalize import (
    SEARCH_TOOL_TYPES, _unique_tools, custom_schema, history_call_id,
    normalize_history_call, normalize_history_calls, parallel_calls,
    tool_items, tool_name, tool_schema, tool_strict, validate_choice,
)


def _items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("input must be a string or an array of objects")
    return value


def _qualified_name(item: dict[str, Any]) -> tuple[str, str | None]:
    name = tool_name(item.get("name"))
    namespace = item.get("namespace")
    if namespace is not None:
        namespace = tool_name(namespace)
        return f"{namespace}.{name}", namespace
    return name, None


def _file_source(part: dict[str, Any]) -> dict[str, Any]:
    file = part.get("file") if isinstance(part.get("file"), dict) else {}
    merged = {key: part[key] for key in ("filename", "file_data", "file_url") if key in part}
    merged.update(file)
    return merged


def response_content(content: Any) -> str | list[dict[str, Any]]:
    """保留 Responses 内容中的文本、图片和文件。"""
    if isinstance(content, str) or content is None:
        return content or ""
    if not isinstance(content, list):
        return text_of(content)
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in {"input_text", "output_text", "text", "summary_text"}:
            texts.append(str(part.get("text") or ""))
        elif kind in {"input_image", "image_url", "image"}:
            image = part.get("image_url") or (part.get("source") or {}).get("url")
            if isinstance(image, dict):
                image = image.get("url")
            block = image_block(image, part.get("detail"))
            if block:
                attachments.append(block)
        elif kind in {"input_file", "file"}:
            block = file_block(_file_source(part))
            if block:
                attachments.append(block)
    return build_content(texts, attachments)


def response_tools(raw_tools: Any) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    declared: set[str] = set()

    def add(item: dict[str, Any], namespace: str | None = None) -> None:
        kind = item.get("type")
        if kind in SEARCH_TOOL_TYPES and namespace is None:
            name = tool_name(item.get("name", "web_search"))
        elif kind in {"function", "custom"}:
            leaf = tool_name(item.get("name"))
            name = f"{namespace}.{leaf}" if namespace else leaf
            if kind == "custom":
                schema = custom_schema(item.get("format"))
            else:
                schema = tool_schema(item.get("parameters"))
            result.append(ToolSpec(
                name=name,
                description=str(item.get("description") or ""),
                parameters=schema,
                kind=kind,
                namespace=namespace,
                strict=tool_strict(item.get("strict")),
            ))
        else:
            raise ValueError(f"Unsupported Responses tool type: {kind!r}")
        if name in declared:
            raise ValueError(f"Duplicate tool name: {name}")
        declared.add(name)

    namespaces: set[str] = set()
    for item in tool_items(raw_tools):
        if item.get("type") == "namespace":
            namespace = tool_name(item.get("name"))
            if namespace in namespaces:
                raise ValueError(f"Duplicate tool namespace: {namespace}")
            namespaces.add(namespace)
            for child in tool_items(item.get("tools")):
                add(child, namespace)
        else:
            add(item)
    return _unique_tools(result)


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        return choice
    if isinstance(choice, dict) and choice.get("type") in {"function", "custom"}:
        return _qualified_name(choice)[0]
    raise ValueError("Unsupported Responses tool_choice")


def prepare_responses(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    if body.get("previous_response_id") is not None:
        raise ValueError("previous_response_id is unsupported; send complete input history")
    if "storage" in body:
        raise ValueError("storage is unsupported")
    if "store" in body and not isinstance(body["store"], bool):
        raise ValueError("store must be a boolean")
    if body.get("store") is True:
        raise ValueError("store=true is unsupported")
    choice = validate_choice(choice, tools, body)
    instructions = text_of(body.get("instructions"))
    raw_input = body.get("input")
    messages: list[TextMessage] = []
    seen_ids: set[str] = set()
    if isinstance(raw_input, str):
        messages.append(TextMessage(role="user", content=raw_input))
    else:
        for item in _items(raw_input):
            kind = item.get("type", "message")
            if kind == "item_reference":
                raise ValueError("item_reference is unsupported; send the complete item")
            if kind == "reasoning":
                reasoning = "\n".join(
                    str(part.get("text") or "")
                    for key in ("summary", "content")
                    for part in item.get(key) or []
                    if isinstance(part, dict) and part.get("text")
                )
                messages.append(TextMessage(
                    role="assistant", content="", reasoning_content=reasoning,
                    extra={"reasoning": deepcopy(item)},
                ))
                continue
            if kind in {"function_call", "custom_tool_call"}:
                name, _ = _qualified_name(item)
                if kind == "custom_tool_call":
                    if not isinstance(item.get("input"), str):
                        raise ValueError("custom_tool_call.input must be a string")
                    arguments = json.dumps({"input": item["input"]}, ensure_ascii=False, separators=(",", ":"))
                else:
                    arguments = item.get("arguments")
                    if not isinstance(arguments, str):
                        raise ValueError("function_call.arguments must be a JSON string")
                call = normalize_history_call({
                    "id": item.get("call_id"),
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }, seen_ids)
                if messages and messages[-1].role == "assistant":
                    previous = messages[-1]
                    messages[-1] = replace(previous, tool_calls=previous.tool_calls + (call,))
                else:
                    messages.append(TextMessage(role="assistant", content="", tool_calls=(call,)))
                continue
            if kind in {"function_call_output", "custom_tool_call_output"}:
                messages.append(TextMessage(
                    role="tool",
                    tool_call_id=history_call_id(item.get("call_id")),
                    content=response_content(item.get("output")),
                    extra=deepcopy({
                        key: value for key, value in item.items()
                        if key not in {"type", "call_id", "output"}
                    }),
                ))
                continue
            if kind == "web_search_call":
                messages.append(TextMessage(role="assistant", content="", extra={"web_search_call": deepcopy(item)}))
                continue
            if kind != "message":
                raise ValueError(f"Unsupported Responses input item type: {kind!r}")
            role = item.get("role", "user")
            if role not in {"system", "developer", "user", "assistant", "tool"}:
                raise ValueError(f"Unsupported message role: {role!r}")
            calls = normalize_history_calls(item.get("tool_calls"), seen_ids)
            if calls and role != "assistant":
                raise ValueError("Only assistant messages may contain tool_calls")
            messages.append(TextMessage(
                role=role,
                content=response_content(item.get("content")),
                tool_calls=calls,
                tool_call_id=history_call_id(item.get("tool_call_id")) if role == "tool" else item.get("tool_call_id"),
                reasoning_content=item.get("reasoning_content") or "",
                extra=deepcopy({
                    key: value for key, value in item.items()
                    if key not in {"type", "role", "content", "tool_calls", "tool_call_id", "reasoning_content"}
                }),
            ))

    sampling = {key: body[key] for key in ("temperature", "top_p") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(messages),
        instructions=instructions,
        tools=tuple(tools),
        tool_choice=choice,
        parallel_tool_calls=parallel_calls(body),
        max_tokens=int(body.get("max_output_tokens") or 8192),
        sampling=sampling,
        upstream_options=request_options(body, body.get("tools"), "responses"),
    )


def _reasoning_item(item_id: str, text: str, status: str) -> dict[str, Any]:
    summary = [{"type": "summary_text", "text": text}] if status == "completed" else []
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": summary,
        "content": [],
        "encrypted_content": None,
        "status": status,
    }


def build_output(result: RelayResult) -> list[dict[str, Any]]:
    decoded = result.action
    output: list[dict[str, Any]] = []
    if result.upstream.reasoning:
        output.append(_reasoning_item("rs_" + uuid.uuid4().hex[:24], result.upstream.reasoning, "completed"))
    if decoded.text:
        output.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex[:24],
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": decoded.text, "annotations": []}],
        })
    for call in decoded.calls:
        custom = call.kind == "custom"
        item = {
            "type": "custom_tool_call" if custom else "function_call",
            "id": ("ctc_" if custom else "fc_") + uuid.uuid4().hex[:24],
            "call_id": call.id,
            "name": call.name.removeprefix(call.namespace + ".") if call.namespace else call.name,
            "status": "completed",
        }
        if call.namespace:
            item["namespace"] = call.namespace
        if custom:
            item["input"] = call.parameters["input"]
        else:
            item["arguments"] = json.dumps(call.parameters, ensure_ascii=False, separators=(",", ":"))
        output.append(item)
    return output


def _usage(result: RelayResult) -> dict[str, int]:
    usage = result.upstream.usage
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion}


def build_response(body: dict[str, Any], result: RelayResult) -> dict[str, Any]:
    output = build_output(result)
    text = "".join(
        block["text"]
        for item in output if item["type"] == "message"
        for block in item["content"]
    )
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": body.get("model") or "chatglm",
        "output": output,
        "output_text": text,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "tool_choice": body.get("tool_choice") or "auto",
        "tools": body.get("tools") or [],
        "usage": _usage(result),
    }




def estimate_tokens(body: dict[str, Any], tools: list[ToolSpec]) -> int:
    """按字符数粗估 token，不为计数调用模型。"""
    total = len(text_of(body.get("instructions")))
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        total += len(raw_input)
    for item in _items(raw_input):
        total += len(text_of(item.get("content")))
        total += len(text_of(item.get("output")))
        total += len(text_of(item.get("arguments")))
    for tool in tools:
        total += len(tool.name) + len(tool.description) + len(json.dumps(tool.parameters, ensure_ascii=False))
    return max(1, total // 4 + 8)


def error(message: str, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}
