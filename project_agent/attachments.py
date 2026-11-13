from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from project_agent.config import config_dir
from project_agent.paths import IGNORE_DIRS

MAX_TEXT_BYTES = 1_000_000
MAX_IMAGE_BYTES = 10_000_000
MAX_TEXT_FILES = 8
MAX_IMAGES = 4
TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    ".html", ".htm", ".css", ".scss", ".sql", ".sh", ".bat", ".ps1",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs", ".rb",
    ".php", ".swift", ".kt", ".kts", ".vue", ".svelte", ".csv", ".log",
    ".env.example", ".gitignore", ".dockerfile",
}
SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
SENSITIVE_SUFFIXES = {".p12", ".pfx", ".key"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def attachment_dir(chat_id: str | None) -> Path:
    key = chat_id or "draft"
    if not key.isalnum():
        raise ValueError("Некорректный идентификатор хранилища")
    return config_dir() / "attachments" / key


def is_sensitive(path: Path) -> bool:
    name = path.name.casefold()
    return name in SENSITIVE_NAMES or path.suffix.casefold() in SENSITIVE_SUFFIXES or (
        "private key" in name or name.endswith(".pem")
    )


def enumerate_supported(source: Path) -> tuple[list[Path], list[str]]:
    """Получить поддерживаемые файлы из файла/дерева, пропуская служебное и секретное."""
    candidates = [source] if source.is_file() else []
    skipped: list[str] = []
    if source.is_dir():
        for folder, dirs, files in __import__("os").walk(source):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for name in files:
                candidates.append(Path(folder) / name)
    result = []
    for path in candidates:
        if is_sensitive(path):
            skipped.append(f"{path.name}: файл похож на секретный")
        elif path.suffix.casefold() in IMAGE_SUFFIXES or path.suffix.casefold() in TEXT_EXTENSIONS:
            result.append(path)
        else:
            skipped.append(f"{path.name}: неподдерживаемый формат")
    return result, skipped


def copy_attachment(source: Path, chat_id: str | None, relative_name: str = "") -> dict:
    """Проверить и сохранить отдельную копию; вернуть только безопасные метаданные."""
    image = source.suffix.casefold() in IMAGE_SUFFIXES
    limit = MAX_IMAGE_BYTES if image else MAX_TEXT_BYTES
    size = source.stat().st_size
    if size > limit:
        raise ValueError(f"{source.name}: размер превышает {limit // 1_000_000} МБ")
    if not image:
        raw = source.read_bytes()
        if b"\x00" in raw[:8192]:
            raise ValueError(f"{source.name}: файл не текстовый")
    folder = attachment_dir(chat_id)
    folder.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    suffix = source.suffix.lower()
    stored = folder / f"{token}{suffix}"
    shutil.copyfile(source, stored)
    return {
        "id": token,
        "name": source.name,
        "relative_name": relative_name.replace("\\", "/") or source.name,
        "size": size,
        "image": image,
        "stored": str(stored),
        "source": str(source),
    }


def remove_attachment(item: dict) -> None:
    stored = Path(str(item.get("stored") or ""))
    try:
        stored.unlink(missing_ok=True)
    except OSError:
        pass


def delete_chat_attachments(chat_id: str) -> None:
    folder = attachment_dir(chat_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def load_text(item: dict, encoding: str = "utf-8-sig") -> str:
    path = Path(str(item["stored"]))
    raw = path.read_bytes()
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError(f"{item.get('name', 'Файл')}: больше 1 МБ")
    return raw.decode(encoding)
