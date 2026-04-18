"""CSV, session package, and diagnostic package export helpers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Iterable

from ..config import EXPORT_DIR, ensure_runtime_dirs
from ..models import ParsedFrame, RawFrameRecord, SessionConfig
from ..version import get_version_info


def export_frames_to_csv(frames: Iterable[ParsedFrame], output_path: str | Path | None = None) -> Path:
    rows = [frame.to_row() for frame in frames]
    if not rows:
        raise ValueError("当前没有可导出的结构化数据。")

    ensure_runtime_dirs()
    if output_path is None:
        output_path = EXPORT_DIR / f"ygas_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import pandas as pd  # type: ignore

        pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except Exception:
        import csv

        fieldnames = sorted({key for row in rows for key in row})
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path


def export_session_package(
    *,
    session_name: str,
    frames: Iterable[ParsedFrame],
    raw_records: Iterable[RawFrameRecord],
    config: SessionConfig,
    output_dir: str | Path | None = None,
    logger_path: str | Path | None = None,
    note: str = "",
) -> Path:
    frame_list = list(frames)
    raw_list = list(raw_records)
    if not frame_list and not raw_list:
        raise ValueError("当前会话没有可导出的帧或日志。")

    package_dir = _prepare_package_dir("session", output_dir)
    if frame_list:
        export_frames_to_csv(frame_list, package_dir / "structured_data.csv")

    _write_raw_log(raw_list, package_dir / "raw_frames.log")
    _write_command_log(raw_list, package_dir / "command_log.tsv")
    _write_json(package_dir / "config_snapshot.json", asdict(config))
    _write_json(package_dir / "session_summary.json", _build_summary(session_name, frame_list, raw_list, logger_path, note))
    _write_summary_text(package_dir / "session_summary.txt", session_name, frame_list, raw_list, config, logger_path, note)
    _copy_session_logger(logger_path, package_dir)
    return package_dir


def export_diagnostic_package(
    *,
    session_name: str,
    frames: Iterable[ParsedFrame],
    raw_records: Iterable[RawFrameRecord],
    config: SessionConfig,
    output_dir: str | Path | None = None,
    logger_path: str | Path | None = None,
    note: str = "",
) -> Path:
    frame_list = list(frames)
    raw_list = list(raw_records)
    package_dir = _prepare_package_dir("diagnostic", output_dir)
    _write_json(package_dir / "app_info.json", get_version_info())
    _write_json(package_dir / "config_snapshot.json", asdict(config))
    _write_json(package_dir / "recent_session_summary.json", _build_summary(session_name, frame_list, raw_list, logger_path, note))
    _write_raw_log(raw_list[-200:], package_dir / "recent_raw_frames.log")
    _write_command_log(raw_list[-200:], package_dir / "recent_command_log.tsv")
    _write_exception_log(raw_list[-200:], package_dir / "recent_exceptions.log")
    _write_text(package_dir / "session_note.txt", note or "--")
    _copy_session_logger(logger_path, package_dir)
    return package_dir


def _prepare_package_dir(prefix: str, output_dir: str | Path | None) -> Path:
    ensure_runtime_dirs()
    base_dir = Path(output_dir or EXPORT_DIR)
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_dir = base_dir / f"{prefix}_{stamp}"
    package_dir.mkdir(parents=True, exist_ok=True)
    return package_dir


def _copy_session_logger(logger_path: str | Path | None, package_dir: Path) -> None:
    if logger_path:
        source = Path(logger_path)
        if source.exists():
            shutil.copyfile(source, package_dir / "session_logger.log")


def _write_raw_log(records: list[RawFrameRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# timestamp\tdirection\tlevel\ttext\n")
        for record in records:
            clean = str(record.text).replace("\n", "\\n").replace("\r", "\\r")
            handle.write(
                f"{record.timestamp.isoformat(timespec='milliseconds')}\t{record.direction}\t{record.level}\t{clean}\n"
            )


def _write_command_log(records: list[RawFrameRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("timestamp\tcommand\tbroadcast\tlevel\ttext\n")
        for record in records:
            if record.direction != "TX":
                continue
            broadcast = "yes" if "target=FFF" in record.text or "broadcast=yes" in record.text else "no"
            command = str(record.text).split(",", 1)[0].replace("[FFF] ", "")
            handle.write(
                f"{record.timestamp.isoformat(timespec='milliseconds')}\t{command}\t{broadcast}\t{record.level}\t{record.text}\n"
            )


def _write_exception_log(records: list[RawFrameRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# timestamp\tlevel\ttext\n")
        for record in records:
            if record.level.upper() not in {"ERROR", "WARN"} and "F," not in record.text and "timeout" not in record.text.lower():
                continue
            handle.write(
                f"{record.timestamp.isoformat(timespec='milliseconds')}\t{record.level}\t{record.text}\n"
            )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(str(text or "") + "\n", encoding="utf-8")


def _build_summary(
    session_name: str,
    frames: list[ParsedFrame],
    records: list[RawFrameRecord],
    logger_path: str | Path | None,
    note: str = "",
) -> dict[str, object]:
    device_ids = sorted({frame.device_id for frame in frames if frame.device_id})
    tx_records = [record for record in records if record.direction == "TX"]
    rx_records = [record for record in records if record.direction == "RX"]
    error_records = [record for record in records if record.level.upper() in {"ERROR", "WARN"}]
    broadcast_count = sum(
        1 for record in tx_records if "target=FFF" in record.text or "broadcast=yes" in record.text
    )
    return {
        "session_name": session_name,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "frame_count": len(frames),
        "raw_record_count": len(records),
        "tx_count": len(tx_records),
        "rx_count": len(rx_records),
        "error_count": len(error_records),
        "broadcast_tx_count": broadcast_count,
        "device_ids": device_ids,
        "started_at": frames[0].timestamp.isoformat(timespec="seconds") if frames else None,
        "ended_at": frames[-1].timestamp.isoformat(timespec="seconds") if frames else None,
        "logger_path": str(logger_path) if logger_path else "",
        "note": note,
    }


def _write_summary_text(
    path: Path,
    session_name: str,
    frames: list[ParsedFrame],
    records: list[RawFrameRecord],
    config: SessionConfig,
    logger_path: str | Path | None,
    note: str = "",
) -> None:
    summary = _build_summary(session_name, frames, records, logger_path, note)
    lines = [
        f"Session: {session_name}",
        f"Exported At: {summary['exported_at']}",
        f"Frame Count: {summary['frame_count']}",
        f"Raw Record Count: {summary['raw_record_count']}",
        f"Broadcast TX Count: {summary['broadcast_tx_count']}",
        f"Devices: {', '.join(summary['device_ids']) if summary['device_ids'] else '--'}",
        f"Current Port: {config.serial.port}",
        f"Mode Preference: {config.mode_preference}",
        f"Acquisition Mode: {config.acquisition_mode}",
        f"Session Mode: {config.session_mode}",
        f"Read-only Lock: {config.read_only_lock}",
        f"Profile: {config.profile_name}",
        f"Target ID: {config.target_id}",
        f"Permission: {config.permission_level}",
        f"Command Timeout: {config.command_timeout_ms}",
        f"Logger Path: {logger_path or '--'}",
        f"Note: {note or '--'}",
    ]
    _write_text(path, "\n".join(lines))
