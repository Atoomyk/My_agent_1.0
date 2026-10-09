from __future__ import annotations

import re
from pathlib import Path

# Теги Tk Text для редактора
TAG_KEYWORD = "syn_kw"
TAG_STRING = "syn_str"
TAG_COMMENT = "syn_cmt"
TAG_NUMBER = "syn_num"
TAG_DECORATOR = "syn_dec"
TAG_HEADING = "syn_hd"
TAG_MD_CODE = "syn_mdcode"
TAG_LITERAL = "syn_lit"

ALL_TAGS = (
    TAG_KEYWORD,
    TAG_STRING,
    TAG_COMMENT,
    TAG_NUMBER,
    TAG_DECORATOR,
    TAG_HEADING,
    TAG_MD_CODE,
    TAG_LITERAL,
)

# Лимит символов — дальше не красим (огромные файлы).
MAX_HIGHLIGHT_CHARS = 200_000

_PY_EXT = {".py", ".pyw", ".pyi"}
_JSON_EXT = {".json", ".jsonc"}
_MD_EXT = {".md", ".markdown", ".mdown"}

_PY_KEYWORDS = (
    "False|True|None|and|as|assert|async|await|break|class|continue|def|del|"
    "elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|"
    "not|or|pass|raise|return|try|while|with|yield|match|case|type"
)

_PY_TOKEN = re.compile(
    rf"""
    (?P<{TAG_COMMENT}>\#[^\n]*)
    |(?P<{TAG_STRING}>
        \"\"\"[\s\S]*?\"\"\"
        |'''[\s\S]*?'''
        |"(?:\\.|[^"\\])*"
        |'(?:\\.|[^'\\])*'
        |f"(?:\\.|[^"\\])*"
        |f'(?:\\.|[^'\\])*'
        |r"(?:\\.|[^"\\])*"
        |r'(?:\\.|[^'\\])*'
        |rf"(?:\\.|[^"\\])*"
        |rf'(?:\\.|[^'\\])*'
        |fr"(?:\\.|[^"\\])*"
        |fr'(?:\\.|[^'\\])*'
        |b"(?:\\.|[^"\\])*"
        |b'(?:\\.|[^'\\])*'
      )
    |(?P<{TAG_DECORATOR}>@[A-Za-z_]\w*)
    |(?P<{TAG_NUMBER}>\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)
    |(?P<{TAG_KEYWORD}>\b(?:{_PY_KEYWORDS})\b)
    """,
    re.VERBOSE,
)

_JSON_TOKEN = re.compile(
    rf"""
    (?P<{TAG_COMMENT}>//[^\n]*|/\*[\s\S]*?\*/)
    |(?P<{TAG_STRING}>"(?:\\.|[^"\\])*")
    |(?P<{TAG_NUMBER}>-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)
    |(?P<{TAG_LITERAL}>\b(?:true|false|null)\b)
    """,
    re.VERBOSE,
)

_MD_TOKEN = re.compile(
    rf"""
    (?P<{TAG_MD_CODE}>```[\s\S]*?```|`[^`\n]+`)
    |(?P<{TAG_HEADING}>^\#{{1,6}}[ \t]+.+$)
    |(?P<{TAG_COMMENT}>^\s*>[^\n]*$)
    """,
    re.VERBOSE | re.MULTILINE,
)


def language_for_path(path: str) -> str | None:
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _PY_EXT:
        return "python"
    if suffix in _JSON_EXT:
        return "json"
    if suffix in _MD_EXT:
        return "markdown"
    return None


def tokenize(text: str, language: str | None) -> list[tuple[int, int, str]]:
    """Вернуть список (start, end, tag) в индексах символов Python-строки."""
    if not text or not language:
        return []
    if len(text) > MAX_HIGHLIGHT_CHARS:
        text = text[:MAX_HIGHLIGHT_CHARS]
    if language == "python":
        return _scan(_PY_TOKEN, text)
    if language == "json":
        return _scan(_JSON_TOKEN, text)
    if language == "markdown":
        return _scan(_MD_TOKEN, text)
    return []


def _scan(pattern: re.Pattern[str], text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in pattern.finditer(text):
        tag = match.lastgroup
        if not tag:
            continue
        start, end = match.span(tag)
        if start < end:
            spans.append((start, end, tag))
    return spans
