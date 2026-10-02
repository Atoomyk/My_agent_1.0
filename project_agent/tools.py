from __future__ import annotations

import fnmatch
import json
import os
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
from project_agent.testing import (
    LABEL_BY_PRESET,
    fail_fingerprint,
    normalize_fix_rounds,
    normalize_preset,
    normalize_timeout,
    run_preset,
)
from project_agent.index_store import build_index, find_paths, index_summary, load_index, touch_file
from project_agent.gitops import (
    GitError,
    git_commit,
    git_diff,
    git_log,
    git_status,
    parse_commit_paths,
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
DEFAULT_READ_LINES = 200
MAX_MATCHES = 40
MAX_SCAN_FILES = 2000
MAX_WRITE_CHARS = 1_000_000


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


def _string(description: str) -> dict:
    return {"type": "string", "description": description}


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
        "description": "Полная перезапись файла внутри проекта. Выполняется только после подтверждения пользователя. Метки [[SEC:...]] копируй как есть, если секрет нужно сохранить.",
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
        "description": "Правка файла. Предпочтительный diff: блоки <<<<<<< SEARCH / ======= / >>>>>>> REPLACE с точным фрагментом. Также принимается unified diff. Нужно подтверждение пользователя.",
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
        "description": "Сгенерировать изображение и сохранить его в проект после подтверждения. В prompt не должно быть секретов.",
        "parameters": _schema(
            {
                "prompt": _string("Описание изображения."),
                "path": _string("Куда сохранить, относительно корня, например images/pic.png."),
            },
            ["prompt", "path"],
        ),
    },
    {
        "name": "project_index",
        "description": "Индекс путей файлов проекта (не содержимое). action=summary|refresh|find. Для find укажи query — подстрока пути.",
        "parameters": _schema(
            {
                "action": _string("summary, refresh или find."),
                "query": _string("Подстрока пути для find, например app.py или tests/."),
            },
            ["action"],
        ),
    },
    {
        "name": "run_tests",
        "description": "Запустить пресет тестов из настроек (unittest или pytest). Нужно подтверждение. Произвольные команды запрещены.",
        "parameters": _schema(
            {"summary": _string("Короткая причина запуска без секретов.")},
            [],
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
AGENT_MODES = ("agent", "ask")


def normalize_agent_mode(raw) -> str:
    value = str(raw or "agent").strip().lower()
    return value if value in AGENT_MODES else "agent"


def tools_for_mode(mode: str) -> list[dict]:
    mode = normalize_agent_mode(mode)
    if mode != "ask":
        return list(TOOL_SPECS)
    return [spec for spec in TOOL_SPECS if spec["name"] in ASK_TOOL_NAMES]


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
        self._last_fail_key = ""
        self._fail_locked = False
        self._touched: list[str] = []

    def set_root(self, root: Path | None) -> None:
        self.root = root

    def begin_turn(self) -> None:
        self._test_runs = 0
        self._last_fail_key = ""
        self._fail_locked = False
        self._touched = []

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

    def cancel(self) -> None:
        process = self._test_holder.get("process")
        if process is not None:
            from project_agent.testing import kill_process

            kill_process(process)

    def execute(self, name: str, arguments) -> ToolOutcome:
        try:
            mode = normalize_agent_mode((self.settings() or {}).get("agent_mode"))
            if mode == "ask" and name not in ASK_TOOL_NAMES:
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
        outcome.model_text = self._scrub(outcome.model_text)
        outcome.journal = " ".join(self._scrub(outcome.journal).split())[:300]
        return outcome

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
        truncated = len(names) > 500
        shown = names[:500]
        rel = relative_posix(self.root, full)
        body = "\n".join(shown) if shown else "(пусто)"
        if truncated:
            body += "\n(список обрезан)"
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
        matches = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
            for filename in filenames:
                if len(matches) >= MAX_MATCHES or scanned >= MAX_SCAN_FILES:
                    break
                full = Path(dirpath) / filename
                rel = relative_posix(root, full)
                if pattern and not _glob_match(rel, pattern):
                    continue
                if is_binary_name(full) or is_secret_blob(full):
                    continue
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                    data = full.read_bytes()
                except OSError:
                    continue
                if b"\x00" in data[:8192]:
                    continue
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    continue
                scanned += 1
                if "-----BEGIN" in text and "PRIVATE KEY-----" in text:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query not in line:
                        continue
                    hidden = self._scrub(line, full)
                    matches.append(f"{rel}:{number}: {hidden[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        break
        body = "\n".join(matches) if matches else "Совпадений нет."
        if len(matches) >= MAX_MATCHES:
            body += "\n(список обрезан)"
        return ToolOutcome(body, f"search: {len(matches)} совпадений")

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
        if self._stopped() or not self._confirm(rel, summary, detail):
            return ToolOutcome(
                "Пользователь отказался записывать файл. Не повторяй эту запись без новой причины.",
                f"write_file {rel}: отказ",
            )
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
        if self._stopped() or not self._confirm(rel, summary, detail):
            return ToolOutcome(
                "Пользователь отказался применять правку. Не повторяй её без новой причины.",
                f"apply_patch {rel}: отказ",
            )
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
        raw = self.image_request(base, key, model, prompt[:1000])
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
        return ToolOutcome("action: summary, refresh или find.", "project_index: неизвестно")

    def _tool_run_tests(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        settings = self.settings() or {}
        preset = normalize_preset(settings.get("test_preset"))
        if not preset:
            return ToolOutcome(
                "Пресет тестов выключен. Включите unittest или pytest в настройках → Проект.",
                "run_tests: выключено",
            )
        rounds = normalize_fix_rounds(settings.get("test_fix_rounds"))
        if self._fail_locked:
            return ToolOutcome(
                "СТОП: повторный тот же FAIL уже зафиксирован. Не вызывай run_tests и другие инструменты; "
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
        try:
            result = run_preset(root, preset, timeout, self.stop, self._test_holder)
        except ValueError as exc:
            return ToolOutcome(str(exc), "run_tests: ошибка")
        if result.stopped or self._stopped():
            return ToolOutcome("Запуск тестов остановлен.", "run_tests: остановлено")
        self._test_runs += 1
        if result.timed_out:
            body = (
                f"Таймаут {timeout} с. Запуск {self._test_runs}/{rounds}.\n"
                f"Команда: {' '.join(result.command)}\n\n{result.output}"
            ).strip()
            return ToolOutcome(body + self._touched_suffix(), f"run_tests {label}: таймаут {self._test_runs}/{rounds}")
        code = 0 if result.code is None else int(result.code)
        status = "OK" if code == 0 else f"FAIL ({code})"
        if code != 0:
            fail_key = fail_fingerprint(result.output, code)
            if fail_key and fail_key == self._last_fail_key:
                self._fail_locked = True
                body = (
                    f"СТОП: {status} — то же падение, что в предыдущем запуске. "
                    f"Запуск {self._test_runs}/{rounds}. Больше не вызывай инструменты; "
                    f"опиши проблему человеку и перечисли тронутые файлы.\n"
                    f"Команда: {' '.join(result.command)}\n\n{result.output}"
                ).strip()
                return ToolOutcome(
                    body + self._touched_suffix(),
                    f"run_tests {label}: повтор {self._test_runs}/{rounds}",
                )
            self._last_fail_key = fail_key
        else:
            self._last_fail_key = ""
        body = (
            f"{status}. Запуск {self._test_runs}/{rounds}.\n"
            f"Команда: {' '.join(result.command)}\n\n{result.output}"
        ).strip()
        return ToolOutcome(
            body + self._touched_suffix(),
            f"run_tests {label}: {status} {self._test_runs}/{rounds}",
        )

    def _tool_git(self, args: dict) -> ToolOutcome:
        root = self._require_root()
        action = str(args.get("action") or "").strip().lower()
        if action == "commit" and normalize_agent_mode((self.settings() or {}).get("agent_mode")) == "ask":
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
                preview = git_diff(root, staged=False)
            except GitError as exc:
                return ToolOutcome(str(exc), "git commit: ошибка")
            summary = self._summary(args.get("summary"), f"git commit: {message[:80]}", 0)
            detail_parts = [
                f"message:\n{message}",
                f"stage: {'add -A' if add_all else ', '.join(paths)}",
                "status:\n" + (status.output or "(чисто)"),
            ]
            if preview.output:
                detail_parts.append("diff (unstaged, обрезка):\n" + "\n".join(preview.output.splitlines()[:60]))
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
