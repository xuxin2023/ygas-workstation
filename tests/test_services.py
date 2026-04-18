from __future__ import annotations

from datetime import datetime, timedelta
import tempfile
import unittest
from pathlib import Path

from ygas_monitor.models import ParsedFrame, RawFrameRecord, SerialSettings, SessionConfig
from ygas_monitor.services.export_service import export_session_package
from ygas_monitor.services.replay_service import load_replay_dataset, validate_replay_headers
from ygas_monitor.services.settings_service import DEFAULT_SETTINGS, SettingsService


class SettingsServiceTests(unittest.TestCase):
    def test_settings_roundtrip_preserves_recent_connection_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            payload = {
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


if __name__ == "__main__":
    unittest.main()
