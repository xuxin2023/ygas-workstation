from __future__ import annotations

from datetime import datetime
import math
import unittest

from ygas_monitor.models import ParsedFrame
from ygas_monitor.ui.widgets.charts import (
    build_display_value_map,
    build_time_axis_window,
    describe_group_state,
    filter_finite_points,
    friendly_field_name,
    group_field_order,
    nearest_time_index,
)


class ChartLogicTests(unittest.TestCase):
    def test_single_point_time_window_has_minimum_span(self) -> None:
        window = build_time_axis_window([12.0])

        self.assertIsNotNone(window)
        assert window is not None
        self.assertLessEqual(window.start, 10.8)
        self.assertGreaterEqual(window.end, 13.2)

    def test_short_time_window_adds_padding_for_two_points(self) -> None:
        window = build_time_axis_window([5.0, 5.2])

        self.assertIsNotNone(window)
        assert window is not None
        self.assertLess(window.start, 5.0)
        self.assertGreater(window.end, 5.2)
        self.assertGreater(window.end - window.start, 2.0)

    def test_filter_finite_points_ignores_nan_and_inf(self) -> None:
        times, values = filter_finite_points([0.0, 1.0, 2.0, 3.0], [1.5, math.nan, math.inf, 2.5])

        self.assertEqual(times, [0.0, 3.0])
        self.assertEqual(values, [1.5, 2.5])

    def test_friendly_field_name_uses_human_label(self) -> None:
        self.assertEqual(friendly_field_name("co2_ppm"), "CO2 浓度")
        self.assertEqual(friendly_field_name("co2_ratio_delta"), "CO2 滤波差值")
        self.assertEqual(friendly_field_name("pressure_kpa"), "压力")

    def test_group_state_distinguishes_empty_unavailable_and_few_points(self) -> None:
        no_frames = describe_group_state(
            total_frames=0,
            enabled_field_count=1,
            seen_field_count=0,
            visible_sample_count=0,
        )
        unavailable = describe_group_state(
            total_frames=5,
            enabled_field_count=2,
            seen_field_count=0,
            visible_sample_count=0,
        )
        single_point = describe_group_state(
            total_frames=5,
            enabled_field_count=2,
            seen_field_count=1,
            visible_sample_count=1,
        )

        self.assertFalse(no_frames.show_plot)
        self.assertIn("尚未收到任何帧", no_frames.placeholder_title)
        self.assertFalse(unavailable.show_plot)
        self.assertIn("当前图组暂无可绘制数据", unavailable.placeholder_title)
        self.assertIn("当前模式下", unavailable.banner_text)
        self.assertTrue(single_point.show_plot)
        self.assertIn("1 个有效点", single_point.banner_text)

    def test_build_display_value_map_derives_ratio_delta_and_status_numeric(self) -> None:
        frame = ParsedFrame(
            timestamp=datetime.now(),
            raw="sample",
            device_id="001",
            mode=2,
            fields={
                "co2_ratio_raw": 0.9812,
                "co2_ratio_f": 0.9804,
                "h2o_ratio_raw": 0.7780,
                "h2o_ratio_f": 0.7815,
            },
            status="000A",
        )

        values = build_display_value_map(frame)

        self.assertAlmostEqual(values["co2_ratio_delta"], -0.0008, places=6)
        self.assertAlmostEqual(values["h2o_ratio_delta"], 0.0035, places=6)
        self.assertEqual(values["status_numeric"], 10.0)

    def test_group_field_order_keeps_delta_visible_in_ratio_group(self) -> None:
        fields = group_field_order("比值对比")

        self.assertIn("co2_ratio_raw", fields)
        self.assertIn("co2_ratio_f", fields)
        self.assertIn("co2_ratio_delta", fields)
        self.assertIn("h2o_ratio_delta", fields)

    def test_nearest_time_index_uses_binary_search_boundaries(self) -> None:
        times = [0.5, 1.0, 2.5, 4.0]

        self.assertEqual(nearest_time_index(times, -1.0), 0)
        self.assertEqual(nearest_time_index(times, 5.0), 3)
        self.assertEqual(nearest_time_index(times, 2.2), 2)
        self.assertEqual(nearest_time_index(times, 0.8), 1)


if __name__ == "__main__":
    unittest.main()
