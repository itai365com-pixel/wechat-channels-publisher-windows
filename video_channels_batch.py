"""Batch desktop publisher using the installed SAU CLI and spreadsheet copy."""
from collections import OrderedDict
from datetime import datetime
import asyncio
import hashlib
import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from batch_table import description_with_tags
from batch_preview import read_batch_preview
from daily_publish_limit import BANGKOK, SUBMIT_PHASES
from channels_first_comments import FirstCommentQueue, FirstCommentWindow
from publisher_preferences import first_directory, load_batch_paths, remember_batch_paths
from video_channels_gui import Publisher, ROOT
from publisher_options import OptionsPanel, options_summary, planned_times, scrollable_panel, initialize_desktop_app, short_title_issues, normalize_short_title

DEFAULT_FOLDER = Path.home() / "Videos"
BATCH_DIR = ROOT / ".local" / "gui" / "batches"
LOG_DIR = ROOT / ".local" / "gui" / "logs"
PATH_PREFS = ROOT / ".local" / "gui" / "batch-paths.json"
FIRST_COMMENT_DIR = ROOT / ".local" / "gui" / "first-comments"


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


FINISHED_ITEMS = frozenset({'completed', 'skipped_completed'})


def video_digest(file):
    with Path(file).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def fingerprint(record, account, draft, *, digest=None):
    digest = digest or video_digest(record['file'])
    copy = {"short_title": record.get("short_title") or record.get("title", ""), "description": record.get("publish_desc", description_with_tags(record))}
    return hashlib.sha256(json.dumps([account, draft, digest, copy], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def verified_completion(record, row):
    return (row.get('status') == 'completed' and not record.get('requires_backend_audit') and
            ((row.get('platform_verification') or {}).get('verified') is True or
             row.get('phase') == 'confirmed_on_platform' or bool(record.get('collection_verification'))))


def publication_history(account, draft, *, directories=None, exclude=None):
    result = {}
    for directory in directories if directories is not None else (BATCH_DIR,):
        for path in directory.glob('*.json'):
            if path.resolve() == exclude:
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            if str(record.get('account', '')).strip().casefold() != account.strip().casefold() or bool(record.get('draft')) != draft:
                continue
            for row in record.get('items', []):
                status = row.get('status')
                if status not in ('completed', 'running', 'unknown') and row.get('phase') not in SUBMIT_PHASES:
                    continue
                if (status == 'unknown' and row.get('returncode') == 1 and row.get('phase') not in SUBMIT_PHASES
                        and not row.get('submit_clicked_at') and failed_before_submit(row.get('log'))):
                    continue
                evidence = {'manifest':str(path), 'confirmed':verified_completion(record, row),
                            'at':(row.get('platform_verification') or {}).get('verified_at') or row.get('finished_at')}
                keys = [('fingerprint', row.get('fingerprint')), ('video', row.get('video_sha256')),
                        ('path', str(Path(row['file']).resolve()).casefold())]
                for key in keys:
                    if key[1] and (key not in result or not evidence['confirmed']):
                        result[key] = evidence
    return result


def previous_completion(history, file, digest, key):
    exact = [history.get(('video', digest)), history.get(('fingerprint', key))]
    path_match = history.get(('path', str(Path(file).resolve()).casefold()))
    if any(match and not match['confirmed'] for match in [*exact, path_match]):
        raise ValueError(f'{Path(file).name} 有发布结果待核对，请先确认视频号后台记录。')
    confirmed = next((match for match in exact if match), None)
    if not confirmed and path_match:
        raise ValueError(f'{Path(file).name} 的路径已有发布记录，但无法确认是同一文件，请先核对。')
    return confirmed


def filter_previously_completed(items, account, draft):
    history = publication_history(account, draft)
    pending, skipped, seen = [], [], set()
    for item in items:
        digest = video_digest(item['file'])
        key = fingerprint(item, account, draft, digest=digest)
        old = previous_completion(history, item['file'], digest, key)
        if old:
            skipped.append({'file':item['file'], 'fingerprint':key, 'manifest':old['manifest']})
        elif digest in seen:
            raise ValueError(f'{Path(item["file"]).name} 与本批另一视频内容相同，请只选择一份。')
        else:
            seen.add(digest)
            pending.append({**item, 'fingerprint':key, 'video_sha256':digest})
    return pending, skipped


def last_publish_batch(account):
    latest = None
    for path in BATCH_DIR.glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('draft') or str(record.get('account', '')).strip().casefold() != account.strip().casefold():
            continue
        for row in record.get('items', []):
            if row.get('status') not in ('completed', 'unknown', 'running', 'failed_before_submit'):
                continue
            value = row.get('finished_at') or (row.get('platform_verification') or {}).get('verified_at') or row.get('started_at') or record.get('created_at')
            if not value:
                continue
            stamp = datetime.fromisoformat(value)
            stamp = stamp.astimezone(BANGKOK)
            if latest is None or stamp > latest[0]:
                latest = (stamp, path, record)
    return latest


class BatchPublisher(Publisher):
    def __init__(self, root, *, auto_preview=True):
        self.path_prefs = load_batch_paths(PATH_PREFS)
        self.folder = tk.StringVar(root, value=self.path_prefs.get("folder", str(DEFAULT_FOLDER)))
        self.table_path = tk.StringVar(root, value=self.path_prefs.get("table", ""))
        self.sheet = tk.StringVar(root, value=self.path_prefs.get("sheet", ""))
        self.first_comment_window = None
        self.start_number = tk.StringVar(root, value="")
        self.count = tk.StringVar(root, value="")
        self.series = tk.StringVar(root, value="自动识别")
        self.filenames = tk.StringVar(root, value="")
        self.summary = tk.StringVar(root, value="请先读取文案表，预览本次要发布的视频。")
        self.stop_requested = threading.Event()
        self.items = []
        self.batch_results = None
        self.preview_config = None
        self.preview_generation = 0
        self.preview_loading = False
        self.preview_running = False
        self.preview_request = None
        self.preview_cancel = None
        self.preview_checks = OrderedDict()
        self.last_preview_context = None
        self.last_preview_selection = None
        super().__init__(root)
        root.title("视频号批量发布助手 v1.12.19")
        root.geometry(f"{min(1420, root.winfo_screenwidth() - 100)}x{min(960, root.winfo_screenheight() - 100)}")
        root.minsize(1060, 850)
        if auto_preview:
            root.after(300, self.preview)

    def make_ui(self):
        self.configure_style()
        self.platform = tk.StringVar(self.root, value="wechat")
        switch = ttk.Frame(self.root, padding=(26, 8, 26, 8))
        switch.pack(fill="x")
        style = ttk.Style(self.root)
        style.configure("Platform.Toolbutton", padding=(13, 2), font=("Microsoft YaHei UI", 10))
        style.map("Platform.Toolbutton", background=[("selected", "#d6e9df"), ("active", "#e8f2ed")])
        for value, label in (("wechat", "视频号"), ("tiktok", "TikTok")):
            ttk.Radiobutton(switch, text=label, value=value, variable=self.platform,
                            style="Platform.Toolbutton", command=self.switch_platform).pack(side="left", padx=(0, 6))
        self.platform_pages = ttk.Frame(self.root)
        self.platform_pages.pack(fill="both", expand=True)
        self.wechat_page = ttk.Frame(self.platform_pages)
        self.wechat_page.pack(fill="both", expand=True)
        self.tiktok_page = None
        self.tiktok_panel = None
        panel = scrollable_panel(self.wechat_page, (26, 12))
        panel.columnconfigure(1, weight=1)
        panel.rowconfigure(14, weight=1)
        heading = ttk.Frame(panel)
        heading.grid(row=0, column=0, columnspan=3, sticky='ew', pady=(0, 10))
        heading.columnconfigure(1, weight=1)
        ttk.Label(heading, text="视频号批量发布", style="Heading.TLabel").grid(row=0, column=0, sticky='w')
        ttk.Label(heading, text="最多 2 个标签 · 每轮最多 6 条 · 等 1 小时自动续发", style="Muted.TLabel").grid(row=0, column=1, sticky='w', padx=(14, 8))
        ttk.Button(heading, text='词映官网', command=self.open_wordmotion).grid(row=0, column=2, sticky='e')
        accounts = ttk.Frame(panel)
        accounts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 5))
        ttk.Label(accounts, text="视频号账号", style="Field.TLabel").pack(side="left", padx=(0, 12))
        self.account_box = ttk.Combobox(accounts, textvariable=self.account_display, state="readonly", width=20)
        self.account_box.pack(side="left")
        self.account_box.bind("<<ComboboxSelected>>", self.select_account)
        for text, callback in (("检查登录", self.check_login), ("扫码登录", self.login)):
            button = ttk.Button(accounts, text=text, command=callback)
            button.pack(side="left", padx=(10, 0))
            self.controls.append(button)
        stats = ttk.Button(accounts, text='近7天Top10与分析', command=self.show_statistics)
        stats.pack(side='left', padx=(10,0))
        self.controls.append(stats)
        ttk.Label(accounts, textvariable=self.quota_status, style='Muted.TLabel').pack(side='left', padx=(18, 0))
        ttk.Label(panel, textvariable=self.login_status, style="Muted.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 12))

        paths = ttk.Frame(panel)
        paths.grid(row=4, column=0, columnspan=3, sticky='ew', pady=4)
        for index, (label, variable, callback) in enumerate((("视频文件夹", self.folder, self.choose_folder), ("文案表格", self.table_path, self.choose_table))):
            paths.columnconfigure(index, weight=1, uniform='paths')
            field = ttk.Frame(paths)
            field.grid(row=0, column=index, sticky='ew', padx=(0, 16) if index == 0 else 0)
            field.columnconfigure(1, weight=1)
            ttk.Label(field, text=label, style='Field.TLabel').grid(row=0, column=0, padx=(0, 8))
            entry = ttk.Entry(field, textvariable=variable, width=1)
            entry.grid(row=0, column=1, sticky='ew')
            button = ttk.Button(field, text='选择…', command=callback)
            button.grid(row=0, column=2, padx=(8, 0))
            self.controls.extend([entry, button])
        ttk.Style(self.root).configure('Preview.TButton', padding=(12, 3))
        preview = ttk.Button(paths, text="读取表格 / 更新预览", command=self.preview, style='Preview.TButton')
        preview.grid(row=0, column=2, padx=(10, 0), sticky='e')
        self.controls.append(preview)
        selection = ttk.Frame(panel)
        selection.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        selection.columnconfigure(2, weight=1)
        options = ttk.Frame(selection)
        options.grid(row=0, column=0, sticky="w")
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
        self.controls.extend([self.sheet_box, self.series_box, start, count])
        ttk.Label(selection, text="指定文件（可选）").grid(row=0, column=1, padx=(14, 8))
        names_entry = ttk.Entry(selection, textvariable=self.filenames, width=1)
        names_entry.grid(row=0, column=2, sticky="ew")
        self.controls.append(names_entry)
        # Keep the hint separate from the variable used for file matching.
        names_hint = ttk.Label(names_entry, text="如 ep001-ep010、1-10 或文件名列表（优先匹配）",
                               foreground='#6b7774', background='white', cursor='xterm',
                               font=('Microsoft YaHei UI', 9), anchor='w')

        def update_names_hint(*_):
            if self.filenames.get():
                names_hint.place_forget()
            else:
                names_hint.place(x=9, rely=0.5, relwidth=1, width=-18, anchor='w')

        names_hint.bind('<Button-1>', lambda _: names_entry.focus_set())
        self.filenames.trace_add('write', update_names_hint)
        update_names_hint()
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
        self.stop_button = ttk.Button(actions, text="暂停队列", state="disabled", command=self.request_stop)
        self.stop_button.pack(side="left")
        edit = ttk.Button(actions, text="编辑本条文案", command=self.edit_item)
        edit.pack(side="left", padx=(10, 0))
        self.controls.append(edit)
        resume = ttk.Button(actions, text="继续已有队列", command=self.choose_resume)
        resume.pack(side="left", padx=(10, 0))
        self.controls.append(resume)
        history = ttk.Button(actions, text="上次发布记录", command=self.show_last_publish)
        history.pack(side="left", padx=(10, 0))
        self.controls.append(history)
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

    def switch_platform(self):
        if self.platform.get() == "tiktok":
            if self.tiktok_page is None:
                from tiktok_gui import TikTokPanel

                self.tiktok_page = ttk.Frame(self.platform_pages)
                self.tiktok_panel = TikTokPanel(self.tiktok_page)
            self.wechat_page.pack_forget()
            self.tiktok_page.pack(fill="both", expand=True)
        else:
            if self.tiktok_page is not None:
                self.tiktok_page.pack_forget()
            self.wechat_page.pack(fill="both", expand=True)

    def remember_paths(self):
        try:
            self.path_prefs = remember_batch_paths(PATH_PREFS, self.folder.get().strip(), self.table_path.get().strip(), self.sheet.get())
        except OSError as exc:
            self.append_log(f"无法保存上次使用的路径：{exc}\n")

    def choose_folder(self):
        start = first_directory(self.folder.get().strip(), self.path_prefs.get("folder"), DEFAULT_FOLDER)
        selected = filedialog.askdirectory(parent=self.root, title="选择待发布视频文件夹", initialdir=start)
        if selected:
            self.folder.set(selected)
            self.reset_selection()
            self.series.set('自动识别')
            self.table_path.set('')
            self.sheet.set('')
            self.remember_paths()
            self.preview()

    def choose_table(self):
        current = self.table_path.get().strip()
        start = first_directory(Path(current).parent if current and Path(current).is_absolute() else None,
                                self.path_prefs.get("table_dir"), self.folder.get().strip(), DEFAULT_FOLDER)
        selected = filedialog.askopenfilename(parent=self.root, title="选择发布文案表格", initialdir=start, filetypes=[("文案表格", "*.xlsx *.csv *.tsv"), ("所有文件", "*.*")])
        if selected:
            self.table_path.set(selected)
            self.sheet.set("")
            self.series.set('自动识别')
            self.reset_selection()
            self.remember_paths()
            self.preview()

    def reset_selection(self):
        self.start_number.set('')
        self.count.set('')
        self.filenames.set('')

    def change_series(self, _event=None):
        self.reset_selection()
        self.preview()

    def cancel_preview(self):
        self.preview_generation += 1
        self.preview_request = None
        self.preview_loading = False
        if self.preview_cancel is not None:
            self.preview_cancel.set()

    def preview(self):
        if self.busy:
            return
        self.cancel_preview()
        self.preview_loading = True
        self.preview_config = None
        self.items = []
        self.tree.delete(*self.tree.get_children())
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        self.summary.set("正在读取文案和检查视频…")
        self.status.set("正在更新预览")
        self.preview_request = (self.preview_generation, self.current_config(),
                                self.last_preview_context, self.last_preview_selection)
        self.start_preview_worker()

    def start_preview_worker(self):
        if self.preview_running or self.preview_request is None:
            return
        generation, config, context, selection = self.preview_request
        self.preview_request = None
        self.preview_running = True
        self.preview_cancel = cancelled = threading.Event()

        def load():
            result = read_batch_preview(config, context, selection, self.preview_checks, cancelled)
            self.events.put(("preview_done", (generation, config, result)))

        threading.Thread(target=load, daemon=True).start()

    def finish_preview(self, generation, config, result):
        self.preview_running = False
        self.preview_cancel = None
        if generation == self.preview_generation:
            self.preview_loading = False
            if config == self.current_config():
                self.apply_preview(result)
            else:
                self.summary.set("设置已变化，请重新读取表格 / 更新预览。")
                if not self.busy:
                    self.status.set("等待更新预览")
        self.start_preview_worker()

    def apply_preview(self, result):
        if result.table != self.table_path.get():
            self.table_path.set(result.table)
        if result.sheets:
            self.sheet_box["values"] = result.sheets
            self.sheet.set(result.sheet)
            self.remember_paths()
        if result.series:
            self.series_box["values"] = result.series
        if result.selection != (self.start_number.get(), self.count.get(), self.filenames.get()):
            self.start_number.set(result.selection[0])
            self.count.set(result.selection[1])
            self.filenames.set(result.selection[2])
        if result.error:
            self.summary.set(f"无法准备队列：{result.error}")
            if not self.busy:
                self.status.set("请检查表格和视频文件夹")
            self.append_log(f"读取失败：{result.error}\n")
            return

        self.items = result.items
        for index, item in enumerate(self.items):
            values = (index + 1, Path(item["file"]).name, item["short_title"] or item["title"],
                      item.get("publish_desc", item["desc"]), item["schedule"] or "立即发表", item["status"])
            invalid = item["decode_error"] or item["status"] == "短标题需修改"
            self.tree.insert("", "end", iid=str(index), values=values, tags=("invalid",) if invalid else ())
        if result.renamed_titles:
            self.append_log("短标题中的逗号已替换为空格：" + "、".join(result.renamed_titles[:10]) + "。\n")
        order = "按指定文件顺序" if result.selection[2].strip() else "按文件名自然排序"
        prefix = result.context[-1]
        series_label = (prefix or "纯数字") if prefix is not None else "按明确文件名"
        bad = [row for row in self.items if row["decode_error"]]
        extra = f"；{len(bad)} 条视频无法解码，已标红。" if bad else "。"
        self.summary.set(f"识别系列：{series_label}；文件夹 {result.video_count} 个视频，表格匹配 {result.matched_count} 条；本次预览 {len(self.items)} 条（{order}）{extra}")
        if not self.busy:
            self.status.set(f"已读取 {len(self.items)} 条 · {len(bad)} 条视频需检查" if bad else f"已准备 {len(self.items)} 条 · 请核对标题和简介")
        if bad:
            self.append_log("视频检查失败：" + "、".join(Path(row["file"]).name for row in bad[:10]) + "。请替换视频后更新预览，或使用“指定文件”排除这些条目。\n")
        self.tree.selection_set("0")
        self.show_detail()
        self.preview_config = self.current_config()
        self.table_signature = result.table_signature
        self.last_preview_context = result.context
        self.last_preview_selection = result.selection

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
            value = normalize_short_title(title.get())
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
        if self.busy or self.preview_loading:
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
            question = f"账号：{self.account_display.get()}\n数量：{len(jobs)} 条\n视频：{names}\n文案来源：{Path(self.table_path.get()).name} / {self.sheet.get()}\n\n"
            question += options_summary(settings, schedules) + "\n\n"
            if skipped:
                question += f'已自动跳过 {len(skipped)} 条后台核验完成的视频。\n'
            question += "确认后将逐条上传并保存草稿。" if draft else "确认后将逐条上传并提交定时发表。" if schedules[0] else "确认后将逐条上传并立即发表到视频号。"
            question += "\n最多同时打开 2 个标签上传，按封面就绪顺序逐条提交。"
            if not draft:
                question += '\n每轮最多 6 条，完成后至少等 1 小时。满每日额度后在曼谷次日自动继续。请保持程序运行。'
                if schedules[0]:
                    question += '\n各条平台定时时间按预览保留，等待后如已来不及提交，队列会暂停。'
            if not messagebox.askyesno(f"确认{action}", question, parent=self.root):
                return
            BATCH_DIR.mkdir(parents=True, exist_ok=True)
            self.manifest_path = BATCH_DIR / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"
            self.manifest = {"account": self.account.get(), "draft": draft, "options": settings, "source": self.table_path.get(), "sheet": self.sheet.get(), "created_at": datetime.now().astimezone().isoformat(), "items": jobs, "parallel_tabs": 2}
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

    def show_first_comments(self):
        try:
            queue = FirstCommentQueue(FIRST_COMMENT_DIR, BATCH_DIR, self.account.get())
            active = self.first_comment_window
            if active and active.window.winfo_exists():
                if active.queue.account == queue.account:
                    active.refresh()
                    active.window.lift()
                    return
                active.window.destroy()
            self.first_comment_window = FirstCommentWindow(self.root, queue, self.path_prefs.get("table_dir"))
        except (OSError, ValueError) as exc:
            messagebox.showerror('无法打开首评队列', str(exc), parent=self.root)

    def show_last_publish(self):
        try:
            latest = last_publish_batch(self.account.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror('无法读取发布记录', str(exc), parent=self.root)
            return
        if latest is None:
            messagebox.showinfo('上次发布记录', '当前账号还没有批量发布记录。', parent=self.root)
            return

        stamp, path, record = latest
        window = tk.Toplevel(self.root)
        window.title('上次发布记录')
        window.transient(self.root)
        window.geometry('920x480')
        panel = ttk.Frame(window, padding=12)
        panel.pack(fill='both', expand=True)
        rows = record['items']
        verified = sum(verified_completion(record, row) for row in rows)
        skipped = sum(row.get('status') == 'skipped_completed' for row in rows)
        ttk.Label(panel, text=f'{self.account_display.get()} · {stamp:%Y-%m-%d %H:%M}（曼谷） · 本批核验 {verified} 条 · 已发布跳过 {skipped} 条 · 共 {len(rows)} 条').pack(anchor='w')
        ttk.Label(panel, text=path.name, style='Muted.TLabel').pack(anchor='w', pady=(3, 8))
        frame = ttk.Frame(panel)
        frame.pack(fill='both', expand=True)
        table = ttk.Treeview(frame, columns=('file', 'title', 'status', 'time'), show='headings')
        for key, label, width in [('file','视频文件',150), ('title','短标题',280), ('status','发布状态',165), ('time','记录时间（曼谷）',170)]:
            table.heading(key, text=label)
            table.column(key, width=width)
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        table.pack(side='left', fill='both', expand=True)
        labels = {'skipped_completed':'已发布，已跳过', 'completed':'待核对', 'pending':'待发布',
                  'running':'处理中 / 待核对', 'unknown':'结果待核对', 'failed_before_submit':'提交前失败'}
        for row in rows:
            proof = row.get('platform_verification') or {}
            earlier = row.get('previous_completion') or {}
            value = proof.get('verified_at') or row.get('finished_at') or row.get('started_at') or earlier.get('at')
            when = datetime.fromisoformat(value).astimezone(BANGKOK).strftime('%Y-%m-%d %H:%M') if value else ''
            status = ('定时发表已核验' if row.get('schedule') else '发表已核验') if verified_completion(record, row) else labels.get(row.get('status'), '待核对')
            table.insert('', 'end', values=(Path(row['file']).name, row.get('short_title') or row.get('title', ''), status, when))
        ttk.Button(panel, text='关闭', command=window.destroy).pack(anchor='e', pady=(8, 0))

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
                raise ValueError('这份队列没有可继续的条目，视频已完成或已有后台核验记录。')
            if confirm:
                completed = sum(row.get('status') in FINISHED_ITEMS for row in record['items'])
                display_name = self.account_names.get(record["account"], record["account"])
                question = f'账号：{display_name}\n已完成 {completed} 条，将跳过。\n继续未完成 {len(jobs)} 条。\n\n' + options_summary(record['options'])
                question += '\n\n最多使用 2 个标签上传，按额度和队列中保存的时间继续。确认继续？'
                if not messagebox.askyesno('继续已有队列', question, parent=self.root):
                    return
            self.cancel_preview()
            self.preview_config = None
            self.manifest_path, self.manifest = path, record
            record['parallel_tabs'] = 2
            self.account.set(record['account'])
            self.refresh_account_status()
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
                label = {'completed':'已发布', 'skipped_completed':'已发布，已跳过', 'failed_before_submit':'待重试'}.get(row.get('status'), '待发布')
                self.tree.insert('', 'end', iid=str(index), values=(index + 1, Path(row['file']).name,
                    row.get('short_title') or row['title'], row.get('publish_desc', row.get('desc', '')),
                    row.get('schedule') or '立即发表', label))
            self.summary.set(f'继续队列：{path.name} · 待处理 {len(jobs)} 条 · 最多 2 个标签并行')
            if not auto_start:
                completed = sum(row.get('status') in FINISHED_ITEMS for row in record['items'])
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
        return self.run_parallel(action)

    def request_stop(self):
        self.stop_requested.set()
        self.stop_button.configure(state="disabled")
        self.status.set("已请求停止 · 当前最多 2 条结束后暂停")

    def run_parallel(self, action):
        from channels_parallel import ParallelPublisher
        code = 1
        try:
            runner = ParallelPublisher(self.manifest_path, record=self.manifest,
                                       event=lambda kind, payload: self.events.put((kind, payload)),
                                       stop=self.stop_requested, tabs=self.manifest.get('parallel_tabs', 2))
            code = asyncio.run(runner.run(acquire_lock=False))
        except Exception as exc:
            self.events.put(('log', f'并行批次停止：{exc}\n'))
        finally:
            completed = sum(row.get('status') in FINISHED_ITEMS for row in self.manifest['items'])
            self.batch_results = f'本批完成 {completed}/{len(self.manifest["items"])} 条'
            if self.manifest.get('status') == 'paused':
                self.batch_results += ' · 队列已暂停'
            failed = [row for row in self.manifest['items'] if row.get('status') == 'failed_before_submit']
            uncertain = [row for row in self.manifest['items'] if row.get('status') == 'unknown']
            if uncertain:
                self.batch_results += f' · {len(uncertain)} 条发表结果待核对'
            elif failed:
                cause = next((row for row in failed if '安全停止' not in row.get('error', '')), failed[0])
                self.batch_results += f' · {Path(cause["file"]).name} 提交前失败：{cause.get("error", "请检查日志")}'
            self.events.put(('done', (code, None, action)))

    def on_event(self, kind, payload):
        if kind == "preview_done":
            self.finish_preview(*payload)
            return
        if kind == 'batch_wait' and not self.stop_requested.is_set():
            self.status.set(payload)
        if kind == "batch_row":
            index, label, status = payload
            self.tree.set(str(index), "status", label)
            self.status.set(status)
            view = self.first_comment_window
            if view and view.window.winfo_exists() and '后台' in label:
                view.refresh()

    def task_finished(self, code, action):
        super().task_finished(code, action)
        self.sheet_box.configure(state="readonly")
        self.series_box.configure(state='readonly')
        if action in {"批量发布", "批量保存草稿", "批量定时发表"}:
            self.stop_button.configure(state="disabled")
            self.sheet_box.configure(state="readonly")
            self.status.set((self.batch_results or "批次已结束") + (" · 请检查日志" if code else ""))

    def close(self):
        if self.tiktok_panel is not None and self.tiktok_panel.busy:
            self.tiktok_panel.pause()
            messagebox.showinfo("TikTok 正在暂停", "请等当前操作返回，再关闭发布助手。", parent=self.root)
            return
        if not self.busy:
            self.cancel_preview()
        super().close()

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
    app = BatchPublisher(root, auto_preview=not (args.resume or args.inspect))
    if args.resume:
        root.after(400, lambda: app.resume_batch(args.resume))
    elif args.inspect:
        root.after(400, lambda: app.resume_batch(args.inspect, auto_start=False))
    root.mainloop()
