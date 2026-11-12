from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase

from publisher_options import BANGKOK
from tiktok_batch import next_slot, run_batch
from tiktok_queue import TikTokQueue, file_signature
from tiktok_studio import StudioPost


class FakeStudio:
    def __init__(self):
        self.entries = []
        self.uploads = []
        self.submits = 0
        self.fail_after_submit = False

    def posts(self):
        return self.entries.copy()

    def prepare(self, file, caption):
        self.uploads.append((file, caption))

    def submit(self):
        self.submits += 1
        self.entries.append(StudioPost(f"https://www.tiktok.com/@demo/video/{self.submits}",
                                       self.uploads[-1][1], True))
        if self.fail_after_submit:
            raise ConnectionError("Browser disconnected after clicking")


class TikTokBatchTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = TikTokQueue(self.root / "queues", "demo")
        self.now = datetime(2026, 11, 12, 10, 0, tzinfo=BANGKOK)

    def load(self, *keys):
        items = {}
        for key in keys:
            file = self.root / f"{key}.mp4"
            file.write_bytes(key.encode())
            items[key] = {"file": str(file), "signature": file_signature(file), "caption": f"{key}\n#เรียนจีน",
                          "scheduled_at": "", "status": "pending", "error": ""}
        self.store.import_items(items, folder=self.root, table=self.root / "copy.csv", sheet="copy")

    def run_queue(self, studio, keys, stop=None):
        return run_batch(self.store, keys, studio, stop or Event(), lambda *_a, **_kw: None, now=lambda: self.now)

    def test_resume_verifies_submitted_item_without_second_click(self):
        self.load("first", "second")
        studio = FakeStudio()
        studio.fail_after_submit = True
        with self.assertRaises(ConnectionError):
            self.run_queue(studio, ["first", "second"])
        self.assertEqual(self.store.read()["items"]["first"]["status"], "checking")
        studio.fail_after_submit = False
        self.run_queue(studio, ["first", "second"])
        self.assertEqual(studio.submits, 2)
        self.assertEqual([Path(file).stem for file, _ in studio.uploads], ["first", "second"])
        self.assertTrue(all(row["status"] == "published" for row in self.store.read()["items"].values()))

    def test_known_caption_and_unknown_submit_do_not_upload(self):
        self.load("first")
        studio = FakeStudio()
        studio.entries.append(StudioPost("https://www.tiktok.com/@demo/video/99", "first #เรียนจีน", True))
        self.assertIn("相同文案", self.run_queue(studio, ["first"]))
        self.assertFalse(studio.uploads)
        studio.entries.clear()
        studio.fail_after_submit = True
        with self.assertRaises(ConnectionError):
            self.run_queue(studio, ["first"])
        studio.entries.clear()
        self.assertIn("待核对", self.run_queue(studio, ["first"]))
        self.assertEqual(studio.submits, 1)

    def test_next_slot_respects_rolling_hour_and_bangkok_day(self):
        now = self.now
        hourly = {str(i): {"reserved_at": (now - timedelta(minutes=i)).isoformat()} for i in (10, 20)}
        self.assertEqual(next_slot(hourly, now), now + timedelta(minutes=40))
        daily = {str(i): {"reserved_at": (now.replace(hour=0) + timedelta(hours=i)).isoformat()} for i in range(10)}
        self.assertEqual(next_slot(daily, now), (now + timedelta(days=1)).replace(hour=0))
