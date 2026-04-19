"""Qt-threaded acquisition service and UI-facing session controller."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..config import DEFAULT_HISTORY_SIZE
from ..models import AlarmEvent, CommandResult, ParsedFrame, RawFrameRecord, SessionConfig
from ..protocols.senco_format import normalize_senco_coefficient
from ..protocols.ygas import CommandEnvelope, YGasProtocol
from ..serial.transport import AbstractTransport, create_transport
from .export_service import export_frames_to_csv
from .logging_service import SessionLogger
from .metrics import MetricsTracker, MonitoringMetrics

ACTIVE_RX_WINDOW_S = 1.5
SILENCE_WINDOW_S = 0.35
SILENCE_REQUIRED_CODES = {
    "GETCO",
    "MODE",
    "FTD",
    "AVERAGE1",
    "AVERAGE2",
    "SENTEMP1",
    "SENTEMP2",
}
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


@dataclass(slots=True)
class _PendingCommand:
    envelope: CommandEnvelope
    expectation: str
    new_target_id: str | None = None

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
    def requires_silence(self) -> bool:
        if self.code in SILENCE_REQUIRED_CODES or self.is_senco_write:
            return True
        return self.code == "SETCOMWAY" and bool(self.args) and self.args[0] == "0"

    @property
    def is_getco(self) -> bool:
        return self.code == "GETCO"

    @property
    def allows_broadcast_ack(self) -> bool:
        return self.target_id == "FFF" and self.expectation == "ack"

    @property
    def requires_object_consistency(self) -> bool:
        return self.code in WRITE_OBJECT_GUARD_CODES or self.code.startswith(("SENCO", "CLEARSENCO"))


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
        self._logger: SessionLogger | None = None
        self._known_ids: set[str] = set()
        self._active_rx_entries: deque[tuple[float, str]] = deque()
        self._active_rx_cache: tuple[str, ...] = ()
        self._latest_rx_device_id: str | None = None
        self._last_alarm_bits: set[int] = set()
        self._last_frame: ParsedFrame | None = None
        self._metrics = MetricsTracker()
        self._last_metrics_emit_ts = 0.0
        self._next_poll_ts = 0.0
        self._pending_poll_deadline = 0.0
        self._pending_id_target: str | None = None

    @Slot(object)
    def open_session(self, config: SessionConfig) -> None:
        self.close_session()
        self._config = config
        self._metrics.update_expected_hz(config.stream_hz)
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
            self._partial = ""
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
        self._metrics.update_expected_hz(config.stream_hz)
        self._emit_info(
            f"会话配置已更新，目标设备 ID: {config.target_id}，解析模式 {config.mode_preference}，"
            f"会话模式: {config.session_mode}。"
        )

    @Slot()
    def initialize_capture(self) -> None:
        if not self._transport or not self._transport.is_open:
            self.error.emit("尚未连接设备。")
            return
        if self._config.listen_only:
            self.command_completed.emit(
                CommandResult(
                    timestamp=datetime.now(),
                    command="INIT",
                    ok=False,
                    message="当前为只监听模式，未发送初始化命令。",
                )
            )
            return

        timeout_ms = max(500, int(self._config.command_timeout_ms))
        sequence: list[tuple[str, str, int]] = []
        write_target = "FFF"
        if self._config.mode_preference == "MODE1":
            sequence.append((YGasProtocol.build_command("MODE", "1", target_id=write_target), "ack", timeout_ms))
        elif self._config.mode_preference == "MODE2":
            sequence.append((YGasProtocol.build_command("MODE", "2", target_id=write_target), "ack", timeout_ms))

        if self._config.acquisition_mode == "LISTEN":
            sequence.append((YGasProtocol.build_command("FTD", str(self._config.stream_hz), target_id=write_target), "ack", timeout_ms))
            sequence.append((YGasProtocol.build_command("SETCOMWAY", "1", target_id=write_target), "ack", timeout_ms))
        else:
            sequence.append((YGasProtocol.build_command("SETCOMWAY", "0", target_id=write_target), "ack", timeout_ms))

        for payload, expectation, payload_timeout in sequence:
            result = self._send_payload(payload, expectation=expectation, timeout_ms=payload_timeout)
            self.command_completed.emit(result)
            if not result.ok:
                return

    @Slot(str, str, int)
    def send_payload(self, payload: str, expectation: str, timeout_ms: int) -> None:
        result = self._send_payload(payload, expectation=expectation, timeout_ms=timeout_ms)
        self.command_completed.emit(result)

    def _send_payload(self, payload: str, *, expectation: str, timeout_ms: int) -> CommandResult:
        if not self._transport or not self._transport.is_open:
            return CommandResult(
                timestamp=datetime.now(),
                command=payload,
                ok=False,
                message="串口未连接。",
            )

        envelope = YGasProtocol.parse_command(payload)
        if envelope is None:
            return CommandResult(timestamp=datetime.now(), command=payload, ok=False, message="原始命令格式无法识别。")

        pending = _PendingCommand(
            envelope=envelope,
            expectation=expectation,
            new_target_id=envelope.args[0] if envelope.code == "ID" and envelope.args else None,
        )
        if pending.new_target_id:
            self._pending_id_target = pending.new_target_id

        mismatch = self._preflight_target_mismatch(pending)
        if mismatch:
            if pending.new_target_id:
                self._pending_id_target = None
            return CommandResult(timestamp=datetime.now(), command=payload, ok=False, message=mismatch)

        if pending.code == "SETCOMWAY" and pending.args[:1] == ["0"]:
            result = self._write_and_wait(payload, pending, timeout_ms=timeout_ms)
            if not result.ok:
                if "超时" in result.message:
                    result.message = "未收到 SETCOMWAY ACK。"
                if pending.new_target_id:
                    self._pending_id_target = None
                return result
            silence_error = self._observe_silence_failure()
            if silence_error:
                if pending.new_target_id:
                    self._pending_id_target = None
                return CommandResult(
                    timestamp=datetime.now(),
                    command=payload,
                    ok=False,
                    message=silence_error,
                    response_lines=result.response_lines,
                )
            self._flush_input_buffer()
            return CommandResult(
                timestamp=datetime.now(),
                command=payload,
                ok=True,
                message="ACK 成功，自动上传已关闭并确认进入静音。",
                response_lines=result.response_lines,
            )

        if pending.requires_silence:
            silence_result = self._ensure_write_silence(timeout_ms)
            if silence_result is not None:
                return CommandResult(
                    timestamp=datetime.now(),
                    command=payload,
                    ok=False,
                    message=silence_result.message,
                    response_lines=silence_result.response_lines,
                )
            mismatch = self._preflight_target_mismatch(pending)
            if mismatch:
                if pending.new_target_id:
                    self._pending_id_target = None
                return CommandResult(timestamp=datetime.now(), command=payload, ok=False, message=mismatch)

        result = self._write_and_wait(payload, pending, timeout_ms=timeout_ms)
        if pending.new_target_id and not result.ok and self._pending_id_target == pending.new_target_id:
            self._pending_id_target = None
        return result

    def _ensure_write_silence(self, timeout_ms: int) -> CommandResult | None:
        silence_payload = YGasProtocol.build_command("SETCOMWAY", "0", target_id="FFF")
        pending = _PendingCommand(
            envelope=YGasProtocol.parse_command(silence_payload) or CommandEnvelope("SETCOMWAY", "FFF", ["0"]),
            expectation="ack",
        )
        result = self._write_and_wait(silence_payload, pending, timeout_ms=timeout_ms)
        if not result.ok:
            message = "未收到 SETCOMWAY ACK。" if "超时" in result.message else result.message
            return CommandResult(timestamp=datetime.now(), command=silence_payload, ok=False, message=message, response_lines=result.response_lines)

        silence_error = self._observe_silence_failure()
        if silence_error:
            return CommandResult(
                timestamp=datetime.now(),
                command=silence_payload,
                ok=False,
                message=silence_error,
                response_lines=result.response_lines,
            )

        self._flush_input_buffer()
        return None

    def _write_and_wait(self, payload: str, pending: _PendingCommand, *, timeout_ms: int) -> CommandResult:
        try:
            self._transport.write_line(payload)
            self._record("TX", self._format_tx_record(payload))
        except Exception as exc:
            message = f"命令发送失败: {exc}"
            self._handle_transport_fault(message)
            self.error.emit(message)
            return CommandResult(timestamp=datetime.now(), command=payload, ok=False, message=message)

        if pending.expectation == "none":
            return CommandResult(timestamp=datetime.now(), command=payload, ok=True, message="命令已发送。")
        return self._wait_for_response(payload, pending=pending, timeout_ms=timeout_ms)

    def _wait_for_response(self, payload: str, *, pending: _PendingCommand, timeout_ms: int) -> CommandResult:
        deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
        lines_seen: list[str] = []
        mismatched_device_ids: set[str] = set()
        ack_seen = False

        while time.monotonic() < deadline:
            batch = self._read_batch()
            if batch:
                lines_seen.extend(batch)
                for line in batch:
                    frame = self._parse_frame_line(line)
                    if frame is not None:
                        self._publish_frame(frame)

                    outcome = self._parse_expected_response_structured(line, pending)
                    if outcome == "ack_seen":
                        ack_seen = True
                        continue
                    if isinstance(outcome, tuple) and outcome[0] == "mismatch":
                        mismatched_device_ids.add(outcome[1])
                        continue
                    if isinstance(outcome, tuple) and outcome[0] == "result":
                        parsed = outcome[1]
                        return CommandResult(
                            timestamp=datetime.now(),
                            command=payload,
                            ok=parsed.ok,
                            message=parsed.message,
                            response_lines=lines_seen[:],
                            response_kind=parsed.kind,
                            response_device_id=parsed.device_id,
                            parsed_payload=parsed.payload,
                        )
                    if pending.expectation == "any":
                        return CommandResult(
                            timestamp=datetime.now(),
                            command=payload,
                            ok=True,
                            message="收到设备响应。",
                            response_lines=lines_seen[:],
                        )
            time.sleep(0.02)

        message = self._timeout_message_for(pending, ack_seen=ack_seen, mismatched_device_ids=mismatched_device_ids, timeout_ms=timeout_ms)
        self.error.emit(f"{payload} -> {message}")
        return CommandResult(
            timestamp=datetime.now(),
            command=payload,
            ok=False,
            message=message,
            response_lines=lines_seen,
        )

    def _parse_expected_response(self, line: str, pending: _PendingCommand) -> tuple[str, _ParsedResponse] | tuple[str, str] | str | None:
        ack = YGasProtocol.parse_ack(line)
        if ack is not None:
            self._track_rx_device(ack.device_id, update_latest=True, source="ACK")
            self._maybe_sync_target_from_rx(ack.device_id, source="ACK")
            if pending.expectation == "ack":
                if not self._matches_target(ack.device_id, pending.target_id, allow_broadcast_ack=pending.allows_broadcast_ack):
                    return ("mismatch", ack.device_id)
                if pending.new_target_id and ack.ok and ack.device_id == pending.new_target_id:
                    self._sync_target_to(pending.new_target_id, source="ACK")
                if not ack.ok:
                    return ("result", False, f"设备返回失败 ACK: {ack.detail or 'F'}")
                return ("result", True, "ACK 成功。")
            if pending.expectation == "coefficient":
                return "ack_seen"

        device_id = YGasProtocol.response_device_id(line, parse_mode=self._config.mode_preference)
        if device_id and not ack:
            self._track_rx_device(device_id, update_latest=False, source="REPLY")
            self._maybe_sync_target_from_rx(device_id, source="REPLY")

        if pending.expectation == "data":
            frame = self._parse_frame_line(line)
            if frame is not None:
                if not self._matches_target(frame.device_id or "", pending.target_id):
                    return ("mismatch", frame.device_id or "")
                return ("result", True, f"收到数据帧，MODE{frame.mode}。")

        if pending.expectation == "coefficient":
            coeff = YGasProtocol.parse_coefficient_reply(line)
            if coeff is not None:
                mismatch = self._preflight_target_mismatch(pending)
                if mismatch:
                    return ("result", False, mismatch)
                suggestion = self._coefficient_suggestion(coeff)
                message = f"收到系数响应: {coeff}"
                if suggestion:
                    message += f" | 建议写回格式: {suggestion}"
                return ("result", True, message)

        if pending.expectation == "serial_config":
            serial_cfg = YGasProtocol.parse_serial_config_reply(line)
            if serial_cfg is not None:
                if not self._matches_target(serial_cfg["device_id"], pending.target_id):
                    return ("mismatch", serial_cfg["device_id"])
                return (
                    "result",
                    True,
                    f"通信参数: {serial_cfg['baudrate']} / {serial_cfg['bytesize']} / {serial_cfg['parity']} / {serial_cfg['stopbits']}",
                )

        if pending.expectation == "mode_value":
            mode_value = YGasProtocol.parse_mode_value_reply(line)
            if mode_value is not None:
                if not self._matches_target(mode_value["device_id"], pending.target_id):
                    return ("mismatch", mode_value["device_id"])
                return ("result", True, f"当前工作模式: MODE{mode_value['mode']}")

        if pending.expectation == "identity":
            identity = YGasProtocol.parse_identity_reply(line)
            if identity is not None:
                if not self._matches_target(identity["device_id"], pending.target_id):
                    return ("mismatch", identity["device_id"])
                return ("result", True, f"当前设备 ID: {identity['device_id']}")

        if pending.expectation == "setting_value":
            value = YGasProtocol.parse_setting_value_reply(line)
            if value is not None:
                if not self._matches_target(value["device_id"], pending.target_id):
                    return ("mismatch", value["device_id"])
                return ("result", True, f"当前值: {value['value']}")

        return None

    def _parse_expected_response_structured(
        self,
        line: str,
        pending: _PendingCommand,
    ) -> tuple[str, _ParsedResponse] | tuple[str, str] | str | None:
        ack = YGasProtocol.parse_ack(line)
        if ack is not None:
            self._track_rx_device(ack.device_id, update_latest=True, source="ACK")
            self._maybe_sync_target_from_rx(ack.device_id, source="ACK")
            if pending.expectation == "ack":
                if not self._matches_target(ack.device_id, pending.target_id, allow_broadcast_ack=pending.allows_broadcast_ack):
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
                        message="ACK 成功。",
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
                if not self._matches_target(frame.device_id or "", pending.target_id):
                    return ("mismatch", frame.device_id or "")
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
                if not self._matches_target(serial_cfg["device_id"], pending.target_id):
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
                if not self._matches_target(mode_value["device_id"], pending.target_id):
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
                if not self._matches_target(identity["device_id"], pending.target_id):
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
                if not self._matches_target(value["device_id"], pending.target_id):
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

            latest_frame: ParsedFrame | None = None
            for line in self._read_batch():
                frame = self._parse_frame_line(line)
                if frame is not None:
                    latest_frame = frame
                    self._pending_poll_deadline = 0.0
            if latest_frame is not None:
                self._publish_frame(latest_frame)

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
        text = chunk.decode("ascii", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
        self._partial += text
        parts = self._partial.split("\n")
        self._partial = parts.pop() if parts else ""
        lines = [line.strip() for line in parts if line.strip()]
        for line in lines:
            self._record("RX", line)
        return lines

    def _publish_frame(self, frame: ParsedFrame) -> None:
        self._last_frame = frame
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

    def _observe_silence_failure(self) -> str | None:
        deadline = time.monotonic() + SILENCE_WINDOW_S
        stream_lines: list[str] = []
        while time.monotonic() < deadline:
            batch = self._read_batch()
            for line in batch:
                frame = self._parse_frame_line(line)
                if frame is not None:
                    self._publish_frame(frame)
                    if frame.mode == 2:
                        stream_lines.append(line)
            if stream_lines:
                return "收到 SETCOMWAY ACK，但静音窗口仍有 MODE2 流帧，自动上传未真正关闭。"
            time.sleep(0.02)
        return None

    def _flush_input_buffer(self) -> None:
        if self._transport is None:
            return
        try:
            self._transport.flush_input()
        except Exception:
            pass
        self._partial = ""

    def _timeout_message_for(
        self,
        pending: _PendingCommand,
        *,
        ack_seen: bool,
        mismatched_device_ids: set[str],
        timeout_ms: int,
    ) -> str:
        mismatch = self._preflight_target_mismatch(pending)
        if mismatch:
            return mismatch
        if mismatched_device_ids:
            online = ",".join(sorted(mismatched_device_ids))
            return (
                f"响应来自错误设备 ID: target={self._display_target_id(pending)}，online={online}，"
                "本次命令未判成功。"
            )
        if pending.is_getco and ack_seen:
            return "已静音，但 GETCO 只有 ACK、没有系数行。"
        return f"命令超时，未在 {timeout_ms} ms 内收到预期响应。"

    def _coefficient_suggestion(self, coeff: dict[str, float]) -> str:
        ordered = sorted(coeff.items(), key=lambda item: int(item[0][1:]))
        formatted: list[str] = []
        for _, value in ordered[:6]:
            formatted.append(normalize_senco_coefficient(str(value)))
        return ",".join(formatted)

    def _matches_target(self, response_device_id: str, target_id: str, *, allow_broadcast_ack: bool = False) -> bool:
        if not response_device_id:
            return False
        normalized_target = str(target_id or "").strip().upper()
        normalized_response = str(response_device_id or "").strip().upper()
        if normalized_target == "FFF":
            return allow_broadcast_ack
        return normalized_response == normalized_target

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

    def _handle_transport_fault(self, message: str) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self.connection_changed.emit(False, str(self._logger.path) if self._logger is not None else "")
        self._emit_info(message)

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
    _payload_requested = Signal(str, str, int)

    def __init__(self, session_name: str):
        super().__init__()
        self.session_name = session_name
        self.current_config = SessionConfig(session_name=session_name)
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

    def send_payload(self, payload: str, expectation: str = "ack", timeout_ms: int = 1500) -> None:
        self._payload_requested.emit(payload, expectation, timeout_ms)

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
