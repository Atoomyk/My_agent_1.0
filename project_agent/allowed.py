from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

from project_agent.testing import npm_test_command, python_command

BUILTIN_IDS = ("unittest", "pytest", "npm_test", "build_ps1")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_BAD_CHARS = re.compile(r"[|;&`$<>\n\r]")


def project_python(root: Path) -> list[str]:
    root = Path(root)
    if os.name == "nt":
        venv = root / ".venv" / "Scripts" / "python.exe"
    else:
        venv = root / ".venv" / "bin" / "python"
    if venv.is_file():
        return [str(venv)]
    return python_command()


def build_ps1_command(root: Path) -> list[str]:
    root = Path(root)
    script = (root / "build.ps1").resolve()
    try:
        script.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("build.ps1 вне корня проекта") from exc
    if not script.is_file():
        raise ValueError("в корне проекта нет build.ps1")
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        raise ValueError("нужен powershell или pwsh в PATH")
    return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]


def builtin_command(root: Path, command_id: str) -> list[str] | str:
    command_id = str(command_id or "").strip().lower()
    if command_id == "unittest":
        return [*project_python(root), "-m", "unittest", "discover", "-s", "tests", "-v"]
    if command_id == "pytest":
        return [*project_python(root), "-m", "pytest"]
    if command_id == "npm_test":
        return npm_test_command()
    if command_id == "build_ps1":
        return build_ps1_command(root)
    raise ValueError(f"неизвестный встроенный id: {command_id}")


def normalize_command_id(raw) -> str:
    value = str(raw or "").strip().lower()
    if not _ID_RE.match(value):
        return ""
    return value


def validate_argv(argv: list[str]) -> list[str]:
    if not argv:
        raise ValueError("пустой argv")
    cleaned: list[str] = []
    for item in argv:
        text = str(item)
        if not text or text.strip() != text:
            raise ValueError("пустой или с пробелами по краям аргумент")
        if _BAD_CHARS.search(text):
            raise ValueError("запрещённые символы в аргументе")
        cleaned.append(text)
    head = cleaned[0]
    lower = Path(head).name.lower()
    if lower in ("cmd", "cmd.exe", "command.com"):
        raise ValueError("cmd запрещён")
    if lower in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        flags = {part.lower() for part in cleaned[1:]}
        if "-command" in flags or "/command" in flags or "-c" in flags or "/c" in flags:
            raise ValueError("powershell -Command запрещён")
        if "-file" not in flags and "/file" not in flags:
            raise ValueError("для powershell нужен -File")
    return cleaned


# совместимость со старым именем
_validate_argv = validate_argv


def _strip_win_quotes(parts: list[str]) -> list[str]:
    """На Windows shlex(posix=False) оставляет кавычки в токенах."""
    if os.name != "nt":
        return parts
    cleaned: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] == '"':
            cleaned.append(part[1:-1].replace('\\"', '"'))
        else:
            cleaned.append(part)
    return cleaned


def resolve_shell_argv(args: dict | None) -> list[str]:
    """argv: list или command: str → shlex + validate_argv."""
    payload = args if isinstance(args, dict) else {}
    argv_raw = payload.get("argv")
    if isinstance(argv_raw, list) and len(argv_raw) > 0:
        return validate_argv([str(part) for part in argv_raw])
    command = str(payload.get("command") or "").strip()
    if not command:
        raise ValueError("нужен argv или command")
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"не разобрать command: {exc}") from exc
    return validate_argv(_strip_win_quotes(parts))


def _flag_detach(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    value = str(raw or "").strip().lower()
    return value in ("1", "true", "yes", "on", "detach")


def normalize_allowed_commands(raw) -> list[dict]:
    items: list[dict] = []
    if not isinstance(raw, list):
        return items
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        command_id = normalize_command_id(entry.get("id"))
        if not command_id or command_id in BUILTIN_IDS or command_id in seen:
            continue
        argv_raw = entry.get("argv")
        if not isinstance(argv_raw, list):
            continue
        try:
            argv = _validate_argv([str(part) for part in argv_raw])
        except ValueError:
            continue
        seen.add(command_id)
        items.append({"id": command_id, "argv": argv, "detach": _flag_detach(entry.get("detach"))})
    return items


def parse_allowed_text(text: str) -> list[dict]:
    """Строки вида: id: arg1 arg2 … или id!: arg1 … (! = detach)."""
    items: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"ожидалось «id: argv» или «id!: argv», получено: {line}")
        left, right = line.split(":", 1)
        left = left.strip()
        detach = False
        if left.endswith("!"):
            detach = True
            left = left[:-1].rstrip()
        command_id = normalize_command_id(left)
        if not command_id:
            raise ValueError(f"некорректный id: {left.strip()}")
        if command_id in BUILTIN_IDS:
            raise ValueError(f"id «{command_id}» зарезервирован")
        try:
            argv = shlex.split(right.strip(), posix=os.name != "nt")
        except ValueError as exc:
            raise ValueError(f"не разобрать argv для {command_id}: {exc}") from exc
        argv = _validate_argv(_strip_win_quotes(argv))
        items.append({"id": command_id, "argv": argv, "detach": detach})
    # dedupe keeping last
    by_id = {item["id"]: item for item in items}
    return list(by_id.values())


def _fmt_arg(part: str) -> str:
    if re.search(r'[\s"]', part):
        return '"' + part.replace('"', '\\"') + '"'
    return part


def format_allowed_text(commands: list[dict]) -> str:
    lines = []
    for item in normalize_allowed_commands(commands):
        argv = " ".join(_fmt_arg(part) for part in item["argv"])
        mark = "!" if item.get("detach") else ""
        lines.append(f"{item['id']}{mark}: {argv}")
    return "\n".join(lines)


def list_allowed_ids(custom: list[dict] | None = None) -> list[str]:
    ids = list(BUILTIN_IDS)
    for item in normalize_allowed_commands(custom or []):
        ids.append(item["id"])
    return ids


def resolve_allowed(
    root: Path, command_id: str, custom: list[dict] | None = None
) -> tuple[str, list[str] | str, bool]:
    command_id = normalize_command_id(command_id)
    if not command_id:
        raise ValueError("нужен id команды из allowlist")
    if command_id in BUILTIN_IDS:
        return command_id, builtin_command(root, command_id), False
    for item in normalize_allowed_commands(custom or []):
        if item["id"] == command_id:
            return command_id, list(item["argv"]), bool(item.get("detach"))
    known = ", ".join(list_allowed_ids(custom))
    raise ValueError(f"id «{command_id}» не в allowlist. Доступно: {known}")
