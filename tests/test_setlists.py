"""Regression tests for TrackID setlist handling (no hardware or API key required)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "trackid"))
from trackid import App, SetStore  # noqa: E402


class SetlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SetStore(self.tmp.name)

    def track(self, name, ts):
        return {"status": "matched", "artist": "DJ Test",
                "title": name, "ts": ts, "bpm": 128.0}

    def make_app_stub(self):
        # Exercise the real App._finish() without invoking a screen or AudD.
        app = Mock()
        app.store = self.store
        app._apply_result.side_effect = lambda entry, result: entry.update(result)
        return app

    def test_auto_ignores_consecutive_duplicate(self):
        app = self.make_app_stub()
        self.store.add(self.track("Alpha", 100))
        App._finish(app, {"artist": "DJ Test", "title": "Alpha"},
                    {"ts": 150, "via": "auto"}, self.store.set_id, False)
        self.assertEqual(len(self.store.tracks()), 1)

    def test_auto_allows_reprise_after_another_track(self):
        app = self.make_app_stub()
        self.store.add(self.track("Alpha", 100))
        self.store.add(self.track("Beta", 200))
        App._finish(app, {"artist": "DJ Test", "title": "Alpha"},
                    {"ts": 300, "via": "auto"}, self.store.set_id, False)
        self.assertEqual([x["title"] for x in self.store.tracks()],
                         ["Alpha", "Beta", "Alpha"])

    def test_auto_does_not_consider_case_variants_new_tracks(self):
        app = self.make_app_stub()
        self.store.add(self.track("Alpha", 100))
        App._finish(app, {"artist": "dj test", "title": "ALPHA"},
                    {"ts": 150, "via": "auto"}, self.store.set_id, False)
        self.assertEqual(len(self.store.tracks()), 1)

    def test_manual_consecutive_duplicate_is_marked(self):
        app = self.make_app_stub()
        self.store.add(self.track("Alpha", 100))
        App._finish(app, {"artist": "DJ Test", "title": "Alpha"},
                    {"ts": 150, "via": "tap"}, self.store.set_id, True)
        self.assertEqual(len(self.store.tracks()), 1)
        self.assertEqual(self.store.entries()[-1]["status"], "duplicate")

    def test_plain_export_contains_formatted_bpm(self):
        self.store.add(self.track("Alpha", self.store.started() + 30))
        output = self.store.export_text("plain")
        self.assertIn("DJ Test - Alpha  (128 BPM)", output)

    def test_plain_export_handles_missing_bpm(self):
        e = self.track("Alpha", self.store.started() + 30)
        e["bpm"] = None
        self.store.add(e)
        output = self.store.export_text("plain")
        self.assertIn("DJ Test - Alpha", output)
        self.assertNotIn("(None BPM)", output)

    def test_youtube_export_uses_intro_at_zero(self):
        self.store.add(self.track("Alpha", self.store.started() + 40))
        output = self.store.export_text("youtube")
        self.assertTrue(output.startswith("00:00 Intro"))
        self.assertIn("00:40 DJ Test - Alpha", output)


if __name__ == "__main__":
    unittest.main()
