from __future__ import annotations

import threading
from pathlib import Path

from project_agent.providers import ApiError, Stopped, build_provider
from project_agent.rules import load_project_rules
from project_agent.secrets import Scrubber, literals_from_settings
from project_agent.tools import normalize_agent_mode, tools_for_mode
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
Запись файла, генерация изображения и запуск тестов выполняются только после подтверждения человека. Если он отказал, не повторяй то же действие.
Можно смотреть изображения проекта, искать в интернете, генерировать картинки, запускать пресет тестов из настроек (run_tests), смотреть индекс путей (project_index), работать с git (status/diff/log; commit только после подтверждения) и вызывать настроенные MCP-инструменты.
Браузер не встроен: если в настройках MCP есть playwright — используй инструмент browser (navigate → snapshot → click/type по ref). Свой Chrome ProjectAgent не запускает.
Push, reset --hard и произвольный shell недоступны.
Чтобы быстро найти файл по имени или фрагменту пути, используй project_index с action=find. Это не содержимое файлов — только пути.
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
В Ask нельзя менять проект: нет write_file, apply_patch, run_tests, git commit, generate_image, browser, call_mcp_tool.
Можно: list_dir, read_file, search, project_index, view_image, web_search, list_mcp_tools, git status/diff/log.
Отвечай на языке пользователя. Когда ответ готов — текстом без инструментов.
"""

# совместимость со старыми импортами/тестами
SYSTEM = SYSTEM_AGENT


class Agent:
    def __init__(self, vault, toolbox, mcp, on_chat, on_journal, on_status, on_context=None) -> None:
        self.vault = vault
        self.toolbox = toolbox
        self.mcp = mcp
        self.on_chat = on_chat
        self.on_journal = on_journal
        self.on_status = on_status
        self.on_context = on_context or (lambda *_args, **_kwargs: None)
        self.provider = None
        self.provider_kind = None
        self.root: Path | None = None
        self.stop = threading.Event()
        self._agent_mode = "agent"
        self._last_tools: list = []
        self._context_from_api = False

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

    def close(self) -> None:
        self.stop.set()
        if self.provider:
            self.provider.cancel()
        self.toolbox.cancel()
        self.mcp.close()

    def _system(self) -> str:
        root = str(self.root) if self.root else ""
        template = SYSTEM_ASK if self._agent_mode == "ask" else SYSTEM_AGENT
        base = template.replace("{root}", root)
        block, _sources = load_project_rules(self.root)
        if block:
            return f"{base}\n{block}"
        return base

    def run_turn(self, text: str, images: list, settings: dict, stop: threading.Event) -> None:
        self.stop = stop
        self.toolbox.stop = stop
        self.toolbox.settings = lambda: settings
        self.toolbox.begin_turn()
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
        self.provider.add_user(scrubber(text), images)
        specs = tools_for_mode(self._agent_mode)
        self._last_tools = specs
        max_steps = int(settings.get("max_steps") or 25)
        if self._agent_mode == "ask":
            max_steps = min(max_steps, 15)
        for step in range(1, max_steps + 1):
            if stop.is_set():
                self.on_chat("Остановлено.")
                self._publish_context(settings)
                return
            self.on_status(f"Шаг {step} из {max_steps}" + (" · Ask" if self._agent_mode == "ask" else ""))
            try:
                turn = self.provider.complete(specs, scrubber)
            except Stopped:
                self.on_chat("Остановлено.")
                self._publish_context(settings)
                return
            except ApiError as exc:
                self.on_chat(f"Ошибка API: {exc}")
                self._publish_context(settings)
                return
            except Exception as exc:
                self.on_chat(f"Ошибка: {exc}")
                self._publish_context(settings)
                return
            if turn.prompt_tokens is not None:
                self._context_from_api = True
                self._emit_context(turn.prompt_tokens, settings, from_api=True)
            if turn.text.strip():
                self.on_chat(scrubber(turn.text))
            if not turn.tool_calls:
                if not turn.text.strip():
                    self.on_chat("(пустой ответ модели)")
                self._announce_touched()
                self._publish_context(settings)
                self.on_status("Готово")
                return
            for call in turn.tool_calls:
                if stop.is_set():
                    self.on_chat("Остановлено.")
                    self._publish_context(settings)
                    return
                outcome = self.toolbox.execute(call.name, call.arguments)
                self.on_journal(outcome.journal)
                self.provider.add_tool_result(call.id, call.name, outcome.model_text, outcome.images)
        self.on_chat(f"Достигнут лимит шагов ({max_steps}).")
        self._announce_touched()
        self._publish_context(settings)
        self.on_status("Готово")

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

    def _announce_touched(self) -> None:
        paths = self.toolbox.touched_paths()
        if not paths:
            return
        self.on_journal("изменены: " + ", ".join(paths))

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
