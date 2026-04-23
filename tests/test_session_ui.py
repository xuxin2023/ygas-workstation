from __future__ import annotations

import ast
from collections import Counter
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QToolBar,
    QToolButton,
)
from unittest import mock

from ygas_monitor.commanding.registry import CommandRegistry
from ygas_monitor.commanding.safety import (
    SESSION_MODE_ENGINEERING,
    SESSION_MODE_LISTEN_ONLY,
    SESSION_MODE_SAFE_HANDSHAKE,
)
from ygas_monitor.models import CommandResult, ParsedFrame, RawFrameRecord, StructuredValueSnapshot, WriteVerificationReport
from ygas_monitor.services.export_service import export_diagnostic_package, export_session_package
from ygas_monitor.services.settings_service import SettingsService
from ygas_monitor.ui.main_window import MainWindow
from ygas_monitor.ui.product_dialogs import _load_help_markdown
from ygas_monitor.ui.session_widget import SessionWidget
from ygas_monitor.ui.widgets.command_cards import CommandWorkspacePanel


def _session_ui_test_method_names() -> list[str]:
    source = Path(__file__).read_text(encoding="utf-8")
    module = ast.parse(source)
    session_ui_tests = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "SessionUiTests")
    return [
        node.name
        for node in session_ui_tests.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]


class SessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _prepare_write_ready_widget(self, name: str) -> SessionWidget:
        widget = SessionWidget(name)
        widget.connected = True
        widget.controller.connected = True
        widget.permission_combo.setCurrentText("CALIBRATION")
        engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
        widget.session_mode_combo.setCurrentIndex(engineering_index)
        widget.read_only_lock_check.setChecked(False)
        widget.target_combo.setCurrentText("012")
        widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
        widget._apply_permission_mode()
        self.app.processEvents()
        return widget

    @staticmethod
    def _table_text(widget: SessionWidget, row: int, column: int) -> str:
        item = widget.change_overview_table.item(row, column)
        return item.text() if item is not None else ""

    @staticmethod
    def _checklist_text(widget: SessionWidget, row: int, column: int) -> str:
        item = widget.default_monitoring_checklist_table.item(row, column)
        return item.text() if item is not None else ""

    @staticmethod
    def _checklist_header_text(widget: SessionWidget, column: int) -> str:
        item = widget.default_monitoring_checklist_table.horizontalHeaderItem(column)
        return item.text() if item is not None else ""

    @staticmethod
    def _page_scroll_area(widget: SessionWidget, index: int) -> QScrollArea | None:
        page = widget.pages.widget(index)
        assert page is not None
        return page.findChild(QScrollArea)

    def _assert_pages_accessible_at_size(self, width: int, height: int) -> None:
        widget = SessionWidget(f"ui-pages-{width}x{height}")
        try:
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            widget._apply_permission_mode()
            widget.resize(width, height)
            widget.show()
            self.app.processEvents()
            self.app.processEvents()

            for index in [
                widget.settings_tab_index,
                widget.monitor_tab_index,
                widget.control_tab_index,
                widget.coeff_tab_index,
                widget.signal_tab_index,
                widget.expert_tab_index,
                widget.export_tab_index,
            ]:
                if index == widget.expert_tab_index and not widget.pages.isTabVisible(widget.expert_tab_index):
                    continue
                widget.pages.setCurrentIndex(index)
                self.app.processEvents()
                self.app.processEvents()

                page = widget.pages.widget(index)
                assert page is not None
                self.assertGreater(page.height(), 0)

                if index == widget.monitor_tab_index:
                    self.assertGreater(widget.chart_panel.height(), 0)
                    self.assertGreaterEqual(widget.chart_panel.height() / max(1, page.height()), 0.55)
                    continue

                scroll = self._page_scroll_area(widget, index)
                self.assertIsNotNone(scroll)
                assert scroll is not None
                self.assertTrue(scroll.widgetResizable())
                self.assertGreaterEqual(scroll.viewport().height(), 1)

                if index == widget.expert_tab_index:
                    self.assertTrue(widget.raw_command_edit.isVisible())
                elif index == widget.export_tab_index:
                    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                    self.app.processEvents()
                    self.assertTrue(widget.change_overview_table.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    @staticmethod
    def _record_change_entry(
        widget: SessionWidget,
        command_id: str,
        result_text: str,
        *,
        target_device_id: str = "012",
        before_value: str = "--",
        target_value: str = "--",
        after_value: str = "--",
        detail_text: str | None = None,
        verification_status: str = "",
    ) -> None:
        report = WriteVerificationReport(
            before=StructuredValueSnapshot(summary=before_value),
            target=StructuredValueSnapshot(summary=target_value),
            after=StructuredValueSnapshot(summary=after_value),
            result_text=result_text,
            detail_text=detail_text or result_text,
            verification_status=verification_status,
            verified_at=datetime(2026, 4, 19, 11, 0, 0),
            source_device_id=target_device_id,
        )
        widget._record_session_change(command_id, report, target_device_id=target_device_id)

    @staticmethod
    def _inject_restore_failure(
        widget: SessionWidget,
        *,
        payload: str = "SETCOMWAY,YGAS,012,1",
        parent_command: str = "GETCO,YGAS,012,1",
    ) -> None:
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 21, 9, 10, 0),
                command=payload,
                ok=False,
                message="恢复主动上传失败：未收到 SETCOMWAY ACK。",
                action_type="auto_silence_restore",
                parent_command=parent_command,
                command_target_id="012",
                expected_device_id="012",
                response_device_id="012",
                effective_scope="single",
                source_page="恢复主动上传",
                auto_upload_state="off",
                original_auto_upload_state="on",
                restore_policy="restore_after_read",
                restore_attempted=True,
                restore_result="restore_failed",
            )
        )

    def _complete_mode2_step_until_auto_upload(self, widget: SessionWidget) -> None:
        mode_definition = widget.registry.get("MODE")
        widget._handle_command_request(mode_definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 12, 0),
                command="MODE,YGAS,012",
                ok=True,
                message="当前工作模式: MODE1",
                response_kind="mode_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "mode": 1},
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 12, 1),
                command="MODE,YGAS,FFF,2",
                ok=True,
                message="ACK 成功",
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 12, 2),
                command="MODE,YGAS,012",
                ok=True,
                message="当前工作模式: MODE2",
                response_kind="mode_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "mode": 2},
            )
        )

    def _complete_capture_init_mode_step_until_ftd(self, widget: SessionWidget) -> None:
        mode_definition = widget.registry.get("MODE")
        widget._handle_command_request(mode_definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 20, 0),
                command="MODE,YGAS,012",
                ok=True,
                message="当前工作模式: MODE1",
                response_kind="mode_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "mode": 1},
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 20, 1),
                command="MODE,YGAS,FFF,2",
                ok=True,
                message="ACK 成功",
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 20, 2),
                command="MODE,YGAS,012",
                ok=True,
                message="当前工作模式: MODE2",
                response_kind="mode_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "mode": 2},
            )
        )

    def _complete_capture_init_ftd_step_until_setcomway(self, widget: SessionWidget) -> None:
        self._complete_capture_init_mode_step_until_ftd(widget)
        ftd_definition = widget.registry.get("FTD")
        widget._handle_command_request(ftd_definition, {"hz": "10"}, "FTD,YGAS,FFF,10", "FFF")
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 21, 0),
                command="FTD,YGAS,012",
                ok=True,
                message="当前值: 5",
                response_kind="setting_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "value": "5", "values": ["5"]},
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 21, 1),
                command="FTD,YGAS,FFF,10",
                ok=True,
                message="ACK 成功",
            )
        )
        widget._handle_command_result(
            CommandResult(
                timestamp=datetime(2026, 4, 20, 10, 21, 2),
                command="FTD,YGAS,012",
                ok=True,
                message="当前值: 10",
                response_kind="setting_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "value": "10", "values": ["10"]},
            )
        )

    def test_monitor_cards_hide_ratio_metrics_from_primary_slots(self) -> None:
        widget = SessionWidget("ui-test")
        try:
            frame = ParsedFrame(
                timestamp=datetime.now() - timedelta(seconds=1.2),
                raw="sample",
                device_id="001",
                mode=2,
                status="0001",
                fields={
                    "co2_ppm": 1.234,
                    "h2o_mmol": 2.345,
                    "temperature_c": 26.5,
                    "pressure_kpa": 101.32,
                    "active_alarm_count": 0,
                    "co2_ratio_raw": 0.9012,
                    "co2_ratio_f": 0.9001,
                    "h2o_ratio_raw": 0.7012,
                    "h2o_ratio_f": 0.7034,
                },
            )

            widget._update_data_cards(frame)
            visible_cards = [card for card in widget.data_cards._cards if not card.isHidden()]
            titles = [card.title_label.text() for card in visible_cards]

            self.assertEqual(len(visible_cards), 6)
            self.assertIn("实际接收频率", titles)
            self.assertNotIn("CO2 原始比值", titles)
            self.assertNotIn("H2O 原始比值", titles)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hidden_monitor_page_refreshes_core_cards_when_returning(self) -> None:
        widget = SessionWidget("ui-hidden-refresh")
        try:
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            self.app.processEvents()

            frame = ParsedFrame(
                timestamp=datetime.now() - timedelta(seconds=0.6),
                raw="sample",
                device_id="001",
                mode=2,
                status="0001",
                fields={
                    "co2_ppm": 1.234,
                    "h2o_mmol": 2.345,
                    "temperature_c": 26.5,
                    "pressure_kpa": 101.32,
                    "active_alarm_count": 0,
                    "co2_ratio_raw": 0.9012,
                    "co2_ratio_f": 0.9001,
                    "h2o_ratio_raw": 0.7012,
                    "h2o_ratio_f": 0.7034,
                },
            )

            widget._handle_frame(frame)
            self.app.processEvents()
            self.assertEqual(widget.data_cards._cards[0].value_label.text(), "--")

            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()
            visible_cards = [card for card in widget.data_cards._cards if not card.isHidden()]
            titles = [card.title_label.text() for card in visible_cards]
            self.assertEqual(len(visible_cards), 6)
            self.assertIn("实际接收频率", titles)
            self.assertNotEqual(widget.data_cards._cards[0].value_label.text(), "--")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_command_tree_search_filters_and_selects_first_visible_result(self) -> None:
        registry = CommandRegistry()
        families = sorted(
            {
                item.family
                for item in registry.all_commands()
                if item.command_id in {"MODE", "MODE_QUERY", "READDATA", "SETCOM_QUERY"}
            }
        )
        panel = CommandWorkspacePanel(registry, families)
        try:
            panel.search_edit.setText("mode")
            self.app.processEvents()

            visible_ids = []
            for index in range(panel.command_tree.topLevelItemCount()):
                family_item = panel.command_tree.topLevelItem(index)
                if family_item.isHidden():
                    continue
                for child_index in range(family_item.childCount()):
                    child = family_item.child(child_index)
                    if not child.isHidden():
                        visible_ids.append(str(child.data(0, Qt.UserRole)))

            self.assertTrue(any("MODE" in command_id for command_id in visible_ids))

            panel._select_first_visible_result()
            current = panel.current_command_id()
            self.assertIn("MODE", current)

            panel.search_edit.setText("definitely-no-such-command")
            self.app.processEvents()
            hidden_count = 0
            total_count = 0
            for index in range(panel.command_tree.topLevelItemCount()):
                family_item = panel.command_tree.topLevelItem(index)
                for child_index in range(family_item.childCount()):
                    total_count += 1
                    if family_item.child(child_index).isHidden():
                        hidden_count += 1
            self.assertEqual(hidden_count, total_count)
        finally:
            panel.close()
            self.app.processEvents()

    def test_command_workspace_shows_current_context_banner(self) -> None:
        registry = CommandRegistry()
        families = sorted({item.family for item in registry.all_commands() if item.command_id in {"MODE", "READDATA"}})
        panel = CommandWorkspacePanel(registry, families)
        try:
            panel.set_target_id("FFF")
            panel.set_online_device_state("002", ["002"])
            panel.set_permission_level("CALIBRATION")
            panel.set_session_mode("ENGINEERING")
            panel.set_read_only_lock(True)
            panel.set_broadcast_enabled(True)
            self.app.processEvents()

            self.assertIn("目标设备 FFF", panel.context_banner.text())
            self.assertIn("实时在线 002", panel.context_banner.text())
            self.assertIn("权限等级 校准模式", panel.context_banner.text())
            self.assertIn("会话模式 工程联调", panel.context_banner.text())
            self.assertIn("只读锁 开启", panel.context_banner.text())
            self.assertIn("广播 FFF 启用", panel.context_banner.text())
            self.assertFalse(panel.context_risk_label.isHidden())
        finally:
            panel.close()
            self.app.processEvents()

    def test_hard_status_bar_detail_tooltip_preserves_write_ready_reason(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-ready")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget._update_hard_status_bar()

            self.assertEqual(widget.session_header_box.title(), "")
            self.assertEqual(widget.hard_online_value_label.text(), "012")
            self.assertEqual(widget.hard_target_value_label.text(), "012")
            self.assertEqual(widget.hard_send_value_label.text(), "FFF")
            self.assertEqual(widget.hard_write_permission_label.text(), "允许")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
            self.assertIn("控制写入：已满足写入条件", widget.hard_reason_value_label.text())
            self.assertIn("控制写入：允许", widget.session_header_detail_button.toolTip())
            self.assertIn("实时流：可启动", widget.session_header_detail_button.toolTip())
            self.assertIn("实时监测模式只允许自动启动实时流", widget.session_header_detail_button.toolTip())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_block_reason_mentions_mismatch_and_multiple_online_devices(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-blocked")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
            self.assertIn("控制写入：target-online 不一致", widget.hard_reason_value_label.text())

            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_online_value_label.text(), "多个")
            self.assertIn("控制写入：禁止", widget.session_header_detail_button.toolTip())
            self.assertIn("当前无唯一在线设备", widget.hard_reason_value_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_reflects_listen_lock_and_replay_states(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-modes")
        try:
            widget.port_combo.setCurrentText("COM35")
            listen_index = widget.session_mode_combo.findData(SESSION_MODE_LISTEN_ONLY)
            widget.session_mode_combo.setCurrentIndex(listen_index)
            widget._apply_permission_mode()
            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "严格只听阻止")
            self.assertIn("严格只听模式", widget.hard_reason_value_label.text())

            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(True)
            widget._apply_permission_mode()
            self.assertEqual(widget.hard_stream_status_label.text(), "严格只读阻止")
            self.assertIn("严格只读", widget.hard_reason_value_label.text())

            widget.read_only_lock_check.setChecked(False)
            widget.replay_running = True
            widget._apply_permission_mode()
            self.assertEqual(widget.hard_stream_status_label.text(), "回放阻止")
            self.assertIn("回放模式", widget.hard_reason_value_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_header_separates_control_write_and_stream_status(self) -> None:
        widget = SessionWidget("ui-header-stream-separation")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
            widget._update_hard_status_bar()

            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
            tooltip = widget.session_header_detail_button.toolTip()
            self.assertIn("控制写入：禁止", tooltip)
            self.assertIn("实时流：可启动", tooltip)
            self.assertIn("控制写入和实时流启动是两套不同的安全语义", tooltip)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_monitoring_mode_shows_write_blocked_but_stream_allowed(self) -> None:
        widget = SessionWidget("ui-monitor-default-header")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})
            widget._update_hard_status_bar()

            self.assertEqual(widget.session_mode_combo.currentData(), SESSION_MODE_SAFE_HANDSHAKE)
            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_strict_read_only_blocks_write_and_stream(self) -> None:
        widget = SessionWidget("ui-header-strict-read-only")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("012")
            widget.read_only_lock_check.setChecked(True)
            widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
            widget._apply_permission_mode()

            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "严格只读阻止")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_fff_blocks_auto_stream_broadcast_in_header(self) -> None:
        widget = SessionWidget("ui-header-fff-stream-block")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("FFF")
            widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
            widget._update_hard_status_bar()

            self.assertEqual(widget.hard_stream_status_label.text(), "禁止自动广播")
            self.assertIn("FFF 目标不会自动发送 SETCOMWAY=1", widget.session_header_detail_button.toolTip())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_new_session_defaults_to_safe_onboarding_state(self) -> None:
        widget = SessionWidget("ui-safe-onboarding-defaults")
        try:
            self.assertEqual(widget.permission_combo.currentText(), "READ_ONLY")
            self.assertEqual(widget.session_mode_combo.currentData(), SESSION_MODE_SAFE_HANDSHAKE)
            self.assertFalse(widget.read_only_lock_check.isChecked())
            self.assertTrue(widget.auto_start_stream_check.isChecked())
            self.assertFalse(widget.broadcast_check.isChecked())
            self.assertIn("默认实时监测模式", widget.session_safety_hint_label.text())
            self.assertIn("工程模式", widget.session_safety_hint_detail_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_persisted_session_state_is_not_overridden_by_safe_defaults(self) -> None:
        widget = SessionWidget(
            "ui-safe-onboarding-persisted",
            initial_state={
                "permission_level": "CALIBRATION",
                "session_mode": SESSION_MODE_ENGINEERING,
                "read_only_lock": False,
                "target_id": "012",
            },
        )
        try:
            self.assertEqual(widget.permission_combo.currentText(), "CALIBRATION")
            self.assertEqual(widget.session_mode_combo.currentData(), SESSION_MODE_ENGINEERING)
            self.assertFalse(widget.read_only_lock_check.isChecked())
            self.assertEqual(widget.target_combo.currentText(), "012")
            self.assertIn("已偏离默认实时监测模式", widget.session_safety_hint_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_parse_mode_is_auto(self) -> None:
        widget = SessionWidget("ui-default-parse-auto")
        try:
            self.assertEqual(widget.mode_combo.currentData(), "AUTO")
            self.assertEqual(widget.monitor_mode_label.text(), "AUTO")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_persisted_state_without_mode_preference_restores_auto(self) -> None:
        widget = SessionWidget(
            "ui-restore-parse-auto",
            initial_state={
                "port": "COM35",
                "acquisition_mode": "LISTEN",
            },
        )
        try:
            self.assertEqual(widget.mode_combo.currentData(), "AUTO")
            self.assertEqual(widget.monitor_mode_label.text(), "AUTO")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_blocks_mismatch_and_multiple_online_devices(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-blocked")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
            self.assertIn("target-online 不一致", widget.hard_reason_value_label.text())

            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_online_value_label.text(), "多个")
            self.assertIn("当前无唯一在线设备", widget.hard_reason_value_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_task_entry_panel_shows_status_summaries_and_routes_without_sending_commands(self) -> None:
        widget = SessionWidget("ui-task-entries")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            widget.task_entry_box.setChecked(True)
            self.app.processEvents()

            self.assertTrue(widget.task_new_device_button.isVisible())
            self.assertEqual(widget.task_new_device_button_label.text(), "新设备接入检查")
            self.assertIn("先确认连接", widget.task_new_device_button_description.text())
            self.assertEqual(widget.task_export_diag_button_label.text(), "导出诊断包")
            self.assertTrue(widget.task_new_device_status_label.isVisible())
            self.assertTrue(widget.task_read_snapshot_status_label.isVisible())
            self.assertTrue(widget.task_address_mode_status_label.isVisible())
            self.assertTrue(widget.task_coeff_review_status_label.isVisible())
            self.assertTrue(widget.task_export_diag_status_label.isVisible())
            self.assertEqual(widget.task_new_device_status_title_label.text(), "当前状态")
            self.assertEqual(widget.task_read_snapshot_status_title_label.text(), "当前状态")
            self.assertEqual(widget.task_address_mode_status_title_label.text(), "当前状态")
            self.assertEqual(widget.task_coeff_review_status_title_label.text(), "当前状态")
            self.assertEqual(widget.task_export_diag_status_title_label.text(), "当前状态")
            self.assertIn("不会自动发送危险写命令", widget.task_entry_feedback_label.text())

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.task_new_device_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.settings_tab_index)

                widget.task_read_snapshot_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)

                widget.task_address_mode_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)

                widget.task_coeff_review_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.coeff_tab_index)

                widget.task_export_diag_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.export_tab_index)

            send_payload.assert_not_called()
            self.assertIn("本次参数变更总览", widget.task_entry_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_task_entry_states_follow_session_context(self) -> None:
        widget = SessionWidget("ui-task-state-context")
        try:
            widget.show()
            self.app.processEvents()

            self.assertEqual(widget.task_new_device_status_label.text(), "未连接")
            self.assertEqual(widget.task_read_snapshot_status_label.text(), "尚未读取")
            self.assertIn("当前目标：001", widget.task_address_mode_status_label.text())
            self.assertEqual(widget.task_coeff_review_status_label.text(), "尚未执行")
            self.assertEqual(widget.task_export_diag_status_label.text(), "本次会话暂无参数变更")

            widget.connected = True
            widget._refresh_task_entry_states()
            self.assertEqual(widget.task_new_device_status_label.text(), "已连接，待识别设备")

            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
            self.assertEqual(widget.task_new_device_status_label.text(), "已识别唯一在线设备：012")

            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]})
            self.assertEqual(widget.task_new_device_status_label.text(), "target/online 不一致")

            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]})
            self.assertEqual(widget.task_new_device_status_label.text(), "多个在线设备，需先收敛")

            widget._pending_readback_requests["MODE,YGAS,012"] = {
                "command_id": "MODE",
                "phase": "manual",
                "profile_name": widget._current_profile_name(),
            }
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 19, 11, 5, 0),
                    command="MODE,YGAS,012",
                    ok=True,
                    message="当前工作模式: MODE1",
                    response_kind="mode_value",
                    response_device_id="012",
                    parsed_payload={"device_id": "012", "mode": 1},
                )
            )
            self.assertIn("最近已读取", widget.task_read_snapshot_status_label.text())
            self.assertIn("设备 012", widget.task_read_snapshot_status_label.text())
            self.assertIn("当前模式：MODE1", widget.task_address_mode_status_label.text())

            widget._pending_readback_requests["ID,YGAS,012"] = {
                "command_id": "ID",
                "phase": "manual",
                "profile_name": widget._current_profile_name(),
            }
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 19, 11, 6, 0),
                    command="ID,YGAS,012",
                    ok=False,
                    message="命令超时，未在 2000 ms 内收到预期响应。",
                )
            )
            self.assertIn("最近读取失败", widget.task_read_snapshot_status_label.text())

            self._record_change_entry(widget, "SENCO1", "一致", before_value="A", target_value="B", after_value="B")
            self.assertEqual(widget.task_coeff_review_status_label.text(), "最近一次复核一致")
            self.assertIn("1 条参数变更待导出", widget.task_export_diag_status_label.text())

            self._record_change_entry(widget, "SENCO1", "写入失败", detail_text="设备拒绝写入")
            self.assertEqual(widget.task_coeff_review_status_label.text(), "最近一次写入失败")
            self.assertIn("2 条参数变更待导出", widget.task_export_diag_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_change_summary_box_breakdown_counts_cover_ack_only_mismatch_and_failure(self) -> None:
        widget = SessionWidget("ui-change-summary")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.export_tab_index)
            widget.change_summary_box.setChecked(True)
            self.app.processEvents()

            self.assertEqual(widget.change_summary_box.title(), "参数变更总览摘要（按需展开）")
            self.assertTrue(widget.change_summary_view_button.isVisible())
            self.assertEqual(widget.change_summary_message_label.text(), "本次会话暂无参数变更")
            self.assertEqual(widget.change_summary_view_button.text(), "查看总览")
            self.assertEqual(widget.change_summary_total_value_label.text(), "0")
            self.assertEqual(widget.change_summary_total_value_label.parentWidget().layout().itemAtPosition(1, 0).widget().text(), "总数")
            self.assertEqual(widget.change_summary_consistent_value_label.parentWidget().layout().itemAtPosition(1, 1).widget().text(), "一致")
            self.assertEqual(widget.change_summary_ack_only_value_label.parentWidget().layout().itemAtPosition(1, 2).widget().text(), "ACK-only 未复核")
            self.assertEqual(widget.change_summary_mismatch_value_label.parentWidget().layout().itemAtPosition(1, 3).widget().text(), "不一致")
            self.assertEqual(widget.change_summary_unverifiable_value_label.parentWidget().layout().itemAtPosition(1, 4).widget().text(), "无法验证")
            self.assertEqual(widget.change_summary_failure_value_label.parentWidget().layout().itemAtPosition(1, 5).widget().text(), "失败类")

            self._record_change_entry(widget, "MODE", "一致", before_value="MODE1", target_value="MODE2", after_value="MODE2")
            self._record_change_entry(
                widget,
                "SETCOMWAY",
                "ACK 成功",
                target_value="主动发送",
                after_value="未读回",
                detail_text="ACK-only / 未复核",
                verification_status="ack_only_unverified",
            )
            self._record_change_entry(widget, "MODE", "不一致", before_value="MODE1", target_value="MODE2", after_value="MODE3")
            self._record_change_entry(widget, "MODE", "无法验证")
            self._record_change_entry(widget, "MODE", "写入失败")
            self._record_change_entry(widget, "MODE", "写前读取失败")

            self.assertEqual(widget.change_summary_total_value_label.text(), "6")
            self.assertEqual(widget.change_summary_consistent_value_label.text(), "1")
            self.assertEqual(widget.change_summary_ack_only_value_label.text(), "1")
            self.assertEqual(widget.change_summary_mismatch_value_label.text(), "1")
            self.assertEqual(widget.change_summary_unverifiable_value_label.text(), "1")
            self.assertEqual(widget.change_summary_failure_value_label.text(), "2")
            self.assertIn("6 条参数变更", widget.change_summary_message_label.text())
            self.assertIn("1 条仅 ACK", widget.change_summary_message_label.text())
            self.assertEqual(widget.change_overview_table.rowCount(), 6)

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.change_summary_view_button.click()
                self.app.processEvents()
                self.assertEqual(widget.pages.currentIndex(), widget.export_tab_index)
                self.assertIn("本次参数变更总览", widget.task_entry_feedback_label.text())
            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_change_summary_message_mentions_unconfirmed_ack_records(self) -> None:
        widget = SessionWidget("ui-change-summary-unconfirmed")
        try:
            self._record_change_entry(
                widget,
                "SETCOMWAY",
                "连接断开，未收到 ACK",
                target_value="主动发送",
                after_value="未读回",
                detail_text="ACK 未确认",
                verification_status="ack_timeout_unverified",
            )

            self.assertEqual(widget.change_summary_failure_value_label.text(), "1")
            self.assertIn("软件未取得 ACK 确认", widget.change_summary_message_label.text())
            self.assertIn("ACK 未确认", widget.task_export_diag_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_curve_settings_panel_actions_update_slot_summary(self) -> None:
        widget = SessionWidget("ui-chart-config")
        try:
            widget.show()
            self.app.processEvents()

            self.assertFalse(widget.chart_config_group.isVisible())
            widget.chart_panel.curve_settings_toggle_button.click()
            self.app.processEvents()

            self.assertTrue(widget.chart_config_group.isVisible())
            self.assertIn("CO2 浓度", widget.chart_upper_summary_label.text())
            self.assertIn("H2O 浓度", widget.chart_lower_summary_label.text())

            widget.chart_upper_view_combo.setCurrentIndex(widget.chart_upper_view_combo.findData("temperature"))
            self.app.processEvents()
            self.assertIn("腔温", widget.chart_upper_summary_label.text())

            widget._clear_chart_slot(1)
            self.assertEqual(widget.chart_lower_summary_label.text(), "未配置")

            widget._restore_chart_defaults()
            self.assertIn("CO2 浓度", widget.chart_upper_summary_label.text())
            self.assertIn("H2O 浓度", widget.chart_lower_summary_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_senco_preview_uses_fff_and_normalized_coefficients(self) -> None:
        widget = SessionWidget("ui-senco")
        try:
            widget.coeff_panel.select_command("SENCO1")
            detail = widget.coeff_panel.detail_widget
            field = detail._fields["coefficients"]
            assert isinstance(field, QLineEdit)

            field.setText("65916.6, -106614, 0")
            self.app.processEvents()

            self.assertIn("SENCO1,YGAS,FFF", detail.preview_label.text())
            self.assertIn("6.59166e04,-1.06614e05,0.00000e00", detail.preview_label.text())
            self.assertIn("写命令默认将使用 FFF 广播地址", detail.target_mode_label.text())
            self.assertIn("对象一致性仍按当前会话目标 ID", detail.target_mode_label.text())
            self.assertIn("对象校验目标", detail.preview_meta_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_invalid_senco_input_disables_send_button(self) -> None:
        widget = SessionWidget("ui-senco-invalid")
        try:
            widget.coeff_panel.select_command("SENCO1")
            detail = widget.coeff_panel.detail_widget
            field = detail._fields["coefficients"]
            assert isinstance(field, QLineEdit)

            field.setText("1,2,3,4,5,6,7")
            self.app.processEvents()

            self.assertFalse(detail.send_button.isEnabled())
            self.assertIn("最多支持 6 个系数", detail._hints["coefficients"].text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_average_labels_are_locked_to_water_and_gas(self) -> None:
        registry = CommandRegistry()
        avg1 = registry.get("AVERAGE1")
        avg2_query = registry.get("AVERAGE2_QUERY")

        self.assertEqual(avg1.display_name, "设置水滤波窗口（AVERAGE1）")
        self.assertEqual(avg2_query.display_name, "读取气滤波窗口（AVERAGE2）")

    def test_readback_button_is_visible_only_for_supported_setting_commands(self) -> None:
        widget = SessionWidget("ui-readback-visibility")
        try:
            widget.control_panel.select_command("MODE")
            self.app.processEvents()
            self.assertFalse(widget.control_panel.detail_widget.readback_button.isHidden())

            widget.signal_panel.select_command("SETILLUM")
            self.app.processEvents()
            self.assertTrue(widget.signal_panel.detail_widget.readback_button.isHidden())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readback_request_sends_query_command_with_explicit_target(self) -> None:
        widget = SessionWidget("ui-readback-send")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("012")

            definition = widget.registry.get("MODE")
            with mock.patch.object(widget.controller, "update_config") as update_config, mock.patch.object(
                widget.controller, "send_payload"
            ) as send_payload:
                widget._handle_readback_request(definition)

            update_config.assert_called_once()
            send_payload.assert_called_once()
            self.assertEqual(send_payload.call_args.args[0], "MODE,YGAS,012")
            self.assertEqual(send_payload.call_args.kwargs["expectation"], "mode_value")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readback_request_uses_unique_online_device_when_target_is_fff(self) -> None:
        widget = SessionWidget("ui-readback-fff")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("FFF")
            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]})

            definition = widget.registry.get("MODE")
            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget._handle_readback_request(definition)

            self.assertEqual(send_payload.call_args.args[0], "MODE,YGAS,002")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readback_request_is_blocked_when_target_is_fff_and_online_is_unknown(self) -> None:
        widget = SessionWidget("ui-readback-unknown")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("FFF")
            widget._handle_rx_device_state({"latest_rx_device_id": "", "active_rx_device_ids": []})

            definition = widget.registry.get("MODE")
            with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(
                widget.controller, "send_payload"
            ) as send_payload:
                widget._handle_readback_request(definition)

            warning.assert_called_once()
            send_payload.assert_not_called()
            self.assertIn("FFF", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readback_request_is_blocked_when_target_is_fff_and_multiple_online_devices_exist(self) -> None:
        widget = SessionWidget("ui-readback-multi")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("FFF")
            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]})

            definition = widget.registry.get("MODE")
            with mock.patch.object(QMessageBox, "warning") as warning, mock.patch.object(
                widget.controller, "send_payload"
            ) as send_payload:
                widget._handle_readback_request(definition)

            warning.assert_called_once()
            send_payload.assert_not_called()
            self.assertIn("002,003", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readback_success_updates_display_and_prefills_mode_field(self) -> None:
        widget = SessionWidget("ui-readback-prefill-mode")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("012")
            widget.control_panel.select_command("MODE")
            self.app.processEvents()

            definition = widget.registry.get("MODE")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._handle_readback_request(definition)

            result = CommandResult(
                timestamp=datetime(2026, 4, 19, 10, 30, 0),
                command="MODE,YGAS,012",
                ok=True,
                message="当前工作模式: MODE2",
                response_kind="mode_value",
                response_device_id="012",
                parsed_payload={"device_id": "012", "mode": 2},
            )
            widget._handle_command_result(result)

            detail = widget.control_panel.detail_widget
            mode_field = detail._fields["mode"]
            self.assertEqual(mode_field.currentText(), "2")
            self.assertIn("MODE2", detail.readback_value_label.text())
            self.assertIn("012", detail.readback_device_label.text())
            self.assertIn("MODE,YGAS,FFF,2", detail.preview_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_senco_readback_success_normalizes_and_prefills_coefficients(self) -> None:
        widget = SessionWidget("ui-readback-senco")
        try:
            widget.connected = True
            for panel in widget._command_panels():
                panel.set_connected(True)
            widget.target_combo.setCurrentText("002")
            widget.coeff_panel.select_command("SENCO1")
            self.app.processEvents()

            definition = widget.registry.get("SENCO1")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._handle_readback_request(definition)

            result = CommandResult(
                timestamp=datetime(2026, 4, 19, 10, 35, 0),
                command="GETCO,YGAS,002,1",
                ok=True,
                message="收到系数响应",
                response_kind="coefficient",
                response_device_id="002",
                parsed_payload={"C0": 65916.6, "C1": -106614, "C2": 0},
            )
            widget._handle_command_result(result)

            detail = widget.coeff_panel.detail_widget
            field = detail._fields["coefficients"]
            assert isinstance(field, QLineEdit)
            self.assertEqual(field.text(), "6.59166e04,-1.06614e05,0.00000e00")
            self.assertIn("当前系数", detail.readback_value_label.text())
            self.assertIn("SENCO1,YGAS,FFF,6.59166e04,-1.06614e05,0.00000e00", detail.preview_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_write_verification_runs_pre_read_write_post_read_sequence(self) -> None:
        widget = self._prepare_write_ready_widget("ui-write-verify-sequence")
        try:
            widget.control_panel.select_command("MODE")
            definition = widget.registry.get("MODE")
            detail = widget.control_panel.detail_widget

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")
                self.assertEqual(send_payload.call_args_list[0].kwargs["expectation"], "mode_value")
                self.assertIn("写前读取中", detail.verify_result_label.text())

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 50, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                self.assertEqual(send_payload.call_args_list[1].args[0], "MODE,YGAS,FFF,2")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 50, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                self.assertEqual(send_payload.call_args_list[2].args[0], "MODE,YGAS,012")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 50, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

            self.assertIn("MODE1", detail.verify_before_label.text())
            self.assertIn("MODE2", detail.verify_target_label.text())
            self.assertIn("MODE2", detail.verify_after_label.text())
            self.assertEqual(detail.verify_result_label.text(), "一致")
            self.assertEqual(detail.verify_device_label.text(), "012")
            self.assertEqual(widget.change_overview_table.rowCount(), 1)
            self.assertEqual(self._table_text(widget, 0, 1), "设置工作模式")
            self.assertEqual(self._table_text(widget, 0, 2), "012")
            self.assertIn("MODE1", self._table_text(widget, 0, 3))
            self.assertIn("MODE2", self._table_text(widget, 0, 4))
            self.assertIn("MODE2", self._table_text(widget, 0, 5))
            self.assertEqual(self._table_text(widget, 0, 6), "一致")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_write_verification_marks_mismatch_and_unverifiable_results(self) -> None:
        widget = self._prepare_write_ready_widget("ui-write-verify-states")
        try:
            widget.control_panel.select_command("MODE")
            definition = widget.registry.get("MODE")
            detail = widget.control_panel.detail_widget

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 55, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 55, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 55, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE3",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 3},
                    )
                )
                self.assertEqual(detail.verify_result_label.text(), "不一致")
                self.assertIn("工作模式", detail.verify_detail_label.text())
                self.assertEqual(widget.change_overview_table.rowCount(), 1)
                self.assertEqual(self._table_text(widget, 0, 6), "不一致")

                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 56, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 56, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 56, 2),
                        command="MODE,YGAS,012",
                        ok=False,
                        message="命令超时，未在 2000 ms 内收到预期响应。",
                    )
                )
                self.assertEqual(detail.verify_result_label.text(), "无法验证")
                self.assertIn("写后读取失败", detail.verify_detail_label.text())
                self.assertEqual(widget.change_overview_table.rowCount(), 2)
                self.assertEqual(self._table_text(widget, 0, 6), "无法验证")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_change_overview_records_write_failure_and_pre_read_failure(self) -> None:
        widget = self._prepare_write_ready_widget("ui-write-verify-overview-failures")
        try:
            widget.control_panel.select_command("MODE")
            definition = widget.registry.get("MODE")
            detail = widget.control_panel.detail_widget

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 57, 0),
                        command="MODE,YGAS,012",
                        ok=False,
                        message="命令超时，未在 2000 ms 内收到预期响应。",
                    )
                )
                self.assertEqual(detail.verify_result_label.text(), "写前读取失败")
                self.assertEqual(widget.change_overview_table.rowCount(), 1)
                self.assertEqual(self._table_text(widget, 0, 6), "写前读取失败")

                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 58, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 58, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=False,
                        message="设备拒绝写入",
                    )
                )
                self.assertEqual(detail.verify_result_label.text(), "写入失败")
                self.assertEqual(widget.change_overview_table.rowCount(), 2)
                self.assertEqual(self._table_text(widget, 0, 6), "写入失败")
                self.assertIn("设备拒绝写入", self._table_text(widget, 0, 7))
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_write_command_is_blocked_when_online_device_mismatches_target(self) -> None:
        widget = SessionWidget("ui-target-mismatch")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("CALIBRATION")
            widget.read_only_lock_check.setChecked(False)
            widget.session_mode_combo.setCurrentIndex(widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING))
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state(
                {"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]}
            )
            definition = widget.registry.get("MODE")

            with mock.patch.object(QMessageBox, "warning") as warning:
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            warning.assert_called_once()
            self.assertIn("target=012", warning.call_args.args[2])
            self.assertIn("online=002", warning.call_args.args[2])
            self.assertIn("虽默认使用 FFF", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_fff_write_can_pass_ui_guard_when_online_matches_session_target(self) -> None:
        widget = SessionWidget("ui-target-match")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("CALIBRATION")
            widget.read_only_lock_check.setChecked(False)
            widget.session_mode_combo.setCurrentIndex(widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING))
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state(
                {"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]}
            )
            definition = widget.registry.get("MODE")

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config") as update_config,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 19, 10, 40, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )

            warning.assert_not_called()
            update_config.assert_called_once()
            self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")
            self.assertEqual(send_payload.call_args_list[1].args[0], "MODE,YGAS,FFF,2")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_fff_write_is_blocked_when_online_device_is_unknown(self) -> None:
        widget = SessionWidget("ui-target-unknown")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("CALIBRATION")
            widget.read_only_lock_check.setChecked(False)
            widget.session_mode_combo.setCurrentIndex(widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING))
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state(
                {"latest_rx_device_id": "", "active_rx_device_ids": []}
            )
            definition = widget.registry.get("MODE")

            with mock.patch.object(QMessageBox, "warning") as warning:
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            warning.assert_called_once()
            self.assertIn("当前无法确认实时在线设备 ID", warning.call_args.args[2])
            self.assertIn("target=012", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_fff_write_is_blocked_when_multiple_online_devices_are_active(self) -> None:
        widget = SessionWidget("ui-target-multi-online")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("CALIBRATION")
            widget.read_only_lock_check.setChecked(False)
            widget.session_mode_combo.setCurrentIndex(widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING))
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state(
                {"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]}
            )
            definition = widget.registry.get("MODE")

            with mock.patch.object(QMessageBox, "warning") as warning:
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            warning.assert_called_once()
            self.assertIn("当前检测到多个实时在线设备 ID", warning.call_args.args[2])
            self.assertIn("online=002,003", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_read_command_is_not_blocked_by_default_fff_write_guard_in_ui(self) -> None:
        widget = SessionWidget("ui-read-command")
        try:
            widget.connected = True
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state(
                {"latest_rx_device_id": "", "active_rx_device_ids": []}
            )
            definition = widget.registry.get("READDATA")

            with mock.patch.object(widget.controller, "update_config") as update_config, mock.patch.object(
                widget.controller, "send_payload"
            ) as send_payload, mock.patch.object(QMessageBox, "warning") as warning:
                widget._handle_command_request(definition, {}, "READDATA,YGAS,012", "012")

            warning.assert_not_called()
            update_config.assert_called_once()
            send_payload.assert_called_once()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_connect_success_auto_starts_stream_by_default_without_auto_preparing_checklist(self) -> None:
        widget = SessionWidget("ui-default-config-manual")
        try:
            self.assertFalse(widget.auto_apply_default_config_check.isChecked())
            widget.port_combo.setCurrentText("COM35")
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QTimer, "singleShot", side_effect=lambda _ms, callback: callback()),
            ):
                widget._handle_connection_state(True, "session.log")
                self.app.processEvents()

            send_payload.assert_called_once()
            self.assertEqual(send_payload.call_args.args[0], "SETCOMWAY,YGAS,001,1")
            self.assertEqual(
                send_payload.call_args.kwargs["context"],
                {
                    "system_action": "auto_start_stream",
                    "action_type": "auto_start_stream",
                    "source_page": "连接后自动启动实时流",
                    "auto_upload_state": "on",
                },
            )
            self.assertFalse(widget._default_config_scheduled)
            self.assertIn("正在启动主动上传", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_start_stream_send_payload_has_system_context(self) -> None:
        widget = SessionWidget("ui-auto-start-context")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget.connected = True
            widget.controller.connected = True
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1", automatic=True)

            send_payload.assert_called_once()
            self.assertEqual(send_payload.call_args.args[0], "SETCOMWAY,YGAS,001,1")
            self.assertEqual(
                send_payload.call_args.kwargs["context"],
                {
                    "system_action": "auto_start_stream",
                    "action_type": "auto_start_stream",
                    "source_page": "连接后自动启动实时流",
                    "auto_upload_state": "on",
                },
            )
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_start_stream_command_log_is_labeled_system_action(self) -> None:
        widget = SessionWidget("ui-auto-start-log")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget.connected = True
            widget.controller.connected = True
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1", automatic=True)

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 23, 9, 0, 0),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                    action_type="auto_start_stream",
                    source_page="连接后自动启动实时流",
                    auto_upload_state="on",
                    command_target_id="001",
                    expected_device_id="001",
                    response_device_id="001",
                    effective_scope="single",
                )
            )

            entry = widget._command_log_history[0]
            self.assertEqual(entry.action_type, "auto_start_stream")
            self.assertEqual(entry.action_label_zh, "连接后自动启动实时流")
            self.assertEqual(entry.result, "ACK 成功（未复核）")
            self.assertIn("系统动作：连接后自动启动实时流", entry.detail_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_start_stream_failure_is_recorded_with_source_page(self) -> None:
        widget = SessionWidget("ui-auto-start-failure-record")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget.connected = True
            widget.controller.connected = True
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1", automatic=True)

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 23, 9, 1, 0),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=False,
                    message="未收到 SETCOMWAY ACK。",
                    action_type="auto_start_stream",
                    source_page="连接后自动启动实时流",
                    auto_upload_state="on",
                    command_target_id="001",
                    expected_device_id="001",
                    effective_scope="single",
                )
            )

            journal_entry = widget._session_change_history[0]
            self.assertEqual(journal_entry.action_type, "auto_start_stream")
            self.assertEqual(journal_entry.source_page, "连接后自动启动实时流")
            self.assertIn("未收到 SETCOMWAY ACK", journal_entry.detail_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_session_export_marks_auto_start_stream_as_system_action(self) -> None:
        widget = SessionWidget("ui-auto-start-export")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget.connected = True
            widget.controller.connected = True
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1", automatic=True)

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 23, 9, 2, 0),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                    action_type="auto_start_stream",
                    source_page="连接后自动启动实时流",
                    auto_upload_state="on",
                    command_target_id="001",
                    expected_device_id="001",
                    response_device_id="001",
                    effective_scope="single",
                )
            )
            widget.controller.frames.append(
                ParsedFrame(
                    timestamp=datetime(2026, 4, 23, 9, 2, 1),
                    raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                    device_id="001",
                    mode=1,
                    fields={"co2_ppm": 488.879},
                    status="0001",
                )
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                package_dir = export_session_package(
                    session_name="ui-auto-start-export",
                    frames=list(widget.controller.frames),
                    raw_records=[],
                    config=widget._build_config(),
                    parameter_change_entries=list(widget._session_change_history),
                    command_entries=list(widget._command_log_history),
                    output_dir=Path(temp_dir),
                )
                journal_json = Path(package_dir / "parameter_change_journal.json").read_text(encoding="utf-8")
                command_log_tsv = Path(package_dir / "command_log.tsv").read_text(encoding="utf-8")
                journal_entries = json.loads(journal_json)

            self.assertIn('"action_type": "auto_start_stream"', journal_json)
            self.assertEqual(journal_entries[0]["is_system_action"], "true")
            self.assertIn("连接后自动启动实时流", command_log_tsv)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_connect_success_with_auto_prepare_only_prefills_default_monitoring_workspace(self) -> None:
        widget = SessionWidget("ui-default-config-auto-prepare")
        try:
            widget.auto_start_stream_check.setChecked(False)
            widget.auto_apply_default_config_check.setChecked(True)
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QTimer, "singleShot", side_effect=lambda _ms, callback: callback()),
            ):
                widget._handle_connection_state(True, "session.log")
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            self.assertIn("MODE2 校准模式", self._checklist_text(widget, 0, 1))
            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
            self.assertIn("开启主动上传", self._checklist_text(widget, 1, 1))
            self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
            self.assertTrue(widget.default_monitoring_continue_button.isEnabled())
            self.assertTrue(widget.default_monitoring_restart_button.isEnabled())
            self.assertTrue(widget.default_monitoring_cancel_button.isEnabled())
            self.assertIn("校准联调准备清单", widget.monitor_quick_status_label.text())
            self.assertIn("尚未发送", widget.monitor_quick_status_label.text())
            self.assertIn("不会自动发送", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_connect_success_with_strict_read_only_does_not_auto_start_stream(self) -> None:
        widget = SessionWidget("ui-auto-stream-blocked-read-only")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget.read_only_lock_check.setChecked(True)
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget._handle_connection_state(True, "session.log")
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertIn("当前为严格只读，不会自动启动主动上传。", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_connect_success_with_poll_mode_disables_auto_stream_start(self) -> None:
        widget = SessionWidget("ui-auto-stream-poll-mode")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget._set_acquisition_mode("POLL")
            self.app.processEvents()

            self.assertFalse(widget.auto_start_stream_check.isEnabled())
            self.assertFalse(widget.auto_start_stream_check.isChecked())

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget._handle_connection_state(True, "session.log")
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertIn("手动读取模式", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_parse_mode_does_not_prepare_mode2_by_default(self) -> None:
        widget = SessionWidget("ui-auto-parse-checklist")
        try:
            widget._set_parse_mode("AUTO")
            widget._set_acquisition_mode("LISTEN")
            self.app.processEvents()

            widget._prepare_capture_initialization_checklist()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            checklist_actions = [self._checklist_text(widget, row, 1) for row in range(2)]
            self.assertTrue(all("MODE2" not in action for action in checklist_actions))
            self.assertIn("FTD", checklist_actions[0])
            self.assertIn("主动上传", checklist_actions[1])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_first_valid_frame_updates_monitor_mode_badge(self) -> None:
        widget = SessionWidget("ui-first-frame-mode-badge")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget._set_parse_mode("AUTO")
            widget._device_output_mode = "AUTO"
            widget._device_mode_confirmed = False
            widget._refresh_monitor_quick_controls()
            self.assertEqual(widget.monitor_mode_label.text(), "AUTO")

            widget._handle_frame(
                ParsedFrame(
                    timestamp=datetime.now(),
                    raw="YGAS,001,0479.572,05.198,0958.423,04.249,1.3030,1.3033,0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005",
                    device_id="001",
                    mode=2,
                    status="0005",
                    fields={
                        "co2_ppm": 479.572,
                        "h2o_mmol": 5.198,
                        "co2_density": 958.423,
                        "h2o_density": 4.249,
                        "co2_ratio_f": 1.3030,
                        "co2_ratio_raw": 1.3033,
                        "h2o_ratio_f": 0.7888,
                        "h2o_ratio_raw": 0.7888,
                        "ref_signal": 3322,
                        "co2_signal": 4356,
                        "h2o_signal": 2631,
                        "chamber_temp_c": 2.18,
                        "case_temp_c": 2.31,
                        "pressure_kpa": 103.97,
                    },
                )
            )
            self.app.processEvents()

            self.assertEqual(widget.monitor_mode_label.text(), "MODE2")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_serial_assistant_tab_is_hidden_by_default_and_requires_expert_toggle(self) -> None:
        widget = SessionWidget("ui-serial-assistant")
        try:
            self.assertFalse(widget.show_expert_check.isChecked())
            self.assertFalse(widget.show_expert_check.isEnabled())
            self.assertFalse(widget.pages.isTabVisible(widget.expert_tab_index))
            self.assertFalse(widget.raw_send_button.isEnabled())

            widget.permission_combo.setCurrentText("EXPERT")
            self.app.processEvents()

            self.assertTrue(widget.show_expert_check.isEnabled())
            self.assertFalse(widget.pages.isTabVisible(widget.expert_tab_index))
            self.assertFalse(widget.raw_send_button.isEnabled())

            widget.show_expert_check.setChecked(True)
            self.app.processEvents()

            self.assertEqual(widget.pages.tabText(widget.expert_tab_index), "串口助手")
            self.assertTrue(widget.pages.isTabVisible(widget.expert_tab_index))
            self.assertTrue(widget.raw_send_button.isEnabled())
            self.assertGreater(widget.serial_template_combo.count(), 0)

            widget.permission_combo.setCurrentText("CONFIG")
            self.app.processEvents()

            self.assertFalse(widget.pages.isTabVisible(widget.expert_tab_index))
            self.assertFalse(widget.raw_send_button.isEnabled())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_non_expert_cannot_send_raw_command(self) -> None:
        widget = SessionWidget("ui-raw-non-expert")
        try:
            widget.connected = True
            widget.raw_command_edit.setText("MODE,YGAS,001,2")
            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._send_raw_command()

            send_payload.assert_not_called()
            warning.assert_called_once()
            self.assertIn("原始命令发送不可用", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_query_template_can_run_with_read_only_lock_when_expert_terminal_enabled(self) -> None:
        widget = SessionWidget("ui-raw-query")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            safe_index = widget.session_mode_combo.findData(SESSION_MODE_SAFE_HANDSHAKE)
            widget.session_mode_combo.setCurrentIndex(safe_index)
            widget.read_only_lock_check.setChecked(True)
            widget._apply_permission_mode()
            widget.raw_command_edit.setText("MODE,YGAS,001")
            widget.raw_expectation_combo.setCurrentText("mode_value")
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "question") as question,
            ):
                widget._send_raw_command()

            send_payload.assert_called_once()
            question.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_write_command_is_blocked_without_engineering_mode_or_with_read_only_lock(self) -> None:
        widget = SessionWidget("ui-raw-write-guards")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            widget.raw_command_edit.setText("MODE,YGAS,001,2")
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._send_raw_command()
                self.assertIn("工程模式", warning.call_args.args[2])

            send_payload.assert_not_called()

            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(True)
            widget._apply_permission_mode()
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._send_raw_command()
                self.assertIn("只读锁", warning.call_args.args[2])

            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_write_command_requires_bypass_confirmation(self) -> None:
        widget = SessionWidget("ui-raw-write-confirm")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(False)
            widget._apply_permission_mode()
            widget.raw_command_edit.setText("MODE,YGAS,001,2")
            self.app.processEvents()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
            ):
                widget._send_raw_command()

            send_payload.assert_not_called()
            question.assert_called_once()

            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            ):
                widget._send_raw_command()

            send_payload.assert_called_once()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_mode2_fff_requires_typed_confirmation_and_accepts_ascii_phrase(self) -> None:
        widget = SessionWidget("ui-raw-mode2-fff-typed")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(False)
            widget._apply_permission_mode()
            widget.target_combo.setCurrentText("012")
            widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
            widget.raw_command_edit.setText("MODE,YGAS,FFF,2")
            widget.raw_expectation_combo.setCurrentText("ack")
            self.app.processEvents()

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question,
                mock.patch.object(QInputDialog, "getText", return_value=("MODE2 FFF", True)) as typed,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._send_raw_command()

            self.assertTrue(typed.called)
            self.assertIn("MODE,YGAS,FFF,2", typed.call_args.args[2])
            self.assertIn("广播 target：FFF", typed.call_args.args[2])
            self.assertIn("当前会话 target：012", typed.call_args.args[2])
            self.assertIn("当前在线设备：012", typed.call_args.args[2])
            self.assertIn("校准模式", typed.call_args.args[2])
            self.assertIn("MODE2 FFF", typed.call_args.args[2])
            self.assertTrue(question.called)
            send_payload.assert_called_once_with("MODE,YGAS,FFF,2", expectation="ack", timeout_ms=widget._current_command_timeout_ms())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_mode2_fff_typed_confirmation_blocks_send_when_phrase_is_wrong(self) -> None:
        widget = SessionWidget("ui-raw-mode2-fff-typed-block")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(False)
            widget._apply_permission_mode()
            widget.raw_command_edit.setText("MODE,YGAS,FFF,2")
            self.app.processEvents()

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                mock.patch.object(QInputDialog, "getText", return_value=("not-approved", True)),
                mock.patch.object(QMessageBox, "warning") as warning,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._send_raw_command()

            send_payload.assert_not_called()
            self.assertIn("确认短语不匹配", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_single_target_mode2_does_not_require_broadcast_typed_confirmation(self) -> None:
        widget = SessionWidget("ui-raw-mode2-single-no-typed")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(False)
            widget._apply_permission_mode()
            widget.raw_command_edit.setText("MODE,YGAS,012,2")
            self.app.processEvents()

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question,
                mock.patch.object(QInputDialog, "getText") as typed,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._send_raw_command()

            self.assertFalse(typed.called)
            send_payload.assert_called_once()
            self.assertTrue(question.called)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_current_page_index_is_restored_from_persisted_state(self) -> None:
        seed_widget = SessionWidget("ui-page-restore-seed")
        try:
            export_index = seed_widget.export_tab_index
        finally:
            seed_widget.shutdown()
            seed_widget.close()
            self.app.processEvents()

        widget = SessionWidget("ui-page-restore", initial_state={"current_page_index": export_index})
        try:
            self.assertEqual(widget.pages.currentIndex(), export_index)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_settings_task_entry_and_export_summary_start_collapsed_on_their_pages(self) -> None:
        widget = SessionWidget("ui-header-collapsed")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            self.app.processEvents()

            self.assertTrue(widget.session_header_box.isVisible())
            self.assertTrue(widget.task_entry_box.isCheckable())
            self.assertFalse(widget.task_entry_box.isChecked())
            self.assertFalse(widget.task_entry_feedback_label.isVisible())
            self.assertTrue(widget.connection_state_label.isVisible())
            self.assertTrue(widget.hard_online_value_label.isVisible())

            widget.pages.setCurrentIndex(widget.export_tab_index)
            self.app.processEvents()
            self.assertTrue(widget.change_summary_box.isCheckable())
            self.assertFalse(widget.change_summary_box.isChecked())
            self.assertFalse(widget.change_summary_message_label.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_settings_page_is_split_into_device_access_safety_and_diagnostics_sections(self) -> None:
        widget = SessionWidget("ui-settings-structure")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            self.app.processEvents()
            self.assertEqual(widget.device_access_section_box.title(), "1. 设备接入")
            self.assertEqual(widget.commissioning_safety_section_box.title(), "2. 联调安全")
            self.assertEqual(widget.diagnostic_environment_section_box.title(), "3. 诊断与环境")
            self.assertTrue(widget.device_access_section_box.isVisible())
            self.assertTrue(widget.commissioning_safety_section_box.isVisible())
            self.assertTrue(widget.diagnostic_environment_section_box.isVisible())
            self.assertTrue(widget.auto_apply_default_config_check.isVisible())
            self.assertTrue(widget.show_expert_check.isVisible())
            self.assertTrue(widget.session_note_edit.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_control_page_adds_common_actions_without_removing_command_workspace(self) -> None:
        widget = SessionWidget("ui-control-common-actions")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.control_tab_index)
            self.app.processEvents()
            self.assertEqual(widget.common_control_actions_box.title(), "现场常用动作")
            self.assertIsNotNone(widget.control_panel)
            self.assertTrue(widget.control_snapshot_button.isVisible())
            self.assertEqual(widget.control_mode1_button.text(), "预填 MODE1")
            self.assertEqual(widget.control_auto_upload_off_button.text(), "预填关闭主动上传")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_common_mode_actions_only_prefill_mode_command_without_sending(self) -> None:
        widget = SessionWidget("ui-common-mode-prefill")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.control_tab_index)
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.control_mode1_button.click()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "MODE")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "1")
                self.assertIn("尚未发送", widget.control_common_feedback_label.text())
                self.assertNotIn("已请求", widget.control_common_feedback_label.text())
                self.assertNotIn("已发送", widget.control_common_feedback_label.text())

                widget.control_mode2_button.click()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "MODE")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
                self.assertIn("尚未发送", widget.control_common_feedback_label.text())
                self.assertNotIn("已请求", widget.control_common_feedback_label.text())
                self.assertNotIn("已发送", widget.control_common_feedback_label.text())

            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_common_auto_upload_actions_only_prefill_setcomway_without_sending(self) -> None:
        widget = SessionWidget("ui-common-upload-prefill")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.control_tab_index)
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.control_auto_upload_on_button.click()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "SETCOMWAY")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "1")
                self.assertIn("尚未发送", widget.control_common_feedback_label.text())
                self.assertNotIn("已请求", widget.control_common_feedback_label.text())
                self.assertNotIn("已发送", widget.control_common_feedback_label.text())

                widget.control_auto_upload_off_button.click()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "SETCOMWAY")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "0")
                self.assertIn("尚未发送", widget.control_common_feedback_label.text())
                self.assertNotIn("已请求", widget.control_common_feedback_label.text())
                self.assertNotIn("已发送", widget.control_common_feedback_label.text())

            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_common_write_action_feedback_marks_blocked_state_without_claiming_request(self) -> None:
        widget = SessionWidget("ui-common-blocked-feedback")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.control_tab_index)
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.control_mode1_button.click()
                self.app.processEvents()

            feedback = widget.control_common_feedback_label.text()
            self.assertIn("尚未发送", feedback)
            self.assertIn("当前未连接设备", feedback)
            self.assertIn("当前未处于工程模式", feedback)
            self.assertNotIn("只读锁已开启", feedback)
            self.assertNotIn("已请求", feedback)
            self.assertNotIn("已发送", feedback)
            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_quick_actions_only_prepare_control_workspace_without_sending(self) -> None:
        widget = SessionWidget("ui-monitor-quick-prefill")
        try:
            widget.show()
            widget._device_auto_upload = False
            widget._refresh_monitor_quick_controls()
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.monitor_action_mode1.trigger()
                self.app.processEvents()
                self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)
                self.assertEqual(widget.control_panel.current_command_id(), "MODE")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "1")
                self.assertTrue(widget.control_panel.detail_widget.send_button.isVisible())
                self.assertIn("尚未发送", widget.monitor_quick_status_label.text())

                widget.monitor_action_upload_on.trigger()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "SETCOMWAY")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "1")
                self.assertIn("尚未发送", widget.monitor_quick_status_label.text())

                widget.apply_default_config_button.click()
                self.app.processEvents()
                self.assertEqual(widget.control_panel.current_command_id(), "MODE")
                self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
                self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
                self.assertEqual(self._checklist_text(widget, 0, 0), "1")
                self.assertIn("MODE2 校准模式", self._checklist_text(widget, 0, 1))
                self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
                self.assertIn("开启主动上传", self._checklist_text(widget, 1, 1))
                self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
                self.assertIn("校准联调准备清单", widget.default_monitoring_checklist_summary_label.text())
                self.assertIn("当前步骤为第 1 步", widget.default_monitoring_checklist_action_label.text())
                self.assertIn("尚未发送", widget.monitor_quick_status_label.text())
                self.assertIn("不会自动发送", widget.monitor_quick_status_label.text())

            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_monitoring_checklist_advances_to_next_step_after_current_step_succeeds(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-step-success")
        try:
            widget._apply_default_monitoring_config()
            self.app.processEvents()
            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")
                self.assertFalse(widget.default_monitoring_continue_button.isEnabled())
                self.assertFalse(widget.default_monitoring_restart_button.isEnabled())
                self.assertTrue(widget.default_monitoring_cancel_button.isEnabled())
                self.assertIn("执行中", widget.default_monitoring_checklist_action_label.text())
                self.assertIn("等待 ACK", widget.monitor_quick_status_label.text())
                self.assertNotIn("尚未发送", widget.default_monitoring_checklist_summary_label.text())
                self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 0, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                self.assertEqual(send_payload.call_args_list[1].args[0], "MODE,YGAS,FFF,2")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 0, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                self.assertEqual(send_payload.call_args_list[2].args[0], "MODE,YGAS,012")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 0, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

            self.assertEqual(self._checklist_text(widget, 0, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 1, 3), "当前步骤")
            self.assertEqual(widget.control_panel.current_command_id(), "SETCOMWAY")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "1")
            self.assertIn("下一步待确认", widget.monitor_quick_status_label.text())
            self.assertEqual(len(send_payload.call_args_list), 3)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_monitoring_checklist_stops_when_current_step_fails(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-step-fail")
        try:
            widget._apply_default_monitoring_config()
            self.app.processEvents()

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")
                self.assertFalse(widget.default_monitoring_continue_button.isEnabled())
                self.assertFalse(widget.default_monitoring_restart_button.isEnabled())

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 5, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 5, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=False,
                        message="设备拒绝写入",
                    )
                )

            self.assertEqual(self._checklist_text(widget, 0, 3), "已失败")
            self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertIn("未自动推进", widget.monitor_quick_status_label.text())
            self.assertIn("设备拒绝写入", widget.default_monitoring_checklist_note_label.text())
            self.assertEqual(len(send_payload.call_args_list), 2)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_continue_current_checklist_step_only_prefills_without_sending(self) -> None:
        widget = SessionWidget("ui-default-config-continue")
        try:
            widget._apply_default_monitoring_config()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.default_monitoring_continue_button.click()
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
            self.assertIn("尚未发送", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_running_default_monitoring_step_disables_continue_without_retriggering_send(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-running-guard")
        try:
            widget._apply_default_monitoring_config()
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.app.processEvents()
                self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")
                self.assertFalse(widget.default_monitoring_continue_button.isEnabled())
                self.assertFalse(widget.default_monitoring_restart_button.isEnabled())

                widget.default_monitoring_continue_button.click()
                self.app.processEvents()

            self.assertEqual(len(send_payload.call_args_list), 1)
            self.assertIn("等待 ACK", widget.monitor_quick_status_label.text())
            self.assertIn("执行中", widget.default_monitoring_checklist_action_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_running_default_monitoring_step_blocks_duplicate_send_at_command_entry(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-duplicate-send")
        try:
            widget._apply_default_monitoring_config()
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(len(send_payload.call_args_list), 1)
                self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")

                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            self.assertEqual(len(send_payload.call_args_list), 1)
            self.assertEqual(len(widget._pending_readback_requests), 1)
            self.assertIn("请勿重复发送", warning.call_args.args[2])
            self.assertIn("请勿重复发送", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_failed_default_monitoring_step_can_retry_without_duplicate_send_guard(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-retry-after-fail")
        try:
            widget._apply_default_monitoring_config()
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 8, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 8, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=False,
                        message="设备拒绝写入",
                    )
                )
                self.assertEqual(self._checklist_text(widget, 0, 3), "已失败")

                widget.default_monitoring_continue_button.click()
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            self.assertEqual(send_payload.call_args_list[2].args[0], "MODE,YGAS,012")
            self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")
            self.assertFalse(warning.called)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_ack_only_pending_blocks_resending_same_payload_without_overwriting_pending(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-pending-block")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config") as update_config,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")
                pending_before = widget._pending_default_monitoring_ack_only_writes["SETCOMWAY,YGAS,FFF,1"]
                send_count_before = send_payload.call_count
                update_count_before = update_config.call_count

                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            self.assertEqual(send_payload.call_args_list[-1].args[0], "SETCOMWAY,YGAS,FFF,1")
            self.assertEqual(send_payload.call_count, send_count_before)
            self.assertEqual(update_config.call_count, update_count_before)
            self.assertIs(widget._pending_default_monitoring_ack_only_writes["SETCOMWAY,YGAS,FFF,1"], pending_before)
            self.assertIn("仍在等待 ACK", warning.call_args.args[2])
            self.assertIn("仍在等待 ACK", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_ack_only_pending_does_not_block_different_payload_and_clears_after_ack(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-pending-different")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")
                widget._handle_command_request(upload_definition, {"mode": "0"}, "SETCOMWAY,YGAS,FFF,0", "FFF")
                self.assertEqual(send_payload.call_args_list[-1].args[0], "SETCOMWAY,YGAS,FFF,0")
                self.assertNotIn("SETCOMWAY,YGAS,FFF,0", widget._pending_default_monitoring_ack_only_writes)

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 3),
                        command="SETCOMWAY,YGAS,FFF,1",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                self.assertFalse(widget._pending_default_monitoring_ack_only_writes)

                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            self.assertEqual(send_payload.call_args_list[-1].args[0], "SETCOMWAY,YGAS,FFF,1")
            self.assertFalse(warning.called)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_continue_current_step_returns_to_failed_step_without_skipping(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-continue-failed")
        try:
            widget._apply_default_monitoring_config()
            self.app.processEvents()

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 8, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 8, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=False,
                        message="设备拒绝写入",
                    )
                )

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.default_monitoring_continue_button.click()
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
            self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
            self.assertIn("失败步骤", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_restart_default_monitoring_checklist_restarts_from_first_step_without_sending(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-restart")
        try:
            widget._apply_default_monitoring_config()
            self.app.processEvents()

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 10, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 10, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 10, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )
            self.assertEqual(self._checklist_text(widget, 1, 3), "当前步骤")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.default_monitoring_restart_button.click()
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
            self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
            self.assertIn("重新开始", widget.monitor_quick_status_label.text())
            self.assertIn("不会自动发送", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_old_default_monitoring_result_does_not_advance_newly_prepared_checklist(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-stale-result")
        try:
            widget._apply_default_monitoring_config()
            old_run_id = widget._default_monitoring_steps[0].run_id

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")

                widget._prepare_default_monitoring_checklist(trigger_source="restart", switch_to_control_page=False)
                self.app.processEvents()

                new_run_id = widget._default_monitoring_steps[0].run_id
                self.assertNotEqual(old_run_id, new_run_id)
                self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")

                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 11, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 11, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 11, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

            self.assertEqual(self._checklist_text(widget, 0, 3), "当前步骤")
            self.assertEqual(self._checklist_text(widget, 1, 3), "待确认")
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
            self.assertEqual(widget.control_panel.detail_widget._fields["mode"].currentText(), "2")
            self.assertEqual(len(send_payload.call_args_list), 3)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_cancel_default_monitoring_checklist_clears_context_without_removing_history(self) -> None:
        widget = SessionWidget("ui-default-config-cancel")
        try:
            self._record_change_entry(widget, "MODE", "一致", detail_text="already-written")
            widget._apply_default_monitoring_config()
            self.app.processEvents()
            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            history_count = len(widget._session_change_history)

            widget.default_monitoring_cancel_button.click()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 0)
            self.assertEqual(len(widget._session_change_history), history_count)
            self.assertFalse(widget.default_monitoring_continue_button.isEnabled())
            self.assertFalse(widget.default_monitoring_cancel_button.isEnabled())
            self.assertIn("不会被撤销", widget.monitor_quick_status_label.text())
            self.assertIn("不会被撤销", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_cancel_running_default_monitoring_checklist_keeps_written_history_and_warns_no_rollback(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-cancel-running")
        try:
            widget._apply_default_monitoring_config()
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
            history_count = len(widget._session_change_history)

            widget.default_monitoring_cancel_button.click()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 0)
            self.assertEqual(len(widget._session_change_history), history_count)
            self.assertIn("命令可能已经发出", widget.monitor_quick_status_label.text())
            self.assertIn("不会撤销已执行写入", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_completed_default_monitoring_checklist_disables_continue_action(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-complete")
        try:
            widget._apply_default_monitoring_config()
            self.app.processEvents()

            mode_definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(mode_definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")
                self.assertEqual(send_payload.call_args_list[3].args[0], "SETCOMWAY,YGAS,FFF,1")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 3),
                        command="SETCOMWAY,YGAS,FFF,1",
                        ok=True,
                        message="ACK 成功",
                    )
                )

            self.assertEqual(self._checklist_text(widget, 0, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 1, 3), "已完成")
            self.assertFalse(widget.default_monitoring_continue_button.isEnabled())
            self.assertTrue(widget.default_monitoring_restart_button.isEnabled())
            self.assertFalse(widget.default_monitoring_cancel_button.isEnabled())
            self.assertIn("已完成", widget.default_monitoring_checklist_action_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_ack_only_default_monitoring_step_records_session_change_with_ack_only_status(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-only")
        try:
            widget._apply_default_monitoring_config()
            mode_definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                widget._handle_command_request(mode_definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")
                self.assertEqual(self._checklist_text(widget, 1, 3), "执行中")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 12, 3),
                        command="SETCOMWAY,YGAS,FFF,1",
                        ok=True,
                        message="ACK 成功",
                    )
                )

            entry = widget._session_change_history[0]
            self.assertEqual(entry.command_name, "设置数据发送方式")
            self.assertEqual(entry.verification_status, "ack_only_unverified")
            self.assertEqual(entry.before_value, "未读取")
            self.assertEqual(entry.after_value, "未读回")
            self.assertIn("无读回复核", entry.detail_text)
            self.assertIn("payload=SETCOMWAY,YGAS,FFF,1", entry.detail_text)
            self.assertNotIn("response_device=", entry.detail_text)
            self.assertIn("FFF 广播", entry.detail_text)
            self.assertEqual(self._checklist_text(widget, 1, 3), "已完成")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_ack_only_note_includes_response_device_when_available(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-response-device")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 12, 3),
                    command="SETCOMWAY,YGAS,FFF,1",
                    ok=True,
                    message="ACK 成功",
                    response_device_id="026",
                )
            )

            entry = widget._session_change_history[0]
            self.assertIn("response_device=026", entry.detail_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_cancelled_checklist_still_records_ack_only_history_and_export(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-cancel-ack-export")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            self.assertIn("SETCOMWAY,YGAS,FFF,1", widget._pending_default_monitoring_ack_only_writes)
            widget.default_monitoring_cancel_button.click()
            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 0)

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 12, 3),
                    command="SETCOMWAY,YGAS,FFF,1",
                    ok=True,
                    message="ACK 成功",
                )
            )

            entry = widget._session_change_history[0]
            self.assertEqual(entry.verification_status, "ack_only_unverified")
            self.assertEqual(entry.source_page, "校准联调准备清单")
            self.assertIn("清单已取消", widget.control_common_feedback_label.text())

            with tempfile.TemporaryDirectory() as temp_dir:
                package_dir = export_diagnostic_package(
                    session_name=widget.session_name,
                    frames=[],
                    raw_records=[],
                    config=widget._build_config(),
                    parameter_change_entries=list(widget._session_change_history),
                    output_dir=temp_dir,
                )
                journal_json = Path(package_dir / "parameter_change_journal.json").read_text(encoding="utf-8")
                journal_csv = Path(package_dir / "parameter_change_journal.csv").read_text(encoding="utf-8-sig")

            self.assertIn("ack_only_unverified", journal_json)
            self.assertIn("SETCOMWAY,YGAS,FFF,1", journal_json)
            self.assertIn("FFF 广播", journal_json)
            self.assertIn("ack_only_unverified", journal_csv)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_ack_only_pending_context_is_cleared_and_duplicate_ack_is_ignored(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-pending-cleanup")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 12, 3),
                    command="SETCOMWAY,YGAS,FFF,1",
                    ok=True,
                    message="ACK 成功",
                )
            )
            history_count = len(widget._session_change_history)
            self.assertFalse(widget._pending_default_monitoring_ack_only_writes)

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 12, 4),
                    command="SETCOMWAY,YGAS,FFF,1",
                    ok=True,
                    message="ACK 成功",
                )
            )

            self.assertEqual(len(widget._session_change_history), history_count)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_disconnect_records_unconfirmed_ack_only_history_before_clearing_pending(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-ack-disconnect-unconfirmed")
        try:
            widget._apply_default_monitoring_config()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_mode2_step_until_auto_upload(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")

            self.assertIn("SETCOMWAY,YGAS,FFF,1", widget._pending_default_monitoring_ack_only_writes)

            widget._handle_connection_state(False, "session.log")

            self.assertFalse(widget._pending_default_monitoring_ack_only_writes)
            entry = widget._session_change_history[0]
            self.assertEqual(entry.verification_status, "ack_timeout_unverified")
            self.assertEqual(entry.result_text, "连接断开，未收到 ACK")
            self.assertIn("本记录不代表设备一定未执行", entry.detail_text)
            self.assertIn("SETCOMWAY,YGAS,FFF,1", entry.detail_text)
            self.assertIn("未确认", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_monitoring_copy_avoids_old_auto_apply_wording(self) -> None:
        widget = SessionWidget("ui-default-config-copy")
        try:
            self.assertEqual(widget.apply_default_config_button.text(), "准备校准清单")
            self.assertEqual(widget.auto_apply_default_config_check.text(), "连接后准备校准清单")
            self.assertIn("校准联调准备清单", widget.default_monitoring_checklist_summary_label.text())
            self.assertIn("MODE2", widget.default_monitoring_checklist_note_label.text())
            self.assertNotIn("默认监测配置", widget.default_monitoring_checklist_summary_label.text())
            self.assertNotIn("自动应用", widget.default_monitoring_checklist_note_label.text())
            self.assertNotIn("自动完成", widget.default_monitoring_checklist_note_label.text())
            self.assertNotIn("已自动设置", widget.default_monitoring_checklist_note_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_init_button_prefills_capture_checklist_steps_without_sending(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-checklist")
        try:
            self.assertEqual(widget.init_button.text(), "准备初始化采集清单")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.init_button.click()
                self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            self.assertEqual(widget.control_panel.current_command_id(), "FTD")
            self.assertEqual(widget.control_panel.detail_widget._fields["hz"].text(), "10")
            self.assertIn("初始化采集清单", widget.default_monitoring_checklist_summary_label.text())
            self.assertIn("FFF 广播", self._checklist_text(widget, 0, 2))
            self.assertEqual(widget._default_monitoring_steps[0].planned_payload, "FTD,YGAS,FFF,10")
            self.assertEqual(widget._default_monitoring_steps[1].planned_payload, "SETCOMWAY,YGAS,FFF,1")
            self.assertIn("计划命令：FTD,YGAS,FFF,10", widget.default_monitoring_checklist_note_label.text())
            self.assertIn("不会自动发送", widget.control_common_feedback_label.text())
            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_checklist_header_shows_name_target_scope_progress_and_status(self) -> None:
        widget = self._prepare_write_ready_widget("ui-checklist-header")
        try:
            widget.init_button.click()
            self.app.processEvents()

            self.assertEqual(widget.checklist_name_value_label.text(), "初始化采集清单（待确认）")
            self.assertIn("当前会话目标：012", widget.checklist_target_value_label.text())
            self.assertIn("FFF 广播", widget.checklist_scope_value_label.text())
            self.assertEqual(widget.checklist_progress_value_label.text(), "1 / 2")
            self.assertEqual(widget.checklist_overall_status_label.text(), "待确认")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_mode_step_runs_and_advances_to_ftd(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-mode-step")
        try:
            widget._set_parse_mode("MODE2")
            widget.init_button.click()
            self.app.processEvents()

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")
                self.assertEqual(self._checklist_text(widget, 0, 3), "执行中")
                self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 20, 0),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE1",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 1},
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 20, 1),
                        command="MODE,YGAS,FFF,2",
                        ok=True,
                        message="ACK 成功",
                    )
                )
                widget._handle_command_result(
                    CommandResult(
                        timestamp=datetime(2026, 4, 20, 10, 20, 2),
                        command="MODE,YGAS,012",
                        ok=True,
                        message="当前工作模式: MODE2",
                        response_kind="mode_value",
                        response_device_id="012",
                        parsed_payload={"device_id": "012", "mode": 2},
                    )
                )

            self.assertEqual(self._checklist_text(widget, 0, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 1, 3), "当前步骤")
            self.assertEqual(widget.control_panel.current_command_id(), "FTD")
            self.assertEqual(widget.control_panel.detail_widget._fields["hz"].text(), "10")
            self.assertIn("初始化采集清单", widget.default_monitoring_checklist_summary_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_ftd_step_advances_to_setcomway(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-ftd-step")
        try:
            widget._set_parse_mode("MODE2")
            widget.init_button.click()
            self.app.processEvents()

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_capture_init_ftd_step_until_setcomway(widget)

            self.assertEqual(self._checklist_text(widget, 0, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 1, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 2, 3), "当前步骤")
            self.assertEqual(widget.control_panel.current_command_id(), "SETCOMWAY")
            self.assertIn("计划命令：SETCOMWAY,YGAS,FFF,1", widget.default_monitoring_checklist_note_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_setcomway_ack_only_uses_capture_source_page(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-source-page")
        try:
            widget._set_parse_mode("MODE2")
            widget.init_button.click()
            self.app.processEvents()

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_capture_init_ftd_step_until_setcomway(widget)
                upload_definition = widget.registry.get("SETCOMWAY")
                widget._handle_command_request(upload_definition, {"mode": "1"}, "SETCOMWAY,YGAS,FFF,1", "FFF")
                self.assertEqual(self._checklist_text(widget, 2, 3), "执行中")

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 22, 0),
                    command="SETCOMWAY,YGAS,FFF,1",
                    ok=True,
                    message="ACK 成功",
                    response_device_id="012",
                )
            )

            entry = widget._session_change_history[0]
            self.assertEqual(entry.source_page, "初始化采集清单")
            self.assertEqual(entry.verification_status, "ack_only_unverified")
            self.assertEqual(self._checklist_text(widget, 2, 3), "已完成")
            self.assertIn("初始化采集清单", widget.control_common_feedback_label.text())
            self.assertIn("已全部完成", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_restart_keeps_capture_initialization_checklist_kind(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-restart")
        try:
            widget._set_parse_mode("MODE2")
            widget.init_button.click()
            self.app.processEvents()
            self.assertEqual(widget._current_prepared_checklist_kind(), "capture_init")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.default_monitoring_restart_button.click()
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertEqual(widget._current_prepared_checklist_kind(), "capture_init")
            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 3)
            self.assertIn("初始化采集清单", widget.default_monitoring_checklist_summary_label.text())
            self.assertNotIn("校准联调准备清单已准备", widget.default_monitoring_checklist_summary_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_step_matching_does_not_depend_on_target_summary_text(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-structured-match")
        try:
            widget._set_parse_mode("MODE2")
            widget.init_button.click()
            self.app.processEvents()
            widget._default_monitoring_steps[0].target_summary = "故意改坏的展示文案"

            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_capture_init_mode_step_until_ftd(widget)

            self.assertEqual(widget._default_monitoring_steps[0].planned_payload, "MODE,YGAS,FFF,2")
            self.assertEqual(self._checklist_text(widget, 0, 3), "已完成")
            self.assertEqual(self._checklist_text(widget, 1, 3), "当前步骤")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_rejects_ftd_above_registry_limit_without_crashing(self) -> None:
        for hz in (21, 50):
            with self.subTest(hz=hz):
                widget = self._prepare_write_ready_widget(f"ui-init-ftd-over-limit-{hz}")
                try:
                    widget._set_parse_mode("MODE2")
                    widget.device_ftd_hz_edit.setValue(hz)
                    with mock.patch.object(widget.controller, "send_payload") as send_payload:
                        widget.init_button.click()
                        self.app.processEvents()

                    self.assertEqual(widget.device_ftd_hz_edit.value(), 20)
                    self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 3)
                    if widget.device_ftd_hz_edit.maximum() == 20:
                        self.assertEqual(widget._default_monitoring_steps[1].planned_payload, "FTD,YGAS,FFF,20")
                        self.assertNotIn(f"FTD,YGAS,FFF,{hz}", widget.default_monitoring_checklist_note_label.text())
                        send_payload.assert_not_called()
                        continue
                    self.assertIn("最大支持 20 Hz", widget.control_common_feedback_label.text())
                    self.assertIn(f"当前设置为 {hz} Hz", widget.control_common_feedback_label.text())
                    self.assertIn("调整到 1-20 Hz", widget.default_monitoring_checklist_note_label.text())
                    self.assertNotIn(f"FTD,YGAS,FFF,{hz}", widget.default_monitoring_checklist_note_label.text())
                    send_payload.assert_not_called()
                finally:
                    widget.shutdown()
                    widget.close()
                    self.app.processEvents()

    def test_capture_initialization_accepts_ftd_at_registry_limit(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-ftd-limit")
        try:
            widget._set_parse_mode("MODE2")
            widget.device_ftd_hz_edit.setValue(20)
            widget.init_button.click()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 3)
            self.assertEqual(widget._default_monitoring_steps[1].planned_payload, "FTD,YGAS,FFF,20")
            self.assertEqual(widget.control_panel.current_command_id(), "MODE")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_ftd_copy_separates_device_and_expected_frequency(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-ftd-copy-separate")
        try:
            widget._set_parse_mode("MODE2")
            widget.device_ftd_hz_edit.setValue(10)
            widget.stream_hz_edit.setValue(50)
            widget.init_button.click()
            self.app.processEvents()
            with (
                mock.patch.object(widget, "_confirm_high_risk_command", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                self._complete_capture_init_mode_step_until_ftd(widget)

            step = widget._default_monitoring_steps[1]
            detail_text = widget._step_detail_text(step)
            self.assertEqual(step.planned_payload, "FTD,YGAS,FFF,10")
            self.assertIn("本步骤写入设备 FTD 频率：10 Hz", step.last_message)
            self.assertIn("当前期望接收频率：50 Hz，仅用于监测统计/丢帧判断，不会写入设备", step.last_message)
            self.assertIn("本步骤写入设备 FTD 频率：10 Hz", detail_text)
            self.assertIn("当前期望接收频率：50 Hz，仅用于监测统计/丢帧判断，不会写入设备", detail_text)
            self.assertIn("本步骤写入设备 FTD 频率：10 Hz", widget.default_monitoring_checklist_note_label.text())
            self.assertNotIn("stream_hz", widget.default_monitoring_checklist_note_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_checklist_scope_display_uses_payload_not_target_summary_for_single_target(self) -> None:
        widget = self._prepare_write_ready_widget("ui-scope-single")
        try:
            step = widget._build_checklist_step(
                step_index=1,
                step_key="single",
                title="单设备模式",
                command_id="MODE",
                values={"mode": "2"},
                target_summary="故意写成 FFF 广播",
                planned_payload="MODE,YGAS,012,2",
                note_text="test",
                flow_label="测试清单",
                source_page="测试页",
                checklist_kind="capture_init",
            )
            widget._default_monitoring_steps = [step]
            widget._refresh_default_monitoring_checklist()

            self.assertEqual(self._checklist_text(widget, 0, 2), "单设备：012")
            self.assertNotIn("FFF 广播", self._checklist_text(widget, 0, 2))
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_checklist_scope_display_uses_payload_not_target_summary_for_broadcast(self) -> None:
        widget = self._prepare_write_ready_widget("ui-scope-broadcast")
        try:
            step = widget._build_checklist_step(
                step_index=1,
                step_key="broadcast",
                title="广播模式",
                command_id="MODE",
                values={"mode": "2"},
                target_summary="故意改坏的目标展示",
                planned_payload="MODE,YGAS,FFF,2",
                note_text="test",
                flow_label="测试清单",
                source_page="测试页",
                checklist_kind="capture_init",
            )
            widget._default_monitoring_steps = [step]
            widget._refresh_default_monitoring_checklist()

            self.assertIn("FFF 广播", self._checklist_text(widget, 0, 2))
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_readdata_ambiguous_result_uses_friendly_ui_explanation(self) -> None:
        widget = SessionWidget("ui-readdata-ambiguous")
        try:
            widget.show()
            widget._select_control_command_task("READDATA")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 20, 10, 30, 0),
                    command="READDATA,YGAS,012",
                    ok=False,
                    message="原始超时消息",
                    timeout_reason="telemetry_data_ambiguous",
                    observed_telemetry_count=6,
                )
            )

            last_response = widget.control_panel.detail_widget.last_response_label.text()
            self.assertIn("自动上传实时数据", last_response)
            self.assertIn("无法可靠区分", last_response)
            self.assertIn("关闭主动上传", last_response)
            self.assertIn("telemetry=6", last_response)
            self.assertNotIn("原始超时消息", last_response)
            self.assertIn("未判定", widget.control_panel.detail_widget.result_status_label.text())
            self.assertNotIn("失败", widget.control_panel.detail_widget.result_status_label.text())
            self.assertIn("telemetry=6", widget.control_panel.detail_widget.result_evidence_label.text())
            self.assertIn("无法可靠区分", widget.control_panel.detail_widget.result_explain_label.text())
            self.assertIn("关闭主动上传", widget.control_panel.detail_widget.result_explain_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_command_card_preview_makes_auto_silence_and_broadcast_risk_visible(self) -> None:
        widget = self._prepare_write_ready_widget("ui-command-card-silence-hint")
        try:
            widget._select_control_command_task("MODE")
            detail = widget.control_panel.detail_widget
            detail._fields["mode"].setCurrentText("2")
            detail.refresh_preview()

            warning_text = detail.context_warning_label.text()
            self.assertIn("FFF 广播：会影响总线上所有响应设备", warning_text)
            self.assertIn("SETCOMWAY=0", warning_text)
            self.assertIn("进入命令复核窗口", warning_text)
            self.assertIn("暂停目标：设备 012。", warning_text)
            self.assertIn("MODE：改变工作模式", warning_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_setcomway_enable_does_not_warn_about_auto_silence(self) -> None:
        widget = self._prepare_write_ready_widget("ui-setcomway-enable-no-silence")
        try:
            widget._select_control_command_task("SETCOMWAY")
            detail = widget.control_panel.detail_widget
            detail._fields["mode"].setCurrentText("1")
            detail.refresh_preview()

            warning_text = detail.context_warning_label.text()
            self.assertNotIn("进入命令复核窗口", warning_text)
            self.assertNotIn("暂停目标：FFF 广播", warning_text)
            self.assertIn("本次命令会开启主动上传。该命令不会修改 FTD 上传频率。", warning_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_coeff_panel_quiet_read_context_uses_coeff_panel_checkbox(self) -> None:
        widget = self._prepare_write_ready_widget("ui-coeff-quiet-read-context")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("on")
            widget.coeff_panel.select_command("GETCO")
            detail = widget.coeff_panel.detail_widget
            detail.quiet_read_check.setChecked(True)

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                mock.patch.object(detail, "quiet_read_requested", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                detail.refresh_preview()
                widget.coeff_panel._relay_command_request(
                    detail.definition,
                    detail.collect_values(),
                    detail.preview_label.text(),
                    detail._effective_target_id(),
                )
                self.app.processEvents()

            context = send_payload.call_args.kwargs["context"]
            self.assertEqual(context["quiet_read"], "1")
            self.assertEqual(context["source_panel"], "coeff_panel")
            self.assertEqual(context["original_auto_upload_state"], "on")
            self.assertEqual(context["restore_policy"], "restore_after_read")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_signal_panel_checkbox_does_not_inherit_coeff_panel_quiet_read(self) -> None:
        widget = self._prepare_write_ready_widget("ui-signal-quiet-read-isolation")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("off")
            widget.coeff_panel.select_command("GETCO")
            widget.coeff_panel.detail_widget.quiet_read_check.setChecked(True)
            widget.signal_panel.select_command("GETCO")
            signal_detail = widget.signal_panel.detail_widget
            signal_detail.quiet_read_check.setChecked(False)

            with (
                mock.patch.object(QMessageBox, "question") as question,
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                signal_detail.send_button.click()
                self.app.processEvents()

            self.assertFalse(question.called)
            self.assertIsNone(send_payload.call_args.kwargs["context"])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_control_panel_quiet_read_checkbox_does_not_affect_coeff_panel(self) -> None:
        widget = self._prepare_write_ready_widget("ui-control-quiet-read-isolation")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("off")
            widget._select_control_command_task("FTD")
            widget.control_panel.detail_widget.quiet_read_check.setChecked(True)
            widget.coeff_panel.select_command("GETCO")
            widget.coeff_panel.detail_widget.quiet_read_check.setChecked(False)

            with (
                mock.patch.object(QMessageBox, "question") as question,
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget.coeff_panel.detail_widget.refresh_preview()
                widget.coeff_panel._relay_command_request(
                    widget.coeff_panel.detail_widget.definition,
                    widget.coeff_panel.detail_widget.collect_values(),
                    widget.coeff_panel.detail_widget.preview_label.text(),
                    widget.coeff_panel.detail_widget._effective_target_id(),
                )
                self.app.processEvents()

            self.assertFalse(question.called)
            self.assertIsNone(send_payload.call_args.kwargs["context"])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_control_panel_readback_uses_current_card_quiet_read_checkbox(self) -> None:
        widget = self._prepare_write_ready_widget("ui-control-readback-quiet-read")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("on")
            widget._select_control_command_task("FTD")
            detail = widget.control_panel.detail_widget
            detail.quiet_read_check.setChecked(True)

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                detail.refresh_preview()
                widget.control_panel._relay_readback_request(detail.definition)
                self.app.processEvents()

            context = send_payload.call_args.kwargs["context"]
            self.assertEqual(context["quiet_read"], "1")
            self.assertEqual(context["source_panel"], "control_panel")
            self.assertEqual(context["original_auto_upload_state"], "on")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_independent_query_cards_expose_quiet_read_entry(self) -> None:
        widget = self._prepare_write_ready_widget("ui-query-quiet-read-visibility")
        try:
            widget.show()
            self.app.processEvents()
            for panel, command_id in (
                (widget.control_panel, "MODE_QUERY"),
                (widget.control_panel, "FTD_QUERY"),
                (widget.signal_panel, "SENTEMP1_QUERY"),
                (widget.signal_panel, "AVERAGE1_QUERY"),
            ):
                with self.subTest(command_id=command_id):
                    if panel is widget.control_panel:
                        widget._select_control_command_task(command_id)
                    else:
                        widget.pages.setCurrentIndex(widget.signal_tab_index)
                    panel.select_command(command_id)
                    detail = panel.detail_widget
                    detail.refresh_preview()
                    self.assertFalse(detail.quiet_read_check.isHidden())
                    self.assertEqual(detail.quiet_read_check.text(), "暂停上传后读取")
                    self.assertIn("读取前临时发送 SETCOMWAY=0", detail.quiet_read_check.toolTip())
                    self.assertIn("不会停止设备测量", detail.quiet_read_check.toolTip())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_query_card_quiet_read_checked_propagates_operation_context(self) -> None:
        widget = self._prepare_write_ready_widget("ui-query-quiet-read-context")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("on")
            widget._select_control_command_task("MODE_QUERY")
            detail = widget.control_panel.detail_widget
            detail.quiet_read_check.setChecked(True)

            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                mock.patch.object(detail, "quiet_read_requested", return_value=True),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                detail.send_button.click()
                self.app.processEvents()

            context = send_payload.call_args.kwargs["context"]
            self.assertEqual(context["quiet_read"], "1")
            self.assertTrue(context["quiet_read_requested"])
            self.assertEqual(context["source_panel"], "control_panel")
            self.assertEqual(context["original_auto_upload_state"], "on")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_query_card_without_quiet_read_does_not_request_auto_silence(self) -> None:
        widget = self._prepare_write_ready_widget("ui-query-no-quiet-read")
        try:
            widget.show()
            self.app.processEvents()
            widget._set_auto_upload_state("on")
            widget.pages.setCurrentIndex(widget.signal_tab_index)
            widget.signal_panel.select_command("SENTEMP1_QUERY")
            detail = widget.signal_panel.detail_widget
            detail.quiet_read_check.setChecked(False)

            with (
                mock.patch.object(QMessageBox, "question") as question,
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                detail.send_button.click()
                self.app.processEvents()

            self.assertFalse(question.called)
            self.assertIsNone(send_payload.call_args.kwargs["context"])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_unknown_state_requires_restore_prompt(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-unknown-prompt")
        try:
            with mock.patch.object(
                QMessageBox,
                "question",
                side_effect=[QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.Yes],
            ) as question:
                context = widget._quiet_read_context_for_payload(
                    "GETCO,YGAS,012,1",
                    {
                        "quiet_read_requested": True,
                        "source_panel": "coeff_panel",
                        "original_auto_upload_state": "unknown",
                    },
                )

            self.assertEqual(question.call_count, 2)
            self.assertIn("读取后是否恢复主动上传", question.call_args_list[0].args[2])
            self.assertEqual(context["restore_policy"], "user_confirmed_restore")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_temporarily_silenced_quiet_read_status_uses_preserved_copy(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-preserved-copy")
        try:
            widget.coeff_panel.select_command("GETCO")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 12, 0),
                    command="GETCO,YGAS,012,1",
                    ok=True,
                    message="系数读取成功。",
                    response_kind="coefficient",
                    response_device_id="012",
                    parsed_payload={"device_id": "012"},
                    auto_upload_state="temporarily_silenced",
                    original_auto_upload_state="temporarily_silenced",
                    restore_policy="preserve_existing_silence",
                    restore_result="preserved_existing_silence",
                )
            )

            self.assertIn("保持主动上传已临时暂停", widget.monitor_auto_upload_state_label.text())
            self.assertNotIn("读取后状态：未确认", widget.monitor_auto_upload_state_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_process_feedback_shows_pause_read_and_restore_stages(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-progress")
        try:
            definition = widget.registry.get("GETCO")
            with (
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload"),
            ):
                widget._handle_command_request(
                    definition,
                    {"index": "1"},
                    "GETCO,YGAS,012,1",
                    "012",
                    {
                        "quiet_read_requested": True,
                        "source_panel": "coeff_panel",
                        "original_auto_upload_state": "on",
                    },
                )

            self.assertIn("正在临时暂停主动上传", widget.control_common_feedback_label.text())

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 8, 0),
                    command="SETCOMWAY,YGAS,012,0",
                    ok=True,
                    message="ACK 成功，主动上传已临时暂停。",
                    action_type="auto_silence",
                    parent_command="GETCO,YGAS,012,1",
                    command_target_id="012",
                    expected_device_id="012",
                    response_device_id="012",
                    effective_scope="single",
                    source_page="主动上传暂停保护",
                    auto_upload_state="temporarily_silenced",
                    original_auto_upload_state="on",
                    restore_policy="restore_after_read",
                    restore_result="pending_restore",
                )
            )
            self.assertIn("正在读取", widget.control_common_feedback_label.text())

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 8, 1),
                    command="SETCOMWAY,YGAS,012,1",
                    ok=True,
                    message="ACK 成功，已恢复主动上传。",
                    action_type="auto_silence_restore",
                    parent_command="GETCO,YGAS,012,1",
                    command_target_id="012",
                    expected_device_id="012",
                    response_device_id="012",
                    effective_scope="single",
                    source_page="恢复主动上传",
                    auto_upload_state="on",
                    original_auto_upload_state="on",
                    restore_policy="restore_after_read",
                    restore_attempted=True,
                    restore_result="restored",
                )
            )
            self.assertIn("正在恢复主动上传", widget.control_common_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_restore_failure_updates_status_strip_and_result_area(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-restore-fail")
        try:
            widget.show()
            self.app.processEvents()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            widget.coeff_panel.select_command("GETCO")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 10, 0),
                    command="SETCOMWAY,YGAS,012,1",
                    ok=False,
                    message="恢复主动上传失败：未收到 SETCOMWAY ACK。",
                    action_type="auto_silence_restore",
                    parent_command="GETCO,YGAS,012,1",
                    command_target_id="012",
                    expected_device_id="012",
                    response_device_id="012",
                    effective_scope="single",
                    source_page="恢复主动上传",
                    auto_upload_state="off",
                    original_auto_upload_state="on",
                    restore_policy="restore_after_read",
                    restore_attempted=True,
                    restore_result="restore_failed",
                )
            )

            self.assertIn("自动上传状态：恢复失败", widget.monitor_auto_upload_state_label.text())
            self.assertIn("原状态：已开启", widget.monitor_auto_upload_state_label.text())
            self.assertIn("读取后状态：恢复失败", widget.monitor_auto_upload_state_label.text())
            self.assertIn("恢复主动上传失败", widget.coeff_panel.detail_widget.last_response_label.text())
            self.assertIn("手动执行 SETCOMWAY=1", widget.coeff_panel.detail_widget.last_response_label.text())
            self.assertIn("失败", widget.coeff_panel.detail_widget.result_status_label.text())
            self.assertFalse(widget.coeff_panel.detail_widget.restore_retry_button.isHidden())
            self.assertTrue(widget.coeff_panel.detail_widget.restore_retry_button.isEnabled())
            self.assertFalse(widget.coeff_panel.detail_widget.keep_upload_closed_button.isHidden())
            self.assertFalse(widget.coeff_panel.detail_widget.view_command_log_button.isHidden())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_restore_failure_retry_button_resends_setcomway_and_updates_state(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-restore-retry")
        try:
            widget.show()
            self.app.processEvents()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            widget.coeff_panel.select_command("GETCO")
            self._inject_restore_failure(widget)

            detail = widget.coeff_panel.detail_widget
            with (
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "validate_restore_retry_payload", return_value=(True, "")),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                detail.restore_retry_button.click()
                self.app.processEvents()

            self.assertEqual(send_payload.call_args.args[0], "SETCOMWAY,YGAS,012,1")
            self.assertEqual(send_payload.call_args.kwargs["expectation"], "ack")
            self.assertIn("正在恢复主动上传", widget.control_common_feedback_label.text())

            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 10, 2),
                    command="SETCOMWAY,YGAS,012,1",
                    ok=True,
                    message="ACK 成功。",
                    response_device_id="012",
                )
            )

            self.assertIn("自动上传状态：已开启", widget.monitor_auto_upload_state_label.text())
            self.assertIn("已恢复主动上传", detail.last_response_label.text())
            self.assertEqual(widget._session_change_history[0].action_type, "auto_silence_restore")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_restore_retry_button_disables_when_not_allowed(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-restore-retry-disabled")
        try:
            widget.show()
            self.app.processEvents()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            widget.coeff_panel.select_command("GETCO")
            self._inject_restore_failure(widget)

            detail = widget.coeff_panel.detail_widget
            widget.read_only_lock_check.setChecked(True)
            widget._apply_permission_mode()
            self.app.processEvents()
            self.assertFalse(detail.restore_retry_button.isEnabled())
            self.assertIn("只读", detail.restore_action_hint_label.text())

            widget.read_only_lock_check.setChecked(False)
            widget.permission_combo.setCurrentText("READ_ONLY")
            widget._apply_permission_mode()
            self.app.processEvents()
            self.assertFalse(detail.restore_retry_button.isEnabled())
            self.assertIn("至少需要", detail.restore_action_hint_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_restore_retry_handler_blocks_invalid_requests(self) -> None:
        scenarios = (
            (
                "read_only_lock",
                lambda widget: (
                    widget.read_only_lock_check.setChecked(True),
                    widget._apply_permission_mode(),
                    self.app.processEvents(),
                ),
                "SETCOMWAY,YGAS,012,1",
                "只读",
            ),
            (
                "permission_downgraded",
                lambda widget: (
                    widget.permission_combo.setCurrentText("READ_ONLY"),
                    widget._apply_permission_mode(),
                    self.app.processEvents(),
                ),
                "SETCOMWAY,YGAS,012,1",
                "至少需要",
            ),
            (
                "disconnected",
                lambda widget: (
                    setattr(widget, "connected", False),
                    setattr(widget.controller, "connected", False),
                    self.app.processEvents(),
                ),
                "SETCOMWAY,YGAS,012,1",
                "未连接",
            ),
            (
                "replay_blocked",
                lambda widget: (
                    setattr(widget, "replay_running", True),
                    self.app.processEvents(),
                ),
                "SETCOMWAY,YGAS,012,1",
                "回放",
            ),
            (
                "invalid_payload",
                lambda widget: self.app.processEvents(),
                "SETCOMWAY,YGAS,012,0",
                "SETCOMWAY=1",
            ),
            (
                "missing_candidate",
                lambda widget: (
                    widget._restore_retry_candidates.clear(),
                    self.app.processEvents(),
                ),
                "SETCOMWAY,YGAS,012,1",
                "匹配的恢复失败上下文",
            ),
        )
        for name, prepare, payload, expected_reason in scenarios:
            with self.subTest(name=name):
                widget = self._prepare_write_ready_widget(f"ui-quiet-read-restore-handler-{name}")
                try:
                    widget.show()
                    self.app.processEvents()
                    widget.pages.setCurrentIndex(widget.coeff_tab_index)
                    self.app.processEvents()
                    widget.coeff_panel.select_command("GETCO")
                    self._inject_restore_failure(widget)
                    prepare(widget)

                    detail = widget.coeff_panel.detail_widget
                    with (
                        mock.patch.object(widget.controller, "send_payload") as send_payload,
                        mock.patch.object(QMessageBox, "warning") as warning,
                    ):
                        detail.retry_restore_requested.emit(payload)
                        self.app.processEvents()

                    send_payload.assert_not_called()
                    self.assertTrue(warning.called)
                    self.assertIn(expected_reason, warning.call_args.args[2])
                finally:
                    widget.shutdown()
                    widget.close()
                    self.app.processEvents()

    def test_keep_auto_upload_off_is_recorded_in_logs_and_exports(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-keep-off-record")
        try:
            widget.show()
            self.app.processEvents()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            widget.coeff_panel.select_command("GETCO")
            self._inject_restore_failure(widget)

            detail = widget.coeff_panel.detail_widget
            detail.keep_upload_closed_button.click()
            self.app.processEvents()

            command_entry = widget._command_log_history[0]
            self.assertEqual(command_entry.action_type, "keep_auto_upload_off")
            self.assertEqual(command_entry.action_label_zh, "保持主动上传关闭")
            self.assertEqual(command_entry.result, "用户确认")
            self.assertIn("系统不会再次自动发送 SETCOMWAY=1", command_entry.detail_text)

            journal_entry = widget._session_change_history[0]
            self.assertEqual(journal_entry.action_type, "keep_auto_upload_off")
            self.assertFalse(journal_entry.is_system_action)
            self.assertEqual(journal_entry.target_value, "主动上传保持关闭")
            self.assertEqual(journal_entry.after_value, "用户确认保持关闭")
            self.assertEqual(journal_entry.verification_status, "decision_recorded")
            widget.controller.frames.append(
                ParsedFrame(
                    timestamp=datetime(2026, 4, 21, 9, 10, 5),
                    raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                    device_id="012",
                    mode=1,
                    fields={"co2_ppm": 488.879},
                    status="0001",
                )
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                package_dir = export_session_package(
                    session_name=widget.session_name,
                    frames=list(widget.controller.frames),
                    raw_records=list(widget.controller.raw_records),
                    config=widget._build_config(),
                    parameter_change_entries=list(widget._session_change_history),
                    command_entries=list(widget._command_log_history),
                    output_dir=Path(temp_dir),
                    logger_path=widget.controller.log_path,
                    note="keep-off export",
                )
                command_log_csv = Path(package_dir / "command_log.csv").read_text(encoding="utf-8-sig")
                journal_json = Path(package_dir / "parameter_change_journal.json").read_text(encoding="utf-8")
                summary_json = json.loads(Path(package_dir / "session_summary.json").read_text(encoding="utf-8"))

            self.assertIn("action_label_zh", command_log_csv)
            self.assertIn("保持主动上传关闭", command_log_csv)
            self.assertIn("keep_auto_upload_off", journal_json)
            self.assertEqual(summary_json["verified_consistent_count"], 0)
            self.assertEqual(summary_json["ack_only_unverified_count"], 0)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_quiet_read_restore_retry_respects_controller_guard(self) -> None:
        widget = self._prepare_write_ready_widget("ui-quiet-read-restore-controller-guard")
        try:
            widget.show()
            self.app.processEvents()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            widget.coeff_panel.select_command("GETCO")
            self._inject_restore_failure(widget)
            widget.controller.connected = False

            detail = widget.coeff_panel.detail_widget
            with (
                mock.patch.object(widget.controller, "send_payload") as send_payload,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                detail.retry_restore_requested.emit("SETCOMWAY,YGAS,012,1")
                self.app.processEvents()

            send_payload.assert_not_called()
            self.assertTrue(warning.called)
            self.assertIn("未连接", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_setcomway_disable_confirmation_mentions_closing_auto_upload_without_pre_silence(self) -> None:
        widget = self._prepare_write_ready_widget("ui-setcomway-disable-confirm")
        try:
            definition = widget.registry.get("SETCOMWAY")
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
                widget._confirm_high_risk_command(definition, "SETCOMWAY,YGAS,FFF,0", {"mode": "0"})

            message = question.call_args.args[2]
            self.assertIn("本次命令会关闭主动上传", message)
            self.assertNotIn("进入命令复核窗口", message)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_mode_confirmation_mentions_pre_silence_and_setcomway_enable_does_not(self) -> None:
        widget = self._prepare_write_ready_widget("ui-confirmation-pre-silence")
        try:
            mode_definition = widget.registry.get("MODE")
            upload_definition = widget.registry.get("SETCOMWAY")
            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
                widget._confirm_high_risk_command(mode_definition, "MODE,YGAS,012,2", {"mode": "2"})
                mode_message = question.call_args.args[2]
                widget._confirm_high_risk_command(upload_definition, "SETCOMWAY,YGAS,012,1", {"mode": "1"})
                upload_message = question.call_args.args[2]

            self.assertIn("SETCOMWAY=0", mode_message)
            self.assertIn("进入命令复核窗口", mode_message)
            self.assertIn("本次命令会开启主动上传。该命令不会修改 FTD 上传频率。", upload_message)
            self.assertNotIn("进入命令复核窗口", upload_message)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_capture_initialization_copy_reflects_listen_and_manual_modes(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-copy-modes")
        try:
            widget._set_acquisition_mode("LISTEN")
            widget.init_button.click()
            self.app.processEvents()
            self.assertIn("FTD 频率写入", widget._checklist_intro_note("capture_init"))
            self.assertIn("SETCOMWAY=1 主动上传开启", widget._checklist_intro_note("capture_init"))

            widget._set_acquisition_mode("POLL")
            widget.init_button.click()
            self.app.processEvents()
            self.assertIn("手动读取初始化", widget._checklist_intro_note("capture_init"))
            self.assertIn("关闭主动上传", widget._checklist_intro_note("capture_init"))
            self.assertIn("READDATA 或手动读取", widget._checklist_intro_note("capture_init"))
            self.assertEqual(widget._default_monitoring_steps[-1].title, "关闭主动上传")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_checklist_headers_are_humanized_and_detail_keeps_full_payload(self) -> None:
        widget = self._prepare_write_ready_widget("ui-checklist-headers")
        try:
            widget.init_button.click()
            self.app.processEvents()

            self.assertEqual(self._checklist_header_text(widget, 0), "步骤")
            self.assertEqual(self._checklist_header_text(widget, 1), "动作")
            self.assertEqual(self._checklist_header_text(widget, 2), "目标")
            self.assertEqual(self._checklist_header_text(widget, 4), "最近证据")
            self.assertNotIn("planned_payload", [self._checklist_header_text(widget, index) for index in range(5)])
            self.assertIn("计划命令：FTD,YGAS,FFF,10", widget.default_monitoring_checklist_note_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_silence_system_action_is_recorded_structurally(self) -> None:
        widget = self._prepare_write_ready_widget("ui-auto-silence-log")
        try:
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 0, 0),
                    command="SETCOMWAY,YGAS,FFF,0",
                    ok=True,
                    message="ACK 成功，主动上传已临时暂停。",
                    action_type="auto_silence",
                    action_label_zh="临时暂停主动上传",
                    parent_command="MODE,YGAS,FFF,2",
                    command_target_id="FFF",
                    expected_device_id="012",
                    response_device_id="012",
                    effective_scope="broadcast",
                    source_page="主动上传暂停保护",
                    original_auto_upload_state="on",
                    restore_policy="restore_after_read",
                    restore_result="pending_restore",
                )
            )

            entry = widget._command_log_history[0]
            self.assertEqual(entry.action_type, "auto_silence")
            self.assertEqual(entry.payload, "SETCOMWAY,YGAS,FFF,0")
            self.assertEqual(entry.parent_command, "MODE,YGAS,FFF,2")
            self.assertEqual(entry.command_target_id, "FFF")
            self.assertEqual(entry.response_device_id, "012")
            self.assertEqual(entry.source_page, "主动上传暂停保护")
            self.assertEqual(entry.action_label_zh, "临时暂停主动上传")
            self.assertEqual(entry.original_auto_upload_state, "on")
            self.assertEqual(entry.restore_policy, "restore_after_read")
            self.assertFalse(entry.restore_attempted)
            self.assertEqual(entry.restore_result, "pending_restore")
            self.assertIn("临时暂停主动上传", self._table_text(widget, 0, 1))

            journal_entry = widget._session_change_history[0]
            self.assertEqual(journal_entry.result_text, "ACK 成功（未复核）")
            self.assertEqual(journal_entry.verification_status, "ack_only_unverified")
            self.assertEqual(journal_entry.after_value, "ACK 已收到，未读回确认")
            self.assertEqual(journal_entry.action_label_zh, "临时暂停主动上传")
            self.assertEqual(journal_entry.original_auto_upload_state, "on")
            self.assertEqual(journal_entry.restore_policy, "restore_after_read")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_silence_restore_system_action_uses_paused_before_value(self) -> None:
        widget = self._prepare_write_ready_widget("ui-auto-silence-restore-log")
        try:
            widget.coeff_panel.select_command("GETCO")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime(2026, 4, 21, 9, 0, 1),
                    command="SETCOMWAY,YGAS,012,1",
                    ok=True,
                    message="ACK 成功，已恢复主动上传。",
                    action_type="auto_silence_restore",
                    action_label_zh="恢复主动上传",
                    parent_command="GETCO,YGAS,012,1",
                    command_target_id="012",
                    expected_device_id="012",
                    response_device_id="012",
                    effective_scope="single",
                    source_page="恢复主动上传",
                    auto_upload_state="on",
                    original_auto_upload_state="on",
                    restore_policy="restore_after_read",
                    restore_attempted=True,
                    restore_result="restored",
                )
            )

            journal_entry = widget._session_change_history[0]
            self.assertEqual(journal_entry.before_value, "主动上传已临时暂停")
            self.assertEqual(journal_entry.target_value, "主动上传已开启")
            self.assertEqual(journal_entry.after_value, "ACK 已收到，未读回确认")
            self.assertEqual(journal_entry.original_auto_upload_state, "on")
            self.assertEqual(journal_entry.verification_status, "ack_only_unverified")
            self.assertIn("原始主动上传状态：已开启", journal_entry.detail_text)
            self.assertIn("恢复结果：已恢复", journal_entry.detail_text)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_fff_broadcast_risk_is_visible_in_checklist_and_confirmation(self) -> None:
        widget = self._prepare_write_ready_widget("ui-default-config-fff-risk")
        try:
            widget._apply_default_monitoring_config()
            self.assertIn("FFF 广播", widget.default_monitoring_checklist_note_label.text())
            self.assertIn("总线上所有设备", widget.default_monitoring_checklist_note_label.text())

            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(QInputDialog, "getText", return_value=("确认广播校准", True)) as typed,
                mock.patch.object(QMessageBox, "question") as question,
            ):
                widget._confirm_high_risk_command(definition, "MODE,YGAS,FFF,2")
            self.assertTrue(typed.called)
            self.assertFalse(question.called)
            self.assertIn("确认广播校准", typed.call_args.args[2])
            self.assertIn("payload：MODE,YGAS,FFF,2", typed.call_args.args[2])
            self.assertIn("广播 target：FFF", typed.call_args.args[2])
            self.assertIn("当前会话 target：012", typed.call_args.args[2])
            self.assertIn("当前在线设备：012", typed.call_args.args[2])
            self.assertIn("MODE2 FFF", typed.call_args.args[2])
            self.assertIn("SETCOMWAY=0", typed.call_args.args[2])
            self.assertIn("进入命令复核窗口", typed.call_args.args[2])
            self.assertIn("暂停目标：设备 012。", typed.call_args.args[2])

            with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as single_question:
                widget._confirm_high_risk_command(definition, "MODE,YGAS,012,2")
            self.assertIn("校准模式", single_question.call_args.args[2])
            self.assertNotIn("总线上所有设备", single_question.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_mode2_fff_typed_confirmation_can_show_explicit_broadcast_silence(self) -> None:
        widget = self._prepare_write_ready_widget("ui-mode2-fff-explicit-broadcast-silence")
        try:
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(QInputDialog, "getText", return_value=("确认广播校准", True)) as typed,
                mock.patch.object(QMessageBox, "question") as question,
            ):
                widget._confirm_high_risk_command(
                    definition,
                    "MODE,YGAS,FFF,2",
                    {"mode": "2", "allow_broadcast_silence": "1"},
                )

            self.assertTrue(typed.called)
            self.assertFalse(question.called)
            self.assertIn("暂停目标：FFF 广播，会影响总线上所有响应设备。", typed.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_mode2_fff_typed_confirmation_blocks_send_when_phrase_is_wrong(self) -> None:
        widget = self._prepare_write_ready_widget("ui-mode2-fff-typed-block")
        try:
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(QInputDialog, "getText", return_value=("wrong phrase", True)),
                mock.patch.object(QMessageBox, "warning") as warning,
                mock.patch.object(widget.controller, "update_config") as update_config,
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            send_payload.assert_not_called()
            update_config.assert_not_called()
            self.assertFalse(widget._pending_readback_requests)
            self.assertIn("确认短语不匹配", warning.call_args.args[2])
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_mode2_fff_typed_confirmation_allows_send_when_chinese_phrase_is_correct(self) -> None:
        widget = self._prepare_write_ready_widget("ui-mode2-fff-typed-pass")
        try:
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(QInputDialog, "getText", return_value=("确认广播校准", True)) as typed,
                mock.patch.object(QMessageBox, "question") as question,
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            self.assertTrue(typed.called)
            self.assertFalse(question.called)
            self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_mode2_fff_typed_confirmation_allows_send_when_ascii_phrase_is_correct(self) -> None:
        widget = self._prepare_write_ready_widget("ui-mode2-fff-typed-pass-ascii")
        try:
            definition = widget.registry.get("MODE")
            with (
                mock.patch.object(QInputDialog, "getText", return_value=("MODE2 FFF", True)) as typed,
                mock.patch.object(QMessageBox, "question") as question,
                mock.patch.object(widget.controller, "update_config"),
                mock.patch.object(widget.controller, "send_payload") as send_payload,
            ):
                widget._handle_command_request(definition, {"mode": "2"}, "MODE,YGAS,FFF,2", "FFF")

            self.assertTrue(typed.called)
            self.assertFalse(question.called)
            self.assertEqual(send_payload.call_args_list[0].args[0], "MODE,YGAS,012")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_non_mode2_or_single_target_does_not_require_broadcast_typed_confirmation(self) -> None:
        widget = self._prepare_write_ready_widget("ui-mode2-typed-scope")
        try:
            mode_definition = widget.registry.get("MODE")
            upload_definition = widget.registry.get("SETCOMWAY")
            with (
                mock.patch.object(QInputDialog, "getText") as typed,
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question,
            ):
                widget._confirm_high_risk_command(mode_definition, "MODE,YGAS,012,2")
                widget._confirm_high_risk_command(upload_definition, "SETCOMWAY,YGAS,FFF,1")

            self.assertFalse(typed.called)
            self.assertEqual(question.call_count, 2)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_export_history_success_message_mentions_recent_cache(self) -> None:
        widget = self._prepare_write_ready_widget("ui-export-history-copy")
        try:
            widget.controller.frames.extend(
                [
                    ParsedFrame(
                        timestamp=datetime(2026, 4, 21, 9, 0, 0),
                        raw="YGAS,012,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                        device_id="012",
                        mode=1,
                        fields={"co2_ppm": 488.879},
                        status="0001",
                    )
                ]
            )
            with (
                mock.patch.object(QFileDialog, "getSaveFileName", return_value=("D:\\temp\\recent.csv", "CSV Files (*.csv)")),
                mock.patch.object(widget.controller, "export_history", return_value="D:\\temp\\recent.csv"),
                mock.patch.object(QMessageBox, "information") as info,
            ):
                widget._export_history()

            message = info.call_args.args[2]
            self.assertIn("结构化数据范围：最近缓存", message)
            self.assertIn("如需全量请使用“导出会话包”", message)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_export_session_package_success_message_includes_scope_and_counts(self) -> None:
        widget = self._prepare_write_ready_widget("ui-export-session-scope")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                for scope, expected_scope_text, expect_recent_note in (
                    ("recent_cache", "最近缓存", True),
                    ("full", "全量会话数据", False),
                ):
                    output_dir = Path(temp_dir) / f"session_{scope}"

                    def fake_export_session_package(**_kwargs: object) -> Path:
                        output_dir.mkdir(parents=True, exist_ok=True)
                        (output_dir / "session_summary.json").write_text(
                            '{\n'
                            f'  "structured_export_scope": "{scope}",\n'
                            '  "structured_frame_count_exported": 128,\n'
                            '  "raw_rx_count_total": 512,\n'
                            '  "raw_tx_count_total": 42\n'
                            '}\n',
                            encoding="utf-8",
                        )
                        return output_dir

                    with (
                        mock.patch.object(QFileDialog, "getExistingDirectory", return_value=temp_dir),
                        mock.patch("ygas_monitor.ui.session_widget.export_session_package", side_effect=fake_export_session_package),
                        mock.patch.object(QMessageBox, "information") as info,
                    ):
                        widget._export_session_package()

                    message = info.call_args.args[2]
                    self.assertIn(f"结构化数据范围：{expected_scope_text}", message)
                    self.assertIn("导出结构化帧数：128", message)
                    self.assertIn("raw RX 总数：512", message)
                    self.assertIn("raw TX 总数：42", message)
                    if expect_recent_note:
                        self.assertIn("当前结构化 CSV 来自最近缓存", message)
                    else:
                        self.assertNotIn("当前结构化 CSV 来自最近缓存", message)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_prepare_callback_skips_when_connection_is_closed_before_delay_fires(self) -> None:
        widget = SessionWidget("ui-default-config-auto-prepare-disconnect")
        try:
            widget.auto_apply_default_config_check.setChecked(True)
            scheduled_callbacks: list[object] = []
            with mock.patch.object(QTimer, "singleShot", side_effect=lambda _ms, callback: scheduled_callbacks.append(callback)):
                widget._handle_connection_state(True, "session.log")
            self.assertEqual(len(scheduled_callbacks), 1)

            widget._handle_connection_state(False, "session.log")
            callback = scheduled_callbacks.pop()
            callback()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 0)
            self.assertFalse(widget._default_config_scheduled)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_auto_prepare_callback_skips_when_option_is_unchecked_before_delay_fires(self) -> None:
        widget = SessionWidget("ui-default-config-auto-prepare-unchecked")
        try:
            widget.auto_apply_default_config_check.setChecked(True)
            scheduled_callbacks: list[object] = []
            with mock.patch.object(QTimer, "singleShot", side_effect=lambda _ms, callback: scheduled_callbacks.append(callback)):
                widget._handle_connection_state(True, "session.log")
            self.assertEqual(len(scheduled_callbacks), 1)

            widget.auto_apply_default_config_check.setChecked(False)
            callback = scheduled_callbacks.pop()
            callback()
            self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 0)
            self.assertFalse(widget._default_config_scheduled)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_session_change_overview_keeps_recent_preview_but_summary_counts_full_history(self) -> None:
        widget = SessionWidget("ui-session-change-history")
        try:
            for index in range(25):
                self._record_change_entry(
                    widget,
                    "MODE",
                    "一致",
                    target_device_id="012",
                    before_value=f"MODE{index}",
                    target_value=f"MODE{index + 1}",
                    after_value=f"MODE{index + 1}",
                    detail_text=f"change-{index}",
                )
            self.app.processEvents()

            self.assertEqual(len(widget._session_change_entries), 20)
            self.assertEqual(len(widget._session_change_history), 25)
            self.assertEqual(widget.change_overview_table.rowCount(), 20)
            self.assertEqual(widget._session_change_counts()["total"], 25)
            self.assertIn("25", widget.change_summary_message_label.text())
            self.assertIn("25", widget._export_diag_task_status()[0])
            self.assertEqual(self._table_text(widget, 0, 7), "change-24")
            preview_details = [self._table_text(widget, row, 7) for row in range(widget.change_overview_table.rowCount())]
            self.assertNotIn("change-0", preview_details)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_coeff_page_is_table_first_and_keeps_underlying_command_workspace(self) -> None:
        widget = SessionWidget("ui-coeff-workspace")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()
            self.assertEqual(widget.coeff_workspace_box.title(), "系数管理工作台")
            self.assertIsInstance(widget.coeff_workspace_table, QTableWidget)
            self.assertEqual(widget.coeff_workspace_table.rowCount(), 9)
            self.assertFalse(widget.coeff_detail_box.isChecked())
            self.assertEqual(widget.coeff_workspace_table.item(0, 0).text(), "系数组 1")
            self.assertEqual(widget.coeff_workspace_table.item(0, 4).text(), "待读取")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_high_frequency_numeric_inputs_use_spin_boxes_with_reasonable_ranges(self) -> None:
        widget = SessionWidget("ui-numeric-spinboxes")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            self.app.processEvents()
            self.assertIsInstance(widget.device_ftd_hz_edit, QSpinBox)
            self.assertEqual(widget.device_ftd_hz_edit.minimum(), 1)
            self.assertEqual(widget.device_ftd_hz_edit.maximum(), 20)
            self.assertEqual(widget.device_ftd_hz_edit.suffix().strip(), "Hz")
            self.assertIsInstance(widget.stream_hz_edit, QSpinBox)
            self.assertEqual(widget.stream_hz_edit.minimum(), 1)
            self.assertEqual(widget.stream_hz_edit.maximum(), 50)
            self.assertEqual(widget.stream_hz_edit.suffix().strip(), "Hz")

            self.assertIsInstance(widget.poll_interval_edit, QSpinBox)
            self.assertEqual(widget.poll_interval_edit.minimum(), 50)
            self.assertEqual(widget.poll_interval_edit.maximum(), 5000)
            self.assertEqual(widget.poll_interval_edit.singleStep(), 50)

            self.assertIsInstance(widget.command_timeout_edit, QSpinBox)
            self.assertEqual(widget.command_timeout_edit.minimum(), 300)
            self.assertEqual(widget.command_timeout_edit.maximum(), 10000)
            self.assertEqual(widget.command_timeout_edit.suffix().strip(), "ms")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_test_session_ui_file_has_unique_test_method_names(self) -> None:
        names = _session_ui_test_method_names()
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)

        self.assertFalse(duplicates, f"duplicate test method names found: {duplicates}")

    def test_monitor_page_geometry_prioritizes_chart_area_on_small_and_medium_screens(self) -> None:
        widget = SessionWidget("ui-monitor-geometry")
        try:
            monitor_page = widget.pages.widget(widget.monitor_tab_index)
            for width, height, min_ratio in [(1280, 720, 0.55), (1366, 768, 0.55), (1600, 900, 0.55)]:
                widget.resize(width, height)
                widget.show()
                widget.pages.setCurrentIndex(widget.monitor_tab_index)
                self.app.processEvents()
                self.app.processEvents()

                visible_cards = [card for card in widget.data_cards._cards if not card.isHidden()]
                page_height = max(1, monitor_page.height())
                chart_height = widget.chart_panel.height()

                self.assertLessEqual(widget.session_header_box.height(), 64)
                self.assertEqual(len(visible_cards), 6)
                self.assertEqual(widget.freeze_button.text(), "冻结显示")
                self.assertTrue(widget.monitor_action_mode1.text().startswith("预填"))
                self.assertTrue(widget.monitor_action_mode2.text().startswith("预填"))
                self.assertTrue(widget.monitor_action_upload_on.text().startswith("预填"))
                self.assertTrue(widget.monitor_action_upload_off.text().startswith("预填"))
                self.assertFalse(widget.chart_config_group.isVisible())
                self.assertTrue((not widget.monitor_aux_container.isVisible()) or widget.monitor_aux_container.height() == 0)
                self.assertGreaterEqual(chart_height / page_height, min_ratio)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_control_page_has_scroll_area_or_bottom_command_card_accessible(self) -> None:
        widget = SessionWidget("ui-control-scroll")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.control_tab_index)
            self.app.processEvents()

            scroll = self._page_scroll_area(widget, widget.control_tab_index)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            self.assertTrue(scroll.widgetResizable())
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertTrue(widget.control_panel.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_coeff_page_has_scroll_area_or_bottom_detail_accessible(self) -> None:
        widget = SessionWidget("ui-coeff-scroll")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.coeff_tab_index)
            self.app.processEvents()

            scroll = self._page_scroll_area(widget, widget.coeff_tab_index)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            self.assertTrue(scroll.widgetResizable())
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertTrue(widget.coeff_detail_box.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_signal_page_has_scroll_area(self) -> None:
        widget = SessionWidget("ui-signal-scroll")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.signal_tab_index)
            self.app.processEvents()

            scroll = self._page_scroll_area(widget, widget.signal_tab_index)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            self.assertTrue(scroll.widgetResizable())
            self.assertTrue(widget.signal_panel.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_expert_page_has_scroll_area(self) -> None:
        widget = SessionWidget("ui-expert-scroll")
        try:
            widget.permission_combo.setCurrentText("EXPERT")
            widget.show_expert_check.setChecked(True)
            widget._apply_permission_mode()
            widget.show()
            widget.pages.setCurrentIndex(widget.expert_tab_index)
            self.app.processEvents()

            scroll = self._page_scroll_area(widget, widget.expert_tab_index)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            self.assertTrue(scroll.widgetResizable())
            scroll.verticalScrollBar().setValue(0)
            self.app.processEvents()
            self.assertTrue(widget.raw_command_edit.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_export_page_has_scroll_area_or_bottom_overview_accessible(self) -> None:
        widget = SessionWidget("ui-export-scroll")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.export_tab_index)
            self.app.processEvents()

            scroll = self._page_scroll_area(widget, widget.export_tab_index)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            self.assertTrue(scroll.widgetResizable())
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            self.app.processEvents()
            self.assertTrue(widget.change_overview_table.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_all_pages_accessible_at_1280x720(self) -> None:
        self._assert_pages_accessible_at_size(1280, 720)

    def test_all_pages_accessible_at_1366x768(self) -> None:
        self._assert_pages_accessible_at_size(1366, 768)

    def test_hidden_monitor_page_defers_card_refresh_until_return(self) -> None:
        widget = SessionWidget("ui-hidden-refresh")
        try:
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            self.app.processEvents()

            frame = ParsedFrame(
                timestamp=datetime.now() - timedelta(seconds=0.6),
                raw="sample",
                device_id="001",
                mode=2,
                status="0001",
                fields={
                    "co2_ppm": 1.234,
                    "h2o_mmol": 2.345,
                    "temperature_c": 26.5,
                    "pressure_kpa": 101.32,
                    "active_alarm_count": 0,
                    "co2_ratio_raw": 0.9012,
                    "co2_ratio_f": 0.9001,
                    "h2o_ratio_raw": 0.7012,
                    "h2o_ratio_f": 0.7034,
                },
            )

            widget._handle_frame(frame)
            self.app.processEvents()
            self.assertEqual(widget.data_cards._cards[0].value_label.text(), "--")

            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()
            visible_cards = [card for card in widget.data_cards._cards if not card.isHidden()]
            titles = [card.title_label.text() for card in visible_cards]
            self.assertEqual(len(visible_cards), 6)
            self.assertIn("实际接收频率", titles)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_cards_default_to_six_core_slots_and_include_receive_hz(self) -> None:
        widget = SessionWidget("ui-test")
        try:
            frame = ParsedFrame(
                timestamp=datetime.now() - timedelta(seconds=1.2),
                raw="sample",
                device_id="001",
                mode=2,
                status="0001",
                fields={
                    "co2_ppm": 1.234,
                    "h2o_mmol": 2.345,
                    "temperature_c": 26.5,
                    "pressure_kpa": 101.32,
                    "active_alarm_count": 0,
                    "co2_ratio_raw": 0.9012,
                    "co2_ratio_f": 0.9001,
                    "h2o_ratio_raw": 0.7012,
                    "h2o_ratio_f": 0.7034,
                },
            )

            widget._update_data_cards(frame)
            visible_cards = [card for card in widget.data_cards._cards if not card.isHidden()]
            titles = [card.title_label.text() for card in visible_cards]

            self.assertEqual(len(visible_cards), 6)
            self.assertIn("实际接收频率", titles)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_shows_write_ready_context(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-ready")
        try:
            widget.port_combo.setCurrentText("COM35")
            widget._update_hard_status_bar()

            self.assertEqual(widget.session_header_box.title(), "")
            self.assertEqual(widget.hard_online_value_label.text(), "012")
            self.assertEqual(widget.hard_target_value_label.text(), "012")
            self.assertEqual(widget.hard_send_value_label.text(), "FFF")
            self.assertEqual(widget.hard_write_permission_label.text(), "允许")
            self.assertEqual(widget.hard_stream_status_label.text(), "可启动")
            self.assertIn("012", widget.session_header_detail_button.toolTip())
            self.assertIn("控制写入", widget.session_header_detail_button.toolTip())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_task_entry_area_is_visible_and_routes_without_sending_commands(self) -> None:
        widget = SessionWidget("ui-task-entries")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.settings_tab_index)
            widget.task_entry_box.setChecked(True)
            self.app.processEvents()

            self.assertTrue(widget.task_new_device_button.isVisible())
            self.assertEqual(widget.task_new_device_button_label.text(), "新设备接入检查")
            self.assertIn("先确认连接", widget.task_new_device_button_description.text())
            self.assertEqual(widget.task_export_diag_button_label.text(), "导出诊断包")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.task_new_device_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.settings_tab_index)

                widget.task_read_snapshot_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)

                widget.task_address_mode_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.control_tab_index)

                widget.task_coeff_review_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.coeff_tab_index)

                widget.task_export_diag_button.click()
                self.assertEqual(widget.pages.currentIndex(), widget.export_tab_index)

            send_payload.assert_not_called()
            self.assertIn("本次参数变更总览", widget.task_entry_feedback_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_change_summary_box_tracks_counts_and_routes_to_overview(self) -> None:
        widget = SessionWidget("ui-change-summary")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.export_tab_index)
            widget.change_summary_box.setChecked(True)
            self.app.processEvents()

            self.assertEqual(widget.change_summary_box.title(), "参数变更总览摘要（按需展开）")
            self.assertTrue(widget.change_summary_view_button.isVisible())
            self.assertEqual(widget.change_summary_view_button.text(), "查看总览")

            self._record_change_entry(widget, "MODE", "一致", before_value="MODE1", target_value="MODE2", after_value="MODE2")
            self._record_change_entry(
                widget,
                "SETCOMWAY",
                "ACK 成功",
                target_value="主动发送",
                after_value="未读回",
                detail_text="ACK-only / 未复核",
                verification_status="ack_only_unverified",
            )

            self.assertEqual(widget.change_summary_total_value_label.text(), "2")
            self.assertIn("2 条参数变更", widget.change_summary_message_label.text())
            self.assertEqual(widget.session_change_badge_button.text(), "参数变更 2 条")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.change_summary_view_button.click()
                self.app.processEvents()
                self.assertEqual(widget.pages.currentIndex(), widget.export_tab_index)
            send_payload.assert_not_called()
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_config_actions_are_visible_and_update_slot_summary(self) -> None:
        widget = SessionWidget("ui-chart-config")
        try:
            widget.show()
            self.app.processEvents()

            self.assertFalse(widget.chart_config_group.isVisible())

            widget.chart_panel.curve_settings_toggle_button.click()
            self.app.processEvents()

            self.assertTrue(widget.chart_config_group.isVisible())
            self.assertIn("CO2", widget.chart_upper_summary_label.text())
            self.assertIn("H2O", widget.chart_lower_summary_label.text())

            widget.chart_upper_view_combo.setCurrentIndex(widget.chart_upper_view_combo.findData("temperature"))
            self.app.processEvents()
            self.assertIn("温度", widget.chart_panel.slot_summary_text(0))

            widget.chart_clear_lower_button.click()
            self.app.processEvents()
            self.assertEqual(widget.chart_panel.slot_summary_text(1), "未配置")

            widget.chart_restore_defaults_button.click()
            self.app.processEvents()
            self.assertIn("CO2", widget.chart_panel.slot_summary_text(0))
            self.assertIn("H2O", widget.chart_panel.slot_summary_text(1))
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_action_row_keeps_primary_stream_button(self) -> None:
        widget = SessionWidget("ui-monitor-buttons")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()

            self.assertEqual(widget.monitor_start_stream_button.text(), "启动实时流")
            self.assertEqual(widget.monitor_diagnostic_button.text(), "诊断")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_diagnostic_actions_moved_into_dropdown(self) -> None:
        widget = SessionWidget("ui-monitor-diagnostic-menu")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()

            self.assertIsInstance(widget.monitor_diagnostic_button, QToolButton)
            self.assertEqual(
                widget.monitor_diagnostic_button.popupMode(),
                QToolButton.ToolButtonPopupMode.InstantPopup,
            )
            texts = [action.text() for action in widget.monitor_diagnostic_menu.actions()]
            self.assertEqual(
                texts,
                ["切换解析模式为自动识别", "读取关键配置", "打开串口日志"],
            )
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_action_row_no_overflow_at_1280_width(self) -> None:
        widget = SessionWidget("ui-monitor-action-row-fit")
        try:
            widget.resize(1280, 720)
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()
            self.app.processEvents()

            row_widget = widget.monitor_action_row_widget
            right_edge = max(
                child.geometry().right()
                for child in (
                    widget.monitor_start_stream_button,
                    widget.monitor_diagnostic_button,
                    widget.monitor_quick_status_label,
                )
            )
            self.assertLessEqual(right_edge, row_widget.width() + 4)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_ratio_not_regressed_after_action_row_slimming(self) -> None:
        widget = SessionWidget("ui-chart-ratio-after-action-row")
        try:
            widget.resize(1280, 720)
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()
            self.app.processEvents()

            page = widget.pages.widget(widget.monitor_tab_index)
            assert page is not None
            self.assertGreater(widget.chart_panel.height(), 0)
            self.assertGreaterEqual(widget.chart_panel.height() / max(1, page.height()), 0.55)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_toolbar_does_not_force_horizontal_overflow_on_1280_width(self) -> None:
        widget = SessionWidget("ui-chart-toolbar-fit")
        try:
            widget.resize(1280, 720)
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()
            self.app.processEvents()

            toolbar = widget.chart_panel.toolbar_primary_widget
            right_edge = max(
                child.geometry().right()
                for child in (
                    widget.chart_panel.preset_combo,
                    widget.chart_panel.window_combo,
                    widget.chart_panel.auto_range_check,
                    widget.chart_panel.clear_button,
                    widget.chart_panel.curve_settings_toggle_button,
                )
            )
            self.assertLessEqual(right_edge, toolbar.width() + 4)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_empty_placeholder_is_inside_chart_area(self) -> None:
        widget = SessionWidget("ui-chart-placeholder-area")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            self.app.processEvents()

            for state in widget.chart_panel._slot_states:
                with self.subTest(slot=state.slot_index):
                    self.assertIs(state.plot_stack.widget(0), state.placeholder)
                    self.assertEqual(state.plot_stack.currentIndex(), 0)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_status_badge_shows_rx_and_valid_frame_state(self) -> None:
        widget = SessionWidget("ui-chart-status-badge")
        try:
            self.assertIn("实时流：待连接", widget.chart_panel.status_badge_text())
            self.assertIn("RX：无", widget.chart_panel.status_badge_text())
            self.assertIn("有效帧：无", widget.chart_panel.status_badge_text())

            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._handle_rx_device_state({"latest_rx_device_id": "001", "active_rx_device_ids": ["001"]})
            widget._refresh_monitor_quick_controls()
            widget._handle_raw(
                RawFrameRecord(
                    timestamp=datetime.now(),
                    direction="RX",
                    text="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                )
            )
            widget._handle_frame(
                ParsedFrame(
                    timestamp=datetime.now(),
                    raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                    device_id="001",
                    mode=1,
                    status="0001",
                    fields={"co2_ppm": 488.879, "h2o_mmol": 0.528, "temperature_c": 26.10, "pressure_kpa": 101.14},
                )
            )
            self.app.processEvents()

            self.assertIn("实时流：已开启", widget.chart_panel.status_badge_text())
            self.assertIn("RX：有", widget.chart_panel.status_badge_text())
            self.assertIn("有效帧：有", widget.chart_panel.status_badge_text())
            self.assertIn("解析：AUTO", widget.chart_panel.status_badge_text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_status_badge_text_stays_compact(self) -> None:
        widget = SessionWidget("ui-chart-status-compact")
        try:
            text = widget.chart_panel.status_badge_text()
            self.assertEqual(len(text.split(" | ")), 4)
            self.assertLessEqual(len(text), 40)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_monitor_start_stream_button_sends_setcomway_when_monitoring_mode_is_ready(self) -> None:
        widget = SessionWidget("ui-monitor-start-stream")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._set_acquisition_mode("LISTEN")
            widget.read_only_lock_check.setChecked(False)
            widget._refresh_monitor_quick_controls()
            self.app.processEvents()

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.monitor_start_stream_button.click()
                self.app.processEvents()

            send_payload.assert_called_once()
            self.assertEqual(send_payload.call_args.args[0], "SETCOMWAY,YGAS,001,1")
            self.assertIn("正在启动主动上传", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_stream_start_ack_starts_first_frame_timer(self) -> None:
        widget = SessionWidget("ui-first-frame-timer")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._set_acquisition_mode("LISTEN")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime.now(),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                )
            )
            self.app.processEvents()

            self.assertTrue(widget.first_frame_timer.isActive())
            self.assertTrue(widget._waiting_for_first_stream_frame)
            self.assertIn("主动上传已开启，等待实时数据。", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_first_frame_arrival_clears_waiting_state(self) -> None:
        widget = SessionWidget("ui-first-frame-arrival")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._set_acquisition_mode("LISTEN")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime.now(),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                )
            )

            widget._handle_frame(
                ParsedFrame(
                    timestamp=datetime.now(),
                    raw="YGAS,001,0479.572,05.198,0958.423,04.249,1.3030,1.3033,0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005",
                    device_id="001",
                    mode=2,
                    status="0005",
                    fields={"co2_ppm": 479.572, "h2o_mmol": 5.198, "pressure_kpa": 103.97},
                )
            )
            self.app.processEvents()

            self.assertFalse(widget.first_frame_timer.isActive())
            self.assertFalse(widget._waiting_for_first_stream_frame)
            self.assertIn("已收到实时数据", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_first_frame_timeout_updates_chart_placeholder(self) -> None:
        widget = SessionWidget("ui-first-frame-timeout")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._set_acquisition_mode("LISTEN")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime.now(),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                )
            )

            widget._handle_first_frame_timeout()
            self.app.processEvents()

            self.assertIn("主动上传已开启，但尚未收到有效气体帧。", widget.monitor_quick_status_label.text())
            self.assertIn("目标设备 ID", widget.chart_panel._idle_placeholder_body)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_first_frame_timeout_keeps_auto_upload_state_on(self) -> None:
        widget = SessionWidget("ui-first-frame-timeout-state")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget.target_combo.setCurrentText("001")
            widget._set_acquisition_mode("LISTEN")
            with mock.patch.object(widget.controller, "send_payload"):
                widget._start_stream_flow("SETCOMWAY,YGAS,001,1")
            widget._handle_command_result(
                CommandResult(
                    timestamp=datetime.now(),
                    command="SETCOMWAY,YGAS,001,1",
                    ok=True,
                    message="ACK 成功",
                )
            )

            widget._handle_first_frame_timeout()
            self.app.processEvents()

            self.assertIn("已开启", widget.monitor_upload_badge.text())
            self.assertIn("实时流：已开启", widget.chart_panel.status_badge_text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_rx_without_valid_frame_updates_chart_placeholder(self) -> None:
        widget = SessionWidget("ui-raw-rx-no-frame")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget.port_combo.setCurrentText("COM35")
            widget._set_parse_mode("MODE1")
            widget._handle_raw(
                RawFrameRecord(
                    timestamp=datetime.now(),
                    direction="RX",
                    text="YGAS,001,0479.572,05.198,0958.423,04.249,1.3030,1.3033,0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005",
                )
            )
            self.app.processEvents()

            self.assertIn("收到串口数据，但未解析为有效气体帧。", widget.monitor_quick_status_label.text())
            self.assertIn("收到串口数据，但未解析为有效气体帧。", widget.chart_panel._idle_placeholder_body)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_rx_parse_failure_suggests_auto_mode_when_not_auto(self) -> None:
        widget = SessionWidget("ui-raw-rx-suggest-auto")
        try:
            widget.connected = True
            widget.controller.connected = True
            widget._set_parse_mode("MODE2")
            widget._handle_raw(
                RawFrameRecord(
                    timestamp=datetime.now(),
                    direction="RX",
                    text="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                )
            )
            self.app.processEvents()

            self.assertIn("建议检查 MODE1/MODE2 输出或切换解析模式为自动识别。", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_raw_rx_parse_failure_does_not_clear_existing_valid_curve(self) -> None:
        widget = SessionWidget("ui-raw-rx-keep-curve")
        try:
            widget.show()
            widget.pages.setCurrentIndex(widget.monitor_tab_index)
            widget.connected = True
            widget.controller.connected = True
            widget._set_parse_mode("AUTO")
            widget._handle_frame(
                ParsedFrame(
                    timestamp=datetime.now(),
                    raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                    device_id="001",
                    mode=1,
                    status="0001",
                    fields={"co2_ppm": 488.879, "h2o_mmol": 0.528, "temperature_c": 26.10, "pressure_kpa": 101.14},
                )
            )
            self.app.processEvents()

            widget._set_parse_mode("MODE2")
            widget._handle_raw(
                RawFrameRecord(
                    timestamp=datetime.now(),
                    direction="RX",
                    text="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                )
            )
            self.app.processEvents()

            self.assertEqual(widget.chart_panel._slot_states[0].plot_stack.currentIndex(), 1)
            self.assertTrue(widget.valid_frame_seen_since_connect)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_top_sections_are_collapsed_by_default_but_session_header_remains_visible(self) -> None:
        widget = SessionWidget("ui-header-collapsed")
        try:
            widget.show()
            self.app.processEvents()

            self.assertTrue(widget.session_header_box.isVisible())
            self.assertFalse(widget.task_entry_box.isVisible())
            self.assertFalse(widget.change_summary_box.isVisible())
            self.assertTrue(widget.connection_state_label.isVisible())
            self.assertTrue(widget.hard_online_value_label.isVisible())
            self.assertTrue(widget.session_workflow_button.isVisible())
            self.assertTrue(widget.session_change_badge_button.isVisible())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_default_monitoring_copy_uses_calibration_mode_wording(self) -> None:
        widget = SessionWidget("ui-default-config-copy")
        try:
            self.assertEqual(widget.apply_default_config_button.text(), "准备校准清单")
            self.assertEqual(widget.auto_apply_default_config_check.text(), "连接后准备校准清单")
            self.assertEqual(widget.init_button.text(), "准备初始化采集清单")
            self.assertIn("校准联调准备清单", widget.default_monitoring_checklist_summary_label.text())
            self.assertIn("MODE2", widget.default_monitoring_checklist_note_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_init_button_prepares_capture_checklist_without_sending(self) -> None:
        widget = self._prepare_write_ready_widget("ui-init-capture-checklist")
        try:
            self.assertEqual(widget.init_button.text(), "准备初始化采集清单")

            with mock.patch.object(widget.controller, "send_payload") as send_payload:
                widget.init_button.click()
                self.app.processEvents()

            self.assertEqual(widget.default_monitoring_checklist_table.rowCount(), 2)
            send_payload.assert_not_called()
            self.assertIn("尚未发送", widget.monitor_quick_status_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_main_window_branding_and_theme_controls_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            service.save({"ui": {"theme": "light"}})
            window = MainWindow(settings_service=service, settings_payload=service.load())
            try:
                toolbar = window.findChild(QToolBar)
                action_texts = [action.text() for action in toolbar.actions() if action.text()]
                theme_button = window.findChild(QToolButton)

                self.assertIn("GasAxis Studio", window.windowTitle())
                self.assertIn("软件说明", action_texts)
                self.assertIn("关于", action_texts)
                self.assertIsNotNone(theme_button)
                self.assertEqual(window.theme_button.text(), "界面主题")
                self.assertEqual([action.text() for action in window.theme_menu.actions()], ["深色", "浅色", "跟随系统"])

                session = window.tabs.widget(0)
                assert isinstance(session, SessionWidget)
                self.assertEqual(str(session.theme_combo.currentData()), "light")

                window.apply_theme_choice("dark", persist=False)
                self.assertEqual(str(session.theme_combo.currentData()), "dark")
            finally:
                window.close()
                self.app.processEvents()

    def test_main_window_clamps_initial_size_to_available_screen(self) -> None:
        class _FakeScreen:
            @staticmethod
            def availableGeometry() -> QRect:
                return QRect(0, 0, 1366, 768)

        class _StubWindow:
            def __init__(self, payload: dict) -> None:
                self.settings_payload = payload

            def screen(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            service = SettingsService(Path(temp_dir) / "settings.json")
            payload = service.load()
            payload["window"]["width"] = 1800
            payload["window"]["height"] = 1100

            stub_window = _StubWindow(payload)
            with mock.patch.object(QGuiApplication, "primaryScreen", return_value=_FakeScreen()):
                width, height = MainWindow._initial_window_size(stub_window)  # type: ignore[arg-type]

            self.assertLessEqual(width, 1342)
            self.assertLessEqual(height, 744)

    def test_help_markdown_uses_product_branding(self) -> None:
        markdown = _load_help_markdown()

        self.assertIn("GasAxis Studio", markdown)
        self.assertIn("气体分析仪监测与联调平台", markdown)
        self.assertIn("Gas Analyzer Monitoring & Commissioning Platform", markdown)
        self.assertIn("实时监测模式下，连接成功后软件会自动向明确目标设备发送 `SETCOMWAY=1`", markdown)
        self.assertIn("专家终端默认隐藏", markdown)
        self.assertNotIn("OpenAI", markdown)
        self.assertNotIn("ChatGPT", markdown)


if __name__ == "__main__":
    unittest.main()
