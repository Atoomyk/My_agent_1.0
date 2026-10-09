from __future__ import annotations

import os
import py_compile
import re
from dataclasses import dataclass
from pathlib import Path

from project_agent.paths import IGNORE_DIRS, normalize_relative, relative_posix

_FILE_LINE_RE = re.compile(
    r'File\s+"(?P<path>[^"]+)",\s+line\s+(?P<line>\d+)',
    re.IGNORECASE,
)
_COLON_LINE_RE = re.compile(
    r"^(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|pyw|pyi)):(?P<line>\d+)(?::\d+)?\s*:\s*(?P<msg>.+)$"
)
_COMPILE_RE = re.compile(
    r"\((?P<path>[^()]+?),\s*line\s+(?P<line>\d+)\)\s*$"
)


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    message: str
    source: str = "diag"

    def label(self, width: int = 120) -> str:
        msg = (self.message or "").strip().replace("\t", " ")
        if len(msg) > width:
            msg = msg[: width - 1] + "…"
        return f"{self.path}:{self.line}: {msg}"


def _rel_or_empty(root: Path | None, raw: str) -> str:
    text = (raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if root is None:
        return normalize_relative(text)
    try:
        full = Path(raw)
        if not full.is_absolute():
            full = (root / raw).resolve(strict=False)
        else:
            full = full.resolve(strict=False)
        return relative_posix(root, full)
    except (OSError, ValueError):
        name = Path(text).name
        return name


def parse_python_locations(text: str, root: Path | None = None) -> list[Diagnostic]:
    """Достаёт path:line из traceback / py_compile / mypy-подобных строк."""
    found: list[Diagnostic] = []
    seen: set[tuple[str, int, str]] = set()
    lines = (text or "").replace("\r\n", "\n").splitlines()
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        match = _FILE_LINE_RE.search(line)
        if match:
            path = _rel_or_empty(root, match.group("path"))
            lineno = int(match.group("line"))
            msg = ""
            if index + 1 < len(lines):
                nxt = lines[index + 1].strip()
                if nxt and not nxt.startswith("File "):
                    msg = nxt[:200]
            if not msg:
                msg = "traceback"
            key = (path, lineno, msg)
            if path and key not in seen:
                seen.add(key)
                found.append(Diagnostic(path=path, line=lineno, message=msg, source="traceback"))
            continue
        colon = _COLON_LINE_RE.match(line.strip())
        if colon:
            path = _rel_or_empty(root, colon.group("path"))
            lineno = int(colon.group("line"))
            msg = (colon.group("msg") or "").strip()[:200] or "error"
            key = (path, lineno, msg)
            if path and key not in seen:
                seen.add(key)
                found.append(Diagnostic(path=path, line=lineno, message=msg, source="line"))
            continue
        compile_hit = _COMPILE_RE.search(line)
        if compile_hit and ("Error" in line or "error" in line or "Sorry:" in line):
            path = _rel_or_empty(root, compile_hit.group("path"))
            lineno = int(compile_hit.group("line"))
            msg = line.strip()[:200]
            key = (path, lineno, msg)
            if path and key not in seen:
                seen.add(key)
                found.append(Diagnostic(path=path, line=lineno, message=msg, source="compile"))
    return found


def compile_project_python(root: Path, *, limit: int = 80) -> list[Diagnostic]:
    """py_compile всех .py в проекте (кроме IGNORE_DIRS)."""
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    limit = max(1, int(limit or 80))
    found: list[Diagnostic] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
        for filename in filenames:
            if len(found) >= limit:
                return found
            if not filename.endswith((".py", ".pyw")):
                continue
            full = Path(dirpath) / filename
            try:
                rel = relative_posix(root, full)
            except ValueError:
                continue
            try:
                py_compile.compile(str(full), doraise=True)
            except py_compile.PyCompileError as exc:
                path = _rel_or_empty(root, str(getattr(exc, "file", "") or rel)) or rel
                err = getattr(exc, "exc_value", None)
                lineno = int(getattr(err, "lineno", 0) or 0) or 0
                msg = str(getattr(exc, "msg", "") or exc).strip()
                if "\n" in msg:
                    msg = msg.splitlines()[-1].strip()
                if not lineno:
                    hit = _COMPILE_RE.search(msg) or _COMPILE_RE.search(str(exc))
                    if hit:
                        lineno = int(hit.group("line"))
                        if not path or path == rel:
                            path = _rel_or_empty(root, hit.group("path")) or path or rel
                lineno = lineno or 1
                found.append(
                    Diagnostic(path=path, line=lineno, message=(msg or "syntax error")[:200], source="compile")
                )
            except OSError as exc:
                found.append(
                    Diagnostic(path=rel, line=1, message=f"не прочитан: {exc}", source="compile")
                )
    return found
