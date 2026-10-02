from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

INIT_TIMEOUT = 120
LIST_TOOLS_TIMEOUT = 60
STDERR_LIMIT = 4000
NODE_CLIS = frozenset({"npx", "npm", "pnpm", "yarn"})


class McpError(RuntimeError):
    pass


class _Waiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.message = None
        self.error: Exception | None = None

    def succeed(self, message) -> None:
        if not self.event.is_set():
            self.message = message
        self.event.set()

    def fail(self, error: Exception) -> None:
        # Не затирать уже пойманную причину (например close() → «закрыт»).
        if self.error is None:
            self.error = error
        self.event.set()


class McpClient:
    def __init__(self, spec: dict, cwd: Path | None) -> None:
        self.spec = spec
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()
        self._pending: dict[int, _Waiter] = {}
        self._next_id = 1
        self._closed = False
        self._stderr = bytearray()
        self._stdout_noise = bytearray()
        self._tools: list[dict] | None = None
        self._argv_preview = ""
        self._last_death: str | None = None

    def start(self) -> None:
        command = self.spec["command"]
        args = list(self.spec.get("args") or [])
        argv = build_argv(command, args)
        self._argv_preview = argv if isinstance(argv, str) else subprocess.list2cmdline(list(argv))
        env = os.environ.copy()
        env.update(self.spec.get("env") or {})
        flags = _creation_flags(argv)
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                bufsize=0,
                creationflags=flags,
            )
        except OSError as exc:
            raise McpError(f"не удалось запустить сервер: {exc}") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        try:
            self.rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ProjectAgent", "version": "1.0"},
                },
                timeout=INIT_TIMEOUT,
            )
        except McpError as exc:
            detail = self._normalize_death(str(exc))
            self.close()
            raise McpError(detail) from exc
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict]:
        if self._tools is not None:
            return self._tools
        tools: list[dict] = []
        params: dict = {}
        for _ in range(10):
            result = self.rpc("tools/list", params, timeout=LIST_TOOLS_TIMEOUT)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
            params = {"cursor": cursor}
        self._tools = tools
        return tools

    def call(self, name: str, arguments: dict, timeout: float = 60) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        if self._closed or self.proc is None:
            raise McpError(self._last_death or "MCP-сервер закрыт")
        request_id = self._next_id
        self._next_id += 1
        waiter = _Waiter()
        self._pending[request_id] = waiter
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        if not waiter.event.wait(timeout):
            self._pending.pop(request_id, None)
            code = None if self.proc is None else self.proc.poll()
            if code is not None:
                raise McpError(self._death_detail(f"таймаут MCP (процесс завершился, code={code})"))
            raise McpError(self._death_detail("таймаут MCP"))
        self._pending.pop(request_id, None)
        if waiter.error:
            raise McpError(self._normalize_death(str(waiter.error)))
        message = waiter.message or {}
        if "error" in message:
            error = message["error"]
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError(text or "ошибка MCP")
        result = message.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def close(self) -> None:
        self._closed = True
        proc = self.proc
        if proc is None:
            self._fail_all(McpError(self._last_death or "MCP-сервер закрыт"))
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Сначала добрать stderr, потом закрывать трубы — иначе drain обрывается.
        self._wait_stderr(0.6)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass
        self._fail_all(McpError(self._last_death or self._death_detail("MCP-сервер завершился")))

    def stderr_tail(self) -> str:
        err = self._stderr.decode("utf-8", "replace")[-STDERR_LIMIT:].strip()
        out = self._stdout_noise.decode("utf-8", "replace")[-STDERR_LIMIT:].strip()
        if err and out:
            return f"{err}\nstdout: {out}"
        if out and not err:
            return f"stdout: {out}"
        return err

    def _wait_stderr(self, seconds: float = 0.5) -> None:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            time.sleep(0.05)
            if self.proc is not None and self.proc.poll() is not None:
                time.sleep(0.15)
                return

    def _death_detail(self, prefix: str) -> str:
        self._wait_stderr()
        code = None if self.proc is None else self.proc.poll()
        tail = self.stderr_tail()
        text = (prefix or "").strip()
        if text.startswith("MCP-сервер закрыт"):
            text = "MCP-сервер завершился" + text[len("MCP-сервер закрыт") :]
        parts = [text.rstrip(" |")]
        if code is not None and f"code={code}" not in parts[0]:
            parts.append(f"code={code}")
        if tail and f"stderr: {tail}" not in parts[0] and f"stdout: {tail}" not in parts[0]:
            parts.append(f"stderr: {tail}" if not tail.startswith("stdout:") else tail)
        elif not tail and self._argv_preview and "argv:" not in parts[0]:
            parts.append(f"argv: {self._argv_preview[:240]}")
        detail = " | ".join(part for part in parts if part)
        self._last_death = detail
        return detail

    def _normalize_death(self, text: str) -> str:
        return self._death_detail(text or "MCP-сервер завершился")

    def _fail_all(self, error: Exception) -> None:
        for waiter in list(self._pending.values()):
            waiter.fail(error)
        self._pending.clear()

    def _write(self, payload: dict) -> None:
        """MCP stdio: одна JSON-RPC строка + \\n (не Content-Length/LSP)."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpError("нет канала к MCP")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if b"\n" in data:
            raise McpError("MCP-сообщение содержит перевод строки")
        with self._write_lock:
            proc.stdin.write(data + b"\n")
            proc.stdin.flush()

    def _reader(self) -> None:
        try:
            while not self._closed:
                message = self._read_one()
                if message is None:
                    self._fail_all(McpError(self._death_detail("MCP-сервер завершился")))
                    return
                if "method" in message and "id" in message:
                    self._reply(message)
                elif "id" in message:
                    waiter = self._pending.get(message["id"])
                    if waiter:
                        waiter.succeed(message)
        except Exception as exc:
            self._fail_all(exc)

    def _reply(self, message: dict) -> None:
        if message.get("method") == "ping":
            body = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "unsupported"},
            }
        try:
            self._write(body)
        except Exception:
            return

    def _read_one(self):
        proc = self.proc
        if proc is None or proc.stdout is None:
            return None
        while True:
            line = proc.stdout.readline()
            if not line:
                return None
            # Совместимость со старыми серверами на Content-Length.
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
                while True:
                    header = proc.stdout.readline()
                    if header in (b"\r\n", b"\n", b""):
                        break
                body = proc.stdout.read(length)
                if len(body) < length:
                    return None
                return json.loads(body.decode("utf-8"))
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(b"{"):
                try:
                    return json.loads(stripped.decode("utf-8"))
                except json.JSONDecodeError:
                    self._note_stdout(stripped)
                    continue
            self._note_stdout(stripped)

    def _note_stdout(self, chunk: bytes) -> None:
        self._stdout_noise.extend(chunk)
        self._stdout_noise.extend(b"\n")
        if len(self._stdout_noise) > STDERR_LIMIT:
            del self._stdout_noise[:-STDERR_LIMIT]

    def _drain(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.read(1024)
                if not chunk:
                    return
                self._stderr.extend(chunk)
                if len(self._stderr) > STDERR_LIMIT:
                    del self._stderr[:-STDERR_LIMIT]
        except Exception:
            return


def resolve_node_cli(command: str) -> str:
    name = str(command or "").strip()
    if not name:
        return name
    if os.name == "nt" and name.lower() in NODE_CLIS:
        found = shutil.which(f"{name.lower()}.cmd") or shutil.which(name)
        if found:
            return found
    return shutil.which(name) or name


def resolve_node_exe() -> str | None:
    if os.name == "nt":
        return shutil.which("node.exe") or shutil.which("node")
    return shutil.which("node")


def find_playwright_mcp_cli() -> str | None:
    """Путь к cli.js глобального/локального @playwright/mcp (обход npx.cmd на Windows)."""
    homes: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        homes.append(Path(appdata) / "npm" / "node_modules" / "@playwright" / "mcp" / "cli.js")
    try:
        npm = resolve_node_cli("npm")
        if os.name == "nt" and npm.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            argv: list[str] | str = [comspec, "/d", "/c", "call", npm, "root", "-g"]
        else:
            argv = [npm, "root", "-g"]
        root = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if root.returncode == 0 and root.stdout.strip():
            homes.append(Path(root.stdout.strip()) / "@playwright" / "mcp" / "cli.js")
    except Exception:
        pass
    for path in homes:
        if path.is_file():
            return str(path)
    return None


def _playwright_package_args(args: list[str]) -> tuple[bool, list[str]]:
    """True, если args — запуск @playwright/mcp; вернуть хвост аргументов после пакета."""
    for index, item in enumerate(args):
        token = str(item)
        if token.startswith("@playwright/mcp"):
            return True, [str(x) for x in args[index + 1 :]]
    return False, []


def build_argv(command: str, args: list[str] | None = None) -> list[str] | str:
    """Собрать argv для MCP.

    На Windows npx→@playwright/mcp по возможности через node cli.js (иначе cmd ломает stdio).
    Остальные npx/npm — список [cmd, /d, /c, call, exe, ...], не одна строка.
    """
    args = list(args or [])
    name = str(command or "").strip()
    lowered = name.lower()
    is_playwright, tail = _playwright_package_args(args)
    if is_playwright and (lowered in NODE_CLIS or lowered.endswith("npx.cmd") or lowered == "npx"):
        node = resolve_node_exe()
        cli = find_playwright_mcp_cli()
        if node and cli:
            return [node, cli, *tail]
    if os.name == "nt" and lowered in NODE_CLIS:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        exe = resolve_node_cli(name)
        # Список, не list2cmdline-строка: так надёжнее наследуют PIPE.
        return [comspec, "/d", "/c", "call", exe, *args]
    if os.name == "nt" and (lowered.endswith(".cmd") or lowered.endswith(".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/c", "call", name, *args]
    resolved = resolve_node_exe() if lowered == "node" else None
    return [resolved or name, *args]


def _creation_flags(argv: list[str] | str) -> int:
    """Скрывать окно только для node.exe; .cmd/npx под CREATE_NO_WINDOW часто мёртвые."""
    if os.name != "nt":
        return 0
    if isinstance(argv, str):
        return 0
    if not argv:
        return 0
    head = Path(str(argv[0])).name.lower()
    if head in {"node.exe", "node"}:
        return subprocess.CREATE_NO_WINDOW
    return 0


class McpHub:
    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}
        self._specs: list[dict] = []
        self._cwd: Path | None = None
        self._lock = threading.Lock()

    def set_project(self, root: Path | None) -> None:
        with self._lock:
            self._cwd = root
            self._close_unlocked()

    def set_servers(self, servers: list[dict]) -> None:
        with self._lock:
            servers = list(servers or [])
            if servers == self._specs:
                return
            self._close_unlocked()
            self._specs = servers

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def list_tools(self) -> str:
        with self._lock:
            specs = list(self._specs)
        if not specs:
            return "MCP-серверы не настроены."
        lines = []
        for spec in specs:
            name = spec.get("name") or "server"
            try:
                tools = self._client(spec).list_tools()
            except Exception as exc:
                lines.append(f"{name}: ошибка ({exc})")
                continue
            if not tools:
                lines.append(f"{name}: инструментов нет")
            for tool in tools:
                description = " ".join(str(tool.get("description") or "").split())[:200]
                lines.append(f"{name}/{tool.get('name')}: {description}".rstrip())
        text = "\n".join(lines)
        return text[:20_000]

    def call(self, server: str, tool: str, arguments: dict, timeout: float | None = None) -> dict:
        spec = self._spec(server)
        if spec is None:
            raise McpError(f"MCP-сервер не найден: {server}")
        return self._client(spec).call(tool, arguments, timeout=60 if timeout is None else timeout)

    def _spec(self, name: str) -> dict | None:
        with self._lock:
            for spec in self._specs:
                if spec.get("name") == name:
                    return spec
        return None

    def _client(self, spec: dict) -> McpClient:
        name = spec["name"]
        with self._lock:
            client = self._clients.get(name)
            if client is not None and client.proc is not None and client.proc.poll() is None and not client._closed:
                return client
            client = McpClient(spec, self._cwd)
            self._clients[name] = client
        client.start()
        return client
