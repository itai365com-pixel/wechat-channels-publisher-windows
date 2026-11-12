"""Read and check a batch preview without touching the desktop widgets."""
from dataclasses import dataclass, field
from pathlib import Path
import re

from batch_table import match_records, read_tables, records_from_rows, resolve_series, select_records, video_series
from publisher_options import normalize_short_title, planned_times, short_title_issues


@dataclass
class BatchPreview:
    table: str
    sheet: str
    selection: tuple
    sheets: list = field(default_factory=list)
    series: list = field(default_factory=list)
    items: list = field(default_factory=list)
    renamed_titles: list = field(default_factory=list)
    context: tuple | None = None
    table_signature: tuple | None = None
    video_count: int = 0
    matched_count: int = 0
    error: str = ""


def _file_signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def check_video_frame(file, cv2, checked):
    path = Path(file)
    try:
        signature = _file_signature(path)
        if checked.get(file) == signature:
            checked.move_to_end(file)
            return ""
        capture = cv2.VideoCapture(file)
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            return "视频首帧无法解码，请检查或替换视频后更新预览。"
        if _file_signature(path) != signature:
            return "视频在检查时发生变化，请更新预览。"
        checked[file] = signature
        checked.move_to_end(file)
        if len(checked) > 4096:
            checked.popitem(last=False)
        return ""
    except Exception as exc:
        return f"视频检查失败：{exc}"


def _choose_rows(matched, selection, prefix):
    first, last, names = (value.strip() for value in selection)
    if names:
        return select_records(matched, names, default_prefix=prefix)
    if not first and not last:
        return [dict(row) for row in matched]
    if not first or not last:
        raise ValueError("起止编号请同时填写，或同时留空读取全部。")
    if prefix is None:
        raise ValueError("没有统一的文件编号系列，请使用“指定文件”填写完整文件名。")
    start, end = int(first), int(last)
    if start < 1 or end < start:
        raise ValueError("请填写正确的起始编号和结束编号，结束编号不能小于起始编号。")
    return select_records(matched, f"{start}-{end}", default_prefix=prefix)


def read_batch_preview(config, previous_context, previous_selection, checked, cancelled):
    folder_value, table, sheet, choice, first, last, names, settings = config
    result = BatchPreview(table=table.strip(), sheet=sheet, selection=(first, last, names))
    if cancelled.is_set():
        return result
    try:
        folder = Path(folder_value.strip())
        if not folder_value.strip() or not folder.is_dir():
            raise ValueError("视频文件夹不存在，请重新选择。")
        if not result.table:
            tables = [path for path in folder.iterdir()
                      if path.suffix.lower() in {".xlsx", ".csv", ".tsv"} and path.is_file()]
            if len(tables) != 1:
                raise ValueError("请先选择对应系列的文案表格；目录有多份表格时不会自动猜选。")
            result.table = str(tables[0])
        table_path = Path(result.table)
        if not table_path.is_file():
            raise ValueError("文案表格不存在，请重新选择。")
        initial_signature = _file_signature(table_path)
        tables = read_tables(table_path)
        result.sheets = list(tables)
        result.sheet = sheet if sheet in tables else result.sheets[0]
        records = records_from_rows(tables[result.sheet])
        groups = video_series(folder)
        result.series = ["自动识别"] + [prefix or "（纯数字）" for prefix in groups]
        selected = None if choice == "自动识别" else "" if choice == "（纯数字）" else choice
        prefix = resolve_series(records, folder, selected, groups=groups)
        matched, result.video_count = match_records(records, folder, series_prefix=prefix)
        if not matched:
            raise ValueError("没有匹配的视频与文案，请核对文件系列、集数或表格中的文件名。")
        result.matched_count = len(matched)
        if prefix is None:
            parts = [re.fullmatch(r"(.*?)(\d+)", Path(row["file"]).stem) for row in matched]
            matched_groups = {part.group(1).casefold() for part in parts if part}
            if all(parts) and len(matched_groups) == 1:
                prefix = next(iter(matched_groups))
        result.context = (str(folder.resolve()), str(table_path.resolve()), result.sheet, prefix)
        if previous_context is not None and result.context != previous_context and result.selection == previous_selection:
            result.selection = ("", "", "")
        items = _choose_rows(matched, result.selection, prefix)
        schedules = planned_times(settings, len(items))
        import cv2

        for item, schedule in zip(items, schedules):
            if cancelled.is_set():
                return result
            if not item["title"]:
                raise ValueError(f"表格第 {item['source_row']} 行缺少标题。")
            source_title = item.get("short_title") or item["title"]
            title = normalize_short_title(source_title)
            if title != source_title:
                item["short_title"] = title
                result.renamed_titles.append(Path(item["file"]).name)
            item["decode_error"] = check_video_frame(item["file"], cv2, checked)
            if item["decode_error"]:
                item["status"] = "视频无法解码"
            elif short_title_issues(item["short_title"] or item["title"]):
                item["status"] = "短标题需修改"
            else:
                item["status"] = "已匹配，待发布"
            item["schedule"] = schedule
        if _file_signature(table_path) != initial_signature:
            raise ValueError("文案表格在读取时发生变化，请更新预览。")
        result.items = items
        result.table_signature = initial_signature[:2]
    except Exception as exc:
        result.error = str(exc)
    return result
