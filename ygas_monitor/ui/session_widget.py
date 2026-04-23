"""Single-session workstation UI."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import math
import time

from PySide6.QtCore import QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..commanding.permissions import has_permission, permission_label
from ..commanding.registry import CommandDefinition, CommandRegistry
from ..commanding.safety import (
    SAFE_QUERY_COMMAND_IDS,
    SESSION_MODE_ENGINEERING,
    SESSION_MODE_LISTEN_ONLY,
    SESSION_MODE_MONITORING,
    SESSION_MODE_REPLAY,
    SESSION_MODE_SAFE_HANDSHAKE,
    can_execute_command,
    is_read_only_command,
)
from ..config import EXPORT_DIR, LOG_DIR
from ..models import (
    AlarmEvent,
    CommandResult,
    DefaultMonitoringAckPendingWrite,
    DefaultMonitoringChecklistStep,
    ParsedFrame,
    RawFrameRecord,
    SessionCommandLogEntry,
    SessionChangeEntry,
    SessionConfig,
    SerialSettings,
    SessionWriteStatus,
    StructuredValueSnapshot,
    WriteVerificationReport,
    action_label_zh,
    auto_upload_state_label_zh,
    enrich_auto_upload_detail_text,
    restore_policy_label_zh,
    restore_result_label_zh,
)
from ..protocols.profiles import PROFILES, get_profile
from ..protocols.ygas import YGasProtocol
from ..serial.transport import list_serial_ports
from ..services.export_service import export_diagnostic_package, export_session_package
from ..services.metrics import MonitoringMetrics
from ..services.replay_service import ReplayDataset, load_replay_dataset
from ..services.auto_silence_policy import AutoSilencePolicy, AutoSilenceTargetResolutionError
from ..services.session_controller import (
    AUTO_START_STREAM_SOURCE_PAGE,
    MANUAL_STREAM_START_SOURCE_PAGE,
    AnalyzerSessionController,
)
from ..version import environment_summary_text
from .widgets.cards import MetricCardGrid
from .widgets.charts import RealtimeChartPanel
from .widgets.command_cards import CommandWorkspacePanel, CustomTemplateEditor
from .widgets.raw_frames import RawFramesWidget
from .widgets.status_panel import StatusPanel
from .theme_tokens import THEME_LABELS, THEME_OPTIONS

CHECKLIST_STATUS_PENDING = "待确认"
CHECKLIST_STATUS_CURRENT = "当前步骤"
CHECKLIST_STATUS_RUNNING = "执行中"
CHECKLIST_STATUS_DONE = "已完成"
CHECKLIST_STATUS_FAILED = "已失败"
CHECKLIST_STATUS_CANCELLED = "已取消"

VERIFICATION_STATUS_ACK_ONLY = "ack_only_unverified"
VERIFICATION_STATUS_ACK_UNCONFIRMED = "ack_timeout_unverified"
DEFAULT_MONITORING_CHECKLIST_LABEL = "校准联调准备清单"
DEFAULT_MONITORING_SOURCE_PAGE = "校准联调准备清单"
MODE2_BROADCAST_CONFIRM_PHRASE = "确认广播校准"
MODE2_BROADCAST_CONFIRM_ASCII_PHRASE = "MODE2 FFF"
FIRST_FRAME_TIMEOUT_MS = 5000


class SessionWidget(QWidget):
    theme_change_requested = Signal(str)

    CONTROL_COMMANDS = {
        "SETCOM",
        "SETCOM_QUERY",
        "SETCOMWAY",
        "FTD",
        "FTD_QUERY",
        "READDATA",
        "RESET",
        "ID",
        "ID_QUERY",
        "MODE",
        "MODE_QUERY",
    }
    SIGNAL_COMMANDS = {
        "SETPOW",
        "SETPOW_QUERY",
        "SETILLUM",
        "SETCO2",
        "SETCO2_QUERY",
        "TIMEOUT",
        "TIMEOUT_QUERY",
        "GETCO",
        "SENTEMP1",
        "SENTEMP1_QUERY",
        "SENTEMP2",
        "SENTEMP2_QUERY",
        "AVERAGE1",
        "AVERAGE1_QUERY",
        "AVERAGE2",
        "AVERAGE2_QUERY",
    }
    COEFFICIENT_PREFIXES = ("SENCO", "CLEARSENCO", "GETCO")

    def __init__(
        self,
        session_name: str,
        initial_state: dict | None = None,
        ui_theme: str = "dark",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.session_name = session_name
        self.registry = CommandRegistry()
        self.controller = AnalyzerSessionController(session_name)
        self.initial_state = initial_state or {}
        self._ui_theme_choice = ui_theme

        self.connected = False
        self.monitor_frozen = False
        self.last_frame: ParsedFrame | None = None
        self.last_metrics: MonitoringMetrics | None = None
        self.latest_frame_time: datetime | None = None
        self.last_connect_time: datetime | None = None
        self.last_disconnect_reason = "--"
        self.last_serial_error = "--"
        self.last_command_timeout = "--"
        self.last_status_anomaly = "--"
        self.last_command_failure = "--"
        self.last_broadcast_command = "--"
        self.last_non_broadcast_target = "001"
        self.last_export_dir = str(EXPORT_DIR)
        self.last_replay_file = ""

        self._disconnect_expected = False
        self._last_port_refresh_ts = 0.0
        self._reconnect_backoff_s = 2
        self._safe_history: list[dict[str, str]] = []

        self._rx_frame_times: deque[float] = deque()
        self._abnormal_frame_times: deque[float] = deque()
        self._cmd_success_times: deque[float] = deque()
        self._cmd_fail_times: deque[float] = deque()
        self._diagnostic_snapshot_dirty = False
        self._expert_terminal_pending: list[str] = []
        self._default_config_scheduled = False
        self._device_output_mode = "AUTO"
        self._device_mode_confirmed = False
        self._device_auto_upload = True
        self._auto_upload_state = "unknown"
        self._auto_start_stream_listen_preference = True
        self._planned_auto_start_stream_payload = ""
        self._planned_auto_start_stream_reason = ""
        self._pending_stream_start_payload = ""
        self._auto_start_stream_schedule_token = 0
        self._waiting_for_first_stream_frame = False
        self._first_frame_wait_timed_out = False
        self._stream_runtime_state = "idle"
        self.raw_rx_seen_since_connect = False
        self.valid_frame_seen_since_connect = False
        self.last_raw_rx_at: datetime | None = None
        self.last_valid_frame_at: datetime | None = None
        self.parse_failure_count_since_connect = 0
        self.latest_online_device_id = ""
        self.active_online_device_ids: list[str] = []
        self._pending_readback_requests: dict[str, dict[str, object]] = {}
        self._pending_write_verifications: dict[str, dict[str, object]] = {}
        self._monitor_aux_expanded = False
        self._monitor_aux_last_height = 140
        self._restored_page_index = 0
        self._device_quick_status = (
            "连接成功后默认自动启动实时流（仅发送 SETCOMWAY=1）；"
            "收到首帧后图表与 KPI 会自动更新，其他写入仍保持受限。"
        )
        self._hard_status_state = SessionWriteStatus()
        self._session_change_history: deque[SessionChangeEntry] = deque()
        self._session_change_entries: deque[SessionChangeEntry] = deque(maxlen=20)
        self._command_log_entries: deque[SessionCommandLogEntry] = deque(maxlen=200)
        self._command_log_history: deque[SessionCommandLogEntry] = deque()
        self._quiet_read_original_state = ""
        self._quiet_read_after_state = ""
        self._restore_retry_candidates: dict[str, dict[str, str]] = {}
        self._pending_restore_retry_requests: dict[str, dict[str, str]] = {}
        # Historical naming is retained for compatibility; these fields now back multiple checklist kinds.
        self._default_monitoring_steps: list[DefaultMonitoringChecklistStep] = []
        self._default_monitoring_prepare_notice = ""
        self._default_monitoring_run_id = 0
        self._prepared_checklist_label = DEFAULT_MONITORING_CHECKLIST_LABEL
        self._prepared_checklist_source_page = DEFAULT_MONITORING_SOURCE_PAGE
        self._prepared_checklist_kind = "default_monitoring"
        self._pending_default_monitoring_ack_only_writes: dict[str, DefaultMonitoringAckPendingWrite] = {}
        self._default_config_scheduled_target_id = ""
        self._last_manual_snapshot_status: dict[str, object] | None = None
        self._last_address_mode_change: dict[str, object] | None = None
        self._last_coeff_review_status: dict[str, object] | None = None
        self._coefficient_workspace_state: dict[str, dict[str, str]] = {}

        self.replay_dataset: ReplayDataset | None = None
        self.replay_index = 0
        self.replay_running = False
        self.replay_alarm_bits: set[int] = set()

        self.replay_timer = QTimer(self)
        self.replay_timer.setSingleShot(True)
        self.replay_timer.timeout.connect(self._replay_tick)

        self.age_timer = QTimer(self)
        self.age_timer.setInterval(500)
        self.age_timer.timeout.connect(self._update_frame_age_status)

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._auto_reconnect_tick)

        self.first_frame_timer = QTimer(self)
        self.first_frame_timer.setSingleShot(True)
        self.first_frame_timer.setInterval(FIRST_FRAME_TIMEOUT_MS)
        self.first_frame_timer.timeout.connect(self._handle_first_frame_timeout)

        self.diagnostic_snapshot_timer = QTimer(self)
        self.diagnostic_snapshot_timer.setInterval(300)
        self.diagnostic_snapshot_timer.setSingleShot(True)
        self.diagnostic_snapshot_timer.timeout.connect(self._flush_diagnostic_snapshot)

        self.expert_terminal_timer = QTimer(self)
        self.expert_terminal_timer.setInterval(120)
        self.expert_terminal_timer.setSingleShot(True)
        self.expert_terminal_timer.timeout.connect(self._flush_terminal_buffer)

        self._build_ui()
        self._connect_signals()
        self._refresh_ports(force=True)
        self._apply_persisted_state(self.initial_state)
        self._apply_permission_mode()
        self._restore_current_page_from_state()
        self._update_target_badge()
        self._update_status_strip()
        self._schedule_diagnostic_snapshot_refresh()
        self._update_anomaly_summary()
        self._update_safe_history_combo()
        self._refresh_expected_hz_hint()
        self._set_auto_upload_state(self._auto_upload_state)
        self.set_theme_choice(self._ui_theme_choice)
        self.apply_theme(self._ui_theme_choice)
        self.age_timer.start()

    def shutdown(self) -> None:
        self.reconnect_timer.stop()
        self.replay_timer.stop()
        self.age_timer.stop()
        self.diagnostic_snapshot_timer.stop()
        self.expert_terminal_timer.stop()
        self.controller.shutdown()

    def collect_persisted_state(self) -> dict:
        return {
            "port": self.port_combo.currentText().strip(),
            "baudrate": self.baud_combo.currentText(),
            "bytesize": self.bytesize_combo.currentText(),
            "parity": self.parity_combo.currentText(),
            "stopbits": self.stopbits_combo.currentText(),
            "acquisition_mode": self._current_acquisition_mode(),
            "mode_preference": self._current_parse_mode(),
            "session_mode": self._current_session_mode(),
            "command_timeout_ms": str(self.command_timeout_edit.value()),
            "auto_reconnect": self.auto_reconnect_check.isChecked(),
            "auto_start_stream_after_connect": self._auto_start_stream_listen_preference,
            "auto_apply_default_monitoring": self.auto_apply_default_config_check.isChecked(),
            "read_only_lock": self.read_only_lock_check.isChecked(),
            "profile_name": self._current_profile_name(),
            "device_ftd_hz": str(self.device_ftd_hz_edit.value()),
            "stream_hz": str(self.stream_hz_edit.value()),
            "poll_interval_ms": str(self.poll_interval_edit.value()),
            "permission_level": self.permission_combo.currentText(),
            "show_expert_terminal": self.show_expert_check.isChecked(),
            "target_id": self._current_target_id(),
            "session_note": self.session_note_edit.text().strip(),
            "current_page_index": self.pages.currentIndex(),
            "last_export_dir": self.last_export_dir,
            "last_replay_file": self.last_replay_file,
            "monitor_aux_expanded": self._monitor_aux_expanded,
            "monitor_aux_height": self._monitor_aux_last_height,
        }

    def _current_acquisition_mode(self) -> str:
        return str(self.capture_combo.currentData() or "LISTEN")

    def _set_acquisition_mode(self, mode: str) -> None:
        index = self.capture_combo.findData(str(mode or "LISTEN"))
        if index < 0:
            index = 0
        self.capture_combo.setCurrentIndex(index)

    def _current_auto_start_stream_enabled(self) -> bool:
        return bool(getattr(self, "auto_start_stream_check", None) and self.auto_start_stream_check.isChecked())

    def _sync_auto_start_stream_option(self, *, force_restore: bool = False) -> None:
        if not hasattr(self, "auto_start_stream_check"):
            return
        is_listen_mode = self._current_acquisition_mode() == "LISTEN"
        self.auto_start_stream_check.blockSignals(True)
        if is_listen_mode:
            self.auto_start_stream_check.setEnabled(True)
            if force_restore or not self.auto_start_stream_check.isChecked():
                self.auto_start_stream_check.setChecked(bool(self._auto_start_stream_listen_preference))
        else:
            if self.auto_start_stream_check.isEnabled():
                self._auto_start_stream_listen_preference = self.auto_start_stream_check.isChecked()
            self.auto_start_stream_check.setChecked(False)
            self.auto_start_stream_check.setEnabled(False)
        self.auto_start_stream_check.blockSignals(False)
        self._refresh_auto_start_stream_hint()

    def _handle_capture_mode_changed(self) -> None:
        self._sync_auto_start_stream_option()
        if self._current_acquisition_mode() != "LISTEN":
            self.first_frame_timer.stop()
            self._waiting_for_first_stream_frame = False
            self._first_frame_wait_timed_out = False
            self._stream_runtime_state = "idle"
        if self.connected and self.last_frame is None:
            self._show_waiting_for_stream_hint()
        self._refresh_monitor_quick_controls()

    def _handle_auto_start_stream_toggled(self, checked: bool) -> None:
        if self._current_acquisition_mode() == "LISTEN":
            self._auto_start_stream_listen_preference = bool(checked)
        self._refresh_auto_start_stream_hint()
        if self.connected and self.last_frame is None:
            self._show_waiting_for_stream_hint()

    def _refresh_auto_start_stream_hint(self) -> None:
        if not hasattr(self, "auto_start_stream_hint_label"):
            return
        if self._current_acquisition_mode() != "LISTEN":
            text = "当前为手动读取模式，系统不会自动启动实时流。"
        elif self._current_auto_start_stream_enabled():
            text = "连接成功后自动向目标设备发送 SETCOMWAY=1，启动主动上传；不会修改 MODE、FTD、SENCO 或其他参数。"
        else:
            text = "当前已关闭自动启动；连接后将只监听已有数据流，可在监测页手动点击“启动实时流”。"
        self.auto_start_stream_hint_label.setText(text)

    def _current_parse_mode(self) -> str:
        return str(self.mode_combo.currentData() or "AUTO")

    def _set_parse_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(str(mode or "AUTO"))
        if index < 0:
            index = 0
        self.mode_combo.setCurrentIndex(index)

    def _handle_parse_mode_changed(self) -> None:
        current_mode = self._current_parse_mode()
        if not self.valid_frame_seen_since_connect:
            self._device_output_mode = current_mode
            self._device_mode_confirmed = current_mode in {"MODE1", "MODE2"}
        if self.connected:
            self.controller.update_config(self._build_config())
        self._refresh_monitor_quick_controls()
        if self.connected and not self.valid_frame_seen_since_connect:
            self._show_waiting_for_stream_hint()

    def _reset_stream_diagnostics(self) -> None:
        self.raw_rx_seen_since_connect = False
        self.valid_frame_seen_since_connect = False
        self.last_raw_rx_at = None
        self.last_valid_frame_at = None
        self.parse_failure_count_since_connect = 0

    def _raw_rx_parse_failure_message(self) -> str:
        if self._current_parse_mode() != "AUTO":
            suggestion = "建议检查 MODE1/MODE2 输出或切换解析模式为自动识别。"
        else:
            suggestion = "请检查设备输出格式、波特率或线缆是否正常。"
        return f"收到串口数据，但未解析为有效气体帧。{suggestion}"

    def _first_frame_timeout_message(self) -> str:
        message = (
            "主动上传已开启，但尚未收到有效气体帧。"
            " 可能原因：目标设备 ID、波特率、线缆、设备未实际输出、解析模式不匹配。"
        )
        if self.raw_rx_seen_since_connect and self.parse_failure_count_since_connect > 0:
            return f"{message} {self._raw_rx_parse_failure_message()}"
        return message

    def _chart_stream_status_text(self) -> str:
        if self.replay_running:
            return "回放"
        if not self.connected:
            return "未连接"
        mapping = {
            "idle": "未启动",
            "starting": "启动中",
            "running": "已开启",
            "failed": "失败",
        }
        return mapping.get(self._stream_runtime_state, "未启动")

    def _refresh_chart_status_badge(self) -> None:
        if hasattr(self, "chart_panel"):
            self.chart_panel.set_runtime_status(
                stream_status=self._chart_stream_status_text(),
                raw_rx_seen=self.raw_rx_seen_since_connect,
                valid_frame_seen=self.valid_frame_seen_since_connect,
                parse_mode=self._current_parse_mode(),
            )

    def _monitor_page_active(self) -> bool:
        return self.pages.currentIndex() == self.monitor_tab_index

    def set_theme_choice(self, theme_choice: str) -> None:
        self._ui_theme_choice = str(theme_choice or "dark")
        if not hasattr(self, "theme_combo"):
            return
        index = self.theme_combo.findData(self._ui_theme_choice)
        if index < 0:
            index = 0
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(index)
        self.theme_combo.blockSignals(False)

    def apply_theme(self, theme_choice: str) -> None:
        self._ui_theme_choice = str(theme_choice or "dark")
        for card_grid_name in ("data_cards", "detail_cards", "monitor_cards"):
            card_grid = getattr(self, card_grid_name, None)
            if card_grid is not None:
                card_grid.apply_theme(self._ui_theme_choice)
        if hasattr(self, "chart_panel"):
            self.chart_panel.apply_theme(self._ui_theme_choice)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.simulator_banner = QLabel("当前处于 SIMULATOR 模式，请勿误认为真机。")
        self.simulator_banner.setProperty("warning", True)
        self.simulator_banner.hide()
        layout.addWidget(self.simulator_banner)

        self.broadcast_banner = QLabel("当前正在使用广播地址 FFF，请确认现场隔离和权限等级。")
        self.broadcast_banner.setProperty("warning", True)
        self.broadcast_banner.hide()
        layout.addWidget(self.broadcast_banner)

        self.session_header_box = self._build_hard_status_bar()
        self.task_entry_box = self._build_task_entry_box()
        self.change_summary_box = self._build_session_change_summary_box()
        layout.addWidget(self.session_header_box)

        self.pages = QTabWidget()
        self.monitor_tab_index = self.pages.addTab(self._build_monitor_page(), "监测总览")
        self.settings_tab_index = self.pages.addTab(self._build_settings_page(), "连接与设备模式")
        self.control_tab_index = self.pages.addTab(self._build_control_page(), "设备控制")
        self.coeff_tab_index = self.pages.addTab(self._build_coeff_page(), "系数中心")
        self.signal_tab_index = self.pages.addTab(self._build_signal_page(), "信号与算法参数")
        self.expert_page = self._build_expert_page()
        self.expert_tab_index = self.pages.addTab(self.expert_page, "串口助手")
        self.export_page = self._build_export_page()
        self.export_tab_index = self.pages.addTab(self.export_page, "数据导出与回放")
        layout.addWidget(self.pages, 1)

    def _build_status_strip(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.connection_state_label = QLabel("未连接")
        self.session_mode_status_label = QLabel("会话模式: 实时监测")
        self.backend_status_label = QLabel("后端: --")
        self.lock_status_label = QLabel("严格只读: 关")
        self.latest_frame_label = QLabel("最近有效数据: --")
        self.frame_age_label = QLabel("最新数据时延: --")
        self.backend_status_label.hide()
        self.latest_frame_label.hide()
        for widget in [
            self.connection_state_label,
            self.session_mode_status_label,
            self.lock_status_label,
            self.frame_age_label,
        ]:
            layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _build_hard_status_bar(self) -> QWidget:
        box = QGroupBox("会话头部")
        layout = QGridLayout(box)
        box.setTitle("")
        box.setFlat(True)
        box.setMaximumHeight(64)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)

        layout.addWidget(self._build_status_strip(), 0, 0, 1, 8)
        self.hard_online_value_label = QLabel("--")
        self.hard_target_value_label = QLabel("--")
        self.hard_send_value_label = QLabel("--")
        self.hard_write_permission_label = QLabel("禁止")
        self.hard_stream_status_label = QLabel("--")
        self.hard_reason_value_label = QLabel("--")
        self.hard_reason_value_label.setWordWrap(True)
        self.hard_status_help_label = QLabel(
            "实时监测模式只允许自动启动实时流；其他写入仍需工程模式、权限和复核。"
        )
        self.hard_status_help_label.setWordWrap(True)
        self.hard_status_help_label.setProperty("muted", True)
        self.hard_status_note_label = QLabel("顶部仅汇总控制写入与实时流启动语义，具体命令仍会单独校验。")
        self.hard_status_note_label.setProperty("muted", True)
        self.session_workflow_button = QPushButton("工作流")
        self.session_change_badge_button = QPushButton("参数变更 0 条")
        self.session_header_detail_button = QPushButton("详情")

        layout.addWidget(self.session_workflow_button, 0, 8)
        layout.addWidget(self.session_change_badge_button, 0, 9)
        layout.addWidget(self.session_header_detail_button, 0, 10)
        layout.addWidget(QLabel("在线"), 1, 0)
        layout.addWidget(self.hard_online_value_label, 1, 1)
        layout.addWidget(QLabel("目标"), 1, 2)
        layout.addWidget(self.hard_target_value_label, 1, 3)
        layout.addWidget(QLabel("发往"), 1, 4)
        layout.addWidget(self.hard_send_value_label, 1, 5)
        layout.addWidget(QLabel("控制写入"), 1, 6)
        layout.addWidget(self.hard_write_permission_label, 1, 7)
        layout.addWidget(QLabel("实时流"), 1, 8)
        layout.addWidget(self.hard_stream_status_label, 1, 9)
        self.hard_reason_value_label.hide()
        self.hard_status_help_label.hide()
        self.hard_status_note_label.hide()
        return box

    def _build_task_entry_box(self) -> QWidget:
        box = QGroupBox("任务入口（按需展开）")
        box.setCheckable(True)
        box.setChecked(False)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        content = QWidget()
        content_layout = QGridLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setHorizontalSpacing(12)
        content_layout.setVerticalSpacing(8)

        self.session_safety_hint_label = QLabel()
        self.session_safety_hint_label.setWordWrap(True)
        self.session_safety_hint_label.setProperty("warning", True)
        self.session_safety_hint_detail_label = QLabel(
            "进入写入阶段需切到工程模式、关闭只读锁，并满足 target/online 一致性。"
        )
        self.session_safety_hint_detail_label.setWordWrap(True)
        self.session_safety_hint_detail_label.setProperty("muted", True)
        self.task_entry_feedback_label = QLabel("可从这里直接打开常用工作流入口，不会自动发送危险写命令。")
        self.task_entry_feedback_label.setWordWrap(True)
        self.task_entry_feedback_label.setProperty("muted", True)

        tasks = [
            (
                "新设备接入检查",
                "先确认连接、安全握手和当前在线设备，再进入后续设置。",
                "开始检查",
                "_open_task_new_device_check",
                "task_new_device_button",
            ),
            (
                "读取当前参数快照",
                "前往参数页并聚焦读取入口，先锁定当前设备状态。",
                "前往读取",
                "_open_task_read_snapshot",
                "task_read_snapshot_button",
            ),
            (
                "地址与模式设置",
                "前往设备控制页处理地址、模式和串口相关参数。",
                "打开控制页",
                "_open_task_address_mode",
                "task_address_mode_button",
            ),
            (
                "系数读写与复核",
                "前往系数中心查看当前系数、执行写入并核对结果。",
                "打开系数页",
                "_open_task_coeff_review",
                "task_coeff_review_button",
            ),
            (
                "导出诊断包",
                "前往导出与回放页，集中查看变更总览并导出诊断资料。",
                "前往导出",
                "_open_task_export_diag",
                "task_export_diag_button",
            ),
        ]

        content_layout.addWidget(self.session_safety_hint_label, 0, 0, 1, 4)
        content_layout.addWidget(self.session_safety_hint_detail_label, 1, 0, 1, 4)
        self._task_status_labels: dict[str, QLabel] = {}
        for row, (title, description, button_text, handler_name, attr_name) in enumerate(tasks, start=2):
            task_key = attr_name.removeprefix("task_").removesuffix("_button")
            title_label = QLabel(title)
            title_label.setProperty("accent", True)
            description_label = QLabel(description)
            description_label.setWordWrap(True)
            description_label.setProperty("muted", True)
            status_title_label = QLabel("当前状态")
            status_title_label.setProperty("muted", True)
            status_label = QLabel("--")
            status_label.setWordWrap(True)
            button = QPushButton(button_text)
            setattr(self, attr_name, button)
            setattr(self, f"{attr_name}_label", title_label)
            setattr(self, f"{attr_name}_description", description_label)
            setattr(self, f"task_{task_key}_status_title_label", status_title_label)
            setattr(self, f"task_{task_key}_status_label", status_label)
            self._task_status_labels[task_key] = status_label
            button.clicked.connect(getattr(self, handler_name))
            content_layout.addWidget(title_label, row, 0)
            content_layout.addWidget(description_label, row, 1)
            status_cell = QWidget()
            status_layout = QVBoxLayout(status_cell)
            status_layout.setContentsMargins(0, 0, 0, 0)
            status_layout.setSpacing(2)
            status_layout.addWidget(status_title_label)
            status_layout.addWidget(status_label)
            content_layout.addWidget(status_cell, row, 2)
            content_layout.addWidget(button, row, 3)
        content_layout.addWidget(self.task_entry_feedback_label, len(tasks) + 2, 0, 1, 4)
        layout.addWidget(content)
        box.toggled.connect(content.setVisible)
        content.setVisible(False)
        self._refresh_session_safety_hint()
        self._refresh_task_entry_states()
        return box

    def _build_session_change_summary_box(self) -> QWidget:
        box = QGroupBox("参数变更总览摘要（按需展开）")
        box.setCheckable(True)
        box.setChecked(False)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        content = QWidget()
        content_layout = QGridLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setHorizontalSpacing(14)
        content_layout.setVerticalSpacing(6)

        self.change_summary_message_label = QLabel("本次会话暂无参数变更")
        self.change_summary_message_label.setWordWrap(True)
        self.change_summary_message_label.setProperty("muted", True)
        self.change_summary_view_button = QPushButton("查看总览")
        self.change_summary_view_button.clicked.connect(self._open_session_change_overview)

        stats = [
            ("total", "总数"),
            ("consistent", "一致"),
            ("ack_only", "ACK-only 未复核"),
            ("mismatch", "不一致"),
            ("unverifiable", "无法验证"),
            ("failure", "失败类"),
        ]
        self._change_summary_value_labels: dict[str, QLabel] = {}
        content_layout.addWidget(self.change_summary_message_label, 0, 0, 1, 5)
        content_layout.addWidget(self.change_summary_view_button, 0, 5, 1, 1)
        for column, (key, title) in enumerate(stats):
            caption = QLabel(title)
            caption.setProperty("muted", True)
            value_label = QLabel("0")
            setattr(self, f"change_summary_{key}_value_label", value_label)
            self._change_summary_value_labels[key] = value_label
            content_layout.addWidget(caption, 1, column)
            content_layout.addWidget(value_label, 2, column)

        layout.addWidget(content)
        box.toggled.connect(content.setVisible)
        content.setVisible(False)
        self._refresh_session_change_summary()
        return box

    def _build_quick_connect_box(self) -> QWidget:
        box = QGroupBox("快速连接")
        layout = QGridLayout(box)
        box.setTitle("快速连接")

        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.refresh_ports_button = QPushButton("刷新串口")
        self.demo_button = QPushButton("演示模式")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText("115200")
        self.capture_combo = QComboBox()
        self.capture_combo.addItem("自动上传", "LISTEN")
        self.capture_combo.addItem("手动读取", "POLL")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("自动识别", "AUTO")
        self.mode_combo.addItem("MODE1", "MODE1")
        self.mode_combo.addItem("MODE2", "MODE2")
        self._set_acquisition_mode("LISTEN")
        self._set_parse_mode("AUTO")
        self.connect_button = QPushButton("连接")
        self.connect_button.setProperty("accent", True)
        self.disconnect_button = QPushButton("断开")
        self.reconnect_button = QPushButton("重新连接")
        self.safe_handshake_button = QPushButton("安全握手")

        layout.addWidget(QLabel("串口"), 0, 0)
        layout.addWidget(self.port_combo, 0, 1)
        layout.addWidget(self.refresh_ports_button, 0, 2)
        layout.addWidget(self.demo_button, 0, 3)
        layout.addWidget(QLabel("波特率"), 1, 0)
        layout.addWidget(self.baud_combo, 1, 1)
        layout.addWidget(QLabel("采集方式"), 1, 2)
        layout.addWidget(self.capture_combo, 1, 3)
        layout.addWidget(QLabel("解析模式"), 2, 0)
        layout.addWidget(self.mode_combo, 2, 1)
        layout.addWidget(self.connect_button, 2, 2)
        layout.addWidget(self.disconnect_button, 2, 3)
        layout.addWidget(self.reconnect_button, 3, 2)
        layout.addWidget(self.safe_handshake_button, 3, 3)
        box.setTitle("常用连接参数")
        return box

    def _build_connection_box(self) -> QWidget:
        box = QGroupBox("串口与采集")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        summary_grid = QGridLayout()
        summary_grid.setContentsMargins(0, 0, 0, 0)
        summary_grid.setSpacing(8)

        self.bytesize_combo = QComboBox()
        self.bytesize_combo.addItems(["7", "8"])
        self.bytesize_combo.setCurrentText("8")
        self.parity_combo = QComboBox()
        self.parity_combo.addItems(["N", "E", "O"])
        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(["1", "2"])
        self.profile_combo = QComboBox()
        bench_profile = PROFILES["bench_default"]
        self.profile_combo.addItem(bench_profile.display_name, bench_profile.name)
        self.device_ftd_hz_edit = self._build_numeric_spin_box(10, minimum=1, maximum=20, step=1, suffix=" Hz")
        self.stream_hz_edit = self._build_numeric_spin_box(10, minimum=1, maximum=50, step=1, suffix=" Hz")
        self.poll_interval_edit = self._build_numeric_spin_box(200, minimum=50, maximum=5000, step=50, suffix=" ms")
        self.command_timeout_edit = self._build_numeric_spin_box(2000, minimum=300, maximum=10000, step=100, suffix=" ms")
        self.expected_hz_hint_label = QLabel("期望接收频率仅用于监测统计，不会直接写入设备。")
        self.expected_hz_hint_label.setWordWrap(True)
        self.expected_hz_hint_label.setProperty("muted", True)
        self.device_ftd_hz_edit.valueChanged.connect(lambda _value: self._refresh_expected_hz_hint())
        self.stream_hz_edit.valueChanged.connect(lambda _value: self._refresh_expected_hz_hint())
        self.auto_reconnect_check = QCheckBox("自动重连")
        self.auto_start_stream_check = QCheckBox("连接后自动启动实时流")
        self.auto_start_stream_check.setChecked(True)
        self.auto_apply_default_config_check = QCheckBox("连接后自动准备校准联调清单（不发送）")
        self.auto_apply_default_config_check.setChecked(False)
        self.init_button = QPushButton("准备初始化采集清单（待确认）")
        self.apply_default_config_button = QPushButton("准备校准联调清单（待确认）")
        self.log_path_label = QLabel("日志文件: --")
        self.log_path_label.setProperty("muted", True)
        self.auto_start_stream_hint_label = QLabel(
            "连接成功后自动向目标设备发送 SETCOMWAY=1，启动主动上传；不会修改 MODE、FTD、SENCO 或其他参数。"
        )
        self.auto_start_stream_hint_label.setWordWrap(True)
        self.auto_start_stream_hint_label.setProperty("muted", True)
        self.auto_apply_default_config_check.setText("连接后准备校准清单")
        self.auto_apply_default_config_check.setToolTip("仅预填，不发送，需人工确认。")
        self.init_button.setText("准备初始化采集清单")
        self.apply_default_config_button.setText("准备校准清单")
        self.auto_apply_default_config_hint_label = QLabel(
            "默认关闭。连接成功不等于自动写入；启用后仅会自动准备校准联调清单到工作区，不发送，仍需人工确认。"
        )
        self.auto_apply_default_config_hint_label.setWordWrap(True)
        self.auto_apply_default_config_hint_label.setProperty("muted", True)
        self.auto_apply_default_config_hint_label.setText("仅预填，不发送，需人工确认。")

        summary_grid.addWidget(self.auto_reconnect_check, 0, 0, 1, 2)
        summary_grid.addWidget(self.init_button, 0, 2, 1, 2)
        summary_grid.addWidget(self.auto_start_stream_check, 1, 0, 1, 2)
        summary_grid.addWidget(self.auto_start_stream_hint_label, 1, 2, 1, 2)
        summary_grid.addWidget(self.apply_default_config_button, 2, 0, 1, 2)
        summary_grid.addWidget(self.log_path_label, 2, 2, 1, 2)
        summary_grid.addWidget(self.auto_apply_default_config_check, 3, 0, 1, 2)
        summary_grid.addWidget(self.auto_apply_default_config_hint_label, 3, 2, 1, 2)

        self.advanced_connection_box = QGroupBox("高级串口与采集设置")
        self.advanced_connection_box.setCheckable(True)
        self.advanced_connection_box.setChecked(False)
        self.advanced_connection_box.setTitle("高级设置")
        advanced_box_layout = QVBoxLayout(self.advanced_connection_box)
        advanced_box_layout.setContentsMargins(8, 8, 8, 8)
        self.advanced_connection_content = QWidget()
        self.advanced_connection_content.setVisible(False)
        self.advanced_connection_box.toggled.connect(self.advanced_connection_content.setVisible)
        advanced_grid = QGridLayout(self.advanced_connection_content)
        advanced_grid.addWidget(QLabel("数据位"), 0, 0)
        advanced_grid.addWidget(self.bytesize_combo, 0, 1)
        advanced_grid.addWidget(QLabel("校验位"), 0, 2)
        advanced_grid.addWidget(self.parity_combo, 0, 3)
        advanced_grid.addWidget(QLabel("停止位"), 1, 0)
        advanced_grid.addWidget(self.stopbits_combo, 1, 1)
        advanced_grid.addWidget(QLabel("滤波规则"), 1, 2)
        advanced_grid.addWidget(self.profile_combo, 1, 3)
        advanced_grid.addWidget(QLabel("设备 FTD 写入频率(Hz)"), 2, 0)
        advanced_grid.addWidget(self.device_ftd_hz_edit, 2, 1)
        advanced_grid.addWidget(QLabel("期望接收频率(Hz)"), 2, 2)
        advanced_grid.addWidget(self.stream_hz_edit, 2, 3)
        advanced_grid.addWidget(QLabel("轮询间隔(ms)"), 3, 0)
        advanced_grid.addWidget(self.poll_interval_edit, 3, 1)
        advanced_grid.addWidget(QLabel("命令超时(ms)"), 3, 2)
        advanced_grid.addWidget(self.command_timeout_edit, 3, 3)
        advanced_grid.addWidget(self.expected_hz_hint_label, 4, 0, 1, 4)
        advanced_box_layout.addWidget(self.advanced_connection_content)

        layout.addLayout(summary_grid)
        layout.addWidget(self.advanced_connection_box)
        box.setTitle("连接行为与高级设置")
        return box

    def _build_workspace_box(self) -> QWidget:
        box = QGroupBox("联调安全上下文")
        layout = QFormLayout(box)
        box.setTitle("设备模式与权限")
        self.permission_combo = QComboBox()
        self.permission_combo.addItems(["READ_ONLY", "CONFIG", "CALIBRATION", "EXPERT"])
        self.permission_combo.setCurrentText("READ_ONLY")
        self.session_mode_combo = QComboBox()
        self.session_mode_combo.addItem("严格只听模式（不发送任何命令）", SESSION_MODE_LISTEN_ONLY)
        self.session_mode_combo.addItem("实时监测模式（默认，仅允许自动启动实时流）", SESSION_MODE_MONITORING)
        self.session_mode_combo.addItem("工程联调模式（允许正式控制命令）", SESSION_MODE_ENGINEERING)
        self.session_mode_combo.setCurrentIndex(1)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["001"])
        self.broadcast_check = QCheckBox("启用广播地址 FFF")
        self.read_only_lock_check = QCheckBox("严格只读 / 禁止任何发送")
        self.read_only_lock_check.setChecked(False)
        self.show_expert_check = QCheckBox("启用专家终端（仅 EXPERT）")
        self.show_expert_check.setChecked(False)
        self.current_target_label = QLabel("当前目标设备 ID: 001")
        self.current_target_label.setProperty("broadcast", False)
        self.profile_hint_label = QLabel(get_profile("bench_default").description)
        self.profile_hint_label.setWordWrap(True)
        self.profile_hint_label.setProperty("muted", True)
        self.discovered_label = QLabel("已发现设备: --")
        self.discovered_label.setWordWrap(True)
        self.discovered_label.setProperty("muted", True)
        self.expert_terminal_hint_label = QLabel("专家终端默认隐藏。仅在权限等级为 EXPERT 且显式启用后显示。")
        self.expert_terminal_hint_label.setWordWrap(True)
        self.expert_terminal_hint_label.setProperty("muted", True)

        layout.addRow("权限等级", self.permission_combo)
        layout.addRow("联调模式", self.session_mode_combo)
        layout.addRow("目标设备 ID", self.target_combo)
        layout.addRow("", self.broadcast_check)
        layout.addRow("", self.read_only_lock_check)
        layout.addRow("", self.show_expert_check)
        layout.addRow("", self.expert_terminal_hint_label)
        layout.addRow("当前上下文", self.current_target_label)
        layout.addRow("滤波映射", self.profile_hint_label)
        layout.addRow("设备发现", self.discovered_label)
        return box

    def _build_diagnostic_box(self) -> QWidget:
        box = QGroupBox("诊断与服务")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.diag_port_label = QLabel("--")
        self.diag_serial_label = QLabel("--")
        self.diag_connect_time_label = QLabel("--")
        self.diag_last_frame_label = QLabel("--")
        self.diag_last_serial_error_label = QLabel("--")
        self.diag_last_timeout_label = QLabel("--")
        self.diag_last_disconnect_label = QLabel("--")
        self.diag_rx_1m_label = QLabel("0")
        self.diag_abnormal_1m_label = QLabel("0")
        self.diag_cmd_1m_label = QLabel("0 / 0")
        self.diag_receive_hz_label = QLabel("0.0 Hz")
        self.session_note_edit = QLineEdit()
        self.session_note_edit.setPlaceholderText("联调备注，将写入导出包")
        self.copy_env_button = QPushButton("复制环境信息")
        self.open_log_dir_button = QPushButton("打开日志目录")
        self.command_history_combo = QComboBox()
        self.command_history_combo.setEditable(False)
        self.resend_history_button = QPushButton("快速重发")

        status_box = QGroupBox("连接状态")
        status_form = QFormLayout(status_box)
        status_form.addRow("当前串口", self.diag_port_label)
        status_form.addRow("串口参数", self.diag_serial_label)
        status_form.addRow("最近连接时间", self.diag_connect_time_label)
        status_form.addRow("最近有效数据时间", self.diag_last_frame_label)

        stats_box = QGroupBox("运行统计")
        stats_form = QFormLayout(stats_box)
        stats_form.addRow("最近 1 分钟接收帧数", self.diag_rx_1m_label)
        stats_form.addRow("最近 1 分钟异常帧数", self.diag_abnormal_1m_label)
        stats_form.addRow("最近 1 分钟命令成/败", self.diag_cmd_1m_label)
        stats_form.addRow("当前接收频率", self.diag_receive_hz_label)

        basic_box = QGroupBox("基础诊断信息")
        basic_form = QFormLayout(basic_box)
        basic_form.addRow("最近串口异常", self.diag_last_serial_error_label)
        basic_form.addRow("最近命令超时", self.diag_last_timeout_label)
        basic_form.addRow("最近断开原因", self.diag_last_disconnect_label)

        note_box = QGroupBox("会话备注")
        note_layout = QVBoxLayout(note_box)
        note_layout.addWidget(self.session_note_edit)

        tools_box = QGroupBox("服务工具")
        tools_layout = QGridLayout(tools_box)
        tools_layout.addWidget(self.copy_env_button, 0, 0)
        tools_layout.addWidget(self.open_log_dir_button, 0, 1)
        tools_layout.addWidget(self.resend_history_button, 1, 0)
        tools_layout.addWidget(QLabel("命令历史"), 2, 0)
        tools_layout.addWidget(self.command_history_combo, 2, 1)

        layout.addWidget(status_box)
        layout.addWidget(stats_box)
        layout.addWidget(basic_box)
        layout.addWidget(note_box)
        layout.addWidget(tools_box)
        layout.addStretch(1)
        return box

    def _build_ui_preferences_box(self) -> QWidget:
        box = QGroupBox("界面外观（次要）")
        layout = QFormLayout(box)
        self.theme_combo = QComboBox()
        for theme_key in THEME_OPTIONS:
            self.theme_combo.addItem(THEME_LABELS[theme_key], theme_key)
        theme_hint = QLabel("界面主题只影响显示外观，不改变联调权限、会话模式或设备控制策略。")
        theme_hint.setProperty("muted", True)
        theme_hint.setWordWrap(True)
        layout.addRow("全局主题", self.theme_combo)
        layout.addRow("", theme_hint)
        return box

    def _build_numeric_spin_box(
        self,
        value: int,
        *,
        minimum: int,
        maximum: int,
        step: int,
        suffix: str,
    ) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setAccelerated(True)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setToolTip(f"范围 {minimum}-{maximum}{suffix}")
        return spin

    def _build_settings_section_box(self, title: str, summary: str, widgets: list[QWidget]) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        summary_label.setProperty("muted", True)
        layout.addWidget(summary_label)
        for widget in widgets:
            layout.addWidget(widget)
        return box

    def _make_scroll_page(self, content: QWidget, *, object_name: str = "") -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("settingsPageScrollArea")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        if object_name:
            scroll.setObjectName(object_name)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        self.device_access_section_box = self._build_settings_section_box(
            "1. 设备接入",
            "先确认串口链路、采集方式和连接动作，再决定是否准备校准联调清单。",
            [self._build_quick_connect_box(), self._build_connection_box()],
        )
        self.commissioning_safety_section_box = self._build_settings_section_box(
            "2. 联调安全",
            "这里集中决定权限等级、会话模式、只读锁、目标设备和专家能力是否开放。",
            [self._build_workspace_box()],
        )
        self.diagnostic_environment_section_box = self._build_settings_section_box(
            "3. 诊断与环境",
            "用于查看连接诊断、最近异常、导出目录和环境工具；不会改变设备控制主链。",
            [self._build_diagnostic_box(), self._build_ui_preferences_box()],
        )

        content_layout.addWidget(self.task_entry_box)
        content_layout.addWidget(self.device_access_section_box)
        content_layout.addWidget(self.commissioning_safety_section_box)
        content_layout.addWidget(self.diagnostic_environment_section_box)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return page

    def _build_monitor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        controls_wrap = QVBoxLayout()
        controls_wrap.setContentsMargins(0, 0, 0, 0)
        controls_wrap.setSpacing(6)
        controls = QHBoxLayout()
        self.freeze_button = QPushButton("冻结显示")
        self.freeze_button.setCheckable(True)
        self.monitor_connection_badge = QLabel("未连接")
        controls.addWidget(self.freeze_button)
        self.monitor_connection_badge.setProperty("state", "disconnected")
        self.monitor_mode_label = QLabel("AUTO")
        self.monitor_mode_label.setProperty("accent", True)
        self.monitor_upload_badge = QLabel("上传：未确认")
        self.monitor_upload_badge.setProperty("accent", True)
        self.monitor_auto_upload_state_label = QLabel("自动上传状态：未确认")
        self.monitor_auto_upload_state_label.setProperty("muted", True)
        self.monitor_auto_upload_state_label.hide()
        self.monitor_quick_status_label = QLabel(self._device_quick_status)
        self.monitor_device_action_button = QPushButton("设备动作")
        self.monitor_device_action_menu = QMenu(self.monitor_device_action_button)
        self.monitor_action_mode1 = self.monitor_device_action_menu.addAction("预填 MODE1")
        self.monitor_action_mode2 = self.monitor_device_action_menu.addAction("预填 MODE2")
        self.monitor_device_action_menu.addSeparator()
        self.monitor_action_upload_on = self.monitor_device_action_menu.addAction("预填 SETCOMWAY=1 到设备控制页")
        self.monitor_action_upload_off = self.monitor_device_action_menu.addAction("预填关闭主动上传到设备控制页")
        self.monitor_device_action_menu.addSeparator()
        self.monitor_action_read_snapshot = self.monitor_device_action_menu.addAction("读取关键配置快照")
        self.monitor_device_action_button.setMenu(self.monitor_device_action_menu)
        self.monitor_aux_toolbar_toggle_button = QPushButton("展开辅助区")
        self.monitor_aux_toolbar_toggle_button.setCheckable(True)
        self.monitor_aux_toolbar_toggle_button.setToolTip("展开后可查看状态面板、扩展数据、原始帧和异常摘要。")
        self.monitor_quick_status_label.setProperty("muted", True)
        self.monitor_quick_status_label.setWordWrap(True)
        self.monitor_quick_status_label.setToolTip("显示模式切换、自动上传和图槽切换的最近状态。")
        controls.addWidget(self.monitor_connection_badge)
        controls.addWidget(self.monitor_mode_label)
        controls.addWidget(self.monitor_upload_badge)
        controls.addWidget(self.monitor_device_action_button)
        controls.addWidget(self.monitor_aux_toolbar_toggle_button)
        controls.addStretch(1)
        controls_wrap.addLayout(controls)

        self.monitor_action_row_widget = QWidget()
        monitor_action_row = QHBoxLayout(self.monitor_action_row_widget)
        monitor_action_row.setContentsMargins(0, 0, 0, 0)
        monitor_action_row.setSpacing(8)
        self.monitor_start_stream_button = QPushButton("启动实时流")
        self.monitor_start_stream_button.setProperty("accent", True)
        self.monitor_diagnostic_button = QPushButton("诊断")
        self.monitor_diagnostic_menu = QMenu(self.monitor_diagnostic_button)
        self.monitor_diagnostic_auto_parse_action = self.monitor_diagnostic_menu.addAction("切换解析模式为自动识别")
        self.monitor_diagnostic_read_snapshot_action = self.monitor_diagnostic_menu.addAction("读取关键配置")
        self.monitor_diagnostic_open_log_action = self.monitor_diagnostic_menu.addAction("打开串口日志")
        self.monitor_diagnostic_button.setMenu(self.monitor_diagnostic_menu)
        monitor_action_row.addWidget(self.monitor_start_stream_button)
        monitor_action_row.addWidget(self.monitor_diagnostic_button)
        monitor_action_row.addStretch(1)
        monitor_action_row.addWidget(self.monitor_quick_status_label, 1)
        controls_wrap.addWidget(self.monitor_action_row_widget)
        layout.addLayout(controls_wrap)

        self.data_cards = MetricCardGrid(rows=1, columns=6, compact=True)
        self.data_cards.setMinimumHeight(86)
        self.data_cards.setMaximumHeight(112)
        self.detail_cards = MetricCardGrid(rows=2, columns=4, compact=True)
        self.monitor_cards = MetricCardGrid(rows=2, columns=4, compact=True)
        self.data_cards.setToolTip("右击监测卡片可将指标切换到上图或下图。")
        self.detail_cards.setToolTip("扩展数据在辅助区中按需查看。")
        self.monitor_cards.setToolTip("异常摘要与运行统计在辅助区中按需查看。")
        self.data_cards.slot_requested.connect(self._handle_metric_card_slot_request)
        self.detail_cards.slot_requested.connect(self._handle_metric_card_slot_request)

        anomaly_box = QGroupBox("异常摘要")
        anomaly_layout = QFormLayout(anomaly_box)
        self.last_status_label = QLabel("--")
        self.last_command_fail_label = QLabel("--")
        self.last_serial_error_label = QLabel("--")
        self.last_broadcast_label = QLabel("--")
        anomaly_layout.addRow("最近状态异常", self.last_status_label)
        anomaly_layout.addRow("最近命令失败", self.last_command_fail_label)
        anomaly_layout.addRow("最近串口异常", self.last_serial_error_label)
        anomaly_layout.addRow("最近广播命令", self.last_broadcast_label)

        self.chart_panel = RealtimeChartPanel()
        self.chart_panel.setMinimumHeight(320)
        self.chart_config_group = self.chart_panel.curve_settings_panel
        self.chart_upper_summary_label = self.chart_panel.upper_summary_label
        self.chart_lower_summary_label = self.chart_panel.lower_summary_label
        self.chart_restore_defaults_button = self.chart_panel.restore_defaults_button
        self.chart_upper_view_combo = self.chart_panel.upper_view_combo
        self.chart_lower_view_combo = self.chart_panel.lower_view_combo
        self.chart_clear_upper_button = self.chart_panel.clear_upper_button
        self.chart_clear_lower_button = self.chart_panel.clear_lower_button
        self.status_panel = StatusPanel()
        self.raw_frames = RawFramesWidget()

        detail_tab = QWidget()
        detail_layout = QVBoxLayout(detail_tab)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)
        detail_layout.addWidget(self.detail_cards, 1)

        diagnostic_tab = QWidget()
        diagnostic_layout = QVBoxLayout(diagnostic_tab)
        diagnostic_layout.setContentsMargins(0, 0, 0, 0)
        diagnostic_layout.setSpacing(8)
        diagnostic_layout.addWidget(anomaly_box)
        diagnostic_layout.addWidget(self.monitor_cards)
        diagnostic_layout.addStretch(1)

        self.monitor_aux_tabs = QTabWidget()
        self.monitor_aux_tabs.addTab(self.status_panel, "状态面板")
        self.monitor_aux_tabs.addTab(detail_tab, "扩展数据")
        self.monitor_aux_tabs.addTab(self.raw_frames, "原始帧")
        self.monitor_aux_tabs.addTab(diagnostic_tab, "异常摘要 / 诊断")
        self.monitor_aux_tabs.setMinimumHeight(120)

        self.monitor_aux_container = QWidget()
        monitor_aux_layout = QVBoxLayout(self.monitor_aux_container)
        monitor_aux_layout.setContentsMargins(0, 0, 0, 0)
        monitor_aux_layout.setSpacing(6)
        monitor_aux_header = QHBoxLayout()
        monitor_aux_title = QLabel("辅助区")
        monitor_aux_title.setProperty("muted", True)
        self.monitor_aux_panel_toggle_button = QPushButton("展开辅助区")
        self.monitor_aux_panel_toggle_button.setCheckable(True)
        self.monitor_aux_panel_toggle_button.setToolTip("展开后可查看状态面板、扩展数据、原始帧和异常摘要。")
        monitor_aux_header.addWidget(monitor_aux_title)
        monitor_aux_header.addStretch(1)
        monitor_aux_header.addWidget(self.monitor_aux_panel_toggle_button)
        monitor_aux_layout.addLayout(monitor_aux_header)
        monitor_aux_layout.addWidget(self.monitor_aux_tabs, 1)

        self.monitor_body_split = QSplitter(Qt.Vertical)
        self.monitor_body_split.setChildrenCollapsible(False)
        self.monitor_body_split.addWidget(self.chart_panel)
        self.monitor_body_split.addWidget(self.monitor_aux_container)
        self.monitor_body_split.setSizes([820, 0])

        layout.addWidget(self.data_cards)
        layout.addWidget(self.monitor_body_split, 1)
        self._set_monitor_aux_expanded(False)
        self._refresh_chart_config_summary()
        return page

    def _build_chart_config_box(self) -> QWidget:
        """Deprecated: chart configuration now lives inside RealtimeChartPanel."""
        return self.chart_panel.curve_settings_panel

    def _build_control_page(self) -> QWidget:
        self.control_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_command_ids(self.CONTROL_COMMANDS),
            profile_name=self._current_profile_name(),
            source_panel="control_panel",
        )
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.common_control_actions_box = self._build_common_control_actions_box()
        layout.addWidget(self.common_control_actions_box)
        layout.addWidget(self.control_panel, 1)
        return self._make_scroll_page(content, object_name="controlPageScrollArea")

    def _build_coeff_page(self) -> QWidget:
        self.coeff_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_prefixes(self.COEFFICIENT_PREFIXES),
            profile_name=self._current_profile_name(),
            source_panel="coeff_panel",
        )
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.coeff_workspace_box = self._build_coefficient_workspace_box()
        layout.addWidget(self.coeff_workspace_box)

        self.coeff_detail_box = QGroupBox("底层命令卡（按需展开）")
        self.coeff_detail_box.setCheckable(True)
        self.coeff_detail_box.setChecked(False)
        coeff_detail_layout = QVBoxLayout(self.coeff_detail_box)
        coeff_detail_layout.setContentsMargins(8, 8, 8, 8)
        self.coeff_detail_container = QWidget()
        coeff_detail_container_layout = QVBoxLayout(self.coeff_detail_container)
        coeff_detail_container_layout.setContentsMargins(0, 0, 0, 0)
        coeff_detail_container_layout.addWidget(self.coeff_panel)
        self.coeff_detail_container.setVisible(False)
        self.coeff_detail_box.toggled.connect(self.coeff_detail_container.setVisible)
        coeff_detail_layout.addWidget(self.coeff_detail_container)
        layout.addWidget(self.coeff_detail_box, 1)
        self.coeff_panel.command_tree.itemSelectionChanged.connect(self._sync_coefficient_workspace_selection_from_panel)
        self._refresh_coefficient_workspace_table()
        self._set_selected_coefficient_command("SENCO1", expand_detail=False)
        return self._make_scroll_page(content, object_name="coeffPageScrollArea")

    def _build_common_control_actions_box(self) -> QWidget:
        box = QGroupBox("现场常用动作")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        summary_label = QLabel(
            "优先覆盖现场高频联调任务；写入类快捷动作仍会继续遵守权限等级、会话模式、只读锁和目标设备一致性限制。"
        )
        summary_label.setWordWrap(True)
        summary_label.setProperty("muted", True)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.control_read_id_button = QPushButton("读取设备地址")
        self.control_change_id_button = QPushButton("修改设备地址")
        self.control_read_mode_button = QPushButton("读取当前模式")
        self.control_mode1_button = QPushButton("预填 MODE1")
        self.control_mode2_button = QPushButton("预填 MODE2")
        self.control_auto_upload_on_button = QPushButton("预填开启主动上传")
        self.control_auto_upload_off_button = QPushButton("预填关闭主动上传")
        self.control_snapshot_button = QPushButton("读取当前关键配置快照")

        buttons = [
            self.control_read_id_button,
            self.control_change_id_button,
            self.control_read_mode_button,
            self.control_mode1_button,
            self.control_mode2_button,
            self.control_auto_upload_on_button,
            self.control_auto_upload_off_button,
            self.control_snapshot_button,
        ]
        for index, button in enumerate(buttons):
            row, column = divmod(index, 4)
            grid.addWidget(button, row, column)

        self.control_common_feedback_label = QLabel("建议先读取关键配置快照；快捷按钮只会预填命令卡，不会直接发送。")
        self.control_common_feedback_label.setWordWrap(True)
        self.control_common_feedback_label.setProperty("muted", True)

        checklist_box = QGroupBox("待确认写入清单")
        checklist_layout = QVBoxLayout(checklist_box)
        checklist_layout.setContentsMargins(8, 8, 8, 8)
        checklist_layout.setSpacing(6)
        self.default_monitoring_checklist_summary_label = QLabel(
            f"当前清单：{DEFAULT_MONITORING_CHECKLIST_LABEL}。尚未准备步骤；准备后会在这里逐步显示待确认动作，且不会自动发送。"
        )
        self.default_monitoring_checklist_summary_label.setWordWrap(True)
        self.default_monitoring_checklist_summary_label.setProperty("muted", True)
        checklist_header_widget = QWidget()
        checklist_header_layout = QGridLayout(checklist_header_widget)
        checklist_header_layout.setContentsMargins(0, 0, 0, 0)
        checklist_header_layout.setHorizontalSpacing(12)
        checklist_header_layout.setVerticalSpacing(4)
        self.checklist_name_value_label = QLabel(DEFAULT_MONITORING_CHECKLIST_LABEL)
        self.checklist_target_value_label = QLabel("--")
        self.checklist_scope_value_label = QLabel("--")
        self.checklist_progress_value_label = QLabel("0 / 0")
        self.checklist_overall_status_label = QLabel("未准备")
        for label in [
            self.checklist_name_value_label,
            self.checklist_target_value_label,
            self.checklist_scope_value_label,
            self.checklist_progress_value_label,
            self.checklist_overall_status_label,
        ]:
            label.setWordWrap(True)
        checklist_header_layout.addWidget(QLabel("当前清单"), 0, 0)
        checklist_header_layout.addWidget(self.checklist_name_value_label, 0, 1)
        checklist_header_layout.addWidget(QLabel("目标设备"), 0, 2)
        checklist_header_layout.addWidget(self.checklist_target_value_label, 0, 3)
        checklist_header_layout.addWidget(QLabel("执行范围"), 1, 0)
        checklist_header_layout.addWidget(self.checklist_scope_value_label, 1, 1)
        checklist_header_layout.addWidget(QLabel("当前进度"), 1, 2)
        checklist_header_layout.addWidget(self.checklist_progress_value_label, 1, 3)
        checklist_header_layout.addWidget(QLabel("整体状态"), 2, 0)
        checklist_header_layout.addWidget(self.checklist_overall_status_label, 2, 1, 1, 3)
        self.default_monitoring_checklist_table = QTableWidget(0, 5)
        self.default_monitoring_checklist_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.default_monitoring_checklist_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.default_monitoring_checklist_table.setAlternatingRowColors(True)
        self.default_monitoring_checklist_table.setWordWrap(True)
        self.default_monitoring_checklist_table.setMaximumHeight(260)
        self.default_monitoring_checklist_table.verticalHeader().setVisible(False)
        self.default_monitoring_checklist_table.horizontalHeader().setStretchLastSection(True)
        self.default_monitoring_checklist_table.setHorizontalHeaderLabels(
            ["步骤", "动作", "目标", "状态", "最近证据"]
        )
        self.default_monitoring_checklist_note_label = QLabel(
            "本清单仅会预填到统一工作区，请逐项确认发送。"
            " 只有在显式选择 MODE1 或 MODE2 时，初始化采集清单才会预填对应模式切换；AUTO 不会默认强行切换。"
        )
        self.default_monitoring_checklist_note_label.setWordWrap(True)
        self.default_monitoring_checklist_note_label.setProperty("muted", True)
        self.default_monitoring_checklist_action_label = QLabel(
            f"当前无待确认清单。请先准备{DEFAULT_MONITORING_CHECKLIST_LABEL}；后续仍需逐项确认，且不会自动发送。"
        )
        self.default_monitoring_checklist_action_label.setWordWrap(True)
        self.default_monitoring_checklist_action_label.setProperty("muted", True)
        self.default_monitoring_empty_state_row = QWidget()
        empty_state_layout = QHBoxLayout(self.default_monitoring_empty_state_row)
        empty_state_layout.setContentsMargins(0, 0, 0, 0)
        empty_state_layout.setSpacing(8)
        self.default_monitoring_empty_state_label = QLabel("当前无待确认清单")
        self.default_monitoring_empty_state_label.setProperty("muted", True)
        self.default_monitoring_prepare_default_button = QPushButton("准备校准清单")
        self.default_monitoring_prepare_capture_button = QPushButton("准备初始化采集清单")
        empty_state_layout.addWidget(self.default_monitoring_empty_state_label)
        empty_state_layout.addStretch(1)
        empty_state_layout.addWidget(self.default_monitoring_prepare_default_button)
        empty_state_layout.addWidget(self.default_monitoring_prepare_capture_button)
        action_row = QHBoxLayout()
        self.default_monitoring_continue_button = QPushButton("继续当前步骤（预填）")
        self.default_monitoring_restart_button = QPushButton("重新准备清单")
        self.default_monitoring_cancel_button = QPushButton("取消本次清单")
        self.default_monitoring_continue_button.setEnabled(False)
        self.default_monitoring_restart_button.setEnabled(False)
        self.default_monitoring_cancel_button.setEnabled(False)
        action_row.addWidget(self.default_monitoring_continue_button)
        action_row.addWidget(self.default_monitoring_restart_button)
        action_row.addWidget(self.default_monitoring_cancel_button)
        action_row.addStretch(1)
        checklist_layout.addWidget(self.default_monitoring_checklist_summary_label)
        checklist_layout.addWidget(checklist_header_widget)
        checklist_layout.addWidget(self.default_monitoring_checklist_table)
        checklist_layout.addWidget(self.default_monitoring_checklist_note_label)
        checklist_layout.addWidget(self.default_monitoring_checklist_action_label)
        checklist_layout.addWidget(self.default_monitoring_empty_state_row)
        checklist_layout.addLayout(action_row)

        layout.addWidget(summary_label)
        layout.addLayout(grid)
        layout.addWidget(checklist_box)
        layout.addWidget(self.control_common_feedback_label)

        self.control_read_id_button.clicked.connect(self._read_device_address_from_common_actions)
        self.control_change_id_button.clicked.connect(self._open_device_address_change_task)
        self.control_read_mode_button.clicked.connect(self._read_mode_from_common_actions)
        self.control_mode1_button.clicked.connect(lambda: self._request_common_output_mode("MODE1"))
        self.control_mode2_button.clicked.connect(lambda: self._request_common_output_mode("MODE2"))
        self.control_auto_upload_on_button.clicked.connect(lambda: self._request_common_auto_upload(True))
        self.control_auto_upload_off_button.clicked.connect(lambda: self._request_common_auto_upload(False))
        self.control_snapshot_button.clicked.connect(self._read_control_snapshot_from_common_actions)
        self.default_monitoring_continue_button.clicked.connect(self._continue_default_monitoring_checklist_step)
        self.default_monitoring_restart_button.clicked.connect(self._restart_default_monitoring_checklist)
        self.default_monitoring_cancel_button.clicked.connect(self._cancel_default_monitoring_checklist)
        self.default_monitoring_prepare_default_button.clicked.connect(self._apply_default_monitoring_config)
        self.default_monitoring_prepare_capture_button.clicked.connect(self._prepare_capture_initialization_checklist)
        return box

    def _build_coefficient_workspace_box(self) -> QWidget:
        box = QGroupBox("系数管理工作台")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        summary_label = QLabel(
            "主视图优先围绕 SENCO 系数读取、目标值编辑、写入与复核闭环展开；底层命令卡仍保留在下方按需展开。"
        )
        summary_label.setWordWrap(True)
        summary_label.setProperty("muted", True)

        toolbar = QHBoxLayout()
        self.coeff_read_current_button = QPushButton("读取当前值")
        self.coeff_edit_target_button = QPushButton("定位目标值编辑")
        self.coeff_write_verify_button = QPushButton("定位写入与复核")
        self.coeff_expand_command_button = QPushButton("展开底层命令卡")
        for button in [
            self.coeff_read_current_button,
            self.coeff_edit_target_button,
            self.coeff_write_verify_button,
            self.coeff_expand_command_button,
        ]:
            toolbar.addWidget(button)
        toolbar.addStretch(1)

        self.coeff_workspace_table = QTableWidget(0, 6)
        self.coeff_workspace_table.setHorizontalHeaderLabels(
            ["系数项", "当前值", "目标值", "写后值 / 复核值", "状态", "说明"]
        )
        self.coeff_workspace_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.coeff_workspace_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.coeff_workspace_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.coeff_workspace_table.setMaximumHeight(360)
        self.coeff_workspace_table.verticalHeader().setVisible(False)
        self.coeff_workspace_table.horizontalHeader().setStretchLastSection(True)

        self.coeff_workspace_feedback_label = QLabel("建议先读取当前值，再编辑目标值并执行写后复核。")
        self.coeff_workspace_feedback_label.setWordWrap(True)
        self.coeff_workspace_feedback_label.setProperty("muted", True)

        layout.addWidget(summary_label)
        layout.addLayout(toolbar)
        layout.addWidget(self.coeff_workspace_table)
        layout.addWidget(self.coeff_workspace_feedback_label)

        self.coeff_workspace_table.itemSelectionChanged.connect(self._handle_coefficient_workspace_selection_changed)
        self.coeff_read_current_button.clicked.connect(self._read_selected_coefficient_current)
        self.coeff_edit_target_button.clicked.connect(self._focus_selected_coefficient_editor)
        self.coeff_write_verify_button.clicked.connect(self._focus_selected_coefficient_write_review)
        self.coeff_expand_command_button.clicked.connect(lambda: self.coeff_detail_box.setChecked(True))
        return box

    def _build_signal_page(self) -> QWidget:
        self.signal_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_command_ids(self.SIGNAL_COMMANDS),
            profile_name=self._current_profile_name(),
            source_panel="signal_panel",
        )
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.signal_panel)
        return self._make_scroll_page(content, object_name="signalPageScrollArea")

    def _build_expert_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        preset_box = QGroupBox("常用模板")
        preset_layout = QHBoxLayout(preset_box)
        self.serial_template_combo = QComboBox()
        self._populate_serial_assistant_templates()
        self.serial_template_apply_button = QPushButton("载入模板")
        self.serial_template_apply_button.clicked.connect(self._apply_serial_assistant_template)
        preset_layout.addWidget(self.serial_template_combo, 1)
        preset_layout.addWidget(self.serial_template_apply_button)

        terminal_box = QGroupBox("串口助手")
        terminal_layout = QVBoxLayout(terminal_box)
        row = QHBoxLayout()
        self.raw_command_edit = QLineEdit()
        self.raw_command_edit.setPlaceholderText("输入完整原始命令，例如 READDATA,YGAS,001")
        self.raw_expectation_combo = QComboBox()
        self.raw_expectation_combo.addItems(sorted(CommandRegistry.RETURN_TYPES))
        self.raw_expectation_combo.setCurrentText("ack")
        self.raw_send_button = QPushButton("发送原始命令")
        self.raw_send_button.setProperty("danger", True)
        row.addWidget(self.raw_command_edit, 1)
        row.addWidget(self.raw_expectation_combo)
        row.addWidget(self.raw_send_button)
        self.raw_terminal_log = QPlainTextEdit()
        self.raw_terminal_log.setReadOnly(True)
        self.raw_terminal_log.document().setMaximumBlockCount(3000)
        self.raw_terminal_log.setMinimumHeight(220)
        terminal_layout.addLayout(row)
        terminal_layout.addWidget(self.raw_terminal_log)

        custom_box = QGroupBox("自定义命令模板系统")
        custom_layout = QVBoxLayout(custom_box)
        self.custom_editor = CustomTemplateEditor(self.registry)
        custom_layout.addWidget(self.custom_editor)
        self.custom_panel_container = QWidget()
        self.custom_panel_layout = QVBoxLayout(self.custom_panel_container)
        custom_layout.addWidget(self.custom_panel_container)
        self.custom_panel: CommandWorkspacePanel | None = None
        self._rebuild_custom_panel()

        layout.addWidget(preset_box)
        layout.addWidget(terminal_box, 2)
        layout.addWidget(custom_box, 3)
        return self._make_scroll_page(content, object_name="expertPageScrollArea")

    def _populate_serial_assistant_templates(self) -> None:
        self.serial_template_combo.clear()
        preset_items: list[tuple[str, str, dict[str, str], str]] = [
            ("ID 查询", "ID_QUERY", {}, "identity"),
            ("ID 写入", "ID", {"new_id": "002"}, "ack"),
            ("MODE 查询", "MODE_QUERY", {}, "mode_value"),
            ("MODE 写入", "MODE", {"mode": "2"}, "ack"),
            ("SETCOMWAY 0", "SETCOMWAY", {"mode": "0"}, "ack"),
            ("SETCOMWAY 1", "SETCOMWAY", {"mode": "1"}, "ack"),
            ("FTD 查询", "FTD_QUERY", {}, "setting_value"),
            ("FTD 写入", "FTD", {"hz": "10"}, "ack"),
            ("水滤波 AVERAGE1", "AVERAGE1", {"window": "49"}, "ack"),
            ("气滤波 AVERAGE2", "AVERAGE2", {"window": "49"}, "ack"),
        ]
        for index in range(1, 10):
            preset_items.append((f"GETCO {index}", "GETCO", {"index": str(index)}, "coefficient"))
        for index in range(1, 10):
            preset_items.append((f"SENCO{index}", f"SENCO{index}", {"coefficients": "1,0,0,0,0,0"}, "ack"))

        for label, command_id, values, expectation in preset_items:
            self.serial_template_combo.addItem(
                label,
                {"command_id": command_id, "values": values, "expectation": expectation},
            )

    def _apply_serial_assistant_template(self) -> None:
        payload_data = self.serial_template_combo.currentData()
        if not payload_data:
            return
        command_id = str(payload_data["command_id"])
        values = dict(payload_data["values"])
        target_id = self.registry.default_target_for_command(command_id, self._current_target_id())
        self.raw_command_edit.setText(
            self.registry.build_preview(command_id, target_id, values, profile_name=self._current_profile_name())
        )
        self.raw_expectation_combo.setCurrentText(str(payload_data["expectation"]))

    def _build_export_page(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        export_box = QGroupBox("正式导出")
        export_layout = QHBoxLayout(export_box)
        self.export_now_button = QPushButton("导出最近数据 CSV")
        self.export_session_button = QPushButton("导出本次会话包")
        self.export_diag_page_button = QPushButton("导出诊断包")
        self.open_export_button = QPushButton("打开导出目录")
        export_layout.addWidget(self.export_session_button)
        export_layout.addWidget(self.export_now_button)
        export_layout.addWidget(self.export_diag_page_button)
        export_layout.addWidget(self.open_export_button)
        export_layout.addStretch(1)
        self.export_session_button.setProperty("accent", True)
        self.export_session_button.setText("导出本次会话包")
        self.export_now_button.setText("导出最近数据 CSV")
        self.export_diag_page_button.setText("导出诊断包")
        self.open_export_button.setText("打开导出目录")
        export_hint_label = QLabel(
            "最近数据 CSV = 快速查看最近缓存 | 会话包 = 完整留档和问题反馈首选 | 诊断包 = 环境与异常排查资料"
        )
        export_hint_label.setWordWrap(True)
        export_hint_label.setProperty("muted", True)

        replay_box = QGroupBox("数据回放")
        replay_layout = QVBoxLayout(replay_box)
        replay_row = QHBoxLayout()
        self.load_csv_button = QPushButton("加载 CSV 回放")
        self.jump_start_button = QPushButton("|<")
        self.step_back_button = QPushButton("<")
        self.start_replay_button = QPushButton("开始回放")
        self.pause_replay_button = QPushButton("暂停")
        self.step_forward_button = QPushButton(">")
        self.jump_end_button = QPushButton(">|")
        self.speed_combo = QComboBox()
        self.speed_combo.addItem("1x", 1.0)
        self.speed_combo.addItem("2x", 2.0)
        self.speed_combo.addItem("5x", 5.0)
        replay_row.addWidget(self.load_csv_button)
        replay_row.addWidget(self.jump_start_button)
        replay_row.addWidget(self.step_back_button)
        replay_row.addWidget(self.start_replay_button)
        replay_row.addWidget(self.pause_replay_button)
        replay_row.addWidget(self.step_forward_button)
        replay_row.addWidget(self.jump_end_button)
        replay_row.addWidget(QLabel("倍率"))
        replay_row.addWidget(self.speed_combo)
        replay_row.addStretch(1)

        self.replay_progress = QSlider(Qt.Horizontal)
        self.replay_progress.setRange(0, 0)
        self.replay_progress.setEnabled(False)
        self.replay_progress_bar = QProgressBar()
        self.replay_progress_bar.setRange(0, 100)
        self.replay_progress_bar.setValue(0)
        self.replay_info_label = QLabel("未加载回放数据")
        self.replay_info_label.setProperty("muted", True)
        self.replay_time_label = QLabel("时间点: -- / -- | 倍率: 1x")
        self.replay_time_label.setProperty("muted", True)

        replay_layout.addLayout(replay_row)
        replay_layout.addWidget(self.replay_progress)
        replay_layout.addWidget(self.replay_progress_bar)
        replay_layout.addWidget(self.replay_info_label)
        replay_layout.addWidget(self.replay_time_label)

        layout.addWidget(export_box)
        layout.addWidget(export_hint_label)
        layout.addWidget(replay_box, 1)
        layout.addWidget(self.change_summary_box)
        layout.addWidget(self._build_session_change_overview_box())
        return self._make_scroll_page(content, object_name="exportPageScrollArea")

    def _build_session_change_overview_box(self) -> QWidget:
        box = QGroupBox("本次参数变更总览")
        layout = QVBoxLayout(box)
        self.change_overview_hint_label = QLabel("这里仅显示当前会话最近 20 条参数变更预览；导出包仍会包含完整会话历史。")
        self.change_overview_hint_label.setWordWrap(True)
        self.change_overview_hint_label.setProperty("muted", True)
        self.change_overview_table = QTableWidget(0, 8)
        self.change_overview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.change_overview_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.change_overview_table.setAlternatingRowColors(True)
        self.change_overview_table.setWordWrap(True)
        self.change_overview_table.setMaximumHeight(360)
        self.change_overview_table.verticalHeader().setVisible(False)
        self.change_overview_table.horizontalHeader().setStretchLastSection(True)
        self.change_overview_table.setHorizontalHeaderLabels(
            ["时间", "业务动作 / 参数", "目标设备", "写前值", "目标值", "写后值", "结果", "说明"]
        )
        layout.addWidget(self.change_overview_hint_label)
        layout.addWidget(self.change_overview_table, 1)
        self._refresh_session_change_overview()
        return box

    def _connect_signals(self) -> None:
        self.refresh_ports_button.clicked.connect(lambda: self._refresh_ports(force=False))
        self.demo_button.clicked.connect(self._activate_demo_mode)
        self.connect_button.clicked.connect(self._connect_session)
        self.disconnect_button.clicked.connect(self._disconnect_session)
        self.reconnect_button.clicked.connect(self._manual_reconnect)
        self.safe_handshake_button.clicked.connect(self._run_safe_handshake)
        self.session_workflow_button.clicked.connect(self._open_workflow_from_header)
        self.session_change_badge_button.clicked.connect(self._open_session_change_overview)
        self.session_header_detail_button.clicked.connect(self._show_session_header_details)
        self.init_button.clicked.connect(self._prepare_capture_initialization_checklist)
        self.open_log_dir_button.clicked.connect(self._open_log_dir)
        self.copy_env_button.clicked.connect(self._copy_environment_info)
        self.resend_history_button.clicked.connect(self._resend_history_command)
        self.auto_apply_default_config_check.toggled.connect(self._handle_auto_apply_default_config_changed)
        self.capture_combo.currentIndexChanged.connect(self._handle_capture_mode_changed)
        self.mode_combo.currentIndexChanged.connect(self._handle_parse_mode_changed)
        self.auto_start_stream_check.toggled.connect(self._handle_auto_start_stream_toggled)

        self.export_now_button.clicked.connect(self._export_history)
        self.export_session_button.clicked.connect(self._export_session_package)
        self.export_diag_page_button.clicked.connect(self._export_diagnostic)
        self.open_export_button.clicked.connect(self._open_export_dir)
        self.load_csv_button.clicked.connect(self._load_replay_csv)
        self.start_replay_button.clicked.connect(self._start_replay)
        self.pause_replay_button.clicked.connect(self._pause_replay)
        self.step_back_button.clicked.connect(lambda: self._step_replay(-1))
        self.step_forward_button.clicked.connect(lambda: self._step_replay(1))
        self.jump_start_button.clicked.connect(lambda: self._seek_replay(0))
        self.jump_end_button.clicked.connect(self._jump_to_replay_end)
        self.replay_progress.sliderReleased.connect(self._seek_replay_from_slider)

        self.permission_combo.currentTextChanged.connect(self._apply_permission_mode)
        self.session_mode_combo.currentIndexChanged.connect(self._apply_permission_mode)
        self.target_combo.currentTextChanged.connect(self._target_changed)
        self.broadcast_check.toggled.connect(self._broadcast_toggled)
        self.read_only_lock_check.toggled.connect(self._apply_permission_mode)
        self.show_expert_check.toggled.connect(self._apply_permission_mode)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.raw_send_button.clicked.connect(self._send_raw_command)
        self.freeze_button.toggled.connect(self._toggle_freeze)
        self.monitor_action_mode1.triggered.connect(lambda: self._request_quick_output_mode("MODE1"))
        self.monitor_action_mode2.triggered.connect(lambda: self._request_quick_output_mode("MODE2"))
        self.monitor_action_upload_on.triggered.connect(lambda: self._request_quick_auto_upload(True))
        self.monitor_action_upload_off.triggered.connect(lambda: self._request_quick_auto_upload(False))
        self.monitor_action_read_snapshot.triggered.connect(self._open_monitor_snapshot_task)
        self.monitor_start_stream_button.clicked.connect(self._handle_monitor_start_stream_clicked)
        self.monitor_diagnostic_auto_parse_action.triggered.connect(self._handle_monitor_set_auto_parse_clicked)
        self.monitor_diagnostic_read_snapshot_action.triggered.connect(self._handle_monitor_read_snapshot_clicked)
        self.monitor_diagnostic_open_log_action.triggered.connect(self._open_current_log_file)
        self.apply_default_config_button.clicked.connect(self._apply_default_monitoring_config)
        self.chart_restore_defaults_button.clicked.connect(self._restore_chart_defaults)
        self.theme_combo.currentIndexChanged.connect(self._handle_theme_changed)
        self.pages.currentChanged.connect(self._handle_page_changed)
        self.monitor_aux_tabs.currentChanged.connect(self._handle_monitor_aux_tab_changed)
        self.chart_upper_view_combo.currentIndexChanged.connect(
            lambda *_: self.chart_panel.set_slot_view(0, str(self.chart_upper_view_combo.currentData() or ""))
        )
        self.chart_lower_view_combo.currentIndexChanged.connect(
            lambda *_: self.chart_panel.set_slot_view(1, str(self.chart_lower_view_combo.currentData() or ""))
        )
        self.chart_clear_upper_button.clicked.connect(lambda: self._clear_chart_slot(0))
        self.chart_clear_lower_button.clicked.connect(lambda: self._clear_chart_slot(1))
        self.monitor_aux_toolbar_toggle_button.toggled.connect(self._set_monitor_aux_expanded)
        self.monitor_aux_panel_toggle_button.toggled.connect(self._set_monitor_aux_expanded)
        self.monitor_body_split.splitterMoved.connect(self._handle_monitor_aux_splitter_moved)

        for panel in self._command_panels():
            panel.command_requested.connect(self._handle_command_request)
            panel.readback_requested.connect(self._handle_readback_request)
            panel.read_page_button.clicked.connect(lambda _=False, p=panel: self._read_panel_defaults(p))
            panel.detail_widget.retry_restore_requested.connect(self._retry_restore_auto_upload)
            panel.detail_widget.keep_upload_closed_requested.connect(self._keep_auto_upload_closed_after_restore_failure)
            panel.detail_widget.view_command_log_requested.connect(self._show_recent_command_log)

        self.custom_editor.template_saved.connect(self._custom_template_saved)

        self.controller.frame_received.connect(self._handle_frame)
        self.controller.raw_received.connect(self._handle_raw)
        self.controller.alarm_emitted.connect(self._handle_alarm)
        self.controller.metrics_updated.connect(self._handle_metrics)
        self.controller.device_ids_updated.connect(self._update_discovered_ids)
        self.controller.rx_device_state_changed.connect(self._handle_rx_device_state)
        self.controller.target_sync_requested.connect(self._handle_target_sync_requested)
        self.controller.connection_changed.connect(self._handle_connection_state)
        self.controller.command_completed.connect(self._handle_command_result)
        self.controller.error.connect(self._handle_error_message)
        self.controller.info.connect(self._handle_info_message)

    def _handle_theme_changed(self) -> None:
        self.theme_change_requested.emit(str(self.theme_combo.currentData() or "dark"))

    def _handle_page_changed(self, index: int) -> None:
        if index == self.monitor_tab_index:
            self._refresh_monitor_from_latest()
            self._refresh_monitor_quick_controls()
        if index == self.expert_tab_index:
            self._flush_terminal_buffer()

    def _restore_current_page_from_state(self) -> None:
        target_index = max(0, min(int(self._restored_page_index or 0), self.pages.count() - 1))
        if target_index == self.expert_tab_index and not self.pages.isTabVisible(self.expert_tab_index):
            target_index = self.settings_tab_index
        self.pages.setCurrentIndex(target_index)

    def _handle_monitor_aux_tab_changed(self, index: int) -> None:
        widget = self.monitor_aux_tabs.widget(index)
        if widget is self.status_panel and self.last_frame is not None:
            self.status_panel.update_frame(self.last_frame)
        if widget is self.raw_frames:
            self.raw_frames.render()

    def _refresh_monitor_from_latest(self) -> None:
        self._update_anomaly_summary()
        if self.monitor_frozen:
            return
        if self.last_frame is not None:
            self._update_data_cards(self.last_frame)
            self.chart_panel.request_refresh(immediate=True)
            self.status_panel.update_frame(self.last_frame)
        if self.last_metrics is not None:
            self._update_monitor_metrics_cards(self.last_metrics)
        current_index = self.monitor_aux_tabs.currentIndex()
        if current_index >= 0:
            self._handle_monitor_aux_tab_changed(current_index)

    def _set_monitor_aux_expanded(self, expanded: bool) -> None:
        if expanded:
            self.monitor_aux_container.show()
            self.monitor_aux_container.setMaximumHeight(16777215)
            self.monitor_aux_tabs.show()
            self._monitor_aux_expanded = True
            self.monitor_aux_panel_toggle_button.blockSignals(True)
            self.monitor_aux_panel_toggle_button.setChecked(True)
            self.monitor_aux_panel_toggle_button.setText("收起辅助区")
            self.monitor_aux_panel_toggle_button.blockSignals(False)
            self.monitor_aux_toolbar_toggle_button.blockSignals(True)
            self.monitor_aux_toolbar_toggle_button.setChecked(True)
            self.monitor_aux_toolbar_toggle_button.setText("收起辅助区")
            self.monitor_aux_toolbar_toggle_button.blockSignals(False)
            QTimer.singleShot(0, lambda: self._apply_monitor_aux_height(self._monitor_aux_last_height))
            current_index = self.monitor_aux_tabs.currentIndex()
            if current_index >= 0:
                self._handle_monitor_aux_tab_changed(current_index)
            return

        current_sizes = self.monitor_body_split.sizes()
        if len(current_sizes) > 1 and current_sizes[1] > 80:
            self._monitor_aux_last_height = current_sizes[1]
        self._monitor_aux_expanded = False
        self.monitor_aux_panel_toggle_button.blockSignals(True)
        self.monitor_aux_panel_toggle_button.setChecked(False)
        self.monitor_aux_panel_toggle_button.setText("展开辅助区")
        self.monitor_aux_panel_toggle_button.blockSignals(False)
        self.monitor_aux_toolbar_toggle_button.blockSignals(True)
        self.monitor_aux_toolbar_toggle_button.setChecked(False)
        self.monitor_aux_toolbar_toggle_button.setText("展开辅助区")
        self.monitor_aux_toolbar_toggle_button.blockSignals(False)
        self.monitor_aux_tabs.hide()
        self.monitor_aux_container.hide()
        self.monitor_aux_container.setMaximumHeight(0)
        QTimer.singleShot(0, lambda: self._apply_monitor_aux_height(0))

    def _apply_monitor_aux_height(self, desired_height: int) -> None:
        total = sum(self.monitor_body_split.sizes())
        if total <= 0:
            total = max(self.monitor_body_split.height(), 400)
        if self._monitor_aux_expanded:
            bottom = max(120, min(int(desired_height or 140), max(120, total - 220)))
        else:
            bottom = 0
        top = max(220, total - bottom)
        self.monitor_body_split.setSizes([top, bottom])

    def _handle_monitor_aux_splitter_moved(self, _pos: int, _index: int) -> None:
        if not self._monitor_aux_expanded:
            return
        sizes = self.monitor_body_split.sizes()
        if len(sizes) > 1 and sizes[1] >= 120:
            self._monitor_aux_last_height = sizes[1]

    def _schedule_diagnostic_snapshot_refresh(self, *, immediate: bool = False) -> None:
        self._diagnostic_snapshot_dirty = True
        if immediate:
            self._flush_diagnostic_snapshot()
            return
        if not self.diagnostic_snapshot_timer.isActive():
            self.diagnostic_snapshot_timer.start()

    def _flush_diagnostic_snapshot(self) -> None:
        if not self._diagnostic_snapshot_dirty:
            return
        self._diagnostic_snapshot_dirty = False
        self._refresh_diagnostic_snapshot()

    def _queue_terminal_line(self, line: str) -> None:
        self._expert_terminal_pending.append(line)
        if self.pages.currentIndex() == self.expert_tab_index:
            if not self.expert_terminal_timer.isActive():
                self.expert_terminal_timer.start()

    def _flush_terminal_buffer(self) -> None:
        if not self._expert_terminal_pending:
            return
        if self.pages.currentIndex() != self.expert_tab_index:
            return
        cursor = self.raw_terminal_log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        existing_text = bool(self.raw_terminal_log.toPlainText())
        for line in self._expert_terminal_pending:
            if existing_text:
                cursor.insertText("\n")
            cursor.insertText(line)
            existing_text = True
        self.raw_terminal_log.setTextCursor(cursor)
        self.raw_terminal_log.ensureCursorVisible()
        self._expert_terminal_pending.clear()

    def _device_command_definition(self, command_id: str) -> CommandDefinition:
        return self.registry.get(command_id, self._current_profile_name())

    def _set_device_quick_status(self, text: str) -> None:
        self._device_quick_status = text
        if hasattr(self, "monitor_quick_status_label"):
            self.monitor_quick_status_label.setText(text)

    def _refresh_expected_hz_hint(self) -> None:
        if not hasattr(self, "expected_hz_hint_label"):
            return
        if self.stream_hz_edit.value() > self._capture_initialization_ftd_max_hz():
            self.expected_hz_hint_label.setText("当前期望接收频率高于设备 FTD 最大支持值，丢帧统计可能失真。")
            self.expected_hz_hint_label.setProperty("warning", True)
            self.expected_hz_hint_label.setProperty("muted", False)
        else:
            self.expected_hz_hint_label.setText("期望接收频率仅用于监测统计，不会直接写入设备。")
            self.expected_hz_hint_label.setProperty("warning", False)
            self.expected_hz_hint_label.setProperty("muted", True)
        self.expected_hz_hint_label.style().unpolish(self.expected_hz_hint_label)
        self.expected_hz_hint_label.style().polish(self.expected_hz_hint_label)

    @staticmethod
    def _auto_upload_state_text(state: str) -> str:
        return auto_upload_state_label_zh(str(state or "unknown")) or "未确认"

    @staticmethod
    def _quiet_read_original_state_text(state: str) -> str:
        return auto_upload_state_label_zh(str(state or "unknown")) or "未确认"

    @staticmethod
    def _quiet_read_after_state_text(result: str) -> str:
        normalized = str(result or "").strip()
        if normalized == "pending_restore":
            return "正在恢复主动上传"
        if normalized == "left_unknown":
            return "未确认"
        return restore_result_label_zh(normalized)

    @staticmethod
    def _has_auto_upload_metadata(result: CommandResult) -> bool:
        return any(
            [
                str(result.original_auto_upload_state or "").strip(),
                str(result.restore_policy or "").strip(),
                bool(result.restore_attempted),
                str(result.restore_result or "").strip(),
            ]
        )

    def _auto_upload_detail_text(self, base_text: str, result: CommandResult) -> str:
        return enrich_auto_upload_detail_text(
            base_text,
            original_auto_upload_state=str(result.original_auto_upload_state or ""),
            restore_policy=str(result.restore_policy or ""),
            restore_attempted=result.restore_attempted if self._has_auto_upload_metadata(result) else None,
            restore_result=str(result.restore_result or ""),
        )

    def _quiet_read_status_suffix(self) -> str:
        if not self._quiet_read_original_state:
            return ""
        original_text = self._quiet_read_original_state_text(self._quiet_read_original_state)
        after_text = self._quiet_read_after_state_text(self._quiet_read_after_state) or "未确认"
        return f" | 原状态：{original_text} | 读取后状态：{after_text}"

    def _clear_quiet_read_status(self) -> None:
        self._quiet_read_original_state = ""
        self._quiet_read_after_state = ""

    def _sync_quiet_read_status_from_result(self, result: CommandResult) -> bool:
        if not result.original_auto_upload_state:
            return False
        self._quiet_read_original_state = str(result.original_auto_upload_state or "")
        if result.restore_result:
            self._quiet_read_after_state = str(result.restore_result or "")
        return True

    def _set_auto_upload_state(self, state: str, *, detail: str = "") -> None:
        self._auto_upload_state = str(state or "unknown")
        if state == "on":
            self._device_auto_upload = True
        elif state in {"off", "temporarily_silenced", "restore_failed"}:
            self._device_auto_upload = False
        label_text = f"自动上传状态：{self._auto_upload_state_text(self._auto_upload_state)}"
        label_text += self._quiet_read_status_suffix()
        if detail:
            label_text += f" | {detail}"
        if hasattr(self, "monitor_auto_upload_state_label"):
            self.monitor_auto_upload_state_label.setText(label_text)
        if hasattr(self, "monitor_upload_badge"):
            short_text = self._auto_upload_state_text(self._auto_upload_state)
            self.monitor_upload_badge.setText(f"上传：{short_text}")
            self.monitor_upload_badge.setToolTip(label_text)
        for panel in self._command_panels():
            panel.set_auto_upload_state(self._auto_upload_state)
        if self.custom_panel is not None:
            self.custom_panel.set_auto_upload_state(self._auto_upload_state)

    def _refresh_monitor_quick_controls(self) -> None:
        inferred_mode = f"MODE{self.last_frame.mode}" if self.last_frame is not None else self._device_output_mode
        connection_text = "已连接" if self.connected else ("回放中" if self.replay_running else "未连接")
        connection_state = "connected" if self.connected else ("replay" if self.replay_running else "disconnected")
        self.monitor_connection_badge.setText(connection_text)
        self.monitor_connection_badge.setProperty("state", connection_state)
        self.monitor_connection_badge.style().unpolish(self.monitor_connection_badge)
        self.monitor_connection_badge.style().polish(self.monitor_connection_badge)
        self.monitor_mode_label.setText(inferred_mode)
        self._set_auto_upload_state(self._auto_upload_state)

        self.monitor_device_action_button.setEnabled(True)
        start_stream_enabled = self.connected and not self.replay_running and self._current_acquisition_mode() == "LISTEN"
        self.monitor_start_stream_button.setEnabled(start_stream_enabled)
        retry_stream = (
            self._stream_runtime_state in {"running", "failed"}
            or self._waiting_for_first_stream_frame
            or self._first_frame_wait_timed_out
            or self.raw_rx_seen_since_connect
        )
        self.monitor_start_stream_button.setText("重新启动实时流" if retry_stream else "启动实时流")
        self.monitor_diagnostic_button.setEnabled(True)
        self.monitor_diagnostic_auto_parse_action.setEnabled(not self.replay_running and self._current_parse_mode() != "AUTO")
        self.monitor_diagnostic_read_snapshot_action.setEnabled(self.connected and not self.replay_running)
        self.monitor_diagnostic_open_log_action.setEnabled(True)
        self.apply_default_config_button.setEnabled(True)
        self.monitor_aux_toolbar_toggle_button.blockSignals(True)
        self.monitor_aux_toolbar_toggle_button.setChecked(self._monitor_aux_expanded)
        self.monitor_aux_toolbar_toggle_button.setText("收起辅助区" if self._monitor_aux_expanded else "展开辅助区")
        self.monitor_aux_toolbar_toggle_button.blockSignals(False)
        self.monitor_device_action_button.setToolTip("仅预填、定位或跳转，不会直接发送写命令。")
        self.monitor_start_stream_button.setToolTip("直接发送 SETCOMWAY=1 启动主动上传；不会修改 MODE、FTD、SENCO 或其他参数。")
        self.monitor_diagnostic_button.setToolTip("收纳解析模式、关键配置快照和串口日志入口。")
        self.monitor_diagnostic_auto_parse_action.setToolTip("将当前解析模式切换为自动识别；不会发送任何设备命令。")
        self.monitor_diagnostic_read_snapshot_action.setToolTip("直接读取 ID / MODE / FTD / SETCOM 关键配置快照。")
        current_log_path = str(self.controller.log_path or "").strip()
        if current_log_path:
            self.monitor_diagnostic_open_log_action.setToolTip(f"打开当前串口日志：{current_log_path}")
        else:
            self.monitor_diagnostic_open_log_action.setToolTip("打开日志目录。")
        self._refresh_chart_status_badge()
        self._update_hard_status_bar()

    def _set_monitor_idle_placeholder(self, body: str, *, title: str = "等待实时数据") -> None:
        if hasattr(self, "chart_panel"):
            self.chart_panel.set_idle_placeholder(title, body)

    def _reset_stream_start_tracking(self) -> None:
        self._planned_auto_start_stream_payload = ""
        self._planned_auto_start_stream_reason = ""
        self._pending_stream_start_payload = ""
        self._waiting_for_first_stream_frame = False
        self._first_frame_wait_timed_out = False
        self._stream_runtime_state = "idle"
        self.first_frame_timer.stop()
        self._auto_start_stream_schedule_token += 1

    def _build_stream_start_payload_preview(self) -> str:
        return self.registry.build_preview(
            "SETCOMWAY",
            self._current_target_id(),
            {"mode": "1"},
            profile_name=self._current_profile_name(),
        )

    def _build_stream_start_context(self, *, automatic: bool) -> dict[str, object]:
        if automatic:
            return {
                "system_action": "auto_start_stream",
                "action_type": "auto_start_stream",
                "source_page": AUTO_START_STREAM_SOURCE_PAGE,
                "auto_upload_state": "on",
            }
        return {
            "action_type": "manual_stream_start",
            "source_page": MANUAL_STREAM_START_SOURCE_PAGE,
            "auto_upload_state": "on",
        }

    def _stream_status_from_reason(self, reason: str) -> tuple[bool, str, str, str]:
        text = str(reason or "").strip()
        if "严格只读" in text:
            return False, "严格只读阻止", "当前为严格只读，控制写入和实时流都已禁止。", "blocked_read_only"
        if "严格只听" in text:
            return False, "严格只听阻止", "当前为严格只听模式，控制写入和实时流都已禁止。", "blocked_listen_only"
        if "自动广播" in text or "FFF" in text:
            return False, "禁止自动广播", "FFF 目标不会自动发送 SETCOMWAY=1，请切换为单设备 ID。", "blocked_broadcast"
        if "回放" in text:
            return False, "回放阻止", "回放期间不会向真实设备发送 SETCOMWAY=1。", "blocked_replay"
        if "手动读取模式" in text:
            return False, "采集方式阻止", "当前采集方式不是自动上传 / LISTEN，不会启动实时流。", "blocked_capture_mode"
        if "未连接" in text or "真实串口" in text:
            return False, "待连接", "连接真实设备后才可启动实时流。", "disconnected"
        if "目标设备 ID 无效" in text:
            return False, "目标待确认", text, "blocked_target"
        if "仅允许发送到当前目标设备" in text:
            return False, "目标不一致", text, "blocked_target"
        return False, "禁止", text or "当前状态下不可启动实时流。", "blocked"

    def _build_stream_start_status(self) -> tuple[bool, str, str, str]:
        if self._current_acquisition_mode() != "LISTEN":
            return self._stream_status_from_reason("当前为手动读取模式，不会自动启动实时流。")
        if self.replay_running or self._current_session_mode() == SESSION_MODE_REPLAY:
            return self._stream_status_from_reason("回放期间禁止向真实设备发送命令。")
        try:
            payload = self._build_stream_start_payload_preview()
        except Exception:
            return False, "禁止", "启动实时流预览命令构建失败。", "blocked"

        self.controller.current_config = self._build_config()
        self.controller.connected = self.connected
        validation_ok, reason = self.controller.validate_stream_start_payload(payload)
        if not validation_ok:
            return self._stream_status_from_reason(reason)
        if self._pending_stream_start_payload or self._stream_runtime_state == "starting":
            return True, "启动中", "系统正在等待 SETCOMWAY=1 的 ACK。", "starting"
        if self._stream_runtime_state == "failed":
            return True, "启动失败", "最近一次 SETCOMWAY=1 未成功，可重试。", "failed"
        if self._stream_runtime_state == "running" or self._waiting_for_first_stream_frame or self._first_frame_wait_timed_out:
            return True, "已开启", "SETCOMWAY=1 已收到 ACK，正在等待或接收实时数据。", "running"
        return True, "可启动", "当前会话允许启动 SETCOMWAY=1 实时流。", "ready"

    def _validated_stream_start_payload(self, *, automatic: bool) -> tuple[str, str]:
        if not self.connected:
            return "", "当前未连接设备。"
        if self.replay_running:
            return "", "当前为回放模式，不会启动实时流。"
        if self._current_acquisition_mode() != "LISTEN":
            if automatic:
                return "", "当前为手动读取模式，不会自动启动实时流。"
            return "", "当前采集方式为手动读取，请切换为“自动上传”后再启动实时流。"
        if automatic and not self._current_auto_start_stream_enabled():
            return "", "已关闭“连接后自动启动实时流”，系统将只监听现有数据流。"
        self.controller.update_config(self._build_config())
        payload = self._build_stream_start_payload_preview()
        ok, reason = self.controller.validate_stream_start_payload(payload)
        if not ok:
            return "", reason
        return payload, ""

    def _should_auto_start_stream_after_connect(self) -> bool:
        payload, reason = self._validated_stream_start_payload(automatic=True)
        self._planned_auto_start_stream_payload = payload
        self._planned_auto_start_stream_reason = reason
        return bool(payload)

    def _schedule_auto_start_stream(self) -> None:
        payload = str(self._planned_auto_start_stream_payload or "")
        if not payload:
            return
        self._stream_runtime_state = "starting"
        self._refresh_monitor_quick_controls()
        self._auto_start_stream_schedule_token += 1
        schedule_token = self._auto_start_stream_schedule_token
        QTimer.singleShot(180, lambda token=schedule_token, planned_payload=payload: self._execute_scheduled_auto_start_stream(token, planned_payload))

    def _execute_scheduled_auto_start_stream(self, schedule_token: int, planned_payload: str) -> None:
        if schedule_token != self._auto_start_stream_schedule_token or not self.connected:
            return
        payload, reason = self._validated_stream_start_payload(automatic=True)
        if not payload or payload != planned_payload:
            self._stream_runtime_state = "idle"
            self._show_waiting_for_stream_hint(reason=reason)
            self._refresh_monitor_quick_controls()
            return
        self._start_stream_flow(payload, automatic=True)

    def _start_stream_flow(self, payload: str, *, interactive: bool = False, automatic: bool = False) -> bool:
        normalized_payload = str(payload or "").strip()
        if not normalized_payload:
            return False
        if self._pending_stream_start_payload == normalized_payload:
            self._set_device_quick_status("启动实时流命令已发送，正在等待设备 ACK。")
            self._set_monitor_idle_placeholder("正在启动主动上传，等待设备 ACK。")
            return True
        self._pending_stream_start_payload = normalized_payload
        self._waiting_for_first_stream_frame = False
        self._first_frame_wait_timed_out = False
        self._stream_runtime_state = "starting"
        self.first_frame_timer.stop()
        self._set_device_quick_status("正在启动主动上传，等待设备 ACK。")
        self._set_monitor_idle_placeholder("正在启动主动上传，等待设备 ACK。")
        self.controller.send_payload(
            normalized_payload,
            expectation="ack",
            timeout_ms=self._current_command_timeout_ms(),
            context=self._build_stream_start_context(automatic=automatic),
        )
        if interactive:
            self.status_panel.append_event("监测", self._fmt_ts(datetime.now()), f"{normalized_payload} -> 正在等待 ACK")
        self._refresh_monitor_quick_controls()
        return True

    def _handle_monitor_start_stream_clicked(self) -> None:
        payload, reason = self._validated_stream_start_payload(automatic=False)
        if not payload:
            self._show_waiting_for_stream_hint(reason=reason)
            QMessageBox.warning(self, "无法启动实时流", reason)
            return
        self._start_stream_flow(payload, interactive=True, automatic=False)

    def _handle_monitor_set_auto_parse_clicked(self) -> None:
        self._set_parse_mode("AUTO")
        self._handle_parse_mode_changed()
        self._set_device_quick_status("解析模式已切换为自动识别，后续将按 AUTO 尝试解析实时数据。")
        if not self.valid_frame_seen_since_connect:
            self._set_monitor_idle_placeholder("解析模式已切换为自动识别，后续将按 AUTO 尝试解析实时数据。")

    def _handle_monitor_read_snapshot_clicked(self) -> None:
        self._read_panel_defaults(self.control_panel)

    def _handle_first_frame_timeout(self) -> None:
        if not self.connected or self.valid_frame_seen_since_connect:
            return
        self._waiting_for_first_stream_frame = False
        self._first_frame_wait_timed_out = True
        self._stream_runtime_state = "running"
        timeout_message = self._first_frame_timeout_message()
        self._set_device_quick_status(timeout_message)
        self._set_monitor_idle_placeholder(timeout_message)
        self._refresh_monitor_quick_controls()

    def _show_waiting_for_stream_hint(self, *, reason: str = "") -> None:
        if not self.connected:
            message = "请先连接设备或加载回放。"
        elif reason:
            message = reason
        elif self._current_acquisition_mode() != "LISTEN":
            message = "当前为手动读取模式，请切换为自动上传或使用“读取关键配置”。"
        elif self._first_frame_wait_timed_out:
            message = self._first_frame_timeout_message()
        elif self.raw_rx_seen_since_connect and not self.valid_frame_seen_since_connect and self.parse_failure_count_since_connect > 0:
            message = self._raw_rx_parse_failure_message()
        else:
            message = "已连接，等待设备主动上传实时数据。"
        self._set_device_quick_status(message)
        self._set_monitor_idle_placeholder(message)

    def _handle_stream_start_result(self, result: CommandResult) -> None:
        if not self._pending_stream_start_payload or result.command != self._pending_stream_start_payload:
            return
        self._pending_stream_start_payload = ""
        self._record_stream_start_change(result)
        if result.ok:
            self._waiting_for_first_stream_frame = True
            self._first_frame_wait_timed_out = False
            self._stream_runtime_state = "running"
            self.first_frame_timer.start(FIRST_FRAME_TIMEOUT_MS)
            self._set_device_quick_status("主动上传已开启，等待实时数据。")
            self._set_monitor_idle_placeholder("主动上传已开启，等待实时数据。")
            self._refresh_monitor_quick_controls()
            return
        self._waiting_for_first_stream_frame = False
        self._first_frame_wait_timed_out = False
        self._stream_runtime_state = "failed"
        self.first_frame_timer.stop()
        failure_text = "已连接，但主动上传启动失败，请检查目标设备、波特率、线缆或手动重试。"
        self._set_device_quick_status(failure_text)
        self._set_monitor_idle_placeholder(failure_text)
        self._refresh_monitor_quick_controls()

    def _request_quick_output_mode(self, mode_name: str) -> None:
        mode_value = "1" if mode_name == "MODE1" else "2"
        message = self._prepare_control_write_action(
            "MODE",
            {"mode": mode_value},
            prepared_text=f"目标值 {mode_name}",
        )
        self._set_device_quick_status(f"监测页快捷动作已切换到“设备控制”：{message}")

    def _request_quick_auto_upload(self, enabled: bool) -> None:
        target_listen = bool(enabled)
        action_text = "开启主动上传" if target_listen else "关闭主动上传"
        followup_hint = ""
        if target_listen:
            followup_hint = (
                "本次仅切换自动上传开关，不会修改 FTD 上传频率；"
                f"当前设备 FTD 写入频率为 {self.device_ftd_hz_edit.value()} Hz；"
                f"当前期望接收频率为 {self.stream_hz_edit.value()} Hz，仅用于监测统计/丢帧判断，不会写入设备。"
            )
        message = self._prepare_control_write_action(
            "SETCOMWAY",
            {"mode": "1" if target_listen else "0"},
            prepared_text=f"“{action_text}”",
            followup_hint=followup_hint,
        )
        self._set_device_quick_status(f"监测页快捷动作已切换到“设备控制”：{message}")

    def _open_monitor_snapshot_task(self) -> None:
        self._open_task_read_snapshot()
        self._set_device_quick_status("监测页快捷动作已跳转到“设备控制”：请先读取关键配置快照并人工确认。")

    def _apply_default_monitoring_config(self, *, show_message: bool = True) -> None:
        message = self._prepare_default_monitoring_checklist(
            trigger_source="manual",
            switch_to_control_page=show_message,
        )
        self._set_device_quick_status(message)

    def _prepare_capture_initialization_checklist(self) -> None:
        message = self._prepare_capture_initialization_steps(switch_to_control_page=True, trigger_source="manual")
        self._set_control_common_feedback(message)
        self._set_device_quick_status(message)

    def _current_prepared_checklist_label(self) -> str:
        return self._prepared_checklist_label or DEFAULT_MONITORING_CHECKLIST_LABEL

    def _current_prepared_checklist_source_page(self) -> str:
        return self._prepared_checklist_source_page or DEFAULT_MONITORING_SOURCE_PAGE

    def _current_prepared_checklist_kind(self) -> str:
        return self._prepared_checklist_kind or "default_monitoring"

    def _set_prepared_checklist_context(self, *, label: str, source_page: str, kind: str) -> None:
        self._prepared_checklist_label = label
        self._prepared_checklist_source_page = source_page
        self._prepared_checklist_kind = kind

    def _reset_prepared_checklist_context(self) -> None:
        self._set_prepared_checklist_context(
            label=DEFAULT_MONITORING_CHECKLIST_LABEL,
            source_page=DEFAULT_MONITORING_SOURCE_PAGE,
            kind="default_monitoring",
        )

    def _expected_checklist_device(self) -> str:
        target_id = self._current_target_id()
        if target_id.isdigit():
            return target_id
        active_numeric_ids = sorted({device_id for device_id in self.active_online_device_ids if str(device_id).isdigit()})
        if len(active_numeric_ids) == 1:
            return active_numeric_ids[0]
        latest_id = str(self.latest_online_device_id or "").strip().upper()
        if latest_id.isdigit():
            return latest_id
        return ""

    def _checklist_step_verification_policy(self, command_id: str) -> str:
        return "readback" if self.registry.supports_readback(command_id) else "ack_only"

    @staticmethod
    def _business_text_for_command(command_id: str) -> str:
        normalized = str(command_id or "").upper()
        mapping = {
            "MODE": "MODE：改变工作模式",
            "FTD": "FTD：改变自动上传频率",
            "SETCOMWAY": "SETCOMWAY：开启/关闭主动上传",
        }
        return mapping.get(normalized, normalized or "设备写入步骤")

    def _step_scope_text(self, step: DefaultMonitoringChecklistStep) -> str:
        payload_target = str(step.planned_target_id or "").strip().upper()
        if not payload_target:
            envelope = YGasProtocol.parse_command(step.planned_payload)
            if envelope is not None:
                payload_target = str(envelope.target_id or "").strip().upper()
        if str(step.effective_scope or "").strip().lower() == "broadcast" or payload_target == "FFF" or step.target_is_broadcast:
            return "FFF 广播：会影响总线上所有响应设备"
        expected_device = step.expected_device or (payload_target if payload_target.isdigit() else self._expected_checklist_device())
        if expected_device:
            return f"单设备：{expected_device}"
        return "单设备：待确认"

    def _step_status_text(self, step: DefaultMonitoringChecklistStep) -> str:
        if step.status == CHECKLIST_STATUS_RUNNING:
            if step.verification_policy == "readback":
                return "执行中：等待 ACK / 写后复核"
            return "执行中：等待 ACK"
        if step.status == CHECKLIST_STATUS_DONE:
            if step.verification_policy == "readback":
                return "已完成：写后复核成功"
            return "已完成：ACK-only"
        if step.status == CHECKLIST_STATUS_FAILED:
            return "已失败"
        if step.status == CHECKLIST_STATUS_CURRENT:
            return "待确认：已预填，未发送"
        if step.status == CHECKLIST_STATUS_CANCELLED:
            return "已取消"
        return "待确认"

    def _step_evidence_text(self, step: DefaultMonitoringChecklistStep) -> str:
        if step.last_evidence_text:
            return step.last_evidence_text
        if step.status == CHECKLIST_STATUS_RUNNING:
            return "尚未拿到 ACK / 复核证据"
        if step.status in {CHECKLIST_STATUS_CURRENT, CHECKLIST_STATUS_PENDING}:
            return "尚未发送"
        return "--"

    def _step_timeout_reason_text(self, step: DefaultMonitoringChecklistStep) -> str:
        if step.last_timeout_reason:
            return step.last_timeout_reason
        return "--"

    def _step_detail_text(self, step: DefaultMonitoringChecklistStep) -> str:
        details = [
            f"当前状态：{self._step_status_text(step)}",
            f"业务解释：{step.business_text or self._business_text_for_command(step.linked_command_id)}",
            f"目标范围：{self._step_scope_text(step)}",
            f"计划命令：{step.planned_payload or '--'}",
            f"风险提示：{step.risk_text or '按统一安全链逐步确认发送。'}",
            f"最近证据：{self._step_evidence_text(step)}",
            f"超时原因：{self._step_timeout_reason_text(step)}",
        ]
        if step.last_message:
            details.append(f"状态说明：{step.last_message}")
        return "\n".join(details)

    def _overall_checklist_status_text(self) -> str:
        if not self._default_monitoring_steps:
            return "未准备"
        if self._running_default_monitoring_step() is not None:
            return "执行中"
        if self._failed_default_monitoring_step() is not None:
            return "已失败"
        if self._default_monitoring_checklist_completed():
            return "已完成"
        return "待确认"

    def _current_step_progress_text(self) -> str:
        if not self._default_monitoring_steps:
            return "0 / 0"
        active = (
            self._running_default_monitoring_step()
            or self._current_default_monitoring_step()
            or self._failed_default_monitoring_step()
            or self._pending_default_monitoring_step()
        )
        if active is None:
            return f"{len(self._default_monitoring_steps)} / {len(self._default_monitoring_steps)}"
        return f"{active[1].step_index} / {len(self._default_monitoring_steps)}"

    def _current_checklist_target_text(self) -> str:
        target_id = self._current_target_id() or "--"
        expected_device = self._expected_checklist_device()
        if target_id == "FFF":
            if expected_device:
                return f"当前会话目标：FFF | 期望设备：{expected_device}"
            return "当前会话目标：FFF | 期望设备：待确认"
        return f"当前会话目标：{target_id}"

    def _current_checklist_scope_text(self) -> str:
        if not self._default_monitoring_steps:
            target_id = self._current_target_id()
            if str(target_id).upper() == "FFF":
                return "FFF 广播：会影响总线上所有响应设备"
            return f"单设备：{target_id or '--'}"
        current = (
            self._running_default_monitoring_step()
            or self._current_default_monitoring_step()
            or self._failed_default_monitoring_step()
            or self._pending_default_monitoring_step()
        )
        if current is None:
            current = (0, self._default_monitoring_steps[0])
        return self._step_scope_text(current[1])

    def _build_checklist_step(
        self,
        *,
        step_index: int,
        step_key: str,
        title: str,
        command_id: str,
        values: dict[str, str],
        target_summary: str,
        planned_payload: str,
        note_text: str,
        flow_label: str,
        source_page: str,
        checklist_kind: str,
    ) -> DefaultMonitoringChecklistStep:
        envelope = YGasProtocol.parse_command(planned_payload)
        planned_target_id = str(envelope.target_id or "").strip().upper() if envelope is not None else ""
        target_is_broadcast = planned_target_id == "FFF"
        effective_scope = "broadcast" if target_is_broadcast else "single"
        expected_device = planned_target_id if planned_target_id.isdigit() else self._expected_checklist_device()
        return DefaultMonitoringChecklistStep(
            step_index=step_index,
            step_key=step_key,
            title=title,
            target_summary=target_summary,
            business_text=self._business_text_for_command(command_id),
            planned_payload=planned_payload,
            flow_label=flow_label,
            source_page=source_page,
            checklist_kind=checklist_kind,
            step_id=step_key,
            expected_device=expected_device,
            planned_target_id=planned_target_id,
            target_is_broadcast=target_is_broadcast,
            effective_scope=effective_scope,
            verification_policy=self._checklist_step_verification_policy(command_id),
            risk_text=note_text or "",
            status=CHECKLIST_STATUS_PENDING,
            linked_command_id=command_id,
            prefill_values={key: str(value) for key, value in values.items()},
            last_message=note_text or "待确认，尚未发送。",
        )

    def _schedule_default_monitoring_config(self) -> None:
        if (
            self._default_config_scheduled
            or not self.connected
            or self.replay_running
            or not self.auto_apply_default_config_check.isChecked()
        ):
            return
        self._default_config_scheduled = True
        self._default_config_scheduled_target_id = self._current_target_id()
        self._set_device_quick_status("已连接，正在自动准备校准联调清单；仅预填，不发送。")
        self._refresh_monitor_quick_controls()
        QTimer.singleShot(200, self._execute_scheduled_default_monitoring_prepare)

    def _execute_scheduled_default_monitoring_prepare(self) -> None:
        scheduled_target_id = self._default_config_scheduled_target_id
        self._default_config_scheduled = False
        self._default_config_scheduled_target_id = ""
        if (
            not self.connected
            or self.replay_running
            or not self.auto_apply_default_config_check.isChecked()
            or scheduled_target_id != self._current_target_id()
        ):
            return
        self._set_device_quick_status(
            self._prepare_default_monitoring_checklist(
                trigger_source="auto",
                switch_to_control_page=True,
            )
        )

    def _handle_metric_card_slot_request(self, metric_key: str, slot_index: int) -> None:
        if self.chart_panel.set_slot_metric(slot_index, metric_key):
            slot_name = "上图" if slot_index == 0 else "下图"
            label = self.chart_panel.friendly_metric_name(metric_key)
            self._set_device_quick_status(f"已将 {label} 切换到{slot_name}。")
        else:
            self._set_device_quick_status("该指标当前没有可用的快捷图槽视图。")
        self._refresh_chart_config_summary()
        self._refresh_monitor_quick_controls()

    def _clear_chart_slot(self, slot_index: int) -> None:
        slot_name = "上图" if slot_index == 0 else "下图"
        if self.chart_panel.clear_slot(slot_index):
            self._set_device_quick_status(f"已从{slot_name}移除当前变量。")
            self._refresh_chart_config_summary()

    def _restore_chart_defaults(self) -> None:
        self.chart_panel.restore_default_views()
        self._set_device_quick_status("图表已恢复默认视图。")
        self._refresh_chart_config_summary()

    def _refresh_chart_config_summary(self) -> None:
        self.chart_upper_summary_label.setText(self.chart_panel.slot_summary_text(0))
        self.chart_lower_summary_label.setText(self.chart_panel.slot_summary_text(1))

    def _refresh_session_safety_hint(self) -> None:
        if not hasattr(self, "permission_combo"):
            self.session_safety_hint_label.setText(
                "当前为默认实时监测模式，连接后可自动启动主动上传，其他写入仍保持受限。"
            )
            return
        if self._is_safe_onboarding_state():
            self.session_safety_hint_label.setText(
                "当前为默认实时监测模式，连接后可自动启动主动上传，其他写入仍保持受限。"
            )
        else:
            self.session_safety_hint_label.setText(
                "当前会话已偏离默认实时监测模式；进入写入阶段前仍需逐项确认安全条件。"
            )

    def _expert_terminal_available(self) -> bool:
        return self.permission_combo.currentText() == "EXPERT" and self.show_expert_check.isChecked()

    def _expert_terminal_block_reason(self) -> str:
        if self.permission_combo.currentText() != "EXPERT":
            return "当前权限等级不是 EXPERT，专家终端保持隐藏。"
        if not self.show_expert_check.isChecked():
            return "当前尚未启用专家终端，请在“连接与设备模式”页显式开启后再使用。"
        return ""

    def _handle_auto_apply_default_config_changed(self, enabled: bool) -> None:
        if enabled:
            self._set_device_quick_status(
                    "已启用“连接后自动准备校准联调清单（不发送）”，下次连接成功后会自动准备待确认清单并定位到第一步，待人工确认。"
            )
        else:
            self._default_config_scheduled = False
            self._default_config_scheduled_target_id = ""
            self._set_device_quick_status(
                "连接后不会自动准备校准联调清单；实时监测仍按当前“连接后自动启动实时流”设置执行。"
            )
        self._refresh_monitor_quick_controls()

    def _is_safe_onboarding_state(self) -> bool:
        return (
            self.permission_combo.currentText() == "READ_ONLY"
            and self._current_session_mode() == SESSION_MODE_MONITORING
            and not self.read_only_lock_check.isChecked()
            and not self.broadcast_check.isChecked()
        )

    def _set_task_entry_feedback(self, text: str) -> None:
        self.task_entry_feedback_label.setText(text)

    def _set_status_label_tone(self, label: QLabel, text: str, tone: str) -> None:
        label.setText(text)
        label.setProperty("muted", tone == "neutral")
        if tone == "success":
            label.setProperty("risk", "low")
        elif tone == "warning":
            label.setProperty("risk", "medium")
        elif tone == "danger":
            label.setProperty("risk", "high")
        else:
            label.setProperty("risk", None)
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _verification_status_from_entry(entry: SessionChangeEntry) -> str:
        result_text = str(entry.result_text or "").strip()
        if result_text == "一致":
            return "verified_consistent"
        if result_text == "不一致":
            return "verified_mismatch"
        if result_text == "无法验证":
            return "verification_unavailable"
        if result_text == "写前读取失败":
            return "pre_read_failed"
        if result_text == "写入失败":
            return "write_failed"
        if result_text in {"未收到 ACK", "连接断开，未收到 ACK", "ACK 未确认"}:
            return VERIFICATION_STATUS_ACK_UNCONFIRMED
        if result_text == "写后复核中":
            return "post_read_pending"
        if result_text == "写入中":
            return "write_pending"
        return "recorded"

    def _session_change_counts(self) -> dict[str, int]:
        counts = {
            "total": len(self._session_change_history),
            "consistent": 0,
            "ack_only": 0,
            "unconfirmed": 0,
            "mismatch": 0,
            "unverifiable": 0,
            "failure": 0,
        }
        for entry in self._session_change_history:
            verification_status = entry.verification_status or self._verification_status_from_entry(entry)
            if verification_status == VERIFICATION_STATUS_ACK_ONLY:
                counts["ack_only"] += 1
            elif verification_status == VERIFICATION_STATUS_ACK_UNCONFIRMED:
                counts["unconfirmed"] += 1
                counts["failure"] += 1
            elif entry.result_text == "一致":
                counts["consistent"] += 1
            elif entry.result_text == "不一致":
                counts["mismatch"] += 1
            elif entry.result_text == "无法验证":
                counts["unverifiable"] += 1
            elif entry.result_text in {"写入失败", "写前读取失败"}:
                counts["failure"] += 1
        return counts

    def _refresh_session_change_summary(self) -> None:
        if not hasattr(self, "change_summary_message_label"):
            return
        counts = self._session_change_counts()
        if hasattr(self, "session_change_badge_button"):
            self.session_change_badge_button.setText(f"参数变更 {counts['total']} 条")
        for key, label in self._change_summary_value_labels.items():
            value = str(counts.get(key, 0))
            tone = "neutral"
            if key == "consistent" and counts[key] > 0:
                tone = "success"
            elif key == "ack_only" and counts[key] > 0:
                tone = "warning"
            elif key in {"mismatch", "unverifiable", "failure"} and counts[key] > 0:
                tone = "danger" if key in {"mismatch", "failure"} else "warning"
            elif key == "total" and counts[key] > 0:
                tone = "warning"
            self._set_status_label_tone(label, value, tone)
        if counts["total"] == 0:
            self._set_status_label_tone(self.change_summary_message_label, "本次会话暂无参数变更", "neutral")
        else:
            ack_only_hint = ""
            unconfirmed_hint = ""
            if counts["ack_only"] > 0:
                ack_only_hint = f" 其中 {counts['ack_only']} 条仅 ACK，未写后复核。"
            if counts["unconfirmed"] > 0:
                unconfirmed_hint = f" 其中 {counts['unconfirmed']} 条已发出但软件未取得 ACK 确认。"
            self._set_status_label_tone(
                self.change_summary_message_label,
                f"本次会话已有 {counts['total']} 条参数变更。{ack_only_hint}{unconfirmed_hint}可直接查看总览并继续导出诊断包。",
                "warning",
            )

    @staticmethod
    def _display_device_mode(mode_text: str, confirmed: bool) -> str:
        if not confirmed:
            return "待确认"
        normalized = str(mode_text or "").strip().upper()
        if normalized in {"MODE1", "1"}:
            return "MODE1"
        if normalized in {"MODE2", "2"}:
            return "MODE2"
        if normalized in {"MODE3", "3"}:
            return "工厂模式"
        return normalized or "待确认"

    def _new_device_task_status(self) -> tuple[str, str]:
        if not self.connected:
            return "未连接", "neutral"
        active_numeric_ids = sorted({device_id for device_id in self.active_online_device_ids if device_id.isdigit()})
        if len(active_numeric_ids) > 1:
            return "多个在线设备，需先收敛", "danger"
        if len(active_numeric_ids) == 1:
            online_id = active_numeric_ids[0]
            target_id = self._current_target_id()
            if target_id.isdigit() and target_id != online_id:
                return "target/online 不一致", "danger"
            return f"已识别唯一在线设备：{online_id}", "success"
        return "已连接，待识别设备", "warning"

    def _read_snapshot_task_status(self) -> tuple[str, str]:
        if self._last_manual_snapshot_status is None:
            return "尚未读取", "neutral"
        timestamp = self._fmt_ts(self._last_manual_snapshot_status.get("timestamp"))
        device_id = str(self._last_manual_snapshot_status.get("device_id") or "--")
        if self._last_manual_snapshot_status.get("ok"):
            return f"最近已读取：{timestamp} | 设备 {device_id}", "success"
        return f"最近读取失败：{timestamp}", "danger"

    def _address_mode_task_status(self) -> tuple[str, str]:
        mode_text = self._display_device_mode(self._device_output_mode, self._device_mode_confirmed)
        status = f"当前目标：{self._current_target_id()} | 当前模式：{mode_text}"
        tone = "neutral" if mode_text == "待确认" else "success"
        if self._last_address_mode_change is None:
            return status, tone
        change_name = str(self._last_address_mode_change.get("command_name") or "参数")
        result_text = str(self._last_address_mode_change.get("result_text") or "--")
        if result_text in {"写入失败", "写前读取失败", "不一致"}:
            tone = "danger"
        elif result_text == "无法验证":
            tone = "warning"
        else:
            tone = "success"
        return f"{status} | 最近设置：{change_name} / {result_text}", tone

    def _coeff_review_task_status(self) -> tuple[str, str]:
        if self._last_coeff_review_status is None:
            return "尚未执行", "neutral"
        result_text = str(self._last_coeff_review_status.get("result_text") or "--")
        mapping = {
            "一致": ("最近一次复核一致", "success"),
            "不一致": ("最近一次复核不一致", "danger"),
            "无法验证": ("最近一次无法验证", "warning"),
            "写前读取失败": ("最近一次写前读取失败", "danger"),
            "写入失败": ("最近一次写入失败", "danger"),
            "未收到 ACK": ("最近一次未收到 ACK", "warning"),
            "连接断开，未收到 ACK": ("最近一次连接断开且未收到 ACK", "warning"),
        }
        return mapping.get(result_text, (f"最近一次结果：{result_text}", "warning"))

    def _export_diag_task_status(self) -> tuple[str, str]:
        counts = self._session_change_counts()
        if counts["total"] == 0:
            return "本次会话暂无参数变更", "neutral"
        if counts["unconfirmed"] > 0:
            return (
                f"本次会话已有 {counts['total']} 条参数变更待导出，其中 {counts['unconfirmed']} 条为 ACK 未确认",
                "warning",
            )
        if counts["ack_only"] > 0:
            return f"本次会话已有 {counts['total']} 条参数变更待导出，其中 {counts['ack_only']} 条为 ACK-only 未复核", "warning"
        return f"本次会话已有 {counts['total']} 条参数变更待导出", "warning"

    def _refresh_task_entry_states(self) -> None:
        if not hasattr(self, "_task_status_labels"):
            return
        status_builders = {
            "new_device": self._new_device_task_status,
            "read_snapshot": self._read_snapshot_task_status,
            "address_mode": self._address_mode_task_status,
            "coeff_review": self._coeff_review_task_status,
            "export_diag": self._export_diag_task_status,
        }
        for task_key, builder in status_builders.items():
            label = self._task_status_labels.get(task_key)
            if label is None:
                continue
            text, tone = builder()
            self._set_status_label_tone(label, text, tone)

    def _open_session_change_overview(self) -> None:
        self.pages.setCurrentIndex(self.export_tab_index)
        if hasattr(self, "change_overview_table"):
            if self.change_overview_table.rowCount() > 0 and self.change_overview_table.item(0, 0) is not None:
                self.change_overview_table.setCurrentCell(0, 0)
                self.change_overview_table.scrollToItem(self.change_overview_table.item(0, 0))
            else:
                self.change_overview_table.scrollToTop()
            self.change_overview_table.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_task_entry_feedback("已打开“数据导出与回放”，并聚焦到“本次参数变更总览”。")

    def _open_workflow_from_header(self) -> None:
        self.pages.setCurrentIndex(self.settings_tab_index)
        self.task_entry_box.setChecked(True)
        self.task_entry_box.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_task_entry_feedback("已打开“工作流”入口，可继续按场景跳转，但仍不会自动发送写命令。")

    def _show_session_header_details(self) -> None:
        detail_text = (
            f"写入状态：{self.hard_write_permission_label.text()}\n"
            f"在线设备：{self.hard_online_value_label.text()}\n"
            f"目标设备：{self.hard_target_value_label.text()}\n"
            f"生效发送对象：{self.hard_send_value_label.text()}\n"
            f"状态说明：{self.hard_reason_value_label.text()}"
        )
        QMessageBox.information(self, "会话状态详情", detail_text)

    def _open_task_new_device_check(self) -> None:
        self.pages.setCurrentIndex(self.settings_tab_index)
        self.safe_handshake_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_task_entry_feedback("已打开“连接与设备模式”。建议先连接设备，再执行安全握手确认唯一在线设备。")

    def _open_task_read_snapshot(self) -> None:
        self.pages.setCurrentIndex(self.control_tab_index)
        self.control_panel.read_page_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_task_entry_feedback("已打开“设备控制”。可先使用“读取本页默认配置/读取当前参数”锁定当前设备参数快照。")

    def _open_task_address_mode(self) -> None:
        self.pages.setCurrentIndex(self.control_tab_index)
        self.control_panel.select_command("ID")
        self._set_task_entry_feedback("已打开“设备控制”。可继续处理地址、模式和串口参数；不会自动发送写命令。")

    def _open_task_coeff_review(self) -> None:
        self.pages.setCurrentIndex(self.coeff_tab_index)
        self._set_selected_coefficient_command("SENCO1", expand_detail=False)
        self._set_task_entry_feedback("已打开“系数中心”。建议先读回当前系数，再执行写入与复核。")

    def _open_task_export_diag(self) -> None:
        self.pages.setCurrentIndex(self.export_tab_index)
        self.export_diag_page_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_task_entry_feedback("已打开“数据导出与回放”。可先查看本次参数变更总览，再导出诊断包。")

    def _set_control_common_feedback(self, text: str) -> None:
        if hasattr(self, "control_common_feedback_label"):
            self.control_common_feedback_label.setText(text)

    def _select_control_command_task(self, command_id: str) -> None:
        self.pages.setCurrentIndex(self.control_tab_index)
        self.control_panel.select_command(command_id)

    def _control_write_blockers(self, definition: CommandDefinition) -> list[str]:
        blockers: list[str] = []
        if not self.connected:
            blockers.append("当前未连接设备")
        if self._current_session_mode() != SESSION_MODE_ENGINEERING:
            blockers.append("当前未处于工程模式")
        if self.read_only_lock_check.isChecked():
            blockers.append("只读锁已开启")
        if not has_permission(self.permission_combo.currentText(), definition.required_permission):
            blockers.append(f"当前权限不足（需 {permission_label(definition.required_permission)}）")
        return blockers

    def _build_control_write_prepare_message(
        self,
        definition: CommandDefinition,
        *,
        prepared_text: str,
        followup_hint: str = "",
    ) -> str:
        blockers = self._control_write_blockers(definition)
        guidance = "发送前请再次核对目标设备、生效发送对象与写后复核信息。"
        if blockers:
            guidance = f"当前尚不满足发送条件：{'；'.join(blockers)}。请先处理后再决定是否执行。"
        if followup_hint:
            guidance = f"{guidance} {followup_hint}"
        return f"已定位到 {definition.command_id} 命令卡并预填{prepared_text}，尚未发送。{guidance}"

    def _prepare_control_write_action(
        self,
        command_id: str,
        values: dict[str, str],
        *,
        prepared_text: str,
        followup_hint: str = "",
        switch_to_control_page: bool = True,
    ) -> str:
        if switch_to_control_page:
            self.pages.setCurrentIndex(self.control_tab_index)
        definition = self.registry.get(command_id, self._current_profile_name())
        self.control_panel.prepare_command(command_id, values)
        message = self._build_control_write_prepare_message(
            definition,
            prepared_text=prepared_text,
            followup_hint=followup_hint,
        )
        self._set_control_common_feedback(message)
        self.control_panel.detail_widget.send_button.setFocus(Qt.FocusReason.OtherFocusReason)
        return message

    def _default_monitoring_send_target_text(self, command_id: str) -> str:
        send_target = self.registry.default_target_for_command(command_id, self._current_target_id())
        if send_target == "FFF":
            return "FFF 广播"
        return f"设备 {send_target}"

    def _default_monitoring_risk_hint(self) -> str:
        send_target = self.registry.default_target_for_command("MODE", self._current_target_id())
        session_target = self._current_target_id()
        if send_target == "FFF":
            target_hint = "当前步骤会使用 FFF 广播，可能影响总线上所有设备。"
            if session_target.isdigit():
                target_hint += f" 对象一致性仍按当前会话目标设备 {session_target} 与在线设备校验。"
            return target_hint
        return f"当前步骤仅会发送到设备 {send_target}，不会悄悄扩散到其他设备。"

    def _default_monitoring_mode2_risk_hint(self) -> str:
        return (
            f"{self._default_monitoring_risk_hint()} "
            "第 1 步会切换到 MODE2 校准模式，请确认现场工况允许。"
        )

    def _default_monitoring_auto_upload_hint(self) -> str:
        return (
            f"{self._default_monitoring_risk_hint()} "
            "第 2 步仅开启主动上传，不修改 FTD 上传频率；"
            f"当前设备 FTD 写入频率为 {self.device_ftd_hz_edit.value()} Hz；"
            f"当前期望接收频率为 {self.stream_hz_edit.value()} Hz，仅用于监测统计/丢帧判断，不会写入设备。"
        )

    def _default_monitoring_target_summary(self, command_id: str, step_key: str, base_summary: str) -> str:
        normalized_summary = base_summary or "--"
        target_text = self._default_monitoring_send_target_text(command_id)
        if step_key == "mode2":
            summary = normalized_summary if normalized_summary != "--" else "MODE2 校准模式"
        elif step_key == "auto_upload_on":
            summary = normalized_summary if normalized_summary != "--" else "主动上传开启（不修改 FTD 频率）"
        else:
            summary = normalized_summary
        return f"{target_text} | {summary}"

    def _capture_initialization_checklist_label(self) -> str:
        return "初始化采集清单（待确认）"

    def _capture_initialization_source_page(self) -> str:
        return "初始化采集清单"

    def _capture_initialization_ftd_copy(self) -> str:
        return (
            f"本步骤写入设备 FTD 频率：{self.device_ftd_hz_edit.value()} Hz。"
            f" 当前期望接收频率：{self.stream_hz_edit.value()} Hz，仅用于监测统计/丢帧判断，不会写入设备。"
        )

    def _capture_initialization_command_summary(self, command_id: str, values: dict[str, str]) -> str:
        target_id = self.registry.default_target_for_command(command_id, self._current_target_id())
        payload = self.registry.build_preview(
            command_id,
            target_id,
            values,
            profile_name=self._current_profile_name(),
        )
        target_text = self._default_monitoring_send_target_text(command_id)
        if str(command_id or "").upper() == "FTD":
            return f"{target_text} | {self._capture_initialization_ftd_copy()} | payload={payload}"
        return f"{target_text} | payload={payload}"

    def _build_capture_initialization_steps(self) -> list[DefaultMonitoringChecklistStep]:
        flow_label = self._capture_initialization_checklist_label()
        source_page = self._capture_initialization_source_page()
        mode_preference = self._current_parse_mode()
        acquisition_mode = self._current_acquisition_mode()
        if acquisition_mode == "LISTEN":
            ftd_limit = self._capture_initialization_ftd_max_hz()
            requested_hz = max(1, self.device_ftd_hz_edit.value())
            if requested_hz > ftd_limit:
                self._default_monitoring_prepare_notice = (
                    f"设备 FTD 主动上传频率最大支持 {ftd_limit} Hz，当前设置为 {requested_hz} Hz，"
                    f"请调整到 1-{ftd_limit} Hz 后再准备初始化采集清单。"
                )
                return []
        step_specs: list[tuple[str, str, str, dict[str, str], str]] = []
        if mode_preference == "MODE1":
            step_specs.append(
                (
                    "init_mode1",
                    "切换到 MODE1 输出模式",
                    "MODE",
                    {"mode": "1"},
                    "初始化采集只会预填，不会自动发送。若当前目标为 FFF 广播，发送前仍需完成广播风险确认。",
                )
            )
        elif mode_preference == "MODE2":
            step_specs.append(
                (
                    "init_mode2",
                    "切换到 MODE2 校准模式",
                    "MODE",
                    {"mode": "2"},
                    f"初始化采集只会预填，不会自动发送。{self._default_monitoring_mode2_risk_hint()}",
                )
            )
        if acquisition_mode == "LISTEN":
            step_specs.append(
                (
                    "init_ftd",
                    "写入主动上报频率 FTD",
                    "FTD",
                    {"hz": str(max(1, self.device_ftd_hz_edit.value()))},
                    f"该步骤沿用原初始化采集中的 FTD 写入，但改为待确认。 {self._capture_initialization_ftd_copy()}"
                    " 若发送目标为 FFF 广播，请先确认不会影响其他在线设备。",
                )
            )
            step_specs.append(
                (
                    "auto_upload_on",
                    "开启主动上传",
                    "SETCOMWAY",
                    {"mode": "1"},
                    "该步骤只会预填到统一命令卡，不会自动发送。广播写入仍需高风险确认，并继续走现有安全写入链。",
                )
            )
        else:
            step_specs.append(
                (
                    "auto_upload_off",
                    "关闭主动上传",
                    "SETCOMWAY",
                    {"mode": "0"},
                    "当前为手动读取初始化；该步骤仅预填关闭主动上传命令，不会自动发送。"
                    " 后续请通过 READDATA 或手动读取获取数据。",
                )
            )

        steps: list[DefaultMonitoringChecklistStep] = []
        for index, (step_key, title, command_id, values, note_text) in enumerate(step_specs, start=1):
            target_summary = self._capture_initialization_command_summary(command_id, values)
            planned_payload = self.registry.build_preview(
                command_id,
                self.registry.default_target_for_command(command_id, self._current_target_id()),
                values,
                profile_name=self._current_profile_name(),
            )
            steps.append(
                self._build_checklist_step(
                    step_index=index,
                    step_key=step_key,
                    title=title,
                    command_id=command_id,
                    values=values,
                    target_summary=target_summary,
                    planned_payload=planned_payload,
                    note_text=note_text,
                    flow_label=flow_label,
                    source_page=source_page,
                    checklist_kind="capture_init",
                )
            )
        return steps

    def _capture_initialization_ftd_max_hz(self) -> int:
        try:
            definition = self.registry.get("FTD", self._current_profile_name())
        except Exception:
            return 20
        for parameter in definition.parameters:
            if parameter.key == "hz" and parameter.maximum is not None:
                return int(parameter.maximum)
        return 20

    def _default_monitoring_step_brief(self, step: DefaultMonitoringChecklistStep) -> str:
        detail_parts: list[str] = []
        if step.planned_payload:
            detail_parts.append(f"planned={step.planned_payload}")
        if step.expected_device:
            detail_parts.append(f"expected_device={step.expected_device}")
        if step.verification_policy == "ack_only":
            detail_parts.append("ACK-only")
        elif step.verification_policy == "readback":
            detail_parts.append("写前/写后复核")
        if step.last_message:
            detail_parts.append(step.last_message)
        return " | ".join(detail_parts) if detail_parts else "尚未发送"

    def _default_monitoring_completion_summary(self) -> str:
        messages: list[str] = []
        for step in self._default_monitoring_steps:
            if step.status != CHECKLIST_STATUS_DONE:
                continue
            if step.checklist_kind == "default_monitoring" and step.step_key == "mode2":
                messages.append("MODE2 校准模式已写后复核一致。")
            elif step.checklist_kind == "default_monitoring" and step.step_key == "auto_upload_on":
                messages.append("自动上传为 ACK-only 未复核；已收到 ACK，未修改 FTD 频率。")
            elif step.checklist_kind == "capture_init" and step.step_key == "init_mode2":
                messages.append("MODE 设置已完成并写后复核。")
            elif step.checklist_kind == "capture_init" and step.step_key == "init_ftd":
                messages.append("FTD 频率写入已完成并写后复核。")
            elif step.checklist_kind == "capture_init" and step.step_key == "auto_upload_on":
                messages.append("初始化采集的自动上传开启已收到 ACK；当前为 ACK-only 未复核。")
            else:
                messages.append(f"{step.title} 已完成。")
        return " ".join(messages) or "清单步骤已完成。"

    @staticmethod
    def _default_monitoring_status_colors(status: str) -> tuple[QColor, QColor]:
        mapping = {
            CHECKLIST_STATUS_PENDING: (QColor("#495057"), QColor("#f1f3f5")),
            CHECKLIST_STATUS_CURRENT: (QColor("#0b7285"), QColor("#e3fafc")),
            CHECKLIST_STATUS_RUNNING: (QColor("#e67700"), QColor("#fff4e6")),
            CHECKLIST_STATUS_DONE: (QColor("#2b8a3e"), QColor("#ebfbee")),
            CHECKLIST_STATUS_FAILED: (QColor("#c92a2a"), QColor("#fff5f5")),
            CHECKLIST_STATUS_CANCELLED: (QColor("#5c6770"), QColor("#f8f9fa")),
        }
        return mapping.get(status, (QColor("#343a40"), QColor("#ffffff")))

    def _build_default_monitoring_steps(self) -> list[DefaultMonitoringChecklistStep]:
        step_specs = [
            (
                "mode2",
                "切换到 MODE2 校准模式",
                "MODE",
                {"mode": "2"},
                (
                    "根据当前 registry 定义，MODE=2 表示校准模式。"
                    f"{self._default_monitoring_mode2_risk_hint()} 本清单只做待确认准备，不会自动发送。"
                ),
            ),
            (
                "auto_upload_on",
                "开启主动上传",
                "SETCOMWAY",
                {"mode": "1"},
                self._default_monitoring_auto_upload_hint(),
            ),
        ]
        steps: list[DefaultMonitoringChecklistStep] = []
        for index, (step_key, title, command_id, values, note_text) in enumerate(step_specs, start=1):
            snapshot = self.registry.build_target_snapshot(
                command_id,
                values,
                profile_name=self._current_profile_name(),
            )
            target_summary = self._default_monitoring_target_summary(command_id, step_key, snapshot.summary or "--")
            planned_payload = self.registry.build_preview(
                command_id,
                self.registry.default_target_for_command(command_id, self._current_target_id()),
                values,
                profile_name=self._current_profile_name(),
            )
            steps.append(
                self._build_checklist_step(
                    step_index=index,
                    step_key=step_key,
                    title=title,
                    command_id=command_id,
                    values=values,
                    target_summary=target_summary,
                    planned_payload=planned_payload,
                    note_text=note_text,
                    flow_label=DEFAULT_MONITORING_CHECKLIST_LABEL,
                    source_page=DEFAULT_MONITORING_SOURCE_PAGE,
                    checklist_kind="default_monitoring",
                )
            )
        return steps

    def _current_default_monitoring_step(self) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        for index, step in enumerate(self._default_monitoring_steps):
            if step.status == CHECKLIST_STATUS_CURRENT:
                return index, step
        return None

    def _running_default_monitoring_step(self) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        for index, step in enumerate(self._default_monitoring_steps):
            if step.status == CHECKLIST_STATUS_RUNNING:
                return index, step
        return None

    def _failed_default_monitoring_step(self) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        for index, step in enumerate(self._default_monitoring_steps):
            if step.status == CHECKLIST_STATUS_FAILED:
                return index, step
        return None

    def _pending_default_monitoring_step(self) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        for index, step in enumerate(self._default_monitoring_steps):
            if step.status == CHECKLIST_STATUS_PENDING:
                return index, step
        return None

    def _actionable_default_monitoring_step(self) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        return (
            self._current_default_monitoring_step()
            or self._failed_default_monitoring_step()
            or self._pending_default_monitoring_step()
        )

    def _find_default_monitoring_step(
        self,
        *,
        command_id: str = "",
        run_id: int | None = None,
        checklist_kind: str = "",
        step_id: str = "",
        planned_payload: str = "",
        pending_payload: str = "",
        verification_policy: str = "",
    ) -> tuple[int, DefaultMonitoringChecklistStep] | None:
        normalized_command = str(command_id or "").upper()
        normalized_kind = str(checklist_kind or "").strip()
        normalized_step_id = str(step_id or "").strip()
        normalized_planned_payload = str(planned_payload or "").strip()
        normalized_pending_payload = str(pending_payload or "").strip()
        normalized_policy = str(verification_policy or "").strip()
        for index, step in enumerate(self._default_monitoring_steps):
            if normalized_command and step.linked_command_id != normalized_command:
                continue
            if run_id is not None and step.run_id != run_id:
                continue
            if normalized_kind and step.checklist_kind != normalized_kind:
                continue
            if normalized_step_id and step.step_id != normalized_step_id:
                continue
            if normalized_planned_payload and step.planned_payload != normalized_planned_payload:
                continue
            if normalized_pending_payload and step.pending_payload != normalized_pending_payload:
                continue
            if normalized_policy and step.verification_policy != normalized_policy:
                continue
            return index, step
        return None

    def _default_monitoring_checklist_running(self) -> bool:
        return self._running_default_monitoring_step() is not None

    def _default_monitoring_checklist_completed(self) -> bool:
        return bool(self._default_monitoring_steps) and all(
            step.status == CHECKLIST_STATUS_DONE for step in self._default_monitoring_steps
        )

    def _checklist_intro_note(self, checklist_kind: str | None = None) -> str:
        kind = checklist_kind or self._current_prepared_checklist_kind()
        if kind == "capture_init":
            if self._current_acquisition_mode() == "LISTEN":
                parse_mode = self._current_parse_mode()
                mode_hint = (
                    " 若显式选择 MODE1 或 MODE2，会先预填对应模式切换；AUTO 模式不会默认强行切换 MODE。"
                    if parse_mode == "AUTO"
                    else f" 当前已选择 {parse_mode}，清单会先预填对应模式切换。"
                )
                return (
                    "本清单仅会预填到统一工作区，请逐步确认后再发送。"
                    f"{mode_hint}"
                    " 自动上传场景下，会继续准备 FTD 频率写入与 SETCOMWAY=1 主动上传开启。"
                    " 目标若为 FFF，将明确作为广播显示；不会自动直发。"
                )
            return (
                "本清单仅会预填到统一工作区，请逐步确认后再发送。"
                " 当前为手动读取初始化：若显式选择 MODE1 或 MODE2，会先准备对应 MODE 设置；随后预填 SETCOMWAY=0 关闭主动上传。"
                " 后续请通过 READDATA 或手动读取获取数据；不会自动直发。"
            )
        return (
            "本清单仅会预填到统一工作区，请逐项确认发送。"
            " 第 1 步会切换到 MODE2 校准模式，第 2 步仅开启主动上传且不修改 FTD 频率。"
        )

    def _checklist_empty_action_hint(self, checklist_label: str, checklist_kind: str | None = None) -> str:
        kind = checklist_kind or self._current_prepared_checklist_kind()
        if kind == "capture_init":
            return f"当前无待确认清单。请先准备{checklist_label}；后续仍需逐步确认，且不会自动发送。"
        return f"当前无待确认清单。请先准备{checklist_label}；后续仍需逐项确认，且不会自动发送。"

    def _auto_silence_transparency_text(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        preview: str,
        target_id: str,
    ) -> str:
        envelope = YGasProtocol.parse_command(preview)
        allow_broadcast = self._allow_broadcast_silence(values)
        pre_silence_text = AutoSilencePolicy.describe_pre_silence(
            definition.command_id,
            target_id,
            values,
            envelope=envelope,
            active_device_ids=self.active_online_device_ids,
            expected_device_id=self._current_target_id(),
            allow_broadcast=allow_broadcast,
        )
        self_silence_text = AutoSilencePolicy.describe_self_silence(
            definition.command_id,
            target_id,
            values,
            envelope=envelope,
        )
        return pre_silence_text or self_silence_text

    @staticmethod
    def _allow_broadcast_silence(values: dict[str, object] | None = None) -> bool:
        value_map = {str(key): str(value) for key, value in dict(values or {}).items()}
        return str(value_map.get("allow_broadcast_silence", "")).strip().lower() in {"1", "true", "yes", "on"}

    def _refresh_default_monitoring_checklist(self) -> None:
        if not hasattr(self, "default_monitoring_checklist_table"):
            return
        checklist_label = self._current_prepared_checklist_label()
        checklist_kind = self._current_prepared_checklist_kind()
        self.checklist_name_value_label.setText(checklist_label)
        self.checklist_target_value_label.setText(self._current_checklist_target_text())
        self.checklist_scope_value_label.setText(self._current_checklist_scope_text())
        self.checklist_progress_value_label.setText(self._current_step_progress_text())
        overall_status = self._overall_checklist_status_text()
        self._set_status_label_tone(
            self.checklist_overall_status_label,
            overall_status,
            "success" if overall_status == "已完成" else ("danger" if overall_status == "已失败" else ("warning" if overall_status == "执行中" else "neutral")),
        )
        self.default_monitoring_checklist_table.setRowCount(len(self._default_monitoring_steps))
        has_steps = bool(self._default_monitoring_steps)
        if hasattr(self, "default_monitoring_empty_state_row"):
            self.default_monitoring_empty_state_row.setVisible(not has_steps)
        self.default_monitoring_checklist_table.setVisible(has_steps)
        for row, step in enumerate(self._default_monitoring_steps):
            values = [
                f"{step.step_index}",
                step.title,
                self._step_scope_text(step),
                step.status,
                self._step_evidence_text(step),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(self._step_detail_text(step))
                if column == 3:
                    foreground, background = self._default_monitoring_status_colors(step.status)
                    item.setForeground(QBrush(foreground))
                    item.setBackground(QBrush(background))
                self.default_monitoring_checklist_table.setItem(row, column, item)
        running = self._running_default_monitoring_step()
        current = self._current_default_monitoring_step()
        failed = self._failed_default_monitoring_step()
        if not self._default_monitoring_steps:
            self.default_monitoring_checklist_summary_label.setText(
                f"当前清单：{checklist_label}。尚未准备步骤；准备后会在这里逐步显示步骤，且不会自动发送。"
            )
            notice_text = self._default_monitoring_prepare_notice or self._checklist_intro_note(checklist_kind)
            self.default_monitoring_checklist_note_label.setText(f"当前步骤详情：{notice_text}")
            self.default_monitoring_checklist_action_label.setText(
                f"操作提示：{self._checklist_empty_action_hint(checklist_label, checklist_kind)}"
            )
            self.default_monitoring_continue_button.setEnabled(False)
            self.default_monitoring_restart_button.setEnabled(False)
            self.default_monitoring_cancel_button.setEnabled(False)
            return
        if running is not None:
            _, step = running
            self.default_monitoring_checklist_summary_label.setText(
                f"当前清单：{checklist_label}。当前步骤：第 {step.step_index} 步“{step.title}”。"
                " 已确认发送，正在等待 ACK 或写后复核结果。"
            )
            self.default_monitoring_checklist_note_label.setText(f"当前步骤详情：{self._step_detail_text(step)}")
            self.default_monitoring_checklist_action_label.setText(
                f"操作提示：当前步骤为第 {step.step_index} 步“{step.title}”，状态为执行中。"
                " 继续和重新准备已禁用；如需取消，只会取消本次清单，不会撤销设备已发出的命令。"
            )
            self.default_monitoring_continue_button.setEnabled(False)
            self.default_monitoring_restart_button.setEnabled(False)
            self.default_monitoring_cancel_button.setEnabled(True)
            return
        if current is not None:
            _, step = current
            self.default_monitoring_checklist_summary_label.setText(
                f"当前清单：{checklist_label}。已准备，共 {len(self._default_monitoring_steps)} 步。"
                f" 当前步骤：第 {step.step_index} 步“{step.title}”，尚未发送。"
            )
            self.default_monitoring_checklist_note_label.setText(f"当前步骤详情：{self._step_detail_text(step)}")
            next_step = self._pending_default_monitoring_step()
            next_hint = (
                f"下一步为第 {next_step[1].step_index} 步“{next_step[1].title}”。"
                if next_step
                else "当前已是最后一步。"
            )
            self.default_monitoring_checklist_action_label.setText(
                f"操作提示：当前步骤为第 {step.step_index} 步“{step.title}”。"
                f" {next_hint} 本步骤仅预填，请在命令卡中人工确认发送。"
            )
            self.default_monitoring_continue_button.setEnabled(True)
            self.default_monitoring_restart_button.setEnabled(True)
            self.default_monitoring_cancel_button.setEnabled(True)
            return
        if failed is not None:
            _, step = failed
            self.default_monitoring_checklist_summary_label.setText(
                f"当前清单：{checklist_label}。当前步骤失败：第 {step.step_index} 步“{step.title}”。请先处理失败原因，再决定是否继续。"
            )
            self.default_monitoring_checklist_note_label.setText(f"当前步骤详情：{self._step_detail_text(step)}")
            self.default_monitoring_checklist_action_label.setText(
                f"操作提示：当前无可继续的下一步；请先回到第 {step.step_index} 步“{step.title}”重新确认。"
                " “继续当前步骤”只会预填，不会自动发送。"
            )
            self.default_monitoring_continue_button.setEnabled(True)
            self.default_monitoring_restart_button.setEnabled(True)
            self.default_monitoring_cancel_button.setEnabled(True)
            return
        completed = sum(1 for step in self._default_monitoring_steps if step.status == CHECKLIST_STATUS_DONE)
        self.default_monitoring_checklist_summary_label.setText(
            f"当前清单：{checklist_label}。完成摘要：已完成，共 {completed}/{len(self._default_monitoring_steps)} 步。"
        )
        self.default_monitoring_checklist_note_label.setText(
            f"当前步骤详情：{self._default_monitoring_completion_summary()}"
        )
        self.default_monitoring_checklist_action_label.setText(
            "操作提示：当前清单已完成，不再有待确认步骤；如需重新开始，请使用“重新准备清单”。"
        )
        self.default_monitoring_continue_button.setEnabled(False)
        self.default_monitoring_restart_button.setEnabled(True)
        self.default_monitoring_cancel_button.setEnabled(False)

    def _clear_default_monitoring_checklist_from_ui(self) -> None:
        self._cancel_default_monitoring_checklist()

    def _clear_default_monitoring_checklist(self, note: str = "") -> None:
        self._default_monitoring_steps = []
        self._default_monitoring_prepare_notice = ""
        self._reset_prepared_checklist_context()
        self._refresh_default_monitoring_checklist()
        if note:
            self._set_control_common_feedback(note)

    def _continue_default_monitoring_checklist_step(self) -> None:
        checklist_label = self._current_prepared_checklist_label()
        running = self._running_default_monitoring_step()
        if running is not None:
            _, step = running
            message = f"{checklist_label}第 {step.step_index} 步“{step.title}”已确认发送，正在等待 ACK 或写后复核结果；当前不能重复预填或重发。"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            return
        actionable = self._actionable_default_monitoring_step()
        if actionable is None:
            if self._default_monitoring_checklist_completed():
                message = f"{checklist_label}已完成；当前没有待确认步骤。如需重新开始，请重新准备清单。"
            else:
                message = f"当前没有活跃的{checklist_label}；请先准备，再逐项确认。"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            return
        step_index, step = actionable
        previous_status = step.status
        message = self._activate_default_monitoring_step(step_index, switch_to_control_page=True)
        if previous_status == CHECKLIST_STATUS_FAILED:
            status_message = (
                f"已重新回到失败步骤“{step.title}”，仅预填到统一工作区，尚未发送；请先确认失败原因。"
            )
        else:
            status_message = (
                f"已继续定位到第 {step.step_index} 步“{step.title}”，仅预填到统一工作区，尚未发送。"
            )
        self._set_control_common_feedback(f"{status_message} {message}")
        self._set_device_quick_status(f"{status_message} 不会自动发送。")

    def _restart_default_monitoring_checklist(self) -> None:
        checklist_label = self._current_prepared_checklist_label()
        if not self._default_monitoring_steps:
            message = f"当前没有可重新准备的{checklist_label}；请先准备对应清单。"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            return
        if self._default_monitoring_checklist_running():
            message = "当前步骤已确认发送，正在等待 ACK 或写后复核结果；为避免旧结果串入新清单，暂不允许重新准备。"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            return
        if self._current_prepared_checklist_kind() == "capture_init":
            message = self._prepare_capture_initialization_steps(switch_to_control_page=True, trigger_source="restart")
        else:
            message = self._prepare_default_monitoring_checklist(trigger_source="restart", switch_to_control_page=True)
        self._set_control_common_feedback(message)
        self._set_device_quick_status(message)

    def _cancel_default_monitoring_checklist(self) -> None:
        checklist_label = self._current_prepared_checklist_label()
        if not self._default_monitoring_steps:
            message = f"当前没有活跃的{checklist_label}可取消。"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            return
        if self._default_monitoring_checklist_running():
            message = (
                f"已取消本次{checklist_label}；当前命令可能已经发出，取消清单不会阻止设备响应，"
                "也不会撤销已执行写入。"
            )
        else:
            message = f"已取消本次{checklist_label}；已执行的设备写入不会被撤销。"
        self._clear_default_monitoring_checklist(message)
        self._set_device_quick_status(message)

    def _activate_default_monitoring_step(
        self,
        step_index: int,
        *,
        switch_to_control_page: bool,
    ) -> str:
        checklist_label = self._current_prepared_checklist_label()
        if step_index < 0 or step_index >= len(self._default_monitoring_steps):
            return f"{checklist_label}无可用步骤。"
        for index, step in enumerate(self._default_monitoring_steps):
            if step.status in {CHECKLIST_STATUS_DONE, CHECKLIST_STATUS_CANCELLED}:
                continue
            previous_status = step.status
            previous_message = step.last_message
            step.status = (
                CHECKLIST_STATUS_CURRENT
                if index == step_index
                else (CHECKLIST_STATUS_FAILED if previous_status == CHECKLIST_STATUS_FAILED else CHECKLIST_STATUS_PENDING)
            )
            if index == step_index:
                if previous_status == CHECKLIST_STATUS_FAILED and previous_message:
                    step.last_message = f"上次失败：{previous_message} 已重新预填到统一工作区，尚未发送。"
                elif previous_message:
                    step.last_message = f"{previous_message} 已预填到统一工作区，尚未发送。"
                else:
                    step.last_message = "已预填到统一工作区，尚未发送。"
                step.last_evidence_text = ""
                step.last_timeout_reason = ""
                step.pending_payload = ""
                step.pending_run_id = 0
            elif previous_status != CHECKLIST_STATUS_FAILED and not step.last_message:
                step.last_message = "待确认，尚未发送。"
            if index != step_index:
                step.pending_payload = ""
                step.pending_run_id = 0
        step = self._default_monitoring_steps[step_index]
        followup_hint = (
            f"这是{checklist_label}；当前步骤仅已预填，尚未发送，不会自动发送。"
        )
        if step.checklist_kind == "default_monitoring" and step.step_key == "mode2":
            followup_hint = f"{followup_hint} {self._default_monitoring_mode2_risk_hint()} 下一步会准备“开启主动上传”。"
        elif step.checklist_kind == "default_monitoring" and step.step_key == "auto_upload_on":
            followup_hint = f"{followup_hint} {self._default_monitoring_auto_upload_hint()}"
        elif step.checklist_kind == "capture_init" and step.step_key == "init_mode2":
            if self._current_acquisition_mode() == "LISTEN":
                followup_hint = (
                    f"{followup_hint} {self._default_monitoring_mode2_risk_hint()} "
                    "下一步会准备 FTD 频率写入，再准备开启主动上传。"
                )
            else:
                followup_hint = (
                    f"{followup_hint} {self._default_monitoring_mode2_risk_hint()} "
                    "下一步会准备关闭主动上传；后续通过 READDATA 或手动读取获取数据。"
                )
        elif step.checklist_kind == "capture_init" and step.step_key == "init_ftd":
            followup_hint = (
                f"{followup_hint} {self._capture_initialization_ftd_copy()}"
                " 下一步会准备开启主动上传。"
            )
        elif step.checklist_kind == "capture_init" and step.step_key == "auto_upload_off":
            followup_hint = (
                f"{followup_hint} 本步骤会关闭主动上传；后续请通过 READDATA 或手动读取获取数据。"
            )
        elif step.checklist_kind == "capture_init" and step.step_key == "auto_upload_on":
            followup_hint = (
                f"{followup_hint} 本步骤会开启主动上传；MODE 与 FTD 写入都需要逐步确认，仍不会自动串发。"
            )
        message = self._prepare_control_write_action(
            step.linked_command_id,
            step.prefill_values,
            prepared_text=f"{checklist_label}第 {step.step_index} 步：{step.title}",
            followup_hint=followup_hint,
            switch_to_control_page=switch_to_control_page,
        )
        self._set_control_common_feedback(
            f"{checklist_label} {step.step_index}/{len(self._default_monitoring_steps)}：{message}"
        )
        self._refresh_default_monitoring_checklist()
        return (
            f"已为当前设备准备{checklist_label}，共 {len(self._default_monitoring_steps)} 步；"
            f"当前停在第 {step.step_index} 步“{step.title}”，尚未发送，不会自动发送。"
        )

    def _prepare_default_monitoring_checklist(
        self,
        *,
        trigger_source: str,
        switch_to_control_page: bool,
    ) -> str:
        self._default_monitoring_prepare_notice = ""
        self._set_prepared_checklist_context(
            label=DEFAULT_MONITORING_CHECKLIST_LABEL,
            source_page=DEFAULT_MONITORING_SOURCE_PAGE,
            kind="default_monitoring",
        )
        self._default_monitoring_steps = self._build_default_monitoring_steps()
        self._default_monitoring_run_id += 1
        for step in self._default_monitoring_steps:
            step.run_id = self._default_monitoring_run_id
            step.pending_run_id = 0
        message = self._activate_default_monitoring_step(0, switch_to_control_page=switch_to_control_page)
        if trigger_source == "auto":
            return f"已为当前设备自动准备{self._current_prepared_checklist_label()}，请逐项确认发送。{message}"
        if trigger_source == "restart":
            return (
                f"已重新准备{self._current_prepared_checklist_label()}；本次将从第 1 步重新开始，仍需逐项确认，且不会自动发送。"
                f"{message}"
            )
        return message

    def _prepare_capture_initialization_steps(self, *, switch_to_control_page: bool, trigger_source: str = "manual") -> str:
        label = self._capture_initialization_checklist_label()
        source_page = self._capture_initialization_source_page()
        self._default_monitoring_prepare_notice = ""
        self._set_prepared_checklist_context(label=label, source_page=source_page, kind="capture_init")
        self._default_monitoring_steps = self._build_capture_initialization_steps()
        if not self._default_monitoring_steps:
            self._refresh_default_monitoring_checklist()
            return self._default_monitoring_prepare_notice or f"{label}无可用步骤。"
        self._default_monitoring_run_id += 1
        for step in self._default_monitoring_steps:
            step.run_id = self._default_monitoring_run_id
            step.pending_run_id = 0
        message = self._activate_default_monitoring_step(0, switch_to_control_page=switch_to_control_page)
        if trigger_source == "restart":
            return f"已重新准备{label}；本次将从第 1 步重新开始，仍需逐步确认，且不会自动发送。{message}"
        return (
            f"原“初始化采集”入口现已改为{label}。"
            "界面只会展示待执行命令与风险，不会自动发送任何写命令。"
            f"{message}"
        )

    def _mark_default_monitoring_step_requested(
        self,
        command_id: str,
        values: dict[str, str],
        preview: str,
    ) -> None:
        current = self._current_default_monitoring_step()
        if current is None:
            return
        _, step = current
        if step.linked_command_id != str(command_id or "").upper():
            return
        if step.planned_payload != preview:
            return
        step.status = CHECKLIST_STATUS_RUNNING
        step.pending_run_id = step.run_id
        step.pending_payload = preview
        definition = self.registry.get(step.linked_command_id, self._current_profile_name())
        envelope = YGasProtocol.parse_command(preview)
        silence_notice = self._auto_silence_transparency_text(
            definition,
            dict(step.prefill_values),
            preview,
            envelope.target_id if envelope is not None else "",
        )
        step.last_message = "已确认发送，正在等待 ACK 或写后复核结果；后续步骤不会自动发送。"
        step.last_evidence_text = "尚未拿到 ACK / 复核证据"
        step.last_timeout_reason = ""
        if silence_notice:
            step.last_message = f"{step.last_message} {silence_notice}"
        self._refresh_default_monitoring_checklist()
        checklist_label = step.flow_label or self._current_prepared_checklist_label()
        running_message = (
            f"{checklist_label}第 {step.step_index} 步“{step.title}”已确认发送，正在等待 ACK 或写后复核结果；"
            "后续步骤不会自动发送。"
        )
        if silence_notice:
            running_message = f"{running_message} {silence_notice}"
        self._set_control_common_feedback(running_message)
        self._set_device_quick_status(running_message)

    def _default_monitoring_duplicate_send_message(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        preview: str,
    ) -> str:
        running = self._running_default_monitoring_step()
        if running is None:
            return ""
        _, step = running
        if step.run_id != self._default_monitoring_run_id or step.pending_run_id != step.run_id:
            return ""
        if step.linked_command_id != str(definition.command_id or "").upper():
            return ""
        if step.pending_payload != preview:
            return ""
        return (
            "当前步骤已确认发送，正在等待 ACK 或写后复核结果，请勿重复发送。"
            f" 当前步骤：第 {step.step_index} 步“{step.title}”。"
        )

    def _default_monitoring_pending_ack_only_duplicate_message(self, preview: str) -> str:
        pending = self._pending_default_monitoring_ack_only_writes.get(preview)
        if pending is None:
            return ""
        return (
            "上一条相同 ACK-only 命令仍在等待 ACK，请等待结果返回后再发送。"
            f" 当前等待步骤：{pending.step_title}。"
        )

    def _running_checklist_context_for_payload(self, command_id: str, payload: str) -> dict[str, object]:
        running = self._running_default_monitoring_step()
        if running is None:
            return {}
        _, step = running
        normalized_command = str(command_id or "").upper()
        normalized_payload = str(payload or "").strip()
        if step.linked_command_id != normalized_command:
            return {}
        if normalized_payload and step.planned_payload != normalized_payload and step.pending_payload != normalized_payload:
            return {}
        return {
            "run_id": step.pending_run_id or step.run_id,
            "kind": step.checklist_kind,
            "step_id": step.step_id,
            "planned_payload": step.planned_payload,
            "source_page": step.source_page or self._current_prepared_checklist_source_page(),
            "verification_policy": step.verification_policy,
        }

    @staticmethod
    def _ack_unconfirmed_result_text(message: str) -> str:
        normalized = str(message or "").strip()
        if "断开" in normalized or "串口异常断开" in normalized:
            return "连接断开，未收到 ACK"
        if "未收到 ACK" in normalized or "超时" in normalized:
            return "未收到 ACK"
        return "写入失败"

    def _register_default_monitoring_ack_only_pending(
        self,
        definition: CommandDefinition,
        preview: str,
        effective_target: str,
        sent_at: datetime | None = None,
    ) -> None:
        if self.registry.supports_readback(definition.command_id):
            return
        running = self._running_default_monitoring_step()
        if running is None:
            return
        _, step = running
        if step.linked_command_id != str(definition.command_id or "").upper() or step.pending_payload != preview:
            return
        self._pending_default_monitoring_ack_only_writes[preview] = DefaultMonitoringAckPendingWrite(
            run_id=step.pending_run_id or step.run_id,
            command_id=step.linked_command_id,
            step_key=step.step_key,
            step_title=step.title,
            flow_label=step.flow_label or self._current_prepared_checklist_label(),
            target_summary=step.target_summary,
            target_device_id=str(effective_target or "--"),
            source_page=step.source_page or self._current_prepared_checklist_source_page(),
            payload=preview,
            sent_at=sent_at or datetime.now(),
            checklist_kind=step.checklist_kind,
            step_id=step.step_id,
            planned_payload=step.planned_payload,
            expected_device=step.expected_device,
            command_target_id=step.planned_target_id or str(effective_target or "").upper(),
            expected_device_id=step.expected_device,
            effective_scope=step.effective_scope or ("broadcast" if step.target_is_broadcast else "single"),
            verification_policy=step.verification_policy,
            target_is_broadcast=step.target_is_broadcast or str(effective_target or "").upper() == "FFF",
        )

    def _record_unconfirmed_default_monitoring_ack_only_pending(self, reason: str) -> int:
        pending_items = list(self._pending_default_monitoring_ack_only_writes.values())
        if not pending_items:
            return 0
        self._pending_default_monitoring_ack_only_writes.clear()
        recorded = 0
        for pending in pending_items:
            report = self._build_ack_only_report_from_pending(
                pending,
                CommandResult(
                    timestamp=datetime.now(),
                    command=pending.payload,
                    ok=False,
                    message=reason,
                    command_target_id=pending.command_target_id,
                    expected_device_id=pending.expected_device_id,
                    effective_scope=pending.effective_scope,
                ),
            )
            self._record_session_change(
                pending.command_id,
                report,
                timestamp=datetime.now(),
                target_device_id=pending.target_device_id,
                source_page_override=pending.source_page,
                checklist_run_id=pending.run_id,
                checklist_kind=pending.checklist_kind,
                checklist_step_id=pending.step_id,
                checklist_planned_payload=pending.planned_payload,
                command_result=CommandResult(
                    timestamp=datetime.now(),
                    command=pending.payload,
                    ok=False,
                    message=reason,
                    command_target_id=pending.command_target_id,
                    expected_device_id=pending.expected_device_id,
                    effective_scope=pending.effective_scope,
                ),
                command_payload=pending.payload,
                expected_device_id=pending.expected_device_id,
            )
            recorded += 1
        return recorded

    def _build_ack_only_report_from_pending(
        self,
        pending: DefaultMonitoringAckPendingWrite,
        result: CommandResult,
    ) -> WriteVerificationReport:
        target_snapshot = StructuredValueSnapshot(summary=pending.target_summary)
        command_target_id = pending.command_target_id or pending.target_device_id
        target_note = f"command_target={command_target_id or '--'}"
        if pending.target_is_broadcast:
            target_note += "（FFF 广播，可能影响总线上所有设备）"
        if pending.expected_device_id:
            target_note += f"；expected_device={pending.expected_device_id}"
        elif pending.target_is_broadcast:
            target_note += "；广播 ACK 归属未绑定到明确会话目标"
        response_device_note = ""
        if result.response_device_id:
            response_device_note = f"；response_device={result.response_device_id}"
        if result.ok:
            detail_text = (
                f"ACK-only / 未复核；payload={pending.payload}；{target_note}{response_device_note}；无读回复核，仅记录 ACK 成功。"
                " 如清单已取消，本记录仍保留追溯，不代表写后复核一致。"
            )
            detail_text = self._append_result_observation(detail_text, result)
            return WriteVerificationReport(
                before=StructuredValueSnapshot(summary="未读取"),
                target=target_snapshot,
                after=StructuredValueSnapshot(summary="未读回"),
                result_text="ACK 成功",
                detail_text=detail_text,
                verification_status=VERIFICATION_STATUS_ACK_ONLY,
                verified_at=result.timestamp,
                source_device_id=str(result.response_device_id or pending.target_device_id),
            )
        result_text = self._ack_unconfirmed_result_text(result.message)
        if result_text != "写入失败":
            telemetry_note = ""
            if result.observed_telemetry_count > 0:
                telemetry_note = f" 等待期间持续收到 {result.observed_telemetry_count} 条实时数据帧。"
            detail_text = (
                f"ACK 未确认；payload={pending.payload}；{target_note}{response_device_note}；"
                f"命令已发出或已登记为 ACK-only pending，但在{result.message}前未收到 ACK。"
                " 本记录不代表设备一定未执行，仅表示软件未取得确认。"
                f"{telemetry_note}"
            )
            detail_text = self._append_result_observation(detail_text, result)
            return WriteVerificationReport(
                before=StructuredValueSnapshot(summary="未读取"),
                target=target_snapshot,
                after=StructuredValueSnapshot(summary="未读回"),
                result_text=result_text,
                detail_text=detail_text,
                verification_status=VERIFICATION_STATUS_ACK_UNCONFIRMED,
                verified_at=result.timestamp,
                source_device_id=str(result.response_device_id or pending.target_device_id),
            )
        detail_text = (
            f"写入失败；payload={pending.payload}；{target_note}{response_device_note}；未收到可确认成功的 ACK。"
            f" 设备返回：{result.message}"
        )
        detail_text = self._append_result_observation(detail_text, result)
        return WriteVerificationReport(
            before=StructuredValueSnapshot(summary="未读取"),
            target=target_snapshot,
            after=StructuredValueSnapshot(summary="未读回"),
            result_text="写入失败",
            detail_text=detail_text,
            verification_status="write_failed",
            verified_at=result.timestamp,
            source_device_id=str(result.response_device_id or pending.target_device_id),
        )

    def _apply_default_monitoring_ack_only_result(
        self,
        pending: DefaultMonitoringAckPendingWrite,
        result: CommandResult,
    ) -> None:
        matched = self._find_default_monitoring_step(
            command_id=pending.command_id,
            run_id=pending.run_id,
            checklist_kind=pending.checklist_kind,
            step_id=pending.step_id,
            planned_payload=pending.planned_payload or pending.payload,
            verification_policy=pending.verification_policy,
        )
        if matched is None:
            return
        current_index, step = matched
        if step.pending_payload != pending.payload or step.pending_run_id != pending.run_id:
            return
        step.pending_payload = ""
        step.pending_run_id = 0
        checklist_label = step.flow_label or pending.flow_label or self._current_prepared_checklist_label()
        if result.ok:
            step.status = CHECKLIST_STATUS_DONE
            step.last_message = "ACK-only / 未复核"
            step.last_evidence_text = (
                f"ACK：{result.matched_response_line}"
                if result.matched_response_line
                else (f"ACK 设备：{result.response_device_id}" if result.response_device_id else "已收到 ACK")
            )
            step.last_timeout_reason = ""
            if current_index + 1 < len(self._default_monitoring_steps):
                message = self._activate_default_monitoring_step(current_index + 1, switch_to_control_page=False)
                self._set_device_quick_status(
                    f"{checklist_label}第 {step.step_index} 步已完成；已切换到下一步待确认。{message}"
                )
            else:
                self._refresh_default_monitoring_checklist()
                self._set_control_common_feedback(
                    f"{checklist_label}已全部完成。{self._default_monitoring_completion_summary()}"
                )
                self._set_device_quick_status(
                    f"{checklist_label}已全部完成。{self._default_monitoring_completion_summary()}"
                )
            return
        step.status = CHECKLIST_STATUS_FAILED
        failure_prefix = "未收到 ACK" if self._ack_unconfirmed_result_text(result.message) != "写入失败" else "发送失败"
        step.last_message = f"{failure_prefix}：{result.message}"
        step.last_evidence_text = f"失败证据：{result.message}"
        step.last_timeout_reason = result.timeout_reason or self._ack_unconfirmed_result_text(result.message)
        self._refresh_default_monitoring_checklist()
        self._set_control_common_feedback(
            f"{checklist_label}停在第 {step.step_index} 步：{step.title}。{step.last_message}"
        )
        self._set_device_quick_status(
            f"{checklist_label}第 {step.step_index} 步执行未完成，后续步骤未自动推进。{step.last_message}"
        )

    def _handle_default_monitoring_ack_only_result(self, result: CommandResult) -> bool:
        pending = self._pending_default_monitoring_ack_only_writes.pop(result.command, None)
        if pending is None:
            return False
        report = self._build_ack_only_report_from_pending(pending, result)
        self._record_session_change(
            pending.command_id,
            report,
            timestamp=result.timestamp,
            target_device_id=pending.target_device_id,
            source_page_override=pending.source_page,
            checklist_run_id=pending.run_id,
            checklist_kind=pending.checklist_kind,
            checklist_step_id=pending.step_id,
            checklist_planned_payload=pending.planned_payload,
            command_result=result,
            command_payload=pending.payload,
            expected_device_id=pending.expected_device_id,
        )
        self._apply_default_monitoring_ack_only_result(pending, result)
        if self._find_default_monitoring_step(
            command_id=pending.command_id,
            run_id=pending.run_id,
            checklist_kind=pending.checklist_kind,
            step_id=pending.step_id,
            planned_payload=pending.planned_payload or pending.payload,
            verification_policy=pending.verification_policy,
        ) is None:
            summary = "已写入参数变更追溯。"
            if not result.ok:
                if report.verification_status == VERIFICATION_STATUS_ACK_UNCONFIRMED:
                    summary = f"已写入未确认追溯：{result.message}"
                else:
                    summary = f"已写入失败追溯：{result.message}"
            self._set_control_common_feedback(
                f"{pending.flow_label or self._current_prepared_checklist_label()}已取消，但此前发出的 {pending.step_title} 结果已回写到参数变更历史。{summary}"
            )
            self._set_device_quick_status(
                f"{pending.flow_label or self._current_prepared_checklist_label()}已取消，但此前发出的 {pending.step_title} 结果仍已保留追溯。"
            )
        return True

    def _handle_default_monitoring_step_report(
        self,
        command_id: str,
        report: WriteVerificationReport,
        checklist_run_id: int | None = None,
        checklist_kind: str | None = None,
        checklist_step_id: str | None = None,
        checklist_planned_payload: str | None = None,
    ) -> None:
        if checklist_run_id is None:
            return
        matched = self._find_default_monitoring_step(
            command_id=str(command_id or "").upper(),
            run_id=checklist_run_id,
            checklist_kind=str(checklist_kind or "").strip(),
            step_id=str(checklist_step_id or "").strip(),
            planned_payload=str(checklist_planned_payload or "").strip(),
            verification_policy="readback",
        )
        if matched is None:
            matched = self._find_default_monitoring_step(
                command_id=str(command_id or "").upper(),
                run_id=checklist_run_id,
                checklist_kind=str(checklist_kind or "").strip(),
                verification_policy="readback",
            )
        if matched is None:
            return
        current_index, step = matched
        if checklist_run_id is not None and step.pending_run_id != checklist_run_id:
            return
        step.pending_payload = ""
        step.pending_run_id = 0
        checklist_label = step.flow_label or self._current_prepared_checklist_label()
        if report.result_text == "一致":
            step.status = CHECKLIST_STATUS_DONE
            step.last_message = report.detail_text or "已完成写前/写后复核。"
            if report.after.summary and report.after.summary != "--":
                step.last_evidence_text = f"读回一致：{report.after.summary}"
            elif report.target.summary and report.target.summary != "--":
                step.last_evidence_text = f"目标值已复核：{report.target.summary}"
            else:
                step.last_evidence_text = "写后复核一致"
            step.last_timeout_reason = ""
            if current_index + 1 < len(self._default_monitoring_steps):
                message = self._activate_default_monitoring_step(current_index + 1, switch_to_control_page=False)
                self._set_device_quick_status(
                    f"{checklist_label}第 {step.step_index} 步已完成；已切换到下一步待确认。{message}"
                )
            else:
                self._refresh_default_monitoring_checklist()
                completion = self._default_monitoring_completion_summary()
                self._set_control_common_feedback(f"{checklist_label}已全部完成。{completion}")
                self._set_device_quick_status(f"{checklist_label}已全部完成。{completion}")
            return
        step.status = CHECKLIST_STATUS_FAILED
        step.last_message = report.detail_text or report.result_text or "当前步骤执行失败。"
        step.last_evidence_text = f"失败证据：{report.result_text or '复核失败'}"
        step.last_timeout_reason = report.result_text if "超时" in str(report.result_text or "") else ""
        self._refresh_default_monitoring_checklist()
        self._set_control_common_feedback(
            f"{checklist_label}停在第 {step.step_index} 步：{step.title}。{step.last_message}"
        )
        self._set_device_quick_status(
            f"{checklist_label}第 {step.step_index} 步执行未完成，后续步骤未自动推进。{step.last_message}"
        )

    def _handle_default_monitoring_step_direct_result(self, result: CommandResult) -> None:
        running = self._running_default_monitoring_step()
        if running is None:
            return
        current_index, step = running
        if not step.pending_payload or result.command != step.pending_payload or step.pending_run_id != step.run_id:
            return
        if self.registry.supports_readback(step.linked_command_id):
            return
        step.pending_payload = ""
        pending_run_id = step.pending_run_id
        step.pending_run_id = 0
        checklist_label = step.flow_label or self._current_prepared_checklist_label()
        if result.ok:
            target_snapshot = StructuredValueSnapshot(summary=step.target_summary)
            command_target_id = result.command_target_id or step.planned_target_id or self._current_target_id()
            scope_note = "（FFF 广播，可能影响总线上所有设备）" if str(command_target_id).upper() == "FFF" else ""
            expected_note = (
                f"；expected_device={result.expected_device_id or step.expected_device}"
                if (result.expected_device_id or step.expected_device)
                else (
                    "；广播 ACK 归属未绑定到明确会话目标"
                    if str(command_target_id).upper() == "FFF"
                    else ""
                )
            )
            response_device_note = f"；response_device={result.response_device_id}" if result.response_device_id else ""
            ack_note = (
                f"ACK-only / 未复核；payload={result.command}；command_target={command_target_id}{scope_note}"
                f"{expected_note}{response_device_note}；"
                "无读回复核，仅记录 ACK 成功。"
            )
            report = WriteVerificationReport(
                before=StructuredValueSnapshot(summary="未读取"),
                target=target_snapshot,
                after=StructuredValueSnapshot(summary="未读回"),
                result_text="ACK 成功",
                detail_text=ack_note,
                verification_status=VERIFICATION_STATUS_ACK_ONLY,
                verified_at=result.timestamp,
                source_device_id=str(result.response_device_id or self._current_target_id() or "--"),
            )
            self._record_session_change(
                step.linked_command_id,
                report,
                timestamp=result.timestamp,
                target_device_id=str(result.response_device_id or self._current_target_id()),
                source_page_override=step.source_page or self._current_prepared_checklist_source_page(),
                checklist_run_id=pending_run_id,
                checklist_kind=step.checklist_kind,
                checklist_step_id=step.step_id,
                checklist_planned_payload=step.planned_payload,
                command_result=result,
                command_payload=step.planned_payload or result.command,
                expected_device_id=step.expected_device,
            )
            step.status = CHECKLIST_STATUS_DONE
            step.last_message = ack_note
            step.last_evidence_text = (
                f"ACK：{result.matched_response_line}"
                if result.matched_response_line
                else (f"ACK 设备：{result.response_device_id}" if result.response_device_id else "已收到 ACK")
            )
            step.last_timeout_reason = ""
            if current_index + 1 < len(self._default_monitoring_steps):
                message = self._activate_default_monitoring_step(current_index + 1, switch_to_control_page=False)
                self._set_device_quick_status(
                    f"{checklist_label}第 {step.step_index} 步已完成；已切换到下一步待确认。{message}"
                )
            else:
                self._refresh_default_monitoring_checklist()
                completion = self._default_monitoring_completion_summary()
                self._set_control_common_feedback(f"{checklist_label}已全部完成。{completion}")
                self._set_device_quick_status(f"{checklist_label}已全部完成。{completion}")
            return
        step.status = CHECKLIST_STATUS_FAILED
        step.last_message = f"发送失败：{result.message}"
        step.last_evidence_text = f"失败证据：{result.message}"
        step.last_timeout_reason = result.timeout_reason or ""
        self._refresh_default_monitoring_checklist()
        self._set_control_common_feedback(
            f"{checklist_label}停在第 {step.step_index} 步：{step.title}。{step.last_message}"
        )
        self._set_device_quick_status(
            f"{checklist_label}第 {step.step_index} 步执行未完成，后续步骤未自动推进。{step.last_message}"
        )

    def _prefill_common_control_write_action(
        self,
        command_id: str,
        values: dict[str, str],
        *,
        prepared_text: str,
        followup_hint: str = "",
    ) -> None:
        self._prepare_control_write_action(
            command_id,
            values,
            prepared_text=prepared_text,
            followup_hint=followup_hint,
        )

    def _read_device_address_from_common_actions(self) -> None:
        self._select_control_command_task("ID")
        detail = self.control_panel.detail_widget
        if detail.readback_button.isVisible() and detail.readback_button.isEnabled():
            detail.readback_button.click()
            self._set_control_common_feedback("已发起设备地址读取；结果会同步回填到命令卡与读回区。")
        else:
            detail.readback_button.setFocus(Qt.FocusReason.OtherFocusReason)
            self._set_control_common_feedback("已定位到设备地址读回入口；当前会话若限制读回，请先完成连接与安全握手。")

    def _open_device_address_change_task(self) -> None:
        self._select_control_command_task("ID")
        detail = self.control_panel.detail_widget
        field = getattr(detail, "_fields", {}).get("new_id")
        if field is not None:
            field.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_control_common_feedback("已定位到“修改设备地址”。请先确认唯一在线设备与目标 ID，再执行写入。")

    def _read_mode_from_common_actions(self) -> None:
        self._select_control_command_task("MODE")
        detail = self.control_panel.detail_widget
        if detail.readback_button.isVisible() and detail.readback_button.isEnabled():
            detail.readback_button.click()
            self._set_control_common_feedback("已发起当前模式读取；可直接据此判断是否需要切换 MODE1 / MODE2。")
        else:
            detail.readback_button.setFocus(Qt.FocusReason.OtherFocusReason)
            self._set_control_common_feedback("已定位到模式读回入口；当前会话若限制读回，请先完成连接与安全握手。")

    def _request_common_output_mode(self, mode_name: str) -> None:
        mode_value = "1" if mode_name == "MODE1" else "2"
        self._prefill_common_control_write_action(
            "MODE",
            {"mode": mode_value},
            prepared_text=f"目标值 {mode_name}",
        )

    def _request_common_auto_upload(self, enable_auto_upload: bool) -> None:
        action_text = "开启主动上传" if enable_auto_upload else "关闭主动上传"
        mode_value = "1" if enable_auto_upload else "0"
        followup_hint = ""
        if enable_auto_upload:
            followup_hint = "如需同步调整主动上报频率，请一并核对 FTD 参数。"
        self._prefill_common_control_write_action(
            "SETCOMWAY",
            {"mode": mode_value},
            prepared_text=f"“{action_text}”",
            followup_hint=followup_hint,
        )

    def _read_control_snapshot_from_common_actions(self) -> None:
        self.pages.setCurrentIndex(self.control_tab_index)
        if not self.connected:
            self._set_control_common_feedback("请先连接设备，再读取关键配置快照。")
            self._read_panel_defaults(self.control_panel)
            return
        self._read_panel_defaults(self.control_panel)
        self._set_control_common_feedback("已读取当前关键配置快照，包括地址、模式、自动上传与相关参数。")

    @staticmethod
    def _coefficient_command_ids() -> list[str]:
        return [f"SENCO{index}" for index in range(1, 10)]

    def _ensure_coefficient_workspace_state(self) -> None:
        for command_id in self._coefficient_command_ids():
            if command_id in self._coefficient_workspace_state:
                continue
            digits = "".join(ch for ch in command_id if ch.isdigit()) or "1"
            self._coefficient_workspace_state[command_id] = {
                "item": f"系数组 {digits}",
                "current": "--",
                "target": "--",
                "after": "--",
                "status": "待读取",
                "note": "建议先读取当前值，再编辑目标值并执行写后复核。",
            }

    def _coefficient_row_for_command(self, command_id: str) -> int:
        try:
            return self._coefficient_command_ids().index(str(command_id or "").upper())
        except ValueError:
            return -1

    def _selected_coefficient_command_id(self) -> str:
        row = self.coeff_workspace_table.currentRow() if hasattr(self, "coeff_workspace_table") else -1
        command_ids = self._coefficient_command_ids()
        if 0 <= row < len(command_ids):
            return command_ids[row]
        current = self.coeff_panel.current_command().command_id if hasattr(self, "coeff_panel") else ""
        return current if current in command_ids else "SENCO1"

    def _set_selected_coefficient_command(self, command_id: str, *, expand_detail: bool) -> None:
        normalized = str(command_id or "").upper()
        row = self._coefficient_row_for_command(normalized)
        if hasattr(self, "pages") and hasattr(self, "coeff_tab_index"):
            self.pages.setCurrentIndex(self.coeff_tab_index)
        if row >= 0 and hasattr(self, "coeff_workspace_table"):
            self.coeff_workspace_table.blockSignals(True)
            self.coeff_workspace_table.setCurrentCell(row, 0)
            self.coeff_workspace_table.blockSignals(False)
        self.coeff_panel.select_command(normalized)
        if expand_detail and hasattr(self, "coeff_detail_box"):
            self.coeff_detail_box.setChecked(True)
        self._refresh_coefficient_workspace_table()

    def _handle_coefficient_workspace_selection_changed(self) -> None:
        command_id = self._selected_coefficient_command_id()
        if command_id:
            self.coeff_panel.select_command(command_id)
            self._refresh_coefficient_workspace_table()

    def _sync_coefficient_workspace_selection_from_panel(self) -> None:
        command_id = self.coeff_panel.current_command().command_id
        row = self._coefficient_row_for_command(command_id)
        if row < 0 or not hasattr(self, "coeff_workspace_table"):
            return
        self.coeff_workspace_table.blockSignals(True)
        self.coeff_workspace_table.setCurrentCell(row, 0)
        self.coeff_workspace_table.blockSignals(False)
        self._refresh_coefficient_workspace_table()

    def _set_coefficient_workspace_feedback(self, text: str) -> None:
        if hasattr(self, "coeff_workspace_feedback_label"):
            self.coeff_workspace_feedback_label.setText(text)

    def _refresh_coefficient_workspace_table(self) -> None:
        if not hasattr(self, "coeff_workspace_table"):
            return
        self._ensure_coefficient_workspace_state()
        current_command_id = self.coeff_panel.current_command().command_id if hasattr(self, "coeff_panel") else ""
        current_field = None
        if current_command_id in self._coefficient_workspace_state:
            current_field = getattr(self.coeff_panel.detail_widget, "_fields", {}).get("coefficients")
            if isinstance(current_field, QLineEdit):
                current_text = current_field.text().strip()
                if current_text:
                    self._coefficient_workspace_state[current_command_id]["target"] = current_text

        command_ids = self._coefficient_command_ids()
        self.coeff_workspace_table.setRowCount(len(command_ids))
        for row, command_id in enumerate(command_ids):
            state = self._coefficient_workspace_state[command_id]
            values = [
                state["item"],
                state["current"],
                state["target"],
                state["after"],
                state["status"],
                state["note"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.coeff_workspace_table.setItem(row, column, item)

    def _read_selected_coefficient_current(self) -> None:
        command_id = self._selected_coefficient_command_id()
        self._set_selected_coefficient_command(command_id, expand_detail=True)
        self._read_panel_defaults(self.coeff_panel)
        self._set_coefficient_workspace_feedback(f"已请求读取 {command_id} 当前值；结果会同步到表格与底层命令卡。")

    def _focus_selected_coefficient_editor(self) -> None:
        command_id = self._selected_coefficient_command_id()
        self._set_selected_coefficient_command(command_id, expand_detail=True)
        field = getattr(self.coeff_panel.detail_widget, "_fields", {}).get("coefficients")
        if field is not None:
            field.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_coefficient_workspace_feedback(f"已定位到 {command_id} 目标值编辑区；建议先确认当前值，再修改目标值。")

    def _focus_selected_coefficient_write_review(self) -> None:
        command_id = self._selected_coefficient_command_id()
        self._set_selected_coefficient_command(command_id, expand_detail=True)
        self.coeff_panel.detail_widget.send_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_coefficient_workspace_feedback(f"已定位到 {command_id} 写入与复核入口；写入后会沿用现有写后复核链路。")

    def _source_page_for_command_id(self, command_id: str) -> str:
        panel = self._panel_for_command_id(command_id)
        if panel is self.control_panel:
            return "设备控制"
        if panel is self.coeff_panel or str(command_id or "").upper().startswith("SENCO"):
            return "系数中心"
        if panel is self.signal_panel:
            return "信号与算法参数"
        return ""

    @staticmethod
    def _is_stream_start_action(result: CommandResult) -> bool:
        return str(result.action_type or "").strip() in {"auto_start_stream", "manual_stream_start"}

    def _stream_start_action_detail_text(self, result: CommandResult) -> str:
        label = str(result.action_label_zh or action_label_zh(result.action_type)).strip() or "启动实时流"
        prefix = "系统动作" if result.action_type == "auto_start_stream" else "监测页动作"
        parts = [f"{prefix}：{label}"]
        if result.source_page:
            parts.append(f"来源页面：{result.source_page}")
        if result.auto_upload_state:
            parts.append(f"自动上传状态：{self._auto_upload_state_text(result.auto_upload_state)}")
        parts.append(f"结果：{result.message or '--'}")
        return "；".join(part for part in parts if part)

    @staticmethod
    def _command_log_result_text(result: CommandResult) -> str:
        if result.action_type == "auto_silence":
            if result.ok:
                return "ACK 成功（未复核）"
            if "未收到 ACK" in str(result.message or "") or "超时" in str(result.message or ""):
                return "未收到 ACK"
            return "暂停主动上传失败"
        if result.action_type == "auto_silence_restore":
            if result.ok:
                return "ACK 成功（未复核）"
            if "未收到 ACK" in str(result.message or "") or "超时" in str(result.message or ""):
                return "未收到 ACK"
            return "恢复主动上传失败"
        if result.action_type == "keep_auto_upload_off":
            return "用户确认"
        if str(result.action_type or "").strip() in {"auto_start_stream", "manual_stream_start"}:
            if result.ok:
                return "ACK 成功（未复核）"
            if "未收到 ACK" in str(result.message or "") or "超时" in str(result.message or ""):
                return "未收到 ACK"
            return "启动实时流失败"
        return "成功" if result.ok else "失败"

    def _record_command_log_event(self, result: CommandResult, detail_text: str) -> None:
        resolved_detail_text = detail_text
        if result.action_type in {"auto_silence", "auto_silence_restore"}:
            resolved_detail_text = self._auto_upload_system_detail_text(result)
        elif self._is_stream_start_action(result):
            resolved_detail_text = self._stream_start_action_detail_text(result)
        elif self._has_auto_upload_metadata(result):
            resolved_detail_text = self._auto_upload_detail_text(detail_text, result)
        entry = SessionCommandLogEntry(
            timestamp=result.timestamp,
            payload=result.command,
            result=self._command_log_result_text(result),
            action_type=result.action_type or "command",
            action_label_zh=str(result.action_label_zh or action_label_zh(result.action_type)),
            parent_command=result.parent_command,
            command_target_id=result.command_target_id,
            expected_device_id=result.expected_device_id,
            response_device_id=str(result.response_device_id or ""),
            effective_scope=result.effective_scope,
            source_page=result.source_page or self._source_page_for_command_id(result.command.split(",", 1)[0]),
            detail_text=resolved_detail_text,
            level="INFO" if result.ok else "WARN",
            original_auto_upload_state=str(result.original_auto_upload_state or ""),
            restore_policy=str(result.restore_policy or ""),
            restore_attempted=bool(result.restore_attempted),
            restore_result=str(result.restore_result or ""),
        )
        self._command_log_entries.appendleft(entry)
        self._command_log_history.appendleft(entry)

    def _record_session_change(
        self,
        command_id: str,
        report: WriteVerificationReport,
        *,
        timestamp: datetime | None = None,
        target_device_id: str | None = None,
        source_page_override: str | None = None,
        checklist_run_id: int | None = None,
        checklist_kind: str | None = None,
        checklist_step_id: str | None = None,
        checklist_planned_payload: str | None = None,
        command_result: CommandResult | None = None,
        command_payload: str | None = None,
        expected_device_id: str | None = None,
        is_system_action_override: bool | None = None,
    ) -> None:
        command_name = command_id
        try:
            command_name = self.registry.get(command_id, self._current_profile_name()).display_name
        except Exception:
            pass
        payload_text = str(command_payload or checklist_planned_payload or getattr(command_result, "command", "") or "").strip()
        target_id = str(getattr(command_result, "command_target_id", "") or "").strip().upper()
        effective_scope = str(getattr(command_result, "effective_scope", "") or "").strip()
        if not target_id and payload_text:
            envelope = YGasProtocol.parse_command(payload_text)
            if envelope is not None:
                target_id = str(envelope.target_id or "").strip().upper()
                effective_scope = effective_scope or AutoSilencePolicy.effective_scope(target_id)
        resolved_expected_device_id = str(expected_device_id or getattr(command_result, "expected_device_id", "") or "").strip()
        if not resolved_expected_device_id and target_id.isdigit():
            resolved_expected_device_id = target_id
        resolved_response_device_id = str(
            getattr(command_result, "response_device_id", "") or report.source_device_id or target_device_id or ""
        ).strip()
        action_type = str(getattr(command_result, "action_type", "") or "command").strip()
        entry = SessionChangeEntry(
            timestamp=timestamp or report.verified_at or datetime.now(),
            command_name=command_name,
            target_device_id=str(target_device_id or report.source_device_id or self._current_target_id() or "--"),
            source_page=source_page_override or self._source_page_for_command_id(command_id),
            before_value=report.before.summary or "--",
            target_value=report.target.summary or "--",
            after_value=report.after.summary or "--",
            result_text=report.result_text or "--",
            detail_text=report.detail_text or "--",
            verification_status=report.verification_status or "",
            action_type=action_type,
            action_label_zh=str(getattr(command_result, "action_label_zh", "") or action_label_zh(action_type)),
            command_payload=payload_text,
            parent_command=str(getattr(command_result, "parent_command", "") or "").strip(),
            command_target_id=target_id,
            expected_device_id=resolved_expected_device_id,
            response_device_id=resolved_response_device_id,
            effective_scope=effective_scope,
            is_system_action=action_type != "command" if is_system_action_override is None else is_system_action_override,
            original_auto_upload_state=str(getattr(command_result, "original_auto_upload_state", "") or ""),
            restore_policy=str(getattr(command_result, "restore_policy", "") or ""),
            restore_attempted=bool(getattr(command_result, "restore_attempted", False)),
            restore_result=str(getattr(command_result, "restore_result", "") or ""),
        )
        change_state = {
            "command_id": str(command_id or "").upper(),
            "command_name": command_name,
            "result_text": entry.result_text,
            "timestamp": entry.timestamp,
        }
        if change_state["command_id"] in {"ID", "MODE"}:
            self._last_address_mode_change = change_state
        panel = self._panel_for_command_id(command_id)
        if panel is self.coeff_panel or str(command_id or "").upper().startswith("SENCO"):
            self._ensure_coefficient_workspace_state()
            self._last_coeff_review_status = change_state
            normalized_command = str(command_id or "").upper()
            if normalized_command in self._coefficient_workspace_state:
                coeff_state = self._coefficient_workspace_state[normalized_command]
                coeff_state["current"] = entry.before_value or coeff_state["current"]
                coeff_state["target"] = entry.target_value or coeff_state["target"]
                coeff_state["after"] = entry.after_value or coeff_state["after"]
                coeff_state["status"] = entry.result_text or coeff_state["status"]
                coeff_state["note"] = entry.detail_text or coeff_state["note"]
                self._refresh_coefficient_workspace_table()
        if self.registry.supports_readback(command_id):
            self._handle_default_monitoring_step_report(
                command_id,
                report,
                checklist_run_id=checklist_run_id,
                checklist_kind=checklist_kind,
                checklist_step_id=checklist_step_id,
                checklist_planned_payload=checklist_planned_payload,
            )
        self._session_change_history.appendleft(entry)
        self._session_change_entries.appendleft(entry)
        self._refresh_session_change_overview()

    def _auto_upload_system_detail_text(self, result: CommandResult) -> str:
        return self._auto_upload_detail_text(str(result.message or "--"), result)

    def _record_stream_start_change(self, result: CommandResult) -> None:
        if not self._is_stream_start_action(result):
            return
        action_name = str(result.action_label_zh or action_label_zh(result.action_type)).strip() or "启动实时流"
        report = WriteVerificationReport(
            before=StructuredValueSnapshot(summary="主动上传待启动"),
            target=StructuredValueSnapshot(summary="主动上传已开启"),
            after=StructuredValueSnapshot(summary="ACK 已收到，未读回确认" if result.ok else "--"),
            result_text="ACK 成功（未复核）" if result.ok else "写入失败",
            detail_text=self._stream_start_action_detail_text(result),
            verification_status="ack_only_unverified" if result.ok else "write_failed",
            verified_at=result.timestamp,
            source_device_id=str(result.response_device_id or result.expected_device_id or self._current_target_id() or "--"),
        )
        self._record_session_change(
            action_name,
            report,
            timestamp=result.timestamp,
            target_device_id=str(result.expected_device_id or result.response_device_id or self._current_target_id() or "--"),
            source_page_override=result.source_page or action_name,
            command_result=result,
            command_payload=result.command,
            expected_device_id=result.expected_device_id,
            is_system_action_override=result.action_type == "auto_start_stream",
        )

    def _record_auto_upload_system_change(self, result: CommandResult) -> None:
        if result.action_type == "auto_silence_restore":
            before_value = "主动上传已临时暂停"
            target_value = "主动上传已开启"
        else:
            before_value = (
                self._quiet_read_original_state_text(result.original_auto_upload_state)
                if result.original_auto_upload_state
                else "--"
            )
            target_value = "主动上传已临时暂停"
        report = WriteVerificationReport(
            before=StructuredValueSnapshot(summary=before_value),
            target=StructuredValueSnapshot(summary=target_value),
            after=StructuredValueSnapshot(summary="ACK 已收到，未读回确认" if result.ok else "--"),
            result_text="ACK 成功（未复核）" if result.ok else "写入失败",
            detail_text=self._auto_upload_system_detail_text(result),
            verification_status="ack_only_unverified" if result.ok else "write_failed",
            verified_at=result.timestamp,
            source_device_id=str(result.response_device_id or result.expected_device_id or self._current_target_id() or "--"),
        )
        self._record_session_change(
            "SETCOMWAY",
            report,
            timestamp=result.timestamp,
            target_device_id=str(result.expected_device_id or result.response_device_id or self._current_target_id() or "--"),
            source_page_override=result.source_page or (action_label_zh(result.action_type) or "命令执行"),
            command_result=result,
            command_payload=result.command,
            expected_device_id=result.expected_device_id,
        )

    def _refresh_session_change_overview(self) -> None:
        self.change_overview_table.setRowCount(len(self._session_change_entries))
        for row, entry in enumerate(self._session_change_entries):
            values = [
                self._fmt_ts(entry.timestamp),
                self._session_change_action_text(entry),
                entry.target_device_id,
                entry.before_value,
                entry.target_value,
                entry.after_value,
                entry.result_text,
                entry.detail_text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.change_overview_table.setItem(row, column, item)
        self._refresh_session_change_summary()
        self._refresh_task_entry_states()

    @staticmethod
    def _session_change_action_text(entry: SessionChangeEntry) -> str:
        label = str(entry.action_label_zh or action_label_zh(entry.action_type or "command")).strip()
        if entry.is_system_action:
            if entry.command_name and entry.command_name != label:
                return f"{label} / {entry.command_name}"
            return label or entry.command_name or "系统动作"
        return entry.command_name or label or "命令执行"

    def _apply_persisted_state(self, state: dict) -> None:
        if not state:
            self._sync_auto_start_stream_option(force_restore=True)
            self._refresh_session_safety_hint()
            return
        self.port_combo.setCurrentText(str(state.get("port", "SIMULATOR")))
        self.baud_combo.setCurrentText(str(state.get("baudrate", "115200")))
        self.bytesize_combo.setCurrentText(str(state.get("bytesize", "8")))
        self.parity_combo.setCurrentText(str(state.get("parity", "N")))
        self.stopbits_combo.setCurrentText(str(state.get("stopbits", "1")))
        self._auto_start_stream_listen_preference = bool(state.get("auto_start_stream_after_connect", True))
        self._set_acquisition_mode(str(state.get("acquisition_mode", "LISTEN")))
        self._set_parse_mode(str(state.get("mode_preference", "AUTO")))
        session_mode = state.get("session_mode")
        if not session_mode and state.get("listen_only", False):
            session_mode = SESSION_MODE_LISTEN_ONLY
        index = self.session_mode_combo.findData(session_mode or SESSION_MODE_MONITORING)
        self.session_mode_combo.setCurrentIndex(max(0, index))
        self.command_timeout_edit.setValue(self._safe_int(str(state.get("command_timeout_ms", "2000")), 2000))
        self.auto_reconnect_check.setChecked(bool(state.get("auto_reconnect", False)))
        self.auto_apply_default_config_check.setChecked(bool(state.get("auto_apply_default_monitoring", False)))
        self.read_only_lock_check.setChecked(bool(state.get("read_only_lock", False)))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(state.get("profile_name", "bench_default"))))
        self.device_ftd_hz_edit.setValue(self._safe_int(str(state.get("device_ftd_hz", state.get("stream_hz", "10"))), 10))
        self.stream_hz_edit.setValue(self._safe_int(str(state.get("stream_hz", "10")), 10))
        self.poll_interval_edit.setValue(self._safe_int(str(state.get("poll_interval_ms", "200")), 200))
        self.permission_combo.setCurrentText(str(state.get("permission_level", "READ_ONLY")))
        self.show_expert_check.setChecked(bool(state.get("show_expert_terminal", False)))
        self.target_combo.setCurrentText(str(state.get("target_id", "001")))
        self.session_note_edit.setText(str(state.get("session_note", "")))
        self.last_export_dir = str(state.get("last_export_dir") or EXPORT_DIR)
        self.last_replay_file = str(state.get("last_replay_file") or "")
        self._monitor_aux_last_height = max(120, int(state.get("monitor_aux_height", 140) or 140))
        self._set_monitor_aux_expanded(bool(state.get("monitor_aux_expanded", False)))
        self._restored_page_index = max(0, self._safe_int(str(state.get("current_page_index", 0) or 0), 0))
        self._sync_auto_start_stream_option(force_restore=True)
        self._refresh_session_safety_hint()

    def _refresh_ports(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and (now - self._last_port_refresh_ts) < 1.0:
            return
        self._last_port_refresh_ts = now
        self.refresh_ports_button.setEnabled(False)
        current = self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(list_serial_ports())
        self.port_combo.setCurrentText(current or "SIMULATOR")
        QTimer.singleShot(800, lambda: self.refresh_ports_button.setEnabled(True))
        self._update_status_strip()

    def _build_config(self) -> SessionConfig:
        session_mode = self._current_session_mode()
        return SessionConfig(
            serial=SerialSettings(
                port=self.port_combo.currentText().strip(),
                baudrate=int(self.baud_combo.currentText()),
                bytesize=int(self.bytesize_combo.currentText()),
                parity=self.parity_combo.currentText(),
                stopbits=float(self.stopbits_combo.currentText()),
                timeout=0.05,
            ),
            mode_preference=self._current_parse_mode(),
            acquisition_mode=self._current_acquisition_mode(),
            listen_only=session_mode == SESSION_MODE_LISTEN_ONLY,
            session_mode=session_mode,
            device_ftd_hz=max(1, self.device_ftd_hz_edit.value()),
            expected_receive_hz=max(1, self.stream_hz_edit.value()),
            stream_hz=max(1, self.stream_hz_edit.value()),
            poll_interval_ms=max(50, self.poll_interval_edit.value()),
            command_timeout_ms=max(300, self.command_timeout_edit.value()),
            auto_reconnect=self.auto_reconnect_check.isChecked(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            session_note=self.session_note_edit.text().strip(),
            profile_name=self._current_profile_name(),
            target_id=self._current_target_id(),
            permission_level=self.permission_combo.currentText(),
            session_name=self.session_name,
        )

    @staticmethod
    def _normalize_auto_upload_state(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"on", "off", "unknown", "temporarily_silenced"}:
            return normalized
        return "unknown"

    @staticmethod
    def _quiet_read_requested(operation_context: dict[str, object] | None) -> bool:
        return bool(isinstance(operation_context, dict) and operation_context.get("quiet_read_requested"))

    def _resolve_quiet_read_restore_policy(self, original_state: str) -> str:
        if original_state == "on":
            return "restore_after_read"
        if original_state == "off":
            return "keep_off"
        if original_state == "temporarily_silenced":
            return "preserve_existing_silence"
        buttons = (
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel
        )
        reply = QMessageBox.question(
            self,
            "确认读取后恢复策略",
            (
                "当前主动上传状态未确认。读取后是否恢复主动上传？\n\n"
                "选择“是”：读取后尝试发送 SETCOMWAY=1 恢复主动上传。\n"
                "选择“否”：读取后不自动恢复主动上传。\n"
                "选择“取消”：放弃本次暂停上传后读取。"
            ),
            buttons,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            return "user_confirmed_restore"
        if reply == QMessageBox.StandardButton.No:
            return "user_declined_restore"
        return ""

    def _quiet_read_restore_text(self, restore_policy: str) -> str:
        mapping = {
            "restore_after_read": "读取后将尝试发送 SETCOMWAY=1 恢复主动上传。",
            "keep_off": "读取后将保持主动上传关闭，不会发送 SETCOMWAY=1。",
            "preserve_existing_silence": "检测到当前主动上传已临时暂停；本次读取后不会重复发送 SETCOMWAY=1。",
            "user_confirmed_restore": "当前主动上传状态未确认；你已明确选择读取后恢复主动上传。",
            "user_declined_restore": "当前主动上传状态未确认；你已明确选择读取后不自动恢复主动上传。",
        }
        normalized = str(restore_policy or "").strip()
        if normalized in mapping:
            return mapping[normalized]
        if normalized:
            return f"读取后恢复策略：{restore_policy_label_zh(normalized)}。"
        return "读取后状态未确认，软件不会擅自恢复主动上传。"

    @staticmethod
    def _quiet_read_progress_text(operation_context: dict[str, object] | None) -> str:
        restore_policy = str((operation_context or {}).get("restore_policy") or "").strip()
        if restore_policy in {"restore_after_read", "user_confirmed_restore"}:
            return "正在临时暂停主动上传；随后将读取，并在读取完成后尝试恢复主动上传。"
        if restore_policy == "keep_off":
            return "正在临时暂停主动上传；随后将读取，读取完成后将保持主动上传关闭。"
        if restore_policy == "preserve_existing_silence":
            return "正在临时暂停主动上传；随后将读取，读取完成后将保持主动上传已临时暂停。"
        return "正在临时暂停主动上传；随后将读取。"

    def _quiet_read_context_for_payload(
        self,
        payload: str,
        operation_context: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        if not self._quiet_read_requested(operation_context):
            return None
        if self.read_only_lock_check.isChecked():
            QMessageBox.warning(self, "暂停上传后读取不可用", "只读锁已开启，禁止系统为读取自动发送 SETCOMWAY。")
            return None
        current_permission = self.permission_combo.currentText()
        if not has_permission(current_permission, "CONFIG"):
            QMessageBox.warning(
                self,
                "暂停上传后读取不可用",
                f"暂停上传后读取会临时写入 SETCOMWAY，至少需要 {permission_label('CONFIG')} 权限。",
            )
            return None
        if self._current_session_mode() != SESSION_MODE_ENGINEERING:
            QMessageBox.warning(self, "暂停上传后读取不可用", "暂停上传后读取仅允许在工程模式下执行。")
            return None
        envelope = YGasProtocol.parse_command(payload)
        if envelope is None:
            QMessageBox.warning(self, "暂停上传后读取不可用", "当前读取命令无法解析，已取消暂停主动上传。")
            return None
        active_ids = [device_id for device_id in self.active_online_device_ids if device_id.isdigit()]
        allow_broadcast = self._allow_broadcast_silence(operation_context)
        try:
            silence_target = AutoSilencePolicy.silence_target_id(
                target_id_policy=envelope.target_id,
                expected_device_id=envelope.target_id if envelope.target_id.isdigit() else self._current_target_id(),
                active_device_ids=active_ids,
                allow_broadcast=allow_broadcast,
            )
        except AutoSilenceTargetResolutionError as exc:
            QMessageBox.warning(
                self,
                "暂停上传后读取不可用",
                str(exc),
            )
            return None
        original_state = self._normalize_auto_upload_state(
            (operation_context or {}).get("original_auto_upload_state", self._auto_upload_state)
        )
        restore_policy = self._resolve_quiet_read_restore_policy(original_state)
        if not restore_policy:
            return None
        pause_target_text = (
            "FFF 广播暂停主动上传，会影响总线上所有响应设备"
            if str(silence_target).strip().upper() == "FFF"
            else f"设备 {silence_target}"
        )
        prompt = (
            "本次读取需要临时暂停主动上传；仅在你确认后才会发送 SETCOMWAY=0，并会记录系统动作。"
            " 该动作不会停止设备测量，只是临时暂停主动上报实时数据。\n\n"
            f"原状态：{self._quiet_read_original_state_text(original_state)}\n"
            f"暂停目标：{pause_target_text}\n"
            f"读取命令：{payload}\n\n"
            f"读取后处理：{self._quiet_read_restore_text(restore_policy)}\n\n"
            "是否继续？"
        )
        if QMessageBox.question(self, "确认暂停上传后读取", prompt) != QMessageBox.StandardButton.Yes:
            return None
        merged_context = dict(operation_context or {})
        merged_context.update(
            {
                "quiet_read": "1",
                "quiet_read_requested": True,
                "original_auto_upload_state": original_state,
                "restore_policy": restore_policy,
                "allow_broadcast_silence": allow_broadcast and silence_target == "FFF",
            }
        )
        return merged_context

    def _connect_session(self) -> None:
        self._disconnect_expected = False
        self.reconnect_timer.stop()
        self._set_connection_state("connecting", "正在连接")
        self.controller.connect_session(self._build_config())

    def _disconnect_session(self) -> None:
        self._disconnect_expected = True
        self.reconnect_timer.stop()
        self.controller.disconnect_session()

    def _manual_reconnect(self) -> None:
        self._disconnect_expected = False
        self.reconnect_timer.stop()
        self._connect_session()

    def _run_safe_handshake(self) -> None:
        if not self.connected:
            QMessageBox.warning(self, "未连接", "请先连接设备，再执行安全握手。")
            return
        if self._current_session_mode() == SESSION_MODE_LISTEN_ONLY:
            QMessageBox.warning(self, "会话模式限制", "严格只听模式禁止发送命令。请切换到“实时监测模式”或“工程联调模式”。")
            return
        self.controller.update_config(self._build_config())
        timeout_ms = self._current_command_timeout_ms()
        for command_id in SAFE_QUERY_COMMAND_IDS:
            definition = self.registry.get(command_id, self._current_profile_name())
            payload = self.registry.build_preview(command_id, self._current_target_id(), {}, profile_name=self._current_profile_name())
            self._remember_safe_command(payload, definition.return_type, definition.display_name)
            self.controller.send_payload(payload, expectation=definition.return_type, timeout_ms=timeout_ms)

    def _activate_demo_mode(self) -> None:
        self.port_combo.setCurrentText("SIMULATOR")
        self._set_acquisition_mode("LISTEN")
        self._set_parse_mode("AUTO")
        self.permission_combo.setCurrentText("CONFIG")
        engineering_index = self.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
        if engineering_index >= 0:
            self.session_mode_combo.setCurrentIndex(engineering_index)
        self.baud_combo.setCurrentText("115200")
        self._device_output_mode = "AUTO"
        self._device_mode_confirmed = False
        self._device_auto_upload = True
        self._set_device_quick_status("演示模式已准备为自动识别 + 自动上传。")
        self._refresh_monitor_quick_controls()
        self._refresh_task_entry_states()
        self._append_info_message("已切换为演示模式，端口使用 SIMULATOR。")
        self._update_status_strip()

    def _handle_command_request(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        preview: str,
        effective_target: str,
        operation_context: dict[str, object] | None = None,
    ) -> None:
        if is_read_only_command(definition):
            resolved_target, target_error = self._resolve_explicit_read_target_id()
            if target_error:
                QMessageBox.warning(self, "读取目标不可用", target_error)
                return
            if resolved_target:
                effective_target = resolved_target
        current_permission = self.permission_combo.currentText()
        valid, reason = self.registry.validate_command(
            definition.command_id,
            effective_target,
            values,
            profile_name=self._current_profile_name(),
            permission_level=current_permission,
            broadcast_enabled=self.broadcast_check.isChecked(),
        )
        if not valid:
            QMessageBox.warning(self, "命令参数校验失败", reason)
            return
        permission_ok = has_permission(current_permission, definition.required_permission)
        if not permission_ok:
            QMessageBox.warning(
                self,
                "权限不足",
                f"{definition.display_name} 需要 {permission_label(definition.required_permission)}，当前为 {permission_label(current_permission)}。",
            )
            return
        safety_ok, safety_reason = can_execute_command(
            definition,
            connected=self.connected,
            session_mode=self._current_session_mode(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            replay_running=self.replay_running,
        )
        if not safety_ok:
            QMessageBox.warning(self, "会话安全限制", safety_reason)
            return

        preview = self.registry.build_preview(
            definition.command_id,
            effective_target,
            values,
            profile_name=self._current_profile_name(),
        )
        ack_pending_message = self._default_monitoring_pending_ack_only_duplicate_message(preview)
        if ack_pending_message:
            QMessageBox.warning(self, "ACK 等待中", ack_pending_message)
            self._set_control_common_feedback(ack_pending_message)
            self._set_device_quick_status(ack_pending_message)
            return
        duplicate_message = self._default_monitoring_duplicate_send_message(definition, values, preview)
        if duplicate_message:
            QMessageBox.warning(self, "重复发送已拦截", duplicate_message)
            self._set_control_common_feedback(duplicate_message)
            self._set_device_quick_status(duplicate_message)
            return
        mismatch_message = self._online_target_mismatch_message(definition, effective_target)
        if mismatch_message:
            QMessageBox.warning(self, "目标设备不一致", mismatch_message)
            return
        if definition.risk_level.lower() in {"medium", "high", "critical"} or effective_target == "FFF":
            if not self._confirm_high_risk_command(definition, preview, values):
                return

        self._mark_default_monitoring_step_requested(definition.command_id, values, preview)
        self._register_default_monitoring_ack_only_pending(definition, preview, effective_target)
        self.controller.update_config(self._build_config())
        if is_read_only_command(definition):
            self._remember_safe_command(preview, definition.return_type, definition.display_name)
            context = self._quiet_read_context_for_payload(preview, operation_context)
            if self._quiet_read_requested(operation_context) and context is None:
                return
            self.controller.send_payload(
                preview,
                expectation=definition.return_type,
                timeout_ms=self._current_command_timeout_ms(),
                context=context,
            )
            if context is not None:
                progress_text = self._quiet_read_progress_text(context)
                self._set_control_common_feedback(progress_text)
                self._set_device_quick_status(progress_text)
            return
        if self._should_auto_verify_write(definition):
            self._start_write_verification(definition, values, preview)
            return
        self.controller.send_payload(preview, expectation=definition.return_type, timeout_ms=self._current_command_timeout_ms())

    def _handle_readback_request(
        self,
        definition: CommandDefinition,
        operation_context: dict[str, object] | None = None,
    ) -> None:
        readback_definition = self.registry.readback_definition(definition.command_id, self._current_profile_name())
        if readback_definition is None:
            QMessageBox.warning(self, "读取当前参数不可用", f"{definition.display_name} 没有安全读取对应项。")
            return

        resolved_target, target_error = self._resolve_explicit_read_target_id()
        if target_error:
            QMessageBox.warning(self, "读取当前参数失败", target_error)
            return
        current_permission = self.permission_combo.currentText()
        permission_ok = has_permission(current_permission, readback_definition.required_permission)
        if not permission_ok:
            QMessageBox.warning(
                self,
                "权限不足",
                f"{readback_definition.display_name} 需要 {permission_label(readback_definition.required_permission)}，当前为 {permission_label(current_permission)}。",
            )
            return
        safety_ok, safety_reason = can_execute_command(
            readback_definition,
            connected=self.connected,
            session_mode=self._current_session_mode(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            replay_running=self.replay_running,
        )
        if not safety_ok:
            QMessageBox.warning(self, "读取当前参数失败", safety_reason)
            return

        payload = ""
        try:
            readback_definition, payload = self.registry.build_readback_preview(
                definition.command_id,
                resolved_target,
                profile_name=self._current_profile_name(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "读取当前参数失败", str(exc))
            return

        self.controller.update_config(self._build_config())
        self._pending_readback_requests[payload] = {
            "command_id": definition.command_id,
            "phase": "manual",
            "profile_name": self._current_profile_name(),
        }
        self._remember_safe_command(payload, readback_definition.return_type, f"{definition.display_name} / 读取当前参数")
        context = self._quiet_read_context_for_payload(payload, operation_context)
        if self._quiet_read_requested(operation_context) and context is None:
            self._pending_readback_requests.pop(payload, None)
            return
        self.controller.send_payload(
            payload,
            expectation=readback_definition.return_type,
            timeout_ms=self._current_command_timeout_ms(),
            context=context,
        )
        if context is not None:
            progress_text = self._quiet_read_progress_text(context)
            self._set_control_common_feedback(progress_text)
            self._set_device_quick_status(progress_text)

    def _should_auto_verify_write(self, definition: CommandDefinition) -> bool:
        return self.registry.is_write_command_id(definition.command_id) and self.registry.supports_readback(definition.command_id)

    def _start_write_verification(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        write_payload: str,
    ) -> None:
        checklist_context = self._running_checklist_context_for_payload(definition.command_id, write_payload)
        checklist_run_id = checklist_context.get("run_id")
        if not isinstance(checklist_run_id, int):
            checklist_run_id = None
        target_snapshot = self.registry.build_target_snapshot(
            definition.command_id,
            values,
            profile_name=self._current_profile_name(),
        )
        read_target, target_error = self._resolve_explicit_read_target_id()
        if target_error:
            report = WriteVerificationReport(
                target=target_snapshot,
                result_text="写前读取失败",
                detail_text=f"未执行写入：{target_error}",
            )
            self._set_verification_report(definition.command_id, report)
            self._record_session_change(
                definition.command_id,
                report,
                target_device_id=self._current_target_id(),
                source_page_override=str(checklist_context.get("source_page") or ""),
                checklist_run_id=checklist_run_id,
                checklist_kind=str(checklist_context.get("kind") or ""),
                checklist_step_id=str(checklist_context.get("step_id") or ""),
                checklist_planned_payload=str(checklist_context.get("planned_payload") or ""),
                command_payload=write_payload,
            )
            QMessageBox.warning(self, "写前读取失败", target_error)
            return
        readback_definition, read_payload = self.registry.build_readback_preview(
            definition.command_id,
            read_target,
            profile_name=self._current_profile_name(),
        )
        self._set_verification_report(
            definition.command_id,
            WriteVerificationReport(
                target=target_snapshot,
                result_text="写前读取中",
                detail_text="正在读取写前值，读取成功后才会执行写入。",
            ),
        )
        self._pending_readback_requests[read_payload] = {
            "command_id": definition.command_id,
            "phase": "before_write",
            "profile_name": self._current_profile_name(),
            "default_monitoring_run_id": checklist_run_id,
            "default_monitoring_kind": str(checklist_context.get("kind") or ""),
            "default_monitoring_step_id": str(checklist_context.get("step_id") or ""),
            "default_monitoring_planned_payload": str(checklist_context.get("planned_payload") or ""),
            "default_monitoring_source_page": str(checklist_context.get("source_page") or ""),
            "write_payload": write_payload,
            "write_expectation": definition.return_type,
            "target_snapshot": target_snapshot,
            "write_values": dict(values),
            "post_read_target": self._post_write_read_target(definition.command_id, values, read_target),
        }
        self._remember_safe_command(read_payload, readback_definition.return_type, f"{definition.display_name} / 写前读取")
        self.controller.send_payload(
            read_payload,
            expectation=readback_definition.return_type,
            timeout_ms=self._current_command_timeout_ms(),
        )

    def _post_write_read_target(self, command_id: str, values: dict[str, str], fallback_target: str) -> str:
        if str(command_id).upper() == "ID":
            new_id = str(values.get("new_id") or "").strip().upper()
            if new_id:
                return new_id
        return fallback_target

    def _set_verification_report(self, command_id: str, report: WriteVerificationReport) -> None:
        panel = self._panel_for_command_id(command_id)
        if panel is None:
            return
        panel.set_verification_report(command_id, report)

    def _resolve_explicit_read_target_id(self) -> tuple[str, str]:
        target_id = self._current_target_id()
        if target_id.isdigit():
            return target_id, ""
        if target_id != "FFF":
            return "", f"当前目标设备 ID 无效：{target_id or '--'}。请先校正目标设备。"
        active_numeric_ids = sorted({device_id for device_id in self.active_online_device_ids if device_id.isdigit()})
        if len(active_numeric_ids) == 1:
            return active_numeric_ids[0], ""
        if not active_numeric_ids:
            return "", "当前 target 为 FFF，且无法确认唯一在线设备 ID，不能执行对象读取。请先选择目标设备或重新握手。"
        return "", f"当前 target 为 FFF，但检测到多个在线设备 ID：{','.join(active_numeric_ids)}。请先隔离现场设备或重新握手。"

    @staticmethod
    def _typed_mode2_broadcast_phrase_matches(text: str) -> bool:
        normalized = str(text or "").strip()
        if normalized == MODE2_BROADCAST_CONFIRM_PHRASE:
            return True
        return " ".join(normalized.upper().split()) == MODE2_BROADCAST_CONFIRM_ASCII_PHRASE

    @staticmethod
    def _is_mode2_broadcast_payload(preview: str) -> bool:
        envelope = YGasProtocol.parse_command(preview)
        if envelope is None:
            return False
        if envelope.code != "MODE":
            return False
        if envelope.target_id != "FFF":
            return False
        if not envelope.args:
            return False
        return str(envelope.args[0] or "").strip().upper() in {"2", "MODE2", "校准模式"}

    def _is_mode2_broadcast_command(self, definition: CommandDefinition, preview: str) -> bool:
        if str(definition.command_id or "").upper() != "MODE":
            return False
        return self._is_mode2_broadcast_payload(preview)

    def _mode2_broadcast_confirmation_prompt(
        self,
        preview: str,
        *,
        source_label: str,
        flow_note: str,
        values: dict[str, str] | None = None,
    ) -> str:
        envelope = YGasProtocol.parse_command(preview)
        effective_target = envelope.target_id if envelope is not None else "FFF"
        active_online = ",".join(self.active_online_device_ids) if self.active_online_device_ids else "--"
        latest_online = self.latest_online_device_id or "--"
        session_target = self._current_target_id() or "--"
        definition = self.registry.get("MODE", self._current_profile_name())
        silence_notice = self._auto_silence_transparency_text(
            definition,
            {str(key): str(value) for key, value in dict(values or {}).items()},
            preview,
            effective_target,
        )
        lines = [
            f"来源：{source_label}",
            f"payload：{preview}",
            f"广播 target：{effective_target}",
            f"当前会话 target：{session_target}",
            f"最近在线设备：{latest_online}",
            f"当前在线设备：{active_online}",
            "风险：MODE2 是校准模式；FFF 广播可能影响总线上所有设备。",
        ]
        if silence_notice:
            lines.append(f"系统行为：{silence_notice}")
        lines.append(f"说明：{flow_note}")
        lines.append("")
        lines.append(
            f"请输入确认短语“{MODE2_BROADCAST_CONFIRM_PHRASE}”或“{MODE2_BROADCAST_CONFIRM_ASCII_PHRASE}”后继续："
        )
        return "\n".join(lines)

    def _confirm_mode2_broadcast_command(
        self,
        preview: str,
        *,
        source_label: str = "统一命令卡",
        flow_note: str = "确认后仍会继续走写前读取 / 写入 / 写后复核；这不是自动执行器。",
        values: dict[str, str] | None = None,
    ) -> bool:
        text, ok = QInputDialog.getText(
            self,
            "广播校准模式确认",
            self._mode2_broadcast_confirmation_prompt(
                preview,
                source_label=source_label,
                flow_note=flow_note,
                values=values,
            ),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            QMessageBox.warning(
                self,
                "广播校准确认已取消",
                "未完成 MODE2 + FFF 广播确认，本次不会继续当前发送链路，也不会发送命令。",
            )
            return False
        if not self._typed_mode2_broadcast_phrase_matches(text):
            QMessageBox.warning(
                self,
                "广播校准确认未通过",
                (
                    f"确认短语不匹配。请输入“{MODE2_BROADCAST_CONFIRM_PHRASE}”或“{MODE2_BROADCAST_CONFIRM_ASCII_PHRASE}”后才能继续；"
                    "本次不会继续当前发送链路，也不会发送命令。"
                ),
            )
            return False
        return True

    def _confirm_high_risk_command(
        self,
        definition: CommandDefinition,
        preview: str,
        values: dict[str, str] | None = None,
    ) -> bool:
        value_map = {str(key): str(value) for key, value in dict(values or {}).items()}
        envelope = YGasProtocol.parse_command(preview)
        effective_target = envelope.target_id if envelope is not None else self._current_target_id()
        if self._is_mode2_broadcast_command(definition, preview):
            return self._confirm_mode2_broadcast_command(
                preview,
                source_label="统一命令卡",
                flow_note="确认后仍会继续走写前读取 / 写入 / 写后复核；这不是自动执行器。",
                values=value_map,
            )
        extra_lines: list[str] = []
        if effective_target == "FFF":
            extra_lines.append("广播风险：当前命令将使用 FFF 广播，可能影响总线上所有设备。")
            session_target = self._current_target_id()
            if session_target.isdigit():
                extra_lines.append(f"对象一致性校验目标：设备 {session_target}。")
        if definition.command_id.upper() == "MODE" and preview.upper().endswith(",2"):
            extra_lines.append("动作风险：本次将切换到 MODE2 校准模式，请确认现场工况允许。")
            if effective_target == "FFF":
                extra_lines.append("MODE2 校准模式 + FFF 广播属于高风险组合，请再次确认不会影响其他在线设备。")
        if definition.command_id.upper() == "SETCOMWAY":
            mode_value = value_map.get("mode", envelope.args[0] if envelope is not None and envelope.args else "")
            if str(mode_value).strip() == "1":
                extra_lines.append("步骤说明：本次命令会开启主动上传。该命令不会修改 FTD 上传频率。")
        silence_notice = self._auto_silence_transparency_text(definition, value_map, preview, effective_target)
        if silence_notice:
            extra_lines.append(f"系统行为：{silence_notice}")
        reply = QMessageBox.question(
            self,
            "高风险命令确认" if effective_target != "FFF" else "高风险广播命令确认",
            (
                (
                    f"中文功能名: {definition.display_name}\n"
                    f"原始命令: {preview}\n"
                    f"当前权限等级: {self.permission_combo.currentText()}\n"
                    f"当前会话模式: {self._current_session_mode()}\n"
                    f"本次生效 target id: {effective_target}\n"
                    f"是否 FFF: {'是' if effective_target == 'FFF' else '否'}\n"
                    f"风险等级: {definition.risk_level.upper()}\n"
                )
                + ("\n".join(extra_lines) + "\n\n" if extra_lines else "\n")
                +
                "确认执行吗？"
            ),
        )
        return reply == QMessageBox.StandardButton.Yes

    def _read_panel_defaults(self, panel: CommandWorkspacePanel) -> None:
        if not self.connected:
            QMessageBox.warning(self, "未连接", "请先连接设备。")
            return
        timeout_ms = self._current_command_timeout_ms()
        if panel is self.control_panel:
            command_ids = SAFE_QUERY_COMMAND_IDS
            values_by_id = {command_id: {} for command_id in command_ids}
            command_sources = {
                "ID_QUERY": "ID",
                "MODE_QUERY": "MODE",
                "FTD_QUERY": "FTD",
                "SETCOM_QUERY": "SETCOM",
            }
        elif panel is self.signal_panel:
            command_ids = [
                "SETPOW_QUERY",
                "SETCO2_QUERY",
                "TIMEOUT_QUERY",
                "SENTEMP1_QUERY",
                "SENTEMP2_QUERY",
                "AVERAGE1_QUERY",
                "AVERAGE2_QUERY",
            ]
            values_by_id = {command_id: {} for command_id in command_ids}
            command_sources = {
                "SETPOW_QUERY": "SETPOW",
                "SETCO2_QUERY": "SETCO2",
                "TIMEOUT_QUERY": "TIMEOUT",
                "SENTEMP1_QUERY": "SENTEMP1",
                "SENTEMP2_QUERY": "SENTEMP2",
                "AVERAGE1_QUERY": "AVERAGE1",
                "AVERAGE2_QUERY": "AVERAGE2",
            }
        else:
            command_ids = ["GETCO"]
            values_by_id = {"GETCO": {"index": str(self._current_coefficient_index())}}
            command_sources = {"GETCO": self.coeff_panel.current_command().command_id}

        for command_id in command_ids:
            definition = self.registry.get(command_id, self._current_profile_name())
            safety_ok, safety_reason = can_execute_command(
                definition,
                connected=self.connected,
                session_mode=self._current_session_mode(),
                read_only_lock=self.read_only_lock_check.isChecked(),
                replay_running=self.replay_running,
            )
            if not safety_ok:
                QMessageBox.warning(self, "读取失败", f"{definition.display_name}: {safety_reason}")
                return

        self.controller.update_config(self._build_config())
        for command_id in command_ids:
            definition = self.registry.get(command_id, self._current_profile_name())
            effective_target = self.registry.default_target_for_command(command_id, self._current_target_id())
            if is_read_only_command(definition):
                effective_target, target_error = self._resolve_explicit_read_target_id()
                if target_error:
                    QMessageBox.warning(self, "读取失败", f"{definition.display_name}: {target_error}")
                    return
            mismatch_message = self._online_target_mismatch_message(definition, effective_target)
            if mismatch_message:
                QMessageBox.warning(self, "目标设备不一致", mismatch_message)
                return
            payload = self.registry.build_preview(
                command_id,
                effective_target,
                values_by_id[command_id],
                profile_name=self._current_profile_name(),
            )
            self._pending_readback_requests[payload] = {
                "command_id": command_sources.get(command_id, command_id),
                "phase": "manual",
                "profile_name": self._current_profile_name(),
            }
            self._remember_safe_command(payload, definition.return_type, definition.display_name)
            self.controller.send_payload(payload, expectation=definition.return_type, timeout_ms=timeout_ms)

    def _current_coefficient_index(self) -> int:
        current = self.coeff_panel.current_command().command_id
        digits = "".join(ch for ch in current if ch.isdigit())
        return max(1, min(9, int(digits))) if digits else 1

    def _online_target_mismatch_message(self, definition: CommandDefinition, effective_target: str) -> str:
        if not self._requires_target_consistency_guard(definition):
            return ""

        target = self._consistency_target_id(definition, effective_target)
        active_numeric_ids = sorted({device_id for device_id in self.active_online_device_ids if device_id.isdigit()})
        if self.registry.is_write_command_id(definition.command_id):
            if not target:
                return "当前写命令缺少明确目标设备 ID，无法执行对象一致性校验，请先校正目标设备或重新握手。"
            if not active_numeric_ids:
                return (
                    f"当前无法确认实时在线设备 ID：target={target}。"
                    "当前写命令虽默认使用 FFF，但对象一致性校验仍要求唯一在线设备。"
                    "请先校正目标设备或重新握手。"
                )
            if len(active_numeric_ids) > 1:
                online = ",".join(active_numeric_ids)
                return (
                    f"当前检测到多个实时在线设备 ID：target={target}，online={online}。"
                    "当前写命令虽默认使用 FFF，但对象一致性校验仍要求唯一在线设备。"
                    "请先隔离现场设备或重新握手。"
                )
            if active_numeric_ids[0] != target:
                return (
                    f"当前目标设备 ID 与实时在线设备 ID 不一致：target={target}，online={active_numeric_ids[0]}。"
                    "当前写命令虽默认使用 FFF，但仍禁止在对象不一致时发送。"
                    "请先校正目标设备或重新握手。"
                )
            return ""
        if target and len(active_numeric_ids) == 1 and active_numeric_ids[0] != target:
            return (
                f"当前目标设备 ID 与实时在线设备 ID 不一致：target={target}，online={active_numeric_ids[0]}，"
                "请先校正目标设备或重新握手。"
            )
        return ""

    def _requires_target_consistency_guard(self, definition: CommandDefinition) -> bool:
        command_id = definition.command_id.upper()
        if self.registry.is_write_command_id(command_id):
            return True
        if command_id in self.SIGNAL_COMMANDS:
            return True
        return command_id == "GETCO"

    def _consistency_target_id(self, definition: CommandDefinition, effective_target: str) -> str:
        direct_target = str(effective_target or "").strip().upper()
        if self.registry.is_write_command_id(definition.command_id):
            if direct_target.isdigit():
                return direct_target
            session_target = self._current_target_id()
            if session_target.isdigit():
                return session_target
            return ""
        if direct_target.isdigit():
            return direct_target
        return ""

    def _remember_safe_command(self, payload: str, expectation: str, label: str) -> None:
        item = {"payload": payload, "expectation": expectation, "label": label}
        self._safe_history = [entry for entry in self._safe_history if entry["payload"] != payload]
        self._safe_history.insert(0, item)
        self._safe_history = self._safe_history[:20]
        self._update_safe_history_combo()

    def _update_safe_history_combo(self) -> None:
        self.command_history_combo.clear()
        for item in self._safe_history:
            display = f"{item['label']} | {item['payload']}"
            self.command_history_combo.addItem(display, item)

    def _resend_history_command(self) -> None:
        item = self.command_history_combo.currentData()
        if not item:
            return
        payload = item["payload"]
        expectation = item["expectation"]
        definition = self._definition_for_payload(payload)
        if definition is not None:
            safety_ok, safety_reason = can_execute_command(
                definition,
                connected=self.connected,
                session_mode=self._current_session_mode(),
                read_only_lock=self.read_only_lock_check.isChecked(),
                replay_running=self.replay_running,
            )
            if not safety_ok:
                QMessageBox.warning(self, "快速重发受限", safety_reason)
                return
        self.controller.update_config(self._build_config())
        self.controller.send_payload(payload, expectation=expectation, timeout_ms=self._current_command_timeout_ms())

    def _definition_for_payload(self, payload: str) -> CommandDefinition | None:
        code = str(payload).split(",", 1)[0].replace("[FFF] ", "").strip().upper()
        for definition in self.registry.all_commands(self._current_profile_name()):
            if definition.code == code and is_read_only_command(definition):
                return definition
        return None

    def _export_history(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出最近数据 CSV",
            str(Path(self.last_export_dir) / "ygas_export.csv"),
            "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            output = self.controller.export_history(path)
            self.last_export_dir = str(Path(output).parent)
            QMessageBox.information(
                self,
                "导出成功",
                (
                    f"数据已导出到:\n{output}\n\n"
                    "结构化数据范围：最近缓存\n"
                    f"导出结构化帧数：{len(self.controller.frames)}\n"
                    "提示：该 CSV 来自最近缓存，不代表全量会话；如需全量请使用“导出会话包”。"
                ),
            )
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def _export_session_package(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(self, "选择会话包导出目录", self.last_export_dir)
        if not selected_dir:
            return
        try:
            output = export_session_package(
                session_name=self.session_name,
                frames=list(self.controller.frames),
                raw_records=list(self.controller.raw_records),
                config=self._build_config(),
                parameter_change_entries=list(self._session_change_history),
                command_entries=list(self._command_log_history),
                output_dir=selected_dir,
                logger_path=self.controller.log_path,
                note=self.session_note_edit.text().strip(),
            )
            self.last_export_dir = str(Path(selected_dir))
            summary_path = Path(output) / "session_summary.json"
            summary: dict[str, object] = {}
            if summary_path.exists():
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except Exception:
                    summary = {}
            structured_scope = "全量会话数据" if str(summary.get("structured_export_scope") or "") == "full" else "最近缓存"
            structured_count = int(summary.get("structured_frame_count_exported") or 0)
            raw_rx_total = int(summary.get("raw_rx_count_total") or 0)
            raw_tx_total = int(summary.get("raw_tx_count_total") or 0)
            message = (
                f"已导出到:\n{output}\n\n"
                f"结构化数据范围：{structured_scope}\n"
                f"导出结构化帧数：{structured_count}\n"
                f"raw RX 总数：{raw_rx_total}\n"
                f"raw TX 总数：{raw_tx_total}"
            )
            if str(summary.get("structured_export_scope") or "") == "recent_cache":
                message += "\n\n当前结构化 CSV 来自最近缓存，如需完整原始日志请查看 session_logger.log。"
            QMessageBox.information(self, "会话包导出成功", message)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))

    def _export_diagnostic(self) -> None:
        selected_dir = QFileDialog.getExistingDirectory(self, "选择诊断包导出目录", self.last_export_dir)
        if not selected_dir:
            return
        try:
            output = export_diagnostic_package(
                session_name=self.session_name,
                frames=list(self.controller.frames),
                raw_records=list(self.controller.raw_records),
                config=self._build_config(),
                parameter_change_entries=list(self._session_change_history),
                command_entries=list(self._command_log_entries),
                output_dir=selected_dir,
                logger_path=self.controller.log_path,
                note=self.session_note_edit.text().strip(),
            )
            self.last_export_dir = str(Path(selected_dir))
            QMessageBox.information(self, "诊断包导出成功", f"已导出到:\n{output}")
        except Exception as exc:
            QMessageBox.critical(self, "诊断包导出失败", str(exc))

    def _open_export_dir(self) -> None:
        path = Path(self.last_export_dir or EXPORT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_log_dir(self) -> None:
        path = Path(LOG_DIR)
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_current_log_file(self) -> None:
        current_log_path = Path(str(self.controller.log_path or "").strip()) if self.controller.log_path else None
        if current_log_path is not None and current_log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(current_log_path)))
            return
        self._open_log_dir()

    def _copy_environment_info(self) -> None:
        text = environment_summary_text()
        QGuiApplication.clipboard().setText(text)
        QMessageBox.information(self, "已复制", "环境信息已复制到剪贴板。")

    def _show_recent_command_log(self) -> None:
        if not self._command_log_history:
            QMessageBox.information(self, "最近命令日志", "当前会话还没有命令日志。")
            return
        preview_lines: list[str] = []
        for entry in list(self._command_log_history)[:10]:
            label = str(entry.action_label_zh or action_label_zh(entry.action_type or "command")).strip() or "命令执行"
            detail = str(entry.detail_text or "--").strip() or "--"
            preview_lines.append(
                f"{self._fmt_ts(entry.timestamp)} | {label} | {entry.payload} | {entry.result} | {detail}"
            )
        QMessageBox.information(
            self,
            "最近命令日志",
            "最近 10 条命令日志：\n\n"
            + "\n".join(preview_lines)
            + "\n\n如需完整命令日志，可导出会话包查看 command_log.tsv / command_log.csv。",
        )

    def _refresh_restore_retry_actions(self) -> None:
        for panel in self._command_panels():
            panel.detail_widget.refresh_restore_actions()
        if self.custom_panel is not None:
            self.custom_panel.detail_widget.refresh_restore_actions()

    def _validate_restore_retry_request(self, payload: str) -> tuple[bool, str, str, dict[str, str]]:
        retry_payload = str(payload or "").strip()
        if not retry_payload:
            return False, "未提供恢复主动上传命令。", "", {}
        envelope = YGasProtocol.parse_command(retry_payload)
        if envelope is None or envelope.code != "SETCOMWAY" or envelope.args[:1] != ["1"]:
            return False, "恢复主动上传只允许发送有效的 SETCOMWAY=1 命令。", retry_payload, {}

        metadata = dict(self._restore_retry_candidates.get(retry_payload, {}))
        if not metadata:
            return False, "当前没有与该恢复命令匹配的恢复失败上下文，已阻止发送。", retry_payload, {}

        definition = self.registry.get("SETCOMWAY", self._current_profile_name())
        if not self.connected:
            return False, "当前未连接设备。", retry_payload, metadata
        if self.read_only_lock_check.isChecked():
            return False, "只读锁已开启，禁止重试恢复主动上传。", retry_payload, metadata
        if not has_permission(self.permission_combo.currentText(), definition.required_permission):
            return (
                False,
                f"当前权限不足，至少需要 {permission_label(definition.required_permission)} 权限。",
                retry_payload,
                metadata,
            )
        if self.replay_running:
            return False, "回放期间禁止向真实设备发送命令。", retry_payload, metadata
        safety_ok, safety_reason = can_execute_command(
            definition,
            connected=self.connected,
            session_mode=self._current_session_mode(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            replay_running=self.replay_running,
        )
        if not safety_ok:
            return False, safety_reason, retry_payload, metadata

        self.controller.update_config(self._build_config())
        controller_ok, controller_reason = self.controller.validate_restore_retry_payload(retry_payload)
        if not controller_ok:
            return False, controller_reason, retry_payload, metadata

        metadata.setdefault("parent_command", "")
        metadata.setdefault("original_auto_upload_state", self._quiet_read_original_state or "unknown")
        metadata.setdefault("restore_policy", "restore_after_read")
        metadata.setdefault("command_target_id", str(envelope.target_id or "").strip().upper())
        metadata.setdefault("expected_device_id", "")
        metadata.setdefault("response_device_id", "")
        metadata.setdefault("effective_scope", AutoSilencePolicy.effective_scope(str(envelope.target_id or "").strip().upper()))
        return True, "", retry_payload, metadata

    def _retry_restore_auto_upload(self, payload: str) -> None:
        allowed, reason, retry_payload, metadata = self._validate_restore_retry_request(payload)
        if not allowed:
            self._refresh_restore_retry_actions()
            message = f"已阻止重试恢复主动上传：{reason}"
            self._set_control_common_feedback(message)
            self._set_device_quick_status(message)
            QMessageBox.warning(self, "重试恢复主动上传失败", reason)
            return
        self._pending_restore_retry_requests[retry_payload] = metadata
        self._quiet_read_after_state = "pending_restore"
        self._set_auto_upload_state("off")
        self.controller.send_payload(retry_payload, expectation="ack", timeout_ms=self._current_command_timeout_ms())
        message = "正在恢复主动上传；已发送 SETCOMWAY=1。"
        self._set_control_common_feedback(message)
        self._set_device_quick_status(message)

    def _keep_auto_upload_closed_after_restore_failure(self, payload: str) -> None:
        self._record_keep_auto_upload_off_decision(payload)
        self._quiet_read_after_state = "kept_off"
        self._set_auto_upload_state("off")
        message = "已选择保持关闭；当前不会再次自动发送 SETCOMWAY=1。"
        self._set_control_common_feedback(message)
        self._set_device_quick_status(message)

    def _record_keep_auto_upload_off_decision(self, payload: str) -> None:
        retry_payload = str(payload or "").strip()
        metadata = dict(self._restore_retry_candidates.pop(retry_payload, {}))
        self._pending_restore_retry_requests.pop(retry_payload, None)

        envelope = YGasProtocol.parse_command(retry_payload) if retry_payload else None
        command_target_id = str(
            metadata.get("command_target_id") or (envelope.target_id if envelope is not None else "") or ""
        ).strip().upper()
        expected_device_id = str(metadata.get("expected_device_id") or "").strip().upper()
        if not expected_device_id and command_target_id.isdigit():
            expected_device_id = command_target_id
        response_device_id = str(
            metadata.get("response_device_id") or expected_device_id or command_target_id or self._current_target_id() or ""
        ).strip().upper()
        effective_scope = str(
            metadata.get("effective_scope") or AutoSilencePolicy.effective_scope(command_target_id or self._current_target_id())
        ).strip()
        original_state = str(
            metadata.get("original_auto_upload_state") or self._quiet_read_original_state or "unknown"
        ).strip()
        parent_command = retry_payload or str(metadata.get("parent_command") or "").strip()
        detail_text = "用户选择保持主动上传关闭，系统不会再次自动发送 SETCOMWAY=1。"
        result = CommandResult(
            timestamp=datetime.now(),
            command=retry_payload,
            ok=True,
            message=detail_text,
            response_device_id=response_device_id or None,
            action_type="keep_auto_upload_off",
            action_label_zh=action_label_zh("keep_auto_upload_off"),
            parent_command=parent_command,
            command_target_id=command_target_id,
            expected_device_id=expected_device_id,
            effective_scope=effective_scope,
            source_page="恢复主动上传",
            auto_upload_state="off",
            original_auto_upload_state=original_state,
            restore_policy="keep_off",
            restore_attempted=True,
            restore_result="kept_off",
        )
        self._record_command_log_event(result, detail_text)
        report = WriteVerificationReport(
            before=StructuredValueSnapshot(summary="主动上传已临时暂停"),
            target=StructuredValueSnapshot(summary="主动上传保持关闭"),
            after=StructuredValueSnapshot(summary="用户确认保持关闭"),
            result_text="用户确认",
            detail_text=self._auto_upload_detail_text(detail_text, result),
            verification_status="decision_recorded",
            verified_at=result.timestamp,
            source_device_id=response_device_id or expected_device_id or command_target_id or self._current_target_id(),
        )
        self._record_session_change(
            "保持主动上传关闭",
            report,
            timestamp=result.timestamp,
            target_device_id=response_device_id or expected_device_id or command_target_id or self._current_target_id(),
            source_page_override="恢复主动上传",
            command_result=result,
            command_payload=retry_payload,
            expected_device_id=expected_device_id,
            is_system_action_override=False,
        )

    def _load_replay_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载 CSV 回放",
            self.last_replay_file or str(Path.cwd()),
            "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            self.replay_dataset = load_replay_dataset(path)
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", str(exc))
            return

        self.last_replay_file = path
        self.replay_index = 0
        self.replay_running = False
        self._set_command_replay_block(False)
        self._reset_replay_view()
        assert self.replay_dataset is not None
        self.replay_progress.setEnabled(True)
        self.replay_progress.setRange(0, max(0, len(self.replay_dataset.frames) - 1))
        self.replay_progress.setValue(0)
        self.replay_progress_bar.setValue(0)
        self.replay_info_label.setText(f"已加载 {len(self.replay_dataset.frames)} 条回放记录。")
        self._update_replay_time_label()

    def _start_replay(self) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            QMessageBox.information(self, "回放提示", "请先加载回放 CSV。")
            return
        if self.replay_index == 0:
            self._reset_replay_view()
        self.replay_running = True
        self.reconnect_timer.stop()
        self._set_command_replay_block(True)
        self._set_connection_state("replay", "正在回放")
        self._update_status_strip(force_mode=SESSION_MODE_REPLAY)
        self._schedule_next_replay(initial=True)

    def _pause_replay(self) -> None:
        self.replay_timer.stop()
        self.replay_running = False
        self._set_command_replay_block(False)
        self._set_connection_state("connected" if self.connected else "disconnected", "已暂停回放")
        self._update_status_strip()

    def _step_replay(self, delta: int) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            return
        self._pause_replay()
        self._seek_replay(max(0, min(len(self.replay_dataset.frames) - 1, self.replay_progress.value() + delta)))

    def _jump_to_replay_end(self) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            return
        self._pause_replay()
        self._seek_replay(len(self.replay_dataset.frames) - 1)

    def _seek_replay_from_slider(self) -> None:
        self._pause_replay()
        self._seek_replay(self.replay_progress.value())

    def _seek_replay(self, index: int) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            return
        index = max(0, min(len(self.replay_dataset.frames) - 1, int(index)))
        self._reset_replay_view()
        for frame_index, frame in enumerate(self.replay_dataset.frames[: index + 1]):
            self._apply_replay_frame(frame)
            self.replay_index = frame_index
        self.replay_progress.setValue(index)
        self._update_replay_progress()
        self._update_replay_time_label()

    def _schedule_next_replay(self, *, initial: bool = False) -> None:
        if not self.replay_running or not self.replay_dataset or not self.replay_dataset.frames:
            return
        if initial or self.replay_index == 0:
            self.replay_timer.start(0)
            return
        prev_frame = self.replay_dataset.frames[self.replay_index - 1]
        next_frame = self.replay_dataset.frames[self.replay_index]
        delta_ms = int((next_frame.timestamp - prev_frame.timestamp).total_seconds() * 1000)
        delta_ms = max(20, min(delta_ms, 2000))
        speed = float(self.speed_combo.currentData() or 1.0)
        self.replay_timer.start(max(1, int(delta_ms / speed)))

    def _replay_tick(self) -> None:
        if not self.replay_dataset or not self.replay_running:
            return
        if self.replay_index >= len(self.replay_dataset.frames):
            self._finish_replay()
            return
        frame = self.replay_dataset.frames[self.replay_index]
        self._apply_replay_frame(frame)
        self.replay_progress.setValue(self.replay_index)
        self._update_replay_progress()
        self._update_replay_time_label()
        self.replay_index += 1
        if self.replay_index >= len(self.replay_dataset.frames):
            self._finish_replay()
            return
        self._schedule_next_replay()

    def _finish_replay(self) -> None:
        self.replay_timer.stop()
        self.replay_running = False
        self._set_command_replay_block(False)
        self._set_connection_state("connected" if self.connected else "disconnected", "回放结束")
        self._update_status_strip()
        QMessageBox.information(self, "回放结束", "CSV 回放已完成。")

    def _apply_replay_frame(self, frame: ParsedFrame) -> None:
        self.chart_panel.add_frame(frame)
        if self.pages.currentIndex() == self.monitor_tab_index:
            self._update_data_cards(frame)
            self.status_panel.update_frame(frame)
        self.raw_frames.append_record(RawFrameRecord(timestamp=frame.timestamp, direction="REPLAY", text=frame.raw))
        decoded = YGasProtocol.decode_status(frame.status)
        active_alarm_bits = {item.bit for item in decoded if item.is_alarm}
        newly_active = active_alarm_bits - self.replay_alarm_bits
        for item in decoded:
            if item.bit in newly_active:
                self.status_panel.append_event("状态", self._fmt_ts(frame.timestamp), f"{item.label}: {item.description}")
        self.replay_alarm_bits = active_alarm_bits

    def _reset_replay_view(self) -> None:
        self.replay_alarm_bits.clear()
        self.chart_panel.clear()
        self.status_panel.clear()
        self.raw_frames.clear_records()
        self.data_cards.update_items([])
        self.detail_cards.update_items([])
        self.monitor_cards.update_items([])

    def _set_command_replay_block(self, blocked: bool) -> None:
        for panel in self._command_panels():
            panel.set_replay_blocked(blocked)
            panel.set_connected(self.connected)
            panel.set_session_mode(self._current_session_mode() if not blocked else SESSION_MODE_REPLAY)
            panel.set_read_only_lock(self.read_only_lock_check.isChecked())
        if self.custom_panel is not None:
            self.custom_panel.set_replay_blocked(blocked)

    def _raw_command_definition(self, payload: str) -> CommandDefinition | None:
        envelope = YGasProtocol.parse_command(payload)
        if envelope is None:
            return None
        candidates = [
            item
            for item in self.registry.all_commands(self._current_profile_name())
            if item.code == envelope.code
        ]
        if not candidates:
            return None
        exact_matches = [item for item in candidates if len(item.parameters) == len(envelope.args)]
        if exact_matches:
            return exact_matches[0]
        if not envelope.args:
            read_candidates = [item for item in candidates if is_read_only_command(item)]
            if read_candidates:
                return read_candidates[0]
        write_candidates = [item for item in candidates if not is_read_only_command(item)]
        if write_candidates:
            return write_candidates[0]
        return candidates[0]

    def _raw_command_metadata(self, payload: str) -> dict[str, object]:
        definition = self._raw_command_definition(payload)
        envelope = YGasProtocol.parse_command(payload)
        if definition is not None:
            return {
                "definition": definition,
                "is_query": is_read_only_command(definition),
                "label": definition.display_name,
                "recognized": True,
            }
        if envelope is None:
            return {
                "definition": None,
                "is_query": False,
                "label": "未识别原始命令",
                "recognized": False,
            }
        return {
            "definition": None,
            "is_query": False,
            "label": envelope.code,
            "recognized": False,
        }

    def _confirm_raw_write_bypass(self, payload: str, label: str) -> bool:
        reply = QMessageBox.question(
            self,
            "原始写命令二次确认",
            (
                "当前发送的是原始写命令旁路能力，可能绕过普通命令卡的参数语义和页面引导。\n"
                f"命令：{label}\n"
                f"原文：{payload}\n\n"
                "请仅在确认现场隔离、目标设备无误且已理解风险时继续。是否发送？"
            ),
        )
        return reply == QMessageBox.StandardButton.Yes

    def _send_raw_command(self) -> None:
        payload = self.raw_command_edit.text().strip()
        if not payload:
            return
        expert_reason = self._expert_terminal_block_reason()
        if expert_reason:
            QMessageBox.warning(self, "专家终端未启用", f"{expert_reason} 原始命令发送不可用。")
            return
        if self.replay_running:
            QMessageBox.warning(self, "回放中", "回放期间禁止向真实设备发送命令。")
            return
        if not self.connected:
            QMessageBox.warning(self, "未连接设备", "请先连接设备，再发送原始命令。")
            return

        metadata = self._raw_command_metadata(payload)
        is_query = bool(metadata.get("is_query"))
        label = str(metadata.get("label") or "原始命令")
        typed_broadcast_confirmed = False
        if not is_query:
            if self._current_session_mode() != SESSION_MODE_ENGINEERING:
                QMessageBox.warning(self, "工程模式未开启", "原始写命令只允许在工程模式下发送。")
                return
            if self.read_only_lock_check.isChecked():
                QMessageBox.warning(self, "只读锁已开启", "原始写命令已被拦截，请先关闭只读锁。")
                return
            if not self._confirm_raw_write_bypass(payload, label):
                return
            if self._is_mode2_broadcast_payload(payload):
                if not self._confirm_mode2_broadcast_command(
                    payload,
                    source_label="专家终端原始命令",
                    flow_note="确认后仍会继续走当前 EXPERT 原始命令发送链路；这不是自动执行器，也不会降低既有门槛。",
                ):
                    return
                typed_broadcast_confirmed = True
        if not typed_broadcast_confirmed and (payload.upper().endswith(",FFF") or ",FFF," in payload.upper()):
            reply = QMessageBox.question(
                self,
                "广播高风险警告",
                "当前原始命令将使用 FFF 广播地址，这可能同时影响多台设备。是否继续？",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.controller.send_payload(
            payload,
            expectation=self.raw_expectation_combo.currentText(),
            timeout_ms=self._current_command_timeout_ms(),
        )

    def _custom_template_saved(self) -> None:
        self.registry = CommandRegistry()
        self.custom_editor.registry = self.registry
        self._rebuild_custom_panel()
        QMessageBox.information(self, "模板已保存", "新的自定义命令模板已写入本地目录。")

    def _rebuild_custom_panel(self) -> None:
        while self.custom_panel_layout.count():
            item = self.custom_panel_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.custom_panel = None
        custom_families = sorted(
            {item.family for item in self.registry.all_commands(self._current_profile_name()) if not item.builtin}
        )
        if not custom_families:
            hint = QLabel("尚未注册自定义命令模板。")
            hint.setProperty("muted", True)
            self.custom_panel_layout.addWidget(hint)
            return

        self.custom_panel = CommandWorkspacePanel(
            self.registry,
            custom_families,
            profile_name=self._current_profile_name(),
            source_panel="custom_panel",
        )
        self.custom_panel.read_page_button.hide()
        self.custom_panel.set_target_id(self._current_target_id())
        self.custom_panel.set_permission_level(self.permission_combo.currentText())
        self.custom_panel.set_auto_upload_state(self._auto_upload_state)
        self.custom_panel.set_broadcast_enabled(self.broadcast_check.isChecked())
        self.custom_panel.set_connected(self.connected)
        self.custom_panel.set_session_mode(self._current_session_mode())
        self.custom_panel.set_read_only_lock(self.read_only_lock_check.isChecked())
        self.custom_panel.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        self.custom_panel.command_requested.connect(self._handle_command_request)
        self.custom_panel.readback_requested.connect(self._handle_readback_request)
        self.custom_panel_layout.addWidget(self.custom_panel)

    def _apply_permission_mode(self) -> None:
        engineering = self._current_session_mode() == SESSION_MODE_ENGINEERING
        self.init_button.setEnabled(engineering and not self.replay_running and not self.read_only_lock_check.isChecked())
        self.safe_handshake_button.setEnabled(
            self.connected and not self.replay_running and self._current_session_mode() != SESSION_MODE_LISTEN_ONLY
        )
        expert_available = self._expert_terminal_available()
        self.show_expert_check.setEnabled(self.permission_combo.currentText() == "EXPERT")
        self.raw_send_button.setEnabled(
            expert_available
            and not self.replay_running
        )
        self.pages.setTabVisible(self.expert_tab_index, expert_available)
        if expert_available:
            self.expert_terminal_hint_label.setText(
                "专家终端已启用。可发送原始命令并使用自定义模板，但所有风险动作仍需现场复核。"
            )
        elif self.permission_combo.currentText() != "EXPERT":
            self.expert_terminal_hint_label.setText("当前权限等级不是 EXPERT，专家终端保持隐藏，原始命令发送不可用。")
        else:
            self.expert_terminal_hint_label.setText(
                "当前已切换到 EXPERT 权限，但尚未启用专家终端；“串口助手”页保持隐藏，原始命令发送不可用。"
            )
        if not expert_available and self.pages.currentIndex() == self.expert_tab_index:
            self.pages.setCurrentIndex(self.settings_tab_index)
        for panel in self._command_panels():
            panel.set_permission_level(self.permission_combo.currentText())
            panel.set_connected(self.connected)
            panel.set_session_mode(self._current_session_mode())
            panel.set_read_only_lock(self.read_only_lock_check.isChecked())
            panel.set_broadcast_enabled(self.broadcast_check.isChecked())
            panel.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        if self.custom_panel is not None:
            self.custom_panel.set_permission_level(self.permission_combo.currentText())
            self.custom_panel.set_connected(self.connected)
            self.custom_panel.set_session_mode(self._current_session_mode())
            self.custom_panel.set_read_only_lock(self.read_only_lock_check.isChecked())
            self.custom_panel.set_broadcast_enabled(self.broadcast_check.isChecked())
            self.custom_panel.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        self._refresh_session_safety_hint()
        self._update_status_strip()
        self._refresh_monitor_quick_controls()

    def _profile_changed(self) -> None:
        profile = get_profile(self._current_profile_name())
        self.profile_hint_label.setText(profile.description)
        for panel in self._command_panels():
            panel.set_profile_name(profile.name)
            panel.set_target_id(self._current_target_id())
        if self.custom_panel is not None:
            self.custom_panel.set_profile_name(profile.name)
            self.custom_panel.set_target_id(self._current_target_id())

    def _target_changed(self, target_id: str) -> None:
        normalized = target_id.strip().upper() or "001"
        if normalized != "FFF":
            self.last_non_broadcast_target = normalized
        self._default_config_scheduled = False
        self._default_config_scheduled_target_id = ""
        if self._default_monitoring_steps:
            checklist_label = self._current_prepared_checklist_label()
            self._clear_default_monitoring_checklist(
                f"目标设备已切换，旧的{checklist_label}已清空；如需继续，请重新准备。"
            )
            self._set_device_quick_status(f"目标设备已切换，请重新准备{checklist_label}；不会自动发送。")
        self._update_target_badge()
        self.controller.update_config(self._build_config())
        for panel in self._command_panels():
            panel.set_target_id(normalized)
        if self.custom_panel is not None:
            self.custom_panel.set_target_id(normalized)

    def _broadcast_toggled(self, checked: bool) -> None:
        if checked:
            reply = QMessageBox.question(
                self,
                "广播高风险警告",
                "FFF 是广播地址，可能同时作用于多台设备。只有确认现场隔离后才应启用。是否继续？",
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.broadcast_check.blockSignals(True)
                self.broadcast_check.setChecked(False)
                self.broadcast_check.blockSignals(False)
                return
            self.target_combo.setCurrentText("FFF")
        elif self._current_target_id() == "FFF":
            self.target_combo.setCurrentText(self.last_non_broadcast_target)
        self._update_target_badge()
        self._apply_permission_mode()

    def _toggle_freeze(self, frozen: bool) -> None:
        self.monitor_frozen = frozen
        self.freeze_button.setText("恢复显示" if frozen else "冻结显示")
        if not frozen:
            self._refresh_monitor_from_cache()

    def _refresh_monitor_from_cache(self) -> None:
        if self.controller.frames:
            self.chart_panel.clear()
            for frame in list(self.controller.frames):
                self.chart_panel.add_frame(frame)
            self.chart_panel.request_refresh(immediate=True)
            latest = self.controller.frames[-1]
            self._update_data_cards(latest)
            self.status_panel.clear()
            self.status_panel.update_frame(latest)
            for alarm in list(self.controller.alarms):
                self.status_panel.append_alarm(alarm)
        else:
            self.chart_panel.clear()
            self.status_panel.clear()
            self.data_cards.update_items([])
            self.detail_cards.update_items([])
            self.monitor_cards.update_items([])
        self.raw_frames.clear_records()
        for record in list(self.controller.raw_records):
            self.raw_frames.append_record(record)
        self.raw_frames.render()
        if self.last_metrics is not None:
            self._update_monitor_metrics_cards(self.last_metrics)
        else:
            self.monitor_cards.update_items([])
        self._update_anomaly_summary()
        self._refresh_monitor_quick_controls()

    def _session_write_definition(self) -> CommandDefinition:
        return self.registry.get("FTD", self._current_profile_name())

    def _current_write_strategy_target(self) -> str:
        page_index = self.pages.currentIndex() if hasattr(self, "pages") else -1
        panel: CommandWorkspacePanel | None = None
        if page_index == self.control_tab_index:
            panel = self.control_panel
        elif page_index == self.coeff_tab_index:
            panel = self.coeff_panel
        elif page_index == self.signal_tab_index:
            panel = self.signal_panel
        elif page_index == self.expert_tab_index:
            panel = self.custom_panel
        if panel is not None:
            detail = panel.detail_widget
            if detail.definition and self.registry.is_write_command_id(detail.definition.command_id):
                if detail.force_single_target_check.isChecked():
                    return detail.target_override_edit.text().strip().upper() or self._current_target_id()
        return self.registry.default_target_for_command("FTD", self._current_target_id())

    def _summarize_write_block_reason(self, reason: str) -> str:
        text = str(reason or "")
        if "只监听" in text:
            return "严格只听模式"
        if "安全握手" in text or "实时监测" in text:
            return "实时监测模式"
        if "只读会话锁" in text or "严格只读" in text:
            return "严格只读"
        if "回放期间" in text:
            return "回放模式"
        if "未连接" in text:
            return "当前未连接设备"
        if "多个实时在线设备" in text or "多个在线设备" in text:
            return "当前无唯一在线设备"
        if "无法确认实时在线设备" in text:
            return "当前无唯一在线设备"
        if "缺少明确目标设备" in text or "目标设备 ID 无效" in text:
            return "当前会话目标不是单 ID"
        if "不一致" in text:
            return "target-online 不一致"
        return text or "--"

    def _build_session_write_status(self) -> SessionWriteStatus:
        active_numeric_ids = sorted({device_id for device_id in self.active_online_device_ids if device_id.isdigit()})
        if len(active_numeric_ids) > 1:
            online_text = "多个"
        elif len(active_numeric_ids) == 1:
            online_text = active_numeric_ids[0]
        else:
            online_text = self.latest_online_device_id or "--"

        definition = self._session_write_definition()
        effective_send = self._current_write_strategy_target()
        current_permission = self.permission_combo.currentText()
        control_write_allowed = False
        control_reason = "--"
        safety_ok, safety_reason = can_execute_command(
            definition,
            connected=self.connected,
            session_mode=self._current_session_mode(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            replay_running=self.replay_running,
        )
        if not safety_ok:
            control_reason = self._summarize_write_block_reason(safety_reason)
        elif not has_permission(current_permission, definition.required_permission):
            control_reason = "当前权限等级为只读"
        else:
            mismatch_message = self._online_target_mismatch_message(definition, effective_send)
            if mismatch_message:
                control_reason = self._summarize_write_block_reason(mismatch_message)
            else:
                control_write_allowed = True
                control_reason = "已满足写入条件"

        stream_start_allowed, stream_status_text, stream_reason_text, stream_status_code = self._build_stream_start_status()
        return SessionWriteStatus(
            online_device_text=online_text,
            session_target_text=self._current_target_id(),
            effective_send_text=effective_send,
            write_allowed=control_write_allowed,
            reason_text=control_reason,
            control_write_text="允许" if control_write_allowed else "禁止",
            control_reason_text=control_reason,
            stream_start_allowed=stream_start_allowed,
            stream_status_text=stream_status_text,
            stream_reason_text=stream_reason_text,
            stream_status_code=stream_status_code,
        )

    def _update_hard_status_bar(self) -> None:
        self._hard_status_state = self._build_session_write_status()
        self.hard_online_value_label.setText(self._hard_status_state.online_device_text)
        self.hard_target_value_label.setText(self._hard_status_state.session_target_text)
        self.hard_send_value_label.setText(self._hard_status_state.effective_send_text)
        self.hard_write_permission_label.setText(self._hard_status_state.control_write_text)
        self.hard_stream_status_label.setText(self._hard_status_state.stream_status_text)
        self.hard_reason_value_label.setText(
            f"控制写入：{self._hard_status_state.control_reason_text}；实时流：{self._hard_status_state.stream_reason_text}"
        )
        permission_state = "connected" if self._hard_status_state.write_allowed else "fault"
        self.hard_write_permission_label.setProperty("state", permission_state)
        self.hard_write_permission_label.style().unpolish(self.hard_write_permission_label)
        self.hard_write_permission_label.style().polish(self.hard_write_permission_label)
        stream_state = "connected"
        if self._hard_status_state.stream_status_code.startswith("blocked") or self._hard_status_state.stream_status_code == "failed":
            stream_state = "fault"
        self.hard_stream_status_label.setProperty("state", stream_state)
        self.hard_stream_status_label.style().unpolish(self.hard_stream_status_label)
        self.hard_stream_status_label.style().polish(self.hard_stream_status_label)
        self.hard_reason_value_label.setProperty("warning", not self._hard_status_state.write_allowed)
        self.hard_reason_value_label.style().unpolish(self.hard_reason_value_label)
        self.hard_reason_value_label.style().polish(self.hard_reason_value_label)
        detail_tooltip = (
            f"在线设备：{self.hard_online_value_label.text()}\n"
            f"目标设备：{self.hard_target_value_label.text()}\n"
            f"生效发送对象：{self.hard_send_value_label.text()}\n"
            f"控制写入：{self.hard_write_permission_label.text()}\n"
            f"控制说明：{self._hard_status_state.control_reason_text}\n"
            f"实时流：{self.hard_stream_status_label.text()}\n"
            f"实时流说明：{self._hard_status_state.stream_reason_text}\n"
            "提示：实时监测模式只允许自动启动实时流；其他写入仍需工程模式、权限和复核。"
        )
        self.hard_write_permission_label.setToolTip(detail_tooltip)
        self.hard_stream_status_label.setToolTip(detail_tooltip)
        self.session_header_detail_button.setToolTip(detail_tooltip)

    def _update_target_badge(self) -> None:
        target = self._current_target_id()
        is_broadcast = target == "FFF"
        online = self.latest_online_device_id or "--"
        active = ",".join(self.active_online_device_ids) if self.active_online_device_ids else "--"
        self.current_target_label.setText(
            f"当前目标设备 ID: {target}"
            + (" | 当前正在使用 FFF" if is_broadcast else "")
            + f" | 实时在线设备 ID: {online} | 活跃设备: {active}"
        )
        self.current_target_label.setProperty("broadcast", is_broadcast)
        self.current_target_label.style().unpolish(self.current_target_label)
        self.current_target_label.style().polish(self.current_target_label)
        self.broadcast_banner.setVisible(is_broadcast)
        self._update_hard_status_bar()
        self._refresh_task_entry_states()

    def _set_connection_state(self, state_key: str, text: str) -> None:
        self.connection_state_label.setText(text)
        self.connection_state_label.setProperty("state", state_key)
        self.connection_state_label.style().unpolish(self.connection_state_label)
        self.connection_state_label.style().polish(self.connection_state_label)

    def _update_status_strip(self, *, force_mode: str | None = None) -> None:
        current_mode = force_mode or self._current_session_mode()
        mode_map = {
            SESSION_MODE_LISTEN_ONLY: "会话模式: 严格只听",
            SESSION_MODE_SAFE_HANDSHAKE: "会话模式: 实时监测",
            SESSION_MODE_ENGINEERING: "会话模式: 工程联调",
            SESSION_MODE_REPLAY: "会话模式: 回放",
        }
        backend_text = "SIMULATOR" if self.port_combo.currentText().strip().upper() == "SIMULATOR" else f"REAL: {self.port_combo.currentText().strip()}"
        self.session_mode_status_label.setText(mode_map.get(current_mode, current_mode))
        self.backend_status_label.setText(f"后端: {backend_text}")
        self.lock_status_label.setText(f"严格只读: {'开' if self.read_only_lock_check.isChecked() else '关'}")
        self.simulator_banner.setVisible(self.port_combo.currentText().strip().upper() == "SIMULATOR")
        self._update_hard_status_bar()

    def _update_frame_age_status(self) -> None:
        if self.latest_frame_time is None:
            self.frame_age_label.setText("最新数据时延: --")
            return
        age_s = max(0.0, (datetime.now() - self.latest_frame_time).total_seconds())
        self.frame_age_label.setText(f"最新数据时延: {age_s:.2f}s")
        if self.last_frame is not None and not self.monitor_frozen and self._monitor_page_active():
            self._update_data_cards(self.last_frame)
        self._update_diagnostic_metrics()

    def _refresh_diagnostic_snapshot(self) -> None:
        self.diag_port_label.setText(self.port_combo.currentText().strip() or "--")
        self.diag_serial_label.setText(
            f"{self.baud_combo.currentText()} / {self.bytesize_combo.currentText()} / "
            f"{self.parity_combo.currentText()} / {self.stopbits_combo.currentText()}"
        )
        self.diag_connect_time_label.setText(self._fmt_ts(self.last_connect_time) if self.last_connect_time else "--")
        self.diag_last_frame_label.setText(self._fmt_ts(self.latest_frame_time) if self.latest_frame_time else "--")
        self.diag_last_serial_error_label.setText(self.last_serial_error)
        self.diag_last_timeout_label.setText(self.last_command_timeout)
        self.diag_last_disconnect_label.setText(self.last_disconnect_reason)

    def _update_anomaly_summary(self) -> None:
        if not self._monitor_page_active():
            return
        self.last_status_label.setText(self.last_status_anomaly)
        self.last_command_fail_label.setText(self.last_command_failure)
        self.last_serial_error_label.setText(self.last_serial_error)
        self.last_broadcast_label.setText(self.last_broadcast_command)

    def _update_diagnostic_metrics(self) -> None:
        now = time.monotonic()
        self._prune_metric_deque(self._rx_frame_times, now)
        self._prune_metric_deque(self._abnormal_frame_times, now)
        self._prune_metric_deque(self._cmd_success_times, now)
        self._prune_metric_deque(self._cmd_fail_times, now)
        self.diag_rx_1m_label.setText(str(len(self._rx_frame_times)))
        self.diag_abnormal_1m_label.setText(str(len(self._abnormal_frame_times)))
        self.diag_cmd_1m_label.setText(f"{len(self._cmd_success_times)} / {len(self._cmd_fail_times)}")
        self.diag_receive_hz_label.setText(f"{self.last_metrics.receive_hz:.1f} Hz" if self.last_metrics else "0.0 Hz")

    @staticmethod
    def _prune_metric_deque(values: deque[float], now: float) -> None:
        while values and (now - values[0]) > 60.0:
            values.popleft()

    @Slot(object)
    def _handle_frame(self, frame: ParsedFrame) -> None:
        had_frame_before = self.last_frame is not None
        timed_out_before = self._first_frame_wait_timed_out
        self.last_frame = frame
        self.latest_frame_time = frame.timestamp
        self.valid_frame_seen_since_connect = True
        self.last_valid_frame_at = frame.timestamp
        next_mode = f"MODE{frame.mode}"
        if next_mode != self._device_output_mode or not self._device_mode_confirmed:
            self._device_output_mode = next_mode
            self._device_mode_confirmed = True
            self._refresh_task_entry_states()
        self.latest_frame_label.setText(f"最近有效数据: {self._fmt_ts(frame.timestamp)}")
        self._rx_frame_times.append(time.monotonic())
        self._schedule_diagnostic_snapshot_refresh()
        self.first_frame_timer.stop()
        self._first_frame_wait_timed_out = False
        self._stream_runtime_state = "running"
        if self.connected and (self._waiting_for_first_stream_frame or not had_frame_before or timed_out_before):
            self._waiting_for_first_stream_frame = False
            self.chart_panel.reset_idle_placeholder()
            self._set_device_quick_status("已收到实时数据，图表与 KPI 正在更新。")
        if self.monitor_frozen:
            self._refresh_monitor_quick_controls()
            return
        self.chart_panel.add_frame(frame)
        if self._monitor_page_active():
            self._update_data_cards(frame)
            self.status_panel.update_frame(frame)
        self._refresh_monitor_quick_controls()

    @Slot(object)
    def _handle_raw(self, record: RawFrameRecord) -> None:
        if record.direction == "RX":
            self.raw_rx_seen_since_connect = True
            self.last_raw_rx_at = record.timestamp
            parse_mode = self._current_parse_mode()
            current_frame = YGasProtocol.parse_line(record.text, parse_mode=parse_mode)
            auto_frame = YGasProtocol.parse_line(record.text, parse_mode="AUTO")
            current_kind = YGasProtocol.classify_line(record.text, parse_mode=parse_mode)
            auto_kind = YGasProtocol.classify_line(record.text, parse_mode="AUTO")
            is_unparsed_telemetry = current_frame is None and (
                auto_frame is not None
                or auto_kind == "telemetry"
                or (not current_kind and str(record.text or "").strip().upper().startswith("YGAS,"))
            )
            if is_unparsed_telemetry:
                self.parse_failure_count_since_connect += 1
                self._abnormal_frame_times.append(time.monotonic())
                if not self.valid_frame_seen_since_connect:
                    self._set_device_quick_status(self._raw_rx_parse_failure_message())
                    self._set_monitor_idle_placeholder(self._raw_rx_parse_failure_message())
            elif not current_kind:
                self._abnormal_frame_times.append(time.monotonic())
        if record.direction == "TX" and ("[FFF]" in record.text or "target=FFF" in record.text):
            self.last_broadcast_command = f"{self._fmt_ts(record.timestamp)} | {record.text}"
            self._update_anomaly_summary()
        if not self.monitor_frozen:
            self.raw_frames.append_record(record)
        if record.direction in {"TX", "RX", "SYS", "REPLAY"}:
            self._queue_terminal_line(f"[{self._fmt_ts(record.timestamp)}] [{record.direction}] {record.text}")
        self._refresh_monitor_quick_controls()

    @staticmethod
    def _command_result_observation_suffix(result: CommandResult) -> str:
        details: list[str] = []
        if result.timeout_reason:
            details.append(f"timeout_reason={result.timeout_reason}")
        if result.observed_telemetry_count > 0:
            details.append(f"telemetry={result.observed_telemetry_count}")
        if result.observed_other_response_count > 0:
            details.append(f"other_response={result.observed_other_response_count}")
        if result.matched_response_line:
            details.append(f"matched={result.matched_response_line}")
        if not details:
            return ""
        return " | 观测：" + "；".join(details)

    def _quiet_read_result_suffix(self, result: CommandResult) -> str:
        if not result.original_auto_upload_state:
            return ""
        original_text = self._quiet_read_original_state_text(result.original_auto_upload_state)
        after_text = self._quiet_read_after_state_text(result.restore_result) or "未确认"
        return f" | 原状态：{original_text}；读取后状态：{after_text}"

    def _command_result_display_message(self, result: CommandResult) -> str:
        if result.action_type == "auto_silence_restore" and not result.ok:
            return (
                f"{result.message} 你可以重试恢复，或手动执行 SETCOMWAY=1。"
                f"{self._command_result_observation_suffix(result)}"
                f"{self._quiet_read_result_suffix(result)}"
            )
        if result.timeout_reason == "telemetry_data_ambiguous":
            return (
                "当前设备正在自动上传实时数据，READDATA 返回帧与自动流无法可靠区分。"
                "请直接查看实时数据，或先关闭主动上传后再执行 READDATA。"
                + self._command_result_observation_suffix(result)
                + self._quiet_read_result_suffix(result)
            )
        return f"{result.message}{self._command_result_observation_suffix(result)}{self._quiet_read_result_suffix(result)}"

    def _apply_pending_restore_retry_annotation(self, result: CommandResult) -> None:
        metadata = self._pending_restore_retry_requests.pop(result.command, None)
        if metadata is None:
            return
        result.action_type = "auto_silence_restore"
        result.action_label_zh = "恢复主动上传"
        result.parent_command = str(metadata.get("parent_command") or result.parent_command or "")
        result.source_page = "恢复主动上传"
        result.original_auto_upload_state = str(
            metadata.get("original_auto_upload_state") or result.original_auto_upload_state or "unknown"
        )
        result.restore_policy = str(metadata.get("restore_policy") or result.restore_policy or "restore_after_read")
        result.restore_attempted = True
        result.parsed_payload["manual_restore_retry"] = True
        if result.ok:
            result.auto_upload_state = "on"
            result.restore_result = "restored"
            result.message = "ACK 成功，已恢复主动上传。"
        else:
            result.auto_upload_state = "off"
            result.restore_result = "restore_failed"
            if "恢复主动上传失败" not in str(result.message or ""):
                result.message = f"恢复主动上传失败：{result.message}"

    def _update_restore_retry_candidate(self, result: CommandResult) -> None:
        if result.action_type != "auto_silence_restore":
            return
        payload = str(result.command or "").strip()
        if not payload:
            return
        if result.ok:
            self._restore_retry_candidates.pop(payload, None)
            return
        self._restore_retry_candidates[payload] = {
            "parent_command": str(result.parent_command or ""),
            "original_auto_upload_state": str(result.original_auto_upload_state or self._quiet_read_original_state or ""),
            "restore_policy": str(result.restore_policy or ""),
            "command_target_id": str(result.command_target_id or ""),
            "expected_device_id": str(result.expected_device_id or ""),
            "response_device_id": str(result.response_device_id or ""),
            "effective_scope": str(result.effective_scope or ""),
        }

    def _quiet_read_progress_update_for_result(self, result: CommandResult) -> str:
        if result.action_type == "auto_silence" and result.ok:
            return "正在读取；主动上传已临时暂停。"
        if result.action_type == "auto_silence_restore":
            if result.ok:
                if bool(result.parsed_payload.get("manual_restore_retry")):
                    return "已恢复主动上传。"
                return "正在恢复主动上传。"
            return "恢复主动上传失败，可重试恢复或保持关闭。"
        if not result.original_auto_upload_state:
            return ""
        after_text = self._quiet_read_after_state_text(result.restore_result)
        if not after_text:
            return ""
        return f"暂停上传后读取完成：{after_text}。"

    def _append_result_observation(self, text: str, result: CommandResult) -> str:
        suffix = self._command_result_observation_suffix(result)
        if not suffix:
            return text
        return f"{text}{suffix}"

    @Slot(object)
    def _handle_alarm(self, alarm: AlarmEvent) -> None:
        self.last_status_anomaly = f"{self._fmt_ts(alarm.timestamp)} | {alarm.message}"
        self._update_anomaly_summary()
        if not self.monitor_frozen and self._monitor_page_active():
            self.status_panel.append_alarm(alarm)

    @Slot(object)
    def _handle_metrics(self, metrics: MonitoringMetrics, from_signal: bool = True) -> None:
        self.last_metrics = metrics
        self._update_diagnostic_metrics()
        if self.monitor_frozen and from_signal:
            return
        if not self._monitor_page_active():
            return
        if self.last_frame is not None:
            self._update_data_cards(self.last_frame)
        self._update_monitor_metrics_cards(metrics)

    def _update_monitor_metrics_cards(self, metrics: MonitoringMetrics) -> None:
        items = [
            ("实际接收频率", f"{metrics.receive_hz:.1f}", "Hz", "最近 1 秒有效帧数", "normal"),
            ("最新数据时延", f"{metrics.latest_frame_age_s:.2f}" if metrics.latest_frame_age_s is not None else "--", "s", "越小越新", "warn" if (metrics.latest_frame_age_s or 0) > 1.5 else "normal"),
            ("丢帧计数", str(metrics.dropped_frames), "frames", "按期望频率估算", "warn" if metrics.dropped_frames else "normal"),
            ("5 秒稳定度", f"{metrics.stability_5s:.3f}" if metrics.stability_5s is not None else "--", "ppmσ", "CO2 5 秒标准差", "normal"),
            ("30 秒稳定度", f"{metrics.stability_30s:.3f}" if metrics.stability_30s is not None else "--", "ppmσ", "CO2 30 秒标准差", "normal"),
            ("腔温-壳温差", f"{metrics.temp_delta_c:.2f}" if metrics.temp_delta_c is not None else "--", "℃", "正值表示腔温更高", "warn" if abs(metrics.temp_delta_c or 0.0) > 2.0 else "normal"),
            ("滤波-原始偏差", f"{metrics.filter_bias:.5f}" if metrics.filter_bias is not None else "--", "ratio", "co2_ratio_f - co2_ratio_raw", "normal"),
        ]
        self.monitor_cards.update_items(items)

    @Slot(bool, str)
    def _handle_connection_state(self, connected: bool, log_path: str) -> None:
        self.connected = connected
        self.controller.connected = connected
        self.log_path_label.setText(f"日志文件: {log_path or '--'}")
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.reconnect_button.setEnabled(not connected)
        if connected:
            self._reset_stream_start_tracking()
            self._reset_stream_diagnostics()
            self.chart_panel.reset_idle_placeholder()
            self._clear_default_monitoring_checklist()
            self._clear_quiet_read_status()
            self._restore_retry_candidates.clear()
            self._pending_restore_retry_requests.clear()
            self.last_connect_time = datetime.now()
            self._reconnect_backoff_s = 2
            self._set_connection_state("connected", "已连接")
            self._disconnect_expected = False
            self._device_output_mode = self._current_parse_mode() or "AUTO"
            self._device_mode_confirmed = False
            self._device_auto_upload = self._current_acquisition_mode() == "LISTEN"
            self._set_auto_upload_state("unknown")
            self._default_config_scheduled = False
            self._default_config_scheduled_target_id = ""
            if self._should_auto_start_stream_after_connect():
                self._schedule_auto_start_stream()
            else:
                self._show_waiting_for_stream_hint(reason=self._planned_auto_start_stream_reason)
            if self.auto_apply_default_config_check.isChecked():
                self._schedule_default_monitoring_config()
        else:
            self._reset_stream_start_tracking()
            self._reset_stream_diagnostics()
            self._default_config_scheduled = False
            self._default_config_scheduled_target_id = ""
            self._clear_quiet_read_status()
            self._restore_retry_candidates.clear()
            self._pending_restore_retry_requests.clear()
            disconnect_reason = "连接断开"
            if self._disconnect_expected:
                disconnect_reason = "手动断开"
            recorded_unconfirmed = self._record_unconfirmed_default_monitoring_ack_only_pending(disconnect_reason)
            checklist_label = self._current_prepared_checklist_label()
            self._clear_default_monitoring_checklist(f"连接已断开，{checklist_label}已清空。")
            self._pending_readback_requests.clear()
            self._pending_write_verifications.clear()
            self.latest_online_device_id = ""
            self.active_online_device_ids = []
            self._device_mode_confirmed = False
            self._device_output_mode = self._current_parse_mode() or "AUTO"
            self._set_auto_upload_state("unknown")
            self.chart_panel.reset_idle_placeholder()
            if self._disconnect_expected:
                self.last_disconnect_reason = "手动断开"
                self._set_connection_state("disconnected", "未连接")
            elif self.replay_running:
                self._set_connection_state("replay", "正在回放")
            else:
                self._set_connection_state("fault", "串口异常断开")
                self._schedule_auto_reconnect()
            if recorded_unconfirmed > 0:
                self._set_control_common_feedback(
                    f"连接已断开；{recorded_unconfirmed} 条 ACK-only pending 已写入“未收到 ACK / 未确认”追溯。"
                )
                self._set_device_quick_status(
                    f"连接已断开；此前已发出的 ACK-only 步骤已按未确认结果保留追溯。"
                )
        if self.broadcast_check.isChecked() and not connected:
            self.broadcast_check.blockSignals(True)
            self.broadcast_check.setChecked(False)
            self.broadcast_check.blockSignals(False)
            self._update_target_badge()
        self._schedule_diagnostic_snapshot_refresh(immediate=True)
        self._apply_permission_mode()
        self._refresh_monitor_quick_controls()
        self._refresh_task_entry_states()

    @Slot(object)
    def _handle_command_result(self, result: CommandResult) -> None:
        self._apply_pending_restore_retry_annotation(result)
        self._update_restore_retry_candidate(result)
        state = "成功" if result.ok else "失败"
        quiet_read_result = self._sync_quiet_read_status_from_result(result)
        envelope = YGasProtocol.parse_command(result.command)
        if not quiet_read_result and result.action_type not in {"auto_silence", "auto_silence_restore"}:
            if envelope is not None and envelope.code == "SETCOMWAY":
                self._clear_quiet_read_status()
        display_message = self._command_result_display_message(result)
        self._record_command_log_event(result, display_message)
        if result.auto_upload_state:
            state_detail = display_message if (not result.ok and not quiet_read_result) else ""
            state_value = (
                "restore_failed"
                if result.action_type == "auto_silence_restore" and not result.ok
                else result.auto_upload_state
            )
            self._set_auto_upload_state(state_value, detail=state_detail)
        elif result.ok:
            if envelope is not None and envelope.code == "SETCOMWAY" and envelope.args[:1] == ["0"]:
                self._set_auto_upload_state("off")
            elif envelope is not None and envelope.code == "SETCOMWAY" and envelope.args[:1] == ["1"]:
                self._set_auto_upload_state("on")
            elif quiet_read_result:
                final_state = {
                    "restored": "on",
                    "kept_off": "off",
                    "preserved_existing_silence": "temporarily_silenced",
                }.get(str(result.restore_result or "").strip(), self._auto_upload_state)
                self._set_auto_upload_state(final_state)
        elif quiet_read_result:
            final_state = "restore_failed" if result.restore_result == "restore_failed" else self._auto_upload_state
            self._set_auto_upload_state(final_state)
        if envelope is not None and envelope.code == "SETCOMWAY" and not quiet_read_result:
            if envelope.args[:1] == ["1"]:
                self._stream_runtime_state = "running" if result.ok else "failed"
            elif envelope.args[:1] == ["0"] and result.ok:
                self._stream_runtime_state = "idle"
        self._handle_stream_start_result(result)
        if result.action_type in {"auto_silence", "auto_silence_restore"}:
            system_feedback = self._quiet_read_progress_update_for_result(result) or display_message
            panel_message = display_message if not result.ok else system_feedback
            self._record_auto_upload_system_change(result)
            self._set_control_common_feedback(system_feedback)
            self._set_device_quick_status(system_feedback)
            linked_command = result.parent_command or result.command
            linked_command_id = str(linked_command).split(",", 1)[0].replace("[FFF] ", "").strip().upper()
            if result.action_type == "auto_silence_restore" and not result.ok:
                if linked_command_id and self._panel_for_command_id(linked_command_id) is self.coeff_panel:
                    self._set_selected_coefficient_command(linked_command_id, expand_detail=True)
            for panel in self._command_panels():
                panel.set_last_response(linked_command, result.ok, panel_message, result=result)
            if self.custom_panel is not None:
                self.custom_panel.set_last_response(linked_command, result.ok, panel_message, result=result)
            self._queue_terminal_line(
                f"[{self._fmt_ts(result.timestamp)}] 系统动作[{self._command_log_result_text(result)}]: {result.command} | {display_message}"
            )
            return
        if result.action_type == "auto_start_stream":
            self._queue_terminal_line(
                f"[{self._fmt_ts(result.timestamp)}] 系统动作[{self._command_log_result_text(result)}]: {result.command} | {display_message}"
            )
        elif result.action_type == "manual_stream_start":
            self._queue_terminal_line(
                f"[{self._fmt_ts(result.timestamp)}] 监测页动作[{self._command_log_result_text(result)}]: {result.command} | {display_message}"
            )
        else:
            self._queue_terminal_line(f"[{self._fmt_ts(result.timestamp)}] 命令{state}: {result.command} | {display_message}")
        if result.ok:
            self._cmd_success_times.append(time.monotonic())
        else:
            self._cmd_fail_times.append(time.monotonic())
            self.last_command_failure = f"{self._fmt_ts(result.timestamp)} | {result.command} | {display_message}"
            self._update_anomaly_summary()
            self.status_panel.append_event("命令", self._fmt_ts(result.timestamp), f"{result.command} -> {display_message}")
            if "超时" in result.message:
                self.last_command_timeout = f"{self._fmt_ts(result.timestamp)} | {result.command}"
                self._schedule_diagnostic_snapshot_refresh()

        for panel in self._command_panels():
            panel.set_last_response(result.command, result.ok, display_message, result=result)
        if self.custom_panel is not None:
            self.custom_panel.set_last_response(result.command, result.ok, display_message, result=result)
        pending_readback = self._pending_readback_requests.pop(result.command, None)
        if pending_readback is not None:
            self._handle_pending_readback_result(pending_readback, result)
        pending_write = self._pending_write_verifications.pop(result.command, None)
        if pending_write is not None:
            self._handle_pending_write_result(pending_write, result)
        if not self._handle_default_monitoring_ack_only_result(result):
            self._handle_default_monitoring_step_direct_result(result)
        self._update_diagnostic_metrics()
        self._refresh_monitor_quick_controls()

    def _handle_pending_readback_result(self, request: dict[str, object], result: CommandResult) -> None:
        command_id = str(request.get("command_id") or "")
        phase = str(request.get("phase") or "manual")
        profile_name = str(request.get("profile_name") or self._current_profile_name())
        checklist_run_id = request.get("default_monitoring_run_id")
        if not isinstance(checklist_run_id, int):
            checklist_run_id = None
        checklist_kind = str(request.get("default_monitoring_kind") or "")
        checklist_step_id = str(request.get("default_monitoring_step_id") or "")
        checklist_planned_payload = str(request.get("default_monitoring_planned_payload") or "")
        checklist_source_page = str(request.get("default_monitoring_source_page") or "")
        if result.ok:
            self._apply_readback_result(command_id, result)
        if phase == "manual":
            self._last_manual_snapshot_status = {
                "ok": result.ok,
                "timestamp": result.timestamp,
                "device_id": str(result.response_device_id or self._current_target_id() or "--"),
                "command_id": command_id,
            }
            self._refresh_task_entry_states()
            return

        target_snapshot = request.get("target_snapshot")
        if not isinstance(target_snapshot, StructuredValueSnapshot):
            target_snapshot = StructuredValueSnapshot()

        if phase == "before_write":
            if not result.ok:
                report = WriteVerificationReport(
                    target=target_snapshot,
                    result_text="写前读取失败",
                    detail_text=f"未执行写入：{result.message}",
                )
                self._set_verification_report(command_id, report)
                self._record_session_change(
                    command_id,
                    report,
                    timestamp=result.timestamp,
                    target_device_id=str(result.response_device_id or self._current_target_id()),
                    source_page_override=checklist_source_page,
                    checklist_run_id=checklist_run_id,
                    checklist_kind=checklist_kind,
                    checklist_step_id=checklist_step_id,
                    checklist_planned_payload=checklist_planned_payload,
                    command_result=result,
                    command_payload=str(request.get("write_payload") or checklist_planned_payload or result.command),
                )
                return
            before_snapshot = self.registry.build_readback_snapshot(
                command_id,
                result.parsed_payload,
                profile_name=profile_name,
            )
            self._set_verification_report(
                command_id,
                WriteVerificationReport(
                    before=before_snapshot,
                    target=target_snapshot,
                    result_text="写入中",
                    detail_text="写前值已锁定，正在执行写命令。",
                ),
            )
            write_payload = str(request.get("write_payload") or "")
            write_expectation = str(request.get("write_expectation") or "ack")
            if write_payload:
                self._pending_write_verifications[write_payload] = {
                    "command_id": command_id,
                    "profile_name": profile_name,
                    "default_monitoring_run_id": checklist_run_id,
                    "default_monitoring_kind": checklist_kind,
                    "default_monitoring_step_id": checklist_step_id,
                    "default_monitoring_planned_payload": checklist_planned_payload,
                    "default_monitoring_source_page": checklist_source_page,
                    "before_snapshot": before_snapshot,
                    "target_snapshot": target_snapshot,
                    "write_payload": write_payload,
                    "post_read_target": str(request.get("post_read_target") or ""),
                }
                self.controller.send_payload(
                    write_payload,
                    expectation=write_expectation,
                    timeout_ms=self._current_command_timeout_ms(),
                )
            return

        if phase == "after_write":
            before_snapshot = request.get("before_snapshot")
            if not isinstance(before_snapshot, StructuredValueSnapshot):
                before_snapshot = StructuredValueSnapshot()
            if not result.ok:
                report = WriteVerificationReport(
                    before=before_snapshot,
                    target=target_snapshot,
                    result_text="无法验证",
                    detail_text=f"写命令 ACK 成功，但写后读取失败：{result.message}",
                )
                self._set_verification_report(command_id, report)
                self._record_session_change(
                    command_id,
                    report,
                    timestamp=result.timestamp,
                    target_device_id=str(result.response_device_id or self._current_target_id()),
                    source_page_override=checklist_source_page,
                    checklist_run_id=checklist_run_id,
                    checklist_kind=checklist_kind,
                    checklist_step_id=checklist_step_id,
                    checklist_planned_payload=checklist_planned_payload,
                    command_result=result,
                    command_payload=str(request.get("write_payload") or checklist_planned_payload or result.command),
                )
                return
            after_snapshot = self.registry.build_readback_snapshot(
                command_id,
                result.parsed_payload,
                profile_name=profile_name,
            )
            mismatches = self.registry.diff_structured_snapshots(target_snapshot, after_snapshot)
            detail_text = "写前、目标、写后已完成复核。"
            result_text = "一致"
            if mismatches:
                result_text = "不一致"
                detail_text = f"不一致项：{', '.join(mismatches)}"
            report = WriteVerificationReport(
                before=before_snapshot,
                target=target_snapshot,
                after=after_snapshot,
                result_text=result_text,
                detail_text=detail_text,
                verified_at=result.timestamp,
                source_device_id=str(result.response_device_id or "--"),
            )
            self._set_verification_report(command_id, report)
            self._record_session_change(
                command_id,
                report,
                timestamp=result.timestamp,
                target_device_id=str(result.response_device_id or self._current_target_id()),
                source_page_override=checklist_source_page,
                checklist_run_id=checklist_run_id,
                checklist_kind=checklist_kind,
                checklist_step_id=checklist_step_id,
                checklist_planned_payload=checklist_planned_payload,
                command_result=result,
                command_payload=str(request.get("write_payload") or checklist_planned_payload or result.command),
            )

    def _handle_pending_write_result(self, request: dict[str, object], result: CommandResult) -> None:
        command_id = str(request.get("command_id") or "")
        profile_name = str(request.get("profile_name") or self._current_profile_name())
        checklist_run_id = request.get("default_monitoring_run_id")
        if not isinstance(checklist_run_id, int):
            checklist_run_id = None
        checklist_kind = str(request.get("default_monitoring_kind") or "")
        checklist_step_id = str(request.get("default_monitoring_step_id") or "")
        checklist_planned_payload = str(request.get("default_monitoring_planned_payload") or "")
        checklist_source_page = str(request.get("default_monitoring_source_page") or "")
        before_snapshot = request.get("before_snapshot")
        if not isinstance(before_snapshot, StructuredValueSnapshot):
            before_snapshot = StructuredValueSnapshot()
        target_snapshot = request.get("target_snapshot")
        if not isinstance(target_snapshot, StructuredValueSnapshot):
            target_snapshot = StructuredValueSnapshot()

        if not result.ok:
            report = WriteVerificationReport(
                before=before_snapshot,
                target=target_snapshot,
                result_text="写入失败",
                detail_text=f"未执行写后复核：{result.message}",
            )
            self._set_verification_report(command_id, report)
            self._record_session_change(
                command_id,
                report,
                timestamp=result.timestamp,
                target_device_id=str(result.response_device_id or self._current_target_id()),
                source_page_override=checklist_source_page,
                checklist_run_id=checklist_run_id,
                checklist_kind=checklist_kind,
                checklist_step_id=checklist_step_id,
                checklist_planned_payload=checklist_planned_payload,
                command_result=result,
                command_payload=str(request.get("write_payload") or checklist_planned_payload or result.command),
            )
            return

        post_read_target = str(request.get("post_read_target") or "")
        readback_definition, readback_payload = self.registry.build_readback_preview(
            command_id,
            post_read_target,
            profile_name=profile_name,
        )
        self._set_verification_report(
            command_id,
            WriteVerificationReport(
                before=before_snapshot,
                target=target_snapshot,
                result_text="写后复核中",
                detail_text="ACK 成功，正在读取写后值。",
            ),
        )
        self._pending_readback_requests[readback_payload] = {
            "command_id": command_id,
            "phase": "after_write",
            "profile_name": profile_name,
            "default_monitoring_run_id": checklist_run_id,
            "default_monitoring_kind": checklist_kind,
            "default_monitoring_step_id": checklist_step_id,
            "default_monitoring_planned_payload": checklist_planned_payload,
            "default_monitoring_source_page": checklist_source_page,
            "before_snapshot": before_snapshot,
            "target_snapshot": target_snapshot,
            "write_payload": str(request.get("write_payload") or result.command),
        }
        self._remember_safe_command(readback_payload, readback_definition.return_type, f"{command_id} / 写后复核")
        self.controller.send_payload(
            readback_payload,
            expectation=readback_definition.return_type,
            timeout_ms=self._current_command_timeout_ms(),
        )

    def _apply_readback_result(self, command_id: str, result: CommandResult) -> None:
        if not result.ok:
            return
        normalized_command = str(command_id or "").strip().upper()
        if normalized_command == "MODE":
            mode_value = result.parsed_payload.get("mode")
            if mode_value not in (None, ""):
                self._device_output_mode = f"MODE{mode_value}"
                self._device_mode_confirmed = True
                self._refresh_task_entry_states()
        panel = self._panel_for_command_id(command_id)
        if panel is None:
            return
        payload = self.registry.adapt_readback_result(command_id, result.parsed_payload)
        panel.set_readback_result(
            command_id,
            current_value=str(payload.get("current_value") or "--"),
            timestamp_text=self._fmt_ts(result.timestamp),
            device_id=str(result.response_device_id or "--"),
            prefill_values={key: str(value) for key, value in dict(payload.get("prefill_values") or {}).items()},
        )
        self._ensure_coefficient_workspace_state()
        if normalized_command in self._coefficient_workspace_state:
            coeff_state = self._coefficient_workspace_state[normalized_command]
            coeff_state["current"] = str(payload.get("current_value") or coeff_state["current"] or "--")
            prefill_values = {key: str(value) for key, value in dict(payload.get("prefill_values") or {}).items()}
            coeff_state["target"] = str(prefill_values.get("coefficients") or coeff_state["target"] or "--")
            coeff_state["status"] = "已读取"
            coeff_state["note"] = "已读取当前值，可继续编辑目标值并执行写后复核。"
            self._refresh_coefficient_workspace_table()

    def _panel_for_command_id(self, command_id: str) -> CommandWorkspacePanel | None:
        normalized = str(command_id or "").strip().upper()
        for panel in self._command_panels():
            if any(item.command_id == normalized for item in panel.commands_in_panel()):
                return panel
        return None

    @Slot(object)
    def _handle_rx_device_state(self, state: dict[str, object]) -> None:
        self.latest_online_device_id = str(state.get("latest_rx_device_id") or "").upper()
        self.active_online_device_ids = [str(item).upper() for item in state.get("active_rx_device_ids", [])]
        for panel in self._command_panels():
            panel.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        if self.custom_panel is not None:
            self.custom_panel.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        self._update_target_badge()

    @Slot(str, str)
    def _handle_target_sync_requested(self, target_id: str, source: str) -> None:
        normalized = str(target_id or "").strip().upper()
        if not normalized:
            return
        existing = [self.target_combo.itemText(index) for index in range(self.target_combo.count())]
        if normalized not in existing:
            self.target_combo.addItem(normalized)
        if self._current_target_id() != normalized:
            self.target_combo.setCurrentText(normalized)
        self._queue_terminal_line(
            f"[{self._fmt_ts(datetime.now())}] [SYS] 检测到设备已切换为新 ID {normalized}（来源: {source}），已同步当前目标。"
        )

    @Slot(object)
    def _update_discovered_ids(self, device_ids: list[str]) -> None:
        current = self._current_target_id()
        self.discovered_label.setText("已发现设备: " + ", ".join(device_ids))
        existing = [self.target_combo.itemText(index) for index in range(self.target_combo.count())]
        for device_id in device_ids:
            if device_id not in existing:
                self.target_combo.addItem(device_id)
        if current in {"", "001"} and device_ids:
            self.target_combo.setCurrentText(device_ids[0])

    def _monitor_temperature_value(self, fields: dict[str, object]) -> object:
        for field_name in ("temperature_c", "chamber_temp_c", "case_temp_c"):
            if fields.get(field_name) is not None:
                return fields.get(field_name)
        return None

    def _build_primary_monitor_items(self, frame: ParsedFrame) -> list[tuple[object, ...]]:
        fields = frame.fields
        active_alarm_count = int(fields.get("active_alarm_count") or 0)
        status_severity = "alarm" if active_alarm_count > 0 else "normal"
        temperature_value = self._monitor_temperature_value(fields)
        status_text = f"报警 {active_alarm_count}" if active_alarm_count > 0 else ("正常" if frame.status else "--")
        status_detail = f"MODE{frame.mode} | 寄存器 {frame.status or '--'}"
        receive_hz_value = f"{self.last_metrics.receive_hz:.1f}" if self.last_metrics is not None else "--"
        return [
            ("CO2 浓度", self._fmt(fields.get("co2_ppm"), 3), "ppm", "实时主指标", "normal", "co2_ppm"),
            ("H2O 浓度", self._fmt(fields.get("h2o_mmol"), 3), "mmol/mol", "实时主指标", "normal", "h2o_mmol"),
            ("温度", self._fmt(temperature_value, 2), "℃", "当前主温度通道", "normal", "temperature_c"),
            ("压力", self._fmt(fields.get("pressure_kpa"), 2), "kPa", "当前腔压", "normal", "pressure_kpa"),
            ("状态 / 告警", status_text, "", status_detail, status_severity, "status_numeric"),
            ("实际接收频率", receive_hz_value, "Hz", "最近 1 秒有效帧数", "normal", ""),
        ]

    def _build_secondary_monitor_items(self, frame: ParsedFrame) -> list[tuple[object, ...]]:
        fields = frame.fields
        active_alarm_count = int(fields.get("active_alarm_count") or 0)
        status_severity = "alarm" if active_alarm_count > 0 else "normal"
        co2_delta = self._fmt_ratio_delta(
            fields.get("co2_ratio_raw"),
            fields.get("co2_ratio_f"),
            fallback=self.last_metrics.filter_bias if self.last_metrics is not None else None,
        )
        h2o_delta = self._fmt_ratio_delta(fields.get("h2o_ratio_raw"), fields.get("h2o_ratio_f"))
        return [
            ("CO2 密度", self._fmt(fields.get("co2_density"), 3), "mg/m³", "扩展浓度视角", "normal", "co2_density"),
            ("H2O 密度", self._fmt(fields.get("h2o_density"), 3), "g/m³", "扩展浓度视角", "normal", "h2o_density"),
            ("CO2 原始比值", self._fmt(fields.get("co2_ratio_raw"), 4), "", "raw", "normal", "co2_ratio_raw"),
            ("CO2 滤波比值", self._fmt(fields.get("co2_ratio_f"), 4), "", "filt", "normal", "co2_ratio_f"),
            ("CO2 差值", co2_delta, "ratio", "filt - raw", "normal", "co2_ratio_delta"),
            ("H2O 原始比值", self._fmt(fields.get("h2o_ratio_raw"), 4), "", "raw", "normal", "h2o_ratio_raw"),
            ("H2O 滤波比值", self._fmt(fields.get("h2o_ratio_f"), 4), "", "filt", "normal", "h2o_ratio_f"),
            ("H2O 差值", h2o_delta, "ratio", "filt - raw", "normal", "h2o_ratio_delta"),
        ]

    def _update_data_cards(self, frame: ParsedFrame) -> None:
        self.data_cards.update_items(self._build_primary_monitor_items(frame))
        self.detail_cards.update_items(self._build_secondary_monitor_items(frame))

    @staticmethod
    def _fmt_ratio_delta(raw_value: object, filtered_value: object, fallback: object | None = None) -> str:
        if isinstance(raw_value, (int, float)) and isinstance(filtered_value, (int, float)):
            delta = float(filtered_value) - float(raw_value)
            if math.isfinite(delta):
                return f"{delta:.5f}"
        if isinstance(fallback, (int, float)) and math.isfinite(float(fallback)):
            return f"{float(fallback):.5f}"
        return "--"

    def _handle_error_message(self, message: str) -> None:
        now_text = self._fmt_ts(datetime.now())
        self._append_info_message(message)
        if "超时" in message:
            self.last_command_timeout = f"{now_text} | {message}"
        if any(keyword in message for keyword in ["串口", "连接", "读取", "写入", "异常", "失败"]):
            self.last_serial_error = f"{now_text} | {message}"
            self.last_disconnect_reason = message
            self.status_panel.append_event("串口", now_text, message)
            if not self.connected and self.auto_reconnect_check.isChecked():
                self._schedule_auto_reconnect()
        self._schedule_diagnostic_snapshot_refresh()
        self._update_anomaly_summary()

    def _handle_info_message(self, message: str) -> None:
        self._append_info_message(message)
        if message.startswith("已连接"):
            self.last_connect_time = datetime.now()
            self._schedule_diagnostic_snapshot_refresh()

    def _append_info_message(self, message: str) -> None:
        self._queue_terminal_line(f"[{self._fmt_ts(datetime.now())}] [SYS] {message}")

    def _schedule_auto_reconnect(self) -> None:
        if not self.auto_reconnect_check.isChecked() or self.replay_running or self.connected:
            return
        if self.reconnect_timer.isActive():
            return
        delay_ms = int(self._reconnect_backoff_s * 1000)
        self._set_connection_state("connecting", f"自动重连 {self._reconnect_backoff_s}s")
        self.reconnect_timer.start(delay_ms)
        self._reconnect_backoff_s = min(15, self._reconnect_backoff_s * 2)

    def _auto_reconnect_tick(self) -> None:
        if self.connected or self.replay_running:
            return
        self._connect_session()

    def _update_replay_progress(self) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            self.replay_progress_bar.setValue(0)
            return
        percent = int(((self.replay_progress.value() + 1) / len(self.replay_dataset.frames)) * 100)
        self.replay_progress_bar.setValue(percent)

    def _update_replay_time_label(self) -> None:
        if not self.replay_dataset or not self.replay_dataset.frames:
            self.replay_time_label.setText("时间点: -- / -- | 倍率: 1x")
            return
        current_index = self.replay_progress.value()
        current_frame = self.replay_dataset.frames[current_index]
        start_time = self.replay_dataset.frames[0].timestamp
        current_s = (current_frame.timestamp - start_time).total_seconds()
        total_s = self.replay_dataset.total_duration_s
        self.replay_time_label.setText(f"时间点: {current_s:.1f}s / {total_s:.1f}s | 倍率: {self.speed_combo.currentText()}")

    def _command_panels(self) -> list[CommandWorkspacePanel]:
        return [self.control_panel, self.coeff_panel, self.signal_panel]

    def _current_profile_name(self) -> str:
        return str(self.profile_combo.currentData() or "bench_default")

    def _current_target_id(self) -> str:
        if not hasattr(self, "target_combo"):
            return "001"
        return self.target_combo.currentText().strip().upper() or "001"

    def _current_session_mode(self) -> str:
        return str(self.session_mode_combo.currentData() or SESSION_MODE_MONITORING)

    def _current_command_timeout_ms(self) -> int:
        return max(300, self.command_timeout_edit.value())

    def _families_for_command_ids(self, command_ids: set[str]) -> list[str]:
        return sorted({item.family for item in self.registry.all_commands(self._current_profile_name()) if item.command_id in command_ids})

    def _families_for_prefixes(self, prefixes: tuple[str, ...]) -> list[str]:
        return sorted({item.family for item in self.registry.all_commands(self._current_profile_name()) if item.command_id.startswith(prefixes)})

    @staticmethod
    def _fmt(value: object, digits: int) -> str:
        if isinstance(value, (int, float)):
            return f"{value:.{digits}f}"
        return "--"

    @staticmethod
    def _fmt_ts(value: datetime | None) -> str:
        if value is None:
            return "--"
        return value.strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _safe_int(text: str, default: int) -> int:
        try:
            return int(float(text))
        except Exception:
            return default
