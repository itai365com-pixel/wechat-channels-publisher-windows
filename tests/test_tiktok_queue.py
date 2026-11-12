import csv
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from publisher_options import BANGKOK
from tiktok_queue import TikTokQueue, file_signature, read_tiktok_items, write_template


class TikTokQueueTests(TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.queue = TikTokQueue(self.root / "queues", "@demo")
        self.now = datetime(2026, 11, 12, 8, tzinfo=BANGKOK)

    def row(self, name, schedule=None):
        path = self.root / (name + ".mp4")
        path.write_bytes(name.encode())
        return {"file": str(path), "signature": file_signature(path), "caption": "เรียนภาษาจีน",
                "scheduled_at": schedule.isoformat() if schedule else "", "status": "pending", "error": ""}

    def load(self, items):
        return self.queue.import_items(items, folder=self.root, table=self.root / "copy.csv", sheet="copy")

    def test_hour_limit_and_release_unsubmitted_slot(self):
        rows = {name: self.row(name, self.now + timedelta(minutes=offset))
                for name, offset in (("late", 80), ("early", 30), ("middle", 60), ("boundary", 90))}
        self.load(rows)
        self.queue.begin("late", now=self.now)
        self.queue.begin("early", now=self.now)
        with self.assertRaisesRegex(ValueError, "连续 60 分钟"):
            self.queue.begin("middle", now=self.now)
        self.queue.begin("boundary", now=self.now)
        self.queue.confirm("early", "pending")
        self.queue.confirm("boundary", "pending")
        self.queue.begin("middle", now=self.now)

    def test_daily_limit_uses_bangkok_and_account_queue(self):
        rows = {str(i): self.row(str(i), self.now + timedelta(hours=i + 1)) for i in range(11)}
        rows["tomorrow"] = self.row("tomorrow", self.now + timedelta(days=1))
        self.load(rows)
        for i in range(10):
            self.queue.begin(str(i), now=self.now)
            self.queue.confirm(str(i), "scheduled")
        with self.assertRaisesRegex(ValueError, "已占用 10 条"):
            self.queue.begin("10", now=self.now)
        self.queue.begin("tomorrow", now=self.now)
        self.assertEqual(TikTokQueue(self.root / "queues", "other").read()["active"], [])

    def test_hour_limit_also_spans_midnight(self):
        midnight = datetime(2026, 11, 13, tzinfo=BANGKOK)
        self.load({str(i): self.row(str(i), midnight + timedelta(minutes=i * 10))
                   for i in (-1, 0, 1)})
        self.queue.begin("-1", now=self.now)
        self.queue.begin("0", now=self.now)
        with self.assertRaisesRegex(ValueError, "连续 60 分钟"):
            self.queue.begin("1", now=self.now)

    def test_reimport_preserves_confirmed_and_uncertain_rows(self):
        first, second = self.row("first"), self.row("second")
        self.load({"first": first, "second": second})
        self.queue.begin("first", now=self.now)
        self.queue.confirm("first", "published")
        self.queue.begin("second", now=self.now)
        state = self.load({"first": dict(first, caption="new"), "second": dict(second, caption="new")})
        self.assertEqual(state["items"]["first"]["status"], "published")
        self.assertEqual(state["items"]["second"]["status"], "checking")
        self.assertEqual(state["items"]["first"]["caption"], first["caption"])
        with self.assertRaisesRegex(ValueError, "已有处理记录"):
            self.queue.begin("second", now=self.now)

    def test_thai_caption_and_tags_from_template(self):
        table = self.root / "example.csv"
        write_template(table)
        self.row("lesson001")
        self.row("lesson002")
        with patch("tiktok_queue.check_video_frame", return_value=""):
            _, items = read_tiktok_items(self.root, table)
        self.assertEqual(len(items), 2)
        first = next(iter(items.values()))
        self.assertIn("สวัสดี", first["caption"])
        self.assertTrue(first["caption"].endswith("#เรียนภาษาจีน #ภาษาจีน"))
        self.assertEqual(first["scheduled_at"], "")

    def test_tiktok_copy_export_with_inline_tags_and_no_schedule(self):
        table = self.root / "sample_tiktok_captions.csv"
        caption = "ตัวอย่างบทเรียน: คำว่า 猫 แปลว่า แมว\n\n#เรียนภาษาจีน #ภาษาจีน"
        with table.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows([
                ("集", "视频文件", "TikTok 文案", "字数"),
                ("lesson003", "lesson003.mp4", caption, len(caption)),
            ])
        self.row("lesson003")
        with patch("tiktok_queue.check_video_frame", return_value=""):
            _, items = read_tiktok_items(self.root, table)
        row, = items.values()
        self.assertEqual(Path(row["file"]).name, "lesson003.mp4")
        self.assertEqual(row["caption"], caption)
        self.assertEqual(row["scheduled_at"], "")
