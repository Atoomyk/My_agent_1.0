from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from project_agent.paths import relative_posix, resolve_inside

MAX_GIT_OUTPUT = 24_000
DEFAULT_TIMEOUT = 45
MAX_LOG = 20
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class GitResult:
    command: list[str]
    code: int
    output: str


class GitError(ValueError):
    pass


def clip_git(text: str, limit: int = MAX_GIT_OUTPUT) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n…\n" + text[-half:]


def find_git() -> str:
    found = shutil.which("git")
    if not found:
        raise GitError("git не найден в PATH")
    return found


def ensure_repo(root: Path) -> Path:
    root = Path(root).resolve()
    probe = root / ".git"
    if not probe.exists():
        raise GitError("в корне проекта нет git-репозитория (.git)")
    return root


def run_git(root: Path, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> GitResult:
    root = ensure_repo(root)
    git = find_git()
    command = [git, *args]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    kwargs = {
        "cwd": str(root),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "timeout": timeout,
    }
    if CREATE_NO_WINDOW:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"таймаут git ({timeout}с)") from exc
    except OSError as exc:
        raise GitError(f"git не запущен: {exc}") from exc
    output = clip_git((completed.stdout or "") + (completed.stderr or ""))
    return GitResult(command=command, code=int(completed.returncode), output=output.strip())


def git_status(root: Path) -> GitResult:
    return run_git(root, ["status", "--short", "--branch"])


def git_diff(root: Path, path: str | None = None, staged: bool = False) -> GitResult:
    args = ["diff", "--no-color"]
    if staged:
        args.append("--staged")
    if path:
        full = resolve_inside(root, path)
        rel = relative_posix(root, full)
        args.extend(["--", rel])
    return run_git(root, args)


def git_log(root: Path, limit: int = 8) -> GitResult:
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 8
    count = min(MAX_LOG, max(1, count))
    return run_git(
        root,
        ["log", f"-{count}", "--oneline", "--decorate", "--no-color"],
    )


def _stage_paths(root: Path, paths: list[str], add_all: bool) -> list[str]:
    if add_all:
        return ["add", "-A"]
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        text = str(raw or "").strip().replace("\\", "/")
        if not text or text in seen:
            continue
        full = resolve_inside(root, text)
        rel = relative_posix(root, full)
        seen.add(rel)
        cleaned.append(rel)
    if not cleaned:
        raise GitError("для commit укажи paths или add_all=true")
    return ["add", "--", *cleaned]


def git_commit(
    root: Path,
    message: str,
    *,
    paths: list[str] | None = None,
    add_all: bool = False,
) -> GitResult:
    root = ensure_repo(root)
    message = (message or "").strip()
    if not message:
        raise GitError("пустой message")
    if "\x00" in message:
        raise GitError("некорректный message")
    # Одна строка для -m; многострочное через \n допустимо, без shell.
    stage = _stage_paths(root, list(paths or []), bool(add_all))
    staged = run_git(root, stage)
    if staged.code != 0:
        raise GitError(staged.output or "git add не выполнен")
    result = run_git(root, ["commit", "-m", message])
    if result.code != 0:
        raise GitError(result.output or "commit не выполнен")
    return result


def parse_commit_paths(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    text = str(raw).replace(",", " ")
    return [part for part in text.split() if part.strip()]


def preview_unified(before: str, after: str, path: str, limit: int | None = None) -> str:
    import difflib

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=2,
        )
    )
    if not lines:
        return "(нет текстового диффа)"
    if limit is not None and limit > 0 and len(lines) > limit:
        lines = lines[:limit] + ["…"]
    return "\n".join(lines)
