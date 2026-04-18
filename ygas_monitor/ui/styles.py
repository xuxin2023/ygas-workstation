"""Qt stylesheet for an industrial workstation look."""

from __future__ import annotations


def build_stylesheet() -> str:
    return """
    QWidget {
        background: #151a1f;
        color: #e7edf4;
        font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
        font-size: 12px;
    }
    QMainWindow, QTabWidget::pane, QGroupBox {
        background: #11161b;
    }
    QGroupBox {
        border: 1px solid #2b3947;
        border-radius: 10px;
        margin-top: 10px;
        padding-top: 8px;
        font-weight: 600;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: #9fd3ff;
    }
    QPushButton {
        background: #22303c;
        border: 1px solid #34506a;
        border-radius: 8px;
        padding: 6px 10px;
    }
    QPushButton:hover {
        background: #2a3d4f;
    }
    QPushButton:pressed {
        background: #1f2d39;
    }
    QPushButton[accent="true"] {
        background: #174f7d;
        border-color: #2f79b8;
    }
    QPushButton[danger="true"] {
        background: #612629;
        border-color: #9f4348;
    }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit, QListWidget, QTableWidget {
        background: #1a222a;
        border: 1px solid #30404f;
        border-radius: 8px;
        padding: 4px;
        selection-background-color: #2369a0;
    }
    QLabel[muted="true"] {
        color: #92a0ad;
    }
    QLabel[risk="low"] {
        color: #7edbb5;
    }
    QLabel[risk="medium"] {
        color: #ffd27d;
    }
    QLabel[risk="high"], QLabel[risk="critical"] {
        color: #ff8e8e;
    }
    QLabel[broadcast="true"] {
        color: #ff8e8e;
        font-weight: 700;
    }
    QLabel[warning="true"] {
        color: #ffd27d;
        background: #2a2414;
        border: 1px solid #6b5726;
        border-radius: 8px;
        padding: 6px 8px;
    }
    QLabel[state="disconnected"] {
        color: #cfd9e4;
    }
    QLabel[state="connecting"] {
        color: #ffd27d;
        font-weight: 700;
    }
    QLabel[state="connected"] {
        color: #7edbb5;
        font-weight: 700;
    }
    QLabel[state="fault"] {
        color: #ff8e8e;
        font-weight: 700;
    }
    QLabel[state="replay"] {
        color: #9fd3ff;
        font-weight: 700;
    }
    QLineEdit[invalid="true"], QComboBox[invalid="true"] {
        border: 1px solid #c45454;
        background: #2b1c1c;
    }
    QHeaderView::section {
        background: #202c36;
        color: #d9e2ea;
        border: none;
        padding: 6px;
    }
    QTabBar::tab {
        background: #1a232c;
        border: 1px solid #2d3b49;
        padding: 8px 12px;
        margin-right: 4px;
        border-top-left-radius: 8px;
        border-top-right-radius: 8px;
    }
    QTabBar::tab:selected {
        background: #21313d;
        color: #9fd3ff;
    }
    QScrollArea {
        border: none;
    }
    QSlider::groove:horizontal {
        height: 6px;
        background: #22303c;
        border-radius: 3px;
    }
    QSlider::handle:horizontal {
        background: #74b3ff;
        width: 14px;
        margin: -4px 0;
        border-radius: 7px;
    }
    """
