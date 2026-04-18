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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from ...models import ParsedFrame

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
    "co2_ratio_raw": "CO2 原始比值",
    "co2_ratio_f": "CO2 滤波比值",
    "co2_ratio_delta": "CO2 滤波差值",
    "h2o_ratio_raw": "H2O 原始比值",
    "h2o_ratio_f": "H2O 滤波比值",
    "h2o_ratio_delta": "H2O 滤波差值",
    "ref_signal": "参考信号",
    "co2_signal": "CO2 信号",
    "h2o_signal": "H2O 信号",
    "temperature_c": "温度",
    "chamber_temp_c": "腔温",
    "case_temp_c": "壳温",
    "pressure_kpa": "压力",
    "active_alarm_count": "告警数",
    "status_numeric": "状态数值",
}

FIELD_COLORS = {
    "co2_ppm": "#7edbb5",
    "co2_density": "#47c48c",
    "h2o_mmol": "#74b3ff",
    "h2o_density": "#4f84ff",
    "co2_ratio_raw": "#ffd27d",
    "co2_ratio_f": "#ff9f40",
    "co2_ratio_delta": "#ff6b6b",
    "h2o_ratio_raw": "#c0a5ff",
    "h2o_ratio_f": "#9c7cff",
    "h2o_ratio_delta": "#ff7aa8",
    "ref_signal": "#7ce3ff",
    "co2_signal": "#5fc9d8",
    "h2o_signal": "#29b6f6",
    "temperature_c": "#f6b26b",
    "chamber_temp_c": "#ff8e8e",
    "case_temp_c": "#ffcc80",
    "pressure_kpa": "#a2d149",
    "active_alarm_count": "#ff8e8e",
    "status_numeric": "#c9d1d9",
}


@dataclass(frozen=True)
class PlotSpec:
    key: str
    title: str
    fields: tuple[str, ...]
    y_label: str
    units: str = ""
    min_height: int = 170


@dataclass(frozen=True)
class GroupSpec:
    title: str
    plots: tuple[PlotSpec, ...]
    columns: int = 1
    summary: str = ""


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


GROUP_SPECS: tuple[GroupSpec, ...] = (
    GroupSpec(
        title="CO2 主监测",
        summary="关注 CO2 浓度变化，密度曲线作为补充参考。",
        plots=(
            PlotSpec("co2_ppm_plot", "CO2 浓度趋势", ("co2_ppm",), "浓度", "ppm", min_height=210),
            PlotSpec("co2_density_plot", "CO2 密度趋势", ("co2_density",), "密度", "mg/m3", min_height=170),
        ),
    ),
    GroupSpec(
        title="H2O 主监测",
        summary="关注 H2O 浓度变化，密度曲线作为补充参考。",
        plots=(
            PlotSpec("h2o_mmol_plot", "H2O 浓度趋势", ("h2o_mmol",), "浓度", "mmol/mol", min_height=210),
            PlotSpec("h2o_density_plot", "H2O 密度趋势", ("h2o_density",), "密度", "g/m3", min_height=170),
        ),
    ),
    GroupSpec(
        title="比值对比",
        summary="对比原始比值、滤波比值和滤波差值，便于现场观察滤波效果。",
        columns=2,
        plots=(
            PlotSpec("co2_ratio_plot", "CO2 原始 / 滤波比值", ("co2_ratio_raw", "co2_ratio_f"), "比值", "", min_height=170),
            PlotSpec("co2_delta_plot", "CO2 滤波差值", ("co2_ratio_delta",), "差值", "ratio", min_height=170),
            PlotSpec("h2o_ratio_plot", "H2O 原始 / 滤波比值", ("h2o_ratio_raw", "h2o_ratio_f"), "比值", "", min_height=170),
            PlotSpec("h2o_delta_plot", "H2O 滤波差值", ("h2o_ratio_delta",), "差值", "ratio", min_height=170),
        ),
    ),
    GroupSpec(
        title="信号质量",
        summary="观察参考通道与 CO2/H2O 信号强度。",
        plots=(
            PlotSpec("signal_quality_plot", "参考 / 采样信号", ("ref_signal", "co2_signal", "h2o_signal"), "信号", "", min_height=260),
        ),
    ),
    GroupSpec(
        title="温度",
        summary="查看主温度、腔温与壳温。",
        plots=(
            PlotSpec("temperature_plot", "温度趋势", ("temperature_c", "chamber_temp_c", "case_temp_c"), "温度", "℃", min_height=260),
        ),
    ),
    GroupSpec(
        title="压力 / 状态",
        summary="分开查看压力与状态 / 告警，避免不同量纲混画。",
        plots=(
            PlotSpec("pressure_plot", "压力趋势", ("pressure_kpa",), "压力", "kPa", min_height=190),
            PlotSpec("state_plot", "状态 / 告警趋势", ("active_alarm_count", "status_numeric"), "状态 / 告警", "", min_height=170),
        ),
    ),
)

GROUP_BY_TITLE = {spec.title: spec for spec in GROUP_SPECS}
PLOT_BY_KEY = {plot.key: plot for group in GROUP_SPECS for plot in group.plots}
PLOT_GROUP = {plot.key: group.title for group in GROUP_SPECS for plot in group.plots}
DISPLAY_FIELDS = tuple(dict.fromkeys(field for group in GROUP_SPECS for plot in group.plots for field in plot.fields))


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
            banner_text="当前图组的曲线已全部关闭。",
            placeholder_title="当前图组没有启用曲线",
            placeholder_body="请勾选至少一条曲线后再查看。",
        )
    if visible_sample_count <= 0 and seen_field_count <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="当前模式下这组字段暂不可用。",
            placeholder_title="当前图组暂无可绘制数据",
            placeholder_body="请切换图组或等待更多数据。",
        )
    if visible_sample_count <= 0:
        return GroupDisplayState(
            show_plot=False,
            banner_text="当前时间窗内暂无有效点。",
            placeholder_title="当前图组暂无可绘制数据",
            placeholder_body="请切换图组或等待更多数据。",
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


def placeholder_html(title: str, body: str) -> str:
    return (
        "<div style='text-align:center; padding:24px;'>"
        f"<div style='font-size:18px; font-weight:700; color:#f7fbff; margin-bottom:8px;'>{title}</div>"
        f"<div style='font-size:13px; color:#9fb0c0; line-height:1.6;'>{body}</div>"
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
    group = GROUP_BY_TITLE[group_title]
    return list(dict.fromkeys(field for plot in group.plots for field in plot.fields))


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
    GROUP_SPECS = GROUP_SPECS
    WINDOW_OPTIONS = WINDOW_OPTIONS

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._start = time.monotonic()
        self._times: list[float] = []
        self._series: dict[str, list[float]] = {field: [] for field in DISPLAY_FIELDS}
        self._field_seen: dict[str, int] = {field: 0 for field in DISPLAY_FIELDS}
        self._field_visible: dict[str, bool] = {field: True for field in DISPLAY_FIELDS}
        self._curves: dict[tuple[str, str], pg.PlotDataItem] = {}
        self._plots: dict[str, pg.PlotWidget] = {}
        self._plot_pages: dict[str, QStackedWidget] = {}
        self._plot_placeholders: dict[str, QLabel] = {}
        self._plot_data_cache: dict[tuple[str, str], tuple[list[float], list[float]]] = {}
        self._last_x_ranges: dict[str, tuple[float, float]] = {}
        self._refresh_dirty = False
        self._toggle_group_title = ""
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
        self.view_combo = QComboBox()
        self.view_combo.addItems([spec.title for spec in self.GROUP_SPECS])
        self.window_combo = QComboBox()
        for label, seconds in self.WINDOW_OPTIONS.items():
            self.window_combo.addItem(label, seconds)
        self.auto_range_check = QCheckBox("自动缩放")
        self.auto_range_check.setChecked(True)
        self.clear_button = QPushButton("清空曲线")
        self.cursor_label = QLabel("悬停读数：-")
        self.cursor_label.setProperty("muted", True)

        controls.addWidget(QLabel("图组"))
        controls.addWidget(self.view_combo)
        controls.addWidget(QLabel("时间窗"))
        controls.addWidget(self.window_combo)
        controls.addWidget(self.auto_range_check)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        controls.addWidget(self.cursor_label)
        layout.addLayout(controls)

        self.series_toggle_widget = QWidget()
        self.series_toggle_layout = QGridLayout(self.series_toggle_widget)
        self.series_toggle_layout.setContentsMargins(0, 0, 0, 0)
        self.series_toggle_layout.setHorizontalSpacing(10)
        self.series_toggle_layout.setVerticalSpacing(6)
        layout.addWidget(self.series_toggle_widget)

        self.hint_label = QLabel("等待实时数据。")
        self.hint_label.setWordWrap(True)
        self.hint_label.setProperty("muted", True)
        layout.addWidget(self.hint_label)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        pg.setConfigOptions(antialias=False)

        for group in self.GROUP_SPECS:
            self.stack.addWidget(self._build_group_page(group))

        self.view_combo.currentTextChanged.connect(self._change_view)
        self.window_combo.currentIndexChanged.connect(lambda *_: self.request_refresh(immediate=True))
        self.auto_range_check.toggled.connect(lambda *_: self.request_refresh(immediate=True))
        self.clear_button.clicked.connect(self.clear)

        self._rebuild_series_toggles(self.view_combo.currentText())
        self._change_view(self.view_combo.currentText())

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
        for field, series in self._series.items():
            series.clear()
            self._field_seen[field] = 0
        self._plot_data_cache.clear()
        self._last_x_ranges.clear()
        self._refresh_dirty = False
        self.cursor_label.setText("悬停读数：-")
        self.request_refresh(immediate=True)

    def _build_group_page(self, group: GroupSpec) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        for index, plot_spec in enumerate(group.plots):
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(6)

            title_label = QLabel(plot_spec.title)
            title_label.setProperty("muted", True)
            section_layout.addWidget(title_label)

            plot_stack = QStackedWidget()
            placeholder = QLabel()
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setWordWrap(True)
            placeholder.setMinimumHeight(plot_spec.min_height)
            placeholder.setTextFormat(Qt.RichText)
            placeholder.setText(
                placeholder_html(
                    "尚未收到任何帧",
                    "连接设备、启动模拟器或开始回放后，此处会自动显示实时曲线。",
                )
            )

            plot = pg.PlotWidget()
            plot.setMinimumHeight(plot_spec.min_height)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.addLegend(offset=(8, 8))
            plot.setMenuEnabled(False)
            plot.getPlotItem().setClipToView(True)
            plot.setMouseEnabled(x=True, y=True)
            plot.setLabel("bottom", "时间", units="s")
            plot.setLabel("left", plot_spec.y_label, units=plot_spec.units)

            plot_stack.addWidget(placeholder)
            plot_stack.addWidget(plot)
            section_layout.addWidget(plot_stack, 1)

            row = index // group.columns
            column = index % group.columns
            grid.addWidget(section, row, column)
            grid.setColumnStretch(column, 1)
            grid.setRowStretch(row, 1)

            self._plots[plot_spec.key] = plot
            self._plot_pages[plot_spec.key] = plot_stack
            self._plot_placeholders[plot_spec.key] = placeholder

            for field in plot_spec.fields:
                color = FIELD_COLORS.get(field, "#f7fbff")
                curve = plot.plot(name=friendly_field_name(field), pen=pg.mkPen(color, width=1.9))
                self._curves[(plot_spec.key, field)] = curve

            plot.scene().sigMouseMoved.connect(self._mouse_moved_factory(plot_spec.key))

        page_layout.addWidget(content, 1)
        return page

    def _change_view(self, title: str) -> None:
        titles = [spec.title for spec in self.GROUP_SPECS]
        if title in titles:
            self.stack.setCurrentIndex(titles.index(title))
        if title != self._toggle_group_title:
            self._rebuild_series_toggles(title)
        self.request_refresh(immediate=True)

    def request_refresh(self, *, immediate: bool = False) -> None:
        self._refresh_dirty = True
        if immediate:
            self._flush_refresh(force=True)
            return
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_refresh(self, *, force: bool = False) -> None:
        if not force and not self._refresh_dirty:
            return
        if not self.isVisible():
            return
        self._refresh_dirty = False
        self._refresh_current_group()

    def _refresh_current_group(self) -> None:
        current_group = self.view_combo.currentText()
        group = GROUP_BY_TITLE[current_group]
        times = list(self._times)
        visible_times, start_index = self._visible_times(times)
        group_sample_has_data = [False] * len(visible_times)
        enabled_group_fields = [field for field in group_field_order(group.title) if self._field_visible.get(field, True)]
        seen_group_count = sum(1 for field in enabled_group_fields if self._field_seen.get(field, 0) > 0)

        for plot_spec in group.plots:
            plot = self._plots[plot_spec.key]
            plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=self.auto_range_check.isChecked())

            sample_has_data = [False] * len(visible_times)
            enabled_fields = [field for field in plot_spec.fields if self._field_visible.get(field, True)]
            seen_field_count = sum(1 for field in enabled_fields if self._field_seen.get(field, 0) > 0)

            for field in plot_spec.fields:
                curve = self._curves[(plot_spec.key, field)]
                if field not in enabled_fields:
                    curve.setData([], [])
                    self._plot_data_cache[(plot_spec.key, field)] = ([], [])
                    continue

                visible_values = self._series[field][start_index:]
                filtered_times, filtered_values = filter_finite_points(visible_times, visible_values)
                self._plot_data_cache[(plot_spec.key, field)] = (filtered_times, filtered_values)

                for index, raw_value in enumerate(visible_values):
                    if is_finite_number(raw_value):
                        sample_has_data[index] = True
                        group_sample_has_data[index] = True

                self._apply_curve_data(plot_spec.key, field, filtered_times, filtered_values)

            valid_times = [visible_times[index] for index, has_data in enumerate(sample_has_data) if has_data]
            plot_state = describe_group_state(
                total_frames=len(times),
                enabled_field_count=len(enabled_fields),
                seen_field_count=seen_field_count,
                visible_sample_count=len(valid_times),
            )
            placeholder_title = plot_state.placeholder_title or f"{plot_spec.title} 暂无可绘制数据"
            placeholder_body = plot_state.placeholder_body or "请切换图组或等待更多数据。"
            self._plot_placeholders[plot_spec.key].setText(placeholder_html(placeholder_title, placeholder_body))
            self._plot_pages[plot_spec.key].setCurrentIndex(1 if plot_state.show_plot else 0)
            if plot_state.show_plot:
                axis_window = build_time_axis_window(valid_times)
                if axis_window is not None:
                    self._apply_x_range(plot_spec.key, axis_window)

        valid_group_times = [visible_times[index] for index, has_data in enumerate(group_sample_has_data) if has_data]
        group_state = describe_group_state(
            total_frames=len(times),
            enabled_field_count=len(enabled_group_fields),
            seen_field_count=seen_group_count,
            visible_sample_count=len(valid_group_times),
        )
        if group.summary:
            self.hint_label.setText(f"{group.summary} {group_state.banner_text}")
        else:
            self.hint_label.setText(group_state.banner_text)

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

    def _apply_curve_data(self, plot_key: str, field: str, times: list[float], values: list[float]) -> None:
        curve = self._curves[(plot_key, field)]
        color = FIELD_COLORS.get(field, "#f7fbff")
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
                    "symbolPen": pg.mkPen("#f7fbff", width=1.0),
                }
            )
        curve.setData(**data_kwargs)

    def _apply_x_range(self, plot_key: str, axis_window: TimeAxisWindow) -> None:
        current = (axis_window.start, axis_window.end)
        previous = self._last_x_ranges.get(plot_key)
        if previous is not None:
            if abs(previous[0] - current[0]) <= XRANGE_EPSILON and abs(previous[1] - current[1]) <= XRANGE_EPSILON:
                return
        self._plots[plot_key].setXRange(axis_window.start, axis_window.end, padding=0.0)
        self._last_x_ranges[plot_key] = current

    def _rebuild_series_toggles(self, title: str) -> None:
        while self.series_toggle_layout.count():
            item = self.series_toggle_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, field in enumerate(group_field_order(title)):
            checkbox = QCheckBox(friendly_field_name(field))
            checkbox.setChecked(self._field_visible.get(field, True))
            checkbox.setStyleSheet(f"color: {FIELD_COLORS.get(field, '#f7fbff')};")
            checkbox.toggled.connect(lambda checked, name=field: self._toggle_field(name, checked))
            self.series_toggle_layout.addWidget(checkbox, index // 4, index % 4)
        self._toggle_group_title = title

    def _toggle_field(self, field: str, checked: bool) -> None:
        self._field_visible[field] = checked
        self.request_refresh(immediate=True)

    def _mouse_moved_factory(self, plot_key: str):
        def handle(position: QPointF) -> None:
            if PLOT_GROUP[plot_key] != self.view_combo.currentText():
                return
            plot = self._plots[plot_key]
            if not plot.sceneBoundingRect().contains(position):
                return
            mouse_point = plot.getPlotItem().vb.mapSceneToView(position)
            nearest = self._nearest_point(plot_key, mouse_point.x(), mouse_point.y())
            plot_title = PLOT_BY_KEY[plot_key].title
            if nearest is None:
                self.cursor_label.setText(
                    f"悬停读数：{plot_title} | 时间 {mouse_point.x():.2f} s | 数值 {mouse_point.y():.4f}"
                )
                return
            field, point_x, point_y = nearest
            self.cursor_label.setText(
                f"悬停读数：{plot_title} | {friendly_field_name(field)} = {point_y:.5f} @ {point_x:.2f} s"
            )

        return handle

    def _nearest_point(self, plot_key: str, mouse_x: float, mouse_y: float) -> tuple[str, float, float] | None:
        best_match: tuple[str, float, float] | None = None
        best_score: tuple[float, float] | None = None

        for field in PLOT_BY_KEY[plot_key].fields:
            if not self._field_visible.get(field, True):
                continue
            times, values = self._plot_data_cache.get((plot_key, field), ([], []))
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
        if self._refresh_dirty:
            QTimer.singleShot(0, lambda: self._flush_refresh(force=True))
