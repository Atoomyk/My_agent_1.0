"""Разметка чата: чистые функции над текстом сообщений.

Здесь живёт вся «текстовая» логика ленты без единого виджета: отделение
хвостовой метки времени, определение роли сообщения (``user``/``agent``/
``tool``/``error``/``checkpoint``), снятие префикса «Вы:», отбрасывание
пометок вложений при копировании, разбор фрагментов markdown-подобной
разметки (``code``/``bold``/``codeblock``) и заголовки свёрнутых групп
инструментов.

Перенесено из ``project_agent.app`` дословно, без изменения поведения.
Модуль **не импортирует** ``project_agent.app`` (иначе получился бы цикл);
извне нужен только ``parse_checkpoint_mark`` из ``project_agent.checkpoints``.

Константы ``_FENCE``, ``_INLINE``, ``_USER_ATTACH_LINE``, ``_CHAT_STAMP``,
``_RUNNING_TAIL`` и ``_GROUP_NAMES`` переехали сюда вместе с функциями,
которые их используют.
"""

from __future__ import annotations

import re
from datetime import datetime

from project_agent.checkpoints import parse_checkpoint_mark

_FENCE = re.compile(r"^\s*```")
_INLINE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
_USER_ATTACH_LINE = re.compile(r"^\((?:папки|файлы): .+\)$|^\(изображений: \d+\)$")
_CHAT_STAMP = re.compile(r"\n(\d{2}:\d{2})\s*$")
_RUNNING_TAIL = "выполняется…"
_GROUP_NAMES = 4


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
