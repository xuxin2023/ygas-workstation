"""Monitoring metric calculations for the realtime dashboard."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
import math
from statistics import pstdev

from ..models import ParsedFrame


@dataclass(slots=True)
class MonitoringMetrics:
    receive_hz: float = 0.0
    latest_frame_age_s: float | None = None
    dropped_frames: int = 0
    stability_5s: float | None = None
    stability_30s: float | None = None
    temp_delta_c: float | None = None
    filter_bias: float | None = None


class MetricsTracker:
    def __init__(self, expected_hz: float = 10.0):
        self.expected_hz = max(0.1, float(expected_hz))
        self._timestamps: deque[float] = deque(maxlen=2000)
        self._co2_history: deque[tuple[float, float]] = deque(maxlen=2000)
        self._drop_count = 0
        self._last_ts: float | None = None

    def update_expected_hz(self, hz: float) -> None:
        self.expected_hz = max(0.1, float(hz))

    def push(self, frame: ParsedFrame) -> MonitoringMetrics:
        now = frame.timestamp.timestamp()
        self._timestamps.append(now)
        if self._last_ts is not None:
            gap = now - self._last_ts
            if gap > (2.2 / self.expected_hz):
                missing = max(1, int(round(gap * self.expected_hz)) - 1)
                self._drop_count += missing
        self._last_ts = now

        co2 = frame.fields.get("co2_ppm")
        if isinstance(co2, (int, float)):
            self._co2_history.append((now, float(co2)))
        return self.snapshot(frame)

    def snapshot(self, latest_frame: ParsedFrame | None) -> MonitoringMetrics:
        now = datetime.now().timestamp()
        last_1s = [ts for ts in self._timestamps if now - ts <= 1.0]
        latest_age = None
        temp_delta = None
        filter_bias = None
        if latest_frame is not None:
            latest_age = max(0.0, now - latest_frame.timestamp.timestamp())
            chamber = latest_frame.fields.get("chamber_temp_c")
            case = latest_frame.fields.get("case_temp_c")
            if isinstance(chamber, (int, float)) and isinstance(case, (int, float)):
                temp_delta = float(chamber) - float(case)
            co2_f = latest_frame.fields.get("co2_ratio_f")
            co2_raw = latest_frame.fields.get("co2_ratio_raw")
            if isinstance(co2_f, (int, float)) and isinstance(co2_raw, (int, float)):
                filter_bias = float(co2_f) - float(co2_raw)

        return MonitoringMetrics(
            receive_hz=float(len(last_1s)),
            latest_frame_age_s=latest_age,
            dropped_frames=self._drop_count,
            stability_5s=self._stability_window(now, 5.0),
            stability_30s=self._stability_window(now, 30.0),
            temp_delta_c=temp_delta,
            filter_bias=filter_bias,
        )

    def _stability_window(self, now: float, seconds: float) -> float | None:
        values = [value for ts, value in self._co2_history if now - ts <= seconds]
        if len(values) < 2:
            return None
        try:
            return float(pstdev(values))
        except Exception:
            return math.nan
