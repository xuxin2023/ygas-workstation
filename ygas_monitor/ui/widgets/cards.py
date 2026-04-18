"""Reusable metric card widgets."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget


class MetricCard(QFrame):
    def __init__(
        self,
        title: str = "",
        *,
        compact: bool = False,
        dense: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("MetricCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame#MetricCard { background: #1a222a; border: 1px solid #2f4050; border-radius: 12px; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._last_numeric_value: float | None = None
        self._density = "dense" if dense else ("compact" if compact else "regular")
        self._last_style_key: tuple[str, str] | None = None
        self._last_text_key: tuple[str, str, str, str] | None = None

        layout = QVBoxLayout(self)
        if self._density == "dense":
            layout.setContentsMargins(8, 7, 8, 7)
            layout.setSpacing(3)
        elif self._density == "compact":
            layout.setContentsMargins(10, 9, 10, 9)
            layout.setSpacing(4)
        else:
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setProperty("muted", True)
        self.value_label = QLabel("--")
        self.value_label.setStyleSheet(self._value_style("#f7fbff"))
        self.unit_label = QLabel("")
        self.unit_label.setProperty("muted", True)
        self.detail_label = QLabel("")
        self.detail_label.setProperty("muted", True)
        self.detail_label.setWordWrap(True)
        self.title_label.setStyleSheet(self._title_style())
        self.unit_label.setStyleSheet(self._meta_style())
        self.detail_label.setStyleSheet(self._detail_style())

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

        text_key = (title, value, unit, detail)
        if text_key != self._last_text_key:
            self._last_text_key = text_key
            if self.title_label.text() != title:
                self.title_label.setText(title)
            if self.value_label.text() != value:
                self.value_label.setText(value)
            if self.unit_label.text() != unit:
                self.unit_label.setText(unit)
            if self.detail_label.text() != detail:
                self.detail_label.setText(detail)

        style_key = (border, value_color)
        if style_key != self._last_style_key:
            self._last_style_key = style_key
            self.setStyleSheet(
                f"QFrame#MetricCard {{ background: #1a222a; border: 1px solid {border}; border-radius: 12px; }}"
            )
            self.value_label.setStyleSheet(self._value_style(value_color))

    @staticmethod
    def _to_float(value: str) -> float | None:
        try:
            return float(str(value).strip())
        except Exception:
            return None

    def _value_style(self, color: str) -> str:
        if self._density == "dense":
            font_size = 18
        elif self._density == "compact":
            font_size = 22
        else:
            font_size = 30
        return f"font-size: {font_size}px; font-weight: 700; color: {color};"

    def _title_style(self) -> str:
        font_size = 11 if self._density == "dense" else 12
        return f"font-size: {font_size}px;"

    def _meta_style(self) -> str:
        font_size = 10 if self._density == "dense" else 11
        return f"font-size: {font_size}px;"

    def _detail_style(self) -> str:
        font_size = 10 if self._density == "dense" else 11
        return f"font-size: {font_size}px; line-height: 1.2;"


class MetricCardGrid(QWidget):
    def __init__(
        self,
        rows: int = 2,
        columns: int = 4,
        *,
        compact: bool = False,
        dense: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._cards: list[MetricCard] = []
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6 if dense else 10)

        for index in range(rows * columns):
            card = MetricCard(compact=compact, dense=dense)
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
