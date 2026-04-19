"""Registry-driven command workspace and custom template helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...commanding.permissions import has_permission, permission_label
from ...commanding.registry import (
    BROADCAST_ALLOWED_WITH_WARNING,
    CommandDefinition,
    CommandParameter,
    CommandRegistry,
)
from ...commanding.safety import can_execute_command
from ...protocols.senco_format import normalize_senco_input


class CommandDetailWidget(QGroupBox):
    command_requested = Signal(object, object, str, str)
    readback_requested = Signal(object)

    def __init__(
        self,
        registry: CommandRegistry,
        definition: CommandDefinition,
        *,
        profile_name: str = "bench_default",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.registry = registry
        self.definition = definition
        self.profile_name = profile_name
        self.target_id = "001"
        self.latest_online_device_id = ""
        self.active_online_device_ids: list[str] = []
        self.permission_level = "READ_ONLY"
        self.broadcast_enabled = False
        self.replay_blocked = False
        self.connected = False
        self.session_mode = "LISTEN_ONLY"
        self.read_only_lock = False
        self._fields: dict[str, QWidget] = {}
        self._hints: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        self.purpose_value = QLabel()
        self.purpose_value.setWordWrap(True)
        self.parameter_value = QLabel()
        self.parameter_value.setWordWrap(True)
        self.response_value = QLabel()
        self.response_value.setWordWrap(True)
        self.risk_value = QLabel()
        self.broadcast_value = QLabel()
        self.preview_meta_label = QLabel()
        self.preview_meta_label.setProperty("muted", True)
        self.preview_meta_label.setWordWrap(True)

        meta_form = QFormLayout()
        meta_form.addRow("用途", self.purpose_value)
        meta_form.addRow("参数说明", self.parameter_value)
        meta_form.addRow("返回说明", self.response_value)
        meta_form.addRow("风险等级", self.risk_value)
        meta_form.addRow("FFF 策略", self.broadcast_value)
        layout.addLayout(meta_form)

        self.target_mode_label = QLabel()
        self.target_mode_label.setWordWrap(True)
        self.target_mode_label.setProperty("muted", True)
        self.force_single_target_check = QCheckBox("改为单设备目标")
        self.force_single_target_check.toggled.connect(self.refresh_preview)
        self.target_override_edit = QLineEdit("001")
        self.target_override_edit.setPlaceholderText("000-999")
        self.target_override_edit.textChanged.connect(self.refresh_preview)
        target_box = QGroupBox("发送目标")
        target_form = QFormLayout(target_box)
        target_form.addRow("默认行为", self.target_mode_label)
        target_form.addRow("", self.force_single_target_check)
        target_form.addRow("单设备目标", self.target_override_edit)
        layout.addWidget(target_box)

        self.form_widget = QWidget()
        self.form = QFormLayout(self.form_widget)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        layout.addWidget(self.form_widget)

        layout.addWidget(QLabel("命令预览"))
        layout.addWidget(self.preview_meta_label)
        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        self.context_warning_label = QLabel("")
        self.context_warning_label.setWordWrap(True)
        self.context_warning_label.setProperty("warning", True)
        self.context_warning_label.hide()
        layout.addWidget(self.context_warning_label)

        self.last_response_label = QLabel("最近一次响应: --")
        self.last_response_label.setWordWrap(True)
        self.last_response_label.setProperty("muted", True)
        layout.addWidget(self.last_response_label)

        self.readback_button = QPushButton("读取当前参数")
        self.readback_button.clicked.connect(self._emit_readback_request)
        layout.addWidget(self.readback_button)

        self.readback_value_label = QLabel("当前值: --")
        self.readback_value_label.setWordWrap(True)
        layout.addWidget(self.readback_value_label)

        self.readback_time_label = QLabel("最近读取时间: --")
        self.readback_time_label.setWordWrap(True)
        self.readback_time_label.setProperty("muted", True)
        layout.addWidget(self.readback_time_label)

        self.readback_device_label = QLabel("读取来源设备 ID: --")
        self.readback_device_label.setWordWrap(True)
        self.readback_device_label.setProperty("muted", True)
        layout.addWidget(self.readback_device_label)

        self.send_button = QPushButton("执行命令")
        self.send_button.clicked.connect(self._emit_request)
        layout.addWidget(self.send_button)
        layout.addStretch(1)

        content_items = []
        while layout.count():
            item = layout.takeAt(0)
            if item.spacerItem() is not None:
                continue
            content_items.append(item)

        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(8)
        for item in content_items[:-2]:
            child_layout = item.layout()
            child_widget = item.widget()
            if child_layout is not None:
                scroll_layout.addLayout(child_layout)
            elif child_widget is not None:
                scroll_layout.addWidget(child_widget)
        scroll_layout.addStretch(1)
        self.content_scroll.setWidget(scroll_content)

        action_box = QGroupBox("执行与响应")
        action_layout = QVBoxLayout(action_box)
        action_layout.setContentsMargins(10, 10, 10, 10)
        action_layout.setSpacing(8)
        action_layout.addWidget(self.last_response_label)
        action_layout.addWidget(self.readback_button)
        action_layout.addWidget(self.readback_value_label)
        action_layout.addWidget(self.readback_time_label)
        action_layout.addWidget(self.readback_device_label)
        action_layout.addWidget(self.send_button)

        layout.addWidget(self.content_scroll, 1)
        layout.addWidget(action_box)
        self.set_definition(definition)

    def set_definition(self, definition: CommandDefinition) -> None:
        self.definition = definition
        self.setTitle(definition.display_name)
        self.purpose_value.setText(definition.purpose)
        self.parameter_value.setText(definition.parameter_help)
        self.response_value.setText(definition.response_help)
        self.risk_value.setText(definition.risk_level.upper())
        self.risk_value.setProperty("risk", definition.risk_level.lower())
        self.risk_value.style().unpolish(self.risk_value)
        self.risk_value.style().polish(self.risk_value)
        self.broadcast_value.setText(self._broadcast_policy_label(definition.broadcast_policy))
        self.last_response_label.setText("最近一次响应: --")
        self.set_readback_snapshot(current_value="--", timestamp_text="--", device_id="--")
        self._rebuild_form()
        self.refresh_preview()

    def set_target_id(self, target_id: str) -> None:
        self.target_id = target_id or "001"
        if not self.force_single_target_check.isChecked() or not self.target_override_edit.text().strip():
            self.target_override_edit.setText(self.target_id)
        self.refresh_preview()

    def set_online_device_state(self, latest_device_id: str, active_device_ids: list[str]) -> None:
        self.latest_online_device_id = str(latest_device_id or "").upper()
        self.active_online_device_ids = [str(item).upper() for item in active_device_ids]
        self.refresh_preview()

    def set_profile_name(self, profile_name: str) -> None:
        self.profile_name = profile_name
        self.set_definition(self.registry.get(self.definition.command_id, profile_name))

    def set_permission_level(self, permission_level: str) -> None:
        self.permission_level = permission_level
        self.refresh_preview()

    def set_broadcast_enabled(self, enabled: bool) -> None:
        self.broadcast_enabled = enabled
        self.refresh_preview()

    def set_replay_blocked(self, blocked: bool) -> None:
        self.replay_blocked = blocked
        self.refresh_preview()

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.refresh_preview()

    def set_session_mode(self, session_mode: str) -> None:
        self.session_mode = session_mode
        self.refresh_preview()

    def set_read_only_lock(self, locked: bool) -> None:
        self.read_only_lock = locked
        self.refresh_preview()

    def collect_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, widget in self._fields.items():
            if isinstance(widget, QComboBox):
                values[key] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                values[key] = widget.text()
            else:
                values[key] = ""
        return values

    def refresh_preview(self) -> None:
        values = self.collect_values()
        valid = self._revalidate_fields(values)
        effective_target = self._effective_target_id()
        preview = ""
        try:
            preview = self.registry.build_preview(
                self.definition.command_id,
                effective_target,
                values,
                profile_name=self.profile_name,
            )
            self.preview_label.setText(preview)
        except Exception as exc:
            self.preview_label.setText(f"参数未就绪: {exc}")

        ok, reason = self.registry.validate_command(
            self.definition.command_id,
            effective_target,
            values,
            profile_name=self.profile_name,
            permission_level=self.permission_level,
            broadcast_enabled=self.broadcast_enabled,
        )
        permission_ok = has_permission(self.permission_level, self.definition.required_permission)
        safety_ok, safety_reason = can_execute_command(
            self.definition,
            connected=self.connected,
            session_mode=self.session_mode,
            read_only_lock=self.read_only_lock,
            replay_running=self.replay_blocked,
        )

        warnings: list[str] = []
        if not ok:
            warnings.append(reason)
        if not permission_ok:
            warnings.append(f"当前权限不足，需要 {permission_label(self.definition.required_permission)}。")
        if not safety_ok:
            warnings.append(safety_reason)
        if self._is_write_command() and self.force_single_target_check.isChecked():
            warnings.append(f"当前已从默认 FFF 广播改为单设备目标 {effective_target}，请确认不会误写其他设备。")

        self.context_warning_label.setText("\n".join(item for item in warnings if item))
        self.context_warning_label.setVisible(bool(warnings))
        self.preview_meta_label.setText(self._preview_meta_text(effective_target))
        self.target_mode_label.setText(self._target_mode_text(effective_target))
        self.force_single_target_check.setVisible(self._is_write_command())
        self.target_override_edit.setVisible(self._is_write_command())
        self._refresh_readback_button()
        self.send_button.setEnabled(valid and ok and permission_ok and safety_ok and bool(preview))

    def set_last_response(self, command_text: str, ok: bool, message: str) -> None:
        code = str(command_text or "").split(",", 1)[0].strip().upper().replace("[FFF] ", "")
        if code != self.definition.code:
            return
        state = "成功" if ok else "失败"
        self.last_response_label.setText(f"最近一次响应: {state} | {message}")
        self.last_response_label.setProperty("risk", "high" if not ok else "low")
        self.last_response_label.setProperty("muted", False if ok else True)
        self.last_response_label.style().unpolish(self.last_response_label)
        self.last_response_label.style().polish(self.last_response_label)

    def _emit_request(self) -> None:
        self.command_requested.emit(
            self.definition,
            self.collect_values(),
            self.preview_label.text(),
            self._effective_target_id(),
        )

    def _emit_readback_request(self) -> None:
        self.readback_requested.emit(self.definition)

    def set_readback_snapshot(self, *, current_value: str, timestamp_text: str, device_id: str) -> None:
        self.readback_value_label.setText(f"当前值: {current_value or '--'}")
        self.readback_time_label.setText(f"最近读取时间: {timestamp_text or '--'}")
        self.readback_device_label.setText(f"读取来源设备 ID: {device_id or '--'}")

    def apply_prefill_values(self, values: dict[str, str]) -> None:
        if not values:
            return
        for key, value in values.items():
            widget = self._fields.get(key)
            if widget is None:
                continue
            if isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))
        self.refresh_preview()

    def _refresh_readback_button(self) -> None:
        visible = self._is_write_command() and self.registry.supports_readback(self.definition.command_id)
        self.readback_button.setVisible(visible)
        self.readback_value_label.setVisible(visible)
        self.readback_time_label.setVisible(visible)
        self.readback_device_label.setVisible(visible)
        if not visible:
            return

        readback_definition = self.registry.readback_definition(self.definition.command_id, self.profile_name)
        if readback_definition is None:
            self.readback_button.setEnabled(False)
            self.readback_button.setToolTip("当前命令没有安全读取对应项。")
            return
        permission_ok = has_permission(self.permission_level, readback_definition.required_permission)
        safety_ok, safety_reason = can_execute_command(
            readback_definition,
            connected=self.connected,
            session_mode=self.session_mode,
            read_only_lock=self.read_only_lock,
            replay_running=self.replay_blocked,
        )
        target_ok, target_reason = self._readback_target_status()
        enabled = permission_ok and safety_ok and target_ok
        self.readback_button.setEnabled(enabled)
        if not permission_ok:
            self.readback_button.setToolTip(f"当前权限不足，需要 {permission_label(readback_definition.required_permission)}。")
        elif not safety_ok:
            self.readback_button.setToolTip(safety_reason)
        elif not target_ok:
            self.readback_button.setToolTip(target_reason)
        else:
            self.readback_button.setToolTip("先读取当前参数，再决定是否修改。")

    def _readback_target_status(self) -> tuple[bool, str]:
        target = str(self.target_id or "").strip().upper()
        if target.isdigit():
            return True, ""
        if target != "FFF":
            return False, "当前目标设备 ID 无效，请先校正目标设备。"
        active_numeric_ids = self._active_numeric_online_ids()
        if len(active_numeric_ids) == 1:
            return True, ""
        if not active_numeric_ids:
            return False, "当前目标为 FFF，且无法确认唯一在线设备 ID，不能执行对象读取。"
        return False, f"当前目标为 FFF，但在线设备不唯一：{','.join(active_numeric_ids)}。"

    def _active_numeric_online_ids(self) -> list[str]:
        return sorted({device_id for device_id in self.active_online_device_ids if device_id.isdigit()})

    def _rebuild_form(self) -> None:
        self._fields.clear()
        self._hints.clear()
        while self.form.rowCount():
            self.form.removeRow(0)

        for parameter in self.definition.parameters:
            widget = self._make_input(parameter)
            hint = QLabel(self._hint_text(parameter))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            self._fields[parameter.key] = widget
            self._hints[parameter.key] = hint

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(widget, 2)
            row_layout.addWidget(hint, 1)
            self.form.addRow(parameter.label, row_widget)

    def _make_input(self, parameter: CommandParameter) -> QWidget:
        if parameter.kind == "choice":
            combo = QComboBox()
            combo.addItems(parameter.choices)
            if parameter.default:
                combo.setCurrentText(parameter.default)
            combo.currentTextChanged.connect(self.refresh_preview)
            return combo

        line = QLineEdit()
        line.setPlaceholderText(parameter.placeholder or parameter.description)
        line.setText(parameter.default)
        line.textChanged.connect(self.refresh_preview)
        return line

    def _revalidate_fields(self, values: dict[str, str]) -> bool:
        overall_valid = True
        for parameter in self.definition.parameters:
            widget = self._fields[parameter.key]
            hint = self._hints[parameter.key]
            text = values.get(parameter.key, "")
            error = self._parameter_error(parameter, text)
            widget.setProperty("invalid", bool(error))
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            if error:
                overall_valid = False
                hint.setText(error)
                hint.setProperty("risk", "high")
                hint.setProperty("muted", False)
            else:
                hint.setText(self._hint_text(parameter))
                hint.setProperty("risk", "low")
                hint.setProperty("muted", True)
            hint.style().unpolish(hint)
            hint.style().polish(hint)
        return overall_valid

    def _parameter_error(self, parameter: CommandParameter, text: str) -> str:
        if parameter.required and text.strip() == "":
            return "必填"
        if text.strip() == "":
            return ""
        if parameter.kind == "choice" and text not in parameter.choices:
            return f"可选: {', '.join(parameter.choices)}"
        if parameter.kind == "device_id" and (len(text) != 3 or not text.isdigit()):
            return "需为三位数字"
        if parameter.kind == "senco_coefficients":
            try:
                normalize_senco_input(text)
            except ValueError as exc:
                return str(exc)
            return ""
        if parameter.kind == "number":
            try:
                numeric = float(text)
            except Exception:
                return "需为数值"
            if parameter.minimum is not None and numeric < parameter.minimum:
                return f"不得小于 {parameter.minimum:g}"
            if parameter.maximum is not None and numeric > parameter.maximum:
                return f"不得大于 {parameter.maximum:g}"
        return ""

    @staticmethod
    def _hint_text(parameter: CommandParameter) -> str:
        if parameter.kind == "choice" and parameter.choices:
            return " / ".join(parameter.choices)
        if parameter.kind == "number" and parameter.minimum is not None and parameter.maximum is not None:
            return f"范围 {parameter.minimum:g}-{parameter.maximum:g}"
        if parameter.kind == "device_id":
            return "000-999"
        if parameter.kind == "senco_coefficients":
            return "支持 1-6 个系数，发送前会规范化为标准科学计数法"
        return parameter.description or parameter.placeholder

    def _is_write_command(self) -> bool:
        return self.registry.is_write_command_id(self.definition.command_id)

    def _effective_target_id(self) -> str:
        if self._is_write_command():
            if self.force_single_target_check.isChecked():
                return self.target_override_edit.text().strip().upper() or self.target_id
            return self.registry.default_target_for_command(self.definition.command_id, self.target_id)
        target = self.registry.default_target_for_command(self.definition.command_id, self.target_id)
        if str(target).isdigit():
            return target
        active_numeric_ids = self._active_numeric_online_ids()
        if str(target).upper() == "FFF" and len(active_numeric_ids) == 1:
            return active_numeric_ids[0]
        return target

    def _target_mode_text(self, effective_target: str) -> str:
        latest = self.latest_online_device_id or "未知"
        session_target = self.target_id.upper()
        if self._is_write_command():
            if self.force_single_target_check.isChecked():
                return (
                    f"写命令已改为单设备目标 {effective_target}；"
                    f"对象一致性仍按当前目标 {effective_target} 与在线设备 ID {latest} 校验。"
                )
            return (
                f"写命令默认将使用 FFF 广播地址；对象一致性仍按当前会话目标 ID {session_target} "
                f"与在线设备 ID {latest} 校验，不一致或在线状态不明确时将禁止发送。"
                "如需修改，建议先用下方“读取当前参数”确认当前值。"
            )
        if self.target_id.upper() == "FFF" and effective_target.isdigit():
            return (
                f"读命令将回退为唯一在线设备 ID {effective_target}；"
                f"当前会话目标为 FFF，当前在线设备 ID: {latest}。"
            )
        return f"读命令将使用当前目标 ID {effective_target}；当前在线设备 ID: {latest}。"

    def _preview_meta_text(self, effective_target: str) -> str:
        active_text = ",".join(self.active_online_device_ids) if self.active_online_device_ids else "--"
        object_target = effective_target if effective_target.isdigit() else self.target_id.upper()
        return (
            f"生效目标: {effective_target} | 对象校验目标: {object_target} | 会话目标: {self.target_id.upper()} | "
            f"当前在线: {self.latest_online_device_id or '--'} | 活跃设备: {active_text} | "
            f"广播策略: {self._broadcast_policy_label(self.definition.broadcast_policy)} | "
            f"风险: {self.definition.risk_level.upper()} | 会话模式: {self._session_mode_label(self.session_mode)} | "
            f"只读锁: {'开' if self.read_only_lock else '关'}"
        )

    @staticmethod
    def _broadcast_policy_label(policy: str) -> str:
        mapping = {
            "forbidden": "禁止使用 FFF",
            "expert_only": "仅专家模式可用 FFF，且必须二次确认",
            "calibration_or_expert": "仅校准/专家模式可用 FFF，且必须二次确认",
            BROADCAST_ALLOWED_WITH_WARNING: "允许使用 FFF，但默认关闭并需要风险确认",
        }
        return mapping.get(policy, policy)

    @staticmethod
    def _session_mode_label(session_mode: str) -> str:
        mapping = {
            "LISTEN_ONLY": "只监听",
            "SAFE_HANDSHAKE": "安全握手",
            "ENGINEERING": "工程模式",
            "REPLAY": "回放",
        }
        return mapping.get(str(session_mode or "").upper(), session_mode or "--")


class CommandWorkspacePanel(QWidget):
    command_requested = Signal(object, object, str, str)
    readback_requested = Signal(object)

    def __init__(
        self,
        registry: CommandRegistry,
        family_names: list[str],
        *,
        profile_name: str = "bench_default",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.registry = registry
        self.family_names = family_names
        self.profile_name = profile_name
        self.target_id = "001"
        self.latest_online_device_id = ""
        self.active_online_device_ids: list[str] = []
        self.permission_level = "READ_ONLY"
        self.broadcast_enabled = False
        self.replay_blocked = False
        self.connected = False
        self.session_mode = "LISTEN_ONLY"
        self.read_only_lock = False
        self._definitions: dict[str, CommandDefinition] = {}
        self._readback_state_by_command: dict[str, dict[str, str]] = {}

        layout = QVBoxLayout(self)
        self.context_banner = QLabel()
        self.context_banner.setWordWrap(True)
        self.context_banner.setProperty("muted", True)
        layout.addWidget(self.context_banner)

        self.context_risk_label = QLabel("")
        self.context_risk_label.setWordWrap(True)
        self.context_risk_label.setProperty("warning", True)
        self.context_risk_label.hide()
        layout.addWidget(self.context_risk_label)

        self.read_page_button = QPushButton("一键读取当前页相关参数")
        self.read_page_button.setProperty("accent", True)
        layout.addWidget(self.read_page_button)

        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索命令名称、命令码或说明")
        self.search_edit.textChanged.connect(self._apply_filter)
        self.search_edit.returnPressed.connect(self._select_first_visible_result)
        left_layout.addWidget(self.search_edit)
        self.command_tree = QTreeWidget()
        self.command_tree.setHeaderLabels(["命令分类 / 中文名称"])
        self.command_tree.setRootIsDecorated(True)
        self.command_tree.setMinimumWidth(220)
        self.command_tree.itemSelectionChanged.connect(self._selection_changed)
        left_layout.addWidget(self.command_tree, 1)
        splitter.addWidget(left_panel)

        first_definition = self._refresh_tree(profile_name)
        self.detail_widget = CommandDetailWidget(self.registry, first_definition, profile_name=profile_name)
        self.detail_widget.command_requested.connect(self.command_requested)
        self.detail_widget.readback_requested.connect(self.readback_requested)
        splitter.addWidget(self.detail_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([260, 760])
        layout.addWidget(splitter, 1)
        self._refresh_context_banner()

    def set_target_id(self, target_id: str) -> None:
        self.target_id = target_id
        self.detail_widget.set_target_id(target_id)
        self._refresh_context_banner()

    def set_online_device_state(self, latest_device_id: str, active_device_ids: list[str]) -> None:
        self.latest_online_device_id = str(latest_device_id or "").upper()
        self.active_online_device_ids = [str(item).upper() for item in active_device_ids]
        self.detail_widget.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        self._refresh_context_banner()

    def set_profile_name(self, profile_name: str) -> None:
        self.profile_name = profile_name
        current_command_id = self.current_command_id()
        first_definition = self._refresh_tree(profile_name)
        selected = current_command_id if current_command_id in self._definitions else first_definition.command_id
        self.select_command(selected)
        self.detail_widget.set_profile_name(profile_name)

    def set_permission_level(self, permission_level: str) -> None:
        self.permission_level = permission_level
        self.detail_widget.set_permission_level(permission_level)
        self._refresh_context_banner()

    def set_broadcast_enabled(self, enabled: bool) -> None:
        self.broadcast_enabled = enabled
        self.detail_widget.set_broadcast_enabled(enabled)
        self._refresh_context_banner()

    def set_replay_blocked(self, blocked: bool) -> None:
        self.replay_blocked = blocked
        self.detail_widget.set_replay_blocked(blocked)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.detail_widget.set_connected(connected)

    def set_session_mode(self, session_mode: str) -> None:
        self.session_mode = session_mode
        self.detail_widget.set_session_mode(session_mode)
        self._refresh_context_banner()

    def set_read_only_lock(self, locked: bool) -> None:
        self.read_only_lock = locked
        self.detail_widget.set_read_only_lock(locked)
        self._refresh_context_banner()

    def set_last_response(self, command_text: str, ok: bool, message: str) -> None:
        self.detail_widget.set_last_response(command_text, ok, message)

    def set_readback_result(
        self,
        command_id: str,
        *,
        current_value: str,
        timestamp_text: str,
        device_id: str,
        prefill_values: dict[str, str],
    ) -> None:
        self._readback_state_by_command[str(command_id).upper()] = {
            "current_value": current_value,
            "timestamp_text": timestamp_text,
            "device_id": device_id,
        }
        if self.current_command_id() == str(command_id).upper():
            self.detail_widget.set_readback_snapshot(
                current_value=current_value,
                timestamp_text=timestamp_text,
                device_id=device_id,
            )
            self.detail_widget.apply_prefill_values(prefill_values)

    def current_command(self) -> CommandDefinition:
        return self.detail_widget.definition

    def current_command_id(self) -> str:
        return self.detail_widget.definition.command_id

    def select_command(self, command_id: str) -> None:
        for index in range(self.command_tree.topLevelItemCount()):
            family_item = self.command_tree.topLevelItem(index)
            for child_index in range(family_item.childCount()):
                child = family_item.child(child_index)
                if child.data(0, Qt.UserRole) == command_id:
                    self.command_tree.setCurrentItem(child)
                    return

    def commands_in_panel(self) -> list[CommandDefinition]:
        return list(self._definitions.values())

    def _refresh_tree(self, profile_name: str) -> CommandDefinition:
        self._definitions.clear()
        self.command_tree.clear()
        first_definition: CommandDefinition | None = None
        for family in self.family_names:
            definitions = self.registry.commands_by_family(family, profile_name)
            if not definitions:
                continue
            family_item = QTreeWidgetItem([family])
            family_item.setFlags(family_item.flags() & ~Qt.ItemIsSelectable)
            self.command_tree.addTopLevelItem(family_item)
            for definition in definitions:
                child = QTreeWidgetItem([definition.display_name])
                child.setData(0, Qt.UserRole, definition.command_id)
                child.setData(
                    0,
                    Qt.UserRole + 1,
                    " ".join(
                        [
                            definition.display_name,
                            definition.command_id,
                            definition.code,
                            definition.purpose,
                            definition.parameter_help,
                            definition.response_help,
                        ]
                    ).lower(),
                )
                family_item.addChild(child)
                self._definitions[definition.command_id] = definition
                if first_definition is None:
                    first_definition = definition
            family_item.setExpanded(True)
        if first_definition is None:
            raise ValueError("命令页没有可显示的命令。")
        self._apply_filter(self.search_edit.text())
        return first_definition

    def _selection_changed(self) -> None:
        item = self.command_tree.currentItem()
        if item is None:
            return
        command_id = str(item.data(0, Qt.UserRole) or "").strip()
        if not command_id or command_id not in self._definitions:
            return
        definition = self._definitions[command_id]
        self.detail_widget.set_definition(definition)
        self.detail_widget.set_target_id(self.target_id)
        self.detail_widget.set_online_device_state(self.latest_online_device_id, self.active_online_device_ids)
        self.detail_widget.set_permission_level(self.permission_level)
        self.detail_widget.set_broadcast_enabled(self.broadcast_enabled)
        self.detail_widget.set_replay_blocked(self.replay_blocked)
        self.detail_widget.set_connected(self.connected)
        self.detail_widget.set_session_mode(self.session_mode)
        self.detail_widget.set_read_only_lock(self.read_only_lock)
        self._apply_readback_state(command_id)

    def _apply_readback_state(self, command_id: str) -> None:
        state = self._readback_state_by_command.get(str(command_id).upper())
        if state is None:
            self.detail_widget.set_readback_snapshot(current_value="--", timestamp_text="--", device_id="--")
            return
        self.detail_widget.set_readback_snapshot(
            current_value=state.get("current_value", "--"),
            timestamp_text=state.get("timestamp_text", "--"),
            device_id=state.get("device_id", "--"),
        )

    def _apply_filter(self, text: str) -> None:
        query = str(text or "").strip().lower()
        for index in range(self.command_tree.topLevelItemCount()):
            family_item = self.command_tree.topLevelItem(index)
            visible_children = 0
            for child_index in range(family_item.childCount()):
                child = family_item.child(child_index)
                search_text = str(child.data(0, Qt.UserRole + 1) or "")
                matched = not query or query in search_text
                child.setHidden(not matched)
                if matched:
                    visible_children += 1
            family_item.setHidden(visible_children == 0)
            family_item.setExpanded(bool(query) or visible_children > 0)

    def _select_first_visible_result(self) -> None:
        for index in range(self.command_tree.topLevelItemCount()):
            family_item = self.command_tree.topLevelItem(index)
            if family_item.isHidden():
                continue
            for child_index in range(family_item.childCount()):
                child = family_item.child(child_index)
                if child.isHidden():
                    continue
                self.command_tree.setCurrentItem(child)
                self.command_tree.scrollToItem(child)
                return

    def _refresh_context_banner(self) -> None:
        target_id = (self.target_id or "001").upper()
        broadcast_active = self.broadcast_enabled or target_id == "FFF"
        online_text = self.latest_online_device_id or "--"
        self.context_banner.setText(
            "当前上下文："
            f"目标设备 {target_id} | "
            f"实时在线 {online_text} | "
            f"权限等级 {permission_label(self.permission_level)} | "
            f"会话模式 {self._session_mode_label(self.session_mode)} | "
            f"只读锁 {'开启' if self.read_only_lock else '关闭'} | "
            f"广播 FFF {'启用' if broadcast_active else '关闭'}"
        )

        risk_text = ""
        if target_id == "FFF":
            risk_text = "当前目标为广播地址 FFF：下发命令前请确认现场隔离、权限等级和影响范围。"
        elif self.broadcast_enabled:
            risk_text = (
                "写命令发送地址默认可使用 FFF，但对象一致性仍按当前目标设备与在线设备校验；"
                "若在线状态不明确或对象不一致，将禁止发送。"
            )
        self.context_risk_label.setText(risk_text)
        self.context_risk_label.setVisible(bool(risk_text))

    @staticmethod
    def _session_mode_label(session_mode: str) -> str:
        return CommandDetailWidget._session_mode_label(session_mode)


class CustomTemplateEditor(QWidget):
    template_saved = Signal()

    def __init__(self, registry: CommandRegistry, parent: QWidget | None = None):
        super().__init__(parent)
        self.registry = registry
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.code_edit = QLineEdit()
        self.name_edit = QLineEdit()
        self.family_edit = QLineEdit("用户自定义命令")
        self.purpose_edit = QLineEdit()
        self.params_edit = QLineEdit()
        self.risk_combo = QComboBox()
        self.risk_combo.addItems(["low", "medium", "high", "critical"])
        self.permission_combo = QComboBox()
        self.permission_combo.addItems(["READ_ONLY", "CONFIG", "CALIBRATION", "EXPERT"])
        self.response_combo = QComboBox()
        self.response_combo.addItems(sorted(CommandRegistry.RETURN_TYPES))
        self.broadcast_combo = QComboBox()
        self.broadcast_combo.addItems(
            ["forbidden", "allowed_with_warning", "calibration_or_expert", "expert_only"]
        )
        form.addRow("协议命令", self.code_edit)
        form.addRow("中文名称", self.name_edit)
        form.addRow("业务分组", self.family_edit)
        form.addRow("用途说明", self.purpose_edit)
        form.addRow("参数定义", self.params_edit)
        form.addRow("风险级别", self.risk_combo)
        form.addRow("权限要求", self.permission_combo)
        form.addRow("返回类型", self.response_combo)
        form.addRow("FFF 策略", self.broadcast_combo)

        save_button = QPushButton("保存为自定义模板")
        save_button.clicked.connect(self._save)
        help_label = QLabel("参数定义示例: window:窗口大小,coefficients:系数列表")
        help_label.setProperty("muted", True)

        layout.addLayout(form)
        layout.addWidget(help_label)
        layout.addWidget(save_button)
        layout.addStretch(1)

    def _save(self) -> None:
        if not self.code_edit.text().strip() or not self.name_edit.text().strip():
            return
        params = []
        raw_params = self.params_edit.text().strip()
        for item in [part.strip() for part in raw_params.split(",") if part.strip()]:
            key, _, label = item.partition(":")
            params.append(CommandParameter(key=key.strip(), label=(label or key).strip(), description="自定义参数"))
        definition = CommandDefinition(
            command_id=self.code_edit.text().strip().upper(),
            code=self.code_edit.text().strip().upper(),
            display_name=self.name_edit.text().strip(),
            family=self.family_edit.text().strip() or "用户自定义命令",
            purpose=self.purpose_edit.text().strip() or "用户自定义命令。",
            parameter_help="自定义参数。",
            response_help="按模板配置的返回类型解释。",
            risk_level=self.risk_combo.currentText(),
            supported_modes=["MODE1", "MODE2"],
            broadcast_policy=self.broadcast_combo.currentText(),
            return_type=self.response_combo.currentText(),
            required_permission=self.permission_combo.currentText(),
            parameters=params,
            builtin=False,
        )
        self.registry.upsert_custom_template(definition)
        self.template_saved.emit()
