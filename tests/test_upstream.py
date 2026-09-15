import unittest

from relay.config import RelayConfig
from relay.engine import TextCompletionRequest, TextMessage
from relay.upstream import UpstreamClient


class Response:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


class Session:
    def __init__(self):
        self.post_call = None

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return Response()


class UpstreamTests(unittest.TestCase):
    def test_http_backend_sends_only_plain_chat_fields(self):
        session = Session()
        config = RelayConfig(
            upstream_base_url="http://first-hop.invalid",
            upstream_api_key="test-only-upstream-key",
        )
        client = UpstreamClient(config, session=session)
        request = TextCompletionRequest(
            model="model",
            messages=(TextMessage(role="user", content="plain text"),),
            max_tokens=123,
            sampling={"temperature": 0.2},
        )
        reply = client.complete(request)

        self.assertEqual(reply.content, "ok")
        url, kwargs = session.post_call
        self.assertEqual(url, "http://first-hop.invalid/v1/chat/completions")
        self.assertEqual(set(kwargs["json"]), {"model", "messages", "stream", "max_tokens", "temperature"})
        self.assertNotIn("tools", kwargs["json"])
        self.assertTrue(all(message["role"] != "system" for message in kwargs["json"]["messages"]))


if __name__ == "__main__":
    unittest.main()
