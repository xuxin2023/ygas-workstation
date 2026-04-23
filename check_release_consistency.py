from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

sys.dont_write_bytecode = True

from ygas_monitor import __version__
from ygas_monitor.version import APP_VERSION


# Negative-control constants: these stale rc2 values are intentionally kept only to assert
# that current release documents and FieldTrial artifacts do not contain them.
OLD_VERSION = "0.9.0-rc2"
OLD_SOURCE_SHA = "15DE815DB0A1A532F181DAE5BFCE1ECEF046B4D3C10B979324860EBDA2CF88C0"

WORKSPACE_MODE = "workspace"
SOURCE_ONLY_MODE = "source-only"

CURRENT_VERSIONED_RELEASE_DOCS = (
    "FIELD_TRIAL_DAILY_LOG_v{version}.md",
    "FIELD_TRIAL_ISSUE_TEMPLATE_v{version}.md",
    "FIELD_TRIAL_NOTES_v{version}.md",
    "FIELD_TRIAL_QUICK_CARD_v{version}.md",
    "FIELD_TRIAL_TEST_PLAN_v{version}.md",
    "MANIFEST_FIELD_TRIAL_v{version}.txt",
    "RELEASE_CHECKLIST_v{version}.md",
)
CURRENT_FIXED_RELEASE_DOCS = (
    "README.md",
    "README_FIELD_TRIAL.md",
    "RC_TRIAGE_RULES.md",
    "installer/GasAxisStudio.iss",
)
ROOT_RC2_FIELDTRIAL_FILES = (
    "FIELD_TRIAL_DAILY_LOG_v0.9.0-rc2.md",
    "FIELD_TRIAL_ISSUE_TEMPLATE_v0.9.0-rc2.md",
    "FIELD_TRIAL_NOTES_v0.9.0-rc2.md",
    "FIELD_TRIAL_QUICK_CARD_v0.9.0-rc2.md",
    "FIELD_TRIAL_TEST_PLAN_v0.9.0-rc2.md",
    "MANIFEST_FIELD_TRIAL_v0.9.0-rc2.txt",
    "RELEASE_CHECKLIST_v0.9.0-rc2.md",
)
FIELDTRIAL_TOP_LEVEL_FILES = (
    "GasAxisStudio_Source_v{version}.zip",
    "RELEASE_CHECKLIST_v{version}.md",
    "FIELD_TRIAL_NOTES_v{version}.md",
    "FIELD_TRIAL_TEST_PLAN_v{version}.md",
    "FIELD_TRIAL_ISSUE_TEMPLATE_v{version}.md",
    "FIELD_TRIAL_DAILY_LOG_v{version}.md",
    "FIELD_TRIAL_QUICK_CARD_v{version}.md",
    "RC_TRIAGE_RULES.md",
    "SHA256SUMS.txt",
    "README_FIELD_TRIAL.md",
    "MANIFEST_FIELD_TRIAL_v{version}.txt",
)
SOURCE_HASH_MATCH_FILES = (
    "ygas_monitor/ui/session_widget.py",
    "ygas_monitor/services/session_controller.py",
    "ygas_monitor/services/settings_service.py",
    "ygas_monitor/ui/main_window.py",
    "ygas_monitor/ui/widgets/charts.py",
    "tests/test_session_ui.py",
    "tests/test_services.py",
    "assets/help_zh_cn.md",
)
FORBIDDEN_BOUNDARY_PHRASES = ("不改 UI", "不改核心代码")
HEX_SHA_PATTERN = re.compile(r"\b[A-F0-9]{64}\b")
SOURCE_BLOCKED_SEGMENTS = {"__pycache__", ".pytest_cache", "build", "dist", "logs", "exports", "review_snapshot"}
VERSIONED_RELEASE_DOC_PREFIXES = (
    "FIELD_TRIAL_DAILY_LOG_v",
    "FIELD_TRIAL_ISSUE_TEMPLATE_v",
    "FIELD_TRIAL_NOTES_v",
    "FIELD_TRIAL_QUICK_CARD_v",
    "FIELD_TRIAL_TEST_PLAN_v",
    "MANIFEST_FIELD_TRIAL_v",
    "RELEASE_CHECKLIST_v",
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_sha_sums_text(text: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            raise ValueError(f"Invalid SHA256SUMS line: {line!r}")
        checksums[parts[-1]] = parts[0].upper()
    return checksums


def parse_sha_sums(path: Path) -> dict[str, str]:
    return parse_sha_sums_text(load_text(path))


def current_release_docs(project_root: Path) -> list[Path]:
    docs = [project_root / name.format(version=APP_VERSION) for name in CURRENT_VERSIONED_RELEASE_DOCS]
    docs.extend(project_root / relative for relative in CURRENT_FIXED_RELEASE_DOCS)
    return docs


def current_versioned_release_doc_names() -> set[str]:
    return {name.format(version=APP_VERSION) for name in CURRENT_VERSIONED_RELEASE_DOCS}


def fieldtrial_archive_dir(project_root: Path) -> Path:
    return project_root / "docs" / "archive" / f"v{OLD_VERSION}"


def source_zip_path(project_root: Path) -> Path:
    return project_root / "dist" / f"GasAxisStudio_Source_v{APP_VERSION}.zip"


def fieldtrial_zip_path(project_root: Path) -> Path:
    return project_root / "dist" / f"GasAxisStudio_FieldTrial_v{APP_VERSION}.zip"


def fieldtrial_sidecar_path(project_root: Path) -> Path:
    return project_root / "dist" / f"GasAxisStudio_FieldTrial_v{APP_VERSION}.zip.sha256"


def sha_sums_path(project_root: Path) -> Path:
    return project_root / "SHA256SUMS.txt"


def detect_mode(project_root: Path) -> str:
    return WORKSPACE_MODE if (project_root / "dist").exists() else SOURCE_ONLY_MODE


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate GasAxis Studio release consistency.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--workspace", action="store_true", help="Run workspace pre-release checks.")
    group.add_argument("--source-only", action="store_true", help="Run source-only checks without dist/docs/archive requirements.")
    return parser.parse_args()


def selected_mode(project_root: Path, args: argparse.Namespace) -> str:
    if args.workspace:
        return WORKSPACE_MODE
    if args.source_only:
        return SOURCE_ONLY_MODE
    return detect_mode(project_root)


def source_entry_is_blocked(entry_name: str) -> bool:
    normalized = entry_name.replace("\\", "/").strip("/")
    if not normalized:
        return False
    path = PurePosixPath(normalized)
    parts = path.parts
    if path.name == "SHA256SUMS.txt":
        return True
    if path.suffix.lower() == ".pyc":
        return True
    if any(part in SOURCE_BLOCKED_SEGMENTS for part in parts):
        return True
    if len(parts) >= 2 and parts[:2] == ("data", "user_settings.json"):
        return True
    if len(parts) >= 2 and parts[:2] == ("docs", "archive"):
        return True
    return False


def is_versioned_release_doc_name(file_name: str) -> bool:
    return any(file_name.startswith(prefix) for prefix in VERSIONED_RELEASE_DOC_PREFIXES)


def validate_current_docs(project_root: Path, failures: list[str]) -> None:
    for path in current_release_docs(project_root):
        if not path.exists():
            failures.append(f"Missing current release document: {path.relative_to(project_root).as_posix()}")
            continue
        text = load_text(path)
        if OLD_VERSION in text:
            failures.append(f"{path.relative_to(project_root).as_posix()} still references {OLD_VERSION}")
        if OLD_SOURCE_SHA in text:
            failures.append(f"{path.relative_to(project_root).as_posix()} still references the stale source SHA")
        for phrase in FORBIDDEN_BOUNDARY_PHRASES:
            if phrase in text:
                failures.append(
                    f"{path.relative_to(project_root).as_posix()} still contains outdated boundary phrase: {phrase}"
                )


def validate_root_current_release_docs(project_root: Path, failures: list[str]) -> None:
    expected_names = current_versioned_release_doc_names()
    for path in project_root.iterdir():
        if not path.is_file():
            continue
        if not is_versioned_release_doc_name(path.name):
            continue
        if path.name not in expected_names:
            failures.append(f"Workspace root still contains non-current release document: {path.name}")


def validate_static_files(project_root: Path, failures: list[str]) -> None:
    if __version__ != APP_VERSION:
        failures.append(f"ygas_monitor.__version__ ({__version__}) does not match APP_VERSION ({APP_VERSION})")

    source_script = load_text(project_root / "package_source_zip.ps1")
    source_fragments = (
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
        "data",
        "user_settings.json",
    )
    for fragment in source_fragments:
        if fragment not in source_script:
            failures.append(f"package_source_zip.ps1 is missing required fragment: {fragment}")

    fieldtrial_script = load_text(project_root / "package_fieldtrial_zip.ps1")
    fieldtrial_fragments = (
        "Write-ChecksumManifest -Version $version -Files $filesForChecksum",
        "$filesToPackage = @($filesForChecksum + $checksumPath)",
        "SHA256SUMS.txt",
        "python $releaseConsistencyScript",
    )
    for fragment in fieldtrial_fragments:
        if fragment not in fieldtrial_script:
            failures.append(f"package_fieldtrial_zip.ps1 is missing required fragment: {fragment}")


def validate_workspace_root(project_root: Path, failures: list[str]) -> None:
    for name in ROOT_RC2_FIELDTRIAL_FILES:
        if (project_root / name).exists():
            failures.append(f"Workspace root still contains archived rc2 field-trial file: {name}")

    archive_dir = fieldtrial_archive_dir(project_root)
    if not archive_dir.is_dir():
        failures.append(f"Missing archive directory for historical rc2 field-trial files: {archive_dir.as_posix()}")
        return

    for name in ROOT_RC2_FIELDTRIAL_FILES:
        archived_path = archive_dir / name
        if not archived_path.exists():
            failures.append(f"Archived rc2 field-trial file is missing: {archived_path.as_posix()}")


def validate_source_tree(project_root: Path, failures: list[str]) -> None:
    for path in project_root.rglob("*"):
        relative = path.relative_to(project_root).as_posix()
        if source_entry_is_blocked(relative):
            failures.append(f"Source-only tree contains blocked entry: {relative}")
            break


def validate_source_zip(project_root: Path, failures: list[str], source_zip: Path) -> None:
    expected_doc_names = current_versioned_release_doc_names()
    with ZipFile(source_zip) as archive:
        entries = archive.namelist()
        if any("\\" in entry for entry in entries):
            failures.append("Source zip contains backslash path separators")

        blocked_entries = [entry for entry in entries if source_entry_is_blocked(entry)]
        if blocked_entries:
            failures.append(f"Source zip contains blocked entries: {blocked_entries[0]}")

        non_current_docs = []
        for entry in entries:
            entry_name = PurePosixPath(entry.rstrip("/")).name
            if is_versioned_release_doc_name(entry_name) and entry_name not in expected_doc_names:
                non_current_docs.append(entry_name)
        if non_current_docs:
            failures.append(f"Source zip still contains non-current release docs: {sorted(non_current_docs)[0]}")

        for relative_path in SOURCE_HASH_MATCH_FILES:
            workspace_path = project_root / relative_path
            if not workspace_path.exists():
                failures.append(f"Missing key workspace file for source zip verification: {relative_path}")
                continue
            normalized = relative_path.replace("\\", "/")
            if normalized not in entries:
                failures.append(f"Source zip is missing key file: {normalized}")
                continue
            if bytes_sha256(archive.read(normalized)) != file_sha256(workspace_path):
                failures.append(f"Source zip key file hash mismatch: {normalized}")


def validate_fieldtrial_zip(
    failures: list[str],
    field_zip: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    expected_root = f"GasAxisStudio_FieldTrial_v{APP_VERSION}"
    expected_file_entries = {
        f"{expected_root}/{name.format(version=APP_VERSION)}" for name in FIELDTRIAL_TOP_LEVEL_FILES
    }

    with ZipFile(field_zip) as archive:
        entries = archive.namelist()
        file_entries = {entry for entry in entries if entry and not entry.endswith("/")}

        if any("\\" in entry for entry in entries):
            failures.append("FieldTrial zip contains backslash path separators")

        top_levels = sorted({entry.split("/")[0] for entry in entries if entry})
        if top_levels != [expected_root]:
            failures.append(f"FieldTrial zip top-level directory is not clean ASCII root: {top_levels}")

        missing_entries = sorted(expected_file_entries - file_entries)
        if missing_entries:
            failures.append(f"FieldTrial zip is missing required entries: {missing_entries[0]}")

        unexpected_entries = sorted(file_entries - expected_file_entries)
        if unexpected_entries:
            failures.append(f"FieldTrial zip contains unexpected entries: {unexpected_entries[0]}")

        archive_entries = sorted(entry for entry in file_entries if entry.startswith(f"{expected_root}/docs/archive/"))
        if archive_entries:
            failures.append(f"FieldTrial zip must not distribute archived docs: {archive_entries[0]}")

        legacy_source_entries = [
            entry for entry in file_entries if entry.endswith(f"GasAxisStudio_Source_v{OLD_VERSION}.zip")
        ]
        if legacy_source_entries:
            failures.append("FieldTrial zip still contains the rc2 source zip")

        checksum_entry = f"{expected_root}/SHA256SUMS.txt"
        if checksum_entry not in file_entries:
            failures.append("FieldTrial zip does not contain a top-level SHA256SUMS.txt")
            return {}, {}

        checksum_map = parse_sha_sums_text(archive.read(checksum_entry).decode("utf-8"))
        expected_checksummed_names = {
            PurePosixPath(entry).name for entry in file_entries if PurePosixPath(entry).name != "SHA256SUMS.txt"
        }

        if set(checksum_map) != expected_checksummed_names:
            missing = sorted(expected_checksummed_names - set(checksum_map))
            extra = sorted(set(checksum_map) - expected_checksummed_names)
            if missing:
                failures.append(f"FieldTrial SHA256SUMS.txt is missing packaged files: {missing[0]}")
            if extra:
                failures.append(f"FieldTrial SHA256SUMS.txt lists unexpected files: {extra[0]}")

        embedded_hash_map: dict[str, str] = {}
        for file_name in sorted(expected_checksummed_names):
            entry_name = f"{expected_root}/{file_name}"
            embedded_hash = bytes_sha256(archive.read(entry_name))
            embedded_hash_map[file_name] = embedded_hash
            listed_hash = checksum_map.get(file_name)
            if listed_hash != embedded_hash:
                failures.append(f"FieldTrial SHA256SUMS.txt hash mismatch for {file_name}")

        return checksum_map, embedded_hash_map


def validate_dist_outputs(project_root: Path, failures: list[str]) -> None:
    source_zip = source_zip_path(project_root)
    field_zip = fieldtrial_zip_path(project_root)
    sidecar = fieldtrial_sidecar_path(project_root)
    root_sha_sums = sha_sums_path(project_root)

    for path in (source_zip, field_zip, sidecar, root_sha_sums):
        if not path.exists():
            failures.append(f"Missing required artifact for release consistency check: {path.name}")
    if failures:
        return

    source_sha = file_sha256(source_zip)
    field_sha = file_sha256(field_zip)
    root_checksum_map = parse_sha_sums(root_sha_sums)
    expected_source_name = source_zip.name
    if root_checksum_map.get(expected_source_name) != source_sha:
        failures.append("Root SHA256SUMS.txt does not match the current source zip hash")

    sidecar_line = sidecar.read_text(encoding="utf-8").strip()
    expected_sidecar = f"{field_sha}  {field_zip.name}"
    if sidecar_line != expected_sidecar:
        failures.append(".sha256 sidecar does not match the current FieldTrial zip hash")

    validate_source_zip(project_root, failures, source_zip)
    embedded_checksum_map, embedded_hash_map = validate_fieldtrial_zip(failures, field_zip)
    if embedded_checksum_map and embedded_checksum_map != root_checksum_map:
        failures.append("FieldTrial top-level SHA256SUMS.txt does not match the workspace SHA256SUMS.txt")

    if embedded_checksum_map:
        embedded_source_hash = embedded_hash_map.get(expected_source_name)
        if embedded_source_hash != source_sha:
            failures.append("Embedded source zip inside FieldTrial bundle does not match dist source zip SHA256")
        if embedded_checksum_map.get(expected_source_name) != source_sha:
            failures.append("FieldTrial SHA256SUMS.txt does not match the embedded source zip SHA256")

    current_hashes = {source_sha, field_sha}
    for path in current_release_docs(project_root):
        if not path.exists():
            continue
        for token in HEX_SHA_PATTERN.findall(load_text(path)):
            if token not in current_hashes:
                failures.append(f"{path.relative_to(project_root).as_posix()} contains an unexpected SHA256 literal: {token}")


def run_checks(project_root: Path, mode: str) -> list[str]:
    failures: list[str] = []
    validate_static_files(project_root, failures)
    validate_current_docs(project_root, failures)
    validate_root_current_release_docs(project_root, failures)
    if mode == WORKSPACE_MODE:
        validate_workspace_root(project_root, failures)
        validate_dist_outputs(project_root, failures)
    else:
        validate_source_tree(project_root, failures)
    return failures


def main() -> int:
    project_root = Path(__file__).resolve().parent
    args = parse_args()
    mode = selected_mode(project_root, args)
    failures = run_checks(project_root, mode)

    if failures:
        print(f"Release consistency check failed ({mode} mode):")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print(f"Release consistency check passed for {APP_VERSION} ({mode} mode).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
