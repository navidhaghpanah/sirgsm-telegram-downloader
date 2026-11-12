import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = {
            "BOT_TOKEN": "123456:test-token",
            "DOWNLOAD_DIR": str(Path(self.temp.name) / "downloads"),
            "DATABASE_PATH": str(Path(self.temp.name) / "bot.db"),
            "FILE_STORAGE_DIR": str(Path(self.temp.name) / "shared"),
            "BOT_API_ROOT": "",
            "TELEGRAM_API_ROOT": "",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_telegram_defaults(self):
        with patch.dict(os.environ, self.base, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.platform, "telegram")
        self.assertIsNone(settings.telegram_api_root)
        self.assertEqual(settings.daily_size_limit, 1024 * 1024 * 1024)

    def test_bale_api_and_independent_database(self):
        values = self.base | {
            "BOT_PLATFORM": "bale",
            "BOT_API_ROOT": "https://tapi.bale.ai",
            "DATABASE_PATH": str(Path(self.temp.name) / "bale.db"),
            "MAX_FILE_SIZE_MB": "49",
        }
        with patch.dict(os.environ, values, clear=False):
            settings = Settings.from_env()
        self.assertEqual(settings.platform, "bale")
        self.assertEqual(settings.telegram_api_root, "https://tapi.bale.ai")
        self.assertEqual(settings.database_path.name, "bale.db")
        self.assertEqual(settings.max_file_size, 49 * 1024 * 1024)

    def test_invalid_platform_is_rejected(self):
        with patch.dict(os.environ, self.base | {"BOT_PLATFORM": "other"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "BOT_PLATFORM"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
