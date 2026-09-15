from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from typing import Any, Iterable

from flask import Flask, Response, jsonify, request, stream_with_context

from .actions import ActionTransportError
from .config import RelayConfig
from .engine import RelayResult, TextActionRelay, TextCompletionBackend, UpstreamReply
from .normalize import chat_tools, normalize_choice, prepare_chat
from .upstream import UpstreamClient, UpstreamError

logger = logging.getLogger(__name__)


def _request_key() -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return request.headers.get("x-api-key", "")


def _error(message: str, status: int = 502):
    return jsonify({"error": {"type": "api_error", "message": message}}), status


def _usage(reply: UpstreamReply) -> dict[str, int]:
    usage = reply.usage
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _chat_response(body: dict[str, Any], result: RelayResult, stream: bool):
    decoded = result.action
    reply = result.upstream
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    tool_calls = [
        {
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {
                "name": call.name,
                "arguments": json.dumps(call.parameters, ensure_ascii=False, separators=(",", ":")),
            },
        }
        for call in decoded.calls
    ]
    finish_reason = "tool_calls" if tool_calls else "stop"
    message: dict[str, Any] = {"role": "assistant", "content": decoded.text or None}
    if reply.reasoning:
        message["reasoning_content"] = reply.reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = _usage(reply)
    if not stream:
        return jsonify({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": body.get("model") or "chatglm",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        })

    def generate() -> Iterable[str]:
        base = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": body.get("model") or "chatglm",
        }
        yield "data: " + json.dumps({
            **base,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }, ensure_ascii=False) + "\n\n"
        delta: dict[str, Any] = {}
        if decoded.text:
            delta["content"] = decoded.text
        if reply.reasoning:
            delta["reasoning_content"] = reply.reasoning
        if tool_calls:
            delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(tool_calls)]
        if delta:
            yield "data: " + json.dumps({
                **base,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }, ensure_ascii=False) + "\n\n"
        yield "data: " + json.dumps({
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": usage,
        }, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def create_app(
    config: RelayConfig | None = None,
    upstream_client: TextCompletionBackend | None = None,
) -> Flask:
    config = config or RelayConfig.from_env()
    backend = upstream_client or UpstreamClient(config)
    action_relay = TextActionRelay(backend, config.upstream_action_retries, config.max_action_bytes)
    app = Flask(__name__)
    app.config["RELAY_CONFIG"] = config

    @app.before_request
    def authenticate():
        if request.path == "/health" or not config.relay_api_key:
            return None
        if not hmac.compare_digest(_request_key(), config.relay_api_key):
            return _error("Invalid or missing relay API key", 401)
        return None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "upstream": config.upstream_base_url})

    @app.get("/models")
    @app.get("/v1/models")
    def models():
        if not hasattr(backend, "models"):
            return _error("The configured backend does not expose a model list", 501)
        try:
            return jsonify(backend.models())
        except UpstreamError as exc:
            return _error(str(exc))

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    def chat_completions():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _error("Request body must be a JSON object", 400)
        if not isinstance(body.get("messages"), list):
            return _error("Missing messages array", 400)
        try:
            tools = chat_tools(body.get("tools"))
            choice = normalize_choice(body.get("tool_choice"))
            relay_request = prepare_chat(body, tools, choice)
        except (TypeError, ValueError) as exc:
            return _error(str(exc), 400)
        try:
            result = action_relay.run(relay_request)
            return _chat_response(body, result, bool(body.get("stream")))
        except (UpstreamError, ActionTransportError) as exc:
            logger.warning("Chat request failed model=%s error=%s", body.get("model"), type(exc).__name__)
            return _error(str(exc))

    return app
