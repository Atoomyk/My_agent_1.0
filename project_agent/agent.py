from __future__ import annotations

import threading
from pathlib import Path

from project_agent.providers import ApiError, Stopped, build_provider
from project_agent.rules import load_project_rules
from project_agent.secrets import Scrubber, literals_from_settings
from project_agent.tools import TOOL_SPECS

SYSTEM = """Ты помощник по файлам проекта. Корень: {root}
Весь проект в контекст не входит: нужные файлы читай инструментами.
Человек может явно приложить файлы и папки (@путь / @папка или вложение): файлы — содержимое, папки — только дерево путей. Изображение можно вставить из буфера (Ctrl+V).
Если ниже есть блок «Правила проекта» из AGENTS.md или .projectagent/rules — следуй им.
Метки вида [[SEC:...:N]] заменяют пароли и ключи. Копируй метку целиком, если значение нужно сохранить. Не пытайся её раскрыть.
Не вставляй секреты в web_search, generate_image и аргументы MCP: оттуда метки будут удалены.
Не выходи за пределы проекта. Каталоги .git, __pycache__, node_modules, .venv, dist и build недоступны.
Запись файла, генерация изображения и запуск тестов выполняются только после подтверждения человека. Если он отказал, не повторяй то же действие.
Можно смотреть изображения проекта, искать в интернете, генерировать картинки, запускать пресет тестов из настроек (run_tests), смотреть индекс путей (project_index), работать с git (status/diff/log; commit только после подтверждения) и вызывать настроенные MCP-инструменты.
Push, reset --hard и произвольный shell недоступны.
Чтобы быстро найти файл по имени или фрагменту пути, используй project_index с action=find. Это не содержимое файлов — только пути.
После правок кода, если тесты включены в настройках, запускай run_tests и по выводу решай, нужна ли ещё правка.
Лимит запусков тестов за один ход задан в настройках; при лимите или повторном том же FAIL остановись и опиши результат человеку.
Отвечай на языке пользователя. Когда задача сделана, ответь текстом без инструментов.
"""


class Agent:
    def __init__(self, vault, toolbox, mcp, on_chat, on_journal, on_status) -> None:
        self.vault = vault
        self.toolbox = toolbox
        self.mcp = mcp
        self.on_chat = on_chat
        self.on_journal = on_journal
        self.on_status = on_status
        self.provider = None
        self.provider_kind = None
        self.root: Path | None = None
        self.stop = threading.Event()

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
        base = SYSTEM.replace("{root}", root)
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
        max_steps = int(settings.get("max_steps") or 25)
        for step in range(1, max_steps + 1):
            if stop.is_set():
                self.on_chat("Остановлено.")
                return
            self.on_status(f"Шаг {step} из {max_steps}")
            try:
                turn = self.provider.complete(TOOL_SPECS, scrubber)
            except Stopped:
                self.on_chat("Остановлено.")
                return
            except ApiError as exc:
                self.on_chat(f"Ошибка API: {exc}")
                return
            except Exception as exc:
                self.on_chat(f"Ошибка: {exc}")
                return
            if turn.text.strip():
                self.on_chat(scrubber(turn.text))
            if not turn.tool_calls:
                if not turn.text.strip():
                    self.on_chat("(пустой ответ модели)")
                self.on_status("Готово")
                return
            for call in turn.tool_calls:
                if stop.is_set():
                    self.on_chat("Остановлено.")
                    return
                outcome = self.toolbox.execute(call.name, call.arguments)
                self.on_journal(outcome.journal)
                self.provider.add_tool_result(call.id, call.name, outcome.model_text, outcome.images)
        self.on_chat(f"Достигнут лимит шагов ({max_steps}).")
        self.on_status("Готово")
