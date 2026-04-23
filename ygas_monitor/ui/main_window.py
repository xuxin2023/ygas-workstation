"""Main application window with multi-session tabs."""

from __future__ import annotations

from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QTabWidget, QToolBar, QToolButton
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication

from .. import config as app_config
from ..services.settings_service import SettingsService
from .product_dialogs import AboutDialog, HelpDialog
from .session_widget import SessionWidget
from .styles import build_stylesheet
from .theme_tokens import THEME_LABELS, THEME_OPTIONS, THEME_SYSTEM, normalize_theme_choice

APP_NAME = getattr(app_config, "APP_NAME", "GasAxis Studio")
if APP_NAME == "YGAS Analyzer Monitor":
    APP_NAME = "GasAxis Studio"
APP_SUBTITLE_ZH = getattr(app_config, "APP_SUBTITLE_ZH", "气体分析仪监测与联调平台")


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        settings_service: SettingsService | None = None,
        settings_payload: dict | None = None,
    ) -> None:
        super().__init__()
        self.settings_service = settings_service or SettingsService()
        self.settings_payload = settings_payload or self.settings_service.load()
        self.theme_choice = normalize_theme_choice(self.settings_payload.get("ui", {}).get("theme", "dark"))
        self._session_count = 0

        self.setWindowTitle(f"{APP_NAME} - {APP_SUBTITLE_ZH}")
        self.resize(*self._initial_window_size())

        self._build_toolbar()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        app = QGuiApplication.instance()
        if app is not None:
            try:
                app.styleHints().colorSchemeChanged.connect(self._handle_system_color_scheme_changed)
            except Exception:
                pass

        self.add_session(initial_state=self.settings_payload.get("session", {}))
        self.apply_theme_choice(self.theme_choice, persist=False)

    def _initial_window_size(self) -> tuple[int, int]:
        requested_width = int(self.settings_payload["window"].get("width", 1600))
        requested_height = int(self.settings_payload["window"].get("height", 980))
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return requested_width, requested_height
        available = screen.availableGeometry()
        safe_width = max(640, available.width() - 24)
        safe_height = max(520, available.height() - 24)
        return min(requested_width, safe_width), min(requested_height, safe_height)

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("主工具栏")
        self.addToolBar(self.toolbar)

        add_action = QAction("新建会话", self)
        add_action.triggered.connect(self.add_session)
        self.toolbar.addAction(add_action)

        self.toolbar.addSeparator()
        self._build_theme_menu_button()
        self.toolbar.addWidget(self.theme_button)
        self.toolbar.addSeparator()

        help_action = QAction("软件说明", self)
        help_action.triggered.connect(self._show_help)
        self.toolbar.addAction(help_action)

        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        self.toolbar.addAction(about_action)

    def _build_theme_menu_button(self) -> None:
        self.theme_menu = QMenu(self)
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self._theme_actions: dict[str, QAction] = {}

        for theme_key in THEME_OPTIONS:
            action = QAction(THEME_LABELS[theme_key], self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked=False, choice=theme_key: self.apply_theme_choice(choice))
            self.theme_action_group.addAction(action)
            self.theme_menu.addAction(action)
            self._theme_actions[theme_key] = action

        self.theme_button = QToolButton(self)
        self.theme_button.setText("界面主题")
        self.theme_button.setPopupMode(QToolButton.InstantPopup)
        self.theme_button.setMenu(self.theme_menu)
        self._sync_theme_menu_state()

    def add_session(self, initial_state: dict | None = None) -> None:
        self._session_count += 1
        name = f"串口会话 {self._session_count}"
        widget = SessionWidget(name, initial_state=initial_state or {}, ui_theme=self.theme_choice)
        widget.theme_change_requested.connect(self.apply_theme_choice)
        self.tabs.addTab(widget, name)
        self.tabs.setCurrentWidget(widget)
        widget.set_theme_choice(self.theme_choice)
        widget.apply_theme(self.theme_choice)

    def apply_theme_choice(self, theme_choice: str, *, persist: bool = True) -> None:
        self.theme_choice = normalize_theme_choice(theme_choice)
        app = QApplication.instance()
        if app is not None:
            try:
                stylesheet = build_stylesheet(self.theme_choice)
            except TypeError:
                stylesheet = build_stylesheet()
            app.setStyleSheet(stylesheet)
        self._sync_theme_menu_state()

        self.settings_payload.setdefault("ui", {})["theme"] = self.theme_choice
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionWidget):
                widget.set_theme_choice(self.theme_choice)
                widget.apply_theme(self.theme_choice)

        if persist:
            self.settings_service.save(self._snapshot_settings_payload())

    def _sync_theme_menu_state(self) -> None:
        current_label = THEME_LABELS.get(self.theme_choice, THEME_LABELS["dark"])
        if hasattr(self, "theme_button"):
            self.theme_button.setToolTip(f"当前主题：{current_label}")
        for theme_key, action in getattr(self, "_theme_actions", {}).items():
            action.blockSignals(True)
            action.setChecked(theme_key == self.theme_choice)
            action.blockSignals(False)

    def _handle_system_color_scheme_changed(self, *_args) -> None:
        if self.theme_choice == THEME_SYSTEM:
            self.apply_theme_choice(THEME_SYSTEM, persist=False)

    def _show_help(self) -> None:
        HelpDialog(self).exec()

    def _show_about(self) -> None:
        AboutDialog(open_help_callback=self._show_help, parent=self).exec()

    def _snapshot_settings_payload(self) -> dict:
        current_session_state: dict = {}
        current_widget = self.tabs.currentWidget()
        if isinstance(current_widget, SessionWidget):
            current_session_state = current_widget.collect_persisted_state()

        return {
            "window": {
                "width": self.width(),
                "height": self.height(),
                "current_session_page": current_session_state.get("current_page_index", 0),
            },
            "ui": {
                "theme": self.theme_choice,
            },
            "session": current_session_state,
            "paths": {
                "export_dir": current_session_state.get("last_export_dir", ""),
                "last_replay_file": current_session_state.get("last_replay_file", ""),
            },
        }

    def _close_tab(self, index: int) -> None:
        if self.tabs.count() == 1:
            return
        widget = self.tabs.widget(index)
        if isinstance(widget, SessionWidget):
            widget.shutdown()
        self.tabs.removeTab(index)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.settings_service.save(self._snapshot_settings_payload())

        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionWidget):
                widget.shutdown()
        super().closeEvent(event)
