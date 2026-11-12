import time
import unittest

from app.main import (
    PendingQuality,
    _store_quality_session,
    _format_duration,
    _quality_keyboard,
    main_menu_keyboard,
)
from app.quality import build_quality_choices


class QualityChoiceTests(unittest.TestCase):
    def setUp(self):
        self.formats = [
            {
                "format_id": "a",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 1_000_000,
            },
            {
                "format_id": "360",
                "height": 360,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "filesize": 4_000_000,
            },
            {
                "format_id": "720",
                "height": 720,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "filesize": 8_000_000,
            },
            {
                "format_id": "1080",
                "height": 1080,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "filesize": 45_000_000,
            },
        ]

    def test_builds_available_unique_quality_options(self):
        choices = build_quality_choices(self.formats, duration=100)
        self.assertEqual([item.label for item in choices], ["360p", "720p", "1080p"])
        self.assertEqual(choices[0].format_id, "360")
        self.assertEqual(choices[1].format_id, "720+a")
        self.assertEqual(choices[1].estimated_size, 9_000_000)
        self.assertEqual(choices[2].estimated_size, 46_000_000)

    def test_respects_maximum_height(self):
        choices = build_quality_choices(self.formats, duration=100, max_height=720)
        self.assertEqual([item.label for item in choices], ["360p", "720p"])

    def test_estimates_size_from_bitrate_and_duration(self):
        formats = [
            self.formats[0],
            {
                "format_id": "v",
                "height": 720,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "tbr": 800,
            },
        ]
        choice = build_quality_choices(formats, duration=100)[0]
        self.assertEqual(choice.estimated_size, 11_000_000)

    def test_keyboard_marks_oversized_choice(self):
        choices = build_quality_choices(self.formats, duration=100)
        keyboard = _quality_keyboard("abc", choices, 8_500_000)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertIn("🎬", buttons[0].text)
        self.assertIn("🚫", buttons[1].text)
        self.assertTrue(all(len(button.callback_data) <= 64 for button in buttons))


class SessionTests(unittest.TestCase):
    def test_main_menu_has_callback_buttons(self):
        keyboard = main_menu_keyboard()
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertEqual(set(callbacks), {"menu:quota", "menu:limits", "menu:help", "menu:about"})

    def test_formats_duration(self):
        self.assertEqual(_format_duration(65), "1:05")
        self.assertEqual(_format_duration(3665), "1:01:05")

    def test_removes_expired_sessions(self):
        expired = PendingQuality("url", 1, "title", [], time.monotonic() - 1000)
        sessions = {"old": expired}
        current = PendingQuality("url2", 2, "new", [], time.monotonic())
        _store_quality_session(sessions, current)
        self.assertNotIn("old", sessions)
        self.assertEqual(len(sessions), 1)


if __name__ == "__main__":
    unittest.main()
