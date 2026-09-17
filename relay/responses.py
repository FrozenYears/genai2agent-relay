from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from .actions import ToolSpec, encode_calls, encode_result
from .engine import RelayRequest, RelayResult, TextMessage
from .normalize import flatten_text


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _arguments(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return {"raw": value}
    return parsed if isinstance(parsed, dict) else {"raw": value}


def response_tools(raw_tools: Any) -> list[ToolSpec]:
    result: list[ToolSpec] = []
    names: set[str] = set()
    for item in raw_tools or []:
        if not isinstance(item, dict) or item.get("type") != "function":
            continue
        name = str(item.get("name") or "")
        if not name or name in names:
            continue
        names.add(name)
        schema = item.get("parameters")
        result.append(ToolSpec(
            name=name,
            description=str(item.get("description") or ""),
            parameters=schema if isinstance(schema, dict) else {"type": "object"},
        ))
    return result


def normalize_choice(choice: Any) -> str:
    if choice is None:
        return "auto"
    if isinstance(choice, str):
        return choice if choice in {"auto", "none", "required"} else "auto"
    if not isinstance(choice, dict):
        return "auto"
    kind = choice.get("type")
    if kind in {"auto", "none", "required"}:
        return kind
    if kind == "function":
        return str(choice.get("name") or "auto")
    return "auto"


def prepare_responses(body: dict[str, Any], tools: list[ToolSpec], choice: str) -> RelayRequest:
    instructions = flatten_text(body.get("instructions"))
    raw_input = body.get("input")
    messages: list[dict[str, str]] = []
    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
    else:
        for item in _items(raw_input):
            kind = item.get("type")
            if kind == "function_call":
                arguments = item.get("arguments")
                try:
                    parameters = json.loads(arguments) if isinstance(arguments, str) else arguments
                except json.JSONDecodeError:
                    parameters = {"raw": arguments}
                messages.append({"role": "assistant", "content": encode_calls([{
                    "operation": str(item.get("name") or ""),
                    "parameters": parameters if isinstance(parameters, dict) else {},
                }])})
                continue
            if kind == "function_call_output":
                messages.append({"role": "user", "content": encode_result(
                    str(item.get("call_id") or "unknown"),
                    flatten_text(item.get("output")),
                )})
                continue
            role = "assistant" if item.get("role") == "assistant" else "user"
            text = flatten_text(item.get("content"))
            if role == "assistant" and item.get("tool_calls"):
                text = (text + "\n" if text else "") + encode_calls([
                    {
                        "operation": str((call.get("function") or {}).get("name") or ""),
                        "parameters": _arguments((call.get("function") or {}).get("arguments")),
                    }
                    for call in item["tool_calls"]
                    if isinstance(call, dict)
                ])
            if text:
                messages.append({"role": role, "content": text})

    sampling = {key: body[key] for key in ("temperature", "top_p") if key in body}
    return RelayRequest(
        model=str(body.get("model") or "chatglm"),
        messages=tuple(TextMessage(**message) for message in messages),
        instructions=instructions,
        tools=tuple(tools),
        tool_choice=choice,
        max_tokens=int(body.get("max_output_tokens") or 8192),
        sampling=sampling,
    )


def build_output(result: RelayResult) -> list[dict[str, Any]]:
    decoded = result.action
    output: list[dict[str, Any]] = []
    if decoded.text:
        output.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex[:24],
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": decoded.text, "annotations": []}],
        })
    output.extend(
        {
            "type": "function_call",
            "id": "fc_" + uuid.uuid4().hex[:24],
            "call_id": "call_" + uuid.uuid4().hex[:24],
            "name": call.name,
            "arguments": json.dumps(call.parameters, ensure_ascii=False, separators=(",", ":")),
            "status": "completed",
        }
        for call in decoded.calls
    )
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
        "parallel_tool_calls": True,
        "tool_choice": body.get("tool_choice") or "auto",
        "tools": body.get("tools") or [],
        "usage": _usage(result),
    }


def stream_events(body: dict[str, Any], result: RelayResult) -> Iterable[str]:
    response = build_response(body, result)

    def emit(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield emit("response.created", {
        "type": "response.created",
        "response": {**response, "status": "in_progress", "output": [], "output_text": ""},
    })
    for index, item in enumerate(response["output"]):
        if item["type"] == "message":
            yield emit("response.output_item.added", {
                "type": "response.output_item.added", "output_index": index,
                "item": {**item, "status": "in_progress", "content": []},
            })
            yield emit("response.content_part.added", {
                "type": "response.content_part.added", "item_id": item["id"],
                "output_index": index, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            yield emit("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": item["id"],
                "output_index": index, "content_index": 0, "delta": item["content"][0]["text"],
            })
            yield emit("response.output_text.done", {
                "type": "response.output_text.done", "item_id": item["id"],
                "output_index": index, "content_index": 0, "text": item["content"][0]["text"],
            })
            yield emit("response.content_part.done", {
                "type": "response.content_part.done", "item_id": item["id"],
                "output_index": index, "content_index": 0, "part": item["content"][0],
            })
        else:
            yield emit("response.output_item.added", {
                "type": "response.output_item.added", "output_index": index,
                "item": {**item, "arguments": "", "status": "in_progress"},
            })
            yield emit("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta", "item_id": item["id"],
                "output_index": index, "delta": item["arguments"],
            })
            yield emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done", "item_id": item["id"],
                "output_index": index, "arguments": item["arguments"],
            })
        yield emit("response.output_item.done", {
            "type": "response.output_item.done", "output_index": index, "item": item,
        })
    yield emit("response.completed", {"type": "response.completed", "response": response})


def estimate_tokens(body: dict[str, Any], tools: list[ToolSpec]) -> int:
    """Rough character-based estimate. This transport never calls the model to count."""
    total = len(flatten_text(body.get("instructions")))
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        total += len(raw_input)
    for item in _items(raw_input):
        total += len(flatten_text(item.get("content")))
        total += len(flatten_text(item.get("output")))
        total += len(flatten_text(item.get("arguments")))
    for tool in tools:
        total += len(tool.name) + len(tool.description) + len(json.dumps(tool.parameters, ensure_ascii=False))
    return max(1, total // 4 + 8)


def error(message: str, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}
