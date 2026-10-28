"""Панель редактора: вкладки, gutter с номерами строк, подсветка синтаксиса, сохранение.

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к редактору: построение
виджетов (шапка с вкладками и кнопкой «Сохранить», тело, gutter, текстовый бокс
с подменой scrollcommand), открытие файла по пути и номеру строки, переключение
и закрытие вкладок, учёт несохранённых правок, номера строк, подсветка
синтаксиса с debounce, сохранение на диск и скрытие панели.

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``work``, ``editor_frame``,
``editor_box``, ``editor_gutter``, ``editor_body``, ``editor_tabs_bar``,
``project``, ``editor_open``/``editor_path``/``editor_saved``/``editor_width``,
``editor_tabs``, ``_editor_tab_buttons``, ``_highlight_job``, ``ui_font``,
``_code_font``, ``set_status``, ``write_chat``, ``_ask``, ``_refresh_git_badge``.
Поэтому код оформлен миксином ``EditorPanelMixin``: методы остаются методами
``App`` (``class App(OutputPanelMixin, ChatViewMixin, SidePanelMixin,
EditorPanelMixin, ctk.CTk)``), но живут в отдельном файле.

Состояние редактора и модель вкладок (``EditorTabs``) остались на ``App``: их
читают другие части приложения (``_apply_font``/``_apply_theme``, watcher
боковой панели, ``_persist``, ``_on_close``), перенос был бы не дословным.

Порядок вызовов при старте: ``_build_editor_panel()`` — виджеты (вызывается из
``App._build`` на том же месте, где раньше создавался блок).
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from project_agent.clipboard import _on_layout_clipboard
from project_agent.config import DEFAULT_UI_FONT
from project_agent.dialogs import SaveChangesDialog
from project_agent.paths import PathError, normalize_relative, read_text_file, resolve_inside
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    FIELD,
    HIGHLIGHT_DEBOUNCE_MS,
    MUTED,
    ON_SEND,
    PANEL,
    SEND,
    SEND_HOVER,
    SYN_COMMENT,
    SYN_DECORATOR,
    SYN_HEADING,
    SYN_KEYWORD,
    SYN_LITERAL,
    SYN_MD_CODE,
    SYN_NUMBER,
    SYN_STRING,
    TEXT,
)
from project_agent.ui_widgets import quiet_button, _tone


class EditorPanelMixin:
    """Виджеты и поведение панели редактора (см. докстринг модуля)."""

    def _build_editor_panel(self) -> None:
        self.editor_frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        self.editor_frame.grid_columnconfigure(0, weight=1)
        self.editor_frame.grid_rowconfigure(1, weight=1)
        editor_head = ctk.CTkFrame(self.editor_frame, fg_color="transparent")
        editor_head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        editor_head.grid_columnconfigure(0, weight=1)
        self.editor_tabs_bar = ctk.CTkFrame(editor_head, fg_color="transparent")
        self.editor_tabs_bar.grid(row=0, column=0, sticky="ew")
        # Высота как у «Сжать» в футере (24); закрытие — × на вкладке.
        quiet_button(editor_head, "Сохранить", lambda: self._save_editor(confirm=False), width=72, height=24).grid(
            row=0, column=1, padx=(6, 0)
        )
        editor_body = ctk.CTkFrame(self.editor_frame, fg_color=FIELD, corner_radius=0)
        self.editor_body = editor_body
        editor_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        editor_body.grid_columnconfigure(1, weight=1)
        editor_body.grid_rowconfigure(0, weight=1)
        self.editor_gutter = tk.Text(
            editor_body,
            width=3,
            wrap="none",
            bd=0,
            highlightthickness=0,
            takefocus=0,
            cursor="arrow",
            padx=2,
            pady=2,
            bg=_tone(PANEL),
            fg=_tone(MUTED),
            font=self._editor_gutter_font(),
            state="disabled",
        )
        self.editor_gutter.grid(row=0, column=0, sticky="nsw")
        self.editor_box = ctk.CTkTextbox(
            editor_body,
            wrap="none",
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=self._editor_text_font(),
            scrollbar_button_color=("#b5ada3", "#2c2824"),
            scrollbar_button_hover_color=("#8d847a", "#3a342e"),
        )
        self.editor_box.grid(row=0, column=1, sticky="nsew")
        inner = self.editor_box._textbox
        inner.configure(yscrollcommand=self._on_editor_scroll)
        if getattr(self.editor_box, "_scrollbar", None) is not None:
            self.editor_box._scrollbar.configure(command=self._editor_yview)
        elif getattr(self.editor_box, "_y_scrollbar", None) is not None:
            self.editor_box._y_scrollbar.configure(command=self._editor_yview)
        inner.bind("<Control-KeyPress>", self._on_editor_control, add="+")
        for seq in ("<KeyRelease>", "<ButtonRelease-1>", "<MouseWheel>", "<Configure>"):
            inner.bind(seq, self._on_editor_change, add="+")
        inner.bind("<<Modified>>", self._on_editor_modified, add="+")
        self.editor_gutter.bind("<MouseWheel>", self._on_gutter_wheel)
        self._configure_editor_syntax_tags()

    def _editor_gutter_font(self, weight: str = "normal") -> tuple:
        return self._code_font(10, weight)

    def _editor_text_font(self, weight: str = "normal") -> tuple:
        return self._code_font(12, weight)

    def _open_file_at(self, relative: str, line: int = 1) -> None:
        if self.project is None:
            return
        relative = normalize_relative(relative)
        if not relative:
            return
        self._flush_editor_to_tab()
        existing = self.editor_tabs.find(relative)
        if existing >= 0:
            self.editor_tabs.select(existing)
            self._load_active_tab_into_editor()
            self._rebuild_editor_tab_bar()
            self._jump_editor_line(line)
            return
        try:
            text = self._editor_source(relative)
        except PathError as exc:
            self.write_chat(f"Файл не открыт: {exc}")
            return
        self.editor_tabs.open(relative, text)
        self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()
        self.after(20, lambda: self._jump_editor_line(line))

    def _jump_editor_line(self, line: int) -> None:
        if not self.editor_open:
            return
        try:
            target = max(1, min(int(line or 1), self._editor_line_count()))
            self.editor_box.mark_set("insert", f"{target}.0")
            self.editor_box.see(f"{target}.0")
            self.editor_box.focus_set()
            self._sync_editor_gutter()
            self.set_status(f"{self.editor_path}:{target}")
        except (tk.TclError, ValueError, TypeError):
            return

    def _flush_editor_to_tab(self) -> None:
        if not self.editor_open or not self.editor_tabs:
            return
        self.editor_tabs.sync_active_text(self._editor_text())

    def _load_active_tab_into_editor(self) -> None:
        tab = self.editor_tabs.active()
        if tab is None:
            return
        self.editor_path = tab.path
        self.editor_saved = tab.saved
        self.editor_box.delete("1.0", "end")
        if tab.text:
            self.editor_box.insert("1.0", tab.text)
        self.editor_box.mark_set("insert", "1.0")
        self.editor_box.see("1.0")
        try:
            self.editor_box._textbox.edit_modified(False)
        except tk.TclError:
            pass
        self._update_editor_gutter()
        self._highlight_editor()
        if not self.editor_open:
            self.work.add(self.editor_frame, stretch="always", minsize=240, sticky="nsew")
            self.editor_open = True
            self.after(30, self._place_editor_sash)
        self.editor_box.focus_set()

    def _rebuild_editor_tab_bar(self) -> None:
        bar = getattr(self, "editor_tabs_bar", None)
        if bar is None:
            return
        for child in bar.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._editor_tab_buttons = []
        face = self.ui_font or DEFAULT_UI_FONT
        for index, tab in enumerate(self.editor_tabs.tabs):
            active = index == self.editor_tabs.active_index
            label = tab.label()
            name_w = max(48, min(120, 10 + len(label) * 7))
            wrap = ctk.CTkFrame(
                bar,
                fg_color=SEND if active else BUTTON,
                corner_radius=8,
                border_width=0 if active else 1,
                border_color=BORDER,
            )
            wrap.pack(side="left", padx=(0, 4))
            name_btn = ctk.CTkButton(
                wrap,
                text=label,
                width=name_w,
                height=22,
                corner_radius=6,
                border_width=0,
                fg_color="transparent",
                hover_color=SEND_HOVER if active else BUTTON_HOVER,
                text_color=ON_SEND if active else TEXT,
                font=(face, 11),
                command=lambda i=index: self._select_editor_tab(i),
            )
            name_btn.pack(side="left", padx=(4, 0), pady=1)
            close_btn = ctk.CTkButton(
                wrap,
                text="×",
                width=22,
                height=22,
                corner_radius=6,
                border_width=0,
                fg_color="transparent",
                hover_color=SEND_HOVER if active else BUTTON_HOVER,
                text_color=ON_SEND if active else MUTED,
                font=(face, 14),
                command=lambda i=index: self._close_editor_tab(i),
            )
            close_btn.pack(side="left", padx=(0, 2), pady=1)
            self._editor_tab_buttons.append(name_btn)

    def _refresh_editor_tab_labels(self) -> None:
        buttons = getattr(self, "_editor_tab_buttons", None) or []
        tabs = self.editor_tabs.tabs
        if len(buttons) != len(tabs):
            self._rebuild_editor_tab_bar()
            return
        for btn, tab in zip(buttons, tabs):
            label = tab.label()
            try:
                if btn.cget("text") != label:
                    self._rebuild_editor_tab_bar()
                    return
            except tk.TclError:
                self._rebuild_editor_tab_bar()
                return

    def _select_editor_tab(self, index: int) -> None:
        if index == self.editor_tabs.active_index:
            return
        self._flush_editor_to_tab()
        if self.editor_tabs.select(index) is None:
            return
        self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()

    def _reload_clean_editor(self) -> None:
        if not self.editor_open or self.project is None or not self.editor_tabs:
            return
        self._flush_editor_to_tab()
        active_changed = False
        for tab in list(self.editor_tabs.tabs):
            if tab.dirty:
                continue
            try:
                text = self._editor_source(tab.path)
            except PathError:
                continue
            if self.editor_tabs.apply_disk_if_clean(tab.path, text):
                active_changed = True
        if active_changed:
            self._load_active_tab_into_editor()
        self._refresh_editor_tab_labels()

    def _editor_source(self, relative: str) -> str:
        text = read_text_file(self.project, relative)
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _editor_text(self) -> str:
        return self.editor_box.get("1.0", "end-1c")

    def _editor_line_count(self) -> int:
        try:
            return max(1, int(str(self.editor_box._textbox.index("end-1c")).split(".")[0]))
        except (tk.TclError, ValueError, AttributeError):
            return 1

    def _editor_dirty(self) -> bool:
        if not self.editor_open:
            return False
        self._flush_editor_to_tab()
        tab = self.editor_tabs.active()
        return bool(tab and tab.dirty)

    def _style_editor_gutter(self) -> None:
        gutter = getattr(self, "editor_gutter", None)
        if gutter is None:
            return
        try:
            gutter.configure(
                bg=_tone(PANEL),
                fg=_tone(MUTED),
                font=self._editor_gutter_font(),
                insertbackground=_tone(TEXT),
            )
        except tk.TclError:
            return
        body = getattr(self, "editor_body", None)
        if body is not None:
            try:
                body.configure(fg_color=FIELD)
            except tk.TclError:
                pass
        self._configure_editor_syntax_tags()
        if self.editor_open:
            self._highlight_editor()

    def _configure_editor_syntax_tags(self) -> None:
        from project_agent.syntax import (
            ALL_TAGS,
            TAG_COMMENT,
            TAG_DECORATOR,
            TAG_HEADING,
            TAG_KEYWORD,
            TAG_LITERAL,
            TAG_MD_CODE,
            TAG_NUMBER,
            TAG_STRING,
        )

        box = getattr(self, "editor_box", None)
        if box is None:
            return
        colors = {
            TAG_KEYWORD: SYN_KEYWORD,
            TAG_STRING: SYN_STRING,
            TAG_COMMENT: SYN_COMMENT,
            TAG_NUMBER: SYN_NUMBER,
            TAG_DECORATOR: SYN_DECORATOR,
            TAG_HEADING: SYN_HEADING,
            TAG_MD_CODE: SYN_MD_CODE,
            TAG_LITERAL: SYN_LITERAL,
        }
        try:
            inner = box._textbox
            for tag in ALL_TAGS:
                tone = colors.get(tag, TEXT)
                inner.tag_configure(tag, foreground=_tone(tone))
            try:
                inner.tag_raise("sel")
            except tk.TclError:
                pass
        except tk.TclError:
            return

    def _schedule_editor_highlight(self) -> None:
        job = getattr(self, "_highlight_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        self._highlight_job = self.after(HIGHLIGHT_DEBOUNCE_MS, self._highlight_editor)

    def _highlight_editor(self) -> None:
        self._highlight_job = None
        from project_agent.syntax import ALL_TAGS, language_for_path, tokenize

        box = getattr(self, "editor_box", None)
        if box is None or not self.editor_open:
            return
        lang = language_for_path(self.editor_path)
        try:
            inner = box._textbox
            for tag in ALL_TAGS:
                inner.tag_remove(tag, "1.0", "end")
            if not lang:
                return
            text = self._editor_text()
            spans = tokenize(text, lang)
            for start, end, tag in spans:
                inner.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
            try:
                inner.tag_raise("sel")
            except tk.TclError:
                pass
        except tk.TclError:
            return

    def _update_editor_gutter(self, _event=None) -> None:
        gutter = getattr(self, "editor_gutter", None)
        box = getattr(self, "editor_box", None)
        if gutter is None or box is None:
            return
        try:
            lines = self._editor_line_count()
            digits = max(3, len(str(lines)))
            content = "\n".join(f"{i:>{digits}}" for i in range(1, lines + 1))
            gutter.configure(state="normal", width=digits + 1)
            gutter.delete("1.0", "end")
            gutter.insert("1.0", content)
            gutter.configure(state="disabled")
            self._sync_editor_gutter()
        except tk.TclError:
            return

    def _sync_editor_gutter(self) -> None:
        gutter = getattr(self, "editor_gutter", None)
        box = getattr(self, "editor_box", None)
        if gutter is None or box is None:
            return
        try:
            first, _last = box._textbox.yview()
            gutter.yview_moveto(first)
        except tk.TclError:
            return

    def _on_editor_scroll(self, first, last) -> None:
        scrollbar = getattr(self.editor_box, "_scrollbar", None) or getattr(self.editor_box, "_y_scrollbar", None)
        if scrollbar is not None:
            try:
                scrollbar.set(first, last)
            except tk.TclError:
                pass
        self._sync_editor_gutter()

    def _editor_yview(self, *args) -> None:
        try:
            self.editor_box._textbox.yview(*args)
        except tk.TclError:
            return
        self._sync_editor_gutter()

    def _on_editor_change(self, _event=None) -> None:
        self.after_idle(self._update_editor_gutter)

    def _on_editor_modified(self, _event=None) -> None:
        try:
            if self.editor_box._textbox.edit_modified():
                self.editor_box._textbox.edit_modified(False)
                self._update_editor_gutter()
                self._schedule_editor_highlight()
                self._flush_editor_to_tab()
                self._refresh_editor_tab_labels()
        except tk.TclError:
            return

    def _on_gutter_wheel(self, event) -> str | None:
        delta = int(getattr(event, "delta", 0) or 0)
        steps = int(-delta / 120) if delta else 0
        if steps == 0 and delta:
            steps = -1 if delta > 0 else 1
        if steps:
            try:
                self.editor_box._textbox.yview_scroll(steps, "units")
            except tk.TclError:
                return "break"
            self._sync_editor_gutter()
        return "break"

    def _on_editor_control(self, event):
        if not (int(getattr(event, "state", 0) or 0) & 0x4):
            return
        key = str(getattr(event, "keysym", "") or "").lower()
        code = int(getattr(event, "keycode", 0) or 0)
        # S / Ы (save) — keycode устойчив к раскладке на Windows.
        if key in {"s", "ы"} or code == 83:
            self._save_editor(confirm=False)
            return "break"
        return _on_layout_clipboard(event)

    def _place_editor_sash(self, tries: int = 0) -> None:
        if not self.editor_open:
            return
        total = self.work.winfo_width()
        if total < 200:
            if tries < 8:
                self.after(50, lambda: self._place_editor_sash(tries + 1))
            return
        try:
            self.work.sash_place(0, max(280, total - self.editor_width), 1)
        except tk.TclError:
            return

    def _remember_editor_width(self, _event=None) -> None:
        if not self.editor_open:
            return
        total = self.work.winfo_width()
        try:
            left, _top = self.work.sash_coord(0)
        except tk.TclError:
            return
        self.editor_width = max(240, total - left)

    def _save_editor(self, confirm: bool = False) -> bool:
        if not self.editor_open or self.project is None:
            return False
        self._flush_editor_to_tab()
        return self._save_editor_tab(self.editor_tabs.active_index, confirm=confirm)

    def _save_editor_tab(self, index: int, confirm: bool = False) -> bool:
        if self.project is None or not (0 <= index < len(self.editor_tabs)):
            return False
        tab = self.editor_tabs.tabs[index]
        text = tab.text
        if index == self.editor_tabs.active_index:
            text = self._editor_text()
            tab.text = text
        if text == tab.saved:
            if index == self.editor_tabs.active_index:
                self.set_status("Изменений нет")
            return True
        if confirm and not self._ask(tab.path, "Записать файл?"):
            return False
        try:
            full = resolve_inside(self.project, tab.path)
            if full.exists() and full.is_dir():
                raise PathError("это папка, не файл")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text, encoding="utf-8", newline="\n")
        except (OSError, PathError) as exc:
            self.write_chat(f"Файл не записан: {exc}")
            return False
        self.editor_tabs.mark_saved_at(index, text)
        if index == self.editor_tabs.active_index:
            self.editor_saved = text
        self._refresh_editor_tab_labels()
        self._refresh_git_badge()
        self.set_status("Файл записан")
        return True

    def _ask_save_changes(self, path: str) -> bool | None:
        dialog = SaveChangesDialog(self, path)
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        return dialog.result

    def _close_editor_tab(self, index: int) -> None:
        if not self.editor_open or not (0 <= index < len(self.editor_tabs)):
            return
        self._flush_editor_to_tab()
        tab = self.editor_tabs.tabs[index]
        if tab.dirty:
            choice = self._ask_save_changes(tab.path)
            if choice is None:
                return
            if choice:
                if not self._save_editor_tab(index, confirm=False):
                    return
        was_active = index == self.editor_tabs.active_index
        nxt = self.editor_tabs.close_at(index)
        if nxt is None:
            self._hide_editor_panel()
            return
        if was_active:
            self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()

    def _hide_editor_panel(self) -> None:
        job = getattr(self, "_highlight_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
            self._highlight_job = None
        try:
            self.work.forget(self.editor_frame)
        except tk.TclError:
            pass
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_tabs.close_all()
        self.editor_box.delete("1.0", "end")
        try:
            self.editor_gutter.configure(state="normal")
            self.editor_gutter.delete("1.0", "end")
            self.editor_gutter.configure(state="disabled")
        except tk.TclError:
            pass
        self._rebuild_editor_tab_bar()

    def _release_editor(self, ask: bool) -> bool:
        if not self.editor_open:
            return True
        self._flush_editor_to_tab()
        if ask and self.editor_tabs.any_dirty():
            if not self._ask("вкладки", "Закрыть несохранённые вкладки без записи?"):
                return False
        self._hide_editor_panel()
        return True
