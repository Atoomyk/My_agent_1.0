from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from project_agent.paths import (
    BINARY_EXTENSIONS,
    IGNORE_DIRS,
    MAX_FILE_BYTES,
    is_binary_name,
    relative_posix,
)

DEFAULT_LIMIT = 80
DEFAULT_SCAN = 2000


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    text: str

    def label(self, width: int = 120) -> str:
        snippet = (self.text or "").strip().replace("\t", " ")
        if len(snippet) > width:
            snippet = snippet[: width - 1] + "…"
        return f"{self.path}:{self.line}: {snippet}"


def _glob_match(relative: str, pattern: str) -> bool:
    pattern = (pattern or "").strip()
    if not pattern:
        return True
    if "/" not in pattern and "\\" not in pattern:
        return fnmatch.fnmatch(Path(relative).name, pattern)
    return fnmatch.fnmatch(relative.replace("\\", "/"), pattern.replace("\\", "/"))


def search_project(
    root: Path,
    query: str,
    *,
    glob: str = "",
    limit: int = DEFAULT_LIMIT,
    max_scan: int = DEFAULT_SCAN,
) -> list[SearchHit]:
    """Подстрочный поиск по текстовым файлам проекта (как tool search)."""
    root = Path(root).resolve()
    needle = str(query or "")
    if not needle.strip() or not root.is_dir():
        return []
    limit = max(1, int(limit or DEFAULT_LIMIT))
    max_scan = max(1, int(max_scan or DEFAULT_SCAN))
    hits: list[SearchHit] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
        for filename in filenames:
            if len(hits) >= limit or scanned >= max_scan:
                return hits
            full = Path(dirpath) / filename
            try:
                rel = relative_posix(root, full)
            except ValueError:
                continue
            if glob and not _glob_match(rel, glob):
                continue
            if is_binary_name(full) or full.suffix.lower() in BINARY_EXTENSIONS:
                continue
            try:
                if full.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = full.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            if "-----BEGIN" in text and "PRIVATE KEY-----" in text:
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), 1):
                if needle not in line:
                    continue
                hits.append(SearchHit(path=rel, line=number, text=line[:200]))
                if len(hits) >= limit:
                    return hits
    return hits
