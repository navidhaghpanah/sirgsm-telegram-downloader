import unittest

from app.downloader import ProgressUpdate
from app.processor import _progress_text


class ProgressTests(unittest.TestCase):
    def test_download_progress_has_percent_and_sizes(self):
        text = _progress_text(ProgressUpdate("download", 50, 100, 10, 5), "720p")
        self.assertIn("50%", text)
        self.assertIn("720p", text)
        self.assertIn("سرعت", text)

    def test_unknown_total_does_not_divide_by_zero(self):
        text = _progress_text(ProgressUpdate("download", 50, None), None)
        self.assertNotIn("%", text)


if __name__ == "__main__":
    unittest.main()
