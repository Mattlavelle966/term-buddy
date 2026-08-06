import io
import json
import unittest
from unittest.mock import patch

from term_buddy.config import Config
from term_buddy.model import ModelClient


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ModelTests(unittest.TestCase):
    def test_openai_compatible_response(self):
        payload = {"choices": [{"message": {"content": "  useful answer  "}}]}
        with patch("urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())):
            answer = ModelClient(Config()).complete([{"role": "user", "content": "hi"}])
        self.assertEqual(answer, "useful answer")

    def test_large_context_uses_long_timeout(self):
        payload = {"choices": [{"message": {"content": "answer"}}]}
        config = Config(context_window_tokens=100, request_timeout=10, long_context_timeout=321)
        with patch("urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())) as request:
            ModelClient(config).ask("question", "x" * 240)
        self.assertEqual(request.call_args.kwargs["timeout"], 321)
