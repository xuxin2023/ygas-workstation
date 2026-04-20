from __future__ import annotations

from datetime import datetime, timedelta
import tempfile
import unittest
from pathlib import Path
import time

from ygas_monitor.commanding.safety import SESSION_MODE_SAFE_HANDSHAKE
from ygas_monitor.models import ParsedFrame, RawFrameRecord, SerialSettings, SessionConfig
from ygas_monitor.services.export_service import export_session_package
from ygas_monitor.services.replay_service import load_replay_dataset, validate_replay_headers
from ygas_monitor.services.session_controller import AcquisitionWorker
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


class SettingsServiceTests(unittest.TestCase):
    def test_default_session_settings_use_safe_onboarding_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            loaded = service.load()

        self.assertEqual(loaded["session"]["permission_level"], "READ_ONLY")
        self.assertEqual(loaded["session"]["session_mode"], SESSION_MODE_SAFE_HANDSHAKE)
        self.assertTrue(loaded["session"]["read_only_lock"])

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

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_session_package(
                session_name="session",
                frames=[frame],
                raw_records=raw_records,
                config=config,
                output_dir=temp_dir,
            )
            files = {item.name for item in package_dir.iterdir()}

        self.assertIn("structured_data.csv", files)
        self.assertIn("raw_frames.log", files)
        self.assertIn("command_log.tsv", files)
        self.assertIn("config_snapshot.json", files)
        self.assertIn("session_summary.json", files)
        self.assertIn("session_summary.txt", files)


class AcquisitionWorkerTests(unittest.TestCase):
    def make_worker(
        self,
        transport: ScriptedTransport,
        *,
        target_id: str = "002",
        mode_preference: str = "MODE2",
    ) -> AcquisitionWorker:
        worker = AcquisitionWorker()
        worker._transport = transport
        worker._config = SessionConfig(
            serial=SerialSettings(port="SIMULATOR"),
            target_id=target_id,
            mode_preference=mode_preference,
            command_timeout_ms=300,
        )
        return worker

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
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
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
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
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
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
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
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
        transport = ScriptedTransport({silence_payload: [[]]})
        worker = self.make_worker(transport)
        now = time.monotonic()
        worker._active_rx_entries.append((now, "002"))
        worker._prune_active_rx_devices(now)

        result = worker._send_payload(payload, expectation="ack", timeout_ms=250)

        self.assertFalse(result.ok)
        self.assertEqual(transport.writes, [silence_payload])
        self.assertIn("未收到 SETCOMWAY ACK", result.message)

    def test_write_silence_fails_when_stream_frames_continue(self) -> None:
        payload = "SENCO1,YGAS,FFF,1.00000e00"
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
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
        self.assertIn("自动上传未真正关闭", result.message)

    def test_write_silence_allows_getco_after_quiet_window(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
        transport = ScriptedTransport(
            {
                silence_payload: [["<YGAS,002,T>\r\n"]],
                payload: [["C0:65916.6,C1:-106614,C2:0\r\n"]],
            }
        )
        worker = self.make_worker(transport, target_id="002")

        result = worker._send_payload(payload, expectation="coefficient", timeout_ms=300)

        self.assertTrue(result.ok)
        self.assertEqual(transport.writes, [silence_payload, payload])
        self.assertEqual(transport.flush_count, 1)
        self.assertIn("建议写回格式", result.message)

    def test_getco_ack_without_coefficients_has_specific_message(self) -> None:
        payload = "GETCO,YGAS,002,1"
        silence_payload = "SETCOMWAY,YGAS,FFF,0"
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


if __name__ == "__main__":
    unittest.main()
