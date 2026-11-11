"""Confirm a submission from the management list, never from a disappearing button."""
import asyncio
from datetime import datetime, timedelta
import re
import time
from urllib.parse import urlsplit


class SubmissionUnconfirmed(RuntimeError):
    pass


def clean_caption(text):
    return str(text).replace('\r\n', '\n').replace('\xa0', ' ').replace('\u200b', '').strip()


def check_session(page):
    if page.is_closed():
        raise SubmissionUnconfirmed('发表页面已关闭，结果待核对；不能直接重传。')
    url = urlsplit(page.url)
    if 'login' in url.path.lower() or url.hostname == 'open.weixin.qq.com':
        raise SubmissionUnconfirmed('登录已失效或被跳回登录页，发表结果待核对；请重新登录核对后台。')


async def visible_feedback(page):
    nodes = page.locator('.weui-desktop-dialog:visible, .weui-desktop-toast:visible, [role="alert"]:visible, .ant-message-notice-content:visible')
    return '\n'.join(await nodes.all_inner_texts())


async def management_snapshot(page):
    # Search replaces the list asynchronously. Read every caption and its row
    # in one browser task: nth locators become stale during that replacement.
    return await page.locator('.post-title').evaluate_all('''elements => elements
        .filter(element => element.isConnected && element.getClientRects().length)
        .map(element => {
            let row = element.parentElement;
            while (row && !row.querySelector('.edit-cover-item')) row = row.parentElement;
            return {caption: element.innerText, row_text: (row || element).innerText};
        })''')


def check_rejection(text):
    markers = ('发表失败', '发布失败', '发布受限', '发表受限', '操作频繁', '操作太频繁', '操作过于频繁', '发布过于频繁',
               '发表过于频繁', '次数已达上限', '数量已达上限', '超过发布限制', '请明天再试')
    if any(marker in text for marker in markers):
        raise SubmissionUnconfirmed('平台提示：' + text[:600] + '；已停止，发表结果须核对。')


async def verify_management_record(page, caption, submitted_at, *, timeout_seconds=90, poll_interval=2, draft=False, short_title=None):
    caption = clean_caption(caption)
    if not caption:
        raise SubmissionUnconfirmed('简介为空，无法从后台准确核验这条作品，结果待核对。')
    await page.goto('https://channels.weixin.qq.com/platform/post/list', wait_until='domcontentloaded', timeout=30000)
    deadline = time.monotonic() + timeout_seconds
    if draft:
        draft_link = page.locator('a:visible').filter(has_text=re.compile(r'^\s*草稿箱\s*$')).first
        await draft_link.click(timeout=10000)
    query_match = re.search(r'第\s*\d+\s*集', caption)
    queries = list(dict.fromkeys(query for query in (
        query_match.group(0) if query_match else None,
        clean_caption(short_title) if short_title else None,
        re.sub(r'\s+', ' ', caption[:24]),
    ) if query))
    query_index = 0
    last_search = 0
    opened_list = False
    while time.monotonic() < deadline:
        check_session(page)
        check_rejection(await visible_feedback(page))
        # Check the newest unfiltered rows before applying search. A phrase
        # search may not be indexed yet even though the new work is present.
        candidates = [row for row in await management_snapshot(page) if clean_caption(row['caption']) == caption]
        if len(candidates) > 1:
            raise SubmissionUnconfirmed('后台存在多条相同简介作品，无法唯一核验本次发表，请人工核对。')
        if len(candidates) == 1:
            row_text = candidates[0]['row_text']
            check_rejection(row_text.replace(candidates[0]['caption'], '', 1))
            date = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})', row_text)
            fresh = False
            if date:
                posted = datetime(*map(int, date.groups()))
                fresh = posted >= submitted_at.replace(second=0, microsecond=0, tzinfo=None) - timedelta(minutes=1)
            if fresh:
                return {'verified': True, 'verified_at': datetime.now().astimezone().isoformat(),
                        'matched_description': caption, 'row_text': row_text,
                        'manager_path': urlsplit(page.url).path, 'draft': draft}
        field = page.get_by_placeholder('搜索视频').first
        if await field.count() and await field.is_visible():
            now = time.monotonic()
            if not last_search or now - last_search >= 10:
                await field.fill(queries[query_index % len(queries)])
                await field.press('Enter')
                last_search = now
                query_index += 1
        elif not opened_list:
            # After a full page load the site's initial router can show its
            # dashboard despite the list URL. Follow the visible list entry.
            all_videos = page.get_by_text('全部视频', exact=True).first
            if await all_videos.count() and await all_videos.is_visible():
                await all_videos.click(timeout=8000)
                opened_list = True
        await asyncio.sleep(poll_interval)
    raise SubmissionUnconfirmed('已点击发表，但后台尚未查到可确认的对应作品；结果待核对，禁止自动重传。')
