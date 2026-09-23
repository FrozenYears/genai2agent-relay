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

    def test_reasoning_only_retry_recovers_or_exhausts_shared_budget(self):
        thinking = UpstreamReply(content=' \n', reasoning='Let me run a few checks.')
        text = UpstreamReply(content='检查完成。')
        action = UpstreamReply(content='@@ACTION@@{"calls":[{"operation":"ping","parameters":{}}]}@@END_ACTION@@')
        malformed = UpstreamReply(content='@@ACTION@@{bad}@@END_ACTION@@')
        request = RelayRequest(
            model='model', messages=(TextMessage(role='user', content='检查'),),
            tools=(ToolSpec('ping', '', {'type': 'object'}),),
        )
        cases = [
            ([thinking, text], 1, True),
            ([thinking, action], 1, True),
            ([thinking, thinking], 1, False),
            ([malformed, thinking], 1, False),
            ([thinking], 0, False),
            ([text], 1, True),
        ]
        for replies, retries, succeeds in cases:
            with self.subTest(replies=replies, retries=retries):
                attempts = []

                class SequenceBackend:
                    def complete(self, attempt):
                        attempts.append(attempt)
                        return replies[len(attempts) - 1]

                relay = TextActionRelay(SequenceBackend(), retries, 4096)
                if succeeds:
                    result = relay.run(request)
                    if replies[-1] == action:
                        self.assertEqual(result.action.calls[0].name, 'ping')
                    else:
                        self.assertEqual(result.action.text, text.content)
                else:
                    with self.assertRaises(ActionTransportError):
                        relay.run(request)
                self.assertEqual(len(attempts), len(replies))
                if replies[0] == thinking and retries:
                    self.assertTrue(all(m.content.strip() for m in attempts[1].messages))
                    self.assertNotIn(thinking.reasoning, [m.content for m in attempts[1].messages])
    def test_empty_retry_appends_to_current_user_instead_of_adding_user_turn(self):
        request = RelayRequest(
            model='model', messages=(
                TextMessage('user', 'current task'),
                TextMessage('assistant', 'previous answer'),
                TextMessage('user', 'latest task'),
            ),
        )
        attempts = []

        class Backend:
            def complete(self, attempt):
                attempts.append(attempt)
                return UpstreamReply('', reasoning='thinking') if len(attempts) == 1 else UpstreamReply('done')

        result = TextActionRelay(Backend(), retries=1, max_action_bytes=4096).run(request)
        self.assertEqual(result.action.text, 'done')
        self.assertEqual([message.role for message in attempts[1].messages], ['user', 'assistant', 'user'])
        self.assertIn('latest task', attempts[1].messages[-1].content)
        self.assertIn('请继续', attempts[1].messages[-1].content)
        self.assertNotIn('thinking', attempts[1].messages[-1].content)



if __name__ == "__main__":
    unittest.main()
