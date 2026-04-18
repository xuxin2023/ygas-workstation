"""Replay CSV validation and conversion helpers."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import ParsedFrame

REQUIRED_REPLAY_COLUMNS = {"timestamp", "mode", "raw"}
META_COLUMNS = {"timestamp", "device_id", "mode", "status", "raw", "extras"}


@dataclass(slots=True)
class ReplayDataset:
    headers: list[str]
    rows: list[dict[str, str]]
    frames: list[ParsedFrame]

    @property
    def total_duration_s(self) -> float:
        if len(self.frames) < 2:
            return 0.0
        delta = self.frames[-1].timestamp - self.frames[0].timestamp
        return max(0.0, delta.total_seconds())


def validate_replay_headers(headers: list[str] | None) -> tuple[bool, list[str]]:
    existing = {str(item).strip() for item in (headers or []) if str(item).strip()}
    missing = sorted(REQUIRED_REPLAY_COLUMNS - existing)
    return (not missing, missing)


def load_replay_dataset(path: str | Path) -> ReplayDataset:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        headers = list(reader.fieldnames or [])

    ok, missing = validate_replay_headers(headers)
    if not ok:
        raise ValueError(f"回放文件缺少关键列: {', '.join(missing)}")

    frames = [row_to_frame(row) for row in rows]
    frames.sort(key=lambda item: item.timestamp)
    return ReplayDataset(headers=headers, rows=rows, frames=frames)


def row_to_frame(row: dict[str, str]) -> ParsedFrame:
    timestamp_text = str(row.get("timestamp", "")).strip()
    try:
        timestamp = datetime.fromisoformat(timestamp_text)
    except Exception:
        raise ValueError(f"无效时间戳: {timestamp_text}") from None

    mode_text = str(row.get("mode", "")).strip()
    try:
        mode = int(float(mode_text))
    except Exception:
        raise ValueError(f"无效模式值: {mode_text}") from None

    raw = str(row.get("raw", "")).strip()
    if not raw:
        raise ValueError("回放文件缺少 raw 原始帧内容")

    fields: dict[str, Any] = {}
    for key, value in row.items():
        if key in META_COLUMNS:
            continue
        text = str(value).strip()
        if text == "":
            continue
        fields[key] = _maybe_number(text)

    extras = [item for item in str(row.get("extras", "")).split("|") if item]
    return ParsedFrame(
        timestamp=timestamp,
        raw=raw,
        device_id=(str(row.get("device_id", "")).strip() or None),
        mode=mode,
        fields=fields,
        status=(str(row.get("status", "")).strip() or None),
        extras=extras,
    )


def _maybe_number(text: str) -> object:
    try:
        value = float(text)
    except Exception:
        return text
    if value.is_integer():
        return int(value)
    return value

