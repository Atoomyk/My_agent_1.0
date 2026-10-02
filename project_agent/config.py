from __future__ import annotations

import json
import os
import time
from pathlib import Path

CONFIG_NAME = "config.json"
AI_FIELDS = (
    "provider",
    "base_url",
    "model",
    "api_key",
    "max_steps",
    "image_model",
    "image_base_url",
    "image_api_key",
)


def default_config() -> dict:
    return {
        "provider": "openai",
        "base_url": "",
        "model": "",
        "api_key": "",
        "max_steps": 25,
        "image_model": "",
        "image_base_url": "",
        "image_api_key": "",
        "project_dir": "",
        "theme": "dark",
        "prompt_height": 0,
        "test_preset": "",
        "test_timeout": 120,
        "test_fix_rounds": 3,
        "active_profile": "",
        "profiles": [],
        "mcp_servers": [],
        "agent_mode": "agent",
        "context_limit": 256000,
        "auto_write_project": False,
    }


def ai_settings(data: dict) -> dict:
    provider = str(data.get("provider") or "openai")
    if provider not in ("openai", "anthropic"):
        provider = "openai"
    try:
        steps = int(data.get("max_steps") or 25)
    except (TypeError, ValueError):
        steps = 25
    return {
        "provider": provider,
        "base_url": str(data.get("base_url") or ""),
        "model": str(data.get("model") or ""),
        "api_key": str(data.get("api_key") or ""),
        "max_steps": min(100, max(1, steps)),
        "image_model": str(data.get("image_model") or ""),
        "image_base_url": str(data.get("image_base_url") or ""),
        "image_api_key": str(data.get("image_api_key") or ""),
    }


def config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "ProjectAgent"


def config_path() -> Path:
    return config_dir() / CONFIG_NAME


def needs_api_key(provider: str, base_url: str, api_key: str) -> bool:
    if (api_key or "").strip():
        return False
    if provider == "anthropic":
        return True
    url = (base_url or "").strip().lower()
    if not url:
        return True
    if "api.openai.com" in url or "openrouter.ai" in url:
        return True
    return False


def _servers(raw) -> list[dict]:
    servers = []
    if not isinstance(raw, list):
        return servers
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        args = item.get("args") or []
        if not isinstance(args, list):
            args = []
        env = item.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        if not name or not command:
            continue
        servers.append(
            {
                "name": name,
                "command": command,
                "args": [str(part) for part in args],
                "env": {str(key): str(value) for key, value in env.items()},
            }
        )
    return servers


def _prompt_height(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return min(640, value)


def _theme(raw) -> str:
    value = str(raw or "").strip().lower()
    if value in ("dark", "light"):
        return value
    return "dark"


def _test_preset(raw) -> str:
    from project_agent.testing import normalize_preset

    return normalize_preset(raw)


def _test_timeout(raw) -> int:
    from project_agent.testing import normalize_timeout

    return normalize_timeout(raw)


def _test_fix_rounds(raw) -> int:
    from project_agent.testing import normalize_fix_rounds

    return normalize_fix_rounds(raw)


def _agent_mode(raw) -> str:
    from project_agent.tools import normalize_agent_mode

    return normalize_agent_mode(raw)


def _context_limit(raw) -> int:
    from project_agent.context_usage import normalize_context_limit

    return normalize_context_limit(raw)


def _auto_write_project(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    value = str(raw or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _write_config(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    last_error: OSError | None = None
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(0.05 * (attempt + 1))
    try:
        path.write_text(text, encoding="utf-8")
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    except OSError as exc:
        raise last_error or exc from exc


def _profile_name(raw) -> str:
    return " ".join(str(raw or "").split())


def _profiles(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    found: dict[str, dict] = {}
    order: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _profile_name(item.get("name"))
        if not name or len(name) > 80:
            continue
        if name not in found:
            order.append(name)
        found[name] = {"name": name, **ai_settings(item)}
    return [found[name] for name in order]


def load_config(path: Path | None = None) -> tuple[dict, str | None]:
    path = path or config_path()
    if not path.exists():
        return default_config(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return default_config(), str(exc)
    if not isinstance(data, dict):
        return default_config(), "config.json должен быть объектом"
    merged = default_config()
    merged.update(ai_settings(data))
    if "project_dir" in data and data["project_dir"] is not None:
        merged["project_dir"] = str(data["project_dir"])
    merged["profiles"] = _profiles(data.get("profiles"))
    active = _profile_name(data.get("active_profile"))
    names = {item["name"] for item in merged["profiles"]}
    merged["active_profile"] = active if active in names else ""
    if merged["active_profile"]:
        current = next(item for item in merged["profiles"] if item["name"] == merged["active_profile"])
        for key in AI_FIELDS:
            merged[key] = current[key]
    merged["mcp_servers"] = _servers(data.get("mcp_servers"))
    merged["theme"] = _theme(data.get("theme"))
    merged["prompt_height"] = _prompt_height(data.get("prompt_height"))
    merged["test_preset"] = _test_preset(data.get("test_preset"))
    merged["test_timeout"] = _test_timeout(data.get("test_timeout"))
    merged["test_fix_rounds"] = _test_fix_rounds(data.get("test_fix_rounds"))
    merged["agent_mode"] = _agent_mode(data.get("agent_mode"))
    merged["context_limit"] = _context_limit(data.get("context_limit"))
    merged["auto_write_project"] = _auto_write_project(data.get("auto_write_project"))
    return merged, None


def save_config(data: dict, path: Path | None = None) -> None:
    path = path or config_path()
    payload = default_config()
    payload.update(ai_settings(data))
    payload["project_dir"] = str(data.get("project_dir") or "")
    payload["profiles"] = _profiles(data.get("profiles"))
    active = _profile_name(data.get("active_profile"))
    names = {item["name"] for item in payload["profiles"]}
    payload["active_profile"] = active if active in names else ""
    payload["mcp_servers"] = _servers(data.get("mcp_servers"))
    payload["theme"] = _theme(data.get("theme"))
    payload["prompt_height"] = _prompt_height(data.get("prompt_height"))
    payload["test_preset"] = _test_preset(data.get("test_preset"))
    payload["test_timeout"] = _test_timeout(data.get("test_timeout"))
    payload["test_fix_rounds"] = _test_fix_rounds(data.get("test_fix_rounds"))
    payload["agent_mode"] = _agent_mode(data.get("agent_mode"))
    payload["context_limit"] = _context_limit(data.get("context_limit"))
    payload["auto_write_project"] = _auto_write_project(data.get("auto_write_project"))
    _write_config(path, payload)


def save_agent_mode(mode: str, path: Path | None = None) -> None:
    path = path or config_path()
    mode = _agent_mode(mode)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("config.json должен быть объектом")
    else:
        data = default_config()
    data["agent_mode"] = mode
    _write_config(path, data)


def save_prompt_height(height: int, path: Path | None = None) -> None:
    path = path or config_path()
    height = _prompt_height(height)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("config.json должен быть объектом")
    else:
        data = default_config()
    data["prompt_height"] = height
    _write_config(path, data)


def save_theme(theme: str, path: Path | None = None) -> None:
    path = path or config_path()
    theme = _theme(theme)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("config.json должен быть объектом")
    else:
        data = default_config()
    data["theme"] = theme
    _write_config(path, data)
