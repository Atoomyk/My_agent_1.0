"""Иконки типов файлов для дерева проекта.

Перенесено из ``project_agent.app`` дословно, без изменения поведения.

Состав:
- ``_tree_kind_for_name`` — по имени и признаку папки выдаёт строковый «вид»
  (``python``, ``json``, ``markdown``, ``git``, ``folder`` и т.д.);
- ``_draw_tree_kind_icon`` — рисует эту иконку через PIL в RGBA-картинку
  нужного размера.

Имена сохранены с подчёркиванием, как в ``ui_widgets`` (``_tone``,
``_paint_copy``): перенос дословный, без переименований.

Свои иконки, а не ассеты VS Code: маленькие схемы на 16 px, чтобы не тащить
в проект чужие ресурсы.

Модуль **не импортирует** ``project_agent.app`` (иначе цикл) и не зависит от
палитры приложения: единственный цвет, который нужен снаружи, — ``muted``
передаётся параметром (``app`` считает его сам через ``_tone(MUTED)``).

Кэш иконок остался в ``App`` — он привязан к теме и размеру шрифта, то есть к
состоянию окна, а не к самим функциям рисования.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _tree_kind_for_name(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    lower = (name or "").lower()
    if lower in {".gitignore", ".gitattributes", ".gitmodules"} or lower.endswith(".git"):
        return "git"
    if lower == ".env" or lower.startswith(".env."):
        return "env"
    if lower in {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}:
        return "docker"
    suffix = Path(name).suffix.lower()
    if suffix in {".py", ".pyw", ".pyi"}:
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "js"
    if suffix in {".ts", ".tsx", ".jsx"}:
        return "ts"
    if suffix in {".json", ".jsonc"}:
        return "json"
    if suffix in {".md", ".markdown", ".mdc"}:
        return "markdown"
    if suffix in {".ps1", ".psm1", ".psd1"}:
        return "powershell"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix in {".toml", ".ini", ".cfg"}:
        return "config"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".css", ".scss", ".sass"}:
        return "css"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}:
        return "image"
    if suffix in {".txt", ".log", ".spec", ".rst"}:
        return "text"
    if suffix in {".bat", ".cmd"}:
        return "shell"
    if suffix in {".sh"}:
        return "shell"
    return "file"


def _draw_tree_kind_icon(kind: str, size: int, muted: str) -> Image.Image:
    """Свои маленькие иконки типов (не ассеты VS Code)."""
    sheet = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    s = size - 1
    m = max(1, size // 16)

    def font(sz: int):
        try:
            path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf"
            if path.is_file():
                return ImageFont.truetype(str(path), size=max(8, sz))
        except OSError:
            pass
        return ImageFont.load_default()

    if kind == "folder":
        color = "#e0a84a"
        draw.rounded_rectangle((2, size // 3, s - 1, s - 2), radius=2, outline=color, width=m + 1)
        draw.polygon([(2, size // 3), (2, size // 5), (size // 2, size // 5), (size // 2 + 2, size // 3)], outline=color)
        draw.line([(2, size // 3), (size // 2 + 2, size // 3)], fill=color, width=m + 1)
    elif kind == "python":
        draw.rounded_rectangle((2, 2, s // 2, s - 2), radius=3, fill="#4584b6")
        draw.rounded_rectangle((s // 2 - 1, 2, s - 2, s - 2), radius=3, fill="#ffde57")
        draw.ellipse((4, 5, 8, 9), fill="#ffde57")
        draw.ellipse((s - 8, s - 9, s - 4, s - 5), fill="#4584b6")
    elif kind == "json":
        color = "#6aab7a"
        f = font(max(9, size - 6))
        draw.text((3, 1), "{}", font=f, fill=color)
    elif kind == "markdown":
        color = "#5b8fd9"
        draw.ellipse((1, 1, s - 1, s - 1), outline=color, width=m + 1)
        f = font(max(8, size - 7))
        draw.text((size // 2 - 2, 2), "M", font=f, fill=color)
    elif kind == "powershell":
        color = "#2b88d8"
        draw.rounded_rectangle((1, 1, s - 1, s - 1), radius=3, outline=color, width=m + 1)
        f = font(max(7, size - 8))
        draw.text((2, 2), ">_", font=f, fill=color)
    elif kind == "git":
        color = "#f05133"
        draw.polygon([(size // 2, 1), (s - 1, size // 2), (size // 2, s - 1), (1, size // 2)], fill=color)
        draw.ellipse((size // 2 - 2, 4, size // 2 + 2, 8), fill="#ffffff")
        draw.ellipse((size // 2 - 2, s - 8, size // 2 + 2, s - 4), fill="#ffffff")
        draw.line([(size // 2, 8), (size // 2, s - 8)], fill="#ffffff", width=m + 1)
    elif kind == "js":
        color = "#c6a000"
        draw.rounded_rectangle((1, 1, s - 1, s - 1), radius=2, fill=color)
        f = font(max(7, size - 8))
        draw.text((2, 3), "JS", font=f, fill="#2c2824")
    elif kind == "ts":
        color = "#3178c6"
        draw.rounded_rectangle((1, 1, s - 1, s - 1), radius=2, fill=color)
        f = font(max(7, size - 8))
        draw.text((2, 3), "TS", font=f, fill="#ffffff")
    elif kind == "yaml":
        color = "#a56ad8"
        draw.rounded_rectangle((3, 1, s - 2, s - 1), radius=2, outline=color, width=m + 1)
        draw.line([(6, 5), (s - 5, 5)], fill=color, width=m)
        draw.line([(6, size // 2), (s - 5, size // 2)], fill=color, width=m)
        draw.line([(6, s - 5), (s - 7, s - 5)], fill=color, width=m)
    elif kind == "config":
        color = "#8d847a"
        draw.ellipse((2, 2, s - 2, s - 2), outline=color, width=m + 1)
        draw.ellipse((size // 2 - 3, size // 2 - 3, size // 2 + 3, size // 2 + 3), outline=color, width=m)
    elif kind == "html":
        color = "#e34c26"
        f = font(max(8, size - 7))
        draw.text((1, 2), "<>", font=f, fill=color)
    elif kind == "css":
        color = "#264de4"
        f = font(max(9, size - 6))
        draw.text((3, 1), "#", font=f, fill=color)
    elif kind == "image":
        color = "#6aab7a"
        draw.rounded_rectangle((1, 2, s - 1, s - 2), radius=2, outline=color, width=m + 1)
        draw.ellipse((4, 5, 8, 9), outline=color, width=m)
        draw.line([(3, s - 4), (7, size // 2), (11, s - 5), (s - 3, 6)], fill=color, width=m + 1)
    elif kind == "env":
        color = "#c48a2e"
        draw.rounded_rectangle((1, 1, s - 1, s - 1), radius=2, outline=color, width=m + 1)
        f = font(max(7, size - 8))
        draw.text((2, 3), "env", font=f, fill=color)
    elif kind == "docker":
        color = "#2496ed"
        draw.rectangle((3, size // 2, s - 3, s - 3), outline=color, width=m + 1)
        for i in range(3):
            x0 = 4 + i * (size // 5)
            draw.rectangle((x0, size // 3, x0 + size // 6, size // 2), outline=color, width=m)
    elif kind == "shell":
        color = "#5a8f6a"
        draw.rounded_rectangle((1, 1, s - 1, s - 1), radius=2, outline=color, width=m + 1)
        draw.line([(4, size // 2), (size // 2, size // 3)], fill=color, width=m + 1)
        draw.line([(4, size // 2), (size // 2, 2 * size // 3)], fill=color, width=m + 1)
        draw.line([(size // 2, 2 * size // 3), (s - 4, 2 * size // 3)], fill=color, width=m + 1)
    elif kind == "text":
        color = muted
        draw.rounded_rectangle((3, 1, s - 2, s - 1), radius=2, outline=color, width=m + 1)
        for y in (5, size // 2, s - 5):
            draw.line([(6, y), (s - 5, y)], fill=color, width=m)
    else:
        color = muted
        draw.rounded_rectangle((3, 1, s - 2, s - 1), radius=2, outline=color, width=m + 1)
        draw.polygon([(s - 7, 1), (s - 2, 1), (s - 2, 6)], fill=color)
    return sheet
