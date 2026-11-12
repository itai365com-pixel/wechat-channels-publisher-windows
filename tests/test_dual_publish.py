from pathlib import Path
from tempfile import TemporaryDirectory
import time
import tkinter as tk
from unittest import TestCase
from unittest.mock import patch

from video_channels_batch import BatchPublisher


class MultiPlatformTests(TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.base = Path(sandbox.name)
        try:
            self.window = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.window.withdraw()
        self.addCleanup(self.window.destroy)
        (self.base / "cookies").mkdir()
        (self.base / "cookies/tencent_creator.json").write_text("{}", encoding="utf-8")
        for patcher in (
            patch("video_channels_batch.ROOT", self.base),
            patch("video_channels_gui.ROOT", self.base),
            patch("video_channels_batch.PATH_PREFS", self.base / "paths.json"),
            patch("video_channels_batch.BATCH_DIR", self.base / "batches"),
            patch("tiktok_gui.ROOT", self.base),
            patch("batch_preview.check_video_frame", return_value=""),
            patch("tiktok_queue.check_video_frame", return_value=""),
        ):
            self.enterContext(patcher)
        self.app = BatchPublisher(self.window, auto_preview=False)
        self.addCleanup(lambda: self.window.after_cancel(self.app.poll_id))
        self.wechat_source = self.write_source("wechat", "视频文件名,短标题,简介\nwx001.mp4,原始短标题,视频号简介\n", ("wx001.mp4",))
        self.tiktok_source = self.write_source("tiktok", "视频文件名,TikTok 文案,曼谷发布时间\n"
                                               "tk001.mp4,คำทดสอบหนึ่ง,2030-11-12 18:30\n"
                                               "tk002.mp4,คำทดสอบสอง,\n", ("tk001.mp4", "tk002.mp4"))
        self.app.folder.set(str(self.wechat_source.parent))
        self.app.table_path.set(str(self.wechat_source))
        self.app.preview()
        self.wait_for(lambda: not self.app.preview_loading and len(self.app.items) == 1)
        self.app.items[0]["short_title"] = "编辑后的短标题"
        self.app.platform.set("tiktok")
        self.app.switch_platform()
        self.tiktok = self.app.tiktok_panel
        self.tiktok.account.set("demo")
        self.tiktok.folder.set(str(self.tiktok_source.parent))
        self.tiktok.table.set(str(self.tiktok_source))
        self.tiktok.import_table()
        self.wait_for(lambda: not self.tiktok.busy and len(self.tiktok.active) == 2)
        self.tiktok.batch_count.set("1")
        self.app.platform.set("dual")
        self.app.switch_platform()
        self.panel = self.app.dual_page

    def write_source(self, platform, text, names):
        folder = self.base / platform
        folder.mkdir()
        for name in names:
            (folder / name).write_bytes(name.encode())
        table = folder / f"{platform}.csv"
        table.write_text(text, encoding="utf-8")
        return table

    def wait_for(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.window.update()
            time.sleep(.015)
        self.assertTrue(condition())

    def test_dispatch_uses_each_platforms_loaded_copy_and_account(self):
        self.assertTrue(self.panel.publish_button.instate(("disabled",)))
        self.panel.enabled["wechat"].set(True)
        self.panel.enabled["tiktok"].set(True)
        with (patch.object(self.app, "start", return_value=True) as wechat_start,
              patch.object(self.tiktok, "start_studio") as tiktok_start,
              patch("dual_publish_gui.messagebox.askyesno", return_value=True) as confirm):
            # A changed, unimported TikTok path cannot accidentally start its old queue or WeChat.
            self.tiktok.table.set(str(self.base / "different.csv"))
            self.panel.publish()
            confirm.assert_not_called()
            wechat_start.assert_not_called()
            tiktok_start.assert_not_called()
            self.assertIn("重新读取", self.panel.summary.get())

            self.tiktok.table.set(str(self.tiktok_source))
            self.panel.publish()

        confirm.assert_called_once()
        wechat_start.assert_called_once_with([], "批量发布")
        tiktok_start.assert_called_once_with(self.tiktok.store, [self.tiktok.active[0]])
        record = self.app.manifest
        self.assertEqual(record["account"], "creator")
        self.assertEqual(Path(record["source"]), self.wechat_source)
        self.assertEqual(Path(record["items"][0]["file"]).name, "wx001.mp4")
        self.assertEqual(record["items"][0]["short_title"], "编辑后的短标题")
        queue = self.tiktok.store.read()
        self.assertEqual(queue["account"], "demo")
        self.assertEqual(Path(queue["table"]), self.tiktok_source)
        self.assertEqual(queue["items"][self.tiktok.active[0]]["caption"], "คำทดสอบหนึ่ง")
        self.assertEqual(queue["items"][self.tiktok.active[0]]["scheduled_at"], "2030-11-12T18:30:00+07:00")

    def test_only_selected_platform_starts_and_wechat_resume_keeps_receipt(self):
        self.panel.enabled["wechat"].set(True)
        with (patch.object(self.app, "start", return_value=True),
              patch.object(self.tiktok, "start_studio") as tiktok_start,
              patch("dual_publish_gui.messagebox.askyesno", return_value=True)):
            self.panel.publish()
        tiktok_start.assert_not_called()
        path = self.app.manifest_path
        self.app.manifest.update(status="paused", next_round_at="2030-11-12T21:00:00+07:00")
        self.app.save_manifest()
        with patch("channels_parallel.ParallelPublisher.preflight", side_effect=lambda: self.app.manifest["items"]):
            plan = self.app.prepare_batch(continue_loaded=True)
        self.assertEqual(plan[2], path)
        self.assertEqual(plan[1]["next_round_at"], "2030-11-12T21:00:00+07:00")

