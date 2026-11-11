"""Local desktop form for the installed WeChat Channels CLI."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from publisher_options import OptionsPanel, option_arguments, options_summary, planned_times, scrollable_panel, initialize_desktop_app, short_title_issues, normalize_short_title
from publisher_lock import AccountLock
from daily_publish_limit import DailyPublishBudget, PublishLimitReached

ROOT = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Publisher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.current_action = ""
        self.process: subprocess.Popen | None = None
        self.video = tk.StringVar()
        self.title = tk.StringVar()
        self.tags = tk.StringVar()
        self.account = tk.StringVar(value="creator")
        self.status = tk.StringVar(value="就绪 · 选择视频后填写发布内容")
        self.file_info = tk.StringVar(value="支持 MP4、MOV 等视频文件")
        self.login_status = tk.StringVar(value="")
        self.quota_status = tk.StringVar(value="每账号每小时 6 条 · 每日 36 条（曼谷时间）")
        self.controls: list = []
        root.title("视频号发布助手 v1.12.10")
        root.geometry(f"960x{min(940, root.winfo_screenheight() - 100)}")
        root.minsize(760, 760)
        root.configure(bg="#f3f6f5")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.make_ui()
        self.refresh_accounts()
        self.poll_id = root.after(120, self.poll)

    def configure_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6f5")
        style.configure("TLabel", background="#f3f6f5", foreground="#18382f", font=("Microsoft YaHei UI", 10))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Muted.TLabel", foreground="#61736c", font=("Microsoft YaHei UI", 9))
        style.configure("Field.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Publish.TButton", background="#087f5b", foreground="white", font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Publish.TButton", background=[("active", "#066548"), ("disabled", "#bbc9c3")])
        style.configure("TEntry", padding=7, font=("Microsoft YaHei UI", 10))

    def make_ui(self):
        self.configure_style()
        panel = scrollable_panel(self.root, (28, 22))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(16, weight=1)

        heading = ttk.Frame(panel)
        heading.grid(row=0, column=0, sticky='ew')
        ttk.Label(heading, text="视频号发布助手", style="Heading.TLabel").pack(side='left')
        ttk.Button(heading, text='词映官网', command=self.open_wordmotion).pack(side='right')
        ttk.Label(panel, text="选择视频，填写内容，发布到你的微信视频号。", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 18))

        accounts = ttk.Frame(panel)
        accounts.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        accounts.columnconfigure(1, weight=1)
        ttk.Label(accounts, text="本地账号", style="Field.TLabel").grid(row=0, column=0, padx=(0, 12))
        self.account_box = ttk.Combobox(accounts, textvariable=self.account, state="readonly", width=20, font=("Microsoft YaHei UI", 10))
        self.account_box.grid(row=0, column=1, sticky="w")
        self.account_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_account_status())
        ttk.Label(accounts, textvariable=self.quota_status, style='Muted.TLabel').grid(row=1, column=0, columnspan=4, sticky='w', pady=(7, 0))
        check = ttk.Button(accounts, text="检查登录", command=self.check_login)
        check.grid(row=0, column=2, padx=(8, 8))
        login = ttk.Button(accounts, text="扫码登录", command=self.login)
        login.grid(row=0, column=3)
        stats = ttk.Button(accounts, text='近7天Top10与分析', command=self.show_statistics)
        stats.grid(row=0, column=4, padx=(8,0))
        self.controls.append(stats)
        self.controls.extend([check, login])
        ttk.Label(panel, textvariable=self.login_status, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(1, 16))

        ttk.Label(panel, text="视频文件 *", style="Field.TLabel").grid(row=4, column=0, sticky="w")
        files = ttk.Frame(panel)
        files.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        files.columnconfigure(0, weight=1)
        entry = ttk.Entry(files, textvariable=self.video)
        entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        choose = ttk.Button(files, text="选择视频…", command=self.choose_video)
        choose.grid(row=0, column=1)
        self.controls.extend([entry, choose])
        ttk.Label(panel, textvariable=self.file_info, style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=(5, 14))

        ttk.Label(panel, text="短标题 *（填写到视频号短标题栏）", style="Field.TLabel").grid(row=7, column=0, sticky="w")
        title = ttk.Entry(panel, textvariable=self.title)
        title.grid(row=8, column=0, sticky="ew", pady=(7, 14))
        self.controls.append(title)

        ttk.Label(panel, text="视频描述 / 简介", style="Field.TLabel").grid(row=9, column=0, sticky="w")
        self.description = ScrolledText(panel, height=5, wrap="word", font=("Microsoft YaHei UI", 11), relief="solid", borderwidth=1, padx=10, pady=8, undo=True)
        self.description.grid(row=10, column=0, sticky="ew", pady=(7, 14))
        self.controls.append(self.description)

        ttk.Label(panel, text="话题标签直接写在简介中，例如 #泰语入门 #学泰语。", style="Muted.TLabel").grid(row=11, column=0, sticky="w", pady=(0, 12))

        self.options = OptionsPanel(panel)
        self.options.grid(row=13, column=0, sticky="ew", pady=(0, 15))
        self.controls.extend(self.options.controls)
        actions = ttk.Frame(panel)
        actions.grid(row=14, column=0, sticky="ew")
        ttk.Label(actions, text="保存草稿后，可到视频号后台检查并发布。", style="Muted.TLabel").pack(side="left")
        publish = ttk.Button(actions, text="立即发布到视频号", style="Publish.TButton", command=lambda: self.publish(False))
        publish.pack(side="right", padx=(10, 0))
        self.options.scheduled.trace_add("write", lambda *_: publish.configure(text="提交定时发表" if self.options.scheduled.get() else "立即发布到视频号"))
        draft = ttk.Button(actions, text="保存草稿", command=lambda: self.publish(True))
        draft.pack(side="right")
        self.controls.extend([draft, publish])

        progress = ttk.Frame(panel)
        progress.grid(row=15, column=0, sticky="ew", pady=(18, 8))
        progress.columnconfigure(0, weight=1)
        ttk.Label(progress, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(progress, mode="indeterminate", length=110)
        self.progress.grid(row=0, column=1, sticky="e")
        self.log = ScrolledText(panel, height=7, wrap="word", font=("Microsoft YaHei UI", 9), bg="#eaf0ed", fg="#29483c", relief="flat", padx=10, pady=8, state="disabled")
        self.log.grid(row=16, column=0, sticky="nsew")
        self.append_log("等待操作。上传及发布期间会打开浏览器，请保持窗口开启。\n")

    def refresh_accounts(self):
        aliases = sorted(p.stem[len("tencent_"):] for p in (ROOT / "cookies").glob("tencent_*.json"))
        self.account_box["values"] = aliases or ["creator"]
        if self.account.get() not in (aliases or ["creator"]):
            self.account.set((aliases or ["creator"])[0])
        self.refresh_account_status()

    def refresh_account_status(self):
        saved = (ROOT / "cookies" / f"tencent_{self.account.get()}.json").is_file()
        self.login_status.set("已保存登录会话 · 发布前会自动检查有效性" if saved else "尚未保存登录会话 · 请先扫码登录")
        try:
            usage = DailyPublishBudget(ROOT).usage(self.account.get())
            self.quota_status.set(f'近 60 分钟 {usage["hourly_used"]}/{usage["hourly_limit"]} · 今日 {usage["used"]}/{usage["limit"]}（曼谷）')
        except Exception as exc:
            self.quota_status.set(f'无法读取提交额度：{exc}')

    def check_daily_capacity(self):
        usage = DailyPublishBudget(ROOT).usage(self.account.get())
        DailyPublishBudget.require_capacity(usage)
        return usage

    def open_wordmotion(self):
        try:
            if webbrowser.open('https://www.tugei.com/', new=2):
                return
        except (OSError, webbrowser.Error):
            pass
        messagebox.showinfo('词映官网', '请用浏览器打开：https://www.tugei.com/', parent=self.root)

    def show_statistics(self):
        if self.busy:
            return
        self.stats_path = ROOT/'.local/gui/reports'/f'video-top-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        self.start(['stats', '--account', self.account.get(), '--days', '7', '--top', '10',
                    '--output', str(self.stats_path), '--details'], '读取播放Top10')

    def show_statistics_details(self, snapshot_path):
        if self.busy:
            return
        self.stats_path = Path(snapshot_path)
        self.stats_details_path = ROOT/'.local/gui/reports'/f'video-details-{datetime.now():%Y%m%d-%H%M%S-%f}.json'
        self.start(['stats-details', '--account', self.account.get(), '--snapshot', str(self.stats_path),
                    '--output', str(self.stats_details_path)], '读取Top10详情与报告')

    def open_statistics_report(self, path):
        path = Path(path)
        if path.suffix.lower() != '.html' or not path.is_file() or not path.resolve().is_relative_to((ROOT/'.local/gui/reports').resolve()):
            messagebox.showwarning('报告不存在', '报告文件不存在或路径无效，请重新生成。', parent=self.root)
            return
        try:
            os.startfile(str(path.resolve()))
        except OSError:
            messagebox.showinfo('报告已保存', f'请用浏览器打开：\n{path}', parent=self.root)

    def choose_video(self):
        selected = filedialog.askopenfilename(parent=self.root, title="选择要发布到视频号的视频", initialdir=str(ROOT / "videos"), filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.m4v *.webm"), ("所有文件", "*.*")])
        if selected:
            self.video.set(selected)
            path = Path(selected)
            self.file_info.set(f"{path.name} · {path.stat().st_size / (1024 * 1024):.1f} MB")

    def publish(self, draft: bool):
        if self.busy:
            return
        raw_path = self.video.get().strip()
        path = Path(raw_path).expanduser()
        if not raw_path or not path.is_file():
            messagebox.showwarning("请选择视频", "请先选择一个存在的视频文件。", parent=self.root)
            return
        if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"} or path.stat().st_size == 0:
            messagebox.showwarning("视频文件无效", "请选择非空的 MP4、MOV 等视频文件。", parent=self.root)
            return
        title = normalize_short_title(self.title.get())
        self.title.set(title)
        if not title:
            messagebox.showwarning("请填写短标题", "视频短标题不能为空。", parent=self.root)
            return
        issues = short_title_issues(title)
        if issues:
            messagebox.showwarning("短标题需修改", "\n".join(issues), parent=self.root)
            return
        if not (ROOT / "cookies" / f"tencent_{self.account.get()}.json").is_file():
            messagebox.showwarning("请先登录", "请点击“扫码登录”完成视频号登录。", parent=self.root)
            return
        description = self.description.get("1.0", "end-1c").strip()
        tags = ",".join(t.strip().lstrip("#") for t in self.tags.get().replace("，", ",").split(",") if t.strip().lstrip("#"))
        try:
            if not draft:
                self.check_daily_capacity()
            settings = self.options.snapshot()
            schedules = planned_times(settings, 1, draft=draft)
        except (ValueError, PublishLimitReached) as exc:
            messagebox.showwarning("请检查发布设置", str(exc), parent=self.root)
            return
        action = "保存草稿" if draft else "定时发表" if schedules[0] else "立即发布"
        prompt = f"账号：{self.account.get()}\n视频：{path.name}\n短标题：{title}\n视频描述：{description[:350] or '（未填写）'}\n话题：{tags or '（未填写）'}\n\n"
        prompt += options_summary(settings, schedules) + "\n\n"
        prompt += "将保存为视频号草稿。" if draft else "确认后会上传并提交定时发表。" if schedules[0] else "确认后会上传并立即发布到视频号。"
        if not messagebox.askyesno(f"确认{action}", prompt, parent=self.root):
            return
        arguments = ["upload-video", "--account", self.account.get(), "--file", str(path.resolve()), "--title", title, "--desc", description, "--tags", tags, "--headed"]
        arguments.extend(["--short-title", title])
        arguments.extend(option_arguments(settings, schedules[0]))
        if draft:
            arguments.append("--draft")
        self.start(arguments, action)

    def check_login(self):
        self.start(["check", "--account", self.account.get()], "检查登录")

    def login(self):
        self.start(["login", "--account", self.account.get(), "--headed"], "扫码登录")

    def start(self, arguments: list[str], action: str):
        if self.busy:
            return False
        try:
            self.account_lock = AccountLock(ROOT / ".local" / "gui" / "account-locks", self.account.get()).acquire()
        except RuntimeError as exc:
            messagebox.showwarning("账号正在使用", str(exc), parent=self.root)
            return False
        self.busy = True
        self.current_action = action
        self.status.set(f"正在{action}…")
        self.account_box.configure(state="disabled")
        for control in self.controls:
            control.configure(state="disabled")
        self.progress.start(15)
        self.append_log(f"\n[{datetime.now():%H:%M:%S}] 开始{action}，账号：{self.account.get()}\n")
        threading.Thread(target=self.run_cli, args=(arguments, action), daemon=True).start()
        return True

    def run_cli(self, arguments: list[str], action: str):
        code, log_path = self.execute_cli(arguments, action)
        self.events.put(("done", (code, log_path, action)))

    def execute_cli(self, arguments: list[str], action: str):
        log_path = None
        code = -1
        try:
            logs = ROOT / ".local" / "gui" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / f"{datetime.now():%Y%m%d-%H%M%S-%f}.log"
            env = os.environ.copy()
            if getattr(getattr(self, 'account_lock', None), 'handle', None) is not None:
                env['SAU_ACCOUNT_LOCK_HELD'] = self.account.get()
            else:
                env.pop('SAU_ACCOUNT_LOCK_HELD', None)
            env['SAU_PUBLISH_ATTEMPT'] = str(log_path)
            env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PLAYWRIGHT_BROWSERS_PATH=str(ROOT / ".local" / "browsers"))
            env["TEMP"] = env["TMP"] = str(ROOT / ".local" / "temp")
            Path(env['TEMP']).mkdir(parents=True, exist_ok=True)
            # Invoke the project entry point directly: no shell interpolation of user content.
            bundled_python = ROOT / 'runtime' / 'python.exe'
            if bundled_python.is_file():
                command = [str(bundled_python), '-I', '-B', '-u', str(ROOT / 'desktop_launcher.pyw'), '--cli', 'tencent', *arguments]
            else:
                command = [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-u", str(ROOT / "sau_cli.py"), "tencent", *arguments]
            with log_path.open("w", encoding="utf-8") as log:
                log.write(f"操作：{action}\n")
                self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    clean = ANSI.sub("", line)
                    log.write(clean)
                    log.flush()
                    self.events.put(("log", clean))
                code = self.process.wait()
        except Exception as exc:
            self.events.put(("log", f"运行失败：{exc}\n"))
        finally:
            if self.process is not None and self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None
        return code, log_path

    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "done":
                    code, log_path, action = payload
                    self.account_lock.release()
                    self.busy = False
                    self.progress.stop()
                    self.account_box.configure(state="readonly")
                    for control in self.controls:
                        control.configure(state="normal")
                    self.options.toggle_schedule()
                    self.refresh_accounts()
                    if code == 0:
                        if action == "检查登录":
                            self.login_status.set("登录有效 · 可以发布视频")
                            self.status.set("登录检查通过")
                        elif action == "扫码登录":
                            self.login_status.set("登录有效 · 会话已保存")
                            self.status.set("扫码登录成功")
                        else:
                            self.status.set(f"{action}流程已完成 · 请到视频号后台核对结果")
                    else:
                        if action == "检查登录":
                            self.login_status.set("登录未通过检查 · 请查看日志或重新扫码")
                        self.status.set(f"{action}未完成 · 请查看下方日志")
                        if action in {"立即发布", "保存草稿"}:
                            self.append_log("结果失败或未知时，请先到视频号后台核对作品，避免重复发布。\n")
                    if log_path:
                        self.append_log(f"日志已保存：{log_path}\n")
                    self.task_finished(code, action)
                else:
                    self.on_event(kind, payload)
        except queue.Empty:
            pass
        self.poll_id = self.root.after(120, self.poll)

    def task_finished(self, code, action):
        if action not in {'读取播放Top10', '读取Top10详情与报告'} or code or not getattr(self, 'stats_path', None):
            return
        result = json.loads(self.stats_path.read_text(encoding='utf-8'))
        snapshot_path = self.stats_path
        detail_path = self.stats_details_path if action == '读取Top10详情与报告' else result.get('details_path')
        details = json.loads(Path(detail_path).read_text(encoding='utf-8')) if detail_path else None
        report_path = details.get('report_path') if details else None
        window = tk.Toplevel(self.root)
        window.title('最近7天发表视频 · 当前累计播放Top10')
        window.geometry('1240x580')
        panel = ttk.Frame(window, padding=18)
        panel.pack(fill='both', expand=True)
        start = datetime.fromisoformat(result['window_start']).strftime('%Y-%m-%d %H:%M')
        end = datetime.fromisoformat(result['window_end']).strftime('%Y-%m-%d %H:%M')
        ttk.Label(panel, text=f'发表时间：{start} 至 {end}（曼谷时间）', style='Field.TLabel').pack(anchor='w')
        ttk.Label(panel, text=f'已扫描 {result["scanned_count"]} 条后台记录；范围内正常发表视频 {result["eligible_video_count"]} 条。按读取时累计播放量排名。', wraplength=1000).pack(anchor='w', pady=(6,12))
        columns = [('rank','排名',45),('title','短标题',360),('published','发表时间（曼谷）',160),('views','累计播放',90)]
        if details:
            columns += [('completion','完播率',85),('average','平均观看/秒',100),('follows','关注',65),('recommend','推荐占比',85)]
        tree = ttk.Treeview(panel, columns=tuple(c[0] for c in columns), show='headings', height=11)
        for key, text, width in columns:
            tree.heading(key, text=text);tree.column(key,width=width,anchor='e' if key=='views' else 'w')
        by_id = {v['id']:v for v in details['videos']} if details else {}
        for row in result['top']:
            published = datetime.fromisoformat(row['published_at']).strftime('%Y-%m-%d %H:%M')
            v = by_id.get(row['id'],row)
            values = [row['rank'],row['title'],published,f'{v["views"]:,}' if v.get('views') is not None else '未提供']
            if details:
                from channels_report import fmt, recommend_share
                values += [fmt(v.get('completion_rate'),percent=True),fmt(v.get('average_watch_seconds')),fmt(v.get('follows'),decimals=0),fmt(recommend_share(v),percent=True)]
            tree.insert('', 'end', values=values)
        tree.pack(fill='both', expand=True)
        ttk.Label(panel, text=f'结果已保存：{detail_path or snapshot_path}', wraplength=1150).pack(anchor='w',pady=(10,0))
        buttons = ttk.Frame(panel);buttons.pack(fill='x',pady=(10,0))
        ttk.Button(buttons, text=f'读取这{len(result["top"])}条详情并生成报告', command=lambda:self.show_statistics_details(snapshot_path)).pack(side='left')
        if report_path:
            ttk.Button(buttons, text='打开分析报告', command=lambda:self.open_statistics_report(report_path)).pack(side='left',padx=10)
        ttk.Button(buttons, text='关闭', command=window.destroy).pack(side='right')
        self.status.set(f'已读取Top10 · {len(result["top"])} 条'+(' · 详情与分析报告已生成' if report_path else ''))
        if report_path:
            self.open_statistics_report(report_path)

    def on_event(self, kind, payload):
        pass

    def append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        if self.busy:
            messagebox.showinfo("任务正在进行", "请等待任务结束后再关闭，以便保存登录或发布结果。\n若浏览器需要扫码或验证，请直接在浏览器中完成。", parent=self.root)
            return
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def main():
    initialize_desktop_app()
    root = tk.Tk()
    Publisher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
