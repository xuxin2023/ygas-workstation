from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ygas_monitor.commanding.registry import (
    BROADCAST_FORBIDDEN,
    CommandDefinition,
    CommandParameter,
    CommandRegistry,
)


class RegistryTests(unittest.TestCase):
    def test_builtin_catalog_contains_manual_command_families(self) -> None:
        registry = CommandRegistry()
        command_ids = [item.command_id for item in registry.all_commands()]
        self.assertIn("SETCOM_QUERY", command_ids)
        self.assertIn("SENCO1", command_ids)
        self.assertIn("AVERAGE2_QUERY", command_ids)

    def test_build_preview_uses_target_device_id(self) -> None:
        registry = CommandRegistry()
        preview = registry.build_preview("READDATA", "001", {})
        self.assertEqual(preview, "READDATA,YGAS,001")

    def test_validate_target_blocks_broadcast_when_forbidden(self) -> None:
        registry = CommandRegistry()
        definition = registry.get("READDATA")
        ok, reason = registry.validate_target(definition, "FFF", broadcast_enabled=True)
        self.assertFalse(ok)
        self.assertIn("FFF", reason)

    def test_validate_command_rejects_out_of_range_ftd(self) -> None:
        registry = CommandRegistry()
        ok, reason = registry.validate_command("FTD", "001", {"hz": "999"})
        self.assertFalse(ok)
        self.assertIn("20", reason)

    def test_validate_command_rejects_invalid_mode_choice(self) -> None:
        registry = CommandRegistry()
        ok, reason = registry.validate_command("MODE", "001", {"mode": "7"})
        self.assertFalse(ok)
        self.assertIn("1, 2, 3", reason)

    def test_validate_command_rejects_invalid_target_id(self) -> None:
        registry = CommandRegistry()
        ok, reason = registry.validate_command("READDATA", "ABC", {})
        self.assertFalse(ok)
        self.assertIn("000-999", reason)

    def test_validate_command_accepts_valid_value(self) -> None:
        registry = CommandRegistry()
        ok, reason = registry.validate_command("FTD", "001", {"hz": "10"})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_broadcast_policy_requires_calibration_or_expert(self) -> None:
        registry = CommandRegistry()
        definition = registry.get("MODE")
        ok, _ = registry.validate_target(
            definition,
            "FFF",
            permission_level="CONFIG",
            broadcast_enabled=True,
        )
        self.assertFalse(ok)
        ok, _ = registry.validate_target(
            definition,
            "FFF",
            permission_level="CALIBRATION",
            broadcast_enabled=True,
        )
        self.assertTrue(ok)

    def test_profile_changes_average_semantics(self) -> None:
        registry = CommandRegistry()
        manual = registry.get("AVERAGE1", "manual_default")
        bench = registry.get("AVERAGE1", "bench_default")
        manual_query = registry.get("AVERAGE2_QUERY", "manual_default")
        bench_query = registry.get("AVERAGE2_QUERY", "bench_default")

        self.assertIn("气滤波窗口", manual.display_name)
        self.assertIn("气滤波窗口", manual.parameter_help)
        self.assertIn("水滤波窗口", bench.display_name)
        self.assertIn("水滤波窗口", bench.parameter_help)
        self.assertIn("水滤波窗口", manual_query.display_name)
        self.assertIn("气滤波窗口", bench_query.display_name)

    def test_default_target_uses_fff_for_write_and_explicit_target_for_read(self) -> None:
        registry = CommandRegistry()

        self.assertEqual(registry.default_target_for_command("MODE", "012"), "FFF")
        self.assertEqual(registry.default_target_for_command("SENCO1", "012"), "FFF")
        self.assertEqual(registry.default_target_for_command("GETCO", "012"), "012")
        self.assertEqual(registry.default_target_for_command("READDATA", "012"), "012")

    def test_senco_preview_is_normalized_to_standard_scientific_notation(self) -> None:
        registry = CommandRegistry()

        preview = registry.build_preview(
            "SENCO1",
            "FFF",
            {"coefficients": " 65916.6, -106614, 0, -0, 1 "},
        )

        self.assertEqual(
            preview,
            "SENCO1,YGAS,FFF,6.59166e04,-1.06614e05,0.00000e00,0.00000e00,1.00000e00",
        )

    def test_query_commands_have_specific_return_types(self) -> None:
        registry = CommandRegistry()
        self.assertEqual(registry.get("SETCOM_QUERY").return_type, "serial_config")
        self.assertEqual(registry.get("MODE_QUERY").return_type, "mode_value")
        self.assertEqual(registry.get("ID_QUERY").return_type, "identity")
        self.assertEqual(registry.get("AVERAGE1_QUERY").return_type, "setting_value")

    def test_settings_with_safe_readback_expose_readback_metadata(self) -> None:
        registry = CommandRegistry()

        self.assertEqual(registry.get("MODE").readback_command_id, "MODE_QUERY")
        self.assertEqual(registry.get("SETCOM").readback_command_id, "SETCOM_QUERY")
        self.assertEqual(registry.get("SENCO1").readback_command_id, "GETCO")
        self.assertIsNone(registry.get("SETILLUM").readback_command_id)
        self.assertIsNone(registry.get("SETCOMWAY").readback_command_id)

    def test_build_readback_preview_uses_query_command_and_explicit_target(self) -> None:
        registry = CommandRegistry()

        definition, payload = registry.build_readback_preview("SENCO3", "012")

        self.assertEqual(definition.command_id, "GETCO")
        self.assertEqual(payload, "GETCO,YGAS,012,3")

    def test_adapt_readback_result_prefills_senco_with_normalized_coefficients(self) -> None:
        registry = CommandRegistry()

        payload = registry.adapt_readback_result(
            "SENCO1",
            {"C0": 65916.6, "C1": -106614, "C2": 0},
        )

        self.assertEqual(
            payload["prefill_values"]["coefficients"],
            "6.59166e04,-1.06614e05,0.00000e00",
        )

    def test_adapt_readback_result_prefills_serial_settings(self) -> None:
        registry = CommandRegistry()

        payload = registry.adapt_readback_result(
            "SETCOM",
            {"device_id": "012", "baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1},
        )

        self.assertEqual(
            payload["prefill_values"],
            {"baudrate": "115200", "bytesize": "8", "parity": "N", "stopbits": "1"},
        )

    def test_build_target_and_readback_snapshots_support_structured_compare(self) -> None:
        registry = CommandRegistry()

        target = registry.build_target_snapshot("MODE", {"mode": "2"})
        actual = registry.build_readback_snapshot("MODE", {"device_id": "012", "mode": 2})

        self.assertEqual(target.summary, "目标工作模式：MODE2")
        self.assertEqual(actual.summary, "工作模式：MODE2")
        self.assertEqual(registry.diff_structured_snapshots(target, actual), [])

    def test_structured_compare_reports_mismatched_fields(self) -> None:
        registry = CommandRegistry()

        target = registry.build_target_snapshot(
            "SETCOM",
            {"baudrate": "115200", "bytesize": "8", "parity": "N", "stopbits": "1"},
        )
        actual = registry.build_readback_snapshot(
            "SETCOM",
            {"device_id": "012", "baudrate": 9600, "bytesize": 8, "parity": "E", "stopbits": 1},
        )

        self.assertEqual(registry.diff_structured_snapshots(target, actual), ["校验位", "波特率"])

    def test_custom_template_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = CommandRegistry(template_path=Path(temp_dir) / "templates.json")
            registry.upsert_custom_template(
                CommandDefinition(
                    command_id="XTEST",
                    code="XTEST",
                    display_name="测试命令",
                    family="用户自定义命令",
                    purpose="测试自定义模板持久化",
                    parameter_help="单参数",
                    response_help="返回 ACK",
                    risk_level="low",
                    supported_modes=["MODE1", "MODE2"],
                    broadcast_policy=BROADCAST_FORBIDDEN,
                    return_type="ack",
                    required_permission="EXPERT",
                    parameters=[CommandParameter(key="value", label="值")],
                    builtin=False,
                )
            )
            registry2 = CommandRegistry(template_path=Path(temp_dir) / "templates.json")
            self.assertEqual(registry2.get("XTEST").display_name, "测试命令")


if __name__ == "__main__":
    unittest.main()
