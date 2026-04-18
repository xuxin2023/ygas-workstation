"""JSON-backed user preference persistence with safe defaults and migration."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
from typing import Any

from ..commanding.safety import SESSION_MODE_LISTEN_ONLY
from ..config import (
    DATA_DIR,
    EXPORT_DIR,
    OLD_SETTINGS_PATH,
    SETTINGS_PATH,
    ensure_runtime_dirs,
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "window": {
        "width": 1600,
        "height": 980,
        "current_session_page": 0,
    },
    "session": {
        "port": "SIMULATOR",
        "baudrate": "115200",
        "bytesize": "8",
        "parity": "N",
        "stopbits": "1",
        "acquisition_mode": "LISTEN",
        "mode_preference": "AUTO",
        "session_mode": SESSION_MODE_LISTEN_ONLY,
        "command_timeout_ms": "2000",
        "auto_reconnect": False,
        "profile_name": "bench_default",
        "stream_hz": "10",
        "poll_interval_ms": "200",
        "permission_level": "READ_ONLY",
        "listen_only": True,
        "read_only_lock": False,
        "show_expert_terminal": False,
        "target_id": "001",
        "session_note": "",
        "current_page_index": 0,
    },
    "paths": {
        "export_dir": str(EXPORT_DIR),
        "last_replay_file": "",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class SettingsService:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or SETTINGS_PATH)

    def load(self) -> dict[str, Any]:
        ensure_runtime_dirs()
        self._migrate_legacy_settings()
        if not self.path.exists():
            return deepcopy(DEFAULT_SETTINGS)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return deepcopy(DEFAULT_SETTINGS)
        if not isinstance(payload, dict):
            return deepcopy(DEFAULT_SETTINGS)
        return _deep_merge(DEFAULT_SETTINGS, payload)

    def save(self, payload: dict[str, Any]) -> None:
        ensure_runtime_dirs()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        merged = _deep_merge(DEFAULT_SETTINGS, payload)
        self.path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _migrate_legacy_settings(self) -> None:
        if self.path.exists() or not OLD_SETTINGS_PATH.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(OLD_SETTINGS_PATH, self.path)
        except Exception:
            # Keep fallback logic simple and safe: default settings will still load.
            pass
