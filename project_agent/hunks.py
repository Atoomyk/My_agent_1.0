from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from project_agent.gitops import preview_unified


@dataclass(frozen=True)
class DiffHunk:
    index: int
    kind: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]

    def header(self) -> str:
        old_span = self.old_count if self.old_count != 1 else None
        new_span = self.new_count if self.new_count != 1 else None
        old = f"-{self.old_start + 1}" + (f",{old_span}" if old_span is not None else "")
        new = f"+{self.new_start + 1}" + (f",{new_span}" if new_span is not None else "")
        return f"@@ {old} {new} @@"

    def label(self, width: int = 72) -> str:
        sample = ""
        for line in self.new_lines[:1] or self.old_lines[:1]:
            sample = (line or "").strip()
            break
        if len(sample) > width - 20:
            sample = sample[: width - 21] + "…"
        mark = {"replace": "~", "delete": "−", "insert": "+"}.get(self.kind, "?")
        base = f"H{self.index + 1} {mark} {self.header()}"
        return f"{base}  {sample}".rstrip() if sample else base


def split_hunks(before: str, after: str) -> list[DiffHunk]:
    """Разбить before→after на хунки (opcodes SequenceMatcher без equal)."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    hunks: list[DiffHunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(
            DiffHunk(
                index=len(hunks),
                kind=tag,
                old_start=i1,
                old_count=i2 - i1,
                new_start=j1,
                new_count=j2 - j1,
                old_lines=tuple(before_lines[i1:i2]),
                new_lines=tuple(after_lines[j1:j2]),
            )
        )
    return hunks


def merge_hunks(before: str, after: str, accepted: set[int] | None = None) -> str:
    """Собрать текст: принятые хунки из after, отклонённые — из before."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    trailing = after.endswith("\n") if after else before.endswith("\n")
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    out: list[str] = []
    hunk_index = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(before_lines[i1:i2])
            continue
        take_new = accepted is None or hunk_index in accepted
        if take_new:
            out.extend(after_lines[j1:j2])
        else:
            out.extend(before_lines[i1:i2])
        hunk_index += 1
    if not out:
        return "\n" if trailing else ""
    body = "\n".join(out)
    return body + ("\n" if trailing else "")


def parse_unified_hunks(diff_text: str) -> list[tuple[str, list[str]]]:
    """Грубый разбор unified-текста на (header, body_lines) — для тестов/превью."""
    import re

    header_re = re.compile(r"^@@ .+ @@")
    chunks: list[tuple[str, list[str]]] = []
    header = ""
    body: list[str] = []
    for raw in (diff_text or "").splitlines():
        if header_re.match(raw):
            if header:
                chunks.append((header, body))
            header = raw
            body = []
            continue
        if raw.startswith("---") or raw.startswith("+++"):
            continue
        if header:
            body.append(raw)
    if header:
        chunks.append((header, body))
    return chunks


def format_hunk_unified(hunk: DiffHunk) -> str:
    lines = [hunk.header()]
    for line in hunk.old_lines:
        lines.append("-" + line)
    for line in hunk.new_lines:
        lines.append("+" + line)
    return "\n".join(lines)


def checkpoint_file_texts(root: Path, relative: str, snap: dict) -> tuple[str, str, str]:
    """(before, after, note). note пуст если ок; иначе причина пропуска."""
    from project_agent.paths import PathError, resolve_inside

    path = str(relative or "").replace("\\", "/").strip().strip("/")
    if not path:
        return "", "", "пустой путь"
    if snap.get("skipped"):
        return "", "", "нет снимка"
    before = ""
    if snap.get("existed"):
        content = snap.get("content")
        if content is None:
            return "", "", "нет снимка"
        before = str(content)
    try:
        full = resolve_inside(Path(root).resolve(), path)
    except PathError:
        return before, "", "путь вне проекта"
    after = ""
    if full.exists() and full.is_file():
        try:
            after = full.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return before, "", "не прочитать файл"
    elif not snap.get("existed"):
        return before, after, "файл ещё не создан или уже удалён"
    return before, after, ""


def build_turn_diff(root: Path, files: dict[str, dict], *, limit_per_file: int | None = 400) -> str:
    """Сводный unified diff хода: снимок → текущее содержимое."""
    parts: list[str] = []
    for relative in sorted((files or {}).keys()):
        snap = files.get(relative) or {}
        before, after, note = checkpoint_file_texts(root, relative, snap)
        if note and before == after:
            parts.append(f"# {relative}: {note}")
            continue
        if before == after:
            parts.append(f"# {relative}: без текстовых отличий")
            continue
        block = preview_unified(before, after, relative, limit=limit_per_file)
        parts.append(block)
    return "\n\n".join(parts) if parts else "(нет изменений)"
