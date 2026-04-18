"""Qt-threaded acquisition service and UI-facing session controller."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import time
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..config import DEFAULT_HISTORY_SIZE
from ..models import AlarmEvent, CommandResult, ParsedFrame, RawFrameRecord, SessionConfig
from ..protocols.ygas import YGasProtocol
from ..serial.transport import AbstractTransport, create_transport
from .export_service import export_frames_to_csv
from .logging_service import SessionLogger
from .metrics import MetricsTracker, MonitoringMetrics


class AcquisitionWorker(QObject):
    frame_received = Signal(object)
    raw_received = Signal(object)
    alarm_emitted = Signal(object)
    metrics_updated = Signal(object)
    device_ids_updated = Signal(object)
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
        self._last_alarm_bits: set[int] = set()
        self._last_frame: ParsedFrame | None = None
        self._metrics = MetricsTracker()
        self._last_metrics_emit_ts = 0.0
        self._next_poll_ts = 0.0
        self._pending_poll_deadline = 0.0

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
            self._last_alarm_bits.clear()
            self._last_frame = None
            self._partial = ""
            self._next_poll_ts = time.monotonic()
            self._pending_poll_deadline = 0.0
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
            f"会话配置已更新，目标设备 ID: {config.target_id}，解析模式: {config.mode_preference}，"
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
        if self._config.mode_preference == "MODE1":
            sequence.append((YGasProtocol.build_command("MODE", "1", target_id=self._config.target_id), "ack", timeout_ms))
        elif self._config.mode_preference == "MODE2":
            sequence.append((YGasProtocol.build_command("MODE", "2", target_id=self._config.target_id), "ack", timeout_ms))

        if self._config.acquisition_mode == "LISTEN":
            sequence.append((YGasProtocol.build_command("SETCOMWAY", "1", target_id=self._config.target_id), "ack", timeout_ms))
            sequence.append((YGasProtocol.build_command("FTD", str(self._config.stream_hz), target_id=self._config.target_id), "ack", timeout_ms))
        else:
            sequence.append((YGasProtocol.build_command("SETCOMWAY", "0", target_id=self._config.target_id), "ack", timeout_ms))

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
        try:
            self._transport.write_line(payload)
            self._record("TX", self._format_tx_record(payload))
        except Exception as exc:
            message = f"命令发送失败: {exc}"
            self._handle_transport_fault(message)
            self.error.emit(message)
            return CommandResult(timestamp=datetime.now(), command=payload, ok=False, message=message)

        if expectation == "none":
            return CommandResult(timestamp=datetime.now(), command=payload, ok=True, message="命令已发送。")
        return self._wait_for_response(payload, expectation=expectation, timeout_ms=timeout_ms)

    def _wait_for_response(self, payload: str, *, expectation: str, timeout_ms: int) -> CommandResult:
        deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
        lines_seen: list[str] = []
        while time.monotonic() < deadline:
            batch = self._read_batch()
            if batch:
                lines_seen.extend(batch)
                for line in batch:
                    parsed = self._parse_expected_response(line, expectation)
                    frame = self._parse_frame_line(line)
                    if frame is not None:
                        self._publish_frame(frame)
                    if parsed is not None:
                        return CommandResult(
                            timestamp=datetime.now(),
                            command=payload,
                            ok=parsed["ok"],
                            message=parsed["message"],
                            response_lines=lines_seen[:],
                        )
                    if expectation == "any":
                        return CommandResult(
                            timestamp=datetime.now(),
                            command=payload,
                            ok=True,
                            message="收到设备响应。",
                            response_lines=lines_seen[:],
                        )
            time.sleep(0.02)

        message = f"命令超时，未在 {timeout_ms} ms 内收到预期响应。"
        self.error.emit(f"{payload} -> {message}")
        return CommandResult(
            timestamp=datetime.now(),
            command=payload,
            ok=False,
            message=message,
            response_lines=lines_seen,
        )

    def _parse_expected_response(self, line: str, expectation: str) -> dict[str, Any] | None:
        ack = YGasProtocol.parse_ack(line)
        if expectation == "ack" and ack is not None:
            return {
                "ok": ack.ok,
                "message": "ACK 成功。" if ack.ok else f"设备返回失败 ACK: {ack.detail or 'F'}",
            }

        if expectation == "data":
            frame = self._parse_frame_line(line)
            if frame is not None:
                return {"ok": True, "message": f"收到数据帧，MODE{frame.mode}。"}

        if expectation == "coefficient":
            coeff = YGasProtocol.parse_coefficient_reply(line)
            if coeff is not None:
                return {"ok": True, "message": f"收到系数响应: {coeff}"}

        if expectation == "serial_config":
            serial_cfg = YGasProtocol.parse_serial_config_reply(line)
            if serial_cfg is not None:
                return {
                    "ok": True,
                    "message": (
                        f"通信参数: {serial_cfg['baudrate']} / {serial_cfg['bytesize']} / "
                        f"{serial_cfg['parity']} / {serial_cfg['stopbits']}"
                    ),
                }

        if expectation == "mode_value":
            mode_value = YGasProtocol.parse_mode_value_reply(line)
            if mode_value is not None:
                return {"ok": True, "message": f"当前工作模式: MODE{mode_value['mode']}"}

        if expectation == "identity":
            identity = YGasProtocol.parse_identity_reply(line)
            if identity is not None:
                return {"ok": True, "message": f"当前设备 ID: {identity['device_id']}"}

        if expectation == "setting_value":
            value = YGasProtocol.parse_setting_value_reply(line)
            if value is not None:
                return {"ok": True, "message": f"当前值: {value['value']}"}

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
        parts = str(payload or "").split(",")
        target_id = parts[2].strip().upper() if len(parts) > 2 else "--"
        broadcast = "yes" if target_id == "FFF" else "no"
        prefix = "[FFF] " if target_id == "FFF" else ""
        return f"{prefix}{payload} | target={target_id} | broadcast={broadcast}"


class AnalyzerSessionController(QObject):
    frame_received = Signal(object)
    raw_received = Signal(object)
    alarm_emitted = Signal(object)
    metrics_updated = Signal(object)
    device_ids_updated = Signal(object)
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
    def _handle_command_completed(self, result: CommandResult) -> None:
        self.command_completed.emit(result)

    @Slot(str)
    def _handle_error(self, message: str) -> None:
        self.error.emit(message)

    @Slot(str)
    def _handle_info(self, message: str) -> None:
        self.info.emit(message)
