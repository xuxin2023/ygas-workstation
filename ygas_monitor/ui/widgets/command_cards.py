"""Registry-driven command workspace and custom template helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
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


class CommandDetailWidget(QGroupBox):
    command_requested = Signal(object, object, str)

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
        self.permission_level = "READ_ONLY"
        self.broadcast_enabled = False
        self.replay_blocked = False
        self.connected = False
        self.session_mode = "LISTEN_ONLY"
        self.read_only_lock = False
        self._fields: dict[str, QWidget] = {}
        self._hints: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
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

        self.form_widget = QWidget()
        self.form = QFormLayout(self.form_widget)
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

        self.send_button = QPushButton("执行命令")
        self.send_button.clicked.connect(self._emit_request)
        layout.addWidget(self.send_button)
        layout.addStretch(1)
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
        self._rebuild_form()
        self.refresh_preview()

    def set_target_id(self, target_id: str) -> None:
        self.target_id = target_id or "001"
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
        preview = ""
        try:
            preview = self.registry.build_preview(
                self.definition.command_id,
                self.target_id,
                values,
                profile_name=self.profile_name,
            )
            self.preview_label.setText(preview)
        except Exception as exc:
            self.preview_label.setText(f"参数未就绪: {exc}")

        ok, reason = self.registry.validate_command(
            self.definition.command_id,
            self.target_id,
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

        self.context_warning_label.setText("\n".join(item for item in warnings if item))
        self.context_warning_label.setVisible(bool(warnings))
        self.preview_meta_label.setText(
            f"目标 ID: {self.target_id.upper()} | 使用 FFF: {'是' if self.target_id.upper() == 'FFF' else '否'} | "
            f"广播策略: {self._broadcast_policy_label(self.definition.broadcast_policy)} | "
            f"风险: {self.definition.risk_level.upper()} | 会话模式: {self.session_mode} | "
            f"只读锁: {'开' if self.read_only_lock else '关'}"
        )
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
        self.command_requested.emit(self.definition, self.collect_values(), self.preview_label.text())

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
        if parameter.kind == "device_id" and len(text) != 3:
            return "需为三位数字"
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
        return parameter.description or parameter.placeholder

    @staticmethod
    def _broadcast_policy_label(policy: str) -> str:
        mapping = {
            "forbidden": "禁止使用 FFF",
            "expert_only": "仅专家模式可用 FFF，且必须二次确认",
            "calibration_or_expert": "仅校准/专家模式可用 FFF，且必须二次确认",
            BROADCAST_ALLOWED_WITH_WARNING: "允许使用 FFF，但默认关闭并需要风险确认",
        }
        return mapping.get(policy, policy)


class CommandWorkspacePanel(QWidget):
    command_requested = Signal(object, object, str)

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
        self.permission_level = "READ_ONLY"
        self.broadcast_enabled = False
        self.replay_blocked = False
        self.connected = False
        self.session_mode = "LISTEN_ONLY"
        self.read_only_lock = False
        self._definitions: dict[str, CommandDefinition] = {}

        layout = QVBoxLayout(self)
        self.read_page_button = QPushButton("一键读取当前页相关参数")
        self.read_page_button.setProperty("accent", True)
        layout.addWidget(self.read_page_button)

        splitter = QSplitter()
        self.command_tree = QTreeWidget()
        self.command_tree.setHeaderLabels(["命令分类 / 中文名称"])
        self.command_tree.setRootIsDecorated(True)
        self.command_tree.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.command_tree)

        first_definition = self._refresh_tree(profile_name)
        self.detail_widget = CommandDetailWidget(self.registry, first_definition, profile_name=profile_name)
        self.detail_widget.command_requested.connect(self.command_requested)
        splitter.addWidget(self.detail_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        layout.addWidget(splitter, 1)

    def set_target_id(self, target_id: str) -> None:
        self.target_id = target_id
        self.detail_widget.set_target_id(target_id)

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

    def set_broadcast_enabled(self, enabled: bool) -> None:
        self.broadcast_enabled = enabled
        self.detail_widget.set_broadcast_enabled(enabled)

    def set_replay_blocked(self, blocked: bool) -> None:
        self.replay_blocked = blocked
        self.detail_widget.set_replay_blocked(blocked)

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        self.detail_widget.set_connected(connected)

    def set_session_mode(self, session_mode: str) -> None:
        self.session_mode = session_mode
        self.detail_widget.set_session_mode(session_mode)

    def set_read_only_lock(self, locked: bool) -> None:
        self.read_only_lock = locked
        self.detail_widget.set_read_only_lock(locked)

    def set_last_response(self, command_text: str, ok: bool, message: str) -> None:
        self.detail_widget.set_last_response(command_text, ok, message)

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
                family_item.addChild(child)
                self._definitions[definition.command_id] = definition
                if first_definition is None:
                    first_definition = definition
            family_item.setExpanded(True)
        if first_definition is None:
            raise ValueError("命令页没有可显示的命令。")
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
        self.detail_widget.set_permission_level(self.permission_level)
        self.detail_widget.set_broadcast_enabled(self.broadcast_enabled)
        self.detail_widget.set_replay_blocked(self.replay_blocked)
        self.detail_widget.set_connected(self.connected)
        self.detail_widget.set_session_mode(self.session_mode)
        self.detail_widget.set_read_only_lock(self.read_only_lock)


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
