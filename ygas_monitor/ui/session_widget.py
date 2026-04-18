"""Single-session workstation UI."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
import time

from PySide6.QtCore import QTimer, Qt, QUrl, Slot
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
from ..models import AlarmEvent, CommandResult, ParsedFrame, RawFrameRecord, SessionConfig, SerialSettings
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


class SessionWidget(QWidget):
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

    def __init__(self, session_name: str, initial_state: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.session_name = session_name
        self.registry = CommandRegistry()
        self.controller = AnalyzerSessionController(session_name)
        self.initial_state = initial_state or {}

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

        self._build_ui()
        self._connect_signals()
        self._refresh_ports(force=True)
        self._apply_persisted_state(self.initial_state)
        self.pages.setCurrentIndex(0)
        self._apply_permission_mode()
        self._update_target_badge()
        self._update_status_strip()
        self._refresh_diagnostic_snapshot()
        self._update_anomaly_summary()
        self._update_safe_history_combo()
        self.age_timer.start()

    def shutdown(self) -> None:
        self.reconnect_timer.stop()
        self.replay_timer.stop()
        self.age_timer.stop()
        self.controller.shutdown()

    def collect_persisted_state(self) -> dict:
        return {
            "port": self.port_combo.currentText().strip(),
            "baudrate": self.baud_combo.currentText(),
            "bytesize": self.bytesize_combo.currentText(),
            "parity": self.parity_combo.currentText(),
            "stopbits": self.stopbits_combo.currentText(),
            "acquisition_mode": self.capture_combo.currentText(),
            "mode_preference": self.mode_combo.currentText(),
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
        }

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

        layout.addWidget(self._build_status_strip())
        layout.addWidget(self._build_quick_connect_box())

        header_splitter = QSplitter(Qt.Horizontal)
        header_splitter.addWidget(self._build_connection_box())
        header_splitter.addWidget(self._build_workspace_box())
        header_splitter.addWidget(self._build_diagnostic_box())
        header_splitter.setStretchFactor(0, 4)
        header_splitter.setStretchFactor(1, 3)
        header_splitter.setStretchFactor(2, 4)
        layout.addWidget(header_splitter)

        self.pages = QTabWidget()
        self.pages.addTab(self._build_monitor_page(), "实时监测")
        self.pages.addTab(self._build_control_page(), "设备控制")
        self.pages.addTab(self._build_coeff_page(), "系数中心")
        self.pages.addTab(self._build_signal_page(), "信号与滤波")
        self.expert_page = self._build_expert_page()
        self.pages.addTab(self.expert_page, "专家终端")
        self.export_page = self._build_export_page()
        self.pages.addTab(self.export_page, "数据导出与回放")
        layout.addWidget(self.pages, 1)

    def _build_status_strip(self) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        self.connection_state_label = QLabel("未连接")
        self.session_mode_status_label = QLabel("会话模式: 只监听")
        self.backend_status_label = QLabel("后端: --")
        self.lock_status_label = QLabel("只读锁: 关")
        self.latest_frame_label = QLabel("最近有效帧: --")
        self.frame_age_label = QLabel("帧年龄: --")
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

    def _build_quick_connect_box(self) -> QWidget:
        box = QGroupBox("快速连接")
        layout = QGridLayout(box)

        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.refresh_ports_button = QPushButton("刷新串口")
        self.demo_button = QPushButton("演示模式")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.baud_combo.setCurrentText("115200")
        self.capture_combo = QComboBox()
        self.capture_combo.addItems(["LISTEN", "POLL"])
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["AUTO", "MODE1", "MODE2"])
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
        box = QGroupBox("连接与导出")
        layout = QGridLayout(box)

        self.bytesize_combo = QComboBox()
        self.bytesize_combo.addItems(["7", "8"])
        self.bytesize_combo.setCurrentText("8")
        self.parity_combo = QComboBox()
        self.parity_combo.addItems(["N", "E", "O"])
        self.stopbits_combo = QComboBox()
        self.stopbits_combo.addItems(["1", "2"])
        self.profile_combo = QComboBox()
        for key, profile in PROFILES.items():
            self.profile_combo.addItem(profile.display_name, key)
        self.stream_hz_edit = QLineEdit("10")
        self.poll_interval_edit = QLineEdit("200")
        self.command_timeout_edit = QLineEdit("2000")
        self.auto_reconnect_check = QCheckBox("自动重连")
        self.init_button = QPushButton("一键初始化采集")
        self.export_button = QPushButton("导出 CSV")
        self.export_package_button = QPushButton("导出会话包")
        self.export_diagnostic_button = QPushButton("导出诊断包")
        self.open_export_dir_button = QPushButton("打开导出目录")
        self.open_log_dir_button = QPushButton("打开日志目录")
        self.log_path_label = QLabel("日志文件: --")
        self.log_path_label.setProperty("muted", True)

        layout.addWidget(QLabel("数据位"), 0, 0)
        layout.addWidget(self.bytesize_combo, 0, 1)
        layout.addWidget(QLabel("校验位"), 0, 2)
        layout.addWidget(self.parity_combo, 0, 3)
        layout.addWidget(QLabel("停止位"), 1, 0)
        layout.addWidget(self.stopbits_combo, 1, 1)
        layout.addWidget(QLabel("AVERAGE Profile"), 1, 2)
        layout.addWidget(self.profile_combo, 1, 3)
        layout.addWidget(QLabel("主动频率(Hz)"), 2, 0)
        layout.addWidget(self.stream_hz_edit, 2, 1)
        layout.addWidget(QLabel("轮询间隔(ms)"), 2, 2)
        layout.addWidget(self.poll_interval_edit, 2, 3)
        layout.addWidget(QLabel("命令超时(ms)"), 3, 0)
        layout.addWidget(self.command_timeout_edit, 3, 1)
        layout.addWidget(self.auto_reconnect_check, 3, 2, 1, 2)
        layout.addWidget(self.init_button, 4, 0)
        layout.addWidget(self.export_button, 4, 1)
        layout.addWidget(self.export_package_button, 4, 2)
        layout.addWidget(self.export_diagnostic_button, 4, 3)
        layout.addWidget(self.open_export_dir_button, 5, 0, 1, 2)
        layout.addWidget(self.open_log_dir_button, 5, 2, 1, 2)
        layout.addWidget(self.log_path_label, 6, 0, 1, 4)
        return box

    def _build_workspace_box(self) -> QWidget:
        box = QGroupBox("联调安全上下文")
        layout = QFormLayout(box)
        self.permission_combo = QComboBox()
        self.permission_combo.addItems(["READ_ONLY", "CONFIG", "CALIBRATION", "EXPERT"])
        self.session_mode_combo = QComboBox()
        self.session_mode_combo.addItem("只监听（绝不发命令）", SESSION_MODE_LISTEN_ONLY)
        self.session_mode_combo.addItem("安全握手（仅查询类命令）", SESSION_MODE_SAFE_HANDSHAKE)
        self.session_mode_combo.addItem("工程模式（允许正式控制命令）", SESSION_MODE_ENGINEERING)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.addItems(["001"])
        self.broadcast_check = QCheckBox("启用广播地址 FFF")
        self.read_only_lock_check = QCheckBox("只读会话锁")
        self.show_expert_check = QCheckBox("显示专家终端")
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
        layout.addRow("", self.show_expert_check)
        layout.addRow("当前上下文", self.current_target_label)
        layout.addRow("滤波映射", self.profile_hint_label)
        layout.addRow("设备发现", self.discovered_label)
        return box

    def _build_diagnostic_box(self) -> QWidget:
        box = QGroupBox("设备诊断与联调")
        layout = QGridLayout(box)

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
        self.command_history_combo = QComboBox()
        self.command_history_combo.setEditable(False)
        self.resend_history_button = QPushButton("快速重发")

        layout.addWidget(QLabel("当前串口"), 0, 0)
        layout.addWidget(self.diag_port_label, 0, 1)
        layout.addWidget(QLabel("串口参数"), 1, 0)
        layout.addWidget(self.diag_serial_label, 1, 1)
        layout.addWidget(QLabel("最近连接时间"), 2, 0)
        layout.addWidget(self.diag_connect_time_label, 2, 1)
        layout.addWidget(QLabel("最近有效帧时间"), 3, 0)
        layout.addWidget(self.diag_last_frame_label, 3, 1)
        layout.addWidget(QLabel("最近串口异常"), 4, 0)
        layout.addWidget(self.diag_last_serial_error_label, 4, 1)
        layout.addWidget(QLabel("最近命令超时"), 5, 0)
        layout.addWidget(self.diag_last_timeout_label, 5, 1)
        layout.addWidget(QLabel("最近断开原因"), 6, 0)
        layout.addWidget(self.diag_last_disconnect_label, 6, 1)
        layout.addWidget(QLabel("最近 1 分钟接收帧数"), 7, 0)
        layout.addWidget(self.diag_rx_1m_label, 7, 1)
        layout.addWidget(QLabel("最近 1 分钟异常帧数"), 8, 0)
        layout.addWidget(self.diag_abnormal_1m_label, 8, 1)
        layout.addWidget(QLabel("最近 1 分钟命令成/败"), 9, 0)
        layout.addWidget(self.diag_cmd_1m_label, 9, 1)
        layout.addWidget(QLabel("当前接收频率"), 10, 0)
        layout.addWidget(self.diag_receive_hz_label, 10, 1)
        layout.addWidget(QLabel("会话备注"), 11, 0)
        layout.addWidget(self.session_note_edit, 11, 1)
        layout.addWidget(self.copy_env_button, 12, 0)
        layout.addWidget(self.resend_history_button, 12, 1)
        layout.addWidget(QLabel("命令历史"), 13, 0)
        layout.addWidget(self.command_history_combo, 13, 1)
        return box

    def _build_monitor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        controls = QHBoxLayout()
        self.freeze_button = QPushButton("监测冻结")
        self.freeze_button.setCheckable(True)
        controls.addWidget(self.freeze_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.data_cards = MetricCardGrid(rows=3, columns=4)
        self.monitor_cards = MetricCardGrid(rows=2, columns=4)

        anomaly_box = QGroupBox("最近异常摘要")
        anomaly_layout = QFormLayout(anomaly_box)
        self.last_status_label = QLabel("--")
        self.last_command_fail_label = QLabel("--")
        self.last_serial_error_label = QLabel("--")
        self.last_broadcast_label = QLabel("--")
        anomaly_layout.addRow("最近一次状态异常", self.last_status_label)
        anomaly_layout.addRow("最近一次命令失败", self.last_command_fail_label)
        anomaly_layout.addRow("最近一次串口异常", self.last_serial_error_label)
        anomaly_layout.addRow("最近一次广播命令", self.last_broadcast_label)

        self.chart_panel = RealtimeChartPanel()
        self.status_panel = StatusPanel()
        self.raw_frames = RawFramesWidget()

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(self.status_panel)
        right_split.addWidget(self.raw_frames)
        right_split.setStretchFactor(0, 3)
        right_split.setStretchFactor(1, 4)

        lower_split = QSplitter(Qt.Horizontal)
        lower_split.addWidget(self.chart_panel)
        lower_split.addWidget(right_split)
        lower_split.setStretchFactor(0, 5)
        lower_split.setStretchFactor(1, 4)

        layout.addWidget(self.data_cards)
        layout.addWidget(self.monitor_cards)
        layout.addWidget(anomaly_box)
        layout.addWidget(lower_split, 1)
        return page

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

        terminal_box = QGroupBox("专家原始终端")
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

        layout.addWidget(terminal_box, 2)
        layout.addWidget(custom_box, 3)
        return page

    def _build_export_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        top_row = QHBoxLayout()
        self.export_now_button = QPushButton("导出当前数据 CSV")
        self.export_session_button = QPushButton("导出本次会话包")
        self.export_diag_page_button = QPushButton("导出诊断包")
        self.open_export_button = QPushButton("打开导出目录")
        self.load_csv_button = QPushButton("加载 CSV 回放")
        top_row.addWidget(self.export_now_button)
        top_row.addWidget(self.export_session_button)
        top_row.addWidget(self.export_diag_page_button)
        top_row.addWidget(self.open_export_button)
        top_row.addWidget(self.load_csv_button)
        top_row.addStretch(1)

        replay_row = QHBoxLayout()
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

        layout.addLayout(top_row)
        layout.addLayout(replay_row)
        layout.addWidget(self.replay_progress)
        layout.addWidget(self.replay_progress_bar)
        layout.addWidget(self.replay_info_label)
        layout.addWidget(self.replay_time_label)
        return page

    def _connect_signals(self) -> None:
        self.refresh_ports_button.clicked.connect(lambda: self._refresh_ports(force=False))
        self.demo_button.clicked.connect(self._activate_demo_mode)
        self.connect_button.clicked.connect(self._connect_session)
        self.disconnect_button.clicked.connect(self._disconnect_session)
        self.reconnect_button.clicked.connect(self._manual_reconnect)
        self.safe_handshake_button.clicked.connect(self._run_safe_handshake)
        self.init_button.clicked.connect(self.controller.initialize_capture)
        self.export_button.clicked.connect(self._export_history)
        self.export_package_button.clicked.connect(self._export_session_package)
        self.export_diagnostic_button.clicked.connect(self._export_diagnostic)
        self.open_export_dir_button.clicked.connect(self._open_export_dir)
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

        for panel in self._command_panels():
            panel.command_requested.connect(self._handle_command_request)
            panel.read_page_button.clicked.connect(lambda _=False, p=panel: self._read_panel_defaults(p))

        self.custom_editor.template_saved.connect(self._custom_template_saved)

        self.controller.frame_received.connect(self._handle_frame)
        self.controller.raw_received.connect(self._handle_raw)
        self.controller.alarm_emitted.connect(self._handle_alarm)
        self.controller.metrics_updated.connect(self._handle_metrics)
        self.controller.device_ids_updated.connect(self._update_discovered_ids)
        self.controller.connection_changed.connect(self._handle_connection_state)
        self.controller.command_completed.connect(self._handle_command_result)
        self.controller.error.connect(self._handle_error_message)
        self.controller.info.connect(self._handle_info_message)

    def _apply_persisted_state(self, state: dict) -> None:
        if not state:
            return
        self.port_combo.setCurrentText(str(state.get("port", "SIMULATOR")))
        self.baud_combo.setCurrentText(str(state.get("baudrate", "115200")))
        self.bytesize_combo.setCurrentText(str(state.get("bytesize", "8")))
        self.parity_combo.setCurrentText(str(state.get("parity", "N")))
        self.stopbits_combo.setCurrentText(str(state.get("stopbits", "1")))
        self.capture_combo.setCurrentText(str(state.get("acquisition_mode", "LISTEN")))
        self.mode_combo.setCurrentText(str(state.get("mode_preference", "AUTO")))
        session_mode = state.get("session_mode")
        if not session_mode and state.get("listen_only", True):
            session_mode = SESSION_MODE_LISTEN_ONLY
        index = self.session_mode_combo.findData(session_mode or SESSION_MODE_LISTEN_ONLY)
        self.session_mode_combo.setCurrentIndex(max(0, index))
        self.command_timeout_edit.setText(str(state.get("command_timeout_ms", "2000")))
        self.auto_reconnect_check.setChecked(bool(state.get("auto_reconnect", False)))
        self.read_only_lock_check.setChecked(bool(state.get("read_only_lock", False)))
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(state.get("profile_name", "bench_default"))))
        self.stream_hz_edit.setText(str(state.get("stream_hz", "10")))
        self.poll_interval_edit.setText(str(state.get("poll_interval_ms", "200")))
        self.permission_combo.setCurrentText(str(state.get("permission_level", "READ_ONLY")))
        self.show_expert_check.setChecked(bool(state.get("show_expert_terminal", False)))
        self.target_combo.setCurrentText(str(state.get("target_id", "001")))
        self.session_note_edit.setText(str(state.get("session_note", "")))
        self.last_export_dir = str(state.get("last_export_dir") or EXPORT_DIR)
        self.last_replay_file = str(state.get("last_replay_file") or "")

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
            mode_preference=self.mode_combo.currentText(),
            acquisition_mode=self.capture_combo.currentText(),
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
        self.capture_combo.setCurrentText("LISTEN")
        self.mode_combo.setCurrentText("AUTO")
        self.baud_combo.setCurrentText("115200")
        self._append_info_message("已切换为演示模式，端口使用 SIMULATOR。")
        self._update_status_strip()

    def _handle_command_request(self, definition: CommandDefinition, values: dict[str, str], preview: str) -> None:
        current_permission = self.permission_combo.currentText()
        valid, reason = self.registry.validate_command(
            definition.command_id,
            self._current_target_id(),
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
            self._current_target_id(),
            values,
            profile_name=self._current_profile_name(),
        )
        if definition.risk_level.lower() in {"medium", "high", "critical"} or self._current_target_id() == "FFF":
            if not self._confirm_high_risk_command(definition, preview):
                return

        self.controller.update_config(self._build_config())
        if is_read_only_command(definition):
            self._remember_safe_command(preview, definition.return_type, definition.display_name)
        self.controller.send_payload(preview, expectation=definition.return_type, timeout_ms=self._current_command_timeout_ms())

    def _confirm_high_risk_command(self, definition: CommandDefinition, preview: str) -> bool:
        reply = QMessageBox.question(
            self,
            "高风险命令确认",
            (
                f"中文功能名: {definition.display_name}\n"
                f"原始命令: {preview}\n"
                f"当前权限等级: {self.permission_combo.currentText()}\n"
                f"当前会话模式: {self._current_session_mode()}\n"
                f"当前 target id: {self._current_target_id()}\n"
                f"是否 FFF: {'是' if self._current_target_id() == 'FFF' else '否'}\n"
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
        else:
            command_ids = ["GETCO"]
            values_by_id = {"GETCO": {"index": str(self._current_coefficient_index())}}

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
            payload = self.registry.build_preview(
                command_id,
                self._current_target_id(),
                values_by_id[command_id],
                profile_name=self._current_profile_name(),
            )
            self._remember_safe_command(payload, definition.return_type, definition.display_name)
            self.controller.send_payload(payload, expectation=definition.return_type, timeout_ms=timeout_ms)

    def _current_coefficient_index(self) -> int:
        current = self.coeff_panel.current_command().command_id
        digits = "".join(ch for ch in current if ch.isdigit())
        return max(1, min(9, int(digits))) if digits else 1

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
        self._update_data_cards(frame)
        self.chart_panel.add_frame(frame)
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
        self.custom_panel.command_requested.connect(self._handle_command_request)
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
        self.pages.setTabVisible(4, self.show_expert_check.isChecked())
        for panel in self._command_panels():
            panel.set_permission_level(self.permission_combo.currentText())
            panel.set_connected(self.connected)
            panel.set_session_mode(self._current_session_mode())
            panel.set_read_only_lock(self.read_only_lock_check.isChecked())
            panel.set_broadcast_enabled(self.broadcast_check.isChecked())
        if self.custom_panel is not None:
            self.custom_panel.set_permission_level(self.permission_combo.currentText())
            self.custom_panel.set_connected(self.connected)
            self.custom_panel.set_session_mode(self._current_session_mode())
            self.custom_panel.set_read_only_lock(self.read_only_lock_check.isChecked())
            self.custom_panel.set_broadcast_enabled(self.broadcast_check.isChecked())
        self._update_status_strip()

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
            latest = self.controller.frames[-1]
            self._update_data_cards(latest)
            self.status_panel.clear()
            self.status_panel.update_frame(latest)
            for alarm in list(self.controller.alarms):
                self.status_panel.append_alarm(alarm)
        self.raw_frames.clear_records()
        for record in list(self.controller.raw_records):
            self.raw_frames.append_record(record)
        if self.last_metrics is not None:
            self._handle_metrics(self.last_metrics, from_signal=False)

    def _update_target_badge(self) -> None:
        target = self._current_target_id()
        is_broadcast = target == "FFF"
        self.current_target_label.setText(f"当前目标设备 ID: {target}" + (" | 当前正在使用 FFF" if is_broadcast else ""))
        self.current_target_label.setProperty("broadcast", is_broadcast)
        self.current_target_label.style().unpolish(self.current_target_label)
        self.current_target_label.style().polish(self.current_target_label)
        self.broadcast_banner.setVisible(is_broadcast)

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

    def _update_frame_age_status(self) -> None:
        if self.latest_frame_time is None:
            self.frame_age_label.setText("帧年龄: --")
            return
        age_s = max(0.0, (datetime.now() - self.latest_frame_time).total_seconds())
        self.frame_age_label.setText(f"帧年龄: {age_s:.2f}s")
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
        self.latest_frame_label.setText(f"最近有效帧: {self._fmt_ts(frame.timestamp)}")
        self._rx_frame_times.append(time.monotonic())
        self._refresh_diagnostic_snapshot()
        if self.monitor_frozen:
            return
        self._update_data_cards(frame)
        self.chart_panel.add_frame(frame)
        self.status_panel.update_frame(frame)

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
            self.raw_terminal_log.appendPlainText(f"[{self._fmt_ts(record.timestamp)}] [{record.direction}] {record.text}")

    @Slot(object)
    def _handle_alarm(self, alarm: AlarmEvent) -> None:
        self.last_status_anomaly = f"{self._fmt_ts(alarm.timestamp)} | {alarm.message}"
        self._update_anomaly_summary()
        if not self.monitor_frozen:
            self.status_panel.append_alarm(alarm)

    @Slot(object)
    def _handle_metrics(self, metrics: MonitoringMetrics, from_signal: bool = True) -> None:
        self.last_metrics = metrics
        self._update_diagnostic_metrics()
        if self.monitor_frozen and from_signal:
            return
        items = [
            ("实际接收频率", f"{metrics.receive_hz:.1f}", "Hz", "最近 1 秒有效帧数", "normal"),
            ("最新有效帧年龄", f"{metrics.latest_frame_age_s:.2f}" if metrics.latest_frame_age_s is not None else "--", "s", "越小越新", "warn" if (metrics.latest_frame_age_s or 0) > 1.5 else "normal"),
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
        else:
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
        self._refresh_diagnostic_snapshot()
        self._apply_permission_mode()

    @Slot(object)
    def _handle_command_result(self, result: CommandResult) -> None:
        state = "成功" if result.ok else "失败"
        self.raw_terminal_log.appendPlainText(f"[{self._fmt_ts(result.timestamp)}] 命令{state}: {result.command} | {result.message}")
        if result.ok:
            self._cmd_success_times.append(time.monotonic())
        else:
            self._cmd_fail_times.append(time.monotonic())
            self.last_command_failure = f"{self._fmt_ts(result.timestamp)} | {result.command} | {result.message}"
            self._update_anomaly_summary()
            self.status_panel.append_event("命令", self._fmt_ts(result.timestamp), f"{result.command} -> {result.message}")
            if "超时" in result.message:
                self.last_command_timeout = f"{self._fmt_ts(result.timestamp)} | {result.command}"
                self._refresh_diagnostic_snapshot()

        for panel in self._command_panels():
            panel.set_last_response(result.command, result.ok, result.message)
        if self.custom_panel is not None:
            self.custom_panel.set_last_response(result.command, result.ok, result.message)
        self._update_diagnostic_metrics()

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
        if frame.mode == 1:
            items = [
                ("设备地址", frame.device_id or "--", "", "MODE1", "normal"),
                ("CO2 浓度", self._fmt(fields.get("co2_ppm"), 3), "ppm", "实时主指标", "normal"),
                ("H2O 浓度", self._fmt(fields.get("h2o_mmol"), 3), "mmol/mol", "实时主指标", "normal"),
                ("CO2 信号", self._fmt(fields.get("co2_signal"), 2), "", "MODE1 信号强度", "normal"),
                ("H2O 信号", self._fmt(fields.get("h2o_signal"), 2), "", "MODE1 信号强度", "normal"),
                ("温度", self._fmt(fields.get("temperature_c"), 2), "℃", "设备温度", "normal"),
                ("压力", self._fmt(fields.get("pressure_kpa"), 2), "kPa", "腔内压力", "normal"),
                ("状态寄存器", frame.status or "--", "", f"告警数: {active_alarm_count}", status_severity),
                ("校验和", str(fields.get("checksum") or "--"), "", "原始帧校验字段", "normal"),
                ("最新原始帧", frame.raw[:40] + ("..." if len(frame.raw) > 40 else ""), "", "用于快速确认接收内容", "normal"),
            ]
        else:
            items = [
                ("设备地址", frame.device_id or "--", "", "MODE2", "normal"),
                ("CO2 浓度", self._fmt(fields.get("co2_ppm"), 3), "ppm", "实时主指标", "normal"),
                ("H2O 浓度", self._fmt(fields.get("h2o_mmol"), 3), "mmol/mol", "实时主指标", "normal"),
                ("CO2 密度", self._fmt(fields.get("co2_density"), 3), "mg/m³", "MODE2 扩展字段", "normal"),
                ("H2O 密度", self._fmt(fields.get("h2o_density"), 3), "g/m³", "MODE2 扩展字段", "normal"),
                ("CO2 比值(滤波)", self._fmt(fields.get("co2_ratio_f"), 4), "", "滤波值", "normal"),
                ("CO2 比值(原始)", self._fmt(fields.get("co2_ratio_raw"), 4), "", "原始值", "normal"),
                ("H2O 比值(滤波)", self._fmt(fields.get("h2o_ratio_f"), 4), "", "滤波值", "normal"),
                ("H2O 比值(原始)", self._fmt(fields.get("h2o_ratio_raw"), 4), "", "原始值", "normal"),
                ("参考信号", self._fmt(fields.get("ref_signal"), 0), "", "原始光学通道", "normal"),
                ("腔温 / 壳温", f"{self._fmt(fields.get('chamber_temp_c'), 2)} / {self._fmt(fields.get('case_temp_c'), 2)}", "℃", "温度双通道", "normal"),
                ("状态寄存器", frame.status or "--", "", f"extras: {len(frame.extras)}", status_severity),
            ]
        self.data_cards.update_items(items)

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
        self._refresh_diagnostic_snapshot()
        self._update_anomaly_summary()

    def _handle_info_message(self, message: str) -> None:
        self._append_info_message(message)
        if message.startswith("已连接"):
            self.last_connect_time = datetime.now()
            self._refresh_diagnostic_snapshot()

    def _append_info_message(self, message: str) -> None:
        self.raw_terminal_log.appendPlainText(f"[{self._fmt_ts(datetime.now())}] [SYS] {message}")

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
