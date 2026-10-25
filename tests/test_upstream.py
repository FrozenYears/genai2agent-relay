import json
import unittest

from relay.actions import ToolSpec
from relay.config import RelayConfig
from relay.engine import TextCompletionRequest, TextMessage
from relay.upstream import UpstreamClient, UpstreamError


def chunk(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def wire(*items):
    return b"".join(("data: " + (item if isinstance(item, str) else json.dumps(item)) + "\n\n").encode() for item in items)


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data
        self.closed = False
        self.chunks_read = 0

    def iter_content(self, chunk_size):
        if chunk_size != 1:
            raise AssertionError("SSE reads must not wait for a multi-byte buffer")
        for offset in range(0, len(self.data), 17):
            self.chunks_read += 1
            yield self.data[offset:offset + 17]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, data):
        self.response = Response(data)
        self.post_call = None

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return self.response


class UpstreamTests(unittest.TestCase):
    def request(self, **kwargs):
        return TextCompletionRequest("model", (TextMessage("system", "rules"), TextMessage("user", "hello")), **kwargs)

    def test_native_payload_and_base_paths(self):
        for base in ("http://first-hop.invalid", "https://api.invalid/v1", "https://api.invalid/api/v1/start"):
            session = Session(wire(chunk({"content": "ok"}), chunk({}, "stop"), "[DONE]"))
            client = UpstreamClient(RelayConfig(base), session)
            request = self.request(tools=(ToolSpec("ping", "", {"type": "object"}),),
                                   tool_choice="required", parallel_tool_calls=False)
            result = list(client.stream(request))
            url, args = session.post_call
            expected = base + ("/v1" if base.endswith(".invalid") else "") + "/chat/completions"
            self.assertEqual(url, expected)
            self.assertTrue(args["stream"])
            self.assertTrue(args["json"]["stream"])
            self.assertEqual(args["json"]["messages"][0]["role"], "system")
            self.assertEqual(args["json"]["tool_choice"], "required")
            self.assertFalse(args["json"]["parallel_tool_calls"])
            self.assertEqual(result[-1].reply.content, "ok")
            self.assertTrue(session.response.closed)

    def test_out_of_order_interleaved_indices_preserve_external_ids(self):
        data = wire(
            chunk({"tool_calls": [{"index": 1, "id": "second", "function": {"name": "ping", "arguments": "{"}}]}),
            chunk({"tool_calls": [{"index": 0, "id": "first", "function": {"name": "ping", "arguments": "{}"}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"arguments": "}"}}]}),
            chunk({}, "tool_calls"), "[DONE]",
        )
        session = Session(data)
        result = list(UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request()))[-1].reply
        self.assertEqual([call["id"] for call in result.tool_calls], ["first", "second"])
        self.assertEqual(result.tool_calls[1]["function"]["arguments"], "{}")

    def test_eof_missing_finish_or_done_is_not_success(self):
        for data in (wire(chunk({"content": "partial"})), wire(chunk({}, "stop")), wire("[DONE]")):
            session = Session(data)
            with self.assertRaises(UpstreamError):
                list(UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request()))
            self.assertTrue(session.response.closed)

    def test_cancel_closes_http_response_before_consuming_remainder(self):
        session = Session(wire(chunk({"content": "first"}), chunk({"content": "second"}), chunk({}, "stop"), "[DONE]"))
        stream = UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request())
        self.assertEqual(next(stream).text, "first")
        read = session.response.chunks_read
        stream.close()
        self.assertTrue(session.response.closed)
        self.assertEqual(read, session.response.chunks_read)

    def test_limit_is_enforced_during_consumption(self):
        session = Session(b"data: " + b"x" * 8192)
        client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=1024), session)
        with self.assertRaises(UpstreamError):
            list(client.stream(self.request()))
        self.assertLess(session.response.chunks_read * 17, 2048)
        self.assertTrue(session.response.closed)

    def test_text_mode_does_not_send_native_tool_fields(self):
        session = Session(wire(chunk({"content": "text"}), chunk({}, "stop"), "[DONE]"))
        list(UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request(tool_mode="text")))
        self.assertNotIn("tools", session.post_call[1]["json"])
        self.assertNotIn("tool_choice", session.post_call[1]["json"])

    def test_many_tiny_argument_deltas_do_not_charge_repeated_frame_metadata(self):
        arguments = json.dumps({"text": "x" * 33000}, separators=(",", ":"))
        frames = [chunk({"tool_calls": [{"index": 0, "id": "external", "function": {"name": "write", "arguments": ""}}]})]
        frames.extend(chunk({"tool_calls": [{"index": 0, "function": {"arguments": char}}]}) for char in arguments)
        frames.extend((chunk({}, "tool_calls"), "[DONE]"))
        data = wire(*frames)
        self.assertGreater(len(data), 1_048_576)
        session = Session(data)
        client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=65536), session)
        result = list(client.stream(self.request()))[-1].reply
        self.assertEqual(result.tool_calls[0]["function"]["arguments"], arguments)
        self.assertTrue(session.response.closed)

    def test_payload_budget_combines_content_reasoning_and_tool_fields(self):
        session = Session(wire(
            chunk({"content": "x" * 400}),
            chunk({"reasoning_content": "y" * 400}),
            chunk({"tool_calls": [{"index": 0, "id": "external", "function": {"name": "write", "arguments": "z" * 300}}]}),
            chunk({}, "tool_calls"), "[DONE]",
        ))
        client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=1024), session)
        with self.assertRaisesRegex(UpstreamError, "payload exceeds"):
            list(client.stream(self.request()))
        self.assertTrue(session.response.closed)

    def test_unclosed_multiline_event_is_bounded(self):
        session = Session(b"data: x\n" * 1024)
        client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=1024), session)
        with self.assertRaisesRegex(UpstreamError, "SSE event exceeds"):
            list(client.stream(self.request()))
        self.assertLess(session.response.chunks_read * 17, 2048)

    def test_many_heartbeats_do_not_exhaust_payload_budget(self):
        session = Session(b": ping\n\n" * 1000 + wire(chunk({"content": "ok"}), chunk({}, "stop"), "[DONE]"))
        client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=1024), session)
        result = list(client.stream(self.request()))
        self.assertEqual(sum(event.kind == "ping" for event in result), 1000)
        self.assertEqual(result[-1].reply.content, "ok")
