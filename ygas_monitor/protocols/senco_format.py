"""Helpers for validating and formatting SENCO coefficient payloads."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Iterable

_SCI_RE = re.compile(r"^(?P<mantissa>-?\d+\.\d{5})e(?P<sign>[+-]?)(?P<exp>\d+)$")


def normalize_senco_coefficient(value: str) -> str:
    text = str(value or "").strip()
    if text == "":
        raise ValueError("系数项不能为空。")
    try:
        numeric = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"系数 {text!r} 不是有效数值。") from exc

    if not numeric.is_finite():
        raise ValueError("系数必须是有限数值。")
    if numeric == 0:
        return "0.00000e00"

    rendered = format(numeric, ".5e").lower()
    match = _SCI_RE.match(rendered)
    if not match:
        raise ValueError(f"无法格式化系数 {text!r}。")

    sign = "-" if match.group("sign") == "-" else ""
    exponent = int(match.group("exp"))
    return f"{match.group('mantissa')}e{sign}{exponent:02d}"


def normalize_senco_coefficients(values: str | Iterable[str]) -> list[str]:
    if isinstance(values, str):
        parts = [part.strip() for part in values.split(",")]
    else:
        parts = [str(part or "").strip() for part in values]

    if not parts or all(part == "" for part in parts):
        raise ValueError("请至少输入 1 个系数。")
    if any(part == "" for part in parts):
        raise ValueError("系数列表中存在空项，请检查逗号和空格。")
    if len(parts) > 6:
        raise ValueError("SENCO 最多支持 6 个系数。")

    return [normalize_senco_coefficient(part) for part in parts]


def normalize_senco_input(values: str | Iterable[str]) -> str:
    return ",".join(normalize_senco_coefficients(values))
