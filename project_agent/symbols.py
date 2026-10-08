from __future__ import annotations

import re
from pathlib import Path

from project_agent.paths import MAX_FILE_BYTES

_CLASS = re.compile(r"^([ \t]*)class[ \t]+([A-Za-z_][\w]*)")
_DEF = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+([A-Za-z_][\w]*)")
_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+(\S+)[ \t]+import[ \t]+(.+?)\s*$")
_IMPORT = re.compile(r"^[ \t]*import[ \t]+(.+?)\s*$")
_NAME = re.compile(r"^([A-Za-z_][\w]*)")


def _indent_width(prefix: str) -> int:
    return len(prefix.expandtabs(4))


def _split_import_names(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    names: list[str] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece or piece == "*":
            if piece == "*":
                names.append("*")
            continue
        piece = piece.split(" as ", 1)[0].strip()
        match = _NAME.match(piece)
        if match:
            names.append(match.group(1))
    return names


def parse_python_file(text: str) -> tuple[list[dict], list[dict]]:
    """Топ-уровень class/def + методы класса; import / from import."""
    symbols: list[dict] = []
    imports: list[dict] = []
    class_name: str | None = None
    class_indent = -1
    method_indent = -1
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        class_match = _CLASS.match(line)
        if class_match:
            indent = _indent_width(class_match.group(1))
            name = class_match.group(2)
            if indent == 0:
                class_name = name
                class_indent = 0
                method_indent = -1
                symbols.append({"name": name, "qualname": name, "kind": "class", "line": number})
            elif class_name is not None and indent <= class_indent:
                class_name = None
                class_indent = -1
                method_indent = -1
            continue
        def_match = _DEF.match(line)
        if def_match:
            indent = _indent_width(def_match.group(1))
            name = def_match.group(2)
            if indent == 0:
                class_name = None
                class_indent = -1
                method_indent = -1
                symbols.append({"name": name, "qualname": name, "kind": "def", "line": number})
            elif class_name is not None and indent > class_indent:
                if method_indent < 0:
                    method_indent = indent
                if indent == method_indent:
                    qual = f"{class_name}.{name}"
                    symbols.append({"name": name, "qualname": qual, "kind": "method", "line": number})
            continue
        stripped = line.lstrip()
        indent = _indent_width(line[: len(line) - len(stripped)])
        if class_name is not None and indent == 0 and not stripped.startswith("@"):
            class_name = None
            class_indent = -1
            method_indent = -1
        if indent != 0:
            continue
        from_match = _FROM_IMPORT.match(line)
        if from_match:
            module = from_match.group(1).strip()
            names = _split_import_names(from_match.group(2))
            imports.append({"module": module, "names": names or ["*"], "line": number})
            continue
        import_match = _IMPORT.match(line)
        if import_match:
            for part in import_match.group(1).split(","):
                piece = part.strip().split(" as ", 1)[0].strip()
                if piece:
                    imports.append({"module": piece, "names": [], "line": number})
    return symbols, imports


def read_python_source(full: Path) -> str | None:
    try:
        if not full.is_file() or full.suffix.lower() != ".py":
            return None
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        data = full.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def scan_python_file(full: Path) -> tuple[list[dict], list[dict]] | None:
    text = read_python_source(full)
    if text is None:
        return None
    return parse_python_file(text)
