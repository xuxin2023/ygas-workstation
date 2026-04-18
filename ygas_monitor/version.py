"""Application version and environment helpers."""

from __future__ import annotations

from datetime import datetime
import platform
import sys
from typing import Any

from .config import APP_INTERNAL_NAME, APP_NAME, LOG_DIR, SETTINGS_DIR

APP_VERSION = "0.9.0-rc1"
APP_DESCRIPTION = "Professional YGAS analyzer workstation for monitoring, diagnostics, control, replay, and export."
BUILD_DATE = datetime.now().strftime("%Y-%m-%d")


def get_version_info() -> dict[str, Any]:
    return {
        "app_internal_name": APP_INTERNAL_NAME,
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "description": APP_DESCRIPTION,
        "build_date": BUILD_DATE,
        "settings_dir": str(SETTINGS_DIR),
        "log_dir": str(LOG_DIR),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "windows_version": platform.version(),
    }


def environment_summary_text() -> str:
    info = get_version_info()
    return (
        f"{info['app_name']} {info['version']}\n"
        f"Build Date: {info['build_date']}\n"
        f"Python: {info['python']}\n"
        f"Platform: {info['platform']}\n"
        f"Windows Version: {info['windows_version']}\n"
        f"Settings Dir: {info['settings_dir']}\n"
        f"Log Dir: {info['log_dir']}"
    )
