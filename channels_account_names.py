"""Store a video's channel nickname separately from its publishing account key."""
from collections import Counter
import hashlib
import json
from pathlib import Path

from publisher_preferences import write_local_json


def _session_identity(account_file):
    raw = account_file.read_bytes()
    state = json.loads(raw)
    usernames = {
        entry["value"]
        for origin in state.get("origins", [])
        if origin.get("origin") == "https://channels.weixin.qq.com"
        for entry in origin.get("localStorage", [])
        if entry.get("name") == "finder_username" and isinstance(entry.get("value"), str) and entry["value"]
    }
    username = next(iter(usernames)) if len(usernames) == 1 else ""
    if username:
        return username, "finder:" + hashlib.sha256(username.encode("utf-8")).hexdigest()
    return "", "session:" + hashlib.sha256(raw).hexdigest()


def _clean_name(value):
    name = " ".join(value.split()) if isinstance(value, str) else ""
    return name if len(name) <= 128 else ""


def read_account_name(account_file):
    account_file = Path(account_file)
    try:
        profile = json.loads(account_file.with_suffix(".profile").read_text(encoding="utf-8"))
        _, session_key = _session_identity(account_file)
        if profile.get("session_key") == session_key:
            return _clean_name(profile.get("nickname"))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return ""


def remember_account_name(account_file, nickname, *, finder_username=""):
    name = _clean_name(nickname)
    if not name:
        return False
    account_file = Path(account_file)
    username, session_key = _session_identity(account_file)
    if username and finder_username and username != finder_username:
        return False
    write_local_json(account_file.with_suffix(".profile"), {"nickname": name, "session_key": session_key})
    return True


def name_from_auth_data(payload):
    if not isinstance(payload, dict) or payload.get("errCode") != 0:
        return "", ""
    data = payload.get("data")
    profile = data.get("finderUser") if isinstance(data, dict) else None
    if not isinstance(profile, dict):
        return "", ""
    username = profile.get("finderUsername")
    return _clean_name(profile.get("nickname")), username if isinstance(username, str) else ""


def account_labels(aliases, cookies):
    names = {alias: read_account_name(cookies / f"tencent_{alias}.json") or alias for alias in aliases}
    counts = Counter(names.values())
    labels = {}
    for alias, name in names.items():
        label = f"{name}（{alias}）" if counts[name] > 1 else name
        while label in labels.values():
            label = f"{label}（{alias}）"
        labels[alias] = label
    return labels
