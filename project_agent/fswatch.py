from __future__ import annotations

from pathlib import Path
from typing import Callable

from project_agent.paths import IGNORE_DIRS

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:  # pragma: no cover - optional until installed
    HAS_WATCHDOG = False
    FileSystemEvent = object  # type: ignore[misc, assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]
    Observer = None  # type: ignore[misc, assignment]


def _ignored_path(root: Path, raw: str) -> bool:
    if not raw:
        return True
    try:
        rel = Path(raw).resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return True
    return any(part in IGNORE_DIRS for part in rel.parts)


if HAS_WATCHDOG:

    class _Handler(FileSystemEventHandler):
        def __init__(self, root: Path, on_change: Callable[[], None]) -> None:
            super().__init__()
            self._root = root
            self._on_change = on_change

        def on_any_event(self, event: FileSystemEvent) -> None:
            if getattr(event, "is_directory", False) and getattr(event, "event_type", "") == "modified":
                # Шум от mtime папок — пропускаем.
                return
            src = str(getattr(event, "src_path", "") or "")
            dest = str(getattr(event, "dest_path", "") or "")
            if _ignored_path(self._root, src) and (not dest or _ignored_path(self._root, dest)):
                return
            try:
                self._on_change()
            except Exception:
                return

else:

    class _Handler:  # type: ignore[no-redef]
        def __init__(self, root: Path, on_change: Callable[[], None]) -> None:
            pass


class ProjectWatcher:
    """Лёгкий recursive watch корня проекта; колбэк зовётся из фонового потока."""

    def __init__(self) -> None:
        self._observer = None
        self._root: Path | None = None

    @property
    def active(self) -> bool:
        return self._observer is not None

    def start(self, root: Path, on_change: Callable[[], None]) -> bool:
        self.stop()
        if not HAS_WATCHDOG or Observer is None:
            return False
        root = Path(root).resolve()
        if not root.is_dir():
            return False
        handler = _Handler(root, on_change)
        observer = Observer()
        observer.schedule(handler, str(root), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._root = root
        return True

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        self._root = None
        if observer is None:
            return
        try:
            observer.stop()
        except Exception:
            pass
        try:
            observer.join(timeout=2.0)
        except Exception:
            pass
