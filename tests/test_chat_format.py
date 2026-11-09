import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_agent import chats  # noqa: E402


def _legacy_record(chat_id: str, project_dir: str, **over) -> dict:
    """Запись в формате до 3.3.0: поля те же, но без version."""
    data = {
        "id": chat_id,
        "title": "Тема",
        "project_dir": project_dir,
        "updated": "2026-10-23T09:00:00",
        "lines": ["Вы: привет"],
        "messages": [{"role": "user", "content": "привет"}],
        "provider": "openai",
    }
    data.update(over)
    return data


class ChatFormatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self._tmp.name})
        self.env.start()
        self.project = str(Path(self._tmp.name) / "proj")
        Path(self.project).mkdir()
        self.chat_id = chats.new_chat_id()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def _write(self, data: dict, chat_id: str | None = None, indent: int | None = 2) -> Path:
        chat_id = chat_id or str(data["id"])
        path = chats.chat_path(chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        return path

    def _saved(self) -> dict:
        return json.loads(chats.chat_path(self.chat_id).read_text(encoding="utf-8"))

    def test_saved_chat_has_version_first(self):
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"], fresh=True)
        raw = self._saved()
        self.assertEqual(raw["version"], chats.CHAT_VERSION)
        self.assertEqual(list(raw)[0], "version")
        self.assertEqual(chats.load_chat(self.chat_id)["version"], chats.CHAT_VERSION)

    def test_legacy_chat_without_version_loads(self):
        self._write(_legacy_record(self.chat_id, self.project))
        record = chats.load_chat(self.chat_id)
        self.assertIsNotNone(record)
        self.assertEqual(record["version"], chats.CHAT_VERSION)
        self.assertEqual(record["provider"], "openai")
        self.assertEqual(record["lines"], ["Вы: привет"])
        self.assertEqual([item["id"] for item in chats.list_chats(self.project)], [self.chat_id])

    def test_legacy_chat_is_rewritten_with_version(self):
        self._write(_legacy_record(self.chat_id, self.project))
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"], fresh=False)
        raw = self._saved()
        self.assertEqual(raw["version"], chats.CHAT_VERSION)
        self.assertEqual(raw["updated"], "2026-10-23T09:00:00")

    def test_newer_format_is_ignored(self):
        self._write(_legacy_record(self.chat_id, self.project, version=chats.CHAT_VERSION + 1))
        self.assertIsNone(chats.load_chat(self.chat_id))
        self.assertEqual(chats.list_chats(self.project), [])

    def test_migrate_chain(self):
        self.assertEqual(chats.chat_version({}), 0)
        self.assertEqual(chats.chat_version({"version": "1"}), 1)
        self.assertEqual(chats.migrate({"version": 0, "id": self.chat_id})["version"], chats.CHAT_VERSION)
        self.assertIsNone(chats.migrate({"version": chats.CHAT_VERSION + 1}))
        self.assertIsNone(chats.migrate([]))

    def test_list_reads_only_head(self):
        large = "м" * 150_000
        chats.save_chat(
            self.chat_id,
            "Тема",
            self.project,
            ["Вы: привет"],
            messages=[{"role": "user", "content": large}],
            provider="openai",
            fresh=True,
        )
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("полный разбор файла")):
            items = chats.list_chats(self.project)
        self.assertEqual(items, [{"id": self.chat_id, "title": "Тема", "updated": items[0]["updated"]}])

    def test_list_matches_full_record_for_escaped_title(self):
        title = 'чат "кавычки" и C:\\путь\nвторая строка'
        chats.save_chat(self.chat_id, title, self.project, ["Вы: привет"], fresh=True)
        listed = chats.list_chats(self.project)[0]["title"]
        self.assertEqual(listed, chats.load_chat(self.chat_id)["title"])
        self.assertEqual(listed, 'чат "кавычки" и C:\\путь вторая строка')

    def test_long_title_is_clipped_in_head_path(self):
        chats.save_chat(self.chat_id, "т" * 120, self.project, ["Вы: привет"], fresh=True)
        listed = chats.list_chats(self.project)[0]["title"]
        self.assertEqual(len(listed), 80)
        self.assertEqual(listed, chats.load_chat(self.chat_id)["title"])

    def test_compact_legacy_json_is_listed(self):
        self._write(_legacy_record(self.chat_id, self.project), indent=None)
        self.assertEqual([item["title"] for item in chats.list_chats(self.project)], ["Тема"])

    def test_empty_lines_chat_is_skipped(self):
        self._write(_legacy_record(self.chat_id, self.project, lines=[]))
        self.assertIsNone(chats.load_chat(self.chat_id))
        self.assertEqual(chats.list_chats(self.project), [])

    def test_other_project_is_filtered(self):
        self._write(_legacy_record(self.chat_id, self.project))
        self.assertEqual(chats.list_chats(str(Path(self._tmp.name) / "other")), [])

    def test_broken_json_is_skipped(self):
        path = chats.chat_path(self.chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"id": "обрыв', encoding="utf-8")
        self.assertIsNone(chats.load_chat(self.chat_id))
        self.assertEqual(chats.list_chats(self.project), [])

    def test_unexpected_key_order_falls_back_to_full_read(self):
        data = _legacy_record(self.chat_id, self.project)
        shuffled = {key: data[key] for key in ("title", "id", "updated", "project_dir", "lines", "provider")}
        self._write(shuffled)
        items = chats.list_chats(self.project)
        self.assertEqual([item["title"] for item in items], ["Тема"])


if __name__ == "__main__":
    unittest.main()