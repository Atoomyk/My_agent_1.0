from __future__ import annotations

import multiprocessing
import shlex
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont

import customtkinter as ctk

from project_agent.agent import Agent
from project_agent.chat_markup import (
    chat_body,
    chat_role,
    chat_segments,
    is_running_journal,
    split_chat_stamp,
    tool_group_title,
    tool_short_name,
    user_copy_text,
    with_chat_stamp,
)
from project_agent.clipboard import (
    apply_layout_clipboard,
    clipboard_action,
    set_clipboard,
    _on_layout_clipboard,
)
from project_agent.chat_view import ChatViewMixin
from project_agent.chats import delete_chat, group_chats, list_chats, load_chat, new_chat_id, save_chat
from project_agent.checkpoints import (
    CheckpointStack,
    checkpoint_mark,
    delete_checkpoints,
    parse_checkpoint_mark,
    restorable_paths,
    restore_one_file,
    restore_files,
)
from project_agent.composer import ComposerMixin, _TASK_MIN_H
from project_agent.config import (
    DEFAULT_CODE_FONT,
    DEFAULT_UI_FONT,
    ai_settings,
    config_dir,
    folder_key,
    load_config,
    needs_api_key,
    save_config,
    save_font,
    save_last_chats,
    save_theme,
)
from project_agent.context_attach import (
    MAX_CONTEXT_DIRS,
    MAX_CONTEXT_FILES,
    compose_user_text,
    load_explicit_context,
    merge_paths,
    parse_at_paths,
)
from project_agent.images import prepare_image
_GROUP_PREFIX = "__group__:"
_GROUP_INDENT = "   "
from project_agent.mcp_client import McpHub
from project_agent.editor_panel import EditorPanelMixin
from project_agent.editor_tabs import EditorTabs
from project_agent.footer import FooterMixin
from project_agent.output_panel import OutputPanelMixin
from project_agent.secrets import Scrubber, SecretVault, literals_from_settings, scrub_outbound
from project_agent.index_store import build_index, index_summary
from project_agent.rules import rules_summary
from project_agent.testing import (
    DEFAULT_SHELL_PER_TURN,
    LABEL_BY_PRESET,
    PRESET_LABELS,
    normalize_fix_rounds,
    normalize_preset,
    normalize_shell_limit,
    normalize_timeout,
)
from project_agent.tools import Toolbox
from project_agent.ui_theme import (
    BORDER,
    BUTTON,
    BUTTON_HOVER,
    CHAT_COLUMN,
    FIELD,
    HINT,
    INK,
    LABEL_BY_PROVIDER,
    LABEL_BY_THEME,
    MUTED,
    PANEL,
    PROVIDER_LABELS,
    SELECT,
    SIDE_DEFAULT,
    TEXT,
    THEME_LABELS,
    _NO_PROFILE,
)
from project_agent.dialogs import (
    ConfirmDialog,
    FontPickDialog,
    GitFileDiffDialog,
    NameDialog,
    TurnDiffDialog,
)
from project_agent.settings_window import SettingsWindow
from project_agent.side_panel import SidePanelMixin
from project_agent.ui_widgets import (
    bind_wheel_scroll,
    elide,
    glyph,
    icon_button,
    quiet_button,
    tree_path_iid,
    tree_path_rel,
    _PENDING,
    _font_catalog,
    _tone,
)


class App(OutputPanelMixin, ChatViewMixin, SidePanelMixin, EditorPanelMixin, ComposerMixin, FooterMixin, ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ProjectAgent")
        self.geometry("1100x760")
        self.minsize(900, 560)
        self.configure(fg_color=INK)
        self.project: Path | None = None
        self.attached: list[str] = []
        self.context_files: list[str] = []
        self.context_dirs: list[str] = []
        self._at_popup: tk.Toplevel | None = None
        self._at_list: tk.Listbox | None = None
        self._at_start: str | None = None
        self.mcp_servers: list[dict] = []
        self.saved_servers: list[dict] = []
        self.stop_event = threading.Event()
        self.running = False
        self._dialog: ConfirmDialog | None = None
        self._settings_window: SettingsWindow | None = None
        self.mcp_list: ctk.CTkTextbox | None = None
        self.profile_menu: ctk.CTkOptionMenu | None = None
        self.index_label: ctk.CTkLabel | None = None
        self.rules_label: ctk.CTkLabel | None = None
        self.profiles: list[dict] = []
        self.active_profile = ""
        self.profile_var = ctk.StringVar(value=_NO_PROFILE)
        self.footer_profile_menu: ctk.CTkOptionMenu | None = None
        self.mode_menu: ctk.CTkOptionMenu | None = None
        self._chat_pad = 0
        self.chat_id: str | None = None
        self.chat_title = ""
        self.transcript: list[str] = []
        self._chat_list_lock = False
        self._stream_open = False
        self._stream_origin = "1.0"
        self._stream_body_at = "1.0"
        self._user_spans: list[tuple[str, str, str]] = []
        self._user_bubbles: list[dict] = []
        self._retry_payload: dict | None = None
        self._can_retry = False
        self._retry_buttons: list[tk.Button] = []
        self._checkpoint_buttons: list[tk.Button] = []
        self._tool_groups: list[dict] = []
        self._tool_group: dict | None = None
        self._group_seq = 0
        self._running_tool: tuple[str, str] | None = None
        self._chip_width = 0
        self._chat_stick = True
        self._search_open = False
        self._search_hits: list[tuple[str, str]] = []
        self._search_index = -1
        self._search_query = ""
        self._closing = False
        self._fs_watcher = None
        self._watcher_job = None
        self._file_util_mode = "find"
        self._find_hits: list = []
        self._diag_hits: list = []
        self._symbol_hits: list = []
        self._symbol_query_mode = "symbol"
        self.checkpoints = CheckpointStack()
        self.vault = SecretVault()
        self.mcp = McpHub()
        self.toolbox = Toolbox(self.vault, self.confirm, self.mcp, self.collect_settings)
        self.toolbox.on_run_output = self._on_run_output
        self._init_output_state()
        self.agent = Agent(
            self.vault,
            self.toolbox,
            self.mcp,
            self.write_chat,
            self.write_journal,
            self.set_status,
            self._on_context,
            self.stream_chat,
            self.write_retryable_error,
            on_stopped=self.write_stopped,
            on_checkpoint=self._on_checkpoint,
            transcript_len=lambda: len(self.transcript),
        )
        self.agent.checkpoints = self.checkpoints
        self.theme = "dark"
        self.theme_var = ctk.StringVar(value="Тёмная")
        self.ui_font = DEFAULT_UI_FONT
        self.code_font = DEFAULT_CODE_FONT
        self.ui_font_var = ctk.StringVar(value=DEFAULT_UI_FONT)
        self.code_font_var = ctk.StringVar(value=DEFAULT_CODE_FONT)
        self.editor_open = False
        self.editor_path = ""
        self.editor_saved = ""
        self.editor_width = 420
        self.side_width = SIDE_DEFAULT
        self.editor_tabs = EditorTabs()
        self._editor_tab_buttons: list = []
        self._highlight_job = None
        self._task_height = _TASK_MIN_H
        self._task_width = 0
        self._fit_composer_job = None
        self._window_h = 0
        self.agent_mode = "agent"
        self.agent_mode_var = ctk.StringVar(value="Agent")
        self.context_limit = 256000
        self.context_used = 0
        self.context_from_api = False
        self.provider_var = ctk.StringVar(value="OpenAI-совместимый")
        self.base_url_var = ctk.StringVar()
        self.model_var = ctk.StringVar()
        self.api_key_var = ctk.StringVar()
        self.steps_var = ctk.StringVar(value="25")
        self.image_model_var = ctk.StringVar()
        self.image_url_var = ctk.StringVar()
        self.image_key_var = ctk.StringVar()
        self.mcp_name_var = ctk.StringVar()
        self.mcp_command_var = ctk.StringVar()
        self.test_preset = ""
        self.test_preset_var = ctk.StringVar(value="Выключено")
        self.test_timeout_var = ctk.StringVar(value="120")
        self.test_fix_rounds_var = ctk.StringVar(value="3")
        self.allowed_commands: list[dict] = []
        self.allowed_box = None
        self.agent_shell_enabled = False
        self.agent_shell_var = ctk.BooleanVar(value=False)
        self.shell_limit_var = ctk.StringVar(value=str(DEFAULT_SHELL_PER_TURN))
        self.auto_write_project = False
        self.auto_write_var = ctk.BooleanVar(value=False)
        self.auto_shell_no_confirm = False
        self.auto_shell_no_confirm_var = ctk.BooleanVar(value=False)
        self.prefer_cheap_provider = False
        self.prefer_cheap_var = ctk.BooleanVar(value=False)
        self.last_chats: dict[str, str] = {}
        self._git_file_status: dict[str, str] = {}
        self._git_dirty_dirs: set[str] = set()
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._ui_error

    def _build(self) -> None:
        self.bind_class("Text", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.bind_class("Entry", "<Control-KeyPress>", _on_layout_clipboard, add="+")
        self.grid_columnconfigure(0, weight=0, minsize=36)
        self.grid_columnconfigure(1, weight=0, minsize=1)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)

        rail = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=36)
        rail.grid(row=0, column=0, sticky="nsew")
        rail.grid_propagate(False)
        self.project_badge = ctk.CTkButton(
            rail,
            text="·",
            width=28,
            height=28,
            corner_radius=8,
            border_width=1,
            border_color=BORDER,
            fg_color=BUTTON,
            hover_color=BUTTON_HOVER,
            text_color=TEXT,
            font=self._font(11, weight="bold"),
            command=self.choose_folder,
        )
        self.project_badge.place(relx=0.5, y=12, anchor="n")
        icon_button(rail, "gear", self.open_settings, size=28).place(relx=0.5, rely=1.0, y=-12, anchor="s")
        ctk.CTkFrame(self, fg_color=BORDER, corner_radius=0, width=1).grid(row=0, column=1, sticky="ns")

        self.body = tk.PanedWindow(
            self,
            orient="horizontal",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_h_double_arrow",
        )
        self.body.grid(row=0, column=2, sticky="nsew")
        self.body.bind("<ButtonRelease-1>", self._remember_side_width)

        self._build_side_panel()

        self.work = tk.PanedWindow(
            self.body,
            orient="horizontal",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_h_double_arrow",
        )
        self.work.bind("<ButtonRelease-1>", self._remember_editor_width)
        self.center = ctk.CTkFrame(self.work, fg_color=INK, corner_radius=0)
        self.center.grid_columnconfigure(0, weight=1)
        self.center.grid_rowconfigure(1, weight=1)
        self.work.add(self.center, stretch="always", minsize=360, sticky="nsew")
        self.body.add(self.work, stretch="always", minsize=360, sticky="nsew")
        self.after(30, self._place_side_sash)
        head_bar = ctk.CTkFrame(self.center, fg_color="transparent")
        self.head_bar = head_bar
        head_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(12, 6))
        head_bar.grid_columnconfigure(0, weight=1)
        self.chat_head = ctk.CTkLabel(
            head_bar, text="Новый чат", anchor="w", text_color=TEXT, font=self._font(13, weight="bold")
        )
        self.chat_head.grid(row=0, column=0, sticky="ew")
        self.chat_head.bind("<Configure>", lambda _event: self._fit_labels())
        self.git_label = ctk.CTkLabel(
            head_bar, text="", anchor="e", text_color=MUTED, font=self._font(11), cursor="hand2"
        )
        self.git_label.grid(row=0, column=1, padx=(8, 6), sticky="e")
        self.git_label.bind("<Button-1>", lambda _event: self._on_git_badge_click())
        self.run_indicator = ctk.CTkLabel(
            head_bar, text="", anchor="e", text_color=MUTED, font=self._font(11), width=1
        )
        self.run_indicator.grid(row=0, column=2, padx=(4, 6), sticky="e")
        self.output_toggle = quiet_button(head_bar, "Вывод", self.toggle_output_panel, width=48, height=24)
        self.output_toggle.grid(row=0, column=3, sticky="e")
        self.center.grid_rowconfigure(2, weight=0)
        self.mid_split = tk.PanedWindow(
            self.center,
            orient="vertical",
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            bg=_tone(INK),
            sashcursor="sb_v_double_arrow",
        )
        self.mid_split.grid(row=1, column=0, sticky="nsew")
        self._build_chat_view()

        self._build_output_panel()

        self._build_editor_panel()

        bottom = self._build_composer_panel()
        self._build_footer_panel(bottom)
        self.bind("<Configure>", self._on_window_configure, add="+")
        self.after(80, self._fit_composer)

    def _font(self, size: int, family: str | None = None, weight: str = "normal") -> tuple:
        return (family or self.ui_font, size, weight)

    def _code_font(self, size: int, weight: str = "normal") -> tuple:
        return self._font(size, self.code_font, weight)

    def _px_font(self, size: int, family: str | None = None, weight: str = "normal") -> tuple:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        return (family or self.ui_font, -round(size * scale), weight)

    def _measure(self, size: int, weight: str = "normal"):
        key = (size, weight, self.ui_font)
        cache = self.__dict__.setdefault("_measure_fonts", {})
        font = cache.get(key)
        if font is None:
            font = tkfont.Font(font=self._px_font(size, weight=weight))
            cache[key] = font
        return font.measure

    def _fit_labels(self) -> None:
        if self._closing or not hasattr(self, "chat_head"):
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        if self.project is None:
            name, path = "Папка не выбрана", "нажмите «…», чтобы выбрать"
        else:
            name, path = self.project.name or str(self.project), str(self.project)
        title = self.chat_title or "Новый чат"
        for label, text, size, weight in (
            (self.folder_name, name, 13, "bold"),
            (self.folder_path, path, 11, "normal"),
            (self.chat_head, title, 13, "bold"),
        ):
            try:
                if not label.winfo_exists():
                    continue
                width = label.winfo_width() - 4
                shown = elide(text, width, self._measure(size, weight)) if width > 20 else text
                if label.cget("text") != shown:
                    label.configure(text=shown)
            except tk.TclError:
                continue

    def _show_folder(self) -> None:
        letter = (self.project.name[:1] if self.project is not None else "") or "·"
        self.project_badge.configure(text=letter.upper())
        self._fit_labels()
        self._refresh_tree()
        self._refresh_chat_list()
        self._refresh_index_label()
        self._refresh_rules_label()
        self._refresh_attach()
        self._refresh_git_badge()
        self._restart_project_watch()

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

    def _column_pad(self, width: int, least: int) -> int:
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        column = round(CHAT_COLUMN * scale)
        return max(least, (width - column) // 2)

    def _center_chat(self, event=None) -> None:
        width = event.width if event is not None else self.chat._textbox.winfo_width()
        pad = self._column_pad(width, 16)
        if pad != self._chat_pad:
            self._chat_pad = pad
            self.chat._textbox.configure(padx=pad)
        bubble_w = self._user_bubble_width()
        if getattr(self, "_bubble_width", None) != bubble_w:
            self._bubble_width = bubble_w
            self._restyle_user_bubbles()

    def _on_window_configure(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            height = int(self.winfo_height())
        except tk.TclError:
            return
        if height < 200 or height == self._window_h:
            return
        self._window_h = height
        self._schedule_fit_composer()

    def _send_or_stop(self) -> None:
        if self.running:
            self.stop()
        else:
            self.send()

    def _show_running(self, running: bool) -> None:
        self.send_button.configure(image=glyph("halt" if running else "send"))

    def _chat_menu(self, event) -> str | None:
        row = self.chat_tree.identify_row(event.y)
        if not row or self.running or str(row).startswith(_GROUP_PREFIX):
            return None
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
        menu.add_command(label="Удалить", command=lambda: self.delete_chat_item(str(row)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _ask(self, path: str, summary: str) -> bool:
        dialog = ConfirmDialog(self, path, summary)
        self._dialog = dialog
        self.wait_window(dialog)
        self._dialog = None
        return bool(dialog.result)

    def open_settings(self, section: str | None = None) -> None:
        window = self._settings_window
        if self._window_alive(window):
            try:
                if str(window.state()) == "withdrawn":
                    window.deiconify()
                if section:
                    window._show(section)
                window.lift()
                window.focus()
                return
            except tk.TclError:
                self._settings_window = None
        else:
            self._settings_window = None
        try:
            self._settings_window = SettingsWindow(self, section=section or "Модель")
        except Exception as exc:
            self._settings_window = None
            self.write_chat(f"Не удалось открыть настройки: {exc}")

    def _window_alive(self, window) -> bool:
        if window is None:
            return False
        try:
            return bool(window.winfo_exists())
        except tk.TclError:
            return False

    def _restore_theme_windows(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._unhide(self)
        window = self._settings_window
        if window is None:
            return
        if not self._window_alive(window):
            self._settings_window = None
            return
        try:
            hidden = str(window.state()) == "withdrawn"
        except tk.TclError:
            self._settings_window = None
            return
        self._unhide(window)
        if not hidden:
            return
        try:
            window.focus()
        except tk.TclError:
            self._settings_window = None

    def _unhide(self, window) -> None:
        try:
            if str(window.state()) == "withdrawn":
                window.deiconify()
            window.lift()
        except tk.TclError:
            return

    def _field(self, parent, text: str, *, top: int = 8) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w", text_color=MUTED).pack(fill="x", padx=8, pady=(top, 0))

    def _entry(
        self,
        parent,
        label: str,
        variable,
        placeholder: str,
        secret: bool = False,
        *,
        top: int = 8,
        gap: int = 4,
    ) -> None:
        self._field(parent, label, top=top)
        entry = ctk.CTkEntry(
            parent,
            textvariable=variable,
            placeholder_text=placeholder,
            show="*" if secret else "",
            fg_color=FIELD,
            border_color=BORDER,
            text_color=TEXT,
            placeholder_text_color=MUTED,
            height=36,
            corner_radius=12,
        )
        entry.pack(fill="x", padx=8, pady=gap)

    def _load(self) -> None:
        data, error = load_config()
        self._apply_theme(data["theme"])
        self.profiles = list(data["profiles"])
        self.active_profile = data["active_profile"]
        self._sync_profile_menu()
        self._apply_ai(data)
        self.test_preset = normalize_preset(data.get("test_preset"))
        self.test_preset_var.set(LABEL_BY_PRESET.get(self.test_preset, "Выключено"))
        self.test_timeout_var.set(str(normalize_timeout(data.get("test_timeout"))))
        self.test_fix_rounds_var.set(str(normalize_fix_rounds(data.get("test_fix_rounds"))))
        self.shell_limit_var.set(str(normalize_shell_limit(data.get("shell_max_per_turn"))))
        from project_agent.allowed import normalize_allowed_commands

        self.allowed_commands = normalize_allowed_commands(data.get("allowed_commands"))
        self.agent_shell_enabled = bool(data.get("agent_shell_enabled"))
        self.agent_shell_var.set(self.agent_shell_enabled)
        self.auto_write_project = bool(data.get("auto_write_project"))
        self.auto_write_var.set(self.auto_write_project)
        self.auto_shell_no_confirm = bool(data.get("auto_shell_no_confirm"))
        self.auto_shell_no_confirm_var.set(self.auto_shell_no_confirm)
        self.prefer_cheap_provider = bool(data.get("prefer_cheap_provider"))
        self.prefer_cheap_var.set(self.prefer_cheap_provider)
        self._apply_font(data.get("ui_font"), data.get("code_font"), rebuild_chat=False, reopen_settings=False)
        self._set_agent_mode(data.get("agent_mode") or "agent", persist=False)
        from project_agent.context_usage import normalize_context_limit

        self.context_limit = normalize_context_limit(data.get("context_limit"))
        self._apply_context(0, self.context_limit, False)
        self.mcp_servers = list(data["mcp_servers"])
        self.saved_servers = list(data["mcp_servers"])
        self._render_mcp()
        self.last_chats = {folder_key(key): value for key, value in (data.get("last_chats") or {}).items()}
        self._prune_last_chats()
        self._restore_project(data.get("project_dir") or "")
        if error:
            self.write_chat(f"Файл настроек не прочитан ({error}). Поля пустые, сохраните их заново.")

    def _apply_theme(self, theme: str) -> None:
        theme = "light" if theme == "light" else "dark"
        self.theme = theme
        label = LABEL_BY_THEME[theme]
        if self.theme_var.get() != label:
            self.theme_var.set(label)
        ctk.set_appearance_mode(theme)
        self._restore_theme_windows()
        self.after(60, self._restore_theme_windows)
        if hasattr(self, "body"):
            self.body.configure(bg=_tone(INK))
        if hasattr(self, "work"):
            self.work.configure(bg=_tone(INK))
        if hasattr(self, "mid_split"):
            self.mid_split.configure(bg=_tone(INK))
        if hasattr(self, "tree"):
            self._style_tree()
        if hasattr(self, "chat_tree"):
            self._style_chat_groups()
        if hasattr(self, "chat"):
            self._tag_chat()
            self._copy_photo_cache = None
        if hasattr(self, "placeholder"):
            self.placeholder.configure(fg=_tone(HINT), bg=_tone(FIELD), font=self._font(11))
        if hasattr(self, "git_label"):
            self._refresh_git_badge()
        if hasattr(self, "editor_box"):
            self._style_editor_gutter()
        if hasattr(self, "file_util_panel"):
            self._style_file_util_panel()
        if hasattr(self, "output_frame"):
            self.output_frame.configure(fg_color=PANEL)
            self.output_title.configure(text_color=MUTED)
            self.run_indicator.configure(text_color=MUTED)
            self.output_box.configure(fg_color=FIELD, text_color=TEXT, font=self._code_font(12))
            self.output_slot_menu.configure(
                fg_color=FIELD,
                button_color=FIELD,
                button_hover_color=BUTTON_HOVER,
                text_color=MUTED,
                dropdown_fg_color=PANEL,
                dropdown_text_color=TEXT,
                dropdown_hover_color=SELECT,
            )
        self._apply_font(rebuild_chat=False, reopen_settings=False)

    def _pick_font(self, which: str) -> None:
        current = self.ui_font if which == "ui_font" else self.code_font
        dialog = FontPickDialog(self, current=current)
        self.wait_window(dialog)
        name = str(dialog.result or "").strip()
        if not name:
            return
        _with_cyr, _without, cyr_set = _font_catalog(self)
        if name not in cyr_set:
            self.write_chat(f"Шрифт «{name}» без кириллицы — русский текст может отображаться некорректно.")
        if which == "ui_font":
            self._apply_font(ui=name, persist=True)
        else:
            self._apply_font(code=name, persist=True)

    def reset_fonts(self) -> None:
        if self.ui_font == DEFAULT_UI_FONT and self.code_font == DEFAULT_CODE_FONT:
            return
        self._apply_font(ui=DEFAULT_UI_FONT, code=DEFAULT_CODE_FONT, persist=True)

    def _apply_font(
        self,
        ui: str | None = None,
        code: str | None = None,
        *,
        persist: bool = False,
        rebuild_chat: bool = True,
        reopen_settings: bool = True,
    ) -> None:
        from project_agent.config import _font_family

        if ui is not None:
            self.ui_font = _font_family(ui, DEFAULT_UI_FONT)
        if code is not None:
            self.code_font = _font_family(code, DEFAULT_CODE_FONT)
        if self.ui_font_var.get() != self.ui_font:
            self.ui_font_var.set(self.ui_font)
        if self.code_font_var.get() != self.code_font:
            self.code_font_var.set(self.code_font)
        self.__dict__["_measure_fonts"] = {}
        self._copy_photo_cache = None
        if hasattr(self, "chat"):
            self.chat.configure(font=self._font(13))
            self._tag_chat()
        if hasattr(self, "task"):
            self.task.configure(font=self._font(13))
        if hasattr(self, "placeholder"):
            self.placeholder.configure(font=self._font(11))
        if hasattr(self, "editor_box"):
            self.editor_box.configure(font=self._editor_text_font())
            self._style_editor_gutter()
            self._update_editor_gutter()
        if hasattr(self, "file_util_panel"):
            self._style_file_util_panel()
        if hasattr(self, "output_box"):
            self.output_box.configure(font=self._code_font(12))
        if hasattr(self, "search_count"):
            self.search_count.configure(font=self._font(11))
        if hasattr(self, "status_label"):
            self.status_label.configure(font=self._font(11))
        if hasattr(self, "context_label"):
            self.context_label.configure(font=self._font(11))
        if self.allowed_box is not None:
            try:
                if self.allowed_box.winfo_exists():
                    self.allowed_box.configure(font=self._code_font(11))
            except tk.TclError:
                pass
        if self.mcp_list is not None:
            try:
                if self.mcp_list.winfo_exists():
                    self.mcp_list.configure(font=self._font(12))
            except tk.TclError:
                pass
        if hasattr(self, "tree"):
            self._style_tree()
            if self.project is not None:
                self._refresh_tree()
        if persist:
            try:
                if ui is not None:
                    save_font("ui_font", self.ui_font)
                if code is not None:
                    save_font("code_font", self.code_font)
            except Exception as exc:
                self.write_chat(f"Не удалось сохранить шрифт: {exc}")
        settings_open = self._window_alive(self._settings_window)
        if reopen_settings and settings_open:
            try:
                self._settings_window._close()
            except Exception:
                self._settings_window = None
            self.open_settings("Внешний вид")
        if rebuild_chat and hasattr(self, "transcript"):
            self._show_transcript()

    def _on_theme_pick(self, label: str) -> None:
        theme = THEME_LABELS.get(label, "dark")
        if theme == self.theme:
            return
        self._apply_theme(theme)
        try:
            save_theme(theme)
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить тему: {exc}")

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

    def _restore_project(self, raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            self._show_folder()
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            self.project = None
            self._show_folder()
            self.write_chat(f"Сохранённая папка недоступна: {text}")
            return
        self.project = path.resolve()
        self.agent.set_root(self.project)
        self._show_folder()
        self.after_idle(self._restore_last_chat)

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

    def choose_folder(self) -> None:
        if self.running:
            return
        selected = filedialog.askdirectory()
        if not selected:
            return
        if not self._release_editor(ask=True):
            return
        self._store_chat()
        self._remember_last_chat()
        self.chat_id = None
        self.chat_title = ""
        self.transcript.clear()
        self._forget_retry()
        self.project = Path(selected).resolve()
        self._show_folder()
        self.agent.set_root(self.project)
        self._clear_box(self.chat)
        try:
            build_index(self.project)
            self._refresh_index_label()
        except Exception as exc:
            self.write_chat(f"Индекс не построен: {exc}")
        self._persist("Папка выбрана")

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

    def _file_menu(self, event) -> str | None:
        row = self.tree.identify_row(event.y)
        if not row or self.project is None:
            return None
        tags = self.tree.item(row, "tags")
        if "file" not in tags and "dir" not in tags:
            return None
        self.tree.selection_set(row)
        relative = tree_path_rel(row)
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
        if "dir" in tags:
            target = "." if relative == "." else relative
            menu.add_command(label="Вложить папку в запрос", command=lambda: self._add_context_path(target + "/"))
        else:
            menu.add_command(label="Вложить в запрос", command=lambda path=relative: self._add_context_path(path))
            menu.add_command(label="Показать diff", command=lambda path=relative: self._show_file_git_diff(path))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_file_git_diff(self, relative: str) -> None:
        if self.project is None or not relative:
            return
        GitFileDiffDialog(self, relative)

    def add_mcp(self) -> None:
        name = self.mcp_name_var.get().strip()
        command_line = self.mcp_command_var.get().strip()
        if not name or not command_line:
            self.write_chat("Для MCP нужны имя и команда.")
            return
        try:
            parts = shlex.split(command_line, posix=False)
        except ValueError as exc:
            self.write_chat(f"Команда MCP не разобрана: {exc}")
            return
        parts = [part.strip('"') for part in parts if part]
        if not parts:
            return
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self.mcp_servers.append({"name": name, "command": parts[0], "args": parts[1:], "env": {}})
        self.mcp_name_var.set("")
        self.mcp_command_var.set("")
        self._render_mcp()

    def add_browser_mcp(self) -> None:
        from project_agent.browser_mcp import PLAYWRIGHT_SERVER

        name = PLAYWRIGHT_SERVER["name"]
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self.mcp_servers.append(
            {
                "name": name,
                "command": PLAYWRIGHT_SERVER["command"],
                "args": list(PLAYWRIGHT_SERVER["args"]),
                "env": {},
            }
        )
        self._render_mcp()
        self.write_chat(
            "Добавлен MCP playwright (npx -y @playwright/mcp@latest). Нужен Node.js. Сохраните настройки."
        )

    def remove_mcp(self) -> None:
        name = self.mcp_name_var.get().strip()
        if not name and self.mcp_servers:
            name = self.mcp_servers[-1]["name"]
        self.mcp_servers = [item for item in self.mcp_servers if item["name"] != name]
        self._render_mcp()

    def _servers_for_save(self) -> list[dict]:
        previous = {item["name"]: item for item in self.saved_servers}
        saved = []
        for item in self.mcp_servers:
            env = (previous.get(item["name"]) or {}).get("env") or item.get("env") or {}
            saved.append(
                {
                    "name": item["name"],
                    "command": item["command"],
                    "args": list(item.get("args") or []),
                    "env": dict(env),
                }
            )
        return saved

    def _render_mcp(self) -> None:
        box = self.mcp_list
        if box is None or not box.winfo_exists():
            return
        lines = []
        for item in self.mcp_servers:
            args = " ".join(item.get("args") or [])
            lines.append(f"{item['name']} — {item['command']} {args}".strip())
        self._set_box(box, "\n".join(lines))

    def _send_key(self, event=None):
        if event is not None and event.state & 0x0001:
            return None
        if self._at_popup_alive():
            self._at_accept()
            return "break"
        self.send()
        return "break"

    def send(self) -> None:
        if self.running:
            return
        self._hide_at_popup()
        text = self.task.get("1.0", "end").strip()
        images = list(self.attached)
        context_paths = merge_paths(
            self.context_files,
            self.context_dirs,
            parse_at_paths(text),
            limit=MAX_CONTEXT_FILES + MAX_CONTEXT_DIRS,
        )
        if self.project is None:
            self.write_chat("Сначала выберите папку проекта.")
            return
        settings = self.collect_settings()
        if not settings["model"]:
            self.write_chat("Укажите модель.")
            return
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        if not text and not images and not context_paths:
            return
        try:
            save_config(settings)
            self.saved_servers = settings["mcp_servers"]
        except Exception as exc:
            self.write_chat(f"Не удалось сохранить настройки: {exc}")
        prepared = []
        for path in images:
            try:
                prepared.append(prepare_image(Path(path)))
            except Exception as exc:
                self.write_chat(f"Изображение не прочитано: {exc}")
                return
        context_block = ""
        loaded_files: list[str] = []
        loaded_dirs: list[str] = []
        if context_paths:
            context_block, loaded_files, loaded_dirs, errors = load_explicit_context(
                self.project, context_paths, self.vault
            )
            for item in errors:
                self.write_chat(f"Вложение: {item}")
        model_text = compose_user_text(text, context_block)
        if not model_text and not prepared:
            return
        self.task.delete("1.0", "end")
        self.attached.clear()
        self.context_files.clear()
        self.context_dirs.clear()
        self._refresh_attach()
        note = text or (
            "(контекст)" if (loaded_files or loaded_dirs) else "(только изображение)"
        )
        if loaded_dirs:
            note += f"\n(папки: {', '.join(loaded_dirs)})"
        if loaded_files:
            note += f"\n(файлы: {', '.join(loaded_files)})"
        if images:
            note += f"\n(изображений: {len(images)})"
        created = self.chat_id is None
        if created:
            self.chat_id = new_chat_id()
            self.chat_title = " ".join(note.split())[:80] or "Чат"
            self.transcript.clear()
            self.checkpoints.clear()
            self._remember_last_chat()
        self.checkpoints.chat_id = self.chat_id or ""
        self.checkpoints.project_dir = str(self.project)
        # Своё сообщение всегда показываем: иначе после скролла вверх лента остаётся на старом месте.
        self._chat_stick = True
        self.write_chat(f"Вы: {note}")
        if created:
            self._refresh_chat_list()
            self._fit_labels()
        self._retry_payload = {"chat_id": self.chat_id, "text": model_text, "images": prepared}
        self._can_retry = False
        self._set_retry_enabled(False)
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Планирую следующие шаги…")
        thread = threading.Thread(target=self._turn, args=(model_text, prepared, settings, False), daemon=True)
        thread.start()

    def retry_last(self) -> None:
        payload = self._retry_payload
        if self.running or not self._can_retry or not payload:
            return
        if payload.get("chat_id") != self.chat_id or self.project is None:
            return
        settings = self.collect_settings()
        if not settings["model"]:
            self.write_chat("Укажите модель.")
            return
        if needs_api_key(settings["provider"], settings["base_url"], settings["api_key"]):
            self.write_chat("Укажите API-ключ.")
            return
        self._can_retry = False
        self._set_retry_enabled(False)
        # Продолжение хода тоже ведём к низу ленты.
        self._chat_stick = True
        self.running = True
        self.stop_event = threading.Event()
        self._show_running(True)
        self.set_status("Планирую следующие шаги…")
        thread = threading.Thread(
            target=self._turn,
            args=(payload["text"], payload["images"], settings, True),
            daemon=True,
        )
        thread.start()

    def _turn(self, text: str, images: list, settings: dict, resend: bool = False) -> None:
        self._can_retry = False
        try:
            self.agent.run_turn(text, images, settings, self.stop_event, resend=resend)
        except Exception as exc:
            self.write_retryable_error(f"Ошибка: {exc}")
        finally:
            self.after(0, self._finish)

    def stop(self) -> None:
        self.stop_event.set()
        self.agent.close()
        dialog = self._dialog
        if dialog is not None:
            self.after(0, dialog.refuse)
        self.set_status("Остановка")

    def _finish(self) -> None:
        self.running = False
        self._show_running(False)
        self._clear_running_tool()
        self._close_tool_group()
        self._collapse_tool_groups()
        if self.stop_event.is_set():
            if self._retry_payload:
                self._can_retry = True
                self._set_retry_enabled(True)
            else:
                self._can_retry = False
                self._set_retry_enabled(False)
            self.set_status("Остановлено")
        elif self._can_retry:
            self.set_status("Ошибка API")
            self._set_retry_enabled(True)
        else:
            self._set_retry_enabled(False)
            self.set_status("Готово")
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_chat_list()
        self._refresh_git_badge()

    def confirm(
        self,
        path: str,
        summary: str,
        detail: str = "",
        before: str | None = None,
        after: str | None = None,
    ) -> bool | str:
        if self.stop_event.is_set():
            return False
        done = threading.Event()
        holder: dict = {"result": False}

        def ask() -> None:
            try:
                dialog = ConfirmDialog(self, path, summary, detail, before=before, after=after)
                self._dialog = dialog
                self.wait_window(dialog)
                holder["result"] = dialog.result
            except Exception:
                holder["result"] = False
            finally:
                self._dialog = None
                done.set()

        try:
            self.after(0, ask)
        except Exception:
            return False
        while not done.wait(0.1):
            if self.stop_event.is_set() and self._dialog is not None:
                self.after(0, self._dialog.refuse)
        if self.stop_event.is_set():
            return False
        return holder["result"]

    def _on_checkpoint(self, checkpoint_id: str | None, file_count: int, dropped: list[str]) -> None:
        if dropped:
            self.after(0, lambda ids=list(dropped): self._drop_checkpoint_marks(ids))
        if not checkpoint_id:
            return
        mark = checkpoint_mark(checkpoint_id)
        self._remember(mark)
        count = max(0, int(file_count or 0))
        self.after(0, lambda cid=checkpoint_id, n=count: self._insert_checkpoint_control(cid, n))

    def _drop_checkpoint_marks(self, checkpoint_ids: list[str]) -> None:
        if not checkpoint_ids:
            return
        marks = {checkpoint_mark(item) for item in checkpoint_ids}
        before = len(self.transcript)
        self.transcript = [line for line in self.transcript if line.strip() not in marks]
        if len(self.transcript) != before:
            self._store_chat()
            self._show_transcript()

    def _insert_checkpoint_control(self, checkpoint_id: str, file_count: int = 0) -> None:
        box = getattr(self, "chat", None)
        if box is None or not box.winfo_exists():
            return
        if self.checkpoints.get(checkpoint_id) is None:
            return
        box.configure(state="normal")
        inner = box._textbox
        self._clear_running_tool()
        self._close_tool_group()
        self._embed_checkpoint_control(inner, checkpoint_id)
        self._chat_see_end()

    def _embed_checkpoint_control(self, inner, checkpoint_id: str) -> None:
        item = self.checkpoints.get(checkpoint_id)
        if item is None:
            return
        start = inner.index("end-1c")
        paths = restorable_paths(item)
        head = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
        button = tk.Button(
            head,
            text="Откатить правки",
            command=lambda cid=checkpoint_id: self.restore_checkpoint(cid),
            relief="flat",
            bd=0,
            padx=8,
            pady=2,
            cursor="hand2",
            bg=_tone(BUTTON),
            fg=_tone(TEXT),
            activebackground=_tone(BUTTON_HOVER),
            activeforeground=_tone(TEXT),
            disabledforeground=_tone(MUTED),
            font=self._px_font(12),
        )
        self._checkpoint_buttons.append(button)
        button.pack(side="left")
        if paths:
            hint = tk.Label(
                head,
                text=f"  {len(paths)}",
                bd=0,
                padx=0,
                pady=2,
                font=self._px_font(10),
                fg=_tone(MUTED),
                bg=_tone(INK),
            )
            hint.pack(side="left")
            diff_btn = tk.Button(
                head,
                text="Diff хода",
                command=lambda cid=checkpoint_id: self.open_turn_diff(cid),
                relief="flat",
                bd=0,
                padx=8,
                pady=2,
                cursor="hand2",
                bg=_tone(BUTTON),
                fg=_tone(TEXT),
                activebackground=_tone(BUTTON_HOVER),
                activeforeground=_tone(TEXT),
                disabledforeground=_tone(MUTED),
                font=self._px_font(11),
            )
            self._checkpoint_buttons.append(diff_btn)
            diff_btn.pack(side="left", padx=(8, 0))
        inner.window_create("end", window=head, padx=0, pady=2)
        inner.insert("end", "\n")
        for path in paths:
            row = tk.Frame(inner, bg=_tone(INK), highlightthickness=0)
            tk.Label(
                row,
                text=path,
                bd=0,
                padx=4,
                pady=1,
                font=self._px_font(11),
                fg=_tone(MUTED),
                bg=_tone(INK),
                anchor="w",
            ).pack(side="left")
            reject = tk.Button(
                row,
                text="Отклонить",
                command=lambda cid=checkpoint_id, rel=path: self.reject_checkpoint_file(cid, rel),
                relief="flat",
                bd=0,
                padx=6,
                pady=1,
                cursor="hand2",
                bg=_tone(BUTTON),
                fg=_tone(TEXT),
                activebackground=_tone(BUTTON_HOVER),
                activeforeground=_tone(TEXT),
                disabledforeground=_tone(MUTED),
                font=self._px_font(10),
            )
            self._checkpoint_buttons.append(reject)
            reject.pack(side="left", padx=(8, 0))
            inner.window_create("end", window=row, padx=8, pady=1)
            inner.insert("end", "\n")
        inner.tag_add("checkpoint", start, inner.index("end-1c"))

    def open_turn_diff(self, checkpoint_id: str) -> None:
        if self.project is None:
            return
        if self.checkpoints.get(checkpoint_id) is None:
            self.set_status("Снимок недоступен")
            return
        TurnDiffDialog(self, checkpoint_id)

    def reject_checkpoint_file(self, checkpoint_id: str, relative: str) -> None:
        if self.running or self.project is None:
            return
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None:
            self.set_status("Снимок недоступен")
            return
        ok, info = restore_one_file(self.project, checkpoint, relative)
        if not ok:
            self.set_status(info or "Не удалось отклонить файл")
            return
        still = self.checkpoints.drop_file(checkpoint_id, relative)
        if not still:
            mark = checkpoint_mark(checkpoint_id)
            self.transcript = [line for line in self.transcript if line.strip() != mark]
        self._show_transcript()
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_git_badge()
        if still:
            self.set_status(f"Отклонён: {relative}")
        else:
            self.set_status(f"Отклонён: {relative} (снимок закрыт)")

    def restore_checkpoint(self, checkpoint_id: str) -> None:
        if self.running or self.project is None:
            return
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None:
            self.set_status("Снимок недоступен")
            return
        restored, failed = restore_files(self.project, checkpoint)
        if not restored and failed:
            self.set_status("Не удалось откатить файлы")
            return
        cut = max(0, int(checkpoint.transcript_len))
        self.transcript = self.transcript[:cut]
        removed = self.checkpoints.drop_from(checkpoint_id)
        if removed:
            marks = {checkpoint_mark(item) for item in removed}
            self.transcript = [line for line in self.transcript if line.strip() not in marks]
        self.agent.truncate_messages(checkpoint.message_count)
        self._forget_retry()
        self._show_transcript()
        self._store_chat()
        self._refresh_tree()
        self._reload_clean_editor()
        self._refresh_git_badge()
        if failed:
            self.set_status(f"Правки откачены ({len(failed)} без снимка)")
        else:
            self.set_status("Правки откачены")

    def _remember(self, text: str) -> None:
        line = text.rstrip()
        if not line or not self.chat_id:
            return
        self.transcript.append(line)
        self._store_chat()

    def _store_chat(self) -> None:
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

    def _sync_checkpoint_marks(self) -> None:
        valid = {item.id for item in self.checkpoints.items}
        kept = []
        changed = False
        for line in self.transcript:
            checkpoint_id = parse_checkpoint_mark(line)
            if checkpoint_id is None:
                kept.append(line)
                continue
            item = self.checkpoints.get(checkpoint_id)
            if item is None or checkpoint_id not in valid:
                changed = True
                continue
            if item.transcript_len > len(kept):
                # маркер после обрезки истории — снимок уже не к месту
                self.checkpoints.drop_from(checkpoint_id)
                valid = {entry.id for entry in self.checkpoints.items}
                changed = True
                continue
            kept.append(line)
        if changed or kept != self.transcript:
            self.transcript = kept
            try:
                self.checkpoints.save()
            except Exception:
                pass

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

    def set_status(self, text: str) -> None:
        shown = text

        def apply() -> None:
            if self._closing:
                return
            try:
                if not self.winfo_exists() or not self.status_label.winfo_exists():
                    return
                self.status_label.configure(text=shown)
            except tk.TclError:
                return

        try:
            self.after(0, apply)
        except tk.TclError:
            return

    def _ui_error(self, exc, value, tb) -> None:
        if self._closing:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.write_chat(f"Ошибка интерфейса: {value}")

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self._release_editor(ask=True):
            return
        self._closing = True
        self._stop_project_watch()
        self.stop_event.set()
        try:
            self._remember_last_chat()
            self._store_chat()
            self.agent.close()
        finally:
            try:
                self.destroy()
            except tk.TclError:
                return


def main() -> None:
    multiprocessing.freeze_support()
    try:
        data, _error = load_config()
        ctk.set_appearance_mode(data["theme"])
        ctk.set_default_color_theme("dark-blue")
        app = App()
        app.mainloop()
    except Exception:
        _crash()


def _crash() -> None:
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "crash.log"
    log.write_text(traceback.format_exc(), encoding="utf-8")
    if __import__("os").name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "Ошибка запуска. Подробности в %APPDATA%\\ProjectAgent\\crash.log",
            "ProjectAgent",
            0x10,
        )
