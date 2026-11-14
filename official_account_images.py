"""Generate and place inline illustrations in a local article draft."""
import base64
from pathlib import Path
import re
from uuid import uuid4

import requests


MAX_IMAGE_BYTES = 1_000_000
IMAGE_MODELS = ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2")


def body_paragraphs(body):
    return [part for part in re.split(r"\n\s*\n", body.replace("\r\n", "\n")) if part.strip()]


def validate_images(draft, directory=None):
    images = draft.get("images", [])
    if not isinstance(images, list):
        raise ValueError("正文配图记录格式无效。")
    paragraphs = body_paragraphs(draft["body"])
    result = []
    for image in images:
        if not isinstance(image, dict) or any(not isinstance(image.get(name), str)
                                              for name in ("after", "caption", "file")):
            raise ValueError("正文配图记录格式无效。")
        if paragraphs.count(image["after"]) != 1:
            raise ValueError("有配图对应的段落已修改或重复，请移除该图后重新选择段落。")
        path = Path(image["file"])
        if not path.is_absolute():
            if directory is None:
                raise ValueError("配图路径未解析，请重新打开本地草稿。")
            path = Path(directory) / path
        result.append({"after": image["after"], "caption": image["caption"], "file": str(path.resolve())})
    return result


def read_image(path):
    path = Path(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES)
    except OSError:
        raise ValueError(f"无法打开配图 {path.name}。") from None
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB\x60\x82"):
        mime = "image/png"
    else:
        mime = ""
    if len(data) >= MAX_IMAGE_BYTES or not mime:
        raise ValueError(f"配图 {path.name} 须为小于 1 MB 的有效 JPG 或 PNG。")
    return data, mime


def generate_image(api_key, prompt, directory, *, model=IMAGE_MODELS[0]):
    try:
        response = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "prompt": prompt, "size": "1536x1024",
                  "quality": "medium", "output_format": "jpeg", "output_compression": 75, "n": 1},
            timeout=(10, 180), allow_redirects=False,
        )
    except requests.RequestException:
        raise RuntimeError("连接 OpenAI 图片接口失败或超时，请稍后重试。") from None
    if response.status_code != 200:
        raise RuntimeError(f"OpenAI 图片生成失败（HTTP {response.status_code}），请检查密钥、额度和模型权限。")
    try:
        data = base64.b64decode(response.json()["data"][0]["b64_json"], validate=True)
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError("OpenAI 未返回有效图片。") from None
    if len(data) >= MAX_IMAGE_BYTES or not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        raise RuntimeError("生成的图片格式异常或超过微信 1 MB 限制，请重试。")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ai-{uuid4().hex}.jpg"
    path.write_bytes(data)
    return path
