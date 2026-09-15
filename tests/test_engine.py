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


if __name__ == "__main__":
    unittest.main()
