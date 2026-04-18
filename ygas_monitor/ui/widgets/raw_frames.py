"""Raw frame viewer with pause, filter, and broadcast/error views."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...models import RawFrameRecord


class RawFramesWidget(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._records: deque[RawFrameRecord] = deque(maxlen=3000)
        self._paused = False
        self._pending_lines: list[str] = []
        self._render_dirty = False
        self._last_count_text = "0 条"
        self._append_timer = QTimer(self)
        self._append_timer.setSingleShot(True)
        self._append_timer.setInterval(80)
        self._append_timer.timeout.connect(self._flush_pending_lines)

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.filter_mode_combo = QComboBox()
        self.filter_mode_combo.addItems(["全部记录", "仅广播记录", "仅异常记录"])
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤关键字")
        self.error_only_check = QCheckBox("仅异常帧")
        self.pause_button = QPushButton("暂停")
        self.pause_button.setCheckable(True)
        self.copy_button = QPushButton("复制")
        self.clear_button = QPushButton("清空")
        self.max_records_spin = QSpinBox()
        self.max_records_spin.setRange(200, 10000)
        self.max_records_spin.setValue(3000)
        self.count_label = QLabel(self._last_count_text)
        self.count_label.setProperty("muted", True)

        controls.addWidget(self.filter_mode_combo)
        controls.addWidget(self.filter_edit, 1)
        controls.addWidget(self.error_only_check)
        controls.addWidget(QLabel("保留上限"))
        controls.addWidget(self.max_records_spin)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.copy_button)
        controls.addWidget(self.clear_button)
        controls.addWidget(self.count_label)

        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.document().setMaximumBlockCount(5000)

        layout.addLayout(controls)
        layout.addWidget(self.viewer, 1)

        self.filter_mode_combo.currentTextChanged.connect(self.render)
        self.filter_edit.textChanged.connect(self.render)
        self.error_only_check.toggled.connect(self.render)
        self.pause_button.toggled.connect(self._set_paused)
        self.copy_button.clicked.connect(self._copy_visible_text)
        self.clear_button.clicked.connect(self.clear_records)
        self.max_records_spin.valueChanged.connect(self._update_max_records)

    def append_record(self, record: RawFrameRecord) -> None:
        self._records.append(record)
        self._trim_records()
        self._update_count_label()
        if self._paused:
            return
        if not self.isVisible():
            self._render_dirty = True
            return

        line = self._format_record(record)
        if not self._match_record(record, line):
            return

        self._pending_lines.append(line)
        if not self._append_timer.isActive():
            self._append_timer.start()

    def render(self) -> None:
        self._pending_lines.clear()
        if not self.isVisible():
            self._render_dirty = True
            return

        lines = []
        for record in self._records:
            text = self._format_record(record)
            if self._match_record(record, text):
                lines.append(text)
        self.viewer.setPlainText("\n".join(lines))
        cursor = self.viewer.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.viewer.setTextCursor(cursor)
        self._render_dirty = False

    def clear_records(self) -> None:
        self._records.clear()
        self._pending_lines.clear()
        self._render_dirty = False
        self.viewer.clear()
        self._update_count_label(force_text="0 条")

    def _copy_visible_text(self) -> None:
        QGuiApplication.clipboard().setText(self.viewer.toPlainText())

    def _set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.pause_button.setText("继续" if paused else "暂停")
        if paused:
            self._append_timer.stop()
            return
        self.render()

    def _update_max_records(self, value: int) -> None:
        self._records = deque(self._records, maxlen=max(200, int(value)))
        if self.isVisible():
            self.render()
        else:
            self._render_dirty = True

    def _trim_records(self) -> None:
        maxlen = max(200, int(self.max_records_spin.value()))
        if self._records.maxlen != maxlen:
            self._records = deque(self._records, maxlen=maxlen)

    def _match_record(self, record: RawFrameRecord, line: str) -> bool:
        keyword = self.filter_edit.text().strip().lower()
        if keyword and keyword not in line.lower():
            return False
        if self.error_only_check.isChecked() and record.level.upper() not in {"ERROR", "WARN"}:
            return False
        mode = self.filter_mode_combo.currentText()
        if mode == "仅广播记录":
            return "[FFF]" in line or "target=FFF" in line
        if mode == "仅异常记录":
            return record.level.upper() in {"ERROR", "WARN"} or "F," in record.text
        return True

    def _flush_pending_lines(self) -> None:
        if not self._pending_lines or self._paused:
            return
        if not self.isVisible():
            self._pending_lines.clear()
            self._render_dirty = True
            return
        cursor = self.viewer.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        has_existing_text = bool(self.viewer.toPlainText())
        for line in self._pending_lines:
            if has_existing_text:
                cursor.insertText("\n")
            cursor.insertText(line)
            has_existing_text = True
        self.viewer.setTextCursor(cursor)
        self.viewer.ensureCursorVisible()
        self._pending_lines.clear()
        self._render_dirty = False

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._render_dirty and not self._paused:
            QTimer.singleShot(0, self.render)

    def _update_count_label(self, *, force_text: str | None = None) -> None:
        text = force_text or f"{len(self._records)} 条"
        if text == self._last_count_text:
            return
        self._last_count_text = text
        self.count_label.setText(text)

    @staticmethod
    def _format_record(record: RawFrameRecord) -> str:
        return f"[{record.timestamp.strftime('%H:%M:%S.%f')[:-3]}] [{record.direction}] [{record.level}] {record.text}"
