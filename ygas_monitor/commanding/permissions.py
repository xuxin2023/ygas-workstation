"""Permission helpers for safe device writes."""

from __future__ import annotations

LEVELS = {
    "READ_ONLY": 0,
    "CONFIG": 1,
    "CALIBRATION": 2,
    "EXPERT": 3,
}

LABELS_ZH = {
    "READ_ONLY": "只读模式",
    "CONFIG": "配置模式",
    "CALIBRATION": "校准模式",
    "EXPERT": "专家模式",
}


def permission_label(level: str) -> str:
    return LABELS_ZH.get(str(level).upper(), str(level))


def has_permission(current_level: str, required_level: str) -> bool:
    current = LEVELS.get(str(current_level).upper(), 0)
    required = LEVELS.get(str(required_level).upper(), 0)
    return current >= required
