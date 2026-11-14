"""Submit a WeChat Official Account article to the draft box and read it back."""
from dataclasses import dataclass, field
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests

from official_account_writer import validate_draft
from official_account_images import body_paragraphs, read_image, validate_images
from publisher_lock import AccountLock
from publisher_preferences import write_local_json


@dataclass(frozen=True)
class WeChatSettings:
    app_id: str
    secret: str = field(repr=False)
    cover_id: str

    def validate(self):
        if not re.fullmatch(r"wx[a-zA-Z0-9]+", self.app_id) or not self.secret or not self.cover_id:
            raise ValueError("请在公众号同步设置中填写 AppID、AppSecret 和永久封面素材 ID。")


class WeChatError(RuntimeError):
    """A response with a definite WeChat error code."""
    def __init__(self, code):
        self.code = code
        hints = {
            40001: "请检查 AppID 和 AppSecret。",
            40013: "请检查公众号 AppID。",
            40125: "请检查公众号 AppSecret。",
            40007: "请检查永久封面素材 ID 是否属于该公众号。",
            40164: "请检查公众号开发设置里的 IP 白名单。",
            48001: "请检查该公众号是否开通草稿箱接口。",
        }
        super().__init__(f"微信接口返回错误 {code}。" + hints.get(code, "请检查公众号后台接口状态。"))


def post(path, payload, token=None, *, files=None):
    try:
        response = requests.post(
            "https://api.weixin.qq.com/cgi-bin/" + path,
            params={"access_token": token} if token else None,
            data=None if files else json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=None if files else {"Content-Type": "application/json; charset=utf-8"},
            files=files,
            timeout=(8, 25), allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"微信接口返回 HTTP {response.status_code}。")
        data = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("未收到有效的微信接口响应，请检查网络。") from None
    if not isinstance(data, dict):
        raise RuntimeError("微信接口响应格式异常。")
    if data.get("errcode"):
        code = data["errcode"]
        if not isinstance(code, int):
            raise RuntimeError("微信接口返回了无法识别的错误。")
        raise WeChatError(code)
    return data


def trusted_image_url(url):
    if not isinstance(url, str):
        raise RuntimeError("微信未返回有效的正文配图地址。")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or parts.netloc not in ("mmbiz.qpic.cn", "mmbiz.qlogo.cn"):
        raise RuntimeError("微信未返回有效的正文配图地址。")
    return parts


class BodyText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.images = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
        if tag == "img":
            values = dict(attrs)
            url = trusted_image_url(values.get("data-src") or values.get("src"))
            self.images.append((normalized("".join(self.parts)), url.path))

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n")


def normalized(value):
    return " ".join(value.split())


def verify_article(data, article, draft):
    items = data.get("news_item")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return False
    saved = items[0]
    fields = ("title", "digest", "content", "thumb_media_id")
    if any(not isinstance(saved.get(name), str) for name in fields):
        return False
    parser = BodyText()
    parser.feed(saved["content"])
    expected = BodyText()
    expected.feed(article["content"])
    return (
        saved["title"] == article["title"]
        and normalized(saved["digest"]) == normalized(article["digest"])
        and saved["thumb_media_id"] == article["thumb_media_id"]
        and normalized("".join(parser.parts)) == normalized(draft["body"])
        and parser.images == expected.images
    )


def sync_draft(directory, settings, draft):
    settings.validate()
    validated = validate_draft(draft)
    images = validate_images({**draft, **validated})
    draft = validated
    if len(draft["title"]) > 32 or "\n" in draft["title"] or len(draft["summary"]) > 120:
        raise ValueError("微信图文标题须为 32 字以内的单行文字，摘要限 120 字。")
    paragraphs = body_paragraphs(draft["body"])
    html_paragraphs = {index: "<p>" + escape(paragraph).replace("\n", "<br>") + "</p>"
                       for index, paragraph in enumerate(paragraphs)}
    content = "".join(html_paragraphs.values())
    if len(content) >= 20000:
        raise ValueError("微信图文正文超过草稿箱长度限制，请缩短后同步。")
    article = {
        "article_type": "news", "title": draft["title"],
        "digest": draft["summary"] or normalized(draft["body"])[:54],
        "content": content, "thumb_media_id": settings.cover_id,
    }
    account_dir = Path(directory) / settings.app_id
    assets = []
    for image in images:
        data, mime = read_image(image["file"])
        assets.append((paragraphs.index(image["after"]), image["caption"], sha256(data).hexdigest(), data, mime))
    identity = {**article, "images": [asset[:3] for asset in assets]} if assets else article
    key = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    receipt = account_dir / (key + ".json")
    lock = AccountLock(account_dir / "locks", "draft-sync").acquire()
    try:
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
        except FileNotFoundError:
            record = {}
        if not isinstance(record, dict):
            raise ValueError("本地草稿回执格式无效，请先核对公众号草稿箱。")
        if record and (not isinstance(record.get("media_id"), str) or not record["media_id"]):
            raise RuntimeError("这篇草稿的上次提交结果待核对，请先在公众号草稿箱确认。")
        token = post("stable_token", {
            "grant_type": "client_credential", "appid": settings.app_id,
            "secret": settings.secret, "force_refresh": False,
        }).get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("微信未返回有效的公众号访问凭据。")

        urls = record.get("image_urls", [])
        if not record:
            uploaded = {}
            for _, _, digest, data, mime in assets:
                if digest not in uploaded:
                    filename = "illustration.jpg" if mime == "image/jpeg" else "illustration.png"
                    url = post("media/uploadimg", None, token, files={"media": (filename, data, mime)}).get("url")
                    trusted_image_url(url)
                    uploaded[digest] = url
                urls.append(uploaded[digest])
        if not isinstance(urls, list) or len(urls) != len(assets):
            raise RuntimeError("本地配图回执不完整，请先核对公众号草稿箱。")
        for (index, caption, _, _, _), url in zip(assets, urls):
            trusted_image_url(url)
            html_paragraphs[index] += f'<p><img src="{escape(url)}" alt="{escape(caption)}"></p>'
        article["content"] = "".join(html_paragraphs.values())
        if len(article["content"]) >= 20000:
            raise ValueError("微信图文正文和配图链接超过草稿箱长度限制，请缩短后同步。")

        media_id = record.get("media_id")
        if not media_id:
            write_local_json(receipt, {"status": "pending", "title": draft["title"]})
            try:
                media_id = post("draft/add", {"articles": [article]}, token).get("media_id")
                if not isinstance(media_id, str) or not media_id:
                    raise RuntimeError("微信未返回草稿 ID。")
                record = {"status": "checking", "title": draft["title"], "media_id": media_id, "image_urls": urls}
                write_local_json(receipt, record)
            except WeChatError as error:
                if error.code != -1:
                    receipt.unlink()
                    raise
                raise RuntimeError("微信系统忙，提交结果待核对，请先检查公众号草稿箱。") from None
            except (RuntimeError, OSError):
                raise RuntimeError("草稿提交结果待核对，请先检查公众号草稿箱。") from None

        try:
            saved = post("draft/get", {"media_id": media_id}, token)
            if not verify_article(saved, article, draft):
                raise RuntimeError("草稿标题、摘要、正文、配图或封面与后台回读不一致。")
        except RuntimeError as error:
            raise RuntimeError(f"草稿 ID 已保存，回读核对失败。{error}") from None
        write_local_json(receipt, {**record, "status": "confirmed"})
        return media_id
    finally:
        lock.release()
