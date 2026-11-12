"""Drive the visible TikTok Studio upload form in an isolated Chrome profile."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import time
from urllib.parse import unquote, urljoin, urlsplit


STUDIO = "https://www.tiktok.com/tiktokstudio/upload"
CONTENT = "https://www.tiktok.com/tiktokstudio/content"
PROFILE_MENU = 'button[aria-haspopup="dialog"][aria-expanded]'
CAPTION = '[contenteditable="true"][role="combobox"]'
PUBLIC = re.compile(r"^(所有人|公開|公开|Everyone|Public|ทุกคน)$", re.I)
SELECT_VIDEO = re.compile(r"^(選取影片|选择视频|Select video|เลือกวิดีโอ)$", re.I)
UPLOAD_DONE = re.compile(r"^(已上傳|已上传|Uploaded|อัปโหลดแล้ว)(?:\s*[（(].*)?$", re.I)
PUBLISH = re.compile(r"^(發佈|發布|发布|Post|Publish|โพสต์)$", re.I)
REVIEW = re.compile(r"審查中|审核中|審核中|处理中|處理中|under review|processing", re.I)
NO_POSTS = re.compile(r"^(沒有作品|没有作品|沒有貼文|暂无作品|尚無作品|No posts(?: yet)?|No videos(?: yet)?)$", re.I)
POST_LINK = 'a[href*="/video/"]'
PRIVACY = re.compile(r"^(所有人|公開|公开|僅自己|仅自己|好友|朋友|Everyone|Public|Only you|Friends|ทุกคน)$", re.I)


@dataclass(frozen=True)
class StudioPost:
    url: str
    caption: str
    public: bool


@contextmanager
def open_studio(profile, username):
    from playwright.sync_api import sync_playwright

    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as browser:
        context = browser.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False, timezone_id="Asia/Bangkok", timeout=20000
        )
        try:
            context.set_default_timeout(8000)
            yield Studio(context, username)
        finally:
            context.close()


class Studio:
    def __init__(self, context, username):
        self.history = context.pages[0] if context.pages else context.new_page()
        self.editor = None
        self.context = context
        self.username = username

    def _goto(self, page, url):
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

    def _check_route(self, page, expected):
        actual, target = urlsplit(page.url), urlsplit(expected)
        if actual.scheme != "https" or actual.netloc != target.netloc or actual.path != target.path:
            raise ValueError("未进入 TikTok Studio，请检查登录或浏览器中的验证提示。")

    def _check_account(self, page, route):
        self._check_route(page, route)
        button = page.locator(PROFILE_MENU).filter(visible=True)
        button.wait_for(state="visible", timeout=12000)
        if button.get_attribute("aria-expanded") != "true":
            button.click()
        profile = page.locator('a.TUXMenuItem[href*="/@"]').filter(visible=True)
        href = profile.get_attribute("href")
        button.click()
        expected = "/@" + self.username
        if unquote(urlsplit(href or "").path).rstrip("/").casefold() != expected:
            raise ValueError(f"Studio 登录的账号与 @{self.username} 不符，请切换账号后继续。")

    def login(self, stop, progress, wait_seconds):
        from playwright.sync_api import TimeoutError

        self._goto(self.history, CONTENT)
        deadline = time.monotonic() + wait_seconds
        last_url = self.history.url
        progress(f"请在打开的 Chrome 窗口登录 TikTok @{self.username}。")
        while not stop.is_set():
            current_url = self.history.url
            if current_url != last_url and urlsplit(current_url).path in ("/", "/foryou", "/following"):
                self._goto(self.history, CONTENT)
            last_url = self.history.url
            if urlsplit(last_url).path == urlsplit(CONTENT).path:
                if self.history.locator(PROFILE_MENU).filter(visible=True).count() == 1:
                    try:
                        self._check_account(self.history, CONTENT)
                    except TimeoutError:
                        pass
                    else:
                        return True
            if time.monotonic() >= deadline:
                break
            self.history.wait_for_timeout(800)
        return False

    def posts(self):
        page = self.history
        self._goto(page, CONTENT)
        self._check_route(page, CONTENT)
        links = page.locator(POST_LINK)
        links.first.or_(page.get_by_text(NO_POSTS)).first.wait_for(state="visible", timeout=16000)
        rows = links.evaluate_all("""(links, pattern) => links.map(link => {
            const privacy = new RegExp(pattern, 'i');
            let parent = link.parentElement;
            for (let depth = 0; parent && depth < 12; depth++, parent = parent.parentElement) {
                if (parent.querySelectorAll('a[href*="/video/"]').length !== 1) break;
                const button = [...parent.querySelectorAll('button')].find(
                    item => privacy.test(item.innerText.trim()));
                if (button) {
                    const caption = link.getAttribute('aria-label') || link.innerText;
                    const disabled = button.disabled || button.getAttribute('aria-disabled') === 'true';
                    return [link.href, caption, button.innerText.trim(), parent.innerText, disabled];
                }
            }
            return [link.href, link.innerText, '', '', true];
        })""", PRIVACY.pattern)
        found = {}
        for href, text, privacy, row, disabled in rows:
            path = urlsplit(href)
            expected = "/@" + self.username + "/video/"
            if path.hostname == "www.tiktok.com" and unquote(path.path).casefold().startswith(expected):
                url = urljoin(CONTENT, path.path)
                public = not disabled and PUBLIC.fullmatch(privacy) and not REVIEW.search(row)
                found[url] = StudioPost(url, text, bool(public))
        if rows and not found:
            raise ValueError(f"作品列表没有 @{self.username} 的记录，请检查登录账号。")
        return list(found.values())

    def prepare(self, file, caption):
        if self.editor is None:
            self.editor = self.context.new_page()
        page = self.editor
        self._goto(page, STUDIO)
        self._check_account(page, STUDIO)
        with page.expect_file_chooser(timeout=12000) as chooser:
            page.get_by_role("button", name=SELECT_VIDEO).click()
        chooser.value.set_files(file)
        page.get_by_text(UPLOAD_DONE).wait_for(state="visible", timeout=120000)
        filename = re.compile(r"^" + re.escape(Path(file).name) + r"(?:\s|$)")
        if not page.get_by_text(filename).first.is_visible():
            raise ValueError("Studio 显示的文件名与队列不一致，请先核对。")

        tips = page.get_by_role("button", name=re.compile(r"^(知道了|Got it|เข้าใจแล้ว)$", re.I))
        if tips.count() == 1 and tips.is_visible():
            tips.click()
        editor = page.locator(CAPTION)
        editor.fill(caption)
        editor.press("Tab")
        if editor.inner_text().replace("\r\n", "\n").strip() != caption:
            raise ValueError("Studio 中的文案与表格不一致，请在浏览器核对。")
        self._check_account(page, STUDIO)
        public_choice = page.get_by_role("combobox").filter(has_text=PUBLIC)
        now = page.get_by_role("radio", name=re.compile(r"^(現在|现在|Now|ตอนนี้)$", re.I))
        if public_choice.count() != 1 or not public_choice.is_visible() or not now.is_checked():
            raise ValueError("Studio 未显示「立即发布、所有人可见」，请核对页面设置。")
        if not page.get_by_role("button", name=PUBLISH).is_enabled():
            raise ValueError("Studio 发布按钮不可用，请检查页面提示。")

    def submit(self):
        self._check_route(self.editor, STUDIO)
        self.editor.get_by_role("button", name=PUBLISH).click(timeout=10000)
