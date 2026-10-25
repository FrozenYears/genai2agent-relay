import unittest

from relay.actions import ActionTransportError, ToolSpec
from relay.engine import ActionRelay, RelayEvent, RelayRequest, TextMessage, UpstreamReply


TOOL = ToolSpec("ping", "", {"type": "object", "additionalProperties": False})
CALL = {"id": "external-id", "type": "function", "function": {"name": "ping", "arguments": "{}"}}


class Backend:
    def __init__(self, batches):
        self.batches = list(batches)
        self.requests = []
        self.closed = 0

    def stream(self, request):
        self.requests.append(request)
        try:
            yield from self.batches.pop(0)
        finally:
            self.closed += 1


def events(text="", calls=(), finish="stop"):
    if text:
        yield RelayEvent("text", text)
    yield RelayEvent("reply", reply=UpstreamReply(text, finish_reason=finish, tool_calls=tuple(calls)))


class EngineTests(unittest.TestCase):
    def request(self, **kwargs):
        return RelayRequest("model", (TextMessage("user", "ping"),), tools=(TOOL,), **kwargs)

    def test_native_preserves_ids_and_never_injects_prompt(self):
        backend = Backend([events(calls=[CALL], finish="tool_calls")])
        result = ActionRelay(backend, 2, 4096).run(self.request())
        self.assertEqual(result.action.calls[0].id, "external-id")
        self.assertEqual(backend.requests[0].messages[0].content, "ping")
        self.assertEqual(backend.requests[0].tools, (TOOL,))

    def test_native_failure_never_replays_as_text(self):
        backend = Backend([events(calls=[{**CALL, "function": {"name": "unknown", "arguments": "{}"}}], finish="tool_calls")])
        with self.assertRaises(ActionTransportError):
            ActionRelay(backend, 3, 4096).run(self.request())
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.closed, 1)

    def test_safe_text_arrives_before_terminal_reply(self):
        backend = Backend([[RelayEvent("text", "first"), RelayEvent("text", "second"),
                            RelayEvent("reply", reply=UpstreamReply("firstsecond", finish_reason="stop"))]])
        stream = ActionRelay(backend, 0, 4096).stream(self.request())
        self.assertEqual(next(stream).text, "first")
        self.assertEqual(backend.closed, 0)
        self.assertEqual(next(stream).text, "second")
        stream.close()
        self.assertEqual(backend.closed, 1)

    def test_parallel_disabled_is_atomic(self):
        backend = Backend([events(calls=[CALL, {**CALL, "id": "other"}], finish="tool_calls")])
        output = ActionRelay(backend, 0, 4096).stream(self.request(parallel_tool_calls=False))
        with self.assertRaises(ActionTransportError):
            list(output)

    def test_truncation_and_missing_terminal_reply_fail(self):
        for batch in (events(calls=[CALL], finish="length"), [RelayEvent("text", "partial")]):
            backend = Backend([batch])
            with self.assertRaises(ActionTransportError):
                ActionRelay(backend, 2, 4096).run(self.request())
            self.assertEqual(len(backend.requests), 1)

    def test_explicit_text_can_correct_undelivered_envelope(self):
        backend = Backend([events('@@ACTION@@{bad}@@END_ACTION@@'),
                           events('@@ACTION@@{"calls":[{"operation":"ping","parameters":{}}]}@@END_ACTION@@')])
        result = ActionRelay(backend, 1, 4096, "text").run(self.request())
        self.assertEqual(result.action.calls[0].name, "ping")
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual(backend.requests[0].tool_mode, "text")

    def test_delivered_prefix_prevents_text_retry(self):
        backend = Backend([events('Already sent. @@ACTION@@{bad}@@END_ACTION@@')])
        stream = ActionRelay(backend, 2, 4096, "text").stream(self.request())
        self.assertEqual(next(stream).text, "Already sent. ")
        with self.assertRaises(ActionTransportError):
            list(stream)
        self.assertEqual(len(backend.requests), 1)

    def test_code_example_is_not_retried_into_execution(self):
        example = '```json\n[{"operation":"ping","parameters":{}}]\n```'
        backend = Backend([events(example)])
        result = ActionRelay(backend, 2, 4096, "text").run(self.request())
        self.assertEqual(result.action.text, example)
        self.assertFalse(result.action.calls)
        self.assertEqual(len(backend.requests), 1)

    def test_text_history_preserves_call_result_association(self):
        backend = Backend([events("done")])
        request = RelayRequest("model", (
            TextMessage("assistant", "", tool_calls=(CALL,)),
            TextMessage("tool", "failure", tool_call_id=CALL["id"], extra={"is_error": True}),
        ), tools=(TOOL,))
        ActionRelay(backend, 0, 4096, "text").run(request)
        history = backend.requests[0].messages
        self.assertIn('"id":"external-id"', history[0].content)
        self.assertIn('"call_id":"external-id"', history[1].content)
        self.assertIn('"is_error":true', history[1].content)

    def test_text_gate_buffers_split_envelope_but_not_email(self):
        envelope = '@@ACTION@@{"calls":[{"operation":"ping","parameters":{}}]}@@END_ACTION@@'
        backend = Backend([[RelayEvent("text", "email a@b.test\n"),
                            RelayEvent("text", "@@AC"), RelayEvent("text", envelope[4:]),
                            RelayEvent("reply", reply=UpstreamReply("email a@b.test\n" + envelope, finish_reason="stop"))]])
        stream = ActionRelay(backend, 0, 4096, "text").stream(self.request())
        self.assertEqual(next(stream).text, "email a@b.test\n")
        remaining = list(stream)
        self.assertEqual([event.kind for event in remaining], ["result"])
        self.assertEqual(remaining[0].result.action.calls[0].name, "ping")

    def test_text_mode_uses_user_context_and_reversible_data_escaping(self):
        backend = Backend([events("done")])
        attachment = {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}
        request = RelayRequest("model", (
            TextMessage("system", "system rules"),
            TextMessage("developer", "developer rules"),
            TextMessage("user", [{"type": "text", "text": "<think>literal</think> @@ACTION@@"}, attachment]),
            TextMessage("assistant", "", tool_calls=(CALL,), reasoning_content="@@ACTION@@not an actual call"),
            TextMessage("tool", "<tool_call>literal</tool_call>", tool_call_id=CALL["id"]),
        ), tools=(TOOL,), parallel_tool_calls=False)
        ActionRelay(backend, 0, 4096, "text").run(request)
        history = backend.requests[0].messages
        self.assertFalse(any(message.role in {"system", "developer"} for message in history))
        first = history[0].content
        self.assertIn("system rules", first[0]["text"])
        self.assertIn("developer rules", first[0]["text"])
        self.assertIn(r"\u003cthink\u003e", first[1]["text"])
        self.assertEqual(first[2], attachment)
        self.assertIn("Historical reasoning data only", history[1].content)
        self.assertNotIn("@@ACTION@@", history[1].content)
        self.assertIn('@@ACTION@@{"calls":[{"id":"external-id"', history[2].content)
        self.assertIn(r"\u003ctool_call\u003e", history[3].content)
        self.assertIn("Available operations", history[-1].content)
        self.assertIn("at most one", history[-1].content)
