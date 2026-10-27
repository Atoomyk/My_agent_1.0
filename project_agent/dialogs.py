"""Модальные диалоги приложения.

Содержит ConfirmDialog, GitFileDiffDialog, TurnDiffDialog, NameDialog,
SaveChangesDialog и FontPickDialog — код перенесён из ``project_agent.app``
без изменения поведения.

Зависимости: только customtkinter, tkinter, ``ui_theme`` (цвета), ``ui_widgets``
(кнопки/иконки/шрифты). Импортировать ``project_agent.app`` отсюда ЗАПРЕЩЕНО —
иначе цикл импортов. Всё, что диалогам нужно от приложения, они берут через
``master`` (переданный App): ``master.project``, ``master.running``,
``master.checkpoints``, ``master.reject_checkpoint_file(...)``,
``master._reload_clean_editor()``, ``master._refresh_tree()``,
``master._refresh_git_badge()``, ``master.set_status(...)``,
``master.ui_font``, ``master.code_font``.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from project_agent.config import DEFAULT_CODE_FONT, DEFAULT_UI_FONT
from project_agent.ui_theme import (
    BORDER,
    DIFF_ADD,
    DIFF_DEL,
    DIFF_HUNK,
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
        self._detail = (detail or "").strip()
        self._expanded = False
        self._box = None
        self._expand_btn = None
        self._hunk_btns: dict[int, ctk.CTkButton] = {}
        self._path = path
        self._before = before
        self._after = after
        self._hunks = []
        self._accepted: dict[int, bool] = {}
        if before is not None and after is not None:
            from project_agent.hunks import split_hunks

            self._hunks = split_hunks(before, after)
            self._accepted = {item.index: True for item in self._hunks}
            if not self._detail:
                from project_agent.gitops import preview_unified

                self._detail = preview_unified(before, after, path)
        ui = getattr(master, "ui_font", None) or DEFAULT_UI_FONT
        code = getattr(master, "code_font", None) or DEFAULT_CODE_FONT
        self.title("Подтверждение")
        hunk_ui = len(self._hunks) > 1
        tall = bool(self._detail) or hunk_ui
        self.geometry("640x520" if hunk_ui else ("560x420" if tall else "520x220"))
        self.minsize(480, 200)
        self.resizable(True, True)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        row = 0
        ctk.CTkLabel(
            self, text=path, wraplength=560, justify="left", anchor="w", text_color=TEXT, font=(ui, 13)
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(16, 8))
        row += 1
        ctk.CTkLabel(
            self, text=summary, wraplength=560, justify="left", anchor="w", text_color=MUTED, font=(ui, 12)
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=4)
        row += 1
        if hunk_ui:
            hint = ctk.CTkLabel(
                self,
                text="Хунки: клик переключает принять/отклонить. Принять запишет выбранные.",
                wraplength=560,
                justify="left",
                anchor="w",
                text_color=MUTED,
                font=(ui, 11),
            )
            hint.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
            row += 1
            hunk_wrap = ctk.CTkScrollableFrame(self, fg_color="transparent", height=72)
            hunk_wrap.grid(row=row, column=0, sticky="ew", padx=12, pady=2)
            row += 1
            for item in self._hunks:
                btn = quiet_button(
                    hunk_wrap,
                    self._hunk_caption(item.index),
                    lambda index=item.index: self._toggle_hunk(index),
                    width=520,
                    height=24,
                )
                btn.pack(anchor="w", pady=1)
                self._hunk_btns[item.index] = btn
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
            self._box.grid(row=row, column=0, sticky="nsew", padx=16, pady=8)
            inner = self._box._textbox
            inner.tag_configure("diff_add", foreground=_tone(DIFF_ADD))
            inner.tag_configure("diff_del", foreground=_tone(DIFF_DEL))
            inner.tag_configure("diff_hunk", foreground=_tone(DIFF_HUNK))
            inner.tag_configure("diff_meta", foreground=_tone(MUTED))
            self._fill_diff(self._preview_text())
            row += 1
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=row, column=0, pady=(8, 16))
        if tall and self._is_truncated() and not hunk_ui:
            self._expand_btn = quiet_button(bar, "Показать всё", self._expand_diff, width=140)
            self._expand_btn.pack(side="left", padx=8)
        refuse_label = "Отклонить" if self._detail or self._hunks else "Нет"
        accept_label = "Принять" if self._detail or self._hunks else "Да"
        quiet_button(bar, refuse_label, self.refuse, width=120, mark="close").pack(side="left", padx=8)
        quiet_button(bar, accept_label, self.allow, width=120, primary=True, mark="check").pack(side="left", padx=8)
        if self._box is not None:
            bind_wheel_scroll(self._box._textbox, self)
        self.protocol("WM_DELETE_WINDOW", self.refuse)
        self.bind("<Escape>", lambda _event: self.refuse())
        self.after(50, self.focus)

    def _hunk_caption(self, index: int) -> str:
        accepted = self._accepted.get(index, True)
        mark = "✓" if accepted else "✗"
        for item in self._hunks:
            if item.index == index:
                return f"{mark} {item.label(64)}"
        return f"{mark} H{index + 1}"

    def _toggle_hunk(self, index: int) -> None:
        self._accepted[index] = not self._accepted.get(index, True)
        btn = self._hunk_btns.get(index)
        if btn is not None:
            try:
                btn.configure(text=self._hunk_caption(index))
            except tk.TclError:
                pass
        if self._before is not None and self._after is not None:
            from project_agent.gitops import preview_unified
            from project_agent.hunks import merge_hunks

            merged = merge_hunks(self._before, self._after, {i for i, ok in self._accepted.items() if ok})
            self._detail = preview_unified(self._before, merged, self._path or "file")
            self._expanded = True
            self._fill_diff(self._detail)

    def _detail_lines(self) -> list[str]:
        return self._detail.splitlines()

    def _is_truncated(self) -> bool:
        return (not self._expanded) and len(self._detail_lines()) > DIFF_PREVIEW_LINES

    def _preview_text(self) -> str:
        lines = self._detail_lines()
        if self._expanded or len(lines) <= DIFF_PREVIEW_LINES:
            return self._detail
        return "\n".join(lines[:DIFF_PREVIEW_LINES] + ["…"])

    def _fill_diff(self, text: str) -> None:
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

    def _expand_diff(self) -> None:
        if self._expanded or self._box is None:
            return
        self._expanded = True
        self._fill_diff(self._detail)
        if self._expand_btn is not None:
            self._expand_btn.configure(state="disabled")
        try:
            self.geometry("640x520")
        except tk.TclError:
            pass

    def allow(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._hunks and self._before is not None and self._after is not None:
            chosen = {index for index, ok in self._accepted.items() if ok}
            if not chosen:
                self.result = False
            elif len(chosen) == len(self._hunks):
                self.result = True
            else:
                from project_agent.hunks import merge_hunks

                self.result = merge_hunks(self._before, self._after, chosen)
        else:
            self.result = True
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
