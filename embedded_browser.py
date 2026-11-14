"""Host a pair of Windows WebView2 controls in the batch window."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from patchright.async_api import async_playwright

from channels_account_names import _session_identity, name_from_auth_data
from publisher_lock import AccountLock
from publisher_preferences import write_local_json

CHANNELS_HOME = 'https://channels.weixin.qq.com/platform'


class EmbeddedBrowser:
    def __init__(self, root, account, parent_handle):
        self.profile = root / '.local/gui/webview2' / hashlib.sha256(account.encode('utf-8')).hexdigest()
        self.marker = uuid4().hex
        self.targets = ()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.endpoint = f'http://127.0.0.1:{port}'
        directory = root / 'webview2-host'
        if not directory.is_dir():
            directory = root / '.local/webview2-host'
        executable = directory / 'EmbeddedBrowser.exe'
        if not executable.is_file():
            raise RuntimeError('缺少内嵌浏览器组件，请使用含 WebView2 组件的版本。')
        self.lock = AccountLock(self.profile.parent / 'locks', account).acquire()
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            with (self.profile / 'host-error.log').open('wb') as errors:
                self.process = subprocess.Popen(
                    [str(executable), str(parent_handle), str(self.profile), str(port), self.marker],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except BaseException:
            self.lock.release()
            raise

    def close(self):
        try:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        finally:
            self.lock.release()

    async def ready(self):
        if self.targets and self.process.poll() is None:
            return
        opener = build_opener(ProxyHandler({}))

        def discover():
            with opener.open(self.endpoint + '/json/list', timeout=0.5) as response:
                targets = {target['url']: target['id'] for target in json.load(response) if target.get('type') == 'page'}
            return tuple(targets.get(f'about:blank#{self.marker}-{index}') for index in range(2))

        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError('内嵌浏览器启动失败。请检查 Microsoft Edge WebView2 Runtime 或本地浏览器日志。')
            try:
                targets = await asyncio.to_thread(discover)
                if all(targets):
                    self.targets = targets
                    return
            except (OSError, ValueError, URLError):
                pass
            await asyncio.sleep(0.2)
        raise RuntimeError('内嵌浏览器连接超时，请重新打开发布助手。')

    @asynccontextmanager
    async def pages(self):
        await self.ready()
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(self.endpoint, timeout=10000, is_local=True)
            try:
                pages = {}
                for context in browser.contexts:
                    for page in context.pages:
                        session = await context.new_cdp_session(page)
                        try:
                            target = await session.send('Target.getTargetInfo')
                            pages[target['targetInfo']['targetId']] = page
                        finally:
                            await session.detach()
                if not all(target in pages for target in self.targets):
                    raise RuntimeError('内嵌浏览器工作标签已断开，请重新打开发布助手。')
                yield [pages[target] for target in self.targets]
            finally:
                await browser.close()


    @property
    def account_binding(self):
        return self.profile / 'account.json'

    async def verify_channels(self, page, cookie, *, login=False):
        try:
            saved = json.loads(self.account_binding.read_text(encoding='utf-8'))['finder_username']
            if not isinstance(saved, str) or not saved:
                raise ValueError
        except FileNotFoundError:
            saved = ''
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError('无法读取内嵌浏览器的账号绑定，请检查本地数据。') from error
        external = ''
        if cookie.exists():
            external, _ = _session_identity(cookie)
            if not external:
                raise RuntimeError('无法识别已保存的视频号身份，请先用外部浏览器重新扫码。')
        if saved and external and saved != external:
            raise RuntimeError('内嵌浏览器与当前视频号保存的账号不一致，请核对账号。')
        expected = external or saved
        if not expected and not login:
            raise RuntimeError('请先勾选内嵌模式，在「浏览器」页为当前视频号扫码登录。')

        def is_auth(response):
            url = urlsplit(response.url)
            return (response.ok and url.scheme == 'https' and url.netloc == 'channels.weixin.qq.com'
                    and url.path == '/cgi-bin/mmfinderassistant-bin/auth/auth_data')

        authenticated = asyncio.get_running_loop().create_future()

        async def inspect_auth(response):
            if is_auth(response) and not authenticated.done():
                try:
                    _, username = name_from_auth_data(await response.json())
                except Exception:
                    return
                if username and not authenticated.done():
                    authenticated.set_result(username)

        page.on('response', inspect_auth)
        try:
            await page.goto(CHANNELS_HOME, wait_until='domcontentloaded', timeout=30000)
            username = await asyncio.wait_for(authenticated, timeout=240 if login else 20)
        except asyncio.TimeoutError as error:
            raise RuntimeError('无法确认内嵌浏览器已登录视频号。请在「浏览器」页扫码或重试登录检查。') from error
        finally:
            page.remove_listener('response', inspect_auth)
        if expected and username != expected:
            raise RuntimeError('内嵌浏览器登录了其他视频号，请退出后用当前账号扫码。')
        if login and username != saved:
            write_local_json(self.account_binding, {'finder_username': username})
        return username

    @staticmethod
    async def assert_channels_account(page, username):
        current = await page.evaluate("""() => location.origin === 'https://channels.weixin.qq.com'
            ? localStorage.getItem('finder_username') : null""")
        if current != username:
            raise RuntimeError('无法确认内嵌浏览器仍登录本批视频号，尚未点击发表。')
