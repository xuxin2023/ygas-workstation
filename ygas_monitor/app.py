"""Application bootstrap."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .config import APP_ICON_PATH, APP_NAME, APP_ORG, ensure_runtime_dirs
from .services.settings_service import SettingsService
from .ui.main_window import MainWindow
from .ui.styles import build_stylesheet
from .version import APP_VERSION


def main() -> int:
    ensure_runtime_dirs()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationVersion(APP_VERSION)
    try:
        app.setApplicationDisplayName(APP_NAME)
    except Exception:
        pass
    app.setStyle("Fusion")
    settings_service = SettingsService()
    settings_payload = settings_service.load()
    app.setStyleSheet(build_stylesheet(settings_payload.get("ui", {}).get("theme", "dark")))
    window = MainWindow(settings_service=settings_service, settings_payload=settings_payload)
    if APP_ICON_PATH.exists():
        from PySide6.QtGui import QIcon

        icon = QIcon(str(APP_ICON_PATH))
        app.setWindowIcon(icon)
        window.setWindowIcon(icon)
    window.show()
    return app.exec()
