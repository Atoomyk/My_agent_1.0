from __future__ import annotations

import re

HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BLOCK_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.S,
)


class PatchError(ValueError):
    pass


def apply_diff(original: str, diff: str) -> str:
    if "<<<<<<< SEARCH" in diff:
        return _apply_search_replace(original, diff)
    if "@@" in diff:
        return _apply_unified(original, diff)
    raise PatchError("неизвестный формат diff: нужен блок SEARCH/REPLACE или unified diff")


def _apply_search_replace(original: str, diff: str) -> str:
    try:
        return _blocks(original, diff)
    except PatchError as exc:
        if "не найден" not in str(exc) and "несколько" not in str(exc):
            raise
        return _blocks(original.replace("\r\n", "\n"), diff.replace("\r\n", "\n"))


def _blocks(original: str, diff: str) -> str:
    matches = list(BLOCK_RE.finditer(diff))
    if not matches:
        raise PatchError("блок SEARCH/REPLACE не распознан")
    updated = original
    for match in matches:
        old = match.group(1)
        new = match.group(2)
        if old == "":
            raise PatchError("пустой фрагмент SEARCH")
        found = updated.count(old)
        if found == 0:
            raise PatchError("фрагмент SEARCH не найден")
        if found > 1:
            raise PatchError("фрагмент SEARCH встречается несколько раз, добавь контекст")
        updated = updated.replace(old, new, 1)
    return updated


def _apply_unified(original: str, diff: str) -> str:
    normalized = original.replace("\r\n", "\n")
    trailing = normalized.endswith("\n")
    lines = normalized.splitlines()
    diff_lines = diff.replace("\r\n", "\n").splitlines()
    index = 0
    while index < len(diff_lines) and not diff_lines[index].startswith("@@"):
        index += 1
    if index >= len(diff_lines):
        raise PatchError("в diff нет хунков")
    while index < len(diff_lines):
        header = HUNK_RE.match(diff_lines[index])
        if not header:
            raise PatchError("некорректный хунк")
        old_start = int(header.group(1))
        index += 1
        hunk: list[tuple[str, str]] = []
        while index < len(diff_lines) and not diff_lines[index].startswith("@@"):
            line = diff_lines[index]
            index += 1
            if line.startswith("\\"):
                continue
            if line == "":
                hunk.append((" ", ""))
                continue
            kind, text = line[0], line[1:]
            if kind not in " +-":
                raise PatchError("строка хунка без префикса")
            hunk.append((kind, text))
        lines = _apply_hunk(lines, max(0, old_start - 1), hunk)
    text = "\n".join(lines)
    if trailing and lines:
        text += "\n"
    return text


def _apply_hunk(lines: list[str], hint: int, hunk: list[tuple[str, str]]) -> list[str]:
    pattern = [text for kind, text in hunk if kind in (" ", "-")]
    replacement = [text for kind, text in hunk if kind in (" ", "+")]
    if not pattern:
        index = min(max(hint, 0), len(lines))
        return lines[:index] + replacement + lines[index:]
    found = _find(lines, pattern, hint)
    if found is None:
        found = _find(lines, pattern, 0)
    if found is None:
        raise PatchError("хунк не совпал с файлом")
    return lines[:found] + replacement + lines[found + len(pattern) :]


def _find(lines: list[str], pattern: list[str], hint: int) -> int | None:
    starts = [hint] if 0 <= hint <= len(lines) else []
    starts.extend(range(len(lines)))
    seen: set[int] = set()
    for start in starts:
        if start in seen:
            continue
        seen.add(start)
        if lines[start : start + len(pattern)] == pattern:
            return start
    return None
