from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from project_agent.chats import delete_chat, list_chats, load_chat, new_chat_id, save_chat
from project_agent.config import default_config, load_config, needs_api_key, save_config, save_theme
from project_agent.mcp_client import McpHub
from project_agent.patching import apply_diff
from project_agent.paths import MAX_FILE_BYTES, PathError, list_entries, read_text_file, resolve_inside
from project_agent.providers import OpenAIProvider
from project_agent.secrets import Scrubber, SecretVault, looks_like_literal_secret, scrub_outbound
from project_agent.tools import Toolbox
from project_agent.websearch import parse_ddg

SECRET = "supersecretvalue"
KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"


class SecretTests(unittest.TestCase):
    def test_roundtrip_and_plain_code(self):
        vault = SecretVault()
        original = 'token = "supersecretvalue"\n'
        hidden = vault.redact(original)
        self.assertNotIn(SECRET, hidden)
        self.assertEqual(vault.restore(hidden), original)
        plain = "def token_count():\n    return 1\n"
        self.assertEqual(vault.redact(plain), plain)
        self.assertEqual(vault.redact('token = os.getenv("TOKEN")\n'), 'token = os.getenv("TOKEN")\n')

    def test_env_pem_prefix_and_literals(self):
        vault = SecretVault()
        env = "PORT=3000\nAPI_KEY=supersecretvalue\n"
        hidden_env = vault.redact(env, Path(".env"))
        self.assertNotIn(SECRET, hidden_env)
        self.assertIn("PORT=3000", hidden_env)
        self.assertEqual(vault.restore(hidden_env), env)
        pem = "-----BEGIN PRIVATE KEY-----\nABCSECRET\n-----END PRIVATE KEY-----\n"
        hidden_pem = vault.redact(pem)
        self.assertNotIn("ABCSECRET", hidden_pem)
        self.assertEqual(vault.restore(hidden_pem), pem)
        hidden_key = vault.redact(f"mark = '{KEY}'\n")
        self.assertNotIn(KEY, hidden_key)
        scrubber = Scrubber(vault, ["unit-test-key-123456"])
        self.assertNotIn("unit-test-key-123456", scrubber("unit-test-key-123456"))
        self.assertFalse(looks_like_literal_secret("localhost"))
        self.assertTrue(looks_like_literal_secret("secret-value-1"))

    def test_scrub_skips_image_payload(self):
        vault = SecretVault()
        marker = f"xx/sk-abcdefghijklmnopqrstuvwxyz012345"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f'token = "{SECRET}"'},
                    {"type": "image_url", "image_url": {"url": marker}},
                ],
            }
        ]
        cleaned = scrub_outbound(messages, vault.redact)
        blob = json.dumps(cleaned)
        self.assertNotIn(SECRET, blob)
        self.assertIn(marker, blob)


class PathTests(unittest.TestCase):
    def test_escape_and_ignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("outside-secret", encoding="utf-8")
            with self.assertRaises(PathError):
                resolve_inside(root, "../outside.txt")
            with self.assertRaises(PathError):
                resolve_inside(root, str(outside))
            with self.assertRaises(PathError):
                resolve_inside(root, ".git/config")
            inside = resolve_inside(root, "src/main.py")
            self.assertEqual(inside, (root / "src" / "main.py").resolve())
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
            (root / "readme.txt").write_text("hi\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            entries, truncated = list_entries(root)
            self.assertFalse(truncated)
            self.assertEqual([name for name, _is_dir in entries], ["src", "readme.txt"])
            nested, _truncated = list_entries(root, "src")
            self.assertEqual([name for name, _is_dir in nested], ["main.py"])
            (root / "plain.txt").write_bytes(b"print(1)\n")
            self.assertEqual(read_text_file(root, "plain.txt"), "print(1)\n")
            (root / "pic.png").write_bytes(b"not-really")
            (root / "notes.txt").write_bytes(b"abc\x00def")
            (root / "wide.txt").write_bytes("привет".encode("utf-16"))
            (root / "big.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
            for relative in ("pic.png", "notes.txt", "wide.txt", "big.txt", "../outside.txt", ".git/config"):
                with self.assertRaises(PathError):
                    read_text_file(root, relative)


class ChatMarkupTests(unittest.TestCase):
    def test_code_fences_inline_code_and_bold(self):
        from project_agent.app import chat_segments

        text = "Сделайте так:\n```js\nconst X = 2.4;\n```\nСм. `game.js` и **важно**."
        self.assertEqual(
            chat_segments(text),
            [
                ("Сделайте так:\n", ""),
                ("const X = 2.4;\n", "codeblock"),
                ("См. ", ""),
                ("game.js", "code"),
                (" и ", ""),
                ("важно", "bold"),
                (".", ""),
            ],
        )
        self.assertEqual(chat_segments("без разметки"), [("без разметки", "")])
        self.assertEqual(chat_segments("```\n`не код`\n"), [("`не код`\n\n", "codeblock")])

    def test_elide_fits_width(self):
        from project_agent.app import elide

        self.assertEqual(elide("короткий", 100, len), "короткий")
        self.assertEqual(elide("D:\\PythonProject\\GameProject", 10, len), "D:\\Python…")
        self.assertEqual(elide("abc", 0, len), "abc")


class ClipboardTests(unittest.TestCase):
    def test_russian_layout_copies_and_english_keys_stay(self):
        from project_agent.app import apply_layout_clipboard, clipboard_action

        self.assertIsNone(clipboard_action("c", 67))
        self.assertIsNone(clipboard_action("v", 86))
        self.assertIsNone(clipboard_action("x", 88))
        self.assertEqual(clipboard_action("a", 65), "select")
        self.assertEqual(clipboard_action("Cyrillic_es", 67), "copy")
        self.assertEqual(clipboard_action("Cyrillic_em", 86), "paste")
        self.assertEqual(clipboard_action("Cyrillic_che", 88), "cut")
        self.assertEqual(clipboard_action("Cyrillic_ef", 65), "select")
        self.assertEqual(clipboard_action("Cyrillic_a", 67), "copy")
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            source = tk.Text(root)
            source.insert("1.0", "привет")
            source.tag_add("sel", "1.0", "1.6")
            apply_layout_clipboard(source, "copy")
            root.update()
            self.assertEqual(root.clipboard_get(), "привет")
            target = tk.Text(root)
            apply_layout_clipboard(target, "paste")
            root.update()
            self.assertEqual(target.get("1.0", "end-1c"), "привет")
            apply_layout_clipboard(target, "select")
            self.assertEqual(target.get("sel.first", "sel.last"), "привет")
        finally:
            root.destroy()


class PatchTests(unittest.TestCase):
    def test_search_and_unified(self):
        original = "alpha\nbeta\ngamma\n"
        search = "<<<<<<< SEARCH\nbeta\n=======\nbeta2\n>>>>>>> REPLACE\n"
        self.assertEqual(apply_diff(original, search), "alpha\nbeta2\ngamma\n")
        unified = "@@ -1,3 +1,3 @@\n alpha\n-beta\n+beta2\n gamma\n"
        self.assertEqual(apply_diff(original, unified), "alpha\nbeta2\ngamma\n")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()
        self.notes = []
        self.vault = SecretVault()
        self.settings = {
            "provider": "openai",
            "base_url": "",
            "model": "m",
            "api_key": "",
            "image_model": "image-model",
            "image_base_url": "http://images.test/v1",
            "image_api_key": "",
            "mcp_servers": [],
        }
        self.box = Toolbox(self.vault, self._confirm, McpHub(), lambda: self.settings)
        self.box.set_root(self.root)
        self.confirm_result = True

    def tearDown(self):
        self.box.mcp.close()
        self.tmp.cleanup()

    def _confirm(self, path, summary):
        self.notes.append((path, summary))
        return self.confirm_result

    def test_read_hides_secret_and_skips_ignored(self):
        note = self.root / "note.py"
        note.write_text(f'token = "{SECRET}"\nmark = "{KEY}"\n', encoding="utf-8")
        nested = self.root / "node_modules" / "pkg"
        nested.mkdir(parents=True)
        (nested / "secret.txt").write_text("only-inside-modules", encoding="utf-8")
        (self.root / ".git").mkdir()
        outcome = self.box.execute("read_file", {"path": "note.py"})
        self.assertNotIn(SECRET, outcome.model_text)
        self.assertNotIn(KEY, outcome.model_text)
        self.assertNotIn(SECRET, outcome.journal)
        listed = self.box.execute("list_dir", {"path": "."})
        self.assertNotIn("node_modules", listed.model_text)
        self.assertNotIn(".git", listed.model_text)
        found = self.box.execute("search", {"query": SECRET})
        self.assertNotIn(SECRET, found.model_text)
        self.assertIn("note.py", found.model_text)
        ignored = self.box.execute("search", {"query": "only-inside-modules"})
        self.assertIn("Совпадений нет", ignored.model_text)

    def test_write_requires_confirmation_and_restores(self):
        hidden = self.vault.redact(f'token = "{SECRET}"\n')
        self.confirm_result = False
        refused = self.box.execute(
            "write_file",
            {"path": "out.py", "content": hidden, "summary": f'token = "{SECRET}"'},
        )
        self.assertFalse((self.root / "out.py").exists())
        self.assertIn("отказался", refused.model_text)
        self.assertNotIn(SECRET, self.notes[-1][1])
        self.confirm_result = True
        self.box.execute("write_file", {"path": "out.py", "content": hidden})
        self.assertEqual((self.root / "out.py").read_text(encoding="utf-8"), f'token = "{SECRET}"\n')
        denied = self.box.execute("write_file", {"path": ".git/config", "content": "x"})
        self.assertIn("служебный", denied.model_text)
        self.assertFalse((self.root / ".git" / "config").exists())

    def test_patch_and_binary_and_escape(self):
        target = self.root / "app.py"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        self.confirm_result = True
        diff = "<<<<<<< SEARCH\nbeta\n=======\nbeta2\n>>>>>>> REPLACE\n"
        outcome = self.box.execute("apply_patch", {"path": "app.py", "diff": diff})
        self.assertIn("записано", outcome.journal)
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nbeta2\ngamma\n")
        binary = self.root / "data.bin"
        binary.write_bytes(b"\x00" + SECRET.encode())
        skipped = self.box.execute("read_file", {"path": "data.bin"})
        self.assertIn("пропущен", skipped.model_text)
        self.assertNotIn(SECRET, skipped.model_text)
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("outside-secret", encoding="utf-8")
        escaped = self.box.execute("read_file", {"path": "../outside.txt"})
        self.assertNotIn("outside-secret", escaped.model_text)

    def test_web_and_image_do_not_receive_secrets(self):
        import project_agent.tools as tools_mod

        queries = []

        def fake_search(query, limit=5):
            queries.append(query)
            return [{"title": "t", "url": "https://example.com", "snippet": SECRET}]

        original = tools_mod.web_search
        tools_mod.web_search = fake_search
        try:
            outcome = self.box.execute("web_search", {"query": f'password = "{SECRET}" docs'})
        finally:
            tools_mod.web_search = original
        self.assertNotIn(SECRET, queries[0])
        self.assertNotIn(SECRET, outcome.model_text)
        prompts = []

        def fake_image(base, key, model, prompt):
            prompts.append(prompt)
            return b"\x89PNG\r\n\x1a\nfake"

        self.box.image_request = fake_image
        self.confirm_result = False
        refused = self.box.execute(
            "generate_image",
            {"prompt": f"cat {SECRET}", "path": "pic.png"},
        )
        self.assertEqual(prompts, [])
        self.assertIn("отказался", refused.model_text)
        self.confirm_result = True
        saved = self.box.execute("generate_image", {"prompt": f"cat {SECRET}", "path": "pic.png"})
        self.assertNotIn(SECRET, prompts[0])
        self.assertTrue((self.root / "pic.png").exists())
        self.assertIn("сохранено", saved.journal)


class McpTests(unittest.TestCase):
    def test_call_does_not_pass_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            script = folder / "server.py"
            dump = folder / "args.json"
            script.write_text(SERVER, encoding="utf-8")
            hub = McpHub()
            try:
                hub.set_servers(
                    [
                        {
                            "name": "fake",
                            "command": sys.executable,
                            "args": [str(script), str(dump)],
                            "env": {},
                        }
                    ]
                )
                box = Toolbox(SecretVault(), lambda *_: True, hub, lambda: {"api_key": "", "mcp_servers": []})
                box.set_root(folder)
                listed = box.execute("list_mcp_tools", {})
                self.assertIn("fake/echo", listed.model_text)
                outcome = box.execute(
                    "call_mcp_tool",
                    {
                        "server": "fake",
                        "tool": "echo",
                        "arguments": {"note": f'token = "{SECRET}"'},
                    },
                )
                echoed = dump.read_text(encoding="utf-8")
                self.assertNotIn(SECRET, echoed)
                self.assertNotIn(SECRET, outcome.model_text)
            finally:
                hub.close()


class ProviderTests(unittest.TestCase):
    def test_openai_request_hides_secret(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        provider = OpenAIProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "unit-test-key-123456", "base_url": "http://example.test/v1"})
        provider.set_system("helper")
        vault = SecretVault()
        marker = "zz/sk-abcdefghijklmnopqrstuvwxyz012345"
        provider.add_user(
            f'token = "{SECRET}"',
            [{"media_type": "image/jpeg", "b64": marker}],
        )
        turn = provider.complete([], Scrubber(vault, ["unit-test-key-123456"]))
        self.assertEqual(turn.text, "ok")
        self.assertNotIn(SECRET, seen["body"])
        self.assertNotIn("unit-test-key-123456", seen["body"])
        self.assertIn(marker, seen["body"])
        self.assertEqual(seen["auth"], "Bearer unit-test-key-123456")
        provider._http.close()

    def test_anthropic_request_hides_secret(self):
        from project_agent.providers import AnthropicProvider

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content.decode()
            seen["key"] = request.headers.get("x-api-key")
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
            )

        provider = AnthropicProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "unit-test-key-123456", "base_url": "https://api.anthropic.com"})
        provider.set_system("helper")
        provider.add_user(f'token = "{SECRET}"', None)
        turn = provider.complete([], Scrubber(SecretVault(), ["unit-test-key-123456"]))
        self.assertEqual(turn.text, "ok")
        self.assertNotIn(SECRET, seen["body"])
        self.assertNotIn("unit-test-key-123456", seen["body"])
        self.assertEqual(seen["key"], "unit-test-key-123456")
        provider._http.close()


class ChatStoreTests(unittest.TestCase):
    def test_roundtrip_list_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                first = Path(tmp) / "one"
                second = Path(tmp) / "two"
                first.mkdir()
                second.mkdir()
                chat_id = new_chat_id()
                save_chat(chat_id, "Привет", str(first), ["Вы: привет", "· list_dir .: 1 элементов"])
                save_chat(new_chat_id(), "Другой", str(second), ["Вы: там"])
                listed = list_chats(str(first))
                self.assertEqual([item["id"] for item in listed], [chat_id])
                loaded = load_chat(chat_id)
                self.assertEqual(loaded["lines"][0], "Вы: привет")
                delete_chat(chat_id)
                self.assertIsNone(load_chat(chat_id))
                self.assertEqual(list_chats(str(first)), [])
                with self.assertRaises(ValueError):
                    delete_chat("../escape")
            finally:
                if previous is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = previous


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = default_config()
            data["model"] = "demo"
            data["api_key"] = "unit-test-key-123456"
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["api_key"], "unit-test-key-123456")
            self.assertEqual(loaded["model"], "demo")
            self.assertEqual(loaded["profiles"], [])
            data["model"] = "other"
            data["active_profile"] = "OpenRouter"
            data["profiles"] = [
                {
                    "name": "OpenRouter",
                    "provider": "openai",
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "openrouter/free",
                    "api_key": "unit-test-key-123456",
                    "max_steps": 10,
                },
                {"name": "  ", "model": "skip"},
            ]
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["active_profile"], "OpenRouter")
            self.assertEqual(loaded["model"], "openrouter/free")
            self.assertEqual(loaded["max_steps"], 10)
            self.assertEqual(len(loaded["profiles"]), 1)
            self.assertEqual(loaded["project_dir"], "")
        example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(example), set(default_config()))
        self.assertEqual(example["api_key"], "")
        self.assertEqual(example["image_api_key"], "")
        self.assertEqual(example["theme"], "")
        self.assertEqual(example["mcp_servers"], [])
        self.assertTrue(needs_api_key("openai", "", ""))
        self.assertFalse(needs_api_key("openai", "http://localhost:11434/v1", ""))

    def test_theme_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = default_config()
            data["model"] = "keep-me"
            data["api_key"] = "unit-test-key-123456"
            data["theme"] = "light"
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["theme"], "light")
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw.pop("theme")
            raw["extra"] = "stay"
            path.write_text(json.dumps(raw), encoding="utf-8")
            loaded, error = load_config(path)
            self.assertEqual(loaded["theme"], "dark")
            raw["theme"] = "blue"
            path.write_text(json.dumps(raw), encoding="utf-8")
            loaded, error = load_config(path)
            self.assertEqual(loaded["theme"], "dark")
            save_theme("light", path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["theme"], "light")
            self.assertEqual(raw["model"], "keep-me")
            self.assertEqual(raw["api_key"], "unit-test-key-123456")
            self.assertEqual(raw["extra"], "stay")
            save_theme("nope", path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["theme"], "dark")
            self.assertEqual(raw["extra"], "stay")
            fresh = Path(tmp) / "fresh.json"
            save_theme("light", fresh)
            loaded, error = load_config(fresh)
            self.assertIsNone(error)
            self.assertEqual(loaded["theme"], "light")
            self.assertEqual(loaded["api_key"], "")
            bad = Path(tmp) / "bad.json"
            bad.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                save_theme("light", bad)
            self.assertEqual(bad.read_text(encoding="utf-8"), "{")

    def test_sources_have_no_live_keys(self):
        import re

        forbidden = (
            re.compile(r"sk-proj-[A-Za-z0-9]"),
            re.compile(r"sk-ant-api\d"),
            re.compile(r"ghp_[A-Za-z0-9]{20,}"),
        )
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json", ".md", ".txt", ".ps1"}:
                continue
            if "tests" in path.parts or ".venv" in path.parts or "build" in path.parts or "dist" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(pattern.search(text), path)


class SearchParseTests(unittest.TestCase):
    def test_parse_lite_html(self):
        html = """
        <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs&amp;rut=1" class="result-link">Example Docs</a>
        <td class="result-snippet">Useful <b>text</b></td>
        """
        results = parse_ddg(html)
        self.assertEqual(results[0]["url"], "https://example.com/docs")
        self.assertEqual(results[0]["title"], "Example Docs")
        self.assertIn("Useful", results[0]["snippet"])


class AgentLoopTests(unittest.TestCase):
    def test_read_is_not_sent_in_clear(self):
        from project_agent.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "note.py").write_text(f'token = "{SECRET}"\nmark = "{KEY}"\n', encoding="utf-8")
            bodies = []

            def handler(request: httpx.Request) -> httpx.Response:
                bodies.append(request.content.decode())
                if len(bodies) == 1:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path":"note.py"}'},
                            }
                        ],
                    }
                else:
                    message = {"role": "assistant", "content": "готово"}
                return httpx.Response(200, json={"choices": [{"message": message}]})

            chats, journals = [], []
            vault = SecretVault()
            hub = McpHub()
            box = Toolbox(vault, lambda *_: False, hub, lambda: {"api_key": "", "mcp_servers": []})
            agent = Agent(vault, box, hub, chats.append, journals.append, lambda _status: None)
            try:
                agent.set_root(root)
                provider = OpenAIProvider()
                provider._transport = httpx.MockTransport(handler)
                agent.provider = provider
                agent.provider_kind = "openai"
                agent.run_turn(
                    "прочитай note.py",
                    [],
                    {"provider": "openai", "model": "m", "api_key": "", "base_url": "http://example.test/v1", "mcp_servers": []},
                    threading.Event(),
                )
            finally:
                hub.close()
            self.assertGreaterEqual(len(bodies), 2)
            combined = "\n".join(bodies)
            self.assertNotIn(SECRET, combined)
            self.assertNotIn(KEY, combined)
            self.assertTrue(any("read_file" in line for line in journals))
            self.assertTrue(any("готово" in line for line in chats))


SERVER = """
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))

def write_message(payload):
    data = json.dumps(payload).encode("utf-8")
    packet = f"Content-Length: {len(data)}\\r\\n\\r\\n".encode("ascii") + data
    sys.stdout.buffer.write(packet)
    sys.stdout.buffer.flush()

dump = sys.argv[1]
while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "0"},
            },
        })
    elif method == "tools/list":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object"}}]},
        })
    elif method == "tools/call":
        arguments = (message.get("params") or {}).get("arguments") or {}
        open(dump, "w", encoding="utf-8").write(json.dumps(arguments))
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": json.dumps(arguments)}]},
        })
    elif "id" in message:
        write_message({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "no"}})
"""


if __name__ == "__main__":
    unittest.main()
