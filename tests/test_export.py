from __future__ import annotations

from datetime import datetime
import tempfile
import unittest

from ygas_monitor.models import ParsedFrame
from ygas_monitor.services.export_service import export_frames_to_csv


class ExportTests(unittest.TestCase):
    def test_export_writes_csv(self) -> None:
        frame = ParsedFrame(
            timestamp=datetime.now(),
            raw="YGAS,001,0488.879,00.528,0.98,0.98,026.10,101.14,0001,2771",
            device_id="001",
            mode=1,
            fields={"co2_ppm": 488.879, "h2o_mmol": 0.528, "pressure_kpa": 101.14},
            status="0001",
            extras=[],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = export_frames_to_csv([frame], output_path=f"{temp_dir}\\sample.csv")
            content = path.read_text(encoding="utf-8-sig")

        self.assertTrue(path.name.endswith(".csv"))
        self.assertIn("co2_ppm", content)
        self.assertIn("488.879", content)


if __name__ == "__main__":
    unittest.main()
