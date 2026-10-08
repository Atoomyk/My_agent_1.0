from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from project_agent.config import config_dir
from project_agent.paths import IGNORE_DIRS, is_binary_name
from project_agent.symbols import scan_python_file

MAX_INDEX_FILES = 8_000
MAX_FIND = 80
INDEX_VERSION = 1
SYMBOLS_VERSION = 1


def index_root() -> Path:
    return config_dir() / "index"


def project_key(root: Path) -> str:
    text = os.path.normcase(str(root.resolve(strict=False)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def index_path(root: Path) -> Path:
    return index_root() / project_key(root) / "files.json"


def symbols_path(root: Path) -> Path:
    return index_root() / project_key(root) / "symbols.json"


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


def empty_symbols(root: Path) -> dict:
    return {
        "version": SYMBOLS_VERSION,
        "root": str(root.resolve(strict=False)),
        "built_at": 0,
        "files": {},
        "truncated": False,
    }


def load_symbols(root: Path) -> dict:
    path = symbols_path(root)
    if not path.exists():
        return empty_symbols(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_symbols(root)
    if not isinstance(data, dict) or data.get("version") != SYMBOLS_VERSION:
        return empty_symbols(root)
    files = data.get("files")
    if not isinstance(files, dict):
        data["files"] = {}
    return data


def save_symbols(root: Path, data: dict) -> Path:
    path = symbols_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SYMBOLS_VERSION,
        "root": str(root.resolve(strict=False)),
        "built_at": int(data.get("built_at") or time.time()),
        "files": dict(data.get("files") or {}),
        "truncated": bool(data.get("truncated")),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    return path


def _scan_entry(root: Path, rel: str) -> dict | None:
    full = root / rel
    scanned = scan_python_file(full)
    if scanned is None:
        return None
    symbols, imports = scanned
    try:
        mtime = int(full.stat().st_mtime)
    except OSError:
        mtime = 0
    return {"mtime": mtime, "symbols": symbols, "imports": imports}


def build_symbols(root: Path, file_index: dict | None = None) -> dict:
    root = root.resolve()
    if file_index is not None:
        data = file_index
    else:
        data = load_index(root)
        if not data.get("built_at"):
            data = build_index(root, with_symbols=False)
    files: dict[str, dict] = {}
    truncated = False
    for item in data.get("files") or []:
        rel = str(item.get("path") or "").replace("\\", "/")
        if not rel.endswith(".py") or item.get("binary"):
            continue
        if len(files) >= MAX_INDEX_FILES:
            truncated = True
            break
        entry = _scan_entry(root, rel)
        if entry is not None:
            files[rel] = entry
    payload = {
        "version": SYMBOLS_VERSION,
        "root": str(root),
        "built_at": int(time.time()),
        "files": files,
        "truncated": truncated or bool(data.get("truncated")),
    }
    save_symbols(root, payload)
    return payload


def ensure_symbols(root: Path) -> dict:
    data = load_symbols(root)
    if data.get("built_at"):
        return data
    return build_symbols(root)


def touch_symbols(root: Path, relative: str) -> None:
    data = load_symbols(root)
    if not data.get("built_at"):
        return
    rel = relative.replace("\\", "/").lstrip("./")
    if not rel.endswith(".py"):
        if rel in data.get("files", {}):
            data["files"].pop(rel, None)
            save_symbols(root, data)
        return
    entry = _scan_entry(root, rel)
    if entry is None:
        data.get("files", {}).pop(rel, None)
    else:
        files = data.setdefault("files", {})
        files[rel] = entry
    save_symbols(root, data)


def build_index(root: Path, *, with_symbols: bool = True) -> dict:
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
    if with_symbols:
        build_symbols(root, data)
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
        touch_symbols(root, rel)
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
    touch_symbols(root, rel)


def find_symbols(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    query = (query or "").strip().lower()
    data = ensure_symbols(root)
    if not query:
        return [], data
    found: list[str] = []
    for path, entry in sorted((data.get("files") or {}).items()):
        for symbol in entry.get("symbols") or []:
            name = str(symbol.get("name") or "")
            qual = str(symbol.get("qualname") or name)
            kind = str(symbol.get("kind") or "")
            line = int(symbol.get("line") or 0)
            hay = f"{qual} {name}".lower()
            if query not in hay:
                continue
            found.append(f"{qual} ({kind}) {path}:{line}")
            if len(found) >= limit:
                return found, data
    return found, data


def list_imports(root: Path, relative: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    rel = (relative or "").replace("\\", "/").lstrip("./")
    data = ensure_symbols(root)
    entry = (data.get("files") or {}).get(rel)
    if entry is None:
        return [], data
    lines: list[str] = []
    for item in entry.get("imports") or []:
        module = str(item.get("module") or "")
        names = item.get("names") or []
        line = int(item.get("line") or 0)
        if names:
            lines.append(f"L{line}: from {module} import {', '.join(str(n) for n in names)}")
        else:
            lines.append(f"L{line}: import {module}")
        if len(lines) >= limit:
            break
    return lines, data


def find_importers(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    query = (query or "").strip().lower().replace("\\", "/")
    data = ensure_symbols(root)
    if not query:
        return [], data
    found: list[str] = []
    for path, entry in sorted((data.get("files") or {}).items()):
        for item in entry.get("imports") or []:
            module = str(item.get("module") or "")
            names = [str(n) for n in (item.get("names") or [])]
            line = int(item.get("line") or 0)
            hay = " ".join([module, *names]).lower()
            if query not in hay and query not in module.lower():
                continue
            if names:
                found.append(f"{path}:{line} from {module} import {', '.join(names)}")
            else:
                found.append(f"{path}:{line} import {module}")
            if len(found) >= limit:
                return found, data
    return found, data


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
    symbols = load_symbols(root)
    if symbols.get("built_at"):
        py_count = len(symbols.get("files") or {})
        sym_count = sum(len(item.get("symbols") or []) for item in (symbols.get("files") or {}).values())
        return f"Индекс: {count} файлов, {py_count} .py / {sym_count} символов, обновлён {built}{note}."
    return f"Индекс: {count} файлов, обновлён {built}{note}."
