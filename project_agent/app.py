from __future__ import annotations

import multiprocessing
import shlex
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import customtkinter as ctk
from PIL import Image, ImageDraw

from project_agent.agent import Agent
from project_agent.chats import delete_chat, list_chats, load_chat, new_chat_id, save_chat
from project_agent.config import (
    ai_settings,
    config_dir,
    load_config,
    needs_api_key,
    save_config,
    save_prompt_height,
    save_theme,
)
from project_agent.images import prepare_image
from project_agent.mcp_client import McpHub
from project_agent.paths import PathError, list_entries, read_text_file, resolve_inside
from project_agent.secrets import Scrubber, SecretVault, literals_from_settings
from project_agent.tools import Toolbox

PROVIDER_LABELS = {
    "OpenAI-совместимый": "openai",
    "Anthropic": "anthropic",
}
LABEL_BY_PROVIDER = {value: key for key, value in PROVIDER_LABELS.items()}
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
USER_TEXT = ("#4a3c30", "#f7f0e4")
SELECT = ("#e4dcd2", "#3a342e")
SEND = ("#3c3631", "#efe6da")
SEND_HOVER = ("#2b2724", "#f7f1e8")
ON_SEND = ("#f6f1ea", "#1c1916")
_ICONS: dict[str, ctk.CTkImage] = {}


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
_LATIN_CLIPBOARD = {"c", "v", "x", "a"}
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
        return "select" if lowered == "a" else None
    if symbol.startswith("Cyrillic"):
        return _KEY_CLIPBOARD.get(int(keycode))
    return None


def _selection(widget) -> str | None:
    try:
        if widget.winfo_class() == "Text":
            return widget.get("sel.first", "sel.last")
        return widget.selection_get()
    except tk.TclError:
        return None


def apply_layout_clipboard(widget, action: str) -> None:
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
            selected = _selection(widget)
            if selected is None:
                return
            widget.clipboard_clear()
            widget.clipboard_append(selected)
            if action == "cut":
                widget.delete("sel.first", "sel.last")
            return
        if action == "paste":
            text = widget.clipboard_get()
            try:
                widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            widget.insert("insert", text)
    except tk.TclError:
        return


def _on_layout_clipboard(event):
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
    def __init__(self, master, path: str, summary: str) -> None:
        super().__init__(master)
        self.result = False
        self._closed = False
        self.title("Подтверждение")
        self.geometry("520x220")
        self.resizable(False, False)
        self.configure(fg_color=INK)
        self.transient(master)
        self.grab_set()
        ctk.CTkLabel(self, text=path, wraplength=480, justify="left", anchor="w", text_color=TEXT).pack(
            padx=16, pady=(16, 8), fill="x"
        )
        ctk.CTkLabel(self, text=summary, wraplength=480, justify="left", anchor="w", text_color=MUTED).pack(
            padx=16, pady=4, fill="x"
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(pady=16)
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
    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title("Настройки")
        self.geometry("440x760")
        self.minsize(400, 520)
        self.configure(fg_color=INK)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        settings = ctk.CTkScrollableFrame(
            self,
            fg_color=PANEL,
            corner_radius=16,
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        settings.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        app._field(settings, "Тема")
        quiet_menu(settings, app.theme_var, list(THEME_LABELS), app._on_theme_pick).pack(fill="x", padx=8, pady=4)
        app._field(settings, "Профиль")
        app.profile_menu = quiet_menu(settings, app.profile_var, [_NO_PROFILE], app._on_profile_pick)
        app.profile_menu.pack(fill="x", padx=8, pady=4)
        app._sync_profile_menu()
        profile_row = ctk.CTkFrame(settings, fg_color="transparent")
        profile_row.pack(fill="x", padx=8, pady=4)
        quiet_button(profile_row, "Сохранить профиль", app.save_profile, width=188, mark="save").pack(side="left", padx=(0, 4))
        quiet_button(profile_row, "Удалить", app.delete_profile, width=112, mark="trash").pack(side="left")
        app._field(settings, "Провайдер")
        quiet_menu(settings, app.provider_var, list(PROVIDER_LABELS)).pack(fill="x", padx=8, pady=4)
        app._entry(settings, "URL API", app.base_url_var, "пусто — OpenAI или Anthropic")
        app._entry(settings, "Модель", app.model_var, "имя модели")
        app._entry(settings, "Ключ", app.api_key_var, "", secret=True)
        app._entry(settings, "Лимит шагов", app.steps_var, "25")
        app._entry(settings, "Модель изображений", app.image_model_var, "пусто — генерация выключена")
        app._entry(settings, "URL изображений", app.image_url_var, "пусто — URL API")
        app._entry(settings, "Ключ изображений", app.image_key_var, "пусто — основной ключ", secret=True)
        ctk.CTkLabel(
            settings,
            text="Ключ хранится только в %APPDATA%\\ProjectAgent\\config.json",
            wraplength=360,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(4, 8))
        quiet_button(settings, "Сохранить", app.save_settings, width=160, primary=True, mark="check").pack(fill="x", padx=8, pady=4)
        ctk.CTkLabel(settings, text="MCP-серверы", anchor="w", text_color=TEXT).pack(fill="x", padx=8, pady=(12, 0))
        app.mcp_list = ctk.CTkTextbox(
            settings,
            height=90,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
        )
        app.mcp_list.pack(fill="x", padx=8, pady=4)
        app.mcp_list.configure(state="disabled")
        app._render_mcp()
        app._entry(settings, "Имя MCP", app.mcp_name_var, "filesystem")
        app._entry(settings, "Команда MCP", app.mcp_command_var, "python -m server")
        mcp_row = ctk.CTkFrame(settings, fg_color="transparent")
        mcp_row.pack(fill="x", padx=8, pady=4)
        quiet_button(mcp_row, "Добавить", app.add_mcp, width=124, mark="plus").pack(side="left", padx=(0, 4))
        quiet_button(mcp_row, "Удалить", app.remove_mcp, width=112, mark="trash").pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.after(50, self.focus)

    def _close(self) -> None:
        self.app.mcp_list = None
        self.app.profile_menu = None
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
        self.mcp_servers: list[dict] = []
        self.saved_servers: list[dict] = []
        self.stop_event = threading.Event()
        self.running = False
        self._dialog: ConfirmDialog | None = None
        self._settings_window: SettingsWindow | None = None
        self.mcp_list: ctk.CTkTextbox | None = None
        self.profile_menu: ctk.CTkOptionMenu | None = None
        self.profiles: list[dict] = []
        self.active_profile = ""
        self.profile_var = ctk.StringVar(value=_NO_PROFILE)
        self._folder_wrap = 0
        self.chat_id: str | None = None
        self.chat_title = ""
        self.transcript: list[str] = []
        self._chat_list_lock = False
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
        )
        self.theme = "dark"
        self.theme_var = ctk.StringVar(value="Тёмная")
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_width = 420
        self.prompt_height = 0
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
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._ui_error

    def _build(self) -> None:
        self.bind_class("Text", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.bind_class("Entry", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.grid_columnconfigure(0, weight=0, minsize=260)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="Папка", anchor="w", text_color=MUTED).grid(row=0, column=0, padx=(12, 8), pady=8)
        self.folder_label = ctk.CTkLabel(top, text="не выбрана", anchor="w", justify="left", text_color=TEXT)
        self.folder_label.grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        self.folder_label.bind("<Configure>", self._fit_folder)
        self.folder_button = quiet_button(top, "Выбрать", self.choose_folder, width=112, mark="folder")
        self.folder_button.grid(row=0, column=2, padx=4, pady=8)
        quiet_button(top, "Настройки", self.open_settings, width=124, mark="sliders").grid(row=0, column=3, padx=4, pady=8)
        quiet_button(top, "Новый чат", self.new_chat, width=128, mark="plus").grid(row=0, column=4, padx=(4, 12), pady=8)

        side = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        side.grid(row=1, column=0, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(1, weight=3)
        side.grid_rowconfigure(3, weight=2)
        side_top = ctk.CTkFrame(side, fg_color="transparent")
        side_top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        side_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(side_top, text="Структура", anchor="w", text_color=MUTED).grid(row=0, column=0, sticky="w")
        quiet_button(side_top, "Обновить", self._refresh_tree, width=118, mark="refresh").grid(row=0, column=1, padx=(8, 0))
        tree_holder = ctk.CTkFrame(side, fg_color="transparent")
        tree_holder.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
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
        chat_top = ctk.CTkFrame(side, fg_color="transparent")
        chat_top.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))
        chat_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(chat_top, text="Чаты", anchor="w", text_color=MUTED).grid(row=0, column=0, sticky="w")
        quiet_button(chat_top, "Удалить", self.delete_selected_chat, width=112, mark="trash").grid(row=0, column=1, padx=(8, 0))
        chat_holder = ctk.CTkFrame(side, fg_color="transparent")
        chat_holder.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 8))
        chat_holder.grid_columnconfigure(0, weight=1)
        chat_holder.grid_rowconfigure(0, weight=1)
        self.chat_tree = ttk.Treeview(chat_holder, show="tree", selectmode="browse", style="Project.Treeview")
        self.chat_tree.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ctk.CTkScrollbar(
            chat_holder, command=self.chat_tree.yview, fg_color=PANEL, button_color=BUTTON, button_hover_color=BUTTON_HOVER
        )
        chat_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.chat_tree.configure(yscrollcommand=chat_scroll.set)
        self.chat_tree.bind("<<TreeviewSelect>>", self._on_chat_select)

        self.work = tk.PanedWindow(
            self,
            orient="horizontal",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_h_double_arrow",
        )
        self.work.grid(row=1, column=1, sticky="nsew")
        self.work.bind("<ButtonRelease-1>", self._remember_editor_width)
        self.center = ctk.CTkFrame(self.work, fg_color=INK, corner_radius=0)
        self.center.grid_columnconfigure(0, weight=1)
        self.center.grid_rowconfigure(0, weight=1)
        self.work.add(self.center, stretch="always", minsize=360, sticky="nsew")
        self.stack = tk.PanedWindow(
            self.center,
            orient="vertical",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_v_double_arrow",
        )
        self.stack.grid(row=0, column=0, sticky="nsew")
        self.stack.bind("<ButtonRelease-1>", self._remember_prompt_height)
        chat_holder = ctk.CTkFrame(self.stack, fg_color=INK, corner_radius=0)
        chat_holder.grid_columnconfigure(0, weight=1)
        chat_holder.grid_rowconfigure(0, weight=1)
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
        self.chat.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
        self.stack.add(chat_holder, stretch="always", minsize=140, sticky="nsew")
        self._tag_chat()
        self.chat.bind("<<Paste>>", lambda _event: "break")
        self.chat.bind("<<Cut>>", lambda _event: "break")
        self.chat.bind("<Key>", self._chat_key)
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

        bottom = ctk.CTkFrame(self.stack, fg_color=FIELD, corner_radius=18, border_width=1, border_color=BORDER)
        self.bottom = bottom
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(1, weight=1)
        self.stack.add(bottom, stretch="never", minsize=188, sticky="nsew")
        ctk.CTkLabel(bottom, text="Задача", anchor="w", text_color=MUTED).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        self.task = ctk.CTkTextbox(
            bottom,
            height=72,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            corner_radius=12,
            font=("Segoe UI", 13),
        )
        self.task.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=8, pady=(4, 0))
        self.task.bind("<Return>", self._send_key)
        self.task.bind("<KP_Enter>", self._send_key)
        self.task.bind("<Control-Return>", self._send_key)
        self.model_label = ctk.CTkLabel(
            bottom,
            text="модель не выбрана",
            anchor="w",
            text_color=MUTED,
            font=("Segoe UI", 11),
        )
        self.model_label.grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 0))
        self.attach_label = ctk.CTkLabel(bottom, text="", anchor="w", text_color=MUTED)
        self.attach_label.grid(row=3, column=0, sticky="w", padx=12, pady=(4, 4))
        self.image_button = ctk.CTkButton(
            bottom,
            text="",
            image=glyph("plus"),
            width=40,
            height=40,
            corner_radius=20,
            border_width=1,
            border_color=BORDER,
            fg_color=BUTTON,
            hover_color=BUTTON_HOVER,
            command=self.attach_image,
        )
        self.image_button.grid(row=3, column=1, padx=4, pady=(4, 4))
        self.stop_button = quiet_button(bottom, "Стоп", self.stop, width=96, mark="stop")
        self.stop_button.grid(row=3, column=2, padx=4, pady=(4, 4))
        self.send_button = quiet_button(bottom, "", self.send, round_mark=True)
        self.send_button.grid(row=3, column=3, padx=(4, 12), pady=(4, 4))
        self.stop_button.configure(state="disabled")
        self.status_label = ctk.CTkLabel(bottom, text="Готово", anchor="w", text_color=MUTED)
        self.status_label.grid(row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

    def _fit_folder(self, event=None) -> None:
        width = event.width if event is not None else self.folder_label.winfo_width()
        width = max(120, width - 8)
        if width == self._folder_wrap:
            return
        self._folder_wrap = width
        self.folder_label.configure(wraplength=width)

    def _show_folder(self) -> None:
        if self.project is None:
            self.folder_label.configure(text="не выбрана")
        else:
            self.folder_label.configure(text=str(self.project))
        self._refresh_tree()
        self._refresh_chat_list()

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
        natural = max(188, self.bottom.winfo_reqheight())
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
        height = max(160, total - int(top))
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
        if event.keysym == "Insert" and event.state & 0x4:
            return
        if event.state & 0x4:
            action = clipboard_action(str(event.keysym), int(getattr(event, "keycode", 0) or 0))
            if event.keysym.lower() in {"c", "a", "insert"} or action in {"copy", "select"}:
                return
        return "break"

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
        self._apply_ai(data)
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

    def _on_theme_pick(self, label: str) -> None:
        theme = THEME_LABELS.get(label, "dark")
        if theme == self.theme:
            return
        self._apply_theme(theme)
        try:
            save_theme(theme)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить тему: {exc}")

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
        menu = self.profile_menu
        names = [item["name"] for item in self.profiles] or [_NO_PROFILE]
        current = self.active_profile if self.active_profile in names else names[0]
        self.profile_var.set(current)
        if menu is not None and menu.winfo_exists():
            menu.configure(values=names)
            menu.set(current)

    def _on_profile_pick(self, name: str) -> None:
        if name == _NO_PROFILE or name == self.active_profile:
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
        return settings

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
        self.project = Path(selected).resolve()
        self._show_folder()
        self.agent.set_root(self.project)
        self._clear_box(self.chat)
        self._persist("Папка выбрана")

    def new_chat(self) -> None:
        if self.running:
            return
        self._store_chat()
        self.chat_id = None
        self.chat_title = ""
        self.transcript.clear()
        self._clear_box(self.chat)
        self.agent.reset_session(announce=False)
        self._refresh_chat_list()
        self.set_status("Новый чат")

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

    def _refresh_attach(self) -> None:
        if not self.attached:
            self.attach_label.configure(text="")
            return
        names = ", ".join(Path(path).name for path in self.attached)
        self.attach_label.configure(text=f"Изображения: {names}")

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
        self.send()
        return "break"

    def send(self) -> None:
        if self.running:
            return
        text = self.task.get("1.0", "end").strip()
        images = list(self.attached)
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
        if not text and not images:
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
        self.task.delete("1.0", "end")
        self.attached.clear()
        self._refresh_attach()
        note = text or "(только изображение)"
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
        self.running = True
        self.stop_event = threading.Event()
        self.send_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.set_status("Запрос отправлен")
        thread = threading.Thread(target=self._turn, args=(text, prepared, settings), daemon=True)
        thread.start()

    def _turn(self, text: str, images: list, settings: dict) -> None:
        try:
            self.agent.run_turn(text, images, settings, self.stop_event)
        except Exception as exc:
            self.write_chat(f"Ошибка: {exc}")
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
        self.send_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if self.stop_event.is_set():
            self.set_status("Остановлено")
        else:
            self.set_status("Готово")
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_chat_list()

    def confirm(self, path: str, summary: str) -> bool:
        if self.stop_event.is_set():
            return False
        done = threading.Event()
        holder = {"ok": False}

        def ask() -> None:
            try:
                dialog = ConfirmDialog(self, path, summary)
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
        self._remember(text)
        self._append(self.chat, text)

    def write_journal(self, text: str) -> None:
        self.write_chat(f"· {text.strip()}")

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
            save_chat(self.chat_id, title, str(self.project), lines)
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
        self._show_transcript()
        self.agent.reset_session(announce=False)
        self._refresh_chat_list()
        self.set_status("Чат открыт. Модель начинает заново.")

    def delete_selected_chat(self) -> None:
        if self.running:
            return
        selected = self.chat_tree.selection()
        chat_id = str(selected[0]) if selected else (self.chat_id or "")
        if not chat_id:
            self.write_chat("Чат не выбран.")
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
            self._clear_box(self.chat)
            self.agent.reset_session(announce=False)
        self._refresh_chat_list()
        self.set_status("Чат удалён")

    def set_status(self, text: str) -> None:
        self.after(0, lambda: self.status_label.configure(text=text))

    def _role(self, text: str) -> str:
        body = text.lstrip()
        if body.startswith("Вы:"):
            return "user"
        if body.startswith("·"):
            return "tool"
        return "agent"

    def _tag_chat(self) -> None:
        inner = self.chat._textbox
        inner.tag_configure("user", lmargin1=48, lmargin2=48, rmargin=18, foreground=_tone(USER_TEXT), spacing1=8, spacing3=10)
        inner.tag_configure("agent", lmargin1=12, lmargin2=12, rmargin=48, foreground=_tone(TEXT), spacing1=4, spacing3=10)
        inner.tag_configure("tool", lmargin1=28, lmargin2=28, rmargin=28, foreground=_tone(MUTED), spacing1=2, spacing3=4)
        inner.configure(padx=10, pady=8)

    def _insert_block(self, box, text: str) -> None:
        inner = box._textbox
        start = inner.index("end-1c")
        inner.insert("end", text.rstrip() + "\n\n")
        inner.tag_add(self._role(text), start, inner.index("end-1c"))

    def _show_transcript(self) -> None:
        box = self.chat
        if not box.winfo_exists():
            return
        box.configure(state="normal")
        box.delete("1.0", "end")
        for line in self.transcript:
            self._insert_block(box, line)
        box.see("end")
        box.configure(state="normal")

    def _append(self, box, text: str) -> None:
        def write() -> None:
            if not box.winfo_exists():
                return
            box.configure(state="normal")
            self._insert_block(box, text)
            box.see("end")

        self.after(0, write)

    def _clear_box(self, box) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")

    def _set_box(self, box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        if text:
            box.insert("1.0", text)
        box.configure(state="disabled")

    def _ui_error(self, exc, value, tb) -> None:
        self.write_chat(f"Ошибка интерфейса: {value}")

    def _on_close(self) -> None:
        if not self._release_editor(ask=True):
            return
        self.stop_event.set()
        try:
            self._store_chat()
            self.agent.close()
        finally:
            self.destroy()


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
