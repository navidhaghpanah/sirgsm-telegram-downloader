from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yt_dlp

from app.downloader import MediaDownloadError

TARGET_HEIGHTS = (360, 480, 720, 1080)


@dataclass(frozen=True, slots=True)
class QualityChoice:
    height: int
    format_id: str
    estimated_size: int | None
    label: str
    kind: str = "video"
    audio_bitrate: int | None = None


@dataclass(frozen=True, slots=True)
class VideoDetails:
    title: str
    duration: int | None
    choices: list[QualityChoice]
    subtitles_available: bool = False


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    url: str
    title: str
    duration: int | None


@dataclass(frozen=True, slots=True)
class PlaylistDetails:
    title: str
    entries: list[PlaylistEntry]
    truncated: bool = False


def _ydl_options(cookies_file: str | None, proxy: str | None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 2,
        "skip_download": True,
    }
    if cookies_file:
        options["cookiefile"] = cookies_file
    if proxy:
        options["proxy"] = proxy
    return options


def probe_video(
    url: str,
    max_height: int = 1080,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> VideoDetails:
    options = _ydl_options(cookies_file, proxy)
    options["noplaylist"] = True
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc).lower()
        if "unsupported url" in message:
            raise MediaDownloadError("UNSUPPORTED_URL") from exc
        if any(word in message for word in ("private", "login", "cookies", "sign in")):
            raise MediaDownloadError("برای دریافت کیفیت‌های این ویدیو باید وارد حساب شوید یا محتوا خصوصی است.") from exc
        raise MediaDownloadError("کیفیت‌های ویدیو دریافت نشد؛ لینک عمومی و معتبر باشد.") from exc

    if not info:
        raise MediaDownloadError("اطلاعات ویدیو دریافت نشد.")
    if info.get("entries"):
        raise MediaDownloadError("PLAYLIST_DETECTED")

    duration = _optional_int(info.get("duration"))
    choices = build_quality_choices(info.get("formats") or [], duration, max_height)
    choices.extend(build_mp3_choices(duration))
    if not choices:
        raise MediaDownloadError("کیفیت قابل دانلودی برای این رسانه پیدا نشد.")

    subtitle_languages = set((info.get("subtitles") or {}).keys())
    subtitle_languages.update((info.get("automatic_captions") or {}).keys())
    return VideoDetails(
        title=str(info.get("title") or "رسانه"),
        duration=duration,
        choices=choices,
        subtitles_available=bool({"fa", "en", "en-US", "en-GB"} & subtitle_languages),
    )


def probe_playlist(
    url: str,
    max_items: int,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> PlaylistDetails:
    options = _ydl_options(cookies_file, proxy)
    options.update({
        "noplaylist": False,
        "extract_flat": True,
        "playlistend": max_items + 1,
        "ignoreerrors": True,
    })
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise MediaDownloadError("اطلاعات پلی‌لیست دریافت نشد.") from exc
    raw_entries = list((info or {}).get("entries") or [])
    entries: list[PlaylistEntry] = []
    for item in raw_entries[:max_items]:
        if not item:
            continue
        entry_url = item.get("url") or item.get("webpage_url")
        if not entry_url and item.get("id"):
            entry_url = f"https://www.youtube.com/watch?v={item['id']}"
        if not entry_url or not str(entry_url).startswith(("http://", "https://")):
            continue
        entries.append(
            PlaylistEntry(str(entry_url), str(item.get("title") or item.get("id") or "ویدیو"), _optional_int(item.get("duration")))
        )
    if not entries:
        raise MediaDownloadError("ویدیوی قابل دانلودی در این پلی‌لیست پیدا نشد.")
    return PlaylistDetails(
        title=str((info or {}).get("title") or "پلی‌لیست"),
        entries=entries,
        truncated=len(raw_entries) > max_items,
    )


def build_quality_choices(
    formats: list[dict[str, Any]], duration: int | None, max_height: int = 1080
) -> list[QualityChoice]:
    videos = [
        item for item in formats
        if item.get("format_id") and item.get("vcodec") != "none"
        and _optional_int(item.get("height"))
        and _optional_int(item.get("height")) <= max_height
        and not str(item.get("protocol", "")).startswith("mhtml")
    ]
    audios = [
        item for item in formats
        if item.get("format_id") and item.get("acodec") != "none" and item.get("vcodec") == "none"
    ]
    choices: list[QualityChoice] = []
    seen_selectors: set[str] = set()
    for target in TARGET_HEIGHTS:
        if target > max_height:
            continue
        candidates = [item for item in videos if int(item["height"]) <= target]
        if not candidates:
            continue
        highest = max(int(item["height"]) for item in candidates)
        candidates = [item for item in candidates if int(item["height"]) == highest]
        video = min(candidates, key=lambda item: _video_sort_key(item, duration))
        height = int(video["height"])
        video_size = _format_size(video, duration)
        if video.get("acodec") != "none":
            selector = str(video["format_id"])
            total_size = video_size
        else:
            if not audios:
                continue
            audio = min(audios, key=lambda item: _audio_sort_key(item, duration))
            selector = f"{video['format_id']}+{audio['format_id']}"
            audio_size = _format_size(audio, duration)
            total_size = video_size + audio_size if video_size is not None and audio_size is not None else None
        if selector in seen_selectors:
            continue
        seen_selectors.add(selector)
        choices.append(QualityChoice(height, selector, total_size, f"{height}p"))
    return choices


def build_mp3_choices(duration: int | None) -> list[QualityChoice]:
    choices = []
    for bitrate in (128, 320):
        estimated = int(bitrate * 1000 / 8 * duration) if duration else None
        choices.append(
            QualityChoice(
                height=0,
                format_id="bestaudio/best",
                estimated_size=estimated,
                label=f"MP3 {bitrate}kbps",
                kind="audio",
                audio_bitrate=bitrate,
            )
        )
    return choices


def _video_sort_key(item: dict[str, Any], duration: int | None) -> tuple:
    size = _format_size(item, duration)
    return (
        0 if item.get("ext") == "mp4" else 1,
        0 if item.get("acodec") != "none" else 1,
        size if size is not None else float("inf"),
        -(item.get("tbr") or 0),
    )


def _audio_sort_key(item: dict[str, Any], duration: int | None) -> tuple:
    size = _format_size(item, duration)
    return (
        0 if item.get("ext") in {"m4a", "mp4"} else 1,
        size if size is not None else float("inf"),
        -(item.get("abr") or item.get("tbr") or 0),
    )


def _format_size(item: dict[str, Any], duration: int | None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = _optional_int(item.get(key))
        if value and value > 0:
            return value
    bitrate = item.get("tbr") or item.get("vbr") or item.get("abr")
    if bitrate and duration:
        return int(float(bitrate) * 1000 / 8 * duration)
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
