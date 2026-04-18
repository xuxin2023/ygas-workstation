"""Realtime chart workspace built on pyqtgraph."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
import pyqtgraph as pg

from ...models import ParsedFrame


class RealtimeChartPanel(QWidget):
    GROUPS: dict[str, list[str]] = {
        "浓度组": ["co2_ppm", "h2o_mmol", "co2_density", "h2o_density"],
        "比值/信号组": ["co2_ratio_f", "co2_ratio_raw", "h2o_ratio_f", "h2o_ratio_raw", "ref_signal", "co2_signal", "h2o_signal"],
        "温压组": ["temperature_c", "chamber_temp_c", "case_temp_c", "pressure_kpa"],
        "质量/状态组": ["active_alarm_count", "status_numeric"],
    }
    WINDOW_OPTIONS = {
        "30秒": 30.0,
        "2分钟": 120.0,
        "10分钟": 600.0,
        "全会话": None,
    }
    COLORS = ["#7edbb5", "#74b3ff", "#ffd27d", "#ff8e8e", "#c0a5ff", "#7ce3ff", "#f6b26b"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._start = time.monotonic()
        self._times: list[float] = []
        self._series: dict[str, list[float]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._plots: dict[str, pg.PlotWidget] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QHBoxLayout()
        self.view_combo = QComboBox()
        self.view_combo.addItems(list(self.GROUPS))
        self.window_combo = QComboBox()
        for label in self.WINDOW_OPTIONS:
            self.window_combo.addItem(label, self.WINDOW_OPTIONS[label])
        self.auto_range_check = QCheckBox("自动缩放")
        self.auto_range_check.setChecked(True)
        self.clear_button = QPushButton("清空当前曲线")
        self.cursor_label = QLabel("鼠标位置: --")
        self.cursor_label.setProperty("muted", True)

        controls.addWidget(QLabel("主视图"))
        controls.addWidget(self.view_combo)
        controls.addWidget(QLabel("时间窗"))
        controls.addWidget(self.window_combo)
        controls.addWidget(self.auto_range_check)
        controls.addWidget(self.clear_button)
        controls.addStretch(1)
        controls.addWidget(self.cursor_label)
        layout.addLayout(controls)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        pg.setConfigOptions(antialias=True)

        for title, fields in self.GROUPS.items():
            box = QGroupBox(title)
            box_layout = QGridLayout(box)
            plot = pg.PlotWidget()
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.addLegend(offset=(8, 8))
            plot.setMenuEnabled(False)
            plot.getPlotItem().setClipToView(True)
            plot.setMouseEnabled(x=True, y=True)
            plot.setLabel("bottom", "Time", units="s")
            box_layout.addWidget(plot, 0, 0)
            self.stack.addWidget(box)
            self._plots[title] = plot
            for field_index, field in enumerate(fields):
                curve = plot.plot(name=field, pen=pg.mkPen(self.COLORS[field_index % len(self.COLORS)], width=1.8))
                self._curves[field] = curve
                self._series[field] = []
            plot.scene().sigMouseMoved.connect(self._mouse_moved_factory(title))

        self.view_combo.currentTextChanged.connect(self._change_view)
        self.window_combo.currentTextChanged.connect(lambda *_: self._refresh())
        self.auto_range_check.toggled.connect(lambda *_: self._refresh())
        self.clear_button.clicked.connect(self.clear)
        self._change_view(self.view_combo.currentText())

    def add_frame(self, frame: ParsedFrame) -> None:
        relative_ts = time.monotonic() - self._start
        self._times.append(relative_ts)
        for field, values in self._series.items():
            raw = frame.fields.get(field)
            values.append(float(raw) if isinstance(raw, (int, float)) else math.nan)
        self._refresh()

    def clear(self) -> None:
        self._start = time.monotonic()
        self._times.clear()
        for series in self._series.values():
            series.clear()
        self.cursor_label.setText("鼠标位置: --")
        self._refresh()

    def _change_view(self, title: str) -> None:
        titles = list(self.GROUPS)
        if title in titles:
            self.stack.setCurrentIndex(titles.index(title))
        self._refresh()

    def _refresh(self) -> None:
        if not self._times:
            for curve in self._curves.values():
                curve.setData([], [])
            return

        times = list(self._times)
        window_s = self.window_combo.currentData()
        start_index = 0
        if window_s is not None:
            min_time = times[-1] - float(window_s)
            for index, value in enumerate(times):
                if value >= min_time:
                    start_index = index
                    break
        visible_times = times[start_index:]
        current_group = self.view_combo.currentText()
        for title, fields in self.GROUPS.items():
            plot = self._plots[title]
            if self.auto_range_check.isChecked():
                plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            else:
                plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            for field in fields:
                self._curves[field].setData(visible_times, self._series[field][start_index:])
            if visible_times:
                plot.setXRange(visible_times[0], visible_times[-1], padding=0.02)
            plot.setVisible(title == current_group)

    def _mouse_moved_factory(self, title: str):
        def handle(position: QPointF) -> None:
            if title != self.view_combo.currentText():
                return
            plot = self._plots[title]
            if not plot.sceneBoundingRect().contains(position):
                return
            mouse_point = plot.getPlotItem().vb.mapSceneToView(position)
            self.cursor_label.setText(f"鼠标位置: t={mouse_point.x():.1f}s, y={mouse_point.y():.4f}")

        return handle
