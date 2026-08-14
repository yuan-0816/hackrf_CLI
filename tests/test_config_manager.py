import json
import tempfile
import unittest
from pathlib import Path

from utils.config_manager import ConfigManager


class ConfigManagerPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.manager = ConfigManager(str(self.config_path))
        self.manager.preset_add("home", 25.0, 121.0, 10.0)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_update_selected_fields(self):
        self.assertTrue(self.manager.preset_update("home", lat=25.5))
        self.assertEqual(
            self.manager.preset_get("home"),
            {"lat": 25.5, "lon": 121.0, "alt": 10.0},
        )

    def test_update_can_rename_preset(self):
        self.assertTrue(self.manager.preset_update("home", new_name="office"))
        self.assertIsNone(self.manager.preset_get("home"))
        self.assertIsNotNone(self.manager.preset_get("office"))

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIn("office", saved["presets"])

    def test_update_rejects_duplicate_name(self):
        self.manager.preset_add("office", 24.0, 120.0, 5.0)
        with self.assertRaisesRegex(ValueError, "Preset 已存在"):
            self.manager.preset_update("home", new_name="office")

    def test_update_missing_preset_returns_false(self):
        self.assertFalse(self.manager.preset_update("missing", lat=25.5))


if __name__ == "__main__":
    unittest.main()
