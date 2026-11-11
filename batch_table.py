"""Read publishing copy without modifying the source spreadsheet."""
import csv
import io
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ALIASES = {
    "file": ("文件名", "视频文件", "视频文件名", "文件路径", "filename", "file", "video"),
    "title": ("标题", "视频标题", "短标题", "title"),
    "desc": ("简介", "介绍+关键词", "介绍＋关键词", "描述", "正文", "文案", "description", "desc"),
    "tags": ("标签", "话题", "tags"),
    "short_title": ("视频号短标题", "短标题", "short_title"),
    "episode": ("集", "集数", "视频编号", "episode"),
}


def expand_file_selection(text, default_prefix="ep"):
    """Accept exact files and inclusive ranges such as ep216-ep277."""
    names = []
    for token in re.split(r"[,，;；\n]+", text):
        token = token.strip()
        if not token:
            continue
        found = re.fullmatch(r"([A-Za-z_]*)(\d+)(?:\.[A-Za-z0-9]+)?\s*[-~～–—至到]\s*([A-Za-z_]*)(\d+)(?:\.[A-Za-z0-9]+)?", token)
        if found:
            left_prefix, first, right_prefix, last = found.groups()
            prefix = left_prefix or right_prefix or default_prefix
            if prefix is None:
                raise ValueError('纯数字编号或范围需要先选择文件系列。')
            if left_prefix and right_prefix and left_prefix.casefold() != right_prefix.casefold():
                raise ValueError(f"编号范围的文件前缀不一致：{token}")
            begin, end = int(first), int(last)
            if end < begin:
                raise ValueError(f"结束编号不能小于起始编号：{token}")
            if end - begin > 10000:
                raise ValueError("编号范围过大，请缩小本次范围。")
            width = max(len(first), len(last)) if first.startswith("0") or last.startswith("0") else 0
            names.extend(prefix + str(number).zfill(width) for number in range(begin, end + 1))
        elif re.fullmatch(r"\d+", token):
            if default_prefix is None:
                raise ValueError('纯数字编号或范围需要先选择文件系列。')
            names.append(default_prefix + str(int(token)))
        else:
            names.append(token)
    return names


def select_records(records, text, default_prefix="ep"):
    names = expand_file_selection(text, default_prefix=default_prefix)
    lookup = {}
    for item in records:
        path = Path(item["file"])
        keys = {path.name.casefold(), path.stem.casefold()}
        number = re.fullmatch(r"(.*?)(\d+)", path.stem)
        if number:
            keys.add((number.group(1) + str(int(number.group(2)))).casefold())
        for key in keys:
            lookup.setdefault(key, []).append(item)
    missing = [name for name in names if name.casefold() not in lookup]
    if missing:
        raise ValueError("以下文件未找到视频与表格文案的准确匹配：" + "、".join(missing[:10]))
    selected = []
    for name in names:
        candidates = lookup[name.casefold()]
        if len(candidates) != 1:
            raise ValueError(f"文件编号有多个匹配：{name}，请填写完整文件名。")
        selected.append(dict(candidates[0]))
    if len({row["file"] for row in selected}) != len(selected):
        raise ValueError("指定文件列表或范围有重复视频，请先核对。")
    return selected


def description_with_tags(record):
    """Keep existing inline hashtags; append only missing legacy tag-column items."""
    description = record.get("desc", "")
    existing = set(re.findall(r"#([^\s#]+)", description))
    extra = []
    for raw in record.get("tags", "").replace("，", ",").split(","):
        tag = raw.strip().lstrip("#")
        if tag and tag not in existing:
            extra.append("#" + tag)
            existing.add(tag)
    if not extra:
        return description
    return description + ("\n" if description else "") + " ".join(extra)


def read_tables(path):
    path = Path(path)
    if path.suffix.lower() in {".csv", ".tsv"}:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        return {path.stem: [(n, row) for n, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), 1)]}
    if path.suffix.lower() != ".xlsx":
        raise ValueError("请选择 .xlsx、.csv 或 .tsv 文案表格。旧版 .xls 请先另存为 .xlsx。")
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("s:si", NS):
                strings.append("".join(t.text or "" for t in si.iterfind(".//s:t", NS)))
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
        relationships = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in relationships}
        tables = {}
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            rel_id = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = targets[rel_id]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            rows = []
            for row in ET.fromstring(z.read(target)).findall("s:sheetData/s:row", NS):
                values = []
                for cell in row.findall("s:c", NS):
                    letters = re.match(r"[A-Z]+", cell.get("r", "A1")).group()
                    index = 0
                    for letter in letters:
                        index = index * 26 + ord(letter) - 64
                    while len(values) < index:
                        values.append("")
                    value = cell.findtext("s:v", "", NS)
                    if cell.get("t") == "s":
                        value = strings[int(value)] if value else ""
                    elif cell.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iterfind(".//s:t", NS))
                    if cell.find("s:f", NS) is not None and not value:
                        raise ValueError(f"工作表 {sheet.get('name')} 的 {cell.get('r')} 是没有缓存结果的公式，请先在 Excel 中计算并保存。")
                    values[index - 1] = value
                rows.append((int(row.get("r")), values))
            tables[sheet.get("name")] = rows
        return tables


def records_from_rows(rows):
    populated = [(n, row) for n, row in rows if any(str(v).strip() for v in row)]
    if not populated:
        raise ValueError("工作表没有数据。")
    _, header = populated[0]
    header = [str(v).strip().lower() for v in header]
    if len(header) != len(set(header)):
        raise ValueError("表头有重复列，请先区分列名。")
    columns = {key: next((header.index(a) for a in aliases if a in header), None) for key, aliases in ALIASES.items()}
    if (columns["file"] is None and columns["episode"] is None) or columns["title"] is None:
        raise ValueError("表格需要“文件名”（或“集”）和“标题”（或“短标题”）列，可选“简介”“介绍+关键词”“标签”列。")
    result = []
    for number, row in populated[1:]:
        record = {key: str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else "" for key, index in columns.items()}
        if not record["file"] and record["episode"]:
            try:
                number_value = float(record["episode"])
                if not number_value.is_integer() or number_value <= 0:
                    raise ValueError()
                record["episode"] = str(int(number_value))
                record["file"] = f"ep{record['episode']}.mp4"
                record["match_by_episode"] = True
            except ValueError:
                raise ValueError(f"表格第 {number} 行的集数不是正整数：{record['episode']}")
        if not record["file"]:
            continue
        record["source_row"] = number
        record["publish_desc"] = description_with_tags(record)
        result.append(record)
    return result


def video_series(folder):
    """Return actual filename prefixes, preserving leading-zero files as-is."""
    groups = {}
    for path in Path(folder).iterdir():
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}:
            number = re.fullmatch(r"(.*?)(\d+)", path.stem)
            if number:
                groups.setdefault(number.group(1).casefold(), []).append(int(number.group(2)))
    return dict(sorted(groups.items()))


def resolve_series(records, folder, selected=None):
    groups = video_series(folder)
    if selected is not None:
        selected = selected.casefold()
        if selected not in groups:
            raise ValueError(f"目录没有 {selected or '纯数字'} 系列的视频，请重新选择系列。")
        return selected
    if any(row.get('match_by_episode') for row in records):
        if len(groups) > 1:
            names = '、'.join(prefix or '纯数字' for prefix in groups)
            raise ValueError(f"目录存在多个系列（{names}），请先在“文件系列”中选择，或在表格提供明确文件名。")
        if not groups:
            raise ValueError('无法从集数匹配视频文件名，请在表格中提供“文件名”列。')
        return next(iter(groups))
    return None


def match_records(records, folder, series_prefix=None):
    folder = Path(folder).resolve()
    videos = {p.name.casefold(): p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}}
    episodes = {}
    for path in videos.values():
        found = re.fullmatch(r"(.*?)(\d+)", path.stem)
        prefix = 'ep' if series_prefix is None else series_prefix.casefold()
        if found and found.group(1).casefold() == prefix:
            episodes.setdefault(str(int(found.group(2))), []).append(path)
    matched = []
    used = set()
    for record in records:
        # Match the exact basename, never an approximate episode number or row order.
        name = record["file"].replace("\\", "/").rsplit("/", 1)[-1]
        path = videos.get(name.casefold())
        if path is not None and series_prefix is not None:
            found = re.fullmatch(r"(.*?)(\d+)", path.stem)
            if not found or found.group(1).casefold() != series_prefix.casefold():
                path = None
        if record.get("match_by_episode"):
            candidates = episodes.get(record["episode"], [])
            if len(candidates) > 1:
                raise ValueError(f"第 {record['episode']} 集有多个同编号视频，请使用明确的“文件名”列匹配。")
            path = candidates[0] if candidates else None
        if path is None:
            continue
        if path.name.casefold() in used:
            raise ValueError(f"表格内有重复文件名：{name}，请先核对文案。")
        used.add(path.name.casefold())
        matched.append({**record, "file": str(path), "status": "待检查"})
    def natural(record):
        return tuple((0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"(\d+)", Path(record["file"]).name))
    return sorted(matched, key=natural), len(videos)
