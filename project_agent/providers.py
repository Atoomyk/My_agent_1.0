from __future__ import annotations

import json
import threading

import httpx

from project_agent.secrets import scrub_outbound

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"


class Stopped(Exception):
    pass


class ApiError(Exception):
    pass


class ToolCall:
    def __init__(self, id: str, name: str, arguments: dict) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments


class ModelTurn:
    def __init__(self, text: str, tool_calls: list[ToolCall], prompt_tokens: int | None = None) -> None:
        self.text = text
        self.tool_calls = tool_calls
        self.prompt_tokens = prompt_tokens


def chat_url(base: str) -> str:
    base = (base or DEFAULT_OPENAI_BASE).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def messages_url(base: str) -> str:
    base = (base or DEFAULT_ANTHROPIC_BASE).rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def images_url(base: str) -> str:
    base = (base or DEFAULT_OPENAI_BASE).rstrip("/")
    if base.endswith("/images/generations"):
        return base
    return base + "/images/generations"


def openai_tool(spec: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }


def anthropic_tool(spec: dict) -> dict:
    return {
        "name": spec["name"],
        "description": spec["description"],
        "input_schema": spec["parameters"],
    }


def _error_text(response: httpx.Response) -> str:
    message = response.text[:400]
    try:
        data = response.json()
    except Exception:
        return f"HTTP {response.status_code}: {message}"
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or message)
    elif isinstance(error, str):
        message = error
    return f"HTTP {response.status_code}: {message[:400]}"


class _HttpProvider:
    kind = ""

    def __init__(self) -> None:
        self.system = ""
        self.messages: list[dict] = []
        self.settings: dict = {}
        self.last_prompt_tokens: int | None = None
        self._http: httpx.Client | None = None
        self._transport = None
        self._cancel = threading.Event()

    def set_system(self, text: str) -> None:
        self.system = text

    def reset(self, system: str) -> None:
        self.system = system
        self.messages = []
        self.last_prompt_tokens = None

    def configure(self, settings: dict) -> None:
        self.settings = settings
        self._cancel.clear()

    def cancel(self) -> None:
        self._cancel.set()
        http = self._http
        if http is not None and not http.is_closed:
            http.close()

    def scrub_inplace(self, redact) -> None:
        self.system = redact(self.system)
        self.messages = scrub_outbound(self.messages, redact)

    def message_count(self) -> int:
        return len(self.messages)

    def _note_usage(self, data: dict) -> int | None:
        from project_agent.context_usage import parse_prompt_tokens

        tokens = parse_prompt_tokens(data)
        if tokens is not None:
            self.last_prompt_tokens = tokens
        return tokens

    def summarize(self, redact) -> str:
        raise NotImplementedError

    def _client(self) -> httpx.Client:
        if self._http is None or self._http.is_closed:
            kwargs = {"timeout": httpx.Timeout(120.0, connect=20.0)}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.Client(**kwargs)
        return self._http

    def _post(self, url: str, payload: dict, headers: dict, redact) -> dict:
        if self._cancel.is_set():
            raise Stopped()
        try:
            response = self._client().post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            if self._cancel.is_set():
                raise Stopped()
            raise ApiError("Превышено время ожидания API")
        except httpx.HTTPError as exc:
            if self._cancel.is_set():
                raise Stopped()
            raise ApiError(redact(f"Нет соединения с API: {exc}")) from None
        if response.status_code >= 400:
            raise ApiError(redact(_error_text(response)))
        try:
            return response.json()
        except json.JSONDecodeError:
            raise ApiError("API вернуло не JSON")


class OpenAIProvider(_HttpProvider):
    kind = "openai"

    def add_user(self, text: str, images: list | None) -> None:
        self.messages.append({"role": "user", "content": _openai_content(text, images)})

    def add_tool_result(self, call_id: str, name: str, text: str, images: list | None) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _openai_content(text, images) if images else text,
            }
        )

    def complete(self, tools: list[dict], redact) -> ModelTurn:
        if self._cancel.is_set():
            raise Stopped()
        self.scrub_inplace(redact)
        payload = {
            "model": self.settings.get("model") or "",
            "messages": [{"role": "system", "content": self.system}, *self.messages],
            "tools": [openai_tool(tool) for tool in tools],
        }
        headers = {"Content-Type": "application/json"}
        key = (self.settings.get("api_key") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = self._post(chat_url(self.settings.get("base_url") or ""), payload, headers, redact)
        prompt_tokens = self._note_usage(data)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ApiError("непонятный ответ API")
        text = _text_of(message.get("content"))
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=_parse_args(function.get("arguments")),
                )
            )
        stored = {"role": "assistant", "content": message.get("content")}
        if message.get("tool_calls"):
            stored["tool_calls"] = message["tool_calls"]
        self.messages.append(stored)
        return ModelTurn(text, calls, prompt_tokens)

    def summarize(self, redact) -> str:
        from project_agent.context_usage import build_summary_user_text, flatten_messages_for_summary

        if self._cancel.is_set():
            raise Stopped()
        if len(self.messages) < 3:
            raise ApiError("Слишком мало истории для сжатия")
        self.scrub_inplace(redact)
        transcript = flatten_messages_for_summary(self.messages)
        payload = {
            "model": self.settings.get("model") or "",
            "messages": [
                {"role": "system", "content": "Ты сжимаешь историю чата для продолжения работы над проектом."},
                {"role": "user", "content": build_summary_user_text(transcript)},
            ],
        }
        headers = {"Content-Type": "application/json"}
        key = (self.settings.get("api_key") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = self._post(chat_url(self.settings.get("base_url") or ""), payload, headers, redact)
        self._note_usage(data)
        try:
            summary = _text_of(data["choices"][0]["message"].get("content")).strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ApiError("непонятный ответ API при сжатии")
        if not summary:
            raise ApiError("пустая сводка")
        self.messages = [
            {
                "role": "user",
                "content": "Сводка предыдущего разговора (контекст сжат):\n\n" + summary,
            },
            {"role": "assistant", "content": "Принял сводку. Продолжаем с учётом неё."},
        ]
        return summary


class AnthropicProvider(_HttpProvider):
    kind = "anthropic"

    def add_user(self, text: str, images: list | None) -> None:
        self.messages.append({"role": "user", "content": _anthropic_content(text, images)})

    def add_tool_result(self, call_id: str, name: str, text: str, images: list | None) -> None:
        block = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": _anthropic_content(text, images) if images else text,
        }
        if (
            self.messages
            and self.messages[-1].get("role") == "user"
            and isinstance(self.messages[-1].get("content"), list)
            and self.messages[-1]["content"]
            and self.messages[-1]["content"][0].get("type") == "tool_result"
        ):
            self.messages[-1]["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    def complete(self, tools: list[dict], redact) -> ModelTurn:
        if self._cancel.is_set():
            raise Stopped()
        self.scrub_inplace(redact)
        payload = {
            "model": self.settings.get("model") or "",
            "max_tokens": 4096,
            "system": self.system,
            "messages": self.messages,
            "tools": [anthropic_tool(tool) for tool in tools],
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": (self.settings.get("api_key") or "").strip(),
        }
        data = self._post(messages_url(self.settings.get("base_url") or ""), payload, headers, redact)
        prompt_tokens = self._note_usage(data)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ApiError("непонятный ответ API")
        text_parts = []
        calls = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                raw_input = block.get("input") or {}
                if not isinstance(raw_input, dict):
                    raw_input = {"value": raw_input}
                calls.append(ToolCall(str(block.get("id") or ""), str(block.get("name") or ""), raw_input))
        self.messages.append({"role": "assistant", "content": blocks})
        return ModelTurn("\n".join(part for part in text_parts if part), calls, prompt_tokens)

    def summarize(self, redact) -> str:
        from project_agent.context_usage import build_summary_user_text, flatten_messages_for_summary

        if self._cancel.is_set():
            raise Stopped()
        if len(self.messages) < 3:
            raise ApiError("Слишком мало истории для сжатия")
        self.scrub_inplace(redact)
        transcript = flatten_messages_for_summary(self.messages)
        payload = {
            "model": self.settings.get("model") or "",
            "max_tokens": 2048,
            "system": "Ты сжимаешь историю чата для продолжения работы над проектом.",
            "messages": [{"role": "user", "content": build_summary_user_text(transcript)}],
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": (self.settings.get("api_key") or "").strip(),
        }
        data = self._post(messages_url(self.settings.get("base_url") or ""), payload, headers, redact)
        self._note_usage(data)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ApiError("непонятный ответ API при сжатии")
        summary = "\n".join(
            str(block.get("text") or "") for block in blocks if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not summary:
            raise ApiError("пустая сводка")
        self.messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Сводка предыдущего разговора (контекст сжат):\n\n" + summary}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Принял сводку. Продолжаем с учётом неё."}],
            },
        ]
        return summary


def build_provider(kind: str):
    if kind == "anthropic":
        return AnthropicProvider()
    return OpenAIProvider()


def request_image(base_url: str, api_key: str, model: str, prompt: str, transport=None) -> bytes:
    payload = {"model": model, "prompt": prompt, "n": 1, "response_format": "b64_json"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = images_url(base_url)
    timeout = httpx.Timeout(120.0, connect=20.0)
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code == 400:
            payload.pop("response_format", None)
            response = client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise ApiError(_error_text(response))
        data = response.json()
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        raise ApiError("API изображений не вернуло файл")
    item = items[0]
    if item.get("b64_json"):
        import base64

        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
            downloaded = client.get(item["url"])
            downloaded.raise_for_status()
            return downloaded.content
    raise ApiError("API изображений вернуло ответ без файла")


def _openai_content(text: str, images: list | None):
    if not images:
        return text
    content = [{"type": "text", "text": text or " "}]
    for image in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image['media_type']};base64,{image['b64']}"},
            }
        )
    return content


def _anthropic_content(text: str, images: list | None):
    content = [{"type": "text", "text": text or " "}]
    for image in images or []:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image["media_type"],
                    "data": image["b64"],
                },
            }
        )
    return content


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
        return "\n".join(parts)
    return ""


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": raw}
