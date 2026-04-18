"""Reusable metric card widgets."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class MetricCard(QFrame):
    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MetricCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame#MetricCard { background: #1a222a; border: 1px solid #2f4050; border-radius: 12px; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._last_numeric_value: float | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setProperty("muted", True)
        self.value_label = QLabel("--")
        self.value_label.setStyleSheet("font-size: 30px; font-weight: 700; color: #f7fbff;")
        self.unit_label = QLabel("")
        self.unit_label.setProperty("muted", True)
        self.detail_label = QLabel("")
        self.detail_label.setProperty("muted", True)
        self.detail_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.unit_label)
        layout.addWidget(self.detail_label)
        layout.addStretch(1)

    def update_card(
        self,
        title: str,
        value: str,
        unit: str = "",
        detail: str = "",
        severity: str = "normal",
    ) -> None:
        self.title_label.setText(title)
        self.value_label.setText(value)
        self.unit_label.setText(unit)
        self.detail_label.setText(detail)

        current_numeric = self._to_float(value)
        highlight = "normal"
        if current_numeric is not None and self._last_numeric_value is not None:
            if abs(current_numeric - self._last_numeric_value) > 1e-9:
                highlight = "changed"
        self._last_numeric_value = current_numeric

        border = "#2f4050"
        value_color = "#f7fbff"
        if severity == "alarm":
            border = "#a84444"
            value_color = "#ffb0b0"
        elif severity == "warn":
            border = "#8d6a2f"
            value_color = "#ffd27d"
        elif highlight == "changed":
            border = "#2f79b8"
            value_color = "#9fd3ff"

        self.setStyleSheet(
            f"QFrame#MetricCard {{ background: #1a222a; border: 1px solid {border}; border-radius: 12px; }}"
        )
        self.value_label.setStyleSheet(f"font-size: 30px; font-weight: 700; color: {value_color};")

    @staticmethod
    def _to_float(value: str) -> float | None:
        try:
            return float(str(value).strip())
        except Exception:
            return None


class MetricCardGrid(QWidget):
    def __init__(self, rows: int = 2, columns: int = 4, parent: QWidget | None = None):
        super().__init__(parent)
        self._cards: list[MetricCard] = []
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        for index in range(rows * columns):
            card = MetricCard()
            row = index // columns
            column = index % columns
            layout.addWidget(card, row, column)
            self._cards.append(card)

    def update_items(self, items: list[tuple[Any, ...]]) -> None:
        for index, card in enumerate(self._cards):
            if index < len(items):
                title, value, unit, detail, *rest = items[index]
                severity = str(rest[0]) if rest else "normal"
                card.update_card(str(title), str(value), str(unit), str(detail), severity=severity)
                card.show()
            else:
                card.hide()
