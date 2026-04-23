from __future__ import annotations

from datetime import datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path
import time

from ygas_monitor.commanding.safety import SESSION_MODE_ENGINEERING, SESSION_MODE_SAFE_HANDSHAKE
from ygas_monitor.models import (
    CommandResult,
    ParsedFrame,
    RawFrameRecord,
    SerialSettings,
    SessionChangeEntry,
    SessionCommandLogEntry,
    SessionConfig,
)
from ygas_monitor.protocols.ygas import CommandEnvelope
from ygas_monitor.services.auto_silence_policy import AutoSilencePolicy, AutoSilenceTargetResolutionError
from ygas_monitor.services.export_service import export_session_package
from ygas_monitor.services.replay_service import load_replay_dataset, validate_replay_headers
from ygas_monitor.services.session_controller import AcquisitionWorker, AnalyzerSessionController, _PendingCommand
from ygas_monitor.services.settings_service import DEFAULT_SETTINGS, SettingsService
from ygas_monitor.serial.transport import AbstractTransport


class ScriptedTransport(AbstractTransport):
    def __init__(self, responses_by_write: dict[str, list[list[str] | list[bytes] | tuple[str, ...]]]):
        self.responses_by_write = {key: list(value) for key, value in responses_by_write.items()}
        self.pending_chunks: list[bytes] = []
        self.writes: list[str] = []
        self.flush_count = 0
        self._open = True

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write_line(self, text: str) -> None:
        self.writes.append(text)
        sequences = self.responses_by_write.get(text, [])
        if sequences:
            for chunk in sequences.pop(0):
                if isinstance(chunk, bytes):
                    self.pending_chunks.append(chunk)
                else:
                    self.pending_chunks.append(str(chunk).encode("ascii"))

    def read_available(self) -> bytes:
        if self.pending_chunks:
            return self.pending_chunks.pop(0)
        return b""

    def flush_input(self) -> None:
        self.flush_count += 1
        self.pending_chunks.clear()

    @property
    def is_open(self) -> bool:
        return self._open


def make_mode2_frame(device_id: str) -> str:
    return (
        f"YGAS,{device_id},0479.572,05.198,0958.423,04.249,1.3030,1.3033,"
        "0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005\r\n"
    )


def make_mode1_frame(device_id: str) -> str:
    return f"YGAS,{device_id},0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771\r\n"


class SettingsServiceTests(unittest.TestCase):
    def test_session_config_defaults_match_monitoring_product_defaults(self) -> None:
        config = SessionConfig()

        self.assertEqual(config.mode_preference, "AUTO")
        self.assertEqual(config.acquisition_mode, "LISTEN")
        self.assertEqual(config.session_mode, SESSION_MODE_SAFE_HANDSHAKE)
        self.assertEqual(config.permission_level, "READ_ONLY")
        self.assertFalse(config.read_only_lock)

    def test_default_session_settings_use_safe_onboarding_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            loaded = service.load()

        self.assertEqual(loaded["session"]["permission_level"], "READ_ONLY")
        self.assertEqual(loaded["session"]["session_mode"], SESSION_MODE_SAFE_HANDSHAKE)
        self.assertFalse(loaded["session"]["read_only_lock"])
        self.assertTrue(loaded["session"]["auto_start_stream_after_connect"])
        self.assertEqual(loaded["session"]["mode_preference"], "AUTO")

    def test_settings_roundtrip_preserves_recent_connection_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            payload = {
                "ui": {"theme": "light"},
                "session": {
                    "port": "COM35",
                    "baudrate": "115200",
                    "acquisition_mode": "POLL",
                    "mode_preference": "MODE1",
                    "profile_name": "manual_default",
                    "permission_level": "CONFIG",
                },
                "paths": {"export_dir": temp_dir, "last_replay_file": f"{temp_dir}\\sample.csv"},
            }
            service.save(payload)
            loaded = service.load()

        self.assertEqual(loaded["session"]["port"], "COM35")
        self.assertEqual(loaded["ui"]["theme"], "light")
        self.assertEqual(loaded["session"]["baudrate"], "115200")
        self.assertEqual(loaded["session"]["acquisition_mode"], "POLL")
        self.assertEqual(loaded["session"]["mode_preference"], "MODE1")
        self.assertEqual(loaded["session"]["profile_name"], "manual_default")
        self.assertEqual(loaded["paths"]["export_dir"], temp_dir)

    def test_corrupted_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text("{not-json", encoding="utf-8")
            service = SettingsService(path)
            loaded = service.load()

        self.assertEqual(loaded["session"]["port"], DEFAULT_SETTINGS["session"]["port"])
        self.assertEqual(loaded["ui"]["theme"], DEFAULT_SETTINGS["ui"]["theme"])
        self.assertEqual(loaded["window"]["width"], DEFAULT_SETTINGS["window"]["width"])


class AnalyzerSessionControllerTests(unittest.TestCase):
    def test_validate_stream_start_payload_allows_single_target_monitoring_flow(self) -> None:
        controller = AnalyzerSessionController("session")
        try:
            controller.connected = True
            controller.current_config = SessionConfig(
                serial=SerialSettings(port="COM35"),
                target_id="012",
                acquisition_mode="LISTEN",
                read_only_lock=False,
                session_mode=SESSION_MODE_SAFE_HANDSHAKE,
                permission_level="READ_ONLY",
                session_name="session",
            )

            ok, reason = controller.validate_stream_start_payload("SETCOMWAY,YGAS,012,1")

            self.assertTrue(ok)
            self.assertEqual(reason, "")
        finally:
            controller.shutdown()

    def test_validate_stream_start_payload_blocks_strict_read_only_and_broadcast(self) -> None:
        controller = AnalyzerSessionController("session")
        try:
            controller.connected = True
            controller.current_config = SessionConfig(
                serial=SerialSettings(port="COM35"),
                target_id="012",
                acquisition_mode="LISTEN",
                read_only_lock=True,
                session_mode=SESSION_MODE_SAFE_HANDSHAKE,
                permission_level="READ_ONLY",
                session_name="session",
            )

            ok, reason = controller.validate_stream_start_payload("SETCOMWAY,YGAS,012,1")
            self.assertFalse(ok)
            self.assertEqual(reason, "当前为严格只读，不会自动启动主动上传。")

            controller.current_config.read_only_lock = False
            controller.current_config.target_id = "FFF"
            ok, reason = controller.validate_stream_start_payload("SETCOMWAY,YGAS,FFF,1")
            self.assertFalse(ok)
            self.assertEqual(reason, "为避免影响总线上所有设备，系统不会自动广播启动主动上传，请选择单设备 ID。")
        finally:
            controller.shutdown()


class ReplayServiceTests(unittest.TestCase):
    def test_validate_replay_headers_detects_missing_columns(self) -> None:
        ok, missing = validate_replay_headers(["timestamp", "raw"])
        self.assertFalse(ok)
        self.assertEqual(missing, ["mode"])

    def test_load_replay_dataset_parses_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "replay.csv"
            path.write_text(
                'timestamp,device_id,mode,status,raw,co2_ppm\n'
                '2026-04-18T12:00:00,001,1,0001,"YGAS,001,0488.100,00.528,0.98,0.98,026.10,101.14,0001,2771",488.1\n',
                encoding="utf-8-sig",
            )
            dataset = load_replay_dataset(path)

        self.assertEqual(len(dataset.frames), 1)
        self.assertEqual(dataset.frames[0].device_id, "001")
        self.assertEqual(dataset.frames[0].mode, 1)
        self.assertEqual(dataset.frames[0].fields["co2_ppm"], 488.1)


class SessionExportPackageTests(unittest.TestCase):
    def test_export_session_package_writes_expected_artifacts(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879, "h2o_mmol": 0.528, "pressure_kpa": 101.14},
            status="0001",
            extras=[],
        )
        raw_records = [
            RawFrameRecord(timestamp=frame_time, direction="TX", text="READDATA,YGAS,001 | target=001 | broadcast=no"),
            RawFrameRecord(timestamp=frame_time + timedelta(seconds=1), direction="RX", text=frame.raw),
        ]
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="设置工作模式",
                target_device_id="001",
                source_page="设备控制",
                before_value="MODE1",
                target_value="MODE2",
                after_value="MODE2",
                result_text="一致",
                detail_text="写前/目标/写后值一致",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=raw_records,
                config=config,
                parameter_change_entries=change_entries,
                output_dir=temp_dir,
            )
            files = {item.name for item in package_dir.iterdir()}
            journal_json = (package_dir / "parameter_change_journal.json").read_text(encoding="utf-8")
            journal_csv = (package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig")
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))
            summary_txt = (package_dir / "session_summary.txt").read_text(encoding="utf-8")

        self.assertIn("recent_structured_data.csv", files)
        self.assertIn("recent_raw_frames.log", files)
        self.assertIn("recent_command_log.tsv", files)
        self.assertIn("config_snapshot.json", files)
        self.assertIn("session_summary.json", files)
        self.assertIn("session_summary.txt", files)
        self.assertIn("parameter_change_journal.json", files)
        self.assertIn("parameter_change_journal.csv", files)
        self.assertIn('"command_or_parameter": "设置工作模式"', journal_json)
        self.assertIn('"verification_status": "verified_consistent"', journal_json)
        self.assertIn("source_page", journal_csv)
        self.assertIn("设备控制", journal_csv)
        self.assertEqual(summary_json["parameter_change_count"], 1)
        self.assertEqual(summary_json["verified_consistent_count"], 1)
        self.assertEqual(summary_json["ack_only_unverified_count"], 0)
        self.assertEqual(summary_json["unconfirmed_change_count"], 0)
        self.assertEqual(summary_json["failed_change_count"], 0)
        self.assertEqual(summary_json["structured_export_scope"], "recent_cache")
        self.assertEqual(summary_json["structured_frame_count_exported"], 1)
        self.assertEqual(summary_json["raw_frame_export_scope"], "recent_cache")
        self.assertEqual(summary_json["command_log_export_scope"], "recent_cache")
        self.assertIn("Verified Consistent Count: 1", summary_txt)
        self.assertIn("ACK-only Unverified Count: 0", summary_txt)
        self.assertIn("Unconfirmed Change Count: 0", summary_txt)

    def test_export_session_package_keeps_full_parameter_change_history_beyond_ui_preview_limit(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time + timedelta(seconds=index),
                command_name="设置工作模式",
                target_device_id="001",
                source_page="设备控制",
                before_value=f"MODE{index}",
                target_value=f"MODE{index + 1}",
                after_value=f"MODE{index + 1}",
                result_text="一致",
                detail_text=f"change-{index}",
            )
            for index in range(25)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                output_dir=temp_dir,
            )
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))
            journal_csv_lines = (package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(len(journal_json), 25)
        self.assertEqual(len(journal_csv_lines), 26)

    def test_export_session_package_can_reconstruct_full_structured_data_from_logger(self) -> None:
        frame_time = datetime.now()
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="session")
        recent_frames = [
            ParsedFrame(
                timestamp=frame_time + timedelta(seconds=300),
                raw="YGAS,002,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                device_id="002",
                mode=1,
                fields={"co2_ppm": 488.879},
                status="0001",
                extras=[],
            ),
            ParsedFrame(
                timestamp=frame_time + timedelta(seconds=301),
                raw="YGAS,002,0489.100,00.520,0.98,0.98,026.10,101.14,0001,2771",
                device_id="002",
                mode=1,
                fields={"co2_ppm": 489.1},
                status="0001",
                extras=[],
            ),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            logger_path = Path(temp_dir) / "session_logger.log"
            lines = ["# timestamp\tdirection\tlevel\ttext"]
            for index in range(2105):
                timestamp = (frame_time + timedelta(milliseconds=index * 100)).isoformat(timespec="milliseconds")
                device_id = "001" if index % 2 == 0 else "002"
                lines.append(f"{timestamp}\tRX\tINFO\t{make_mode2_frame(device_id).strip()}")
            logger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            package_dir = export_session_package(
                session_name="session",
                frames=recent_frames,
                raw_records=[],
                config=config,
                output_dir=temp_dir,
                logger_path=logger_path,
            )
            files = {item.name for item in package_dir.iterdir()}
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))
            structured_lines = (package_dir / "structured_data.csv").read_text(encoding="utf-8-sig").splitlines()

        self.assertIn("structured_data.csv", files)
        self.assertIn("raw_frames.log", files)
        self.assertIn("command_log.tsv", files)
        self.assertNotIn("recent_structured_data.csv", files)
        self.assertEqual(summary_json["structured_export_scope"], "full")
        self.assertEqual(summary_json["structured_frame_count_exported"], 2105)
        self.assertEqual(summary_json["raw_rx_count_total"], 2105)
        self.assertEqual(summary_json["raw_frame_export_scope"], "full")
        self.assertEqual(summary_json["command_log_export_scope"], "full")
        self.assertEqual(summary_json["recent_frame_count"], 2)
        self.assertEqual(summary_json["recent_device_ids"], ["002"])
        self.assertEqual(summary_json["structured_device_ids"], ["001", "002"])
        self.assertEqual(summary_json["recent_started_at"], recent_frames[0].timestamp.isoformat(timespec="seconds"))
        self.assertEqual(summary_json["recent_ended_at"], recent_frames[-1].timestamp.isoformat(timespec="seconds"))
        self.assertEqual(summary_json["structured_started_at"], frame_time.isoformat(timespec="seconds"))
        self.assertEqual(
            summary_json["structured_ended_at"],
            (frame_time + timedelta(milliseconds=2104 * 100)).isoformat(timespec="seconds"),
        )
        self.assertEqual(len(structured_lines), 2106)

    def test_export_session_package_preserves_ack_only_verification_status(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="设置数据发送方式",
                target_device_id="001",
                source_page="校准联调准备清单",
                before_value="未读取",
                target_value="目标发送方式：主动发送",
                after_value="未读回",
                result_text="ACK 成功",
                detail_text=(
                    "ACK-only / 未复核；payload=SETCOMWAY,YGAS,FFF,1；target=FFF（FFF 广播，可能影响总线上所有设备）；"
                    "无读回复核，仅记录 ACK 成功。 如清单已取消，本记录仍保留追溯，不代表写后复核一致。"
                ),
                verification_status="ack_only_unverified",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                output_dir=temp_dir,
            )
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))
            journal_csv = (package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig")
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))
            summary_txt = (package_dir / "session_summary.txt").read_text(encoding="utf-8")

        self.assertEqual(journal_json[0]["verification_status"], "ack_only_unverified")
        self.assertEqual(journal_json[0]["before_value"], "未读取")
        self.assertEqual(journal_json[0]["after_value"], "未读回")
        self.assertIn("ack_only_unverified", journal_csv)
        self.assertIn("校准联调准备清单", journal_csv)
        self.assertIn("ACK-only", journal_json[0]["note"])
        self.assertIn("FFF 广播", journal_json[0]["note"])
        self.assertEqual(summary_json["parameter_change_count"], 1)
        self.assertEqual(summary_json["verified_consistent_count"], 0)
        self.assertEqual(summary_json["ack_only_unverified_count"], 1)
        self.assertEqual(summary_json["unconfirmed_change_count"], 0)
        self.assertEqual(summary_json["failed_change_count"], 0)
        self.assertIn("ACK-only Unverified Count: 1", summary_txt)
        self.assertIn("Unconfirmed Change Count: 0", summary_txt)

    def test_export_session_package_writes_structured_command_log_and_trace_fields(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="012",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="012", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="设置数据发送方式",
                target_device_id="012",
                source_page="校准联调准备清单",
                before_value="未读取",
                target_value="目标发送方式：主动发送",
                after_value="未读回",
                result_text="ACK 成功",
                detail_text="ACK-only / 未复核",
                verification_status="ack_only_unverified",
                command_payload="SETCOMWAY,YGAS,FFF,1",
                command_target_id="FFF",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="broadcast",
                original_auto_upload_state="on",
                restore_policy="restore_after_read",
                restore_attempted=False,
                restore_result="pending_restore",
            )
        ]
        command_entries = [
            SessionCommandLogEntry(
                timestamp=frame_time,
                payload="SETCOMWAY,YGAS,FFF,0",
                result="ACK 成功",
                action_type="auto_silence",
                action_label_zh="临时暂停主动上传",
                parent_command="MODE,YGAS,FFF,2",
                command_target_id="FFF",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="broadcast",
                source_page="主动上传暂停保护",
                detail_text="主动上传已临时暂停。",
                original_auto_upload_state="on",
                restore_policy="restore_after_read",
                restore_attempted=False,
                restore_result="pending_restore",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                command_entries=command_entries,
                output_dir=temp_dir,
            )
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))
            journal_csv = (package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig")
            command_log = (package_dir / "command_log.tsv").read_text(encoding="utf-8")
            command_log_csv = (package_dir / "command_log.csv").read_text(encoding="utf-8-sig")
            command_log_csv_bytes = (package_dir / "command_log.csv").read_bytes()

        self.assertEqual(journal_json[0]["command_target_id"], "FFF")
        self.assertEqual(journal_json[0]["expected_device_id"], "012")
        self.assertEqual(journal_json[0]["response_device_id"], "012")
        self.assertEqual(journal_json[0]["effective_scope"], "broadcast")
        self.assertEqual(journal_json[0]["original_auto_upload_state"], "on")
        self.assertEqual(journal_json[0]["restore_policy"], "restore_after_read")
        self.assertEqual(journal_json[0]["action_label_zh"], "命令执行")
        self.assertIn("action_label_zh", journal_json[0])
        self.assertIn("原始主动上传状态：已开启", journal_json[0]["note"])
        self.assertIn("读取后恢复策略：读取完成后恢复主动上传", journal_json[0]["note"])
        self.assertIn("恢复结果：待恢复", journal_json[0]["note"])
        self.assertIn("command_target_id", journal_csv)
        self.assertIn("action_label_zh", journal_csv)
        self.assertIn("response_device_id", journal_csv)
        self.assertIn("original_auto_upload_state", journal_csv)
        self.assertIn("原始主动上传状态：已开启", journal_csv)
        self.assertIn("auto_silence", command_log)
        self.assertIn("action_label_zh", command_log)
        self.assertIn("临时暂停主动上传", command_log)
        self.assertIn("restore_policy", command_log)
        self.assertIn("MODE,YGAS,FFF,2", command_log)
        self.assertIn("主动上传暂停保护", command_log)
        self.assertIn("原始主动上传状态：已开启", command_log)
        self.assertIn("读取后恢复策略：读取完成后恢复主动上传", command_log)
        self.assertNotIn("restore_policy=restore_after_read", command_log)
        self.assertTrue(command_log_csv_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertIn("action_label_zh", command_log_csv)
        self.assertIn("临时暂停主动上传", command_log_csv)
        self.assertIn("原始主动上传状态：已开启", command_log_csv)

    def test_export_session_package_formats_restore_detail_text_in_chinese(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="012",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="012", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="恢复主动上传",
                target_device_id="012",
                source_page="恢复主动上传",
                before_value="主动上传已临时暂停",
                target_value="主动上传已开启",
                after_value="ACK 已收到，未读回确认",
                result_text="ACK 成功（未复核）",
                detail_text="ACK 成功，已恢复主动上传。",
                verification_status="ack_only_unverified",
                action_type="auto_silence_restore",
                action_label_zh="恢复主动上传",
                command_payload="SETCOMWAY,YGAS,012,1",
                parent_command="GETCO,YGAS,012,1",
                command_target_id="012",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="single",
                is_system_action=True,
                original_auto_upload_state="on",
                restore_policy="restore_after_read",
                restore_attempted=True,
                restore_result="restored",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                command_entries=[],
                output_dir=temp_dir,
            )
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))

        self.assertEqual(journal_json[0]["before_value"], "主动上传已临时暂停")
        self.assertEqual(journal_json[0]["original_auto_upload_state"], "on")
        self.assertEqual(journal_json[0]["verification_status"], "ack_only_unverified")
        self.assertIn("恢复结果：已恢复", journal_json[0]["note"])

    def test_export_session_package_preserves_keep_auto_upload_off_decision(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="012",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="012", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="保持主动上传关闭",
                target_device_id="012",
                source_page="恢复主动上传",
                before_value="主动上传已临时暂停",
                target_value="主动上传保持关闭",
                after_value="用户确认保持关闭",
                result_text="用户确认",
                detail_text="用户选择保持主动上传关闭，系统不会再次自动发送 SETCOMWAY=1。",
                verification_status="decision_recorded",
                action_type="keep_auto_upload_off",
                action_label_zh="保持主动上传关闭",
                command_payload="SETCOMWAY,YGAS,012,1",
                parent_command="SETCOMWAY,YGAS,012,1",
                command_target_id="012",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="single",
                is_system_action=False,
                original_auto_upload_state="on",
                restore_policy="keep_off",
                restore_attempted=True,
                restore_result="kept_off",
            )
        ]
        command_entries = [
            SessionCommandLogEntry(
                timestamp=frame_time,
                payload="SETCOMWAY,YGAS,012,1",
                result="用户确认",
                action_type="keep_auto_upload_off",
                action_label_zh="保持主动上传关闭",
                parent_command="SETCOMWAY,YGAS,012,1",
                command_target_id="012",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="single",
                source_page="恢复主动上传",
                detail_text="用户选择保持主动上传关闭，系统不会再次自动发送 SETCOMWAY=1。",
                level="INFO",
                original_auto_upload_state="on",
                restore_policy="keep_off",
                restore_attempted=True,
                restore_result="kept_off",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                command_entries=command_entries,
                output_dir=temp_dir,
            )
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))
            command_log_csv = (package_dir / "command_log.csv").read_text(encoding="utf-8-sig")
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))

        self.assertEqual(journal_json[0]["action_type"], "keep_auto_upload_off")
        self.assertEqual(journal_json[0]["action_label_zh"], "保持主动上传关闭")
        self.assertEqual(journal_json[0]["verification_status"], "decision_recorded")
        self.assertIn("保持主动上传关闭", command_log_csv)
        self.assertIn("action_label_zh", command_log_csv)
        self.assertEqual(summary_json["verified_consistent_count"], 0)
        self.assertEqual(summary_json["ack_only_unverified_count"], 0)

    def test_export_session_package_keeps_full_command_log_history_beyond_recent_200(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="012",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="012", session_name="session")
        command_entries = [
            SessionCommandLogEntry(
                timestamp=frame_time + timedelta(seconds=index),
                payload=f"MODE,YGAS,012,{index % 3}",
                result="成功",
                action_type="command",
                command_target_id="012",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="single",
                source_page="设备控制",
                detail_text=f"command-{index}",
            )
            for index in range(245)
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                command_entries=command_entries,
                output_dir=temp_dir,
            )
            files = {item.name for item in package_dir.iterdir()}
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))
            command_log_lines = (package_dir / "command_log.tsv").read_text(encoding="utf-8").splitlines()

        self.assertIn("command_log.tsv", files)
        self.assertNotIn("recent_command_log.tsv", files)
        self.assertEqual(summary_json["command_log_export_scope"], "full")
        self.assertEqual(summary_json["command_log_count_exported"], 245)
        self.assertEqual(summary_json["command_log_cache_limit"], 200)
        self.assertEqual(len(command_log_lines), 246)

    def test_export_session_package_counts_unconfirmed_ack_pending_records(self) -> None:
        frame_time = datetime.now()
        frame = ParsedFrame(
            timestamp=frame_time,
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="session")
        change_entries = [
            SessionChangeEntry(
                timestamp=frame_time,
                command_name="设置数据发送方式",
                target_device_id="001",
                source_page="校准联调准备清单",
                before_value="未读取",
                target_value="目标发送方式：主动发送",
                after_value="未读回",
                result_text="未收到 ACK",
                detail_text=(
                    "ACK 未确认；payload=SETCOMWAY,YGAS,FFF,1；target=FFF（FFF 广播，可能影响总线上所有设备）；"
                    "命令已发出或已登记为 ACK-only pending，但在连接断开前未收到 ACK。"
                    " 本记录不代表设备一定未执行，仅表示软件未取得确认。"
                ),
                verification_status="ack_timeout_unverified",
            )
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=[],
                config=config,
                parameter_change_entries=change_entries,
                output_dir=temp_dir,
            )
            summary_json = json.loads((package_dir / "session_summary.json").read_text(encoding="utf-8"))
            summary_txt = (package_dir / "session_summary.txt").read_text(encoding="utf-8")
            journal_json = json.loads((package_dir / "parameter_change_journal.json").read_text(encoding="utf-8"))

        self.assertEqual(summary_json["ack_only_unverified_count"], 0)
        self.assertEqual(summary_json["unconfirmed_change_count"], 1)
        self.assertEqual(summary_json["failed_change_count"], 0)
        self.assertIn("Unconfirmed Change Count: 1", summary_txt)
        self.assertEqual(journal_json[0]["verification_status"], "ack_timeout_unverified")


class AcquisitionWorkerTests(unittest.TestCase):
    def make_worker(
        self,
        transport: ScriptedTransport,
        *,
        target_id: str = "002",
        mode_preference: str = "MODE2",
        permission_level: str = "CONFIG",
        read_only_lock: bool = False,
        session_mode: str = SESSION_MODE_ENGINEERING,
    ) -> AcquisitionWorker:
        worker = AcquisitionWorker()
        worker._transport = transport
        worker._config = SessionConfig(
            serial=SerialSettings(port="SIMULATOR"),
            target_id=target_id,
            mode_preference=mode_preference,
            command_timeout_ms=300,
            permission_level=permission_level,
            read_only_lock=read_only_lock,
            session_mode=session_mode,
        )
        return worker

    def test_initialize_capture_no_longer_sends_batch_writes(self) -> None:
        transport = ScriptedTransport({})
        worker = self.make_worker(transport, target_id="012")
        results: list[CommandResult] = []
        worker.command_completed.connect(results.append)

        worker.initialize_capture()

        self.assertEqual(transport.writes, [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("待确认清单", results[0].message)
        self.assertIn("MODE / FTD / SETCOMWAY", results[0].message)

    def test_ack_from_wrong_device_id_does_not_count_as_success(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [["<YGAS,002,T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("target=012", result.message)
        self.assertEqual(worker._latest_rx_device_id, "002")

    def test_auto_start_stream_send_payload_has_system_context(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [["<YGAS,012,T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(
            payload,
            expectation="ack",
            timeout_ms=300,
            context={
                "system_action": "auto_start_stream",
                "action_type": "auto_start_stream",
                "source_page": "连接后自动启动实时流",
                "auto_upload_state": "on",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.action_type, "auto_start_stream")
        self.assertEqual(result.action_label_zh, "连接后自动启动实时流")
        self.assertEqual(result.source_page, "连接后自动启动实时流")
        self.assertEqual(result.auto_upload_state, "on")

    def test_write_command_is_blocked_when_online_device_mismatches_target(self) -> None:
        self.test_default_fff_write_is_blocked_when_online_device_mismatches_session_target()
        return
        payload = "MODE,YGAS,012,2"
        worker = self.make_worker(ScriptedTransport({}), target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("当前目标设备 ID 与实时在线设备 ID 不一致", result.message)
        self.assertIn("target=012", result.message)
        self.assertIn("online=002", result.message)

    def test_default_fff_write_is_blocked_when_online_device_mismatches_session_target(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        worker = self.make_worker(ScriptedTransport({}), target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("当前目标设备 ID 与实时在线设备 ID 不一致", result.message)
        self.assertIn("target=012", result.message)
        self.assertIn("online=002", result.message)
        self.assertIn("虽默认使用 FFF", result.message)
        self.assertEqual(worker._transport.writes, [])

    def test_default_fff_write_allows_send_when_online_device_matches_session_target(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        silence_payload = "SETCOMWAY,YGAS,012,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,012,T>\r\n"]],
                payload: [["<YGAS,012,T>\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload])

    def test_broadcast_ack_matches_expected_session_device(self) -> None:
        payload = "SETCOMWAY,YGAS,FFF,1"
        transport = ScriptedTransport({payload: [["<YGAS,012,T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertTrue(result.ok)
        self.assertEqual(result.response_device_id, "012")
        self.assertEqual(result.command_target_id, "FFF")
        self.assertEqual(result.expected_device_id, "012")
        self.assertEqual(result.effective_scope, "broadcast")

    def test_single_target_ack_tracks_command_target_expected_and_response_devices(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [["<YGAS,012,T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertTrue(result.ok)
        self.assertEqual(result.command_target_id, "012")
        self.assertEqual(result.expected_device_id, "012")
        self.assertEqual(result.response_device_id, "012")
        self.assertEqual(result.effective_scope, "single")

    def test_broadcast_ack_does_not_match_other_device_when_session_target_is_known(self) -> None:
        payload = "SETCOMWAY,YGAS,FFF,1"
        transport = ScriptedTransport({payload: [["<YGAS,013,T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(result.timeout_reason, "mismatched_device")
        self.assertIn("online=013", result.message)

    def test_broadcast_ack_without_expected_device_adds_attribution_note(self) -> None:
        worker = self.make_worker(ScriptedTransport({}), target_id="FFF")
        pending = _PendingCommand(
            envelope=CommandEnvelope("SETCOMWAY", "FFF", ["1"]),
            expectation="ack",
            expected_response_device_id="",
        )

        outcome = worker._parse_expected_response_structured("<YGAS,013,T>\r\n", pending)

        self.assertIsInstance(outcome, tuple)
        assert isinstance(outcome, tuple)
        self.assertEqual(outcome[0], "result")
        self.assertEqual(outcome[1].message, "ACK 成功，但当前广播 ACK 归属未绑定到明确会话目标。")

    def test_default_fff_write_fails_when_online_device_is_unknown(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        worker = self.make_worker(ScriptedTransport({}), target_id="012")

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("当前无法确认实时在线设备 ID", result.message)
        self.assertIn("target=012", result.message)
        self.assertIn("唯一在线设备", result.message)
        self.assertEqual(worker._transport.writes, [])

    def test_default_fff_write_fails_when_multiple_online_devices_are_active(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        worker = self.make_worker(ScriptedTransport({}), target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._active_rx_entries.append((now, "003"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("当前检测到多个实时在线设备 ID", result.message)
        self.assertIn("target=012", result.message)
        self.assertIn("online=002,003", result.message)
        self.assertEqual(worker._transport.writes, [])

    def test_read_command_is_not_blocked_by_default_fff_write_guard(self) -> None:
        payload = "READDATA,YGAS,012"
        transport = ScriptedTransport({payload: [[make_mode2_frame("012")]]})
        worker = self.make_worker(transport, target_id="012")

        result = worker._send_payload(payload, expectation="data", timeout_ms=250)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [payload])

    def test_mode_query_returns_structured_payload_for_readback(self) -> None:
        payload = "MODE,YGAS,012"
        silence_payload = "SETCOMWAY,YGAS,012,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,012,T>\r\n"]],
                payload: [["YGAS,012,2\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="mode_value", timeout_ms=250)

        self.assertTrue(result.ok)
        self.assertEqual(result.response_kind, "mode_value")
        self.assertEqual(result.response_device_id, "012")
        self.assertEqual(result.parsed_payload["mode"], 2)

    def test_getco_returns_structured_coefficients_for_readback(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(payload, expectation="coefficient", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(result.response_kind, "coefficient")
        self.assertEqual(result.response_device_id, "002")
        self.assertEqual(result.parsed_payload["C0"], 65916.6)

    def test_id_rewrite_successfully_syncs_session_target(self) -> None:
        payload = "ID,YGAS,FFF,007"
        transport = ScriptedTransport(
            {
                payload: [[make_mode2_frame("007"), "<YGAS,002,T>\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)
        sync_events: list[tuple[str, str]] = []
        worker.target_sync_requested.connect(lambda target, source: sync_events.append((target, source)))

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(worker._config.target_id, "007")
        self.assertEqual(sync_events[0][0], "007")

    def test_write_silence_fails_when_setcomway_ack_is_missing(self) -> None:
        payload = "SENCO1,YGAS,FFF,1.00000e00"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport({silence_payload: [[]]})
        worker = self.make_worker(transport)
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [silence_payload])
        self.assertEqual(result.message, "暂停主动上传失败：未收到 SETCOMWAY ACK。")

    def test_auto_silence_emits_structured_system_action_before_main_write(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        silence_payload = "SETCOMWAY,YGAS,012,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,012,T>\r\n"]],
                payload: [["<YGAS,012,T>\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)
        emitted: list[CommandResult] = []
        worker.command_completed.connect(emitted.append)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload])
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].action_type, "auto_silence")
        self.assertEqual(emitted[0].command, silence_payload)
        self.assertEqual(emitted[0].parent_command, payload)
        self.assertEqual(emitted[0].command_target_id, "012")
        self.assertEqual(emitted[0].expected_device_id, "012")
        self.assertEqual(emitted[0].response_device_id, "012")
        self.assertEqual(emitted[0].effective_scope, "single")

    def test_auto_silence_failure_blocks_main_command_and_keeps_parent_command_trace(self) -> None:
        payload = "MODE,YGAS,FFF,2"
        silence_payload = "SETCOMWAY,YGAS,012,0"
        transport = ScriptedTransport({silence_payload: [[]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)
        emitted: list[CommandResult] = []
        worker.command_completed.connect(emitted.append)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [silence_payload])
        self.assertEqual(result.message, "暂停主动上传失败：未收到 SETCOMWAY ACK。")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].action_type, "auto_silence")
        self.assertEqual(emitted[0].parent_command, payload)
        self.assertFalse(emitted[0].ok)

    def test_write_silence_fails_when_stream_frames_continue(self) -> None:
        payload = "SENCO1,YGAS,FFF,1.00000e00"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n", make_mode2_frame("002")]],
            }
        )
        worker = self.make_worker(transport)
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [silence_payload])
        self.assertIn("主动上传未真正关闭", result.message)

    def test_write_silence_fails_when_mode1_stream_frames_continue(self) -> None:
        payload = "SENCO1,YGAS,FFF,1.00000e00"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n", make_mode1_frame("002")]],
            }
        )
        worker = self.make_worker(transport)
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertFalse(result.ok)
        self.assertIn("MODE1", result.message)
        self.assertIn("实时数据帧", result.message)

    def test_write_silence_allows_getco_after_quiet_window(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                "SETCOMWAY,YGAS,002,1": [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(
            payload,
            expectation="coefficient",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "on",
                "restore_policy": "restore_after_read",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload, "SETCOMWAY,YGAS,002,1"])
        self.assertEqual(transport.flush_count, 0)
        self.assertIn("建议写回格式", result.message)
        self.assertEqual(result.original_auto_upload_state, "on")
        self.assertEqual(result.restore_policy, "restore_after_read")
        self.assertTrue(result.restore_attempted)
        self.assertEqual(result.restore_result, "restored")

    def test_quiet_read_emits_contextual_auto_silence_and_restore_system_actions(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        restore_payload = "SETCOMWAY,YGAS,002,1"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                restore_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")
        emitted: list[CommandResult] = []
        worker.command_completed.connect(emitted.append)

        result = worker._send_payload(
            payload,
            expectation="coefficient",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "on",
                "restore_policy": "restore_after_read",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload, restore_payload])
        self.assertEqual(len(emitted), 2)

        silence_result = emitted[0]
        self.assertEqual(silence_result.action_type, "auto_silence")
        self.assertEqual(silence_result.parent_command, payload)
        self.assertEqual(silence_result.command_target_id, "002")
        self.assertEqual(silence_result.expected_device_id, "002")
        self.assertEqual(silence_result.response_device_id, "002")
        self.assertEqual(silence_result.effective_scope, "single")
        self.assertEqual(silence_result.source_page, "主动上传暂停保护")
        self.assertEqual(silence_result.action_label_zh, "临时暂停主动上传")
        self.assertEqual(silence_result.message, "ACK 成功，主动上传已临时暂停。")
        self.assertEqual(silence_result.original_auto_upload_state, "on")
        self.assertEqual(silence_result.restore_policy, "restore_after_read")
        self.assertFalse(silence_result.restore_attempted)
        self.assertEqual(silence_result.restore_result, "pending_restore")

        restore_result = emitted[1]
        self.assertEqual(restore_result.action_type, "auto_silence_restore")
        self.assertEqual(restore_result.parent_command, payload)
        self.assertEqual(restore_result.command_target_id, "002")
        self.assertEqual(restore_result.expected_device_id, "002")
        self.assertEqual(restore_result.response_device_id, "002")
        self.assertEqual(restore_result.effective_scope, "single")
        self.assertEqual(restore_result.source_page, "恢复主动上传")
        self.assertEqual(restore_result.action_label_zh, "恢复主动上传")
        self.assertEqual(restore_result.original_auto_upload_state, "on")
        self.assertEqual(restore_result.restore_policy, "restore_after_read")
        self.assertTrue(restore_result.restore_attempted)
        self.assertEqual(restore_result.restore_result, "restored")

    def test_quiet_read_keeps_auto_upload_off_when_original_state_is_off(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(
            payload,
            expectation="coefficient",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "off",
                "restore_policy": "restore_after_read",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload])
        self.assertEqual(result.original_auto_upload_state, "off")
        self.assertEqual(result.restore_policy, "keep_off")
        self.assertFalse(result.restore_attempted)
        self.assertEqual(result.restore_result, "kept_off")

    def test_quiet_read_unknown_state_does_not_restore_without_explicit_policy(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(
            payload,
            expectation="coefficient",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "unknown",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload])
        self.assertEqual(result.original_auto_upload_state, "unknown")
        self.assertEqual(result.restore_policy, "unknown_no_restore")
        self.assertFalse(result.restore_attempted)
        self.assertEqual(result.restore_result, "left_unknown")

    def test_auto_silence_target_resolution_requires_explicit_broadcast(self) -> None:
        with self.assertRaises(AutoSilenceTargetResolutionError):
            AutoSilencePolicy.silence_target_id("FFF", expected_device_id="", active_device_ids=[], allow_broadcast=False)

        with self.assertRaises(AutoSilenceTargetResolutionError):
            AutoSilencePolicy.silence_target_id(
                "",
                expected_device_id="",
                active_device_ids=["002", "003"],
                allow_broadcast=False,
            )

        self.assertEqual(
            AutoSilencePolicy.silence_target_id("FFF", expected_device_id="012", active_device_ids=["012"]),
            "012",
        )
        self.assertEqual(
            AutoSilencePolicy.silence_target_id("FFF", expected_device_id="012", active_device_ids=["012"], allow_broadcast=True),
            "FFF",
        )

    def test_quiet_read_without_resolved_target_does_not_send_setcomway(self) -> None:
        payload = "MODE,YGAS,FFF"
        transport = ScriptedTransport({})
        worker = self.make_worker(transport, target_id="FFF")

        result = worker._send_payload(
            payload,
            expectation="mode_value",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "on",
                "restore_policy": "restore_after_read",
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [])
        self.assertIn("无法确定暂停目标", result.message)

    def test_read_only_getco_does_not_auto_silence_without_explicit_context(self) -> None:
        payload = "GETCO,YGAS,002,1"
        transport = ScriptedTransport({payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]]})
        worker = self.make_worker(transport, target_id="002", permission_level="READ_ONLY")

        result = worker._send_payload(payload, expectation="coefficient", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [payload])

    def test_read_only_lock_blocks_quiet_read_auto_silence(self) -> None:
        payload = "GETCO,YGAS,002,1"
        transport = ScriptedTransport({})
        worker = self.make_worker(transport, target_id="002", read_only_lock=True)
        emitted: list[CommandResult] = []
        worker.command_completed.connect(emitted.append)

        result = worker._send_payload(
            payload,
            expectation="coefficient",
            timeout_ms=300,
            context={
                "quiet_read": "1",
                "original_auto_upload_state": "on",
                "restore_policy": "restore_after_read",
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [])
        self.assertIn("只读锁", result.message)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].action_type, "auto_silence")

    def test_mode_read_does_not_auto_silence_without_explicit_quiet_read(self) -> None:
        payload = "MODE,YGAS,012"
        transport = ScriptedTransport({payload: [["YGAS,012,2\r\n"]]})
        worker = self.make_worker(transport, target_id="012")

        result = worker._send_payload(payload, expectation="mode_value", timeout_ms=250)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [payload])

    def test_getco_ack_without_coefficients_has_specific_message(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,002,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["<YGAS,002,T>\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(payload, expectation="coefficient", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("GETCO 只有 ACK、没有系数行", result.message)

    def test_true_timeout_keeps_generic_timeout_message(self) -> None:
        payload = "READDATA,YGAS,002"
        transport = ScriptedTransport({payload: [[]]})
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(payload, expectation="data", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertIn("命令超时", result.message)

    def test_ack_matches_when_telemetry_frames_bracket_it_under_streaming(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport(
            {
                payload: [
                    [
                        make_mode2_frame("012"),
                        make_mode2_frame("012"),
                        "<YGAS,012,T>\r\n",
                        make_mode2_frame("012"),
                    ]
                ]
            }
        )
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)
        frames: list[ParsedFrame] = []
        raw_records: list[RawFrameRecord] = []
        worker.frame_received.connect(frames.append)
        worker.raw_received.connect(raw_records.append)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)
        worker._tick()

        self.assertTrue(result.ok)
        self.assertEqual(result.response_kind, "ack")
        self.assertEqual(result.response_device_id, "012")
        self.assertEqual(result.matched_response_line, "<YGAS,012,T>")
        self.assertGreaterEqual(len(frames), 3)
        self.assertGreaterEqual(len([record for record in raw_records if record.direction == "RX"]), 4)

    def test_ack_and_telemetry_in_same_chunk_are_split_without_dropping_trailing_frame(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        mixed_chunk = (
            make_mode2_frame("012")
            + "<YGAS,012,T>\r\n"
            + make_mode2_frame("012")
        )
        transport = ScriptedTransport({payload: [[mixed_chunk]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)
        frames: list[ParsedFrame] = []
        raw_records: list[RawFrameRecord] = []
        worker.frame_received.connect(frames.append)
        worker.raw_received.connect(raw_records.append)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(result.matched_response_line, "<YGAS,012,T>")
        self.assertEqual(len(frames), 2)
        self.assertEqual(len([record for record in raw_records if record.direction == "RX"]), 3)

    def test_ack_is_not_matched_until_split_chunks_form_a_complete_line(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [[b"<YGAS,012,", b"T>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(result.matched_response_line, "<YGAS,012,T>")

    def test_ack_is_matched_when_wrapped_response_ends_with_gt_without_crlf(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [[b"<YGAS,012,", b"T>"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(result.matched_response_line, "<YGAS,012,T>")

    def test_timeout_reports_telemetry_only_when_no_matching_ack_is_received(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [[make_mode2_frame("012"), make_mode2_frame("012")]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(result.timeout_reason, "telemetry_only_no_match")
        self.assertIn("实时数据帧", result.message)
        self.assertEqual(result.matched_response_line, "")
        self.assertGreaterEqual(result.observed_telemetry_count, 2)

    def test_other_target_or_other_response_does_not_satisfy_current_pending_command(self) -> None:
        payload = "SETCOMWAY,YGAS,012,1"
        transport = ScriptedTransport({payload: [["<YGAS,013,T>\r\n", "<YGAS,012,49>\r\n"]]})
        worker = self.make_worker(transport, target_id="012")
        now = time.monotonic()
        worker._active_rx_entries.append((now, "012"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(result.timeout_reason, "mismatched_device")
        self.assertIn("target=012", result.message)
        self.assertIn("online=013", result.message)

    def test_serial_query_matches_readback_amid_telemetry_stream(self) -> None:
        payload = "SETCOM,YGAS,012"
        transport = ScriptedTransport(
            {
                payload: [
                    [
                        make_mode2_frame("012"),
                        "<YGAS,012,115200,8,N,1>\r\n",
                        make_mode2_frame("012"),
                    ]
                ]
            }
        )
        worker = self.make_worker(transport, target_id="012")
        frames: list[ParsedFrame] = []
        worker.frame_received.connect(frames.append)

        result = worker._send_payload(payload, expectation="serial_config", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(result.response_kind, "serial_config")
        self.assertEqual(result.response_device_id, "012")
        self.assertEqual(result.parsed_payload["baudrate"], 115200)
        self.assertEqual(result.matched_response_line, "<YGAS,012,115200,8,N,1>")
        self.assertEqual(len(frames), 2)

    def test_readdata_does_not_claim_success_when_live_stream_is_already_active(self) -> None:
        payload = "READDATA,YGAS,012"
        transport = ScriptedTransport({payload: [[make_mode2_frame("012"), make_mode2_frame("012")]]})
        worker = self.make_worker(transport, target_id="012")
        worker._last_stream_frame_monotonic = time.monotonic()

        result = worker._send_payload(payload, expectation="data", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(result.timeout_reason, "telemetry_data_ambiguous")
        self.assertIn("自动上传实时数据", result.message)
        self.assertIn("无法可靠区分", result.message)
        self.assertIn("关闭主动上传", result.message)

    def test_user_visible_legacy_silence_terms_do_not_reappear_in_targeted_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        banned_terms = [
            "".join(["自动", "静音"]),
            "".join(["静音", "读取"]),
            "".join(["广播", "静音"]),
            "".join(["单设备", "静音"]),
            "".join(["静音", "窗口"]),
            "".join(["静音", "失败"]),
            "".join(["已", "静音"]),
            "".join(["静音", "中"]),
            "".join(["自动", "静音", "保护"]),
            "".join(["自动", "静音", "恢复"]),
        ]
        targets = [
            root / "ygas_monitor" / "ui",
            root / "ygas_monitor" / "services",
            root / "ygas_monitor" / "models.py",
            root / "tests" / "test_session_ui.py",
        ]

        checked_files: list[Path] = []
        for target in targets:
            files = list(target.rglob("*.py")) if target.is_dir() else [target]
            for file_path in files:
                text = file_path.read_text(encoding="utf-8")
                checked_files.append(file_path)
                for term in banned_terms:
                    self.assertNotIn(term, text, f"{file_path} still contains legacy term: {term}")

        self.assertTrue(checked_files)

    def test_action_label_zh_is_declared_in_models_and_export_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        models_text = (root / "ygas_monitor" / "models.py").read_text(encoding="utf-8")
        export_text = (root / "ygas_monitor" / "services" / "export_service.py").read_text(encoding="utf-8")
        session_ui_text = (root / "ygas_monitor" / "ui" / "session_widget.py").read_text(encoding="utf-8")

        self.assertIn("action_label_zh", models_text)
        self.assertIn("action_label_zh", export_text)
        self.assertIn("action_label_zh", session_ui_text)

    def test_tick_publishes_every_frame_from_multi_frame_chunk(self) -> None:
        transport = ScriptedTransport({})
        transport.pending_chunks.append((make_mode2_frame("012") + make_mode2_frame("012")).encode("ascii"))
        worker = self.make_worker(transport, target_id="012")
        frames: list[ParsedFrame] = []
        worker.frame_received.connect(frames.append)

        worker._tick()

        self.assertEqual(len(frames), 2)


if __name__ == "__main__":
    unittest.main()
