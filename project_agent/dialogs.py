"""Модальные диалоги приложения.

Содержит ConfirmDialog, GitFileDiffDialog, TurnDiffDialog, NameDialog,
SaveChangesDialog и FontPickDialog — код перенесён из ``project_agent.app``
без изменения поведения.

Зависимости: только customtkinter, tkinter, ``ui_theme`` (цвета), ``ui_widgets``
(кнопки/иконки/шрифты) и ``diff_view`` (строки/метрики diff без Tk).
Импортировать ``project_agent.app`` отсюда ЗАПРЕЩЕНО — иначе цикл импортов.
Всё, что диалогам нужно от приложения, они берут через ``master`` (переданный
App): ``master.project``, ``master.running``, ``master.checkpoints``,
``master.reject_checkpoint_file(...)``, ``master.open_editor_draft(...)``,
``master._reload_clean_editor()``, ``master._refresh_tree()``,
``master._refresh_git_badge()``, ``master.set_status(...)``,
``master.write_chat(...)``, ``master.ui_font``, ``master.code_font``.

``ConfirmDialog`` — просмотр изменений перед записью: построчный diff с номерами
строк, метрики (``+N −M``, фрагменты, размеры), переключение фрагментов кликом по
``@@``, кнопки «Применить» / «Отклонить» / «Открыть в редакторе». ``result``:
False — отказ (в том числе переход в редактор), True — принять всё, str — текст,
собранный из принятых фрагментов.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from project_agent.config import DEFAULT_CODE_FONT, DEFAULT_UI_FONT
from project_agent.diff_view import (
    NUMBER_WIDTH,
    build_rows,
    change_stats,
    format_fragments,
    format_selection,
    format_sizes,
    format_stats,
    normalize_text,
    row_line,
)
from project_agent.ui_theme import (
    BORDER,
    DIFF_ADD,
    DIFF_ADD_BG,
    DIFF_DEL,
    DIFF_DEL_BG,
    DIFF_HUNK,
    DIFF_HUNK_BG,
    DIFF_PREVIEW_LINES,
    FIELD,
    INK,
    MUTED,
    SELECT,
    TEXT,
)
from project_agent.ui_widgets import _font_catalog, _tone, bind_wheel_scroll, quiet_button


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master,
        path: str,
        summary: str,
        detail: str = "",
        before: str | None = None,
        after: str | None = None,
    ) -> None:
        super().__init__(master)
        self.result: bool | str = False
        self._closed = False
        self._path = path
        self._detail = (detail or "").strip()
        self._diff_mode = before is not None and after is not None
        self._before = normalize_text(before) if before is not None else None
        self._after = normalize_text(after) if after is not None else None
        self._hunks = []
        self._accepted: dict[int, bool] = {}
        self._expanded = False
        self._box = None
        self._stats_bar = None
        self._choice_label = None
        self._expand_btn = None
        self._apply_btn = None
        self._row_count = 0
        self._line_hunks: dict[int, int] = {}
        if self._diff_mode:
            from project_agent.hunks import split_hunks

            self._hunks = split_hunks(self._before, self._after)
            self._accepted = {item.index: True for item in self._hunks}
            self._row_count = len(build_rows(self._before, self._after))
            if not self._detail:
                from project_agent.gitops import preview_unified

                self._detail = preview_unified(self._before, self._after, path)
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        code = getattr(master, "code_font", None) or DEFAULT_CODE_FONT
        self.title("Подтверждение")
        tall = bool(self._detail) or self._diff_mode
        self.geometry("780x600" if self._diff_mode else ("600x440" if tall else "520x220"))
        self.minsize(520, 240)
        self.resizable(True, True)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        row = 0
        ctk.CTkLabel(
            self, text=path, wraplength=700, justify="left", anchor="w", text_color=TEXT, font=(ui, 13)
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(16, 6))
        row += 1
        ctk.CTkLabel(
            self, text=summary, wraplength=700, justify="left", anchor="w", text_color=MUTED, font=(ui, 12)
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1
        if self._diff_mode:
            self._stats_bar = ctk.CTkFrame(self, fg_color="transparent")
            self._stats_bar.grid(row=row, column=0, sticky="ew", padx=16, pady=(2, 0))
            row += 1
            self._choice_label = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=(ui, 11))
            self._choice_label.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
            row += 1
        if tall:
            self.grid_rowconfigure(row, weight=1)
            self._box = ctk.CTkTextbox(
                self,
                fg_color=FIELD,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
                corner_radius=10,
                font=(code, 11),
                wrap="none",
            )
            self._box.grid(row=row, column=0, sticky="nsew", padx=16, pady=6)
            self._configure_tags()
            if self._diff_mode:
                self._box._textbox.bind("<Button-1>", self._on_diff_click, add="+")
            self._fill()
            row += 1
        if self._diff_mode:
            hint_row = ctk.CTkFrame(self, fg_color="transparent")
            hint_row.grid(row=row, column=0, sticky="ew", padx=16, pady=(2, 2))
            ctk.CTkLabel(
                hint_row,
                text="Клик по строке @@ переключает принять/отклонить · Ctrl+Enter — применить",
                anchor="w",
                text_color=MUTED,
                font=(ui, 11),
            ).pack(side="left")
            quiet_button(hint_row, "Ничего", self._reject_all, width=72, height=22).pack(side="right")
            quiet_button(hint_row, "Всё", self._accept_all, width=56, height=22).pack(side="right", padx=(0, 4))
            row += 1
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=row, column=0, pady=(8, 16))
        if self._truncated():
            self._expand_btn = quiet_button(bar, "Показать всё", self._expand_diff, width=140)
            self._expand_btn.pack(side="left", padx=8)
        if self._diff_mode and callable(getattr(master, "open_editor_draft", None)):
            quiet_button(bar, "Открыть в редакторе", self.open_editor, width=170).pack(side="left", padx=8)
        wordy = self._diff_mode or bool(self._detail)
        refuse_label = "Отклонить" if wordy else "Нет"
        accept_label = "Применить" if self._diff_mode else ("Принять" if self._detail else "Да")
        quiet_button(bar, refuse_label, self.refuse, width=120, mark="close").pack(side="left", padx=8)
        self._apply_btn = quiet_button(bar, accept_label, self.allow, width=130, primary=True, mark="check")
        self._apply_btn.pack(side="left", padx=8)
        if self._box is not None:
            bind_wheel_scroll(self._box._textbox, self)
        self._refresh_stats()
        self.protocol("WM_DELETE_WINDOW", self.refuse)
        self.bind("<Escape>", lambda _event: self.refuse())
        self.bind("<Control-Return>", lambda _event: self.allow())
        self.after(50, self.focus)

    def _configure_tags(self) -> None:
        inner = self._box._textbox
        inner.tag_configure("diff_num", foreground=_tone(MUTED))
        inner.tag_configure("diff_add", foreground=_tone(DIFF_ADD), background=_tone(DIFF_ADD_BG))
        inner.tag_configure("diff_del", foreground=_tone(DIFF_DEL), background=_tone(DIFF_DEL_BG))
        inner.tag_configure("diff_hunk", foreground=_tone(DIFF_HUNK), background=_tone(DIFF_HUNK_BG))
        inner.tag_configure("diff_off", foreground=_tone(MUTED))
        inner.tag_configure("diff_meta", foreground=_tone(MUTED))

    def _chosen(self) -> set[int]:
        return {index for index, ok in self._accepted.items() if ok}

    def _fill(self) -> None:
        if self._diff_mode:
            self._fill_rows()
        else:
            self._fill_text(self._preview_text())

    def _fill_rows(self) -> None:
        """Нарисовать предложение целиком: принятые фрагменты цветные, отклонённые приглушены."""
        if self._box is None:
            return
        limit = None if self._expanded else DIFF_PREVIEW_LINES
        rows = build_rows(self._before or "", self._after or "", accepted=self._chosen(), limit=limit)
        inner = self._box._textbox
        self._box.configure(state="normal")
        inner.delete("1.0", "end")
        self._line_hunks = {}
        for number, row in enumerate(rows, start=1):
            start = inner.index("end-1c")
            inner.insert("end", row_line(row) + "\n")
            end = inner.index("end-1c")
            if row.kind == "hunk" and row.hunk_index is not None:
                self._line_hunks[number] = row.hunk_index
            self._tag_row(inner, row, start, end)
        self._box.configure(state="disabled")

    def _on_diff_click(self, event) -> None:
        """Клик по строке ``@@`` переключает её фрагмент.

        Тег-биндинги в Text с ``state=disabled`` не вызываются, поэтому ловим
        событие на самом виджете и переводим координаты в номер строки. Строки
        фрагмента намеренно не кликабельны: иначе выделение текста мышью
        переключало бы фрагмент.
        """
        if not self._diff_mode or self._box is None:
            return
        try:
            position = self._box._textbox.index(f"@{int(event.x)},{int(event.y)}")
            number = int(str(position).split(".")[0])
        except (tk.TclError, ValueError, TypeError):
            return
        index = self._line_hunks.get(number)
        if index is not None:
            self._toggle_hunk(index)

    def _tag_row(self, inner, row, start, end) -> None:
        if row.kind == "hunk":
            inner.tag_add("diff_hunk", start, end)
            tag = f"hunk_{row.hunk_index}"
            inner.tag_add(tag, start, end)
            try:
                inner.tag_configure(tag, cursor="hand2")
            except tk.TclError:
                pass
            return
        if row.kind == "skip":
            inner.tag_add("diff_meta", start, end)
            return
        if row.kind in {"add", "del"} and not row.accepted:
            inner.tag_add("diff_off", start, end)
            return
        numbers_end = f"{start} + {NUMBER_WIDTH * 2 + 2} chars"
        inner.tag_add("diff_num", start, numbers_end)
        if row.kind == "add":
            inner.tag_add("diff_add", numbers_end, end)
        elif row.kind == "del":
            inner.tag_add("diff_del", numbers_end, end)

    def _fill_text(self, text: str) -> None:
        if self._box is None:
            return
        inner = self._box._textbox
        self._box.configure(state="normal")
        inner.delete("1.0", "end")
        for raw in text.splitlines():
            line = raw + "\n"
            start = inner.index("end-1c")
            inner.insert("end", line)
            end = inner.index("end-1c")
            if raw.startswith("+++") or raw.startswith("---"):
                inner.tag_add("diff_meta", start, end)
            elif raw.startswith("@@"):
                inner.tag_add("diff_hunk", start, end)
            elif raw.startswith("+"):
                inner.tag_add("diff_add", start, end)
            elif raw.startswith("-"):
                inner.tag_add("diff_del", start, end)
        self._box.configure(state="disabled")

    def _refresh_stats(self) -> None:
        if self._stats_bar is None or not self._diff_mode:
            return
        for child in self._stats_bar.winfo_children():
            child.destroy()
        stats = change_stats(self._before or "", self._after or "", self._chosen())
        ui = getattr(self.master, "ui_font", None) or DEFAULT_UI_FONT
        if stats.hunks == 0:
            ctk.CTkLabel(
                self._stats_bar,
                text=format_stats(stats),
                anchor="w",
                text_color=MUTED,
                font=(ui, 11),
            ).pack(side="left")
        else:
            ctk.CTkLabel(
                self._stats_bar, text=f"{stats.kind} · ", anchor="w", text_color=MUTED, font=(ui, 11)
            ).pack(side="left")
            ctk.CTkLabel(
                self._stats_bar, text=f"+{stats.plus}", anchor="w", text_color=DIFF_ADD, font=(ui, 11)
            ).pack(side="left")
            rest = f" −{stats.minus} · {format_fragments(stats.hunks)} · {format_sizes(stats)}"
            ctk.CTkLabel(self._stats_bar, text=rest, anchor="w", text_color=MUTED, font=(ui, 11)).pack(side="left")
        if self._choice_label is not None:
            try:
                self._choice_label.configure(text=format_selection(stats))
            except tk.TclError:
                pass
        if self._apply_btn is not None:
            state = "disabled" if stats.nothing else "normal"
            try:
                self._apply_btn.configure(state=state)
            except tk.TclError:
                pass

    def _toggle_hunk(self, index: int) -> None:
        """Переключить фрагмент: предложение агента остаётся видимым целиком."""
        if index not in self._accepted:
            return
        self._accepted[index] = not self._accepted[index]
        self._fill_rows()
        self._refresh_stats()

    def _accept_all(self) -> None:
        for index in list(self._accepted):
            self._accepted[index] = True
        self._fill_rows()
        self._refresh_stats()

    def _reject_all(self) -> None:
        for index in list(self._accepted):
            self._accepted[index] = False
        self._fill_rows()
        self._refresh_stats()

    def _detail_lines(self) -> list[str]:
        return self._detail.splitlines()

    def _truncated(self) -> bool:
        if self._expanded:
            return False
        if self._diff_mode:
            return self._row_count > DIFF_PREVIEW_LINES
        return len(self._detail_lines()) > DIFF_PREVIEW_LINES

    def _preview_text(self) -> str:
        lines = self._detail_lines()
        if self._expanded or len(lines) <= DIFF_PREVIEW_LINES:
            return self._detail
        return "\n".join(lines[:DIFF_PREVIEW_LINES] + ["…"])

    def _expand_diff(self) -> None:
        if self._expanded:
            return
        self._expanded = True
        self._fill()
        if self._expand_btn is not None:
            self._expand_btn.configure(state="disabled")
        try:
            self.geometry("900x680")
        except tk.TclError:
            pass

    def open_editor(self) -> None:
        """Отдать предложение в панель редактора как несохранённый черновик (без записи)."""
        if self._closed or not self._diff_mode:
            return
        from project_agent.hunks import merge_hunks

        opener = getattr(self.master, "open_editor_draft", None)
        if not callable(opener):
            return
        chosen = self._chosen()
        if not chosen or len(chosen) == len(self._hunks):
            text = self._after or ""
        else:
            text = merge_hunks(self._before or "", self._after or "", chosen)
        if not opener(self._path, self._before or "", text):
            try:
                self.grab_set()
            except tk.TclError:
                pass
            return
        self._closed = True
        self.result = False
        message = "Открыто в редакторе (черновик), запись отменена"
        for notify in ("write_chat", "set_status"):
            handler = getattr(self.master, notify, None)
            if callable(handler):
                try:
                    handler(message)
                except Exception:
                    pass
        self.grab_release()
        self.destroy()

    def allow(self) -> None:
        if self._closed:
            return
        if self._diff_mode and self._hunks:
            chosen = self._chosen()
            if not chosen:
                return
            if len(chosen) == len(self._hunks):
                self.result = True
            else:
                from project_agent.hunks import merge_hunks

                self.result = merge_hunks(self._before or "", self._after or "", chosen)
        else:
            self.result = True
        self._closed = True
        self.grab_release()
        self.destroy()

    def refuse(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = False
        self.grab_release()
        self.destroy()


class GitFileDiffDialog(ctk.CTkToplevel):
    """Просмотр git diff одного файла (ПКМ в дереве)."""

    def __init__(self, master, relative: str) -> None:
        super().__init__(master)
        self._master = master
        self._relative = relative
        self._closed = False
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        code = getattr(master, "code_font", None) or DEFAULT_CODE_FONT
        self.title(f"Diff · {relative}")
        self.geometry("720x520")
        self.minsize(480, 320)
        self.resizable(True, True)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self,
            text=relative,
            anchor="w",
            text_color=TEXT,
            font=(ui, 13),
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self._box = ctk.CTkTextbox(
            self,
            fg_color=FIELD,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=10,
            font=(code, 11),
            wrap="none",
        )
        self._box.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        inner = self._box._textbox
        inner.tag_configure("diff_add", foreground=_tone(DIFF_ADD))
        inner.tag_configure("diff_del", foreground=_tone(DIFF_DEL))
        inner.tag_configure("diff_hunk", foreground=_tone(DIFF_HUNK))
        inner.tag_configure("diff_meta", foreground=_tone(MUTED))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, pady=(4, 14))
        quiet_button(bar, "Обновить", self._reload, width=100).pack(side="left", padx=6)
        quiet_button(bar, "Закрыть", self._close, width=100, mark="close").pack(side="left", padx=6)
        bind_wheel_scroll(self._box._textbox, self)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._reload()
        self.after(50, self.focus)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.destroy()

    def _fill_diff(self, text: str) -> None:
        inner = self._box._textbox
        self._box.configure(state="normal")
        inner.delete("1.0", "end")
        for raw in (text or "").splitlines():
            line = raw + "\n"
            start = inner.index("end-1c")
            inner.insert("end", line)
            end = inner.index("end-1c")
            if raw.startswith("+++") or raw.startswith("---"):
                inner.tag_add("diff_meta", start, end)
            elif raw.startswith("@@"):
                inner.tag_add("diff_hunk", start, end)
            elif raw.startswith("+"):
                inner.tag_add("diff_add", start, end)
            elif raw.startswith("-"):
                inner.tag_add("diff_del", start, end)
        self._box.configure(state="disabled")

    def _reload(self) -> None:
        from project_agent.gitops import GitError, working_tree_diff

        app = self._master
        if app.project is None:
            self._fill_diff("(нет проекта)")
            return
        try:
            text = working_tree_diff(app.project, self._relative)
        except GitError as exc:
            self._fill_diff(str(exc))
            return
        except Exception as exc:
            self._fill_diff(f"Diff недоступен: {exc}")
            return
        self._fill_diff(text or "(пусто)")


class TurnDiffDialog(ctk.CTkToplevel):
    """Сводный diff хода (P1.7) + отклонение хунка/файла."""

    def __init__(self, master, checkpoint_id: str) -> None:
        super().__init__(master)
        self._master = master
        self._checkpoint_id = checkpoint_id
        self._closed = False
        self._hunks_by_file: dict[str, list] = {}
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        code = getattr(master, "code_font", None) or DEFAULT_CODE_FONT
        self.title("Diff хода")
        self.geometry("720x560")
        self.minsize(520, 360)
        self.resizable(True, True)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(
            self,
            text="Сводка правок хода (снимок → сейчас)",
            anchor="w",
            text_color=TEXT,
            font=(ui, 13),
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        self._status = ctk.CTkLabel(self, text="", anchor="w", text_color=MUTED, font=(ui, 11))
        self._status.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        self._box = ctk.CTkTextbox(
            self,
            fg_color=FIELD,
            text_color=TEXT,
            border_width=1,
            border_color=BORDER,
            corner_radius=10,
            font=(code, 11),
            wrap="none",
        )
        self._box.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        inner = self._box._textbox
        inner.tag_configure("diff_add", foreground=_tone(DIFF_ADD))
        inner.tag_configure("diff_del", foreground=_tone(DIFF_DEL))
        inner.tag_configure("diff_hunk", foreground=_tone(DIFF_HUNK))
        inner.tag_configure("diff_meta", foreground=_tone(MUTED))
        self._actions = ctk.CTkScrollableFrame(self, fg_color="transparent", height=120)
        self._actions.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 4))
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, pady=(4, 14))
        quiet_button(bar, "Обновить", self._reload, width=100).pack(side="left", padx=6)
        quiet_button(bar, "Закрыть", self._close, width=100, mark="close").pack(side="left", padx=6)
        bind_wheel_scroll(self._box._textbox, self)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._reload()
        self.after(50, self.focus)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.destroy()

    def _reload(self) -> None:
        from project_agent.checkpoints import restorable_paths
        from project_agent.hunks import build_turn_diff, checkpoint_file_texts, split_hunks

        app = self._master
        checkpoint = app.checkpoints.get(self._checkpoint_id)
        if checkpoint is None or app.project is None:
            self._status.configure(text="Снимок недоступен")
            return
        text = build_turn_diff(app.project, checkpoint.files)
        self._fill_diff(text)
        for child in self._actions.winfo_children():
            child.destroy()
        self._hunks_by_file = {}
        paths = restorable_paths(checkpoint)
        self._status.configure(text=f"Файлов: {len(paths)}")
        for relative in paths:
            snap = (checkpoint.files or {}).get(relative) or {}
            before, after, note = checkpoint_file_texts(app.project, relative, snap)
            block = ctk.CTkFrame(self._actions, fg_color="transparent")
            block.pack(fill="x", pady=3)
            row = ctk.CTkFrame(block, fg_color="transparent")
            row.pack(fill="x")
            ctk.CTkLabel(row, text=relative, anchor="w", text_color=TEXT, width=280).pack(side="left", padx=(4, 8))
            quiet_button(
                row,
                "Отклонить файл",
                lambda rel=relative: self._reject_file(rel),
                width=120,
                height=24,
            ).pack(side="left", padx=2)
            if note or before == after:
                continue
            hunks = split_hunks(before, after)
            self._hunks_by_file[relative] = hunks
            if not hunks:
                continue
            hunk_row = ctk.CTkFrame(block, fg_color="transparent")
            hunk_row.pack(fill="x", padx=(12, 0))
            ctk.CTkLabel(hunk_row, text="Хунки:", anchor="w", text_color=MUTED, width=52).pack(side="left")
            for item in hunks:
                quiet_button(
                    hunk_row,
                    f"H{item.index + 1}✗",
                    lambda rel=relative, index=item.index: self._reject_hunk(rel, index),
                    width=52,
                    height=24,
                ).pack(side="left", padx=2)

    def _fill_diff(self, text: str) -> None:
        inner = self._box._textbox
        self._box.configure(state="normal")
        inner.delete("1.0", "end")
        for raw in (text or "").splitlines():
            line = raw + "\n"
            start = inner.index("end-1c")
            inner.insert("end", line)
            end = inner.index("end-1c")
            if raw.startswith("+++") or raw.startswith("---") or raw.startswith("#"):
                inner.tag_add("diff_meta", start, end)
            elif raw.startswith("@@"):
                inner.tag_add("diff_hunk", start, end)
            elif raw.startswith("+"):
                inner.tag_add("diff_add", start, end)
            elif raw.startswith("-"):
                inner.tag_add("diff_del", start, end)
        self._box.configure(state="disabled")

    def _reject_file(self, relative: str) -> None:
        app = self._master
        app.reject_checkpoint_file(self._checkpoint_id, relative)
        if app.checkpoints.get(self._checkpoint_id) is None:
            self._close()
            return
        self._reload()

    def _reject_hunk(self, relative: str, index: int) -> None:
        from project_agent.hunks import checkpoint_file_texts, merge_hunks, split_hunks
        from project_agent.paths import PathError, resolve_inside

        app = self._master
        if app.project is None or app.running:
            return
        checkpoint = app.checkpoints.get(self._checkpoint_id)
        if checkpoint is None:
            self._status.configure(text="Снимок недоступен")
            return
        snap = (checkpoint.files or {}).get(relative)
        if snap is None:
            return
        before, after, note = checkpoint_file_texts(app.project, relative, snap)
        if note:
            self._status.configure(text=note)
            return
        hunks = split_hunks(before, after)
        if index < 0 or index >= len(hunks):
            return
        accepted = {item.index for item in hunks if item.index != index}
        if not accepted:
            app.reject_checkpoint_file(self._checkpoint_id, relative)
            if app.checkpoints.get(self._checkpoint_id) is None:
                self._close()
                return
            self._reload()
            return
        merged = merge_hunks(before, after, accepted)
        try:
            full = resolve_inside(app.project, relative)
        except PathError:
            self._status.configure(text="Путь вне проекта")
            return
        try:
            if not snap.get("existed") and merged == "":
                if full.exists() and full.is_file():
                    full.unlink()
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(merged, encoding="utf-8", newline="\n")
        except OSError as exc:
            self._status.configure(text=str(exc))
            return
        app._reload_clean_editor()
        app._refresh_tree()
        app._refresh_git_badge()
        app.set_status(f"Отклонён хунк H{index + 1}: {relative}")
        self._reload()


class NameDialog(ctk.CTkToplevel):
    def __init__(self, master, initial: str = "", title: str = "Профиль") -> None:
        super().__init__(master)
        self.result = ""
        self._closed = False
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        self.title(title)
        self.geometry("420x168")
        self.resizable(False, False)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        ctk.CTkLabel(self, text="Имя профиля", anchor="w", text_color=MUTED, font=(ui, 12)).pack(
            fill="x", padx=16, pady=(16, 4)
        )
        self.entry = ctk.CTkEntry(
            self,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            height=36,
            corner_radius=12,
            font=(ui, 12),
        )
        self.entry.pack(fill="x", padx=16, pady=4)
        if initial:
            self.entry.insert(0, initial)
            self.entry.select_range(0, "end")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=12)
        quiet_button(row, "Отмена", self._cancel, width=120, mark="close", family=ui).pack(side="left", padx=8)
        quiet_button(row, "Сохранить", self._ok, width=140, primary=True, mark="save", family=ui).pack(
            side="left", padx=8
        )
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.entry.bind("<Return>", lambda _event: self._ok())
        self.after(50, self.entry.focus)

    def _ok(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = self.entry.get()
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = ""
        self.grab_release()
        self.destroy()


class SaveChangesDialog(ctk.CTkToplevel):
    """Да = сохранить, Нет = закрыть без записи, None = отмена."""

    def __init__(self, master, path: str) -> None:
        super().__init__(master)
        self.result: bool | None = None
        self._closed = False
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        self.title("Сохранить изменения")
        self.geometry("440x180")
        self.resizable(False, False)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        ctk.CTkLabel(
            self, text=path, wraplength=400, justify="left", anchor="w", text_color=TEXT, font=(ui, 13)
        ).pack(fill="x", padx=16, pady=(16, 6))
        ctk.CTkLabel(
            self,
            text="Сохранить изменения?",
            wraplength=400,
            justify="left",
            anchor="w",
            text_color=MUTED,
            font=(ui, 12),
        ).pack(fill="x", padx=16, pady=4)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=14)
        quiet_button(row, "Не сохранять", self._discard, width=120, family=ui).pack(side="left", padx=6)
        quiet_button(row, "Отмена", self._cancel, width=100, mark="close", family=ui).pack(side="left", padx=6)
        quiet_button(row, "Сохранить", self._save, width=120, primary=True, mark="check", family=ui).pack(
            side="left", padx=6
        )
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.after(50, self.focus)

    def _save(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = True
        self.grab_release()
        self.destroy()

    def _discard(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = False
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = None
        self.grab_release()
        self.destroy()


class FontPickDialog(ctk.CTkToplevel):
    _HEAD_CYR = "— С кириллицей —"
    _HEAD_LAT = "— Без кириллицы —"

    def __init__(self, master, current: str = "") -> None:
        super().__init__(master)
        self.result = ""
        self._closed = False
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        self.title("Шрифт")
        self.geometry("420x480")
        self.minsize(360, 360)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        ctk.CTkLabel(self, text="Выберите семейство", anchor="w", text_color=MUTED, font=(ui, 12)).pack(
            fill="x", padx=16, pady=(16, 4)
        )
        frame = ctk.CTkFrame(self, fg_color=FIELD, corner_radius=12, border_width=1, border_color=BORDER)
        frame.pack(fill="both", expand=True, padx=16, pady=4)
        self.listbox = tk.Listbox(
            frame,
            activestyle="dotbox",
            borderwidth=0,
            highlightthickness=0,
            bg=_tone(FIELD),
            fg=_tone(TEXT),
            selectbackground=_tone(SELECT),
            selectforeground=_tone(TEXT),
            font=(ui, 12),
        )
        scroll = tk.Scrollbar(frame, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        with_cyr, without, _cyr = _font_catalog(master)
        self._items: list[str] = []
        self._append_section(self._HEAD_CYR, with_cyr)
        self._append_section(self._HEAD_LAT, without)
        current = str(current or "").strip()
        if current:
            try:
                index = self._items.index(current)
                self.listbox.selection_set(index)
                self.listbox.see(index)
            except ValueError:
                pass
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=12)
        quiet_button(row, "Отмена", self._cancel, width=120, mark="close", family=ui).pack(side="left", padx=8)
        quiet_button(row, "Выбрать", self._ok, width=140, primary=True, mark="check", family=ui).pack(
            side="left", padx=8
        )
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.listbox.bind("<Return>", lambda _event: self._ok())
        self.listbox.bind("<Double-Button-1>", lambda _event: self._ok())
        bind_wheel_scroll(self.listbox, self)
        self.after(50, self.listbox.focus_set)

    def _append_section(self, title: str, names: list[str]) -> None:
        if not names:
            return
        self.listbox.insert("end", title)
        self._items.append(title)
        for name in names:
            self.listbox.insert("end", name)
            self._items.append(name)

    def _selected(self) -> str:
        selection = self.listbox.curselection()
        if not selection:
            return ""
        value = self._items[int(selection[0])]
        if value in (self._HEAD_CYR, self._HEAD_LAT):
            return ""
        return value

    def _ok(self) -> None:
        if self._closed:
            return
        name = self._selected()
        if not name:
            return
        self._closed = True
        self.result = name
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = ""
        self.grab_release()
        self.destroy()
