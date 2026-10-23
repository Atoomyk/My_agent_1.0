import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_agent import chats  # noqa: E402


class ChatDateTests(unittest.TestCase):
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

    def _set_updated(self, value: str) -> None:
        path = chats.chat_path(self.chat_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated"] = value
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_reopen_without_changes_keeps_date(self):
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"])
        self._set_updated("2020-01-02T10:00:00")
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"])
        self.assertEqual(chats.load_chat(self.chat_id)["updated"], "2020-01-02T10:00:00")

    def test_real_change_updates_date(self):
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"])
        self._set_updated("2020-01-02T10:00:00")
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет", "Ассистент: ок"])
        self.assertNotEqual(chats.load_chat(self.chat_id)["updated"], "2020-01-02T10:00:00")

    def test_list_contains_updated_for_grouping(self):
        chats.save_chat(self.chat_id, "Тема", self.project, ["Вы: привет"])
        items = chats.list_chats(self.project)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["updated"])


class ChatGroupTests(unittest.TestCase):
    def test_groups_today_yesterday_earlier(self):
        today = date(2026, 10, 23)
        items = [
            {"id": "a", "title": "A", "updated": "2026-10-23T09:00:00"},
            {"id": "b", "title": "B", "updated": "2026-10-22T20:00:00"},
            {"id": "c", "title": "C", "updated": "2026-10-01T08:00:00"},
            {"id": "d", "title": "D", "updated": ""},
        ]
        groups = chats.group_chats(items, today=today)
        self.assertEqual(
            [(key, [item["id"] for item in rows]) for key, _label, rows in groups],
            [("today", ["a"]), ("yesterday", ["b"]), ("earlier", ["c", "d"])],
        )

    def test_empty_groups_skipped(self):
        today = date(2026, 10, 23)
        items = [{"id": "c", "title": "C", "updated": "2026-01-01T00:00:00"}]
        groups = chats.group_chats(items, today=today)
        self.assertEqual([key for key, _l, _r in groups], ["earlier"])


if __name__ == "__main__":
    unittest.main()
