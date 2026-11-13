import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_agent import chats  # noqa: E402
from project_agent.chat_store import ChatStoreMixin, _GROUP_PREFIX  # noqa: E402
from project_agent.secrets import SecretVault  # noqa: E402


class _FakeAgent:
    def export_session(self):
        return "openai", [{"role": "user", "content": "привет"}]


class _RenameRig(ChatStoreMixin):
    def __init__(self, project: Path) -> None:
        self.chat_id: str | None = chats.new_chat_id()
        self.chat_title = "Старое имя"
        self.transcript: list[str] = []
        self.project = project
        self.vault = SecretVault()
        self.agent = _FakeAgent()
        self.running = False
        self.statuses: list[str] = []
        self.messages: list[str] = []
        self.refreshed = 0
        self.fitted = 0
        self.jobs: dict[int, tuple[int, object]] = {}
        self._seq = 0
        self._chat_save_job = None
        self._chat_save_pending = False
        self._next_title = ""

    def after(self, delay, callback):
        self._seq += 1
        self.jobs[self._seq] = (delay, callback)
        return self._seq

    def after_cancel(self, job) -> None:
        self.jobs.pop(job, None)

    def collect_settings(self) -> dict:
        return {}

    def set_status(self, text: str) -> None:
        self.statuses.append(text)

    def write_chat(self, text: str) -> None:
        self.messages.append(text)

    def _refresh_chat_list(self) -> None:
        self.refreshed += 1

    def _fit_labels(self) -> None:
        self.fitted += 1

    def _ask_chat_title(self, _initial: str) -> str:
        return self._next_title


class ChatRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self._tmp.name})
        self.env.start()
        self.project = Path(self._tmp.name) / "proj"
        self.project.mkdir()
        self.chat_id = chats.new_chat_id()

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def _set_updated(self, chat_id: str, value: str) -> None:
        path = chats.chat_path(chat_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated"] = value
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_rename_chat_keeps_date_and_history(self):
        chats.save_chat(
            self.chat_id,
            "Старое",
            str(self.project),
            ["Вы: привет", "Ассистент: здравствуй"],
            messages=[{"role": "user", "content": "привет"}],
            provider="openai",
            fresh=True,
        )
        self._set_updated(self.chat_id, "2020-01-02T10:00:00")

        saved = chats.rename_chat(self.chat_id, "  Новое   имя  ")
        self.assertEqual(saved, "Новое имя")

        record = chats.load_chat(self.chat_id)
        self.assertEqual(record["title"], "Новое имя")
        self.assertEqual(record["updated"], "2020-01-02T10:00:00")
        self.assertEqual(record["lines"], ["Вы: привет", "Ассистент: здравствуй"])
        self.assertEqual(record["messages"], [{"role": "user", "content": "привет"}])
        self.assertEqual(record["provider"], "openai")
        self.assertEqual(chats.list_chats(str(self.project))[0]["title"], "Новое имя")

    def test_rename_chat_clips_long_title(self):
        chats.save_chat(self.chat_id, "Тема", str(self.project), ["Вы: привет"], fresh=True)
        saved = chats.rename_chat(self.chat_id, "б" * 120)
        self.assertEqual(len(saved), 80)
        self.assertEqual(chats.load_chat(self.chat_id)["title"], saved)

    def test_rename_missing_chat_returns_none(self):
        self.assertIsNone(chats.rename_chat(self.chat_id, "Новое"))

    def test_rename_active_chat_flushes_pending_and_updates_ui(self):
        rig = _RenameRig(self.project)
        rig._remember("Вы: первое сообщение")
        rig._next_title = "Рабочая задача"
        rig.rename_chat_item(rig.chat_id)

        self.assertFalse(rig._chat_save_pending)
        self.assertEqual(rig.chat_title, "Рабочая задача")
        self.assertEqual(rig.fitted, 1)
        self.assertEqual(rig.refreshed, 1)
        record = chats.load_chat(rig.chat_id)
        self.assertEqual(record["title"], "Рабочая задача")
        self.assertEqual(record["lines"], ["Вы: первое сообщение"])

    def test_rename_inactive_chat_does_not_overwrite_active_title(self):
        other_id = chats.new_chat_id()
        chats.save_chat(other_id, "Другая", str(self.project), ["Вы: старая"], fresh=True)
        self._set_updated(other_id, "2020-01-02T10:00:00")

        rig = _RenameRig(self.project)
        rig.chat_title = "Активная"
        rig._next_title = "Переименованная"
        rig.rename_chat_item(other_id)

        self.assertEqual(rig.chat_title, "Активная")
        self.assertEqual(rig.fitted, 0)
        self.assertEqual(rig.refreshed, 1)
        record = chats.load_chat(other_id)
        self.assertEqual(record["title"], "Переименованная")
        self.assertEqual(record["updated"], "2020-01-02T10:00:00")

    def test_rename_ignored_when_running_or_group_or_empty(self):
        chats.save_chat(self.chat_id, "Старая", str(self.project), ["Вы: привет"], fresh=True)
        rig = _RenameRig(self.project)
        rig.chat_id = self.chat_id

        rig.running = True
        rig._next_title = "Нельзя"
        rig.rename_chat_item(self.chat_id)
        self.assertEqual(chats.load_chat(self.chat_id)["title"], "Старая")

        rig.running = False
        rig.rename_chat_item(f"{_GROUP_PREFIX}today")
        self.assertEqual(rig.refreshed, 0)

        rig._next_title = ""
        rig.rename_chat_item(self.chat_id)
        self.assertEqual(chats.load_chat(self.chat_id)["title"], "Старая")
        self.assertEqual(rig.refreshed, 0)

    def test_rename_current_chat_before_first_message(self):
        rig = _RenameRig(self.project)
        rig.chat_id = None
        rig.chat_title = ""
        rig._next_title = "Черновик темы"
        rig.rename_current_chat()

        self.assertEqual(rig.chat_title, "Черновик темы")
        self.assertEqual(rig.fitted, 1)
        self.assertEqual(rig.statuses[-1], "Беседа переименована")


if __name__ == "__main__":
    unittest.main()
