from __future__ import annotations

import os
import re
from pathlib import Path

from project_agent.paths import MAX_FILE_BYTES

JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
GO_EXTENSIONS = (".go",)
PS_EXTENSIONS = (".ps1", ".psm1")
RUST_EXTENSIONS = (".rs",)
SYMBOL_EXTENSIONS = (".py", *JS_EXTENSIONS, *GO_EXTENSIONS, *PS_EXTENSIONS, *RUST_EXTENSIONS)

_CLASS = re.compile(r"^([ \t]*)class[ \t]+([A-Za-z_][\w]*)")
_DEF = re.compile(r"^([ \t]*)(?:async[ \t]+)?def[ \t]+([A-Za-z_][\w]*)")
_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+(\S+)[ \t]+import[ \t]+(.+?)\s*$")
_IMPORT = re.compile(r"^[ \t]*import[ \t]+(.+?)\s*$")
_NAME = re.compile(r"^([A-Za-z_][\w]*)")

_JS_IDENT = r"[A-Za-z_$][\w$]*"
_JS_FUNCTION = re.compile(
    rf"^[ \t]*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*({_JS_IDENT})"
)
_JS_CLASS = re.compile(rf"^[ \t]*(?:export\s+(?:default\s+)?)?class\s+({_JS_IDENT})")
_JS_EXPORT_CONST = re.compile(rf"^[ \t]*export\s+(?:const|let|var)\s+({_JS_IDENT})")
_JS_TYPE = re.compile(rf"^[ \t]*(?:export\s+)?(?:type|interface)\s+({_JS_IDENT})")
_JS_EXPORT_DEFAULT_NAME = re.compile(rf"^[ \t]*export\s+default\s+({_JS_IDENT})\s*;?\s*$")
_JS_METHOD = re.compile(
    rf"^[ \t]+(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?({_JS_IDENT})\s*\("
)
_JS_IMPORT_FROM = re.compile(
    rf"""^[ \t]*import\s+(.+?)\s+from\s+['"]([^'"]+)['"]"""
)
_JS_IMPORT_SIDE = re.compile(rf"""^[ \t]*import\s+['"]([^'"]+)['"]""")
_JS_EXPORT_FROM = re.compile(
    rf"""^[ \t]*export\s+(?:\*(?:\s+as\s+{_JS_IDENT})?|type\s+\{{[^}}]*\}}|\{{[^}}]*\}})\s+from\s+['"]([^'"]+)['"]"""
)
_JS_REQUIRE = re.compile(rf"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_IMPORT_NAMES = re.compile(rf"""\{{([^}}]*)\}}""")
_JS_DEFAULT_AS = re.compile(rf"^({_JS_IDENT})(?:\s*,\s*|\s+$)")
_JS_STAR_AS = re.compile(rf"^\*\s+as\s+({_JS_IDENT})")

_JS_METHOD_SKIP = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "else",
        "do",
        "try",
        "return",
        "typeof",
        "new",
        "delete",
        "throw",
        "with",
        "case",
        "default",
        "of",
        "in",
        "await",
        "async",
        "static",
        "get",
        "set",
        "function",
        "class",
        "const",
        "let",
        "var",
        "import",
        "export",
        "from",
        "typeof",
        "instanceof",
        "yield",
    }
)


def is_symbol_path(relative: str) -> bool:
    lower = relative.replace("\\", "/").lower()
    return any(lower.endswith(ext) for ext in SYMBOL_EXTENSIONS)


def is_js_path(relative: str) -> bool:
    lower = relative.replace("\\", "/").lower()
    return any(lower.endswith(ext) for ext in JS_EXTENSIONS)


def is_go_path(relative: str) -> bool:
    return relative.replace("\\", "/").lower().endswith(".go")


def is_ps_path(relative: str) -> bool:
    lower = relative.replace("\\", "/").lower()
    return any(lower.endswith(ext) for ext in PS_EXTENSIONS)


def is_rust_path(relative: str) -> bool:
    return relative.replace("\\", "/").lower().endswith(".rs")


def lang_label_for_path(relative: str) -> str:
    lower = relative.replace("\\", "/").lower()
    if lower.endswith(".py"):
        return "py"
    if is_js_path(lower):
        return "js"
    if is_go_path(lower):
        return "go"
    if is_ps_path(lower):
        return "ps"
    if is_rust_path(lower):
        return "rs"
    return ""


def _indent_width(prefix: str) -> int:
    return len(prefix.expandtabs(4))


def _split_import_names(raw: str) -> list[str]:
    text = raw.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    names: list[str] = []
    for part in text.split(","):
        piece = part.strip()
        if not piece or piece == "*":
            if piece == "*":
                names.append("*")
            continue
        piece = piece.split(" as ", 1)[0].strip()
        match = _NAME.match(piece)
        if match:
            names.append(match.group(1))
    return names


def _sym(name: str, qualname: str, kind: str, line: int, lang: str) -> dict:
    return {"name": name, "qualname": qualname, "kind": kind, "line": line, "lang": lang}


def parse_python_file(text: str) -> tuple[list[dict], list[dict]]:
    """Топ-уровень class/def + методы класса; import / from import."""
    symbols: list[dict] = []
    imports: list[dict] = []
    class_name: str | None = None
    class_indent = -1
    method_indent = -1
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        class_match = _CLASS.match(line)
        if class_match:
            indent = _indent_width(class_match.group(1))
            name = class_match.group(2)
            if indent == 0:
                class_name = name
                class_indent = 0
                method_indent = -1
                symbols.append(_sym(name, name, "class", number, "py"))
            elif class_name is not None and indent <= class_indent:
                class_name = None
                class_indent = -1
                method_indent = -1
            continue
        def_match = _DEF.match(line)
        if def_match:
            indent = _indent_width(def_match.group(1))
            name = def_match.group(2)
            if indent == 0:
                class_name = None
                class_indent = -1
                method_indent = -1
                symbols.append(_sym(name, name, "def", number, "py"))
            elif class_name is not None and indent > class_indent:
                if method_indent < 0:
                    method_indent = indent
                if indent == method_indent:
                    qual = f"{class_name}.{name}"
                    symbols.append(_sym(name, qual, "method", number, "py"))
            continue
        stripped = line.lstrip()
        indent = _indent_width(line[: len(line) - len(stripped)])
        if class_name is not None and indent == 0 and not stripped.startswith("@"):
            class_name = None
            class_indent = -1
            method_indent = -1
        if indent != 0:
            continue
        from_match = _FROM_IMPORT.match(line)
        if from_match:
            module = from_match.group(1).strip()
            names = _split_import_names(from_match.group(2))
            imports.append({"module": module, "names": names or ["*"], "line": number})
            continue
        import_match = _IMPORT.match(line)
        if import_match:
            for part in import_match.group(1).split(","):
                piece = part.strip().split(" as ", 1)[0].strip()
                if piece:
                    imports.append({"module": piece, "names": [], "line": number})
    return symbols, imports


def _strip_js_line_comment(line: str, in_block: bool) -> tuple[str, bool]:
    if in_block:
        end = line.find("*/")
        if end < 0:
            return "", True
        line = line[end + 2 :]
        in_block = False
    out: list[str] = []
    i = 0
    in_str: str | None = None
    while i < len(line):
        ch = line[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(line):
            nxt = line[i + 1]
            if nxt == "/":
                break
            if nxt == "*":
                end = line.find("*/", i + 2)
                if end < 0:
                    return "".join(out), True
                i = end + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out), in_block


def _js_import_names(clause: str) -> list[str]:
    text = clause.strip()
    names: list[str] = []
    star = _JS_STAR_AS.match(text)
    if star:
        return ["*"]
    if text.startswith("*"):
        return ["*"]
    braced = _JS_IMPORT_NAMES.search(text)
    if braced:
        for part in braced.group(1).split(","):
            piece = part.strip()
            if not piece or piece.startswith("type "):
                piece = piece[5:].strip() if piece.startswith("type ") else piece
            if not piece:
                continue
            piece = re.split(r"\s+as\s+", piece, maxsplit=1)[0].strip()
            if re.match(rf"^{_JS_IDENT}$", piece):
                names.append(piece)
        head = text[: braced.start()].strip().rstrip(",").strip()
        if head and not head.startswith("{") and head != "type":
            default = head.split(",", 1)[0].strip()
            if re.match(rf"^{_JS_IDENT}$", default):
                names.insert(0, default)
        return names or ["*"]
    default = _JS_DEFAULT_AS.match(text)
    if default:
        return [default.group(1)]
    if re.match(rf"^{_JS_IDENT}$", text):
        return [text]
    return []


def _brace_delta(line: str) -> int:
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == "\\" and i + 1 < len(line):
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            in_str = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def parse_js_file(text: str) -> tuple[list[dict], list[dict]]:
    """Лёгкий regex-разбор JS/TS: export/function/class/type + import/require."""
    symbols: list[dict] = []
    imports: list[dict] = []
    depth = 0
    class_name: str | None = None
    class_depth = -1
    pending_class: str | None = None
    in_block = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line, in_block = _strip_js_line_comment(raw, in_block)
        stripped = line.strip()
        if not stripped:
            continue

        if depth == 0:
            fn = _JS_FUNCTION.match(line)
            if fn:
                name = fn.group(1)
                symbols.append(_sym(name, name, "function", number, "js"))
            else:
                cls = _JS_CLASS.match(line)
                if cls:
                    name = cls.group(1)
                    pending_class = name
                    symbols.append(_sym(name, name, "class", number, "js"))
                else:
                    exported = _JS_EXPORT_CONST.match(line)
                    if exported:
                        name = exported.group(1)
                        symbols.append(_sym(name, name, "export", number, "js"))
                    else:
                        typed = _JS_TYPE.match(line)
                        if typed:
                            name = typed.group(1)
                            head = line[: line.find(name)]
                            kind = "interface" if re.search(r"\binterface\b", head) else "type"
                            symbols.append(_sym(name, name, kind, number, "js"))
                        else:
                            default_name = _JS_EXPORT_DEFAULT_NAME.match(line)
                            if default_name and default_name.group(1) not in {
                                "class",
                                "function",
                                "async",
                            }:
                                name = default_name.group(1)
                                symbols.append(_sym(name, name, "export", number, "js"))

            imp_from = _JS_IMPORT_FROM.match(line)
            if imp_from:
                names = _js_import_names(imp_from.group(1))
                imports.append({"module": imp_from.group(2), "names": names, "line": number})
            else:
                imp_side = _JS_IMPORT_SIDE.match(line)
                if imp_side:
                    imports.append({"module": imp_side.group(1), "names": [], "line": number})
                else:
                    exp_from = _JS_EXPORT_FROM.match(line)
                    if exp_from:
                        imports.append({"module": exp_from.group(1), "names": ["*"], "line": number})

            for req in _JS_REQUIRE.finditer(line):
                imports.append({"module": req.group(1), "names": [], "line": number})

        elif class_name is not None and depth == class_depth:
            method = _JS_METHOD.match(line)
            if method:
                name = method.group(1)
                if name not in _JS_METHOD_SKIP:
                    symbols.append(_sym(name, f"{class_name}.{name}", "method", number, "js"))

        delta = _brace_delta(line)
        depth += delta
        if depth < 0:
            depth = 0
        if pending_class is not None and depth > 0:
            class_name = pending_class
            class_depth = depth
            pending_class = None
        if class_name is not None and depth < class_depth:
            class_name = None
            class_depth = -1
    return symbols, imports


def resolve_js_relative(from_file: str, module: str) -> str | None:
    """Нормализованный относительный путь без суффикса; только ./ и ../."""
    module = (module or "").strip()
    if not module.startswith("."):
        return None
    base = Path(from_file.replace("\\", "/")).parent
    joined = os.path.normpath(str(base / module)).replace("\\", "/")
    if joined.startswith("../") or joined == "..":
        return None
    for ext in JS_EXTENSIONS:
        if joined.lower().endswith(ext):
            return joined[: -len(ext)]
    if joined.endswith("/"):
        joined = joined.rstrip("/")
    return joined


def path_module_aliases(relative: str) -> set[str]:
    """Варианты имени модуля для поиска importers (a/b.py → a.b, a/b, b)."""
    rel = (relative or "").replace("\\", "/").strip("/")
    if not rel:
        return set()
    lower = rel.lower()
    stem = rel
    for ext in SYMBOL_EXTENSIONS:
        if lower.endswith(ext):
            stem = rel[: -len(ext)]
            break
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    elif stem == "__init__":
        stem = ""
    aliases: set[str] = set()
    if stem:
        aliases.add(stem.lower())
        aliases.add(stem.replace("/", ".").lower())
        aliases.add(Path(stem).name.lower())
    aliases.add(rel.lower())
    aliases.add(Path(rel).name.lower())
    return {item for item in aliases if item}


_GO_FUNC = re.compile(r"^func\s+(?:\([^)]+\)\s*)?([A-Za-z_][\w]*)\s*\(")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_][\w]*)\b")
_GO_IMPORT_ONE = re.compile(r'^import\s+(?:[A-Za-z_][\w.]*\s+)?\"([^\"]+)\"')
_GO_IMPORT_START = re.compile(r"^import\s+\(\s*$")
_GO_IMPORT_LINE = re.compile(r'^\s*(?:[A-Za-z_][\w.]*\s+)?\"([^\"]+)\"')

_PS_FUNCTION = re.compile(
    r"^(?:function|filter|workflow)\s+(?:global:|script:|local:|private:)?([A-Za-z_][\w-]*)",
    re.IGNORECASE,
)
_PS_CLASS = re.compile(r"^class\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_PS_USING = re.compile(
    r"^using\s+(?:module|namespace|assembly)\s+([^\s;#]+)",
    re.IGNORECASE,
)

_RS_FN = re.compile(
    r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+([A-Za-z_][\w]*)\s*(?:<[^>]*>)?\s*\("
)
_RS_TYPE = re.compile(
    r"^(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|union)\s+([A-Za-z_][\w]*)"
)
_RS_MOD = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_][\w]*)")
_RS_USE = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?use\s+(.+?)\s*;?\s*$")


def parse_go_file(text: str) -> tuple[list[dict], list[dict]]:
    symbols: list[dict] = []
    imports: list[dict] = []
    in_import = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("//", 1)[0].rstrip()
        if not line.strip():
            continue
        if in_import:
            if line.strip() == ")":
                in_import = False
                continue
            match = _GO_IMPORT_LINE.match(line)
            if match:
                imports.append({"module": match.group(1), "names": [], "line": number})
            continue
        if _GO_IMPORT_START.match(line):
            in_import = True
            continue
        one = _GO_IMPORT_ONE.match(line)
        if one:
            imports.append({"module": one.group(1), "names": [], "line": number})
            continue
        fn = _GO_FUNC.match(line)
        if fn:
            name = fn.group(1)
            kind = "method" if "(" in line[: line.find(name)] else "func"
            symbols.append(_sym(name, name, kind, number, "go"))
            continue
        typed = _GO_TYPE.match(line)
        if typed:
            name = typed.group(1)
            kind = "type"
            if re.search(r"\bstruct\b", line):
                kind = "struct"
            elif re.search(r"\binterface\b", line):
                kind = "interface"
            symbols.append(_sym(name, name, kind, number, "go"))
    return symbols, imports


def parse_ps_file(text: str) -> tuple[list[dict], list[dict]]:
    symbols: list[dict] = []
    imports: list[dict] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        fn = _PS_FUNCTION.match(stripped)
        if fn:
            name = fn.group(1)
            symbols.append(_sym(name, name, "function", number, "ps"))
            continue
        cls = _PS_CLASS.match(stripped)
        if cls:
            name = cls.group(1)
            symbols.append(_sym(name, name, "class", number, "ps"))
            continue
        using = _PS_USING.match(stripped)
        if using:
            imports.append({"module": using.group(1).strip("'\""), "names": [], "line": number})
    return symbols, imports


def parse_rust_file(text: str) -> tuple[list[dict], list[dict]]:
    symbols: list[dict] = []
    imports: list[dict] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("//", 1)[0].rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        fn = _RS_FN.match(stripped)
        if fn:
            name = fn.group(1)
            symbols.append(_sym(name, name, "fn", number, "rs"))
            continue
        typed = _RS_TYPE.match(stripped)
        if typed:
            name = typed.group(1)
            head = stripped.split(name, 1)[0]
            if "struct" in head:
                kind = "struct"
            elif "enum" in head:
                kind = "enum"
            elif "trait" in head:
                kind = "trait"
            elif "union" in head:
                kind = "union"
            else:
                kind = "type"
            symbols.append(_sym(name, name, kind, number, "rs"))
            continue
        mod = _RS_MOD.match(stripped)
        if mod:
            name = mod.group(1)
            symbols.append(_sym(name, name, "mod", number, "rs"))
            continue
        use = _RS_USE.match(stripped)
        if use:
            clause = use.group(1).strip()
            module = clause.split("::{", 1)[0].split(" as ", 1)[0].strip()
            names: list[str] = []
            if "::{" in clause and clause.endswith("}"):
                inner = clause.rsplit("::{", 1)[-1].rstrip("}")
                for part in inner.split(","):
                    piece = part.strip().split(" as ", 1)[0].strip()
                    if piece and piece != "self" and piece != "*":
                        names.append(piece)
                    elif piece == "*":
                        names.append("*")
            imports.append({"module": module, "names": names, "line": number})
    return symbols, imports


def read_source_text(full: Path) -> str | None:
    try:
        if not full.is_file() or full.suffix.lower() not in {
            ".py",
            *JS_EXTENSIONS,
            *GO_EXTENSIONS,
            *PS_EXTENSIONS,
            *RUST_EXTENSIONS,
        }:
            return None
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            return None
        data = full.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def read_python_source(full: Path) -> str | None:
    if full.suffix.lower() != ".py":
        return None
    return read_source_text(full)


def read_js_source(full: Path) -> str | None:
    if full.suffix.lower() not in JS_EXTENSIONS:
        return None
    return read_source_text(full)


def scan_python_file(full: Path) -> tuple[list[dict], list[dict]] | None:
    text = read_python_source(full)
    if text is None:
        return None
    return parse_python_file(text)


def scan_js_file(full: Path) -> tuple[list[dict], list[dict]] | None:
    text = read_js_source(full)
    if text is None:
        return None
    return parse_js_file(text)


def scan_source_file(full: Path) -> tuple[list[dict], list[dict]] | None:
    text = read_source_text(full)
    if text is None:
        return None
    suffix = full.suffix.lower()
    if suffix == ".py":
        return parse_python_file(text)
    if suffix in JS_EXTENSIONS:
        return parse_js_file(text)
    if suffix in GO_EXTENSIONS:
        return parse_go_file(text)
    if suffix in PS_EXTENSIONS:
        return parse_ps_file(text)
    if suffix in RUST_EXTENSIONS:
        return parse_rust_file(text)
    return None
