"""YouTube preparation and batch publishing page."""
import asyncio
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from publisher_lock import AccountLock
from publisher_preferences import first_directory, write_local_json
from youtube_batch import AUDIENCE, CHANNEL_ID, RECEIPT_STATES, TEMPLATE, VISIBILITY, ReceiptBook, read_items, run_batch
from youtube_batch_studio import STUDIO, open_studio


class YouTubePanel(ttk.Frame):
    def __init__(self, parent, data_dir):
        super().__init__(parent, padding=(24, 14))
        self.data_dir = Path(data_dir)
        self.prefs = self.data_dir / "preferences.json"
        try:
            saved = json.loads(self.prefs.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self.channel = tk.StringVar(self, value=saved.get("channel", ""))
        self.folder = tk.StringVar(self, value=saved.get("folder", ""))
        self.table = tk.StringVar(self, value=saved.get("table", ""))
        self.sheet = tk.StringVar(self, value=saved.get("sheet", ""))
        self.visibility = tk.StringVar(self, value=VISIBILITY.get(saved.get("visibility"), VISIBILITY["private"]))
        self.audience = tk.StringVar(self, value=AUDIENCE.get(saved.get("audience"), ""))
        self.use_current_chrome = tk.BooleanVar(self, value=saved.get("use_current_chrome") is True)
        self.status = tk.StringVar(self, value="先登录频道，再选择视频文件夹和文案表。")
        self.items = []
        self.receipts = {}
        self.loaded = None
        self.busy = False
        self.stop = Event()
        self.events = Queue()
        self.controls = []
        self.build()
        self.poll_id = self.after(100, self.poll)
        self.bind("<Destroy>", self.on_destroy, add=True)
        if CHANNEL_ID.fullmatch(self.channel.get()) and Path(self.folder.get()).is_dir() and Path(self.table.get()).is_file():
            self.after_idle(self.load_table)

    def build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="YouTube 批量发布", style="Heading.TLabel").pack(side="left")
        ttk.Button(head, text="打开 YouTube Studio", command=lambda: webbrowser.open(STUDIO)).pack(side="right")
        browser_mode = ttk.Checkbutton(head, text="使用当前 Chrome", variable=self.use_current_chrome)
        browser_mode.pack(side="right", padx=(0, 8))
        self.controls.append(browser_mode)
        inputs = ttk.Frame(self)
        inputs.pack(fill="x")
        inputs.columnconfigure(1, weight=1)
        fields = (
            ("频道 ID", self.channel, "登录发布浏览器", self.login),
            ("视频文件夹", self.folder, "选择…", self.choose_folder),
            ("文案表格", self.table, "选择…", self.choose_table),
        )
        for row, (label, value, action, command) in enumerate(fields):
            ttk.Label(inputs, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            entry = ttk.Entry(inputs, textvariable=value)
            entry.grid(row=row, column=1, sticky="ew", pady=3)
            button = ttk.Button(inputs, text=action, command=command)
            button.grid(row=row, column=2, sticky="ew", padx=(8, 0))
            self.controls.extend((entry, button))

        options = ttk.Frame(self)
        options.pack(fill="x", pady=(9, 6))
        for label, variable, values, width in (
                ("工作表", self.sheet, (), 18),
                ("公开范围", self.visibility, tuple(VISIBILITY.values()), 13),
                ("儿童受众", self.audience, tuple(AUDIENCE.values()), 14)):
            ttk.Label(options, text=label).pack(side="left", padx=(0, 6))
            field = ttk.Combobox(options, textvariable=variable, values=values, width=width, state="readonly")
            field.pack(side="left", padx=(0, 12))
            self.controls.append(field)
            if variable is self.sheet:
                self.sheet_box = field
                field.bind("<<ComboboxSelected>>", lambda event: self.load_table())
        for label, command in (("读取表格", self.load_table), ("保存示例表", self.save_template)):
            button = ttk.Button(options, text=label, command=command)
            button.pack(side="left", padx=(0, 8))
            self.controls.append(button)
        ttk.Label(self, textvariable=self.status, wraplength=980, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(self)
        grid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(grid, show="headings", columns=("file", "title", "description", "status"), selectmode="browse")
        for key, name, width in (
                ("file", "视频文件", 135), ("title", "标题", 245), ("description", "简介", 415), ("status", "状态", 100)):
            self.tree.heading(key, text=name)
            self.tree.column(key, width=width, minwidth=65, stretch=key in ("title", "description"))
        scroll = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        self.detail = tk.Text(self, height=4, state="disabled", wrap="word", font=("Microsoft YaHei UI", 10))
        self.detail.pack(fill="x", pady=(8, 6))

        actions = ttk.Frame(self)
        actions.pack(fill="x")
        self.pause_button = ttk.Button(actions, text="暂停队列", command=self.pause, state="disabled")
        self.pause_button.pack(side="left")
        ttk.Button(actions, text="打开所选作品", command=self.open_selected).pack(side="left", padx=8)
        publish = ttk.Button(actions, text="开始批量发布", command=self.confirm_start, style="Publish.TButton")
        publish.pack(side="right")
        self.controls.append(publish)

    def on_destroy(self, event):
        if event.widget is self:
            self.after_cancel(self.poll_id)

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=first_directory(self.folder.get()))
        if folder:
            self.folder.set(folder)

    def choose_table(self):
        path = filedialog.askopenfilename(parent=self,
            initialdir=first_directory(Path(self.table.get()).parent if self.table.get() else None, self.folder.get()),
            filetypes=[("文案表格", "*.csv *.xlsx *.tsv")])
        if path:
            self.table.set(path)
            self.sheet.set("")

    def save_template(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
            initialfile="YouTube 示例文案表.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                Path(path).write_text(TEMPLATE, encoding="utf-8-sig")
                self.status.set("已保存示例表。简介中的 #话题保留，标签列用逗号分隔。")
            except OSError as exc:
                self.status.set(str(exc))

    def config(self):
        return tuple(value.get().strip() for value in (self.channel, self.folder, self.table, self.sheet))

    def save_preferences(self):
        channel, folder, table, sheet = self.config()
        visibility = next(key for key, value in VISIBILITY.items() if value == self.visibility.get())
        audience = next((key for key, value in AUDIENCE.items() if value == self.audience.get()), None)
        write_local_json(self.prefs, dict(channel=channel, folder=folder, table=table, sheet=sheet,
                                          visibility=visibility, audience=audience,
                                          use_current_chrome=self.use_current_chrome.get()))

    def work(self, action, target, *, pausable=False):
        self.busy = True
        self.stop.clear()
        for widget in self.controls:
            widget.state(["disabled"])
        self.pause_button.state(["!disabled"] if pausable else ["disabled"])

        def run():
            try:
                self.events.put(("done", action, target(), None))
            except Exception as exc:
                self.events.put(("done", action, None, str(exc)))

        Thread(target=run, daemon=True).start()

    def browser_action(self, operation, *, use_current_chrome):
        lock = AccountLock(self.data_dir / "locks", "youtube-chrome-profile").acquire()
        try:
            async def run():
                async with open_studio(self.data_dir / "chrome", use_current_chrome=use_current_chrome) as studio:
                    return await operation(studio)
            return asyncio.run(run())
        finally:
            lock.release()

    def login(self):
        expected = self.channel.get().strip()
        if expected and not CHANNEL_ID.fullmatch(expected):
            self.status.set("请填写有效的 YouTube 频道 ID，或留空后从浏览器读取。")
            return

        async def detect(studio):
            return await studio.detect_channel(self.stop, 600, expected)

        use_current_chrome = self.use_current_chrome.get()
        self.status.set("正在连接当前 Chrome，请查看浏览器授权提示。" if use_current_chrome else
                        "请在打开的 Chrome 登录 Google，并在 YouTube Studio 切换至要发布的频道。")
        self.work("login", lambda: self.browser_action(detect, use_current_chrome=use_current_chrome), pausable=True)

    def load_table(self):
        if self.busy:
            return
        channel, folder, table, sheet = self.config()
        if not CHANNEL_ID.fullmatch(channel):
            self.status.set("请先登录发布浏览器以读取频道 ID，或输入有效的 YouTube 频道 ID。")
            return
        if not folder or not table:
            self.status.set("请先选择视频文件夹和文案表。")
            return
        self.loaded = None
        self.items = []
        self.render()
        self.status.set("正在读取文案并核对视频…")

        def load():
            sheets, items = read_items(folder, table, sheet)
            return sheets, items, ReceiptBook(self.data_dir, channel).read()

        self.work("load", load)

    def pending(self):
        return [item for item in self.items if self.receipts.get(item["digest"], {}).get("status") != "confirmed"]

    def preview_state(self):
        if self.busy:
            return self.status.get()
        if not self.loaded or self.config() != self.loaded:
            return "请在 YouTube 页配置频道并读取文案表。"
        pending = self.pending()
        checking = sum(self.receipts.get(item["digest"], {}).get("status") == "checking" for item in pending)
        return f"已载入 {len(self.items)} 条 · 待发布 {len(pending) - checking} 条 · 待核对 {checking} 条"

    def prepare_batch(self):
        if self.busy:
            raise ValueError("YouTube 正在执行任务。")
        if not self.loaded or self.config() != self.loaded:
            raise ValueError("请在 YouTube 页重新读取当前频道的文案表。")
        items = self.pending()
        visibility = next(key for key, value in VISIBILITY.items() if value == self.visibility.get())
        audience = next((key for key, value in AUDIENCE.items() if value == self.audience.get()), None)
        if items and audience is None:
            raise ValueError("请在 YouTube 页选择儿童受众。")
        return self.loaded[0], items, visibility, audience

    def describe(self, plan):
        channel, items, visibility, audience = plan
        checking = sum(self.receipts.get(item["digest"], {}).get("status") == "checking" for item in items)
        return (f"YouTube\n频道：{channel}\n待上传：{len(items) - checking} 条，{VISIBILITY[visibility]}，"
                f"{AUDIENCE[audience]}\n待核对：{checking} 条，沿用原提交设置。")

    def confirm_start(self):
        try:
            plan = self.prepare_batch()
            if not plan[1]:
                self.status.set("当前队列没有待处理的视频。")
            elif messagebox.askyesno("确认 YouTube 批量发布", self.describe(plan), parent=self):
                self.start_prepared_batch(plan)
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.set(str(exc))

    def start_prepared_batch(self, plan):
        if self.busy:
            return False
        channel, items, visibility, audience = plan
        book = ReceiptBook(self.data_dir, channel)
        use_current_chrome = self.use_current_chrome.get()
        self.save_preferences()

        async def publish(studio):
            await studio.connect(channel, self.stop)
            return await run_batch(studio, book, items, visibility, audience, self.stop,
                                   lambda message: self.events.put(("progress", message)))

        self.status.set("正在核对发布浏览器的 YouTube 频道…")
        self.work("publish", lambda: self.browser_action(publish, use_current_chrome=use_current_chrome), pausable=True)
        return True

    def pause(self):
        self.stop.set()
        self.pause_button.state(["disabled"])
        self.status.set("已请求暂停，等待当前操作结束。")

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    if not self.stop.is_set():
                        self.status.set(event[1])
                    continue
                _, action, result, error = event
                self.finish(action, result, error)
        except Empty:
            pass
        self.poll_id = self.after(100, self.poll)

    def finish(self, action, result, error):
        self.busy = False
        for widget in self.controls:
            widget.state(["!disabled"])
        self.pause_button.state(["disabled"])
        try:
            if action == "publish":
                self.receipts = ReceiptBook(self.data_dir, self.loaded[0]).read()
                self.render()
            if error:
                self.status.set(error)
            elif action == "login":
                self.channel.set(result)
                self.save_preferences()
                self.status.set(f"已登录 YouTube Studio · {result}")
                if self.folder.get() and self.table.get():
                    self.load_table()
            elif action == "load":
                sheets, items, self.receipts = result
                self.sheet_box.configure(values=sheets)
                if items is None:
                    self.status.set("文案表有多个工作表，请选择要使用的一张。")
                else:
                    self.sheet.set(self.sheet.get() or sheets[0])
                    self.loaded = self.config()
                    self.items = items
                    self.save_preferences()
                    self.render()
                    self.status.set(self.preview_state())
            elif action == "publish":
                self.status.set(f"已核验 {result} 条 · " + ("队列已暂停。" if self.stop.is_set() else "本批已完成。"))
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def render(self):
        self.tree.delete(*self.tree.get_children())
        for item in self.items:
            receipt = self.receipts.get(item["digest"], {})
            self.tree.insert("", "end", iid=item["digest"], values=(
                Path(item["file"]).name, item["title"], item["desc"].replace("\n", " "),
                RECEIPT_STATES.get(receipt.get("status"), "待发布")))

    def show_detail(self, _=None):
        selected = self.tree.selection()
        item = next((row for row in self.items if selected and row["digest"] == selected[0]), None)
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if item:
            self.detail.insert("1.0", item["title"] + "\n" + item["desc"] + "\n标签  " + "、".join(item["tags"]))
        self.detail.configure(state="disabled")

    def open_selected(self):
        selection = self.tree.selection()
        video_id = self.receipts.get(selection[0], {}).get("video_id") if selection else None
        if video_id:
            webbrowser.open(f"{STUDIO}/video/{video_id}/edit")
        else:
            self.status.set("所选作品暂无已记录的 Studio 链接。")
