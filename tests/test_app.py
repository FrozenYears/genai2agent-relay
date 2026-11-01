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


class ToolBatchBackend(Backend):
    def stream(self, request):
        self.requests.append(request)
        try:
            self.phase = 1
            yield RelayEvent("reasoning", "thinking")
            self.phase = 2
            yield RelayEvent("text", "first")
            content = "first"
            if request.tool_mode == "text":
                envelope = "@@ACTION@@" + json.dumps({"calls": [
                    {"id": call["id"], "operation": call["function"]["name"],
                     "parameters": json.loads(call["function"]["arguments"])}
                    for call in self.calls
                ]}, ensure_ascii=False) + "@@END_ACTION@@"
                self.phase = 3
                yield RelayEvent("text", envelope)
                content += envelope
            self.phase = 4
            yield RelayEvent("reply", reply=UpstreamReply(
                content, "thinking", finish_reason="tool_calls" if request.tool_mode == "native" else "stop",
                tool_calls=tuple(self.calls) if request.tool_mode == "native" else (),
            ))
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

    def history_payload(self, path, calls, stream):
        schema = {"type": "object"}
        history = []
        for call in calls:
            function = call["function"]
            if path.endswith("completions"):
                history.extend([
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": call["id"], "content": "done"},
                ])
            elif path.endswith("messages"):
                history.extend([
                    {"role": "assistant", "content": [{"type": "tool_use", "id": call["id"],
                     "name": function["name"], "input": json.loads(function["arguments"])}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"], "content": "done"}]},
                ])
            else:
                history.extend([
                    {"type": "function_call", "call_id": call["id"], **function},
                    {"type": "function_call_output", "call_id": call["id"], "output": "done"},
                ])
        history.append({"role": "user", "content": "continue"})
        if path.endswith("completions"):
            tool = {"type": "function", "function": {"name": "ping", "parameters": schema}}
        elif path.endswith("messages"):
            tool = {"name": "ping", "input_schema": schema}
        else:
            tool = {"type": "function", "name": "ping", "parameters": schema}
        return {"model": "model", "stream": stream, "tools": [tool],
                "input" if path.endswith("responses") else "messages": history}

    def consumer_calls(self, path, response, stream):
        parsed = data_events(response.get_data(as_text=True)) if stream else None
        if path.endswith("completions"):
            calls = ([call for event in parsed for choice in event.get("choices", [])
                      for call in choice["delta"].get("tool_calls", [])] if stream
                     else response.json["choices"][0]["message"].get("tool_calls", []))
            return [{key: value for key, value in call.items() if key != "index"} for call in calls]
        if path.endswith("messages"):
            if stream:
                blocks = {}
                arguments = {}
                for event in parsed:
                    if event["type"] == "content_block_start" and event["content_block"]["type"] == "tool_use":
                        blocks[event["index"]] = event["content_block"]
                        arguments[event["index"]] = ""
                    elif event["type"] == "content_block_delta" and event["delta"]["type"] == "input_json_delta":
                        arguments[event["index"]] += event["delta"]["partial_json"]
                calls = [{**block, "input": json.loads(arguments[index])} for index, block in blocks.items()]
            else:
                calls = [block for block in response.json["content"] if block["type"] == "tool_use"]
            return [{"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": json.dumps(call["input"], ensure_ascii=False),
            }} for call in calls]
        if stream:
            calls = [item for item in parsed[-1]["response"]["output"] if item["type"] == "function_call"]
            for event_type in ("response.output_item.added", "response.output_item.done"):
                items = [event["item"] for event in parsed if event["type"] == event_type
                         and event["item"]["type"] == "function_call"]
                self.assertEqual([(item["id"], item["call_id"]) for item in items],
                                 [(item["id"], item["call_id"]) for item in calls])
        else:
            calls = [item for item in response.json["output"] if item["type"] == "function_call"]
        return [{"id": call["call_id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"]}} for call in calls]

    def test_colliding_ids_round_trip_every_protocol_and_mode(self):
        old_ids = ["call_1", "call_2845a4e20c8d4ea889a2acd9"]
        arguments = [{"path": "旧文件"}, {"input": "line\n\\path\""}, {"value": [1, True, None]}]
        batch = [{"id": call_id, "type": "function", "function": {
            "name": "ping", "arguments": json.dumps(parameters, ensure_ascii=False),
        }} for call_id, parameters in zip([*old_ids, "keep-this-id"], arguments)]
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            for mode in ("native", "text"):
                for stream in (False, True):
                    with self.subTest(path=path, mode=mode, stream=stream):
                        history = list(batch[:2])
                        backend = ToolBatchBackend(batch)
                        client = self.client(backend, upstream_tool_mode=mode)
                        for round_index in range(2):
                            response = client.post(path, json=self.history_payload(path, history, stream), buffered=False)
                            self.assertEqual(response.status_code, 200)
                            if stream:
                                iterator = iter(response.response)
                                prefix = ""
                                while "first" not in prefix:
                                    prefix += next(iterator).decode()
                                self.assertEqual(backend.phase, 2)
                                self.assertIn("thinking", prefix)
                                response.set_data(prefix.encode() + b"".join(iterator))
                            calls = self.consumer_calls(path, response, stream)
                            ids = [call["id"] for call in calls]
                            self.assertEqual(len(ids), 3)
                            self.assertEqual(len(set(ids)), 3)
                            self.assertTrue(set(ids).isdisjoint(call["id"] for call in history))
                            if round_index == 0:
                                self.assertEqual(ids[2], "keep-this-id")
                            self.assertEqual([call["function"]["name"] for call in calls], ["ping"] * 3)
                            self.assertEqual([json.loads(call["function"]["arguments"]) for call in calls], arguments)
                            history.extend(calls)
                        self.assertEqual(len(backend.requests), 2)

    def test_duplicate_batch_ids_are_not_repaired_or_partially_delivered(self):
        call = {"id": "call_1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            for mode in ("native", "text"):
                for stream in (False, True):
                    with self.subTest(path=path, mode=mode, stream=stream):
                        backend = ToolBatchBackend([call, call])
                        response = self.client(backend, upstream_tool_mode=mode).post(
                            path, json=self.history_payload(path, [call], stream),
                        )
                        if stream:
                            parsed = data_events(response.get_data(as_text=True))
                            self.assertTrue(any("error" in event or event.get("type") == "error" for event in parsed))
                            self.assertFalse(any(choice["delta"].get("tool_calls") for event in parsed
                                                 for choice in event.get("choices", [])))
                            self.assertFalse(any(event.get("content_block", {}).get("type") == "tool_use" for event in parsed))
                            self.assertFalse(any(event.get("item", {}).get("type") == "function_call" for event in parsed))
                            self.assertNotIn("message_stop", [event.get("type") for event in parsed])
                            self.assertNotIn("response.completed", [event.get("type") for event in parsed])
                            self.assertNotIn("[DONE]", response.get_data(as_text=True))
                        else:
                            self.assertEqual(response.status_code, 502)
                        self.assertEqual(len(backend.requests), 1)

    def test_already_duplicate_history_is_rejected_before_upstream(self):
        call = {"id": "call_1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    backend = ToolBatchBackend([call])
                    response = self.client(backend).post(
                        path, json=self.history_payload(path, [call, call], stream),
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("Duplicate historical tool call ID", response.get_data(as_text=True))
                    self.assertEqual(backend.requests, [])

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
