import tempfile
import time
import unittest
from pathlib import Path

from app.database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.db")
        await self.db.connect()
        await self.db.touch_user(42, "tester", "Test")

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_quota_and_recording(self):
        allowed, _, _, _, count, size = await self.db.quota_status(42, 10, 1000)
        self.assertTrue(allowed)
        self.assertEqual((count, size), (10, 1000))
        await self.db.record_download(42, 400)
        self.assertTrue(await self.db.ensure_daily_capacity(42, 500, 1000))
        self.assertFalse(await self.db.ensure_daily_capacity(42, 700, 1000))

    async def test_block_and_custom_quota(self):
        await self.db.set_blocked(42, True)
        allowed, reason, *_ = await self.db.quota_status(42, 10, 1000)
        self.assertFalse(allowed)
        self.assertIn("مسدود", reason)
        await self.db.set_blocked(42, False)
        await self.db.set_quota(42, 20, 5000)
        _, _, count, size, _, _ = await self.db.quota_status(42, 10, 1000)
        self.assertEqual((count, size), (20, 5000))

    async def test_file_cache_roundtrip(self):
        key = self.db.cache_key("https://example.com", "720", "video")
        await self.db.save_cached_file(key, "https://example.com", "720", "fid", "uid", "video", "Title", 123)
        cached = await self.db.get_cached_file(key)
        self.assertEqual(cached.file_id, "fid")
        self.assertEqual(cached.size, 123)
        await self.db.delete_cached_file(key)
        self.assertIsNone(await self.db.get_cached_file(key))

    async def test_shared_file_expiry(self):
        path = Path(self.temp.name) / "shared.bin"
        path.write_bytes(b"abc")
        await self.db.save_shared_file("token", path, "shared.bin", 3, int(time.time()) + 60)
        shared = await self.db.get_shared_file("token")
        self.assertEqual(shared.filename, "shared.bin")
        await self.db.save_shared_file("expired", path, "old.bin", 3, int(time.time()) - 1)
        self.assertIsNone(await self.db.get_shared_file("expired"))
        self.assertEqual(len(await self.db.expired_shared_files()), 1)


if __name__ == "__main__":
    unittest.main()
