"""Qt stylesheet builder for GasAxis Studio."""

from __future__ import annotations

from .theme_tokens import get_theme_tokens


def build_stylesheet(theme: str) -> str:
    tokens = get_theme_tokens(theme)
    return f"""
    QWidget {{
        background: {tokens.window_bg};
        color: {tokens.text};
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 12px;
    }}
    QMainWindow, QTabWidget::pane, QGroupBox, QDialog, QMenu, QToolBar {{
        background: {tokens.surface_bg};
    }}
    QToolBar {{
        border: none;
        spacing: 6px;
        padding: 4px;
    }}
    QToolButton {{
        background: {tokens.elevated_bg};
        border: 1px solid {tokens.border};
        border-radius: 8px;
        padding: 6px 10px;
    }}
    QToolButton:hover {{
        background: {tokens.card_bg};
        border-color: {tokens.border_strong};
    }}
    QGroupBox {{
        border: 1px solid {tokens.border};
        border-radius: 10px;
        margin-top: 10px;
        padding-top: 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: {tokens.accent};
    }}
    QPushButton {{
        background: {tokens.elevated_bg};
        border: 1px solid {tokens.border_strong};
        border-radius: 8px;
        padding: 6px 10px;
    }}
    QPushButton:hover {{
        background: {tokens.card_bg};
    }}
    QPushButton:pressed {{
        background: {tokens.input_bg};
    }}
    QPushButton[accent="true"] {{
        background: {tokens.accent_strong};
        border-color: {tokens.accent};
        color: {tokens.text};
    }}
    QPushButton[danger="true"] {{
        background: {tokens.warning_bg};
        border-color: {tokens.danger};
        color: {tokens.danger};
    }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QTextBrowser, QListWidget, QTableWidget {{
        background: {tokens.input_bg};
        border: 1px solid {tokens.border};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {tokens.selection_bg};
    }}
    QTextBrowser {{
        line-height: 1.5;
    }}
    QLabel[muted="true"] {{
        color: {tokens.text_muted};
    }}
    QLabel[accent="true"] {{
        color: {tokens.accent};
        font-weight: 700;
    }}
    QLabel[risk="low"] {{
        color: {tokens.success};
    }}
    QLabel[risk="medium"] {{
        color: {tokens.warning};
    }}
    QLabel[risk="high"], QLabel[risk="critical"] {{
        color: {tokens.danger};
    }}
    QLabel[broadcast="true"] {{
        color: {tokens.danger};
        font-weight: 700;
    }}
    QLabel[warning="true"] {{
        color: {tokens.warning};
        background: {tokens.warning_bg};
        border: 1px solid {tokens.warning_border};
        border-radius: 8px;
        padding: 6px 8px;
    }}
    QLabel[state="disconnected"] {{
        color: {tokens.text};
    }}
    QLabel[state="connecting"] {{
        color: {tokens.warning};
        font-weight: 700;
    }}
    QLabel[state="connected"] {{
        color: {tokens.success};
        font-weight: 700;
    }}
    QLabel[state="fault"] {{
        color: {tokens.danger};
        font-weight: 700;
    }}
    QLabel[state="replay"] {{
        color: {tokens.accent};
        font-weight: 700;
    }}
    QLineEdit[invalid="true"], QComboBox[invalid="true"] {{
        border: 1px solid {tokens.danger};
        background: {tokens.warning_bg};
    }}
    QHeaderView::section {{
        background: {tokens.elevated_bg};
        color: {tokens.text};
        border: none;
        padding: 6px;
    }}
    QTabBar::tab {{
        background: {tokens.input_bg};
        border: 1px solid {tokens.border};
        padding: 8px 12px;
        margin-right: 4px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
    }}
    QTabBar::tab:selected {{
        background: {tokens.elevated_bg};
        color: {tokens.accent};
        border-color: {tokens.border_strong};
    }}
    QScrollArea {{
        border: none;
    }}
    QSlider::groove:horizontal {{
        height: 6px;
        background: {tokens.elevated_bg};
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        background: {tokens.accent};
        width: 14px;
        margin: -4px 0;
        border-radius: 7px;
    }}
    """
