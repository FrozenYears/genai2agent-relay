"""Live HTTP smoke test for all three protocol endpoints.

Starts the real Flask app against a stub text backend, then calls every route
over HTTP. Not a unit test: its purpose is to prove the wiring works end to
end with a real socket and real SSE framing.
"""
from __future__ import annotations

import json
import threading

import requests
from werkzeug.serving import make_server

from relay.app import create_app
from relay.config import RelayConfig
from relay.engine import UpstreamReply

SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}
ACTION = '@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'


class StubBackend:
    def complete(self, request):
        return UpstreamReply(content="Reading. " + ACTION, usage={"prompt_tokens": 7, "completion_tokens": 3})

    def models(self):
        return {"object": "list", "data": [{"id": "chatglm", "object": "model"}]}


def main() -> None:
    app = create_app(RelayConfig(upstream_base_url="http://stub.invalid", upstream_action_retries=0), StubBackend())
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        assert requests.get(base + "/health", timeout=10).json()["status"] == "ok"

        chat = requests.post(base + "/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "Read"}],
            "tools": [{"type": "function", "function": {"name": "read_file", "parameters": SCHEMA}}],
        }, timeout=10)
        assert chat.status_code == 200, chat.text
        name = chat.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        assert name == "read_file"

        message = requests.post(base + "/v1/messages", json={
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Read"}],
            "tools": [{"name": "read_file", "input_schema": SCHEMA}],
        }, timeout=10)
        assert message.status_code == 200, message.text
        block = message.json()["content"][-1]
        assert block["type"] == "tool_use" and block["input"] == {"path": "README.md"}
        assert message.json()["stop_reason"] == "tool_use"

        counted = requests.post(base + "/v1/messages/count_tokens", json={
            "messages": [{"role": "user", "content": "Read"}],
        }, timeout=10)
        assert counted.json()["input_tokens"] > 0

        response = requests.post(base + "/v1/responses", json={
            "input": "Read",
            "tools": [{"type": "function", "name": "read_file", "parameters": SCHEMA}],
        }, timeout=10)
        assert response.status_code == 200, response.text
        call = next(item for item in response.json()["output"] if item["type"] == "function_call")
        assert call["name"] == "read_file"

        tokens = requests.post(base + "/v1/responses/input_tokens", json={"input": "Read"}, timeout=10)
        assert tokens.json()["input_tokens"] > 0

        events = requests.post(base + "/v1/messages", json={
            "stream": True,
            "messages": [{"role": "user", "content": "Read"}],
            "tools": [{"name": "read_file", "input_schema": SCHEMA}],
        }, timeout=10)
        assert "event: message_stop" in events.text
        assert "event: content_block_stop" in events.text

        streamed = requests.post(base + "/v1/responses", json={
            "stream": True,
            "input": "Read",
            "tools": [{"type": "function", "name": "read_file", "parameters": SCHEMA}],
        }, timeout=10)
        assert "event: response.completed" in streamed.text
        assert "event: response.function_call_arguments.done" in streamed.text

        print("smoke ok: chat, messages, count_tokens, responses, input_tokens, both streams")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
