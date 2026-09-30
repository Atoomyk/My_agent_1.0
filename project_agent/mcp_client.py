from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path


class McpError(RuntimeError):
    pass


class _Waiter:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.message = None
        self.error: Exception | None = None

    def succeed(self, message) -> None:
        self.message = message
        self.event.set()

    def fail(self, error: Exception) -> None:
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
        self._tools: list[dict] | None = None

    def start(self) -> None:
        command = self.spec["command"]
        args = list(self.spec.get("args") or [])
        argv = _argv(command, args)
        env = os.environ.copy()
        env.update(self.spec.get("env") or {})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            raise McpError(f"не удалось запустить сервер: {exc}") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ProjectAgent", "version": "1.0"},
            },
            timeout=20,
        )
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict]:
        if self._tools is not None:
            return self._tools
        tools: list[dict] = []
        params: dict = {}
        for _ in range(10):
            result = self.rpc("tools/list", params, timeout=30)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
            params = {"cursor": cursor}
        self._tools = tools
        return tools

    def call(self, name: str, arguments: dict) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout=60)

    def rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        if self._closed or self.proc is None:
            raise McpError("MCP-сервер закрыт")
        request_id = self._next_id
        self._next_id += 1
        waiter = _Waiter()
        self._pending[request_id] = waiter
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        if not waiter.event.wait(timeout):
            self._pending.pop(request_id, None)
            raise McpError("таймаут MCP")
        self._pending.pop(request_id, None)
        if waiter.error:
            raise McpError(str(waiter.error))
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
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass
        self._fail_all(McpError("MCP-сервер закрыт"))

    def stderr_tail(self) -> str:
        return self._stderr.decode("utf-8", "replace")[-200:]

    def _fail_all(self, error: Exception) -> None:
        for waiter in list(self._pending.values()):
            waiter.fail(error)
        self._pending.clear()

    def _write(self, payload: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpError("нет канала к MCP")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        packet = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
        with self._write_lock:
            proc.stdin.write(packet)
            proc.stdin.flush()

    def _reader(self) -> None:
        try:
            while not self._closed:
                message = self._read_one()
                if message is None:
                    self._fail_all(McpError(self.stderr_tail() or "MCP-сервер завершился"))
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
            if stripped.startswith(b"{"):
                return json.loads(stripped.decode("utf-8"))

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
                if len(self._stderr) > 4000:
                    del self._stderr[:-4000]
        except Exception:
            return


def _argv(command: str, args: list[str]) -> list[str]:
    if os.name == "nt" and command.lower() in {"npx", "npm", "pnpm", "yarn"}:
        return ["cmd", "/c", command, *args]
    return [command, *args]


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

    def call(self, server: str, tool: str, arguments: dict) -> dict:
        spec = self._spec(server)
        if spec is None:
            raise McpError(f"MCP-сервер не найден: {server}")
        return self._client(spec).call(tool, arguments)

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
