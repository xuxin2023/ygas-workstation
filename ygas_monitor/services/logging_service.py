"""Rotating file logger for raw serial IO and session events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading

from ..config import LOG_DIR, LOG_RETENTION_BYTES, LOG_RETENTION_FILES, ensure_runtime_dirs
from ..models import RawFrameRecord


class SessionLogger:
    def __init__(self, session_name: str):
        ensure_runtime_dirs()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._prune_old_logs()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = session_name.replace(" ", "_")
        self.path = LOG_DIR / f"{safe_name}_{stamp}.log"
        self._lock = threading.Lock()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("# timestamp\tdirection\tlevel\ttext\n")

    def log_record(self, record: RawFrameRecord) -> None:
        self.log(record.timestamp.isoformat(timespec="milliseconds"), record.direction, record.level, record.text)

    def log(self, timestamp: str, direction: str, level: str, text: str) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                clean = str(text or "").replace("\n", "\\n").replace("\r", "\\r")
                handle.write(f"{timestamp}\t{direction}\t{level}\t{clean}\n")

    @staticmethod
    def _prune_old_logs() -> None:
        files = sorted(LOG_DIR.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
        total_bytes = 0
        for index, path in enumerate(files):
            try:
                size = path.stat().st_size
            except Exception:
                continue
            total_bytes += size
            if index >= LOG_RETENTION_FILES or total_bytes > LOG_RETENTION_BYTES:
                try:
                    path.unlink()
                except Exception:
                    pass
