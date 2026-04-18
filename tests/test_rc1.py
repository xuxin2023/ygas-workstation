from __future__ import annotations

from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ygas_monitor import config as config_module
from ygas_monitor.commanding.registry import CommandRegistry
from ygas_monitor.commanding.safety import (
    SAFE_QUERY_COMMAND_IDS,
    SESSION_MODE_ENGINEERING,
    SESSION_MODE_SAFE_HANDSHAKE,
    can_execute_command,
    is_read_only_command,
)
from ygas_monitor.models import ParsedFrame, RawFrameRecord, SerialSettings, SessionConfig
from ygas_monitor.services.export_service import export_diagnostic_package
from ygas_monitor.services.settings_service import SettingsService
from ygas_monitor.version import environment_summary_text, get_version_info


class RuntimeDirectoryTests(unittest.TestCase):
    def test_ensure_runtime_dirs_creates_user_writable_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings_dir = root / "settings"
            local_state_dir = root / "local"
            log_dir = local_state_dir / "logs"
            export_dir = local_state_dir / "exports"
            cache_dir = local_state_dir / "cache"
            replay_dir = cache_dir / "replay"

            with mock.patch.object(config_module, "SETTINGS_DIR", settings_dir), mock.patch.object(
                config_module, "LOCAL_STATE_DIR", local_state_dir
            ), mock.patch.object(config_module, "LOG_DIR", log_dir), mock.patch.object(
                config_module, "EXPORT_DIR", export_dir
            ), mock.patch.object(config_module, "CACHE_DIR", cache_dir), mock.patch.object(
                config_module, "REPLAY_CACHE_DIR", replay_dir
            ):
                config_module.ensure_runtime_dirs()

            self.assertTrue(settings_dir.exists())
            self.assertTrue(log_dir.exists())
            self.assertTrue(export_dir.exists())
            self.assertTrue(replay_dir.exists())


class SettingsMigrationTests(unittest.TestCase):
    def test_legacy_settings_file_is_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy_path = root / "legacy_settings.json"
            new_path = root / "new_settings.json"
            legacy_path.write_text(
                '{"session": {"port": "COM35", "mode_preference": "MODE2"}, "paths": {"export_dir": "X"}}',
                encoding="utf-8",
            )

            with mock.patch("ygas_monitor.services.settings_service.OLD_SETTINGS_PATH", legacy_path):
                service = SettingsService(new_path)
                payload = service.load()

            self.assertEqual(payload["session"]["port"], "COM35")
            self.assertTrue(new_path.exists())
            self.assertIn("COM35", new_path.read_text(encoding="utf-8"))


class SafetyTests(unittest.TestCase):
    def test_safe_handshake_commands_are_all_read_only(self) -> None:
        registry = CommandRegistry()
        for command_id in SAFE_QUERY_COMMAND_IDS:
            definition = registry.get(command_id)
            self.assertTrue(is_read_only_command(definition), command_id)

    def test_read_only_lock_blocks_write_command(self) -> None:
        registry = CommandRegistry()
        definition = registry.get("FTD")
        ok, reason = can_execute_command(
            definition,
            connected=True,
            session_mode=SESSION_MODE_ENGINEERING,
            read_only_lock=True,
            replay_running=False,
        )
        self.assertFalse(ok)
        self.assertIn("只读会话锁", reason)

    def test_safe_handshake_mode_allows_query_command(self) -> None:
        registry = CommandRegistry()
        definition = registry.get("MODE_QUERY")
        ok, reason = can_execute_command(
            definition,
            connected=True,
            session_mode=SESSION_MODE_SAFE_HANDSHAKE,
            read_only_lock=False,
            replay_running=False,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")


class DiagnosticPackageTests(unittest.TestCase):
    def test_export_diagnostic_package_writes_expected_files(self) -> None:
        now = datetime.now()
        frame = ParsedFrame(
            timestamp=now,
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879},
            status="0001",
            extras=[],
        )
        records = [
            RawFrameRecord(timestamp=now, direction="TX", text="[FFF] MODE,YGAS,FFF,1 | target=FFF | broadcast=yes"),
            RawFrameRecord(timestamp=now, direction="RX", text=frame.raw),
            RawFrameRecord(timestamp=now, direction="SYS", text="串口异常断开", level="ERROR"),
        ]
        config = SessionConfig(serial=SerialSettings(port="COM35"), target_id="001", session_name="diag")

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_diagnostic_package(
                session_name="diag",
                frames=[frame],
                raw_records=records,
                config=config,
                output_dir=temp_dir,
                note="现场联调备注",
            )
            files = {item.name for item in package_dir.iterdir()}
            note_text = (package_dir / "session_note.txt").read_text(encoding="utf-8")

        self.assertIn("app_info.json", files)
        self.assertIn("config_snapshot.json", files)
        self.assertIn("recent_session_summary.json", files)
        self.assertIn("recent_raw_frames.log", files)
        self.assertIn("recent_command_log.tsv", files)
        self.assertIn("recent_exceptions.log", files)
        self.assertIn("session_note.txt", files)
        self.assertIn("现场联调备注", note_text)


class VersionInfoTests(unittest.TestCase):
    def test_version_info_exposes_paths_and_summary(self) -> None:
        info = get_version_info()
        summary = environment_summary_text()

        self.assertIn("app_name", info)
        self.assertIn("version", info)
        self.assertIn("settings_dir", info)
        self.assertIn("log_dir", info)
        self.assertIn(str(info["version"]), summary)
        self.assertIn(str(info["settings_dir"]), summary)
        self.assertIn(str(info["log_dir"]), summary)


if __name__ == "__main__":
    unittest.main()
