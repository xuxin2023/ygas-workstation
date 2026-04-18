"""Registry-driven command catalog for business-oriented workstation UI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import re
from typing import Any

from ..config import COMMAND_TEMPLATE_PATH, DATA_DIR, OLD_COMMAND_TEMPLATE_PATH, ensure_runtime_dirs
from ..protocols.profiles import get_profile
from ..protocols.ygas import YGasProtocol

BROADCAST_FORBIDDEN = "forbidden"
BROADCAST_EXPERT_ONLY = "expert_only"
BROADCAST_CALIBRATION_OR_EXPERT = "calibration_or_expert"
BROADCAST_ALLOWED_WITH_WARNING = "allowed_with_warning"

_TARGET_RE = re.compile(r"^(?:\d{3}|FFF)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CommandParameter:
    key: str
    label: str
    kind: str = "text"
    description: str = ""
    required: bool = True
    default: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = field(default_factory=list)
    placeholder: str = ""


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    command_id: str
    code: str
    display_name: str
    family: str
    purpose: str
    parameter_help: str
    response_help: str
    risk_level: str
    supported_modes: list[str]
    broadcast_policy: str
    return_type: str
    required_permission: str
    parameters: list[CommandParameter] = field(default_factory=list)
    builtin: bool = True

    @property
    def allow_broadcast(self) -> bool:
        return self.broadcast_policy != BROADCAST_FORBIDDEN

    def serialize(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["parameters"] = [asdict(item) for item in self.parameters]
        return payload


class CommandRegistry:
    RETURN_TYPES = {
        "ack",
        "data",
        "coefficient",
        "serial_config",
        "mode_value",
        "identity",
        "setting_value",
        "any",
        "none",
    }

    def __init__(self, template_path: Path | None = None):
        self.template_path = Path(template_path or COMMAND_TEMPLATE_PATH)
        self._definitions = self._build_builtin_catalog()
        self._definitions.update(self._load_custom_catalog())

    def all_commands(self, profile_name: str = "bench_default") -> list[CommandDefinition]:
        return sorted(
            (self._definition_for_profile(item, profile_name) for item in self._definitions.values()),
            key=lambda item: (item.family, item.display_name, item.command_id),
        )

    def families(self, profile_name: str = "bench_default") -> list[str]:
        return sorted({item.family for item in self.all_commands(profile_name)})

    def commands_by_family(self, family: str, profile_name: str = "bench_default") -> list[CommandDefinition]:
        return [item for item in self.all_commands(profile_name) if item.family == family]

    def get(self, command_id: str, profile_name: str = "bench_default") -> CommandDefinition:
        definition = self._definitions[command_id.upper()]
        return self._definition_for_profile(definition, profile_name)

    def upsert_custom_template(self, definition: CommandDefinition) -> None:
        custom_items = self._load_custom_catalog()
        normalized = replace(
            definition,
            command_id=definition.command_id.upper(),
            code=definition.code.upper(),
            builtin=False,
        )
        custom_items[normalized.command_id] = normalized
        self._save_custom_catalog(custom_items)
        self._definitions[normalized.command_id] = normalized

    def build_preview(
        self,
        command_id: str,
        target_id: str,
        values: dict[str, Any],
        *,
        profile_name: str = "bench_default",
    ) -> str:
        definition = self.get(command_id, profile_name)
        normalized_values = self.validate_parameters(definition, values)
        args = self._parameter_values(definition, normalized_values)
        return YGasProtocol.build_command(definition.code, *args, target_id=target_id)

    def validate_target(
        self,
        definition: CommandDefinition,
        target_id: str,
        *,
        permission_level: str = "READ_ONLY",
        broadcast_enabled: bool = False,
    ) -> tuple[bool, str]:
        normalized = str(target_id or "").strip().upper()
        if not normalized:
            return False, "目标设备 ID 不能为空。"
        if not _TARGET_RE.match(normalized):
            return False, "目标设备 ID 必须是 000-999 三位数字，或 FFF。"
        if normalized != "FFF":
            return True, ""
        if not broadcast_enabled:
            return False, "当前目标地址为 FFF，但尚未显式启用广播开关。"
        if definition.broadcast_policy == BROADCAST_FORBIDDEN:
            return False, f"{definition.display_name} 不允许使用广播地址 FFF。"
        if definition.broadcast_policy == BROADCAST_EXPERT_ONLY and str(permission_level).upper() != "EXPERT":
            return False, f"{definition.display_name} 使用 FFF 仅允许专家模式。"
        if definition.broadcast_policy == BROADCAST_CALIBRATION_OR_EXPERT and str(permission_level).upper() not in {"CALIBRATION", "EXPERT"}:
            return False, f"{definition.display_name} 使用 FFF 仅允许校准模式或专家模式。"
        return True, ""

    def validate_parameters(self, definition: CommandDefinition, values: dict[str, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for param in definition.parameters:
            raw = values.get(param.key, param.default)
            text = str(raw).strip()
            if param.required and text == "":
                raise ValueError(f"{param.label} 不能为空。")
            if text == "":
                normalized[param.key] = ""
                continue

            if param.kind == "choice":
                if text not in param.choices:
                    raise ValueError(f"{param.label} 只能是 {', '.join(param.choices)}。")
                normalized[param.key] = text
                continue

            if param.kind == "device_id":
                if not re.fullmatch(r"\d{3}", text):
                    raise ValueError(f"{param.label} 必须是 000-999 三位数字。")
                normalized[param.key] = text
                continue

            if param.kind == "number":
                try:
                    numeric = float(text)
                except Exception as exc:
                    raise ValueError(f"{param.label} 必须是数值。") from exc
                if param.minimum is not None and numeric < param.minimum:
                    raise ValueError(f"{param.label} 不能小于 {param.minimum:g}。")
                if param.maximum is not None and numeric > param.maximum:
                    raise ValueError(f"{param.label} 不能大于 {param.maximum:g}。")
                normalized[param.key] = text
                continue

            normalized[param.key] = text
        return normalized

    def validate_command(
        self,
        command_id: str,
        target_id: str,
        values: dict[str, Any],
        *,
        profile_name: str = "bench_default",
        permission_level: str = "READ_ONLY",
        broadcast_enabled: bool = False,
    ) -> tuple[bool, str]:
        definition = self.get(command_id, profile_name)
        ok, reason = self.validate_target(
            definition,
            target_id,
            permission_level=permission_level,
            broadcast_enabled=broadcast_enabled,
        )
        if not ok:
            return False, reason
        try:
            self.validate_parameters(definition, values)
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def _parameter_values(self, definition: CommandDefinition, values: dict[str, Any]) -> list[str]:
        output: list[str] = []
        for param in definition.parameters:
            text = str(values.get(param.key, param.default)).strip()
            if text != "":
                output.append(text)
        return output

    def _definition_for_profile(self, definition: CommandDefinition, profile_name: str) -> CommandDefinition:
        if not definition.code.startswith("AVERAGE"):
            return definition

        profile = get_profile(profile_name)
        channel = profile.channel_name(definition.code)
        role = "读取" if definition.command_id.endswith("_QUERY") else "设置"
        purpose = (
            f"{role}{definition.code} 滤波参数。当前 profile 下，{definition.code} 对应 {channel} 通道。"
        )
        parameter_help = definition.parameter_help
        response_help = definition.response_help
        display_name = definition.display_name

        if definition.command_id.endswith("_QUERY"):
            display_name = f"读取 {channel} 滤波窗口（{definition.code}）"
            response_help = f"返回当前 {channel} 通道滤波窗口值。"
        else:
            display_name = f"设置 {channel} 滤波窗口（{definition.code}）"
            parameter_help = f"设置 {channel} 通道对应的 {definition.code} 滤波窗口，范围 1-399。"

        return replace(
            definition,
            display_name=display_name,
            purpose=purpose,
            parameter_help=parameter_help,
            response_help=response_help,
        )

    def _load_custom_catalog(self) -> dict[str, CommandDefinition]:
        ensure_runtime_dirs()
        source_path = self.template_path
        if not source_path.exists() and OLD_COMMAND_TEMPLATE_PATH.exists():
            source_path = OLD_COMMAND_TEMPLATE_PATH
        if not source_path.exists():
            return {}
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        items: dict[str, CommandDefinition] = {}
        for item in payload if isinstance(payload, list) else []:
            try:
                params = [CommandParameter(**param) for param in item.get("parameters", [])]
                broadcast_policy = str(item.get("broadcast_policy") or "").strip()
                if not broadcast_policy:
                    broadcast_policy = BROADCAST_ALLOWED_WITH_WARNING if item.get("allow_broadcast") else BROADCAST_FORBIDDEN
                definition = CommandDefinition(
                    command_id=str(item.get("command_id") or item["code"]).upper(),
                    code=str(item["code"]).upper(),
                    display_name=str(item["display_name"]),
                    family=str(item["family"]),
                    purpose=str(item["purpose"]),
                    parameter_help=str(item["parameter_help"]),
                    response_help=str(item["response_help"]),
                    risk_level=str(item["risk_level"]),
                    supported_modes=list(item.get("supported_modes", [])),
                    broadcast_policy=broadcast_policy,
                    return_type=str(item.get("return_type", "ack")),
                    required_permission=str(item.get("required_permission", "EXPERT")),
                    parameters=params,
                    builtin=False,
                )
                items[definition.command_id] = definition
            except Exception:
                continue
        return items

    def _save_custom_catalog(self, items: dict[str, CommandDefinition]) -> None:
        ensure_runtime_dirs()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = [item.serialize() for item in sorted(items.values(), key=lambda value: value.command_id)]
        self.template_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _build_builtin_catalog() -> dict[str, CommandDefinition]:
        defs: dict[str, CommandDefinition] = {}

        def add(definition: CommandDefinition) -> None:
            defs[definition.command_id] = definition

        def text_param(
            key: str,
            label: str,
            *,
            description: str = "",
            default: str = "",
            required: bool = True,
            placeholder: str = "",
            kind: str = "text",
            minimum: float | None = None,
            maximum: float | None = None,
            choices: list[str] | None = None,
        ) -> CommandParameter:
            return CommandParameter(
                key=key,
                label=label,
                kind=kind,
                description=description,
                default=default,
                required=required,
                placeholder=placeholder,
                minimum=minimum,
                maximum=maximum,
                choices=choices or [],
            )

        def make(
            command_id: str,
            code: str,
            display_name: str,
            family: str,
            purpose: str,
            parameter_help: str,
            response_help: str,
            risk_level: str,
            supported_modes: list[str],
            broadcast_policy: str,
            return_type: str,
            required_permission: str,
            parameters: list[CommandParameter] | None = None,
        ) -> CommandDefinition:
            return CommandDefinition(
                command_id=command_id.upper(),
                code=code.upper(),
                display_name=display_name,
                family=family,
                purpose=purpose,
                parameter_help=parameter_help,
                response_help=response_help,
                risk_level=risk_level,
                supported_modes=supported_modes,
                broadcast_policy=broadcast_policy,
                return_type=return_type,
                required_permission=required_permission,
                parameters=parameters or [],
                builtin=True,
            )

        add(
            make(
                "SETCOM",
                "SETCOM",
                "设置串口通信参数",
                "通信与连接",
                "写入设备串口参数。通常只在现场维护或设备地址整理时使用。",
                "波特率、数据位、校验位、停止位。",
                "成功返回 ACK，失败返回 F。",
                "high",
                ["MODE1", "MODE2"],
                BROADCAST_EXPERT_ONLY,
                "ack",
                "CONFIG",
                [
                    text_param("baudrate", "波特率", default="115200", kind="number", minimum=1200, maximum=115200),
                    text_param("bytesize", "数据位", default="8", kind="choice", choices=["7", "8"]),
                    text_param("parity", "校验位", default="N", kind="choice", choices=["N", "E", "O"]),
                    text_param("stopbits", "停止位", default="1", kind="choice", choices=["1", "2"]),
                ],
            )
        )
        add(
            make(
                "SETCOM_QUERY",
                "SETCOM",
                "读取通信参数",
                "通信与连接",
                "读取设备当前串口通信参数。",
                "无额外参数。",
                "返回波特率、数据位、校验位、停止位。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "serial_config",
                "READ_ONLY",
            )
        )
        add(
            make(
                "SETCOMWAY",
                "SETCOMWAY",
                "设置数据发送方式",
                "通信与连接",
                "切换主动发送或被动读取。",
                "1=主动发送，0=被动读取。",
                "成功返回 ACK。",
                "medium",
                ["MODE1", "MODE2"],
                BROADCAST_ALLOWED_WITH_WARNING,
                "ack",
                "CONFIG",
                [text_param("mode", "发送方式", default="1", kind="choice", choices=["0", "1"])],
            )
        )
        add(
            make(
                "FTD",
                "FTD",
                "设置主动发送频率",
                "通信与连接",
                "配置主动流模式下设备输出频率。",
                "1-20 Hz。",
                "成功返回 ACK。",
                "medium",
                ["MODE1", "MODE2"],
                BROADCAST_ALLOWED_WITH_WARNING,
                "ack",
                "CONFIG",
                [text_param("hz", "频率(Hz)", default="10", kind="number", minimum=1, maximum=20)],
            )
        )
        add(
            make(
                "FTD_QUERY",
                "FTD",
                "查询主动发送频率",
                "通信与连接",
                "读取设备当前主动发送频率。",
                "无额外参数。",
                "返回当前主动发送频率。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "READDATA",
                "READDATA",
                "读取最新数据",
                "通信与连接",
                "主动请求设备返回最新一帧数据。",
                "无额外参数。",
                "返回一帧实时数据。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "data",
                "READ_ONLY",
            )
        )
        add(
            make(
                "RESET",
                "RESET",
                "设备重启",
                "维护与恢复",
                "重启分析仪，用于异常恢复。",
                "无额外参数。",
                "成功返回 ACK，设备随后短暂掉线。",
                "critical",
                ["MODE1", "MODE2"],
                BROADCAST_EXPERT_ONLY,
                "ack",
                "EXPERT",
            )
        )
        add(
            make(
                "ID",
                "ID",
                "设置设备地址",
                "设备身份与模式",
                "修改设备 ID。",
                "三位数字地址。",
                "成功返回 ACK。",
                "high",
                ["MODE1", "MODE2"],
                BROADCAST_EXPERT_ONLY,
                "ack",
                "CONFIG",
                [text_param("new_id", "新设备 ID", default="001", kind="device_id", placeholder="000-999")],
            )
        )
        add(
            make(
                "ID_QUERY",
                "ID",
                "读取当前设备 ID",
                "设备身份与模式",
                "读取设备当前地址。",
                "无额外参数。",
                "返回当前设备 ID。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "identity",
                "READ_ONLY",
            )
        )
        add(
            make(
                "MODE",
                "MODE",
                "设置工作模式",
                "设备身份与模式",
                "切换正常模式、校准模式、工厂模式。",
                "1=正常模式，2=校准模式，3=工厂模式。",
                "成功返回 ACK。",
                "high",
                ["MODE1", "MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CONFIG",
                [text_param("mode", "工作模式", default="1", kind="choice", choices=["1", "2", "3"])],
            )
        )
        add(
            make(
                "MODE_QUERY",
                "MODE",
                "读取当前工作模式",
                "设备身份与模式",
                "读取设备当前工作模式。",
                "无额外参数。",
                "返回当前工作模式值。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "mode_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "SETPOW",
                "SETPOW",
                "设置发光功率",
                "光学与参考信号",
                "调整发光管功率电压。",
                "范围 0-3000 mV。",
                "成功返回 ACK。",
                "high",
                ["MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CALIBRATION",
                [text_param("millivolt", "功率电压(mV)", default="2000", kind="number", minimum=0, maximum=3000)],
            )
        )
        add(
            make(
                "SETPOW_QUERY",
                "SETPOW",
                "读取发光功率",
                "光学与参考信号",
                "读取当前发光功率设定值。",
                "无额外参数。",
                "返回当前发光功率值。",
                "low",
                ["MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "SETILLUM",
                "SETILLUM",
                "记录参考信号满值",
                "光学与参考信号",
                "将当前参考电压设置为参考信号满值。",
                "无额外参数。",
                "成功返回 ACK。",
                "high",
                ["MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CALIBRATION",
            )
        )
        add(
            make(
                "SETCO2",
                "SETCO2",
                "设置参考信号满值",
                "光学与参考信号",
                "设置参考信号标定满值。",
                "范围 100-4000 mV。",
                "成功返回 ACK。",
                "high",
                ["MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CALIBRATION",
                [text_param("millivolt", "参考满值(mV)", default="3000", kind="number", minimum=100, maximum=4000)],
            )
        )
        add(
            make(
                "SETCO2_QUERY",
                "SETCO2",
                "读取参考满值",
                "光学与参考信号",
                "读取当前参考信号满值。",
                "无额外参数。",
                "返回当前参考满值。",
                "low",
                ["MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "TIMEOUT",
                "TIMEOUT",
                "设置定时器补偿",
                "时间与温度相关参数",
                "调整数据发送时间间隔补偿。",
                "范围 900-1100。",
                "成功返回 ACK。",
                "medium",
                ["MODE1", "MODE2"],
                BROADCAST_EXPERT_ONLY,
                "ack",
                "CONFIG",
                [text_param("counter", "计数器", default="999", kind="number", minimum=900, maximum=1100)],
            )
        )
        add(
            make(
                "TIMEOUT_QUERY",
                "TIMEOUT",
                "读取 TIMEOUT",
                "时间与温度相关参数",
                "读取当前 TIMEOUT 补偿值。",
                "无额外参数。",
                "返回当前 TIMEOUT 值。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "GETCO",
                "GETCO",
                "读取标定系数",
                "校准系数中心",
                "读取指定组系数。",
                "系数组编号 1-9。",
                "返回 C0/C1... 系数列表。",
                "low",
                ["MODE2"],
                BROADCAST_FORBIDDEN,
                "coefficient",
                "READ_ONLY",
                [text_param("index", "系数组编号", default="1", kind="number", minimum=1, maximum=9)],
            )
        )
        for index in range(1, 10):
            add(
                make(
                    f"SENCO{index}",
                    f"SENCO{index}",
                    f"写入系数组 {index}",
                    "校准系数中心",
                    f"写入 SENCO{index} 标定系数。",
                    "支持 1-6 个系数，多个值用英文逗号分隔。",
                    "成功返回 ACK。",
                    "critical",
                    ["MODE2"],
                    BROADCAST_CALIBRATION_OR_EXPERT,
                    "ack",
                    "CALIBRATION",
                    [
                        text_param(
                            "coefficients",
                            "系数列表",
                            kind="text",
                            placeholder="如 1.0,0.0,0.0,0.0,0.0,0.0",
                            description="多个系数使用英文逗号分隔。",
                        )
                    ],
                )
            )
            add(
                make(
                    f"CLEARSENCO{index}",
                    f"CLEARSENCO{index}",
                    f"清空系数组 {index}",
                    "校准系数中心",
                    f"清空 SENCO{index} 中的全部系数。",
                    "无额外参数。",
                    "成功返回 ACK。",
                    "critical",
                    ["MODE2"],
                    BROADCAST_CALIBRATION_OR_EXPERT,
                    "ack",
                    "CALIBRATION",
                )
            )
        add(
            make(
                "SENTEMP1",
                "SENTEMP1",
                "设置 CO2 校准温度",
                "时间与温度相关参数",
                "设置 CO2 校准环境温度点。",
                "-20 到 40 摄氏度。",
                "成功返回 ACK。",
                "medium",
                ["MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CALIBRATION",
                [text_param("temp_c", "温度(℃)", default="22.5", kind="number", minimum=-20, maximum=40)],
            )
        )
        add(
            make(
                "SENTEMP1_QUERY",
                "SENTEMP1",
                "读取 CO2 校准温度",
                "时间与温度相关参数",
                "读取 CO2 校准环境温度点。",
                "无额外参数。",
                "返回当前 CO2 校准温度值。",
                "low",
                ["MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "SENTEMP2",
                "SENTEMP2",
                "设置 H2O 校准温度",
                "时间与温度相关参数",
                "设置 H2O 校准环境温度点。",
                "-20 到 40 摄氏度。",
                "成功返回 ACK。",
                "medium",
                ["MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CALIBRATION",
                [text_param("temp_c", "温度(℃)", default="22.5", kind="number", minimum=-20, maximum=40)],
            )
        )
        add(
            make(
                "SENTEMP2_QUERY",
                "SENTEMP2",
                "读取 H2O 校准温度",
                "时间与温度相关参数",
                "读取 H2O 校准环境温度点。",
                "无额外参数。",
                "返回当前 H2O 校准温度值。",
                "low",
                ["MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "AVERAGE1",
                "AVERAGE1",
                "设置滤波通道 1",
                "数据处理与滤波",
                "设置 AVERAGE1 窗口，通道含义由 profile 决定。",
                "设置 AVERAGE1 滤波窗口，范围 1-399。",
                "成功返回 ACK。",
                "medium",
                ["MODE1", "MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CONFIG",
                [text_param("window", "窗口大小", default="49", kind="number", minimum=1, maximum=399)],
            )
        )
        add(
            make(
                "AVERAGE1_QUERY",
                "AVERAGE1",
                "读取滤波通道 1",
                "数据处理与滤波",
                "读取 AVERAGE1 窗口，通道含义由 profile 决定。",
                "无额外参数。",
                "返回 AVERAGE1 当前值。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        add(
            make(
                "AVERAGE2",
                "AVERAGE2",
                "设置滤波通道 2",
                "数据处理与滤波",
                "设置 AVERAGE2 窗口，通道含义由 profile 决定。",
                "设置 AVERAGE2 滤波窗口，范围 1-399。",
                "成功返回 ACK。",
                "medium",
                ["MODE1", "MODE2"],
                BROADCAST_CALIBRATION_OR_EXPERT,
                "ack",
                "CONFIG",
                [text_param("window", "窗口大小", default="49", kind="number", minimum=1, maximum=399)],
            )
        )
        add(
            make(
                "AVERAGE2_QUERY",
                "AVERAGE2",
                "读取滤波通道 2",
                "数据处理与滤波",
                "读取 AVERAGE2 窗口，通道含义由 profile 决定。",
                "无额外参数。",
                "返回 AVERAGE2 当前值。",
                "low",
                ["MODE1", "MODE2"],
                BROADCAST_FORBIDDEN,
                "setting_value",
                "READ_ONLY",
            )
        )
        return defs
