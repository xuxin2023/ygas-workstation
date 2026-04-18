"""Status register decoding panel and event timeline."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
        layout = QVBoxLayout(self)
        summary_row = QHBoxLayout()
        self.device_label = QLabel("设备: --")
        self.mode_label = QLabel("模式: --")
        self.status_label = QLabel("状态寄存器: --")
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

        layout.addLayout(summary_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(event_box, 1)

    def update_frame(self, frame: ParsedFrame) -> None:
        self.device_label.setText(f"设备: {frame.device_id or '--'}")
        self.mode_label.setText(f"模式: MODE{frame.mode}")
        self.status_label.setText(f"状态寄存器: {frame.status or '--'}")

        decoded = YGasProtocol.decode_status(frame.status)
        self.table.setRowCount(len(decoded))
        for row, item in enumerate(decoded):
            self.table.setItem(row, 0, QTableWidgetItem(str(item.bit)))
            self.table.setItem(row, 1, QTableWidgetItem(item.label))
            self.table.setItem(row, 2, QTableWidgetItem(item.state_text))
            self.table.setItem(row, 3, QTableWidgetItem(item.description))

    def append_alarm(self, alarm: AlarmEvent) -> None:
        self.append_event("状态", alarm.timestamp.strftime("%H:%M:%S.%f")[:-3], alarm.message)

    def append_event(self, category: str, time_text: str, message: str) -> None:
        item = QListWidgetItem(f"[{category}] {time_text} | {message}")
        self.events.insertItem(0, item)

    def clear(self) -> None:
        self.device_label.setText("设备: --")
        self.mode_label.setText("模式: --")
        self.status_label.setText("状态寄存器: --")
        self.table.setRowCount(0)
        self.events.clear()
