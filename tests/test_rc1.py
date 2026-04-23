from __future__ import annotations

import ast
from datetime import datetime
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import zipfile

from ygas_monitor import config as config_module
from ygas_monitor.commanding.registry import CommandRegistry
from ygas_monitor.commanding.safety import (
    SAFE_QUERY_COMMAND_IDS,
    SESSION_MODE_ENGINEERING,
    SESSION_MODE_SAFE_HANDSHAKE,
    can_execute_command,
    is_read_only_command,
)
from ygas_monitor.models import ParsedFrame, RawFrameRecord, SerialSettings, SessionChangeEntry, SessionConfig
from ygas_monitor.services.export_service import export_diagnostic_package
from ygas_monitor.services.settings_service import SettingsService
from ygas_monitor.version import APP_VERSION, environment_summary_text, get_version_info


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
        change_entries: list[SessionChangeEntry] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            package_dir = export_diagnostic_package(
                session_name="diag",
                frames=[frame],
                raw_records=records,
                config=config,
                parameter_change_entries=change_entries,
                output_dir=temp_dir,
                note="现场联调备注",
            )
            files = {item.name for item in package_dir.iterdir()}
            note_text = (package_dir / "session_note.txt").read_text(encoding="utf-8")
            journal_json = (package_dir / "parameter_change_journal.json").read_text(encoding="utf-8")
            journal_csv = (package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig")
            summary_json = json.loads((package_dir / "recent_session_summary.json").read_text(encoding="utf-8"))

        self.assertIn("app_info.json", files)
        self.assertIn("config_snapshot.json", files)
        self.assertIn("recent_session_summary.json", files)
        self.assertIn("recent_raw_frames.log", files)
        self.assertIn("recent_command_log.tsv", files)
        self.assertIn("recent_exceptions.log", files)
        self.assertIn("parameter_change_journal.json", files)
        self.assertIn("parameter_change_journal.csv", files)
        self.assertIn("session_note.txt", files)
        self.assertEqual(journal_json.strip(), "[]")
        self.assertIn("verification_status", journal_csv)
        self.assertIn("现场联调备注", note_text)
        self.assertEqual(summary_json["parameter_change_count"], 0)
        self.assertEqual(summary_json["verified_consistent_count"], 0)
        self.assertEqual(summary_json["ack_only_unverified_count"], 0)
        self.assertEqual(summary_json["unconfirmed_change_count"], 0)
        self.assertEqual(summary_json["failed_change_count"], 0)


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


class StaticCheckTests(unittest.TestCase):
    def test_no_duplicate_method_names_within_same_class(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        duplicates: list[str] = []
        for path in (project_root / "ygas_monitor").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                methods: dict[str, list[int]] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.setdefault(item.name, []).append(item.lineno)
                for name, lines in methods.items():
                    if len(lines) > 1:
                        duplicates.append(f"{path.name}:{node.name}.{name}:{lines}")

        self.assertEqual(duplicates, [])

    def test_docs_and_tooltips_use_pause_upload_read_terms(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        files_to_check = [
            project_root / "README.md",
            project_root / "PACKAGING.md",
            project_root / "assets" / "help_zh_cn.md",
            project_root / "ygas_monitor" / "ui" / "widgets" / "command_cards.py",
        ]
        forbidden_terms = ["自动静音", "静音读取", "广播静音", "静音窗口"]
        for path in files_to_check:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for forbidden in forbidden_terms:
                    self.assertNotIn(forbidden, text)
        help_text = (project_root / "assets" / "help_zh_cn.md").read_text(encoding="utf-8")
        readme_text = (project_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("现场试用注意事项", readme_text)
        self.assertIn("现场试用注意事项", help_text)
        self.assertIn("暂停上传后读取", help_text)
        self.assertIn("不会停止设备测量", help_text)
        self.assertIn("临时发送 `SETCOMWAY=0`", help_text)

    @staticmethod
    def _prepare_check_packaging_workspace(root: Path) -> Path:
        project_root = Path(__file__).resolve().parents[1]
        (root / "ygas_monitor").mkdir(parents=True, exist_ok=True)
        shutil.copy2(project_root / "check_packaging.ps1", root / "check_packaging.ps1")
        shutil.copy2(project_root / "ygas_monitor" / "version.py", root / "ygas_monitor" / "version.py")
        return root / "check_packaging.ps1"

    def test_check_packaging_validates_zip_segments_and_ascii_root(self) -> None:
        cases = (
            (
                "nested_pycache",
                {f"GasAxisStudio_Source_v{APP_VERSION}/ygas_monitor/__pycache__/x.pyc": "compiled"},
                False,
                "__pycache__",
            ),
            (
                "non_ascii_top_level",
                {"气体分析仪实时数据/main.py": "print('hi')\n"},
                False,
                "ASCII",
            ),
            (
                "clean_zip",
                {"README.md": "# clean\n", "ygas_monitor/version.py": f'APP_VERSION = "{APP_VERSION}"\n'},
                True,
                "passed",
            ),
        )
        for name, entries, should_pass, expected_text in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir)
                script_path = self._prepare_check_packaging_workspace(workspace)
                zip_path = workspace / f"GasAxisStudio_Source_v{APP_VERSION}.zip"
                with zipfile.ZipFile(zip_path, "w") as archive:
                    for entry_name, content in entries.items():
                        archive.writestr(entry_name, content)

                result = subprocess.run(
                    [
                        "powershell",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script_path),
                        "-ZipPath",
                        str(zip_path),
                    ],
                    cwd=workspace,
                    capture_output=True,
                )
                stdout = result.stdout.decode("utf-8", errors="ignore")
                stderr = result.stderr.decode("utf-8", errors="ignore")
                output = f"{stdout}\n{stderr}"
                if should_pass:
                    self.assertEqual(result.returncode, 0, output)
                else:
                    self.assertNotEqual(result.returncode, 0, output)
                self.assertIn(expected_text, output)

    def test_version_and_source_package_script_are_aligned_to_app_version(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        package_script = (project_root / "package_source_zip.ps1").read_text(encoding="utf-8")
        check_script = (project_root / "check_packaging.ps1").read_text(encoding="utf-8")

        self.assertIn("GasAxisStudio_Source_v$version.zip", package_script)
        self.assertIn("GasAxisStudio_Source_v$version.zip", check_script)
        self.assertEqual(APP_VERSION, "0.9.0-rc5")


if __name__ == "__main__":
    unittest.main()
