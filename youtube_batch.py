"""Read and run a per-channel YouTube batch with durable upload receipts."""
from hashlib import file_digest
import json
from pathlib import Path
import re
from zipfile import BadZipFile

from batch_table import match_records, read_tables, records_from_rows, resolve_series
from publisher_preferences import write_local_json

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
VISIBILITY = {"private": "私享", "unlisted": "不公开列出", "public": "公开"}
AUDIENCE = {"no": "不面向儿童", "yes": "面向儿童"}
TEMPLATE = ("文件名,标题,简介,标签\n"
            'lesson001.mp4,日常会话第 1 课,本课练习如何打招呼 #Shorts,"日常会话,零基础"\n')
RECEIPT_STATES = {"checking": "待核对", "confirmed": "已核验"}


def read_items(folder, table, sheet=""):
    try:
        tables = read_tables(table)
    except (BadZipFile, KeyError) as exc:
        raise ValueError("无法读取文案表，请确认文件是有效的 XLSX。") from exc
    if not sheet and len(tables) != 1:
        return tuple(tables), None
    rows = tables.get(sheet) if sheet else next(iter(tables.values()), None)
    if rows is None:
        raise ValueError("请选择有效的工作表。")
    records = records_from_rows(rows)
    items, _ = match_records(records, folder, resolve_series(records, folder))
    if not items:
        raise ValueError("文案表没有匹配到视频。")
    found = {row["source_row"] for row in items}
    missing = [row["file"] for row in records if row["source_row"] not in found]
    if missing:
        raise ValueError("找不到表格指定的视频：" + "、".join(missing[:8]))
    populated = [number for number, values in rows if any(str(value).strip() for value in values)]
    if any(number not in found for number in populated[1:]):
        raise ValueError("有文案行缺少视频文件名，请核对表格。")
    seen = set()
    for item in items:
        label = Path(item["file"]).name
        item["tags"] = list(dict.fromkeys(tag.strip() for tag in re.split(r"[,，]", item["tags"]) if tag.strip()))
        if not item["title"] or len(item["title"]) > 100 or "\n" in item["title"] or any(
                character in item["title"] + item["desc"] for character in "<>"):
            raise ValueError(f"{label} 的标题须为 1–100 字的单行文字，标题和简介不能包含 < 或 >。")
        if len(item["desc"]) > 5000 or sum(len(tag) + (2 if " " in tag else 0) + 1
                                         for tag in item["tags"]) > 501:
            raise ValueError(f"{label} 的简介或标签超出 YouTube 长度限制。")
        path = Path(item["file"])
        with path.open("rb") as stream:
            if not path.stat().st_size:
                raise ValueError(f"{label} 是空文件。")
            item["digest"] = file_digest(stream, "sha256").hexdigest()
        if item["digest"] in seen:
            raise ValueError(f"{label} 与本批另一文件内容相同。")
        seen.add(item["digest"])
    return tuple(tables), items


class ReceiptBook:
    def __init__(self, directory, channel):
        if not CHANNEL_ID.fullmatch(channel):
            raise ValueError("YouTube 频道 ID 无效。")
        self.path = Path(directory) / "receipts" / (channel + ".json")

    def read(self):
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(rows, dict) or any(
                not isinstance(row, dict) or row.get("status") not in RECEIPT_STATES
                or (row.get("video_id") and not VIDEO_ID.fullmatch(row["video_id"]))
                or (row["status"] == "confirmed" and not row.get("video_id"))
                for row in rows.values()):
            raise ValueError("YouTube 发布记录无法识别，请先核对记录文件。")
        return rows

    def save(self, rows):
        write_local_json(self.path, rows)


async def run_batch(studio, book, items, visibility, audience, stop, progress):
    """After the file chooser is used, only reconcile an existing upload on retry."""
    if visibility not in VISIBILITY or audience not in AUDIENCE:
        raise ValueError("请选择公开范围和儿童受众。")
    rows = book.read()
    confirmed = 0
    for item in items:
        if stop.is_set():
            break
        key = item["digest"]
        name = Path(item["file"]).name
        record = rows.get(key)
        if record and record["status"] == "confirmed":
            confirmed += 1
            continue
        progress(f"正在处理 {name}（{confirmed + 1}/{len(items)}）")
        if record is None:
            with Path(item["file"]).open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() != key:
                    raise ValueError(f"{name} 的文件已变化，请重新读取表格。")
            record = {"status": "checking", "title": item["title"], "description": item["desc"],
                      "visibility": visibility, "video_id": ""}

            def reserve():
                rows[key] = record
                book.save(rows)

            def identify(video_id):
                if not VIDEO_ID.fullmatch(video_id) or any(
                        row.get("video_id") == video_id for digest, row in rows.items() if digest != key):
                    raise ValueError("Studio 返回的作品 ID 无法唯一匹配。")
                record["video_id"] = video_id
                book.save(rows)

            try:
                await studio.upload(item, visibility, audience, reserve, identify, stop)
            except Exception as exc:
                if key in book.read():
                    raise RuntimeError(f"{name} 上传结果待核对：{exc}") from exc
                raise
            if key not in rows:
                break
        video_id = record.get("video_id")
        if not video_id or not await studio.verify(record):
            raise RuntimeError(f"{name} 待核对，请在 Studio 确认作品、公开范围和文案。"
                               + (f" 作品 ID：{video_id}" if video_id else ""))
        record["status"] = "confirmed"
        book.save(rows)
        confirmed += 1
    return confirmed
