"""Лимит шагов агента: сообщение с кнопкой «Продолжить» и корректное продолжение хода.

Проверяем два уровня:
* ``Agent.run_turn`` при исчерпании ``max_steps`` зовёт ``on_limit`` (а не просто
  ``on_chat``), не трогая ветки стопа и ошибок, и повторный ход с ``resend=True``
  продолжает диалог без второй реплики «Вы»;
* ``ChatViewMixin.write_limit`` переиспользует механизм ``write_stopped`` /
  ``retry_last``: в ленте появляется доступная кнопка «Продолжить», нажатие
  которой возобновляет ход.

Тест ленты намеренно не зависит от ``customtkinter``: модуль ``chat_view``
использует его только внутри методов, поэтому при отсутствии пакета (в этом
окружении его нет) подставляется минимальная заглушка, а лента собирается на
обычном ``tk.Text`` с атрибутом ``_textbox``.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import tkinter as tk
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from project_agent.agent import Agent  # noqa: E402
from project_agent.mcp_client import McpHub  # noqa: E402
from project_agent.providers import ModelTurn, ToolCall  # noqa: E402
from project_agent.secrets import SecretVault  # noqa: E402
from project_agent.tools import ToolOutcome  # noqa: E402


def _load_chat_view():
    """Импорт ``chat_view``; при отсутствии customtkinter — с минимальной заглушкой."""
    try:
        from project_agent import chat_view
    except ModuleNotFoundError as exc:
        if exc.name != "customtkinter":
            raise
    else:
        return chat_view
    stub = types.ModuleType("customtkinter")
    stub.get_appearance_mode = lambda: "Dark"
    stub.CTkImage = type("CTkImage", (), {})
    stub.CTkFrame = type("CTkFrame", (), {})
    stub.CTkTextbox = type("CTkTextbox", (), {})
    sys.modules["customtkinter"] = stub
    try:
        from project_agent import chat_view
    finally:
        # Сразу убираем заглушку: остальные модули (app) должны падать на импорте
        # ровно так же, как и до теста, — поведение окружения не меняем.
        sys.modules.pop("customtkinter", None)
    return chat_view


ChatViewMixin = _load_chat_view().ChatViewMixin


class _FakeProvider:
    """Минимальный провайдер: отдаёт заранее заданные ходы, хранит messages."""

    kind = "openai"

    def __init__(self, turns: list) -> None:
        self.system = ""
        self.messages: list = []
        self.last_prompt_tokens = None
        self.settings: dict = {}
        self._turns = list(turns)
        self.complete_calls = 0

    def configure(self, settings: dict) -> None:
        self.settings = settings

    def set_system(self, text: str) -> None:
        self.system = text

    def message_count(self) -> int:
        return len(self.messages)

    def add_user(self, text: str, images=None) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_result(self, call_id: str, name: str, text: str, images=None) -> None:
        self.messages.append({"role": "tool", "content": text})

    def complete(self, specs, scrubber, on_piece=None) -> ModelTurn:
        self.complete_calls += 1
        if self._turns:
            return self._turns.pop(0)
        return ModelTurn("", [])

    def rewind_to_last_user(self) -> bool:
        index = None
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                index = i
                break
        if index is None:
            return False
        del self.messages[index + 1 :]
        return True

    def drop_incomplete_tail(self) -> None:
        return None

    def user_count(self) -> int:
        return sum(1 for item in self.messages if item.get("role") == "user")


class _FakeToolbox:
    def __init__(self) -> None:
        self.root = None
        self.stop = None
        self.settings = lambda: {}
        self.on_before_write = None

    def begin_turn(self) -> None:
        return None

    def execute(self, name: str, arguments) -> ToolOutcome:
        return ToolOutcome("ok", f"{name}: ok")

    def touched_paths(self) -> list[str]:
        return []

    def cancel(self) -> None:
        return None


def _tool_turn() -> ModelTurn:
    return ModelTurn("", [ToolCall("call-1", "list_dir", {"path": ""})])


class StepLimitAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        self.root.mkdir()
        self.chats: list[str] = []
        self.limits: list[str] = []
        self.stops: list[str] = []
        self.errors: list[str] = []
        self.statuses: list[str] = []
        self.agent = Agent(
            SecretVault(),
            _FakeToolbox(),
            McpHub(),
            self.chats.append,
            lambda *_a, **_k: None,
            self.statuses.append,
            on_context=lambda *_a, **_k: None,
            on_error=self.errors.append,
            on_stopped=self.stops.append,
            on_limit=self.limits.append,
        )
        self.agent.root = self.root
        self.settings = {
            "provider": "openai",
            "agent_mode": "agent",
            "max_steps": 2,
            "mcp_servers": [],
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _install(self, provider: _FakeProvider) -> _FakeProvider:
        self.agent.provider = provider
        self.agent.provider_kind = provider.kind
        return provider

    def test_limit_calls_on_limit_and_continue_keeps_context(self):
        provider = self._install(_FakeProvider([_tool_turn(), _tool_turn(), ModelTurn("готово", [])]))
        stop = threading.Event()

        self.agent.run_turn("сделай шаги", [], self.settings, stop)

        self.assertEqual(provider.complete_calls, 2)
        self.assertEqual(self.limits, ["Достигнут лимит шагов (2)."])
        self.assertNotIn("Достигнут лимит шагов (2).", self.chats)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(provider.user_count(), 1)

        # «Продолжить»: тот же ход без второй реплики «Вы» и без потери контекста.
        self.agent.run_turn("сделай шаги", [], self.settings, threading.Event(), resend=True)

        self.assertEqual(provider.user_count(), 1)
        self.assertIn("готово", self.chats)
        self.assertEqual(self.limits, ["Достигнут лимит шагов (2)."])
        self.assertEqual(provider.complete_calls, 3)

    def test_stop_and_error_paths_stay_unchanged(self):
        provider = self._install(_FakeProvider([_tool_turn(), _tool_turn()]))
        stop = threading.Event()

        def stop_after_first_turn(specs, scrubber, on_piece=None) -> ModelTurn:
            stop.set()
            return _tool_turn()

        provider.complete = stop_after_first_turn
        self.agent.run_turn("стоп", [], self.settings, stop)
        self.assertEqual(self.stops, ["Остановлено."])
        self.assertEqual(self.limits, [])

        self.stops.clear()
        broken = self._install(_FakeProvider([]))

        def fail(*_args, **_kwargs) -> ModelTurn:
            raise RuntimeError("boom")

        broken.complete = fail
        self.agent.run_turn("ошибка", [], self.settings, threading.Event())
        self.assertEqual(self.limits, [])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.errors, ["Ошибка: boom"])

    def test_on_limit_defaults_to_on_stopped(self):
        stops: list[str] = []
        agent = Agent(
            SecretVault(),
            _FakeToolbox(),
            McpHub(),
            lambda *_a, **_k: None,
            lambda *_a, **_k: None,
            lambda *_a, **_k: None,
            on_stopped=stops.append,
        )
        self.assertIs(agent.on_limit, agent.on_stopped)
        agent.on_limit("Достигнут лимит шагов (1).")
        self.assertEqual(stops, ["Достигнут лимит шагов (1)."])


class _ChatBox(tk.Text):
    """``tk.Text`` с интерфейсом ``CTkTextbox``: ленте нужен только ``_textbox``."""

    @property
    def _textbox(self):
        return self


def _call_now(_ms, func=None, *args):
    if func is not None:
        return func(*args)
    return None


class _ChatRig(ChatViewMixin):
    """Лента без всего приложения: только то, что нужно кнопке «Продолжить»."""

    def __init__(self, root) -> None:
        self.chat = _ChatBox(root, width=60, height=12)
        self.chat.pack()
        self.after = _call_now
        self.jump_down = None
        self._retry_payload: dict | None = None
        self._can_retry = False
        self._limit_hit = False
        self._retry_buttons: list = []
        self._checkpoint_buttons: list = []
        self.transcript: list[str] = []
        self._stream_open = False
        self._user_spans: list = []
        self._user_bubbles: list = []
        self._tool_groups: list = []
        self._tool_group = None
        self._running_tool = None
        self._chat_stick = True
        self._search_open = False
        self.retry_calls = 0

    def _px_font(self, size: int, family: str | None = None, weight: str = "normal") -> tuple:
        return ("Segoe UI", -size, weight)

    def _remember(self, text: str) -> None:
        self.transcript.append(text)

    def retry_last(self) -> None:
        self.retry_calls += 1


class StepLimitChatViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.rig = _ChatRig(self.root)

    def tearDown(self) -> None:
        self.root.destroy()

    def _labels(self) -> list[str]:
        return [button.cget("text") for button in self.rig._retry_buttons]

    def test_limit_shows_enabled_continue_button(self):
        self.rig._retry_payload = {"chat_id": "abc", "text": "запрос", "images": []}
        self.rig.write_limit("Достигнут лимит шагов (2).")

        text = self.rig.chat.get("1.0", "end-1c")
        self.assertIn("Достигнут лимит шагов (2).", text)
        self.assertTrue(self.rig._limit_hit)
        self.assertTrue(self.rig._can_retry)
        self.assertEqual(self._labels(), ["Продолжить"])
        self.assertEqual(self.rig._retry_buttons[0].cget("state"), "normal")
        self.assertTrue(any(line.startswith("Достигнут лимит") for line in self.rig.transcript))

        # Как после стопа: кнопка включается при завершении хода и продолжает диалог.
        self.rig._set_retry_enabled(True)
        self.rig._retry_buttons[0].invoke()
        self.assertEqual(self.rig.retry_calls, 1)

    def test_limit_without_payload_keeps_message_only(self):
        self.rig.write_limit("Достигнут лимит шагов (5).")

        text = self.rig.chat.get("1.0", "end-1c")
        self.assertIn("Достигнут лимит шагов (5).", text)
        self.assertEqual(self.rig._retry_buttons, [])
        self.assertFalse(self.rig._can_retry)

    def test_stop_message_still_uses_continue_button(self):
        self.rig._retry_payload = {"chat_id": "abc", "text": "запрос", "images": []}
        self.rig.write_stopped("Остановлено.")

        self.assertFalse(self.rig._limit_hit)
        self.assertIn("Остановлено.", self.rig.chat.get("1.0", "end-1c"))
        self.assertEqual(self._labels(), ["Продолжить"])


if __name__ == "__main__":
    unittest.main()
