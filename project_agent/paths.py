from __future__ import annotations

import os
from pathlib import Path

IGNORE_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "dist", "build"})
MAX_FILE_BYTES = 1_000_000

BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
        ".zip", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
        ".pyc", ".pyo", ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4",
        ".mov", ".avi", ".wasm", ".sqlite", ".db", ".bin", ".dat", ".class",
        ".jar", ".pkl", ".parquet", ".xlsx", ".docx", ".pptx", ".p12", ".pfx",
    }
)
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


class PathError(ValueError):
    pass


def resolve_inside(root: Path, raw: str) -> Path:
    if raw is None:
        raw = "."
    text = str(raw).strip() or "."
    if "\x00" in text:
        raise PathError("некорректный путь")
    candidate = Path(text)
    if not candidate.is_absolute() and ":" in text:
        raise PathError("некорректный путь")
    root = root.resolve()
    full = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
    try:
        relative = full.relative_to(root)
    except ValueError as exc:
        raise PathError("путь вне проекта") from exc
    if any(part in IGNORE_DIRS for part in relative.parts):
        raise PathError("служебный каталог недоступен")
    walk = root
    for part in relative.parts:
        walk = walk / part
        if walk.is_symlink():
            target = walk.resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise PathError("ссылка ведёт за пределы проекта") from exc
    return full


def relative_posix(root: Path, full: Path) -> str:
    return full.resolve(strict=False).relative_to(root.resolve()).as_posix()


def is_binary_name(path: Path) -> bool:
    return path.suffix.lower() in BINARY_EXTENSIONS


def is_image_name(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def read_text_file(root: Path, raw: str) -> str:
    full = resolve_inside(root, raw)
    if not full.is_file():
        raise PathError("файл не найден")
    if is_binary_name(full):
        raise PathError("файл не текстовый")
    try:
        size = full.stat().st_size
        data = full.read_bytes()
    except OSError as exc:
        raise PathError("файл не прочитан") from exc
    if size > MAX_FILE_BYTES:
        raise PathError("файл больше 1 МБ")
    if b"\x00" in data[:8192]:
        raise PathError("файл не текстовый")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PathError("файл не в UTF-8") from exc


def list_entries(root: Path, relative: str = ".") -> tuple[list[tuple[str, bool]], bool]:
    parent = (relative or ".").replace("\\", "/").strip("/") or "."
    full = resolve_inside(root, parent)
    if not full.exists() or not full.is_dir():
        raise PathError("папка не найдена")
    found: list[tuple[str, bool]] = []
    try:
        scan = os.scandir(full)
    except OSError as exc:
        raise PathError("папка не прочитана") from exc
    with scan:
        for entry in scan:
            if entry.name in IGNORE_DIRS or entry.name in {".", ".."}:
                continue
            child = entry.name if parent == "." else f"{parent}/{entry.name}"
            try:
                resolved = resolve_inside(root, child)
            except PathError:
                continue
            is_dir = resolved.is_dir() if entry.is_symlink() else entry.is_dir(follow_symlinks=False)
            found.append((entry.name, is_dir))
    found.sort(key=lambda item: (not item[1], item[0].casefold()))
    truncated = len(found) > 500
    return found[:500], truncated
