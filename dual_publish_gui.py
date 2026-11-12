"""Launch the queues already prepared on the individual platform pages."""
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


PLATFORMS = {"wechat": "视频号", "tiktok": "TikTok"}


class DualPublishPanel(ttk.Frame):
    def __init__(self, parent, owner):
        super().__init__(parent, padding=(24, 14))
        self.owner = owner
        self.tiktok = owner.tiktok_panel
        self.enabled = {name: tk.BooleanVar(self, value=False) for name in PLATFORMS}
        self.sources = {name: tk.StringVar(self) for name in PLATFORMS}
        self.states = {name: tk.StringVar(self) for name in PLATFORMS}
        self.last_results = {}
        self.active = set()
        self.summary = tk.StringVar(self, value="勾选要发布的平台。")
        self.checkboxes = {}
        self.make_ui()
        for flag in self.enabled.values():
            flag.trace_add("write", self.update_buttons)
        self.poll_id = self.after(300, self.poll)
        self.bind("<Destroy>", self.on_destroy, add=True)

    def make_ui(self):
        ttk.Label(self, text="多平台发布", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(self, text="使用视频号和 TikTok 页签中已经准备好的账号、文案与队列。",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 14))
        for name, label in PLATFORMS.items():
            card = ttk.LabelFrame(self, text=label, padding=(12, 9))
            card.pack(fill="x", pady=(0, 12))
            card.columnconfigure(1, weight=1)
            check = ttk.Checkbutton(card, text=f"发布到{label}" if name == "wechat" else "发布到 TikTok",
                                    variable=self.enabled[name])
            check.grid(row=0, column=0, sticky="w", padx=(0, 12))
            self.checkboxes[name] = check
            ttk.Label(card, textvariable=self.sources[name], wraplength=740).grid(row=0, column=1, sticky="w")
            ttk.Button(card, text="查看设置", command=lambda platform=name: self.open_platform(platform)).grid(
                row=0, column=2, sticky="e", padx=(12, 0))
            ttk.Label(card, textvariable=self.states[name], style="Muted.TLabel", wraplength=920).grid(
                row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(2, 0))
        ttk.Label(actions, textvariable=self.summary, wraplength=660).pack(side="left", fill="x", expand=True)
        self.publish_button = ttk.Button(actions, text="发布到勾选的平台", command=self.publish,
                                         style="Publish.TButton")
        self.publish_button.pack(side="right")
        self.pause_button = ttk.Button(actions, text="暂停本次发布", command=self.pause, state="disabled")
        self.pause_button.pack(side="right", padx=(0, 8))

    def open_platform(self, name):
        self.owner.platform.set(name)
        self.owner.switch_platform()

    def update_buttons(self, *_):
        can_publish = not self.active and any(flag.get() for flag in self.enabled.values())
        self.publish_button.configure(state="normal" if can_publish else "disabled")
        self.pause_button.configure(state="normal" if self.active else "disabled")
        for check in self.checkboxes.values():
            check.configure(state="disabled" if self.active else "normal")

    def refresh(self):
        owner = self.owner
        tiktok = self.tiktok
        for name, panel in (("wechat", owner), ("tiktok", tiktok)):
            if name in self.active and not panel.busy:
                self.active.remove(name)
                self.last_results[name] = panel.status.get()

        self.sources["wechat"].set(f"{owner.account_display.get()} · {Path(owner.table_path.get()).name or '未选择文案表'}")
        self.sources["tiktok"].set(f"{'@' + tiktok.account.get().lstrip('@') if tiktok.account.get() else '未选择账号'}"
                                   f" · {Path(tiktok.table.get()).name or '未选择文案表'}")
        if owner.busy:
            wechat_state = owner.status.get()
        else:
            try:
                mode = owner.publishing_preview()
                wechat_state = f"已载入 {len(owner.items)} 条 · {'沿用已载入的队列' if mode == 'resume' else '等待发布'}"
            except ValueError as exc:
                wechat_state = str(exc)
        if tiktok.busy:
            tiktok_state = tiktok.status.get()
        elif not tiktok.active:
            tiktok_state = "请在 TikTok 页加载账号队列或读取文案表。"
        else:
            try:
                _, selected = tiktok.selected_batch({"active": tiktok.active, "items": tiktok.items})
                tiktok_state = f"本批 {len(selected)} 条待发布或待核对" if selected else "当前队列没有待处理的作品。"
            except ValueError as exc:
                tiktok_state = str(exc)
        for name, panel, message in (("wechat", owner, wechat_state), ("tiktok", tiktok, tiktok_state)):
            previous = self.last_results.get(name)
            self.states[name].set(f"{message} · 上次结果：{previous}" if previous and not panel.busy else message)
        self.update_buttons()

    def publish(self):
        if self.active:
            return
        plans, details, errors, skipped = {}, [], [], []
        for name, label in PLATFORMS.items():
            if not self.enabled[name].get():
                continue
            try:
                if name == "wechat":
                    plan = self.owner.prepare_batch(continue_loaded=True)
                    if plan[-1]:
                        plans[name] = (self.owner.start_prepared_batch, (plan,))
                        details.append(f"视频号\n{plan[3]}")
                    else:
                        skipped.append("视频号没有待发布的作品。")
                else:
                    store, selected = self.tiktok.selected_batch()
                    if selected:
                        plans[name] = (self.tiktok.start_studio, (store, selected))
                        details.append(f"TikTok\n账号：@{store.username}\n本批：{len(selected)} 条，沿用已载入的文案和时间。")
                    else:
                        skipped.append("TikTok 没有待发布或待核对的作品。")
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(f"{label}：{exc}")
        if errors or not plans:
            self.summary.set(" ".join(errors or skipped) or "请勾选要发布的平台。")
            return
        confirmation = "\n\n".join(details + skipped)
        if not messagebox.askyesno("确认发布到勾选的平台", confirmation, parent=self):
            return

        for name, (start, arguments) in plans.items():
            self.last_results.pop(name, None)
            try:
                started = start(*arguments)
                if started is False:
                    self.last_results[name] = "未启动，请检查平台页提示。"
                else:
                    self.active.add(name)
            except (OSError, ValueError, RuntimeError) as exc:
                self.last_results[name] = f"未启动：{exc}"
        self.summary.set("已启动选中的队列，可分别查看进度。" if self.active else "队列未启动，请查看各平台提示。")
        self.refresh()

    def pause(self):
        if "wechat" in self.active and self.owner.busy:
            self.owner.request_stop()
        if "tiktok" in self.active and self.tiktok.busy:
            self.tiktok.pause()
        self.summary.set("已请求暂停，等待当前操作结束。")

    def poll(self):
        was_active = bool(self.active)
        if self.winfo_ismapped() or was_active:
            self.refresh()
        if was_active and not self.active:
            self.summary.set("本次操作已结束，请查看各平台结果。")
        self.poll_id = self.after(300, self.poll)

    def on_destroy(self, event):
        if event.widget is self:
            self.after_cancel(self.poll_id)

