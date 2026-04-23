"""Dataclasses shared across protocol, service, and UI layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


ACTION_LABELS_ZH = {
    "command": "命令执行",
    "auto_silence": "临时暂停主动上传",
    "auto_silence_restore": "恢复主动上传",
    "keep_auto_upload_off": "保持主动上传关闭",
    "auto_start_stream": "连接后自动启动实时流",
    "manual_stream_start": "手动启动实时流",
}

AUTO_UPLOAD_STATE_LABELS_ZH = {
    "on": "已开启",
    "off": "已关闭",
    "unknown": "未确认",
    "temporarily_silenced": "主动上传已临时暂停",
    "restore_failed": "恢复失败",
}

RESTORE_POLICY_LABELS_ZH = {
    "restore_after_read": "读取完成后恢复主动上传",
    "keep_off": "读取完成后保持主动上传关闭",
    "preserve_existing_silence": "读取完成后保持主动上传已临时暂停",
    "user_confirmed_restore": "读取完成后恢复主动上传（用户确认）",
    "user_declined_restore": "读取完成后不自动恢复主动上传（用户确认）",
    "unknown_no_restore": "主动上传状态未确认，读取完成后不自动恢复",
}

RESTORE_RESULT_LABELS_ZH = {
    "pending_restore": "待恢复",
    "restored": "已恢复",
    "restore_failed": "恢复失败",
    "kept_off": "保持关闭",
    "preserved_existing_silence": "保持主动上传已临时暂停",
    "left_unknown": "保持未确认",
    "not_attempted": "未尝试恢复",
}


def action_label_zh(action_type: str) -> str:
    """Return a user-facing Chinese label for structured action types."""
    normalized = str(action_type or "").strip()
    if not normalized:
        return ""
    return ACTION_LABELS_ZH.get(normalized, normalized)


def auto_upload_state_label_zh(value: str) -> str:
    """Return a user-facing Chinese label for active-upload state values."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return AUTO_UPLOAD_STATE_LABELS_ZH.get(normalized, normalized)


def restore_policy_label_zh(value: str) -> str:
    """Return a user-facing Chinese label for quiet-read restore policies."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return RESTORE_POLICY_LABELS_ZH.get(normalized, normalized)


def restore_result_label_zh(value: str) -> str:
    """Return a user-facing Chinese label for quiet-read restore outcomes."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return RESTORE_RESULT_LABELS_ZH.get(normalized, normalized)


def auto_upload_detail_lines(
    *,
    original_auto_upload_state: str = "",
    restore_policy: str = "",
    restore_attempted: bool | None = None,
    restore_result: str = "",
) -> list[str]:
    """Build Chinese business-detail lines for active-upload pause/restore metadata."""
    lines: list[str] = []
    if original_auto_upload_state:
        lines.append(f"原始主动上传状态：{auto_upload_state_label_zh(original_auto_upload_state)}")
    if restore_policy:
        lines.append(f"读取后恢复策略：{restore_policy_label_zh(restore_policy)}")
    if restore_attempted is not None:
        lines.append(f"是否尝试恢复：{'是' if restore_attempted else '否'}")
    if restore_result:
        lines.append(f"恢复结果：{restore_result_label_zh(restore_result)}")
    return lines


def enrich_auto_upload_detail_text(
    base_text: str,
    *,
    original_auto_upload_state: str = "",
    restore_policy: str = "",
    restore_attempted: bool | None = None,
    restore_result: str = "",
) -> str:
    """Append Chinese business-detail lines without duplicating existing text."""
    normalized_base = str(base_text or "").strip()
    parts = [normalized_base] if normalized_base else []
    for line in auto_upload_detail_lines(
        original_auto_upload_state=original_auto_upload_state,
        restore_policy=restore_policy,
        restore_attempted=restore_attempted,
        restore_result=restore_result,
    ):
        if line and line not in normalized_base:
            parts.append(line)
    return "；".join(part for part in parts if part) or "--"


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
    listen_only: bool = False
    session_mode: str = "SAFE_HANDSHAKE"
    device_ftd_hz: int = 10
    expected_receive_hz: int = 10
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
    response_kind: str = ""
    response_device_id: str | None = None
    parsed_payload: dict[str, Any] = field(default_factory=dict)
    matched_response_line: str = ""
    timeout_reason: str = ""
    observed_telemetry_count: int = 0
    observed_other_response_count: int = 0
    action_type: str = ""
    action_label_zh: str = ""
    parent_command: str = ""
    command_target_id: str = ""
    expected_device_id: str = ""
    effective_scope: str = ""
    source_page: str = ""
    auto_upload_state: str = ""
    original_auto_upload_state: str = ""
    restore_policy: str = ""
    restore_attempted: bool = False
    restore_result: str = ""


@dataclass(slots=True)
class StructuredValueSnapshot:
    summary: str = "--"
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class WriteVerificationReport:
    before: StructuredValueSnapshot = field(default_factory=StructuredValueSnapshot)
    target: StructuredValueSnapshot = field(default_factory=StructuredValueSnapshot)
    after: StructuredValueSnapshot = field(default_factory=StructuredValueSnapshot)
    result_text: str = "--"
    detail_text: str = "--"
    verification_status: str = ""
    verified_at: datetime | None = None
    source_device_id: str | None = None


@dataclass(slots=True)
class SessionChangeEntry:
    timestamp: datetime
    command_name: str
    target_device_id: str
    source_page: str = ""
    before_value: str = "--"
    target_value: str = "--"
    after_value: str = "--"
    result_text: str = "--"
    detail_text: str = "--"
    verification_status: str = ""
    action_type: str = "command"
    action_label_zh: str = ""
    command_payload: str = ""
    parent_command: str = ""
    command_target_id: str = ""
    expected_device_id: str = ""
    response_device_id: str = ""
    effective_scope: str = ""
    is_system_action: bool = False
    original_auto_upload_state: str = ""
    restore_policy: str = ""
    restore_attempted: bool = False
    restore_result: str = ""


@dataclass(slots=True)
class SessionWriteStatus:
    online_device_text: str = "--"
    session_target_text: str = "--"
    effective_send_text: str = "--"
    write_allowed: bool = False
    reason_text: str = "--"
    control_write_text: str = "禁止"
    control_reason_text: str = "--"
    stream_start_allowed: bool = False
    stream_status_text: str = "--"
    stream_reason_text: str = "--"
    stream_status_code: str = "blocked"


@dataclass(slots=True)
class DefaultMonitoringChecklistStep:
    # Historical naming is retained for compatibility; the same structure now serves multiple checklist_kind values.
    step_index: int
    step_key: str
    title: str
    target_summary: str
    business_text: str = ""
    planned_payload: str = ""
    flow_label: str = ""
    source_page: str = ""
    checklist_kind: str = "default_monitoring"
    step_id: str = ""
    expected_device: str = ""
    planned_target_id: str = ""
    target_is_broadcast: bool = False
    effective_scope: str = ""
    verification_policy: str = ""
    risk_text: str = ""
    status: str = "待确认"
    linked_command_id: str = ""
    prefill_values: dict[str, str] = field(default_factory=dict)
    last_message: str = ""
    last_evidence_text: str = ""
    last_timeout_reason: str = ""
    pending_payload: str = ""
    run_id: int = 0
    pending_run_id: int = 0


@dataclass(slots=True)
class DefaultMonitoringAckPendingWrite:
    # Historical naming is retained for compatibility; the same structure now serves multiple checklist_kind values.
    run_id: int
    command_id: str
    step_key: str
    step_title: str
    flow_label: str
    target_summary: str
    target_device_id: str
    source_page: str
    payload: str
    sent_at: datetime
    checklist_kind: str = "default_monitoring"
    step_id: str = ""
    planned_payload: str = ""
    expected_device: str = ""
    command_target_id: str = ""
    expected_device_id: str = ""
    effective_scope: str = ""
    verification_policy: str = ""
    from_default_monitoring_checklist: bool = True
    target_is_broadcast: bool = False


@dataclass(slots=True)
class SessionCommandLogEntry:
    timestamp: datetime
    payload: str
    result: str
    action_type: str = "command"
    action_label_zh: str = ""
    parent_command: str = ""
    command_target_id: str = ""
    expected_device_id: str = ""
    response_device_id: str = ""
    effective_scope: str = ""
    source_page: str = ""
    detail_text: str = ""
    level: str = "INFO"
    original_auto_upload_state: str = ""
    restore_policy: str = ""
    restore_attempted: bool = False
    restore_result: str = ""


# Backward-compatible aliases for the newer, checklist-generic naming used in docs and tests.
ChecklistStep = DefaultMonitoringChecklistStep
ChecklistAckPendingWrite = DefaultMonitoringAckPendingWrite
