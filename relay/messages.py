from __future__ import annotations

import base64
import json
import uuid
from copy import deepcopy
from typing import Any

from .actions import ToolSpec
from .content import (
    build_content as merge_content,
    data_url,
    file_block,
    image_block,
    text_of,
)
from .engine import RelayRequest, RelayResult, TextMessage
from .features import request_options
from .normalize import (
    SEARCH_TOOL_TYPES, _unique_tools, history_call_id, normalize_history_call,
    tool_items, tool_name, tool_schema, tool_strict, validate_choice,
)

THINKING_SIGNATURE = "genai-compat-no-signature"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part for item in value if (part := text_of(item))]
    return [text_of(value)]


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _attachment(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = block.get("type")
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    if kind == "image":
        url = source.get("url")
        if source.get("type") == "base64":
            url = data_url(source.get("media_type") or "image/png", source.get("data"))
        return image_block(url)
    if kind == "document":
        filename = block.get("title") or "document"
        if source.get("type") == "url":
            return file_block({"filename": filename, "file_url": source.get("url")})
        if source.get("type") == "text":
            encoded = base64.b64encode(str(source.get("data") or "").encode()).decode()
            return file_block({"filename": filename, "file_data": "data:text/plain;base64," + encoded})
        return file_block({
            "filename": filename,
            "file_data": data_url(source.get("media_type") or "application/pdf", source.get("data")),
        })
    return None


def _contents(blocks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            if part := text_of(block.get("text")):
                texts.append(part)
        elif kind in {"image", "document"}:
            if attachment := _attachment(block):
                attachments.append(attachment)
    return "\n".join(texts), attachments


def message_tools(raw_tools: Any) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    names: set[str] = set()
    for item in tool_items(raw_tools):
        kind = item.get("type")
        if kind not in {None, "custom"} and kind not in SEARCH_TOOL_TYPES:
            raise ValueError(f"Unsupported Messages tool type: {kind!r}")
        name = tool_name(item.get("name"))
        if name in names:
            raise ValueError(f"Duplicate tool name: {name}")
        names.add(name)
        if kind in SEARCH_TOOL_TYPES:
            continue
        if not isinstance(item.get("input_schema"), dict):
            raise ValueError("Messages tool input_schema must be a JSON Schema object")
        result.append(ToolSpec(
            name=name,
            description=str(item.get("description") or ""),
            parameters=tool_schema(item.get("input_schema")),
            strict=tool_strict(item.get("strict")),
        ))
    return _unique_tools(result)


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "any":
            return "required"
        if kind in {"auto", "none"}:
            return kind
        if kind == "tool":
            return tool_name(choice.get("name"))
    raise ValueError("Unsupported Messages tool_choice")


def prepare_messages(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    choice = validate_choice(choice, tools, body)
    instructions = "\n\n".join(_string_list(body.get("system")))
    messages: list[TextMessage] = []
    seen_ids: set[str] = set()
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            raise ValueError("messages must contain objects")
        role = message.get("role", "user")
        if role not in {"assistant", "user"}:
            raise ValueError(f"Unsupported Messages role: {role!r}")
        pending: list[dict[str, Any]] = []

        def flush() -> None:
            if not pending:
                return
            text, attachments = _contents(pending)
            calls: list[dict[str, Any]] = []
            thinking: list[dict[str, Any]] = []
            for block in pending:
                kind = block.get("type")
                if kind == "tool_use":
                    if role != "assistant":
                        raise ValueError("Only assistant messages may contain tool_use")
                    if not isinstance(block.get("input"), dict):
                        raise ValueError("tool_use.input must be an object")
                    calls.append(normalize_history_call({
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": tool_name(block.get("name")),
                            "arguments": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":")),
                        },
                    }, seen_ids))
                elif kind in {"thinking", "redacted_thinking"}:
                    thinking.append(deepcopy(block))
                elif kind not in {"text", "image", "document"}:
                    raise ValueError(f"Unsupported Messages content block: {kind!r}")
            messages.append(TextMessage(
                role=role,
                content=merge_content([text], attachments),
                tool_calls=tuple(calls),
                reasoning_content="\n".join(str(block.get("thinking") or "") for block in thinking if block.get("type") == "thinking"),
                extra={"thinking_blocks": thinking} if thinking else {},
            ))
            pending.clear()

        for block in _blocks(message.get("content")):
            if block.get("type") != "tool_result":
                pending.append(block)
                continue
            flush()
            result_text, result_attachments = _contents(_blocks(block.get("content")))
            messages.append(TextMessage(
                role="tool",
                tool_call_id=history_call_id(block.get("tool_use_id")),
                content=merge_content([result_text], result_attachments),
                extra={"is_error": bool(block.get("is_error", False))},
            ))
        flush()

    raw_choice = body.get("tool_choice") or {}
    disable_parallel = raw_choice.get("disable_parallel_tool_use", False)
    if not isinstance(disable_parallel, bool):
        raise ValueError("disable_parallel_tool_use must be a boolean")
    sampling = {key: body[key] for key in ("temperature", "top_p") if key in body}
    if "stop_sequences" in body:
        sampling["stop"] = body["stop_sequences"]
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(messages),
        instructions=instructions,
        tools=tuple(tools),
        tool_choice=choice,
        parallel_tool_calls=not disable_parallel,
        max_tokens=int(body.get("max_tokens") or 8192),
        sampling=sampling,
        upstream_options=request_options(body, body.get("tools"), "anthropic"),
    )


def build_content(result: RelayResult) -> list[dict[str, Any]]:
    decoded = result.action
    blocks: list[dict[str, Any]] = []
    if result.upstream.reasoning:
        blocks.append({
            "type": "thinking",
            "thinking": result.upstream.reasoning,
            "signature": THINKING_SIGNATURE,
        })
    if decoded.text:
        blocks.append({"type": "text", "text": decoded.text})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name.removeprefix(call.namespace + ".") if call.namespace else call.name, "input": call.parameters}
        for call in decoded.calls
    )
    return blocks


def build_response(body: dict[str, Any], result: RelayResult) -> dict[str, Any]:
    stop_reason = "tool_use" if result.action.calls else "end_turn"
    usage = result.upstream.usage
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": body.get("model") or "chatglm",
        "content": build_content(result),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }




def estimate_tokens(body: dict[str, Any], tools: list[ToolSpec]) -> int:
    """按字符数粗估 token，不为计数调用模型。"""
    total = len("\n".join(_string_list(body.get("system"))))
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        total += len(text_of(message.get("content")))
        for block in _blocks(message.get("content")):
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                total += len(json.dumps(block["input"], ensure_ascii=False))
    for tool in tools:
        total += len(tool.name) + len(tool.description) + len(json.dumps(tool.parameters, ensure_ascii=False))
    return max(1, total // 4 + 8)


def error(message: str, kind: str = "api_error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}
