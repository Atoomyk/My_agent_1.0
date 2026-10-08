from __future__ import annotations

import copy
import re
from pathlib import Path

TOKEN_RE = re.compile(r"\[\[SEC:[0-9a-f]{4}:\d+\]\]")
PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)
PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{20,}|github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|"
    r"gho_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9\-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?)"
)
URL_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)@", re.I)
BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9\-._~+/]{12,}={0,2})")
QUOTED_RE = re.compile(
    r"""(?ix)
    (["']?)
    (?P<key>password|passwd|pwd|secret|api_key|apikey|api-key|access_key|private_key|client_secret|token)
    (["']?)
    \s*[:=]\s*
    (?P<quote>['"])
    (?P<val>[^'"]{6,})
    (?P=quote)
    """
)
ENV_LINE_RE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*)(.*)$")
ENV_COMMENT_RE = re.compile(r"^(\s*#+\s*)(.*)$")
JSON_VALUE_RE = re.compile(r'(:\s*")((?:\\.|[^"\\])*)(")')

JSON_SECRET_NAMES = {
    "secrets.json",
    "credentials.json",
    "auth.json",
    "service-account.json",
}
SECRET_BLOB_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
IMAGE_PART_TYPES = {"image", "image_url"}


def is_env_file(path: Path | None) -> bool:
    if path is None:
        return False
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def is_json_secret_file(path: Path | None) -> bool:
    if path is None:
        return False
    name = path.name.lower()
    return name in JSON_SECRET_NAMES or name.endswith(".secrets.json")


def is_secret_blob(path: Path | None) -> bool:
    if path is None:
        return False
    name = path.name.lower()
    return name in SECRET_BLOB_NAMES or name.endswith(".key")


def looks_like_literal_secret(value: str) -> bool:
    if len(value) < 8:
        return False
    if re.fullmatch(r"[A-Za-z]{1,15}", value):
        return False
    if re.fullmatch(r"https?://[A-Za-z0-9.-]+/?", value):
        return False
    return True


def literals_from_settings(settings: dict) -> list[str]:
    found: list[str] = []
    for key in ("api_key", "image_api_key"):
        value = str(settings.get(key) or "")
        if len(value) >= 8:
            found.append(value)
    for server in settings.get("mcp_servers") or []:
        env = server.get("env") or {}
        if not isinstance(env, dict):
            continue
        for value in env.values():
            text = str(value)
            if looks_like_literal_secret(text):
                found.append(text)
    return found


def _should_hide_env(value: str) -> bool:
    text = value.strip()
    if len(text) < 4 or TOKEN_RE.fullmatch(text):
        return False
    if re.fullmatch(r"\d{1,5}", text):
        return False
    if text.lower() in {"true", "false", "null", "none"}:
        return False
    if text.startswith("$") or text.startswith("%"):
        return False
    return True


class SecretVault:
    def __init__(self) -> None:
        import secrets as _secrets

        self._nonce = _secrets.token_hex(2)
        self._map: dict[str, str] = {}
        self._rev: dict[str, str] = {}
        self._n = 0
        self._token_re = TOKEN_RE

    def clear(self) -> None:
        import secrets as _secrets

        self._nonce = _secrets.token_hex(2)
        self._map.clear()
        self._rev.clear()
        self._n = 0

    def take(self, secret: str) -> str:
        existing = self._rev.get(secret)
        if existing:
            return existing
        self._n += 1
        token = f"[[SEC:{self._nonce}:{self._n}]]"
        self._rev[secret] = token
        self._map[token] = secret
        return token

    def restore(self, text: str) -> str:
        return self._token_re.sub(lambda match: self._map.get(match.group(0), match.group(0)), text)

    def count(self, text: str) -> int:
        return len(self._token_re.findall(text))

    def strip_markers(self, text: str) -> str:
        return self._token_re.sub("", self.redact(text))

    def redact(self, text: str, path: Path | None = None) -> str:
        if not text:
            return text
        text = self._replay_known(text)
        text = PEM_RE.sub(lambda match: self.take(match.group(0)), text)
        text = PREFIX_RE.sub(lambda match: self.take(match.group(0)), text)
        text = URL_RE.sub(lambda match: match.group(1) + self.take(match.group(2)) + "@", text)
        text = BEARER_RE.sub(lambda match: match.group(1) + self.take(match.group(2)), text)
        text = QUOTED_RE.sub(self._replace_quoted, text)
        if "-----BEGIN" in text and "PRIVATE KEY" in text:
            text = re.sub(r"-----BEGIN[\s\S]*", self.take("incomplete-private-key"), text, count=1)
        if is_env_file(path):
            text = self._redact_env(text)
        if is_json_secret_file(path):
            text = self._redact_json(text)
        return text

    def _replay_known(self, text: str) -> str:
        for secret in sorted(self._rev, key=len, reverse=True):
            if len(secret) < 8:
                continue
            if len(secret) < 16 and re.fullmatch(r"[A-Za-z]+", secret):
                continue
            if secret in text:
                text = text.replace(secret, self._rev[secret])
        return text

    def _replace_quoted(self, match: re.Match) -> str:
        value = match.group("val")
        if TOKEN_RE.fullmatch(value):
            return match.group(0)
        token = self.take(value)
        return match.group(0).replace(value, token, 1)

    def _redact_env(self, text: str) -> str:
        lines = []
        for line in text.splitlines(keepends=True):
            newline = ""
            body = line
            if body.endswith("\r\n"):
                body, newline = body[:-2], "\r\n"
            elif body.endswith("\n"):
                body, newline = body[:-1], "\n"
            comment_prefix = ""
            assignment = body
            commented = ENV_COMMENT_RE.match(body)
            if commented:
                comment_prefix, assignment = commented.group(1), commented.group(2)
            matched = ENV_LINE_RE.match(assignment)
            if not matched:
                lines.append(line)
                continue
            prefix, raw = matched.group(1), matched.group(2)
            stripped = raw.strip()
            quote = ""
            inner = stripped
            if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "'\"":
                quote = stripped[0]
                inner = stripped[1:-1]
            if not _should_hide_env(inner):
                lines.append(line)
                continue
            lines.append(f"{comment_prefix}{prefix}{quote}{self.take(inner)}{quote}{newline}")
        return "".join(lines)

    def _redact_json(self, text: str) -> str:
        def replace(match: re.Match) -> str:
            value = match.group(2)
            if not _should_hide_env(value) or TOKEN_RE.fullmatch(value):
                return match.group(0)
            return f"{match.group(1)}{self.take(value)}{match.group(3)}"

        return JSON_VALUE_RE.sub(replace, text)


class Scrubber:
    def __init__(self, vault: SecretVault, literals: list[str]):
        self.vault = vault
        unique: list[str] = []
        for value in literals:
            if value and value not in unique:
                unique.append(value)
        self.literals = unique

    def __call__(self, text: str, path: Path | None = None) -> str:
        if text is None:
            return ""
        if not isinstance(text, str):
            text = str(text)
        for secret in self.literals:
            if len(secret) >= 8 and secret in text:
                text = text.replace(secret, self.vault.take(secret))
        return self.vault.redact(text, path)

    def strip_outbound(self, text: str) -> str:
        hidden = self(text)
        return self.vault._token_re.sub("", hidden)


def scrub_outbound(messages: list, redact) -> list:
    cleaned = []
    for message in messages:
        item = copy.deepcopy(message)
        _scrub_message(item, redact)
        cleaned.append(item)
    return cleaned


def _scrub_message(message: dict, redact) -> None:
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = redact(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                _scrub_part(part, redact)
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            message[key] = redact(value)
    for call in message.get("tool_calls") or []:
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            function["arguments"] = redact(arguments)
        elif isinstance(arguments, dict):
            function["arguments"] = _scrub_json(arguments, redact)


def _scrub_part(part: dict, redact) -> None:
    part_type = part.get("type")
    if part_type in IMAGE_PART_TYPES:
        return
    if part_type == "tool_result":
        inner = part.get("content")
        if isinstance(inner, str):
            part["content"] = redact(inner)
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict):
                    _scrub_part(sub, redact)
        return
    if part_type == "tool_use" and isinstance(part.get("input"), dict):
        part["input"] = _scrub_json(part["input"], redact)
        return
    if isinstance(part.get("text"), str):
        part["text"] = redact(part["text"])


def _scrub_json(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_scrub_json(item, redact) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_json(item, redact) for key, item in value.items()}
    return value
