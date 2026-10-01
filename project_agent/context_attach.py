from __future__ import annotations

import re
from pathlib import Path

from project_agent.paths import (
    MAX_FILE_BYTES,
    PathError,
    is_binary_name,
    is_image_name,
    relative_posix,
    resolve_inside,
)
from project_agent.secrets import is_secret_blob

MAX_CONTEXT_FILES = 8
AT_TOKEN_RE = re.compile(
    r'(?<!\S)@(?:"([^"\n]+)"|\'([^\'\n]+)\'|([^\s@]+))'
)
AT_TAIL_RE = re.compile(
    r'(?<!\S)@(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s@]*))$'
)


def parse_at_paths(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in AT_TOKEN_RE.finditer(text or ""):
        raw = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        raw = raw.replace("\\", "/").strip("/")
        if not raw or raw in seen:
            continue
        seen.add(raw)
        found.append(raw)
    return found


def at_token_at_end(text_before_cursor: str) -> tuple[str, int] | None:
    match = AT_TAIL_RE.search(text_before_cursor or "")
    if not match:
        return None
    query = (match.group(1) or match.group(2) or match.group(3) or "").replace("\\", "/")
    return query, match.start()


def merge_paths(*groups: list[str], limit: int = MAX_CONTEXT_FILES) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            path = str(raw or "").replace("\\", "/").strip().strip("/")
            if not path or path in seen:
                continue
            seen.add(path)
            merged.append(path)
            if len(merged) >= limit:
                return merged
    return merged


def load_context_files(root: Path, paths: list[str], vault) -> tuple[str, list[str], list[str]]:
    """Читает явные файлы для запроса. Возвращает блок текста, успешные пути и ошибки."""
    blocks: list[str] = []
    loaded: list[str] = []
    errors: list[str] = []
    for raw in merge_paths(paths):
        try:
            full = resolve_inside(root, raw)
        except PathError as exc:
            errors.append(f"{raw}: {exc}")
            continue
        rel = relative_posix(root, full)
        if not full.exists() or not full.is_file():
            errors.append(f"{rel}: файл не найден")
            continue
        if is_image_name(full):
            errors.append(f"{rel}: изображение — приложите через «+» → Изображение")
            continue
        if is_binary_name(full):
            errors.append(f"{rel}: бинарный файл")
            continue
        try:
            size = full.stat().st_size
            data = full.read_bytes()
        except OSError:
            errors.append(f"{rel}: не прочитан")
            continue
        if size > MAX_FILE_BYTES:
            errors.append(f"{rel}: больше 1 МБ")
            continue
        if b"\x00" in data[:8192]:
            errors.append(f"{rel}: бинарный файл")
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"{rel}: не UTF-8")
            continue
        if is_secret_blob(full) or ("-----BEGIN" in text and "PRIVATE KEY-----" in text):
            token = vault.take(text)
            body = f"Содержимое скрыто ({token})."
        else:
            body = vault.redact(text, full)
        blocks.append(f"### {rel}\n{body}")
        loaded.append(rel)
    if not blocks:
        return "", loaded, errors
    joined = "\n\n".join(blocks)
    return f"Приложенные файлы (явный контекст):\n\n{joined}", loaded, errors


def compose_user_text(text: str, context_block: str) -> str:
    base = (text or "").strip()
    block = (context_block or "").strip()
    if not block:
        return base
    if not base:
        return block
    return f"{base}\n\n---\n{block}"
