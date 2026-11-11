import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from channels_parallel import ParallelPublisher, ReadyFirstScheduler
from daily_publish_limit import BANGKOK, DailyPublishBudget, HourlyPublishLimitReached


class Clock:
    def __init__(self, hour=9, minute=0):
        self.now = datetime(2026, 5, 12, hour, minute, tzinfo=BANGKOK)

    def __call__(self):
        return self.now


class BudgetTests(unittest.TestCase):
    def test_legacy_policy_and_hour_across_midnight(self):
        with TemporaryDirectory() as folder:
            clock = Clock(23, 50)
            budget = DailyPublishBudget(folder, clock)
            Path(folder, '.local/gui/publish-policy.json').write_text('{"daily_limit":20,"account_limits":{"limited":3}}')
            for _ in range(6):
                budget.reserve('DEMO')
            self.assertEqual(budget.usage('demo')['limit'], 36)
            self.assertEqual(budget.usage('limited')['limit'], 3)
            clock.now += timedelta(minutes=10)
            self.assertEqual(budget.usage('demo')['used'], 0)
            with self.assertRaises(HourlyPublishLimitReached):
                budget.reserve('demo')
            clock.now += timedelta(minutes=50)
            self.assertEqual(budget.usage('demo')['hourly_remaining'], 6)

    def test_concurrent_reservations_and_cancelled_slot(self):
        with TemporaryDirectory() as folder:
            budget = DailyPublishBudget(folder, Clock())
            def submit(_):
                try:
                    return budget.reserve('demo')
                except HourlyPublishLimitReached:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                tokens = [token for token in pool.map(submit, range(12)) if token]
            self.assertEqual(len(tokens), 6)
            budget.update(tokens[0], 'cancelled_before_click')
            budget.reserve('demo')
            budget.reserve('other-account')
            self.assertEqual(budget.usage('demo')['used'], 6)

    def test_dispatch_moves_reservation_to_actual_bangkok_day(self):
        with TemporaryDirectory() as folder:
            clock = Clock(23, 59)
            budget = DailyPublishBudget(folder, clock)
            token = budget.reserve('demo')
            clock.now += timedelta(minutes=1)
            token = budget.update(token, 'click_started')
            self.assertEqual(token['day'], '2026-05-13')
            self.assertEqual(budget.usage('demo')['used'], 1)


class SimulatedPublisher(ParallelPublisher):
    def preflight(self, *, reconcile=False):
        return [row for row in self.record['items'] if row['status'] == 'pending']

    async def run_round(self, jobs):
        self.rounds.append((self.clock.now, len(jobs)))
        for row in jobs:
            if not self.record.get('draft'):
                token = self.budget.reserve(self.record['account'])
                self.budget.update(token, 'click_started')
            self.clock.now += timedelta(minutes=1)
            row.update(status='completed', finished_at=self.clock.now.isoformat())
            self.save()


class RoundTests(unittest.IsolatedAsyncioTestCase):
    def publisher(self, folder, clock, count=37, *, path=None, stop=None, draft=False):
        path = path or Path(folder, 'demo.json')
        record = None if path.exists() else {'account':'demo','draft':draft,'items':[
            {'file':f'demo{i:03}.mp4','status':'pending'} for i in range(count)]}
        async def wait(_):
            if stop:
                stop.set()
            else:
                clock.now = datetime.fromisoformat(runner.record['next_run_at'])
        with patch('channels_parallel.BASE_DIR', folder):
            runner = SimulatedPublisher(path, record=record, tabs=5, stop=stop,
                                        budget=DailyPublishBudget(folder, clock), sleep=wait, event=lambda *_:None)
        runner.clock, runner.rounds = clock, []
        return runner

    async def test_six_per_round_hour_after_completion_and_daily_rollover(self):
        with TemporaryDirectory() as folder:
            clock = Clock()
            runner = self.publisher(folder, clock)
            self.assertEqual(await runner.run(), 0)
            self.assertEqual(runner.tabs, 2)
            self.assertEqual([count for _, count in runner.rounds], [6]*6+[1])
            self.assertEqual(runner.rounds[1][0], datetime(2026,5,12,10,6,tzinfo=BANGKOK))
            self.assertEqual(runner.rounds[-1][0], datetime(2026,5,13,0,0,tzinfo=BANGKOK))
            self.assertEqual(runner.record['status'], 'completed')

    async def test_pause_and_resume_keep_round_deadline(self):
        with TemporaryDirectory() as folder:
            clock = Clock()
            runner = self.publisher(folder, clock, 8, stop=threading.Event())
            self.assertEqual(await runner.run(), 0)
            self.assertEqual(runner.record['status'], 'paused')
            saved = json.loads(runner.path.read_text(encoding='utf-8'))
            deadline = datetime.fromisoformat(saved['next_round_at'])
            again = self.publisher(folder, clock, path=runner.path)
            self.assertEqual(await again.run(), 0)
            self.assertEqual(again.rounds, [(deadline, 2)])

    async def test_drafts_are_not_delayed_or_counted(self):
        with TemporaryDirectory() as folder:
            runner = self.publisher(folder, Clock(), 8, draft=True)
            self.assertEqual(await runner.run(), 0)
            self.assertEqual([count for _, count in runner.rounds], [8])
            self.assertEqual(runner.budget.usage('demo')['used'], 0)

    async def test_upload_width_and_serial_submission(self):
        ready = asyncio.Event()
        active_uploads = active_submits = 0
        order = []
        async def prepare(row, _slot):
            nonlocal active_uploads
            active_uploads += 1
            self.assertLessEqual(active_uploads, 2)
            if row['file'] == 'slow':
                await ready.wait()
            active_uploads -= 1
        async def publish(row, _prepared, _slot):
            nonlocal active_submits
            active_submits += 1
            self.assertEqual(active_submits, 1)
            order.append(row['file'])
            ready.set()
            await asyncio.sleep(0)
            active_submits -= 1
        async def failure(_row, exc):
            raise exc
        async def cancelled(_row):
            self.fail('Unexpected cancellation')
        await asyncio.wait_for(ReadyFirstScheduler().run([{'file':'slow'},{'file':'fast'}],prepare,publish,failure,cancelled), 3)
        self.assertEqual(order, ['fast','slow'])
