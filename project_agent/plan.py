from __future__ import annotations

import re

PLAN_MARK = "⟦plan⟧"
MAX_STEPS = 40
MAX_STEP_TEXT = 400
MAX_PATH = 260

_STEP = re.compile(
    r"""^
    \s*[-*]\s*
    \[([ xX])\]\s*
    (?:
        `([^`]+)`\s*:\s*
        |
        (?P<path>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)\s*:\s*
    )?
    (?P<text>.+?)
    \s*$
    """,
    re.VERBOSE,
)


def plan_mark() -> str:
    return PLAN_MARK


def parse_plan_mark(text: str) -> bool:
    return (text or "").strip() == PLAN_MARK


def empty_plan() -> dict:
    return {"steps": []}


def clean_plan(raw) -> dict:
    if not isinstance(raw, dict):
        return empty_plan()
    steps: list[dict] = []
    for item in (raw.get("steps") or [])[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())[:MAX_STEP_TEXT]
        if not text:
            continue
        path = str(item.get("path") or "").replace("\\", "/").strip().strip("/")[:MAX_PATH]
        steps.append({"text": text, "path": path, "done": bool(item.get("done"))})
    return {"steps": steps}


def parse_plan_checklist(text: str) -> list[dict]:
    """Извлечь шаги `- [ ]` / `- [x]` из ответа Plan; путь опционален."""
    steps: list[dict] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = _STEP.match(raw)
        if not match:
            continue
        done = match.group(1).lower() == "x"
        path = (match.group(2) or match.group("path") or "").replace("\\", "/").strip().strip("/")
        body = (match.group("text") or "").strip()
        if not body:
            continue
        steps.append(
            {
                "text": body[:MAX_STEP_TEXT],
                "path": path[:MAX_PATH],
                "done": done,
            }
        )
        if len(steps) >= MAX_STEPS:
            break
    return steps


def format_plan_context(plan: dict | None) -> str:
    data = clean_plan(plan)
    steps = data.get("steps") or []
    if not steps:
        return ""
    lines = ["Актуальный план задачи (чеклист; отмечай прогресс по факту):"]
    for index, step in enumerate(steps, start=1):
        mark = "x" if step.get("done") else " "
        path = str(step.get("path") or "").strip()
        text = str(step.get("text") or "").strip()
        if path:
            lines.append(f"{index}. [{mark}] `{path}`: {text}")
        else:
            lines.append(f"{index}. [{mark}] {text}")
    return "\n".join(lines)


def toggle_step(plan: dict | None, index: int) -> dict:
    data = clean_plan(plan)
    steps = data.get("steps") or []
    if 0 <= index < len(steps):
        steps[index]["done"] = not bool(steps[index].get("done"))
    return {"steps": steps}
