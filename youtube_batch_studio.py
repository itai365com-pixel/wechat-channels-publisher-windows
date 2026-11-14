"""YouTube Studio UI used by the desktop batch queue."""
from contextlib import asynccontextmanager
import re
import time
from urllib.parse import parse_qs, urlsplit

from youtube_batch import CHANNEL_ID, VIDEO_ID


STUDIO = "https://studio.youtube.com"
UPLOAD_DIALOG = "ytcp-uploads-dialog"
TITLE = "#title-textarea #textbox"
DESCRIPTION = "#description-textarea #textbox"
VISIBILITY_RADIO = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
VISIBILITY_NAMES = {
    "public": {"public", "公开", "公開", "สาธารณะ"},
    "unlisted": {"unlisted", "不公开列出", "不公開", "不公開列出", "ไม่เป็นสาธารณะ"},
    "private": {"private", "私享", "私人", "私人影片", "ส่วนตัว"},
}
READY = re.compile(r"upload complete|finished uploading|uploaded|\bprocessing\b"
                   r"|上传完成|上传完毕|已上传|正在处理|开始处理|上傳完成|上傳完畢|已上傳|處理中|อัปโหลดเสร็จ|กำลังประมวลผล", re.I)
CHECKS = re.compile(r"^(?:正在检查|正在檢查|检查完毕|檢查完畢|checks? (?:running|complete)|checking:)", re.I)
UPLOADING = re.compile(r"\d+\s*%|uploading|正在上传|正在上傳", re.I)
UPLOAD_ERROR = re.compile(r"daily upload limit|upload limit reached|upload failed|upload abandoned"
                          r"|上传失败|上传次数.*上限|上传数量.*上限|每日上传数上限|上傳失敗|上傳.*上限|อัปโหลดไม่สำเร็จ", re.I)
CREATE = re.compile(r"^(Create|创建|建立|สร้าง)$", re.I)
UPLOAD = re.compile(r"^(Upload videos|上传视频|上傳影片|อัปโหลดวิดีโอ)$", re.I)


def video_id_from_link(value):
    link = urlsplit(value)
    if link.hostname in ("www.youtube.com", "youtube.com"):
        if link.path == "/watch":
            candidate = parse_qs(link.query).get("v", [""])[0]
        else:
            candidate = link.path.removeprefix("/shorts/") if link.path.startswith("/shorts/") else ""
    elif link.hostname == "youtu.be":
        candidate = link.path.strip("/")
    elif link.hostname == "studio.youtube.com" and re.fullmatch(r"/video/[A-Za-z0-9_-]{11}/edit", link.path):
        candidate = link.path.split("/")[2]
    else:
        candidate = ""
    return candidate if VIDEO_ID.fullmatch(candidate) else ""


def upload_bytes_complete(status):
    return bool(CHECKS.match(status) or (READY.search(status) and not UPLOADING.search(status)))


@asynccontextmanager
async def open_studio(profile, *, use_current_chrome=False):
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        if use_current_chrome:
            from chrome_session import current_chrome_endpoint

            connected = await driver.chromium.connect_over_cdp(current_chrome_endpoint(), timeout=45000, is_local=True)
            if len(connected.contexts) != 1:
                raise RuntimeError("无法识别当前 Chrome 的普通浏览上下文。")
            context = connected.contexts[0]
            studio = Studio(context, page=await context.new_page())
            try:
                yield studio
            finally:
                for page in (studio.history, studio.page):
                    if page is not None and not page.is_closed():
                        await page.close()
            return
        context = await driver.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=False, timeout=20000
        )
        try:
            context.set_default_timeout(10000)
            yield Studio(context)
        finally:
            await context.close()


class Studio:
    def __init__(self, context, *, page=None):
        self.context = context
        self.page = page or (context.pages[0] if context.pages else None)
        self.history = None
        self.channel = ""

    async def goto(self, path):
        if self.page is None:
            self.page = await self.context.new_page()
        await self.page.goto(STUDIO + path, wait_until="domcontentloaded", timeout=30000)

    async def detect_channel(self, stop, wait_seconds, expected=""):
        if self.page is None or urlsplit(self.page.url).hostname != "studio.youtube.com":
            await self.goto("/")
        deadline = time.monotonic() + wait_seconds
        while not stop.is_set():
            route = urlsplit(self.page.url)
            segments = route.path.strip("/").split("/")
            if (route.hostname == "studio.youtube.com" and len(segments) >= 2
                    and segments[0] == "channel" and CHANNEL_ID.fullmatch(segments[1])):
                channel = segments[1]
                # The sidebar renders only when the selected channel is accessible.
                menu = self.page.locator(f'ytcp-navigation-drawer a[href^="/channel/{channel}/"]')
                dashboard = self.page.get_by_role("button", name=CREATE)
                if (not expected or channel == expected) and (await menu.count() or await dashboard.is_visible()):
                    self.channel = channel
                    return channel
            if time.monotonic() >= deadline or self.page.is_closed():
                break
            await self.page.wait_for_timeout(500)
        if stop.is_set():
            raise RuntimeError("已暂停连接 YouTube Studio。")
        raise RuntimeError("未进入目标频道，请在发布浏览器登录并切换频道。")

    async def connect(self, expected, stop):
        actual = await self.detect_channel(stop, 18)
        if actual != expected:
            raise ValueError(f"发布浏览器当前频道是 {actual}，请切换为 {expected} 后继续。")

    async def content(self, tab, *, page=None):
        page = self.page if page is None else page
        await page.goto(f"{STUDIO}/channel/{self.channel}/videos/{tab}", wait_until="domcontentloaded", timeout=30000)
        route = urlsplit(page.url)
        if route.hostname != "studio.youtube.com" or not route.path.startswith(f"/channel/{self.channel}/"):
            raise RuntimeError("Studio 已退出目标频道，请重新登录或切换频道。")
        await page.get_by_role("table").first.wait_for(state="visible", timeout=20000)

    async def upload(self, item, visibility, audience, reserve, identify, stop):
        from playwright.async_api import TimeoutError

        from uploader.youtube_uploader.main import _fill_editable

        await self.content("upload")
        await self.page.get_by_role("button", name=CREATE, exact=True).click()
        await self.page.get_by_text(UPLOAD, exact=True).filter(visible=True).first.click()
        upload = self.page.locator(UPLOAD_DIALOG)
        file_input = upload.locator('input[type="file"]')
        await file_input.wait_for(state="attached", timeout=20000)
        if stop.is_set():
            return
        reserve()
        await file_input.set_input_files(item["file"], timeout=30000)

        title_field = self.page.locator(TITLE).first
        deadline = time.monotonic() + 35
        while not await title_field.is_visible():
            error = next((text for text in await upload.all_inner_texts() if UPLOAD_ERROR.search(text)), "")
            if error:
                raise RuntimeError(next((line.strip() for line in error.splitlines() if UPLOAD_ERROR.search(line)), error))
            if time.monotonic() >= deadline:
                raise RuntimeError("等待 YouTube 上传详情超时。")
            await self.page.wait_for_timeout(500)
        details = self.page.get_by_role("dialog").filter(has=self.page.locator(TITLE)).last
        video_id = await self.capture_id(details, 20)
        identify(video_id)
        await _fill_editable(self.page, TITLE, item["title"])
        await _fill_editable(self.page, DESCRIPTION, item["desc"])
        await self.check_copy(details, item["title"], item["desc"])

        audience_value = "MFK" if audience == "yes" else "NOT_MFK"
        radio = details.locator(f'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_{audience_value}"]')
        await self.select_radio(radio)
        if item["tags"]:
            await details.locator("#toggle-button").click()
            tags = details.locator("#tags-container input").first
            for tag in item["tags"]:
                await tags.fill(tag)
                await tags.press("Enter")
                await details.locator("#tags-container").get_by_text(tag, exact=True).first.wait_for(
                    state="visible", timeout=5000
                )

        choice = details.locator(f'tp-yt-paper-radio-button[name="{VISIBILITY_RADIO[visibility]}"]')
        for _ in range(4):
            if await choice.is_visible():
                break
            await details.locator("#next-button").click()
        await self.select_radio(choice)
        await self.wait_uploaded(details)
        submit = details.locator("#done-button")
        await submit.click(timeout=15000)
        result = self.page.get_by_role("dialog", name=re.compile(
            r"^(正在处理视频|视频发布时间|视频已发布|Processing video|Video published)$", re.I
        ))
        try:
            await result.wait_for(state="visible", timeout=6000)
        except TimeoutError:
            # The details page may close directly. The batch always verifies
            # the unique ID and visibility separately before saving success.
            return
        close = result.get_by_role("button", name=re.compile(r"^(关闭|關閉|Close)$", re.I)).first
        if await close.is_visible():
            await close.click()

    @staticmethod
    async def check_copy(scope, title, description):
        def clean(value):
            return (value or "").replace("\r\n", "\n").strip()
        for selector, expected in ((TITLE, title), (DESCRIPTION, description)):
            field = scope.locator(selector).first
            await field.wait_for(state="visible", timeout=15000)
            if clean(await field.text_content()) != clean(expected) and clean(await field.inner_text()) != clean(expected):
                raise ValueError("Studio 中的标题或简介与表格不一致。")

    @staticmethod
    async def select_radio(radio):
        if await radio.get_attribute("aria-checked") != "true":
            await radio.click()
        if await radio.get_attribute("aria-checked") != "true" and await radio.get_attribute("checked") is None:
            raise ValueError("Studio 未确认受众或公开范围。")

    async def capture_id(self, details, seconds):
        links = details.locator('a[href*="youtu.be/"], a[href*="youtube.com/watch?"], a[href*="youtube.com/shorts/"]')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            hrefs = await links.evaluate_all("(links) => links.map(link => link.href)")
            identifiers = {video_id_from_link(href) for href in hrefs} - {""}
            if len(identifiers) == 1:
                return identifiers.pop()
            if len(identifiers) > 1:
                break
            await self.page.wait_for_timeout(500)
        raise RuntimeError("Studio 未显示唯一作品链接。")

    async def wait_uploaded(self, details):
        progress = details.locator("ytcp-video-upload-progress")
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            status = (await progress.first.inner_text()).strip() if await progress.count() else ""
            if UPLOAD_ERROR.search(status):
                raise RuntimeError(status)
            if upload_bytes_complete(status):
                return
            await self.page.wait_for_timeout(800)
        raise RuntimeError("等待上传完成超时。")

    async def verify(self, record):
        video_id = record["video_id"]
        expected = VISIBILITY_NAMES[record["visibility"]]
        href = f"/video/{video_id}/edit"
        if self.history is None:
            self.history = await self.context.new_page()
        page = self.history
        for _ in range(3):
            for tab in ("upload", "short"):
                await self.content(tab, page=page)
                edit_link = f'a[href$="{href}"], a[href*="{href}?"]'
                row = page.locator("ytcp-video-row").filter(has=page.locator(edit_link))
                if await row.count() != 1:
                    continue
                visibility = row.locator(".tablecell-visibility, ytcp-video-list-cell-visibility, #visibility-cell").first
                if not await visibility.count():
                    continue
                value = " ".join((await visibility.inner_text()).split()).casefold()
                if value not in expected:
                    continue
                await row.locator(edit_link).first.click()
                await page.wait_for_url(f"{STUDIO}{href}*", timeout=15000)
                await self.check_copy(page, record["title"], record["description"])
                return True
            await page.wait_for_timeout(1500)
        return False
