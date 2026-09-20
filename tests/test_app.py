import json
import unittest

from relay.app import create_app
from relay.config import RelayConfig
from relay.engine import TextCompletionRequest, UpstreamReply


class FakeUpstream:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[TextCompletionRequest] = []

    def complete(self, request):
        self.requests.append(request)
        return self.replies.pop(0)

    def models(self):
        return {"object": "list", "data": [{"id": "chatglm", "object": "model"}]}


def reply(content):
    return UpstreamReply(content=content, usage={"prompt_tokens": 10, "completion_tokens": 5})


SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}

TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": SCHEMA,
    },
}

ANTHROPIC_TOOL = {
    "name": "read_file",
    "description": "Read a file",
    "input_schema": SCHEMA,
}

RESPONSES_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file",
    "parameters": SCHEMA,
}


class AppTests(unittest.TestCase):
    def config(self, **overrides):
        values = dict(upstream_base_url="http://upstream.invalid", upstream_action_retries=0)
        values.update(overrides)
        return RelayConfig(**values)

    def test_tool_request_is_converted_and_reconstructed(self):
        upstream = FakeUpstream([reply(
            'Checking. @@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        )])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/chat/completions", json={
            "model": "chatglm",
            "messages": [
                {"role": "system", "content": "Follow project rules"},
                {"role": "user", "content": "Read the file"},
            ],
            "tools": [TOOL],
        })

        self.assertEqual(response.status_code, 200)
        choice = response.get_json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(
            json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]),
            {"path": "README.md"},
        )

        sent = upstream.requests[0]
        self.assertFalse(hasattr(sent, "tools"))
        self.assertTrue(all(message.role != "system" for message in sent.messages))
        self.assertIn("Follow project rules", sent.messages[0].content)
        self.assertIn("@@ACTION@@", sent.messages[0].content)

    def test_plain_text_passes_through(self):
        upstream = FakeUpstream([reply("hello")])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/chat/completions", json={
            "model": "chatglm",
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["choices"][0]["message"]["content"], "hello")

    def test_invalid_action_is_retried_before_returning(self):
        upstream = FakeUpstream([
            reply('@@ACTION@@{"calls":[{"operation":"read_file","parameters":{}}]}@@END_ACTION@@'),
            reply('@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'),
        ])
        client = create_app(self.config(upstream_action_retries=1), upstream).test_client()
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "Read the file"}],
            "tools": [TOOL],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(upstream.requests), 2)
        self.assertIn("nothing was executed", upstream.requests[1].messages[-1].content)

    def test_tool_result_history_is_text_encoded(self):
        upstream = FakeUpstream([reply("done")])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/chat/completions", json={
            "messages": [
                {"role": "assistant", "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
            ],
            "tools": [TOOL],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("@@ACTION@@", upstream.requests[0].messages[0].content)
        self.assertIn("@@RESULT@@", upstream.requests[0].messages[1].content)

    def test_buffered_stream_reconstructs_tool_call(self):
        action = '@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        upstream = FakeUpstream([reply(action)])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/chat/completions", json={
            "stream": True,
            "messages": [{"role": "user", "content": "Read"}],
            "tools": [TOOL],
        })
        self.assertIn(b'"tool_calls"', response.data)
        self.assertIn(b'"finish_reason": "tool_calls"', response.data)
        self.assertTrue(response.data.endswith(b"data: [DONE]\n\n"))

    def test_auth_and_models(self):
        upstream = FakeUpstream([])
        client = create_app(self.config(relay_api_key="test-only-key"), upstream).test_client()
        self.assertEqual(client.get("/v1/models").status_code, 401)
        headers = {"Authorization": "Bearer test-only-key"}
        self.assertEqual(client.get("/v1/models", headers=headers).status_code, 200)

    def test_all_three_protocol_routes_are_exposed(self):
        action = '@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        client = create_app(self.config(), FakeUpstream([reply(action), reply(action), reply(action)])).test_client()
        self.assertEqual(client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "Read"}], "tools": [TOOL],
        }).status_code, 200)
        self.assertEqual(client.post("/v1/messages", json={
            "messages": [{"role": "user", "content": "Read"}], "tools": [ANTHROPIC_TOOL],
        }).status_code, 200)
        self.assertEqual(client.post("/v1/responses", json={
            "input": "Read", "tools": [RESPONSES_TOOL],
        }).status_code, 200)

    def test_messages_request_is_converted_and_reconstructed(self):
        upstream = FakeUpstream([reply(
            'Reading. @@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        )])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/messages", json={
            "model": "chatglm",
            "max_tokens": 512,
            "system": [{"type": "text", "text": "Follow project rules"}],
            "messages": [{"role": "user", "content": "Read the file"}],
            "tools": [ANTHROPIC_TOOL],
        })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["stop_reason"], "tool_use")
        block = payload["content"][-1]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["name"], "read_file")
        self.assertEqual(block["input"], {"path": "README.md"})

        sent = upstream.requests[0]
        self.assertFalse(hasattr(sent, "tools"))
        self.assertTrue(all(message.role != "system" for message in sent.messages))
        self.assertIn("Follow project rules", sent.messages[0].content)
        self.assertIn("@@ACTION@@", sent.messages[0].content)

    def test_messages_history_and_streaming(self):
        action = '@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        upstream = FakeUpstream([reply(action)])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/messages", json={
            "stream": True,
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "contents"},
                ]},
            ],
            "tools": [ANTHROPIC_TOOL],
        })
        self.assertIn(b"event: content_block_delta", response.data)
        self.assertIn(b"event: message_stop", response.data)
        self.assertIn("@@ACTION@@", upstream.requests[0].messages[0].content)
        self.assertIn("@@RESULT@@", upstream.requests[0].messages[1].content)

    def test_messages_count_tokens(self):
        client = create_app(self.config(), FakeUpstream([])).test_client()
        response = client.post("/v1/messages/count_tokens", json={
            "messages": [{"role": "user", "content": "hello"}], "tools": [ANTHROPIC_TOOL],
        })
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.get_json()["input_tokens"], 0)

    def test_responses_request_is_converted_and_reconstructed(self):
        upstream = FakeUpstream([reply(
            'Reading. @@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        )])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/responses", json={
            "model": "chatglm",
            "instructions": "Follow project rules",
            "input": "Read the file",
            "tools": [RESPONSES_TOOL],
        })

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")
        call = next(item for item in payload["output"] if item["type"] == "function_call")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"]), {"path": "README.md"})
        self.assertIn("Reading.", payload["output_text"])

        sent = upstream.requests[0]
        self.assertFalse(hasattr(sent, "tools"))
        self.assertIn("Follow project rules", sent.messages[0].content)

    def test_responses_history_streaming_and_input_tokens(self):
        action = '@@ACTION@@{"calls":[{"operation":"read_file","parameters":{"path":"README.md"}}]}@@END_ACTION@@'
        upstream = FakeUpstream([reply(action)])
        client = create_app(self.config(), upstream).test_client()
        response = client.post("/v1/responses", json={
            "stream": True,
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"README.md"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "contents"},
            ],
            "tools": [RESPONSES_TOOL],
        })
        self.assertIn(b"event: response.function_call_arguments.delta", response.data)
        self.assertIn(b"event: response.completed", response.data)
        self.assertIn("@@ACTION@@", upstream.requests[0].messages[0].content)
        self.assertIn("@@RESULT@@", upstream.requests[0].messages[1].content)

        counted = client.post("/v1/responses/input_tokens", json={
            "input": "hello", "tools": [RESPONSES_TOOL],
        })
        self.assertEqual(counted.status_code, 200)
        self.assertGreater(counted.get_json()["input_tokens"], 0)

    def test_invalid_request_and_tool_schema_return_400(self):
        client = create_app(self.config(), FakeUpstream([])).test_client()
        self.assertEqual(client.post("/v1/chat/completions", json=[]).status_code, 400)
        response = client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "bad",
                    "parameters": {"type": "not-a-json-schema-type"},
                },
            }],
        })
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
