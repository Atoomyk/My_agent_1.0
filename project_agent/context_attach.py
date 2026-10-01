from __future__ import annotations

import re
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageGrab

from project_agent.index_store import find_paths, load_index
from project_agent.paths import (
    MAX_FILE_BYTES,
    PathError,
    is_binary_name,
    is_image_name,
    list_entries,
    relative_posix,
    resolve_inside,
)
from project_agent.secrets import is_secret_blob

MAX_CONTEXT_FILES = 8
MAX_CONTEXT_DIRS = 4
MAX_TREE_ENTRIES = 200
MAX_TREE_DEPTH = 3
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


def split_files_and_dirs(root: Path, paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    files: list[str] = []
    dirs: list[str] = []
    errors: list[str] = []
    seen_f: set[str] = set()
    seen_d: set[str] = set()
    for raw in merge_paths(paths, limit=MAX_CONTEXT_FILES + MAX_CONTEXT_DIRS):
        try:
            full = resolve_inside(root, raw)
        except PathError as exc:
            errors.append(f"{raw}: {exc}")
            continue
        rel = relative_posix(root, full)
        if full.is_dir():
            if rel in seen_d or len(dirs) >= MAX_CONTEXT_DIRS:
                continue
            seen_d.add(rel)
            dirs.append(rel)
        elif full.is_file():
            if rel in seen_f or len(files) >= MAX_CONTEXT_FILES:
                continue
            seen_f.add(rel)
            files.append(rel)
        else:
            errors.append(f"{rel}: не найден")
    return files, dirs, errors


def build_dir_listing(root: Path, relative: str, max_entries: int = MAX_TREE_ENTRIES, max_depth: int = MAX_TREE_DEPTH) -> str:
    lines: list[str] = []
    count = 0
    relative = (relative or ".").replace("\\", "/").strip("/") or "."

    def walk(rel: str, depth: int, indent: str) -> None:
        nonlocal count
        if count >= max_entries:
            return
        try:
            entries, truncated = list_entries(root, rel)
        except PathError:
            lines.append(f"{indent}(не прочитано)")
            count += 1
            return
        for name, is_dir in entries:
            if count >= max_entries:
                lines.append(f"{indent}…")
                return
            child = name if rel == "." else f"{rel}/{name}"
            count += 1
            if is_dir:
                lines.append(f"{indent}{name}/")
                if depth < max_depth:
                    walk(child, depth + 1, indent + "  ")
            else:
                lines.append(f"{indent}{name}")
        if truncated:
            lines.append(f"{indent}…")
            count += 1

    walk(relative, 1, "")
    return "\n".join(lines) if lines else "(пусто)"


def load_context_dirs(root: Path, paths: list[str]) -> tuple[str, list[str], list[str]]:
    blocks: list[str] = []
    loaded: list[str] = []
    errors: list[str] = []
    for raw in merge_paths(paths, limit=MAX_CONTEXT_DIRS):
        try:
            full = resolve_inside(root, raw)
        except PathError as exc:
            errors.append(f"{raw}: {exc}")
            continue
        rel = relative_posix(root, full)
        if not full.exists() or not full.is_dir():
            errors.append(f"{rel}: папка не найдена")
            continue
        listing = build_dir_listing(root, rel)
        blocks.append(f"### {rel}/\n{listing}")
        loaded.append(rel + "/")
    if not blocks:
        return "", loaded, errors
    joined = "\n\n".join(blocks)
    return f"Приложенные папки (только дерево путей):\n\n{joined}", loaded, errors


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
        if full.is_dir():
            errors.append(f"{rel}: это папка — вложите как папку")
            continue
        if not full.exists() or not full.is_file():
            errors.append(f"{rel}: файл не найден")
            continue
        if is_image_name(full):
            errors.append(f"{rel}: изображение — приложите через «+» или Ctrl+V")
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


def load_explicit_context(root: Path, paths: list[str], vault) -> tuple[str, list[str], list[str], list[str]]:
    files, dirs, split_errors = split_files_and_dirs(root, paths)
    file_block, loaded_files, file_errors = load_context_files(root, files, vault)
    dir_block, loaded_dirs, dir_errors = load_context_dirs(root, dirs)
    parts = [part for part in (file_block, dir_block) if part]
    block = "\n\n".join(parts)
    errors = [*split_errors, *file_errors, *dir_errors]
    return block, loaded_files, loaded_dirs, errors


def compose_user_text(text: str, context_block: str) -> str:
    base = (text or "").strip()
    block = (context_block or "").strip()
    if not block:
        return base
    if not base:
        return block
    return f"{base}\n\n---\n{block}"


def find_at_targets(root: Path, query: str, limit: int = 8) -> list[str]:
    """Файлы и папки для подсказки @."""
    query = (query or "").strip().lower().replace("\\", "/")
    dirs: set[str] = set()
    files: list[str] = []
    try:
        data = load_index(root)
        if not data.get("built_at"):
            from project_agent.index_store import build_index

            data = build_index(root)
        for item in data.get("files") or []:
            path = str(item.get("path") or "").replace("\\", "/")
            if not path:
                continue
            parts = path.split("/")
            for index in range(len(parts) - 1):
                dirs.add("/".join(parts[: index + 1]))
            if not query or query in path.lower():
                files.append(path)
    except Exception:
        try:
            files, _meta = find_paths(root, query, limit=limit)
        except Exception:
            files = []
    dir_hits: list[str] = []
    for folder in sorted(dirs, key=str.casefold):
        last = folder.split("/")[-1].lower()
        if query and query not in folder.lower() and not last.startswith(query):
            continue
        dir_hits.append(folder + "/")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in [*dir_hits, *files]:
        key = item.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
        if len(ordered) >= limit:
            break
    return ordered


def save_clipboard_image() -> Path | None:
    """Сохраняет изображение из буфера Windows во временный PNG. Иначе None."""
    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception:
        return None
    image = None
    if grabbed is None:
        return None
    if isinstance(grabbed, list):
        for item in grabbed:
            path = Path(str(item))
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                try:
                    with Image.open(path) as opened:
                        image = opened.convert("RGB")
                    break
                except OSError:
                    continue
        if image is None:
            return None
    elif isinstance(grabbed, Image.Image):
        image = grabbed.convert("RGB") if grabbed.mode != "RGB" else grabbed.copy()
    else:
        return None
    folder = Path(tempfile.gettempdir()) / "ProjectAgent"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"clipboard_{uuid.uuid4().hex}.png"
    try:
        image.save(target, format="PNG")
    except OSError:
        return None
    return target
