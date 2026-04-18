"""Dataclasses shared across protocol, service, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SerialSettings:
    port: str = "SIMULATOR"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    timeout: float = 0.05


@dataclass(slots=True)
class SessionConfig:
    serial: SerialSettings = field(default_factory=SerialSettings)
    mode_preference: str = "AUTO"
    acquisition_mode: str = "LISTEN"
    listen_only: bool = True
    session_mode: str = "LISTEN_ONLY"
    stream_hz: int = 10
    poll_interval_ms: int = 200
    command_timeout_ms: int = 2000
    auto_reconnect: bool = False
    read_only_lock: bool = False
    session_note: str = ""
    profile_name: str = "bench_default"
    target_id: str = "001"
    permission_level: str = "READ_ONLY"
    session_name: str = "session"


@dataclass(slots=True)
class StatusBitState:
    bit: int
    label: str
    active: bool
    is_alarm: bool
    state_text: str
    description: str


@dataclass(slots=True)
class ParsedFrame:
    timestamp: datetime
    raw: str
    device_id: str | None
    mode: int
    fields: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    extras: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "timestamp": self.timestamp.isoformat(timespec="milliseconds"),
            "device_id": self.device_id,
            "mode": self.mode,
            "status": self.status,
            "raw": self.raw,
            "extras": "|".join(self.extras),
        }
        row.update(self.fields)
        return row


@dataclass(slots=True)
class RawFrameRecord:
    timestamp: datetime
    direction: str
    text: str
    level: str = "INFO"


@dataclass(slots=True)
class AlarmEvent:
    timestamp: datetime
    message: str
    severity: str = "WARN"


@dataclass(slots=True)
class CommandResult:
    timestamp: datetime
    command: str
    ok: bool
    message: str
    response_lines: list[str] = field(default_factory=list)
