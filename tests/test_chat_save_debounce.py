import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_agent import chats  # noqa: E402
from project_agent.chat_store import CHAT_SAVE_DELAY_MS, ChatStoreMixin  # noqa: E402
from project_agent.secrets import SecretVault  # noqa: E402


class _FakeAgent:
    def export_session(self):
        return "openai", [{"role": "user", "content": "привет"}]


class _Rig(ChatStoreMixin):
    """Минимальный ``App``: только то, что читает сохранение чата, и фальшивый Tk-таймер."""

    def __init__(self, project: Path) -> None:
        self.chat_id: str | None = chats.new_chat_id()
        self.chat_title = "Тема"
        self.transcript: list[str] = []
        self.project = project
        self.vault = SecretVault()
        self.agent = _FakeAgent()
        self.statuses: list[str] = []
        self.jobs: dict[int, tuple[int, object]] = {}
        self.cancelled: list[int] = []
        self._seq = 0
        self._chat_save_job = None
        self._chat_save_pending = False

    # --- Tk ---------------------------------------------------------------
    def after(self, delay, callback):
        self._seq += 1
        self.jobs[self._seq] = (delay, callback)
        return self._seq

    def after_cancel(self, job) -> None:
        self.cancelled.append(job)
        self.jobs.pop(job, None)

    def collect_settings(self) -> dict:
        return {}

    def set_status(self, text: str) -> None:
        self.statuses.append(text)

    # --- управление тестом ------------------------------------------------
    def fire(self) -> None:
        jobs = list(self.jobs.items())
        self.jobs.clear()
        for _job, (_delay, callback) in jobs:
            callback()

    def file(self) -> Path:
        return chats.chat_path(self.chat_id)


class ChatSaveDebounceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"APPDATA": self._tmp.name})
        self.env.start()
        self.project = Path(self._tmp.name) / "proj"
        self.project.mkdir()
        self.rig = _Rig(self.project)

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def test_remember_does_not_write_immediately(self):
        self.rig._remember("Вы: привет")
        self.assertFalse(self.rig.file().exists(), "файл записан до паузы")
        self.assertTrue(self.rig._chat_save_pending)
        self.assertEqual(len(self.rig.jobs), 1)

    def test_delay_uses_configured_pause(self):
        self.rig._remember("Вы: привет")
        delay = next(iter(self.rig.jobs.values()))[0]
        self.assertEqual(delay, CHAT_SAVE_DELAY_MS)

    def test_many_lines_share_one_job(self):
        for index in range(20):
            self.rig._remember(f"Вы: строка {index}")
        self.assertEqual(len(self.rig.jobs), 1)
        self.rig.fire()
        record = chats.load_chat(self.rig.chat_id)
        self.assertEqual(len(record["lines"]), 20)
        self.assertEqual(record["lines"][-1], "Вы: строка 19")
        self.assertEqual(len([job for job in self.rig.statuses]), 0)

    def test_flush_after_pause_writes_once(self):
        self.rig._remember("Вы: привет")
        self.rig.fire()
        record = chats.load_chat(self.rig.chat_id)
        self.assertEqual(record["lines"], ["Вы: привет"])
        self.assertEqual(record["provider"], "openai")
        self.assertFalse(self.rig._chat_save_pending)
        self.assertEqual(self.rig.jobs, {})

    def test_store_chat_writes_now_and_cancels_job(self):
        self.rig._remember("Вы: привет")
        job = self.rig._chat_save_job
        self.rig._store_chat()
        self.assertIn(job, self.rig.cancelled)
        self.assertEqual(self.rig.jobs, {})
        self.assertEqual(chats.load_chat(self.rig.chat_id)["lines"], ["Вы: привет"])

    def test_store_chat_keeps_new_date_after_pending_lines(self):
        # Переключение чата сразу после сообщения: дату надо сдвинуть, иначе
        # беседа не поднимется в списке (регрессия из test_chat_dates).
        self.rig._remember("Вы: первое")
        self.rig.fire()
        path = self.rig.file()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated"] = "2020-01-02T10:00:00"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self.rig._remember("Вы: второе")
        self.rig._store_chat()
        self.assertNotEqual(chats.load_chat(self.rig.chat_id)["updated"], "2020-01-02T10:00:00")

    def test_store_chat_without_pending_keeps_old_date(self):
        self.rig._remember("Вы: привет")
        self.rig.fire()
        path = self.rig.file()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["updated"] = "2020-01-02T10:00:00"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        self.rig._store_chat()
        self.assertEqual(chats.load_chat(self.rig.chat_id)["updated"], "2020-01-02T10:00:00")

    def test_empty_line_is_not_saved(self):
        self.rig._remember("   ")
        self.assertFalse(self.rig._chat_save_pending)
        self.assertEqual(self.rig.jobs, {})

    def test_no_chat_means_no_save(self):
        self.rig.chat_id = None
        self.rig._remember("Вы: привет")
        self.assertFalse(self.rig._chat_save_pending)

    def test_save_without_tk_pause_writes_immediately(self):
        # Если ``after`` недоступен (нет цикла Tk), запись идёт сразу — данные не теряются.
        rig = self.rig

        def boom(*_args):
            raise RuntimeError("нет Tk")

        rig.after = boom
        rig._remember("Вы: привет")
        self.assertEqual(chats.load_chat(rig.chat_id)["lines"], ["Вы: привет"])
        self.assertFalse(rig._chat_save_pending)

    def test_run_save_without_pending_does_nothing(self):
        self.rig._run_chat_save()
        self.assertFalse(self.rig.file().exists())


if __name__ == "__main__":
    unittest.main()