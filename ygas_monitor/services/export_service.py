"""CSV, session package, and diagnostic package export helpers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import csv
import json
from pathlib import Path
import shutil
from typing import Iterable

from ..config import DEFAULT_HISTORY_SIZE, EXPORT_DIR, ensure_runtime_dirs
from ..models import (
    ParsedFrame,
    RawFrameRecord,
    SessionChangeEntry,
    SessionCommandLogEntry,
    SessionConfig,
    action_label_zh,
    enrich_auto_upload_detail_text,
)
from ..protocols.ygas import PARSE_MODE_AUTO, YGasProtocol
from ..version import get_version_info

COMMAND_LOG_CACHE_LIMIT = 200
STRUCTURED_COMMAND_LOG_FIELDNAMES = [
    "timestamp",
    "action_type",
    "action_label_zh",
    "payload",
    "parent_command",
    "command_target_id",
    "expected_device_id",
    "response_device_id",
    "effective_scope",
    "result",
    "source_page",
    "level",
    "original_auto_upload_state",
    "restore_policy",
    "restore_attempted",
    "restore_result",
    "detail_text",
]
RAW_COMMAND_LOG_FIELDNAMES = ["timestamp", "command", "broadcast", "level", "text"]


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
    parameter_change_entries: Iterable[SessionChangeEntry] = (),
    command_entries: Iterable[SessionCommandLogEntry] = (),
    output_dir: str | Path | None = None,
    logger_path: str | Path | None = None,
    note: str = "",
) -> Path:
    frame_list = list(frames)
    raw_list = list(raw_records)
    change_entries = list(parameter_change_entries)
    command_log_entries = list(command_entries)
    logger_records = _read_logger_records(logger_path)
    if not frame_list and not raw_list and not logger_records:
        raise ValueError("当前会话没有可导出的帧或日志。")

    structured_filename, structured_frames, structured_scope, raw_rx_total, raw_tx_total = _resolve_structured_export(
        frame_list,
        raw_list if raw_list else logger_records,
        logger_path=logger_path,
        mode_preference=config.mode_preference,
    )
    raw_filename, raw_export_records, raw_frame_scope = _resolve_raw_frame_export(raw_list, logger_records)
    command_filename, command_log_scope, command_log_count = _resolve_command_log_export(
        raw_list,
        logger_records,
        command_log_entries,
    )
    package_dir = _prepare_package_dir("session", output_dir)
    if structured_frames:
        export_frames_to_csv(structured_frames, package_dir / structured_filename)

    _write_raw_log(raw_export_records, package_dir / raw_filename)
    _write_command_log(raw_export_records, package_dir / command_filename, command_entries=command_log_entries)
    _write_parameter_change_journal(package_dir, session_name, change_entries)
    _write_json(package_dir / "config_snapshot.json", asdict(config))
    _write_json(
        package_dir / "session_summary.json",
        _build_summary(
            session_name,
            frame_list,
            raw_export_records,
            logger_path,
            note,
            change_entries,
            command_entries=command_log_entries,
            structured_frames=structured_frames,
            structured_export_scope=structured_scope,
            structured_frame_count_exported=len(structured_frames),
            raw_rx_count_total=raw_rx_total,
            raw_tx_count_total=raw_tx_total,
            command_log_export_scope=command_log_scope,
            command_log_count_exported=command_log_count,
            raw_frame_export_scope=raw_frame_scope,
            raw_frame_count_exported=len(raw_export_records),
        ),
    )
    _write_summary_text(
        package_dir / "session_summary.txt",
        session_name,
        frame_list,
        raw_export_records,
        config,
        logger_path,
        note,
        change_entries,
        command_entries=command_log_entries,
        structured_frames=structured_frames,
        structured_export_scope=structured_scope,
        structured_frame_count_exported=len(structured_frames),
        raw_rx_count_total=raw_rx_total,
        raw_tx_count_total=raw_tx_total,
        command_log_export_scope=command_log_scope,
        command_log_count_exported=command_log_count,
        raw_frame_export_scope=raw_frame_scope,
        raw_frame_count_exported=len(raw_export_records),
    )
    _copy_session_logger(logger_path, package_dir)
    return package_dir


def export_diagnostic_package(
    *,
    session_name: str,
    frames: Iterable[ParsedFrame],
    raw_records: Iterable[RawFrameRecord],
    config: SessionConfig,
    parameter_change_entries: Iterable[SessionChangeEntry] = (),
    command_entries: Iterable[SessionCommandLogEntry] = (),
    output_dir: str | Path | None = None,
    logger_path: str | Path | None = None,
    note: str = "",
) -> Path:
    frame_list = list(frames)
    raw_list = list(raw_records)
    change_entries = list(parameter_change_entries)
    command_log_entries = list(command_entries)[-COMMAND_LOG_CACHE_LIMIT:]
    package_dir = _prepare_package_dir("diagnostic", output_dir)
    _write_json(package_dir / "app_info.json", get_version_info())
    _write_json(package_dir / "config_snapshot.json", asdict(config))
    _write_json(
        package_dir / "recent_session_summary.json",
        _build_summary(
            session_name,
            frame_list,
            raw_list,
            logger_path,
            note,
            change_entries,
            command_entries=command_log_entries,
            structured_frames=frame_list,
            command_log_export_scope="recent_cache",
            command_log_count_exported=len(command_log_entries) if command_log_entries else sum(1 for record in raw_list[-200:] if record.direction == "TX"),
            raw_frame_export_scope="recent_cache",
            raw_frame_count_exported=len(raw_list[-200:]),
        ),
    )
    _write_parameter_change_journal(package_dir, session_name, change_entries)
    _write_raw_log(raw_list[-200:], package_dir / "recent_raw_frames.log")
    _write_command_log(raw_list[-200:], package_dir / "recent_command_log.tsv", command_entries=command_log_entries)
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


def _resolve_raw_frame_export(
    raw_records: list[RawFrameRecord],
    logger_records: list[RawFrameRecord],
) -> tuple[str, list[RawFrameRecord], str]:
    if logger_records:
        return "raw_frames.log", list(logger_records), "full"
    return "recent_raw_frames.log", list(raw_records), "recent_cache"


def _resolve_command_log_export(
    raw_records: list[RawFrameRecord],
    logger_records: list[RawFrameRecord],
    command_entries: list[SessionCommandLogEntry],
) -> tuple[str, str, int]:
    if command_entries:
        return "command_log.tsv", "full", len(command_entries)
    if logger_records:
        return "command_log.tsv", "full", sum(1 for record in logger_records if record.direction == "TX")
    return "recent_command_log.tsv", "recent_cache", sum(1 for record in raw_records if record.direction == "TX")


def _write_raw_log(records: list[RawFrameRecord], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# timestamp\tdirection\tlevel\ttext\n")
        for record in records:
            clean = str(record.text).replace("\n", "\\n").replace("\r", "\\r")
            handle.write(
                f"{record.timestamp.isoformat(timespec='milliseconds')}\t{record.direction}\t{record.level}\t{clean}\n"
            )


def _write_command_log(
    records: list[RawFrameRecord],
    path: Path,
    *,
    command_entries: list[SessionCommandLogEntry] | None = None,
) -> None:
    if command_entries:
        rows = _structured_command_log_rows(command_entries)
        _write_delimited_rows(path, STRUCTURED_COMMAND_LOG_FIELDNAMES, rows, delimiter="\t", encoding="utf-8")
        _write_delimited_rows(path.with_suffix(".csv"), STRUCTURED_COMMAND_LOG_FIELDNAMES, rows, delimiter=",", encoding="utf-8-sig")
        return
    rows = _raw_command_log_rows(records)
    _write_delimited_rows(path, RAW_COMMAND_LOG_FIELDNAMES, rows, delimiter="\t", encoding="utf-8")
    _write_delimited_rows(path.with_suffix(".csv"), RAW_COMMAND_LOG_FIELDNAMES, rows, delimiter=",", encoding="utf-8-sig")


def _structured_command_log_rows(entries: Iterable[SessionCommandLogEntry]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in reversed(list(entries)):
        has_auto_upload_metadata = any(
            [
                str(entry.original_auto_upload_state or "").strip(),
                str(entry.restore_policy or "").strip(),
                bool(entry.restore_attempted),
                str(entry.restore_result or "").strip(),
            ]
        )
        detail_text = enrich_auto_upload_detail_text(
            str(entry.detail_text or ""),
            original_auto_upload_state=str(entry.original_auto_upload_state or ""),
            restore_policy=str(entry.restore_policy or ""),
            restore_attempted=entry.restore_attempted if has_auto_upload_metadata else None,
            restore_result=str(entry.restore_result or ""),
        ).replace("\n", "\\n").replace("\r", "\\r")
        rows.append(
            {
                "timestamp": entry.timestamp.isoformat(timespec="milliseconds"),
                "action_type": str(entry.action_type or ""),
                "action_label_zh": str(entry.action_label_zh or action_label_zh(entry.action_type)),
                "payload": str(entry.payload or ""),
                "parent_command": str(entry.parent_command or ""),
                "command_target_id": str(entry.command_target_id or ""),
                "expected_device_id": str(entry.expected_device_id or ""),
                "response_device_id": str(entry.response_device_id or ""),
                "effective_scope": str(entry.effective_scope or ""),
                "result": str(entry.result or ""),
                "source_page": str(entry.source_page or ""),
                "level": str(entry.level or ""),
                "original_auto_upload_state": str(entry.original_auto_upload_state or ""),
                "restore_policy": str(entry.restore_policy or ""),
                "restore_attempted": "true" if entry.restore_attempted else "false",
                "restore_result": str(entry.restore_result or ""),
                "detail_text": detail_text,
            }
        )
    return rows


def _raw_command_log_rows(records: Iterable[RawFrameRecord]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        if record.direction != "TX":
            continue
        broadcast = "yes" if "target=FFF" in record.text or "broadcast=yes" in record.text else "no"
        command = str(record.text).split(",", 1)[0].replace("[FFF] ", "")
        rows.append(
            {
                "timestamp": record.timestamp.isoformat(timespec="milliseconds"),
                "command": command,
                "broadcast": broadcast,
                "level": str(record.level or ""),
                "text": str(record.text or ""),
            }
        )
    return rows


def _write_delimited_rows(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    *,
    delimiter: str,
    encoding: str,
) -> None:
    with path.open("w", newline="", encoding=encoding) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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


def _parameter_change_journal_rows(
    session_name: str,
    entries: Iterable[SessionChangeEntry],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for entry in entries:
        has_auto_upload_metadata = any(
            [
                str(entry.original_auto_upload_state or "").strip(),
                str(entry.restore_policy or "").strip(),
                bool(entry.restore_attempted),
                str(entry.restore_result or "").strip(),
            ]
        )
        rows.append(
            {
                "timestamp": entry.timestamp.isoformat(timespec="milliseconds"),
                "session": session_name,
                "source_page": entry.source_page or "",
                "target_device": entry.target_device_id,
                "command_or_parameter": entry.command_name,
                "action_type": entry.action_type,
                "action_label_zh": str(entry.action_label_zh or action_label_zh(entry.action_type)),
                "command_payload": entry.command_payload,
                "parent_command": entry.parent_command,
                "command_target_id": entry.command_target_id,
                "expected_device_id": entry.expected_device_id,
                "response_device_id": entry.response_device_id,
                "effective_scope": entry.effective_scope,
                "is_system_action": "true" if entry.is_system_action else "false",
                "original_auto_upload_state": entry.original_auto_upload_state,
                "restore_policy": entry.restore_policy,
                "restore_attempted": "true" if entry.restore_attempted else "false",
                "restore_result": entry.restore_result,
                "before_value": entry.before_value,
                "target_value": entry.target_value,
                "after_value": entry.after_value,
                "result": entry.result_text,
                "verification_status": entry.verification_status or _verification_status_text(entry.result_text),
                "note": enrich_auto_upload_detail_text(
                    str(entry.detail_text or ""),
                    original_auto_upload_state=str(entry.original_auto_upload_state or ""),
                    restore_policy=str(entry.restore_policy or ""),
                    restore_attempted=entry.restore_attempted if has_auto_upload_metadata else None,
                    restore_result=str(entry.restore_result or ""),
                ),
            }
        )
    return rows


def _verification_status_text(result_text: str) -> str:
    normalized = str(result_text or "").strip()
    if normalized == "一致":
        return "verified_consistent"
    if normalized == "不一致":
        return "verified_mismatch"
    if normalized == "无法验证":
        return "verification_unavailable"
    if normalized == "写前读取失败":
        return "pre_read_failed"
    if normalized == "写入失败":
        return "write_failed"
    if normalized in {"未收到 ACK", "连接断开，未收到 ACK", "ACK 未确认"}:
        return "ack_timeout_unverified"
    if normalized == "写后复核中":
        return "post_read_pending"
    if normalized == "写入中":
        return "write_pending"
    return "recorded"


def _write_parameter_change_journal(
    package_dir: Path,
    session_name: str,
    entries: Iterable[SessionChangeEntry],
) -> None:
    rows = _parameter_change_journal_rows(session_name, entries)
    fieldnames = [
        "timestamp",
        "session",
        "source_page",
        "target_device",
        "command_or_parameter",
        "action_type",
        "action_label_zh",
        "command_payload",
        "parent_command",
        "command_target_id",
        "expected_device_id",
        "response_device_id",
        "effective_scope",
        "is_system_action",
        "original_auto_upload_state",
        "restore_policy",
        "restore_attempted",
        "restore_result",
        "before_value",
        "target_value",
        "after_value",
        "result",
        "verification_status",
        "note",
    ]
    _write_json(package_dir / "parameter_change_journal.json", rows)
    with (package_dir / "parameter_change_journal.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_text(path: Path, text: str) -> None:
    path.write_text(str(text or "") + "\n", encoding="utf-8")


def _read_logger_records(logger_path: str | Path | None) -> list[RawFrameRecord]:
    if not logger_path:
        return []
    path = Path(logger_path)
    if not path.exists():
        return []
    records: list[RawFrameRecord] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        parts = raw_line.split("\t", 3)
        if len(parts) != 4:
            continue
        timestamp_text, direction, level, text = parts
        try:
            timestamp = datetime.fromisoformat(timestamp_text)
        except Exception:
            continue
        records.append(
            RawFrameRecord(
                timestamp=timestamp,
                direction=direction,
                level=level,
                text=text.replace("\\n", "\n").replace("\\r", "\r"),
            )
        )
    return records


def _resolve_structured_export(
    frames: list[ParsedFrame],
    records: list[RawFrameRecord],
    *,
    logger_path: str | Path | None,
    mode_preference: str,
) -> tuple[str, list[ParsedFrame], str, int, int]:
    logger_records = _read_logger_records(logger_path)
    source_records = logger_records or records
    full_frames: list[ParsedFrame] = []
    parse_mode = mode_preference or PARSE_MODE_AUTO
    for record in source_records:
        if record.direction != "RX":
            continue
        if YGasProtocol.classify_line(record.text, parse_mode=parse_mode) != "telemetry":
            continue
        parsed = YGasProtocol.parse_line(record.text, parse_mode=parse_mode)
        if parsed is None:
            continue
        parsed.timestamp = record.timestamp
        full_frames.append(parsed)
    raw_rx_total = sum(1 for record in source_records if record.direction == "RX")
    raw_tx_total = sum(1 for record in source_records if record.direction == "TX")
    if full_frames and logger_records:
        return "structured_data.csv", full_frames, "full", raw_rx_total, raw_tx_total
    if frames:
        return "recent_structured_data.csv", list(frames), "recent_cache", raw_rx_total, raw_tx_total
    return "recent_structured_data.csv", [], "recent_cache", raw_rx_total, raw_tx_total


def _parameter_change_counts(entries: Iterable[SessionChangeEntry]) -> dict[str, int]:
    counts = {
        "parameter_change_count": 0,
        "verified_consistent_count": 0,
        "verified_mismatch_count": 0,
        "verification_unavailable_count": 0,
        "ack_only_unverified_count": 0,
        "unconfirmed_change_count": 0,
        "failed_change_count": 0,
    }
    for entry in entries:
        counts["parameter_change_count"] += 1
        verification_status = entry.verification_status or _verification_status_text(entry.result_text)
        if verification_status == "verified_consistent":
            counts["verified_consistent_count"] += 1
        elif verification_status == "verified_mismatch":
            counts["verified_mismatch_count"] += 1
            counts["failed_change_count"] += 1
        elif verification_status == "verification_unavailable":
            counts["verification_unavailable_count"] += 1
        elif verification_status == "ack_only_unverified":
            counts["ack_only_unverified_count"] += 1
        elif verification_status == "ack_timeout_unverified":
            counts["unconfirmed_change_count"] += 1
        elif verification_status in {"write_failed", "pre_read_failed"}:
            counts["failed_change_count"] += 1
    return counts


def _build_summary(
    session_name: str,
    frames: list[ParsedFrame],
    records: list[RawFrameRecord],
    logger_path: str | Path | None,
    note: str = "",
    change_entries: list[SessionChangeEntry] | None = None,
    command_entries: list[SessionCommandLogEntry] | None = None,
    *,
    structured_frames: list[ParsedFrame] | None = None,
    structured_export_scope: str = "recent_cache",
    structured_frame_count_exported: int = 0,
    raw_rx_count_total: int | None = None,
    raw_tx_count_total: int | None = None,
    command_log_export_scope: str = "recent_cache",
    command_log_count_exported: int = 0,
    raw_frame_export_scope: str = "recent_cache",
    raw_frame_count_exported: int = 0,
) -> dict[str, object]:
    recent_device_ids = sorted({frame.device_id for frame in frames if frame.device_id})
    exported_structured_frames = list(structured_frames if structured_frames is not None else frames)
    structured_device_ids = sorted({frame.device_id for frame in exported_structured_frames if frame.device_id})
    tx_records = [record for record in records if record.direction == "TX"]
    rx_records = [record for record in records if record.direction == "RX"]
    error_records = [record for record in records if record.level.upper() in {"ERROR", "WARN"}]
    change_counts = _parameter_change_counts(change_entries or [])
    broadcast_count = sum(
        1 for record in tx_records if "target=FFF" in record.text or "broadcast=yes" in record.text
    )
    recent_started_at = frames[0].timestamp.isoformat(timespec="seconds") if frames else None
    recent_ended_at = frames[-1].timestamp.isoformat(timespec="seconds") if frames else None
    structured_started_at = (
        exported_structured_frames[0].timestamp.isoformat(timespec="seconds") if exported_structured_frames else None
    )
    structured_ended_at = (
        exported_structured_frames[-1].timestamp.isoformat(timespec="seconds") if exported_structured_frames else None
    )
    return {
        "session_name": session_name,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "frame_count": len(frames),
        "frame_count_scope": "recent_cache",
        "recent_frame_count": len(frames),
        "recent_started_at": recent_started_at,
        "recent_ended_at": recent_ended_at,
        "recent_device_ids": recent_device_ids,
        "raw_record_count": len(records),
        "tx_count": len(tx_records),
        "rx_count": len(rx_records),
        "structured_export_scope": structured_export_scope,
        "structured_frame_count_exported": structured_frame_count_exported,
        "structured_started_at": structured_started_at,
        "structured_ended_at": structured_ended_at,
        "structured_device_ids": structured_device_ids,
        "raw_rx_count_total": len(rx_records) if raw_rx_count_total is None else raw_rx_count_total,
        "raw_tx_count_total": len(tx_records) if raw_tx_count_total is None else raw_tx_count_total,
        "cache_frame_limit": DEFAULT_HISTORY_SIZE,
        "raw_frame_export_scope": raw_frame_export_scope,
        "raw_frame_count_exported": raw_frame_count_exported,
        "command_log_export_scope": command_log_export_scope,
        "command_log_count_exported": command_log_count_exported,
        "command_log_cache_limit": COMMAND_LOG_CACHE_LIMIT,
        "error_count": len(error_records),
        "broadcast_tx_count": broadcast_count,
        "device_ids": recent_device_ids,
        "started_at": recent_started_at,
        "ended_at": recent_ended_at,
        "logger_path": str(logger_path) if logger_path else "",
        "command_entry_count": len(command_entries or []),
        **change_counts,
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
    change_entries: list[SessionChangeEntry] | None = None,
    command_entries: list[SessionCommandLogEntry] | None = None,
    *,
    structured_frames: list[ParsedFrame] | None = None,
    structured_export_scope: str = "recent_cache",
    structured_frame_count_exported: int = 0,
    raw_rx_count_total: int | None = None,
    raw_tx_count_total: int | None = None,
    command_log_export_scope: str = "recent_cache",
    command_log_count_exported: int = 0,
    raw_frame_export_scope: str = "recent_cache",
    raw_frame_count_exported: int = 0,
) -> None:
    summary = _build_summary(
        session_name,
        frames,
        records,
        logger_path,
        note,
        change_entries,
        command_entries=command_entries,
        structured_frames=structured_frames,
        structured_export_scope=structured_export_scope,
        structured_frame_count_exported=structured_frame_count_exported,
        raw_rx_count_total=raw_rx_count_total,
        raw_tx_count_total=raw_tx_count_total,
        command_log_export_scope=command_log_export_scope,
        command_log_count_exported=command_log_count_exported,
        raw_frame_export_scope=raw_frame_export_scope,
        raw_frame_count_exported=raw_frame_count_exported,
    )
    lines = [
        f"Session: {session_name}",
        f"Exported At: {summary['exported_at']}",
        f"Recent Frame Count: {summary['recent_frame_count']}",
        f"Recent Started At: {summary['recent_started_at'] or '--'}",
        f"Recent Ended At: {summary['recent_ended_at'] or '--'}",
        f"Recent Devices: {', '.join(summary['recent_device_ids']) if summary['recent_device_ids'] else '--'}",
        f"Structured Export Scope: {summary['structured_export_scope']}",
        f"Structured Frame Count Exported: {summary['structured_frame_count_exported']}",
        f"Structured Started At: {summary['structured_started_at'] or '--'}",
        f"Structured Ended At: {summary['structured_ended_at'] or '--'}",
        f"Structured Devices: {', '.join(summary['structured_device_ids']) if summary['structured_device_ids'] else '--'}",
        f"Raw Frame Export Scope: {summary['raw_frame_export_scope']}",
        f"Raw Frame Count Exported: {summary['raw_frame_count_exported']}",
        f"Raw Record Count: {summary['raw_record_count']}",
        f"Raw RX Count Total: {summary['raw_rx_count_total']}",
        f"Raw TX Count Total: {summary['raw_tx_count_total']}",
        f"Cache Frame Limit: {summary['cache_frame_limit']}",
        f"Command Log Export Scope: {summary['command_log_export_scope']}",
        f"Command Log Count Exported: {summary['command_log_count_exported']}",
        f"Command Log Cache Limit: {summary['command_log_cache_limit']}",
        f"Broadcast TX Count: {summary['broadcast_tx_count']}",
        f"Parameter Change Count: {summary['parameter_change_count']}",
        f"Verified Consistent Count: {summary['verified_consistent_count']}",
        f"ACK-only Unverified Count: {summary['ack_only_unverified_count']}",
        f"Unconfirmed Change Count: {summary['unconfirmed_change_count']}",
        f"Failed Change Count: {summary['failed_change_count']}",
        f"Recent Device Alias: {', '.join(summary['device_ids']) if summary['device_ids'] else '--'}",
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
