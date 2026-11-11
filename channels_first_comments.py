"""Match comment copy to verified publications and track manual confirmations."""
from datetime import datetime
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from batch_table import ALIASES, read_tables, records_from_rows
from publisher_lock import AccountLock
from publisher_preferences import first_directory, write_local_json

WAITING = '等待发表核验'
CHECK = '待核对作者评论'
SCHEDULED = '定时发表待核对'
CONFLICT = '有多条匹配，需核对'
SENT = '已确认有作者评论'


def read_comment_table(path):
    tables = read_tables(path)
    candidates = []
    for name, rows in tables.items():
        populated = [(n, row) for n, row in rows if any(str(value).strip() for value in row)]
        if populated:
            header = [str(value).strip().casefold() for value in populated[0][1]]
            if '首评' in header:
                candidates.append((name, populated, header))
    if len(candidates) != 1:
        raise ValueError('请提供一张含“首评”列的工作表。')
    _, rows, header = candidates[0]
    if not all(any(alias in header for alias in ALIASES[k]) for k in ('file', 'desc')):
        raise ValueError('首评表还需要“文件名”“短标题”和“简介”列，用来核对对应作品。')
    comment_index = header.index('首评')
    source = dict(rows[1:])
    result = {}
    for row in records_from_rows(rows):
        number = row['source_row']
        cells = source[number]
        comment = cells[comment_index] if comment_index < len(cells) else ''
        if not comment.strip():
            continue
        filename = row['file']
        title = row['short_title'] or row['title']
        if row.get('match_by_episode') or not Path(filename).suffix or PureWindowsPath(filename).name != filename:
            raise ValueError(f'首评表第 {number} 行需要填写完整文件名（含扩展名）。')
        if not title or not row['publish_desc']:
            raise ValueError(f'首评表第 {number} 行缺少短标题或简介。')
        key = filename.casefold()
        if key in result:
            raise ValueError(f'首评表有重复视频文件名：{filename}')
        result[key] = dict(file=filename, title=title, desc=row['publish_desc'], comment=comment)
    if not result:
        raise ValueError('没有找到可用的首评文案。')
    return result


class FirstCommentQueue:
    def __init__(self, directory, batch_dir, account):
        self.directory = Path(directory)
        self.batch_dir = Path(batch_dir)
        self.account = account.strip().casefold()
        if not self.account:
            raise ValueError('请先选择视频号账号。')
        self.path = self.directory / (hashlib.sha256(self.account.encode('utf-8')).hexdigest() + '.json')

    def state(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'source': '', 'confirmed': {}}
        if not isinstance(data, dict) or not isinstance(data.get('confirmed'), dict) or not isinstance(data.get('source'), str):
            raise ValueError('首评记录格式异常，已停止读取，请保留原记录。')
        return data

    def set_source(self, path):
        path = Path(path).resolve()
        read_comment_table(path)
        lock = AccountLock(self.directory / 'locks', self.account).acquire()
        try:
            data = self.state()
            data['source'] = str(path)
            write_local_json(self.path, data)
        finally:
            lock.release()

    def rows(self, state=None, *, files=None):
        data = self.state() if state is None else state
        source = read_comment_table(data['source']) if data['source'] else {}
        if files is not None:
            source = {key: row for key, row in source.items() if key in files}
        if not source:
            return []
        matches = {}
        for path in self.batch_dir.glob('*.json'):
            batch = json.loads(path.read_text(encoding='utf-8'))
            if (batch.get('draft') or batch.get('requires_backend_audit') or
                    str(batch.get('account', '')).strip().casefold() != self.account):
                continue
            for row in batch.get('items', []):
                filename = PureWindowsPath(row.get('file', '')).name.casefold()
                target = source.get(filename)
                proof = row.get('platform_verification') or {}
                digest = row.get('video_sha256', '').lower()
                if (not target or row.get('status') != 'completed' or proof.get('verified') is not True
                        or proof.get('draft') or not re.fullmatch(r'[a-f0-9]{64}', digest)
                        or target['title'] != (row.get('short_title') or row.get('title'))
                        or target['desc'] != row.get('publish_desc', row.get('desc'))
                        or target['desc'] != proof.get('matched_description', target['desc'])):
                    continue
                # Duplicate receipts for a video are one candidate. If any proof is
                # for immediate publication, prefer it to a scheduling receipt.
                prior = matches.setdefault(filename, {}).get(digest)
                if prior is None or (prior.get('schedule') and not row.get('schedule')):
                    matches[filename][digest] = row
        output = []
        for filename, target in source.items():
            evidence = matches.get(filename, {})
            item = dict(target, status=WAITING, digest='', published='')
            if len(evidence) > 1:
                item['status'] = CONFLICT
            elif evidence:
                digest, row = next(iter(evidence.items()))
                confirmed = digest in data['confirmed']
                status = SENT if confirmed else SCHEDULED if row.get('schedule') else CHECK
                verified = row.get('platform_verification') or {}
                published = row.get('schedule') or verified.get('published_at') or row.get('submit_clicked_at') or ''
                item.update(status=status, digest=digest, published=published)
            output.append(item)
        return output

    def confirm(self, item):
        lock = AccountLock(self.directory / 'locks', self.account).acquire()
        try:
            data = self.state()
            key = item['file'].casefold()
            row = next(iter(self.rows(data, files={key})), None)
            if not row or row['digest'] != item['digest'] or row['status'] not in (CHECK, SCHEDULED, SENT):
                raise ValueError('这条作品的匹配已改变，请刷新后重新核对。')
            if row['status'] == SENT:
                return False
            data['confirmed'][row['digest']] = {
                'file': row['file'], 'confirmed_at': datetime.now().astimezone().isoformat(),
                'method': 'manual_platform_check',
            }
            write_local_json(self.path, data)
            return True
        finally:
            lock.release()


class FirstCommentWindow:
    def __init__(self, parent, queue, initial_directory):
        self.queue = queue
        self.initial_directory = initial_directory
        self.window = window = tk.Toplevel(parent)
        window.title(f'首评队列 · {queue.account}')
        window.geometry('920x570')
        window.transient(parent)
        panel = ttk.Frame(window, padding=12)
        panel.pack(fill='both', expand=True)
        self.status = tk.StringVar(window)
        ttk.Label(panel, textvariable=self.status, wraplength=840).pack(anchor='w')
        ttk.Label(panel, text='先在视频号评论管理核对该作品。已有作者评论就标记；没有再复制对应文案。', wraplength=840).pack(anchor='w', pady=(4, 8))
        top = ttk.Frame(panel)
        top.pack(fill='x', pady=(0, 8))
        ttk.Button(top, text='选择首评表…', command=self.choose).pack(side='left')
        ttk.Button(top, text='刷新', command=self.refresh).pack(side='left', padx=(8, 0))
        frame = ttk.Frame(panel)
        frame.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(frame, columns=('file', 'title', 'status'), show='headings', selectmode='browse')
        for key, label, width in [('file', '视频文件', 115), ('title', '短标题', 380), ('status', '首评状态', 180)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key == 'title')
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.show_selected)
        self.detail = ScrolledText(panel, wrap='word', height=7, state='disabled')
        self.detail.pack(fill='x', pady=(8, 0))
        bottom = ttk.Frame(panel)
        bottom.pack(fill='x', pady=(8, 0))
        self.copy_button = ttk.Button(bottom, text='复制首评', command=self.copy)
        self.copy_button.pack(side='left')
        self.confirm_button = ttk.Button(bottom, text='标记已有作者评论', command=self.confirm)
        self.confirm_button.pack(side='left', padx=(8, 0))
        self.items = {}
        self.refresh()

    def choose(self):
        try:
            previous = self.queue.state()['source']
            file = filedialog.askopenfilename(parent=self.window, title='选择首评文案表',
                                              initialdir=first_directory(Path(previous).parent if previous else None, self.initial_directory),
                                              filetypes=[('首评表', '*.xlsx *.csv *.tsv')])
            if file:
                self.queue.set_source(file)
                self.refresh()
        except Exception as exc:
            messagebox.showerror('无法读取首评表', str(exc), parent=self.window)

    def refresh(self):
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        try:
            state = self.queue.state()
            for row in self.queue.rows(state):
                key = row['file'].casefold()
                self.items[key] = row
                self.tree.insert('', 'end', iid=key, values=(row['file'], row['title'], row['status']))
            if selection and selection[0] in self.items:
                self.tree.selection_set(selection[0])
            totals = {s: sum(row['status'] == s for row in self.items.values()) for s in (CHECK, SCHEDULED, SENT)}
            self.status.set(f'{Path(state["source"]).name} · 待核对首评 {totals[CHECK]} · 定时待核对 {totals[SCHEDULED]} · 已确认 {totals[SENT]}'
                            if state['source'] else '请先选择包含首评文案的表格。')
        except Exception as exc:
            self.status.set(f'无法读取首评队列：{exc}')
        self.show_selected()

    def selected(self):
        selection = self.tree.selection()
        return self.items.get(selection[0]) if selection else None

    def show_selected(self, _event=None):
        row = self.selected()
        ready = bool(row and row['status'] in (CHECK, SCHEDULED))
        self.copy_button.configure(state='normal' if ready else 'disabled')
        self.confirm_button.configure(state='normal' if ready else 'disabled')
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        if row:
            when = f'\n发表记录：{row["published"]}' if row['published'] else ''
            self.detail.insert('1.0', f'{row["comment"]}\n\n作品简介：{row["desc"]}{when}')
        self.detail.configure(state='disabled')

    def copy(self):
        self.refresh()
        row = self.selected()
        if not row or row['status'] not in (CHECK, SCHEDULED):
            return
        if not messagebox.askyesno('核对作品', f'{row["file"]}：是否已在评论管理确认作品已发表，且没有作者评论？',
                                  parent=self.window):
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(row['comment'])
        self.status.set(f'{row["file"]} 首评已复制；发表后，请核对评论并标记。')

    def confirm(self):
        row = self.selected()
        if not row or not messagebox.askyesno('确认已有作者评论', f'已在 {row["file"]} 对应的作品下看到作者评论？',
                                             parent=self.window):
            return
        try:
            self.queue.confirm(row)
            self.refresh()
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror('无法保存首评记录', str(exc), parent=self.window)
