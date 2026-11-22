"""Local TikTok preparation and operator-confirmed publication records."""
from collections import OrderedDict
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from batch_preview import check_video_frame
from batch_table import description_with_tags, read_tables
from publisher_lock import AccountLock
from publisher_options import BANGKOK, TIME_FORMAT
from publisher_preferences import write_local_json

HOURLY_LIMIT = 2
DAILY_LIMIT = 10
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
HEADERS = ("视频文件名", "TikTok 文案", "话题", "曼谷发布时间")
COLUMN_NAMES = (
    (HEADERS[0], "视频文件", "文件名", "视频", "视频名称", "视频编号", "文件编号", "filename", "video filename", "video file name", "file", "video"),
    (HEADERS[1], "tiktok文案", "文案", "文案内容", "视频文案", "中文文案", "中文内容", "中文简介", "中文标题", "泰文内容", "泰文文案", "caption", "description", "content", "thai caption", "thai copy", "chinese caption", "chinese copy"),
    (HEADERS[2], "标签", "话题标签", "主题标签", "hashtags", "hash tags", "hashtag", "tag"),
    (HEADERS[3], "发布时间", "排期时间", "曼谷时间", "发布时间（曼谷）", "schedule", "scheduled at"),
)


def write_template(path):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows([
            HEADERS,
            ("lesson001.mp4", "คำจีนวันนี้: 你好 (nǐ hǎo) แปลว่า สวัสดี ลองพูดตามได้เลย",
             "#เรียนภาษาจีน #ภาษาจีน", ""),
            ("lesson002.mp4", "คำจีนวันนี้: 谢谢 (xièxie) แปลว่า ขอบคุณ",
             "#เรียนภาษาจีน", ""),
        ])


def account_key(name):
    username = name.strip().removeprefix("@").casefold()
    if not re.fullmatch(r"[\w.]+", username) or not any(char.isalnum() for char in username):
        raise ValueError("请填写 TikTok Studio 显示的 @用户名。")
    return username


def file_signature(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _source_rows(table, *, allow_no_filename=False):
    matches = []
    for sheet, rows in read_tables(table).items():
        filled = [(n, row) for n, row in rows if any(str(cell).strip() for cell in row)]
        if not filled:
            continue
        header = [" ".join(str(cell).split()).casefold() for cell in filled[0][1]]
        positions = [[i for i, name in enumerate(header) if name in aliases] for aliases in COLUMN_NAMES]
        if (not positions[0] and not allow_no_filename) or not positions[1]:
            continue
        for label, indexes in zip(HEADERS, positions):
            if len(indexes) > 1:
                raise ValueError(f"工作表「{sheet}」有多列可作为「{label}」，请只保留一列。")
        columns = [indexes[0] if indexes else None for indexes in positions]
        matches.append((sheet, columns, filled[1:]))
    if not matches:
        if allow_no_filename:
            raise ValueError("表格需要“文案”或“TikTok 文案”列。")
        raise ValueError("表格需要「视频文件名」或「视频文件」列，以及「文案」或「TikTok 文案」列。话题和曼谷发布时间可省略。")
    if len(matches) > 1:
        raise ValueError("检测到多个 TikTok 文案工作表，请将要读取的工作表另存为单独文件。")
    return matches[0]


def _parse_schedule(value, row_number):
    if not value:
        return ""
    try:
        # Import date text explicitly so that spreadsheet epoch or timezone
        # differences cannot silently shift a TikTok publication slot.
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=BANGKOK).isoformat()
    except ValueError as exc:
        raise ValueError(f"第 {row_number} 行曼谷发布时间请用文本格式 YYYY-MM-DD HH:MM，或留空。") from exc


def read_tiktok_items(folder, table, first_video=""):
    folder, table = Path(folder), Path(table)
    if not folder.is_dir() or not table.is_file():
        raise ValueError("请选择有效的视频文件夹和文案表格。")
    table_signature = file_signature(table)
    sheet, columns, rows = _source_rows(table, allow_no_filename=bool(first_video))
    if not rows:
        raise ValueError("文案表格没有视频条目。")
    videos = {}
    ordered_videos = []
    for path in folder.iterdir():
        if path.suffix.lower() in VIDEO_EXTENSIONS and path.is_file():
            videos.setdefault(path.name.casefold(), []).append(path)
            ordered_videos.append(path)
    if not videos:
        raise ValueError("目录中没有 MP4、MOV、WebM、MKV、AVI 或 M4V 视频。")

    def natural(path):
        return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                     for part in re.split(r"(\d+)", path.name))

    ordered_videos.sort(key=natural)
    start_index = 0
    if first_video:
        selected = Path(first_video).resolve()
        positions = {path.resolve(): index for index, path in enumerate(ordered_videos)}
        if selected not in positions:
            raise ValueError("选择的首个视频不在当前视频文件夹中，请重新选择。")
        start_index = positions[selected]
        if len(ordered_videos) - start_index < len(rows):
            raise ValueError(f"从所选视频开始只有 {len(ordered_videos) - start_index} 个视频，但文案表有 {len(rows)} 行。")

    by_stem = {}
    by_number = {}
    for path in ordered_videos:
        by_stem.setdefault(path.stem.casefold(), []).append(path)
        numbered = re.fullmatch(r"(.*?)(\d+)", path.stem)
        if numbered:
            by_number.setdefault((numbered.group(1).casefold(), int(numbered.group(2))), []).append(path)

    import cv2

    checked = OrderedDict()
    items = {}
    for row_index, (number, values) in enumerate(rows):
        name, text, tags, schedule = (
            str(values[i]).strip() if i is not None and i < len(values) else "" for i in columns
        )
        if (not name and not first_video) or not text:
            missing = "文案" if not text else "视频文件名"
            raise ValueError(f"第 {number} 行缺少{missing}。")
        if first_video:
            path = ordered_videos[start_index + row_index].resolve()
        else:
            basename = name.replace("\\", "/").rsplit("/", 1)[-1]
            candidates = videos.get(basename.casefold(), [])
            if not candidates:
                requested = Path(basename)
                candidates = by_stem.get(requested.stem.casefold(), [])
                if not candidates:
                    numbered = re.fullmatch(r"(.*?)(\d+)", requested.stem)
                    if numbered:
                        candidates = by_number.get((numbered.group(1).casefold(), int(numbered.group(2))), [])
            if len(candidates) != 1:
                if not candidates:
                    raise ValueError(f"第 {number} 行无法匹配「{name}」。可核对文件名，或选择“首个匹配视频”按表格顺序读取。")
                raise ValueError(f"第 {number} 行的「{name}」匹配到多个视频，请选择“首个匹配视频”或填写完整文件名。")
            path = candidates[0].resolve()
        scheduled_at = _parse_schedule(schedule, number)
        signature = file_signature(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest in items:
            raise ValueError(f"「{name}」与本表另一行的视频内容相同。")
        caption = description_with_tags({"desc": text, "tags": ",".join(re.split(r"[\s,，;；]+", tags))})
        error = check_video_frame(str(path), cv2, checked)
        if file_signature(path) != signature:
            raise ValueError(f"「{name}」在读取期间发生变化，请重新读取。")
        items[digest] = {"file": str(path), "signature": signature, "caption": caption,
                         "scheduled_at": scheduled_at, "error": error, "status": "pending"}
    if file_signature(table) != table_signature:
        raise ValueError("表格在读取期间发生变化，请重新读取。")
    return sheet, items


def _check_capacity(items, slot, *, hourly_limit=HOURLY_LIMIT, daily_limit=DAILY_LIMIT):
    hour = timedelta(hours=1)
    reserved = [datetime.fromisoformat(row["reserved_at"]).astimezone(BANGKOK)
                for row in items.values() if row.get("reserved_at")]
    if sum(time.date() == slot.date() for time in reserved) >= daily_limit:
        raise ValueError(f"曼谷时间 {slot:%Y-%m-%d} 已占用 {daily_limit} 条额度。")
    nearby = [time for time in reserved if slot - hour < time < slot + hour]
    for start in [slot, *(time for time in nearby if time <= slot)]:
        if sum(start <= time < start + hour for time in nearby) >= hourly_limit:
            raise ValueError(f"与其他条目的发布时间间隔太近，任意连续 60 分钟最多 {hourly_limit} 条。")


class TikTokQueue:
    def __init__(self, directory, username):
        self.directory = Path(directory)
        self.username = account_key(username)
        digest = hashlib.sha256(self.username.encode("utf-8")).hexdigest()
        self.path = self.directory / f"{digest}.json"

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"account": self.username, "items": {}, "active": []}
        if (not isinstance(data, dict) or data.get("account") != self.username
                or not isinstance(data.get("items"), dict) or not isinstance(data.get("active"), list)):
            raise ValueError("TikTok 队列格式异常，请保留文件并检查。")
        return data

    @contextmanager
    def batch_lock(self):
        lock = AccountLock(self.directory / "batch-locks", self.username).acquire()
        try:
            yield
        finally:
            lock.release()

    @contextmanager
    def edit(self):
        lock = AccountLock(self.directory / "locks", self.username).acquire()
        try:
            state = self.read()
            yield state
            write_local_json(self.path, state)
        finally:
            lock.release()

    def import_items(self, items, *, folder, table, sheet):
        with self.edit() as state:
            for digest, row in items.items():
                previous = state["items"].get(digest)
                if previous is None or previous["status"] == "pending":
                    state["items"][digest] = row
            state.update(active=list(items), folder=str(folder), table=str(table), sheet=sheet)
        return self.read()

    def begin(self, digest, *, now=None):
        now = (now or datetime.now(BANGKOK)).astimezone(BANGKOK)
        with self.edit() as state:
            row = state["items"][digest]
            if row["status"] != "pending" or row.get("error"):
                raise ValueError("这条作品已有处理记录或视频检查失败，请先核对状态。")
            if file_signature(row["file"]) != row["signature"]:
                raise ValueError("视频已变化，请重新读取表格。")
            planned = row.get("scheduled_at")
            slot = datetime.fromisoformat(planned).astimezone(BANGKOK) if planned else now
            if planned and slot <= now:
                raise ValueError("排期已过，请更新表格后重新读取。")
            _check_capacity(state["items"], slot)
            row.update(status="checking", reserved_at=slot.isoformat(), started_at=now.isoformat())
        return row

    def confirm(self, digest, status):
        transitions = {"checking": {"pending", "scheduled", "published"}, "scheduled": {"published"}}
        with self.edit() as state:
            row = state["items"][digest]
            if status not in transitions.get(row["status"], set()):
                raise ValueError("这条作品的状态已变化，请刷新后再操作。")
            if status == "scheduled" and not row["scheduled_at"]:
                raise ValueError("这条作品没有预定发布时间。请确认 Studio 中的实际状态。")
            row.update(status=status, confirmed_at=datetime.now(BANGKOK).isoformat())
            if status == "pending":
                row.pop("reserved_at", None)
                row.pop("started_at", None)
                row.pop("studio_attempt", None)
