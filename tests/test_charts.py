from __future__ import annotations

from datetime import datetime
import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ygas_monitor.models import ParsedFrame
from ygas_monitor.ui.widgets.charts import (
    METRIC_TO_VIEW,
    RealtimeChartPanel,
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
        self.assertEqual(no_frames.placeholder_title, "等待实时数据")
        self.assertEqual(no_frames.placeholder_body, "请先连接设备或加载回放。")
        self.assertFalse(unavailable.show_plot)
        self.assertIn("当前图槽暂无可绘制数据", unavailable.placeholder_title)
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

    def test_metric_to_view_mapping_covers_primary_monitor_cards(self) -> None:
        self.assertEqual(METRIC_TO_VIEW["co2_ppm"], "co2_concentration")
        self.assertEqual(METRIC_TO_VIEW["h2o_ratio_delta"], "h2o_ratio_compare")
        self.assertEqual(METRIC_TO_VIEW["pressure_kpa"], "pressure_status")

    def test_nearest_time_index_uses_binary_search_boundaries(self) -> None:
        times = [0.5, 1.0, 2.5, 4.0]

        self.assertEqual(nearest_time_index(times, -1.0), 0)
        self.assertEqual(nearest_time_index(times, 5.0), 3)
        self.assertEqual(nearest_time_index(times, 2.2), 2)
        self.assertEqual(nearest_time_index(times, 0.8), 1)


class ChartWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _frame(*, mode: int = 2, offset: float = 0.0) -> ParsedFrame:
        if mode == 1:
            return ParsedFrame(
                timestamp=datetime(2026, 4, 22, 9, 0, 0),
                raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
                device_id="001",
                mode=1,
                status="0001",
                fields={
                    "co2_ppm": 488.879 + offset,
                    "h2o_mmol": 0.528 + offset,
                    "temperature_c": 26.10,
                    "pressure_kpa": 101.14,
                },
            )
        return ParsedFrame(
            timestamp=datetime(2026, 4, 22, 9, 0, 0),
            raw="YGAS,001,0479.572,05.198,0958.423,04.249,1.3030,1.3033,0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005",
            device_id="001",
            mode=2,
            status="0005",
            fields={
                "co2_ppm": 479.572 + offset,
                "h2o_mmol": 5.198 + offset,
                "temperature_c": 26.31,
                "pressure_kpa": 103.97,
            },
        )

    def test_single_point_frame_is_visible(self) -> None:
        panel = RealtimeChartPanel()
        try:
            panel.show()
            panel.add_frame(self._frame())
            panel.request_refresh(immediate=True)
            self.app.processEvents()

            self.assertEqual(panel._slot_states[0].plot_stack.currentIndex(), 1)
            self.assertEqual(panel._slot_states[1].plot_stack.currentIndex(), 1)
        finally:
            panel.close()
            self.app.processEvents()

    def test_default_chart_views_are_co2_and_h2o(self) -> None:
        panel = RealtimeChartPanel()
        try:
            panel.show()
            self.app.processEvents()

            self.assertEqual(panel._slot_view_ids, ["co2_concentration", "h2o_concentration"])
            self.assertIn("CO2 浓度", panel.slot_summary_text(0))
            self.assertIn("H2O 浓度", panel.slot_summary_text(1))
        finally:
            panel.close()
            self.app.processEvents()

    def test_chart_switches_from_placeholder_to_curve_after_first_frame(self) -> None:
        panel = RealtimeChartPanel()
        try:
            panel.show()
            panel.request_refresh(immediate=True)
            self.app.processEvents()
            self.assertEqual(panel._slot_states[0].plot_stack.currentIndex(), 0)
            self.assertEqual(panel._slot_states[1].plot_stack.currentIndex(), 0)

            panel.add_frame(self._frame())
            panel.request_refresh(immediate=True)
            self.app.processEvents()

            self.assertEqual(panel._slot_states[0].plot_stack.currentIndex(), 1)
            self.assertEqual(panel._slot_states[1].plot_stack.currentIndex(), 1)
        finally:
            panel.close()
            self.app.processEvents()

    def test_two_points_frame_is_visible(self) -> None:
        panel = RealtimeChartPanel()
        try:
            panel.show()
            panel.add_frame(self._frame(offset=0.0))
            panel.add_frame(self._frame(offset=0.5))
            panel.request_refresh(immediate=True)
            self.app.processEvents()

            self.assertEqual(panel._slot_states[0].plot_stack.currentIndex(), 1)
            self.assertEqual(panel._slot_states[1].plot_stack.currentIndex(), 1)
        finally:
            panel.close()
            self.app.processEvents()

    def test_single_and_two_point_curves_show_symbols(self) -> None:
        panel = RealtimeChartPanel()
        try:
            panel.show()
            panel.add_frame(self._frame(offset=0.0))
            panel.request_refresh(immediate=True)
            self.app.processEvents()

            upper_curve = panel._curves[(0, "co2_ppm")]
            lower_curve = panel._curves[(1, "h2o_mmol")]
            self.assertEqual(upper_curve.opts.get("symbol"), "o")
            self.assertEqual(lower_curve.opts.get("symbol"), "o")

            panel.add_frame(self._frame(offset=0.5))
            panel.request_refresh(immediate=True)
            self.app.processEvents()

            self.assertEqual(upper_curve.opts.get("symbol"), "o")
            self.assertEqual(lower_curve.opts.get("symbol"), "o")
        finally:
            panel.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
