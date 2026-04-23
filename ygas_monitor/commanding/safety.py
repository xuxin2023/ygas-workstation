"""Session-mode and read-only safety helpers."""

from __future__ import annotations

from .registry import CommandDefinition

SESSION_MODE_LISTEN_ONLY = "LISTEN_ONLY"
SESSION_MODE_SAFE_HANDSHAKE = "SAFE_HANDSHAKE"
SESSION_MODE_MONITORING = SESSION_MODE_SAFE_HANDSHAKE
SESSION_MODE_ENGINEERING = "ENGINEERING"
SESSION_MODE_REPLAY = "REPLAY"

SAFE_QUERY_COMMAND_IDS = ["ID_QUERY", "MODE_QUERY", "FTD_QUERY", "SETCOM_QUERY"]


def is_read_only_command(definition: CommandDefinition) -> bool:
    command_id = definition.command_id.upper()
    if command_id.endswith("_QUERY"):
        return True
    return command_id in {"READDATA", "GETCO"}


def can_execute_command(
    definition: CommandDefinition,
    *,
    connected: bool,
    session_mode: str,
    read_only_lock: bool,
    replay_running: bool,
) -> tuple[bool, str]:
    if replay_running:
        return False, "回放期间禁止向真实设备发送命令。"
    if not connected:
        return False, "当前未连接设备。"
    if read_only_lock and not is_read_only_command(definition):
        return False, "只读会话锁已开启，仅允许查询类命令和监听。"
    if session_mode == SESSION_MODE_LISTEN_ONLY:
        return False, "当前会话模式为只监听，禁止发送任何命令。"
    if session_mode == SESSION_MODE_SAFE_HANDSHAKE and not is_read_only_command(definition):
        return False, "当前会话模式为实时监测，仅允许查询类命令。"
    return True, ""
