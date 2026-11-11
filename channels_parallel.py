"""Two concurrent upload tabs, serial submission, and durable publishing rounds."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime
import json
import hashlib
from pathlib import Path
import threading

from conf import BASE_DIR
from patchright.async_api import async_playwright
from loguru import logger
from publisher_lock import AccountLock
from publisher_options import cli_schedule, short_title_issues
from batch_table import description_with_tags
from daily_publish_limit import BANGKOK, DailyPublishBudget, HOURLY_LIMIT, PublishLimitReached, ROUND_INTERVAL, SUBMIT_PHASES
from uploader.tencent_uploader.main import (
    TencentVideo, cookie_auth, _build_launch_kwargs,
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE, TENCENT_PUBLISH_STRATEGY_SCHEDULED,
)


MAX_TABS = 2


class ReadyFirstScheduler:
    """A fixed number of slots, FIFO ready order, and a single submit lock."""

    def __init__(self, width=MAX_TABS, stop=None):
        if not 1 <= width <= MAX_TABS:
            raise ValueError('并行标签数量必须为 1 或 2')
        self.width = width
        self.stop = stop or threading.Event()
        self.halt = asyncio.Event()
        self.submit_lock = asyncio.Lock()

    async def run(self, jobs, prepare, publish, failure, cancelled, last=()):
        jobs = list(jobs)
        queue = deque(row for row in jobs if Path(row['file']).name not in last)
        tail = deque(row for row in jobs if Path(row['file']).name in last)
        condition = asyncio.Condition()
        active = 0
        failed = False

        async def take():
            nonlocal active
            async with condition:
                while True:
                    if self.stop.is_set() or self.halt.is_set():
                        return None
                    if queue:
                        active += 1
                        return queue.popleft()
                    if active:
                        await condition.wait()
                        continue
                    if tail and not failed:
                        queue.extend(tail)
                        tail.clear()
                        continue
                    return None

        async def worker(slot):
            nonlocal active, failed
            while (row := await take()) is not None:
                try:
                    prepared = await prepare(row, slot)
                    # No awaits between prepare completion and joining the fair
                    # lock: cover readiness determines the submission order.
                    async with self.submit_lock:
                        if self.halt.is_set():
                            await cancelled(row)
                            return
                        await publish(row, prepared, slot)
                except asyncio.CancelledError:
                    await cancelled(row)
                    raise
                except Exception as exc:
                    failed = True
                    try:
                        fatal = await failure(row, exc)
                    except BaseException:
                        self.halt.set()
                        raise
                    if fatal:
                        self.halt.set()
                finally:
                    async with condition:
                        active -= 1
                        condition.notify_all()

        workers = [asyncio.create_task(worker(slot)) for slot in range(min(self.width, len(jobs)))]
        if not workers:
            return
        group = asyncio.gather(*workers, return_exceptions=True)
        halted = asyncio.create_task(self.halt.wait())
        try:
            await asyncio.wait((group, halted), return_when=asyncio.FIRST_COMPLETED)
            if self.halt.is_set():
                for task in workers:
                    if not task.done():
                        task.cancel()
            results = await group
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        finally:
            halted.cancel()
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def normalized_copy(text):
    return str(text).replace('\r\n', '\n').replace('\xa0', ' ').replace('\u200b', '').strip()


async def verify_form(page, app, row):
    """Check page-local copy again after all dialogs, before the first click."""
    title = page.locator('input[placeholder="填写短标题有机会获得更多流量"]').first
    if not await title.count():
        title = page.get_by_text('短标题', exact=True).locator('..').locator('xpath=following-sibling::div').locator('span input[type="text"]').first
    expected_title = (row.get('short_title') or row['title']).strip()
    if not await title.count() or (await title.input_value()).strip() != expected_title:
        raise RuntimeError('短标题与本标签任务不一致，尚未点击发表')
    editor = page.locator('div.input-editor').first
    expected_desc = row.get('publish_desc', description_with_tags(row))
    if not await editor.count() or normalized_copy(await editor.inner_text()) != normalized_copy(expected_desc):
        raise RuntimeError('简介与本标签任务不一致，尚未点击发表')
    if app.require_collection:
        await app.verify_collection(page)
    if app.declare_original:
        original = page.locator('.declare-original-checkbox input[type="checkbox"]').first
        if not await original.count() or not await original.is_checked():
            raise RuntimeError('原创声明未确认，尚未点击发表')


async def verify_video_file(page, path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    expected = digest.hexdigest()
    for video in await page.locator('video').all():
        if await video.evaluate('e=>e.currentSrc.startsWith("blob:")'):
            actual = await asyncio.wait_for(video.evaluate('''async e=>{
                const bytes=await (await fetch(e.currentSrc)).arrayBuffer();
                const digest=await crypto.subtle.digest('SHA-256',bytes);
                return Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
            }'''), timeout=20)
            if actual != expected:
                raise RuntimeError('标签内视频内容与指定文件不一致，尚未点击发表')
            return actual
    raise RuntimeError('无法核验标签内视频文件，尚未点击发表')


class ParallelPublisher:
    def __init__(self, manifest_path, *, record=None, event=None, stop=None, tabs=MAX_TABS,
                 cover_timeout=1800, limit=None, debug_port=None, budget=None, sleep=asyncio.sleep):
        self.path = Path(manifest_path).resolve()
        self.record = record if record is not None else json.loads(self.path.read_text(encoding='utf-8'))
        self.event = event or (lambda kind, payload: print(payload, flush=True))
        self.stop = stop or threading.Event()
        if tabs < 1 or (limit is not None and limit < 1):
            raise ValueError('标签数量和任务上限必须为正数')
        self.tabs, self.cover_timeout, self.limit = min(tabs, MAX_TABS), cover_timeout, limit
        self.debug_port = debug_port
        self.root = Path(BASE_DIR)
        self.logs = self.root / '.local/gui/logs'
        self.logs.mkdir(parents=True, exist_ok=True)
        self.pages = []
        self.apps = {}
        self.sinks = {}
        self.started = set()
        self.indices = {id(row): i for i, row in enumerate(self.record['items'])}
        self.budget = budget or DailyPublishBudget(self.root)
        self.sleep = sleep
        self.limit_reached = False

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.record, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def emit(self, row, label):
        from video_channels_batch import FINISHED_ITEMS

        message = f'[{Path(row["file"]).name}] {label}'
        self.event('log', message + '\n')
        done = sum(item.get('status') in FINISHED_ITEMS for item in self.record['items'])
        self.event('batch_row', (self.indices[id(row)], label, f'已完成 {done}/{len(self.record["items"])} · 最多{self.tabs}标签并行'))
        if row.get('log'):
            with Path(row['log']).open('a', encoding='utf-8') as stream:
                stream.write(f'{datetime.now().isoformat()} {message}\n')

    def preflight(self, *, reconcile=False):
        from video_channels_batch import FINISHED_ITEMS, fingerprint, previous_completion, publication_history, video_digest
        if self.record.get('requires_backend_audit'):
            raise ValueError('旧成功记录尚未经过后台核对，请先完成结果核对；不能直接继续或重传。')
        settings = self.record['options']
        if settings['declare_original'] and not self.record.get('original_terms_batch_confirmed_at'):
            raise ValueError('本批尚未确认原创协议，不能自动勾选')
        pending = []
        files = set()
        digests = set()
        matches = []
        account = self.record['account']
        draft = bool(self.record.get('draft'))
        directories = dict.fromkeys((self.root / '.local/gui/batches', self.path.parent))
        history = publication_history(account, draft, directories=directories, exclude=self.path)
        for row in self.record['items']:
            file = str(Path(row['file']).resolve())
            if file in files:
                raise ValueError('队列包含重复视频文件')
            files.add(file)
            if row.get('status') in FINISHED_ITEMS:
                continue
            if row.get('status') not in ('pending', 'failed_before_submit'):
                raise ValueError(f'{Path(file).name} 结果待核对，不能重新上传')
            if row.get('phase') in SUBMIT_PHASES:
                raise ValueError(f'{Path(file).name} 存在点击发表记录，须核对后台结果后再处理')
            if row.get('decode_error'):
                raise ValueError(f'{Path(file).name} 视频检查未通过：{row["decode_error"]}')
            digest = video_digest(file)
            key = fingerprint(row, account, draft, digest=digest)
            if row['fingerprint'] != key:
                raise ValueError(f'{Path(file).name} 视频或文案与准备记录不一致')
            previous = previous_completion(history, file, digest, key)
            if previous:
                matches.append((row, previous))
                continue
            issues = short_title_issues(row.get('short_title') or row['title'])
            if issues:
                raise ValueError(f'{Path(file).name} 短标题需修改：' + '；'.join(issues))
            if digest in digests:
                raise ValueError(f'{Path(file).name} 与本批另一视频内容相同，请只选择一份。')
            digests.add(digest)
            TencentVideo.validate_video_file(file)
            pending.append(row)
        if reconcile and matches:
            for row, previous in matches:
                row.update(status='skipped_completed', phase='prior_publish_verified', previous_completion=previous)
            self.save()
            for row, _ in matches:
                self.emit(row, '已发布，自动跳过')
        pending.sort(key=lambda row: Path(row['file']).name in self.record.get('publish_last', []))
        return pending

    def app_for(self, row):
        options = self.record['options']
        scheduled = bool(row.get('schedule'))
        date = datetime.strptime(cli_schedule(row['schedule']), '%Y-%m-%d %H:%M') if scheduled else 0
        if scheduled:
            TencentVideo.validate_publish_date(date)
        app = TencentVideo(
            row['title'], row['file'], [], date, str(self.root / 'cookies' / f'tencent_{self.record["account"]}.json'),
            short_title=row.get('short_title') or row['title'], desc=row.get('publish_desc', description_with_tags(row)),
            description_only=True, collection_name=options['collection'], require_collection=bool(options['collection']),
            declare_original=options['declare_original'], content_label=options['content_label'],
            is_draft=self.record.get('draft', False), headless=False,
            publish_strategy=TENCENT_PUBLISH_STRATEGY_SCHEDULED if scheduled else TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        )
        app.quota_account = self.record['account']
        return app

    async def prepare(self, row, slot):
        self.started.add(id(row))
        if row.get('log'):
            row.setdefault('previous_attempts', []).append({key: row.get(key) for key in ('log', 'status', 'phase', 'returncode', 'started_at', 'finished_at', 'error', 'quota_reservation', 'submit_clicked_at')})
        for key in ('finished_at', 'quota_reservation', 'submit_clicked_at'):
            row.pop(key, None)
        row.pop('error', None)
        row.pop('uploaded_file', None)
        row.update(status='running', phase='preparing', slot=slot + 1, started_at=datetime.now().astimezone().isoformat())
        row['log'] = str(self.logs / f'{datetime.now():%Y%m%d-%H%M%S-%f}-{Path(row["file"]).stem}-parallel.log')
        sink = logger.add(row['log'], colorize=False, format='{time:YYYY-MM-DD HH:mm:ss} {message}',
                          filter=lambda r, key=row['fingerprint']: r['extra'].get('parallel_item') == key)
        self.sinks[id(row)] = sink
        self.save()
        self.emit(row, f'标签{slot+1}：上传中')
        app, page = self.app_for(row), self.pages[slot]
        self.apps[id(row)] = app
        with logger.contextualize(parallel_item=row['fingerprint']):
            await app.open_upload_page(page)
            await app.upload_video_file(page, row['file'])
            row['uploaded_file'] = str(Path(row['file']).resolve())
            await app.prepare_video_for_publish(page)
            await app.wait_for_upload_complete(page)
            row['phase'] = 'waiting_cover'
            self.save()
            self.emit(row, f'标签{slot+1}：等待封面')
            await app.wait_for_generated_cover(page, timeout_seconds=self.cover_timeout)
        row.update(phase='cover_ready', cover_ready_at=datetime.now().astimezone().isoformat())
        self.save()
        self.emit(row, f'标签{slot+1}：封面就绪，排队发表')
        return app, page

    async def publish(self, row, prepared, slot):
        app, page = prepared
        with logger.contextualize(parallel_item=row['fingerprint']):
            row['phase'] = 'finalizing'
            self.save()
            self.emit(row, f'标签{slot+1}：核对设置并发表')
            await page.bring_to_front()
            await app.apply_collection(page)
            await app.apply_original_statement(page)
            await app.set_thumbnail(page)
            if row.get('schedule'):
                await app.set_schedule_time_tencent(page, app.publish_date)
            await app.set_short_title(page, app.title, app.short_title)
            await verify_form(page, app, row)
            from video_channels_batch import fingerprint
            digest = await verify_video_file(page, row['file'])
            if fingerprint(row, self.record['account'], self.record.get('draft', False), digest=digest) != row['fingerprint']:
                raise RuntimeError('视频或文案在发布前发生变化，尚未点击发表')
            row['video_sha256'] = digest
            if row['uploaded_file'] != str(Path(row['file']).resolve()):
                raise RuntimeError('视频文件与标签任务不一致，尚未点击发表')
            row['before_submit_text'] = '\n'.join(await page.locator('body').all_inner_texts())
            self.emit(row, '正在提交并核验后台记录')
            async def reserved(token):
                row.update(phase='submit_started', submit_clicked_at=datetime.now().astimezone().isoformat())
                if token:
                    row['quota_reservation'] = token
                self.save()
            proof = await app.submit_publish(page, quota_attempt_key=row['log'], on_reserved=reserved)
            if not isinstance(proof, dict) or proof.get('verified') is not True:
                raise RuntimeError('未获得后台核验凭据，发表结果待核对。')
            row.update(phase='backend_verified', status='completed', returncode=0, platform_verification=proof,
                       finished_at=datetime.now().astimezone().isoformat())
            if proof.get('quota_reservation'):
                row['quota_reservation'] = proof['quota_reservation']
            self.save()
            self.emit(row, '视频草稿保存成功（后台已核验）' if self.record.get('draft') else '视频发布成功（后台已核验）')
        self.close_sink(row)

    def close_sink(self, row):
        sink = self.sinks.pop(id(row), None)
        if sink is not None:
            logger.remove(sink)

    async def failure(self, row, exc):
        if isinstance(exc, PublishLimitReached):
            self.limit_reached = True
            row.update(status='pending', phase='quota_wait', error=str(exc))
            self.save()
            self.emit(row, str(exc))
            self.close_sink(row)
            return True
        uncertain = row.get('phase') == 'submit_started'
        row.update(status='unknown' if uncertain else 'failed_before_submit', returncode=1,
                   error=str(exc), finished_at=datetime.now().astimezone().isoformat())
        self.save()
        self.emit(row, ('发表结果待核对：' if uncertain else '发表前失败：') + str(exc))
        # Keep a scoped page snapshot; no media URLs or cookies are dumped.
        try:
            page = self.pages[row['slot'] - 1]
            proof = Path(row['log']).with_suffix('.page.txt')
            proof.write_text('\n'.join(await page.locator('body').all_inner_texts()), encoding='utf-8')
            row['failure_page_text'] = str(proof)
            self.save()
        except Exception:
            pass
        self.close_sink(row)
        # A cover timeout affects this item only; other failures may indicate
        # account/terms/shared state trouble and stop further submissions.
        cover_timeout = row.get('phase') == 'waiting_cover' and '封面在等待时间内未生成完成' in str(exc)
        if cover_timeout:
            slot = row['slot'] - 1
            await self.pages[slot].close()
            self.pages[slot] = await self.context.new_page()
        return uncertain or not cover_timeout

    async def cancelled(self, row):
        if row.get('status') == 'completed':
            return
        uncertain = row.get('phase') == 'submit_started'
        row.update(status='unknown' if uncertain else 'pending' if self.limit_reached else 'failed_before_submit', returncode=1,
                   error='并行任务停止：结果待核对' if uncertain else '并行任务在点击发表前安全停止',
                   finished_at=datetime.now().astimezone().isoformat())
        self.save()
        self.emit(row, row['error'])
        self.close_sink(row)

    def finish(self, status, code):
        self.record['status'] = status
        self.record.pop('next_run_at', None)
        self.save()
        return code

    async def wait_until(self, deadline):
        previous = self.record.get('next_run_at')
        self.record.update(status='waiting_for_next_round', next_run_at=deadline.isoformat())
        self.save()
        if previous != self.record['next_run_at']:
            message = f'下一轮最早 {deadline.astimezone(BANGKOK):%m-%d %H:%M:%S}（曼谷时间）'
            self.event('batch_wait', message)
            self.event('log', message + '\n')
        while not self.stop.is_set():
            seconds = (deadline - self.budget.current_time()).total_seconds()
            if seconds <= 0:
                return
            await self.sleep(min(seconds, 1))

    def round_deadline(self):
        saved = self.record.get('next_round_at')
        if saved:
            return datetime.fromisoformat(saved)
        if self.record.get('round_in_progress'):
            # Recover a round whose final result was saved just before the app closed.
            finished = [datetime.fromisoformat(row['finished_at']) for row in self.record['items']
                        if row.get('status') == 'completed' and row.get('finished_at')]
            if finished:
                return max(finished) + ROUND_INTERVAL
        return None

    async def run_round(self, jobs):
        cookie = self.root / 'cookies' / f'tencent_{self.record["account"]}.json'
        if not cookie.is_file() or not await cookie_auth(str(cookie)):
            raise RuntimeError('视频号登录已失效，请重新扫码')
        if self.stop.is_set():
            return
        self.started.clear()
        self.limit_reached = False
        self.scheduler = ReadyFirstScheduler(self.tabs, self.stop)
        async with async_playwright() as p:
            launch = _build_launch_kwargs(headless=False)
            launch['args'] = [*launch.get('args', []), '--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows']
            if self.debug_port:
                launch['args'] += ['--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={self.debug_port}']
            browser = await p.chromium.launch(**launch)
            try:
                context = await browser.new_context(storage_state=str(cookie))
                self.context = context
                try:
                    self.pages = [await context.new_page() for _ in range(min(self.tabs, len(jobs)))]
                    await self.scheduler.run(jobs, self.prepare, self.publish, self.failure, self.cancelled,
                                             last=set(self.record.get('publish_last', [])))
                    await context.storage_state(path=str(cookie))
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def run(self, *, acquire_lock=True):
        from video_channels_batch import FINISHED_ITEMS

        account = self.record['account']
        account_lock = AccountLock(self.root / '.local/gui/account-locks', account).acquire() if acquire_lock else None
        try:
            selected = {id(row) for row in self.preflight(reconcile=True)[:self.limit]}
            draft = self.record.get('draft', False)
            while True:
                if self.stop.is_set():
                    return self.finish('paused', 0)
                jobs = [row for row in self.preflight(reconcile=True) if id(row) in selected]
                if not jobs:
                    status = 'completed' if all(r.get('status') in FINISHED_ITEMS for r in self.record['items']) else 'ready_to_resume'
                    if status == 'completed':
                        self.record.pop('next_round_at', None)
                    return self.finish(status, 0)
                if not draft:
                    usage = self.budget.usage(account)
                    self.record['daily_budget'] = usage
                    now = self.budget.current_time()
                    quota_time = datetime.fromisoformat(usage['next_available_at'])
                    deadline = max(quota_time, self.round_deadline() or now)
                    if deadline > now:
                        await self.wait_until(deadline)
                        continue
                    capacity = min(HOURLY_LIMIT, usage['hourly_remaining'], usage['remaining'])
                    if not capacity:
                        continue
                    jobs = jobs[:capacity]
                self.record.pop('next_run_at', None)
                self.record.pop('next_round_at', None)
                self.record.update(status='running', parallel_tabs=self.tabs, publish_order='cover_ready_first',
                                   round_in_progress=not draft, publish_round=self.record.get('publish_round', 0)+1)
                self.save()
                self.event('batch_wait', f'第 {self.record["publish_round"]} 轮 · 本轮 {len(jobs)} 条')
                await self.run_round(jobs)
                self.record.pop('round_in_progress', None)
                if not draft and any(row.get('status') == 'completed' for row in jobs):
                    self.record['next_round_at'] = (self.budget.current_time()+ROUND_INTERVAL).isoformat()
                self.save()
                if any(row.get('status') in ('unknown','failed_before_submit') for row in jobs if id(row) in self.started):
                    return self.finish('stopped_for_review', 1)
                if all(row.get('status') in FINISHED_ITEMS for row in self.record['items']):
                    self.record.pop('next_round_at', None)
                    return self.finish('completed', 0)
                if not self.stop.is_set() and not self.limit_reached and not any(row.get('status') == 'completed' for row in jobs):
                    return self.finish('stopped_for_review', 1)
        except BaseException:
            self.finish('stopped_for_review', 1)
            raise
        finally:
            for sink in list(self.sinks.values()):
                logger.remove(sink)
            self.sinks.clear()
            if account_lock:
                account_lock.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--tabs', default=MAX_TABS, type=int, choices=range(1, 6), help='最多使用 2 个标签；旧参数会自动限制。')
    parser.add_argument('--cover-timeout', default=1800, type=float)
    parser.add_argument('--keep-last', action='append', default=[])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--debug-port', type=int)
    args = parser.parse_args()
    runner = ParallelPublisher(args.manifest, tabs=args.tabs, cover_timeout=args.cover_timeout, limit=args.limit, debug_port=args.debug_port)
    if args.keep_last:
        runner.record['publish_last'] = args.keep_last
    return asyncio.run(runner.run())


if __name__ == '__main__':
    raise SystemExit(main())
