"""Application-wide defaults and runtime filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path

APP_INTERNAL_NAME = "YGasWorkstation"
APP_NAME = "YGAS Analyzer Monitor"
APP_ORG = "OpenAI"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = PROJECT_ROOT / "assets"
APP_ICON_PATH = ASSET_DIR / "app.ico"

OLD_DATA_DIR = PROJECT_ROOT / "data"
OLD_LOG_DIR = PROJECT_ROOT / "logs"
OLD_EXPORT_DIR = PROJECT_ROOT / "exports"
OLD_SETTINGS_PATH = OLD_DATA_DIR / "user_settings.json"
OLD_COMMAND_TEMPLATE_PATH = OLD_DATA_DIR / "command_templates.json"

USER_HOME = Path.home()
APPDATA_DIR = Path(os.environ.get("APPDATA", USER_HOME / "AppData" / "Roaming"))
LOCALAPPDATA_DIR = Path(os.environ.get("LOCALAPPDATA", USER_HOME / "AppData" / "Local"))

SETTINGS_DIR = APPDATA_DIR / APP_INTERNAL_NAME
LOCAL_STATE_DIR = LOCALAPPDATA_DIR / APP_INTERNAL_NAME
DATA_DIR = SETTINGS_DIR
LOG_DIR = LOCAL_STATE_DIR / "logs"
EXPORT_DIR = LOCAL_STATE_DIR / "exports"
CACHE_DIR = LOCAL_STATE_DIR / "cache"
REPLAY_CACHE_DIR = CACHE_DIR / "replay"
COMMAND_TEMPLATE_PATH = DATA_DIR / "command_templates.json"
SETTINGS_PATH = DATA_DIR / "user_settings.json"

DEFAULT_STREAM_HZ = 10
DEFAULT_POLL_INTERVAL_MS = 200
DEFAULT_HISTORY_SIZE = 2000
DEFAULT_CHART_POINTS = 600
DEFAULT_COMMAND_TIMEOUT_MS = 2000
LOG_RETENTION_FILES = 30
LOG_RETENTION_BYTES = 64 * 1024 * 1024


def ensure_runtime_dirs() -> None:
    for path in (SETTINGS_DIR, LOCAL_STATE_DIR, LOG_DIR, EXPORT_DIR, CACHE_DIR, REPLAY_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
