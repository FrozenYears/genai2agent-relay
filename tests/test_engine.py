import json
import unittest

from relay.actions import ActionTransportError, ToolSpec
from relay.engine import RelayRequest, TextActionRelay, TextMessage, UpstreamReply


class Backend:
    def __init__(self, content):
        self.content = content

    def complete(self, request):
        return UpstreamReply(content=self.content)


class EngineTests(unittest.TestCase):
    def test_engine_has_no_http_protocol_dependency(self):
        tools = (ToolSpec(name="ping", description="Ping", parameters={"type": "object"}),)
        request = RelayRequest(
            model="model",
            messages=(TextMessage(role="user", content="ping"),),
            tools=tools,
        )
        relay = TextActionRelay(
            Backend('@@ACTION@@{"calls":[{"operation":"ping","parameters":{}}]}@@END_ACTION@@'),
            retries=0,
            max_action_bytes=4096,
        )
        self.assertEqual(relay.run(request).action.calls[0].name, "ping")

    def test_required_choice_is_enforced(self):
        request = RelayRequest(
            model="model",
            messages=(TextMessage(role="user", content="ping"),),
            tool_choice="required",
        )
        relay = TextActionRelay(Backend("No call"), retries=0, max_action_bytes=4096)
        with self.assertRaises(ActionTransportError):
            relay.run(request)

    def test_json_retry_locates_outer_envelope_and_can_recover(self):
        bad_body = '{"calls":[{"operation":"ping","parameters":{"text":"@@ACTION@@"},{"operation":"ping","parameters":{}}]}'
        bad = '@@ACTION@@' + bad_body + '@@END_ACTION@@'
        good = '@@ACTION@@{"calls":[{"operation":"ping","parameters":{}}]}@@END_ACTION@@'
        try:
            json.loads(bad_body)
        except json.JSONDecodeError as error:
            location = f'line {error.lineno}, column {error.colno}'
            reason = error.msg
        request = RelayRequest(
            model='model', messages=(TextMessage(role='user', content='ping'),),
            tools=(ToolSpec('ping', '', {'type': 'object'}),),
        )
        for final_reply in (good, bad):
            with self.subTest(recovered=final_reply == good):
                attempts = []

                class RetryingBackend:
                    def complete(self, attempt):
                        attempts.append(attempt)
                        return UpstreamReply(bad if len(attempts) == 1 else final_reply)

                relay = TextActionRelay(RetryingBackend(), retries=1, max_action_bytes=4096)
                if final_reply == good:
                    self.assertEqual(relay.run(request).action.calls[0].name, 'ping')
                else:
                    with self.assertRaises(ActionTransportError):
                        relay.run(request)
                self.assertEqual(len(attempts), 2)
                feedback = attempts[1].messages[-1].content
                self.assertIn(location, feedback)
                self.assertIn(reason, feedback)
                self.assertNotIn(bad_body, feedback)


if __name__ == "__main__":
    unittest.main()
