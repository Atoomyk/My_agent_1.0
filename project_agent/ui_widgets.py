"""Общие Tk-виджеты, иконки и мелкие UI-хелперы.

Модуль самодостаточен: импортирует только стандартную библиотеку, customtkinter,
PIL, ``project_agent.ui_theme`` (цвета) и ``project_agent.paths.normalize_relative``.
Импортировать ``project_agent.app`` отсюда ЗАПРЕЩЕНО (иначе цикл импортов:
app импортирует этот модуль наверх).

Что здесь лежит:
- ``_PENDING`` / ``_TREE_NS`` — префиксы iid дерева файлов (голые ``.env``
  ломают Tk, т.к. выглядят как путь виджета), ``tree_path_iid`` / ``tree_path_rel``;
- ``bind_wheel_scroll`` — колесо мыши для модалок на Windows;
- ``elide``, ``_tone`` — обрезка текста по ширине и выбор light/dark цвета;
- ``_paint_*``, ``_PAINT``, ``glyph``, кэш ``_ICONS`` — векторные иконки 16x16;
- ``quiet_button``, ``icon_button``, ``quiet_menu`` — кнопки/меню в стиле приложения;
- ``_FONT_CATALOG``, ``_font_catalog`` — кэш списка шрифтов с кириллицей.

Кэши ``_ICONS`` и ``_FONT_CATALOG`` — мутабельные глобалы, живут вместе со своими
функциями. Код, которому нужен приватный помощник (например ``_paint_copy``),
импортирует его отсюда напрямую: ``from project_agent.ui_widgets import _paint_copy``.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

import customtkinter as ctk
from PIL import Image, ImageDraw

from project_agent.config import DEFAULT_CODE_FONT, DEFAULT_UI_FONT
from project_agent.paths import normalize_relative
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    ON_SEND,
    PANEL,
    SELECT,
    SEND,
    SEND_HOVER,
    TEXT,
)

_PENDING = "\uE000"
# Prefixed tree iids: bare ".env" / ".gitignore" break Tk (look like widget paths).
_TREE_NS = "n:"


def tree_path_iid(relative: str) -> str:
    text = str(relative or ".").replace("\\", "/").strip()
    if not text or text in {".", "./"}:
        return f"{_TREE_NS}."
    rel = normalize_relative(text)
    if not rel or rel == ".":
        return f"{_TREE_NS}."
    return f"{_TREE_NS}{rel}"


def tree_path_rel(iid: str) -> str:
    text = str(iid or "")
    if text.startswith(_PENDING):
        text = text[len(_PENDING) :]
    if text.startswith(_TREE_NS):
        body = text[len(_TREE_NS) :]
        return body if body else "."
    return text or "."


def bind_wheel_scroll(view, root) -> None:
    """Колесо на Windows идёт в focused-виджет — биндим всё дерево модалки на yview."""

    def on_wheel(event):
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            steps = int(-delta / 120)
            if steps == 0:
                steps = -1 if delta > 0 else 1
            view.yview_scroll(steps, "units")
        elif getattr(event, "num", None) == 4:
            view.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            view.yview_scroll(1, "units")
        else:
            return None
        return "break"

    def walk(widget) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(seq, on_wheel, add="+")
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            walk(child)

    try:
        walk(root)
    except tk.TclError:
        return
_ICONS: dict[str, ctk.CTkImage] = {}


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


def _paint_search(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.ellipse((6, 6, 20, 20), outline=color, width=2)
    draw.line((18, 18, 26, 26), fill=color, width=2)


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
    "search": _paint_search,
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


def quiet_button(
    parent, text, command, width=96, primary=False, mark=None, round_mark=False, height=32, family: str | None = None
):
    face = family or DEFAULT_UI_FONT
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
        height=height,
        corner_radius=max(8, height // 2),
        border_width=0 if primary else 1,
        border_color=BORDER,
        fg_color=SEND if primary else BUTTON,
        hover_color=SEND_HOVER if primary else BUTTON_HOVER,
        text_color=ON_SEND if primary else TEXT,
        font=(face, 12 if height >= 28 else 11),
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


def quiet_menu(parent, variable, values, command=None, family: str | None = None):
    face = family or DEFAULT_UI_FONT
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
        font=(face, 12),
        dropdown_fg_color=PANEL,
        dropdown_text_color=TEXT,
        dropdown_hover_color=SELECT,
        dropdown_font=(face, 12),
    )


_FONT_CATALOG: tuple[list[str], list[str], set[str]] | None = None


def _font_catalog(root) -> tuple[list[str], list[str], set[str]]:
    global _FONT_CATALOG
    if _FONT_CATALOG is not None:
        return _FONT_CATALOG
    names = sorted({str(name) for name in tkfont.families(root)}, key=str.casefold)
    with_cyr: list[str] = []
    without: list[str] = []
    cyr_set: set[str] = set()
    for name in names:
        try:
            has = tkfont.Font(root=root, family=name, size=12).measure("Я") != 0
        except tk.TclError:
            has = False
        if has:
            with_cyr.append(name)
            cyr_set.add(name)
        else:
            without.append(name)
    _FONT_CATALOG = (with_cyr, without, cyr_set)
    return _FONT_CATALOG
