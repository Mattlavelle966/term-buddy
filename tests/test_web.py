import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from term_buddy.config import Config
from term_buddy.web import WebClient, WebError, _validate_public_url, ensure_searxng


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class WebTests(unittest.TestCase):
    def test_search_returns_structured_urls_and_snippets(self):
        payload = {"results": [{
            "title": "Nuxt directory structure",
            "url": "https://nuxt.com/docs/guide/directory-structure",
            "content": "Nuxt provides an opinionated directory structure.",
        }]}
        with patch("term_buddy.web.urllib.request.urlopen", return_value=Response(json.dumps(payload).encode())):
            result = WebClient("http://127.0.0.1:8888").search("Nuxt project structure")
        self.assertIn("Nuxt directory structure", result)
        self.assertIn("https://nuxt.com/docs/guide/directory-structure", result)

    def test_running_container_is_reused_not_duplicated(self):
        running = subprocess.CompletedProcess([], 0, "true\n", "")
        with patch("term_buddy.web.shutil.which", return_value="/usr/bin/docker"), patch(
            "term_buddy.web._docker", return_value=running,
        ) as docker, patch("term_buddy.web._wait_ready"):
            started = ensure_searxng(Config(web=True))
        self.assertFalse(started)
        docker.assert_called_once()

    def test_missing_container_is_created_with_loopback_port(self):
        missing = subprocess.CompletedProcess([], 1, "", "missing")
        created = subprocess.CompletedProcess([], 0, "container-id\n", "")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}
        ), patch("term_buddy.web.shutil.which", return_value="/usr/bin/docker"), patch(
            "term_buddy.web._docker", side_effect=[missing, created],
        ) as docker, patch("term_buddy.web._wait_ready"):
            started = ensure_searxng(Config(web=True))
        self.assertTrue(started)
        run_args = docker.call_args_list[1].args[0]
        self.assertIn("term-buddy-searxng", run_args)
        self.assertIn("127.0.0.1:8888:8080", run_args)
        self.assertIn("unless-stopped", run_args)

    def test_fetch_blocks_private_network_destinations(self):
        address = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch("term_buddy.web.socket.getaddrinfo", return_value=address), self.assertRaises(WebError):
            _validate_public_url("http://localhost/private")
