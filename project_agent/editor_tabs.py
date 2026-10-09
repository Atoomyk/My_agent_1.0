"""Состояние вкладок редактора (без Tk) — dirty per tab."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class EditorTab:
    path: str
    saved: str
    text: str

    @property
    def dirty(self) -> bool:
        return self.text != self.saved

    def label(self) -> str:
        name = Path(self.path).name or self.path
        return f"{name} ●" if self.dirty else name


class EditorTabs:
    def __init__(self) -> None:
        self._tabs: list[EditorTab] = []
        self._active: int = -1

    @property
    def tabs(self) -> list[EditorTab]:
        return self._tabs

    @property
    def active_index(self) -> int:
        return self._active

    def __bool__(self) -> bool:
        return bool(self._tabs)

    def __len__(self) -> int:
        return len(self._tabs)

    def active(self) -> EditorTab | None:
        if 0 <= self._active < len(self._tabs):
            return self._tabs[self._active]
        return None

    def find(self, path: str) -> int:
        for index, tab in enumerate(self._tabs):
            if tab.path == path:
                return index
        return -1

    def sync_active_text(self, text: str) -> None:
        tab = self.active()
        if tab is not None:
            tab.text = text

    def open(self, path: str, text: str) -> int:
        """Открыть путь или сфокусировать существующую вкладку. Возвращает индекс."""
        index = self.find(path)
        if index >= 0:
            self._active = index
            return index
        self._tabs.append(EditorTab(path=path, saved=text, text=text))
        self._active = len(self._tabs) - 1
        return self._active

    def select(self, index: int) -> EditorTab | None:
        if 0 <= index < len(self._tabs):
            self._active = index
            return self._tabs[index]
        return None

    def mark_saved(self, text: str | None = None) -> None:
        self.mark_saved_at(self._active, text)

    def mark_saved_at(self, index: int, text: str | None = None) -> None:
        if not (0 <= index < len(self._tabs)):
            return
        tab = self._tabs[index]
        if text is not None:
            tab.text = text
        tab.saved = tab.text

    def close_active(self) -> EditorTab | None:
        """Закрыть активную вкладку; вернуть новую активную или None."""
        return self.close_at(self._active)

    def close_at(self, index: int) -> EditorTab | None:
        """Закрыть вкладку по индексу; вернуть новую активную или None."""
        if not (0 <= index < len(self._tabs)):
            return self.active()
        was_active = index == self._active
        del self._tabs[index]
        if not self._tabs:
            self._active = -1
            return None
        if was_active:
            if self._active >= len(self._tabs):
                self._active = len(self._tabs) - 1
        elif index < self._active:
            self._active -= 1
        return self._tabs[self._active]

    def close_all(self) -> None:
        self._tabs.clear()
        self._active = -1

    def any_dirty(self) -> bool:
        return any(tab.dirty for tab in self._tabs)

    def apply_disk_if_clean(self, path: str, text: str) -> bool:
        """Обновить чистую вкладку с диска. True, если изменилась активная."""
        index = self.find(path)
        if index < 0:
            return False
        tab = self._tabs[index]
        if tab.dirty:
            return False
        if tab.saved == text and tab.text == text:
            return False
        tab.saved = text
        tab.text = text
        return index == self._active
