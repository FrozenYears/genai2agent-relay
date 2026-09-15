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
