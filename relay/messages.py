from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Iterable

from .actions import ToolSpec, encode_calls, encode_result
from .content import (
    build_content as merge_content,
    data_url,
    file_block,
    image_block,
    text_of,
)
from .engine import RelayRequest, RelayResult, TextMessage
from .features import request_options

STOP_REASONS = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
}
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
    for item in raw_tools or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name or name in names:
            continue
        names.add(name)
        schema = item.get("input_schema")
        result.append(ToolSpec(
            name=name,
            description=str(item.get("description") or ""),
            parameters=schema if isinstance(schema, dict) else {"type": "object"},
        ))
    return result


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if not isinstance(choice, dict):
        return "auto"
    kind = choice.get("type")
    if kind == "any":
        return "required"
    if kind in {"auto", "none"}:
        return kind
    if kind == "tool":
        return str(choice.get("name") or "auto")
    return "auto"


def prepare_messages(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    instructions = "\n\n".join(_string_list(body.get("system")))
    messages: list[TextMessage] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = "assistant" if message.get("role") == "assistant" else "user"
        blocks = _blocks(message.get("content"))
        text, attachments = _contents(blocks)
        calls: list[dict[str, Any]] = []
        results: list[tuple[str, list[dict[str, Any]], str, bool]] = []
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                parameters = block.get("input")
                calls.append({
                    "operation": str(block.get("name") or ""),
                    "parameters": parameters if isinstance(parameters, dict) else {},
                })
            elif kind == "tool_result":
                result_text, result_attachments = _contents(_blocks(block.get("content")))
                results.append((
                    str(block.get("tool_use_id") or "unknown"),
                    result_attachments,
                    result_text,
                    bool(block.get("is_error")),
                ))
        if calls:
            text = "\n".join(part for part in (text, encode_calls(calls)) if part)
        if results:
            if text or attachments:
                messages.append(TextMessage(role=role, content=merge_content([text], attachments)))
            for call_id, result_attachments, result_text, is_error in results:
                envelope = encode_result(call_id, result_text, is_error)
                messages.append(TextMessage(
                    role="user",
                    content=merge_content([envelope], result_attachments),
                ))
            continue
        messages.append(TextMessage(role=role, content=merge_content([text], attachments)))

    sampling = {key: body[key] for key in ("temperature", "top_p") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(messages),
        instructions=instructions,
        tools=tuple(tools),
        tool_choice=choice,
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
        {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": call.name, "input": call.parameters}
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


def stream_events(body: dict[str, Any], result: RelayResult) -> Iterable[str]:
    response = build_response(body, result)
    usage = response["usage"]
    start_usage = {"input_tokens": usage["input_tokens"], "output_tokens": 0}

    def emit(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield emit("message_start", {
        "type": "message_start",
        "message": {**response, "content": [], "stop_reason": None, "stop_sequence": None, "usage": start_usage},
    })
    yield emit("ping", {"type": "ping"})
    for index, block in enumerate(response["content"]):
        if block["type"] == "thinking":
            yield emit("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            })
            yield emit("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "thinking_delta", "thinking": block["thinking"]},
            })
            yield emit("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "signature_delta", "signature": block["signature"]},
            })
        elif block["type"] == "text":
            yield emit("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""},
            })
            yield emit("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": block["text"]},
            })
        else:
            yield emit("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
            })
            yield emit("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":")),
                },
            })
        yield emit("content_block_stop", {"type": "content_block_stop", "index": index})
    yield emit("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": response["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": usage["output_tokens"]},
    })
    yield emit("message_stop", {"type": "message_stop"})


def estimate_tokens(body: dict[str, Any], tools: list[ToolSpec]) -> int:
    """Rough character-based estimate. This transport never calls the model to count."""
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
