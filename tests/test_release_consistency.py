from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_release_consistency as release_consistency
from ygas_monitor import __version__
from ygas_monitor.version import APP_VERSION


# Negative-control constants: these stale rc2 values are intentionally kept only to assert
# that current release documents and FieldTrial artifacts do not contain them.
OLD_VERSION = "0.9.0-rc2"
OLD_SOURCE_SHA = "15DE815DB0A1A532F181DAE5BFCE1ECEF046B4D3C10B979324860EBDA2CF88C0"


class ReleaseConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_root = Path(__file__).resolve().parents[1]

    def _copy_source_only_fixture(self, destination_root: Path) -> None:
        ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "dist", "logs", "exports", "review_snapshot")
        relative_paths = [
            "assets",
            "installer",
            "tests",
            "ygas_monitor",
            "README.md",
            "README_FIELD_TRIAL.md",
            "RC_TRIAGE_RULES.md",
            "PACKAGING.md",
            "check_packaging.ps1",
            "check_release_consistency.py",
            "clean_packaging.ps1",
            "clean_review_snapshot.ps1",
            "package_fieldtrial_zip.ps1",
            "package_source_zip.ps1",
            f"FIELD_TRIAL_DAILY_LOG_v{APP_VERSION}.md",
            f"FIELD_TRIAL_ISSUE_TEMPLATE_v{APP_VERSION}.md",
            f"FIELD_TRIAL_NOTES_v{APP_VERSION}.md",
            f"FIELD_TRIAL_QUICK_CARD_v{APP_VERSION}.md",
            f"FIELD_TRIAL_TEST_PLAN_v{APP_VERSION}.md",
            f"MANIFEST_FIELD_TRIAL_v{APP_VERSION}.txt",
            f"RELEASE_CHECKLIST_v{APP_VERSION}.md",
        ]
        for relative in relative_paths:
            source = self.project_root / relative
            destination = destination_root / relative
            if source.is_dir():
                shutil.copytree(source, destination, ignore=ignore)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

    def test_app_version_and_package_version_match(self) -> None:
        self.assertEqual(__version__, APP_VERSION)
        self.assertEqual(APP_VERSION, "0.9.0-rc5")

    def test_negative_control_constants_are_comment_documented(self) -> None:
        for relative in ("check_release_consistency.py", "tests/test_release_consistency.py"):
            text = (self.project_root / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn("Negative-control constants:", text)

    def test_release_support_files_do_not_reference_old_version(self) -> None:
        files = (
            self.project_root / "README.md",
            self.project_root / "RC_TRIAGE_RULES.md",
            self.project_root / "installer" / "GasAxisStudio.iss",
        )
        for path in files:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn(OLD_VERSION, text)
                self.assertNotIn(OLD_SOURCE_SHA, text)

    def test_current_release_docs_do_not_contain_old_sha_or_wrong_boundary_copy(self) -> None:
        forbidden_phrases = release_consistency.FORBIDDEN_BOUNDARY_PHRASES
        for path in release_consistency.current_release_docs(self.project_root):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(self.project_root).as_posix()):
                self.assertNotIn(OLD_VERSION, text)
                self.assertNotIn(OLD_SOURCE_SHA, text)
                for phrase in forbidden_phrases:
                    self.assertNotIn(phrase, text)

    def test_release_docs_point_to_checksum_sources(self) -> None:
        quick_card = (self.project_root / f"FIELD_TRIAL_QUICK_CARD_v{APP_VERSION}.md").read_text(encoding="utf-8")
        test_plan = (self.project_root / f"FIELD_TRIAL_TEST_PLAN_v{APP_VERSION}.md").read_text(encoding="utf-8")
        checklist = (self.project_root / f"RELEASE_CHECKLIST_v{APP_VERSION}.md").read_text(encoding="utf-8")

        self.assertIn("SHA256SUMS.txt", quick_card)
        self.assertIn(".sha256", quick_card)
        self.assertIn("SHA256SUMS.txt", test_plan)
        self.assertIn("RELEASE_CHECKLIST", test_plan)
        self.assertIn("SHA256SUMS.txt", checklist)
        self.assertIn(".sha256", checklist)

    def test_workspace_root_archives_rc2_field_trial_materials(self) -> None:
        archive_dir = release_consistency.fieldtrial_archive_dir(self.project_root)
        self.assertTrue(archive_dir.is_dir())

        for name in release_consistency.ROOT_RC2_FIELDTRIAL_FILES:
            with self.subTest(name=name):
                self.assertFalse((self.project_root / name).exists())
                self.assertTrue((archive_dir / name).exists())

    def test_package_source_script_covers_release_suite_and_exclusions(self) -> None:
        script = (self.project_root / "package_source_zip.ps1").read_text(encoding="utf-8")

        required_fragments = (
            "tests/test_protocol.py",
            "tests/test_registry.py",
            "tests/test_services.py",
            "tests/test_session_ui.py",
            "tests/test_charts.py",
            "tests/test_export.py",
            "tests/test_rc1.py",
            "tests/test_release_consistency.py",
            "--collect-only",
            '("-m", "pytest", "-q")',
            "SHA256SUMS.txt",
            '"docs"',
            '"archive"',
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
            "logs",
            "exports",
            "review_snapshot",
            "data",
            "user_settings.json",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)

    def test_package_source_script_cleans_workspace_before_running_full_suite(self) -> None:
        script = (self.project_root / "package_source_zip.ps1").read_text(encoding="utf-8")

        self.assertIn("Invoke-CheckedScript -ScriptPath $cleanBeforeTests", script)
        self.assertLess(
            script.index("Invoke-CheckedScript -ScriptPath $cleanBeforeTests"),
            script.index('foreach ($arguments in $testCommands) {'),
        )
        self.assertIn("if (-not $?)", script)

    def test_package_fieldtrial_script_generates_checksum_before_packaging(self) -> None:
        script = (self.project_root / "package_fieldtrial_zip.ps1").read_text(encoding="utf-8")

        self.assertIn("Write-ChecksumManifest -Version $version -Files $filesForChecksum", script)
        self.assertIn("$filesToPackage = @($filesForChecksum + $checksumPath)", script)
        self.assertIn("python $releaseConsistencyScript --workspace", script)
        self.assertLess(
            script.index("Write-ChecksumManifest -Version $version -Files $filesForChecksum"),
            script.index("$filesToPackage = @($filesForChecksum + $checksumPath)"),
        )

    def test_source_entry_block_rules_cover_sha_manifest_and_archive(self) -> None:
        blocked_entries = (
            "SHA256SUMS.txt",
            "docs/archive/v0.9.0-rc2/FIELD_TRIAL_NOTES_v0.9.0-rc2.md",
            "build/output.bin",
            f"dist/GasAxisStudio_Source_v{APP_VERSION}.zip",
            "logs/session.log",
            "exports/run.csv",
            "review_snapshot/GasAxisStudio_Workspace_v0.9.0-rc5/README.md",
            "__pycache__/mod.cpython-311.pyc",
            ".pytest_cache/v/cache/nodeids",
            "data/user_settings.json",
        )
        allowed_entries = (
            "README.md",
            "docs/notes.md",
            "assets/help_zh_cn.md",
            "ygas_monitor/version.py",
        )

        for entry in blocked_entries:
            with self.subTest(entry=entry):
                self.assertTrue(release_consistency.source_entry_is_blocked(entry))

        for entry in allowed_entries:
            with self.subTest(entry=entry):
                self.assertFalse(release_consistency.source_entry_is_blocked(entry))

    def test_parse_sha_sums_text_ignores_comments_and_preserves_file_names(self) -> None:
        text = "\n".join(
            [
                "# comment",
                "",
                f"ABCDEF  GasAxisStudio_Source_v{APP_VERSION}.zip",
                "123456  README_FIELD_TRIAL.md",
            ]
        )

        self.assertEqual(
            release_consistency.parse_sha_sums_text(text),
            {
                f"GasAxisStudio_Source_v{APP_VERSION}.zip": "ABCDEF",
                "README_FIELD_TRIAL.md": "123456",
            },
        )

    def test_source_hash_match_files_cover_rc4_delivery_sources(self) -> None:
        expected = {
            "ygas_monitor/ui/session_widget.py",
            "ygas_monitor/services/session_controller.py",
            "ygas_monitor/services/settings_service.py",
            "ygas_monitor/ui/main_window.py",
            "ygas_monitor/ui/widgets/charts.py",
            "tests/test_session_ui.py",
            "tests/test_services.py",
            "assets/help_zh_cn.md",
        }
        self.assertEqual(set(release_consistency.SOURCE_HASH_MATCH_FILES), expected)

    def test_release_consistency_source_only_mode_does_not_require_dist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            self._copy_source_only_fixture(fixture_root)

            failures = release_consistency.run_checks(fixture_root, release_consistency.SOURCE_ONLY_MODE)

        self.assertEqual(failures, [])

    def test_release_consistency_workspace_mode_requires_fieldtrial_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            self._copy_source_only_fixture(fixture_root)

            failures = release_consistency.run_checks(fixture_root, release_consistency.WORKSPACE_MODE)

        self.assertTrue(any("Missing required artifact for release consistency check" in failure for failure in failures))

    def test_source_zip_can_run_source_only_consistency_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture_root = Path(temp_dir)
            self._copy_source_only_fixture(fixture_root)

            result = subprocess.run(
                [sys.executable, "check_release_consistency.py", "--source-only"],
                cwd=fixture_root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("source-only mode", result.stdout)


if __name__ == "__main__":
    unittest.main()
