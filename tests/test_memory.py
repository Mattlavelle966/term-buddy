import tempfile
import unittest
from pathlib import Path

from term_buddy.memory import ProjectMemory


class MemoryTests(unittest.TestCase):
    def test_incremental_index_and_targeted_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "README.md").write_text("Example application")
            (root / "ScrapForm.vue").write_text("const formColor = 'blue'")
            memory = ProjectMemory(Path(directory) / "memory.sqlite3")
            first = memory.index(str(root))
            second = memory.index(str(root))
            context, sources = memory.retrieve(str(root), "change ScrapForm color", 4000)
            self.assertEqual(first.indexed, 2)
            self.assertEqual(second.unchanged, 2)
            self.assertEqual(sources[0], "ScrapForm.vue")
            self.assertIn("formColor", context)
            self.assertEqual(memory.root_for(str(root)), str(root))
