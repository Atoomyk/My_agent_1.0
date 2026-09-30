from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from project_agent.config import config_dir

_ID = re.compile(r"[0-9a-f]{32}")
MAX_LINES = 400
MAX_LINE = 8_000


def chats_dir() -> Path:
    return config_dir() / "chats"


def new_chat_id() -> str:
    return uuid.uuid4().hex


def chat_path(chat_id: str) -> Path:
    if not _ID.fullmatch(chat_id or ""):
        raise ValueError("некорректный идентификатор чата")
    return chats_dir() / f"{chat_id}.json"


def _norm(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(str(Path(text).expanduser().resolve(strict=False)))


def _clean_lines(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    lines = []
    for item in raw[:MAX_LINES]:
        if not isinstance(item, str):
            continue
        text = item.replace("\x00", "").rstrip()
        if text:
            lines.append(text[:MAX_LINE])
    return lines


def _record(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    chat_id = str(data.get("id") or "")
    if not _ID.fullmatch(chat_id):
        return None
    title = " ".join(str(data.get("title") or "").split())[:80] or "Чат"
    project_dir = str(data.get("project_dir") or "")
    updated = str(data.get("updated") or "")
    lines = _clean_lines(data.get("lines"))
    if not project_dir or not lines:
        return None
    return {
        "id": chat_id,
        "title": title,
        "project_dir": project_dir,
        "updated": updated,
        "lines": lines,
    }


def save_chat(chat_id: str, title: str, project_dir: str, lines: list[str]) -> None:
    record = _record(
        {
            "id": chat_id,
            "title": title,
            "project_dir": project_dir,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "lines": lines,
        }
    )
    if record is None:
        raise ValueError("чат не сохранён")
    folder = chats_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = chat_path(record["id"])
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_chat(chat_id: str) -> dict | None:
    path = chat_path(chat_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record = _record(data)
    if record is None or record["id"] != chat_id:
        return None
    return record


def list_chats(project_dir: str) -> list[dict]:
    folder = chats_dir()
    if not folder.is_dir():
        return []
    target = _norm(project_dir)
    found = []
    for path in folder.glob("*.json"):
        if path.suffix != ".json" or path.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        record = _record(data)
        if record is None or _norm(record["project_dir"]) != target:
            continue
        found.append({"id": record["id"], "title": record["title"], "updated": record["updated"]})
    found.sort(key=lambda item: item["updated"], reverse=True)
    return found


def delete_chat(chat_id: str) -> None:
    path = chat_path(chat_id)
    if path.is_file():
        path.unlink()
