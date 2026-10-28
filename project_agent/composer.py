"""Композер запроса: поле ввода, подсказка, вложения, чипы и popup «@».

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к нижней карточке ввода:
построение виджетов (рамка ``bottom``, карточка, ``task``, подсказка, строка
чипов вложений, кнопки «плюс» и «отправить»), подбор высоты поля
(``_fit_composer``/``_task_needed_height``), placeholder, вложения (файлы и
папки проекта, правила, изображения из буфера), чипы вложений и popup-список
путей по ``@``.

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``center``, ``bottom``, ``card``,
``task``, ``placeholder``, ``attach_row``, ``image_button``, ``send_button``,
``footer``, ``project``, ``attached``/``context_files``/``context_dirs``,
``_at_popup``/``_at_list``/``_at_start``, ``_task_height``/``_task_width``/
``_chip_width``/``_composer_pad``/``_fit_composer_job``/``_window_h``,
``_closing``, ``_font``/``_px_font``/``_measure``, ``write_chat``,
``set_status``. Поэтому код оформлен миксином ``ComposerMixin``: методы
остаются методами ``App`` (``class App(..., ComposerMixin, ctk.CTk)``), но
живут в отдельном файле.

Константы ``_CHIP_ROWS``, ``_TASK_MIN_H``, ``_COMPOSER_MAX_RATIO`` переехали
сюда вместе с кодом; ``_TASK_MIN_H`` дополнительно импортируется обратно в
``app`` — его читает ``App.__init__``.

Порядок вызовов при старте: ``_build_composer_panel()`` — виджеты (вызывается
из ``App._build`` на том же месте, где раньше создавался блок). Метод
возвращает созданную рамку ``bottom``: футер, который строится следом в
``_build``, кладётся поверх неё.
"""

from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont

import customtkinter as ctk

from project_agent.clipboard import apply_layout_clipboard, clipboard_action
from project_agent.context_attach import (
    MAX_CONTEXT_DIRS,
    MAX_CONTEXT_FILES,
    at_token_at_end,
    find_at_targets,
    save_clipboard_image,
)
from project_agent.paths import PathError, relative_posix, resolve_inside
from project_agent.rules import rule_chip_text
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    FIELD,
    HINT,
    INK,
    MUTED,
    PANEL,
    SELECT,
    TEXT,
)
from project_agent.ui_widgets import icon_button, _tone

_CHIP_ROWS = 2
_TASK_MIN_H = 56
_COMPOSER_MAX_RATIO = 1 / 3


class ComposerMixin:
    """Виджеты и поведение композера; требует виджетов ``App``."""

    def _build_composer_panel(self):
        bottom = ctk.CTkFrame(self.center, fg_color=INK, corner_radius=0)
        self.bottom = bottom
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(0, weight=0)
        bottom.bind("<Configure>", self._center_composer, add="+")
        card = ctk.CTkFrame(bottom, fg_color=FIELD, corner_radius=16, border_width=1, border_color=BORDER)
        self.card = card
        card.grid(row=0, column=0, sticky="ew", padx=24, pady=(0, 0))
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(1, weight=0)
        self.attach_row = ctk.CTkFrame(card, fg_color="transparent")
        self.attach_row.grid(row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 0))
        self.attach_row.grid_remove()
        self.attach_row.bind("<Configure>", self._on_attach_resize, add="+")
        self.task = ctk.CTkTextbox(
            card,
            height=_TASK_MIN_H,
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=self._font(13),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.task.grid(row=1, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 0))
        self.task.bind("<Return>", self._send_key)
        self.task.bind("<KP_Enter>", self._send_key)
        self.task.bind("<Control-Return>", self._send_key)
        inner = self.task._textbox
        inner.bind("<Control-KeyPress>", self._on_task_clipboard, add="+")
        inner.bind("<<Paste>>", self._on_task_paste_event, add="+")
        self.placeholder = tk.Label(
            inner,
            text="Спросите что угодно…",
            anchor="w",
            bd=0,
            padx=0,
            pady=0,
            font=self._font(11),
            fg=_tone(HINT),
            bg=_tone(FIELD),
            cursor="xterm",
            takefocus=0,
        )
        self.placeholder.bind("<Button-1>", self._focus_task)
        self.card.bind("<Button-1>", self._focus_task, add="+")
        self.task.bind("<Button-1>", self._focus_task, add="+")
        inner.bind("<FocusIn>", self._task_focus_in, add="+")
        inner.bind("<FocusOut>", self._task_focus_out, add="+")
        inner.bind("<<Modified>>", self._on_task_modified, add="+")
        inner.bind("<Configure>", self._on_task_configure, add="+")
        self._sync_placeholder()
        self.image_button = icon_button(card, "plus", self._attach_menu, size=32)
        self.image_button.grid(row=2, column=0, padx=(8, 4), pady=(2, 6), sticky="w")
        self.send_button = icon_button(card, "send", self._send_or_stop, size=32)
        self.send_button.grid(row=2, column=2, padx=(4, 8), pady=(2, 6), sticky="e")
        inner.bind("<KeyRelease>", self._on_task_key, add="+")
        inner.bind("<Escape>", self._hide_at_popup, add="+")
        inner.bind("<Down>", self._at_move, add="+")
        inner.bind("<Up>", self._at_move, add="+")
        inner.bind("<Tab>", self._at_accept_key, add="+")
        return self.bottom

    def _center_composer(self, event=None) -> None:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        width = event.width if event is not None else self.bottom.winfo_width()
        pad = round(self._column_pad(width, round(24 * scale)) / scale)
        if getattr(self, "_composer_pad", None) == pad:
            return
        self._composer_pad = pad
        self.card.grid_configure(padx=pad)
        self.footer.grid_configure(padx=pad)

    def _focus_task(self, _event=None):
        if not hasattr(self, "task"):
            return None
        inner = self.task._textbox
        try:
            self.placeholder.place_forget()
            inner.focus_set()
            if not self.task.get("1.0", "end-1c"):
                inner.mark_set("insert", "1.0")
        except tk.TclError:
            return None
        return None

    def _task_focus_in(self, _event=None) -> None:
        try:
            self.placeholder.place_forget()
        except tk.TclError:
            return

    def _task_focus_out(self, _event=None) -> None:
        self.after(10, self._sync_placeholder)
        self.after(120, self._hide_at_if_unfocused)

    def _hide_at_if_unfocused(self) -> None:
        if not self._at_popup_alive():
            return
        try:
            focus = self.focus_get()
        except tk.TclError:
            focus = None
        if focus is self._at_list:
            return
        self._hide_at_popup()

    def _on_task_modified(self, _event=None) -> None:
        inner = self.task._textbox
        try:
            inner.edit_modified(False)
        except tk.TclError:
            return
        if self.task.get("1.0", "end-1c"):
            self.placeholder.place_forget()
        else:
            self._sync_placeholder()
        self._schedule_fit_composer()

    def _on_task_configure(self, event=None) -> None:
        if event is None:
            self._schedule_fit_composer()
            return
        width = int(getattr(event, "width", 0) or 0)
        if width < 40 or width == self._task_width:
            return
        self._task_width = width
        self._schedule_fit_composer()

    def _schedule_fit_composer(self) -> None:
        if self._closing:
            return
        job = self._fit_composer_job
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._fit_composer_job = self.after(30, self._fit_composer)
        except tk.TclError:
            self._fit_composer_job = None

    def _task_needed_height(self) -> int:
        inner = self.task._textbox
        try:
            text = inner.get("1.0", "end-1c")
        except tk.TclError:
            return _TASK_MIN_H
        font = tkfont.Font(font=self._px_font(13))
        line_h = max(14, int(font.metrics("linespace")))
        try:
            width = max(inner.winfo_width() - 8, 120)
        except tk.TclError:
            width = 120
        lines = 0
        for paragraph in text.split("\n"):
            if not paragraph:
                lines += 1
                continue
            span = max(1, math.ceil(font.measure(paragraph) / width))
            lines += span
        lines = max(1, lines)
        return max(_TASK_MIN_H, lines * line_h + 14)

    def _fit_composer(self) -> None:
        self._fit_composer_job = None
        if self._closing or not hasattr(self, "task"):
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            window_h = max(int(self.winfo_height()), 400)
        except tk.TclError:
            return
        max_bottom = max(160, int(window_h * _COMPOSER_MAX_RATIO))
        try:
            footer_h = self.footer.winfo_reqheight() if self.footer.winfo_ismapped() else 36
        except tk.TclError:
            footer_h = 36
        try:
            attach_h = self.attach_row.winfo_reqheight() if self.attach_row.winfo_ismapped() else 0
        except tk.TclError:
            attach_h = 0
        # ряд кнопок + поля карточки + футер
        chrome = footer_h + attach_h + 48
        max_task = max(_TASK_MIN_H, max_bottom - chrome)
        needed = self._task_needed_height()
        height = min(max(needed, _TASK_MIN_H), max_task)
        if height == self._task_height:
            return
        self._task_height = height
        try:
            self.task.configure(height=height)
        except tk.TclError:
            return
        self.after(0, self._sync_placeholder)

    def _sync_placeholder(self) -> None:
        if not hasattr(self, "task") or not hasattr(self, "placeholder"):
            return
        try:
            empty = not self.task.get("1.0", "end-1c")
            focused = self.focus_get() is self.task._textbox
        except tk.TclError:
            return
        if empty and not focused:
            self.placeholder.place(x=4, y=4)
        else:
            self.placeholder.place_forget()

    def _attach_menu(self) -> None:
        menu = tk.Menu(
            self,
            tearoff=0,
            bd=0,
            bg=_tone(PANEL),
            fg=_tone(TEXT),
            activebackground=_tone(SELECT),
            activeforeground=_tone(TEXT),
            font=self._px_font(12),
        )
        menu.add_command(label="Файл проекта…", command=self.attach_project_file)
        menu.add_command(label="Папка проекта…", command=self.attach_project_dir)
        menu.add_command(label="Изображение…", command=self.attach_image)
        if self.context_files or self.context_dirs or self.attached:
            menu.add_separator()
            menu.add_command(label="Очистить вложения", command=self.clear_attachments)
        try:
            x = self.image_button.winfo_rootx()
            y = self.image_button.winfo_rooty() + self.image_button.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def attach_project_file(self) -> None:
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        if len(self.context_files) >= MAX_CONTEXT_FILES:
            self.write_chat(f"Можно вложить не больше {MAX_CONTEXT_FILES} файлов.")
            return
        selected = filedialog.askopenfilename(initialdir=str(self.project))
        if not selected:
            return
        try:
            full = resolve_inside(self.project, selected)
            rel = relative_posix(self.project, full)
        except PathError as exc:
            self.write_chat(f"Файл вне проекта: {exc}")
            return
        if not full.is_file():
            self.write_chat("Нужен файл, не папка.")
            return
        self._add_context_path(rel)

    def attach_project_dir(self) -> None:
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        if len(self.context_dirs) >= MAX_CONTEXT_DIRS:
            self.write_chat(f"Можно вложить не больше {MAX_CONTEXT_DIRS} папок.")
            return
        selected = filedialog.askdirectory(initialdir=str(self.project))
        if not selected:
            return
        try:
            full = resolve_inside(self.project, selected)
            rel = relative_posix(self.project, full)
        except PathError as exc:
            self.write_chat(f"Папка вне проекта: {exc}")
            return
        if not full.is_dir():
            self.write_chat("Нужна папка.")
            return
        self._add_context_path(rel + "/")

    def _add_context_path(self, relative: str) -> None:
        path = str(relative or "").replace("\\", "/").strip()
        is_dir = path.endswith("/")
        path = path.strip("/")
        if not path and not is_dir:
            return
        if is_dir or (self.project is not None and (self.project / path).is_dir()):
            if path in self.context_dirs:
                self._refresh_attach()
                return
            if len(self.context_dirs) >= MAX_CONTEXT_DIRS:
                self.write_chat(f"Можно вложить не больше {MAX_CONTEXT_DIRS} папок.")
                return
            self.context_dirs.append(path)
            self._refresh_attach()
            return
        if path in self.context_files:
            self._refresh_attach()
            return
        if len(self.context_files) >= MAX_CONTEXT_FILES:
            self.write_chat(f"Можно вложить не больше {MAX_CONTEXT_FILES} файлов.")
            return
        self.context_files.append(path)
        self._refresh_attach()

    def _add_context_file(self, relative: str) -> None:
        self._add_context_path(relative)

    def clear_attachments(self) -> None:
        self.attached.clear()
        self.context_files.clear()
        self.context_dirs.clear()
        self._refresh_attach()

    def attach_image(self) -> None:
        if len(self.attached) >= 4:
            self.write_chat("Можно приложить не больше 4 изображений.")
            return
        selected = filedialog.askopenfilename(
            filetypes=[("Изображения", "*.png *.jpg *.jpeg *.gif *.webp *.bmp")]
        )
        if selected:
            self.attached.append(selected)
            self._refresh_attach()

    def _paste_clipboard_image(self) -> bool:
        if len(self.attached) >= 4:
            self.write_chat("Можно приложить не больше 4 изображений.")
            return True
        path = save_clipboard_image()
        if path is None:
            return False
        self.attached.append(str(path))
        self._refresh_attach()
        self.set_status("Изображение из буфера")
        return True

    def _on_task_clipboard(self, event):
        if not (int(getattr(event, "state", 0) or 0) & 0x4):
            return
        action = clipboard_action(str(event.keysym), int(getattr(event, "keycode", 0) or 0))
        if not action:
            return
        if action == "paste":
            if self._paste_clipboard_image():
                return "break"
            apply_layout_clipboard(event.widget, "paste")
            return "break"
        apply_layout_clipboard(event.widget, action)
        return "break"

    def _on_task_paste_event(self, _event=None):
        if self._paste_clipboard_image():
            return "break"
        return None

    def _attach_specs(self) -> list[dict]:
        specs = [{"kind": "dir", "path": path, "text": "@" + Path(path).name + "/"} for path in self.context_dirs]
        specs += [{"kind": "file", "path": path, "text": "@" + Path(path).name} for path in self.context_files]
        specs += [{"kind": "image", "path": path, "text": Path(path).name} for path in self.attached]
        return specs

    def _rule_specs(self) -> list[dict]:
        try:
            text = rule_chip_text(self.project)
        except Exception:
            return []
        if not text:
            return []
        return [{"kind": "rule", "path": "", "text": text}]

    def _strip_specs(self) -> list[dict]:
        return self._rule_specs() + self._attach_specs()

    def _drop_attachment(self, kind: str, path: str) -> None:
        if kind == "rule":
            return
        bucket = {"dir": self.context_dirs, "file": self.context_files, "image": self.attached}.get(kind)
        if bucket is not None and path in bucket:
            bucket.remove(path)
        self._refresh_attach()

    def _clear_chips(self) -> None:
        for widget in self.attach_row.winfo_children():
            widget.destroy()

    def _on_attach_resize(self, event=None) -> None:
        width = event.width if event is not None else self.attach_row.winfo_width()
        if width == getattr(self, "_chip_width", 0):
            return
        self._chip_width = width
        if self._strip_specs():
            self._fill_chips()

    def _refresh_attach(self) -> None:
        if not hasattr(self, "attach_row"):
            return
        if not self._strip_specs():
            self._clear_chips()
            self.attach_row.grid_remove()
        else:
            self.attach_row.grid()
            self._fill_chips()
        # ряд чипов двигает поле ввода — подсказку и высоту надо пересчитать.
        self.after(0, self._sync_placeholder)
        self._schedule_fit_composer()

    def _fill_chips(self) -> None:
        self._clear_chips()
        specs = self._strip_specs()
        removable = self._attach_specs()
        measure = self._measure(11)
        room = max(self.attach_row.winfo_width(), self.card.winfo_width() - 24, 240)
        if removable:
            # место под «очистить» в конце последнего ряда
            room = max(room - 74, 200)
        lines: list[list[dict]] = [[]]
        used = 0
        for spec in specs:
            # ширина текста плюс поля чипа и крестик (у правил без крестика — чуть уже)
            span = measure(spec["text"]) + (28 if spec.get("kind") == "rule" else 46)
            if lines[-1] and used + span > room:
                if len(lines) >= _CHIP_ROWS:
                    break
                lines.append([])
                used = 0
            lines[-1].append(spec)
            used += span
        shown = sum(len(line) for line in lines)
        self._schedule_fit_composer()
        for index, line in enumerate(lines):
            if not line:
                continue
            strip = ctk.CTkFrame(self.attach_row, fg_color="transparent")
            strip.pack(fill="x", anchor="w", pady=(0 if index == 0 else 3, 0))
            for spec in line:
                self._make_chip(strip, spec).pack(side="left", padx=(0, 4))
            if index == len(lines) - 1:
                if shown < len(specs):
                    ctk.CTkLabel(
                        strip,
                        text=f"+{len(specs) - shown}",
                        text_color=MUTED,
                        font=self._font(11),
                    ).pack(side="left", padx=(2, 6))
                if removable:
                    clear = ctk.CTkButton(
                        strip,
                        text="очистить",
                        width=56,
                        height=20,
                        corner_radius=8,
                        fg_color="transparent",
                        hover_color=BUTTON_HOVER,
                        text_color=MUTED,
                        font=self._font(11),
                        command=self.clear_attachments,
                    )
                    clear.pack(side="left", padx=(2, 0))

    def _make_chip(self, parent, spec: dict) -> ctk.CTkFrame:
        is_rule = spec.get("kind") == "rule"
        chip = ctk.CTkFrame(
            parent,
            fg_color="transparent" if is_rule else BUTTON,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
        )
        label = ctk.CTkLabel(chip, text=spec["text"], text_color=MUTED, font=self._font(11), height=18)
        label.grid(row=0, column=0, padx=(8, 8 if is_rule else 2), pady=1)
        if is_rule:
            return chip
        close = ctk.CTkButton(
            chip,
            text="×",
            width=16,
            height=16,
            corner_radius=8,
            fg_color="transparent",
            hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(13),
            command=lambda kind=spec["kind"], path=spec["path"]: self._drop_attachment(kind, path),
        )
        close.grid(row=0, column=1, padx=(0, 4), pady=1)
        return chip

    def _on_task_key(self, _event=None) -> None:
        if self.project is None:
            self._hide_at_popup()
            return
        inner = self.task._textbox
        before = inner.get("1.0", "insert")
        token = at_token_at_end(before)
        if token is None:
            self._hide_at_popup()
            return
        query, start = token
        try:
            found = find_at_targets(self.project, query, limit=8)
        except Exception:
            found = []
        if not found:
            self._hide_at_popup()
            return
        self._at_start = f"1.0+{start}c"
        self._show_at_popup(found)

    def _at_popup_alive(self) -> bool:
        popup = self._at_popup
        return popup is not None and bool(popup.winfo_exists())

    def _show_at_popup(self, paths: list[str]) -> None:
        if not self._at_popup_alive():
            popup = tk.Toplevel(self)
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            listbox = tk.Listbox(
                popup,
                height=min(8, max(1, len(paths))),
                activestyle="dotbox",
                bg=_tone(PANEL),
                fg=_tone(TEXT),
                selectbackground=_tone(SELECT),
                selectforeground=_tone(TEXT),
                font=self._px_font(11),
                borderwidth=1,
                highlightthickness=0,
                relief="solid",
            )
            listbox.pack(fill="both", expand=True)
            listbox.bind("<ButtonRelease-1>", lambda _e: self._at_accept())
            self._at_popup = popup
            self._at_list = listbox
        listbox = self._at_list
        popup = self._at_popup
        if listbox is None or popup is None:
            return
        listbox.delete(0, "end")
        for path in paths:
            listbox.insert("end", path)
        listbox.selection_set(0)
        listbox.activate(0)
        listbox.configure(height=min(8, max(1, len(paths))))
        try:
            x, y, _w, h = self.task._textbox.bbox("insert")
            abs_x = self.task._textbox.winfo_rootx() + x
            abs_y = self.task._textbox.winfo_rooty() + y + h + 2
        except Exception:
            abs_x = self.card.winfo_rootx() + 12
            abs_y = self.card.winfo_rooty() + 40
        popup.geometry(f"360x{min(8, len(paths)) * 22 + 4}+{abs_x}+{abs_y}")
        popup.deiconify()

    def _hide_at_popup(self, _event=None):
        popup = self._at_popup
        if popup is not None and popup.winfo_exists():
            popup.destroy()
        self._at_popup = None
        self._at_list = None
        self._at_start = None
        return "break" if _event is not None else None

    def _at_move(self, event):
        if not self._at_popup_alive() or self._at_list is None:
            return None
        size = self._at_list.size()
        if size <= 0:
            return "break"
        current = self._at_list.curselection()
        index = int(current[0]) if current else 0
        if event.keysym == "Down":
            index = min(size - 1, index + 1)
        else:
            index = max(0, index - 1)
        self._at_list.selection_clear(0, "end")
        self._at_list.selection_set(index)
        self._at_list.activate(index)
        self._at_list.see(index)
        return "break"

    def _at_accept_key(self, _event=None):
        if not self._at_popup_alive():
            return None
        self._at_accept()
        return "break"

    def _at_accept(self) -> None:
        if not self._at_popup_alive() or self._at_list is None or self._at_start is None:
            self._hide_at_popup()
            return
        selected = self._at_list.curselection()
        if not selected:
            self._hide_at_popup()
            return
        path = str(self._at_list.get(selected[0]) or "").strip()
        inner = self.task._textbox
        try:
            inner.delete(self._at_start, "insert")
        except tk.TclError:
            pass
        self._hide_at_popup()
        if path:
            self._add_context_path(path)
            if inner.get("1.0", "end-1c").strip():
                self.placeholder.place_forget()
            else:
                self._sync_placeholder()
