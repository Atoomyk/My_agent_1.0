"""Хранилище чатов: сохранение, список, открытие и удаление беседы.

Перенесено из ``project_agent.app`` дословно, без изменения поведения.
Внутри — только та часть ``App``, которая относится к чатам: создание беседы
(``new_chat``), запись ленты и истории модели (``_remember`` → отложенная запись
``_schedule_chat_save``/``_run_chat_save`` с паузой ``CHAT_SAVE_DELAY_MS``,
немедленная ``_store_chat`` → ``_write_chat``),
список чатов в боковой панели (``_refresh_chat_list``, ``_style_chat_groups``,
``_on_chat_select``), открытие и восстановление (``_open_chat``,
``_restore_last_chat``), память о последнем чате (``_remember_last_chat``,
``_prune_last_chats``) и удаление (``delete_chat_item``).

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``chat_tree``, ``chat_id``,
``chat_title``, ``transcript``, ``last_chats``, ``agent``, ``checkpoints``,
``_chat_list_lock``, ``running``, ``_clear_box``, ``_fit_labels``,
``_show_transcript``, ``_forget_retry``, ``_apply_context``, ``_hide_at_popup``,
``collect_settings``, ``set_status``, ``write_chat``. Поэтому код оформлен
миксином ``ChatStoreMixin``: методы остаются методами ``App``, но живут
в отдельном файле.

Состояние чатов (``chat_id``, ``chat_title``, ``transcript``, ``last_chats``,
``_chat_list_lock``) осталось в ``App.__init__``: его читают и пишут другие
части ``App``, а сам перенос — дословный.

Формат файла чата и его версия живут в ``project_agent.chats`` — этот модуль
только вызывает ``save_chat``/``load_chat``/``list_chats``/``delete_chat``.
"""

from __future__ import annotations

from pathlib import Path

from project_agent.chats import delete_chat, group_chats, list_chats, load_chat, rename_chat, save_chat
from project_agent.checkpoints import delete_checkpoints
from project_agent.config import folder_key, save_last_chats
from project_agent.dialogs import ConfirmDialog, NameDialog
from project_agent.secrets import Scrubber, literals_from_settings, scrub_outbound
from project_agent.ui_theme import HINT
from project_agent.ui_widgets import _tone

_GROUP_PREFIX = "__group__:"
_GROUP_INDENT = "   "
# Пауза перед отложенной записью чата: за один ход строк в ленте много,
# а полная перезапись файла на каждую — квадратичная цена.
CHAT_SAVE_DELAY_MS = 800


class ChatStoreMixin:
    def _schedule_chat_save(self) -> None:
        """Отложить запись чата: за один ход строк в ленте много.

        Каждая строка раньше переписывала файл целиком (Scrubber по всей ленте,
        export_session по всей истории, json.dumps всего файла), то есть цена
        хода росла квадратично. Теперь запись одна — после паузы. Все прочие
        вызовы ``_store_chat`` остаются синхронными и вызывают ``_cancel_chat_save``.
        """
        self._chat_save_pending = True
        if getattr(self, "_chat_save_job", None) is not None:
            return
        try:
            self._chat_save_job = self.after(CHAT_SAVE_DELAY_MS, self._run_chat_save)
        except Exception:
            # Нет цикла Tk (тесты, ранний старт) — пишем сразу, как раньше.
            self._run_chat_save()

    def _run_chat_save(self) -> None:
        self._chat_save_job = None
        if not getattr(self, "_chat_save_pending", False):
            return
        self._chat_save_pending = False
        self._write_chat(fresh=True)

    def _cancel_chat_save(self) -> None:
        job = getattr(self, "_chat_save_job", None)
        if job is None:
            return
        self._chat_save_job = None
        try:
            self.after_cancel(job)
        except Exception:
            pass

    def new_chat(self) -> None:
        if self.running:
            return
        self._hide_at_popup()
        self.clear_attachments()
        self._store_chat()
        self.chat_id = None
        self.chat_title = ""
        self.transcript.clear()
        self._forget_retry()
        self.checkpoints.clear()
        self._clear_box(self.chat)
        self.agent.reset_session(announce=False)
        self._apply_context(0, self.context_limit, False)
        self._remember_last_chat()
        self._refresh_chat_list()
        self._fit_labels()
        self.set_status("Новый чат")

    def _remember(self, text: str) -> None:
        line = text.rstrip()
        if not line or not self.chat_id:
            return
        self.transcript.append(line)
        # Новая строка в ленте — единственный повод сдвинуть дату изменения беседы.
        self._schedule_chat_save()

    def _store_chat(self, fresh: bool = False) -> None:
        """Записать чат сразу — там, где откладывать нельзя.

        Отложенная запись отменяется: если в ленте были несохранённые строки,
        ``fresh`` принудительно включается, иначе переключение чата сразу после
        сообщения оставило бы старую дату и беседа не поднялась бы в списке.
        """
        pending = bool(getattr(self, "_chat_save_pending", False))
        self._cancel_chat_save()
        self._chat_save_pending = False
        self._write_chat(fresh=fresh or pending)

    def _write_chat(self, fresh: bool = False) -> None:
        """Записать чат на диск.

        По умолчанию флаг снят: открытие, переключение и закрытие чата не должны
        делать беседу «свежей» — дату обновляет только ``_remember``.
        """
        if not self.chat_id or self.project is None or not self.transcript:
            return
        try:
            scrubber = Scrubber(self.vault, literals_from_settings(self.collect_settings()))
            lines = [scrubber(line) for line in self.transcript]
            title = scrubber(self.chat_title) or "Чат"
            provider, messages = self.agent.export_session()
            if messages:
                messages = scrub_outbound(messages, scrubber)
            save_chat(
                self.chat_id,
                title,
                str(self.project),
                lines,
                messages=messages,
                provider=provider,
                fresh=fresh,
            )
        except Exception as exc:
            self.set_status(f"Чат не сохранён: {exc}")

    def _refresh_chat_list(self) -> None:
        if not hasattr(self, "chat_tree"):
            return
        self._chat_list_lock = True
        try:
            self.chat_tree.delete(*self.chat_tree.get_children())
            self._style_chat_groups()
            if self.project is None:
                return
            for key, label, items in group_chats(list_chats(str(self.project))):
                group_iid = f"{_GROUP_PREFIX}{key}"
                # Пробелы в начале дают отступ заголовка; беседы идут без отступа
                self.chat_tree.insert(
                    "", "end", iid=group_iid, text=f"{_GROUP_INDENT}{label}", open=True, tags=("group",)
                )
                for item in items:
                    self.chat_tree.insert(group_iid, "end", iid=item["id"], text=item["title"])
            if self.chat_id and self.chat_tree.exists(self.chat_id):
                self.chat_tree.selection_set(self.chat_id)
                self.chat_tree.focus(self.chat_id)
                self.chat_tree.see(self.chat_id)
        finally:
            self._chat_list_lock = False

    def _style_chat_groups(self) -> None:
        if not hasattr(self, "chat_tree"):
            return
        self.chat_tree.tag_configure(
            "group",
            font=(self.ui_font, 8, "italic"),
            foreground=_tone(HINT),
        )

    def _on_chat_select(self, _event=None) -> None:
        if self._chat_list_lock:
            return
        selected = self.chat_tree.selection()
        if not selected:
            return
        chat_id = str(selected[0])
        if chat_id.startswith(_GROUP_PREFIX):
            # Заголовок группы: только раскрыть/свернуть, чат не открываем
            self._chat_list_lock = True
            try:
                self.chat_tree.selection_remove(chat_id)
                if self.chat_id and self.chat_tree.exists(self.chat_id):
                    self.chat_tree.selection_set(self.chat_id)
            finally:
                self._chat_list_lock = False
            return
        if chat_id == self.chat_id:
            return
        if self.running:
            self._refresh_chat_list()
            return
        self._open_chat(chat_id)

    def _open_chat(self, chat_id: str) -> None:
        self._store_chat()
        record = load_chat(chat_id)
        if record is None or self.project is None:
            self._refresh_chat_list()
            return
        self.chat_id = record["id"]
        self._remember_last_chat()
        self.chat_title = record["title"]
        self.transcript = list(record["lines"])
        self._forget_retry()
        self.checkpoints.load(self.chat_id, str(self.project))
        self._sync_checkpoint_marks()
        self._show_transcript()
        from project_agent.tools import normalize_agent_mode

        self.agent._agent_mode = normalize_agent_mode(self.collect_settings().get("agent_mode"))
        restored = False
        messages = record.get("messages") or []
        provider = record.get("provider") or ""
        if messages and provider:
            restored = self.agent.restore_session(provider, messages)
        if not restored:
            self.agent.reset_session(announce=False)
            self.set_status("Чат открыт. Модель начинает заново.")
        else:
            self.set_status("Чат открыт. История модели восстановлена.")
            settings = self.collect_settings()
            if self.agent.provider is not None:
                try:
                    self.agent._publish_context(settings)
                except Exception:
                    self._apply_context(0, self.context_limit, False)
        self._refresh_chat_list()
        self._fit_labels()

    def _restore_last_chat(self) -> None:
        if self.project is None:
            return
        chat_id = self.last_chats.get(folder_key(self.project), "")
        record = load_chat(chat_id) if chat_id else None
        if record is None or folder_key(record["project_dir"]) != folder_key(self.project):
            self._refresh_chat_list()
            return
        self._open_chat(record["id"])

    def _remember_last_chat(self) -> None:
        if self.project is None:
            return
        key = folder_key(self.project)
        value = self.chat_id or ""
        if key in self.last_chats and self.last_chats[key] == value:
            return
        self.last_chats[key] = value
        try:
            save_last_chats(self.last_chats)
        except Exception as exc:
            self.set_status(f"Последний чат не запомнен: {exc}")

    def _prune_last_chats(self) -> None:
        kept: dict[str, str] = {}
        for key, chat_id in self.last_chats.items():
            if not key or not Path(key).is_dir():
                continue
            if chat_id:
                record = load_chat(chat_id)
                if record is None or folder_key(record["project_dir"]) != key:
                    continue
            kept[key] = chat_id
        if kept == self.last_chats:
            return
        self.last_chats = kept
        try:
            save_last_chats(kept)
        except Exception:
            pass

    def _on_chat_f2(self, _event=None) -> str | None:
        if self.running or not hasattr(self, "chat_tree"):
            return None
        selected = self.chat_tree.selection()
        if not selected:
            return None
        chat_id = str(selected[0])
        if chat_id.startswith(_GROUP_PREFIX):
            return None
        self.rename_chat_item(chat_id)
        return "break"

    def _ask_chat_title(self, initial: str) -> str:
        dialog = NameDialog(
            self,
            initial=initial,
            title="Переименовать беседу",
            label="Название беседы",
        )
        self.wait_window(dialog)
        raw = " ".join(str(dialog.result or "").split())
        if not raw:
            return ""
        scrubber = Scrubber(self.vault, literals_from_settings(self.collect_settings()))
        return " ".join(scrubber(raw).split())[:80]

    def rename_current_chat(self) -> None:
        if self.running:
            return
        if self.chat_id:
            self.rename_chat_item(self.chat_id)
            return
        if self.project is None:
            return
        name = self._ask_chat_title(self.chat_title)
        if not name or name == self.chat_title:
            return
        self.chat_title = name
        self._fit_labels()
        self.set_status("Беседа переименована")

    def rename_chat_item(self, chat_id: str) -> None:
        if self.running or not chat_id or str(chat_id).startswith(_GROUP_PREFIX):
            return
        if hasattr(self, "chat_tree") and self.chat_tree.exists(chat_id):
            old = str(self.chat_tree.item(chat_id, "text") or "")
        elif chat_id == self.chat_id:
            old = self.chat_title
        else:
            record = load_chat(chat_id)
            old = record["title"] if record else ""
        name = self._ask_chat_title(old)
        if not name or name == old:
            return
        if chat_id == self.chat_id and getattr(self, "_chat_save_pending", False):
            self._store_chat()
        try:
            saved_title = rename_chat(chat_id, name)
        except Exception as exc:
            self.write_chat(f"Чат не переименован: {exc}")
            return
        if saved_title is None:
            if chat_id == self.chat_id:
                self.chat_title = name
                self._fit_labels()
                self.set_status("Беседа переименована")
            else:
                self._refresh_chat_list()
                self.set_status("Чат не найден")
            return
        if chat_id == self.chat_id:
            self.chat_title = saved_title
            self._fit_labels()
        self._refresh_chat_list()
        self.set_status("Беседа переименована")

    def delete_chat_item(self, chat_id: str) -> None:
        if self.running or not chat_id:
            return
        title = self.chat_tree.item(chat_id, "text") if self.chat_tree.exists(chat_id) else self.chat_title
        dialog = ConfirmDialog(self, title or "Чат", "Удалить этот чат?")
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        if not dialog.result:
            return
        try:
            delete_chat(chat_id)
            delete_checkpoints(chat_id)
        except Exception as exc:
            self.write_chat(f"Чат не удалён: {exc}")
            return
        if chat_id == self.chat_id:
            self.chat_id = None
            self.chat_title = ""
            self.transcript.clear()
            self._forget_retry()
            self.checkpoints.clear()
            self._clear_box(self.chat)
            self.agent.reset_session(announce=False)
        self._remember_last_chat()
        self._refresh_chat_list()
        self._fit_labels()
        self.set_status("Чат удалён")