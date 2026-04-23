"""Shared auto-silence policy used by UI hints and controller behavior.

Historical naming is retained for compatibility. User-facing copy should
describe this behavior as temporarily pausing active upload.
"""

from __future__ import annotations

from typing import Mapping

from ..protocols.ygas import CommandEnvelope, YGasProtocol


class AutoSilenceTargetResolutionError(ValueError):
    """Raised when the policy cannot safely choose a single-device pause target."""


class AutoSilencePolicy:
    """Historical naming retained; user-facing copy means temporary active-upload pause."""

    # TODO: Migrate this internal historical name toward AutoUploadPausePolicy
    # while keeping a backward-compatible alias for existing integrations/tests.

    _WRITE_CODES = {
        "MODE",
        "FTD",
        "AVERAGE1",
        "AVERAGE2",
        "SENTEMP1",
        "SENTEMP2",
    }
    _QUIET_READ_CODES = {
        "GETCO",
        "MODE",
        "FTD",
        "AVERAGE1",
        "AVERAGE2",
        "SENTEMP1",
        "SENTEMP2",
    }
    _WRITE_PREFIXES = ("SENCO", "CLEARSENCO")
    _SILENCE_TARGET_ID = "FFF"
    _QUERY_CODE_MAP = {
        "MODE_QUERY": "MODE",
        "FTD_QUERY": "FTD",
        "SENTEMP1_QUERY": "SENTEMP1",
        "SENTEMP2_QUERY": "SENTEMP2",
        "AVERAGE1_QUERY": "AVERAGE1",
        "AVERAGE2_QUERY": "AVERAGE2",
    }
    _TARGET_RESOLUTION_BLOCK_TEXT = "无法确定暂停目标，系统不会自动使用 FFF 广播暂停主动上传。"

    @classmethod
    def requires_pre_silence_for_write(
        cls,
        command_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
    ) -> bool:
        normalized = cls._normalized_code(command_id, envelope)
        args = cls._args(values, envelope)
        if cls.is_silence_command(command_id, values, envelope):
            return False
        if normalized.startswith(cls._WRITE_PREFIXES):
            return bool(args)
        return normalized in cls._WRITE_CODES and bool(args)

    @classmethod
    def requires_quiet_window_for_read(
        cls,
        command_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
    ) -> bool:
        normalized = cls._normalized_code(command_id, envelope)
        args = cls._args(values, envelope)
        if normalized not in cls._QUIET_READ_CODES:
            return False
        if normalized != "GETCO" and args:
            return False
        value_map = cls._value_map(values)
        return str(value_map.get("quiet_read", "")).strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def is_silence_command(
        cls,
        command_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
    ) -> bool:
        normalized = cls._normalized_code(command_id, envelope)
        args = cls._args(values, envelope)
        return normalized == "SETCOMWAY" and args[:1] == ["0"]

    @classmethod
    def describe_pre_silence(
        cls,
        command_id: str,
        target_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
        *,
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        expected_device_id: str = "",
        allow_broadcast: bool = False,
    ) -> str:
        if not cls.requires_pre_silence_for_write(command_id, values, envelope):
            return ""
        try:
            pause_target = cls.silence_target_id(
                target_id_policy=target_id,
                expected_device_id=expected_device_id,
                active_device_ids=active_device_ids,
                allow_broadcast=allow_broadcast,
            )
        except AutoSilenceTargetResolutionError:
            return cls._TARGET_RESOLUTION_BLOCK_TEXT
        return (
            "为避免主动上传数据干扰 ACK/读回判断，系统将先发送 SETCOMWAY=0，进入命令复核窗口，再执行当前命令。"
            "该动作会写入设备并记录日志。"
            f" {cls._pause_target_text(pause_target)}"
        )

    @classmethod
    def describe_self_silence(
        cls,
        command_id: str,
        target_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
    ) -> str:
        if not cls.is_silence_command(command_id, values, envelope):
            return ""
        return (
            "本次命令会关闭主动上传。成功后设备将停止主动上报实时数据，进入被动读取/等待命令状态。"
            f" 当前目标：{cls._disable_upload_target_text(target_id)}。"
        )

    @classmethod
    def describe_quiet_read(
        cls,
        command_id: str,
        target_id: str,
        values: Mapping[str, object] | None = None,
        envelope: CommandEnvelope | None = None,
        *,
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        expected_device_id: str = "",
        allow_broadcast: bool = False,
    ) -> str:
        if not cls.requires_quiet_window_for_read(command_id, values, envelope):
            return ""
        try:
            pause_target = cls.silence_target_id(
                target_id_policy=target_id,
                expected_device_id=expected_device_id,
                active_device_ids=active_device_ids,
                allow_broadcast=allow_broadcast,
            )
        except AutoSilenceTargetResolutionError:
            return cls._TARGET_RESOLUTION_BLOCK_TEXT
        return (
            "本次读取需要临时暂停主动上传；仅在你确认后才会发送 SETCOMWAY=0，并会记录系统动作。"
            f" {cls._pause_target_text(pause_target)}"
        )

    @classmethod
    def silence_payload(
        cls,
        target_id_policy: str | None = None,
        *,
        expected_device_id: str = "",
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        allow_broadcast: bool = False,
    ) -> str:
        return YGasProtocol.build_command(
            "SETCOMWAY",
            "0",
            target_id=cls._silence_target_id(
                target_id_policy,
                expected_device_id=expected_device_id,
                active_device_ids=active_device_ids,
                allow_broadcast=allow_broadcast,
            ),
        )

    @classmethod
    def restore_payload(
        cls,
        target_id_policy: str | None = None,
        *,
        expected_device_id: str = "",
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        allow_broadcast: bool = False,
    ) -> str:
        return YGasProtocol.build_command(
            "SETCOMWAY",
            "1",
            target_id=cls._silence_target_id(
                target_id_policy,
                expected_device_id=expected_device_id,
                active_device_ids=active_device_ids,
                allow_broadcast=allow_broadcast,
            ),
        )

    @classmethod
    def silence_target_id(
        cls,
        target_id_policy: str | None = None,
        *,
        expected_device_id: str = "",
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        allow_broadcast: bool = False,
    ) -> str:
        return cls._silence_target_id(
            target_id_policy,
            expected_device_id=expected_device_id,
            active_device_ids=active_device_ids,
            allow_broadcast=allow_broadcast,
        )

    @classmethod
    def effective_scope(cls, target_id: str) -> str:
        return "broadcast" if str(target_id or "").strip().upper() == "FFF" else "single"

    @classmethod
    def _silence_target_id(
        cls,
        target_id_policy: str | None = None,
        *,
        expected_device_id: str = "",
        active_device_ids: list[str] | tuple[str, ...] | None = None,
        allow_broadcast: bool = False,
    ) -> str:
        normalized_target = str(target_id_policy or "").strip().upper()
        normalized_expected = str(expected_device_id or "").strip().upper()
        active_numeric = sorted(
            {
                str(item).strip().upper()
                for item in list(active_device_ids or [])
                if str(item).strip().isdigit()
            }
        )
        if normalized_target.isdigit():
            return normalized_target
        if allow_broadcast:
            return cls._SILENCE_TARGET_ID
        if normalized_expected.isdigit():
            return normalized_expected
        if len(active_numeric) == 1:
            return active_numeric[0]
        raise AutoSilenceTargetResolutionError(cls._TARGET_RESOLUTION_BLOCK_TEXT)

    @staticmethod
    def _normalized_code(command_id: str, envelope: CommandEnvelope | None) -> str:
        if envelope is not None:
            return str(envelope.code or "").strip().upper()
        normalized = str(command_id or "").strip().upper()
        return AutoSilencePolicy._QUERY_CODE_MAP.get(normalized, normalized)

    @staticmethod
    def _value_map(values: Mapping[str, object] | None) -> dict[str, str]:
        return {str(key): str(value) for key, value in dict(values or {}).items()}

    @classmethod
    def _args(cls, values: Mapping[str, object] | None, envelope: CommandEnvelope | None) -> list[str]:
        if envelope is not None:
            return list(envelope.args)
        value_map = cls._value_map(values)
        if "mode" in value_map:
            return [value_map["mode"]]
        if "hz" in value_map:
            return [value_map["hz"]]
        if "coefficients" in value_map:
            return [value_map["coefficients"]]
        return [value for key, value in value_map.items() if key != "quiet_read"]

    @classmethod
    def _pause_target_text(cls, target_id: str) -> str:
        normalized = str(target_id or "").strip().upper()
        if normalized == cls._SILENCE_TARGET_ID:
            return "暂停目标：FFF 广播，会影响总线上所有响应设备。"
        return f"暂停目标：设备 {normalized}。"

    @classmethod
    def _disable_upload_target_text(cls, target_id: str) -> str:
        normalized = str(target_id or "").strip().upper()
        if normalized == cls._SILENCE_TARGET_ID:
            return "FFF 广播关闭主动上传，会影响总线上所有响应设备"
        return f"设备 {normalized} 关闭主动上传"
