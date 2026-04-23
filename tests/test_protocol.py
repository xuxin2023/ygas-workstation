from __future__ import annotations

import unittest

from ygas_monitor.protocols.ygas import (
    PARSE_MODE_AUTO,
    PARSE_MODE_FORCE_MODE1,
    PARSE_MODE_FORCE_MODE2,
    StreamBuffer,
    YGasProtocol,
)


class ProtocolTests(unittest.TestCase):
    MODE2_LINE = (
        "noise prefix YGAS,087,0479.572,05.198,0958.423,04.249,1.3030,1.3033,"
        "0.7888,0.7888,03322,04356,02631,002.18,002.31,103.97,0005,SIM,TRACE"
    )
    MODE1_LINE = "YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771"

    def test_parse_mode2_prefers_mode2_and_keeps_extras(self) -> None:
        parsed = YGasProtocol.parse_line(self.MODE2_LINE)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.mode, 2)
        self.assertEqual(parsed.device_id, "087")
        self.assertEqual(parsed.status, "0005")
        self.assertEqual(parsed.extras, ["SIM", "TRACE"])
        self.assertAlmostEqual(parsed.fields["co2_ppm"], 479.572, places=3)

    def test_parse_mode1_fallback(self) -> None:
        parsed = YGasProtocol.parse_line(self.MODE1_LINE)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.mode, 1)
        self.assertEqual(parsed.status, "0001")
        self.assertAlmostEqual(parsed.fields["pressure_kpa"], 101.14, places=2)

    def test_force_mode1_does_not_accept_mode2_frame(self) -> None:
        parsed = YGasProtocol.parse_line(self.MODE2_LINE, parse_mode=PARSE_MODE_FORCE_MODE1)
        self.assertIsNone(parsed)

    def test_force_mode2_does_not_fallback_to_mode1(self) -> None:
        parsed = YGasProtocol.parse_line(self.MODE1_LINE, parse_mode=PARSE_MODE_FORCE_MODE2)
        self.assertIsNone(parsed)

    def test_auto_parse_still_works(self) -> None:
        parsed = YGasProtocol.parse_line(self.MODE2_LINE, parse_mode=PARSE_MODE_AUTO)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.mode, 2)

    def test_ack_does_not_parse_as_data(self) -> None:
        self.assertTrue(YGasProtocol.is_ack("<YGAS,001,T>"))
        self.assertIsNone(YGasProtocol.parse_line("<YGAS,001,T>"))

    def test_stream_buffer_handles_half_frames(self) -> None:
        buffer = StreamBuffer()
        first = buffer.feed("YGAS,001,0488")
        second = buffer.feed(".879,00.528,0.98,0.98,026.10,101.14,0001,2771\r\n")

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        parsed = YGasProtocol.parse_line(second[0])
        self.assertIsNotNone(parsed)

    def test_stream_buffer_flushes_wrapped_ack_without_crlf(self) -> None:
        buffer = StreamBuffer()

        first = buffer.feed("<YGAS,012,")
        second = buffer.feed("T>")

        self.assertEqual(first, [])
        self.assertEqual(second, ["<YGAS,012,T>"])
        self.assertTrue(YGasProtocol.is_ack(second[0]))

    def test_split_stream_lines_supports_multiple_wrapped_records_without_crlf(self) -> None:
        lines = YGasProtocol.split_stream_lines("<YGAS,012,T><YGAS,012,2>")

        self.assertEqual(lines, ["<YGAS,012,T>", "<YGAS,012,2>"])

    def test_status_decode_marks_alarm_bits(self) -> None:
        decoded = YGasProtocol.decode_status("3009")
        active_bits = {item.bit for item in decoded if item.active}

        self.assertIn(0, active_bits)
        self.assertIn(3, active_bits)
        self.assertIn(12, active_bits)
        self.assertIn(13, active_bits)

    def test_parse_coefficient_reply(self) -> None:
        parsed = YGasProtocol.parse_coefficient_reply("<C0:65916.6,C1:-106614,C2:57735.1>")
        self.assertEqual(parsed, {"C0": 65916.6, "C1": -106614.0, "C2": 57735.1})

    def test_parse_query_replies(self) -> None:
        self.assertEqual(
            YGasProtocol.parse_serial_config_reply("<YGAS,012,115200,8,N,1>"),
            {"device_id": "012", "baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1},
        )
        self.assertEqual(YGasProtocol.parse_mode_value_reply("<YGAS,012,2>"), {"device_id": "012", "mode": 2})
        self.assertEqual(YGasProtocol.parse_identity_reply("<YGAS,012>"), {"device_id": "012"})
        self.assertEqual(
            YGasProtocol.parse_setting_value_reply("<YGAS,012,49>"),
            {"device_id": "012", "value": "49", "values": ["49"]},
        )

    def test_classify_line_distinguishes_ack_query_and_telemetry(self) -> None:
        self.assertEqual(YGasProtocol.classify_line("<YGAS,012,T>"), "ack")
        self.assertEqual(YGasProtocol.classify_line("<YGAS,012,2>"), "mode_value")
        self.assertEqual(YGasProtocol.classify_line("<YGAS,012,115200,8,N,1>"), "serial_config")
        self.assertEqual(YGasProtocol.classify_line(self.MODE2_LINE), "telemetry")


if __name__ == "__main__":
    unittest.main()
