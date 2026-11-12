import unittest
from pathlib import Path

from app.downloader import (
    DownloadedMedia,
    MediaDownloadError,
    _validate_public_url,
    extract_url,
    human_size,
    uses_ytdlp,
)


class UrlTests(unittest.TestCase):
    def test_extracts_url_from_persian_message(self):
        text = "این را بگیر https://example.com/file.zip، ممنون"
        self.assertEqual(extract_url(text), "https://example.com/file.zip")

    def test_returns_none_without_url(self):
        self.assertIsNone(extract_url("سلام"))

    def test_youtube_and_instagram_detection(self):
        for url in (
            "https://www.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
            "https://www.instagram.com/reel/abc/",
        ):
            with self.subTest(url=url):
                self.assertTrue(uses_ytdlp(url))

    def test_lookalike_domain_is_not_trusted_as_youtube(self):
        self.assertFalse(uses_ytdlp("https://youtube.com.evil.example/video"))

    def test_direct_url_is_not_sent_to_ytdlp(self):
        self.assertFalse(uses_ytdlp("https://cdn.example.com/file.zip"))


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_http_scheme(self):
        with self.assertRaises(MediaDownloadError):
            await _validate_public_url("file:///etc/passwd")

    async def test_rejects_loopback_ipv4(self):
        with self.assertRaises(MediaDownloadError):
            await _validate_public_url("http://127.0.0.1/file")

    async def test_rejects_loopback_ipv6(self):
        with self.assertRaises(MediaDownloadError):
            await _validate_public_url("http://[::1]/file")

    async def test_rejects_private_network(self):
        with self.assertRaises(MediaDownloadError):
            await _validate_public_url("http://192.168.1.10/file")

    async def test_rejects_embedded_credentials(self):
        with self.assertRaises(MediaDownloadError):
            await _validate_public_url("https://user:pass@example.com/file")


class MediaTests(unittest.TestCase):
    def test_video_detection(self):
        media = DownloadedMedia(Path("movie.mp4"), 10, "movie", "application/octet-stream")
        self.assertTrue(media.is_video)

    def test_document_detection(self):
        media = DownloadedMedia(Path("archive.zip"), 10, "archive", "application/zip")
        self.assertFalse(media.is_video)

    def test_human_size(self):
        self.assertEqual(human_size(512), "512 بایت")
        self.assertEqual(human_size(1024), "1.0 کیلوبایت")
        self.assertEqual(human_size(1024 * 1024), "1.0 مگابایت")


if __name__ == "__main__":
    unittest.main()
