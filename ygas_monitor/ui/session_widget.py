"""Single-session workstation UI."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import math
import time

from PySide6.QtCore import QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSlider,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..commanding.permissions import has_permission, permission_label
from ..commanding.registry import CommandDefinition, CommandRegistry
from ..commanding.safety import (
    SAFE_QUERY_COMMAND_IDS,
    SESSION_MODE_ENGINEERING,
    SESSION_MODE_LISTEN_ONLY,
    SESSION_MODE_REPLAY,
    SESSION_MODE_SAFE_HANDSHAKE,
    can_execute_command,
    is_read_only_command,
)
from ..config import EXPORT_DIR, LOG_DIR
from ..models import (
    AlarmEvent,
    CommandResult,
    ParsedFrame,
    RawFrameRecord,
    SessionConfig,
    SerialSettings,
    SessionWriteStatus,
    StructuredValueSnapshot,
    WriteVerificationReport,
)
from ..protocols.profiles import PROFILES, get_profile
from ..protocols.ygas import YGasProtocol
from ..serial.transport import list_serial_ports
from ..services.export_service import export_diagnostic_package, export_session_package
from ..services.metrics import MonitoringMetrics
from ..services.replay_service import ReplayDataset, load_replay_dataset
from ..services.session_controller import AnalyzerSessionController
from ..version import environment_summary_text
from .widgets.cards import MetricCardGrid
from .widgets.charts import RealtimeChartPanel
from .widgets.command_cards import CommandWorkspacePanel, CustomTemplateEditor
from .widgets.raw_frames import RawFramesWidget
from .widgets.status_panel import StatusPanel
from .theme_tokens import THEME_LABELS, THEME_OPTIONS


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
        self._device_action_queue: list[dict[str, object]] = []
        self._device_action_active: dict[str, object] | None = None
        self._default_config_scheduled = False
        self._device_output_mode = "MODE2"
        self._device_auto_upload = True
        self.latest_online_device_id = ""
        self.active_online_device_ids: list[str] = []
        self._pending_readback_requests: dict[str, dict[str, object]] = {}
        self._pending_write_verifications: dict[str, dict[str, object]] = {}
        self._monitor_aux_expanded = False
        self._monitor_aux_last_height = 140
        self._device_quick_status = "默认监测配置待连接后应用"
        self._hard_status_state = SessionWriteStatus()

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
        self.pages.setCurrentIndex(0)
        self._apply_permission_mode()
        self._update_target_badge()
        self._update_status_strip()
        self._schedule_diagnostic_snapshot_refresh()
        self._update_anomaly_summary()
        self._update_safe_history_combo()
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
            "command_timeout_ms": self.command_timeout_edit.text().strip() or "2000",
            "auto_reconnect": self.auto_reconnect_check.isChecked(),
            "read_only_lock": self.read_only_lock_check.isChecked(),
            "profile_name": self._current_profile_name(),
            "stream_hz": self.stream_hz_edit.text().strip() or "10",
            "poll_interval_ms": self.poll_interval_edit.text().strip() or "200",
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

    def _current_parse_mode(self) -> str:
        return str(self.mode_combo.currentData() or "AUTO")

    def _set_parse_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(str(mode or "AUTO"))
        if index < 0:
            index = 0
        self.mode_combo.setCurrentIndex(index)

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

        layout.addWidget(self._build_hard_status_bar())
        layout.addWidget(self._build_status_strip())

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
        self.connection_state_label = QLabel("未连接")
        self.session_mode_status_label = QLabel("会话模式: 只监听")
        self.backend_status_label = QLabel("后端: --")
        self.lock_status_label = QLabel("只读锁: 关")
        self.latest_frame_label = QLabel("最近有效数据: --")
        self.frame_age_label = QLabel("最新数据时延: --")
        for widget in [
            self.connection_state_label,
            self.session_mode_status_label,
            self.backend_status_label,
            self.lock_status_label,
            self.latest_frame_label,
            self.frame_age_label,
        ]:
            layout.addWidget(widget)
        layout.addStretch(1)
        return container

    def _build_hard_status_bar(self) -> QWidget:
        box = QGroupBox("会话写入基础状态")
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(6)

        self.hard_online_value_label = QLabel("--")
        self.hard_target_value_label = QLabel("--")
        self.hard_send_value_label = QLabel("--")
        self.hard_write_permission_label = QLabel("禁止")
        self.hard_reason_value_label = QLabel("--")
        self.hard_reason_value_label.setWordWrap(True)
        self.hard_status_help_label = QLabel(
            "该状态仅表示当前会话是否满足基础写入条件；具体命令仍需通过权限、风险和命令级校验。"
        )
        self.hard_status_help_label.setWordWrap(True)
        self.hard_status_help_label.setProperty("muted", True)
        self.hard_status_note_label = QLabel("当前选中命令仍会单独校验")
        self.hard_status_note_label.setProperty("muted", True)

        layout.addWidget(QLabel("在线设备"), 0, 0)
        layout.addWidget(self.hard_online_value_label, 0, 1)
        layout.addWidget(QLabel("会话目标"), 0, 2)
        layout.addWidget(self.hard_target_value_label, 0, 3)
        layout.addWidget(QLabel("生效发送"), 0, 4)
        layout.addWidget(self.hard_send_value_label, 0, 5)
        layout.addWidget(QLabel("基础写入条件"), 1, 0)
        layout.addWidget(self.hard_write_permission_label, 1, 1)
        layout.addWidget(QLabel("原因"), 1, 2)
        layout.addWidget(self.hard_reason_value_label, 1, 3, 1, 3)
        layout.addWidget(self.hard_status_help_label, 2, 0, 1, 5)
        layout.addWidget(self.hard_status_note_label, 2, 5, 1, 1)
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
        self._set_parse_mode("MODE2")
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
        self.stream_hz_edit = QLineEdit("10")
        self.poll_interval_edit = QLineEdit("200")
        self.command_timeout_edit = QLineEdit("2000")
        self.auto_reconnect_check = QCheckBox("自动重连")
        self.init_button = QPushButton("一键初始化采集")
        self.apply_default_config_button = QPushButton("应用默认监测配置")
        self.log_path_label = QLabel("日志文件: --")
        self.log_path_label.setProperty("muted", True)

        summary_grid.addWidget(self.auto_reconnect_check, 0, 0, 1, 2)
        summary_grid.addWidget(self.init_button, 0, 2, 1, 2)
        summary_grid.addWidget(self.apply_default_config_button, 1, 0, 1, 2)
        summary_grid.addWidget(self.log_path_label, 1, 2, 1, 2)

        self.advanced_connection_box = QGroupBox("高级串口与采集设置")
        self.advanced_connection_box.setCheckable(True)
        self.advanced_connection_box.setChecked(False)
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
        advanced_grid.addWidget(QLabel("主动上报频率(Hz)"), 2, 0)
        advanced_grid.addWidget(self.stream_hz_edit, 2, 1)
        advanced_grid.addWidget(QLabel("轮询间隔(ms)"), 2, 2)
        advanced_grid.addWidget(self.poll_interval_edit, 2, 3)
        advanced_grid.addWidget(QLabel("命令超时(ms)"), 3, 0)
        advanced_grid.addWidget(self.command_timeout_edit, 3, 1)
        advanced_box_layout.addWidget(self.advanced_connection_content)

        layout.addLayout(summary_grid)
        layout.addWidget(self.advanced_connection_box)
        return box

    def _build_workspace_box(self) -> QWidget:
        box = QGroupBox("联调安全上下文")
        layout = QFormLayout(box)
        box.setTitle("设备模式与权限")
        self.permission_combo = QComboBox()
        self.permission_combo.addItems(["READ_ONLY", "CONFIG", "CALIBRATION", "EXPERT"])
        self.permission_combo.setCurrentText("CONFIG")
        self.session_mode_combo = QComboBox()
        self.session_mode_combo.addItem("只监听（绝不发命令）", SESSION_MODE_LISTEN_ONLY)
        self.session_mode_combo.addItem("安全握手（仅查询类命令）", SESSION_MODE_SAFE_HANDSHAKE)
        self.session_mode_combo.addItem("工程模式（允许正式控制命令）", SESSION_MODE_ENGINEERING)
        self.session_mode_combo.setCurrentIndex(2)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["001"])
        self.broadcast_check = QCheckBox("启用广播地址 FFF")
        self.read_only_lock_check = QCheckBox("只读会话锁")
        self.show_expert_check = QCheckBox("显示串口助手")
        self.show_expert_check.setChecked(True)
        self.show_expert_check.hide()
        self.current_target_label = QLabel("当前目标设备 ID: 001")
        self.current_target_label.setProperty("broadcast", False)
        self.profile_hint_label = QLabel(get_profile("bench_default").description)
        self.profile_hint_label.setWordWrap(True)
        self.profile_hint_label.setProperty("muted", True)
        self.discovered_label = QLabel("已发现设备: --")
        self.discovered_label.setWordWrap(True)
        self.discovered_label.setProperty("muted", True)

        layout.addRow("权限等级", self.permission_combo)
        layout.addRow("联调模式", self.session_mode_combo)
        layout.addRow("目标设备 ID", self.target_combo)
        layout.addRow("", self.broadcast_check)
        layout.addRow("", self.read_only_lock_check)
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
        box = QGroupBox("界面外观（全局）")
        layout = QFormLayout(box)
        self.theme_combo = QComboBox()
        for theme_key in THEME_OPTIONS:
            self.theme_combo.addItem(THEME_LABELS[theme_key], theme_key)
        theme_hint = QLabel("界面主题为全局设置，也可直接在主工具栏中的“界面主题”菜单切换。")
        theme_hint.setProperty("muted", True)
        theme_hint.setWordWrap(True)
        layout.addRow("全局主题", self.theme_combo)
        layout.addRow("", theme_hint)
        return box

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(self._build_quick_connect_box())
        left_layout.addWidget(self._build_connection_box())
        left_layout.addWidget(self._build_workspace_box())
        left_layout.addStretch(1)

        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self._build_diagnostic_box())
        right_layout.addWidget(self._build_ui_preferences_box())
        right_layout.addStretch(1)

        content_layout.addWidget(left_column, 5)
        content_layout.addWidget(right_column, 4)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        return page

    def _build_monitor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        self.freeze_button = QPushButton("监测冻结")
        self.freeze_button.setCheckable(True)
        self.monitor_connection_badge = QLabel("未连接")
        controls.addWidget(self.freeze_button)
        self.monitor_connection_badge.setProperty("state", "disconnected")
        self.monitor_mode_label = QLabel("MODE2")
        self.monitor_mode_label.setProperty("accent", True)
        self.mode1_quick_button = QPushButton("MODE1")
        self.mode2_quick_button = QPushButton("MODE2")
        self.auto_upload_quick_button = QPushButton("自动上传")
        self.monitor_quick_status_label = QLabel(self._device_quick_status)
        self.monitor_quick_status_label.setProperty("muted", True)
        self.monitor_quick_status_label.setToolTip("显示模式切换、自动上传和图槽切换的最近状态。")
        controls.addWidget(self.monitor_connection_badge)
        controls.addWidget(self.monitor_mode_label)
        controls.addWidget(self.mode1_quick_button)
        controls.addWidget(self.mode2_quick_button)
        controls.addWidget(self.auto_upload_quick_button)
        controls.addStretch(1)
        controls.addWidget(self.monitor_quick_status_label)
        layout.addLayout(controls)

        self.data_cards = MetricCardGrid(rows=2, columns=6, dense=True)
        self.data_cards.setMinimumHeight(184)
        self.data_cards.setMaximumHeight(226)
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
        self.chart_panel.setMinimumHeight(420)
        self.chart_config_group = self._build_chart_config_box()
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
        self.monitor_aux_toggle_button = QPushButton("展开辅助区")
        self.monitor_aux_toggle_button.setCheckable(True)
        self.monitor_aux_toggle_button.setToolTip("展开后可查看状态面板、扩展数据、原始帧和异常摘要。")
        monitor_aux_header.addWidget(monitor_aux_title)
        monitor_aux_header.addStretch(1)
        monitor_aux_header.addWidget(self.monitor_aux_toggle_button)
        monitor_aux_layout.addLayout(monitor_aux_header)
        monitor_aux_layout.addWidget(self.monitor_aux_tabs, 1)

        self.monitor_body_split = QSplitter(Qt.Vertical)
        self.monitor_body_split.setChildrenCollapsible(False)
        self.monitor_body_split.addWidget(self.chart_panel)
        self.monitor_body_split.addWidget(self.monitor_aux_container)
        self.monitor_body_split.setSizes([720, 44])

        layout.addWidget(self.chart_config_group)
        layout.addWidget(self.data_cards)
        layout.addWidget(self.monitor_body_split, 1)
        self._set_monitor_aux_expanded(False)
        self._refresh_chart_config_summary()
        return page

    def _build_chart_config_box(self) -> QWidget:
        box = QGroupBox("图表配置")
        layout = QGridLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        self.chart_view_combo = QComboBox()
        for view_id, title in self.chart_panel.available_views():
            self.chart_view_combo.addItem(title, view_id)
        self.chart_add_upper_button = QPushButton("加入上图")
        self.chart_add_lower_button = QPushButton("加入下图")
        self.chart_remove_upper_button = QPushButton("从上图移除")
        self.chart_remove_lower_button = QPushButton("从下图移除")
        self.chart_restore_defaults_button = QPushButton("恢复默认视图")
        self.chart_upper_summary_label = QLabel("--")
        self.chart_upper_summary_label.setWordWrap(True)
        self.chart_lower_summary_label = QLabel("--")
        self.chart_lower_summary_label.setWordWrap(True)

        layout.addWidget(QLabel("可选视图"), 0, 0)
        layout.addWidget(self.chart_view_combo, 0, 1, 1, 2)
        layout.addWidget(self.chart_add_upper_button, 0, 3)
        layout.addWidget(self.chart_add_lower_button, 0, 4)
        layout.addWidget(self.chart_remove_upper_button, 0, 5)
        layout.addWidget(self.chart_remove_lower_button, 0, 6)
        layout.addWidget(self.chart_restore_defaults_button, 0, 7)
        layout.addWidget(QLabel("上图"), 1, 0)
        layout.addWidget(self.chart_upper_summary_label, 1, 1, 1, 3)
        layout.addWidget(QLabel("下图"), 1, 4)
        layout.addWidget(self.chart_lower_summary_label, 1, 5, 1, 3)
        return box

    def _build_control_page(self) -> QWidget:
        self.control_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_command_ids(self.CONTROL_COMMANDS),
            profile_name=self._current_profile_name(),
        )
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.control_panel)
        return page

    def _build_coeff_page(self) -> QWidget:
        self.coeff_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_prefixes(self.COEFFICIENT_PREFIXES),
            profile_name=self._current_profile_name(),
        )
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.coeff_panel)
        return page

    def _build_signal_page(self) -> QWidget:
        self.signal_panel = CommandWorkspacePanel(
            self.registry,
            self._families_for_command_ids(self.SIGNAL_COMMANDS),
            profile_name=self._current_profile_name(),
        )
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.signal_panel)
        return page

    def _build_expert_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

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
        return page

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
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        export_box = QGroupBox("正式导出")
        export_layout = QHBoxLayout(export_box)
        self.export_now_button = QPushButton("导出当前数据 CSV")
        self.export_session_button = QPushButton("导出本次会话包")
        self.export_diag_page_button = QPushButton("导出诊断包")
        self.open_export_button = QPushButton("打开导出目录")
        export_layout.addWidget(self.export_now_button)
        export_layout.addWidget(self.export_session_button)
        export_layout.addWidget(self.export_diag_page_button)
        export_layout.addWidget(self.open_export_button)
        export_layout.addStretch(1)

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
        layout.addWidget(replay_box, 1)
        return page

    def _connect_signals(self) -> None:
        self.refresh_ports_button.clicked.connect(lambda: self._refresh_ports(force=False))
        self.demo_button.clicked.connect(self._activate_demo_mode)
        self.connect_button.clicked.connect(self._connect_session)
        self.disconnect_button.clicked.connect(self._disconnect_session)
        self.reconnect_button.clicked.connect(self._manual_reconnect)
        self.safe_handshake_button.clicked.connect(self._run_safe_handshake)
        self.init_button.clicked.connect(self.controller.initialize_capture)
        self.open_log_dir_button.clicked.connect(self._open_log_dir)
        self.copy_env_button.clicked.connect(self._copy_environment_info)
        self.resend_history_button.clicked.connect(self._resend_history_command)

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
        self.mode1_quick_button.clicked.connect(lambda: self._request_quick_output_mode("MODE1"))
        self.mode2_quick_button.clicked.connect(lambda: self._request_quick_output_mode("MODE2"))
        self.auto_upload_quick_button.clicked.connect(self._toggle_quick_auto_upload)
        self.apply_default_config_button.clicked.connect(self._apply_default_monitoring_config)
        self.chart_add_upper_button.clicked.connect(lambda: self._apply_chart_view_to_slot(0))
        self.chart_add_lower_button.clicked.connect(lambda: self._apply_chart_view_to_slot(1))
        self.chart_remove_upper_button.clicked.connect(lambda: self._clear_chart_slot(0))
        self.chart_remove_lower_button.clicked.connect(lambda: self._clear_chart_slot(1))
        self.chart_restore_defaults_button.clicked.connect(self._restore_chart_defaults)
        self.theme_combo.currentIndexChanged.connect(self._handle_theme_changed)
        self.pages.currentChanged.connect(self._handle_page_changed)
        self.monitor_aux_tabs.currentChanged.connect(self._handle_monitor_aux_tab_changed)
        self.monitor_aux_toggle_button.toggled.connect(self._set_monitor_aux_expanded)
        self.monitor_body_split.splitterMoved.connect(self._handle_monitor_aux_splitter_moved)

        for panel in self._command_panels():
            panel.command_requested.connect(self._handle_command_request)
            panel.readback_requested.connect(self._handle_readback_request)
            panel.read_page_button.clicked.connect(lambda _=False, p=panel: self._read_panel_defaults(p))

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
            self.monitor_aux_container.setMaximumHeight(16777215)
            self.monitor_aux_tabs.show()
            self._monitor_aux_expanded = True
            self.monitor_aux_toggle_button.blockSignals(True)
            self.monitor_aux_toggle_button.setChecked(True)
            self.monitor_aux_toggle_button.setText("收起辅助区")
            self.monitor_aux_toggle_button.blockSignals(False)
            QTimer.singleShot(0, lambda: self._apply_monitor_aux_height(self._monitor_aux_last_height))
            current_index = self.monitor_aux_tabs.currentIndex()
            if current_index >= 0:
                self._handle_monitor_aux_tab_changed(current_index)
            return

        current_sizes = self.monitor_body_split.sizes()
        if len(current_sizes) > 1 and current_sizes[1] > 80:
            self._monitor_aux_last_height = current_sizes[1]
        self._monitor_aux_expanded = False
        self.monitor_aux_toggle_button.blockSignals(True)
        self.monitor_aux_toggle_button.setChecked(False)
        self.monitor_aux_toggle_button.setText("展开辅助区")
        self.monitor_aux_toggle_button.blockSignals(False)
        self.monitor_aux_tabs.hide()
        self.monitor_aux_container.setMaximumHeight(52)
        QTimer.singleShot(0, lambda: self._apply_monitor_aux_height(44))

    def _apply_monitor_aux_height(self, desired_height: int) -> None:
        total = sum(self.monitor_body_split.sizes())
        if total <= 0:
            total = max(self.monitor_body_split.height(), 400)
        if self._monitor_aux_expanded:
            bottom = max(120, min(int(desired_height or 140), max(120, total - 220)))
        else:
            bottom = 44
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

    def _build_device_payload(self, command_id: str, values: dict[str, str]) -> tuple[str, str]:
        definition = self._device_command_definition(command_id)
        target_id = self.registry.default_target_for_command(command_id, self._current_target_id())
        payload = self.registry.build_preview(
            command_id,
            target_id,
            values,
            profile_name=self._current_profile_name(),
        )
        return payload, definition.return_type

    def _can_run_quick_device_action(self, *, show_message: bool) -> bool:
        if self.replay_running:
            if show_message:
                QMessageBox.warning(self, "回放中", "回放期间不能向真实设备发送模式或自动上传切换命令。")
            return False
        if not self.connected:
            if show_message:
                QMessageBox.warning(self, "未连接", "请先连接设备，再执行快速配置。")
            return False
        if self.read_only_lock_check.isChecked():
            if show_message:
                QMessageBox.warning(self, "只读锁已开启", "请先关闭只读会话锁，再执行快速配置。")
            return False
        if self._current_session_mode() == SESSION_MODE_LISTEN_ONLY:
            if show_message:
                QMessageBox.warning(self, "当前为只监听模式", "请在“连接与设备模式”页切换到工程模式后再执行快速配置。")
            return False
        if self._current_session_mode() == SESSION_MODE_SAFE_HANDSHAKE:
            if show_message:
                QMessageBox.warning(self, "当前为安全握手模式", "请切换到工程模式后再执行快速配置。")
            return False
        if self.permission_combo.currentText() not in {"CONFIG", "CALIBRATION", "EXPERT"}:
            if show_message:
                QMessageBox.warning(self, "权限不足", "快速配置至少需要 CONFIG 权限。")
            return False
        return True

    def _set_device_quick_status(self, text: str) -> None:
        self._device_quick_status = text
        if hasattr(self, "monitor_quick_status_label"):
            self.monitor_quick_status_label.setText(text)

    def _refresh_monitor_quick_controls(self) -> None:
        inferred_mode = f"MODE{self.last_frame.mode}" if self.last_frame is not None else self._device_output_mode
        inferred_upload = self._device_auto_upload
        connection_text = "已连接" if self.connected else ("回放中" if self.replay_running else "未连接")
        connection_state = "connected" if self.connected else ("replay" if self.replay_running else "disconnected")
        self.monitor_connection_badge.setText(connection_text)
        self.monitor_connection_badge.setProperty("state", connection_state)
        self.monitor_connection_badge.style().unpolish(self.monitor_connection_badge)
        self.monitor_connection_badge.style().polish(self.monitor_connection_badge)
        self.monitor_mode_label.setText(inferred_mode)
        self.auto_upload_quick_button.setText(f"自动上传：{'开' if inferred_upload else '关'}")

        busy = self._device_action_active is not None
        quick_allowed = self._can_run_quick_device_action(show_message=False) and not busy
        self.mode1_quick_button.setEnabled(quick_allowed)
        self.mode2_quick_button.setEnabled(quick_allowed)
        self.auto_upload_quick_button.setEnabled(quick_allowed)
        self.apply_default_config_button.setEnabled(self.connected and not self.replay_running and not busy)

        self.mode1_quick_button.setProperty("accent", inferred_mode == "MODE1")
        self.mode2_quick_button.setProperty("accent", inferred_mode == "MODE2")
        for button in [self.mode1_quick_button, self.mode2_quick_button]:
            button.style().unpolish(button)
            button.style().polish(button)

    def _enqueue_device_action(self, action: dict[str, object]) -> None:
        self._device_action_queue.append(action)
        if self._device_action_active is None:
            self._dispatch_next_device_action()

    def _dispatch_next_device_action(self) -> None:
        if self._device_action_active is not None or not self._device_action_queue:
            return
        self._device_action_active = self._device_action_queue.pop(0)
        self._dispatch_current_device_action_step()

    def _dispatch_current_device_action_step(self) -> None:
        if self._device_action_active is None:
            return
        steps = self._device_action_active.get("steps", [])
        if not steps:
            self._finish_device_action(ok=False, message="设备操作缺少可执行步骤。")
            return
        step = steps[0]
        self._set_device_quick_status(str(self._device_action_active["start_text"]))
        self._refresh_monitor_quick_controls()
        self.controller.update_config(self._build_config())
        self.controller.send_payload(str(step["payload"]), expectation=str(step["expectation"]), timeout_ms=self._current_command_timeout_ms())

    def _finish_device_action(self, *, ok: bool, message: str) -> None:
        action = self._device_action_active
        self._device_action_active = None
        if ok and action is not None:
            if action.get("mode_preference"):
                self._set_parse_mode(str(action["mode_preference"]))
                self._device_output_mode = str(action["mode_preference"])
            if action.get("acquisition_mode"):
                self._set_acquisition_mode(str(action["acquisition_mode"]))
                self._device_auto_upload = str(action["acquisition_mode"]) == "LISTEN"
                self.controller.update_config(self._build_config())
        self._set_device_quick_status(message)
        self._refresh_monitor_quick_controls()
        if self._device_action_queue:
            QTimer.singleShot(0, self._dispatch_next_device_action)

    def _request_quick_output_mode(self, mode_name: str) -> None:
        if not self._can_run_quick_device_action(show_message=True):
            return
        mode_value = "1" if mode_name == "MODE1" else "2"
        payload, expectation = self._build_device_payload("MODE", {"mode": mode_value})
        self._enqueue_device_action(
            {
                "action_id": f"quick-mode-{mode_name}",
                "steps": [{"payload": payload, "expectation": expectation}],
                "mode_preference": mode_name,
                "start_text": f"正在切换到 {mode_name}...",
                "success_text": f"设备已切换到 {mode_name}。",
                "failure_text": f"{mode_name} 切换失败",
            }
        )

    def _toggle_quick_auto_upload(self) -> None:
        if not self._can_run_quick_device_action(show_message=True):
            return
        target_listen = not self._device_auto_upload
        self._enqueue_stream_mode_action(enable_auto_upload=target_listen, source="quick")

    def _enqueue_stream_mode_action(self, *, enable_auto_upload: bool, source: str) -> None:
        steps: list[dict[str, str]] = []
        if enable_auto_upload:
            ftd_payload, ftd_expectation = self._build_device_payload("FTD", {"hz": self.stream_hz_edit.text().strip() or "10"})
            steps.append({"payload": ftd_payload, "expectation": ftd_expectation})
        payload, expectation = self._build_device_payload("SETCOMWAY", {"mode": "1" if enable_auto_upload else "0"})
        steps.append({"payload": payload, "expectation": expectation})
        self._enqueue_device_action(
            {
                "action_id": f"stream-{source}",
                "steps": steps,
                "acquisition_mode": "LISTEN" if enable_auto_upload else "POLL",
                "start_text": "正在切换自动上传..." if enable_auto_upload else "正在切换到手动读取...",
                "success_text": "自动上传已开启。" if enable_auto_upload else "自动上传已关闭，当前为手动读取。",
                "failure_text": "自动上传切换失败",
            }
        )

    def _apply_default_monitoring_config(self, *, show_message: bool = True) -> None:
        if not self._can_run_quick_device_action(show_message=show_message):
            self._set_device_quick_status("默认监测配置未执行：当前会话限制发送控制命令")
            self._refresh_monitor_quick_controls()
            return
        mode_payload, mode_expectation = self._build_device_payload("MODE", {"mode": "2"})
        ftd_payload, ftd_expectation = self._build_device_payload("FTD", {"hz": self.stream_hz_edit.text().strip() or "10"})
        way_payload, way_expectation = self._build_device_payload("SETCOMWAY", {"mode": "1"})
        self._enqueue_device_action(
            {
                "action_id": "default-monitoring-config",
                "steps": [
                    {"payload": mode_payload, "expectation": mode_expectation},
                    {"payload": ftd_payload, "expectation": ftd_expectation},
                    {"payload": way_payload, "expectation": way_expectation},
                ],
                "mode_preference": "MODE2",
                "acquisition_mode": "LISTEN",
                "start_text": "默认监测配置进行中...",
                "success_text": "默认监测配置成功：MODE2 + 自动上传。",
                "failure_text": "默认监测配置失败",
            }
        )

    def _schedule_default_monitoring_config(self) -> None:
        if self._default_config_scheduled or not self.connected or self.replay_running:
            return
        self._default_config_scheduled = True
        self._set_device_quick_status("连接成功，准备应用默认监测配置...")
        self._refresh_monitor_quick_controls()
        QTimer.singleShot(200, lambda: self._apply_default_monitoring_config(show_message=False))

    def _handle_metric_card_slot_request(self, metric_key: str, slot_index: int) -> None:
        if self.chart_panel.set_slot_metric(slot_index, metric_key):
            slot_name = "上图" if slot_index == 0 else "下图"
            label = self.chart_panel.friendly_metric_name(metric_key)
            self._set_device_quick_status(f"已将 {label} 切换到{slot_name}。")
        else:
            self._set_device_quick_status("该指标当前没有可用的快捷图槽视图。")
        self._refresh_chart_config_summary()
        self._refresh_monitor_quick_controls()

    def _apply_chart_view_to_slot(self, slot_index: int) -> None:
        view_id = str(self.chart_view_combo.currentData() or "")
        if not view_id:
            return
        slot_name = "上图" if slot_index == 0 else "下图"
        if self.chart_panel.set_slot_view(slot_index, view_id):
            self._set_device_quick_status(f"已将 {self.chart_view_combo.currentText()} 加入{slot_name}。")
            self._refresh_chart_config_summary()

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

    def _apply_persisted_state(self, state: dict) -> None:
        if not state:
            return
        self.port_combo.setCurrentText(str(state.get("port", "SIMULATOR")))
        self.baud_combo.setCurrentText(str(state.get("baudrate", "115200")))
        self.bytesize_combo.setCurrentText(str(state.get("bytesize", "8")))
        self.parity_combo.setCurrentText(str(state.get("parity", "N")))
        self.stopbits_combo.setCurrentText(str(state.get("stopbits", "1")))
        self._set_acquisition_mode(str(state.get("acquisition_mode", "LISTEN")))
        self._set_parse_mode(str(state.get("mode_preference", "MODE2")))
        session_mode = state.get("session_mode")
        if not session_mode and state.get("listen_only", False):
            session_mode = SESSION_MODE_LISTEN_ONLY
        index = self.session_mode_combo.findData(session_mode or SESSION_MODE_ENGINEERING)
        self.session_mode_combo.setCurrentIndex(max(0, index))
        self.command_timeout_edit.setText(str(state.get("command_timeout_ms", "2000")))
        self.auto_reconnect_check.setChecked(bool(state.get("auto_reconnect", False)))
        self.read_only_lock_check.setChecked(bool(state.get("read_only_lock", False)))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(state.get("profile_name", "bench_default"))))
        self.stream_hz_edit.setText(str(state.get("stream_hz", "10")))
        self.poll_interval_edit.setText(str(state.get("poll_interval_ms", "200")))
        self.permission_combo.setCurrentText(str(state.get("permission_level", "CONFIG")))
        self.show_expert_check.setChecked(True)
        self.target_combo.setCurrentText(str(state.get("target_id", "001")))
        self.session_note_edit.setText(str(state.get("session_note", "")))
        self.last_export_dir = str(state.get("last_export_dir") or EXPORT_DIR)
        self.last_replay_file = str(state.get("last_replay_file") or "")
        self._monitor_aux_last_height = max(120, int(state.get("monitor_aux_height", 140) or 140))
        self._set_monitor_aux_expanded(bool(state.get("monitor_aux_expanded", False)))

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
            stream_hz=max(1, self._safe_int(self.stream_hz_edit.text(), 10)),
            poll_interval_ms=max(50, self._safe_int(self.poll_interval_edit.text(), 200)),
            command_timeout_ms=max(300, self._safe_int(self.command_timeout_edit.text(), 2000)),
            auto_reconnect=self.auto_reconnect_check.isChecked(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            session_note=self.session_note_edit.text().strip(),
            profile_name=self._current_profile_name(),
            target_id=self._current_target_id(),
            permission_level=self.permission_combo.currentText(),
            session_name=self.session_name,
        )

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
            QMessageBox.warning(self, "会话模式限制", "只监听模式禁止发送命令。请切换到“安全握手”或“工程模式”。")
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
        self._set_parse_mode("MODE2")
        self.permission_combo.setCurrentText("CONFIG")
        engineering_index = self.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
        if engineering_index >= 0:
            self.session_mode_combo.setCurrentIndex(engineering_index)
        self.baud_combo.setCurrentText("115200")
        self._device_output_mode = "MODE2"
        self._device_auto_upload = True
        self._set_device_quick_status("演示模式已准备为 MODE2 + 自动上传。")
        self._refresh_monitor_quick_controls()
        self._append_info_message("已切换为演示模式，端口使用 SIMULATOR。")
        self._update_status_strip()

    def _handle_command_request(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        preview: str,
        effective_target: str,
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
        mismatch_message = self._online_target_mismatch_message(definition, effective_target)
        if mismatch_message:
            QMessageBox.warning(self, "目标设备不一致", mismatch_message)
            return
        if definition.risk_level.lower() in {"medium", "high", "critical"} or effective_target == "FFF":
            if not self._confirm_high_risk_command(definition, preview):
                return

        self.controller.update_config(self._build_config())
        if is_read_only_command(definition):
            self._remember_safe_command(preview, definition.return_type, definition.display_name)
            self.controller.send_payload(preview, expectation=definition.return_type, timeout_ms=self._current_command_timeout_ms())
            return
        if self._should_auto_verify_write(definition):
            self._start_write_verification(definition, values, preview)
            return
        self.controller.send_payload(preview, expectation=definition.return_type, timeout_ms=self._current_command_timeout_ms())

    def _handle_readback_request(self, definition: CommandDefinition) -> None:
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
        self.controller.send_payload(
            payload,
            expectation=readback_definition.return_type,
            timeout_ms=self._current_command_timeout_ms(),
        )

    def _should_auto_verify_write(self, definition: CommandDefinition) -> bool:
        return self.registry.is_write_command_id(definition.command_id) and self.registry.supports_readback(definition.command_id)

    def _start_write_verification(
        self,
        definition: CommandDefinition,
        values: dict[str, str],
        write_payload: str,
    ) -> None:
        read_target, target_error = self._resolve_explicit_read_target_id()
        if target_error:
            QMessageBox.warning(self, "写前读取失败", target_error)
            return
        readback_definition, read_payload = self.registry.build_readback_preview(
            definition.command_id,
            read_target,
            profile_name=self._current_profile_name(),
        )
        target_snapshot = self.registry.build_target_snapshot(
            definition.command_id,
            values,
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

    def _confirm_high_risk_command(self, definition: CommandDefinition, preview: str) -> bool:
        envelope = YGasProtocol.parse_command(preview)
        effective_target = envelope.target_id if envelope is not None else self._current_target_id()
        reply = QMessageBox.question(
            self,
            "高风险命令确认",
            (
                f"中文功能名: {definition.display_name}\n"
                f"原始命令: {preview}\n"
                f"当前权限等级: {self.permission_combo.currentText()}\n"
                f"当前会话模式: {self._current_session_mode()}\n"
                f"本次生效 target id: {effective_target}\n"
                f"是否 FFF: {'是' if effective_target == 'FFF' else '否'}\n"
                f"风险等级: {definition.risk_level.upper()}\n\n"
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
            "导出 CSV",
            str(Path(self.last_export_dir) / "ygas_export.csv"),
            "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            output = self.controller.export_history(path)
            self.last_export_dir = str(Path(output).parent)
            QMessageBox.information(self, "导出成功", f"数据已导出到:\n{output}")
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
                output_dir=selected_dir,
                logger_path=self.controller.log_path,
                note=self.session_note_edit.text().strip(),
            )
            self.last_export_dir = str(Path(selected_dir))
            QMessageBox.information(self, "会话包导出成功", f"已导出到:\n{output}")
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

    def _copy_environment_info(self) -> None:
        text = environment_summary_text()
        QGuiApplication.clipboard().setText(text)
        QMessageBox.information(self, "已复制", "环境信息已复制到剪贴板。")

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

    def _send_raw_command(self) -> None:
        if self.permission_combo.currentText() != "EXPERT":
            QMessageBox.warning(self, "权限不足", "专家原始终端仅在专家模式下开放。")
            return
        if self.replay_running:
            QMessageBox.warning(self, "回放中", "回放期间禁止向真实设备发送命令。")
            return
        payload = self.raw_command_edit.text().strip()
        if not payload:
            return
        if self._current_session_mode() == SESSION_MODE_LISTEN_ONLY:
            QMessageBox.warning(self, "会话模式限制", "只监听模式禁止发送命令。")
            return
        if payload.upper().endswith(",FFF") or ",FFF," in payload.upper():
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

        self.custom_panel = CommandWorkspacePanel(self.registry, custom_families, profile_name=self._current_profile_name())
        self.custom_panel.read_page_button.hide()
        self.custom_panel.set_target_id(self._current_target_id())
        self.custom_panel.set_permission_level(self.permission_combo.currentText())
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
        self.raw_send_button.setEnabled(
            self.permission_combo.currentText() == "EXPERT"
            and not self.replay_running
            and self._current_session_mode() != SESSION_MODE_LISTEN_ONLY
        )
        self.pages.setTabVisible(self.expert_tab_index, True)
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
        self.freeze_button.setText("解除冻结" if frozen else "监测冻结")
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
            return "只监听模式"
        if "安全握手" in text:
            return "安全握手模式"
        if "只读会话锁" in text:
            return "只读锁开启"
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
        safety_ok, safety_reason = can_execute_command(
            definition,
            connected=self.connected,
            session_mode=self._current_session_mode(),
            read_only_lock=self.read_only_lock_check.isChecked(),
            replay_running=self.replay_running,
        )
        if not safety_ok:
            return SessionWriteStatus(
                online_device_text=online_text,
                session_target_text=self._current_target_id(),
                effective_send_text=effective_send,
                write_allowed=False,
                reason_text=self._summarize_write_block_reason(safety_reason),
            )

        if not has_permission(current_permission, definition.required_permission):
            return SessionWriteStatus(
                online_device_text=online_text,
                session_target_text=self._current_target_id(),
                effective_send_text=effective_send,
                write_allowed=False,
                reason_text="当前权限等级为只读",
            )

        mismatch_message = self._online_target_mismatch_message(definition, effective_send)
        if mismatch_message:
            return SessionWriteStatus(
                online_device_text=online_text,
                session_target_text=self._current_target_id(),
                effective_send_text=effective_send,
                write_allowed=False,
                reason_text=self._summarize_write_block_reason(mismatch_message),
            )

        return SessionWriteStatus(
            online_device_text=online_text,
            session_target_text=self._current_target_id(),
            effective_send_text=effective_send,
            write_allowed=True,
            reason_text="已满足写入条件",
        )

    def _update_hard_status_bar(self) -> None:
        self._hard_status_state = self._build_session_write_status()
        self.hard_online_value_label.setText(self._hard_status_state.online_device_text)
        self.hard_target_value_label.setText(self._hard_status_state.session_target_text)
        self.hard_send_value_label.setText(self._hard_status_state.effective_send_text)
        self.hard_write_permission_label.setText("允许" if self._hard_status_state.write_allowed else "禁止")
        self.hard_reason_value_label.setText(self._hard_status_state.reason_text)
        permission_state = "connected" if self._hard_status_state.write_allowed else "fault"
        self.hard_write_permission_label.setProperty("state", permission_state)
        self.hard_write_permission_label.style().unpolish(self.hard_write_permission_label)
        self.hard_write_permission_label.style().polish(self.hard_write_permission_label)
        self.hard_reason_value_label.setProperty("warning", not self._hard_status_state.write_allowed)
        self.hard_reason_value_label.style().unpolish(self.hard_reason_value_label)
        self.hard_reason_value_label.style().polish(self.hard_reason_value_label)

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

    def _set_connection_state(self, state_key: str, text: str) -> None:
        self.connection_state_label.setText(text)
        self.connection_state_label.setProperty("state", state_key)
        self.connection_state_label.style().unpolish(self.connection_state_label)
        self.connection_state_label.style().polish(self.connection_state_label)

    def _update_status_strip(self, *, force_mode: str | None = None) -> None:
        current_mode = force_mode or self._current_session_mode()
        mode_map = {
            SESSION_MODE_LISTEN_ONLY: "会话模式: 只监听",
            SESSION_MODE_SAFE_HANDSHAKE: "会话模式: 安全握手",
            SESSION_MODE_ENGINEERING: "会话模式: 工程模式",
            SESSION_MODE_REPLAY: "会话模式: 回放",
        }
        backend_text = "SIMULATOR" if self.port_combo.currentText().strip().upper() == "SIMULATOR" else f"REAL: {self.port_combo.currentText().strip()}"
        self.session_mode_status_label.setText(mode_map.get(current_mode, current_mode))
        self.backend_status_label.setText(f"后端: {backend_text}")
        self.lock_status_label.setText(f"只读锁: {'开' if self.read_only_lock_check.isChecked() else '关'}")
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
        self.last_frame = frame
        self.latest_frame_time = frame.timestamp
        self._device_output_mode = f"MODE{frame.mode}"
        self.latest_frame_label.setText(f"最近有效数据: {self._fmt_ts(frame.timestamp)}")
        self._rx_frame_times.append(time.monotonic())
        self._schedule_diagnostic_snapshot_refresh()
        if self.monitor_frozen:
            return
        self.chart_panel.add_frame(frame)
        if self._monitor_page_active():
            self._update_data_cards(frame)
            self.status_panel.update_frame(frame)
        self._refresh_monitor_quick_controls()

    @Slot(object)
    def _handle_raw(self, record: RawFrameRecord) -> None:
        if record.direction == "RX" and not YGasProtocol.is_ack(record.text) and YGasProtocol.parse_line(record.text) is None:
            self._abnormal_frame_times.append(time.monotonic())
        if record.direction == "TX" and ("[FFF]" in record.text or "target=FFF" in record.text):
            self.last_broadcast_command = f"{self._fmt_ts(record.timestamp)} | {record.text}"
            self._update_anomaly_summary()
        if not self.monitor_frozen:
            self.raw_frames.append_record(record)
        if record.direction in {"TX", "RX", "SYS", "REPLAY"}:
            self._queue_terminal_line(f"[{self._fmt_ts(record.timestamp)}] [{record.direction}] {record.text}")

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
        self.log_path_label.setText(f"日志文件: {log_path or '--'}")
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.reconnect_button.setEnabled(not connected)
        if connected:
            self.last_connect_time = datetime.now()
            self._reconnect_backoff_s = 2
            self._set_connection_state("connected", "已连接")
            self._disconnect_expected = False
            self._device_output_mode = self._current_parse_mode() or "MODE2"
            self._device_auto_upload = self._current_acquisition_mode() == "LISTEN"
            self._default_config_scheduled = False
            self._schedule_default_monitoring_config()
        else:
            self._default_config_scheduled = False
            self._device_action_queue.clear()
            self._device_action_active = None
            self._pending_readback_requests.clear()
            self._pending_write_verifications.clear()
            self.latest_online_device_id = ""
            self.active_online_device_ids = []
            if self._disconnect_expected:
                self.last_disconnect_reason = "手动断开"
                self._set_connection_state("disconnected", "未连接")
            elif self.replay_running:
                self._set_connection_state("replay", "正在回放")
            else:
                self._set_connection_state("fault", "串口异常断开")
                self._schedule_auto_reconnect()
        if self.broadcast_check.isChecked() and not connected:
            self.broadcast_check.blockSignals(True)
            self.broadcast_check.setChecked(False)
            self.broadcast_check.blockSignals(False)
            self._update_target_badge()
        self._schedule_diagnostic_snapshot_refresh(immediate=True)
        self._apply_permission_mode()
        self._refresh_monitor_quick_controls()

    @Slot(object)
    def _handle_command_result(self, result: CommandResult) -> None:
        state = "成功" if result.ok else "失败"
        self._queue_terminal_line(f"[{self._fmt_ts(result.timestamp)}] 命令{state}: {result.command} | {result.message}")
        if result.ok:
            self._cmd_success_times.append(time.monotonic())
        else:
            self._cmd_fail_times.append(time.monotonic())
            self.last_command_failure = f"{self._fmt_ts(result.timestamp)} | {result.command} | {result.message}"
            self._update_anomaly_summary()
            self.status_panel.append_event("命令", self._fmt_ts(result.timestamp), f"{result.command} -> {result.message}")
            if "超时" in result.message:
                self.last_command_timeout = f"{self._fmt_ts(result.timestamp)} | {result.command}"
                self._schedule_diagnostic_snapshot_refresh()

        for panel in self._command_panels():
            panel.set_last_response(result.command, result.ok, result.message)
        if self.custom_panel is not None:
            self.custom_panel.set_last_response(result.command, result.ok, result.message)
        pending_readback = self._pending_readback_requests.pop(result.command, None)
        if pending_readback is not None:
            self._handle_pending_readback_result(pending_readback, result)
        pending_write = self._pending_write_verifications.pop(result.command, None)
        if pending_write is not None:
            self._handle_pending_write_result(pending_write, result)
        if self._device_action_active is not None:
            steps = self._device_action_active.get("steps", [])
            current_step = steps[0] if steps else None
            expected_payload = str(current_step["payload"]) if isinstance(current_step, dict) else None
            if expected_payload == result.command:
                if not result.ok:
                    failure_text = str(self._device_action_active.get("failure_text", "设备操作失败"))
                    self._finish_device_action(ok=False, message=f"{failure_text}：{result.message}")
                else:
                    steps.pop(0)
                    if steps:
                        QTimer.singleShot(0, self._dispatch_current_device_action_step)
                    else:
                        success_text = str(self._device_action_active.get("success_text", "设备操作成功。"))
                        self._finish_device_action(ok=True, message=success_text)
        self._update_diagnostic_metrics()
        self._refresh_monitor_quick_controls()

    def _handle_pending_readback_result(self, request: dict[str, object], result: CommandResult) -> None:
        command_id = str(request.get("command_id") or "")
        phase = str(request.get("phase") or "manual")
        profile_name = str(request.get("profile_name") or self._current_profile_name())
        if result.ok:
            self._apply_readback_result(command_id, result)
        if phase == "manual":
            return

        target_snapshot = request.get("target_snapshot")
        if not isinstance(target_snapshot, StructuredValueSnapshot):
            target_snapshot = StructuredValueSnapshot()

        if phase == "before_write":
            if not result.ok:
                self._set_verification_report(
                    command_id,
                    WriteVerificationReport(
                        target=target_snapshot,
                        result_text="写前读取失败",
                        detail_text=f"未执行写入：{result.message}",
                    ),
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
                    "before_snapshot": before_snapshot,
                    "target_snapshot": target_snapshot,
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
                self._set_verification_report(
                    command_id,
                    WriteVerificationReport(
                        before=before_snapshot,
                        target=target_snapshot,
                        result_text="无法验证",
                        detail_text=f"写命令 ACK 成功，但写后读取失败：{result.message}",
                    ),
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
            self._set_verification_report(
                command_id,
                WriteVerificationReport(
                    before=before_snapshot,
                    target=target_snapshot,
                    after=after_snapshot,
                    result_text=result_text,
                    detail_text=detail_text,
                    verified_at=result.timestamp,
                    source_device_id=str(result.response_device_id or "--"),
                ),
            )

    def _handle_pending_write_result(self, request: dict[str, object], result: CommandResult) -> None:
        command_id = str(request.get("command_id") or "")
        profile_name = str(request.get("profile_name") or self._current_profile_name())
        before_snapshot = request.get("before_snapshot")
        if not isinstance(before_snapshot, StructuredValueSnapshot):
            before_snapshot = StructuredValueSnapshot()
        target_snapshot = request.get("target_snapshot")
        if not isinstance(target_snapshot, StructuredValueSnapshot):
            target_snapshot = StructuredValueSnapshot()

        if not result.ok:
            self._set_verification_report(
                command_id,
                WriteVerificationReport(
                    before=before_snapshot,
                    target=target_snapshot,
                    result_text="写入失败",
                    detail_text=f"未执行写后复核：{result.message}",
                ),
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
            "before_snapshot": before_snapshot,
            "target_snapshot": target_snapshot,
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

    def _update_data_cards(self, frame: ParsedFrame) -> None:
        fields = frame.fields
        active_alarm_count = int(fields.get("active_alarm_count") or 0)
        status_severity = "alarm" if active_alarm_count > 0 else "normal"
        frame_age_s = max(0.0, (datetime.now() - frame.timestamp).total_seconds())
        frame_age_severity = "warn" if frame_age_s > 1.5 else "normal"
        temperature_value = fields.get("temperature_c")
        if temperature_value is None:
            temperature_value = fields.get("chamber_temp_c")
        if temperature_value is None:
            temperature_value = fields.get("case_temp_c")
        status_text = f"报警 {active_alarm_count}" if active_alarm_count > 0 else ("正常" if frame.status else "--")
        status_detail = f"MODE{frame.mode} | 寄存器 {frame.status or '--'}"
        co2_delta = self._fmt_ratio_delta(
            fields.get("co2_ratio_raw"),
            fields.get("co2_ratio_f"),
            fallback=self.last_metrics.filter_bias if self.last_metrics is not None else None,
        )
        h2o_delta = self._fmt_ratio_delta(fields.get("h2o_ratio_raw"), fields.get("h2o_ratio_f"))
        primary_items = [
            ("CO2 浓度", self._fmt(fields.get("co2_ppm"), 3), "ppm", "实时主指标", "normal", "co2_ppm"),
            ("H2O 浓度", self._fmt(fields.get("h2o_mmol"), 3), "mmol/mol", "实时主指标", "normal", "h2o_mmol"),
            ("温度", self._fmt(temperature_value, 2), "℃", "当前主温度通道", "normal", "temperature_c"),
            ("压力", self._fmt(fields.get("pressure_kpa"), 2), "kPa", "当前腔压", "normal", "pressure_kpa"),
            ("状态 / 告警", status_text, "", status_detail, status_severity, "status_numeric"),
            ("最新数据时延", f"{frame_age_s:.2f}", "s", "越小越新", frame_age_severity, ""),
            ("CO2 原始比值", self._fmt(fields.get("co2_ratio_raw"), 4), "", "raw", "normal", "co2_ratio_raw"),
            ("CO2 滤波比值", self._fmt(fields.get("co2_ratio_f"), 4), "", "filt", "normal", "co2_ratio_f"),
            ("CO2 滤波差值", co2_delta, "ratio", "filt - raw", "normal", "co2_ratio_delta"),
            ("H2O 原始比值", self._fmt(fields.get("h2o_ratio_raw"), 4), "", "raw", "normal", "h2o_ratio_raw"),
            ("H2O 滤波比值", self._fmt(fields.get("h2o_ratio_f"), 4), "", "filt", "normal", "h2o_ratio_f"),
            ("H2O 滤波差值", h2o_delta, "ratio", "filt - raw", "normal", "h2o_ratio_delta"),
        ]
        secondary_items = [
            ("设备地址", frame.device_id or "--", "", f"MODE{frame.mode}", "normal"),
            ("CO2 密度", self._fmt(fields.get("co2_density"), 3), "mg/m³", "扩展浓度视角", "normal", "co2_density"),
            ("H2O 密度", self._fmt(fields.get("h2o_density"), 3), "g/m³", "扩展浓度视角", "normal", "h2o_density"),
            ("参考信号", self._fmt(fields.get("ref_signal"), 0), "", "原始光学通道", "normal", "ref_signal"),
            ("CO2 信号", self._fmt(fields.get("co2_signal"), 2), "", "信号强度", "normal", "co2_signal"),
            ("H2O 信号", self._fmt(fields.get("h2o_signal"), 2), "", "信号强度", "normal", "h2o_signal"),
            (
                "腔温 / 壳温",
                f"{self._fmt(fields.get('chamber_temp_c'), 2)} / {self._fmt(fields.get('case_temp_c'), 2)}",
                "℃",
                "双温度通道",
                "normal",
            ),
            ("状态寄存器", frame.status or "--", "", f"checksum: {fields.get('checksum') or '--'}", status_severity, "status_numeric"),
        ]
        self.data_cards.update_items(primary_items)
        self.detail_cards.update_items(secondary_items)

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
        return self.target_combo.currentText().strip().upper() or "001"

    def _current_session_mode(self) -> str:
        return str(self.session_mode_combo.currentData() or SESSION_MODE_LISTEN_ONLY)

    def _current_command_timeout_ms(self) -> int:
        return max(300, self._safe_int(self.command_timeout_edit.text(), 2000))

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
