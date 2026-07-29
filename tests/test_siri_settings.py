from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from ai_mime.provider_settings import read_siri_enabled, write_siri_enabled, register_siri_shortcuts, unregister_siri_shortcuts


class SiriSettingsTests(unittest.TestCase):
    def test_default_siri_enabled_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "user_config.yml"
            self.assertTrue(read_siri_enabled(config_path))

    def test_write_and_read_siri_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "user_config.yml"
            write_siri_enabled(False, config_path)
            self.assertFalse(read_siri_enabled(config_path))

            write_siri_enabled(True, config_path)
            self.assertTrue(read_siri_enabled(config_path))

    def test_register_and_unregister_shortcuts_callable(self) -> None:
        self.assertIn(register_siri_shortcuts(), (True, False))
        self.assertIn(unregister_siri_shortcuts(), (True, False))


if __name__ == "__main__":
    unittest.main()
