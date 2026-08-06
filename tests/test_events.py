import tempfile
import unittest
from pathlib import Path

from term_buddy.events import append_event, read_events


class EventTests(unittest.TestCase):
    def test_event_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            written = append_event(path, "question", {"message": "hello"})
            events, offset = read_events(path)
            self.assertEqual(events[0]["id"], written["id"])
            self.assertEqual(events[0]["message"], "hello")
            self.assertGreater(offset, 0)
            self.assertEqual(read_events(path, offset)[0], [])

