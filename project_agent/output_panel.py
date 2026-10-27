"""Панель вывода прогонов (run_tests / run_allowed / run_shell).

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к панели вывода:
построение виджетов (шапка «Текущий/Предыдущий», очистка, закрытие, сам
текстовый бокс), состояние слотов прогонов, потоковая досыпка чанков с
debounce и разбор диагностики из текста прогона.

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``mid_split``, ``run_indicator``,
``project``, ``_closing``, ``_code_font``/``_font``, ``_show_file_util_panel``,
``set_status``. Поэтому код оформлен миксином ``OutputPanelMixin``: методы
остаются методами ``App`` (``class App(OutputPanelMixin, ctk.CTk)``), но живут
в отдельном файле.

Порядок вызовов при старте: ``_init_output_state()`` — состояние, затем
``_build_output_panel()`` — виджеты (вызывается из ``App._build`` на том же
месте, где раньше создавался блок).
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from project_agent.clipboard import _on_layout_clipboard, mark_readonly
from project_agent.ui_theme import BUTTON, BUTTON_HOVER, FIELD, MUTED, PANEL, SELECT, TEXT
from project_agent.ui_widgets import icon_button, quiet_button


class OutputPanelMixin:
    """Состояние и поведение панели вывода; требует виджетов ``App``."""

    def _init_output_state(self) -> None:
        self._output_open = False
        self._output_slots: list[dict] = []
        self._output_view = 0
        self._output_pending = ""
        self._output_flush_job = None

    def _build_output_panel(self) -> None:
        self.output_frame = ctk.CTkFrame(
            self.mid_split, fg_color=PANEL, corner_radius=0, border_width=0
        )
        self.output_frame.grid_columnconfigure(0, weight=1)
        self.output_frame.grid_rowconfigure(1, weight=1)
        out_head = ctk.CTkFrame(self.output_frame, fg_color="transparent")
        out_head.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 2))
        out_head.grid_columnconfigure(0, weight=1)
        self.output_title = ctk.CTkLabel(
            out_head, text="Пока нет прогонов", anchor="w", text_color=MUTED, font=self._font(12)
        )
        self.output_title.grid(row=0, column=0, sticky="ew")
        self.output_slot_var = ctk.StringVar(value="Текущий")
        self.output_slot_menu = ctk.CTkOptionMenu(
            out_head,
            variable=self.output_slot_var,
            values=["Текущий"],
            command=self._on_output_slot_pick,
            height=24,
            width=110,
            corner_radius=8,
            fg_color=FIELD,
            button_color=FIELD,
            button_hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(11),
            dropdown_fg_color=PANEL,
            dropdown_text_color=TEXT,
            dropdown_hover_color=SELECT,
            dropdown_font=self._font(11),
        )
        self.output_slot_menu.grid(row=0, column=1, padx=(6, 4))
        quiet_button(out_head, "Очистить", self.clear_output_panel, width=84).grid(
            row=0, column=2, padx=(0, 4)
        )
        icon_button(out_head, "close", self.close_output_panel, size=26).grid(row=0, column=3)
        self.output_box = ctk.CTkTextbox(
            self.output_frame,
            wrap="none",
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=8,
            font=self._code_font(12),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.output_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self.output_box.configure(state="disabled")
        self.output_box._textbox.bind("<Control-KeyPress>", _on_layout_clipboard, add="+")
        mark_readonly(self.output_box._textbox)

    def toggle_output_panel(self) -> None:
        if self._output_open:
            self.close_output_panel()
        else:
            self.open_output_panel()

    def open_output_panel(self) -> None:
        if self._output_open or not hasattr(self, "mid_split"):
            return
        try:
            self.mid_split.add(self.output_frame, stretch="never", minsize=80)
        except tk.TclError:
            # Уже в пане: путь CTkFrame ≠ panes(), add может ругнуться — ок.
            pass
        self.after(20, self._place_output_sash)
        self._output_open = True
        self._show_output_slot(self._output_view)

    def close_output_panel(self) -> None:
        if not hasattr(self, "mid_split"):
            return
        try:
            self.mid_split.forget(self.output_frame)
        except tk.TclError:
            pass
        self._output_open = False

    def _place_output_sash(self) -> None:
        if not self._output_open:
            return
        try:
            height = max(self.mid_split.winfo_height(), 200)
            sash = max(100, height - 180)
            self.mid_split.sash_place(0, 0, sash)
        except tk.TclError:
            return

    def clear_output_panel(self) -> None:
        self._output_slots.clear()
        self._output_view = 0
        self._output_pending = ""
        if self._output_flush_job is not None:
            try:
                self.after_cancel(self._output_flush_job)
            except Exception:
                pass
            self._output_flush_job = None
        self._set_output_text("")
        self.output_title.configure(text="Пока нет прогонов")
        self.run_indicator.configure(text="")
        self.output_slot_var.set("Текущий")
        self.output_slot_menu.configure(values=["Текущий"])

    def _on_output_slot_pick(self, label: str) -> None:
        if label == "Предыдущий" and len(self._output_slots) > 1:
            self._output_view = 1
        else:
            self._output_view = 0
        self._show_output_slot(self._output_view)

    def _show_output_slot(self, index: int) -> None:
        if not self._output_slots:
            self._set_output_text("")
            self.output_title.configure(text="Пока нет прогонов")
            return
        index = max(0, min(index, len(self._output_slots) - 1))
        self._output_view = index
        slot = self._output_slots[index]
        title = slot.get("title") or "Прогон"
        status = slot.get("status") or ""
        head = f"{title} · {status}".strip(" ·") if status else title
        self.output_title.configure(text=head)
        self._set_output_text(slot.get("text") or "")
        labels = ["Текущий"]
        if len(self._output_slots) > 1:
            labels.append("Предыдущий")
        self.output_slot_menu.configure(values=labels)
        self.output_slot_var.set("Предыдущий" if index == 1 else "Текущий")

    def _set_output_text(self, text: str) -> None:
        box = getattr(self, "output_box", None)
        if box is None:
            return
        try:
            box.configure(state="normal")
            box.delete("1.0", "end")
            if text:
                box.insert("1.0", text)
                box.see("end")
            box.configure(state="disabled")
        except tk.TclError:
            return

    def _append_output_text(self, text: str) -> None:
        if not text:
            return
        box = getattr(self, "output_box", None)
        if box is None:
            return
        try:
            box.configure(state="normal")
            box.insert("end", text)
            box.see("end")
            box.configure(state="disabled")
        except tk.TclError:
            return

    def _on_run_output(self, event: str, **payload) -> None:
        def apply() -> None:
            if self._closing:
                return
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if event == "start":
                self._begin_output_run(str(payload.get("title") or "Прогон"), str(payload.get("command") or ""))
            elif event == "chunk":
                self._chunk_output_run(str(payload.get("text") or ""))
            elif event == "end":
                self._end_output_run(
                    str(payload.get("title") or ""),
                    str(payload.get("status") or ""),
                )

        try:
            self.after(0, apply)
        except tk.TclError:
            return

    def _begin_output_run(self, title: str, command: str) -> None:
        previous = self._output_slots[0] if self._output_slots else None
        slot = {
            "title": title,
            "command": command,
            "status": "идёт…",
            "text": (f"$ {command}\n\n" if command else ""),
        }
        self._output_slots = [slot] + ([previous] if previous else [])
        self._output_slots = self._output_slots[:2]
        self._output_view = 0
        self._output_pending = ""
        self.open_output_panel()
        self._show_output_slot(0)
        mark = (
            title.replace("run_tests · ", "")
            .replace("run_allowed · ", "")
            .replace("run_shell", "shell")
        )
        self.run_indicator.configure(text=f"▶ {mark}")

    def _chunk_output_run(self, text: str) -> None:
        if not self._output_slots:
            return
        self._output_slots[0]["text"] = (self._output_slots[0].get("text") or "") + text
        if self._output_view != 0:
            return
        self._output_pending += text
        if self._output_flush_job is not None:
            return
        self._output_flush_job = self.after(50, self._flush_output_pending)

    def _flush_output_pending(self) -> None:
        self._output_flush_job = None
        chunk = self._output_pending
        self._output_pending = ""
        if chunk and self._output_view == 0:
            self._append_output_text(chunk)

    def _end_output_run(self, title: str, status: str) -> None:
        if self._output_flush_job is not None:
            try:
                self.after_cancel(self._output_flush_job)
            except Exception:
                pass
            self._output_flush_job = None
        if self._output_pending:
            pending = self._output_pending
            self._output_pending = ""
            if self._output_view == 0:
                self._append_output_text(pending)
        if self._output_slots:
            if title:
                self._output_slots[0]["title"] = title
            self._output_slots[0]["status"] = status or "готово"
            if self._output_view == 0:
                self._show_output_slot(0)
        mark = (
            (title or self.run_indicator.cget("text"))
            .replace("run_tests · ", "")
            .replace("run_allowed · ", "")
            .replace("run_shell", "shell")
            .replace("▶ ", "")
        )
        self.run_indicator.configure(text=f"{mark}: {status}" if status else mark)
        if self.project is not None and self._output_slots:
            self._ingest_run_diagnostics(str(self._output_slots[0].get("text") or ""), status or "")

    def _ingest_run_diagnostics(self, text: str, status: str) -> None:
        blob = f"{status}\n{text}"
        if not any(mark in blob for mark in ("Traceback", "FAIL", "ERROR", "Error", "Sorry:")):
            return
        from project_agent.diagnostics import parse_python_locations

        hits = parse_python_locations(text, self.project)
        if not hits:
            return
        self._diag_hits = hits
        self._show_file_util_panel("problems")
        self.set_status(f"Проблемы из прогона: {len(hits)}")
