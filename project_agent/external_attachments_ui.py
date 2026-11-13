"""External attachment UI, draft persistence and tkdnd integration."""
from __future__ import annotations

import hashlib
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from project_agent.attachments import (
    IMAGE_SUFFIXES, MAX_IMAGES, MAX_TEXT_FILES, copy_attachment,
    enumerate_supported, load_attachment_list, load_draft, save_draft,
)


class ExternalAttachmentsMixin:
    def _draft_key(self):
        if self.chat_id:
            return self.chat_id
        root = str(self.project or '').casefold()
        return 'draft' + hashlib.sha256(root.encode('utf-8')).hexdigest()[:32]

    def _save_composer_draft(self):
        if getattr(self, '_loading_composer', False) or not hasattr(self, 'task'):
            return
        try:
            save_draft(self._draft_key(), self.task.get('1.0', 'end-1c'),
                       self.external_attachments, self.context_files, self.context_dirs)
        except (OSError, ValueError) as exc:
            self.set_status(f'Черновик не сохранён: {exc}')

    def _restore_composer_draft(self):
        self._loading_composer = True
        try:
            data = load_draft(self._draft_key())
            self.task.delete('1.0', 'end')
            self.task.insert('1.0', str(data.get('text') or ''))
            self.external_attachments = load_attachment_list(self._draft_key())
            self.context_files = list(data.get('files') or [])
            self.context_dirs = list(data.get('dirs') or [])
            self.attached = []
            self._refresh_attach()
        finally:
            self._loading_composer = False

    def _init_external_drop(self):
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            self.TkdndVersion = TkinterDnD._require(self)
            def register(widget):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._on_external_drop)
                widget.dnd_bind('<<DropEnter>>', self._drop_enter)
                widget.dnd_bind('<<DropLeave>>', self._drop_leave)
                for child in widget.winfo_children():
                    register(child)
            register(self.card)
            self._external_drop_available = True
        except (ImportError, tk.TclError, AttributeError) as exc:
            self._external_drop_available = False
            self.set_status('Перетаскивание недоступно: установите зависимости; файлы можно выбрать через «+».')

    def _drop_enter(self, _event):
        self.card.configure(border_color='#5d9cec')
        self.set_status('Отпустите файлы, чтобы прикрепить')
        return 'copy'

    def _drop_leave(self, _event=None):
        from project_agent.ui_theme import BORDER
        self.card.configure(border_color=BORDER)
        return 'copy'

    def _on_external_drop(self, event):
        self._drop_leave()
        if self.running:
            return 'none'
        try:
            paths = [Path(value) for value in self.tk.splitlist(event.data)]
        except (tk.TclError, ValueError):
            return 'none'
        self._import_external_paths(paths)
        return 'copy'

    def _select_external_candidates(self, candidates, text_room, image_room):
        win = tk.Toplevel(self)
        win.title('Выберите вложения')
        win.geometry('680x460')
        win.transient(self)
        tk.Label(win, text=f'Можно выбрать: текстовых файлов {text_room}, изображений {image_room}').pack(pady=8)
        box = tk.Listbox(win, selectmode='multiple', exportselection=False)
        box.pack(fill='both', expand=True, padx=10, pady=8)
        scroll = tk.Scrollbar(box, command=box.yview)
        scroll.pack(side='right', fill='y')
        box.configure(yscrollcommand=scroll.set)
        for path, name in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            box.insert('end', f'{name} — {size:,} байт')
        result = []
        def accept():
            selected = [candidates[index] for index in box.curselection()]
            images = sum(path.suffix.casefold() in IMAGE_SUFFIXES for path, _ in selected)
            if images > image_room or len(selected) - images > text_room:
                messagebox.showwarning('Лимит вложений', 'Уменьшите количество выбранных файлов.', parent=win)
                return
            result.extend(selected)
            win.destroy()
        tk.Button(win, text='Добавить выбранные', command=accept).pack(pady=8)
        tk.Button(win, text='Отмена', command=win.destroy).pack(pady=4)
        win.grab_set()
        self.wait_window(win)
        return result

    def _import_external_paths(self, paths):
        if self.running:
            return
        if any(path.is_dir() for path in paths):
            if not messagebox.askyesno('Вложения из папки',
                    'Добавить файлы из папки и подпапок? Служебные каталоги и явно чувствительные файлы будут пропущены. '
                    'Автоматически обнаружить все секреты невозможно.', parent=self):
                return
        candidates, errors = [], []
        known = {str(item.get('source') or '').casefold() for item in self.external_attachments}
        for source in paths:
            found, skipped = enumerate_supported(source)
            errors.extend(skipped)
            for path in found:
                identity = str(path.resolve()).casefold()
                if identity in known:
                    continue
                known.add(identity)
                name = (source.name + '/' + path.relative_to(source).as_posix()) if source.is_dir() else path.name
                candidates.append((path, name))
        image_room = max(0, MAX_IMAGES - len(self.attached) - sum(bool(x['image']) for x in self.external_attachments))
        text_room = max(0, MAX_TEXT_FILES - len(self.context_files) - sum(not x['image'] for x in self.external_attachments))
        images = sum(path.suffix.casefold() in IMAGE_SUFFIXES for path, _ in candidates)
        if images > image_room or len(candidates) - images > text_room:
            candidates = self._select_external_candidates(candidates, text_room, image_room)
        for path, name in candidates:
            item = None
            try:
                item = copy_attachment(path, self._draft_key(), name)
                if not item['image']:
                    from project_agent.attachments import load_text
                    try:
                        load_text(item)
                    except UnicodeDecodeError:
                        body = load_text(item, 'cp1251')
                        if not self._confirm_encoding(item['name'], body):
                            from project_agent.attachments import remove_attachment
                            remove_attachment(item)
                            continue
                        item['encoding'] = 'cp1251'
                self.external_attachments.append(item)
            except (OSError, ValueError) as exc:
                if item:
                    from project_agent.attachments import remove_attachment
                    remove_attachment(item)
                errors.append(f'{path.name}: {exc}')
        self._refresh_attach()
        self._save_composer_draft()
        if errors:
            messagebox.showwarning('Некоторые файлы не добавлены', '\n'.join(errors[:30]), parent=self)

    def _confirm_encoding(self, name, body):
        win = tk.Toplevel(self)
        win.title('Предпросмотр Windows-1251 — ' + name)
        win.geometry('700x450')
        win.transient(self)
        tk.Label(win, text='UTF-8 не подошёл. Проверьте текст Windows-1251 перед добавлением.').pack(pady=8)
        text = tk.Text(win, wrap='word')
        text.pack(fill='both', expand=True, padx=10)
        text.insert('1.0', body[:12000])
        text.configure(state='disabled')
        result = []
        def accept():
            result.append(True)
            win.destroy()
        tk.Button(win, text='Использовать Windows-1251', command=accept).pack(pady=8)
        tk.Button(win, text='Отмена', command=win.destroy).pack(pady=4)
        win.grab_set()
        self.wait_window(win)
        return bool(result)
