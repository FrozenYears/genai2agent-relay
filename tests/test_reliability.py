import json
import unittest

from relay.actions import ActionTransportError, ToolSpec, encode_calls
from relay.engine import RelayRequest, TextActionRelay, TextMessage, UpstreamReply
from relay.config import RelayConfig
from relay.upstream import UpstreamClient


class Backend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return next(self.replies)


EDIT = ToolSpec('edit', 'Edit using the supplied patch', {
    'type': 'object', 'properties': {'input': {'type': 'string'}},
    'required': ['input'], 'additionalProperties': False,
})


class ReliabilityTests(unittest.TestCase):
    def test_current_schema_moves_near_task_without_losing_history_or_attachments(self):
        image = {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/image.png'}}
        history = (
            TextMessage('user', '旧任务'),
            TextMessage('assistant', '旧回复'),
            TextMessage('user', [{'type': 'text', 'text': '当前任务'}, image]),
        )
        backend = Backend([UpstreamReply('完成')])
        result = TextActionRelay(backend, 1, 4096).run(RelayRequest(
            model='test', messages=history, instructions='保留用户规则', tools=(EDIT,),
        ))
        sent = backend.requests[0].messages
        self.assertEqual(result.action.text, '完成')
        self.assertIn('旧任务', sent[0].content)
        self.assertIn('保留用户规则', sent[0].content)
        self.assertNotIn('parameters_schema', sent[0].content)
        self.assertEqual(sent[1], history[1])
        self.assertIn(image, sent[2].content)
        text = '\n'.join(b['text'] for b in sent[2].content if b['type'] == 'text')
        self.assertIn('当前任务', text)
        self.assertIn(json.dumps(EDIT.parameters, separators=(',', ':')), text)
        self.assertEqual(text.count('parameters_schema'), 1)

    def test_structural_and_parameter_errors_receive_targeted_feedback(self):
        valid = UpstreamReply(encode_calls([{'operation': 'edit', 'parameters': {'input': 'patch'}}]))
        other = ToolSpec('unrelated', 'Not needed here', {'type': 'object'})
        cases = [
            ({'operation': 'edit', 'arguments': {}}, 'exactly operation and parameters'),
            ({'operation': 'edit', 'parameters': {'old_string': 'secret sample'}}, 'Missing required fields'),
            ({'operation': 'edit', 'parameters': 'patch'}, 'must be an object'),
        ]
        for call, expected in cases:
            with self.subTest(call=call):
                backend = Backend([UpstreamReply(encode_calls([call])), valid])
                result = TextActionRelay(backend, 1, 4096).run(RelayRequest(
                    model='test', messages=(TextMessage('user', 'edit'),), tools=(EDIT, other),
                ))
                self.assertEqual(result.action.calls[0].parameters, {'input': 'patch'})
                feedback = backend.requests[1].messages[-1].content
                self.assertIn(expected, feedback)
                self.assertIn(json.dumps(EDIT.parameters, separators=(',', ':')), feedback)
                self.assertNotIn('unrelated', feedback)
                self.assertNotIn('secret sample', feedback)

    def test_http_length_result_is_not_executed_or_retried_as_bad_json(self):
        valid = encode_calls([{'operation': 'edit', 'parameters': {'input': 'patch'}}])
        for content in ('', '@@ACTION@@{', valid):
            with self.subTest(content=content):
                class Response:
                    status_code = 200

                    def json(self):
                        return {'choices': [{'message': {'content': content, 'reasoning_content': 'thinking'}, 'finish_reason': 'length'}]}

                class Session:
                    count = 0

                    def post(self, *args, **kwargs):
                        self.count += 1
                        return Response()

                session = Session()
                backend = UpstreamClient(RelayConfig(upstream_base_url='http://unused.invalid'), session)
                with self.assertRaisesRegex(ActionTransportError, 'finish_reason=length'):
                    TextActionRelay(backend, 1, 4096).run(RelayRequest(
                        model='test', messages=(TextMessage('user', 'edit'),), tools=(EDIT,),
                    ))
                self.assertEqual(session.count, 1)


if __name__ == '__main__':
    unittest.main()
