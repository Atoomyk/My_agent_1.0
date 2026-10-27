"""Палитра и общие константы интерфейса.

Модуль содержит только данные: цвета (пары light/dark), размеры колонок и
подписи-лейблы для выпадающих меню. Здесь нет Tk-виджетов и нет импортов из
``project_agent.app`` — модуль может импортироваться кем угодно.
"""

from __future__ import annotations

PROVIDER_LABELS = {
    "OpenAI-совместимый": "openai",
    "Anthropic": "anthropic",
}
LABEL_BY_PROVIDER = {value: key for key, value in PROVIDER_LABELS.items()}
MODE_LABELS = {
    "Agent": "agent",
    "Debug": "debug",
    "Ask": "ask",
}
LABEL_BY_MODE = {value: key for key, value in MODE_LABELS.items()}
THEME_LABELS = {
    "Тёмная": "dark",
    "Светлая": "light",
}
LABEL_BY_THEME = {value: key for key, value in THEME_LABELS.items()}
_NO_PROFILE = "нет профилей"
INK = ("#f6f3ee", "#1c1916")
PANEL = ("#efeae3", "#26221e")
FIELD = ("#fffdf8", "#161310")
BUTTON = ("#fffdf8", "#2c2824")
BUTTON_HOVER = ("#e6dfd6", "#3a342e")
BORDER = ("#ddd4c8", "#4a433c")
TEXT = ("#2c2824", "#f3eee6")
MUTED = ("#8d847a", "#a3988c")
HINT = ("#c2bbb2", "#5c564f")
USER_TEXT = ("#4a3c30", "#f7f0e4")
USER_BG = ("#e8e0d5", "#322d28")
SELECT = ("#e4dcd2", "#3a342e")
SEND = ("#3c3631", "#efe6da")
SEND_HOVER = ("#2b2724", "#f7f1e8")
ON_SEND = ("#f6f1ea", "#1c1916")
CODE_BG = ("#efe9e1", "#24201c")
CODE_TEXT = ("#0f7b8a", "#6cc7d3")
CTX_OK = ("#5a8f6a", "#6aab7a")
CTX_WARN = ("#c48a2e", "#d4a04a")
CTX_FULL = ("#c45a4a", "#d46a5a")
DIFF_ADD = ("#2f6b3c", "#7dba8a")
DIFF_DEL = ("#a33d3d", "#e08a8a")
DIFF_HUNK = ("#6a5a8a", "#b0a0d0")
GIT_MOD = ("#b8860b", "#e0b84a")  # modified / dirty folder
GIT_NEW = ("#2f6b3c", "#7dba8a")  # untracked / added
GIT_DEL = ("#a33d3d", "#e08a8a")  # deleted (если ещё виден в дереве)
SEARCH_BG = ("#efe0b8", "#4a3f24")
SEARCH_CUR = ("#e0b86a", "#7a5e28")
# Подсветка редактора (P0.2): light / dark, без «AI-purple».
SYN_KEYWORD = ("#8a4b2f", "#e0a070")
SYN_STRING = CODE_TEXT
SYN_COMMENT = MUTED
SYN_NUMBER = ("#2f6b5a", "#7dbaa8")
SYN_DECORATOR = ("#6a5a2a", "#c4b06a")
SYN_HEADING = ("#3a5a7a", "#8ab0d0")
SYN_MD_CODE = ("#0f7b8a", "#6cc7d3")
SYN_LITERAL = ("#8a4b2f", "#e0a070")
CHAT_COLUMN = 820
DIFF_PREVIEW_LINES = 48
HIGHLIGHT_DEBOUNCE_MS = 280
SIDE_DEFAULT = 248
SIDE_MIN = 180
