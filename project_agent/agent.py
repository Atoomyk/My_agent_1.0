from __future__ import annotations

import threading
import time
from pathlib import Path

from project_agent.providers import ApiError, Stopped, build_provider
from project_agent.plan import format_plan_context, parse_plan_checklist
from project_agent.rules import load_project_rules
from project_agent.secrets import Scrubber, literals_from_settings
from project_agent.tools import mode_allows_writes, normalize_agent_mode, tools_for_mode
from project_agent.context_usage import (
    DEFAULT_CONTEXT_LIMIT,
    estimate_tokens,
    normalize_context_limit,
)

SYSTEM_AGENT = """Ты помощник по файлам проекта. Режим: Agent. Корень: {root}
Весь проект в контекст не входит: нужные файлы читай инструментами.
Человек может явно приложить файлы и папки (@путь / @папка или вложение): файлы — содержимое, папки — только дерево путей. Изображение можно вставить из буфера (Ctrl+V).
Если ниже есть блок «Правила проекта» из AGENTS.md или .projectagent/rules — следуй им.
Метки вида [[SEC:...:N]] заменяют пароли и ключи. Копируй метку целиком, если значение нужно сохранить. Не пытайся её раскрыть.
Не вставляй секреты в web_search, generate_image и аргументы MCP: оттуда метки будут удалены.
Не выходи за пределы проекта. Каталоги .git, __pycache__, node_modules, .venv, dist и build недоступны.
Запись файла, генерация изображения, run_tests и run_allowed выполняются только после подтверждения человека. Если он отказал, не повторяй то же действие.
Можно смотреть изображения проекта, искать в интернете, генерировать картинки, запускать пресет тестов (run_tests), команды из allowlist (run_allowed), смотреть индекс путей (project_index), работать с git (status/diff/log; commit только после подтверждения) и вызывать настроенные MCP-инструменты.
Тесты после правок — через run_tests и пресет в настройках → Проект. Сборка и разовые allowlist-команды — run_allowed с id (unittest, pytest, npm_test, build_ps1 или свои из настроек). Произвольный shell недоступен.
Браузер не встроен: если в настройках MCP есть playwright — используй инструмент browser (navigate → snapshot → click/type по ref). Свой Chrome ProjectAgent не запускает.
Push, reset --hard и произвольный shell недоступны.
Чтобы быстро найти файл по имени или фрагменту пути — project_index action=find. Символы Python/JS/TS — find_symbol; импорты файла — imports+path; кто импортирует — importers. Не содержимое файлов и не LSP.
Перед правкой читай связанные файлы (импорты, соседние модули, тесты по имени) — не правь вслепую и не крути много мелких шагов наугад.
После правок кода, если тесты включены в настройках, запускай run_tests и по выводу решай, нужна ли ещё правка.
Лимит запусков тестов за один ход задан в настройках. При повторном том же FAIL или сообщении СТОП — больше не вызывай инструменты (в т.ч. run_tests), кратко опиши проблему.
Когда задача сделана или ход остановлен: ответь текстом без инструментов и перечисли изменённые файлы (пути). Если правок не было — скажи об этом.
Отвечай на языке пользователя.
"""

SYSTEM_ASK = """Ты помощник по файлам проекта. Режим: Ask (только чтение и ответы). Корень: {root}
Весь проект в контекст не входит: нужные файлы читай инструментами.
Человек может явно приложить файлы и папки (@путь / @папка или вложение): файлы — содержимое, папки — только дерево путей. Изображение можно вставить из буфера (Ctrl+V).
Если ниже есть блок «Правила проекта» из AGENTS.md или .projectagent/rules — следуй им.
Метки вида [[SEC:...:N]] заменяют пароли и ключи. Не пытайся их раскрыть.
Не выходи за пределы проекта. Каталоги .git, __pycache__, node_modules, .venv, dist и build недоступны.
В Ask нельзя менять проект: нет write_file, apply_patch, run_tests, run_allowed, git commit, generate_image, browser, call_mcp_tool.
Можно: list_dir, read_file, search, project_index (find/find_symbol/imports/importers), view_image, web_search, list_mcp_tools, git status/diff/log.
Отвечай на языке пользователя. Когда ответ готов — текстом без инструментов.
"""

SYSTEM_PLAN = """Ты помощник по файлам проекта. Режим: Plan (план без записи). Корень: {root}
Весь проект в контекст не входит: нужные файлы читай инструментами.
Человек может явно приложить файлы и папки (@путь / @папка или вложение): файлы — содержимое, папки — только дерево путей. Изображение можно вставить из буфера (Ctrl+V).
Если ниже есть блок «Правила проекта» из AGENTS.md или .projectagent/rules — следуй им.
Метки вида [[SEC:...:N]] заменяют пароли и ключи. Не пытайся их раскрыть.
Не выходи за пределы проекта. Каталоги .git, __pycache__, node_modules, .venv, dist и build недоступны.
В Plan нельзя менять проект и запускать команды: нет write_file, apply_patch, run_tests, run_allowed, git commit, generate_image, browser, call_mcp_tool.
Можно: list_dir, read_file, search, project_index (find/find_symbol/imports/importers), view_image, web_search, list_mcp_tools, git status/diff/log.
Задача: разобрать задачу, при необходимости прочитать код, затем выдать короткий план чеклистом в чате.
Формат шагов (по одному в строке): «- [ ] шаг» или с якорем файла «- [ ] `path/to/file`: шаг».
Не пиши файлы и не предлагай «уже внести правку» в этом режиме. В конце напомни: чтобы выполнить план, переключиться в Agent и написать «делай».
Отвечай на языке пользователя. Когда план готов — текстом без инструментов.
"""

SYSTEM_DEBUG = """Ты помощник по файлам проекта. Режим: Debug. Корень: {root}
Весь проект в контекст не входит: нужные файлы читай инструментами.
Человек может явно приложить файлы и папки (@путь / @папка или вложение): файлы — содержимое, папки — только дерево путей. Изображение можно вставить из буфера (Ctrl+V).
Если ниже есть блок «Правила проекта» из AGENTS.md или .projectagent/rules — следуй им.
Метки вида [[SEC:...:N]] заменяют пароли и ключи. Копируй метку целиком, если значение нужно сохранить. Не пытайся её раскрыть.
Не вставляй секреты в web_search, generate_image и аргументы MCP: оттуда метки будут удалены.
Не выходи за пределы проекта. Каталоги .git, __pycache__, node_modules, .venv, dist и build недоступны.
Запись файла, генерация изображения, run_tests и run_allowed — только после подтверждения. Если отказал — не повторяй то же действие.
Дисциплина Debug: 1) воспроизведи или собери факты (чтение кода, логи, run_tests / run_allowed); 2) кратко сформулируй гипотезу; 3) одна точечная правка; 4) снова проверь.
Не размазывай правки по многим файлам наугад. Push, reset --hard и произвольный shell недоступны.
Тесты после правок — run_tests; разовые allowlist-команды — run_allowed. При FAIL смотри блок «Этот ход тронул» и «Суть падения». При повторном том же FAIL или СТОП — остановись и опиши проблему.
Когда задача сделана или ход остановлен: ответь текстом без инструментов и перечисли изменённые файлы. Если правок не было — скажи об этом.
Отвечай на языке пользователя.
"""

_SYSTEM_BY_MODE = {
    "agent": SYSTEM_AGENT,
    "ask": SYSTEM_ASK,
    "plan": SYSTEM_PLAN,
    "debug": SYSTEM_DEBUG,
}
_STATUS_BY_MODE = {
    "ask": " · Ask",
    "plan": " · Plan",
    "debug": " · Debug",
}

# совместимость со старыми импортами/тестами
SYSTEM = SYSTEM_AGENT


def tool_running_label(name: str, arguments) -> str:
    """Короткая метка для статуса/журнала, пока инструмент ещё выполняется."""
    tool = str(name or "").strip() or "tool"
    args = arguments if isinstance(arguments, dict) else {}
    if tool == "browser":
        action = str(args.get("action") or "").strip()
        return f"browser {action}".strip() if action else "browser"
    if tool == "call_mcp_tool":
        server = str(args.get("server") or "").strip()
        mcp_tool = str(args.get("tool") or "").strip()
        if server and mcp_tool:
            return f"{server}/{mcp_tool}"
        return "call_mcp_tool"
    if tool == "run_tests":
        return "run_tests"
    if tool in {"read_file", "write_file", "apply_patch", "view_image"}:
        path = str(args.get("path") or "").strip()
        if path:
            short = path if len(path) <= 40 else "…" + path[-39:]
            return f"{tool} {short}"
    return tool


class Agent:
    def __init__(
        self,
        vault,
        toolbox,
        mcp,
        on_chat,
        on_journal,
        on_status,
        on_context=None,
        on_stream=None,
        on_error=None,
        on_stopped=None,
        on_checkpoint=None,
        on_plan=None,
        transcript_len=None,
        get_plan=None,
    ) -> None:
        self.vault = vault
        self.toolbox = toolbox
        self.mcp = mcp
        self.on_chat = on_chat
        self.on_journal = on_journal
        self.on_status = on_status
        self.on_context = on_context or (lambda *_args, **_kwargs: None)
        self.on_stream = on_stream
        self.on_error = on_error
        self.on_stopped = on_stopped or on_chat
        self.on_checkpoint = on_checkpoint
        self.on_plan = on_plan
        self.get_plan = get_plan
        self.transcript_len = transcript_len or (lambda: 0)
        self.provider = None
        self.provider_kind = None
        self.root: Path | None = None
        self.stop = threading.Event()
        self._agent_mode = "agent"
        self._last_tools: list = []
        self._context_from_api = False
        self.checkpoints = None

    def set_root(self, root: Path | None) -> None:
        self.root = None if root is None else Path(root).resolve()
        self.toolbox.set_root(self.root)
        self.mcp.set_project(self.root)
        self.reset_session(announce=False)

    def reset_session(self, announce: bool = False) -> None:
        self.vault.clear()
        kind = self.provider_kind
        self.provider = build_provider(kind) if kind else None
        if self.provider:
            self.provider.reset(self._system())
        self.mcp.close()
        if announce:
            self.on_chat("Новый чат.")

    def export_session(self) -> tuple[str, list]:
        if self.provider is None:
            return "", []
        kind = self.provider.kind if self.provider.kind in ("openai", "anthropic") else ""
        messages = list(self.provider.messages or [])
        return kind, messages

    def restore_session(self, provider: str, messages: list) -> bool:
        from project_agent.chats import sanitize_messages

        kind = str(provider or "").strip().lower()
        if kind not in ("openai", "anthropic"):
            return False
        cleaned = sanitize_messages(messages)
        if not cleaned:
            return False
        self.vault.clear()
        self.mcp.close()
        self.provider = build_provider(kind)
        self.provider_kind = kind
        self.provider.reset(self._system())
        self.provider.messages = cleaned
        return True

    def close(self) -> None:
        self.stop.set()
        if self.provider:
            self.provider.cancel()
        self.toolbox.cancel()
        self.mcp.close()

    def _system(self) -> str:
        root = str(self.root) if self.root else ""
        template = _SYSTEM_BY_MODE.get(self._agent_mode, SYSTEM_AGENT)
        base = template.replace("{root}", root)
        block, _sources = load_project_rules(self.root)
        parts = [base]
        if block:
            parts.append(block)
        if self._agent_mode in ("agent", "debug") and self.get_plan is not None:
            plan_block = format_plan_context(self.get_plan())
            if plan_block:
                parts.append(plan_block)
        return "\n".join(parts)

    def run_turn(self, text: str, images: list, settings: dict, stop: threading.Event, resend: bool = False) -> None:
        self.stop = stop
        self.toolbox.stop = stop
        self.toolbox.settings = lambda: settings
        if not resend:
            self.toolbox.begin_turn()
            self._abandon_checkpoint()
        if self.root is None:
            self.on_chat("Сначала выберите папку проекта.")
            return
        self._agent_mode = normalize_agent_mode(settings.get("agent_mode"))
        self.mcp.set_servers(settings.get("mcp_servers") or [])
        kind = settings.get("provider") or "openai"
        if self.provider is None or self.provider.kind != kind:
            previous = self.provider_kind
            self.provider = build_provider(kind)
            self.provider_kind = kind
            self.provider.reset(self._system())
            if previous is not None:
                self.on_chat("Контекст модели сброшен: сменился провайдер.")
        self.provider.configure(settings)
        self.provider.set_system(self._system())
        scrubber = Scrubber(self.vault, literals_from_settings(settings))
        if resend and self.provider.message_count():
            if not self.provider.rewind_to_last_user():
                self.provider.drop_incomplete_tail()
        if not resend or self.provider.message_count() == 0:
            self.provider.add_user(scrubber(text), images)
        if not resend:
            self._open_checkpoint()
        specs = tools_for_mode(self._agent_mode)
        self._last_tools = specs
        max_steps = int(settings.get("max_steps") or 25)
        if self._agent_mode in ("ask", "plan"):
            max_steps = min(max_steps, 15)
        try:
            for step in range(1, max_steps + 1):
                if stop.is_set():
                    self.on_stopped("Остановлено.")
                    self._publish_context(settings)
                    return
                mark = _STATUS_BY_MODE.get(self._agent_mode, "")
                self.on_status(f"Шаг {step} из {max_steps}{mark}")
                try:
                    turn = self.provider.complete(specs, scrubber, self._stream_hook(scrubber))
                except Stopped as exc:
                    self._emit_partial(exc, scrubber)
                    self.on_stopped("Остановлено.")
                    self._publish_context(settings)
                    return
                except ApiError as exc:
                    self._emit_partial(exc, scrubber)
                    self._report_error(f"Ошибка API: {exc}")
                    self._publish_context(settings)
                    return
                except Exception as exc:
                    self._report_error(f"Ошибка: {exc}")
                    self._publish_context(settings)
                    return
                if turn.prompt_tokens is not None:
                    self._context_from_api = True
                    self._emit_context(turn.prompt_tokens, settings, from_api=True)
                if turn.text.strip():
                    self._emit_assistant(scrubber(turn.text))
                    if self._agent_mode == "plan" and not turn.tool_calls:
                        self._capture_plan(turn.text)
                if not turn.tool_calls:
                    if not turn.text.strip():
                        self.on_chat("(пустой ответ модели)")
                    self._announce_touched()
                    self._publish_context(settings)
                    self.on_status("Готово")
                    return
                for call in turn.tool_calls:
                    if stop.is_set():
                        self.on_stopped("Остановлено.")
                        self._publish_context(settings)
                        return
                    running = tool_running_label(call.name, call.arguments)
                    mark = _STATUS_BY_MODE.get(self._agent_mode, "")
                    self.on_status(f"Шаг {step} из {max_steps} · {running}…{mark}")
                    self.on_journal(f"{running}: выполняется…")
                    outcome = self.toolbox.execute(call.name, call.arguments)
                    self.on_journal(outcome.journal)
                    self.provider.add_tool_result(call.id, call.name, outcome.model_text, outcome.images)
            self.on_chat(f"Достигнут лимит шагов ({max_steps}).")
            self._announce_touched()
            self._publish_context(settings)
            self.on_status("Готово")
        finally:
            self._finish_checkpoint()

    def compress_context(self, settings: dict, stop: threading.Event) -> None:
        self.stop = stop
        if self.provider is None:
            self.on_chat("Нет активной сессии модели.")
            return
        if self.provider.message_count() < 3:
            self.on_chat("Мало истории для сжатия — продолжайте диалог.")
            return
        kind = settings.get("provider") or "openai"
        if self.provider.kind != kind:
            self.on_chat("Сменился провайдер — сначала отправьте сообщение.")
            return
        self.provider.configure(settings)
        scrubber = Scrubber(self.vault, literals_from_settings(settings))
        self.on_status("Сжатие контекста…")
        try:
            summary = self.provider.summarize(scrubber)
        except Stopped:
            self.on_chat("Сжатие остановлено.")
            return
        except ApiError as exc:
            self.on_chat(f"Не удалось сжать контекст: {exc}")
            return
        except Exception as exc:
            self.on_chat(f"Не удалось сжать контекст: {exc}")
            return
        preview = " ".join(summary.split())[:240]
        self.on_chat(
            "Контекст сжат для модели (лента чата на экране не очищена). "
            f"Сводка: {preview}" + ("…" if len(preview) >= 240 else "")
        )
        self._last_tools = tools_for_mode(normalize_agent_mode(settings.get("agent_mode")))
        self._publish_context(settings)
        self.on_status("Готово")

    def _stream_hook(self, scrubber):
        if self.on_stream is None:
            return None
        parts: list[str] = []
        state = {"last": 0.0}

        def on_piece(piece: str) -> None:
            parts.append(piece)
            now = time.monotonic()
            if now - state["last"] < 0.05:
                return
            state["last"] = now
            self.on_stream(scrubber("".join(parts)), False)

        return on_piece

    def _report_error(self, text: str) -> None:
        if self.on_error is not None:
            self.on_error(text)
        else:
            self.on_chat(text)

    def _emit_partial(self, exc, scrubber) -> None:
        self._emit_assistant(scrubber(getattr(exc, "partial", "") or ""))

    def _emit_assistant(self, text: str) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        if self.on_stream is not None:
            self.on_stream(text, True)
        else:
            self.on_chat(text)

    def _announce_touched(self) -> None:
        paths = self.toolbox.touched_paths()
        if not paths:
            return
        self.on_journal("изменены: " + ", ".join(paths))

    def _capture_plan(self, text: str) -> None:
        if self.on_plan is None:
            return
        steps = parse_plan_checklist(text)
        if not steps:
            return
        try:
            self.on_plan(steps)
        except Exception:
            pass

    def _open_checkpoint(self) -> None:
        stack = self.checkpoints
        if stack is None or not mode_allows_writes(self._agent_mode) or self.provider is None:
            return
        try:
            transcript_len = int(self.transcript_len() or 0)
        except Exception:
            transcript_len = 0
        stack.begin(transcript_len, self.provider.message_count())
        self.toolbox.on_before_write = stack.capture

    def _abandon_checkpoint(self) -> None:
        stack = self.checkpoints
        self.toolbox.on_before_write = None
        if stack is not None:
            stack.abandon()

    def _finish_checkpoint(self) -> None:
        stack = self.checkpoints
        self.toolbox.on_before_write = None
        if stack is None or not mode_allows_writes(self._agent_mode):
            self._abandon_checkpoint()
            return
        checkpoint, dropped = stack.finalize()
        if self.on_checkpoint is None:
            return
        if dropped:
            try:
                self.on_checkpoint(None, 0, dropped)
            except Exception:
                pass
        if checkpoint is None:
            return
        try:
            self.on_checkpoint(checkpoint.id, checkpoint.file_count(), [])
        except Exception:
            pass

    def truncate_messages(self, count: int) -> None:
        if self.provider is None:
            return
        keep = max(0, int(count))
        self.provider.messages = list(self.provider.messages[:keep])

    def _publish_context(self, settings: dict) -> None:
        if self.provider is None:
            return
        limit = normalize_context_limit(settings.get("context_limit") or DEFAULT_CONTEXT_LIMIT)
        estimated = estimate_tokens(self.provider.system, self.provider.messages, self._last_tools)
        api = self.provider.last_prompt_tokens
        if api is not None:
            # После хода контекст мог вырасти tool-результатами — берём max.
            used = max(api, estimated)
            from_api = used == api
        else:
            used = estimated
            from_api = False
        self._emit_context(used, settings, from_api=from_api)

    def _emit_context(self, used: int, settings: dict, from_api: bool) -> None:
        limit = normalize_context_limit(settings.get("context_limit") or DEFAULT_CONTEXT_LIMIT)
        try:
            self.on_context(int(used), int(limit), from_api)
        except TypeError:
            self.on_context(int(used), int(limit))
