from __future__ import annotations

from pathlib import Path

from project_agent.paths import MAX_FILE_BYTES

MAX_RULES_CHARS = 48_000
MAX_RULE_FILES = 20
_RULE_SUFFIXES = frozenset({".md", ".txt"})


def list_rule_files(root: Path | None) -> list[Path]:
    if root is None:
        return []
    root = Path(root).resolve()
    found: list[Path] = []
    agents = root / "AGENTS.md"
    if agents.is_file():
        found.append(agents)
    elif (root / "agents.md").is_file():
        found.append(root / "agents.md")
    rules = root / ".projectagent" / "rules"
    if rules.is_file():
        found.append(rules)
    elif rules.is_dir():
        children = [
            item
            for item in rules.iterdir()
            if item.is_file() and item.suffix.lower() in _RULE_SUFFIXES and not item.name.startswith(".")
        ]
        children.sort(key=lambda item: item.name.casefold())
        found.extend(children[:MAX_RULE_FILES])
    return found


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def load_project_rules(root: Path | None) -> tuple[str, list[str]]:
    """Читает явные правила проекта. Не сканирует весь репозиторий."""
    if root is None:
        return "", []
    root = Path(root).resolve()
    chunks: list[str] = []
    sources: list[str] = []
    total = 0
    for path in list_rule_files(root):
        rel = _rel(root, path)
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            continue
        header = f"### {rel}\n"
        room = MAX_RULES_CHARS - total - len(header) - 2
        if room < 200:
            break
        if len(text) > room:
            text = text[:room].rstrip() + "\n…"
        piece = header + text
        chunks.append(piece)
        sources.append(rel)
        total += len(piece) + 2
        if total >= MAX_RULES_CHARS:
            break
    if not chunks:
        return "", []
    body = "\n\n".join(chunks)
    return f"Правила проекта (обязательны):\n\n{body}", sources


def rules_summary(root: Path | None) -> str:
    sources = [_rel(Path(root).resolve(), path) for path in list_rule_files(root)] if root else []
    if not sources:
        return "Правила не найдены (AGENTS.md или .projectagent/rules)."
    return "Правила: " + ", ".join(sources)
