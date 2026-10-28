"""Футер окна: меню профиля и режима, «Сжать», шкала контекста, статус.

Перенесено из ``project_agent.app`` дословно, без изменения поведения и вида.
Внутри — только та часть ``App``, которая относится к нижней строке окна под
композером: построение виджетов (рамка ``footer`` внутри ``bottom``, меню
профиля ``footer_profile_menu``, меню режима ``mode_menu``, кнопка «Сжать»,
рамка ``ctx`` со шкалой ``context_bar`` и подписью ``context_label``,
``status_label``), переключение режима агента (``_set_agent_mode`` /
``_on_mode_pick``), приём статистики контекста от агента (``_on_context`` /
``_apply_context``) и сжатие контекста (``compress_context``).

Модуль **не импортирует** ``project_agent.app`` (иначе цикл). Всё нужное
приходит через ``self`` — экземпляр ``App``: ``bottom``, ``profile_var``,
``agent_mode_var``, ``agent_mode``, ``project``, ``running``/``stop_event``/
``_show_running``, ``agent``, ``collect_settings``, ``_finish``, ``set_status``,
``write_chat``, ``_font`` и поля ``context_limit``/``context_used``/
``context_from_api``, которые читают ``_persist``, ``send``, ``new_chat`` и
``SettingsWindow``. Поэтому код оформлен миксином ``FooterMixin``: методы
остаются методами ``App`` (``class App(..., FooterMixin, ctk.CTk)``), но живут
в отдельном файле.

Виджеты строит ``_build_footer_panel(bottom)``; вызов стоит ровно там, где был
инлайн-блок в ``App._build`` — сразу после ``_build_composer_panel()``, потому
что футер кладётся внутрь возвращённой ею рамки ``bottom``.
"""

from __future__ import annotations

import threading

import customtkinter as ctk

from project_agent.config import needs_api_key, save_agent_mode
from project_agent.ui_theme import (
    BORDER,
    BUTTON_HOVER,
    CTX_FULL,
    CTX_OK,
    CTX_WARN,
    INK,
    LABEL_BY_MODE,
    MODE_LABELS,
    MUTED,
    PANEL,
    SELECT,
    TEXT,
    _NO_PROFILE,
)
from project_agent.ui_widgets import quiet_button


class FooterMixin:
    """Нижняя строка окна: профиль, режим, сжатие контекста, статус."""

    def _build_footer_panel(self, bottom) -> None:
        footer = ctk.CTkFrame(bottom, fg_color="transparent")
        self.footer = footer
        footer.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 8))
        footer.grid_columnconfigure(3, weight=1)
        self.footer_profile_menu = ctk.CTkOptionMenu(
            footer,
            variable=self.profile_var,
            values=[_NO_PROFILE],
            command=self._on_profile_pick,
            height=26,
            width=60,
            corner_radius=8,
            dynamic_resizing=True,
            fg_color=INK,
            button_color=INK,
            button_hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(12),
            dropdown_fg_color=PANEL,
            dropdown_text_color=TEXT,
            dropdown_hover_color=SELECT,
            dropdown_font=self._font(12),
        )
        self.footer_profile_menu.grid(row=0, column=0, sticky="w")
        self.mode_menu = ctk.CTkOptionMenu(
            footer,
            variable=self.agent_mode_var,
            values=list(MODE_LABELS),
            command=self._on_mode_pick,
            height=26,
            width=96,
            corner_radius=8,
            dynamic_resizing=False,
            fg_color=INK,
            button_color=INK,
            button_hover_color=BUTTON_HOVER,
            text_color=MUTED,
            font=self._font(12),
            dropdown_fg_color=PANEL,
            dropdown_text_color=TEXT,
            dropdown_hover_color=SELECT,
            dropdown_font=self._font(12),
        )
        self.mode_menu.grid(row=0, column=1, sticky="w", padx=(8, 0))
        quiet_button(footer, "Сжать", self.compress_context, width=48, height=24).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )
        ctx = ctk.CTkFrame(footer, fg_color="transparent")
        ctx.grid(row=0, column=3, sticky="e", padx=(8, 8))
        self.context_label = ctk.CTkLabel(
            ctx,
            text="0%",
            anchor="e",
            text_color=MUTED,
            font=self._font(11),
        )
        self.context_label.pack(side="left", padx=(0, 6))
        self.context_bar = ctk.CTkProgressBar(
            ctx,
            width=96,
            height=8,
            corner_radius=4,
            progress_color=CTX_OK,
            fg_color=BORDER,
        )
        self.context_bar.pack(side="left")
        self.context_bar.set(0)
        self.status_label = ctk.CTkLabel(footer, text="Готово", anchor="e", text_color=MUTED, font=self._font(11))
        self.status_label.grid(row=0, column=4, sticky="e")

    def _set_agent_mode(self, mode: str, persist: bool = True) -> None:
        from project_agent.tools import normalize_agent_mode

        mode = normalize_agent_mode(mode)
        self.agent_mode = mode
        label = LABEL_BY_MODE.get(mode, "Agent")
        if self.agent_mode_var.get() != label:
            self.agent_mode_var.set(label)
        if persist:
            try:
                save_agent_mode(mode)
            except Exception as exc:
                self.write_chat(f"Не удалось сохранить режим: {exc}")

    def _on_mode_pick(self, choice: str) -> None:
        self._set_agent_mode(MODE_LABELS.get(choice, "agent"), persist=True)

    def _on_context(self, used: int, limit: int, from_api: bool = False) -> None:
        self.after(0, lambda: self._apply_context(used, limit, from_api))

    def _apply_context(self, used: int, limit: int, from_api: bool) -> None:
        from project_agent.context_usage import (
            FULL_RATIO,
            WARN_RATIO,
            context_ratio,
            format_context_detail,
            normalize_context_limit,
        )

        self.context_limit = normalize_context_limit(limit)
        self.context_used = max(0, int(used or 0))
        self.context_from_api = bool(from_api)
        ratio = context_ratio(self.context_used, self.context_limit)
        if hasattr(self, "context_bar") and self.context_bar is not None:
            self.context_bar.set(ratio)
            color = CTX_OK
            if ratio >= FULL_RATIO:
                color = CTX_FULL
            elif ratio >= WARN_RATIO:
                color = CTX_WARN
            self.context_bar.configure(progress_color=color)
        if hasattr(self, "context_label") and self.context_label is not None:
            self.context_label.configure(
                text=format_context_detail(self.context_used, self.context_limit, self.context_from_api)
            )

    def compress_context(self) -> None:
        if self.running:
            return
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        settings = self.collect_settings()
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Сжатие контекста…")

        def work() -> None:
            try:
                self.agent.compress_context(settings, self.stop_event)
            except Exception as exc:
                self.write_chat(f"Ошибка сжатия: {exc}")
            finally:
                self.after(0, self._finish)

        threading.Thread(target=work, daemon=True).start()
