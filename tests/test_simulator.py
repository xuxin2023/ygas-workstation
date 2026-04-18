from __future__ import annotations

import time
import unittest

from ygas_monitor.models import SerialSettings
from ygas_monitor.protocols.ygas import YGasProtocol
from ygas_monitor.serial.simulator import SimulatorTransport


class SimulatorTests(unittest.TestCase):
    def test_passive_readdata_returns_parseable_frame(self) -> None:
        transport = SimulatorTransport(SerialSettings(port="SIMULATOR"))
        transport.open()
        transport.write_line("READDATA,YGAS,FFF")
        payload = transport.read_available().decode("ascii", errors="ignore")
        lines = YGasProtocol.split_stream_lines(payload)
        parsed = YGasProtocol.parse_line(lines[-1])

        self.assertTrue(lines)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn(parsed.mode, {1, 2})

    def test_mode_switch_and_stream_generation(self) -> None:
        transport = SimulatorTransport(SerialSettings(port="SIMULATOR"))
        transport.open()
        transport.write_line("MODE,YGAS,FFF,1")
        transport.write_line("SETCOMWAY,YGAS,FFF,1")
        transport.read_available()
        time.sleep(0.15)
        payload = transport.read_available().decode("ascii", errors="ignore")
        lines = [line for line in YGasProtocol.split_stream_lines(payload) if not YGasProtocol.is_ack(line)]

        self.assertTrue(lines)
        parsed = YGasProtocol.parse_line(lines[-1], parse_mode="FORCE_MODE1")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.mode, 1)

    def test_query_commands_return_parseable_payloads(self) -> None:
        transport = SimulatorTransport(SerialSettings(port="SIMULATOR"))
        transport.open()

        transport.write_line("SETCOM,YGAS,001")
        serial_reply = transport.read_available().decode("ascii", errors="ignore")
        serial_lines = YGasProtocol.split_stream_lines(serial_reply)
        self.assertIsNotNone(YGasProtocol.parse_serial_config_reply(serial_lines[-1]))

        transport.write_line("MODE,YGAS,001")
        mode_reply = transport.read_available().decode("ascii", errors="ignore")
        mode_lines = YGasProtocol.split_stream_lines(mode_reply)
        self.assertEqual(YGasProtocol.parse_mode_value_reply(mode_lines[-1]), {"device_id": "001", "mode": 2})

        transport.write_line("ID,YGAS,001")
        id_reply = transport.read_available().decode("ascii", errors="ignore")
        id_lines = YGasProtocol.split_stream_lines(id_reply)
        self.assertEqual(YGasProtocol.parse_identity_reply(id_lines[-1]), {"device_id": "001"})

        transport.write_line("AVERAGE1,YGAS,001")
        average_reply = transport.read_available().decode("ascii", errors="ignore")
        average_lines = YGasProtocol.split_stream_lines(average_reply)
        self.assertEqual(
            YGasProtocol.parse_setting_value_reply(average_lines[-1]),
            {"device_id": "001", "value": "49", "values": ["49"]},
        )

    def test_mode_three_returns_failure_and_does_not_change_internal_mode(self) -> None:
        transport = SimulatorTransport(SerialSettings(port="SIMULATOR"))
        transport.open()

        transport.write_line("MODE,YGAS,001,1")
        transport.read_available()

        transport.write_line("MODE,YGAS,001,3")
        payload = transport.read_available().decode("ascii", errors="ignore")
        lines = YGasProtocol.split_stream_lines(payload)
        ack = YGasProtocol.parse_ack(lines[-1])
        self.assertIsNotNone(ack)
        assert ack is not None
        self.assertFalse(ack.ok)

        transport.write_line("MODE,YGAS,001")
        mode_reply = transport.read_available().decode("ascii", errors="ignore")
        mode_lines = YGasProtocol.split_stream_lines(mode_reply)
        self.assertEqual(YGasProtocol.parse_mode_value_reply(mode_lines[-1]), {"device_id": "001", "mode": 1})


if __name__ == "__main__":
    unittest.main()
