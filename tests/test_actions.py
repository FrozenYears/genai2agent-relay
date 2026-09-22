import json
import unittest

from relay.actions import ActionTransportError, ToolSpec, decode_action


TOOLS = [
    ToolSpec(
        name="Bash",
        description="Run a command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    )
]


class ActionTests(unittest.TestCase):
    def test_plain_text_is_not_an_action(self):
        decoded = decode_action("hello", TOOLS, 4096)
        self.assertEqual(decoded.text, "hello")
        self.assertEqual(decoded.calls, ())

    def test_final_valid_action_is_decoded(self):
        decoded = decode_action(
            'Working. @@ACTION@@{"calls":[{"operation":"Bash","parameters":{"command":"pwd"}}]}@@END_ACTION@@',
            TOOLS,
            4096,
        )
        self.assertEqual(decoded.text, "Working.")
        self.assertEqual(decoded.calls[0].name, "Bash")
        self.assertEqual(decoded.calls[0].parameters, {"command": "pwd"})

    def test_quoted_bad_example_does_not_hide_final_action(self):
        decoded = decode_action(
            'Example @@ACTION@@bad then @@ACTION@@{"calls":[{"operation":"Bash","parameters":{"command":"pwd"}}]}@@END_ACTION@@',
            TOOLS,
            4096,
        )
        self.assertEqual(decoded.calls[0].name, "Bash")

    def test_quoted_examples_remain_text(self):
        envelope = '@@ACTION@@{"calls":[{"operation":"Bash","parameters":{"command":"pwd"}}]}@@END_ACTION@@'
        examples = [
            '动作信封 `@@ACTION@@{...}@@END_ACTION@@` 时解析失败。',
            f'Example `{envelope}`',
            f'Example ``{envelope}``',
            f'```json\n{envelope}\n```',
            f'~~~json\n{envelope}\n~~~',
            'Example `@@ACTION@{...}@@END_ACTION@@`',
            '```text\n@@ACTION@{...}@@END_ACTION@@\n```',
            '结束标记是 `@@END_ACTION@@`。',
        ]
        for text in examples:
            with self.subTest(text=text):
                result = decode_action(text, TOOLS, 4096)
                self.assertEqual(result.text, text)
                self.assertEqual(result.calls, ())

    def test_real_call_after_example_preserves_markdown_parameters(self):
        command = 'echo "`code` ``` @@ACTION@ @@ACTION@@ @@END_ACTION@@"\nnext'
        prefix = 'Example `@@ACTION@@{...}@@END_ACTION@@`.\n'
        payload = json.dumps({'calls': [{'operation': 'Bash', 'parameters': {'command': command}}]})
        result = decode_action(prefix + '@@ACTION@@' + payload + '@@END_ACTION@@', TOOLS, 4096)
        self.assertEqual(result.text, prefix.rstrip())
        self.assertEqual(result.calls[0].parameters['command'], command)

    def test_quoted_example_does_not_hide_broken_real_call(self):
        prefix = 'Example `@@ACTION@@{...}@@END_ACTION@@`.\n'
        for suffix in ['@@ACTION@@{', '@@ACTION@@{bad}@@END_ACTION@@', '`@@ACTION@@{']:
            with self.subTest(suffix=suffix):
                with self.assertRaises(ActionTransportError):
                    decode_action(prefix + suffix, TOOLS, 4096)

    def test_damaged_delimiters_are_not_plain_text(self):
        for text in [
            '@@ACTION@{"calls":[]}@@END_ACTION@@',
            '@@ACTION@{"calls":[]}',
            '{"calls":[]}@@END_ACTION@@',
            'Example `@@ACTION@` then @@END_ACTION@@',
        ]:
            with self.subTest(text=text), self.assertRaises(ActionTransportError):
                decode_action(text, TOOLS, 4096)

    def test_rejects_incomplete_unknown_and_schema_invalid_actions(self):
        invalid = [
            '@@ACTION@@{"calls":[]}',
            '@@ACTION@@{"calls":[{"operation":"Write","parameters":{}}]}@@END_ACTION@@',
            '@@ACTION@@{"calls":[{"operation":"Bash","parameters":{"command":2}}]}@@END_ACTION@@',
            '<|open|>call tool="Bash"',
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ActionTransportError):
                decode_action(value, TOOLS, 4096)


if __name__ == "__main__":
    unittest.main()
