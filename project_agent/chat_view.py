"""Чат-рендер: лента сообщений, теги, поиск по чату, копирование, поток ответа.

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к ленте чата: построение
виджетов чата (панель поиска, сам ``CTkTextbox``, кнопка «вниз»), теги
оформления, вставка блоков (пользователь/ассистент/инструмент/ошибка/снимок),
пузыри и чипы копирования, группы инструментов, поиск по тексту, поведение
скролла и потоковая досыпка ответа (``stream_chat``/``_apply_stream``).

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``mid_split``, ``_font``/
``_px_font``, ``code_font``, ``checkpoints``, ``running``, ``transcript``,
``_remember``/``_store_chat``, ``set_status``, ``_embed_checkpoint_control``.
Поэтому код оформлен миксином ``ChatViewMixin``: методы остаются методами
``App`` (``class App(OutputPanelMixin, ChatViewMixin, ctk.CTk)``), но живут
в отдельном файле.

Порядок вызовов при старте: виджеты чата строит ``_build_chat_view()`` —
она вызывается из ``App._build`` ровно на том месте, где раньше стоял
инлайн-блок (внутри ``mid_split``, перед панелью вывода). Состояние ленты
(``_chat_pad``, ``chat_id``, ``transcript``, ``_stream_*``, ``_user_*``,
``_retry_*``, ``_tool_*``, ``_search_*``) осталось в ``App.__init__``:
его читают и пишут другие части ``App``, а сам перенос — дословный.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from project_agent.chat_markup import (
    chat_body,
    chat_role,
    chat_segments,
    is_running_journal,
    split_chat_stamp,
    tool_group_title,
    tool_short_name,
    user_copy_text,
    with_chat_stamp,
)
from project_agent.checkpoints import parse_checkpoint_mark
from project_agent.clipboard import (
    apply_layout_clipboard,
    clipboard_action,
    mark_readonly,
    set_clipboard,
    _on_layout_clipboard,
    _selection,
)
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    CHAT_COLUMN,
    CODE_BG,
    CODE_TEXT,
    CTX_FULL,
    FIELD,
    INK,
    MUTED,
    PANEL,
    SEARCH_BG,
    SEARCH_CUR,
    SELECT,
    TEXT,
    USER_TEXT,
)
from project_agent.ui_widgets import (
    bind_wheel_scroll,
    icon_button,
    quiet_button,
    _paint_copy,
    _tone,
)

# Группы с одним действием не сворачиваем: единственная строка и так информативна.
_GROUP_FOLD = 2

_CHAT_FREE = {
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
}


class ChatViewMixin:
    """Лента чата: виджеты, оформление, поиск, копирование и поток ответа."""

    def _build_chat_view(self) -> None:
        chat_holder = ctk.CTkFrame(self.mid_split, fg_color=INK, corner_radius=0)
        self.chat_holder = chat_holder
        chat_holder.grid_columnconfigure(0, weight=1)
        chat_holder.grid_rowconfigure(1, weight=1)
        self.mid_split.add(chat_holder, stretch="always", minsize=120)
        self.search_bar = ctk.CTkFrame(
            chat_holder, fg_color=PANEL, corner_radius=10, border_width=1, border_color=BORDER, height=36
        )
        self.search_bar.grid(row=0, column=0, sticky="ew", padx=(16, 16), pady=(0, 6))
        self.search_bar.grid_columnconfigure(0, weight=1)
        self.search_bar.grid_remove()
        self.search_var = ctk.StringVar(value="")
        self.search_entry = ctk.CTkEntry(
            self.search_bar,
            textvariable=self.search_var,
            placeholder_text="Поиск по чату",
            height=28,
            border_width=0,
            fg_color=FIELD,
            text_color=TEXT,
            font=self._font(12),
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=4)
        self.search_count = ctk.CTkLabel(
            self.search_bar, text="", width=52, text_color=MUTED, font=self._font(11)
        )
        self.search_count.grid(row=0, column=1, padx=(0, 2))
        quiet_button(self.search_bar, "↑", self._search_prev, width=32).grid(row=0, column=2, padx=2, pady=4)
        quiet_button(self.search_bar, "↓", self._search_next, width=32).grid(row=0, column=3, padx=2, pady=4)
        icon_button(self.search_bar, "close", self.close_chat_search, size=28).grid(
            row=0, column=4, padx=(2, 6), pady=4
        )
        self.search_var.trace_add("write", lambda *_args: self._run_chat_search())
        self.search_entry.bind("<Return>", lambda _e: self._search_next())
        self.search_entry.bind("<Shift-Return>", lambda _e: self._search_prev())
        self.search_entry.bind("<Escape>", lambda _e: self.close_chat_search())
        self.chat = ctk.CTkTextbox(
            chat_holder,
            wrap="word",
            fg_color=INK,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=self._font(13),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.chat.grid(row=1, column=0, sticky="nsew", padx=(8, 0))
        self._tag_chat()
        self.chat._textbox.bind("<Configure>", self._center_chat, add="+")
        self.chat.bind("<<Paste>>", lambda _event: "break")
        self.chat.bind("<<Cut>>", lambda _event: "break")
        self.chat.bind("<Key>", self._chat_key)
        self.chat._textbox.bind("<Control-KeyPress>", self._on_chat_control, add="+")
        self.chat._textbox.bind("<Button-3>", self._chat_text_menu)
        self.chat._textbox.configure(yscrollcommand=self._chat_scroll_set)
        self.chat._y_scrollbar.configure(command=self._chat_yview)
        self.jump_down = ctk.CTkButton(
            chat_holder,
            text="↓",
            width=34,
            height=34,
            corner_radius=17,
            border_width=1,
            border_color=BORDER,
            fg_color=BUTTON,
            hover_color=BUTTON_HOVER,
            text_color=TEXT,
            font=self._font(14, weight="bold"),
            command=self._jump_chat_end,
        )
        self.jump_down.place_forget()
        self.bind_all("<Control-KeyPress>", self._on_find_key, add="+")
        mark_readonly(self.chat._textbox)

    def _chat_key(self, event):
        if event.keysym in _CHAT_FREE:
            return
        if event.state & 0x4:
            if self._is_find_key(event):
                self.open_chat_search()
                return "break"
            action = clipboard_action(str(event.keysym), int(getattr(event, "keycode", 0) or 0))
            if action in {"copy", "select"}:
                apply_layout_clipboard(event.widget, action)
                return "break"
            if action in {"paste", "cut"}:
                return "break"
            if event.keysym == "Insert":
                apply_layout_clipboard(event.widget, "copy")
                return "break"
            return "break"
        return "break"

    def _on_chat_control(self, event):
        if not (int(getattr(event, "state", 0) or 0) & 0x4):
            return
        if self._is_find_key(event):
            self.open_chat_search()
            return "break"
        return _on_layout_clipboard(event)

    def _is_find_key(self, event) -> bool:
        # F / А (ЙЦУКЕН) — поиск по чату.
        symbol = str(getattr(event, "keysym", "") or "")
        if symbol.lower() in {"f", "cyrillic_a"}:
            return True
        return int(getattr(event, "keycode", 0) or 0) == 70

    def _on_find_key(self, event):
        if not (int(getattr(event, "state", 0) or 0) & 0x4):
            return
        if not self._is_find_key(event):
            return
        # Не перехватываем Ctrl+F в чужих toplevel (настройки/диалоги).
        try:
            if event.widget.winfo_toplevel() is not self:
                return
        except tk.TclError:
            return
        self.open_chat_search()
        return "break"

    def open_chat_search(self) -> None:
        if not hasattr(self, "search_bar"):
            return
        self._search_open = True
        self.search_bar.grid()
        try:
            self.search_entry.focus_set()
            self.search_entry.select_range(0, "end")
        except tk.TclError:
            pass
        self._run_chat_search()

    def close_chat_search(self, _event=None):
        if not hasattr(self, "search_bar"):
            return None
        self._search_open = False
        self.search_bar.grid_remove()
        self._clear_search_marks()
        self._search_hits = []
        self._search_index = -1
        self._search_query = ""
        self.search_count.configure(text="")
        try:
            self.task._textbox.focus_set()
        except tk.TclError:
            pass
        return "break"

    def _clear_search_marks(self) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        try:
            box._textbox.tag_remove("search", "1.0", "end")
            box._textbox.tag_remove("searchcur", "1.0", "end")
        except tk.TclError:
            return

    def _run_chat_search(self) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists() or not self._search_open:
            return
        query = self.search_var.get()
        self._search_query = query
        self._clear_search_marks()
        self._search_hits = []
        self._search_index = -1
        needle = query.strip()
        if not needle:
            self.search_count.configure(text="")
            return
        inner = box._textbox
        start = "1.0"
        while True:
            # -elide: искать и в свёрнутых блоках инструментов
            found = inner.search(needle, start, stopindex="end", nocase=True, elide=True)
            if not found:
                break
            end = f"{found}+{len(needle)}c"
            self._search_hits.append((found, end))
            inner.tag_add("search", found, end)
            start = end
        if not self._search_hits:
            self.search_count.configure(text="0/0")
            return
        self._search_index = 0
        self._focus_search_hit()

    def _focus_search_hit(self) -> None:
        if not self._search_hits:
            self.search_count.configure(text="0/0")
            return
        index = max(0, min(self._search_index, len(self._search_hits) - 1))
        self._search_index = index
        box = self.chat
        inner = box._textbox
        try:
            inner.tag_remove("searchcur", "1.0", "end")
            start, end = self._search_hits[index]
            # Если совпадение в свёрнутом блоке инструментов — раскрыть.
            for group in self._tool_groups:
                if not group["open"] and group["tag"] in inner.tag_names(start):
                    self._toggle_tool_group(group)
                    break
            inner.tag_add("searchcur", start, end)
            inner.see(start)
            self._chat_stick = False
            self._update_jump_down()
        except tk.TclError:
            pass
        self.search_count.configure(text=f"{index + 1}/{len(self._search_hits)}")

    def _search_next(self, _event=None):
        if not self._search_hits:
            self._run_chat_search()
            return "break"
        self._search_index = (self._search_index + 1) % len(self._search_hits)
        self._focus_search_hit()
        return "break"

    def _search_prev(self, _event=None):
        if not self._search_hits:
            self._run_chat_search()
            return "break"
        self._search_index = (self._search_index - 1) % len(self._search_hits)
        self._focus_search_hit()
        return "break"

    def _chat_at_bottom(self, first=None, last=None) -> bool:
        try:
            inner = self.chat._textbox
            if int(inner.winfo_height()) < 80:
                return True
            if not inner.get("1.0", "end-1c").strip():
                return True
            if first is None or last is None:
                first, last = inner.yview()
            first_f = float(first)
            last_f = float(last)
        except (tk.TclError, TypeError, ValueError, AttributeError):
            return True
        visible = last_f - first_f
        # Нет прокрутки (всё видно) или низ в кадре.
        if visible <= 0.0 or visible >= 0.99:
            return True
        return last_f >= 0.985

    def _chat_scroll_set(self, first, last) -> None:
        try:
            self.chat._y_scrollbar.set(first, last)
        except (tk.TclError, AttributeError):
            pass
        self._chat_stick = self._chat_at_bottom(first, last)
        self._update_jump_down(last)

    def _chat_yview(self, *args) -> None:
        self.chat._textbox.yview(*args)
        try:
            first, last = self.chat._textbox.yview()
            self._chat_stick = self._chat_at_bottom(first, last)
            self._update_jump_down(last)
        except (tk.TclError, ValueError, TypeError):
            return

    def _chat_near_bottom(self) -> bool:
        return self._chat_at_bottom()

    def _chat_see_end(self, force: bool = False) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        if force or self._chat_stick or self._chat_near_bottom():
            box.see("end")
            self._chat_stick = True
        self._update_jump_down()

    def _jump_chat_end(self) -> None:
        self._chat_stick = True
        self._chat_see_end(force=True)

    def _update_jump_down(self, last=None) -> None:
        button = getattr(self, "jump_down", None)
        box = getattr(self, "chat", None)
        if button is None or box is None or not box.winfo_exists():
            return
        try:
            inner = box._textbox
            # Пока paned/layout даёт высоту 1px, yview врёт (например 0..0.06) — стрелку не рисуем.
            if int(inner.winfo_height()) < 80:
                button.place_forget()
                return
            if not inner.get("1.0", "end-1c").strip():
                button.place_forget()
                return
            first, bottom = inner.yview()
            first_f = float(first)
            last_f = float(bottom if last is None else last)
        except (tk.TclError, TypeError, ValueError):
            button.place_forget()
            return
        visible = last_f - first_f
        can_scroll = visible > 0.0 and visible < 0.99
        show = can_scroll and last_f < 0.985
        if show:
            button.place(relx=1.0, rely=1.0, x=-18, y=-18, anchor="se")
            button.lift()
        else:
            button.place_forget()

    def _chat_text_menu(self, event) -> str | None:
        inner = self.chat._textbox
        # Текст снимаем до tk_popup: на Windows выделение сбрасывается при grab меню.
        selected = _selection(inner)
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
        request = self._user_request_at(event)
        if request:
            menu.add_command(
                label="Копировать запрос",
                command=lambda text=request: self._copy_agent_message(text),
            )
        menu.add_command(
            label="Копировать",
            state="normal" if selected else "disabled",
            command=lambda text=selected: apply_layout_clipboard(inner, "copy", text),
        )
        menu.add_command(label="Выделить всё", command=self._chat_select_all)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _user_request_at(self, event) -> str:
        inner = self.chat._textbox
        try:
            point = inner.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return ""
        found = ""
        for start, end, text in self._user_spans:
            try:
                if inner.compare(start, "<=", point) and inner.compare(point, "<", end):
                    found = text
            except tk.TclError:
                continue
        return found

    def _chat_select_all(self) -> None:
        inner = self.chat._textbox
        apply_layout_clipboard(inner, "select")
        try:
            inner.focus_set()
        except tk.TclError:
            return

    def write_chat(self, text: str) -> None:
        line = with_chat_stamp(text)
        self._remember(line)
        self._append(self.chat, line)

    def write_retryable_error(self, text: str) -> None:
        self._can_retry = True
        line = with_chat_stamp(text)
        self._remember(line)
        self.after(0, lambda line=line: self._append_retryable(line, button="Повторить", error=True))

    def write_stopped(self, text: str = "Остановлено.") -> None:
        self._can_retry = bool(self._retry_payload)
        line = with_chat_stamp(text)
        self._remember(line)
        self.after(
            0,
            lambda line=line, can=self._can_retry: self._append_retryable(
                line, button="Продолжить", error=False, with_button=can
            ),
        )

    def _forget_retry(self) -> None:
        self._retry_payload = None
        self._can_retry = False
        self._retry_buttons = []

    def _set_retry_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        alive = []
        for button in self._retry_buttons:
            try:
                if not button.winfo_exists():
                    continue
                button.configure(state=state)
                alive.append(button)
            except tk.TclError:
                continue
        self._retry_buttons = alive

    def _append_retryable(
        self, text: str, *, button: str, error: bool = False, with_button: bool = True
    ) -> None:
        box = self.chat
        if not box.winfo_exists():
            return
        box.configure(state="normal")
        inner = box._textbox
        self._clear_running_tool()
        self._close_tool_group()
        start = inner.index("end-1c")
        body = chat_body(text, "error" if error else None)
        _, stamp = split_chat_stamp(text)
        inner.insert("end", body + "\n")
        if with_button:
            self._insert_retry_button(inner, label=button, error=error)
        if stamp:
            mark = inner.index("end-1c")
            inner.insert("end", f"{stamp}\n\n", ("time",))
            if error:
                inner.tag_add("error", start, mark)
        elif error:
            inner.tag_add("error", start, inner.index("end-1c"))
        self._chat_see_end()

    def _append_error(self, text: str) -> None:
        self._append_retryable(text, button="Повторить", error=True)

    def _insert_retry_button(self, inner, *, label: str = "Повторить", error: bool = True) -> None:
        self._set_retry_enabled(False)
        button = tk.Button(
            inner,
            text=label,
            command=self.retry_last,
            relief="flat",
            bd=0,
            padx=8,
            pady=2,
            cursor="hand2",
            bg=_tone(BUTTON),
            fg=_tone(TEXT),
            activebackground=_tone(BUTTON_HOVER),
            activeforeground=_tone(TEXT),
            disabledforeground=_tone(MUTED),
            font=self._px_font(12),
        )
        self._retry_buttons.append(button)
        mark = inner.index("end-1c")
        inner.window_create("end", window=button, padx=0, pady=2)
        inner.insert("end", "\n")
        if error:
            inner.tag_add("error", mark, inner.index("end-1c"))

    def write_journal(self, text: str) -> None:
        line = f"· {text.strip()}"
        # Строка «выполняется…» живёт только на экране и гаснет, когда пришёл итог.
        if is_running_journal(line):
            self.after(0, lambda line=line: self._show_running_tool(line))
            return
        self.after(0, self._clear_running_tool)
        self.write_chat(line)

    def _role(self, text: str) -> str:
        return chat_role(text)

    def _tag_chat(self) -> None:
        inner = self.chat._textbox
        inner.tag_configure(
            "user",
            font=self._px_font(13),
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=4,
            spacing3=4,
        )
        inner.tag_configure(
            "agent",
            font=self._px_font(13),
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            foreground=_tone(TEXT),
            spacing1=2,
            spacing3=4,
        )
        inner.tag_configure(
            "tool",
            font=self._px_font(12),
            lmargin1=40,
            lmargin2=48,
            rmargin=28,
            foreground=_tone(MUTED),
            spacing1=0,
            spacing3=0,
        )
        inner.tag_configure(
            "toolhead",
            font=self._px_font(12),
            lmargin1=24,
            lmargin2=24,
            rmargin=28,
            spacing1=4,
            spacing3=2,
        )
        inner.tag_configure("toolgap", font=self._px_font(5), spacing1=0, spacing3=0)
        inner.tag_configure(
            "error",
            font=self._px_font(13),
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            foreground=_tone(CTX_FULL),
            spacing1=2,
            spacing3=4,
        )
        inner.tag_configure(
            "checkpoint",
            font=self._px_font(13),
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            spacing1=2,
            spacing3=4,
        )
        inner.tag_configure("label", font=self._px_font(11), foreground=_tone(MUTED), spacing1=0, spacing3=2)
        inner.tag_configure(
            "time",
            font=self._px_font(10),
            foreground=_tone(MUTED),
            spacing1=0,
            spacing3=2,
        )
        inner.tag_configure("bold", font=self._px_font(13, weight="bold"))
        inner.tag_configure(
            "code", font=self._px_font(12, self.code_font), foreground=_tone(CODE_TEXT)
        )
        inner.tag_configure(
            "codeblock",
            font=self._px_font(12, self.code_font),
            background=_tone(CODE_BG),
            foreground=_tone(TEXT),
            lmargin1=24,
            lmargin2=24,
            rmargin=24,
            spacing1=0,
            spacing3=0,
        )
        inner.tag_configure(
            "copyrow",
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            spacing1=0,
            spacing3=2,
        )
        inner.tag_configure(
            "usercopy",
            lmargin1=12,
            lmargin2=12,
            rmargin=12,
            spacing1=0,
            spacing3=2,
        )
        inner.tag_configure("search", background=_tone(SEARCH_BG))
        inner.tag_configure("searchcur", background=_tone(SEARCH_CUR))
        inner.tag_raise("sel")
        inner.tag_raise("search")
        inner.tag_raise("searchcur")
        inner.configure(pady=8)
        if self._chat_pad:
            inner.configure(padx=self._chat_pad)
        self._restyle_tool_heads()
        self._restyle_user_bubbles()
        if hasattr(self, "jump_down"):
            self.jump_down.configure(
                fg_color=BUTTON, hover_color=BUTTON_HOVER, text_color=TEXT, border_color=BORDER
            )
        if hasattr(self, "search_bar"):
            self.search_bar.configure(fg_color=PANEL, border_color=BORDER)
            self.search_entry.configure(fg_color=FIELD, text_color=TEXT)
            self.search_count.configure(text_color=MUTED)

    def _copy_photo(self) -> ImageTk.PhotoImage:
        cached = getattr(self, "_copy_photo_cache", None)
        if cached is not None:
            return cached
        image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        _paint_copy(ImageDraw.Draw(image), _tone(MUTED))
        image = image.resize((14, 14), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self._copy_photo_cache = photo
        return photo

    def _make_copy_chip(self, body: str, bg: tuple[str, str] | None = None, parent=None) -> tk.Label:
        fill = _tone(bg or INK)
        photo = self._copy_photo()
        chip = tk.Label(
            parent or self.chat._textbox,
            image=photo,
            bd=0,
            padx=2,
            pady=1,
            bg=fill,
            cursor="hand2",
            takefocus=0,
        )
        chip.image = photo
        chip.bind("<Button-1>", lambda _event, text=body: self._copy_agent_message(text))
        chip.bind("<Enter>", lambda _event: chip.configure(bg=_tone(PANEL)))
        chip.bind("<Leave>", lambda _event, color=fill: chip.configure(bg=color))
        return chip

    def _user_bubble_width(self) -> int:
        """Ширина пузыря по активной колонке чата (равные поля), не по длине текста."""
        try:
            inner = self.chat._textbox
            width = int(inner.winfo_width()) - 2 * int(inner.cget("padx") or 0) - 24
        except (tk.TclError, AttributeError, TypeError, ValueError):
            width = 0
        if width < 160:
            width = max(160, CHAT_COLUMN - 48)
        return width

    def _make_user_bubble(self, body: str, copy_text: str) -> dict:
        bubble_w = self._user_bubble_width()
        inner_w = max(120, bubble_w - 24)
        frame = ctk.CTkFrame(
            self.chat._textbox,
            fg_color=INK,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
            width=inner_w + 24,
            height=1,
        )
        frame.pack_propagate(False)
        # Сообщение — read-only Text: в CTkLabel нельзя ни выделить, ни скопировать текст.
        box = tk.Text(
            frame,
            wrap="word",
            height=max(1, body.count("\n") + 1),
            width=1,
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            bg=_tone(INK),
            fg=_tone(USER_TEXT),
            font=self._px_font(13),
            cursor="arrow",
            takefocus=0,
            insertwidth=0,
            selectbackground=_tone(SELECT),
            selectforeground=_tone(TEXT),
        )
        self._sync_bubble_selection(box)
        box.insert("1.0", body)
        box.configure(state="disabled")
        mark_readonly(box)
        chip = None
        if copy_text:
            chip = self._make_copy_chip(copy_text, INK, parent=frame)
        item = {
            "frame": frame,
            "box": box,
            "chip": chip,
            "body": body,
            "copy_text": copy_text,
            "inner_w": inner_w,
        }
        box.pack(fill="both", expand=True, padx=12, pady=(10, 4 if copy_text else 10))
        if chip is not None:
            chip.pack(anchor="w", padx=10, pady=(0, 8))
        box.bind("<Button-3>", lambda event, item=item: self._bubble_menu(event, item))
        box.bind("<Configure>", lambda _event, item=item: self._fit_user_bubble(item))
        bind_wheel_scroll(self.chat._textbox, frame)
        # До маппинга размер считаем по переводам строк; после — уточняем по displaylines.
        self._fit_user_bubble(item)
        self.after_idle(lambda item=item: self._fit_user_bubble(item))
        return item

    def _sync_bubble_selection(self, box) -> None:
        """Клик по disabled-Text фокус не забирает, а неактивный Text не рисует выделение.

        Без явного ``-inactiveselectbackground`` пользователь не увидел бы, что именно
        он выделил (сам Ctrl+C работает и так, через ``bind_class("Text", ...)``).
        """
        try:
            box.tk.call(box._w, "configure", "-inactiveselectbackground", _tone(SELECT))
        except tk.TclError:
            return

    def _user_bubble_line_h(self, box) -> int:
        try:
            linespace = box.tk.call("font", "metrics", box.cget("font"), "-linespace")
            return max(1, int(float(linespace)))
        except (tk.TclError, TypeError, ValueError):
            return max(1, -int(self._px_font(13)[1]))

    def _fit_user_bubble(self, item: dict) -> None:
        """Высота пузыря — по числу перенесённых строк; до маппинга считаем по переводам строк."""
        frame = item.get("frame")
        box = item.get("box")
        if frame is None or box is None:
            return
        try:
            if not (frame.winfo_exists() and box.winfo_exists()):
                return
        except tk.TclError:
            return
        try:
            lines = 0
            if int(box.winfo_width()) >= 50:
                counted = box.count("1.0", "end", "displaylines")
                if counted:
                    lines = max(1, int(counted[0]))
            if not lines:
                lines = max(1, str(item.get("body") or "").count("\n") + 1)
            chip = item.get("chip")
            extra = 10 + (4 if chip is not None else 10) + 2
            if chip is not None:
                extra += max(1, int(chip.winfo_reqheight())) + 8
            width = max(120, int(item.get("inner_w") or 0)) + 24
            height = lines * self._user_bubble_line_h(box) + extra
            # <Configure> приходит и от нашей же подгонки: без сверки получился бы цикл.
            if item.get("_fit_size") == (width, height, lines):
                return
            item["_fit_size"] = (width, height, lines)
            box.configure(height=lines)
            frame.configure(width=width, height=height)
        except (tk.TclError, TypeError, ValueError):
            return

    def _bubble_menu(self, event, item: dict) -> str | None:
        box = item.get("box")
        if box is None:
            return None
        # Выделение снимаем до tk_popup: на Windows оно сбрасывается при grab меню.
        selected = _selection(box)
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
        menu.add_command(
            label="Копировать",
            state="normal" if selected else "disabled",
            command=lambda text=selected: apply_layout_clipboard(box, "copy", text),
        )
        menu.add_command(
            label="Выделить всё", command=lambda: apply_layout_clipboard(box, "select")
        )
        copy_text = item.get("copy_text") or item.get("body") or ""
        if copy_text:
            menu.add_command(
                label="Копировать сообщение",
                command=lambda text=copy_text: self._copy_agent_message(text),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _restyle_user_bubbles(self) -> None:
        bubble_w = self._user_bubble_width()
        inner_w = max(120, bubble_w - 24)
        for item in list(getattr(self, "_user_bubbles", [])):
            frame = item.get("frame")
            box = item.get("box")
            try:
                if frame is None or not frame.winfo_exists():
                    continue
                frame.configure(fg_color=INK, border_color=BORDER)
                item["inner_w"] = inner_w
                if box is not None and box.winfo_exists():
                    box.configure(
                        bg=_tone(INK),
                        fg=_tone(USER_TEXT),
                        font=self._px_font(13),
                        selectbackground=_tone(SELECT),
                        selectforeground=_tone(TEXT),
                    )
                    self._sync_bubble_selection(box)
                chip = item.get("chip")
                if chip is not None and chip.winfo_exists():
                    chip.configure(bg=_tone(INK))
            except tk.TclError:
                continue
            self._fit_user_bubble(item)

    def _copy_agent_message(self, text: str) -> None:
        body = (text or "").rstrip()
        if not body:
            return
        set_clipboard(self.chat._textbox, body)
        self.set_status("Скопировано")

    def _restyle_tool_heads(self) -> None:
        for group in getattr(self, "_tool_groups", []):
            head = group.get("head")
            try:
                if head is None or not head.winfo_exists():
                    continue
                head.configure(font=self._px_font(11), fg=_tone(MUTED), bg=_tone(INK))
            except tk.TclError:
                continue

    def _reset_tool_groups(self) -> None:
        self._tool_groups = []
        self._tool_group = None
        self._running_tool = None

    def _close_tool_group(self) -> None:
        group = self._tool_group
        self._tool_group = None
        if group is None or not group["count"]:
            return
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        inner = box._textbox
        start = inner.index("end-1c")
        inner.insert("end", "\n")
        inner.tag_add("toolgap", start, inner.index("end-1c"))

    def _open_tool_group(self, inner) -> dict:
        self._group_seq += 1
        tag = f"toolgrp{self._group_seq}"
        inner.tag_configure(tag, elide=False)
        start = inner.index("end-1c")
        head = tk.Label(
            inner,
            text="",
            anchor="w",
            bd=0,
            padx=4,
            pady=0,
            font=self._px_font(11),
            fg=_tone(MUTED),
            bg=_tone(INK),
            cursor="hand2",
            takefocus=0,
        )
        inner.window_create("end", window=head, padx=0, pady=1)
        inner.insert("end", "\n")
        inner.tag_add("toolhead", start, inner.index("end-1c"))
        group = {"tag": tag, "head": head, "count": 0, "names": [], "open": True}
        head.bind("<Button-1>", lambda _event, item=group: self._toggle_tool_group(item))
        head.bind("<Enter>", lambda _event, item=head: item.configure(fg=_tone(TEXT)))
        head.bind("<Leave>", lambda _event, item=head: item.configure(fg=_tone(MUTED)))
        self._tool_groups.append(group)
        self._tool_group = group
        self._set_group_head(group)
        return group

    def _set_group_head(self, group: dict) -> None:
        head = group.get("head")
        try:
            if head is None or not head.winfo_exists():
                return
            head.configure(text=tool_group_title(group["names"], group["count"], group["open"]))
        except tk.TclError:
            return

    def _toggle_tool_group(self, group: dict) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        group["open"] = not group["open"]
        try:
            box._textbox.tag_configure(group["tag"], elide=not group["open"])
        except tk.TclError:
            return
        self._set_group_head(group)

    def _collapse_tool_groups(self) -> None:
        for group in list(self._tool_groups):
            if group["open"] and group["count"] >= _GROUP_FOLD:
                self._toggle_tool_group(group)

    def _insert_tool_line(self, inner, body: str) -> dict:
        group = self._tool_group or self._open_tool_group(inner)
        start = inner.index("end-1c")
        inner.insert("end", body + "\n")
        end = inner.index("end-1c")
        inner.tag_add("tool", start, end)
        inner.tag_add(group["tag"], start, end)
        return group

    def _show_running_tool(self, line: str) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        self._clear_running_tool()
        box.configure(state="normal")
        inner = box._textbox
        start = inner.index("end-1c")
        self._insert_tool_line(inner, chat_body(line, "tool"))
        self._running_tool = (start, inner.index("end-1c"))
        self._chat_see_end()

    def _clear_running_tool(self) -> None:
        span = self._running_tool
        self._running_tool = None
        if span is None:
            return
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        try:
            box._textbox.delete(span[0], span[1])
        except tk.TclError:
            return

    def _insert_time(self, inner, stamp: str | None) -> None:
        if not stamp:
            inner.insert("end", "\n")
            return
        inner.insert("end", f"{stamp}\n", ("time",))

    def _insert_block(self, box, text: str) -> None:
        inner = box._textbox
        role = self._role(text)
        body = chat_body(text, role)
        _, stamp = split_chat_stamp(text)
        if role == "tool":
            group = self._insert_tool_line(inner, body)
            group["count"] += 1
            name = tool_short_name(body)
            if name and name not in group["names"]:
                group["names"].append(name)
            self._set_group_head(group)
            return
        self._clear_running_tool()
        self._close_tool_group()
        start = inner.index("end-1c")
        copy_text = ""
        if role == "checkpoint":
            checkpoint_id = parse_checkpoint_mark(body) or ""
            item = self.checkpoints.get(checkpoint_id) if checkpoint_id else None
            if item is None:
                return
            self._embed_checkpoint_control(inner, checkpoint_id)
            return
        if role == "user":
            copy_text = user_copy_text(body)
            bubble = self._make_user_bubble(body, copy_text)
            self._user_bubbles.append(bubble)
            inner.window_create("end", window=bubble["frame"], padx=0, pady=4)
            inner.insert("end", "\n")
            self._insert_time(inner, stamp)
        elif role == "agent":
            inner.insert("end", "Ассистент\n", ("label",))
            for chunk, tag in chat_segments(body):
                inner.insert("end", chunk, (tag,) if tag else ())
            if body.strip():
                inner.insert("end", "\n")
                mark = inner.index("end-1c")
                chip = self._make_copy_chip(body)
                inner.window_create("end", window=chip, padx=0, pady=2)
                inner.insert("end", "\n")
                inner.tag_add("copyrow", mark, inner.index("end-1c"))
            self._insert_time(inner, stamp)
        elif role == "error":
            inner.insert("end", body + "\n")
            self._insert_time(inner, stamp)
        else:
            inner.insert("end", body + "\n")
            self._insert_time(inner, stamp)
        inner.tag_add(role, start, inner.index("end-1c"))
        if role == "user" and copy_text:
            self._user_spans.append((start, inner.index("end-1c"), copy_text))

    def _show_transcript(self) -> None:
        box = self.chat
        if not box.winfo_exists():
            return
        self._stream_open = False
        self._user_spans = []
        self._user_bubbles = []
        self._retry_buttons = []
        self._checkpoint_buttons = []
        self._reset_tool_groups()
        box.configure(state="normal")
        box.delete("1.0", "end")
        for line in self.transcript:
            self._insert_block(box, line)
        self._collapse_tool_groups()
        self._chat_see_end(force=True)
        box.configure(state="normal")
        if self._search_open:
            self._run_chat_search()

    def stream_chat(self, text: str, final: bool = False) -> None:
        shown = text or ""
        done = bool(final)
        try:
            self.after(0, lambda shown=shown, done=done: self._apply_stream(shown, done))
        except Exception:
            return

    def _apply_stream(self, text: str, final: bool) -> None:
        shown = (text or "").rstrip()
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            if final and shown:
                self._remember(with_chat_stamp(shown))
            self._stream_open = False
            return
        if not shown and not self._stream_open:
            return
        box.configure(state="normal")
        inner = box._textbox
        if not self._stream_open:
            self._clear_running_tool()
            self._close_tool_group()
            origin = inner.index("end-1c")
            inner.insert("end", "Ассистент\n", ("label",))
            self._stream_origin = origin
            self._stream_body_at = inner.index("end-1c")
            self._stream_open = True
        try:
            if inner.compare(self._stream_body_at, "<", "end-1c"):
                inner.delete(self._stream_body_at, "end-1c")
        except tk.TclError:
            self._stream_open = False
            return
        for chunk, tag in chat_segments(shown):
            inner.insert("end-1c", chunk, (tag,) if tag else ())
        end = inner.index("end-1c")
        try:
            if inner.compare(self._stream_origin, "<", end):
                inner.tag_add("agent", self._stream_origin, end)
        except tk.TclError:
            pass
        if final:
            if shown:
                stamped = with_chat_stamp(shown)
                _, stamp = split_chat_stamp(stamped)
                inner.insert("end", "\n")
                mark = inner.index("end-1c")
                chip = self._make_copy_chip(shown)
                inner.window_create("end", window=chip, padx=0, pady=2)
                inner.insert("end", "\n")
                inner.tag_add("copyrow", mark, inner.index("end-1c"))
                self._insert_time(inner, stamp)
                inner.tag_add("agent", self._stream_origin, inner.index("end-1c"))
                self._remember(stamped)
            self._stream_open = False
        self._chat_see_end()

    def _append(self, box, text: str) -> None:
        def write() -> None:
            if not box.winfo_exists():
                return
            box.configure(state="normal")
            self._insert_block(box, text)
            if box is getattr(self, "chat", None):
                self._chat_see_end()
            else:
                box.see("end")
            if box is getattr(self, "chat", None) and self._search_open and self.search_var.get().strip():
                self._run_chat_search()

        self.after(0, write)

    def _clear_box(self, box) -> None:
        if box is getattr(self, "chat", None):
            self._stream_open = False
            self._user_spans = []
            self._user_bubbles = []
            self._retry_buttons = []
            self._checkpoint_buttons = []
            self._reset_tool_groups()
        box.configure(state="normal")
        box.delete("1.0", "end")

    def _set_box(self, box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        if text:
            box.insert("1.0", text)
        box.configure(state="disabled")
