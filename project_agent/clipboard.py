"""Буфер обмена с учётом русской раскладки.

Перенесено из ``project_agent.app`` дословно. Здесь живёт всё, что нужно,
чтобы Ctrl+C/V/X/A работали при ЙЦУКЕН: физические клавиши C V X A приходят
как ``Cyrillic_es``/``Cyrillic_em``/``Cyrillic_che``/``Cyrillic_ef``, а
дополнительно распознаём их по ``keycode`` Windows.

Состав: ``clipboard_action`` (keysym+keycode → действие), ``_selection``
(выделение из Text/Entry), ``set_clipboard`` (запись в буфер Windows через
clear+append+update_idletasks), ``apply_layout_clipboard`` (собственно
операция copy/cut/paste/select) и ``_on_layout_clipboard`` (обработчик
события ``<Control-KeyPress>``).

Модуль **не импортирует** ``project_agent.app`` (иначе цикл) — зависит только
от ``tkinter``.

Реестр только для чтения ``_READONLY_TEXT`` переехал сюда вместе со своими
пользователями. Снаружи его пополняют через ``mark_readonly(widget)``:
``app.py`` не импортирует сам набор, чтобы не получить копию ссылки.
"""

from __future__ import annotations

import tkinter as tk

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
_READONLY_TEXT: set[str] = set()


def mark_readonly(widget) -> None:
    """Пометить виджет как «только чтение»: у него заблокированы paste/cut."""
    _READONLY_TEXT.add(str(widget))


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
