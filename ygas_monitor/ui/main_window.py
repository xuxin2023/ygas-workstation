"""Main application window with multi-session tabs."""

from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget, QToolBar

from ..config import LOG_DIR, SETTINGS_DIR
from ..services.settings_service import SettingsService
from ..version import APP_DESCRIPTION, APP_NAME, APP_VERSION
from .session_widget import SessionWidget


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings_service = SettingsService()
        self.settings_payload = self.settings_service.load()

        self.setWindowTitle("专业气体分析仪工作站")
        self.resize(
            int(self.settings_payload["window"].get("width", 1600)),
            int(self.settings_payload["window"].get("height", 980)),
        )
        self._session_count = 0

        toolbar = QToolBar("主工具栏")
        self.addToolBar(toolbar)

        add_action = QAction("新建会话", self)
        add_action.triggered.connect(self.add_session)
        toolbar.addAction(add_action)

        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        toolbar.addAction(about_action)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        self.add_session(initial_state=self.settings_payload.get("session", {}))

    def add_session(self, initial_state: dict | None = None) -> None:
        self._session_count += 1
        name = f"串口会话 {self._session_count}"
        widget = SessionWidget(name, initial_state=initial_state or {})
        self.tabs.addTab(widget, name)
        self.tabs.setCurrentWidget(widget)

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            (
                f"{APP_NAME}\n"
                f"版本: {APP_VERSION}\n\n"
                f"{APP_DESCRIPTION}\n\n"
                "协议支持概览:\n"
                "- MODE1 / MODE2 / AUTO\n"
                "- 监听流 / 被动轮询\n"
                "- 查询命令 / 控制命令 / 系数命令\n"
                "- 地址系统 / FFF 风险控制 / 回放 / 导出\n\n"
                f"配置目录:\n{SETTINGS_DIR}\n\n"
                f"日志目录:\n{LOG_DIR}"
            ),
        )

    def _close_tab(self, index: int) -> None:
        if self.tabs.count() == 1:
            return
        widget = self.tabs.widget(index)
        if isinstance(widget, SessionWidget):
            widget.shutdown()
        self.tabs.removeTab(index)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        current_session_state: dict = {}
        current_widget = self.tabs.currentWidget()
        if isinstance(current_widget, SessionWidget):
            current_session_state = current_widget.collect_persisted_state()

        self.settings_service.save(
            {
                "window": {
                    "width": self.width(),
                    "height": self.height(),
                    "current_session_page": current_session_state.get("current_page_index", 0),
                },
                "session": current_session_state,
                "paths": {
                    "export_dir": current_session_state.get("last_export_dir", ""),
                    "last_replay_file": current_session_state.get("last_replay_file", ""),
                },
            }
        )

        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, SessionWidget):
                widget.shutdown()
        super().closeEvent(event)
