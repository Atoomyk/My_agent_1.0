from __future__ import annotations

import json
import threading
import time

import httpx

from project_agent.secrets import scrub_outbound

DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE = "https://api.anthropic.com"

# Пауза между чанками и ожидание первого байта. Не общий лимит хода.
API_CONNECT_TIMEOUT = 20.0
API_READ_TIMEOUT = 300.0
# Весь один stream, даже если чанки продолжают идти.
API_OVERALL_TIMEOUT = 900.0


class Stopped(Exception):
    def __init__(self, partial: str = "") -> None:
        super().__init__("stopped")
        self.partial = partial or ""


class ApiError(Exception):
    def __init__(self, message: str = "", partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial or ""


class _RetryPlainStream(Exception):
    """Сервер не принял stream_options — повторить stream без этого поля."""


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

    def drop_incomplete_tail(self) -> None:
        """Убрать оборванный ответ без вызовов инструментов, чтобы повтор не дублировал user."""
        if not self.messages:
            return
        last = self.messages[-1]
        if not isinstance(last, dict) or last.get("role") != "assistant":
            return
        if last.get("tool_calls"):
            return
        content = last.get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use" for block in content
        ):
            return
        self.messages.pop()

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
            kwargs = {"timeout": httpx.Timeout(API_READ_TIMEOUT, connect=API_CONNECT_TIMEOUT)}
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

    def _check_stream_limits(self, started: float, partial) -> None:
        if self._cancel.is_set():
            raise Stopped(partial())
        if time.monotonic() - started > API_OVERALL_TIMEOUT:
            raise ApiError("Превышено время ожидания API", partial())

    def _take_event(self, feed, event: dict, on_text, started: float) -> None:
        self._check_stream_limits(started, feed.text)
        piece = feed.feed(event)
        if piece and on_text is not None:
            try:
                on_text(piece)
            except Exception:
                pass
        self._check_stream_limits(started, feed.text)

    def _read_model(self, url, payload, headers, redact, feed, on_text, retry_stream_options: bool) -> None:
        if self._cancel.is_set():
            raise Stopped()
        started = time.monotonic()
        try:
            with self._client().stream("POST", url, json=payload, headers=headers) as response:
                self._consume_model(response, redact, feed, on_text, started, retry_stream_options)
        except _RetryPlainStream:
            raise
        except Stopped as exc:
            raise Stopped(feed.text() or exc.partial)
        except ApiError as exc:
            if not exc.partial:
                exc.partial = feed.text()
            raise
        except httpx.TimeoutException:
            if self._cancel.is_set():
                raise Stopped(feed.text())
            raise ApiError("Превышено время ожидания API", feed.text())
        except httpx.HTTPError as exc:
            if self._cancel.is_set():
                raise Stopped(feed.text())
            raise ApiError(redact(f"Нет соединения с API: {exc}"), feed.text()) from None
        except Exception:
            if self._cancel.is_set():
                raise Stopped(feed.text()) from None
            raise

    def _consume_model(self, response, redact, feed, on_text, started: float, retry_stream_options: bool) -> None:
        self._check_stream_limits(started, feed.text)
        if response.status_code >= 400:
            response.read()
            message = _error_text(response)
            if retry_stream_options and "stream_options" in message.lower():
                raise _RetryPlainStream()
            raise ApiError(redact(message), feed.text())
        ctype = (response.headers.get("content-type") or "").lower()
        if "event-stream" in ctype:
            for event in self._iter_sse(response, started, feed):
                self._take_event(feed, event, on_text, started)
            return
        raw = response.read()
        text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").lstrip()
        if text.startswith("data:") or text.startswith("event:"):
            for event in iter_sse_payloads(text.splitlines()):
                self._take_event(feed, event, on_text, started)
            return
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            raise ApiError("API вернуло не JSON", feed.text())
        if not isinstance(data, dict):
            raise ApiError("непонятный ответ API", feed.text())
        feed.from_json(data, on_text)

    def _iter_sse(self, response: httpx.Response, started: float, feed):
        data_lines: list[str] = []

        def flush():
            nonlocal data_lines
            if not data_lines:
                return None
            payload = "\n".join(data_lines)
            data_lines = []
            if payload.strip() == "[DONE]":
                return "DONE"
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None

        for raw_line in response.iter_lines():
            self._check_stream_limits(started, feed.text)
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            line = str(raw_line).rstrip("\r")
            if line == "":
                item = flush()
                if item == "DONE":
                    return
                if isinstance(item, dict):
                    yield item
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        item = flush()
        if isinstance(item, dict):
            yield item

    def _store_partial(self, feed) -> None:
        if not str(feed.text() or "").strip():
            return
        self.messages.append(feed.partial_message())

    def _run_stream(self, url, payload, headers, redact, feed, on_text, retry_stream_options: bool = False) -> ModelTurn:
        try:
            self._read_model(url, payload, headers, redact, feed, on_text, retry_stream_options)
        except _RetryPlainStream:
            raise
        except Stopped as exc:
            self._store_partial(feed)
            raise Stopped(feed.text() or exc.partial)
        except ApiError as exc:
            self._store_partial(feed)
            if not exc.partial:
                exc.partial = feed.text()
            raise
        self.messages.append(feed.stored_message())
        return ModelTurn(feed.text(), feed.tool_calls(), self._note_usage(feed.usage_payload()))


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

    def complete(self, tools: list[dict], redact, on_text=None) -> ModelTurn:
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
        url = chat_url(self.settings.get("base_url") or "")
        streamed = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        try:
            return self._run_stream(url, streamed, headers, redact, _OpenAIFeed(), on_text, retry_stream_options=True)
        except _RetryPlainStream:
            return self._run_stream(
                url,
                {**payload, "stream": True},
                headers,
                redact,
                _OpenAIFeed(),
                on_text,
            )

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

    def complete(self, tools: list[dict], redact, on_text=None) -> ModelTurn:
        if self._cancel.is_set():
            raise Stopped()
        self.scrub_inplace(redact)
        payload = {
            "model": self.settings.get("model") or "",
            "max_tokens": 4096,
            "system": self.system,
            "messages": self.messages,
            "tools": [anthropic_tool(tool) for tool in tools],
            "stream": True,
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": (self.settings.get("api_key") or "").strip(),
        }
        return self._run_stream(
            messages_url(self.settings.get("base_url") or ""),
            payload,
            headers,
            redact,
            _AnthropicFeed(),
            on_text,
        )

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


def request_image(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    transport=None,
    size: str | None = None,
) -> bytes:
    payload = {"model": model, "prompt": prompt, "n": 1, "response_format": "b64_json"}
    if size:
        payload["size"] = size
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = images_url(base_url)
    timeout = httpx.Timeout(120.0, connect=20.0)
    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code == 400 and "response_format" in payload:
            payload.pop("response_format", None)
            response = client.post(url, json=payload, headers=headers)
        if response.status_code == 400 and size and "size" in payload:
            payload.pop("size", None)
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


def iter_sse_payloads(lines):
    data_lines: list[str] = []

    def flush():
        nonlocal data_lines
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        data_lines = []
        if payload.strip() == "[DONE]":
            return "DONE"
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="replace")
        line = str(raw_line).rstrip("\r")
        if line == "":
            item = flush()
            if item == "DONE":
                return
            if isinstance(item, dict):
                yield item
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    item = flush()
    if isinstance(item, dict):
        yield item


def _openai_calls_from_message(message: dict) -> list[ToolCall]:
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
    return calls


class _OpenAIFeed:
    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.calls: dict[int, dict] = {}
        self.usage = None
        self.usage_data = None
        self.raw_message = None

    def text(self) -> str:
        if self.raw_message is not None:
            return _text_of(self.raw_message.get("content"))
        return "".join(self.text_parts)

    def reasoning(self) -> str:
        if self.raw_message is not None:
            for key in ("reasoning_content", "reasoning"):
                value = self.raw_message.get(key)
                if isinstance(value, str) and value:
                    return value
            return ""
        return "".join(self.reasoning_parts)

    def feed(self, event: dict) -> str:
        error = event.get("error")
        if isinstance(error, dict) and not event.get("choices"):
            raise ApiError(str(error.get("message") or "ошибка API")[:400])
        usage = event.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            delta = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        piece = ""
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.text_parts.append(content)
            piece = content
        elif isinstance(content, list):
            extra = _text_of(content)
            if extra:
                self.text_parts.append(extra)
                piece = extra
        for key in ("reasoning_content", "reasoning"):
            reason = delta.get(key)
            if isinstance(reason, str) and reason:
                self.reasoning_parts.append(reason)
                break
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            try:
                index = int(call.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            slot = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if call.get("id"):
                slot["id"] = str(call["id"])
            function = call.get("function") or {}
            if function.get("name") and not slot["name"]:
                slot["name"] = str(function["name"])
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                slot["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments:
                slot["arguments"] += str(arguments)
        return piece

    def from_json(self, data: dict, on_text) -> None:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ApiError("непонятный ответ API")
        if not isinstance(message, dict):
            raise ApiError("непонятный ответ API")
        self.raw_message = message
        self.usage_data = data
        text = self.text()
        if text and on_text is not None:
            on_text(text)

    def tool_calls(self) -> list[ToolCall]:
        if self.raw_message is not None:
            return _openai_calls_from_message(self.raw_message)
        calls = []
        for index in sorted(self.calls):
            slot = self.calls[index]
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=_parse_args(slot["arguments"]),
                )
            )
        return calls

    def _stored_calls(self) -> list[dict]:
        stored = []
        for index in sorted(self.calls):
            slot = self.calls[index]
            stored.append(
                {
                    "id": slot["id"] or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"] if slot["arguments"] else "{}",
                    },
                }
            )
        return stored

    def stored_message(self) -> dict:
        if self.raw_message is not None:
            stored = {"role": "assistant", "content": self.raw_message.get("content")}
            if self.raw_message.get("tool_calls"):
                stored["tool_calls"] = self.raw_message["tool_calls"]
            for key in ("reasoning_content", "reasoning"):
                value = self.raw_message.get(key)
                if isinstance(value, str) and value:
                    stored["reasoning_content"] = value
                    break
            return stored
        text = self.text()
        stored = {"role": "assistant", "content": text if text else None}
        calls = self._stored_calls()
        if calls:
            stored["tool_calls"] = calls
        reason = self.reasoning()
        if reason:
            stored["reasoning_content"] = reason
        return stored

    def partial_message(self) -> dict:
        stored = {"role": "assistant", "content": self.text()}
        reason = self.reasoning()
        if reason:
            stored["reasoning_content"] = reason
        return stored

    def usage_payload(self) -> dict:
        if isinstance(self.usage_data, dict):
            return self.usage_data
        if isinstance(self.usage, dict):
            return {"usage": self.usage}
        return {}


class _AnthropicFeed:
    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.usage = None
        self.usage_data = None

    def text(self) -> str:
        parts = []
        for block in self.blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(part for part in parts if part)

    def _block(self, index: int) -> dict:
        while len(self.blocks) <= index:
            self.blocks.append({})
        block = self.blocks[index]
        if not isinstance(block, dict):
            block = {}
            self.blocks[index] = block
        return block

    def feed(self, event: dict) -> str:
        if event.get("type") == "error":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else error
            raise ApiError(str(message or "ошибка API")[:400])
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.usage = dict(usage)
            return ""
        if kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                merged = dict(self.usage or {})
                merged.update(usage)
                self.usage = merged
            return ""
        if kind == "content_block_start":
            try:
                index = int(event.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["_json"] = ""
                if not isinstance(block.get("input"), dict):
                    block["input"] = {}
            self._block(index)
            self.blocks[index] = block
            return ""
        if kind == "content_block_delta":
            try:
                index = int(event.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            delta = event.get("delta") or {}
            block = self._block(index)
            if delta.get("type") == "text_delta":
                piece = delta.get("text") or ""
                block["type"] = "text"
                block["text"] = (block.get("text") or "") + piece
                return piece
            if delta.get("type") == "input_json_delta":
                block["_json"] = (block.get("_json") or "") + str(delta.get("partial_json") or "")
            return ""
        if kind == "content_block_stop":
            try:
                index = int(event.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            if 0 <= index < len(self.blocks):
                self._seal_block(self.blocks[index])
            return ""
        return ""

    def _seal_block(self, block: dict) -> None:
        if not isinstance(block, dict) or "_json" not in block:
            return
        raw = block.pop("_json") or ""
        if raw:
            block["input"] = _parse_args(raw)
        elif not isinstance(block.get("input"), dict):
            block["input"] = {}

    def _seal(self) -> None:
        for block in self.blocks:
            if isinstance(block, dict):
                self._seal_block(block)

    def from_json(self, data: dict, on_text) -> None:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ApiError("непонятный ответ API")
        self.blocks = [block for block in blocks if isinstance(block, dict)]
        self.usage_data = data
        text = self.text()
        if text and on_text is not None:
            on_text(text)

    def tool_calls(self) -> list[ToolCall]:
        self._seal()
        calls = []
        for block in self.blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            raw_input = block.get("input") or {}
            if not isinstance(raw_input, dict):
                raw_input = {"value": raw_input}
            calls.append(ToolCall(str(block.get("id") or ""), str(block.get("name") or ""), raw_input))
        return calls

    def stored_message(self) -> dict:
        self._seal()
        clean = []
        for block in self.blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                clean.append({"type": "text", "text": block.get("text") or ""})
            elif block.get("type") == "tool_use":
                raw_input = block.get("input") or {}
                if not isinstance(raw_input, dict):
                    raw_input = {"value": raw_input}
                clean.append(
                    {
                        "type": "tool_use",
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "input": raw_input,
                    }
                )
        return {"role": "assistant", "content": clean}

    def partial_message(self) -> dict:
        return {"role": "assistant", "content": [{"type": "text", "text": self.text()}]}

    def usage_payload(self) -> dict:
        if isinstance(self.usage_data, dict):
            return self.usage_data
        if isinstance(self.usage, dict):
            return {"usage": self.usage}
        return {}
