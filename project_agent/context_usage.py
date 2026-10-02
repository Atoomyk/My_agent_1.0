from __future__ import annotations

import json

DEFAULT_CONTEXT_LIMIT = 256_000
MIN_CONTEXT_LIMIT = 8_000
MAX_CONTEXT_LIMIT = 2_000_000
WARN_RATIO = 0.8
FULL_RATIO = 0.95


def normalize_context_limit(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_LIMIT
    return min(MAX_CONTEXT_LIMIT, max(MIN_CONTEXT_LIMIT, value))


def parse_prompt_tokens(data: dict) -> int | None:
    """Достаёт prompt/input tokens из ответа OpenAI-совместимого или Anthropic API."""
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "input_tokens", "prompt_token_count"):
            value = usage.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    pass
        details = usage.get("input_tokens_details")
        if isinstance(details, dict) and usage.get("input_tokens") is None:
            try:
                return max(0, int(sum(int(v) for v in details.values() if v is not None)))
            except (TypeError, ValueError):
                pass
    for key in ("prompt_tokens", "input_tokens"):
        value = data.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                pass
    return None


def _chars_to_tokens(text: str) -> int:
    text = text or ""
    if not text:
        return 0
    # Чуть консервативнее простого /4 для смешанного RU/EN кода.
    return max(1, (len(text) + 3) // 3)


def _walk_tokens(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        # data:image...;base64,XXXX — не считаем всю base64 как текст
        if "base64," in value[:200] or value.startswith("data:image"):
            return 1200
        return _chars_to_tokens(value)
    if isinstance(value, (bytes, bytearray)):
        return max(1, len(value) // 3)
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {"b64", "data"} and isinstance(item, str) and len(item) > 200:
                total += 1200
                continue
            if key_l in {"image_url", "source", "input_image"}:
                total += _walk_tokens(item)
                continue
            total += _walk_tokens(item)
        return total
    if isinstance(value, (list, tuple)):
        return sum(_walk_tokens(item) for item in value)
    if isinstance(value, (int, float, bool)):
        return 1
    try:
        return _chars_to_tokens(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _chars_to_tokens(str(value))


def estimate_tokens(system: str, messages: list, tools: list | None = None) -> int:
    """Грубая оценка, если API не вернул usage. Картинки — фикс. надбавка."""
    total = _chars_to_tokens(system or "")
    total += _walk_tokens(messages or [])
    if tools:
        try:
            total += _chars_to_tokens(json.dumps(tools, ensure_ascii=False))
        except (TypeError, ValueError):
            total += _walk_tokens(tools)
    return max(0, int(total))


def format_context_short(used: int, limit: int) -> str:
    limit = normalize_context_limit(limit)
    used = max(0, int(used or 0))
    pct = 0 if limit <= 0 else min(999, round(100 * used / limit))
    return f"{pct}%"


def format_context_detail(used: int, limit: int, from_api: bool = False) -> str:
    limit = normalize_context_limit(limit)
    used = max(0, int(used or 0))
    pct = 0 if limit <= 0 else min(999, round(100 * used / limit))
    mark = "" if from_api else "~"
    return f"{pct}% · {mark}{_fmt_k(used)}/{_fmt_k(limit)}"


def context_ratio(used: int, limit: int) -> float:
    limit = normalize_context_limit(limit)
    if limit <= 0:
        return 0.0
    return max(0.0, min(1.0, float(used) / float(limit)))


def _fmt_k(value: int) -> str:
    value = max(0, int(value))
    if value < 1000:
        return str(value)
    if value < 10_000:
        return f"{value / 1000:.1f}K".replace(".0K", "K")
    return f"{value / 1000:.0f}K"


_SUMMARY_PROMPT = (
    "Сжми предыдущий разговор в краткую сводку для продолжения работы. "
    "Сохрани: цель задачи, принятые решения, изменённые файлы, ошибки/тесты, что ещё сделать. "
    "Без секретов и без длинных листингов кода — только суть. Язык: русский."
)


def build_summary_user_text(transcript: str) -> str:
    body = (transcript or "").strip()
    if len(body) > 120_000:
        body = body[:60_000] + "\n…\n" + body[-60_000:]
    return f"{_SUMMARY_PROMPT}\n\n---\n{body}"


def flatten_messages_for_summary(messages: list) -> str:
    parts: list[str] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "?")
        content = item.get("content")
        text = _plain(content)
        if item.get("tool_calls"):
            names = []
            for call in item.get("tool_calls") or []:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    names.append(str(fn.get("name") or call.get("name") or "tool"))
            if names:
                text = (text + "\n" if text else "") + "tools: " + ", ".join(names)
        if role == "tool" or (isinstance(content, list) and content and isinstance(content[0], dict) and content[0].get("type") == "tool_result"):
            role = "tool"
        if text.strip():
            parts.append(f"{role}: {text.strip()[:4000]}")
    return "\n\n".join(parts)


def _plain(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    chunks.append(_plain(block.get("content")))
                elif "text" in block:
                    chunks.append(str(block.get("text") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content)
