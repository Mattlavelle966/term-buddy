import unittest

from term_buddy.diagnostics import plan_diagnostics


class DiagnosticTests(unittest.TestCase):
    def test_common_intents_have_deterministic_commands(self):
        self.assertEqual(
            plan_diagnostics("tell me about the last 2 commits"),
            ["git log --stat --oneline -2"],
        )
        self.assertIn("git diff", plan_diagnostics("what uncommitted changes exist?"))
        self.assertEqual(plan_diagnostics("explain this Vue component"), [])
