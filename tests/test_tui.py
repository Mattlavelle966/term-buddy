import unittest

from term_buddy.tui import BuddyUI


class BuddyUiTests(unittest.TestCase):
    def test_project_learning_triggers(self):
        for prompt in [
            "learn project", "Learn my project!", "please index this project", "scan the project",
        ]:
            with self.subTest(prompt=prompt):
                self.assertTrue(BuddyUI.is_project_trigger(prompt))
        self.assertFalse(BuddyUI.is_project_trigger("how does this project work?"))
