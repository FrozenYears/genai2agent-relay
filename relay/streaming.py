from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from . import messages, responses
from .actions import ActionCall, ActionTransportError
from .engine import ActionRelay, RelayRequest
from .upstream import UpstreamError


def chat_call(call: ActionCall) -> dict[str, Any]:
    if call.kind == "custom":
        return {"id": call.id, "type": "custom", "custom": {"name": call.name, "input": call.parameters["input"]}}
    return {"id": call.id, "type": "function", "function": {
        "name": call.name, "arguments": json.dumps(call.parameters, ensure_ascii=False, separators=(",", ":")),
    }}


def _sse(data: dict[str, Any], event: str | None = None) -> str:
    return (f"event: {event}\n" if event else "") + "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def chat_events(body: dict[str, Any], relay: ActionRelay, request: RelayRequest) -> Iterator[str]:
    base = {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion.chunk",
            "created": int(time.time()), "model": request.model}

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> str:
        return _sse({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})

    yield chunk({"role": "assistant"})
    events = relay.stream(request)
    try:
        for event in events:
            if event.kind in {"text", "reasoning"}:
                yield chunk({"content" if event.kind == "text" else "reasoning_content": event.text})
            elif event.kind == "ping":
                yield ": ping\n\n"
            elif event.result is not None:
                result = event.result
                if result.action.calls:
                    yield chunk({"tool_calls": [dict(chat_call(call), index=index)
                                                for index, call in enumerate(result.action.calls)]})
                usage = result.upstream.usage
                prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                extra = {}
                if (body.get("stream_options") or {}).get("include_usage"):
                    extra["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
                yield chunk({}, "tool_calls" if result.action.calls else "stop", **extra)
                yield "data: [DONE]\n\n"
    except (UpstreamError, ActionTransportError) as exc:
        yield _sse({"error": {"type": "api_error", "message": str(exc)}})
    finally:
        events.close()


def message_events(body: dict[str, Any], relay: ActionRelay, request: RelayRequest) -> Iterator[str]:
    message = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
               "model": request.model, "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 0, "output_tokens": 0}}

    def emit(kind: str, **data: Any) -> str:
        return _sse({"type": kind, **data}, kind)

    yield emit("message_start", message=message)
    yield emit("ping")
    index = -1
    active = None
    events = relay.stream(request)
    try:
        for event in events:
            if event.kind in {"text", "reasoning"}:
                kind = "text" if event.kind == "text" else "thinking"
                if active != kind:
                    if active is not None:
                        if active == "thinking":
                            yield emit("content_block_delta", index=index,
                                       delta={"type": "signature_delta", "signature": messages.THINKING_SIGNATURE})
                        yield emit("content_block_stop", index=index)
                    index += 1
                    active = kind
                    yield emit("content_block_start", index=index, content_block={"type": kind, kind: ""})
                yield emit("content_block_delta", index=index,
                           delta={"type": "text_delta" if kind == "text" else "thinking_delta", kind: event.text})
            elif event.kind == "ping":
                yield emit("ping")
            elif event.result is not None:
                result = event.result
                if active is not None:
                    if active == "thinking":
                        yield emit("content_block_delta", index=index,
                                   delta={"type": "signature_delta", "signature": messages.THINKING_SIGNATURE})
                    yield emit("content_block_stop", index=index)
                    active = None
                for call in result.action.calls:
                    index += 1
                    yield emit("content_block_start", index=index, content_block={
                        "type": "tool_use", "id": call.id, "name": call.name, "input": {},
                    })
                    yield emit("content_block_delta", index=index, delta={
                        "type": "input_json_delta", "partial_json": json.dumps(call.parameters, ensure_ascii=False, separators=(",", ":")),
                    })
                    yield emit("content_block_stop", index=index)
                usage = result.upstream.usage
                yield emit("message_delta", delta={"stop_reason": "tool_use" if result.action.calls else "end_turn", "stop_sequence": None},
                           usage={"input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                                  "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)})
                yield emit("message_stop")
    except (UpstreamError, ActionTransportError) as exc:
        yield _sse(messages.error(str(exc)), "error")
    finally:
        events.close()


def response_events(body: dict[str, Any], relay: ActionRelay, request: RelayRequest) -> Iterator[str]:
    response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
                "status": "in_progress", "model": request.model, "output": [], "output_text": "",
                "parallel_tool_calls": request.parallel_tool_calls, "tool_choice": body.get("tool_choice") or "auto",
                "tools": body.get("tools") or [], "usage": None}
    sequence = 0

    def emit(kind: str, **data: Any) -> str:
        nonlocal sequence
        value = _sse({"type": kind, "sequence_number": sequence, **data}, kind)
        sequence += 1
        return value

    yield emit("response.created", response=response)
    yield emit("response.in_progress", response=response)
    items: list[dict[str, Any]] = []
    text_items: dict[str, tuple[int, dict[str, Any]]] = {}
    events = relay.stream(request)
    try:
        for event in events:
            if event.kind == "ping":
                yield ": ping\n\n"
            elif event.kind in {"text", "reasoning"}:
                reasoning = event.kind == "reasoning"
                if event.kind not in text_items:
                    item = ({"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": [], "status": "in_progress"}
                            if reasoning else {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                                               "content": [], "status": "in_progress"})
                    index = len(items)
                    items.append(item)
                    text_items[event.kind] = (index, item)
                    yield emit("response.output_item.added", output_index=index, item=dict(item))
                    part = {"type": "summary_text", "text": ""} if reasoning else {"type": "output_text", "text": "", "annotations": []}
                    item["summary" if reasoning else "content"].append(part)
                    key = "summary_index" if reasoning else "content_index"
                    yield emit("response.reasoning_summary_part.added" if reasoning else "response.content_part.added",
                               output_index=index, item_id=item["id"], part=dict(part), **{key: 0})
                index, item = text_items[event.kind]
                item["summary" if reasoning else "content"][0]["text"] += event.text
                yield emit("response.reasoning_summary_text.delta" if reasoning else "response.output_text.delta",
                           output_index=index, item_id=item["id"], delta=event.text,
                           **{"summary_index" if reasoning else "content_index": 0})
            elif event.result is not None:
                result = event.result
                for kind, (index, item) in text_items.items():
                    reasoning = kind == "reasoning"
                    part = item["summary" if reasoning else "content"][0]
                    indices = {"output_index": index, "item_id": item["id"], "summary_index" if reasoning else "content_index": 0}
                    yield emit("response.reasoning_summary_text.done" if reasoning else "response.output_text.done", text=part["text"], **indices)
                    yield emit("response.reasoning_summary_part.done" if reasoning else "response.content_part.done", part=part, **indices)
                    item["status"] = "completed"
                    yield emit("response.output_item.done", output_index=index, item=item)
                for item in responses.build_output(result):
                    if item["type"] not in {"function_call", "custom_tool_call"}:
                        continue
                    index = len(items)
                    custom = item["type"] == "custom_tool_call"
                    field = "input" if custom else "arguments"
                    stem = "response.custom_tool_call_input" if custom else "response.function_call_arguments"
                    yield emit("response.output_item.added", output_index=index,
                               item={**item, field: "", "status": "in_progress"})
                    yield emit(stem + ".delta", output_index=index, item_id=item["id"], delta=item[field])
                    yield emit(stem + ".done", output_index=index, item_id=item["id"], **{field: item[field]})
                    yield emit("response.output_item.done", output_index=index, item=item)
                    items.append(item)
                response.update(status="completed", output=items, output_text=result.action.text, usage=responses._usage(result))
                yield emit("response.completed", response=response)
    except (UpstreamError, ActionTransportError) as exc:
        failure = {"type": "server_error", "code": "upstream_error", "message": str(exc)}
        yield emit("error", code=failure["code"], message=str(exc), param=None)
        response.update(status="failed", output=items, error=failure)
        yield emit("response.failed", response=response)
    finally:
        events.close()
