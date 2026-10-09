from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_FAIL_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*s\b", re.IGNORECASE)
_FAIL_RAN = re.compile(r"^Ran\s+\d+\s+tests?\s+in\s+.+$", re.IGNORECASE)
_FAIL_PASSED = re.compile(r"\d+\s+passed(?:\s+in\s+\S+)?", re.IGNORECASE)

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
MAX_OUTPUT_CHARS = 24_000
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


@dataclass
class DetachRun:
    command: list[str] | str
    pid: int
    output: str


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


def _stabilize_fail_line(raw: str) -> str:
    line = " ".join((raw or "").strip().split()).replace("\\", "/")
    if not line or line.startswith("====") or line.startswith("----"):
        return ""
    if _FAIL_RAN.match(line):
        return ""
    line = _FAIL_DURATION.sub("Xs", line)
    line = _FAIL_PASSED.sub("N passed", line)
    return line[:240]


def fail_fingerprint(output: str, code: int | None = None) -> str:
    """Стабильный ключ падения: FAIL/ERROR/AssertionError, без шума длительностей/путей."""
    lines = (output or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hits: list[str] = []
    for raw in lines:
        line = _stabilize_fail_line(raw)
        if not line:
            continue
        upper = line.upper()
        if (
            upper.startswith("FAIL")
            or upper.startswith("ERROR")
            or upper.startswith("FILE ")
            or "FAILED" in upper
            or "ASSERTIONERROR" in upper
            or "ERROR:" in upper
            or line.startswith("E ")
        ):
            hits.append(line)
            if len(hits) >= 12:
                break
    if not hits:
        for raw in lines[-16:]:
            line = _stabilize_fail_line(raw)
            if line:
                hits.append(line[:200])
            if len(hits) >= 12:
                break
    return f"{0 if code is None else int(code)}:" + "\n".join(hits)


def fail_brief(output: str, code: int | None = None, limit: int = 3) -> str:
    """Короткая суть падения для модели (1–3 строки из fingerprint)."""
    key = fail_fingerprint(output, code)
    lines = key.splitlines()
    if not lines:
        return ""
    first = lines[0]
    if ":" in first:
        first = first.split(":", 1)[1].strip()
    body: list[str] = []
    if first:
        body.append(first)
    body.extend(lines[1:])
    return "\n".join(body[: max(1, int(limit))])


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


def _scrub_run_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "API_KEY", "PROJECTAGENT_API_KEY"):
        env.pop(key, None)
    return env


def spawn_detached(root: Path, command: list[str] | str) -> DetachRun:
    """Запуск без ожидания и без kill по таймауту (GUI / долгие процессы)."""
    if isinstance(command, str):
        if not command.strip():
            raise ValueError("пустая команда")
    elif not command:
        raise ValueError("пустой argv")
    kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": _scrub_run_env(),
        "close_fds": True,
    }
    if os.name == "nt":
        # Не CREATE_NO_WINDOW — GUI должно открыться. Отрыв от родителя.
        flags = 0
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = flags
        kwargs["close_fds"] = False  # на Windows с PIPE/redirects иначе нельзя; DEVNULL ок с False
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    pid = int(process.pid or 0)
    # Не ждём: процесс отсоединён. Сброс returncode глушит ResourceWarning при GC.
    try:
        process.poll()
    except OSError:
        pass
    if process.returncode is None:
        process.returncode = 0
    return DetachRun(
        command=command,
        pid=pid,
        output=f"Запущено без ожидания (detach), pid={pid}. Процесс не убивается по таймауту хода.",
    )


def run_argv(
    root: Path,
    command: list[str] | str,
    timeout: int,
    stop: threading.Event | None = None,
    holder: dict | None = None,
    on_output: Callable[[str], None] | None = None,
) -> TestRun:
    timeout = normalize_timeout(timeout)
    env = _scrub_run_env()
    process = subprocess.Popen(
        command,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    if holder is not None:
        holder["process"] = process
    timed_out = False
    stopped = False
    chunks: list[str] = []
    reader_done = threading.Event()

    def _emit(piece: str) -> None:
        if not piece or on_output is None:
            return
        try:
            on_output(piece)
        except Exception:
            pass

    def _read_stdout() -> None:
        stream = process.stdout
        try:
            if stream is None:
                return
            while True:
                line = stream.readline()
                if line == "":
                    break
                chunks.append(line)
                _emit(line)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            reader_done.set()

    reader = threading.Thread(target=_read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while not reader_done.wait(0.15):
            if stop is not None and stop.is_set():
                stopped = True
                kill_process(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                kill_process(process)
                break
        reader_done.wait(5)
        reader.join(timeout=2)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill_process(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
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
        output=clip_output("".join(chunks)),
        timed_out=timed_out,
        stopped=stopped,
    )


def run_preset(
    root: Path,
    preset: str,
    timeout: int,
    stop: threading.Event | None = None,
    holder: dict | None = None,
    on_output: Callable[[str], None] | None = None,
) -> TestRun:
    return run_argv(root, preset_command(preset), timeout, stop, holder, on_output)
