from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QToolBar, QToolButton
from unittest import mock

from ygas_monitor.commanding.registry import CommandRegistry
from ygas_monitor.commanding.safety import SESSION_MODE_ENGINEERING, SESSION_MODE_LISTEN_ONLY
from ygas_monitor.models import CommandResult, ParsedFrame
from ygas_monitor.services.settings_service import SettingsService
from ygas_monitor.ui.main_window import MainWindow
from ygas_monitor.ui.product_dialogs import _load_help_markdown
from ygas_monitor.ui.session_widget import SessionWidget
from ygas_monitor.ui.widgets.command_cards import CommandWorkspacePanel


class SessionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _prepare_write_ready_widget(self, name: str) -> SessionWidget:
        widget = SessionWidget(name)
        widget.connected = True
        widget.permission_combo.setCurrentText("CALIBRATION")
        engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
        widget.session_mode_combo.setCurrentIndex(engineering_index)
        widget.target_combo.setCurrentText("012")
        widget._handle_rx_device_state({"latest_rx_device_id": "012", "active_rx_device_ids": ["012"]})
        widget._apply_permission_mode()
        self.app.processEvents()
        return widget

    def test_monitor_cards_fill_twelve_slots_and_include_latest_data_delay(self) -> None:
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

            self.assertEqual(len(visible_cards), 12)
            self.assertIn("最新数据时延", titles)
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

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
            self.assertEqual(len(visible_cards), 12)
            self.assertIn("最新数据时延", titles)
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
            self.assertIn("会话模式 工程模式", panel.context_banner.text())
            self.assertIn("只读锁 开启", panel.context_banner.text())
            self.assertIn("广播 FFF 启用", panel.context_banner.text())
            self.assertFalse(panel.context_risk_label.isHidden())
        finally:
            panel.close()
            self.app.processEvents()

    def test_hard_status_bar_shows_write_ready_context(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-ready")
        try:
            widget._update_hard_status_bar()

            self.assertEqual(widget.hard_online_value_label.parentWidget().title(), "会话写入基础状态")
            self.assertEqual(widget.hard_online_value_label.text(), "012")
            self.assertEqual(widget.hard_target_value_label.text(), "012")
            self.assertEqual(widget.hard_send_value_label.text(), "FFF")
            self.assertEqual(widget.hard_write_permission_label.text(), "允许")
            self.assertIn("已满足写入条件", widget.hard_reason_value_label.text())
            self.assertIn("基础写入条件", widget.hard_status_help_label.text())
            self.assertIn("具体命令仍需通过权限、风险和命令级校验", widget.hard_status_help_label.text())
            self.assertEqual(widget.hard_status_note_label.text(), "当前选中命令仍会单独校验")
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_blocks_mismatch_and_multiple_online_devices(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-blocked")
        try:
            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_write_permission_label.text(), "禁止")
            self.assertIn("target-online 不一致", widget.hard_reason_value_label.text())

            widget._handle_rx_device_state({"latest_rx_device_id": "002", "active_rx_device_ids": ["002", "003"]})
            widget._update_hard_status_bar()
            self.assertEqual(widget.hard_online_value_label.text(), "多个")
            self.assertIn("当前无唯一在线设备", widget.hard_reason_value_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_hard_status_bar_reflects_listen_lock_and_replay_states(self) -> None:
        widget = self._prepare_write_ready_widget("ui-hard-status-modes")
        try:
            listen_index = widget.session_mode_combo.findData(SESSION_MODE_LISTEN_ONLY)
            widget.session_mode_combo.setCurrentIndex(listen_index)
            widget._apply_permission_mode()
            self.assertIn("只监听模式", widget.hard_reason_value_label.text())

            engineering_index = widget.session_mode_combo.findData(SESSION_MODE_ENGINEERING)
            widget.session_mode_combo.setCurrentIndex(engineering_index)
            widget.read_only_lock_check.setChecked(True)
            widget._apply_permission_mode()
            self.assertIn("只读锁开启", widget.hard_reason_value_label.text())

            widget.read_only_lock_check.setChecked(False)
            widget.replay_running = True
            widget._apply_permission_mode()
            self.assertIn("回放模式", widget.hard_reason_value_label.text())
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_chart_config_actions_are_visible_and_update_slot_summary(self) -> None:
        widget = SessionWidget("ui-chart-config")
        try:
            widget.show()
            self.app.processEvents()

            self.assertTrue(widget.chart_config_group.isVisible())
            self.assertIn("CO2 浓度", widget.chart_upper_summary_label.text())
            self.assertIn("H2O 浓度", widget.chart_lower_summary_label.text())

            widget.chart_view_combo.setCurrentIndex(widget.chart_view_combo.findData("temperature"))
            widget._apply_chart_view_to_slot(0)
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
        finally:
            widget.shutdown()
            widget.close()
            self.app.processEvents()

    def test_write_command_is_blocked_when_online_device_mismatches_target(self) -> None:
        widget = SessionWidget("ui-target-mismatch")
        try:
            widget.connected = True
            widget.permission_combo.setCurrentText("CALIBRATION")
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

    def test_serial_assistant_tab_is_visible_by_default(self) -> None:
        widget = SessionWidget("ui-serial-assistant")
        try:
            self.assertEqual(widget.pages.tabText(widget.expert_tab_index), "串口助手")
            self.assertTrue(widget.pages.isTabVisible(widget.expert_tab_index))
            self.assertGreater(widget.serial_template_combo.count(), 0)
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

    def test_help_markdown_uses_product_branding(self) -> None:
        markdown = _load_help_markdown()

        self.assertIn("GasAxis Studio", markdown)
        self.assertIn("气体分析仪监测与联调平台", markdown)
        self.assertIn("Gas Analyzer Monitoring & Commissioning Platform", markdown)
        self.assertNotIn("OpenAI", markdown)
        self.assertNotIn("ChatGPT", markdown)


if __name__ == "__main__":
    unittest.main()
