import json
import unittest

from relay.actions import ToolSpec
from relay.app import create_app
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
        self.get_call = None
        self.call_count = 0

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        self.call_count += 1
        return self.response

    def get(self, url, **kwargs):
        self.get_call = (url, kwargs)
        self.call_count += 1
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

    def test_structured_stream_error_keeps_diagnosis_and_closes_without_reply(self):
        for code in ("upstream_empty_response", "tool_call_parse_error", "upstream_stream_incomplete", None):
            with self.subTest(code=code):
                session = Session(wire({"error": {
                    "message": "Web stream ended without assistant content", "type": "upstream_error",
                    "code": code, "trace": "private-trace", "headers": {"Cookie": "private-cookie"},
                }}, chunk({"content": "must not become success"}), chunk({}, "stop"), "[DONE]"))
                stream = UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request())
                with self.assertRaises(UpstreamError) as caught:
                    list(stream)
                self.assertEqual(caught.exception.error, {
                    "message": "Web stream ended without assistant content", "type": "upstream_error", "code": code,
                })
                self.assertTrue(session.response.closed)
                self.assertEqual(session.call_count, 1)

    def test_http_error_reads_only_bounded_structured_fields(self):
        for endpoint in ("stream", "models"):
            session = Session(json.dumps({"error": {
                "message": "Browser stream was incomplete", "type": "upstream_error", "code": "upstream_stream_incomplete",
                "body": "private-body", "trace": "private-trace", "headers": {"Authorization": "private-header"},
            }, "secret": "private-top-level"}).encode())
            session.response.status_code = 503
            client = UpstreamClient(RelayConfig("http://upstream.invalid"), session)
            with self.subTest(endpoint=endpoint), self.assertRaises(UpstreamError) as caught:
                list(client.stream(self.request())) if endpoint == "stream" else client.models()
            self.assertEqual(caught.exception.error, {
                "message": "Browser stream was incomplete", "type": "upstream_error", "code": "upstream_stream_incomplete",
            })
            self.assertTrue(session.response.closed)
            self.assertTrue((session.post_call or session.get_call)[1]["stream"])

    def test_unknown_http_bodies_are_not_reflected_and_oversize_reads_stop(self):
        cases = [
            b"<html>private-html</html>", b'{"message":"private-unstructured"}',
            b'{"error":"private-string"}', b'{"error":{"message":{"secret":"private-object"}}}',
            b'{"error":{"message":"private-duplicate","message":"private-duplicate"}}',
            b'{"error":{"message":"private-invalid-utf8\\xff"}}',
            b'{"error":{"message":"' + b"private-oversized" * 2048 + b'"}}',
        ]
        for data in cases:
            session = Session(data)
            session.response.status_code = 502
            client = UpstreamClient(RelayConfig("http://upstream.invalid", max_action_bytes=1024), session)
            with self.subTest(data_length=len(data)), self.assertRaises(UpstreamError) as caught:
                list(client.stream(self.request()))
            self.assertEqual(str(caught.exception), "First-hop service returned HTTP 502")
            self.assertLessEqual(session.response.chunks_read * 17, 1040)
            self.assertTrue(session.response.closed)

    def test_error_message_is_bounded_and_credentials_are_redacted(self):
        secrets = ["test-upstream-key", "test-relay-key", "short-bearer", "short-basic", "private-cookie",
                   "short-token", "sk-test-secret", "aGVsbG93b3JsZHNlY3JldC10ZXN0LWJhc2U2NC1wYXlsb2Fk"]
        message = (
            f"Empty browser stream; {secrets[0]}; {secrets[1]}; Bearer {secrets[2]}; Basic {secrets[3]}; "
            f'Cookie: "{secrets[4]}"; access_token="{secrets[5]}"; {secrets[6]}; {secrets[7]}; ' + "诊断" * 600
        )
        session = Session(wire({"error": {"message": message, "type": "upstream_error", "code": None}}))
        client = UpstreamClient(RelayConfig("http://upstream.invalid", upstream_api_key=secrets[0], relay_api_key=secrets[1]), session)
        with self.assertRaises(UpstreamError) as caught:
            list(client.stream(self.request()))
        self.assertIn("Empty browser stream", str(caught.exception))
        self.assertLessEqual(len(str(caught.exception)), 512)
        for secret in secrets:
            self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.error["code"])

    def test_bad_error_identifiers_and_malformed_sse_do_not_expose_credentials(self):
        for data in (
            wire({"error": {"message": "Empty response", "type": "Bearer type-secret", "code": "sk-code-secret"}}),
            b'data: {"short-known-secret":1,"short-known-secret":2}\n\n',
        ):
            session = Session(data)
            client = UpstreamClient(RelayConfig("http://upstream.invalid", upstream_api_key="short-known-secret"), session)
            with self.assertRaises(UpstreamError) as caught:
                list(client.stream(self.request()))
            self.assertNotIn("type-secret", json.dumps(caught.exception.error))
            self.assertNotIn("code-secret", json.dumps(caught.exception.error))
            self.assertNotIn("short-known-secret", json.dumps(caught.exception.error))

    def test_all_consumers_keep_safe_first_hop_error_in_both_delivery_modes(self):
        paths = ("/v1/chat/completions", "/v1/messages", "/v1/responses")
        for path in paths:
            for streaming in (False, True):
                for status in (200, 503):
                    for code in ("upstream_empty_response", "tool_call_parse_error", "upstream_stream_incomplete", None):
                        error = {"message": "Web SSE exhausted without content; Bearer fake-bearer; plain-test-key",
                                 "type": "upstream_error", "code": code,
                                 "trace": "private-trace", "headers": {"Cookie": "private-cookie"},
                                 "body": "private-body"}
                        packet = {"error": error, "credentials": "private-credentials",
                                  "choices": [{"delta": {"content": "must-not-become-content"}}]}
                        data = (wire(packet, chunk({"content": "must-not-become-content"}), chunk({}, "stop"), "[DONE]")
                                if status == 200 else json.dumps(packet).encode())
                        session = Session(data)
                        session.response.status_code = status
                        config = RelayConfig("http://upstream.invalid", upstream_api_key="plain-test-key")
                        client = create_app(config, UpstreamClient(config, session)).test_client()
                        payload = {"input": "hello"} if path.endswith("responses") else {"messages": [{"role": "user", "content": "hello"}]}
                        with self.subTest(path=path, stream=streaming, status=status, code=code):
                            response = client.post(path, json={**payload, "stream": streaming})
                            raw = response.get_data(as_text=True)
                            self.assertEqual(response.status_code, 200 if streaming else 502)
                            if streaming:
                                events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
                                failure = events[-1]["response"]["error"] if path.endswith("responses") else events[-1]["error"]
                                self.assertNotIn("[DONE]", raw)
                                self.assertNotIn("message_stop", raw)
                                self.assertNotIn("response.completed", raw)
                                self.assertNotIn('"finish_reason": "stop"', raw)
                                if path.endswith("responses"):
                                    self.assertEqual(events[-1]["type"], "response.failed")
                                    self.assertEqual(events[-2]["code"], code)
                                    self.assertEqual(events[-2]["message"], failure["message"])
                            else:
                                failure = response.json["error"]
                            self.assertEqual(set(failure), {"message", "type", "code"})
                            self.assertTrue(failure["message"].startswith("Web SSE exhausted without content"))
                            self.assertEqual(failure["type"], "upstream_error")
                            self.assertEqual(failure["code"], code)
                            for private in ("fake-bearer", "plain-test-key", "private-trace", "private-cookie",
                                            "private-body", "private-credentials", "must-not-become-content"):
                                self.assertNotIn(private, raw)
                            self.assertEqual(session.call_count, 1)
                            self.assertTrue(session.response.closed)

    def test_all_consumers_do_not_turn_invalid_http_body_into_content(self):
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            for streaming in (False, True):
                for data in (b"<html>private-body</html>", b'{"message":"private-body"}',
                             b'{"error":{"message":"' + b"private-body" * 2048 + b'"}}',
                             b"[" * 1200 + b"]" * 1200):
                    session = Session(data)
                    session.response.status_code = 502
                    config = RelayConfig("http://upstream.invalid")
                    client = create_app(config, UpstreamClient(config, session)).test_client()
                    payload = {"input": "hello"} if path.endswith("responses") else {"messages": []}
                    with self.subTest(path=path, stream=streaming, length=len(data)):
                        response = client.post(path, json={**payload, "stream": streaming})
                        raw = response.get_data(as_text=True)
                        self.assertIn("First-hop service returned HTTP 502", raw)
                        self.assertNotIn("private-body", raw)
                        if not streaming:
                            self.assertNotIn('"choices":', raw)
                        self.assertNotIn("[DONE]", raw)
                        self.assertNotIn("message_stop", raw)
                        self.assertNotIn("response.completed", raw)
                        self.assertTrue(session.response.closed)
                        self.assertEqual(session.call_count, 1)

    def test_all_consumers_disconnect_closes_first_hop_without_reading_remainder(self):
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            session = Session(wire(chunk({"content": "first-visible"}), chunk({"content": "unread-remainder"}), chunk({}, "stop"), "[DONE]"))
            config = RelayConfig("http://upstream.invalid")
            client = create_app(config, UpstreamClient(config, session)).test_client()
            payload = {"input": "hello"} if path.endswith("responses") else {"messages": []}
            with self.subTest(path=path):
                response = client.post(path, json={**payload, "stream": True}, buffered=False)
                iterator = iter(response.response)
                collected = ""
                while "first-visible" not in collected:
                    collected += next(iterator).decode()
                chunks_read = session.response.chunks_read
                response.close()
                self.assertTrue(session.response.closed)
                self.assertEqual(session.response.chunks_read, chunks_read)

    def test_all_consumers_report_error_after_partial_content_without_success(self):
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            session = Session(wire(
                chunk({"content": "partial-visible"}), chunk({}, "stop"),
                {"error": {"message": "Web terminal was incomplete", "type": "upstream_error", "code": "upstream_stream_incomplete"}},
                "[DONE]",
            ))
            config = RelayConfig("http://upstream.invalid")
            client = create_app(config, UpstreamClient(config, session)).test_client()
            payload = {"input": "hello"} if path.endswith("responses") else {"messages": []}
            with self.subTest(path=path):
                response = client.post(path, json={**payload, "stream": True})
                raw = response.get_data(as_text=True)
                self.assertIn("partial-visible", raw)
                self.assertIn("Web terminal was incomplete", raw)
                self.assertIn("upstream_stream_incomplete", raw)
                self.assertNotIn("[DONE]", raw)
                self.assertNotIn("message_stop", raw)
                self.assertNotIn("response.completed", raw)
                self.assertNotIn('"finish_reason": "stop"', raw)
                self.assertTrue(session.response.closed)
                self.assertEqual(session.call_count, 1)

    def test_cookie_lists_jwt_and_url_credentials_are_not_forwarded(self):
        for message, secrets in (
            ("Empty reply; Cookie: first=short-one, second=short-two", ("short-one", "short-two")),
            ("Empty reply; Bearer 'quoted-short'", ("quoted-short",)),
            ("Empty reply; eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature", ("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "signature")),
            ("Empty reply; https://user:short-password@example.invalid/v1", ("user:short-password",)),
        ):
            session = Session(wire({"error": {"message": message, "type": "upstream_error", "code": None}}))
            with self.subTest(message=message), self.assertRaises(UpstreamError) as caught:
                list(UpstreamClient(RelayConfig("http://upstream.invalid"), session).stream(self.request()))
            self.assertIn("Empty reply", str(caught.exception))
            for secret in secrets:
                self.assertNotIn(secret, str(caught.exception))
