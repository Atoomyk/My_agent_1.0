from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from project_agent.config import config_dir
from project_agent.paths import IGNORE_DIRS, is_binary_name

MAX_INDEX_FILES = 8_000
MAX_FIND = 80
INDEX_VERSION = 1


def index_root() -> Path:
    return config_dir() / "index"


def project_key(root: Path) -> str:
    text = os.path.normcase(str(root.resolve(strict=False)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def index_path(root: Path) -> Path:
    return index_root() / project_key(root) / "files.json"


def empty_index(root: Path) -> dict:
    return {
        "version": INDEX_VERSION,
        "root": str(root.resolve(strict=False)),
        "built_at": 0,
        "files": [],
        "truncated": False,
    }


def load_index(root: Path) -> dict:
    path = index_path(root)
    if not path.exists():
        return empty_index(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_index(root)
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return empty_index(root)
    files = data.get("files")
    if not isinstance(files, list):
        data["files"] = []
    return data


def save_index(root: Path, data: dict) -> Path:
    path = index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "root": str(root.resolve(strict=False)),
        "built_at": int(data.get("built_at") or time.time()),
        "files": list(data.get("files") or []),
        "truncated": bool(data.get("truncated")),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    return path


def build_index(root: Path) -> dict:
    root = root.resolve()
    files: list[dict] = []
    truncated = False
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORE_DIRS and name not in {".", ".."})
        base = Path(current)
        try:
            relative_dir = base.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative_dir != "." and any(part in IGNORE_DIRS for part in Path(relative_dir).parts):
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if len(files) >= MAX_INDEX_FILES:
                truncated = True
                break
            full = base / name
            if full.is_symlink():
                continue
            rel = name if relative_dir == "." else f"{relative_dir}/{name}"
            try:
                stat = full.stat()
            except OSError:
                continue
            files.append(
                {
                    "path": rel.replace("\\", "/"),
                    "size": int(stat.st_size),
                    "mtime": int(stat.st_mtime),
                    "binary": bool(is_binary_name(full)),
                }
            )
        if truncated:
            break
    data = {
        "version": INDEX_VERSION,
        "root": str(root),
        "built_at": int(time.time()),
        "files": files,
        "truncated": truncated,
    }
    save_index(root, data)
    return data


def touch_file(root: Path, relative: str, size: int | None = None) -> None:
    data = load_index(root)
    if not data.get("files") and not data.get("built_at"):
        return
    rel = relative.replace("\\", "/").lstrip("./")
    full = root / rel
    binary = is_binary_name(full)
    try:
        stat = full.stat()
        size = int(stat.st_size) if size is None else int(size)
        mtime = int(stat.st_mtime)
    except OSError:
        data["files"] = [item for item in data["files"] if item.get("path") != rel]
        save_index(root, data)
        return
    entry = {"path": rel, "size": size, "mtime": mtime, "binary": binary}
    updated = False
    for index, item in enumerate(data["files"]):
        if item.get("path") == rel:
            data["files"][index] = entry
            updated = True
            break
    if not updated:
        if len(data["files"]) < MAX_INDEX_FILES:
            data["files"].append(entry)
        else:
            data["truncated"] = True
    save_index(root, data)


def find_paths(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    query = (query or "").strip().lower().replace("\\", "/")
    data = load_index(root)
    if not data.get("built_at"):
        data = build_index(root)
    if not query:
        paths = [str(item.get("path") or "") for item in data["files"][:limit]]
        return [path for path in paths if path], data
    found: list[str] = []
    for item in data["files"]:
        path = str(item.get("path") or "")
        if query in path.lower():
            found.append(path)
            if len(found) >= limit:
                break
    return found, data


def index_summary(root: Path) -> str:
    data = load_index(root)
    if not data.get("built_at"):
        return "Индекс ещё не построен."
    count = len(data.get("files") or [])
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(data["built_at"])))
    note = ", список обрезан" if data.get("truncated") else ""
    return f"Индекс: {count} файлов, обновлён {built}{note}."
