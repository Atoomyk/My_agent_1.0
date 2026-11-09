"""Строки diff и метрики изменения — чистые данные, без Tk.

Модуль обслуживает диалог подтверждения записи (``ConfirmDialog``): строит
построчный diff ``before → after`` с номерами строк и состоянием «принят /
отклонён» для каждого фрагмента, а также считает метрики для шапки окна
(тип изменения, ``+N −M``, число фрагментов, размеры в байтах).

Импортировать ``project_agent.app`` и любой GUI отсюда ЗАПРЕЩЕНО: модуль
проверяется обычным ``unittest`` в ``tests/test_diff_view.py``.

Нумерация фрагментов совпадает с ``project_agent.hunks.split_hunks`` — это
важно, потому что итоговый текст собирается через ``hunks.merge_hunks``.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

KIND_NEW = "новый файл"
KIND_MODIFIED = "изменён"
KIND_CLEARED = "очищен"
KIND_EMPTY = "без изменений"
CONTEXT_LINES = 2
SIZE_UNIT = 1024
NUMBER_WIDTH = 4


def normalize_text(text: str | None) -> str:
    """Привести переводы строк к LF: иначе CRLF-файл выглядит как переписанный целиком."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def plural(count: int, one: str, few: str, many: str) -> str:
    number = abs(int(count))
    if number % 100 in (11, 12, 13, 14):
        return many
    tail = number % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def format_fragments(count: int) -> str:
    number = max(0, int(count))
    return f"{number} {plural(number, 'фрагмент', 'фрагмента', 'фрагментов')}"


def format_fragments_of(count: int) -> str:
    """«из 2 фрагментов» — после «из» всегда родительный падеж множественного."""
    number = max(0, int(count))
    return f"{number} {plural(number, 'фрагмента', 'фрагментов', 'фрагментов')}"


def format_size(size: int) -> str:
    """Байты в человекочитаемый вид: ``381 Б``, ``4.2 КБ``, ``1.5 МБ``."""
    value = max(0, int(size))
    if value < SIZE_UNIT:
        return f"{value} Б"
    if value < SIZE_UNIT * SIZE_UNIT:
        return f"{value / SIZE_UNIT:.1f} КБ"
    return f"{value / (SIZE_UNIT * SIZE_UNIT):.1f} МБ"


def text_bytes(text: str | None) -> int:
    return len(normalize_text(text).encode("utf-8"))


def change_kind(before: str, after: str) -> str:
    head = normalize_text(before)
    tail = normalize_text(after)
    if head == tail:
        return KIND_EMPTY
    if head == "":
        return KIND_NEW
    if tail == "":
        return KIND_CLEARED
    return KIND_MODIFIED


def _hunk_header(old_start: int, old_count: int, new_start: int, new_count: int) -> str:
    old = f"-{old_start + 1}" + (f",{old_count}" if old_count != 1 else "")
    new = f"+{new_start + 1}" + (f",{new_count}" if new_count != 1 else "")
    return f"@@ {old} {new} @@"


@dataclass(frozen=True)
class DiffRow:
    """Одна строка отображения. ``kind``: ctx | add | del | hunk | skip."""

    kind: str
    old_no: int | None
    new_no: int | None
    text: str
    hunk_index: int | None = None
    accepted: bool = True


@dataclass(frozen=True)
class ChangeStats:
    kind: str
    plus: int
    minus: int
    hunks: int
    accepted_hunks: int
    selected_plus: int
    selected_minus: int
    before_bytes: int
    after_bytes: int

    @property
    def partial(self) -> bool:
        return 0 < self.accepted_hunks < self.hunks

    @property
    def nothing(self) -> bool:
        return self.hunks > 0 and self.accepted_hunks == 0


def change_stats(before: str, after: str, accepted: set[int] | None = None) -> ChangeStats:
    """Метрики предложения; ``accepted=None`` — принято всё."""
    from project_agent.hunks import split_hunks

    head = normalize_text(before)
    tail = normalize_text(after)
    hunks = split_hunks(head, tail)
    chosen = {item.index for item in hunks} if accepted is None else set(accepted)
    plus = sum(item.new_count for item in hunks)
    minus = sum(item.old_count for item in hunks)
    selected_plus = sum(item.new_count for item in hunks if item.index in chosen)
    selected_minus = sum(item.old_count for item in hunks if item.index in chosen)
    return ChangeStats(
        kind=change_kind(head, tail),
        plus=plus,
        minus=minus,
        hunks=len(hunks),
        accepted_hunks=len([item for item in hunks if item.index in chosen]),
        selected_plus=selected_plus,
        selected_minus=selected_minus,
        before_bytes=text_bytes(head),
        after_bytes=text_bytes(tail),
    )


def format_stats(stats: ChangeStats) -> str:
    """Строка «итог предложения»: ``изменён · +12 −5 · 3 фрагмента · 4.2 КБ → 4.6 КБ (+381 Б)``."""
    if stats.hunks == 0:
        return KIND_EMPTY
    parts = [stats.kind, f"+{stats.plus} −{stats.minus}", format_fragments(stats.hunks)]
    parts.append(format_sizes(stats))
    return " · ".join(parts)


def format_selection(stats: ChangeStats) -> str:
    """Строка «текущий выбор»; пустая, когда выбрано всё (или нечего выбирать)."""
    if stats.hunks == 0 or stats.accepted_hunks == stats.hunks:
        return ""
    if stats.accepted_hunks == 0:
        return "выбрано: ничего"
    fragments = f"{stats.accepted_hunks} из {format_fragments_of(stats.hunks)}"
    return f"выбрано: +{stats.selected_plus} −{stats.selected_minus} · {fragments}"


def _context_spans(start: int, end: int, context: int) -> list[tuple[int, int] | None]:
    """Какие строки блока ``equal`` показать: (голова, хвост), между ними None-разрыв."""
    total = end - start
    if total <= 0:
        return []
    if total <= context * 2:
        return [(start, end)]
    return [(start, start + context), None, (end - context, end)]


def format_sizes(stats: ChangeStats) -> str:
    """Размеры: ``4.2 КБ → 4.6 КБ (+381 Б)``; дельта опускается, если её нет."""
    delta = stats.after_bytes - stats.before_bytes
    sizes = f"{format_size(stats.before_bytes)} → {format_size(stats.after_bytes)}"
    if delta:
        sizes += f" ({'+' if delta > 0 else '−'}{format_size(abs(delta))})"
    return sizes


def build_rows(
    before: str,
    after: str,
    *,
    accepted: set[int] | None = None,
    context: int = CONTEXT_LINES,
    limit: int | None = None,
) -> list[DiffRow]:
    """Построчный diff с номерами строк и состоянием фрагментов."""
    head = normalize_text(before)
    tail = normalize_text(after)
    before_lines = head.splitlines()
    after_lines = tail.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    opcodes = matcher.get_opcodes()

    states: list[bool] = []
    for tag, _i1, _i2, _j1, _j2 in opcodes:
        if tag == "equal":
            continue
        states.append(accepted is None or len(states) in accepted)

    rows: list[DiffRow] = []
    hunk_index = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for span in _context_spans(i1, i2, max(0, context)):
                if span is None:
                    rows.append(DiffRow("skip", None, None, "…"))
                    continue
                for index in range(span[0], span[1]):
                    rows.append(
                        DiffRow("ctx", index + 1, j1 + (index - i1) + 1, before_lines[index])
                    )
            continue
        ok = states[hunk_index]
        rows.append(
            DiffRow(
                "hunk",
                None,
                None,
                _hunk_header(i1, i2 - i1, j1, j2 - j1),
                hunk_index=hunk_index,
                accepted=ok,
            )
        )
        for index in range(i1, i2):
            rows.append(DiffRow("del", index + 1, None, before_lines[index], hunk_index, ok))
        for index in range(j1, j2):
            rows.append(DiffRow("add", None, index + 1, after_lines[index], hunk_index, ok))
        hunk_index += 1
    if limit is not None and limit > 0 and len(rows) > limit:
        rows = rows[:limit] + [DiffRow("skip", None, None, "…")]
    return rows


def row_line(row: DiffRow, number_width: int = NUMBER_WIDTH) -> str:
    """Готовая строка для текстового поля: номера, знак, текст."""
    old = f"{row.old_no:>{number_width}}" if row.old_no else " " * number_width
    new = f"{row.new_no:>{number_width}}" if row.new_no else " " * number_width
    if row.kind == "hunk":
        mark = "✓ принят" if row.accepted else "✗ отклонён"
        return f"{row.text}  {mark}"
    if row.kind == "skip":
        return "…"
    sign = {"add": "+", "del": "-"}.get(row.kind, " ")
    return f"{old} {new} {sign} {row.text}"
