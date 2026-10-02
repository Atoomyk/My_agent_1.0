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
from project_agent.providers import AnthropicProvider, OpenAIProvider, Stopped
from project_agent.secrets import Scrubber, SecretVault, looks_like_literal_secret, scrub_outbound
from project_agent.testing import (
    clip_output,
    normalize_fix_rounds,
    normalize_preset,
    normalize_timeout,
    preset_command,
)
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
        commented = (
            "# just a note\n"
            "#SFERUM_TOKEN_LRMIAC=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJvcmdhbml6YXRpb25faWQiOjI5Njc1NCwidmVuZG9yIjoid\n"
            "# API_KEY=supersecretvalue\n"
            "# PORT=3000\n"
        )
        hidden_commented = vault.redact(commented, Path(".env"))
        self.assertNotIn("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9", hidden_commented)
        self.assertNotIn(SECRET, hidden_commented)
        self.assertIn("# just a note", hidden_commented)
        self.assertIn("# PORT=3000", hidden_commented)
        self.assertIn("#SFERUM_TOKEN_LRMIAC=", hidden_commented)
        self.assertEqual(vault.restore(hidden_commented), commented)
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

    def test_chat_role_and_body(self):
        from project_agent.app import chat_body, chat_role

        self.assertEqual(chat_role("Вы: привет"), "user")
        self.assertEqual(chat_role("· list_dir"), "tool")
        self.assertEqual(chat_role("готово"), "agent")
        self.assertEqual(chat_role("Ошибка API: таймаут"), "error")
        self.assertEqual(chat_role("Ошибка: обрыв"), "error")
        self.assertEqual(chat_body("Вы: привет"), "привет")
        self.assertEqual(chat_body("  Вы:  задача\nвторая"), "задача\nвторая")
        self.assertEqual(chat_body("ответ модели"), "ответ модели")

    def test_user_copy_text_drops_attachment_tail(self):
        from project_agent.app import user_copy_text

        self.assertEqual(user_copy_text("привет"), "привет")
        self.assertEqual(
            user_copy_text("сделай\n(папки: src)\n(файлы: a.py, b.py)\n(изображений: 2)"),
            "сделай",
        )
        self.assertEqual(
            user_copy_text("первая\nвторая\n(файлы: a.py)"),
            "первая\nвторая",
        )
        self.assertEqual(user_copy_text("(только изображение)\n(изображений: 1)"), "(только изображение)")
        self.assertEqual(user_copy_text("(контекст)\n(папки: src)"), "(контекст)")
        self.assertEqual(user_copy_text("см. (файлы: a.py) в тексте\nхвост"), "см. (файлы: a.py) в тексте\nхвост")


class ClipboardTests(unittest.TestCase):
    def test_russian_layout_copies_and_english_keys_stay(self):
        from project_agent.app import apply_layout_clipboard, clipboard_action, set_clipboard

        self.assertEqual(clipboard_action("c", 67), "copy")
        self.assertEqual(clipboard_action("v", 86), "paste")
        self.assertEqual(clipboard_action("x", 88), "cut")
        self.assertEqual(clipboard_action("a", 65), "select")
        self.assertEqual(clipboard_action("Cyrillic_es", 67), "copy")
        self.assertEqual(clipboard_action("Cyrillic_em", 86), "paste")
        self.assertEqual(clipboard_action("Cyrillic_che", 88), "cut")
        self.assertEqual(clipboard_action("Cyrillic_ef", 65), "select")
        self.assertEqual(clipboard_action("Cyrillic_a", 67), "copy")
        self.assertEqual(clipboard_action("unknown", 67), "copy")
        self.assertEqual(clipboard_action("unknown", 86), "paste")
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
            # Как после ПКМ: выделение уже сброшено, текст передан явно.
            source.tag_remove("sel", "1.0", "end")
            apply_layout_clipboard(source, "copy", "ответ ИИ")
            root.update()
            self.assertEqual(root.clipboard_get(), "ответ ИИ")
            set_clipboard(source, "буфер")
            root.update()
            self.assertEqual(root.clipboard_get(), "буфер")
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

    def test_auto_write_skips_confirm(self):
        self.settings["auto_write_project"] = True
        before = len(self.notes)
        self.confirm_result = False
        outcome = self.box.execute("write_file", {"path": "auto.py", "content": "ok\n", "summary": "auto"})
        self.assertEqual(len(self.notes), before)
        self.assertIn("записано", outcome.journal)
        self.assertEqual((self.root / "auto.py").read_text(encoding="utf-8"), "ok\n")
        target = self.root / "auto.py"
        diff = "<<<<<<< SEARCH\nok\n=======\nok2\n>>>>>>> REPLACE\n"
        patched = self.box.execute("apply_patch", {"path": "auto.py", "diff": diff, "summary": "patch"})
        self.assertEqual(len(self.notes), before)
        self.assertIn("записано", patched.journal)
        self.assertEqual(target.read_text(encoding="utf-8"), "ok2\n")
        self.settings["auto_write_project"] = False
        refused = self.box.execute("write_file", {"path": "auto.py", "content": "no\n", "summary": "back"})
        self.assertEqual(len(self.notes), before + 1)
        self.assertIn("отказался", refused.model_text)
        self.assertEqual(target.read_text(encoding="utf-8"), "ok2\n")

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


class TestRunnerTests(unittest.TestCase):
    def test_preset_and_clip(self):
        self.assertEqual(normalize_preset("pytest"), "pytest")
        self.assertEqual(normalize_preset("npm"), "npm")
        self.assertEqual(normalize_preset("rm -rf"), "")
        self.assertEqual(normalize_timeout(5), 15)
        self.assertEqual(normalize_timeout(900), 600)
        self.assertEqual(normalize_fix_rounds(0), 1)
        self.assertEqual(normalize_fix_rounds(99), 10)
        command = preset_command("unittest")
        self.assertIn("-m", command)
        self.assertIn("unittest", command)
        with self.assertRaises(ValueError):
            preset_command("")
        from unittest import mock
        import subprocess as sp

        npm_path = r"C:\Program Files\nodejs\npm.cmd"

        def which_win(name):
            if name == "npm.cmd":
                return npm_path
            return None

        with mock.patch("project_agent.testing.shutil.which", side_effect=which_win):
            with mock.patch("project_agent.testing.os.name", "nt"):
                with mock.patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}, clear=False):
                    npm_cmd = preset_command("npm")
        self.assertIsInstance(npm_cmd, str)
        expected = (
            sp.list2cmdline([r"C:\Windows\System32\cmd.exe", "/d", "/c"])
            + " "
            + sp.list2cmdline(["call", npm_path, "test"])
        )
        self.assertEqual(npm_cmd, expected)
        self.assertIn('"C:\\Program Files\\nodejs\\npm.cmd"', npm_cmd)
        self.assertNotIn('\\"', npm_cmd)
        with mock.patch("project_agent.testing.shutil.which", return_value="/usr/bin/npm"):
            with mock.patch("project_agent.testing.os.name", "posix"):
                unix_cmd = preset_command("npm")
        self.assertEqual(unix_cmd, ["/usr/bin/npm", "test"])
        with mock.patch("project_agent.testing.shutil.which", return_value=None):
            with mock.patch("project_agent.testing.os.name", "posix"):
                with self.assertRaises(ValueError):
                    preset_command("npm")
        long = "a" * 20_000 + "MID" + "b" * 20_000
        clipped = clip_output(long, 100)
        self.assertLessEqual(len(clipped), 110)
        self.assertIn("…", clipped)

    def test_run_tests_tool_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "tests").mkdir()
            (root / "tests" / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            notes = []
            settings = {
                "test_preset": "",
                "test_timeout": 60,
                "api_key": "",
                "mcp_servers": [],
            }
            box = Toolbox(
                SecretVault(),
                lambda path, summary: notes.append((path, summary)) or True,
                McpHub(),
                lambda: settings,
            )
            box.set_root(root)
            disabled = box.execute("run_tests", {})
            self.assertIn("выключен", disabled.model_text.lower())
            settings["test_preset"] = "unittest"
            box2 = Toolbox(SecretVault(), lambda *_: False, McpHub(), lambda: settings)
            box2.set_root(root)
            refused = box2.execute("run_tests", {"summary": "check"})
            self.assertIn("отказался", refused.model_text)
            ok = box.execute("run_tests", {"summary": "check"})
            self.assertTrue(notes)
            self.assertIn("OK", ok.model_text)
            self.assertIn("run_tests", ok.journal)

    def test_run_tests_respects_round_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "tests").mkdir()
            (root / "tests" / "test_ok.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            settings = {
                "test_preset": "unittest",
                "test_timeout": 60,
                "test_fix_rounds": 2,
                "api_key": "",
                "mcp_servers": [],
            }
            box = Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: settings)
            box.set_root(root)
            box.begin_turn()
            self.assertIn("OK", box.execute("run_tests", {}).model_text)
            self.assertIn("OK", box.execute("run_tests", {}).model_text)
            limited = box.execute("run_tests", {})
            self.assertIn("Лимит", limited.model_text)
            self.assertIn("лимит", limited.journal)

    def test_fail_fingerprint_stable_and_repeat_locks(self):
        from project_agent.testing import fail_fingerprint

        out_a = (
            "test_x (tests.test_mod.T) ... FAIL\n"
            "======================================================================\n"
            "FAIL: test_x (tests.test_mod.T)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_mod.py", line 4, in test_x\n'
            "AssertionError: 1 != 2\n"
            "Ran 1 test in 0.012s\n"
        )
        out_b = out_a.replace("0.012s", "0.991s")
        self.assertEqual(fail_fingerprint(out_a, 1), fail_fingerprint(out_b, 1))
        self.assertIn("FAIL", fail_fingerprint(out_a, 1))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "tests").mkdir()
            bad = root / "tests" / "test_bad.py"
            bad.write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )
            settings = {
                "test_preset": "unittest",
                "test_timeout": 60,
                "test_fix_rounds": 5,
                "api_key": "",
                "mcp_servers": [],
            }
            box = Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: settings)
            box.set_root(root)
            box.begin_turn()
            first = box.execute("run_tests", {})
            self.assertIn("FAIL", first.model_text)
            self.assertFalse(box.fail_locked)
            second = box.execute("run_tests", {})
            self.assertIn("СТОП", second.model_text)
            self.assertTrue(box.fail_locked)
            third = box.execute("run_tests", {})
            self.assertIn("СТОП", third.model_text)
            self.assertIn("стоп повтор", third.journal)

    def test_write_tracks_touched_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            box = Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: {"api_key": "", "mcp_servers": []})
            box.set_root(root)
            box.begin_turn()
            out = box.execute("write_file", {"path": "a.py", "content": "x=1\n", "summary": "add"})
            self.assertIn("Тронутые за ход: a.py", out.model_text)
            self.assertEqual(box.touched_paths(), ["a.py"])


class GitOpsTests(unittest.TestCase):
    def test_status_diff_log_and_commit_confirm(self):
        import shutil
        import subprocess

        from project_agent.gitops import git_diff, git_log, git_status, preview_unified

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            git = shutil.which("git")
            if not git:
                self.skipTest("git not installed")
            flags = {"cwd": str(root), "check": True, "capture_output": True, "text": True}
            subprocess.run([git, "init"], **flags)
            subprocess.run([git, "config", "user.email", "t@example.com"], **flags)
            subprocess.run([git, "config", "user.name", "Test"], **flags)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            subprocess.run([git, "add", "a.txt"], **flags)
            subprocess.run([git, "commit", "-m", "init"], **flags)
            (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
            status = git_status(root)
            self.assertEqual(status.code, 0)
            self.assertIn("a.txt", status.output)
            diff = git_diff(root, "a.txt")
            self.assertIn("+two", diff.output)
            log = git_log(root, 5)
            self.assertIn("init", log.output)
            preview = preview_unified("one\n", "one\ntwo\n", "a.txt")
            self.assertIn("+two", preview)
            notes = []
            box = Toolbox(
                SecretVault(),
                lambda path, summary, detail="": notes.append((path, summary, detail)) or True,
                McpHub(),
                lambda: {"api_key": "", "mcp_servers": []},
            )
            box.set_root(root)
            refused = Toolbox(
                SecretVault(),
                lambda *_args, **_kw: False,
                McpHub(),
                lambda: {"api_key": "", "mcp_servers": []},
            )
            refused.set_root(root)
            deny = refused.execute("git", {"action": "commit", "message": "x", "add_all": True})
            self.assertIn("отказался", deny.model_text)
            ok = box.execute(
                "git",
                {"action": "commit", "message": "add two", "paths": "a.txt", "summary": "save"},
            )
            self.assertTrue(notes)
            self.assertIn("git commit", notes[0][0])
            self.assertIn("add two", notes[0][2])
            self.assertIn("git commit: ok", ok.journal)
            clean = box.execute("git", {"action": "status"})
            self.assertNotIn("a.txt", clean.model_text)


class IndexStoreTests(unittest.TestCase):
    def test_build_find_and_touch(self):
        from project_agent.index_store import build_index, find_paths, index_summary, load_index, touch_file

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                root = Path(tmp) / "proj"
                root.mkdir()
                (root / "src").mkdir()
                (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
                (root / "readme.md").write_text("hi\n", encoding="utf-8")
                (root / ".venv").mkdir()
                (root / ".venv" / "x.py").write_text("skip\n", encoding="utf-8")
                (root / "node_modules").mkdir()
                (root / "node_modules" / "pkg.js").write_text("skip\n", encoding="utf-8")
                data = build_index(root)
                paths = {item["path"] for item in data["files"]}
                self.assertIn("src/app.py", paths)
                self.assertIn("readme.md", paths)
                self.assertNotIn(".venv/x.py", paths)
                self.assertNotIn("node_modules/pkg.js", paths)
                found, _data = find_paths(root, "app.py")
                self.assertEqual(found, ["src/app.py"])
                self.assertIn("файлов", index_summary(root))
                (root / "src" / "app.py").write_text("print(2)\n", encoding="utf-8")
                touch_file(root, "src/app.py")
                loaded = load_index(root)
                entry = next(item for item in loaded["files"] if item["path"] == "src/app.py")
                self.assertEqual(entry["size"], (root / "src" / "app.py").stat().st_size)
            finally:
                if previous is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = previous

    def test_project_index_tool(self):
        from project_agent.index_store import build_index

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                root = Path(tmp) / "proj"
                root.mkdir()
                (root / "a.py").write_text("x\n", encoding="utf-8")
                build_index(root)
                box = Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: {"api_key": "", "mcp_servers": []})
                box.set_root(root)
                summary = box.execute("project_index", {"action": "summary"})
                self.assertIn("файлов", summary.model_text)
                found = box.execute("project_index", {"action": "find", "query": "a.py"})
                self.assertIn("a.py", found.model_text)
            finally:
                if previous is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = previous


class ContextAttachTests(unittest.TestCase):
    def test_parse_and_load_context(self):
        from project_agent.context_attach import (
            at_token_at_end,
            compose_user_text,
            find_at_targets,
            load_context_files,
            load_explicit_context,
            merge_paths,
            parse_at_paths,
        )

        self.assertEqual(parse_at_paths("смотри @src/a.py и @\"docs/x y.md\""), ["src/a.py", "docs/x y.md"])
        self.assertEqual(at_token_at_end("привет @util"), ("util", 7))
        self.assertIsNone(at_token_at_end("без упоминания"))
        self.assertEqual(merge_paths(["a.py", "b.py"], ["a.py", "c.py"], limit=2), ["a.py", "b.py"])
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                root = Path(tmp) / "proj"
                root.mkdir()
                (root / "a.py").write_text("print(1)\n", encoding="utf-8")
                (root / "secret.bin").write_bytes(b"\x00\x01\x02")
                src = root / "src"
                src.mkdir()
                (src / "main.py").write_text("x=1\n", encoding="utf-8")
                vault = SecretVault()
                block, loaded, errors = load_context_files(root, ["a.py", "missing.py", "secret.bin"], vault)
                self.assertIn("a.py", loaded)
                self.assertIn("### a.py", block)
                self.assertIn("print(1)", block)
                self.assertTrue(any("missing.py" in item for item in errors))
                self.assertTrue(any("secret.bin" in item for item in errors))
                text = compose_user_text("исправь", block)
                self.assertIn("исправь", text)
                self.assertIn("Приложенные файлы", text)
                from project_agent.index_store import build_index

                build_index(root)
                mixed, files, dirs, mixed_errors = load_explicit_context(root, ["a.py", "src"], vault)
                self.assertIn("a.py", files)
                self.assertTrue(any(item.rstrip("/") == "src" for item in dirs))
                self.assertIn("Приложенные папки", mixed)
                self.assertIn("main.py", mixed)
                targets = find_at_targets(root, "src", limit=8)
                self.assertTrue(any(item.rstrip("/") == "src" or item.startswith("src/") for item in targets))
            finally:
                if previous is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = previous


class ProjectRulesTests(unittest.TestCase):
    def test_load_agents_and_dir_rules(self):
        from project_agent.rules import load_project_rules, rules_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            self.assertEqual(rules_summary(root), "Правила не найдены (AGENTS.md или .projectagent/rules).")
            (root / "AGENTS.md").write_text("Пиши кратко.\n", encoding="utf-8")
            rules_dir = root / ".projectagent" / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "style.md").write_text("Без эмодзи.\n", encoding="utf-8")
            (rules_dir / "extra.txt").write_text("Тесты обязательны.\n", encoding="utf-8")
            block, sources = load_project_rules(root)
            self.assertIn("AGENTS.md", sources)
            self.assertIn(".projectagent/rules/extra.txt", sources)
            self.assertIn(".projectagent/rules/style.md", sources)
            self.assertIn("Пиши кратко.", block)
            self.assertIn("Без эмодзи.", block)
            self.assertTrue(rules_summary(root).startswith("Правила:"))
            from project_agent.agent import Agent

            agent = Agent(SecretVault(), Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: {}), McpHub(), lambda *_: None, lambda *_: None, lambda *_: None)
            agent.set_root(root)
            system = agent._system()
            self.assertIn("Правила проекта", system)
            self.assertIn("Пиши кратко.", system)
            self.assertIn("Режим: Agent", system)
            agent._agent_mode = "ask"
            ask_system = agent._system()
            self.assertIn("Режим: Ask", ask_system)
            self.assertIn("Пиши кратко.", ask_system)


class AgentModeTests(unittest.TestCase):
    def test_tools_filter_and_ask_blocks_writes(self):
        from project_agent.tools import ASK_TOOL_NAMES, normalize_agent_mode, tools_for_mode

        self.assertEqual(normalize_agent_mode("ASK"), "ask")
        self.assertEqual(normalize_agent_mode("nope"), "agent")
        ask_names = {spec["name"] for spec in tools_for_mode("ask")}
        self.assertEqual(ask_names, set(ASK_TOOL_NAMES))
        self.assertNotIn("write_file", ask_names)
        self.assertNotIn("browser", ask_names)
        agent_names = {spec["name"] for spec in tools_for_mode("agent")}
        self.assertIn("write_file", agent_names)
        self.assertIn("browser", agent_names)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            settings = {"api_key": "", "mcp_servers": [], "agent_mode": "ask"}
            box = Toolbox(SecretVault(), lambda *_: True, McpHub(), lambda: settings)
            box.set_root(root)
            blocked = box.execute("write_file", {"path": "a.py", "content": "x=1\n", "summary": "x"})
            self.assertIn("Ask", blocked.model_text)
            self.assertFalse((root / "a.py").exists())
            commit = box.execute("git", {"action": "commit", "message": "x", "add_all": True})
            self.assertIn("Ask", commit.model_text)


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

    def test_browser_preset_and_tool_routing(self):
        from project_agent.browser_mcp import (
            PLAYWRIGHT_SERVER,
            find_browser_server_name,
            map_browser_arguments,
        )

        self.assertEqual(PLAYWRIGHT_SERVER["command"], "npx")
        self.assertIn("@playwright/mcp", PLAYWRIGHT_SERVER["args"][0])
        self.assertEqual(
            find_browser_server_name([{"name": "playwright"}, {"name": "other"}]),
            "playwright",
        )
        self.assertIsNone(find_browser_server_name([{"name": "filesystem"}]))
        tool, payload = map_browser_arguments("navigate", {"url": "https://example.com"})
        self.assertEqual(tool, "browser_navigate")
        self.assertEqual(payload, {"url": "https://example.com"})
        tool2, payload2 = map_browser_arguments("click", {"ref": "e5", "element": "Submit"})
        self.assertEqual(tool2, "browser_click")
        self.assertEqual(payload2["ref"], "e5")

        class FakeHub:
            def __init__(self):
                self.calls = []

            def call(self, server, tool, arguments, timeout=None):
                self.calls.append((server, tool, arguments, timeout))
                return {"content": [{"type": "text", "text": "snapshot ok"}]}

            def list_tools(self):
                return "playwright/browser_navigate: go"

        hub = FakeHub()
        settings = {
            "api_key": "",
            "mcp_servers": [dict(PLAYWRIGHT_SERVER)],
        }
        box = Toolbox(SecretVault(), lambda *_: True, hub, lambda: settings)
        missing = Toolbox(SecretVault(), lambda *_: True, FakeHub(), lambda: {"api_key": "", "mcp_servers": []})
        self.assertIn("не настроен", missing.execute("browser", {"action": "navigate", "url": "https://x"}).model_text)
        out = box.execute("browser", {"action": "snapshot"})
        self.assertIn("snapshot ok", out.model_text)
        self.assertEqual(hub.calls[0][0], "playwright")
        self.assertEqual(hub.calls[0][1], "browser_snapshot")
        self.assertIn("browser snapshot", out.journal)


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

    def test_openai_stream_collects_text_tools_and_usage(self):
        events = [
            {"choices": [{"delta": {"content": "Смотрю"}}]},
            {"choices": [{"delta": {"content": " файл"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"note.py"}'}}]}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            },
        ]
        pieces = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            self.assertTrue(body.get("stream"))
            self.assertIn("stream_options", body)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(events),
            )

        provider = OpenAIProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "", "base_url": "http://example.test/v1"})
        provider.set_system("helper")
        provider.add_user("прочитай", None)
        try:
            turn = provider.complete([], Scrubber(SecretVault(), []), lambda piece: pieces.append(piece))
        finally:
            provider._http.close()
        self.assertEqual(pieces, ["Смотрю", " файл"])
        self.assertEqual(turn.text, "Смотрю файл")
        self.assertEqual(turn.prompt_tokens, 50)
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "read_file")
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "note.py"})
        stored = provider.messages[-1]
        self.assertEqual(stored["content"], "Смотрю файл")
        self.assertEqual(stored["tool_calls"][0]["id"], "call-1")
        self.assertIn("note.py", stored["tool_calls"][0]["function"]["arguments"])

    def test_openai_stream_stop_keeps_partial(self):
        events = [
            {"choices": [{"delta": {"content": "Раз"}}]},
            {"choices": [{"delta": {"content": " Два"}}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(events),
            )

        provider = OpenAIProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "", "base_url": "http://example.test/v1"})
        provider.set_system("helper")
        provider.add_user("вопрос", None)

        def on_text(_piece: str) -> None:
            provider._cancel.set()

        try:
            with self.assertRaises(Stopped) as caught:
                provider.complete([], Scrubber(SecretVault(), []), on_text)
        finally:
            provider._http.close()
        self.assertEqual(caught.exception.partial, "Раз")
        self.assertEqual(provider.messages[-1]["content"], "Раз")
        self.assertNotIn("tool_calls", provider.messages[-1])

    def test_openai_retries_without_stream_options(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            seen.append("stream_options" in body)
            if "stream_options" in body:
                return httpx.Response(
                    400,
                    json={"error": {"message": "Unrecognized request argument supplied: stream_options"}},
                )
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        provider = OpenAIProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "", "base_url": "http://example.test/v1"})
        provider.set_system("helper")
        provider.add_user("вопрос", None)
        try:
            turn = provider.complete([], Scrubber(SecretVault(), []))
        finally:
            provider._http.close()
        self.assertEqual(seen, [True, False])
        self.assertEqual(turn.text, "ok")

    def test_anthropic_stream_collects_text_and_tool(self):
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 15, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Привет"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"a.py"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
            {"type": "message_stop"},
        ]
        pieces = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            self.assertTrue(body.get("stream"))
            parts = []
            for event in events:
                parts.append(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(parts).encode("utf-8"),
            )

        provider = AnthropicProvider()
        provider._transport = httpx.MockTransport(handler)
        provider.configure({"model": "m", "api_key": "unit-test-key-123456", "base_url": "https://api.anthropic.com"})
        provider.set_system("helper")
        provider.add_user("прочитай", None)
        try:
            turn = provider.complete([], Scrubber(SecretVault(), ["unit-test-key-123456"]), pieces.append)
        finally:
            provider._http.close()
        self.assertEqual(pieces, ["Привет"])
        self.assertEqual(turn.text, "Привет")
        self.assertEqual(turn.prompt_tokens, 15)
        self.assertEqual(turn.tool_calls[0].name, "read_file")
        self.assertEqual(turn.tool_calls[0].arguments, {"path": "a.py"})
        self.assertEqual(turn.tool_calls[0].id, "toolu_1")
        stored = provider.messages[-1]["content"]
        self.assertEqual(stored[1]["input"], {"path": "a.py"})


def _sse(events: list[dict], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


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
                self.assertEqual(loaded["messages"], [])
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

    def test_messages_roundtrip_and_image_strip(self):
        from project_agent.chats import sanitize_messages
        from project_agent.agent import Agent
        from project_agent.mcp_client import McpHub
        from project_agent.tools import Toolbox

        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("APPDATA")
            os.environ["APPDATA"] = tmp
            try:
                root = Path(tmp) / "proj"
                root.mkdir()
                chat_id = new_chat_id()
                raw_messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "посмотри"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AAAA"},
                            },
                        ],
                    },
                    {"role": "assistant", "content": "ок, вижу"},
                ]
                save_chat(
                    chat_id,
                    "С картинкой",
                    str(root),
                    ["Вы: посмотри", "ок, вижу"],
                    messages=raw_messages,
                    provider="openai",
                )
                loaded = load_chat(chat_id)
                self.assertEqual(loaded["provider"], "openai")
                self.assertEqual(len(loaded["messages"]), 2)
                blob = json.dumps(loaded["messages"], ensure_ascii=False)
                self.assertNotIn("AAAA", blob)
                self.assertIn("изображение опущено", blob)
                cleaned = sanitize_messages(raw_messages)
                self.assertEqual(cleaned[0]["content"][1]["type"], "text")

                vault = SecretVault()
                box = Toolbox(vault, lambda *_: True, McpHub(), lambda: {})
                agent = Agent(vault, box, box.mcp, lambda *_: None, lambda *_: None, lambda *_: None)
                agent.set_root(root)
                self.assertTrue(agent.restore_session(loaded["provider"], loaded["messages"]))
                self.assertEqual(agent.provider_kind, "openai")
                self.assertEqual(len(agent.provider.messages), 2)
                self.assertEqual(agent.provider.messages[-1]["content"], "ок, вижу")
                kind, exported = agent.export_session()
                self.assertEqual(kind, "openai")
                self.assertEqual(len(exported), 2)
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
            self.assertEqual(loaded["agent_mode"], "agent")
            data["agent_mode"] = "ask"
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
            self.assertEqual(loaded["agent_mode"], "ask")
            self.assertEqual(loaded["active_profile"], "OpenRouter")
            self.assertEqual(loaded["model"], "openrouter/free")
            self.assertEqual(loaded["max_steps"], 10)
            self.assertEqual(len(loaded["profiles"]), 1)
            data["test_preset"] = "unittest"
            data["test_timeout"] = 90
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["test_preset"], "unittest")
            self.assertEqual(loaded["test_timeout"], 90)
            data["test_preset"] = "npm"
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["test_preset"], "npm")
            data["test_preset"] = "rm -rf /"
            data["test_timeout"] = 9999
            data["test_fix_rounds"] = 0
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertEqual(loaded["test_preset"], "")
            self.assertEqual(loaded["test_timeout"], 600)
            self.assertEqual(loaded["test_fix_rounds"], 1)
            self.assertEqual(loaded["project_dir"], "")
            data["auto_write_project"] = True
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertTrue(loaded["auto_write_project"])
            data["auto_write_project"] = "nope"
            save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertFalse(loaded["auto_write_project"])
            self.assertEqual(default_config()["auto_write_project"], False)
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

    def test_write_retries_locked_replace(self):
        from unittest import mock

        import project_agent.config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = default_config()
            data["model"] = "retry-me"
            calls = {"n": 0}
            real_replace = os.replace

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "Отказано в доступе")
                return real_replace(src, dst)

            with mock.patch.object(config_mod.os, "replace", flaky):
                save_config(data, path)
            loaded, error = load_config(path)
            self.assertIsNone(error)
            self.assertEqual(loaded["model"], "retry-me")
            self.assertGreaterEqual(calls["n"], 3)

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
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": message}],
                        "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240},
                    },
                )

            chats, journals, contexts = [], [], []
            vault = SecretVault()
            hub = McpHub()
            box = Toolbox(vault, lambda *_: False, hub, lambda: {"api_key": "", "mcp_servers": []})
            agent = Agent(
                vault,
                box,
                hub,
                chats.append,
                journals.append,
                lambda _status: None,
                lambda used, limit, from_api=False: contexts.append((used, limit, from_api)),
            )
            try:
                agent.set_root(root)
                provider = OpenAIProvider()
                provider._transport = httpx.MockTransport(handler)
                agent.provider = provider
                agent.provider_kind = "openai"
                agent.run_turn(
                    "прочитай note.py",
                    [],
                    {
                        "provider": "openai",
                        "model": "m",
                        "api_key": "",
                        "base_url": "http://example.test/v1",
                        "mcp_servers": [],
                        "context_limit": 256000,
                    },
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
            self.assertTrue(contexts)
            self.assertEqual(contexts[-1][1], 256000)

    def test_stream_updates_callback_without_duplicating_chat(self):
        from project_agent.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            events = [
                {"choices": [{"delta": {"content": "го"}}]},
                {"choices": [{"delta": {"content": "тово"}}]},
            ]

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=_sse(events),
                )

            chats = []
            snapshots = []
            vault = SecretVault()
            hub = McpHub()
            box = Toolbox(vault, lambda *_: False, hub, lambda: {"api_key": "", "mcp_servers": []})
            agent = Agent(
                vault,
                box,
                hub,
                chats.append,
                lambda _journal: None,
                lambda _status: None,
                lambda *_args, **_kwargs: None,
                lambda text, final: snapshots.append((text, final)),
            )
            try:
                agent.set_root(root)
                provider = OpenAIProvider()
                provider._transport = httpx.MockTransport(handler)
                agent.provider = provider
                agent.provider_kind = "openai"
                agent.run_turn(
                    "скажи готово",
                    [],
                    {
                        "provider": "openai",
                        "model": "m",
                        "api_key": "",
                        "base_url": "http://example.test/v1",
                        "mcp_servers": [],
                    },
                    threading.Event(),
                )
            finally:
                provider._http.close()
                hub.close()
            self.assertTrue(any(not final and text == "го" for text, final in snapshots))
            self.assertIn(("готово", True), snapshots)
            self.assertFalse(any("готово" in line for line in chats))

    def test_resend_repeats_same_user_turn(self):
        from project_agent.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            bodies = []

            def handler(request: httpx.Request) -> httpx.Response:
                body = request.content.decode()
                bodies.append(body)
                if len(bodies) == 1:
                    return httpx.Response(500, json={"error": {"message": "timeout"}})
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": "ок"}}]},
                )

            errors = []
            chats = []
            vault = SecretVault()
            hub = McpHub()
            box = Toolbox(vault, lambda *_: False, hub, lambda: {"api_key": "", "mcp_servers": []})
            agent = Agent(
                vault,
                box,
                hub,
                chats.append,
                lambda _journal: None,
                lambda _status: None,
                on_error=errors.append,
            )
            settings = {
                "provider": "openai",
                "model": "m",
                "api_key": "",
                "base_url": "http://example.test/v1",
                "mcp_servers": [],
                "agent_mode": "agent",
            }
            try:
                agent.set_root(root)
                provider = OpenAIProvider()
                provider._transport = httpx.MockTransport(handler)
                agent.provider = provider
                agent.provider_kind = "openai"
                agent.run_turn("вопрос", [], settings, threading.Event())
                self.assertTrue(errors and errors[0].startswith("Ошибка API:"))
                provider.messages.append({"role": "assistant", "content": "обрывок"})
                settings = dict(settings)
                settings["agent_mode"] = "ask"
                agent.run_turn("вопрос", [], settings, threading.Event(), resend=True)
            finally:
                provider._http.close()
                hub.close()
            users = [item for item in provider.messages if item.get("role") == "user"]
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["content"], "вопрос")
            self.assertNotIn("обрывок", bodies[-1])
            self.assertNotIn('"name": "write_file"', bodies[-1])
            self.assertNotIn('"name":"write_file"', bodies[-1])
            self.assertTrue(any("ок" in line for line in chats))
            self.assertFalse(any(line.startswith("Вы:") for line in chats))


class ContextUsageTests(unittest.TestCase):
    def test_parse_estimate_and_flatten(self):
        from project_agent.context_usage import (
            estimate_tokens,
            flatten_messages_for_summary,
            format_context_detail,
            normalize_context_limit,
            parse_prompt_tokens,
        )

        self.assertEqual(normalize_context_limit(None), 256000)
        self.assertEqual(normalize_context_limit(100), 8000)
        self.assertEqual(parse_prompt_tokens({"usage": {"prompt_tokens": 321}}), 321)
        self.assertEqual(parse_prompt_tokens({"usage": {"input_tokens": 99}}), 99)
        self.assertIsNone(parse_prompt_tokens({"usage": {}}))
        est = estimate_tokens("sys", [{"role": "user", "content": "привет мир"}], [{"name": "read_file"}])
        self.assertGreater(est, 5)
        flat = flatten_messages_for_summary(
            [
                {"role": "user", "content": "задача"},
                {"role": "assistant", "content": "ок", "tool_calls": [{"function": {"name": "read_file"}}]},
                {"role": "tool", "content": "code here"},
            ]
        )
        self.assertIn("user:", flat)
        self.assertIn("read_file", flat)
        self.assertIn("%", format_context_detail(128000, 256000, True))


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
