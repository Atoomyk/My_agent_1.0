from __future__ import annotations

import math
import multiprocessing
import os
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
from PIL import Image, ImageDraw, ImageFont, ImageTk

from project_agent.agent import Agent
from project_agent.chats import delete_chat, list_chats, load_chat, new_chat_id, save_chat
from project_agent.plan import (
    clean_plan,
    empty_plan,
    parse_plan_mark,
    plan_mark,
    toggle_step,
)
from project_agent.checkpoints import (
    CheckpointStack,
    checkpoint_mark,
    delete_checkpoints,
    parse_checkpoint_mark,
    restorable_paths,
    restore_one_file,
    restore_files,
)
from project_agent.config import (
    DEFAULT_CODE_FONT,
    DEFAULT_UI_FONT,
    ai_settings,
    config_dir,
    load_config,
    needs_api_key,
    save_agent_mode,
    save_config,
    save_font,
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
from project_agent.editor_tabs import EditorTabs
from project_agent.paths import (
    PathError,
    list_entries,
    normalize_relative,
    read_text_file,
    relative_posix,
    resolve_inside,
)
from project_agent.secrets import Scrubber, SecretVault, literals_from_settings, scrub_outbound
from project_agent.index_store import build_index, index_summary
from project_agent.rules import rule_chip_text, rules_summary
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
    "Plan": "plan",
    "Debug": "debug",
    "Ask": "ask",
}
LABEL_BY_MODE = {value: key for key, value in MODE_LABELS.items()}
THEME_LABELS = {
    "Тёмная": "dark",
    "Светлая": "light",
}
LABEL_BY_THEME = {value: key for key, value in THEME_LABELS.items()}
_PENDING = "\uE000"
# Prefixed tree iids: bare ".env" / ".gitignore" break Tk (look like widget paths).
_TREE_NS = "n:"
_NO_PROFILE = "нет профилей"


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
DIFF_ADD = ("#2f6b3c", "#7dba8a")
DIFF_DEL = ("#a33d3d", "#e08a8a")
DIFF_HUNK = ("#6a5a8a", "#b0a0d0")
GIT_MOD = ("#b8860b", "#e0b84a")  # modified / dirty folder
GIT_NEW = ("#2f6b3c", "#7dba8a")  # untracked / added
GIT_DEL = ("#a33d3d", "#e08a8a")  # deleted (если ещё виден в дереве)
SEARCH_BG = ("#efe0b8", "#4a3f24")
SEARCH_CUR = ("#e0b86a", "#7a5e28")
# Подсветка редактора (P0.2): light / dark, без «AI-purple».
SYN_KEYWORD = ("#8a4b2f", "#e0a070")
SYN_STRING = CODE_TEXT
SYN_COMMENT = MUTED
SYN_NUMBER = ("#2f6b5a", "#7dbaa8")
SYN_DECORATOR = ("#6a5a2a", "#c4b06a")
SYN_HEADING = ("#3a5a7a", "#8ab0d0")
SYN_MD_CODE = ("#0f7b8a", "#6cc7d3")
SYN_LITERAL = ("#8a4b2f", "#e0a070")
CHAT_COLUMN = 820
DIFF_PREVIEW_LINES = 48
HIGHLIGHT_DEBOUNCE_MS = 280


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
_FENCE = re.compile(r"^\s*```")
_INLINE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
_USER_ATTACH_LINE = re.compile(r"^\((?:папки|файлы): .+\)$|^\(изображений: \d+\)$")
_CHAT_STAMP = re.compile(r"\n(\d{2}:\d{2})\s*$")
_RUNNING_TAIL = "выполняется…"
_GROUP_NAMES = 4
# Группы с одним действием не сворачиваем: единственная строка и так информативна.
_GROUP_FOLD = 2
_CHIP_ROWS = 2
_TASK_MIN_H = 56
_COMPOSER_MAX_RATIO = 1 / 3


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
    if parse_checkpoint_mark(body):
        return "checkpoint"
    if parse_plan_mark(body):
        return "plan"
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


def _paint_search(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.ellipse((6, 6, 20, 20), outline=color, width=2)
    draw.line((18, 18, 26, 26), fill=color, width=2)


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


class SettingsWindow(ctk.CTkToplevel):
    _SECTIONS = ("Внешний вид", "Модель", "Проект", "MCP", "Безопасность")

    def __init__(self, app: "App", section: str = "Модель") -> None:
        super().__init__(app)
        self.app = app
        self._pages: dict[str, ctk.CTkScrollableFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        start = section if section in self._SECTIONS else "Модель"
        face = app.ui_font
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
        ctk.CTkLabel(nav, text="Разделы", anchor="w", text_color=MUTED, font=(face, 11)).grid(
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
                font=(face, 12),
                command=lambda section_name=name: self._show(section_name),
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
        self._fill_project(self._pages["Проект"])
        self._fill_mcp(self._pages["MCP"])
        self._fill_security(self._pages["Безопасность"])

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        quiet_button(footer, "Сохранить", app.save_settings, width=160, primary=True, mark="check", family=face).pack(
            side="right"
        )

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._show(start)
        self.after(50, self.focus)

    def _fill_appearance(self, page) -> None:
        app = self.app
        face = app.ui_font
        ctk.CTkLabel(page, text="Внешний вид", anchor="w", text_color=TEXT, font=(face, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app._field(page, "Тема")
        quiet_menu(page, app.theme_var, list(THEME_LABELS), app._on_theme_pick, family=face).pack(
            fill="x", padx=8, pady=4
        )
        app._field(page, "Основной интерфейс", top=10)
        ui_box = ctk.CTkLabel(
            page,
            textvariable=app.ui_font_var,
            anchor="w",
            height=34,
            corner_radius=12,
            fg_color=FIELD,
            text_color=TEXT,
            font=(face, 12),
            cursor="hand2",
        )
        ui_box.pack(fill="x", padx=8, pady=4)
        ui_box.bind("<Button-3>", lambda _e: app._pick_font("ui_font"))
        ctk.CTkLabel(
            page,
            text="ПКМ по названию — выбрать шрифт. Применяется сразу.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(face, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        app._field(page, "Вывод кода и логов", top=10)
        code_box = ctk.CTkLabel(
            page,
            textvariable=app.code_font_var,
            anchor="w",
            height=34,
            corner_radius=12,
            fg_color=FIELD,
            text_color=TEXT,
            font=(face, 12),
            cursor="hand2",
        )
        code_box.pack(fill="x", padx=8, pady=4)
        code_box.bind("<Button-3>", lambda _e: app._pick_font("code_font"))
        ctk.CTkLabel(
            page,
            text="Редактор, консоль тестов, лог MCP, allowlist, диффы. Для кода удобнее моноширинный.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(face, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        quiet_button(page, "По умолчанию", app.reset_fonts, width=140, family=face).pack(
            anchor="w", padx=8, pady=(10, 4)
        )

    def _fill_model(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Модель и профили", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(6, 2)
        )
        app._field(page, "Профиль", top=4)
        app.profile_menu = quiet_menu(page, app.profile_var, [_NO_PROFILE], app._on_profile_pick)
        app.profile_menu.pack(fill="x", padx=8, pady=(2, 2))
        app._bind_profile_rename(app.profile_menu)
        app._sync_profile_menu()
        app._field(page, "Провайдер", top=4)
        quiet_menu(page, app.provider_var, list(PROVIDER_LABELS)).pack(fill="x", padx=8, pady=(2, 2))
        app._entry(page, "URL API", app.base_url_var, "пусто — OpenAI или Anthropic", top=4, gap=2)
        app._entry(page, "Модель", app.model_var, "имя модели", top=4, gap=2)
        app._entry(page, "Ключ", app.api_key_var, "", secret=True, top=4, gap=2)
        app._entry(page, "Лимит шагов", app.steps_var, "25", top=4, gap=2)
        ctk.CTkSwitch(
            page,
            text="Выбирать самый дешёвый провайдер",
            variable=app.prefer_cheap_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(10, 2))
        ctk.CTkLabel(
            page,
            text="В запрос добавляется provider: { sort: \"price\" }. Выкл — авторежим агрегатора. "
            "Настройка общая, не в профиле. Генерацию изображений не затрагивает.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(page, text="Генерация изображений", anchor="w", text_color=TEXT, font=(app.ui_font, 13)).pack(
            fill="x", padx=8, pady=(10, 2)
        )
        ctk.CTkLabel(
            page,
            text="Пустые поля — генерация выключена; URL и ключ по умолчанию из чата выше.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        app._entry(page, "Модель изображений", app.image_model_var, "пусто — генерация выключена", top=4, gap=2)
        app._entry(page, "URL изображений", app.image_url_var, "пусто — URL API", top=4, gap=2)
        app._entry(page, "Ключ изображений", app.image_key_var, "пусто — основной ключ", secret=True, top=4, gap=2)
        ctk.CTkLabel(page, text="Действия с профилем", anchor="w", text_color=TEXT, font=(app.ui_font, 13)).pack(
            fill="x", padx=8, pady=(12, 2)
        )
        ctk.CTkLabel(
            page,
            text="«Сохранить профиль» записывает текущие поля выше в выбранный или новый профиль. "
            "Кнопка «Сохранить» внизу окна — все настройки приложения.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        profile_row = ctk.CTkFrame(page, fg_color="transparent")
        profile_row.pack(fill="x", padx=8, pady=(2, 8))
        quiet_button(profile_row, "Сохранить профиль", app.save_profile, width=188, mark="save").pack(
            side="left", padx=(0, 4)
        )
        quiet_button(profile_row, "Удалить", app.delete_profile, width=112, mark="trash").pack(side="left")

    def _fill_project(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Проект и тесты", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkLabel(
            page,
            text="Пресет тестов — для run_tests. Allowlist — для run_allowed. Почти свободный shell (run_shell) — по чекбоксу ниже, выкл по умолчанию. Каждый запуск с подтверждением.",
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
        ctk.CTkSwitch(
            page,
            text="Почти свободный shell для агента (run_shell)",
            variable=app.agent_shell_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(8, 2))
        ctk.CTkLabel(
            page,
            text="Выкл по умолчанию. Только Agent/Debug; confirm каждый раз; лимит 3/ход. Не интерактивный терминал.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 8))
        app._field(page, "Свои команды allowlist")
        ctk.CTkLabel(
            page,
            text="Встроенные id: unittest, pytest, npm_test, build_ps1. Свои — id: arg1 arg2 …. Для GUI без ожидания: id!: arg1 … (detach).",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        from project_agent.allowed import format_allowed_text

        app.allowed_box = ctk.CTkTextbox(
            page,
            height=88,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            font=(app.code_font, 11),
        )
        app.allowed_box.pack(fill="x", padx=8, pady=4)
        app.allowed_box.insert("1.0", format_allowed_text(getattr(app, "allowed_commands", [])))
        bind_wheel_scroll(app.allowed_box._textbox, app.allowed_box)
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
        ctk.CTkLabel(page, text="MCP-серверы", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app.mcp_list = ctk.CTkTextbox(
            page,
            height=120,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            font=(app.ui_font, 12),
        )
        app.mcp_list.pack(fill="x", padx=8, pady=4)
        app.mcp_list.configure(state="disabled")
        bind_wheel_scroll(app.mcp_list._textbox, app.mcp_list)
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
        ctk.CTkLabel(page, text="Безопасность", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
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
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(8, 4))
        notes = (
            "Ключ и настройки хранятся только в %APPDATA%\\ProjectAgent\\config.json — в программу они не зашиты.\n\n"
            "Перед отправкой модели пароли и похожие значения заменяются метками [[SEC:...]].\n\n"
            "По умолчанию запись файла спрашивает подтверждение. Переключатель выше снимает диалог "
            "только для write_file / apply_patch внутри открытой папки проекта.\n\n"
            "Генерация изображения, run_tests, run_allowed, run_shell и git commit всегда спрашивают подтверждение.\n\n"
            "Почти свободный shell (run_shell) — только если включён в Проект; по умолчанию выкл. "
            "Тесты — пресет; разовые команды — id из allowlist; shell — argv без метасимволов, лимит 3/ход.\n\n"
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
        self.app.allowed_box = None
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
        self._user_bubbles: list[dict] = []
        self._retry_payload: dict | None = None
        self._can_retry = False
        self._retry_buttons: list[tk.Button] = []
        self._checkpoint_buttons: list[tk.Button] = []
        self._plan_buttons: list[tk.Button] = []
        self.chat_plan: dict = empty_plan()
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
        self._fs_watcher = None
        self._watcher_job = None
        self._file_util_mode = "find"
        self._find_hits: list = []
        self._diag_hits: list = []
        self._symbol_hits: list = []
        self._symbol_query_mode = "symbol"
        self.checkpoints = CheckpointStack()
        self.vault = SecretVault()
        self.mcp = McpHub()
        self.toolbox = Toolbox(self.vault, self.confirm, self.mcp, self.collect_settings)
        self.toolbox.on_run_output = self._on_run_output
        self._output_open = False
        self._output_slots: list[dict] = []
        self._output_view = 0
        self._output_pending = ""
        self._output_flush_job = None
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
            on_stopped=self.write_stopped,
            on_checkpoint=self._on_checkpoint,
            on_plan=self._on_plan,
            transcript_len=lambda: len(self.transcript),
            get_plan=lambda: self.chat_plan,
        )
        self.agent.checkpoints = self.checkpoints
        self.theme = "dark"
        self.theme_var = ctk.StringVar(value="Тёмная")
        self.ui_font = DEFAULT_UI_FONT
        self.code_font = DEFAULT_CODE_FONT
        self.ui_font_var = ctk.StringVar(value=DEFAULT_UI_FONT)
        self.code_font_var = ctk.StringVar(value=DEFAULT_CODE_FONT)
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_width = 420
        self.editor_tabs = EditorTabs()
        self._editor_tab_buttons: list = []
        self._highlight_job = None
        self._task_height = _TASK_MIN_H
        self._task_width = 0
        self._fit_composer_job = None
        self._window_h = 0
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
        self.allowed_commands: list[dict] = []
        self.allowed_box = None
        self.agent_shell_enabled = False
        self.agent_shell_var = ctk.BooleanVar(value=False)
        self.auto_write_project = False
        self.auto_write_var = ctk.BooleanVar(value=False)
        self.prefer_cheap_provider = False
        self.prefer_cheap_var = ctk.BooleanVar(value=False)
        self._git_file_status: dict[str, str] = {}
        self._git_dirty_dirs: set[str] = set()
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._ui_error

    def _build(self) -> None:
        self.bind_class("Text", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.bind_class("Entry", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.grid_columnconfigure(0, weight=0, minsize=36)
        self.grid_columnconfigure(1, weight=0, minsize=1)
        self.grid_columnconfigure(2, weight=0, minsize=248)
        self.grid_columnconfigure(3, weight=1)
        self.grid_rowconfigure(0, weight=1)

        rail = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=36)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_propagate(False)
        self.project_badge = ctk.CTkButton(
            rail,
            text="·",
            width=28,
            height=28,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
            fg_color=BUTTON,
            hover_color=BUTTON_HOVER,
            text_color=TEXT,
            font=self._font(11, weight="bold"),
            command=self.choose_folder,
        )
        self.project_badge.place(relx=0.5, y=12, anchor="n")
        icon_button(rail, "gear", self.open_settings, size=28).place(relx=0.5, rely=1.0, y=-12, anchor="s")
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
        self.file_pane.grid_rowconfigure(2, weight=0)
        files_top = ctk.CTkFrame(self.file_pane, fg_color="transparent")
        files_top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        files_top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(files_top, text="Структура", anchor="w", text_color=MUTED, font=self._font(12)).grid(
            row=0, column=0, sticky="w", padx=(6, 0)
        )
        self.file_util_toggle = icon_button(files_top, "search", self.toggle_file_util_panel, size=28)
        self.file_util_toggle.grid(row=0, column=1, padx=(0, 2))
        icon_button(files_top, "refresh", self._refresh_tree, size=28).grid(row=0, column=2)
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
        self._configure_git_tree_tags()
        self._file_util_open = False
        self._build_file_util_panel()
        self._hide_file_util_panel()
        self.chat_tree = ttk.Treeview(self.chat_pane, show="tree", selectmode="browse", style="Chats.Treeview")
        self.chat_tree.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ctk.CTkScrollbar(
            self.chat_pane, command=self.chat_tree.yview, fg_color=PANEL, button_color=BUTTON, button_hover_color=BUTTON_HOVER
        )
        chat_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.chat_tree.configure(yscrollcommand=chat_scroll.set)
        self.chat_tree.bind("<<TreeviewSelect>>", self._on_chat_select)
        self.chat_tree.bind("<Button-3>", self._chat_menu)
        self.side_tabs.set("Файлы")
        self._show_side_tab("Файлы")

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
        head_bar = ctk.CTkFrame(self.center, fg_color="transparent")
        self.head_bar = head_bar
        head_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(12, 6))
        head_bar.grid_columnconfigure(0, weight=1)
        self.chat_head = ctk.CTkLabel(
            head_bar, text="Новый чат", anchor="w", text_color=TEXT, font=self._font(13, weight="bold")
        )
        self.chat_head.grid(row=0, column=0, sticky="ew")
        self.chat_head.bind("<Configure>", lambda _event: self._fit_labels())
        self.git_label = ctk.CTkLabel(
            head_bar, text="", anchor="e", text_color=MUTED, font=self._font(11), cursor="hand2"
        )
        self.git_label.grid(row=0, column=1, padx=(8, 6), sticky="e")
        self.git_label.bind("<Button-1>", lambda _event: self._on_git_badge_click())
        self.run_indicator = ctk.CTkLabel(
            head_bar, text="", anchor="e", text_color=MUTED, font=self._font(11), width=1
        )
        self.run_indicator.grid(row=0, column=2, padx=(4, 6), sticky="e")
        self.output_toggle = quiet_button(head_bar, "Вывод", self.toggle_output_panel, width=72)
        self.output_toggle.grid(row=0, column=3, sticky="e")
        self.center.grid_rowconfigure(2, weight=0)
        self.mid_split = tk.PanedWindow(
            self.center,
            orient="vertical",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_v_double_arrow",
        )
        self.mid_split.grid(row=1, column=0, sticky="nsew")
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
        _READONLY_TEXT.add(str(self.chat._textbox))

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
        _READONLY_TEXT.add(str(self.output_box._textbox))

        self.editor_frame = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        self.editor_frame.grid_columnconfigure(0, weight=1)
        self.editor_frame.grid_rowconfigure(1, weight=1)
        editor_head = ctk.CTkFrame(self.editor_frame, fg_color="transparent")
        editor_head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        editor_head.grid_columnconfigure(0, weight=1)
        self.editor_tabs_bar = ctk.CTkFrame(editor_head, fg_color="transparent")
        self.editor_tabs_bar.grid(row=0, column=0, sticky="ew")
        # Высота как у «Сжать» в футере (24); закрытие — × на вкладке.
        quiet_button(editor_head, "Сохранить", lambda: self._save_editor(confirm=False), width=72, height=24).grid(
            row=0, column=1, padx=(6, 0)
        )
        editor_body = ctk.CTkFrame(self.editor_frame, fg_color=FIELD, corner_radius=0)
        self.editor_body = editor_body
        editor_body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        editor_body.grid_columnconfigure(1, weight=1)
        editor_body.grid_rowconfigure(0, weight=1)
        self.editor_gutter = tk.Text(
            editor_body,
            width=3,
            wrap="none",
            bd=0,
            highlightthickness=0,
            takefocus=0,
            cursor="arrow",
            padx=2,
            pady=2,
            bg=_tone(PANEL),
            fg=_tone(MUTED),
            font=self._editor_gutter_font(),
            state="disabled",
        )
        self.editor_gutter.grid(row=0, column=0, sticky="nsw")
        self.editor_box = ctk.CTkTextbox(
            editor_body,
            wrap="none",
            fg_color=FIELD,
            text_color=TEXT,
            border_width=0,
            corner_radius=0,
            font=self._editor_text_font(),
            scrollbar_button_color=BUTTON,
            scrollbar_button_hover_color=BUTTON_HOVER,
        )
        self.editor_box.grid(row=0, column=1, sticky="nsew")
        inner = self.editor_box._textbox
        inner.configure(yscrollcommand=self._on_editor_scroll)
        if getattr(self.editor_box, "_scrollbar", None) is not None:
            self.editor_box._scrollbar.configure(command=self._editor_yview)
        elif getattr(self.editor_box, "_y_scrollbar", None) is not None:
            self.editor_box._y_scrollbar.configure(command=self._editor_yview)
        inner.bind("<Control-KeyPress>", self._on_editor_control, add="+")
        for seq in ("<KeyRelease>", "<ButtonRelease-1>", "<MouseWheel>", "<Configure>"):
            inner.bind(seq, self._on_editor_change, add="+")
        inner.bind("<<Modified>>", self._on_editor_modified, add="+")
        self.editor_gutter.bind("<MouseWheel>", self._on_gutter_wheel)
        self._configure_editor_syntax_tags()

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
        footer = ctk.CTkFrame(bottom, fg_color="transparent")
        self.footer = footer
        footer.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 8))
        footer.grid_columnconfigure(3, weight=1)
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
            width=96,
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
        quiet_button(footer, "Сжать", self.compress_context, width=48, height=24).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        ctx = ctk.CTkFrame(footer, fg_color="transparent")
        ctx.grid(row=0, column=3, sticky="e", padx=(8, 8))
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
        self.status_label.grid(row=0, column=4, sticky="e")
        self.bind("<Configure>", self._on_window_configure, add="+")
        self.after(80, self._fit_composer)

    def _font(self, size: int, family: str | None = None, weight: str = "normal") -> tuple:
        return (family or self.ui_font, size, weight)

    def _code_font(self, size: int, weight: str = "normal") -> tuple:
        return self._font(size, self.code_font, weight)

    def _editor_gutter_font(self, weight: str = "normal") -> tuple:
        return self._code_font(10, weight)

    def _editor_text_font(self, weight: str = "normal") -> tuple:
        return self._code_font(12, weight)

    def _px_font(self, size: int, family: str | None = None, weight: str = "normal") -> tuple:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        return (family or self.ui_font, -round(size * scale), weight)

    def _measure(self, size: int, weight: str = "normal"):
        key = (size, weight, self.ui_font)
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

    def _build_file_util_panel(self) -> None:
        """Поиск / символы / диагностики под деревом (P0.4 / P0.5 / P1.9)."""
        panel = ctk.CTkFrame(self.file_pane, fg_color=PANEL, corner_radius=10, border_width=1, border_color=BORDER)
        self.file_util_panel = panel
        panel.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        mode_row = ctk.CTkFrame(panel, fg_color="transparent")
        mode_row.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.file_find_mode_btn = quiet_button(mode_row, "Поиск", lambda: self._set_file_util_mode("find"), width=64, height=24)
        self.file_find_mode_btn.pack(side="left", padx=(0, 4))
        self.file_symbol_mode_btn = quiet_button(
            mode_row, "Символы", lambda: self._set_file_util_mode("symbols"), width=72, height=24
        )
        self.file_symbol_mode_btn.pack(side="left", padx=(0, 4))
        self.file_diag_mode_btn = quiet_button(
            mode_row, "Проблемы", lambda: self._set_file_util_mode("problems"), width=80, height=24
        )
        self.file_diag_mode_btn.pack(side="left")
        self.file_util_status = ctk.CTkLabel(mode_row, text="", anchor="e", text_color=MUTED, font=self._font(10))
        self.file_util_status.pack(side="right")

        self.file_find_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_find_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        self.file_find_row.grid_columnconfigure(0, weight=1)
        self.file_find_var = ctk.StringVar(value="")
        self.file_find_entry = ctk.CTkEntry(
            self.file_find_row,
            textvariable=self.file_find_var,
            placeholder_text="Найти в файлах…",
            height=26,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            font=self._font(11),
        )
        self.file_find_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.file_find_entry.bind("<Return>", lambda _e: self._run_file_find())
        quiet_button(self.file_find_row, "Найти", self._run_file_find, width=56, height=24).grid(row=0, column=1)

        self.file_symbol_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_symbol_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        self.file_symbol_row.grid_columnconfigure(0, weight=1)
        self.file_symbol_var = ctk.StringVar(value="")
        self.file_symbol_entry = ctk.CTkEntry(
            self.file_symbol_row,
            textvariable=self.file_symbol_var,
            placeholder_text="Имя символа или модуль…",
            height=26,
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            font=self._font(11),
        )
        self.file_symbol_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.file_symbol_entry.bind("<Return>", lambda _e: self._run_symbol_find())
        self.file_symbol_kind_btn = quiet_button(
            self.file_symbol_row, "Символ", self._toggle_symbol_query_mode, width=72, height=24
        )
        self.file_symbol_kind_btn.grid(row=0, column=1, padx=(0, 4))
        quiet_button(self.file_symbol_row, "Найти", self._run_symbol_find, width=56, height=24).grid(row=0, column=2)

        self.file_diag_row = ctk.CTkFrame(panel, fg_color="transparent")
        self.file_diag_row.grid(row=1, column=0, sticky="ew", padx=6, pady=2)
        quiet_button(self.file_diag_row, "Проверить py", self._run_py_compile_check, width=100, height=24).pack(
            side="left"
        )
        quiet_button(self.file_diag_row, "Из вывода", self._diag_from_output, width=88, height=24).pack(
            side="left", padx=(4, 0)
        )
        quiet_button(self.file_diag_row, "Очистить", self._clear_diagnostics, width=72, height=24).pack(side="left", padx=(4, 0))

        list_holder = ctk.CTkFrame(panel, fg_color="transparent")
        list_holder.grid(row=2, column=0, sticky="nsew", padx=6, pady=(2, 6))
        list_holder.grid_columnconfigure(0, weight=1)
        list_holder.grid_rowconfigure(0, weight=1)
        self.file_hit_list = tk.Listbox(
            list_holder,
            activestyle="dotbox",
            exportselection=False,
            bd=0,
            highlightthickness=0,
            bg=_tone(FIELD),
            fg=_tone(TEXT),
            selectbackground=_tone(SELECT),
            selectforeground=_tone(TEXT),
            font=self._code_font(10),
        )
        self.file_hit_list.grid(row=0, column=0, sticky="nsew")
        hit_scroll = ctk.CTkScrollbar(
            list_holder,
            command=self.file_hit_list.yview,
            fg_color=PANEL,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
        )
        hit_scroll.grid(row=0, column=1, sticky="ns", padx=(4, 0))
        self.file_hit_list.configure(yscrollcommand=hit_scroll.set)
        self.file_hit_list.bind("<Double-Button-1>", self._on_file_hit_activate)
        self.file_hit_list.bind("<Return>", self._on_file_hit_activate)
        self._set_file_util_mode("find")

    def toggle_file_util_panel(self) -> None:
        if self._file_util_open:
            self._hide_file_util_panel()
        else:
            self._show_file_util_panel()

    def _show_file_util_panel(self, mode: str | None = None) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        if mode is not None:
            self._set_file_util_mode(mode)
        try:
            panel.grid()
            self.file_pane.grid_rowconfigure(1, weight=3)
            self.file_pane.grid_rowconfigure(2, weight=2)
        except tk.TclError:
            return
        self._file_util_open = True
        toggle = getattr(self, "file_util_toggle", None)
        if toggle is not None:
            try:
                toggle.configure(fg_color=BUTTON_HOVER)
            except tk.TclError:
                pass
        if self._file_util_mode == "find":
            try:
                self.file_find_entry.focus_set()
            except tk.TclError:
                pass
        elif self._file_util_mode == "symbols":
            try:
                self.file_symbol_entry.focus_set()
            except tk.TclError:
                pass

    def _hide_file_util_panel(self) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        try:
            panel.grid_remove()
            self.file_pane.grid_rowconfigure(1, weight=1)
            self.file_pane.grid_rowconfigure(2, weight=0)
        except tk.TclError:
            return
        self._file_util_open = False
        toggle = getattr(self, "file_util_toggle", None)
        if toggle is not None:
            try:
                toggle.configure(fg_color="transparent")
            except tk.TclError:
                pass

    def _set_file_util_mode(self, mode: str) -> None:
        if mode == "problems":
            self._file_util_mode = "problems"
        elif mode == "symbols":
            self._file_util_mode = "symbols"
        else:
            self._file_util_mode = "find"
        self.file_find_row.grid_remove()
        self.file_symbol_row.grid_remove()
        self.file_diag_row.grid_remove()
        if self._file_util_mode == "find":
            self.file_find_row.grid()
            self._fill_file_hit_list(self._find_hits)
        elif self._file_util_mode == "symbols":
            self.file_symbol_row.grid()
            self._sync_symbol_kind_button()
            self._fill_file_hit_list(self._symbol_hits)
        else:
            self.file_diag_row.grid()
            self._fill_file_hit_list(self._diag_hits)

    def _sync_symbol_kind_button(self) -> None:
        btn = getattr(self, "file_symbol_kind_btn", None)
        if btn is None:
            return
        label = "Importers" if self._symbol_query_mode == "importers" else "Символ"
        try:
            btn.configure(text=label)
        except tk.TclError:
            return

    def _toggle_symbol_query_mode(self) -> None:
        self._symbol_query_mode = "importers" if self._symbol_query_mode == "symbol" else "symbol"
        self._sync_symbol_kind_button()
        entry = getattr(self, "file_symbol_entry", None)
        if entry is None:
            return
        hint = "Модуль / путь — кто импортирует…" if self._symbol_query_mode == "importers" else "Имя символа или модуль…"
        try:
            entry.configure(placeholder_text=hint)
        except tk.TclError:
            pass

    def _style_file_util_panel(self) -> None:
        panel = getattr(self, "file_util_panel", None)
        if panel is None:
            return
        try:
            panel.configure(fg_color=PANEL, border_color=BORDER)
            self.file_find_entry.configure(fg_color=FIELD, border_color=BORDER, text_color=TEXT, font=self._font(11))
            self.file_symbol_entry.configure(fg_color=FIELD, border_color=BORDER, text_color=TEXT, font=self._font(11))
            self.file_util_status.configure(text_color=MUTED, font=self._font(10))
            self.file_hit_list.configure(
                bg=_tone(FIELD),
                fg=_tone(TEXT),
                selectbackground=_tone(SELECT),
                selectforeground=_tone(TEXT),
                font=self._code_font(10),
            )
            toggle = getattr(self, "file_util_toggle", None)
            if toggle is not None:
                toggle.configure(
                    image=glyph("search"),
                    fg_color=BUTTON_HOVER if self._file_util_open else "transparent",
                    hover_color=BUTTON_HOVER,
                )
        except tk.TclError:
            return

    def _fill_file_hit_list(self, items: list) -> None:
        box = getattr(self, "file_hit_list", None)
        if box is None:
            return
        try:
            box.delete(0, "end")
            for item in items:
                box.insert("end", item.label())
        except tk.TclError:
            return
        count = len(items)
        if self._file_util_mode == "problems":
            self.file_util_status.configure(text=f"{count}" if count else "чисто")
        else:
            self.file_util_status.configure(text=f"{count}" if count else "")

    def _run_file_find(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        query = (self.file_find_var.get() or "").strip()
        if not query:
            self.set_status("Введите строку поиска")
            return
        from project_agent.findfiles import search_project

        self._find_hits = search_project(self.project, query, limit=80)
        self._set_file_util_mode("find")
        if not self._find_hits:
            self.set_status("Совпадений нет")
        else:
            self.set_status(f"Найдено: {len(self._find_hits)}")

    def _run_symbol_find(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        query = (self.file_symbol_var.get() or "").strip()
        if not query:
            self.set_status("Введите имя символа или модуля")
            return
        from project_agent.index_store import search_importers, search_symbols

        if self._symbol_query_mode == "importers":
            hits, _data = search_importers(self.project, query, limit=80)
            self._symbol_hits = hits
            label = "Importers"
        else:
            hits, _data = search_symbols(self.project, query, limit=80)
            self._symbol_hits = hits
            label = "Символы"
        self._set_file_util_mode("symbols")
        if not self._symbol_hits:
            self.set_status(f"{label}: нет совпадений")
        else:
            self.set_status(f"{label}: {len(self._symbol_hits)}")

    def _run_py_compile_check(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        from project_agent.diagnostics import compile_project_python

        self.set_status("Проверка синтаксиса…")
        self.update_idletasks()
        self._diag_hits = compile_project_python(self.project)
        self._set_file_util_mode("problems")
        if not self._diag_hits:
            self.set_status("Синтаксис Python: ок")
        else:
            self.set_status(f"Проблемы: {len(self._diag_hits)}")

    def _diag_from_output(self) -> None:
        if self.project is None:
            self.set_status("Сначала выберите папку проекта")
            return
        text = ""
        if self._output_slots:
            text = str(self._output_slots[0].get("text") or "")
        if not text.strip():
            self.set_status("Вывод пуст")
            return
        from project_agent.diagnostics import parse_python_locations

        self._diag_hits = parse_python_locations(text, self.project)
        self._set_file_util_mode("problems")
        if not self._diag_hits:
            self.set_status("В выводе нет path:line")
        else:
            self.set_status(f"Из вывода: {len(self._diag_hits)}")

    def _clear_diagnostics(self) -> None:
        self._diag_hits = []
        if self._file_util_mode == "problems":
            self._fill_file_hit_list(self._diag_hits)
        self.set_status("Проблемы очищены")

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

    def _on_file_hit_activate(self, _event=None) -> None:
        box = getattr(self, "file_hit_list", None)
        if box is None:
            return
        try:
            selection = box.curselection()
        except tk.TclError:
            return
        if not selection:
            return
        index = int(selection[0])
        if self._file_util_mode == "find":
            items = self._find_hits
        elif self._file_util_mode == "symbols":
            items = self._symbol_hits
        else:
            items = self._diag_hits
        if index < 0 or index >= len(items):
            return
        item = items[index]
        self._open_file_at(str(item.path), int(item.line or 1))

    def _show_folder(self) -> None:
        letter = (self.project.name[:1] if self.project is not None else "") or "·"
        self.project_badge.configure(text=letter.upper())
        self._fit_labels()
        self._refresh_tree()
        self._refresh_chat_list()
        self._refresh_index_label()
        self._refresh_rules_label()
        self._refresh_attach()
        self._refresh_git_badge()
        self._restart_project_watch()

    def _restart_project_watch(self) -> None:
        self._stop_project_watch()
        if self.project is None or self._closing:
            return
        from project_agent.fswatch import HAS_WATCHDOG, ProjectWatcher

        if not HAS_WATCHDOG:
            return
        if self._fs_watcher is None:
            self._fs_watcher = ProjectWatcher()
        ok = self._fs_watcher.start(self.project, self._on_fs_change)
        if not ok:
            return

    def _stop_project_watch(self) -> None:
        job = self._watcher_job
        self._watcher_job = None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        watcher = self._fs_watcher
        if watcher is not None:
            watcher.stop()

    def _on_fs_change(self) -> None:
        """Колбэк из потока watchdog → в UI-поток с debounce."""
        if self._closing:
            return
        try:
            self.after(0, self._schedule_watcher_refresh)
        except Exception:
            return

    def _schedule_watcher_refresh(self) -> None:
        if self._closing or self.project is None:
            return
        job = self._watcher_job
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        self._watcher_job = self.after(450, self._run_watcher_refresh)

    def _run_watcher_refresh(self) -> None:
        self._watcher_job = None
        if self._closing or self.project is None:
            return
        try:
            self._refresh_tree()
            self._refresh_git_badge()
            if self.editor_open:
                self._reload_clean_editor()
        except Exception:
            return

    def _refresh_git_badge(self) -> None:
        label = getattr(self, "git_label", None)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            return
        if self.project is None:
            try:
                label.configure(text="", text_color=_tone(MUTED))
            except tk.TclError:
                pass
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            return
        text = self._reload_git_decorations(apply=True)
        try:
            dirty = bool(text) and not str(text).endswith(" · clean")
            label.configure(text=text, text_color=_tone(GIT_MOD if dirty else MUTED))
        except tk.TclError:
            return

    def _on_git_badge_click(self) -> None:
        if self.project is None or self.running:
            return
        try:
            from project_agent.gitops import GitError, git_status

            result = git_status(self.project)
            body = result.output or "(чисто)"
            self.write_chat(f"git status:\n{body}")
            from project_agent.gitops import dirty_ancestor_dirs, format_git_badge, parse_status_head, parse_status_paths

            self._git_file_status = parse_status_paths(result.output)
            self._git_dirty_dirs = dirty_ancestor_dirs(self._git_file_status)
            branch, modified, untracked = parse_status_head(result.output)
            badge = format_git_badge(branch, modified, untracked)
            label = getattr(self, "git_label", None)
            if label is not None:
                try:
                    dirty = bool(badge) and not str(badge).endswith(" · clean")
                    label.configure(text=badge, text_color=_tone(GIT_MOD if dirty else MUTED))
                except tk.TclError:
                    pass
            self._apply_git_tree_tags()
        except GitError as exc:
            self.write_chat(f"git: {exc}")
        except Exception as exc:
            self.write_chat(f"git: {exc}")

    def _reload_git_decorations(self, apply: bool = False) -> str:
        """Обновить кэш dirty-путей. Возвращает текст Git badge или ''."""
        self._git_file_status = {}
        self._git_dirty_dirs = set()
        if self.project is None:
            return ""
        try:
            from project_agent.gitops import (
                dirty_ancestor_dirs,
                format_git_badge,
                git_status,
                parse_status_head,
                parse_status_paths,
            )

            result = git_status(self.project)
            self._git_file_status = parse_status_paths(result.output)
            self._git_dirty_dirs = dirty_ancestor_dirs(self._git_file_status)
            branch, modified, untracked = parse_status_head(result.output)
            badge = format_git_badge(branch, modified, untracked)
        except Exception:
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            badge = ""
        if apply:
            self._apply_git_tree_tags()
        return badge

    def _clear_tree_label_cache(self) -> None:
        self._tree_text_photos = {}
        self._tree_kind_icons = {}
        self._pil_font_cache = {}

    def _tree_label_px(self) -> int:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        return max(14, round(14 * scale))

    def _tree_icon_px(self) -> int:
        return max(16, self._tree_label_px())

    def _pil_ui_font(self, size: int = 14):
        key = (self.ui_font, size)
        cache = getattr(self, "_pil_font_cache", None)
        if cache is None:
            self._pil_font_cache = {}
            cache = self._pil_font_cache
        hit = cache.get(key)
        if hit is not None:
            return hit
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        family = (self.ui_font or "").lower().replace(" ", "")
        mapped = {
            "segoeui": ["segoeui.ttf"],
            "arial": ["arial.ttf"],
            "tahoma": ["tahoma.ttf"],
            "calibri": ["calibri.ttf"],
            "verdana": ["verdana.ttf"],
            "timesnewroman": ["times.ttf"],
            "couriernew": ["cour.ttf"],
        }
        candidates: list[Path] = []
        for name, files in mapped.items():
            if name == family or name in family or family in name:
                candidates.extend(fonts_dir / item for item in files)
        raw = (self.ui_font or "").replace(" ", "")
        if raw:
            candidates.append(fonts_dir / f"{raw}.ttf")
            candidates.append(fonts_dir / f"{raw}.TTF")
        candidates.extend(
            [
                fonts_dir / "segoeui.ttf",
                fonts_dir / "arial.ttf",
                fonts_dir / "tahoma.ttf",
            ]
        )
        for path in candidates:
            try:
                if path.is_file():
                    font = ImageFont.truetype(str(path), size=size)
                    cache[key] = font
                    return font
            except OSError:
                continue
        font = ImageFont.load_default()
        cache[key] = font
        return font

    def _tree_caption_rgba(self, text: str, fill: str) -> Image.Image:
        px = self._tree_label_px()
        font = self._pil_ui_font(px)
        left, top, right, bottom = font.getbbox(text or " ")
        glyph_h = max(1, bottom - top)
        width = max(1, right - left + 2)
        # Высота = глиф; иначе пустой низ сдвигает текст вверх относительно иконки.
        height = glyph_h + 2
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(sheet).text((-left + 1, -top + 1), text or " ", font=font, fill=fill)
        return sheet

    def _tree_kind_icon_rgba(self, kind: str) -> Image.Image:
        mode = ctk.get_appearance_mode()
        size = self._tree_icon_px()
        key = (kind, mode, size)
        cache = getattr(self, "_tree_kind_icons", None)
        if cache is None:
            self._tree_kind_icons = {}
            cache = self._tree_kind_icons
        hit = cache.get(key)
        if hit is not None:
            return hit
        icon = _draw_tree_kind_icon(kind, size, _tone(MUTED))
        cache[key] = icon
        return icon

    def _tree_text_photo(self, text: str, fill: str, kind: str = "file") -> ImageTk.PhotoImage:
        mode = ctk.get_appearance_mode()
        px = self._tree_label_px()
        icon_px = self._tree_icon_px()
        key = (text, fill, mode, self.ui_font, px, kind, icon_px)
        cache = getattr(self, "_tree_text_photos", None)
        if cache is None:
            self._tree_text_photos = {}
            cache = self._tree_text_photos
        hit = cache.get(key)
        if hit is not None:
            return hit
        icon = self._tree_kind_icon_rgba(kind)
        caption = self._tree_caption_rgba(text, fill)
        gap = max(4, px // 4)
        height = max(icon.height, caption.height)
        width = icon.width + gap + caption.width
        sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # Центр по общей высоте: иконка и подпись на одной оптической линии.
        sheet.paste(icon, (0, (height - icon.height) // 2), icon)
        sheet.paste(caption, (icon.width + gap, (height - caption.height) // 2), caption)
        photo = ImageTk.PhotoImage(sheet)
        cache[key] = photo
        if len(cache) > 800:
            self._tree_text_photos = {key: photo}
        return photo

    def _configure_git_tree_tags(self) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        try:
            tree.tag_configure("file")
            tree.tag_configure("dir")
            tree.tag_configure("git_modified")
            tree.tag_configure("git_untracked")
            tree.tag_configure("git_deleted")
            tree.tag_configure("git_dir_dirty")
        except tk.TclError:
            return

    def _tree_item_tags(self, rel: str, is_dir: bool) -> tuple[str, ...]:
        from project_agent.gitops import git_path_kind

        if is_dir:
            if rel == ".":
                if self._git_file_status or self._git_dirty_dirs:
                    return ("dir", "git_dir_dirty")
                return ("dir",)
            if rel in self._git_dirty_dirs or git_path_kind(self._git_file_status, rel):
                return ("dir", "git_dir_dirty")
            return ("dir",)
        kind = git_path_kind(self._git_file_status, rel)
        if kind == "modified":
            return ("file", "git_modified")
        if kind == "untracked":
            return ("file", "git_untracked")
        if kind == "deleted":
            return ("file", "git_deleted")
        return ("file",)

    def _tree_item_fill(self, tags: tuple[str, ...] | list[str] | set[str]) -> str:
        tagset = set(tags or ())
        if "git_deleted" in tagset:
            return _tone(GIT_DEL)
        if "git_untracked" in tagset:
            return _tone(GIT_NEW)
        if "git_modified" in tagset or "git_dir_dirty" in tagset:
            return _tone(GIT_MOD)
        return _tone(TEXT)

    def _tree_item_caption(self, name: str, is_dir: bool, tags: tuple[str, ...] | list[str]) -> str:
        # У dirty-папок точка справа через 5 пробелов (цвет имени — в PIL-подписи).
        if is_dir and "git_dir_dirty" in set(tags or ()):
            return f"{name}     ●"
        return name

    def _tree_item_image(self, name: str, is_dir: bool, tags: tuple[str, ...] | list[str]) -> ImageTk.PhotoImage:
        caption = self._tree_item_caption(name, is_dir, tags)
        kind = _tree_kind_for_name(name, is_dir)
        return self._tree_text_photo(caption, self._tree_item_fill(tags), kind=kind)

    def _tree_entry_name(self, iid: str) -> str:
        rel = tree_path_rel(iid)
        if rel == ".":
            if self.project is not None:
                return self.project.name or str(self.project)
            return "."
        return rel.rsplit("/", 1)[-1]

    def _apply_git_tree_tags(self) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return

        def walk(node: str) -> None:
            for child in tree.get_children(node):
                iid = str(child)
                if iid.startswith(_PENDING):
                    continue
                rel = tree_path_rel(iid)
                tags = set(tree.item(iid, "tags") or ())
                is_dir = rel == "." or "dir" in tags
                if "file" in tags:
                    is_dir = False
                try:
                    new_tags = self._tree_item_tags(rel, is_dir)
                    name = self._tree_entry_name(iid)
                    tree.item(
                        iid,
                        text="",
                        tags=new_tags,
                        image=self._tree_item_image(name, is_dir, new_tags),
                    )
                except tk.TclError:
                    continue
                if is_dir:
                    walk(iid)

        try:
            walk("")
        except tk.TclError:
            return

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
        if pad != self._chat_pad:
            self._chat_pad = pad
            self.chat._textbox.configure(padx=pad)
        bubble_w = self._user_bubble_width()
        if getattr(self, "_bubble_width", None) != bubble_w:
            self._bubble_width = bubble_w
            self._restyle_user_bubbles()

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

    def _on_window_configure(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            height = int(self.winfo_height())
        except tk.TclError:
            return
        if height < 200 or height == self._window_h:
            return
        self._window_h = height
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

    def _send_or_stop(self) -> None:
        if self.running:
            self.stop()
        else:
            self.send()

    def _show_running(self, running: bool) -> None:
        self.send_button.configure(image=glyph("halt" if running else "send"))

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
        # Подписи файлов — цветные PIL-image (ttk tag foreground на Windows ненадёжен).
        self._clear_tree_label_cache()
        style.configure(
            "Project.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            borderwidth=0,
            # Межстрочный зазор −30% относительно прежнего (px+8).
            rowheight=max(self._tree_label_px() + 2, round(self._tree_label_px() + 8 * 0.7)),
            font=(self.ui_font, 14),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", _tone(SELECT))],
        )
        style.configure(
            "Chats.Treeview",
            background=_tone(PANEL),
            fieldbackground=_tone(PANEL),
            foreground=_tone(TEXT),
            borderwidth=0,
            rowheight=32,
            indent=0,
            font=(self.ui_font, 10),
        )
        style.layout("Chats.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout("Project.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.layout(
            "Project.Treeview.Item",
            [
                (
                    "Treeitem.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            ("Treeitem.indicator", {"side": "left", "sticky": ""}),
                            ("Treeitem.image", {"side": "left", "sticky": ""}),
                            ("Treeitem.text", {"side": "left", "sticky": ""}),
                        ],
                    },
                )
            ],
        )
        style.layout("Chats.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [("Treeitem.text", {"sticky": "nswe"})]})])
        style.map(
            "Chats.Treeview",
            background=[("selected", _tone(SELECT))],
            foreground=[("selected", _tone(TEXT))],
        )
        self._configure_git_tree_tags()

    def _refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        reopen = self._expanded_paths()
        self.tree.delete(*self.tree.get_children())
        if self.project is None or not self.project.is_dir():
            self._git_file_status = {}
            self._git_dirty_dirs = set()
            self.tree.insert("", "end", text="Папка не выбрана")
            return
        self._reload_git_decorations(apply=False)
        root_tags = self._tree_item_tags(".", True)
        root_name = self.project.name or str(self.project)
        root_iid = tree_path_iid(".")
        self.tree.insert(
            "",
            "end",
            iid=root_iid,
            text="",
            open=True,
            tags=root_tags,
            image=self._tree_item_image(root_name, True, root_tags),
        )
        self._fill_node(root_iid, reopen)

    def _expanded_paths(self) -> set[str]:
        found: set[str] = set()

        def walk(node: str) -> None:
            for child in self.tree.get_children(node):
                child = str(child)
                if child.startswith(_PENDING):
                    continue
                if self.tree.item(child, "open"):
                    found.add(tree_path_rel(child))
                    walk(child)

        walk("")
        return found

    def _fill_node(self, iid: str, reopen: set[str]) -> None:
        for child in self.tree.get_children(iid):
            self.tree.delete(child)
        parent_rel = tree_path_rel(iid)
        try:
            entries, truncated = list_entries(self.project, parent_rel)
        except PathError:
            self.tree.insert(iid, "end", text="(не прочитано)")
            return
        for name, is_dir in entries:
            rel = name if parent_rel == "." else f"{parent_rel}/{name}"
            child_iid = tree_path_iid(rel)
            tags = self._tree_item_tags(rel, is_dir)
            self.tree.insert(
                iid,
                "end",
                iid=child_iid,
                text="",
                open=False,
                tags=tags,
                image=self._tree_item_image(name, is_dir, tags),
            )
            if not is_dir:
                continue
            if rel in reopen:
                self.tree.item(child_iid, open=True)
                self._fill_node(child_iid, reopen)
            else:
                self.tree.insert(child_iid, "end", iid=f"{_PENDING}{child_iid}", text="")
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
            self._reload_git_decorations(apply=False)
            self._fill_node(iid, set())
        else:
            self._reload_git_decorations(apply=True)

    def _on_file_click(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected or self.project is None:
            return
        iid = str(selected[0])
        if "file" not in self.tree.item(iid, "tags"):
            return
        relative = tree_path_rel(iid)
        if self.editor_open and relative == self.editor_path:
            return
        self._open_file_at(relative, 1)

    def _open_file_at(self, relative: str, line: int = 1) -> None:
        if self.project is None:
            return
        relative = normalize_relative(relative)
        if not relative:
            return
        self._flush_editor_to_tab()
        existing = self.editor_tabs.find(relative)
        if existing >= 0:
            self.editor_tabs.select(existing)
            self._load_active_tab_into_editor()
            self._rebuild_editor_tab_bar()
            self._jump_editor_line(line)
            return
        try:
            text = self._editor_source(relative)
        except PathError as exc:
            self.write_chat(f"Файл не открыт: {exc}")
            return
        self.editor_tabs.open(relative, text)
        self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()
        self.after(20, lambda: self._jump_editor_line(line))

    def _jump_editor_line(self, line: int) -> None:
        if not self.editor_open:
            return
        try:
            target = max(1, min(int(line or 1), self._editor_line_count()))
            self.editor_box.mark_set("insert", f"{target}.0")
            self.editor_box.see(f"{target}.0")
            self.editor_box.focus_set()
            self._sync_editor_gutter()
            self.set_status(f"{self.editor_path}:{target}")
        except (tk.TclError, ValueError, TypeError):
            return

    def _flush_editor_to_tab(self) -> None:
        if not self.editor_open or not self.editor_tabs:
            return
        self.editor_tabs.sync_active_text(self._editor_text())

    def _load_active_tab_into_editor(self) -> None:
        tab = self.editor_tabs.active()
        if tab is None:
            return
        self.editor_path = tab.path
        self.editor_saved = tab.saved
        self.editor_box.delete("1.0", "end")
        if tab.text:
            self.editor_box.insert("1.0", tab.text)
        self.editor_box.mark_set("insert", "1.0")
        self.editor_box.see("1.0")
        try:
            self.editor_box._textbox.edit_modified(False)
        except tk.TclError:
            pass
        self._update_editor_gutter()
        self._highlight_editor()
        if not self.editor_open:
            self.work.add(self.editor_frame, stretch="always", minsize=240, sticky="nsew")
            self.editor_open = True
            self.after(30, self._place_editor_sash)
        self.editor_box.focus_set()

    def _rebuild_editor_tab_bar(self) -> None:
        bar = getattr(self, "editor_tabs_bar", None)
        if bar is None:
            return
        for child in bar.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._editor_tab_buttons = []
        face = self.ui_font or DEFAULT_UI_FONT
        for index, tab in enumerate(self.editor_tabs.tabs):
            active = index == self.editor_tabs.active_index
            label = tab.label()
            name_w = max(48, min(120, 10 + len(label) * 7))
            wrap = ctk.CTkFrame(
                bar,
                fg_color=SEND if active else BUTTON,
                corner_radius=8,
                border_width=0 if active else 1,
                border_color=BORDER,
            )
            wrap.pack(side="left", padx=(0, 4))
            name_btn = ctk.CTkButton(
                wrap,
                text=label,
                width=name_w,
                height=22,
                corner_radius=6,
                border_width=0,
                fg_color="transparent",
                hover_color=SEND_HOVER if active else BUTTON_HOVER,
                text_color=ON_SEND if active else TEXT,
                font=(face, 11),
                command=lambda i=index: self._select_editor_tab(i),
            )
            name_btn.pack(side="left", padx=(4, 0), pady=1)
            close_btn = ctk.CTkButton(
                wrap,
                text="×",
                width=22,
                height=22,
                corner_radius=6,
                border_width=0,
                fg_color="transparent",
                hover_color=SEND_HOVER if active else BUTTON_HOVER,
                text_color=ON_SEND if active else MUTED,
                font=(face, 14),
                command=lambda i=index: self._close_editor_tab(i),
            )
            close_btn.pack(side="left", padx=(0, 2), pady=1)
            self._editor_tab_buttons.append(name_btn)

    def _refresh_editor_tab_labels(self) -> None:
        buttons = getattr(self, "_editor_tab_buttons", None) or []
        tabs = self.editor_tabs.tabs
        if len(buttons) != len(tabs):
            self._rebuild_editor_tab_bar()
            return
        for btn, tab in zip(buttons, tabs):
            label = tab.label()
            try:
                if btn.cget("text") != label:
                    self._rebuild_editor_tab_bar()
                    return
            except tk.TclError:
                self._rebuild_editor_tab_bar()
                return

    def _select_editor_tab(self, index: int) -> None:
        if index == self.editor_tabs.active_index:
            return
        self._flush_editor_to_tab()
        if self.editor_tabs.select(index) is None:
            return
        self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()

    def _reload_clean_editor(self) -> None:
        if not self.editor_open or self.project is None or not self.editor_tabs:
            return
        self._flush_editor_to_tab()
        active_changed = False
        for tab in list(self.editor_tabs.tabs):
            if tab.dirty:
                continue
            try:
                text = self._editor_source(tab.path)
            except PathError:
                continue
            if self.editor_tabs.apply_disk_if_clean(tab.path, text):
                active_changed = True
        if active_changed:
            self._load_active_tab_into_editor()
        self._refresh_editor_tab_labels()

    def _editor_source(self, relative: str) -> str:
        text = read_text_file(self.project, relative)
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _editor_text(self) -> str:
        return self.editor_box.get("1.0", "end-1c")

    def _editor_line_count(self) -> int:
        try:
            return max(1, int(str(self.editor_box._textbox.index("end-1c")).split(".")[0]))
        except (tk.TclError, ValueError, AttributeError):
            return 1

    def _editor_dirty(self) -> bool:
        if not self.editor_open:
            return False
        self._flush_editor_to_tab()
        tab = self.editor_tabs.active()
        return bool(tab and tab.dirty)

    def _style_editor_gutter(self) -> None:
        gutter = getattr(self, "editor_gutter", None)
        if gutter is None:
            return
        try:
            gutter.configure(
                bg=_tone(PANEL),
                fg=_tone(MUTED),
                font=self._editor_gutter_font(),
                insertbackground=_tone(TEXT),
            )
        except tk.TclError:
            return
        body = getattr(self, "editor_body", None)
        if body is not None:
            try:
                body.configure(fg_color=FIELD)
            except tk.TclError:
                pass
        self._configure_editor_syntax_tags()
        if self.editor_open:
            self._highlight_editor()

    def _configure_editor_syntax_tags(self) -> None:
        from project_agent.syntax import (
            ALL_TAGS,
            TAG_COMMENT,
            TAG_DECORATOR,
            TAG_HEADING,
            TAG_KEYWORD,
            TAG_LITERAL,
            TAG_MD_CODE,
            TAG_NUMBER,
            TAG_STRING,
        )

        box = getattr(self, "editor_box", None)
        if box is None:
            return
        colors = {
            TAG_KEYWORD: SYN_KEYWORD,
            TAG_STRING: SYN_STRING,
            TAG_COMMENT: SYN_COMMENT,
            TAG_NUMBER: SYN_NUMBER,
            TAG_DECORATOR: SYN_DECORATOR,
            TAG_HEADING: SYN_HEADING,
            TAG_MD_CODE: SYN_MD_CODE,
            TAG_LITERAL: SYN_LITERAL,
        }
        try:
            inner = box._textbox
            for tag in ALL_TAGS:
                tone = colors.get(tag, TEXT)
                inner.tag_configure(tag, foreground=_tone(tone))
            try:
                inner.tag_raise("sel")
            except tk.TclError:
                pass
        except tk.TclError:
            return

    def _schedule_editor_highlight(self) -> None:
        job = getattr(self, "_highlight_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        self._highlight_job = self.after(HIGHLIGHT_DEBOUNCE_MS, self._highlight_editor)

    def _highlight_editor(self) -> None:
        self._highlight_job = None
        from project_agent.syntax import ALL_TAGS, language_for_path, tokenize

        box = getattr(self, "editor_box", None)
        if box is None or not self.editor_open:
            return
        lang = language_for_path(self.editor_path)
        try:
            inner = box._textbox
            for tag in ALL_TAGS:
                inner.tag_remove(tag, "1.0", "end")
            if not lang:
                return
            text = self._editor_text()
            spans = tokenize(text, lang)
            for start, end, tag in spans:
                inner.tag_add(tag, f"1.0+{start}c", f"1.0+{end}c")
            try:
                inner.tag_raise("sel")
            except tk.TclError:
                pass
        except tk.TclError:
            return

    def _update_editor_gutter(self, _event=None) -> None:
        gutter = getattr(self, "editor_gutter", None)
        box = getattr(self, "editor_box", None)
        if gutter is None or box is None:
            return
        try:
            lines = self._editor_line_count()
            digits = max(3, len(str(lines)))
            content = "\n".join(f"{i:>{digits}}" for i in range(1, lines + 1))
            gutter.configure(state="normal", width=digits + 1)
            gutter.delete("1.0", "end")
            gutter.insert("1.0", content)
            gutter.configure(state="disabled")
            self._sync_editor_gutter()
        except tk.TclError:
            return

    def _sync_editor_gutter(self) -> None:
        gutter = getattr(self, "editor_gutter", None)
        box = getattr(self, "editor_box", None)
        if gutter is None or box is None:
            return
        try:
            first, _last = box._textbox.yview()
            gutter.yview_moveto(first)
        except tk.TclError:
            return

    def _on_editor_scroll(self, first, last) -> None:
        scrollbar = getattr(self.editor_box, "_scrollbar", None) or getattr(self.editor_box, "_y_scrollbar", None)
        if scrollbar is not None:
            try:
                scrollbar.set(first, last)
            except tk.TclError:
                pass
        self._sync_editor_gutter()

    def _editor_yview(self, *args) -> None:
        try:
            self.editor_box._textbox.yview(*args)
        except tk.TclError:
            return
        self._sync_editor_gutter()

    def _on_editor_change(self, _event=None) -> None:
        self.after_idle(self._update_editor_gutter)

    def _on_editor_modified(self, _event=None) -> None:
        try:
            if self.editor_box._textbox.edit_modified():
                self.editor_box._textbox.edit_modified(False)
                self._update_editor_gutter()
                self._schedule_editor_highlight()
                self._flush_editor_to_tab()
                self._refresh_editor_tab_labels()
        except tk.TclError:
            return

    def _on_gutter_wheel(self, event) -> str | None:
        delta = int(getattr(event, "delta", 0) or 0)
        steps = int(-delta / 120) if delta else 0
        if steps == 0 and delta:
            steps = -1 if delta > 0 else 1
        if steps:
            try:
                self.editor_box._textbox.yview_scroll(steps, "units")
            except tk.TclError:
                return "break"
            self._sync_editor_gutter()
        return "break"

    def _on_editor_control(self, event):
        if not (int(getattr(event, "state", 0) or 0) & 0x4):
            return
        key = str(getattr(event, "keysym", "") or "").lower()
        code = int(getattr(event, "keycode", 0) or 0)
        # S / Ы (save) — keycode устойчив к раскладке на Windows.
        if key in {"s", "ы"} or code == 83:
            self._save_editor(confirm=False)
            return "break"
        return _on_layout_clipboard(event)

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

    def _save_editor(self, confirm: bool = False) -> bool:
        if not self.editor_open or self.project is None:
            return False
        self._flush_editor_to_tab()
        return self._save_editor_tab(self.editor_tabs.active_index, confirm=confirm)

    def _save_editor_tab(self, index: int, confirm: bool = False) -> bool:
        if self.project is None or not (0 <= index < len(self.editor_tabs)):
            return False
        tab = self.editor_tabs.tabs[index]
        text = tab.text
        if index == self.editor_tabs.active_index:
            text = self._editor_text()
            tab.text = text
        if text == tab.saved:
            if index == self.editor_tabs.active_index:
                self.set_status("Изменений нет")
            return True
        if confirm and not self._ask(tab.path, "Записать файл?"):
            return False
        try:
            full = resolve_inside(self.project, tab.path)
            if full.exists() and full.is_dir():
                raise PathError("это папка, не файл")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text, encoding="utf-8", newline="\n")
        except (OSError, PathError) as exc:
            self.write_chat(f"Файл не записан: {exc}")
            return False
        self.editor_tabs.mark_saved_at(index, text)
        if index == self.editor_tabs.active_index:
            self.editor_saved = text
        self._refresh_editor_tab_labels()
        self._refresh_git_badge()
        self.set_status("Файл записан")
        return True

    def _ask_save_changes(self, path: str) -> bool | None:
        dialog = SaveChangesDialog(self, path)
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        return dialog.result

    def _close_editor_tab(self, index: int) -> None:
        if not self.editor_open or not (0 <= index < len(self.editor_tabs)):
            return
        self._flush_editor_to_tab()
        tab = self.editor_tabs.tabs[index]
        if tab.dirty:
            choice = self._ask_save_changes(tab.path)
            if choice is None:
                return
            if choice:
                if not self._save_editor_tab(index, confirm=False):
                    return
        was_active = index == self.editor_tabs.active_index
        nxt = self.editor_tabs.close_at(index)
        if nxt is None:
            self._hide_editor_panel()
            return
        if was_active:
            self._load_active_tab_into_editor()
        self._rebuild_editor_tab_bar()

    def _hide_editor_panel(self) -> None:
        job = getattr(self, "_highlight_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
            self._highlight_job = None
        try:
            self.work.forget(self.editor_frame)
        except tk.TclError:
            pass
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_tabs.close_all()
        self.editor_box.delete("1.0", "end")
        try:
            self.editor_gutter.configure(state="normal")
            self.editor_gutter.delete("1.0", "end")
            self.editor_gutter.configure(state="disabled")
        except tk.TclError:
            pass
        self._rebuild_editor_tab_bar()

    def _release_editor(self, ask: bool) -> bool:
        if not self.editor_open:
            return True
        self._flush_editor_to_tab()
        if ask and self.editor_tabs.any_dirty():
            if not self._ask("вкладки", "Закрыть несохранённые вкладки без записи?"):
                return False
        self._hide_editor_panel()
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

    def open_settings(self, section: str | None = None) -> None:
        window = self._settings_window
        if self._window_alive(window):
            try:
                if str(window.state()) == "withdrawn":
                    window.deiconify()
                if section:
                    window._show(section)
                window.lift()
                window.focus()
                return
            except tk.TclError:
                self._settings_window = None
        else:
            self._settings_window = None
        try:
            self._settings_window = SettingsWindow(self, section=section or "Модель")
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

    def _field(self, parent, text: str, *, top: int = 8) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w", text_color=MUTED).pack(fill="x", padx=8, pady=(top, 0))

    def _entry(
        self,
        parent,
        label: str,
        variable,
        placeholder: str,
        secret: bool = False,
        *,
        top: int = 8,
        gap: int = 4,
    ) -> None:
        self._field(parent, label, top=top)
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
        entry.pack(fill="x", padx=8, pady=gap)

    def _load(self) -> None:
        data, error = load_config()
        self._apply_theme(data["theme"])
        self.profiles = list(data["profiles"])
        self.active_profile = data["active_profile"]
        self._sync_profile_menu()
        self._apply_ai(data)
        self.test_preset = normalize_preset(data.get("test_preset"))
        self.test_preset_var.set(LABEL_BY_PRESET.get(self.test_preset, "Выключено"))
        self.test_timeout_var.set(str(normalize_timeout(data.get("test_timeout"))))
        self.test_fix_rounds_var.set(str(normalize_fix_rounds(data.get("test_fix_rounds"))))
        from project_agent.allowed import normalize_allowed_commands

        self.allowed_commands = normalize_allowed_commands(data.get("allowed_commands"))
        self.agent_shell_enabled = bool(data.get("agent_shell_enabled"))
        self.agent_shell_var.set(self.agent_shell_enabled)
        self.auto_write_project = bool(data.get("auto_write_project"))
        self.auto_write_var.set(self.auto_write_project)
        self.prefer_cheap_provider = bool(data.get("prefer_cheap_provider"))
        self.prefer_cheap_var.set(self.prefer_cheap_provider)
        self._apply_font(data.get("ui_font"), data.get("code_font"), rebuild_chat=False, reopen_settings=False)
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
        if hasattr(self, "mid_split"):
            self.mid_split.configure(bg=_tone(INK))
        if hasattr(self, "tree"):
            self._style_tree()
        if hasattr(self, "chat"):
            self._tag_chat()
            self._copy_photo_cache = None
        if hasattr(self, "placeholder"):
            self.placeholder.configure(fg=_tone(HINT), bg=_tone(FIELD), font=self._font(11))
        if hasattr(self, "git_label"):
            self._refresh_git_badge()
        if hasattr(self, "editor_box"):
            self._style_editor_gutter()
        if hasattr(self, "file_util_panel"):
            self._style_file_util_panel()
        if hasattr(self, "output_frame"):
            self.output_frame.configure(fg_color=PANEL)
            self.output_title.configure(text_color=MUTED)
            self.run_indicator.configure(text_color=MUTED)
            self.output_box.configure(fg_color=FIELD, text_color=TEXT, font=self._code_font(12))
            self.output_slot_menu.configure(
                fg_color=FIELD,
                button_color=FIELD,
                button_hover_color=BUTTON_HOVER,
                text_color=MUTED,
                dropdown_fg_color=PANEL,
                dropdown_text_color=TEXT,
                dropdown_hover_color=SELECT,
            )
        self._apply_font(rebuild_chat=False, reopen_settings=False)

    def _pick_font(self, which: str) -> None:
        current = self.ui_font if which == "ui_font" else self.code_font
        dialog = FontPickDialog(self, current=current)
        self.wait_window(dialog)
        name = str(dialog.result or "").strip()
        if not name:
            return
        _with_cyr, _without, cyr_set = _font_catalog(self)
        if name not in cyr_set:
            self.write_chat(f"Шрифт «{name}» без кириллицы — русский текст может отображаться некорректно.")
        if which == "ui_font":
            self._apply_font(ui=name, persist=True)
        else:
            self._apply_font(code=name, persist=True)

    def reset_fonts(self) -> None:
        if self.ui_font == DEFAULT_UI_FONT and self.code_font == DEFAULT_CODE_FONT:
            return
        self._apply_font(ui=DEFAULT_UI_FONT, code=DEFAULT_CODE_FONT, persist=True)

    def _apply_font(
        self,
        ui: str | None = None,
        code: str | None = None,
        *,
        persist: bool = False,
        rebuild_chat: bool = True,
        reopen_settings: bool = True,
    ) -> None:
        from project_agent.config import _font_family

        if ui is not None:
            self.ui_font = _font_family(ui, DEFAULT_UI_FONT)
        if code is not None:
            self.code_font = _font_family(code, DEFAULT_CODE_FONT)
        if self.ui_font_var.get() != self.ui_font:
            self.ui_font_var.set(self.ui_font)
        if self.code_font_var.get() != self.code_font:
            self.code_font_var.set(self.code_font)
        self.__dict__["_measure_fonts"] = {}
        self._copy_photo_cache = None
        if hasattr(self, "chat"):
            self.chat.configure(font=self._font(13))
            self._tag_chat()
        if hasattr(self, "task"):
            self.task.configure(font=self._font(13))
        if hasattr(self, "placeholder"):
            self.placeholder.configure(font=self._font(11))
        if hasattr(self, "editor_box"):
            self.editor_box.configure(font=self._editor_text_font())
            self._style_editor_gutter()
            self._update_editor_gutter()
        if hasattr(self, "file_util_panel"):
            self._style_file_util_panel()
        if hasattr(self, "output_box"):
            self.output_box.configure(font=self._code_font(12))
        if hasattr(self, "search_count"):
            self.search_count.configure(font=self._font(11))
        if hasattr(self, "status_label"):
            self.status_label.configure(font=self._font(11))
        if hasattr(self, "context_label"):
            self.context_label.configure(font=self._font(11))
        if self.allowed_box is not None:
            try:
                if self.allowed_box.winfo_exists():
                    self.allowed_box.configure(font=self._code_font(11))
            except tk.TclError:
                pass
        if self.mcp_list is not None:
            try:
                if self.mcp_list.winfo_exists():
                    self.mcp_list.configure(font=self._font(12))
            except tk.TclError:
                pass
        if hasattr(self, "tree"):
            self._style_tree()
            if self.project is not None:
                self._refresh_tree()
        if persist:
            try:
                if ui is not None:
                    save_font("ui_font", self.ui_font)
                if code is not None:
                    save_font("code_font", self.code_font)
            except Exception as exc:
                self.write_chat(f"Не удалось сохранить шрифт: {exc}")
        settings_open = self._window_alive(self._settings_window)
        if reopen_settings and settings_open:
            try:
                self._settings_window._close()
            except Exception:
                self._settings_window = None
            self.open_settings("Внешний вид")
        if rebuild_chat and hasattr(self, "transcript"):
            self._show_transcript()

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

    def _bind_profile_rename(self, widget) -> None:
        widget.bind("<Button-3>", self._profile_menu_context)
        for child in widget.winfo_children():
            self._bind_profile_rename(child)

    def _profile_menu_context(self, event) -> str | None:
        name = self.active_profile
        if not name or name == _NO_PROFILE:
            return "break"
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
        menu.add_command(label="Переименовать", command=self.rename_profile)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def rename_profile(self) -> None:
        old = self.active_profile
        if not old or old == _NO_PROFILE:
            return
        if self.running:
            return
        dialog = NameDialog(self, initial=old, title="Переименовать")
        self.wait_window(dialog)
        name = " ".join(str(dialog.result or "").split())
        if not name or name == old:
            return
        if len(name) > 80 or name == _NO_PROFILE:
            self.write_chat("Такое имя профиля не подходит.")
            return
        if any(item["name"] == name for item in self.profiles):
            self.write_chat("Профиль с таким именем уже есть.")
            return
        updated = []
        for item in self.profiles:
            if item["name"] == old:
                updated.append({**item, "name": name})
            else:
                updated.append(item)
        self.profiles = updated
        self.active_profile = name
        self._sync_profile_menu()
        self._persist("Профиль переименован")

    def delete_profile(self) -> None:
        name = self.active_profile
        if not name:
            self.write_chat("Профиль не выбран.")
            return
        dialog = ConfirmDialog(self, name, "Удалить этот профиль?")
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        if not dialog.result:
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
        settings["prompt_height"] = 0
        settings["test_preset"] = PRESET_LABELS.get(self.test_preset_var.get(), self.test_preset)
        settings["test_timeout"] = normalize_timeout(self.test_timeout_var.get())
        settings["test_fix_rounds"] = normalize_fix_rounds(self.test_fix_rounds_var.get())
        settings["allowed_commands"] = self._read_allowed_commands()
        settings["agent_shell_enabled"] = bool(self.agent_shell_var.get())
        self.agent_shell_enabled = settings["agent_shell_enabled"]
        settings["agent_mode"] = self.agent_mode
        settings["context_limit"] = self.context_limit
        settings["auto_write_project"] = bool(self.auto_write_var.get())
        self.auto_write_project = settings["auto_write_project"]
        settings["prefer_cheap_provider"] = bool(self.prefer_cheap_var.get())
        self.prefer_cheap_provider = settings["prefer_cheap_provider"]
        settings["ui_font"] = self.ui_font
        settings["code_font"] = self.code_font
        return settings

    def _read_allowed_commands(self) -> list[dict]:
        from project_agent.allowed import normalize_allowed_commands, parse_allowed_text

        box = self.allowed_box
        if box is None:
            return normalize_allowed_commands(self.allowed_commands)
        try:
            exists = bool(box.winfo_exists())
        except tk.TclError:
            exists = False
        if not exists:
            return normalize_allowed_commands(self.allowed_commands)
        parsed = parse_allowed_text(box.get("1.0", "end-1c"))
        self.allowed_commands = parsed
        return parsed

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
        self.chat_plan = empty_plan()
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
        self.chat_plan = empty_plan()
        self._forget_retry()
        self.checkpoints.clear()
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

    def _file_menu(self, event) -> str | None:
        row = self.tree.identify_row(event.y)
        if not row or self.project is None:
            return None
        tags = self.tree.item(row, "tags")
        if "file" not in tags and "dir" not in tags:
            return None
        self.tree.selection_set(row)
        relative = tree_path_rel(row)
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
            target = "." if relative == "." else relative
            menu.add_command(label="Вложить папку в запрос", command=lambda: self._add_context_path(target + "/"))
        else:
            menu.add_command(label="Вложить в запрос", command=lambda path=relative: self._add_context_path(path))
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
            self.chat_plan = empty_plan()
            self.checkpoints.clear()
        self.checkpoints.chat_id = self.chat_id or ""
        self.checkpoints.project_dir = str(self.project)
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
        self.set_status("Планирую следующие шаги…")
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
        self.set_status("Планирую следующие шаги…")
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
            if self._retry_payload:
                self._can_retry = True
                self._set_retry_enabled(True)
            else:
                self._can_retry = False
                self._set_retry_enabled(False)
            self.set_status("Остановлено")
        elif self._can_retry:
            self.set_status("Ошибка API")
            self._set_retry_enabled(True)
        else:
            self._set_retry_enabled(False)
            self.set_status("Готово")
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_chat_list()
        self._refresh_git_badge()

    def confirm(
        self,
        path: str,
        summary: str,
        detail: str = "",
        before: str | None = None,
        after: str | None = None,
    ) -> bool | str:
        if self.stop_event.is_set():
            return False
        done = threading.Event()
        holder: dict = {"result": False}

        def ask() -> None:
            try:
                dialog = ConfirmDialog(self, path, summary, detail, before=before, after=after)
                self._dialog = dialog
                self.wait_window(dialog)
                holder["result"] = dialog.result
            except Exception:
                holder["result"] = False
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
        if self.stop_event.is_set():
            return False
        return holder["result"]

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

    def _on_plan(self, steps: list[dict]) -> None:
        cleaned = clean_plan({"steps": steps})
        if not (cleaned.get("steps") or []):
            return
        self.chat_plan = cleaned
        mark = plan_mark()
        self.transcript = [line for line in self.transcript if not parse_plan_mark(line)]
        self.transcript.append(mark)
        self._store_chat()
        self.after(0, self._show_transcript)

    def _sync_plan_mark(self) -> None:
        has_steps = bool((self.chat_plan.get("steps") or []))
        kept: list[str] = []
        seen = False
        changed = False
        for line in self.transcript:
            if parse_plan_mark(line):
                if not has_steps or seen:
                    changed = True
                    continue
                seen = True
                kept.append(plan_mark())
                continue
            kept.append(line)
        if has_steps and not seen:
            kept.append(plan_mark())
            changed = True
        if changed or kept != self.transcript:
            self.transcript = kept

    def _on_checkpoint(self, checkpoint_id: str | None, file_count: int, dropped: list[str]) -> None:
        if dropped:
            self.after(0, lambda ids=list(dropped): self._drop_checkpoint_marks(ids))
        if not checkpoint_id:
            return
        mark = checkpoint_mark(checkpoint_id)
        self._remember(mark)
        count = max(0, int(file_count or 0))
        self.after(0, lambda cid=checkpoint_id, n=count: self._insert_checkpoint_control(cid, n))

    def _drop_checkpoint_marks(self, checkpoint_ids: list[str]) -> None:
        if not checkpoint_ids:
            return
        marks = {checkpoint_mark(item) for item in checkpoint_ids}
        before = len(self.transcript)
        self.transcript = [line for line in self.transcript if line.strip() not in marks]
        if len(self.transcript) != before:
            self._store_chat()
            self._show_transcript()

    def _insert_checkpoint_control(self, checkpoint_id: str, file_count: int = 0) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        if self.checkpoints.get(checkpoint_id) is None:
            return
        box.configure(state="normal")
        inner = box._textbox
        self._clear_running_tool()
        self._close_tool_group()
        self._embed_checkpoint_control(inner, checkpoint_id)
        self._chat_see_end()

    def _embed_plan_control(self, inner) -> None:
        steps = list((self.chat_plan.get("steps") or []))
        if not steps:
            return
        start = inner.index("end-1c")
        head = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
        tk.Label(
            head,
            text="План",
            bd=0,
            padx=4,
            pady=2,
            font=self._px_font(12),
            fg=_tone(TEXT),
            bg=_tone(INK),
        ).pack(side="left")
        done = sum(1 for step in steps if step.get("done"))
        tk.Label(
            head,
            text=f"  {done}/{len(steps)}",
            bd=0,
            padx=0,
            pady=2,
            font=self._px_font(10),
            fg=_tone(MUTED),
            bg=_tone(INK),
        ).pack(side="left")
        inner.window_create("end", window=head, padx=0, pady=2)
        inner.insert("end", "\n")
        for index, step in enumerate(steps):
            row = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
            mark = "✓" if step.get("done") else "○"
            toggle = tk.Button(
                row,
                text=mark,
                command=lambda i=index: self.toggle_plan_step(i),
                relief="flat",
                bd=0,
                padx=4,
                pady=1,
                cursor="hand2",
                bg=_tone(BUTTON),
                fg=_tone(TEXT),
                activebackground=_tone(BUTTON_HOVER),
                activeforeground=_tone(TEXT),
                disabledforeground=_tone(MUTED),
                font=self._px_font(11),
                width=2,
            )
            self._plan_buttons.append(toggle)
            toggle.pack(side="left")
            path = str(step.get("path") or "").strip()
            text = str(step.get("text") or "").strip()
            label = f"{path}: {text}" if path else text
            if step.get("done"):
                label = f"~~ {label}"
            tk.Label(
                row,
                text=label,
                bd=0,
                padx=6,
                pady=1,
                font=self._px_font(11),
                fg=_tone(MUTED) if step.get("done") else _tone(TEXT),
                bg=_tone(INK),
                anchor="w",
                justify="left",
            ).pack(side="left", fill="x", expand=True)
            inner.window_create("end", window=row, padx=4, pady=1)
            inner.insert("end", "\n")
        inner.tag_add("plan", start, inner.index("end-1c"))

    def toggle_plan_step(self, index: int) -> None:
        if self.running:
            return
        self.chat_plan = toggle_step(self.chat_plan, index)
        if not (self.chat_plan.get("steps") or []):
            self.transcript = [line for line in self.transcript if not parse_plan_mark(line)]
        else:
            self._sync_plan_mark()
        self._store_chat()
        self._show_transcript()

    def _embed_checkpoint_control(self, inner, checkpoint_id: str) -> None:
        item = self.checkpoints.get(checkpoint_id)
        if item is None:
            return
        start = inner.index("end-1c")
        paths = restorable_paths(item)
        head = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
        button = tk.Button(
            head,
            text="Откатить правки",
            command=lambda cid=checkpoint_id: self.restore_checkpoint(cid),
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
        self._checkpoint_buttons.append(button)
        button.pack(side="left")
        if paths:
            hint = tk.Label(
                head,
                text=f"  {len(paths)}",
                bd=0,
                padx=0,
                pady=2,
                font=self._px_font(10),
                fg=_tone(MUTED),
                bg=_tone(INK),
            )
            hint.pack(side="left")
            diff_btn = tk.Button(
                head,
                text="Diff хода",
                command=lambda cid=checkpoint_id: self.open_turn_diff(cid),
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
                font=self._px_font(11),
            )
            self._checkpoint_buttons.append(diff_btn)
            diff_btn.pack(side="left", padx=(8, 0))
        inner.window_create("end", window=head, padx=0, pady=2)
        inner.insert("end", "\n")
        for path in paths:
            row = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
            tk.Label(
                row,
                text=path,
                bd=0,
                padx=4,
                pady=1,
                font=self._px_font(11),
                fg=_tone(MUTED),
                bg=_tone(INK),
                anchor="w",
            ).pack(side="left")
            reject = tk.Button(
                row,
                text="Отклонить",
                command=lambda cid=checkpoint_id, rel=path: self.reject_checkpoint_file(cid, rel),
                relief="flat",
                bd=0,
                padx=6,
                pady=1,
                cursor="hand2",
                bg=_tone(BUTTON),
                fg=_tone(TEXT),
                activebackground=_tone(BUTTON_HOVER),
                activeforeground=_tone(TEXT),
                disabledforeground=_tone(MUTED),
                font=self._px_font(10),
            )
            self._checkpoint_buttons.append(reject)
            reject.pack(side="left", padx=(8, 0))
            inner.window_create("end", window=row, padx=8, pady=1)
            inner.insert("end", "\n")
        inner.tag_add("checkpoint", start, inner.index("end-1c"))

    def open_turn_diff(self, checkpoint_id: str) -> None:
        if self.project is None:
            return
        if self.checkpoints.get(checkpoint_id) is None:
            self.set_status("Снимок недоступен")
            return
        TurnDiffDialog(self, checkpoint_id)

    def reject_checkpoint_file(self, checkpoint_id: str, relative: str) -> None:
        if self.running or self.project is None:
            return
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None:
            self.set_status("Снимок недоступен")
            return
        ok, info = restore_one_file(self.project, checkpoint, relative)
        if not ok:
            self.set_status(info or "Не удалось отклонить файл")
            return
        still = self.checkpoints.drop_file(checkpoint_id, relative)
        if not still:
            mark = checkpoint_mark(checkpoint_id)
            self.transcript = [line for line in self.transcript if line.strip() != mark]
        self._show_transcript()
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_git_badge()
        if still:
            self.set_status(f"Отклонён: {relative}")
        else:
            self.set_status(f"Отклонён: {relative} (снимок закрыт)")

    def restore_checkpoint(self, checkpoint_id: str) -> None:
        if self.running or self.project is None:
            return
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None:
            self.set_status("Снимок недоступен")
            return
        restored, failed = restore_files(self.project, checkpoint)
        if not restored and failed:
            self.set_status("Не удалось откатить файлы")
            return
        cut = max(0, int(checkpoint.transcript_len))
        self.transcript = self.transcript[:cut]
        removed = self.checkpoints.drop_from(checkpoint_id)
        if removed:
            marks = {checkpoint_mark(item) for item in removed}
            self.transcript = [line for line in self.transcript if line.strip() not in marks]
        self.agent.truncate_messages(checkpoint.message_count)
        self._forget_retry()
        self._show_transcript()
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_git_badge()
        if failed:
            self.set_status(f"Правки откачены ({len(failed)} без снимка)")
        else:
            self.set_status("Правки откачены")

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
                plan=self.chat_plan,
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
        self.chat_plan = clean_plan(record.get("plan"))
        self._forget_retry()
        self.checkpoints.load(self.chat_id, str(self.project))
        self._sync_checkpoint_marks()
        self._sync_plan_mark()
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

    def _sync_checkpoint_marks(self) -> None:
        valid = {item.id for item in self.checkpoints.items}
        kept = []
        changed = False
        for line in self.transcript:
            checkpoint_id = parse_checkpoint_mark(line)
            if checkpoint_id is None:
                kept.append(line)
                continue
            item = self.checkpoints.get(checkpoint_id)
            if item is None or checkpoint_id not in valid:
                changed = True
                continue
            if item.transcript_len > len(kept):
                # маркер после обрезки истории — снимок уже не к месту
                self.checkpoints.drop_from(checkpoint_id)
                valid = {entry.id for entry in self.checkpoints.items}
                changed = True
                continue
            kept.append(line)
        if changed or kept != self.transcript:
            self.transcript = kept
            try:
                self.checkpoints.save()
            except Exception:
                pass

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
            delete_checkpoints(chat_id)
        except Exception as exc:
            self.write_chat(f"Чат не удалён: {exc}")
            return
        if chat_id == self.chat_id:
            self.chat_id = None
            self.chat_title = ""
            self.transcript.clear()
            self._forget_retry()
            self.checkpoints.clear()
            self._clear_box(self.chat)
            self.agent.reset_session(announce=False)
        self._refresh_chat_list()
        self._fit_labels()
        self.set_status("Чат удалён")

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
        wrap = max(100, inner_w - 4)
        frame = ctk.CTkFrame(
            self.chat._textbox,
            fg_color=INK,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        message = ctk.CTkLabel(
            frame,
            text=body,
            text_color=USER_TEXT,
            font=self._font(13),
            width=inner_w,
            wraplength=wrap,
            justify="left",
            anchor="w",
        )
        message.pack(fill="x", padx=12, pady=(10, 4 if copy_text else 10))
        chip = None
        if copy_text:
            chip = self._make_copy_chip(copy_text, INK, parent=frame)
            chip.pack(anchor="w", padx=10, pady=(0, 8))
        return {"frame": frame, "message": message, "chip": chip, "body": body}

    def _restyle_user_bubbles(self) -> None:
        bubble_w = self._user_bubble_width()
        inner_w = max(120, bubble_w - 24)
        wrap = max(100, inner_w - 4)
        for item in list(getattr(self, "_user_bubbles", [])):
            frame = item.get("frame")
            try:
                if frame is None or not frame.winfo_exists():
                    continue
                frame.configure(fg_color=INK, border_color=BORDER)
                item["message"].configure(text_color=USER_TEXT, width=inner_w, wraplength=wrap)
                chip = item.get("chip")
                if chip is not None and chip.winfo_exists():
                    chip.configure(bg=_tone(INK))
            except tk.TclError:
                continue

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
        if role == "plan":
            if not (self.chat_plan.get("steps") or []):
                return
            self._embed_plan_control(inner)
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
        self._plan_buttons = []
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
            self._plan_buttons = []
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
        self._stop_project_watch()
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
