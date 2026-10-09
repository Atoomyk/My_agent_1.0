from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from project_agent.images import prepare_image, prepare_image_bytes
from project_agent.patching import PatchError, apply_diff
from project_agent.paths import (
    IGNORE_DIRS,
    IMAGE_EXTENSIONS,
    MAX_FILE_BYTES,
    PathError,
    is_binary_name,
    is_image_name,
    relative_posix,
    resolve_inside,
)
from project_agent.providers import request_image
from project_agent.secrets import Scrubber, is_env_file, is_json_secret_file, is_secret_blob, literals_from_settings
from project_agent.allowed import (
    list_allowed_ids,
    normalize_allowed_commands,
    resolve_allowed,
    resolve_shell_argv,
)
from project_agent.testing import (
    LABEL_BY_PRESET,
    fail_brief,
    fail_fingerprint,
    format_command,
    normalize_fix_rounds,
    normalize_preset,
    normalize_timeout,
    preset_command,
    run_argv,
    run_preset,
    spawn_detached,
)
from project_agent.index_store import (
    build_index,
    find_importers,
    find_paths,
    find_symbols,
    index_summary,
    list_imports,
    load_index,
    touch_file,
)
from project_agent.gitops import (
    GitError,
    git_commit,
    git_diff,
    git_log,
    git_status,
    parse_commit_paths,
    preview_commit_diff,
    preview_unified,
)
from project_agent.websearch import web_search
from project_agent.browser_mcp import (
    BROWSER_CALL_TIMEOUT,
    find_browser_server_name,
    map_browser_arguments,
)
from project_agent.mcp_client import McpError

MAX_READ_LINES = 400
DEFAULT_READ_LINES = 160
MAX_MATCHES = 30
MAX_SCAN_FILES = 2000
MAX_LIST_DIR = 120
MAX_WRITE_CHARS = 1_000_000
MAX_TOOL_RESULT_CHARS = 24_000
SHELL_MAX_PER_TURN = 3


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


def _flag_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    value = str(raw or "").strip().lower()
    return value in ("1", "true", "yes", "on")


TOOL_SPECS = [
    {
        "name": "list_dir",
        "description": "Список файлов и папок на один уровень внутри проекта. Глубокой рекурсии нет.",
        "parameters": _schema({"path": _string("Путь относительно корня. Пусто — корень.")}, []),
    },
    {
        "name": "read_file",
        "description": "Текст файла с номерами строк. Бинарные и слишком большие файлы не отдаются. Секреты заменены метками [[SEC:...]].",
        "parameters": _schema(
            {
                "path": _string("Путь к файлу внутри проекта."),
                "offset": {"type": "integer", "description": "Первая строка, с 1."},
                "limit": {"type": "integer", "description": "Сколько строк, по умолчанию 200."},
            },
            ["path"],
        ),
    },
    {
        "name": "search",
        "description": "Поиск подстроки по текстовым файлам проекта.",
        "parameters": _schema(
            {
                "query": _string("Подстрока."),
                "glob": _string("Необязательный шаблон, например *.py."),
            },
            ["query"],
        ),
    },
    {
        "name": "write_file",
        "description": "Полная перезапись файла внутри проекта. Обычно нужно подтверждение; если в настройках включены авто-правки проекта — без диалога. Метки [[SEC:...]] копируй как есть, если секрет нужно сохранить.",
        "parameters": _schema(
            {
                "path": _string("Путь к файлу."),
                "content": _string("Новое содержимое целиком."),
                "summary": _string("Короткая причина без содержимого файла и без секретов."),
            },
            ["path", "content"],
        ),
    },
    {
        "name": "apply_patch",
        "description": "Правка файла. Предпочтительный diff: блоки <<<<<<< SEARCH / ======= / >>>>>>> REPLACE с точным фрагментом. Также принимается unified diff. Обычно нужно подтверждение; при авто-правках проекта — без диалога.",
        "parameters": _schema(
            {
                "path": _string("Путь к файлу."),
                "diff": _string("SEARCH/REPLACE или unified diff."),
                "summary": _string("Короткая причина без секретов."),
            },
            ["path", "diff"],
        ),
    },
    {
        "name": "view_image",
        "description": "Показать изображение из проекта (png, jpg, gif, webp). Используй, когда нужно увидеть картинку.",
        "parameters": _schema({"path": _string("Путь к изображению внутри проекта.")}, ["path"]),
    },
    {
        "name": "web_search",
        "description": "Поиск в интернете. Не передавай секреты и метки [[SEC:...]] в запрос.",
        "parameters": _schema({"query": _string("Поисковый запрос.")}, ["query"]),
    },
    {
        "name": "generate_image",
        "description": "Сгенерировать изображение и сохранить его в проект после подтверждения. В prompt не должно быть секретов. size — опционально, например 1024x1024 или 512x512 (если провайдер не поддерживает — будет без size).",
        "parameters": _schema(
            {
                "prompt": _string("Описание изображения."),
                "path": _string("Куда сохранить, относительно корня, например images/pic.png."),
                "size": _string("Размер WxH, например 1024x1024. Пусто — выбор провайдера."),
            },
            ["prompt", "path"],
        ),
    },
    {
        "name": "project_index",
        "description": (
            "Индекс проекта: пути и лёгкие символы/импорты Python и JS/TS (не LSP). "
            "action=summary|refresh|find|find_symbol|imports|importers. "
            "find/find_symbol/importers — query; imports — path к .py/.js/.ts/…. "
            "В ответ только совпадения, не весь индекс."
        ),
        "parameters": _schema(
            {
                "action": _string("summary, refresh, find, find_symbol, imports или importers."),
                "query": _string("Подстрока пути/символа/модуля."),
                "path": _string("Для imports: путь к исходному файлу относительно корня."),
            },
            ["action"],
        ),
    },
    {
        "name": "run_tests",
        "description": "Запустить пресет тестов из настроек (unittest, pytest или npm test). Нужно подтверждение. Произвольные команды запрещены.",
        "parameters": _schema(
            {"summary": _string("Короткая причина запуска без секретов.")},
            [],
        ),
    },
    {
        "name": "run_allowed",
        "description": (
            "Запустить команду из жёсткого allowlist по id. "
            "Встроенные: unittest, pytest, npm_test, build_ps1; плюс свои id из настроек → Проект. "
            "Свои с суффиксом id! — detach (GUI/долгое без ожидания). "
            "Всегда подтверждение. Нет произвольного shell и свободных аргументов. "
            "Для цикла правок+тестов предпочитай run_tests."
        ),
        "parameters": _schema(
            {
                "id": _string("Id из allowlist, например unittest или build_ps1."),
                "summary": _string("Короткая причина запуска без секретов."),
            },
            ["id"],
        ),
    },
    {
        "name": "run_shell",
        "description": (
            "Почти свободный запуск argv в корне проекта (не интерактивный терминал). "
            "Только если включено в настройках → Проект. Всегда подтверждение. "
            "Лимит 3 запуска за ход. Без shell-метасимволов (|;&`$<>) и без cmd / powershell -Command. "
            "Предпочитай run_tests и run_allowed; run_shell — только если нет подходящего пресета или id. "
            "Передай argv (массив) или command (строка → shlex). "
            "detach=true — запуск без ожидания (GUI), иначе ждём до таймаута."
        ),
        "parameters": _schema(
            {
                "summary": _string("Короткая причина запуска без секретов."),
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Аргументы команды. Если задан — приоритетнее command.",
                },
                "command": _string("Строка команды; разбирается shlex, если argv нет."),
                "detach": {
                    "type": "boolean",
                    "description": "true — запустить и отпустить (GUI); false/пусто — ждать вывод.",
                },
            },
            ["summary"],
        ),
    },
    {
        "name": "git",
        "description": "Git в корне проекта. action=status|diff|log|commit. Нет push/reset/shell. commit только после подтверждения; для staging укажи paths или add_all=true.",
        "parameters": _schema(
            {
                "action": _string("status, diff, log или commit."),
                "path": _string("Для diff: путь файла (необязательно)."),
                "staged": {"type": "boolean", "description": "Для diff: показывать staged."},
                "limit": {"type": "integer", "description": "Для log: число коммитов, до 20."},
                "message": _string("Для commit: сообщение."),
                "paths": _string("Для commit: пути через пробел/запятую для git add."),
                "add_all": {"type": "boolean", "description": "Для commit: git add -A."},
                "summary": _string("Короткая причина commit без секретов."),
            },
            ["action"],
        ),
    },
    {
        "name": "list_mcp_tools",
        "description": "Список инструментов подключённых MCP-серверов.",
        "parameters": _schema({}, []),
    },
    {
        "name": "call_mcp_tool",
        "description": "Вызов инструмента MCP. Секреты в аргументы не подставляются.",
        "parameters": _schema(
            {
                "server": _string("Имя сервера из настроек."),
                "tool": _string("Имя инструмента."),
                "arguments": {"type": "object", "description": "Аргументы инструмента."},
            },
            ["server", "tool"],
        ),
    },
    {
        "name": "browser",
        "description": (
            "Браузер через MCP Playwright (не встроенный Chrome). "
            "Нужен сервер playwright в настройках → MCP. "
            "Типичный цикл: navigate → snapshot → click/type по ref из снимка."
        ),
        "parameters": _schema(
            {
                "action": _string(
                    "navigate|snapshot|click|type|fill|press|hover|select|tabs|back|close|screenshot|wait|…"
                ),
                "url": _string("Для navigate."),
                "ref": _string("ref элемента из snapshot."),
                "element": _string("Краткое описание элемента (для click/type)."),
                "text": _string("Текст для type/wait."),
                "key": _string("Клавиша для press."),
                "tabs_action": _string("Для tabs: list|new|close|select."),
                "index": {"type": "number", "description": "Индекс вкладки для tabs select."},
                "fields": {"type": "array", "description": "Для fill: поля формы."},
                "submit": {"type": "boolean", "description": "Для type: отправить Enter."},
                "time": {"type": "number", "description": "Для wait: секунды."},
            },
            ["action"],
        ),
    },
]

ASK_TOOL_NAMES = frozenset(
    {
        "list_dir",
        "read_file",
        "search",
        "view_image",
        "web_search",
        "project_index",
        "git",
        "list_mcp_tools",
    }
)
# Plan — те же read-only инструменты, что Ask (без записи и run_tests).
PLAN_TOOL_NAMES = ASK_TOOL_NAMES
AGENT_MODES = ("agent", "ask", "plan", "debug")
WRITE_MODES = frozenset({"agent", "debug"})
READONLY_MODES = frozenset({"ask", "plan"})


def normalize_agent_mode(raw) -> str:
    value = str(raw or "agent").strip().lower()
    return value if value in AGENT_MODES else "agent"


def mode_allows_writes(mode: str) -> bool:
    return normalize_agent_mode(mode) in WRITE_MODES


def tools_for_mode(mode: str, settings: dict | None = None) -> list[dict]:
    mode = normalize_agent_mode(mode)
    if mode in READONLY_MODES:
        names = PLAN_TOOL_NAMES if mode == "plan" else ASK_TOOL_NAMES
        return [spec for spec in TOOL_SPECS if spec["name"] in names]
    specs = list(TOOL_SPECS)
    enabled = bool((settings or {}).get("agent_shell_enabled"))
    if not enabled:
        specs = [spec for spec in specs if spec["name"] != "run_shell"]
    return specs


@dataclass
class ToolOutcome:
    model_text: str
    journal: str
    images: list = field(default_factory=list)


class Toolbox:
    def __init__(self, vault, confirm, mcp, settings) -> None:
        self.root: Path | None = None
        self.vault = vault
        self.confirm = confirm
        self.mcp = mcp
        self.settings = settings
        self.stop = None
        self.image_request = request_image
        self._image_http = None
        self._test_holder: dict = {}
        self._test_runs = 0
        self._shell_runs = 0
        self._last_fail_key = ""
        self._fail_locked = False
        self._touched: list[str] = []
        self.on_before_write = None
        self.on_run_output = None

    def set_root(self, root: Path | None) -> None:
        self.root = root

    def _emit_run(self, event: str, **payload) -> None:
        hook = self.on_run_output
        if hook is None:
            return
        try:
            hook(event, **payload)
        except Exception:
            pass

    def begin_turn(self) -> None:
        self._test_runs = 0
        self._shell_runs = 0
        self._last_fail_key = ""
        self._fail_locked = False
        self._touched = []

    def _snapshot_before_write(self, relative: str, full: Path) -> None:
        hook = self.on_before_write
        if hook is None:
            return
        try:
            hook(relative, full)
        except Exception:
            pass

    @property
    def fail_locked(self) -> bool:
        return self._fail_locked

    def touched_paths(self) -> list[str]:
        return list(self._touched)

    def _note_touched(self, relative: str) -> None:
        path = str(relative or "").replace("\\", "/").strip().strip("/")
        if path and path not in self._touched:
            self._touched.append(path)

    def _touched_suffix(self) -> str:
        if not self._touched:
            return ""
        return "\nТронутые за ход: " + ", ".join(self._touched)

    def _fail_context(self, output: str, code: int | None) -> str:
        parts: list[str] = []
        if self._touched:
            parts.append("Этот ход тронул: " + ", ".join(self._touched))
        brief = fail_brief(output, code)
        if brief:
            parts.append("Суть падения:\n" + brief)
        if not parts:
            return ""
        return "\n" + "\n".join(parts)

    def cancel(self) -> None:
        process = self._test_holder.get("process")
        if process is not None:
            from project_agent.testing import kill_process

            kill_process(process)

    def execute(self, name: str, arguments) -> ToolOutcome:
        try:
            settings = self.settings() or {}
            mode = normalize_agent_mode(settings.get("agent_mode"))
            allowed = {spec["name"] for spec in tools_for_mode(mode, settings)}
            if mode in READONLY_MODES and name not in allowed:
                if mode == "plan":
                    outcome = ToolOutcome(
                        f"Режим Plan: инструмент «{name}» недоступен. Сначала план без записи; "
                        "переключитесь в Agent и напишите «делай», чтобы выполнять.",
                        f"{name}: запрещено в Plan",
                    )
                else:
                    outcome = ToolOutcome(
                        f"Режим Ask: инструмент «{name}» недоступен. Переключитесь в Agent, чтобы менять проект.",
                        f"{name}: запрещено в Ask",
                    )
            else:
                arguments = _arguments(arguments)
                handler = getattr(self, f"_tool_{name}", None)
                if handler is None:
                    outcome = ToolOutcome(f"Неизвестный инструмент: {name}", f"{name}: неизвестный инструмент")
                else:
                    outcome = handler(arguments)
        except Exception as exc:
            message = self._scrub(str(exc))[:300]
            outcome = ToolOutcome(f"Ошибка {name}: {message}", f"{name}: ошибка")
        outcome.model_text = self._clip_tool_text(self._scrub(outcome.model_text))
        outcome.journal = " ".join(self._scrub(outcome.journal).split())[:300]
        return outcome

    def _clip_tool_text(self, text: str) -> str:
        body = text or ""
        if len(body) <= MAX_TOOL_RESULT_CHARS:
            return body
        head = MAX_TOOL_RESULT_CHARS // 2
        tail = MAX_TOOL_RESULT_CHARS - head - 20
        return body[:head].rstrip() + "\n… обрезано …\n" + body[-tail:].lstrip()

    def _scrub(self, text: str, path: Path | None = None) -> str:
        scrubber = Scrubber(self.vault, literals_from_settings(self.settings() or {}))
        return scrubber(text, path)

    def _require_root(self) -> Path:
        if self.root is None:
            raise PathError("папка проекта не выбрана")
        return self.root

    def _inside(self, raw: str) -> Path:
        return resolve_inside(self._require_root(), raw)

    def _stopped(self) -> bool:
        return bool(self.stop is not None and self.stop.is_set())

    def _tool_list_dir(self, args: dict) -> ToolOutcome:
        full = self._inside(str(args.get("path") or "."))
        if not full.exists():
            raise PathError("папка не найдена")
        if not full.is_dir():
            raise PathError("это файл, не папка")
        names = []
        with os.scandir(full) as scan:
            for entry in scan:
                if entry.name in IGNORE_DIRS:
                    continue
                suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
                names.append(entry.name + suffix)
        names.sort(key=lambda item: (not item.endswith("/"), item.lower()))
        truncated = len(names) > MAX_LIST_DIR
        shown = names[:MAX_LIST_DIR]
        rel = relative_posix(self.root, full)
        body = "\n".join(shown) if shown else "(пусто)"
        if truncated:
            body += f"\n(список обрезан, показаны {MAX_LIST_DIR} из {len(names)})"
        return ToolOutcome(f"{rel}\n{body}", f"list_dir {rel}: {len(names)} элементов")

    def _tool_read_file(self, args: dict) -> ToolOutcome:
        full = self._inside(str(args.get("path") or ""))
        rel = relative_posix(self.root, full)
        if not full.exists() or not full.is_file():
            raise PathError("файл не найден")
        if is_image_name(full):
            return ToolOutcome(
                "Файл пропущен: это изображение. Используй view_image.",
                f"read_file {rel}: пропущен, изображение",
            )
        if is_binary_name(full):
            return ToolOutcome("Файл пропущен: бинарный.", f"read_file {rel}: пропущен")
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            return ToolOutcome("Файл пропущен: слишком большой.", f"read_file {rel}: пропущен, большой")
        data = full.read_bytes()
        if b"\x00" in data[:8192]:
            return ToolOutcome("Файл пропущен: бинарный.", f"read_file {rel}: пропущен")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ToolOutcome("Файл пропущен: не текст.", f"read_file {rel}: пропущен")
        if is_secret_blob(full) or ("-----BEGIN" in text and "PRIVATE KEY-----" in text):
            token = self.vault.take(text)
            return ToolOutcome(
                f"Содержимое скрыто ({token}). Скопируй метку без изменений, чтобы сохранить файл.",
                f"read_file {rel}: содержимое скрыто",
            )
        redacted = self._scrub(text, full)
        lines = redacted.splitlines()
        offset = _int(args.get("offset"), 1)
        limit = min(MAX_READ_LINES, _int(args.get("limit"), DEFAULT_READ_LINES))
        if offset < 1:
            offset = 1
        if limit < 1:
            limit = 1
        chunk = lines[offset - 1 : offset - 1 + limit]
        if not chunk:
            return ToolOutcome("В этом диапазоне строк нет.", f"read_file {rel}: пустой диапазон")
        end = offset + len(chunk) - 1
        numbered = "\n".join(f"{number}|{line}" for number, line in enumerate(chunk, offset))
        note = ""
        if is_env_file(full) or is_json_secret_file(full):
            note = "\nСекреты заменены метками."
        return ToolOutcome(
            f"{rel}, строки {offset}-{end} из {len(lines)}{note}\n{numbered}",
            f"read_file {rel}: строки {offset}-{end}",
        )

    def _tool_search(self, args: dict) -> ToolOutcome:
        query = str(args.get("query") or "")
        if not query.strip():
            raise ValueError("пустой запрос")
        pattern = str(args.get("glob") or "")
        root = self._require_root()
        from project_agent.findfiles import search_project

        hits = search_project(root, query, glob=pattern, limit=MAX_MATCHES, max_scan=MAX_SCAN_FILES)
        lines = []
        for hit in hits:
            full = root / hit.path
            hidden = self._scrub(hit.text, full if full.is_file() else root)
            lines.append(f"{hit.path}:{hit.line}: {hidden[:160]}")
        body = "\n".join(lines) if lines else "Совпадений нет."
        if len(hits) >= MAX_MATCHES:
            body += f"\n(список обрезан, максимум {MAX_MATCHES})"
        return ToolOutcome(body, f"search: {len(hits)} совпадений")

    def _tool_write_file(self, args: dict) -> ToolOutcome:
        full = self._inside(str(args.get("path") or ""))
        rel = relative_posix(self.root, full)
        if full.exists() and full.is_dir():
            raise PathError("это папка, не файл")
        raw = args.get("content")
        if not isinstance(raw, str):
            raise ValueError("content должен быть строкой")
        if len(raw) > MAX_WRITE_CHARS:
            raise ValueError("содержимое слишком большое")
        content = self.vault.restore(raw)
        before = ""
        if full.exists() and full.is_file():
            try:
                before = full.read_text(encoding="utf-8-sig")
            except OSError:
                before = ""
        hidden = self.vault.count(raw)
        if before:
            summary = self._summary(args.get("summary"), f"Запись, правка файла", hidden)
        else:
            summary = self._summary(args.get("summary"), f"Запись, новый файл, {content.count(chr(10)) + 1} строк", hidden)
        detail = preview_unified(self._scrub(before, full), self._scrub(content, full), rel)
        if self._stopped() or not self._confirm_project_write(rel, summary, detail):
            return ToolOutcome(
                "Пользователь отказался записывать файл. Не повторяй эту запись без новой причины.",
                f"write_file {rel}: отказ",
            )
        self._snapshot_before_write(rel, full)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        try:
            touch_file(self.root, rel)
        except Exception:
            pass
        self._note_touched(rel)
        return ToolOutcome(
            "Файл записан. Содержимое в ответ не входит." + self._touched_suffix(),
            f"write_file {rel}: записано",
        )

    def _tool_apply_patch(self, args: dict) -> ToolOutcome:
        full = self._inside(str(args.get("path") or ""))
        rel = relative_posix(self.root, full)
        if not full.exists() or not full.is_file():
            raise PathError("файл не найден")
        if is_binary_name(full) or is_secret_blob(full):
            raise PathError("этот файл нельзя править патчем")
        data = full.read_bytes()
        if b"\x00" in data[:8192]:
            raise PathError("бинарный файл")
        try:
            original = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PathError("файл не в UTF-8")
        diff = args.get("diff")
        if not isinstance(diff, str) or not diff.strip():
            raise ValueError("пустой diff")
        updated = apply_diff(original, self.vault.restore(diff))
        if updated == original:
            return ToolOutcome("Изменений нет.", f"apply_patch {rel}: без изменений")
        if len(updated) > MAX_WRITE_CHARS:
            raise ValueError("результат слишком большой")
        plus, minus = _diff_stat(original, updated)
        hidden = self.vault.count(diff)
        summary = self._summary(args.get("summary"), f"Правка +{plus} -{minus}", hidden)
        detail = preview_unified(self._scrub(original, full), self._scrub(updated, full), rel)
        if self._stopped() or not self._confirm_project_write(rel, summary, detail):
            return ToolOutcome(
                "Пользователь отказался применять правку. Не повторяй её без новой причины.",
                f"apply_patch {rel}: отказ",
            )
        self._snapshot_before_write(rel, full)
        full.write_text(updated, encoding="utf-8", newline="\n")
        try:
            touch_file(self.root, rel)
        except Exception:
            pass
        self._note_touched(rel)
        return ToolOutcome(
            "Правка записана. Содержимое в ответ не входит." + self._touched_suffix(),
            f"apply_patch {rel}: записано",
        )

    def _tool_view_image(self, args: dict) -> ToolOutcome:
        full = self._inside(str(args.get("path") or ""))
        rel = relative_posix(self.root, full)
        if not full.exists() or not full.is_file():
            raise PathError("файл не найден")
        if full.suffix.lower() not in IMAGE_EXTENSIONS:
            raise PathError("это не изображение")
        image = prepare_image(full)
        text = f"Изображение {rel}, {image['width']}x{image['height']}. Оно приложено."
        return ToolOutcome(text, f"view_image {rel}: {image['width']}x{image['height']}", [image])

    def _tool_web_search(self, args: dict) -> ToolOutcome:
        query = self._public_text(str(args.get("query") or ""))
        if not query.strip():
            raise ValueError("в запросе не осталось текста после удаления секретов")
        try:
            results = web_search(query, limit=5)
        except Exception as exc:
            raise ValueError(f"поиск не выполнен: {exc}") from exc
        if not results:
            return ToolOutcome("Поиск не дал результатов.", "web_search: 0 результатов")
        lines = []
        for item in results:
            snippet = self._scrub(item.get("snippet") or "")[:240]
            title = self._scrub(item.get("title") or "")
            lines.append(f"{title}\n{item.get('url')}\n{snippet}".strip())
        return ToolOutcome("\n\n".join(lines), f"web_search: {len(results)} результатов")

    def _tool_generate_image(self, args: dict) -> ToolOutcome:
        settings = self.settings() or {}
        model = (settings.get("image_model") or "").strip()
        if not model:
            raise ValueError("не задана модель изображений")
        provider = settings.get("provider")
        base = (settings.get("image_base_url") or "").strip()
        if not base:
            if provider != "openai":
                raise ValueError("для этого провайдера укажите URL изображений")
            base = settings.get("base_url") or ""
        key = (settings.get("image_api_key") or "").strip() or (settings.get("api_key") or "").strip()
        prompt = self._public_text(str(args.get("prompt") or ""))
        if not prompt.strip():
            raise ValueError("в описании не осталось текста после удаления секретов")
        requested = self._inside(str(args.get("path") or "image.png"))
        if requested.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            requested = resolve_inside(self.root, relative_posix(self.root, requested.with_suffix(".png")))
        rel = relative_posix(self.root, requested)
        if self._stopped() or not self.confirm(rel, "Сгенерировать изображение и сохранить файл."):
            return ToolOutcome(
                "Пользователь отказался генерировать изображение.",
                f"generate_image {rel}: отказ",
            )
        if self._stopped():
            return ToolOutcome("Остановлено.", f"generate_image {rel}: остановлено")
        size = str(args.get("size") or "").strip().lower().replace(" ", "")
        if size and not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
            size = ""
        raw = self.image_request(base, key, model, prompt[:1000], size=size or None)
        if len(raw) > 15_000_000:
            raise ValueError("ответ генерации слишком большой")
        final = resolve_inside(self.root, relative_posix(self.root, requested.with_suffix(_image_suffix(raw))))
        rel = relative_posix(self.root, final)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(raw)
        return ToolOutcome(f"Изображение сохранено: {rel}", f"generate_image {rel}: сохранено")

    def _tool_project_index(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        action = str(args.get("action") or "").strip().lower()
        if action in {"summary", "status"}:
            data = load_index(root)
            if not data.get("built_at"):
                data = build_index(root)
            text = index_summary(root)
            if data.get("truncated"):
                text += f" Лимит индекса: больше {len(data.get('files') or [])} файлов не взято."
            return ToolOutcome(text, "project_index: summary")
        if action in {"refresh", "rebuild", "update"}:
            data = build_index(root)
            text = index_summary(root)
            return ToolOutcome(text, f"project_index: refresh {len(data.get('files') or [])}")
        if action == "find":
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolOutcome("Для find нужна подстрока query.", "project_index: find пусто")
            found, data = find_paths(root, query)
            if not found:
                return ToolOutcome(
                    f"По «{query}» в индексе ничего нет. Файлов в индексе: {len(data.get('files') or [])}.",
                    "project_index: find 0",
                )
            body = "\n".join(found)
            if len(found) >= 80:
                body += "\n(список обрезан)"
            return ToolOutcome(body, f"project_index: find {len(found)}")
        if action in {"find_symbol", "symbol", "symbols"}:
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolOutcome("Для find_symbol нужна подстрока query.", "project_index: find_symbol пусто")
            found, data = find_symbols(root, query)
            if not found:
                file_count = len(data.get("files") or {})
                return ToolOutcome(
                    f"По символу «{query}» ничего нет. Проиндексировано исходников: {file_count}.",
                    "project_index: find_symbol 0",
                )
            body = "\n".join(found)
            if len(found) >= 80:
                body += "\n(список обрезан)"
            return ToolOutcome(body, f"project_index: find_symbol {len(found)}")
        if action == "imports":
            path = str(args.get("path") or args.get("query") or "").strip()
            if not path:
                return ToolOutcome(
                    "Для imports нужен path к .py/.js/.ts/….",
                    "project_index: imports пусто",
                )
            found, data = list_imports(root, path)
            if path not in (data.get("files") or {}) and not found:
                return ToolOutcome(
                    f"Файл «{path}» не в символьном индексе (нужен .py/.js/.ts/… ≤1 МБ). Обновите refresh.",
                    "project_index: imports нет",
                )
            if not found:
                return ToolOutcome(f"В «{path}» импортов не найдено.", "project_index: imports 0")
            body = "\n".join(found)
            if len(found) >= 80:
                body += "\n(список обрезан)"
            return ToolOutcome(body, f"project_index: imports {len(found)}")
        if action in {"importers", "who_imports"}:
            query = str(args.get("query") or "").strip()
            if not query:
                return ToolOutcome("Для importers нужна подстрока query (модуль/имя).", "project_index: importers пусто")
            found, data = find_importers(root, query)
            if not found:
                return ToolOutcome(
                    f"Импортеров «{query}» не найдено. Исходников в индексе: {len(data.get('files') or {})}.",
                    "project_index: importers 0",
                )
            body = "\n".join(found)
            if len(found) >= 80:
                body += "\n(список обрезан)"
            return ToolOutcome(body, f"project_index: importers {len(found)}")
        return ToolOutcome(
            "action: summary, refresh, find, find_symbol, imports или importers.",
            "project_index: неизвестно",
        )

    def _tool_run_tests(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        settings = self.settings() or {}
        preset = normalize_preset(settings.get("test_preset"))
        if not preset:
            return ToolOutcome(
                "Пресет тестов выключен. Включите unittest, pytest или npm test в настройках → Проект.",
                "run_tests: выключено",
            )
        rounds = normalize_fix_rounds(settings.get("test_fix_rounds"))
        if self._fail_locked:
            return ToolOutcome(
                "СТОП: повторный тот же FAIL уже зафиксирован. Не вызывай run_tests/run_allowed/run_shell и другие инструменты; "
                "кратко опиши проблему и перечисли тронутые файлы."
                + self._touched_suffix(),
                "run_tests: стоп повтор",
            )
        if self._test_runs >= rounds:
            return ToolOutcome(
                f"Лимит запусков тестов за этот ход ({rounds}). Кратко опиши, что осталось, и остановись."
                + self._touched_suffix(),
                f"run_tests: лимит {rounds}",
            )
        timeout = normalize_timeout(settings.get("test_timeout"))
        label = LABEL_BY_PRESET.get(preset, preset)
        summary = self._summary(
            args.get("summary"),
            f"Запуск тестов ({label}), {self._test_runs + 1}/{rounds}, таймаут {timeout} с",
            0,
        )
        if self._stopped() or not self.confirm("тесты", summary):
            return ToolOutcome(
                "Пользователь отказался запускать тесты. Не повторяй запуск без новой причины.",
                "run_tests: отказ",
            )
        if self._stopped():
            return ToolOutcome("Остановлено.", "run_tests: остановлено")
        title = f"run_tests · {label}"
        try:
            command = preset_command(preset)
        except ValueError as exc:
            return ToolOutcome(str(exc), "run_tests: ошибка")
        self._emit_run("start", title=title, command=format_command(command))
        try:
            result = run_preset(
                root,
                preset,
                timeout,
                self.stop,
                self._test_holder,
                on_output=lambda chunk: self._emit_run("chunk", text=chunk),
            )
        except ValueError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(str(exc), "run_tests: ошибка")
        if result.stopped or self._stopped():
            self._emit_run("end", title=title, status="остановлено")
            return ToolOutcome("Запуск тестов остановлен.", "run_tests: остановлено")
        self._test_runs += 1
        if result.timed_out:
            body = (
                f"Таймаут {timeout} с. Запуск {self._test_runs}/{rounds}.\n"
                f"Команда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status=f"таймаут {self._test_runs}/{rounds}")
            return ToolOutcome(body + self._touched_suffix(), f"run_tests {label}: таймаут {self._test_runs}/{rounds}")
        code = 0 if result.code is None else int(result.code)
        status = "OK" if code == 0 else f"FAIL ({code})"
        if code != 0:
            fail_key = f"tests:{fail_fingerprint(result.output, code)}"
            if fail_key and fail_key == self._last_fail_key:
                self._fail_locked = True
                body = (
                    f"СТОП: {status} — то же падение, что в предыдущем запуске. "
                    f"Запуск {self._test_runs}/{rounds}. Больше не вызывай инструменты; "
                    f"опиши проблему человеку и перечисли тронутые файлы.\n"
                    f"Команда: {format_command(result.command)}\n\n{result.output}"
                ).strip()
                self._emit_run("end", title=title, status=f"повтор {status} {self._test_runs}/{rounds}")
                return ToolOutcome(
                    body + self._fail_context(result.output, code),
                    f"run_tests {label}: повтор {self._test_runs}/{rounds}",
                )
            self._last_fail_key = fail_key
            body = (
                f"{status}. Запуск {self._test_runs}/{rounds}.\n"
                f"Команда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status=f"{status} {self._test_runs}/{rounds}")
            return ToolOutcome(
                body + self._fail_context(result.output, code),
                f"run_tests {label}: {status} {self._test_runs}/{rounds}",
            )
        self._last_fail_key = ""
        body = (
            f"{status}. Запуск {self._test_runs}/{rounds}.\n"
            f"Команда: {format_command(result.command)}\n\n{result.output}"
        ).strip()
        self._emit_run("end", title=title, status=f"{status} {self._test_runs}/{rounds}")
        return ToolOutcome(
            body + self._touched_suffix(),
            f"run_tests {label}: {status} {self._test_runs}/{rounds}",
        )

    def _tool_run_allowed(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        settings = self.settings() or {}
        custom = normalize_allowed_commands(settings.get("allowed_commands"))
        command_id = str(args.get("id") or "").strip()
        timeout = normalize_timeout(settings.get("test_timeout"))
        if self._fail_locked:
            return ToolOutcome(
                "СТОП: повторный тот же FAIL уже зафиксирован. Не вызывай run_tests/run_allowed/run_shell и другие инструменты; "
                "кратко опиши проблему и перечисли тронутые файлы."
                + self._touched_suffix(),
                "run_allowed: стоп повтор",
            )
        try:
            resolved_id, command, detach = resolve_allowed(root, command_id, custom)
        except ValueError as exc:
            known = ", ".join(list_allowed_ids(custom))
            return ToolOutcome(
                f"{exc}. Доступные id: {known}." if "allowlist" not in str(exc).lower() else str(exc),
                f"run_allowed: ошибка",
            )
        if detach:
            summary = self._summary(
                args.get("summary"),
                f"Allowlist «{resolved_id}» (detach, без ожидания)",
                0,
            )
        else:
            summary = self._summary(
                args.get("summary"),
                f"Allowlist «{resolved_id}», таймаут {timeout} с",
                0,
            )
        detail = format_command(command)
        if detach:
            detail = f"[detach]\n{detail}"
        if self._stopped() or not self.confirm(f"run_allowed:{resolved_id}", summary, detail):
            return ToolOutcome(
                "Пользователь отказался запускать команду. Не повторяй без новой причины.",
                f"run_allowed {resolved_id}: отказ",
            )
        if self._stopped():
            return ToolOutcome("Остановлено.", f"run_allowed {resolved_id}: остановлено")
        title = f"run_allowed · {resolved_id}"
        if detach:
            return self._run_detached(title, root, command, f"run_allowed {resolved_id}")
        self._emit_run("start", title=title, command=format_command(command))
        try:
            result = run_argv(
                root,
                command,
                timeout,
                self.stop,
                self._test_holder,
                on_output=lambda chunk: self._emit_run("chunk", text=chunk),
            )
        except ValueError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(str(exc), f"run_allowed {resolved_id}: ошибка")
        except OSError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(f"Не удалось запустить: {exc}", f"run_allowed {resolved_id}: ошибка")
        if result.stopped or self._stopped():
            self._emit_run("end", title=title, status="остановлено")
            return ToolOutcome("Запуск остановлен.", f"run_allowed {resolved_id}: остановлено")
        if result.timed_out:
            body = (
                f"Таймаут {timeout} с.\nКоманда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status="таймаут")
            return ToolOutcome(body + self._touched_suffix(), f"run_allowed {resolved_id}: таймаут")
        code = 0 if result.code is None else int(result.code)
        status = "OK" if code == 0 else f"FAIL ({code})"
        if code != 0:
            fail_key = f"allowed:{resolved_id}:{fail_fingerprint(result.output, code)}"
            if fail_key and fail_key == self._last_fail_key:
                self._fail_locked = True
                body = (
                    f"СТОП: {status} — то же падение allowlist «{resolved_id}», что в предыдущем запуске. "
                    f"Больше не вызывай инструменты; опиши проблему человеку и перечисли тронутые файлы.\n"
                    f"Команда: {format_command(result.command)}\n\n{result.output}"
                ).strip()
                self._emit_run("end", title=title, status=f"повтор {status}")
                return ToolOutcome(
                    body + self._fail_context(result.output, code),
                    f"run_allowed {resolved_id}: повтор",
                )
            self._last_fail_key = fail_key
            body = (
                f"{status}.\nКоманда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status=status)
            return ToolOutcome(
                body + self._fail_context(result.output, code),
                f"run_allowed {resolved_id}: {status}",
            )
        self._last_fail_key = ""
        body = (
            f"{status}.\nКоманда: {format_command(result.command)}\n\n{result.output}"
        ).strip()
        self._emit_run("end", title=title, status=status)
        return ToolOutcome(body + self._touched_suffix(), f"run_allowed {resolved_id}: {status}")

    def _tool_run_shell(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        settings = self.settings() or {}
        mode = normalize_agent_mode(settings.get("agent_mode"))
        if mode in READONLY_MODES:
            label = "Plan" if mode == "plan" else "Ask"
            return ToolOutcome(
                f"Режим {label}: run_shell недоступен.",
                f"run_shell: запрещено в {label}",
            )
        if not bool(settings.get("agent_shell_enabled")):
            return ToolOutcome(
                "run_shell выключен. Включите «Почти свободный shell» в настройках → Проект.",
                "run_shell: выключено",
            )
        if self._fail_locked:
            return ToolOutcome(
                "СТОП: повторный тот же FAIL уже зафиксирован. Не вызывай run_tests/run_allowed/run_shell и другие инструменты; "
                "кратко опиши проблему и перечисли тронутые файлы."
                + self._touched_suffix(),
                "run_shell: стоп повтор",
            )
        if self._shell_runs >= SHELL_MAX_PER_TURN:
            return ToolOutcome(
                f"Лимит запусков run_shell за этот ход ({SHELL_MAX_PER_TURN}). "
                "Кратко опиши, что осталось, и остановись."
                + self._touched_suffix(),
                f"run_shell: лимит {SHELL_MAX_PER_TURN}",
            )
        timeout = normalize_timeout(settings.get("test_timeout"))
        detach = _flag_bool(args.get("detach"))
        try:
            command = resolve_shell_argv(args)
        except ValueError as exc:
            return ToolOutcome(str(exc), "run_shell: ошибка")
        rendered = format_command(command)
        if detach:
            summary = self._summary(args.get("summary"), "Shell detach (без ожидания)", 0)
            detail = f"[detach]\n{rendered}"
        else:
            summary = self._summary(args.get("summary"), f"Shell, таймаут {timeout} с", 0)
            detail = rendered
        if self._stopped() or not self.confirm("run_shell", summary, detail):
            return ToolOutcome(
                "Пользователь отказался запускать команду. Не повторяй без новой причины.",
                "run_shell: отказ",
            )
        if self._stopped():
            return ToolOutcome("Остановлено.", "run_shell: остановлено")
        title = "run_shell"
        if detach:
            outcome = self._run_detached(title, root, command, "run_shell")
            if outcome.journal.endswith(": ошибка"):
                return outcome
            self._shell_runs += 1
            mark = f"{self._shell_runs}/{SHELL_MAX_PER_TURN}"
            outcome.model_text = (outcome.model_text + f"\nЗапуск {mark}.").strip()
            outcome.journal = f"run_shell: detach {mark}"
            return outcome
        self._emit_run("start", title=title, command=rendered)
        try:
            result = run_argv(
                root,
                command,
                timeout,
                self.stop,
                self._test_holder,
                on_output=lambda chunk: self._emit_run("chunk", text=chunk),
            )
        except ValueError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(str(exc), "run_shell: ошибка")
        except OSError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(f"Не удалось запустить: {exc}", "run_shell: ошибка")
        if result.stopped or self._stopped():
            self._emit_run("end", title=title, status="остановлено")
            return ToolOutcome("Запуск остановлен.", "run_shell: остановлено")
        self._shell_runs += 1
        if result.timed_out:
            body = (
                f"Таймаут {timeout} с. Запуск {self._shell_runs}/{SHELL_MAX_PER_TURN}.\n"
                f"Команда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status=f"таймаут {self._shell_runs}/{SHELL_MAX_PER_TURN}")
            return ToolOutcome(
                body + self._touched_suffix(),
                f"run_shell: таймаут {self._shell_runs}/{SHELL_MAX_PER_TURN}",
            )
        code = 0 if result.code is None else int(result.code)
        status = "OK" if code == 0 else f"FAIL ({code})"
        if code != 0:
            fail_key = f"shell:{fail_fingerprint(result.output, code)}"
            if fail_key and fail_key == self._last_fail_key:
                self._fail_locked = True
                body = (
                    f"СТОП: {status} — то же падение shell, что в предыдущем запуске. "
                    f"Запуск {self._shell_runs}/{SHELL_MAX_PER_TURN}. Больше не вызывай инструменты; "
                    f"опиши проблему человеку и перечисли тронутые файлы.\n"
                    f"Команда: {format_command(result.command)}\n\n{result.output}"
                ).strip()
                self._emit_run(
                    "end",
                    title=title,
                    status=f"повтор {status} {self._shell_runs}/{SHELL_MAX_PER_TURN}",
                )
                return ToolOutcome(
                    body + self._fail_context(result.output, code),
                    f"run_shell: повтор {self._shell_runs}/{SHELL_MAX_PER_TURN}",
                )
            self._last_fail_key = fail_key
            body = (
                f"{status}. Запуск {self._shell_runs}/{SHELL_MAX_PER_TURN}.\n"
                f"Команда: {format_command(result.command)}\n\n{result.output}"
            ).strip()
            self._emit_run("end", title=title, status=f"{status} {self._shell_runs}/{SHELL_MAX_PER_TURN}")
            return ToolOutcome(
                body + self._fail_context(result.output, code),
                f"run_shell: {status} {self._shell_runs}/{SHELL_MAX_PER_TURN}",
            )
        self._last_fail_key = ""
        body = (
            f"{status}. Запуск {self._shell_runs}/{SHELL_MAX_PER_TURN}.\n"
            f"Команда: {format_command(result.command)}\n\n{result.output}"
        ).strip()
        self._emit_run("end", title=title, status=f"{status} {self._shell_runs}/{SHELL_MAX_PER_TURN}")
        return ToolOutcome(
            body + self._touched_suffix(),
            f"run_shell: {status} {self._shell_runs}/{SHELL_MAX_PER_TURN}",
        )

    def _run_detached(self, title: str, root, command, journal_prefix: str) -> ToolOutcome:
        rendered = format_command(command)
        self._emit_run("start", title=title, command=f"[detach] {rendered}")
        try:
            result = spawn_detached(root, command)
        except ValueError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(str(exc), f"{journal_prefix}: ошибка")
        except OSError as exc:
            self._emit_run("end", title=title, status="ошибка")
            return ToolOutcome(f"Не удалось запустить: {exc}", f"{journal_prefix}: ошибка")
        body = (
            f"OK (detach).\nКоманда: {format_command(result.command)}\n\n{result.output}"
        ).strip()
        self._emit_run("end", title=title, status=f"detach pid={result.pid}")
        return ToolOutcome(body + self._touched_suffix(), f"{journal_prefix}: detach pid={result.pid}")

    def _tool_git(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        action = str(args.get("action") or "").strip().lower()
        mode = normalize_agent_mode((self.settings() or {}).get("agent_mode"))
        if action == "commit" and mode in READONLY_MODES:
            if mode == "plan":
                return ToolOutcome(
                    "Режим Plan: git commit недоступен. Переключитесь в Agent.",
                    "git commit: запрещено в Plan",
                )
            return ToolOutcome(
                "Режим Ask: git commit недоступен. Переключитесь в Agent.",
                "git commit: запрещено в Ask",
            )
        if action == "status":
            try:
                result = git_status(root)
            except GitError as exc:
                return ToolOutcome(str(exc), "git status: ошибка")
            body = result.output or "(чисто)"
            return ToolOutcome(self._scrub(body), f"git status: code {result.code}")
        if action == "diff":
            path = str(args.get("path") or "").strip() or None
            staged = bool(args.get("staged"))
            try:
                result = git_diff(root, path=path, staged=staged)
            except (GitError, PathError) as exc:
                return ToolOutcome(str(exc), "git diff: ошибка")
            body = result.output or "(нет изменений)"
            return ToolOutcome(self._scrub(body), f"git diff: code {result.code}")
        if action == "log":
            try:
                result = git_log(root, args.get("limit"))
            except GitError as exc:
                return ToolOutcome(str(exc), "git log: ошибка")
            body = result.output or "(пусто)"
            return ToolOutcome(self._scrub(body), f"git log: code {result.code}")
        if action == "commit":
            message = self._public_text(str(args.get("message") or "")).strip()
            if not message:
                return ToolOutcome("Нужен message для commit.", "git commit: нет message")
            add_all = bool(args.get("add_all"))
            paths = parse_commit_paths(args.get("paths"))
            if not add_all and not paths:
                return ToolOutcome(
                    "Укажи paths (файлы для git add) или add_all=true. Push нет.",
                    "git commit: нет staging",
                )
            try:
                status = git_status(root)
                preview = preview_commit_diff(root, paths, add_all)
            except (GitError, PathError) as exc:
                return ToolOutcome(str(exc), "git commit: ошибка")
            summary = self._summary(args.get("summary"), f"git commit: {message[:80]}", 0)
            detail_parts = [
                f"message:\n{message}",
                f"stage: {'add -A' if add_all else ', '.join(paths)}",
                "status:\n" + (status.output or "(чисто)"),
            ]
            if preview:
                detail_parts.append("diff (будет в commit):\n" + preview)
            detail = self._scrub("\n\n".join(detail_parts))
            if self._stopped() or not self._confirm("git commit", summary, detail):
                return ToolOutcome(
                    "Пользователь отказался от commit. Не повторяй без новой причины.",
                    "git commit: отказ",
                )
            if self._stopped():
                return ToolOutcome("Остановлено.", "git commit: остановлено")
            try:
                result = git_commit(root, message, paths=paths, add_all=add_all)
            except (GitError, PathError) as exc:
                return ToolOutcome(str(exc), "git commit: ошибка")
            body = result.output or "Commit создан."
            return ToolOutcome(self._scrub(body), "git commit: ok")
        return ToolOutcome("action: status, diff, log или commit.", "git: неизвестно")

    def _tool_list_mcp_tools(self, args: dict) -> ToolOutcome:
        text = self.mcp.list_tools()
        count = 0 if text.startswith("MCP-серверы не настроены") else len([line for line in text.splitlines() if line.strip()])
        return ToolOutcome(text, f"mcp: список, {count}")

    def _tool_call_mcp_tool(self, args: dict) -> ToolOutcome:
        server = str(args.get("server") or "").strip()
        tool = str(args.get("tool") or "").strip()
        if not server or not tool:
            raise ValueError("нужны server и tool")
        arguments = args.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments) if arguments.strip() else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments должен быть объектом")
        hidden_args = _scrub_json(arguments, lambda value: self._scrub(value))
        try:
            result = self.mcp.call(server, tool, hidden_args)
        except McpError as exc:
            return ToolOutcome(f"Ошибка MCP. {exc}", f"mcp {server}.{tool}: ошибка")
        text, images = _mcp_result(result)
        journal = f"mcp {server}.{tool}: выполнено"
        if isinstance(result, dict) and result.get("isError"):
            journal = f"mcp {server}.{tool}: ошибка"
            text = "Ошибка MCP. " + text
        return ToolOutcome(text[:30_000], journal, images)

    def _tool_browser(self, args: dict) -> ToolOutcome:
        settings = self.settings() or {}
        server = find_browser_server_name(settings.get("mcp_servers") or [])
        if not server:
            return ToolOutcome(
                "Браузер MCP не настроен. В настройках → MCP нажмите «Браузер Playwright» "
                "(нужен Node.js / npx), сохраните настройки и повторите.",
                "browser: нет MCP",
            )
        action = str(args.get("action") or "").strip()
        try:
            tool, payload = map_browser_arguments(action, args)
        except ValueError as exc:
            return ToolOutcome(str(exc), "browser: неизвестный action")
        hidden = _scrub_json(payload, lambda value: self._scrub(value))
        try:
            result = self.mcp.call(server, tool, hidden, timeout=BROWSER_CALL_TIMEOUT)
        except McpError as exc:
            return ToolOutcome(
                f"Ошибка браузера MCP ({server}.{tool}): {exc}",
                f"browser {action}: ошибка",
            )
        text, images = _mcp_result(result)
        journal = f"browser {action}: ok"
        if isinstance(result, dict) and result.get("isError"):
            journal = f"browser {action}: ошибка"
            text = "Ошибка браузера. " + text
        return ToolOutcome(text[:30_000], journal, images)

    def _auto_write_project(self) -> bool:
        settings = self.settings() or {}
        raw = settings.get("auto_write_project")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")

    def _confirm_project_write(self, path: str, summary: str, detail: str = "") -> bool:
        if self._auto_write_project():
            return True
        return self._confirm(path, summary, detail)

    def _confirm(self, path: str, summary: str, detail: str = "") -> bool:
        try:
            return bool(self.confirm(path, summary, detail))
        except TypeError:
            body = summary if not detail else f"{summary}\n\n{detail[:4000]}"
            return bool(self.confirm(path, body))

    def _summary(self, given, fallback: str, hidden: int) -> str:
        text = str(given or "").strip() or fallback
        text = self._scrub(text)[:200]
        if hidden:
            text += f". Локально будут восстановлены секреты: {hidden}."
        return text

    def _public_text(self, text: str) -> str:
        scrubber = Scrubber(self.vault, literals_from_settings(self.settings() or {}))
        return " ".join(scrubber.strip_outbound(text).split())


def _arguments(arguments) -> dict:
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments.strip() else {}
    if not isinstance(arguments, dict):
        raise ValueError("аргументы должны быть объектом")
    return arguments


def _int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _glob_match(relative: str, pattern: str) -> bool:
    if pattern in {"", "*"}:
        return True
    if "/" not in pattern and "\\" not in pattern:
        return fnmatch.fnmatch(Path(relative).name, pattern)
    return fnmatch.fnmatch(relative, pattern.replace("\\", "/"))


def _diff_stat(before: str, after: str) -> tuple[int, int]:
    import difflib

    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
    plus = minus = 0
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
    return plus, minus


def _image_suffix(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return ".png"
    if raw.startswith(b"\xff\xd8"):
        return ".jpg"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return ".webp"
    return ".png"


def _scrub_json(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_scrub_json(item, redact) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_json(item, redact) for key, item in value.items()}
    return value


def _mcp_result(result: dict) -> tuple[str, list]:
    if not isinstance(result, dict):
        return str(result), []
    parts = []
    images = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif block.get("type") == "image":
            encoded = block.get("data") or ""
            try:
                import base64

                raw = base64.b64decode(encoded)
                images.append(prepare_image_bytes(raw))
                parts.append("Изображение MCP приложено.")
            except Exception:
                parts.append("Изображение MCP пропущено.")
    if not parts and "value" in result:
        parts.append(str(result["value"]))
    return "\n".join(parts) or "MCP не вернул текст.", images
