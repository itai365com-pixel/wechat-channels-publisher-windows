import asyncio
import csv
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from youtube_batch import ReceiptBook, read_items, run_batch
from youtube_batch_studio import upload_bytes_complete


class FakeStudio:
    def __init__(self, fail=False, stop=None):
        self.fail = fail
        self.stop = stop
        self.uploaded = []
        self.verified = []

    async def upload(self, item, visibility, audience, reserve, identify, stop):
        reserve()
        identify("sampleID001" if item["title"] == "你好" else "sampleID002")
        self.uploaded.append(item["title"])
        if self.stop:
            self.stop.set()
        if self.fail:
            raise OSError("network lost")

    async def verify(self, record):
        self.verified.append(record.copy())
        return True


class YouTubeBatchTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.path = Path(work.name)
        self.book = ReceiptBook(self.path, "UC" + "1" * 22)

    def make_item(self, filename, data, title):
        path = self.path / filename
        path.write_bytes(data)
        return dict(file=str(path), digest=sha256(data).hexdigest(), title=title, desc="สวัสดี #Shorts")

    def run_queue(self, studio, items, stop=None):
        return asyncio.run(run_batch(studio, self.book, items, "unlisted", "no",
                                     stop or Event(), lambda _: None))

    def test_multiline_csv_uses_exact_file_and_preserves_copy(self):
        self.make_item("lesson001.mp4", b"one", "你好")
        table = self.path / "文案.csv"
        with table.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows([
                ["文件名", "标题", "简介", "标签"],
                ["lesson001.mp4", "你好", "สวัสดี\n#Shorts", "中文,ภาษาไทย"],
            ])
        _, rows = read_items(self.path, table)
        self.assertEqual(rows[0]["desc"], "สวัสดี\n#Shorts")
        self.assertEqual(rows[0]["tags"], ["中文", "ภาษาไทย"])

    def test_interrupted_upload_reconciles_and_never_resubmits(self):
        item = self.make_item("lesson001.mp4", b"one", "你好")
        studio = FakeStudio(fail=True)
        with self.assertRaisesRegex(RuntimeError, "待核对"):
            self.run_queue(studio, [item])
        self.assertEqual(self.book.read()[item["digest"]]["status"], "checking")
        studio = FakeStudio()
        changed = {**item, "desc": "新文案"}
        self.assertEqual(self.run_queue(studio, [changed]), 1)
        self.assertEqual(studio.uploaded, [])
        self.assertEqual(studio.verified[0]["description"], item["desc"])
        studio = FakeStudio()
        self.assertEqual(self.run_queue(studio, [item]), 1)
        self.assertEqual(studio.uploaded + studio.verified, [])

    def test_pause_verifies_current_and_does_not_start_next(self):
        items = [self.make_item("lesson001.mp4", b"one", "你好"), self.make_item("lesson002.mp4", b"two", "再见")]
        stop = Event()
        studio = FakeStudio(stop=stop)
        self.assertEqual(self.run_queue(studio, items, stop), 1)
        self.assertEqual(studio.uploaded, ["你好"])
        self.assertEqual(set(self.book.read()), {items[0]["digest"]})

    def test_studio_copyright_progress_is_after_file_upload(self):
        self.assertFalse(upload_bytes_complete("正在上传：2% ... 还剩 10 分钟"))
        self.assertTrue(upload_bytes_complete("上传完毕 ... 即将开始处理"))
        self.assertTrue(upload_bytes_complete("正在检查：2% ... 还剩 10 分钟"))
        self.assertTrue(upload_bytes_complete("检查完毕。未发现任何问题。"))
