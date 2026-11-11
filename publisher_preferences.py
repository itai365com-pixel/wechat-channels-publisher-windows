"""Local batch window preferences; never write to a source table."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_local_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
        temp = Path(stream.name)
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_batch_paths(path):
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in ('folder', 'table', 'table_dir', 'sheet')
            if isinstance(raw.get(k), str)}


def remember_batch_paths(path, folder, table, sheet):
    old = load_batch_paths(path)
    data = dict(old)
    if folder and Path(folder).is_dir():
        data['folder'] = str(Path(folder).resolve())
    if table and Path(table).is_file():
        table = Path(table).resolve()
        data.update(table=str(table), table_dir=str(table.parent), sheet=sheet)
    elif not table:
        data.update(table='', sheet='')
    if data != old:
        write_local_json(path, data)
    return data


def first_directory(*paths):
    return next((str(Path(p)) for p in paths if p and Path(p).is_dir()), str(Path.home()))
