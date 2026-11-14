"""Compose articles and sync them to the WeChat Official Account draft box."""
import json
import os
from pathlib import Path
from queue import Empty, Queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from official_account_drafts import WeChatSettings, sync_draft
from official_account_images import IMAGE_MODELS, body_paragraphs, generate_image, validate_images
from official_account_writer import DEFAULT_MODEL, generate_draft, validate_draft
from publisher_preferences import write_local_json


WRITING_MODELS = (DEFAULT_MODEL, "gpt-5.4", "gpt-5.4-nano")


class OfficialAccountPanel(ttk.Frame):
    def __init__(self, parent, data_dir):
        super().__init__(parent, padding=(26, 14))
        self.data_dir = data_dir
        self.results = Queue()
        self.generation_poll = None
        self.wechat = WeChatSettings(
            os.environ.get("WECHAT_OFFICIAL_APP_ID", "").strip(),
            os.environ.get("WECHAT_OFFICIAL_APP_SECRET", "").strip(),
            os.environ.get("WECHAT_OFFICIAL_THUMB_MEDIA_ID", "").strip(),
        )
        self.auto_sync = tk.BooleanVar(self, value=False)
        self.syncing = False
        self.images = []
        self.image_count = tk.StringVar(self, value="正文配图 0 张")
        self.model = tk.StringVar(self, value=os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL)
        self.image_model = tk.StringVar(self, value=IMAGE_MODELS[0])
        self.api_key = tk.StringVar(self)
        self.topic = tk.StringVar(self)
        self.title = tk.StringVar(self)
        self.summary = tk.StringVar(self)
        self.status = tk.StringVar(self, value="填写选题后可生成文章。")
        self.build()
        self.bind("<Destroy>", self.cancel_poll, add=True)

    def build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(7, weight=1)
        ttk.Label(self, text="微信公众号 · AI 写作", style="Heading.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        settings = ttk.Frame(self)
        settings.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(settings, text="OpenAI API Key").pack(side="left")
        ttk.Entry(settings, textvariable=self.api_key, show="*", width=28).pack(side="left", padx=(8, 18))
        ttk.Label(settings, text="写作模型").pack(side="left")
        ttk.Combobox(settings, textvariable=self.model, values=WRITING_MODELS, width=22).pack(
            side="left", padx=(8, 12))
        ttk.Label(settings, text="密钥仅在本次打开期间使用，也可从系统设置读取。",
                  style="Muted.TLabel").pack(side="left")
        self.add_field(2, "选题", self.topic)
        ttk.Label(self, text="写作要求（可选）").grid(row=3, column=0, sticky="nw", padx=(0, 12), pady=4)
        self.brief = tk.Text(self, height=3, wrap="word", font=("Microsoft YaHei UI", 10))
        self.brief.grid(row=3, column=1, sticky="ew", pady=4)
        controls = ttk.Frame(self)
        controls.grid(row=4, column=1, sticky="ew", pady=(5, 9))
        self.generate_button = ttk.Button(controls, text="生成文章", command=self.generate, style="Publish.TButton")
        self.generate_button.pack(side="left")
        self.sync_option = ttk.Checkbutton(controls, text="生成后自动同步到公众号草稿箱", variable=self.auto_sync)
        self.sync_option.pack(side="left", padx=12)
        self.settings_button = ttk.Button(controls, text="公众号同步设置", command=self.configure_wechat)
        self.settings_button.pack(side="left")
        self.add_field(5, "文章标题", self.title)
        self.add_field(6, "摘要", self.summary)
        body_tools = ttk.Frame(self)
        body_tools.grid(row=7, column=0, sticky="nw", padx=(0, 12), pady=4)
        ttk.Label(body_tools, text="正文").pack(anchor="w")
        self.image_button = ttk.Button(body_tools, text="AI 正文配图", command=self.configure_images)
        self.image_button.pack(anchor="w", pady=(8, 4))
        ttk.Label(body_tools, textvariable=self.image_count).pack(anchor="w")
        self.body = ScrolledText(self, height=12, wrap="word", font=("Microsoft YaHei UI", 10))
        self.body.grid(row=7, column=1, sticky="nsew", pady=4)
        actions = ttk.Frame(self)
        actions.grid(row=8, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(actions, textvariable=self.status, wraplength=480).pack(side="left", fill="x", expand=True)
        self.open_button = ttk.Button(actions, text="打开本地草稿", command=self.open_draft)
        self.open_button.pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="保存本地草稿", command=self.save_draft).pack(side="right", padx=(8, 0))
        self.sync_button = ttk.Button(actions, text="同步当前草稿", command=self.sync_current)
        self.sync_button.pack(side="right")

    def add_field(self, row, label, variable):
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)

    def draft(self):
        draft = {"title": self.title.get(), "summary": self.summary.get(), "body": self.body.get("1.0", "end-1c")}
        if self.images:
            draft["images"] = list(self.images)
        return draft

    def show_draft(self, draft):
        self.title.set(draft["title"])
        self.summary.set(draft["summary"])
        self.body.delete("1.0", "end")
        self.body.insert("1.0", draft["body"])
        self.images = list(draft.get("images", []))
        self.refresh_images()

    def refresh_images(self):
        self.image_count.set(f"正文配图 {len(self.images)} 张")

    def configure_images(self):
        paragraphs = body_paragraphs(self.draft()["body"].strip())
        if not paragraphs:
            self.status.set("请先填写正文，再选择要配图的段落。")
            return
        dialog = tk.Toplevel(self)
        dialog.title("AI 正文配图")
        dialog.transient(self.winfo_toplevel())
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="将图片插在所选段落后").pack(anchor="w")
        choices = [f"{index + 1}. {' '.join(part.split())[:68]}" for index, part in enumerate(paragraphs)]
        choice = ttk.Combobox(panel, values=choices, state="readonly", width=65)
        choice.current(0)
        choice.pack(fill="x", pady=(3, 8))
        ttk.Label(panel, text="配图模型").pack(anchor="w")
        ttk.Combobox(panel, textvariable=self.image_model, values=IMAGE_MODELS).pack(fill="x", pady=(3, 8))
        ttk.Label(panel, text="配图描述（可选）").pack(anchor="w")
        prompt = ttk.Entry(panel)
        prompt.pack(fill="x", pady=(3, 8))
        ttk.Label(panel, text="已添加的配图").pack(anchor="w")
        items = tk.Listbox(panel, height=5, exportselection=False)
        items.pack(fill="x")
        for item in self.images:
            items.insert("end", item["caption"])

        def generate():
            key = self.api_key.get().strip() or os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                messagebox.showwarning("请填写 OpenAI API Key", "图片生成使用写作页的 OpenAI API Key。", parent=dialog)
                return
            model = self.image_model.get().strip()
            if not model:
                messagebox.showwarning("请选择配图模型", "请选择或输入配图模型名称。", parent=dialog)
                return
            paragraph = paragraphs[choice.current()]
            note = prompt.get().strip()
            caption = note or " ".join(paragraph.split())[:75]
            description = (
                f"为公众号文章《{self.title.get()}》创作一张横版正文插画。"
                "画面清晰，适合手机阅读，不加文字、水印或标志。"
                f"对应段落：{paragraph}\n画面要求：{note or '用一个具体场景表现这段的主要内容。'}"
            )
            dialog.destroy()
            self.status.set("OpenAI 正在生成正文配图…")

            def finish(path, error):
                if error:
                    self.status.set(error)
                    return
                self.images.append({"after": paragraph, "caption": caption, "file": str(path.resolve())})
                self.refresh_images()
                self.status.set(f"已添加正文配图，可在「AI 正文配图」中查看。")

            self.run_in_background(lambda: generate_image(key, description, self.data_dir / "images", model=model), finish)

        def preview():
            indexes = items.curselection()
            if indexes:
                try:
                    os.startfile(self.images[indexes[0]]["file"])
                except OSError:
                    messagebox.showwarning("无法打开配图", "请检查图片文件是否仍在本机。", parent=dialog)

        def remove():
            indexes = items.curselection()
            if indexes:
                self.images.pop(indexes[0])
                items.delete(indexes[0])
                self.refresh_images()

        buttons = ttk.Frame(panel)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="生成并插入", command=generate).pack(side="left")
        ttk.Button(buttons, text="查看所选图片", command=preview).pack(side="left", padx=8)
        ttk.Button(buttons, text="移除所选图片", command=remove).pack(side="left")
        dialog.grab_set()

    def can_replace(self):
        return not any(value.strip() for value in self.draft().values()) or messagebox.askyesno(
            "替换编辑区内容", "当前编辑区已有文章。继续后将替换编辑区里的文章，已保存的草稿会保留。",
            parent=self)

    def configure_wechat(self):
        dialog = tk.Toplevel(self)
        dialog.title("公众号同步设置")
        dialog.transient(self.winfo_toplevel())
        panel = ttk.Frame(dialog, padding=18)
        panel.pack(fill="both", expand=True)
        values = []
        for row, (label, initial, masked) in enumerate((
            ("公众号 AppID", self.wechat.app_id, False),
            ("AppSecret", self.wechat.secret, True),
            ("永久封面素材 ID", self.wechat.cover_id, False),
        )):
            value = tk.StringVar(dialog, value=initial)
            values.append(value)
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            ttk.Entry(panel, textvariable=value, width=42, show="*" if masked else "").grid(
                row=row, column=1, sticky="ew", pady=5)
        ttk.Label(panel, text="配置在本次打开期间有效。").grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 8))

        def apply_settings():
            settings = WeChatSettings(*(value.get().strip() for value in values))
            try:
                settings.validate()
            except ValueError as error:
                messagebox.showwarning("请检查公众号设置", str(error), parent=dialog)
                return
            self.wechat = settings
            self.status.set("公众号同步配置已填写。")
            dialog.destroy()

        ttk.Button(panel, text="应用", command=apply_settings).grid(row=4, column=1, sticky="e")
        dialog.grab_set()

    def set_busy(self, busy):
        for control in (self.generate_button, self.open_button, self.sync_option,
                        self.settings_button, self.sync_button, self.image_button):
            control.state(["disabled" if busy else "!disabled"])

    def run_in_background(self, operation, on_done):
        self.set_busy(True)

        def work():
            try:
                self.results.put((operation(), ""))
            except (RuntimeError, OSError, ValueError) as error:
                self.results.put((None, str(error)))

        def poll():
            try:
                value, error = self.results.get_nowait()
            except Empty:
                self.generation_poll = self.after(100, poll)
                return
            self.generation_poll = None
            self.set_busy(False)
            on_done(value, error)

        threading.Thread(target=work, daemon=True).start()
        self.generation_poll = self.after(100, poll)

    def generate(self):
        key = self.api_key.get().strip() or os.environ.get("OPENAI_API_KEY", "").strip()
        model, topic = self.model.get().strip(), self.topic.get().strip()
        if not key or not model or not topic:
            self.status.set("请填写选题、模型和 OpenAI API Key。")
            return
        settings = self.wechat if self.auto_sync.get() else None
        if settings:
            try:
                settings.validate()
            except ValueError as error:
                self.status.set(str(error))
                return
        if not self.can_replace():
            return
        arguments = (key, model, topic, self.brief.get("1.0", "end-1c"))
        previous = self.draft()
        self.status.set("OpenAI 正在撰写文章…")
        def finish(draft, error):
            if error:
                self.status.set(error)
            elif self.draft() != previous and not self.can_replace():
                self.status.set("已保留编辑区内容。")
            else:
                self.show_draft(draft)
                if settings:
                    self.start_sync(settings, draft)
                else:
                    self.status.set("文章已生成，可编辑后保存本地草稿。")

        self.run_in_background(lambda: generate_draft(*arguments), finish)

    def sync_current(self):
        try:
            self.wechat.validate()
            draft = self.validated_draft(self.draft())
        except ValueError as error:
            self.status.set(str(error))
            return
        self.start_sync(self.wechat, draft)

    def start_sync(self, settings, draft):
        self.syncing = True
        self.status.set("正在同步到公众号草稿箱…")
        directory = self.data_dir.parent / "receipts"

        def finish(_media_id, error):
            self.syncing = False
            self.status.set(error or f"已同步到公众号草稿箱并核对《{draft['title']}》。")

        self.run_in_background(lambda: sync_draft(directory, settings, draft), finish)

    def cancel_poll(self, event):
        if event.widget is self and self.generation_poll is not None:
            self.after_cancel(self.generation_poll)

    def save_draft(self):
        try:
            draft = self.validated_draft(self.draft())
            self.data_dir.mkdir(parents=True, exist_ok=True)
            selected = filedialog.asksaveasfilename(
                parent=self, title="保存微信公众号草稿", initialdir=self.data_dir,
                defaultextension=".json", initialfile="公众号草稿.json", filetypes=[("公众号草稿", "*.json")])
            if selected:
                path = Path(selected)
                write_local_json(path, draft)
                self.status.set(f"已保存本地草稿 {path.name}")
        except (OSError, ValueError) as error:
            self.status.set(str(error))

    def open_draft(self):
        if not self.can_replace():
            return
        selected = filedialog.askopenfilename(
            parent=self, title="打开微信公众号草稿",
            initialdir=self.data_dir if self.data_dir.is_dir() else Path.home(),
            filetypes=[("公众号草稿", "*.json")])
        if selected:
            try:
                path = Path(selected)
                self.show_draft(self.validated_draft(json.loads(path.read_text(encoding="utf-8")), path.parent))
                self.status.set(f"已打开本地草稿 {path.name}")
            except (OSError, ValueError):
                self.status.set("无法打开草稿，请选择发布助手保存的草稿文件。")

    @staticmethod
    def validated_draft(raw, directory=None):
        draft = validate_draft(raw)
        images = validate_images({**raw, **draft}, directory)
        if images:
            draft["images"] = images
        return draft
