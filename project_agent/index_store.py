from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from project_agent.config import config_dir
from project_agent.paths import IGNORE_DIRS, PathError, is_binary_name, normalize_relative, resolve_inside
from project_agent.symbols import (
    JS_EXTENSIONS,
    is_go_path,
    is_js_path,
    is_ps_path,
    is_rust_path,
    is_symbol_path,
    path_module_aliases,
    resolve_js_relative,
    scan_source_file,
    read_source_text,
)

MAX_INDEX_FILES = 8_000
MAX_FIND = 80
INDEX_VERSION = 1
SYMBOLS_VERSION = 4


@dataclass(frozen=True)
class SymbolHit:
    path: str
    line: int
    text: str

    def label(self, width: int = 120) -> str:
        snippet = (self.text or "").strip().replace("\t", " ")
        if len(snippet) > width:
            snippet = snippet[: width - 1] + "…"
        return f"{self.path}:{self.line}: {snippet}"


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
    scanned = scan_source_file(full)
    if scanned is None:
        return None
    symbols, imports = scanned
    try:
        stat = full.stat()
        mtime = int(stat.st_mtime)
        mtime_ns = int(stat.st_mtime_ns)
        size = int(stat.st_size)
    except OSError:
        mtime = 0
        mtime_ns = 0
        size = 0
    return {"mtime": mtime, "mtime_ns": mtime_ns, "size": size, "symbols": symbols, "imports": imports}


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
        if not is_symbol_path(rel) or item.get("binary"):
            continue
        if len(files) >= MAX_INDEX_FILES:
            truncated = True
            break
        entry = _scan_entry(root, rel)
        if entry is not None:
            files[rel] = entry
    _resolve_project_imports(root, files)
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
    rel = normalize_relative(relative)
    if not is_symbol_path(rel):
        if rel in data.get("files", {}):
            data["files"].pop(rel, None)
            _resolve_project_imports(root, data.get("files", {}))
            save_symbols(root, data)
        return
    entry = _scan_entry(root, rel)
    if entry is None:
        data.get("files", {}).pop(rel, None)
    else:
        files = data.setdefault("files", {})
        files[rel] = entry
    _resolve_project_imports(root, data.get("files", {}))
    save_symbols(root, data)


def _resolve_js_targets(source: str, module: str, known: set[str]) -> list[str]:
    if not module.startswith((".", "..")):
        return []
    candidate = resolve_js_relative(source, module)
    if not candidate:
        return []
    mod_clean = module.strip()
    explicit_ext = next((ext for ext in JS_EXTENSIONS if mod_clean.lower().endswith(ext)), "")
    candidates: list[str] = []
    if explicit_ext:
        candidates.append(f"{candidate}{explicit_ext}")
    candidates.extend(f"{candidate}{ext}" for ext in JS_EXTENSIONS if ext != explicit_ext)
    candidates.extend(f"{candidate}/index{ext}" for ext in JS_EXTENSIONS)
    hit = next((item for item in candidates if item in known), "")
    return [hit] if hit else []


def _resolve_python_targets(source: str, module: str, names: list[str], known: set[str]) -> list[str]:
    package = Path(source).parent.as_posix()
    base_parts = [part for part in package.split("/") if part and part != "."]
    resolved: list[str] = []

    if module.startswith("."):
        level = len(module) - len(module.lstrip("."))
        suffix = module[level:]
        remove = max(0, level - 1)
        if remove > len(base_parts):
            return []
        pkg_parts = base_parts[:-remove] if remove else list(base_parts)
        mod_parts = pkg_parts + [p for p in suffix.split(".") if p] if suffix else list(pkg_parts)
        mod_path = "/".join(mod_parts)
        if suffix:
            # `from .sub import a, b` -> submodule `sub.a` if present, otherwise `sub.py` / `sub/__init__.py`
            sub_hits: list[str] = []
            for name in names:
                if name == "*":
                    continue
                child = f"{mod_path}/{name}" if mod_path else name
                hit = next((c for c in (f"{child}.py", f"{child}/__init__.py") if c in known), "")
                if hit:
                    sub_hits.append(hit)
            if sub_hits:
                resolved.extend(sub_hits)
            else:
                hit = next((c for c in (f"{mod_path}.py", f"{mod_path}/__init__.py") if c in known), "")
                if hit:
                    resolved.append(hit)
        else:
            # `from . import a, b` or `from .. import a, b`
            base = "/".join(pkg_parts)
            for name in names:
                if name == "*":
                    continue
                child = f"{base}/{name}" if base else name
                hit = next((c for c in (f"{child}.py", f"{child}/__init__.py") if c in known), "")
                if hit:
                    resolved.append(hit)
            if not resolved:
                init_path = f"{base}/__init__.py" if base else "__init__.py"
                if init_path in known:
                    resolved.append(init_path)
    else:
        mod_parts = [p for p in module.split(".") if p] if module else []
        mod_path = "/".join(mod_parts)
        if not mod_path:
            return []
        sub_hits = []
        for name in names:
            if name == "*":
                continue
            child = f"{mod_path}/{name}"
            hit = next((c for c in (f"{child}.py", f"{child}/__init__.py") if c in known), "")
            if hit:
                sub_hits.append(hit)
        if sub_hits:
            resolved.extend(sub_hits)
        else:
            hit = next((c for c in (f"{mod_path}.py", f"{mod_path}/__init__.py") if c in known), "")
            if hit:
                resolved.append(hit)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in resolved:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _resolve_project_imports(root: Path, files: dict[str, dict]) -> None:
    """Annotate imports with confidently resolved in-project source paths."""
    known = set(files)
    for source, entry in files.items():
        dependencies: set[str] = set()
        for imported in entry.get("imports") or []:
            module = str(imported.get("module") or "")
            names = [str(name) for name in imported.get("names") or []]
            targets: list[str] = []
            if is_js_path(source):
                targets = _resolve_js_targets(source, module, known)
            elif source.lower().endswith(".py"):
                targets = _resolve_python_targets(source, module, names, known)
            if targets:
                imported["target"] = targets[0]
                if len(targets) > 1:
                    imported["targets"] = targets
                else:
                    imported.pop("targets", None)
                dependencies.update(targets)
            else:
                imported.pop("target", None)
                imported.pop("targets", None)
        entry["dependencies"] = sorted(dependencies)


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
                    "mtime_ns": int(stat.st_mtime_ns),
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
    rel = normalize_relative(relative)
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
    entry = {"path": rel, "size": size, "mtime": mtime, "mtime_ns": int(stat.st_mtime_ns), "binary": binary}
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


def _importer_needles(query: str) -> set[str]:
    text = (query or "").strip().lower().replace("\\", "/")
    if not text:
        return set()
    needles = set(path_module_aliases(text))
    needles.add(text)
    dotted = text.replace("/", ".")
    needles.add(dotted)
    if dotted.endswith(".__init__"):
        needles.add(dotted[: -len(".__init__")])
    return {item for item in needles if item}


def _import_haystack(path: str, module: str, names: list[str]) -> str:
    parts = [module, *names]
    if is_js_path(path):
        resolved = resolve_js_relative(path, module)
        if resolved:
            parts.append(resolved)
            parts.extend(path_module_aliases(resolved))
    parts.extend(path_module_aliases(module.replace(".", "/") + ".py"))
    parts.append(module.replace(".", "/"))
    return " ".join(str(part).lower() for part in parts if part)


def search_symbols(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[SymbolHit], dict]:
    query = (query or "").strip().lower()
    data = ensure_symbols(root)
    if not query:
        return [], data
    found: list[SymbolHit] = []
    for path, entry in sorted((data.get("files") or {}).items()):
        for symbol in entry.get("symbols") or []:
            name = str(symbol.get("name") or "")
            qual = str(symbol.get("qualname") or name)
            kind = str(symbol.get("kind") or "")
            line = int(symbol.get("line") or 0)
            lang = str(symbol.get("lang") or "")
            hay = f"{qual} {name}".lower()
            if query not in hay:
                continue
            suffix = f" [{lang}]" if lang else ""
            found.append(SymbolHit(path=path, line=line or 1, text=f"{qual} ({kind}){suffix}"))
            if len(found) >= limit:
                return found, data
    return found, data

def find_symbols(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    hits, data = search_symbols(root, query, limit=limit)
    return [f"{hit.text} {hit.path}:{hit.line}" for hit in hits], data


def find_symbol_definitions(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    """Exact lookup by symbol name or qualified name."""
    needle = (query or "").strip().casefold()
    data = ensure_symbols(root)
    found: list[str] = []
    if not needle:
        return found, data
    for path, entry in sorted((data.get("files") or {}).items()):
        for symbol in entry.get("symbols") or []:
            raw_name = str(symbol.get("name") or "")
            raw_qualname = str(symbol.get("qualname") or raw_name)
            if needle not in {raw_name.casefold(), raw_qualname.casefold()}:
                continue
            line = int(symbol.get("line") or 1)
            kind = str(symbol.get("kind") or "symbol")
            found.append(f"{path}:{line}: {raw_qualname} ({kind})")
            if len(found) >= max(1, limit):
                return found, data
    return found, data


def search_symbol_references(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    """Heuristic identifier search; it does not resolve lexical scopes."""
    needle = (query or "").strip()
    data = ensure_symbols(root)
    if not needle:
        return [], data
    import re

    pattern = re.compile(rf"(?<![\w$]){re.escape(needle)}(?![\w$])")
    found: list[str] = []
    for path in sorted((data.get("files") or {})):
        try:
            full = resolve_inside(root, path)
        except PathError:
            continue
        text = read_source_text(full)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                snippet = line.strip()
                if len(snippet) > 180:
                    snippet = snippet[:179] + "…"
                found.append(f"{path}:{line_no}: {snippet}")
                if len(found) >= max(1, limit):
                    return found, data
    return found, data


def import_graph(root: Path, relative: str, *, depth: int = 2, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    rel = normalize_relative(relative or "")
    data = ensure_symbols(root)
    files = data.get("files") or {}
    if rel not in files:
        return [], data
    depth = max(1, min(int(depth), 5))
    visited = {rel}
    frontier = [(rel, 0)]
    lines: list[str] = []
    while frontier and len(lines) < max(1, limit):
        current, level = frontier.pop(0)
        if level >= depth:
            continue
        for target in sorted((files.get(current) or {}).get("dependencies") or []):
            lines.append(f"{'  ' * level}{current} -> {target}")
            if target not in visited:
                visited.add(target)
                frontier.append((target, level + 1))
            if len(lines) >= max(1, limit):
                break
    return lines, data


def list_dependencies(
    root: Path, relative: str, *, reverse: bool = False, limit: int = MAX_FIND
) -> tuple[list[str], dict]:
    rel = normalize_relative(relative or "")
    data = ensure_symbols(root)
    files = data.get("files") or {}
    if rel not in files:
        return [], data
    if reverse:
        paths = [source for source, entry in sorted(files.items()) if rel in (entry.get("dependencies") or [])]
    else:
        paths = sorted(files[rel].get("dependencies") or [])
    return paths[:max(1, limit)], data


def list_imports(root: Path, relative: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    rel = normalize_relative(relative or "")
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


def search_importers(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[SymbolHit], dict]:
    needles = _importer_needles(query)
    data = ensure_symbols(root)
    if not needles:
        return [], data
    found: list[SymbolHit] = []
    for path, entry in sorted((data.get("files") or {}).items()):
        for item in entry.get("imports") or []:
            module = str(item.get("module") or "")
            names = [str(n) for n in (item.get("names") or [])]
            line = int(item.get("line") or 0)
            hay = _import_haystack(path, module, names)
            if not any(needle in hay for needle in needles):
                continue
            if names:
                text = f"from {module} import {', '.join(names)}"
            else:
                text = f"import {module}"
            found.append(SymbolHit(path=path, line=line or 1, text=text))
            if len(found) >= limit:
                return found, data
    return found, data


def find_importers(root: Path, query: str, limit: int = MAX_FIND) -> tuple[list[str], dict]:
    hits, data = search_importers(root, query, limit=limit)
    return [f"{hit.path}:{hit.line} {hit.text}" for hit in hits], data


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


def _entry_stale(root: Path, rel: str, item: dict) -> bool:
    try:
        full = resolve_inside(root, rel)
        stat = full.stat()
    except (OSError, PathError):
        return True
    if int(stat.st_size) != int(item.get("size") or 0):
        return True
    stored_ns = item.get("mtime_ns")
    if stored_ns is None:
        # Старые записи файлового индекса хранят только mtime в секундах.
        return int(stat.st_mtime) != int(item.get("mtime") or 0)
    return int(stat.st_mtime_ns) != int(stored_ns)


def _has_unindexed_files(root: Path, indexed_paths: set[str]) -> bool:
    resolved_root = root.resolve()
    for current, dirnames, filenames in os.walk(resolved_root):
        dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS and name not in {".", ".."}]
        base = Path(current)
        try:
            relative_dir = base.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if relative_dir != "." and any(part in IGNORE_DIRS for part in Path(relative_dir).parts):
            dirnames[:] = []
            continue
        for name in filenames:
            full = base / name
            if full.is_symlink():
                continue
            rel = name if relative_dir == "." else f"{relative_dir}/{name}"
            if rel not in indexed_paths:
                return True
    return False


def index_summary(root: Path) -> str:
    data = load_index(root)
    if not data.get("built_at"):
        return "Индекс ещё не построен."
    entries = data.get("files") or []
    count = len(entries)
    built = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(data["built_at"])))
    note = ", список обрезан" if data.get("truncated") else ""
    stale = 0
    indexed_paths: set[str] = set()
    for item in entries:
        rel = str(item.get("path") or "")
        indexed_paths.add(rel)
        if _entry_stale(root, rel, item):
            stale += 1
    freshness = f", устарело записей: {stale}" if stale else ""
    has_new = not data.get("truncated") and _has_unindexed_files(root, indexed_paths)
    new_note = ", есть новые файлы" if has_new else ""
    symbols = load_symbols(root)
    symbol_freshness = ", символьный индекс не построен" if not symbols.get("built_at") else ""
    if symbols.get("built_at"):
        symbol_files = symbols.get("files") or {}
        expected = {
            str(item.get("path") or "")
            for item in entries
            if is_symbol_path(str(item.get("path") or "")) and not item.get("binary")
        }
        if has_new or set(symbol_files) != expected:
            symbol_freshness = ", символьный индекс устарел"
        else:
            for path, symbol_entry in symbol_files.items():
                if _entry_stale(root, path, symbol_entry):
                    symbol_freshness = ", символьный индекс устарел"
                    break
        if symbols.get("truncated") and not data.get("truncated"):
            note = ", символьный индекс обрезан"
    if symbols.get("built_at"):
        sym_files = symbols.get("files") or {}
        py_count = sum(1 for path in sym_files if str(path).lower().endswith(".py"))
        js_count = sum(1 for path in sym_files if is_js_path(str(path)))
        go_count = sum(1 for path in sym_files if is_go_path(str(path)))
        ps_count = sum(1 for path in sym_files if is_ps_path(str(path)))
        rs_count = sum(1 for path in sym_files if is_rust_path(str(path)))
        sym_count = sum(len(item.get("symbols") or []) for item in sym_files.values())
        parts = [f"{py_count} .py"]
        if js_count:
            parts.append(f"{js_count} JS/TS")
        if go_count:
            parts.append(f"{go_count} Go")
        if ps_count:
            parts.append(f"{ps_count} PS")
        if rs_count:
            parts.append(f"{rs_count} Rust")
        langs = ", ".join(parts)
        return f"Индекс: {count} файлов, {langs} / {sym_count} символов, обновлён {built}{note}{freshness}{new_note}{symbol_freshness}."
    return f"Индекс: {count} файлов, обновлён {built}{note}{freshness}{new_note}{symbol_freshness}."
