from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from project_agent.config import config_dir
from project_agent.paths import MAX_FILE_BYTES, PathError, resolve_inside

MAX_STACK = 3
_MARK = re.compile(r"^⟦checkpoint:([0-9a-f]{32})⟧$")
_ID = re.compile(r"^[0-9a-f]{32}$")


def checkpoints_dir() -> Path:
    return config_dir() / "checkpoints"


def checkpoint_mark(checkpoint_id: str) -> str:
    return f"⟦checkpoint:{checkpoint_id}⟧"


def parse_checkpoint_mark(text: str) -> str | None:
    body = (text or "").strip()
    match = _MARK.fullmatch(body)
    return match.group(1) if match else None


def new_checkpoint_id() -> str:
    return uuid.uuid4().hex


@dataclass
class TurnCheckpoint:
    id: str
    transcript_len: int
    message_count: int
    files: dict[str, dict] = field(default_factory=dict)

    def capture(self, relative: str, full: Path) -> None:
        path = str(relative or "").replace("\\", "/").strip().strip("/")
        if not path or path in self.files:
            return
        if not full.exists():
            self.files[path] = {"existed": False, "content": None, "skipped": False}
            return
        if not full.is_file():
            return
        try:
            size = full.stat().st_size
            if size > MAX_FILE_BYTES:
                self.files[path] = {"existed": True, "content": None, "skipped": True}
                return
            data = full.read_bytes()
        except OSError:
            self.files[path] = {"existed": True, "content": None, "skipped": True}
            return
        if b"\x00" in data[:8192]:
            self.files[path] = {"existed": True, "content": None, "skipped": True}
            return
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            self.files[path] = {"existed": True, "content": None, "skipped": True}
            return
        self.files[path] = {"existed": True, "content": text, "skipped": False}

    def restorable(self) -> bool:
        for snap in self.files.values():
            if not snap.get("existed"):
                return True
            if not snap.get("skipped") and snap.get("content") is not None:
                return True
        return False

    def file_count(self) -> int:
        return len(self.files)

    def to_record(self) -> dict:
        return {
            "id": self.id,
            "transcript_len": int(self.transcript_len),
            "message_count": int(self.message_count),
            "files": self.files,
        }

    @classmethod
    def from_record(cls, data) -> TurnCheckpoint | None:
        if not isinstance(data, dict):
            return None
        checkpoint_id = str(data.get("id") or "")
        if not _ID.fullmatch(checkpoint_id):
            return None
        try:
            transcript_len = max(0, int(data.get("transcript_len") or 0))
            message_count = max(0, int(data.get("message_count") or 0))
        except (TypeError, ValueError):
            return None
        files: dict[str, dict] = {}
        raw_files = data.get("files")
        if isinstance(raw_files, dict):
            for key, value in raw_files.items():
                path = str(key or "").replace("\\", "/").strip().strip("/")
                if not path or not isinstance(value, dict):
                    continue
                existed = bool(value.get("existed"))
                skipped = bool(value.get("skipped"))
                content = value.get("content")
                if content is not None and not isinstance(content, str):
                    continue
                if existed and not skipped and content is None:
                    skipped = True
                if content is not None and len(content.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
                    skipped = True
                    content = None
                files[path] = {"existed": existed, "content": content, "skipped": skipped}
        item = cls(checkpoint_id, transcript_len, message_count, files)
        return item if item.restorable() else None


class CheckpointStack:
    def __init__(self) -> None:
        self.chat_id = ""
        self.project_dir = ""
        self.items: list[TurnCheckpoint] = []
        self.current: TurnCheckpoint | None = None

    def clear(self) -> None:
        self.chat_id = ""
        self.project_dir = ""
        self.items = []
        self.current = None

    def load(self, chat_id: str, project_dir: str) -> None:
        self.clear()
        self.chat_id = chat_id or ""
        self.project_dir = project_dir or ""
        if not self.chat_id:
            return
        path = _stack_path(self.chat_id)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return
        loaded = []
        for raw in raw_items[-MAX_STACK:]:
            item = TurnCheckpoint.from_record(raw)
            if item is not None:
                loaded.append(item)
        self.items = loaded

    def save(self) -> None:
        if not self.chat_id:
            return
        folder = checkpoints_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = _stack_path(self.chat_id)
        payload = {
            "id": self.chat_id,
            "project_dir": self.project_dir,
            "items": [item.to_record() for item in self.items[-MAX_STACK:]],
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def begin(self, transcript_len: int, message_count: int) -> TurnCheckpoint:
        self.current = TurnCheckpoint(new_checkpoint_id(), max(0, int(transcript_len)), max(0, int(message_count)))
        return self.current

    def capture(self, relative: str, full: Path) -> None:
        if self.current is None:
            return
        self.current.capture(relative, full)

    def abandon(self) -> None:
        self.current = None

    def finalize(self) -> tuple[TurnCheckpoint | None, list[str]]:
        current = self.current
        self.current = None
        if current is None or not current.restorable():
            return None, []
        dropped: list[str] = []
        self.items.append(current)
        while len(self.items) > MAX_STACK:
            old = self.items.pop(0)
            dropped.append(old.id)
        self.save()
        return current, dropped

    def get(self, checkpoint_id: str) -> TurnCheckpoint | None:
        for item in self.items:
            if item.id == checkpoint_id:
                return item
        return None

    def drop_from(self, checkpoint_id: str) -> list[str]:
        index = next((i for i, item in enumerate(self.items) if item.id == checkpoint_id), -1)
        if index < 0:
            return []
        removed = [item.id for item in self.items[index:]]
        self.items = self.items[:index]
        self.save()
        return removed


def delete_checkpoints(chat_id: str) -> None:
    path = _stack_path(chat_id)
    if path.is_file():
        path.unlink()


def restore_files(root: Path, checkpoint: TurnCheckpoint) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    failed: list[str] = []
    root = Path(root).resolve()
    for relative, snap in checkpoint.files.items():
        try:
            full = resolve_inside(root, relative)
        except PathError:
            failed.append(relative)
            continue
        if not snap.get("existed"):
            try:
                if full.exists() and full.is_file():
                    full.unlink()
                restored.append(relative)
            except OSError:
                failed.append(relative)
            continue
        if snap.get("skipped") or snap.get("content") is None:
            failed.append(relative)
            continue
        content = str(snap["content"])
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8", newline="\n")
            restored.append(relative)
        except OSError:
            failed.append(relative)
    return restored, failed


def _stack_path(chat_id: str) -> Path:
    if not _ID.fullmatch(chat_id or ""):
        raise ValueError("некорректный идентификатор чата")
    return checkpoints_dir() / f"{chat_id}.json"
