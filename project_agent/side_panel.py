"""Боковая панель: список чатов, дерево файлов, git-декорации, поиск/символы/проблемы.

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к боковой панели: построение
виджетов (шапка с именем папки, сегменты «Чаты/Файлы», дерево проекта, дерево
чатов, панель поиска), стили ``ttk``, рендер подписей файлов в PIL (иконка типа
+ цветная подпись), git-декорации (badge над лентой и цветные имена), watcher
изменений проекта и перетаскиваемая граница панели.

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``body``, ``file_pane``,
``chat_pane``, ``tree``, ``project``, ``ui_font``, ``side_width``,
``_git_file_status``/``_git_dirty_dirs``, ``_font``/``_code_font``,
``set_status``, ``write_chat``, ``_open_file_at``, ``_refresh_chat_list``,
``_refresh_attach``, ``_expanded_paths`` и т.п. Поэтому код оформлен миксином
``SidePanelMixin``: методы остаются методами ``App``
(``class App(OutputPanelMixin, ChatViewMixin, SidePanelMixin, ctk.CTk)``),
но живут в отдельном файле.

Состояние (``side_width``, ``_git_file_status``, ``_git_dirty_dirs``) и кэши
картинок (``_tree_text_photos``, ``_tree_kind_icons``, ``_pil_font_cache``)
остались на ``App``: их читают/сбрасывают другие части приложения, а атрибуты
создаются лениво через ``getattr(self, ...)``, как и раньше.

Порядок вызовов при старте: ``_build_side_panel()`` — виджеты (вызывается из
``App._build`` на том же месте, где раньше создавался блок).
"""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageTk

from project_agent.paths import PathError, list_entries
from project_agent.tree_icons import _draw_tree_kind_icon, _tree_kind_for_name
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    FIELD,
    GIT_DEL,
    GIT_MOD,
    GIT_NEW,
    MUTED,
    PANEL,
    SELECT,
    SIDE_DEFAULT,
    SIDE_MIN,
    TEXT,
)
from project_agent.ui_widgets import (
    _PENDING,
    _tone,
    glyph,
    icon_button,
    quiet_button,
    tree_path_iid,
    tree_path_rel,
)


class SidePanelMixin:
    """Виджеты и поведение боковой панели; требует атрибутов ``App``."""

    def _build_side_panel(self) -> None:
        side = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=0)
        self.side = side
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(3, weight=1)
        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=(12, 8))
        head.grid_columnconfigure(0, weight=1)
        self.folder_name = ctk.CTkLabel(head, text="", anchor="w", text_color=TEXT, font=self._font(13, weight="bold"))
        self.folder_name.grid(row=0, column=0, sticky="ew")
        self.folder_path = ctk.CTkLabel(head, text="", anchor="w", text_color=MUTED, font=self._font(11))
        self.folder_path.grid(row=1, column=0, sticky="ew")
        self.folder_name.bind("<Configure>", lambda _event: self._fit_labels())
        self.folder_path.bind("<Configure>", lambda _event: self._fit_labels())
        icon_button(head, "gear", self.open_settings, size=24).grid(row=0, column=1, padx=(4, 0))
        icon_button(head, "dots", self.choose_folder, size=24).grid(row=1, column=1, padx=(4, 0))
        quiet_button(side, "Новый чат", self.new_chat, mark="plus").grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.side_tabs = ctk.CTkSegmentedButton(
            side,
            values=["Чаты", "Файлы"],
            command=self._show_side_tab,
            height=30,
            corner_radius=10,
            fg_color=BUTTON_HOVER,
            selected_color=BUTTON,
            selected_hover_color=BUTTON,
            unselected_color=BUTTON_HOVER,
            unselected_hover_color=SELECT,
            text_color=TEXT,
            font=self._font(12),
        )
        self.side_tabs.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.chat_pane = ctk.CTkFrame(side, fg_color="transparent")
        self.chat_pane.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.chat_pane.grid_columnconfigure(0, weight=1)
        self.chat_pane.grid_rowconfigure(0, weight=1)
        self.file_pane = ctk.CTkFrame(side, fg_color="transparent")
        self.file_pane.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.file_pane.grid_columnconfigure(0, weight=1)
        self.file_pane.grid_rowconfigure(1, weight=1)
        self.file_pane.grid_rowconfigure(2, weight=0)
        files_top = ctk.CTkFrame(self.file_pane, fg_color="transparent")
        files_top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        files_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(files_top, text="Структура", anchor="w", text_color=MUTED, font=self._font(12)).grid(
            row=0, column=0, sticky="w", padx=(6, 0)
        )
        self.file_util_toggle = icon_button(files_top, "search", self.toggle_file_util_panel, size=28)
        self.file_util_toggle.grid(row=0, column=1, padx=(0, 2))
        icon_button(files_top, "refresh", self._refresh_tree, size=28).grid(row=0, column=2)
        tree_holder = ctk.CTkFrame(self.file_pane, fg_color="transparent")
        tree_holder.grid(row=1, column=0, sticky="nsew")
        tree_holder.grid_columnconfigure(0, weight=1)
        tree_holder.grid_rowconfigure(0, weight=1)
        self._style_tree()
        self.tree = ttk.Treeview(tree_holder, show="tree", selectmode="browse", style="Project.Treeview")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ctk.CTkScrollbar(
            tree_holder, command=self.tree.yview, fg_color=PANEL, button_color=BUTTON, button_hover_color=BUTTON_HOVER
        )
        tree_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.bind("<<TreeviewOpen>>", self._tree_open)
        self.tree.bind("<<TreeviewSelect>>", self._on_file_click)
        self.tree.bind("<Button-3>", self._file_menu)
        self._configure_git_tree_tags()
        self._file_util_open = False
        self._build_file_util_panel()
        self._hide_file_util_panel()
        self.chat_tree = ttk.Treeview(self.chat_pane, show="tree", selectmode="browse", style="Chats.Treeview")
        self.chat_tree.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ctk.CTkScrollbar(
            self.chat_pane, command=self.chat_tree.yview, fg_color=PANEL, button_color=BUTTON, button_hover_color=BUTTON_HOVER
        )
        chat_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.chat_tree.configure(yscrollcommand=chat_scroll.set)
        self.chat_tree.bind("<<TreeviewSelect>>", self._on_chat_select)
        self.chat_tree.bind("<Button-3>", self._chat_menu)
        self.side_tabs.set("Файлы")
        self._show_side_tab("Файлы")
        self.body.add(side, stretch="never", minsize=SIDE_MIN, sticky="nsew")

    def _show_side_tab(self, name: str) -> None:
        if name == "Файлы":
            self.chat_pane.grid_remove()
            self.file_pane.grid()
        else:
            self.file_pane.grid_remove()
            self.chat_pane.grid()

    def _place_side_sash(self, tries: int = 0) -> None:
        body = getattr(self, "body", None)
        if body is None:
            return
        total = body.winfo_width()
        if total < SIDE_MIN + 360:
            if tries < 10:
                self.after(50, lambda: self._place_side_sash(tries + 1))
            return
        width = max(SIDE_MIN, min(int(self.side_width or SIDE_DEFAULT), total - 360))
        try:
            body.sash_place(0, width, 1)
        except tk.TclError:
            return

    def _remember_side_width(self, _event=None) -> None:
        body = getattr(self, "body", None)
        if body is None:
            return
        try:
            left, _top = body.sash_coord(0)
        except tk.TclError:
            return
        self.side_width = max(SIDE_MIN, int(left))
        self._fit_labels()

    def _build_file_util_panel(self) -> None:
        """Поиск / символы / диагностики под деревом (P0.4 / P0.5 / P1.9)."""
        panel = ctk.CTkFrame(self.file_pane, fg_color=PANEL, corner_radius=10, border_width=1, border_color=BORDER)
        self.file_util_panel = panel
        panel.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        mode_row = ctk.CTkFrame(panel, fg_color="transparent")
        mode_row.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.file_find_mode_btn = quiet_button(mode_row, "Поиск", lambda: self._set_file_util_mode("find"), width=64, height=24)
        self.file_find_mode_btn.pack(side="left", padx=(0, 4))
        self.file_symbol_mode_btn = quiet_button(
            mode_row, "Символы", lambda: self._set_file_util_mode("symbols"), width=72, height=24
        )
        self.file_symbol_mode_btn.pack(side="left", padx=(0, 4))
        self.file_diag_mode_btn = quiet_button(
            mode_row, "Проблемы", lambda: self._set_file_util_mode("problems"), width=80, height=24
        )
        self.file_diag_mode_btn.pack(side="left")
        self.file_util_status = ctk.CTkLabel(mode_row, text="", anchor="e", text_color=MUTED, font=self._font(10))
        self.file_util_status.pack(side="right")

        self.file_find_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_find_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        self.file_find_row.grid_columnconfigure(0, weight=1)
        self.file_find_var = ctk.StringVar(value="")
        self.file_find_entry = ctk.CTkEntry(
            self.file_find_row,
            textvariable=self.file_find_var,
            placeholder_text="Найти в файлах…",
            height=26,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            font=self._font(11),
        )
        self.file_find_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.file_find_entry.bind("<Return>", lambda _e: self._run_file_find())
        quiet_button(self.file_find_row, "Найти", self._run_file_find, width=56, height=24).grid(row=0, column=1)

        self.file_symbol_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_symbol_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        self.file_symbol_row.grid_columnconfigure(0, weight=1)
        self.file_symbol_var = ctk.StringVar(value="")
        self.file_symbol_entry = ctk.CTkEntry(
            self.file_symbol_row,
            textvariable=self.file_symbol_var,
            placeholder_text="Имя символа или модуль…",
            height=26,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            font=self._font(11),
        )
        self.file_symbol_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.file_symbol_entry.bind("<Return>", lambda _e: self._run_symbol_find())
        self.file_symbol_kind_btn = quiet_button(
            self.file_symbol_row, "Символ", self._toggle_symbol_query_mode, width=72, height=24
        )
        self.file_symbol_kind_btn.grid(row=0, column=1, padx=(0, 4))
        quiet_button(self.file_symbol_row, "Найти", self._run_symbol_find, width=56, height=24).grid(row=0, column=2)

        self.file_diag_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_diag_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        quiet_button(self.file_diag_row, "Проверить py", self._run_py_compile_check, width=100, height=24).pack(
            side="left"
        )
        quiet_button(self.file_diag_row, "Из вывода", self._diag_from_output, width=88, height=24).pack(
            side="left", padx=(4, 0)
        )
        quiet_button(self.file_diag_row, "Очистить", self._clear_diagnostics, width=72, height=24).pack(side="left", padx=(4, 0))

        list_holder = ctk.CTkFrame(panel, fg_color="transparent")
        list_holder.grid(row=2, column=0, sticky="nsew", padx=6, pady=(2, 6))
        list_holder.grid_columnconfigure(0, weight=1)
        list_holder.grid_rowconfigure(0, weight=1)
        self.file_hit_list = tk.Listbox(
            list_holder,
            activestyle="dotbox",
            exportselection=False,
            bd=0,
            highlightthickness=0,
            bg=_tone(FIELD),
            fg=_tone(TEXT),
            selectbackground=_tone(SELECT),
            selectforeground=_tone(TEXT),
            font=self._code_font(10),
        )
        self.file_hit_list.grid(row=0, column=0, sticky="nsew")
        hit_scroll = ctk.CTkScrollbar(
            list_holder,
            command=self.file_hit_list.yview,
            fg_color=PANEL,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
        )
        hit_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.file_hit_list.configure(yscrollcommand=hit_scroll.set)
        self.file_hit_list.bind("<Double-Button-1>", self._on_file_hit_activate)
        self.file_hit_list.bind("<Return>", self._on_file_hit_activate)
        self._set_file_util_mode("find")

    def toggle_file_util_panel(self) -> None:
        if self._file_util_open:
            self._hide_file_util_panel()
        else:
            self._show_file_util_panel()

    def _show_file_util_panel(self, mode: str | None = None) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        if mode is not None:
            self._set_file_util_mode(mode)
        try:
            panel.grid()
            self.file_pane.grid_rowconfigure(1, weight=3)
            self.file_pane.grid_rowconfigure(2, weight=2)
        except tk.TclError:
            return
        self._file_util_open = True
        toggle = getattr(self, "file_util_toggle", None)
        if toggle is not None:
            try:
                toggle.configure(fg_color=BUTTON_HOVER)
            except tk.TclError:
                pass
        if self._file_util_mode == "find":
            try:
                self.file_find_entry.focus_set()
            except tk.TclError:
                pass
        elif self._file_util_mode == "symbols":
            try:
                self.file_symbol_entry.focus_set()
            except tk.TclError:
                pass

    def _hide_file_util_panel(self) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        try:
            panel.grid_remove()
            self.file_pane.grid_rowconfigure(1, weight=1)
            self.file_pane.grid_rowconfigure(2, weight=0)
        except tk.TclError:
            return
        self._file_util_open = False
        toggle = getattr(self, "file_util_toggle", None)
        if toggle is not None:
            try:
                toggle.configure(fg_color="transparent")
            except tk.TclError:
                pass

    def _set_file_util_mode(self, mode: str) -> None:
        if mode == "problems":
            self._file_util_mode = "problems"
        elif mode == "symbols":
            self._file_util_mode = "symbols"
        else:
            self._file_util_mode = "find"
        self.file_find_row.grid_remove()
        self.file_symbol_row.grid_remove()
        self.file_diag_row.grid_remove()
        if self._file_util_mode == "find":
            self.file_find_row.grid()
            self._fill_file_hit_list(self._find_hits)
        elif self._file_util_mode == "symbols":
            self.file_symbol_row.grid()
            self._sync_symbol_kind_button()
            self._fill_file_hit_list(self._symbol_hits)
        else:
            self.file_diag_row.grid()
            self._fill_file_hit_list(self._diag_hits)

    def _sync_symbol_kind_button(self) -> None:
        btn = getattr(self, "file_symbol_kind_btn", None)
        if btn is None:
            return
        label = "Importers" if self._symbol_query_mode == "importers" else "Символ"
        try:
            btn.configure(text=label)
        except tk.TclError:
            return

    def _toggle_symbol_query_mode(self) -> None:
        self._symbol_query_mode = "importers" if self._symbol_query_mode == "symbol" else "symbol"
        self._sync_symbol_kind_button()
        entry = getattr(self, "file_symbol_entry", None)
        if entry is None:
            return
        hint = "Модуль / путь — кто импортирует…" if self._symbol_query_mode == "importers" else "Имя символа или модуль…"
        try:
            entry.configure(placeholder_text=hint)
        except tk.TclError:
            pass

    def _style_file_util_panel(self) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        try:
            panel.configure(fg_color=PANEL, border_color=BORDER)
            self.file_find_entry.configure(fg_color=FIELD, border_color=BORDER, text_color=TEXT, font=self._font(11))
            self.file_symbol_entry.configure(fg_color=FIELD, border_color=BORDER, text_color=TEXT, font=self._font(11))
            self.file_util_status.configure(text_color=MUTED, font=self._font(10))
            self.file_hit_list.configure(
                bg=_tone(FIELD),
                fg=_tone(TEXT),
                selectbackground=_tone(SELECT),
                selectforeground=_tone(TEXT),
                font=self._code_font(10),
            )
            toggle = getattr(self, "file_util_toggle", None)
            if toggle is not None:
                toggle.configure(
                    image=glyph("search"),
                    fg_color=BUTTON_HOVER if self._file_util_open else "transparent",
                    hover_color=BUTTON_HOVER,
                )
        except tk.TclError:
            return

    def _fill_file_hit_list(self, items: list) -> None:
        box = getattr(self, "file_hit_list", None)
        if box is None:
            return
        try:
            box.delete(0, "end")
            for item in items:
                box.insert("end", item.label())
        except tk.TclError:
            return
        count = len(items)
        if self._file_util_mode == "problems":
            self.file_util_status.configure(text=f"{count}" if count else "чисто")
        else:
            self.file_util_status.configure(text=f"{count}" if count else "")

    def _run_file_find(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        query = (self.file_find_var.get() or "").strip()
        if not query:
            self.set_status("Введите строку поиска")
            return
        from project_agent.findfiles import search_project

        self._find_hits = search_project(self.project, query, limit=80)
        self._set_file_util_mode("find")
        if not self._find_hits:
            self.set_status("Совпадений нет")
        else:
            self.set_status(f"Найдено: {len(self._find_hits)}")

    def _run_symbol_find(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        query = (self.file_symbol_var.get() or "").strip()
        if not query:
            self.set_status("Введите имя символа или модуля")
            return
        from project_agent.index_store import search_importers, search_symbols

        if self._symbol_query_mode == "importers":
            hits, _data = search_importers(self.project, query, limit=80)
            self._symbol_hits = hits
            label = "Importers"
        else:
            hits, _data = search_symbols(self.project, query, limit=80)
            self._symbol_hits = hits
            label = "Символы"
        self._set_file_util_mode("symbols")
        if not self._symbol_hits:
            self.set_status(f"{label}: нет совпадений")
        else:
            self.set_status(f"{label}: {len(self._symbol_hits)}")

    def _run_py_compile_check(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        from project_agent.diagnostics import compile_project_python

        self.set_status("Проверка синтаксиса…")
        self.update_idletasks()
        self._diag_hits = compile_project_python(self.project)
        self._set_file_util_mode("problems")
        if not self._diag_hits:
            self.set_status("Синтаксис Python: ок")
        else:
            self.set_status(f"Проблемы: {len(self._diag_hits)}")

    def _diag_from_output(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        text = ""
        if self._output_slots:
            text = str(self._output_slots[0].get("text") or "")
        if not text.strip():
            self.set_status("Вывод пуст")
            return
        from project_agent.diagnostics import parse_python_locations

        self._diag_hits = parse_python_locations(text, self.project)
        self._set_file_util_mode("problems")
        if not self._diag_hits:
            self.set_status("В выводе нет path:line")
        else:
            self.set_status(f"Из вывода: {len(self._diag_hits)}")

    def _clear_diagnostics(self) -> None:
        self._diag_hits = []
        if self._file_util_mode == "problems":
            self._fill_file_hit_list(self._diag_hits)
        self.set_status("Проблемы очищены")

    def _on_file_hit_activate(self, _event=None) -> None:
        box = getattr(self, "file_hit_list", None)
        if box is None:
            return
        try:
            selection = box.curselection()
        except tk.TclError:
            return
        if not selection:
            return
        index = int(selection[0])
        if self._file_util_mode == "find":
            items = self._find_hits
        elif self._file_util_mode == "symbols":
            items = self._symbol_hits
        else:
            items = self._diag_hits
        if index < 0 or index >= len(items):
            return
        item = items[index]
        self._open_file_at(str(item.path), int(item.line or 1))

    def _clear_tree_label_cache(self) -> None:
        self._tree_text_photos = {}
        self._tree_kind_icons = {}
        self._pil_font_cache = {}

    def _tree_label_px(self) -> int:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        return max(14, round(14 * scale))

    def _tree_icon_px(self) -> int:
        return max(16, self._tree_label_px())

    def _pil_ui_font(self, size: int = 14):
        key = (self.ui_font, size)
        cache = getattr(self, "_pil_font_cache", None)
        if cache is None:
            self._pil_font_cache = {}
            cache = self._pil_font_cache
        hit = cache.get(key)
        if hit is not None:
            return hit
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        family = (self.ui_font or "").lower().replace(" ", "")
        mapped = {
            "segoeui": ["segoeui.ttf"],
            "arial": ["arial.ttf"],
            "tahoma": ["tahoma.ttf"],
            "calibri": ["calibri.ttf"],
            "verdana": ["verdana.ttf"],
            "timesnewroman": ["times.ttf"],
            "couriernew": ["cour.ttf"],
        }
        candidates: list[Path] = []
        for name, files in mapped.items():
            if name == family or name in family or family in name:
                candidates.extend(fonts_dir / item for item in files)
        raw = (self.ui_font or "").replace(" ", "")
        if raw:
            candidates.append(fonts_dir / f"{raw}.ttf")
            candidates.append(fonts_dir / f"{raw}.TTF")
        candidates.extend(
            [
                fonts_dir / "segoeui.ttf",
                fonts_dir / "arial.ttf",
                fonts_dir / "tahoma.ttf",
            ]
        )
        for path in candidates:
            try:
                if path.is_file():
                    font = ImageFont.truetype(str(path), size=size)
                    cache[key] = font
                    return font
            except OSError:
                continue
        font = ImageFont.load_default()
        cache[key] = font
        return font

    def _tree_caption_rgba(self, text: str, fill: str) -> Image.Image:
        px = self._tree_label_px()
        font = self._pil_ui_font(px)
        left, top, right, bottom = font.getbbox(text or " ")
        glyph_h = max(1, bottom - top)
        width = max(1, right - left + 2)
        # Высота = глиф; иначе пустой низ сдвигает текст вверх относительно иконки.
        height = glyph_h + 2
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(sheet).text((-left + 1, -top + 1), text or " ", font=font, fill=fill)
        return sheet

    def _tree_kind_icon_rgba(self, kind: str) -> Image.Image:
        mode = ctk.get_appearance_mode()
        size = self._tree_icon_px()
        key = (kind, mode, size)
        cache = getattr(self, "_tree_kind_icons", None)
        if cache is None:
            self._tree_kind_icons = {}
            cache = self._tree_kind_icons
        hit = cache.get(key)
        if hit is not None:
            return hit
        icon = _draw_tree_kind_icon(kind, size, _tone(MUTED))
        cache[key] = icon
        return icon

    def _tree_text_photo(self, text: str, fill: str, kind: str = "file") -> ImageTk.PhotoImage:
        mode = ctk.get_appearance_mode()
        px = self._tree_label_px()
        icon_px = self._tree_icon_px()
        key = (text, fill, mode, self.ui_font, px, kind, icon_px)
        cache = getattr(self, "_tree_text_photos", None)
        if cache is None:
            self._tree_text_photos = {}
            cache = self._tree_text_photos
        hit = cache.get(key)
        if hit is not None:
            return hit
        icon = self._tree_kind_icon_rgba(kind)
        caption = self._tree_caption_rgba(text, fill)
        gap = max(4, px // 4)
        height = max(icon.height, caption.height)
        width = icon.width + gap + caption.width
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # Центр по общей высоте: иконка и подпись на одной оптической линии.
        sheet.paste(icon, (0, (height - icon.height) // 2), icon)
        sheet.paste(caption, (icon.width + gap, (height - caption.height) // 2), caption)
        photo = ImageTk.PhotoImage(sheet)
        cache[key] = photo
        if len(cache) > 800:
            self._tree_text_photos = {key: photo}
        return photo

    def _configure_git_tree_tags(self) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        try:
            tree.tag_configure("file")
            tree.tag_configure("dir")
            tree.tag_configure("git_modified")
            tree.tag_configure("git_untracked")
            tree.tag_configure("git_deleted")
            tree.tag_configure("git_dir_dirty")
        except tk.TclError:
            return

    def _tree_item_tags(self, rel: str, is_dir: bool) -> tuple[str, ...]:
        from project_agent.gitops import git_path_kind

        if is_dir:
            if rel == ".":
                if self._git_file_status or self._git_dirty_dirs:
                    return ("dir", "git_dir_dirty")
                return ("dir",)
            if rel in self._git_dirty_dirs or git_path_kind(self._git_file_status, rel):
                return ("dir", "git_dir_dirty")
            return ("dir",)
        kind = git_path_kind(self._git_file_status, rel)
        if kind == "modified":
            return ("file", "git_modified")
        if kind == "untracked":
            return ("file", "git_untracked")
        if kind == "deleted":
            return ("file", "git_deleted")
        return ("file",)

    def _tree_item_fill(self, tags: tuple[str, ...] | list[str] | set[str]) -> str:
        tagset = set(tags or ())
        if "git_deleted" in tagset:
            return _tone(GIT_DEL)
        if "git_untracked" in tagset:
            return _tone(GIT_NEW)
        if "git_modified" in tagset or "git_dir_dirty" in tagset:
            return _tone(GIT_MOD)
        return _tone(TEXT)

    def _tree_item_caption(self, name: str, is_dir: bool, tags: tuple[str, ...] | list[str]) -> str:
        # У dirty-папок точка справа через 5 пробелов (цвет имени — в PIL-подписи).
        if is_dir and "git_dir_dirty" in set(tags or ()):
            return f"{name}     ●"
        return name

    def _tree_item_image(self, name: str, is_dir: bool, tags: tuple[str, ...] | list[str]) -> ImageTk.PhotoImage:
        caption = self._tree_item_caption(name, is_dir, tags)
        kind = _tree_kind_for_name(name, is_dir)
        return self._tree_text_photo(caption, self._tree_item_fill(tags), kind=kind)

    def _tree_entry_name(self, iid: str) -> str:
        rel = tree_path_rel(iid)
        if rel == ".":
            if self.project is not None:
                return self.project.name or str(self.project)
            return "."
        return rel.rsplit("/", 1)[-1]

    def _apply_git_tree_tags(self) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return

        def walk(node: str) -> None:
            for child in tree.get_children(node):
                iid = str(child)
                if iid.startswith(_PENDING):
                    continue
                rel = tree_path_rel(iid)
                tags = set(tree.item(iid, "tags") or ())
                is_dir = rel == "." or "dir" in tags
                if "file" in tags:
                    is_dir = False
                try:
                    new_tags = self._tree_item_tags(rel, is_dir)
                    name = self._tree_entry_name(iid)
                    tree.item(
                        iid,
                        text="",
                        tags=new_tags,
                        image=self._tree_item_image(name, is_dir, new_tags),
                    )
                except tk.TclError:
                    continue
                if is_dir:
                    walk(iid)

        try:
            walk("")
        except tk.TclError:
            return

    def _style_tree(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        # Подписи файлов — цветные PIL-image (ttk tag foreground на Windows ненадёжен).
        self._clear_tree_label_cache()
        style.configure(
            "Project.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            borderwidth=0,
            # Межстрочный зазор −30% относительно прежнего (px+8).
            rowheight=max(self._tree_label_px() + 2, round(self._tree_label_px() + 8 * 0.7)),
            font=(self.ui_font, 14),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", _tone(SELECT))],
        )
        style.configure(
            "Chats.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            foreground=_tone(TEXT),
            borderwidth=0,
            rowheight=32,
            indent=0,
            font=(self.ui_font, 10),
        )
        style.layout("Chats.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Project.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout(
            "Project.Treeview.Item",
            [
                (
                    "Treeitem.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            ("Treeitem.indicator", {"side": "left", "sticky": ""}),
                            ("Treeitem.image", {"side": "left", "sticky": ""}),
                            ("Treeitem.text", {"side": "left", "sticky": ""}),
                        ],
                    },
                )
            ],
        )
        style.layout("Chats.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [("Treeitem.text", {"sticky": "nswe"})]})])
        style.map(
            "Chats.Treeview",
            background=[("selected", _tone(SELECT))],
            foreground=[("selected", _tone(TEXT))],
        )
        self._configure_git_tree_tags()

    def _refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        reopen = self._expanded_paths()
        self.tree.delete(*self.tree.get_children())
        if self.project is None or not self.project.is_dir():
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            self.tree.insert("", "end", text="Папка не выбрана")
            return
        self._reload_git_decorations(apply=False)
        root_tags = self._tree_item_tags(".", True)
        root_name = self.project.name or str(self.project)
        root_iid = tree_path_iid(".")
        self.tree.insert(
            "",
            "end",
            iid=root_iid,
            text="",
            open=True,
            tags=root_tags,
            image=self._tree_item_image(root_name, True, root_tags),
        )
        self._fill_node(root_iid, reopen)

    def _expanded_paths(self) -> set[str]:
        found: set[str] = set()

        def walk(node: str) -> None:
            for child in self.tree.get_children(node):
                child = str(child)
                if child.startswith(_PENDING):
                    continue
                if self.tree.item(child, "open"):
                    found.add(tree_path_rel(child))
                    walk(child)

        walk("")
        return found

    def _fill_node(self, iid: str, reopen: set[str]) -> None:
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        parent_rel = tree_path_rel(iid)
        try:
            entries, truncated = list_entries(self.project, parent_rel)
        except PathError:
            self.tree.insert(iid, "end", text="(не прочитано)")
            return
        for name, is_dir in entries:
            rel = name if parent_rel == "." else f"{parent_rel}/{name}"
            child_iid = tree_path_iid(rel)
            tags = self._tree_item_tags(rel, is_dir)
            self.tree.insert(
                iid,
                "end",
                iid=child_iid,
                text="",
                open=False,
                tags=tags,
                image=self._tree_item_image(name, is_dir, tags),
            )
            if not is_dir:
                continue
            if rel in reopen:
                self.tree.item(child_iid, open=True)
                self._fill_node(child_iid, reopen)
            else:
                self.tree.insert(child_iid, "end", iid=f"{_PENDING}{child_iid}", text="")
        if truncated:
            self.tree.insert(iid, "end", text="(список обрезан)")

    def _tree_open(self, _event=None) -> None:
        iid = str(self.tree.focus() or "")
        if not iid:
            selected = self.tree.selection()
            iid = str(selected[0]) if selected else ""
        if not iid or iid.startswith(_PENDING) or self.project is None:
            return
        children = self.tree.get_children(iid)
        if len(children) == 1 and str(children[0]).startswith(_PENDING):
            self._reload_git_decorations(apply=False)
            self._fill_node(iid, set())
        else:
            self._reload_git_decorations(apply=True)

    def _on_file_click(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected or self.project is None:
            return
        iid = str(selected[0])
        if "file" not in self.tree.item(iid, "tags"):
            return
        relative = tree_path_rel(iid)
        if self.editor_open and relative == self.editor_path:
            return
        self._open_file_at(relative, 1)

    def _refresh_git_badge(self) -> None:
        label = getattr(self, "git_label", None)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        if self.project is None:
            try:
                label.configure(text="", text_color=_tone(MUTED))
            except tk.TclError:
                pass
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            return
        text = self._reload_git_decorations(apply=True)
        try:
            dirty = bool(text) and not str(text).endswith(" · clean")
            label.configure(text=text, text_color=_tone(GIT_MOD if dirty else MUTED))
        except tk.TclError:
            return

    def _on_git_badge_click(self) -> None:
        if self.project is None or self.running:
            return
        try:
            from project_agent.gitops import GitError, git_status

            result = git_status(self.project)
            body = result.output or "(чисто)"
            self.write_chat(f"git status:\n{body}")
            from project_agent.gitops import dirty_ancestor_dirs, format_git_badge, parse_status_head, parse_status_paths

            self._git_file_status = parse_status_paths(result.output)
            self._git_dirty_dirs = dirty_ancestor_dirs(self._git_file_status)
            branch, modified, untracked = parse_status_head(result.output)
            badge = format_git_badge(branch, modified, untracked)
            label = getattr(self, "git_label", None)
            if label is not None:
                try:
                    dirty = bool(badge) and not str(badge).endswith(" · clean")
                    label.configure(text=badge, text_color=_tone(GIT_MOD if dirty else MUTED))
                except tk.TclError:
                    pass
            self._apply_git_tree_tags()
        except GitError as exc:
            self.write_chat(f"git: {exc}")
        except Exception as exc:
            self.write_chat(f"git: {exc}")

    def _reload_git_decorations(self, apply: bool = False) -> str:
        """Обновить кэш dirty-путей. Возвращает текст Git badge или ''."""
        self._git_file_status = {}
        self._git_dirty_dirs = set()
        if self.project is None:
            return ""
        try:
            from project_agent.gitops import (
                dirty_ancestor_dirs,
                format_git_badge,
                git_status,
                parse_status_head,
                parse_status_paths,
            )

            result = git_status(self.project)
            self._git_file_status = parse_status_paths(result.output)
            self._git_dirty_dirs = dirty_ancestor_dirs(self._git_file_status)
            branch, modified, untracked = parse_status_head(result.output)
            badge = format_git_badge(branch, modified, untracked)
        except Exception:
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            badge = ""
        if apply:
            self._apply_git_tree_tags()
        return badge

    def _restart_project_watch(self) -> None:
        self._stop_project_watch()
        if self.project is None or self._closing:
            return
        from project_agent.fswatch import HAS_WATCHDOG, ProjectWatcher

        if not HAS_WATCHDOG:
            return
        if self._fs_watcher is None:
            self._fs_watcher = ProjectWatcher()
        ok = self._fs_watcher.start(self.project, self._on_fs_change)
        if not ok:
            return

    def _stop_project_watch(self) -> None:
        job = self._watcher_job
        self._watcher_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        watcher = self._fs_watcher
        if watcher is not None:
            watcher.stop()

    def _on_fs_change(self) -> None:
        """Колбэк из потока watchdog → в UI-поток с debounce."""
        if self._closing:
            return
        try:
            self.after(0, self._schedule_watcher_refresh)
        except Exception:
            return

    def _schedule_watcher_refresh(self) -> None:
        if self._closing or self.project is None:
            return
        job = self._watcher_job
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._watcher_job = self.after(450, self._run_watcher_refresh)

    def _run_watcher_refresh(self) -> None:
        self._watcher_job = None
        if self._closing or self.project is None:
            return
        try:
            self._refresh_tree()
            self._refresh_git_badge()
            if self.editor_open:
                self._reload_clean_editor()
        except Exception:
            return
