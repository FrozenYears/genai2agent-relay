import json
import unittest

from relay import messages, normalize, responses
from relay.actions import ActionCall, DecodedAction
from relay.engine import RelayResult, UpstreamReply


def chat_request(body):
    return normalize.prepare_chat(body, normalize.chat_tools(body.get('tools')), normalize.normalize_choice(body.get('tool_choice')))


def messages_request(body):
    return messages.prepare_messages(body, messages.message_tools(body.get('tools')), messages.normalize_choice(body.get('tool_choice')))


def responses_request(body):
    return responses.prepare_responses(body, responses.response_tools(body.get('tools')), responses.normalize_choice(body.get('tool_choice')))


class InputContractTests(unittest.TestCase):
    def test_chat_preserves_native_history_roles_reasoning_and_attachments(self):
        call = {'id': 'call_history', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"path":"x"}'}}
        image = {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/a.png', 'detail': 'high'}}
        file = {'type': 'file', 'file': {'filename': 'a.txt', 'file_data': 'data:text/plain;base64,YQ=='}}
        body = {
            'messages': [
                {'role': 'system', 'content': '系统规则'},
                {'role': 'developer', 'content': '开发者规则'},
                {'role': 'user', 'content': [{'type': 'text', 'text': '请读取'}, image, file]},
                {'role': 'assistant', 'content': '', 'tool_calls': [call], 'reasoning_content': '保留思考'},
                {'role': 'tool', 'content': '读取失败', 'tool_call_id': 'call_history', 'is_error': True},
            ],
            'parallel_tool_calls': False,
        }
        request = chat_request(body)
        self.assertEqual([item.role for item in request.messages], ['system', 'developer', 'user', 'assistant', 'tool'])
        self.assertEqual(request.instructions, '')
        self.assertIn(image, request.messages[2].content)
        self.assertIn(file, request.messages[2].content)
        self.assertEqual(request.messages[3].tool_calls, (call,))
        self.assertEqual(request.messages[3].reasoning_content, '保留思考')
        self.assertEqual(request.messages[4].tool_call_id, 'call_history')
        self.assertTrue(request.messages[4].as_dict()['is_error'])
        self.assertEqual(request.messages[4].content, '读取失败')
        self.assertFalse(request.parallel_tool_calls)

    def test_messages_preserves_thinking_ids_errors_and_result_order(self):
        thinking = {'type': 'thinking', 'thinking': '检查文件', 'signature': 'original-signature'}
        redacted = {'type': 'redacted_thinking', 'data': 'opaque'}
        body = {
            'system': [{'type': 'text', 'text': '系统规则'}],
            'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': True},
            'messages': [
                {'role': 'assistant', 'content': [thinking, redacted, {'type': 'tool_use', 'id': 'toolu_original', 'name': 'read', 'input': {'path': 'a'}}]},
                {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_original', 'is_error': True, 'content': [
                        {'type': 'text', 'text': '拒绝访问'},
                        {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.invalid/a.png'}},
                    ]},
                    {'type': 'text', 'text': '请说明原因'},
                ]},
            ],
        }
        request = messages_request(body)
        self.assertEqual(request.instructions, '系统规则')
        self.assertEqual([item.role for item in request.messages], ['assistant', 'tool', 'user'])
        assistant, result, user = request.messages
        self.assertEqual(assistant.reasoning_content, '检查文件')
        self.assertEqual(assistant.extra['thinking_blocks'], [thinking, redacted])
        self.assertEqual(assistant.tool_calls[0]['id'], 'toolu_original')
        self.assertEqual(json.loads(assistant.tool_calls[0]['function']['arguments']), {'path': 'a'})
        self.assertEqual(result.tool_call_id, 'toolu_original')
        self.assertTrue(result.extra['is_error'])
        self.assertEqual(result.content[1]['image_url']['url'], 'https://example.invalid/a.png')
        self.assertEqual(user.content, '请说明原因')
        self.assertFalse(request.parallel_tool_calls)

    def test_responses_namespaced_custom_round_trip_and_reasoning(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_history', 'summary': [{'type': 'summary_text', 'text': '准备编辑'}], 'encrypted_content': 'opaque'}
        body = {
            'tools': [{'type': 'namespace', 'name': 'workspace', 'description': '工作区', 'tools': [
                {'type': 'custom', 'name': 'edit', 'format': {'type': 'text'}},
                {'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}, 'strict': True},
            ]}],
            'tool_choice': {'type': 'custom', 'name': 'edit', 'namespace': 'workspace'},
            'parallel_tool_calls': False,
            'store': False,
            'input': [
                reasoning,
                {'type': 'custom_tool_call', 'call_id': 'call_edit', 'name': 'edit', 'namespace': 'workspace', 'input': '*** patch ***'},
                {'type': 'function_call', 'call_id': 'call_read', 'name': 'read', 'namespace': 'workspace', 'arguments': '{"path":"a"}'},
                {'type': 'custom_tool_call_output', 'call_id': 'call_edit', 'output': '失败', 'is_error': True, 'error': {'message': 'denied'}},
                {'type': 'function_call_output', 'call_id': 'call_read', 'output': [{'type': 'input_text', 'text': '文件内容'}]},
            ],
        }
        request = responses_request(body)
        self.assertEqual([tool.name for tool in request.tools], ['workspace.edit', 'workspace.read'])
        self.assertEqual(request.tools[0].kind, 'custom')
        self.assertEqual(request.tools[0].namespace, 'workspace')
        self.assertEqual(request.tools[0].parameters['properties'], {'input': {'type': 'string'}})
        self.assertTrue(request.tools[1].strict)
        self.assertEqual(request.tool_choice, 'workspace.edit')
        self.assertFalse(request.parallel_tool_calls)
        assistant = request.messages[0]
        self.assertEqual(assistant.reasoning_content, '准备编辑')
        self.assertEqual(assistant.extra['reasoning'], reasoning)
        self.assertEqual(len(assistant.tool_calls), 2)
        custom = assistant.tool_calls[0]
        self.assertEqual(custom['function']['name'], 'workspace.edit')
        self.assertEqual(json.loads(custom['function']['arguments']), {'input': '*** patch ***'})
        self.assertEqual([item.role for item in request.messages], ['assistant', 'tool', 'tool'])
        self.assertEqual(request.messages[1].tool_call_id, 'call_edit')
        self.assertEqual(request.messages[1].extra['error'], {'message': 'denied'})
        self.assertTrue(request.messages[1].extra['is_error'])
        self.assertEqual(request.messages[2].content, '文件内容')

    def test_responses_preserves_roles_attachments_and_reasoning_content(self):
        request = responses_request({'input': [
            {'role': 'developer', 'content': '约束'},
            {'role': 'user', 'content': [{'type': 'input_file', 'filename': 'a.txt', 'file_data': 'data:text/plain;base64,YQ=='}]},
            {'role': 'assistant', 'content': '已读', 'reasoning_content': '历史思考'},
        ]})
        self.assertEqual(request.messages[0].role, 'developer')
        self.assertEqual(request.messages[1].content[0]['file']['filename'], 'a.txt')
        self.assertEqual(request.messages[2].reasoning_content, '历史思考')

    def test_responses_rejects_unsupported_storage_and_item_references(self):
        for option in ({'previous_response_id': 'resp_old'}, {'store': True}, {'storage': {}}, {'input': [{'type': 'item_reference', 'id': 'item_old'}]}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                responses_request(option)

    def test_unknown_tool_types_and_custom_grammar_are_rejected(self):
        for prepare in (chat_request, messages_request, responses_request):
            with self.subTest(prepare=prepare.__name__), self.assertRaises(ValueError):
                prepare({'tools': [{'type': 'not_supported', 'name': 'x'}]})
        for format_ in ({'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "x"'}, {'type': 'json'}, 'text'):
            with self.subTest(format=format_), self.assertRaises(ValueError):
                responses_request({'tools': [{'type': 'custom', 'name': 'edit', 'format': format_}]})
        with self.assertRaises(ValueError):
            responses_request({'tools': [{'type': 'namespace', 'name': 'ns', 'tools': [{'type': 'unknown', 'name': 'x'}]}]})

    def test_duplicate_tools_are_rejected_in_every_protocol(self):
        cases = [
            (chat_request, {'type': 'function', 'function': {'name': 'read'}}),
            (messages_request, {'name': 'read', 'input_schema': {'type': 'object'}}),
            (responses_request, {'type': 'function', 'name': 'read'}),
        ]
        for prepare, tool in cases:
            with self.subTest(prepare=prepare.__name__), self.assertRaises(ValueError):
                prepare({'tools': [tool, tool]})
        with self.assertRaises(ValueError):
            responses_request({'tools': [
                {'type': 'function', 'name': 'ns.read'},
                {'type': 'namespace', 'name': 'ns', 'tools': [{'type': 'function', 'name': 'read'}]},
            ]})

    def test_invalid_or_missing_tool_choices_are_rejected(self):
        for prepare in (chat_request, messages_request, responses_request):
            for choice in ('bogus', 42, {'type': 'unknown'}):
                with self.subTest(prepare=prepare.__name__, choice=choice), self.assertRaises(ValueError):
                    prepare({'tool_choice': choice})
        for prepare, choice in (
            (chat_request, {'type': 'function', 'function': {'name': 'missing'}}),
            (messages_request, {'type': 'tool', 'name': 'missing'}),
            (responses_request, {'type': 'custom', 'namespace': 'ns', 'name': 'missing'}),
            (responses_request, 'required'),
            (messages_request, {'type': 'any'}),
        ):
            with self.subTest(prepare=prepare.__name__, choice=choice), self.assertRaises(ValueError):
                prepare({'tool_choice': choice})
        with self.assertRaises(ValueError):
            responses_request({'tools': [{'type': 'function', 'name': 'read'}], 'tool_choice': {'type': 'custom', 'name': 'read'}})

    def test_search_and_thinking_controls_are_preserved(self):
        request = responses_request({'tools': [{'type': 'web_search'}], 'reasoning': {'effort': 'high'}})
        self.assertEqual(request.tools, ())
        self.assertEqual(request.upstream_options, {'thinking': True, 'web_search': True})
        request = messages_request({'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}], 'tool_choice': {'type': 'auto'}})
        self.assertTrue(request.upstream_options['web_search'])
        request = responses_request({'tools': [{'type': 'web_search'}], 'tool_choice': 'none'})
        self.assertFalse(request.upstream_options.get('web_search', False))
        for prepare, choice in (
            (messages_request, {'type': 'tool', 'name': 'web_search'}),
            (messages_request, {'type': 'any'}),
            (responses_request, 'required'),
            (responses_request, {'type': 'function', 'name': 'web_search'}),
            (chat_request, 'required'),
            (chat_request, {'type': 'function', 'function': {'name': 'web_search'}}),
        ):
            with self.subTest(prepare=prepare.__name__, choice=choice), self.assertRaisesRegex(ValueError, 'Built-in search'):
                prepare({'tools': [{'type': 'web_search', 'name': 'web_search'}], 'tool_choice': choice})

    def test_nonstream_output_preserves_call_ids_namespace_custom_and_parallel(self):
        result = RelayResult(
            action=DecodedAction('完成', (
                ActionCall(name='workspace.read', parameters={'path': 'a'}, kind='function', id='call_read', namespace='workspace'),
                ActionCall(name='workspace.edit', parameters={'input': 'patch'}, kind='custom', id='call_edit', namespace='workspace'),
            )),
            upstream=UpstreamReply('完成', reasoning='思考', usage={'prompt_tokens': 2, 'completion_tokens': 3}),
        )
        response = responses.build_response({'parallel_tool_calls': False}, result)
        self.assertFalse(response['parallel_tool_calls'])
        function, custom = response['output'][-2:]
        self.assertEqual(function['type'], 'function_call')
        self.assertEqual(function['call_id'], 'call_read')
        self.assertEqual(function['namespace'], 'workspace')
        self.assertEqual(function['name'], 'read')
        self.assertEqual(json.loads(function['arguments']), {'path': 'a'})
        self.assertEqual(custom['type'], 'custom_tool_call')
        self.assertEqual(custom['call_id'], 'call_edit')
        self.assertEqual(custom['namespace'], 'workspace')
        self.assertEqual(custom['name'], 'edit')
        self.assertEqual(custom['input'], 'patch')
        self.assertNotIn('arguments', custom)
        self.assertEqual(response['usage']['total_tokens'], 5)
        response = messages.build_response({}, result)
        self.assertEqual([block['id'] for block in response['content'] if block['type'] == 'tool_use'], ['call_read', 'call_edit'])
        self.assertEqual(response['stop_reason'], 'tool_use')

    def test_chat_custom_tools_and_history_use_native_function_wrapper(self):
        body = {
            'tools': [{'type': 'custom', 'custom': {'name': 'edit', 'description': '编辑', 'format': {'type': 'text'}}}],
            'tool_choice': {'type': 'custom', 'custom': {'name': 'edit'}},
            'messages': [
                {'role': 'assistant', 'tool_calls': [{'type': 'custom', 'id': 'call_edit', 'custom': {'name': 'edit', 'input': 'patch\ncontent'}}]},
                {'role': 'tool', 'tool_call_id': 'call_edit', 'content': '完成'},
            ],
        }
        request = chat_request(body)
        self.assertEqual(request.tools[0].kind, 'custom')
        self.assertEqual(request.tools[0].parameters['properties'], {'input': {'type': 'string'}})
        self.assertEqual(request.tool_choice, 'edit')
        call = request.messages[0].tool_calls[0]
        self.assertEqual(call['id'], 'call_edit')
        self.assertEqual(call['type'], 'function')
        self.assertEqual(json.loads(call['function']['arguments']), {'input': 'patch\ncontent'})
        with self.assertRaisesRegex(ValueError, 'grammar'):
            chat_request({'tools': [{'type': 'custom', 'custom': {'name': 'edit', 'format': {'type': 'grammar'}}}]})

    def test_chat_rejects_malformed_history_and_missing_result_ids(self):
        invalid_calls = [
            None, 'bad', {}, {'id': 'x', 'type': 'unknown'},
            {'id': 'x', 'type': 'function', 'function': {}},
            {'id': 'x', 'type': 'function', 'function': {'name': 'read', 'arguments': {}}},
            {'id': 'x', 'type': 'function', 'function': {'name': 'read', 'arguments': '{'}},
            {'id': 'x', 'type': 'function', 'function': {'name': 'read', 'arguments': '[]'}},
            {'id': 'x', 'type': 'custom', 'custom': {'name': 'edit', 'input': {}}},
            {'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}},
        ]
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                chat_request({'messages': [{'role': 'assistant', 'tool_calls': [call]}]})
        with self.assertRaises(ValueError):
            chat_request({'messages': [{'role': 'assistant', 'tool_calls': {}}]})
        with self.assertRaises(ValueError):
            chat_request({'messages': [{'role': 'tool', 'content': '结果'}]})

    def test_every_protocol_rejects_duplicate_historical_call_ids(self):
        call = {'id': 'duplicate', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}
        cases = [
            (chat_request, {'messages': [{'role': 'assistant', 'tool_calls': [call, call]}]}),
            (messages_request, {'messages': [{'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'duplicate', 'name': 'read', 'input': {}},
                {'type': 'tool_use', 'id': 'duplicate', 'name': 'read', 'input': {}},
            ]}]}),
            (responses_request, {'input': [
                {'type': 'function_call', 'call_id': 'duplicate', 'name': 'read', 'arguments': '{}'},
                {'type': 'custom_tool_call', 'call_id': 'duplicate', 'name': 'edit', 'input': 'patch'},
            ]}),
        ]
        for prepare, body in cases:
            with self.subTest(prepare=prepare.__name__), self.assertRaisesRegex(ValueError, 'Duplicate historical'):
                prepare(body)

    def test_messages_and_responses_reject_missing_ids_and_malformed_arguments(self):
        cases = [
            (messages_request, {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'read', 'input': {}}]}]}),
            (messages_request, {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'content': '结果'}]}]}),
            (responses_request, {'input': [{'type': 'function_call', 'name': 'read', 'arguments': '{}'}]}),
            (responses_request, {'input': [{'type': 'function_call', 'call_id': 'x', 'name': 'read', 'arguments': 'not json'}]}),
            (responses_request, {'input': [{'type': 'custom_tool_call_output', 'output': '结果'}]}),
            (responses_request, {'input': [{'role': 'tool', 'content': '结果'}]}),
        ]
        for prepare, body in cases:
            with self.subTest(prepare=prepare.__name__, body=body), self.assertRaises(ValueError):
                prepare(body)


class SchemaBoundaryTests(unittest.TestCase):
    def test_omitted_parameters_are_zero_argument_schema(self):
        expected = {"type": "object", "properties": {}, "additionalProperties": False}
        self.assertEqual(normalize.chat_tools([{"type": "function", "function": {"name": "ping"}}])[0].parameters, expected)
        self.assertEqual(responses.response_tools([{"type": "function", "name": "ping"}])[0].parameters, expected)
        self.assertEqual(normalize.chat_tools([{"type": "function", "function": {"name": "ping", "parameters": {}}}])[0].parameters, {})
        with self.assertRaises(ValueError):
            messages.message_tools([{"name": "ping"}])

    def test_history_arguments_reject_duplicate_keys_and_nonfinite_values(self):
        for arguments in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            call = {"id": "id", "type": "function", "function": {"name": "ping", "arguments": arguments}}
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                chat_request({"messages": [{"role": "assistant", "tool_calls": [call]}]})


if __name__ == '__main__':
    unittest.main()
