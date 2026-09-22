import json
import unittest

from relay.actions import ActionTransportError, ToolSpec, decode_action, encode_calls
from relay.app import create_app
from relay.config import RelayConfig
from relay.engine import UpstreamReply


CALLS = [{'operation': 'bash', 'parameters': {'command': 'echo diagnostic'}}]
ARRAY = json.dumps(CALLS)
TOOLS = [ToolSpec('bash', '', {'type': 'object'})]


class AlternateCallTests(unittest.TestCase):
    def test_known_calls_in_alternate_wrappers_are_rejected(self):
        for text in [
            '说明\n```json\n' + ARRAY + '\n```',
            '```json\n' + ARRAY,
            '~~~json\n' + json.dumps({'calls': CALLS}) + '\n~~~',
            '<parameter name="calls">' + ARRAY + '</parameter>',
            '```json\n[{"operation":"bash","parameters":{"command":"bad "quote""}}]\n```',
        ]:
            with self.subTest(text=text), self.assertRaises(ActionTransportError):
                decode_action(text, TOOLS, 4096)

    def test_ordinary_data_and_real_call_parameters_remain_unchanged(self):
        for text in ['```json\n[{"answer":42}]\n```', '<parameter name="calls">[1,2]</parameter>']:
            self.assertEqual(decode_action(text, TOOLS, 4096).text, text)
        text = '```json\n' + ARRAY + '\n```'
        self.assertEqual(decode_action(text, [], 4096).text, text)
        command = '<parameter name="calls">' + ARRAY + '</parameter>'
        result = decode_action(encode_calls([{'operation': 'bash', 'parameters': {'command': command}}]), TOOLS, 4096)
        self.assertEqual(result.calls[0].parameters['command'], command)

    def test_route_recovers_before_delivery_or_rejects_exhausted_retry(self):
        bad = '<parameter name="calls">' + ARRAY + '</parameter>'
        for final, expected in [(encode_calls(CALLS), 200), (bad, 502)]:
            with self.subTest(expected=expected):
                class Backend:
                    attempts = 0
                    requests = None

                    def complete(self, request):
                        self.attempts += 1
                        self.requests = request
                        return UpstreamReply(bad if self.attempts == 1 else final)

                backend = Backend()
                client = create_app(RelayConfig(upstream_base_url='http://unused.invalid', upstream_action_retries=1), backend).test_client()
                response = client.post('/v1/messages', json={
                    'model': 'test', 'stream': True,
                    'messages': [{'role': 'user', 'content': 'check'}],
                    'tools': [{'name': 'bash', 'input_schema': {'type': 'object'}}],
                })
                self.assertEqual(response.status_code, expected)
                self.assertEqual(backend.attempts, 2)
                self.assertIn('without code fences or XML', backend.requests.messages[-1].content)
                if expected == 200:
                    self.assertIn('"tool_use"', response.text)
                    self.assertNotIn('<parameter', response.text)
                else:
                    self.assertEqual(response.get_json()['error']['type'], 'api_error')


if __name__ == '__main__':
    unittest.main()
