from __future__ import annotations

import copy
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
MAX_MESSAGES = 200
MAX_MESSAGE_CHARS = 200_000
MAX_CHAT_JSON_CHARS = 4_000_000
IMAGE_NOTE = "[изображение опущено при сохранении чата]"
PROVIDERS = frozenset({"openai", "anthropic"})


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


def _provider(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if value in PROVIDERS else ""


def _clip_str(text: str) -> str:
    text = text.replace("\x00", "")
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - 1] + "…"


def _looks_like_data_url(text: str) -> bool:
    value = text.strip().lower()
    return value.startswith("data:image/") and ";base64," in value


def _strip_image_part(part: dict) -> dict | None:
    part_type = str(part.get("type") or "")
    if part_type in ("image_url", "image", "input_image"):
        return {"type": "text", "text": IMAGE_NOTE}
    if part_type == "image_url" or "image_url" in part:
        return {"type": "text", "text": IMAGE_NOTE}
    source = part.get("source")
    if isinstance(source, dict) and (source.get("data") or source.get("type") == "base64"):
        return {"type": "text", "text": IMAGE_NOTE}
    return part


def _clean_part(part):
    if isinstance(part, str):
        return _clip_str(part)
    if not isinstance(part, dict):
        return None
    item = copy.deepcopy(part)
    cleaned = _strip_image_part(item)
    if cleaned is not item:
        return cleaned
    if isinstance(item.get("text"), str):
        item["text"] = _clip_str(item["text"])
    content = item.get("content")
    if isinstance(content, str):
        if _looks_like_data_url(content):
            item["content"] = IMAGE_NOTE
        else:
            item["content"] = _clip_str(content)
    elif isinstance(content, list):
        nested = []
        for sub in content:
            cleaned_sub = _clean_part(sub)
            if cleaned_sub is not None:
                nested.append(cleaned_sub)
        item["content"] = nested
    image_url = item.get("image_url")
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        if _looks_like_data_url(image_url["url"]):
            return {"type": "text", "text": IMAGE_NOTE}
    return item


def _clean_message(message) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip()
    if role not in ("user", "assistant", "tool", "system"):
        return None
    item: dict = {"role": role}
    if "name" in message and message["name"] is not None:
        item["name"] = str(message.get("name") or "")[:200]
    if "tool_call_id" in message and message["tool_call_id"] is not None:
        item["tool_call_id"] = str(message.get("tool_call_id") or "")[:200]
    content = message.get("content")
    if isinstance(content, str):
        item["content"] = IMAGE_NOTE if _looks_like_data_url(content) else _clip_str(content)
    elif isinstance(content, list):
        parts = []
        for part in content:
            cleaned = _clean_part(part)
            if cleaned is not None:
                parts.append(cleaned)
        item["content"] = parts
    elif content is None:
        item["content"] = ""
    else:
        item["content"] = _clip_str(str(content))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        cleaned_calls = []
        for call in tool_calls[:40]:
            if not isinstance(call, dict):
                continue
            entry = {
                "id": str(call.get("id") or "")[:200],
                "type": str(call.get("type") or "function")[:40],
            }
            function = call.get("function")
            if isinstance(function, dict):
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    arguments = _clip_str(arguments)
                elif arguments is not None:
                    try:
                        arguments = _clip_str(json.dumps(arguments, ensure_ascii=False))
                    except (TypeError, ValueError):
                        arguments = "{}"
                else:
                    arguments = "{}"
                entry["function"] = {
                    "name": str(function.get("name") or "")[:120],
                    "arguments": arguments,
                }
            cleaned_calls.append(entry)
        if cleaned_calls:
            item["tool_calls"] = cleaned_calls
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str) or not reasoning:
        reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        item["reasoning_content"] = _clip_str(reasoning)
    return item


def sanitize_messages(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw[-MAX_MESSAGES:]:
        message = _clean_message(item)
        if message is not None:
            cleaned.append(message)
    while cleaned and len(json.dumps(cleaned, ensure_ascii=False)) > MAX_CHAT_JSON_CHARS:
        cleaned = cleaned[max(1, len(cleaned) // 4) :]
    return cleaned


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
    messages = sanitize_messages(data.get("messages"))
    provider = _provider(data.get("provider"))
    if not messages or not provider:
        messages = []
        provider = ""
    return {
        "id": chat_id,
        "title": title,
        "project_dir": project_dir,
        "updated": updated,
        "lines": lines,
        "messages": messages,
        "provider": provider,
    }


def save_chat(
    chat_id: str,
    title: str,
    project_dir: str,
    lines: list[str],
    messages: list | None = None,
    provider: str = "",
) -> None:
    record = _record(
        {
            "id": chat_id,
            "title": title,
            "project_dir": project_dir,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "lines": lines,
            "messages": messages or [],
            "provider": provider,
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
