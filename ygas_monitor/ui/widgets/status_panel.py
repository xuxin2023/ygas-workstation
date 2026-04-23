"""Status register decoding panel and event timeline."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...models import AlarmEvent, ParsedFrame
from ...protocols.ygas import YGasProtocol


class StatusPanel(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._pending_frame: ParsedFrame | None = None
        self._pending_events: list[tuple[str, str, str]] = []
        self._last_status_key: tuple[str, int] | None = None
        self._last_device_text = "设备: --"
        self._last_mode_text = "模式: --"
        self._last_status_text = "状态寄存器: --"

        layout = QVBoxLayout(self)
        summary_row = QHBoxLayout()
        self.device_label = QLabel(self._last_device_text)
        self.mode_label = QLabel(self._last_mode_text)
        self.status_label = QLabel(self._last_status_text)
        summary_row.addWidget(self.device_label)
        summary_row.addWidget(self.mode_label)
        summary_row.addWidget(self.status_label)
        summary_row.addStretch(1)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["位", "名称", "当前状态", "说明"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)

        event_box = QGroupBox("状态位事件时间线")
        event_layout = QVBoxLayout(event_box)
        self.events = QListWidget()
        event_layout.addWidget(self.events)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.table)
        splitter.addWidget(event_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout.addLayout(summary_row)
        layout.addWidget(splitter, 1)

    def update_frame(self, frame: ParsedFrame) -> None:
        self._pending_frame = frame
        if not self.isVisible():
            return
        self._apply_frame(frame)

    def _apply_frame(self, frame: ParsedFrame) -> None:
        self._set_label(self.device_label, f"设备: {frame.device_id or '--'}", "_last_device_text")
        self._set_label(self.mode_label, f"模式: MODE{frame.mode}", "_last_mode_text")
        self._set_label(self.status_label, f"状态寄存器: {frame.status or '--'}", "_last_status_text")

        status_key = (frame.status or "", int(frame.mode))
        if status_key == self._last_status_key:
            return

        decoded = YGasProtocol.decode_status(frame.status)
        self.table.setRowCount(len(decoded))
        for row, item in enumerate(decoded):
            self.table.setItem(row, 0, QTableWidgetItem(str(item.bit)))
            self.table.setItem(row, 1, QTableWidgetItem(item.label))
            self.table.setItem(row, 2, QTableWidgetItem(item.state_text))
            self.table.setItem(row, 3, QTableWidgetItem(item.description))
        self._last_status_key = status_key

    def append_alarm(self, alarm: AlarmEvent) -> None:
        self.append_event("状态", alarm.timestamp.strftime("%H:%M:%S.%f")[:-3], alarm.message)

    def append_event(self, category: str, time_text: str, message: str) -> None:
        if not self.isVisible():
            self._pending_events.append((category, time_text, message))
            return
        item = QListWidgetItem(f"[{category}] {time_text} | {message}")
        self.events.insertItem(0, item)

    def clear(self) -> None:
        self._pending_frame = None
        self._pending_events.clear()
        self._last_status_key = None
        self._set_label(self.device_label, "设备: --", "_last_device_text")
        self._set_label(self.mode_label, "模式: --", "_last_mode_text")
        self._set_label(self.status_label, "状态寄存器: --", "_last_status_text")
        self.table.setRowCount(0)
        self.events.clear()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._pending_frame is not None:
            self._apply_frame(self._pending_frame)
        self._flush_pending_events()

    def _set_label(self, label: QLabel, text: str, attr_name: str) -> None:
        if getattr(self, attr_name) == text:
            return
        setattr(self, attr_name, text)
        label.setText(text)

    def _flush_pending_events(self) -> None:
        if not self._pending_events:
            return
        pending = list(reversed(self._pending_events))
        self._pending_events.clear()
        for category, time_text, message in pending:
            item = QListWidgetItem(f"[{category}] {time_text} | {message}")
            self.events.insertItem(0, item)
