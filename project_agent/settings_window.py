"""Окно настроек приложения.

Класс ``SettingsWindow`` перенесён из ``project_agent.app`` без изменения
поведения. Он не импортирует ``project_agent.app`` (иначе цикл): всё нужное
приходит через ``app`` — переданный экземпляр ``App``.

Что используется у ``app``: ``ui_font``/``code_font``, ``theme_var``,
``ui_font_var``, ``code_font_var``, ``profile_var``/``profile_menu``,
``provider_var``, ``base_url_var``, ``model_var``, ``api_key_var``,
``steps_var``, ``prefer_cheap_var``, ``image_*`` переменные, ``test_preset_var``,
``test_timeout_var``, ``test_fix_rounds_var``, ``agent_shell_var``,
``auto_write_var``, ``auto_shell_no_confirm_var``, ``mcp_*`` переменные и поля-виджеты (``mcp_list``,
``rules_label``, ``index_label``, ``allowed_box``), а также методы
``_field``, ``_entry``, ``_pick_font``, ``_on_theme_pick``, ``_on_profile_pick``,
``_bind_profile_rename``, ``_sync_profile_menu``, ``_on_test_preset_pick``,
``_render_mcp``, ``_refresh_rules_label``, ``_refresh_index_label``,
``save_settings``, ``reset_fonts``, ``save_profile``, ``delete_profile``,
``add_mcp``, ``remove_mcp``, ``add_browser_mcp``, ``refresh_index``.
"""

from __future__ import annotations

import customtkinter as ctk

from project_agent.testing import PRESET_LABELS
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    CTX_OK,
    FIELD,
    INK,
    MUTED,
    PANEL,
    PROVIDER_LABELS,
    SELECT,
    TEXT,
    THEME_LABELS,
    _NO_PROFILE,
)
from project_agent.ui_widgets import bind_wheel_scroll, quiet_button, quiet_menu


class SettingsWindow(ctk.CTkToplevel):
    _SECTIONS = ("Внешний вид", "Модель", "Проект", "MCP", "Безопасность")

    def __init__(self, app: "App", section: str = "Модель") -> None:
        super().__init__(app)
        self.app = app
        self._pages: dict[str, ctk.CTkScrollableFrame] = {}
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        start = section if section in self._SECTIONS else "Модель"
        face = app.ui_font
        self.title("Настройки")
        self.geometry("640x720")
        self.minsize(560, 520)
        self.configure(fg_color=INK)
        self.transient(app)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        nav = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=16, width=168)
        nav.grid(row=0, column=0, sticky="nsw", padx=(12, 6), pady=12)
        nav.grid_propagate(False)
        nav.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(nav, text="Разделы", anchor="w", text_color=MUTED, font=(face, 11)).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(12, 8)
        )
        for index, name in enumerate(self._SECTIONS, start=1):
            button = ctk.CTkButton(
                nav,
                text=name,
                anchor="w",
                height=34,
                corner_radius=10,
                border_width=0,
                fg_color="transparent",
                hover_color=BUTTON_HOVER,
                text_color=TEXT,
                font=(face, 12),
                command=lambda section_name=name: self._show(section_name),
            )
            button.grid(row=index, column=0, sticky="ew", padx=8, pady=2)
            self._nav_buttons[name] = button

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        for name in self._SECTIONS:
            page = ctk.CTkScrollableFrame(
                body,
                fg_color=PANEL,
                corner_radius=16,
                scrollbar_button_color=BUTTON,
                scrollbar_button_hover_color=BUTTON_HOVER,
            )
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[name] = page

        self._fill_appearance(self._pages["Внешний вид"])
        self._fill_model(self._pages["Модель"])
        self._fill_project(self._pages["Проект"])
        self._fill_mcp(self._pages["MCP"])
        self._fill_security(self._pages["Безопасность"])

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        quiet_button(footer, "Сохранить", app.save_settings, width=160, primary=True, mark="check", family=face).pack(
            side="right"
        )

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._show(start)
        self.after(50, self.focus)

    def _fill_appearance(self, page) -> None:
        app = self.app
        face = app.ui_font
        ctk.CTkLabel(page, text="Внешний вид", anchor="w", text_color=TEXT, font=(face, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app._field(page, "Тема")
        quiet_menu(page, app.theme_var, list(THEME_LABELS), app._on_theme_pick, family=face).pack(
            fill="x", padx=8, pady=4
        )
        app._field(page, "Основной интерфейс", top=10)
        ui_box = ctk.CTkLabel(
            page,
            textvariable=app.ui_font_var,
            anchor="w",
            height=34,
            corner_radius=12,
            fg_color=FIELD,
            text_color=TEXT,
            font=(face, 12),
            cursor="hand2",
        )
        ui_box.pack(fill="x", padx=8, pady=4)
        ui_box.bind("<Button-3>", lambda _e: app._pick_font("ui_font"))
        ctk.CTkLabel(
            page,
            text="ПКМ по названию — выбрать шрифт. Применяется сразу.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(face, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        app._field(page, "Вывод кода и логов", top=10)
        code_box = ctk.CTkLabel(
            page,
            textvariable=app.code_font_var,
            anchor="w",
            height=34,
            corner_radius=12,
            fg_color=FIELD,
            text_color=TEXT,
            font=(face, 12),
            cursor="hand2",
        )
        code_box.pack(fill="x", padx=8, pady=4)
        code_box.bind("<Button-3>", lambda _e: app._pick_font("code_font"))
        ctk.CTkLabel(
            page,
            text="Редактор, консоль тестов, лог MCP, allowlist, диффы. Для кода удобнее моноширинный.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(face, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        quiet_button(page, "По умолчанию", app.reset_fonts, width=140, family=face).pack(
            anchor="w", padx=8, pady=(10, 4)
        )

    def _fill_model(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Модель и профили", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(6, 2)
        )
        app._field(page, "Профиль", top=4)
        app.profile_menu = quiet_menu(page, app.profile_var, [_NO_PROFILE], app._on_profile_pick)
        app.profile_menu.pack(fill="x", padx=8, pady=(2, 2))
        app._bind_profile_rename(app.profile_menu)
        app._sync_profile_menu()
        app._field(page, "Провайдер", top=4)
        quiet_menu(page, app.provider_var, list(PROVIDER_LABELS)).pack(fill="x", padx=8, pady=(2, 2))
        app._entry(page, "URL API", app.base_url_var, "пусто — OpenAI, Anthropic или Responses", top=4, gap=2)
        app._entry(page, "Модель", app.model_var, "имя модели", top=4, gap=2)
        app._entry(page, "Ключ", app.api_key_var, "", secret=True, top=4, gap=2)
        app._entry(page, "Лимит шагов", app.steps_var, "25", top=4, gap=2)
        ctk.CTkSwitch(
            page,
            text="Выбирать самый дешёвый провайдер",
            variable=app.prefer_cheap_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(10, 2))
        ctk.CTkLabel(
            page,
            text="В запрос добавляется provider: { sort: \"price\" }. Выкл — авторежим агрегатора. "
            "Настройка общая, не в профиле. Генерацию изображений не затрагивает.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(page, text="Генерация изображений", anchor="w", text_color=TEXT, font=(app.ui_font, 13)).pack(
            fill="x", padx=8, pady=(10, 2)
        )
        ctk.CTkLabel(
            page,
            text="Пустые поля — генерация выключена; URL и ключ по умолчанию из чата выше.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 2))
        app._entry(page, "Модель изображений", app.image_model_var, "пусто — генерация выключена", top=4, gap=2)
        app._entry(page, "URL изображений", app.image_url_var, "пусто — URL API", top=4, gap=2)
        app._entry(page, "Ключ изображений", app.image_key_var, "пусто — основной ключ", secret=True, top=4, gap=2)
        ctk.CTkLabel(page, text="Действия с профилем", anchor="w", text_color=TEXT, font=(app.ui_font, 13)).pack(
            fill="x", padx=8, pady=(12, 2)
        )
        ctk.CTkLabel(
            page,
            text="«Сохранить профиль» записывает текущие поля выше в выбранный или новый профиль. "
            "Кнопка «Сохранить» внизу окна — все настройки приложения.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        profile_row = ctk.CTkFrame(page, fg_color="transparent")
        profile_row.pack(fill="x", padx=8, pady=(2, 8))
        quiet_button(profile_row, "Сохранить профиль", app.save_profile, width=188, mark="save").pack(
            side="left", padx=(0, 4)
        )
        quiet_button(profile_row, "Удалить", app.delete_profile, width=112, mark="trash").pack(side="left")

    def _fill_project(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Проект и тесты", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkLabel(
            page,
            text="Пресет тестов — для run_tests. Allowlist — для run_allowed. Почти свободный shell (run_shell) — по чекбоксу ниже, выкл по умолчанию. Подтверждение — для каждого запуска, если его не сняли в разделе Безопасность.",
            wraplength=400,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(0, 8))
        app._field(page, "Пресет тестов")
        quiet_menu(page, app.test_preset_var, list(PRESET_LABELS), app._on_test_preset_pick).pack(
            fill="x", padx=8, pady=4
        )
        app._entry(page, "Таймаут тестов (сек)", app.test_timeout_var, "120")
        app._entry(page, "Лимит запусков тестов за ход", app.test_fix_rounds_var, "3")
        ctk.CTkSwitch(
            page,
            text="Почти свободный shell для агента (run_shell)",
            variable=app.agent_shell_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(8, 2))
        ctk.CTkLabel(
            page,
            text=(
                "Выкл по умолчанию. Только Agent/Debug; confirm каждый раз "
                "(снять можно в разделе Безопасность, для detach он остаётся). Не интерактивный терминал. "
                "Агент обязан указать причину запуска (summary) — она видна в диалоге подтверждения."
            ),
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        app._entry(page, "Лимит запусков shell за ход (0 — без лимита)", app.shell_limit_var, "3")
        app._field(page, "Свои команды allowlist")
        ctk.CTkLabel(
            page,
            text="Встроенные id: unittest, pytest, npm_test, build_ps1. Свои — id: arg1 arg2 …. Для GUI без ожидания: id!: arg1 … (detach).",
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        from project_agent.allowed import format_allowed_text

        app.allowed_box = ctk.CTkTextbox(
            page,
            height=88,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            font=(app.code_font, 11),
        )
        app.allowed_box.pack(fill="x", padx=8, pady=4)
        app.allowed_box.insert("1.0", format_allowed_text(getattr(app, "allowed_commands", [])))
        bind_wheel_scroll(app.allowed_box._textbox, app.allowed_box)
        app._field(page, "Правила проекта")
        app.rules_label = ctk.CTkLabel(
            page,
            text="Правила не найдены (AGENTS.md или .projectagent/rules).",
            anchor="w",
            justify="left",
            wraplength=400,
            text_color=MUTED,
        )
        app.rules_label.pack(fill="x", padx=8, pady=4)
        app._field(page, "Индекс файлов")
        app.index_label = ctk.CTkLabel(page, text="Индекс ещё не построен.", anchor="w", text_color=MUTED)
        app.index_label.pack(fill="x", padx=8, pady=4)
        quiet_button(page, "Обновить индекс", app.refresh_index, width=180, mark="refresh").pack(
            fill="x", padx=8, pady=4
        )
        app._refresh_rules_label()
        app._refresh_index_label()

    def _fill_mcp(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="MCP-серверы", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        app.mcp_list = ctk.CTkTextbox(
            page,
            height=120,
            fg_color=FIELD,
            text_color=TEXT,
            border_color=BORDER,
            border_width=1,
            font=(app.ui_font, 12),
        )
        app.mcp_list.pack(fill="x", padx=8, pady=4)
        app.mcp_list.configure(state="disabled")
        bind_wheel_scroll(app.mcp_list._textbox, app.mcp_list)
        app._render_mcp()
        app._entry(page, "Имя MCP", app.mcp_name_var, "filesystem")
        app._entry(page, "Команда MCP", app.mcp_command_var, "python -m server")
        mcp_row = ctk.CTkFrame(page, fg_color="transparent")
        mcp_row.pack(fill="x", padx=8, pady=4)
        quiet_button(mcp_row, "Добавить", app.add_mcp, width=124, mark="plus").pack(side="left", padx=(0, 4))
        quiet_button(mcp_row, "Удалить", app.remove_mcp, width=112, mark="trash").pack(side="left", padx=(0, 4))
        quiet_button(mcp_row, "Браузер Playwright", app.add_browser_mcp, width=168).pack(side="left")
        ctk.CTkLabel(
            page,
            text=(
                "Браузер — через MCP Playwright (кнопка выше), не встроенный Chrome. "
                "Нужен Node.js (npx). Агент вызывает инструмент browser: открыть → snapshot → клик.\n"
                "Переменные env для сервера дописываются в config.json вручную."
            ),
            wraplength=400,
            justify="left",
            text_color=MUTED,
        ).pack(fill="x", padx=8, pady=(8, 4))

    def _fill_security(self, page) -> None:
        app = self.app
        ctk.CTkLabel(page, text="Безопасность", anchor="w", text_color=TEXT, font=(app.ui_font, 14)).pack(
            fill="x", padx=8, pady=(8, 4)
        )
        ctk.CTkSwitch(
            page,
            text="Разрешить правки в папке проекта без подтверждения",
            variable=app.auto_write_var,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(8, 4))
        ctk.CTkSwitch(
            page,
            text="Не спрашивать подтверждение для run_shell",
            variable=app.auto_shell_no_confirm_var,
            command=app._on_shell_confirm_toggle,
            text_color=TEXT,
            progress_color=CTX_OK,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            font=(app.ui_font, 12),
        ).pack(fill="x", padx=8, pady=(8, 2))
        ctk.CTkLabel(
            page,
            text=(
                "Выкл по умолчанию. При включении спросим подтверждение один раз. "
                "Detach-запуски (фоновые процессы) спрашивают всегда: у них нет ни вывода, "
                "ни кода возврата. Нужен включённый «Почти свободный shell» в разделе Проект."
            ),
            wraplength=400,
            justify="left",
            text_color=MUTED,
            font=(app.ui_font, 11),
        ).pack(fill="x", padx=8, pady=(0, 4))
        notes = (
            "Ключ и настройки хранятся только в %APPDATA%\\ProjectAgent\\config.json — в программу они не зашиты.\n\n"
            "Перед отправкой модели пароли и похожие значения заменяются метками [[SEC:...]].\n\n"
            "По умолчанию запись файла спрашивает подтверждение. Переключатель выше снимает диалог "
            "только для write_file / apply_patch внутри открытой папки проекта.\n\n"
            "Генерация изображения, run_tests, run_allowed и git commit всегда спрашивают подтверждение.\n\n"
            "run_shell спрашивает подтверждение по умолчанию; свитч выше снимает диалог, "
            "кроме detach-запусков (фоновые процессы).\n\n"
            "Почти свободный shell (run_shell) — только если включён в Проект; по умолчанию выкл. "
            "Тесты — пресет; разовые команды — id из allowlist; shell — argv без метасимволов. "
            "Лимит shell за ход настраивается (0 — без лимита). Агент обязан указать причину запуска.\n\n"
            "Браузер не встроен: только MCP Playwright из настроек (если добавлен).\n\n"
            "Агент не выходит за выбранную папку проекта."
        )
        ctk.CTkLabel(page, text=notes, wraplength=400, justify="left", anchor="w", text_color=MUTED).pack(
            fill="x", padx=8, pady=4
        )

    def _show(self, name: str) -> None:
        if name not in self._pages:
            return
        for section, page in self._pages.items():
            if section == name:
                page.grid()
            else:
                page.grid_remove()
        for section, button in self._nav_buttons.items():
            active = section == name
            button.configure(fg_color=BUTTON if active else "transparent", hover_color=BUTTON_HOVER if active else SELECT)

    def _close(self) -> None:
        self.app.mcp_list = None
        self.app.profile_menu = None
        self.app.index_label = None
        self.app.rules_label = None
        self.app.allowed_box = None
        self.app._settings_window = None
        self.destroy()
