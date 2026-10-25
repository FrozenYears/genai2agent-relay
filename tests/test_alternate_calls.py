import json
import unittest

from relay.actions import ActionTransportError, ToolSpec, decode_action, encode_calls


CALLS = [{'operation': 'bash', 'parameters': {'command': 'echo diagnostic'}}]
ARRAY = json.dumps(CALLS)
TOOLS = [ToolSpec('bash', '', {'type': 'object'})]


class AlternateCallTests(unittest.TestCase):
    def test_documentation_examples_are_plain_text_without_correction(self):
        examples = [
            '说明\n```json\n' + ARRAY + '\n```',
            '```json\n' + ARRAY,
            '~~~json\n' + json.dumps({'calls': CALLS}) + '\n~~~',
            '<parameter name="calls">' + ARRAY + '</parameter>',
            '```json\n[{"operation":"bash","parameters":{"command":"bad "quote""}}]\n```',
            '```json\n[{"answer":42}]\n```',
            '<parameter name="calls">[1,2]</parameter>',
            ARRAY,
            json.dumps({'calls': CALLS}),
            '`' + encode_calls(CALLS) + '`',
            '```json\n' + encode_calls(CALLS) + '\n```',
        ]
        for text in examples:
            with self.subTest(text=text):
                result = decode_action(text, TOOLS, 4096)
                self.assertEqual(result.text, text)
                self.assertEqual(result.calls, ())

    def test_examples_before_real_envelope_remain_text(self):
        prefix = '```json\n' + ARRAY + '\n```\n'
        result = decode_action(prefix + encode_calls(CALLS), TOOLS, 4096)
        self.assertEqual(result.text, prefix.rstrip())
        self.assertEqual(result.calls[0].parameters, CALLS[0]['parameters'])

    def test_alternate_syntax_inside_parameters_remains_data(self):
        command = '<parameter name="calls">' + ARRAY + '</parameter>'
        result = decode_action(encode_calls([
            {'operation': 'bash', 'parameters': {'command': command}},
        ]), TOOLS, 4096)
        self.assertEqual(result.calls[0].parameters['command'], command)

    def test_multiple_or_broken_real_envelopes_are_never_repaired(self):
        envelope = encode_calls(CALLS)
        for text in [
            envelope + '\n' + envelope,
            '@@ACTION@@broken\n' + envelope,
            '@@ACTION@broken\n' + envelope,
            envelope + '\nordinary suffix',
            '@@ACTION@@' + json.dumps({'calls': CALLS}) + ',@@END_ACTION@@',
        ]:
            with self.subTest(text=text), self.assertRaises(ActionTransportError):
                decode_action(text, TOOLS, 4096)


if __name__ == '__main__':
    unittest.main()
