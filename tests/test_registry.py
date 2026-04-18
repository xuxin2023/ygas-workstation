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

        self.assertIn("CO2", manual.display_name)
        self.assertIn("CO2", manual.parameter_help)
        self.assertIn("H2O", bench.display_name)
        self.assertIn("H2O", bench.parameter_help)
        self.assertIn("H2O", manual_query.display_name)
        self.assertIn("CO2", bench_query.display_name)

    def test_query_commands_have_specific_return_types(self) -> None:
        registry = CommandRegistry()
        self.assertEqual(registry.get("SETCOM_QUERY").return_type, "serial_config")
        self.assertEqual(registry.get("MODE_QUERY").return_type, "mode_value")
        self.assertEqual(registry.get("ID_QUERY").return_type, "identity")
        self.assertEqual(registry.get("AVERAGE1_QUERY").return_type, "setting_value")

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
