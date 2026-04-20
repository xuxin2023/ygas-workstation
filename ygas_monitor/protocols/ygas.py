"""Independent YGAS protocol parser and command builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from ..models import ParsedFrame, StatusBitState

MODE2_KEYS = [
    "co2_ppm",
    "h2o_mmol",
    "co2_density",
    "h2o_density",
    "co2_ratio_f",
    "co2_ratio_raw",
    "h2o_ratio_f",
    "h2o_ratio_raw",
    "ref_signal",
    "co2_signal",
    "h2o_signal",
    "chamber_temp_c",
    "case_temp_c",
    "pressure_kpa",
]

PARSE_MODE_AUTO = "AUTO"
PARSE_MODE_FORCE_MODE1 = "FORCE_MODE1"
PARSE_MODE_FORCE_MODE2 = "FORCE_MODE2"

STATUS_BIT_DEFINITIONS: dict[int, tuple[str, str, bool]] = {
    0: ("系统运行", "1=运行正常，0=未运行或停机。", False),
    1: ("数据异常", "数据采样或计算结果异常。", True),
    2: ("电机转速异常", "电机转速超限或失步。", True),
    3: ("温度异常", "腔室或壳体温度超限。", True),
    4: ("光功率偏高", "发光管功率偏高。", True),
    5: ("光功率偏低", "发光管功率偏低。", True),
    6: ("光电流异常", "光电流信号异常。", True),
    7: ("脉冲不同步", "同步脉冲异常。", True),
    8: ("CO2 信号超标", "CO2 通道信号超上限。", True),
    9: ("H2O 信号超标", "H2O 通道信号超上限。", True),
    10: ("CO2 变化量超标", "CO2 变化率异常。", True),
    11: ("H2O 变化量超标", "H2O 变化率异常。", True),
    12: ("CO2 信号偏低", "CO2 通道信号偏低。", True),
    13: ("H2O 信号偏低", "H2O 通道信号偏低。", True),
}

_ACK_RE = re.compile(r"YGAS\s*,\s*(?P<id>[0-9A-Z]{3})\s*,\s*(?P<flag>T|F)(?:\s*,\s*(?P<detail>.*))?$", re.IGNORECASE)
_COEFF_RE = re.compile(
    r"C(?P<index>\d+)\s*:\s*(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)",
    re.IGNORECASE,
)
_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_STATUS_RE = re.compile(r"^[0-9A-F]{1,4}$", re.IGNORECASE)
_DEVICE_RE = re.compile(r"^(?:\d{3}|FFF)$", re.IGNORECASE)


@dataclass(slots=True)
class AckResult:
    device_id: str
    ok: bool
    detail: str | None = None


@dataclass(slots=True)
class CommandEnvelope:
    code: str
    target_id: str
    args: list[str]


class StreamBuffer:
    """Accumulates byte chunks and emits complete CR/LF-delimited lines."""

    def __init__(self) -> None:
        self._buffer = ""

    def clear(self) -> None:
        self._buffer = ""

    def feed(self, chunk: bytes | str | None) -> list[str]:
        if chunk is None:
            return []
        if isinstance(chunk, bytes):
            text = chunk.decode("ascii", errors="ignore")
        else:
            text = str(chunk)
        if not text:
            return []
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        parts = self._buffer.split("\n")
        self._buffer = parts.pop() if parts else ""
        return [part.strip() for part in parts if part.strip()]


class YGasProtocol:
    """Pure protocol helper, independent from transport and UI."""

    @staticmethod
    def normalize_parse_mode(value: Any) -> str:
        text = str(value or "").strip().upper()
        if text in {"MODE1", PARSE_MODE_FORCE_MODE1}:
            return PARSE_MODE_FORCE_MODE1
        if text in {"MODE2", PARSE_MODE_FORCE_MODE2}:
            return PARSE_MODE_FORCE_MODE2
        return PARSE_MODE_AUTO

    @staticmethod
    def build_command(command: str, *args: Any, target_id: str = "FFF") -> str:
        items = [str(command).strip().upper(), "YGAS", str(target_id).strip().upper()]
        items.extend(str(arg).strip() for arg in args if str(arg).strip() != "")
        return ",".join(items)

    @staticmethod
    def parse_command(payload: str) -> CommandEnvelope | None:
        parts = YGasProtocol._split_parts(YGasProtocol._clean_wrappers(payload))
        if len(parts) < 3 or parts[1].upper() != "YGAS" or not _DEVICE_RE.match(parts[2]):
            return None
        return CommandEnvelope(
            code=parts[0].upper(),
            target_id=parts[2].upper(),
            args=parts[3:],
        )

    @staticmethod
    def split_stream_lines(raw: bytes | str | list[Any] | tuple[Any, ...] | None) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            lines: list[str] = []
            for item in raw:
                lines.extend(YGasProtocol.split_stream_lines(item))
            return lines
        if isinstance(raw, bytes):
            text = raw.decode("ascii", errors="ignore")
        else:
            text = str(raw)
        return [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]

    @staticmethod
    def is_ack(line: str) -> bool:
        return YGasProtocol.parse_ack(line) is not None

    @staticmethod
    def parse_ack(line: str) -> AckResult | None:
        text = YGasProtocol._clean_wrappers(line)
        match = _ACK_RE.match(text)
        if not match:
            return None
        return AckResult(
            device_id=match.group("id").upper(),
            ok=match.group("flag").upper() == "T",
            detail=(match.group("detail") or "").strip() or None,
        )

    @staticmethod
    def parse_coefficient_reply(line: str) -> dict[str, float] | None:
        text = YGasProtocol._clean_wrappers(line)
        matches = list(_COEFF_RE.finditer(text))
        if not matches:
            return None
        result: dict[str, float] = {}
        for match in matches:
            result[f"C{int(match.group('index'))}"] = float(match.group("value"))
        return result

    @staticmethod
    def parse_identity_reply(line: str) -> dict[str, str] | None:
        parts = YGasProtocol._split_parts(YGasProtocol._clean_wrappers(line))
        if len(parts) != 2 or "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None
        return {"device_id": parts[1].upper()}

    @staticmethod
    def parse_mode_value_reply(line: str) -> dict[str, Any] | None:
        parts = YGasProtocol._split_parts(YGasProtocol._clean_wrappers(line))
        if len(parts) != 3 or "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None
        if parts[2] not in {"1", "2", "3"}:
            return None
        return {"device_id": parts[1].upper(), "mode": int(parts[2])}

    @staticmethod
    def parse_serial_config_reply(line: str) -> dict[str, Any] | None:
        parts = YGasProtocol._split_parts(YGasProtocol._clean_wrappers(line))
        if len(parts) != 6 or "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None
        if not parts[2].isdigit() or parts[3] not in {"7", "8"} or parts[4] not in {"N", "E", "O"} or parts[5] not in {"1", "2"}:
            return None
        return {
            "device_id": parts[1].upper(),
            "baudrate": int(parts[2]),
            "bytesize": int(parts[3]),
            "parity": parts[4],
            "stopbits": int(parts[5]),
        }

    @staticmethod
    def parse_setting_value_reply(line: str) -> dict[str, Any] | None:
        parts = YGasProtocol._split_parts(YGasProtocol._clean_wrappers(line))
        if len(parts) < 3 or "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None
        if YGasProtocol.parse_ack(line) or YGasProtocol.parse_mode_value_reply(line) or YGasProtocol.parse_serial_config_reply(line):
            return None
        if YGasProtocol.parse_line(line, parse_mode=PARSE_MODE_FORCE_MODE1) or YGasProtocol.parse_line(line, parse_mode=PARSE_MODE_FORCE_MODE2):
            return None
        values = parts[2:]
        return {
            "device_id": parts[1].upper(),
            "value": values[0] if len(values) == 1 else ",".join(values),
            "values": values,
        }

    @staticmethod
    def response_device_id(line: str, *, parse_mode: str = PARSE_MODE_AUTO) -> str | None:
        ack = YGasProtocol.parse_ack(line)
        if ack is not None:
            return ack.device_id

        for parser in (
            YGasProtocol.parse_identity_reply,
            YGasProtocol.parse_mode_value_reply,
            YGasProtocol.parse_serial_config_reply,
            YGasProtocol.parse_setting_value_reply,
        ):
            parsed = parser(line)
            if parsed is not None:
                return str(parsed.get("device_id") or "").upper() or None

        frame = YGasProtocol.parse_line(line, parse_mode=parse_mode)
        if frame is not None:
            return frame.device_id
        return None

    @staticmethod
    def parse_line(line: str, *, parse_mode: str = PARSE_MODE_AUTO) -> ParsedFrame | None:
        normalized_mode = YGasProtocol.normalize_parse_mode(parse_mode)
        for candidate in YGasProtocol._iter_frame_candidates(line):
            if normalized_mode == PARSE_MODE_FORCE_MODE1:
                parsed = YGasProtocol._parse_mode1(candidate, line)
                if parsed is not None:
                    return parsed
                continue
            if normalized_mode == PARSE_MODE_FORCE_MODE2:
                parsed = YGasProtocol._parse_mode2(candidate, line)
                if parsed is not None:
                    return parsed
                continue

            parsed = YGasProtocol._parse_mode2(candidate, line)
            if parsed is not None:
                return parsed
            parsed = YGasProtocol._parse_mode1(candidate, line)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def decode_status(status: str | None) -> list[StatusBitState]:
        text = str(status or "").strip()
        if not text or not _STATUS_RE.match(text):
            return []
        value = int(text, 16)
        states: list[StatusBitState] = []
        for bit, (label, description, is_alarm) in STATUS_BIT_DEFINITIONS.items():
            active = bool(value & (1 << bit))
            state_text = "正常"
            effective_alarm = bool(is_alarm and active)
            if bit == 0:
                state_text = "运行正常" if active else "未运行/停机"
                effective_alarm = False
            elif active:
                state_text = "告警"
            states.append(
                StatusBitState(
                    bit=bit,
                    label=label,
                    active=active,
                    is_alarm=effective_alarm,
                    state_text=state_text,
                    description=description,
                )
            )
        return states

    @staticmethod
    def status_numeric(status: str | None) -> int | None:
        text = str(status or "").strip()
        if not text or not _STATUS_RE.match(text):
            return None
        return int(text, 16)

    @staticmethod
    def _parse_mode2(candidate: str, raw: str) -> ParsedFrame | None:
        parts = YGasProtocol._split_parts(candidate)
        if len(parts) < 2 + len(MODE2_KEYS):
            return None
        if "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None

        values: dict[str, Any] = {}
        for index, key in enumerate(MODE2_KEYS, start=2):
            number = YGasProtocol._to_float(parts[index])
            if number is None:
                return None
            values[key] = number

        status = None
        extras_start = 2 + len(MODE2_KEYS)
        if len(parts) > extras_start and YGasProtocol._looks_like_status(parts[extras_start]):
            status = parts[extras_start].upper()
            extras_start += 1

        extras = [token for token in parts[extras_start:] if token]
        values["temperature_c"] = values.get("chamber_temp_c")
        values["active_alarm_count"] = sum(1 for item in YGasProtocol.decode_status(status) if item.is_alarm)
        values["status_numeric"] = YGasProtocol.status_numeric(status)

        return ParsedFrame(
            timestamp=datetime.now(),
            raw=str(raw).strip(),
            device_id=parts[1].upper(),
            mode=2,
            fields=values,
            status=status,
            extras=extras,
        )

    @staticmethod
    def _parse_mode1(candidate: str, raw: str) -> ParsedFrame | None:
        parts = YGasProtocol._split_parts(candidate)
        if len(parts) < 9:
            return None
        if "YGAS" not in parts[0].upper() or not _DEVICE_RE.match(parts[1]):
            return None
        if not YGasProtocol._looks_like_status(parts[8]):
            return None

        co2_ppm = YGasProtocol._to_float(parts[2])
        h2o_mmol = YGasProtocol._to_float(parts[3])
        if co2_ppm is None or h2o_mmol is None:
            return None

        status = parts[8].upper()
        checksum = parts[9] if len(parts) > 9 else None
        extras = [token for token in parts[10:] if token]
        values: dict[str, Any] = {
            "co2_ppm": co2_ppm,
            "h2o_mmol": h2o_mmol,
            "co2_signal": YGasProtocol._to_float(parts[4]) if len(parts) > 4 else None,
            "h2o_signal": YGasProtocol._to_float(parts[5]) if len(parts) > 5 else None,
            "temperature_c": YGasProtocol._to_float(parts[6]) if len(parts) > 6 else None,
            "pressure_kpa": YGasProtocol._to_float(parts[7]) if len(parts) > 7 else None,
            "checksum": checksum,
            "active_alarm_count": sum(1 for item in YGasProtocol.decode_status(status) if item.is_alarm),
            "status_numeric": YGasProtocol.status_numeric(status),
        }
        return ParsedFrame(
            timestamp=datetime.now(),
            raw=str(raw).strip(),
            device_id=parts[1].upper(),
            mode=1,
            fields=values,
            status=status,
            extras=extras,
        )

    @staticmethod
    def _iter_frame_candidates(line: str) -> list[str]:
        text = str(line or "").strip()
        if not text:
            return []
        candidates: list[str] = []
        seen: set[str] = set()
        upper = text.upper()
        for match in re.finditer(r"YGAS\s*,", upper):
            candidate = text[match.start() :].strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        if text not in seen:
            candidates.append(text)
        return candidates

    @staticmethod
    def _looks_like_status(token: str | None) -> bool:
        return bool(_STATUS_RE.match(str(token or "").strip()))

    @staticmethod
    def _split_parts(frame: str) -> list[str]:
        return [YGasProtocol._clean_token(part) for part in str(frame or "").split(",")]

    @staticmethod
    def _clean_wrappers(text: Any) -> str:
        return str(text or "").strip().strip("<>[](){} \t\r\n")

    @staticmethod
    def _clean_token(token: Any) -> str:
        text = str(token or "").strip()
        text = text.lstrip("<>[](){} \t\r\n")
        for marker in (">", "]", ")", "}", "\r", "\n"):
            if marker in text:
                text = text.split(marker, 1)[0]
        return text.strip().strip("<>[](){} \t\r\n")

    @staticmethod
    def _to_float(token: Any) -> float | None:
        text = str(token or "").strip()
        try:
            return float(text)
        except Exception:
            match = _FLOAT_RE.search(text)
            if not match:
                return None
            try:
                return float(match.group(0))
            except Exception:
                return None
