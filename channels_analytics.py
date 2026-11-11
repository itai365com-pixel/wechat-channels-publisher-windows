"""Read details for a fixed ranking; export only allowlisted, read-only metrics."""
import asyncio
import copy
from datetime import datetime
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

from patchright.async_api import async_playwright
from conf import BASE_DIR, LOCAL_CHROME_PATH
from daily_publish_limit import BANGKOK
from publisher_lock import AccountLock
from publish_verification import check_session
from utils.base_social_media import set_init_script
from channels_statistics import StatisticsReadError

METRICS = ('browse', 'like', 'comment', 'forward', 'fav', 'follow')


def load_selection(path, account):
    snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    targets = snapshot.get('top')
    if not snapshot.get('complete') or not isinstance(targets, list) or not 1 <= len(targets) <= 10:
        raise StatisticsReadError('请选择已完整读取、包含1至10条作品的Top10结果。')
    if snapshot.get('account') != account:
        raise StatisticsReadError('Top10结果所属账号与当前账号不一致，请重新读取。')
    ids = [r.get('id') for r in targets]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise StatisticsReadError('Top10作品标识缺失或重复，请重新读取。')
    for row in targets:
        if not isinstance(row.get('description'), str):
            raise StatisticsReadError('Top10作品简介缺失，无法核对身份。')
    try:
        start = datetime.fromisoformat(snapshot['window_start']).astimezone(BANGKOK)
        end = datetime.fromisoformat(snapshot['window_end']).astimezone(BANGKOK)
        if start > end:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise StatisticsReadError('Top10统计日期无效。') from None
    return snapshot


def number(value, *, fraction=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value if not fraction or value <= 1 else None


def normalize_detail(target, obj, loss, flow, start, end):
    if str(obj.get('exportId')) != target['id'] or (obj.get('desc') or {}).get('description') != target['description']:
        raise StatisticsReadError('详情作品标识或简介与选定作品不一致，已停止，避免串条。')
    media = ((obj.get('desc') or {}).get('media') or [{}])[0]
    retention_error = (loss.get('baseResp') or {}).get('errcode')
    points = []
    if retention_error in (None, 0):
        for point in loss.get('items') or []:
            sec, rate = number(point.get('sec')), number(point.get('rate'), fraction=True)
            if sec is not None and rate is not None:
                points.append({'sec': sec, 'rate': rate})
    feed = (flow.get('feedData') or [{}])[0]
    def series(values):
        # Unknown and malformed measurements stay unknown, never zero-filled.
        result = []
        for value in values if isinstance(values, list) else []:
            try:
                n = int(value)
                result.append(n if not isinstance(value, bool) and n >= 0 and str(n) == str(value) else None)
            except (TypeError, ValueError, OverflowError):
                result.append(None)
        return result
    sources = []
    for tab in feed.get('dataByTabtype') or []:
        sources.append({'source': str(tab.get('tabTypeName') or '未分类'),
                        'source_type': tab.get('tabType'),
                        'metrics': {k: series((tab.get('data') or {}).get(k)) for k in METRICS}})
    totals = {k: series((feed.get('totalData') or {}).get(k)) for k in METRICS}
    return {'id': target['id'], 'rank': target['rank'], 'title': target['title'],
            'description': target['description'], 'published_at': target['published_at'],
            **{out: number(obj.get(key)) for out, key in [('views', 'readCount'), ('likes', 'likeCount'),
                ('comments', 'commentCount'), ('forwards', 'forwardCount'), ('favorites', 'favCount'), ('follows', 'followCount')]},
            'duration_seconds': number(media.get('videoPlayLen')),
            'completion_rate': number(obj.get('fullPlayRate'), fraction=True),
            'average_watch_seconds': number(obj.get('avgPlayTimeSec')),
            'retention': {'points': points, 'video_length': number(loss.get('videoLen')),
                          'error_code': retention_error},
            'trend_window_start': start.isoformat(), 'trend_window_end_inclusive_date': end.date().isoformat(),
            'source_data': sources, 'daily_totals': totals,
            'fan_segmentation_unavailable': flow.get('notExistFansData'),
            'unavailable_metrics': ['曝光', '点击率', '观众画像', '粉丝与非粉丝分层', '手机端专属指标']}


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def selection_filter(body, snapshot):
    # These date fields are observed in the data center's own list request.
    if not isinstance(body, dict) or not {'startTime','endTime'} <= body.keys():
        raise StatisticsReadError('数据中心日期筛选格式已变化，请更新程序。')
    result = copy.deepcopy(body)
    for field, key in [('startTime','window_start'),('endTime','window_end')]:
        day = datetime.fromisoformat(snapshot[key]).astimezone(BANGKOK).replace(hour=0,minute=0,second=0,microsecond=0)
        result[field] = int(day.timestamp())
    return result


async def collect_details(snapshot, find_objects, fetch_loss, fetch_flow, *, progress=None):
    """Pure orchestration seam: fixed IDs only, with identity checks before requests."""
    targets = snapshot['top']
    start = datetime.fromisoformat(snapshot['window_start']).astimezone(BANGKOK).replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(snapshot['window_end']).astimezone(BANGKOK).replace(hour=0, minute=0, second=0, microsecond=0)
    objects = await find_objects(targets)
    if set(objects) != {r['id'] for r in targets}:
        raise StatisticsReadError('未找到全部选定作品；未生成不完整分析报告。')
    # Check every identity before the first per-video detail request.
    for target in targets:
        obj = objects[target['id']]
        if str(obj.get('exportId')) != target['id'] or (obj.get('desc') or {}).get('description') != target['description']:
            raise StatisticsReadError('详情与选定作品身份不一致，已停止。')
    result = {'schema_version': 1, 'account': snapshot['account'], 'selection_fetched_at': snapshot.get('fetched_at'),
              'window_start': snapshot['window_start'], 'window_end': snapshot['window_end'],
              'metric': snapshot.get('metric', 'current_cumulative_video_views'),
              'scope': 'fixed_selected_videos_only', 'requested_count': len(targets), 'videos': [], 'complete': False}
    for target in targets:
        obj = objects[target['id']]
        feed_id = str(obj['objectId'])
        loss = await fetch_loss(feed_id)
        flow = await fetch_flow(feed_id, start, end)
        result['videos'].append(normalize_detail(target, obj, loss, flow, start, end))
        if progress:
            progress(f'详情读取 {len(result["videos"])}/{len(targets)}：{target["title"]}')
        await asyncio.sleep(.2)
    result.update(complete=True, fetched_at=datetime.now(BANGKOK).isoformat())
    return result


async def read_details(account, snapshot_path, *, output=None, headless=True, acquire_lock=True, progress=None):
    snapshot = load_selection(snapshot_path, account)
    root = Path(BASE_DIR)
    account_file = root/'cookies'/f'tencent_{account}.json'
    if not account_file.is_file():
        raise StatisticsReadError('请先扫码登录此视频号，再读取详情。')
    lock = AccountLock(root/'.local/gui/account-locks', account).acquire() if acquire_lock else None
    progress = progress or (lambda text: print(text, flush=True))
    tasks, captures = set(), []
    stage = '启动统计浏览器'
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=LOCAL_CHROME_PATH, headless=headless)
            try:
                context = await browser.new_context(storage_state=str(account_file), viewport={'width':1440,'height':1000})
                await set_init_script(context)
                page = await context.new_page()
                filter_error = None
                async def filter_list(route):
                    nonlocal filter_error
                    if not urlsplit(route.request.url).path.endswith('/statistic/post_list'):
                        await route.continue_(); return
                    try:
                        body = selection_filter(route.request.post_data_json, snapshot)
                        await route.continue_(post_data=json.dumps(body))
                    except Exception:
                        filter_error = '数据中心日期筛选未完成，请更新程序或重新读取Top10。'
                        await route.abort()
                # Make both visible rows and API lookup use the saved ranking's
                # exact date range, including when refreshing it on another day.
                await page.route('**/statistic/post_list*', filter_list)
                async def observe(response):
                    path = urlsplit(response.url).path
                    if not any(path.endswith('/statistic/'+suffix) for suffix in
                               ('post_list', 'get_feed_lost_rate', 'feed_aggreagate_data_by_tab_type')):
                        return
                    try:
                        captures.append({'url': response.url, 'path': path, 'payload': response.request.post_data_json,
                                         'headers': await response.request.all_headers(), 'json': await response.json()})
                    except Exception:
                        pass
                def observed(response):
                    task = asyncio.create_task(observe(response)); tasks.add(task); task.add_done_callback(tasks.discard)
                page.on('response', observed)
                async def waiting(predicate, seconds=45):
                    deadline = time.monotonic()+seconds
                    while time.monotonic() < deadline:
                        check_session(page)
                        if filter_error:
                            raise StatisticsReadError(filter_error)
                        matches = [c for c in captures if predicate(c)]
                        if matches:
                            return matches[-1]
                        await asyncio.sleep(.3)
                    raise StatisticsReadError('视频详情加载超时，请检查网络或重新登录。')
                async def nav(text):
                    deadline = time.monotonic()+30
                    while time.monotonic() < deadline:
                        check_session(page)
                        for node in await page.get_by_text(text, exact=True).all():
                            if not await node.is_visible():
                                parent = node
                                for _ in range(4):
                                    parent = parent.locator('..')
                                    if await parent.is_visible():
                                        await parent.hover(timeout=5000); await asyncio.sleep(.3); break
                            if await node.is_visible():
                                await node.click(timeout=8000); await asyncio.sleep(.5); return
                        await asyncio.sleep(.3)
                    raise StatisticsReadError('未找到数据中心入口：'+text)
                async def api(template, updates):
                    body = copy.deepcopy(template['payload'])
                    body.update(updates, timestamp=str(int(time.time()*1000)))
                    if template['path'].endswith('/statistic/post_list'):
                        body = selection_filter(body, snapshot)
                    headers = {k:v for k,v in template['headers'].items() if k.lower() not in
                               ('cookie','content-length','host','connection')}
                    try:
                        response = await context.request.post(template['url'], data=body, headers=headers, timeout=45000)
                        if not 200 <= response.status < 300:
                            raise StatisticsReadError(f'视频统计请求失败（HTTP {response.status}）。')
                        payload = await response.json()
                    except StatisticsReadError:
                        raise
                    except Exception:
                        # Request exceptions may contain authentication URLs. Never print them.
                        raise StatisticsReadError('视频统计网络请求失败，请检查登录和网络后重试。') from None
                    if payload.get('errCode') != 0:
                        raise StatisticsReadError('视频统计接口未成功，可能登录失效、操作频繁或平台接口变化。')
                    return payload.get('data') or {}
                stage = '进入数据中心'
                await page.goto('https://channels.weixin.qq.com/platform', wait_until='domcontentloaded', timeout=45000)
                await nav('数据中心'); await nav('视频数据'); await nav('单篇视频')
                listing = await waiting(lambda c:c['path'].endswith('/statistic/post_list'))
                progress('已进入数据中心，核对选定作品身份。')
                # Capture the actual UI's descending-play list (observed fourth data column).
                header = page.locator('th').nth(4)
                if '播放' not in await header.inner_text():
                    raise StatisticsReadError('数据中心表格列已变化，请更新程序。')
                for _ in range(2):
                    before = len(captures)
                    await header.click()
                    candidate = await waiting(lambda c:c in captures[before:] and c['path'].endswith('/statistic/post_list'))
                    if candidate['payload'].get('sort') == 2 and candidate['payload'].get('order') == 1:
                        listing = candidate; break
                else:
                    raise StatisticsReadError('无法按播放量读取数据中心作品。')
                stage = '核对固定作品'
                selected = {r['id'] for r in snapshot['top']}
                matches, seed_page, seed_index = {}, None, None
                seen = set()
                async def find_objects(targets):
                    nonlocal seed_page, seed_index
                    for number_ in range(1, 1001):
                        data = listing['json'].get('data') or {} if number_ == 1 else await api(listing, {'currentPage':number_})
                        rows = data.get('list')
                        if not isinstance(rows, list):
                            raise StatisticsReadError('数据中心列表格式已变化。')
                        signature = tuple(str(o.get('exportId') or o.get('objectId')) for o in rows)
                        if rows and signature in seen:
                            raise StatisticsReadError('数据中心重复返回同一页，已停止。')
                        seen.add(signature)
                        for i, obj in enumerate(rows):
                            identifier = str(obj.get('exportId'))
                            if identifier in selected:
                                matches[identifier] = obj
                                if seed_page is None:
                                    seed_page, seed_index = number_, i
                        if set(matches) == selected or not rows:
                            break
                        if len(rows) < listing['payload'].get('pageSize',10):
                            break
                    return matches
                # No unselected per-video detail page is opened.
                await find_objects(snapshot['top'])
                if set(matches) != selected:
                    raise StatisticsReadError('数据中心未找到全部选定作品，未生成分析报告。')
                for target in snapshot['top']:
                    if (matches[target['id']].get('desc') or {}).get('description') != target['description']:
                        raise StatisticsReadError('选定作品简介与详情不一致，已停止。')
                if seed_page != 1:
                    # Navigate the observed Ant table pagination to the selected seed row.
                    current = 1
                    while current < seed_page:
                        await page.locator('.ant-pagination-next').first.click(timeout=8000)
                        current += 1
                        await waiting(lambda c:c['path'].endswith('/statistic/post_list') and c['payload'].get('currentPage')==current)
                    await asyncio.sleep(.5)
                seed_rows = [c for c in captures if c['path'].endswith('/statistic/post_list')
                             and c['payload'].get('currentPage') == seed_page and c['payload'].get('sort')==2
                             and c['payload'].get('order')==1][-1]['json']['data']['list']
                seed_obj = seed_rows[seed_index]
                if str(seed_obj.get('exportId')) not in selected:
                    raise StatisticsReadError('数据中心列表发生变化，请重试。')
                stage = '打开选定作品详情'
                seed = str(seed_obj['objectId'])
                await page.locator('.ant-table-fixed-right a.detail-wrap').nth(seed_index).click(timeout=10000)
                loss_template = await waiting(lambda c:c['path'].endswith('/get_feed_lost_rate') and str(c['payload'].get('feedId'))==seed)
                flow_template = await waiting(lambda c:c['path'].endswith('/feed_aggreagate_data_by_tab_type') and str(c['payload'].get('feedId'))==seed)
                async def fixed_objects(_):
                    return matches
                async def loss(feed_id):
                    return await api(loss_template, {'feedId':feed_id})
                async def flow(feed_id, start, end):
                    return await api(flow_template, {'feedId':feed_id, 'startTs':str(int(start.timestamp())), 'endTs':str(int(end.timestamp()))})
                stage = '读取选定作品指标'
                result = await collect_details(snapshot, fixed_objects, loss, flow, progress=progress)
                result['selection_snapshot'] = str(Path(snapshot_path).resolve())
                if output:
                    save_json(output, result)
                return result
            except StatisticsReadError:
                raise
            except Exception as exc:
                raise StatisticsReadError(f'{stage}未完成（{type(exc).__name__}），请检查登录或网络后重试；没有修改或发表视频。') from None
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await browser.close()
    finally:
        if lock:
            lock.release()
