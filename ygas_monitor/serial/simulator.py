"""Standalone simulator for demo and parser verification without hardware."""

from __future__ import annotations

from collections import deque
from math import cos, sin
import time

from ..models import SerialSettings
from .transport import AbstractTransport, TransportError


class SimulatorTransport(AbstractTransport):
    def __init__(self, settings: SerialSettings):
        self.settings = settings
        self.device_id = "001"
        self.mode = 2
        self.active_send = True
        self.ftd_hz = 10
        self.timeout_value = 999
        self.setpow_mv = 2000
        self.setco2_mv = 3000
        self.sentemp_values = {"SENTEMP1": 22.5, "SENTEMP2": 22.5}
        self.average_windows = {"AVERAGE1": 49, "AVERAGE2": 49}
        self._coefficients: dict[int, list[float]] = {
            1: [65916.6, -106614.0, 57735.1, -10584.9, 0.0, 0.0],
            2: [0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        }
        self._opened = False
        self._start_ts = time.monotonic()
        self._last_stream_emit = self._start_ts
        self._read_buffer: deque[bytes] = deque()

    @property
    def is_open(self) -> bool:
        return self._opened

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._read_buffer.clear()

    def flush_input(self) -> None:
        self._read_buffer.clear()

    def write_line(self, text: str) -> None:
        if not self.is_open:
            raise TransportError("模拟器尚未打开。")
        response = self.process_command(text)
        if response:
            self._queue(response)

    def read_available(self) -> bytes:
        if not self.is_open:
            return b""
        self._maybe_emit_stream()
        if not self._read_buffer:
            return b""
        chunks = bytearray()
        while self._read_buffer:
            chunks.extend(self._read_buffer.popleft())
        return bytes(chunks)

    def process_command(self, text: str) -> str:
        command = str(text or "").strip().strip("<>")
        if not command:
            return ""
        parts = [item.strip() for item in command.split(",")]
        cmd = parts[0].upper()
        args = parts[3:]

        if cmd == "READDATA":
            return self._build_frame() + "\r\n"
        if cmd == "SETCOM":
            if args:
                try:
                    self.settings.baudrate = int(float(args[0]))
                    self.settings.bytesize = int(args[1])
                    self.settings.parity = args[2].upper()
                    self.settings.stopbits = float(args[3])
                except Exception:
                    return self._error_ack(-113)
                return self._ack()
            return f"<YGAS,{self.device_id},{self.settings.baudrate},{self.settings.bytesize},{self.settings.parity},{int(self.settings.stopbits)}>\r\n"
        if cmd == "SETCOMWAY":
            if args:
                self.active_send = bool(int(args[0]))
                return self._ack()
            return f"<YGAS,{self.device_id},{1 if self.active_send else 0}>\r\n"
        if cmd == "FTD":
            if args:
                self.ftd_hz = max(1, min(20, int(float(args[0]))))
                return self._ack()
            return f"<YGAS,{self.device_id},{self.ftd_hz}>\r\n"
        if cmd == "MODE":
            if args:
                requested = int(float(args[0]))
                if requested not in {1, 2}:
                    return self._error_ack(-113)
                self.mode = requested
                return self._ack()
            return f"<YGAS,{self.device_id},{self.mode}>\r\n"
        if cmd == "ID":
            if args:
                try:
                    self.device_id = f"{int(args[0]):03d}"
                except Exception:
                    return self._error_ack(-113)
                return self._ack()
            return f"<YGAS,{self.device_id}>\r\n"
        if cmd.startswith("AVERAGE"):
            if args:
                try:
                    self.average_windows[cmd] = max(1, min(399, int(float(args[0]))))
                except Exception:
                    return self._error_ack(-113)
                return self._ack()
            return f"<YGAS,{self.device_id},{self.average_windows.get(cmd, 49)}>\r\n"
        if cmd == "GETCO":
            index = int(args[0]) if args else 1
            coeffs = self._coefficients.get(index, [0.0, 1.0, 0.0, 0.0, 0.0, 0.0])
            items = [f"C{slot}:{value:.6g}" for slot, value in enumerate(coeffs)]
            return "<" + ",".join(items) + ">\r\n"
        if cmd.startswith("SENCO"):
            index = int(cmd.replace("SENCO", "") or 1)
            try:
                self._coefficients[index] = [float(arg) for arg in args] if args else []
            except Exception:
                return self._error_ack(-113)
            return self._ack()
        if cmd.startswith("CLEARSENCO"):
            index = int(cmd.replace("CLEARSENCO", "") or 1)
            self._coefficients[index] = []
            return self._ack()
        if cmd == "SETPOW":
            if args:
                self.setpow_mv = int(float(args[0]))
                return self._ack()
            return f"<YGAS,{self.device_id},{self.setpow_mv}>\r\n"
        if cmd == "SETILLUM":
            return self._ack()
        if cmd == "SETCO2":
            if args:
                self.setco2_mv = int(float(args[0]))
                return self._ack()
            return f"<YGAS,{self.device_id},{self.setco2_mv}>\r\n"
        if cmd == "TIMEOUT":
            if args:
                self.timeout_value = int(float(args[0]))
                return self._ack()
            return f"<YGAS,{self.device_id},{self.timeout_value}>\r\n"
        if cmd in {"SENTEMP1", "SENTEMP2"}:
            if args:
                self.sentemp_values[cmd] = float(args[0])
                return self._ack()
            return f"<YGAS,{self.device_id},{self.sentemp_values[cmd]:.1f}>\r\n"
        if cmd == "RESET":
            return self._ack()
        return self._error_ack(-113)

    def _maybe_emit_stream(self) -> None:
        if not self.active_send:
            return
        now = time.monotonic()
        period = 1.0 / max(1, self.ftd_hz)
        emit_count = int((now - self._last_stream_emit) / period)
        if emit_count <= 0:
            return
        for _ in range(min(emit_count, 8)):
            self._queue(self._build_frame() + "\r\n")
        self._last_stream_emit = now

    def _queue(self, payload: str) -> None:
        self._read_buffer.append(payload.encode("ascii", errors="ignore"))

    def _ack(self) -> str:
        return f"<YGAS,{self.device_id},T>\r\n"

    def _error_ack(self, code: int) -> str:
        return f"<YGAS,{self.device_id},F,{int(code)}>\r\n"

    def _build_frame(self) -> str:
        t = time.monotonic() - self._start_ts
        co2_ppm = 430.0 + 18.0 * sin(t * 0.8)
        h2o_mmol = 5.20 + 0.35 * cos(t * 0.5)
        co2_density = 850.0 + 24.0 * sin(t * 0.7)
        h2o_density = 4.10 + 0.22 * cos(t * 0.4)
        co2_ratio_f = 1.3030 + 0.005 * sin(t)
        co2_ratio_raw = co2_ratio_f + 0.0015 * cos(t * 1.9)
        h2o_ratio_f = 0.7888 + 0.004 * sin(t * 0.75)
        h2o_ratio_raw = h2o_ratio_f + 0.0012 * cos(t * 1.3)
        ref_signal = 33220 + 180 * sin(t * 1.1)
        co2_signal = 43560 + 220 * cos(t * 0.7)
        h2o_signal = 26310 + 140 * sin(t * 0.4)
        chamber_temp_c = 25.8 + 0.45 * sin(t * 0.3)
        case_temp_c = 26.5 + 0.35 * cos(t * 0.25)
        pressure_kpa = 101.25 + 0.18 * sin(t * 0.15)
        status = self._status_word(chamber_temp_c, ref_signal, co2_signal, h2o_signal)

        if self.mode == 1:
            checksum = int((co2_ppm * 10 + h2o_mmol * 1000) % 10000)
            return (
                f"YGAS,{self.device_id},{co2_ppm:07.3f},{h2o_mmol:06.3f},"
                f"{co2_ratio_f:0.2f},{h2o_ratio_f:0.2f},{chamber_temp_c:06.2f},"
                f"{pressure_kpa:06.2f},{status:04X},{checksum:04d}"
            )

        return (
            f"YGAS,{self.device_id},{co2_ppm:07.3f},{h2o_mmol:06.3f},{co2_density:08.3f},"
            f"{h2o_density:06.3f},{co2_ratio_f:0.4f},{co2_ratio_raw:0.4f},"
            f"{h2o_ratio_f:0.4f},{h2o_ratio_raw:0.4f},{int(ref_signal):05d},"
            f"{int(co2_signal):05d},{int(h2o_signal):05d},{chamber_temp_c:06.2f},"
            f"{case_temp_c:06.2f},{pressure_kpa:06.2f},{status:04X},SIM"
        )

    @staticmethod
    def _status_word(chamber_temp_c: float, ref_signal: float, co2_signal: float, h2o_signal: float) -> int:
        value = 0x0001
        if chamber_temp_c > 26.15:
            value |= 1 << 3
        if ref_signal > 33350:
            value |= 1 << 4
        if co2_signal < 43350:
            value |= 1 << 12
        if h2o_signal < 26220:
            value |= 1 << 13
        return value
