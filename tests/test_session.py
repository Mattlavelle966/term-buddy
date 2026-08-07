import subprocess
import unittest
from unittest.mock import patch

from term_buddy.config import Config
from term_buddy.session import Session


class SessionTests(unittest.TestCase):
    def test_splash_hook_is_one_shot_and_zooms_buddy_pane(self):
        session = Session(Config(), "term-buddy")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("term_buddy.session.Session._tmux", return_value=completed) as tmux:
            session._install_splash_hook("%7")
        args = tmux.call_args.args
        self.assertEqual(args[:4], ("set-hook", "-t", "term-buddy", "client-attached"))
        self.assertIn("set-hook -u -t term-buddy client-attached", args[4])
        self.assertIn("resize-pane -Z -t %7", args[4])
