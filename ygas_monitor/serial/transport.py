"""Transport abstraction for real serial ports and simulation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import SerialSettings

try:
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover - optional dependency on test machines
    serial = None
    list_ports = None


class TransportError(RuntimeError):
    """Raised when a transport cannot be opened or used."""


class AbstractTransport(ABC):
    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def write_line(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_available(self) -> bytes:
        raise NotImplementedError

    @abstractmethod
    def flush_input(self) -> None:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_open(self) -> bool:
        raise NotImplementedError


def _describe_serial_exception(exc: Exception, action: str) -> str:
    text = str(exc).lower()
    if any(keyword in text for keyword in ["permission", "拒绝访问", "access is denied"]):
        return f"{action}失败：当前串口无权限访问。"
    if any(keyword in text for keyword in ["could not open port", "busy", "被占用"]):
        return f"{action}失败：串口可能被其他程序占用。"
    if any(keyword in text for keyword in ["file not found", "cannot find", "找不到"]):
        return f"{action}失败：串口不存在或设备已拔掉。"
    if any(keyword in text for keyword in ["device reports readiness to read but returned no data", "disconnected"]):
        return f"{action}失败：设备可能已断开。"
    return f"{action}失败: {exc}"


class SerialTransport(AbstractTransport):
    def __init__(self, settings: SerialSettings):
        self.settings = settings
        self._serial: Any | None = None

    @property
    def is_open(self) -> bool:
        return bool(self._serial and getattr(self._serial, "is_open", False))

    def open(self) -> None:
        if serial is None:
            raise TransportError("未安装 pyserial，无法连接真实串口。")
        try:
            self._serial = serial.Serial(
                port=self.settings.port,
                baudrate=self.settings.baudrate,
                bytesize=self.settings.bytesize,
                parity=self.settings.parity,
                stopbits=self.settings.stopbits,
                timeout=self.settings.timeout,
            )
        except Exception as exc:  # pragma: no cover - depends on OS/serial stack
            raise TransportError(_describe_serial_exception(exc, "打开串口")) from exc

    def close(self) -> None:
        if self._serial is None:
            return
        try:
            self._serial.close()
        finally:
            self._serial = None

    def write_line(self, text: str) -> None:
        if not self.is_open:
            raise TransportError("串口未连接。")
        payload = text if text.endswith("\r\n") else text + "\r\n"
        try:
            assert self._serial is not None
            self._serial.write(payload.encode("ascii", errors="ignore"))
        except Exception as exc:  # pragma: no cover - depends on OS/serial stack
            raise TransportError(_describe_serial_exception(exc, "串口写入")) from exc

    def read_available(self) -> bytes:
        if not self.is_open:
            return b""
        try:
            assert self._serial is not None
            waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
            if waiting > 0:
                return bytes(self._serial.read(waiting))
            return bytes(self._serial.read(1))
        except Exception as exc:  # pragma: no cover - depends on OS/serial stack
            raise TransportError(_describe_serial_exception(exc, "串口读取")) from exc

    def flush_input(self) -> None:
        if not self.is_open:
            return
        try:
            assert self._serial is not None
            self._serial.reset_input_buffer()
        except Exception as exc:  # pragma: no cover - depends on OS/serial stack
            raise TransportError(_describe_serial_exception(exc, "清空串口缓存")) from exc


def list_serial_ports() -> list[str]:
    ports = ["SIMULATOR"]
    if list_ports is None:
        return ports
    discovered = sorted(info.device for info in list_ports.comports())
    return ports + discovered


def create_transport(settings: SerialSettings) -> AbstractTransport:
    if str(settings.port).strip().upper() == "SIMULATOR":
        from .simulator import SimulatorTransport

        return SimulatorTransport(settings)
    return SerialTransport(settings)
