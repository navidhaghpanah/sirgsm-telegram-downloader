from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

TEHRAN = ZoneInfo("Asia/Tehran")


@dataclass(frozen=True, slots=True)
class CachedFile:
    file_id: str
    file_unique_id: str
    file_type: str
    title: str
    size: int


@dataclass(frozen=True, slots=True)
class SharedFile:
    token: str
    path: Path
    filename: str
    size: int
    expires_at: int


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA foreign_keys=ON")
        await self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                blocked INTEGER NOT NULL DEFAULT 0,
                usage_date TEXT NOT NULL,
                daily_count INTEGER NOT NULL DEFAULT 0,
                daily_bytes INTEGER NOT NULL DEFAULT 0,
                daily_limit INTEGER,
                daily_byte_limit INTEGER,
                total_downloads INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_cache (
                cache_key TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                format_id TEXT,
                file_id TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                title TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cache_created ON file_cache(created_at);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                quality TEXT,
                status TEXT NOT NULL,
                size INTEGER,
                error TEXT,
                created_at INTEGER NOT NULL,
                finished_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS shared_files (
                token TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shared_expiry ON shared_files(expires_at);
            """
        )
        await self.connection.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    @property
    def db(self) -> aiosqlite.Connection:
        if not self.connection:
            raise RuntimeError("Database is not connected")
        return self.connection

    async def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        now = int(time.time())
        today = datetime.now(TEHRAN).date().isoformat()
        await self.db.execute(
            """
            INSERT INTO users (user_id, username, first_name, usage_date, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=COALESCE(excluded.username, users.username),
                first_name=COALESCE(excluded.first_name, users.first_name),
                last_seen=excluded.last_seen,
                usage_date=CASE WHEN users.usage_date != excluded.usage_date
                    THEN excluded.usage_date ELSE users.usage_date END,
                daily_count=CASE WHEN users.usage_date != excluded.usage_date
                    THEN 0 ELSE users.daily_count END,
                daily_bytes=CASE WHEN users.usage_date != excluded.usage_date
                    THEN 0 ELSE users.daily_bytes END
            """,
            (user_id, username, first_name, today, now, now),
        )
        await self.db.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cursor:
            return await cursor.fetchone()

    async def quota_status(
        self, user_id: int, default_count: int, default_bytes: int
    ) -> tuple[bool, str, int, int, int, int]:
        user = await self.get_user(user_id)
        if not user:
            return True, "", default_count, default_bytes, 0, 0
        if user["blocked"]:
            return False, "دسترسی شما توسط مدیر مسدود شده است.", 0, 0, 0, 0
        count_limit = user["daily_limit"] or default_count
        byte_limit = user["daily_byte_limit"] or default_bytes
        remaining_count = max(0, count_limit - user["daily_count"])
        remaining_bytes = max(0, byte_limit - user["daily_bytes"])
        if remaining_count <= 0:
            return False, "سهمیه‌ی تعداد دانلود امروز شما تمام شده است.", count_limit, byte_limit, 0, remaining_bytes
        if remaining_bytes <= 0:
            return False, "سهمیه‌ی حجم دانلود امروز شما تمام شده است.", count_limit, byte_limit, remaining_count, 0
        return True, "", count_limit, byte_limit, remaining_count, remaining_bytes

    async def record_download(self, user_id: int, size: int) -> None:
        await self.db.execute(
            """
            UPDATE users SET daily_count=daily_count+1, daily_bytes=daily_bytes+?,
                total_downloads=total_downloads+1, total_bytes=total_bytes+?
            WHERE user_id=?
            """,
            (size, size, user_id),
        )
        await self.db.commit()

    async def ensure_daily_capacity(self, user_id: int, size: int, default_bytes: int) -> bool:
        user = await self.get_user(user_id)
        if not user:
            return True
        limit = user["daily_byte_limit"] or default_bytes
        return user["daily_bytes"] + size <= limit

    async def set_blocked(self, user_id: int, blocked: bool) -> None:
        await self.touch_user(user_id, None, None)
        await self.db.execute("UPDATE users SET blocked=? WHERE user_id=?", (int(blocked), user_id))
        await self.db.commit()

    async def set_quota(self, user_id: int, count: int, size: int) -> None:
        await self.touch_user(user_id, None, None)
        await self.db.execute(
            "UPDATE users SET daily_limit=?, daily_byte_limit=? WHERE user_id=?",
            (count, size, user_id),
        )
        await self.db.commit()

    async def list_users(self, limit: int = 50) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
        ) as cursor:
            return list(await cursor.fetchall())

    async def stats(self) -> dict[str, int]:
        queries = {
            "users": "SELECT COUNT(*) FROM users",
            "blocked": "SELECT COUNT(*) FROM users WHERE blocked=1",
            "downloads": "SELECT COALESCE(SUM(total_downloads),0) FROM users",
            "bytes": "SELECT COALESCE(SUM(total_bytes),0) FROM users",
            "cached": "SELECT COUNT(*) FROM file_cache",
            "jobs": "SELECT COUNT(*) FROM jobs",
        }
        result: dict[str, int] = {}
        for key, query in queries.items():
            async with self.db.execute(query) as cursor:
                row = await cursor.fetchone()
                result[key] = int(row[0])
        return result

    @staticmethod
    def cache_key(url: str, format_id: str, mode: str, subtitles: bool = False) -> str:
        raw = f"{url}\n{format_id}\n{mode}\n{int(subtitles)}".encode()
        return hashlib.sha256(raw).hexdigest()

    async def get_cached_file(self, key: str) -> CachedFile | None:
        async with self.db.execute("SELECT * FROM file_cache WHERE cache_key=?", (key,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return CachedFile(
            file_id=row["file_id"],
            file_unique_id=row["file_unique_id"],
            file_type=row["file_type"],
            title=row["title"],
            size=row["size"],
        )

    async def save_cached_file(
        self,
        key: str,
        url: str,
        format_id: str,
        file_id: str,
        file_unique_id: str,
        file_type: str,
        title: str,
        size: int,
    ) -> None:
        await self.db.execute(
            """
            INSERT OR REPLACE INTO file_cache
            (cache_key,url,format_id,file_id,file_unique_id,file_type,title,size,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (key, url, format_id, file_id, file_unique_id, file_type, title, size, int(time.time())),
        )
        await self.db.commit()

    async def delete_cached_file(self, key: str) -> None:
        await self.db.execute("DELETE FROM file_cache WHERE cache_key=?", (key,))
        await self.db.commit()

    async def create_job(self, job_id: str, user_id: int, url: str, quality: str) -> None:
        await self.db.execute(
            "INSERT INTO jobs (id,user_id,url,quality,status,created_at) VALUES (?,?,?,?,?,?)",
            (job_id, user_id, url, quality, "queued", int(time.time())),
        )
        await self.db.commit()

    async def finish_job(
        self, job_id: str, status: str, size: int | None = None, error: str | None = None
    ) -> None:
        await self.db.execute(
            "UPDATE jobs SET status=?,size=?,error=?,finished_at=? WHERE id=?",
            (status, size, error, int(time.time()), job_id),
        )
        await self.db.commit()

    async def recent_jobs(self, limit: int = 30) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return list(await cursor.fetchall())

    async def save_shared_file(
        self, token: str, path: Path, filename: str, size: int, expires_at: int
    ) -> None:
        await self.db.execute(
            "INSERT INTO shared_files (token,path,filename,size,expires_at,created_at) VALUES (?,?,?,?,?,?)",
            (token, str(path), filename, size, expires_at, int(time.time())),
        )
        await self.db.commit()

    async def get_shared_file(self, token: str) -> SharedFile | None:
        async with self.db.execute("SELECT * FROM shared_files WHERE token=?", (token,)) as cursor:
            row = await cursor.fetchone()
        if not row or row["expires_at"] < int(time.time()):
            return None
        return SharedFile(
            token=row["token"], path=Path(row["path"]), filename=row["filename"],
            size=row["size"], expires_at=row["expires_at"]
        )

    async def expired_shared_files(self) -> list[SharedFile]:
        now = int(time.time())
        async with self.db.execute("SELECT * FROM shared_files WHERE expires_at<?", (now,)) as cursor:
            rows = await cursor.fetchall()
        return [
            SharedFile(row["token"], Path(row["path"]), row["filename"], row["size"], row["expires_at"])
            for row in rows
        ]

    async def delete_shared_file(self, token: str) -> None:
        await self.db.execute("DELETE FROM shared_files WHERE token=?", (token,))
        await self.db.commit()
