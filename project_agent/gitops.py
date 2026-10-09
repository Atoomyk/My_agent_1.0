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


def parse_status_head(output: str) -> tuple[str, int, int]:
    """Ветка, число изменённых tracked, число untracked из `git status --short --branch`."""
    branch = "—"
    modified = 0
    untracked = 0
    for raw in (output or "").splitlines():
        line = raw.rstrip()
        if line.startswith("##"):
            part = line[2:].strip().split("...")[0].strip()
            branch = part.split()[0] if part else "—"
            continue
        if len(line) < 2:
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return branch, modified, untracked


def _unquote_git_path(raw: str) -> str:
    text = (raw or "").strip().replace("\\", "/")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text.strip().strip("/")


def _classify_xy(code: str) -> str:
    """XY из status --short → modified|untracked|deleted."""
    xy = (code or "  ")[:2]
    if xy == "??":
        return "untracked"
    if "D" in xy and "R" not in xy and "C" not in xy and "A" not in xy:
        return "deleted"
    if "A" in xy and "D" not in xy:
        return "untracked"
    return "modified"


def parse_status_paths(output: str) -> dict[str, str]:
    """Пути из `git status --short` → modified|untracked|deleted (posix rel)."""
    found: dict[str, str] = {}
    for raw in (output or "").splitlines():
        line = raw.rstrip()
        if not line or line.startswith("##") or len(line) < 2:
            continue
        code = line[:2]
        rest = line[3:] if len(line) > 2 and line[2] == " " else line[2:].lstrip()
        if " -> " in rest:
            left, right = rest.split(" -> ", 1)
            old = _unquote_git_path(left)
            new = _unquote_git_path(right)
            if old:
                found[old] = "deleted"
            if new:
                found[new] = "untracked"
            continue
        was_dir = rest.rstrip().endswith("/")
        path = _unquote_git_path(rest)
        if not path:
            continue
        key = f"{path}/" if was_dir else path
        kind = _classify_xy(code)
        prev = found.get(key)
        rank = {"untracked": 1, "modified": 2, "deleted": 3}
        if prev is None or rank.get(kind, 0) >= rank.get(prev, 0):
            found[key] = kind
    return found


def dirty_ancestor_dirs(paths: dict[str, str]) -> set[str]:
    """Родительские каталоги (posix) для dirty-путей; untracked dir (path/) сам тоже."""
    dirs: set[str] = set()
    for raw in paths:
        original = str(raw or "").replace("\\", "/")
        path = original.strip().strip("/")
        if not path:
            continue
        parts = path.split("/")
        for i in range(len(parts) - 1):
            dirs.add("/".join(parts[: i + 1]))
        if original.rstrip().endswith("/"):
            dirs.add(path)
    return dirs


def git_path_kind(status: dict[str, str], rel: str) -> str:
    """Статус для iid дерева (файл или папка)."""
    path = str(rel or "").replace("\\", "/").strip().strip("/")
    if not path or path == ".":
        return ""
    return status.get(path) or status.get(path + "/") or ""


def format_git_badge(branch: str, modified: int, untracked: int) -> str:
    if modified <= 0 and untracked <= 0:
        dirty = "clean"
    else:
        parts: list[str] = []
        if modified > 0:
            parts.append(f"M{modified}")
        if untracked > 0:
            parts.append(f"U{untracked}")
        dirty = " ".join(parts)
    name = (branch or "—").strip() or "—"
    return f"{name} · {dirty}"


def git_badge(root: Path) -> str:
    """Короткая строка для шапки: `main · M2 U1` или пусто, если не репозиторий."""
    try:
        result = git_status(root)
    except GitError:
        return ""
    branch, modified, untracked = parse_status_head(result.output)
    return format_git_badge(branch, modified, untracked)


def git_diff(root: Path, path: str | None = None, staged: bool = False) -> GitResult:
    args = ["diff", "--no-color"]
    if staged:
        args.append("--staged")
    if path:
        full = resolve_inside(root, path)
        rel = relative_posix(root, full)
        args.extend(["--", rel])
    return run_git(root, args)


def preview_commit_diff(root: Path, paths: list[str] | None, add_all: bool) -> str:
    """Diff того, что уйдёт в commit после add (без изменения индекса)."""
    root = ensure_repo(root)
    if add_all:
        result = run_git(root, ["diff", "HEAD", "--no-color"])
        body = (result.output or "").strip()
        return body or "(нет текстового diff по tracked; смотри status — untracked/бинарные)"
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in list(paths or []):
        text = str(raw or "").strip().replace("\\", "/")
        if not text or text in seen:
            continue
        full = resolve_inside(root, text)
        rel = relative_posix(root, full)
        seen.add(rel)
        cleaned.append(rel)
    if not cleaned:
        return "(нет путей для diff)"
    result = run_git(root, ["diff", "HEAD", "--no-color", "--", *cleaned])
    body = (result.output or "").strip()
    # Untracked: diff HEAD пуст — пометим.
    notes: list[str] = []
    for rel in cleaned:
        path = root / rel
        if path.is_file():
            tracked = run_git(root, ["ls-files", "--", rel])
            if tracked.code == 0 and not (tracked.output or "").strip():
                notes.append(f"new file: {rel}")
    parts: list[str] = []
    if body:
        parts.append(body)
    if notes:
        parts.append("\n".join(notes))
    return "\n\n".join(parts).strip() or "(нет текстового diff)"


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
