import json
import unittest

from relay.actions import (
    ActionCall,
    ActionTransportError,
    ActionValidationError,
    DecodedAction,
    ToolSpec,
    decode_action,
    encode_calls,
    strict_json_loads,
    validate_choice,
    validate_native_calls,
)
from relay.content import escape_action_markers


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

    def test_control_markers_round_trip_through_json_parameters(self):
        original = '<think>keep</think> </tool_call> <|DSML|call> @@ACTION@@ @@END_ACTION@@\nunicode=雪'
        escaped = escape_action_markers(original)
        self.assertIn(r"\u003cthink\u003e", escaped)
        tool = ToolSpec(
            name="write_file",
            description="Write a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        )
        payload = json.dumps({"calls": [{"operation": "write_file", "parameters": {
            "path": "file.txt", "content": escaped,
        }}]}, ensure_ascii=False)
        payload = payload.replace(r"\\u003c", r"\u003c").replace(r"\\u003e", r"\u003e")
        payload = payload.replace(r"\\u0040", r"\u0040")

        decoded = decode_action("@@ACTION@@" + payload + "@@END_ACTION@@", [tool], 4096)

        self.assertEqual(decoded.calls[0].parameters["content"], original)

    def test_broken_prefix_cannot_be_discarded_for_a_valid_envelope(self):
        envelope = encode_calls([{"operation": "Bash", "parameters": {"command": "pwd"}}])
        for prefix in [
            "Example @@ACTION@@bad then ",
            "@@ACTION@broken ",
            "@@END_ACTION@@ ",
            envelope,
        ]:
            with self.subTest(prefix=prefix), self.assertRaises(ActionTransportError):
                decode_action(prefix + envelope, TOOLS, 4096)

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

    def test_inline_thinking_never_becomes_an_action(self):
        envelope = encode_calls([{"operation": "Bash", "parameters": {"command": "pwd"}}])
        for text in ("<think>" + envelope, "<think>" + envelope + "</think>Final answer"):
            with self.subTest(text=text):
                self.assertEqual(decode_action(text, TOOLS, 4096).calls, ())

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

    def test_verified_marker_examples_are_plain_text(self):
        examples = [
            'Verified: `<think>keep</think>`, `</tool_call>`, `<|DSML|call>`, `@@ACTION@@`, `@@END_ACTION@@`.',
            'Verified file:\n```text\n<think>keep</think> </tool_call> <|DSML|call> @@ACTION@@ @@END_ACTION@@\n```',
        ]
        for content in examples:
            with self.subTest(content=content):
                result = decode_action(content, TOOLS, 4096)
                self.assertEqual(result.text, content)
                self.assertEqual(result.calls, ())

    def test_native_marker_inside_valid_parameters_is_data(self):
        command = 'echo "<tool_call> <|open|>call <｜DSML｜call>"'
        payload = {"calls": [{"operation": "Bash", "parameters": {"command": command}}]}
        result = decode_action(
            "@@ACTION@@" + json.dumps(payload) + "@@END_ACTION@@", TOOLS, 4096
        )
        self.assertEqual(result.calls[0].parameters, {"command": command})

    def test_duplicate_keys_and_non_finite_numbers_are_rejected(self):
        bodies = [
            '{"calls":[],"calls":[{"operation":"Bash","parameters":{"command":"pwd"}}]}',
            '{"calls":[{"operation":"Bash","operation":"Bash","parameters":{"command":"pwd"}}]}',
            '{"calls":[{"operation":"Bash","parameters":{"command":"pwd","command":"ls"}}]}',
            '{"calls":[{"operation":"Bash","parameters":{"command":"pwd","value":NaN}}]}',
            '{"calls":[{"operation":"Bash","parameters":{"command":"pwd","value":Infinity}}]}',
            '{"calls":[{"operation":"Bash","parameters":{"command":"pwd","value":-Infinity}}]}',
            '{"calls":[{"operation":"Bash","parameters":{"command":"pwd","value":1e999}}]}',
        ]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ActionTransportError):
                decode_action("@@ACTION@@" + body + "@@END_ACTION@@", TOOLS, 4096)

    def test_text_calls_preserve_optional_ids_and_generate_missing_ids(self):
        decoded = decode_action(encode_calls([
            {"operation": "Bash", "parameters": {"command": "pwd"}, "id": "external-id"},
            {"operation": "Bash", "parameters": {"command": "ls"}},
        ]), TOOLS, 4096)
        self.assertEqual(decoded.calls[0].id, "external-id")
        self.assertRegex(decoded.calls[1].id, r"^call_[0-9a-f]{32}$")
        self.assertNotEqual(decoded.calls[0].id, decoded.calls[1].id)
        self.assertRegex(ActionCall("Bash", {}, "function").id, r"^call_[0-9a-f]{32}$")

    def test_text_calls_reject_invalid_or_duplicate_ids(self):
        for call_id in [None, "", "  ", 1, [], {}]:
            with self.subTest(call_id=call_id), self.assertRaises(ActionValidationError):
                decode_action(encode_calls([
                    {"operation": "Bash", "parameters": {"command": "pwd"}, "id": call_id},
                ]), TOOLS, 4096)
        call = {"operation": "Bash", "parameters": {"command": "pwd"}, "id": "same"}
        with self.assertRaises(ActionValidationError):
            decode_action(encode_calls([call, call]), TOOLS, 4096)

    def test_invalid_batch_collects_schema_failures_without_partial_delivery(self):
        with self.assertRaises(ActionValidationError) as caught:
            decode_action(encode_calls([
                {"operation": "Bash", "parameters": {"command": "pwd"}},
                {"operation": "Bash", "parameters": {}},
                {"operation": "Bash", "parameters": {"command": 1}},
            ]), TOOLS, 4096)
        self.assertIn("calls[1]", caught.exception.feedback)
        self.assertIn("calls[2]", caught.exception.feedback)


class NativeCallTests(unittest.TestCase):
    @staticmethod
    def function_call(arguments='{"command":"pwd"}', name="Bash", call_id="native-id"):
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}

    def test_native_function_preserves_external_id(self):
        calls = validate_native_calls([self.function_call()], TOOLS)
        self.assertEqual(calls, (ActionCall("Bash", {"command": "pwd"}, "function", "native-id"),))

    def test_custom_native_and_function_wrapper_keep_custom_kind(self):
        tool = ToolSpec("shell.run", "", {}, kind="custom", namespace="shell", strict=True)
        raw_calls = [
            {"id": "custom-id", "type": "custom", "custom": {"name": "shell.run", "input": "echo 雪"}},
            self.function_call('{"input":"echo 雪"}', name="shell.run", call_id="wrapped-id"),
        ]
        calls = validate_native_calls(raw_calls, [tool])
        self.assertEqual([call.id for call in calls], ["custom-id", "wrapped-id"])
        for call in calls:
            self.assertEqual(call.name, "shell.run")
            self.assertEqual(call.namespace, "shell")
            self.assertEqual(call.kind, "custom")
            self.assertEqual(call.parameters, {"input": "echo 雪"})
        decoded = decode_action(encode_calls([
            {"operation": "shell.run", "parameters": {"input": "pwd"}},
        ]), [tool], 4096)
        self.assertEqual(decoded.calls[0].namespace, "shell")
        self.assertTrue(tool.strict)

    def test_native_ids_are_required_non_empty_strings_and_unique(self):
        missing = self.function_call()
        del missing["id"]
        invalid_batches = [[missing]]
        invalid_batches.extend([[self.function_call(call_id=value)] for value in [None, "", " ", 1, {}, []]])
        invalid_batches.append([self.function_call(), self.function_call()])
        for raw_calls in invalid_batches:
            with self.subTest(raw_calls=raw_calls), self.assertRaises(ActionValidationError):
                validate_native_calls(raw_calls, TOOLS)

    def test_native_shapes_and_tool_types_are_strict(self):
        invalid = [
            None,
            {},
            [None],
            [{"id": "x", "function": {"name": "Bash", "arguments": "{}"}}],
            [{"id": "x", "type": [], "function": {"name": "Bash", "arguments": "{}"}}],
            [{"id": "x", "type": "other", "function": {"name": "Bash", "arguments": "{}"}}],
            [{"id": "x", "type": "function", "function": {"name": "Bash"}}],
            [{"id": "x", "type": "function", "function": {"name": "Bash", "arguments": "{}", "extra": 1}}],
            [{"id": "x", "type": "function", "function": {"name": "Bash", "arguments": "{}"}, "extra": 1}],
            [{"id": "x", "type": "custom", "custom": {"name": "Bash", "input": "pwd"}}],
            [self.function_call(name="unknown")],
            [self.function_call(name=[])],
        ]
        for raw_calls in invalid:
            with self.subTest(raw_calls=raw_calls), self.assertRaises(ActionValidationError):
                validate_native_calls(raw_calls, TOOLS)

    def test_native_arguments_require_strict_json_objects(self):
        for arguments in [
            {"command": "pwd"}, None, "[]", "null", '"pwd"', '{"command":"pwd",}',
            '{"command":"pwd","command":"ls"}', '{"command":NaN}',
            '{"command":Infinity}', '{"command":-Infinity}', '{"command":1e999}',
            '{"command":"pwd"} trailing',
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ActionValidationError):
                validate_native_calls([self.function_call(arguments)], TOOLS)

    def test_custom_input_and_wrapper_schema_are_strict(self):
        tool = ToolSpec("custom", "", {}, kind="custom")
        invalid = [
            {"id": "x", "type": "custom", "custom": {"name": "custom", "input": {}}},
            self.function_call("{}", name="custom"),
            self.function_call('{"input":1}', name="custom"),
            self.function_call('{"input":"pwd","extra":true}', name="custom"),
        ]
        for raw_call in invalid:
            with self.subTest(raw_call=raw_call), self.assertRaises(ActionValidationError):
                validate_native_calls([raw_call], [tool])

    def test_native_batch_uses_full_schema_atomically(self):
        tool = ToolSpec("nested", "", {
            "type": "object",
            "$defs": {"value": {"type": "integer", "minimum": 1}},
            "properties": {
                "values": {"type": "array", "items": {"$ref": "#/$defs/value"}, "minItems": 1},
                "mode": {"enum": ["read", "write"]},
            },
            "required": ["values", "mode"],
            "additionalProperties": False,
        })
        valid = {"values": [1, 2], "mode": "read"}
        for invalid in [
            {"values": [0], "mode": "read"},
            {"values": [], "mode": "read"},
            {"values": [1], "mode": "other"},
            {"values": [1], "mode": "read", "extra": True},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ActionValidationError):
                validate_native_calls([
                    self.function_call(json.dumps(valid), name="nested", call_id="valid"),
                    self.function_call(json.dumps(invalid), name="nested", call_id="invalid"),
                ], [tool])
        calls = validate_native_calls([self.function_call(json.dumps(valid), name="nested")], [tool])
        self.assertEqual(calls[0].parameters, valid)

    def test_native_choice_and_parallel_constraints(self):
        self.assertEqual(validate_native_calls([], TOOLS, choice="none"), ())
        self.assertEqual(validate_native_calls([], TOOLS), ())
        for choice in ["required", "Bash"]:
            with self.subTest(choice=choice), self.assertRaises(ActionValidationError):
                validate_native_calls([], TOOLS, choice=choice)
        for choice in ["none", "other"]:
            with self.subTest(choice=choice), self.assertRaises(ActionValidationError):
                validate_native_calls([self.function_call()], TOOLS, choice=choice)
        with self.assertRaises(ActionValidationError):
            validate_native_calls([
                self.function_call(call_id="one"), self.function_call(call_id="two"),
            ], TOOLS, parallel_tool_calls=False)
        self.assertEqual(len(validate_native_calls(
            [self.function_call()], TOOLS, choice="Bash", parallel_tool_calls=False,
        )), 1)


class ChoiceTests(unittest.TestCase):
    def test_choice_is_applied_to_complete_decoded_batch(self):
        empty = DecodedAction("answer", ())
        one = DecodedAction("", (ActionCall("Bash", {"command": "pwd"}, "function"),))
        mixed = DecodedAction("", one.calls + (ActionCall("Other", {}, "function"),))
        for choice in [None, "auto", "none"]:
            self.assertIs(validate_choice(empty, choice), empty)
        for choice in [None, "auto", "required", "Bash"]:
            self.assertIs(validate_choice(one, choice, False), one)
        for decoded, choice, parallel in [
            (empty, "required", True), (empty, "Bash", True),
            (one, "none", True), (one, "Other", True),
            (mixed, "Bash", True), (mixed, "auto", False),
        ]:
            with self.subTest(choice=choice, parallel=parallel), self.assertRaises(ActionValidationError):
                validate_choice(decoded, choice, parallel)

    def test_strict_loader_rejects_nested_duplicates(self):
        with self.assertRaises(ValueError):
            strict_json_loads('{"outer":{"same":1,"same":2}}')
        self.assertEqual(strict_json_loads('{"value":1.25,"text":"NaN"}'), {"value": 1.25, "text": "NaN"})


class OfflineSchemaTests(unittest.TestCase):
    def test_remote_references_are_rejected_at_request_boundary(self):
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ToolSpec("tool", "", {"type": "object", "properties": {"x": {key: "https://example.invalid/schema"}}})

    def test_local_reference_validation_remains_atomic(self):
        tool = ToolSpec("tool", "", {"type": "object", "$defs": {"value": {"type": "integer"}},
                                    "properties": {"x": {"$ref": "#/$defs/value"}}})
        good = {"id": "good", "type": "function", "function": {"name": "tool", "arguments": '{"x":1}'}}
        bad = {"id": "bad", "type": "function", "function": {"name": "tool", "arguments": '{"x":"invalid"}'}}
        self.assertEqual(validate_native_calls([good], [tool])[0].parameters, {"x": 1})
        with self.assertRaises(ActionValidationError):
            validate_native_calls([good, bad], [tool])

    def test_unresolvable_local_reference_is_explicit_validation_error(self):
        tool = ToolSpec("tool", "", {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing"}}})
        call = {"id": "id", "type": "function", "function": {"name": "tool", "arguments": '{"x":1}'}}
        with self.assertRaises(ActionValidationError):
            validate_native_calls([call], [tool])


if __name__ == "__main__":
    unittest.main()
