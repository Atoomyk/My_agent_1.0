from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

PRESETS = {
    "unittest": "unittest",
    "pytest": "pytest",
    "npm": "npm",
}
PRESET_LABELS = {
    "Выключено": "",
    "unittest": "unittest",
    "pytest": "pytest",
    "npm test": "npm",
}
LABEL_BY_PRESET = {value: key for key, value in PRESET_LABELS.items()}
MAX_OUTPUT_CHARS = 32_000
DEFAULT_TIMEOUT = 120
MIN_TIMEOUT = 15
MAX_TIMEOUT = 600
DEFAULT_FIX_ROUNDS = 3
MIN_FIX_ROUNDS = 1
MAX_FIX_ROUNDS = 10


@dataclass
class TestRun:
    command: list[str] | str
    code: int | None
    output: str
    timed_out: bool
    stopped: bool


def format_command(command: list[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(command)


def normalize_preset(raw) -> str:
    value = str(raw or "").strip().lower()
    return value if value in PRESETS else ""


def normalize_timeout(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, value))


def normalize_fix_rounds(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FIX_ROUNDS
    return min(MAX_FIX_ROUNDS, max(MIN_FIX_ROUNDS, value))


def clip_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n…\n" + text[-half:]


def fail_fingerprint(output: str, code: int | None = None) -> str:
    """Стабильный ключ падения: FAIL/ERROR/AssertionError, без шума разделителей."""
    lines = (output or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hits: list[str] = []
    for raw in lines:
        line = " ".join(raw.strip().split())
        if not line or line.startswith("====") or line.startswith("----"):
            continue
        upper = line.upper()
        if (
            upper.startswith("FAIL")
            or upper.startswith("ERROR")
            or "FAILED" in upper
            or "ASSERTIONERROR" in upper
            or "ERROR:" in upper
            or line.startswith("E ")
        ):
            hits.append(line[:240])
            if len(hits) >= 12:
                break
    if not hits:
        hits = [" ".join(item.strip().split())[:200] for item in lines[-12:] if item.strip()]
    return f"{0 if code is None else int(code)}:" + "\n".join(hits)


def python_command() -> list[str]:
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    for name in ("python", "py"):
        found = shutil.which(name)
        if not found:
            continue
        if name == "py":
            return [found, "-3"]
        return [found]
    raise ValueError("для exe нужен Python в PATH (python или py)")


def resolve_npm() -> str:
    if os.name == "nt":
        found = shutil.which("npm.cmd") or shutil.which("npm")
    else:
        found = shutil.which("npm")
    if not found:
        raise ValueError("для пресета npm test нужен npm в PATH")
    return found


def npm_test_command() -> list[str] | str:
    found = resolve_npm()
    if os.name == "nt":
        # .cmd нельзя через CreateProcess (193). Нужен cmd /c.
        # Важно: одна строка целиком. Если передать list, Popen снова
        # вызовет list2cmdline и экранирует кавычки в пути с пробелами.
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return (
            subprocess.list2cmdline([comspec, "/d", "/c"])
            + " "
            + subprocess.list2cmdline(["call", found, "test"])
        )
    return [found, "test"]


def preset_command(preset: str) -> list[str] | str:
    preset = normalize_preset(preset)
    if not preset:
        raise ValueError("пресет тестов выключен в настройках")
    if preset == "unittest":
        return [*python_command(), "-m", "unittest", "discover", "-s", "tests", "-v"]
    if preset == "pytest":
        return [*python_command(), "-m", "pytest", "-q"]
    if preset == "npm":
        return npm_test_command()
    raise ValueError("неизвестный пресет тестов")


def kill_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        process.kill()
    except OSError:
        return


def run_argv(
    root: Path,
    command: list[str] | str,
    timeout: int,
    stop: threading.Event | None = None,
    holder: dict | None = None,
) -> TestRun:
    timeout = normalize_timeout(timeout)
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "API_KEY", "PROJECTAGENT_API_KEY"):
        env.pop(key, None)
    process = subprocess.Popen(
        command,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if holder is not None:
        holder["process"] = process
    timed_out = False
    stopped = False
    try:
        try:
            output, _err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process(process)
            output, _err = process.communicate(timeout=5)
    finally:
        if holder is not None:
            holder.pop("process", None)
    if stop is not None and stop.is_set():
        stopped = True
        kill_process(process)
    code = process.poll()
    return TestRun(
        command=command,
        code=code,
        output=clip_output(output or ""),
        timed_out=timed_out,
        stopped=stopped,
    )


def run_preset(
    root: Path,
    preset: str,
    timeout: int,
    stop: threading.Event | None = None,
    holder: dict | None = None,
) -> TestRun:
    return run_argv(root, preset_command(preset), timeout, stop, holder)
