"""Windows desktop queue for preparing TikTok Studio submissions."""
from datetime import datetime
from pathlib import Path
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from publisher_options import TIME_FORMAT
from publisher_preferences import first_directory, write_local_json
from tiktok_batch import run_batch
from tiktok_queue import DAILY_LIMIT, HOURLY_LIMIT, TikTokQueue, account_key, file_signature, read_tiktok_items, write_template
from tiktok_studio import STUDIO, open_studio

ROOT = Path(__file__).resolve().parent
STATUS = {"pending": "待处理", "checking": "待核对", "scheduled": "已排期", "published": "已发布"}


class TikTokPanel:
    def __init__(self, window, data_dir=None):
        self.window = window
        self.data_dir = Path(data_dir) if data_dir else ROOT / ".local/gui/tiktok"
        self.prefs = self.data_dir / "preferences.json"
        self.account = tk.StringVar(window)
        self.folder = tk.StringVar(window)
        self.table = tk.StringVar(window)
        self.all_records = tk.BooleanVar(window)
        self.batch_count = tk.StringVar(window)
        self.stop_requested = threading.Event()
        self.status = tk.StringVar(window, value="填写 TikTok 用户名并读取文案表。")
        self.events = queue.Queue()
        self.items = {}
        self.active = []
        self.store = None
        self.busy = False
        self.build()
        try:
            self.account.set(json.loads(self.prefs.read_text(encoding="utf-8")).get("account", ""))
        except (OSError, ValueError, AttributeError):
            pass
        if self.account.get():
            self.load_account()
        self.poll_id = window.after(100, self.poll)
        window.bind("<Destroy>", self.stop_polling, add=True)

    def stop_polling(self, event):
        if event.widget is self.window:
            self.window.after_cancel(self.poll_id)

    def build(self):
        panel = ttk.Frame(self.window, padding=16)
        panel.pack(fill="both", expand=True)
        top = ttk.Frame(panel)
        top.pack(fill="x")
        ttk.Label(top, text="TikTok 发布助手", font=("Microsoft YaHei UI", 19, "bold")).pack(side="left")
        ttk.Button(top, text="打开 TikTok Studio", command=self.open_studio).pack(side="right")
        self.login_button = ttk.Button(top, text="登录发布浏览器", command=self.login)
        self.login_button.pack(side="right", padx=(0, 8))
        ttk.Label(panel, text=f"每账号最近 60 分钟最多 {HOURLY_LIMIT} 条，曼谷自然日最多 {DAILY_LIMIT} 条。").pack(anchor="w", pady=(4, 8))
        inputs = ttk.Frame(panel)
        inputs.pack(fill="x")
        inputs.columnconfigure(1, weight=1)
        self.inputs = []
        fields = (
            ("TikTok @用户名", self.account, "加载账号队列", self.load_account),
            ("视频文件夹", self.folder, "选择…", self.choose_folder),
            ("泰文文案表", self.table, "选择…", self.choose_table),
        )
        for index, (label, variable, action, callback) in enumerate(fields):
            ttk.Label(inputs, text=label).grid(row=index, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(inputs, textvariable=variable)
            entry.grid(row=index, column=1, sticky="ew", pady=3)
            button = ttk.Button(inputs, text=action, command=callback)
            button.grid(row=index, column=2, sticky="ew", padx=(8, 0), pady=3)
            self.inputs.extend((entry, button))
        bar = ttk.Frame(panel)
        bar.pack(fill="x", pady=(8, 4))
        self.read_button = ttk.Button(bar, text="读取表格 / 保存队列", command=self.import_table)
        self.read_button.pack(side="left")
        ttk.Button(bar, text="保存示例表", command=self.save_template).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(bar, text="查看全部记录", variable=self.all_records, command=self.render).pack(side="right")
        batch = ttk.Frame(panel)
        batch.pack(fill="x", pady=(3, 5))
        ttk.Label(batch, text="本批条数（留空＝当前队列全部）").pack(side="left")
        self.count_entry = ttk.Entry(batch, width=6, textvariable=self.batch_count)
        self.count_entry.pack(side="left", padx=(5, 9))
        self.batch_button = ttk.Button(batch, text="一键批量发布", command=self.start_batch)
        self.batch_button.pack(side="left")
        self.pause_button = ttk.Button(batch, text="暂停", command=self.pause, state="disabled")
        self.pause_button.pack(side="left", padx=(6, 10))
        ttk.Label(batch, text="公开发布 · 到表格时间再提交 · 请保持助手运行").pack(side="left")
        ttk.Label(panel, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(0, 7))
        grid = ttk.Frame(panel)
        grid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(grid, columns=("file", "caption", "time", "status"),
                                 show="headings", selectmode="browse")
        for key, label, width in (("file", "视频文件", 140), ("caption", "泰文文案和话题", 420),
                                  ("time", "曼谷发布时间", 150), ("status", "状态", 95)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=75, stretch=key == "caption")
        scroll = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_row)
        self.detail = tk.Text(panel, height=4, wrap="word", state="disabled", relief="flat",
                              font=("Leelawadee UI", 10), padx=8, pady=5)
        self.detail.pack(fill="x", pady=(8, 4))
        actions = ttk.Frame(panel)
        actions.pack(fill="x")
        self.row_buttons = {}
        for key, label, callback in (
            ("begin", "开始处理 / 复制路径", self.begin),
            ("path", "复制视频路径", lambda: self.copy_selected("file")),
            ("caption", "复制泰文文案", lambda: self.copy_selected("caption")),
            ("scheduled", "登记已排期", lambda: self.confirm("scheduled")),
            ("published", "登记已发布", lambda: self.confirm("published")),
            ("pending", "确认未提交", lambda: self.confirm("pending")),
        ):
            button = ttk.Button(actions, text=label, command=callback, state="disabled")
            button.pack(side="left", padx=(0, 6))
            self.row_buttons[key] = button

    def choose_folder(self):
        selected = filedialog.askdirectory(parent=self.window, initialdir=first_directory(self.folder.get()))
        if selected:
            self.folder.set(selected)

    def choose_table(self):
        previous = Path(self.table.get()).parent if self.table.get() else None
        selected = filedialog.askopenfilename(parent=self.window, initialdir=first_directory(previous, self.folder.get()),
                                              filetypes=[("文案表格", "*.xlsx *.csv *.tsv")])
        if selected:
            self.table.set(selected)

    def save_template(self):
        path = filedialog.asksaveasfilename(parent=self.window, defaultextension=".csv",
                                            initialfile="TikTok示例文案表.csv", filetypes=[("CSV", "*.csv")])
        if path:
            try:
                write_template(path)
                self.status.set("示例表已保存。排期列请设为文本，格式 YYYY-MM-DD HH:MM，或留空。")
            except OSError as exc:
                self.status.set(str(exc))

    def load_account(self):
        if self.busy:
            return
        try:
            store = TikTokQueue(self.data_dir, self.account.get())
            state = store.read()
            self.store = store
            self.folder.set(state.get("folder", ""))
            self.table.set(state.get("table", ""))
            self.load_state(state)
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

    def load_state(self, state):
        self.items = state["items"]
        self.active = state["active"]
        self.render()
        write_local_json(self.prefs, {"account": self.store.username})
        self.status.set(f"@{self.store.username} · 当前队列 {len(self.active)} 条。排期留空时按开始处理的时间占用额度。")

    def import_table(self):
        if self.busy:
            return
        try:
            store = TikTokQueue(self.data_dir, self.account.get())
            folder, table = self.folder.get().strip(), self.table.get().strip()
            if not folder or not table:
                raise ValueError("请先选择视频文件夹和文案表。")
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.set_busy(True, can_pause=False)
        self.status.set("正在读取泰文文案和检查视频…")

        def load():
            try:
                with store.batch_lock():
                    sheet, items = read_tiktok_items(folder, table)
                    state = store.import_items(items, folder=folder, table=table, sheet=sheet)
                self.events.put(("done", store, state, None))
            except Exception as exc:
                self.events.put(("done", store, None, str(exc)))

        threading.Thread(target=load, daemon=True).start()

    def set_busy(self, busy, *, can_pause=False):
        self.busy = busy
        for control in [*self.inputs, self.read_button, self.login_button, self.count_entry, self.batch_button]:
            control.configure(state="disabled" if busy else "normal")
        self.pause_button.configure(state="normal" if can_pause else "disabled")
        self.show_row()

    def pause(self):
        self.stop_requested.set()
        self.pause_button.configure(state="disabled")
        self.status.set("正在暂停，请等待当前操作返回。")

    def login(self):
        if self.busy:
            return
        try:
            store = TikTokQueue(self.data_dir, self.account.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.start_studio(store, selected=None)

    def start_batch(self):
        if self.busy:
            return
        try:
            self.check_account()
            state = self.store.read()
            selected = [key for key in state["active"]
                        if state["items"][key]["status"] in ("pending", "checking") and not state["items"][key]["error"]]
            count = self.batch_count.get().strip()
            if count and (not count.isdecimal() or int(count) < 1):
                raise ValueError("本批条数请填正整数，或留空使用当前队列。")
            if count:
                selected = selected[:int(count)]
            if not selected:
                raise ValueError("当前队列没有待发布或待核对的视频。")
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))
            return
        self.start_studio(self.store, selected)

    def start_studio(self, store, selected):
        self.stop_requested.clear()
        self.set_busy(True, can_pause=True)
        self.status.set("正在打开 TikTok 发布浏览器…")

        def progress(message, *, refresh=False):
            self.events.put(("progress", message, store.read() if refresh else None))

        def work():
            try:
                from playwright.sync_api import Error as BrowserError, TimeoutError
            except ImportError:
                self.events.put(("done", store, None, "缺少 TikTok 发布组件，请安装新版 Windows 安装包。"))
                return

            profile = self.data_dir / "profiles" / store.path.stem
            try:
                with store.batch_lock(), open_studio(profile, store.username) as studio:
                    if not studio.login(self.stop_requested, progress, wait_seconds=180):
                        message = "已暂停。" if self.stop_requested.is_set() else "尚未完成登录，请点击「登录发布浏览器」重试。"
                    elif selected is None:
                        message = f"@{store.username} 已登录发布浏览器。"
                    else:
                        message = run_batch(store, selected, studio, self.stop_requested, progress)
            except TimeoutError:
                message = "等待 TikTok Studio 超时。请检查网络、浏览器验证和队列中的待核对条目。"
            except BrowserError:
                message = "Chrome 窗口已关闭或 Studio 操作未完成。请检查浏览器及待核对条目。"
            except Exception as exc:
                message = str(exc)
            try:
                state = store.read()
            except (OSError, ValueError) as exc:
                message += f" 无法刷新队列：{exc}"
                state = None
            self.events.put(("done", store, state, message))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                if event[2] is not None:
                    self.items, self.active = event[2]["items"], event[2]["active"]
                    self.render()
                if not self.stop_requested.is_set():
                    self.status.set(event[1])
                continue
            _, store, state, message = event
            self.set_busy(False)
            if state is not None:
                self.store = store
                try:
                    self.folder.set(state.get("folder", ""))
                    self.table.set(state.get("table", ""))
                    self.load_state(state)
                except OSError as exc:
                    message = f"{message or ''} 无法保存上次账号：{exc}".strip()
            if message:
                self.status.set(message)
        self.poll_id = self.window.after(100, self.poll)

    def render(self):
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        for digest in self.items if self.all_records.get() else self.active:
            row = self.items[digest]
            stamp = datetime.fromisoformat(row["scheduled_at"]).strftime(TIME_FORMAT) if row["scheduled_at"] else "立即"
            self.tree.insert("", "end", iid=digest, values=(Path(row["file"]).name, row["caption"].replace("\n", " "),
                             stamp, "视频检查失败" if row["error"] else STATUS[row["status"]]))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        self.show_row()

    def selected(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def show_row(self, _event=None):
        row = self.items.get(self.selected())
        enabled = set()
        if row and not self.busy:
            if row["status"] == "pending" and not row["error"]:
                enabled.update(("begin", "caption"))
            elif row["status"] == "checking":
                enabled.update(("path", "caption", "published", "pending"))
                if row["scheduled_at"]:
                    enabled.add("scheduled")
            elif row["status"] == "scheduled":
                enabled.add("published")
        for key, button in self.row_buttons.items():
            button.configure(state="normal" if key in enabled else "disabled")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if row:
            self.detail.insert("1.0", row["caption"] + ("\n" + row["error"] if row["error"] else ""))
        self.detail.configure(state="disabled")

    def check_account(self):
        if self.store is None or account_key(self.account.get()) != self.store.username:
            raise ValueError("用户名已改变，请先加载对应账号的队列。")

    def copy(self, text):
        self.window.clipboard_clear()
        self.window.clipboard_append(text)

    def copy_selected(self, field):
        try:
            self.check_account()
            row = self.store.read()["items"][self.selected()]
            if row["status"] not in ("pending", "checking"):
                raise ValueError("已有排期或发布记录，请刷新队列。")
            if field == "file" and (row["status"] != "checking" or file_signature(row["file"]) != row["signature"]):
                raise ValueError("视频或处理状态已变化，请先核对队列。")
            self.copy(row[field])
            self.status.set("已复制视频路径。" if field == "file" else "已复制泰文文案和话题。")
        except (KeyError, OSError, ValueError, tk.TclError) as exc:
            self.status.set(str(exc))

    def begin(self):
        try:
            self.check_account()
            with self.store.batch_lock():
                row = self.store.begin(self.selected())
            self.load_state(self.store.read())
            self.copy(row["file"])
            self.status.set("已复制视频路径并占用额度。请在 Studio 核对账号、上传并审核。")
        except (KeyError, OSError, ValueError, RuntimeError, tk.TclError) as exc:
            self.status.set(str(exc))

    def confirm(self, status):
        try:
            self.check_account()
            digest = self.selected()
            file = Path(self.items[digest]["file"]).name
            prompts = {
                "pending": "已核对 Studio，确认没有提交这条作品？确认后会释放占用额度。",
                "scheduled": "已在 Studio 核对账号、作品和排期时间？",
                "published": "已在 Studio 核对账号，并看到作品已发布？",
            }
            if not messagebox.askyesno(f"@{self.store.username} · {file}", prompts[status], parent=self.window):
                return
            with self.store.batch_lock():
                self.store.confirm(digest, status)
            self.load_state(self.store.read())
        except (KeyError, OSError, ValueError, RuntimeError) as exc:
            self.status.set(str(exc))

    def open_studio(self):
        if not webbrowser.open(STUDIO, new=2):
            self.status.set(f"请在浏览器打开 {STUDIO}")

