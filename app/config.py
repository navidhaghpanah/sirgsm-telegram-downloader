from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    download_dir: Path
    database_path: Path
    file_storage_dir: Path
    max_file_size: int
    max_concurrent_jobs: int
    request_timeout: int
    ytdlp_max_height: int
    allowed_user_ids: frozenset[int]
    admin_ids: frozenset[int]
    cookies_file: str | None
    ytdlp_proxy: str | None
    telegram_api_root: str | None
    log_level: str
    daily_download_limit: int
    daily_size_limit: int
    public_base_url: str | None
    file_link_ttl: int
    web_enabled: bool
    web_host: str
    web_port: int
    admin_panel_token: str
    force_join_channel: str | None
    max_playlist_items: int
    auto_compress: bool
    ytdlp_all_sites: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

        max_mb = _env_float("MAX_FILE_SIZE_MB", 49)
        if not 0.1 <= max_mb <= 2000:
            raise RuntimeError("MAX_FILE_SIZE_MB باید بین 0.1 و 2000 باشد.")
        max_jobs = _bounded_int("MAX_CONCURRENT_JOBS", 2, 1, 20)
        timeout = _bounded_int("DOWNLOAD_TIMEOUT_SECONDS", 1800, 30, 86400)
        max_height = _bounded_int("YTDLP_MAX_HEIGHT", 1080, 144, 4320)
        daily_count = _bounded_int("DAILY_DOWNLOAD_LIMIT", 10, 1, 100000)
        daily_mb = _env_float("DAILY_SIZE_LIMIT_MB", 500)
        if daily_mb <= 0:
            raise RuntimeError("DAILY_SIZE_LIMIT_MB باید بزرگ‌تر از صفر باشد.")
        link_ttl = _bounded_int("FILE_LINK_TTL_SECONDS", 3600, 60, 604800)
        web_port = _bounded_int("WEB_PORT", 8080, 1, 65535)
        playlist_items = _bounded_int("MAX_PLAYLIST_ITEMS", 5, 1, 50)

        download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser().resolve()
        database_path = Path(os.getenv("DATABASE_PATH", "./bot.db")).expanduser().resolve()
        storage_dir = Path(os.getenv("FILE_STORAGE_DIR", "./shared_files")).expanduser().resolve()
        for directory in (download_dir, database_path.parent, storage_dir):
            directory.mkdir(parents=True, exist_ok=True)

        cookies_file = os.getenv("YTDLP_COOKIES_FILE", "").strip() or None
        if cookies_file and not Path(cookies_file).expanduser().is_file():
            raise RuntimeError("فایل مشخص‌شده در YTDLP_COOKIES_FILE پیدا نشد.")

        base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None
        if base_url and not base_url.startswith(("https://", "http://")):
            raise RuntimeError("PUBLIC_BASE_URL باید با http یا https شروع شود.")

        return cls(
            bot_token=token,
            download_dir=download_dir,
            database_path=database_path,
            file_storage_dir=storage_dir,
            max_file_size=int(max_mb * 1024 * 1024),
            max_concurrent_jobs=max_jobs,
            request_timeout=timeout,
            ytdlp_max_height=max_height,
            allowed_user_ids=_env_ids("ALLOWED_USER_IDS"),
            admin_ids=_env_ids("ADMIN_IDS"),
            cookies_file=cookies_file,
            ytdlp_proxy=os.getenv("YTDLP_PROXY", "").strip() or None,
            telegram_api_root=os.getenv("TELEGRAM_API_ROOT", "").strip() or None,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            daily_download_limit=daily_count,
            daily_size_limit=int(daily_mb * 1024 * 1024),
            public_base_url=base_url,
            file_link_ttl=link_ttl,
            web_enabled=_env_bool("WEB_ENABLED", False),
            web_host=os.getenv("WEB_HOST", "0.0.0.0"),
            web_port=web_port,
            admin_panel_token=os.getenv("ADMIN_PANEL_TOKEN", "").strip() or secrets.token_urlsafe(32),
            force_join_channel=os.getenv("FORCE_JOIN_CHANNEL", "").strip() or None,
            max_playlist_items=playlist_items,
            auto_compress=_env_bool("AUTO_COMPRESS", True),
            ytdlp_all_sites=_env_bool("YTDLP_ALL_SITES", True),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} باید یک عدد صحیح باشد.") from exc


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _env_int(name, default)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} باید بین {minimum} و {maximum} باشد.")
    return value


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} باید یک عدد باشد.") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "").strip()
    try:
        return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise RuntimeError(f"{name} باید فهرست عددهای جداشده با ویرگول باشد.") from exc
