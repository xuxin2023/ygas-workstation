"""Reusable metric card widgets."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QMenu, QSizePolicy, QVBoxLayout, QWidget

from ..theme_tokens import get_theme_tokens


class MetricCard(QFrame):
    slot_requested = Signal(str, int)

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
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._last_numeric_value: float | None = None
        self._density = "dense" if dense else ("compact" if compact else "regular")
        self._last_style_key: tuple[str, str] | None = None
        self._last_text_key: tuple[str, str, str, str] | None = None
        self._metric_key = ""
        self._theme_name = "dark"
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

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
        self.apply_theme(self._theme_name)

    def update_card(
        self,
        title: str,
        value: str,
        unit: str = "",
        detail: str = "",
        severity: str = "normal",
        metric_key: str = "",
    ) -> None:
        self._metric_key = metric_key
        current_numeric = self._to_float(value)
        highlight = "normal"
        if current_numeric is not None and self._last_numeric_value is not None:
            if abs(current_numeric - self._last_numeric_value) > 1e-9:
                highlight = "changed"
        self._last_numeric_value = current_numeric

        tokens = get_theme_tokens(self._theme_name)
        border = tokens.border_strong
        value_color = tokens.text
        if severity == "alarm":
            border = tokens.danger
            value_color = tokens.danger
        elif severity == "warn":
            border = tokens.warning
            value_color = tokens.warning
        elif highlight == "changed":
            border = tokens.accent
            value_color = tokens.accent

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
                f"QFrame#MetricCard {{ background: {tokens.card_bg}; border: 1px solid {border}; border-radius: 12px; }}"
            )
            self.value_label.setStyleSheet(self._value_style(value_color))

    def apply_theme(self, theme_name: str) -> None:
        self._theme_name = theme_name
        tokens = get_theme_tokens(theme_name)
        self._last_style_key = None
        self.setStyleSheet(
            f"QFrame#MetricCard {{ background: {tokens.card_bg}; border: 1px solid {tokens.border_strong}; border-radius: 12px; }}"
        )
        self.value_label.setStyleSheet(self._value_style(tokens.text))

    def _show_context_menu(self, position) -> None:
        if not self._metric_key:
            return
        menu = QMenu(self)
        upper_action = QAction("显示到上图", menu)
        lower_action = QAction("显示到下图", menu)
        upper_action.triggered.connect(lambda: self.slot_requested.emit(self._metric_key, 0))
        lower_action.triggered.connect(lambda: self.slot_requested.emit(self._metric_key, 1))
        menu.addAction(upper_action)
        menu.addAction(lower_action)
        menu.exec(self.mapToGlobal(position))

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
    slot_requested = Signal(str, int)

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
            card.slot_requested.connect(self.slot_requested.emit)

    def update_items(self, items: list[tuple[Any, ...]]) -> None:
        for index, card in enumerate(self._cards):
            if index < len(items):
                title, value, unit, detail, *rest = items[index]
                severity = str(rest[0]) if rest else "normal"
                metric_key = str(rest[1]) if len(rest) > 1 else ""
                card.update_card(str(title), str(value), str(unit), str(detail), severity=severity, metric_key=metric_key)
                card.show()
            else:
                card.hide()

    def apply_theme(self, theme_name: str) -> None:
        for card in self._cards:
            card.apply_theme(theme_name)
