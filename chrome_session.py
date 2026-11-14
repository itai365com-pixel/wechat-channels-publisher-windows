"""Discover the local endpoint that Chrome exposes when the user enables debugging."""
import os
from pathlib import Path
import re


def current_chrome_endpoint():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("此功能需要 Windows 上正在运行的 Google Chrome。")
    port_file = Path(local) / "Google/Chrome/User Data/DevToolsActivePort"
    try:
        port, path, *_ = port_file.read_text(encoding="utf-8").splitlines()
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ValueError("invalid port")
        if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", path):
            raise ValueError("invalid endpoint")
    except (OSError, ValueError) as exc:
        raise RuntimeError("请在当前 Chrome 的 chrome://inspect/#remote-debugging 启用远程调试，然后重试。") from exc
    return f"ws://127.0.0.1:{int(port)}{path}"
