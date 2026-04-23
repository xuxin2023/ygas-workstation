"""Qt-threaded acquisition service and UI-facing session controller."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..commanding.permissions import has_permission, permission_label
from ..commanding.safety import SESSION_MODE_ENGINEERING, SESSION_MODE_LISTEN_ONLY, SESSION_MODE_REPLAY
from ..config import DEFAULT_HISTORY_SIZE
from ..models import AlarmEvent, CommandResult, ParsedFrame, RawFrameRecord, SessionConfig, action_label_zh
from ..protocols.senco_format import normalize_senco_coefficient
from ..protocols.ygas import CommandEnvelope, PARSE_MODE_AUTO, StreamBuffer, YGasProtocol
from ..serial.transport import AbstractTransport, create_transport
from .auto_silence_policy import AutoSilencePolicy, AutoSilenceTargetResolutionError
from .export_service import export_frames_to_csv
from .logging_service import SessionLogger
from .metrics import MetricsTracker, MonitoringMetrics

ACTIVE_RX_WINDOW_S = 1.5
SILENCE_WINDOW_S = 0.35
WRITE_OBJECT_GUARD_CODES = {
    "MODE",
    "SETCOMWAY",
    "FTD",
    "SETPOW",
    "SETILLUM",
    "SETCO2",
    "TIMEOUT",
    "ID",
    "SENTEMP1",
    "SENTEMP2",
    "AVERAGE1",
    "AVERAGE2",
}
AUTO_UPLOAD_PAUSE_SOURCE_PAGE = "主动上传暂停保护"
AUTO_UPLOAD_RESTORE_SOURCE_PAGE = "恢复主动上传"
AUTO_START_STREAM_SOURCE_PAGE = "连接后自动启动实时流"
MANUAL_STREAM_START_SOURCE_PAGE = "监测页启动实时流"


@dataclass(slots=True)
class _PendingCommand:
    envelope: CommandEnvelope
    expectation: str
    new_target_id: str | None = None
    expected_response_device_id: str = ""
    operation_context: dict[str, Any] = field(default_factory=dict)

    @property
    def code(self) -> str:
        return self.envelope.code

    @property
    def target_id(self) -> str:
        return self.envelope.target_id

    @property
    def args(self) -> list[str]:
        return self.envelope.args

    @property
    def is_senco_write(self) -> bool:
        return self.code.startswith(("SENCO", "CLEARSENCO"))

    @property
    def requires_pre_silence_for_write(self) -> bool:
        return AutoSilencePolicy.requires_pre_silence_for_write(self.code, self.operation_context, self.envelope)

    @property
    def requires_quiet_window_for_read(self) -> bool:
        return AutoSilencePolicy.requires_quiet_window_for_read(self.code, self.operation_context, self.envelope)

    @property
    def is_silence_command(self) -> bool:
        return AutoSilencePolicy.is_silence_command(self.code, self.operation_context, self.envelope)

    @property
    def is_getco(self) -> bool:
        return self.code == "GETCO"

    @property
    def allows_broadcast_ack(self) -> bool:
        return self.target_id == "FFF" and self.expectation == "ack"

    @property
    def effective_scope(self) -> str:
        return AutoSilencePolicy.effective_scope(self.target_id)

    @property
    def requires_object_consistency(self) -> bool:
        if self.code.startswith(("SENCO", "CLEARSENCO")):
            return True
        return self.code in WRITE_OBJECT_GUARD_CODES and bool(self.args)


@dataclass(slots=True)
class _ParsedResponse:
    ok: bool
    message: str
    kind: str = ""
    device_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class AcquisitionWorker(QObject):
    frame_received = Signal(object)
    raw_received = Signal(object)
    alarm_emitted = Signal(object)
    metrics_updated = Signal(object)
    device_ids_updated = Signal(object)
    rx_device_state_changed = Signal(object)
    target_sync_requested = Signal(str, str)
    command_completed = Signal(object)
    connection_changed = Signal(bool, str)
    error = Signal(str)
    info = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._timer: QTimer | None = None
        self._transport: AbstractTransport | None = None
        self._config = SessionConfig()
        self._partial = ""
        self._stream_buffer = StreamBuffer()
        self._logger: SessionLogger | None = None
        self._known_ids: set[str] = set()
        self._active_rx_entries: deque[tuple[float, str]] = deque()
        self._active_rx_cache: tuple[str, ...] = ()
        self._latest_rx_device_id: str | None = None
        self._last_alarm_bits: set[int] = set()
        self._last_frame: ParsedFrame | None = None
        self._last_stream_frame_monotonic = 0.0
        self._metrics = MetricsTracker()
        self._last_metrics_emit_ts = 0.0
        self._next_poll_ts = 0.0
        self._pending_poll_deadline = 0.0
        self._pending_id_target: str | None = None

    @Slot(object)
    def open_session(self, config: SessionConfig) -> None:
        self.close_session()
        self._config = config
        self._metrics.update_expected_hz(max(1, int(config.expected_receive_hz or config.stream_hz or 1)))
        self._logger = SessionLogger(config.session_name)
        try:
            self._transport = create_transport(config.serial)
            self._transport.open()
            self._known_ids.clear()
            self._active_rx_entries.clear()
            self._active_rx_cache = ()
            self._latest_rx_device_id = None
            self._last_alarm_bits.clear()
            self._last_frame = None
            self._last_stream_frame_monotonic = 0.0
            self._partial = ""
            self._stream_buffer.clear()
            self._pending_id_target = None
            self._next_poll_ts = time.monotonic()
            self._pending_poll_deadline = 0.0
            self._emit_rx_device_state()
            self._ensure_timer()
            self.connection_changed.emit(True, str(self._logger.path))
            self._emit_info(f"已连接 {config.serial.port}，当前目标设备 ID: {config.target_id}。")
        except Exception as exc:
            self._transport = None
            self.connection_changed.emit(False, "")
            self.error.emit(f"连接失败: {exc}")

    @Slot()
    def close_session(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self.connection_changed.emit(False, str(self._logger.path) if self._logger is not None else "")

    @Slot(object)
    def update_session_config(self, config: SessionConfig) -> None:
        self._config = config
        self._metrics.update_expected_hz(max(1, int(config.expected_receive_hz or config.stream_hz or 1)))
        self._emit_info(
            f"会话配置已更新，目标设备 ID: {config.target_id}，解析模式 {config.mode_preference}，"
            f"会话模式: {config.session_mode}。"
        )

    @Slot()
    def initialize_capture(self) -> None:
        self.command_completed.emit(
            CommandResult(
                timestamp=datetime.now(),
                command="INIT",
                ok=False,
                message=(
                    "初始化采集入口已改为待确认清单；当前不会直接发送 MODE / FTD / SETCOMWAY。"
                    "请在界面中准备初始化采集清单后逐项确认。"
                ),
            )
        )

    @Slot(str, str, int, object)
    def send_payload(self, payload: str, expectation: str, timeout_ms: int, context: object = None) -> None:
        result = self._send_payload(payload, expectation=expectation, timeout_ms=timeout_ms, context=context)
        self.command_completed.emit(result)

    def _send_payload(self, payload: str, *, expectation: str, timeout_ms: int, context: object = None) -> CommandResult:
        if not self._transport or not self._transport.is_open:
            return self._command_result(
                payload,
                ok=False,
                message="串口未连接。",
            )

        envelope = YGasProtocol.parse_command(payload)
        if envelope is None:
            return self._command_result(payload, ok=False, message="原始命令格式无法识别。")

        pending = _PendingCommand(
            envelope=envelope,
            expectation=expectation,
            new_target_id=envelope.args[0] if envelope.code == "ID" and envelope.args else None,
            expected_response_device_id=self._snapshot_expected_response_device_id(envelope),
            operation_context=dict(context) if isinstance(context, dict) else {},
        )
        if pending.new_target_id:
            self._pending_id_target = pending.new_target_id

        mismatch = self._preflight_target_mismatch(pending)
        if mismatch:
            if pending.new_target_id:
                self._pending_id_target = None
            return self._command_result(payload, ok=False, message=mismatch, pending=pending)

        if pending.is_silence_command:
            result = self._write_and_wait(payload, pending, timeout_ms=timeout_ms)
            if not result.ok:
                if "超时" in result.message:
                    result.message = "未收到 SETCOMWAY ACK。"
                if pending.new_target_id:
                    self._pending_id_target = None
                return result
            silence_error = self._observe_silence_failure(initial_lines=result.response_lines)
            if silence_error:
                if pending.new_target_id:
                    self._pending_id_target = None
                return self._command_result(
                    payload,
                    ok=False,
                    message=silence_error,
                    pending=pending,
                    response_lines=result.response_lines,
                    matched_response_line=result.matched_response_line,
                    observed_telemetry_count=result.observed_telemetry_count,
                    observed_other_response_count=result.observed_other_response_count,
                )
            return self._command_result(
                payload,
                ok=True,
                message="ACK 成功，主动上传已关闭，设备已进入被动读取/等待命令状态。",
                pending=pending,
                response_lines=result.response_lines,
                matched_response_line=result.matched_response_line,
                observed_telemetry_count=result.observed_telemetry_count,
                observed_other_response_count=result.observed_other_response_count,
                auto_upload_state="off",
            )

        if pending.requires_pre_silence_for_write or pending.requires_quiet_window_for_read:
            silence_result = self._ensure_write_silence(timeout_ms, parent_command=payload, pending=pending)
            if silence_result is not None:
                self.command_completed.emit(silence_result)
                if not silence_result.ok:
                    return self._command_result(
                        payload,
                        ok=False,
                        message=silence_result.message,
                        pending=pending,
                        response_lines=silence_result.response_lines,
                        response_kind=silence_result.response_kind,
                        response_device_id=silence_result.response_device_id,
                        parsed_payload=silence_result.parsed_payload,
                        matched_response_line=silence_result.matched_response_line,
                        timeout_reason=silence_result.timeout_reason,
                        observed_telemetry_count=silence_result.observed_telemetry_count,
                        observed_other_response_count=silence_result.observed_other_response_count,
                    )
            mismatch = self._preflight_target_mismatch(pending)
            if mismatch:
                if pending.new_target_id:
                    self._pending_id_target = None
                return self._command_result(payload, ok=False, message=mismatch, pending=pending)

        result = self._write_and_wait(payload, pending, timeout_ms=timeout_ms)
        if pending.new_target_id and not result.ok and self._pending_id_target == pending.new_target_id:
            self._pending_id_target = None
        if pending.requires_quiet_window_for_read:
            restore_attempted = self._should_restore_after_quiet_read(pending)
            result.restore_attempted = restore_attempted
            if restore_attempted:
                restore_result = self._restore_auto_upload(timeout_ms, parent_command=payload, pending=pending)
                if restore_result is not None:
                    self.command_completed.emit(restore_result)
                    result.restore_result = "restored" if restore_result.ok else "restore_failed"
            else:
                result.restore_result = self._quiet_read_restore_result_without_attempt(pending)
        return result

    @staticmethod
    def _normalize_auto_upload_state(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"on", "off", "unknown", "temporarily_silenced"}:
            return normalized
        return "unknown"

    def _quiet_read_original_auto_upload_state(self, pending: _PendingCommand | None) -> str:
        if pending is None or not pending.requires_quiet_window_for_read:
            return ""
        return self._normalize_auto_upload_state(pending.operation_context.get("original_auto_upload_state"))

    def _quiet_read_restore_policy(self, pending: _PendingCommand | None) -> str:
        if pending is None or not pending.requires_quiet_window_for_read:
            return ""
        original_state = self._quiet_read_original_auto_upload_state(pending)
        if original_state == "on":
            return "restore_after_read"
        if original_state == "off":
            return "keep_off"
        if original_state == "temporarily_silenced":
            return "preserve_existing_silence"
        requested_policy = str(pending.operation_context.get("restore_policy") or "").strip()
        if requested_policy == "user_confirmed_restore":
            return requested_policy
        if requested_policy == "user_declined_restore":
            return requested_policy
        return "unknown_no_restore"

    def _should_restore_after_quiet_read(self, pending: _PendingCommand | None) -> bool:
        return self._quiet_read_restore_policy(pending) in {"restore_after_read", "user_confirmed_restore"}

    def _quiet_read_restore_result_without_attempt(self, pending: _PendingCommand | None) -> str:
        policy = self._quiet_read_restore_policy(pending)
        mapping = {
            "keep_off": "kept_off",
            "preserve_existing_silence": "preserved_existing_silence",
            "user_declined_restore": "left_unknown",
            "unknown_no_restore": "left_unknown",
        }
        return mapping.get(policy, "not_attempted")

    def _command_result(
        self,
        payload: str,
        *,
        ok: bool,
        message: str,
        pending: _PendingCommand | None = None,
        timestamp: datetime | None = None,
        response_lines: list[str] | None = None,
        response_kind: str = "",
        response_device_id: str | None = None,
        parsed_payload: dict[str, Any] | None = None,
        matched_response_line: str = "",
        timeout_reason: str = "",
        observed_telemetry_count: int = 0,
        observed_other_response_count: int = 0,
        action_type: str = "",
        parent_command: str = "",
        source_page: str = "",
        auto_upload_state: str = "",
        restore_attempted: bool = False,
        restore_result: str = "",
        parent_pending_for_context: _PendingCommand | None = None,
    ) -> CommandResult:
        target_id = ""
        expected_device_id = ""
        effective_scope = ""
        original_auto_upload_state = ""
        restore_policy = ""
        context_pending = parent_pending_for_context or pending
        operation_context = dict(context_pending.operation_context) if context_pending is not None else {}
        resolved_action_type = str(action_type or operation_context.get("action_type") or "").strip()
        resolved_parent_command = str(parent_command or operation_context.get("parent_command") or "").strip()
        resolved_source_page = str(source_page or operation_context.get("source_page") or "").strip()
        resolved_auto_upload_state = str(auto_upload_state or operation_context.get("auto_upload_state") or "").strip()
        if pending is not None:
            target_id = pending.target_id
            expected_device_id = self._expected_response_device_id(pending)
            effective_scope = pending.effective_scope
        if context_pending is not None:
            original_auto_upload_state = self._quiet_read_original_auto_upload_state(context_pending)
            restore_policy = self._quiet_read_restore_policy(context_pending)
        else:
            envelope = YGasProtocol.parse_command(payload)
            if envelope is not None:
                target_id = envelope.target_id
                effective_scope = AutoSilencePolicy.effective_scope(envelope.target_id)
        return CommandResult(
            timestamp=timestamp or datetime.now(),
            command=payload,
            ok=ok,
            message=message,
            response_lines=list(response_lines or []),
            response_kind=response_kind,
            response_device_id=response_device_id,
            parsed_payload=dict(parsed_payload or {}),
            matched_response_line=matched_response_line,
            timeout_reason=timeout_reason,
            observed_telemetry_count=observed_telemetry_count,
            observed_other_response_count=observed_other_response_count,
            action_type=resolved_action_type,
            action_label_zh=action_label_zh(resolved_action_type or "command"),
            parent_command=resolved_parent_command,
            command_target_id=target_id,
            expected_device_id=expected_device_id,
            effective_scope=effective_scope,
            source_page=resolved_source_page,
            auto_upload_state=resolved_auto_upload_state,
            original_auto_upload_state=original_auto_upload_state,
            restore_policy=restore_policy,
            restore_attempted=restore_attempted,
            restore_result=restore_result,
        )

    def _ensure_write_silence(
        self,
        timeout_ms: int,
        *,
        parent_command: str,
        pending: _PendingCommand,
    ) -> CommandResult | None:
        auto_write_error = self._can_issue_automatic_write()
        if auto_write_error:
            return self._command_result(
                parent_command,
                ok=False,
                message=f"暂停主动上传失败：{auto_write_error}",
                pending=pending,
                action_type="auto_silence",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_PAUSE_SOURCE_PAGE,
                auto_upload_state="unknown",
                restore_result="not_attempted",
                parent_pending_for_context=pending,
            )

        try:
            silence_target_id = AutoSilencePolicy.silence_target_id(
                target_id_policy=pending.target_id,
                expected_device_id=self._expected_response_device_id(pending),
                active_device_ids=list(self._active_rx_cache),
                allow_broadcast=bool(pending.operation_context.get("allow_broadcast_silence", False)),
            )
        except AutoSilenceTargetResolutionError as exc:
            return self._command_result(
                parent_command,
                ok=False,
                message=f"暂停主动上传失败：{exc}",
                pending=pending,
                action_type="auto_silence",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_PAUSE_SOURCE_PAGE,
                auto_upload_state="unknown",
                restore_result="not_attempted",
                parent_pending_for_context=pending,
            )
        silence_payload = AutoSilencePolicy.silence_payload(
            silence_target_id,
            expected_device_id=self._expected_response_device_id(pending),
            active_device_ids=list(self._active_rx_cache),
            allow_broadcast=bool(pending.operation_context.get("allow_broadcast_silence", False)),
        )
        silence_pending = _PendingCommand(
            envelope=YGasProtocol.parse_command(silence_payload) or CommandEnvelope("SETCOMWAY", silence_target_id, ["0"]),
            expectation="ack",
            expected_response_device_id=self._expected_response_device_id(pending),
            operation_context=dict(pending.operation_context),
        )
        result = self._write_and_wait(silence_payload, silence_pending, timeout_ms=timeout_ms)
        if not result.ok:
            message = (
                "暂停主动上传失败：未收到 SETCOMWAY ACK。"
                if "超时" in result.message
                else f"暂停主动上传失败：{result.message}"
            )
            return self._command_result(
                silence_payload,
                ok=False,
                message=message,
                pending=silence_pending,
                timestamp=result.timestamp,
                response_lines=result.response_lines,
                response_kind=result.response_kind,
                response_device_id=result.response_device_id,
                parsed_payload=result.parsed_payload,
                matched_response_line=result.matched_response_line,
                timeout_reason=result.timeout_reason,
                observed_telemetry_count=result.observed_telemetry_count,
                observed_other_response_count=result.observed_other_response_count,
                action_type="auto_silence",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_PAUSE_SOURCE_PAGE,
                auto_upload_state="unknown",
                restore_result="not_attempted",
                parent_pending_for_context=pending,
            )

        silence_error = self._observe_silence_failure(initial_lines=result.response_lines)
        if silence_error:
            return self._command_result(
                silence_payload,
                ok=False,
                message=f"暂停主动上传失败：{silence_error}",
                pending=silence_pending,
                timestamp=result.timestamp,
                response_lines=result.response_lines,
                response_kind=result.response_kind,
                response_device_id=result.response_device_id,
                parsed_payload=result.parsed_payload,
                matched_response_line=result.matched_response_line,
                observed_telemetry_count=result.observed_telemetry_count,
                observed_other_response_count=result.observed_other_response_count,
                action_type="auto_silence",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_PAUSE_SOURCE_PAGE,
                auto_upload_state="unknown",
                restore_result="not_attempted",
                parent_pending_for_context=pending,
            )
        return self._command_result(
            silence_payload,
            ok=True,
            message="ACK 成功，主动上传已临时暂停。",
            pending=silence_pending,
            timestamp=result.timestamp,
            response_lines=result.response_lines,
            response_kind=result.response_kind or "ack",
            response_device_id=result.response_device_id,
            parsed_payload=result.parsed_payload,
            matched_response_line=result.matched_response_line,
            observed_telemetry_count=result.observed_telemetry_count,
            observed_other_response_count=result.observed_other_response_count,
            action_type="auto_silence",
            parent_command=parent_command,
            source_page=AUTO_UPLOAD_PAUSE_SOURCE_PAGE,
            auto_upload_state="temporarily_silenced" if pending.requires_quiet_window_for_read else "off",
            restore_result=(
                "pending_restore"
                if self._should_restore_after_quiet_read(pending)
                else self._quiet_read_restore_result_without_attempt(pending)
            ),
            parent_pending_for_context=pending,
        )

    def _restore_auto_upload(
        self,
        timeout_ms: int,
        *,
        parent_command: str,
        pending: _PendingCommand,
    ) -> CommandResult | None:
        auto_write_error = self._can_issue_automatic_write()
        if auto_write_error:
            return self._command_result(
                parent_command,
                ok=False,
                message=f"恢复主动上传失败：{auto_write_error}",
                pending=pending,
                action_type="auto_silence_restore",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_RESTORE_SOURCE_PAGE,
                auto_upload_state="off",
                restore_attempted=True,
                restore_result="restore_failed",
                parent_pending_for_context=pending,
            )
        try:
            restore_payload = AutoSilencePolicy.restore_payload(
                pending.target_id,
                expected_device_id=self._expected_response_device_id(pending),
                active_device_ids=list(self._active_rx_cache),
                allow_broadcast=bool(pending.operation_context.get("allow_broadcast_silence", False)),
            )
        except AutoSilenceTargetResolutionError as exc:
            return self._command_result(
                parent_command,
                ok=False,
                message=f"恢复主动上传失败：{exc}",
                pending=pending,
                action_type="auto_silence_restore",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_RESTORE_SOURCE_PAGE,
                auto_upload_state="off",
                restore_attempted=True,
                restore_result="restore_failed",
                parent_pending_for_context=pending,
            )
        restore_pending = _PendingCommand(
            envelope=YGasProtocol.parse_command(restore_payload) or CommandEnvelope("SETCOMWAY", pending.target_id, ["1"]),
            expectation="ack",
            expected_response_device_id=self._expected_response_device_id(pending),
            operation_context=dict(pending.operation_context),
        )
        result = self._write_and_wait(restore_payload, restore_pending, timeout_ms=timeout_ms)
        if not result.ok:
            message = (
                "恢复主动上传失败：未收到 SETCOMWAY ACK。"
                if "超时" in result.message
                else f"恢复主动上传失败：{result.message}"
            )
            return self._command_result(
                restore_payload,
                ok=False,
                message=message,
                pending=restore_pending,
                timestamp=result.timestamp,
                response_lines=result.response_lines,
                response_kind=result.response_kind,
                response_device_id=result.response_device_id,
                parsed_payload=result.parsed_payload,
                matched_response_line=result.matched_response_line,
                timeout_reason=result.timeout_reason,
                observed_telemetry_count=result.observed_telemetry_count,
                observed_other_response_count=result.observed_other_response_count,
                action_type="auto_silence_restore",
                parent_command=parent_command,
                source_page=AUTO_UPLOAD_RESTORE_SOURCE_PAGE,
                auto_upload_state="off",
                restore_attempted=True,
                restore_result="restore_failed",
                parent_pending_for_context=pending,
            )
        return self._command_result(
            restore_payload,
            ok=True,
            message="ACK 成功，已恢复主动上传。",
            pending=restore_pending,
            timestamp=result.timestamp,
            response_lines=result.response_lines,
            response_kind=result.response_kind or "ack",
            response_device_id=result.response_device_id,
            parsed_payload=result.parsed_payload,
            matched_response_line=result.matched_response_line,
            observed_telemetry_count=result.observed_telemetry_count,
            observed_other_response_count=result.observed_other_response_count,
            action_type="auto_silence_restore",
            parent_command=parent_command,
            source_page=AUTO_UPLOAD_RESTORE_SOURCE_PAGE,
            auto_upload_state="on",
            restore_attempted=True,
            restore_result="restored",
            parent_pending_for_context=pending,
        )

    def _write_and_wait(self, payload: str, pending: _PendingCommand, *, timeout_ms: int) -> CommandResult:
        try:
            self._transport.write_line(payload)
            self._record("TX", self._format_tx_record(payload))
        except Exception as exc:
            message = f"命令发送失败: {exc}"
            self._handle_transport_fault(message)
            self.error.emit(message)
            return self._command_result(payload, ok=False, message=message, pending=pending)

        if pending.expectation == "none":
            return self._command_result(payload, ok=True, message="命令已发送。", pending=pending)
        return self._wait_for_response(payload, pending=pending, timeout_ms=timeout_ms)

    def _wait_for_response(self, payload: str, *, pending: _PendingCommand, timeout_ms: int) -> CommandResult:
        deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
        lines_seen: list[str] = []
        mismatched_device_ids: set[str] = set()
        ack_seen = False
        telemetry_seen = 0
        other_response_seen = 0
        matched_response: _ParsedResponse | None = None
        matched_response_line = ""
        stream_active_before_wait = self._has_recent_stream_frame()

        while time.monotonic() < deadline:
            try:
                batch = self._read_batch()
            except Exception as exc:
                message = f"命令等待期间串口异常断开: {exc}"
                self._handle_transport_fault(message)
                self.error.emit(message)
                return self._command_result(
                    payload,
                    ok=False,
                    message="连接断开，未在等待期间收到预期响应。",
                    pending=pending,
                    response_lines=lines_seen[:],
                    response_kind="transport_error",
                    timeout_reason="transport_disconnected",
                    observed_telemetry_count=telemetry_seen,
                    observed_other_response_count=other_response_seen,
                )
            if batch:
                lines_seen.extend(batch)
                for line in batch:
                    line_kind = YGasProtocol.classify_line(line, parse_mode=self._config.mode_preference)
                    frame = self._parse_frame_line(line) if line_kind == "telemetry" else None
                    if frame is not None:
                        self._publish_frame(frame)

                    outcome = self._parse_expected_response_structured(
                        line,
                        pending,
                        stream_active_before_wait=stream_active_before_wait,
                    )
                    if outcome == "ack_seen":
                        ack_seen = True
                        continue
                    if isinstance(outcome, tuple) and outcome[0] == "mismatch":
                        mismatched_device_ids.add(outcome[1])
                        continue
                    if isinstance(outcome, tuple) and outcome[0] == "result":
                        parsed = outcome[1]
                        if matched_response is None:
                            matched_response = parsed
                            matched_response_line = line
                        continue
                    if pending.expectation == "any":
                        if matched_response is None:
                            matched_response = _ParsedResponse(
                                ok=True,
                                message="收到设备响应。",
                                kind=line_kind or "any",
                                device_id=YGasProtocol.response_device_id(line, parse_mode=self._config.mode_preference),
                            )
                            matched_response_line = line
                        continue
                    if line_kind == "telemetry":
                        telemetry_seen += 1
                    elif line_kind:
                        other_response_seen += 1
                continue
            if matched_response is not None:
                return self._command_result(
                    payload,
                    ok=matched_response.ok,
                    message=matched_response.message,
                    pending=pending,
                    response_lines=lines_seen[:],
                    response_kind=matched_response.kind,
                    response_device_id=matched_response.device_id,
                    parsed_payload=matched_response.payload,
                    matched_response_line=matched_response_line,
                    observed_telemetry_count=telemetry_seen,
                    observed_other_response_count=other_response_seen,
                )

        if matched_response is not None:
            return self._command_result(
                payload,
                ok=matched_response.ok,
                message=matched_response.message,
                pending=pending,
                response_lines=lines_seen[:],
                response_kind=matched_response.kind,
                response_device_id=matched_response.device_id,
                parsed_payload=matched_response.payload,
                matched_response_line=matched_response_line,
                observed_telemetry_count=telemetry_seen,
                observed_other_response_count=other_response_seen,
            )

        message, timeout_reason = self._timeout_message_for(
            pending,
            ack_seen=ack_seen,
            mismatched_device_ids=mismatched_device_ids,
            timeout_ms=timeout_ms,
            telemetry_seen=telemetry_seen,
            other_response_seen=other_response_seen,
        )
        self.error.emit(f"{payload} -> {message}")
        return self._command_result(
            payload,
            ok=False,
            message=message,
            pending=pending,
            response_lines=lines_seen,
            response_kind="timeout",
            timeout_reason=timeout_reason,
            observed_telemetry_count=telemetry_seen,
            observed_other_response_count=other_response_seen,
        )

    def _parse_expected_response_structured(
        self,
        line: str,
        pending: _PendingCommand,
        *,
        stream_active_before_wait: bool = False,
    ) -> tuple[str, _ParsedResponse] | tuple[str, str] | str | None:
        ack = YGasProtocol.parse_ack(line)
        if ack is not None:
            self._track_rx_device(ack.device_id, update_latest=True, source="ACK")
            self._maybe_sync_target_from_rx(ack.device_id, source="ACK")
            if pending.expectation == "ack":
                if not self._matches_target(
                    ack.device_id,
                    pending.target_id,
                    allow_broadcast_ack=pending.allows_broadcast_ack,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", ack.device_id)
                if pending.new_target_id and ack.ok and ack.device_id == pending.new_target_id:
                    self._sync_target_to(pending.new_target_id, source="ACK")
                if not ack.ok:
                    return (
                        "result",
                        _ParsedResponse(
                            ok=False,
                            message=f"设备返回失败 ACK: {ack.detail or 'F'}",
                            kind="ack",
                            device_id=ack.device_id,
                            payload={"detail": ack.detail or "F"},
                        ),
                    )
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=(
                            "ACK 成功，但当前广播 ACK 归属未绑定到明确会话目标。"
                            if pending.target_id == "FFF"
                            and pending.allows_broadcast_ack
                            and not self._expected_response_device_id(pending)
                            else "ACK 成功。"
                        ),
                        kind="ack",
                        device_id=ack.device_id,
                        payload={"detail": ack.detail or ""},
                    ),
                )
            if pending.expectation == "coefficient":
                return "ack_seen"

        device_id = YGasProtocol.response_device_id(line, parse_mode=self._config.mode_preference)
        if device_id and ack is None:
            self._track_rx_device(device_id, update_latest=False, source="REPLY")
            self._maybe_sync_target_from_rx(device_id, source="REPLY")

        if pending.expectation == "data":
            frame = self._parse_frame_line(line)
            if frame is not None:
                if not self._matches_target(
                    frame.device_id or "",
                    pending.target_id,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", frame.device_id or "")
                if pending.code == "READDATA" and stream_active_before_wait:
                    return None
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=f"收到数据帧，MODE{frame.mode}。",
                        kind="data",
                        device_id=frame.device_id,
                        payload={"mode": frame.mode, "fields": dict(frame.fields), "status": frame.status or ""},
                    ),
                )

        if pending.expectation == "coefficient":
            coeff = YGasProtocol.parse_coefficient_reply(line)
            if coeff is not None:
                mismatch = self._preflight_target_mismatch(pending)
                if mismatch:
                    return ("result", _ParsedResponse(ok=False, message=mismatch))
                suggestion = self._coefficient_suggestion(coeff)
                message = f"收到系数响应: {coeff}"
                if suggestion:
                    message += f" | 建议写回格式: {suggestion}"
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=message,
                        kind="coefficient",
                        device_id=self._display_target_id(pending),
                        payload=coeff,
                    ),
                )

        if pending.expectation == "serial_config":
            serial_cfg = YGasProtocol.parse_serial_config_reply(line)
            if serial_cfg is not None:
                if not self._matches_target(
                    serial_cfg["device_id"],
                    pending.target_id,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", serial_cfg["device_id"])
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=(
                            f"通信参数: {serial_cfg['baudrate']} / {serial_cfg['bytesize']} / "
                            f"{serial_cfg['parity']} / {serial_cfg['stopbits']}"
                        ),
                        kind="serial_config",
                        device_id=serial_cfg["device_id"],
                        payload=serial_cfg,
                    ),
                )

        if pending.expectation == "mode_value":
            mode_value = YGasProtocol.parse_mode_value_reply(line)
            if mode_value is not None:
                if not self._matches_target(
                    mode_value["device_id"],
                    pending.target_id,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", mode_value["device_id"])
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=f"当前工作模式: MODE{mode_value['mode']}",
                        kind="mode_value",
                        device_id=mode_value["device_id"],
                        payload=mode_value,
                    ),
                )

        if pending.expectation == "identity":
            identity = YGasProtocol.parse_identity_reply(line)
            if identity is not None:
                if not self._matches_target(
                    identity["device_id"],
                    pending.target_id,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", identity["device_id"])
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=f"当前设备 ID: {identity['device_id']}",
                        kind="identity",
                        device_id=identity["device_id"],
                        payload=identity,
                    ),
                )

        if pending.expectation == "setting_value":
            value = YGasProtocol.parse_setting_value_reply(line)
            if value is not None:
                if not self._matches_target(
                    value["device_id"],
                    pending.target_id,
                    expected_device_id=self._expected_response_device_id(pending),
                ):
                    return ("mismatch", value["device_id"])
                return (
                    "result",
                    _ParsedResponse(
                        ok=True,
                        message=f"当前值: {value['value']}",
                        kind="setting_value",
                        device_id=value["device_id"],
                        payload=value,
                    ),
                )

        return None

    def _ensure_timer(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(40)
            self._timer.timeout.connect(self._tick)
        self._timer.start()

    @Slot()
    def _tick(self) -> None:
        if not self._transport or not self._transport.is_open:
            return
        try:
            now = time.monotonic()
            self._prune_active_rx_devices(now)
            if self._config.acquisition_mode == "POLL" and not self._config.listen_only and now >= self._next_poll_ts:
                payload = YGasProtocol.build_command("READDATA", target_id=self._config.target_id)
                self._transport.write_line(payload)
                self._record("TX", self._format_tx_record(payload))
                self._next_poll_ts = now + max(0.05, self._config.poll_interval_ms / 1000.0)
                self._pending_poll_deadline = now + max(1.0, self._config.command_timeout_ms / 1000.0)

            for line in self._read_batch():
                frame = self._parse_frame_line(line)
                if frame is not None:
                    self._publish_frame(frame)
                    self._pending_poll_deadline = 0.0

            if self._pending_poll_deadline and time.monotonic() > self._pending_poll_deadline:
                self._pending_poll_deadline = 0.0
                self.error.emit("被动轮询超时：未在预期时间内收到 READDATA 响应。")

            if self._last_frame is not None and (now - self._last_metrics_emit_ts) >= 0.25:
                self._last_metrics_emit_ts = now
                self.metrics_updated.emit(self._metrics.snapshot(self._last_frame))
        except Exception as exc:
            self._handle_transport_fault(f"串口异常断开: {exc}")
            self.error.emit(f"采集线程异常: {exc}")

    def _read_batch(self) -> list[str]:
        if not self._transport:
            return []
        chunk = self._transport.read_available()
        if not chunk:
            return []
        lines = self._stream_buffer.feed(chunk)
        for line in lines:
            self._record("RX", line)
        return lines

    def _publish_frame(self, frame: ParsedFrame) -> None:
        self._last_frame = frame
        self._last_stream_frame_monotonic = time.monotonic()
        self._track_rx_device(frame.device_id, update_latest=True, source="DATA")
        self._maybe_sync_target_from_rx(frame.device_id, source="DATA")
        self.frame_received.emit(frame)
        self.metrics_updated.emit(self._metrics.push(frame))
        if frame.device_id and frame.device_id not in self._known_ids:
            self._known_ids.add(frame.device_id)
            self.device_ids_updated.emit(sorted(self._known_ids))
        decoded = YGasProtocol.decode_status(frame.status)
        active_alarm_bits = {item.bit for item in decoded if item.is_alarm}
        newly_active = active_alarm_bits - self._last_alarm_bits
        for item in decoded:
            if item.bit in newly_active:
                event = AlarmEvent(
                    timestamp=frame.timestamp,
                    message=f"{item.label}: {item.description}",
                    severity="WARN",
                )
                self.alarm_emitted.emit(event)
        self._last_alarm_bits = active_alarm_bits

    def _observe_silence_failure(self, *, initial_lines: list[str] | None = None) -> str | None:
        deadline = time.monotonic() + SILENCE_WINDOW_S
        stream_modes: list[int] = []
        for line in initial_lines or []:
            frame = self._parse_telemetry_line_any_mode(line)
            if frame is not None:
                stream_modes.append(frame.mode)
        if stream_modes:
            modes = "/".join(f"MODE{mode}" for mode in sorted(set(stream_modes)))
            return f"收到 SETCOMWAY ACK，但暂停上传窗口内仍收到实时数据帧（{modes}），主动上传未真正关闭。"
        while time.monotonic() < deadline:
            try:
                batch = self._read_batch()
            except Exception as exc:
                return f"等待暂停上传窗口时串口异常断开：{exc}"
            for line in batch:
                frame = self._parse_telemetry_line_any_mode(line)
                if frame is not None:
                    self._publish_frame(frame)
                    stream_modes.append(frame.mode)
            if stream_modes:
                modes = "/".join(f"MODE{mode}" for mode in sorted(set(stream_modes)))
                return f"收到 SETCOMWAY ACK，但暂停上传窗口内仍收到实时数据帧（{modes}），主动上传未真正关闭。"
        return None

    def _flush_input_buffer(self) -> None:
        if self._transport is None:
            return
        try:
            self._transport.flush_input()
        except Exception:
            pass
        self._partial = ""
        self._stream_buffer.clear()

    def _timeout_message_for(
        self,
        pending: _PendingCommand,
        *,
        ack_seen: bool,
        mismatched_device_ids: set[str],
        timeout_ms: int,
        telemetry_seen: int,
        other_response_seen: int,
    ) -> tuple[str, str]:
        if mismatched_device_ids:
            online = ",".join(sorted(mismatched_device_ids))
            return (
                f"响应来自错误设备 ID: target={self._display_target_id(pending)}，online={online}。"
                "本次命令未判定成功。"
            ), "mismatched_device"
        mismatch = self._preflight_target_mismatch(pending)
        if mismatch:
            return mismatch, "preflight_target_mismatch"
        if pending.is_getco and ack_seen:
            return "已暂停主动上传，但 GETCO 只有 ACK、没有系数行。", "ack_without_followup"
        if pending.expectation == "data" and pending.code == "READDATA" and telemetry_seen > 0:
            return (
                "当前设备正在自动上传实时数据，READDATA 返回帧与自动流无法可靠区分。"
                f"等待期间收到 {telemetry_seen} 条实时数据帧。"
                "请直接查看实时数据，或先关闭主动上传后再执行 READDATA。"
            ), "telemetry_data_ambiguous"
        if telemetry_seen > 0 and other_response_seen <= 0:
            return (
                f"命令超时，等待期间持续收到 {telemetry_seen} 条实时数据帧，"
                "但未匹配到预期响应。"
            ), "telemetry_only_no_match"
        if telemetry_seen > 0 and other_response_seen > 0:
            return (
                f"命令超时，等待期间收到 {telemetry_seen} 条实时数据帧和 {other_response_seen} 条非匹配响应，"
                "但未匹配到预期响应。"
            ), "telemetry_and_other_responses"
        if other_response_seen > 0:
            return (
                f"命令超时，等待期间收到 {other_response_seen} 条非匹配响应，"
                "但未匹配到预期响应。"
            ), "other_response_no_match"
        return f"命令超时，未在 {timeout_ms} ms 内收到预期响应。", "deadline_expired"

    def _coefficient_suggestion(self, coeff: dict[str, float]) -> str:
        ordered = sorted(coeff.items(), key=lambda item: int(item[0][1:]))
        formatted: list[str] = []
        for _, value in ordered[:6]:
            formatted.append(normalize_senco_coefficient(str(value)))
        return ",".join(formatted)

    def _matches_target(
        self,
        response_device_id: str,
        target_id: str,
        *,
        allow_broadcast_ack: bool = False,
        expected_device_id: str = "",
    ) -> bool:
        if not response_device_id:
            return False
        normalized_target = str(target_id or "").strip().upper()
        normalized_response = str(response_device_id or "").strip().upper()
        if normalized_target == "FFF":
            if not allow_broadcast_ack:
                return False
            normalized_expected = str(expected_device_id or "").strip().upper()
            if normalized_expected:
                return normalized_response == normalized_expected
            return True
        return normalized_response == normalized_target

    def _expected_response_device_id(self, pending: _PendingCommand) -> str:
        return str(pending.expected_response_device_id or "").strip().upper()

    def _snapshot_expected_response_device_id(self, envelope: CommandEnvelope) -> str:
        payload_target = str(envelope.target_id or "").strip().upper()
        if payload_target.isdigit():
            return payload_target
        session_target = str(self._config.target_id or "").strip().upper()
        if session_target.isdigit():
            return session_target
        single_online = self._single_online_numeric_device_id()
        return str(single_online or "").strip().upper()

    def _preflight_target_mismatch(self, pending: _PendingCommand) -> str:
        target = self._object_target_id(pending)
        active_numeric_ids = self._active_online_numeric_device_ids()
        if pending.requires_object_consistency:
            if not target:
                return "当前写命令缺少明确目标设备 ID，无法执行对象一致性校验，请先校正目标设备或重新握手。"
            if not active_numeric_ids:
                return (
                    f"当前无法确认实时在线设备 ID：target={target}。"
                    "当前写命令虽默认使用 FFF，但对象一致性校验仍要求唯一在线设备。"
                    "请先校正目标设备或重新握手。"
                )
            if len(active_numeric_ids) > 1:
                online = ",".join(active_numeric_ids)
                return (
                    f"当前检测到多个实时在线设备 ID：target={target}，online={online}。"
                    "当前写命令虽默认使用 FFF，但对象一致性校验仍要求唯一在线设备。"
                    "请先隔离现场设备或重新握手。"
                )
            online = active_numeric_ids[0]
            if online != target:
                if pending.target_id == "FFF":
                    return (
                        f"当前目标设备 ID 与实时在线设备 ID 不一致：target={target}，online={online}。"
                        "当前写命令虽默认使用 FFF，但仍禁止在对象不一致时发送。"
                        "请先校正目标设备或重新握手。"
                    )
                return (
                    f"当前目标设备 ID 与实时在线设备 ID 不一致：target={target}，online={online}，"
                    "请先校正目标设备或重新握手。"
                )
            return ""
        if target and target.isdigit() and len(active_numeric_ids) == 1 and active_numeric_ids[0] != target:
            return (
                f"当前目标设备 ID 与实时在线设备 ID 不一致：target={target}，online={active_numeric_ids[0]}，"
                "请先校正目标设备或重新握手。"
            )
        return ""

    def _display_target_id(self, pending: _PendingCommand) -> str:
        return self._object_target_id(pending) or pending.target_id

    def _has_recent_stream_frame(self) -> bool:
        if self._last_stream_frame_monotonic <= 0:
            return False
        return (time.monotonic() - self._last_stream_frame_monotonic) <= ACTIVE_RX_WINDOW_S

    def _object_target_id(self, pending: _PendingCommand) -> str:
        payload_target = str(pending.target_id or "").strip().upper()
        if payload_target.isdigit():
            return payload_target
        session_target = str(self._config.target_id or "").strip().upper()
        if session_target.isdigit():
            return session_target
        return ""

    def _active_online_numeric_device_ids(self) -> list[str]:
        return sorted({device_id for device_id in self._active_rx_cache if device_id.isdigit()})

    def _single_online_numeric_device_id(self) -> str | None:
        active = self._active_online_numeric_device_ids()
        if len(active) == 1:
            return active[0]
        return None

    def _track_rx_device(self, device_id: str | None, *, update_latest: bool, source: str) -> None:
        normalized = str(device_id or "").strip().upper()
        if not normalized:
            return
        now = time.monotonic()
        self._active_rx_entries.append((now, normalized))
        latest_changed = False
        if update_latest:
            latest_changed = self._latest_rx_device_id != normalized
            self._latest_rx_device_id = normalized
        self._prune_active_rx_devices(now)
        if latest_changed:
            self._emit_rx_device_state()
        if update_latest and latest_changed:
            self._emit_info(f"最近有效{source}设备 ID: {normalized}")

    def _prune_active_rx_devices(self, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        while self._active_rx_entries and (current - self._active_rx_entries[0][0]) > ACTIVE_RX_WINDOW_S:
            self._active_rx_entries.popleft()
        active = tuple(sorted({device_id for _, device_id in self._active_rx_entries}))
        if active != self._active_rx_cache:
            self._active_rx_cache = active
            self._emit_rx_device_state()

    def _emit_rx_device_state(self) -> None:
        self.rx_device_state_changed.emit(
            {
                "latest_rx_device_id": self._latest_rx_device_id,
                "active_rx_device_ids": list(self._active_rx_cache),
            }
        )

    def _maybe_sync_target_from_rx(self, device_id: str | None, *, source: str) -> None:
        normalized = str(device_id or "").strip().upper()
        if not normalized or not self._pending_id_target:
            return
        if normalized != self._pending_id_target:
            return
        self._sync_target_to(normalized, source=source)

    def _sync_target_to(self, target_id: str, *, source: str) -> None:
        normalized = str(target_id or "").strip().upper()
        if not normalized:
            return
        self._config.target_id = normalized
        self._pending_id_target = None
        self.target_sync_requested.emit(normalized, source)
        self._emit_info(f"检测到设备 ID 已切换为 {normalized}，已同步当前会话目标。")

    def _record(self, direction: str, text: str, level: str = "INFO") -> None:
        record = RawFrameRecord(timestamp=datetime.now(), direction=direction, text=text, level=level)
        self.raw_received.emit(record)
        if self._logger is not None:
            self._logger.log_record(record)

    def _emit_info(self, message: str) -> None:
        self.info.emit(message)
        if self._logger is not None:
            self._logger.log(datetime.now().isoformat(timespec="milliseconds"), "SYS", "INFO", message)

    def _parse_frame_line(self, line: str) -> ParsedFrame | None:
        return YGasProtocol.parse_line(line, parse_mode=self._config.mode_preference)

    @staticmethod
    def _parse_telemetry_line_any_mode(line: str) -> ParsedFrame | None:
        return YGasProtocol.parse_line(line, parse_mode=PARSE_MODE_AUTO)

    def _handle_transport_fault(self, message: str) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self.connection_changed.emit(False, str(self._logger.path) if self._logger is not None else "")
        self._emit_info(message)

    def _can_issue_automatic_write(self) -> str:
        if self._config.read_only_lock:
            return "只读锁已开启，禁止系统自动发送 SETCOMWAY。"
        if self._config.session_mode != SESSION_MODE_ENGINEERING:
            return "当前会话模式不允许系统自动发送 SETCOMWAY。"
        if not has_permission(self._config.permission_level, "CONFIG"):
            return "当前权限不足，系统不会自动发送 SETCOMWAY。"
        return ""

    @staticmethod
    def _format_tx_record(payload: str) -> str:
        envelope = YGasProtocol.parse_command(payload)
        target_id = envelope.target_id if envelope is not None else "--"
        broadcast = "yes" if target_id == "FFF" else "no"
        prefix = "[FFF] " if target_id == "FFF" else ""
        return f"{prefix}{payload} | target={target_id} | broadcast={broadcast}"


class AnalyzerSessionController(QObject):
    frame_received = Signal(object)
    raw_received = Signal(object)
    alarm_emitted = Signal(object)
    metrics_updated = Signal(object)
    device_ids_updated = Signal(object)
    rx_device_state_changed = Signal(object)
    target_sync_requested = Signal(str, str)
    connection_changed = Signal(bool, str)
    command_completed = Signal(object)
    error = Signal(str)
    info = Signal(str)

    _open_requested = Signal(object)
    _close_requested = Signal()
    _config_requested = Signal(object)
    _init_requested = Signal()
    _payload_requested = Signal(str, str, int, object)

    def __init__(self, session_name: str):
        super().__init__()
        self.session_name = session_name
        self.current_config = SessionConfig(session_name=session_name)
        self.connected = False
        self.frames: deque[ParsedFrame] = deque(maxlen=DEFAULT_HISTORY_SIZE)
        self.raw_records: deque[RawFrameRecord] = deque(maxlen=DEFAULT_HISTORY_SIZE)
        self.alarms: deque[AlarmEvent] = deque(maxlen=DEFAULT_HISTORY_SIZE)
        self.log_path = ""
        self.latest_rx_device_id: str | None = None
        self.active_rx_device_ids: set[str] = set()

        self._thread = QThread(self)
        self._worker = AcquisitionWorker()
        self._worker.moveToThread(self._thread)
        self._thread.start()

        self._open_requested.connect(self._worker.open_session)
        self._close_requested.connect(self._worker.close_session)
        self._config_requested.connect(self._worker.update_session_config)
        self._init_requested.connect(self._worker.initialize_capture)
        self._payload_requested.connect(self._worker.send_payload)

        self._worker.frame_received.connect(self._handle_frame)
        self._worker.raw_received.connect(self._handle_raw)
        self._worker.alarm_emitted.connect(self._handle_alarm)
        self._worker.metrics_updated.connect(self._handle_metrics)
        self._worker.device_ids_updated.connect(self._handle_device_ids)
        self._worker.rx_device_state_changed.connect(self._handle_rx_device_state)
        self._worker.target_sync_requested.connect(self._handle_target_sync)
        self._worker.connection_changed.connect(self._handle_connection)
        self._worker.command_completed.connect(self._handle_command_completed)
        self._worker.error.connect(self._handle_error)
        self._worker.info.connect(self._handle_info)

    def connect_session(self, config: SessionConfig) -> None:
        self.current_config = config
        self._open_requested.emit(config)

    def disconnect_session(self) -> None:
        self._close_requested.emit()

    def update_config(self, config: SessionConfig) -> None:
        self.current_config = config
        self._config_requested.emit(config)

    def initialize_capture(self) -> None:
        self._init_requested.emit()

    def send_payload(
        self,
        payload: str,
        expectation: str = "ack",
        timeout_ms: int = 1500,
        context: dict[str, object] | None = None,
    ) -> None:
        self._payload_requested.emit(payload, expectation, timeout_ms, dict(context or {}))

    def validate_restore_retry_payload(self, payload: str) -> tuple[bool, str]:
        retry_payload = str(payload or "").strip()
        if not retry_payload:
            return False, "未提供恢复主动上传命令。"
        envelope = YGasProtocol.parse_command(retry_payload)
        if envelope is None or envelope.code != "SETCOMWAY" or envelope.args[:1] != ["1"]:
            return False, "恢复主动上传只允许发送有效的 SETCOMWAY=1 命令。"
        if not self.connected:
            return False, "当前未连接设备。"
        if self.current_config.read_only_lock:
            return False, "只读锁已开启，禁止发送 SETCOMWAY=1。"
        if self.current_config.session_mode == SESSION_MODE_REPLAY:
            return False, "回放期间禁止向真实设备发送命令。"
        if self.current_config.session_mode != SESSION_MODE_ENGINEERING:
            return False, "当前会话模式不允许发送 SETCOMWAY=1。"
        if not has_permission(self.current_config.permission_level, "CONFIG"):
            return False, f"当前权限不足，至少需要 {permission_label('CONFIG')} 权限。"
        return True, ""

    def validate_stream_start_payload(self, payload: str) -> tuple[bool, str]:
        stream_payload = str(payload or "").strip()
        if not stream_payload:
            return False, "未提供启动实时流命令。"
        envelope = YGasProtocol.parse_command(stream_payload)
        if envelope is None or envelope.code != "SETCOMWAY" or envelope.args[:1] != ["1"]:
            return False, "启动实时流只允许发送有效的 SETCOMWAY=1 命令。"
        if not self.connected:
            return False, "当前未连接设备。"
        if self.current_config.session_mode == SESSION_MODE_REPLAY:
            return False, "回放期间禁止向真实设备发送命令。"
        port_name = str(self.current_config.serial.port or "").strip().upper()
        if not port_name or port_name == "SIMULATOR":
            return False, "当前未连接真实串口，系统不会启动主动上传。"
        if self.current_config.read_only_lock:
            return False, "当前为严格只读，不会自动启动主动上传。"
        if self.current_config.session_mode == SESSION_MODE_LISTEN_ONLY:
            return False, "当前为严格只听模式，不会自动启动主动上传。"

        session_target_id = str(self.current_config.target_id or "").strip().upper()
        payload_target_id = str(envelope.target_id or "").strip().upper()
        if session_target_id == "FFF" or payload_target_id == "FFF":
            return False, "为避免影响总线上所有设备，系统不会自动广播启动主动上传，请选择单设备 ID。"
        if not (session_target_id.isdigit() and len(session_target_id) == 3):
            return False, f"当前目标设备 ID 无效：{session_target_id or '--'}。请先选择明确三位设备 ID。"
        if payload_target_id != session_target_id:
            return False, f"启动实时流仅允许发送到当前目标设备 {session_target_id}。"
        return True, ""

    def export_history(self, output_path: str | None = None) -> str:
        path = export_frames_to_csv(list(self.frames), output_path=output_path)
        return str(path)

    def shutdown(self) -> None:
        self.disconnect_session()
        self._thread.quit()
        self._thread.wait(3000)

    @Slot(object)
    def _handle_frame(self, frame: ParsedFrame) -> None:
        self.frames.append(frame)
        self.frame_received.emit(frame)

    @Slot(object)
    def _handle_raw(self, record: RawFrameRecord) -> None:
        self.raw_records.append(record)
        self.raw_received.emit(record)

    @Slot(object)
    def _handle_alarm(self, alarm: AlarmEvent) -> None:
        self.alarms.append(alarm)
        self.alarm_emitted.emit(alarm)

    @Slot(bool, str)
    def _handle_connection(self, connected: bool, log_path: str) -> None:
        self.connected = connected
        self.log_path = log_path
        self.connection_changed.emit(connected, log_path)

    @Slot(object)
    def _handle_metrics(self, metrics: MonitoringMetrics) -> None:
        self.metrics_updated.emit(metrics)

    @Slot(object)
    def _handle_device_ids(self, device_ids: list[str]) -> None:
        self.device_ids_updated.emit(device_ids)

    @Slot(object)
    def _handle_rx_device_state(self, state: dict[str, object]) -> None:
        self.latest_rx_device_id = str(state.get("latest_rx_device_id") or "").upper() or None
        self.active_rx_device_ids = {str(item).upper() for item in state.get("active_rx_device_ids", [])}
        self.rx_device_state_changed.emit(
            {
                "latest_rx_device_id": self.latest_rx_device_id,
                "active_rx_device_ids": sorted(self.active_rx_device_ids),
            }
        )

    @Slot(str, str)
    def _handle_target_sync(self, target_id: str, source: str) -> None:
        self.current_config.target_id = target_id
        self.target_sync_requested.emit(target_id, source)

    @Slot(object)
    def _handle_command_completed(self, result: CommandResult) -> None:
        self.command_completed.emit(result)

    @Slot(str)
    def _handle_error(self, message: str) -> None:
        self.error.emit(message)

    @Slot(str)
    def _handle_info(self, message: str) -> None:
        self.info.emit(message)
