import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from channels_parallel import ParallelPublisher
from daily_publish_limit import DailyPublishBudget
from publisher_options import normalize_short_title
from video_channels_batch import filter_previously_completed, fingerprint, last_publish_batch, video_digest


class PublishHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.batches = self.root / '.local/gui/batches'
        self.batches.mkdir(parents=True)
        for target, value in [('video_channels_batch.BATCH_DIR', self.batches), ('channels_parallel.BASE_DIR', self.root)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.original = self.video('original.mp4', b'first synthetic video')
        self.copy = self.video('copy.mp4', b'first synthetic video')
        self.fresh = self.video('fresh.mp4', b'second synthetic video')

    def video(self, filename, content):
        path = self.root / filename
        path.write_bytes(content)
        return {'file':str(path), 'title':filename, 'desc':f'示例简介 {filename}'}

    def manifest(self, name, items, *, account='demo', draft=False, created='2026-05-12T10:00:00+07:00'):
        path = self.batches / (name+'.json')
        path.write_text(json.dumps({'account':account, 'draft':draft, 'items':items, 'created_at':created,
                                    'options':{'declare_original':False}}),encoding='utf-8')
        return path

    def receipt(self, *, status='completed', verified=True, digest=True):
        row = {**self.original, 'status':status, 'fingerprint':fingerprint(self.original,'demo',False),
               'finished_at':'2026-05-12T10:30:00+07:00', 'platform_verification':{'verified':verified}}
        if digest:
            row['video_sha256'] = video_digest(self.original['file'])
        return row

    def test_restarted_new_batch_skips_same_video_after_rename_and_copy_change(self):
        self.manifest('last', [self.receipt()])
        pending, skipped = filter_previously_completed([self.copy,self.fresh],'DEMO',False)
        self.assertEqual([row['file'] for row in pending], [self.fresh['file']])
        self.assertEqual([row['file'] for row in skipped], [self.copy['file']])
        self.assertEqual(len(filter_previously_completed([self.copy],'other',False)[0]),1)

    def test_unknown_result_blocks_copy_and_legacy_fingerprint_still_skips(self):
        self.manifest('unknown', [self.receipt(status='unknown',verified=False)])
        with self.assertRaisesRegex(ValueError,'待核对'):
            filter_previously_completed([self.copy],'demo',False)
        self.manifest('unknown', [self.receipt(digest=False)])
        self.assertEqual(len(filter_previously_completed([self.original],'demo',False)[1]),1)

    def test_last_record_uses_actual_attempt_and_ignores_drafts_and_pending(self):
        expected = self.manifest('latest-run',[self.receipt()])
        self.manifest('new-draft',[self.receipt()],draft=True,created='2026-05-13T12:00:00+07:00')
        self.manifest('new-queue',[{**self.fresh,'status':'pending'}],created='2026-05-13T13:00:00+07:00')
        before = expected.read_bytes()
        self.assertEqual(last_publish_batch('demo')[1],expected)
        self.assertEqual(expected.read_bytes(),before)

    async def test_resuming_old_queue_skips_verified_video_without_browser_or_new_quota(self):
        self.manifest('last',[self.receipt()])
        row = {**self.copy,'status':'pending','fingerprint':fingerprint(self.copy,'demo',False)}
        path = self.manifest('old-queue',[row])
        original = path.read_bytes()
        runner = ParallelPublisher(path,event=lambda *_:None)
        self.assertEqual(runner.preflight(),[])
        self.assertEqual(path.read_bytes(),original)
        before = DailyPublishBudget(self.root).usage('demo')['used']
        with patch.object(runner,'run_round',new_callable=AsyncMock) as upload:
            self.assertEqual(await runner.run(),0)
            upload.assert_not_awaited()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['items'][0]['status'],'skipped_completed')
        self.assertEqual(DailyPublishBudget(self.root).usage('demo')['used'],before)

    def test_duplicate_copies_inside_fresh_selection_are_blocked(self):
        with self.assertRaisesRegex(ValueError,'本批另一视频内容相同'):
            filter_previously_completed([self.original,self.copy],'demo',False)

    def test_invalid_short_title_is_detected_before_upload_and_repair_preserves_description(self):
        item = {**self.fresh, 'short_title':'示例课程 第一期 欢迎，回来',
                'desc':'示例简介，保留逗号 #示例话题', 'status':'pending'}
        item['fingerprint'] = fingerprint(item, 'demo', False)
        path = self.manifest('comma-title', [item])
        runner = ParallelPublisher(path)
        before = path.read_bytes()
        with patch('channels_parallel.TencentVideo.validate_video_file') as validate:
            with self.assertRaisesRegex(ValueError, '逗号'):
                runner.preflight()
            validate.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        row = runner.record['items'][0]
        original_desc = row['desc']
        row['short_title'] = normalize_short_title(row['short_title'])
        row['fingerprint'] = fingerprint(row, 'demo', False)
        with patch('channels_parallel.TencentVideo.validate_video_file'):
            self.assertEqual(runner.preflight(), [row])
        self.assertEqual(row['desc'], original_desc)
        self.assertEqual(row['short_title'], '示例课程 第一期 欢迎 回来')

    def test_previously_published_video_skips_even_with_old_comma_title(self):
        self.manifest('last', [self.receipt()])
        item = {**self.copy, 'short_title':'已发布的旧标题，继续跳过', 'status':'pending'}
        item['fingerprint'] = fingerprint(item, 'demo', False)
        path = self.manifest('old-title', [item])
        self.assertEqual(ParallelPublisher(path).preflight(), [])
