"""Theme tokens shared by widgets, charts, and stylesheet generation."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

THEME_DARK = "dark"
THEME_LIGHT = "light"
THEME_SYSTEM = "system"
DEFAULT_THEME = THEME_DARK
THEME_OPTIONS = (THEME_DARK, THEME_LIGHT, THEME_SYSTEM)

THEME_LABELS = {
    THEME_DARK: "深色",
    THEME_LIGHT: "浅色",
    THEME_SYSTEM: "跟随系统",
}


@dataclass(frozen=True)
class ThemeTokens:
    name: str
    window_bg: str
    surface_bg: str
    elevated_bg: str
    input_bg: str
    card_bg: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    accent: str
    accent_strong: str
    success: str
    warning: str
    danger: str
    warning_bg: str
    warning_border: str
    selection_bg: str
    chart_bg: str
    chart_grid: str
    chart_axis: str
    chart_text: str
    chart_placeholder_title: str
    chart_placeholder_body: str
    chart_cursor_text: str
    chart_legend_bg: str
    chart_legend_border: str
    chart_symbol_outline: str
    field_colors: dict[str, str]


_DARK_FIELD_COLORS = {
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
    "status_numeric": "#d5dde6",
}

_LIGHT_FIELD_COLORS = {
    "co2_ppm": "#187a57",
    "co2_density": "#0f8a58",
    "h2o_mmol": "#1d65cf",
    "h2o_density": "#3c57c7",
    "co2_ratio_raw": "#b76a06",
    "co2_ratio_f": "#d97706",
    "co2_ratio_delta": "#c2410c",
    "h2o_ratio_raw": "#6d4bc2",
    "h2o_ratio_f": "#7c3aed",
    "h2o_ratio_delta": "#c02664",
    "ref_signal": "#0f766e",
    "co2_signal": "#0f766e",
    "h2o_signal": "#0284c7",
    "temperature_c": "#b45309",
    "chamber_temp_c": "#dc2626",
    "case_temp_c": "#d97706",
    "pressure_kpa": "#5b8c00",
    "active_alarm_count": "#c62828",
    "status_numeric": "#475569",
}

_TOKENS = {
    THEME_DARK: ThemeTokens(
        name=THEME_DARK,
        window_bg="#151a1f",
        surface_bg="#11161b",
        elevated_bg="#16202a",
        input_bg="#1a222a",
        card_bg="#1a222a",
        border="#2b3947",
        border_strong="#34506a",
        text="#e7edf4",
        text_muted="#92a0ad",
        accent="#74b3ff",
        accent_strong="#174f7d",
        success="#7edbb5",
        warning="#ffd27d",
        danger="#ff8e8e",
        warning_bg="#2a2414",
        warning_border="#6b5726",
        selection_bg="#2369a0",
        chart_bg="#11161b",
        chart_grid="#324354",
        chart_axis="#44596d",
        chart_text="#dce7f2",
        chart_placeholder_title="#f7fbff",
        chart_placeholder_body="#9fb0c0",
        chart_cursor_text="#dce7f2",
        chart_legend_bg="#16202acc",
        chart_legend_border="#34506a",
        chart_symbol_outline="#f7fbff",
        field_colors=_DARK_FIELD_COLORS,
    ),
    THEME_LIGHT: ThemeTokens(
        name=THEME_LIGHT,
        window_bg="#f3f6fa",
        surface_bg="#ffffff",
        elevated_bg="#eef3f9",
        input_bg="#ffffff",
        card_bg="#f7fafc",
        border="#cfd9e4",
        border_strong="#9eb1c5",
        text="#16202a",
        text_muted="#5f7083",
        accent="#0f6db8",
        accent_strong="#dceefe",
        success="#0f8a58",
        warning="#b76a06",
        danger="#c62828",
        warning_bg="#fff6dd",
        warning_border="#e7c46a",
        selection_bg="#cfe5fb",
        chart_bg="#ffffff",
        chart_grid="#d8e0e8",
        chart_axis="#8ca0b3",
        chart_text="#22303c",
        chart_placeholder_title="#1d2a35",
        chart_placeholder_body="#5f7083",
        chart_cursor_text="#22303c",
        chart_legend_bg="#ffffffdd",
        chart_legend_border="#c4d1de",
        chart_symbol_outline="#ffffff",
        field_colors=_LIGHT_FIELD_COLORS,
    ),
}


def normalize_theme_choice(theme: str | None) -> str:
    choice = str(theme or DEFAULT_THEME).strip().lower()
    return choice if choice in THEME_OPTIONS else DEFAULT_THEME


def resolve_theme_choice(theme: str | None) -> str:
    choice = normalize_theme_choice(theme)
    if choice != THEME_SYSTEM:
        return choice
    app = QGuiApplication.instance()
    if app is None:
        return DEFAULT_THEME
    try:
        scheme = app.styleHints().colorScheme()
    except Exception:
        return DEFAULT_THEME
    return THEME_DARK if scheme == Qt.ColorScheme.Dark else THEME_LIGHT


def theme_label(theme: str | None) -> str:
    return THEME_LABELS.get(normalize_theme_choice(theme), THEME_LABELS[DEFAULT_THEME])


def get_theme_tokens(theme: str | None) -> ThemeTokens:
    return _TOKENS[resolve_theme_choice(theme)]
