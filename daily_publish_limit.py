"""Durable per-account rolling-hour and Bangkok calendar-day submission limits."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from publisher_lock import AccountLock

BANGKOK = timezone(timedelta(hours=7), 'Asia/Bangkok')
DEFAULT_LIMIT = 36
HOURLY_LIMIT = 6
ROUND_INTERVAL = timedelta(hours=1)
SUBMIT_PHASES = {'submit_started', 'submit_returned_success', 'backend_verified', 'confirmed_on_platform', 'post_submit_unverified'}


class PublishLimitReached(RuntimeError):
    pass


class DailyPublishLimitReached(PublishLimitReached):
    pass


class HourlyPublishLimitReached(PublishLimitReached):
    pass


class DailyPublishBudget:
    def __init__(self, root, now=None):
        self.root = Path(root)
        self.folder = self.root / '.local/gui/daily-quota'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: datetime.now(BANGKOK))

    def day(self):
        return self.current_time().date().isoformat()

    def current_time(self):
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=BANGKOK)
        return value.astimezone(BANGKOK)

    def limit(self, account):
        path = self.root / '.local/gui/publish-policy.json'
        policy = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        default = policy.get('daily_limit', DEFAULT_LIMIT)
        # Older installers persisted the old default and leave existing policy files intact.
        if 'hourly_limit' not in policy and type(default) is int and default == 20:
            default = DEFAULT_LIMIT
        limit = policy.get('account_limits', {}).get(account, default)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= DEFAULT_LIMIT:
            raise ValueError('单日发表额度配置无效，已阻止提交。')
        return limit

    def path(self, account):
        key = hashlib.sha256(account.strip().casefold().encode('utf-8')).hexdigest()
        return self.folder / f'{key}.json'

    @contextmanager
    def locked(self, account):
        account = account.strip().casefold()
        deadline = time.monotonic() + 3
        while True:
            try:
                lock = AccountLock(self.folder / 'locks', account).acquire()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('额度记录正在使用，请稍后重试；本次未提交。')
                time.sleep(0.01)
        try:
            path = self.path(account)
            data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'account': account, 'entries': {}}
            self.import_history(data, account)
            yield data
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            lock.release()

    def import_history(self, data, account):
        tracked_logs = {str(Path(entry['attempt_key']).resolve()) for entry in data['entries'].values() if entry.get('attempt_key')}
        for path in (self.root / '.local/gui/batches').glob('*.json'):
            batch = json.loads(path.read_text(encoding='utf-8'))
            if str(batch.get('account', '')).strip().casefold() != account or batch.get('draft'):
                continue
            for row in batch.get('items', []):
                for attempt in [*row.get('previous_attempts', []), row]:
                    if attempt.get('phase') not in SUBMIT_PHASES and attempt.get('status') not in ('completed', 'unknown'):
                        continue
                    reservation = attempt.get('quota_reservation')
                    if reservation:
                        key = reservation['id']
                        if key not in data['entries']:
                            data['entries'][key] = reservation
                        continue
                    stamp = attempt.get('submit_clicked_at') or attempt.get('finished_at')
                    if not stamp:
                        continue
                    posted_at = datetime.fromisoformat(stamp)
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=BANGKOK)
                    log = attempt.get('log') or row.get('external_completed_log')
                    if log and str(Path(log).resolve()) in tracked_logs:
                        continue
                    identity = str(Path(log).resolve()) if log else row.get('fingerprint', row['file']) + str(stamp)
                    key = 'legacy-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
                    data['entries'].setdefault(key, {'id': key, 'account': account,
                        'day': posted_at.astimezone(BANGKOK).date().isoformat(), 'state': 'legacy_unverified',
                        'source_log': log, 'source_manifest': str(path), 'at': posted_at.isoformat()})

    def summary(self, data, account, now=None, *, exclude=None):
        now = now or self.current_time()
        day = now.date().isoformat()
        entries = [entry for key, entry in data['entries'].items()
                   if key != exclude and entry.get('state') != 'cancelled_before_click']
        used = sum(entry.get('day') == day for entry in entries)
        recent = []
        for entry in entries:
            stamp = datetime.fromisoformat(entry.get('submitted_at') or entry['at'])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=BANGKOK)
            if stamp > now - ROUND_INTERVAL:
                recent.append(stamp)
        limit = self.limit(account)
        remaining = max(0, limit - used)
        hourly_remaining = max(0, HOURLY_LIMIT - len(recent))
        available = now
        if not remaining:
            available = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if not hourly_remaining:
            available = max(available, sorted(recent)[-HOURLY_LIMIT] + ROUND_INTERVAL)
        return {'account': account, 'day': day, 'timezone': 'Asia/Bangkok', 'limit': limit,
                'used': used, 'remaining': remaining, 'hourly_limit': HOURLY_LIMIT,
                'hourly_used': len(recent), 'hourly_remaining': hourly_remaining,
                'next_available_at': available.isoformat()}

    def usage(self, account):
        account = account.strip().casefold()
        with self.locked(account) as data:
            return self.summary(data, account)

    def reserve(self, account, attempt_key=None):
        account = account.strip().casefold()
        with self.locked(account) as data:
            now = self.current_time()
            usage = self.summary(data, account, now)
            self.require_capacity(usage)
            token = {'id': uuid.uuid4().hex, 'account': account, 'day': usage['day'],
                     'at': now.isoformat(), 'state': 'reserved', 'attempt_key': attempt_key}
            data['entries'][token['id']] = token
            return dict(token)

    def update(self, token, state):
        with self.locked(token['account']) as data:
            entry = data['entries'][token['id']]
            now = self.current_time()
            if state == 'click_started':
                # The dispatch can cross midnight after the original reservation.
                usage = self.summary(data, token['account'], now, exclude=token['id'])
                self.require_capacity(usage)
                entry.update(day=usage['day'], submitted_at=now.isoformat())
            entry.update(state=state, updated_at=now.isoformat())
            return dict(entry)

    @staticmethod
    def require_capacity(usage):
        if not usage['remaining']:
            raise DailyPublishLimitReached(f'今日提交已达 {usage["limit"]} 条上限（曼谷时间）。')
        if not usage['hourly_remaining']:
            raise HourlyPublishLimitReached(f'过去 60 分钟已达 {HOURLY_LIMIT} 条提交上限。')
