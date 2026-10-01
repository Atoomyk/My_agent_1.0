from __future__ import annotations

"""Пресет и маппинг браузера через MCP Playwright — без встроенного Chrome."""

PLAYWRIGHT_SERVER_NAME = "playwright"
PLAYWRIGHT_SERVER = {
    "name": PLAYWRIGHT_SERVER_NAME,
    "command": "npx",
    "args": ["@playwright/mcp@latest"],
    "env": {},
}

# Короткие action → имя инструмента @playwright/mcp
ACTION_TO_TOOL = {
    "navigate": "browser_navigate",
    "open": "browser_navigate",
    "back": "browser_navigate_back",
    "forward": "browser_navigate_forward",
    "reload": "browser_reload",
    "snapshot": "browser_snapshot",
    "click": "browser_click",
    "type": "browser_type",
    "fill": "browser_fill_form",
    "fill_form": "browser_fill_form",
    "select": "browser_select_option",
    "hover": "browser_hover",
    "drag": "browser_drag",
    "press": "browser_press_key",
    "key": "browser_press_key",
    "tabs": "browser_tabs",
    "dialog": "browser_handle_dialog",
    "screenshot": "browser_take_screenshot",
    "wait": "browser_wait_for",
    "resize": "browser_resize",
    "close": "browser_close",
}

BROWSER_CALL_TIMEOUT = 120


def find_browser_server_name(servers: list[dict] | None) -> str | None:
    """Имя настроенного browser MCP: playwright / browser / *playwright*."""
    names = [str(item.get("name") or "").strip() for item in (servers or [])]
    names = [name for name in names if name]
    for preferred in (PLAYWRIGHT_SERVER_NAME, "browser", "playwright-mcp"):
        if preferred in names:
            return preferred
    for name in names:
        lowered = name.lower()
        if "playwright" in lowered or lowered.endswith("browser"):
            return name
    return None


def map_browser_arguments(action: str, args: dict) -> tuple[str, dict]:
    """Превращает action + поля в (mcp_tool_name, arguments)."""
    key = str(action or "").strip().lower()
    tool = ACTION_TO_TOOL.get(key)
    if not tool:
        known = ", ".join(sorted(ACTION_TO_TOOL))
        raise ValueError(f"Неизвестный action «{action}». Доступно: {known}")
    payload: dict = {}
    mapping = {
        "url": "url",
        "ref": "ref",
        "element": "element",
        "text": "text",
        "key": "key",
        "values": "values",
        "fields": "fields",
        "submit": "submit",
        "slowly": "slowly",
        "time": "time",
        "text_gone": "textGone",
        "timeout": "timeout",
        "width": "width",
        "height": "height",
        "accept": "accept",
        "prompt_text": "promptText",
        "start_ref": "startRef",
        "end_ref": "endRef",
        "filename": "filename",
        "type": "type",
        "tabs_action": "action",
        "index": "index",
    }
    for src, dest in mapping.items():
        if src in args and args[src] is not None and args[src] != "":
            payload[dest] = args[src]
    return tool, payload
