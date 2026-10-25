import json
import unittest

from relay.app import create_app
from relay.config import RelayConfig
from relay.engine import RelayEvent, UpstreamReply
from relay.upstream import UpstreamError


class Backend:
    def __init__(self, calls=(), fail=False):
        self.calls = calls
        self.fail = fail
        self.requests = []
        self.phase = 0
        self.closed = False

    def stream(self, request):
        self.requests.append(request)
        try:
            self.phase = 1
            yield RelayEvent("reasoning", "thinking")
            self.phase = 2
            yield RelayEvent("text", "first ")
            self.phase = 3
            yield RelayEvent("text", "second")
            if self.fail:
                raise UpstreamError("transport stopped")
            self.phase = 4
            yield RelayEvent("reply", reply=UpstreamReply("first second", "thinking", {"prompt_tokens": 2, "completion_tokens": 3},
                                                        "tool_calls" if self.calls else "stop", tuple(self.calls)))
        finally:
            self.closed = True


def data_events(raw):
    events = []
    for line in raw.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    return events


class AppTests(unittest.TestCase):
    def client(self, backend, **kwargs):
        return create_app(RelayConfig("http://upstream.invalid", **kwargs), backend).test_client()

    def payload(self, path, **kwargs):
        return {"model": "model", **({"input": "hello"} if path.endswith("responses") else {"messages": [{"role": "user", "content": "hello"}]}), **kwargs}

    def test_all_protocols_start_before_backend_and_stream_incrementally(self):
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            backend = Backend()
            response = self.client(backend).post(path, json=self.payload(path, stream=True), buffered=False)
            iterator = iter(response.response)
            first = next(iterator).decode()
            self.assertIn("data:", first)
            self.assertEqual(backend.phase, 0)
            collected = first
            while "first " not in collected:
                collected += next(iterator).decode()
            self.assertEqual(backend.phase, 2)
            collected += b"".join(iterator).decode()
            self.assertTrue(backend.closed)
            parsed = data_events(collected)
            if path.endswith("responses"):
                self.assertEqual([event["sequence_number"] for event in parsed], list(range(len(parsed))))
                created = parsed[0]["response"]["id"]
                self.assertEqual(parsed[-1]["response"]["id"], created)
                additions = [event["item"]["id"] for event in parsed if event["type"] == "response.output_item.added"]
                self.assertEqual(additions, [item["id"] for item in parsed[-1]["response"]["output"]])
            elif path.endswith("completions"):
                self.assertEqual(len({event["id"] for event in parsed}), 1)

    def test_stream_failure_has_error_without_success_terminal(self):
        for path, terminal in (("/v1/chat/completions", "[DONE]"), ("/v1/messages", "message_stop"),
                               ("/v1/responses", "response.completed")):
            backend = Backend(fail=True)
            response = self.client(backend).post(path, json=self.payload(path, stream=True))
            raw = response.get_data(as_text=True)
            self.assertIn("transport stopped", raw)
            self.assertNotIn(terminal, raw)
            self.assertEqual(len(backend.requests), 1)
            self.assertTrue(backend.closed)

    def test_disconnect_closes_stream(self):
        backend = Backend()
        response = self.client(backend).post("/v1/chat/completions", json=self.payload("/v1/chat/completions", stream=True), buffered=False)
        iterator = iter(response.response)
        next(iterator)
        next(iterator)
        self.assertEqual(backend.phase, 1)
        response.close()
        self.assertTrue(backend.closed)

    def test_native_external_id_preserved_nonstream(self):
        call = {"id": "external", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
        backend = Backend([call])
        response = self.client(backend).post("/v1/chat/completions", json={
            "messages": [{"role": "developer", "content": "rules"}, {"role": "user", "content": "go"}],
            "tools": [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["choices"][0]["message"]["tool_calls"][0]["id"], "external")
        self.assertEqual(backend.requests[0].messages[0].role, "developer")

    def test_responses_custom_namespace_lifecycle(self):
        backend = Backend([{"id": "custom-id", "type": "function", "function": {"name": "fs.apply", "arguments": '{"input":"patch"}'}}])
        response = self.client(backend).post("/v1/responses", json={
            "input": "apply", "stream": True,
            "tools": [{"type": "namespace", "name": "fs", "tools": [{"type": "custom", "name": "apply", "format": {"type": "text"}}]}],
        })
        parsed = data_events(response.get_data(as_text=True))
        output = parsed[-1]["response"]["output"][-1]
        self.assertEqual(output["type"], "custom_tool_call")
        self.assertEqual(output["call_id"], "custom-id")
        self.assertEqual(output["namespace"], "fs")
        self.assertEqual(output["name"], "apply")
        self.assertEqual(output["input"], "patch")
        self.assertIn("response.custom_tool_call_input.done", [event["type"] for event in parsed])

    def test_unsupported_persistent_state_and_grammar_are_400(self):
        for extra in ({"previous_response_id": "resp_old"}, {"store": True}, {"input": [{"type": "item_reference", "id": "old"}]},
                      {"tools": [{"type": "custom", "name": "x", "format": {"type": "grammar", "definition": "x"}}]},
                      {"tools": [{"type": "computer_use_preview"}]}):
            backend = Backend()
            response = self.client(backend).post("/v1/responses", json={"input": "hello", **extra})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(backend.requests, [])

    def test_authentication_still_applies(self):
        response = self.client(Backend(), relay_api_key="test-key").post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(response.status_code, 401)

    def test_invalid_batch_emits_no_partial_tool_call(self):
        valid = {"id": "valid", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
        invalid = {"id": "invalid", "type": "function", "function": {"name": "unknown", "arguments": "{}"}}
        backend = Backend([valid, invalid])
        response = self.client(backend).post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "go"}], "stream": True,
            "tools": [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}],
        })
        raw = response.get_data(as_text=True)
        self.assertIn('"error"', raw)
        self.assertNotIn('"tool_calls":', raw)
        self.assertNotIn("[DONE]", raw)

    def test_tool_choice_none_is_enforced_without_replay(self):
        backend = Backend([{"id": "external", "type": "function", "function": {"name": "ping", "arguments": "{}"}}])
        response = self.client(backend).post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "go"}], "tool_choice": "none",
            "tools": [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}],
        })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(backend.requests), 1)
