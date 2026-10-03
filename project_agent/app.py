from __future__ import annotations

import math
import multiprocessing
import re
import shlex
import threading
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from project_agent.agent import Agent
from project_agent.chats import delete_chat, list_chats, load_chat, new_chat_id, save_chat
from project_agent.config import (
    ai_settings,
    config_dir,
    load_config,
    needs_api_key,
    save_agent_mode,
    save_config,
    save_prompt_height,
    save_theme,
)
from project_agent.context_attach import (
    MAX_CONTEXT_DIRS,
    MAX_CONTEXT_FILES,
    at_token_at_end,
    compose_user_text,
    find_at_targets,
    load_explicit_context,
    merge_paths,
    parse_at_paths,
    save_clipboard_image,
)
from project_agent.images import prepare_image
from project_agent.mcp_client import McpHub
from project_agent.paths import PathError, list_entries, read_text_file, relative_posix, resolve_inside
from project_agent.secrets import Scrubber, SecretVault, literals_from_settings, scrub_outbound
from project_agent.index_store import build_index, index_summary
from project_agent.rules import rules_summary
from project_agent.testing import (
    LABEL_BY_PRESET,
    PRESET_LABELS,
    normalize_fix_rounds,
    normalize_preset,
    normalize_timeout,
)
from project_agent.tools import Toolbox

PROVIDER_LABELS = {
    "OpenAI-совместимый": "openai",
    "Anthropic": "anthropic",
}
LABEL_BY_PROVIDER = {value: key for key, value in PROVIDER_LABELS.items()}
MODE_LABELS = {
    "Agent": "agent",
    "Ask": "ask",
}
LABEL_BY_MODE = {value: key for key, value in MODE_LABELS.items()}
THEME_LABELS = {
    "Тёмная": "dark",
    "Светлая": "light",
}
LABEL_BY_THEME = {value: key for key, value in THEME_LABELS.items()}
_PENDING = "\uE000"
_NO_PROFILE = "нет профилей"
INK = ("#f6f3ee", "#1c1916")
PANEL = ("#efeae3", "#26221e")
FIELD = ("#fffdf8", "#161310")
BUTTON = ("#fffdf8", "#2c2824")
BUTTON_HOVER = ("#e6dfd6", "#3a342e")
BORDER = ("#ddd4c8", "#4a433c")
TEXT = ("#2c2824", "#f3eee6")
MUTED = ("#8d847a", "#a3988c")
HINT = ("#c2bbb2", "#5c564f")
USER_TEXT = ("#4a3c30", "#f7f0e4")
USER_BG = ("#e8e0d5", "#322d28")
SELECT = ("#e4dcd2", "#3a342e")
SEND = ("#3c3631", "#efe6da")
SEND_HOVER = ("#2b2724", "#f7f1e8")
ON_SEND = ("#f6f1ea", "#1c1916")
CODE_BG = ("#efe9e1", "#24201c")
CODE_TEXT = ("#0f7b8a", "#6cc7d3")
CTX_OK = ("#5a8f6a", "#6aab7a")
CTX_WARN = ("#c48a2e", "#d4a04a")
CTX_FULL = ("#c45a4a", "#d46a5a")
SEARCH_BG = ("#efe0b8", "#4a3f24")
SEARCH_CUR = ("#e0b86a", "#7a5e28")
CHAT_COLUMN = 820
_ICONS: dict[str, ctk.CTkImage] = {}
_FENCE = re.compile(r"^\s*```")
_INLINE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
_USER_ATTACH_LINE = re.compile(r"^\((?:папки|файлы): .+\)$|^\(изображений: \d+\)$")
_CHAT_STAMP = re.compile(r"\n(\d{2}:\d{2})\s*$")
_RUNNING_TAIL = "выполняется…"
_GROUP_NAMES = 4
# Группы с одним действием не сворачиваем: единственная строка и так информативна.
_GROUP_FOLD = 2
_CHIP_ROWS = 2


def split_chat_stamp(text: str) -> tuple[str, str | None]:
    body = (text or "").rstrip()
    match = _CHAT_STAMP.search(body)
    if not match:
        return body, None
    return body[: match.start()].rstrip(), match.group(1)


def with_chat_stamp(text: str) -> str:
    body, stamp = split_chat_stamp(text)
    if not body:
        return body
    if stamp:
        return f"{body}\n{stamp}"
    return f"{body}\n{datetime.now().strftime('%H:%M')}"


def chat_role(text: str) -> str:
    body, _ = split_chat_stamp(text)
    body = body.lstrip()
    if body.startswith("Вы:"):
        return "user"
    if body.startswith("·"):
        return "tool"
    if body.startswith("Ошибка API:") or body.startswith("Ошибка:"):
        return "error"
    return "agent"


def chat_body(text: str, role: str | None = None) -> str:
    body, _ = split_chat_stamp(text)
    role = role or chat_role(body)
    if role != "user":
        return body
    stripped = body.lstrip()
    if stripped.startswith("Вы:"):
        return stripped[3:].lstrip()
    return body


def user_copy_text(body: str) -> str:
    """Набранный запрос без хвостовых пометок вложений."""
    body, _ = split_chat_stamp(body)
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and _USER_ATTACH_LINE.match(lines[-1].strip()):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def chat_segments(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    fenced = False
    lines = text.split("\n")
    for index, line in enumerate(lines):
        tail = "\n" if index < len(lines) - 1 else ""
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            parts.append((line + "\n", "codeblock"))
            continue
        position = 0
        for match in _INLINE.finditer(line):
            if match.start() > position:
                parts.append((line[position:match.start()], ""))
            if match.group(1) is not None:
                parts.append((match.group(1), "code"))
            else:
                parts.append((match.group(2), "bold"))
            position = match.end()
        parts.append((line[position:] + tail, ""))
    merged: list[tuple[str, str]] = []
    for chunk, tag in parts:
        if not chunk:
            continue
        if merged and merged[-1][1] == tag:
            merged[-1] = (merged[-1][0] + chunk, tag)
        else:
            merged.append((chunk, tag))
    return merged


def is_running_journal(text: str) -> bool:
    body, _ = split_chat_stamp(text)
    return body.rstrip().endswith(_RUNNING_TAIL)


def tool_short_name(body: str) -> str:
    head = (body or "").strip().lstrip("·").strip()
    head = head.split(":", 1)[0]
    head = head.split(" ", 1)[0]
    return head.strip()[:24]


def actions_word(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "действий"
    last = count % 10
    if last == 1:
        return "действие"
    if 2 <= last <= 4:
        return "действия"
    return "действий"


def tool_group_title(names, count: int, opened: bool) -> str:
    total = max(int(count or 0), 0)
    label = f"{total} {actions_word(total)}"
    if opened:
        return f"▾ {label}"
    listed = list(names or [])
    tail = ", ".join(listed[:_GROUP_NAMES])
    if len(listed) > _GROUP_NAMES:
        tail += ", …"
    return f"▸ {label} · {tail}" if tail else f"▸ {label}"


def elide(text: str, width: int, measure) -> str:
    if width <= 0 or measure(text) <= width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


def _tone(color: tuple[str, str]) -> str:
    return color[0] if ctk.get_appearance_mode() == "Light" else color[1]


# ЙЦУКЕН: физические C V X A приходят как с м ч ф.
_RU_CLIPBOARD = {
    "Cyrillic_es": "copy",
    "Cyrillic_ES": "copy",
    "Cyrillic_em": "paste",
    "Cyrillic_EM": "paste",
    "Cyrillic_che": "cut",
    "Cyrillic_CHE": "cut",
    "Cyrillic_ef": "select",
    "Cyrillic_EF": "select",
}
_KEY_CLIPBOARD = {67: "copy", 86: "paste", 88: "cut", 65: "select"}
_LATIN_CLIPBOARD = {"c": "copy", "v": "paste", "x": "cut", "a": "select"}
_CHAT_FREE = {
    "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Caps_Lock", "Num_Lock", "Scroll_Lock",
}
_READONLY_TEXT: set[str] = set()


def clipboard_action(keysym: str, keycode: int) -> str | None:
    symbol = str(keysym or "")
    if symbol in _RU_CLIPBOARD:
        return _RU_CLIPBOARD[symbol]
    lowered = symbol.lower()
    if lowered in _LATIN_CLIPBOARD:
        return _LATIN_CLIPBOARD[lowered]
    # Физическая клавиша (C/V/X/A) при любой раскладке — по keycode Windows.
    return _KEY_CLIPBOARD.get(int(keycode or 0))


def _selection(widget) -> str | None:
    try:
        if widget.winfo_class() == "Text":
            return widget.get("sel.first", "sel.last")
        return widget.selection_get()
    except tk.TclError:
        return None


def set_clipboard(widget, text: str) -> None:
    """Надёжная запись в буфер Windows: clear+append без update часто теряется."""
    if text is None:
        return
    try:
        widget.clipboard_clear()
        widget.clipboard_append(text)
        widget.update_idletasks()
    except tk.TclError:
        return


def apply_layout_clipboard(widget, action: str, text: str | None = None) -> None:
    try:
        if action in {"paste", "cut"} and str(widget) in _READONLY_TEXT:
            return
        if action == "select":
            if widget.winfo_class() == "Text":
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
            else:
                widget.select_range(0, "end")
                widget.icursor("end")
            return
        if action in {"copy", "cut"}:
            selected = text if text is not None else _selection(widget)
            if selected is None:
                return
            set_clipboard(widget, selected)
            if action == "cut":
                widget.delete("sel.first", "sel.last")
            return
        if action == "paste":
            pasted = widget.clipboard_get()
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", pasted)
    except tk.TclError:
        return


def _on_layout_clipboard(event):
    if not (int(getattr(event, "state", 0) or 0) & 0x4):
        return
    action = clipboard_action(str(event.keysym), int(getattr(event, "keycode", 0) or 0))
    if not action:
        return
    apply_layout_clipboard(event.widget, action)
    return "break"


def _paint_folder(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((5, 13, 27, 26), radius=3, outline=color, width=2)
    draw.line((6, 14, 12, 14, 15, 8, 22, 8), fill=color, width=2)


def _paint_sliders(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((7, 10, 25, 10), fill=color, width=2)
    draw.ellipse((14, 7, 20, 13), outline=color, width=2)
    draw.line((7, 16, 25, 16), fill=color, width=2)
    draw.ellipse((10, 13, 16, 19), outline=color, width=2)
    draw.line((7, 22, 25, 22), fill=color, width=2)
    draw.ellipse((16, 19, 22, 25), outline=color, width=2)


def _paint_plus(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((16, 8, 16, 24), fill=color, width=2)
    draw.line((8, 16, 24, 16), fill=color, width=2)


def _paint_refresh(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.arc((7, 8, 25, 26), start=40, end=310, fill=color, width=2)
    draw.polygon([(22, 6), (27, 12), (19, 13)], fill=color)


def _paint_trash(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((8, 11, 24, 11), fill=color, width=2)
    draw.line((13, 11, 13, 8, 19, 8, 19, 11), fill=color, width=2)
    draw.rounded_rectangle((9, 13, 23, 26), radius=2, outline=color, width=2)
    draw.line((13, 16, 13, 23), fill=color, width=2)
    draw.line((19, 16, 19, 23), fill=color, width=2)


def _paint_save(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((16, 6, 16, 18), fill=color, width=2)
    draw.line((10, 14, 16, 20, 22, 14), fill=color, width=2)
    draw.line((8, 24, 24, 24), fill=color, width=2)


def _paint_close(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((9, 9, 23, 23), fill=color, width=2)
    draw.line((23, 9, 9, 23), fill=color, width=2)


def _paint_image(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((5, 8, 27, 24), radius=3, outline=color, width=2)
    draw.ellipse((18, 11, 22, 15), outline=color, width=2)
    draw.line((8, 20, 13, 15, 17, 19, 21, 15, 25, 20), fill=color, width=2)


def _paint_send(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((16, 24, 16, 8), fill=color, width=2)
    draw.line((9, 15, 16, 8, 23, 15), fill=color, width=2)


def _paint_stop(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((9, 9, 23, 23), radius=3, outline=color, width=2)


def _paint_check(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.line((8, 17, 14, 23, 24, 10), fill=color, width=2)


def _paint_halt(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((10, 10, 22, 22), radius=2, fill=color)


def _paint_gear(draw: ImageDraw.ImageDraw, color: str) -> None:
    for step in range(8):
        angle = math.pi * step / 4
        draw.line(
            (
                16 + 7 * math.cos(angle),
                16 + 7 * math.sin(angle),
                16 + 11 * math.cos(angle),
                16 + 11 * math.sin(angle),
            ),
            fill=color,
            width=3,
        )
    draw.ellipse((9, 9, 23, 23), outline=color, width=2)
    draw.ellipse((13, 13, 19, 19), outline=color, width=2)


def _paint_dots(draw: ImageDraw.ImageDraw, color: str) -> None:
    for x in (8, 16, 24):
        draw.ellipse((x - 2, 14, x + 2, 18), fill=color)


def _paint_copy(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((11, 7, 24, 20), radius=2, outline=color, width=2)
    draw.rounded_rectangle((7, 11, 20, 25), radius=2, outline=color, width=2)


_PAINT = {
    "folder": _paint_folder,
    "sliders": _paint_sliders,
    "plus": _paint_plus,
    "refresh": _paint_refresh,
    "trash": _paint_trash,
    "save": _paint_save,
    "close": _paint_close,
    "image": _paint_image,
    "send": _paint_send,
    "stop": _paint_stop,
    "check": _paint_check,
    "halt": _paint_halt,
    "gear": _paint_gear,
    "dots": _paint_dots,
    "copy": _paint_copy,
}


def glyph(name: str, invert: bool = False) -> ctk.CTkImage:
    key = f"{name}:{int(invert)}"
    cached = _ICONS.get(key)
    if cached is not None:
        return cached
    light = ON_SEND[0] if invert else TEXT[0]
    dark = ON_SEND[1] if invert else TEXT[1]

    def sheet(color: str) -> Image.Image:
        image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        _PAINT[name](ImageDraw.Draw(image), color)
        return image

    icon = ctk.CTkImage(light_image=sheet(light), dark_image=sheet(dark), size=(16, 16))
    _ICONS[key] = icon
    return icon


def quiet_button(parent, text, command, width=96, primary=False, mark=None, round_mark=False):
    if round_mark:
        return ctk.CTkButton(
            parent,
            text="",
            image=glyph("send", invert=True),
            width=40,
            height=40,
            corner_radius=20,
            border_width=0,
            fg_color=SEND,
            hover_color=SEND_HOVER,
            command=command,
        )
    return ctk.CTkButton(
        parent,
        text=text,
        image=None if mark is None else glyph(mark, invert=primary),
        width=width,
        height=32,
        corner_radius=16,
        border_width=0 if primary else 1,
        border_color=BORDER,
        fg_color=SEND if primary else BUTTON,
        hover_color=SEND_HOVER if primary else BUTTON_HOVER,
        text_color=ON_SEND if primary else TEXT,
        font=("Segoe UI", 12),
        command=command,
    )


def icon_button(parent, mark, command, size=32, fg=None):
    return ctk.CTkButton(
        parent,
        text="",
        image=glyph(mark),
        width=size,
        height=size,
        corner_radius=size // 2,
        border_width=0,
        fg_color=fg or "transparent",
        hover_color=BUTTON_HOVER,
        command=command,
    )


def quiet_menu(parent, variable, values, command=None):
    return ctk.CTkOptionMenu(
        parent,
        variable=variable,
        values=values,
        command=command,
        height=34,
        corner_radius=12,
        fg_color=BUTTON,
        button_color=BUTTON_HOVER,
        button_hover_color=BORDER,
        text_color=TEXT,
        font=("Segoe UI", 12),
        dropdown_fg_color=PANEL,
        dropdown_text_color=TEXT,
        dropdown_hover_color=SELECT,
        dropdown_font=("Segoe UI", 12),
    )


class ConfirmDialog(ctk.CTkToplevel):
    def __init__(self, master, path: str, summary: str, detail: str = "") -> None:
        super().__init__(master)
        self.result = False
        self._closed = False
        self.title("Подтверждение")
        tall = bool((detail or "").strip())
        self.geometry("560x420" if tall else "520x220")
        self.minsize(480, 200)
        self.resizable(True, True)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2 if tall else 1, weight=1)
        ctk.CTkLabel(self, text=path, wraplength=520, justify="left", anchor="w", text_color=TEXT).grid(
            row=0, column=0, sticky="ew", padx=16, pady=(16, 8)
        )
        ctk.CTkLabel(self, text=summary, wraplength=520, justify="left", anchor="w", text_color=MUTED).grid(
            row=1, column=0, sticky="ew", padx=16, pady=4
        )
        if tall:
            box = ctk.CTkTextbox(
                self,
                fg_color=FIELD,
                text_color=TEXT,
                border_width=1,
                border_color=BORDER,
                corner_radius=10,
                font=("Consolas", 11),
                wrap="none",
            )
            box.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
            box.insert("1.0", detail.strip())
            box.configure(state="disabled")
            button_row = 3
        else:
            button_row = 2
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=button_row, column=0, pady=(8, 16))
        quiet_button(row, "Нет", self.refuse, width=110, mark="close").pack(side="left", padx=8)
        quiet_button(row, "Да", self.allow, width=110, primary=True, mark="check").pack(side="left", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.refuse)
        self.bind("<Escape>", lambda _event: self.refuse())
        self.after(50, self.focus)

    def allow(self) -> None:
        if self._closed:
            return
        self._closed = True
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


class NameDialog(ctk.CTkToplevel):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.result = ""
        self._closed = False
        self.title("Профиль")
        self.geometry("420x168")
        self.resizable(False, False)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        ctk.CTkLabel(self, text="Имя профиля", anchor="w", text_color=MUTED).pack(fill="x", padx=16, pady=(16, 4))
        self.entry = ctk.CTkEntry(
            self,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            height=36,
            corner_radius=12,
        )
        self.entry.pack(fill="x", padx=16, pady=4)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=12)
        quiet_button(row, "Отмена", self._cancel, width=120, mark="close").pack(side="left", padx=8)
        quiet_button(row, "Сохранить", self._ok, width=140, primary=True, mark="save").pack(side="left", padx=8)
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


class SettingsWindow(ctk.CTkToplevel):
    _SECTIONS = ("Внешний вид", "Модель", "Изображения", "Проект", "MCP", "Безопасность")

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self._pages: dict[str, ctk.CTkScrollableFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        self.title("Настройки")
        self.geometry("640x720")
        self.minsize(560, 520)
        self.configure(fg_color=INK)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        nav = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16, width=168)
        nav.grid(row=0, column=0, sticky="nsw", padx=(12, 6), pady=12)
        nav.grid_propagate(False)
        nav.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(nav, text="Разделы", anchor="w", text_color=MUTED, font=("Segoe UI", 11)).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(12, 8)
        )
        for index, name in enumerate(self._SECTIONS, start=1):
            button = ctk.CTkButton(
                nav,
                text=name,
                anchor="w",
                height=34,
                corner_radius=10,
                border_width=0,
                fg_color="transparent",
                hover_color=BUTTON_HOVER,
                text_color=TEXT,
                font=("Segoe UI", 12),
                command=lambda section=name: self._show(section),
            )
            button.grid(row=index, column=0, sticky="ew", padx=8, pady=2)
            self._nav_buttons[name] = button

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        for name in self._SECTIONS:
            page = ctk.CTkScrollableFrame(
                body,
                fg_color=PANEL,
                corner_radius=16,
                scrollbar_button_color=BUTTON,
                scrollbar_button_hover_color=BUTTON_HOVER,
            )
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[name] = page

        self._fill_appearance(self._pages["Внешний вид"])
        self._fill_model(self._pages["Модель"])
        self._fill_images(self._pages["Изображения"])
        self._fill_project(self._pages["Проект"])
        self._fill_mcp(self._pages["MCP"])
        self._fill_security(self._pages["Безопасность"])

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        quiet_button(footer, "Сохранить", app.save_settings, width=160, primary=True, mark="check").pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._show("Модель")
        self.after(50, self.focus)

    def _fill_appearance(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Внешний вид", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app._field(page, "Тема")
        quiet_menu(page, app.theme_var, list(THEME_LABELS), app._on_theme_pick).pack(fill="x", padx=8, pady=4)

    def _fill_model(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Модель и профили", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app._field(page, "Профиль")
        app.profile_menu = quiet_menu(page, app.profile_var, [_NO_PROFILE], app._on_profile_pick)
        app.profile_menu.pack(fill="x", padx=8, pady=4)
        app._sync_profile_menu()
        profile_row = ctk.CTkFrame(page, fg_color="transparent")
        profile_row.pack(fill="x", padx=8, pady=4)
        quiet_button(profile_row, "Сохранить профиль", app.save_profile, width=188, mark="save").pack(
            side="left", padx=(0, 4)
        )
        quiet_button(profile_row, "Удалить", app.delete_profile, width=112, mark="trash").pack(side="left")
        app._field(page, "Провайдер")
        quiet_menu(page, app.provider_var, list(PROVIDER_LABELS)).pack(fill="x", padx=8, pady=4)
        app._entry(page, "URL API", app.base_url_var, "пусто — OpenAI или Anthropic")
        app._entry(page, "Модель", app.model_var, "имя модели")
        app._entry(page, "Ключ", app.api_key_var, "", secret=True)
        app._entry(page, "Лимит шагов", app.steps_var, "25")

    def _fill_images(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Изображения", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkLabel(
            page,
            text="Пустые поля — генерация выключена или берутся значения из раздела «Модель».",
            wraplength=400,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(0, 8))
        app._entry(page, "Модель изображений", app.image_model_var, "пусто — генерация выключена")
        app._entry(page, "URL изображений", app.image_url_var, "пусто — URL API")
        app._entry(page, "Ключ изображений", app.image_key_var, "пусто — основной ключ", secret=True)

    def _fill_project(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Проект и тесты", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkLabel(
            page,
            text="Агент может запускать только выбранный пресет. Произвольный терминал недоступен. Каждый запуск спрашивает подтверждение.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(0, 8))
        app._field(page, "Пресет тестов")
        quiet_menu(page, app.test_preset_var, list(PRESET_LABELS), app._on_test_preset_pick).pack(
            fill="x", padx=8, pady=4
        )
        app._entry(page, "Таймаут тестов (сек)", app.test_timeout_var, "120")
        app._entry(page, "Лимит запусков тестов за ход", app.test_fix_rounds_var, "3")
        app._field(page, "Правила проекта")
        app.rules_label = ctk.CTkLabel(
            page,
            text="Правила не найдены (AGENTS.md или .projectagent/rules).",
            anchor="w",
            justify="left",
            wraplength=400,
            text_color=MUTED,
        )
        app.rules_label.pack(fill="x", padx=8, pady=4)
        app._field(page, "Индекс файлов")
        app.index_label = ctk.CTkLabel(page, text="Индекс ещё не построен.", anchor="w", text_color=MUTED)
        app.index_label.pack(fill="x", padx=8, pady=4)
        quiet_button(page, "Обновить индекс", app.refresh_index, width=180, mark="refresh").pack(
            fill="x", padx=8, pady=4
        )
        app._refresh_rules_label()
        app._refresh_index_label()

    def _fill_mcp(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="MCP-серверы", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app.mcp_list = ctk.CTkTextbox(
            page,
            height=120,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
        )
        app.mcp_list.pack(fill="x", padx=8, pady=4)
        app.mcp_list.configure(state="disabled")
        app._render_mcp()
        app._entry(page, "Имя MCP", app.mcp_name_var, "filesystem")
        app._entry(page, "Команда MCP", app.mcp_command_var, "python -m server")
        mcp_row = ctk.CTkFrame(page, fg_color="transparent")
        mcp_row.pack(fill="x", padx=8, pady=4)
        quiet_button(mcp_row, "Добавить", app.add_mcp, width=124, mark="plus").pack(side="left", padx=(0, 4))
        quiet_button(mcp_row, "Удалить", app.remove_mcp, width=112, mark="trash").pack(side="left", padx=(0, 4))
        quiet_button(mcp_row, "Браузер Playwright", app.add_browser_mcp, width=168).pack(side="left")
        ctk.CTkLabel(
            page,
            text=(
                "Браузер — через MCP Playwright (кнопка выше), не встроенный Chrome. "
                "Нужен Node.js (npx). Агент вызывает инструмент browser: открыть → snapshot → клик.\n"
                "Переменные env для сервера дописываются в config.json вручную."
            ),
            wraplength=400,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(8, 4))

    def _fill_security(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Безопасность", anchor="w", text_color=TEXT, font=("Segoe UI", 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkSwitch(
            page,
            text="Разрешить правки в папке проекта без подтверждения",
            variable=app.auto_write_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=("Segoe UI", 12),
        ).pack(fill="x", padx=8, pady=(8, 4))
        notes = (
            "Ключ и настройки хранятся только в %APPDATA%\\ProjectAgent\\config.json — в программу они не зашиты.\n\n"
            "Перед отправкой модели пароли и похожие значения заменяются метками [[SEC:...]].\n\n"
            "По умолчанию запись файла спрашивает подтверждение. Переключатель выше снимает диалог "
            "только для write_file / apply_patch внутри открытой папки проекта.\n\n"
            "Генерация изображения, запуск тестов и git commit всегда спрашивают подтверждение.\n\n"
            "Команд произвольного терминала нет. Тесты — только пресет unittest, pytest или npm test из настроек.\n\n"
            "Браузер не встроен: только MCP Playwright из настроек (если добавлен).\n\n"
            "Агент не выходит за выбранную папку проекта."
        )
        ctk.CTkLabel(page, text=notes, wraplength=400, justify="left", anchor="w", text_color=MUTED).pack(
            fill="x", padx=8, pady=4
        )

    def _show(self, name: str) -> None:
        if name not in self._pages:
            return
        for section, page in self._pages.items():
            if section == name:
                page.grid()
            else:
                page.grid_remove()
        for section, button in self._nav_buttons.items():
            active = section == name
            button.configure(fg_color=BUTTON if active else "transparent", hover_color=BUTTON_HOVER if active else SELECT)

    def _close(self) -> None:
        self.app.mcp_list = None
        self.app.profile_menu = None
        self.app.index_label = None
        self.app.rules_label = None
        self.app._settings_window = None
        self.destroy()


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ProjectAgent")
        self.geometry("1100x760")
        self.minsize(900, 560)
        self.configure(fg_color=INK)
        self.project: Path | None = None
        self.attached: list[str] = []
        self.context_files: list[str] = []
        self.context_dirs: list[str] = []
        self._at_popup: tk.Toplevel | None = None
        self._at_list: tk.Listbox | None = None
        self._at_start: str | None = None
        self.mcp_servers: list[dict] = []
        self.saved_servers: list[dict] = []
        self.stop_event = threading.Event()
        self.running = False
        self._dialog: ConfirmDialog | None = None
        self._settings_window: SettingsWindow | None = None
        self.mcp_list: ctk.CTkTextbox | None = None
        self.profile_menu: ctk.CTkOptionMenu | None = None
        self.index_label: ctk.CTkLabel | None = None
        self.rules_label: ctk.CTkLabel | None = None
        self.profiles: list[dict] = []
        self.active_profile = ""
        self.profile_var = ctk.StringVar(value=_NO_PROFILE)
        self.footer_profile_menu: ctk.CTkOptionMenu | None = None
        self.mode_menu: ctk.CTkOptionMenu | None = None
        self._chat_pad = 0
        self.chat_id: str | None = None
        self.chat_title = ""
        self.transcript: list[str] = []
        self._chat_list_lock = False
        self._stream_open = False
        self._stream_origin = "1.0"
        self._stream_body_at = "1.0"
        self._user_spans: list[tuple[str, str, str]] = []
        self._retry_payload: dict | None = None
        self._can_retry = False
        self._retry_buttons: list[tk.Button] = []
        self._tool_groups: list[dict] = []
        self._tool_group: dict | None = None
        self._group_seq = 0
        self._running_tool: tuple[str, str] | None = None
        self._chip_width = 0
        self._chat_stick = True
        self._search_open = False
        self._search_hits: list[tuple[str, str]] = []
        self._search_index = -1
        self._search_query = ""
        self._closing = False
        self.vault = SecretVault()
        self.mcp = McpHub()
        self.toolbox = Toolbox(self.vault, self.confirm, self.mcp, self.collect_settings)
        self.agent = Agent(
            self.vault,
            self.toolbox,
            self.mcp,
            self.write_chat,
            self.write_journal,
            self.set_status,
            self._on_context,
            self.stream_chat,
            self.write_retryable_error,
        )
        self.theme = "dark"
        self.theme_var = ctk.StringVar(value="Тёмная")
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_width = 420
        self.prompt_height = 0
        self.agent_mode = "agent"
        self.agent_mode_var = ctk.StringVar(value="Agent")
        self.context_limit = 256000
        self.context_used = 0
        self.context_from_api = False
        self.provider_var = ctk.StringVar(value="OpenAI-совместимый")
        self.base_url_var = ctk.StringVar()
        self.model_var = ctk.StringVar()
        self.api_key_var = ctk.StringVar()
        self.steps_var = ctk.StringVar(value="25")
        self.image_model_var = ctk.StringVar()
        self.image_url_var = ctk.StringVar()
        self.image_key_var = ctk.StringVar()
        self.mcp_name_var = ctk.StringVar()
        self.mcp_command_var = ctk.StringVar()
        self.test_preset = ""
        self.test_preset_var = ctk.StringVar(value="Выключено")
        self.test_timeout_var = ctk.StringVar(value="120")
        self.test_fix_rounds_var = ctk.StringVar(value="3")
        self.auto_write_project = False
        self.auto_write_var = ctk.BooleanVar(value=False)
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._ui_error

    def _build(self) -> None:
        self.bind_class("Text", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.bind_class("Entry", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.grid_columnconfigure(0, weight=0, minsize=56)
        self.grid_columnconfigure(1, weight=0, minsize=1)
        self.grid_columnconfigure(2, weight=0, minsize=248)
        self.grid_columnconfigure(3, weight=1)
        self.grid_rowconfigure(0, weight=1)

        rail = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=56)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_rowconfigure(1, weight=1)
        self.project_badge = ctk.CTkButton(
            rail,
            text="·",
            width=36,
            height=36,
            corner_radius=10,
            border_width=1,
            border_color=BORDER,
            fg_color=BUTTON,
            hover_color=BUTTON_HOVER,
            text_color=TEXT,
            font=self._font(14, weight="bold"),
            command=self.choose_folder,
        )
        self.project_badge.grid(row=0, column=0, padx=10, pady=(12, 0))
        icon_button(rail, "gear", self.open_settings, size=36).grid(row=2, column=0, padx=10, pady=(0, 12))
        ctk.CTkFrame(self, fg_color=BORDER, corner_radius=0, width=1).grid(row=0, column=1, sticky="ns")

        side = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=248)
        side.grid(row=0, column=2, sticky="nsew")
        side.grid_propagate(False)
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
        icon_button(head, "dots", self.choose_folder, size=28).grid(row=0, column=1, rowspan=2, padx=(4, 0))
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
        files_top = ctk.CTkFrame(self.file_pane, fg_color="transparent")
        files_top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        files_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(files_top, text="Структура", anchor="w", text_color=MUTED, font=self._font(12)).grid(
            row=0, column=0, sticky="w", padx=(6, 0)
        )
        icon_button(files_top, "refresh", self._refresh_tree, size=28).grid(row=0, column=1)
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
        self.chat_tree = ttk.Treeview(self.chat_pane, show="tree", selectmode="browse", style="Chats.Treeview")
        self.chat_tree.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ctk.CTkScrollbar(
            self.chat_pane, command=self.chat_tree.yview, fg_color=PANEL, button_color=BUTTON, button_hover_color=BUTTON_HOVER
        )
        chat_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.chat_tree.configure(yscrollcommand=chat_scroll.set)
        self.chat_tree.bind("<<TreeviewSelect>>", self._on_chat_select)
        self.chat_tree.bind("<Button-3>", self._chat_menu)
        self.side_tabs.set("Чаты")
        self._show_side_tab("Чаты")

        self.work = tk.PanedWindow(
            self,
            orient="horizontal",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_h_double_arrow",
        )
        self.work.grid(row=0, column=3, sticky="nsew")
        self.work.bind("<ButtonRelease-1>", self._remember_editor_width)
        self.center = ctk.CTkFrame(self.work, fg_color=INK, corner_radius=0)
        self.center.grid_columnconfigure(0, weight=1)
        self.center.grid_rowconfigure(1, weight=1)
        self.work.add(self.center, stretch="always", minsize=360, sticky="nsew")
        self.chat_head = ctk.CTkLabel(
            self.center, text="Новый чат", anchor="w", text_color=TEXT, font=self._font(13, weight="bold")
        )
        self.chat_head.grid(row=0, column=0, sticky="ew", padx=24, pady=(12, 6))
        self.chat_head.bind("<Configure>", lambda _event: self._fit_labels())
        self.stack = tk.PanedWindow(
            self.center,
            orient="vertical",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_v_double_arrow",
        )
        self.stack.grid(row=1, column=0, sticky="nsew")
        self.stack.bind("<ButtonRelease-1>", self._remember_prompt_height)
        chat_holder = ctk.CTkFrame(self.stack, fg_color=INK, corner_radius=0)
        self.chat_holder = chat_holder
        chat_holder.grid_columnconfigure(0, weight=1)
        chat_holder.grid_rowconfigure(1, weight=1)
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
            font=("Segoe UI", 13),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.chat.grid(row=1, column=0, sticky="nsew", padx=(8, 0))
        self.stack.add(chat_holder, stretch="always", minsize=140, sticky="nsew")
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
        _READONLY_TEXT.add(str(self.chat._textbox))

        self.editor_frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        self.editor_frame.grid_columnconfigure(0, weight=1)
        self.editor_frame.grid_rowconfigure(1, weight=1)
        editor_head = ctk.CTkFrame(self.editor_frame, fg_color="transparent")
        editor_head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        editor_head.grid_columnconfigure(0, weight=1)
        self.editor_title = ctk.CTkLabel(editor_head, text="", anchor="w", text_color=TEXT)
        self.editor_title.grid(row=0, column=0, sticky="ew")
        quiet_button(editor_head, "Сохранить", self._save_editor, width=128, mark="save").grid(row=0, column=1, padx=(8, 4))
        quiet_button(editor_head, "Закрыть", self._close_editor, width=112, mark="close").grid(row=0, column=2)
        self.editor_box = ctk.CTkTextbox(
            self.editor_frame,
            wrap="none",
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=("Consolas", 13),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.editor_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.editor_box._textbox.bind("<Control-KeyPress>", _on_layout_clipboard, add="+")

        bottom = ctk.CTkFrame(self.stack, fg_color=INK, corner_radius=0)
        self.bottom = bottom
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(0, weight=1)
        self.stack.add(bottom, stretch="never", minsize=150, sticky="nsew")
        bottom.bind("<Configure>", self._center_composer, add="+")
        card = ctk.CTkFrame(bottom, fg_color=FIELD, corner_radius=16, border_width=1, border_color=BORDER)
        self.card = card
        card.grid(row=0, column=0, sticky="nsew", padx=24, pady=(6, 0))
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(1, weight=1)
        self.attach_row = ctk.CTkFrame(card, fg_color="transparent")
        self.attach_row.grid(row=0, column=0, columnspan=3, sticky="ew", padx=10, pady=(8, 0))
        self.attach_row.grid_remove()
        self.attach_row.bind("<Configure>", self._on_attach_resize, add="+")
        self.task = ctk.CTkTextbox(
            card,
            height=56,
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=("Segoe UI", 13),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.task.grid(row=1, column=0, columnspan=3, sticky="nsew", padx=10, pady=(8, 0))
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
        self._sync_placeholder()
        self.image_button = icon_button(card, "plus", self._attach_menu, size=32)
        self.image_button.grid(row=2, column=0, padx=(8, 4), pady=8, sticky="w")
        self.send_button = quiet_button(card, "", self._send_or_stop, round_mark=True)
        self.send_button.configure(width=34, height=34, corner_radius=17)
        self.send_button.grid(row=2, column=2, padx=(4, 8), pady=8, sticky="e")
        inner.bind("<KeyRelease>", self._on_task_key, add="+")
        inner.bind("<Escape>", self._hide_at_popup, add="+")
        inner.bind("<Down>", self._at_move, add="+")
        inner.bind("<Up>", self._at_move, add="+")
        inner.bind("<Tab>", self._at_accept_key, add="+")
        footer = ctk.CTkFrame(bottom, fg_color="transparent")
        self.footer = footer
        footer.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 8))
        footer.grid_columnconfigure(4, weight=1)
        self.footer_profile_menu = ctk.CTkOptionMenu(
            footer,
            variable=self.profile_var,
            values=[_NO_PROFILE],
            command=self._on_profile_pick,
            height=26,
            width=60,
            corner_radius=8,
            dynamic_resizing=True,
            fg_color=INK,
            button_color=INK,
            button_hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(12),
            dropdown_fg_color=PANEL,
            dropdown_text_color=TEXT,
            dropdown_hover_color=SELECT,
            dropdown_font=self._font(12),
        )
        self.footer_profile_menu.grid(row=0, column=0, sticky="w")
        self.mode_menu = ctk.CTkOptionMenu(
            footer,
            variable=self.agent_mode_var,
            values=list(MODE_LABELS),
            command=self._on_mode_pick,
            height=26,
            width=88,
            corner_radius=8,
            dynamic_resizing=False,
            fg_color=INK,
            button_color=INK,
            button_hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(12),
            dropdown_fg_color=PANEL,
            dropdown_text_color=TEXT,
            dropdown_hover_color=SELECT,
            dropdown_font=self._font(12),
        )
        self.mode_menu.grid(row=0, column=1, sticky="w", padx=(8, 0))
        quiet_button(footer, "Сжать", self.compress_context, width=72).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        self.model_label = ctk.CTkLabel(
            footer,
            text="модель не выбрана",
            anchor="w",
            text_color=MUTED,
            font=self._font(11),
        )
        self.model_label.grid(row=0, column=3, sticky="w", padx=(10, 0))
        ctx = ctk.CTkFrame(footer, fg_color="transparent")
        ctx.grid(row=0, column=4, sticky="e", padx=(8, 8))
        self.context_label = ctk.CTkLabel(
            ctx,
            text="0%",
            anchor="e",
            text_color=MUTED,
            font=self._font(11),
        )
        self.context_label.pack(side="left", padx=(0, 6))
        self.context_bar = ctk.CTkProgressBar(
            ctx,
            width=96,
            height=8,
            corner_radius=4,
            progress_color=CTX_OK,
            fg_color=BORDER,
        )
        self.context_bar.pack(side="left")
        self.context_bar.set(0)
        self.status_label = ctk.CTkLabel(footer, text="Готово", anchor="e", text_color=MUTED, font=self._font(11))
        self.status_label.grid(row=0, column=5, sticky="e")

    def _font(self, size: int, family: str = "Segoe UI", weight: str = "normal") -> tuple:
        return (family, size, weight)

    def _px_font(self, size: int, family: str = "Segoe UI", weight: str = "normal") -> tuple:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        return (family, -round(size * scale), weight)

    def _measure(self, size: int, weight: str = "normal"):
        key = (size, weight)
        cache = self.__dict__.setdefault("_measure_fonts", {})
        font = cache.get(key)
        if font is None:
            font = tkfont.Font(font=self._px_font(size, weight=weight))
            cache[key] = font
        return font.measure

    def _fit_labels(self) -> None:
        if self._closing or not hasattr(self, "chat_head"):
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self.project is None:
            name, path = "Папка не выбрана", "нажмите «…», чтобы выбрать"
        else:
            name, path = self.project.name or str(self.project), str(self.project)
        title = self.chat_title or "Новый чат"
        for label, text, size, weight in (
            (self.folder_name, name, 13, "bold"),
            (self.folder_path, path, 11, "normal"),
            (self.chat_head, title, 13, "bold"),
        ):
            try:
                if not label.winfo_exists():
                    continue
                width = label.winfo_width() - 4
                shown = elide(text, width, self._measure(size, weight)) if width > 20 else text
                if label.cget("text") != shown:
                    label.configure(text=shown)
            except tk.TclError:
                continue

    def _show_folder(self) -> None:
        letter = (self.project.name[:1] if self.project is not None else "") or "·"
        self.project_badge.configure(text=letter.upper())
        self._fit_labels()
        self._refresh_tree()
        self._refresh_chat_list()
        self._refresh_index_label()
        self._refresh_rules_label()

    def _refresh_rules_label(self) -> None:
        label = self.rules_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            self.rules_label = None
            return
        if self.project is None:
            label.configure(text="Выберите папку проекта.")
            return
        try:
            label.configure(text=rules_summary(self.project))
        except Exception as exc:
            label.configure(text=f"Правила недоступны: {exc}")

    def _refresh_index_label(self) -> None:
        label = self.index_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            self.index_label = None
            return
        if self.project is None:
            label.configure(text="Выберите папку проекта.")
            return
        try:
            label.configure(text=index_summary(self.project))
        except Exception as exc:
            label.configure(text=f"Индекс недоступен: {exc}")

    def refresh_index(self) -> None:
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        try:
            data = build_index(self.project)
        except Exception as exc:
            self.write_chat(f"Индекс не обновлён: {exc}")
            return
        self._refresh_index_label()
        self.set_status(f"Индекс: {len(data.get('files') or [])} файлов")

    def _show_side_tab(self, name: str) -> None:
        if name == "Файлы":
            self.chat_pane.grid_remove()
            self.file_pane.grid()
        else:
            self.file_pane.grid_remove()
            self.chat_pane.grid()

    def _column_pad(self, width: int, least: int) -> int:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        column = round(CHAT_COLUMN * scale)
        return max(least, (width - column) // 2)

    def _center_chat(self, event=None) -> None:
        width = event.width if event is not None else self.chat._textbox.winfo_width()
        pad = self._column_pad(width, 16)
        if pad == self._chat_pad:
            return
        self._chat_pad = pad
        self.chat._textbox.configure(padx=pad)

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

    def _send_or_stop(self) -> None:
        if self.running:
            self.stop()
        else:
            self.send()

    def _show_running(self, running: bool) -> None:
        self.send_button.configure(image=glyph("halt" if running else "send", invert=True))

    def _chat_menu(self, event) -> str | None:
        row = self.chat_tree.identify_row(event.y)
        if not row or self.running:
            return None
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
        menu.add_command(label="Удалить", command=lambda: self.delete_chat_item(str(row)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _style_tree(self) -> None:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "Project.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            foreground=_tone(TEXT),
            borderwidth=0,
            rowheight=24,
            font=("Segoe UI", 10),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", _tone(SELECT))],
            foreground=[("selected", _tone(TEXT))],
        )
        style.configure(
            "Chats.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            foreground=_tone(TEXT),
            borderwidth=0,
            rowheight=32,
            indent=0,
            font=("Segoe UI", 10),
        )
        style.layout("Chats.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Project.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Chats.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [("Treeitem.text", {"sticky": "nswe"})]})])
        style.map(
            "Chats.Treeview",
            background=[("selected", _tone(SELECT))],
            foreground=[("selected", _tone(TEXT))],
        )

    def _refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        reopen = self._expanded_paths()
        self.tree.delete(*self.tree.get_children())
        if self.project is None or not self.project.is_dir():
            self.tree.insert("", "end", text="Папка не выбрана")
            return
        self.tree.insert("", "end", iid=".", text=self.project.name or str(self.project), open=True, tags=("dir",))
        self._fill_node(".", reopen)

    def _expanded_paths(self) -> set[str]:
        found: set[str] = set()

        def walk(node: str) -> None:
            for child in self.tree.get_children(node):
                child = str(child)
                if child.startswith(_PENDING):
                    continue
                if self.tree.item(child, "open"):
                    found.add(child)
                    walk(child)

        walk("")
        return found

    def _fill_node(self, iid: str, reopen: set[str]) -> None:
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        try:
            entries, truncated = list_entries(self.project, iid)
        except PathError:
            self.tree.insert(iid, "end", text="(не прочитано)")
            return
        for name, is_dir in entries:
            rel = name if iid == "." else f"{iid}/{name}"
            self.tree.insert(
                iid, "end", iid=rel, text=name, open=False, tags=("dir",) if is_dir else ("file",)
            )
            if not is_dir:
                continue
            if rel in reopen:
                self.tree.item(rel, open=True)
                self._fill_node(rel, reopen)
            else:
                self.tree.insert(rel, "end", iid=f"{_PENDING}{rel}", text="")
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
            self._fill_node(iid, set())

    def _on_file_click(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected or self.project is None:
            return
        relative = str(selected[0])
        if "file" not in self.tree.item(relative, "tags"):
            return
        if self.editor_open and relative == self.editor_path:
            return
        try:
            text = self._editor_source(relative)
        except PathError as exc:
            self.write_chat(f"Файл не открыт: {exc}")
            return
        if self.editor_open and self._editor_dirty():
            if not self._ask(self.editor_path, "Открыть другой файл без записи?"):
                return
        self._show_editor(relative, text)

    def _show_editor(self, relative: str, text: str) -> None:
        self.editor_path = relative
        self.editor_saved = text
        self.editor_title.configure(text=relative)
        self.editor_box.delete("1.0", "end")
        if text:
            self.editor_box.insert("1.0", text)
        self.editor_box.mark_set("insert", "1.0")
        self.editor_box.see("1.0")
        if not self.editor_open:
            self.work.add(self.editor_frame, stretch="always", minsize=240, sticky="nsew")
            self.editor_open = True
            self.after(30, self._place_editor_sash)
        self.editor_box.focus_set()

    def _reload_clean_editor(self) -> None:
        if not self.editor_open or self.project is None or self._editor_dirty():
            return
        try:
            text = self._editor_source(self.editor_path)
        except PathError:
            return
        if text != self.editor_saved:
            self._show_editor(self.editor_path, text)

    def _editor_source(self, relative: str) -> str:
        text = read_text_file(self.project, relative)
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _editor_text(self) -> str:
        return self.editor_box.get("1.0", "end-1c")

    def _editor_dirty(self) -> bool:
        return self.editor_open and self._editor_text() != self.editor_saved

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

    def _place_composer_sash(self, tries: int = 0) -> None:
        total = self.stack.winfo_height()
        if total < 200:
            if tries < 8:
                self.after(50, lambda: self._place_composer_sash(tries + 1))
            return
        natural = max(150, self.bottom.winfo_reqheight())
        wanted = self.prompt_height or natural
        wanted = max(natural, min(wanted, total - 140))
        try:
            self.stack.sash_place(0, 1, max(140, total - wanted))
        except tk.TclError:
            return

    def _remember_prompt_height(self, _event=None) -> None:
        total = self.stack.winfo_height()
        try:
            _left, top = self.stack.sash_coord(0)
        except tk.TclError:
            return
        height = max(150, total - int(top))
        if height == self.prompt_height:
            return
        self.prompt_height = height
        try:
            save_prompt_height(height)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить высоту поля: {exc}")

    def _save_editor(self) -> None:
        if not self.editor_open or self.project is None:
            return
        text = self._editor_text()
        if text == self.editor_saved:
            self.set_status("Изменений нет")
            return
        if not self._ask(self.editor_path, "Записать файл?"):
            return
        try:
            full = resolve_inside(self.project, self.editor_path)
            if full.exists() and full.is_dir():
                raise PathError("это папка, не файл")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text, encoding="utf-8", newline="\n")
        except (OSError, PathError) as exc:
            self.write_chat(f"Файл не записан: {exc}")
            return
        self.editor_saved = text
        self.set_status("Файл записан")

    def _close_editor(self) -> None:
        self._release_editor(ask=True)

    def _release_editor(self, ask: bool) -> bool:
        if not self.editor_open:
            return True
        if ask and self._editor_dirty():
            if not self._ask(self.editor_path, "Закрыть файл без записи?"):
                return False
        try:
            self.work.forget(self.editor_frame)
        except tk.TclError:
            pass
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_box.delete("1.0", "end")
        self.editor_title.configure(text="")
        return True

    def _ask(self, path: str, summary: str) -> bool:
        dialog = ConfirmDialog(self, path, summary)
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        return bool(dialog.result)

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

    def _chat_scroll_set(self, first, last) -> None:
        try:
            self.chat._y_scrollbar.set(first, last)
        except (tk.TclError, AttributeError):
            pass
        try:
            self._chat_stick = float(last) >= 0.985
        except (TypeError, ValueError):
            self._chat_stick = True
        self._update_jump_down(last)

    def _chat_yview(self, *args) -> None:
        self.chat._textbox.yview(*args)
        try:
            _first, last = self.chat._textbox.yview()
            self._chat_stick = float(last) >= 0.985
            self._update_jump_down(last)
        except (tk.TclError, ValueError, TypeError):
            return

    def _chat_near_bottom(self) -> bool:
        try:
            _first, last = self.chat._textbox.yview()
            return float(last) >= 0.985
        except (tk.TclError, ValueError, TypeError):
            return True

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
        if last is None:
            try:
                _first, last = box._textbox.yview()
            except tk.TclError:
                button.place_forget()
                return
        try:
            show = float(last) < 0.985
        except (TypeError, ValueError):
            show = False
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

    def open_settings(self) -> None:
        window = self._settings_window
        if self._window_alive(window):
            try:
                if str(window.state()) == "withdrawn":
                    window.deiconify()
                window.lift()
                window.focus()
                return
            except tk.TclError:
                self._settings_window = None
        else:
            self._settings_window = None
        try:
            self._settings_window = SettingsWindow(self)
        except Exception as exc:
            self._settings_window = None
            self.write_chat(f"Не удалось открыть настройки: {exc}")

    def _window_alive(self, window) -> bool:
        if window is None:
            return False
        try:
            return bool(window.winfo_exists())
        except tk.TclError:
            return False

    def _restore_theme_windows(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._unhide(self)
        window = self._settings_window
        if window is None:
            return
        if not self._window_alive(window):
            self._settings_window = None
            return
        try:
            hidden = str(window.state()) == "withdrawn"
        except tk.TclError:
            self._settings_window = None
            return
        self._unhide(window)
        if not hidden:
            return
        try:
            window.focus()
        except tk.TclError:
            self._settings_window = None

    def _unhide(self, window) -> None:
        try:
            if str(window.state()) == "withdrawn":
                window.deiconify()
            window.lift()
        except tk.TclError:
            return

    def _field(self, parent, text: str) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w", text_color=MUTED).pack(fill="x", padx=8, pady=(8, 0))

    def _entry(self, parent, label: str, variable, placeholder: str, secret: bool = False) -> None:
        self._field(parent, label)
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            placeholder_text=placeholder,
            show="*" if secret else "",
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            placeholder_text_color=MUTED,
            height=36,
            corner_radius=12,
        )
        entry.pack(fill="x", padx=8, pady=4)

    def _load(self) -> None:
        data, error = load_config()
        self._apply_theme(data["theme"])
        self.prompt_height = int(data.get("prompt_height") or 0)
        self.after(50, self._place_composer_sash)
        self.profiles = list(data["profiles"])
        self.active_profile = data["active_profile"]
        self._sync_profile_menu()
        self._apply_ai(data)
        self.test_preset = normalize_preset(data.get("test_preset"))
        self.test_preset_var.set(LABEL_BY_PRESET.get(self.test_preset, "Выключено"))
        self.test_timeout_var.set(str(normalize_timeout(data.get("test_timeout"))))
        self.test_fix_rounds_var.set(str(normalize_fix_rounds(data.get("test_fix_rounds"))))
        self.auto_write_project = bool(data.get("auto_write_project"))
        self.auto_write_var.set(self.auto_write_project)
        self._set_agent_mode(data.get("agent_mode") or "agent", persist=False)
        from project_agent.context_usage import normalize_context_limit

        self.context_limit = normalize_context_limit(data.get("context_limit"))
        self._apply_context(0, self.context_limit, False)
        self.mcp_servers = list(data["mcp_servers"])
        self.saved_servers = list(data["mcp_servers"])
        self._render_mcp()
        self._restore_project(data.get("project_dir") or "")
        if error:
            self.write_chat(f"Файл настроек не прочитан ({error}). Поля пустые, сохраните их заново.")

    def _apply_theme(self, theme: str) -> None:
        theme = "light" if theme == "light" else "dark"
        self.theme = theme
        label = LABEL_BY_THEME[theme]
        if self.theme_var.get() != label:
            self.theme_var.set(label)
        ctk.set_appearance_mode(theme)
        self._restore_theme_windows()
        self.after(60, self._restore_theme_windows)
        if hasattr(self, "work"):
            self.work.configure(bg=_tone(INK))
        if hasattr(self, "stack"):
            self.stack.configure(bg=_tone(INK))
        if hasattr(self, "tree"):
            self._style_tree()
        if hasattr(self, "chat"):
            self._tag_chat()
            self._copy_photo_cache = None
        if hasattr(self, "placeholder"):
            self.placeholder.configure(fg=_tone(HINT), bg=_tone(FIELD), font=self._font(11))

    def _on_theme_pick(self, label: str) -> None:
        theme = THEME_LABELS.get(label, "dark")
        if theme == self.theme:
            return
        self._apply_theme(theme)
        try:
            save_theme(theme)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить тему: {exc}")

    def _on_test_preset_pick(self, label: str) -> None:
        self.test_preset = PRESET_LABELS.get(label, "")

    def _apply_ai(self, data: dict) -> None:
        fields = ai_settings(data)
        self.provider_var.set(LABEL_BY_PROVIDER.get(fields["provider"], "OpenAI-совместимый"))
        self.base_url_var.set(fields["base_url"])
        self.model_var.set(fields["model"])
        self.api_key_var.set(fields["api_key"])
        self.steps_var.set(str(fields["max_steps"]))
        self.image_model_var.set(fields["image_model"])
        self.image_url_var.set(fields["image_base_url"])
        self.image_key_var.set(fields["image_api_key"])
        self._show_model()

    def _show_model(self) -> None:
        name = self.model_var.get().strip()
        self.model_label.configure(text=name or "модель не выбрана")

    def _ai_snapshot(self) -> dict:
        try:
            steps = int(self.steps_var.get().strip() or "25")
        except ValueError:
            steps = 25
        return ai_settings(
            {
                "provider": PROVIDER_LABELS.get(self.provider_var.get(), "openai"),
                "base_url": self.base_url_var.get().strip(),
                "model": self.model_var.get().strip(),
                "api_key": self.api_key_var.get().strip(),
                "max_steps": steps,
                "image_model": self.image_model_var.get().strip(),
                "image_base_url": self.image_url_var.get().strip(),
                "image_api_key": self.image_key_var.get().strip(),
            }
        )

    def _sync_profile_menu(self) -> None:
        names = [item["name"] for item in self.profiles] or [_NO_PROFILE]
        current = self.active_profile if self.active_profile in names else names[0]
        self.profile_var.set(current)
        for menu in (self.profile_menu, self.footer_profile_menu):
            if menu is not None and menu.winfo_exists():
                menu.configure(values=names)
                menu.set(current)

    def _on_profile_pick(self, name: str) -> None:
        if name == _NO_PROFILE or name == self.active_profile:
            return
        if self.running:
            self._sync_profile_menu()
            return
        profile = next((item for item in self.profiles if item["name"] == name), None)
        if profile is None:
            return
        self._apply_ai(profile)
        self.active_profile = name
        self._persist("Профиль выбран")

    def save_profile(self) -> None:
        dialog = NameDialog(self)
        self.wait_window(dialog)
        name = " ".join(str(dialog.result or "").split())
        if not name:
            return
        if len(name) > 80 or name == _NO_PROFILE:
            self.write_chat("Такое имя профиля не подходит.")
            return
        stored = {"name": name, **self._ai_snapshot()}
        self.profiles = [stored if item["name"] == name else item for item in self.profiles]
        if all(item["name"] != name for item in self.profiles):
            self.profiles.append(stored)
        self.active_profile = name
        self._sync_profile_menu()
        self._persist("Профиль сохранён")

    def delete_profile(self) -> None:
        name = self.active_profile
        if not name:
            self.write_chat("Профиль не выбран.")
            return
        self.profiles = [item for item in self.profiles if item["name"] != name]
        self.active_profile = ""
        self._sync_profile_menu()
        self._persist("Профиль удалён")

    def _restore_project(self, raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            self._show_folder()
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            self.project = None
            self._show_folder()
            self.write_chat(f"Сохранённая папка недоступна: {text}")
            return
        self.project = path.resolve()
        self.agent.set_root(self.project)
        self._show_folder()

    def collect_settings(self) -> dict:
        settings = self._ai_snapshot()
        settings["project_dir"] = "" if self.project is None else str(self.project)
        settings["active_profile"] = self.active_profile
        settings["profiles"] = list(self.profiles)
        settings["mcp_servers"] = self._servers_for_save()
        settings["theme"] = self.theme
        settings["prompt_height"] = self.prompt_height
        settings["test_preset"] = PRESET_LABELS.get(self.test_preset_var.get(), self.test_preset)
        settings["test_timeout"] = normalize_timeout(self.test_timeout_var.get())
        settings["test_fix_rounds"] = normalize_fix_rounds(self.test_fix_rounds_var.get())
        settings["agent_mode"] = self.agent_mode
        settings["context_limit"] = self.context_limit
        settings["auto_write_project"] = bool(self.auto_write_var.get())
        self.auto_write_project = settings["auto_write_project"]
        return settings

    def _set_agent_mode(self, mode: str, persist: bool = True) -> None:
        from project_agent.tools import normalize_agent_mode

        mode = normalize_agent_mode(mode)
        self.agent_mode = mode
        label = LABEL_BY_MODE.get(mode, "Agent")
        if self.agent_mode_var.get() != label:
            self.agent_mode_var.set(label)
        if persist:
            try:
                save_agent_mode(mode)
            except Exception as exc:
                self.write_chat(f"Не удалось сохранить режим: {exc}")

    def _on_mode_pick(self, choice: str) -> None:
        self._set_agent_mode(MODE_LABELS.get(choice, "agent"), persist=True)

    def _on_context(self, used: int, limit: int, from_api: bool = False) -> None:
        self.after(0, lambda: self._apply_context(used, limit, from_api))

    def _apply_context(self, used: int, limit: int, from_api: bool) -> None:
        from project_agent.context_usage import (
            FULL_RATIO,
            WARN_RATIO,
            context_ratio,
            format_context_detail,
            normalize_context_limit,
        )

        self.context_limit = normalize_context_limit(limit)
        self.context_used = max(0, int(used or 0))
        self.context_from_api = bool(from_api)
        ratio = context_ratio(self.context_used, self.context_limit)
        if hasattr(self, "context_bar") and self.context_bar is not None:
            self.context_bar.set(ratio)
            color = CTX_OK
            if ratio >= FULL_RATIO:
                color = CTX_FULL
            elif ratio >= WARN_RATIO:
                color = CTX_WARN
            self.context_bar.configure(progress_color=color)
        if hasattr(self, "context_label") and self.context_label is not None:
            self.context_label.configure(
                text=format_context_detail(self.context_used, self.context_limit, self.context_from_api)
            )

    def compress_context(self) -> None:
        if self.running:
            return
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        settings = self.collect_settings()
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Сжатие контекста…")

        def work() -> None:
            try:
                self.agent.compress_context(settings, self.stop_event)
            except Exception as exc:
                self.write_chat(f"Ошибка сжатия: {exc}")
            finally:
                self.after(0, self._finish)

        threading.Thread(target=work, daemon=True).start()

    def save_settings(self) -> None:
        self._persist("Настройки сохранены")
        self._show_model()

    def _capture_active_profile(self) -> None:
        name = self.active_profile
        if not name:
            return
        stored = {"name": name, **self._ai_snapshot()}
        updated = []
        found = False
        for item in self.profiles:
            if item["name"] == name:
                updated.append(stored)
                found = True
            else:
                updated.append(item)
        if found:
            self.profiles = updated

    def _persist(self, status: str | None = None) -> None:
        try:
            self._capture_active_profile()
            save_config(self.collect_settings())
            self.saved_servers = self._servers_for_save()
            if status:
                self.set_status(status)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить настройки: {exc}")

    def choose_folder(self) -> None:
        if self.running:
            return
        selected = filedialog.askdirectory()
        if not selected:
            return
        if not self._release_editor(ask=True):
            return
        self._store_chat()
        self.chat_id = None
        self.chat_title = ""
        self.transcript.clear()
        self._forget_retry()
        self.project = Path(selected).resolve()
        self._show_folder()
        self.agent.set_root(self.project)
        self._clear_box(self.chat)
        try:
            build_index(self.project)
            self._refresh_index_label()
        except Exception as exc:
            self.write_chat(f"Индекс не построен: {exc}")
        self._persist("Папка выбрана")

    def new_chat(self) -> None:
        if self.running:
            return
        self._hide_at_popup()
        self.clear_attachments()
        self._store_chat()
        self.chat_id = None
        self.chat_title = ""
        self.transcript.clear()
        self._forget_retry()
        self._clear_box(self.chat)
        self.agent.reset_session(announce=False)
        self._apply_context(0, self.context_limit, False)
        self._refresh_chat_list()
        self._fit_labels()
        self.set_status("Новый чат")

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

    def _drop_attachment(self, kind: str, path: str) -> None:
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
        if self._attach_specs():
            self._fill_chips()

    def _refresh_attach(self) -> None:
        if not self._attach_specs():
            self._clear_chips()
            self.attach_row.grid_remove()
        else:
            self.attach_row.grid()
            self._fill_chips()
        # ряд чипов двигает поле ввода — подсказку надо переставить.
        self.after(0, self._sync_placeholder)

    def _fill_chips(self) -> None:
        self._clear_chips()
        specs = self._attach_specs()
        measure = self._measure(11)
        room = max(self.attach_row.winfo_width(), self.card.winfo_width() - 24, 240)
        if len(specs) > 1:
            # место под «очистить» в конце последнего ряда
            room = max(room - 74, 200)
        lines: list[list[dict]] = [[]]
        used = 0
        for spec in specs:
            # ширина текста плюс поля чипа и крестик
            span = measure(spec["text"]) + 46
            if lines[-1] and used + span > room:
                if len(lines) >= _CHIP_ROWS:
                    break
                lines.append([])
                used = 0
            lines[-1].append(spec)
            used += span
        shown = sum(len(line) for line in lines)
        self._ensure_composer_room(sum(1 for line in lines if line))
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
                if len(specs) > 1:
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

    def _ensure_composer_room(self, rows: int) -> None:
        """Чипы отъедают высоту у поля ввода — опускаем разделитель, если тесно."""
        if rows <= 0 or not hasattr(self, "stack"):
            return
        least = 150 + rows * 28
        total = self.stack.winfo_height()
        if total <= least + 140:
            return
        try:
            _left, top = self.stack.sash_coord(0)
            if total - int(top) >= least:
                return
            self.stack.sash_place(0, 1, total - least)
        except tk.TclError:
            return

    def _make_chip(self, parent, spec: dict) -> ctk.CTkFrame:
        chip = ctk.CTkFrame(parent, fg_color=BUTTON, corner_radius=8, border_width=1, border_color=BORDER)
        label = ctk.CTkLabel(chip, text=spec["text"], text_color=MUTED, font=self._font(11), height=18)
        label.grid(row=0, column=0, padx=(8, 2), pady=1)
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

    def _file_menu(self, event) -> str | None:
        row = self.tree.identify_row(event.y)
        if not row or self.project is None:
            return None
        tags = self.tree.item(row, "tags")
        if "file" not in tags and "dir" not in tags:
            return None
        self.tree.selection_set(row)
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
        if "dir" in tags:
            target = "." if row == "." else str(row)
            menu.add_command(label="Вложить папку в запрос", command=lambda: self._add_context_path(target + "/"))
        else:
            menu.add_command(label="Вложить в запрос", command=lambda: self._add_context_path(str(row)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

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

    def add_mcp(self) -> None:
        name = self.mcp_name_var.get().strip()
        command_line = self.mcp_command_var.get().strip()
        if not name or not command_line:
            self.write_chat("Для MCP нужны имя и команда.")
            return
        try:
            parts = shlex.split(command_line, posix=False)
        except ValueError as exc:
            self.write_chat(f"Команда MCP не разобрана: {exc}")
            return
        parts = [part.strip('"') for part in parts if part]
        if not parts:
            return
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self.mcp_servers.append({"name": name, "command": parts[0], "args": parts[1:], "env": {}})
        self.mcp_name_var.set("")
        self.mcp_command_var.set("")
        self._render_mcp()

    def add_browser_mcp(self) -> None:
        from project_agent.browser_mcp import PLAYWRIGHT_SERVER

        name = PLAYWRIGHT_SERVER["name"]
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self.mcp_servers.append(
            {
                "name": name,
                "command": PLAYWRIGHT_SERVER["command"],
                "args": list(PLAYWRIGHT_SERVER["args"]),
                "env": {},
            }
        )
        self._render_mcp()
        self.write_chat(
            "Добавлен MCP playwright (npx -y @playwright/mcp@latest). Нужен Node.js. Сохраните настройки."
        )

    def remove_mcp(self) -> None:
        name = self.mcp_name_var.get().strip()
        if not name and self.mcp_servers:
            name = self.mcp_servers[-1]["name"]
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self._render_mcp()

    def _servers_for_save(self) -> list[dict]:
        previous = {item["name"]: item for item in self.saved_servers}
        saved = []
        for item in self.mcp_servers:
            env = (previous.get(item["name"]) or {}).get("env") or item.get("env") or {}
            saved.append(
                {
                    "name": item["name"],
                    "command": item["command"],
                    "args": list(item.get("args") or []),
                    "env": dict(env),
                }
            )
        return saved

    def _render_mcp(self) -> None:
        box = self.mcp_list
        if box is None or not box.winfo_exists():
            return
        lines = []
        for item in self.mcp_servers:
            args = " ".join(item.get("args") or [])
            lines.append(f"{item['name']} — {item['command']} {args}".strip())
        self._set_box(box, "\n".join(lines))

    def _send_key(self, event=None):
        if event is not None and event.state & 0x0001:
            return None
        if self._at_popup_alive():
            self._at_accept()
            return "break"
        self.send()
        return "break"

    def send(self) -> None:
        if self.running:
            return
        self._hide_at_popup()
        text = self.task.get("1.0", "end").strip()
        images = list(self.attached)
        context_paths = merge_paths(
            self.context_files,
            self.context_dirs,
            parse_at_paths(text),
            limit=MAX_CONTEXT_FILES + MAX_CONTEXT_DIRS,
        )
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        settings = self.collect_settings()
        if not settings["model"]:
            self.write_chat("Укажите модель.")
            return
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        if not text and not images and not context_paths:
            return
        try:
            save_config(settings)
            self.saved_servers = settings["mcp_servers"]
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить настройки: {exc}")
        prepared = []
        for path in images:
            try:
                prepared.append(prepare_image(Path(path)))
            except Exception as exc:
                self.write_chat(f"Изображение не прочитано: {exc}")
                return
        context_block = ""
        loaded_files: list[str] = []
        loaded_dirs: list[str] = []
        if context_paths:
            context_block, loaded_files, loaded_dirs, errors = load_explicit_context(
                self.project, context_paths, self.vault
            )
            for item in errors:
                self.write_chat(f"Вложение: {item}")
        model_text = compose_user_text(text, context_block)
        if not model_text and not prepared:
            return
        self.task.delete("1.0", "end")
        self.attached.clear()
        self.context_files.clear()
        self.context_dirs.clear()
        self._refresh_attach()
        note = text or (
            "(контекст)" if (loaded_files or loaded_dirs) else "(только изображение)"
        )
        if loaded_dirs:
            note += f"\n(папки: {', '.join(loaded_dirs)})"
        if loaded_files:
            note += f"\n(файлы: {', '.join(loaded_files)})"
        if images:
            note += f"\n(изображений: {len(images)})"
        created = self.chat_id is None
        if created:
            self.chat_id = new_chat_id()
            self.chat_title = " ".join(note.split())[:80] or "Чат"
            self.transcript.clear()
        self.write_chat(f"Вы: {note}")
        if created:
            self._refresh_chat_list()
            self._fit_labels()
        self._retry_payload = {"chat_id": self.chat_id, "text": model_text, "images": prepared}
        self._can_retry = False
        self._set_retry_enabled(False)
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Запрос отправлен")
        thread = threading.Thread(target=self._turn, args=(model_text, prepared, settings, False), daemon=True)
        thread.start()

    def retry_last(self) -> None:
        payload = self._retry_payload
        if self.running or not self._can_retry or not payload:
            return
        if payload.get("chat_id") != self.chat_id or self.project is None:
            return
        settings = self.collect_settings()
        if not settings["model"]:
            self.write_chat("Укажите модель.")
            return
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        self._can_retry = False
        self._set_retry_enabled(False)
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Повтор запроса")
        thread = threading.Thread(
            target=self._turn,
            args=(payload["text"], payload["images"], settings, True),
            daemon=True,
        )
        thread.start()

    def _turn(self, text: str, images: list, settings: dict, resend: bool = False) -> None:
        self._can_retry = False
        try:
            self.agent.run_turn(text, images, settings, self.stop_event, resend=resend)
        except Exception as exc:
            self.write_retryable_error(f"Ошибка: {exc}")
        finally:
            self.after(0, self._finish)

    def stop(self) -> None:
        self.stop_event.set()
        self.agent.close()
        dialog = self._dialog
        if dialog is not None:
            self.after(0, dialog.refuse)
        self.set_status("Остановка")

    def _finish(self) -> None:
        self.running = False
        self._show_running(False)
        self._clear_running_tool()
        self._close_tool_group()
        self._collapse_tool_groups()
        if self.stop_event.is_set():
            self._can_retry = False
            self._set_retry_enabled(False)
            self.set_status("Остановлено")
        elif self._can_retry:
            self.set_status("Ошибка API")
        else:
            self._set_retry_enabled(False)
            self.set_status("Готово")
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_chat_list()

    def confirm(self, path: str, summary: str, detail: str = "") -> bool:
        if self.stop_event.is_set():
            return False
        done = threading.Event()
        holder = {"ok": False}

        def ask() -> None:
            try:
                dialog = ConfirmDialog(self, path, summary, detail)
                self._dialog = dialog
                self.wait_window(dialog)
                holder["ok"] = bool(dialog.result)
            except Exception:
                holder["ok"] = False
            finally:
                self._dialog = None
                done.set()

        try:
            self.after(0, ask)
        except Exception:
            return False
        while not done.wait(0.1):
            if self.stop_event.is_set() and self._dialog is not None:
                self.after(0, self._dialog.refuse)
        return holder["ok"] and not self.stop_event.is_set()

    def write_chat(self, text: str) -> None:
        line = with_chat_stamp(text)
        self._remember(line)
        self._append(self.chat, line)

    def write_retryable_error(self, text: str) -> None:
        self._can_retry = True
        line = with_chat_stamp(text)
        self._remember(line)
        self.after(0, lambda line=line: self._append_error(line))

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

    def _append_error(self, text: str) -> None:
        box = self.chat
        if not box.winfo_exists():
            return
        box.configure(state="normal")
        inner = box._textbox
        self._clear_running_tool()
        self._close_tool_group()
        start = inner.index("end-1c")
        body = chat_body(text, "error")
        _, stamp = split_chat_stamp(text)
        inner.insert("end", body + "\n")
        self._insert_retry_button(inner)
        if stamp:
            mark = inner.index("end-1c")
            inner.insert("end", f"{stamp}\n\n", ("time",))
            inner.tag_add("error", start, mark)
        else:
            inner.tag_add("error", start, inner.index("end-1c"))
        self._chat_see_end()

    def _insert_retry_button(self, inner) -> None:
        self._set_retry_enabled(False)
        button = tk.Button(
            inner,
            text="Повторить",
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
        inner.insert("end", "\n\n")
        inner.tag_add("error", mark, inner.index("end-1c"))

    def write_journal(self, text: str) -> None:
        line = f"· {text.strip()}"
        # Строка «выполняется…» живёт только на экране и гаснет, когда пришёл итог.
        if is_running_journal(line):
            self.after(0, lambda line=line: self._show_running_tool(line))
            return
        self.after(0, self._clear_running_tool)
        self.write_chat(line)

    def _remember(self, text: str) -> None:
        line = text.rstrip()
        if not line or not self.chat_id:
            return
        self.transcript.append(line)
        self._store_chat()

    def _store_chat(self) -> None:
        if not self.chat_id or self.project is None or not self.transcript:
            return
        try:
            scrubber = Scrubber(self.vault, literals_from_settings(self.collect_settings()))
            lines = [scrubber(line) for line in self.transcript]
            title = scrubber(self.chat_title) or "Чат"
            provider, messages = self.agent.export_session()
            if messages:
                messages = scrub_outbound(messages, scrubber)
            save_chat(
                self.chat_id,
                title,
                str(self.project),
                lines,
                messages=messages,
                provider=provider,
            )
        except Exception as exc:
            self.set_status(f"Чат не сохранён: {exc}")

    def _refresh_chat_list(self) -> None:
        if not hasattr(self, "chat_tree"):
            return
        self._chat_list_lock = True
        try:
            self.chat_tree.delete(*self.chat_tree.get_children())
            if self.project is None:
                return
            for item in list_chats(str(self.project)):
                self.chat_tree.insert("", "end", iid=item["id"], text=item["title"])
            if self.chat_id and self.chat_tree.exists(self.chat_id):
                self.chat_tree.selection_set(self.chat_id)
                self.chat_tree.focus(self.chat_id)
        finally:
            self._chat_list_lock = False

    def _on_chat_select(self, _event=None) -> None:
        if self._chat_list_lock:
            return
        selected = self.chat_tree.selection()
        if not selected:
            return
        chat_id = str(selected[0])
        if chat_id == self.chat_id:
            return
        if self.running:
            self._refresh_chat_list()
            return
        self._open_chat(chat_id)

    def _open_chat(self, chat_id: str) -> None:
        self._store_chat()
        record = load_chat(chat_id)
        if record is None or self.project is None:
            self._refresh_chat_list()
            return
        self.chat_id = record["id"]
        self.chat_title = record["title"]
        self.transcript = list(record["lines"])
        self._forget_retry()
        self._show_transcript()
        from project_agent.tools import normalize_agent_mode

        self.agent._agent_mode = normalize_agent_mode(self.collect_settings().get("agent_mode"))
        restored = False
        messages = record.get("messages") or []
        provider = record.get("provider") or ""
        if messages and provider:
            restored = self.agent.restore_session(provider, messages)
        if not restored:
            self.agent.reset_session(announce=False)
            self.set_status("Чат открыт. Модель начинает заново.")
        else:
            self.set_status("Чат открыт. История модели восстановлена.")
            settings = self.collect_settings()
            if self.agent.provider is not None:
                try:
                    self.agent._publish_context(settings)
                except Exception:
                    self._apply_context(0, self.context_limit, False)
        self._refresh_chat_list()
        self._fit_labels()

    def delete_chat_item(self, chat_id: str) -> None:
        if self.running or not chat_id:
            return
        title = self.chat_tree.item(chat_id, "text") if self.chat_tree.exists(chat_id) else self.chat_title
        dialog = ConfirmDialog(self, title or "Чат", "Удалить этот чат?")
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        if not dialog.result:
            return
        try:
            delete_chat(chat_id)
        except Exception as exc:
            self.write_chat(f"Чат не удалён: {exc}")
            return
        if chat_id == self.chat_id:
            self.chat_id = None
            self.chat_title = ""
            self.transcript.clear()
            self._forget_retry()
            self._clear_box(self.chat)
            self.agent.reset_session(announce=False)
        self._refresh_chat_list()
        self._fit_labels()
        self.set_status("Чат удалён")

    def set_status(self, text: str) -> None:
        shown = text

        def apply() -> None:
            if self._closing:
                return
            try:
                if not self.winfo_exists() or not self.status_label.winfo_exists():
                    return
                self.status_label.configure(text=shown)
            except tk.TclError:
                return

        try:
            self.after(0, apply)
        except tk.TclError:
            return

    def _role(self, text: str) -> str:
        return chat_role(text)

    def _tag_chat(self) -> None:
        inner = self.chat._textbox
        inner.tag_configure(
            "user",
            lmargin1=56,
            lmargin2=56,
            rmargin=12,
            foreground=_tone(USER_TEXT),
            background=_tone(USER_BG),
            spacing1=8,
            spacing3=10,
        )
        inner.tag_configure(
            "agent",
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            foreground=_tone(TEXT),
            spacing1=4,
            spacing3=10,
        )
        inner.tag_configure(
            "tool",
            lmargin1=40,
            lmargin2=48,
            rmargin=28,
            foreground=_tone(MUTED),
            spacing1=0,
            spacing3=0,
        )
        inner.tag_configure(
            "toolhead",
            lmargin1=24,
            lmargin2=24,
            rmargin=28,
            spacing1=4,
            spacing3=2,
        )
        inner.tag_configure("toolgap", font=self._px_font(5), spacing1=0, spacing3=0)
        inner.tag_configure(
            "error",
            lmargin1=12,
            lmargin2=12,
            rmargin=56,
            foreground=_tone(CTX_FULL),
            spacing1=4,
            spacing3=6,
        )
        inner.tag_configure("label", font=self._px_font(11), foreground=_tone(MUTED), spacing1=0, spacing3=2)
        inner.tag_configure(
            "time",
            font=self._px_font(10),
            foreground=_tone(MUTED),
            spacing1=0,
            spacing3=8,
        )
        inner.tag_configure("bold", font=self._px_font(13, weight="bold"))
        inner.tag_configure("code", font=self._px_font(12, "Consolas"), foreground=_tone(CODE_TEXT))
        inner.tag_configure(
            "codeblock",
            font=self._px_font(12, "Consolas"),
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
            spacing3=8,
        )
        inner.tag_configure(
            "usercopy",
            lmargin1=56,
            lmargin2=56,
            rmargin=12,
            spacing1=0,
            spacing3=8,
        )
        inner.tag_configure("search", background=_tone(SEARCH_BG))
        inner.tag_configure("searchcur", background=_tone(SEARCH_CUR))
        # Фон реплики «Вы» иначе перекрывает подсветку выделения.
        inner.tag_raise("sel")
        inner.tag_raise("search")
        inner.tag_raise("searchcur")
        inner.configure(pady=8)
        if self._chat_pad:
            inner.configure(padx=self._chat_pad)
        self._restyle_tool_heads()
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

    def _make_copy_chip(self, body: str, bg: tuple[str, str] | None = None) -> tk.Label:
        fill = _tone(bg or INK)
        photo = self._copy_photo()
        chip = tk.Label(
            self.chat._textbox,
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
        inner.insert("end", f"{stamp}\n\n", ("time",))

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
        if role == "user":
            inner.insert("end", "Вы\n", ("label",))
            copy_text = user_copy_text(body)
            inner.insert("end", body + "\n")
            if copy_text:
                mark = inner.index("end-1c")
                chip = self._make_copy_chip(copy_text, USER_BG)
                inner.window_create("end", window=chip, padx=0, pady=2)
                inner.insert("end", "\n")
                inner.tag_add("usercopy", mark, inner.index("end-1c"))
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
        self._retry_buttons = []
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
            self._retry_buttons = []
            self._reset_tool_groups()
        box.configure(state="normal")
        box.delete("1.0", "end")

    def _set_box(self, box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        if text:
            box.insert("1.0", text)
        box.configure(state="disabled")

    def _ui_error(self, exc, value, tb) -> None:
        if self._closing:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.write_chat(f"Ошибка интерфейса: {value}")

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._release_editor(ask=True):
            return
        self._closing = True
        self.stop_event.set()
        try:
            self._store_chat()
            self.agent.close()
        finally:
            try:
                self.destroy()
            except tk.TclError:
                return


def main() -> None:
    multiprocessing.freeze_support()
    try:
        data, _error = load_config()
        ctk.set_appearance_mode(data["theme"])
        ctk.set_default_color_theme("dark-blue")
        app = App()
        app.mainloop()
    except Exception:
        _crash()


def _crash() -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "crash.log"
    log.write_text(traceback.format_exc(), encoding="utf-8")
    if __import__("os").name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "Ошибка запуска. Подробности в %APPDATA%\\ProjectAgent\\crash.log",
            "ProjectAgent",
            0x10,
        )
