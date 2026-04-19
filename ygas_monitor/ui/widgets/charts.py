"""Realtime chart workspace built on pyqtgraph."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
import time
from typing import Any, Sequence

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from ...models import ParsedFrame
from ..theme_tokens import get_theme_tokens

REFRESH_INTERVAL_MS = 120
XRANGE_EPSILON = 0.05

WINDOW_OPTIONS = {
    "30s": 30.0,
    "2min": 120.0,
    "10min": 600.0,
    "全会话": None,
}

FIELD_LABELS = {
    "co2_ppm": "CO2 浓度",
    "co2_density": "CO2 密度",
    "h2o_mmol": "H2O 浓度",
    "h2o_density": "H2O 密度",
    "co2_ratio_raw": "CO2 比值（原始）",
    "co2_ratio_f": "CO2 比值（滤波）",
    "co2_ratio_delta": "CO2 比值（差值）",
    "h2o_ratio_raw": "H2O 比值（原始）",
    "h2o_ratio_f": "H2O 比值（滤波）",
    "h2o_ratio_delta": "H2O 比值（差值）",
    "ref_signal": "参考信号",
    "co2_signal": "CO2 信号",
    "h2o_signal": "H2O 信号",
    "temperature_c": "主温",
    "chamber_temp_c": "腔温",
    "case_temp_c": "壳温",
    "pressure_kpa": "压力",
    "active_alarm_count": "告警数",
    "status_numeric": "状态数值",
}

@dataclass(frozen=True)
class TimeAxisWindow:
    start: float
    end: float


@dataclass(frozen=True)
class GroupDisplayState:
    show_plot: bool
    banner_text: str
    placeholder_title: str = ""
    placeholder_body: str = ""


@dataclass(frozen=True)
class ViewSpec:
    view_id: str
    title: str
    fields: tuple[str, ...]
    y_label: str
    units: str = ""
    summary: str = ""
    metric_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PresetSpec:
    preset_id: str
    title: str
    upper_view_id: str
    lower_view_id: str


@dataclass(frozen=True)
class SlotState:
    slot_index: int
    slot_name: str
    combo: QComboBox
    plot: pg.PlotWidget
    plot_stack: QStackedWidget
    placeholder: QLabel
    note_label: QLabel


SLOT_VIEW_SPECS: tuple[ViewSpec, ...] = (
    ViewSpec(
        "co2_concentration",
        "CO2 浓度",
        ("co2_ppm",),
        "浓度",
        "ppm",
        summary="用于观察 CO2 实时浓度变化。",
        metric_keys=("co2_ppm", "co2_density"),
    ),
    ViewSpec(
        "h2o_concentration",
        "H2O 浓度",
        ("h2o_mmol",),
        "浓度",
        "mmol/mol",
        summary="用于观察 H2O 实时浓度变化。",
        metric_keys=("h2o_mmol", "h2o_density"),
    ),
    ViewSpec(
        "co2_ratio_compare",
        "CO2 比值",
        ("co2_ratio_raw", "co2_ratio_f", "co2_ratio_delta"),
        "比值 / 差值",
        "ratio",
        summary="对比 CO2 原始比值、滤波比值和滤波差值。",
        metric_keys=("co2_ratio_raw", "co2_ratio_f", "co2_ratio_delta"),
    ),
    ViewSpec(
        "h2o_ratio_compare",
        "H2O 比值",
        ("h2o_ratio_raw", "h2o_ratio_f", "h2o_ratio_delta"),
        "比值 / 差值",
        "ratio",
        summary="对比 H2O 原始比值、滤波比值和滤波差值。",
        metric_keys=("h2o_ratio_raw", "h2o_ratio_f", "h2o_ratio_delta"),
    ),
    ViewSpec(
        "signal_quality",
        "信号质量",
        ("ref_signal", "co2_signal", "h2o_signal"),
        "信号",
        "",
        summary="观察参考信号和采样信号强度。",
        metric_keys=("ref_signal", "co2_signal", "h2o_signal"),
    ),
    ViewSpec(
        "temperature",
        "温度",
        ("temperature_c", "chamber_temp_c", "case_temp_c"),
        "温度",
        "℃",
        summary="观察主温度、腔温和壳温。",
        metric_keys=("temperature_c", "chamber_temp_c", "case_temp_c"),
    ),
    ViewSpec(
        "pressure_status",
        "压力 / 状态",
        ("pressure_kpa", "active_alarm_count", "status_numeric"),
        "压力 / 状态",
        "",
        summary="查看压力、告警数和状态数值。",
        metric_keys=("pressure_kpa", "active_alarm_count", "status_numeric"),
    ),
)

EMPTY_VIEW_SPEC = ViewSpec(
    "empty",
    "未配置",
    (),
    "",
    "",
    summary="当前图槽未配置变量。",
)
ALL_SLOT_VIEW_SPECS: tuple[ViewSpec, ...] = (EMPTY_VIEW_SPEC,) + SLOT_VIEW_SPECS
DEFAULT_SLOT_VIEW_IDS = ("co2_concentration", "h2o_concentration")

PRESETS: tuple[PresetSpec, ...] = (
    PresetSpec("preset_monitor", "预设1：上 CO2 / 下 H2O", "co2_concentration", "h2o_concentration"),
    PresetSpec("preset_ratio", "预设2：上 CO2 比值 / 下 H2O 比值", "co2_ratio_compare", "h2o_ratio_compare"),
    PresetSpec("preset_env", "预设3：上 温度 / 下 压力状态", "temperature", "pressure_status"),
)

LEGACY_GROUP_FIELDS = {
    "比值对比": ["co2_ratio_raw", "co2_ratio_f", "co2_ratio_delta", "h2o_ratio_raw", "h2o_ratio_f", "h2o_ratio_delta"],
    "CO2 主监测": ["co2_ppm", "co2_density"],
    "H2O 主监测": ["h2o_mmol", "h2o_density"],
    "信号质量": ["ref_signal", "co2_signal", "h2o_signal"],
    "温度": ["temperature_c", "chamber_temp_c", "case_temp_c"],
    "压力 / 状态": ["pressure_kpa", "active_alarm_count", "status_numeric"],
}

VIEW_BY_ID = {spec.view_id: spec for spec in ALL_SLOT_VIEW_SPECS}
METRIC_TO_VIEW = {
    metric_key: spec.view_id
    for spec in SLOT_VIEW_SPECS
    for metric_key in spec.metric_keys
}
DISPLAY_FIELDS = tuple(dict.fromkeys(field for spec in SLOT_VIEW_SPECS for field in spec.fields))


def friendly_field_name(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def coerce_numeric(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def filter_finite_points(times: Sequence[float], values: Sequence[Any]) -> tuple[list[float], list[float]]:
    visible_times: list[float] = []
    visible_values: list[float] = []
    for current_time, raw_value in zip(times, values):
        numeric = coerce_numeric(raw_value)
        if math.isfinite(numeric):
            visible_times.append(float(current_time))
            visible_values.append(numeric)
    return visible_times, visible_values


def build_time_axis_window(
    times: Sequence[float],
    *,
    min_span: float = 2.0,
    min_margin: float = 0.2,
) -> TimeAxisWindow | None:
    if not times:
        return None
    start = float(min(times))
    end = float(max(times))
    span = end - start
    if span < min_span:
        center = (start + end) / 2.0
        half_span = min_span / 2.0
        start = center - half_span
        end = center + half_span
        span = end - start
    margin = max(min_margin, span * 0.08)
    return TimeAxisWindow(start=start - margin, end=end + margin)


def describe_group_state(
    *,
    total_frames: int,
    enabled_field_count: int,
    seen_field_count: int,
    visible_sample_count: int,
) -> GroupDisplayState:
    if total_frames <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="等待实时数据或开始回放。",
            placeholder_title="尚未收到任何帧",
            placeholder_body="连接设备、启动模拟器或开始回放后，此处会自动显示实时曲线。",
        )
    if enabled_field_count <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="当前图槽没有启用字段。",
            placeholder_title="当前图槽没有启用字段",
            placeholder_body="请切换图槽内容后再查看。",
        )
    if visible_sample_count <= 0 and seen_field_count <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="当前模式下该视图字段暂不可用。",
            placeholder_title="当前图槽暂无可绘制数据",
            placeholder_body="请切换视图或等待更多数据。",
        )
    if visible_sample_count <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="当前时间窗内暂无有效点。",
            placeholder_title="当前图槽暂无可绘制数据",
            placeholder_body="请切换视图或等待更多数据。",
        )
    if visible_sample_count == 1:
        return GroupDisplayState(
            show_plot=True,
            banner_text="当前仅有 1 个有效点，已显示点标记并扩展 X 轴时间窗。",
        )
    if visible_sample_count == 2:
        return GroupDisplayState(
            show_plot=True,
            banner_text="当前仅有 2 个有效点，已保留明显点标记。",
        )
    return GroupDisplayState(
        show_plot=True,
        banner_text=f"当前时间窗内共 {visible_sample_count} 个有效采样点。",
    )


def placeholder_html(title: str, body: str, *, theme: str = "dark") -> str:
    tokens = get_theme_tokens(theme)
    return (
        "<div style='text-align:center; padding:24px;'>"
        f"<div style='font-size:18px; font-weight:700; color:{tokens.chart_placeholder_title}; margin-bottom:8px;'>{title}</div>"
        f"<div style='font-size:13px; color:{tokens.chart_placeholder_body}; line-height:1.6;'>{body}</div>"
        "</div>"
    )


def _delta_value(raw_value: Any, filtered_value: Any) -> float:
    raw_numeric = coerce_numeric(raw_value)
    filtered_numeric = coerce_numeric(filtered_value)
    if not math.isfinite(raw_numeric) or not math.isfinite(filtered_numeric):
        return math.nan
    return filtered_numeric - raw_numeric


def _status_numeric_value(frame: ParsedFrame) -> float:
    direct_value = coerce_numeric(frame.fields.get("status_numeric"))
    if math.isfinite(direct_value):
        return direct_value
    if frame.status:
        try:
            return float(int(str(frame.status), 16))
        except ValueError:
            return math.nan
    return math.nan


def build_display_value_map(frame: ParsedFrame) -> dict[str, float]:
    values = {field: coerce_numeric(frame.fields.get(field)) for field in DISPLAY_FIELDS}
    values["co2_ratio_delta"] = _delta_value(frame.fields.get("co2_ratio_raw"), frame.fields.get("co2_ratio_f"))
    values["h2o_ratio_delta"] = _delta_value(frame.fields.get("h2o_ratio_raw"), frame.fields.get("h2o_ratio_f"))
    values["status_numeric"] = _status_numeric_value(frame)
    return values


def group_field_order(group_title: str) -> list[str]:
    if group_title in LEGACY_GROUP_FIELDS:
        return list(LEGACY_GROUP_FIELDS[group_title])
    if group_title in VIEW_BY_ID:
        return list(VIEW_BY_ID[group_title].fields)
    return []


def nearest_time_index(times: Sequence[float], target: float) -> int:
    if len(times) <= 1:
        return 0
    insert_at = bisect_left(times, target)
    if insert_at <= 0:
        return 0
    if insert_at >= len(times):
        return len(times) - 1
    before = insert_at - 1
    after = insert_at
    if abs(times[before] - target) <= abs(times[after] - target):
        return before
    return after


class RealtimeChartPanel(QWidget):
    SLOT_VIEW_SPECS = SLOT_VIEW_SPECS
    WINDOW_OPTIONS = WINDOW_OPTIONS
    PRESETS = PRESETS

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme_name = "dark"
        self._theme_tokens = get_theme_tokens(self._theme_name)
        self._start = time.monotonic()
        self._times: list[float] = []
        self._series: dict[str, list[float]] = {field: [] for field in DISPLAY_FIELDS}
        self._field_seen: dict[str, int] = {field: 0 for field in DISPLAY_FIELDS}
        self._plot_data_cache: dict[tuple[int, str], tuple[list[float], list[float]]] = {}
        self._curves: dict[tuple[int, str], pg.PlotDataItem] = {}
        self._last_x_ranges: dict[int, tuple[float, float]] = {}
        self._slot_states: list[SlotState] = []
        self._slot_view_ids = list(DEFAULT_SLOT_VIEW_IDS)
        self._slot_refresh_dirty = [False, False]
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_INTERVAL_MS)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._flush_refresh)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义双图", "")
        for preset in PRESETS:
            self.preset_combo.addItem(preset.title, preset.preset_id)
        self.window_combo = QComboBox()
        for label, seconds in self.WINDOW_OPTIONS.items():
            self.window_combo.addItem(label, seconds)
        self.auto_range_check = QCheckBox("自动缩放")
        self.auto_range_check.setChecked(True)
        self.clear_button = QPushButton("清空曲线")
        self.cursor_label = QLabel("悬停读数：-")
        self.cursor_label.setProperty("muted", True)

        controls.addWidget(QLabel("双图预设"))
        controls.addWidget(self.preset_combo)
        controls.addWidget(QLabel("时间窗"))
        controls.addWidget(self.window_combo)
        controls.addWidget(self.auto_range_check)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        controls.addWidget(self.cursor_label)
        layout.addLayout(controls)

        self.hint_label = QLabel("上图和下图都可以独立切换显示内容。")
        self.hint_label.setWordWrap(True)
        self.hint_label.setProperty("muted", True)
        layout.addWidget(self.hint_label)

        pg.setConfigOptions(antialias=False)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_slot(slot_index=0, slot_name="上图"))
        splitter.addWidget(self._build_slot(slot_index=1, slot_name="下图"))
        splitter.setSizes([360, 280])
        layout.addWidget(splitter, 1)

        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self.window_combo.currentIndexChanged.connect(lambda *_: self.request_refresh(immediate=True))
        self.auto_range_check.toggled.connect(lambda *_: self.request_refresh(immediate=True))
        self.clear_button.clicked.connect(self.clear)

        self._sync_slot_combos()
        self.apply_theme(self._theme_name)
        self.request_refresh(immediate=True)

    def available_views(self) -> list[tuple[str, str]]:
        return [(spec.view_id, spec.title) for spec in SLOT_VIEW_SPECS]

    @staticmethod
    def friendly_metric_name(metric_key: str) -> str:
        return friendly_field_name(metric_key)

    def add_frame(self, frame: ParsedFrame) -> None:
        relative_ts = time.monotonic() - self._start
        self._times.append(relative_ts)
        display_values = build_display_value_map(frame)
        for field, values in self._series.items():
            numeric = display_values.get(field, math.nan)
            values.append(numeric)
            if math.isfinite(numeric):
                self._field_seen[field] += 1
        self.request_refresh()

    def clear(self) -> None:
        self._start = time.monotonic()
        self._times.clear()
        for field, values in self._series.items():
            values.clear()
            self._field_seen[field] = 0
        self._plot_data_cache.clear()
        self._last_x_ranges.clear()
        self._slot_refresh_dirty = [False, False]
        self.cursor_label.setText("悬停读数：-")
        self.request_refresh(immediate=True)

    def apply_theme(self, theme_name: str) -> None:
        self._theme_name = theme_name
        self._theme_tokens = get_theme_tokens(theme_name)
        for state in self._slot_states:
            self._configure_plot_widget(state.plot)
        for slot_index in range(len(self._slot_states)):
            self._refresh_slot(slot_index)
        self._update_global_hint()

    def set_slot_view(self, slot_index: int, view_id: str) -> bool:
        if not (0 <= slot_index < len(self._slot_states)):
            return False
        if view_id not in VIEW_BY_ID:
            return False
        if self._slot_view_ids[slot_index] == view_id:
            return True
        self._slot_view_ids[slot_index] = view_id
        self._slot_states[slot_index].combo.blockSignals(True)
        self._slot_states[slot_index].combo.setCurrentIndex(self._slot_states[slot_index].combo.findData(view_id))
        self._slot_states[slot_index].combo.blockSignals(False)
        self._update_preset_combo()
        self.request_refresh(slot_index=slot_index, immediate=True)
        return True

    def set_slot_metric(self, slot_index: int, metric_key: str) -> bool:
        view_id = METRIC_TO_VIEW.get(metric_key)
        if not view_id:
            return False
        return self.set_slot_view(slot_index, view_id)

    def clear_slot(self, slot_index: int) -> bool:
        return self.set_slot_view(slot_index, "empty")

    def restore_default_views(self) -> None:
        self._slot_view_ids = list(DEFAULT_SLOT_VIEW_IDS)
        self._sync_slot_combos()
        self._update_preset_combo()
        self.request_refresh(immediate=True)

    def slot_summary_text(self, slot_index: int) -> str:
        if not (0 <= slot_index < len(self._slot_view_ids)):
            return "--"
        spec = VIEW_BY_ID[self._slot_view_ids[slot_index]]
        if not spec.fields:
            return "未配置"
        return f"{spec.title}：{'、'.join(friendly_field_name(field) for field in spec.fields)}"

    def apply_preset(self, preset_id: str) -> bool:
        preset = next((item for item in PRESETS if item.preset_id == preset_id), None)
        if preset is None:
            return False
        self._slot_view_ids[0] = preset.upper_view_id
        self._slot_view_ids[1] = preset.lower_view_id
        self._sync_slot_combos()
        self._update_preset_combo()
        self.request_refresh(immediate=True)
        return True

    def request_refresh(self, *, slot_index: int | None = None, immediate: bool = False) -> None:
        if slot_index is None:
            self._slot_refresh_dirty = [True, True]
        elif 0 <= slot_index < len(self._slot_refresh_dirty):
            self._slot_refresh_dirty[slot_index] = True
        if immediate:
            self._flush_refresh(force=True)
            return
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _build_slot(self, *, slot_index: int, slot_name: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel(slot_name))
        combo = QComboBox()
        for spec in ALL_SLOT_VIEW_SPECS:
            combo.addItem(spec.title, spec.view_id)
        header.addWidget(combo, 1)
        note_label = QLabel("")
        note_label.setProperty("muted", True)
        header.addWidget(note_label, 2)
        layout.addLayout(header)

        plot_stack = QStackedWidget()
        placeholder = QLabel()
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setWordWrap(True)
        placeholder.setTextFormat(Qt.RichText)
        placeholder.setMinimumHeight(180)

        plot = pg.PlotWidget()
        plot.setMinimumHeight(200)
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.addLegend(offset=(8, 8))
        plot.setMenuEnabled(False)
        plot.getPlotItem().setClipToView(True)
        plot.setMouseEnabled(x=True, y=True)
        plot.setLabel("bottom", "时间", units="s", color=self._theme_tokens.chart_text)
        self._configure_plot_widget(plot)

        for spec in SLOT_VIEW_SPECS:
            for field in spec.fields:
                if (slot_index, field) in self._curves:
                    continue
                color = self._theme_tokens.field_colors.get(field, self._theme_tokens.chart_text)
                curve = plot.plot(name=friendly_field_name(field), pen=pg.mkPen(color, width=1.9))
                self._curves[(slot_index, field)] = curve

        plot_stack.addWidget(placeholder)
        plot_stack.addWidget(plot)
        layout.addWidget(plot_stack, 1)

        plot.scene().sigMouseMoved.connect(self._mouse_moved_factory(slot_index))
        combo.currentIndexChanged.connect(lambda *_: self._slot_view_changed(slot_index))

        state = SlotState(
            slot_index=slot_index,
            slot_name=slot_name,
            combo=combo,
            plot=plot,
            plot_stack=plot_stack,
            placeholder=placeholder,
            note_label=note_label,
        )
        self._slot_states.append(state)
        return container

    def _slot_view_changed(self, slot_index: int) -> None:
        state = self._slot_states[slot_index]
        view_id = str(state.combo.currentData() or self._slot_view_ids[slot_index])
        self._slot_view_ids[slot_index] = view_id
        self._update_preset_combo()
        self.request_refresh(slot_index=slot_index, immediate=True)

    def _preset_changed(self) -> None:
        preset_id = str(self.preset_combo.currentData() or "")
        if not preset_id:
            return
        self.apply_preset(preset_id)

    def _sync_slot_combos(self) -> None:
        for slot_index, state in enumerate(self._slot_states):
            state.combo.blockSignals(True)
            state.combo.setCurrentIndex(state.combo.findData(self._slot_view_ids[slot_index]))
            state.combo.blockSignals(False)

    def _update_preset_combo(self) -> None:
        matched = next(
            (
                preset
                for preset in PRESETS
                if preset.upper_view_id == self._slot_view_ids[0] and preset.lower_view_id == self._slot_view_ids[1]
            ),
            None,
        )
        self.preset_combo.blockSignals(True)
        if matched is None:
            self.preset_combo.setCurrentIndex(0)
        else:
            self.preset_combo.setCurrentIndex(self.preset_combo.findData(matched.preset_id))
        self.preset_combo.blockSignals(False)

    def _flush_refresh(self, *, force: bool = False) -> None:
        if not force and not any(self._slot_refresh_dirty):
            return
        if not self.isVisible():
            return
        for slot_index in range(len(self._slot_states)):
            if force or self._slot_refresh_dirty[slot_index]:
                self._slot_refresh_dirty[slot_index] = False
                self._refresh_slot(slot_index)
        self._update_global_hint()

    def _refresh_slot(self, slot_index: int) -> None:
        state = self._slot_states[slot_index]
        spec = VIEW_BY_ID[self._slot_view_ids[slot_index]]
        times = list(self._times)
        visible_times, start_index = self._visible_times(times)
        state.plot.setLabel("left", spec.y_label, units=spec.units, color=self._theme_tokens.chart_text)
        state.note_label.setText(spec.summary)
        state.plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=self.auto_range_check.isChecked())

        sample_has_data = [False] * len(visible_times)
        seen_field_count = sum(1 for field in spec.fields if self._field_seen.get(field, 0) > 0)

        active_fields = set(spec.fields)
        for field in DISPLAY_FIELDS:
            curve = self._curves[(slot_index, field)]
            if field not in active_fields:
                curve.setData([], [])
                self._plot_data_cache[(slot_index, field)] = ([], [])
                continue

            visible_values = self._series[field][start_index:]
            filtered_times, filtered_values = filter_finite_points(visible_times, visible_values)
            self._plot_data_cache[(slot_index, field)] = (filtered_times, filtered_values)

            for index, raw_value in enumerate(visible_values):
                if is_finite_number(raw_value):
                    sample_has_data[index] = True

            self._apply_curve_data(slot_index, field, filtered_times, filtered_values)

        valid_times = [visible_times[index] for index, has_data in enumerate(sample_has_data) if has_data]
        state_result = describe_group_state(
            total_frames=len(times),
            enabled_field_count=len(spec.fields),
            seen_field_count=seen_field_count,
            visible_sample_count=len(valid_times),
        )
        placeholder_title = state_result.placeholder_title or f"{spec.title} 暂无可绘制数据"
        placeholder_body = state_result.placeholder_body or "请切换视图或等待更多数据。"
        state.placeholder.setText(placeholder_html(placeholder_title, placeholder_body, theme=self._theme_name))
        state.plot_stack.setCurrentIndex(1 if state_result.show_plot else 0)
        if state_result.show_plot:
            axis_window = build_time_axis_window(valid_times)
            if axis_window is not None:
                self._apply_x_range(slot_index, axis_window)

    def _update_global_hint(self) -> None:
        upper_title = VIEW_BY_ID[self._slot_view_ids[0]].title
        lower_title = VIEW_BY_ID[self._slot_view_ids[1]].title
        self.hint_label.setText(f"上图：{upper_title} | 下图：{lower_title}")

    def _visible_times(self, times: Sequence[float]) -> tuple[list[float], int]:
        if not times:
            return [], 0
        window_s = self.window_combo.currentData()
        start_index = 0
        if window_s is not None:
            min_time = times[-1] - float(window_s)
            for index, value in enumerate(times):
                if value >= min_time:
                    start_index = index
                    break
        return list(times[start_index:]), start_index

    def _apply_curve_data(self, slot_index: int, field: str, times: list[float], values: list[float]) -> None:
        curve = self._curves[(slot_index, field)]
        color = self._theme_tokens.field_colors.get(field, self._theme_tokens.chart_text)
        point_count = len(times)
        symbol: str | None = None
        symbol_size = 0
        if point_count <= 2:
            symbol = "o"
            symbol_size = 8
        elif point_count <= 8:
            symbol = "o"
            symbol_size = 5

        data_kwargs: dict[str, Any] = {
            "x": times,
            "y": values,
            "pen": pg.mkPen(color, width=1.9),
            "connect": "finite",
        }
        if symbol is not None:
            data_kwargs.update(
                {
                    "symbol": symbol,
                    "symbolSize": symbol_size,
                    "symbolBrush": pg.mkBrush(color),
                    "symbolPen": pg.mkPen(self._theme_tokens.chart_symbol_outline, width=1.0),
                }
            )
        curve.setData(**data_kwargs)

    def _configure_plot_widget(self, plot: pg.PlotWidget) -> None:
        tokens = self._theme_tokens
        plot.setBackground(tokens.chart_bg)
        plot.showGrid(x=True, y=True, alpha=0.35 if tokens.name == "light" else 0.25)
        plot_item = plot.getPlotItem()
        for axis_name in ("left", "bottom"):
            axis = plot_item.getAxis(axis_name)
            axis.setPen(pg.mkPen(tokens.chart_axis, width=1.0))
            axis.setTextPen(pg.mkPen(tokens.chart_text, width=1.0))
        legend = plot_item.legend
        if legend is not None:
            try:
                legend.setBrush(pg.mkBrush(tokens.chart_legend_bg))
                legend.setPen(pg.mkPen(tokens.chart_legend_border, width=1.0))
            except Exception:
                pass

    def _apply_x_range(self, slot_index: int, axis_window: TimeAxisWindow) -> None:
        current = (axis_window.start, axis_window.end)
        previous = self._last_x_ranges.get(slot_index)
        if previous is not None:
            if abs(previous[0] - current[0]) <= XRANGE_EPSILON and abs(previous[1] - current[1]) <= XRANGE_EPSILON:
                return
        self._slot_states[slot_index].plot.setXRange(axis_window.start, axis_window.end, padding=0.0)
        self._last_x_ranges[slot_index] = current

    def _mouse_moved_factory(self, slot_index: int):
        def handle(position: QPointF) -> None:
            state = self._slot_states[slot_index]
            if not state.plot.sceneBoundingRect().contains(position):
                return
            mouse_point = state.plot.getPlotItem().vb.mapSceneToView(position)
            nearest = self._nearest_point(slot_index, mouse_point.x(), mouse_point.y())
            view_title = VIEW_BY_ID[self._slot_view_ids[slot_index]].title
            if nearest is None:
                self.cursor_label.setText(
                    f"悬停读数：{state.slot_name} {view_title} | 时间 {mouse_point.x():.2f} s | 数值 {mouse_point.y():.4f}"
                )
                return
            field, point_x, point_y = nearest
            self.cursor_label.setText(
                f"悬停读数：{state.slot_name} {view_title} | {friendly_field_name(field)} = {point_y:.5f} @ {point_x:.2f} s"
            )

        return handle

    def _nearest_point(self, slot_index: int, mouse_x: float, mouse_y: float) -> tuple[str, float, float] | None:
        best_match: tuple[str, float, float] | None = None
        best_score: tuple[float, float] | None = None
        spec = VIEW_BY_ID[self._slot_view_ids[slot_index]]
        for field in spec.fields:
            times, values = self._plot_data_cache.get((slot_index, field), ([], []))
            if not times:
                continue
            nearest_index = nearest_time_index(times, mouse_x)
            candidate = (field, times[nearest_index], values[nearest_index])
            score = (abs(candidate[1] - mouse_x), abs(candidate[2] - mouse_y))
            if best_score is None or score < best_score:
                best_match = candidate
                best_score = score
        return best_match

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if any(self._slot_refresh_dirty):
            QTimer.singleShot(0, lambda: self._flush_refresh(force=True))
