"""Профили моделей и оркестрация настроек (миксин ``App``).

Дословно перенесённые из ``project_agent.app`` методы: подписи индекса и правил
проекта, профили моделей, сбор и сохранение настроек, свитч подтверждения shell.

Модуль не импортирует ``project_agent.app`` (иначе цикл импортов): всё, что ему
нужно, приходит через ``self`` — экземпляр ``App``.
"""

from __future__ import annotations

import tkinter as tk

from project_agent.config import ai_settings, save_config
from project_agent.dialogs import ConfirmDialog, NameDialog
from project_agent.index_store import build_index, index_summary
from project_agent.rules import rules_summary
from project_agent.testing import (
    PRESET_LABELS,
    normalize_fix_rounds,
    normalize_shell_limit,
    normalize_timeout,
)
from project_agent.ui_theme import (
    LABEL_BY_PROVIDER,
    PANEL,
    PROVIDER_LABELS,
    SELECT,
    TEXT,
    _NO_PROFILE,
)
from project_agent.ui_widgets import _tone


class SettingsOpsMixin:
    def _refresh_rules_label(self) -> None:
        label = self.rules_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            self.rules_label = None
            return
        if self.project is None:
            label.configure(text="Выберите папку проекта.")
            return
        try:
            label.configure(text=rules_summary(self.project))
        except Exception as exc:
            label.configure(text=f"Правила недоступны: {exc}")

    def _refresh_index_label(self) -> None:
        label = self.index_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except tk.TclError:
            self.index_label = None
            return
        if self.project is None:
            label.configure(text="Выберите папку проекта.")
            return
        try:
            label.configure(text=index_summary(self.project))
        except Exception as exc:
            label.configure(text=f"Индекс недоступен: {exc}")

    def refresh_index(self) -> None:
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        try:
            data = build_index(self.project)
        except Exception as exc:
            self.write_chat(f"Индекс не обновлён: {exc}")
            return
        self._refresh_index_label()
        self.set_status(f"Индекс: {len(data.get('files') or [])} файлов")

    def _on_test_preset_pick(self, label: str) -> None:
        self.test_preset = PRESET_LABELS.get(label, "")

    def _apply_ai(self, data: dict) -> None:
        fields = ai_settings(data)
        self.provider_var.set(LABEL_BY_PROVIDER.get(fields["provider"], "OpenAI-совместимый"))
        self.base_url_var.set(fields["base_url"])
        self.model_var.set(fields["model"])
        self.api_key_var.set(fields["api_key"])
        self.steps_var.set(str(fields["max_steps"]))
        self.image_model_var.set(fields["image_model"])
        self.image_url_var.set(fields["image_base_url"])
        self.image_key_var.set(fields["image_api_key"])

    def _ai_snapshot(self) -> dict:
        try:
            steps = int(self.steps_var.get().strip() or "25")
        except ValueError:
            steps = 25
        return ai_settings(
            {
                "provider": PROVIDER_LABELS.get(self.provider_var.get(), "openai"),
                "base_url": self.base_url_var.get().strip(),
                "model": self.model_var.get().strip(),
                "api_key": self.api_key_var.get().strip(),
                "max_steps": steps,
                "image_model": self.image_model_var.get().strip(),
                "image_base_url": self.image_url_var.get().strip(),
                "image_api_key": self.image_key_var.get().strip(),
            }
        )

    def _sync_profile_menu(self) -> None:
        names = [item["name"] for item in self.profiles] or [_NO_PROFILE]
        current = self.active_profile if self.active_profile in names else names[0]
        self.profile_var.set(current)
        for menu in (self.profile_menu, self.footer_profile_menu):
            if menu is not None and menu.winfo_exists():
                menu.configure(values=names)
                menu.set(current)

    def _on_profile_pick(self, name: str) -> None:
        if name == _NO_PROFILE or name == self.active_profile:
            return
        if self.running:
            self._sync_profile_menu()
            return
        profile = next((item for item in self.profiles if item["name"] == name), None)
        if profile is None:
            return
        self._apply_ai(profile)
        self.active_profile = name
        self._persist("Профиль выбран")

    def save_profile(self) -> None:
        dialog = NameDialog(self)
        self.wait_window(dialog)
        name = " ".join(str(dialog.result or "").split())
        if not name:
            return
        if len(name) > 80 or name == _NO_PROFILE:
            self.write_chat("Такое имя профиля не подходит.")
            return
        stored = {"name": name, **self._ai_snapshot()}
        self.profiles = [stored if item["name"] == name else item for item in self.profiles]
        if all(item["name"] != name for item in self.profiles):
            self.profiles.append(stored)
        self.active_profile = name
        self._sync_profile_menu()
        self._persist("Профиль сохранён")

    def _bind_profile_rename(self, widget) -> None:
        widget.bind("<Button-3>", self._profile_menu_context)
        for child in widget.winfo_children():
            self._bind_profile_rename(child)

    def _profile_menu_context(self, event) -> str | None:
        name = self.active_profile
        if not name or name == _NO_PROFILE:
            return "break"
        menu = tk.Menu(
            self,
            tearoff=0,
            bd=0,
            bg=_tone(PANEL),
            fg=_tone(TEXT),
            activebackground=_tone(SELECT),
            activeforeground=_tone(TEXT),
            font=self._px_font(12),
        )
        menu.add_command(label="Переименовать", command=self.rename_profile)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def rename_profile(self) -> None:
        old = self.active_profile
        if not old or old == _NO_PROFILE:
            return
        if self.running:
            return
        dialog = NameDialog(self, initial=old, title="Переименовать")
        self.wait_window(dialog)
        name = " ".join(str(dialog.result or "").split())
        if not name or name == old:
            return
        if len(name) > 80 or name == _NO_PROFILE:
            self.write_chat("Такое имя профиля не подходит.")
            return
        if any(item["name"] == name for item in self.profiles):
            self.write_chat("Профиль с таким именем уже есть.")
            return
        updated = []
        for item in self.profiles:
            if item["name"] == old:
                updated.append({**item, "name": name})
            else:
                updated.append(item)
        self.profiles = updated
        self.active_profile = name
        self._sync_profile_menu()
        self._persist("Профиль переименован")

    def delete_profile(self) -> None:
        name = self.active_profile
        if not name:
            self.write_chat("Профиль не выбран.")
            return
        dialog = ConfirmDialog(self, name, "Удалить этот профиль?")
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        if not dialog.result:
            return
        self.profiles = [item for item in self.profiles if item["name"] != name]
        self.active_profile = ""
        self._sync_profile_menu()
        self._persist("Профиль удалён")

    def _on_shell_confirm_toggle(self) -> None:
        """Свитч «не спрашивать подтверждение для run_shell»: спрашиваем при включении."""
        if not self.auto_shell_no_confirm_var.get():
            self.auto_shell_no_confirm = False
            return
        parent = self._settings_window
        if parent is None or not self._window_alive(parent):
            parent = self
        dialog = ConfirmDialog(
            parent,
            "Безопасность: shell без подтверждения",
            "Запускать run_shell без диалога подтверждения?",
            "Команды будут выполняться в папке проекта сразу, без вашего клика.\n\n"
            "Остаётся в силе: подтверждение для detach (фоновые процессы без вывода и кода "
            "возврата), для run_tests / run_allowed / git commit / записи файлов и изображений, "
            "лимит запусков shell за ход и запрет shell-метасимволов.\n\n"
            "Настройка сохраняется в config.json и действует до выключения свитча.",
        )
        self.wait_window(dialog)
        allowed = bool(dialog.result)
        self.auto_shell_no_confirm_var.set(allowed)
        self.auto_shell_no_confirm = allowed

    def collect_settings(self) -> dict:
        settings = self._ai_snapshot()
        settings["project_dir"] = "" if self.project is None else str(self.project)
        settings["active_profile"] = self.active_profile
        settings["profiles"] = list(self.profiles)
        settings["mcp_servers"] = self._servers_for_save()
        settings["theme"] = self.theme
        settings["prompt_height"] = 0
        settings["test_preset"] = PRESET_LABELS.get(self.test_preset_var.get(), self.test_preset)
        settings["test_timeout"] = normalize_timeout(self.test_timeout_var.get())
        settings["test_fix_rounds"] = normalize_fix_rounds(self.test_fix_rounds_var.get())
        settings["allowed_commands"] = self._read_allowed_commands()
        settings["agent_shell_enabled"] = bool(self.agent_shell_var.get())
        self.agent_shell_enabled = settings["agent_shell_enabled"]
        settings["shell_max_per_turn"] = normalize_shell_limit(self.shell_limit_var.get())
        settings["agent_mode"] = self.agent_mode
        settings["context_limit"] = self.context_limit
        settings["auto_write_project"] = bool(self.auto_write_var.get())
        self.auto_write_project = settings["auto_write_project"]
        settings["auto_shell_no_confirm"] = bool(self.auto_shell_no_confirm_var.get())
        self.auto_shell_no_confirm = settings["auto_shell_no_confirm"]
        settings["prefer_cheap_provider"] = bool(self.prefer_cheap_var.get())
        self.prefer_cheap_provider = settings["prefer_cheap_provider"]
        settings["ui_font"] = self.ui_font
        settings["code_font"] = self.code_font
        settings["last_chats"] = dict(self.last_chats)
        return settings

    def _read_allowed_commands(self) -> list[dict]:
        from project_agent.allowed import normalize_allowed_commands, parse_allowed_text

        box = self.allowed_box
        if box is None:
            return normalize_allowed_commands(self.allowed_commands)
        try:
            exists = bool(box.winfo_exists())
        except tk.TclError:
            exists = False
        if not exists:
            return normalize_allowed_commands(self.allowed_commands)
        parsed = parse_allowed_text(box.get("1.0", "end-1c"))
        self.allowed_commands = parsed
        return parsed

    def save_settings(self) -> None:
        self._persist("Настройки сохранены")

    def _capture_active_profile(self) -> None:
        name = self.active_profile
        if not name:
            return
        stored = {"name": name, **self._ai_snapshot()}
        updated = []
        found = False
        for item in self.profiles:
            if item["name"] == name:
                updated.append(stored)
                found = True
            else:
                updated.append(item)
        if found:
            self.profiles = updated

    def _persist(self, status: str | None = None) -> None:
        try:
            self._capture_active_profile()
            save_config(self.collect_settings())
            self.saved_servers = self._servers_for_save()
            if status:
                self.set_status(status)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить настройки: {exc}")
