"""Read-only, complete pagination of the account's video management statistics."""
import asyncio
import copy
from datetime import datetime, timedelta
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


class StatisticsReadError(RuntimeError):
    pass


def report_window(days=7, now=None):
    if not 1 <= days <= 365:
        raise ValueError('统计天数应为1至365。')
    end = (now or datetime.now(BANGKOK)).astimezone(BANGKOK)
    start = (end-timedelta(days=days-1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def normalize_video(obj, start, end):
    # Observed management data: mediaType=4 is video, status=1 is active;
    # handleStatus=5 corresponds to failed/rejected works still in the list.
    desc = obj.get('desc') or {}
    if desc.get('mediaType') != 4 or obj.get('status') != 1:
        return None
    if obj.get('handleStatus') not in (None, 0) or (obj.get('disableInfo') or {}).get('isDisabled'):
        return None
    stamp = obj.get('createTime')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        raise StatisticsReadError('作品发表时间字段异常，无法生成可靠排名。')
    if stamp > 10**12:
        stamp /= 1000
    published = datetime.fromtimestamp(stamp, BANGKOK)
    if not start <= published <= end:
        return None
    views = obj.get('readCount')
    if isinstance(views, bool) or not isinstance(views, int) or views < 0:
        raise StatisticsReadError('作品播放量字段异常，无法生成可靠排名。')
    title = desc.get('shortTitle')
    if isinstance(title, list):
        title = next((part.get('shortTitle') for part in title if isinstance(part, dict) and part.get('shortTitle')), '')
    if not isinstance(title, str):
        title = ''
    caption = str(desc.get('description') or '')
    identifier = str(obj.get('objectId') or '')
    if not identifier:
        raise StatisticsReadError('作品缺少标识，无法去重统计。')
    return {'id':identifier, 'title':title.strip() or caption, 'description':caption,
            'published_at':published.isoformat(), 'published_timestamp':stamp, 'views':views,
            'likes':obj.get('likeCount'), 'comments':obj.get('commentCount'),
            'forwards':obj.get('forwardCount'), 'favorites':obj.get('favCount')}


async def collect_statistics(fetch_page, *, days=7, top=10, now=None, page_size=20, progress=None):
    if not 1 <= top <= 100:
        raise ValueError('排名数量应为1至100。')
    start, end = report_window(days, now)
    expected, scanned, seen_pages, videos = None, 0, set(), {}
    for number in range(1, 1001):
        response = await fetch_page(number, page_size)
        if response.get('errCode') != 0:
            raise StatisticsReadError('后台统计请求失败：' + str(response.get('errMsg') or response.get('errCode'))[:180])
        data = response.get('data') or {}
        rows, total = data.get('list'), data.get('totalCount')
        if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise StatisticsReadError('后台列表格式已变化，请更新程序。')
        if expected is None:
            expected = total
        elif total != expected:
            raise StatisticsReadError('扫描期间作品总数变化，请重新读取以获得完整排名。')
        signature = tuple((str(row.get('objectId')), row.get('createTime'), (row.get('desc') or {}).get('description')) for row in rows)
        if rows and signature in seen_pages:
            raise StatisticsReadError('后台重复返回同一页，扫描未完成，不输出不完整Top10。')
        seen_pages.add(signature)
        if len(rows) > page_size or (not rows and scanned < expected):
            raise StatisticsReadError('后台分页返回异常，扫描未完成。')
        scanned += len(rows)
        for obj in rows:
            video = normalize_video(obj, start, end)
            if video:
                if video['id'] in videos:
                    raise StatisticsReadError('作品跨页重复，扫描期间列表可能变化，请重新读取。')
                videos[video['id']] = video
        if progress:
            progress(f'已读取第{number}页，扫描{scanned}/{expected}条，近{days}天正常发表视频{len(videos)}条。')
        if scanned >= expected:
            if scanned != expected:
                raise StatisticsReadError('扫描数量与后台总数不一致，不输出不完整排名。')
            ranked = sorted(videos.values(), key=lambda row:(-row['views'], -row['published_timestamp'], row['id']))[:top]
            for rank, row in enumerate(ranked, 1):
                row['rank'] = rank
            return {'complete':True, 'days':days, 'metric':'current_cumulative_video_views',
                    'timezone':'Asia/Bangkok', 'window_start':start.isoformat(), 'window_end':end.isoformat(),
                    'fetched_at':datetime.now(BANGKOK).isoformat(), 'scanned_count':scanned,
                    'pages_scanned':number, 'eligible_video_count':len(videos), 'requested_top':top, 'top':ranked}
        if number >= max(1, math.ceil(expected/page_size)):
            raise StatisticsReadError('后台分页数量不一致，不输出不完整排名。')
    raise StatisticsReadError('作品超过本次扫描范围，未生成排名。')


async def read_statistics(account, *, days=7, top=10, output=None, headless=True, acquire_lock=True, progress=None):
    report_window(days)
    if not 1 <= top <= 100:
        raise ValueError('排名数量应为1至100。')
    root = Path(BASE_DIR)
    account_file = root/'cookies'/f'tencent_{account}.json'
    if not account_file.is_file():
        raise StatisticsReadError('请先扫码登录此视频号，再读取统计。')
    lock = AccountLock(root/'.local/gui/account-locks', account).acquire() if acquire_lock else None
    progress = progress or (lambda text: print(text, flush=True))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=LOCAL_CHROME_PATH, headless=headless)
            tasks = set()
            try:
                context = await browser.new_context(storage_state=str(account_file))
                await set_init_script(context)
                page = await context.new_page()
                template = asyncio.get_running_loop().create_future()
                async def observe(response):
                    if not urlsplit(response.url).path.endswith('/post/post_list'):
                        return
                    try:
                        request = response.request
                        body = request.post_data_json
                        if body.get('pageSize') == 20 and body.get('currentPage') == 1 and not template.done():
                            payload = await response.json()
                            headers = {k:v for k,v in (await request.all_headers()).items()
                                       if k.lower() not in ('cookie','content-length','host','connection')}
                            template.set_result((response.url, body, headers, payload))
                    except Exception:
                        pass
                def observed(response):
                    task = asyncio.create_task(observe(response));tasks.add(task);task.add_done_callback(tasks.discard)
                page.on('response', observed)
                await page.goto('https://channels.weixin.qq.com/platform/post/list', wait_until='domcontentloaded', timeout=45000)
                deadline, opened = time.monotonic()+90, False
                while not template.done() and time.monotonic() < deadline:
                    check_session(page)
                    link = page.get_by_text('全部视频', exact=True).first
                    if not opened and await link.count() and await link.is_visible():
                        await link.click(timeout=8000);opened=True
                    await asyncio.sleep(.5)
                if not template.done():
                    raise StatisticsReadError('未能加载视频管理统计，请检查登录或网络后重试。')
                url, payload, headers, first_page = template.result()
                # The endpoint and request are captured from the user's own UI.
                # Auth and transport fields remain in memory and are never exported.
                async def fetch(number, size):
                    if number == 1:
                        return first_page
                    body = copy.deepcopy(payload)
                    body.update(currentPage=number, pageSize=size, timestamp=str(int(time.time()*1000)))
                    response = await context.request.post(url, data=body, headers=headers, timeout=45000)
                    if not 200 <= response.status < 300:
                        raise StatisticsReadError(f'读取第{number}页失败（HTTP {response.status}）。')
                    await asyncio.sleep(.25)
                    return await response.json()
                result = await collect_statistics(fetch, days=days, top=top, progress=progress)
                result['account'] = account
                if output:
                    output = Path(output)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = output.with_suffix('.tmp')
                    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(output)
                return result
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await browser.close()
    finally:
        if lock:
            lock.release()
