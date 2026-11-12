from __future__ import annotations

import asyncio
import ipaddress
import logging
import mimetypes
import re
import socket
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit

import aiofiles
import aiohttp
import yt_dlp

logger = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
YTDLP_DOMAINS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com", "instagram.com",
    "tiktok.com", "x.com", "twitter.com", "facebook.com", "fb.watch",
    "soundcloud.com", "vimeo.com", "aparat.com", "twitch.tv",
    "dailymotion.com", "reddit.com", "streamable.com", "pinterest.com",
)
DIRECT_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".msi", ".apk", ".aab",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".mp4", ".mkv", ".webm",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".iso", ".dmg", ".deb",
}
CHUNK_SIZE = 1024 * 1024
ABSOLUTE_DOWNLOAD_LIMIT = 2 * 1024 * 1024 * 1024


class MediaDownloadError(Exception):
    """An error that can safely be shown to the bot user."""


class FileTooLargeError(MediaDownloadError):
    pass


class DownloadCancelled(MediaDownloadError):
    pass


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    stage: str
    downloaded: int
    total: int | None = None
    speed: float | None = None
    eta: int | None = None


ProgressCallback = Callable[[ProgressUpdate], None]


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    size: int
    title: str
    mime_type: str
    duration: int | None = None
    thumbnail: Path | None = None
    kind: str = "video"

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def is_video(self) -> bool:
        return self.kind == "video" and (
            self.mime_type.startswith("video/") or self.path.suffix.lower() in VIDEO_SUFFIXES
        )

    @property
    def is_audio(self) -> bool:
        return self.kind == "audio" or self.mime_type.startswith("audio/")


def extract_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text, flags=re.IGNORECASE)
    return match.group(0).rstrip(".,!،؛") if match else None


def uses_ytdlp(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in YTDLP_DOMAINS)


def should_probe(url: str, all_sites: bool = True) -> bool:
    if uses_ytdlp(url):
        return True
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in DIRECT_SUFFIXES:
        return False
    return all_sites


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("بایت", "کیلوبایت", "مگابایت", "گیگابایت"):
        if value < 1024 or unit == "گیگابایت":
            return f"{value:.1f} {unit}" if unit != "بایت" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} بایت"


async def download_media(
    url: str,
    directory: Path,
    max_size: int,
    timeout: int,
    ytdlp_max_height: int,
    cookies_file: str | None = None,
    proxy: str | None = None,
    format_id: str | None = None,
    audio_bitrate: int | None = None,
    subtitles: bool = False,
    auto_compress: bool = True,
    allow_oversize: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancel_event: asyncio.Event | None = None,
    force_direct: bool = False,
) -> DownloadedMedia:
    limit = ABSOLUTE_DOWNLOAD_LIMIT if allow_oversize else max_size
    if not force_direct and should_probe(url, all_sites=True):
        return await asyncio.to_thread(
            _download_with_ytdlp, url, directory, max_size, limit, timeout,
            ytdlp_max_height, cookies_file, proxy, format_id, audio_bitrate,
            subtitles, auto_compress, progress_callback, cancel_event,
        )
    return await _download_direct(url, directory, limit, timeout, progress_callback, cancel_event)


async def _download_direct(
    url: str,
    directory: Path,
    max_size: int,
    timeout: int,
    progress_callback: ProgressCallback | None,
    cancel_event: asyncio.Event | None,
) -> DownloadedMedia:
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=30, sock_read=120)
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=60)
    async with aiohttp.ClientSession(timeout=client_timeout, connector=connector) as session:
        current_url = url
        for redirect_number in range(6):
            await _validate_public_url(current_url)
            try:
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise MediaDownloadError("پاسخ تغییر مسیر سرور معتبر نیست.")
                        if redirect_number == 5:
                            raise MediaDownloadError("تعداد تغییر مسیرهای لینک بیش از حد مجاز است.")
                        current_url = urljoin(str(response.url), location)
                        continue
                    if response.status in {401, 403}:
                        raise MediaDownloadError("دسترسی به این فایل نیازمند ورود به حساب یا مجوز است.")
                    if response.status == 404:
                        raise MediaDownloadError("فایل پیدا نشد؛ لینک را بررسی کنید.")
                    if response.status >= 400:
                        raise MediaDownloadError(f"سرور فایل با خطای HTTP {response.status} پاسخ داد.")
                    content_length = response.content_length
                    if content_length is not None and content_length > max_size:
                        raise FileTooLargeError(
                            f"حجم فایل ({human_size(content_length)}) از سقف دریافت ({human_size(max_size)}) بیشتر است."
                        )
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        raise MediaDownloadError("این لینک یک صفحه‌ی وب است، نه فایل مستقیم.")
                    filename = _choose_filename(response, current_url, content_type)
                    destination = directory / filename
                    size = 0
                    async with aiofiles.open(destination, "wb") as file:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            _check_cancel(cancel_event)
                            size += len(chunk)
                            if size > max_size:
                                raise FileTooLargeError(
                                    f"حجم فایل از سقف دریافت ({human_size(max_size)}) بیشتر است."
                                )
                            await file.write(chunk)
                            _report(progress_callback, "download", size, content_length)
                    if size == 0:
                        raise MediaDownloadError("فایل دریافت‌شده خالی است.")
                    mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    kind = "audio" if mime_type.startswith("audio/") else "video" if mime_type.startswith("video/") else "document"
                    return DownloadedMedia(destination, size, filename, mime_type, kind=kind)
            except aiohttp.ClientError as exc:
                raise MediaDownloadError("اتصال به سرور فایل برقرار نشد یا قطع شد.") from exc
        raise MediaDownloadError("دریافت فایل به دلیل تغییر مسیرهای پی‌درپی متوقف شد.")


async def _validate_public_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise MediaDownloadError("لینک یا شماره‌ی پورت آن معتبر نیست.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MediaDownloadError("فقط لینک‌های معتبر HTTP و HTTPS پشتیبانی می‌شوند.")
    if parsed.username or parsed.password:
        raise MediaDownloadError("لینک‌های دارای نام کاربری یا رمز عبور پشتیبانی نمی‌شوند.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise MediaDownloadError("دسترسی به آدرس‌های محلی مجاز نیست.")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(
                hostname, parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = [ipaddress.ip_address(item[4][0]) for item in results]
        except (socket.gaierror, ValueError) as exc:
            raise MediaDownloadError("نام میزبان لینک قابل شناسایی نیست.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise MediaDownloadError("لینک به یک آدرس خصوصی یا غیرعمومی اشاره دارد.")


def _choose_filename(response: aiohttp.ClientResponse, url: str, content_type: str) -> str:
    disposition = response.content_disposition
    raw_name = disposition.filename if disposition else None
    if not raw_name:
        raw_name = unquote(Path(urlsplit(url).path).name)
    raw_name = re.sub(r"[\x00-\x1f/\\]", "_", raw_name or "").strip(" .") or "file"
    path = Path(raw_name)
    if not path.suffix:
        extension = mimetypes.guess_extension(content_type) if content_type else None
        path = path.with_suffix(extension or ".bin")
    stem = path.stem[:140].rstrip(" .") or "file"
    return f"{stem}{path.suffix[:20]}"


def _download_with_ytdlp(
    url: str,
    directory: Path,
    upload_size: int,
    download_limit: int,
    timeout: int,
    max_height: int,
    cookies_file: str | None,
    proxy: str | None,
    format_id: str | None,
    audio_bitrate: int | None,
    subtitles: bool,
    auto_compress: bool,
    progress_callback: ProgressCallback | None,
    cancel_event: asyncio.Event | None,
) -> DownloadedMedia:
    deadline = time.monotonic() + timeout

    def hook(status: dict) -> None:
        _check_cancel(cancel_event)
        if time.monotonic() > deadline:
            raise yt_dlp.utils.DownloadError("Download timed out")
        if status.get("status") == "downloading":
            _report(
                progress_callback, "download", int(status.get("downloaded_bytes") or 0),
                _optional_int(status.get("total_bytes") or status.get("total_bytes_estimate")),
                _optional_float(status.get("speed")), _optional_int(status.get("eta")),
            )

    def post_hook(status: dict) -> None:
        _check_cancel(cancel_event)
        if status.get("status") == "started":
            _report(progress_callback, "processing", 0, None)

    is_audio = audio_bitrate is not None
    postprocessors: list[dict] = []
    if is_audio:
        postprocessors.extend([
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(audio_bitrate)},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ])
    else:
        postprocessors.extend([
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ])
        if subtitles:
            postprocessors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})

    options: dict = {
        "outtmpl": str(directory / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "overwrites": True,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "max_filesize": download_limit,
        "format": format_id or (
            f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/"
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}]+bestaudio/best"
        ),
        "progress_hooks": [hook],
        "postprocessor_hooks": [post_hook],
        "postprocessors": postprocessors,
        "writethumbnail": True,
        "windowsfilenames": True,
    }
    if not is_audio:
        options["merge_output_format"] = "mp4"
    if subtitles and not is_audio:
        options.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["fa", "en", "en-US", "en-GB"],
            "subtitlesformat": "srt/vtt/best",
        })
    if cookies_file:
        options["cookiefile"] = str(Path(cookies_file).expanduser().resolve())
    if proxy:
        options["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).lower()
        if "cancelled" in message or "canceled" in message:
            raise DownloadCancelled("عملیات توسط کاربر لغو شد.") from exc
        if "max-filesize" in message or "file is larger" in message:
            raise FileTooLargeError(f"فایل از سقف دریافت ({human_size(download_limit)}) بزرگ‌تر است.") from exc
        if "timed out" in message or "timeout" in message:
            raise MediaDownloadError("زمان دانلود ویدیو بیش از حد مجاز شد.") from exc
        if any(word in message for word in ("private", "login", "cookies", "sign in")):
            raise MediaDownloadError("این محتوا عمومی نیست یا نیازمند ورود به حساب است.") from exc
        logger.warning("yt-dlp download failed: %s", exc)
        raise MediaDownloadError("دانلود رسانه انجام نشد. لینک باید مستقیم، عمومی و بدون محدودیت باشد.") from exc

    ignored = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp", ".description", ".srt", ".vtt"}
    candidates = [
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() not in ignored and not path.name.endswith(".part")
        and ".compressed" not in path.stem
    ]
    if not candidates:
        raise MediaDownloadError("فایل رسانه پس از دانلود پیدا نشد؛ احتمالاً ffmpeg لازم است.")
    path = max(candidates, key=lambda item: item.stat().st_size)
    size = path.stat().st_size
    title = str(info.get("title") or path.stem)
    duration = _optional_int(info.get("duration"))
    thumbnails = [
        item for item in directory.iterdir()
        if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    thumbnail = max(thumbnails, key=lambda item: item.stat().st_size) if thumbnails else None
    mime_type = "audio/mpeg" if is_audio else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = DownloadedMedia(
        path, size, title, mime_type, duration=duration, thumbnail=thumbnail,
        kind="audio" if is_audio else "video",
    )
    if size > upload_size and auto_compress and not is_audio and duration:
        media = _compress_video(media, upload_size, progress_callback, cancel_event)
    if media.size > download_limit:
        raise FileTooLargeError(f"حجم فایل از سقف دریافت ({human_size(download_limit)}) بیشتر است.")
    if media.size == 0:
        raise MediaDownloadError("فایل دانلودشده خالی است.")
    return media


def _compress_video(
    media: DownloadedMedia,
    max_size: int,
    progress_callback: ProgressCallback | None,
    cancel_event: asyncio.Event | None,
) -> DownloadedMedia:
    duration = max(1, int(media.duration or 0))
    audio_bps = 96_000
    video_bps = max(100_000, int((max_size * 0.94 * 8 - audio_bps * duration) / duration))
    output = media.path.with_name(media.path.stem + ".compressed.mp4")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media.path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", str(video_bps), "-maxrate", str(int(video_bps * 1.25)),
        "-bufsize", str(video_bps * 2), "-c:a", "aac", "-b:a", str(audio_bps),
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output),
    ]
    _report(progress_callback, "compress", 0, duration * 1_000_000)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        _check_cancel(cancel_event, process)
        if line.startswith("out_time_ms="):
            value = _optional_int(line.strip().split("=", 1)[1]) or 0
            _report(progress_callback, "compress", value, duration * 1_000_000)
    return_code = process.wait()
    if return_code != 0:
        error = process.stderr.read()[-1000:] if process.stderr else ""
        logger.warning("ffmpeg compression failed: %s", error)
        raise MediaDownloadError("فشرده‌سازی ویدیو انجام نشد.")
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaDownloadError("خروجی فشرده‌سازی ساخته نشد.")
    return replace(media, path=output, size=output.stat().st_size, mime_type="video/mp4")


def _check_cancel(event: asyncio.Event | None, process: subprocess.Popen | None = None) -> None:
    if event and event.is_set():
        if process and process.poll() is None:
            process.kill()
            process.wait()
        raise DownloadCancelled("عملیات توسط کاربر لغو شد.")


def _report(
    callback: ProgressCallback | None,
    stage: str,
    downloaded: int,
    total: int | None = None,
    speed: float | None = None,
    eta: int | None = None,
) -> None:
    if callback:
        callback(ProgressUpdate(stage, downloaded, total, speed, eta))


def _optional_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
