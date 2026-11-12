"""Shared publishing settings and minute-accurate Bangkok schedules."""
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk

BANGKOK = timezone(timedelta(hours=7), "Asia/Bangkok")
TIME_FORMAT = "%Y-%m-%d %H:%M"


def normalize_short_title(value):
    """Apply the platform's comma-to-space guidance to a short title."""
    return value.replace(",", " ").replace("，", " ").strip()


def short_title_issues(value):
    issues = []
    # The current live form rejects the IPA schwa in ep206's short title.
    # Keep the source copy intact and let the user choose its replacement.
    if "ə" in value:
        issues.append("视频号当前不接受短标题字符 ə，请改成文字说明")
    if "," in value or "，" in value:
        issues.append("视频号短标题不接受逗号，请替换为空格")
    return issues


def initialize_desktop_app():
    import sys
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SocialAutoUpload.VideoChannelsPublisher")


def scrollable_panel(root, padding):
    outer = ttk.Frame(root)
    outer.pack(fill="both", expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0, bg="#f3f6f5")
    scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    panel = ttk.Frame(canvas, padding=padding)
    window = canvas.create_window((0, 0), window=panel, anchor="nw")
    panel.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
    def wheel(event):
        widget = event.widget
        if not isinstance(widget, tk.Misc) or widget.winfo_toplevel() != root.winfo_toplevel():
            return
        if widget.winfo_class() in {"Text", "Treeview", "TCombobox"}:
            return
        while widget is not None:
            if widget == root:
                canvas.yview_scroll(-int(event.delta / 120), "units")
                return
            widget = getattr(widget, "master", None)
    root.bind_all("<MouseWheel>", wheel, add=True)
    return panel


def planned_times(settings, count, draft=False, validate=True):
    if draft or not settings["scheduled"]:
        return [""] * count
    try:
        first = datetime.strptime(settings["first_time"], TIME_FORMAT).replace(tzinfo=BANGKOK)
    except ValueError as exc:
        raise ValueError("首条发表时间格式应为 年-月-日 时:分，例如 2026-11-06 18:30。") from exc
    interval = int(settings["interval"])
    if interval not in (1, 3, 5):
        raise ValueError("发表间隔请选择 1、3 或 5 分钟。")
    if validate and first <= datetime.now(BANGKOK) + timedelta(hours=2):
        raise ValueError("项目当前要求定时发表提前超过 2 小时，请将首条时间设得更晚一些。")
    return [(first + timedelta(minutes=index * interval)).strftime(TIME_FORMAT) for index in range(count)]


def cli_schedule(bangkok_time):
    # Existing CLI/browser date pickers use this Windows machine's local time.
    # Display all user-entered times in Bangkok and convert only at the boundary.
    return datetime.strptime(bangkok_time, TIME_FORMAT).replace(tzinfo=BANGKOK).astimezone().strftime(TIME_FORMAT)


def option_arguments(settings, schedule=""):
    args = ["--description-only", "--content-label", settings["content_label"]]
    if settings["collection"]:
        args += ["--collection", settings["collection"], "--require-collection"]
    if settings["declare_original"]:
        args.append("--declare-original")
    if schedule:
        args += ["--schedule", cli_schedule(schedule)]
    return args


def options_summary(settings, schedules=None):
    lines = [f"合集：{settings['collection'] or '不添加'}", f"声明原创：{'勾选' if settings['declare_original'] else '不勾选'}", f"视频标注：{settings['content_label'] or '不设置'}"]
    if settings["declare_original"]:
        lines.append("将确认已阅读并同意《原创声明须知》和《使用条款》，并点击弹窗“声明原创”。")
    if schedules and schedules[0]:
        lines += [f"首条发表：{schedules[0]}（曼谷时间 UTC+7）", f"间隔：{settings['interval']} 分钟", f"末条发表：{schedules[-1]}"]
    return "\n".join(lines)


class OptionsPanel(ttk.LabelFrame):
    def __init__(self, parent, batch=False):
        super().__init__(parent, text="发布设置", padding=(8, 5))
        self.collection = tk.StringVar(self)
        self.original = tk.BooleanVar(self, value=False)
        self.label = tk.StringVar(self, value="不设置")
        self.scheduled = tk.BooleanVar(self, value=False)
        self.first = tk.StringVar(self, value=(datetime.now(BANGKOK) + timedelta(hours=3)).strftime(TIME_FORMAT))
        self.interval = tk.StringVar(self, value="1")
        self.columnconfigure(1, weight=1)
        ttk.Label(self, text="添加到合集").grid(row=0, column=0, sticky="w", padx=(0, 8))
        collection = ttk.Entry(self, textvariable=self.collection, width=12)
        collection.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        original = ttk.Checkbutton(self, text="声明原创", variable=self.original)
        original.grid(row=0, column=2, sticky="w", padx=(0, 12))
        scheduled = ttk.Checkbutton(self, text="定时发表", variable=self.scheduled)
        scheduled.grid(row=0, column=3, sticky="w", padx=(0, 16))
        ttk.Label(self, text="视频标注").grid(row=0, column=4, sticky="w", padx=(0, 8))
        label = ttk.Combobox(self, textvariable=self.label, values=("不设置", "含AI生成内容", "内容为转载"), width=15)
        label.grid(row=0, column=5, sticky="ew")

        self.schedule_row = ttk.Frame(self)
        self.schedule_row.grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(self.schedule_row, text="发表时间（曼谷 UTC+7）").pack(side="left", padx=(0, 8))
        self.first_entry = ttk.Entry(self.schedule_row, textvariable=self.first, width=19)
        self.first_entry.pack(side="left")
        self.interval_box = None
        if batch:
            ttk.Label(self.schedule_row, text="间隔").pack(side="left", padx=(12, 0))
            self.interval_box = ttk.Combobox(self.schedule_row, textvariable=self.interval, values=("1", "3", "5"), state="readonly", width=3)
            self.interval_box.pack(side="left", padx=4)
            ttk.Label(self.schedule_row, text="分钟").pack(side="left")
        ttk.Label(self.schedule_row, text="须提前超过 2 小时", style="Muted.TLabel").pack(side="left", padx=(12, 0))
        self.controls = [collection, original, label, scheduled, self.first_entry]
        if self.interval_box is not None:
            self.controls.append(self.interval_box)
        self.scheduled.trace_add("write", self.toggle_schedule)
        self.toggle_schedule()

    def toggle_schedule(self, *_):
        enabled = bool(self.scheduled.get())
        if enabled:
            self.schedule_row.grid()
        else:
            self.schedule_row.grid_remove()
        self.first_entry.configure(state="normal" if enabled else "disabled")
        if self.interval_box is not None:
            self.interval_box.configure(state="readonly" if enabled else "disabled")

    def snapshot(self):
        label = self.label.get().strip()
        return {"collection": self.collection.get().strip(), "declare_original": self.original.get(), "content_label": "" if label == "不设置" else label, "scheduled": self.scheduled.get(), "first_time": self.first.get().strip(), "interval": int(self.interval.get())}
