"""Batch desktop publisher using the installed SAU CLI and spreadsheet copy."""
from datetime import datetime
import asyncio
import hashlib
import json
import re
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from batch_table import read_tables, records_from_rows, match_records, description_with_tags, select_records, video_series, resolve_series
from video_channels_gui import Publisher, ROOT
from publisher_options import OptionsPanel, option_arguments, options_summary, planned_times, scrollable_panel, initialize_desktop_app, short_title_issues

DEFAULT_FOLDER = Path.home() / "Videos"
BATCH_DIR = ROOT / ".local" / "gui" / "batches"
LOG_DIR = ROOT / ".local" / "gui" / "logs"


def failed_before_submit(log_path):
    if not log_path:
        return False
    path = Path(log_path).resolve()
    if not path.is_relative_to(LOG_DIR.resolve()) or not path.is_file():
        return False
    content = path.read_text(encoding="utf-8", errors="replace")
    if "视频发布成功" in content or "视频草稿保存成功" in content:
        return False
    # These errors originate exclusively in the preparation step, before
    # submit_publish is called. Historical evidence remains untouched.
    return any(line.strip() in {"未找到“声明原创”复选框，已停止发布", "封面在等待时间内未生成完成，尚未点击发表"} or line.strip().startswith(("原创声明确认失败，尚未点击发表：", "短标题校验失败，尚未点击发表：", "指定合集未成功选择，尚未点击发表：", "指定合集未成功选择，已停止发布：")) for line in content.splitlines())


def fingerprint(record, account, draft):
    video_hash = hashlib.sha256()
    with Path(record["file"]).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            video_hash.update(chunk)
    copy = {"short_title": record.get("short_title") or record.get("title", ""), "description": record.get("publish_desc", description_with_tags(record))}
    return hashlib.sha256(json.dumps([account, draft, video_hash.hexdigest(), copy], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def prior_attempts(*, with_evidence=False):
    result = {}
    for path in BATCH_DIR.glob("*.json"):
        # Unreadable history must not silently disable duplicate protection.
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data.get("items", []):
            if row.get("status") == "unknown" and row.get("returncode") == 1 and failed_before_submit(row.get("log")):
                continue
            if row.get("status") in {"completed", "running", "unknown"}:
                status = row['status']
                evidence = {'status':status, 'manifest':str(path),
                    'confirmed': status == 'completed' and not data.get('requires_backend_audit') and
                    ((row.get('platform_verification') or {}).get('verified') is True or
                     row.get('phase') == 'confirmed_on_platform' or bool(data.get('collection_verification')))}
                previous = result.get(row['fingerprint'])
                priority = {'completed':1, 'running':2, 'unknown':3}
                if previous is None or priority[status] > priority[previous['status']] or (priority[status] == priority[previous['status']] and evidence['confirmed']):
                    result[row['fingerprint']] = evidence
    return result if with_evidence else {key:value['status'] for key,value in result.items()}


def filter_previously_completed(items, account, draft):
    previous = prior_attempts(with_evidence=True)
    pending, skipped = [], []
    for item in items:
        key = fingerprint(item, account, draft)
        old = previous.get(key)
        if old and old['confirmed']:
            skipped.append({'file':item['file'], 'fingerprint':key, 'manifest':old['manifest']})
        elif old:
            raise ValueError(f"{Path(item['file']).name} 已有运行中或结果待核对的记录，不能自动重传；请先核对后台结果。")
        else:
            pending.append({**item, 'fingerprint':key})
    return pending, skipped


class BatchPublisher(Publisher):
    def __init__(self, root):
        self.folder = tk.StringVar(root, value=str(DEFAULT_FOLDER))
        sources = sorted(p for p in DEFAULT_FOLDER.glob("*") if p.suffix.lower() in {".xlsx", ".csv", ".tsv"})
        self.table_path = tk.StringVar(root, value=str(sources[0]) if len(sources) == 1 else "")
        self.sheet = tk.StringVar(root)
        self.start_number = tk.StringVar(root, value="")
        self.count = tk.StringVar(root, value="")
        self.series = tk.StringVar(root, value="自动识别")
        self.filenames = tk.StringVar(root, value="")
        self.summary = tk.StringVar(root, value="请先读取文案表，预览本次要发布的视频。")
        self.stop_requested = threading.Event()
        self.items = []
        self.batch_results = None
        self.tables = {}
        self.last_preview_context = None
        self.last_preview_selection = None
        super().__init__(root)
        root.title("视频号批量发布助手 v1.11.1")
        root.geometry(f"{min(1420, root.winfo_screenwidth() - 100)}x{min(960, root.winfo_screenheight() - 100)}")
        root.minsize(1060, 850)
        root.after(300, self.preview)

    def make_ui(self):
        self.configure_style()
        panel = scrollable_panel(self.root, (26, 20))
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(14, weight=1)
        heading = ttk.Frame(panel)
        heading.grid(row=0, column=0, columnspan=3, sticky='ew')
        ttk.Label(heading, text="视频号批量发布", style="Heading.TLabel").pack(side='left')
        ttk.Button(heading, text='词映官网', command=self.open_wordmotion).pack(side='right')
        ttk.Label(panel, text="5 个标签并行上传，封面先就绪的先发表；每条独立核对文案与合集。", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 16))
        accounts = ttk.Frame(panel)
        accounts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(accounts, text="视频号账号", style="Field.TLabel").pack(side="left", padx=(0, 12))
        self.account_box = ttk.Combobox(accounts, textvariable=self.account, state="readonly", width=20)
        self.account_box.pack(side="left")
        self.account_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_account_status())
        for text, callback in (("检查登录", self.check_login), ("扫码登录", self.login)):
            button = ttk.Button(accounts, text=text, command=callback)
            button.pack(side="left", padx=(10, 0))
            self.controls.append(button)
        stats = ttk.Button(accounts, text='近7天Top10与分析', command=self.show_statistics)
        stats.pack(side='left', padx=(10,0))
        self.controls.append(stats)
        ttk.Label(accounts, textvariable=self.quota_status, style='Muted.TLabel').pack(side='left', padx=(18, 0))
        ttk.Label(panel, textvariable=self.login_status, style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 12))

        for row, label, variable, callback in ((4, "视频文件夹", self.folder, self.choose_folder), (5, "文案表格", self.table_path, self.choose_table)):
            ttk.Label(panel, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            entry = ttk.Entry(panel, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            button = ttk.Button(panel, text="选择…", command=callback)
            button.grid(row=row, column=2, padx=(10, 0), pady=4)
            self.controls.extend([entry, button])
        options = ttk.Frame(panel)
        options.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(10, 10))
        ttk.Label(options, text="工作表").pack(side="left")
        self.sheet_box = ttk.Combobox(options, textvariable=self.sheet, state="readonly", width=17)
        self.sheet_box.pack(side="left", padx=(8, 18))
        ttk.Label(options, text="文件系列").pack(side="left")
        self.series_box = ttk.Combobox(options, textvariable=self.series, state="readonly", width=12, values=('自动识别',))
        self.series_box.pack(side="left", padx=(8, 14))
        self.series_box.bind('<<ComboboxSelected>>', self.change_series)
        ttk.Label(options, text="视频编号 从").pack(side="left")
        start = ttk.Spinbox(options, from_=1, to=99999, textvariable=self.start_number, width=5)
        start.pack(side="left", padx=5)
        ttk.Label(options, text="至").pack(side="left")
        count = ttk.Spinbox(options, from_=1, to=99999, textvariable=self.count, width=5)
        count.pack(side="left", padx=5)
        ttk.Label(options, text="（留空=全部）").pack(side="left")
        preview = ttk.Button(options, text="读取表格 / 更新预览", command=self.preview)
        preview.pack(side="right")
        self.controls.extend([self.sheet_box, self.series_box, start, count, preview])
        named = ttk.Frame(panel)
        named.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 9))
        named.columnconfigure(1, weight=1)
        ttk.Label(named, text="指定文件（可选）").grid(row=0, column=0, padx=(0, 8))
        names_entry = ttk.Entry(named, textvariable=self.filenames)
        names_entry.grid(row=0, column=1, sticky="ew")
        self.controls.append(names_entry)
        ttk.Label(named, text="支持 z001-z010、ep216-ep277 或文件名列表；纯数字范围按当前系列匹配，填写后优先使用这里。", style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(panel, textvariable=self.summary, style="Muted.TLabel").grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 7))

        columns = ("number", "file", "short_title", "desc", "when", "status")
        table = ttk.Frame(panel)
        table.grid(row=9, column=0, columnspan=3, sticky="ew")
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=10, selectmode="browse")
        for key, label, width in (("number", "序号", 45), ("file", "视频文件", 110), ("short_title", "短标题", 240), ("desc", "简介", 510), ("when", "发表时间（曼谷）", 165), ("status", "状态", 145)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=90 if key in {"short_title", "desc"} else 45, stretch=(key in {"short_title", "desc"}))
        self.tree.grid(row=0, column=0, sticky="ew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.tag_configure("invalid", foreground="#b42318")
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.tree.bind("<Double-1>", self.edit_item)
        self.detail = ScrolledText(panel, height=4, wrap="word", font=("Microsoft YaHei UI", 10), padx=8, pady=6, state="disabled")
        self.detail.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(8, 10))

        self.options = OptionsPanel(panel, batch=True)
        self.options.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.controls.extend(self.options.controls)

        actions = ttk.Frame(panel)
        actions.grid(row=12, column=0, columnspan=3, sticky="ew")
        self.stop_button = ttk.Button(actions, text="本轮结束后停止", state="disabled", command=self.request_stop)
        self.stop_button.pack(side="left")
        edit = ttk.Button(actions, text="编辑本条文案", command=self.edit_item)
        edit.pack(side="left", padx=(10, 0))
        self.controls.append(edit)
        resume = ttk.Button(actions, text="继续已有队列", command=self.choose_resume)
        resume.pack(side="left", padx=(10, 0))
        self.controls.append(resume)
        publish = ttk.Button(actions, text="开始批量发布", style="Publish.TButton", command=lambda: self.publish_batch(False))
        publish.pack(side="right", padx=(10, 0))
        self.options.scheduled.trace_add("write", lambda *_: publish.configure(text="批量提交定时发表" if self.options.scheduled.get() else "开始批量发布"))
        draft = ttk.Button(actions, text="批量保存草稿", command=lambda: self.publish_batch(True))
        draft.pack(side="right")
        self.controls.extend([draft, publish])
        progress = ttk.Frame(panel)
        progress.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(12, 7))
        ttk.Label(progress, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(progress, mode="indeterminate", length=110)
        self.progress.pack(side="right")
        self.log = ScrolledText(panel, height=6, wrap="word", font=("Microsoft YaHei UI", 9), bg="#eaf0ed", relief="flat", padx=8, pady=6, state="disabled")
        self.log.grid(row=14, column=0, columnspan=3, sticky="ew")
        self.append_log("就绪。原视频和表格会保留在原位置；选中一条可查看完整文案。\n")

    def choose_folder(self):
        selected = filedialog.askdirectory(parent=self.root, title="选择待发布视频文件夹", initialdir=self.folder.get())
        if selected:
            self.folder.set(selected)
            self.reset_selection()
            self.series.set('自动识别')
            self.use_folder_table()
            self.preview()

    def choose_table(self):
        selected = filedialog.askopenfilename(parent=self.root, title="选择发布文案表格", initialdir=self.folder.get(), filetypes=[("文案表格", "*.xlsx *.csv *.tsv"), ("所有文件", "*.*")])
        if selected:
            self.table_path.set(selected)
            self.sheet.set("")
            self.series.set('自动识别')
            self.reset_selection()
            self.preview()

    def reset_selection(self):
        self.start_number.set('')
        self.count.set('')
        self.filenames.set('')

    def use_folder_table(self):
        sources = sorted(p for p in Path(self.folder.get()).glob('*') if p.suffix.lower() in {'.xlsx', '.csv', '.tsv'} and p.is_file())
        self.table_path.set(str(sources[0]) if len(sources) == 1 else '')
        self.sheet.set('')

    def change_series(self, _event=None):
        self.reset_selection()
        self.preview()

    def preview(self):
        if self.busy:
            return
        self.items = []
        for key in self.tree.get_children():
            self.tree.delete(key)
        try:
            if not Path(self.table_path.get()).is_file():
                self.use_folder_table()
            if not self.table_path.get():
                raise ValueError('请先选择对应系列的文案表格；目录有多份表格时不会自动猜选。')
            self.tables = read_tables(self.table_path.get())
            sheets = list(self.tables)
            self.sheet_box["values"] = sheets
            if self.sheet.get() not in sheets:
                self.sheet.set(sheets[0])
            records = records_from_rows(self.tables[self.sheet.get()])
            groups = video_series(self.folder.get())
            self.series_box['values'] = ['自动识别'] + [prefix or '（纯数字）' for prefix in groups]
            choice = self.series.get()
            prefix = resolve_series(records, self.folder.get(), None if choice == '自动识别' else '' if choice == '（纯数字）' else choice)
            matched, video_count = match_records(records, self.folder.get(), series_prefix=prefix)
            if not matched:
                raise ValueError('没有匹配的视频与文案，请核对文件系列、集数或表格中的文件名。')
            if prefix is None:
                parts = [re.fullmatch(r'(.*?)(\d+)', Path(row['file']).stem) for row in matched]
                matched_groups = {part.group(1).casefold() for part in parts if part}
                if all(parts) and len(matched_groups) == 1:
                    prefix = next(iter(matched_groups))
            context = (str(Path(self.folder.get()).resolve()), str(Path(self.table_path.get()).resolve()), self.sheet.get(), prefix)
            selection = (self.start_number.get(), self.count.get(), self.filenames.get())
            if self.last_preview_context is not None and context != self.last_preview_context and selection == self.last_preview_selection:
                self.reset_selection()
            names = self.filenames.get().strip()
            if names:
                self.items = select_records(matched, names, default_prefix=prefix)
            else:
                first, last = self.start_number.get().strip(), self.count.get().strip()
                if not first and not last:
                    self.items = [dict(row) for row in matched]
                else:
                    if not first or not last:
                        raise ValueError('起止编号请同时填写，或同时留空读取全部。')
                    if prefix is None:
                        raise ValueError('没有统一的文件编号系列，请使用“指定文件”填写完整文件名。')
                    start, end = int(first), int(last)
                    if start < 1 or end < start:
                        raise ValueError("请填写正确的起始编号和结束编号，结束编号不能小于起始编号。")
                    self.items = select_records(matched, f"{start}-{end}", default_prefix=prefix)
            settings = self.options.snapshot()
            schedules = planned_times(settings, len(self.items))
            import cv2
            for index, item in enumerate(self.items):
                if not item["title"]:
                    raise ValueError(f"表格第 {item['source_row']} 行缺少标题。")
                capture = None
                item['decode_error'] = ''
                try:
                    capture = cv2.VideoCapture(item['file'])
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        item['decode_error'] = '视频首帧无法解码，请检查或替换视频后更新预览。'
                except Exception as exc:
                    item['decode_error'] = f'视频检查失败：{exc}'
                finally:
                    if capture is not None:
                        capture.release()
                item["status"] = "视频无法解码" if item['decode_error'] else "短标题需修改" if short_title_issues(item["short_title"] or item["title"]) else "已匹配，待发布"
                item["schedule"] = schedules[index]
                self.tree.insert("", "end", iid=str(index), values=(index + 1, Path(item["file"]).name, item["short_title"] or item["title"], item.get("publish_desc", item["desc"]), schedules[index] or "立即发表", item["status"]), tags=("invalid",) if item['decode_error'] or item["status"] == "短标题需修改" else ())
            order = "按指定文件顺序" if names else "按文件名自然排序"
            series_label = (prefix or '纯数字') if prefix is not None else '按明确文件名'
            bad = [row for row in self.items if row.get('decode_error')]
            extra = f'；{len(bad)} 条视频无法解码，已标红。' if bad else ''
            self.summary.set(f"识别系列：{series_label}；文件夹 {video_count} 个视频，表格匹配 {len(matched)} 条；本次预览 {len(self.items)} 条（{order}）{extra or '。'}")
            self.status.set(f'已读取 {len(self.items)} 条 · {len(bad)} 条视频需检查' if bad else f"已准备 {len(self.items)} 条 · 请核对标题和简介")
            if bad:
                self.append_log('视频检查失败：' + '、'.join(Path(row['file']).name for row in bad) + '。请替换视频后更新预览，或在指定文件中排除这些条目。\n')
            if self.items:
                self.tree.selection_set("0")
                self.show_detail()
            self.preview_config = self.current_config()
            self.table_signature = (Path(self.table_path.get()).stat().st_size, Path(self.table_path.get()).stat().st_mtime_ns)
            self.last_preview_context = context
            self.last_preview_selection = (self.start_number.get(), self.count.get(), self.filenames.get())
        except Exception as exc:
            self.items = []
            for key in self.tree.get_children():
                self.tree.delete(key)
            self.summary.set(f"无法准备队列：{exc}")
            self.status.set("请检查表格和视频文件夹")
            self.append_log(f"读取失败：{exc}\n")

    def show_detail(self, _event=None):
        selected = self.tree.selection()
        if not selected or not self.items:
            return
        row = self.items[int(selected[0])]
        text = f"视频：{Path(row['file']).name}（表格第 {row['source_row']} 行）\n短标题：{row['short_title'] or row['title']}\n简介：{row.get('publish_desc', row['desc'])}\n发表时间：{row.get('schedule') or '立即发表'}"
        if row.get('decode_error'):
            text += '\n检查结果：' + row['decode_error']
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.configure(state="disabled")

    def edit_item(self, _event=None):
        if self.busy:
            return
        selected = self.tree.selection()
        if not selected:
            return
        index = int(selected[0])
        item = self.items[index]
        dialog = tk.Toplevel(self.root)
        dialog.title(f"编辑本条文案 · {Path(item['file']).name}")
        dialog.geometry("760x450")
        dialog.transient(self.root)
        dialog.grab_set()
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        ttk.Label(panel, text="短标题", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        title = tk.StringVar(dialog, value=item["short_title"] or item["title"])
        entry = ttk.Entry(panel, textvariable=title)
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        ttk.Label(panel, text="简介（#标签保留在这里）", style="Field.TLabel").grid(row=2, column=0, sticky="w")
        description = ScrolledText(panel, height=8, wrap="word", font=("Microsoft YaHei UI", 10), padx=8, pady=7)
        description.grid(row=3, column=0, sticky="nsew", pady=(6, 10))
        description.insert("1.0", item.get("publish_desc", item["desc"]))
        ttk.Label(panel, text="仅修改本次队列，原表格不变。重新读取表格会恢复表格原文。", style="Muted.TLabel").grid(row=4, column=0, sticky="w")
        def save():
            value = title.get().strip()
            issues = short_title_issues(value)
            if not value or issues:
                messagebox.showwarning("短标题需修改", "\n".join(issues) if issues else "短标题不能为空。", parent=dialog)
                return
            item["short_title"] = value
            item["title"] = value
            item["desc"] = description.get("1.0", "end-1c").strip()
            item["publish_desc"] = item["desc"]
            item["tags"] = ""
            item["status"] = "视频无法解码" if item.get('decode_error') else "已编辑，待发布"
            self.tree.set(str(index), "short_title", value)
            self.tree.set(str(index), "desc", item["desc"])
            self.tree.set(str(index), "status", item["status"])
            self.tree.item(str(index), tags=('invalid',) if item.get('decode_error') else ())
            self.show_detail()
            dialog.destroy()
        buttons = ttk.Frame(panel)
        buttons.grid(row=5, column=0, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 10))
        ttk.Button(buttons, text="保存到队列", command=save).pack(side="left")
        entry.focus_set()

    def publish_batch(self, draft):
        if self.busy:
            return
        if not self.items:
            messagebox.showwarning("请先预览", "请先读取文案表并检查视频匹配结果。", parent=self.root)
            return
        current = self.current_config()
        if current != self.preview_config:
            messagebox.showwarning("请更新预览", "设置已变化，请先更新预览再发布。", parent=self.root)
            return
        try:
            issues = [f"{Path(row['file']).name}：{'；'.join(short_title_issues(row['short_title'] or row['title']))}" for row in self.items if short_title_issues(row['short_title'] or row['title'])]
            if issues:
                raise ValueError("请先编辑以下短标题：\n" + "\n".join(issues[:5]))
            bad = [Path(row['file']).name for row in self.items if row.get('decode_error')]
            if bad:
                raise ValueError('以下视频无法解码：' + '、'.join(bad[:10]) + '。请替换视频后更新预览，或使用“指定文件”排除这些条目。')
            usage = self.check_daily_capacity() if not draft else None
            source = Path(self.table_path.get()).stat()
            if (source.st_size, source.st_mtime_ns) != self.table_signature:
                raise ValueError("表格已修改，请更新预览后再发布。")
            if not (ROOT / "cookies" / f"tencent_{self.account.get()}.json").is_file():
                raise ValueError("请先扫码登录视频号。")
            for history in BATCH_DIR.glob("*.json"):
                previous_batch = json.loads(history.read_text(encoding="utf-8"))
                if previous_batch.get("account") == self.account.get() and any(row.get("status") == "running" for row in previous_batch.get("items", [])):
                    raise ValueError("此账号还有运行中或尚未核对结束的批次，请先处理旧窗口中的任务。")
            settings = self.options.snapshot()
            eligible, skipped = filter_previously_completed(self.items, self.account.get(), draft)
            for item in skipped:
                self.append_log(f'自动跳过已核验发表的视频：{Path(item["file"]).name}\n')
                for index, preview in enumerate(self.items):
                    if preview['file'] == item['file']:
                        self.tree.set(str(index), 'status', '已发布，自动跳过')
            if not eligible:
                messagebox.showinfo('无需重复发布', '所选视频均已有后台核验完成记录，已全部跳过。', parent=self.root)
                return
            schedules = planned_times(settings, len(eligible), draft=draft)
            jobs = []
            for index, item in enumerate(eligible):
                jobs.append({**item, "status": "pending", "schedule": schedules[index]})
            action = "批量保存草稿" if draft else "批量定时发表" if settings["scheduled"] else "批量发布"
            names = "、".join(Path(item["file"]).stem for item in jobs)
            question = f"账号：{self.account.get()}\n数量：{len(jobs)} 条\n视频：{names}\n文案来源：{Path(self.table_path.get()).name} / {self.sheet.get()}\n\n"
            question += options_summary(settings, schedules) + "\n\n"
            if skipped:
                question += f'已自动跳过 {len(skipped)} 条后台核验完成的视频。\n'
            question += "确认后将逐条上传并保存草稿。" if draft else "确认后将逐条上传并提交定时发表。" if schedules[0] else "确认后将逐条上传并立即发表到视频号。"
            question += "\n最多同时打开 5 个标签上传，按封面就绪顺序逐条提交；各条定时时间按预览保持。"
            if usage:
                question += f'\n本账号每日上限 {usage["limit"]} 条，今天最多再提交 {usage["remaining"]} 条；超出部分保留在队列，明天手动继续。'
            if not messagebox.askyesno(f"确认{action}", question, parent=self.root):
                return
            BATCH_DIR.mkdir(parents=True, exist_ok=True)
            self.manifest_path = BATCH_DIR / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
            self.manifest = {"account": self.account.get(), "draft": draft, "options": settings, "source": self.table_path.get(), "sheet": self.sheet.get(), "created_at": datetime.now().astimezone().isoformat(), "items": jobs, "parallel_tabs": 5}
            self.manifest['skipped_completed'] = skipped
            self.items = jobs
            for key in self.tree.get_children():
                self.tree.delete(key)
            for index, row in enumerate(jobs):
                self.tree.insert('', 'end', iid=str(index), values=(index + 1, Path(row['file']).name,
                    row.get('short_title') or row['title'], row.get('publish_desc', row.get('desc', '')),
                    row.get('schedule') or '立即发表', '待发布'))
            self.summary.set(f'本次待发布 {len(jobs)} 条 · 已自动跳过 {len(skipped)} 条已核验视频')
            if settings['declare_original']:
                self.manifest['original_terms_batch_confirmed_at'] = datetime.now().astimezone().isoformat()
            self.save_manifest()
            self.stop_requested.clear()
            self.stop_button.configure(state="normal")
            self.batch_results = None
            if not self.start([], action):
                self.stop_button.configure(state="disabled")
        except Exception as exc:
            messagebox.showerror("无法开始批次", str(exc), parent=self.root)

    def save_manifest(self):
        temp = self.manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.manifest_path)

    def choose_resume(self):
        path = filedialog.askopenfilename(parent=self.root, title='选择未完成的发布队列',
                                         initialdir=BATCH_DIR, filetypes=[('发布队列', '*.json')])
        if path:
            self.resume_batch(Path(path), confirm=True)

    def resume_batch(self, path, confirm=False, *, auto_start=True):
        if self.busy:
            return
        try:
            path = Path(path).resolve()
            record = json.loads(path.read_text(encoding='utf-8'))
            from channels_parallel import ParallelPublisher
            # Read-only checks before opening browsers or rewriting a receipt.
            checker = ParallelPublisher(path, record=record)
            jobs = checker.preflight()
            if not jobs:
                raise ValueError('这份队列没有可继续的条目。')
            if confirm:
                completed = sum(row.get('status') == 'completed' for row in record['items'])
                question = f'账号：{record["account"]}\n已完成 {completed} 条，将跳过。\n继续未完成 {len(jobs)} 条。\n\n' + options_summary(record['options'])
                question += '\n\n同时使用 5 个标签上传，封面就绪后依次发表。确认继续？'
                if not messagebox.askyesno('继续已有队列', question, parent=self.root):
                    return
            self.manifest_path, self.manifest = path, record
            record['parallel_tabs'] = 5
            self.account.set(record['account'])
            self.folder.set(str(Path(record['items'][0]['file']).parent))
            self.table_path.set(record.get('source', ''))
            self.sheet.set(record.get('sheet', ''))
            self.sheet_box['values'] = (record.get('sheet', ''),)
            settings = record['options']
            self.options.collection.set(settings['collection'])
            self.options.original.set(settings['declare_original'])
            self.options.label.set(settings['content_label'] or '不设置')
            self.options.scheduled.set(settings['scheduled'])
            self.options.first.set(settings.get('first_time', ''))
            self.options.interval.set(str(settings['interval']))
            self.items = record['items']
            for key in self.tree.get_children():
                self.tree.delete(key)
            for index, row in enumerate(self.items):
                label = '已发布' if row.get('status') == 'completed' else '待重试' if row.get('status') == 'failed_before_submit' else '待发布'
                self.tree.insert('', 'end', iid=str(index), values=(index + 1, Path(row['file']).name,
                    row.get('short_title') or row['title'], row.get('publish_desc', row.get('desc', '')),
                    row.get('schedule') or '立即发表', label))
            self.summary.set(f'继续队列：{path.name} · 待处理 {len(jobs)} 条 · 5 标签并行')
            if not auto_start:
                completed = sum(row.get('status') == 'completed' for row in record['items'])
                self.preview_config = None
                self.batch_results = f'本批已核验 {completed}/{len(record["items"])} 条'
                self.summary.set(f'已载入队列：{path.name} · 已完成 {completed} 条 · 待处理 {len(jobs)} 条')
                self.status.set('队列已暂停 · 可通过“继续已有队列”确认后继续')
                self.stop_button.configure(state='disabled')
                return
            self.stop_requested.clear()
            self.stop_button.configure(state='normal')
            self.batch_results = None
            action = '批量保存草稿' if record.get('draft') else '批量定时发表' if settings['scheduled'] else '批量发布'
            if not self.start([], action):
                self.stop_button.configure(state='disabled')
        except Exception as exc:
            messagebox.showerror('无法继续队列', str(exc), parent=self.root)

    def run_cli(self, arguments, action):
        if action not in {"批量发布", "批量保存草稿", "批量定时发表"}:
            return super().run_cli(arguments, action)
        if self.manifest.get('parallel_tabs', 1) > 1:
            return self.run_parallel(action)
        final_code = 0
        completed = 0
        log_path = None
        try:
            jobs = self.manifest["items"]
            # Check history again while holding the account lock, closing the
            # race between two windows that prepared their previews together.
            previous = prior_attempts()
            if any(item["fingerprint"] in previous for item in jobs):
                raise RuntimeError("另一个批次已处理相同内容，请核对历史记录后更新队列。")
            for index, item in enumerate(jobs):
                if self.stop_requested.is_set():
                    break
                item["status"] = "running"
                self.save_manifest()
                self.events.put(("batch_row", (index, "正在处理", f"正在处理 {index + 1}/{len(jobs)}：{Path(item['file']).name}")))
                args = ["upload-video", "--account", self.manifest["account"], "--file", item["file"], "--title", item["title"], "--desc", item.get("publish_desc", description_with_tags(item)), "--tags", "", "--headed"]
                args.extend(option_arguments(self.manifest["options"], item["schedule"]))
                args.extend(["--short-title", item["short_title"] or item["title"]])
                if self.manifest["draft"]:
                    args.append("--draft")
                code, log_path = self.execute_cli(args, f"{action} {index + 1}/{len(jobs)}")
                output = log_path.read_text(encoding="utf-8") if log_path else ""
                evidence = "视频草稿保存成功" if self.manifest["draft"] else "视频发布成功"
                item["log"] = str(log_path) if log_path else None
                item["returncode"] = code
                if code == 0 and evidence in output:
                    item["status"] = "completed"
                    completed += 1
                    label = "草稿已保存" if self.manifest["draft"] else "工具确认已定时" if item["schedule"] else "工具确认已发布"
                else:
                    known_failure = code != 0 and failed_before_submit(log_path)
                    item["status"] = "failed_before_submit" if known_failure else "unknown"
                    label = "设置失败，未发表" if known_failure else "失败 / 结果待核对"
                    final_code = 1
                item["finished_at"] = datetime.now().astimezone().isoformat()
                self.save_manifest()
                self.events.put(("batch_row", (index, label, f"本批已完成 {completed}/{len(jobs)} 条")))
                if final_code:
                    self.events.put(("log", "批次已停止。请先核对视频号后台，确认本条结果后再处理后续视频。\n"))
                    break
            self.batch_results = f"本批完成 {completed}/{len(jobs)} 条" + (" · 已按要求停止" if self.stop_requested.is_set() else "")
        except Exception as exc:
            final_code = 1
            self.events.put(("log", f"批次异常，已停止：{exc}\n"))
            self.batch_results = f"批次中断，已完成 {completed} 条 · 请核对记录"
        finally:
            self.events.put(("log", f"批次记录：{self.manifest_path}\n"))
            self.events.put(("done", (final_code, log_path, action)))

    def request_stop(self):
        self.stop_requested.set()
        self.stop_button.configure(state="disabled")
        self.status.set("已请求停止 · 当前最多 5 条结束后不再补入新任务")

    def run_parallel(self, action):
        from channels_parallel import ParallelPublisher
        code = 1
        try:
            runner = ParallelPublisher(self.manifest_path, record=self.manifest,
                                       event=lambda kind, payload: self.events.put((kind, payload)),
                                       stop=self.stop_requested, tabs=self.manifest.get('parallel_tabs', 5))
            code = asyncio.run(runner.run(acquire_lock=False))
        except Exception as exc:
            self.events.put(('log', f'并行批次停止：{exc}\n'))
        finally:
            completed = sum(row.get('status') == 'completed' for row in self.manifest['items'])
            self.batch_results = f'本批完成 {completed}/{len(self.manifest["items"])} 条'
            if self.manifest.get('status') == 'daily_limit_reached':
                self.batch_results += ' · 已达到今日单账号20条上限，剩余队列明天可继续'
            self.events.put(('done', (code, None, action)))

    def on_event(self, kind, payload):
        if kind == "batch_row":
            index, label, status = payload
            self.tree.set(str(index), "status", label)
            self.status.set(status)

    def task_finished(self, code, action):
        super().task_finished(code, action)
        self.sheet_box.configure(state="readonly")
        self.series_box.configure(state='readonly')
        if action in {"批量发布", "批量保存草稿", "批量定时发表"}:
            self.stop_button.configure(state="disabled")
            self.sheet_box.configure(state="readonly")
            limited = getattr(self, 'manifest', {}).get('status') == 'daily_limit_reached'
            self.status.set((self.batch_results or "批次已结束") + (" · 请核对视频号后台" if code and not limited else ""))

    def current_config(self):
        return (self.folder.get(), self.table_path.get(), self.sheet.get(), self.series.get(), self.start_number.get(), self.count.get(), self.filenames.get(), self.options.snapshot())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument('--resume', type=Path)
    entry.add_argument('--inspect', type=Path)
    args = parser.parse_args()
    initialize_desktop_app()
    root = tk.Tk()
    app = BatchPublisher(root)
    if args.resume:
        root.after(400, lambda: app.resume_batch(args.resume))
    elif args.inspect:
        root.after(400, lambda: app.resume_batch(args.inspect, auto_start=False))
    root.mainloop()
